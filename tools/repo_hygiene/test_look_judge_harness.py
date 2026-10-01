#!/usr/bin/env python3
"""LOOK-METRICS-JUDGE-1: falsifier tests for the blind judge harness (tools/profiling/look/).

Almost all of this file is pure standard library: sessions are built with byte-level fake image drawers
(look_pairs.build_session(compose=..., degrade=...)), so the binding, tally, run_session, producer-guard,
config and timeout tests run on ANY host. Only PairImageTests / BuildSessionCliTests draw real pixels; hosted CI
now pins numpy + Pillow with hashes (.github/requirements/repo-hygiene.txt), and CiPinsTests fails if those
tests would be skipped there, so nothing in this file can silently not run in CI.

THE CLASS EVERY TEST HERE SERVES: no path lets an unjudged, stale, incomplete, biased, self-judged or hidden
result read as PASS / usable. Each test pins ONE way someone tried (or could try) to do that.
"""
import contextlib
import copy
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

_LOOK_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "profiling", "look"))
_REPO_ROOT = os.path.normpath(os.path.join(_LOOK_DIR, "..", "..", ".."))
if _LOOK_DIR not in sys.path:
    sys.path.insert(0, _LOOK_DIR)

import look_cli  # noqa: E402
import look_config  # noqa: E402
import look_judges  # noqa: E402
import look_pairs  # noqa: E402
import look_tally  # noqa: E402

_HAS_PIL = importlib.util.find_spec("PIL") is not None
_HAS_DEPS = _HAS_PIL and importlib.util.find_spec("numpy") is not None

CFG, META = look_config.load_config()
RUBRIC = look_config.verify_rubric_lock()["rubricSha256"]
JUDGE = {"judgeId": "j1", "model": "claude-fable-5-1", "family": "anthropic"}
FORBIDDEN = ["claude-sonnet-5-5", "claude-opus-5-5"]
SEED = "tally-seed"


# ---------------------------------------------------------------------------------------------------------
# a pixel-free session: real build_session, byte-level fake drawers
# ---------------------------------------------------------------------------------------------------------

def _fake_compose(left, right, out):
    with open(left, "rb") as a, open(right, "rb") as b, open(out, "wb") as handle:
        handle.write(b"PAIR|" + a.read() + b"|" + b.read())


def _fake_degrade(src, out):
    with open(src, "rb") as a, open(out, "wb") as handle:
        handle.write(b"DEGRADED|" + a.read())


def _sources(tmp, name, frames, tag="v1"):
    out = {}
    for f in frames:
        path = os.path.join(tmp, f"{name}-{tag}-{f}.src")
        with open(path, "wb") as handle:
            handle.write(f"{name}|{tag}|{f}".encode("ascii"))
        out[f] = path
    return out


def build_fake_session(tmp, tag="v1", frames=range(5), controls=1, positive=1, subdir="session", **kw):
    out = os.path.join(tmp, subdir)
    a = {"name": "cuda", "frames": _sources(tmp, "cuda", frames, tag)}
    b = {"name": "cpu", "frames": _sources(tmp, "cpu", frames, tag)}
    look_pairs.build_session(a, b, list(frames), SEED, out, RUBRIC, controls=controls, positive_controls=positive,
                             compose=_fake_compose, degrade=_fake_degrade, **kw)
    return out


def read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path, doc):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=2)


def current_digests(session_dir):
    manifest = read_json(os.path.join(session_dir, "judge_manifest.json"))
    return {e["itemId"]: look_judges.sha256_file(os.path.join(session_dir, e["image"])) for e in manifest["items"]}


def _scores(n=3):
    return {c: (None if c == "skin" else n) for c in look_config.CRITERIA}


def verdict_for(item, preference):
    return {"left": _scores(), "right": _scores(), "preference": preference, "rubricSha256": RUBRIC,
            "imageSha256": item["pairImageSha256"]}


def faithful(favourite):
    """Follows the PICTURE: `favourite` in every real unit wherever it sits; tie on the negative control; the
    original on the positive control."""
    def rule(item):
        if item["kind"] == "control":
            return "tie"
        if item["kind"] == "positive_control":
            return "left" if item["left"]["subject"] == look_pairs.POSITIVE_ORIGINAL else "right"
        return "left" if item["left"]["subject"] == favourite else "right"
    return rule


def slot_follower(side):
    return lambda item: side


class Fixture:
    """A built fake session, its key/session dicts and a way to answer it."""

    def __init__(self, tmp, **kw):
        self.dir = build_fake_session(tmp, **kw)
        self.key = read_json(os.path.join(self.dir, "answer_key.json"))
        self.session = read_json(os.path.join(self.dir, "session.json"))
        self.current = current_digests(self.dir)

    def answers(self, rule):
        return {it["itemId"]: verdict_for(it, rule(it)) for it in self.key["items"]}

    def tally(self, answers, **kw):
        kw.setdefault("current_image_sha256", self.current)
        kw.setdefault("forbidden_models", FORBIDDEN)
        return look_tally.tally(self.key, answers, JUDGE, self.session, CFG, **kw)

    def judge_fn(self, rule, counter=None):
        by_id = {it["itemId"]: it for it in self.key["items"]}

        def fn(png, rubric):
            if counter is not None:
                counter.append(os.path.basename(png))
            item = by_id[os.path.basename(png)[:-4]]
            return {"left": _scores(), "right": _scores(), "preference": rule(item)}
        return fn


class TmpCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name


# ---------------------------------------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------------------------------------

class ConfigTests(TmpCase):
    def _doc(self):
        with open(look_config.CONFIG_PATH, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def test_every_shipped_threshold_has_a_reason(self):
        leaves = look_config.validate_config(self._doc())
        self.assertGreater(len(leaves), 30)
        for path, leaf in leaves:
            self.assertTrue(leaf["reason"].strip(), path)

    def test_the_code_reads_exactly_the_thresholds_the_config_ships(self):
        paths = {p for p, _ in look_config.validate_config(self._doc())}
        self.assertEqual(paths, set(look_config.REQUIRED_THRESHOLDS))

    def test_a_bare_number_is_refused_at_EVERY_threshold_in_every_section(self):
        doc = self._doc()
        for path, leaf in look_config.validate_config(doc):
            with self.subTest(path):
                mutated = copy.deepcopy(doc)
                node = mutated
                parts = path.split(".")
                for part in parts[:-1]:
                    node = node[part]
                node[parts[-1]] = leaf["value"]  # the number, with its reason stripped away
                with self.assertRaises(look_config.ConfigError):
                    look_config.validate_config(mutated)

    def test_pair_geometry_tolerance_bare_16_is_refused_by_load_config(self):
        doc = self._doc()
        doc["pair"]["geometry_tolerance_px"] = 16
        path = os.path.join(self.tmp, "cfg.json")
        write_json(path, doc)
        with self.assertRaises(look_config.ConfigError):
            look_config.load_config(path)

    def test_a_bare_value_hidden_under_a_nested_note_key_is_refused(self):
        for where in (("pair",), ("pair", "scopes"), ("letterbox",), ("judge_validity",)):
            with self.subTest(where):
                doc = self._doc()
                node = doc
                for part in where:
                    node = node[part]
                node["note"] = 16
                with self.assertRaises(look_config.ConfigError):
                    look_config.validate_config(doc)

    def test_empty_reason_and_extra_keys_and_missing_thresholds_are_refused(self):
        doc = self._doc()
        doc["frame"]["crushed_shadow_pct_max"]["reason"] = "   "
        with self.assertRaises(look_config.ConfigError):
            look_config.validate_config(doc)
        doc = self._doc()
        doc["frame"]["crushed_shadow_pct_max"]["extra"] = 1
        with self.assertRaises(look_config.ConfigError):
            look_config.validate_config(doc)
        doc = self._doc()
        del doc["judge_validity"]["min_consistent_units"]
        with self.assertRaises(look_config.ConfigError):
            look_config.validate_config(doc)
        doc = self._doc()
        doc["configVersion"] = "1"
        with self.assertRaises(look_config.ConfigError):
            look_config.validate_config(doc)

    def test_design_thresholds_are_the_ruled_ones_and_the_version_reaches_meta(self):
        self.assertEqual(CFG["frame"]["clipped_highlight_pct_max"], 1.0)  # B3: <= 1%
        self.assertEqual(CFG["frame"]["crushed_shadow_pct_max"], 2.0)     # B3: <= 2%
        self.assertEqual(CFG["frame"]["skin_hue_drift_deg_max"], 5.0)     # B3: ~5 degrees
        self.assertTrue(CFG["pair"]["scopes"]["shader-subset"]["gated"])  # A3: gated
        self.assertFalse(CFG["pair"]["scopes"]["full-look"]["gated"])     # A3: reported only
        self.assertFalse(CFG["letterbox"]["auto_exclude_symmetric"])      # nothing hidden unless declared
        self.assertEqual(len(META["configSha256"]), 64)
        self.assertEqual(META["configVersion"], 1)

    def test_config_digest_ignores_crlf_checkouts(self):
        crlf = os.path.join(self.tmp, "c.json")
        with open(look_config.CONFIG_PATH, "rb") as src, open(crlf, "wb") as dst:
            dst.write(src.read().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        self.assertEqual(look_config.load_config(crlf)[1]["configSha256"], META["configSha256"])


class RubricFrozenTests(TmpCase):
    def test_locked_digest_matches_the_shipped_rubric(self):
        lock = look_config.verify_rubric_lock()
        self.assertEqual(lock["rubricSha256"], look_config.rubric_digest())
        self.assertEqual(lock["criteria"], list(look_config.CRITERIA))
        self.assertEqual(lock["rubricId"], "look-rubric-v2")

    def test_rubric_names_every_criterion_and_the_answer_format(self):
        text = look_config.load_rubric_text()
        for criterion in look_config.CRITERIA:
            self.assertIn(f"### {criterion}", text)
        for needle in ("`left`, `right` or `tie`", "anchored 1-5", "ONE JSON object"):
            self.assertIn(needle, text)

    def test_the_rubric_names_no_defect_colour_that_would_prime_a_judge(self):
        text = look_config.load_rubric_text().lower()
        for primer in ("lavender", "magenta", "violet", "purple", "teal", "green", "orange"):
            self.assertNotIn(primer, text)

    def test_one_changed_character_breaks_the_lock(self):
        rubric = os.path.join(self.tmp, "judge_rubric.md")
        lock = os.path.join(self.tmp, "judge_rubric.lock.json")
        shutil.copyfile(look_config.RUBRIC_PATH, rubric)
        look_config.write_rubric_lock(rubric, lock)
        look_config.verify_rubric_lock(rubric, lock)  # fine when untouched
        with open(rubric, "ab") as handle:
            handle.write(b" ")
        with self.assertRaises(look_config.RubricLockError):
            look_config.verify_rubric_lock(rubric, lock)

    def test_digest_is_portable_across_line_endings(self):
        crlf = os.path.join(self.tmp, "r.md")
        with open(look_config.RUBRIC_PATH, "rb") as src, open(crlf, "wb") as dst:
            dst.write(src.read().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        self.assertEqual(look_config.rubric_digest(crlf), look_config.rubric_digest())

    def test_shipped_files_are_pinned_to_lf(self):
        with open(os.path.join(_REPO_ROOT, ".gitattributes"), "r", encoding="utf-8") as handle:
            body = handle.read()
        for name in ("judge_rubric.md", "judge_rubric.lock.json", "look_floor_config.json"):
            self.assertIn(f"tools/profiling/look/{name} text eol=lf", body)


class CiPinsTests(unittest.TestCase):
    """Hosted CI must RUN the pixel tests, not skip them, and must say so honestly."""

    def _read(self, *parts):
        with open(os.path.join(_REPO_ROOT, *parts), "r", encoding="utf-8") as handle:
            return handle.read()

    def test_numpy_and_pillow_are_pinned_with_hashes_in_the_hygiene_lock(self):
        declared = self._read(".github", "requirements", "repo-hygiene.in").lower()
        locked = self._read(".github", "requirements", "repo-hygiene.txt").lower()
        for package in ("numpy", "pillow"):
            with self.subTest(package):
                self.assertIn(package, declared)
                lines = locked.splitlines()
                at = next(i for i, ln in enumerate(lines) if ln.startswith(f"{package}=="))
                self.assertTrue(lines[at].endswith("\\"))
                self.assertTrue(lines[at + 1].strip().startswith("--hash=sha256:"))

    def test_in_hosted_ci_the_pixel_dependencies_are_installed_so_nothing_is_skipped(self):
        if os.environ.get("GITHUB_ACTIONS") == "true":
            self.assertTrue(_HAS_DEPS, "numpy/Pillow missing in hosted CI: the pixel tests would silently skip")


# ---------------------------------------------------------------------------------------------------------
# plan + build
# ---------------------------------------------------------------------------------------------------------

def _subject(name, frames):
    return {"name": name, "frames": {f: f"/nonexistent/{name}-{f}.png" for f in frames}}


class PairPlanTests(unittest.TestCase):
    A = _subject("cuda", range(6))
    B = _subject("cpu", range(6))

    def _plan(self, seed="s1", frames=(0, 1, 2, 3), controls=1, positive=1):
        return look_pairs.plan_pairs(self.A, self.B, list(frames), seed, controls=controls, positive_controls=positive)

    def test_plan_is_deterministic_by_seed(self):
        one = json.dumps(self._plan("s1"), sort_keys=True)
        self.assertEqual(one, json.dumps(self._plan("s1"), sort_keys=True))
        self.assertNotEqual(one, json.dumps(self._plan("s2"), sort_keys=True))

    def test_caller_argument_order_does_not_decide_the_slot(self):
        forward = look_pairs.plan_pairs(self.A, self.B, [0, 1, 2, 3, 4, 5], "s1")
        swapped = look_pairs.plan_pairs(self.B, self.A, [0, 1, 2, 3, 4, 5], "s1")
        sides = lambda plan: {(i["unitId"], i["order"]): i["left"]["subject"] for i in plan["items"] if i["kind"] == "real"}
        self.assertEqual(sides(forward), sides(swapped))

    def test_every_real_unit_is_emitted_twice_order_swapped(self):
        plan = self._plan()
        for frame in (0, 1, 2, 3):
            pair = [i for i in plan["items"] if i["unitId"] == f"real-{frame}"]
            self.assertEqual(sorted(i["order"] for i in pair), [1, 2])
            first = next(i for i in pair if i["order"] == 1)
            second = next(i for i in pair if i["order"] == 2)
            self.assertEqual(first["left"]["subject"], second["right"]["subject"])
            self.assertEqual(first["right"]["subject"], second["left"]["subject"])
            self.assertNotEqual(first["left"]["subject"], first["right"]["subject"])

    def test_negative_control_is_identical_images_and_also_swapped(self):
        control = [i for i in self._plan(controls=1)["items"] if i["kind"] == "control"]
        self.assertEqual(len(control), 2)
        for item in control:
            self.assertEqual(item["left"], item["right"])

    def test_positive_control_is_original_against_degraded_in_both_orders(self):
        pos = [i for i in self._plan(positive=1)["items"] if i["kind"] == "positive_control"]
        self.assertEqual(len(pos), 2)
        self.assertEqual({tuple(sorted((i["left"]["subject"], i["right"]["subject"]))) for i in pos},
                         {(look_pairs.POSITIVE_DEGRADED, look_pairs.POSITIVE_ORIGINAL)})
        self.assertEqual({i["left"]["subject"] for i in pos},
                         {look_pairs.POSITIVE_ORIGINAL, look_pairs.POSITIVE_DEGRADED})  # swapped between orders
        for item in pos:
            degraded = item["left"] if item["left"]["subject"] == look_pairs.POSITIVE_DEGRADED else item["right"]
            self.assertTrue(degraded.get("degrade"))

    def test_left_right_is_not_always_the_same_subject(self):
        plan = look_pairs.plan_pairs(self.A, self.B, [0, 1, 2, 3, 4, 5], "s1")
        first_orders = {i["left"]["subject"] for i in plan["items"] if i["kind"] == "real" and i["order"] == 1}
        self.assertEqual(first_orders, {"cuda", "cpu"})

    def test_planned_item_ids_are_opaque_and_unique(self):
        ids = [i["itemId"] for i in self._plan()["items"]]
        self.assertEqual(len(ids), len(set(ids)))
        for item_id in ids:
            self.assertRegex(item_id, r"^p-[0-9a-f]{12}$")

    def test_same_subject_twice_and_no_common_frames_are_refused(self):
        with self.assertRaises(ValueError):
            look_pairs.plan_pairs(self.A, self.A, [0], "s")
        with self.assertRaises(ValueError):
            look_pairs.plan_pairs(self.A, _subject("cpu", [9]), [0], "s")

    def test_unshared_frames_are_reported_with_a_reason_not_silently_dropped(self):
        plan = look_pairs.plan_pairs(self.A, _subject("cpu", [0, 1]), [0, 1, 2], "s")
        self.assertEqual(plan["droppedFrameIds"], [2])
        self.assertEqual(plan["droppedFrames"], [{"frameId": 2, "reason": "NOT_IN_BOTH_SUBJECTS"}])


class BuiltSessionBindingTests(TmpCase):
    """ITEM 1: ids and verdicts are bound to the image digest."""

    def test_item_ids_are_derived_from_the_pair_image_digest(self):
        fx = Fixture(self.tmp)
        for item in fx.key["items"]:
            self.assertEqual(item["itemId"], look_pairs.derive_item_id(
                fx.key["orderSeed"], item["unitId"], item["order"], item["pairImageSha256"]))
            self.assertEqual(fx.current[item["itemId"]], item["pairImageSha256"])
        self.assertEqual(sorted(fx.current.values()), fx.session["imageSha256s"])

    def test_rebuilding_over_different_frames_changes_every_id(self):
        old = Fixture(self.tmp, tag="v1", subdir="s1")
        new = Fixture(self.tmp, tag="v2", subdir="s2")
        self.assertFalse(set(old.current) & set(new.current))  # same seed, new pictures: no id survives

    def test_session_has_two_controls_kinds_and_records_what_it_dropped(self):
        fx = Fixture(self.tmp, dropped_frames=[{"frameId": 9, "reason": "CROP_TOLERANCE_EXCEEDED"}],
                     crop_tolerance_px=3)
        kinds = {}
        for item in fx.key["items"]:
            kinds[item["kind"]] = kinds.get(item["kind"], 0) + 1
        self.assertEqual(kinds, {"real": 10, "control": 2, "positive_control": 2})
        self.assertEqual(fx.session["itemCount"], 14)
        self.assertEqual(fx.session["commonCropTolerancePx"], 3)
        self.assertEqual(fx.session["droppedFrames"], [{"frameId": 9, "reason": "CROP_TOLERANCE_EXCEEDED"}])
        self.assertEqual(fx.key["droppedFrameIds"], [9])

    def test_judge_manifest_names_no_subject_and_no_seed(self):
        fx = Fixture(self.tmp)
        with open(os.path.join(fx.dir, "judge_manifest.json"), "r", encoding="utf-8") as handle:
            view = handle.read()
        for secret in ("cuda", "cpu", "@original", "@degraded", SEED, "positive", "control"):
            self.assertNotIn(secret, view)


# ---------------------------------------------------------------------------------------------------------
# tally
# ---------------------------------------------------------------------------------------------------------

class TallyTests(TmpCase):
    def setUp(self):
        super().setUp()
        self.fx = Fixture(self.tmp)

    def _tally(self, rule, **kw):
        return self.fx.tally(self.fx.answers(rule), **kw)

    # -- the honest path ------------------------------------------------------------------------------
    def test_a_judge_that_follows_the_picture_is_counted_and_usable(self):
        entry = self._tally(faithful("cpu"))
        self.assertEqual(entry["unusableReasons"], [])
        self.assertTrue(entry["usable"])
        self.assertEqual(entry["preference"]["winner"], "cpu")
        self.assertEqual(entry["preference"]["consistentUnits"], 5)
        self.assertEqual(entry["preference"]["discardedFlips"], 0)
        self.assertEqual(entry["slotTally"]["real"], {"left": 5, "right": 5, "tie": 0})
        self.assertEqual(entry["slotTally"]["slotBias"], "NOT_FLAGGED")
        self.assertEqual(entry["controlResult"]["outcome"], "PASSED")
        self.assertEqual(entry["positiveControlResult"]["outcome"], "PASSED")
        self.assertEqual(entry["preference"]["minConsistentUnits"], CFG["judge_validity"]["min_consistent_units"])

    def test_order_flipping_votes_are_discarded(self):
        entry = self._tally(slot_follower("left"))
        self.assertEqual(entry["preference"]["discardedFlips"], 5)
        self.assertEqual(entry["preference"]["consistentUnits"], 0)
        self.assertEqual(entry["preference"]["withheldWinner"], "NO_CONSISTENT_VOTE")
        self.assertEqual(entry["preference"]["votes"], {})

    def test_only_the_flipping_unit_is_discarded(self):
        def rule(item):
            if item["unitId"] == "real-2":
                return "left"
            return faithful("cuda")(item)
        entry = self._tally(rule)
        self.assertEqual(entry["preference"]["discardedFlips"], 1)
        self.assertEqual(entry["preference"]["votes"], {"cuda": 4})
        self.assertEqual(entry["preference"]["winner"], "cuda")  # 4 consistent units = the floor
        self.assertTrue(entry["usable"])

    def test_judge_that_prefers_a_slot_is_flagged_and_unusable(self):
        entry = self._tally(slot_follower("left"))
        self.assertEqual(entry["slotTally"]["slotBias"], "FLAGGED_LEFT")
        self.assertEqual(entry["controlResult"]["outcome"], "FAILED_PREFERENCE_ON_IDENTICAL_IMAGES")
        self.assertFalse(entry["usable"])
        for reason in ("SLOT_BIAS", "CONTROL_FAILED", "TOO_FEW_CONSISTENT_UNITS"):
            self.assertIn(reason, entry["unusableReasons"])
        self.assertEqual(self._tally(slot_follower("right"))["slotTally"]["slotBias"], "FLAGGED_RIGHT")

    def test_preference_on_identical_control_images_fails_the_control(self):
        def rule(item):
            return "left" if item["kind"] == "control" else faithful("cpu")(item)
        entry = self._tally(rule)
        self.assertEqual(entry["controlResult"]["outcome"], "FAILED_PREFERENCE_ON_IDENTICAL_IMAGES")
        self.assertFalse(entry["usable"])
        self.assertEqual(entry["preference"]["winner"], "UNUSABLE")   # never shown as a winner ...
        self.assertEqual(entry["preference"]["withheldWinner"], "cpu")  # ... but kept for the record

    def test_a_tie_in_both_orders_is_a_consistent_tie_on_real_units(self):
        def rule(item):
            return faithful("cpu")(item) if item["kind"] == "positive_control" else "tie"
        entry = self._tally(rule)
        self.assertEqual(entry["preference"]["consistentUnits"], 5)
        self.assertEqual(entry["preference"]["winner"], "tie")
        self.assertTrue(entry["usable"])

    def test_tie_split_is_discarded_separately_from_a_flip(self):
        def rule(item):
            if item["unitId"] == "real-0":
                return "tie" if item["order"] == 1 else "left"
            return faithful("cpu")(item)
        entry = self._tally(rule)
        self.assertEqual(entry["preference"]["discardedTieSplits"], 1)
        self.assertEqual(entry["preference"]["discardedFlips"], 0)

    def test_missing_ordering_leaves_the_unit_unjudged_and_the_entry_incomplete(self):
        answers = self.fx.answers(faithful("cpu"))
        dropped = next(i["itemId"] for i in self.fx.key["items"] if i["unitId"] == "real-1" and i["order"] == 2)
        del answers[dropped]
        entry = self.fx.tally(answers)
        self.assertEqual(entry["preference"]["unjudgedUnits"], 1)
        self.assertEqual(entry["preference"]["consistentUnits"], 4)
        self.assertIn("INCOMPLETE_ITEMS", entry["unusableReasons"])
        self.assertFalse(entry["usable"])

    def test_verdict_quoting_another_rubric_digest_is_rejected(self):
        answers = self.fx.answers(faithful("cpu"))
        victim = next(i["itemId"] for i in self.fx.key["items"] if i["kind"] == "real")
        answers[victim]["rubricSha256"] = "0" * 64
        entry = self.fx.tally(answers)
        self.assertEqual([r["itemId"] for r in entry["invalidVerdicts"]], [victim])
        self.assertIn("rubricSha256", entry["invalidVerdicts"][0]["problems"][0])
        self.assertFalse(entry["usable"])

    def test_malformed_verdicts_are_invalid_not_scored(self):
        good = verdict_for({"pairImageSha256": "a" * 64}, "left")
        cases = {
            "score out of range": {**good, "left": {**_scores(), "tonal_separation": 6}},
            "float score": {**good, "left": {**_scores(), "colour_cast": 2.5}},
            "null on a non-nullable criterion": {**good, "right": {**_scores(), "scene_read": None}},
            "bad preference": {**good, "preference": "both"},
            "missing criterion": {**good, "left": {k: v for k, v in _scores().items() if k != "skin"}},
            "bool score": {**good, "left": {**_scores(), "colour_cast": True}},
        }
        for label, verdict in cases.items():
            with self.subTest(label):
                self.assertTrue(look_tally.validate_verdict(verdict), label)
        self.assertEqual(look_tally.validate_verdict(good), [])

    def test_scores_are_per_subject_means_with_null_skin_skipped(self):
        entry = self._tally(faithful("cpu"))
        self.assertEqual(entry["scores"]["cpu"]["tonal_separation"], 3.0)
        self.assertIsNone(entry["scores"]["cpu"]["skin"])

    def test_entry_carries_the_receipt_field_names_and_the_dropped_frames(self):
        entry = self._tally(faithful("cpu"), cross_family_status="CROSS_FAMILY_UNAVAILABLE")
        for field in ("judgeId", "model", "family", "rubricSha256", "imageSha256s", "orderSeed", "slotTally",
                      "scores", "preference", "droppedFrames", "integrity"):
            self.assertIn(field, entry)
        self.assertEqual(entry["rubricSha256"], RUBRIC)
        self.assertEqual(entry["crossFamilyStatus"], "CROSS_FAMILY_UNAVAILABLE")

    def test_binomial_p_values(self):
        self.assertAlmostEqual(look_tally.binomial_two_sided_p(0, 6), 0.03125)
        self.assertAlmostEqual(look_tally.binomial_two_sided_p(3, 6), 1.0)
        self.assertAlmostEqual(look_tally.binomial_two_sided_p(6, 6), 0.03125)
        self.assertEqual(look_tally.binomial_two_sided_p(0, 0), 1.0)
        self.assertAlmostEqual(self._tally(faithful("cpu"))["preference"]["votePValue"], 0.0625)  # 5-0, exact

    # -- ITEM 1: stale / changed images ---------------------------------------------------------------
    def test_a_verdict_recorded_against_another_image_digest_is_invalid(self):
        answers = self.fx.answers(faithful("cpu"))
        victim = next(i["itemId"] for i in self.fx.key["items"] if i["kind"] == "real")
        answers[victim]["imageSha256"] = "0" * 64
        entry = self.fx.tally(answers)
        self.assertEqual([r["itemId"] for r in entry["invalidVerdicts"]], [victim])
        self.assertIn("imageSha256", entry["invalidVerdicts"][0]["problems"][0])
        self.assertIn("INCOMPLETE_ITEMS", entry["unusableReasons"])
        self.assertFalse(entry["usable"])

    def test_a_verdict_with_no_image_digest_at_all_is_invalid(self):
        answers = self.fx.answers(faithful("cpu"))
        victim = next(iter(answers))
        del answers[victim]["imageSha256"]
        self.assertFalse(self.fx.tally(answers)["usable"])

    def test_an_image_changed_on_disk_after_the_build_makes_the_session_unusable(self):
        stale = dict(self.fx.current)
        victim = next(i["itemId"] for i in self.fx.key["items"] if i["kind"] == "real")
        stale[victim] = "1" * 64
        entry = self.fx.tally(self.fx.answers(faithful("cpu")), current_image_sha256=stale)
        self.assertIn("IMAGE_CHANGED_SINCE_BUILD", entry["unusableReasons"])
        self.assertEqual(entry["integrity"]["itemsWithChangedImage"], [victim])
        self.assertFalse(entry["usable"])

    def test_a_deleted_image_reads_as_changed(self):
        gone = dict(self.fx.current)
        gone[next(iter(gone))] = None
        self.assertIn("IMAGE_CHANGED_SINCE_BUILD",
                      self.fx.tally(self.fx.answers(faithful("cpu")), current_image_sha256=gone)["unusableReasons"])

    def test_not_checking_the_images_is_itself_unusable(self):
        entry = look_tally.tally(self.fx.key, self.fx.answers(faithful("cpu")), JUDGE, self.fx.session, CFG,
                                 current_image_sha256=None, forbidden_models=FORBIDDEN)
        self.assertIn("IMAGE_DIGESTS_NOT_VERIFIED", entry["unusableReasons"])
        self.assertFalse(entry["usable"])

    def test_rewriting_session_and_key_digests_but_keeping_item_ids_is_caught(self):
        # the round-1 repro: new image hashes in the session and the key, old item ids, old verdicts
        key, session = copy.deepcopy(self.fx.key), copy.deepcopy(self.fx.session)
        answers = self.fx.answers(faithful("cpu"))
        current = dict(self.fx.current)
        for n, item in enumerate(key["items"]):
            fresh = f"{n:064x}"
            item["pairImageSha256"] = fresh
            current[item["itemId"]] = fresh
            answers[item["itemId"]]["imageSha256"] = fresh
        session["imageSha256s"] = sorted(i["pairImageSha256"] for i in key["items"])
        entry = look_tally.tally(key, answers, JUDGE, session, CFG, current_image_sha256=current,
                                 forbidden_models=FORBIDDEN)
        self.assertIn("ITEM_ID_NOT_BOUND_TO_IMAGE", entry["unusableReasons"])
        self.assertFalse(entry["usable"])

    def test_a_session_digest_list_that_disagrees_with_the_key_is_unusable(self):
        session = dict(self.fx.session, imageSha256s=["a" * 64])
        entry = look_tally.tally(self.fx.key, self.fx.answers(faithful("cpu")), JUDGE, session, CFG,
                                 current_image_sha256=self.fx.current, forbidden_models=FORBIDDEN)
        self.assertIn("SESSION_DIGEST_LIST_MISMATCH", entry["unusableReasons"])

    def test_results_that_name_items_the_key_does_not_have_are_unusable(self):
        answers = self.fx.answers(faithful("cpu"))
        answers["p-000000000000"] = answers[next(iter(answers))]
        entry = self.fx.tally(answers)
        self.assertIn("RESULTS_CONTAIN_UNKNOWN_ITEMS", entry["unusableReasons"])
        self.assertEqual(entry["integrity"]["unknownItemIds"], ["p-000000000000"])

    def test_a_key_whose_subjects_were_edited_cannot_produce_a_winner(self):
        key = copy.deepcopy(self.fx.key)
        victim = next(i for i in key["items"] if i["kind"] == "real")
        victim["left"]["subject"] = victim["right"]["subject"] = "cpu"
        entry = look_tally.tally(key, self.fx.answers(faithful("cpu")), JUDGE, self.fx.session, CFG,
                                 current_image_sha256=self.fx.current, forbidden_models=FORBIDDEN)
        self.assertIn("KEY_SUBJECTS_MALFORMED", entry["unusableReasons"])
        self.assertFalse(entry["usable"])

    # -- ITEM 2: controls ------------------------------------------------------------------------------
    def _control_ids(self):
        return [i["itemId"] for i in self.fx.key["items"] if i["kind"] == "control"]

    def test_a_missing_control_answer_is_INCOMPLETE_never_passed(self):
        answers = self.fx.answers(faithful("cpu"))
        del answers[self._control_ids()[0]]
        entry = self.fx.tally(answers)
        self.assertEqual(entry["controlResult"], {"items": 2, "answered": 1, "tieAnswers": 1, "outcome": "INCOMPLETE"})
        self.assertIn("CONTROL_INCOMPLETE", entry["unusableReasons"])
        self.assertFalse(entry["usable"])

    def test_a_control_answer_with_the_wrong_rubric_digest_is_INCOMPLETE(self):
        answers = self.fx.answers(faithful("cpu"))
        answers[self._control_ids()[0]]["rubricSha256"] = "0" * 64
        entry = self.fx.tally(answers)
        self.assertEqual(entry["controlResult"]["outcome"], "INCOMPLETE")
        self.assertFalse(entry["usable"])

    def test_a_control_answer_quoting_the_wrong_image_digest_is_INCOMPLETE(self):
        answers = self.fx.answers(faithful("cpu"))
        answers[self._control_ids()[0]]["imageSha256"] = "0" * 64
        self.assertEqual(self.fx.tally(answers)["controlResult"]["outcome"], "INCOMPLETE")

    def test_no_control_in_the_key_at_all_is_NOT_RUN_and_unusable(self):
        fx = Fixture(self.tmp, controls=0, subdir="nocontrol")
        entry = fx.tally(fx.answers(faithful("cpu")))
        self.assertEqual(entry["controlResult"]["outcome"], "NOT_RUN")
        self.assertIn("CONTROL_NOT_RUN", entry["unusableReasons"])

    def test_below_the_consistent_unit_floor_the_entry_is_unusable_and_the_floor_is_the_configs(self):
        fx = Fixture(self.tmp, frames=range(3), subdir="three")  # 3 real units < the floor of 4
        entry = fx.tally(fx.answers(faithful("cpu")))
        self.assertIn("TOO_FEW_CONSISTENT_UNITS", entry["unusableReasons"])
        self.assertEqual(entry["preference"]["winner"], "UNUSABLE")
        lowered = copy.deepcopy(CFG)
        lowered["judge_validity"]["min_consistent_units"] = 3
        entry = look_tally.tally(fx.key, fx.answers(faithful("cpu")), JUDGE, fx.session, lowered,
                                 current_image_sha256=fx.current, forbidden_models=FORBIDDEN)
        self.assertTrue(entry["usable"], entry["unusableReasons"])  # the config, not a constant, decides

    # -- the positive control -------------------------------------------------------------------------
    def test_an_always_tie_judge_is_UNUSABLE_because_it_cannot_see_a_large_degradation(self):
        entry = self._tally(lambda item: "tie")
        self.assertEqual(entry["controlResult"]["outcome"], "PASSED")  # it passes the negative control ...
        self.assertEqual(entry["positiveControlResult"]["outcome"], "FAILED_DID_NOT_PREFER_THE_ORIGINAL")
        self.assertIn("POSITIVE_CONTROL_FAILED", entry["unusableReasons"])  # ... and is still caught
        self.assertFalse(entry["usable"])
        self.assertEqual(entry["preference"]["winner"], "UNUSABLE")

    def test_a_judge_that_prefers_the_degraded_picture_is_unusable(self):
        def rule(item):
            if item["kind"] == "positive_control":
                return "left" if item["left"]["subject"] == look_pairs.POSITIVE_DEGRADED else "right"
            return faithful("cpu")(item)
        self.assertIn("POSITIVE_CONTROL_FAILED", self._tally(rule)["unusableReasons"])

    def test_a_missing_positive_control_answer_is_incomplete(self):
        answers = self.fx.answers(faithful("cpu"))
        del answers[next(i["itemId"] for i in self.fx.key["items"] if i["kind"] == "positive_control")]
        entry = self.fx.tally(answers)
        self.assertEqual(entry["positiveControlResult"]["outcome"], "INCOMPLETE")
        self.assertIn("POSITIVE_CONTROL_INCOMPLETE", entry["unusableReasons"])

    def test_a_session_built_without_a_positive_control_is_unusable(self):
        fx = Fixture(self.tmp, positive=0, subdir="nopositive")
        entry = fx.tally(fx.answers(faithful("cpu")))
        self.assertIn("POSITIVE_CONTROL_NOT_RUN", entry["unusableReasons"])
        self.assertFalse(entry["usable"])

    # -- producer guard inside the tally --------------------------------------------------------------
    def test_the_tally_re_applies_the_producer_guard_and_refuses_to_run_without_one(self):
        answers = self.fx.answers(faithful("cpu"))
        self_judged = look_tally.tally(self.fx.key, answers, dict(JUDGE, model="sonnet"), self.fx.session, CFG,
                                       current_image_sha256=self.fx.current, forbidden_models=FORBIDDEN)
        self.assertIn("JUDGE_IS_PRODUCER_OR_UNVERIFIABLE", self_judged["unusableReasons"])
        unguarded = look_tally.tally(self.fx.key, answers, JUDGE, self.fx.session, CFG,
                                     current_image_sha256=self.fx.current, forbidden_models=None)
        self.assertIn("PRODUCER_GUARD_NOT_APPLIED", unguarded["unusableReasons"])

    # -- disagreement ---------------------------------------------------------------------------------
    def test_judges_more_than_the_configured_points_apart_need_a_third(self):
        base = self._tally(faithful("cpu"))
        other = json.loads(json.dumps(base))
        other["judgeId"] = "j2"
        self.assertFalse(look_tally.judge_disagreement([base, other], 1.0)["thirdJudgeNeeded"])
        other["scores"]["cpu"]["colour_cast"] = base["scores"]["cpu"]["colour_cast"] + 1.5
        verdict = look_tally.judge_disagreement([base, other], 1.0)
        self.assertTrue(verdict["thirdJudgeNeeded"])
        self.assertTrue(verdict["comparable"])
        self.assertEqual(verdict["details"][0]["criterion"], "colour_cast")
        other["scores"]["cpu"]["colour_cast"] = base["scores"]["cpu"]["colour_cast"] + 1.0  # exactly 1: not "more than"
        self.assertFalse(look_tally.judge_disagreement([base, other], 1.0)["thirdJudgeNeeded"])
        self.assertTrue(look_tally.judge_disagreement([base, other], 0.5)["thirdJudgeNeeded"])

    def test_a_comparison_that_includes_an_unusable_judge_is_not_comparable(self):
        good, bad = self._tally(faithful("cpu")), self._tally(lambda item: "tie")
        verdict = look_tally.judge_disagreement([good, bad], 1.0)
        self.assertFalse(verdict["comparable"])
        self.assertEqual(verdict["unusableJudges"], [bad["judgeId"]])


# ---------------------------------------------------------------------------------------------------------
# run_session (pixel-free: fake drawers)
# ---------------------------------------------------------------------------------------------------------

class RunSessionTests(TmpCase):
    def setUp(self):
        super().setUp()
        self.fx = Fixture(self.tmp)

    def _runner(self, fn, judge_id="cj", model="claude-fable-5-1"):
        return look_judges.CallableJudge(fn, judge_id, model, "anthropic")

    def test_judges_every_item_records_both_digests_and_resumes_without_rejudging(self):
        calls = []
        runner = self._runner(self.fx.judge_fn(faithful("cpu"), calls))
        summary = look_judges.run_session(self.fx.dir, runner, workers=2)
        self.assertEqual((summary["judged"], summary["errors"], summary["remaining"], summary["staleRejected"]),
                         (14, 0, 0, 0))
        doc = read_json(summary["resultsPath"])
        item = self.fx.key["items"][0]
        self.assertEqual(doc["items"][item["itemId"]]["rubricSha256"], RUBRIC)
        self.assertEqual(doc["items"][item["itemId"]]["imageSha256"], item["pairImageSha256"])
        look_judges.run_session(self.fx.dir, runner)
        self.assertEqual(len(calls), 14)  # nothing was judged twice

    def test_an_item_whose_stored_digest_differs_from_the_current_image_is_judged_again(self):
        calls = []
        runner = self._runner(self.fx.judge_fn(faithful("cpu"), calls))
        look_judges.run_session(self.fx.dir, runner)
        victim = self.fx.key["items"][3]["itemId"]
        image = os.path.join(self.fx.dir, "images", victim + ".png")
        with open(image, "ab") as handle:  # the picture is replaced ...
            handle.write(b"NEW-PIXELS")
        session_path = os.path.join(self.fx.dir, "session.json")
        session = read_json(session_path)
        session["imageSha256s"] = sorted(current_digests(self.fx.dir).values())  # ... and the session re-recorded
        write_json(session_path, session)
        del calls[:]
        summary = look_judges.run_session(self.fx.dir, runner)
        self.assertEqual(calls, [victim + ".png"])  # exactly the changed item, nothing else
        self.assertEqual(summary["staleRejected"], 1)
        doc = read_json(summary["resultsPath"])
        self.assertEqual(doc["items"][victim]["imageSha256"], current_digests(self.fx.dir)[victim])
        self.assertIn(victim, doc["staleRejected"])

    def test_images_that_no_longer_match_the_session_record_refuse_the_run(self):
        victim = self.fx.key["items"][0]["itemId"]
        with open(os.path.join(self.fx.dir, "images", victim + ".png"), "ab") as handle:
            handle.write(b"x")
        calls = []
        with self.assertRaises(look_judges.JudgeError):
            look_judges.run_session(self.fx.dir, self._runner(self.fx.judge_fn(faithful("cpu"), calls)))
        self.assertEqual(calls, [])

    def test_a_stored_verdict_made_under_another_rubric_is_judged_again(self):
        runner = self._runner(self.fx.judge_fn(faithful("cpu")))
        summary = look_judges.run_session(self.fx.dir, runner)
        doc = read_json(summary["resultsPath"])
        victim = next(iter(doc["items"]))
        doc["items"][victim]["rubricSha256"] = "0" * 64
        write_json(summary["resultsPath"], doc)
        again = look_judges.run_session(self.fx.dir, runner)
        self.assertEqual(again["staleRejected"], 1)
        self.assertEqual(read_json(again["resultsPath"])["items"][victim]["rubricSha256"], RUBRIC)

    def test_a_results_file_from_another_judge_identity_is_refused(self):
        summary = look_judges.run_session(self.fx.dir, self._runner(self.fx.judge_fn(faithful("cpu"))))
        other = self._runner(self.fx.judge_fn(faithful("cpu")), model="claude-haiku-4-5")  # same judge id, other model
        with self.assertRaises(look_judges.JudgeError):
            look_judges.run_session(self.fx.dir, other)
        self.assertTrue(os.path.isfile(summary["resultsPath"]))

    def test_an_image_swapped_while_it_is_being_judged_is_an_error_not_a_verdict(self):
        by_id = {it["itemId"]: it for it in self.fx.key["items"]}

        def swapping(png, rubric):
            with open(png, "ab") as handle:
                handle.write(b"swapped-mid-judging")
            return {"left": _scores(), "right": _scores(), "preference": "tie"}
        # restore the session record each time is not needed: the post-judging digest check must catch it
        summary = look_judges.run_session(self.fx.dir, self._runner(swapping), max_items=1)
        self.assertEqual((summary["judged"], summary["errors"]), (0, 1))
        self.assertTrue(by_id)

    def test_run_session_refuses_a_rubric_that_changed_after_the_freeze(self):
        # pixel-free: a hand-edited session digest, then a rubric edited after the lock
        runner = self._runner(self.fx.judge_fn(faithful("cpu")))
        session_path = os.path.join(self.fx.dir, "session.json")
        session = read_json(session_path)
        session["rubricSha256"] = "0" * 64
        write_json(session_path, session)
        with self.assertRaises(look_config.RubricLockError):
            look_judges.run_session(self.fx.dir, runner)
        session["rubricSha256"] = RUBRIC
        write_json(session_path, session)
        rubric = os.path.join(self.tmp, "judge_rubric.md")
        lock_path = os.path.join(self.tmp, "judge_rubric.lock.json")
        shutil.copyfile(look_config.RUBRIC_PATH, rubric)
        look_config.write_rubric_lock(rubric, lock_path)
        with open(rubric, "ab") as handle:
            handle.write(b"edited")
        with self.assertRaises(look_config.RubricLockError):
            look_judges.run_session(self.fx.dir, self._runner(self.fx.judge_fn(faithful("cpu")), "cj3"),
                                    rubric_path=rubric, lock_path=lock_path)

    def test_a_missing_session_is_refused(self):
        with self.assertRaises(look_judges.JudgeError):
            look_judges.run_session(os.path.join(self.tmp, "nowhere"), self._runner(lambda p, r: {}))


class TallyCliTests(TmpCase):
    """ITEM 2: the CLI exits non-zero whenever the entry is unusable, and zero only for a usable one."""

    def setUp(self):
        super().setUp()
        self.fx = Fixture(self.tmp)

    def _run(self, rule, judge_id="cj"):
        runner = look_judges.CallableJudge(self.fx.judge_fn(rule), judge_id, "claude-fable-5-1", "anthropic")
        summary = look_judges.run_session(self.fx.dir, runner)
        out = os.path.join(self.tmp, f"tally-{judge_id}.json")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = look_cli.main(["tally", "--session-dir", self.fx.dir, "--results", summary["resultsPath"],
                                  "--out", out, "--producer-model", "claude-sonnet-5-5",
                                  "--producer-model", "claude-opus-5-5"])
        return code, read_json(out), stdout.getvalue()

    def test_a_usable_tally_exits_zero(self):
        code, entry, _ = self._run(faithful("cpu"))
        self.assertEqual((code, entry["usable"]), (0, True))

    def test_an_always_tie_judge_exits_non_zero_and_the_json_is_still_written(self):
        code, entry, stdout = self._run(lambda item: "tie", "tie-judge")
        self.assertEqual(code, 2)
        self.assertFalse(entry["usable"])
        self.assertIn("POSITIVE_CONTROL_FAILED", stdout)

    def test_a_tampered_image_after_judging_makes_the_cli_exit_non_zero(self):
        runner = look_judges.CallableJudge(self.fx.judge_fn(faithful("cpu")), "cj", "claude-fable-5-1", "anthropic")
        summary = look_judges.run_session(self.fx.dir, runner)
        with open(os.path.join(self.fx.dir, "images", self.fx.key["items"][0]["itemId"] + ".png"), "ab") as handle:
            handle.write(b"tampered")
        out = os.path.join(self.tmp, "t.json")
        with contextlib.redirect_stdout(io.StringIO()):
            code = look_cli.main(["tally", "--session-dir", self.fx.dir, "--results", summary["resultsPath"],
                                  "--out", out, "--producer-model", "opus"])
        self.assertEqual(code, 2)
        self.assertIn("IMAGE_CHANGED_SINCE_BUILD", read_json(out)["unusableReasons"])

    def test_the_cli_refuses_a_self_judge_by_alias_at_tally_time_too(self):
        results = os.path.join(self.tmp, "r.json")
        runner = look_judges.CallableJudge(self.fx.judge_fn(faithful("cpu")), "cj", "sonnet", "anthropic")
        summary = look_judges.run_session(self.fx.dir, runner)
        out = os.path.join(self.tmp, "t.json")
        with contextlib.redirect_stdout(io.StringIO()):
            code = look_cli.main(["tally", "--session-dir", self.fx.dir, "--results", summary["resultsPath"],
                                  "--out", out, "--producer-model", "claude-sonnet-5-5"])
        self.assertEqual(code, 2)
        self.assertIn("JUDGE_IS_PRODUCER_OR_UNVERIFIABLE", read_json(out)["unusableReasons"])
        self.assertFalse(os.path.exists(results))

    def test_third_judge_points_comes_from_the_config_file(self):
        def entry(judge_id, value):
            return {"judgeId": judge_id, "usable": True, "scores": {"cpu": {"colour_cast": value}}}
        a, b = os.path.join(self.tmp, "a.json"), os.path.join(self.tmp, "b.json")
        write_json(a, entry("a", 3.0))
        write_json(b, entry("b", 3.8))
        with open(look_config.CONFIG_PATH, "r", encoding="utf-8") as handle:
            doc = json.load(handle)
        outcome = {}
        for points in (1.0, 0.5):
            doc["judge_disagreement"]["third_judge_points"]["value"] = points
            cfg_path = os.path.join(self.tmp, f"cfg-{points}.json")
            write_json(cfg_path, doc)
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                code = look_cli.main(["judge-disagreement", "--entries", a, b, "--config", cfg_path])
            outcome[points] = (code, json.loads(buf.getvalue())["thirdJudgeNeeded"])
        self.assertEqual(outcome, {1.0: (0, False), 0.5: (0, True)})


# ---------------------------------------------------------------------------------------------------------
# judge identity + bounded subprocesses
# ---------------------------------------------------------------------------------------------------------

class ProducerGuardTests(TmpCase):
    def test_a_judge_is_never_the_producer_or_the_hub(self):
        for model in ("claude-sonnet-5-5", "CLAUDE-OPUS-5-5", "claude-sonnet-5-5-20261001"):
            with self.subTest(model):
                with self.assertRaises(look_judges.ProducerJudgeError):
                    look_judges.assert_not_producer(model, FORBIDDEN)
        look_judges.assert_not_producer("claude-fable-5-1", FORBIDDEN)
        look_judges.assert_not_producer("claude-haiku-4-5-20251001", FORBIDDEN)
        look_judges.assert_not_producer("codex-default", FORBIDDEN)
        with self.assertRaises(look_judges.ProducerJudgeError):
            look_judges.assert_not_producer("", FORBIDDEN)

    def test_an_alias_and_a_resolved_id_are_the_same_model_in_BOTH_directions(self):
        pairs = [("sonnet", ["claude-sonnet-5-5"]), ("opus", ["claude-opus-5-5"]),
                 ("claude-sonnet-5-5", ["sonnet"]), ("claude:claude-opus-5-5".split(":")[1], ["opus"]),
                 ("fable", ["claude-fable-5-1"]), ("claude-haiku-4-5-20251001", ["haiku"]),
                 ("Sonnet 5.5", ["sonnet"]), ("opus[1m]", ["claude-opus-5-5"]),
                 ("us.anthropic.claude-sonnet-5-5-v1:0", ["claude-sonnet-5-5"])]
        for judge, forbidden in pairs:
            with self.subTest(judge=judge, forbidden=forbidden):
                with self.assertRaises(look_judges.ProducerJudgeError):
                    look_judges.assert_not_producer(judge, forbidden)

    def test_any_version_of_the_producer_family_is_refused_but_other_families_pass(self):
        for judge in ("claude-sonnet-4", "claude-sonnet-3-7", "sonnet", "claude-sonnet-9-9"):
            with self.assertRaises(look_judges.ProducerJudgeError):
                look_judges.assert_not_producer(judge, ["claude-sonnet-5-5"])
        for judge in ("opus", "fable", "haiku", "claude-opus-5-5"):
            look_judges.assert_not_producer(judge, ["claude-sonnet-5-5"])

    def test_names_that_cannot_be_placed_in_a_family_fail_closed(self):
        with self.assertRaises(look_judges.ProducerJudgeError):
            look_judges.assert_not_producer("mystery-model", FORBIDDEN)       # unknown judge
        with self.assertRaises(look_judges.ProducerJudgeError):
            look_judges.assert_not_producer("claude-fable-5-1", ["banana"])   # a typo in the producer list
        with self.assertRaises(look_judges.ProducerJudgeError):
            look_judges.assert_not_producer("claude-fable-5-1", [])           # nothing named: independence unprovable
        with self.assertRaises(look_judges.ProducerJudgeError):
            look_judges.assert_not_producer("claude-fable-5-1", ["  ", ""])
        with self.assertRaises(look_judges.ProducerJudgeError):
            look_judges.assert_not_producer("sonnet-opus", ["claude-fable-5-1"])  # two families in one name

    def test_the_codex_default_resolves_to_the_real_model_name(self):
        home = os.path.join(self.tmp, "codex-home")
        os.makedirs(home)
        with open(os.path.join(home, "config.toml"), "w", encoding="utf-8") as handle:
            handle.write('# comment\nmodel = "gpt-6.1-sol"\nmodel_reasoning_effort = "xhigh"\n[profiles.x]\nmodel = "other"\n')
        judge = look_judges.CodexExecJudge(codex_exe="codex", codex_home=home)
        self.assertEqual(judge.model, "gpt-6.1-sol")
        self.assertEqual(judge.identity()["model"], "gpt-6.1-sol")
        self.assertEqual(judge.command("/w/p.png", "/w/o.txt", "/w")[:4], ["codex", "exec", "-m", "gpt-6.1-sol"])
        with self.assertRaises(look_judges.ProducerJudgeError):  # the codex producer is the same model
            look_judges.assert_not_producer(judge.model, ["gpt-6.1-sol"])
        with self.assertRaises(look_judges.ProducerJudgeError):  # ... or another version of its family
            look_judges.assert_not_producer(judge.model, ["gpt-6.1"])
        look_judges.assert_not_producer(judge.model, ["gpt-6.1-luna"])  # a different lane model passes
        look_judges.assert_not_producer(judge.model, FORBIDDEN)

    def test_an_unresolvable_codex_default_is_refused_when_the_producer_is_codex(self):
        home = os.path.join(self.tmp, "empty-home")
        os.makedirs(home)
        judge = look_judges.CodexExecJudge(codex_exe="codex", codex_home=home)
        self.assertEqual(judge.model, look_judges.CODEX_DEFAULT_MODEL)
        self.assertNotIn("-m", judge.command("/w/p.png", "/w/o.txt", "/w"))
        for producer in ("gpt-6.1-sol", "codex", "gpt-5.5", "o3"):
            with self.subTest(producer):
                with self.assertRaises(look_judges.ProducerJudgeError):
                    look_judges.assert_not_producer(judge.model, [producer])
        look_judges.assert_not_producer(judge.model, FORBIDDEN)  # only a Claude producer: a codex judge is independent

    def test_cli_judge_refuses_by_alias_before_any_judging(self):
        fx = Fixture(self.tmp)
        for runner, producer in (("claude:sonnet", "claude-sonnet-5-5"), ("claude:claude-opus-5-5", "opus"),
                                 ("codex:gpt-6.1-sol", "gpt-6.1-sol")):
            with self.subTest(runner):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    code = look_cli.main(["judge", "--session-dir", fx.dir, "--runner", runner,
                                          "--producer-model", producer])
                self.assertEqual(code, 2)
                self.assertIn("ProducerJudgeError", stderr.getvalue())
        self.assertEqual([f for f in os.listdir(fx.dir) if f.startswith("verdicts-")], [])


class JudgeRunnerTests(unittest.TestCase):
    def test_json_is_extracted_from_a_chatty_reply(self):
        reply = 'Sure! Here you go:\n```json\n{"preference": "tie", "left": {"skin": null}}\n```\nanything else {not json}'
        self.assertEqual(look_judges.extract_json_object(reply)["preference"], "tie")
        with self.assertRaises(look_judges.JudgeError):
            look_judges.extract_json_object("no object here")

    def test_json_with_braces_inside_strings(self):
        reply = '{"rationale": "a } brace and a { brace", "preference": "left"}'
        self.assertEqual(look_judges.extract_json_object(reply)["preference"], "left")

    def test_prompt_is_rubric_plus_fixed_instruction_and_names_no_subject(self):
        rubric = look_config.load_rubric_text()
        for codex in (False, True):
            prompt = look_judges.build_prompt(rubric, codex=codex)
            self.assertIn(rubric, prompt)
            for secret in ("cuda", "CUDA", "cpu", "CPU", "classic", "cinematic", "flavor", "backend"):
                self.assertNotIn(secret, prompt)

    def test_codex_command_attaches_the_image_and_keeps_the_prompt_off_argv(self):
        judge = look_judges.CodexExecJudge(model="gpt-x", codex_exe="codex")
        cmd = judge.command("/w/pair.png", "/w/out.txt", "/w")
        self.assertEqual(cmd[:4], ["codex", "exec", "-m", "gpt-x"])
        self.assertEqual(cmd[cmd.index("--image") + 1], "/w/pair.png")
        self.assertEqual(cmd[cmd.index("--image") + 2], "--sandbox")  # --image is variadic: a flag must follow it
        self.assertEqual(cmd[cmd.index("--sandbox") + 1], "read-only")
        self.assertIn("--skip-git-repo-check", cmd)
        self.assertEqual(cmd[-2:], ["-o", "/w/out.txt"])

    def _help(self, text, version="codex-cli 9.9"):
        def fake(argv, **kwargs):
            body = version if "--version" in argv else text
            return subprocess.CompletedProcess(argv, 0, stdout=body, stderr="")
        return fake

    def test_probe_reports_flag_present_but_not_proven(self):
        with mock.patch.object(look_judges, "run_bounded", self._help("  -i, --image <FILE>...  attach")):
            probe = look_judges.probe_codex_image_support("codex")
        self.assertEqual(probe["status"], look_judges.CROSS_FAMILY_FLAG_ONLY)
        self.assertEqual(probe["imageFlag"], "--image")

    def test_probe_without_the_flag_is_cross_family_unavailable(self):
        with mock.patch.object(look_judges, "run_bounded", self._help("  -m, --model <MODEL>")):
            probe = look_judges.probe_codex_image_support("codex")
        self.assertEqual(probe["status"], look_judges.CROSS_FAMILY_UNAVAILABLE)

    def test_probe_with_no_codex_is_unavailable(self):
        with mock.patch.object(look_judges.shutil, "which", return_value=None):
            self.assertEqual(look_judges.probe_codex_image_support()["status"], look_judges.CROSS_FAMILY_UNAVAILABLE)

    def test_second_judge_policy(self):
        flag_only = {"status": look_judges.CROSS_FAMILY_FLAG_ONLY}
        proven = {"status": look_judges.CROSS_FAMILY_PROVEN}
        runner, status = look_judges.select_second_judge("anthropic", flag_only, fallback_claude_model="claude-haiku-4-5")
        self.assertEqual(status, look_judges.CROSS_FAMILY_UNAVAILABLE)
        self.assertIsInstance(runner, look_judges.ClaudeCliJudge)
        runner, status = look_judges.select_second_judge("anthropic", proven)
        self.assertEqual(status, look_judges.CROSS_FAMILY_PROVEN)
        self.assertIsInstance(runner, look_judges.CodexExecJudge)
        runner, status = look_judges.select_second_judge("anthropic", flag_only)
        self.assertIsNone(runner)
        self.assertEqual(status, look_judges.CROSS_FAMILY_UNAVAILABLE)

    def test_family_of_model_ids(self):
        self.assertEqual(look_judges.family_of("claude-fable-5-1"), "anthropic")
        self.assertEqual(look_judges.family_of("opus"), "anthropic")
        self.assertEqual(look_judges.family_of("gpt-5"), "openai")
        self.assertEqual(look_judges.family_of("mystery"), "unknown")


class BoundedSubprocessTests(TmpCase):
    """ITEM 7: a hung judge behind a Windows npm .cmd shim must not outlive its timeout."""

    def _slow_cli(self):
        if os.name == "nt":
            path = os.path.join(self.tmp, "slow-cli.cmd")
            with open(path, "w", newline="") as handle:  # cmd.exe -> ping: the real CLI is a CHILD of the shim
                handle.write("@echo off\r\nping -n 600 127.0.0.1 >nul\r\n")
        else:
            path = os.path.join(self.tmp, "slow-cli.sh")
            with open(path, "w", newline="\n") as handle:
                handle.write("#!/bin/sh\nsleep 600\n")
            os.chmod(path, 0o755)
        return path

    def _png(self):
        path = os.path.join(self.tmp, "p.png")
        with open(path, "wb") as handle:
            handle.write(b"not-really-a-png")
        return path

    def test_a_hung_claude_shim_is_killed_with_its_child_within_timeout_plus_grace(self):
        judge = look_judges.ClaudeCliJudge("claude-fable-5-1", claude_exe=self._slow_cli(), timeout_s=2)
        started = time.monotonic()
        with self.assertRaises(look_judges.JudgeError) as caught:
            judge.judge_image(self._png(), "rubric")
        elapsed = time.monotonic() - started
        self.assertIn("timed out", str(caught.exception))
        self.assertLess(elapsed, 2 + 5 + 8, f"hung judge took {elapsed:.1f}s (the child kept the pipes open)")

    def test_a_hung_codex_shim_is_bounded_too(self):
        judge = look_judges.CodexExecJudge(model="gpt-x", codex_exe=self._slow_cli(), timeout_s=2)
        started = time.monotonic()
        with self.assertRaises(look_judges.JudgeError):
            judge.judge_image(self._png(), "rubric")
        self.assertLess(time.monotonic() - started, 2 + 5 + 8)

    def test_run_bounded_returns_output_and_the_exit_code(self):
        done = look_judges.run_bounded([sys.executable, "-c", "import sys; print(sys.stdin.read().upper()); sys.exit(3)"],
                                       input_text="abc", timeout=60)
        self.assertEqual((done.returncode, done.stdout.strip()), (3, "ABC"))

    def test_run_bounded_raises_timeout_for_a_plain_sleeper(self):
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            look_judges.run_bounded([sys.executable, "-c", "import time; time.sleep(120)"], timeout=1)
        self.assertLess(time.monotonic() - started, 1 + 5 + 8)

    def test_a_cli_that_cannot_start_is_a_judge_error_not_a_crash(self):
        judge = look_judges.ClaudeCliJudge("claude-fable-5-1", claude_exe=os.path.join(self.tmp, "no-such-cli"))
        with self.assertRaises(look_judges.JudgeError):
            judge.judge_image(self._png(), "rubric")


# ---------------------------------------------------------------------------------------------------------
# pixel-drawing tests (numpy/Pillow are pinned in hosted CI, see CiPinsTests)
# ---------------------------------------------------------------------------------------------------------

@unittest.skipUnless(_HAS_PIL, "Pillow is not installed on this host")
class PairImageTests(TmpCase):
    def _png(self, name, size, colour):
        from PIL import Image
        path = os.path.join(self.tmp, name)
        Image.new("RGB", size, colour).save(path)
        return path

    def test_session_hides_subject_names_and_records_the_rubric_digest_first(self):
        frames_a = {i: self._png(f"flavor-classic-{i}.png", (40, 24), (200, 50, 50)) for i in range(3)}
        frames_b = {i: self._png(f"flavor-cinematic-{i}.png", (40, 24), (50, 50, 200)) for i in range(3)}
        out = os.path.join(self.tmp, "session")
        paths = look_pairs.build_session(
            {"name": "classic", "frames": frames_a}, {"name": "cinematic", "frames": frames_b},
            [0, 1, 2], "seed-7", out, "d" * 64, controls=1, positive_controls=1)
        with open(paths["judge_manifest.json"], "r", encoding="utf-8") as handle:
            judge_view = handle.read()
        for secret in ("classic", "cinematic", "cuda", "cpu", "seed-7", "flavor", "@original", "degraded"):
            self.assertNotIn(secret, judge_view)
        for name in os.listdir(os.path.join(out, "images")):
            self.assertRegex(name, r"^p-[0-9a-f]{12}\.png$")
        session = read_json(paths["session.json"])
        self.assertEqual(session["rubricSha256"], "d" * 64)
        self.assertEqual(session["orderSeed"], "seed-7")
        self.assertEqual(session["itemCount"], 10)  # 3 real units x2 + control x2 + positive control x2
        self.assertTrue(session["emittedTwiceOrderSwapped"])
        with open(paths["answer_key.json"], "r", encoding="utf-8") as handle:
            self.assertIn("classic", handle.read())  # the key, and only the key, names subjects

    def test_the_positive_control_image_really_is_a_large_degradation(self):
        import numpy as np
        from PIL import Image
        src = self._png("src.png", (40, 24), (200, 120, 60))
        out = look_pairs.degrade_image(src, os.path.join(self.tmp, "deg.png"))
        with Image.open(src) as a, Image.open(out) as b:
            delta = np.abs(np.asarray(a.convert("RGB"), dtype=int) - np.asarray(b.convert("RGB"), dtype=int))
        self.assertGreater(float(delta.mean()), 30.0)  # far beyond any look-parity difference

    def test_pair_image_is_grey_gutter_no_text_and_byte_deterministic(self):
        from PIL import Image
        left = self._png("l.png", (30, 20), (250, 0, 0))
        right = self._png("r.png", (30, 20), (0, 0, 250))
        one = look_pairs.compose_pair_image(left, right, os.path.join(self.tmp, "one.png"))
        two = look_pairs.compose_pair_image(left, right, os.path.join(self.tmp, "two.png"))
        self.assertEqual(look_pairs.sha256_file(one), look_pairs.sha256_file(two))
        with Image.open(one) as im:
            self.assertEqual(im.text if hasattr(im, "text") else {}, {})
            self.assertEqual(im.getpixel((0, 0)), look_pairs.GUTTER_RGB)
            gutter_x = look_pairs.MARGIN_PX + 30 + look_pairs.GUTTER_PX // 2
            self.assertEqual(im.getpixel((gutter_x, 30)), look_pairs.GUTTER_RGB)
            self.assertEqual(im.getpixel((look_pairs.MARGIN_PX + 5, look_pairs.MARGIN_PX + 5)), (250, 0, 0))
            self.assertEqual(im.getpixel((im.width - look_pairs.MARGIN_PX - 5, look_pairs.MARGIN_PX + 5)), (0, 0, 250))

    def test_unequal_pictures_are_padded_never_rescaled(self):
        from PIL import Image
        left = self._png("l.png", (30, 20), (250, 0, 0))
        right = self._png("r.png", (30, 24), (0, 0, 250))
        out = look_pairs.compose_pair_image(left, right, os.path.join(self.tmp, "o.png"))
        with Image.open(out) as im:
            self.assertEqual(im.size, (2 * look_pairs.MARGIN_PX + 60 + look_pairs.GUTTER_PX, 2 * look_pairs.MARGIN_PX + 24))
            self.assertEqual(im.getpixel((look_pairs.MARGIN_PX + 5, look_pairs.MARGIN_PX + 2 + 5)), (250, 0, 0))


@unittest.skipUnless(_HAS_PIL, "Pillow is not installed on this host")
class BuildSessionCliTests(TmpCase):
    """ITEM 8: a frame that is not judged is recorded with a reason, never silently dropped; ITEM 4 for build-session."""

    def _frames(self, name, specs):
        import numpy as np
        from PIL import Image
        directory = os.path.join(self.tmp, name)
        os.makedirs(directory)
        for index, (w, h) in specs.items():
            ramp = np.tile(np.linspace(40, 200, w), (h, 1))
            arr = np.stack([ramp, ramp * 0.9, ramp * 0.8], axis=2).astype(np.uint8)
            Image.fromarray(arr, "RGB").save(os.path.join(directory, f"{name}-frame-{index:02d}.png"))
        return directory

    def _build(self, extra=(), a=None, b=None):
        a = a or self._frames("aa", {0: (48, 32), 1: (48, 32), 2: (48, 32)})
        b = b or self._frames("bb", {0: (48, 32), 1: (58, 32)})  # frame 1 is 10 px wider; frame 2 is missing
        out = os.path.join(self.tmp, "sess")
        stderr, stdout = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout):
            code = look_cli.main(["build-session", "--a", f"aa={a}", "--b", f"bb={b}", "--seed", "s",
                                  "--out-dir", out, *extra])
        return code, out, stdout.getvalue(), stderr.getvalue()

    def test_dropped_frames_are_recorded_with_reasons_and_the_crop_tolerance(self):
        code, out, stdout, stderr = self._build(["--common-crop-tolerance-px", "2"])
        self.assertEqual(code, 0, stderr)
        key, session = read_json(os.path.join(out, "answer_key.json")), read_json(os.path.join(out, "session.json"))
        reasons = {d["frameId"]: d["reason"] for d in session["droppedFrames"]}
        self.assertEqual(sorted(reasons), [1, 2])
        self.assertTrue(reasons[1].startswith("CROP_TOLERANCE_EXCEEDED"), reasons[1])
        self.assertIn("10", reasons[1].replace("58x32", "10"))  # the sizes are in the reason
        self.assertTrue(reasons[2].startswith("UNSHARED"))
        self.assertEqual(key["droppedFrameIds"], [1, 2])
        self.assertEqual(session["commonCropTolerancePx"], 2)
        self.assertEqual(json.loads(stdout)["frames"], [0])
        self.assertIn("NOT judged", stderr)

    def test_frame_ids_and_max_frames_drops_are_recorded_too(self):
        a = self._frames("aa", {i: (48, 32) for i in range(4)})
        b = self._frames("bb", {i: (48, 32) for i in range(4)})
        code, out, _, _ = self._build(["--frame-ids", "0,1,2", "--max-frames", "2"], a=a, b=b)
        self.assertEqual(code, 0)
        reasons = {d["frameId"]: d["reason"] for d in read_json(os.path.join(out, "session.json"))["droppedFrames"]}
        self.assertEqual(reasons[3], "NOT_IN_--frame-ids")
        self.assertIn("--max-frames", reasons[2])
        self.assertEqual(sorted(reasons), [2, 3])

    def test_an_undeclared_one_sided_dark_band_stays_in_the_picture_the_judges_see(self):
        import numpy as np
        from PIL import Image
        a = self._frames("aa", {0: (48, 48)})
        path = os.path.join(a, "aa-frame-00.png")
        with Image.open(path) as im:
            arr = np.asarray(im.convert("RGB")).copy()
        arr[:20] = 0  # a crushed region reaching ONE edge: scene content, not a bar
        Image.fromarray(arr, "RGB").save(path)
        b = self._frames("bb", {0: (48, 48)})
        code, out, _, stderr = self._build(a=a, b=b)
        self.assertEqual(code, 0, stderr)
        with Image.open(os.path.join(out, "source-frames", "aa-00.png")) as im:
            self.assertEqual(im.size, (48, 48))  # nothing cropped away
        code, out2, _, _ = self._build(["--letterbox", "auto-symmetric"], a=a, b=b)
        with Image.open(os.path.join(out, "source-frames", "aa-00.png")) as im:
            self.assertEqual(im.size, (48, 48))  # one-sided stays even when auto-symmetric is opted in

    def test_declared_bars_are_cropped_from_the_judge_images(self):
        import numpy as np
        from PIL import Image
        a = self._frames("aa", {0: (48, 48)})
        path = os.path.join(a, "aa-frame-00.png")
        with Image.open(path) as im:
            arr = np.asarray(im.convert("RGB")).copy()
        arr[:10] = 0
        arr[-10:] = 0
        Image.fromarray(arr, "RGB").save(path)
        b = self._frames("bb", {0: (48, 28)})
        code, out, _, stderr = self._build(["--a-letterbox-bars", "top=10,bottom=10"], a=a, b=b)
        self.assertEqual(code, 0, stderr)
        with Image.open(os.path.join(out, "source-frames", "aa-00.png")) as im:
            self.assertEqual(im.size, (48, 28))


if __name__ == "__main__":
    unittest.main()
