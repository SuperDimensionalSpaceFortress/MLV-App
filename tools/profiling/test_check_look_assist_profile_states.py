#!/usr/bin/env python3
"""Falsifier tests for check_look_assist_profile_states.py (stdlib; synthetic traces; Pillow optional)."""
import os
import tempfile
import unittest

import check_look_assist_profile_states as chk

PREFIX = "[2026-10-02T01:56:36.037Z] [INFO] [0x2e638] interaction_trace event="


def sync_trace(source="rendered-neutral-patch", decision="accepted", temperature=8950, tint=-35, exposure=160, scene="shade"):
    return "\n".join([
        PREFIX + "look_assist.apply.auto_wb valid=1 source=%s decision=%s damping=1.000" % (source, decision),
        PREFIX + "look_assist.apply.result analysis=raw scene=%s median=37.000 preset_exp=%d preset_contrast=15 "
                 "final_temp=%d final_tint=%d thumb=180x226" % (scene, exposure, temperature, tint),
    ]) + "\n"


# The PR #221 r2 trace of the same state (evidence/after-sync-large_dual_iso.trace.txt): the base balance stands.
R2_TRACE = sync_trace(source="as-shot-prior", decision="prior", temperature=6000, tint=0)
MASTER_TRACE = sync_trace(source="none", decision="none", temperature=6480, tint=-19, exposure=174, scene="night")


def make_run(root, tag, clip, mode, trace):
    directory = os.path.join(root, "%s-%s-%s" % (tag, clip, mode))
    os.makedirs(os.path.join(directory, "logs"))
    with open(os.path.join(directory, "logs", "mlvapp.log"), "w", encoding="utf-8") as handle:
        handle.write(trace)
    return directory


class DecisionGate(unittest.TestCase):
    def test_the_refined_daylight_decision_passes_in_both_modes(self):
        with tempfile.TemporaryDirectory() as root:
            for clip in ("tiny_dual_iso", "large_dual_iso"):
                for mode in ("sync", "async"):
                    make_run(root, "t", clip, mode, sync_trace())
            self.assertEqual(0, chk.main([root, "--tag", "t"]))

    def test_r2_as_shot_prior_at_the_base_balance_fails(self):
        # PR #221 r2, sol + fable: shade / as-shot-prior / 6000 K / 0 in the profile-settle state.
        with tempfile.TemporaryDirectory() as root:
            make_run(root, "t", "large_dual_iso", "sync", R2_TRACE)
            self.assertEqual(1, chk.main([root, "--tag", "t"]))

    def test_master_night_verdict_fails(self):
        with tempfile.TemporaryDirectory() as root:
            make_run(root, "t", "large_dual_iso", "sync", MASTER_TRACE)
            self.assertEqual(1, chk.main([root, "--tag", "t"]))

    def test_sync_and_async_must_land_on_the_same_receipt(self):
        with tempfile.TemporaryDirectory() as root:
            make_run(root, "t", "large_dual_iso", "sync", sync_trace())
            make_run(root, "t", "large_dual_iso", "async", sync_trace(temperature=5840, tint=7))   # the worker's other picture
            self.assertEqual(1, chk.main([root, "--tag", "t"]))

    def test_outside_the_daylight_window_fails(self):
        with tempfile.TemporaryDirectory() as root:
            make_run(root, "t", "large_dual_iso", "sync", sync_trace(temperature=3200))
            self.assertEqual(1, chk.main([root, "--tag", "t"]))

    def test_an_unsettled_run_fails_and_no_runs_is_unusable(self):
        with tempfile.TemporaryDirectory() as root:
            make_run(root, "t", "large_dual_iso", "sync", PREFIX + "look_assist.daylight_evidence_render stops=1.60\n")
            self.assertEqual(1, chk.main([root, "--tag", "t"]))
        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(2, chk.main([empty, "--tag", "t"]))

    def test_async_receipt_is_read_from_the_async_applied_event(self):
        text = (PREFIX + "look_assist.apply.async_dispatch generation=3 frame=0 scene=shade floor_lifted=0\n"
                + PREFIX + "look_assist.apply.auto_wb_async_applied generation=3 valid=1 source=processed-neutral-patch "
                           "decision=accepted damping=1.000 awb_temp=9990 awb_tint=-35 final_temp=9990 final_tint=-35 preset_exp=160 frame=0\n")
        receipt = chk.parse_log_text(text)
        self.assertEqual(("shade", "processed-neutral-patch", "accepted", 9990, -35, 160),
                         tuple(receipt[k] for k in ("scene", "source", "decision", "temperature", "tint", "exposure")))


class PictureGate(unittest.TestCase):
    def _png(self, directory, name, rgb):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not installed")
        os.makedirs(os.path.join(directory, "pic"), exist_ok=True)
        Image.new("RGB", (80, 40), rgb).save(os.path.join(directory, "pic", name))

    def test_neutral_deck_passes_and_a_cast_deck_or_one_worse_than_master_fails(self):
        with tempfile.TemporaryDirectory() as root:
            directory = make_run(root, "t", "large_dual_iso", "sync", sync_trace())
            self._png(directory, "look-assist-final-frame0.png", (132, 132, 134))
            self._png(directory, "state-master.png", (140, 146, 170))
            self.assertEqual(0, chk.main([root, "--tag", "t", "--deck-chroma-max", "6"]))
        with tempfile.TemporaryDirectory() as root:
            directory = make_run(root, "t", "large_dual_iso", "sync", sync_trace())
            self._png(directory, "look-assist-final-frame0.png", (124, 132, 165))   # the r2 picture
            self.assertEqual(1, chk.main([root, "--tag", "t", "--deck-chroma-max", "6"]))
        with tempfile.TemporaryDirectory() as root:
            directory = make_run(root, "t", "large_dual_iso", "sync", sync_trace())
            self._png(directory, "look-assist-final-frame0.png", (132, 132, 140))   # under 6? no: still cast
            self._png(directory, "state-master.png", (132, 132, 134))               # master's look is better here
            self.assertEqual(1, chk.main([root, "--tag", "t", "--deck-chroma-max", "9"]))


if __name__ == "__main__":
    unittest.main()
