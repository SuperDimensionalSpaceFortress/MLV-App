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
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.repo_hygiene.synthetic_mlv import (
    FRAMES_30S_AT_23976,
    FRAMES_SHORT_FIXTURE,
    MLV_EXTENSION,
    mlvi_header,
    write_synthetic_mlv,
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
        self.assertEqual(_gate(self.clip("f479", 479), 10)["verdict"], "CLIP_TOO_SHORT")
        self.assertEqual(_gate(self.clip("f480", 480), 10)["verdict"], "OK")

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
            "zero_fps": (mlvi_header(720, fps_denom=0), "bad_fps"),
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
        # Both parts: 400 + 400 = 800 frames = 33.37 s.
        write_synthetic_mlv(self.tmp / "span.M00", 400, file_num=1, file_count=2, guid=guid)
        complete = _gate(first, 24)
        self.assertEqual(complete["verdict"], "OK", complete)
        self.assertAlmostEqual(complete["clipSeconds"], 33.37, places=2)

    def test_a_spanned_set_from_two_recordings_is_unknown(self) -> None:
        first = write_synthetic_mlv(self.tmp / ("mix" + MLV_EXTENSION), 400, file_num=0, file_count=2, guid=1)
        write_synthetic_mlv(self.tmp / "mix.M00", 400, file_num=1, file_count=2, guid=2)
        verdict = _gate(first, 24)
        self.assertEqual(verdict["message"], "CLIP_LENGTH_UNKNOWN (reason=spanned_set_mismatch)")

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

    def test_a_thirty_second_clip_is_refused_for_a_longer_window(self) -> None:
        proc = self.run_runner(self.clip("thirty", FRAMES_30S_AT_23976), "-Seconds", "45")
        self.assertEqual(proc.returncode, EXIT_TOO_SHORT, proc.stdout + proc.stderr)

    def test_allowloop_is_refused_for_anything_that_could_be_playback_evidence(self) -> None:
        clip = self.clip("thirty", FRAMES_30S_AT_23976)
        for extra in (("-AllowLoop",), ("-AllowLoop", "-FrameTelemetry"), ("-AllowLoop", "-CaptureScreenshot")):
            with self.subTest(extra):
                proc = self.run_runner(clip, *extra)
                self.assertNotEqual(proc.returncode, 0, proc.stdout)
                self.assertIn("-AllowLoop is refused", proc.stdout + proc.stderr)

    def test_allowloop_with_noloop_is_a_contradiction(self) -> None:
        proc = self.run_runner(self.clip("thirty", FRAMES_30S_AT_23976), "-AllowLoop", "-NoLoop")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("contradict", proc.stdout + proc.stderr)

    def test_allowloop_is_honoured_only_for_a_launch_only_probe(self) -> None:
        # The one sanctioned use: a launch-only probe, which is never playback evidence.
        proc = self.run_runner(
            self.clip("sixteen", FRAMES_SHORT_FIXTURE), "-AllowLoop", "-LaunchOnlyProbe", "-AllowZeroPresentedFrames")
        arguments = self.dry_run_arguments(proc)
        self.assertIn("--loop", arguments)
        self.assertEqual(json.loads(proc.stdout)["clipLengthGate"], "SKIPPED_LAUNCH_ONLY_PROBE")


class RuntimeBackstopContractTests(unittest.TestCase):
    """3. The runtime backstop, pinned as text (MainWindow.cpp needs a full GUI build)."""

    def test_the_app_prints_wrapped_and_the_clip_length_on_summary_and_gate(self) -> None:
        text = MAIN_WINDOW.read_text(encoding="utf-8")
        self.assertIn("wrapped=%68 total_frames=%69 clip_seconds=%70", text)
        self.assertIn("wrapped=%7 total_frames=%8 clip_seconds=%9", text)

    def test_the_runner_turns_a_wrap_into_invalid_looped_and_never_a_pass(self) -> None:
        text = RUNNER.read_text(encoding="utf-8")
        self.assertIn('Get-ObjectPropertyValue $playbackSummary "wrapped"', text)
        self.assertIn("INVALID_LOOPED", text)
        self.assertIn("$scriptExitCode = if ($invalidLooped) { 43 } else { 2 }", text)
        # The failure is appended to $validationFailures, the list that decides the exit code.
        wrapped_at = text.index("INVALID_LOOPED: the playback timeline wrapped")
        self.assertIn("$validationFailures +=", text[wrapped_at - 80:wrapped_at])

    def test_loop_is_passed_only_under_an_explicit_allowloop(self) -> None:
        text = RUNNER.read_text(encoding="utf-8")
        self.assertIn('if ($AllowLoop) { $arguments += "--loop" }', text)
        self.assertNotIn("if (-not $NoLoop) { $arguments", text)
        self.assertEqual(text.count('"--loop"'), 1)


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
        self.assertRegex(text, r"\[ValidateRange\(20, 3600\)\]\s*\[int\]\$PlaySeconds")

    def test_a_fixture_id_is_refused_at_generation_with_the_shared_verdict(self) -> None:
        text = JOB_GENERATOR.read_text(encoding="utf-8")
        self.assertIn("gui-smoke-length-gate.ps1", text)
        self.assertIn("PLAYBACK_ATTR3_$($fixtureLengthGate.message)", text)


# ---- 5. the scan: no NEW launcher may play the app without the gate ---------------------------------

# Files under tools/ that may spell a playback launch WITHOUT being a launcher that needs the gate, or
# that launch through the gate by another route. Every entry carries its reason; adding one is a
# decision a reviewer reads, not a default. NONE of these may pass --loop.
PLAYBACK_LAUNCH_ALLOWLIST: dict[str, str] = {
    "tools/profiling/run-release-gui-smoke.ps1":
        "THE choke point: it dot-sources the gate and applies it before any launch (asserted below).",
    "tools/profiling/bachelor/AttrCudaArtifacts.psm1":
        "spells `--gui-smoke-playback --help` only (the feature probe text); it never launches a playback.",
}

GATE_CALL = re.compile(r"Test-GuiSmokeClipLength\b")
GATE_SOURCE = re.compile(r"gui-smoke-clip-length\.ps1")
# A playback LAUNCH: the smoke-playback verb used as an argument. `--gui-smoke-playback --help` is the
# feature probe (answered by the app without playing anything) and a comment mentions the word freely.
LAUNCH_TOKEN = re.compile(r"--gui-smoke-playback(?!\s+--help)")
LOOP_TOKEN = re.compile(r"""['"]--loop['"]""")
SCANNED_SUFFIXES = {".ps1", ".psm1", ".py", ".bat", ".cmd", ".sh", ".mjs", ".js"}


def _tool_files() -> list[Path]:
    files = []
    for path in sorted((ROOT / "tools").rglob("*")):
        if path.suffix.lower() not in SCANNED_SUFFIXES or not path.is_file():
            continue
        name = path.name.lower()
        # Tests and the test's own support code NAME these tokens as strings; they are not launchers.
        if name.startswith("test_") or name.startswith("test-") or name == "synthetic_mlv.py":
            continue
        files.append(path)
    return files


def _code_lines(path: Path) -> list[str]:
    lines = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = raw.strip()
        if stripped.startswith("#") or stripped.startswith("//"):
            continue
        lines.append(raw)
    return lines


class PlaybackLauncherScanTests(unittest.TestCase):
    def test_no_script_launches_playback_without_the_gate_or_an_allowlist_entry(self) -> None:
        offenders = []
        for path in _tool_files():
            rel = path.relative_to(ROOT).as_posix()
            code = "\n".join(_code_lines(path))
            if not LAUNCH_TOKEN.search(code):
                continue
            if rel in PLAYBACK_LAUNCH_ALLOWLIST:
                continue
            if GATE_SOURCE.search(code) and GATE_CALL.search(code):
                continue
            offenders.append(rel)
        self.assertEqual(
            offenders, [],
            "these tools launch `--gui-smoke-playback` without going through "
            "tools/profiling/gui-smoke-length-gate.ps1 (Test-GuiSmokeClipLength) or run-release-gui-smoke.ps1, "
            "and are not on PLAYBACK_LAUNCH_ALLOWLIST. A playback leg needs >= 20 s of footage and must "
            "never loop (owner rule 2026-09-30): route it through run-release-gui-smoke.ps1.",
        )

    def test_no_script_other_than_the_runner_ever_passes_loop(self) -> None:
        offenders = []
        for path in _tool_files():
            rel = path.relative_to(ROOT).as_posix()
            if rel == "tools/profiling/run-release-gui-smoke.ps1":
                continue
            if LOOP_TOKEN.search("\n".join(_code_lines(path))):
                offenders.append(rel)
        self.assertEqual(offenders, [], "a playback launcher passes --loop; only the runner's -AllowLoop may")

    def test_every_allowlist_entry_still_exists_and_names_a_reason(self) -> None:
        for rel, reason in PLAYBACK_LAUNCH_ALLOWLIST.items():
            self.assertTrue((ROOT / rel).is_file(), f"stale allowlist entry {rel}")
            self.assertGreater(len(reason), 20, rel)

    def test_the_runner_really_applies_the_gate_before_it_launches(self) -> None:
        text = RUNNER.read_text(encoding="utf-8")
        self.assertIn("gui-smoke-length-gate.ps1", text)
        self.assertLess(text.index("Test-GuiSmokeClipLength"), text.index('"--gui-smoke-playback",'))

    def test_the_direct_launchers_run_the_gate(self) -> None:
        for name in ("validate-visible-playback.ps1", "capture-reference-frame.ps1"):
            with self.subTest(name):
                code = "\n".join(_code_lines(PROFILING / name))
                self.assertTrue(GATE_SOURCE.search(code) and GATE_CALL.search(code), name)
                self.assertFalse(LOOP_TOKEN.search(code), f"{name} passes --loop")

    def test_the_scan_actually_detects_a_bare_launcher(self) -> None:
        # Guards the scan against rotting into a check that can never fail.
        bare = "& $exe --gui-smoke-playback --input $clip --seconds 30\n"
        self.assertTrue(LAUNCH_TOKEN.search(bare))
        self.assertFalse(LAUNCH_TOKEN.search("& $exe --gui-smoke-playback --help"))
        self.assertTrue(LOOP_TOKEN.search("$args += '--loop'"))

    def test_the_smoke_runner_closure_carries_the_gate(self) -> None:
        text = (PROFILING / "bachelor" / "AttrCudaArtifacts.psm1").read_text(encoding="utf-8")
        self.assertIn("'tools/profiling/gui-smoke-length-gate.ps1'", text)


if __name__ == "__main__":
    unittest.main()
