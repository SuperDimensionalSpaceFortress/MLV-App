#!/usr/bin/env python3
"""LOOK-METRICS-JUDGE-1: falsifier tests for the blind judge harness (tools/profiling/look/).

Most of this file is pure standard library, so the repo-hygiene CI image (no numpy / Pillow) runs it. The
classes that need to draw or compare pixels are skipped there and run wherever Pillow is installed
(`_HAS_PIL`), exactly as test_playback_attr_3_cuda_contact_sheet.py does for its real-interpreter test.

Every test pins ONE refusal: a frozen rubric, a blinded pair, an order-swapped vote that is discarded, a
slot-biased judge that is flagged, a control that must come back `tie`, a judge that is never the producer.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

_LOOK_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "profiling", "look"))
if _LOOK_DIR not in sys.path:
    sys.path.insert(0, _LOOK_DIR)

import look_config  # noqa: E402
import look_judges  # noqa: E402
import look_pairs  # noqa: E402
import look_tally  # noqa: E402

_HAS_PIL = importlib.util.find_spec("PIL") is not None


def _subject(name, frames):
    return {"name": name, "frames": {f: f"/nonexistent/{name}-{f}.png" for f in frames}}


def _key_from_plan(plan, subjects=("cuda", "cpu")):
    return {
        "subjects": list(subjects),
        "items": [
            {"itemId": it["itemId"], "unitId": it["unitId"], "kind": it["kind"], "order": it["order"],
             "frame": it["frame"], "left": {"subject": it["left"]["subject"]},
             "right": {"subject": it["right"]["subject"]}}
            for it in plan["items"]
        ],
    }


SESSION = {"rubricSha256": "f" * 64, "orderSeed": "seed-x", "imageSha256s": ["a" * 64]}
JUDGE = {"judgeId": "j1", "model": "m1", "family": "anthropic"}


def _scores(n=3):
    return {c: (None if c == "skin" else n) for c in look_config.CRITERIA}


def _verdict(preference, rubric=None):
    return {"left": _scores(), "right": _scores(), "preference": preference,
            "rubricSha256": rubric or SESSION["rubricSha256"]}


def _answers(plan, rule):
    return {it["itemId"]: _verdict(rule(it)) for it in plan["items"]}


def faithful(favourite):
    """Follows the PICTURE: prefers `favourite` in every real unit wherever it sits; ties on the control."""
    def rule(item):
        if item["kind"] == "control":
            return "tie"
        return "left" if item["left"]["subject"] == favourite else "right"
    return rule


def slot_follower(side):
    return lambda item: side


class ConfigTests(unittest.TestCase):
    def test_every_shipped_threshold_has_a_reason(self):
        with open(look_config.CONFIG_PATH, "r", encoding="utf-8") as handle:
            leaves = look_config.validate_config(json.load(handle))
        self.assertGreater(len(leaves), 20)
        for path, leaf in leaves:
            self.assertTrue(leaf["reason"].strip(), path)

    def test_bare_number_is_rejected(self):
        with open(look_config.CONFIG_PATH, "r", encoding="utf-8") as handle:
            doc = json.load(handle)
        doc["frame"]["clipped_highlight_pct_max"] = 1.0
        with self.assertRaises(look_config.ConfigError):
            look_config.validate_config(doc)

    def test_empty_reason_is_rejected(self):
        with open(look_config.CONFIG_PATH, "r", encoding="utf-8") as handle:
            doc = json.load(handle)
        doc["frame"]["crushed_shadow_pct_max"]["reason"] = "   "
        with self.assertRaises(look_config.ConfigError):
            look_config.validate_config(doc)

    def test_design_thresholds_are_the_ruled_ones(self):
        cfg, meta = look_config.load_config()
        self.assertEqual(cfg["frame"]["clipped_highlight_pct_max"], 1.0)  # B3: <= 1%
        self.assertEqual(cfg["frame"]["crushed_shadow_pct_max"], 2.0)     # B3: <= 2%
        self.assertEqual(cfg["frame"]["skin_hue_drift_deg_max"], 5.0)     # B3: ~5 degrees
        self.assertTrue(cfg["pair"]["scopes"]["shader-subset"]["gated"])  # A3: gated
        self.assertFalse(cfg["pair"]["scopes"]["full-look"]["gated"])     # A3: reported only
        self.assertEqual(len(meta["configSha256"]), 64)

    def test_config_digest_ignores_crlf_checkouts(self):
        with tempfile.TemporaryDirectory() as tmp:
            crlf = os.path.join(tmp, "c.json")
            with open(look_config.CONFIG_PATH, "rb") as src, open(crlf, "wb") as dst:
                dst.write(src.read().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
            self.assertEqual(look_config.load_config(crlf)[1]["configSha256"],
                             look_config.load_config()[1]["configSha256"])


class RubricFrozenTests(unittest.TestCase):
    def test_locked_digest_matches_the_shipped_rubric(self):
        lock = look_config.verify_rubric_lock()
        self.assertEqual(lock["rubricSha256"], look_config.rubric_digest())
        self.assertEqual(lock["criteria"], list(look_config.CRITERIA))

    def test_rubric_names_every_criterion_and_the_answer_format(self):
        text = look_config.load_rubric_text()
        for criterion in look_config.CRITERIA:
            self.assertIn(f"### {criterion}", text)
        for needle in ("`left`, `right` or `tie`", "anchored 1-5", "ONE JSON object"):
            self.assertIn(needle, text)

    def test_one_changed_character_breaks_the_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            rubric = os.path.join(tmp, "judge_rubric.md")
            lock = os.path.join(tmp, "judge_rubric.lock.json")
            shutil.copyfile(look_config.RUBRIC_PATH, rubric)
            look_config.write_rubric_lock(rubric, lock)
            look_config.verify_rubric_lock(rubric, lock)  # fine when untouched
            with open(rubric, "ab") as handle:
                handle.write(b" ")
            with self.assertRaises(look_config.RubricLockError):
                look_config.verify_rubric_lock(rubric, lock)

    def test_digest_is_portable_across_line_endings(self):
        with tempfile.TemporaryDirectory() as tmp:
            crlf = os.path.join(tmp, "r.md")
            with open(look_config.RUBRIC_PATH, "rb") as src, open(crlf, "wb") as dst:
                dst.write(src.read().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
            self.assertEqual(look_config.rubric_digest(crlf), look_config.rubric_digest())

    def test_shipped_files_are_pinned_to_lf(self):
        attrs = os.path.join(_LOOK_DIR, "..", "..", "..", ".gitattributes")
        with open(attrs, "r", encoding="utf-8") as handle:
            body = handle.read()
        for name in ("judge_rubric.md", "judge_rubric.lock.json", "look_floor_config.json"):
            self.assertIn(f"tools/profiling/look/{name} text eol=lf", body)


class PairPlanTests(unittest.TestCase):
    A = _subject("cuda", range(6))
    B = _subject("cpu", range(6))

    def _plan(self, seed="s1", frames=(0, 1, 2, 3), controls=1):
        return look_pairs.plan_pairs(self.A, self.B, list(frames), seed, controls=controls)

    def test_plan_is_deterministic_by_seed(self):
        one = json.dumps(self._plan("s1"), sort_keys=True)
        two = json.dumps(self._plan("s1"), sort_keys=True)
        self.assertEqual(one, two)
        self.assertNotEqual(one, json.dumps(self._plan("s2"), sort_keys=True))

    def test_caller_argument_order_does_not_decide_the_slot(self):
        forward = look_pairs.plan_pairs(self.A, self.B, [0, 1, 2, 3, 4, 5], "s1")
        swapped = look_pairs.plan_pairs(self.B, self.A, [0, 1, 2, 3, 4, 5], "s1")
        sides = lambda plan: {(i["unitId"], i["order"]): i["left"]["subject"] for i in plan["items"] if i["kind"] == "real"}
        # the slot of a subject is a function of (seed, unit, ordering) only: reversing the argument order
        # gives the same physical arrangement, so the seed alone reproduces the answer key
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

    def test_control_pair_is_identical_images_and_also_swapped(self):
        plan = self._plan(controls=1)
        control = [i for i in plan["items"] if i["kind"] == "control"]
        self.assertEqual(len(control), 2)
        for item in control:
            self.assertEqual(item["left"], item["right"])  # same subject, same frame, same file

    def test_left_right_is_not_always_the_same_subject(self):
        plan = look_pairs.plan_pairs(self.A, self.B, [0, 1, 2, 3, 4, 5], "s1")
        first_orders = {i["left"]["subject"] for i in plan["items"] if i["kind"] == "real" and i["order"] == 1}
        self.assertEqual(first_orders, {"cuda", "cpu"})

    def test_item_ids_are_opaque_and_unique(self):
        plan = self._plan()
        ids = [i["itemId"] for i in plan["items"]]
        self.assertEqual(len(ids), len(set(ids)))
        for item_id in ids:
            self.assertRegex(item_id, r"^p-[0-9a-f]{12}$")

    def test_same_subject_twice_and_no_common_frames_are_refused(self):
        with self.assertRaises(ValueError):
            look_pairs.plan_pairs(self.A, self.A, [0], "s")
        with self.assertRaises(ValueError):
            look_pairs.plan_pairs(self.A, _subject("cpu", [9]), [0], "s")

    def test_unshared_frames_are_reported_not_silently_dropped(self):
        plan = look_pairs.plan_pairs(self.A, _subject("cpu", [0, 1]), [0, 1, 2], "s")
        self.assertEqual(plan["droppedFrameIds"], [2])


@unittest.skipUnless(_HAS_PIL, "Pillow is not installed on this host")
class PairImageTests(unittest.TestCase):
    def _png(self, directory, name, size, colour):
        from PIL import Image
        path = os.path.join(directory, name)
        Image.new("RGB", size, colour).save(path)
        return path

    def test_session_hides_subject_names_and_records_the_rubric_digest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            frames_a = {i: self._png(tmp, f"flavor-classic-{i}.png", (40, 24), (200, 50, 50)) for i in range(3)}
            frames_b = {i: self._png(tmp, f"flavor-cinematic-{i}.png", (40, 24), (50, 50, 200)) for i in range(3)}
            out = os.path.join(tmp, "session")
            paths = look_pairs.build_session(
                {"name": "classic", "frames": frames_a}, {"name": "cinematic", "frames": frames_b},
                [0, 1, 2], "seed-7", out, "d" * 64, controls=1)
            with open(paths["judge_manifest.json"], "r", encoding="utf-8") as handle:
                judge_view = handle.read()
            for secret in ("classic", "cinematic", "cuda", "cpu", "seed-7", "flavor"):
                self.assertNotIn(secret, judge_view)
            for name in os.listdir(os.path.join(out, "images")):
                self.assertRegex(name, r"^p-[0-9a-f]{12}\.png$")
            with open(paths["session.json"], "r", encoding="utf-8") as handle:
                session = json.load(handle)
            self.assertEqual(session["rubricSha256"], "d" * 64)
            self.assertEqual(session["orderSeed"], "seed-7")
            self.assertEqual(session["itemCount"], 8)  # 3 real units x2 + 1 control x2
            self.assertTrue(session["emittedTwiceOrderSwapped"])
            with open(paths["answer_key.json"], "r", encoding="utf-8") as handle:
                self.assertIn("classic", handle.read())  # the key, and only the key, names subjects

    def test_pair_image_is_grey_gutter_no_text_and_byte_deterministic(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            left = self._png(tmp, "l.png", (30, 20), (250, 0, 0))
            right = self._png(tmp, "r.png", (30, 20), (0, 0, 250))
            one = look_pairs.compose_pair_image(left, right, os.path.join(tmp, "one.png"))
            two = look_pairs.compose_pair_image(left, right, os.path.join(tmp, "two.png"))
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
        with tempfile.TemporaryDirectory() as tmp:
            left = self._png(tmp, "l.png", (30, 20), (250, 0, 0))
            right = self._png(tmp, "r.png", (30, 24), (0, 0, 250))
            out = look_pairs.compose_pair_image(left, right, os.path.join(tmp, "o.png"))
            with Image.open(out) as im:
                self.assertEqual(im.size, (2 * look_pairs.MARGIN_PX + 60 + look_pairs.GUTTER_PX, 2 * look_pairs.MARGIN_PX + 24))
                self.assertEqual(im.getpixel((look_pairs.MARGIN_PX + 5, look_pairs.MARGIN_PX + 2 + 5)), (250, 0, 0))

    def test_run_session_judges_with_a_callable_and_refuses_when_the_rubric_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            fa = {i: self._png(tmp, f"a{i}.png", (40, 24), (200, 50, 50)) for i in range(2)}
            fb = {i: self._png(tmp, f"b{i}.png", (40, 24), (50, 50, 200)) for i in range(2)}
            out = os.path.join(tmp, "s")
            lock = look_config.verify_rubric_lock()
            look_pairs.build_session({"name": "a", "frames": fa}, {"name": "b", "frames": fb}, [0, 1], "k", out,
                                     lock["rubricSha256"], controls=1)
            seen = []

            def fn(png, rubric):
                seen.append(os.path.basename(png))
                return _verdict("tie")

            summary = look_judges.run_session(out, look_judges.CallableJudge(fn, "cj", "cm", "test"), workers=2)
            self.assertEqual((summary["judged"], summary["errors"], summary["remaining"]), (6, 0, 0))
            with open(summary["resultsPath"], "r", encoding="utf-8") as handle:
                doc = json.load(handle)
            first = next(iter(doc["items"].values()))
            self.assertEqual(first["rubricSha256"], lock["rubricSha256"])
            self.assertEqual(len(first["imageSha256"]), 64)
            # a session that recorded another digest is refused before any judge is called
            with open(os.path.join(out, "session.json"), "r", encoding="utf-8") as handle:
                session = json.load(handle)
            session["rubricSha256"] = "0" * 64
            with open(os.path.join(out, "session.json"), "w", encoding="utf-8") as handle:
                json.dump(session, handle)
            with self.assertRaises(look_config.RubricLockError):
                look_judges.run_session(out, look_judges.CallableJudge(fn, "cj2", "cm", "test"))
            session["rubricSha256"] = lock["rubricSha256"]
            with open(os.path.join(out, "session.json"), "w", encoding="utf-8") as handle:
                json.dump(session, handle)
            # ... and so is a rubric edited after the freeze
            rubric = os.path.join(tmp, "judge_rubric.md")
            lock_path = os.path.join(tmp, "judge_rubric.lock.json")
            shutil.copyfile(look_config.RUBRIC_PATH, rubric)
            look_config.write_rubric_lock(rubric, lock_path)
            with open(rubric, "ab") as handle:
                handle.write(b"edited")
            with self.assertRaises(look_config.RubricLockError):
                look_judges.run_session(out, look_judges.CallableJudge(fn, "cj3", "cm", "test"),
                                        rubric_path=rubric, lock_path=lock_path)


class TallyTests(unittest.TestCase):
    def setUp(self):
        a, b = _subject("cuda", range(5)), _subject("cpu", range(5))
        self.plan = look_pairs.plan_pairs(a, b, [0, 1, 2, 3, 4], "tally-seed", controls=1)
        self.key = _key_from_plan(self.plan)

    def _tally(self, rule, **kw):
        return look_tally.tally(self.key, _answers(self.plan, rule), JUDGE, SESSION, **kw)

    def test_a_judge_that_follows_the_picture_is_counted(self):
        entry = self._tally(faithful("cpu"))
        self.assertEqual(entry["preference"]["winner"], "cpu")
        self.assertEqual(entry["preference"]["consistentUnits"], 5)
        self.assertEqual(entry["preference"]["discardedFlips"], 0)
        self.assertEqual(entry["slotTally"]["real"], {"left": 5, "right": 5, "tie": 0})  # balanced by construction
        self.assertEqual(entry["slotTally"]["slotBias"], "NOT_FLAGGED")
        self.assertEqual(entry["controlResult"]["outcome"], "PASSED")
        self.assertTrue(entry["usable"])

    def test_order_flipping_votes_are_discarded(self):
        entry = self._tally(slot_follower("left"))
        self.assertEqual(entry["preference"]["discardedFlips"], 5)  # every unit flips with the swap
        self.assertEqual(entry["preference"]["consistentUnits"], 0)
        self.assertEqual(entry["preference"]["winner"], "NO_CONSISTENT_VOTE")
        self.assertEqual(entry["preference"]["votes"], {})

    def test_only_the_flipping_unit_is_discarded(self):
        flip_unit = "real-2"

        def rule(item):
            if item["unitId"] == flip_unit:
                return "left"
            return faithful("cuda")(item)

        entry = self._tally(rule)
        self.assertEqual(entry["preference"]["discardedFlips"], 1)
        self.assertEqual(entry["preference"]["votes"], {"cuda": 4})
        self.assertEqual(entry["preference"]["winner"], "cuda")

    def test_judge_that_prefers_a_slot_is_flagged_and_unusable(self):
        entry = self._tally(slot_follower("left"))
        self.assertEqual(entry["slotTally"]["slotBias"], "FLAGGED_LEFT")
        self.assertEqual(entry["controlResult"]["outcome"], "FAILED_PREFERENCE_ON_IDENTICAL_IMAGES")
        self.assertFalse(entry["usable"])
        self.assertIn("SLOT_BIAS", entry["unusableReasons"])
        self.assertIn("CONTROL_FAILED", entry["unusableReasons"])

    def test_right_slot_bias_is_named_right(self):
        self.assertEqual(self._tally(slot_follower("right"))["slotTally"]["slotBias"], "FLAGGED_RIGHT")

    def test_preference_on_identical_control_images_fails_the_control(self):
        def rule(item):
            return "left" if item["kind"] == "control" else faithful("cpu")(item)
        entry = self._tally(rule)
        self.assertEqual(entry["controlResult"]["outcome"], "FAILED_PREFERENCE_ON_IDENTICAL_IMAGES")
        self.assertFalse(entry["usable"])
        self.assertEqual(entry["preference"]["winner"], "cpu")  # the number is reported, but flagged unusable

    def test_too_few_choices_is_insufficient_not_unbiased(self):
        small = look_pairs.plan_pairs(_subject("cuda", [0]), _subject("cpu", [0]), [0], "t", controls=1)
        key = _key_from_plan(small)
        answers = {i["itemId"]: _verdict("left") for i in small["items"]}
        entry = look_tally.tally(key, answers, JUDGE, SESSION)
        self.assertEqual(entry["slotTally"]["slotBias"], "INSUFFICIENT_N")

    def test_tie_in_both_orders_is_a_consistent_tie(self):
        entry = self._tally(lambda item: "tie")
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

    def test_missing_ordering_leaves_the_unit_unjudged(self):
        answers = _answers(self.plan, faithful("cpu"))
        dropped = next(i["itemId"] for i in self.plan["items"] if i["unitId"] == "real-1" and i["order"] == 2)
        del answers[dropped]
        entry = look_tally.tally(self.key, answers, JUDGE, SESSION)
        self.assertEqual(entry["preference"]["unjudgedUnits"], 1)
        self.assertEqual(entry["preference"]["consistentUnits"], 4)

    def test_verdict_quoting_another_rubric_digest_is_rejected(self):
        answers = _answers(self.plan, faithful("cpu"))
        victim = next(iter(answers))
        answers[victim]["rubricSha256"] = "0" * 64
        entry = look_tally.tally(self.key, answers, JUDGE, SESSION)
        self.assertEqual([r["itemId"] for r in entry["invalidVerdicts"]], [victim])
        self.assertIn("rubricSha256", entry["invalidVerdicts"][0]["problems"][0])

    def test_malformed_verdicts_are_invalid_not_scored(self):
        good = _verdict("left")
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

    def test_entry_carries_the_receipt_field_names(self):
        entry = self._tally(faithful("cpu"), cross_family_status="CROSS_FAMILY_UNAVAILABLE")
        for field in ("judgeId", "model", "family", "rubricSha256", "imageSha256s", "orderSeed", "slotTally",
                      "scores", "preference"):
            self.assertIn(field, entry)
        self.assertEqual(entry["rubricSha256"], SESSION["rubricSha256"])
        self.assertEqual(entry["orderSeed"], "seed-x")
        self.assertEqual(entry["crossFamilyStatus"], "CROSS_FAMILY_UNAVAILABLE")

    def test_binomial_p_values(self):
        self.assertAlmostEqual(look_tally.binomial_two_sided_p(0, 6), 0.03125)
        self.assertAlmostEqual(look_tally.binomial_two_sided_p(3, 6), 1.0)
        self.assertAlmostEqual(look_tally.binomial_two_sided_p(6, 6), 0.03125)
        self.assertEqual(look_tally.binomial_two_sided_p(0, 0), 1.0)

    def test_judges_more_than_one_point_apart_need_a_third(self):
        base = self._tally(faithful("cpu"))
        other = json.loads(json.dumps(base))
        other["judgeId"] = "j2"
        self.assertFalse(look_tally.judge_disagreement([base, other])["thirdJudgeNeeded"])
        other["scores"]["cpu"]["colour_cast"] = base["scores"]["cpu"]["colour_cast"] + 1.5
        verdict = look_tally.judge_disagreement([base, other])
        self.assertTrue(verdict["thirdJudgeNeeded"])
        self.assertEqual(verdict["details"][0]["criterion"], "colour_cast")
        other["scores"]["cpu"]["colour_cast"] = base["scores"]["cpu"]["colour_cast"] + 1.0  # exactly 1: not "more than"
        self.assertFalse(look_tally.judge_disagreement([base, other])["thirdJudgeNeeded"])


class JudgeRunnerTests(unittest.TestCase):
    def test_a_judge_is_never_the_producer_or_the_hub(self):
        forbidden = ["claude-sonnet-5-5", "claude-opus-5-5"]
        for model in ("claude-sonnet-5-5", "CLAUDE-OPUS-5-5", "claude-sonnet-5-5-20261001"):
            with self.subTest(model):
                with self.assertRaises(look_judges.ProducerJudgeError):
                    look_judges.assert_not_producer(model, forbidden)
        look_judges.assert_not_producer("claude-fable-5-1", forbidden)
        look_judges.assert_not_producer("codex-default", forbidden)
        with self.assertRaises(look_judges.ProducerJudgeError):
            look_judges.assert_not_producer("", forbidden)

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
        def fake_run(argv, **kwargs):
            body = version if "--version" in argv else text
            return subprocess.CompletedProcess(argv, 0, stdout=body, stderr="")
        return fake_run

    def test_probe_reports_flag_present_but_not_proven(self):
        with mock.patch.object(look_judges.subprocess, "run", self._help("  -i, --image <FILE>...  attach")):
            probe = look_judges.probe_codex_image_support("codex")
        self.assertEqual(probe["status"], look_judges.CROSS_FAMILY_FLAG_ONLY)
        self.assertEqual(probe["imageFlag"], "--image")

    def test_probe_without_the_flag_is_cross_family_unavailable(self):
        with mock.patch.object(look_judges.subprocess, "run", self._help("  -m, --model <MODEL>")):
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
        self.assertEqual(look_judges.family_of("gpt-5"), "openai")
        self.assertEqual(look_judges.family_of("mystery"), "unknown")


if __name__ == "__main__":
    unittest.main()
