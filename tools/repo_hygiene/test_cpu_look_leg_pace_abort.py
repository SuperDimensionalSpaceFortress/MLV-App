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
     records cpuPaceInformational and the measured cpuPaceTimelineFps; a CUDA job carries none of it. That last guarantee is a STRUCTURAL invariant of the
     CURRENT generator (CPU_PACE_MARKERS absent from every CUDA variant; the pace mode is the only delta between two CPU jobs of the same arguments), never
     a snapshot of a past master (CPU-PACE-BYTE-IDENTITY-BASELINE-1: a frozen snapshot blocked every later legitimate change to the job generator).

Every rule has a mutation test (the checks are pure functions of the file text, so a mutant of the text must turn a check red).
"""

from __future__ import annotations

import difflib
import math
import re
import shutil
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
# Everything the CPU-informational mode writes into a generated job. A CUDA job (any arguments) must contain none of it.
CPU_PACE_MARKERS = ("-CpuPlayPaceInformational", "MLVAPP_PLAY_PACE_MODE", "cpuPaceInformational", "cpuPaceTimelineFps")
# The CUDA argument sets the invariants run over (the CPU variants are the same arguments plus -Backend cpu).
CUDA_VARIANTS = (
    ("default", []),
    ("look", ["-ForceLookAssist", "-ContactSheet"]),
    ("um", ["-Venue", "ultra-magnus"]),
    ("play30", ["-PlaySeconds", "30"]),
)


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


def job_timeouts(text: str) -> tuple[int, int]:
    """(SmokeProcessTimeoutMs, PresentMonTimedSeconds) as a generated job declares them."""
    return (int(re.search(r"(?m)^\$SmokeProcessTimeoutMs = (\d+)", text).group(1)),
            int(re.search(r"(?m)^\$PresentMonTimedSeconds = (\d+)", text).group(1)))


def check_cuda_job_text(text: str, cpu_ceiling_sec: int) -> list[str]:
    """The structural form of 'CUDA legs are unaffected by the CPU-informational mode': a property of the job text itself, not of any past generator."""
    problems = [f"a CUDA job carries the CPU-informational marker {marker}" for marker in CPU_PACE_MARKERS if marker in text]
    smoke_ms, presentmon_sec = job_timeouts(text)
    if smoke_ms >= cpu_ceiling_sec * 1000:
        problems.append(f"a CUDA job's smoke timeout ({smoke_ms} ms) carries the extended CPU Play ceiling ({cpu_ceiling_sec} s)")
    if presentmon_sec >= cpu_ceiling_sec:
        problems.append(f"a CUDA job's PresentMon capture ({presentmon_sec} s) carries the extended CPU Play ceiling ({cpu_ceiling_sec} s)")
    return problems


def normalise_identity(text: str, *commits: str) -> str:
    """Blank the lines that are a pure function of -SourceCommit (names, closure hash, the runner's own sha256), so two jobs generated at two commits compare."""
    for commit in commits:
        text = text.replace(commit, "<COMMIT>").replace(commit[:12], "<COMMIT12>")
    text = re.sub(r"smoke-runner-[0-9a-f]{16}", "smoke-runner-<HASH>", text)
    return re.sub(r"(name = 'run-release-gui-smoke\.ps1'; sha256 = ')[0-9a-f]{64}", r"\1<SHA256>", text)


def changed_lines(before: str, after: str) -> list[str]:
    return [line for line in difflib.unified_diff(lf(before).split("\n"), lf(after).split("\n"), lineterm="", n=0)
            if line[:1] in "+-" and not line.startswith(("+++", "---"))]


def check_pace_mode_delta(without_switch: str, with_switch: str) -> list[str]:
    """`without_switch` and `with_switch` are the SAME arguments and backend, generated at a commit whose runner lacks / has the switch. The documented allow-list
    is exactly: the smoke command line gains the switch; the two timeouts grow to the CPU ceiling; the mode field flips. Anything else that differs is a problem."""
    problems = []
    old = [line[1:] for line in changed_lines(without_switch, with_switch) if line[0] == "-"]
    new = [line[1:] for line in changed_lines(without_switch, with_switch) if line[0] == "+"]
    if len(old) != len(new):
        return [f"the pace mode changed {len(old)} lines out and {len(new)} in; each change must be a one-for-one replacement"]
    for before, after in zip(old, new):
        if before.startswith("$cmd = "):
            if after.replace(" -CpuPlayPaceInformational", "", 1) != before or after == before:
                problems.append("the smoke command line differs by more than the one switch")
        elif before.strip() == "cpuPaceInformational = $false":
            if after.strip() != "cpuPaceInformational = $true":
                problems.append(f"the mode field became {after.strip()[:80]}")
        elif before.startswith("$SmokeProcessTimeoutMs = ") or before.startswith("$PresentMonTimedSeconds = "):
            name = before.split(" = ")[0]
            if not after.startswith(name + " = ") or int(after.split(" = ")[1]) <= int(before.split(" = ")[1]):
                problems.append(f"{name} did not grow to the CPU ceiling")
        else:
            problems.append(f"an unexpected changed line: {before[:100]!r} -> {after[:100]!r}")
    seen = {("cmd" if x.startswith("$cmd = ") else x.split(" = ")[0].strip()) for x in old}
    for needed in ("cmd", "cpuPaceInformational", "$SmokeProcessTimeoutMs", "$PresentMonTimedSeconds"):
        if needed not in seen:
            problems.append(f"the pace mode did not change {needed}")
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


class CpuPaceJobInvariantMutations(unittest.TestCase):
    """The two job-text invariants are pure functions of job text, so a mutant of a (synthetic) job must turn each one red."""

    CUDA_JOB = "$Backend = 'cuda'\n$PresentMonTimedSeconds = 133\n$SmokeProcessTimeoutMs = 133000\n$cmd = \"& smoke -MaxSkippedOrUnpresentedRatio 1 -FrameTelemetry\"\n"
    BEFORE = ("$Backend = 'cpu'\n$PresentMonTimedSeconds = 133\n$SmokeProcessTimeoutMs = 133000\n"
              "$cmd = \"& smoke -MaxSkippedOrUnpresentedRatio 1 -FrameTelemetry\"\n    cpuPaceInformational = $false\n")
    AFTER = ("$Backend = 'cpu'\n$PresentMonTimedSeconds = 858\n$SmokeProcessTimeoutMs = 858000\n"
             "$cmd = \"& smoke -MaxSkippedOrUnpresentedRatio 1 -CpuPlayPaceInformational -FrameTelemetry\"\n    cpuPaceInformational = $true\n")

    def test_the_synthetic_jobs_are_clean(self) -> None:
        self.assertEqual(check_cuda_job_text(self.CUDA_JOB, 765), [])
        self.assertEqual(check_pace_mode_delta(self.BEFORE, self.AFTER), [])

    def test_a_cuda_job_that_gains_any_marker_or_the_extended_budget_is_caught(self) -> None:
        for marker in CPU_PACE_MARKERS:
            with self.subTest(marker):
                self.assertTrue(check_cuda_job_text(self.CUDA_JOB + f"# {marker}\n", 765))
        self.assertTrue(check_cuda_job_text(self.CUDA_JOB.replace("133000", "858000"), 765), "the extended smoke timeout")
        self.assertTrue(check_cuda_job_text(self.CUDA_JOB.replace("= 133\n", "= 858\n"), 765), "the extended PresentMon capture")

    def test_a_pace_mode_that_changes_anything_else_is_caught(self) -> None:
        mutants = {
            "an unrelated line changes": (self.AFTER + "$Extra = 1\n", self.BEFORE + "$Extra = 0\n"),
            "the command changes beyond the switch": (self.AFTER.replace("-FrameTelemetry", "-FrameTelemetry -Other"), self.BEFORE),
            "the timeouts do not grow": (self.AFTER.replace("858000", "133000").replace("= 858\n", "= 133\n"), self.BEFORE),
            "the switch is not passed": (self.AFTER.replace(" -CpuPlayPaceInformational", ""), self.BEFORE),
            "the mode is not recorded": (self.AFTER.replace("cpuPaceInformational = $true", "cpuPaceInformational = $false"), self.BEFORE),
        }
        for name, (after, before) in mutants.items():
            with self.subTest(name):
                self.assertTrue(check_pace_mode_delta(before, after))

    def test_identity_lines_are_blanked_but_nothing_else(self) -> None:
        a, b = "a" * 40, "b" * 40
        first = f"$S = '{a}'\n$E = 'x-{a[:12]}.exe'\ndir smoke-runner-{'1' * 16}\n[pscustomobject]@{{ name = 'run-release-gui-smoke.ps1'; sha256 = '{'2' * 64}' }}\n$T = 1\n"
        second = f"$S = '{b}'\n$E = 'x-{b[:12]}.exe'\ndir smoke-runner-{'3' * 16}\n[pscustomobject]@{{ name = 'run-release-gui-smoke.ps1'; sha256 = '{'4' * 64}' }}\n$T = 1\n"
        self.assertEqual(normalise_identity(first, a), normalise_identity(second, b))
        self.assertNotEqual(normalise_identity(first, a), normalise_identity(second.replace("$T = 1", "$T = 2"), b))


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
        return job_timeouts(job.read_text(encoding="utf-8"))

    @staticmethod
    def cpu_ceiling_sec() -> int:
        gate = header_constants(HEADER.read_text(encoding="utf-8"))
        return expected_budget_ms(25, gate["cpu_fraction"], gate["margin_ms"]) // 1000          # 765 s

    def generate_with_runner_lacking_the_switch(self, out_name: str, extra: list[str]) -> tuple[Path, str, str]:
        """Generates at a SourceCommit whose committed runner predates the switch (committed in the clone, never in the real repo); returns (job, output, that commit)."""
        runner = self.repo / "tools" / "profiling" / "run-release-gui-smoke.ps1"
        original = runner.read_bytes()
        try:
            runner.write_bytes(original.replace(b"CpuPlayPaceInformational", b"CpuPlayPaceGone"))
            subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "predates the switch", "--", "tools/profiling/run-release-gui-smoke.ps1"], check=True)
            old_head = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
            job, out = self.generate(GENERATOR, out_name, extra, source_commit=old_head)
            return job, out, old_head
        finally:
            subprocess.run(["git", "-C", str(self.repo), "reset", "-q", "--soft", self.head], check=True)
            subprocess.run(["git", "-C", str(self.repo), "checkout", "-q", self.head, "--", "tools/profiling/run-release-gui-smoke.ps1"], check=True)

    def recommended_job_timeout_sec(self, extra: list[str], out_name: str) -> int:
        # the generator's result object, asked for the one property by name (never parsed out of its console formatting)
        out = self.tmp / out_name
        args = " ".join(x if re.fullmatch(r"-[A-Za-z0-9]+", x) else "'" + x.replace("'", "''") + "'" for x in [
            "-SourceCommit", self.head, "-BuildManifestSha256", "ab" * 32, "-ClipId", FIXTURE_IDS[0], "-FixtureSha256", "cd" * 32, "-RepoRoot", str(self.repo),
            "-OutFile", str(out), *extra])
        proc = run_pwsh(["-Command", f"& {_q(GENERATOR)} {args} | Select-Object -ExpandProperty recommendedJobTimeoutSec"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return int(proc.stdout.strip().splitlines()[-1])

    def test_a_cpu_job_passes_the_switch_and_records_the_pace(self) -> None:
        job, _out = self.generate(GENERATOR, "cpu.job.ps1", ["-Backend", "cpu"])
        text = job.read_text(encoding="utf-8")
        self.assertIn(" -MaxSkippedOrUnpresentedRatio 1 -CpuPlayPaceInformational -FrameTelemetry", text)
        self.assertIn("cpuPaceInformational = $true", text)
        self.assertIn("cpuPaceTimelineFps = $resultJson.playbackFps.smokeTimelineFps", text)

    def test_a_cuda_job_has_none_of_the_cpu_informational_mode(self) -> None:
        # CLASS: CUDA legs are unaffected by the CPU-informational mode -- a property of the current generator's CUDA output, whatever else it emits.
        for name, extra in CUDA_VARIANTS:
            job, _out = self.generate(GENERATOR, f"cuda-{name}.job.ps1", extra)
            self.assertEqual(check_cuda_job_text(job.read_text(encoding="utf-8"), self.cpu_ceiling_sec()), [], name)

    def test_the_cpu_timeouts_cover_the_cpu_ceiling_and_derive_from_it_alone(self) -> None:
        ceiling_sec = self.cpu_ceiling_sec()
        job, out = self.generate(GENERATOR, "cpu.job.ps1", ["-Backend", "cpu"])
        smoke_ms, presentmon_sec = self.timeouts(job)
        self.assertGreaterEqual(smoke_ms, ceiling_sec * 1000, "the smoke process timeout must cover the CPU Play ceiling")
        self.assertEqual(smoke_ms, (0 + 60 + ceiling_sec + 3 + 30) * 1000, "launch + ceiling + settle + runner slack (a fixture has no identity read)")
        self.assertGreaterEqual(presentmon_sec * 1000, smoke_ms, "the PresentMon capture ceiling follows the smoke timeout")
        self.assertGreaterEqual(self.recommended_job_timeout_sec(["-Backend", "cpu"], "cpu-timeout.job.ps1"), smoke_ms // 1000, "the um-run -TimeoutSec covers the smoke timeout")
        # a CUDA job's budget is never the extended one
        cuda_job, cuda_out = self.generate(GENERATOR, "cuda.job.ps1", [])
        self.assertLess(self.timeouts(cuda_job)[0], ceiling_sec * 1000)

    def test_mutation_a_generator_that_hands_the_switch_to_a_cuda_job_is_caught(self) -> None:
        # the mutant generator (a copy of the real one in a copy of its tools tree) arms the mode for every backend; the invariant must go red on its CUDA job
        mutant_root = self.tmp / "mutant"
        shutil.copytree(ROOT / "tools" / "profiling", mutant_root / "tools" / "profiling")
        shutil.copytree(ROOT / "tools" / "gates", mutant_root / "tools" / "gates")
        mutant = mutant_root / "tools" / "profiling" / "bachelor" / GENERATOR.name
        text = mutant.read_bytes().decode("utf-8")
        old = "$cpuPlayPaceInformational = ($Backend -eq 'cpu') -and $runnerAcceptsCpuPlayPaceInformational"
        self.assertEqual(text.count(old), 1, "mutation anchor missing or ambiguous")
        mutant.write_bytes(text.replace(old, "$cpuPlayPaceInformational = $runnerAcceptsCpuPlayPaceInformational", 1).encode("utf-8"))
        job, _out = self.generate(mutant, "mutant-cuda.job.ps1", [])
        problems = check_cuda_job_text(job.read_text(encoding="utf-8"), self.cpu_ceiling_sec())
        self.assertTrue(problems, "the CUDA invariant did not go red when a CUDA job gained the switch")
        # the generator emits the switch and fields only inside its cpu-backend edit, so what leaks into a CUDA job is the extended budget
        self.assertTrue(any("extended CPU Play ceiling" in p for p in problems), problems)

    def test_the_pace_mode_is_the_only_delta_between_two_cpu_jobs_of_the_same_arguments(self) -> None:
        # The card's lines are found by diffing two CURRENT outputs: the same CPU arguments generated at a SourceCommit whose runner lacks the switch, and at HEAD
        # (which has it). Backend-dependent lines are identical in both, so the allow-list is exactly the card's lines and needs no upkeep when other cards edit them.
        for name, extra in (("cpu", ["-Backend", "cpu"]),
                            ("um-cpu-look", ["-Backend", "cpu", "-Venue", "ultra-magnus", "-ForceLookAssist", "-ContactSheet"])):
            without, _out, old_head = self.generate_with_runner_lacking_the_switch(f"without-{name}.job.ps1", extra)
            with_switch, _out = self.generate(GENERATOR, f"with-{name}.job.ps1", extra)
            self.assertEqual(check_pace_mode_delta(normalise_identity(without.read_text(encoding="utf-8"), old_head),
                                                   normalise_identity(with_switch.read_text(encoding="utf-8"), self.head)), [], name)

    def test_a_runner_without_the_switch_keeps_the_gated_behaviour_with_a_warning(self) -> None:
        job, out, _old_head = self.generate_with_runner_lacking_the_switch("cpu-old-runner.job.ps1", ["-Backend", "cpu"])
        text = job.read_text(encoding="utf-8")
        self.assertNotIn("-CpuPlayPaceInformational", text)
        self.assertIn("cpuPaceInformational = $false", text)
        self.assertIn("does not take -CpuPlayPaceInformational", out)
        self.assertLess(self.timeouts(job)[0], self.cpu_ceiling_sec() * 1000, "no switch, no extended budget")


if __name__ == "__main__":
    unittest.main()
