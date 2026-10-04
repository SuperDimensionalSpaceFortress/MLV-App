"""Tests for CPU-LEG-SMOKE-CEILING-1 (round 1): an owner CPU leg fits its time budget.

CLASS: every owner leg the committed specs describe (M16-1243, about 3.2 GB, on bachelor and ultra-magnus, cuda and cpu) can be GENERATED and stays BOUNDED;
every timeout is derived once; no budget is silently loosened for CUDA or for the interactive app.
Source: VENUE-OWNER-LEGS-UM-3 r1 (the owner CPU leg was refused at generation, ATTRCUDA_TIMEBUDGET_EXCEEDS_SMOKE_CEILING: 3239 s identity read + 60 + 765 s CPU play
+ 3 + 30 = 4097 s > 3600 s). Sizes and keys only: no footage path, name or frame is read or written here.

THE FIX (generator side only, no runner or app edit, so the staged build and smoke-runner closure stay valid): the smoke runner caps a process timeout at 3600 s
and that cap is not ours to raise. The smoke timeout is  [the app's own re-read of the clip] + launch + play + settle + slack. The job's FULL margined identity read
(inputMB / ColdReadMBps x 1.5) happens BEFORE the runner and is budgeted separately in the um-run timeout, so it never needed to be repeated at full margin inside the
runner's 3600 s. A CPU-informational leg now lets the in-runner re-read allowance shrink to whatever is left of the 3600 s once its own fixed parts (launch + the CPU
play ceiling + settle + slack) are paid, and refuses (same typed token) only when that remainder cannot cover the re-read at the measured rate with NO margin.
CUDA, fixtures and the interactive app are untouched.

  1. the derivation: Get-AttrCudaLegTimeBudget -ShareSmokeCeiling (EXECUTED in pwsh) -- 3238 MB fits and stays at the 3600 s ceiling, an input over the new cap refuses;
  2. the generator passes the switch for the CPU-informational variant ONLY (text, mutation-tested), and the job traces smokeCeilingClamped / appReadAllowanceSec;
  3. "CUDA is untouched" is a set of STRUCTURAL invariants of the CURRENT generator, never a snapshot of a past master (a frozen snapshot blocks every later
     legitimate change to the job template; CPU-PACE-BYTE-IDENTITY-BASELINE-1 removed the sibling pin for exactly that reason):
       (a) every CUDA variant's budget is the UNSHARED derivation for its input (smoke timeout, PresentMon capture and traced allowance equal Get-AttrCudaLegTimeBudget
           asked without -ShareSmokeCeiling), traces smokeCeilingClamped false and carries no CPU-informational marker;
       (b) a fixture CPU job is identical to itself with and without the clamp path (a fixture has no input to share a read of);
       (c) the clamp lines (the switch request, the generator warning, a true clamp trace) exist only in the CPU-informational owner variant.

Every rule has a mutation test.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.repo_hygiene.test_cpu_look_leg_pace_abort import CPU_PACE_MARKERS, CUDA_VARIANTS, expected_budget_ms, header_constants, job_timeouts, HEADER
from tools.repo_hygiene.test_dual_venue_evidence import (
    FIXTURE_IDS,
    GENERATOR,
    MLV_EXT,
    ROOT,
    lf,
    requires_windows_pwsh,
    run_pwsh,
)
from tools.repo_hygiene.synthetic_mlv import FRAMES_30S_AT_23976, write_synthetic_mlv
from tools.repo_hygiene.test_playback_attr_3_cuda_behaviour import MODULE, _PwshCase, requires_pwsh

CLAMP_REQUEST = "$timeBudgetArgs['ShareSmokeCeiling'] = $true"
CLAMP_WARNING = "CPU-LEG-SMOKE-CEILING-1: this cpu leg's smoke timeout is held"
CLAMP_TRACE = "smokeCeilingClamped="                      # what a clamped leg's job first trace line carries and no other job does
CLAMP_TRACE_EXPRESSION = ('SMOKE_CEILING_TRACE = $(if ($timeBudget.smokeCeilingClamped) { " smokeCeilingClamped=True '
                          'appReadAllowanceSec=$([int]$timeBudget.appReadAllowanceSec)" } else { \'\' })')
OWNER_MB = 3238                      # the M16-1243 owner input, rounded (size only)
CPU_PLAY_SEC = 765                    # Get-GuiSmokePlaySafetyMs -CpuPaceInformational for the 25 s window (see test_cpu_look_leg_pace_abort)
FIXED_SMOKE_SEC = 60 + CPU_PLAY_SEC + 3 + 30
SMOKE_CEILING_SEC = 3600
RATE = 1.5                            # AttrCudaMeasuredColdReadMBps (Bachelor)


def _bytes(mb: float) -> int:
    return int(mb * 1048576)


# --- the pure check on the generator text (mutation-tested below) -------------------------------------------------------------------------------
def check_generator_text(text: str) -> list[str]:
    problems = []
    if "$timeBudgetArgs['ShareSmokeCeiling'] = $true" not in text:
        problems.append("the CPU-informational budget does not ask for the shared smoke ceiling")
    if text.count("$timeBudgetArgs['ShareSmokeCeiling']") != 1:
        problems.append("the shared smoke ceiling is requested in more than one place")
    # it sits inside the CPU-informational block and nowhere else
    block = re.search(r"if \(\$cpuPlayPaceInformational\) \{\n(.*?)\n\}\n\$timeBudget = ", text, re.S)
    if block is None or "$timeBudgetArgs['ShareSmokeCeiling'] = $true" not in block.group(1):
        problems.append("the shared smoke ceiling is not inside the CPU-informational block")
    if "$timeBudget = Get-AttrCudaLegTimeBudget -InputBytes $clipBytesForBudget @timeBudgetArgs" not in text:
        problems.append("the budget is not derived once through Get-AttrCudaLegTimeBudget")
    if "SMOKE_PROCESS_TIMEOUT_MS = [string]$timeBudget.smokeProcessTimeoutMs" not in text:
        problems.append("the runner's process timeout is not the derived one")
    if "PRESENTMON_TIMED_SECONDS = [string][int][math]::Ceiling($timeBudget.smokeProcessTimeoutMs / 1000.0)" not in text:
        problems.append("the PresentMon capture ceiling is not derived from the smoke timeout")
    if "recommendedJobTimeoutSec = $timeBudget.jobTimeoutSec" not in text:
        problems.append("the um-run timeout is not the derived one")
    # (c) the clamp lines exist only on the CPU-informational path: the warning fires on the derived flag alone, and the job's trace fields are the derived values
    if text.count(CLAMP_WARNING) != 1 or 'if ($timeBudget.smokeCeilingClamped) {\n    Write-Warning ("' + CLAMP_WARNING not in text:
        problems.append("the clamp warning is not guarded by the derived clamp flag alone")
    if CLAMP_TRACE_EXPRESSION not in text or text.count(CLAMP_TRACE) != 1:
        problems.append("the job's clamp trace is not the derived flag alone (a clamped leg traces it, every other leg expands it to nothing)")
    if 'smokeProcessTimeoutMs=$SmokeProcessTimeoutMs__SMOKE_CEILING_TRACE__"' not in text:
        problems.append("the job's first trace line does not carry the clamp trace token")
    return problems


def check_unshared_job(text: str, output: str, unshared: dict, cpu_markers_allowed: bool) -> list[str]:
    """(a) / (c) on a job NOT clamped by this card: its budget is exactly the unshared derivation for its input (`unshared` is Get-AttrCudaLegTimeBudget asked WITHOUT
    -ShareSmokeCeiling for the same input and play seconds), and no clamp line or (for CUDA) CPU-informational marker leaked into it."""
    problems = []
    smoke_ms, presentmon_sec = job_timeouts(text)
    if smoke_ms != unshared["smokeProcessTimeoutMs"]:
        problems.append(f"smoke timeout {smoke_ms} ms is not the unshared derivation {unshared['smokeProcessTimeoutMs']} ms")
    if presentmon_sec != math.ceil(unshared["smokeProcessTimeoutMs"] / 1000.0):
        problems.append(f"PresentMon capture {presentmon_sec} s is not derived from the unshared smoke timeout")
    if CLAMP_TRACE in text or CLAMP_WARNING in output:
        problems.append("a clamp line leaked into a leg that cannot clamp")
    if not cpu_markers_allowed:
        problems += [f"a CUDA job carries the CPU-informational marker {marker}" for marker in CPU_PACE_MARKERS if marker in text]
    return problems


def check_clamp_neutral(with_path: str, without_path: str) -> list[str]:
    """(b) the same fixture CPU arguments generated with and without the clamp path must be the same bytes: there is no input to share a read of."""
    return [] if lf(with_path) == lf(without_path) else ["a fixture CPU job changed with the clamp path (it has no read to share)"]


def check_module_text(text: str) -> list[str]:
    problems = []
    if "[switch]$ShareSmokeCeiling" not in text:
        problems.append("the budget has no -ShareSmokeCeiling switch")
    if "if ($smokeProcessTimeoutMs -gt 3600000 -and $ShareSmokeCeiling) {" not in text:
        problems.append("the clamp is not behind the switch alone")
    if text.count("ATTRCUDA_TIMEBUDGET_EXCEEDS_SMOKE_CEILING") < 2:
        problems.append("the refusal token is not raised both by the generic ceiling and by the shared ceiling")
    return problems


@requires_pwsh
class SharedSmokeCeilingBudget(_PwshCase):
    """Get-AttrCudaLegTimeBudget: arithmetic on a size, a measured rate and the runner's 3600 s ceiling."""

    def budget(self, args: str, module: Path | None = None) -> dict:
        body = f"Get-AttrCudaLegTimeBudget {args} | ConvertTo-Json -Compress\n"
        if module is None:
            proc = self.run_with_module(body)
        else:
            script = self.tmp / "mutant-probe.ps1"
            script.write_text("$ErrorActionPreference = 'Stop'\n" + f"Import-Module '{module}' -Force\n" + body, encoding="utf-8")
            from tools.repo_hygiene.test_playback_attr_3_cuda_behaviour import _run_pwsh_file
            proc = _run_pwsh_file(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def refusal(self, args: str, module: Path | None = None) -> str:
        body = f"try {{ [void](Get-AttrCudaLegTimeBudget {args}); Write-Output 'NO_THROW' }} catch {{ Write-Output ('THREW ' + $_.Exception.Message) }}\n"
        if module is None:
            proc = self.run_with_module(body)
        else:
            script = self.tmp / "mutant-refusal.ps1"
            script.write_text("$ErrorActionPreference = 'Stop'\n" + f"Import-Module '{module}' -Force\n" + body, encoding="utf-8")
            from tools.repo_hygiene.test_playback_attr_3_cuda_behaviour import _run_pwsh_file
            proc = _run_pwsh_file(script)
        return proc.stdout

    def owner_cpu_args(self, mb: float) -> str:
        return f"-InputBytes {_bytes(mb)} -PlaySeconds {CPU_PLAY_SEC} -ShareSmokeCeiling"

    def test_the_cpu_ceiling_constant_is_the_one_the_card_pr242_documents(self) -> None:
        gate = header_constants(HEADER.read_text(encoding="utf-8"))
        self.assertEqual(expected_budget_ms(25, gate["cpu_fraction"], gate["margin_ms"]) // 1000, CPU_PLAY_SEC)

    def test_the_owner_input_that_was_refused_now_derives_and_stays_at_the_smoke_ceiling(self) -> None:
        identity = math.ceil(OWNER_MB / RATE * 1.5)
        # the refusal this card cures: stacked at full margin it is over the ceiling
        self.assertGreater((identity + FIXED_SMOKE_SEC) * 1000, SMOKE_CEILING_SEC * 1000)
        budget = self.budget(self.owner_cpu_args(OWNER_MB))
        self.assertEqual(budget["identityReadSec"], identity, "the job's own identity read keeps its full margined allowance")
        self.assertEqual(budget["smokeProcessTimeoutMs"], SMOKE_CEILING_SEC * 1000)
        self.assertEqual(budget["appReadAllowanceSec"], SMOKE_CEILING_SEC - FIXED_SMOKE_SEC)
        self.assertTrue(budget["smokeCeilingClamped"])
        # the um-run timeout is derived from the same two numbers, never a third
        self.assertEqual(budget["jobTimeoutSec"], identity + SMOKE_CEILING_SEC + 1800 + 300)

    def test_the_clamped_allowance_still_covers_the_in_runner_reread_at_the_measured_rate_without_margin(self) -> None:
        budget = self.budget(self.owner_cpu_args(OWNER_MB))
        self.assertGreaterEqual(budget["appReadAllowanceSec"], math.ceil(OWNER_MB / RATE))

    def test_the_same_input_without_the_switch_is_still_refused_the_generic_ceiling_is_not_loosened(self) -> None:
        out = self.refusal(f"-InputBytes {_bytes(OWNER_MB)} -PlaySeconds {CPU_PLAY_SEC}")
        self.assertIn("THREW ATTRCUDA_TIMEBUDGET_EXCEEDS_SMOKE_CEILING", out)

    def test_a_cuda_sized_budget_of_the_same_input_is_exactly_what_it_was(self) -> None:
        identity = math.ceil(OWNER_MB / RATE * 1.5)
        budget = self.budget(f"-InputBytes {_bytes(OWNER_MB)}")
        self.assertEqual(budget["smokeProcessTimeoutMs"], (identity + 60 + 40 + 3 + 30) * 1000)
        self.assertFalse(budget["smokeCeilingClamped"])
        self.assertEqual(budget["jobTimeoutSec"], identity + (identity + 60 + 40 + 3 + 30) + 1800 + 300)

    def test_a_cuda_input_over_its_own_ceiling_is_still_refused(self) -> None:
        out = self.refusal(f"-InputBytes {_bytes(3600)}")        # 3600 s read + 133 s > 3600 s
        self.assertIn("THREW ATTRCUDA_TIMEBUDGET_EXCEEDS_SMOKE_CEILING", out)

    def test_an_input_that_already_fits_with_full_margin_is_not_clamped_and_matches_the_unshared_budget(self) -> None:
        args = f"-InputBytes {_bytes(1024)} -PlaySeconds {CPU_PLAY_SEC}"
        plain = self.budget(args)
        shared = self.budget(args + " -ShareSmokeCeiling")
        self.assertFalse(shared["smokeCeilingClamped"])
        for key in ("identityReadSec", "smokeProcessTimeoutMs", "jobTimeoutSec"):
            self.assertEqual(shared[key], plain[key], key)

    def test_a_fixture_cpu_leg_has_no_identity_read_and_is_not_clamped(self) -> None:
        budget = self.budget(f"-InputBytes 0 -PlaySeconds {CPU_PLAY_SEC} -ShareSmokeCeiling")
        self.assertEqual(budget["smokeProcessTimeoutMs"], FIXED_SMOKE_SEC * 1000)
        self.assertFalse(budget["smokeCeilingClamped"])

    def test_the_new_cap_is_exact_and_an_input_above_it_still_refuses_with_the_typed_token(self) -> None:
        cap_mb = (SMOKE_CEILING_SEC - FIXED_SMOKE_SEC) * RATE            # 4113 MB: re-read with no margin exactly fills the remainder
        inside = self.budget(self.owner_cpu_args(cap_mb - 1))
        self.assertEqual(inside["smokeProcessTimeoutMs"], SMOKE_CEILING_SEC * 1000)
        out = self.refusal(self.owner_cpu_args(cap_mb + 1))
        self.assertIn("THREW ATTRCUDA_TIMEBUDGET_EXCEEDS_SMOKE_CEILING", out)
        self.assertIn("coldReadMBps=", out)

    def test_a_slower_measured_rate_shrinks_the_cap_a_faster_one_raises_it(self) -> None:
        self.assertIn("THREW ATTRCUDA_TIMEBUDGET_EXCEEDS_SMOKE_CEILING", self.refusal(self.owner_cpu_args(OWNER_MB) + " -ColdReadMBps 1.0"))
        faster = self.budget(self.owner_cpu_args(OWNER_MB) + " -ColdReadMBps 11")
        self.assertFalse(faster["smokeCeilingClamped"], "at 11 MB/s the full margined read fits and nothing is clamped")

    def test_the_text_checks_hold_on_the_real_files(self) -> None:
        self.assertEqual(check_module_text(MODULE.read_text(encoding="utf-8")), [])
        self.assertEqual(check_generator_text((ROOT / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-job.ps1").read_text(encoding="utf-8")), [])

    # --- mutation: one per rule -----------------------------------------------------------------------------------------------------------------
    def _mutant(self, old: str, new: str) -> Path:
        text = MODULE.read_text(encoding="utf-8")
        self.assertEqual(text.count(old), 1, f"mutation anchor missing or ambiguous: {old!r}")
        path = self.tmp / "AttrCudaArtifacts.mutant.psm1"
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        return path

    def test_mutation_the_clamp_is_removed_so_the_owner_leg_is_refused_again(self) -> None:
        mutant = self._mutant("if ($smokeProcessTimeoutMs -gt 3600000 -and $ShareSmokeCeiling) {", "if ($false) {")
        self.assertIn("THREW ATTRCUDA_TIMEBUDGET_EXCEEDS_SMOKE_CEILING", self.refusal(self.owner_cpu_args(OWNER_MB), mutant))

    def test_mutation_the_clamp_applies_without_the_switch_so_cuda_would_be_loosened(self) -> None:
        mutant = self._mutant("if ($smokeProcessTimeoutMs -gt 3600000 -and $ShareSmokeCeiling) {", "if ($smokeProcessTimeoutMs -gt 3600000) {")
        self.assertNotIn("THREW", self.refusal(f"-InputBytes {_bytes(OWNER_MB)} -PlaySeconds {CPU_PLAY_SEC}", mutant), "the unshared refusal must be gone in the mutant")

    def test_mutation_the_shared_refusal_is_removed_so_an_input_over_the_cap_is_admitted(self) -> None:
        text = MODULE.read_text(encoding="utf-8")
        old = "if ($appReadAllowanceSec -lt $minimumReadSec) {"
        self.assertEqual(text.count(old), 1)
        path = self.tmp / "AttrCudaArtifacts.mutant2.psm1"
        path.write_text(text.replace(old, "if ($false) {", 1), encoding="utf-8")
        self.assertNotIn("THREW", self.refusal(self.owner_cpu_args(5000), path))

    def test_mutation_the_allowance_ignores_the_cpu_play_ceiling(self) -> None:
        mutant = self._mutant("$appReadAllowanceSec = 3600 - $fixedSmokeSec", "$appReadAllowanceSec = 3600 - ($LaunchSeconds + 40 + $SettleSeconds + $RunnerSlackSeconds)")
        budget = self.budget(self.owner_cpu_args(OWNER_MB), mutant)
        self.assertNotEqual(budget["appReadAllowanceSec"], SMOKE_CEILING_SEC - FIXED_SMOKE_SEC, "the allowance test must go red when the play ceiling is dropped")

    def test_mutation_generator_text(self) -> None:
        text = (ROOT / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-job.ps1").read_text(encoding="utf-8")
        mutants = {
            "switch not requested": ("$timeBudgetArgs['ShareSmokeCeiling'] = $true", ""),
            "switch requested for everyone": ("if ($cpuPlayPaceInformational) {\n    $timeBudgetArgs['PlaySeconds']", "$timeBudgetArgs['ShareSmokeCeiling'] = $true\nif ($cpuPlayPaceInformational) {\n    $timeBudgetArgs['PlaySeconds']"),
            "smoke timeout not derived": ("SMOKE_PROCESS_TIMEOUT_MS = [string]$timeBudget.smokeProcessTimeoutMs", "SMOKE_PROCESS_TIMEOUT_MS = '3600000'"),
            "presentmon not derived": ("PRESENTMON_TIMED_SECONDS = [string][int][math]::Ceiling($timeBudget.smokeProcessTimeoutMs / 1000.0)", "PRESENTMON_TIMED_SECONDS = '3600'"),
            "um-run timeout not derived": ("recommendedJobTimeoutSec = $timeBudget.jobTimeoutSec", "recommendedJobTimeoutSec = 9000"),
            "warning not guarded by the flag": ("if ($timeBudget.smokeCeilingClamped) {\n    Write-Warning", "if ($true) {\n    Write-Warning"),
            "clamp trace always on": (CLAMP_TRACE_EXPRESSION, 'SMOKE_CEILING_TRACE = " smokeCeilingClamped=True appReadAllowanceSec=0"'),
            "clamp trace not derived from the flag": ("SMOKE_CEILING_TRACE = $(if ($timeBudget.smokeCeilingClamped) {", "SMOKE_CEILING_TRACE = $(if ($true) {"),
            "first trace line drops the token": ('$SmokeProcessTimeoutMs__SMOKE_CEILING_TRACE__"', '$SmokeProcessTimeoutMs"'),
        }
        for name, (old, new) in mutants.items():
            with self.subTest(name):
                self.assertEqual(text.count(old), 1, f"mutation anchor missing or ambiguous: {name}")
                self.assertTrue(check_generator_text(text.replace(old, new, 1)), f"the check did not go red on: {name}")

    def test_mutation_module_text(self) -> None:
        text = MODULE.read_text(encoding="utf-8")
        for name, (old, new) in {
            "no switch": ("[switch]$ShareSmokeCeiling", "[switch]$SharedGone"),
            "clamp not behind the switch": ("-gt 3600000 -and $ShareSmokeCeiling) {", "-gt 3600000) {"),
        }.items():
            with self.subTest(name):
                self.assertEqual(text.count(old), 1, f"mutation anchor missing or ambiguous: {name}")
                self.assertTrue(check_module_text(text.replace(old, new, 1)), f"the check did not go red on: {name}")




@requires_windows_pwsh
class ClampIsConfinedToTheCpuInformationalOwnerLeg(unittest.TestCase):
    """STRUCTURAL invariants (a), (b), (c) of the CURRENT generator, run against a sparse clone whose fixture files are header-only ~30 s stand-ins (never real
    footage). A fixture has zero input bytes, so what is proven here is that the shared-ceiling path is neutral where there is nothing to share and that nothing
    else in a CUDA / fixture job carries a clamp line; the owner-size arithmetic is the budget function's (SharedSmokeCeilingBudget above)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="cpu-ceiling-gen-")
        cls.tmp = Path(cls._tmp.name)
        cls.head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        cls.repo = cls.tmp / "repo"
        subprocess.run(["git", "clone", "-q", "--shared", "--no-checkout", str(ROOT), str(cls.repo)], check=True)
        subprocess.run(["git", "-C", str(cls.repo), "sparse-checkout", "set", "--cone", "tools", "tests/fixtures/clips"], check=True)
        subprocess.run(["git", "-C", str(cls.repo), "checkout", "-q", cls.head], check=True)
        for stem in FIXTURE_IDS:
            write_synthetic_mlv(cls.repo / "tests" / "fixtures" / "clips" / (stem + MLV_EXT), FRAMES_30S_AT_23976)
        gate = header_constants(HEADER.read_text(encoding="utf-8"))
        cls.cpu_play_sec = expected_budget_ms(25, gate["cpu_fraction"], gate["margin_ms"]) // 1000      # 765 s, the CPU Play ceiling of the 25 s window

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    # --- helpers --------------------------------------------------------------------------------------------------------------------------------
    def generate(self, script: Path, out_name: str, extra: list[str]) -> tuple[str, str]:
        out = self.tmp / out_name
        proc = run_pwsh(["-File", str(script), "-SourceCommit", self.head, "-BuildManifestSha256", "ab" * 32,
                         "-ClipId", FIXTURE_IDS[0], "-FixtureSha256", "cd" * 32, "-RepoRoot", str(self.repo), "-OutFile", str(out), *extra])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return out.read_text(encoding="utf-8"), proc.stdout + proc.stderr

    def unshared_budget(self, play_seconds: int) -> dict:
        """Get-AttrCudaLegTimeBudget for a fixture (zero input bytes) asked WITHOUT -ShareSmokeCeiling: the derivation a leg that cannot clamp must equal."""
        proc = run_pwsh(["-Command", f"Import-Module '{MODULE}' -Force; Get-AttrCudaLegTimeBudget -InputBytes 0 -PlaySeconds {play_seconds} | ConvertTo-Json -Compress"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def mutant_generator(self, name: str, edit) -> Path:
        """A copy of the tools tree the generator needs, with `edit(generator_text, module_text) -> (generator_text, module_text)` applied."""
        root = self.tmp / f"mutant-{name}"
        gen = root / "tools" / "profiling" / "bachelor" / GENERATOR.name
        if gen.exists():            # already built in this class (one tree per mutation name)
            return gen
        shutil.copytree(ROOT / "tools" / "profiling", root / "tools" / "profiling")
        shutil.copytree(ROOT / "tools" / "gates", root / "tools" / "gates")
        gen = root / "tools" / "profiling" / "bachelor" / GENERATOR.name
        mod = root / "tools" / "profiling" / "bachelor" / MODULE.name
        gen_text, mod_text = edit(gen.read_bytes().decode("utf-8"), mod.read_bytes().decode("utf-8"))
        gen.write_bytes(gen_text.encode("utf-8"))
        mod.write_bytes(mod_text.encode("utf-8"))
        return gen

    @staticmethod
    def replaced_once(text: str, old: str, new: str) -> str:
        assert text.count(old) == 1, f"mutation anchor missing or ambiguous: {old!r}"
        return text.replace(old, new, 1)

    def no_clamp_path_generator(self) -> Path:
        # the CURRENT generator minus the one line that asks for the shared ceiling: "the same job without the clamp path"
        return self.mutant_generator("no-clamp-path", lambda g, m: (self.replaced_once(g, CLAMP_REQUEST, ""), m))

    # --- (a) every CUDA variant is the unshared derivation ----------------------------------------------------------------------------------------
    def test_every_cuda_variant_budget_is_the_unshared_derivation_and_carries_no_clamp_or_cpu_marker(self) -> None:
        for name, extra in CUDA_VARIANTS:
            with self.subTest(name):
                play = int(extra[extra.index("-PlaySeconds") + 1]) if "-PlaySeconds" in extra else 25        # the generator's own default
                job, out = self.generate(GENERATOR, f"cuda-{name}.job.ps1", extra)
                self.assertEqual(check_unshared_job(job, out, self.unshared_budget(max(40, play)), cpu_markers_allowed=False), [], name)

    def test_mutation_a_cuda_job_that_traces_a_clamp_or_takes_another_budget_is_caught(self) -> None:
        job, out = self.generate(GENERATOR, "cuda-base.job.ps1", [])
        unshared = self.unshared_budget(40)
        self.assertEqual(check_unshared_job(job, out, unshared, cpu_markers_allowed=False), [])
        smoke_ms, presentmon_sec = job_timeouts(job)
        mutants = {
            "traces a clamp": job.replace('smokeProcessTimeoutMs=$SmokeProcessTimeoutMs"', 'smokeProcessTimeoutMs=$SmokeProcessTimeoutMs smokeCeilingClamped=True appReadAllowanceSec=0"', 1),
            "smoke timeout is another budget": job.replace(f"$SmokeProcessTimeoutMs = {smoke_ms}", f"$SmokeProcessTimeoutMs = {smoke_ms + 1000}", 1),
            "PresentMon capture is another budget": job.replace(f"$PresentMonTimedSeconds = {presentmon_sec}", f"$PresentMonTimedSeconds = {presentmon_sec + 1}", 1),
            "a CPU-informational marker leaks into CUDA": job + "\n# -CpuPlayPaceInformational\n",
        }
        for name, mutant in mutants.items():
            with self.subTest(name):
                self.assertNotEqual(mutant, job, f"mutation anchor missing: {name}")
                self.assertTrue(check_unshared_job(mutant, out, unshared, cpu_markers_allowed=False), f"the invariant did not go red on: {name}")
        self.assertTrue(check_unshared_job(job, out + CLAMP_WARNING, unshared, cpu_markers_allowed=False), "the generator warning leaking into a CUDA run must be caught")

    def test_mutation_a_generator_whose_cuda_job_always_traces_a_clamp_is_caught(self) -> None:
        mutant = self.mutant_generator("cuda-traces-clamp", lambda g, m: (self.replaced_once(
            g, CLAMP_TRACE_EXPRESSION, 'SMOKE_CEILING_TRACE = " smokeCeilingClamped=True appReadAllowanceSec=0"'), m))
        job, out = self.generate(mutant, "mutant-cuda-clamp.job.ps1", [])
        self.assertTrue(check_unshared_job(job, out, self.unshared_budget(40), cpu_markers_allowed=False), "(a) did not go red on a CUDA job that traces a clamp")

    def test_mutation_requesting_the_switch_for_cuda_is_caught(self) -> None:
        # the request line moved out of the CPU-informational block so every backend asks for the shared ceiling (a fixture has no read to clamp, so the proof is the text rule)
        text = lf(GENERATOR.read_text(encoding="utf-8"))
        mutant = self.replaced_once(text, "if ($cpuPlayPaceInformational) {\n    $timeBudgetArgs['PlaySeconds']", CLAMP_REQUEST + "\nif ($cpuPlayPaceInformational) {\n    $timeBudgetArgs['PlaySeconds']")
        self.assertTrue(check_generator_text(mutant), "(a) did not go red on a generator that asks for the shared ceiling for every backend")

    # --- (b) a fixture CPU job is the same with and without the clamp path -------------------------------------------------------------------------
    def test_a_fixture_cpu_job_is_identical_with_and_without_the_clamp_path(self) -> None:
        without = self.no_clamp_path_generator()
        for name, extra in (("cpu", ["-Backend", "cpu"]),
                            ("um-cpu-look", ["-Backend", "cpu", "-Venue", "ultra-magnus", "-ForceLookAssist", "-ContactSheet"])):
            with self.subTest(name):
                with_path, out = self.generate(GENERATOR, f"with-{name}.job.ps1", extra)
                without_path, _out = self.generate(without, f"without-{name}.job.ps1", extra)
                self.assertEqual(check_clamp_neutral(with_path, without_path), [], name)
                # and its budget is the unshared derivation for the CPU ceiling
                self.assertEqual(check_unshared_job(with_path, out, self.unshared_budget(self.cpu_play_sec), cpu_markers_allowed=True), [], name)

    def test_mutation_a_clamp_that_reads_no_input_changes_a_fixture_cpu_job_and_is_caught(self) -> None:
        # the clamp fires whenever the switch is asked for, whatever the input: a fixture CPU job then differs from the same job without the clamp path
        mutant = self.mutant_generator("clamp-without-input", lambda g, m: (g, self.replaced_once(
            m, "if ($smokeProcessTimeoutMs -gt 3600000 -and $ShareSmokeCeiling) {", "if ($ShareSmokeCeiling) {")))
        extra = ["-Backend", "cpu"]
        mutated, _out = self.generate(mutant, "mutant-fixture-cpu.job.ps1", extra)
        without_path, _out = self.generate(self.no_clamp_path_generator(), "without-for-mutant.job.ps1", extra)
        self.assertTrue(check_clamp_neutral(mutated, without_path), "(b) did not go red when the clamp fires on a fixture")

    # --- (c) the clamp lines exist only in the CPU-informational owner variant ---------------------------------------------------------------------
    def test_the_clamp_lines_are_confined_to_the_cpu_informational_path(self) -> None:
        text = lf(GENERATOR.read_text(encoding="utf-8"))
        self.assertEqual(check_generator_text(text), [])
        self.assertEqual(text.count(CLAMP_REQUEST), 1)
        block = re.search(r"if \(\$cpuPlayPaceInformational\) \{\n(.*?)\n\}\n\$timeBudget = ", text, re.S)
        self.assertIsNotNone(block)
        self.assertIn(CLAMP_REQUEST, block.group(1))
        # no generated job (CUDA or fixture CPU) and no generator output carries a clamp line: only an owner CPU leg over the ceiling can
        for name, extra in (*CUDA_VARIANTS, ("cpu", ["-Backend", "cpu"])):
            with self.subTest(name):
                job, out = self.generate(GENERATOR, f"confined-{name}.job.ps1", extra)
                self.assertNotIn(CLAMP_TRACE, job)
                self.assertNotIn(CLAMP_WARNING, out)

    def test_mutation_clamp_lines_outside_the_cpu_informational_path_are_caught(self) -> None:
        text = lf(GENERATOR.read_text(encoding="utf-8"))
        mutants = {
            "request outside the cpu block": self.replaced_once(text, CLAMP_REQUEST + "\n}", "}\n" + CLAMP_REQUEST),
            "request duplicated": self.replaced_once(text, CLAMP_REQUEST, CLAMP_REQUEST + "\n    " + CLAMP_REQUEST),
            "warning unguarded": self.replaced_once(text, "if ($timeBudget.smokeCeilingClamped) {\n    Write-Warning", "if ($true) {\n    Write-Warning"),
        }
        for name, mutant in mutants.items():
            with self.subTest(name):
                self.assertTrue(check_generator_text(mutant), f"(c) did not go red on: {name}")


if __name__ == "__main__":
    unittest.main()
