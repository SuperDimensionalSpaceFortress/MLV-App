#!/usr/bin/env python3
"""LOOK-METRICS-JUDGE-1: falsifier tests for the objective look floor (tools/profiling/look/look_metrics.py).

Every image here is SYNTHETIC with a KNOWN-by-construction answer (a patch that is exactly 5% white, a
brightness offset of exactly +5 codes, a skin patch rotated by a known hue). Needs numpy + Pillow, which the
hosted repo-hygiene CI image does not install (see test_playback_attr_3_cuda_contact_sheet.py for the same
guard); the pure-stdlib half of the harness is covered by test_look_judge_harness.py, which CI does run.
"""
import importlib.util
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

    def test_verdict_binds_to_the_config_digest(self):
        sheet = lm.evaluate_sheet({0: _gradient()}, self.cfg, self.meta, label="x")
        self.assertEqual(sheet["config"]["configSha256"], self.meta["configSha256"])
        self.assertEqual(sheet["schema"], lm.SCHEMA_FRAME_SHEET)


@unittest.skipUnless(_HAS_DEPS, "numpy/Pillow are not installed on this host")
class LetterboxTests(unittest.TestCase):
    def setUp(self):
        self.cfg, self.meta = look_config.load_config()

    def _barred(self):
        arr = _gradient(100, 100)
        arr[:20] = 0
        arr[-20:] = 0
        return arr

    def test_black_bars_are_found_and_excluded_from_every_percentage(self):
        v = lm.evaluate_frame(self._barred(), self.cfg)
        lb = v["geometry"]["letterbox"]
        self.assertEqual((lb["top"], lb["bottom"], lb["left"], lb["right"]), (20, 20, 0, 0))
        self.assertAlmostEqual(lb["excludedPct"], 40.0, places=3)
        self.assertEqual(v["metrics"]["crushed_shadow_pct"], 0.0)
        self.assertEqual(v["outcome"], lm.PASS)
        self.assertEqual((v["geometry"]["activeHeight"], v["geometry"]["activeWidth"]), (60, 100))

    def test_without_detection_the_same_frame_fails_crushed_shadows(self):
        cfg = look_config.load_config()[0]
        cfg["letterbox"]["detect"] = False
        v = lm.evaluate_frame(self._barred(), cfg)
        self.assertAlmostEqual(v["metrics"]["crushed_shadow_pct"], 40.0, places=3)
        self.assertEqual(v["outcome"], lm.FAIL)
        self.assertEqual(v["geometry"]["letterbox"]["refused"], "DETECTION_DISABLED")

    def test_pillarbox_is_found_too(self):
        arr = _gradient(100, 100)
        arr[:, :10] = 0
        arr[:, -10:] = 0
        lb = lm.evaluate_frame(arr, self.cfg)["geometry"]["letterbox"]
        self.assertEqual((lb["left"], lb["right"]), (10, 10))

    def test_an_all_black_frame_is_not_cropped_to_nothing(self):
        v = lm.evaluate_frame(_flat(50, 50, (0, 0, 0)), self.cfg)
        self.assertEqual(v["geometry"]["letterbox"]["refused"], "ALL_BLACK_FRAME")
        self.assertEqual(v["outcome"], lm.FAIL)  # 100 % crushed

    def test_an_implausible_exclusion_is_refused_as_a_dark_frame(self):
        arr = _gradient(100, 100)
        arr[:70] = 0  # 70 % black: more likely a dark frame than a letterbox
        v = lm.evaluate_frame(arr, self.cfg)
        self.assertEqual(v["geometry"]["letterbox"]["refused"], "EXCLUSION_IMPLAUSIBLE_PROBABLY_DARK_FRAME")
        self.assertEqual(v["geometry"]["activeHeight"], 100)
        self.assertEqual(v["outcome"], lm.FAIL)

    def test_ringing_next_to_a_bar_within_the_bar_code_still_counts_as_bar(self):
        arr = _gradient(100, 100)
        arr[:20] = 0
        arr[19] = 3  # <= bar_max_code (4)
        lb = lm.detect_letterbox(arr, self.cfg)
        self.assertEqual(lb["top"], 20)

    def test_a_real_dark_row_is_not_a_bar(self):
        arr = _gradient(100, 100)
        arr[:5] = 0
        arr[5, 40:60] = 90  # one lit pixel run: the row is content
        self.assertEqual(lm.detect_letterbox(arr, self.cfg)["top"], 5)


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

    def test_no_skin_region_is_not_applicable_never_a_pass(self):
        base = _flat(80, 80, (90, 110, 160))
        v = lm.evaluate_frame(base, self.cfg, reference=base)
        self.assertEqual(v["checks"]["skin_hue_drift"]["outcome"], lm.NOT_APPLICABLE)
        self.assertEqual(v["checks"]["skin_hue_drift"]["reason"], "NO_SKIN_TONE_REGION_IN_FRAME")

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
        self.assertEqual(sheet["skinCheck"], {"appliedToFrames": 1, "ofFrames": 2})
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

    def test_letterbox_bars_do_not_break_pairing(self):
        content = _gradient(60, 100)
        barred = np.zeros((100, 100, 3), dtype=np.uint8)
        barred[20:80] = content
        v = lm.compare_frames(content, barred, self.cfg, "shader-subset")
        self.assertEqual(v["geometry"]["bActive"], [100, 60])
        self.assertEqual(v["outcome"], lm.PASS)

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


if __name__ == "__main__":
    unittest.main()
