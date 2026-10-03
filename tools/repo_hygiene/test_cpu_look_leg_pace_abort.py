"""Tests for CPU-LOOK-LEG-PACE-ABORT-1 (round 1): a slow CPU look leg plays to 25 s instead of aborting.

CLASS: for a CPU-backend leg the playback pace probe never aborts the run (docs/dual-venue-evidence.md: CPU frame rate is informational and never gates);
the run still plays at least 20 s of real source (never looping) inside a wall-time budget that is extended for CPU legs and bounded; the measured pace is
recorded; CUDA legs and the interactive app are unchanged.
Source: VENUE-OWNER-LEGS-UM-2 r2 (the UM CPU leg ended PLAY_PACE_TOO_SLOW after 8 s, 34 of 600 source frames, under host load). Keys and shapes only:
no footage path, name or frame is read or written here.

  1. the app: platform/qt/PlaybackFrameRange.h PlayPaceMode (executed by tests/console/test_playback_frame_range.cpp; here: the constants the launchers mirror);
  2. the launcher: Get-GuiSmokePlaySafetyMs -CpuPaceInformational is the C++ formula; the runner's -CpuPlayPaceInformational is the ONLY way the app's
     MLVAPP_PLAY_PACE_MODE=informational is set (an -ExtraEnvironment or inherited value is refused, an ambient one is stripped);
  3. the job: only the CPU variant passes the switch, its smoke / PresentMon / um-run timeouts are derived from the same ceiling, and its success summary
     records cpuPaceInformational and the measured cpuPaceTimelineFps; a CUDA job is byte-identical to master's.

Every rule has a mutation test (the checks are pure functions of the file text, so a mutant of the text must turn a check red).
"""

from __future__ import annotations

import difflib
import math
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.repo_hygiene.synthetic_mlv import FRAMES_30S_AT_23976, write_synthetic_mlv
from tools.repo_hygiene.test_dual_venue_evidence import (
    FIXTURE_IDS,
    GENERATOR,
    LAUNCHER,
    MLV_EXT,
    ROOT,
    lf,
    requires_windows_pwsh,
    run_pwsh,
)

HEADER = ROOT / "platform" / "qt" / "PlaybackFrameRange.h"
GATE = ROOT / "tools" / "profiling" / "gui-smoke-length-gate.ps1"
MAIN_WINDOW = ROOT / "platform" / "qt" / "MainWindow.cpp"
# master immediately before this card (the CUDA job it emits is the byte-identity baseline of this card).
PRIOR_MASTER = "b575192806246f845dc8e353259d791bed3b8fbe"


def _q(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def header_constants(text: str) -> dict:
    return {
        "cpu_fraction": eval(re.search(r"constexpr double kCpuInformationalMinPaceFraction = ([0-9. /]+);", text).group(1)),  # noqa: S307 - "1.0 / 30.0" from our own header
        "gated_fraction": float(re.search(r"constexpr double kMinSustainedPaceFraction = ([0-9.]+);", text).group(1)),
        "margin_ms": int(re.search(r"constexpr int kPlaySafetyMarginMs = (\d+);", text).group(1)),
    }


def expected_budget_ms(seconds: float, fraction: float, margin_ms: int) -> int:
    return int(math.ceil(seconds * 1000.0 / fraction)) + margin_ms


# --- the pure checks (each is mutation-tested below) -----------------------------------------------------------------------------------------------
def check_gate_text(text: str) -> list[str]:
    problems = []
    if "$script:GuiSmokeCpuInformationalMinPaceFraction = 1.0 / 30.0" not in text:
        problems.append("the gate does not mirror the C++ CPU ceiling fraction")
    if "param([Parameter(Mandatory = $true)][double]$Seconds, [switch]$CpuPaceInformational)" not in text:
        problems.append("Get-GuiSmokePlaySafetyMs has no -CpuPaceInformational switch")
    if "$fraction = if ($CpuPaceInformational) { $script:GuiSmokeCpuInformationalMinPaceFraction } else { $script:GuiSmokeMinSustainedPaceFraction }" not in text:
        problems.append("the CPU ceiling is not selected by the switch alone")
    if "if ($pairKey -eq $script:GuiSmokePaceModeEnvironment) {" not in text:
        problems.append("an -ExtraEnvironment pace-mode variable is not refused")
    if "if (([string]$name).ToUpperInvariant() -eq $script:GuiSmokePaceModeEnvironment) {" not in text:
        problems.append("an inherited pace-mode variable is not refused")
    return problems


def check_runner_text(text: str) -> list[str]:
    problems = []
    if "[switch]$CpuPlayPaceInformational," not in text:
        problems.append("the runner has no -CpuPlayPaceInformational switch")
    if '$launchEnv[$script:GuiSmokePaceModeEnvironment] = "informational"' not in text:
        problems.append("the switch does not set the pace mode in the reported launch environment")
    if "[void]$envBlock.Remove($script:GuiSmokePaceModeEnvironment)" not in text:
        problems.append("an ambient pace-mode variable is not stripped from the app's environment")
    if 'if ($CpuPlayPaceInformational) { $envBlock[$script:GuiSmokePaceModeEnvironment] = "informational" }' not in text:
        problems.append("the switch does not set the pace mode in the app's environment")
    if "(Get-GuiSmokePlaySafetyMs -Seconds $Seconds -CpuPaceInformational:$CpuPlayPaceInformational) +" not in text:
        problems.append("the derived process timeout ignores the CPU ceiling")
    if "cpuPaceInformational = [bool]$CpuPlayPaceInformational" not in text:
        problems.append("the result does not record the pace mode")
    # the mode is never sourced from anything but the switch
    if len(re.findall(r'\$(?:launchEnv|envBlock)\[\$script:GuiSmokePaceModeEnvironment\] = ', text)) != 2:
        problems.append("the pace-mode variable is assigned in a place other than the switch's two env blocks")
    return problems


def check_generator_text(text: str) -> list[str]:
    problems = []
    if "$runnerAcceptsCpuPlayPaceInformational = ($LASTEXITCODE -eq 0) -and" not in text or r"'\[switch\]\$CpuPlayPaceInformational\b'" not in text:
        problems.append("the switch is passed without asking the runner committed at -SourceCommit whether it takes it")
    if "$cpuPlayPaceInformational = ($Backend -eq 'cpu') -and $runnerAcceptsCpuPlayPaceInformational" not in text:
        problems.append("the switch is not CPU-only")
    if "$timeBudgetArgs['PlaySeconds'] = [int][Math]::Max($timeBudgetArgs['PlaySeconds']," not in text \
            or "(Get-GuiSmokePlaySafetyMs -Seconds $PlaySeconds -CpuPaceInformational) / 1000.0" not in text:
        problems.append("the smoke / um-run timeouts are not derived from the runner's CPU ceiling")
    if "if ($cpuPlayPaceInformational) {\n    $timeBudgetArgs" not in text:
        problems.append("the extended budget is not CPU-only")
    if "$cpuPaceSwitch = if ($cpuPlayPaceInformational) { ' -CpuPlayPaceInformational' } else { '' }" not in text:
        problems.append("the CPU variant does not pass the switch")
    if "'    cpuPaceTimelineFps = $resultJson.playbackFps.smokeTimelineFps'" not in text:
        problems.append("the CPU summary does not record the measured pace")
    if "'    cpuPaceInformational = '" not in text:
        problems.append("the CPU summary does not record the pace mode")
    return problems


def check_main_window_text(text: str) -> list[str]:
    problems = []
    if 'qgetenv( "MLVAPP_PLAY_PACE_MODE" )' not in text:
        problems.append("the app does not read the pace mode")
    if text.count("playback_frame_range::playSafetyMs( m_playRequestedSeconds, automationPlayPaceMode() )") != 4:
        problems.append("a safety-net budget ignores the pace mode")
    if "elapsedMs, safetyMs, automationPlayPaceMode() );" not in text:
        problems.append("the stop decision ignores the pace mode")
    if "enginePaceFps > 0.0 ? enginePaceFps : -1.0, automationPlayPaceMode() );" not in text:
        problems.append("the admission ignores the pace mode")
    return problems


class CpuPaceLaunchers(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.gate = GATE.read_text(encoding="utf-8")
        cls.runner = LAUNCHER.read_text(encoding="utf-8")
        cls.generator = lf(GENERATOR.read_text(encoding="utf-8"))
        cls.main_window = MAIN_WINDOW.read_text(encoding="utf-8")
        cls.header = header_constants(HEADER.read_text(encoding="utf-8"))

    def test_the_header_states_the_ceiling_the_card_documents(self) -> None:
        self.assertAlmostEqual(self.header["cpu_fraction"], 1.0 / 30.0)
        self.assertEqual(expected_budget_ms(25, self.header["cpu_fraction"], self.header["margin_ms"]), 765000)   # 12.75 min
        self.assertEqual(expected_budget_ms(25, self.header["gated_fraction"], self.header["margin_ms"]), 65000)  # the gated budget is unchanged

    def test_the_powershell_gate_mirrors_the_cpp_cpu_ceiling(self) -> None:
        self.assertIn(f"$script:GuiSmokeCpuInformationalMinPaceFraction = {'1.0 / 30.0'}", self.gate)
        self.assertEqual(check_gate_text(self.gate), [])

    @requires_windows_pwsh
    def test_the_powershell_budget_is_the_formula_the_app_uses_in_both_modes(self) -> None:
        for seconds in (20, 25, 40):
            gated = run_pwsh(["-Command", f". {_q(GATE)}; Get-GuiSmokePlaySafetyMs -Seconds {seconds}"])
            cpu = run_pwsh(["-Command", f". {_q(GATE)}; Get-GuiSmokePlaySafetyMs -Seconds {seconds} -CpuPaceInformational"])
            self.assertEqual(int(gated.stdout.strip()), expected_budget_ms(seconds, self.header["gated_fraction"], self.header["margin_ms"]), gated.stderr)
            self.assertEqual(int(cpu.stdout.strip()), expected_budget_ms(seconds, self.header["cpu_fraction"], self.header["margin_ms"]), cpu.stderr)

    @requires_windows_pwsh
    def test_a_pace_mode_variable_is_refused_as_pass_through_and_when_inherited(self) -> None:
        extra = run_pwsh(["-Command", f". {_q(GATE)}; (Test-GuiSmokeEnvironmentEntries -Entries @('MLVAPP_PLAY_PACE_MODE=informational')).verdict"])
        self.assertEqual(extra.stdout.strip(), "PASS_THROUGH_REFUSED", extra.stderr)
        ok = run_pwsh(["-Command", f". {_q(GATE)}; (Test-GuiSmokeEnvironmentEntries -Entries @('MLVAPP_SOMETHING_ELSE=1')).verdict"])
        self.assertEqual(ok.stdout.strip(), "OK", ok.stderr)
        inherited = run_pwsh(["-Command", f". {_q(GATE)}; (Test-GuiSmokeParentEnvironment -Environment @{{ MLVAPP_PLAY_PACE_MODE = 'informational' }}).verdict"])
        self.assertEqual(inherited.stdout.strip(), "PASS_THROUGH_REFUSED", inherited.stderr)
        clean = run_pwsh(["-Command", f". {_q(GATE)}; (Test-GuiSmokeParentEnvironment -Environment @{{ PATH = 'x' }}).verdict"])
        self.assertEqual(clean.stdout.strip(), "OK", clean.stderr)

    def test_the_runner_arms_the_mode_from_its_switch_alone(self) -> None:
        self.assertEqual(check_runner_text(self.runner), [])

    def test_the_app_reads_the_mode_and_hands_it_to_every_budget_and_decision(self) -> None:
        self.assertEqual(check_main_window_text(self.main_window), [])

    def test_the_generator_passes_the_switch_to_the_cpu_variant_only_and_derives_its_timeouts(self) -> None:
        self.assertEqual(check_generator_text(self.generator), [])

    # --- mutation: every rule above goes red when its text is removed -------------------------------------------------------------------------------
    def _assert_each_mutant_is_caught(self, text: str, check, mutants: dict[str, tuple[str, str]]) -> None:
        for name, (old, new) in mutants.items():
            with self.subTest(name):
                self.assertEqual(text.count(old), 1, f"mutation anchor missing or ambiguous: {name}")
                self.assertTrue(check(text.replace(old, new, 1)), f"the check did not go red on: {name}")

    def test_mutation_gate(self) -> None:
        self._assert_each_mutant_is_caught(self.gate, check_gate_text, {
            "fraction drifts": ("= 1.0 / 30.0", "= 1.0 / 2.0"),
            "the switch is dropped": ("[double]$Seconds, [switch]$CpuPaceInformational)", "[double]$Seconds)"),
            "the ceiling is always used": ("$fraction = if ($CpuPaceInformational) {", "$fraction = if ($true) {"),
            "extra environment passes": ("if ($pairKey -eq $script:GuiSmokePaceModeEnvironment) {", "if ($false) {"),
            "inherited passes": ("if (([string]$name).ToUpperInvariant() -eq $script:GuiSmokePaceModeEnvironment) {", "if ($false) {"),
        })

    def test_mutation_runner(self) -> None:
        self._assert_each_mutant_is_caught(self.runner, check_runner_text, {
            "no switch": ("[switch]$CpuPlayPaceInformational,", "[switch]$CpuPlayPaceInformationalGone,"),
            "launch env not set": ('$launchEnv[$script:GuiSmokePaceModeEnvironment] = "informational"', ""),
            "ambient value not stripped": ("[void]$envBlock.Remove($script:GuiSmokePaceModeEnvironment)", ""),
            "app env not set": ('if ($CpuPlayPaceInformational) { $envBlock[$script:GuiSmokePaceModeEnvironment] = "informational" }', ""),
            "timeout ignores the ceiling": ("-CpuPaceInformational:$CpuPlayPaceInformational) +", ") +"),
            "mode not recorded": ("cpuPaceInformational = [bool]$CpuPlayPaceInformational", "cpuPaceInformational = $false"),
            "mode armed unconditionally": ('if ($CpuPlayPaceInformational) { $envBlock[$script:GuiSmokePaceModeEnvironment] = "informational" }',
                                           '$envBlock[$script:GuiSmokePaceModeEnvironment] = "informational"; $x = 1'),
        })

    def test_mutation_generator(self) -> None:
        self._assert_each_mutant_is_caught(self.generator, check_generator_text, {
            "no runner capability probe": ("$runnerAcceptsCpuPlayPaceInformational = ($LASTEXITCODE -eq 0) -and", "$runnerAcceptsCpuPlayPaceInformational = $true -and"),
            "not cpu only": ("$cpuPlayPaceInformational = ($Backend -eq 'cpu') -and", "$cpuPlayPaceInformational = $true -and"),
            "budget not derived": ("(Get-GuiSmokePlaySafetyMs -Seconds $PlaySeconds -CpuPaceInformational) / 1000.0", "765.0"),
            "budget for everyone": ("if ($cpuPlayPaceInformational) {\n    $timeBudgetArgs", "if ($true) {\n    $timeBudgetArgs"),
            "switch not passed": ("{ ' -CpuPlayPaceInformational' } else", "{ '' } else"),
            "pace not recorded": ("'    cpuPaceTimelineFps = $resultJson.playbackFps.smokeTimelineFps'", "''"),
            "mode not recorded": ("'    cpuPaceInformational = '", "''"),
        })


@requires_windows_pwsh
class CpuPaceGeneratedJobs(unittest.TestCase):
    """Runs the real generator against a sparse clone whose two fixture files are header-only ~30 s stand-ins (never real footage), the device the sibling
    generator suite uses. The clone is checked out at HEAD, so the runner it reads for the capability probe is this branch's."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="cpu-pace-gen-")
        cls.tmp = Path(cls._tmp.name)
        cls.head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        cls.repo = cls.tmp / "repo"
        subprocess.run(["git", "clone", "-q", "--shared", "--no-checkout", str(ROOT), str(cls.repo)], check=True)
        subprocess.run(["git", "-C", str(cls.repo), "sparse-checkout", "set", "--cone", "tools", "tests/fixtures/clips"], check=True)
        subprocess.run(["git", "-C", str(cls.repo), "checkout", "-q", cls.head], check=True)
        for stem in FIXTURE_IDS:
            write_synthetic_mlv(cls.repo / "tests" / "fixtures" / "clips" / (stem + MLV_EXT), FRAMES_30S_AT_23976)
        cls.prior_available = subprocess.run(["git", "-C", str(ROOT), "cat-file", "-e", f"{PRIOR_MASTER}^{{commit}}"], capture_output=True).returncode == 0
        cls.prior_root = cls.tmp / "prior"
        if cls.prior_available:
            tar = cls.tmp / "prior.tar"
            subprocess.run(["git", "-C", str(ROOT), "archive", PRIOR_MASTER, "--format=tar", "-o", str(tar), "tools/profiling", "tools/gates"], check=True)
            cls.prior_root.mkdir()
            subprocess.run(["tar", "-xf", str(tar), "-C", str(cls.prior_root)], check=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def generate(self, script: Path, out_name: str, extra: list[str], source_commit: str | None = None) -> tuple[Path, str]:
        out = self.tmp / out_name
        proc = run_pwsh(["-File", str(script), "-SourceCommit", source_commit or self.head, "-BuildManifestSha256", "ab" * 32,
                         "-ClipId", FIXTURE_IDS[0], "-FixtureSha256", "cd" * 32, "-RepoRoot", str(self.repo), "-OutFile", str(out), *extra])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return out, proc.stdout + proc.stderr

    @staticmethod
    def timeouts(job: Path) -> tuple[int, int]:
        text = job.read_text(encoding="utf-8")
        return (int(re.search(r"(?m)^\$SmokeProcessTimeoutMs = (\d+)", text).group(1)),
                int(re.search(r"(?m)^\$PresentMonTimedSeconds = (\d+)", text).group(1)))

    @staticmethod
    def recommended_job_timeout_sec(stdout: str) -> int:
        return int(re.search(r"recommendedJobTimeoutSec\s*:\s*(\d+)", stdout).group(1))

    def test_a_cpu_job_passes_the_switch_and_records_the_pace(self) -> None:
        job, _out = self.generate(GENERATOR, "cpu.job.ps1", ["-Backend", "cpu"])
        text = job.read_text(encoding="utf-8")
        self.assertIn(" -MaxSkippedOrUnpresentedRatio 1 -CpuPlayPaceInformational -FrameTelemetry", text)
        self.assertIn("cpuPaceInformational = $true", text)
        self.assertIn("cpuPaceTimelineFps = $resultJson.playbackFps.smokeTimelineFps", text)

    def test_a_cuda_job_has_neither_the_switch_nor_the_fields(self) -> None:
        for name, extra in (("cuda", []), ("cuda-look", ["-ForceLookAssist", "-ContactSheet"]), ("um-cuda", ["-Venue", "ultra-magnus"])):
            job, _out = self.generate(GENERATOR, f"{name}.job.ps1", extra)
            text = job.read_text(encoding="utf-8")
            self.assertNotIn("CpuPlayPaceInformational", text, name)
            self.assertNotIn("cpuPaceTimelineFps", text, name)
            self.assertNotIn("cpuPaceInformational", text, name)

    def test_the_cpu_timeouts_cover_the_cpu_ceiling_and_derive_from_it_alone(self) -> None:
        gate = header_constants(HEADER.read_text(encoding="utf-8"))
        ceiling_sec = expected_budget_ms(25, gate["cpu_fraction"], gate["margin_ms"]) // 1000          # 765 s
        job, out = self.generate(GENERATOR, "cpu.job.ps1", ["-Backend", "cpu"])
        smoke_ms, presentmon_sec = self.timeouts(job)
        self.assertGreaterEqual(smoke_ms, ceiling_sec * 1000, "the smoke process timeout must cover the CPU Play ceiling")
        self.assertEqual(smoke_ms, (0 + 60 + ceiling_sec + 3 + 30) * 1000, "launch + ceiling + settle + runner slack (a fixture has no identity read)")
        self.assertGreaterEqual(presentmon_sec * 1000, smoke_ms, "the PresentMon capture ceiling follows the smoke timeout")
        self.assertGreaterEqual(self.recommended_job_timeout_sec(out), smoke_ms // 1000, "the um-run -TimeoutSec covers the smoke timeout")
        # the CUDA job's budget is exactly what it was (60 + 40 + 3 + 30)
        cuda_job, cuda_out = self.generate(GENERATOR, "cuda.job.ps1", [])
        self.assertEqual(self.timeouts(cuda_job)[0], 133000)

    def test_a_cuda_job_is_byte_identical_to_the_pre_card_generators(self) -> None:
        if not self.prior_available:
            self.skipTest(f"{PRIOR_MASTER[:12]} is not in this clone")
        prior = self.prior_root / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-job.ps1"
        for name, extra in (("default", []), ("look", ["-ForceLookAssist", "-ContactSheet"]), ("um", ["-Venue", "ultra-magnus"]), ("play30", ["-PlaySeconds", "30"])):
            new, _ = self.generate(GENERATOR, f"new-{name}.job.ps1", extra)
            old, _ = self.generate(prior, f"old-{name}.job.ps1", extra)
            self.assertEqual(new.read_bytes(), old.read_bytes(), f"the {name} CUDA job changed")

    def test_only_the_cpu_variant_changes_and_only_in_the_card_s_lines(self) -> None:
        if not self.prior_available:
            self.skipTest(f"{PRIOR_MASTER[:12]} is not in this clone")
        prior = self.prior_root / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-job.ps1"
        for name, extra in (("cpu", ["-Backend", "cpu"]), ("um-cpu-look", ["-Backend", "cpu", "-Venue", "ultra-magnus", "-ForceLookAssist", "-ContactSheet"])):
            new, _ = self.generate(GENERATOR, f"new-{name}.job.ps1", extra)
            old, _ = self.generate(prior, f"old-{name}.job.ps1", extra)
            changed = [line for line in difflib.unified_diff(lf(old.read_text(encoding="utf-8")).split("\n"), lf(new.read_text(encoding="utf-8")).split("\n"), lineterm="", n=0)
                       if line[:1] in "+-" and not line.startswith(("+++", "---"))]
            kinds = {"switch": 0, "mode": 0, "pace": 0, "smoke_timeout": 0, "presentmon_timeout": 0}
            for line in changed:
                body = line[1:]
                if "-CpuPlayPaceInformational" in body or body.startswith("$cmd = "):
                    kinds["switch"] += 1
                elif "cpuPaceInformational" in body:
                    kinds["mode"] += 1
                elif "cpuPaceTimelineFps" in body:
                    kinds["pace"] += 1
                elif body.startswith("$SmokeProcessTimeoutMs = "):
                    kinds["smoke_timeout"] += 1
                elif body.startswith("$PresentMonTimedSeconds = "):
                    kinds["presentmon_timeout"] += 1
                elif "smokeProcessTimeoutMs=" in body:
                    kinds.setdefault("trace", 0)
                    kinds["trace"] += 1
                else:
                    self.fail(f"{name}: an unexpected changed line in the CPU job: {line[:160]}")
            # old line and new line each: the switch line (-/+), the two summary fields (+), the two timeouts (-/+), the trace line (-/+)
            self.assertEqual(kinds["switch"], 2, name)
            self.assertEqual(kinds["mode"], 1, name)
            self.assertEqual(kinds["pace"], 1, name)
            self.assertEqual(kinds["smoke_timeout"], 2, name)
            self.assertEqual(kinds["presentmon_timeout"], 2, name)

    def test_a_runner_without_the_switch_keeps_the_gated_behaviour_with_a_warning(self) -> None:
        # a SourceCommit whose committed runner predates the switch (committed in the clone, never in the real repo)
        runner = self.repo / "tools" / "profiling" / "run-release-gui-smoke.ps1"
        original = runner.read_bytes()
        try:
            runner.write_bytes(original.replace(b"CpuPlayPaceInformational", b"CpuPlayPaceGone"))
            subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "predates the switch", "--", "tools/profiling/run-release-gui-smoke.ps1"], check=True)
            old_head = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
            job, out = self.generate(GENERATOR, "cpu-old-runner.job.ps1", ["-Backend", "cpu"], source_commit=old_head)
            text = job.read_text(encoding="utf-8")
            self.assertNotIn("-CpuPlayPaceInformational", text)
            self.assertIn("cpuPaceInformational = $false", text)
            self.assertIn("does not take -CpuPlayPaceInformational", out)
            self.assertEqual(self.timeouts(job)[0], 133000, "no switch, no extended budget")
        finally:
            subprocess.run(["git", "-C", str(self.repo), "reset", "-q", "--soft", self.head], check=True)
            subprocess.run(["git", "-C", str(self.repo), "checkout", "-q", self.head, "--", "tools/profiling/run-release-gui-smoke.ps1"], check=True)


if __name__ == "__main__":
    unittest.main()
