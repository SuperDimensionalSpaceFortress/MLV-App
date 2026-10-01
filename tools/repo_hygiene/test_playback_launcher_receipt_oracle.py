"""PLAYBACK-CLIP-LENGTH-ENFORCE-3 round 2 -- the CLASS test for "no tracked tool or app path can end an evidence Play,
or report a normal result, having consumed < 20 s of source footage without the result being INVALID".

Round 1 closed the app (it counts source frames and every automation stop waits for them) and the runner / job
(exit 43 / 29 on the summary). Round 2 found the launchers that start the app for an evidence Play themselves and
trusted something else: validate-visible-playback.ps1 killed the app on its own wall clock (SettleMs + Captures x
IntervalMs, ~14 s) and read nothing; capture-reference-frame.ps1 and run-release-playback-profile.ps1 trusted the exit
code alone, so a binary that predates ENFORCE-3 passed them after a wall-clock hold. This file pins:

  1. the receipt oracle functions (gui-smoke-length-gate.ps1) -- EXECUTED on the app's real summary format, a profile
     receipt and a log directory: a short count, a missing summary, a build that predates ENFORCE-3, a wrap, an fps
     override, a launcher kill and a non-zero exit are all INVALID (exit 43);
  2. each direct launcher, EXECUTED against a fake app that writes a chosen summary and exits with a chosen code, and
     MUTATION-TESTED (take the oracle out of the launcher and the scenario that proved it must change);
  3. the static scan: every script that can make the app play carries the oracle unless it is on an explicit,
     reasoned exemption list; a script that kills the app must pass the kill into the verdict; and the counter /
     requirement WRITE PINS and the settings factory (app_play_scan.py) go red on deliberately broken sources;
  4. parity between the C++ safety-budget constants and the PowerShell mirror, and the docs.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.repo_hygiene.app_play_scan import (
    PINNED_COUNTER_WRITES,
    PLAY_SOURCES,
    find_raw_app_settings,
    scan_app_play_sources,
)
from tools.repo_hygiene.synthetic_mlv import MLV_EXTENSION, write_synthetic_mlv
from tools.repo_hygiene.test_playback_clip_length_gate import (
    GATE,
    PLAYBACK_LAUNCH_ALLOWLIST,
    PROFILING,
    PWSH,
    ROOT,
    _launches_play,
    _pwsh,
    _q,
    _strip_comments,
    _summary_line,
    _tool_files,
    requires_pwsh,
)

CAPTURE = PROFILING / "capture-reference-frame.ps1"
PROFILE_WRAPPER = PROFILING / "run-release-playback-profile.ps1"
VISIBLE = PROFILING / "validate-visible-playback.ps1"
RUNNER = PROFILING / "run-release-gui-smoke.ps1"
OFFSCREEN = PROFILING / "test-app-play-gate-offscreen.ps1"
HEADER = ROOT / "platform" / "qt" / "PlaybackFrameRange.h"
DOC = ROOT / "docs" / "playback-clip-length-rule.md"

EXIT_INVALID = 43
requires_windows = unittest.skipUnless(os.name == "nt", "the filmstrip launcher drives Win32 window capture")

FORTY_SECONDS_AT_23976 = 960 + 24
GOOD_FIELDS = dict(wrapped=0, wrap_count=0, total_frames=FORTY_SECONDS_AT_23976, clip_seconds=41.04, presented_frames=900,
                   source_advanced=480, required_source_frames=480, native_fps=23.976, pace_fps=23.976, fps_override=0)
SOURCE_KEYS = ("source_advanced", "required_source_frames", "native_fps", "pace_fps", "fps_override", "source_start_frame")


def _good_line(**overrides: object) -> str:
    return _summary_line(**{**GOOD_FIELDS, **overrides})


def _pre_enforce_3_line() -> str:
    """A summary from a build that predates ENFORCE-3: everything else, none of the source-frame fields."""
    line = _good_line()
    for key in SOURCE_KEYS:
        line = re.sub(rf"\b{key}=\S+", "", line)
    return line


def _oracle(summary_line: str | None, *, exit_code: object = 0, killed: bool = False, message: str = "",
            gate: Path = GATE, window: float = 25.0) -> dict:
    """Runs Get-GuiSmokeEvidencePlayVerdict on a parsed summary line (None = the app wrote none)."""
    summary = "$null" if summary_line is None else f"(Convert-PlaybackLogLineToObject {_q(summary_line)})"
    code = "$null" if exit_code is None else str(int(exit_code))
    proc = _pwsh(
        f". {_q(gate)}; Get-GuiSmokeEvidencePlayVerdict -Summary {summary} -ExitCode {code} "
        f"-KilledByLauncher ${'true' if killed else 'false'} -WindowSeconds {window} -AppMessage {_q(message)} "
        "| Select-Object invalid, failures, exitCode | ConvertTo-Json -Compress -Depth 4")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout)


@requires_pwsh
class ReceiptOracleFunctionTests(unittest.TestCase):
    """1. The oracle, executed."""

    def test_a_run_that_consumed_its_source_frames_at_native_pace_is_valid(self) -> None:
        verdict = _oracle(_good_line())
        self.assertFalse(verdict["invalid"], verdict)

    def test_every_way_to_report_a_normal_result_on_less_footage_is_invalid_exit_43(self) -> None:
        cases = {
            "239 of 480 source frames (a ~10 s Play)": (_good_line(source_advanced=239), 0, False, "INVALID_SOURCE_FRAMES"),
            "no playback_smoke.summary at all": (None, 0, False, "INVALID_SOURCE_FRAMES"),
            "a build that predates ENFORCE-3": (_pre_enforce_3_line(), 0, False, "INVALID_SOURCE_FRAMES"),
            "required_source_frames unknown (0)": (_good_line(required_source_frames=0), 0, False, "INVALID_SOURCE_FRAMES"),
            "the timeline wrapped": (_good_line(wrapped=1, wrap_count=2), 0, False, "INVALID_LOOPED"),
            "a persisted fps override paced the run": (_good_line(fps_override=1), 0, False, "INVALID_SOURCE_FRAMES"),
            "the engine paced at half the clip's fps": (_good_line(pace_fps=11.988), 0, False, "INVALID_SOURCE_FRAMES"),
            "the launcher had to kill the app": (_good_line(), 0, True, "PLAY_SAFETY_TIMEOUT"),
            "the app never reported an exit code": (_good_line(), None, False, "PLAY_NOT_FINISHED"),
            "the app exited 14 (typed refusal)": (_good_line(), 14, False, "APP_EXIT_NONZERO"),
        }
        for name, (line, exit_code, killed, token) in cases.items():
            with self.subTest(name):
                verdict = _oracle(line, exit_code=exit_code, killed=killed)
                self.assertTrue(verdict["invalid"], verdict)
                self.assertEqual(verdict["exitCode"], EXIT_INVALID)
                self.assertIn(token, json.dumps(verdict["failures"]))

    def test_a_non_zero_exit_names_the_apps_typed_reason(self) -> None:
        verdict = _oracle(None, exit_code=14, message="[GUI-SMOKE] ERROR: SOURCE_FRAMES_SHORT (source_advanced=200)")
        self.assertIn("SOURCE_FRAMES_SHORT", json.dumps(verdict["failures"]))

    def test_the_verdict_decision_is_mutation_tested(self) -> None:
        source = GATE.read_text(encoding="utf-8")
        mutations = {
            "a launcher kill is forgiven": ("if ($KilledByLauncher) {", "if ($false) {"),
            "a missing exit code is forgiven": ("} elseif ($null -eq $ExitCode) {", "} elseif ($false) {"),
            "a non-zero exit is forgiven": ("} elseif ([int]$ExitCode -ne 0) {", "} elseif ($false) {"),
            "the receipt oracle is skipped": ("$failures += @($loop.failures)", "$null = @($loop.failures)"),
            "the verdict is never invalid": ("invalid = ($failures.Count -gt 0); failures = $failures; exitCode = 43",
                                             "invalid = $false; failures = $failures; exitCode = 43"),
        }
        probes = [(_good_line(), 0, True), (None, 0, False), (_pre_enforce_3_line(), 0, False),
                  (_good_line(source_advanced=1), 0, False), (_good_line(), 14, False), (_good_line(), None, False)]
        with tempfile.TemporaryDirectory(prefix="oracle-mut-") as tmp:
            for name, (old, new) in mutations.items():
                self.assertEqual(source.count(old), 1, f"mutation anchor missing: {name}")
                mutated = Path(tmp) / (re.sub(r"\W", "_", name) + ".ps1")
                mutated.write_text(source.replace(old, new), encoding="utf-8")
                with self.subTest(name):
                    differing = [probe for probe in probes
                                 if _oracle(probe[0], exit_code=probe[1], killed=probe[2], gate=mutated)["invalid"]
                                 != _oracle(probe[0], exit_code=probe[1], killed=probe[2])["invalid"]]
                    self.assertTrue(differing, f"mutation `{name}` survived: no probe changed its verdict")


@requires_pwsh
class ReceiptReadersTests(unittest.TestCase):
    """1b. The two receipt readers: the app log directory and the profile receipt."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="receipt-readers-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(os.path.realpath(self._tmp.name))

    def _summary_from_logs(self, since: str = "[datetime]::MinValue") -> dict | None:
        proc = _pwsh(f". {_q(GATE)}; $s = Read-GuiSmokePlaybackSummaryFromLogDir -LogDir {_q(self.tmp)} -SinceUtc ({since}); "
                     "if ($null -eq $s) { 'null' } else { $s | ConvertTo-Json -Compress }")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def test_the_last_summary_line_of_the_newest_log_is_read(self) -> None:
        (self.tmp / "mlvapp-a.log").write_text(
            "t1 playback_smoke.start session=1\nt2 " + _good_line(source_advanced=7) + "\nt3 playback_smoke.gpu_summary session=1 x=1\n"
            "t4 " + _good_line(source_advanced=480) + "\n", encoding="utf-8")
        summary = self._summary_from_logs()
        self.assertEqual(summary["source_advanced"], 480)
        self.assertEqual(summary["required_source_frames"], 480)

    def test_no_directory_no_log_and_no_summary_line_all_read_as_none(self) -> None:
        self.assertIsNone(self._summary_from_logs())
        (self.tmp / "mlvapp-a.log").write_text("t1 playback_smoke.start session=1\n", encoding="utf-8")
        self.assertIsNone(self._summary_from_logs())
        proc = _pwsh(f". {_q(GATE)}; $null -eq (Read-GuiSmokePlaybackSummaryFromLogDir -LogDir {_q(self.tmp / 'absent')})")
        self.assertEqual(proc.stdout.strip(), "True", proc.stdout + proc.stderr)

    def test_a_log_older_than_the_launch_is_not_this_runs_receipt(self) -> None:
        log = self.tmp / "mlvapp-old.log"
        log.write_text("t1 " + _good_line() + "\n", encoding="utf-8")
        os.utime(log, (1_250_000_000, 1_250_000_000))   # 2009: long before this run started
        self.assertIsNone(self._summary_from_logs(since="[datetime]::UtcNow"))
        self.assertIsNotNone(self._summary_from_logs())

    def _profile_summary(self, document: object) -> dict | None:
        path = self.tmp / "profile.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        proc = _pwsh(f". {_q(GATE)}; $s = Get-GuiSmokeProfileReceiptSummary -Path {_q(path)}; "
                     "if ($null -eq $s) { 'null' } else { $s | ConvertTo-Json -Compress }")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def test_the_profile_receipt_metadata_is_shaped_like_a_summary(self) -> None:
        summary = self._profile_summary({"metadata": {
            "source_advanced": 480, "required_source_frames": 480, "native_fps": 23.976, "pace_fps": 23.976,
            "fps_override_active": False, "play_wrap_count": 0, "programmatic_play_admitted": 1}})
        self.assertEqual(summary["source_advanced"], 480)
        self.assertEqual(summary["fps_override"], 0)
        self.assertEqual(summary["wrapped"], 0)          # derived from play_wrap_count; absent when that is absent
        self.assertEqual(summary["play_admitted"], 1)
        self.assertNotIn("play_performed", summary)       # ENFORCE-4: the reader never infers `not played`

    def test_a_missing_or_metadata_less_receipt_reads_as_none_and_is_invalid(self) -> None:
        self.assertIsNone(self._profile_summary({"frames": []}))
        proc = _pwsh(f". {_q(GATE)}; $s = Get-GuiSmokeProfileReceiptSummary -Path {_q(self.tmp / 'absent.json')}; "
                     "(Get-GuiSmokeEvidencePlayVerdict -Summary $s -ExitCode 0).invalid")
        self.assertEqual(proc.stdout.strip(), "True", proc.stdout + proc.stderr)

    def test_a_play_capable_profile_that_admitted_nothing_is_invalid_not_not_played(self) -> None:
        # ENFORCE-4: this wrapper only reads the receipt of a PLAY-CAPABLE profile, so nothing admitted is not "a profile
        # that never played" (the ENFORCE-3 reading, which let a master-era receipt through) -- it is INVALID.
        summary = self._profile_summary({"metadata": {"programmatic_play_admitted": 0, "required_source_frames": 0}})
        self.assertEqual(summary["play_admitted"], 0)
        path = self.tmp / "profile.json"
        proc = _pwsh(f". {_q(GATE)}; $s = Get-GuiSmokeProfileReceiptSummary -Path {_q(path)}; "
                     "(Get-GuiSmokeEvidencePlayVerdict -Summary $s -ExitCode 0 -RequireAdmission $true).invalid")
        self.assertEqual(proc.stdout.strip(), "True", proc.stdout + proc.stderr)

    def test_a_profile_receipt_that_predates_enforce_3_is_invalid_when_it_played(self) -> None:
        self._profile_summary({"metadata": {"programmatic_play_admitted": 1}})
        path = self.tmp / "profile.json"
        proc = _pwsh(f". {_q(GATE)}; $s = Get-GuiSmokeProfileReceiptSummary -Path {_q(path)}; "
                     "(Get-GuiSmokeEvidencePlayVerdict -Summary $s -ExitCode 0).invalid")
        self.assertEqual(proc.stdout.strip(), "True", proc.stdout + proc.stderr)


# ---- 2. each direct launcher, executed against a fake app ------------------------------------------------------------

def _make_fake_app(tmp: Path, *, exit_code: int, copies: tuple[tuple[Path, str], ...] = (), stderr_text: str = "") -> Path:
    """A stand-in for MLVApp.exe: copies the given files to the given destinations (shell expressions that may name the
    MLVAPP_CRASH_FORENSICS_LOG_DIR the launcher exports), optionally writes to stderr, exits with `exit_code`."""
    if os.name == "nt":
        path = tmp / "fake_app.cmd"
        lines = ["@echo off"]
        for source, destination in copies:
            lines.append(f'copy /y "{source}" "{destination}" >nul')
        if stderr_text:
            lines.append(f"echo {stderr_text} 1>&2")
        lines.append(f"exit /b {exit_code}")
        path.write_text("\r\n".join(lines) + "\r\n", encoding="ascii")
    else:
        path = tmp / "fake_app.sh"
        lines = ["#!/bin/sh"]
        for source, destination in copies:
            lines.append(f'cp "{source}" "{destination}"')
        if stderr_text:
            lines.append(f"echo {stderr_text} 1>&2")
        lines.append(f"exit {exit_code}")
        path.write_text("\n".join(lines) + "\n", encoding="ascii")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


LOG_DESTINATION = "%MLVAPP_CRASH_FORENSICS_LOG_DIR%\\mlvapp-fake.log" if os.name == "nt" else "$MLVAPP_CRASH_FORENSICS_LOG_DIR/mlvapp-fake.log"


class _LauncherCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="launcher-oracle-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(os.path.realpath(self._tmp.name))
        self.clip = write_synthetic_mlv(self.tmp / ("forty" + MLV_EXTENSION), FORTY_SECONDS_AT_23976)

    def log_template(self, line: str | None) -> tuple[tuple[Path, str], ...]:
        if line is None:
            return ()
        template = self.tmp / "app.log.template"
        template.write_text("t0 playback_smoke.start session=1\nt1 " + line + "\n", encoding="utf-8")
        return ((template, LOG_DESTINATION),)

    def scripts_dir(self, launcher: Path, text: str | None = None) -> Path:
        """A temp copy of the tools a launcher dot-sources / calls (so a MUTATED launcher can run), launcher last."""
        directory = self.tmp / "tools-copy"
        directory.mkdir(exist_ok=True)
        for name in ("gui-smoke-length-gate.ps1", "filmstrip-balance-trace.ps1", "capture-window-screenshot.ps1"):
            source = PROFILING / name
            if source.is_file():
                shutil.copy(source, directory / name)
        target = directory / launcher.name
        target.write_text(launcher.read_text(encoding="utf-8") if text is None else text, encoding="utf-8")
        return target


@requires_pwsh
class CaptureReferenceFrameLauncherTests(_LauncherCase):
    """capture-reference-frame.ps1 (a pinned-frame / timed capture): exit code AND receipt must prove the footage."""

    def run_capture(self, line: str | None, exit_code: int = 0, stderr_text: str = "", script: Path = CAPTURE) -> tuple[subprocess.CompletedProcess, Path]:
        out = self.tmp / "capture-out"
        out.mkdir(exist_ok=True)
        (out / "reference-frame.png").write_bytes(b"\0" * 65536)   # the fake app cannot draw: pre-seed the grab
        fake = _make_fake_app(self.tmp, exit_code=exit_code, copies=self.log_template(line), stderr_text=stderr_text)
        proc = subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
             "-Exe", str(fake), "-Clip", str(self.clip), "-OutDir", str(out), "-Seconds", "25", "-SettleMs", "100",
             "-Commit", "0123456789abcdef0123456789abcdef01234567"],
            capture_output=True, text=True, timeout=240)
        return proc, out

    def test_a_run_that_consumed_its_source_frames_is_a_candidate(self) -> None:
        proc, out = self.run_capture(_good_line())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("CAPTURE: CANDIDATE", proc.stdout)
        manifest = json.loads((out / "reference-frame-candidate.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(manifest["sourceFrames"]["verdict"], "VALID")
        self.assertEqual(manifest["sourceFrames"]["sourceAdvanced"], 480)
        self.assertEqual(manifest["appSettings"]["store"], "run_scoped")

    def test_exit_zero_is_not_enough_a_build_that_predates_enforce_3_is_invalid(self) -> None:
        proc, _ = self.run_capture(_pre_enforce_3_line())
        self.assertEqual(proc.returncode, EXIT_INVALID, proc.stdout + proc.stderr)
        self.assertIn("CAPTURE: INVALID", proc.stdout)
        self.assertIn("INVALID_SOURCE_FRAMES", proc.stdout)

    def test_a_short_count_a_missing_summary_a_wrap_and_an_override_are_each_invalid(self) -> None:
        for name, line in {"short count": _good_line(source_advanced=240), "no summary": None,
                           "wrap": _good_line(wrapped=1, wrap_count=1), "override": _good_line(fps_override=1)}.items():
            with self.subTest(name):
                proc, _ = self.run_capture(line)
                self.assertEqual(proc.returncode, EXIT_INVALID, proc.stdout + proc.stderr)

    def test_a_non_zero_app_exit_is_a_failure_with_the_typed_reason_never_a_candidate(self) -> None:
        proc, _ = self.run_capture(None, exit_code=14, stderr_text="SOURCE_FRAMES_SHORT")
        self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)
        self.assertIn("exe exited 14", proc.stdout)
        self.assertNotIn("CANDIDATE", proc.stdout)

    def test_taking_the_oracle_out_of_the_launcher_is_caught_by_the_scenarios(self) -> None:
        text = CAPTURE.read_text(encoding="utf-8")
        needle = "if ($captureVerdict.invalid) {"
        self.assertEqual(text.count(needle), 1)
        mutated = self.scripts_dir(CAPTURE, text.replace(needle, "if ($false) {"))
        proc, _ = self.run_capture(_pre_enforce_3_line(), script=mutated)
        self.assertNotEqual(proc.returncode, EXIT_INVALID, "the mutated launcher still refused the pre-ENFORCE-3 binary")
        self.assertEqual(proc.returncode, 0, "with the oracle gone the launcher reports a normal result: that is the bug")

    def test_the_kill_path_passes_the_kill_into_the_verdict(self) -> None:
        text = CAPTURE.read_text(encoding="utf-8")
        self.assertIn("-KilledByLauncher $captureKilled", text)
        self.assertRegex(text, r"WaitForExit\(\$captureBudgetMs\)\)\s*\{\s*\$captureKilled = \$true")


@requires_pwsh
class ProfileWrapperLauncherTests(_LauncherCase):
    """run-release-playback-profile.ps1 with a play-capable option: the profile receipt must prove the footage."""

    GOOD_METADATA = {"source_advanced": 480, "required_source_frames": 480, "native_fps": 23.976, "pace_fps": 23.976,
                     "fps_override_active": False, "play_wrap_count": 0, "programmatic_play_admitted": 1}

    def run_wrapper(self, metadata: dict | None, exit_code: int = 0, option: str = "--exercise-play-action",
                    script: Path = PROFILE_WRAPPER) -> subprocess.CompletedProcess:
        bin_dir = self.tmp / "bin"
        (bin_dir / "platforms").mkdir(parents=True, exist_ok=True)
        (bin_dir / "platforms" / "qwindows.dll").write_bytes(b"stand-in")
        output = self.tmp / "profile.json"
        if output.exists():
            output.unlink()
        copies: tuple[tuple[Path, str], ...] = ()
        if metadata is not None:
            template = self.tmp / "profile.template.json"
            template.write_text(json.dumps({"metadata": metadata}), encoding="utf-8")
            copies = ((template, str(output)),)
        fake = _make_fake_app(self.tmp, exit_code=exit_code, copies=copies)
        target = bin_dir / fake.name
        shutil.copy(fake, target)
        command = (f"& {_q(script)} -RepoRoot {_q(ROOT)} -ExePath {_q(target)} -Input {_q(self.clip)} "
                   f"-Output {_q(output)} -Frames 3 -AdditionalArgs @({_q(option)}); exit $LASTEXITCODE")
        return subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
                              capture_output=True, text=True, timeout=240)

    def test_a_profile_that_consumed_its_source_frames_passes(self) -> None:
        proc = self.run_wrapper(self.GOOD_METADATA)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_exit_zero_is_not_enough_a_receipt_without_source_frames_is_invalid(self) -> None:
        for name, metadata in {
            "predates ENFORCE-3": {"programmatic_play_admitted": 1},
            "short count": {**self.GOOD_METADATA, "source_advanced": 240},
            "fps override": {**self.GOOD_METADATA, "fps_override_active": True},
            "wrap": {**self.GOOD_METADATA, "play_wrap_count": 3},
            "no receipt written": None,
        }.items():
            with self.subTest(name):
                proc = self.run_wrapper(metadata)
                self.assertEqual(proc.returncode, EXIT_INVALID, proc.stdout + proc.stderr)
                self.assertIn("INVALID", proc.stderr)

    def test_the_apps_own_typed_failure_code_passes_through(self) -> None:
        self.assertEqual(self.run_wrapper(None, exit_code=14).returncode, 14)

    def test_a_decode_only_profile_is_not_playback_and_needs_no_receipt(self) -> None:
        proc = self.run_wrapper(None, option="--threads")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_taking_the_oracle_out_of_the_wrapper_is_caught_by_the_scenarios(self) -> None:
        text = PROFILE_WRAPPER.read_text(encoding="utf-8")
        needle = "if ($profileVerdict.invalid) {"
        self.assertEqual(text.count(needle), 1)
        mutated = self.scripts_dir(PROFILE_WRAPPER, text.replace(needle, "if ($false) {"))
        proc = self.run_wrapper({"programmatic_play_admitted": 1}, script=mutated)
        self.assertEqual(proc.returncode, 0, "with the oracle gone the wrapper reports a normal result: that is the bug")

    def test_the_kill_path_passes_the_kill_into_the_verdict(self) -> None:
        text = PROFILE_WRAPPER.read_text(encoding="utf-8")
        self.assertIn("-KilledByLauncher $profileKilled", text)


@requires_pwsh
@requires_windows
class VisiblePlaybackLauncherTests(_LauncherCase):
    """validate-visible-playback.ps1 (the fable BLOCKER): it used to kill the app on its own wall clock and read nothing."""

    def run_visible(self, line: str | None, exit_code: int = 0, script: Path = VISIBLE) -> subprocess.CompletedProcess:
        out = self.tmp / "visible-out"
        fake = _make_fake_app(self.tmp, exit_code=exit_code, copies=self.log_template(line))
        return subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
             "-ExePath", str(fake), "-ClipPath", str(self.clip), "-OutDir", str(out), "-Captures", "0",
             "-SettleMs", "100", "-Seconds", "25"],
            capture_output=True, text=True, timeout=300)

    def test_a_play_the_app_finished_having_consumed_its_frames_is_valid(self) -> None:
        proc = self.run_visible(_good_line())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("playbackVerdict", proc.stdout)
        self.assertIn("VALID", proc.stdout)

    def test_exit_zero_is_not_enough_a_short_count_or_an_old_build_is_invalid(self) -> None:
        for name, line in {"short count (a ~10 s Play)": _good_line(source_advanced=240), "predates ENFORCE-3": _pre_enforce_3_line(),
                           "no summary": None}.items():
            with self.subTest(name):
                proc = self.run_visible(line)
                self.assertEqual(proc.returncode, EXIT_INVALID, proc.stdout + proc.stderr)
                self.assertIn("INVALID", proc.stderr)

    def test_taking_the_oracle_out_of_the_launcher_is_caught_by_the_scenarios(self) -> None:
        text = VISIBLE.read_text(encoding="utf-8")
        needle = "\nif ($playbackVerdict.invalid) {"
        self.assertEqual(text.count(needle), 1)
        mutated = self.scripts_dir(VISIBLE, text.replace(needle, "\nif ($false) {"))
        proc = self.run_visible(_good_line(source_advanced=240), script=mutated)
        self.assertEqual(proc.returncode, 0, "with the oracle gone the launcher reports a normal result: that is the bug")


class VisiblePlaybackLauncherStaticTests(unittest.TestCase):
    """The blocker's own anatomy, pinned as text (these run on every OS)."""

    TEXT = VISIBLE.read_text(encoding="utf-8")

    def test_the_script_no_longer_ends_the_play_on_a_clock_of_its_own(self) -> None:
        code = _strip_comments(self.TEXT)
        # The only kill is the safety-net branch after the app had its whole budget; it is passed into the verdict.
        kills = re.findall(r"\.Kill\(", code)
        self.assertEqual(len(kills), 2, "one Kill($true) with a plain Kill() fallback, in the `finally` safety net")
        self.assertIn("$killedByLauncher = $true", code)
        self.assertIn("-KilledByLauncher $killedByLauncher", code)
        # the capture loop is followed by a wait for the app's OWN exit, never by a kill
        loop_end = code.index('Write-Host "[live-filmstrip] captured $captured frames')
        wait_at = code.index("$proc.WaitForExit($remainingMs)")
        kill_at = code.index("$proc.Kill($true)")
        self.assertLess(loop_end, wait_at)
        self.assertLess(wait_at, kill_at)

    def test_the_process_budget_is_the_apps_safety_net_not_the_capture_schedule(self) -> None:
        code = _strip_comments(self.TEXT)
        self.assertIn("(Get-GuiSmokePlaySafetyMs -Seconds $Seconds)", code)
        self.assertNotRegex(code, r"Start-Sleep -Milliseconds \(\$SettleMs \+ \$Captures")

    def test_the_verdict_is_acted_on_with_exit_43(self) -> None:
        self.assertRegex(_strip_comments(self.TEXT),
                         r"if \(\$playbackVerdict\.invalid\) \{[\s\S]*?exit \$playbackVerdict\.exitCode")

    def test_the_app_log_dir_the_verdict_reads_is_the_one_the_app_writes_to(self) -> None:
        code = _strip_comments(self.TEXT)
        self.assertIn('$psi.EnvironmentVariables["MLVAPP_CRASH_FORENSICS_LOG_DIR"] = $logRoot', code)
        self.assertIn("Read-GuiSmokePlaybackSummaryFromLogDir -LogDir $logRoot", code)


# ---- 3. the static scan: every launcher carries the oracle; the write pins; the settings factory --------------------

# Scripts that can make the app play (the PLAY_TOKENS / DIRECT_LAUNCH scan of test_playback_clip_length_gate.py) and
# yet do NOT need the receipt oracle, each with the reason a reviewer reads. A NEW launcher is not on this list, so it
# fails until it carries the oracle or a reviewer adds it here.
RECEIPT_ORACLE_EXEMPT: dict[str, str] = {
    "tools/profiling/gui-smoke-length-gate.ps1":
        "the gate and the oracle themselves: they name every token to refuse it and never launch the app.",
    "tools/profiling/bachelor/AttrCudaArtifacts.psm1":
        "spells the feature-probe text only; the attribution job's own source-frame oracle lives in this module.",
    "tools/profiling/run-local-gpu-capability.ps1": "decode-only benchmark: a FIXED argument list, no Play action.",
    "tools/profiling/run-ultra-magnus-profile.ps1": "decode-only benchmark: a FIXED argument list, no Play action.",
    "tools/profiling/ssh-gpu-probe.ps1": "decode-only probe: a FIXED argument list, no Play action.",
    "tools/profiling/test-app-play-gate-offscreen.ps1":
        "refusal-only proof on the tracked short fixtures: every entry must be REFUSED before Play, so nothing is played.",
    "tools/profiling/run-release-cdng-export-profile.ps1":
        "export launcher: -Context 'launcher' refuses every play mode before it launches (asserted in test_playback_clip_length_gate).",
    "tools/profiling/run-release-cuda-dng-export.ps1":
        "export launcher: -Context 'launcher' refuses every play mode before it launches (asserted in test_playback_clip_length_gate).",
    "tools/profiling/start-release-cuda-playback.ps1":
        "interactive launcher: -Context 'launcher' refuses every play mode; a HUMAN pressing Play is outside the class.",
}
# Launchers that start the app for an evidence Play themselves and so MUST carry the oracle (named, so dropping one
# from the derived scan cannot pass silently).
RECEIPT_ORACLE_REQUIRED = {
    "tools/profiling/run-release-gui-smoke.ps1",
    "tools/profiling/run-release-playback-profile.ps1",
    "tools/profiling/capture-reference-frame.ps1",
    "tools/profiling/validate-visible-playback.ps1",
}
# The oracle must be ACTED ON: its result is assigned, and `.invalid` of that variable is tested before an exit /
# the runner's validation failures.
ORACLE_ACTED_ON = re.compile(
    r"\$(\w+)\s*=\s*(?:Get-GuiSmokeEvidencePlayVerdict|Get-GuiSmokeLoopVerdict)\b[\s\S]{0,4000}?\$\1\.invalid\b"
    r"[\s\S]{0,600}?\b(?:exit|validationFailures)\b")
KILL_CALL = re.compile(r"\.Kill\s*\(|Stop-Process\b")


def find_launchers_without_receipt_oracle(files: dict[str, str], exempt: dict[str, str] | None = None) -> list[str]:
    """rel path -> source text. A script that can make the app play, is not exempt, and does not act on the receipt
    oracle -- or that kills the app without passing the kill into the verdict -- is an offender."""
    exempt = RECEIPT_ORACLE_EXEMPT if exempt is None else exempt
    offenders = []
    for rel, text in sorted(files.items()):
        code = _strip_comments(text)
        if rel in exempt or not _launches_play(code):
            continue
        if not ORACLE_ACTED_ON.search(code):
            offenders.append(f"{rel}: starts the app for a Play but does not act on the receipt oracle")
        elif rel != "tools/profiling/run-release-gui-smoke.ps1" and KILL_CALL.search(code) and "-KilledByLauncher" not in code:
            offenders.append(f"{rel}: kills the app without passing the kill into the verdict (-KilledByLauncher)")
    return offenders


def _tools_by_path() -> dict[str, str]:
    return {p.relative_to(ROOT).as_posix(): p.read_text(encoding="utf-8", errors="replace") for p in _tool_files()}


class LauncherReceiptOracleScanTests(unittest.TestCase):
    """3a. Every launcher that starts the app for an evidence Play carries the oracle."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.files = _tools_by_path()

    def test_every_launcher_carries_the_receipt_oracle_or_a_reasoned_exemption(self) -> None:
        self.assertEqual(find_launchers_without_receipt_oracle(self.files), [])

    def test_the_named_launchers_are_found_by_the_scan_and_carry_it(self) -> None:
        derived = {rel for rel, text in self.files.items() if _launches_play(_strip_comments(text))}
        self.assertTrue(RECEIPT_ORACLE_REQUIRED <= derived, sorted(RECEIPT_ORACLE_REQUIRED - derived))
        self.assertEqual(sorted(derived - set(RECEIPT_ORACLE_EXEMPT) - RECEIPT_ORACLE_REQUIRED), [],
                         "a launcher the scan sees is neither exempt (with a reason) nor required to carry the oracle")
        for rel in sorted(RECEIPT_ORACLE_REQUIRED):
            with self.subTest(rel):
                self.assertRegex(_strip_comments(self.files[rel]), ORACLE_ACTED_ON)

    def test_the_exemptions_are_real_scripts_with_reasons_and_overlap_nothing_required(self) -> None:
        for rel, reason in RECEIPT_ORACLE_EXEMPT.items():
            self.assertTrue((ROOT / rel).is_file(), f"stale exemption {rel}")
            self.assertGreater(len(reason), 30, rel)
        self.assertFalse(set(RECEIPT_ORACLE_EXEMPT) & RECEIPT_ORACLE_REQUIRED)
        # the exemptions that are not refusal / decode-only / forwarding must be in the launch allowlist's spirit too
        for rel in ("tools/profiling/test-app-play-gate-offscreen.ps1", "tools/profiling/run-local-gpu-capability.ps1"):
            self.assertIn(rel, PLAYBACK_LAUNCH_ALLOWLIST)

    def test_mutation_a_launcher_with_the_oracle_removed_is_caught(self) -> None:
        for rel in sorted(RECEIPT_ORACLE_REQUIRED - {"tools/profiling/run-release-gui-smoke.ps1"}):
            with self.subTest(rel):
                text = self.files[rel]
                mutated = re.sub(r"Get-GuiSmokeEvidencePlayVerdict", "Get-NothingAtAll", text)
                self.assertNotEqual(mutated, text)
                self.assertTrue(find_launchers_without_receipt_oracle({rel: mutated}))

    def test_mutation_the_runner_without_its_loop_verdict_is_caught(self) -> None:
        rel = "tools/profiling/run-release-gui-smoke.ps1"
        mutated = self.files[rel].replace("$loopVerdict = Get-GuiSmokeLoopVerdict", "$loopVerdict = Get-NothingAtAll")
        self.assertNotEqual(mutated, self.files[rel])
        self.assertTrue(find_launchers_without_receipt_oracle({rel: mutated}))

    def test_mutation_a_verdict_that_is_computed_but_never_acted_on_is_caught(self) -> None:
        rel = "tools/profiling/capture-reference-frame.ps1"
        mutated = self.files[rel].replace("if ($captureVerdict.invalid) {", "if ($captureVerdict.somethingElse) {")
        self.assertNotEqual(mutated, self.files[rel])
        self.assertTrue(find_launchers_without_receipt_oracle({rel: mutated}))

    def test_mutation_a_kill_that_is_not_passed_into_the_verdict_is_caught(self) -> None:
        rel = "tools/profiling/validate-visible-playback.ps1"
        mutated = self.files[rel].replace("-KilledByLauncher $killedByLauncher", "")
        self.assertNotEqual(mutated, self.files[rel])
        self.assertTrue(any("kills the app" in problem for problem in find_launchers_without_receipt_oracle({rel: mutated})))

    def test_mutation_a_brand_new_launcher_with_no_oracle_is_caught(self) -> None:
        fresh = {"tools/profiling/brand-new-playback.ps1": "& $exe --gui-smoke-playback --input $x --seconds 25"}
        self.assertEqual(len(find_launchers_without_receipt_oracle(fresh)), 1)
        # ... and a comment that names the oracle does not count
        sneaky = {"tools/profiling/brand-new-playback.ps1":
                  "# Get-GuiSmokeEvidencePlayVerdict $v.invalid exit\n& $exe --gui-smoke-playback --input $x --seconds 25"}
        self.assertEqual(len(find_launchers_without_receipt_oracle(sneaky)), 1)


def _load_sources() -> dict[str, str]:
    return {path: (ROOT / path).read_text(encoding="utf-8") for path in PLAY_SOURCES}


class CounterWritePinTests(unittest.TestCase):
    """3b. m_sourceAdvance / m_playRequiredSourceFrames are written ONLY at the pinned sites (fable H2)."""

    MAIN_WINDOW = "platform/qt/MainWindow.cpp"
    MAIN = "platform/qt/main.cpp"

    @classmethod
    def setUpClass(cls) -> None:
        cls.sources = _load_sources()

    def mutated(self, old: str, new: str, path: str = MAIN_WINDOW) -> dict[str, str]:
        text = self.sources[path]
        self.assertIn(old, text, "mutation anchor missing: update the test with the source")
        sources = dict(self.sources)
        sources[path] = text.replace(old, new, 1)
        return sources

    def inject_in_smoke(self, statement: str) -> dict[str, str]:
        anchor = 'forceLoopOffForAutomation( "gui-smoke-measured" );'
        return self.mutated(anchor, anchor + "\n    " + statement)

    def assert_caught(self, sources: dict[str, str], needle: str) -> None:
        problems = scan_app_play_sources(sources)
        self.assertTrue(any(needle in p for p in problems), problems)

    def test_the_real_sources_are_clean(self) -> None:
        self.assertEqual(scan_app_play_sources(self.sources), [])

    def test_the_pins_name_the_reviewed_functions(self) -> None:
        self.assertEqual(sorted(PINNED_COUNTER_WRITES), [
            "MainWindow::on_actionPlay_toggled", "MainWindow::playbackHandling", "MainWindow::programmaticPlay"])

    def test_fables_two_repros_are_caught(self) -> None:
        self.assert_caught(self.inject_in_smoke("m_playRequiredSourceFrames = 1;"), "m_playRequiredSourceFrames written")
        self.assert_caught(self.inject_in_smoke("m_sourceAdvance.noteEngineTick( 0, 100000, false );"), "source-frame counter")

    def test_every_other_way_to_write_or_leak_the_counter_is_caught(self) -> None:
        injections = {
            "a counter reset outside the gate": ("m_sourceAdvance = playback_frame_range::SourceFrameAdvanceCounter();", "source-frame counter"),
            "a member poked directly": ("m_sourceAdvance.forwardSteps += 500;", "unreviewed use of m_sourceAdvance"),
            "the high-water mark moved": ("m_sourceAdvance.highWater = 100000;", "unreviewed use of m_sourceAdvance"),
            "a begin() outside the pinned handler": ("m_sourceAdvance.begin( 0 );", "source-frame counter"),
            "an alias that can write": ("auto &adv = m_sourceAdvance; adv.forwardSteps = 9999;", "m_sourceAdvance"),
            "a pointer that can write": ("auto *adv = &m_sourceAdvance;", "escapes"),
            "a swap": ("std::swap( m_sourceAdvance, m_sourceAdvance );", "m_sourceAdvance"),
            "the requirement incremented": ("++m_playRequiredSourceFrames;", "escapes"),
            "the requirement compound-assigned": ("m_playRequiredSourceFrames -= 400;", "m_playRequiredSourceFrames written"),
            "the requirement assigned a literal": ("m_playRequiredSourceFrames = 480;", "m_playRequiredSourceFrames written"),
            "the requirement exposed by reference": ("auto &req = m_playRequiredSourceFrames; req = 1;", "m_playRequiredSourceFrames"),
            "a requirement swap": ("std::swap( m_playRequiredSourceFrames, m_playRequiredSourceFrames );", "m_playRequiredSourceFrames"),
        }
        for name, (statement, needle) in injections.items():
            with self.subTest(name):
                self.assert_caught(self.inject_in_smoke(statement), needle)

    def test_a_removed_or_extra_pinned_write_is_caught(self) -> None:
        self.assert_caught(self.mutated("m_sourceAdvance.begin( ui->horizontalSliderPosition->value() );\n        // CUDA-PERF",
                                        "// removed\n        // CUDA-PERF"), "on_actionPlay_toggled")
        self.assert_caught(self.mutated("m_sourceAdvance.noteEngineTick( sourcePositionBeforeTick, ui->horizontalSliderPosition->value(), false );",
                                        "m_sourceAdvance.noteEngineTick( sourcePositionBeforeTick, ui->horizontalSliderPosition->value(), false );\n"
                                        "                    m_sourceAdvance.noteEngineTick( 0, 9999, false );"), "playbackHandling")

    def test_the_already_running_play_is_measured_from_now_and_that_is_pinned(self) -> None:
        old = "        m_sourceAdvance = playback_frame_range::SourceFrameAdvanceCounter();\n        m_sourceAdvance.begin( ui->horizontalSliderPosition->value() );\n"
        self.assert_caught(self.mutated(old, ""), "programmaticPlay")

    def test_the_process_pins_in_main_cpp_are_caught_when_broken(self) -> None:
        main = self.sources[self.MAIN]
        broken = {
            "the settings store is no longer isolated": ("automation_settings::isolate(", "automation_settings_isolate_removed("),
            "the verdict is no longer the exit code": ("return guiExitCode != 0 ? guiExitCode : w.automationVerdictExitCode();", "return guiExitCode;"),
            "autoplay no longer counts as an automation run": ("|| qEnvironmentVariableIntValue(\"MLVAPP_AUTOPLAY_SECONDS\") > 0;", ";"),
        }
        for name, (old, new) in broken.items():
            with self.subTest(name):
                self.assertIn(old, main)
                self.assert_caught(self.mutated(old, new, self.MAIN), "main.cpp")
        # the store must be opened BEFORE the first QSettings (CrashForensics::install)
        isolate_block_start = main.index("    const bool automationRun")
        install = "    CrashForensics::install(argc, argv);\n"
        moved = main.replace(install, "").replace("    const bool automationRun", install + "    const bool automationRun", 1)
        self.assertNotEqual(moved, main)
        self.assertLess(isolate_block_start, main.index(install))
        sources = dict(self.sources)
        sources[self.MAIN] = moved
        self.assert_caught(sources, "BEFORE CrashForensics::install")

    def test_every_stop_and_wait_pin_added_in_round_2_is_mutation_tested(self) -> None:
        cases = {
            "the settle may ask for a second Play again":
                ("&& playback_frame_range::lookAssistSettleNeedsOwnPlay( m_programmaticPlayLedger.admitted ) )", ")"),
            "the smoke loop no longer breaks on a too-slow pace":
                ("         || measuredState == playback_frame_range::PlayStopState::PaceTooSlow\n", ""),
            "the autoplay verdict is no longer resolved":
                ("m_automationVerdict.resolve( autoplayState );", ""),
            "the autoplay refusal is no longer latched":
                ("m_automationVerdict.fail();\n", ""),
            "the autoplay verdict is no longer armed":
                ("m_automationVerdict.armPending();", ""),
        }
        for name, (old, new) in cases.items():
            with self.subTest(name):
                self.assert_caught(self.mutated(old, new), "")


class SettingsFactoryScanTests(unittest.TestCase):
    """3c. Every app QSettings opens through automation_settings::openAppSettings() (fable H5 / sol H2)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.files = {p.relative_to(ROOT).as_posix(): p.read_text(encoding="utf-8", errors="replace")
                     for p in sorted((ROOT / "platform" / "qt").iterdir()) if p.suffix in (".cpp", ".h")}

    def test_no_raw_app_settings_construction_remains(self) -> None:
        self.assertEqual(find_raw_app_settings(self.files), [])

    def test_the_factory_exists_and_is_where_the_files_look_for_it(self) -> None:
        self.assertIn("openAppSettings", self.files["platform/qt/AutomationSettings.h"])
        for name in ("MainWindow.cpp", "ExportSettingsDialog.cpp", "SingleFrameExportDialog.cpp", "TranscodeDialog.cpp",
                     "CrashForensics.cpp", "PlaybackQualityPolicy.h"):
            self.assertIn("automation_settings::openAppSettings()", self.files["platform/qt/" + name], name)

    def test_mutation_a_raw_construction_is_caught_in_any_spelling(self) -> None:
        for statement in ('QSettings set( QSettings::UserScope, "magiclantern.MLVApp", "MLVApp" );',
                          'QSettings set(QSettings::UserScope,\n  QStringLiteral("a"), QStringLiteral("b"));',
                          'auto v = QSettings( QSettings::SystemScope, "a", "b" ).value( "k" );'):
            with self.subTest(statement):
                mutated = dict(self.files)
                mutated["platform/qt/Extra.cpp"] = "void f() {\n" + statement + "\n}\n"
                self.assertEqual(len(find_raw_app_settings(mutated)), 1)
        # a mention in a comment or a string is not a construction
        mutated = dict(self.files)
        mutated["platform/qt/Extra.cpp"] = '// QSettings set( QSettings::UserScope, "a", "b" );\nconst char *s = "QSettings( QSettings::UserScope";\n'
        self.assertEqual(find_raw_app_settings(mutated), [])


class OffscreenProbeAndParityTests(unittest.TestCase):
    """4. The offscreen persisted-override probe never touches the user's settings; constants agree; docs say it."""

    def test_the_offscreen_probe_writes_only_into_the_run_scoped_store(self) -> None:
        code = _strip_comments(OFFSCREEN.read_text(encoding="utf-8"))
        self.assertNotRegex(code, r"\b(?:Set|New|Remove|Clear)-ItemProperty\b")
        self.assertNotRegex(code, r"\b(?:Set|New|Remove)-Item\b[^\n]*HKCU")
        self.assertIn("MLVAPP_AUTOMATION_SETTINGS_DIR", code)
        self.assertIn("settings_store=run_scoped", code)
        self.assertIn("real-settings-untouched", code)

    def test_the_powershell_safety_budget_mirrors_the_cpp_constants(self) -> None:
        header = HEADER.read_text(encoding="utf-8")
        fraction = float(re.search(r"constexpr double kMinSustainedPaceFraction = ([0-9.]+);", header).group(1))
        margin = int(re.search(r"constexpr int kPlaySafetyMarginMs = (\d+);", header).group(1))
        gate = GATE.read_text(encoding="utf-8")
        self.assertIn(f"$script:GuiSmokeMinSustainedPaceFraction = {fraction}", gate)
        self.assertIn(f"$script:GuiSmokePlaySafetyMarginMs = {margin}", gate)

    @requires_pwsh
    def test_the_powershell_safety_budget_is_the_formula_the_app_uses(self) -> None:
        for seconds, expected in ((20, 55000), (24, 63000), (40, 95000)):
            proc = _pwsh(f". {_q(GATE)}; Get-GuiSmokePlaySafetyMs -Seconds {seconds}")
            self.assertEqual(int(proc.stdout.strip()), expected, proc.stdout + proc.stderr)

    def test_the_runners_process_budget_starts_from_the_apps_safety_net(self) -> None:
        text = RUNNER.read_text(encoding="utf-8")
        self.assertIn("(Get-GuiSmokePlaySafetyMs -Seconds $Seconds) +", text)

    def test_the_rule_doc_documents_the_source_frame_class_its_tokens_and_the_launchers(self) -> None:
        doc = DOC.read_text(encoding="utf-8")
        for token in ("source frames", "source_advanced", "required_source_frames", "PLAY_PACE_TOO_SLOW",
                      "SOURCE_FRAMES_SHORT", "PLAY_SAFETY_TIMEOUT", "REPLAY_REFUSED", "INVALID_SOURCE_FRAMES",
                      "exit 43", "exit 29", "exit 14", "SETTINGS_ISOLATION_FAILED", "MLVAPP_AUTOMATION_SETTINGS_DIR",
                      "AutomationSettings.h", "Get-GuiSmokeEvidencePlayVerdict", "validate-visible-playback.ps1",
                      "capture-reference-frame.ps1", "run-release-playback-profile.ps1", "run-release-gui-smoke.ps1",
                      "kMinSustainedPaceFraction", "PLAY_NOT_FINISHED"):
            self.assertIn(token, doc)
        for rel in RECEIPT_ORACLE_REQUIRED:
            self.assertIn(Path(rel).name, doc)


if __name__ == "__main__":
    unittest.main()
