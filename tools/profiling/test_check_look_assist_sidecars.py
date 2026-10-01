#!/usr/bin/env python3
"""Falsifier tests for check_look_assist_sidecars.py (stdlib only; synthetic sidecars)."""
import json
import os
import tempfile
import unittest

import check_look_assist_sidecars as chk


def sidecar(**overrides):
    base = {
        "look_assist_enabled": True,
        "look_assist_scene": "shade",
        "look_assist_wb_source": "processed-neutral-patch",
        "look_assist_wb_decision": "accepted",
        "look_assist_temperature": 9990,
        "look_assist_tint": -35,
        "saved": True,
    }
    base.update(overrides)
    return base


class CheckLookAssistSidecars(unittest.TestCase):
    def test_the_fixed_daylight_decision_passes(self):
        self.assertEqual([], chk.check([("frame-00.json", sidecar())]))
        self.assertEqual([], chk.check([("frame-00.json", sidecar(look_assist_wb_source="as-shot-prior",
                                                                  look_assist_temperature=5270,
                                                                  look_assist_tint=-27))]))

    def test_r1_regression_right_scene_but_no_balance_solved_fails(self):
        # r1: scene=shade, source=none, base 6000 K / tint 0 stands, the deck goes blue.
        failures = chk.check([("frame-00.json", sidecar(look_assist_wb_source="none"))])
        self.assertEqual(1, len(failures))
        self.assertIn("look_assist_wb_source", failures[0])

    def test_night_misclassification_fails(self):
        failures = chk.check([("frame-00.json", sidecar(look_assist_scene="night"))])
        self.assertTrue(any("look_assist_scene" in f for f in failures))

    def test_balance_outside_the_daylight_bounds_fails(self):
        self.assertTrue(chk.check([("f", sidecar(look_assist_temperature=3200))]))
        self.assertTrue(chk.check([("f", sidecar(look_assist_tint=25))]))

    def test_look_assist_off_and_too_few_frames_fail(self):
        self.assertTrue(chk.check([("f", sidecar(look_assist_enabled=False))]))
        self.assertTrue(chk.check([("f", sidecar())], min_frames=2))

    def test_main_reads_sidecars_from_a_directory_and_skips_unsaved(self):
        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, "frame-00.json"), "w", encoding="utf-8") as handle:
                json.dump(sidecar(), handle)
            with open(os.path.join(directory, "frame-01.json"), "w", encoding="utf-8") as handle:
                json.dump(sidecar(look_assist_scene="night", saved=False), handle)   # unsaved: ignored
            self.assertEqual(0, chk.main([directory]))
            with open(os.path.join(directory, "frame-02.json"), "w", encoding="utf-8") as handle:
                json.dump(sidecar(look_assist_wb_source="none"), handle)
            self.assertEqual(1, chk.main([directory]))
        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(2, chk.main([empty]))


if __name__ == "__main__":
    unittest.main()
