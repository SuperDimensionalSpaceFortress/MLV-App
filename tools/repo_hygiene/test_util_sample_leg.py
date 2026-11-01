"""Tests for CUDA-HEADROOM-UTIL-SAMPLER-1: the opt-in GPU/CPU utilisation sampler for venue legs (generatorArgs.utilSample -> -UtilSample).

WHAT IS PINNED
  * T1 byte identity: with -UtilSample absent the job is byte-for-byte the base generator's (the pinned base commit's generator run against this
    tree's modules), and a copy of the generator with its one seam ($utilSampleEmit) turned off emits, given -UtilSample, the job without it;
  * T2 presence: with -UtilSample the five bracketed regions are there exactly once each, carry the exact nvidia-smi query once and both CSV
    names, start after the pre-launch PresentMon stop sites (session-locked, keep-alive) and stop before every post-launch one, and stripping
    them gives back the job without -UtilSample;
  * T3 anchors: a template that lost the measured launch line (or its done line) is the typed DUAL_VENUE_UTIL_SAMPLE_ANCHOR_MISSING refusal;
  * T4 the pure parsers and summaries ([N/A], a torn last row, rows outside the window, two rows per timestamp, an empty file, no window,
    host-local nvidia-smi time to UTC), lifted from the generator by AST;
  * T5 fail open: a missing nvidia-smi, a D3 dGPU on a cpu leg, a D-state read that throws, a CPU counter that throws and a sampler process
    that cannot start each give a typed status and never throw, and a skipped start starts no process;
  * T6 forwarding: Invoke-VenueLeg passes UtilSample only for a JSON true (stub generator), never for "true", false or absent;
  * T7 schema: generatorArgs.utilSample is a boolean (a string is refused), and the two committed utilsample legs validate.
No test launches MLVApp (owner rule 2026-10-11): the generator only writes job text, and the runner runs against stubs.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.repo_hygiene.synthetic_mlv import FRAMES_30S_AT_23976, write_synthetic_mlv
from tools.repo_hygiene.test_dual_venue_evidence import (
    DV,
    FIXTURE_IDS,
    GENERATOR,
    MLV_EXT,
    ROOT,
    STUB_GENERATOR,
    RunnerHarness,
    git,
    lf,
    requires_windows_pwsh,
    run_pwsh,
    run_pwsh_many,
)

# The base this card was cut from (fork/master at dispatch). T1 runs its generator against this tree's modules.
BASE_COMMIT = "df039a92e8af57d57b3594d466c09cf2947d3bc7"
SEAM_ON = re.compile(r"(?m)^\$utilSampleEmit = \[bool\]\$UtilSample(?=\r?$)")
SEAM_OFF = "$utilSampleEmit = $false"
OPEN = "# CUDA-HEADROOM-UTIL-SAMPLER-1 >>>"
CLOSE = "# CUDA-HEADROOM-UTIL-SAMPLER-1 <<<"
REGIONS = 5
REGION = re.compile(r"(?ms)^[ \t]*" + re.escape(OPEN) + r"\n.*?^[ \t]*" + re.escape(CLOSE) + r"\n")
UTIL_TOKENS = ("utilsample", "util-sample", "gpu-util.csv", "cpu-util.csv")
QUERY = ("--query-gpu=timestamp,utilization.gpu,utilization.memory,clocks.sm,pstate,power.draw --format=csv,noheader,nounits -lms 250")
LAUNCH_LINE = "Write-JobTrace 'step smoke-launch start (app launch + load + playback)'"
DONE_LINE = 'Write-JobTrace "step smoke-launch done rc=$smokeRc"'
# The headroom legs' generator shape (what Invoke-VenueLeg passes for m16-1243-headroom-unpaced-windowed-s1, per backend).
CPU_SHAPE = ["-Backend", "cpu", "-ScaleFactor", "1", "-ExpectedScaleRequest", "1", "-DisplayMode", "windowed", "-TelemetryArm", "LIGHT",
             "-CpuQuiescenceThresholdPercent", "95", "-ForceLookAssist", "-LookPaceLeg", "-LookFlavor", "cinematic", "-SwapInterval", "0",
             "-Unpaced", "-PresentTrace"]
CUDA_SHAPE = ["-Backend", "cuda", "-ScaleFactor", "1", "-ExpectedScaleRequest", "1", "-DisplayMode", "windowed", "-TelemetryArm", "LIGHT",
              "-CpuQuiescenceThresholdPercent", "95", "-ForceLookAssist", "-LookPaceLeg", "-LookFlavor", "cinematic", "-SwapInterval", "0",
              "-Unpaced", "-PresentTrace"]
CP_ARGS = ["-CorePartitionTelemetry", "-CorePartitionHelperMask", "0xFF000"]
LEGS = ("legs/m16-1243-utilsample-unpaced-windowed-s1.json",)
SCALARS = ["utilSampleStatus", "utilSampleReason", "gpuUtilStatus", "gpuUtilMean", "gpuUtilP95", "gpuRowsInWindow", "gpuRowsTotal",
           "gpuUtilDisclosure", "cpuTotalMean", "appCpuMeanCores", "cpuRowsInWindow", "cpuRowsTotal"]


def strip_regions(text: str) -> str:
    return REGION.sub("", lf(text))


@requires_windows_pwsh
class UtilSampleGeneratorTests(unittest.TestCase):
    """Runs the generator for real against a sparse clone with header-only ~30 s fixtures (the device test_dual_venue_evidence uses)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="util-sample-")
        cls.tmp = Path(cls._tmp.name)
        cls.head = git("rev-parse", "HEAD")
        cls.repo = cls.tmp / "repo"
        subprocess.run(["git", "clone", "-q", "--shared", "--no-checkout", str(ROOT), str(cls.repo)], check=True)
        subprocess.run(["git", "-C", str(cls.repo), "sparse-checkout", "set", "--cone", "tools", "tests/fixtures/clips"], check=True)
        subprocess.run(["git", "-C", str(cls.repo), "checkout", "-q", cls.head], check=True)
        for stem in FIXTURE_IDS:
            write_synthetic_mlv(cls.repo / "tests" / "fixtures" / "clips" / (stem + MLV_EXT), FRAMES_30S_AT_23976)
        source = GENERATOR.read_bytes().decode("utf-8")
        cls.seam_sites = len(SEAM_ON.findall(source))

        def tree_copy(name: str, text: str) -> Path:
            root = cls.tmp / name
            for part in ("profiling", "gates"):
                shutil.copytree(ROOT / "tools" / part, root / "tools" / part, ignore=shutil.ignore_patterns("__pycache__"))
            out = root / GENERATOR.relative_to(ROOT)
            out.write_bytes(text.encode("utf-8"))
            return out

        cls.off_generator = tree_copy("seam-off", SEAM_ON.sub(lambda _m: SEAM_OFF, source))
        # T3: the template's own launch / done line (the FIRST occurrence in the generator: the template precedes every edit block) moved
        cls.no_launch = tree_copy("no-launch", source.replace(LAUNCH_LINE, "Write-JobTrace 'step smoke-launch start (moved)'", 1))
        cls.no_done = tree_copy("no-done", source.replace(DONE_LINE, 'Write-JobTrace "step smoke-launch finished rc=$smokeRc"', 1))
        cls.base_available = subprocess.run(["git", "-C", str(ROOT), "cat-file", "-e", f"{BASE_COMMIT}:{GENERATOR.relative_to(ROOT).as_posix()}"],
                                            capture_output=True).returncode == 0
        requests: list[tuple[str, Path, list[str]]] = [
            ("default", GENERATOR, []), ("cpu", GENERATOR, CPU_SHAPE), ("cuda", GENERATOR, CUDA_SHAPE),
            ("us-default", GENERATOR, ["-UtilSample"]), ("us-cpu", GENERATOR, [*CPU_SHAPE, "-UtilSample"]), ("us-cuda", GENERATOR, [*CUDA_SHAPE, "-UtilSample"]),
            ("cp-cpu", GENERATOR, [*CPU_SHAPE, *CP_ARGS]), ("us-cp-cpu", GENERATOR, [*CPU_SHAPE, *CP_ARGS, "-UtilSample"]),
            ("off-default", cls.off_generator, []), ("off-us-cpu", cls.off_generator, [*CPU_SHAPE, "-UtilSample"]),
            ("off-us-cuda", cls.off_generator, [*CUDA_SHAPE, "-UtilSample"]),
            ("no-launch", cls.no_launch, [*CUDA_SHAPE, "-UtilSample"]), ("no-done", cls.no_done, [*CUDA_SHAPE, "-UtilSample"]),
            ("no-launch-plain", cls.no_launch, CUDA_SHAPE),
        ]
        if cls.base_available:
            base_text = subprocess.run(["git", "-C", str(ROOT), "show", f"{BASE_COMMIT}:{GENERATOR.relative_to(ROOT).as_posix()}"],
                                       capture_output=True, check=True).stdout.decode("utf-8")
            cls.base_generator = tree_copy("base", base_text)
            requests += [("base-default", cls.base_generator, []), ("base-cpu", cls.base_generator, CPU_SHAPE), ("base-cuda", cls.base_generator, CUDA_SHAPE)]
        commands = []
        cls.out = {}
        for key, script, extra in requests:
            out = cls.tmp / f"{key}.job.ps1"
            cls.out[key] = out
            commands.append(["-File", str(script), "-SourceCommit", cls.head, "-BuildManifestSha256", "ab" * 32, "-ClipId", FIXTURE_IDS[0],
                             "-FixtureSha256", "cd" * 32, "-RepoRoot", str(cls.repo), "-OutFile", str(out), *extra])
        cls.proc = dict(zip([k for k, _s, _e in requests], run_pwsh_many(commands)))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def job(self, key: str) -> str:
        proc = self.proc[key]
        self.assertEqual(proc.returncode, 0, f"{key}: {proc.stdout[-2000:]}{proc.stderr[-2000:]}")
        return self.out[key].read_text(encoding="utf-8")

    def test_t1_a_job_without_util_sample_is_the_base_generators_and_the_seam_carries_every_emission(self) -> None:
        self.assertEqual(self.seam_sites, 1, "the generator must carry exactly one $utilSampleEmit seam line")
        if self.base_available:
            for new, base in (("default", "base-default"), ("cpu", "base-cpu"), ("cuda", "base-cuda")):
                with self.subTest(job=new):
                    self.job(new), self.job(base)
                    self.assertEqual(self.out[new].read_bytes(), self.out[base].read_bytes(), f"{new}: the job without -UtilSample is not the base generator's")
        for key in ("off-default", "default"):
            self.job(key)
        self.assertEqual(self.out["off-default"].read_bytes(), self.out["default"].read_bytes(), "the seam-off copy emitted a different default job")
        for off, plain in (("off-us-cpu", "cpu"), ("off-us-cuda", "cuda")):
            with self.subTest(job=off):
                self.job(off), self.job(plain)
                self.assertEqual(self.out[off].read_bytes(), self.out[plain].read_bytes(), f"{off}: with the seam off, -UtilSample still changed the job")
                self.assertNotEqual(self.job(off.removeprefix("off-")), self.job(plain), f"{off}: the seam is on in the generator under test")
        for key in ("default", "cpu", "cuda", "cp-cpu"):
            with self.subTest(job=key):
                low = self.job(key).lower()
                self.assertEqual([t for t in UTIL_TOKENS if t in low], [], f"{key}: a job without -UtilSample carries util-sample text")

    def test_t2_the_regions_hold_the_samplers_and_strip_back_to_the_plain_job(self) -> None:
        for key, plain in (("us-default", "default"), ("us-cpu", "cpu"), ("us-cuda", "cuda"), ("us-cp-cpu", "cp-cpu")):
            with self.subTest(job=key):
                text = lf(self.job(key))
                self.assertEqual((text.count(OPEN), text.count(CLOSE)), (REGIONS, REGIONS), "five regions: start, stop, scalars, summary lines, outer finally")
                self.assertEqual(text.count(QUERY), 1, "the exact nvidia-smi query, once")
                for name in ("'gpu-util.csv'", "'cpu-util.csv'"):
                    self.assertEqual(text.count(f"Join-Path $legOut {name}"), 1, name)
                    self.assertEqual(text.count(f"Join-Path $Pub {name}"), 1, f"{name} is published once (a guarded helper called from two sites)")
                self.assertEqual(strip_regions(text), lf(self.job(plain)), "outside its regions the job is the job without -UtilSample")
                starts = [m.start() for m in re.finditer(re.escape(OPEN), text)]
                launch = text.index(LAUNCH_LINE)
                done = text.index(DONE_LINE)
                # region 1 sits right before the measured launch line, region 2 right after its done line
                self.assertTrue(text[:launch].rstrip("\n").endswith(CLOSE), "the start region ends right before the measured launch line")
                self.assertTrue(text[done + len(DONE_LINE) + 1:].startswith(OPEN), "the stop region opens right after the measured launch's done line")
                self.assertEqual(sum(1 for s in starts if s < launch), 1)
                # every PresentMon stop site after the launch is preceded by the samplers' stop; the two before it precede the samplers' start
                for site in ("site=session-locked", "site=keepalive"):
                    self.assertLess(text.index(site), starts[0], site)
                for site in ("site=smoke-run-failed", "site=smoke-log-unavailable", "$presentMonDoneResult = Wait-PresentMonCapture"):
                    self.assertGreater(text.index(site), starts[1], site)
                self.assertIn("Stop-UtilSampleAll", text[starts[1]:text.index(CLOSE, starts[1])])
                # the outer finally stops and publishes on every other exit
                finally_at = text.rindex("} finally {\n")
                self.assertIn("Stop-UtilSampleAll", text[finally_at:])
                self.assertIn("Publish-UtilSampleFiles", text[finally_at:])
                for name in SCALARS:
                    self.assertEqual(text.count(f"    {name} = $utilSampleScalars['{name}']"), 1, name)
        cuda, cpu = lf(self.job("us-cuda")), lf(self.job("us-cpu"))
        self.assertIn("$utilSampleBackend = 'cuda'", cuda)
        self.assertIn("$utilSampleBackend = 'cpu'", cpu)
        cp = lf(self.job("us-cp-cpu"))
        self.assertIn("$utilSampleAffinity = [int64]1044480", cp, "with a helper mask the CPU sampler gets that affinity")
        self.assertIn("$utilSampleAffinity = [int64]0", cpu)

    def test_t3_a_template_without_the_launch_or_done_line_is_a_typed_refusal(self) -> None:
        for key in ("no-launch", "no-done"):
            with self.subTest(job=key):
                proc = self.proc[key]
                self.assertNotEqual(proc.returncode, 0, f"{key}: the generator accepted a template without its anchor")
                self.assertIn("DUAL_VENUE_UTIL_SAMPLE_ANCHOR_MISSING ", proc.stdout + proc.stderr)
                self.assertFalse(self.out[key].exists(), "a refused generation wrote a job")
        self.job("no-launch-plain")

    def test_the_jobs_parse(self) -> None:
        script = self.tmp / "parse.ps1"
        script.write_text("$bad = 0\nforeach ($p in $args) { $e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile($p, [ref]$null, [ref]$e); "
                          "if ($e.Count) { $bad++; Write-Output \"$p $($e[0].Message)\" } }\nWrite-Output \"BAD=$bad\"\n", encoding="utf-8")
        proc = run_pwsh(["-File", str(script), *[str(self.out[k]) for k in ("us-cpu", "us-cuda", "us-cp-cpu") if self.job(k)]])
        self.assertIn("BAD=0", proc.stdout, proc.stdout + proc.stderr)
        encoded = re.search(r"(?m)^\$utilSampleEncoded = '([^']+)'", self.job("us-cuda"))
        self.assertIsNotNone(encoded)


LIFT = r"""
$ErrorActionPreference = 'Stop'
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:US_GEN, [ref]$null, [ref]$null)
foreach ($f in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -like '*UtilSample*' }, $false)) {
    . ([scriptblock]::Create($f.Extent.Text))
}
"""

DRIVER = r"""
$r = [ordered]@{}
$cst = [TimeZoneInfo]::FindSystemTimeZoneById('Central Standard Time')
function J($v) { $v | ConvertTo-Json -Depth 8 -Compress }
$ws = [DateTime]::new(2026, 11, 1, 14, 0, 0, 0, [DateTimeKind]::Utc); $we = $ws.AddMilliseconds(800)
$win = [ordered]@{ startUtc = $ws; endUtc = $we }
$gpuText = "2026/11/01 08:00:00.000, 40, 10, 1800, P2, 50.00`n2026/11/01 08:00:00.250, 60, 12, 1800, P2, 55.00`n2026/11/01 08:00:00.500, [N/A], 12, 1800, P2, [N/A]`n" +
           "2026/11/01 08:00:00.750, 80, 14, 1800, P2, 60.00`n2026/11/01 08:00:01.000, 99, 1, 1800, P2, 70.00`n2026/11/01 08:00:01.2"
$g = ConvertFrom-UtilSampleGpuCsv $gpuText $cst
$r['gpuParse'] = @{ rows = $g.rows.Count; torn = $g.tornTail; why = $g.why; firstUtc = $g.rows[0].utc.ToString('o'); nullUtil = ($null -eq $g.rows[2].util); nullPower = ($null -eq $g.rows[2].power); pstate = $g.rows[0].pstate }
$gs = Get-UtilSampleGpuSummary $g ([ordered]@{ status = 'running'; reason = $null }) $win
$r['gpuWin'] = @{ status = $gs.status; mean = $gs.mean; p95 = $gs.p95; inWindow = $gs.rowsInWindow; total = $gs.rowsTotal }
$gn = Get-UtilSampleGpuSummary $g ([ordered]@{ status = 'running'; reason = $null }) $null
$r['gpuNoWin'] = @{ status = $gn.status; mean = $gn.mean; p95 = $gn.p95; inWindow = $gn.rowsInWindow; total = $gn.rowsTotal }
# a complete last row without its newline is kept (the Repair-PresentMonCsvTail rule)
$gk = ConvertFrom-UtilSampleGpuCsv "2026/11/01 08:00:00.000, 40, 10, 1800, P2, 50.00`n2026/11/01 08:00:00.250, 60, 12, 1800, P2, 55.00" $cst
$r['gpuKeptTail'] = @{ rows = $gk.rows.Count; torn = $gk.tornTail }
$ga = ConvertFrom-UtilSampleGpuCsv "2026/11/01 08:00:00.000, 40, 10, 1800, P2, 50.00`n2026/11/01 08:00:00.000, 3, 1, 210, P8, 9.00`n" $cst
$r['gpuAmbiguous'] = (Get-UtilSampleGpuSummary $ga ([ordered]@{ status = 'running'; reason = $null }) $win).status
$r['gpuEmpty'] = (Get-UtilSampleGpuSummary (ConvertFrom-UtilSampleGpuCsv '' $cst) ([ordered]@{ status = 'running'; reason = $null }) $win).status
$r['gpuMissingFile'] = (Get-UtilSampleGpuSummary (ConvertFrom-UtilSampleGpuCsv $null $cst) ([ordered]@{ status = 'running'; reason = $null }) $win).status
$r['gpuGarbage'] = (Get-UtilSampleGpuSummary (ConvertFrom-UtilSampleGpuCsv "garbage`nmore garbage`n" $cst) ([ordered]@{ status = 'running'; reason = $null }) $win).status
$r['gpuAllNa'] = (Get-UtilSampleGpuSummary (ConvertFrom-UtilSampleGpuCsv "2026/11/01 08:00:00.100, [N/A], [N/A], [N/A], [N/A], [N/A]`n" $cst) ([ordered]@{ status = 'running'; reason = $null }) $win).status
$r['gpuSkipped'] = (Get-UtilSampleGpuSummary $g ([ordered]@{ status = 'skipped_wake_risk'; reason = 'dgpu-D3' }) $win).status
$r['gpuStartFailed'] = (Get-UtilSampleGpuSummary $g ([ordered]@{ status = 'unavailable'; reason = 'nvidia-smi-missing' }) $win).status
$r['gpuSummerUtc'] = (ConvertFrom-UtilSampleGpuCsv "2026/07/01 08:00:00.000, 1, 1, 1, P0, 1.0`n" $cst).rows[0].utc.ToString('o')
$cpuText = "utc,cpuTotalPct,appPid,appCpuCores`n2026-11-01T13:59:59.5000000Z,10,,`n2026-11-01T14:00:00.1000000Z,50,1234,2.5`n2026-11-01T14:00:00.6000000Z,,1234,3.5`n" +
           "2026-11-01T14:00:01.5000000Z,90,1234;1240,8`n2026-11-01T14:00:0"
$c = ConvertFrom-UtilSampleCpuCsv $cpuText
$r['cpuParse'] = @{ rows = $c.rows.Count; torn = $c.tornTail; why = $c.why }
$cs = Get-UtilSampleCpuSummary $c ([ordered]@{ status = 'running'; reason = $null }) $win
$r['cpuWin'] = @{ status = $cs.status; total = $cs.totalMean; cores = $cs.appCoresMean; inWindow = $cs.rowsInWindow; rows = $cs.rowsTotal; pids = $cs.appPids }
$cn = Get-UtilSampleCpuSummary $c ([ordered]@{ status = 'running'; reason = $null }) $null
$r['cpuNoWin'] = @{ status = $cn.status; total = $cn.totalMean; cores = $cn.appCoresMean; inWindow = $cn.rowsInWindow; pids = $cn.appPids }
$r['cpuEmpty'] = (Get-UtilSampleCpuSummary (ConvertFrom-UtilSampleCpuCsv '' ) ([ordered]@{ status = 'running'; reason = $null }) $win).status
$r['cpuNoHeader'] = (Get-UtilSampleCpuSummary (ConvertFrom-UtilSampleCpuCsv "2026-11-01T14:00:00.1000000Z,50,1234,2.5`n") ([ordered]@{ status = 'running'; reason = $null }) $win).status
$r['cpuStartFailed'] = (Get-UtilSampleCpuSummary $c ([ordered]@{ status = 'unavailable'; reason = 'cpu-sampler-start-failed' }) $win).status
$sOk = Get-UtilSampleScalars $gs $cs $true
$r['scalarsOk'] = $sOk
$r['scalarsNoWindow'] = Get-UtilSampleScalars $gn $cn $false
$r['scalarsSkipped'] = Get-UtilSampleScalars (Get-UtilSampleGpuSummary $g ([ordered]@{ status = 'skipped_wake_risk'; reason = 'dgpu-D3' }) $win) $cs $true
$r['scalarsNone'] = Get-UtilSampleScalars (Get-UtilSampleGpuSummary $g ([ordered]@{ status = 'unavailable'; reason = 'nvidia-smi-missing' }) $win) (Get-UtilSampleCpuSummary $c ([ordered]@{ status = 'unavailable'; reason = 'cpu-sampler-start-failed' }) $win) $true
$r['window'] = Get-UtilSampleWindow ([DateTime]::new(2026, 11, 1, 14, 0, 0, 0, [DateTimeKind]::Utc)) 1500.5 21500.25
$r['windowNull'] = $null -eq (Get-UtilSampleWindow ([DateTime]::new(2026, 11, 1, 14, 0, 0, 0, [DateTimeKind]::Utc)) $null 21500)
$r['windowInverted'] = $null -eq (Get-UtilSampleWindow ([DateTime]::new(2026, 11, 1, 14, 0, 0, 0, [DateTimeKind]::Utc)) 9000 100)
# T5 fail open
$t = $env:US_TMP
$noSmi = Join-Path $t 'no-such\nvidia-smi.exe'
$m = Start-UtilSampleGpu 'cuda' $noSmi (Join-Path $t 'g1.csv') { @{ state = 'D0'; reason = $null } }
$r['startMissing'] = @{ status = $m.status; reason = $m.reason; proc = ($null -eq $m.proc); disclosure = $m.wakeDisclosure }
$m = Start-UtilSampleGpu 'cuda' '' (Join-Path $t 'g1b.csv') { @{ state = 'D0'; reason = $null } }
$r['startEmptyPath'] = @{ status = $m.status; reason = $m.reason }
$pw = (Get-Process -Id $PID).Path
$d3 = Start-UtilSampleGpu 'cpu' $pw (Join-Path $t 'g2.csv') { @{ state = 'D3'; reason = $null } }
$r['startD3'] = @{ status = $d3.status; reason = $d3.reason; proc = ($null -eq $d3.proc); dState = $d3.dState; file = (Test-Path -LiteralPath (Join-Path $t 'g2.csv')) }
$dt = Start-UtilSampleGpu 'cpu' $pw (Join-Path $t 'g3.csv') { throw 'pnp exploded' }
$r['startDThrows'] = @{ status = $dt.status; reason = $dt.reason; proc = ($null -eq $dt.proc) }
$dn = Start-UtilSampleGpu 'cpu' $pw (Join-Path $t 'g4.csv') { @{ state = $null; reason = 'multi-dgpu-ambiguous' } }
$r['startDAmbiguous'] = @{ status = $dn.status; reason = $dn.reason; proc = ($null -eq $dn.proc) }
$dx = Start-UtilSampleGpu 'cpu' $pw (Join-Path $t 'g5.csv') { @{ state = 'absent'; reason = 'no-dgpu' } }
$r['startNoDgpu'] = @{ status = $dx.status; reason = $dx.reason; proc = ($null -eq $dx.proc) }
$cf = Start-UtilSampleCpu (Join-Path $t 'no-such\pwsh.exe') 'AAAA' (Join-Path $t 'c1.csv') (Join-Path $t 'c1.stop') 'MLVApp' 0 1000 0 0
$r['cpuStartMissing'] = @{ status = $cf.status; reason = $cf.reason; proc = ($null -eq $cf.proc) }
$loop = Invoke-UtilSampleCpuLoop (Join-Path $t 'c2.csv') (Join-Path $t 'c2.stop') 'no-such-process-name-xyz' 400 1300 { throw 'counter exploded' } 0 0 0
$lines = @([IO.File]::ReadAllLines((Join-Path $t 'c2.csv')))
$r['cpuLoopThrowingCounter'] = @{ endedBy = $loop.endedBy; header = $lines[0]; rows = ($lines.Count - 1); firstRow = $(if ($lines.Count -gt 1) { $lines[1] }); errors = $loop.counterErrors }
$st = Stop-UtilSampleProcess $null 100
$r['stopNull'] = @{ stopped = $st.stopped; why = $st.why }
$r['stopAllNothing'] = (Stop-UtilSampleAll ([ordered]@{ status = 'unavailable'; proc = $null }) ([ordered]@{ status = 'unavailable'; proc = $null }) { param($p) throw 'no stop file' } 100).gpu.why
Write-Output (J $r)
"""


@requires_windows_pwsh
class UtilSamplePureHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="util-sample-pure-")
        cls.addClassCleanup(cls._tmp.cleanup)
        script = Path(cls._tmp.name) / "driver.ps1"
        script.write_text(LIFT + DRIVER, encoding="utf-8")
        proc = run_pwsh(["-File", str(script)], env_extra={"US_GEN": str(GENERATOR), "US_TMP": cls._tmp.name})
        cls.raw = proc
        cls.r = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.returncode == 0 and proc.stdout.strip() else None

    def setUp(self) -> None:
        self.assertIsNotNone(self.r, f"the lifted helpers did not run: {self.raw.stdout[-3000:]}{self.raw.stderr[-3000:]}")

    def test_t4_gpu_rows_na_fields_torn_tail_and_local_time(self) -> None:
        self.assertEqual(self.r["gpuParse"], {"rows": 5, "torn": True, "why": None, "firstUtc": "2026-11-01T14:00:00.0000000Z", "nullUtil": True,
                                              "nullPower": True, "pstate": "P2"})
        self.assertEqual(self.r["gpuKeptTail"], {"rows": 2, "torn": False})
        self.assertEqual(self.r["gpuSummerUtc"], "2026-07-01T13:00:00.0000000Z", "host-local daylight time converts with its own offset")

    def test_t4_gpu_window_statistics(self) -> None:
        self.assertEqual(self.r["gpuWin"], {"status": "ok", "mean": 60.0, "p95": 80.0, "inWindow": 4, "total": 5})
        self.assertEqual(self.r["gpuNoWin"], {"status": "partial(no_window)", "mean": 69.75, "p95": 99.0, "inWindow": 5, "total": 5})

    def test_t4_gpu_typed_unavailable(self) -> None:
        self.assertEqual(self.r["gpuAmbiguous"], "unavailable(nvidia-smi-ambiguous)")
        self.assertEqual(self.r["gpuEmpty"], "unavailable(nvidia-smi-no-rows)")
        self.assertEqual(self.r["gpuMissingFile"], "unavailable(gpu-util-missing)")
        self.assertEqual(self.r["gpuGarbage"], "unavailable(nvidia-smi-unparseable)")
        self.assertEqual(self.r["gpuAllNa"], "unavailable(not-supported)")
        self.assertEqual(self.r["gpuSkipped"], "skipped_wake_risk")
        self.assertEqual(self.r["gpuStartFailed"], "unavailable(nvidia-smi-missing)")

    def test_t4_cpu_rows_and_window(self) -> None:
        self.assertEqual(self.r["cpuParse"], {"rows": 4, "torn": True, "why": None})
        self.assertEqual(self.r["cpuWin"], {"status": "ok", "total": 50.0, "cores": 3.0, "inWindow": 2, "rows": 4, "pids": "1234"})
        self.assertEqual(self.r["cpuNoWin"], {"status": "partial(no_window)", "total": 50.0, "cores": 4.667, "inWindow": 4, "pids": "1234;1240"})
        self.assertEqual(self.r["cpuEmpty"], "unavailable(cpu-util-no-rows)")
        self.assertEqual(self.r["cpuNoHeader"], "unavailable(cpu-util-unparseable)")
        self.assertEqual(self.r["cpuStartFailed"], "unavailable(cpu-sampler-start-failed)")

    def test_t4_summary_scalars(self) -> None:
        ok = self.r["scalarsOk"]
        self.assertEqual(list(ok), SCALARS)
        self.assertEqual([ok[k] for k in SCALARS], ["ok", None, "ok", 60.0, 80.0, 4, 5, None, 50.0, 3.0, 2, 4])
        nw = self.r["scalarsNoWindow"]
        self.assertEqual((nw["utilSampleStatus"], nw["utilSampleReason"], nw["gpuUtilStatus"], nw["gpuUtilMean"]), ("partial", "no_window", "partial(no_window)", 69.75))
        sk = self.r["scalarsSkipped"]
        self.assertEqual((sk["utilSampleStatus"], sk["utilSampleReason"], sk["gpuUtilStatus"], sk["gpuUtilMean"], sk["gpuRowsInWindow"], sk["cpuTotalMean"]),
                         ("ok", "gpu:skipped_wake_risk(dgpu-D3)", "skipped_wake_risk", None, 0, 50.0))
        none = self.r["scalarsNone"]
        self.assertEqual((none["utilSampleStatus"], none["utilSampleReason"], none["cpuTotalMean"], none["appCpuMeanCores"]),
                         ("unavailable", "gpu:unavailable(nvidia-smi-missing)+cpu:unavailable(cpu-sampler-start-failed)", None, None))

    def test_t4_window_from_the_presentmon_bracket(self) -> None:
        self.assertEqual(self.r["window"], {"startUtc": "2026-11-01T14:00:01.5005Z", "endUtc": "2026-11-01T14:00:21.50025Z"})
        self.assertTrue(self.r["windowNull"])
        self.assertTrue(self.r["windowInverted"])

    def test_t5_every_start_failure_is_typed_and_a_skip_starts_nothing(self) -> None:
        self.assertEqual(self.r["startMissing"]["status"], "unavailable")
        self.assertEqual(self.r["startMissing"]["reason"], "nvidia-smi-missing")
        self.assertTrue(self.r["startMissing"]["proc"])
        self.assertIn("wake", self.r["startMissing"]["disclosure"])
        self.assertEqual(self.r["startEmptyPath"], {"status": "unavailable", "reason": "nvidia-smi-missing"})
        self.assertEqual(self.r["startD3"], {"status": "skipped_wake_risk", "reason": "dgpu-D3", "proc": True, "dState": "D3", "file": False})
        self.assertEqual(self.r["startDThrows"], {"status": "skipped_wake_risk", "reason": "pnp-read-failed", "proc": True})
        self.assertEqual(self.r["startDAmbiguous"], {"status": "skipped_wake_risk", "reason": "multi-dgpu-ambiguous", "proc": True})
        self.assertEqual(self.r["startNoDgpu"], {"status": "unavailable", "reason": "no-dgpu", "proc": True})
        self.assertEqual(self.r["cpuStartMissing"], {"status": "unavailable", "reason": "cpu-sampler-start-failed", "proc": True})

    def test_t5_a_throwing_counter_still_writes_rows_and_ends_typed(self) -> None:
        got = self.r["cpuLoopThrowingCounter"]
        self.assertEqual(got["endedBy"], "deadline")
        self.assertEqual(got["header"], "utc,cpuTotalPct,appPid,appCpuCores")
        self.assertGreaterEqual(got["rows"], 1)
        self.assertRegex(got["firstRow"], r"^\d{4}-\d\d-\d\dT[^,]+Z,,,$", "a throwing counter is an empty field, never a throw or a 0")
        self.assertGreaterEqual(got["errors"], 1)
        self.assertEqual(self.r["stopNull"], {"stopped": False, "why": "not-started"})
        self.assertEqual(self.r["stopAllNothing"], "not-started")


UTIL_STUB_GENERATOR = STUB_GENERATOR.replace("$AutomationSettingsDir,$GpuPreLaunches)", "$AutomationSettingsDir,$GpuPreLaunches,[switch]$UtilSample)")


@requires_windows_pwsh
class UtilSampleRunnerMappingTests(RunnerHarness, unittest.TestCase):
    def setUp(self) -> None:
        self.assertEqual(UTIL_STUB_GENERATOR.count("[switch]$UtilSample"), 1, "the stub generator's param list moved")
        self.make_harness()
        self.gen.write_text(UTIL_STUB_GENERATOR, encoding="utf-8")

    def spec_with(self, args: dict | None, leg_id: str) -> Path:
        path = self.write_spec(card="CUDA-HEADROOM-UTIL-SAMPLER-1", leg_id=leg_id)
        spec = json.loads(path.read_text(encoding="utf-8"))
        if args is not None:
            spec["generatorArgs"] = args
        path.write_text(json.dumps(spec), encoding="utf-8")
        return path

    def test_t6_only_a_json_true_passes_util_sample(self) -> None:
        for name, args, want in (("true", {"utilSample": True}, "True"), ("string", {"utilSample": "true"}, None), ("false", {"utilSample": False}, None),
                                 ("absent", {}, None), ("none", None, None)):
            with self.subTest(case=name):
                if self.gen_log.exists():
                    self.gen_log.unlink()
                proc, _receipt, _submitted = self.run_leg("bachelor", self.spec_with(args, f"us-{name}"))
                calls = self.generator_calls()
                self.assertTrue(calls, proc.stdout[-2000:] + proc.stderr[-2000:])
                fields = dict(kv.split("=", 1) for kv in calls[-1].split(";") if "=" in kv)
                self.assertEqual(fields.get("UtilSample"), want, calls[-1])


class UtilSampleSpecTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            import jsonschema  # noqa: F401
        except ImportError:
            self.skipTest("jsonschema is not installed")
        self.schema = json.loads((DV / "leg-spec.schema.json").read_text(encoding="utf-8"))

    def test_t7_the_field_is_a_boolean_and_a_string_is_refused(self) -> None:
        import jsonschema
        node = self.schema["properties"]["generatorArgs"]["properties"]["utilSample"]
        self.assertEqual(node["type"], "boolean")
        self.assertIn("Absent or false", node["description"])
        base = json.loads((DV / "legs" / "m16-1243-headroom-unpaced-windowed-s1.json").read_text(encoding="utf-8"))
        good = json.loads(json.dumps(base))
        good["generatorArgs"]["utilSample"] = True
        jsonschema.validate(good, self.schema)
        bad = json.loads(json.dumps(base))
        bad["generatorArgs"]["utilSample"] = "true"
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(bad, self.schema)

    def test_t7_the_utilsample_leg_is_the_headroom_leg_plus_the_sampler(self) -> None:
        import jsonschema
        base = json.loads((DV / "legs" / "m16-1243-headroom-unpaced-windowed-s1.json").read_text(encoding="utf-8"))
        for rel in LEGS:
            with self.subTest(leg=rel):
                spec = json.loads((DV / rel).read_text(encoding="utf-8"))
                jsonschema.validate(spec, self.schema)
                self.assertEqual((spec["legId"], spec["card"]), (Path(rel).stem, "CUDA-HEADROOM-UTIL-SAMPLER-1"))
                self.assertEqual(spec["generatorArgs"].pop("utilSample"), True)
                self.assertEqual(spec["backends"], ["cuda", "cpu"])
                for key in ("legId", "card"):
                    spec.pop(key), base.pop(key, None)
                self.assertEqual(spec, base, "everything else, criteria included, is the headroom leg's")
                base = json.loads((DV / "legs" / "m16-1243-headroom-unpaced-windowed-s1.json").read_text(encoding="utf-8"))

    def test_t7_the_card_has_venue_roles(self) -> None:
        roles = json.loads((DV / "venues.json").read_text(encoding="utf-8"))["roles"]
        self.assertEqual(roles["CUDA-HEADROOM-UTIL-SAMPLER-1"], {"bachelor": "acceptance", "ultra-magnus": "supplementary"})


if __name__ == "__main__":
    unittest.main()
