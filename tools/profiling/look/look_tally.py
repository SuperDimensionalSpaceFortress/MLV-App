#!/usr/bin/env python3
"""Tally for blind model judging: turns raw per-item judge verdicts into one `model_verdicts[]` entry.

Pure standard library (no numpy/Pillow), so the repo-hygiene CI proves every rule here.

THE RULES (DESIGN.md AMENDMENT 2 B4)
    * Each real unit was judged TWICE with left/right swapped. Map each answer back to a SUBJECT. If the two
      orderings name different subjects the judge followed the SLOT, not the picture: the vote is a FLIP and
      is DISCARDED. A tie in one ordering and a pick in the other is a tie-split and is also discarded.
    * The zero-effect CONTROL (same image both sides) must be answered `tie`. Any side preference on identical
      images is a failed control; the entry says so and is not `usable`.
    * SLOT BIAS: among non-tie answers the left/right split is tested (exact two-sided binomial). Because every
      real unit appears in both orders, a judge that follows the PICTURE splits exactly 50/50, so a lopsided
      split is slot preference. Below slot_bias.min_choices the verdict is INSUFFICIENT_N, never "no bias".
    * A verdict quoting a different rubric digest than the session's frozen one is rejected, not scored.
    * Metrics gate; this entry only refines (bus TRAPS s8). `usable` says whether the judge's preference may be
      read at all.

OUTPUT FIELD NAMES follow the dual-venue receipt's `model_verdicts[]` element: judgeId, model, family,
rubricSha256, imageSha256s[], orderSeed, slotTally, scores, preference.
"""
import math

import look_config

SCHEMA_VERDICT = "mlv-app/model-verdict/v1"
SCHEMA_ITEM_VERDICT = "mlv-app/look-judge-item-verdict/v1"

PREFERENCES = ("left", "right", "tie")
DEFAULT_MIN_CHOICES = 6
DEFAULT_ALPHA = 0.05
DEFAULT_THIRD_JUDGE_POINTS = 1.0


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


def tally(answer_key, item_verdicts, judge, session, slot_bias_cfg=None, cross_family_status=None):
    """Build one model_verdicts[] entry.

    answer_key     the answer_key.json dict
    item_verdicts  {itemId: {"left":..,"right":..,"preference":..,"rubricSha256":..}}  (raw judge output)
    judge          {"judgeId","model","family"}
    session        the session.json dict (carries the frozen rubricSha256, the seed and the image digests)
    """
    slot_cfg = slot_bias_cfg or {}
    min_choices = int(slot_cfg.get("min_choices", DEFAULT_MIN_CHOICES))
    alpha = float(slot_cfg.get("alpha", DEFAULT_ALPHA))
    frozen = session["rubricSha256"]

    by_unit = {}
    for item in answer_key["items"]:
        by_unit.setdefault(item["unitId"], {})[item["order"]] = item

    invalid = []
    valid = {}  # itemId -> verdict
    for item in answer_key["items"]:
        raw = item_verdicts.get(item["itemId"])
        if raw is None:
            continue
        problems = validate_verdict(raw)
        if raw.get("rubricSha256") != frozen:
            problems.append("rubricSha256 does not match the session's frozen digest")
        if problems:
            invalid.append({"itemId": item["itemId"], "problems": problems})
        else:
            valid[item["itemId"]] = raw

    real_slots, control_slots = _empty_slots(), _empty_slots()
    votes = {}
    flips = tie_splits = unjudged = 0
    consistent_units = 0
    control_items = control_tie = 0
    score_values = {}

    for unit_id, orders in by_unit.items():
        kind = next(iter(orders.values()))["kind"]
        got = {o: (it, valid.get(it["itemId"])) for o, it in orders.items()}
        for _, (item, verdict) in got.items():
            if verdict is None:
                continue
            (control_slots if kind == "control" else real_slots)[verdict["preference"]] += 1
        if kind == "control":
            for _, (item, verdict) in got.items():
                if verdict is not None:
                    control_items += 1
                    control_tie += verdict["preference"] == "tie"
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

    non_tie_left = real_slots["left"] + control_slots["left"]
    non_tie_right = real_slots["right"] + control_slots["right"]
    n_choices = non_tie_left + non_tie_right
    p_value = binomial_two_sided_p(non_tie_left, n_choices)
    if n_choices < min_choices:
        bias = "INSUFFICIENT_N"
    elif p_value < alpha:
        bias = "FLAGGED_LEFT" if non_tie_left > non_tie_right else "FLAGGED_RIGHT"
    else:
        bias = "NOT_FLAGGED"

    if control_items == 0:
        control_outcome = "NOT_RUN"
    else:
        control_outcome = "PASSED" if control_tie == control_items else "FAILED_PREFERENCE_ON_IDENTICAL_IMAGES"

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

    unusable = []
    if control_outcome.startswith("FAILED"):
        unusable.append("CONTROL_FAILED")
    if bias.startswith("FLAGGED"):
        unusable.append("SLOT_BIAS")
    if consistent_units == 0:
        unusable.append("NO_CONSISTENT_UNITS")
    if control_outcome == "NOT_RUN":
        unusable.append("CONTROL_NOT_RUN")

    return {
        "schema": SCHEMA_VERDICT,
        "judgeId": judge["judgeId"], "model": judge["model"], "family": judge["family"],
        "crossFamilyStatus": cross_family_status,
        "rubricSha256": frozen,
        "imageSha256s": list(session["imageSha256s"]),
        "orderSeed": session["orderSeed"],
        "slotTally": {
            "real": real_slots, "control": control_slots,
            "nonTieChoices": n_choices, "leftOfNonTie": non_tie_left, "pValue": round(p_value, 6),
            "slotBias": bias, "minChoicesForTest": min_choices,
        },
        "controlResult": {"items": control_items, "tieAnswers": control_tie, "outcome": control_outcome},
        "scores": scores,
        "preference": {
            "winner": winner, "votes": votes, "consistentUnits": consistent_units,
            "discardedFlips": flips, "discardedTieSplits": tie_splits, "unjudgedUnits": unjudged,
        },
        "invalidVerdicts": invalid,
        "usable": not unusable, "unusableReasons": unusable,
        "note": "Metrics gate; this entry refines only (bus TRAPS s8: blinded agent image judgments can be near chance).",
    }


def judge_disagreement(entries, points=DEFAULT_THIRD_JUDGE_POINTS):
    """Two judges more than `points` apart on any subject/criterion need a third judge (B4)."""
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
    return {"thirdJudgeNeeded": bool(details), "thresholdPoints": points, "details": details}
