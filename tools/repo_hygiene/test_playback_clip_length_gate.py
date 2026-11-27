"""PLAYBACK-CLIP-LENGTH-ENFORCE-1 -- the CLASS test for the owner rule of 2026-09-30.

OWNER (repeat violation): "it did not use a long 20-30 second clip. it looped a super short clip. I
have brought this to your attention before and it keeps happening again. DURABLY FIX!"

RULE: any leg that PLAYS the app on a venue uses >= 20 s of real footage, over a window that never
exceeds the clip, and never loops. The 2026-09-22 edition of the rule lived only in prose (its
enforcement card was never built), so every new lane re-broke it. This file is the enforcement:

  1. the gate itself (tools/profiling/gui-smoke-length-gate.ps1) refuses a short / empty / unreadable
     clip, typed and fail-closed, and accepts a ~30 s one -- header check only, no app launch;
  2. the runner (the one choke point every venue playback leg goes through) applies it, never passes
     --loop by default, and refuses -AllowLoop for anything that could be playback evidence;
  3. the runtime backstop: the app prints wrapped/total_frames/clip_seconds and the runner turns a
     wrap into INVALID_LOOPED;
  4. the job generator passes the play window through (no hard-coded -Seconds 40) and refuses a
     fixture at GENERATION time;
  5. a scan of tools/** fails if ANY script launches MLVApp playback without the gate or an explicit,
     commented allowlist entry -- so a NEW launcher cannot silently re-open the hole.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from tools.repo_hygiene.synthetic_mlv import (
    FRAMES_30S_AT_23976,
    FRAMES_SHORT_FIXTURE,
    MLV_EXTENSION,
    block,
    mlv_bytes_with_random_vidf_payload,
    mlvi_header,
    write_header_only_mlv,
    write_synthetic_mlv,
    write_synthetic_mlv_with_blocks,
)

ROOT = Path(__file__).resolve().parents[2]
PROFILING = ROOT / "tools" / "profiling"
GATE = PROFILING / "gui-smoke-length-gate.ps1"
RUNNER = PROFILING / "run-release-gui-smoke.ps1"
JOB_GENERATOR = PROFILING / "bachelor" / "playback-attr-3-cuda-job.ps1"
MAIN_WINDOW = ROOT / "platform" / "qt" / "MainWindow.cpp"
FIXTURES = ROOT / "tests" / "fixtures" / "clips"
TRACKED_LARGE_FIXTURE = FIXTURES / ("large_dual_iso" + MLV_EXTENSION)

PWSH = shutil.which("pwsh")
requires_pwsh = unittest.skipIf(PWSH is None, "pwsh is not on PATH")
requires_windows = unittest.skipUnless(
    os.name == "nt", "the runner's dry run resolves a Windows exe and its build stamp")

EXIT_TOO_SHORT = 41
EXIT_UNKNOWN = 42
EXIT_PASS_THROUGH = 44


def _q(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _pwsh(script: str, *, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, timeout=timeout,
    )


def _gate(path: Path, window: float, start_frame: int = 0) -> dict:
    """Runs Test-GuiSmokeClipLength on one clip and returns its verdict object."""
    proc = _pwsh(
        f". {_q(GATE)}; "
        f"Test-GuiSmokeClipLength -Path {_q(path)} -WindowSeconds {window} -StartFrame {start_frame} "
        "| ConvertTo-Json -Compress"
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout)


class _TmpCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="clip-length-gate-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(os.path.realpath(self._tmp.name))

    def clip(self, name: str, frames: int, **kwargs) -> Path:
        return write_synthetic_mlv(self.tmp / (name + MLV_EXTENSION), frames, **kwargs)


@requires_pwsh
class ClipLengthGateTests(_TmpCase):
    """1. The gate: header only, typed, fail closed."""

    def test_a_16_frame_clip_is_refused_too_short(self) -> None:
        verdict = _gate(self.clip("sixteen", FRAMES_SHORT_FIXTURE), 24)
        self.assertEqual(verdict["verdict"], "CLIP_TOO_SHORT", verdict)
        self.assertRegex(verdict["message"], r"^CLIP_TOO_SHORT \(clip=0\.667 window=24\)$")

    def test_a_zero_frame_clip_is_length_unknown_not_a_pass(self) -> None:
        verdict = _gate(self.clip("zero", 0), 24)
        self.assertEqual(verdict["verdict"], "CLIP_LENGTH_UNKNOWN", verdict)
        self.assertEqual(verdict["message"], "CLIP_LENGTH_UNKNOWN (reason=zero_frames)")

    def test_a_720_frame_clip_at_23976_is_accepted(self) -> None:
        verdict = _gate(self.clip("thirty", FRAMES_30S_AT_23976), 24)
        self.assertEqual(verdict["verdict"], "OK", verdict)
        self.assertAlmostEqual(verdict["clipSeconds"], 30.03, places=2)

    def test_the_20_second_floor_is_exact(self) -> None:
        # 479 frames / 23.976 = 19.98 s (refused); 480 frames = 20.02 s (accepted).
        self.assertEqual(_gate(self.clip("f479", 479), 20)["verdict"], "CLIP_TOO_SHORT")
        self.assertEqual(_gate(self.clip("f480", 480), 20)["verdict"], "OK")

    def test_the_window_may_not_exceed_the_clip(self) -> None:
        # 600 frames = 25.03 s: fine for a 24 s window, refused for the old hard-coded 40 s window.
        clip = self.clip("twentyfive", 600)
        self.assertEqual(_gate(clip, 24)["verdict"], "OK")
        refused = _gate(clip, 40)
        self.assertEqual(refused["verdict"], "CLIP_TOO_SHORT", refused)
        self.assertRegex(refused["message"], r"^CLIP_TOO_SHORT \(clip=25.025 window=40\)$")

    def test_the_window_is_measured_from_the_start_frame(self) -> None:
        # 25.03 s clip, start at frame 240 (10.01 s in): 15.02 s remain, a 24 s window overruns.
        clip = self.clip("startframe", 600)
        self.assertEqual(_gate(clip, 24, start_frame=0)["verdict"], "OK")
        self.assertEqual(_gate(clip, 24, start_frame=240)["verdict"], "CLIP_TOO_SHORT")

    def test_an_unreadable_header_fails_closed(self) -> None:
        cases = {
            "badmagic": (mlvi_header(720, magic=b"NOPE"), "bad_magic"),
            "truncated": (mlvi_header(720)[:40], "header_truncated"),
            "empty": (b"", "header_truncated"),
            # header validity precedes the block walk: a zero-fps header alone reports bad_fps, not header_vidf_count_mismatch
            "zero_fps": (mlvi_header(720, fps_denom=0), "bad_fps"),
            "zero_fps_with_blocks": (mlvi_header(720, fps_denom=0) + b"".join(block(b"VIDF") for _ in range(720)), "bad_fps"),
        }
        for name, (payload, reason) in cases.items():
            with self.subTest(name):
                path = self.tmp / (name + MLV_EXTENSION)
                path.write_bytes(payload)
                verdict = _gate(path, 24)
                self.assertEqual(verdict["verdict"], "CLIP_LENGTH_UNKNOWN", verdict)
                self.assertEqual(verdict["message"], f"CLIP_LENGTH_UNKNOWN (reason={reason})")

    def test_a_missing_file_fails_closed(self) -> None:
        verdict = _gate(self.tmp / ("absent" + MLV_EXTENSION), 24)
        self.assertEqual(verdict["verdict"], "CLIP_LENGTH_UNKNOWN", verdict)

    def test_a_spanned_set_is_summed_and_must_be_complete(self) -> None:
        guid = 0xABCDEF0123456789
        first = write_synthetic_mlv(self.tmp / ("span" + MLV_EXTENSION), 400, file_num=0, file_count=2, guid=guid)
        # Only one of two parts on disk: the length is UNKNOWN, never "400 frames = too short".
        incomplete = _gate(first, 24)
        self.assertEqual(incomplete["verdict"], "CLIP_LENGTH_UNKNOWN", incomplete)
        self.assertEqual(incomplete["message"], "CLIP_LENGTH_UNKNOWN (reason=spanned_set_incomplete)")
        # Both parts: 400 + 400 = 800 frames = 33.37 s. (The part is named exactly as the app probes it, the first part's own
        # case, so this holds on the case-sensitive ubuntu shard too: GUI-SMOKE-OVERRUN-ORACLE-SPANNED-COUNT-1, adversary A1.)
        write_synthetic_mlv(self.tmp / ("span" + self._probed(0)), 400, file_num=1, file_count=2, guid=guid)
        complete = _gate(first, 24)
        self.assertEqual(complete["verdict"], "OK", complete)
        self.assertAlmostEqual(complete["clipSeconds"], 33.37, places=2)

    def test_a_spanned_set_from_two_recordings_is_unknown(self) -> None:
        first = write_synthetic_mlv(self.tmp / ("mix" + MLV_EXTENSION), 400, file_num=0, file_count=2, guid=1)
        write_synthetic_mlv(self.tmp / ("mix" + self._probed(0)), 400, file_num=1, file_count=2, guid=2)
        verdict = _gate(first, 24)
        self.assertEqual(verdict["message"], "CLIP_LENGTH_UNKNOWN (reason=spanned_set_mismatch)")

    # GUI-SMOKE-LENGTH-GATE-VIDF-COUNT-1: the app plays the VIDF blocks it indexes (src/mlv/video_mlv.c, the
    # `video_frames++` of the block walk, assigned to video->frames and read back by getMlvFrames), not the MLVI
    # header's videoFrameCount. M16-1243's headers declared 612 frames while 937 VIDF blocks were indexed.
    def test_the_vidf_block_count_outranks_an_understating_header(self) -> None:
        # Header says 400 (16.7 s: too short); the file really holds 720 VIDF blocks (30.03 s, what the app plays).
        path = write_synthetic_mlv_with_blocks(self.tmp / ("understated" + MLV_EXTENSION), 400, FRAMES_30S_AT_23976)
        verdict = _gate(path, 24)
        self.assertEqual(verdict["verdict"], "OK", verdict)
        self.assertEqual(verdict["frames"], FRAMES_30S_AT_23976, verdict)
        self.assertAlmostEqual(verdict["clipSeconds"], 30.03, places=2)

    def test_the_start_frame_cap_follows_the_vidf_count_not_the_header(self) -> None:
        path = write_synthetic_mlv_with_blocks(self.tmp / ("startcap" + MLV_EXTENSION), 400, FRAMES_30S_AT_23976)
        # 600 frames in leaves 120 frames = 5.0 s, under the 24 s window; 120 frames in leaves 25.0 s.
        self.assertEqual(_gate(path, 24, start_frame=600)["verdict"], "CLIP_TOO_SHORT")
        self.assertEqual(_gate(path, 24, start_frame=120)["verdict"], "OK")

    def test_a_header_that_overstates_is_corrected_down_to_the_vidf_count(self) -> None:
        path = write_synthetic_mlv_with_blocks(self.tmp / ("overstated" + MLV_EXTENSION), 720, 100)
        verdict = _gate(path, 24)
        self.assertEqual(verdict["verdict"], "CLIP_TOO_SHORT", verdict)
        self.assertEqual(verdict["frames"], 100, verdict)

    def test_a_spanned_set_sums_the_vidf_blocks_of_every_part(self) -> None:
        guid = 0x1122334455667788
        first = write_synthetic_mlv_with_blocks(self.tmp / ("vspan" + MLV_EXTENSION), 200, 400,
                                                file_num=0, file_count=3, guid=guid)
        write_synthetic_mlv_with_blocks(self.tmp / ("vspan" + self._probed(0)), 200, 300, file_num=1, file_count=3, guid=guid)
        write_synthetic_mlv_with_blocks(self.tmp / ("vspan" + self._probed(1)), 212, 237, file_num=2, file_count=3, guid=guid)
        verdict = _gate(first, 24)
        self.assertEqual(verdict["frames"], 937, verdict)
        self.assertAlmostEqual(verdict["clipSeconds"], 39.08, places=2)

    # GUI-SMOKE-OVERRUN-ORACLE-SPANNED-COUNT-1 (d2): the app opens the first part, then probes <base>.M00, .M01 ... by NAME
    # until one is missing (src/mlv/video_mlv.c load_all_chunks); it reads no fileNum / fileCount. Bachelor M16-1457's first
    # part declares fileCount <= 1, so the header-led gate counted 1671 frames where the app plays 2859.
    def _span(self, name: str, first_count: int, parts: dict, guid: int = 0x5A5A0001) -> Path:
        """The first part (400 VIDF, file_count=first_count) plus each `parts` suffix -> (file_num, file_count, guid)."""
        first = write_synthetic_mlv(self.tmp / (name + MLV_EXTENSION), 400, file_num=0, file_count=first_count, guid=guid)
        for suffix, (num, count, part_guid) in parts.items():
            write_synthetic_mlv(self.tmp / (name + suffix), 400, file_num=num, file_count=count, guid=part_guid)
        return first

    # The name the app probes for part n: the first part's path with its LAST TWO characters replaced by the two-digit
    # number (load_all_chunks: strcpy(&filename[strlen(filename) - 2], seq_name)), so it keeps the first part's own case.
    # MLV_EXTENSION is lower case, so the probed names here end ".m00", ".m01" ... and hold on a case-sensitive file system.
    @staticmethod
    def _probed(n: int) -> str:
        return MLV_EXTENSION[:-2] + f"{n:02d}"

    def _case_insensitive_tmp(self) -> bool:
        probe = self.tmp / "case-probe-lower"
        probe.write_bytes(b"")
        try:
            return (self.tmp / "CASE-PROBE-LOWER").exists()
        finally:
            probe.unlink()

    def test_g1_an_under_declaring_first_part_still_counts_the_probed_m00(self) -> None:
        verdict = _gate(self._span("g1", 1, {self._probed(0): (1, 1, 0x5A5A0001)}), 24)
        self.assertEqual(verdict["verdict"], "OK", verdict)
        self.assertEqual(verdict["frames"], 800, verdict)

    def test_g2_a_zero_file_count_header_still_counts_the_probed_m00(self) -> None:
        verdict = _gate(self._span("g2", 0, {self._probed(0): (1, 0, 0x5A5A0001)}), 24)
        self.assertEqual(verdict["frames"], 800, verdict)

    def test_g3_a_probed_part_from_another_recording_fails_closed(self) -> None:
        verdict = _gate(self._span("g3", 1, {self._probed(0): (1, 1, 0x5A5A0002)}), 24)
        self.assertEqual(verdict["verdict"], "CLIP_LENGTH_UNKNOWN", verdict)
        self.assertEqual(verdict["message"], "CLIP_LENGTH_UNKNOWN (reason=spanned_set_mismatch)")

    def test_g4_a_gap_ends_the_probed_set_as_it_does_in_the_app(self) -> None:
        verdict = _gate(self._span("g4", 1, {self._probed(0): (1, 1, 0x5A5A0001), self._probed(2): (3, 1, 0x5A5A0001)}), 24)
        self.assertEqual(verdict["frames"], 800, verdict)

    def test_g5_a_sibling_in_the_other_case_counts_only_where_the_app_can_open_it(self) -> None:
        # adversary e8eeaee1 A1: the app opens the probed NAME, so a sibling whose extension differs in case from the first
        # part's is a part only on a case-insensitive volume (the venues' NTFS). On a case-sensitive one (the ubuntu CI shard)
        # fopen misses it and the app plays the first part alone.
        other_case = self._probed(0).swapcase()
        verdict = _gate(self._span("g5", 1, {other_case: (1, 1, 0x5A5A0001)}), 24)
        self.assertEqual(verdict["frames"], 800 if self._case_insensitive_tmp() else 400, verdict)

    def test_g7_a_sibling_whose_base_differs_in_case_counts_only_where_the_app_can_open_it(self) -> None:
        # The same rule from the base-name side: on a case-insensitive volume fopen("g7" + probed) opens "G7" + probed, so the
        # app plays it and the count must include it; on a case-sensitive volume it does not exist for the app.
        first = write_synthetic_mlv(self.tmp / ("g7" + MLV_EXTENSION), 400, file_num=0, file_count=1, guid=0x5A5A0001)
        write_synthetic_mlv(self.tmp / ("G7" + self._probed(0)), 400, file_num=1, file_count=1, guid=0x5A5A0001)
        verdict = _gate(first, 24)
        self.assertEqual(verdict["frames"], 800 if self._case_insensitive_tmp() else 400, verdict)

    def test_g6_a_declared_three_part_set_with_a_gap_is_incomplete(self) -> None:
        verdict = _gate(self._span("g6", 3, {self._probed(0): (1, 3, 0x5A5A0001), self._probed(2): (2, 3, 0x5A5A0001)}), 24)
        self.assertEqual(verdict["verdict"], "CLIP_LENGTH_UNKNOWN", verdict)
        self.assertEqual(verdict["message"], "CLIP_LENGTH_UNKNOWN (reason=spanned_set_incomplete)")

    # adversary f408901c A1: a header that DECLARES the set is followed by the same probed names (the app reads no fileCount),
    # so its parts are found exactly as the app finds them: by the probed name, as the file system resolves it.
    def test_g8_a_declared_part_whose_base_differs_in_case_counts_only_where_the_app_can_open_it(self) -> None:
        first = write_synthetic_mlv(self.tmp / ("g8" + MLV_EXTENSION), 400, file_num=0, file_count=2, guid=0x5A5A0001)
        write_synthetic_mlv(self.tmp / ("G8" + self._probed(0)), 400, file_num=1, file_count=2, guid=0x5A5A0001)
        verdict = _gate(first, 24)
        if self._case_insensitive_tmp():
            self.assertEqual(verdict["frames"], 800, verdict)
        else:
            self.assertEqual(verdict["message"], "CLIP_LENGTH_UNKNOWN (reason=spanned_set_incomplete)")

    def test_g9_a_declared_part_in_the_other_case_counts_only_where_the_app_can_open_it(self) -> None:
        verdict = _gate(self._span("g9", 2, {self._probed(0).swapcase(): (1, 2, 0x5A5A0001)}), 24)
        if self._case_insensitive_tmp():
            self.assertEqual(verdict["frames"], 800, verdict)
        else:
            self.assertEqual(verdict["message"], "CLIP_LENGTH_UNKNOWN (reason=spanned_set_incomplete)")

    # GUI-SMOKE-OVERRUN-ORACLE-SPANNED-COUNT-1 r4 (adversary ff851a0b A1/A2). A case-sensitive directory cannot be made without
    # elevation, so the parity decision is a pure function over the directory's entry names and is driven with the listings
    # such a directory would give.
    def _binding_matches(self, gate_parts: list, bound_parts: list, entries: list) -> bool:
        def arr(items: list) -> str:
            return "@(" + ",".join(_q(item) for item in items) + ")" if items else "@()"
        proc = _pwsh(f". {_q(GATE)}; Test-GuiSmokeBindingMatchesGateParts -GateParts {arr(gate_parts)} "
                     f"-BoundParts {arr(bound_parts)} -DirectoryEntries {arr(entries)}")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc.stdout.strip() == "True"

    def test_r4_a1_the_binding_must_be_the_files_the_gate_counted_not_a_case_variant_of_them(self) -> None:
        d = str(self.tmp) + os.sep
        mlv, hidden_m00, other_m00 = d + "clip.MLV", d + "clip.M00", d + "clip.m00"
        # A case-sensitive directory holding the counted (hidden) clip.M00 AND a visible, different clip.m00: the binding
        # lists the visible one. Equal counts and equal case-folded paths, yet not the file the gate counted.
        self.assertFalse(self._binding_matches([mlv, hidden_m00], [mlv, other_m00], ["clip.MLV", "clip.M00", "clip.m00"]))
        # The binding that lists the counted file passes, even though a case variant sits beside it.
        self.assertTrue(self._binding_matches([mlv, hidden_m00], [mlv, hidden_m00], ["clip.MLV", "clip.M00", "clip.m00"]))
        # A case-insensitive volume: one entry, resolved from another spelling of its name, is that file.
        self.assertTrue(self._binding_matches([mlv, d + "clip.m00"], [mlv, hidden_m00], ["clip.MLV", "clip.M00"]))
        # Two case variants and neither is the spelling the gate used: ambiguous, so refused.
        self.assertFalse(self._binding_matches([d + "clip.mlv"], [d + "clip.MLV"], ["clip.MLV", "clip.Mlv"]))
        # Counts differ (the G8 case: the gate counts a part the binding never lists), and an empty binding.
        self.assertFalse(self._binding_matches([mlv, hidden_m00], [mlv], ["clip.MLV", "clip.M00"]))
        self.assertFalse(self._binding_matches([mlv], [], ["clip.MLV"]))
        # A name the directory does not hold is not a counted file.
        self.assertFalse(self._binding_matches([mlv], [mlv], []))

    def test_r4_a2_the_gates_parts_are_full_paths_even_for_a_relative_input(self) -> None:
        clip = write_synthetic_mlv(self.tmp / ("rel" + MLV_EXTENSION), 400, file_num=0, file_count=1, guid=0x5A5A0001)
        proc = subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
             f". {_q(GATE)}; (Get-GuiSmokeClipLength -Path ('.' + [IO.Path]::DirectorySeparatorChar + {_q(clip.name)})).parts "
             "| ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=120, cwd=str(self.tmp))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        parts = json.loads(proc.stdout)
        parts = parts if isinstance(parts, list) else [parts]
        self.assertEqual([p.lower() for p in parts], [str(clip).lower()])

    def test_other_blocks_are_skipped_and_only_vidf_is_counted(self) -> None:
        path = self.tmp / ("mixed" + MLV_EXTENSION)
        body = b"".join(block(b"VIDF") + block(b"AUDF") + block(b"NULL", 40) for _ in range(30))
        write_synthetic_mlv_with_blocks(path, 5, 0, extra=body)
        verdict = _gate(path, 1, start_frame=0)
        self.assertEqual(verdict["frames"], 30, verdict)

    def test_a_clip_with_blocks_but_no_vidf_is_a_typed_mismatch_not_the_header_count(self) -> None:
        path = write_synthetic_mlv_with_blocks(self.tmp / ("novidf" + MLV_EXTENSION), 720, 0, extra=block(b"AUDF"))
        verdict = _gate(path, 24)
        self.assertEqual(verdict["verdict"], "CLIP_LENGTH_UNKNOWN", verdict)
        self.assertEqual(verdict["message"], "CLIP_LENGTH_UNKNOWN (reason=header_vidf_count_mismatch)")

    # r4 (sol r3 BLOCKER): the count-or-fail-closed rule has no header-only exception. A file cut off right after its
    # MLVI header holds zero playable VIDF blocks (src/mlv/video_mlv.c rejects zero indexed frames), so the declared
    # count is never admitted in its place.
    def test_a_header_only_clip_declaring_720_frames_is_length_unknown_never_ok(self) -> None:
        path = write_header_only_mlv(self.tmp / ("headeronly" + MLV_EXTENSION), FRAMES_30S_AT_23976)
        self.assertEqual(path.stat().st_size, 52)
        verdict = _gate(path, 24)
        self.assertEqual(verdict["verdict"], "CLIP_LENGTH_UNKNOWN", verdict)
        self.assertEqual(verdict["message"], "CLIP_LENGTH_UNKNOWN (reason=header_vidf_count_mismatch)")

    def test_an_mlvi_block_that_skips_beyond_eof_is_length_unknown_never_ok(self) -> None:
        # The MLVI block declares 1 MB in a 52-byte file: the walk skips past EOF having seen no VIDF at all.
        path = self.tmp / ("oversizedmlvi" + MLV_EXTENSION)
        path.write_bytes(mlvi_header(FRAMES_30S_AT_23976, block_size=1_000_000))
        verdict = _gate(path, 24)
        self.assertEqual(verdict["verdict"], "CLIP_LENGTH_UNKNOWN", verdict)
        self.assertEqual(verdict["message"], "CLIP_LENGTH_UNKNOWN (reason=header_vidf_count_mismatch)")
        # ... and with 720 real VIDF blocks behind it: an MLVI block that swallows them is still not a clip.
        path.write_bytes(mlvi_header(FRAMES_30S_AT_23976, block_size=1_000_000) + b"".join(
            block(b"VIDF") for _ in range(FRAMES_30S_AT_23976)))
        verdict = _gate(path, 24)
        self.assertEqual(verdict["verdict"], "CLIP_LENGTH_UNKNOWN", verdict)

    def test_a_trailing_vidf_block_that_runs_past_eof_fails_closed(self) -> None:
        # 720 whole VIDF blocks, then one whose declared size reaches past the end of the file.
        path = write_synthetic_mlv_with_blocks(self.tmp / ("overrun" + MLV_EXTENSION), FRAMES_30S_AT_23976,
                                               FRAMES_30S_AT_23976, extra=block(b"VIDF", size=1_000_000))
        verdict = _gate(path, 24)
        self.assertEqual(verdict["verdict"], "CLIP_LENGTH_UNKNOWN", verdict)
        self.assertEqual(verdict["message"], "CLIP_LENGTH_UNKNOWN (reason=vidf_count_unverified)")

    def test_a_zero_frame_header_with_no_blocks_is_still_zero_frames(self) -> None:
        verdict = _gate(write_header_only_mlv(self.tmp / ("emptyheader" + MLV_EXTENSION), 0), 24)
        self.assertEqual(verdict["message"], "CLIP_LENGTH_UNKNOWN (reason=zero_frames)")

    def test_a_block_walk_that_cannot_finish_fails_closed_and_typed(self) -> None:
        # blockSize 8 is below the 16-byte block header: the app refuses such a file (MLV_ERR_INVALID).
        path = write_synthetic_mlv_with_blocks(self.tmp / ("badsize" + MLV_EXTENSION), 720, 10,
                                               extra=block(b"VIDF", size=8))
        verdict = _gate(path, 24)
        self.assertEqual(verdict["verdict"], "CLIP_LENGTH_UNKNOWN", verdict)
        self.assertEqual(verdict["message"], "CLIP_LENGTH_UNKNOWN (reason=vidf_count_unverified)")

    def test_a_header_in_front_of_a_random_body_is_refused_as_a_vidf_mismatch(self) -> None:
        # The shape test_attr3_owner_clip_stage_stall used before this card: a genuine header, then random bytes.
        path = self.tmp / ("randombody" + MLV_EXTENSION)
        path.write_bytes(mlvi_header(FRAMES_30S_AT_23976) + os.urandom(700_000 - 52))
        verdict = _gate(path, 24)
        self.assertEqual(verdict["verdict"], "CLIP_LENGTH_UNKNOWN", verdict)
        self.assertEqual(verdict["message"], "CLIP_LENGTH_UNKNOWN (reason=header_vidf_count_mismatch)")

    def test_a_random_payload_clip_of_a_fixed_size_still_counts_its_frames(self) -> None:
        path = self.tmp / ("randompayload" + MLV_EXTENSION)
        data = mlv_bytes_with_random_vidf_payload(FRAMES_30S_AT_23976, 700_000)
        path.write_bytes(data)
        self.assertEqual(len(data), 700_000)
        verdict = _gate(path, 24)
        self.assertEqual(verdict["verdict"], "OK", verdict)
        self.assertEqual(verdict["frames"], FRAMES_30S_AT_23976, verdict)

    def test_the_block_walk_is_bounded(self) -> None:
        path = write_synthetic_mlv_with_blocks(self.tmp / ("bounded" + MLV_EXTENSION), 720, 50)
        proc = _pwsh(
            f". {_q(GATE)}; $script:GuiSmokeVidfWalkMaxBlocks = 10; "
            f"Get-GuiSmokeClipLength -Path {_q(path)} | ConvertTo-Json -Compress"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads(proc.stdout)
        self.assertFalse(result["known"], result)
        self.assertEqual(result["reason"], "vidf_count_unverified", result)

    def test_messages_never_name_the_clip_path(self) -> None:
        verdict = _gate(self.clip("distinctive_name_xyz", 16), 24)
        self.assertNotIn("distinctive_name_xyz", json.dumps(verdict["message"]))
        self.assertNotIn(str(self.tmp), json.dumps(verdict["message"]))

    def test_both_tracked_fixtures_are_refused(self) -> None:
        for stem in ("tiny_dual_iso", "large_dual_iso"):
            with self.subTest(stem):
                fixture = FIXTURES / (stem + MLV_EXTENSION)
                self.assertTrue(fixture.is_file(), "tracked fixture missing")
                self.assertEqual(_gate(fixture, 24)["verdict"], "CLIP_TOO_SHORT")


    # ---- ENFORCE-2: the PLAY WINDOW of an evidence run is >= 20 s too -------------------------------------

    def test_a_play_window_under_20_seconds_is_refused_even_on_a_long_clip(self) -> None:
        thirty = self.clip("thirty", FRAMES_30S_AT_23976)
        verdict = _gate(thirty, 10)
        self.assertEqual(verdict["verdict"], "PLAY_WINDOW_TOO_SHORT", verdict)
        self.assertEqual(verdict["message"], "PLAY_WINDOW_TOO_SHORT (window=10 required=20)")
        self.assertEqual(_gate(thirty, 19.5)["verdict"], "PLAY_WINDOW_TOO_SHORT")
        self.assertEqual(_gate(thirty, 20)["verdict"], "OK")
        self.assertEqual(_gate(thirty, 24)["verdict"], "OK")

    # ---- PLAYBACK-CLIP-LENGTH-HFR-10S-1 (owner amendment 2026-10-15): HFR (>= 50 fps native) clips need 10 s ------------
    # "Amend rule so that hfr 60fps clips are allowed to be 10 seconds". The floor is Get-GuiSmokeMinPlaySeconds of the
    # clip's HEADER fps; every other clip, and an unknown fps, keeps 20 s.

    def test_hfr_pins_from_the_clip_header(self) -> None:
        hfr_5994 = self.clip("hfr5994", 720, fps_nom=60000, fps_denom=1001)     # 12.01 s
        self.assertEqual(_gate(hfr_5994, 10)["verdict"], "OK")                      # 59.94 fps 10 s ACCEPTED
        refused = _gate(hfr_5994, 9.9)                                              # 59.94 fps 9.9 s REFUSED
        self.assertEqual(refused["verdict"], "PLAY_WINDOW_TOO_SHORT", refused)
        self.assertEqual(refused["message"], "PLAY_WINDOW_TOO_SHORT (window=9.9 required=10)")
        # a 59.94 clip under 10 s of footage cannot be played (or looped) to 10 s
        self.assertEqual(_gate(self.clip("hfr599", 599, fps_nom=60000, fps_denom=1001), 10)["verdict"], "CLIP_TOO_SHORT")
        thirty = self.clip("thirty", FRAMES_30S_AT_23976)
        refused = _gate(thirty, 10)                                                 # 23.976 fps 10 s REFUSED
        self.assertEqual(refused["message"], "PLAY_WINDOW_TOO_SHORT (window=10 required=20)")
        self.assertEqual(_gate(thirty, 20)["verdict"], "OK")                        # 23.976 fps 20 s ACCEPTED
        self.assertEqual(_gate(self.clip("fifty", 600, fps_nom=50, fps_denom=1), 10)["verdict"], "OK")   # 50 fps 10 s ACCEPTED
        # a 12 s 29.97 clip stays under its 20 s floor however short the window
        self.assertEqual(_gate(self.clip("ntsc", 360, fps_nom=30000, fps_denom=1001), 20)["verdict"], "CLIP_TOO_SHORT")
        # unknown fps: the length cannot be read, fail closed (never the 10 s floor)
        self.assertEqual(_gate(self.clip("nofps", 720, fps_nom=60000, fps_denom=0), 10)["verdict"], "CLIP_LENGTH_UNKNOWN")

    def test_the_floor_derivation_table(self) -> None:
        proc = _pwsh(
            f". {_q(GATE)}; "
            "@(59.94, 60, 50, 120, 49.999, 29.97, 23.976, 0, -60, 'NaN', 'Infinity', 'abc', $null, $true) | "
            "ForEach-Object { Get-GuiSmokeMinPlaySeconds -NativeFps $_ }; "
            "Get-GuiSmokeMinPlaySeconds; Get-GuiSmokeMinPlayWindowMs -NativeFps 59.94; Get-GuiSmokeMinPlayWindowMs -NativeFps 23.976; "
            "Get-GuiSmokeMinPresentedFrames -Fps 59.94; Get-GuiSmokeMinPresentedFrames -Fps 23.976")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.split(),
                         ["10", "10", "10", "10", "20", "20", "20", "20", "20", "20", "20", "20", "20", "20", "20",
                          "10000", "20000", "600", "480"])

    def test_clip_only_is_the_explicit_opt_out_for_callers_that_play_nothing(self) -> None:
        def gate(path: Path, extra: str) -> dict:
            proc = _pwsh(f". {_q(GATE)}; Test-GuiSmokeClipLength -Path {_q(path)} -WindowSeconds 0 {extra} | ConvertTo-Json -Compress")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            return json.loads(proc.stdout)
        thirty = self.clip("thirty", FRAMES_30S_AT_23976)
        self.assertEqual(gate(thirty, "")["verdict"], "PLAY_WINDOW_TOO_SHORT")     # window 0 is a window under the floor
        self.assertEqual(gate(thirty, "-ClipOnly")["verdict"], "OK")
        self.assertEqual(gate(self.clip("sixteen", FRAMES_SHORT_FIXTURE), "-ClipOnly")["verdict"], "CLIP_TOO_SHORT")

    def test_a_conformed_high_frame_rate_clip_is_gated_in_source_seconds_at_its_native_fps(self) -> None:
        """HUB RULING 2026-10-30T22:17:51Z (HFR-10S-1's unit, docs/playback-clip-length-rule.md): a play window is SOURCE seconds at the clip's NATIVE
        fps, conformed or not -- a 24 fps conform changes the pace, never the footage the window must cover. 969 frames at 59.94 fps (M16-1456's
        shape) are 16.17 s, so a 25 s window is refused pre-launch; 1667 frames (M16-1457's) are 27.8 s and pass it."""
        hfr = {"fps_nom": 59940, "fps_denom": 1000}
        short, long_ = self.clip("hfr969", 969, **hfr), self.clip("hfr1667", 1667, **hfr)
        refused = _gate(short, 25)
        self.assertEqual(refused["verdict"], "CLIP_TOO_SHORT", refused)
        self.assertAlmostEqual(refused["clipSeconds"], 969 / 59.94, places=2)
        self.assertAlmostEqual(refused["fps"], 59.94, places=3)
        self.assertEqual(_gate(short, 16)["verdict"], "OK")                          # 16 s of the 16.17 s
        self.assertEqual(_gate(short, 17)["verdict"], "CLIP_TOO_SHORT")
        self.assertEqual(_gate(long_, 25)["verdict"], "OK")
        self.assertEqual(_gate(long_, 25, start_frame=168)["verdict"], "OK")         # (1667 - 168) / 59.94 = 25.008 s remain
        self.assertEqual(_gate(long_, 25, start_frame=169)["verdict"], "CLIP_TOO_SHORT")   # 24.99 s remain
        # the HFR floor is 10 s of SOURCE: 599 frames = 9.99 s, 600 frames = 10.01 s
        self.assertEqual(_gate(self.clip("hfr599", 599, **hfr), 10)["verdict"], "CLIP_TOO_SHORT")
        self.assertEqual(_gate(self.clip("hfr600", 600, **hfr), 10)["verdict"], "OK")

    def test_the_length_gate_takes_no_conform_input(self) -> None:
        """Source seconds do not depend on the playback rate, so conform never reaches the pre-launch gate: Test-GuiSmokeClipLength has no
        -ConformEnvOverride, there is no playback-rate mirror, and the runner passes none (adversary A3's MLVAPP_PLAYBACK_CONFORM parse went with it)."""
        proc = _pwsh(f". {_q(GATE)}; (Get-Command Test-GuiSmokeClipLength).Parameters.ContainsKey('ConformEnvOverride'); "
                     "[bool](Get-Command Get-GuiSmokePlaybackFps -ErrorAction SilentlyContinue); "
                     "[bool](Get-Command Get-GuiSmokeConformEnvOverride -ErrorAction SilentlyContinue)")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.split(), ["False", "False", "False"])
        runner = (ROOT / "tools" / "profiling" / "run-release-gui-smoke.ps1").read_text(encoding="utf-8")
        self.assertNotIn("ConformEnvOverride", runner)

    def test_a_presented_frames_target_on_a_high_frame_rate_clip_counts_source_frames_at_its_native_fps(self) -> None:
        clip = self.clip("hfr1667", 1667, fps_nom=59940, fps_denom=1000)
        def gate(frames: int) -> dict:
            proc = _pwsh(f". {_q(GATE)}; Test-GuiSmokeClipLength -Path {_q(clip)} -WindowSeconds 25 -TargetPresentedFrames {frames} | ConvertTo-Json -Compress")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            return json.loads(proc.stdout)
        self.assertEqual(gate(599)["verdict"], "PLAY_WINDOW_TOO_SHORT")   # 599 / 59.94 = 9.99 s of source
        self.assertEqual(gate(600)["verdict"], "OK")                      # the HFR floor: ceil(10 x 59.94) = 600

    def test_a_presented_frames_target_is_a_play_window_and_must_reach_the_floor(self) -> None:
        def gate(frames: int) -> dict:
            proc = _pwsh(
                f". {_q(GATE)}; Test-GuiSmokeClipLength -Path {_q(self.clip('thirty', FRAMES_30S_AT_23976))} "
                f"-WindowSeconds 25 -TargetPresentedFrames {frames} | ConvertTo-Json -Compress")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            return json.loads(proc.stdout)
        refused = gate(24)   # the old capture-reference-frame default: a ~1 s play
        self.assertEqual(refused["verdict"], "PLAY_WINDOW_TOO_SHORT", refused)
        self.assertIn("presented_frames=24", refused["message"])
        self.assertEqual(gate(479)["verdict"], "PLAY_WINDOW_TOO_SHORT")
        self.assertEqual(gate(480)["verdict"], "OK")       # 480 / 23.976 = 20.02 s
        self.assertEqual(gate(0)["verdict"], "OK")         # no early stop

    def test_the_exit_code_and_refusal_reason_maps(self) -> None:
        proc = _pwsh(
            f". {_q(GATE)}; "
            "@('CLIP_TOO_SHORT','PLAY_WINDOW_TOO_SHORT','CLIP_LENGTH_UNKNOWN' | ForEach-Object { Get-GuiSmokeGateExitCode -Verdict $_ }) -join ','; "
            "(Get-GuiSmokeRefusalReason -ExitCode 41 -Message 'PLAY_WINDOW_TOO_SHORT (window=10 required=20)'), "
            "(Get-GuiSmokeRefusalReason -ExitCode 14 -Message '[GUI-SMOKE] ERROR: REPLAY_REFUSED (site=x)'), "
            "(Get-GuiSmokeRefusalReason -ExitCode 43 -Message ''), (Get-GuiSmokeRefusalReason -ExitCode 0 -Message 'CLIP_TOO_SHORT') -join ','")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        lines = proc.stdout.split()
        self.assertEqual(lines[0], "41,41,42")
        self.assertEqual(lines[1], "PLAY_WINDOW_TOO_SHORT,REPLAY_REFUSED,EXIT_43,NONE")

    def test_exit_43_types_a_play_that_overran_its_window(self) -> None:
        proc = _pwsh(f". {_q(GATE)}; Get-GuiSmokeRefusalReason -ExitCode 43 -Message 'PLAY_OVERRAN_WINDOW: x'")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.strip(), "PLAY_OVERRAN_WINDOW")

    def test_an_autoplay_variable_inherited_from_the_parent_environment_is_refused(self) -> None:
        def check(extra_env: dict, call: str = "Test-GuiSmokeParentEnvironment") -> dict:
            env = {k: v for k, v in os.environ.items() if not k.upper().startswith("MLVAPP_AUTOPLAY_")}
            env.update(extra_env)
            proc = subprocess.run(
                [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                 f". {_q(GATE)}; {call} | ConvertTo-Json -Compress"],
                capture_output=True, text=True, timeout=120, env=env)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            return json.loads(proc.stdout)
        self.assertEqual(check({})["verdict"], "OK")
        refused = check({"MLVAPP_AUTOPLAY_SECONDS": "30"})
        self.assertEqual(refused["verdict"], "PASS_THROUGH_REFUSED")
        self.assertEqual(refused["message"], "PASS_THROUGH_REFUSED (env=MLVAPP_AUTOPLAY_SECONDS reason=inherited_autoplay_hook_has_no_length_gate)")
        self.assertEqual(check({"mlvapp_autoplay_loop": "1"})["verdict"], "PASS_THROUGH_REFUSED")
        # an explicit hashtable (capture-reference-frame's -ExtraEnv) is checked without touching the process env
        self.assertEqual(check({}, "Test-GuiSmokeParentEnvironment -Environment @{ MLVAPP_AUTOPLAY_LOOP = '1' }")["verdict"],
                         "PASS_THROUGH_REFUSED")
        self.assertEqual(check({}, "Test-GuiSmokeParentEnvironment -Environment @{ MLVAPP_SOMETHING_ELSE = '1' }")["verdict"], "OK")

    def test_enforce_2_r2_sol_b3_the_cut_range_repair_knob_is_refused_in_every_environment_surface(self) -> None:
        # The engine can switch the collapsed-range repair off (MLVAPP_F3_DISABLE_CUT_RANGE_REPAIR); the app's gate
        # follows the knob, and the tools additionally refuse it so no evidence run depends on that agreement.
        def run(call: str, env_extra: dict | None = None) -> dict:
            env = {k: v for k, v in os.environ.items()
                   if not k.upper().startswith(("MLVAPP_AUTOPLAY_", "MLVAPP_F3_"))}
            env.update(env_extra or {})
            proc = subprocess.run(
                [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                 f". {_q(GATE)}; {call} | ConvertTo-Json -Compress"],
                capture_output=True, text=True, timeout=120, env=env)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            return json.loads(proc.stdout)
        for entry in ("MLVAPP_F3_DISABLE_CUT_RANGE_REPAIR=1", "mlvapp_f3_disable_cut_range_repair=0",
                      "A=1,MLVAPP_F3_DISABLE_CUT_RANGE_REPAIR=true"):
            with self.subTest(entry):
                refused = run(f"Test-GuiSmokeEnvironmentEntries -Entries @({_q(entry)})")
                self.assertEqual(refused["verdict"], "PASS_THROUGH_REFUSED")
                self.assertEqual(refused["message"], "PASS_THROUGH_REFUSED (env=MLVAPP_F3_DISABLE_CUT_RANGE_REPAIR "
                                                     "reason=changes_the_effective_play_range)")
        self.assertEqual(run("Test-GuiSmokeEnvironmentEntries -Entries @('MLVAPP_PLAYBACK_SCALE_FACTOR=4')")["verdict"], "OK")
        inherited = run("Test-GuiSmokeParentEnvironment", {"MLVAPP_F3_DISABLE_CUT_RANGE_REPAIR": "1"})
        self.assertEqual(inherited["verdict"], "PASS_THROUGH_REFUSED")
        self.assertIn("inherited_variable_changes_the_effective_play_range", inherited["message"])
        self.assertEqual(run("Test-GuiSmokeParentEnvironment")["verdict"], "OK")


@requires_pwsh
@requires_windows
class RunnerChokePointTests(_TmpCase):
    """2. The runner: the choke point every venue playback leg goes through. -DryRun stops after the
    gate and the argument list, so no app is launched and no footage is needed."""

    def setUp(self) -> None:
        super().setUp()
        # The runner only resolves -ExePath and stamps it on a dry run; any existing file stands in.
        self.exe = self.tmp / "MLVApp.exe"
        self.exe.write_bytes(b"MZ stand-in, never launched")

    def run_runner(self, clip: Path, *extra: str) -> subprocess.CompletedProcess:
        out = self.tmp / "out.json"
        return subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(RUNNER),
             "-RepoRoot", str(ROOT), "-ExePath", str(self.exe), "-Input", str(clip), "-Output", str(out),
             "-DryRun", *extra],
            capture_output=True, text=True, timeout=180,
        )

    def dry_run_arguments(self, proc: subprocess.CompletedProcess) -> list[str]:
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)["arguments"]

    def test_the_default_argument_list_never_contains_loop(self) -> None:
        proc = self.run_runner(self.clip("thirty", FRAMES_30S_AT_23976))
        self.assertNotIn("--loop", self.dry_run_arguments(proc))
        self.assertEqual(json.loads(proc.stdout)["clipLengthGate"], "OK")

    def test_the_legacy_noloop_switch_still_binds_and_stays_loop_free(self) -> None:
        proc = self.run_runner(self.clip("thirty", FRAMES_30S_AT_23976), "-NoLoop")
        self.assertNotIn("--loop", self.dry_run_arguments(proc))

    def test_a_short_clip_is_refused_before_anything_launches(self) -> None:
        proc = self.run_runner(self.clip("sixteen", FRAMES_SHORT_FIXTURE))
        self.assertEqual(proc.returncode, EXIT_TOO_SHORT, proc.stdout + proc.stderr)
        self.assertIn("CLIP_TOO_SHORT (clip=0.667 window=24)", proc.stderr)

    def test_an_empty_clip_is_refused_length_unknown(self) -> None:
        proc = self.run_runner(self.clip("zero", 0))
        self.assertEqual(proc.returncode, EXIT_UNKNOWN, proc.stdout + proc.stderr)
        self.assertIn("CLIP_LENGTH_UNKNOWN (reason=zero_frames)", proc.stderr)

    def test_the_tracked_large_fixture_is_refused_against_seconds_40(self) -> None:
        self.assertTrue(TRACKED_LARGE_FIXTURE.is_file())
        proc = self.run_runner(TRACKED_LARGE_FIXTURE, "-Seconds", "40")
        self.assertEqual(proc.returncode, EXIT_TOO_SHORT, proc.stdout + proc.stderr)
        self.assertIn("CLIP_TOO_SHORT (clip=0.667 window=40)", proc.stderr)

    # GUI-SMOKE-OVERRUN-ORACLE-SPANNED-COUNT-1 r4 (Grok G1, adversary pass 4 A1): the gate's counted parts and the runner's
    # immutable binding must be the same set. The binding lists parts by an exact-case base name; the gate (like the app's
    # fopen) resolves the probed name through the file system, so on NTFS a continuation whose base differs in case is counted
    # by the gate and absent from the binding. That run is refused, typed, before anything is staged or launched.
    @staticmethod
    def _continuation_extension() -> str:
        return MLV_EXTENSION[:-2] + "00"

    def _case_insensitive_volume(self) -> bool:
        probe = self.tmp / "case-probe-lower"
        probe.write_bytes(b"")
        try:
            return (self.tmp / "CASE-PROBE-LOWER").exists()
        finally:
            probe.unlink()

    def test_r4_g1_a_continuation_whose_base_differs_in_case_is_refused_by_the_runner_not_bound_partially(self) -> None:
        first = write_synthetic_mlv(self.tmp / ("g8" + MLV_EXTENSION), 400, file_num=0, file_count=2, guid=0x5A5A0001)
        write_synthetic_mlv(self.tmp / ("G8" + self._continuation_extension()), 400, file_num=1, file_count=2, guid=0x5A5A0001)
        proc = self.run_runner(first)
        self.assertEqual(proc.returncode, EXIT_UNKNOWN, proc.stdout + proc.stderr)
        if self._case_insensitive_volume():
            # The gate counts both parts (800 frames, 33 s) and the binding holds one: the typed parity refusal.
            self.assertIn("CLIP_LENGTH_UNKNOWN (reason=binding_parts_mismatch", proc.stderr)
        else:
            # A case-sensitive volume: the app cannot open it either, so the gate itself refuses it as an incomplete set.
            self.assertIn("CLIP_LENGTH_UNKNOWN (reason=spanned_set_incomplete)", proc.stderr)
        self.assertNotIn(str(self.tmp), proc.stderr)

    def test_r4_g1_a_set_the_gate_and_the_binding_agree_on_proceeds(self) -> None:
        first = write_synthetic_mlv(self.tmp / ("agree" + MLV_EXTENSION), 400, file_num=0, file_count=2, guid=0x5A5A0001)
        write_synthetic_mlv(self.tmp / ("agree" + self._continuation_extension()), 400, file_num=1, file_count=2, guid=0x5A5A0001)
        proc = self.run_runner(first)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["clipLengthGate"], "OK")

    def test_r4_g1_the_gate_returns_the_parts_it_counted_in_probe_order(self) -> None:
        first = write_synthetic_mlv(self.tmp / ("order" + MLV_EXTENSION), 400, file_num=0, file_count=2, guid=0x5A5A0001)
        second = write_synthetic_mlv(self.tmp / ("order" + self._continuation_extension()), 400, file_num=1, file_count=2,
                                     guid=0x5A5A0001)
        proc = _pwsh(f". {_q(GATE)}; (Get-GuiSmokeClipLength -Path {_q(first)}).parts | ConvertTo-Json -Compress")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual([p.lower() for p in json.loads(proc.stdout)], [str(first).lower(), str(second).lower()])

    def test_a_thirty_second_clip_is_refused_for_a_longer_window(self) -> None:
        proc = self.run_runner(self.clip("thirty", FRAMES_30S_AT_23976), "-Seconds", "45")
        self.assertEqual(proc.returncode, EXIT_TOO_SHORT, proc.stdout + proc.stderr)

    def test_enforce_2_a_play_window_under_20_seconds_is_refused_by_the_runner(self) -> None:
        thirty = self.clip("thirty", FRAMES_30S_AT_23976)
        proc = self.run_runner(thirty, "-Seconds", "10")
        self.assertEqual(proc.returncode, EXIT_TOO_SHORT, proc.stdout + proc.stderr)
        self.assertIn("PLAY_WINDOW_TOO_SHORT (window=10 required=20)", proc.stderr)
        proc = self.run_runner(thirty, "-Seconds", "20")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_enforce_2_a_short_presented_frames_stop_is_refused_by_the_runner(self) -> None:
        thirty = self.clip("thirty", FRAMES_30S_AT_23976)
        proc = self.run_runner(thirty, "-TargetPresentedFrames", "24")
        self.assertEqual(proc.returncode, EXIT_TOO_SHORT, proc.stdout + proc.stderr)
        self.assertIn("PLAY_WINDOW_TOO_SHORT (presented_frames=24", proc.stderr)

    def test_enforce_2_an_inherited_autoplay_variable_is_refused_by_the_runner(self) -> None:
        env = dict(os.environ, MLVAPP_AUTOPLAY_SECONDS="30")
        proc = subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(RUNNER),
             "-RepoRoot", str(ROOT), "-ExePath", str(self.exe), "-Input", str(self.clip("thirty", FRAMES_30S_AT_23976)),
             "-Output", str(self.tmp / "out.json"), "-DryRun"],
            capture_output=True, text=True, timeout=180, env=env)
        self.assertEqual(proc.returncode, EXIT_PASS_THROUGH, proc.stdout + proc.stderr)
        self.assertIn("inherited_autoplay_hook_has_no_length_gate", proc.stderr)

    # ---- round 2 (sol B1/B2): the -AllowLoop parameter is GONE, and nothing else can loop ------------
    # (r1 had -AllowLoop-only-with-LaunchOnlyProbe tests here; the owner/hub ruling of round 2 retires
    # the parameter outright -- no evidence run can ever loop -- so they became the tests below.)

    def run_runner_ps(self, clip: Path, tail_ps: str = "") -> subprocess.CompletedProcess:
        """Runs the runner through -Command so -AdditionalArgs / -ExtraEnvironment take real arrays."""
        out = self.tmp / "out.json"
        script = (
            f"& {_q(RUNNER)} -RepoRoot {_q(ROOT)} -ExePath {_q(self.exe)} -Input {_q(clip)} "
            f"-Output {_q(out)} -DryRun {tail_ps}; exit $LASTEXITCODE")
        return subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, text=True, timeout=180)

    def test_the_allowloop_parameter_no_longer_exists(self) -> None:
        clip = self.clip("thirty", FRAMES_30S_AT_23976)
        for extra in (("-AllowLoop",), ("-AllowLoop", "-LaunchOnlyProbe", "-AllowZeroPresentedFrames")):
            with self.subTest(extra):
                proc = self.run_runner(clip, *extra)
                self.assertNotEqual(proc.returncode, 0, proc.stdout)
                self.assertIn("AllowLoop", proc.stdout + proc.stderr)
        self.assertNotIn("AllowLoop", "\n".join(_code_lines(RUNNER)))

    def test_sol_b1_additionalargs_loop_is_refused_before_launch(self) -> None:
        # Sol's exact repro: a 720-frame 24-fps clip, -Seconds 30, -AdditionalArgs @('--loop').
        proc = self.run_runner_ps(
            self.clip("thirty", FRAMES_30S_AT_23976), "-Seconds 30 -DisableLookAssist -AdditionalArgs @('--loop')")
        self.assertEqual(proc.returncode, EXIT_PASS_THROUGH, proc.stdout + proc.stderr)
        self.assertIn("PASS_THROUGH_REFUSED (option=loop", proc.stderr)
        self.assertNotIn("--loop", proc.stdout)

    def test_every_spelling_of_loop_and_every_window_control_is_refused(self) -> None:
        clip = self.clip("thirty", FRAMES_30S_AT_23976)
        spellings = ["--loop", "-loop", "--LOOP", "/loop", "--loop=1", "--seconds 3 --loop", "--seconds", "--start-frame",
                     "--presented-frames", "--input", "-i", "--gui-smoke-playback", "--exercise-clip-lifecycle-stress",
                     "--stress-switch-input", "--launch-only", "--exercise-play-action", "--profile-playback"]
        for spelling in spellings:
            with self.subTest(spelling):
                proc = self.run_runner_ps(clip, f"-AdditionalArgs @({_q(spelling)})")
                self.assertEqual(proc.returncode, EXIT_PASS_THROUGH, proc.stdout + proc.stderr)
                self.assertIn("PASS_THROUGH_REFUSED", proc.stderr)

    def test_an_unclassified_option_is_refused_fail_closed(self) -> None:
        proc = self.run_runner_ps(self.clip("thirty", FRAMES_30S_AT_23976), "-AdditionalArgs @('--some-new-flag')")
        self.assertEqual(proc.returncode, EXIT_PASS_THROUGH, proc.stdout + proc.stderr)
        self.assertIn("reason=unclassified", proc.stderr)

    def test_harmless_additionalargs_still_ride_through(self) -> None:
        proc = self.run_runner_ps(
            self.clip("thirty", FRAMES_30S_AT_23976), "-AdditionalArgs @('--no-zebras','--scope','none')")
        arguments = self.dry_run_arguments(proc)
        self.assertIn("--no-zebras", arguments)
        self.assertNotIn("--loop", arguments)

    def test_the_autoplay_environment_hook_is_refused(self) -> None:
        clip = self.clip("thirty", FRAMES_30S_AT_23976)
        for entry in ("MLVAPP_AUTOPLAY_LOOP=1", "mlvapp_autoplay_seconds=5", "A=1,MLVAPP_AUTOPLAY_EXIT=1"):
            with self.subTest(entry):
                proc = self.run_runner_ps(clip, f"-ExtraEnvironment @({_q(entry)})")
                self.assertEqual(proc.returncode, EXIT_PASS_THROUGH, proc.stdout + proc.stderr)
                self.assertIn("PASS_THROUGH_REFUSED (env=MLVAPP_AUTOPLAY_", proc.stderr)

    def test_the_lifecycle_stress_switch_clip_is_length_gated_too(self) -> None:
        # The stress leg switches to ANOTHER clip mid-play; a short one is a short-clip playback.
        thirty = self.clip("thirty", FRAMES_30S_AT_23976)
        short = self.clip("sixteen", FRAMES_SHORT_FIXTURE)
        refused = self.run_runner_ps(thirty, f"-ExerciseClipLifecycleStress -StressSwitchInput {_q(short)}")
        self.assertEqual(refused.returncode, EXIT_TOO_SHORT, refused.stdout + refused.stderr)
        accepted = self.run_runner_ps(thirty, f"-ExerciseClipLifecycleStress -StressSwitchInput {_q(thirty)}")
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)

    def test_enforce_2_r2_sol_b2_the_lifecycle_stress_switch_may_not_stop_play_before_the_floor(self) -> None:
        thirty = self.clip("thirty", FRAMES_30S_AT_23976)
        for at_ms in (0, 1000, 19999):
            with self.subTest(at_ms):
                refused = self.run_runner_ps(
                    thirty, f"-ExerciseClipLifecycleStress -StressSwitchInput {_q(thirty)} -StressSwitchAtMs {at_ms}")
                self.assertEqual(refused.returncode, EXIT_TOO_SHORT, refused.stdout + refused.stderr)
                self.assertIn("PLAY_WINDOW_TOO_SHORT (stress-switch-at-ms=", refused.stderr)
        for at_ms in (20000, 24000):
            with self.subTest(at_ms):
                accepted = self.run_runner_ps(
                    thirty, f"-Seconds 25 -ExerciseClipLifecycleStress -StressSwitchInput {_q(thirty)} -StressSwitchAtMs {at_ms}")
                self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        # the DEFAULT is the floor, so a bare -ExerciseClipLifecycleStress is admitted and passes 20000 on
        proc = self.run_runner(thirty, "-ExerciseClipLifecycleStress", "-StressSwitchInput", str(thirty))
        arguments = self.dry_run_arguments(proc)
        self.assertEqual(arguments[arguments.index("--stress-switch-at-ms") + 1], "20000")

    # ---- round 2 (sol B2): a launch-only probe must not play ---------------------------------------

    def test_a_launch_only_probe_launches_without_any_playback_option(self) -> None:
        proc = self.run_runner(
            self.clip("sixteen", FRAMES_SHORT_FIXTURE), "-LaunchOnlyProbe", "-AllowZeroPresentedFrames")
        arguments = self.dry_run_arguments(proc)
        self.assertIn("--launch-only", arguments)
        self.assertNotIn("--loop", arguments)
        self.assertEqual(json.loads(proc.stdout)["clipLengthGate"], "SKIPPED_LAUNCH_ONLY_PROBE")

    def test_a_playback_run_never_carries_the_launch_only_flag(self) -> None:
        proc = self.run_runner(self.clip("thirty", FRAMES_30S_AT_23976))
        self.assertNotIn("--launch-only", self.dry_run_arguments(proc))


def _literal_template(text: str, anchor: str) -> str:
    """The concatenated string literals of the app's real log format that starts with `anchor`."""
    start = text.index(anchor)
    literal_start = text.rindex('"', 0, start)
    end = text.index(".arg(", start)
    return "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', text[literal_start:end]))


# ENFORCE-4 r2 (sol BLOCKER): every receipt carries the launcher's per-run nonce. Tests that judge a summary line stand in
# for the launcher: the line they build carries RUN_NONCE (unless a case overrides run_nonce) and the verdict they run is
# handed the same RUN_NONCE as the nonce THIS run generated.
RUN_NONCE = "n0123456789abcdef0123456789abcdef"


def _summary_line(**values: object) -> str:
    """The app's REAL playback_smoke.summary format (read from MainWindow.cpp, not retyped here) with
    every placeholder filled: `values` by key name, everything else 0 (run_nonce: RUN_NONCE)."""
    template = _literal_template(MAIN_WINDOW.read_text(encoding="utf-8"), "playback_smoke.summary session=%1")
    keys = dict(re.findall(r"(\w+)=%(\d+)", template))
    by_index = {int(index): str(values.get(key, RUN_NONCE if key == "run_nonce" else 0)) for key, index in keys.items()}
    return re.sub(r"%(\d+)", lambda m: by_index.get(int(m.group(1)), "0"), template)


def _loop_verdict(line: str, window: float, launch_only: bool = False, gate: Path = GATE, clip_frames: int = 0,
                  nonce: str = RUN_NONCE, require_window_stop: bool = False) -> dict:
    """Runs the shared parser + Get-GuiSmokeLoopVerdict (the code the runner calls) on a summary line.
    -RequireWindowStop is passed only when the keyword is True, so every other row calls the base signature."""
    window_stop = " -RequireWindowStop $true" if require_window_stop else ""
    proc = _pwsh(
        f". {_q(gate)}; $s = Convert-PlaybackLogLineToObject {_q(line)}; "
        f"Get-GuiSmokeLoopVerdict -Summary $s -WindowSeconds {window} -ExpectedRunNonce {_q(nonce)} "
        f"-LaunchOnlyProbe ${'true' if launch_only else 'false'} -ClipFrames {clip_frames}{window_stop} "
        "| ConvertTo-Json -Compress -Depth 4")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout)


# (summary fields, window, launch_only, expected invalid) -- the behaviour table the mutation test reuses.
LOOP_VERDICT_CASES = [
    (dict(wrapped=0, wrap_count=0, total_frames=720, clip_seconds=30.03, presented_frames=500), 24, False, False),
    (dict(wrapped=1, wrap_count=1, total_frames=720, clip_seconds=30.03, presented_frames=500), 24, False, True),
    # sol B4: the engine's own wrap count outranks the presented-frame heuristic (wrapped stayed 0).
    (dict(wrapped=0, wrap_count=2, total_frames=720, clip_seconds=30.03, presented_frames=500), 24, False, True),
    (dict(wrapped=0, wrap_count=0, total_frames=16, clip_seconds=0.667, presented_frames=500), 24, False, True),
    (dict(wrapped=0, wrap_count=0, total_frames=480, clip_seconds=19.98, presented_frames=500), 10, False, True),
    (dict(wrapped=0, wrap_count=0, total_frames=600, clip_seconds=25.025, presented_frames=500), 40, False, True),
    (dict(wrapped=0, wrap_count=0, total_frames=720, clip_seconds=30.03, presented_frames=0), 24, True, False),
    (dict(wrapped=0, wrap_count=0, total_frames=720, clip_seconds=30.03, presented_frames=5), 24, True, True),
    (dict(wrapped=1, wrap_count=1, total_frames=16, clip_seconds=0.667, presented_frames=0), 24, True, True),
    # an older binary (the app's own wrap fields silent: 0) is still caught from the clip's header frame count
    (dict(total_frames=0, clip_seconds=0, presented_frames=900, first_presented_frame=0, last_presented_frame=719), 24, False, True, 720),
    (dict(total_frames=0, clip_seconds=0, presented_frames=500, first_presented_frame=300, last_presented_frame=100), 24, False, True, 720),
    (dict(total_frames=0, clip_seconds=0, presented_frames=500, first_presented_frame=0, last_presented_frame=499), 24, False, False, 720),
]


# PLAYBACK-CLIP-LENGTH-ENFORCE-3: a run that consumed its source frames at the clip's native pace. Every row that
# is not ABOUT the source-frame oracle carries it, so each row isolates one cause.
GOOD_SOURCE = dict(source_advanced=480, required_source_frames=480, native_fps=23.976, pace_fps=23.976, fps_override=0)

SOURCE_FRAME_CASES = [
    # sol r2's repro as the app would have reported it: fpsOverride=12 on a 24 fps clip, a 20 s wall-clock stop.
    (dict(wrapped=0, wrap_count=0, total_frames=720, clip_seconds=30.03, presented_frames=240,
          source_advanced=241, required_source_frames=480, native_fps=24.0, pace_fps=12.0, fps_override=1), 24, False, True),
    (dict(wrapped=0, wrap_count=0, total_frames=720, clip_seconds=30.03, presented_frames=500,
          source_advanced=479, required_source_frames=480), 24, False, True),
    (dict(wrapped=0, wrap_count=0, total_frames=720, clip_seconds=30.03, presented_frames=500,
          source_advanced=480, required_source_frames=480), 24, False, False),
    (dict(wrapped=0, wrap_count=0, total_frames=720, clip_seconds=30.03, presented_frames=500,
          source_advanced=900, required_source_frames=0), 24, False, True),
    (dict(wrapped=0, wrap_count=0, total_frames=720, clip_seconds=30.03, presented_frames=500, fps_override=1), 24, False, True),
    (dict(wrapped=0, wrap_count=0, total_frames=720, clip_seconds=30.03, presented_frames=500,
          native_fps=24.0, pace_fps=12.0), 24, False, True),
    (dict(wrapped=0, wrap_count=0, total_frames=720, clip_seconds=30.03, presented_frames=500,
          native_fps=24.0, pace_fps=24.05), 24, False, False),
    # wrapped stays invalid even with every source frame counted
    (dict(wrapped=1, wrap_count=1, total_frames=720, clip_seconds=30.03, presented_frames=500,
          source_advanced=480, required_source_frames=480), 24, False, True),
    # a launch-only probe never plays: no source frames is exactly right
    (dict(wrapped=0, wrap_count=0, total_frames=720, clip_seconds=30.03, presented_frames=0,
          source_advanced=0, required_source_frames=0), 24, True, False),
]
# PLAYBACK-CLIP-LENGTH-HFR-10S-1 (owner amendment 2026-10-15): an HFR clip (>= 50 fps native) needs 10 s, ceil(10 x native fps)
# source frames; every other clip keeps 20 s. Both oracle carriers and the loop verdict execute these rows.
HFR_SOURCE_FRAME_CASES = [
    # 59.94 fps, a 12 s clip, 10 s window: 600 source frames consumed at native pace -- VALID
    (dict(wrapped=0, wrap_count=0, total_frames=720, clip_seconds=12.012, presented_frames=600,
          source_advanced=600, required_source_frames=600, native_fps=59.94, pace_fps=59.94), 10, False, False),
    # 59.94 fps admitted for 9.9 s (594 frames): under ceil(10 x 59.94) = 600 -- INVALID
    (dict(wrapped=0, wrap_count=0, total_frames=720, clip_seconds=12.012, presented_frames=594,
          source_advanced=594, required_source_frames=594, native_fps=59.94, pace_fps=59.94), 10, False, True),
    (dict(wrapped=0, wrap_count=0, total_frames=720, clip_seconds=12.012, presented_frames=599,
          source_advanced=599, required_source_frames=599, native_fps=59.94, pace_fps=59.94), 10, False, True),
    # 23.976 fps, 10 s of footage (240 frames): a non-HFR clip keeps the 20 s floor -- INVALID
    (dict(wrapped=0, wrap_count=0, total_frames=720, clip_seconds=30.03, presented_frames=240,
          source_advanced=240, required_source_frames=240, native_fps=23.976, pace_fps=23.976), 10, False, True),
    # 23.976 fps, 20 s -- VALID (the unchanged rule)
    (dict(wrapped=0, wrap_count=0, total_frames=720, clip_seconds=30.03, presented_frames=480,
          source_advanced=480, required_source_frames=480, native_fps=23.976, pace_fps=23.976), 20, False, False),
    # 50 fps, a 12 s clip, 10 s: 500 source frames -- VALID
    (dict(wrapped=0, wrap_count=0, total_frames=600, clip_seconds=12.0, presented_frames=500,
          source_advanced=500, required_source_frames=500, native_fps=50.0, pace_fps=50.0), 10, False, False),
    # an HFR clip LOOPED to reach 10 s (a 5 s clip wrapped twice) -- INVALID_LOOPED, whatever the count says
    (dict(wrapped=1, wrap_count=2, total_frames=300, clip_seconds=5.005, presented_frames=600,
          source_advanced=600, required_source_frames=600, native_fps=59.94, pace_fps=59.94), 10, False, True),
]
SOURCE_FRAME_CASES.extend(HFR_SOURCE_FRAME_CASES)
LOOP_VERDICT_CASES.extend(SOURCE_FRAME_CASES)


def _case(row):
    fields, window, launch_only, expected, *rest = row
    return {**GOOD_SOURCE, **fields}, window, launch_only, expected, (rest[0] if rest else 0)


# GUI-SMOKE-OVERRUN-ORACLE-SPANNED-COUNT-1: a measured Play that ran PAST its window. Bachelor M16-1457 fullscreen legs 8bcdb433
# (F1) and c7948b18 (F3) played the whole two-part clip once (source_advanced 2859 of required 1499, wrap_count 0, reason play-stop):
# no loop, but no measurement of the 25 s window either. NOT part of LOOP_VERDICT_CASES / SOURCE_FRAME_CASES (the parity test
# iterates those, and the overrun oracle is the loop verdict's alone, behind -RequireWindowStop).
_OVERRUN_BASE = dict(wrapped=0, wrap_count=0, total_frames=2859, clip_seconds=47.698, first_presented_frame=0,
                     required_source_frames=1499, native_fps=59.94, pace_fps=59.94, fps_override=0)
_OVERRUN_F1 = dict(_OVERRUN_BASE, presented_frames=1858, last_presented_frame=2856, source_advanced=2859,
                   elapsed_ms=47904, reason="play-stop")
_OVERRUN_VALID = dict(_OVERRUN_BASE, reason="visual-capture")
_OVERRUN_2397 = dict(_OVERRUN_BASE, total_frames=1200, clip_seconds=50.05, required_source_frames=600, native_fps=23.976,
                     pace_fps=23.976, presented_frames=600, last_presented_frame=640, reason="visual-capture")

# (name, summary fields, window, launch_only, require_window_stop, clip_frames, expect invalid, expect PLAY_OVERRAN_WINDOW,
#  expect INVALID_LOOPED)
OVERRUN_CASES = [
    ("O1 F1 8bcdb433, true count", _OVERRUN_F1, 25, False, True, 2859, True, True, False),
    ("O2 F1 with the recorded 1671 count", _OVERRUN_F1, 25, False, True, 1671, True, True, False),
    ("O3 F3 c7948b18", dict(_OVERRUN_F1, presented_frames=1862, last_presented_frame=2857, elapsed_ms=47880),
     25, False, True, 2859, True, True, False),
    ("O4 count fix without the oracle", _OVERRUN_F1, 25, False, False, 2859, False, False, False),
    ("O5 1a667e8d", dict(_OVERRUN_VALID, presented_frames=1044, last_presented_frame=1496, source_advanced=1499,
                         elapsed_ms=27556), 25, False, True, 2859, False, False, False),
    ("O5 d2378536", dict(_OVERRUN_VALID, presented_frames=1226, last_presented_frame=1523, source_advanced=1525,
                         elapsed_ms=25618), 25, False, True, 2859, False, False, False),
    ("O5 62c557ff", dict(_OVERRUN_VALID, presented_frames=980, last_presented_frame=1509, source_advanced=1510,
                         elapsed_ms=25363), 25, False, True, 2859, False, False, False),
    ("O6 59.94 at the tolerance", dict(_OVERRUN_VALID, presented_frames=1000, last_presented_frame=1617, source_advanced=1619),
     25, False, True, 2859, False, False, False),
    ("O6 59.94 one over", dict(_OVERRUN_VALID, presented_frames=1000, last_presented_frame=1618, source_advanced=1620),
     25, False, True, 2859, True, True, False),
    ("O7 23.976 at the tolerance", dict(_OVERRUN_2397, source_advanced=648), 25, False, True, 1200, False, False, False),
    ("O7 23.976 one over", dict(_OVERRUN_2397, source_advanced=649), 25, False, True, 1200, True, True, False),
    ("O8 a real wrap", dict(_OVERRUN_F1, wrapped=1, wrap_count=1), 25, False, True, 2859, True, True, True),
    ("O9 launch-only probe", dict(_OVERRUN_BASE, presented_frames=0, last_presented_frame=0, source_advanced=0,
                                  required_source_frames=0), 25, True, True, 2859, False, False, False),
    # adversary e8eeaee1 A2: a counter far outside any real Play (9e15) is still an overrun, never a silent pass.
    ("O10 a counter of 9e15", dict(_OVERRUN_2397, native_fps=24, pace_fps=24, source_advanced=9000000000000000),
     25, False, True, 1200, True, True, False),
    # adversary f408901c A2: past 2^53 a double cannot hold the difference (49 frames read as 48); the overrun is exact.
    ("O11 an exact overrun past 2^53", dict(_OVERRUN_2397, native_fps=24, pace_fps=24, source_advanced=9007199254741041,
                                             required_source_frames=9007199254740992), 25, False, True, 1200, True, True, False),
    ("O11 the same at the tolerance", dict(_OVERRUN_2397, native_fps=24, pace_fps=24, source_advanced=9007199254741040,
                                            required_source_frames=9007199254740992), 25, False, True, 1200, False, False, False),
]


def _overrun_outcome(row, gate: Path = GATE) -> tuple:
    _name, fields, window, launch_only, require, clip_frames, *_expected = row
    verdict = _loop_verdict(_summary_line(**fields), window, launch_only, gate=gate, clip_frames=clip_frames,
                            require_window_stop=require)
    failures = verdict.get("failures") or []
    if isinstance(failures, str):
        failures = [failures]
    return (verdict["invalid"], any(f.startswith("PLAY_OVERRAN_WINDOW: ") for f in failures),
            any(f.startswith("INVALID_LOOPED") for f in failures)), failures


@requires_pwsh
class RuntimeBackstopBehaviourTests(unittest.TestCase):
    """3. The runtime backstop, EXECUTED: the app's real summary format goes through the runner's real
    parser and decision function (sol hardening: a text match passed under inverted/disabled guards)."""

    def test_the_app_summary_and_gate_lines_carry_wrap_fields(self) -> None:
        text = MAIN_WINDOW.read_text(encoding="utf-8")
        summary = _literal_template(text, "playback_smoke.summary session=%1")
        gate = _literal_template(text, "playback_smoke.gate session=%1")
        for template in (summary, gate):
            for key in ("wrapped", "wrap_count", "total_frames", "clip_seconds"):
                self.assertRegex(template, rf"\b{key}=%\d+", template)

    def test_the_behaviour_table(self) -> None:
        for row in LOOP_VERDICT_CASES:
            fields, window, launch_only, expected, clip_frames = _case(row)
            with self.subTest(fields=fields, window=window, launch_only=launch_only):
                verdict = _loop_verdict(_summary_line(**fields), window, launch_only, clip_frames=clip_frames)
                self.assertEqual(verdict["invalid"], expected, verdict)

    def test_a_summary_without_the_source_frame_fields_is_invalid(self) -> None:
        # ENFORCE-3: a build that cannot report source_advanced / required_source_frames cannot prove the footage
        # played; the receipt oracle fails closed (it used to wave such a build through with a warning).
        verdict = _loop_verdict("playback_smoke.summary session=1 presented_frames=900", 24)
        self.assertTrue(verdict["invalid"], verdict)
        self.assertIn("INVALID_SOURCE_FRAMES", json.dumps(verdict))
        # ... and a launch-only probe, which never plays, needs none.
        self.assertFalse(_loop_verdict("playback_smoke.summary session=1 presented_frames=0", 24, launch_only=True)["invalid"])

    def test_the_absent_fields_check_is_mutation_tested(self) -> None:
        source = GATE.read_text(encoding="utf-8")
        needle = "} elseif ($null -eq $advanced -or $null -eq $required) {"
        self.assertIn(needle, source)
        mutated = self.tmp_gate(source.replace(needle, "} elseif ($false) {"))
        line = "playback_smoke.summary session=1 presented_frames=900"
        self.assertTrue(_loop_verdict(line, 24)["invalid"])
        # with the check gone the verdict no longer names the absent fields (the absent-summary branch is the only
        # other way to fail), so the mutation is visible
        self.assertNotIn("carries no source_advanced", json.dumps(_loop_verdict(line, 24, gate=mutated)))

    def test_the_app_summary_and_gate_lines_carry_the_source_frame_fields(self) -> None:
        text = MAIN_WINDOW.read_text(encoding="utf-8")
        for anchor in ("playback_smoke.summary session=%1", "playback_smoke.gate session=%1"):
            template = _literal_template(text, anchor)
            for key in ("source_advanced", "required_source_frames", "native_fps", "pace_fps", "fps_override"):
                self.assertRegex(template, rf"\b{key}=%\d+", template)

    def test_the_decision_code_is_mutation_tested(self) -> None:
        # Sol's two mutations (invert the wrapped check; disable the enclosing guard) plus the other
        # decision points: each must flip at least one row of the behaviour table, or the table is too weak.
        source = GATE.read_text(encoding="utf-8")
        mutations = {
            "wrapped check inverted": ("[int]$wrapped -ne 0", "[int]$wrapped -eq 0"),
            "wrap_count check inverted": ("[int64]$wrapCount -gt 0", "[int64]$wrapCount -le 0"),
            "playback guard disabled": ("if ($LaunchOnlyProbe) {", "if ($true) {"),
            "clip_seconds compare inverted": ("[double]$clipSeconds -lt [Math]::Max", "[double]$clipSeconds -gt [Math]::Max"),
            "verdict never invalid": ("invalid = ($failures.Count -gt 0)", "invalid = $false"),
            "presented-over-clip check inverted": ("[int64]$presented -gt $ClipFrames", "[int64]$presented -le $ClipFrames"),
            "last-before-first check inverted": ("[int64]$lastPresented -lt [int64]$firstPresented", "[int64]$lastPresented -gt [int64]$firstPresented"),
            # ENFORCE-3: the source-frame oracle
            "source short check inverted": ("[int64]$advanced -lt [int64]$required", "[int64]$advanced -ge [int64]$required"),
            "unknown requirement accepted": ("[int64]$required -le 0", "[int64]$required -lt 0"),
            "fps override ignored": ("[int]$fpsOverride -ne 0", "[int]$fpsOverride -eq 0"),
            "pace comparison inverted": ("-gt (0.005 * [double]$nativeFps)", "-le (0.005 * [double]$nativeFps)"),
            "source verdict dropped": ("$failures += @($sourceFramesVerdict.failures)", "$null = @($sourceFramesVerdict.failures)"),
            # PLAYBACK-CLIP-LENGTH-HFR-10S-1: the floor derivation itself
            "HFR floor collapsed to 20 s": ("        return $script:GuiSmokeHfrMinPlaySeconds\n", "        return $script:GuiSmokeStandardMinPlaySeconds\n"),
            "every clip given the 10 s floor": ("$floorSeconds = Get-GuiSmokeMinPlaySeconds -NativeFps $nativeFps", "$floorSeconds = 10.0"),
            "HFR threshold ignored": ("$fps -ge $script:GuiSmokeHfrNativeFpsThreshold", "$fps -ge 0"),
        }
        for name, (needle, replacement) in mutations.items():
            with self.subTest(name):
                self.assertIn(needle, source, f"mutation anchor vanished: {name}")
                mutated = self.tmp_gate(source.replace(needle, replacement))
                flipped = []
                for row in LOOP_VERDICT_CASES:
                    fields, window, launch_only, expected, clip_frames = _case(row)
                    got = _loop_verdict(_summary_line(**fields), window, launch_only, gate=mutated, clip_frames=clip_frames)
                    if got["invalid"] != expected:
                        flipped.append(fields)
                self.assertTrue(flipped, f"mutation '{name}' survived the behaviour table")

    def test_the_overrun_table(self) -> None:
        for row in OVERRUN_CASES:
            name, _fields, _window, _launch, _require, _clip, *expected = row
            with self.subTest(name):
                outcome, failures = _overrun_outcome(row)
                self.assertEqual(outcome, tuple(expected), failures)

    def test_the_overrun_failure_carries_its_numbers_and_says_what_it_means(self) -> None:
        _outcome, failures = _overrun_outcome(OVERRUN_CASES[0])
        overran = [f for f in failures if f.startswith("PLAY_OVERRAN_WINDOW: ")]
        self.assertEqual(len(overran), 1, failures)
        for token in ("source_advanced=2859", "required_source_frames=1499", "overrun=1360", "tolerance=120",
                      "native_fps=59.94", "reason=play-stop", "elapsed_ms=47904", "ran past its window",
                      "does not measure the requested window"):
            self.assertIn(token, overran[0])
        self.assertRegex(overran[0], r"tolerance=120 frames \(2(\.0)? s\b")

    def test_a_real_wrap_is_reported_before_any_overrun(self) -> None:
        row = next(r for r in OVERRUN_CASES if r[0] == "O8 a real wrap")
        _outcome, failures = _overrun_outcome(row)
        self.assertTrue(failures[0].startswith("INVALID_LOOPED"), failures)

    def test_the_overrun_oracle_is_mutation_tested(self) -> None:
        source = GATE.read_text(encoding="utf-8")
        mutations = {
            "overrun compare inverted": ("$overrun -gt $tolerance", "$overrun -le $tolerance"),
            "tolerance replaced by 0": (
                "$tolerance = [Math]::Ceiling($script:GuiSmokePlayOverrunToleranceSeconds * $overrunFps)",
                "$tolerance = 0.0"),
            "oracle guard always false": ("if ($RequireWindowStop -and ", "if ($false -and "),
            "RequireWindowStop default ignored": ("if ($RequireWindowStop -and ", "if ($true -and "),
            "presented-over-clip suppression dropped": (" -and -not $overranWithoutWrap", ""),
        }
        for name, (needle, replacement) in mutations.items():
            with self.subTest(name):
                self.assertEqual(source.count(needle), 1, f"mutation anchor not unique or vanished: {name}")
                mutated = self.tmp_gate(source.replace(needle, replacement))
                flipped = [row[0] for row in OVERRUN_CASES if _overrun_outcome(row, gate=mutated)[0] != tuple(row[6:])]
                self.assertTrue(flipped, f"mutation '{name}' survived the overrun table")

    def test_exit_43_is_reachable_under_erroractionpreference_stop(self) -> None:
        # Fable r1: Write-Error under $ErrorActionPreference = "Stop" terminated the runner before the exit
        # code was assigned, so INVALID_LOOPED never exited 43. EXECUTE the runner's real tail block.
        text = RUNNER.read_text(encoding="utf-8")
        start = text.index("$scriptExitCode = 0")
        block = text[start:text.index("finally {", start)]
        block = block[:block.rindex("}")]          # drop the try-closing brace that precedes `finally`
        for invalid_looped, expected in (("$true", 43), ("$false", 2)):
            with self.subTest(invalid_looped=invalid_looped):
                proc = _pwsh(
                    "$ErrorActionPreference = 'Stop'; $processExitCode = 0; "
                    f"$invalidLooped = {invalid_looped}; $validationFailures = @('first failure', 'second failure'); "
                    f"{block}\nexit $scriptExitCode")
                self.assertEqual(proc.returncode, expected, proc.stdout + proc.stderr)
                self.assertIn("second failure", proc.stderr)   # every failure is still reported

    def tmp_gate(self, text: str) -> Path:
        tmp = tempfile.TemporaryDirectory(prefix="gate-mutant-")
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "gate.ps1"
        path.write_text(text, encoding="utf-8")
        return path

    def test_the_runner_turns_a_wrap_into_invalid_looped_and_never_a_pass(self) -> None:
        # The DECISION is executed above; this pins only the wiring that feeds it and applies its result.
        text = RUNNER.read_text(encoding="utf-8")
        self.assertIn("Get-GuiSmokeLoopVerdict -Summary $playbackSummary", text)
        self.assertIn("$validationFailures += $loopVerdict.failures", text)
        self.assertIn("$scriptExitCode = if ($invalidLooped) { 43 } else { 2 }", text)
        self.assertIn("$invalidLooped = -not $LaunchOnlyProbe", text)
        # The runner no longer carries its own copy of the parser or of the decision.
        self.assertNotIn("function Convert-PlaybackLogLineToObject", text)

    # ---- sol r2 hardening (ENFORCE-2): the runner's APPLICATION of the verdict is executed and mutated ----

    def _application_block(self, text: str) -> str:
        start = text.index('$loopWrappedRaw = Get-ObjectPropertyValue $playbackSummary "wrapped"')
        end = text.index("if ($LaunchOnlyProbe -and ($playbackStartLine -or $summaryLine)) {", start)
        return text[start:end]

    def _run_application_block(self, block: str, recent_lines: tuple = (), stress: bool = False, seconds: float = 24,
                               **summary_fields: object) -> dict:
        proc = _pwsh(
            f". {_q(GATE)}\n"
            "function Get-ObjectPropertyValue { param($Object, $Name) "
            "if ($null -ne $Object -and $Object.PSObject.Properties[$Name]) { $Object.$Name } else { $null } }\n"
            f"$playbackSummary = Convert-PlaybackLogLineToObject {_q(_summary_line(**summary_fields))}\n"
            f"$recentLines = @({', '.join(_q(line) for line in recent_lines)})\n"
            f"$runNonce = {_q(RUN_NONCE)}; "
            f"$Seconds = {seconds}; $LaunchOnlyProbe = $false; $validationFailures = @(); $validationWarnings = @(); "
            f"$ExerciseClipLifecycleStress = ${'true' if stress else 'false'}; "
            "$clipLengthGate = [pscustomobject]@{ frames = 720 }\n"
            f"{block}\n"
            "[pscustomobject]@{ invalidLooped = [bool]$invalidLooped; invalidSourceFrames = [bool]$invalidSourceFrames; "
            "playOverranWindow = [bool]$playOverranWindow; windowStopRequired = [bool]$windowStopRequired; "
            "failures = @($validationFailures).Count; paceRule = $loopVerdict.paceRule } | ConvertTo-Json -Compress")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def test_the_runners_verdict_application_is_executed_and_every_guard_mutation_is_caught(self) -> None:
        text = RUNNER.read_text(encoding="utf-8")
        block = self._application_block(text)
        wrapped = dict(wrapped=1, wrap_count=1, total_frames=720, clip_seconds=30.03, presented_frames=500, **GOOD_SOURCE)
        clean = dict(wrapped=0, wrap_count=0, total_frames=720, clip_seconds=30.03, presented_frames=500, **GOOD_SOURCE)
        # sol r2: 20 s of wall clock under a 12 fps override = 240 source frames of the 480 required
        short = dict(clean, source_advanced=240, native_fps=24.0, pace_fps=12.0, fps_override=1)
        # The real block: a wrap makes the run INVALID_LOOPED with its failure recorded; a clean run does not.
        got = self._run_application_block(block, **wrapped)
        self.assertTrue(got["invalidLooped"], got)
        self.assertGreaterEqual(got["failures"], 1, got)
        got = self._run_application_block(block, **clean)
        self.assertFalse(got["invalidLooped"], got)
        self.assertEqual(got["failures"], 0, got)
        got = self._run_application_block(block, **short)
        self.assertTrue(got["invalidLooped"], got)           # exit 43, never a PASS
        self.assertTrue(got["invalidSourceFrames"], got)
        self.assertGreaterEqual(got["failures"], 1, got)
        got = self._run_application_block(block, **wrapped)
        self.assertFalse(got["invalidSourceFrames"], got)    # a wrap is INVALID_LOOPED, not a source-frame shortfall

        # Mutations of the APPLICATION guard (fable/sol r2: the old test pinned text, so these survived).
        mutations = {
            "guard disabled": ("if ($loopVerdict.invalid) {", "if ($false -and $loopVerdict.invalid) {"),
            "guard inverted": ("if ($loopVerdict.invalid) {", "if (-not $loopVerdict.invalid) {"),
            "invalid flag never set": ("$invalidLooped = -not $LaunchOnlyProbe", "$invalidLooped = $false"),
            "failures dropped": ("$validationFailures += $loopVerdict.failures", "$null = $loopVerdict.failures"),
            "source-frame flag never set": ("$invalidSourceFrames = (-not $LaunchOnlyProbe) -and", "$invalidSourceFrames = $false -and"),
            "verdict ignored": ("$loopVerdict = Get-GuiSmokeLoopVerdict -Summary $playbackSummary",
                                "$loopVerdict = [pscustomobject]@{ invalid = $false; failures = @() }; "
                                "$null = Get-GuiSmokeLoopVerdict -Summary $playbackSummary"),
        }
        for name, (needle, replacement) in mutations.items():
            with self.subTest(name):
                self.assertIn(needle, block, f"mutation anchor vanished: {name}")
                mutated = block.replace(needle, replacement, 1)
                survived = True
                for fields, expected_invalid in ((wrapped, True), (clean, False), (short, True)):
                    outcome = self._run_application_block(mutated, **fields)
                    if outcome["invalidLooped"] != expected_invalid or \
                            (expected_invalid and outcome["failures"] < 1) or \
                            (fields is short and not outcome["invalidSourceFrames"]):
                        survived = False
                self.assertFalse(survived, f"guard mutation '{name}' survived the application test")

    def test_the_runner_requires_the_window_stop_except_for_launch_only_and_lifecycle_stress(self) -> None:
        """GUI-SMOKE-OVERRUN-ORACLE-SPANNED-COUNT-1: the runner opts into the overrun oracle for a measured Play only. The call
        passes -RequireWindowStop with an expression (directly, or through the one variable it assigns) naming both switches,
        the receipt's clipLength block records it, and the executed block types the F1 overrun."""
        text = RUNNER.read_text(encoding="utf-8")
        block = self._application_block(text)
        call = next(line for line in block.splitlines()
                    if line.startswith("$loopVerdict = Get-GuiSmokeLoopVerdict -Summary $playbackSummary"))
        match = re.search(r"-RequireWindowStop\s+(\$\w+|\(.*\))\s*$", call)
        self.assertIsNotNone(match, call)
        expression = match.group(1)
        if re.fullmatch(r"\$\w+", expression):
            assignment = re.search(rf"^\{expression}\s*=\s*(.+)$", block, re.MULTILINE)
            self.assertIsNotNone(assignment, f"{expression} is never assigned in the application block")
            expression = assignment.group(1)
        self.assertIn("$LaunchOnlyProbe", expression)
        self.assertIn("$ExerciseClipLifecycleStress", expression)
        clip_length = text[text.index("    clipLength = [pscustomobject]@{"):]
        clip_length = clip_length[:clip_length.index("\n    }\n")]
        after = clip_length[clip_length.index("invalidSourceFrames = $invalidSourceFrames"):]
        self.assertIn("playOverranWindow = $playOverranWindow", after)
        self.assertRegex(after, r"windowStopRequired = \$\w+")

        f1 = dict(_OVERRUN_F1)
        got = self._run_application_block(block, seconds=25, **f1)
        self.assertEqual((got["playOverranWindow"], got["windowStopRequired"], got["invalidLooped"]), (True, True, True), got)
        self.assertFalse(got["invalidSourceFrames"], got)
        got = self._run_application_block(block, seconds=25, stress=True, **f1)
        self.assertEqual((got["playOverranWindow"], got["windowStopRequired"]), (False, False), got)

    def test_the_runner_hands_the_summarys_own_session_conform_line_to_the_verdict(self) -> None:
        """HFR-CONFORM-AWARE-RECEIPT-ORACLES-1: the runner picks the playback_smoke.conform line of the summary's session out of the run log and
        the loop verdict relaxes the native-pace rule only on a line it can prove; the block is executed and its wiring mutated."""
        block = self._application_block(RUNNER.read_text(encoding="utf-8"))
        hfr = dict(wrapped=0, wrap_count=0, total_frames=1500, clip_seconds=25.0, presented_frames=500, session=7, source_advanced=1200,
                   required_source_frames=1199, native_fps="59.940", pace_fps="24.000", fps_override=0)
        own, other = _conform_line("7"), _conform_line("8")
        got = self._run_application_block(block, recent_lines=("noise", own), **hfr)
        self.assertEqual((got["invalidSourceFrames"], got["failures"], got["paceRule"]), (False, 0, "conform"), got)
        self.assertEqual(self._run_application_block(block, recent_lines=(own, other), **hfr)["paceRule"], "conform")   # another session's line is never picked
        for lines in ((), (other,), ("playback_smoke.conform session=70 clip_fps=59.940 playback_fps=24.000 conform_active=1",)):
            got = self._run_application_block(block, recent_lines=lines, **hfr)
            self.assertTrue(got["invalidSourceFrames"], (lines, got))
            self.assertEqual(got["paceRule"], "native", got)
        # the conform line must actually reach the verdict: without it the conformed run is judged on the native pace and is INVALID
        needle = " -ConformLine $loopConformLine"
        self.assertIn(needle, block, "mutation anchor vanished: conform line not passed on")
        mutated = block.replace(needle, "", 1)
        self.assertTrue(self._run_application_block(mutated, recent_lines=(own,), **hfr)["invalidSourceFrames"],
                        "runner conform-wiring mutation 'conform line not passed on' survived")

    def test_the_runner_source_never_spells_loop_as_an_argument(self) -> None:
        text = "\n".join(_code_lines(RUNNER))
        self.assertNotIn('"--loop"', text)
        self.assertNotIn("'--loop'", text)
        self.assertNotIn("$AllowLoop", text)

    def test_the_app_refuses_loop_and_has_a_launch_only_mode(self) -> None:
        main = (ROOT / "platform" / "qt" / "main.cpp").read_text(encoding="utf-8")
        self.assertRegex(main, r'QStringLiteral\("launch-only"\)')
        self.assertIn("--loop is refused", main)
        window = MAIN_WINDOW.read_text(encoding="utf-8")
        # launch-only returns BEFORE the measured Play trigger of runGuiPlaybackSmoke.
        launch_only_at = window.index("if( options.launchOnly )")
        self.assertLess(launch_only_at, window.index("m_playbackSmokeFullscreenLossLatchArmed = true;"))
        # The app's own gates (a direct exe launch that bypassed the runner): the whole clip, the stress
        # switch clip and the receipt's cut range (the shared programmaticPlay gate), each refused before
        # the measured Play.
        play_at = window.index('"[GUI-SMOKE] ERROR: Play action did not enter checked state.')
        for marker in ("ERROR: CLIP_TOO_SHORT (clip=", "(stress switch clip)",
                       'programmaticPlay( "gui-smoke-measured"'):
            self.assertIn(marker, window)
            self.assertLess(window.index(marker), play_at, marker)
        # Loop is forced OFF before the measured Play (a persisted GUI Loop must not loop it).
        self.assertIn('forceLoopOffForAutomation( "gui-smoke-measured" );', window)
        self.assertNotIn("options.loopPlayback", window)
        # The autoplay hook never loops and plays only through the shared gate (typed refusal reason).
        self.assertIn("autoplay.loop_ignored", window)
        self.assertIn('programmaticPlay( "autoplay", autoplaySeconds )', window)
        self.assertIn('QStringLiteral("reason=%1 window_seconds=%2")', window)


@requires_pwsh
class SourceFramesOracleParityTests(unittest.TestCase):
    """ENFORCE-3: the attribution job cannot dot-source the gate (the emitted-template lint forbids it), so it embeds
    Get-AttrCudaSourceFramesVerdict from AttrCudaArtifacts.psm1. Both implementations are EXECUTED on the same
    summary lines and must agree -- one rule, two carriers."""

    MODULE = ROOT / "tools" / "profiling" / "bachelor" / "AttrCudaArtifacts.psm1"

    def _gate_invalid(self, line: str) -> bool:
        proc = _pwsh(
            f". {_q(GATE)}; $s = Convert-PlaybackLogLineToObject {_q(line)}; "
            f"$v = Get-GuiSmokeSourceFramesVerdict -Summary $s -ExpectedRunNonce {_q(RUN_NONCE)}; "
            "$w = (($s.PSObject.Properties['wrapped'] -and [int]$s.wrapped -ne 0) -or "
            "($s.PSObject.Properties['wrap_count'] -and [int64]$s.wrap_count -gt 0)); "
            "[pscustomobject]@{ invalid = ($v.invalid -or $w) } | ConvertTo-Json -Compress")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)["invalid"]

    def _module_invalid(self, line: str) -> bool:
        proc = _pwsh(
            f"Import-Module {_q(self.MODULE)} -Force -DisableNameChecking; "
            f"$v = Get-AttrCudaSourceFramesVerdict -SummaryLine {_q(line)} -ExpectedRunNonce {_q(RUN_NONCE)}; "
            "[pscustomobject]@{ invalid = [bool]$v.invalid } | ConvertTo-Json -Compress")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)["invalid"]

    def test_both_carriers_agree_on_every_source_frame_row_and_on_an_absent_summary(self) -> None:
        for row in SOURCE_FRAME_CASES:
            fields, _window, launch_only, expected, _frames = _case(row)
            if launch_only:
                continue   # a launch-only probe never reaches the oracle
            with self.subTest(fields=fields):
                line = _summary_line(**fields)
                self.assertEqual(self._gate_invalid(line), expected, line)
                self.assertEqual(self._module_invalid(line), expected, line)
        absent = "playback_smoke.summary session=1 presented_frames=900"
        self.assertTrue(self._gate_invalid(absent))
        self.assertTrue(self._module_invalid(absent))
        self.assertTrue(self._module_invalid(""))

    def _module_failures(self, line: str, module: Path | None = None) -> list[str]:
        proc = _pwsh(
            f"Import-Module {_q(module or self.MODULE)} -Force -DisableNameChecking; "
            f"$v = Get-AttrCudaSourceFramesVerdict -SummaryLine {_q(line)} -ExpectedRunNonce {_q(RUN_NONCE)}; "
            "[pscustomobject]@{ failures = @($v.failures | ForEach-Object { [string]$_ }) } | ConvertTo-Json -Compress")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)["failures"]

    def _assert_hfr_wording(self, module: Path | None = None) -> None:
        """PLAYBACK-CLIP-LENGTH-HFR-10S-1 r3e: the module applies the HFR wording with a -replace on the base message, so a
        base-message change would make that replace a silent no-op and an HFR failure would name 20 s again. Pin the text."""
        hfr = _summary_line(**{**GOOD_SOURCE, "wrapped": 0, "wrap_count": 0, "total_frames": 720, "clip_seconds": 12.012,
                              "presented_frames": 500, "native_fps": 59.94, "pace_fps": 59.94,
                              "source_advanced": 300, "required_source_frames": 600})
        hfr_failures = self._module_failures(hfr, module)
        self.assertTrue(any(f.startswith("INVALID_SOURCE_FRAMES") and "under 10 s of real footage" in f for f in hfr_failures), hfr_failures)
        self.assertFalse(any("20 s of real footage" in f for f in hfr_failures), hfr_failures)

    def test_an_hfr_source_frames_failure_names_ten_seconds_and_every_other_rate_names_twenty(self) -> None:
        self._assert_hfr_wording()
        for native in (23.976, 49.99):
            with self.subTest(native_fps=native):
                low = _summary_line(**{**GOOD_SOURCE, "wrapped": 0, "wrap_count": 0, "native_fps": native, "pace_fps": native,
                                       "source_advanced": 100, "required_source_frames": 1200, "total_frames": 1200})
                failures = self._module_failures(low)
                self.assertTrue(any("under 20 s of real footage" in f for f in failures), failures)
                self.assertFalse(any("10 s of real footage" in f for f in failures), failures)
        # RED arm: break the -replace pattern in a scratch copy and the pin must fail on it (the real file stays untouched).
        source = self.MODULE.read_text(encoding="utf-8")
        needle = "-replace 'under 20 s of real footage', 'under 10 s of real footage'"
        self.assertEqual(source.count(needle), 1, "the HFR wording replace moved; update this pin")
        with tempfile.TemporaryDirectory(prefix="hfr-wording-red-") as tmp:
            broken = Path(tmp) / "AttrCudaArtifacts.psm1"
            broken.write_text(source.replace(needle, "-replace 'under 21 s of real footage', 'under 10 s of real footage'"), encoding="utf-8")
            with self.assertRaises(AssertionError):
                self._assert_hfr_wording(broken)

    def _job_oracle_block(self) -> str:
        job = JOB_GENERATOR.read_text(encoding="utf-8")
        start = job.index("$sourceFramesSummaryLine = $null")
        end = job.index("$gpuSummary = Get-LastGpuSummary $rawLog $measuredSmokeSessionId", start)
        return job[start:end]

    def _run_job_block(self, block: str, raw_log: str) -> tuple[int, dict]:
        with tempfile.TemporaryDirectory(prefix="job-oracle-") as tmp:
            pub = Path(tmp) / "pub"
            pub.mkdir()
            script = Path(tmp) / "probe.ps1"
            script.write_text(
                "$ErrorActionPreference = 'Stop'\n"
                f"Import-Module {_q(SourceFramesOracleParityTests.MODULE)} -Force -DisableNameChecking\n"
                f"$Pub = {_q(pub)}\n"
                f"$rawLog = {_q(raw_log)}\n"
                "$measuredSmokeSessionId = '7'; $FixtureRehearsal = $false; $displayWake = [ordered]@{}; $displayBlock = $null\n"
                f"$runLog = [pscustomobject]@{{ runNonce = {_q(RUN_NONCE)} }}\n"
                "$SourceCommit = ('1' * 40); $ClipId = 'clip'; $ContactSheetEnabled = $false; $contactSheetDir = ''\n"
                "function Publish-AttrCudaContactSheetRawCaptures { param($Enabled, $SourceDir, $PubRoot) }\n"
                "function Save-Json($Object, [string]$Path) { [IO.File]::WriteAllText($Path, ($Object | ConvertTo-Json -Depth 20)) }\n"
                + block + "\nWrite-Output 'RESULT=FELL_THROUGH'\n",
                encoding="utf-8")
            proc = _pwsh(f"& {_q(script)}; exit $LASTEXITCODE")
            summary_path = pub / "summary.json"
            summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
            if proc.returncode == 0:
                self.assertIn("RESULT=FELL_THROUGH", proc.stdout, proc.stdout + proc.stderr)
            elif proc.returncode != 29:
                self.fail(f"job oracle probe exited {proc.returncode}: {proc.stdout}{proc.stderr}")
            return proc.returncode, summary

    def test_the_jobs_oracle_block_is_executed_and_mutation_tested(self) -> None:
        block = self._job_oracle_block()

        def line(**extra: object) -> str:
            fields = {**GOOD_SOURCE, "wrapped": 0, "wrap_count": 0, **extra}
            return _summary_line(**fields).replace("session=0", "session=7", 1)

        good = line()
        short = line(source_advanced=241, native_fps=24.0, pace_fps=12.0, fps_override=1)
        wrapped = line(wrapped=1, wrap_count=1)
        other_session = short.replace("session=7", "session=8", 1)
        # the last line FOR THE MEASURED SESSION decides; another session's short line is not this run's evidence
        code, summary = self._run_job_block(block, good + "\n" + other_session)
        self.assertEqual(code, 0, summary)
        code, summary = self._run_job_block(block, short)
        self.assertEqual(code, 29)
        self.assertEqual(summary["result"], "SOURCE_FRAMES_INVALID")
        self.assertEqual(summary["smokeRefusalReason"], "INVALID_SOURCE_FRAMES")
        self.assertEqual(summary["sourceFrames"]["sourceAdvanced"], 241)
        self.assertEqual(summary["sourceFrames"]["requiredSourceFrames"], 480)
        code, summary = self._run_job_block(block, wrapped)
        self.assertEqual(code, 29)
        self.assertEqual(summary["smokeRefusalReason"], "INVALID_LOOPED")
        code, summary = self._run_job_block(block, "")
        self.assertEqual(code, 29)                      # no summary line for the session at all -> fail closed
        mutations = {
            "guard disabled": ("if ($sourceFramesVerdict.invalid) {", "if ($false -and $sourceFramesVerdict.invalid) {"),
            "guard inverted": ("if ($sourceFramesVerdict.invalid) {", "if (-not $sourceFramesVerdict.invalid) {"),
            # HFR-NATIVE-M16-1457-FULLSCREEN-S1S2-PRESENT-GAP-1 r5: the effective summary pick is the anchored re-pick (SESSION-ANCHOR-1), which
            # replaces the baseline's substring pick kept verbatim above it, so the mutation targets the re-pick
            "any session's line accepted": ("($sourceFramesRecordStart + 'playback_smoke\\.summary session=' + [regex]::Escape([string]$measuredSmokeSessionId)",
                                            "($sourceFramesRecordStart + 'playback_smoke\\.summary session=' + ''"),
            "verdict ignored": ("$sourceFramesVerdict = Get-AttrCudaConformAwareSourceFramesVerdict -SummaryLine $sourceFramesSummaryLine -ExpectedRunNonce $runLog.runNonce -ConformLine $sourceFramesConformLine",
                                "$sourceFramesVerdict = [pscustomobject]@{ invalid = $false; wrapped = $false; failures = @(); sourceAdvanced = 0; requiredSourceFrames = 0 }"),
        }
        for name, (needle, replacement) in mutations.items():
            with self.subTest(name):
                self.assertIn(needle, block, f"mutation anchor vanished: {name}")
                mutated = block.replace(needle, replacement, 1)
                verdicts = [
                    self._run_job_block(mutated, short)[0] == 29,
                    self._run_job_block(mutated, good + "\n" + other_session)[0] == 0,
                ]
                self.assertFalse(all(verdicts), f"job oracle mutation '{name}' survived")

    def test_the_jobs_block_hands_the_measured_sessions_conform_line_to_the_oracle(self) -> None:
        """HFR-CONFORM-AWARE-RECEIPT-ORACLES-1: the job picks the playback_smoke.conform line of the MEASURED session (the last one wins) and the
        oracle relaxes the native-pace rule only on a line it can prove; the block is executed, its verdict recorded on the receipt, its wiring mutated."""
        block = self._job_oracle_block()
        hfr = {**GOOD_SOURCE, "wrapped": 0, "wrap_count": 0, "source_advanced": 1200, "required_source_frames": 1199,
               "native_fps": "59.940", "pace_fps": "24.000"}
        summary = _summary_line(**hfr).replace("session=0", "session=7", 1)
        own, other = _conform_line("7"), _conform_line("8")
        code, _ = self._run_job_block(block, summary + "\n" + own)
        self.assertEqual(code, 0)
        code, _ = self._run_job_block(block, summary + "\n" + own + "\n" + other)      # another session's line never replaces the measured one
        self.assertEqual(code, 0)
        for name, raw in (("no conform line", summary), ("another session's line only", summary + "\n" + other),
                          ("inactive", summary + "\n" + _conform_line("7", conform_active=0))):
            with self.subTest(name):
                code, receipt = self._run_job_block(block, raw)
                self.assertEqual(code, 29, name)
                self.assertEqual(receipt["sourceFrames"]["paceRule"], "native", name)
        needle = "-ConformLine $sourceFramesConformLine"
        self.assertIn(needle, block, "mutation anchor vanished: conform line not passed on")
        self.assertEqual(self._run_job_block(block.replace(needle, "", 1), summary + "\n" + own)[0], 29,
                         "job conform-wiring mutation 'conform line not passed on' survived")

    def test_r19_a1_the_source_frame_gate_reads_the_record_body_never_a_location_tail(self) -> None:
        """HFR-NATIVE-M16-1457-FULLSCREEN-S1S2-PRESENT-GAP-1 r19 (r18 adversary A1 on 1d2d2850): the job's anchored summary and conform picks are read as
        their record BODY by the Get-DvLogRecordBody rule (DualVenueRunner.psm1): the first ` (` that is not the summary's in-place conform group starts
        the Qt location, a readable `:<digits>)` / `:%2)` tail is cut, and any other tail makes the record unreadable (no pick, so the oracle refuses).
        A key=value inside the location path never supplies a field. The module oracle reads a handed-in line the same way (the class fix)."""
        block = self._job_oracle_block()

        def line(**extra: object) -> str:
            return _summary_line(**{**GOOD_SOURCE, "wrapped": 0, "wrap_count": 0, **extra}).replace("session=0", "session=7", 1)

        # the adversary's input, its summary as the app writes it: 1 of 600 source frames advanced, the location path holding source_advanced=600
        short = line(source_advanced=1, required_source_frames=600, native_fps=60, pace_fps=60)
        located = " (C:/build source_advanced=600 x/MainWindow.cpp:1)"
        code, receipt = self._run_job_block(block, short + located)
        self.assertEqual(code, 29, "a location tail supplied source_advanced over the record's own value")
        self.assertEqual(receipt["sourceFrames"]["sourceAdvanced"], 1)
        good = line()
        self.assertEqual(self._run_job_block(block, good + " (C:/b x/MainWindow.cpp:25848)")[0], 0, "a readable location keeps the body")
        self.assertEqual(self._run_job_block(block, good + " (C:/b %1 x/MainWindow.cpp:%2)")[0], 0, "the old writer's :%2) ending keeps the body")
        self.assertEqual(self._run_job_block(block, good + " (settings_store=venue)")[0], 29, "an unreadable tail is no readable summary")
        # the app's own conform group inside gui_fps_status_text is stepped over in its place, and the location after it is still cut
        # (_summary_line keeps the C++ literal's escaped quotes, \"; the app writes plain quotes)
        status = re.sub(r'gui_fps_status_text=\\?"0\\?"', lambda _m: 'gui_fps_status_text="Playback: 24 fps (59.94 -> 24)"', good, count=1)
        self.assertNotEqual(status, good, "the summary format moved; update this test")
        self.assertEqual(self._run_job_block(block, status + " (C:/b source_advanced=1 x/M.cpp:1)")[0], 0)
        # the conform sibling (job :3829): a location that says conform_active=1 never proves a conform the record denies
        hfr = _summary_line(**{**GOOD_SOURCE, "wrapped": 0, "wrap_count": 0, "source_advanced": 1200, "required_source_frames": 1199,
                               "native_fps": "59.940", "pace_fps": "24.000"}).replace("session=0", "session=7", 1)
        forged = _conform_line("7", conform_active=0) + " (C:/b conform_active=1 x/MainWindow.cpp:1)"
        code, receipt = self._run_job_block(block, hfr + "\n" + forged)
        self.assertEqual((code, receipt["sourceFrames"]["paceRule"]), (29, "native"), "a location tail proved a conform")
        self.assertEqual(self._run_job_block(block, hfr + "\n" + _conform_line("7") + " (C:/b x/MainWindow.cpp:9)")[0], 0)
        self.assertEqual(self._run_job_block(block, hfr + "\n" + _conform_line("7") + " (settings_store=venue)")[0], 29, "an unreadable conform tail proves nothing")
        # the module oracle on a raw line handed to it directly (every caller, not only the job's pick)
        proc = _pwsh(
            f"Import-Module {_q(self.MODULE)} -Force -DisableNameChecking; "
            f"$a = Get-AttrCudaConformAwareSourceFramesVerdict -SummaryLine {_q(short + located)} -ExpectedRunNonce {_q(RUN_NONCE)}; "
            f"$b = Get-AttrCudaConformAwareSourceFramesVerdict -SummaryLine {_q(hfr)} -ExpectedRunNonce {_q(RUN_NONCE)} -ConformLine {_q(forged)}; "
            "[pscustomobject]@{ a = [bool]$a.invalid; aAdvanced = $a.sourceAdvanced; b = [bool]$b.invalid; bRule = $b.paceRule } | ConvertTo-Json -Compress")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(json.loads(proc.stdout), {"a": True, "aAdvanced": 1, "b": True, "bRule": "native"})

    def test_the_job_calls_the_embedded_oracle_before_any_backend_verdict(self) -> None:
        job = JOB_GENERATOR.read_text(encoding="utf-8")
        self.assertIn("'Get-AttrCudaSourceFramesVerdict'", job)       # embedded into the emitted job
        oracle_at = job.index("Get-AttrCudaSourceFramesVerdict -SummaryLine")
        # after the backend-availability gate (a run that never used CUDA says so first), before any number is read
        self.assertGreater(oracle_at, job.index("$verdict = Get-AttrCudaEligibilityVerdict"))
        self.assertLess(oracle_at, job.index("$gpuSummary = Get-LastGpuSummary $rawLog $measuredSmokeSessionId"))
        self.assertLess(oracle_at, job.index("RESULT=$resultVerb"))
        # HFR-CONFORM-AWARE-RECEIPT-ORACLES-1: the bracketed conform-line pick and the receipt fields sit between the call and its refusal (+~1500 chars)
        self.assertIn("exit 29", job[oracle_at:oracle_at + 4000])
        self.assertIn("result='SOURCE_FRAMES_INVALID'", job)
        self.assertEqual(job.count("sourceFrames = $sourceFramesBlock"), 2)   # evidence manifest and summary.json


# ---------------------------------------------------------------------------------------------------
# HFR-CONFORM-AWARE-RECEIPT-ORACLES-1. PLAYBACK-HFR-CONFORM-DEFAULT-1 plays a clip above 30 fps as slow motion at a conform rate (24 fps by
# default), so the engine's pace_fps is that rate, not the clip's native fps, and the three receipt oracles' native-pace rule rejected every
# conformed run. Hub ruling: the owner's rule is 20 s of REAL FOOTAGE (the floor is never relaxed); the PACE rule is relaxed ONLY when the run
# PROVES it was conformed -- a present playback_smoke.conform line for the measured session with conform_active=1, clip_fps within 0.5 % of
# native_fps, playback_fps within 0.5 % of pace_fps. Absence never helps. The three oracles (gui-smoke gate, AttrCuda job module, dual-venue
# receipt reader) are carriers of ONE rule, so one case table is executed through all three.
def _conform_line(session: str = "7", **over: object) -> str:
    """The app's playback_smoke.conform line (PLAYBACK-HFR-CONFORM-DEFAULT-1, platform/qt/MainWindow.cpp); a key given as None is left out."""
    fields = dict(clip_fps="59.940", playback_fps="24.000", conform_active=1, conform_enabled=1, conform_target="24.000", conform_threshold="30.000",
                  export_fps_override=0, export_frame_rate=0, audio_sync_requests=0, presented=574, source_advanced=1200)
    fields.update(over)
    return " ".join(["playback_smoke.conform", f"session={session}"] + [f"{k}={v}" for k, v in fields.items() if v is not None])


DV_NONCE = "ab" * 16
HFR_RUN = dict(session=7, wrapped=0, wrap_count=0, source_advanced=1200, required_source_frames=1199, native_fps="59.940", pace_fps="24.000", fps_override=0)
PACE_TEXT = "the engine paced at pace_fps="
FLOOR_TEXT = "ceil(20 s x native_fps"
# PLAYBACK-CLIP-LENGTH-HFR-10S-1 (owner amendment 2026-10-15): a 59.94 clip's floor is 10 s, ceil(10 x 59.94) = 600 source frames.
HFR_FLOOR_TEXT = "ceil(10 s x native_fps"

# (name, summary fields, conform line or None, expected invalid, texts every oracle's failures must contain, texts none may contain, paceRule)
CONFORM_CASES = [
    ("a: the real ab404cff shape (600/600 on a 59.94 clip, conformed) is VALID under the HFR 10 s amendment",
     dict(HFR_RUN, source_advanced=600, required_source_frames=600), _conform_line(), False, [], [PACE_TEXT, FLOOR_TEXT, HFR_FLOOR_TEXT], "conform"),
    ("a2: 599/599 on a 59.94 clip (9.99 s) is INVALID on the HFR floor alone",
     dict(HFR_RUN, source_advanced=599, required_source_frames=599), _conform_line(), True, [HFR_FLOOR_TEXT], [PACE_TEXT], "conform"),
    ("b: a conformed run with 1199+ source frames is valid", HFR_RUN, _conform_line(), False, [], [PACE_TEXT, FLOOR_TEXT], "conform"),
    ("b2: exactly the floor (1199 = ceil(20 s x 59.94 - 0.02)) is valid", dict(HFR_RUN, source_advanced=1199), _conform_line(), False, [], [], "conform"),
    ("b3: the HFR floor is not relaxed by a long advance: required 599 is under ceil(10 s x 59.94)",
     dict(HFR_RUN, source_advanced=1198, required_source_frames=599), _conform_line(), True, [HFR_FLOOR_TEXT], [PACE_TEXT], "conform"),
    ("c: the same run without a conform line is INVALID native-pace (absence never helps)", HFR_RUN, None, True, [PACE_TEXT], ["conform"], "native"),
    ("d: conform_active=0 -> INVALID", HFR_RUN, _conform_line(conform_active=0), True, [PACE_TEXT, "conform_active=0"], [], "native"),
    ("e: playback_fps disagreeing with pace_fps -> INVALID", HFR_RUN, _conform_line(playback_fps="30.000"), True, [PACE_TEXT, "does not match pace_fps"], [], "native"),
    ("f: a conform line for another session -> INVALID", HFR_RUN, _conform_line("8"), True, [PACE_TEXT, "not the measured session"], [], "native"),
    ("g1: a non-HFR 23.976 run with no conform line is valid on the native pace",
     dict(HFR_RUN, source_advanced=480, required_source_frames=480, native_fps="23.976", pace_fps="23.976"), None, False, [], [], "native"),
    ("g2: a non-HFR 23.976 run paced at 30 with no conform line is INVALID native-pace",
     dict(HFR_RUN, source_advanced=480, required_source_frames=480, native_fps="23.976", pace_fps="30.000"), None, True, [PACE_TEXT + "30"], ["conform"], "native"),
    ("h1: a malformed conform line (no playback_fps) -> INVALID", HFR_RUN, _conform_line(playback_fps=None), True, [PACE_TEXT, "malformed"], [], "native"),
    ("h2: conform clip_fps off the native fps -> INVALID", HFR_RUN, _conform_line(clip_fps="29.970"), True, [PACE_TEXT, "does not match native_fps"], [], "native"),
    ("h3: conform never speeds a clip up (playback above clip) -> INVALID",
     dict(HFR_RUN, source_advanced=480, required_source_frames=480, native_fps="23.976", pace_fps="30.000"),
     _conform_line(clip_fps="23.976", playback_fps="30.000"), True, [PACE_TEXT, "never speeds"], [], "native"),
    ("h4: an fps override is still INVALID under a proven conform", dict(HFR_RUN, fps_override=1), _conform_line(), True, ["override"], [PACE_TEXT], "conform"),
    # MERGE-MASTER r4 (Grok G1 / adversary A3): NaN and Infinity parse as doubles and fail every ordered comparison, so before r4 the gui-smoke
    # and AttrCuda oracles PROVED a conform line carrying them (and accepted a NaN summary pace); every oracle now rejects them first.
    ("i1: conform playback_fps=NaN -> INVALID", HFR_RUN, _conform_line(playback_fps="NaN"), True, [PACE_TEXT, "conform relaxation not applied"], [], "native"),
    ("i2: conform playback_fps=Infinity -> INVALID", HFR_RUN, _conform_line(playback_fps="Infinity"), True, [PACE_TEXT, "conform relaxation not applied"], [], "native"),
    ("i3: conform playback_fps=-Infinity -> INVALID", HFR_RUN, _conform_line(playback_fps="-Infinity"), True, [PACE_TEXT, "conform relaxation not applied"], [], "native"),
    ("i4: conform clip_fps=NaN -> INVALID", HFR_RUN, _conform_line(clip_fps="NaN"), True, [PACE_TEXT, "conform relaxation not applied"], [], "native"),
    ("i5: conform clip_fps=Infinity -> INVALID", HFR_RUN, _conform_line(clip_fps="Infinity"), True, [PACE_TEXT, "conform relaxation not applied"], [], "native"),
    ("i6: summary pace_fps=NaN under a sound conform line -> INVALID", dict(HFR_RUN, pace_fps="NaN"), _conform_line(), True,
     ["pace_fps=NaN", "present engine pace that is not positive"], [], "native"),
    ("i7: summary pace_fps=NaN with no conform line -> INVALID", dict(HFR_RUN, pace_fps="NaN"), None, True, ["pace_fps=NaN", "present engine pace that is not positive"], [], "native"),
    ("i8: summary pace_fps=Infinity -> INVALID", dict(HFR_RUN, pace_fps="Infinity"), _conform_line(), True, ["present engine pace that is not positive"], [], "native"),
    ("i9: summary native_fps=NaN -> INVALID", dict(HFR_RUN, native_fps="NaN"), _conform_line(), True, ["native_fps=NaN", "native frame rate is unknown"], [], "native"),
    ("i10: summary native_fps=Infinity -> INVALID", dict(HFR_RUN, native_fps="Infinity"), _conform_line(), True, ["native frame rate is unknown"], [], "native"),
    # MERGE-MASTER r7 (sol r6 B1, Dot ask 6545453593): sol's literal row. conform_active=1 with clip = playback = native = pace = 59.94 (target 24, threshold
    # 30 logged) is native-rate playback: no conform happened, so no oracle may PROVE a conform from it (paceRule native). Before r7 all three proved it.
    # The source frames here are native-paced and enough, so the source-frame verdict itself is valid; a REQUESTED conform-on arm is INVALID
    # (ConformAwareReceiptOracleTests.test_a_requested_conform_arm_at_the_native_rate_is_never_honoured).
    ("j1: sol r6 B1 -- conform_active=1 at the native rate (clip = playback = native = pace = 59.94) proves no conform",
     dict(HFR_RUN, session=3, source_advanced=1499, required_source_frames=1499, native_fps="59.940", pace_fps="59.940"),
     _conform_line("3", clip_fps="59.940", playback_fps="59.940", conform_env_override=1), False, [], [PACE_TEXT], "native"),
    ("j2: a sound conform well inside the clip rate (59.94 clip played at 50) stays proven",
     dict(HFR_RUN, pace_fps="50.000"), _conform_line(playback_fps="50.000", conform_target="50.000"), False, [], [PACE_TEXT], "conform"),
    ("j3: a 23.976 clip whose conform line claims active=1 at 23.976 proves no conform (the native rule judges it)",
     dict(HFR_RUN, source_advanced=480, required_source_frames=480, native_fps="23.976", pace_fps="23.976"),
     _conform_line(clip_fps="23.976", playback_fps="23.976"), False, [], [], "native"),
    # MERGE-MASTER r8 (sol r7 B1, issuecomment-6549349607): sol's literal row. native = pace = 59.94, clip 60, playback 59.69, conform_active=1. Each tolerance
    # passed on its own (clip/native 0.06, playback/pace 0.25, 59.69 < 60 x 0.995), so before r8 all three oracles PROVED a conform from native engine pace.
    # Now the clip must equal native at print precision and the engine pace must sit below native by more than the margin: no proof, the native rule judges.
    ("k1: sol r7 B1 -- stacked tolerances: native = pace = 59.94, clip 60, playback 59.69, active=1 proves no conform",
     dict(HFR_RUN, session=3, source_advanced=1499, required_source_frames=1499, native_fps="59.940", pace_fps="59.940"),
     _conform_line("3", clip_fps="60.000", playback_fps="59.690", conform_env_override=1), False, [], [PACE_TEXT], "native"),
    # key3 CONFORM-PROOF-CLIP-NATIVE-EXACT-1: clip_fps and native_fps print one value to 3 decimals; a real 24-fps conform whose clip is off native by even one
    # print unit is not proven (before r8 the 0.5 % tolerance proved it), so the native rule judges pace 24 against 59.94.
    ("k2: clip_fps 60.000 vs native 59.940 under a real 24-fps conform proves no conform",
     HFR_RUN, _conform_line(clip_fps="60.000"), True, [PACE_TEXT, "does not match native_fps"], [], "native"),
    ("k3: clip_fps one print unit off native (59.941 vs 59.940) proves no conform",
     HFR_RUN, _conform_line(clip_fps="59.941"), True, [PACE_TEXT, "does not match native_fps"], [], "native"),
    # The engine-pace check alone: clip = native = 59.94, playback 59.63 (below 59.94 x 0.995 = 59.6403) agrees with pace 59.90 (|0.27| <= 0.2995), but the
    # engine paced within 0.5 % of native. Before r8 this proved a conform; now no proof, and the native rule passes it (pace 59.90 vs native 59.94).
    ("k4: engine pace within 0.5 % of native (59.90 vs 59.94) proves no conform even with playback 59.63 below the clip margin",
     dict(HFR_RUN, pace_fps="59.900"), _conform_line(playback_fps="59.630"), False, [], [PACE_TEXT], "native"),
    # The r7 playback/clip check alone (the engine-pace check would pass it): playback 59.65 is within 0.5 % of clip 59.94, the engine paced at 59.40
    # (|0.25| <= 0.297). No proof, so the native rule judges pace 59.40 against 59.94 and names why the relaxation was refused.
    ("k5: playback within 0.5 % of the clip rate proves no conform even with the engine pace below the native margin",
     dict(HFR_RUN, pace_fps="59.400"), _conform_line(playback_fps="59.650"), True, [PACE_TEXT, "indistinguishable from native pace"], [], "native"),
]


def _run_oracle_table(script_body: str, cases: list[dict]) -> list[dict]:
    with tempfile.TemporaryDirectory(prefix="conform-oracle-") as tmp:
        table = Path(tmp) / "cases.json"
        table.write_text(json.dumps(cases), encoding="utf-8")
        proc = _pwsh(f"$cases = @(Get-Content -Raw -LiteralPath {_q(table)} | ConvertFrom-Json)\n{script_body}\n"
                     "ConvertTo-Json -Compress -Depth 4 -InputObject @($out)")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout)


def _dv_block(summary: dict, conform: str | None) -> dict:
    """The receipt `playback` block Get-DvPlaybackEvidence would record from these run-log lines (the conform fields mirror the log line).
    A non-finite rate stays its log text ('NaN', 'Infinity'): JSON has no NaN, and ConvertTo-DvDouble parses the text to the same double."""
    def rate(v: object) -> float | str:
        x = float(v)  # type: ignore[arg-type]
        return x if math.isfinite(x) else str(v)
    block = dict(sourceAdvanced=int(summary["source_advanced"]), requiredSourceFrames=int(summary["required_source_frames"]),
                 nativeFps=rate(summary["native_fps"]), paceFps=rate(summary["pace_fps"]), fpsOverride=int(summary["fps_override"]),
                 wrapped=False, wrapCount=0, expectedRunNonce=DV_NONCE, observedRunNonce=DV_NONCE, manifestRunNonce=DV_NONCE,
                 logShaBound=True, settingsIsolated=True, jobFailures=[], fixtureRehearsal=False, clipId="CLIP",
                 summarySession=str(summary["session"]), conformLinePresent=conform is not None)
    if conform is not None:
        f = dict(re.findall(r"(\w+)=(\S+)", conform))
        block.update(conformSession=f.get("session"), conformActive=int(f["conform_active"]) if "conform_active" in f else None,
                     conformClipFps=rate(f["clip_fps"]) if "clip_fps" in f else None,
                     conformPlaybackFps=rate(f["playback_fps"]) if "playback_fps" in f else None)
    return block


@requires_pwsh
class ConformAwareReceiptOracleTests(unittest.TestCase):
    """HFR-CONFORM-AWARE-RECEIPT-ORACLES-1: one case table, executed through all three receipt oracles."""

    MODULE = SourceFramesOracleParityTests.MODULE
    DV_MODULE = PROFILING / "dual-venue" / "DualVenueRunner.psm1"

    @classmethod
    def setUpClass(cls) -> None:
        rows = [{"summary": _summary_line(**summary), "conform": conform, "block": _dv_block(summary, conform)} for _n, summary, conform, *_r in CONFORM_CASES]
        cls.gate = _run_oracle_table(
            f". {_q(GATE)}\n$out = foreach ($c in $cases) {{ $s = Convert-PlaybackLogLineToObject $c.summary; "
            f"$v = Get-GuiSmokeSourceFramesVerdict -Summary $s -ExpectedRunNonce {_q(RUN_NONCE)} -ConformLine $c.conform; "
            "[pscustomobject]@{ invalid = [bool]$v.invalid; failures = @($v.failures); paceRule = $v.paceRule } }", rows)
        cls.attr = _run_oracle_table(
            f"Import-Module {_q(cls.MODULE)} -Force -DisableNameChecking\n$out = foreach ($c in $cases) {{ "
            f"$v = Get-AttrCudaConformAwareSourceFramesVerdict -SummaryLine $c.summary -ExpectedRunNonce {_q(RUN_NONCE)} -ConformLine $c.conform; "
            "[pscustomobject]@{ invalid = [bool]$v.invalid; failures = @($v.failures); paceRule = $v.paceRule } }", rows)
        cls.dv = _run_oracle_table(
            f"Import-Module {_q(cls.DV_MODULE)} -Force -DisableNameChecking\n$out = foreach ($c in $cases) {{ "
            "$p = @(Get-DvPlaybackProblems -Playback $c.block -ExpectedClipId 'CLIP'); $proof = Get-DvConformPaceProof -Playback $c.block; "
            "[pscustomobject]@{ invalid = ($p.Count -gt 0); failures = $p; paceRule = $(if ($proof.proven) { 'conform' } else { 'native' }) } }", rows)

    def test_every_case_is_judged_as_the_hub_ruling_says_by_each_of_the_three_oracles(self) -> None:
        for index, (name, _summary, _conform, invalid, must, must_not, _rule) in enumerate(CONFORM_CASES):
            for carrier, results in (("gui-smoke gate", self.gate), ("AttrCuda module", self.attr), ("dual-venue reader", self.dv)):
                with self.subTest(case=name, oracle=carrier):
                    got = results[index]
                    text = " | ".join(got["failures"])
                    self.assertEqual(got["invalid"], invalid, text)
                    for needle in must:
                        self.assertIn(needle, text)
                    for needle in must_not:
                        self.assertNotIn(needle, text)

    def test_parity_the_three_oracles_agree_on_valid_invalid_and_the_pace_rule_in_every_case(self) -> None:
        for index, (name, *_rest, rule) in enumerate(CONFORM_CASES):
            with self.subTest(case=name):
                got = [r[index] for r in (self.gate, self.attr, self.dv)]
                self.assertEqual({g["invalid"] for g in got}, {CONFORM_CASES[index][3]}, got)
                self.assertEqual([g["paceRule"] for g in got], [rule] * 3, got)

    def test_the_real_ab404cff_receipt_is_valid_under_the_hfr_amendment(self) -> None:
        """The receipt PLAYBACK-HFR-CONFORM-DEFAULT-1's Bachelor leg wrote: 600/600, native 59.94, pace 24, conformed. Before the owner's
        2026-10-15 amendment its one remaining reason was the 20 s floor (reason two, the pace, went with the conform proof); 600 source
        frames ARE 10 s of 59.94 footage, so PLAYBACK-CLIP-LENGTH-HFR-10S-1 clears reason one too -- and one frame less is still INVALID."""
        for results in (self.gate, self.attr, self.dv):
            self.assertEqual(results[0]["failures"], [], results[0])
            self.assertFalse(results[0]["invalid"], results[0])
            self.assertEqual(len(results[1]["failures"]), 1, results[1])
            self.assertIn("required_source_frames=599 is under ceil(10 s x native_fps=59.94)", results[1]["failures"][0])

    def test_an_unproven_conform_line_says_why_the_relaxation_was_not_applied(self) -> None:
        for results in (self.gate, self.attr, self.dv):
            self.assertIn("conform relaxation not applied", " | ".join(results[6]["failures"]))      # d: inactive
            self.assertNotIn("conform relaxation", " | ".join(results[5]["failures"]))               # c: no line at all keeps the native text unchanged

    def test_a_requested_conform_arm_at_the_native_rate_is_never_honoured(self) -> None:
        """MERGE-MASTER r7 (sol r6 B1, Dot ask 6545453593): sol's literal repro through the dual-venue arm verdict and the playback block with
        playbackConform=on: session=3, sourceAdvanced=requiredSourceFrames=1499, native=pace=clip=playback=59.94, conformActive=1,
        conformEnvOverride=1. Before r7: proven=true, honoured=true, valid=true. Now: not proven (no conform happened), not honoured, INVALID.
        Controls: the sound 24-fps conform stays proven/honoured/valid, and the conform-OFF native arm stays honoured."""
        sol = dict(conformLinePresent=True, summarySession="3", conformSession="3", conformActive=1, conformEnvOverride=1, conformClipFps=59.94,
                   conformPlaybackFps=59.94, nativeFps=59.94, paceFps=59.94, sourceAdvanced=1499, requiredSourceFrames=1499, fpsOverride=0, wrapped=False,
                   wrapCount=0, expectedRunNonce=DV_NONCE, observedRunNonce=DV_NONCE, manifestRunNonce=DV_NONCE, logShaBound=True, settingsIsolated=True,
                   jobFailures=[], fixtureRehearsal=False, clipId="CLIP")
        # MERGE-MASTER r8 (sol r7 B1): sol's stacked-tolerance repro (clip 60 / playback 59.69 / native = pace = 59.94), the engine-pace-only shape,
        # and key3's near-native reason (CONFORM-PROOF-NEAR-NATIVE-REASON-1: clip 30.1 played at 30 is indistinguishable from native pace, not "no conform").
        rows = [("sol-on", "on", sol), ("sound-on", "on", dict(sol, conformPlaybackFps=24.0, paceFps=24.0)),
                ("native-off", "off", dict(sol, conformActive=0, conformEnvOverride=0)),
                ("sol-r7-on", "on", dict(sol, conformClipFps=60.0, conformPlaybackFps=59.69)),
                ("pace-native-on", "on", dict(sol, conformPlaybackFps=59.63, paceFps=59.90)),
                ("near-native-on", "on", dict(sol, conformClipFps=30.1, nativeFps=30.1, conformPlaybackFps=30.0, paceFps=30.0, requiredSourceFrames=602,
                                              sourceAdvanced=602))]
        got = {r["name"]: r for r in _run_oracle_table(
            f"Import-Module {_q(self.DV_MODULE)} -Force -DisableNameChecking\n$out = foreach ($c in $cases) {{ "
            "$spec = [pscustomobject]@{ generatorArgs = [pscustomobject]@{ playbackConform = $c.arm } }; $proof = Get-DvConformPaceProof -Playback $c.block; "
            "$arm = Get-DvConformArmVerdict -Spec $spec -Playback $c.block; "
            "$p = @(Get-DvPlaybackProblems -Playback $c.block -ExpectedClipId 'CLIP' -RequestedConform $c.arm); "
            "[pscustomobject]@{ name = $c.name; proven = [bool]$proof.proven; why = [string]$proof.reason; honoured = [bool]$arm.honoured; "
            "blocks = [bool]$arm.blocks; armReason = [string]$arm.reason; valid = ($p.Count -eq 0); problems = @($p) } }",
            [{"name": n, "arm": a, "block": b} for n, a, b in rows])}
        s = got["sol-on"]
        self.assertEqual((s["proven"], s["honoured"], s["blocks"], s["valid"]), (False, False, True, False), s)
        self.assertIn("no conform is proven", s["why"])
        self.assertIn("indistinguishable from native pace", s["armReason"])
        self.assertTrue(any("CONFORM_PACE_NOT_PROVEN" in p and "no conform is proven" in p for p in s["problems"]), s["problems"])
        self.assertEqual((got["sound-on"]["proven"], got["sound-on"]["honoured"], got["sound-on"]["valid"]), (True, True, True), got["sound-on"])
        self.assertEqual((got["native-off"]["honoured"], got["native-off"]["valid"]), (True, True), got["native-off"])
        # r8: sol's r7 repro -- before r8 proven=true, honoured=true, blocks=false, valid=true.
        r7 = got["sol-r7-on"]
        self.assertEqual((r7["proven"], r7["honoured"], r7["blocks"], r7["valid"]), (False, False, True, False), r7)
        self.assertIn("clip_fps=60 does not match native_fps=59.94", r7["why"])
        self.assertTrue(any("CONFORM_PACE_NOT_PROVEN" in p for p in r7["problems"]), r7["problems"])
        pn = got["pace-native-on"]
        self.assertEqual((pn["proven"], pn["honoured"], pn["blocks"], pn["valid"]), (False, False, True, False), pn)
        self.assertIn("pace_fps=59.9 is within 0.5 % of native_fps=59.94: the engine ran at native pace", pn["why"])
        nn = got["near-native-on"]
        self.assertEqual((nn["proven"], nn["honoured"]), (False, False), nn)
        self.assertIn("is within 0.5 % of clip_fps=30.1: indistinguishable from native pace, so no conform is proven", nn["why"])
        self.assertNotIn("no conform happened", nn["why"])

    def test_the_three_conform_proofs_share_one_reduction_margin_and_one_print_tolerance(self) -> None:
        """MERGE-MASTER r8 (sol r7 B1): the 'measurably below native' margin and the clip/native print tolerance are named constants mirrored in the three
        siblings; this pins them equal, single-assigned, and used by every check that needs them (a sibling that drifts or inlines a literal goes red)."""
        uses = {"$conformReductionMargin": 3, "$conformPrintTolerance": 2}
        values: dict[str, set[str]] = {name: set() for name in uses}
        proofs = ((GATE, "Get-GuiSmokeConformPaceProof"), (self.MODULE, "Get-AttrCudaConformAwareSourceFramesVerdict"), (self.DV_MODULE, "Get-DvConformPaceProof"))
        for path, function in proofs:
            whole = path.read_text(encoding="utf-8")
            start = whole.index(f"function {function} {{")
            end = whole.find("\nfunction ", start + 1)
            text = whole[start:] if end < 0 else whole[start:end]
            for name, min_count in uses.items():
                with self.subTest(file=path.name, constant=name):
                    assigned = re.findall(re.escape(name) + r" = ([0-9.]+)\b", text)
                    self.assertEqual(len(assigned), 1, f"{name} must be assigned exactly once in {path.name}: {assigned}")
                    values[name].add(assigned[0])
                    self.assertGreaterEqual(text.count(name), min_count, f"{name} is assigned but not used by every check in {path.name}")
            for literal in ("0.995", "(0.005 * $native)"):
                self.assertNotIn(literal, text, f"{path.name} inlines a conform-proof margin instead of the named constant")
        self.assertEqual(values, {"$conformReductionMargin": {"0.005"}, "$conformPrintTolerance": {"0.0005"}})

    def test_a_non_boolean_conform_line_flag_is_absent_never_present_and_never_a_throw(self) -> None:
        """MERGE-MASTER r8 (fable r7 hardening / producer adversary A3): conformLinePresent that is not a real boolean (an array, a string) is ABSENT to both the
        proof and the requested-arm verdict -- never a throw at [int]$present and never a present line that proves a conform."""
        sound = dict(conformLinePresent=True, summarySession="3", conformSession="3", conformActive=1, conformEnvOverride=1, conformClipFps=59.94,
                     conformPlaybackFps=24.0, nativeFps=59.94, paceFps=24.0)
        rows = [("bool", True), ("empty-array", []), ("array-false", [False]), ("array-true", [True]), ("string", "true")]
        got = {r["name"]: r for r in _run_oracle_table(
            f"Import-Module {_q(self.DV_MODULE)} -Force -DisableNameChecking\n$spec = [pscustomobject]@{{ generatorArgs = [pscustomobject]@{{ playbackConform = 'on' }} }}\n"
            "$out = foreach ($c in $cases) { try { $proof = Get-DvConformPaceProof -Playback $c.block; $arm = Get-DvConformArmVerdict -Spec $spec -Playback $c.block; "
            "[pscustomobject]@{ name = $c.name; proven = [bool]$proof.proven; present = [bool]$proof.present; blocks = [bool]$arm.blocks; err = '' } } "
            "catch { [pscustomobject]@{ name = $c.name; proven = $null; present = $null; blocks = $null; err = [string]$_ } } }",
            [{"name": n, "block": dict(sound, conformLinePresent=v)} for n, v in rows])}
        self.assertEqual((got["bool"]["proven"], got["bool"]["blocks"]), (True, False), got["bool"])
        for name, _value in rows[1:]:
            with self.subTest(name):
                self.assertEqual(got[name]["err"], "", got[name])
                self.assertEqual((got[name]["proven"], got[name]["present"], got[name]["blocks"]), (False, False, True), got[name])

    def test_the_dual_venue_evidence_block_records_the_conform_fields_and_which_rule_judged_the_pace(self) -> None:
        """Get-DvPlaybackEvidence end to end, from the run log: the conform line's values and paceRule land on the block and a key can re-derive them."""
        import hashlib

        def evidence(log_lines: list[str]) -> dict:
            log = "\n".join(log_lines) + "\n"
            result = {"evidence": {"runNonce": DV_NONCE, "runLogSnapshot": {"sha256": hashlib.sha256(log.encode()).hexdigest()}}}
            proc = _pwsh(
                f"Import-Module {_q(self.DV_MODULE)} -Force -DisableNameChecking\n"
                f"$res = {_q(json.dumps(result))} | ConvertFrom-Json\n"
                f"$sum = [pscustomobject]@{{ clipId = 'CLIP'; fixtureRehearsal = $false }}\n"
                f"Get-DvPlaybackEvidence -Summary $sum -EvidenceManifest $null -ResultJson $res -LogText {_q(log)} "
                f"-LogSha256 {_q(hashlib.sha256(log.encode()).hexdigest())} -ExpectedClipId 'CLIP' | ConvertTo-Json -Compress -Depth 6")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            return json.loads(proc.stdout)

        summary = _summary_line(**dict(HFR_RUN, run_nonce=DV_NONCE))
        base = ["playback_smoke.measured_session id=7", "interaction_trace event=automation.pacing_isolated settings_store=run_scoped", summary]
        conformed = evidence(base + [_conform_line("7"), _conform_line("8", conform_active=0)])
        self.assertTrue(conformed["valid"], conformed["invalidReasons"])
        self.assertEqual((conformed["paceRule"], conformed["conformPlaybackFps"], conformed["conformClipFps"], conformed["conformActive"],
                          conformed["conformSession"], conformed["summarySession"]), ("conform", 24.0, 59.94, 1, "7", "7"))
        bare = evidence(base)
        self.assertFalse(bare["valid"])
        self.assertEqual((bare["paceRule"], bare["conformLinePresent"]), ("native", False))
        self.assertTrue(any(PACE_TEXT in r for r in bare["invalidReasons"]), bare["invalidReasons"])
        # PLAYBACK-CLIP-LENGTH-HFR-10S-1: the ab404cff shape (600/600) is 10 s of 59.94 footage, valid end to end; one frame less is the
        # HFR floor's one reason.
        hfr10 = evidence([base[0], base[1], _summary_line(**dict(HFR_RUN, run_nonce=DV_NONCE, source_advanced=600, required_source_frames=600)), _conform_line("7")])
        self.assertTrue(hfr10["valid"], hfr10["invalidReasons"])
        short = evidence([base[0], base[1], _summary_line(**dict(HFR_RUN, run_nonce=DV_NONCE, source_advanced=599, required_source_frames=599)), _conform_line("7")])
        self.assertEqual(len(short["invalidReasons"]), 1, short["invalidReasons"])
        self.assertIn(HFR_FLOOR_TEXT, short["invalidReasons"][0])

    def test_mutation_a_conform_blind_pace_rule_or_a_relaxed_floor_is_caught(self) -> None:
        """Each oracle's relaxation switch is mutated; the case table must flag it (a green mutant means the table does not pin that arm)."""
        mutants = {
            "gate: conform never relaxes": (GATE, "if ($conformProof.proven) {", "if ($false) {"),
            "gate: conform always relaxes": (GATE, "if ($conformProof.proven) {", "if ($true) {"),
            "gate: inactive line accepted": (GATE, "} elseif ($active -ne 1) {", "} elseif ($false) {"),
            "gate: session not checked": (GATE, "$out.session -cne [string]$SummarySession", "$false"),
            "attr: conform never relaxes": (self.MODULE, "if ($conformProven) { $judged =", "if ($false) { $judged ="),
            "attr: inactive line accepted": (self.MODULE, "} elseif ($cn['conform_active'] -ne 1) {", "} elseif ($false) {"),
            "dv: conform never relaxes": (self.DV_MODULE, "if ($conformProof.proven) {\n            $conformPlayback", "if ($false) {\n            $conformPlayback"),
            "dv: inactive line accepted": (self.DV_MODULE, "if ($active -ne 1) {", "if ($false) {"),
            "attr: session not checked": (self.MODULE, "[string]$cf['session'] -cne $summarySession", "$false"),
            "dv: session not checked": (self.DV_MODULE, "$conformSession -cne $summarySession", "$false"),
            # MERGE-MASTER r7 (sol r6 B1): drop the 'no conform happened' check and row j1 proves a conform again.
            "gate: native-rate conform accepted": (GATE, "} elseif ($play -ge ($clip * (1 - $conformReductionMargin))) {", "} elseif ($false) {"),
            "attr: native-rate conform accepted": (self.MODULE, "} elseif ($conformPlaybackFps -ge ($conformSourceFps * (1 - $conformReductionMargin))) {", "} elseif ($false) {"),
            "dv: native-rate conform accepted": (self.DV_MODULE, "if ($play -ge ($clip * (1 - $conformReductionMargin))) {", "if ($false) {"),
            # MERGE-MASTER r8 (sol r7 B1): drop the engine-pace check and k1 / k4 prove a conform again; relax the clip/native match back to the 0.5 %
            # tolerance (key3 CONFORM-PROOF-CLIP-NATIVE-EXACT-1) and k2 / k3 prove a conform again.
            "gate: native engine pace accepted": (GATE, "} elseif ($pace -ge ($native * (1 - $conformReductionMargin))) {", "} elseif ($false) {"),
            "attr: native engine pace accepted": (self.MODULE, "} elseif ($pace -ge ($native * (1 - $conformReductionMargin))) {", "} elseif ($false) {"),
            "dv: native engine pace accepted": (self.DV_MODULE, "if ($pace -ge ($native * (1 - $conformReductionMargin))) {", "if ($false) {"),
            "gate: clip off native tolerated": (GATE, "[Math]::Abs($clip - $native) -gt $conformPrintTolerance", "[Math]::Abs($clip - $native) -gt (0.005 * $native)"),
            "attr: source off native tolerated": (self.MODULE, "[Math]::Abs($conformSourceFps - $native) -gt $conformPrintTolerance",
                                                  "[Math]::Abs($conformSourceFps - $native) -gt (0.005 * $native)"),
            "dv: clip off native tolerated": (self.DV_MODULE, "[Math]::Abs($clip - $native) -gt $conformPrintTolerance", "[Math]::Abs($clip - $native) -gt (0.005 * $native)"),
        }
        rows = [{"summary": _summary_line(**summary), "conform": conform, "block": _dv_block(summary, conform)} for _n, summary, conform, *_r in CONFORM_CASES]
        with tempfile.TemporaryDirectory(prefix="conform-mutant-") as tmp:
            for name, (path, needle, replacement) in mutants.items():
                with self.subTest(name):
                    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
                    self.assertIn(needle, text, f"mutation anchor vanished: {name}")
                    mutant = Path(tmp) / f"{abs(hash(name))}{path.suffix}"
                    mutant.write_text(text.replace(needle, replacement, 1), encoding="utf-8")
                    if name.startswith("gate"):
                        body = (f". {_q(mutant)}\n$out = foreach ($c in $cases) {{ $s = Convert-PlaybackLogLineToObject $c.summary; "
                                f"$v = Get-GuiSmokeSourceFramesVerdict -Summary $s -ExpectedRunNonce {_q(RUN_NONCE)} -ConformLine $c.conform; "
                                "[pscustomobject]@{ invalid = [bool]$v.invalid; paceRule = $v.paceRule } }")
                    elif name.startswith("attr"):
                        body = (f"Import-Module {_q(mutant)} -Force -DisableNameChecking\n$out = foreach ($c in $cases) {{ "
                                f"$v = Get-AttrCudaConformAwareSourceFramesVerdict -SummaryLine $c.summary -ExpectedRunNonce {_q(RUN_NONCE)} -ConformLine $c.conform; "
                                "[pscustomobject]@{ invalid = [bool]$v.invalid; paceRule = $v.paceRule } }")
                    else:
                        body = (f"Import-Module {_q(mutant)} -Force -DisableNameChecking\n$out = foreach ($c in $cases) {{ "
                                "$p = @(Get-DvPlaybackProblems -Playback $c.block -ExpectedClipId 'CLIP'); $proof = Get-DvConformPaceProof -Playback $c.block; "
                                "[pscustomobject]@{ invalid = ($p.Count -gt 0); paceRule = $(if ($proof.proven) { 'conform' } else { 'native' }) } }")
                    got = _run_oracle_table(body, rows)
                    wanted = [(c[3], c[6]) for c in CONFORM_CASES]
                    self.assertNotEqual([(g["invalid"], g["paceRule"]) for g in got], wanted, f"mutant '{name}' survived the case table")


class JobGeneratorContractTests(unittest.TestCase):
    """4. The job generator passes the play window through and never hard-codes 40."""

    def test_the_emitted_command_uses_the_play_window_not_a_literal(self) -> None:
        text = JOB_GENERATOR.read_text(encoding="utf-8")
        self.assertNotRegex(text, r"-Seconds\s+40\b")
        self.assertIn("-Seconds $PlaySeconds", text)
        self.assertIn("[int]$PlaySeconds = 25", text)
        self.assertIn("$PlaySeconds = __PLAY_SECONDS__", text)

    def test_the_play_window_floor_is_the_owner_minimum(self) -> None:
        text = JOB_GENERATOR.read_text(encoding="utf-8")
        # PLAYBACK-CLIP-LENGTH-HFR-10S-1: the generator never opens an owner clip, so it admits the lowest floor (an HFR clip's 10 s);
        # the runner applies the clip's own floor from its header at the venue.
        self.assertRegex(text, r"\[ValidateRange\(10, 3600\)\]\s*\[int\]\$PlaySeconds")

    def test_a_fixture_id_is_refused_at_generation_with_the_shared_verdict(self) -> None:
        text = JOB_GENERATOR.read_text(encoding="utf-8")
        self.assertIn("gui-smoke-length-gate.ps1", text)
        self.assertIn("PLAYBACK_ATTR3_$($fixtureLengthGate.message)", text)


# ---- 4b. the option classes: the app's own option tables vs the gate's policy -----------------------

MAIN_CPP = ROOT / "platform" / "qt" / "main.cpp"
_OPTION = re.compile(
    r'QCommandLineOption(?:\s+\w+)?\(\s*(?:QStringList\(\)\s*<<\s*)?QStringLiteral\("([^"]+)"\)'
    r'(?:\s*<<\s*QStringLiteral\("([^"]+)"\))?')


def _declared_options(function_signature: str) -> set[str]:
    """Every option name (long and short) the named parser function in main.cpp declares."""
    text = MAIN_CPP.read_text(encoding="utf-8")
    start = text.index(function_signature)
    end = text.index("parser.process(app);", start)
    names: set[str] = set()
    for match in _OPTION.finditer(text[start:end]):
        names.update(group for group in match.groups() if group)
    return names


# What the class test PINS (independent of the gate's own tables): which app options a pass-through
# argument may never carry. Changing this set is a decision a reviewer reads.
SMOKE_REFUSED = {
    "gui-smoke-playback", "i", "input", "seconds", "start-frame", "presented-frames", "loop", "launch-only",
    "exercise-clip-lifecycle-stress", "stress-switch-input", "stress-switch-at-ms", "stress-seek-frame",
}
PROFILE_PLAY = {"exercise-play-action", "exercise-look-assist-toggle", "exercise-look-assist-settle"}
PROFILE_REFUSED = {"profile-playback", "i", "input"}


def _policy(name: str) -> dict:
    proc = _pwsh(f". {_q(GATE)}; ${name} | ConvertTo-Json -Compress")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout)


def _pass_through(arguments: list[str], context: str = "gui-smoke") -> dict:
    array = "@(" + ",".join(_q(a) for a in arguments) + ")" if arguments else "@()"
    proc = _pwsh(
        f". {_q(GATE)}; Test-GuiSmokePassThroughArguments -Arguments {array} -Context {context} "
        "| ConvertTo-Json -Compress")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout)


@requires_pwsh
class AppOptionClassTests(unittest.TestCase):
    """The class: enumerate EVERY option the app's playback parsers accept and pin which are refused."""

    def test_the_smoke_policy_classifies_exactly_the_options_the_app_declares(self) -> None:
        declared = _declared_options("static int runGuiPlaybackSmoke(")
        self.assertIn("loop", declared)
        self.assertIn("launch-only", declared)
        policy = _policy("script:GuiSmokeOptionPolicy")
        self.assertEqual(
            sorted(declared - set(policy)), [],
            "the app declares gui-smoke options the gate's policy does not classify; add each to "
            "$script:GuiSmokeOptionPolicy in gui-smoke-length-gate.ps1 (refuse unless it cannot change "
            "what is played or for how long)")
        self.assertEqual(sorted(set(policy) - declared), [], "the policy names options the app no longer declares")

    def test_the_seek_frame_list_is_allowed_because_it_never_plays(self) -> None:
        # CONTACT-SHEET-SEEK-FRAME-LIST-APP-1: the list only picks which frames the seek capture AFTER the
        # stop lands on; it never plays and never changes what is played or for how long.
        self.assertIn("contact-sheet-seek-frames", _declared_options("static int runGuiPlaybackSmoke("))
        policy = _policy("script:GuiSmokeOptionPolicy")
        self.assertEqual(policy.get("contact-sheet-seek-frames"), "allow")
        ok = _pass_through(["--contact-sheet-seek-frames", "50,150"])
        self.assertEqual(ok["verdict"], "OK", ok)

    def test_the_smoke_refused_set_is_pinned(self) -> None:
        policy = _policy("script:GuiSmokeOptionPolicy")
        self.assertEqual({k for k, v in policy.items() if v == "refuse"}, SMOKE_REFUSED)
        self.assertEqual({v for v in policy.values()}, {"refuse", "allow"})

    def test_the_profile_policy_classifies_exactly_the_options_the_app_declares(self) -> None:
        declared = _declared_options("static int runPlaybackProfile(")
        policy = _policy("script:PlaybackProfileOptionPolicy")
        self.assertEqual(sorted(declared - set(policy)), [], "unclassified --profile-playback options")
        self.assertEqual(sorted(set(policy) - declared), [], "stale profile policy entries")
        self.assertEqual({k for k, v in policy.items() if v == "play"}, PROFILE_PLAY)
        self.assertEqual({k for k, v in policy.items() if v == "refuse"}, PROFILE_REFUSED)

    def test_every_pinned_option_is_refused_by_the_real_function_in_every_spelling(self) -> None:
        for name in sorted(SMOKE_REFUSED):
            for spelling in (f"--{name}", f"-{name}", f"--{name.upper()}", f"/{name}", f"--{name}=1"):
                with self.subTest(option=name, spelling=spelling):
                    verdict = _pass_through([spelling])
                    self.assertEqual(verdict["verdict"], "PASS_THROUGH_REFUSED", verdict)
                    self.assertEqual(verdict["option"], name)

    def test_loop_is_refused_in_the_profile_context_too_and_values_are_not_options(self) -> None:
        self.assertEqual(_pass_through(["--loop"], "profile")["verdict"], "PASS_THROUGH_REFUSED")
        ok = _pass_through(["--scope", "none", "C:\\some\\path\\clip", "--no-zebras"])
        self.assertEqual(ok["verdict"], "OK", ok)

    def test_the_refusal_message_never_names_a_path(self) -> None:
        verdict = _pass_through(["--input=C:\\owner\\distinctive_name_xyz"])
        self.assertEqual(verdict["verdict"], "PASS_THROUGH_REFUSED")
        self.assertNotIn("distinctive_name_xyz", verdict["message"])

    def test_the_wrap_branches_of_the_engine_are_counted_where_the_position_wraps(self) -> None:
        # sol B4, wiring half: playbackHandling's loop branch AND the drop-frame tick count a wrap; the
        # decision itself is executed by tests/console/test_playback_frame_range.cpp.
        text = MAIN_WINDOW.read_text(encoding="utf-8")
        handling = text[text.index("void MainWindow::playbackHandling(int timeDiff)"):]
        handling = handling[:handling.index("void MainWindow::showPerformanceProfilingDialog")]
        self.assertGreaterEqual(handling.count("m_playbackWrapRecorder.noteEngineWrap("), 2, handling[:200])
        # ... and the summary/gate lines print the recorder (engine count + heuristic), not the flag.
        self.assertEqual(text.count(".arg( bool01( m_playbackWrapRecorder.wrapped() ) )"), 2)
        # ENFORCE-2: wrap_count is engine wraps + jump-to-first + restarts (a replay of any kind), with the
        # two new counters printed beside it.
        self.assertEqual(text.count(".arg( m_playbackWrapRecorder.replayCount() )"), 2)
        self.assertEqual(text.count(".arg( m_playbackWrapRecorder.jumpToFirstCount )"), 2)
        self.assertEqual(text.count(".arg( m_playbackWrapRecorder.restartCount )"), 2)
        self.assertIn("m_playbackWrapRecorder.noteJumpToFirst();", text)
        self.assertIn("m_playbackWrapRecorder.noteRestart()", text)


# ---- 4c. the profile wrapper (sol B3) --------------------------------------------------------------

PROFILE_WRAPPER = PROFILING / "run-release-playback-profile.ps1"


@requires_pwsh
@requires_windows
class ProfileWrapperGateTests(_TmpCase):
    """run-release-playback-profile.ps1 (-ShowWindow, --exercise-play-action ...) goes through the gate."""

    def setUp(self) -> None:
        super().setUp()
        bin_dir = self.tmp / "bin"
        (bin_dir / "platforms").mkdir(parents=True)
        (bin_dir / "platforms" / "qwindows.dll").write_bytes(b"stand-in")
        self.exe = bin_dir / "MLVApp.exe"
        self.exe.write_bytes(b"MZ stand-in, never launched")

    def run_wrapper(self, clip: Path, tail_ps: str = "") -> subprocess.CompletedProcess:
        script = (
            f"& {_q(PROFILE_WRAPPER)} -RepoRoot {_q(ROOT)} -ExePath {_q(self.exe)} -Input {_q(clip)} "
            f"-Output {_q(self.tmp / 'profile.json')} -DryRun {tail_ps}; exit $LASTEXITCODE")
        return subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, text=True, timeout=180)

    def test_sol_b3_exercise_play_action_on_the_tracked_fixture_is_refused(self) -> None:
        # Sol's exact repro: -Input tracked large_dual_iso -Frames 3 -ShowWindow -AdditionalArgs @('--exercise-play-action').
        self.assertTrue(TRACKED_LARGE_FIXTURE.is_file())
        proc = self.run_wrapper(TRACKED_LARGE_FIXTURE, "-Frames 3 -ShowWindow -AdditionalArgs @('--exercise-play-action')")
        self.assertEqual(proc.returncode, EXIT_TOO_SHORT, proc.stdout + proc.stderr)
        self.assertIn("CLIP_TOO_SHORT", proc.stderr)

    def test_every_play_capable_option_in_every_spelling_is_length_gated(self) -> None:
        short = self.clip("sixteen", FRAMES_SHORT_FIXTURE)
        for option in sorted(PROFILE_PLAY):
            for spelling in (f"--{option}", f"-{option}", f"--{option.upper()}", f"--{option}=1"):
                with self.subTest(spelling):
                    proc = self.run_wrapper(short, f"-AdditionalArgs @({_q(spelling)})")
                    self.assertEqual(proc.returncode, EXIT_TOO_SHORT, proc.stdout + proc.stderr)

    def test_an_unreadable_clip_is_refused_length_unknown(self) -> None:
        proc = self.run_wrapper(self.clip("zero", 0), "-AdditionalArgs @('--exercise-play-action')")
        self.assertEqual(proc.returncode, EXIT_UNKNOWN, proc.stdout + proc.stderr)

    def test_a_long_clip_passes_the_gate_for_a_play_action(self) -> None:
        proc = self.run_wrapper(self.clip("thirty", FRAMES_30S_AT_23976), "-AdditionalArgs @('--exercise-play-action')")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["clipLengthGate"], "OK")
        self.assertTrue(report["playCapable"])

    def test_a_decode_only_profile_of_a_short_clip_is_not_playback_and_is_not_gated(self) -> None:
        proc = self.run_wrapper(self.clip("sixteen", FRAMES_SHORT_FIXTURE), "-Frames 3 -ShowWindow")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        report = json.loads(proc.stdout)
        self.assertFalse(report["playCapable"])
        self.assertEqual(report["clipLengthGate"], "NOT_PLAYING")

    def test_loop_mode_changes_clip_changes_and_the_autoplay_hook_are_refused(self) -> None:
        clip = self.clip("thirty", FRAMES_30S_AT_23976)
        for tail in ("-AdditionalArgs @('--loop')", "-AdditionalArgs @('--gui-smoke-playback')",
                     "-AdditionalArgs @('--profile-playback')", "-AdditionalArgs @('-i','other')",
                     "-AdditionalArgs @('--some-new-flag')", "-ExtraEnvironment @('MLVAPP_AUTOPLAY_LOOP=1')"):
            with self.subTest(tail):
                proc = self.run_wrapper(clip, tail)
                self.assertEqual(proc.returncode, EXIT_PASS_THROUGH, proc.stdout + proc.stderr)
                self.assertIn("PASS_THROUGH_REFUSED", proc.stderr)


# ---- 5. the scan: no launcher may play the app without the gate --------------------------------------
# Files under tools/ that launch the app WITHOUT going through the gate. Every entry carries its reason;
# adding one is a decision a reviewer reads, not a default. NONE of these may pass --loop, forward
# arbitrary arguments, or name a play-capable option (asserted below, so the "decode only" claim is
# checked, not trusted).
PLAYBACK_LAUNCH_ALLOWLIST: dict[str, str] = {
    "tools/profiling/run-release-gui-smoke.ps1":
        "THE choke point: it dot-sources the gate and applies it before any launch (asserted below).",
    "tools/profiling/gui-smoke-length-gate.ps1":
        "the gate itself: it NAMES every play token in order to refuse them and never launches the app.",
    "tools/profiling/bachelor/AttrCudaArtifacts.psm1":
        "spells `--gui-smoke-playback --help` only (the feature probe text); it never launches a playback.",
    "tools/profiling/run-local-gpu-capability.ps1":
        "decode-only `--profile-playback` benchmark with a FIXED argument list: no pass-through, no Play action.",
    "tools/profiling/run-ultra-magnus-profile.ps1":
        "decode-only `--profile-playback` benchmark with a FIXED argument list: no pass-through, no Play action.",
    "tools/profiling/ssh-gpu-probe.ps1":
        "decode-only `--profile-playback` probe with a FIXED argument list: no pass-through, no Play action.",
    "tools/profiling/test-app-play-gate-offscreen.ps1":
        "ENFORCE-2 proof script: launches ONLY the tracked 16-frame fixture through every programmatic Play "
        "entry, deliberately bypassing the tool-side gates, and asserts the APP refuses before Play (exit 14 / 2).",
}
DECODE_ONLY = {rel for rel in PLAYBACK_LAUNCH_ALLOWLIST if rel.endswith(("capability.ps1", "magnus-profile.ps1", "probe.ps1"))}

# The length gate, or the pass-through gate that leaves a launcher unable to carry a play mode at all.
GATE_CALL = re.compile(r"Test-GuiSmokeClipLength\b|Test-GuiSmokePassThroughArguments\b")
GATE_SOURCE = re.compile(r"gui-smoke-length-gate\.ps1")
# Every way a script can make the app PLAY. `--gui-smoke-playback --help` is the feature probe (answered
# by the app without playing) and a comment mentions the words freely.
PLAY_TOKENS = [
    re.compile(r"--gui-smoke-playback(?!\s+--help)"),
    re.compile(r"--profile-playback(?!\s+--help)"),
    re.compile(r"--exercise-play-action"),
    re.compile(r"--exercise-look-assist-(?:toggle|settle)"),
    re.compile(r"MLVAPP_AUTOPLAY_", re.I),
]
LAUNCH_TOKEN = PLAY_TOKENS[0]
# A loop argument in ANY form: quoted, bare on a native command line, or inside a longer string.
LOOP_TOKEN = re.compile(r"(?<![\w-])--loop\b")
# A DIRECT launch of the exe that forwards caller-supplied arguments: whatever the caller passes decides
# what plays, so such a script needs the gate even if it spells no play token itself.
DIRECT_LAUNCH = re.compile(
    r"(?:&\s*\$(?:exe|Executable|ExePath)\b|\.FileName\s*=\s*\$(?:exe|Executable|ExePath)\b|"
    r"Start-Process\b[^\n]*\$(?:exe|Executable|ExePath)\b)", re.I)
PASS_THROUGH = re.compile(r"\$AdditionalArgs\b|@AdditionalArgs\b|\$ExtraArgs\b|@ExtraArgs\b", re.I)
SCANNED_SUFFIXES = {".ps1", ".psm1", ".py", ".bat", ".cmd", ".sh", ".mjs", ".js", ".yml", ".yaml"}
SCAN_ROOTS = ("tools", ".github")


def _tool_files() -> list[Path]:
    files = []
    for root in SCAN_ROOTS:
        base = ROOT / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.suffix.lower() not in SCANNED_SUFFIXES or not path.is_file() or "__pycache__" in path.parts:
                continue
            # ONLY python unit-test modules (and this test's support code) are exempt: they NAME these
            # tokens as strings. A test-*.ps1 or test_* script that LAUNCHES the app is scanned like any other.
            if path.suffix.lower() == ".py" and (path.name.lower().startswith("test_") or path.name in ("synthetic_mlv.py", "app_play_scan.py")):
                continue
            files.append(path)
    return files


def _strip_comments(text: str) -> str:
    return "\n".join(
        raw for raw in text.splitlines() if not raw.strip().startswith(("#", "//")))


def _code_lines(path: Path) -> list[str]:
    return _strip_comments(path.read_text(encoding="utf-8", errors="replace")).splitlines()


_QUOTED_CONCAT = re.compile(r"""(['"])([^'"\n]*)\1\s*\+\s*(['"])([^'"\n]*)\3""")
_FORMAT_OP = re.compile(r"""(['"])([^'"\n]*)\1\s+-f\s+((?:['"][^'"\n]*['"]\s*,\s*)*['"][^'"\n]*['"])""", re.IGNORECASE)
_JOIN_OP = re.compile(r"""@\(\s*((?:['"][^'"\n]*['"]\s*,\s*)*['"][^'"\n]*['"])\s*\)\s*-join\s*(['"])([^'"\n]*)\2""", re.IGNORECASE)


def _literal_list(text: str) -> list[str]:
    return [m.group(2) for m in re.finditer(r"""(['"])([^'"\n]*)\1""", text)]


def compose_literals(code: str) -> str:
    """ENFORCE-2 (fable r1 evasion (e)): resolve the ways a script can COMPOSE a play verb out of string
    literals -- 'a' + 'b', "{0}{1}" -f 'a','b', @('a','b') -join '' -- and drop PowerShell backtick escapes,
    so a play token spelled in pieces is still seen by the scan. A verb assembled through VARIABLES is not
    resolvable here (stated limit); the app-side gate (MainWindow::programmaticPlay) refuses that launch
    anyway when it would play under 20 s, which is why the app is the gate and this scan only the first line."""
    code = code.replace("`", "")
    for _ in range(12):
        before = code
        code = _QUOTED_CONCAT.sub(lambda m: "'" + m.group(2) + m.group(4) + "'", code)
        code = _FORMAT_OP.sub(
            lambda m: "'" + re.sub(r"\{(\d+)\}",
                                    lambda g: (_literal_list(m.group(3)) + [""] * 9)[int(g.group(1))],
                                    m.group(2)) + "'", code)
        code = _JOIN_OP.sub(lambda m: "'" + m.group(3).join(_literal_list(m.group(1))) + "'", code)
        if code == before:
            break
    return code


def _launches_play(code: str) -> bool:
    code = code + "\n" + compose_literals(code)
    if any(token.search(code) for token in PLAY_TOKENS):
        return True
    return bool(DIRECT_LAUNCH.search(code) and PASS_THROUGH.search(code))


# The gate must be ACTED ON, not merely present: its result is assigned, and an exit/throw follows within a few
# lines (a script that dot-sources the gate and ignores the verdict is not gated).
GATE_ACTED_ON = re.compile(
    r"\$\w+\s*=\s*(?:Test-GuiSmokeClipLength|Test-GuiSmokePassThroughArguments)\b[^\n]*\n"
    r"(?:[^\n]*\n){0,10}?[^\n]*\b(?:exit|throw)\b")


def _gated(code: str) -> bool:
    return bool(GATE_SOURCE.search(code) and GATE_CALL.search(code) and GATE_ACTED_ON.search(code))


def find_ungated_launchers(files: dict[str, str]) -> list[str]:
    """rel path -> source text. The scan: who can make the app play, without the gate or an allowlist entry."""
    offenders = []
    for rel, text in sorted(files.items()):
        code = _strip_comments(text)
        if not _launches_play(code) or rel in PLAYBACK_LAUNCH_ALLOWLIST or _gated(code):
            continue
        offenders.append(rel)
    return offenders


# The pre-round-2 wrapper, as the scan saw it on 3303e9d7: a `--profile-playback` launch that forwards
# -AdditionalArgs and -ShowWindow to the app and never touches the gate. (Excerpted, not re-typed
# freehand: these are the exact lines at tools/profiling/run-release-playback-profile.ps1:135-152.)
OLD_PROFILE_WRAPPER_EXCERPT = """
    $arguments = @(
        "--profile-playback",
        "--input", $inputPath,
        "--frames", [string]$Frames
    )
    if ($ShowWindow) {
        $arguments += "--show-window"
    }
    if ($AdditionalArgs.Count -gt 0) {
        $arguments += $AdditionalArgs
    }
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $exe
"""


class PlaybackLauncherScanTests(unittest.TestCase):
    def test_no_script_launches_playback_without_the_gate_or_an_allowlist_entry(self) -> None:
        files = {p.relative_to(ROOT).as_posix(): p.read_text(encoding="utf-8", errors="replace") for p in _tool_files()}
        self.assertEqual(
            find_ungated_launchers(files), [],
            "these tools can make the app PLAY (a play token, or a direct exe launch that forwards caller "
            "arguments) without tools/profiling/gui-smoke-length-gate.ps1 (Test-GuiSmokeClipLength) and are "
            "not on PLAYBACK_LAUNCH_ALLOWLIST. A playback leg needs >= 20 s of footage and must never loop "
            "(owner rule 2026-09-30): route it through run-release-gui-smoke.ps1.")

    def test_the_scan_flags_the_pre_round_2_profile_wrapper(self) -> None:
        # RED-FIRST (sol B3): the r1 scan returned no match for this launcher.
        offenders = find_ungated_launchers({"tools/profiling/run-release-playback-profile.ps1": OLD_PROFILE_WRAPPER_EXCERPT})
        self.assertEqual(offenders, ["tools/profiling/run-release-playback-profile.ps1"])

    def test_the_scan_flags_every_play_token_and_a_forwarding_direct_launch(self) -> None:
        samples = {
            "a.ps1": "& $exe --gui-smoke-playback --input $clip",
            "b.ps1": "& $exe --profile-playback --input $clip --exercise-play-action",
            "c.ps1": "$env:MLVAPP_AUTOPLAY_SECONDS = 30; Start-Process $exe",
            "d.ps1": "& $exe --exercise-look-assist-settle -i $clip",
            "e.ps1": "$p = [Diagnostics.ProcessStartInfo]::new(); $p.FileName = $exe; $a += $AdditionalArgs",
            "f.ps1": "& $exe @AdditionalArgs",
        }
        self.assertEqual(find_ungated_launchers(samples), sorted(samples))

    def test_the_scan_leaves_a_gated_script_a_help_probe_and_a_comment_alone(self) -> None:
        gated = (". ./gui-smoke-length-gate.ps1\n$g = Test-GuiSmokeClipLength -Path $p -WindowSeconds 20\n"
                 "if ($g.verdict -ne 'OK') { exit 41 }\n& $exe --gui-smoke-playback -i $p")
        samples = {
            "gated.ps1": gated,
            "probe.ps1": "& $exe --gui-smoke-playback --help",
            "comment.ps1": "# the app's --gui-smoke-playback and MLVAPP_AUTOPLAY_SECONDS modes\nWrite-Host hi",
        }
        self.assertEqual(find_ungated_launchers(samples), [])

    def test_the_scan_closes_fables_five_evasions(self) -> None:
        # (d) a gate that is dot-sourced and called but whose verdict is ignored is not a gate.
        ignored = {
            "g.ps1": ". ./gui-smoke-length-gate.ps1\nTest-GuiSmokeClipLength -Path $p -WindowSeconds 20\n& $exe --gui-smoke-playback -i $p",
            "h.ps1": ". ./gui-smoke-length-gate.ps1\n$g = Test-GuiSmokeClipLength -Path $p -WindowSeconds 20\nWrite-Host $g\n& $exe --gui-smoke-playback -i $p",
        }
        self.assertEqual(find_ungated_launchers(ignored), sorted(ignored))
        # (c) a bare --loop on a native command line (no quotes) is a loop token.
        self.assertTrue(LOOP_TOKEN.search("& $exe --gui-smoke-playback --input $clip --loop"))
        self.assertTrue(LOOP_TOKEN.search("--loop=1"))
        self.assertFalse(LOOP_TOKEN.search("--no-loop-guard-x"))
        # (a) the scan reaches CI workflows, and (b) a test-*.ps1 / test_*.sh script is scanned (only
        # python unit-test modules are exempt).
        scanned = {p.relative_to(ROOT).as_posix() for p in _tool_files()}
        self.assertTrue(any(name.startswith(".github/") for name in scanned), "the scan must cover .github")
        self.assertTrue(any(Path(name).name.startswith("test-") and name.endswith(".ps1") for name in scanned))
        self.assertNotIn("tools/repo_hygiene/test_playback_clip_length_gate.py", scanned)

    def test_the_scan_sees_a_play_verb_composed_out_of_string_literals(self) -> None:
        # ENFORCE-2 (fable r1 evasion (e), composed verb strings): each of these launches a play mode whose
        # token never appears in one piece in the source.
        samples = {
            "concat.ps1": "& $exe ('--gui-smoke-' + 'playback') -i $clip",
            "concat2.ps1": "$m = \"--exercise-\" + \"play-action\"; & $exe $m -i $clip",
            "format.ps1": "$m = \"{0}-{1}\" -f '--exercise','play-action'; & $exe $m -i $clip",
            "join.ps1": "$m = @('--profile','-playback') -join ''; & $exe $m -i $clip",
            "backtick.ps1": "& $exe --gui-smoke-play`back -i $clip",
            "env.ps1": "$env:MLVAPP_AUTO' + 'PLAY_SECONDS = 30",
        }
        # the last sample is deliberately NOT a valid composition (a lone quote): it must not crash the scan.
        # RED-FIRST: the pre-ENFORCE-2 token search alone (no composition) saw NONE of these.
        for name, code in samples.items():
            if name != "env.ps1":
                self.assertFalse(any(token.search(code) for token in PLAY_TOKENS), name)
        flagged = find_ungated_launchers({k: v for k, v in samples.items() if k != "env.ps1"})
        self.assertEqual(flagged, sorted(k for k in samples if k != "env.ps1"))
        find_ungated_launchers({"env.ps1": samples["env.ps1"]})
        # ... and a composed token inside a gated script is still left alone.
        gated = (". ./gui-smoke-length-gate.ps1\n$g = Test-GuiSmokeClipLength -Path $p -WindowSeconds 20\n"
                 "if ($g.verdict -ne 'OK') { exit 41 }\n& $exe ('--gui-smoke-' + 'playback') -i $p")
        self.assertEqual(find_ungated_launchers({"gated.ps1": gated}), [])

    def test_no_script_other_than_the_gate_policy_ever_spells_loop_as_an_argument(self) -> None:
        offenders = []
        for path in _tool_files():
            if LOOP_TOKEN.search("\n".join(_code_lines(path))):
                offenders.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(offenders, [], "a playback launcher passes --loop; nothing may")

    def test_every_launcher_that_can_play_refuses_an_inherited_autoplay_variable(self) -> None:
        # ENFORCE-2: -ExtraEnvironment was already refused; an MLVAPP_AUTOPLAY_* variable inherited from the
        # PARENT environment reaches the app the same way and must be refused by every playing launcher.
        for name in ("run-release-gui-smoke.ps1", "run-release-playback-profile.ps1", "validate-visible-playback.ps1",
                     "capture-reference-frame.ps1", "start-release-cuda-playback.ps1"):
            with self.subTest(name):
                code = "\n".join(_code_lines(PROFILING / name))
                self.assertRegex(
                    code,
                    r"\$parentEnvironmentGate\s*=\s*Test-GuiSmokeParentEnvironment\b[^\n]*\n(?:[^\n]*\n){0,3}?[^\n]*\bexit 44\b"
                    if name != "capture-reference-frame.ps1" else r"Test-GuiSmokeParentEnvironment\b[\s\S]{0,400}?\bexit 44\b")

    def test_every_allowlist_entry_still_exists_and_names_a_reason(self) -> None:
        for rel, reason in PLAYBACK_LAUNCH_ALLOWLIST.items():
            self.assertTrue((ROOT / rel).is_file(), f"stale allowlist entry {rel}")
            self.assertGreater(len(reason), 20, rel)

    def test_the_decode_only_allowlist_entries_really_cannot_play(self) -> None:
        self.assertEqual(len(DECODE_ONLY), 3)
        for rel in sorted(DECODE_ONLY):
            with self.subTest(rel):
                code = _strip_comments((ROOT / rel).read_text(encoding="utf-8", errors="replace"))
                self.assertFalse(PASS_THROUGH.search(code), f"{rel} forwards caller arguments")
                for token in PLAY_TOKENS[0:1] + PLAY_TOKENS[2:]:
                    self.assertFalse(token.search(code), f"{rel} names a play-capable option ({token.pattern})")
                self.assertFalse(LOOP_TOKEN.search(code), rel)

    def test_the_runner_really_applies_the_gate_before_it_launches(self) -> None:
        text = RUNNER.read_text(encoding="utf-8")
        self.assertIn("gui-smoke-length-gate.ps1", text)
        self.assertLess(text.index("Test-GuiSmokeClipLength"), text.index('"--gui-smoke-playback",'))
        # and the pass-through gate runs before the gate, so a refusal costs nothing.
        self.assertLess(text.index("Test-GuiSmokePassThroughArguments"), text.index("Test-GuiSmokeClipLength"))

    def test_the_direct_launchers_run_the_gate(self) -> None:
        for name in ("validate-visible-playback.ps1", "capture-reference-frame.ps1", "run-release-playback-profile.ps1"):
            with self.subTest(name):
                code = "\n".join(_code_lines(PROFILING / name))
                self.assertTrue(_gated(code), name)
                self.assertFalse(LOOP_TOKEN.search(code), f"{name} passes --loop")

    def test_the_forwarding_launchers_refuse_every_play_mode_before_they_launch(self) -> None:
        # export / interactive launchers that forward -AdditionalArgs to the exe (sol B3 class).
        for name in ("run-release-cdng-export-profile.ps1", "run-release-cuda-dng-export.ps1",
                     "start-release-cuda-playback.ps1"):
            with self.subTest(name):
                code = "\n".join(_code_lines(PROFILING / name))
                self.assertTrue(_gated(code), name)
                self.assertIn("-Context 'launcher'", code)
                self.assertLess(code.index("Test-GuiSmokePassThroughArguments"), code.index("ProcessStartInfo"))

    @requires_pwsh
    def test_the_launcher_context_refuses_play_modes_and_lets_export_options_through(self) -> None:
        for refused in ("--gui-smoke-playback", "--profile-playback", "--loop", "--exercise-play-action",
                        "--exercise-look-assist-settle", "--exercise-clip-lifecycle-stress", "--launch-only"):
            with self.subTest(refused):
                self.assertEqual(_pass_through([refused], "launcher")["verdict"], "PASS_THROUGH_REFUSED")
        self.assertEqual(_pass_through(["--max-frames", "5", "--skip-errors", "--verbose"], "launcher")["verdict"], "OK")

    def test_the_smoke_runner_closure_carries_the_gate(self) -> None:
        text = (PROFILING / "bachelor" / "AttrCudaArtifacts.psm1").read_text(encoding="utf-8")
        self.assertIn("'tools/profiling/gui-smoke-length-gate.ps1'", text)


# ---- 6. PLAYBACK-CLIP-LENGTH-ENFORCE-2: THE APP IS THE GATE -- static class test over the C++ ------------
#
# The app's every programmatic Play goes through MainWindow::programmaticPlay (window gate + one-Play
# ledger); user-input handlers are never gated. This scan fails on:
#   * ANY use of the Play action other than isChecked()/setChecked(false), outside the three PINNED helper bodies
#     (programmaticPlay's structure, programmaticStop and forceLoopOffForAutomation's exact text) -- a trigger, an
#     alias, a split line, a variable setChecked, invokeMethod, a synthesized key. No comment can exempt a line;
#   * any Loop use other than isChecked() outside the one pinned unchecker, anywhere in platform/qt or src;
#   * any programmaticPlay( call whose site is not in the reviewed set, so a NEW programmatic Play cannot
#     appear without updating this list (and therefore being reviewed);
#   * a programmaticPlay body that triggers Play before it has evaluated the window and admitted it to the
#     ledger;
#   * a reviewed entry point that never forces Loop off.
# The scan is a pure function over {path: text} so the test can MUTATE the real sources in memory and
# require the scan to go red (a scan that passes on a deliberately broken tree is worthless).

# The scanner itself lives in tools/repo_hygiene/app_play_scan.py (ENFORCE-2 round 2: exact pins, a real tokenizer,
# whole-text matching; no `allowlisted:` comment can exempt a line any more).
from tools.repo_hygiene import app_play_scan as _app_play_scan  # noqa: E402
from tools.repo_hygiene.app_play_scan import (  # noqa: E402
    LOOP_OFF_ENTRY_MARKERS,
    PINNED_EVENT_SITES,
    PINNED_HELPER_BODIES,
    PINNED_KEY_SPACE_BINDINGS,
    SPACE_BINDING_STATEMENT_IDENTIFIERS,
    PLAY_SOURCES,
    REVIEWED_PROGRAMMATIC_PLAY_SITES,
    REVIEWED_PROGRAMMATIC_STOP_SITES,
    function_body as _function_body,
    scan_app_play_sources,
    strip_cpp,
)

_BUILD_DIR_NAMES = {"moc", "obj", "rcc", "ui", "release", "debug"}
# r2 adversary pass 2: also every includer of the key table, every notify( dispatch, and every #define of a token of
# the reviewed Space binding statement (a macro in an including file rewrites the binding). r3 A2: every MENTION of
# notify (a grouped `(qApp->notify)(...)` or a member pointer has no `notify(`), as the scan now counts mentions.
_PLAY_RELEVANT = re.compile(
    r"actionPlay|actionLoop|setPlaying|invokeMethod|QKeyEvent|Key_Space|postEvent|sendEvent|"
    r"sendSpontaneousEvent|programmaticPlay|programmaticStop|QTest\s*::\s*key|keyClick|PlayerShortcutBinding|"
    r"\bnotify\b|#\s*define\s+(?:" + "|".join(sorted(SPACE_BINDING_STATEMENT_IDENTIFIERS)) + r")\b")


def _load_play_sources() -> dict[str, str]:
    sources = {path: (ROOT / path).read_text(encoding="utf-8") for path in PLAY_SOURCES}
    # r5: every pinned file is always scanned. A pinned file whose mentions all vanished no longer matches _PLAY_RELEVANT
    # and would otherwise leave the scan, hiding the vanished pin; a deleted pinned file raises here (a red test).
    for pinned in sorted({path for path, _ in PINNED_EVENT_SITES} | set(PINNED_KEY_SPACE_BINDINGS)):
        sources.setdefault(pinned, (ROOT / pinned).read_text(encoding="utf-8"))
    # Every other C++ translation unit under platform/qt and src that can reach the Play / Loop action at all.
    # Generated and build output (a shadow or in-tree build dir, uic's ui_*.h) is not source.
    for base in ("platform/qt", "src"):
        for candidate in sorted((ROOT / base).rglob("*")):
            if candidate.suffix not in (".cpp", ".h", ".hpp", ".c") or not candidate.is_file():
                continue
            relative = candidate.relative_to(ROOT)
            parts = {part.lower() for part in relative.parts[:-1]}
            if any(part.startswith("build") or part in _BUILD_DIR_NAMES for part in parts):
                continue
            if relative.name.startswith("ui_") or relative.name.startswith("moc_") or relative.name.startswith("qrc_"):
                continue
            key = relative.as_posix()
            if key in sources:
                continue
            body = candidate.read_text(encoding="utf-8", errors="replace")
            if _PLAY_RELEVANT.search(body):
                sources[key] = body
    return sources


class AppPlayGateStaticClassTests(unittest.TestCase):
    """5. The static class test: no programmatic Play and no Loop-enable outside the reviewed gate."""

    MAIN_WINDOW = "platform/qt/MainWindow.cpp"
    MAIN = "platform/qt/main.cpp"

    @classmethod
    def setUpClass(cls) -> None:
        cls.sources = _load_play_sources()

    def test_the_real_sources_are_clean(self) -> None:
        self.assertEqual(scan_app_play_sources(self.sources), [])

    def test_no_generated_or_build_output_is_scanned(self) -> None:
        for path in self.sources:
            self.assertNotIn("/build", path.lower())
            self.assertFalse(Path(path).name.startswith("ui_"), path)

    def test_the_actions_are_not_persisted_or_declared_checked(self) -> None:
        # Loop starts unchecked and nothing persists it: a test entry never inherits a looping GUI.
        ui = (ROOT / "platform" / "qt" / "MainWindow.ui").read_text(encoding="utf-8")
        loop = ui[ui.index('<action name="actionLoop">'):]
        loop = loop[:loop.index("</action>")]
        self.assertNotRegex(loop, r'<property name="checked">\s*<bool>true</bool>')
        _, code = strip_cpp(self.sources[self.MAIN_WINDOW])
        for line in code.splitlines():
            if "actionLoop" in line and re.search(r"setValue|QSettings|settings\.", line):
                self.fail(f"Loop appears to be persisted: {line.strip()}")

    def test_the_pure_window_logic_lives_in_the_unit_tested_header(self) -> None:
        header = (ROOT / "platform" / "qt" / "PlaybackFrameRange.h").read_text(encoding="utf-8")
        # PLAYBACK-CLIP-LENGTH-HFR-10S-1: the floor is ONE derivation of the native fps; the single 20 s constant is gone.
        self.assertIn("constexpr double kStandardMinPlayWindowSeconds = 20.0;", header)
        self.assertIn("constexpr double kHfrMinPlayWindowSeconds = 10.0;", header)
        self.assertIn("constexpr double kHfrNativeFpsThreshold = 50.0;", header)
        self.assertIn("inline double minPlayWindowSeconds( double nativeFps )", header)
        self.assertNotIn("kMinPlayWindowSeconds", header)
        self.assertNotIn("kMinPlayWindowMs", header)
        for source in (self.sources[self.MAIN_WINDOW], self.sources[self.MAIN]):
            self.assertNotIn("kMinPlayWindow", source)
            # no second hard-coded 20 s floor on a clip / window / stress-switch quantity
            self.assertNotRegex(strip_cpp(source)[0], r"(?:Seconds|seconds)\s*(?:[<>]=?)\s*20(?:\.0)?\b|qMax\(\s*20\.0\s*,")
        for name in ("evaluatePlayableWindow", "ProgrammaticPlayLedger", "noteJumpToFirst", "noteRestart",
                     "engineCutRangeForPlay", "smokePlayRequestSeconds", "stressSwitchReachesFloor",
                     "PLAY_DURATION_TOO_SHORT", "SourceFrameAdvanceCounter", "requiredSourceFrames",
                     "evaluatePlayStop", "playSafetyMs", "PLAY_PACE_TOO_SLOW", "SOURCE_FRAMES_SHORT",
                     "PLAY_SAFETY_TIMEOUT"):
            self.assertIn(name, header)
        self.assertNotIn("playHoldReachedFloor", header)   # the wall-clock hold predicate is retired
        tests = (ROOT / "tests" / "console" / "test_playback_frame_range.cpp").read_text(encoding="utf-8")
        for name in ("PlayableWindow", "ProgrammaticPlayLedger"):
            self.assertIn(f"TEST( {name}, ", tests)
        for name in ("SolB1ARequestedWindowUnderTheFloor", "SolB3WithTheCollapsedRangeRepairDisabled",
                     "TheLifecycleStressSwitchMayOnlyHappenAfterTheTwentySecondFloor",
                     "NoWallClockPredicateRemainsThatCanCallAPlayLongEnough",
                     "SolR2ReproTheOldWallClockRuleStopsAtTwentySecondsAfterTenSecondsOfFootage",
                     "TheSameRunUnderTheNewRuleIsRefusedBeforePlayAtTheEnginesRealPace",
                     "ALoopingEngineNeverReachesTheRequirementByReplayingFootage",
                     "AWrapOrABackwardStepNeverAddsAndNeverCountsFramesTwice"):
            self.assertIn(name, tests)

    # -- mutation tests: the scan must go red on a deliberately broken tree ------------------------------

    def _mutated(self, old: str, new: str, path: str = MAIN_WINDOW, count: int = 1) -> dict[str, str]:
        text = self.sources[path]
        self.assertIn(old, text, "mutation anchor missing: the scan test needs updating with the source")
        mutated = dict(self.sources)
        mutated[path] = text.replace(old, new, count)
        return mutated

    def _mutated_re(self, pattern: str, replacement: str, path: str = MAIN_WINDOW) -> dict[str, str]:
        text = self.sources[path]
        mutated_text, count = re.subn(pattern, replacement, text, count=1)
        self.assertEqual(count, 1, "mutation pattern missing: the scan test needs updating with the source")
        mutated = dict(self.sources)
        mutated[path] = mutated_text
        return mutated

    def _inject_after_measured_loop_off(self, injected: str) -> dict[str, str]:
        anchor = 'forceLoopOffForAutomation( "gui-smoke-measured" );'
        return self._mutated(anchor, anchor + "\n    " + injected)

    def _assert_caught(self, mutated: dict[str, str], needle: str = "") -> None:
        problems = scan_app_play_sources(mutated)
        self.assertTrue(problems, "the scan did not go red on a deliberately broken tree")
        if needle:
            self.assertTrue(any(needle in p for p in problems), problems)

    def test_mutation_a_bare_play_trigger_in_an_automation_path_is_caught(self) -> None:
        self._assert_caught(self._inject_after_measured_loop_off("ui->actionPlay->trigger();"), "Play action")

    def test_mutation_a_loop_enable_is_caught(self) -> None:
        self._assert_caught(self._inject_after_measured_loop_off("ui->actionLoop->setChecked( true );"), "Loop action")

    def test_mutation_a_new_programmatic_play_site_must_be_reviewed(self) -> None:
        self._assert_caught(self._mutated('programmaticPlay( "autoplay", autoplaySeconds )',
                                          'programmaticPlay( "autoplay-two", autoplaySeconds )'), "programmaticPlay sites")

    def test_mutation_a_second_call_at_a_reviewed_site_is_caught(self) -> None:
        self._assert_caught(self._inject_after_measured_loop_off(
            'programmaticPlay( "gui-smoke-measured", playback_frame_range::smokePlayRequestSeconds( 1, 0, 24.0 ) );'),
            "programmaticPlay sites")

    def test_mutation_a_short_requested_window_at_a_site_is_caught(self) -> None:
        # hub ruling: every caller requests a window that itself reaches the floor.
        for site in ("profile-exercise-play-action", "profile-look-assist-settle"):
            with self.subTest(site):
                self._assert_caught(self._mutated_re(
                    r'(programmaticPlay\(\s*"' + site + r'"\s*,\s*)playGateMinPlayWindowSeconds\(\)',
                    r'\g<1>0.0'), "requested window")
        self._assert_caught(self._mutated_re(
            r'(programmaticPlay\(\s*"gui-smoke-measured"\s*,\s*)playback_frame_range::smokePlayRequestSeconds\(',
            r'\g<1>qMax( 100, options.durationMs ) / 1000.0 + ( 0 * playback_frame_range::smokePlayRequestSeconds('),
            "requested window")

    def test_mutation_a_new_programmatic_stop_site_must_be_reviewed(self) -> None:
        self._assert_caught(self._inject_after_measured_loop_off('programmaticStop( "a-new-early-stop" );'),
                            "programmaticStop sites")

    def test_mutation_removing_the_ledger_or_the_window_check_from_the_gate_is_caught(self) -> None:
        for anchor in ("m_programmaticPlayLedger.admit( verdict )", "checkPlayableWindow( site, requestedSeconds )"):
            with self.subTest(anchor):
                body = _function_body(self.sources[self.MAIN_WINDOW], "bool MainWindow::programmaticPlay(")
                self.assertIn(anchor, body)
                mutated = self._mutated(anchor, "true /* mutated */", count=1)
                self._assert_caught(mutated, "programmaticPlay must")

    def test_mutation_disabling_the_ledger_guard_with_false_and_is_caught(self) -> None:
        # sol hardening: `false &&` used to leave the substring the old scan looked for.
        self._assert_caught(self._mutated("if( !m_programmaticPlayLedger.admit( verdict ) )",
                                          "if( false && !m_programmaticPlayLedger.admit( verdict ) )"),
                            "programmaticPlay must contain")

    def test_mutation_restoring_the_already_playing_early_return_is_caught(self) -> None:
        # fable PLAY-GATE-ALREADY-PLAYING-1: an already-running Play must not skip the caller's window check.
        self._assert_caught(self._mutated("const bool alreadyPlaying = ui->actionPlay->isChecked();",
                                          "if( ui->actionPlay->isChecked() ) return true;\n"
                                          "    const bool alreadyPlaying = false;"),
                            "already-running Play")

    def test_mutation_gating_the_interactive_handler_is_caught(self) -> None:
        self._assert_caught(self._mutated_re(
            r'(void MainWindow::on_actionPlay_triggered\(bool checked\)\s*\{)',
            r'\1 programmaticPlay( "x", 0.0 );'), "user-input handler")

    def test_mutation_dropping_a_loop_off_at_an_entry_is_caught(self) -> None:
        self._assert_caught(self._mutated('forceLoopOffForAutomation( "gui-smoke-entry" );', ""), "missing Loop-off")

    def test_mutation_the_pinned_helper_bodies_cannot_grow_a_second_trigger(self) -> None:
        self._assert_caught(self._mutated(
            "if( ui->actionPlay->isChecked() ) ui->actionPlay->trigger();",
            "if( ui->actionPlay->isChecked() ) ui->actionPlay->trigger();\n    ui->actionPlay->trigger();"),
            "pinned helper body changed")
        self._assert_caught(self._mutated("ui->actionLoop->trigger();", "ui->actionLoop->trigger();\n    ui->actionLoop->trigger();"),
                            "pinned helper body changed")

    # -- sol hardening: the evasions a free-text `allowlisted:` comment and a per-line regex let through ---

    def test_an_allowlisted_comment_no_longer_exempts_anything(self) -> None:
        for injected in ("ui->actionPlay->trigger();   // allowlisted: new warm-up",
                         "ui->actionLoop->trigger();   // allowlisted: nothing to see",
                         "ui->actionPlay->trigger(); /* allowlisted: x */"):
            with self.subTest(injected):
                self._assert_caught(self._inject_after_measured_loop_off(injected), "action")

    def test_every_evasion_sol_and_fable_listed_is_caught(self) -> None:
        evasions = {
            "trigger split across lines": "ui->actionPlay\n        ->trigger();",
            "setChecked of a variable": "const bool warm = true;\n    ui->actionPlay->setChecked( warm );",
            "setChecked( 1 )": "ui->actionPlay->setChecked( 1 );",
            "an alias of the action": "QAction *playAlias = ui->actionPlay;\n    playAlias->trigger();",
            "invokeMethod on the handler": 'QMetaObject::invokeMethod( this, "on_actionPlay_triggered", Q_ARG( bool, true ) );',
            "invokeMethod on the action": 'QMetaObject::invokeMethod( ui->actionPlay, "trigger" );',
            "a // inside a string earlier on the line": 'const QString s = "//"; ui->actionPlay->trigger();',
            "a block comment before the call": "/* quiet */ ui->actionPlay->trigger();",
            "a synthesized Space key": "QKeyEvent press( QEvent::KeyPress, Qt::Key_Space, Qt::NoModifier );",
            "a posted key event": "QCoreApplication::postEvent( this, nullptr );",
            "the Play handler called directly": "on_actionPlay_triggered( true );",
            "the toggled handler called directly": "on_actionPlay_toggled( true );",
            "setPlaying": "setPlaying( true );",
            "a Loop alias": "QAction *loopAlias = ui->actionLoop;",
            "Loop setChecked of a variable": "ui->actionLoop->setChecked( m_wantLoop );",
        }
        for name, injected in evasions.items():
            with self.subTest(name):
                self._assert_caught(self._inject_after_measured_loop_off(injected))

    # -- LOOK-ASSIST-CLIP-OPEN-GUI-BLOCK-1 guard contract: the event-dispatch pin grew from 1 to 3 entries ------

    _PINNED_POST_FIXTURE = {
        "platform/qt/MyApplication.h": "bool f() { return QApplication::sendEvent( m_mainWindow, event ); }\n",
        "platform/qt/LookAssistAnalysisPump.h": "void land() { if( target ) QCoreApplication::postEvent( target.data(), held ); }\n",
        "platform/qt/MainWindow.cpp": "void g() { m_pump.hold( [this, o]() { QCoreApplication::postEvent( this, o ); } ); }\n",
    }

    @staticmethod
    def _event_dispatch_problems(sources: dict[str, str]) -> list[str]:
        # Fixture text is not a whole tree, so the other scans report it; only the event-dispatch rows are under test.
        return [p for p in scan_app_play_sources(sources) if p.startswith("event dispatch ")]

    def test_the_event_dispatch_pin_is_exactly_the_three_declared_sites(self) -> None:
        # r3 A2: every MENTION is counted, so the two reviewed local callables named notify (no QEvent) are pinned too.
        self.assertEqual(PINNED_EVENT_SITES, {
            ("platform/qt/MyApplication.h", "sendEvent"): 1,
            ("platform/qt/LookAssistAnalysisPump.h", "postEvent"): 1,
            ("platform/qt/MainWindow.cpp", "postEvent"): 1,
            ("platform/qt/FocusPixelMapManager.cpp", "notify"): 3,
            ("platform/qt/SyncDownloadWaiter.h", "notify"): 2,
            # PLAYER-FULLSCREEN-CONTROL-BAR-1 r3: the CPU bar's wheel re-dispatch to the viewport (a delivered clone).
            ("platform/qt/FullscreenControlBar.cpp", "sendEvent"): 1,
        })

    def test_the_two_pinned_postevent_sites_pass(self) -> None:
        self.assertEqual(self._event_dispatch_problems(dict(self._PINNED_POST_FIXTURE)), [])

    # -- PLAYER-STANDARD-SHORTCUT-MAP-1: Space is BOUND as the Play shortcut in the one key table -------------
    # (PlayerShortcutBinding::bindings(); before, MainWindow.ui carried it). A binding is not an event: exactly the
    # reviewed expression, at the reviewed path and count, is exempt from the synthesized-key scan. Nothing else is.

    _SPACE_TABLE = "platform/qt/PlayerShortcutBinding.h"
    _SPACE_BINDING = ("inline void t() { table << ActionBinding{ QStringLiteral( \"actionPlay\" ),\n"
                      "    { { QKeySequence( Qt::Key_Space ), KeyKind::Primary } } }; }\n")

    @staticmethod
    def _key_synth_problems(sources: dict[str, str]) -> list[str]:
        # Fixture text is not a whole tree, so the other scans report it; only the key-synthesis rows are under test.
        return [p for p in scan_app_play_sources(sources)
                if "synthesized key event" in p or "Space shortcut binding" in p or "Space binding statement" in p]

    def test_the_space_binding_pin_is_exactly_the_one_reviewed_site(self) -> None:
        self.assertEqual(PINNED_KEY_SPACE_BINDINGS, {"platform/qt/PlayerShortcutBinding.h": (
            'table << ActionBinding{ QStringLiteral( "actionPlay" ), '
            '{ { QKeySequence( Qt::Key_Space ), KeyKind::Primary } } };', 1)})

    def test_the_reviewed_space_shortcut_binding_passes(self) -> None:
        self.assertEqual(self._key_synth_problems({self._SPACE_TABLE: self._SPACE_BINDING}), [])

    def test_the_real_key_table_holds_exactly_the_reviewed_binding(self) -> None:
        self.assertIn(self._SPACE_TABLE, self.sources)
        blank, _ = strip_cpp(self.sources[self._SPACE_TABLE])
        self.assertEqual(len(re.findall(r"\bKey_Space\b", blank)), 1)
        self.assertEqual(len(re.findall(r"\bQKeySequence\s*\(\s*Qt\s*::\s*Key_Space\s*\)", blank)), 1)
        # r2 adversary A1: and that one expression sits in the reviewed table statement, nowhere else.
        _, keep = strip_cpp(self.sources[self._SPACE_TABLE])
        statement, count = PINNED_KEY_SPACE_BINDINGS[self._SPACE_TABLE]
        self.assertEqual(re.sub(r"\s+", " ", keep).count(statement), count)

    # r5 (sol r4 blocker, fable key3 r4 APPROVE hardening): the exact-count guards must fail a count of ZERO too. A pinned
    # Space statement that is deleted or retargeted, and a pinned event site whose mentions all disappear, scanned clean.

    def _space_statement_span(self) -> tuple[str, int, int]:
        # The span indexes the comment-stripped text (comments are removed, literals kept), so mutate that text.
        statement, _ = PINNED_KEY_SPACE_BINDINGS[self._SPACE_TABLE]
        _, keep = strip_cpp(self.sources[self._SPACE_TABLE])
        match = _app_play_scan._statement_pattern(statement).search(keep)
        self.assertIsNotNone(match, "the pinned Space statement is missing from the real key table")
        return keep, match.start(), match.end()

    def test_r5_the_pinned_space_statement_deleted_fails(self) -> None:
        text, start, end = self._space_statement_span()
        mutated = dict(self.sources)
        mutated[self._SPACE_TABLE] = text[:start] + text[end:]
        problems = scan_app_play_sources(mutated)
        self.assertIn(f"{self._SPACE_TABLE}: 0 Space shortcut binding(s), 1 reviewed "
                      "(a new binding is reviewed in app_play_scan.py PINNED_KEY_SPACE_BINDINGS)", problems)
        self.assertIn(f"{self._SPACE_TABLE}: the reviewed Space binding statement x0, pinned x1 "
                      "(a new binding is reviewed in app_play_scan.py PINNED_KEY_SPACE_BINDINGS)", problems)

    def test_r5_the_pinned_space_statement_retargeted_to_another_key_fails(self) -> None:
        text, start, end = self._space_statement_span()
        retargeted = text[start:end].replace("Qt::Key_Space", "Qt::Key_P")
        self.assertNotEqual(retargeted, text[start:end])
        mutated = dict(self.sources)
        mutated[self._SPACE_TABLE] = text[:start] + retargeted + text[end:]
        problems = scan_app_play_sources(mutated)
        self.assertIn(f"{self._SPACE_TABLE}: 0 Space shortcut binding(s), 1 reviewed "
                      "(a new binding is reviewed in app_play_scan.py PINNED_KEY_SPACE_BINDINGS)", problems)
        self.assertIn(f"{self._SPACE_TABLE}: the reviewed Space binding statement x0, pinned x1 "
                      "(a new binding is reviewed in app_play_scan.py PINNED_KEY_SPACE_BINDINGS)", problems)

    def test_r5_a_pinned_event_site_whose_mentions_all_vanish_fails(self) -> None:
        for (path, name), pinned in PINNED_EVENT_SITES.items():
            with self.subTest(path=path, name=name):
                text = self.sources[path]
                self.assertEqual(len(re.findall(rf"\b{name}\b", strip_cpp(text)[0])), pinned)
                mutated = dict(self.sources)
                mutated[path] = re.sub(rf"\b{name}\b", "renamedAway", text)
                problems = scan_app_play_sources(mutated)
                self.assertTrue(any(p.startswith(f"event dispatch {name} in {path} x0, pinned x{pinned}")
                                    for p in problems), problems)

    def test_r5_the_real_loader_always_scans_every_pinned_file(self) -> None:
        # A pinned file whose mentions all vanish would no longer match _PLAY_RELEVANT and silently leave the scan.
        pinned_paths = {path for path, _ in PINNED_EVENT_SITES} | set(PINNED_KEY_SPACE_BINDINGS)
        self.assertLessEqual(pinned_paths, set(self.sources))
        with mock.patch.dict(globals(), {"_PLAY_RELEVANT": re.compile(r"(?!)")}):   # nothing matches any more
            self.assertLessEqual(pinned_paths, set(_load_play_sources()))

    def test_a_second_space_binding_in_the_key_table_fails(self) -> None:
        text = self._SPACE_BINDING + "inline QKeySequence u() { return QKeySequence( Qt::Key_Space ); }\n"
        problems = self._key_synth_problems({self._SPACE_TABLE: text})
        self.assertEqual(sum("synthesized key event" in p for p in problems), 2, problems)
        self.assertIn(f"{self._SPACE_TABLE}: 2 Space shortcut binding(s), 1 reviewed "
                      "(a new binding is reviewed in app_play_scan.py PINNED_KEY_SPACE_BINDINGS)", problems)

    def test_a_space_binding_in_any_other_file_fails(self) -> None:
        for path in ("platform/qt/MainWindow.cpp", "platform/qt/OtherWidget.h", "src/batch/Thing.cpp"):
            with self.subTest(path):
                problems = self._key_synth_problems({path: self._SPACE_BINDING})
                self.assertIn(f"{path}:2: synthesized key event (the Play shortcut is Space)", problems)

    def test_every_other_space_key_in_the_key_table_still_fails(self) -> None:
        others = {
            "a synthesized Space key event": "QKeyEvent press( QEvent::KeyPress, Qt::Key_Space, Qt::NoModifier );",
            "a heap Space key event": "auto *e = new QKeyEvent( QEvent::KeyPress, Qt::Key_Space, {} );",
            "a bare Space key code": "const int k = Qt::Key_Space;",
            "a modified Space binding": "const QKeySequence s( QKeySequence( Qt::SHIFT | Qt::Key_Space ) );",
            "a QTest key click": "QTest::keyClick( w, Qt::Key_Space );",
        }
        for name, injected in others.items():
            with self.subTest(name):
                text = self._SPACE_BINDING + "inline void v() { " + injected + " }\n"
                problems = self._key_synth_problems({self._SPACE_TABLE: text})
                self.assertTrue(any(p.startswith(f"{self._SPACE_TABLE}:3: synthesized key event") for p in problems),
                                problems)

    # r2 adversary A1 (f85cb8e9): the exemption keyed on the bare expression anywhere in the file, so the reviewed
    # QKeySequence could be moved into a lambda that also builds a brace-initialised QKeyEvent (which _KEY_SYNTH did
    # not see), or wrapped into a modified-Space sequence, and the whole tree still scanned clean.
    _A1_MUTANTS = {
        "a lambda that brace-builds a QKeyEvent from the reviewed sequence":
            "([]() { const auto s = QKeySequence( Qt::Key_Space ); "
            "QKeyEvent press{ QEvent::KeyPress, s[0].key(), Qt::NoModifier }; return s; }())",
        "a modified Space sequence wrapped around the reviewed expression":
            "QKeySequence( Qt::SHIFT | QKeySequence( Qt::Key_Space )[0].toCombined() )",
    }

    def test_r2_a1_the_reviewed_expression_moved_out_of_its_table_statement_fails(self) -> None:
        for name, replacement in self._A1_MUTANTS.items():
            with self.subTest(name):
                mutated = self._mutated("QKeySequence( Qt::Key_Space )", replacement, path=self._SPACE_TABLE)
                problems = self._key_synth_problems(mutated)
                self.assertTrue(any(p.startswith(f"{self._SPACE_TABLE}:") and "synthesized key event" in p
                                    for p in problems), problems)
                self.assertIn(f"{self._SPACE_TABLE}: the reviewed Space binding statement x0, pinned x1 "
                              "(a new binding is reviewed in app_play_scan.py PINNED_KEY_SPACE_BINDINGS)", problems)

    def test_r2_a1_a_brace_or_template_built_qkeyevent_is_a_synthesized_key(self) -> None:
        # No Key_Space token at all: the construction itself is the finding, in every file.
        for injected in ("QKeyEvent press{ QEvent::KeyPress, k, Qt::NoModifier };",
                         "QKeyEvent press { QEvent::KeyPress, k, Qt::NoModifier };",
                         "auto e = QKeyEvent{ QEvent::KeyPress, k, Qt::NoModifier };",
                         "auto e = std::make_unique<QKeyEvent>( QEvent::KeyPress, k, Qt::NoModifier );",
                         "auto e = std::make_shared< QKeyEvent >( QEvent::KeyPress, k, Qt::NoModifier );"):
            for path in (self._SPACE_TABLE, "platform/qt/MainWindow.cpp"):
                with self.subTest(injected=injected, path=path):
                    problems = self._key_synth_problems({path: "inline void z( int k ) {\n    " + injected + "\n}\n"})
                    self.assertIn(f"{path}:2: synthesized key event (the Play shortcut is Space)", problems)

    # r2 adversary pass 2 (629995bd). A1: a macro rewrote the unchanged statement into Shift+Space. A2: an array
    # declarator, a type alias and explicit template arguments built a QKeyEvent unseen; qApp->notify dispatched it.

    def test_r2_p2_a1_a_macro_over_a_token_of_the_reviewed_statement_fails(self) -> None:
        statement_line = "    table << ActionBinding{ QStringLiteral( \"actionPlay\" ),"
        mutated = self._mutated(statement_line, "#define QKeySequence(key) QKeySequence(int(key) | int(Qt::SHIFT))\n"
                                + statement_line, path=self._SPACE_TABLE)
        mutated[self._SPACE_TABLE] = mutated[self._SPACE_TABLE].replace(
            "KeyKind::Primary } } };", "KeyKind::Primary } } };\n#undef QKeySequence", 1)
        problems = scan_app_play_sources(mutated)
        self.assertTrue(any(p.startswith(f"{self._SPACE_TABLE}:") and "#define of QKeySequence" in p for p in problems),
                        problems)
        for name in ("Key_Space", "KeyKind", "Primary", "ActionBinding", "QStringLiteral", "table", "Qt"):
            for path in (self._SPACE_TABLE, "platform/qt/MainWindow.cpp"):
                with self.subTest(name=name, path=path):
                    problems = scan_app_play_sources({path: f"#define {name} x\n"})
                    self.assertIn(f"{path}:1: #define of {name}, a token of the reviewed Space binding statement", problems)

    def test_r2_p2_a1_every_includer_of_the_key_table_and_every_such_macro_is_loaded(self) -> None:
        self.assertRegex('#include "PlayerShortcutBinding.h"\n', _PLAY_RELEVANT)
        self.assertRegex("#define QKeySequence(k) k\n", _PLAY_RELEVANT)
        self.assertRegex("#  define Key_Space 0\n", _PLAY_RELEVANT)
        self.assertRegex("qApp->notify( this, &press );\n", _PLAY_RELEVANT)

    def test_r2_p2_a2_every_other_spelling_of_a_qkeyevent_is_a_synthesized_key(self) -> None:
        for injected in ("QKeyEvent press[1]{{ QEvent::KeyPress, 32, Qt::NoModifier }};",
                         "using Event = QKeyEvent;\n    Event press{ QEvent::KeyPress, 32, Qt::NoModifier };",
                         "using EventPtr = QKeyEvent*;",
                         "typedef QKeyEvent Event;",
                         "typedef QKeyEvent *EventPtr;",
                         "auto e = std::make_unique<QKeyEvent, QEvent::Type, int, Qt::KeyboardModifiers>( "
                         "QEvent::KeyPress, 32, Qt::NoModifier );",
                         "auto e = std::unique_ptr<QKeyEvent>();"):   # fail-closed: a key-event owner is reviewed here
            with self.subTest(injected):
                self._assert_caught(self._inject_after_measured_loop_off(injected), "synthesized key event")

    def test_r2_p2_a2_notify_with_an_event_is_a_dispatch_that_must_be_pinned(self) -> None:
        self._assert_caught(self._inject_after_measured_loop_off("qApp->notify( this, nullptr );"),
                            "event dispatch notify in platform/qt/MainWindow.cpp x1")
        # r3 A2 (HUB RULING HUB-TICK 20261118T1142Z): a local callable named notify is counted too (fail-closed); it
        # passes only at a pinned, reviewed file and count, never by a looser rule.
        self.assertEqual(self._event_dispatch_problems({"platform/qt/X.cpp": "auto f = [notify](bool) { notify(); };\n"}),
                         ["event dispatch notify in platform/qt/X.cpp x2: not a pinned site (the Play shortcut can be synthesized)"])

    def test_r2_p2_a2_reading_a_delivered_key_event_is_not_a_synthesized_key(self) -> None:
        for text in ("#include <QKeyEvent>\n", "void keyPressEvent( QKeyEvent *event ) override;\n",
                     "void f( const QKeyEvent &event );\n", "void g( QKeyEvent* event );\n"):
            with self.subTest(text):
                self.assertEqual(self._key_synth_problems({"platform/qt/StatusDialog.h": text}), [])

    def test_r2_a1_a_cast_of_a_delivered_key_event_is_not_a_synthesized_key(self) -> None:
        # FocusGuard reads the ShortcutOverride the user's key produced; reading an event is not making one.
        # r6 (adversary pass on c1f09601 A1): a name a read binds is only READ (a bare `return keyEvent;` hands the pointer
        # to a deduced type, so it is flagged now); the fixture reads it through a reviewed member, as FocusGuard does.
        text = "bool f( QEvent *event ) { QKeyEvent *keyEvent = static_cast<QKeyEvent*>( event ); return keyEvent->key(); }\n"
        # r5: the key table must hold its one reviewed binding (a count of zero fails), so the fixture carries it.
        self.assertEqual(self._key_synth_problems({self._SPACE_TABLE: self._SPACE_BINDING + text}), [])

    # r3 (adversary pass 3 on f64af6f5, HUB RULING HUB-TICK 20261118T1142Z: close each class with one cheap fail-closed
    # rule, never chase spellings). A1: a digraph `%:define` or a spliced `#de\<nl>fine` re-spelled a macro over the
    # unchanged table statement. The class is "the preprocessor in the key table": in the ONE pinned key-table file any
    # directive but the include guard, an #include and the reviewed `#ifdef Q_OS_WIN` / `#endif` fails, and so does any
    # digraph %:, any trigraph, or any line ending in a backslash, anywhere in the file (comments and literals included).
    _TABLE_STATEMENT_LINE = '    table << ActionBinding{ QStringLiteral( "actionPlay" ),'
    _R3_A1_DIRECTIVES = {
        "a digraph #define around the statement":
            ("%:define QKeySequence(key) QKeySequence(int(key) | int(Qt::SHIFT))", "%:undef QKeySequence"),
        "a backslash-newline spliced #define":
            ("#de\\\nfine QKeySequence(key) QKeySequence(int(key) | int(Qt::SHIFT))", "#undef QKeySequence"),
        "a trigraph #define": ("??=define QKeySequence(key) QKeySequence(int(key) | int(Qt::SHIFT))", ""),
        "an #undef": ("#undef QKeySequence", ""),
        "an #if around the statement": ("#if 1", "#endif"),
        "another #ifdef": ("#ifdef Q_OS_MAC", "#endif"),
        "a #pragma push_macro": ('#pragma push_macro("QKeySequence")', ""),
        "a #line": ("#line 1", ""),
        "a #define of a token outside the statement": ("#  define OTHER_MACRO 1", ""),
        "an #include_next": ("#include_next <QKeySequence>", ""),
        "a comment that splices the next line into itself": ("// a note \\", ""),
        # r3 adversary pass on fce56625 A1: a form feed or vertical tab is whitespace before `#` to the compiler.
        "a form feed before a #define":
            ("\f#define QKeySequence(key) QKeySequence(int(key) | int(Qt::SHIFT))", "\f#undef QKeySequence"),
        "a vertical tab before an #undef": ("\v#undef QKeySequence", ""),
        "a control character anywhere in the file": ("// a note \x0c with a form feed", ""),
        # r3 adversary pass on c1216855 A1: a quoted include of an unscanned fragment carried the macro in. Only a Qt
        # class header or a standard library header may be included by the key table.
        "a quoted include of a fragment around the statement":
            ('#include "player-shortcut-key-compat.inc"', '#include "player-shortcut-key-compat-end.inc"'),
        "a quoted include of a project header": ('#include "LocalHelper.h"', ""),
        "an angle include of a fragment": ("#include <compat.inc>", ""),
        "an angle include of a path": ("#include <../platform/qt/compat.h>", ""),
        # r5 adversary pass (f422d164 A1): an extensionless angle include of a project fragment, and a Qt-looking name,
        # carried a macro over the statement unscanned. The key table includes ONLY the reviewed headers, by name.
        "an extensionless angle include of a fragment around the statement":
            ("#include <shortcut_compat>", "#include <shortcut_restore>"),
        "a Qt-looking angle include of a fragment": ("#include <QShortcutCompat>", "#include <QShortcutRestore>"),
        "a module-qualified angle include that is not reviewed": ("#include <QtGui/QShortcutCompat>", ""),
        "a standard-library-looking include that is not reviewed": ("#include <type_traits>", ""),
    }

    def _key_table_with(self, before: str, after: str) -> dict[str, str]:
        mutated = self._mutated(self._TABLE_STATEMENT_LINE, before + "\n" + self._TABLE_STATEMENT_LINE, path=self._SPACE_TABLE)
        if after:
            mutated[self._SPACE_TABLE] = mutated[self._SPACE_TABLE].replace(
                "KeyKind::Primary } } };", "KeyKind::Primary } } };\n" + after, 1)
        return mutated

    def test_r3_a1_any_other_preprocessor_use_in_the_key_table_fails(self) -> None:
        for name, (before, after) in self._R3_A1_DIRECTIVES.items():
            with self.subTest(name):
                problems = scan_app_play_sources(self._key_table_with(before, after))
                self.assertTrue(any(p.startswith(f"{self._SPACE_TABLE}:") and "preprocessor" in p for p in problems),
                                problems)

    def test_r3_a1_the_key_table_directive_pin_is_exactly_the_reviewed_set(self) -> None:
        self.assertEqual(_app_play_scan.KEY_TABLE_SOURCE, self._SPACE_TABLE)
        self.assertEqual(_app_play_scan.KEY_TABLE_REVIEWED_DIRECTIVES, {
            "#ifndef PLAYERSHORTCUTBINDING_H": 1, "#define PLAYERSHORTCUTBINDING_H": 1, "#ifdef Q_OS_WIN": 1, "#endif": 2})

    def test_r3_a1_a_vanished_or_duplicated_reviewed_directive_fails(self) -> None:
        for name, mutated in {
                "the Windows #ifdef removed": self._mutated("#ifdef Q_OS_WIN\n", "", path=self._SPACE_TABLE),
                "a second Windows #ifdef": self._key_table_with("#ifdef Q_OS_WIN", "#endif")}.items():
            with self.subTest(name):
                problems = scan_app_play_sources(mutated)
                self.assertTrue(any(p.startswith(f"{self._SPACE_TABLE}:") and "preprocessor" in p for p in problems),
                                problems)

    def test_r3_a1_other_scanned_files_keep_the_identifier_define_rule_only(self) -> None:
        # The fail-closed directive rule is for the one key-table file; elsewhere only a #define of a statement token fails.
        for text in ("#undef QKeySequence\n", "#if 1\n#endif\n", "#define OTHER_MACRO 1\n", "// a note \\\n"):
            with self.subTest(text):
                problems = scan_app_play_sources({"platform/qt/OtherWidget.h": text})
                self.assertFalse([p for p in problems if "preprocessor" in p or "#define of" in p], problems)

    def test_r3_p2_a1_a_form_feed_or_vertical_tab_before_a_define_is_still_a_define(self) -> None:
        # The sibling: the identifier #define check every other scanned file keeps.
        for lead in ("\f", "\v", " \f\t"):
            with self.subTest(repr(lead)):
                problems = scan_app_play_sources({"platform/qt/MainWindow.cpp": lead + "#define QKeySequence(k) k\n"})
                self.assertIn("platform/qt/MainWindow.cpp:1: #define of QKeySequence, a token of the reviewed Space "
                              "binding statement", problems)

    def test_r3_p3_a1_the_key_table_may_include_only_its_reviewed_headers(self) -> None:
        # r5 (adversary pass on f422d164 A1): the shape rule (any Qt-looking or lowercase name) is replaced by the exact
        # reviewed set, so no extensionless project fragment can be spelled as a library header.
        for include in ("#include <QKeySequence>", "#include <algorithm>", "#include <QAction>"):
            with self.subTest(include):
                problems = scan_app_play_sources(self._key_table_with(include, ""))
                self.assertFalse([p for p in problems if "preprocessor" in p], problems)

    def test_r5_a1_the_reviewed_include_set_is_exactly_what_the_real_key_table_includes(self) -> None:
        blank, _ = strip_cpp(self.sources[self._SPACE_TABLE])
        real = set(re.findall(r"^[ \t]*#[ \t]*include[ \t]*<[ \t]*(\w+)[ \t]*>", blank, re.MULTILINE))
        self.assertEqual(real, set(_app_play_scan.KEY_TABLE_REVIEWED_INCLUDES))
        self.assertEqual(len(re.findall(r"^[ \t]*#[ \t]*include\b", blank, re.MULTILINE)), len(real))   # none quoted/pathed

    def test_r5_a2_a_key_event_pointer_or_reference_as_a_template_argument_is_a_synthesized_key(self) -> None:
        # adversary pass on f422d164 A2: `QKeyEvent*` in any template argument list but a cast's built a key event.
        for injected in ("std::remove_pointer_t<QKeyEvent*> press{ QEvent::KeyPress, 32, Qt::NoModifier };",
                         "std::remove_reference_t<QKeyEvent&> press{ QEvent::KeyPress, 32, Qt::NoModifier };",
                         "std::decay_t<QKeyEvent&> press{ QEvent::KeyPress, 32, Qt::NoModifier };",
                         "std::remove_pointer_t< const QKeyEvent * > press{ QEvent::KeyPress, 32, Qt::NoModifier };",
                         "std::remove_pointer<std::add_pointer_t<QKeyEvent*>>::type press{ QEvent::KeyPress, 32, {} };",
                         "std::tuple<int, QKeyEvent*>* t = nullptr;"):
            with self.subTest(injected):
                self._assert_caught(self._inject_after_measured_loop_off(injected), "synthesized key event")

    def test_r5_p2_a1_a2_a_type_computed_from_a_key_event_pointer_is_a_synthesized_key(self) -> None:
        # adversary pass on d3ad0b0f A1: a decltype over a cast named the object type; A2: a 600-character lookback cap
        # returned "no template" (exempt) on long whitespace. Also parentheses inside the template argument list.
        press = " press{ QEvent::KeyPress, 32, Qt::NoModifier };"
        for name, injected in {
                "decltype over a static_cast": "std::remove_pointer_t<decltype(static_cast<QKeyEvent*>(nullptr))>" + press,
                "__typeof__ over a cast": "std::remove_pointer_t<__typeof__(static_cast<QKeyEvent*>(nullptr))>" + press,
                "declval": "std::remove_pointer_t<decltype(std::declval<QKeyEvent*>())>" + press,
                "600 spaces": "std::remove_pointer_t<" + " " * 600 + "QKeyEvent*>" + press,
                "5000 spaces": "std::remove_pointer_t<" + " " * 5000 + "QKeyEvent*>" + press,
                "5000 newlines": "std::remove_pointer_t<" + "\n" * 5000 + "QKeyEvent*>" + press,
                "a parenthesised expression before it":
                    "std::remove_pointer_t<std::enable_if_t<(sizeof(int)>1), QKeyEvent*>>" + press}.items():
            with self.subTest(name):
                self._assert_caught(self._inject_after_measured_loop_off(injected), "synthesized key event")

    def test_r5_a2_casts_and_parameter_lists_with_a_delivered_key_event_stay_clean(self) -> None:
        for text in ("QKeyEvent *k = static_cast<QKeyEvent*>( e );\n", "auto *k = dynamic_cast< QKeyEvent const * >( e );\n",
                     "auto *k = reinterpret_cast<QKeyEvent *>( e );\n", "auto *k = const_cast<QKeyEvent*>( p );\n",
                     "auto *k = qobject_cast<QKeyEvent*>( e );\n",
                     "void f( QObject *o, QKeyEvent *e, std::vector<int> v, QKeyEvent const &c );\n"):
            with self.subTest(text):
                self.assertEqual(self._key_synth_problems({"platform/qt/StatusDialog.h": text}), [])

    def test_r5_a3_a_volatile_qualified_pointer_or_reference_is_not_a_synthesized_key(self) -> None:
        for text in ("void inspect(QKeyEvent volatile *event);\n", "void inspect(QKeyEvent const volatile &event);\n"):
            with self.subTest(text):
                self.assertEqual(self._key_synth_problems({"platform/qt/StatusDialog.h": text}), [])

    def test_r3_p3_a3_a_const_qualified_pointer_or_reference_is_not_a_synthesized_key(self) -> None:
        for text in ("auto inspect = [](QKeyEvent const *e) { return e->key(); };\n",
                     "auto inspect = [](QKeyEvent const &e) { return e.key(); };\n",
                     "void f( QKeyEvent const* event );\n"):
            with self.subTest(text):
                self.assertEqual(self._key_synth_problems({"platform/qt/StatusDialog.h": text}), [])
        # A const-qualified OBJECT is still a construction.
        problems = self._key_synth_problems({"platform/qt/StatusDialog.h":
                                             "void g() { QKeyEvent const press( QEvent::KeyPress, 32, {} ); }\n"})
        self.assertTrue(any("synthesized key event" in p for p in problems), problems)

    # r6 (adversary pass on 4adb9c0d A1 A2, HUB RULING HUB-TICK 20261118T1142Z): the exemption was decided by a backward
    # text walk that guessed the template context, and each stop character it knew (a C-style cast's `(`, a `{` inside a
    # balanced group) let one more spelling through. The rule is inverted: a QKeyEvent pointer / reference is exempt ONLY
    # as a function or lambda parameter declarator, or in a named-cast statement, in a plain block context; anything
    # else is flagged.
    _R6_PRESS = " press{ QEvent::KeyPress, 32, Qt::NoModifier };"
    _R6_TYPE_COMPUTATIONS = {
        "A1 decltype over a C-style cast": "std::remove_pointer_t<decltype((QKeyEvent*)nullptr)>" + _R6_PRESS,
        "A1 __typeof__ over a C-style cast": "std::remove_pointer_t<__typeof__((QKeyEvent*)nullptr)>" + _R6_PRESS,
        "A2 a brace inside a balanced parenthesised argument":
            "std::remove_pointer_t<std::enable_if_t<(sizeof(int{}) > 0), QKeyEvent*>>" + _R6_PRESS,
        "a functional-cast brace before the argument":
            "std::remove_pointer_t<std::conditional_t<decltype(0){} == 0, QKeyEvent*, int>>" + _R6_PRESS,
        "a lambda parameter inside a decltype":
            "std::remove_pointer_t<decltype([](QKeyEvent* e) { return e; }(nullptr))>" + _R6_PRESS,
        "a lambda parameter in a lambda body inside a decltype":
            "std::remove_pointer_t<decltype([]{ auto f = [](QKeyEvent* e) { return e; }; return f(nullptr); }())>"
            + _R6_PRESS,
        "a function type argument": "std::remove_pointer_t<std::function<void(QKeyEvent*)>*>" + _R6_PRESS,
        "a parameter list in a template argument with a comma":
            "std::tuple_element_t<1, std::tuple<int, QKeyEvent*, int>> p = nullptr;",
        "a C-style cast statement": "auto *k = (QKeyEvent*)nullptr;",
        "a member pointer": "struct Holder { QKeyEvent *held; };",
        "a local pointer with no cast": "QKeyEvent *k = nullptr;",
        "a default argument": "void f( QKeyEvent *e = nullptr );",
        "a cast inside a call argument": "g( static_cast<QKeyEvent*>( nullptr ) );",
    }

    def test_r6_a1_a2_a_key_event_pointer_outside_a_declarator_or_cast_statement_is_a_synthesized_key(self) -> None:
        for name, injected in self._R6_TYPE_COMPUTATIONS.items():
            with self.subTest(name):
                self._assert_caught(self._inject_after_measured_loop_off(injected), "synthesized key event")

    # r6 adversary pass on c1f09601 A1: an exempt read binds a name, and a LATER statement computes the object type from it
    # (no QKeyEvent spelled there). The class is "a delivered key event used to build one": no scanned source may use a
    # type-of operator, and a name an exempt read binds is used only as a reviewed member read or a whole call argument.
    _R6_P2_BUILT_FROM_A_READ = {
        "A1 decltype of a cast-bound pointer": "auto *keyType = static_cast<QKeyEvent*>(nullptr); "
                                               "std::remove_pointer_t<decltype(keyType)>" + _R6_PRESS,
        "A1 __typeof__ of a cast-bound pointer": "auto *keyType = static_cast<QKeyEvent*>(nullptr); "
                                                 "std::remove_pointer_t<__typeof__(keyType)>" + _R6_PRESS,
        "A1 a lambda that returns its key-event parameter": "auto f = [](QKeyEvent* e) { return e; }; "
                                                            "std::remove_pointer_t<decltype(f(nullptr))>" + _R6_PRESS,
        "a copy through a dereference": "QKeyEvent *k = static_cast<QKeyEvent*>( nullptr ); auto copy = *k;",
        "a copy through a subscript": "QKeyEvent *k = static_cast<QKeyEvent*>( nullptr ); auto copy = k[0];",
        "a clone": "QKeyEvent *k = static_cast<QKeyEvent*>( nullptr ); auto *c = k->clone();",
        "an alias pointer": "QKeyEvent *k = static_cast<QKeyEvent*>( nullptr ); auto *alias = k;",
        "a parameter that escapes": "auto f = [](QKeyEvent *e) { auto copy = *e; };",
    }

    def test_r6_p2_a1_a_key_event_built_from_a_delivered_read_is_a_synthesized_key(self) -> None:
        for name, injected in self._R6_P2_BUILT_FROM_A_READ.items():
            with self.subTest(name):
                self._assert_caught(self._inject_after_measured_loop_off(injected), "key event")

    # r6 adversary pass on a91a899d A1: grouping parentheses `(keyEvent)` were read as a whole call argument; A2: a whole
    # argument handed the event to a generic lambda that cloned or copied it. Handing a delivered event on is allowed
    # ONLY as the single argument of a qualified base key handler (`QDialog::keyPressEvent( event )`).
    _FOCUS_GUARD_CAST = "QKeyEvent *keyEvent = static_cast<QKeyEvent*>( event );"
    _R6_P3_HANDED_ON = (
        "auto *copy = (keyEvent)->clone();",
        "auto copy = *(keyEvent);",
        "auto *alias = (keyEvent); auto copy = *alias;",
        "auto *copy = [](auto *source) { return source->clone(); }(keyEvent);",
        "auto copy = [](auto *source) { return *source; }(keyEvent);",
        "auto *copy = make( keyEvent );",
        "auto *copy = keyPressEvent( keyEvent );",
        "QDialog::keyPressEvent( keyEvent, 1 );",
    )

    def test_r6_p3_a1_a2_a_delivered_key_event_is_handed_on_only_to_a_base_key_handler(self) -> None:
        for injected in self._R6_P3_HANDED_ON:
            with self.subTest(injected):
                self._assert_caught(self._mutated(self._FOCUS_GUARD_CAST, self._FOCUS_GUARD_CAST + "\n        " + injected,
                                                  path=self._SPACE_TABLE), "synthesized key event")
        # The real hand-on stays exempt (StatusDialog.cpp passes its event to QDialog::keyPressEvent).
        text = "void D::keyPressEvent( QKeyEvent *event )\n{\n    QDialog::keyPressEvent( event );\n}\n"
        self.assertEqual(self._key_synth_problems({"platform/qt/StatusDialog.cpp": text}), [])

    def test_r6_p2_a1_no_scanned_source_computes_a_type_from_an_expression(self) -> None:
        for text in ("int f() { int a = 0; decltype(a) b = a; return b; }\n", "__typeof__(1) x = 1;\n", "typeof(1) y = 1;\n"):
            with self.subTest(text):
                problems = scan_app_play_sources({"platform/qt/OtherWidget.h": text})
                self.assertTrue(any("type-of operator" in p for p in problems), problems)

    def test_r6_a1_a2_the_real_key_event_reads_stay_exempt(self) -> None:
        # The three real sites: a handler declaration, its definition, and FocusGuard's cast of the delivered event.
        for text in ("class D : public QDialog\n{\n    Q_OBJECT\nprotected:\n    void keyPressEvent( QKeyEvent *event ) override;\n};\n",
                     "void D::keyPressEvent( QKeyEvent *event )\n{\n    event->ignore();\n}\n",
                     "namespace N\n{\nclass G : public QObject\n{\npublic:\n    bool eventFilter( QObject *watched, QEvent *event ) "
                     "override\n    {\n        if( event->type() != QEvent::ShortcutOverride ) return false;\n"
                     "        QKeyEvent *keyEvent = static_cast<QKeyEvent*>( event );\n        return keyEvent->key();\n    }\n};\n}\n",
                     "void f()\n{\n    auto h = []( QKeyEvent *e ) { return e->key(); };\n}\n",
                     "int g( QEvent *e ) { return static_cast<QKeyEvent*>( e )->key(); }\n"):
            with self.subTest(text):
                self.assertEqual(self._key_synth_problems({"platform/qt/StatusDialog.h": text}), [])

    # r7 (sol r6 blocker, HUB RULING 2026-11-18 ROUND_TAIL): a C++14 digit separator (`1'000`) inside a number is part of the
    # number. The lexer read it as a character-literal opener, blanked the rest of the line (and its newline) and hid every
    # construction after it, in the pinned key-table file and in every file the other consumers scan.
    _R7_PRESS = " QKeyEvent press{ QEvent::KeyPress, 32, Qt::NoModifier };"
    _R7_SEPARATED_PRESS = {
        "decimal": "int delay = 1'000;" + _R7_PRESS,
        "alias": "int delay = 1'000; using Event=QKeyEvent; Event press{ QEvent::KeyPress, 32, Qt::NoModifier };",
        "hex": "int delay = 0x1'000;" + _R7_PRESS,
        "binary": "int delay = 0b1'0;" + _R7_PRESS,
        "suffixed": "unsigned delay = 1'000u;" + _R7_PRESS,
        "floating": "float delay = 1'000.5f;" + _R7_PRESS,
        "exponent": "double delay = 1'0e+1'0;" + _R7_PRESS,
        "leading dot": "double delay = .5'5;" + _R7_PRESS,
        "two separators": "long delay = 1'000'000;" + _R7_PRESS,
    }

    def test_r7_a_digit_separator_never_hides_a_key_event_construction_in_the_key_table(self) -> None:
        for name, injected in self._R7_SEPARATED_PRESS.items():
            with self.subTest(name):
                self._assert_caught(self._mutated(self._FOCUS_GUARD_CAST, self._FOCUS_GUARD_CAST + "\n        " + injected,
                                                  path=self._SPACE_TABLE), "synthesized key event")

    def test_r7_a_digit_separator_never_hides_a_protected_token_from_any_consumer(self) -> None:
        # One control per consumer the sol r6 class sweep named. Each runs twice: with a plain number (the twin must be
        # caught, so the control is valid) and with a digit separator (must be caught too).
        cast = self._FOCUS_GUARD_CAST
        controls = {
            "reference check": (lambda n: self._mutated(cast, cast + "\n        int delay = " + n + "; auto copy = *keyEvent;",
                                                        path=self._SPACE_TABLE), "key event"),
            "type-of": (lambda n: self._mutated(cast, cast + "\n        int delay = " + n + "; decltype(delay) twin = delay;",
                                                path=self._SPACE_TABLE), "type-of operator"),
            "dispatch": (lambda n: self._inject_after_measured_loop_off("int delay = " + n + "; (qApp->notify)( this, nullptr );"),
                         "event dispatch notify in platform/qt/MainWindow.cpp x1"),
            "following-line macro": (lambda n: {"platform/qt/OtherWidget.h": "int delay = " + n + ";\n#define Primary Secondary\n"},
                                     "#define of Primary"),
            "synthesis": (lambda n: self._mutated(cast, cast + "\n        int delay = " + n + ";" + self._R7_PRESS,
                                                  path=self._SPACE_TABLE), "synthesized key event"),
        }
        for name, (build, needle) in controls.items():
            for number in ("1000", "1'000", "0x1'000", "0b1'0", "1'000u", "1'000.5f"):
                with self.subTest(name + " " + number):
                    self._assert_caught(build(number), needle)

    def test_r7_real_character_literals_and_strings_are_still_lexed_as_literals(self) -> None:
        # Literals only (green before and after the fix): the separator rule must not touch a real literal.
        literals = ("char a = 'a'; char b = '\\''; char c = u8'x'; char16_t d = u'y'; char32_t e = U'z'; wchar_t f = L'w';\n"
                    "const char *s = \"it's\"; char g = '1'; char h = '2'; char i = '0'; auto j = (1 == 1) ? 'a' : 'b';\n")
        blank, keep = strip_cpp(literals)
        self.assertEqual(blank, ("char a = ' '; char b = '  '; char c = u8' '; char16_t d = u' '; char32_t e = U' '; wchar_t f = L' ';\n"
                                 "const char *s = \"    \"; char g = ' '; char h = ' '; char i = ' '; auto j = (1 == 1) ? ' ' : ' ';\n"))
        self.assertEqual(keep, literals)
        # The same literals beside separated numbers: numbers stay whole and the literals around them still blank.
        mixed = "int n = 1'000; char a = 'a'; int m = 0x1'000; const char *s = \"it's\"; char b = '\\''; float f = 1'0.5f;\n"
        blank, keep = strip_cpp(mixed)
        self.assertEqual(blank, "int n = 1'000; char a = ' '; int m = 0x1'000; const char *s = \"    \"; char b = '  '; float f = 1'0.5f;\n")
        self.assertEqual(keep, mixed)
        # A key-event spelling inside a literal is still not a construction, with a separated number on the same line.
        literal = "const char *s = \"QKeyEvent press{}\"; char c = 'Q'; int n = 1'000;\nchar x = 'K';\n"
        self.assertEqual(self._key_synth_problems({"platform/qt/StatusDialog.h": literal}), [])

    # r8 (sol r7 blocker, HUB RULING 2026-11-18 ROUND_TAIL): the named-cast exemption read the cast's operand group as an
    # empty `()`, so a cast whose operand PRODUCES an event (`event->clone()`) passed as a read of the delivered event. A cast
    # is exempt only when its operand is the bare identifier of the delivered event; any other operand (a call, a member
    # access, `->clone()`, a nested cast, a dereference) is never exempt.
    _R8_FOCUS_GUARD_CAST_RE = "QKeyEvent *keyEvent = static_cast<QKeyEvent*>( event );"
    _R8_CLONE_OPERANDS = {
        "typed declaration, clone": "QKeyEvent *keyEvent = static_cast<QKeyEvent*>( event->clone() );",
        "auto declaration, clone": "auto *keyEvent = static_cast<QKeyEvent*>( event->clone() );",
        "dynamic_cast declaration, clone": "QKeyEvent *keyEvent = dynamic_cast<QKeyEvent*>( event->clone() );",
        "qobject_cast declaration, clone": "QKeyEvent *keyEvent = qobject_cast<QKeyEvent*>( event->clone() );",
        "reinterpret_cast declaration, clone": "QKeyEvent *keyEvent = reinterpret_cast<QKeyEvent*>( event->clone() );",
        "a call": "QKeyEvent *keyEvent = static_cast<QKeyEvent*>( cloneOf( event ) );",
        "a call with no argument": "QKeyEvent *keyEvent = static_cast<QKeyEvent*>( makeEvent() );",
        "a member access": "QKeyEvent *keyEvent = static_cast<QKeyEvent*>( holder.event );",
        "an arrow member access": "QKeyEvent *keyEvent = static_cast<QKeyEvent*>( holder->event );",
        "a dereference": "QKeyEvent *keyEvent = static_cast<QKeyEvent*>( *eventPointer );",
        "a nested cast": "QKeyEvent *keyEvent = static_cast<QKeyEvent*>( static_cast<QEvent*>( event ) );",
        "a subscript": "QKeyEvent *keyEvent = static_cast<QKeyEvent*>( events[0] );",
        "a qualified name": "QKeyEvent *keyEvent = static_cast<QKeyEvent*>( Holder::event );",
        "a grouped operand": "QKeyEvent *keyEvent = static_cast<QKeyEvent*>( ( event ) );",
        "a ternary": "QKeyEvent *keyEvent = static_cast<QKeyEvent*>( ok ? event : other );",
        "a new expression": "QKeyEvent *keyEvent = static_cast<QKeyEvent*>( new QEvent( QEvent::KeyPress ) );",
        "an empty operand": "QKeyEvent *keyEvent = static_cast<QKeyEvent*>( );",
    }

    def test_r8_a_cast_whose_operand_produces_an_event_is_a_synthesized_key_in_the_declaration_form(self) -> None:
        for name, injected in self._R8_CLONE_OPERANDS.items():
            with self.subTest(name):
                self._assert_caught(self._mutated(self._R8_FOCUS_GUARD_CAST_RE, injected, path=self._SPACE_TABLE),
                                    "synthesized key event")

    def test_r8_a_cast_whose_operand_produces_an_event_is_a_synthesized_key_in_the_assignment_and_inline_forms(self) -> None:
        cast = self._R8_FOCUS_GUARD_CAST_RE
        for name, injected in {
                "assignment, clone": "keyEvent = static_cast<QKeyEvent*>( event->clone() );",
                "assignment, call": "keyEvent = static_cast<QKeyEvent*>( cloneOf( event ) );",
                "an inline member read through a clone cast": "int copiedKey = static_cast<QKeyEvent*>( event->clone() )->key();",
                "a return of a member read through a clone cast": "return static_cast<QKeyEvent*>( event->clone() )->key();",
                "an inline member read through a call cast": "int copiedKey = static_cast<QKeyEvent*>( cloneOf( event ) )->key();",
                "a bare inline cast of a clone": "static_cast<QKeyEvent*>( event->clone() )->accept();"}.items():
            with self.subTest(name):
                self._assert_caught(self._mutated(cast, cast + "\n        " + injected, path=self._SPACE_TABLE),
                                    "synthesized key event")

    def test_r8_a_cast_whose_operand_produces_an_event_is_caught_in_a_fixture_file(self) -> None:
        # The same class off the real table: a fixture file, so no pinned-site machinery is involved.
        for name, injected in self._R8_CLONE_OPERANDS.items():
            with self.subTest(name):
                text = "bool f( QObject *w, QEvent *event )\n{\n    " + injected + "\n    return keyEvent->key();\n}\n"
                problems = self._key_synth_problems({"platform/qt/StatusDialog.h": text})
                self.assertTrue(any("synthesized key event" in p for p in problems), problems)

    def test_r8_a_cast_of_the_bare_delivered_event_identifier_stays_exempt(self) -> None:
        # Negative controls: the real tree scans clean, and every bare-identifier cast (any spacing, any cast keyword, a
        # member read through it) is still a read of the delivered event.
        self.assertEqual(scan_app_play_sources(self.sources), [])
        head = "bool f( QObject *w, QEvent *event )\n{\n"
        bound = "    QKeyEvent *keyEvent = static_cast<QKeyEvent*>( event );\n"
        for text in (bound + "    return true;\n",
                     "    QKeyEvent *keyEvent = static_cast<QKeyEvent*>(event);\n    return true;\n",
                     "    QKeyEvent *keyEvent = static_cast<QKeyEvent*>(\n        event\n    );\n    return true;\n",
                     "    QKeyEvent *keyEvent = static_cast<QKeyEvent*>( event_ );\n    return true;\n",
                     "    auto *keyEvent = dynamic_cast<QKeyEvent*>( event );\n    return true;\n",
                     "    auto *keyEvent = qobject_cast<QKeyEvent*>( e );\n    return true;\n",
                     "    keyEvent = static_cast<QKeyEvent*>( event );\n    return true;\n",
                     "    int k = static_cast<QKeyEvent*>( event )->key();\n    return k;\n",
                     "    return static_cast<QKeyEvent*>( event )->key();\n"):
            with self.subTest(text):
                self.assertEqual(self._key_synth_problems({"platform/qt/StatusDialog.h": head + text + "}\n"}), [])

    def test_r8_a_trailing_dot_at_the_end_of_a_file_with_no_final_newline_does_not_crash_the_lexer(self) -> None:
        # adversary pass on 70336ec9 A3 (advisory, introduced by r7): `.` as the LAST character took the leading-dot number
        # branch because an empty `nxt` is "in" the digit string, then dereferenced a failed regex match.
        for text in ("#define DOT .", "// QKeyEvent: a comment only\n#define DOT .", ".", "int x = 1.", "int x = 0x1'0", "x = 5 ."):
            with self.subTest(text):
                blank, keep = strip_cpp(text)
                self.assertTrue(blank.endswith(text[-1]), blank)   # the last token survives (comment text is blanked)
                self.assertTrue(keep.endswith(text[-1]), keep)
                scan_app_play_sources({"platform/qt/OtherWidget.h": text})

    def test_r3_p2_a2_a_qualified_qkeyevent_include_is_not_a_synthesized_key(self) -> None:
        for text in ("#include <QtGui/QKeyEvent>\n", "#include <qevent.h>\n", '#include "QKeyEvent"\n'):
            with self.subTest(text):
                self.assertEqual(self._key_synth_problems({"platform/qt/StatusDialog.h": text}), [])

    # A2: a parenthesised `(qApp->notify)( w, e )` or a grouped `(QCoreApplication::sendEvent)(...)` escaped _EVENT_POST,
    # which required `name(`. The class is "a dispatch named any other way": EVERY identifier mention of postEvent,
    # sendEvent, sendSpontaneousEvent or notify is counted against the pinned sites (a call, a grouping, a pointer).
    _R3_A2_MUTANTS = {
        "(qApp->notify)( this, nullptr );": "event dispatch notify in platform/qt/MainWindow.cpp x1",
        "(QCoreApplication::sendEvent)( this, nullptr );": "event dispatch sendEvent in platform/qt/MainWindow.cpp x1",
        "(QCoreApplication::postEvent)( this, nullptr );": "event dispatch postEvent in platform/qt/MainWindow.cpp x2",
        "auto dispatch = &QCoreApplication::sendSpontaneousEvent;":
            "event dispatch sendSpontaneousEvent in platform/qt/MainWindow.cpp x1",
        "auto dispatch = std::mem_fn( &QApplication::notify );": "event dispatch notify in platform/qt/MainWindow.cpp x1",
    }

    def test_r3_a2_every_mention_of_a_dispatch_name_is_counted(self) -> None:
        for injected, needle in self._R3_A2_MUTANTS.items():
            with self.subTest(injected):
                self._assert_caught(self._inject_after_measured_loop_off(injected), needle)

    def test_r3_a2_a_grouped_notify_in_the_focus_guard_is_caught(self) -> None:
        anchor = "QKeyEvent *keyEvent = static_cast<QKeyEvent*>( event );"
        self._assert_caught(self._mutated(anchor, anchor + "\n        (qApp->notify)( QApplication::focusWidget(), keyEvent );",
                                          path=self._SPACE_TABLE),
                            "event dispatch notify in platform/qt/PlayerShortcutBinding.h x1")

    def test_r3_a2_every_file_that_mentions_a_dispatch_name_is_loaded(self) -> None:
        for text in ("(qApp->notify)( w, e );\n", "connectReady(notify)\n", "auto p = &QCoreApplication::postEvent;\n",
                     "(QCoreApplication::sendSpontaneousEvent)( w, e );\n"):
            with self.subTest(text):
                self.assertRegex(text, _PLAY_RELEVANT)
        # The two local callables named notify are loaded and pinned by file and count (reviewed, not a looser rule).
        for path in ("platform/qt/FocusPixelMapManager.cpp", "platform/qt/SyncDownloadWaiter.h"):
            with self.subTest(path):
                self.assertIn(path, self.sources)

    def test_the_binding_exemption_needs_the_exact_expression(self) -> None:
        # A Key_Space merely NEAR a QKeySequence is not the reviewed expression.
        for text in ("QKeySequence( Qt::Key_Space + 0 )", "QKeySequence( k ), Qt::Key_Space", "QKeySequence( Key_Space )"):
            with self.subTest(text):
                problems = self._key_synth_problems({self._SPACE_TABLE: "inline void w() { f( " + text + " ); }\n"})
                self.assertTrue(any("synthesized key event" in p for p in problems), problems)

    def test_a_third_postevent_fails_the_scan(self) -> None:
        third = "\nvoid h() { QCoreApplication::postEvent( this, new QKeyEvent( QEvent::KeyPress, 0, {} ) ); }\n"
        for path in ("platform/qt/MainWindow.cpp", "platform/qt/LookAssistAnalysisPump.h", "platform/qt/OtherWidget.cpp"):
            with self.subTest(path):
                sources = dict(self._PINNED_POST_FIXTURE)
                sources[path] = sources.get(path, "") + third
                problems = self._event_dispatch_problems(sources)
                count = 2 if path in self._PINNED_POST_FIXTURE else 1
                self.assertEqual(problems, [f"event dispatch postEvent in {path} x{count}: not a pinned site "
                                            "(the Play shortcut can be synthesized)"])

    def test_the_tokenizer_is_not_fooled_by_comments_or_strings(self) -> None:
        blank, keep = strip_cpp('a = "//"; b(); // c\n/* d */ e = \'x\'; f = R"x(//)x";\n')
        self.assertIn("b();", blank)
        self.assertNotIn("c", blank.replace("b", "").replace("e", "").replace("f", "").replace("a", ""))
        self.assertIn('"//"', keep)
        self.assertEqual(blank.count("\n"), 2)

    def test_the_scan_finds_the_original_pre_enforce_2_tree_red(self) -> None:
        # The ENFORCE-1 head (767219f0) plays through bare actionPlay->trigger() and enables Loop in its
        # warm-up: embedded excerpt of that code (red-first for this class).
        excerpt = {
            "platform/qt/MainWindow.cpp":
                "            if( !loopWasCheckedForLookAssist ) ui->actionLoop->trigger();\n"
                "            if( !ui->actionPlay->isChecked() )\n            {\n"
                "                ui->actionPlay->trigger();\n            }\n",
        }
        problems = scan_app_play_sources(excerpt)
        self.assertTrue(any("Loop action" in p for p in problems), problems)
        self.assertTrue(any("Play action" in p for p in problems), problems)

    # -- ENFORCE-2 round 2: pins for each blocker's resolution -------------------------------------------

    def test_b1_every_caller_requests_a_window_that_reaches_the_floor(self) -> None:
        _, code = strip_cpp(self.sources[self.MAIN_WINDOW])
        # the gate evaluates the request with the unit-tested rule; the shared helper rejects < 20 s
        self.assertIn("checkPlayableWindow( site, requestedSeconds )", code)
        main = strip_cpp(self.sources[self.MAIN])[1]
        self.assertIn("PLAY_DURATION_TOO_SHORT (--seconds=", main)
        # HFR-10S-1: at parse the clip is not open, so only what NO clip could admit is refused there; the clip's own
        # floor is applied by runGuiPlaybackSmoke once it is open, before anything plays.
        self.assertRegex(main, r"seconds \+ 1e-9 < playback_frame_range::kLowestMinPlayWindowSeconds")
        self.assertIn("if( options.durationMs + 1e-6 < playGateMinPlayWindowSeconds() * 1000.0 )", code)
        smoke = _function_body(self.sources[self.MAIN_WINDOW], "int MainWindow::runGuiPlaybackSmoke(")
        self.assertLess(smoke.index("options.durationMs + 1e-6 < playGateMinPlayWindowSeconds()"),
                        smoke.index('programmaticPlay( "gui-smoke-measured"'))

    def test_b2_every_automation_mode_waits_for_the_engines_source_frame_count(self) -> None:
        # ENFORCE-3: the ENFORCE-2 wall-clock floor (playHoldReachedFloor) is retired. Each automation mode waits on
        # programmaticPlayState (the engine's counted source frames); the exact statements are pinned in
        # app_play_scan.PINNED_STOP_STATEMENTS and the wait loops in PINNED_WAIT_LOOPS (both mutation-tested below).
        _, code = strip_cpp(self.sources[self.MAIN_WINDOW])
        self.assertNotIn("playHoldReachedFloor", code)
        for pinned in ("programmaticPlayState( autoSettleClock.elapsed(), autoSettleSafetyMs )",
                       "programmaticPlayState( playActionClock.elapsed(), playActionSafetyMs )",
                       "programmaticPlayState( playbackClock.elapsed(), measuredSafetyMs )",
                       "programmaticPlayState( autoplayClock->elapsed(),"):
            self.assertEqual(code.count(pinned), 1, pinned)
        self.assertNotIn("playActionTimeout", code)
        # the lifecycle stress switch: only once the engine counted the source frames, refused below 20 s at launch
        self.assertIn("&& programmaticPlayConsumed()", code)
        self.assertIn("!playback_frame_range::stressSwitchReachesFloor( options.stressSwitchAtMs, playGateMinPlayWindowSeconds() )", code)
        header = (ROOT / "platform" / "qt" / "MainWindow.h").read_text(encoding="utf-8")
        self.assertRegex(header, r"int stressSwitchAtMs = 20000;")
        main = strip_cpp(self.sources[self.MAIN])[1]
        self.assertIn("stressSwitchReachesFloor(stressSwitchAtMs, playback_frame_range::kLowestMinPlayWindowSeconds)", main)
        self.assertRegex(main, r'QStringLiteral\("20000"\)')
        self.assertNotRegex(main, r'QStringLiteral\("1000"\)\);\s*parser\.addOption\(stressSwitchAtMsOpt\)')
        # a measured Play that did not consume the source frames is a typed failure, never a pass
        self.assertIn('programmaticStop( "gui-smoke-measured-early-end" )', code)
        self.assertIn("playStopFailureReason( measuredState )", code)

    def test_b2_the_play_window_is_timed_from_the_moment_play_has_started(self) -> None:
        _, code = strip_cpp(self.sources[self.MAIN_WINDOW])
        after_play = code[code.index('"gui-smoke-measured",'):]
        self.assertIn("playbackClock.restart();", after_play[:2500])

    def test_b3_the_gate_measures_the_range_the_engine_plays(self) -> None:
        _, code = strip_cpp(self.sources[self.MAIN_WINDOW])
        body = _function_body(self.sources[self.MAIN_WINDOW], "playback_frame_range::PlayableWindowVerdict MainWindow::checkPlayableWindow(")
        self.assertIn("!f3CutRangeRepairDisabledByEnvironment()", body)
        mutated = self._mutated("!f3CutRangeRepairDisabledByEnvironment(),", "true,")
        mutated_body = _function_body(mutated[self.MAIN_WINDOW], "playback_frame_range::PlayableWindowVerdict MainWindow::checkPlayableWindow(")
        self.assertNotIn("f3CutRangeRepairDisabledByEnvironment", mutated_body)
        # the knob is the ONLY environment variable that touches the cut range anywhere in platform/qt
        range_knobs = set()
        for path, text in self.sources.items():
            for name in re.findall(r"MLVAPP_[A-Z0-9_]*(?:CUT_RANGE|CUTRANGE|CUT_IN|CUT_OUT)[A-Z0-9_]*", strip_cpp(text)[1]):
                range_knobs.add(name)
        self.assertEqual(range_knobs, {"MLVAPP_F3_DISABLE_CUT_RANGE_REPAIR"})

    # -- PLAYBACK-CLIP-LENGTH-ENFORCE-3: the source-frame class, pinned and mutation-tested -------------------

    def _enforce3_mutations(self) -> dict[str, dict[str, str]]:
        """name -> a deliberately broken copy of the real sources the scan must flag."""
        restart = "playbackClock.restart();"
        loop_body = (r"(== playback_frame_range::PlayStopState::Continue \)\s*\{)"
                     r"(\s*qApp->processEvents\( QEventLoop::AllEvents \);\s*sleepGuiThreadForSmokeRunner\( 10 \);)")
        return {
            # sol r2 hardening: a raw stop inserted after the clock restart (the exact repro)
            "raw setChecked(false) after playbackClock.restart()":
                self._mutated(restart, restart + "\n    ui->actionPlay->setChecked( false );"),
            "raw on_actionPlay_triggered(false) after the restart":
                self._mutated(restart, restart + "\n    on_actionPlay_triggered( false );"),
            "a conditional raw stop on a wall clock":
                self._mutated(restart, restart + "\n    if( playbackClock.elapsed() > 5000 ) ui->actionPlay->setChecked( false );"),
            "a new programmaticStop site": self._mutated(restart, restart + '\n    programmaticStop( "a-new-early-stop" );'),
            # sol r2 hardening: COMPLETE request expressions
            "settle request divided by 20":
                self._mutated_re(r'(programmaticPlay\(\s*"profile-look-assist-settle"\s*,\s*playGateMinPlayWindowSeconds\(\))',
                                 r"\1 / 20.0"),
            "play-action request minus 19":
                self._mutated_re(r'(programmaticPlay\(\s*"profile-exercise-play-action"\s*,\s*playGateMinPlayWindowSeconds\(\))',
                                 r"\1 - 19"),
            "autoplay request times zero": self._mutated("programmaticPlay( \"autoplay\", autoplaySeconds )",
                                                          "programmaticPlay( \"autoplay\", autoplaySeconds * 0 )"),
            "smoke request widened": self._mutated_re(
                r'(playback_frame_range::smokePlayRequestSeconds\(\s*options\.durationMs,\s*options\.targetPresentedFrames,\s*presentedTargetFps\s*\))',
                r"\1 + 100.0"),
            # sol r2 hardening: an extra exit condition inside a hold loop
            "an extra break in the settle hold loop": self._mutated_re(loop_body, r"\1 if( m_lastLookAssistDiagnosticsValid ) break; \2"),
            # the wall clock comes back
            "a wall-clock wait loop on the Play action":
                self._mutated(restart, restart + "\n    while( playbackClock.elapsed() < 20000 && ui->actionPlay->isChecked() ) { qApp->processEvents(); }"),
            "the retired wall-clock predicate":
                self._mutated(restart, restart + "\n    const bool wallClockFloor = playHoldReachedFloor( playbackClock.elapsed() );"),
            # REFERENCE-FRAME-PRESENTED-COUNT-LOOP-EXIT-1: the loop's stop decision is measuredPlayLoopShouldStop (its states are
            # exercised by PlaybackMeasuredLoopExit in test_playback_frame_range.cpp); the call must still see the real state and
            # the real presented counts, or a pinned capture is back to breaking on a timeline Reached (exit 11).
            "the measured loop ignores an engine that ended early":
                self._mutated_re(r"measuredPlayLoopShouldStop\(\s*measuredState,",
                                 "measuredPlayLoopShouldStop( measuredState == playback_frame_range::PlayStopState::EndedEarly "
                                 "? playback_frame_range::PlayStopState::Continue : measuredState,"),
            "the measured loop stops on a timeline Reached before the pinned presented count": self._mutated_re(
                r"m_playbackSmokeTargetPresentedFrames,\s*playback_frame_range::pinnedPresentStopCount\(\s*m_playbackSmokePinnedWindow\s*\),\s*m_playRequiredSourceFrames,\s*ui->actionPlay->isChecked\(\),\s*m_sourceAdvance\.consumed\(\)\s*\)\s*\)\s*\{\s*break;",
                "0, 0, 0, false, 0 ) ) { break;"),
            # r8 (the r7 venue run, observed=470 of 480): the loop counts the distinct window frames shown, never raw presents
            "the measured loop counts raw presents again": self._mutated_re(
                r"playback_frame_range::pinnedPresentStopCount\(\s*m_playbackSmokePinnedWindow\s*\),\s*m_playRequiredSourceFrames,",
                "m_playbackSmokePresentedFrames, m_playRequiredSourceFrames,"),
            # adversary A2/A3 on 7f0f6d58: the loop is told the admitted window and whether Play still runs
            "the measured loop is no longer told the admitted window": self._mutated_re(
                r"pinnedPresentStopCount\(\s*m_playbackSmokePinnedWindow\s*\),\s*m_playRequiredSourceFrames,",
                "pinnedPresentStopCount( m_playbackSmokePinnedWindow ), INT64_MAX,"),
            "the measured loop is no longer told whether Play still runs": self._mutated_re(
                r"pinnedPresentStopCount\(\s*m_playbackSmokePinnedWindow\s*\),\s*m_playRequiredSourceFrames,\s*ui->actionPlay->isChecked\(\),",
                "pinnedPresentStopCount( m_playbackSmokePinnedWindow ), m_playRequiredSourceFrames, true,"),
            # r3 (Grok G1(b) on ee8f00bc): the loop is told how far the timeline has run, or a stalled present plays on past the window
            "the measured loop is no longer told how far the timeline has run": self._mutated_re(
                r"ui->actionPlay->isChecked\(\),\s*m_sourceAdvance\.consumed\(\)\s*\)\s*\)\s*\{\s*break;",
                "ui->actionPlay->isChecked(), m_playRequiredSourceFrames ) ) { break;"),
            # the engine counts, the gate paces, the stop waits
            "the presented-frames stop no longer waits for the source frames": self._mutated_re(
                r"\n\s*&& programmaticPlayConsumed\(\)\s*// ENFORCE-3: N presented frames[^\n]*", ""),
            "the gate is not told the engine pace": self._mutated("enginePaceFps > 0.0 ? enginePaceFps : -1.0, automationPlayPaceMode() );", "0.0, automationPlayPaceMode() );"),
            # CPU-LOOK-LEG-PACE-ABORT-1: the pace mode reaches the admission, the stop decision and every safety-net budget
            "the admission is no longer told the pace mode": self._mutated("enginePaceFps > 0.0 ? enginePaceFps : -1.0, automationPlayPaceMode() );", "enginePaceFps > 0.0 ? enginePaceFps : -1.0 );"),
            "the stop decision is no longer told the pace mode": self._mutated("elapsedMs, safetyMs, automationPlayPaceMode(), m_playPaceHistory );", "elapsedMs, safetyMs, m_playPaceHistory );"),
            # MERGE-MASTER r11 (ADV-A1b): the probe judges the pace the Play has shown, so it must be noted and handed over
            "the stop decision is no longer given the pace history": self._mutated("automationPlayPaceMode(), m_playPaceHistory );", "automationPlayPaceMode() );"),
            "the observation is no longer noted in the pace history": self._mutated("m_playPaceHistory.note( m_sourceAdvance.consumed(), elapsedMs );", ""),
            "the autoplay budget ignores the pace mode": self._mutated(
                "playback_frame_range::playSafetyMs( m_playRequestedSeconds, automationPlayPaceMode() ) );\n                        if( autoplayState",
                "playback_frame_range::playSafetyMs( m_playRequestedSeconds ) );\n                        if( autoplayState"),
            "the settle budget ignores the pace mode": self._mutated(
                "autoSettleSafetyMs = playback_frame_range::playSafetyMs( m_playRequestedSeconds, automationPlayPaceMode() );",
                "autoSettleSafetyMs = playback_frame_range::playSafetyMs( m_playRequestedSeconds );"),
            "the play-action budget ignores the pace mode": self._mutated(
                "playActionSafetyMs = playback_frame_range::playSafetyMs( m_playRequestedSeconds, automationPlayPaceMode() );",
                "playActionSafetyMs = playback_frame_range::playSafetyMs( m_playRequestedSeconds );"),
            "the engine tick stops feeding the counter (non-drop)":
                self._mutated("m_sourceAdvance.noteEngineTick( sourcePositionBeforeTick,", "(void)( sourcePositionBeforeTick,"),
            "the engine tick stops feeding the counter (drop-frame)":
                self._mutated("m_sourceAdvance.noteEngineTick( sourcePositionBeforeDropTick,", "(void)( sourcePositionBeforeDropTick,"),
            "getFramerate reads the persisted override again":
                self._mutated("if( m_fpsOverride && !m_automationPacingIsolated ) return m_frameRate;",
                              "if( m_fpsOverride ) return m_frameRate;"),
            "the smoke entry no longer isolates pacing": self._mutated('isolateAutomationPacing( "gui-smoke-entry" );', ""),
            "the profile entry no longer isolates pacing": self._mutated('isolateAutomationPacing( "profile-entry" );', ""),
            "the autoplay hook no longer isolates pacing": self._mutated('isolateAutomationPacing( "autoplay" );', ""),
            "the gate stops resetting the counter for its Play": self._mutated(
                "m_sourceAdvance = playback_frame_range::SourceFrameAdvanceCounter();", ""),
            "the gate stops recording the required frames": self._mutated(
                "m_playRequiredSourceFrames = verdict.requiredFrames;", ""),
            "programmaticPlayConsumed always true": self._mutated(
                "return m_playRequiredSourceFrames > 0 && m_sourceAdvance.consumed() >= m_playRequiredSourceFrames;",
                "return true;"),
            "the autoplay poll stops without asking the engine": self._mutated(
                "if( autoplayState == playback_frame_range::PlayStopState::Continue ) return;", ""),
        }

    def test_enforce3_every_source_frame_mutation_is_caught(self) -> None:
        for name, mutated in self._enforce3_mutations().items():
            with self.subTest(name):
                problems = scan_app_play_sources(mutated)
                self.assertTrue(problems, f"the scan did not go red on: {name}")

    def test_enforce3_sol_r2_repro_the_old_scan_returned_nothing(self) -> None:
        # Red-first: the f9998815 scan (a raw setChecked(false) was a "safe form" anywhere) is reproduced as the
        # property it lacked -- the mutation above must be caught by NAMING the function, not by luck.
        restart = "playbackClock.restart();"
        problems = scan_app_play_sources(
            self._mutated(restart, restart + "\n    ui->actionPlay->setChecked( false );"))
        self.assertTrue(any("MainWindow::runGuiPlaybackSmoke" in p and "(3, 0, 2)" in p for p in problems), problems)

    def test_player_fullscreen_keeps_playing_1_a_resize_stop_is_an_unreviewed_stop(self) -> None:
        # PLAYER-FULLSCREEN-KEEPS-PLAYING-1: resizeEvent no longer stops Play, so its pin is gone; putting the old
        # stop back (a fullscreen toggle ends playback again) must fail the scan by naming the function.
        from tools.repo_hygiene.app_play_scan import REVIEWED_PLAY_STOPS
        self.assertNotIn("MainWindow::resizeEvent", REVIEWED_PLAY_STOPS)
        self.assertEqual(scan_app_play_sources(self.sources), [])
        anchor = "case resize_playback_policy::Action::KeepPlaying:"
        problems = scan_app_play_sources(self._mutated(anchor, anchor + "\n        ui->actionPlay->setChecked( false );"))
        self.assertTrue(problems, "the scan did not go red on a resize stop")
        self.assertTrue(any("unreviewed Play stop in MainWindow::resizeEvent" in p for p in problems), problems)

    # PLAYER-FULLSCREEN-CONTROL-BAR-1 r3: the bar's Play button is one reviewed user-input trigger, pinned as the WHOLE
    # fullscreenBarInput body; the bar reads the play state only through isChecked() (no connect on the action).
    _BAR_TOGGLE = "case fullscreen_bar::Action::TogglePlay: ui->actionPlay->trigger(); fullscreenBarSyncPlayState(); break;"

    def test_player_fullscreen_control_bar_1_the_bar_input_body_is_pinned(self) -> None:
        self.assertIn("bool MainWindow::fullscreenBarInput(", PINNED_HELPER_BODIES)
        self.assertEqual(scan_app_play_sources(self.sources), [])

    def test_player_fullscreen_control_bar_1_a_second_trigger_in_the_bar_input_changes_the_pin(self) -> None:
        problems = scan_app_play_sources(self._mutated(
            self._BAR_TOGGLE, self._BAR_TOGGLE.replace("trigger();", "trigger(); ui->actionPlay->trigger();", 1)))
        self.assertTrue(any("pinned helper body changed" in p and "fullscreenBarInput" in p for p in problems), problems)

    def test_player_fullscreen_control_bar_1_a_trigger_on_another_action_changes_the_pin(self) -> None:
        problems = scan_app_play_sources(self._mutated(
            "case fullscreen_bar::Action::PrevFrame: ui->actionPreviousFrame->trigger(); break;",
            "case fullscreen_bar::Action::PrevFrame: ui->actionPlay->trigger(); break;"))
        self.assertTrue(any("pinned helper body changed" in p and "fullscreenBarInput" in p for p in problems), problems)

    def test_player_fullscreen_control_bar_1_a_connect_on_the_play_action_is_unreviewed(self) -> None:
        # The r2 head's feed (hosted CI run 44773031416): a connect on the Play action in the bar's show path.
        anchor = "m_fullscreenBarFrameFeed = true;"
        problems = scan_app_play_sources(self._mutated(anchor, "m_fullscreenBarPlayFeed = connect( ui->actionPlay, "
                                                       "&QAction::toggled, this, [this]( bool ) { fullscreenBarFeed(); } );\n"
                                                       "        " + anchor))
        self.assertTrue(any("unreviewed use of the Play action" in p and "&QAction::toggled" in p for p in problems), problems)

    def test_player_fullscreen_control_bar_1_a_second_dispatch_in_the_bar_widget_is_unreviewed(self) -> None:
        bar = "platform/qt/FullscreenControlBar.cpp"
        self.assertEqual(PINNED_EVENT_SITES[(bar, "sendEvent")], 1)
        anchor = "    QCoreApplication::sendEvent( target, &copy );"
        problems = scan_app_play_sources(self._mutated(anchor, anchor + "\n" + anchor, path=bar))
        self.assertTrue(any("event dispatch sendEvent in platform/qt/FullscreenControlBar.cpp x2" in p for p in problems),
                        problems)

    def test_player_fullscreen_control_bar_1_a_trigger_outside_the_pinned_body_is_unreviewed(self) -> None:
        anchor = "void MainWindow::fullscreenBarSyncPlayState( void )\n{"
        problems = scan_app_play_sources(self._mutated(anchor, anchor + "\n    ui->actionPlay->trigger();"))
        self.assertTrue(any("unreviewed use of the Play action" in p for p in problems), problems)

    def test_player_toolbar_transport_buttons_1_the_stop_button_is_one_reviewed_user_input_stop(self) -> None:
        # PLAYER-TOOLBAR-TRANSPORT-BUTTONS-1: the Stop button seeks to cut in, then pauses (the closeEvent user-pause form).
        from tools.repo_hygiene.app_play_scan import REVIEWED_PLAY_STOPS
        self.assertEqual(REVIEWED_PLAY_STOPS["MainWindow::on_actionStop_triggered"], (1, 1, 0))

    def test_player_toolbar_transport_buttons_1_a_second_stop_in_the_stop_slot_fails_by_name(self) -> None:
        anchor = ("        m_newPosDropMode = cutInFrame;\n    }\n    if( ui->actionPlay->isChecked() )\n    {\n"
                  "        ui->actionPlay->setChecked( false );")
        problems = scan_app_play_sources(self._mutated(anchor, anchor + "\n        ui->actionPlay->setChecked( false );"))
        self.assertTrue(any("MainWindow::on_actionStop_triggered" in p and "(2, 1, 0)" in p for p in problems), problems)

    def test_enforce3_the_reviewed_stop_table_covers_every_stop_in_the_real_sources(self) -> None:
        from tools.repo_hygiene.app_play_scan import REVIEWED_PLAY_STOPS
        for function in ("MainWindow::MainWindow", "MainWindow::runGuiPlaybackSmoke",
                         "MainWindow::runHeadlessPlaybackProfile", "MainWindow::notePlaybackSmokePresentedFrame",
                         "MainWindow::playbackHandling", "MainWindow::programmaticPlay"):
            self.assertIn(function, REVIEWED_PLAY_STOPS)

    def test_fable_the_autoplay_hook_cannot_arm_in_a_smoke_or_profile_run(self) -> None:
        main = strip_cpp(self.sources[self.MAIN])[1]
        # the windows are built with argv = { appName } only, so the startup-file hook cannot arm ...
        self.assertEqual(len(re.findall(r"char \*smokeArgv\[\] = \{ appName\.data\(\), nullptr \};", main)), 1)
        self.assertEqual(len(re.findall(r"char \*profileArgv\[\] = \{ appName\.data\(\), nullptr \};", main)), 1)
        # ... and a launch that carries the variable is refused, typed, before any window exists
        self.assertIn("AUTOPLAY_REFUSED_IN_AUTOMATION", main)
        self.assertRegex(main, r'\(profile_playback \|\| gui_playback_smoke\)\s*&&\s*qEnvironmentVariableIntValue\("MLVAPP_AUTOPLAY_SECONDS"\) != 0')
        self.assertLess(main.index("AUTOPLAY_REFUSED_IN_AUTOMATION"), main.index("MyApplication a(argc, argv);"))


# HUB-VM-NO-LOCAL-PLAYBACK-1: this module runs the real launcher scripts against a stand-in exe that is never launched. The launchers
# refuse on the hub VM (tools/profiling/dual-venue/hub-hosts.json) unless the CI-fixture override is set, so set it for this module's
# own subprocesses only, in the test process (an agent command can never set it: hook rule NA-11 proposal).
_HUB_FIXTURE_ENV = "MLVAPP_HUB_PLAYBACK_CI_FIXTURE"
_hub_fixture_env_before = None


def setUpModule() -> None:
    global _hub_fixture_env_before
    _hub_fixture_env_before = os.environ.get(_HUB_FIXTURE_ENV)
    os.environ[_HUB_FIXTURE_ENV] = "CI_FIXTURE_ONLY"


def tearDownModule() -> None:
    if _hub_fixture_env_before is None:
        os.environ.pop(_HUB_FIXTURE_ENV, None)
    else:
        os.environ[_HUB_FIXTURE_ENV] = _hub_fixture_env_before


if __name__ == "__main__":
    unittest.main()
