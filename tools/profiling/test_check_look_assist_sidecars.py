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
        "settled": True,
    }
    base.update(overrides)
    return base


class CheckLookAssistSidecars(unittest.TestCase):
    def test_the_fixed_daylight_decision_passes(self):
        self.assertEqual([], chk.check([("frame-00.json", sidecar())]))
        for source, decision in (("rendered-neutral-patch", "accepted"), ("processed-neutral-patch", "accepted-damped")):
            self.assertEqual([], chk.check([("frame-00.json", sidecar(look_assist_wb_source=source,
                                                                      look_assist_wb_decision=decision,
                                                                      look_assist_temperature=8950,
                                                                      look_assist_tint=-35))]), source)

    def test_r2_as_shot_prior_at_the_base_balance_fails(self):
        # PR #221 r2: scene=shade, source=as-shot-prior, 6000 K / 0 (the app default): deck chroma 18.9 in the
        # real app against master's 13.5. The label must not launder the base balance.
        failures = chk.check([("frame-00.json", sidecar(look_assist_wb_source="as-shot-prior",
                                                        look_assist_wb_decision="prior",
                                                        look_assist_temperature=6000,
                                                        look_assist_tint=0))])
        self.assertTrue(any("look_assist_wb_source" in f for f in failures))
        self.assertTrue(any("look_assist_wb_decision" in f for f in failures))

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

    def test_unsettled_or_rejected_decisions_fail(self):
        # Sol r1: a rejected-unstable decision on an unsettled sidecar used to pass.
        self.assertTrue(chk.check([("f", sidecar(settled=False))]))
        self.assertTrue(chk.check([("f", sidecar())] + [("g", {k: v for k, v in sidecar().items() if k != "settled"})]))
        rejected = chk.check([("f", sidecar(look_assist_wb_decision="rejected-unstable"))])
        self.assertTrue(any("look_assist_wb_decision" in f for f in rejected))
        for decision in ("none", "candidate", "rejected-safety-fallback", None):
            self.assertTrue(chk.check([("f", sidecar(look_assist_wb_decision=decision))]), decision)
        for decision in ("accepted", "accepted-damped"):
            self.assertEqual([], chk.check([("f", sidecar(look_assist_wb_decision=decision))]), decision)
        self.assertTrue(chk.check([("f", sidecar(look_assist_wb_decision="prior"))]))

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


class DeckPictureGate(unittest.TestCase):
    def _frame(self, directory, rgb):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not installed")
        Image.new("RGB", (80, 40), rgb).save(os.path.join(directory, "frame-00.png"))
        with open(os.path.join(directory, "frame-00.json"), "w", encoding="utf-8") as handle:
            json.dump(sidecar(), handle)

    def test_neutral_deck_passes_and_lavender_deck_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            self._frame(directory, (128, 128, 128))
            self.assertEqual(0, chk.main([directory, "--deck-chroma-max", "6"]))
        with tempfile.TemporaryDirectory() as directory:
            self._frame(directory, (140, 118, 150))   # lavender: the master / r1 picture
            self.assertEqual(1, chk.main([directory, "--deck-chroma-max", "6"]))

    def test_the_picture_gate_is_off_unless_asked_for(self):
        with tempfile.TemporaryDirectory() as directory:
            self._frame(directory, (140, 118, 150))
            self.assertEqual(0, chk.main([directory]))


if __name__ == "__main__":
    unittest.main()
