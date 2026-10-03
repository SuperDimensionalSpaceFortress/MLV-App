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
  2. the generator passes the switch for the CPU-informational variant ONLY (text, mutation-tested);
  3. a CUDA job is byte-identical to the one the generators emitted at a16177f5 (the master this card started from).

Every rule has a mutation test.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.repo_hygiene.test_cpu_look_leg_pace_abort import expected_budget_ms, header_constants, HEADER
from tools.repo_hygiene.test_dual_venue_evidence import (
    FIXTURE_IDS,
    GENERATOR,
    MLV_EXT,
    ROOT,
    requires_windows_pwsh,
    run_pwsh,
)
from tools.repo_hygiene.synthetic_mlv import FRAMES_30S_AT_23976, write_synthetic_mlv
from tools.repo_hygiene.test_playback_attr_3_cuda_behaviour import MODULE, _PwshCase, requires_pwsh

# master this card started from (it contains the CPU-LOOK-LEG-PACE-ABORT-1 generator): the byte-identity baseline of the CUDA job.
PRIOR_MASTER = "a16177f59bc132c4896e9e0518709984cb9959eb"
OWNER_MB = 3238                       # the M16-1243 owner input, rounded (size only)
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
    return problems


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
class CudaJobByteIdentityToThisCardsBaseline(unittest.TestCase):
    """The CUDA job text is identical to what the generators emitted at a16177f5 (this card changes the CPU-informational budget only).
    Runs the real generator against a sparse clone whose fixture files are header-only ~30 s stand-ins (never real footage)."""

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

    def generate(self, script: Path, out_name: str, extra: list[str]) -> Path:
        out = self.tmp / out_name
        proc = run_pwsh(["-File", str(script), "-SourceCommit", self.head, "-BuildManifestSha256", "ab" * 32,
                         "-ClipId", FIXTURE_IDS[0], "-FixtureSha256", "cd" * 32, "-RepoRoot", str(self.repo), "-OutFile", str(out), *extra])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return out

    def test_every_cuda_variant_is_byte_identical_to_the_baseline(self) -> None:
        if not self.prior_available:
            self.skipTest(f"{PRIOR_MASTER[:12]} is not in this clone")
        prior = self.prior_root / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-job.ps1"
        for name, extra in (("default", []), ("look", ["-ForceLookAssist", "-ContactSheet"]), ("um", ["-Venue", "ultra-magnus"]), ("play30", ["-PlaySeconds", "30"])):
            new = self.generate(GENERATOR, f"new-{name}.job.ps1", extra)
            old = self.generate(prior, f"old-{name}.job.ps1", extra)
            self.assertEqual(new.read_bytes(), old.read_bytes(), f"the {name} CUDA job changed")

    def test_a_fixture_cpu_job_is_byte_identical_too_a_fixture_has_no_identity_read_to_share(self) -> None:
        if not self.prior_available:
            self.skipTest(f"{PRIOR_MASTER[:12]} is not in this clone")
        prior = self.prior_root / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-job.ps1"
        for name, extra in (("cpu", ["-Backend", "cpu"]), ("um-cpu-look", ["-Backend", "cpu", "-Venue", "ultra-magnus", "-ForceLookAssist", "-ContactSheet"])):
            new = self.generate(GENERATOR, f"new-{name}.job.ps1", extra)
            old = self.generate(prior, f"old-{name}.job.ps1", extra)
            self.assertEqual(new.read_bytes(), old.read_bytes(), f"the {name} CPU fixture job changed")


if __name__ == "__main__":
    unittest.main()
