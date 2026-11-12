#!/usr/bin/env python3
"""Falsifier tests for check_look_assist_profile_states.py and its helpers (stdlib only: synthetic traces and
synthetic PNGs written with zlib), plus the recorded app run the hosted CI replays."""
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
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


def main_legacy(argv):
    """The pre-793-I2 tests build evidence with no picture_dump line (and the recorded run predates the emitter): they opt in
    to UNBOUND_LEGACY explicitly, as the recorded-run gate does."""
    return chk.main(list(argv) + ["--allow-unbound-legacy"])


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
            self.assertEqual(0, main_legacy([root, "--tag", "t"]))

    def test_masters_own_analysis_passes_the_safety_gate_but_not_the_improvement_gate(self):
        with tempfile.TemporaryDirectory() as root:
            make_full_set(root, MASTER_TRACE, applied=(140, 146, 170), master=(140, 146, 170))
            self.assertEqual(0, main_legacy([root, "--tag", "t"]))
            self.assertEqual(1, main_legacy([root, "--tag", "t", "--require-daylight"]))

    def test_r2_as_shot_prior_at_the_base_balance_fails(self):
        # PR #221 r2, sol + fable: shade / as-shot-prior / 6000 K / 0 in the profile-settle state.
        with tempfile.TemporaryDirectory() as root:
            make_full_set(root, R2_TRACE)
            self.assertEqual(1, main_legacy([root, "--tag", "t", "--decisions-only"]))

    def test_the_shade_scene_with_masters_balance_fails(self):
        # PR #222 r1's no-surface state: it measured worse than master's look, so no label may launder it.
        with tempfile.TemporaryDirectory() as root:
            make_full_set(root, HALF_STATE_TRACE)
            self.assertEqual(1, main_legacy([root, "--tag", "t", "--decisions-only"]))

    def test_sync_and_async_must_land_on_the_same_receipt(self):
        with tempfile.TemporaryDirectory() as root:
            make_full_set(root)
            for clip in chk.DEFAULT_CLIPS:
                path = os.path.join(root, "t-%s-async" % clip, "logs", "mlvapp.log")
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(build_sha_line() + sync_trace(temperature=5840, tint=7))   # the worker's other picture
            self.assertEqual(1, main_legacy([root, "--tag", "t"]))

    def test_outside_the_daylight_window_fails(self):
        with tempfile.TemporaryDirectory() as root:
            make_full_set(root, sync_trace(temperature=3200))
            self.assertEqual(1, main_legacy([root, "--tag", "t", "--decisions-only"]))

    def test_an_unsettled_run_fails_and_no_runs_is_unusable(self):
        with tempfile.TemporaryDirectory() as root:
            make_full_set(root, PREFIX + "look_assist.daylight_evidence_render stops=1.60\n")
            self.assertEqual(1, main_legacy([root, "--tag", "t", "--decisions-only"]))
        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(2, main_legacy([empty, "--tag", "t"]))

    def test_a_missing_sync_or_async_partner_fails(self):
        # sol: a mocked directory view holding only one arm used to exit 0.
        with tempfile.TemporaryDirectory() as root:
            directory = make_run(root, "t", "large_dual_iso", "async", sync_trace())
            pictures(directory, (132, 132, 134), (140, 146, 170))
            self.assertEqual(1, main_legacy([root, "--tag", "t", "--clips", "large_dual_iso"]))
        with tempfile.TemporaryDirectory() as root:
            make_full_set(root)
            os.rename(os.path.join(root, "t-tiny_dual_iso-async"), os.path.join(root, "x-tiny_dual_iso-async"))
            self.assertEqual(1, main_legacy([root, "--tag", "t"]))

    def test_missing_pictures_fail_unless_decisions_only(self):
        with tempfile.TemporaryDirectory() as root:
            for clip in chk.DEFAULT_CLIPS:
                for mode in chk.MODES:
                    make_run(root, "t", clip, mode, sync_trace())
            self.assertEqual(1, main_legacy([root, "--tag", "t"]))
            self.assertEqual(0, main_legacy([root, "--tag", "t", "--decisions-only"]))
        with tempfile.TemporaryDirectory() as root:
            make_full_set(root)
            os.remove(os.path.join(root, "t-large_dual_iso-sync", chk.MASTER_PICTURE))   # master's look is half the gate
            self.assertEqual(1, main_legacy([root, "--tag", "t"]))

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
            self.assertEqual(0, main_legacy([root, "--tag", "t", "--subject-sha", SHA]))
            path = os.path.join(root, "t-large_dual_iso-sync", "logs", "mlvapp.log")
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(reasonless)
            self.assertEqual(1, main_legacy([root, "--tag", "t", "--subject-sha", SHA]))
        with tempfile.TemporaryDirectory() as root:
            make_full_set(root, MASTER_TRACE, applied=(140, 146, 170), master=(140, 146, 170))
            path = os.path.join(root, "t-tiny_dual_iso-async", "logs", "mlvapp.log")
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(reasonless)
            self.assertEqual(1, main_legacy([root, "--tag", "t"]))

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
                        self.assertEqual(0, main_legacy([root, "--tag", "rec", "--subject-sha", manifest_sha]))
                        logs = os.path.join(root, "rec-%s-%s" % (clip, mode), "logs")
                        path = os.path.join(logs, sorted(os.listdir(logs))[0])
                        with open(path, "r", encoding="utf-8", errors="replace", newline="") as handle:
                            original = handle.read()
                        with open(path, "w", encoding="utf-8", newline="") as handle:
                            handle.write(PREFIX + skip + original)
                        self.assertEqual(1, main_legacy([root, "--tag", "rec", "--subject-sha", manifest_sha]),
                                         "%s/%s %s" % (clip, mode, skip))
        # a skip prepended to a synthetic arm that ends on a master fallback, sync and async
        with tempfile.TemporaryDirectory() as root:
            fallback = PREFIX + "look_assist.daylight_fallback_to_master reason=no_verified_surface frame=0\n"
            make_full_set(root, fallback + MASTER_TRACE, applied=(140, 146, 170), master=(140, 146, 170))
            self.assertEqual(0, main_legacy([root, "--tag", "t", "--subject-sha", SHA]))
            path = os.path.join(root, "t-tiny_dual_iso-sync", "logs", "mlvapp.log")
            with open(path, "r", encoding="utf-8") as handle:
                original = handle.read()
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(PREFIX + "look_assist.apply.skip file_loaded=1 mlv=1 receipt=1 enabled=1\n" + original)
            self.assertEqual(1, main_legacy([root, "--tag", "t", "--subject-sha", SHA]))

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
            self.assertEqual(0, main_legacy([root, "--tag", "t", "--subject-sha", SHA]))
            self.assertEqual(1, main_legacy([root, "--tag", "t", "--subject-sha", "b" * 40]))     # an older build's logs
        with tempfile.TemporaryDirectory() as root:
            make_full_set(root, sha=None)                                                       # no build_sha at all
            self.assertEqual(1, main_legacy([root, "--tag", "t", "--subject-sha", SHA]))

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
            self.assertEqual(0, main_legacy([root, "--tag", "t", "--deck-chroma-max", "6"]))
        with tempfile.TemporaryDirectory() as root:
            make_full_set(root, applied=(124, 132, 165), master=(140, 146, 170))             # the r2 picture, still worse than master? no
            self.assertEqual(1, main_legacy([root, "--tag", "t", "--deck-chroma-max", "6"]))   # but not within 6
        with tempfile.TemporaryDirectory() as root:
            make_full_set(root, applied=(132, 132, 140), master=(132, 132, 134))             # master's look is better here
            self.assertEqual(1, main_legacy([root, "--tag", "t", "--deck-chroma-max", "9"]))

    def test_a_master_analysis_arm_gets_the_two_path_tolerance_and_no_more(self):
        with tempfile.TemporaryDirectory() as root:
            make_full_set(root, MASTER_TRACE, applied=(140, 146, 170), master=(140, 146, 170))
            self.assertEqual(0, main_legacy([root, "--tag", "t"]))
        with tempfile.TemporaryDirectory() as root:
            make_full_set(root, MASTER_TRACE, applied=(124, 132, 165), master=(140, 146, 170))   # r2's picture under master's label
            self.assertEqual(1, main_legacy([root, "--tag", "t"]))


def dump_line(sha, exposure=160, frame=0, final=None, final_sha=None, final_exposure=None, safety="keep"):
    """The look_assist.apply.picture_dump line exactly as src/batch/LookAssistAnalysis.cpp lookAssistPictureDumpFields
    formats it at cc3205de (PR #793): final=none / final_sha256=none when the final PNG was not written."""
    final_name = "look-assist-final-frame%d.png" % frame if final is None else final
    return (PREFIX + "look_assist.apply.picture_dump pre_safety=look-assist-pre-safety-frame%d.png pre_safety_sha256=%s "
                     "pre_safety_exposure=%d final=%s final_sha256=%s final_exposure=%d safety=%s frame=%d\n"
            % (frame, "c" * 64, exposure, final_name, sha if final_sha is None else final_sha,
               exposure if final_exposure is None else final_exposure, safety, frame))


def result_line(exposure=160, frame=0):
    return (PREFIX + "look_assist.apply.result analysis=raw scene=shade median=37.000 preset_exp=%d preset_contrast=15 "
                     "final_temp=8950 final_tint=-35 thumb=180x226 frame=%d\n" % (exposure, frame))


AUTO_WB_LINE = PREFIX + "look_assist.apply.auto_wb valid=1 source=rendered-neutral-patch decision=accepted damping=1.000\n"
DISPATCH_LINE = PREFIX + "look_assist.apply.async_dispatch generation=3 frame=0 scene=shade floor_lifted=0\n"
ASYNC_APPLIED_LINE = (PREFIX + "look_assist.apply.auto_wb_async_applied generation=3 valid=1 source=processed-neutral-patch "
                               "decision=accepted damping=1.000 awb_temp=9990 awb_tint=-35 final_temp=9990 final_tint=-35 "
                               "preset_exp=160 frame=0\n")


def run_checker(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = chk.main(argv)
    return code, out.getvalue()


def make_bound_set(root, trace_for, applied=(132, 132, 134), master=(140, 146, 170)):
    """Every arm's log is built AFTER its picture exists, so trace_for(sha) can bind the PNG's real bytes."""
    for clip in chk.DEFAULT_CLIPS:
        for mode in chk.MODES:
            directory = make_run(root, "t", clip, mode, "", SHA)
            pictures(directory, applied, master)
            with open(os.path.join(directory, chk.APPLIED_PICTURE), "rb") as handle:
                sha = hashlib.sha256(handle.read()).hexdigest()
            with open(os.path.join(directory, "logs", "mlvapp.log"), "a", encoding="utf-8") as handle:
                handle.write(trace_for(sha))


class PictureDumpBinding(unittest.TestCase):
    """LOOK-ASSIST-PROFILE-STATES-VERIFY-PICTURE-DUMP-BINDING-1 (Dot 793-I2): the picture the gate measures must be the
    one the application's own look_assist.apply.picture_dump line names, by content, for the application under test."""

    def test_a_stale_readable_png_with_final_none_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            make_bound_set(root, lambda sha: AUTO_WB_LINE + dump_line(sha, final="none", final_sha="none") + result_line())
            code, text = run_checker([root, "--tag", "t"])
            self.assertEqual(1, code, text)
            self.assertIn("STALE", text)
            self.assertIn("final=none", text)

    def test_a_png_whose_bytes_do_not_match_final_sha256_fails(self):
        with tempfile.TemporaryDirectory() as root:
            make_bound_set(root, lambda sha: AUTO_WB_LINE + dump_line(sha, final_sha="b" * 64) + result_line())
            code, text = run_checker([root, "--tag", "t"])
            self.assertEqual(1, code, text)
            self.assertIn("sha256", text)

    def test_a_dump_for_another_application_frame_or_exposure_fails(self):
        cases = {
            "frame": lambda sha: AUTO_WB_LINE + dump_line(sha, frame=3) + result_line(frame=3),
            "frame (final names another frame's file)": lambda sha: AUTO_WB_LINE + dump_line(sha, final="look-assist-final-frame3.png") + result_line(),
            "exposure": lambda sha: AUTO_WB_LINE + dump_line(sha, final_exposure=380) + result_line(exposure=160),
            "application (the dump precedes an earlier result, a later async application has none)":
                lambda sha: AUTO_WB_LINE + dump_line(sha) + result_line() + DISPATCH_LINE + ASYNC_APPLIED_LINE,
            "application (the dump follows the last result)": lambda sha: AUTO_WB_LINE + result_line() + dump_line(sha),
        }
        for label, trace_for in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as root:
                make_bound_set(root, trace_for)
                code, text = run_checker([root, "--tag", "t"])
                self.assertEqual(1, code, text)
                self.assertIn("STALE", text)

    def test_legacy_unbound_evidence_fails_unless_opted_in_and_is_never_reported_as_pass(self):
        with tempfile.TemporaryDirectory() as root:
            make_bound_set(root, lambda sha: AUTO_WB_LINE + result_line())     # no picture_dump line at all
            code, text = run_checker([root, "--tag", "t"])
            self.assertEqual(1, code, text)
            self.assertIn("UNBOUND_LEGACY", text)
            code, text = run_checker([root, "--tag", "t", "--allow-unbound-legacy"])
            self.assertEqual(0, code, text)
            self.assertIn("UNBOUND_LEGACY", text)
            self.assertNotIn("\nPASS", "\n" + text)

    # ---- controls: bound and correct evidence keeps every verdict it had ----
    def test_bound_and_correct_evidence_passes_and_says_it_is_bound(self):
        for safety in ("keep", "retreat"):
            with self.subTest(safety), tempfile.TemporaryDirectory() as root:
                make_bound_set(root, lambda sha: AUTO_WB_LINE + dump_line(sha, safety=safety) + result_line())
                code, text = run_checker([root, "--tag", "t"])
                self.assertEqual(0, code, text)
                self.assertIn("PASS", text)
                self.assertNotIn("UNBOUND_LEGACY", text)
                self.assertIn("bound", text)

    def test_the_newest_picture_dump_line_governs(self):
        stale_then_good = lambda sha: (AUTO_WB_LINE + dump_line(sha, final="none", final_sha="none") + result_line()
                                       + AUTO_WB_LINE + dump_line(sha) + result_line())
        good_then_stale = lambda sha: (AUTO_WB_LINE + dump_line(sha) + result_line()
                                       + AUTO_WB_LINE + dump_line(sha, final="none", final_sha="none") + result_line())
        with tempfile.TemporaryDirectory() as root:
            make_bound_set(root, stale_then_good)
            self.assertEqual(0, run_checker([root, "--tag", "t"])[0])
        with tempfile.TemporaryDirectory() as root:
            make_bound_set(root, good_then_stale)
            code, text = run_checker([root, "--tag", "t"])
            self.assertEqual(1, code, text)
            self.assertIn("STALE", text)

    def test_decisions_only_never_reads_the_picture_so_it_needs_no_binding(self):
        with tempfile.TemporaryDirectory() as root:
            make_bound_set(root, lambda sha: AUTO_WB_LINE + result_line())
            self.assertEqual(0, run_checker([root, "--tag", "t", "--decisions-only"])[0])

    def test_a_bound_line_in_one_arm_does_not_excuse_an_unbound_partner(self):
        with tempfile.TemporaryDirectory() as root:
            make_bound_set(root, lambda sha: AUTO_WB_LINE + dump_line(sha) + result_line())
            path = os.path.join(root, "t-tiny_dual_iso-async", "logs", "mlvapp.log")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(build_sha_line() + AUTO_WB_LINE + result_line())
            code, text = run_checker([root, "--tag", "t"])
            self.assertEqual(1, code, text)
            self.assertIn("tiny_dual_iso/async", text)
            self.assertIn("UNBOUND_LEGACY", text)


class PictureDumpBindingAdversaryR1(unittest.TestCase):
    """Adversary pass r1 on 7404a9ca (A1-A4): each input below passed the first cut of the binding."""

    def bound(self, trace_for):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        make_bound_set(root.name, trace_for)
        return root.name

    def test_a1_a_picture_dump_token_inside_a_skip_or_fallback_event_never_hides_it(self):
        good = lambda sha: AUTO_WB_LINE + dump_line(sha) + result_line()
        for label, extra in (("skip", PREFIX + "look_assist.apply.skip reason=look_assist.apply.picture_dump\n"),
                             ("safety_fallback", PREFIX + "look_assist.apply.safety_fallback warning=look_assist.apply.picture_dump\n")):
            with self.subTest(label):
                root = self.bound(lambda sha, extra=extra: extra + good(sha))
                code, text = run_checker([root, "--tag", "t"])
                self.assertEqual(1, code, text)
                self.assertTrue("skipped" in text or "safety fallback" in text, text)
        # and the token as a FIELD of another event is not a picture_dump line at all
        root = self.bound(lambda sha: AUTO_WB_LINE + PREFIX + "look_assist.apply.post_balance note=look_assist.apply.picture_dump final=none\n"
                          + dump_line(sha) + result_line())
        self.assertEqual(0, run_checker([root, "--tag", "t"])[0])

    def test_a2_the_result_must_carry_exactly_one_frame_equal_to_the_dumps(self):
        no_frame = PREFIX + ("look_assist.apply.result analysis=raw scene=shade median=37.000 preset_exp=160 preset_contrast=15 "
                             "final_temp=8950 final_tint=-35 thumb=180x226\n")
        for label, result in (("-1", result_line().replace("frame=0", "frame=-1")),
                              ("missing", no_frame),
                              ("0.5", result_line().replace("frame=0", "frame=0.5")),
                              ("0x10", result_line().replace("frame=0", "frame=0x10")),
                              ("empty", result_line().replace("frame=0", "frame=")),
                              ("last-frame=0 frame=1", result_line().replace("frame=0", "last-frame=0 frame=1")),
                              ("frame given twice", result_line().rstrip("\n") + " frame=0\n"),
                              ("another frame", result_line(frame=2))):
            with self.subTest(label):
                root = self.bound(lambda sha, result=result: AUTO_WB_LINE + dump_line(sha) + result)
                code, text = run_checker([root, "--tag", "t"])
                self.assertEqual(1, code, text)
                # a key given twice is rejected as "no complete apply" before the binding is reached (r2 A3)
                self.assertRegex(text, "STALE|no complete Look Assist apply")
        # an async application owns its dump the same way
        root = self.bound(lambda sha: DISPATCH_LINE + dump_line(sha) + ASYNC_APPLIED_LINE.replace("frame=0", "frame=-1"))
        self.assertEqual(1, run_checker([root, "--tag", "t"])[0])
        root = self.bound(lambda sha: DISPATCH_LINE + dump_line(sha) + ASYNC_APPLIED_LINE)
        code, text = run_checker([root, "--tag", "t"])
        self.assertEqual(0, code, text)
        self.assertIn("picture_dump-bound", text)

    def test_a_key_given_twice_in_the_dump_line_is_ambiguous_and_stale(self):
        root = self.bound(lambda sha: AUTO_WB_LINE + dump_line(sha).rstrip("\n") + " final_exposure=160\n" + result_line())
        code, text = run_checker([root, "--tag", "t"])
        self.assertEqual(1, code, text)
        self.assertIn("twice", text)

    def test_a3_the_measured_picture_and_the_hashed_picture_are_one_read(self):
        import builtins
        from unittest import mock
        root = self.bound(lambda sha: AUTO_WB_LINE + dump_line(sha) + result_line())
        real_open = builtins.open
        reads = []

        def counting_open(path, *args, **kwargs):
            if os.path.basename(str(path)) == os.path.basename(chk.APPLIED_PICTURE) and "tiny_dual_iso-sync" in str(path):
                reads.append(str(path))
            return real_open(path, *args, **kwargs)

        with mock.patch("builtins.open", counting_open):
            code, text = run_checker([root, "--tag", "t"])
        self.assertEqual(0, code, text)
        self.assertEqual(1, len(reads), reads)

    def test_a4_a_picture_that_cannot_be_read_is_exit_2_never_a_crash(self):
        import builtins
        from unittest import mock
        root = self.bound(lambda sha: AUTO_WB_LINE + dump_line(sha) + result_line())
        real_open = builtins.open
        seen = []

        def locked_after_first(path, *args, **kwargs):
            if "tiny_dual_iso-sync" in str(path) and os.path.basename(str(path)) == os.path.basename(chk.APPLIED_PICTURE):
                seen.append(1)
                if len(seen) > 1:
                    raise PermissionError(13, "sharing violation", str(path))
            return real_open(path, *args, **kwargs)

        with mock.patch("builtins.open", locked_after_first):
            code, text = run_checker([root, "--tag", "t"])           # a lock after the single read changes nothing
        self.assertEqual(0, code, text)

        def locked(path, *args, **kwargs):
            if "tiny_dual_iso-sync" in str(path) and os.path.basename(str(path)) == os.path.basename(chk.APPLIED_PICTURE):
                raise PermissionError(13, "sharing violation", str(path))
            return real_open(path, *args, **kwargs)

        with mock.patch("builtins.open", locked):
            code, text = run_checker([root, "--tag", "t"])
        self.assertEqual(2, code, text)


class PictureDumpBindingAdversaryR2(unittest.TestCase):
    """Adversary pass r1 on b9106c64 (A1, A2)."""

    def bound(self, trace_for):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        make_bound_set(root.name, trace_for)
        return root.name

    def test_a1_an_event_spelled_out_inside_another_events_field_is_not_a_dump_or_a_result(self):
        embedded_dump = lambda sha: AUTO_WB_LINE + dump_line(sha).replace(
            "event=look_assist.apply.picture_dump",
            "event=look_assist.apply.post_balance note=interaction_trace event=look_assist.apply.picture_dump") + result_line()
        root = self.bound(embedded_dump)
        code, text = run_checker([root, "--tag", "t"])
        self.assertEqual(1, code, text)
        self.assertIn("UNBOUND_LEGACY", text)
        self.assertNotIn("picture_dump-bound", text)
        # a genuine bound application, then another event that spells a result out in a field: the result stands
        forged = PREFIX + "look_assist.apply.post_balance note=interaction_trace event=look_assist.apply.result scene=night preset_exp=999 final_temp=1 final_tint=1\n"
        root = self.bound(lambda sha: AUTO_WB_LINE + dump_line(sha) + result_line() + forged)
        code, text = run_checker([root, "--tag", "t"])
        self.assertEqual(0, code, text)
        self.assertIn("picture_dump-bound", text)

    def test_a2_an_absurdly_long_frame_is_stale_not_a_crash(self):
        root = self.bound(lambda sha: AUTO_WB_LINE + dump_line(sha) + result_line().replace("frame=0", "frame=" + "9" * 4301))
        code, text = run_checker([root, "--tag", "t"])
        self.assertEqual(1, code, text)
        # r5: the result's frame is validated when the line is read (every mode), so a huge frame is a malformed result
        # (no complete apply) before the binding is reached; either way the arm is refused and nothing binds.
        self.assertRegex(text, "STALE|no complete Look Assist apply")
        self.assertNotIn("picture_dump-bound", text)


class PictureDumpBindingAdversaryR3(unittest.TestCase):
    """Adversary pass 3 on 8c6a72e1 (A1, A2) and its same-path advisories (A3 event anchor, A5 corrupt PNG, A6 digit bound)."""

    def bound(self, trace_for):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        make_bound_set(root.name, trace_for)
        return root.name

    def test_a1_a_result_or_applied_line_without_the_event_marker_never_owns_a_dump(self):
        def markerless(line):
            return line.replace("interaction_trace event=", "note=event=")
        sync = lambda sha: AUTO_WB_LINE + dump_line(sha) + markerless(result_line())
        asyn = lambda sha: DISPATCH_LINE + dump_line(sha) + markerless(ASYNC_APPLIED_LINE)
        everything = lambda sha: markerless(AUTO_WB_LINE + dump_line(sha) + result_line())
        for label, trace_for in (("sync", sync), ("async", asyn)):
            with self.subTest(label):
                code, text = run_checker([self.bound(trace_for), "--tag", "t"])
                self.assertEqual(1, code, text)
                self.assertIn("STALE", text)
                self.assertNotIn("picture_dump-bound", text)
        with self.subTest("every line markerless"):
            self.assertEqual(1, run_checker([self.bound(everything), "--tag", "t"])[0])
        # a markerless line keeps its DECISION reading (back-compat) when the arm is opted in as legacy
        legacy = self.bound(lambda sha: markerless(AUTO_WB_LINE + result_line()))
        self.assertEqual(0, main_legacy([legacy, "--tag", "t"]))

    def test_a2_the_result_fields_are_whitespace_delimited_pairs_given_once_as_integers(self):
        good_dump = lambda sha: AUTO_WB_LINE + dump_line(sha)
        cases = {
            "prefix token previous-preset_exp=160 before preset_exp=380":
                result_line().replace("preset_exp=160", "previous-preset_exp=160 preset_exp=380"),
            "preset_exp=160.5": result_line().replace("preset_exp=160", "preset_exp=160.5"),
            "preset_exp given twice": result_line().replace("preset_exp=160", "preset_exp=160 preset_exp=160"),
            "final_temp=8950.5": result_line().replace("final_temp=8950", "final_temp=8950.5"),
            "final_tint=-35.5": result_line().replace("final_tint=-35", "final_tint=-35.5"),
            "shadow x-final_tint": result_line().replace("final_tint=-35", "x-final_tint=-35"),
            "scene given twice": result_line().replace("scene=shade", "scene=shade scene=night"),
            "4301-digit preset_exp": result_line().replace("preset_exp=160", "preset_exp=" + "1" * 4301),
        }
        for label, result in cases.items():
            with self.subTest(label):
                code, text = run_checker([self.bound(lambda sha, result=result: good_dump(sha) + result), "--tag", "t"])
                self.assertEqual(1, code, text)
                self.assertNotIn("picture_dump-bound", text)
        for label, applied in (("async preset_exp=160.5", ASYNC_APPLIED_LINE.replace("preset_exp=160", "preset_exp=160.5")),
                               ("async prefix token", ASYNC_APPLIED_LINE.replace("preset_exp=160", "previous-preset_exp=160 preset_exp=380")),
                               ("async 4301-digit final_temp", ASYNC_APPLIED_LINE.replace(" final_temp=9990", " final_temp=" + "9" * 4301))):
            with self.subTest(label):
                root = self.bound(lambda sha, applied=applied: DISPATCH_LINE + dump_line(sha) + applied)
                code, text = run_checker([root, "--tag", "t"])
                self.assertEqual(1, code, text)
                self.assertNotIn("picture_dump-bound", text)
        # a LATER malformed result is not ignored in favour of an earlier receipt
        root = self.bound(lambda sha: good_dump(sha) + result_line() + AUTO_WB_LINE + dump_line(sha)
                          + result_line().replace("final_temp=8950", "final_temp="))
        self.assertEqual(1, run_checker([root, "--tag", "t"])[0])
        # control: well-formed fields still bind
        code, text = run_checker([self.bound(lambda sha: good_dump(sha) + result_line()), "--tag", "t"])
        self.assertEqual(0, code, text)

    def test_a3_only_the_named_event_counts_as_wb_evidence_and_a_skip_is_never_a_safety_fallback(self):
        forged_wb = PREFIX + ("look_assist.apply.post_balance note=look_assist.apply.auto_wb valid=1 "
                              "source=rendered-neutral-patch decision=accepted\n")
        code, text = run_checker([self.bound(lambda sha: forged_wb + dump_line(sha) + result_line()), "--tag", "t"])
        self.assertEqual(1, code, text)
        self.assertNotIn("picture_dump-bound", text)
        skip = PREFIX + "look_assist.apply.skip reason=look_assist.apply.safety_fallback\n"
        code, text = run_checker([self.bound(lambda sha: skip + AUTO_WB_LINE + dump_line(sha) + result_line()), "--tag", "t"])
        self.assertEqual(1, code, text)
        self.assertIn("skipped", text)
        self.assertNotIn("safety fallback", text)

    # ---- adversary pass on 85b1d8b3 (A1, A2, A3, A6): each input below passed the r2 first cut ----
    def test_p4_a1_a_markerless_result_discards_the_pending_dump_so_a_later_result_cannot_inherit_it(self):
        def markerless(line):
            return line.replace("interaction_trace event=", "note=event=")
        later = result_line().replace("final_temp=8950 final_tint=-35", "final_temp=5840 final_tint=7")
        sync = lambda sha: AUTO_WB_LINE + dump_line(sha) + markerless(result_line()) + later
        asyn = lambda sha: (DISPATCH_LINE + dump_line(sha) + markerless(ASYNC_APPLIED_LINE) + ASYNC_APPLIED_LINE)
        for label, trace_for in (("sync", sync), ("async", asyn)):
            with self.subTest(label):
                code, text = run_checker([self.bound(trace_for), "--tag", "t"])
                self.assertEqual(1, code, text)
                self.assertNotIn("picture_dump-bound", text)

    def test_p4_a2_the_event_is_what_follows_the_first_interaction_trace_tag_even_when_it_is_empty(self):
        stamp = "[2026-10-02T01:56:36.037Z] [INFO] [0x2e638] "
        empty_first = lambda line: stamp + "interaction_trace event= " + line
        no_event_first = lambda line: stamp + "interaction_trace note=x " + line
        for name, wrap in (("empty event", empty_first), ("tag without event", no_event_first)):
            cases = {
                "result": lambda sha: AUTO_WB_LINE + dump_line(sha) + wrap(result_line()),
                "dump": lambda sha: AUTO_WB_LINE + wrap(dump_line(sha)) + result_line(),
                "auto_wb": lambda sha: wrap(AUTO_WB_LINE) + dump_line(sha) + result_line(),
                "async applied": lambda sha: DISPATCH_LINE + dump_line(sha) + wrap(ASYNC_APPLIED_LINE),
                "async dispatch": lambda sha: wrap(DISPATCH_LINE) + dump_line(sha) + ASYNC_APPLIED_LINE,
            }
            for label, trace_for in cases.items():
                with self.subTest("%s / %s" % (name, label)):
                    code, text = run_checker([self.bound(trace_for), "--tag", "t"])
                    self.assertEqual(1, code, text)
                    self.assertNotIn("picture_dump-bound", text)

    def test_p4_a3_a_key_given_twice_on_an_event_line_is_ambiguous_and_no_apply(self):
        cases = {
            "async valid twice": lambda sha: DISPATCH_LINE + dump_line(sha) + ASYNC_APPLIED_LINE.replace("valid=1", "valid=1 valid=0"),
            "result analysis twice": lambda sha: AUTO_WB_LINE + dump_line(sha) + result_line().replace("analysis=raw", "analysis=raw analysis=raw"),
            "auto_wb damping twice": lambda sha: AUTO_WB_LINE.replace("damping=1.000", "damping=1.000 damping=0.500") + dump_line(sha) + result_line(),
            "dispatch generation twice": lambda sha: DISPATCH_LINE.replace("generation=3", "generation=3 generation=4") + dump_line(sha) + ASYNC_APPLIED_LINE,
        }
        for label, trace_for in cases.items():
            with self.subTest(label):
                code, text = run_checker([self.bound(trace_for), "--tag", "t"])
                self.assertEqual(1, code, text)
                self.assertNotIn("picture_dump-bound", text)

    def test_p4_a6_a_new_applications_auto_wb_does_not_inherit_the_previous_results_receipt(self):
        other_wb = AUTO_WB_LINE.replace("source=rendered-neutral-patch", "source=processed-neutral-patch")
        root = self.bound(lambda sha: AUTO_WB_LINE + result_line() + other_wb)       # the log ends right after the new auto_wb
        for argv in (["--decisions-only"], ["--decisions-only", "--allow-unbound-legacy"]):
            with self.subTest(argv):
                code, text = run_checker([root, "--tag", "t"] + argv)
                self.assertEqual(1, code, text)
                self.assertIn("no complete Look Assist apply", text)
        # control: an async flow keeps the dispatch's scene for its own auto_wb and applied lines
        root = self.bound(lambda sha: AUTO_WB_LINE + result_line() + DISPATCH_LINE + other_wb + dump_line(sha) + ASYNC_APPLIED_LINE)
        code, text = run_checker([root, "--tag", "t"])
        self.assertEqual(0, code, text)

    # ---- adversary pass on 2c000780 (A1, A2): each input below passed the second cut ----
    def test_p5_a1_the_results_frame_is_a_field_of_its_own_event_body_not_a_token_before_the_marker(self):
        no_frame = lambda line: line.replace(" frame=0", "")
        cases = {
            "result": lambda sha: AUTO_WB_LINE + dump_line(sha) + "frame=0 " + no_frame(result_line()),
            "async applied": lambda sha: DISPATCH_LINE + dump_line(sha) + "frame=0 " + no_frame(ASYNC_APPLIED_LINE),
            "result with frame=1 before the marker too": lambda sha: AUTO_WB_LINE + dump_line(sha) + "frame=1 " + result_line(),
        }
        for label, trace_for in cases.items():
            with self.subTest(label):
                code, text = run_checker([self.bound(trace_for), "--tag", "t"])
                if label.startswith("result with"):
                    self.assertEqual(0, code, text)      # control: the body's own single frame=0 is what counts
                else:
                    self.assertEqual(1, code, text)
                    self.assertNotIn("picture_dump-bound", text)

    def test_p5_a2_the_interaction_trace_tag_is_a_standalone_token_not_a_suffix_or_a_field_value(self):
        for label, forged in (("hyphen", "not-interaction_trace event="), ("field value", "note=interaction_trace event="),
                              ("underscore suffix", "x_interaction_trace event=")):
            with self.subTest(label):
                root = self.bound(lambda sha, forged=forged: (AUTO_WB_LINE + dump_line(sha) + result_line()).replace(
                    "interaction_trace event=", forged))
                code, text = run_checker([root, "--tag", "t"])
                self.assertEqual(1, code, text)
                self.assertNotIn("picture_dump-bound", text)

    # ---- adversary pass on 5ebbea40 (A1, A2): each input below passed the third cut ----
    def test_p6_a1_a_complete_application_closes_the_receipt_so_a_later_result_cannot_be_a_composite(self):
        later = result_line().replace("final_temp=8950", "final_temp=5840")
        cases = {
            "result after result": lambda sha: AUTO_WB_LINE + result_line() + dump_line(sha) + later,
            "applied after result": lambda sha: AUTO_WB_LINE + result_line() + dump_line(sha) + ASYNC_APPLIED_LINE,
            "result after applied": lambda sha: DISPATCH_LINE + ASYNC_APPLIED_LINE + dump_line(sha) + later,
            "applied after applied": lambda sha: (DISPATCH_LINE + ASYNC_APPLIED_LINE + dump_line(sha)
                                                  + ASYNC_APPLIED_LINE.replace("final_temp=9990", "final_temp=5840")),
        }
        for label, trace_for in cases.items():
            root = self.bound(trace_for)
            for argv in ([], ["--decisions-only"]):
                with self.subTest("%s %s" % (label, argv)):
                    code, text = run_checker([root, "--tag", "t"] + argv)
                    self.assertEqual(1, code, text)
                    self.assertNotIn("picture_dump-bound", text)
        # controls: a NEW application (auto_wb, or a dispatch) opens a fresh base and binds its own dump
        for label, trace_for in (
                ("sync then sync", lambda sha: AUTO_WB_LINE + result_line() + AUTO_WB_LINE + dump_line(sha) + result_line()),
                ("sync then async", lambda sha: AUTO_WB_LINE + result_line() + DISPATCH_LINE + dump_line(sha) + ASYNC_APPLIED_LINE),
                ("async then async", lambda sha: DISPATCH_LINE + ASYNC_APPLIED_LINE + DISPATCH_LINE + dump_line(sha) + ASYNC_APPLIED_LINE)):
            with self.subTest("control " + label):
                code, text = run_checker([self.bound(trace_for), "--tag", "t"])
                self.assertEqual(0, code, text)

    def test_p6_a2_an_applied_event_must_carry_the_generation_of_its_own_dispatch(self):
        cases = {
            "applied of another generation": lambda sha: DISPATCH_LINE + dump_line(sha) + ASYNC_APPLIED_LINE.replace("generation=3", "generation=4"),
            "applied with no generation": lambda sha: DISPATCH_LINE + dump_line(sha) + ASYNC_APPLIED_LINE.replace("generation=3 ", ""),
            "dispatch with no generation, applied with one": lambda sha: DISPATCH_LINE.replace("generation=3 ", "") + dump_line(sha) + ASYNC_APPLIED_LINE,
            "dispatch of another generation after the first": lambda sha: DISPATCH_LINE + DISPATCH_LINE.replace("generation=3", "generation=4") + dump_line(sha) + ASYNC_APPLIED_LINE,
        }
        for label, trace_for in cases.items():
            root = self.bound(trace_for)
            for argv in ([], ["--decisions-only"]):
                with self.subTest("%s %s" % (label, argv)):
                    code, text = run_checker([root, "--tag", "t"] + argv)
                    self.assertEqual(1, code, text)
                    self.assertNotIn("picture_dump-bound", text)
        # controls: the same generation binds, and a dispatch and an applied that BOTH carry no generation keep the old reading
        for label, trace_for in (
                ("matching generation 4", lambda sha: DISPATCH_LINE.replace("generation=3", "generation=4") + dump_line(sha)
                                          + ASYNC_APPLIED_LINE.replace("generation=3", "generation=4")),
                ("neither carries one", lambda sha: DISPATCH_LINE.replace("generation=3 ", "") + dump_line(sha)
                                        + ASYNC_APPLIED_LINE.replace("generation=3 ", ""))):
            with self.subTest("control " + label):
                code, text = run_checker([self.bound(trace_for), "--tag", "t"])
                self.assertEqual(0, code, text)

    # ---- adversary pass on 5cb9b1a9 (A1): each input below passed the fourth cut ----
    def test_p7_a1_every_field_the_apps_dispatch_and_applied_lines_always_carry_is_validated(self):
        cases = {}
        for bad in ("1.5", "", "true", "{}", "1234567890", "x"):
            cases["applied valid=%r" % bad] = lambda sha, bad=bad: DISPATCH_LINE + dump_line(sha) + ASYNC_APPLIED_LINE.replace("valid=1", "valid=" + bad)
        cases["applied valid absent"] = lambda sha: DISPATCH_LINE + dump_line(sha) + ASYNC_APPLIED_LINE.replace("valid=1 ", "")
        for bad in ("0.5", "bad", "", "1234567890"):
            cases["dispatch frame=%r" % bad] = lambda sha, bad=bad: DISPATCH_LINE.replace("frame=0", "frame=" + bad) + dump_line(sha) + ASYNC_APPLIED_LINE
        cases["dispatch frame absent"] = lambda sha: DISPATCH_LINE.replace(" frame=0", "") + dump_line(sha) + ASYNC_APPLIED_LINE
        for bad in ("awb_temp=x", "awb_tint=1.5", "awb_temp="):
            key = bad.split("=")[0]
            cases["applied " + bad] = lambda sha, key=key, bad=bad: (DISPATCH_LINE + dump_line(sha) + ASYNC_APPLIED_LINE.replace(
                {"awb_temp": "awb_temp=9990", "awb_tint": "awb_tint=-35"}[key], bad))
        for label, trace_for in cases.items():
            root = self.bound(trace_for)
            for argv in ([], ["--decisions-only"]):
                with self.subTest("%s %s" % (label, argv)):
                    code, text = run_checker([root, "--tag", "t"] + argv)
                    self.assertEqual(1, code, text)
                    self.assertNotIn("picture_dump-bound", text)
        # control: valid=0 is a well-formed flag, and a clean pair still binds
        for flag in ("0", "1"):
            with self.subTest("control valid=" + flag):
                trace_for = lambda sha, flag=flag: DISPATCH_LINE + dump_line(sha) + ASYNC_APPLIED_LINE.replace("valid=1", "valid=" + flag)
                self.assertEqual(0, run_checker([self.bound(trace_for), "--tag", "t"])[0])

    # ---- adversary pass on 7e0cb505 (A1, A2): each input below passed the fifth cut ----
    def test_p8_a1_the_applied_event_repeats_the_frame_its_dispatch_analysed(self):
        cases = {
            "dispatch frame=1, applied and dump frame=0": lambda sha: DISPATCH_LINE.replace("frame=0", "frame=1") + dump_line(sha) + ASYNC_APPLIED_LINE,
            "dispatch frame=0, applied frame=1": lambda sha: DISPATCH_LINE + dump_line(sha) + ASYNC_APPLIED_LINE.replace("frame=0", "frame=1"),
            "dispatch frame=00": lambda sha: DISPATCH_LINE.replace("frame=0", "frame=00") + dump_line(sha) + ASYNC_APPLIED_LINE,
        }
        for label, trace_for in cases.items():
            root = self.bound(trace_for)
            for argv in ([], ["--decisions-only"]):
                with self.subTest("%s %s" % (label, argv)):
                    code, text = run_checker([root, "--tag", "t"] + argv)
                    self.assertEqual(1, code, text)
                    self.assertNotIn("picture_dump-bound", text)
        # controls: the same frame binds (frame 0), and decisions-only accepts another frame when both lines agree
        self.assertEqual(0, run_checker([self.bound(lambda sha: DISPATCH_LINE + dump_line(sha) + ASYNC_APPLIED_LINE), "--tag", "t"])[0])
        agree = lambda sha: DISPATCH_LINE.replace("frame=0", "frame=1") + ASYNC_APPLIED_LINE.replace("frame=0", "frame=1")
        self.assertEqual(0, run_checker([self.bound(agree), "--tag", "t", "--decisions-only"])[0])

    def test_p8_a2_the_valid_flag_is_zero_or_one_as_the_app_logs_it(self):
        cases = {}
        for bad in ("9", "2", "10", "-1"):
            cases["applied valid=" + bad] = lambda sha, bad=bad: DISPATCH_LINE + dump_line(sha) + ASYNC_APPLIED_LINE.replace("valid=1", "valid=" + bad)
            cases["auto_wb valid=" + bad] = lambda sha, bad=bad: AUTO_WB_LINE.replace("valid=1", "valid=" + bad) + dump_line(sha) + result_line()
        for label, trace_for in cases.items():
            root = self.bound(trace_for)
            for argv in ([], ["--decisions-only"]):
                with self.subTest("%s %s" % (label, argv)):
                    code, text = run_checker([root, "--tag", "t"] + argv)
                    self.assertEqual(1, code, text)
                    self.assertNotIn("picture_dump-bound", text)
        for flag in ("0", "1"):         # controls
            with self.subTest("control auto_wb valid=" + flag):
                trace_for = lambda sha, flag=flag: AUTO_WB_LINE.replace("valid=1", "valid=" + flag) + dump_line(sha) + result_line()
                self.assertEqual(0, run_checker([self.bound(trace_for), "--tag", "t"])[0])

    # ---- sol on af63f817 (r3, INV-1): a duplicate whose name the field pattern did not take was omitted before the duplicate check ----
    def test_p9_a1_a_duplicate_field_of_any_spelling_on_any_event_line_is_ambiguous_and_never_bound(self):
        def add(line, extra):
            return line.rstrip("\n") + " " + extra + "\n"
        spellings = {
            "Audit=a Audit=b": "Audit=a Audit=b",
            "audit-note=a audit-note=b": "audit-note=a audit-note=b",
            "case-folded Audit=a audit=b": "Audit=a audit=b",
            "case-folded known key Scene=night": "Scene=night",       # only the events that carry scene=
            "case-folded known key FRAME=7": "FRAME=7",          # only the events that carry frame=
            "hyphen and case AUDIT-NOTE=a audit-note=b": "AUDIT-NOTE=a audit-note=b",
            # adversary pass on 978b1ac0 A1: the event selector itself is a field of the line, so a second one is a duplicate
            "second event= selector (skip)": "event=look_assist.apply.skip",
            "second EVENT= selector (skip)": "EVENT=look_assist.apply.skip",
            "second event= selector (result)": "event=look_assist.apply.result",
            "second Event= selector (same name)": "Event=look_assist.apply.picture_dump",
        }
        flows = {
            "dump": lambda sha, x: (AUTO_WB_LINE + add(dump_line(sha), x) + result_line()),
            "result": lambda sha, x: (AUTO_WB_LINE + dump_line(sha) + add(result_line(), x)),
            "auto_wb": lambda sha, x: (add(AUTO_WB_LINE, x) + dump_line(sha) + result_line()),
            "dispatch": lambda sha, x: (add(DISPATCH_LINE, x) + dump_line(sha) + ASYNC_APPLIED_LINE),
            "applied": lambda sha, x: (DISPATCH_LINE + dump_line(sha) + add(ASYNC_APPLIED_LINE, x)),
        }
        carries = {"Scene=night": ("result", "dispatch"), "FRAME=7": ("dump", "result", "dispatch", "applied")}
        for spelling, extra in spellings.items():
            for event, flow in flows.items():
                if event not in carries.get(extra, flows):
                    continue
                root = self.bound(lambda sha, flow=flow, extra=extra: flow(sha, extra))
                for argv in ([], ["--decisions-only"]):      # r5 (adversary 0e450da2 A1): a dump line the arm carries is validated in EVERY mode
                    with self.subTest("%s on %s %s" % (spelling, event, argv)):
                        code, text = run_checker([root, "--tag", "t"] + argv)
                        self.assertEqual(1, code, text)
                        self.assertNotIn("picture_dump-bound", text)
        # controls: distinct names (one hyphenated, one upper-case) are no duplicate and still bind
        for event, flow in flows.items():
            with self.subTest("control " + event):
                code, text = run_checker([self.bound(lambda sha, flow=flow: flow(sha, "Audit=a audit-note=b")), "--tag", "t"])
                self.assertEqual(0, code, text)

    # ---- adversary pass on 20c35304 (A1): damping and floor_lifted are always logged, damping as %.3f and floor_lifted as 0/1 ----
    def test_p10_a1_damping_and_floor_lifted_are_validated_as_the_app_logs_them(self):
        cases = {}
        for bad in ("x", "", "{}", "1", "1.0", "1.0000", "1.5.0", "1e0", "1234567890.000"):
            cases["applied damping=%r" % bad] = lambda sha, bad=bad: DISPATCH_LINE + dump_line(sha) + ASYNC_APPLIED_LINE.replace("damping=1.000", "damping=" + bad)
            cases["auto_wb damping=%r" % bad] = lambda sha, bad=bad: AUTO_WB_LINE.replace("damping=1.000", "damping=" + bad) + dump_line(sha) + result_line()
        for bad in ("x", "", "2", "-1", "true", "00"):
            cases["dispatch floor_lifted=%r" % bad] = lambda sha, bad=bad: DISPATCH_LINE.replace("floor_lifted=0", "floor_lifted=" + bad) + dump_line(sha) + ASYNC_APPLIED_LINE
        cases["applied damping absent"] = lambda sha: DISPATCH_LINE + dump_line(sha) + ASYNC_APPLIED_LINE.replace(" damping=1.000", "")
        cases["auto_wb damping absent"] = lambda sha: AUTO_WB_LINE.replace(" damping=1.000", "") + dump_line(sha) + result_line()
        cases["dispatch floor_lifted absent"] = lambda sha: DISPATCH_LINE.replace(" floor_lifted=0", "") + dump_line(sha) + ASYNC_APPLIED_LINE
        for label, trace_for in cases.items():
            root = self.bound(trace_for)
            for argv in ([], ["--decisions-only"]):
                with self.subTest("%s %s" % (label, argv)):
                    code, text = run_checker([root, "--tag", "t"] + argv)
                    self.assertEqual(1, code, text)
                    self.assertNotIn("picture_dump-bound", text)
        # controls: the app's own spellings bind
        controls = {
            "applied damping=0.500": lambda sha: DISPATCH_LINE + dump_line(sha) + ASYNC_APPLIED_LINE.replace("damping=1.000", "damping=0.500"),
            "auto_wb damping=0.000": lambda sha: AUTO_WB_LINE.replace("damping=1.000", "damping=0.000") + dump_line(sha) + result_line(),
            "dispatch floor_lifted=1": lambda sha: DISPATCH_LINE.replace("floor_lifted=0", "floor_lifted=1") + dump_line(sha) + ASYNC_APPLIED_LINE,
        }
        for label, trace_for in controls.items():
            with self.subTest("control " + label):
                code, text = run_checker([self.bound(trace_for), "--tag", "t"])
                self.assertEqual(0, code, text)

    def test_a5_a_corrupt_png_is_exit_2_not_a_crash(self):
        import struct
        import zlib

        def chunk(kind, body):
            return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xffffffff)
        sig = b"\x89PNG\r\n\x1a\n"
        ihdr = struct.pack(">IIBBBBB", 80, 40, 8, 2, 0, 0, 0)
        corrupt = {
            "not a zlib stream": sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", b"this is not zlib") + chunk(b"IEND", b""),
            "short IHDR": sig + chunk(b"IHDR", b"\x00\x00") + chunk(b"IDAT", zlib.compress(b"x")) + chunk(b"IEND", b""),
            "truncated chunk header": sig + b"\x00\x00\x00",
        }
        for label, data in corrupt.items():
            with self.subTest(label):
                root = self.bound(lambda sha: AUTO_WB_LINE + dump_line(sha) + result_line())
                with open(os.path.join(root, "t-tiny_dual_iso-sync", chk.APPLIED_PICTURE), "wb") as handle:
                    handle.write(data)
                code, text = run_checker([root, "--tag", "t"])
                self.assertEqual(2, code, text)
                self.assertIn("cannot read a picture", text)


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


# ---- r5 (sol key on 41f96076, B1 + B2): ONE table of the fields the app prints, checked for every event in every mode ----
# These lines carry EVERY field the app's format strings print (platform/qt/MainWindow.cpp auto_wb / result / async_dispatch /
# auto_wb_async_applied, src/batch/LookAssistAnalysis.cpp lookAssistDecisionLogFields / lookAssistPictureDumpFields), written out
# here independently of the checker's table; TableDrift proves the two agree with each other and with the app's source.
FULL_AUTO_WB = ("look_assist.apply.auto_wb valid=1 source=rendered-neutral-patch decision=accepted damping=1.000 raw_x=40 raw_y=20 "
                "patch_luma=0.520 patch_chroma=0.010 patch_green_axis=0.001 patch_blue_amber_axis=-0.002 base_temp=6000 base_tint=0 "
                "candidate_temp=8950 candidate_tint=-35 awb_temp=8950 awb_tint=-35 final_temp_delta=2950 final_tint_delta=-35")
FULL_RESULT = ("look_assist.apply.result analysis=raw scene=shade median=37.000 p05=35.000 p95=37.000 p99=38.000 clip_low=0.000000 "
               "clip_high=0.000000 balance_samples=40680 preset_exp=160 preset_contrast=15 preset_pivot=46 preset_shadows=32 "
               "preset_highlights=-26 preset_vibrance=3 preset_temp_delta=2950 preset_tint_delta=-35 final_temp=8950 final_tint=-35 "
               "thumb=180x226 downscale=10 color_thumb=602x756 color_downscale=3 frame=0 last_serial=1 last_frame=0 next_serial=2 "
               "has_ev100=1 ev100=12.345 daylight_gate=open post_walk_ran=1 post_walk_branch=recovery post_walk_recovery=-100/5 "
               "display_meter_ran=0 playback_scale=NA ev100_bound=NA ev100_source=recorded surface_search=converged "
               "surface_search_balance=8950/-35 flavor=classic grade=none")
FULL_DISPATCH = "look_assist.apply.async_dispatch generation=3 frame=0 scene=shade floor_lifted=0 flavor=classic grade=none"
FULL_APPLIED = ("look_assist.apply.auto_wb_async_applied generation=3 valid=1 source=processed-neutral-patch decision=accepted "
                "damping=1.000 awb_temp=9990 awb_tint=-35 final_temp=9990 final_tint=-35 preset_exp=160 frame=0")
FIELD_TYPES = {
    "auto_wb": {"valid": "bool01", "source": "word", "decision": "word", "damping": "fixed3", "raw_x": "int", "raw_y": "int",
                "patch_luma": "fixed3", "patch_chroma": "fixed3", "patch_green_axis": "fixed3", "patch_blue_amber_axis": "fixed3",
                "base_temp": "int", "base_tint": "int", "candidate_temp": "int", "candidate_tint": "int", "awb_temp": "int",
                "awb_tint": "int", "final_temp_delta": "int", "final_tint_delta": "int"},
    "result": {"analysis": "word", "scene": "word", "median": "fixed3", "p05": "fixed3", "p95": "fixed3", "p99": "fixed3",
               "clip_low": "fixed6", "clip_high": "fixed6", "balance_samples": "int", "preset_exp": "int", "preset_contrast": "int",
               "preset_pivot": "int", "preset_shadows": "int", "preset_highlights": "int", "preset_vibrance": "int",
               "preset_temp_delta": "int", "preset_tint_delta": "int", "final_temp": "int", "final_tint": "int", "thumb": "dims",
               "downscale": "int", "color_thumb": "dims", "color_downscale": "int", "frame": "uint", "last_serial": "uint64",
               "last_frame": "int", "next_serial": "uint64", "has_ev100": "bool01", "ev100": "fixed3|NA", "daylight_gate": "word",
               "post_walk_ran": "bool01", "post_walk_branch": "post_walk_branch", "post_walk_recovery": "ratio|NA",
               "display_meter_ran": "bool01", "playback_scale": "uint|NA", "ev100_bound": "fixed3|NA",
               "ev100_source": "ev100_source", "surface_search": "word", "surface_search_balance": "ratio|NA", "flavor": "word",
               "grade": "word"},
    "dispatch": {"generation": "int", "frame": "uint", "scene": "word", "floor_lifted": "bool01", "flavor": "word", "grade": "word"},
    "applied": {"generation": "int", "valid": "bool01", "source": "word", "decision": "word", "damping": "fixed3", "awb_temp": "int",
                "awb_tint": "int", "final_temp": "int", "final_tint": "int", "preset_exp": "int", "frame": "uint"},
    "dump": {"pre_safety": "pre_safety_png|none", "pre_safety_sha256": "sha256|none", "pre_safety_exposure": "int",
             "final": "final_png|none", "final_sha256": "sha256|none", "final_exposure": "int", "safety": "safety", "frame": "uint"},
}
HUGE = "9" * 4301
_NUM = ["NaN", "nan", "", "1.5", "-", "+1", "1e3", "0x10", "-0", "007", "1234567890", "-1234567890", HUGE, "-" + HUGE, "{}", "x"]
_FIX = ["NaN", "nan", "inf", "-inf", "", "1", "1.0", "1e0", "x", "1.5.0", "+1.000", "1234567890.000", ".500", HUGE + ".000"]
BAD_VALUES = {
    "int": _NUM,
    "uint": _NUM + ["-1", "-999999999"],
    "uint64": ["NaN", "", "1.5", "-1", "+1", "007", "1" * 21, HUGE, "x",
               "18446744073709551616", "99999999999999999999"],      # r5 (adversary 0e450da2 A2): 20 digits above the qulonglong maximum
    "bool01": ["2", "-1", "true", "", "01", "1.0", "x", "10"],
    "fixed3": _FIX + ["1.0000"],
    "fixed6": _FIX + ["1.000", "1.0000000"],
    "word": [""],
    "dims": ["10", "10x", "x10", "-1x5", "1.5x2", "NaNxNaN", "10x10x10", ""],
    "ratio|NA": ["", "1", "1/", "/1", "1.5/2", "NaN/NaN", "1/2/3", "na", "N/A"],
    "fixed3|NA": _FIX + ["1.0000", "na", "N/A"],
    "uint|NA": _NUM + ["-1", "na"],
    "sha256|none": ["", "x", "a" * 63, "a" * 65, "g" * 64, "NONE"],
    "pre_safety_png|none": ["", "x.png", "look-assist-pre-safety-frame-1.png", "../look-assist-pre-safety-frame0.png", "look-assist-pre-safety-frameX.png"],
    "final_png|none": ["", "x.png", "look-assist-final-frame-1.png", "../look-assist-final-frame0.png", "look-assist-final-frameX.png"],
    "safety": ["", "Keep", "other", "keep2"],
    "ev100_source": ["", "x", "Recorded"],
    "post_walk_branch": ["", "x", "Steps"],
}
GOOD_VALUES = {
    "int": ["0", "-1", "123456789", "-123456789"], "uint": ["0", "7", "123456789"], "uint64": ["0", "18446744073709551615"],
    "bool01": ["0", "1"], "fixed3": ["0.000", "-0.500", "123.456"], "fixed6": ["0.000000", "0.250000"], "word": ["x"],
    "dims": ["0x0", "602x756"], "ratio|NA": ["NA", "0/0", "-100/5"], "fixed3|NA": ["NA", "0.000", "-1.250"], "uint|NA": ["NA", "0", "4"],
    "sha256|none": ["none", "d" * 64, "D" * 64], "safety": ["keep", "retreat", "revert"], "ev100_source": ["recorded", "aperture_bound", "none"],
    "post_walk_branch": ["none", "steps", "cleanup", "recovery"],
}
# What the checker's verdict reads: a different (still well-typed) value there changes the outcome, so the sweep of good values
# leaves those fields to their own tests.
READ_BY_CHECKER = {"scene", "source", "decision", "final_temp", "final_tint", "preset_exp", "frame", "generation", "valid", "damping",
                   "final", "final_sha256", "final_exposure", "floor_lifted"}


def set_field(line, key, value):
    new, count = re.subn(r"(?<!\S)%s=\S*" % re.escape(key), lambda _m: "%s=%s" % (key, value), line, count=1)
    assert count == 1, (key, line)
    return new


class TableDrift(unittest.TestCase):
    """The checker's table, the tests' independent statement of the app's lines, and the app's own format strings agree."""

    def test_the_checker_table_is_the_apps_field_set_with_the_apps_types(self):
        expected = {"auto_wb": chk.AUTO_WB_FIELDS, "result": chk.RESULT_FIELDS, "dispatch": chk.DISPATCH_FIELDS,
                    "applied": chk.APPLIED_FIELDS, "dump": chk.DUMP_FIELDS}
        for event, table in expected.items():
            with self.subTest(event):
                self.assertEqual(FIELD_TYPES[event], table)
        dump = dump_line("e" * 64).replace(PREFIX, "").rstrip("\n")
        for event, line in (("auto_wb", FULL_AUTO_WB), ("result", FULL_RESULT), ("dispatch", FULL_DISPATCH), ("applied", FULL_APPLIED),
                            ("dump", dump)):
            with self.subTest("full line " + event):
                pairs = chk.FIELD.findall(line.split(" ", 1)[1])          # everything after the event name
                self.assertEqual(sorted(FIELD_TYPES[event]), sorted(key for key, _ in pairs))
                for key, value in pairs:
                    self.assertTrue(chk.field_has_type(FIELD_TYPES[event][key], value), (event, key, value))
        for type_name in set(t for table in FIELD_TYPES.values() for t in table.values()):
            with self.subTest("type " + type_name):
                self.assertIn(type_name, BAD_VALUES)
                for bad in BAD_VALUES[type_name]:
                    self.assertFalse(chk.field_has_type(type_name, bad), (type_name, bad[:20]))
                for good in GOOD_VALUES.get(type_name, []):
                    self.assertTrue(chk.field_has_type(type_name, good), (type_name, good))

    def test_the_tables_name_exactly_the_fields_in_the_apps_format_strings_and_their_precision(self):
        def literal_of(source_text, anchor):
            start = source_text.index(anchor)
            return start, source_text[start:source_text.index(")", start)]

        def keys(source_text, anchor):
            return sorted(set(re.findall(r"(\w+)=%\d+", literal_of(source_text, anchor)[1])))

        def args_of(source_text, anchor):
            """The arguments of the .arg( ... ) chain that fills the format string, balanced-paren split (adversary 0e450da2 A3)."""
            position = source_text.index(".arg(", literal_of(source_text, anchor)[0])
            args = []
            while source_text.startswith(".arg(", position):
                depth, index, quoted = 1, position + len(".arg("), False
                while depth:
                    char = source_text[index]
                    if char == '"':
                        quoted = not quoted
                    elif not quoted and char == "(":
                        depth += 1
                    elif not quoted and char == ")":
                        depth -= 1
                    index += 1
                args.append(source_text[position + len(".arg("):index - 1])
                position = index
                while source_text[position].isspace():
                    position += 1
            return args

        def precision_is_pinned(source_text, anchor, table):
            """Every key=%N of the format string is printed with the precision its table type says: 'f', 3 for fixed3, 'f', 6 for
            fixed6, and no 'f' argument at all for an integer, flag, name or size. -> how many fields were checked."""
            wanted = {"fixed3": 3, "fixed6": 6, "fixed3|NA": 3}
            args = args_of(source_text, anchor)
            pairs = re.findall(r"(\w+)=%(\d+)", literal_of(source_text, anchor)[1])
            self.assertTrue(pairs)
            for key, number in pairs:
                found = re.search(r"'f',\s*(\d+)", args[int(number) - 1])
                self.assertEqual(wanted.get(table[key]), int(found.group(1)) if found else None, (anchor, key, args[int(number) - 1]))
            return len(pairs)

        def read(*parts):
            with open(os.path.join(REPO, *parts), "r", encoding="utf-8", errors="replace") as handle:
                return handle.read()
        main_window = read("platform", "qt", "MainWindow.cpp")
        analysis = read("src", "batch", "LookAssistAnalysis.cpp")
        decision = keys(analysis, '"has_ev100=%1')
        self.assertEqual(sorted(chk.DECISION_FIELDS), decision)
        self.assertEqual(sorted(chk.AUTO_WB_FIELDS), keys(main_window, '"valid=%1 source=%2 decision=%3 damping=%4'))
        self.assertEqual(sorted(chk.RESULT_FIELDS),
                         sorted(set(keys(main_window, '"analysis=raw scene=%1')) | set(decision) | {"analysis", "grade"}))     # analysis=raw is literal; %29 is the decision fields; grade is the Film / Vision tail
        self.assertEqual(sorted(chk.DISPATCH_FIELDS), sorted(set(keys(main_window, '"generation=%1 frame=%2 scene=%3')) | {"grade"}))
        self.assertEqual(sorted(chk.APPLIED_FIELDS), keys(main_window, '"generation=%1 valid=%2 source=%3'))
        # the precision each field is printed with is what its type pins (a 'f', 6 where the table says fixed3 is a drift)
        self.assertEqual(18, precision_is_pinned(main_window, '"valid=%1 source=%2 decision=%3 damping=%4', chk.AUTO_WB_FIELDS))
        self.assertGreater(precision_is_pinned(main_window, '"analysis=raw scene=%1', chk.RESULT_FIELDS), 25)
        self.assertEqual(11, precision_is_pinned(main_window, '"generation=%1 valid=%2 source=%3', chk.APPLIED_FIELDS))
        self.assertEqual(5, precision_is_pinned(main_window, '"generation=%1 frame=%2 scene=%3', chk.DISPATCH_FIELDS))
        self.assertEqual(12, precision_is_pinned(analysis, '"has_ev100=%1', chk.DECISION_FIELDS))
        if "lookAssistPictureDumpFields" not in analysis:
            self.skipTest("this branch has not merged the picture_dump emitter (PR #793) yet; hosted CI tests the merge ref, which has it")
        self.assertEqual(sorted(chk.DUMP_FIELDS), keys(analysis, '"pre_safety=%1'))
        self.assertEqual(8, precision_is_pinned(analysis, '"pre_safety=%1', chk.DUMP_FIELDS))


class TableDrivenFieldGrammar(unittest.TestCase):
    """Sol, PR #799 r4 on 41f96076, B1 (a malformed field the checker did not read was skipped over) and B2 (--decisions-only
    never validated the completion frame): every field of every event type, every malformed value of its type, both modes."""

    @classmethod
    def setUpClass(cls):
        cls.template = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.template.cleanup)
        pictures(cls.template.name, (132, 132, 134), (140, 146, 170))
        with open(os.path.join(cls.template.name, chk.APPLIED_PICTURE), "rb") as handle:
            cls.sha = hashlib.sha256(handle.read()).hexdigest()

    def evidence(self, lines):
        """A four-arm run whose every log is PREFIX-marked `lines`, with the real pictures in place."""
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        text = "".join(PREFIX + line + "\n" for line in lines)
        for clip in chk.DEFAULT_CLIPS:
            for mode in chk.MODES:
                directory = make_run(root.name, "t", clip, mode, text, SHA)
                for picture in (chk.APPLIED_PICTURE, chk.MASTER_PICTURE):
                    os.makedirs(os.path.dirname(os.path.join(directory, picture)), exist_ok=True)
                    shutil.copyfile(os.path.join(self.template.name, picture), os.path.join(directory, picture))
        return root.name

    def flow(self, event, key=None, value=None):
        dump = dump_line(self.sha).replace(PREFIX, "").rstrip("\n")
        paired = key in ("generation", "frame")
        put = lambda line: set_field(line, key, value) if key is not None else line
        lines = {
            "auto_wb": lambda: [put(FULL_AUTO_WB), dump, FULL_RESULT],
            "result": lambda: [FULL_AUTO_WB, dump, put(FULL_RESULT)],
            "dispatch": lambda: [put(FULL_DISPATCH), dump, put(FULL_APPLIED) if paired else FULL_APPLIED],
            "applied": lambda: [put(FULL_DISPATCH) if paired else FULL_DISPATCH, dump, put(FULL_APPLIED)],
            "dump": lambda: [FULL_AUTO_WB, put(dump), FULL_RESULT],
        }
        return lines[event]()

    def test_the_full_apps_lines_bind_in_every_event_flow(self):
        for event in FIELD_TYPES:
            with self.subTest(event):
                code, text = run_checker([self.evidence(self.flow(event)), "--tag", "t"])
                self.assertEqual(0, code, text)
                self.assertIn("picture_dump-bound", text)
                self.assertEqual(0, run_checker([self.evidence(self.flow(event)), "--tag", "t", "--decisions-only"])[0])

    def test_b1_b2_every_malformed_field_of_every_event_refuses_the_arm_in_every_mode(self):
        for event, table in FIELD_TYPES.items():
            for key, type_name in table.items():
                for bad in BAD_VALUES[type_name]:
                    root = self.evidence(self.flow(event, key, bad))
                    for argv in ([], ["--decisions-only"]):      # r5 (adversary 0e450da2 A1): the dump line too, in every mode
                        with self.subTest("%s %s=%r %s" % (event, key, bad[:12] + ("...(%d)" % len(bad) if len(bad) > 12 else ""), argv)):
                            code, text = run_checker([root, "--tag", "t"] + argv)
                            self.assertEqual(1, code, text)
                            self.assertNotIn("picture_dump-bound", text)
                            self.assertNotIn("PASS", text)

    def test_every_well_typed_value_of_a_field_the_checker_does_not_read_is_accepted(self):
        for event, table in FIELD_TYPES.items():
            for key, type_name in table.items():
                if key in READ_BY_CHECKER:
                    continue
                for good in GOOD_VALUES.get(type_name, []):
                    with self.subTest("control %s %s=%s" % (event, key, good)):
                        code, text = run_checker([self.evidence(self.flow(event, key, good)), "--tag", "t"])
                        self.assertEqual(0, code, text)

    def test_adversary_0e450da2_a1_a_malformed_dump_line_refuses_the_arm_under_decisions_only_too(self):
        auto_wb = "look_assist.apply.auto_wb valid=1 source=rendered-neutral-patch decision=accepted damping=1.000"
        result = "look_assist.apply.result scene=shade preset_exp=160 final_temp=8950 final_tint=-35 frame=0"
        dump = ("look_assist.apply.picture_dump final=look-assist-final-frame0.png final_sha256=%s final_exposure=160 safety=%s frame=0"
                % ("a" * 64, "%s"))
        for bad in ("NaN", "", "other"):
            with self.subTest("safety=%r" % bad):
                code, text = run_checker([self.evidence([auto_wb, dump % bad, result]), "--tag", "t", "--decisions-only"])
                self.assertEqual(1, code, text)
                self.assertNotIn("PASS", text)
        # a dump line from an EARLIER application is as much the arm's evidence as the newest one
        stale = dump % "NaN"
        code, text = run_checker([self.evidence([auto_wb, stale, result, auto_wb, dump % "keep", result]), "--tag", "t", "--decisions-only"])
        self.assertEqual(1, code, text)
        self.assertEqual(0, run_checker([self.evidence([auto_wb, dump % "keep", result]), "--tag", "t", "--decisions-only"])[0])

    def test_adversary_0e450da2_a2_a_serial_above_the_unsigned_64_bit_maximum_is_malformed(self):
        for key, bad in (("last_serial", "18446744073709551616"), ("next_serial", "99999999999999999999")):
            with self.subTest(key):
                code, text = run_checker([self.evidence(self.flow("result", key, bad)), "--tag", "t"])
                self.assertEqual(1, code, text)
                self.assertNotIn("picture_dump-bound", text)
        code, text = run_checker([self.evidence(self.flow("result", "last_serial", "18446744073709551615")), "--tag", "t"])
        self.assertEqual(0, code, text)

    def test_b2_decisions_only_validates_the_completion_frame_of_every_application(self):
        auto_wb = "look_assist.apply.auto_wb valid=1 source=rendered-neutral-patch decision=accepted damping=1.000"
        result = "look_assist.apply.result analysis=raw scene=shade preset_exp=160 final_temp=8950 final_tint=-35 frame=%s"
        dispatch = "look_assist.apply.async_dispatch generation=3 frame=%s scene=shade floor_lifted=0"
        applied = ("look_assist.apply.auto_wb_async_applied generation=3 valid=1 source=processed-neutral-patch decision=accepted "
                   "damping=1.000 awb_temp=9990 awb_tint=-35 final_temp=9990 final_tint=-35 preset_exp=160 frame=%s")
        for bad in ("NaN", "", "1.5", "-1", "-999999999", HUGE):
            for label, lines in (("sync result", [auto_wb, result % bad]),
                                 ("async dispatch+applied (matching)", [dispatch % bad, applied % bad])):
                with self.subTest("%s frame=%r" % (label, bad[:12])):
                    code, text = run_checker([self.evidence(lines), "--tag", "t", "--decisions-only"])
                    self.assertEqual(1, code, text)
                    self.assertNotIn("PASS", text)
        for label, lines in (("sync result", [auto_wb, result % "5"]), ("async", [dispatch % "5", applied % "5"])):
            with self.subTest("control frame=5 " + label):
                self.assertEqual(0, run_checker([self.evidence(lines), "--tag", "t", "--decisions-only"])[0])


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
        self.assertEqual(0, main_legacy([RECORDED, "--tag", "rec", "--subject-sha", self.manifest["subject_sha"]]))

    def test_the_recorded_run_is_unbound_legacy_and_says_so_only_when_opted_in(self):
        # It predates the picture_dump emitter (and its pictures are downscaled copies): without the explicit opt-in it fails.
        code, text = run_checker([RECORDED, "--tag", "rec", "--subject-sha", self.manifest["subject_sha"]])
        self.assertEqual(1, code, text)
        self.assertIn("UNBOUND_LEGACY", text)
        code, text = run_checker([RECORDED, "--tag", "rec", "--subject-sha", self.manifest["subject_sha"], "--allow-unbound-legacy"])
        self.assertEqual(0, code, text)
        self.assertTrue(text.startswith("UNBOUND_LEGACY-ACCEPTED"), text)

    def test_the_recorded_run_fails_if_it_is_claimed_for_another_commit(self):
        self.assertEqual(1, main_legacy([RECORDED, "--tag", "rec", "--subject-sha", "0" * 40]))

    def test_the_gate_refuses_the_recorded_run_with_an_arm_removed(self):
        import shutil
        with tempfile.TemporaryDirectory() as root:
            for name in os.listdir(RECORDED):
                source = os.path.join(RECORDED, name)
                if os.path.isdir(source) and name != "rec-large_dual_iso-async":
                    shutil.copytree(source, os.path.join(root, name))
            self.assertEqual(1, main_legacy([root, "--tag", "rec"]))


if __name__ == "__main__":
    unittest.main()
