#!/usr/bin/env python3
"""Tally for blind model judging: turns raw per-item judge verdicts into one `model_verdicts[]` entry.

Pure standard library (no numpy/Pillow), so every rule here is proven wherever the repo-hygiene tests run.

THE RULE THIS FILE EXISTS FOR: no path lets an unjudged, stale, incomplete, biased, self-judged or hidden result
read as `usable`. `usable` is true only when EVERY reason below is absent, and a winner is only ever shown on a
usable entry (otherwise `preference.winner` is `UNUSABLE` and the computed one is parked in `withheldWinner`).

THE RULES (DESIGN.md AMENDMENT 2 B4, hardened in round 2)
    * Each real unit was judged TWICE with left/right swapped. Map each answer back to a SUBJECT. If the two
      orderings name different subjects the judge followed the SLOT, not the picture: the vote is a FLIP and
      is DISCARDED. A tie in one ordering and a pick in the other is a tie-split and is also discarded.
    * The NEGATIVE CONTROL (same image both sides) must be answered `tie` in EVERY control item. A control that
      is missing, invalid, or answered against the wrong image is INCOMPLETE, not passed.
    * The POSITIVE CONTROL (an image against a large known degradation of itself) must be answered for the
      ORIGINAL in every item. A judge that ties it, or prefers the degraded picture, cannot see; its preference
      is unusable. A session with no positive control is unusable (POSITIVE_CONTROL_NOT_RUN).
    * IMAGE BINDING: a verdict is valid only if its imageSha256 equals the pair image the answer key recorded AND
      the image on disk still hashes to it AND the item id is the one derived from that digest. Anything else
      is IMAGE_CHANGED_SINCE_BUILD / ITEM_ID_NOT_BOUND_TO_IMAGE, and the session is unusable.
    * COMPLETENESS: every key item needs a valid verdict (INCOMPLETE_ITEMS otherwise), the results may not name
      items the key does not have (RESULTS_CONTAIN_UNKNOWN_ITEMS), and at least judge_validity.min_consistent_units
      real units must be consistent (TOO_FEW_CONSISTENT_UNITS).
    * SLOT BIAS: among non-tie answers the left/right split is tested (exact two-sided binomial). Because every
      unit appears in both orders, a judge that follows the PICTURE splits exactly 50/50, so a lopsided split
      is slot preference. Below slot_bias.min_choices the verdict is INSUFFICIENT_N, never "no bias".
    * A verdict quoting a different rubric digest than the session's frozen one is rejected, not scored.
    * The judge may not be the producer or the hub (look_judges.assert_not_producer), and the guard having not
      been applied is itself a reason (PRODUCER_GUARD_NOT_APPLIED).
    * Every threshold comes from the config the caller passes; there is no built-in default to fall back to.
    * Metrics gate; this entry only refines (bus TRAPS s8). `usable` says whether the judge's preference may be
      read at all.

OUTPUT FIELD NAMES follow the dual-venue receipt's `model_verdicts[]` element: judgeId, model, family,
rubricSha256, imageSha256s[], orderSeed, slotTally, scores, preference.
"""
import math

import look_config
import look_judges
import look_pairs

SCHEMA_VERDICT = "mlv-app/model-verdict/v1"
SCHEMA_ITEM_VERDICT = "mlv-app/look-judge-item-verdict/v1"

PREFERENCES = ("left", "right", "tie")
UNUSABLE_WINNER = "UNUSABLE"
_HEX64 = frozenset("0123456789abcdef")


def _is_sha256(value):
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX64


def validate_verdict(verdict):
    """Return a list of problems (empty = valid). Strict: integers 1-5, skin may be null, preference exact."""
    problems = []
    if not isinstance(verdict, dict):
        return ["verdict is not an object"]
    for side in ("left", "right"):
        scores = verdict.get(side)
        if not isinstance(scores, dict):
            problems.append(f"{side} scores missing")
            continue
        for criterion in look_config.CRITERIA:
            if criterion not in scores:
                problems.append(f"{side}.{criterion} missing")
                continue
            value = scores[criterion]
            if value is None:
                if criterion not in look_config.NULLABLE_CRITERIA:
                    problems.append(f"{side}.{criterion} may not be null")
            elif isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
                problems.append(f"{side}.{criterion} must be an integer 1-5, got {value!r}")
        extra = set(scores) - set(look_config.CRITERIA)
        if extra:
            problems.append(f"{side} has unknown criteria {sorted(extra)}")
    if verdict.get("preference") not in PREFERENCES:
        problems.append(f"preference must be one of {PREFERENCES}, got {verdict.get('preference')!r}")
    return problems


def binomial_two_sided_p(k, n):
    """Exact two-sided p for k successes of n at p=0.5 (doubling the smaller tail, capped at 1)."""
    if n <= 0:
        return 1.0
    tail = min(k, n - k)
    p = sum(math.comb(n, i) for i in range(tail + 1)) / float(2 ** n)
    return min(1.0, 2.0 * p)


def _subject_of(item, preference):
    if preference == "tie":
        return "tie"
    return item["left"]["subject"] if preference == "left" else item["right"]["subject"]


def _empty_slots():
    return {"left": 0, "right": 0, "tie": 0}


def _check_key(answer_key, session, current_image_sha256):
    """Structural + image-binding checks of the key against the session and the images on disk.
    Returns (reasons, details, bad_image_items) where bad_image_items is the set of itemIds whose image cannot be trusted."""
    reasons, details, bad = [], {}, set()
    seed = answer_key.get("orderSeed")
    if seed != session.get("orderSeed"):
        reasons.append("SEED_MISMATCH")
    digests, unbound, no_digest = [], [], []
    for item in answer_key["items"]:
        sha = item.get("pairImageSha256")
        if not _is_sha256(sha):
            no_digest.append(item["itemId"])
            bad.add(item["itemId"])
            continue
        digests.append(sha)
        if look_pairs.derive_item_id(seed, item["unitId"], item["order"], sha) != item["itemId"]:
            unbound.append(item["itemId"])
            bad.add(item["itemId"])
    subjects = set(answer_key.get("subjects") or [])
    malformed = []
    for item in answer_key["items"]:
        left, right = item["left"]["subject"], item["right"]["subject"]
        kind = item["kind"]
        if kind == "real":
            ok = left != right and {left, right} <= subjects
        elif kind == "control":
            ok = left == right and left in subjects
        elif kind == "positive_control":
            ok = {left, right} == {look_pairs.POSITIVE_ORIGINAL, look_pairs.POSITIVE_DEGRADED}
        else:
            ok = False
        if not ok:
            malformed.append(item["itemId"])
    if len(subjects) != 2:
        malformed.append("<subjects>")
    if malformed:
        reasons.append("KEY_SUBJECTS_MALFORMED")
        details["itemsWithMalformedSubjects"] = malformed
    if no_digest:
        reasons.append("KEY_ITEM_WITHOUT_IMAGE_DIGEST")
        details["itemsWithoutImageDigest"] = no_digest
    if unbound:
        reasons.append("ITEM_ID_NOT_BOUND_TO_IMAGE")
        details["itemsNotBoundToImage"] = unbound
    if sorted(digests) != sorted(session.get("imageSha256s") or []):
        reasons.append("SESSION_DIGEST_LIST_MISMATCH")
    if current_image_sha256 is None:
        reasons.append("IMAGE_DIGESTS_NOT_VERIFIED")
    else:
        changed = []
        for item in answer_key["items"]:
            if current_image_sha256.get(item["itemId"]) != item.get("pairImageSha256"):
                changed.append(item["itemId"])
                bad.add(item["itemId"])
        if changed:
            reasons.append("IMAGE_CHANGED_SINCE_BUILD")
            details["itemsWithChangedImage"] = changed
    return reasons, details, bad


def _check_capture(answer_key, session, config_sha256):
    """How the judged frames were prepared (config digest, letterbox policy, per-frame crops) must be on record in
    BOTH the session and the key, identical, and the config used now must be the one the session was built with."""
    reasons = []
    capture = session.get("capture")
    if not isinstance(capture, dict) or any(k not in capture for k in look_pairs.CAPTURE_REQUIRED_KEYS):
        reasons.append("CAPTURE_NOT_RECORDED")
    elif answer_key.get("capture") != capture:
        reasons.append("CAPTURE_SESSION_KEY_MISMATCH")
    if config_sha256 is None:
        reasons.append("CONFIG_SHA_NOT_VERIFIED")
    elif isinstance(capture, dict) and capture.get("configSha256") is not None \
            and capture["configSha256"] != config_sha256:
        reasons.append("CONFIG_DIFFERS_FROM_SESSION")
    return reasons


def _check_drops(answer_key, session):
    """Frames that were not judged: the session's recorded policy must match what the key says was dropped, and every
    LOST frame (not a chosen exclusion) needs a recorded allowance with a reason that covers it.
    Returns (reasons, allowance-or-None)."""
    policy = session.get("droppedFramePolicy")
    if not isinstance(policy, dict):
        return ["DROPPED_FRAME_POLICY_NOT_RECORDED"], None
    dropped = answer_key.get("droppedFrames") or []
    derived = look_pairs.drop_policy(dropped)
    reasons = []
    if (policy.get("lossFrameIds") != derived["lossFrameIds"]
            or policy.get("selectionFrameIds") != derived["selectionFrameIds"]
            or session.get("droppedFrames") != dropped):
        reasons.append("DROPPED_FRAME_POLICY_MISMATCH")
    allowance = policy.get("allowance")
    acknowledged = (isinstance(allowance, dict) and isinstance(allowance.get("reason"), str)
                    and bool(allowance["reason"].strip())
                    and set(allowance.get("frameIds") or []) >= set(derived["lossFrameIds"]))
    if derived["lossFrameIds"] and not acknowledged:
        reasons.append("UNACKNOWLEDGED_DROPPED_FRAMES")
    return reasons, (allowance if acknowledged and derived["lossFrameIds"] else None)


def tally(answer_key, item_verdicts, judge, session, cfg, current_image_sha256=None, forbidden_models=None,
          cross_family_status=None, config_sha256=None):
    """Build one model_verdicts[] entry.

    answer_key           the answer_key.json dict
    item_verdicts        {itemId: {"left":..,"right":..,"preference":..,"rubricSha256":..,"imageSha256":..}}
    judge                {"judgeId","model","family"}
    session              the session.json dict (carries the frozen rubricSha256, the seed and the image digests)
    cfg                  the loaded floor config (look_config.load_config()[0]); every threshold is read from it
    current_image_sha256 {itemId: sha256 of the pair image as it is on disk NOW}; None leaves the entry unusable
    forbidden_models     the producer/hub model ids; None leaves the entry unusable (the guard was not applied)
    config_sha256        sha256 of the config the caller is tallying under; it must equal the one the session was
                         built with, and None leaves the entry unusable (the config was not verified)
    """
    slot_cfg = cfg["slot_bias"]
    min_choices = int(slot_cfg["min_choices"])
    alpha = float(slot_cfg["alpha"])
    min_consistent = int(cfg["judge_validity"]["min_consistent_units"])
    max_discarded = float(cfg["judge_validity"]["max_discarded_unit_fraction"])
    frozen = session["rubricSha256"]
    unusable, details = [], {}

    key_reasons, key_details, bad_images = _check_key(answer_key, session, current_image_sha256)
    unusable.extend(key_reasons)
    details.update(key_details)
    unusable.extend(_check_capture(answer_key, session, config_sha256))
    drop_reasons, drop_allowance = _check_drops(answer_key, session)
    unusable.extend(drop_reasons)

    by_unit = {}
    for item in answer_key["items"]:
        by_unit.setdefault(item["unitId"], {})[item["order"]] = item
    if any(sorted(orders) != [1, 2] for orders in by_unit.values()):
        unusable.append("MALFORMED_KEY_UNIT_NOT_TWO_ORDERINGS")

    key_ids = {item["itemId"] for item in answer_key["items"]}
    unknown = sorted(set(item_verdicts) - key_ids)
    if unknown:
        unusable.append("RESULTS_CONTAIN_UNKNOWN_ITEMS")
        details["unknownItemIds"] = unknown

    invalid = []
    valid = {}  # itemId -> verdict
    missing = []
    for item in answer_key["items"]:
        raw = item_verdicts.get(item["itemId"])
        if raw is None:
            missing.append(item["itemId"])
            continue
        problems = validate_verdict(raw)
        if raw.get("rubricSha256") != frozen:
            problems.append("rubricSha256 does not match the session's frozen digest")
        if item["itemId"] in bad_images:
            problems.append("the item's image digest cannot be trusted (changed since build, unbound or unrecorded)")
        elif raw.get("imageSha256") != item.get("pairImageSha256"):
            problems.append("imageSha256 does not match the pair image the answer key recorded for this item")
        if problems:
            invalid.append({"itemId": item["itemId"], "problems": problems})
        else:
            valid[item["itemId"]] = raw
    if missing or invalid:
        unusable.append("INCOMPLETE_ITEMS")
        details["itemsWithoutValidVerdict"] = sorted(set(missing) | {r["itemId"] for r in invalid})

    real_slots, control_slots, positive_slots = _empty_slots(), _empty_slots(), _empty_slots()
    slots_by_kind = {"real": real_slots, "control": control_slots, "positive_control": positive_slots}
    votes = {}
    flips = tie_splits = unjudged = 0
    consistent_units = 0
    control_expected = control_answered = control_tie = 0
    positive_expected = positive_answered = positive_for_original = 0
    score_values = {}

    for unit_id, orders in by_unit.items():
        kind = next(iter(orders.values()))["kind"]
        got = {o: (it, valid.get(it["itemId"])) for o, it in orders.items()}
        for _, (item, verdict) in got.items():
            if verdict is not None and kind in slots_by_kind:
                slots_by_kind[kind][verdict["preference"]] += 1
        if kind == "control":
            for _, (item, verdict) in got.items():
                control_expected += 1
                if verdict is not None:
                    control_answered += 1
                    control_tie += verdict["preference"] == "tie"
            continue
        if kind == "positive_control":
            for _, (item, verdict) in got.items():
                positive_expected += 1
                if verdict is not None:
                    positive_answered += 1
                    positive_for_original += (
                        _subject_of(item, verdict["preference"]) == look_pairs.POSITIVE_ORIGINAL)
            continue
        if kind != "real":
            unusable.append("UNKNOWN_ITEM_KIND")
            continue
        if any(v is None for _, v in got.values()) or len(got) != 2:
            unjudged += 1
            continue
        subjects = {o: _subject_of(item, verdict["preference"]) for o, (item, verdict) in got.items()}
        first, second = subjects[1], subjects[2]
        for _, (item, verdict) in got.items():  # scores are per picture, so every valid real item counts
            for side in ("left", "right"):
                subject = item[side]["subject"]
                for criterion, value in verdict[side].items():
                    if value is not None:
                        score_values.setdefault(subject, {}).setdefault(criterion, []).append(value)
        if first == second:
            consistent_units += 1
            votes[first] = votes.get(first, 0) + 1
        elif "tie" in (first, second):
            tie_splits += 1
        else:
            flips += 1

    non_tie_left = real_slots["left"] + control_slots["left"] + positive_slots["left"]
    non_tie_right = real_slots["right"] + control_slots["right"] + positive_slots["right"]
    n_choices = non_tie_left + non_tie_right
    p_value = binomial_two_sided_p(non_tie_left, n_choices)
    if n_choices < min_choices:
        bias = "INSUFFICIENT_N"
    elif p_value < alpha:
        bias = "FLAGGED_LEFT" if non_tie_left > non_tie_right else "FLAGGED_RIGHT"
    else:
        bias = "NOT_FLAGGED"

    if control_expected == 0:
        control_outcome = "NOT_RUN"
    elif control_tie != control_answered:
        control_outcome = "FAILED_PREFERENCE_ON_IDENTICAL_IMAGES"
    elif control_answered < control_expected:
        control_outcome = "INCOMPLETE"
    else:
        control_outcome = "PASSED"

    if positive_expected == 0:
        positive_outcome = "NOT_RUN"
    elif positive_for_original != positive_answered:
        positive_outcome = "FAILED_DID_NOT_PREFER_THE_ORIGINAL"
    elif positive_answered < positive_expected:
        positive_outcome = "INCOMPLETE"
    else:
        positive_outcome = "PASSED"

    subjects_seen = answer_key["subjects"]
    scores = {
        s: {c: (round(sum(v) / len(v), 4) if v else None)
            for c, v in ((c, score_values.get(s, {}).get(c, [])) for c in look_config.CRITERIA)}
        for s in subjects_seen
    }
    ranked = sorted(((votes.get(s, 0), s) for s in subjects_seen), reverse=True)
    if consistent_units == 0:
        winner = "NO_CONSISTENT_VOTE"
    elif votes.get("tie", 0) >= ranked[0][0]:
        winner = "tie"
    elif len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
        winner = "tie"
    else:
        winner = ranked[0][1]
    subject_votes = [votes.get(s, 0) for s in subjects_seen]
    vote_p = binomial_two_sided_p(subject_votes[0], sum(subject_votes)) if len(subject_votes) == 2 else None

    if control_outcome != "PASSED":
        unusable.append({"NOT_RUN": "CONTROL_NOT_RUN", "INCOMPLETE": "CONTROL_INCOMPLETE"}.get(
            control_outcome, "CONTROL_FAILED"))
    if positive_outcome != "PASSED":
        unusable.append({"NOT_RUN": "POSITIVE_CONTROL_NOT_RUN", "INCOMPLETE": "POSITIVE_CONTROL_INCOMPLETE"}.get(
            positive_outcome, "POSITIVE_CONTROL_FAILED"))
    if bias.startswith("FLAGGED"):
        unusable.append("SLOT_BIAS")
    if consistent_units < min_consistent:
        unusable.append("TOO_FEW_CONSISTENT_UNITS")
    judged_units = consistent_units + flips + tie_splits
    discarded_fraction = (flips + tie_splits) / float(judged_units) if judged_units else 0.0
    if discarded_fraction > max_discarded:
        unusable.append("TOO_MANY_DISCARDED_UNITS")
    if forbidden_models is None:
        unusable.append("PRODUCER_GUARD_NOT_APPLIED")
    else:
        try:
            look_judges.assert_not_producer(judge["model"], forbidden_models)
        except look_judges.ProducerJudgeError as exc:
            unusable.append("JUDGE_IS_PRODUCER_OR_UNVERIFIABLE")
            details["producerGuard"] = str(exc)
    unusable = list(dict.fromkeys(unusable))
    usable = not unusable

    return {
        "schema": SCHEMA_VERDICT,
        "judgeId": judge["judgeId"], "model": judge["model"], "family": judge["family"],
        "crossFamilyStatus": cross_family_status,
        "rubricSha256": frozen,
        "imageSha256s": list(session["imageSha256s"]),
        "orderSeed": session["orderSeed"],
        "capture": session.get("capture"),
        "configSha256": config_sha256,
        "sessionConfigSha256": (session.get("capture") or {}).get("configSha256"),
        "droppedFramePolicy": session.get("droppedFramePolicy"),
        "droppedFrameAllowance": drop_allowance,
        "slotTally": {
            "real": real_slots, "control": control_slots, "positiveControl": positive_slots,
            "nonTieChoices": n_choices, "leftOfNonTie": non_tie_left, "pValue": round(p_value, 6),
            "slotBias": bias, "minChoicesForTest": min_choices,
        },
        "controlResult": {"items": control_expected, "answered": control_answered, "tieAnswers": control_tie,
                          "outcome": control_outcome},
        "positiveControlResult": {"items": positive_expected, "answered": positive_answered,
                                  "preferredOriginal": positive_for_original, "outcome": positive_outcome},
        "scores": scores,
        "preference": {
            "winner": winner if usable else UNUSABLE_WINNER, "withheldWinner": None if usable else winner,
            "votes": votes, "votePValue": None if vote_p is None else round(vote_p, 6),
            "consistentUnits": consistent_units, "minConsistentUnits": min_consistent,
            "discardedFlips": flips, "discardedTieSplits": tie_splits, "unjudgedUnits": unjudged,
            "discardedUnitFraction": round(discarded_fraction, 6), "maxDiscardedUnitFraction": max_discarded,
        },
        "invalidVerdicts": invalid,
        "integrity": details,
        "droppedFrames": list(answer_key.get("droppedFrames") or []),
        "usable": usable, "unusableReasons": unusable,
        "note": "Metrics gate; this entry refines only (bus TRAPS s8: blinded agent image judgments can be near chance). "
                "A winner is shown only when usable.",
    }


def judge_disagreement(entries, points):
    """Two judges more than `points` apart on any subject/criterion need a third judge (B4). `points` comes from
    the config (judge_disagreement.third_judge_points); there is no default. The result says whether every entry
    was usable: a comparison that includes an unusable judge is reported but is not `comparable`."""
    details = []
    for i in range(len(entries)):
        for j in range(i + 1, len(entries)):
            a, b = entries[i], entries[j]
            for subject, criteria in a["scores"].items():
                for criterion, a_value in criteria.items():
                    b_value = b["scores"].get(subject, {}).get(criterion)
                    if a_value is None or b_value is None:
                        continue
                    if abs(a_value - b_value) > points:
                        details.append({"judges": [a["judgeId"], b["judgeId"]], "subject": subject,
                                        "criterion": criterion, "values": [a_value, b_value]})
    unusable = [e["judgeId"] for e in entries if not e.get("usable")]
    mismatches = session_mismatches(entries)
    return {"thirdJudgeNeeded": bool(details), "thresholdPoints": points, "details": details,
            "unusableJudges": unusable, "sessionMismatches": mismatches,
            "comparable": len(entries) >= 2 and not unusable and not mismatches}


SESSION_IDENTITY_FIELDS = ("rubricSha256", "orderSeed", "imageSha256s")


def session_mismatches(entries):
    """The identity fields on which the entries do NOT agree (or that an entry lacks). Two judges' scores are only
    comparable when they judged the same session under the same frozen rubric; imageSha256s compare as sets."""
    def ident(entry, field):
        value = entry.get(field)
        return sorted(value) if field == "imageSha256s" and isinstance(value, list) else value

    bad = []
    for field in SESSION_IDENTITY_FIELDS:
        values = [ident(e, field) for e in entries]
        if any(v is None for v in values) or any(v != values[0] for v in values[1:]):
            bad.append(field)
    return bad
