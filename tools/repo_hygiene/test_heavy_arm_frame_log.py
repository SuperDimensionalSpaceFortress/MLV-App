"""Tests for PROCESSED8-MEASURED-BRANCH-HEAVY-LEG-SPEC-1 round 2 (sol r1 blocker): a HEAVY telemetry arm keeps its per-frame rows.

CLASS: the generated job hands the smoke runner an -ExtraEnvironment list, and the runner builds the app's environment from its OWN process environment plus that
list. The app (platform/qt/MainWindow.cpp environmentFlagEnabled) suppresses the playback_smoke.frame rows for ANY value of
MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG that is not empty / 0 / false / off / no, and Get-FrameRows then throws 'only 0 high-resolution frame rows'
before the leg publishes. LIGHT sets the flag to 1 on purpose; HEAVY used to say nothing, so an inherited 1 (a shell, a Scheduled Task env block) turned a
HEAVY leg into a LIGHT one. The job now names the flag in the HEAVY arm too, at its off value.

Source: sol r1 on PR 711. Keys and shapes only: the generator runs against header-only stand-in clips, no footage is read, and no app is launched. The
environment the app would see is rebuilt from the job's own text (the real `$telemetryArmEnvs` .. `$envs` slice, executed by pwsh) and the app's own flag rule.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.repo_hygiene.synthetic_mlv import FRAMES_30S_AT_23976, write_synthetic_mlv
from tools.repo_hygiene.test_dual_venue_evidence import (
    FIXTURE_IDS,
    GENERATOR,
    MLV_EXT,
    ROOT,
    lf,
    requires_windows_pwsh,
    run_pwsh,
)

FLAG = "MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG"
LEGS = ROOT / "tools" / "profiling" / "dual-venue" / "legs"
NEW_SPEC = LEGS / "m16-1243-pace-cinematic-cpu-receipt-fullscreen-s4-heavy.json"
# platform/qt/MainWindow.cpp environmentFlagEnabled(): the values that leave the flag OFF (frame rows kept).
APP_OFF_VALUES = ("", "0", "false", "off", "no")
SLICE_START = "$telemetryArmEnvs = if ($TelemetryArm -eq 'LIGHT') {"
SLICE_END = '$envList = "\'" + ($envs -join "\',\'") + "\'"'


def app_frame_rows_suppressed(value: str | None) -> bool:
    """The app's rule for the flag, applied to the value the child process ends up with (None = not set at all)."""
    return value is not None and value.strip().lower() not in APP_OFF_VALUES


def expand_env_pairs(envs: list[str]) -> list[str]:
    """The runner's Add-EnvironmentPairs (run-release-gui-smoke.ps1) first splits an entry on commas and keeps the parts only when there is more than one and
    every part is KEY=VALUE (HEAVY-FRAME-LOG-TEST-COMMA-PACKED-ENV-1); any other entry stays whole."""
    pairs: list[str] = []
    for raw in envs:
        if not raw.strip():
            continue
        parts = raw.split(",")
        pairs.extend(parts if len(parts) > 1 and all(part.find("=") >= 1 for part in parts) else [raw])
    return pairs


def child_flag_value(envs: list[str], inherited: str | None) -> str | None:
    """What the app sees: the runner's Add-EnvironmentPairs writes each KEY=VALUE onto the inherited environment, the last entry for a key wins."""
    value = inherited
    for entry in expand_env_pairs(envs):
        key, _, rest = entry.partition("=")
        if key.strip().upper() == FLAG:
            value = rest
    return value


def spec_generator_args(path: Path) -> list[str]:
    """The generator arguments a committed spec asks for, for the keys that decide the telemetry environment (the look arguments need a receipt and
    do not touch the environment slice)."""
    spec = json.loads(path.read_text(encoding="utf-8"))
    gen = spec["generatorArgs"]
    args = ["-Backend", spec["backends"][0], "-TelemetryArm", gen["telemetryArm"]]
    if gen.get("playbackProcessing"):
        args += ["-PlaybackProcessing", gen["playbackProcessing"]]
    return args


@requires_windows_pwsh
class HeavyArmKeepsFrameRows(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="heavy-frame-log-")
        cls.tmp = Path(cls._tmp.name)
        cls.head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        cls.repo = cls.tmp / "repo"
        subprocess.run(["git", "clone", "-q", "--shared", "--no-checkout", str(ROOT), str(cls.repo)], check=True)
        subprocess.run(["git", "-C", str(cls.repo), "sparse-checkout", "set", "--cone", "tools", "tests/fixtures/clips"], check=True)
        subprocess.run(["git", "-C", str(cls.repo), "checkout", "-q", cls.head], check=True)
        for stem in FIXTURE_IDS:
            write_synthetic_mlv(cls.repo / "tests" / "fixtures" / "clips" / (stem + MLV_EXT), FRAMES_30S_AT_23976)
        cls.count = 0

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def generate(self, extra: list[str]) -> str:
        type(self).count += 1
        out = self.tmp / f"job-{type(self).count}.ps1"
        proc = run_pwsh(["-File", str(GENERATOR), "-SourceCommit", self.head, "-BuildManifestSha256", "ab" * 32, "-ClipId", FIXTURE_IDS[0],
                         "-FixtureSha256", "cd" * 32, "-RepoRoot", str(self.repo), "-OutFile", str(out), *extra])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return lf(out.read_text(encoding="utf-8"))

    def job_envs(self, extra: list[str]) -> list[str]:
        """The `$envs` list the generated job passes to the smoke runner: the job's own slice, run by pwsh with the variables it reads stubbed."""
        text = self.generate(extra)
        start = text.index(SLICE_START)
        end = text.index(SLICE_END, start)
        script = self.tmp / "slice.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            "$TelemetryArm = '" + self.arm_in(text) + "'\n$reconDll = 'recon.dll'\n$pkgDir = 'pkg'\n$DisablePaintPerSubmit = $false\n"
            + text[start:end] + "\n$envs | ForEach-Object { $_ }\n",
            encoding="utf-8",
        )
        proc = run_pwsh(["-File", str(script)])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return [line for line in proc.stdout.splitlines() if line.strip()]

    @staticmethod
    def arm_in(text: str) -> str:
        for line in text.splitlines():
            if line.startswith("$TelemetryArm = '"):
                return line.split("'")[1]
        raise AssertionError("the generated job carries no $TelemetryArm assignment")

    def assert_rows_kept(self, extra: list[str], inherited: str | None) -> None:
        envs = self.job_envs(extra)
        self.assertIn("MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1", envs)
        seen = child_flag_value(envs, inherited)
        self.assertFalse(app_frame_rows_suppressed(seen), f"inherited {FLAG}={inherited!r} reaches the app as {seen!r}: every playback_smoke.frame row is suppressed")

    def assert_rows_suppressed(self, extra: list[str], inherited: str | None) -> None:
        seen = child_flag_value(self.job_envs(extra), inherited)
        self.assertTrue(app_frame_rows_suppressed(seen), f"LIGHT must keep suppressing the frame rows (inherited {inherited!r}, child {seen!r})")

    # --- sol r1 catalogue row: HEAVY receipt CPU spec + inherited disable flag 1 -> frame logging must remain enabled -------------------------------
    def test_heavy_receipt_cpu_spec_plus_inherited_disable_flag_1_must_retain_frame_rows(self) -> None:
        self.assert_rows_kept(spec_generator_args(NEW_SPEC), "1")

    def test_the_new_spec_still_asks_for_the_heavy_receipt_cpu_shape(self) -> None:
        self.assertEqual(spec_generator_args(NEW_SPEC), ["-Backend", "cpu", "-TelemetryArm", "HEAVY", "-PlaybackProcessing", "receipt"])

    # --- the class: every HEAVY job, whichever backend or processing mode the spec asks for ---------------------------------------------------------
    def test_heavy_cuda_job_retains_frame_rows_under_every_inherited_value(self) -> None:
        for inherited in (None, "1", "true", "on", "yes", "2"):
            with self.subTest(inherited=inherited):
                self.assert_rows_kept(["-TelemetryArm", "HEAVY"], inherited)

    def test_heavy_cpu_job_retains_frame_rows_under_every_inherited_value(self) -> None:
        for inherited in (None, "1", "true", "on", "yes", "2"):
            with self.subTest(inherited=inherited):
                self.assert_rows_kept(["-Backend", "cpu", "-TelemetryArm", "HEAVY"], inherited)

    def test_a_shared_heavy_spec_retains_frame_rows_when_the_flag_is_inherited(self) -> None:
        shared = [p for p in sorted(LEGS.glob("*.json")) if json.loads(p.read_text(encoding="utf-8"))["generatorArgs"].get("telemetryArm") == "HEAVY"]
        self.assertGreater(len(shared), 1, "expected the committed HEAVY specs")
        for path in shared[:3] + [NEW_SPEC]:
            with self.subTest(spec=path.name):
                self.assert_rows_kept(spec_generator_args(path), "1")

    # --- controls: LIGHT is unchanged ----------------------------------------------------------------------------------------------------------------
    def test_light_jobs_keep_suppressing_frame_rows_with_or_without_an_inherited_flag(self) -> None:
        for extra in (["-TelemetryArm", "LIGHT"], ["-Backend", "cpu", "-TelemetryArm", "LIGHT"]):
            for inherited in (None, "0", "1"):
                with self.subTest(extra=extra, inherited=inherited):
                    self.assert_rows_suppressed(extra, inherited)

    def test_light_job_environment_is_the_three_entries_it_always_had(self) -> None:
        envs = self.job_envs(["-TelemetryArm", "LIGHT"])
        self.assertEqual(
            [e for e in envs if e.startswith("MLVAPP_PLAYBACK_SMOKE_") or e.startswith("MLVAPP_GPU_PLAYBACK_RECON_ELIGIBILITY")],
            ["MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1", "MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG=1", "MLVAPP_GPU_PLAYBACK_RECON_ELIGIBILITY_DIAG=1"],
        )


GL_FLAGS = ("MLVAPP_EXPERIMENTAL_GL_VIEWPORT", "MLVAPP_EXPERIMENTAL_GL_WINDOW_VIEWPORT")
ROUTE_FUNCTION = "function Get-AttrCpuProcessed8Route("
ROUTE_CALL = "$processed8Route = Get-AttrCpuProcessed8Route -RawLog $rawLog -MeasuredSessionId $measuredSmokeSessionId"
RECEIPT_FUNCTION = "function Test-AttrSmokeCommandAsksReceipt("
RECEIPT_CALL = "$processed8RouteRequired = Test-AttrSmokeCommandAsksReceipt $cmd"
CPU_RECEIPT_LIGHT = ["-Backend", "cpu", "-TelemetryArm", "LIGHT", "-PlaybackProcessing", "receipt"]


def child_env_value(envs: list[str], inherited: str | None, flag: str) -> str | None:
    """child_flag_value for any flag: the runner writes each KEY=VALUE onto the environment it inherited, the last entry for a key wins."""
    value = inherited
    for entry in expand_env_pairs(envs):
        key, _, rest = entry.partition("=")
        if key.strip().upper() == flag:
            value = rest
    return value


def cache_summary_line(session: str, p8: str, p16: str) -> str:
    """The app's one-shot playback_smoke.processed8_cache_summary line (MainWindow.cpp), with the two counters the route gate reads."""
    return (f"[info] playback_smoke.processed8_cache_summary session={session} processed8_cache_hits=0 processed8_prefetch_hits=0 "
            f"processed8_direct_path_frames=0 processed8_measured_frames={p8} processed16_measured_frames={p16}")


@requires_windows_pwsh
class CpuReceiptLegRunsProcessed8(unittest.TestCase):
    """PROCESSED8-MEASURED-BRANCH-HEAVY-LEG-SPEC-1 r5 (sol r4 B1, PROCESSED8-CPU-LEG-GL-VIEWPORT-ENV-CLEAR-1). Catalogue row: CPU receipt HEAVY spec + inherited
    GL viewport/window flag 1 -> retain Processed8 or refuse the leg explicitly; never accept Processed16 as the Processed8 measurement.

    The smoke command passes -PreserveExperimentalEnvironment, so an inherited MLVAPP_EXPERIMENTAL_GL_VIEWPORT / _GL_WINDOW_VIEWPORT reached the app, which
    installed the GL surface and ran OutputProcessed16 on every frame (processed8_measured=0, cpu frames and no gpu frame: the exit-28 gate passed it).
    (a) every cpu receipt job removes both flags from its own process before the smoke child starts; (b) it refuses, typed, a measured session whose
    processed8 counters show no Processed8 frame or a Processed16-only frame."""

    setUpClass = classmethod(HeavyArmKeepsFrameRows.setUpClass.__func__)
    tearDownClass = classmethod(HeavyArmKeepsFrameRows.tearDownClass.__func__)
    generate = HeavyArmKeepsFrameRows.generate
    arm_in = staticmethod(HeavyArmKeepsFrameRows.arm_in)

    def child_gl_flags(self, extra: list[str], inherited: dict[str, str]) -> dict[str, str | None]:
        """What the smoke child (and so the app) sees for each GL flag: the job's own slice runs in a pwsh whose environment carries `inherited`, then the
        runner's rule (inherited process environment plus the -ExtraEnvironment list, last entry wins) is applied to what the job left behind."""
        text = self.generate(extra)
        start = text.index(SLICE_START)
        end = text.index(SLICE_END, start)
        script = self.tmp / "gl-slice.ps1"
        report = "".join(
            f"$v = [Environment]::GetEnvironmentVariable('{flag}'); if ($null -eq $v) {{ 'PROC {flag} <unset>' }} else {{ 'PROC {flag}=' + $v }}\n"
            for flag in GL_FLAGS)
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            "$TelemetryArm = '" + self.arm_in(text) + "'\n$reconDll = 'recon.dll'\n$pkgDir = 'pkg'\n$DisablePaintPerSubmit = $false\n"
            + text[start:end] + "\n$envs | ForEach-Object { 'ENV ' + $_ }\n" + report,
            encoding="utf-8",
        )
        proc = run_pwsh(["-File", str(script)], env_extra=inherited)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        lines = proc.stdout.splitlines()
        envs = [line[4:] for line in lines if line.startswith("ENV ")]
        seen: dict[str, str | None] = {}
        for flag in GL_FLAGS:
            row = next(line for line in lines if line.startswith(f"PROC {flag}"))
            left = None if row.endswith(" <unset>") else row.split("=", 1)[1]
            seen[flag] = child_env_value(envs, left, flag)
        return seen

    def route(self, log_lines: list[str], session: str = "7") -> dict:
        """The job's own Get-AttrCpuProcessed8Route (cut from the generated cpu receipt job), run by pwsh over a stand-in app log."""
        text = self.generate(spec_generator_args(NEW_SPEC))
        self.assertTrue(ROUTE_FUNCTION in text, "the cpu receipt job carries no Processed8 route gate: a session that ran no Processed8 frame is not refused")
        start = text.index(ROUTE_FUNCTION)
        end = text.index('$envList = "', start)
        log = self.tmp / "app.log"
        log.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        script = self.tmp / "route.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n" + text[start:end]
            + f"\n$r = Get-AttrCpuProcessed8Route -RawLog ([IO.File]::ReadAllText('{log}')) -MeasuredSessionId '{session}'\n$r | ConvertTo-Json -Compress\n",
            encoding="utf-8",
        )
        proc = run_pwsh(["-File", str(script)])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout.strip().splitlines()[-1])

    # --- (a) sol r4 catalogue row: the inherited flag never reaches the child ------------------------------------------------------------------------
    def test_cpu_receipt_heavy_spec_plus_inherited_gl_viewport_flags_1_reach_no_child(self) -> None:
        seen = self.child_gl_flags(spec_generator_args(NEW_SPEC), {flag: "1" for flag in GL_FLAGS})
        self.assertEqual(seen, {flag: None for flag in GL_FLAGS}, "an inherited GL viewport/window flag reaches the app: it installs the GL surface and runs Processed16")

    def test_every_cpu_receipt_shape_clears_each_flag_alone_under_every_inherited_value(self) -> None:
        for extra in (spec_generator_args(NEW_SPEC), CPU_RECEIPT_LIGHT):
            for flag in GL_FLAGS:
                for inherited in ("1", "true", "yes", "0"):
                    with self.subTest(extra=extra, flag=flag, inherited=inherited):
                        # unset, never 0: main.cpp tests these flags with qEnvironmentVariableIsSet (shared GL contexts), so a 0 is not "off" there
                        self.assertIsNone(self.child_gl_flags(extra, {flag: inherited})[flag])

    # --- (b) a session that did not run Processed8 is refused, typed -------------------------------------------------------------------------------
    def test_cpu_receipt_job_refuses_a_session_that_ran_no_processed8_frame(self) -> None:
        cases = {
            "processed16 route (sol's repro: processed8_measured stays 0)": ([cache_summary_line("7", "0", "600")], False, "PROCESSED8_NOT_MEASURED", 0, 600),
            "no frame measured at all": ([cache_summary_line("7", "0", "0")], False, "PROCESSED8_NOT_MEASURED", 0, 0),
            "some frames took the Processed16 route": ([cache_summary_line("7", "300", "600")], False, "PROCESSED16_ROUTE_FRAMES", 300, 600),
            "no summary line": (["[info] playback_smoke.summary session=7"], False, "PROCESSED8_ROUTE_SUMMARY_MISSING", None, None),
            "only another session's line": ([cache_summary_line("8", "600", "600")], False, "PROCESSED8_ROUTE_SUMMARY_MISSING", None, None),
            "unparsable counter": ([cache_summary_line("7", "na", "600")], False, "PROCESSED8_ROUTE_SUMMARY_UNPARSABLE", None, None),
            "processed16 below processed8 (every Processed8 frame counts in both)": ([cache_summary_line("7", "600", "10")], False, "PROCESSED8_ROUTE_SUMMARY_UNPARSABLE", None, None),
            "Processed8 on every frame": ([cache_summary_line("7", "600", "600")], True, "NONE", 600, 600),
            "the measured session's LAST line wins": ([cache_summary_line("7", "0", "600"), cache_summary_line("8", "0", "9"), cache_summary_line("7", "600", "600")], True, "NONE", 600, 600),
        }
        for name, (lines, taken, reason, p8, p16) in cases.items():
            with self.subTest(case=name):
                got = self.route(lines)
                self.assertEqual((got["taken"], got["reason"]), (taken, reason))
                self.assertEqual((got["processed8MeasuredFrames"], got["processed16MeasuredFrames"]), (p8, p16))
                self.assertEqual(got["processed16OnlyFrames"], None if p8 is None else p16 - p8)

    def test_a_sessionless_summary_row_never_replaces_the_measured_sessions_counters(self) -> None:
        """Adversary r5 A1 (sol, on bab3f11c): a processed8_cache_summary row with no session= key was taken as the measured session's and, being later,
        replaced its failing counters (taken=true). The app always prints session=, so a row without it is not the measured session's."""
        lines = ["playback_smoke.measured_session id=7",
                 "playback_smoke.processed8_cache_summary session=7 processed8_measured_frames=0 processed16_measured_frames=600",
                 "playback_smoke.processed8_cache_summary processed8_measured_frames=600 processed16_measured_frames=600"]
        got = self.route(lines)
        self.assertEqual((got["taken"], got["reason"], got["processed8MeasuredFrames"]), (False, "PROCESSED8_NOT_MEASURED", 0))
        got = self.route(lines[2:])
        self.assertEqual((got["taken"], got["reason"]), (False, "PROCESSED8_ROUTE_SUMMARY_MISSING"), "a sessionless row alone is no measured-session evidence")
        got = self.route([cache_summary_line("7", "600", "600")], session="")
        self.assertEqual((got["taken"], got["reason"]), (False, "PROCESSED8_ROUTE_SUMMARY_MISSING"), "no measured session id: nothing is admitted")

    def test_a_key_suffix_token_is_never_the_session_or_a_counter(self) -> None:
        """Sol r6 BLOCKER (on 7db6bf13). Catalogue row: a failing measured-session counter plus `not-session=<measured>` or `not-processed8_measured_frames=<positive>`
        is still refused. The row was read for any `[A-Za-z0-9_]+=` match, so the tail `session=7` of `not-session=7` (and `processed8_measured_frames=600` of
        `not-processed8_measured_frames=600`) was taken as the key and replaced the real one. A key is a whole token: it starts at the line start or after whitespace."""
        failing = "playback_smoke.processed8_cache_summary session=7 processed8_measured_frames=0 processed16_measured_frames=600"
        refused = (False, "PROCESSED8_NOT_MEASURED", 0, 600)
        cases = {
            "another session's row spells not-session=<measured> (sol repro 1)": (
                [failing, "playback_smoke.processed8_cache_summary session=8 not-session=7 processed8_measured_frames=600 processed16_measured_frames=600"], refused),
            "the measured row carries a not-processed8_measured_frames=600 suffix (sol repro 2)": (
                [failing + " not-processed8_measured_frames=600"], refused),
            "a not-processed16_measured_frames suffix cannot lift the Processed16 count": (
                [cache_summary_line("7", "600", "600") + " not-processed16_measured_frames=1000"], (True, "NONE", 600, 600)),
            "a dotted prefix is not a key boundary either": (
                [failing, "playback_smoke.processed8_cache_summary session=8 x.session=7 processed8_measured_frames=600 processed16_measured_frames=600"], refused),
            "a row whose only session= is a suffix token is no evidence for the session": (
                ["playback_smoke.processed8_cache_summary not-session=7 processed8_measured_frames=600 processed16_measured_frames=600"],
                (False, "PROCESSED8_ROUTE_SUMMARY_MISSING", None, None)),
            "a whole key that merely ends the same way is a different key and is ignored": (
                [failing + " xprocessed8_measured_frames=600"], refused),
        }
        for name, (lines, (taken, reason, p8, p16)) in cases.items():
            with self.subTest(case=name):
                got = self.route(lines)
                self.assertEqual((got["taken"], got["reason"]), (taken, reason))
                self.assertEqual((got["processed8MeasuredFrames"], got["processed16MeasuredFrames"]), (p8, p16))

    def test_the_route_gate_runs_after_the_exit_28_gate_and_before_any_success_summary(self) -> None:
        for extra in (spec_generator_args(NEW_SPEC), CPU_RECEIPT_LIGHT):
            with self.subTest(extra=extra):
                text = self.generate(extra)
                call = text.index(ROUTE_CALL)
                self.assertGreater(call, text.index("RESULT=CPU_BACKEND_PATH_MISMATCH"))
                self.assertLess(call, text.index("$stats = [ordered]@{}", call))
                self.assertEqual(text.count(ROUTE_CALL), 1)
                gate = text[call:text.index("$stats = [ordered]@{}", call)]
                self.assertIn("RESULT=PROCESSED8_ROUTE_NOT_TAKEN", gate)
                self.assertIn("exit 33", gate)
                self.assertIn("    processed8MeasuredFrames = $processed8Route.processed8MeasuredFrames\n", text, "the success summary records the counters it was admitted on")
                job = self.tmp / "parse.ps1"
                job.write_text(text, encoding="utf-8")
                proc = run_pwsh(["-Command", f"$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile('{job}', [ref]$null, [ref]$e); $e.Count"])
                self.assertEqual(proc.stdout.strip(), "0", proc.stdout + proc.stderr)

    def test_the_refusal_is_never_a_pass(self) -> None:
        module = ROOT / "tools" / "profiling" / "dual-venue" / "DualVenueRunner.psm1"
        proc = run_pwsh(["-Command", f"$m = Import-Module '{module}' -PassThru -Force; & $m {{ (Resolve-DvJobOutcome -ResultToken 'PROCESSED8_ROUTE_NOT_TAKEN' -ExitCode 33).outcome }}"])
        self.assertEqual(proc.stdout.strip().splitlines()[-1], "FAIL", proc.stdout + proc.stderr)

    # --- controls: the cuda legs and the cpu legs without receipt are the text they were -----------------------------------------------------------
    def test_cuda_job_keeps_its_gl_window_viewport_and_carries_no_route_gate(self) -> None:
        text = self.generate(["-TelemetryArm", "HEAVY"])
        self.assertIn("'MLVAPP_EXPERIMENTAL_GL_WINDOW_VIEWPORT=1',", text)
        for needle in ("Get-AttrCpuProcessed8Route", "PROCESSED8_ROUTE_NOT_TAKEN", "$glViewportInheritedCleared"):
            self.assertNotIn(needle, text)

    def receipt_required(self, smoke_command: str) -> bool:
        """The job's own decision (cut from the generated cpu receipt job) whether `$cmd` is a Processed8 receipt command: its helper when it has one, then the
        `$processed8RouteRequired` line, run by pwsh over a stand-in smoke command."""
        text = self.generate(spec_generator_args(NEW_SPEC))
        helper = ""
        if RECEIPT_FUNCTION in text:
            start = text.index(RECEIPT_FUNCTION)
            helper = text[start:text.index("\n}\n", start) + 3]
        line = next(line for line in text.splitlines() if line.startswith("$processed8RouteRequired = "))
        script = self.tmp / "receipt-required.ps1"
        script.write_text("$ErrorActionPreference = 'Stop'\n" + helper + "\n$cmd = @'\n" + smoke_command + "\n'@\n" + line + "\n[string]$processed8RouteRequired\n", encoding="utf-8")
        proc = run_pwsh(["-File", str(script)])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc.stdout.strip().splitlines()[-1] == "True"

    def test_the_receipt_switch_is_read_without_regard_to_case_and_never_from_a_quoted_path_or_environment_entry(self) -> None:
        """Adversary r6 A1 (the runner's ValidateSet is case-insensitive: -PlaybackProcessing RECEIPT is a receipt run, and was not gated) and A2 (an agent root
        whose text spells the switch made a subset run look like a receipt run, so it was refused at exit 33). Only the switch itself counts."""
        smoke = "& 'C:\\mlv\\smoke.ps1' -ExePath 'C:\\mlv\\MLVApp.exe' -Input 'C:\\clips\\a.mlv' -Seconds 28 -ScaleFactor 1 -UsePersistedPlaybackSettings{pp} -RequireLookAssist:$false -Scope none -ExtraEnvironment @({env})"
        trap = "C:\\mlvtmp\\ -PlaybackProcessing receipt \\mlv-agent"
        for name, pp, env, expected in (
                ("receipt", " -PlaybackProcessing receipt", "'A=1'", True),
                ("upper-case receipt", " -PlaybackProcessing RECEIPT", "'A=1'", True),
                ("mixed-case receipt", " -PlaybackProcessing Receipt", "'A=1'", True),
                ("subset", " -PlaybackProcessing subset", "'A=1'", False),
                ("no processing switch", "", "'A=1'", False),
                ("subset, receipt spelled in a quoted path", " -PlaybackProcessing subset", "'A=1'", False),
                ("subset, receipt spelled in an environment entry", " -PlaybackProcessing subset", "'X= -PlaybackProcessing receipt '", False),
                ("receipt, with a quoted path that spells it too", " -PlaybackProcessing receipt", "'A=1'", True)):
            with self.subTest(case=name):
                command = smoke.format(pp=pp, env=env)
                if "quoted path" in name:
                    command = command.replace("C:\\mlv\\smoke.ps1", trap)
                self.assertEqual(self.receipt_required(command), expected, command)

    def test_the_gate_binds_only_a_job_whose_smoke_command_asks_for_receipt(self) -> None:
        """The block is the same text in every cpu job (so -PlaybackProcessing receipt adds only its own token); the job reads whether it is a receipt job
        from its own smoke command. A subset / default cpu job (Debayered16 by design) clears the GL flags too but is never refused for lacking Processed8."""
        needle = RECEIPT_CALL
        for extra, required in ((spec_generator_args(NEW_SPEC), True), (CPU_RECEIPT_LIGHT, True),
                                (["-Backend", "cpu", "-TelemetryArm", "HEAVY"], False),
                                (["-Backend", "cpu", "-TelemetryArm", "HEAVY", "-PlaybackProcessing", "subset"], False)):
            with self.subTest(extra=extra):
                text = self.generate(extra)
                self.assertEqual(text.count(needle), 1)
                self.assertIn("if ($processed8RouteRequired -and -not $processed8Route.taken) {", text)
                smoke = [line for line in text.splitlines() if line.lstrip().startswith('$cmd = "& ')]
                self.assertEqual(len(smoke), 1)
                self.assertEqual(" -PlaybackProcessing receipt " in smoke[0], required)
                self.assertEqual(self.child_gl_flags(extra, {flag: "1" for flag in GL_FLAGS}), {flag: None for flag in GL_FLAGS})


class Processed8RouteMirrorsTheApp(unittest.TestCase):
    """The gate's reading of the app is checked against the C++ it relies on, so the two cannot drift apart unseen."""

    def test_the_counters_the_gate_reads_are_the_ones_the_app_prints(self) -> None:
        main_window = (ROOT / "platform" / "qt" / "MainWindow.cpp").read_text(encoding="utf-8")
        self.assertIn('"playback_smoke.processed8_cache_summary session=%1 "', main_window)
        self.assertIn('"processed8_measured_frames=%5 processed16_measured_frames=%6" )', main_window)
        render = (ROOT / "platform" / "qt" / "RenderFrameThread.cpp").read_text(encoding="utf-8")
        # a Processed8 frame counts in processed16_measured too, so processed16 - processed8 is the Processed16-only (OutputProcessed16) frames
        self.assertIn("const bool processed16StageMeasured =\n        renderThreadProcessed8StageRan || renderThreadProcessed16StageRan;", lf(render))

    def test_main_reads_the_gl_flags_as_set_or_unset_so_the_job_unsets_them(self) -> None:
        main = (ROOT / "platform" / "qt" / "main.cpp").read_text(encoding="utf-8")
        for flag in GL_FLAGS:
            self.assertIn(f'qEnvironmentVariableIsSet("{flag}")', main)


class EnvModelExpandsCommaPackedPairs(unittest.TestCase):
    """HEAVY-FRAME-LOG-TEST-COMMA-PACKED-ENV-1 (sol r6 hardening): the test model of the runner's environment reads a comma-packed entry as the separate pairs."""

    def test_a_comma_packed_entry_is_read_as_its_pairs(self) -> None:
        self.assertEqual(child_flag_value([f"{FLAG}=0,OTHER=1"], "1"), "0")
        self.assertEqual(child_flag_value(["OTHER=1,B=2", f"{FLAG}=1,X=2"], None), "1")
        self.assertEqual(child_env_value([f"A=1,{FLAG}=off"], "1", FLAG), "off")

    def test_an_entry_that_is_not_all_pairs_stays_whole(self) -> None:
        self.assertEqual(child_flag_value([f"{FLAG}=a,b"], None), "a,b")
        self.assertEqual(expand_env_pairs(["", " ", "A=1"]), ["A=1"])


class FlagRuleMirrorsTheApp(unittest.TestCase):
    """The test's copy of the app's flag rule is checked against the C++ it mirrors, so the two cannot drift apart unseen."""

    def test_environment_flag_enabled_off_values_are_the_ones_the_tests_assume(self) -> None:
        text = (ROOT / "platform" / "qt" / "MainWindow.cpp").read_text(encoding="utf-8")
        start = text.index("static bool environmentFlagEnabled( const char *name )")
        body = text[start:text.index("\n}\n", start)]
        for literal in ("0", "false", "off", "no"):
            self.assertIn(f'QByteArrayLiteral("{literal}")', body)
        self.assertIn("trimmed().toLower()", body)
        self.assertTrue(app_frame_rows_suppressed("1"))
        self.assertFalse(app_frame_rows_suppressed("0"))
        self.assertFalse(app_frame_rows_suppressed(None))


if __name__ == "__main__":
    unittest.main()
