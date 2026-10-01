#!/usr/bin/env python3
"""LOOK-METRICS-JUDGE-1: falsifier tests for the objective look floor (tools/profiling/look/look_metrics.py).

Every image here is SYNTHETIC with a KNOWN-by-construction answer (a patch that is exactly 5% white, a
brightness offset of exactly +5 codes, a skin patch rotated by a known hue). Needs numpy + Pillow, which hosted
CI installs from .github/requirements/repo-hygiene.txt (pinned with hashes), so these tests RUN there on both
OSes; they skip only on a developer host without the packages, and test_look_judge_harness.CiPinsTests fails
if they would be skipped in CI. The pure-stdlib half of the harness is covered by test_look_judge_harness.py.

The round-1 reproductions (a one-sided / symmetric dark band that hid a crushed frame, skin pushed out of the
colour box reading NOT_APPLICABLE, a bare pair threshold turning a geometry refusal into PASS) are pinned here.
"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest

_LOOK_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "profiling", "look"))
if _LOOK_DIR not in sys.path:
    sys.path.insert(0, _LOOK_DIR)

_HAS_DEPS = importlib.util.find_spec("numpy") is not None and importlib.util.find_spec("PIL") is not None

if _HAS_DEPS:
    import numpy as np
    from PIL import Image

    import look_config
    import look_metrics as lm


def _flat(h, w, rgb):
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    arr[:, :] = rgb
    return arr


def _gradient(h=64, w=96):
    ramp = np.linspace(40, 200, w, dtype=np.float64)
    base = np.tile(ramp, (h, 1))
    return np.stack([base, base * 0.9 + 5, base * 0.8 + 10], axis=2).astype(np.uint8)


def _noise(shape, seed, sigma):
    return np.random.RandomState(seed).normal(0.0, sigma, shape)


@unittest.skipUnless(_HAS_DEPS, "numpy/Pillow are not installed on this host")
class FloorMetricTests(unittest.TestCase):
    def setUp(self):
        self.cfg, self.meta = look_config.load_config()

    def _frame(self, arr, **kw):
        return lm.evaluate_frame(arr, self.cfg, **kw)

    def test_clipped_highlight_percentage_is_exact(self):
        arr = _flat(100, 100, (120, 120, 120))
        arr[:5, :] = 255  # exactly 5.0 % of the pixels
        v = self._frame(arr)
        self.assertAlmostEqual(v["metrics"]["clipped_highlight_pct"], 5.0, places=4)
        self.assertEqual(v["checks"]["clipped_highlight"]["outcome"], lm.FAIL)
        self.assertEqual(v["outcome"], lm.FAIL)
        self.assertIn("clipped_highlight", v["failedChecks"])

    def test_exactly_one_percent_clipped_is_a_pass_and_just_over_is_not(self):
        arr = _flat(100, 100, (120, 120, 120))
        arr[:1, :] = 255  # 1.00 %
        self.assertEqual(self._frame(arr)["checks"]["clipped_highlight"]["outcome"], lm.PASS)
        arr[1, :2] = 255  # 1.02 %
        self.assertEqual(self._frame(arr)["checks"]["clipped_highlight"]["outcome"], lm.FAIL)

    def test_crushed_shadow_percentage_is_exact(self):
        arr = _flat(100, 100, (120, 120, 120))
        arr[0:10, 0:30] = 0  # 300 px = 3 %; a corner block is not a full-width row or full-height column, so not a bar
        v = self._frame(arr)
        self.assertAlmostEqual(v["metrics"]["crushed_shadow_pct"], 3.0, places=4)
        self.assertEqual(v["checks"]["crushed_shadow"]["outcome"], lm.FAIL)

    def test_clean_frame_passes_every_check(self):
        v = self._frame(_gradient())
        self.assertEqual(v["outcome"], lm.PASS)
        self.assertEqual(v["failedChecks"], [])

    def test_saturation_ceiling_mean_and_oversaturated_share(self):
        vivid = _flat(60, 60, (255, 20, 20))  # HSV saturation ~0.92, value 1.0
        v = self._frame(vivid)
        self.assertGreater(v["metrics"]["mean_saturation"], self.cfg["frame"]["mean_saturation_max"])
        self.assertEqual(v["checks"]["mean_saturation"]["outcome"], lm.FAIL)
        self.assertEqual(v["checks"]["oversaturated_pixels"]["outcome"], lm.FAIL)
        self.assertAlmostEqual(v["metrics"]["oversaturated_pixel_pct"], 100.0, places=3)
        grey = _flat(60, 60, (128, 128, 128))
        self.assertEqual(self._frame(grey)["checks"]["mean_saturation"]["outcome"], lm.PASS)

    def test_oversaturation_ignores_near_black_pixels(self):
        arr = _flat(60, 60, (100, 100, 100))
        arr[:20, :20] = (20, 0, 0)  # saturation 1.0 but value 0.078 < 0.15: noise, not colour
        v = self._frame(arr)
        self.assertEqual(v["metrics"]["oversaturated_pixel_pct"], 0.0)

    def test_numbers_are_the_make_contact_sheet_numbers(self):
        # reuse, not re-implementation: the floor's luma/clip/crush/saturation must equal channel_stats on the same pixels
        spec = importlib.util.spec_from_file_location(
            "mcs_for_test", os.path.join(_LOOK_DIR, os.pardir, "make-contact-sheet.py"))
        mcs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mcs)
        arr = _gradient()
        arr[:3, :4] = 255
        ref = mcs.channel_stats(Image.fromarray(arr, "RGB"))
        got = self._frame(arr)["metrics"]
        self.assertAlmostEqual(got["clipped_highlight_pct"], ref["clipped_highlight_pct"], places=4)
        self.assertAlmostEqual(got["crushed_shadow_pct"], ref["crushed_black_pct"], places=4)
        self.assertAlmostEqual(got["mean_saturation"], ref["mean_saturation"], places=4)
        self.assertAlmostEqual(got["luma_p50"], ref["luma_p50"], places=3)

    def test_unreadable_input_is_a_typed_terminal_not_a_pass(self):
        v = lm.evaluate_frame(os.path.join(tempfile.gettempdir(), "no-such-look-frame.png"), self.cfg)
        self.assertEqual(v["outcome"], lm.NOT_EVALUABLE)
        self.assertIn("error", v)
        v = lm.evaluate_frame(np.zeros((4, 4), dtype=np.uint8), self.cfg)
        self.assertEqual(v["outcome"], lm.NOT_EVALUABLE)

    def test_verdict_binds_to_the_config_digest_and_carries_its_version(self):
        sheet = lm.evaluate_sheet({0: _gradient()}, self.cfg, self.meta, label="x")
        self.assertEqual(sheet["config"]["configSha256"], self.meta["configSha256"])
        self.assertEqual(sheet["config"]["configVersion"], 1)  # round 1 wrote null here
        self.assertEqual(sheet["schema"], lm.SCHEMA_FRAME_SHEET)
        pair = lm.compare_sheets({0: _gradient()}, {0: _gradient()}, self.cfg, self.meta, "shader-subset")
        self.assertEqual(pair["config"]["configVersion"], 1)
        self.assertEqual(sheet["letterboxPolicy"]["mode"], "off")


@unittest.skipUnless(_HAS_DEPS, "numpy/Pillow are not installed on this host")
class LetterboxTests(unittest.TestCase):
    def setUp(self):
        self.cfg, self.meta = look_config.load_config()

    def _barred(self, top=20, bottom=20):
        arr = _gradient(100, 100)
        if top:
            arr[:top] = 0
        if bottom:
            arr[-bottom:] = 0
        return arr

    def _auto(self):
        return lm.make_letterbox_policy(self.cfg, mode="auto-symmetric")

    # -- nothing is hidden unless the caller says so ---------------------------------------------------
    def test_the_shipped_default_hides_nothing_so_a_barred_frame_fails_loudly(self):
        v = lm.evaluate_frame(self._barred(), self.cfg)
        lb = v["geometry"]["letterbox"]
        self.assertEqual((lb["top"], lb["bottom"], lb["left"], lb["right"]), (0, 0, 0, 0))
        self.assertEqual(lb["provenance"], "NONE")
        self.assertEqual(lb["candidate"], {"top": 20, "bottom": 20, "left": 0, "right": 0})  # seen, not hidden
        self.assertIn("UNDECLARED_DARK_BANDS_MEASURED_AS_SCENE", lb["notes"])
        self.assertAlmostEqual(v["metrics"]["crushed_shadow_pct"], 40.0, places=3)
        self.assertEqual(v["outcome"], lm.FAIL)
        sheet = lm.evaluate_sheet({0: self._barred()}, self.cfg, self.meta)
        self.assertEqual(sheet["framesWithDarkBandsMeasuredAsScene"], [0])

    def test_round1_repro_symmetric_crushed_scene_content_no_longer_passes(self):
        # Sol B4: 100x100, code-8 picture with the first and last 20 rows crushed to zero, NO capture bars.
        arr = _flat(100, 100, (8, 8, 8))
        arr[:20] = 0
        arr[-20:] = 0
        v = lm.evaluate_frame(arr, self.cfg)
        self.assertAlmostEqual(v["metrics"]["crushed_shadow_pct"], 40.0, places=3)
        self.assertEqual(v["checks"]["crushed_shadow"]["outcome"], lm.FAIL)
        self.assertEqual(v["outcome"], lm.FAIL)

    def test_round1_repro_one_sided_dark_band_no_longer_passes_even_with_auto_symmetric_opted_in(self):
        # Fable B1: arr[:40] = 0 on a gradient. A band on ONE edge is scene content, whatever the policy.
        arr = _gradient(100, 100)
        arr[:40] = 0
        for policy in (None, self._auto()):
            v = lm.evaluate_frame(arr, self.cfg, letterbox_policy=policy)
            self.assertAlmostEqual(v["metrics"]["crushed_shadow_pct"], 40.0, places=3)
            self.assertEqual(v["outcome"], lm.FAIL)
            self.assertEqual(v["geometry"]["letterbox"]["top"], 0)
        self.assertIn("ONE_SIDED_DARK_BAND_IS_SCENE_CONTENT",
                      lm.evaluate_frame(arr, self.cfg, letterbox_policy=self._auto())["geometry"]["letterbox"]["notes"])

    def test_declared_bars_are_excluded_and_recorded_as_declared(self):
        policy = lm.make_letterbox_policy(self.cfg, declared={"top": 20, "bottom": 20})
        v = lm.evaluate_frame(self._barred(), self.cfg, letterbox_policy=policy)
        lb = v["geometry"]["letterbox"]
        self.assertEqual((lb["top"], lb["bottom"]), (20, 20))
        self.assertEqual(lb["provenance"], "DECLARED")
        self.assertAlmostEqual(lb["excludedPct"], 40.0, places=3)
        self.assertEqual(v["metrics"]["crushed_shadow_pct"], 0.0)
        self.assertAlmostEqual(v["metrics"]["crushed_shadow_pct_full_frame"], 40.0, places=3)  # the hidden share is on record
        self.assertEqual(v["outcome"], lm.PASS)
        self.assertEqual((v["geometry"]["activeHeight"], v["geometry"]["activeWidth"]), (60, 100))

    def test_declared_bars_that_are_not_actually_dark_are_refused_and_the_full_frame_is_measured(self):
        policy = lm.make_letterbox_policy(self.cfg, declared={"top": 30, "bottom": 20})  # only 20 rows are dark
        v = lm.evaluate_frame(self._barred(), self.cfg, letterbox_policy=policy)
        self.assertEqual(v["geometry"]["letterbox"]["refused"], "DECLARED_BARS_NOT_DARK")
        self.assertEqual(v["geometry"]["activeHeight"], 100)
        self.assertEqual(v["outcome"], lm.FAIL)

    def test_a_declaration_cannot_hide_a_dark_scene_it_was_never_true_of(self):
        arr = _gradient(100, 100)  # no dark rows at all
        policy = lm.make_letterbox_policy(self.cfg, declared={"top": 10, "bottom": 10})
        self.assertEqual(lm.evaluate_frame(arr, self.cfg, letterbox_policy=policy)["geometry"]["letterbox"]["refused"],
                         "DECLARED_BARS_NOT_DARK")

    def test_auto_symmetric_is_an_explicit_opt_in_that_excludes_symmetric_bars_within_the_tolerance(self):
        v = lm.evaluate_frame(self._barred(), self.cfg, letterbox_policy=self._auto())
        lb = v["geometry"]["letterbox"]
        self.assertEqual((lb["top"], lb["bottom"], lb["provenance"]), (20, 20, "AUTO_SYMMETRIC"))
        self.assertEqual(v["outcome"], lm.PASS)
        tol = self.cfg["letterbox"]["symmetry_tolerance_px"]
        near = lm.detect_letterbox(self._barred(20, 20 + tol), self.cfg, self._auto())
        self.assertEqual((near["top"], near["bottom"]), (20, 20 + tol))
        far = lm.detect_letterbox(self._barred(20, 20 + tol + 1), self.cfg, self._auto())
        self.assertEqual((far["top"], far["bottom"], far["provenance"]), (0, 0, "NONE"))

    def test_the_config_can_turn_auto_symmetric_on_but_the_shipped_value_is_off(self):
        cfg = look_config.load_config()[0]
        self.assertFalse(lm.make_letterbox_policy(cfg)["mode"] == "auto-symmetric")
        cfg["letterbox"]["auto_exclude_symmetric"] = True
        self.assertEqual(lm.make_letterbox_policy(cfg)["mode"], "auto-symmetric")
        self.assertEqual(lm.evaluate_frame(self._barred(), cfg)["outcome"], lm.PASS)

    def test_pillarbox_is_found_too_under_the_opt_in_and_never_one_sided(self):
        arr = _gradient(100, 100)
        arr[:, :10] = 0
        arr[:, -10:] = 0
        lb = lm.evaluate_frame(arr, self.cfg, letterbox_policy=self._auto())["geometry"]["letterbox"]
        self.assertEqual((lb["left"], lb["right"]), (10, 10))
        one = _gradient(100, 100)
        one[:, :10] = 0
        lb = lm.evaluate_frame(one, self.cfg, letterbox_policy=self._auto())["geometry"]["letterbox"]
        self.assertEqual((lb["left"], lb["right"]), (0, 0))

    def test_an_all_black_frame_is_not_cropped_to_nothing(self):
        v = lm.evaluate_frame(_flat(50, 50, (0, 0, 0)), self.cfg, letterbox_policy=self._auto())
        self.assertEqual(v["geometry"]["letterbox"]["refused"], "ALL_BLACK_FRAME")
        self.assertEqual(v["outcome"], lm.FAIL)  # 100 % crushed

    def test_an_implausible_exclusion_is_refused_as_a_dark_frame(self):
        arr = _gradient(100, 100)
        arr[:35] = 0
        arr[-35:] = 0  # 70 % black, symmetric: still more likely a dark frame than a letterbox
        for policy in (self._auto(), lm.make_letterbox_policy(self.cfg, declared={"top": 35, "bottom": 35})):
            v = lm.evaluate_frame(arr, self.cfg, letterbox_policy=policy)
            self.assertEqual(v["geometry"]["letterbox"]["refused"], "EXCLUSION_IMPLAUSIBLE_PROBABLY_DARK_FRAME")
            self.assertEqual(v["geometry"]["activeHeight"], 100)
            self.assertEqual(v["outcome"], lm.FAIL)

    def test_ringing_next_to_a_bar_within_the_bar_code_still_counts_as_bar(self):
        arr = self._barred()
        arr[19] = 3  # <= bar_max_code (4)
        arr[-20] = 3
        lb = lm.detect_letterbox(arr, self.cfg, self._auto())
        self.assertEqual((lb["top"], lb["bottom"]), (20, 20))

    def test_a_real_dark_row_is_not_a_bar(self):
        arr = _gradient(100, 100)
        arr[:5] = 0
        arr[5, 40:60] = 90  # one lit pixel run: the row is content
        self.assertEqual(lm.detect_letterbox(arr, self.cfg)["candidate"]["top"], 5)

    def test_policy_and_bar_spec_parsing_refuse_nonsense(self):
        self.assertEqual(lm.parse_declared_bars("top=34,bottom=34"), {"top": 34, "bottom": 34})
        for bad in ("", "top", "top=x", "middle=3", "top=-1"):
            with self.assertRaises(ValueError):
                lm.parse_declared_bars(bad)
        with self.assertRaises(ValueError):
            lm.make_letterbox_policy(self.cfg, mode="always")
        with self.assertRaises(ValueError):
            lm.make_letterbox_policy(self.cfg, declared={"top": -2})


@unittest.skipUnless(_HAS_DEPS, "numpy/Pillow are not installed on this host")
class SkinTests(unittest.TestCase):
    def setUp(self):
        self.cfg, self.meta = look_config.load_config()

    def _skin_scene(self, rgb):
        arr = _flat(100, 100, (90, 110, 160))  # blue-ish background, not skin
        arr[30:60, 30:60] = rgb  # 900 px = 9 % skin-tone region
        return arr

    def test_skin_patch_is_detected_and_background_is_not(self):
        mask = lm.skin_tone_region(self._skin_scene((224, 172, 140)), self.cfg)
        self.assertEqual(int(mask.sum()), 900)
        self.assertFalse(mask[0, 0])

    def test_no_skin_region_in_the_baseline_is_not_applicable_never_a_pass(self):
        base = _flat(80, 80, (90, 110, 160))
        v = lm.evaluate_frame(base, self.cfg, reference=base)
        self.assertEqual(v["checks"]["skin_hue_drift"]["outcome"], lm.NOT_APPLICABLE)
        self.assertEqual(v["checks"]["skin_hue_drift"]["reason"], "NO_SKIN_TONE_REGION_IN_BASELINE")

    def test_round1_repro_skin_pushed_out_of_the_colour_box_is_a_FAIL_not_NOT_APPLICABLE(self):
        # Sol B5 / Fable B2: the baseline has a 9 % skin region; the subject moves ONLY that patch.
        base = self._skin_scene((224, 172, 140))
        for label, rgb in (("blue (sol)", (140, 172, 224)), ("green (fable)", (150, 200, 150)),
                           ("magenta (fable)", (224, 150, 190)), ("grey", (128, 128, 128))):
            with self.subTest(label):
                v = lm.evaluate_frame(self._skin_scene(rgb), self.cfg, reference=base)
                check = v["checks"]["skin_hue_drift"]
                self.assertEqual(check["outcome"], lm.FAIL)
                self.assertEqual(check["reason"], "SKIN_REGION_LOST_OR_SHRUNK")
                self.assertEqual(v["outcome"], lm.FAIL)
                self.assertIn("skin_hue_drift", v["failedChecks"])
                self.assertEqual(check["baselineRegionPct"], 9.0)
                self.assertEqual(check["subjectRegionPct"], 0.0)

    def test_a_100_degree_drift_can_no_longer_pass_while_a_6_degree_one_fails(self):
        base = self._skin_scene((224, 172, 140))
        far = lm.evaluate_frame(self._skin_scene((150, 200, 150)), self.cfg, reference=base)
        near = lm.evaluate_frame(self._skin_scene((224, 150, 140)), self.cfg, reference=base)
        self.assertEqual((far["outcome"], near["outcome"]), (lm.FAIL, lm.FAIL))

    def test_a_sheet_with_a_lost_skin_region_fails_and_counts_the_check_as_applied(self):
        base = self._skin_scene((224, 172, 140))
        sheet = lm.evaluate_sheet({0: self._skin_scene((140, 172, 224))}, self.cfg, self.meta, references={0: base})
        self.assertEqual(sheet["outcome"], lm.FAIL)
        self.assertEqual((sheet["skinCheck"]["appliedToFrames"], sheet["skinCheck"]["ofFrames"]), (1, 1))

    def test_only_part_of_the_skin_moving_is_caught_by_measuring_over_the_baselines_mask(self):
        base = self._skin_scene((224, 172, 140))
        half = base.copy()
        half[30:60, 45:60] = (150, 200, 150)  # half the patch turns green; the other half keeps its own hue
        v = lm.evaluate_frame(half, self.cfg, reference=base)
        check = v["checks"]["skin_hue_drift"]
        self.assertEqual(check["maskBasis"], "BASELINE_MASK")
        self.assertEqual(check["outcome"], lm.FAIL)
        self.assertGreater(check["value"], 30.0)  # the subject's own mask alone would have read ~0

    def test_a_shrunken_region_below_the_retain_fraction_fails_and_one_above_it_is_measured(self):
        base = self._skin_scene((224, 172, 140))
        shrunk = self._skin_scene((224, 172, 140))
        shrunk[30:60, 50:60] = (90, 110, 160)  # a third of the patch gone: 6 % left of 9 %, above retain 0.5
        kept = lm.evaluate_frame(shrunk, self.cfg, reference=base)["checks"]["skin_hue_drift"]
        self.assertEqual(kept["maskBasis"], "BASELINE_MASK")  # kept, so the drift over the baseline's pixels is measured
        self.assertNotIn("reason", kept)
        self.assertEqual(kept["outcome"], lm.FAIL)            # ... and the pixels that stopped being skin show as drift
        shrunk[30:60, 33:60] = (90, 110, 160)  # 0.9 % left of 9 %: below the 4.5 % that must be kept
        lost = lm.evaluate_frame(shrunk, self.cfg, reference=base)["checks"]["skin_hue_drift"]
        self.assertEqual((lost["outcome"], lost["reason"]), (lm.FAIL, "SKIN_REGION_LOST_OR_SHRUNK"))

    def test_the_retain_fraction_is_the_configs(self):
        base = self._skin_scene((224, 172, 140))
        shrunk = self._skin_scene((224, 172, 140))
        shrunk[30:60, 33:60] = (90, 110, 160)
        self.assertEqual(lm.evaluate_frame(shrunk, self.cfg, reference=base)["checks"]["skin_hue_drift"]["reason"],
                         "SKIN_REGION_LOST_OR_SHRUNK")
        cfg = look_config.load_config()[0]
        cfg["frame"]["skin_region_retain_fraction"] = 0.05
        relaxed = lm.evaluate_frame(shrunk, cfg, reference=base)["checks"]["skin_hue_drift"]
        self.assertNotIn("reason", relaxed)  # no longer 'lost': the config decided
        self.assertEqual(relaxed["maskBasis"], "BASELINE_MASK")

    def test_a_baseline_that_was_given_but_cannot_be_read_is_not_evaluable_never_not_applicable(self):
        v = lm.evaluate_frame(self._skin_scene((224, 172, 140)), self.cfg,
                              reference=os.path.join(tempfile.gettempdir(), "no-such-baseline.png"))
        self.assertEqual(v["outcome"], lm.NOT_EVALUABLE)
        self.assertIn("BASELINE_UNREADABLE", v["error"])
        sheet = lm.evaluate_sheet({0: self._skin_scene((224, 172, 140))}, self.cfg, self.meta,
                                  references={0: np.zeros((4, 4), dtype=np.uint8)})
        self.assertEqual(sheet["outcome"], lm.INCOMPLETE)

    def test_frames_that_cannot_be_aligned_are_NOT_EVALUABLE_never_compared_on_their_own_regions(self):
        # LOOK-METRICS-JUDGE-2 item 4 (fable r2 note): the old fallback compared each frame's OWN mask, which can
        # miss a partial skin move. An unalignable pair now makes the frame (and the sheet) INCOMPLETE.
        base = self._skin_scene((224, 172, 140))
        wide = np.concatenate([base, base[:, :20]], axis=1)  # 20 px wider than the tolerance allows
        v = lm.evaluate_frame(wide, self.cfg, reference=base)
        check = v["checks"]["skin_hue_drift"]
        self.assertEqual((check["outcome"], check["reason"]), (lm.NOT_EVALUABLE, "SKIN_MASKS_NOT_ALIGNABLE"))
        self.assertEqual(v["outcome"], lm.NOT_EVALUABLE)
        sheet = lm.evaluate_sheet({0: wide}, self.cfg, self.meta, references={0: base})
        self.assertEqual(sheet["outcome"], lm.INCOMPLETE)
        self.assertEqual([r["code"] for r in sheet["incompleteReasons"]], ["FRAME_NOT_EVALUABLE"])

    def test_a_partial_skin_move_on_unalignable_frames_cannot_pass(self):
        base = self._skin_scene((224, 172, 140))
        half = base.copy()
        half[30:60, 45:60] = (150, 200, 150)  # half the patch moves; the other half keeps its own hue
        half = np.concatenate([half, half[:, :20]], axis=1)
        v = lm.evaluate_frame(half, self.cfg, reference=base)
        self.assertNotEqual(v["outcome"], lm.PASS)
        self.assertNotIn("maskBasis", v["checks"]["skin_hue_drift"])

    def test_a_thumbnail_one_row_taller_than_the_baseline_is_still_aligned(self):
        base = self._skin_scene((224, 172, 140))
        taller = np.concatenate([base, base[-2:]], axis=0)  # +2 rows: inside the 4 px crop tolerance
        check = lm.evaluate_frame(taller, self.cfg, reference=base)["checks"]["skin_hue_drift"]
        self.assertEqual((check["maskBasis"], check["outcome"]), ("BASELINE_MASK", lm.PASS))

    def test_without_a_baseline_the_skin_check_is_not_applicable(self):
        v = lm.evaluate_frame(self._skin_scene((224, 172, 140)), self.cfg)
        self.assertEqual(v["checks"]["skin_hue_drift"]["outcome"], lm.NOT_APPLICABLE)
        self.assertEqual(v["checks"]["skin_hue_drift"]["reason"], "NO_BASELINE_FRAME")
        self.assertGreater(v["metrics"]["skin_tone_region_pct"], 8.0)  # still reported

    def test_identical_baseline_has_zero_drift(self):
        scene = self._skin_scene((224, 172, 140))
        v = lm.evaluate_frame(scene, self.cfg, reference=scene)
        check = v["checks"]["skin_hue_drift"]
        self.assertEqual(check["outcome"], lm.PASS)
        self.assertAlmostEqual(check["value"], 0.0, places=3)

    def test_a_hue_shift_beyond_five_degrees_fails_and_a_small_one_passes(self):
        base = self._skin_scene((224, 172, 140))
        warm = self._skin_scene((224, 150, 140))   # HSV hue 22.9 -> 7.1 degrees: a move of ~15.7, still inside the skin box
        tiny = self._skin_scene((224, 171, 140))   # sub-degree move
        far = lm.evaluate_frame(warm, self.cfg, reference=base)["checks"]["skin_hue_drift"]
        near = lm.evaluate_frame(tiny, self.cfg, reference=base)["checks"]["skin_hue_drift"]
        self.assertGreater(far["value"], 5.0)
        self.assertEqual(far["outcome"], lm.FAIL)
        self.assertLess(near["value"], 5.0)
        self.assertEqual(near["outcome"], lm.PASS)

    def test_circular_hue_distance_wraps(self):
        self.assertAlmostEqual(lm.hue_distance_deg(359.0, 1.0), 2.0)
        self.assertAlmostEqual(lm.hue_distance_deg(10.0, 350.0), 20.0)

    def test_sheet_reports_how_many_frames_the_skin_check_actually_applied_to(self):
        skin = self._skin_scene((224, 172, 140))
        none = _flat(80, 80, (90, 110, 160))
        sheet = lm.evaluate_sheet({0: skin, 1: none}, self.cfg, self.meta, references={0: skin, 1: none})
        self.assertEqual((sheet["skinCheck"]["appliedToFrames"], sheet["skinCheck"]["ofFrames"]), (1, 2))
        self.assertEqual(sheet["outcome"], lm.PASS)  # N/A frames do not fail the sheet, and are never counted as passes


@unittest.skipUnless(_HAS_DEPS, "numpy/Pillow are not installed on this host")
class SheetAggregationTests(unittest.TestCase):
    def setUp(self):
        self.cfg, self.meta = look_config.load_config()

    def test_one_failing_frame_fails_the_sheet_no_partial_credit(self):
        bad = _flat(50, 50, (255, 255, 255))
        sheet = lm.evaluate_sheet({0: _gradient(), 1: bad, 2: _gradient()}, self.cfg, self.meta)
        self.assertEqual(sheet["outcome"], lm.FAIL)
        self.assertEqual(sheet["counts"], {lm.PASS: 2, lm.FAIL: 1, lm.NOT_EVALUABLE: 0})

    def test_an_empty_sheet_is_incomplete_not_a_pass(self):
        self.assertEqual(lm.evaluate_sheet({}, self.cfg, self.meta)["outcome"], lm.INCOMPLETE)

    def test_one_unreadable_frame_makes_the_sheet_incomplete(self):
        sheet = lm.evaluate_sheet({0: _gradient(), 1: "/no/such/file.png"}, self.cfg, self.meta)
        self.assertEqual(sheet["outcome"], lm.INCOMPLETE)
        self.assertEqual(sheet["counts"][lm.NOT_EVALUABLE], 1)

    def test_frames_load_from_png_files_and_are_indexed_by_trailing_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            for i in (0, 3, 12):
                Image.fromarray(_gradient(), "RGB").save(os.path.join(tmp, f"cuda-frame-{i:02d}.png"))
            open(os.path.join(tmp, "notes.txt"), "w").close()
            self.assertEqual(sorted(lm.index_frames(tmp)), [0, 3, 12])
            sheet = lm.evaluate_sheet(lm.index_frames(tmp), self.cfg, self.meta)
            self.assertEqual(sheet["outcome"], lm.PASS)
            self.assertEqual(len(sheet["frames"][0]["imageSha256"]), 64)


@unittest.skipUnless(_HAS_DEPS, "numpy/Pillow are not installed on this host")
class SsimTests(unittest.TestCase):
    def test_identical_images_are_exactly_one(self):
        g = lm.luma(_gradient(64, 96))
        self.assertAlmostEqual(lm.ssim(g, g), 1.0, places=9)
        n = np.random.RandomState(1).uniform(0, 255, (48, 48))
        self.assertAlmostEqual(lm.ssim(n, n), 1.0, places=9)

    def test_two_constant_images_match_the_closed_form(self):
        # sigma = 0 everywhere, so SSIM = (2*a*b + C1) / (a^2 + b^2 + C1) with C1 = (0.01 * 255)^2
        a, b = 100.0, 110.0
        c1 = (0.01 * 255) ** 2
        expected = (2 * a * b + c1) / (a * a + b * b + c1)
        got = lm.ssim(np.full((32, 32), a), np.full((32, 32), b))
        self.assertAlmostEqual(got, expected, places=9)

    def test_symmetric(self):
        x = lm.luma(_gradient(48, 48))
        y = x + _noise(x.shape, 3, 8.0)
        self.assertAlmostEqual(lm.ssim(x, y), lm.ssim(y, x), places=12)

    def test_more_noise_means_lower_ssim(self):
        x = lm.luma(_gradient(64, 64))
        scores = [lm.ssim(x, np.clip(x + _noise(x.shape, 5, s), 0, 255)) for s in (2.0, 8.0, 24.0, 60.0)]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertLess(scores[-1], 0.6)
        self.assertGreater(scores[0], 0.9)

    def test_known_degradations_rank_sensibly(self):
        rs = np.random.RandomState(7)
        base = np.clip(128 + 40 * rs.standard_normal((64, 64)), 0, 255)  # textured
        inverted = 255 - base
        shifted = np.clip(base + 5, 0, 255)
        self.assertLess(lm.ssim(base, inverted), 0.1)  # structure destroyed
        self.assertGreater(lm.ssim(base, shifted), 0.98)  # a +5 DC offset barely moves structure
        self.assertLess(lm.ssim(base, inverted), lm.ssim(base, shifted))

    def test_blur_lowers_ssim_more_than_a_dc_shift(self):
        rs = np.random.RandomState(9)
        base = np.clip(128 + 40 * rs.standard_normal((64, 64)), 0, 255)
        blurred = (base + np.roll(base, 1, 0) + np.roll(base, 1, 1) + np.roll(np.roll(base, 1, 0), 1, 1)) / 4.0
        self.assertLess(lm.ssim(base, blurred), lm.ssim(base, np.clip(base + 5, 0, 255)))

    def test_bounded_and_shape_checked(self):
        x = np.random.RandomState(2).uniform(0, 255, (40, 40))
        y = np.random.RandomState(3).uniform(0, 255, (40, 40))
        self.assertLessEqual(lm.ssim(x, y), 1.0)
        self.assertGreaterEqual(lm.ssim(x, y), -1.0)
        with self.assertRaises(ValueError):
            lm.ssim(x, y[:30])
        with self.assertRaises(ValueError):
            lm.ssim(np.zeros((8, 8)), np.zeros((8, 8)))  # smaller than the 11x11 window


@unittest.skipUnless(_HAS_DEPS, "numpy/Pillow are not installed on this host")
class PairMetricTests(unittest.TestCase):
    def setUp(self):
        self.cfg, self.meta = look_config.load_config()

    def test_a_known_brightness_offset_is_measured_exactly(self):
        a = _gradient(64, 96)
        b = (a.astype(np.int16) + 5).astype(np.uint8)
        m = lm.pair_metrics(a, b, mismatch_tol=3)
        self.assertEqual(m["meanAbsDeltaPerChannel"], [5.0, 5.0, 5.0])
        self.assertEqual(m["maxAbsDeltaPerChannel"], [5, 5, 5])
        self.assertEqual(m["meanSignedDeltaPerChannel"], [5.0, 5.0, 5.0])  # b minus a: b is brighter
        self.assertEqual(m["mismatchFraction"], 1.0)  # every pixel is beyond tolerance 3
        self.assertEqual(lm.pair_metrics(a, b, mismatch_tol=5)["mismatchFraction"], 0.0)  # and none beyond 5

    def test_per_channel_deltas_are_per_channel(self):
        a = _flat(40, 40, (100, 100, 100))
        b = _flat(40, 40, (100, 110, 90))
        m = lm.pair_metrics(a, b, 3)
        self.assertEqual(m["meanAbsDeltaPerChannel"], [0.0, 10.0, 10.0])
        self.assertEqual(m["meanSignedDeltaPerChannel"], [0.0, 10.0, -10.0])

    def test_mismatch_fraction_counts_pixels_not_channels(self):
        a = _flat(100, 100, (100, 100, 100))
        b = a.copy()
        b[:10, :, :] = (160, 160, 160)  # exactly 10 % of the pixels, all three channels
        self.assertAlmostEqual(lm.pair_metrics(a, b, 3)["mismatchFraction"], 0.10, places=6)
        c = a.copy()
        c[:10, :, 0] = 160  # one channel only: still 10 % of PIXELS
        self.assertAlmostEqual(lm.pair_metrics(a, c, 3)["mismatchFraction"], 0.10, places=6)

    def test_identical_frames_pass_the_gated_scope_with_ssim_one(self):
        a = _gradient(64, 96)
        v = lm.compare_frames(a, a.copy(), self.cfg, "shader-subset")
        self.assertEqual(v["outcome"], lm.PASS)
        self.assertAlmostEqual(v["metrics"]["ssimLuma"], 1.0, places=6)
        self.assertEqual(v["metrics"]["mismatchFraction"], 0.0)
        self.assertTrue(v["gated"])

    def test_a_real_difference_fails_shader_subset_but_is_only_reported_for_full_look(self):
        a = _gradient(64, 96)
        b = (a.astype(np.int16) + 14).clip(0, 255).astype(np.uint8)
        gated = lm.compare_frames(a, b, self.cfg, "shader-subset")
        reported = lm.compare_frames(a, b, self.cfg, "full-look")
        self.assertEqual(gated["outcome"], lm.FAIL)
        self.assertEqual(reported["outcome"], lm.REPORTED)  # A3: never gated until the shader carries every stage
        self.assertFalse(reported["gated"])
        self.assertFalse(reported["thresholdsMet"])  # ...but it still says the bar would not have been met

    def test_full_look_never_returns_pass_or_fail(self):
        a = _gradient(64, 96)
        for b in (a.copy(), (a.astype(np.int16) + 30).clip(0, 255).astype(np.uint8)):
            self.assertEqual(lm.compare_frames(a, b, self.cfg, "full-look")["outcome"], lm.REPORTED)

    def test_geometry_mismatch_is_a_typed_terminal_with_no_numbers(self):
        v = lm.compare_frames(_gradient(64, 96), _gradient(60, 96), self.cfg, "shader-subset")
        self.assertEqual(v["outcome"], lm.GEOMETRY_MISMATCH)
        self.assertNotIn("metrics", v)

    def test_geometry_tolerance_centre_crops_and_says_so(self):
        cfg = look_config.load_config()[0]
        cfg["pair"]["geometry_tolerance_px"] = 2
        a = _gradient(64, 96)
        v = lm.compare_frames(a, a[1:63], cfg, "shader-subset")  # 62 rows vs 64
        self.assertEqual(v["geometry"]["centreCroppedTo"], [96, 62])
        self.assertEqual(v["outcome"], lm.PASS)  # a[1:63] is exactly the centre of a

    def test_declared_or_symmetric_letterbox_bars_do_not_break_pairing_but_undeclared_ones_do(self):
        content = _gradient(60, 100)
        barred = np.zeros((100, 100, 3), dtype=np.uint8)
        barred[20:80] = content
        undeclared = lm.compare_frames(content, barred, self.cfg, "shader-subset")
        self.assertEqual(undeclared["outcome"], lm.GEOMETRY_MISMATCH)  # nothing was hidden to make it match
        declared = lm.compare_frames(
            content, barred, self.cfg, "shader-subset",
            letterbox_policy_b=lm.make_letterbox_policy(self.cfg, declared={"top": 20, "bottom": 20}))
        self.assertEqual(declared["geometry"]["bActive"], [100, 60])
        self.assertEqual(declared["geometry"]["bLetterbox"]["provenance"], "DECLARED")
        self.assertEqual(declared["outcome"], lm.PASS)
        auto = lm.compare_frames(
            content, barred, self.cfg, "shader-subset",
            letterbox_policy_b=lm.make_letterbox_policy(self.cfg, mode="auto-symmetric"))
        self.assertEqual(auto["outcome"], lm.PASS)
        sheet = lm.compare_sheets({0: content}, {0: barred}, self.cfg, self.meta, "shader-subset",
                                  letterbox_policy_b=lm.make_letterbox_policy(self.cfg, mode="auto-symmetric"))
        self.assertEqual(sheet["outcome"], lm.PASS)
        self.assertEqual(sheet["letterboxPolicy"]["b"]["mode"], "auto-symmetric")

    def test_round1_repro_a_bare_geometry_tolerance_can_no_longer_turn_a_mismatch_into_a_pass(self):
        # Sol B6: pair.geometry_tolerance_px bare 16 made 64x96 vs 80x96 compare as PASS. The loader refuses it now,
        # and with the shipped 0 the same pair is a typed GEOMETRY_MISMATCH.
        ramp = _gradient(96, 80)
        self.assertEqual(lm.compare_frames(ramp[:, :64], ramp, self.cfg, "shader-subset")["outcome"], lm.GEOMETRY_MISMATCH)
        with tempfile.TemporaryDirectory() as tmp:
            import json
            with open(look_config.CONFIG_PATH, "r", encoding="utf-8") as handle:
                doc = json.load(handle)
            doc["pair"]["geometry_tolerance_px"] = 16
            path = os.path.join(tmp, "cfg.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(doc, handle)
            with self.assertRaises(look_config.ConfigError):
                look_config.load_config(path)
            doc["pair"]["geometry_tolerance_px"] = {"value": 0, "reason": "r"}
            doc["pair"]["mismatch_channel_tolerance"] = 3
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(doc, handle)
            with self.assertRaises(look_config.ConfigError):
                look_config.load_config(path)

    def test_unknown_scope_is_refused(self):
        with self.assertRaises(ValueError):
            lm.compare_frames(_gradient(), _gradient(), self.cfg, "everything")

    def test_sheet_pairing_unpaired_index_makes_it_incomplete(self):
        a = {0: _gradient(), 1: _gradient()}
        b = {0: _gradient()}
        sheet = lm.compare_sheets(a, b, self.cfg, self.meta, "shader-subset")
        self.assertEqual(sheet["outcome"], lm.INCOMPLETE)
        self.assertEqual(sheet["unpairedIndices"], [1])

    def test_sheet_with_a_geometry_mismatch_is_incomplete_never_pass(self):
        sheet = lm.compare_sheets({0: _gradient(64, 96)}, {0: _gradient(50, 96)}, self.cfg, self.meta, "shader-subset")
        self.assertEqual(sheet["outcome"], lm.INCOMPLETE)

    def test_sheet_outcomes_by_scope(self):
        a = {i: _gradient() for i in range(3)}
        same = {i: _gradient() for i in range(3)}
        off = {i: (_gradient().astype(np.int16) + 20).clip(0, 255).astype(np.uint8) for i in range(3)}
        self.assertEqual(lm.compare_sheets(a, same, self.cfg, self.meta, "shader-subset")["outcome"], lm.PASS)
        self.assertEqual(lm.compare_sheets(a, off, self.cfg, self.meta, "shader-subset")["outcome"], lm.FAIL)
        self.assertEqual(lm.compare_sheets(a, off, self.cfg, self.meta, "full-look")["outcome"], lm.REPORTED)

    def test_overrides_are_written_into_the_verdict(self):
        sheet = lm.compare_sheets({0: _gradient()}, {0: _gradient()}, self.cfg, self.meta, "shader-subset",
                                  overrides={"pair.geometry_tolerance_px": {"from": 0, "to": 2, "reason": "r"}})
        self.assertEqual(sheet["config"]["overrides"]["pair.geometry_tolerance_px"]["to"], 2)
        self.assertEqual(sheet["config"]["configSha256"], self.meta["configSha256"])


@unittest.skipUnless(_HAS_DEPS, "numpy/Pillow are not installed on this host")
class FloorCliTests(unittest.TestCase):
    """The CLI makes the caller say what may be hidden: undeclared bars FAIL, declared ones are excluded and recorded."""

    def _run(self, argv):
        import look_cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            return look_cli.main(argv), out.getvalue()

    def _dir(self, tmp, name, arr):
        directory = os.path.join(tmp, name)
        os.makedirs(directory)
        Image.fromarray(arr, "RGB").save(os.path.join(directory, f"{name}-frame-00.png"))
        return directory

    def test_floor_cli_undeclared_bars_fail_declared_bars_pass_and_both_are_on_record(self):
        barred = _gradient(100, 100)
        barred[:20] = 0
        barred[-20:] = 0
        with tempfile.TemporaryDirectory() as tmp:
            frames = self._dir(tmp, "cuda", barred)
            out = os.path.join(tmp, "v.json")
            code, _ = self._run(["floor", "--frames-dir", frames, "--label", "x", "--out", out])
            self.assertEqual(code, 1)  # FAIL: crushed shadows, loudly
            with open(out, "r", encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["framesWithDarkBandsMeasuredAsScene"], [0])
            code, _ = self._run(["floor", "--frames-dir", frames, "--label", "x", "--out", out,
                                 "--letterbox-bars", "top=20,bottom=20"])
            self.assertEqual(code, 0)
            with open(out, "r", encoding="utf-8") as handle:
                verdict = json.load(handle)
            self.assertEqual(verdict["frames"][0]["geometry"]["letterbox"]["provenance"], "DECLARED")
            self.assertEqual(verdict["letterboxPolicy"]["declared"], {"top": 20, "bottom": 20, "left": 0, "right": 0})
            code, _ = self._run(["floor", "--frames-dir", frames, "--label", "x", "--out", out,
                                 "--letterbox", "auto-symmetric"])
            self.assertEqual(code, 0)

    def test_floor_cli_a_one_sided_band_fails_whatever_the_flags(self):
        arr = _gradient(100, 100)
        arr[:40] = 0
        with tempfile.TemporaryDirectory() as tmp:
            frames = self._dir(tmp, "cuda", arr)
            out = os.path.join(tmp, "v.json")
            self.assertEqual(self._run(["floor", "--frames-dir", frames, "--label", "x", "--out", out])[0], 1)
            self.assertEqual(self._run(["floor", "--frames-dir", frames, "--label", "x", "--out", out,
                                        "--letterbox", "auto-symmetric"])[0], 1)
            code, _ = self._run(["floor", "--frames-dir", frames, "--label", "x", "--out", out,
                                 "--letterbox-bars", "top=40"])
            self.assertEqual(code, 0)  # the caller may declare it: that is an explicit, recorded choice

    def test_floor_cli_skin_lost_against_a_baseline_exits_one(self):
        base = _flat(100, 100, (90, 110, 160))
        base[30:60, 30:60] = (224, 172, 140)
        moved = base.copy()
        moved[30:60, 30:60] = (140, 172, 224)
        with tempfile.TemporaryDirectory() as tmp:
            frames, baseline = self._dir(tmp, "sub", moved), self._dir(tmp, "base", base)
            out = os.path.join(tmp, "v.json")
            code, _ = self._run(["floor", "--frames-dir", frames, "--baseline-dir", baseline, "--label", "x",
                                 "--out", out])
            self.assertEqual(code, 1)

    def test_pair_metrics_cli_per_side_declaration(self):
        content = _gradient(60, 100)
        barred = np.zeros((100, 100, 3), dtype=np.uint8)
        barred[20:80] = content
        with tempfile.TemporaryDirectory() as tmp:
            a, b = self._dir(tmp, "cpu", content), self._dir(tmp, "cuda", barred)
            out = os.path.join(tmp, "p.json")
            base = ["pair-metrics", "--a-dir", a, "--b-dir", b, "--scope", "shader-subset", "--out", out]
            self.assertEqual(self._run(base)[0], 2)  # GEOMETRY_MISMATCH -> INCOMPLETE
            self.assertEqual(self._run(base + ["--b-letterbox-bars", "top=20,bottom=20"])[0], 0)


def _skin_scene(rgb):
    arr = _flat(100, 100, (90, 110, 160))
    arr[30:60, 30:60] = rgb  # 9 % skin-tone region
    return arr


class _InputHelpers(unittest.TestCase):
    """Shared fixtures for the requested-input tests (no tests of its own)."""

    def setUp(self):
        self.cfg, self.meta = look_config.load_config()
        self.original = _skin_scene((224, 172, 140))
        self.moved = _skin_scene((140, 172, 224))  # skin pushed out of the colour box

    def _run(self, argv):
        import look_cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            return look_cli.main(argv), out.getvalue()

    def _dir(self, tmp, name, files):
        directory = os.path.join(tmp, name)
        os.makedirs(directory)
        for filename, arr in files.items():
            Image.fromarray(arr, "RGB").save(os.path.join(directory, filename))
        return directory

    def _floor(self, tmp, subject, baseline):
        out = os.path.join(tmp, "v.json")
        argv = ["floor", "--frames-dir", subject, "--label", "x", "--out", out]
        if baseline is not None:
            argv += ["--baseline-dir", baseline]
        code, stdout = self._run(argv)
        verdict = None
        if os.path.isfile(out):
            with open(out, "r", encoding="utf-8") as handle:
                verdict = json.load(handle)
        return code, verdict, stdout

@unittest.skipUnless(_HAS_DEPS, "numpy/Pillow are not installed on this host")
class RequestedBaselineTests(_InputHelpers):
    """LOOK-METRICS-JUDGE-2 item 1 (sol r2 blocker): a baseline that was REQUESTED but has no frame for a subject
    index never reads as agreement. Floor, sheet and CLI all say INCOMPLETE with a typed reason."""

    # -- sol r2's exact repro ----------------------------------------------------------------------------
    def test_sol_r2_repro_baseline_dir_without_the_subjects_frame_index_is_INCOMPLETE_not_PASS(self):
        with tempfile.TemporaryDirectory() as tmp:
            subject = self._dir(tmp, "sub", {"frame-01.png": self.moved})
            wrong = self._dir(tmp, "base-wrong", {"frame-00.png": self.original})
            right = self._dir(tmp, "base-right", {"frame-01.png": self.original})
            code, verdict, _ = self._floor(tmp, subject, wrong)
            self.assertEqual(code, 2)
            self.assertEqual(verdict["outcome"], lm.INCOMPLETE)
            reasons = {r["code"]: r for r in verdict["incompleteReasons"]}
            self.assertEqual(reasons["BASELINE_MISSING_FRAMES"]["indices"], [1])
            self.assertEqual(reasons["BASELINE_FRAMES_WITHOUT_SUBJECT"]["indices"], [0])
            check = verdict["frames"][0]["checks"]["skin_hue_drift"]
            self.assertEqual((check["outcome"], check["reason"]),
                             (lm.NOT_EVALUABLE, "BASELINE_REQUESTED_BUT_NO_FRAME_FOR_INDEX"))
            self.assertTrue(verdict["baseline"]["requested"])
            code, verdict, _ = self._floor(tmp, subject, right)
            self.assertEqual((code, verdict["outcome"]), (1, lm.FAIL))  # the matching baseline catches it

    def test_every_subject_frame_must_have_its_baseline_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            subject = self._dir(tmp, "sub", {"f-00.png": self.original, "f-01.png": self.original})
            partial = self._dir(tmp, "base", {"f-00.png": self.original})
            code, verdict, _ = self._floor(tmp, subject, partial)
            self.assertEqual((code, verdict["outcome"]), (2, lm.INCOMPLETE))
            self.assertEqual(verdict["counts"][lm.PASS], 1)  # the matched frame is still reported as measured
            full = self._dir(tmp, "base-full", {"f-00.png": self.original, "f-01.png": self.original})
            code, verdict, _ = self._floor(tmp, subject, full)
            self.assertEqual((code, verdict["outcome"]), (0, lm.PASS))
            self.assertEqual(verdict["incompleteReasons"], [])

    def test_baseline_frames_that_have_no_subject_frame_are_reported_and_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            subject = self._dir(tmp, "sub", {"f-00.png": self.original})
            baseline = self._dir(tmp, "base", {"f-00.png": self.original, "f-07.png": self.original})
            code, verdict, _ = self._floor(tmp, subject, baseline)
            self.assertEqual((code, verdict["outcome"]), (2, lm.INCOMPLETE))
            self.assertEqual(verdict["baseline"]["baselineIndicesWithoutSubjectFrame"], [7])

    def test_evaluate_frame_with_a_requested_but_absent_baseline_is_not_evaluable(self):
        v = lm.evaluate_frame(self.moved, self.cfg, baseline_requested=True)
        self.assertEqual(v["outcome"], lm.NOT_EVALUABLE)
        self.assertEqual(v["checks"]["skin_hue_drift"]["reason"], "BASELINE_REQUESTED_BUT_NO_FRAME_FOR_INDEX")
        self.assertIn("luma_p50", v["metrics"])  # the other measurements are not thrown away
        plain = lm.evaluate_frame(self.moved, self.cfg)  # nothing requested: the old, disclosed N/A
        self.assertEqual(plain["checks"]["skin_hue_drift"]["reason"], "NO_BASELINE_FRAME")
        self.assertEqual(plain["outcome"], lm.PASS)

    def test_a_requested_baseline_with_no_frames_at_all_is_incomplete_through_evaluate_sheet(self):
        sheet = lm.evaluate_sheet({0: self.moved}, self.cfg, self.meta, references={}, baseline_requested=True)
        self.assertEqual(sheet["outcome"], lm.INCOMPLETE)
        self.assertEqual(sheet["baseline"]["framesMissingBaseline"], [0])

    def test_no_baseline_requested_is_recorded_as_such_and_never_as_applied(self):
        sheet = lm.evaluate_sheet({0: self.moved}, self.cfg, self.meta)
        self.assertEqual(sheet["outcome"], lm.PASS)
        self.assertFalse(sheet["baseline"]["requested"])
        self.assertEqual(sheet["skinCheck"]["status"], "NOT_REQUESTED")

    def test_a_requested_baseline_with_no_skin_region_is_recorded_not_applicable_and_not_silent(self):
        plain = _flat(100, 100, (90, 110, 160))
        with tempfile.TemporaryDirectory() as tmp:
            subject = self._dir(tmp, "sub", {"f-00.png": plain})
            baseline = self._dir(tmp, "base", {"f-00.png": plain})
            code, verdict, stdout = self._floor(tmp, subject, baseline)
            self.assertEqual((code, verdict["outcome"]), (0, lm.PASS))
            skin = verdict["skinCheck"]
            self.assertEqual(skin["status"], "REQUESTED_NOT_APPLICABLE")
            self.assertEqual(skin["appliedToFrames"], 0)
            self.assertEqual(skin["notApplicableFrames"], {"0": "NO_SKIN_TONE_REGION_IN_BASELINE"})
            self.assertTrue(verdict["frames"][0]["checks"]["skin_hue_drift"]["baselineRequested"])
            self.assertIn("REQUESTED_NOT_APPLICABLE", stdout)  # the console line says it too

    def test_a_requested_baseline_that_is_applied_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            subject = self._dir(tmp, "sub", {"f-00.png": self.original})
            baseline = self._dir(tmp, "base", {"f-00.png": self.original})
            _, verdict, _ = self._floor(tmp, subject, baseline)
            self.assertEqual(verdict["skinCheck"]["status"], "APPLIED")


@unittest.skipUnless(_HAS_DEPS, "numpy/Pillow are not installed on this host")
class FrameIndexTests(_InputHelpers):
    """LOOK-METRICS-JUDGE-2 item 2: index_frames never silently skips a file in a supplied directory, and a
    supplied directory that yields no frame is an error."""

    def test_mis_named_png_without_digits_is_reported_and_blocks_the_floor(self):
        with tempfile.TemporaryDirectory() as tmp:
            subject = self._dir(tmp, "sub", {"cuda-00.png": self.original, "cuda-final.png": self.moved})
            index = lm.index_frames(subject)
            self.assertEqual(sorted(index), [0])
            self.assertEqual([(s["name"], s["reason"], s["blocking"]) for s in index.skipped],
                             [("cuda-final.png", "NO_DIGITS_IN_NAME", True)])
            code, verdict, _ = self._floor(tmp, subject, None)
            self.assertEqual((code, verdict["outcome"]), (2, lm.INCOMPLETE))
            self.assertEqual([r["code"] for r in verdict["incompleteReasons"]], ["FRAME_FILES_NOT_INDEXED"])
            self.assertEqual(verdict["inputs"]["frames"]["skipped"][0]["name"], "cuda-final.png")

    def test_a_non_png_image_is_reported_and_blocks_but_a_sidecar_is_only_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            subject = self._dir(tmp, "sub", {"f-00.png": self.original, "f-01.jpg": self.original})
            with open(os.path.join(subject, "notes.txt"), "w") as handle:
                handle.write("x")
            os.makedirs(os.path.join(subject, "old"))
            index = lm.index_frames(subject)
            by_name = {s["name"]: s for s in index.skipped}
            self.assertEqual((by_name["f-01.jpg"]["reason"], by_name["f-01.jpg"]["blocking"]), ("NOT_A_PNG", True))
            self.assertEqual((by_name["notes.txt"]["reason"], by_name["notes.txt"]["blocking"]), ("NOT_A_FRAME_FILE", False))
            self.assertEqual((by_name["old"]["reason"], by_name["old"]["blocking"]), ("NOT_A_FILE", False))
            code, verdict, _ = self._floor(tmp, subject, None)
            self.assertEqual((code, verdict["outcome"]), (2, lm.INCOMPLETE))

    def test_sidecars_alone_do_not_block_but_are_listed_in_the_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            subject = self._dir(tmp, "sub", {"f-00.png": self.original})
            with open(os.path.join(subject, "notes.txt"), "w") as handle:
                handle.write("x")
            code, verdict, _ = self._floor(tmp, subject, None)
            self.assertEqual((code, verdict["outcome"]), (0, lm.PASS))
            self.assertEqual([s["name"] for s in verdict["inputs"]["frames"]["skipped"]], ["notes.txt"])

    def test_two_files_with_one_index_are_ambiguous_and_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            subject = self._dir(tmp, "sub", {"a-00.png": self.original, "b-00.png": self.moved})
            index = lm.index_frames(subject)
            self.assertEqual(sorted(index), [0])
            self.assertEqual([(s["name"], s["reason"].split(":")[0]) for s in index.skipped],
                             [("b-00.png", "DUPLICATE_FRAME_INDEX")])
            code, verdict, _ = self._floor(tmp, subject, None)
            self.assertEqual((code, verdict["outcome"]), (2, lm.INCOMPLETE))

    def test_a_mis_named_baseline_file_blocks_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            subject = self._dir(tmp, "sub", {"f-00.png": self.original})
            baseline = self._dir(tmp, "base", {"f-00.png": self.original, "baseline-final.png": self.original})
            code, verdict, _ = self._floor(tmp, subject, baseline)
            self.assertEqual((code, verdict["outcome"]), (2, lm.INCOMPLETE))
            self.assertIn("BASELINE_FILES_NOT_INDEXED", [r["code"] for r in verdict["incompleteReasons"]])

    def test_a_directory_that_yields_zero_frames_is_an_error_everywhere(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = self._dir(tmp, "empty", {})
            only_text = os.path.join(tmp, "text")
            os.makedirs(only_text)
            with open(os.path.join(only_text, "a.txt"), "w") as handle:
                handle.write("x")
            ok = self._dir(tmp, "ok", {"f-00.png": self.original})
            missing = os.path.join(tmp, "no-such-dir")
            for bad in (empty, only_text, missing):
                with self.subTest(bad=os.path.basename(bad)):
                    with self.assertRaises(lm.FrameIndexError):
                        lm.index_frames(bad)
                    out = os.path.join(tmp, "v.json")
                    self.assertEqual(self._run(["floor", "--frames-dir", bad, "--label", "x", "--out", out])[0], 2)
                    self.assertEqual(self._run(["floor", "--frames-dir", ok, "--baseline-dir", bad, "--label", "x",
                                                "--out", out])[0], 2)
                    self.assertEqual(self._run(["pair-metrics", "--a-dir", bad, "--b-dir", ok, "--scope",
                                                "shader-subset", "--out", out])[0], 2)
                    self.assertEqual(self._run(["pair-metrics", "--a-dir", ok, "--b-dir", bad, "--scope",
                                                "shader-subset", "--out", out])[0], 2)

    def test_a_directory_compared_with_itself_is_refused_not_measured_as_perfect_agreement(self):
        with tempfile.TemporaryDirectory() as tmp:
            same = self._dir(tmp, "same", {"f-00.png": self.moved})
            out = os.path.join(tmp, "v.json")
            self.assertEqual(self._run(["floor", "--frames-dir", same, "--baseline-dir", same, "--label", "x",
                                        "--out", out])[0], 2)
            self.assertEqual(self._run(["pair-metrics", "--a-dir", same, "--b-dir", same, "--scope", "shader-subset",
                                        "--out", out])[0], 2)
            self.assertFalse(os.path.exists(out))

    def test_pair_metrics_blocks_on_a_mis_named_file_on_either_side(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = self._dir(tmp, "a", {"f-00.png": self.original})
            b = self._dir(tmp, "b", {"f-00.png": self.original, "g-last.png": self.original})
            out = os.path.join(tmp, "p.json")
            code, _ = self._run(["pair-metrics", "--a-dir", a, "--b-dir", b, "--scope", "shader-subset", "--out", out])
            self.assertEqual(code, 2)
            with open(out, "r", encoding="utf-8") as handle:
                verdict = json.load(handle)
            self.assertEqual(verdict["outcome"], lm.INCOMPLETE)
            self.assertEqual([r["code"] for r in verdict["incompleteReasons"]], ["B_FILES_NOT_INDEXED"])
            self.assertEqual(verdict["inputs"]["b"]["skipped"][0]["name"], "g-last.png")


if __name__ == "__main__":
    unittest.main()
