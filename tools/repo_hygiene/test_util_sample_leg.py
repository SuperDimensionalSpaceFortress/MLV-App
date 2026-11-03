"""Tests for CUDA-HEADROOM-UTIL-SAMPLER-1: the opt-in GPU/CPU utilisation sampler for venue legs (generatorArgs.utilSample -> -UtilSample).

WHAT IS PINNED
  * T1 byte identity, from this tree only (r5 K1: no frozen-commit byte pin, catalogue P-01): a copy of the generator with its one seam
    ($utilSampleEmit) turned off emits, given -UtilSample, the job without it, and a job without -UtilSample carries no sampler text;
  * T2 presence: with -UtilSample the five bracketed regions are there exactly once each, carry the exact nvidia-smi query once and both CSV
    names, start after the pre-launch PresentMon stop sites (session-locked, keep-alive) and stop before every post-launch one, and stripping
    them gives back the job without -UtilSample;
  * T3 anchors: a template that lost the measured launch line (or its done line) is the typed DUAL_VENUE_UTIL_SAMPLE_ANCHOR_MISSING refusal;
  * T4 the pure parsers and summaries ([N/A], a torn last row, rows outside the window, two rows per timestamp, an empty file, no window,
    host-local nvidia-smi time to UTC), lifted from the generator by AST;
  * T5 fail open: a missing nvidia-smi, a D3 dGPU on a cpu leg, a D-state read that throws, a CPU counter that throws and a sampler process
    that cannot start each give a typed status and never throw, and a skipped start starts no process;
  * T6 forwarding: Invoke-VenueLeg passes UtilSample only for a JSON true (stub generator), never for "true", false or absent;
  * T7 schema: generatorArgs.utilSample is a boolean (a string is refused), and the one committed utilsample leg validates.
No test launches MLVApp (owner rule 2026-10-11): the generator only writes job text, and the runner runs against stubs.
"""

from __future__ import annotations

import base64
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

SEAM_ON = re.compile(r"(?m)^\$utilSampleEmit = \[bool\]\$UtilSample(?=\r?$)")
SEAM_OFF = "$utilSampleEmit = $false"
OPEN = "# CUDA-HEADROOM-UTIL-SAMPLER-1 >>>"
CLOSE = "# CUDA-HEADROOM-UTIL-SAMPLER-1 <<<"
REGIONS = 5
REGION = re.compile(r"(?ms)^[ \t]*" + re.escape(OPEN) + r"\n.*?^[ \t]*" + re.escape(CLOSE) + r"\n")
UTIL_TOKENS = ("utilsample", "util-sample", "gpu-util.csv", "cpu-util.csv")
QUERY = ("--query-gpu=timestamp,utilization.gpu,utilization.memory,clocks.sm,pstate,power.draw --format=csv,noheader,nounits -i 0 -lms 250")
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


# r5 K1 (catalogue P-01): a frozen-commit byte pin compares this tree's output with a fixed commit's, so any later unrelated edit to the generator
# reds every PR. These are its shapes: a full commit id literal, and a `git show <rev>:<path>` read. The tokens are split so this scan is clean.
FROZEN_SHA = re.compile(r"(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])")
FROZEN_SHOW = re.compile(r"[\"']" + "sh" + r"ow[\"']\s*,\s*f?[\"'][^\"']*:")


def frozen_pins(text: str) -> list[str]:
    return [m.group(0) for m in FROZEN_SHA.finditer(text)] + [m.group(0) for m in FROZEN_SHOW.finditer(text)]


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
        requests: list[tuple[str, Path, list[str]]] = [
            ("default", GENERATOR, []), ("cpu", GENERATOR, CPU_SHAPE), ("cuda", GENERATOR, CUDA_SHAPE),
            ("us-default", GENERATOR, ["-UtilSample"]), ("us-cpu", GENERATOR, [*CPU_SHAPE, "-UtilSample"]), ("us-cuda", GENERATOR, [*CUDA_SHAPE, "-UtilSample"]),
            ("cp-cpu", GENERATOR, [*CPU_SHAPE, *CP_ARGS]), ("us-cp-cpu", GENERATOR, [*CPU_SHAPE, *CP_ARGS, "-UtilSample"]),
            ("off-default", cls.off_generator, []), ("off-us-cpu", cls.off_generator, [*CPU_SHAPE, "-UtilSample"]),
            ("off-us-cuda", cls.off_generator, [*CUDA_SHAPE, "-UtilSample"]),
            ("no-launch", cls.no_launch, [*CUDA_SHAPE, "-UtilSample"]), ("no-done", cls.no_done, [*CUDA_SHAPE, "-UtilSample"]),
            ("no-launch-plain", cls.no_launch, CUDA_SHAPE),
        ]
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

    def test_t1_the_seam_carries_every_emission_and_a_job_without_util_sample_has_no_sampler_text(self) -> None:
        self.assertEqual(self.seam_sites, 1, "the generator must carry exactly one $utilSampleEmit seam line")
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
                # adversary 9d648094 A1/A6/A7: the window comes from the run log, the stop is proven, the stop record reaches the summaries
                region3 = text[starts[2]:text.index(CLOSE, starts[2])]
                self.assertIn("Get-UtilSamplePlayWindow $rawLog", region3)
                self.assertNotIn("windowStartMs", region3)
                self.assertIn("$utilSampleStopGpu", region3)
                # r5 B2: the window carries the CPU child's clock witness before either summary reads it
                self.assertIn("$utilSampleWindow['clockWhy'] = Get-UtilSampleClockWitness $utilSampleCpuParsed $utilSampleCpuStart $utilSampleWindow", region3)
                self.assertLess(region3.index("Get-UtilSampleClockWitness"), region3.index("Get-UtilSampleGpuSummary"))
                # r5 B3: only a file the job created is published
                self.assertEqual(text.count("if ($utilSampleGpuStart.owned -and (Test-Path -LiteralPath $utilSampleGpuCsv -PathType Leaf))"), 1)
                self.assertEqual(text.count("if ((Test-UtilSampleCpuOwned $utilSampleCpuStart $utilSampleStopRecord) -and (Test-Path -LiteralPath $utilSampleCpuCsv -PathType Leaf))"), 1)
                # adversary 01d6ad51 A5: the CPU child binds the app by the job's own exe path
                self.assertIn("$utilSampleGpuPid $utilSampleGpuTicks ([string]$exePath)", text)
                self.assertIn("$utilSampleStopped = Test-UtilSampleStopped $utilSampleStopRecord", text[starts[1]:text.index(CLOSE, starts[1])])
                self.assertLess(text.index("$rawLog = [IO.File]::ReadAllText($logPath)"), starts[2], "the run log is read before the scalars")
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
        # r4 key hardening: the encoded CPU child body itself parses (the comment / indentation stripping cannot break it unseen) and carries the
        # clock read and the loop with the r5 row shape
        child = base64.b64decode(encoded.group(1)).decode("utf-16-le")
        child_path = self.tmp / "us-child.ps1"
        child_path.write_text(child, encoding="utf-8")
        proc = run_pwsh(["-File", str(script), str(child_path)])
        self.assertIn("BAD=0", proc.stdout, proc.stdout + proc.stderr)
        # adversary f685c478 A2: the child's exit 0 (its ownership signal) is emitted only once its CreateNew succeeded
        for name in ("function Read-UtilSampleClock", "function Invoke-UtilSampleCpuLoop", "utc,cpuTotalPct,appPid,appCpuCores,fromUtc,clockStepMs,intervalMs,counterMs",
                     "if ($null -ne $us -and $us.created) { exit 0 }\nexit 6"):
            self.assertIn(name, child)


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
$win = [ordered]@{ startUtc = $ws; endUtc = $we; clockWhy = $null }
$H = "utc,cpuTotalPct,appPid,appCpuCores,fromUtc,clockStepMs,intervalMs,counterMs`n"
# one r6 cpu-util.csv row: its interval fromUtc..utc stated, its clock step, its snapshot span and its counter-read span ($cm: $null = the
# snapshot span, i.e. a counter read and a snapshot that sat together; '' = no bound counter interval)
function CpuRow([DateTime]$u, $t, $p, $c, [DateTime]$f, $s = '0', $iv = $null, $cm = $null) { if ($null -eq $iv) { $iv = ($u - $f).TotalMilliseconds }; if ($null -eq $cm) { $cm = $iv }; "$($u.ToString('o')),$t,$p,$c,$($f.ToString('o')),$s,$iv,$cm`n" }
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
$cpuText = $H + "2026-11-01T13:59:59.5000000Z,10,,,2026-11-01T13:59:59.0000000Z,0,500,500`n2026-11-01T14:00:00.1000000Z,50,1234,2.5,2026-11-01T13:59:59.5000000Z,0,600,600`n" +
           "2026-11-01T14:00:00.6000000Z,,1234,3.5,2026-11-01T14:00:00.1000000Z,0,500,500`n2026-11-01T14:00:01.5000000Z,90,1234;1240,8,2026-11-01T14:00:00.6000000Z,0,900,900`n2026-11-01T14:00:0"
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
# adversary 9d648094 A1/A2: the window is the measured session's Play span from the run log, never PresentMon's capture offsets
$log = "[2026-11-01T13:59:58.000Z] [info] [main] app.start`n[2026-11-01T14:00:01.000Z] [info] [main] playback_smoke.start session=1`n" +
       "[2026-11-01T14:00:05.000Z] [info] [main] playback_smoke.summary session=1 frames=10 elapsed_ms=4000`n" +
       "[2026-11-01T14:00:09.000Z] [info] [main] playback_smoke.measured_session id=2`n[2026-11-01T14:00:10.001Z] [info] [main] playback_smoke.start session=2`n" +
       "[2026-11-01T14:00:30.200Z] [info] [main] playback_smoke.summary session=2 frames=480 elapsed_ms=20000 fps=24`n"
$pw2 = Get-UtilSamplePlayWindow $log
$r['window'] = @{ startUtc = $pw2.startUtc.ToString('o'); endUtc = $pw2.endUtc.ToString('o'); session = $pw2.session }
$pw1 = Get-UtilSamplePlayWindow (($log -split "`n" | Where-Object { $_ -notmatch 'measured_session' }) -join "`n")
$r['windowFirstSummary'] = @{ startUtc = $pw1.startUtc.ToString('o'); endUtc = $pw1.endUtc.ToString('o'); session = $pw1.session }
$r['windowNull'] = $null -eq (Get-UtilSamplePlayWindow ($log -replace 'playback_smoke\.start session=2', 'other'))
$r['windowNoElapsed'] = $null -eq (Get-UtilSamplePlayWindow ($log -replace 'elapsed_ms=20000', 'elapsed=20000'))
$r['windowEmpty'] = $null -eq (Get-UtilSamplePlayWindow '')
$sb = [Text.StringBuilder]::new($H)
for ($i = 1; $i -le 30; $i++) { if ($i -le 10) { $tv = 90; $cv = 9 } else { $tv = 10; $cv = 1 }; $iu = [DateTime]::new(2026, 11, 1, 14, 0, $i, 0, [DateTimeKind]::Utc); [void]$sb.Append((CpuRow $iu $tv 1234 $cv $iu.AddSeconds(-1))) }
$c30 = Get-UtilSampleCpuSummary (ConvertFrom-UtilSampleCpuCsv $sb.ToString()) ([ordered]@{ status = 'running'; reason = $null }) ([ordered]@{ startUtc = $pw2.startUtc; endUtc = $pw2.endUtc; clockWhy = $null })
$r['cpuPlayOnly'] = @{ status = $c30.status; total = $c30.totalMean; cores = $c30.appCoresMean; inWindow = $c30.rowsInWindow }
# A3: the repeated fall-back hour is unparseable, never placed at one of its two instants
$r['gpuAmbiguousLocal'] = (ConvertFrom-UtilSampleGpuCsv "2026/11/01 01:30:00.000, 40, 10, 1800, P2, 50.00`n" $cst).why
# A7: a sampler that exited before the stop does not cover the window
$r['gpuExitedEarly'] = (Get-UtilSampleGpuSummary $g ([ordered]@{ status = 'running'; reason = $null }) $win ([ordered]@{ stopped = $true; exitedBeforeStop = $true; exitCode = 0 })).status
$r['cpuExitedEarly'] = (Get-UtilSampleCpuSummary $c ([ordered]@{ status = 'running'; reason = $null }) $win ([ordered]@{ stopped = $true; killed = $false; exitedBeforeStop = $true; exitCode = 0 })).status
$r['cpuExitNonZero'] = (Get-UtilSampleCpuSummary $c ([ordered]@{ status = 'running'; reason = $null }) $win ([ordered]@{ stopped = $true; killed = $false; exitedBeforeStop = $false; exitCode = 3 })).status
$r['cpuStopClean'] = (Get-UtilSampleCpuSummary $c ([ordered]@{ status = 'running'; reason = $null }) $win ([ordered]@{ stopped = $true; killed = $false; exitedBeforeStop = $false; exitCode = 0 })).status
# A8: an overflowing mean is unavailable(non-finite), never ok without a number
$big = [ordered]@{ rows = [Collections.Generic.List[object]]::new(); why = $null }
foreach ($ms in 100, 200, 300) { $big.rows.Add([ordered]@{ utc = $ws.AddMilliseconds($ms); util = 1.7e308; total = 1.7e308; cores = 1.7e308; pids = '1'; fromUtc = $ws.AddMilliseconds($ms - 50); step = 0.0; intervalMs = 50.0; counterMs = 50.0 }) }
$r['gpuNonFinite'] = (Get-UtilSampleGpuSummary $big ([ordered]@{ status = 'running'; reason = $null }) $win).status
$r['cpuNonFinite'] = (Get-UtilSampleCpuSummary $big ([ordered]@{ status = 'running'; reason = $null }) $win).status
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
$loop = Invoke-UtilSampleCpuLoop (Join-Path $t 'c2.csv') (Join-Path $t 'c2.stop') 'no-such-process-name-xyz' 400 1300 { throw 'counter exploded' } 0 0 0 0
$lines = @([IO.File]::ReadAllLines((Join-Path $t 'c2.csv')))
$r['cpuLoopThrowingCounter'] = @{ endedBy = $loop.endedBy; header = $lines[0]; rows = ($lines.Count - 1); firstRow = $(if ($lines.Count -gt 1) { $lines[1] }); errors = $loop.counterErrors }
# A10: a live pid whose start time is not the parent's is a reused pid: parent-gone
$loopReused = Invoke-UtilSampleCpuLoop (Join-Path $t 'c3.csv') (Join-Path $t 'c3.stop') 'no-such-process-name-xyz' 400 5000 { 1 } $PID 12345 0 0
$loopSame = Invoke-UtilSampleCpuLoop (Join-Path $t 'c4.csv') (Join-Path $t 'c4.stop') 'no-such-process-name-xyz' 400 900 { 1 } $PID ((Get-Process -Id $PID).StartTime.ToUniversalTime().Ticks) 0 0
$r['cpuLoopParent'] = @{ reused = $loopReused.endedBy; same = $loopSame.endedBy }
# A4 / A5: a cuda leg reads the PnP record too (one NVIDIA adapter or no process); the backend is compared lower-cased
$ca = Start-UtilSampleGpu 'cuda' $pw (Join-Path $t 'g6.csv') { @{ state = $null; reason = 'multi-dgpu-ambiguous' } }
$r['cudaAmbiguous'] = @{ status = $ca.status; reason = $ca.reason; proc = ($null -eq $ca.proc) }
$cn0 = Start-UtilSampleGpu 'cuda' $pw (Join-Path $t 'g7.csv') { @{ state = 'absent'; reason = 'no-dgpu' } }
$r['cudaNoDgpu'] = @{ status = $cn0.status; reason = $cn0.reason; proc = ($null -eq $cn0.proc) }
$cu = Start-UtilSampleGpu 'CUDA' $noSmi (Join-Path $t 'g8.csv') { @{ state = 'D3'; reason = $null } }
$r['cudaUpper'] = @{ status = $cu.status; reason = $cu.reason; disclosure = $cu.wakeDisclosure }
# A6: a Kill that throws once is retried; a record with a side still running is not stopped
$script:kills = 0
$dbl = [pscustomobject]@{ exited = $false }
$dbl | Add-Member -MemberType ScriptProperty -Name HasExited -Value { $this.exited }
$dbl | Add-Member -MemberType ScriptProperty -Name ExitCode -Value { 1 }
$dbl | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value { param($ms) $this.exited }
$dbl | Add-Member -MemberType ScriptMethod -Name Kill -Value { $script:kills++; if ($script:kills -eq 1) { throw [ComponentModel.Win32Exception]::new(5) }; $this.exited = $true }
$sk = Stop-UtilSampleProcess $dbl 100 $true
$r['stopKillRetry'] = @{ stopped = $sk.stopped; killed = $sk.killed; kills = $script:kills; early = $sk.exitedBeforeStop }
$r['stoppedTest'] = @{ both = (Test-UtilSampleStopped ([ordered]@{ gpu = $sk; cpu = [ordered]@{ stopped = $false; why = 'not-started' } }));
    running = (Test-UtilSampleStopped ([ordered]@{ gpu = [ordered]@{ stopped = $false; why = 'stop-failed: x' }; cpu = $sk })); none = (Test-UtilSampleStopped $null) }
$early = [pscustomobject]@{ HasExited = $true; ExitCode = 0 }
$se = Stop-UtilSampleProcess $early 100 $true
$r['stopEarly'] = @{ stopped = $se.stopped; early = $se.exitedBeforeStop; killed = $se.killed }
$st = Stop-UtilSampleProcess $null 100
$r['stopNull'] = @{ stopped = $st.stopped; why = $st.why }
$r['stopAllNothing'] = (Stop-UtilSampleAll ([ordered]@{ status = 'unavailable'; proc = $null }) ([ordered]@{ status = 'unavailable'; proc = $null }) { param($p) throw 'no stop file' } 100).gpu.why
# r3 adversary 693bf053 A1: a summary stamp that disagrees with start + elapsed_ms (a stepped host clock) is no window, so no ok answer
$a1log = "[2026-11-02T14:00:00.000Z] [info] [app] playback_smoke.start session=2`n[2026-11-02T14:00:00.001Z] [info] [app] playback_smoke.measured_session id=2`n" +
         "[2026-11-02T14:01:20.000Z] [info] [app] playback_smoke.summary session=2 reason=play-stop elapsed_ms=20000`n"
$a1w = Get-UtilSamplePlayWindow $a1log
$a1csv = $H + "2026-11-02T14:00:01.0000000Z,10,1234,1,2026-11-02T14:00:00.0000000Z,0,1000,1000`n2026-11-02T14:01:02.0000000Z,90,1234,9,2026-11-02T14:00:01.0000000Z,0,61000,61000`n2026-11-02T14:01:19.0000000Z,90,1234,9,2026-11-02T14:01:02.0000000Z,0,17000,17000`n"
$a1c = Get-UtilSampleCpuSummary (ConvertFrom-UtilSampleCpuCsv $a1csv) ([ordered]@{ status = 'running'; reason = $null }) $a1w ([ordered]@{ stopped = $true; killed = $false; exitedBeforeStop = $false; exitCode = 0 })
$r['r3A1'] = @{ windowNull = ($null -eq $a1w); status = $a1c.status; inWindow = $a1c.rowsInWindow; total = $a1c.rowsTotal }
$a1ok = Get-UtilSamplePlayWindow ($a1log -replace '14:01:20\.000Z', '14:00:20.400Z')
$r['r3A1Within'] = @{ windowNull = ($null -eq $a1ok); end = $(if ($a1ok) { $a1ok.endUtc.ToString('o') }) }
$a1nostamp = Get-UtilSamplePlayWindow ($a1log -replace '\[2026-11-02T14:01:20\.000Z\]', '[not-a-time]')
$r['r3A1BadSummaryStamp'] = ($null -eq $a1nostamp)
# r3 A3: a marker quoted inside a source-location suffix is not the measured-session / summary line (only the log sink's line prefix counts)
$a3log = "[2026-11-02T14:00:00.000Z] [info] [0x1] interaction_trace event=automation.start (C:/b playback_smoke.measured_session id=1 x/MainWindow.cpp:1)`n" +
         "[2026-11-02T14:00:01.000Z] [info] [0x1] playback_smoke.start session=1 position=0`n[2026-11-02T14:00:05.000Z] [info] [0x1] playback_smoke.summary session=1 reason=play-stop elapsed_ms=4000`n" +
         "[2026-11-02T14:00:10.000Z] [info] [0x1] playback_smoke.start session=2 position=0`n[2026-11-02T14:00:10.001Z] [info] [0x1] playback_smoke.measured_session id=2`n" +
         "[2026-11-02T14:00:30.000Z] [info] [0x1] playback_smoke.summary session=2 reason=play-stop elapsed_ms=20000`n"
$a3w = Get-UtilSamplePlayWindow $a3log
$r['r3A3Measured'] = @{ session = $(if ($a3w) { $a3w.session }); start = $(if ($a3w) { $a3w.startUtc.ToString('o') }) }
$a3log2 = "[2026-11-02T14:00:00.000Z] [info] [0x1] trace (C:/b playback_smoke.summary session=9 elapsed_ms=5 x/MainWindow.cpp:1)`n" +
          "[2026-11-02T14:00:01.000Z] [info] [0x1] playback_smoke.start session=1 position=0`n[2026-11-02T14:00:05.000Z] [info] [0x1] playback_smoke.summary session=1 reason=play-stop elapsed_ms=4000`n"
$a3w2 = Get-UtilSamplePlayWindow $a3log2
$r['r3A3Summary'] = @{ session = $(if ($a3w2) { $a3w2.session }) }
# r3 A2: a sampler whose stop failed (stopped=false, a live process after both Kills) is a typed failure, never ok / utilSampleStatus ok
$a2stop = [ordered]@{ stopped = $false; killed = $false; exitedBeforeStop = $false; exitCode = $null; why = 'stop-failed: Access is denied' }
$a2g = Get-UtilSampleGpuSummary $g ([ordered]@{ status = 'running'; reason = $null }) $win $a2stop
$a2c = Get-UtilSampleCpuSummary $c ([ordered]@{ status = 'running'; reason = $null }) $win $a2stop
$a2s = Get-UtilSampleScalars $a2g $a2c $true
$r['r3A2'] = @{ gpu = $a2g.status; cpu = $a2c.status; scalarStatus = $a2s.utilSampleStatus; scalarReason = $a2s.utilSampleReason }
$a2ns = [ordered]@{ stopped = $false; killed = $false; exitedBeforeStop = $false; exitCode = $null; why = 'not-started' }
$r['r3A2NotStarted'] = @{ gpu = (Get-UtilSampleGpuSummary $g ([ordered]@{ status = 'running'; reason = $null }) $win $a2ns).status }
# r3 adversary e4bc0d80 B1: a CPU child that exits on its own AFTER the stop request (it saw the stop file) was running when the stop came: not exitedBeforeStop
$b1 = [pscustomobject]@{ exited = $false }
$b1 | Add-Member -MemberType ScriptProperty -Name HasExited -Value { $this.exited }
$b1 | Add-Member -MemberType ScriptProperty -Name ExitCode -Value { 0 }
$b1 | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value { param($ms) $this.exited }
$b1 | Add-Member -MemberType ScriptMethod -Name Kill -Value { $this.exited = $true }
$b1r = Stop-UtilSampleAll ([ordered]@{ proc = $null }) ([ordered]@{ proc = $b1 }) { $b1.exited = $true } 100
$b1s = Get-UtilSampleCpuSummary $c ([ordered]@{ status = 'running'; reason = $null }) $win $b1r.cpu
$b0 = [pscustomobject]@{ exited = $true }
$b0 | Add-Member -MemberType ScriptProperty -Name HasExited -Value { $this.exited }
$b0 | Add-Member -MemberType ScriptProperty -Name ExitCode -Value { 0 }
$b0r = Stop-UtilSampleAll ([ordered]@{ proc = $null }) ([ordered]@{ proc = $b0 }) { } 100
$r['r3B1'] = @{ stopped = $b1r.cpu.stopped; early = $b1r.cpu.exitedBeforeStop; exit = $b1r.cpu.exitCode; status = $b1s.status; inWindow = $b1s.rowsInWindow; preExitedEarly = $b0r.cpu.exitedBeforeStop }
# r3 B2: a CPU row is the mean over the interval ENDING at its stamp; the row whose interval opened before Play started is not Play-only
$b2sb = [Text.StringBuilder]::new($H)
for ($i = 0; $i -le 21; $i++) {
    if ($i -eq 0) { $tv = 100; $cv = 10 } elseif ($i -eq 1) { $tv = 90; $cv = 9 } else { $tv = 0; $cv = 0 }
    $iu = [DateTime]::new(2026, 11, 2, 14, 0, $i, 0, [DateTimeKind]::Utc); [void]$b2sb.Append((CpuRow $iu $tv 1234 $cv $iu.AddSeconds(-1)))
}
$b2win = [ordered]@{ startUtc = [DateTime]::new(2026, 11, 2, 14, 0, 0, 900, [DateTimeKind]::Utc); endUtc = [DateTime]::new(2026, 11, 2, 14, 0, 20, 900, [DateTimeKind]::Utc); clockWhy = $null }
$b2 = Get-UtilSampleCpuSummary (ConvertFrom-UtilSampleCpuCsv $b2sb.ToString()) ([ordered]@{ status = 'running'; reason = $null }) $b2win ([ordered]@{ stopped = $true; killed = $false; exitedBeforeStop = $false; exitCode = 0 })
$r['r3B2'] = @{ status = $b2.status; inWindow = $b2.rowsInWindow; total = $b2.totalMean; cores = $b2.appCoresMean }
# r3 B3: a +60 s host-clock excursion mid-play and back leaves the endpoints agreeing but the sample stamps out of order: the subset is unverified, not ok
$b3win = [ordered]@{ startUtc = [DateTime]::new(2026, 11, 2, 14, 0, 0, 0, [DateTimeKind]::Utc); endUtc = [DateTime]::new(2026, 11, 2, 14, 0, 20, 0, [DateTimeKind]::Utc) }
# r5: the CPU stamps are out of order, so the witness the job computes says unverified, for the GPU summary as well
$b3cpu = ConvertFrom-UtilSampleCpuCsv ($H + "2026-11-02T14:00:01.0000000Z,10,1234,1,2026-11-02T13:59:59.0000000Z,0,2000,2000`n2026-11-02T14:01:02.0000000Z,90,1234,9,2026-11-02T14:00:01.0000000Z,0,61000,61000`n2026-11-02T14:00:21.0000000Z,10,1234,1,2026-11-02T14:00:19.0000000Z,0,2000,2000`n")
$b3gpu = ConvertFrom-UtilSampleGpuCsv "2026/11/02 08:00:01.000, 10, 10, 1200, P0, 40`n2026/11/02 08:01:02.000, 90, 10, 1200, P0, 40`n2026/11/02 08:00:19.000, 10, 10, 1200, P0, 40`n" $cst
$b3run = [ordered]@{ status = 'running'; reason = $null }; $b3stop = [ordered]@{ stopped = $true; killed = $false; exitedBeforeStop = $false; exitCode = 0 }
$b3win['clockWhy'] = Get-UtilSampleClockWitness $b3cpu $b3run $b3win
$b3c = Get-UtilSampleCpuSummary $b3cpu $b3run $b3win $b3stop
$b3g = Get-UtilSampleGpuSummary $b3gpu $b3run $b3win $b3stop
$r['r3B3'] = @{ cpu = $b3c.status; gpu = $b3g.status; scalar = (Get-UtilSampleScalars $b3g $b3c $true).utilSampleStatus }
$b3okcpu = ConvertFrom-UtilSampleCpuCsv ($H + "2026-11-02T14:00:01.0000000Z,10,1234,1,2026-11-02T13:59:59.5000000Z,0,1500,1500`n2026-11-02T14:00:02.0000000Z,20,1234,2,2026-11-02T14:00:01.0000000Z,0,1000,1000`n2026-11-02T14:00:03.0000000Z,30,1234,3,2026-11-02T14:00:02.0000000Z,0,1000,1000`n")
$b3ok = Get-UtilSampleCpuSummary $b3okcpu $b3run ([ordered]@{ startUtc = $b3win.startUtc; endUtc = $b3win.endUtc; clockWhy = $null }) $b3stop
$r['r3B3Steady'] = @{ cpu = $b3ok.status; inWindow = $b3ok.rowsInWindow }
# r4 adversary b544cdb8 A1: a PATH entry on an absent drive is skipped (Join-Path throws there), so System32\nvidia-smi.exe is still found
$a1Drive = @('Z', 'Y', 'X', 'W', 'V', 'U', 'T', 'S', 'R', 'Q') | Where-Object { -not (Test-Path -LiteralPath "$($_):\") } | Select-Object -First 1
$a1Root = Join-Path $t 'a1root'
[void](New-Item -ItemType Directory -Force -Path (Join-Path $a1Root 'System32'))
Set-Content -LiteralPath (Join-Path $a1Root 'System32\nvidia-smi.exe') -Value 'x'
$a1OldPath = $env:PATH; $a1OldRoot = $env:SystemRoot; $a1OldPf = $env:ProgramFiles
try {
    $env:PATH = "$($a1Drive):\removed-toolchain\bin;" + (Join-Path $t 'empty-bin'); $env:SystemRoot = $a1Root; $env:ProgramFiles = Join-Path $t 'no-pf'
    $a1Found = Resolve-UtilSampleNvidiaSmi
    $env:PATH = Join-Path $t 'empty-bin'; $env:SystemRoot = Join-Path $t 'no-root'
    $a1None = Resolve-UtilSampleNvidiaSmi
} finally { $env:PATH = $a1OldPath; $env:SystemRoot = $a1OldRoot; $env:ProgramFiles = $a1OldPf }
$r['r4A1'] = @{ drive = [string]$a1Drive; found = [string]$a1Found; expected = (Join-Path $a1Root 'System32\nvidia-smi.exe'); none = [string]$a1None }
# r4 adversary b544cdb8 A2: one stalled counter read must not be followed by a burst of catch-up rows (each weighted as a whole interval by the summary)
$script:a2n = 0
$a2 = Invoke-UtilSampleCpuLoop (Join-Path $t 'a2.csv') (Join-Path $t 'a2.stop') 'no-such-process-name-xyz' 200 2500 { $script:a2n++; if ($script:a2n -eq 2) { Start-Sleep -Milliseconds 1200 }; 5 } 0 0 0 0
$a2stamps = @([IO.File]::ReadAllLines((Join-Path $t 'a2.csv')) | Select-Object -Skip 1 | ForEach-Object { [DateTime]::Parse(($_ -split ',')[0], [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind) })
$a2min = 1e9
for ($i = 1; $i -lt $a2stamps.Count; $i++) { $a2min = [math]::Min($a2min, ($a2stamps[$i] - $a2stamps[$i - 1]).TotalMilliseconds) }
$r['r4A2'] = @{ rows = $a2stamps.Count; minGapMs = [math]::Round($a2min); endedBy = $a2.endedBy }
# r4 key hardening: an empty SystemRoot no longer ends the search before PATH is walked
$a1OldPath = $env:PATH; $a1OldRoot = $env:SystemRoot
try { $env:SystemRoot = ''; $env:PATH = Join-Path $a1Root 'System32'; $a1NoRoot = Resolve-UtilSampleNvidiaSmi } finally { $env:PATH = $a1OldPath; $env:SystemRoot = $a1OldRoot }
$r['r5EmptyRoot'] = @{ found = [string]$a1NoRoot; expected = (Join-Path $a1Root 'System32\nvidia-smi.exe') }
$inv = [Globalization.CultureInfo]::InvariantCulture; $rk = [Globalization.DateTimeStyles]::RoundtripKind
# ---- r5 B1 (sol r4): a counter read descheduled for 600 ms AFTER it measured (so the row is written late) must not move an interval boundary:
# every row's stated interval (fromUtc .. utc) must contain the span between the two reads it measures (call 1 is the warm-up read)
$script:b1m = [Collections.Generic.List[int64]]::new()
$b1loop = Invoke-UtilSampleCpuLoop (Join-Path $t 'r5b1.csv') (Join-Path $t 'r5b1.stop') 'no-such-process-name-xyz' 200 2200 { $script:b1m.Add([DateTime]::UtcNow.Ticks); if ($script:b1m.Count -eq 3) { Start-Sleep -Milliseconds 600 }; 5 } 0 0 0 0
$b1lines = @([IO.File]::ReadAllLines((Join-Path $t 'r5b1.csv')) | Select-Object -Skip 1)
$b1bad = [Collections.Generic.List[string]]::new(); $b1prev = $null; $b1steps = [Collections.Generic.List[double]]::new()
for ($i = 0; $i -lt $b1lines.Count -and $i + 1 -lt $script:b1m.Count; $i++) {
    $f = $b1lines[$i] -split ','
    $hiT = [DateTime]::Parse($f[0], $inv, $rk).Ticks
    # a 4-column (pre-r5) row states no interval start: its interval is read as opening at the previous row's stamp, as the old summary did
    if ($f.Count -ge 6) { $fromT = [DateTime]::Parse($f[4], $inv, $rk).Ticks; $b1steps.Add([double]::Parse($f[5], $inv)) } else { $fromT = $b1prev }
    if ($null -ne $fromT -and $fromT -gt $script:b1m[$i]) { $b1bad.Add("row$i-opens-late-by-$([math]::Round(($fromT - $script:b1m[$i]) / 1e4))ms") }
    if ($hiT -lt $script:b1m[$i + 1]) { $b1bad.Add("row$i-ends-early") }
    $b1prev = $hiT
}
$r['r5B1Loop'] = @{ rows = $b1lines.Count; bad = @($b1bad); maxStep = $(if ($b1steps.Count) { ($b1steps | Measure-Object -Maximum).Maximum } else { -1 }) }
# r5 B1 (sol r4 exact case): Play .500..20.500; the previous snapshot was at .100, so the 01.100 row (40 %, 4 app cores, all before Play) opened
# before Play whatever time its row was written; rows 02.100..20.100 are zero
$p0 = [DateTime]::new(2026, 11, 2, 14, 0, 0, 0, [DateTimeKind]::Utc)
$b1t = $H + (CpuRow $p0.AddMilliseconds(100) 0 1234 0 $p0.AddMilliseconds(-910)) + (CpuRow $p0.AddMilliseconds(1100) 40 1234 4 $p0.AddMilliseconds(90))
for ($i = 2; $i -le 21; $i++) { $b1t += (CpuRow $p0.AddMilliseconds(1000 * $i + 100) 0 1234 0 $p0.AddMilliseconds(1000 * ($i - 1) + 90)) }
$b1s = Get-UtilSampleCpuSummary (ConvertFrom-UtilSampleCpuCsv $b1t) ([ordered]@{ status = 'running'; reason = $null }) ([ordered]@{ startUtc = $p0.AddMilliseconds(500); endUtc = $p0.AddMilliseconds(20500); clockWhy = $null }) ([ordered]@{ stopped = $true; killed = $false; exitedBeforeStop = $false; exitCode = 0 })
$r['r5B1'] = @{ status = $b1s.status; total = $b1s.totalMean; cores = $b1s.appCoresMean; inWindow = $b1s.rowsInWindow }
# ---- r5 B2 (sol r4): an ordered clock excursion. Loop: a clock stepped +100 ms for 300 ms, then back; the rows' clockStepMs must show it and the
# witness must refuse a window over them
$script:b2t0 = $null
$b2clock = { $n = [DateTime]::UtcNow.Ticks; if ($null -eq $script:b2t0) { $script:b2t0 = $n }; $el = ($n - $script:b2t0) / 1e4; if ($el -ge 500 -and $el -lt 800) { $n + 1000000 } else { $n } }
$b2loop = Invoke-UtilSampleCpuLoop (Join-Path $t 'r5b2.csv') (Join-Path $t 'r5b2.stop') 'no-such-process-name-xyz' 200 1600 { 5 } 0 0 0 0 $b2clock
$b2p = ConvertFrom-UtilSampleCpuCsv ([IO.File]::ReadAllText((Join-Path $t 'r5b2.csv')))
$b2steps = @($b2p.rows | ForEach-Object { $_.step } | Where-Object { $null -ne $_ })
$b2rows = @($b2p.rows)
$b2w = $null
if ($b2rows.Count -gt 0) { $b2w = [ordered]@{ startUtc = $b2rows[0].fromUtc; endUtc = $b2rows[$b2rows.Count - 1].utc } }
$r['r5B2Loop'] = @{ rows = $b2rows.Count; why = $b2p.why; maxStep = $(if ($b2steps.Count) { ($b2steps | Measure-Object -Maximum).Maximum } else { -1 })
    witness = $(if ($b2w) { Get-UtilSampleClockWitness $b2p ([ordered]@{ status = 'running' }) $b2w } else { 'no-rows' }) }
# r5 B2 (sol r4 exact case): Play 14:00:00..14:00:20; a startup GPU sample taken at 13:59:59.950 carries the +100 ms stamp 14:00:00.050 (util 100)
# between 13:59:59.700 and 14:00:00.200, then zeros every 250 ms to 19.950: stamps in order, so only the witness can refuse it
$sgText = "2026/11/02 07:59:59.700, 0, 0, 1800, P2, 50`n2026/11/02 08:00:00.050, 100, 10, 1800, P2, 50`n"
for ($ms = 200; $ms -le 19950; $ms += 250) { $sgText += "$($p0.AddHours(-6).AddMilliseconds($ms).ToString('yyyy/MM/dd HH:mm:ss.fff', $inv)), 0, 0, 1800, P2, 50`n" }
$sg = ConvertFrom-UtilSampleGpuCsv $sgText $cst
function SgCpu($StepRow, $SkipRow, $Last) { $x = $H; for ($i = 0; $i -le $Last; $i++) { if ($i -eq $SkipRow) { continue }; $iu = $p0.AddSeconds($i); $x += (CpuRow $iu 0 1234 0 $iu.AddSeconds(-1) $(if ($i -eq $StepRow) { '100' } else { '0.4' })) }; ConvertFrom-UtilSampleCpuCsv $x }
$run = [ordered]@{ status = 'running'; reason = $null }; $okStop = [ordered]@{ stopped = $true; killed = $false; exitedBeforeStop = $false; exitCode = 0 }
function SgWin($Cpu, $Start) { $w = [ordered]@{ startUtc = $p0; endUtc = $p0.AddSeconds(20) }; $w['clockWhy'] = Get-UtilSampleClockWitness $Cpu $Start $w; $w }
$cStep = SgCpu 1 -1 21; $wStep = SgWin $cStep $run
$cClean = SgCpu -1 -1 21; $wClean = SgWin $cClean $run
$gClean = Get-UtilSampleGpuSummary $sg $run $wClean $okStop
$r['r5B2'] = @{
    gpuStepped = (Get-UtilSampleGpuSummary $sg $run $wStep $okStop).status; cpuStepped = (Get-UtilSampleCpuSummary $cStep $run $wStep $okStop).status
    gpuClean = $gClean.status; gpuCleanMean = $gClean.mean; gpuCleanRows = $gClean.rowsInWindow
    gpuNoChild = (Get-UtilSampleGpuSummary $sg $run (SgWin $cClean ([ordered]@{ status = 'unavailable'; reason = 'cpu-sampler-start-failed' })) $okStop).status
    gpuNoKey = (Get-UtilSampleGpuSummary $sg $run ([ordered]@{ startUtc = $p0; endUtc = $p0.AddSeconds(20) }) $okStop).status
    gap = (SgWin (SgCpu -1 9 21) $run).clockWhy; short = (SgWin (SgCpu -1 -1 12) $run).clockWhy; clean = $wClean.clockWhy
    noWindow = (Get-UtilSampleGpuSummary $sg $run $null $okStop).status }
# ---- r5 B3 (sol r4): nvidia-smi -f truncates its file; an occupied gpu-util.csv is refused before any process starts and keeps its bytes. The
# stand-in executable is whoami.exe (it rejects nvidia-smi's arguments and exits; it never opens the CSV)
$who = Join-Path $env:SystemRoot 'System32\whoami.exe'
$g9 = Join-Path $t 'r5b3-occupied.csv'; [IO.File]::WriteAllText($g9, 'KEEP-UNOWNED')
$b3o = Start-UtilSampleGpu 'cuda' $who $g9 { @{ state = 'D0'; reason = $null } }
if ($null -ne $b3o.proc) { try { $b3o.proc.Kill() } catch { } }
$r['r5B3'] = @{ status = $b3o.status; reason = $b3o.reason; noProc = ($null -eq $b3o.proc); owned = [bool]$b3o.owned; bytes = [IO.File]::ReadAllText($g9) }
$g10 = Join-Path $t 'r5b3-fresh.csv'
$b3f = Start-UtilSampleGpu 'cuda' $who $g10 { @{ state = 'D0'; reason = $null } }
if ($null -ne $b3f.proc) { try { [void]$b3f.proc.WaitForExit(5000); if (-not $b3f.proc.HasExited) { $b3f.proc.Kill() } } catch { } }
$r['r5B3Fresh'] = @{ status = $b3f.status; owned = [bool]$b3f.owned; exists = (Test-Path -LiteralPath $g10) }
if ($null -ne $b3f.hold) { $b3f.hold.Dispose() }
$c9 = Join-Path $t 'r5b3-cpu-occupied.csv'; [IO.File]::WriteAllText($c9, 'KEEP-UNOWNED-CPU')
$b3cs = Start-UtilSampleCpu $who 'AAAA' $c9 (Join-Path $t 'r5b3.stop') 'MLVApp' 0 1000 0 0
if ($null -ne $b3cs.proc) { try { $b3cs.proc.Kill() } catch { } }
$r['r5B3Cpu'] = @{ status = $b3cs.status; reason = $b3cs.reason; noProc = ($null -eq $b3cs.proc); bytes = [IO.File]::ReadAllText($c9) }
# ---- r5 B4 (sol r4 exact case): Play 14:00:00..14:00:20; a read due at 2 s stalled to 6 s, so the rows are 1 s (its interval opened before Play),
# 6 s (100 %, 10 cores, a 5 s interval) and 7..20 s (zero, 1 s each): duration-weighted 500/19 % and 50/19 cores, not the per-row 100/15 and 10/15
$b4t = $H + (CpuRow $p0.AddSeconds(1) 100 1234 10 $p0.AddMilliseconds(-500)) + (CpuRow $p0.AddSeconds(6) 100 1234 10 $p0.AddSeconds(1))
for ($i = 7; $i -le 20; $i++) { $b4t += (CpuRow $p0.AddSeconds($i) 0 1234 0 $p0.AddSeconds($i - 1)) }
$b4 = Get-UtilSampleCpuSummary (ConvertFrom-UtilSampleCpuCsv $b4t) $run ([ordered]@{ startUtc = $p0; endUtc = $p0.AddSeconds(20); clockWhy = $null }) $okStop
$r['r5B4'] = @{ status = $b4.status; total = $b4.totalMean; cores = $b4.appCoresMean; inWindow = $b4.rowsInWindow }
# ---- adversary 01d6ad51 (r5 fix iteration) ----
# A1: a step that lands inside every clock bracket for 300 ms (one read of each pair stepped +100 ms) must still be counted
$script:a1t0 = $null; $script:a1n = 0
$a1clock = { $n = [DateTime]::UtcNow.Ticks; if ($null -eq $script:a1t0) { $script:a1t0 = $n }; $el = ($n - $script:a1t0) / 1e4; if ($el -ge 500 -and $el -lt 800) { $script:a1n++; if ($script:a1n % 2 -eq 0) { return $n + 1000000 } }; $n }
[void](Invoke-UtilSampleCpuLoop (Join-Path $t 'r5a1.csv') (Join-Path $t 'r5a1.stop') 'no-such-process-name-xyz' 200 1600 { 5 } 0 0 0 0 $a1clock)
$a1s = @((ConvertFrom-UtilSampleCpuCsv ([IO.File]::ReadAllText((Join-Path $t 'r5a1.csv')))).rows | ForEach-Object { $_.step } | Where-Object { $null -ne $_ })
$r['advA1'] = @{ rows = $a1s.Count; maxStep = $(if ($a1s.Count) { ($a1s | Measure-Object -Maximum).Maximum } else { -1 }) }
# A2 (exact case): the read begun at 2 s completes at 6 s; rows (end, start, total, cores, intervalMs): (1,0,0,0,1000) (6,1,100,1,5000) (7,2,0,0,1000),
# then zero 1 s rows to 20 s: the 7 s row measured 1 s, not the 5 s its bounds span -> 25 % / 0.25 cores
$a2t = $H + (CpuRow $p0.AddSeconds(1) 0 1234 0 $p0 '0' 1000) + (CpuRow $p0.AddSeconds(6) 100 1234 1 $p0.AddSeconds(1) '0' 5000) + (CpuRow $p0.AddSeconds(7) 0 1234 0 $p0.AddSeconds(2) '0' 1000)
for ($i = 8; $i -le 20; $i++) { $a2t += (CpuRow $p0.AddSeconds($i) 0 1234 0 $p0.AddSeconds($i - 1) '0' 1000) }
$a2w = [ordered]@{ startUtc = $p0; endUtc = $p0.AddSeconds(20) }
$a2p = ConvertFrom-UtilSampleCpuCsv $a2t
$a2w['clockWhy'] = Get-UtilSampleClockWitness $a2p $run $a2w
$a2s = Get-UtilSampleCpuSummary $a2p $run $a2w $okStop
$r['advA2'] = @{ clock = $a2w.clockWhy; status = $a2s.status; total = $a2s.totalMean; cores = $a2s.appCoresMean; inWindow = $a2s.rowsInWindow }
# A3 (GPU): the job's created gpu-util.csv is held: it cannot be deleted or renamed (a hard link swapped in) while nvidia-smi runs, yet a writer
# that truncates and writes it the way nvidia-smi -f does still can
$a3p = Join-Path $t 'r5a3.csv'
$a3o = New-UtilSampleOwnedFile $a3p
$a3del = 'kept'; try { [IO.File]::Delete($a3p); if (-not (Test-Path -LiteralPath $a3p)) { $a3del = 'deleted' } } catch { $a3del = 'refused' }
$a3mv = 'kept'; try { [IO.File]::Move($a3p, (Join-Path $t 'r5a3-moved.csv')); $a3mv = 'moved' } catch { $a3mv = 'refused' }
$a3w = 'refused'; try { $ws2 = [IO.File]::Open($a3p, [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::ReadWrite); $ws2.WriteByte(65); $ws2.Dispose(); $a3w = 'written' } catch { }
if ($null -ne $a3o -and $null -ne $a3o.hold) { $a3o.hold.Dispose() }
# A3 (CPU): a child whose CreateNew meets another writer's file ends (a throw), never loops on, and the bytes stay; only a child still running
# at the stop owns its file
$a3q = Join-Path $t 'r5a3-foreign.csv'; [IO.File]::WriteAllText($a3q, 'KEEP-FOREIGN')
$a3refused = 'not-refused'; try { [void](Invoke-UtilSampleCpuLoop $a3q (Join-Path $t 'r5a3f.stop') 'no-such-process-name-xyz' 200 500 { 5 } 0 0 0 0) } catch { $a3refused = [string]$_.Exception.Message }
$a3own = @(
    (Test-UtilSampleCpuOwned ([ordered]@{ status = 'running' }) ([ordered]@{ cpu = [ordered]@{ stopped = $true; killed = $false; exitedBeforeStop = $false; exitCode = 0 } })),
    (Test-UtilSampleCpuOwned ([ordered]@{ status = 'running' }) ([ordered]@{ cpu = [ordered]@{ stopped = $true; killed = $false; exitedBeforeStop = $true; exitCode = 0 } })),
    (Test-UtilSampleCpuOwned ([ordered]@{ status = 'running' }) $null),
    (Test-UtilSampleCpuOwned ([ordered]@{ status = 'unavailable' }) ([ordered]@{ cpu = [ordered]@{ stopped = $true; exitedBeforeStop = $false } })))
$r['advA3'] = @{ held = ($null -ne $a3o -and $null -ne $a3o.hold); delete = $a3del; move = $a3mv; writer = $a3w; foreign = $a3refused; foreignBytes = [IO.File]::ReadAllText($a3q); owned = @($a3own) }
# A4 / A5: an unreadable CPU time is no zero baseline, and only the job's own executable path counts
$fs = [DateTime]::new(2026, 11, 2, 13, 0, 0, [DateTimeKind]::Utc)
$pa = [pscustomobject]@{ Id = 1234; Path = 'C:\jobA\MLVApp.exe'; StartTime = $fs; TotalProcessorTime = $null }
$pb = [pscustomobject]@{ Id = 1240; Path = 'C:\jobB\MLVApp.exe'; StartTime = $fs; TotalProcessorTime = [TimeSpan]::FromSeconds(100) }
$pc = [pscustomobject]@{ Id = 1234; Path = 'C:\jobA\MLVApp.exe'; StartTime = $fs; TotalProcessorTime = [TimeSpan]::FromSeconds(101) }
$s1 = Get-UtilSampleAppSnapshot @($pa, $pb) 'C:\jobA\MLVApp.exe'
$s2 = Get-UtilSampleAppSnapshot @($pc, $pb) 'c:\JOBA\mlvapp.exe'
$r['advA4A5'] = @{ unreadable = @($s1.Keys).Count; ownOnly = (@($s2.Keys) -join ';'); expected = "1234,$($fs.Ticks)"; noPath = @((Get-UtilSampleAppSnapshot @($pc, $pb) '').Keys).Count }
# ---- adversary f685c478 (r6) ----
# A1 (exact case): Play 14:00:00..14:00:20. Counter baseline at 0, a zero sample at 1, the 100 % sample read at 2, then a deschedule after that counter
# read until the app snapshot at 6; samples resume at 7..21, all zero. Rows (utc, from, total, cores, intervalMs) as the finding states, plus the
# counter-read span the loop writes for each: (1,0,0,0,1000 | 1000) (6,1,100,0.2,5000 | 1000) (7,2,0,0,1000 | 5000) then (i,i-1,0,0,1000 | 1000).
# The counter measured 1 s at 100 % over a 20 s window: 5.0 %, not 25.0; the app cores keep the snapshot weights (0.2 x 5 s / 20 s = 0.05)
$x1t = $H + (CpuRow $p0.AddSeconds(1) 0 1234 0 $p0 '0' 1000 1000) + (CpuRow $p0.AddSeconds(6) 100 1234 0.2 $p0.AddSeconds(1) '0' 5000 1000) + (CpuRow $p0.AddSeconds(7) 0 1234 0 $p0.AddSeconds(2) '0' 1000 5000)
for ($i = 8; $i -le 21; $i++) { $x1t += (CpuRow $p0.AddSeconds($i) 0 1234 0 $p0.AddSeconds($i - 1) '0' 1000 1000) }
$x1p = ConvertFrom-UtilSampleCpuCsv $x1t
$x1w = [ordered]@{ startUtc = $p0; endUtc = $p0.AddSeconds(20) }
$x1w['clockWhy'] = Get-UtilSampleClockWitness $x1p $run $x1w
$x1s = Get-UtilSampleCpuSummary $x1p $run $x1w $okStop
# the same rows with the 6 s row's counter interval unbound (its read was not bracketed, or the read before it failed): fail closed
$x1u = Get-UtilSampleCpuSummary (ConvertFrom-UtilSampleCpuCsv ($x1t -replace '(T14:00:06\.0000000Z,100,[^\n]*,5000),1000', '$1,')) $run $x1w $okStop
$r['r6A1'] = @{ parseWhy = $x1p.why; clock = $x1w.clockWhy; status = $x1s.status; total = $x1s.totalMean; cores = $x1s.appCoresMean; inWindow = $x1s.rowsInWindow; unbound = $x1u.status }
# A1 (loop): the snapshot after the second row's counter read stalls 1 s (the finding's deschedule); every row's counterMs must be the span between
# the two counter reads it measured (recorded by the counter itself), not the snapshots' span
$script:x1n = 0; $script:x1cm = [Collections.Generic.List[double]]::new(); $x1sw = [Diagnostics.Stopwatch]::StartNew()
$x1loop = @(Invoke-UtilSampleCpuLoop (Join-Path $t 'r6a1.csv') (Join-Path $t 'r6a1.stop') 'no-such-process-name-xyz' 200 2600 { $script:x1cm.Add($x1sw.Elapsed.TotalMilliseconds); 5 } 0 0 0 0 $null '' { $script:x1n++; if ($script:x1n -eq 3) { Start-Sleep -Milliseconds 1000 }; @{} })
$x1lines = @([IO.File]::ReadAllLines((Join-Path $t 'r6a1.csv')) | Select-Object -Skip 1)
$x1bad = [Collections.Generic.List[string]]::new(); $x1stall = $null
for ($i = 0; $i -lt $x1lines.Count -and $i + 1 -lt $script:x1cm.Count; $i++) {
    $f = $x1lines[$i] -split ','
    $want = $script:x1cm[$i + 1] - $script:x1cm[$i]
    if ($f.Count -lt 8 -or $f[7] -eq '') { $x1bad.Add("row$i-no-counterMs"); continue }
    $got = [double]::Parse($f[7], $inv)
    if ([math]::Abs($got - $want) -gt 25) { $x1bad.Add("row$i-counterMs-$([math]::Round($got))-measured-$([math]::Round($want))") }
    if ([double]::Parse($f[6], $inv) -ge 900 -and $null -eq $x1stall) { $x1stall = @{ intervalMs = [math]::Round([double]::Parse($f[6], $inv)); counterMs = [math]::Round($got) } }
}
$r['r6A1Loop'] = @{ rows = $x1lines.Count; bad = @($x1bad); stall = $x1stall; outputs = $x1loop.Count; created = [bool]$x1loop[-1].created }
# A2 (exact case, in-memory double): the CPU precheck saw no file; the child started; a second writer created cpu-util.csv holding KEEP-FOREIGN. The
# stop reads HasExited=false, then the child refuses CreateNew and exits 1 before the stop completes: stopped=true, exitedBeforeStop=false, exitCode=1
function New-ChildDouble([bool]$ExitsOnStop) {
    $d = [pscustomobject]@{ exited = $false; code = $null; onStop = $ExitsOnStop }
    $d | Add-Member -MemberType ScriptProperty -Name HasExited -Value { $this.exited }
    $d | Add-Member -MemberType ScriptProperty -Name ExitCode -Value { $this.code }
    $d | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value { param($ms) $this.exited }
    $d | Add-Member -MemberType ScriptMethod -Name Kill -Value { $this.exited = $true; $this.code = -1 }
    $d
}
$y2dir = Join-Path $t 'r6a2'; [void](New-Item -ItemType Directory -Force -Path $y2dir); $y2pub = Join-Path $t 'r6a2-pub'; [void](New-Item -ItemType Directory -Force -Path $y2pub)
$y2csv = Join-Path $y2dir 'cpu-util.csv'
$y2d = New-ChildDouble $true
$y2rec = Stop-UtilSampleAll ([ordered]@{ proc = $null }) ([ordered]@{ proc = $y2d }) { [IO.File]::WriteAllText($y2csv, 'KEEP-FOREIGN'); $y2d.exited = $true; $y2d.code = 1 } 100
$y2own = Test-UtilSampleCpuOwned $run $y2rec
# a child the stop had to kill gave no ownership signal either, even over a well-formed foreign file; one that exited 0 on the stop owns its file
$y2k = New-ChildDouble $false
$y2krec = Stop-UtilSampleAll ([ordered]@{ proc = $null }) ([ordered]@{ proc = $y2k }) { } 100
$y2c = New-ChildDouble $true
$y2crec = Stop-UtilSampleAll ([ordered]@{ proc = $null }) ([ordered]@{ proc = $y2c }) { $y2c.exited = $true; $y2c.code = 0 } 100
# the job's own region 3 and Publish-UtilSampleFiles, lifted from the generator's emitted text, run against those records
$genAst = [System.Management.Automation.Language.Parser]::ParseFile($env:US_GEN, [ref]$null, [ref]$null)
$genStrings = @($genAst.FindAll({ param($n) $n -is [System.Management.Automation.Language.StringConstantExpressionAst] }, $true) | ForEach-Object { [string]$_.Value })
$pubSrc = @($genStrings | Where-Object { $_.Contains('function Publish-UtilSampleFiles') })[0]
$pubAst = [System.Management.Automation.Language.Parser]::ParseInput($pubSrc, [ref]$null, [ref]$null)
. ([scriptblock]::Create($pubAst.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Publish-UtilSampleFiles' }, $true).Extent.Text))
$region3 = @($genStrings | Where-Object { $_.Contains('$utilSampleCpuParsed = ConvertFrom-UtilSampleCpuCsv') })[0]
function Write-JobTrace([string]$m) { }
function Publish-AttrCudaFileCopy([string]$Source, [string]$Destination) { $sx = [IO.File]::ReadAllText($Source); $script:y2pubs.Add([IO.Path]::GetFileName($Destination) + '=' + $sx.Substring(0, [math]::Min(12, $sx.Length))); [IO.File]::Copy($Source, $Destination, $true) }
$rawLog = "[2026-11-02T14:00:00.000Z] [info] [0x1] playback_smoke.start session=1 position=0`n[2026-11-02T14:00:20.000Z] [info] [0x1] playback_smoke.summary session=1 reason=play-stop elapsed_ms=20000`n"
$Pub = $y2pub; $utilSampleGpuCsv = Join-Path $y2dir 'gpu-util.csv'; $utilSampleCpuCsv = $y2csv
$utilSampleGpuStart = [ordered]@{ status = 'unavailable'; reason = 'nvidia-smi-missing'; owned = $false }; $utilSampleCpuStart = $run
function Invoke-Region3($Rec) {
    $script:utilSampleStopRecord = $Rec; $script:utilSamplePublished = $false; $script:utilSampleScalars = $null; $script:y2pubs = [Collections.Generic.List[string]]::new()
    . ([scriptblock]::Create($region3))
    @{ reason = [string]$utilSampleScalars['utilSampleReason']; total = $utilSampleScalars['cpuTotalMean']; published = @($script:y2pubs); pubFile = (Test-Path -LiteralPath (Join-Path $Pub 'cpu-util.csv')) }
}
$y2run = Invoke-Region3 $y2rec
$y2fin = $(Remove-Item -LiteralPath (Join-Path $Pub 'cpu-util.csv') -ErrorAction SilentlyContinue; $script:utilSampleStopRecord = $y2rec; $script:utilSamplePublished = $false; $script:y2pubs = [Collections.Generic.List[string]]::new(); Publish-UtilSampleFiles; @($script:y2pubs))
# a well-formed foreign file (a clean witness's rows) under the killed and the clean records
$y2good = $H; for ($i = 0; $i -le 21; $i++) { $iu = $p0.AddSeconds($i); $y2good += (CpuRow $iu 50 1234 1 $iu.AddSeconds(-1) '0.4') }
[IO.File]::WriteAllText($y2csv, $y2good)
$y2killRun = Invoke-Region3 $y2krec
Remove-Item -LiteralPath (Join-Path $Pub 'cpu-util.csv') -ErrorAction SilentlyContinue
$y2cleanRun = Invoke-Region3 $y2crec
$r['r6A2'] = @{ exit = $y2rec.cpu.exitCode; stopped = $y2rec.cpu.stopped; early = $y2rec.cpu.exitedBeforeStop; foreignFileOwned = [bool]$y2own; region3 = $y2run; finallyPublished = @($y2fin)
    killed = @{ killed = $y2krec.cpu.killed; owned = [bool](Test-UtilSampleCpuOwned $run $y2krec); run = $y2killRun; summary = (Get-UtilSampleCpuSummary (ConvertFrom-UtilSampleCpuCsv $y2good) $run $x1w $y2krec.cpu).status }
    clean = @{ owned = [bool](Test-UtilSampleCpuOwned $run $y2crec); run = $y2cleanRun } }
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
        # r3 B2: the 00.1 row's interval opened at 13:59:59.5, before the window, so only the 00.6 row (whole interval inside, no total) counts
        self.assertEqual(self.r["cpuWin"], {"status": "ok", "total": None, "cores": 3.5, "inWindow": 1, "rows": 4, "pids": "1234"})
        # r5 B4: duration-weighted over the stated intervals (total 10x500+50x600+90x900 over 2000 ms; cores 2.5x600+3.5x500+8x900 over 2000 ms)
        self.assertEqual(self.r["cpuNoWin"], {"status": "partial(no_window)", "total": 58.0, "cores": 5.225, "inWindow": 4, "pids": "1234;1240"})
        self.assertEqual(self.r["cpuEmpty"], "unavailable(cpu-util-no-rows)")
        self.assertEqual(self.r["cpuNoHeader"], "unavailable(cpu-util-unparseable)")
        self.assertEqual(self.r["cpuStartFailed"], "unavailable(cpu-sampler-start-failed)")

    def test_t4_summary_scalars(self) -> None:
        ok = self.r["scalarsOk"]
        self.assertEqual(list(ok), SCALARS)
        self.assertEqual([ok[k] for k in SCALARS], ["ok", None, "ok", 60.0, 80.0, 4, 5, None, None, 3.5, 1, 4])  # r3 B2: one whole-interval cpu row (no total)
        nw = self.r["scalarsNoWindow"]
        self.assertEqual((nw["utilSampleStatus"], nw["utilSampleReason"], nw["gpuUtilStatus"], nw["gpuUtilMean"]), ("partial", "no_window", "partial(no_window)", 69.75))
        sk = self.r["scalarsSkipped"]
        self.assertEqual((sk["utilSampleStatus"], sk["utilSampleReason"], sk["gpuUtilStatus"], sk["gpuUtilMean"], sk["gpuRowsInWindow"], sk["cpuTotalMean"]),
                         ("ok", "gpu:skipped_wake_risk(dgpu-D3)", "skipped_wake_risk", None, 0, None))
        none = self.r["scalarsNone"]
        self.assertEqual((none["utilSampleStatus"], none["utilSampleReason"], none["cpuTotalMean"], none["appCpuMeanCores"]),
                         ("unavailable", "gpu:unavailable(nvidia-smi-missing)+cpu:unavailable(cpu-sampler-start-failed)", None, None))

    def test_t4_window_is_the_measured_sessions_play_span_from_the_run_log(self) -> None:
        # adversary 9d648094 A1: the run log's measured session start plus its elapsed_ms, not PresentMon's capture offsets
        self.assertEqual(self.r["window"], {"startUtc": "2026-11-01T14:00:10.0010000Z", "endUtc": "2026-11-01T14:00:30.0010000Z", "session": 2})
        self.assertEqual(self.r["windowFirstSummary"], {"startUtc": "2026-11-01T14:00:01.0000000Z", "endUtc": "2026-11-01T14:00:05.0000000Z", "session": 1})
        self.assertTrue(self.r["windowNull"])
        self.assertTrue(self.r["windowNoElapsed"])
        self.assertTrue(self.r["windowEmpty"])

    def test_a2_startup_rows_before_play_are_excluded(self) -> None:
        # r3 B2: 20 rows end inside the window, but the first (14:00:11) is the interval that opened at 14:00:10, before Play started at 14:00:10.001
        self.assertEqual(self.r["cpuPlayOnly"], {"status": "ok", "total": 10.0, "cores": 1.0, "inWindow": 19})

    def test_a3_the_repeated_fall_back_hour_is_unparseable(self) -> None:
        self.assertEqual(self.r["gpuAmbiguousLocal"], "nvidia-smi-unparseable")

    def test_a7_a_sampler_that_died_early_or_failed_is_unavailable(self) -> None:
        self.assertEqual(self.r["gpuExitedEarly"], "unavailable(nvidia-smi-exited-early)")
        self.assertEqual(self.r["cpuExitedEarly"], "unavailable(cpu-sampler-exited-early)")
        self.assertEqual(self.r["cpuExitNonZero"], "unavailable(cpu-sampler-exit-3)")
        self.assertEqual(self.r["cpuStopClean"], "ok")

    def test_a8_a_non_finite_mean_is_unavailable(self) -> None:
        self.assertEqual(self.r["gpuNonFinite"], "unavailable(non-finite)")
        self.assertEqual(self.r["cpuNonFinite"], "unavailable(non-finite)")

    def test_a10_a_reused_parent_pid_is_parent_gone(self) -> None:
        self.assertEqual(self.r["cpuLoopParent"], {"reused": "parent-gone", "same": "deadline"})

    def test_a4_a5_cuda_reads_the_pnp_record_and_the_backend_is_case_blind(self) -> None:
        self.assertEqual(self.r["cudaAmbiguous"], {"status": "unavailable", "reason": "multi-dgpu-ambiguous", "proc": True})
        self.assertEqual(self.r["cudaNoDgpu"], {"status": "unavailable", "reason": "no-dgpu", "proc": True})
        self.assertEqual(self.r["cudaUpper"]["status"], "unavailable")
        self.assertEqual(self.r["cudaUpper"]["reason"], "nvidia-smi-missing", "a 'CUDA' leg is a cuda leg, not a D3 skip")
        self.assertIn("cuda leg", self.r["cudaUpper"]["disclosure"])

    def test_r3_a1_a_summary_stamp_that_disagrees_with_start_plus_elapsed_is_no_window(self) -> None:
        # adversary 693bf053 A1: the host clock stepped 60 s mid-play, so the log's own stamps span 80 s for a 20 s elapsed_ms; the old window
        # (start + elapsed_ms) silently kept 1 of 3 rows and said ok
        a1 = self.r["r3A1"]
        self.assertTrue(a1["windowNull"], "an unverified clock mapping is no window")
        self.assertEqual((a1["status"], a1["inWindow"], a1["total"]), ("partial(no_window)", 3, 3))
        self.assertEqual(self.r["r3A1Within"], {"windowNull": False, "end": "2026-11-02T14:00:20.0000000Z"}, "a 400 ms stamp skew still maps")
        self.assertTrue(self.r["r3A1BadSummaryStamp"], "an unparseable summary stamp cannot verify the mapping")

    def test_r3_a3_a_marker_quoted_in_a_location_suffix_is_not_the_measured_session(self) -> None:
        self.assertEqual(self.r["r3A3Measured"], {"session": 2, "start": "2026-11-02T14:00:10.0000000Z"})
        self.assertEqual(self.r["r3A3Summary"], {"session": 1}, "the fall-back summary line is the log sink's line too")

    def test_r3_a2_a_failed_stop_is_a_typed_failure_not_ok(self) -> None:
        self.assertEqual(self.r["r3A2"], {"gpu": "unavailable(nvidia-smi-stop-failed)", "cpu": "unavailable(cpu-sampler-stop-failed)",
                                          "scalarStatus": "unavailable",
                                          "scalarReason": "gpu:unavailable(nvidia-smi-stop-failed)+cpu:unavailable(cpu-sampler-stop-failed)"})
        self.assertEqual(self.r["r3A2NotStarted"], {"gpu": "ok"}, "a side that was never started is not a stop failure")

    def test_r3_b1_a_child_that_exits_on_the_stop_request_is_not_exited_early(self) -> None:
        self.assertEqual(self.r["r3B1"], {"stopped": True, "early": False, "exit": 0, "status": "ok", "inWindow": 1, "preExitedEarly": True})

    def test_r3_b2_the_cpu_interval_that_opened_before_play_is_not_play_only(self) -> None:
        # rows 02..20 only: row 00/01 end at or before 14:00:01 (the interval of row 01 opened at 14:00:00 < Play start 14:00:00.9) and row 21 is past the end
        self.assertEqual(self.r["r3B2"], {"status": "ok", "inWindow": 19, "total": 0.0, "cores": 0.0})

    def test_r3_b3_out_of_order_sample_stamps_are_an_unverified_clock_not_ok(self) -> None:
        self.assertEqual(self.r["r3B3"], {"cpu": "unavailable(sample-clock-unverified)", "gpu": "unavailable(sample-clock-unverified)", "scalar": "unavailable"})
        self.assertEqual(self.r["r3B3Steady"], {"cpu": "ok", "inWindow": 2}, "a steady clock still maps (rows 02 and 03 are whole intervals in the window)")

    def test_r4_a1_a_path_entry_on_an_absent_drive_does_not_hide_system32_nvidia_smi(self) -> None:
        a1 = self.r["r4A1"]
        self.assertTrue(a1["drive"], "no absent drive letter on this host to build the probe")
        self.assertEqual(a1["found"], a1["expected"], "an unreachable PATH entry ended the search before System32")
        self.assertEqual(a1["none"], "", "no nvidia-smi anywhere is the empty string")

    def test_r4_a2_a_stalled_read_is_not_followed_by_a_burst_of_catch_up_rows(self) -> None:
        a2 = self.r["r4A2"]
        self.assertGreaterEqual(a2["rows"], 3, a2)
        self.assertGreaterEqual(a2["minGapMs"], 100, f"rows landed back to back after the stall (interval 200 ms): {a2}")

    def test_r5_k1_no_frozen_commit_byte_pin_in_this_test_file(self) -> None:
        # opus / fable r4 key K1 (catalogue P-01): T1 compared this tree's jobs with a frozen commit's generator, which reds on any later edit
        self.assertEqual(frozen_pins(Path(__file__).read_text(encoding="utf-8")), [])

    def test_r5_b1_a_late_row_write_does_not_move_its_interval_boundary(self) -> None:
        loop = self.r["r5B1Loop"]
        self.assertGreaterEqual(loop["rows"], 3, loop)
        self.assertEqual(loop["bad"], [], f"a row's stated interval does not contain the span its two reads measure: {loop}")
        self.assertGreaterEqual(loop["maxStep"], 0, "every row states its clock step")
        self.assertLessEqual(loop["maxStep"], 25, f"a steady host clock read as a step (the witness would refuse every run): {loop}")
        self.assertEqual(self.r["r5B1"], {"status": "ok", "total": 0.0, "cores": 0.0, "inWindow": 19},
                         "the 01.100 row opened at the .090 read, before Play at .500, so its pre-Play 40 % / 4 cores are not Play-only")

    def test_r5_b2_an_ordered_clock_excursion_is_not_verified_play_only_statistics(self) -> None:
        loop = self.r["r5B2Loop"]
        self.assertGreaterEqual(loop["maxStep"], 50, f"a +100 ms excursion out and back was not seen by the child's clock reads: {loop}")
        self.assertEqual(loop["witness"], "sample-clock-unverified", loop)
        b2 = self.r["r5B2"]
        self.assertEqual((b2["gpuStepped"], b2["cpuStepped"]), ("unavailable(sample-clock-unverified)", "unavailable(sample-clock-unverified)"),
                         "sol r4 B2: the startup sample stamped inside the window under a witnessed step is no ok Play-only mean (it was ok, 1.235)")
        self.assertEqual(b2["gpuNoChild"], "unavailable(sample-clock-unwitnessed)", "no running CPU child, no witness, no verified window")
        self.assertEqual(b2["gpuNoKey"], "unavailable(sample-clock-unwitnessed)", "a window the job did not witness fails closed")
        self.assertEqual((b2["gap"], b2["short"], b2["clean"]), ("sample-clock-unwitnessed", "sample-clock-unwitnessed", None),
                         "a missing row or rows that stop before the window ends leave part of it unwitnessed")
        self.assertEqual((b2["gpuClean"], b2["gpuCleanRows"], b2["gpuCleanMean"]), ("ok", 81, 1.235), "a clean witness still maps the window")
        self.assertEqual(b2["noWindow"], "partial(no_window)")

    def test_r5_b3_an_occupied_gpu_csv_is_refused_and_keeps_its_bytes(self) -> None:
        self.assertEqual(self.r["r5B3"], {"status": "unavailable", "reason": "gpu-util-occupied", "noProc": True, "owned": False, "bytes": "KEEP-UNOWNED"},
                         "nvidia-smi -f would truncate a file the job did not create")
        self.assertEqual(self.r["r5B3Fresh"], {"status": "running", "owned": True, "exists": True}, "an absent name is created by the job, then handed over")
        self.assertEqual(self.r["r5B3Cpu"], {"status": "unavailable", "reason": "cpu-util-occupied", "noProc": True, "bytes": "KEEP-UNOWNED-CPU"})

    def test_r5_b4_unequal_cpu_intervals_are_duration_weighted(self) -> None:
        # sol r4 B4 exact case: unweighted it was ok 6.667 % / 0.667 cores; the retained intervals span 19 s with 5 loaded seconds
        self.assertEqual(self.r["r5B4"], {"status": "ok", "total": 26.316, "cores": 2.632, "inWindow": 15})

    def test_adv_01d6ad51_a1_a_step_inside_every_clock_bracket_is_still_counted(self) -> None:
        a1 = self.r["advA1"]
        self.assertGreaterEqual(a1["rows"], 3, a1)
        self.assertGreater(a1["maxStep"], 25, f"a +100 ms step hidden inside the brackets was absorbed by them: {a1}")

    def test_adv_01d6ad51_a2_the_row_after_a_stalled_read_weighs_its_own_interval(self) -> None:
        self.assertEqual(self.r["advA2"], {"clock": None, "status": "ok", "total": 25.0, "cores": 0.25, "inWindow": 16},
                         "the 7 s row's bounds span 2..7 s because the read before it stalled, but it measured 1 s")

    def test_adv_01d6ad51_a3_the_job_holds_its_csv_and_a_writer_refuses_foreign_bytes(self) -> None:
        a3 = self.r["advA3"]
        self.assertEqual((a3["held"], a3["delete"], a3["move"]), (True, "refused", "refused"), "the name could be swapped while nvidia-smi runs")
        self.assertEqual(a3["writer"], "written", "an nvidia-smi -f style truncating writer must still open the held file")
        self.assertIn("cpu-util-occupied", a3["foreign"], "a child whose CreateNew failed must end, not loop on a null handle")
        self.assertEqual(a3["foreignBytes"], "KEEP-FOREIGN")
        self.assertEqual(a3["owned"], [True, False, False, False], "only a child still running at the stop, which exited 0, owns (and publishes) cpu-util.csv")

    def test_adv_01d6ad51_a4_a5_unreadable_cpu_time_and_other_jobs_instances_are_left_out(self) -> None:
        a = self.r["advA4A5"]
        self.assertEqual(a["unreadable"], 0, "a null CPU time became a zero baseline, or another job's instance was counted")
        self.assertEqual(a["ownOnly"], a["expected"], "only the job's own executable path counts (case-blind)")
        self.assertEqual(a["noPath"], 2, "without a path the name alone binds, as before")

    def test_adv_f685c478_a1_a_cpu_percentage_weighs_the_interval_its_counter_measured(self) -> None:
        # exact case: the 100 % sample was read at 2 s and the snapshot stalled to 6 s; at f685c478 the row took the snapshots' 5 s weight (25.0)
        self.assertEqual(self.r["r6A1"], {"parseWhy": None, "clock": None, "status": "ok", "total": 5.0, "cores": 0.05, "inWindow": 16,
                                          "unbound": "unavailable(cpu-counter-interval-unbound)"},
                         "cpuTotalPct is weighted by its counter-read span; one without a bound counter interval fails closed")
        loop = self.r["r6A1Loop"]
        self.assertGreaterEqual(loop["rows"], 3, loop)
        self.assertEqual(loop["bad"], [], f"a row's counterMs is not the span between the two counter reads it measured: {loop}")
        self.assertIsNotNone(loop["stall"], f"the stalled snapshot left no long row: {loop}")
        self.assertLess(loop["stall"]["counterMs"], 400, f"the stalled row's counter measured about 200 ms, not the snapshots' span: {loop}")
        self.assertEqual((loop["outputs"], loop["created"]), (1, True), "the loop emits one result, which says its CreateNew succeeded")

    def test_adv_f685c478_a2_a_child_that_failed_owns_nothing_and_nothing_is_published(self) -> None:
        # exact case (in-memory double): stop read HasExited=false, then the child refused CreateNew over KEEP-FOREIGN and exited 1
        a2 = self.r["r6A2"]
        self.assertEqual((a2["exit"], a2["stopped"], a2["early"]), (1, True, False), a2)
        self.assertFalse(a2["foreignFileOwned"], "a child that exited 1 gave no ownership signal (it was True at f685c478)")
        self.assertEqual(a2["region3"], {"published": [], "total": None, "pubFile": False,
                                         "reason": "gpu:unavailable(nvidia-smi-missing)+cpu:unavailable(cpu-sampler-exit-1)"},
                         "the job's region 3 published KEEP-FOREIGN or summarised it")
        self.assertEqual(a2["finallyPublished"], [], "the outer finally's publish must not publish it either")
        self.assertEqual(a2["killed"], {"killed": True, "owned": False, "summary": "unavailable(cpu-util-unowned)",
                                        "run": {"published": [], "total": None, "pubFile": False,
                                                "reason": "gpu:unavailable(nvidia-smi-missing)+cpu:unavailable(cpu-util-unowned)"}},
                         "a killed child gave no ownership signal: a well-formed foreign file is neither summarised nor published")
        self.assertEqual(a2["clean"], {"owned": True, "run": {"published": ["cpu-util.csv=utc,cpuTotal"], "total": 50.0, "pubFile": True,
                                                              "reason": "gpu:unavailable(nvidia-smi-missing)"}},
                         "a child that exited 0 on the stop owns, summarises and publishes its file")

    def test_r5_an_empty_system_root_does_not_end_the_nvidia_smi_search(self) -> None:
        self.assertEqual(self.r["r5EmptyRoot"]["found"], self.r["r5EmptyRoot"]["expected"])

    def test_a6_a_throwing_kill_is_retried_and_a_running_side_is_not_stopped(self) -> None:
        self.assertEqual(self.r["stopKillRetry"], {"stopped": True, "killed": True, "kills": 2, "early": False})
        self.assertEqual(self.r["stoppedTest"], {"both": True, "running": False, "none": False})
        self.assertEqual(self.r["stopEarly"], {"stopped": True, "early": True, "killed": False})

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
        self.assertEqual(got["header"], "utc,cpuTotalPct,appPid,appCpuCores,fromUtc,clockStepMs,intervalMs,counterMs")
        self.assertGreaterEqual(got["rows"], 1)
        self.assertRegex(got["firstRow"], r"^\d{4}-\d\d-\d\dT[^,]+Z,,,,\d{4}-\d\d-\d\dT[^,]+Z,\d+(\.\d+)?,\d+(\.\d+)?,$",
                         "a throwing counter is an empty field, never a throw or a 0; the interval start and clock step are still stated")
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
