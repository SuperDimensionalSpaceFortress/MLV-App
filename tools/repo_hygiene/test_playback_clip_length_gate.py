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


    # ---- ENFORCE-2: the PLAY WINDOW of an evidence run is >= 20 s too -------------------------------------

    def test_a_play_window_under_20_seconds_is_refused_even_on_a_long_clip(self) -> None:
        thirty = self.clip("thirty", FRAMES_30S_AT_23976)
        verdict = _gate(thirty, 10)
        self.assertEqual(verdict["verdict"], "PLAY_WINDOW_TOO_SHORT", verdict)
        self.assertEqual(verdict["message"], "PLAY_WINDOW_TOO_SHORT (window=10 required=20)")
        self.assertEqual(_gate(thirty, 19.5)["verdict"], "PLAY_WINDOW_TOO_SHORT")
        self.assertEqual(_gate(thirty, 20)["verdict"], "OK")
        self.assertEqual(_gate(thirty, 24)["verdict"], "OK")

    def test_clip_only_is_the_explicit_opt_out_for_callers_that_play_nothing(self) -> None:
        def gate(path: Path, extra: str) -> dict:
            proc = _pwsh(f". {_q(GATE)}; Test-GuiSmokeClipLength -Path {_q(path)} -WindowSeconds 0 {extra} | ConvertTo-Json -Compress")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            return json.loads(proc.stdout)
        thirty = self.clip("thirty", FRAMES_30S_AT_23976)
        self.assertEqual(gate(thirty, "")["verdict"], "PLAY_WINDOW_TOO_SHORT")     # window 0 is a window under the floor
        self.assertEqual(gate(thirty, "-ClipOnly")["verdict"], "OK")
        self.assertEqual(gate(self.clip("sixteen", FRAMES_SHORT_FIXTURE), "-ClipOnly")["verdict"], "CLIP_TOO_SHORT")

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


def _summary_line(**values: object) -> str:
    """The app's REAL playback_smoke.summary format (read from MainWindow.cpp, not retyped here) with
    every placeholder filled: `values` by key name, everything else 0."""
    template = _literal_template(MAIN_WINDOW.read_text(encoding="utf-8"), "playback_smoke.summary session=%1")
    keys = dict(re.findall(r"(\w+)=%(\d+)", template))
    by_index = {int(index): str(values.get(key, 0)) for key, index in keys.items()}
    return re.sub(r"%(\d+)", lambda m: by_index.get(int(m.group(1)), "0"), template)


def _loop_verdict(line: str, window: float, launch_only: bool = False, gate: Path = GATE, clip_frames: int = 0) -> dict:
    """Runs the shared parser + Get-GuiSmokeLoopVerdict (the code the runner calls) on a summary line."""
    proc = _pwsh(
        f". {_q(gate)}; $s = Convert-PlaybackLogLineToObject {_q(line)}; "
        f"Get-GuiSmokeLoopVerdict -Summary $s -WindowSeconds {window} "
        f"-LaunchOnlyProbe ${'true' if launch_only else 'false'} -ClipFrames {clip_frames} "
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


def _case(row):
    fields, window, launch_only, expected, *rest = row
    return fields, window, launch_only, expected, (rest[0] if rest else 0)


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

    def test_a_summary_without_the_fields_is_not_a_wrap(self) -> None:
        # A binary that predates the fields: the pre-launch gate is its control, and the runner's
        # existing synthetic-log contract tests feed summaries without them.
        self.assertFalse(_loop_verdict("playback_smoke.summary session=1 presented_frames=900", 24)["invalid"])

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

    def _run_application_block(self, block: str, **summary_fields: object) -> dict:
        proc = _pwsh(
            f". {_q(GATE)}\n"
            "function Get-ObjectPropertyValue { param($Object, $Name) "
            "if ($null -ne $Object -and $Object.PSObject.Properties[$Name]) { $Object.$Name } else { $null } }\n"
            f"$playbackSummary = Convert-PlaybackLogLineToObject {_q(_summary_line(**summary_fields))}\n"
            "$Seconds = 24; $LaunchOnlyProbe = $false; $validationFailures = @(); $validationWarnings = @(); "
            "$clipLengthGate = [pscustomobject]@{ frames = 720 }\n"
            f"{block}\n"
            "[pscustomobject]@{ invalidLooped = [bool]$invalidLooped; failures = @($validationFailures).Count } "
            "| ConvertTo-Json -Compress")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def test_the_runners_verdict_application_is_executed_and_every_guard_mutation_is_caught(self) -> None:
        text = RUNNER.read_text(encoding="utf-8")
        block = self._application_block(text)
        wrapped = dict(wrapped=1, wrap_count=1, total_frames=720, clip_seconds=30.03, presented_frames=500)
        clean = dict(wrapped=0, wrap_count=0, total_frames=720, clip_seconds=30.03, presented_frames=500)
        # The real block: a wrap makes the run INVALID_LOOPED with its failure recorded; a clean run does not.
        got = self._run_application_block(block, **wrapped)
        self.assertTrue(got["invalidLooped"], got)
        self.assertGreaterEqual(got["failures"], 1, got)
        got = self._run_application_block(block, **clean)
        self.assertFalse(got["invalidLooped"], got)
        self.assertEqual(got["failures"], 0, got)

        # Mutations of the APPLICATION guard (fable/sol r2: the old test pinned text, so these survived).
        mutations = {
            "guard disabled": ("if ($loopVerdict.invalid) {", "if ($false -and $loopVerdict.invalid) {"),
            "guard inverted": ("if ($loopVerdict.invalid) {", "if (-not $loopVerdict.invalid) {"),
            "invalid flag never set": ("$invalidLooped = -not $LaunchOnlyProbe", "$invalidLooped = $false"),
            "failures dropped": ("$validationFailures += $loopVerdict.failures", "$null = $loopVerdict.failures"),
            "verdict ignored": ("$loopVerdict = Get-GuiSmokeLoopVerdict -Summary $playbackSummary",
                                "$loopVerdict = [pscustomobject]@{ invalid = $false; failures = @() }; "
                                "$null = Get-GuiSmokeLoopVerdict -Summary $playbackSummary"),
        }
        for name, (needle, replacement) in mutations.items():
            with self.subTest(name):
                self.assertIn(needle, block, f"mutation anchor vanished: {name}")
                mutated = block.replace(needle, replacement, 1)
                survived = True
                for fields, expected_invalid in ((wrapped, True), (clean, False)):
                    outcome = self._run_application_block(mutated, **fields)
                    if outcome["invalidLooped"] != expected_invalid or \
                            (expected_invalid and outcome["failures"] < 1):
                        survived = False
                self.assertFalse(survived, f"guard mutation '{name}' survived the application test")

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
        self.assertGreaterEqual(handling.count("m_playbackWrapRecorder.noteEngineWrap()"), 2, handling[:200])
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
            if path.suffix.lower() == ".py" and (path.name.lower().startswith("test_") or path.name == "synthetic_mlv.py"):
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
#   * any actionPlay->trigger()/toggle()/setChecked(true)/setPlaying/on_actionPlay_triggered(true) outside
#     a line carrying an explicit `allowlisted:` comment (the gate's own Play, and a stop of a checked Play);
#   * any Loop-enable (actionLoop->trigger()/toggle()/setChecked(true)) outside the one allowlisted line in
#     forceLoopOffForAutomation (which only ever UNCHECKS), anywhere in platform/qt or src;
#   * any programmaticPlay( call whose site is not in the reviewed set, so a NEW programmatic Play cannot
#     appear without updating this list (and therefore being reviewed);
#   * a programmaticPlay body that triggers Play before it has evaluated the window and admitted it to the
#     ledger;
#   * a reviewed entry point that never forces Loop off.
# The scan is a pure function over {path: text} so the test can MUTATE the real sources in memory and
# require the scan to go red (a scan that passes on a deliberately broken tree is worthless).

PLAY_SOURCES = ("platform/qt/MainWindow.cpp", "platform/qt/main.cpp")
REVIEWED_PROGRAMMATIC_PLAY_SITES = {
    "autoplay",                          # MLVAPP_AUTOPLAY_* hook (constructor)
    "profile-look-assist-settle",        # --profile-playback --exercise-look-assist-settle / -toggle
    "profile-exercise-play-action",      # --profile-playback --exercise-play-action
    "gui-smoke-measured",                # --gui-smoke-playback measured Play
}
# Entry functions that must force Loop OFF (the autoplay hook is a lambda in the MainWindow constructor).
LOOP_OFF_ENTRY_MARKERS = (
    'forceLoopOffForAutomation( "autoplay" )',
    'forceLoopOffForAutomation( "profile-entry" )',
    'forceLoopOffForAutomation( "profile-look-assist-settle" )',
    'forceLoopOffForAutomation( "profile-exercise-play-action" )',
    'forceLoopOffForAutomation( "gui-smoke-entry" )',
    'forceLoopOffForAutomation( "gui-smoke-measured" )',
)

_PLAY_START = re.compile(
    r"actionPlay\s*->\s*(?:trigger|toggle|activate)\s*\(|actionPlay\s*->\s*setChecked\s*\(\s*true"
    r"|on_actionPlay_triggered\s*\(\s*true|setPlaying\s*\(")
_LOOP_ENABLE = re.compile(
    r"actionLoop\s*->\s*(?:trigger|toggle|activate)\s*\(|actionLoop\s*->\s*setChecked\s*\(\s*true")
_GATED_CALL = re.compile(r'programmaticPlay\s*\(\s*"([^"]+)"')


def _cpp_code_lines(text: str) -> list[tuple[int, str, str]]:
    """(line number, code with // comments stripped, the original line) for every line."""
    out = []
    for number, line in enumerate(text.splitlines(), start=1):
        code = line.split("//", 1)[0]
        out.append((number, code, line))
    return out


def _function_body(text: str, signature: str) -> str:
    at = text.find(signature)
    if at < 0:
        return ""
    brace = text.index("{", at)
    depth = 0
    for index in range(brace, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[brace:index + 1]
    return ""


def scan_app_play_sources(sources: dict[str, str]) -> list[str]:
    """Returns one string per violation; empty means the app-side play gate class is closed."""
    problems: list[str] = []
    sites_seen: set[str] = set()
    for path, text in sources.items():
        for number, code, line in _cpp_code_lines(text):
            if _PLAY_START.search(code) and "allowlisted:" not in line:
                problems.append(f"{path}:{number}: un-allowlisted programmatic Play start: {line.strip()}")
            if _LOOP_ENABLE.search(code) and "allowlisted:" not in line:
                problems.append(f"{path}:{number}: un-allowlisted Loop toggle: {line.strip()}")
            for site in _GATED_CALL.findall(code):
                sites_seen.add(site)
    main_window = sources.get("platform/qt/MainWindow.cpp", "")
    if sites_seen != REVIEWED_PROGRAMMATIC_PLAY_SITES:
        problems.append(
            f"programmaticPlay sites {sorted(sites_seen)} != reviewed {sorted(REVIEWED_PROGRAMMATIC_PLAY_SITES)}")
    body = _function_body(main_window, "bool MainWindow::programmaticPlay(")
    if not body:
        problems.append("MainWindow::programmaticPlay not found")
    else:
        trigger_at = body.find("actionPlay->trigger()")
        for needed in ("checkPlayableWindow(", "m_programmaticPlayLedger.admit("):
            at = body.find(needed)
            if at < 0 or trigger_at < 0 or at > trigger_at:
                problems.append(f"programmaticPlay must call {needed} before it triggers Play")
        if "return false" not in body[:trigger_at if trigger_at > 0 else len(body)]:
            problems.append("programmaticPlay has no refusal path before the Play trigger")
    for marker in LOOP_OFF_ENTRY_MARKERS:
        if marker not in main_window:
            problems.append(f"missing Loop-off at an automation entry: {marker}")
    # The gate's own helper must be the only Loop toggle, and must only ever uncheck.
    loop_body = _function_body(main_window, "void MainWindow::forceLoopOffForAutomation(")
    if "if( !ui->actionLoop->isChecked() ) return;" not in loop_body:
        problems.append("forceLoopOffForAutomation must return early unless Loop is checked (it only unchecks)")
    # Interactive handlers are never gated.
    for signature in ("void MainWindow::on_actionPlay_triggered(bool checked)",
                      "void MainWindow::on_actionPlay_toggled(bool checked)"):
        if "programmaticPlay(" in _function_body(main_window, signature):
            problems.append(f"{signature} is a user-input handler and must not be gated")
    return problems


def _load_play_sources() -> dict[str, str]:
    sources = {path: (ROOT / path).read_text(encoding="utf-8") for path in PLAY_SOURCES}
    # Every other C++ translation unit under platform/qt and src that names the Play action at all.
    for base in ("platform/qt", "src"):
        for candidate in sorted((ROOT / base).rglob("*")):
            if candidate.suffix not in (".cpp", ".h", ".hpp", ".c") or not candidate.is_file():
                continue
            relative = candidate.relative_to(ROOT).as_posix()
            if relative in sources:
                continue
            text = candidate.read_text(encoding="utf-8", errors="replace")
            if "actionPlay" in text or "actionLoop" in text or "setPlaying" in text:
                sources[relative] = text
    return sources


class AppPlayGateStaticClassTests(unittest.TestCase):
    """5. The static class test: no programmatic Play and no Loop-enable outside the reviewed gate."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.sources = _load_play_sources()

    def test_the_real_sources_are_clean(self) -> None:
        self.assertEqual(scan_app_play_sources(self.sources), [])

    def test_every_programmatic_play_site_is_reviewed_and_ordered_before_the_gate_is_trusted(self) -> None:
        text = self.sources["platform/qt/MainWindow.cpp"]
        self.assertEqual(set(_GATED_CALL.findall(text)), REVIEWED_PROGRAMMATIC_PLAY_SITES)

    def test_the_actions_are_not_persisted_or_declared_checked(self) -> None:
        # Loop starts unchecked and nothing persists it: a test entry never inherits a looping GUI.
        ui = (ROOT / "platform" / "qt" / "MainWindow.ui").read_text(encoding="utf-8")
        loop = ui[ui.index('<action name="actionLoop">'):]
        loop = loop[:loop.index("</action>")]
        self.assertNotRegex(loop, r'<property name="checked">\s*<bool>true</bool>')
        text = self.sources["platform/qt/MainWindow.cpp"]
        for line in text.splitlines():
            code = line.split("//", 1)[0]
            if "actionLoop" in code and re.search(r"setValue|QSettings|settings\.", code):
                self.fail(f"Loop appears to be persisted: {line.strip()}")

    def test_the_pure_window_logic_lives_in_the_unit_tested_header(self) -> None:
        header = (ROOT / "platform" / "qt" / "PlaybackFrameRange.h").read_text(encoding="utf-8")
        self.assertIn("constexpr double kMinPlayWindowSeconds = 20.0;", header)
        for name in ("evaluatePlayableWindow", "ProgrammaticPlayLedger", "noteJumpToFirst", "noteRestart"):
            self.assertIn(name, header)
        tests = (ROOT / "tests" / "console" / "test_playback_frame_range.cpp").read_text(encoding="utf-8")
        for name in ("PlayableWindow", "ProgrammaticPlayLedger"):
            self.assertIn(f"TEST( {name}, ", tests)

    # -- mutation tests: the scan must go red on a deliberately broken tree ------------------------------

    def _mutated(self, old: str, new: str, path: str = "platform/qt/MainWindow.cpp") -> dict[str, str]:
        text = self.sources[path]
        self.assertIn(old, text, "mutation anchor missing: the scan test needs updating with the source")
        mutated = dict(self.sources)
        mutated[path] = text.replace(old, new, 1)
        return mutated

    def test_mutation_a_bare_play_trigger_in_an_automation_path_is_caught(self) -> None:
        mutated = self._mutated('forceLoopOffForAutomation( "gui-smoke-measured" );',
                                'forceLoopOffForAutomation( "gui-smoke-measured" );\n    ui->actionPlay->trigger();')
        problems = scan_app_play_sources(mutated)
        self.assertTrue(any("un-allowlisted programmatic Play start" in p for p in problems), problems)

    def test_mutation_a_loop_enable_is_caught(self) -> None:
        mutated = self._mutated('forceLoopOffForAutomation( "gui-smoke-measured" );',
                                'forceLoopOffForAutomation( "gui-smoke-measured" );\n    ui->actionLoop->setChecked( true );')
        problems = scan_app_play_sources(mutated)
        self.assertTrue(any("un-allowlisted Loop toggle" in p for p in problems), problems)

    def test_mutation_a_new_programmatic_play_site_must_be_reviewed(self) -> None:
        mutated = self._mutated('programmaticPlay( "autoplay", autoplaySeconds )',
                                'programmaticPlay( "autoplay-two", autoplaySeconds )')
        problems = scan_app_play_sources(mutated)
        self.assertTrue(any("programmaticPlay sites" in p for p in problems), problems)

    def test_mutation_removing_the_ledger_or_the_window_check_from_the_gate_is_caught(self) -> None:
        for anchor in ("m_programmaticPlayLedger.admit(", "checkPlayableWindow( site, requestedSeconds );"):
            with self.subTest(anchor):
                text = self.sources["platform/qt/MainWindow.cpp"]
                body = _function_body(text, "bool MainWindow::programmaticPlay(")
                self.assertIn(anchor, body)
                mutated_body = body.replace(anchor, "true; /* mutated */", 1)
                mutated = dict(self.sources)
                mutated["platform/qt/MainWindow.cpp"] = text.replace(body, mutated_body, 1)
                problems = scan_app_play_sources(mutated)
                self.assertTrue(any("programmaticPlay must call" in p for p in problems), problems)

    def test_mutation_gating_the_interactive_handler_is_caught(self) -> None:
        mutated = self._mutated("//Play button pressed\nvoid MainWindow::on_actionPlay_triggered(bool checked)\n{",
                                "//Play button pressed\nvoid MainWindow::on_actionPlay_triggered(bool checked)\n{\n    programmaticPlay( \"x\", 0.0 );")
        problems = scan_app_play_sources(mutated)
        self.assertTrue(any("user-input handler" in p for p in problems), problems)

    def test_mutation_dropping_a_loop_off_at_an_entry_is_caught(self) -> None:
        mutated = self._mutated('forceLoopOffForAutomation( "gui-smoke-entry" );', "")
        problems = scan_app_play_sources(mutated)
        self.assertTrue(any("missing Loop-off" in p for p in problems), problems)

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
        self.assertTrue(any("Loop toggle" in p for p in problems), problems)
        self.assertTrue(any("Play start" in p for p in problems), problems)


if __name__ == "__main__":
    unittest.main()
