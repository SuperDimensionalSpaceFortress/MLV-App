#!/usr/bin/env python3
"""Falsifier tests for check_look_assist_profile_states.py and its helpers (stdlib only: synthetic traces and
synthetic PNGs written with zlib), plus the recorded app run the hosted CI replays."""
import json
import os
import struct
import tempfile
import unittest
import zlib

import check_look_assist_profile_states as chk
import look_assist_png
import verify_exe_stamp

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
RECORDED = os.path.join(REPO, "tests", "fixtures", "look_assist_profile_runs", "pr222-r2")

PREFIX = "[2026-10-02T01:56:36.037Z] [INFO] [0x2e638] interaction_trace event="
SHA = "a" * 40


def sync_trace(source="rendered-neutral-patch", decision="accepted", temperature=8950, tint=-35, exposure=160, scene="shade"):
    return "\n".join([
        PREFIX + "look_assist.apply.auto_wb valid=1 source=%s decision=%s damping=1.000" % (source, decision),
        PREFIX + "look_assist.apply.result analysis=raw scene=%s median=37.000 preset_exp=%d preset_contrast=15 "
                 "final_temp=%d final_tint=%d thumb=180x226" % (scene, exposure, temperature, tint),
    ]) + "\n"


# The PR #221 r2 trace of the same state: the base balance stands.
R2_TRACE = sync_trace(source="as-shot-prior", decision="prior", temperature=6000, tint=0)
# PR #222 r1's no-surface state: shade, master's balance on the daylight preset (6000 K / 0 in the real app).
HALF_STATE_TRACE = sync_trace(source="master-balance", decision="legacy", temperature=6000, tint=0)
# Master's own analysis, what the fallback produces (and what master logged for the same state).
MASTER_TRACE = sync_trace(source="none", decision="none", temperature=6480, tint=-19, exposure=174, scene="night")


def build_sha_line(sha=SHA):
    return '[2026-10-02T01:56:36.000Z] [INFO] run_metadata {"build_sha":"%s","schema":"x"}\n' % sha


def make_run(root, tag, clip, mode, trace, sha=SHA):
    directory = os.path.join(root, "%s-%s-%s" % (tag, clip, mode))
    os.makedirs(os.path.join(directory, "logs"))
    with open(os.path.join(directory, "logs", "mlvapp.log"), "w", encoding="utf-8") as handle:
        handle.write((build_sha_line(sha) if sha else "") + trace)
    return directory


def write_png(path, width, height, rgb, filter_type=0):
    """A solid-colour 8-bit RGB PNG (zlib only). filter_type exercises the decoder's unfilter paths."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    row = bytes(rgb) * width
    raw = bytearray()
    previous = bytes(len(row))
    for _ in range(height):
        raw.append(filter_type)
        if filter_type == 0:
            raw += row
        elif filter_type == 1:    # Sub
            raw += bytes((row[i] - (row[i - 3] if i >= 3 else 0)) & 255 for i in range(len(row)))
        elif filter_type == 2:    # Up
            raw += bytes((row[i] - previous[i]) & 255 for i in range(len(row)))
        elif filter_type == 3:    # Average
            raw += bytes((row[i] - (((row[i - 3] if i >= 3 else 0) + previous[i]) >> 1)) & 255 for i in range(len(row)))
        elif filter_type == 4:    # Paeth
            def paeth(a, b, c):
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                return a if pa <= pb and pa <= pc else (b if pb <= pc else c)
            raw += bytes((row[i] - paeth(row[i - 3] if i >= 3 else 0, previous[i], previous[i - 3] if i >= 3 else 0)) & 255
                         for i in range(len(row)))
        previous = row

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    with open(path, "wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b""))


def pictures(directory, applied_rgb, master_rgb):
    write_png(os.path.join(directory, chk.APPLIED_PICTURE), 80, 40, applied_rgb)
    write_png(os.path.join(directory, chk.MASTER_PICTURE), 80, 40, master_rgb)


def make_full_set(root, trace=None, sha=SHA, applied=(132, 132, 134), master=(140, 146, 170)):
    for clip in chk.DEFAULT_CLIPS:
        for mode in chk.MODES:
            directory = make_run(root, "t", clip, mode, trace or sync_trace(), sha)
            pictures(directory, applied, master)


class DecisionGate(unittest.TestCase):
    def test_the_refined_daylight_decision_passes_in_both_modes(self):
        with tempfile.TemporaryDirectory() as root:
            make_full_set(root)
            self.assertEqual(0, chk.main([root, "--tag", "t"]))

    def test_masters_own_analysis_passes_the_safety_gate_but_not_the_improvement_gate(self):
        with tempfile.TemporaryDirectory() as root:
            make_full_set(root, MASTER_TRACE, applied=(140, 146, 170), master=(140, 146, 170))
            self.assertEqual(0, chk.main([root, "--tag", "t"]))
            self.assertEqual(1, chk.main([root, "--tag", "t", "--require-daylight"]))

    def test_r2_as_shot_prior_at_the_base_balance_fails(self):
        # PR #221 r2, sol + fable: shade / as-shot-prior / 6000 K / 0 in the profile-settle state.
        with tempfile.TemporaryDirectory() as root:
            make_full_set(root, R2_TRACE)
            self.assertEqual(1, chk.main([root, "--tag", "t", "--decisions-only"]))

    def test_the_shade_scene_with_masters_balance_fails(self):
        # PR #222 r1's no-surface state: it measured worse than master's look, so no label may launder it.
        with tempfile.TemporaryDirectory() as root:
            make_full_set(root, HALF_STATE_TRACE)
            self.assertEqual(1, chk.main([root, "--tag", "t", "--decisions-only"]))

    def test_sync_and_async_must_land_on_the_same_receipt(self):
        with tempfile.TemporaryDirectory() as root:
            make_full_set(root)
            for clip in chk.DEFAULT_CLIPS:
                path = os.path.join(root, "t-%s-async" % clip, "logs", "mlvapp.log")
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(build_sha_line() + sync_trace(temperature=5840, tint=7))   # the worker's other picture
            self.assertEqual(1, chk.main([root, "--tag", "t"]))

    def test_outside_the_daylight_window_fails(self):
        with tempfile.TemporaryDirectory() as root:
            make_full_set(root, sync_trace(temperature=3200))
            self.assertEqual(1, chk.main([root, "--tag", "t", "--decisions-only"]))

    def test_an_unsettled_run_fails_and_no_runs_is_unusable(self):
        with tempfile.TemporaryDirectory() as root:
            make_full_set(root, PREFIX + "look_assist.daylight_evidence_render stops=1.60\n")
            self.assertEqual(1, chk.main([root, "--tag", "t", "--decisions-only"]))
        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(2, chk.main([empty, "--tag", "t"]))

    def test_a_missing_sync_or_async_partner_fails(self):
        # sol: a mocked directory view holding only one arm used to exit 0.
        with tempfile.TemporaryDirectory() as root:
            directory = make_run(root, "t", "large_dual_iso", "async", sync_trace())
            pictures(directory, (132, 132, 134), (140, 146, 170))
            self.assertEqual(1, chk.main([root, "--tag", "t", "--clips", "large_dual_iso"]))
        with tempfile.TemporaryDirectory() as root:
            make_full_set(root)
            os.rename(os.path.join(root, "t-tiny_dual_iso-async"), os.path.join(root, "x-tiny_dual_iso-async"))
            self.assertEqual(1, chk.main([root, "--tag", "t"]))

    def test_missing_pictures_fail_unless_decisions_only(self):
        with tempfile.TemporaryDirectory() as root:
            for clip in chk.DEFAULT_CLIPS:
                for mode in chk.MODES:
                    make_run(root, "t", clip, mode, sync_trace())
            self.assertEqual(1, chk.main([root, "--tag", "t"]))
            self.assertEqual(0, chk.main([root, "--tag", "t", "--decisions-only"]))
        with tempfile.TemporaryDirectory() as root:
            make_full_set(root)
            os.remove(os.path.join(root, "t-large_dual_iso-sync", chk.MASTER_PICTURE))   # master's look is half the gate
            self.assertEqual(1, chk.main([root, "--tag", "t"]))

    def test_an_accepted_receipt_is_not_kept_by_a_later_failed_application(self):
        # sol: an accepted sync trace followed by async_dispatch and safety_fallback retained the earlier receipt.
        accepted = sync_trace()
        dispatch = PREFIX + "look_assist.apply.async_dispatch generation=3 frame=0 scene=shade floor_lifted=0\n"
        fallback = PREFIX + "look_assist.apply.safety_fallback reason=x\n"
        self.assertTrue(chk.check_receipt("a", chk.parse_log_text(accepted)) == [])
        self.assertTrue(chk.check_receipt("a", chk.parse_log_text(accepted + dispatch + fallback)))
        self.assertTrue(chk.check_receipt("a", chk.parse_log_text(accepted + dispatch)))     # dispatched, never applied
        self.assertTrue(chk.check_receipt("a", chk.parse_log_text(accepted + PREFIX + "look_assist.apply.skip reason=empty_stats\n")))
        # a fall-back to master discards pass 1 and takes the pass that follows
        text = (sync_trace(source="master-balance", decision="legacy", temperature=6000, tint=0)
                + PREFIX + "look_assist.daylight_fallback_to_master reason=no_verified_surface refused_at_base=1 frame=0\n"
                + MASTER_TRACE)
        receipt = chk.parse_log_text(text)
        self.assertEqual(("night", 6480, -19, 174), tuple(receipt[k] for k in ("scene", "temperature", "tint", "exposure")))
        self.assertEqual([], chk.check_receipt("a", receipt))

    def test_a_reasonless_skip_event_invalidates_the_arm(self):
        # sol, PR #222 r2: the app's own env-disabled / file / receipt / enabled skip carries NO reason=
        # (MainWindow "look_assist.apply.skip" with file_loaded= mlv= receipt= enabled=); the checker used to
        # keep the earlier accepted receipt (and its picture) standing after it.
        reasonless = PREFIX + "look_assist.apply.skip file_loaded=1 mlv=1 receipt=1 enabled=1\n"
        env_disabled = PREFIX + "look_assist.apply.skip file_loaded=1 mlv=1 receipt=1 enabled=0\n"
        accepted = sync_trace()
        self.assertEqual([], chk.check_receipt("a", chk.parse_log_text(accepted)))
        for skip in (reasonless, env_disabled):
            self.assertTrue(chk.check_receipt("a", chk.parse_log_text(accepted + skip)))       # after the accepted application
            self.assertTrue(chk.check_receipt("a", chk.parse_log_text(skip)))                  # instead of one
            self.assertTrue(chk.check_receipt("a", chk.parse_log_text(skip + accepted)))       # an arm that ever skipped
            # master's own analysis is not exempt either
            self.assertTrue(chk.check_receipt("a", chk.parse_log_text(MASTER_TRACE + skip)))
        # a reason-bearing skip still fails, and still names its reason
        failures = chk.check_receipt("a", chk.parse_log_text(accepted + PREFIX + "look_assist.apply.skip reason=empty_stats\n"))
        self.assertTrue(failures and "empty_stats" in failures[0])
        # a reason-less one is named as a skip too
        failures = chk.check_receipt("a", chk.parse_log_text(accepted + reasonless))
        self.assertTrue(failures and "skipped" in failures[0])

    def test_the_full_checker_exits_non_zero_for_a_stale_arm_after_a_reasonless_skip(self):
        # sol's repro, through the whole checker: append the real event grammar to a recorded arm's log (its
        # pictures and subject binding untouched). It used to report PASS; it must fail that arm.
        reasonless = PREFIX + "look_assist.apply.skip file_loaded=1 mlv=1 receipt=1 enabled=1\n"
        with tempfile.TemporaryDirectory() as root:
            make_full_set(root)
            self.assertEqual(0, chk.main([root, "--tag", "t", "--subject-sha", SHA]))
            path = os.path.join(root, "t-large_dual_iso-sync", "logs", "mlvapp.log")
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(reasonless)
            self.assertEqual(1, chk.main([root, "--tag", "t", "--subject-sha", SHA]))
        with tempfile.TemporaryDirectory() as root:
            make_full_set(root, MASTER_TRACE, applied=(140, 146, 170), master=(140, 146, 170))
            path = os.path.join(root, "t-tiny_dual_iso-async", "logs", "mlvapp.log")
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(reasonless)
            self.assertEqual(1, chk.main([root, "--tag", "t"]))

    def test_a_skip_invalidates_the_arm_whatever_rebuilds_the_receipt_after_it(self):
        # sol, PR #224 r1: a master fallback and an async dispatch each REPLACE the receipt dictionary, which used to
        # take the skipped marker with them, so skip -> fallback -> master result passed. The marker is arm-level.
        reasonless = PREFIX + "look_assist.apply.skip file_loaded=1 mlv=1 receipt=1 enabled=1\n"
        reasoned = PREFIX + "look_assist.apply.skip reason=clip_lifecycle_mutating\n"
        fallback = PREFIX + "look_assist.daylight_fallback_to_master reason=no_verified_surface refused_at_base=1 frame=0\n"
        dispatch = PREFIX + "look_assist.apply.async_dispatch generation=3 frame=0 scene=shade floor_lifted=0\n"
        async_applied = (PREFIX + "look_assist.apply.auto_wb_async_applied generation=3 valid=1 source=processed-neutral-patch "
                                  "decision=accepted damping=1.000 awb_temp=9990 awb_tint=-35 final_temp=9990 final_tint=-35 "
                                  "preset_exp=160 frame=0\n")
        for skip in (reasonless, reasoned):
            for text in (skip + fallback + MASTER_TRACE,                      # skip -> master fallback -> master result
                         skip + dispatch + async_applied,                     # skip -> async dispatch -> async applied
                         skip + sync_trace() + fallback + MASTER_TRACE,       # skip first, then a whole second application
                         sync_trace() + skip + fallback + MASTER_TRACE,       # skip in the middle
                         fallback + MASTER_TRACE + skip,                      # skip after the result
                         skip + MASTER_TRACE + dispatch + async_applied):     # every reset after it
                receipt = chk.parse_log_text(text)
                self.assertTrue(receipt.get("skipped"), text)
                failures = chk.check_receipt("a", receipt)
                self.assertTrue(failures and "skipped" in failures[0], text)
        # the first skip is the one named, and a clean arm still passes
        named = chk.check_receipt("a", chk.parse_log_text(reasoned + reasonless + MASTER_TRACE))
        self.assertIn("clip_lifecycle_mutating", named[0])
        self.assertEqual([], chk.check_receipt("a", chk.parse_log_text(fallback + MASTER_TRACE)))
        self.assertEqual([], chk.check_receipt("a", chk.parse_log_text(dispatch + async_applied)))

    def test_the_full_checker_fails_every_arm_that_skipped_before_a_fallback_or_dispatch(self):
        # sol's repro, through the whole checker and the recorded arms: prepend the skip to ONE arm's log, pictures and
        # subject binding untouched; the recorded arms hold a master fallback, which used to clear it (exit 0).
        import shutil
        manifest_sha = None
        with open(os.path.join(RECORDED, "MANIFEST.json"), "r", encoding="utf-8") as handle:
            manifest_sha = json.load(handle)["subject_sha"]
        for skip in ("look_assist.apply.skip file_loaded=1 mlv=1 receipt=1 enabled=1\n",
                     "look_assist.apply.skip reason=clip_lifecycle_mutating\n"):
            for clip in chk.DEFAULT_CLIPS:
                for mode in chk.MODES:
                    with tempfile.TemporaryDirectory() as root:
                        for name in os.listdir(RECORDED):
                            source = os.path.join(RECORDED, name)
                            if os.path.isdir(source):
                                shutil.copytree(source, os.path.join(root, name))
                        self.assertEqual(0, chk.main([root, "--tag", "rec", "--subject-sha", manifest_sha]))
                        logs = os.path.join(root, "rec-%s-%s" % (clip, mode), "logs")
                        path = os.path.join(logs, sorted(os.listdir(logs))[0])
                        with open(path, "r", encoding="utf-8", errors="replace", newline="") as handle:
                            original = handle.read()
                        with open(path, "w", encoding="utf-8", newline="") as handle:
                            handle.write(PREFIX + skip + original)
                        self.assertEqual(1, chk.main([root, "--tag", "rec", "--subject-sha", manifest_sha]),
                                         "%s/%s %s" % (clip, mode, skip))
        # a skip prepended to a synthetic arm that ends on a master fallback, sync and async
        with tempfile.TemporaryDirectory() as root:
            fallback = PREFIX + "look_assist.daylight_fallback_to_master reason=no_verified_surface frame=0\n"
            make_full_set(root, fallback + MASTER_TRACE, applied=(140, 146, 170), master=(140, 146, 170))
            self.assertEqual(0, chk.main([root, "--tag", "t", "--subject-sha", SHA]))
            path = os.path.join(root, "t-tiny_dual_iso-sync", "logs", "mlvapp.log")
            with open(path, "r", encoding="utf-8") as handle:
                original = handle.read()
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(PREFIX + "look_assist.apply.skip file_loaded=1 mlv=1 receipt=1 enabled=1\n" + original)
            self.assertEqual(1, chk.main([root, "--tag", "t", "--subject-sha", SHA]))

    def test_async_receipt_is_read_from_the_async_applied_event(self):
        text = (PREFIX + "look_assist.apply.async_dispatch generation=3 frame=0 scene=shade floor_lifted=0\n"
                + PREFIX + "look_assist.apply.auto_wb_async_applied generation=3 valid=1 source=processed-neutral-patch "
                           "decision=accepted damping=1.000 awb_temp=9990 awb_tint=-35 final_temp=9990 final_tint=-35 preset_exp=160 frame=0\n")
        receipt = chk.parse_log_text(text)
        self.assertEqual(("shade", "processed-neutral-patch", "accepted", 9990, -35, 160),
                         tuple(receipt[k] for k in ("scene", "source", "decision", "temperature", "tint", "exposure")))


class SubjectBinding(unittest.TestCase):
    def test_every_arm_must_report_the_subject_build(self):
        with tempfile.TemporaryDirectory() as root:
            make_full_set(root)
            self.assertEqual(0, chk.main([root, "--tag", "t", "--subject-sha", SHA]))
            self.assertEqual(1, chk.main([root, "--tag", "t", "--subject-sha", "b" * 40]))     # an older build's logs
        with tempfile.TemporaryDirectory() as root:
            make_full_set(root, sha=None)                                                       # no build_sha at all
            self.assertEqual(1, chk.main([root, "--tag", "t", "--subject-sha", SHA]))

    def test_the_executable_must_carry_the_subjects_stamp(self):
        with tempfile.TemporaryDirectory() as root:
            exe = os.path.join(root, "MLVApp.exe")
            with open(exe, "wb") as handle:
                handle.write(b"MZ..." + b"MLVAPP_BUILDSTAMP_v1|sha=" + SHA.encode() + b"|dirty=1\x00..")
            self.assertEqual(0, verify_exe_stamp.main([exe, "--sha", SHA]))
            self.assertEqual(1, verify_exe_stamp.main([exe, "--sha", "c" * 40]))
            with open(exe, "wb") as handle:
                handle.write(b"MZ..." + b"MLVAPP_BUILDSTAMP_v1|sha=unknown|dirty=0\x00")      # a build that skipped the generator
            self.assertEqual(1, verify_exe_stamp.main([exe, "--sha", SHA]))
            with open(exe, "wb") as handle:
                handle.write(b"MZ... no stamp")
            self.assertEqual(1, verify_exe_stamp.main([exe, "--sha", SHA]))
            self.assertEqual(2, verify_exe_stamp.main([os.path.join(root, "missing.exe"), "--sha", SHA]))


class PictureGate(unittest.TestCase):
    def test_neutral_deck_passes_and_a_cast_deck_or_one_worse_than_master_fails(self):
        with tempfile.TemporaryDirectory() as root:
            make_full_set(root, applied=(132, 132, 134), master=(140, 146, 170))
            self.assertEqual(0, chk.main([root, "--tag", "t", "--deck-chroma-max", "6"]))
        with tempfile.TemporaryDirectory() as root:
            make_full_set(root, applied=(124, 132, 165), master=(140, 146, 170))             # the r2 picture, still worse than master? no
            self.assertEqual(1, chk.main([root, "--tag", "t", "--deck-chroma-max", "6"]))   # but not within 6
        with tempfile.TemporaryDirectory() as root:
            make_full_set(root, applied=(132, 132, 140), master=(132, 132, 134))             # master's look is better here
            self.assertEqual(1, chk.main([root, "--tag", "t", "--deck-chroma-max", "9"]))

    def test_a_master_analysis_arm_gets_the_two_path_tolerance_and_no_more(self):
        with tempfile.TemporaryDirectory() as root:
            make_full_set(root, MASTER_TRACE, applied=(140, 146, 170), master=(140, 146, 170))
            self.assertEqual(0, chk.main([root, "--tag", "t"]))
        with tempfile.TemporaryDirectory() as root:
            make_full_set(root, MASTER_TRACE, applied=(124, 132, 165), master=(140, 146, 170))   # r2's picture under master's label
            self.assertEqual(1, chk.main([root, "--tag", "t"]))


class PngReader(unittest.TestCase):
    def test_every_png_filter_decodes_to_the_same_pixels(self):
        with tempfile.TemporaryDirectory() as root:
            reference = None
            for filter_type in range(5):
                path = os.path.join(root, "f%d.png" % filter_type)
                write_png(path, 40, 30, (10, 120, 250), filter_type)
                width, height, rows = look_assist_png.read_png_rgb(path)
                self.assertEqual((40, 30), (width, height))
                self.assertTrue(all(row == bytes((10, 120, 250)) * 40 for row in rows))
                chroma = look_assist_png.deck_cast_chroma(path)
                reference = chroma if reference is None else reference
                self.assertAlmostEqual(reference, chroma)

    def test_a_grey_deck_has_no_chroma_and_a_blue_one_has_plenty(self):
        with tempfile.TemporaryDirectory() as root:
            grey, blue = os.path.join(root, "g.png"), os.path.join(root, "b.png")
            write_png(grey, 60, 40, (128, 128, 128))
            write_png(blue, 60, 40, (124, 132, 165))
            self.assertLess(look_assist_png.deck_cast_chroma(grey), 0.5)
            self.assertGreater(look_assist_png.deck_cast_chroma(blue), 15.0)

    def test_an_unsupported_png_is_a_value_error_not_a_pass(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "x.png")
            with open(path, "wb") as handle:
                handle.write(b"not a png")
            with self.assertRaises(ValueError):
                look_assist_png.read_png_rgb(path)


class RecordedAppRun(unittest.TestCase):
    """The hosted CI's gate for the profile states: it replays the recorded real-app run (traces and downscaled
    copies of the app's own pictures; tests/fixtures/look_assist_profile_runs/pr222-r2/MANIFEST.json says which
    build produced them and with which commands) through the real checker, so a consumer-side regression that
    changes what the app does in these states cannot land with every test green while the gate sits unrun."""

    def setUp(self):
        if not os.path.isdir(RECORDED):
            self.fail("the recorded app run is missing: %s" % RECORDED)
        with open(os.path.join(RECORDED, "MANIFEST.json"), "r", encoding="utf-8") as handle:
            self.manifest = json.load(handle)

    def test_the_manifest_binds_the_run_to_one_subject_and_lists_every_command(self):
        self.assertRegex(self.manifest["subject_sha"], r"^[0-9a-f]{40}$")
        self.assertRegex(self.manifest["exe_sha256"], r"^[0-9A-Fa-f]{64}$")
        self.assertTrue(self.manifest["commands"], "no generating commands recorded")
        for key in ("profile_run", "picture_dump_env", "master_look", "downscale"):
            self.assertIn(key, self.manifest["commands"])
        self.assertIn("master:", self.manifest["commands"]["master_look"])
        pictures_listed = {p["file"] for p in self.manifest["pictures"]}
        for clip in chk.DEFAULT_CLIPS:
            for mode in chk.MODES:
                for picture in (chk.APPLIED_PICTURE, chk.MASTER_PICTURE):
                    self.assertIn("rec-%s-%s/%s" % (clip, mode, picture.replace(os.sep, "/")), pictures_listed)

    def test_the_recorded_run_passes_the_gate_bound_to_its_subject(self):
        self.assertEqual(0, chk.main([RECORDED, "--tag", "rec", "--subject-sha", self.manifest["subject_sha"]]))

    def test_the_recorded_run_fails_if_it_is_claimed_for_another_commit(self):
        self.assertEqual(1, chk.main([RECORDED, "--tag", "rec", "--subject-sha", "0" * 40]))

    def test_the_gate_refuses_the_recorded_run_with_an_arm_removed(self):
        import shutil
        with tempfile.TemporaryDirectory() as root:
            for name in os.listdir(RECORDED):
                source = os.path.join(RECORDED, name)
                if os.path.isdir(source) and name != "rec-large_dual_iso-async":
                    shutil.copytree(source, os.path.join(root, name))
            self.assertEqual(1, chk.main([root, "--tag", "rec"]))


if __name__ == "__main__":
    unittest.main()
