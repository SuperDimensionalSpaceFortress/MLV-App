"""Tests for HFR-NATIVE-M16-1457-TIMEBUDGET-1 (round 1): an M16-1457 CUDA venue leg is admissible under the 3600 s smoke ceiling.

CLASS: an owner input whose full margined identity read plus a backend's fixed smoke seconds crosses the smoke runner's 3600 s process-timeout cap is admitted
for EVERY backend, not for the cpu-informational leg alone, as long as the in-runner re-read (the app's own load of pages the job's identity pass just warmed)
still fits the remainder at the MEASURED cold rate with NO margin; an input that cannot is refused with the same typed token.
Source: HFR-NATIVE-60FPS-FULLRES-1 r2 (the 3964 MB M16-1457 CUDA leg was refused at generation: 3965 s identity read + 60 + 40 + 3 + 30 = 4098 s > 3600 s).
CPU-LEG-SMOKE-CEILING-1 (test_cpu_leg_smoke_ceiling.py) already built the switch; it requested it for the cpu leg only, by scope choice.

Sizes come from the committed tools/gates/output-budget.json (part `length`s); no footage path, name, byte or frame is read or written here, and nothing is launched.
Get-AttrCudaLegTimeBudget's logic, the measured 1.5 MB/s pin and the refusal token are NOT changed by this card; what is pinned is the arithmetic and the one
generator line that asks for the switch.

  1. budget (EXECUTED in pwsh): M16-1457 on CUDA is refused without the switch and admitted with it (3467 s allowance, 9665 s um-run timeout);
  2. still refused: CUDA above floor(3467 x 1.5) MB, and M16-1457 at a slower measured rate;
  3. unchanged where it fits: M16-1243 and a 1 GiB input produce the same budget with and without the switch;
  4. the real generator, asked for -ClipId M16-1457 -Backend cuda, emits the clamped job; the master shape (request confined to the cpu block) is refused.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.repo_hygiene.test_cpu_leg_smoke_ceiling import CLAMP_REQUEST, confine_request_to_cpu_block
from tools.repo_hygiene.test_dual_venue_evidence import GENERATOR, ROOT, lf, requires_windows_pwsh, run_pwsh
from tools.repo_hygiene.test_playback_attr_3_cuda_behaviour import _PwshCase, normalize_pwsh_message_text, requires_pwsh

OUTPUT_BUDGET = ROOT / "tools" / "gates" / "output-budget.json"
TOKEN = "ATTRCUDA_TIMEBUDGET_EXCEEDS_SMOKE_CEILING"
RATE = 1.5                                  # $script:AttrCudaMeasuredColdReadMBps (Bachelor); not changed by this card
CUDA_FIXED_SMOKE_SEC = 60 + max(40, 25) + 3 + 30      # launch + play (the 40 s floor) + settle + slack
SMOKE_CEILING_SEC = 3600
APP_READ_ALLOWANCE_SEC = SMOKE_CEILING_SEC - CUDA_FIXED_SMOKE_SEC     # 3467
CUDA_CAP_MB = APP_READ_ALLOWANCE_SEC * RATE                           # 5200.5: re-read with no margin exactly fills the remainder


def clip_bytes(clip_id: str) -> int:
    """The clip's size from the committed spec: the sum of its parts' `length`s. A size, never a read of the footage."""
    spec = json.loads(OUTPUT_BUDGET.read_text(encoding="utf-8"))
    entry = next(c for c in spec["clips"] if c["id"] == clip_id)
    return sum(int(part["length"]) for part in entry["parts"])


def _bytes(mb: float) -> int:
    return int(mb * 1048576)


@requires_pwsh
class M161457Budget(_PwshCase):
    """Get-AttrCudaLegTimeBudget for the CUDA play window (40 s floor): arithmetic on a size, the measured rate and the runner's 3600 s ceiling."""

    def budget(self, args: str) -> dict:
        proc = self.run_with_module(f"Get-AttrCudaLegTimeBudget {args} | ConvertTo-Json -Compress\n")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def refusal(self, args: str) -> str:
        proc = self.run_with_module(f"try {{ [void](Get-AttrCudaLegTimeBudget {args}); Write-Output 'NO_THROW' }} catch {{ Write-Output ('THREW ' + $_.Exception.Message) }}\n")
        return proc.stdout

    # --- 1. M16-1457 on CUDA ----------------------------------------------------------------------------------------------------------------------
    def test_the_card_arithmetic_is_what_the_card_says(self) -> None:
        size = clip_bytes("M16-1457")
        self.assertEqual(size, 2431795200 + 1724845568)
        self.assertEqual(size, 4156640768)
        self.assertEqual(round(size / 1048576.0, 2), 3964.08)
        self.assertEqual(CUDA_FIXED_SMOKE_SEC, 133)
        self.assertEqual(APP_READ_ALLOWANCE_SEC, 3467)
        self.assertEqual(math.ceil(size / 1048576.0 / RATE), 2643)

    def test_m16_1457_cuda_is_refused_without_the_switch_the_master_generators_call(self) -> None:
        out = self.refusal(f"-InputBytes {clip_bytes('M16-1457')} -PlaySeconds 40")
        self.assertIn("THREW " + TOKEN, out)

    def test_m16_1457_cuda_is_admitted_with_the_switch(self) -> None:
        size = clip_bytes("M16-1457")
        budget = self.budget(f"-InputBytes {size} -PlaySeconds 40 -ShareSmokeCeiling")
        identity = math.ceil(size / 1048576.0 / RATE * 1.5)
        self.assertEqual(identity, 3965)
        self.assertEqual(budget["identityReadSec"], 3965, "the job's own identity read keeps its full margined allowance")
        self.assertEqual(budget["smokeProcessTimeoutMs"], 3600000)
        self.assertEqual(budget["appReadAllowanceSec"], 3467)
        self.assertTrue(budget["smokeCeilingClamped"])
        self.assertEqual(budget["jobTimeoutSec"], 9665)
        self.assertEqual(budget["jobTimeoutSec"], identity + SMOKE_CEILING_SEC + 1800 + 300, "the um-run timeout is derived from the same two numbers")
        self.assertGreaterEqual(budget["appReadAllowanceSec"], math.ceil(size / 1048576.0 / RATE), "the clamped allowance still covers the re-read at the measured rate, no margin")

    # --- 2. still refused -------------------------------------------------------------------------------------------------------------------------
    def test_a_cuda_input_just_over_the_new_cap_is_refused_and_just_under_it_is_admitted(self) -> None:
        over = math.floor(CUDA_CAP_MB) + 1
        under = math.floor(CUDA_CAP_MB) - 1
        out = self.refusal(f"-InputBytes {_bytes(over)} -PlaySeconds 40 -ShareSmokeCeiling")
        self.assertIn("THREW " + TOKEN, out)
        self.assertIn("coldReadMBps=", out)
        inside = self.budget(f"-InputBytes {_bytes(under)} -PlaySeconds 40 -ShareSmokeCeiling")
        self.assertEqual(inside["smokeProcessTimeoutMs"], 3600000)
        self.assertTrue(inside["smokeCeilingClamped"])

    def test_m16_1457_at_a_slower_measured_rate_is_still_refused(self) -> None:
        size = clip_bytes("M16-1457")
        self.assertGreater(math.ceil(size / 1048576.0 / 1.1), APP_READ_ALLOWANCE_SEC)
        out = self.refusal(f"-InputBytes {size} -PlaySeconds 40 -ShareSmokeCeiling -ColdReadMBps 1.1")
        self.assertIn("THREW " + TOKEN, out)
        self.assertIn("coldReadMBps=", out)

    # --- 3. unchanged where it fits ---------------------------------------------------------------------------------------------------------------
    def test_an_input_that_fits_unshared_has_the_same_budget_with_and_without_the_switch(self) -> None:
        for name, size in (("M16-1243", clip_bytes("M16-1243")), ("1 GiB", 1073741824), ("fixture", 0)):
            with self.subTest(name):
                args = f"-InputBytes {size} -PlaySeconds 40"
                plain = self.budget(args)
                shared = self.budget(args + " -ShareSmokeCeiling")
                self.assertFalse(shared["smokeCeilingClamped"], name)
                self.assertEqual(shared, plain, f"{name}: the budget changed with the switch")

    def test_m16_1243_is_the_3238_mb_input_the_cpu_leg_card_sized(self) -> None:
        self.assertEqual(round(clip_bytes("M16-1243") / 1048576.0), 3238)


@requires_windows_pwsh
class M161457Generator(unittest.TestCase):
    """The real generator, asked for the owner clip id M16-1457 on CUDA. It resolves the id against the committed spec ref (no footage is opened or hashed, the
    resolver reads the git ref and the hook's consent table only) and writes one job file into a temp directory; nothing is launched."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="m16-1457-gen-")
        cls.tmp = Path(cls._tmp.name)
        cls.head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        cls.repo = cls.tmp / "repo"
        subprocess.run(["git", "clone", "-q", "--shared", "--no-checkout", str(ROOT), str(cls.repo)], check=True)
        subprocess.run(["git", "-C", str(cls.repo), "sparse-checkout", "set", "--cone", "tools"], check=True)
        subprocess.run(["git", "-C", str(cls.repo), "checkout", "-q", cls.head], check=True)
        # the resolver reads the spec from the full ref refs/remotes/fork/master, never the working tree
        subprocess.run(["git", "-C", str(cls.repo), "update-ref", "refs/remotes/fork/master", cls.head], check=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def generate(self, script: Path, out_name: str) -> tuple[int, str, dict | None, str]:
        out = self.tmp / out_name
        command = (f"$r = @(& '{script}' -SourceCommit '{self.head}' -BuildManifestSha256 '{'ab' * 32}' -ClipId 'M16-1457' -Backend cuda "
                   f"-RepoRoot '{self.repo}' -OutFile '{out}' | Where-Object {{ $_.PSObject.Properties['recommendedJobTimeoutSec'] }}); "
                   "$r | Select-Object recommendedJobTimeoutSec, smokeProcessTimeoutMs | ConvertTo-Json -Compress")
        proc = run_pwsh(["-Command", command])
        message = normalize_pwsh_message_text(proc.stdout + proc.stderr)
        budget = None
        if proc.returncode == 0:
            budget = json.loads(proc.stdout.strip().splitlines()[-1])
        return proc.returncode, message, budget, (out.read_text(encoding="utf-8") if out.exists() else "")

    def mutant_generator(self, name: str, edit) -> Path:
        """A copy of the tools the generator and its owner-clip resolver need, with `edit(generator_text) -> generator_text` applied."""
        root = self.tmp / f"mutant-{name}"
        for sub in ("profiling", "gates", "hooks"):
            shutil.copytree(ROOT / "tools" / sub, root / "tools" / sub)
        gen = root / "tools" / "profiling" / "bachelor" / GENERATOR.name
        gen.write_bytes(edit(gen.read_bytes().decode("utf-8")).encode("utf-8"))
        return gen

    def test_the_current_generator_emits_the_clamped_m16_1457_cuda_job(self) -> None:
        code, message, budget, job = self.generate(GENERATOR, "fix.job.ps1")
        self.assertEqual(code, 0, message)
        self.assertEqual(budget["recommendedJobTimeoutSec"], 9665)
        self.assertEqual(budget["smokeProcessTimeoutMs"], 3600000)
        first_trace = next(line for line in lf(job).splitlines() if line.startswith("Write-JobTrace \"job start "))
        self.assertIn("smokeCeilingClamped=True appReadAllowanceSec=3467", first_trace)
        self.assertIn("clip=$ClipId", first_trace)
        self.assertIn("CPU-LEG-SMOKE-CEILING-1: this leg's smoke timeout is held", message)

    def test_mutation_the_master_generator_with_the_request_confined_to_the_cpu_block_refuses_the_leg(self) -> None:
        text = lf(GENERATOR.read_text(encoding="utf-8"))
        self.assertEqual(text.count(CLAMP_REQUEST + "\n"), 1)
        mutant = self.mutant_generator("master-shape", lambda g: confine_request_to_cpu_block(g))
        code, message, budget, job = self.generate(mutant, "master-shape.job.ps1")
        self.assertNotEqual(code, 0, "the master shape must refuse M16-1457 on CUDA")
        self.assertIn(TOKEN, message)
        self.assertEqual(job, "")


if __name__ == "__main__":
    unittest.main()
