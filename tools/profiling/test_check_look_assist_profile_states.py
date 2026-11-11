#!/usr/bin/env python3
"""Falsifier tests for check_look_assist_profile_states.py and its helpers (stdlib only: synthetic traces and
synthetic PNGs written with zlib), plus the recorded app run the hosted CI replays."""
import contextlib
import hashlib
import io
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
                self.assertIn("STALE", text)
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
        self.assertIn("STALE", text)


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
