"""Tests for BACHELOR-CORE-PARTITION-AB-1: the opt-in core-partition leg (bench affinity, an MLV-owned E-core load, a telemetry helper).

WHAT IS PINNED
  * with none of the five new generator arguments the emitted job is byte-identical to the base commit's generator, for the default
    job and for the partition legs' own shape (cpu, forced cinematic Look Assist, unpaced, windowed, scale 1);
  * every generation-time refusal is a typed CORE_PARTITION_* throw;
  * the bracketed regions exist ONLY with -CorePartitionTelemetry, wrap ONLY the measured smoke launch, and once they (and the three wrapper
    lines they need) are taken out the job is exactly the job without telemetry;
  * the job and the encoded helper both parse, and the helper carries its spliced functions;
  * Invoke-VenueLeg maps the five generatorArgs (offline test mode, stub generator), and maps nothing when they are absent;
  * the leg-spec schema accepts the three partition legs and refuses a bad mask or thread count;
  * the pure helpers (mask parse, topology class check, MHz math, feed window, counter summary, worker count, summary scalars) are lifted
    from the generator by AST and fed fixtures.
No test launches MLVApp (owner rule 2026-10-11): the generator only writes job text, and the runner runs against stubs.
"""

from __future__ import annotations

import base64
import json
import os
import re
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
    OWNER_CLIP,
    ROOT,
    STUB_GENERATOR,
    RunnerHarness,
    git,
    lf,
    requires_windows_pwsh,
    run_pwsh,
    run_pwsh_many,
)

# The commit this card branched from: the generator whose job text every unset-argument job must still be.
BASE_COMMIT = "a235df4d4c47ab77056b0016f8c4a8443811a6a9"
OPEN = "# BACHELOR-CORE-PARTITION-AB-1 >>>"
CLOSE = "# BACHELOR-CORE-PARTITION-AB-1 <<<"
LEGS = {
    "a": "legs/m16-1243-partition-a-unpaced-windowed-s1.json",
    "b1": "legs/m16-1243-partition-b1-pcore-unpaced-windowed-s1.json",
    "b2": "legs/m16-1243-partition-b2-pcore-eload-unpaced-windowed-s1.json",
}
# The partition legs' generator shape (what Invoke-VenueLeg passes for the cpu backend of the three specs, minus the new arguments).
SHAPE = ["-Backend", "cpu", "-ScaleFactor", "1", "-ExpectedScaleRequest", "1", "-DisplayMode", "windowed", "-TelemetryArm", "LIGHT",
         "-CpuQuiescenceThresholdPercent", "95", "-ForceLookAssist", "-LookPaceLeg", "-LookFlavor", "cinematic", "-SwapInterval", "0",
         "-Unpaced", "-PresentTrace"]
HELPER = ["-CorePartitionTelemetry", "-CorePartitionHelperMask", "0xFF000"]
ARM_A = [*SHAPE, *HELPER]
ARM_B1 = [*SHAPE, *HELPER, "-BenchAffinityMask", "0x00FFF"]
ARM_B2 = [*ARM_B1, "-PartitionLoadMask", "0xFF000", "-PartitionLoadThreads", "8"]
CUDA_B2 = ["-Backend", "cuda", "-ScaleFactor", "1", "-ExpectedScaleRequest", "1", "-DisplayMode", "windowed", "-ForceLookAssist", "-LookPaceLeg",
           "-LookFlavor", "cinematic", "-Unpaced", *HELPER, "-BenchAffinityMask", "0x00FFF", "-PartitionLoadMask", "0xFF000", "-PartitionLoadThreads", "8"]
REFUSALS = {
    "CORE_PARTITION_MASK_FORMAT": [*HELPER, "-BenchAffinityMask", "FFF"],
    "CORE_PARTITION_MASK_FORMAT ": [*HELPER, "-BenchAffinityMask", "0x1FFFFF"],
    "CORE_PARTITION_MASK_ZERO": [*HELPER, "-BenchAffinityMask", "0x0"],
    "CORE_PARTITION_LOAD_NEEDS_BENCH": [*HELPER, "-PartitionLoadMask", "0xFF000", "-PartitionLoadThreads", "8"],
    "CORE_PARTITION_MASK_OVERLAP": ["-CorePartitionTelemetry", "-CorePartitionHelperMask", "0xF0000", "-BenchAffinityMask", "0x00FFF", "-PartitionLoadMask", "0x01F00", "-PartitionLoadThreads", "2"],
    "CORE_PARTITION_LOAD_THREADS": [*HELPER, "-BenchAffinityMask", "0x00FFF", "-PartitionLoadMask", "0xFF000"],
    "CORE_PARTITION_LOAD_THREADS ": [*HELPER, "-BenchAffinityMask", "0x00FFF", "-PartitionLoadMask", "0xFF000", "-PartitionLoadThreads", "17"],
    "CORE_PARTITION_LOAD_THREADS  ": [*HELPER, "-BenchAffinityMask", "0x00FFF", "-PartitionLoadThreads", "4"],
    "CORE_PARTITION_NEEDS_TELEMETRY": ["-BenchAffinityMask", "0x00FFF"],
    "CORE_PARTITION_NEEDS_TELEMETRY ": ["-CorePartitionHelperMask", "0xFF000"],
    "CORE_PARTITION_NEEDS_HELPER_MASK": ["-CorePartitionTelemetry"],
    "CORE_PARTITION_HELPER_OVERLAP": ["-CorePartitionTelemetry", "-CorePartitionHelperMask", "0x01F00", "-BenchAffinityMask", "0x00FFF"],
}
# sol r2 (A6): a mask ending in LF slipped past the `$`-anchored format check (.NET `$` matches before a final LF) and reached the untyped converter
# throw; each of the three mask fields must get the typed CORE_PARTITION_MASK_FORMAT refusal for a trailing LF, a trailing CRLF and a doubled LF.
EOL_SUFFIXES = {"lf": "\n", "crlf": "\r\n", "lflf": "\n\n"}
EOL_FIELDS = {
    "BenchAffinityMask": lambda v: [*HELPER, "-BenchAffinityMask", "0x00FFF" + v],
    "PartitionLoadMask": lambda v: [*HELPER, "-BenchAffinityMask", "0x00FFF", "-PartitionLoadMask", "0xFF000" + v, "-PartitionLoadThreads", "8"],
    "CorePartitionHelperMask": lambda v: ["-CorePartitionTelemetry", "-CorePartitionHelperMask", "0xFF000" + v],
}
SCALAR_NAMES = ["corePartitionTelemetry", "corePartitionTopologyOk", "appAffinityObserved", "benchAffinityRequested", "benchAffinityApplied", "partitionLoadMask",
                "partitionLoadThreads", "partitionLoadApplied", "partitionLoadGBps", "pkgWattsMean", "pkgWattsMax", "pkgTempCMean", "pkgTempCMax", "pkgThrottledRows",
                "pkgTempCAtStart", "pcoreMHzMean", "ecoreMHzMean", "pcoreParkedSampleFraction", "playbackWorkerThreads", "corePartitionNullReasons"]
REGION = re.compile(r"(?ms)^[ \t]*" + re.escape(OPEN) + r"\n.*?^[ \t]*" + re.escape(CLOSE) + r"\n")


def strip_regions(text: str) -> str:
    """The job with every bracketed region and the three wrapper lines (the outer try, its finally and its closing brace) taken out."""
    out = REGION.sub("", lf(text))
    out = out.replace("Write-JobTrace 'step smoke-launch start (app launch + load + playback)'\ntry {\ntry {\n",
                      "Write-JobTrace 'step smoke-launch start (app launch + load + playback)'\ntry {\n", 1)
    return out.replace("    $smokeLaunchException = $_\n}\n} finally {\n}\nWrite-JobTrace \"step smoke-launch done rc=$smokeRc\"",
                       "    $smokeLaunchException = $_\n}\nWrite-JobTrace \"step smoke-launch done rc=$smokeRc\"", 1)


# r2 (Dot B1): the job's pre-launch region run for real, with a fake HWiNFO feed and a stand-in helper that is only ready after a delay. The helper
# first writes low-power "startup" rows to the feed, then signals ready (recording the feed length at that instant); the driver then appends the
# "smoke" rows and reads the window the way the job's finally block does. Arm A: no bench mask, so no affinity is set on the test process.
FAKE_HELPER = r"""
$f = $env:HEAVY_SLOT_FEED
Start-Sleep -Milliseconds 400
[IO.File]::AppendAllText($f, "26.10.2026,01:00:00,x,30.0,10.0,No,No`n26.10.2026,01:00:01,x,30.0,10.0,No,No`n")
Start-Sleep -Milliseconds 1200
[IO.File]::WriteAllText($env:MLV_CP_READY, [string](New-Object IO.FileInfo $f).Length)
Start-Sleep -Seconds 60
"""
READINESS_DRIVER = r"""
$ErrorActionPreference = 'Stop'
$job = [IO.File]::ReadAllText($env:CP_JOB)
$s = $job.IndexOf($env:CP_OPEN)
$e = $job.IndexOf($env:CP_CLOSE, $s)
$region = $job.Substring($s, $e - $s)
$fake = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($env:CP_FAKE_HELPER))
$region = [regex]::Replace($region, "(?m)^\`$CorePartitionHelperEncoded = '[^']*'", { "`$CorePartitionHelperEncoded = '$fake'" })
$region = [regex]::Replace($region, '(?m)^\$CorePartitionHelperAffinity = \[int64\]\d+', '$CorePartitionHelperAffinity = [int64]1')
function Write-JobTrace([string]$Message) { }
function Publish-AttrCudaText { param([string]$Path, [string]$Value) [IO.File]::WriteAllText($Path, $Value) }
$Work = $env:CP_WORK
$exePath = 'C:\no\such\MLVApp.exe'
$SmokeProcessTimeoutMs = 60000
. ([scriptblock]::Create($region))
try {
    $ready = $null
    for ($i = 0; $i -lt 40 -and -not $ready; $i++) { $ready = (Get-Content -LiteralPath $corePartitionReadyFile -Raw -ErrorAction SilentlyContinue); if (-not $ready) { Start-Sleep -Milliseconds 100 } }
    [IO.File]::AppendAllText($corePartitionFeedPath, "26.10.2026,01:00:02,x,70.0,45.0,No,No`n26.10.2026,01:00:03,x,80.0,55.0,No,No`n26.10.2026,01:00:04,x,75.0,50.0,No,No`n")
    $end = Read-CorePartitionFeedSince $corePartitionFeedPath ([int64]$corePartitionFeedStart.length)
    $sum = Get-CorePartitionFeedSummary $corePartitionFeedStart $end 120
    [ordered]@{
        helperReady = $corePartitionHelperLaunch.ready; readyLength = [int64]([string]$ready).Trim(); startLength = $corePartitionFeedStart.length
        rows = $sum.rows; wattsMean = $sum.pkgWattsMean; tempAtStart = $sum.pkgTempCAtStart
        launchFileCarriesTheMeasuredLaunch = ([IO.File]::ReadAllText($corePartitionLaunchFile) -eq [string]$corePartitionMeasuredLaunchUtc.Ticks)
        launchUtcAfterReady = ($null -ne $corePartitionMeasuredLaunchUtc -and $corePartitionMeasuredLaunchUtc -gt $corePartitionPreHelperUtc.AddMilliseconds(1000))
    } | ConvertTo-Json -Compress
} finally {
    if ($null -ne $corePartitionHelper -and -not $corePartitionHelper.HasExited) { $corePartitionHelper.Kill() }
}
"""


# r2 (Dot improvement): the decoded helper run for real against a stand-in "app" (a copy of PING.EXE named mlvfake.exe; never MLVApp). One instance
# of that exe already runs when the helper starts, a second starts after it is ready: only the second may be bound as the measured app.
IDENTITY_DRIVER = r"""
$ErrorActionPreference = 'Stop'
$dir = $env:CP_DIR
$exe = Join-Path $dir 'mlvfake.exe'
Copy-Item -LiteralPath 'C:\Windows\System32\PING.EXE' -Destination $exe
$pre = Start-Process -FilePath $exe -ArgumentList @('-n', '40', '127.0.0.1') -PassThru -WindowStyle Hidden
try {
    Start-Sleep -Milliseconds 1500
    $psi = [Diagnostics.ProcessStartInfo]::new("$env:ProgramFiles\PowerShell\7\pwsh.exe", "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand $($env:CP_ENCODED)")
    $psi.UseShellExecute = $false; $psi.CreateNoWindow = $true
    $psi.Environment['MLV_CP_EXE'] = $exe
    $psi.Environment['MLV_CP_STOP'] = Join-Path $dir 'stop'
    $psi.Environment['MLV_CP_READY'] = Join-Path $dir 'ready'
    $psi.Environment['MLV_CP_OUT'] = Join-Path $dir 'helper.json'
    $psi.Environment['MLV_CP_LAUNCH'] = Join-Path $dir 'launch'
    $psi.Environment['MLV_CP_BENCH'] = '0'; $psi.Environment['MLV_CP_LOAD'] = '0'; $psi.Environment['MLV_CP_HELPER'] = '1'; $psi.Environment['MLV_CP_THREADS'] = '0'
    $psi.Environment['MLV_CP_DEADLINE_TICKS'] = [string]([DateTime]::UtcNow.AddSeconds(90).Ticks)
    $h = [Diagnostics.Process]::Start($psi)
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while (-not (Test-Path (Join-Path $dir 'ready')) -and -not $h.HasExited -and $sw.ElapsedMilliseconds -lt 30000) { Start-Sleep -Milliseconds 100 }
    $mid = Start-Process -FilePath $exe -ArgumentList @('-n', '40', '127.0.0.1') -PassThru -WindowStyle Hidden
    Start-Sleep -Milliseconds 1500
    [IO.File]::WriteAllText((Join-Path $dir 'launch'), [string][DateTime]::UtcNow.Ticks)
    Start-Sleep -Milliseconds 700
    $post = Start-Process -FilePath $exe -ArgumentList @('-n', '6', '127.0.0.1') -PassThru -WindowStyle Hidden
    [void]$post.WaitForExit(20000)
    Start-Sleep -Seconds 1
    Set-Content -LiteralPath (Join-Path $dir 'stop') -Value 'stop'
    if (-not $h.WaitForExit(20000)) { $h.Kill() }
    $r = Get-Content -LiteralPath (Join-Path $dir 'helper.json') -Raw | ConvertFrom-Json
    [ordered]@{ prePid = $pre.Id; midPid = $mid.Id; postPid = $post.Id; seen = $r.app.pid; instances = @($r.instances | ForEach-Object { $_.pid }) } | ConvertTo-Json -Compress
} finally {
    if (-not $pre.HasExited) { $pre.Kill() }
    if ($null -ne $mid -and -not $mid.HasExited) { $mid.Kill() }
}
"""


NEWFILE_DRIVER = r"""
$ErrorActionPreference = 'Stop'
$src = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($env:CP_ENCODED))
$m = [regex]::Match($src, '(?ms)^function Write-CorePartitionNewFile.*?^\}$')
if (-not $m.Success) { throw 'Write-CorePartitionNewFile is not in the helper' }
. ([scriptblock]::Create($m.Value))
$owner = Join-Path $env:CP_DIR 'owner.bin'
[IO.File]::WriteAllText($owner, 'original-bytes')
$ready = Join-Path $env:CP_DIR 'core-partition.ready'
New-Item -ItemType HardLink -Path $ready -Target $owner | Out-Null
$threw = $false
try { Write-CorePartitionNewFile $ready 'ready' } catch { $threw = $true }
$fresh = Join-Path $env:CP_DIR 'fresh.txt'
Write-CorePartitionNewFile $fresh 'ok'
[ordered]@{ threw = $threw; original = [IO.File]::ReadAllText($owner); fresh = [IO.File]::ReadAllText($fresh) } | ConvertTo-Json -Compress
"""


@requires_windows_pwsh
class CorePartitionGeneratorTests(unittest.TestCase):
    """Runs the generator for real against a sparse clone with header-only ~30 s fixtures (the device test_dual_venue_evidence uses)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="core-partition-")
        cls.tmp = Path(cls._tmp.name)
        cls.head = git("rev-parse", "HEAD")
        cls.repo = cls.tmp / "repo"
        subprocess.run(["git", "clone", "-q", "--shared", "--no-checkout", str(ROOT), str(cls.repo)], check=True)
        subprocess.run(["git", "-C", str(cls.repo), "sparse-checkout", "set", "--cone", "tools", "tests/fixtures/clips"], check=True)
        subprocess.run(["git", "-C", str(cls.repo), "checkout", "-q", cls.head], check=True)
        for stem in FIXTURE_IDS:
            write_synthetic_mlv(cls.repo / "tests" / "fixtures" / "clips" / (stem + MLV_EXT), FRAMES_30S_AT_23976)
        cls.base_available = subprocess.run(["git", "-C", str(ROOT), "cat-file", "-e", f"{BASE_COMMIT}^{{commit}}"], capture_output=True).returncode == 0
        base_root = cls.tmp / "base"
        cls.base_generator = base_root / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-job.ps1"
        if cls.base_available:
            tar = cls.tmp / "base.tar"
            subprocess.run(["git", "-C", str(ROOT), "archive", BASE_COMMIT, "--format=tar", "-o", str(tar), "tools/profiling", "tools/gates"], check=True)
            base_root.mkdir()
            subprocess.run(["tar", "-xf", str(tar), "-C", str(base_root)], check=True)
        requests: list[tuple[str, Path, list[str]]] = [("default", GENERATOR, []), ("shape", GENERATOR, SHAPE), ("a", GENERATOR, ARM_A), ("b1", GENERATOR, ARM_B1),
                                                       ("b2", GENERATOR, ARM_B2), ("cuda-b2", GENERATOR, CUDA_B2)]
        if cls.base_available:
            requests += [("base-default", cls.base_generator, []), ("base-shape", cls.base_generator, SHAPE)]
        requests += [(f"refuse-{n}", GENERATOR, extra) for n, extra in enumerate(REFUSALS.values())]
        requests += [(f"eol-{field}-{eol}", GENERATOR, build(suffix)) for field, build in EOL_FIELDS.items() for eol, suffix in EOL_SUFFIXES.items()]
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

    def test_unset_arguments_emit_the_base_commits_job_byte_for_byte(self) -> None:
        if not self.base_available:
            self.skipTest(f"base commit {BASE_COMMIT} is not in this clone")
        for key in ("default", "shape"):
            with self.subTest(job=key):
                self.assertEqual(self.out[key].read_bytes(), self.out["base-" + key].read_bytes() if self.proc["base-" + key].returncode == 0 else b"<base failed>",
                                 f"{key}: a job without the core-partition arguments changed")

    def test_every_refusal_is_typed(self) -> None:
        for n, code in enumerate(REFUSALS):
            with self.subTest(code=code.strip(), args=REFUSALS[code]):
                proc = self.proc[f"refuse-{n}"]
                self.assertNotEqual(proc.returncode, 0, "the generator accepted a bad core-partition combination")
                self.assertIn(code.strip() + " ", proc.stdout + proc.stderr)
                self.assertFalse(self.out[f"refuse-{n}"].exists(), "a refused generation wrote a job")

    def test_a_trailing_line_ending_on_any_mask_is_the_typed_format_refusal_not_a_converter_throw(self) -> None:
        for field in EOL_FIELDS:
            for eol in EOL_SUFFIXES:
                with self.subTest(field=field, eol=eol):
                    proc = self.proc[f"eol-{field}-{eol}"]
                    text = proc.stdout + proc.stderr
                    self.assertNotEqual(proc.returncode, 0, "the generator accepted a mask with a trailing line ending")
                    self.assertIn("CORE_PARTITION_MASK_FORMAT ", text)
                    self.assertIn(f"-{field} ", text)
                    self.assertFalse(self.out[f"eol-{field}-{eol}"].exists(), "a refused generation wrote a job")

    def test_the_regions_exist_only_with_telemetry_and_strip_back_to_the_plain_job(self) -> None:
        for key in ("default", "shape"):
            self.assertNotIn("BACHELOR-CORE-PARTITION-AB-1", self.job(key), key)
        plain = lf(self.job("shape"))
        for key in ("a", "b1", "b2"):
            with self.subTest(job=key):
                text = lf(self.job(key))
                self.assertEqual((text.count(OPEN), text.count(CLOSE)), (4, 4), "four bracketed regions: before, finally, scalars, summary lines")
                self.assertEqual(strip_regions(text), plain, "outside its regions (and the try/finally wrapper) the job must be the job without telemetry")

    def test_only_the_measured_launch_is_wrapped_and_the_masks_are_literal(self) -> None:
        text = lf(self.job("b2"))
        launch = text.index("Write-JobTrace 'step smoke-launch start (app launch + load + playback)'\ntry {\ntry {\n")
        before = text.rfind(CLOSE + "\n", 0, launch)
        self.assertEqual(before + len(CLOSE) + 1, launch, "the start region ends right before the measured launch")
        self.assertIn("} finally {\n" + OPEN + "\n", text[launch:launch + 1200])
        self.assertEqual(text.count("-EncodedCommand $CorePartitionHelperEncoded"), 1)
        for line in ("$CorePartitionBenchMask = [int64]4095\n", "$CorePartitionLoadMask = [int64]1044480\n", "$CorePartitionLoadThreads = 8\n",
                     "$CorePartitionHelperMask = [int64]1044480\n", "$CorePartitionHelperAffinity = [int64]1044480\n"):
            self.assertIn(line, text)
        a = lf(self.job("a"))
        for line in ("$CorePartitionBenchMask = [int64]0\n", "$CorePartitionLoadMask = [int64]0\n", "$CorePartitionLoadThreads = 0\n", "$CorePartitionHelperAffinity = [int64]1044480\n"):
            self.assertIn(line, a)
        # The success summary carries every scalar, right after its cpuFrames line.
        block = "    cpuFrames = $gpuSummary.cpuFrames\n    " + OPEN + "\n" + "".join(f"    {n} = $corePartitionScalars['{n}']\n" for n in SCALAR_NAMES) + "    " + CLOSE + "\n"
        self.assertIn(block, text)
        self.assertIn("Get-CorePartitionScalars $corePartitionHelperRecord $corePartitionFeedSummary", text)

    def test_the_feed_window_opens_after_the_helper_is_ready_not_before_it_starts(self) -> None:
        """Dot B1: a helper that takes seconds to become ready must not leak its startup rows (here 10 W) into the measured window (50 W)."""
        job = self.out["a"]
        self.job("a")
        feed = self.tmp / "ready-delay-feed.csv"
        feed.write_text("26.10.2026,00:59:59,x,20.0,5.0,No,No\n", encoding="utf-8")
        work = self.tmp / "ready-delay-work"
        work.mkdir()
        driver = self.tmp / "ready-delay-driver.ps1"
        driver.write_text(READINESS_DRIVER, encoding="ascii")
        proc = run_pwsh(["-File", str(driver)], env_extra={"CP_JOB": str(job), "CP_OPEN": OPEN, "CP_CLOSE": CLOSE, "CP_FAKE_HELPER": FAKE_HELPER,
                                                           "CP_WORK": str(work), "HEAVY_SLOT_FEED": str(feed)})
        self.assertEqual(proc.returncode, 0, proc.stdout[-2000:] + proc.stderr[-2000:])
        got = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertTrue(got["helperReady"], got)
        self.assertGreaterEqual(got["startLength"], got["readyLength"], "the feed window opened before the helper was ready")
        self.assertEqual(got["rows"], 3, got)
        self.assertEqual(got["wattsMean"], 50.0, "the window mean includes the helper's startup rows")
        self.assertEqual(got["tempAtStart"], 30.0, "the start temperature is the newest row before the measured launch")
        self.assertTrue(got["launchUtcAfterReady"], "the measured-launch timestamp must be taken after the ready wait")
        self.assertTrue(got["launchFileCarriesTheMeasuredLaunch"], "the helper is handed the measured-launch instant it binds the app by")

    def test_the_helper_binds_only_an_app_started_after_it_not_a_same_path_process_already_running(self) -> None:
        """Dot improvement: affinity and load evidence must come from the measured app, not from any process at the same exe path."""
        m = re.search(r"(?m)^\$CorePartitionHelperEncoded = '([A-Za-z0-9+/=]+)'$", self.job("a"))
        self.assertIsNotNone(m)
        work = self.tmp / "identity-work"
        work.mkdir()
        driver = self.tmp / "identity-driver.ps1"
        driver.write_text(IDENTITY_DRIVER, encoding="ascii")
        proc = run_pwsh(["-File", str(driver)], env_extra={"CP_DIR": str(work), "CP_ENCODED": m.group(1)})
        self.assertEqual(proc.returncode, 0, proc.stdout[-2000:] + proc.stderr[-2000:])
        got = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(len({got["prePid"], got["midPid"], got["postPid"]}), 3)
        self.assertEqual(got["seen"], got["postPid"], f"the helper bound a same-path process that started before the measured launch: {got}")
        self.assertEqual(got["instances"], [got["postPid"]], got)

    def test_the_helpers_output_files_are_created_never_truncated(self) -> None:
        """Adversary A5: a planted hard link at the helper's ready / record name must be refused, not truncated through."""
        m = re.search(r"(?m)^\$CorePartitionHelperEncoded = '([A-Za-z0-9+/=]+)'$", self.job("a"))
        self.assertIsNotNone(m)
        src = base64.b64decode(m.group(1)).decode("utf-16-le")
        self.assertNotIn("WriteAllText(", src, "the helper still truncating-writes a file by name")
        work = self.tmp / "newfile-work"
        work.mkdir()
        driver = self.tmp / "newfile-driver.ps1"
        driver.write_text(NEWFILE_DRIVER, encoding="ascii")
        proc = run_pwsh(["-File", str(driver)], env_extra={"CP_DIR": str(work), "CP_ENCODED": m.group(1)})
        self.assertEqual(proc.returncode, 0, proc.stdout[-2000:] + proc.stderr[-2000:])
        got = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertTrue(got["threw"], "CreateNew must refuse a name that already exists (a hard link to something else)")
        self.assertEqual(got["original"], "original-bytes", "the file behind the planted link was changed")
        self.assertEqual(got["fresh"], "ok")

    def test_the_jobs_and_the_encoded_helper_parse(self) -> None:
        files = [self.out[k] for k in ("a", "b1", "b2", "cuda-b2") if self.job(k)]
        helpers = []
        for f in files:
            m = re.search(r"(?m)^\$CorePartitionHelperEncoded = '([A-Za-z0-9+/=]+)'$", f.read_text(encoding="utf-8"))
            self.assertIsNotNone(m, f.name)
            src = base64.b64decode(m.group(1)).decode("utf-16-le")
            for name in ("function Get-CorePartitionTopology", "function Test-CorePartitionMaskClass", "function Format-CorePartitionMask", "MlvCorePartitionLoad",
                         "ReadCategory()", "'Parking Status'", "MLV_CP_DEADLINE_TICKS"):
                self.assertIn(name, src, f.name)
            self.assertLess(len(m.group(1)), 30000)
            h = self.tmp / (f.stem + ".helper.ps1")
            h.write_text(src, encoding="utf-8")
            helpers.append(h)
        script = ("foreach ($f in ($env:CP_PARSE -split '\\|')) { $t=$null;$e=$null;"
                  "[void][System.Management.Automation.Language.Parser]::ParseFile($f,[ref]$t,[ref]$e);Write-Output ((Split-Path $f -Leaf) + '|' + $e.Count) }")
        proc = run_pwsh(["-Command", script], env_extra={"CP_PARSE": "|".join(str(p) for p in [*files, *helpers])})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        counts = dict(line.rsplit("|", 1) for line in proc.stdout.splitlines() if "|" in line)
        self.assertEqual(len(counts), len(files) + len(helpers))
        for name, count in counts.items():
            self.assertEqual(count.strip(), "0", f"{name} has parse errors")


# --- the pure helpers, lifted from the generator by AST -------------------------------------------------------------------------------------
LIFT = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:CP_GEN, [ref]$tokens, [ref]$errors)
foreach ($f in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -like '*CorePartition*' }, $false)) {
    . ([scriptblock]::Create($f.Extent.Text))
}
"""

FIXTURES = r"""
$r = [ordered]@{}
$r.mask = @((ConvertFrom-CorePartitionMask '0x00FFF' 'X'), (ConvertFrom-CorePartitionMask '0xfffff' 'X'), (Format-CorePartitionMask 4095), (Format-CorePartitionMask 1048575))
$r.eol = @(foreach ($m in @("0x00FFF`n", "0x00FFF`r`n", "0x00FFF`n`n", "0x0`n")) { try { [void](ConvertFrom-CorePartitionMask $m 'X'); 'accepted' } catch { ([string]$_.Exception.Message -split '\s+')[0] } })
$map = @{}; for ($i = 0; $i -lt 20; $i++) { $map["0,$i"] = $(if ($i -lt 12) { 1 } else { 0 }) }
$ok = Test-CorePartitionMaskClass $map 4095 @(1044480, 1044480)
$badBench = Test-CorePartitionMaskClass $map 8191 @(1044480)
$badLow = Test-CorePartitionMaskClass $map 4095 @(522240)
$noBench = Test-CorePartitionMaskClass $map 0 @(0, 1044480)
$uni = @{}; for ($i = 0; $i -lt 8; $i++) { $uni["0,$i"] = 0 }
$uniform = Test-CorePartitionMaskClass $uni 15 @(240)
$r.topo = @($ok.ok, $ok.pMask, $ok.eMask, $badBench.ok, $badBench.reason, $badLow.ok, $badLow.reason, $noBench.ok, $uniform.ok, $uniform.reason)
$r.mhz = @((Get-CorePartitionEffectiveMHz 2500 160), (Get-CorePartitionEffectiveMHz $null 100), (Get-CorePartitionEffectiveMHz 0 100), (Get-CorePartitionEffectiveMHz 2500 -1))
$start = @{ exists = $true; length = 500; ageSec = 1.0; before = "26.10.2026,01:00:00,x,61.0,20.5,No,No`n26.10.2026,01:00:01,x,62.5,21.0,No,No`n"; window = ''; windowStartsMidRow = $false }
$end = @{ exists = $true; length = 900; ageSec = 0.5; before = ''; window = "5,00,x,99.0,99.0,Yes,Yes`n26.10.2026,01:00:02,x,70,45,No,No`r`n26.10.2026,01:00:03,x,80,55,Yes,No`nDate,Time,a,b,c,d,e`n26.10.2026,01:00:04,x,75,50,No,No`n"; windowStartsMidRow = $true }
$feed = Get-CorePartitionFeedSummary $start $end 120
$r.feed = @($feed.rows, $feed.pkgWattsMean, $feed.pkgWattsMax, $feed.pkgTempCMean, $feed.pkgTempCMax, $feed.pkgThrottledRows, $feed.pkgTempCAtStart, $feed.reasons.Count)
$whole = $end.Clone(); $whole.windowStartsMidRow = $false
$r.feedWhole = (Get-CorePartitionFeedSummary $start $whole 120).rows
$rot = $end.Clone(); $rot.length = 100
$r.rotated = @((Get-CorePartitionFeedSummary $start $rot 120).reasons['pkgWattsMean'], (Get-CorePartitionFeedSummary $start $rot 120).pkgTempCAtStart)
$stale = $end.Clone(); $stale.ageSec = 500
$r.stale = (Get-CorePartitionFeedSummary $start $stale 120).reasons['pkgTempCMax']
$bad = $end.Clone(); $bad.window = "26.10.2026,01:00:02,x,abc,45,No,No`n"; $bad.windowStartsMidRow = $false
$r.unparseable = (Get-CorePartitionFeedSummary $start $bad 120).reasons['rows']
$empty = $end.Clone(); $empty.window = ''; $empty.windowStartsMidRow = $false
$r.empty = (Get-CorePartitionFeedSummary $start $empty 120).reasons['rows']
$missing = @{ exists = $false; length = 0; ageSec = $null; before = ''; window = ''; windowStartsMidRow = $false }
$mf = Get-CorePartitionFeedSummary $missing $missing 120
$r.missing = @($mf.reasons['pkgWattsMean'], $mf.reasons['pkgTempCAtStart'])
$samples = @(
    @{ app = $true; inst = @('0,0', '0,12'); freq = @(2500, 2500); perf = @(160, 80); park = @(0, 0) },
    @{ app = $true; inst = @('0,0', '0,12'); freq = @(2500, 2500); perf = @(120, 100); park = @(1, 0) },
    @{ app = $false; inst = @('0,0', '0,12'); freq = @(2500, 2500); perf = @(10, 10); park = @(1, 1) })
$cs = Get-CorePartitionCounterSummary $samples 1 4096
$r.counters = @($cs.appSamples, $cs.pcoreMHzMean, $cs.ecoreMHzMean, $cs.pcoreParkedSampleFraction)
$r.workers = @((Get-CorePartitionWorkerThreads "playback_smoke.start session=1 worker_threads=16 x=1`nplayback_smoke.start session=3 scale_request=1 worker_threads=12 start_serial=2`n").value,
    (Get-CorePartitionWorkerThreads "nothing here").why, (Get-CorePartitionWorkerThreads '').why)
$helper = '{"topologyCheck":{"ok":true,"reason":null,"pMask":4095,"eMask":1044480},"helperAffinityObserved":1044480,"app":{"affinityFirst":4095,"affinityLast":4095},"load":{"seconds":10.0,"bytes":20000000000},"samples":[{"app":true,"inst":["0,0","0,12"],"freq":[2500,2500],"perf":[160,80],"park":[0,0]}]}' | ConvertFrom-Json
$w = @{ value = 16; why = $null }
$b2 = Get-CorePartitionScalars $helper $feed 4095 1044480 8 $w $null
$r.b2 = @($b2.corePartitionTopologyOk, $b2.appAffinityObserved, $b2.benchAffinityRequested, $b2.benchAffinityApplied, $b2.partitionLoadMask, $b2.partitionLoadApplied, $b2.partitionLoadGBps,
    $b2.pcoreMHzMean, $b2.ecoreMHzMean, $b2.pcoreParkedSampleFraction, $b2.playbackWorkerThreads, $b2.pkgWattsMean, $b2.corePartitionNullReasons, @($b2.Keys).Count)
$helperA = '{"topologyCheck":{"ok":true,"reason":null,"pMask":4095,"eMask":1044480},"helperAffinityObserved":1044480,"app":{"affinityFirst":1048575,"affinityLast":1048575},"load":{"seconds":0.0,"bytes":0},"samples":[]}' | ConvertFrom-Json
$a = Get-CorePartitionScalars $helperA $feed 0 0 0 $w $null
$r.a = @($a.appAffinityObserved, $a.benchAffinityApplied, $a.benchAffinityRequested, $a.partitionLoadApplied, $a.partitionLoadGBps, $a.pcoreMHzMean, $a.corePartitionNullReasons)
$helperMoved = '{"topologyCheck":{"ok":true,"reason":null,"pMask":4095,"eMask":1044480},"helperAffinityObserved":1044480,"app":{"affinityFirst":4095,"affinityLast":1048575},"load":{"seconds":0.5,"bytes":10},"samples":[]}' | ConvertFrom-Json
$m = Get-CorePartitionScalars $helperMoved $feed 4095 1044480 8 $w $null
$r.moved = @($m.appAffinityObserved, $m.benchAffinityApplied, $m.partitionLoadApplied)
$wrongHelper = '{"topologyCheck":{"ok":true,"reason":null,"pMask":4095,"eMask":1044480},"helperAffinityObserved":1048575,"app":{"affinityFirst":4095,"affinityLast":4095},"load":{"seconds":10,"bytes":10},"samples":[]}' | ConvertFrom-Json
$r.wrongHelper = (Get-CorePartitionScalars $wrongHelper $feed 4095 1044480 8 $w $null).partitionLoadApplied
$none = Get-CorePartitionScalars $null $mf 4095 0 0 @{ value = $null; why = 'run-log-missing' } 'helper-not-started'
$r.none = @($none.corePartitionTopologyOk, $none.appAffinityObserved, $none.benchAffinityApplied, $none.corePartitionNullReasons)
$badTopo = '{"topologyCheck":{"ok":false,"reason":"topology-uniform (1 efficiency class(es))","pMask":null,"eMask":null},"app":{"affinityFirst":4095,"affinityLast":4095},"samples":[]}' | ConvertFrom-Json
$r.badTopo = @((Get-CorePartitionScalars $badTopo $feed 4095 0 0 $w $null).corePartitionTopologyOk, (Get-CorePartitionScalars $badTopo $feed 4095 0 0 $w $null).corePartitionNullReasons)
# adversary r2 A3 / A4: a replaced or over-cap feed is no evidence (real files, the I/O readers)
$fa = Join-Path $env:CP_TMP 'feed-a.csv'
[IO.File]::WriteAllText($fa, "26.10.2026,01:00:00,x,30,10,No,No`n")
$ma = Read-CorePartitionFeedMark $fa
[IO.File]::AppendAllText($fa, "26.10.2026,01:00:01,x,90,99,Yes,No`n")
$ea = Read-CorePartitionFeedSince $fa ([int64]$ma.length) ([string]$ma.before)
$sa = Get-CorePartitionFeedSummary $ma $ea 120
$r.integrityAppend = @($ea.replaced, $ea.truncated, $sa.rows, $sa.pkgWattsMean, $sa.reasons.Count)
$mc = $ma.Clone(); $mc.created = [int64]$ma.created - 10000000
$r.integrityCreated = (Get-CorePartitionFeedSummary $mc $ea 120).reasons['rows']
[IO.File]::WriteAllText($fa, "26.10.2026,00:00:09,x,55,55,No,No`n26.10.2026,01:00:01,x,90,99,Yes,No`n")
$eb = Read-CorePartitionFeedSince $fa ([int64]$ma.length) ([string]$ma.before)
$r.integrityReplaced = @($eb.replaced, (Get-CorePartitionFeedSummary $ma $eb 120).reasons['rows'])
$fb = Join-Path $env:CP_TMP 'feed-b.csv'
[IO.File]::WriteAllText($fb, '')
$mb = Read-CorePartitionFeedMark $fb
$cool = '26.10.2026,00:00:00,x,30,10,No,No'
$sb = [Text.StringBuilder]::new()
for ($i = 0; $i -lt 32768; $i++) { [void]$sb.Append($cool + ',' + ('x' * (62 - $cool.Length)) + "`n") }
[void]$sb.Append("26.10.2026,00:00:01,x,90,99,Yes,No`n")
[IO.File]::AppendAllText($fb, $sb.ToString())
$ec = Read-CorePartitionFeedSince $fb ([int64]$mb.length) ([string]$mb.before)
$r.integrityTruncated = @($ec.truncated, (Get-CorePartitionFeedSummary $mb $ec 120).reasons['rows'], (Get-CorePartitionFeedSummary $mb $ec 120).pkgWattsMax)
# adversary r2 A1: a helper that was not ready at the measured launch fails the topology, bench and load flags closed
$helperGood = '{"topologyCheck":{"ok":true,"reason":null,"pMask":4095,"eMask":1044480},"helperAffinityObserved":1044480,"app":{"affinityFirst":4095,"affinityLast":4095},"load":{"seconds":15,"bytes":30000000000},"samples":[]}' | ConvertFrom-Json
$nr = Get-CorePartitionScalars $helperGood $feed 4095 1044480 8 $w 'helper-not-ready-at-launch'
$okc = Get-CorePartitionScalars $helperGood $feed 4095 1044480 8 $w $null
$r.notReady = @($nr.corePartitionTopologyOk, $nr.benchAffinityApplied, $nr.partitionLoadApplied, $nr.corePartitionNullReasons, $okc.corePartitionTopologyOk, $okc.benchAffinityApplied, $okc.partitionLoadApplied)
$r | ConvertTo-Json -Depth 6 -Compress
"""


@requires_windows_pwsh
class CorePartitionPureHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="core-partition-feed-")
        cls.addClassCleanup(cls._tmp.cleanup)
        proc = run_pwsh(["-Command", LIFT + FIXTURES], env_extra={"CP_GEN": str(GENERATOR), "CP_TMP": cls._tmp.name})
        cls.raw = proc
        cls.r = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.returncode == 0 and proc.stdout.strip() else None

    def setUp(self) -> None:
        self.assertIsNotNone(self.r, f"the lifted helpers did not run: {self.raw.stdout[-3000:]}{self.raw.stderr[-3000:]}")

    def test_masks(self) -> None:
        self.assertEqual(self.r["mask"], [4095, 1048575, "0x00FFF", "0xFFFFF"])

    def test_the_mask_converter_refuses_a_trailing_line_ending_typed(self) -> None:
        self.assertEqual(self.r["eol"], ["CORE_PARTITION_MASK_FORMAT"] * 4)

    def test_topology_class_check(self) -> None:
        t = self.r["topo"]
        self.assertEqual(t[:3], [True, 4095, 1044480])
        self.assertEqual((t[3], t[4].split(" ")[0]), (False, "bench-mask-not-all-p"))
        self.assertEqual((t[5], t[6].split(" ")[0]), (False, "low-mask-not-all-e"))
        self.assertTrue(t[7], "an A leg (no bench mask) still checks its helper mask and passes on E CPUs")
        self.assertEqual((t[8], t[9].split(" ")[0]), (False, "topology-uniform"))

    def test_effective_mhz(self) -> None:
        self.assertEqual(self.r["mhz"], [4000.0, None, None, None])

    def test_feed_window(self) -> None:
        # the mid-row fragment is dropped, a header row is skipped, CRLF rows parse; the start temperature is the newest row before the launch
        self.assertEqual(self.r["feed"], [3, 50.0, 55.0, 75.0, 80.0, 1, 62.5, 0])
        self.assertEqual(self.r["feedWhole"], 4, "a window that starts on a row boundary keeps its first row")
        self.assertEqual(self.r["rotated"], ["feed-rotated", 62.5])
        self.assertEqual(self.r["stale"], "feed-stale")
        self.assertEqual(self.r["unparseable"], "feed-unparseable")
        self.assertEqual(self.r["empty"], "feed-no-rows-in-window")
        self.assertEqual(self.r["missing"], ["feed-missing", "feed-missing"])

    def test_counter_summary_counts_app_alive_samples_only(self) -> None:
        self.assertEqual(self.r["counters"], [2, 3500.0, 2250.0, 0.5])

    def test_worker_threads(self) -> None:
        self.assertEqual(self.r["workers"], [12, "worker-threads-line-missing", "run-log-missing"])

    def test_a_replaced_or_over_cap_feed_is_no_evidence_and_an_append_still_is(self) -> None:
        # a plain append (the normal case) is unchanged: the one appended row, no reason
        self.assertEqual(self.r["integrityAppend"], [False, False, 1, 99.0, 0])
        self.assertEqual(self.r["integrityCreated"], "feed-replaced", "a feed whose creation time changed is a different file")
        self.assertEqual(self.r["integrityReplaced"], [True, "feed-replaced"], "different bytes just before the start offset: the file was replaced")
        truncated, reason, wmax = self.r["integrityTruncated"]
        self.assertTrue(truncated)
        self.assertEqual(reason, "feed-window-truncated", "a window over the 2 MiB cap must not report the last row's absence as a 10 W maximum")
        self.assertIsNone(wmax)

    def test_a_helper_not_ready_at_launch_fails_topology_bench_and_load_closed(self) -> None:
        not_ready, control = self.r["notReady"][:4], self.r["notReady"][4:]
        self.assertEqual(not_ready[:3], [False, False, False])
        self.assertIn("corePartitionTopologyOk=helper-not-ready-at-launch", not_ready[3])
        self.assertEqual(control, [True, True, True], "the same record with a ready helper is accepted")

    def test_summary_scalars(self) -> None:
        b2 = self.r["b2"]
        self.assertEqual(b2[:13], [True, "0x00FFF", "0x00FFF", True, "0xFF000", True, 2.0, 4000.0, 2000.0, 0.0, 16, 50.0, None])
        self.assertEqual(b2[13], len(SCALAR_NAMES), "the scalar set is exactly the summary lines' names")
        self.assertEqual(self.r["a"][:6], ["0xFFFFF", False, None, False, None, None])
        self.assertIn("pcoreMHzMean=no-app-samples", self.r["a"][6])
        self.assertEqual(self.r["moved"], ["0x00FFF/0xFFFFF", False, False], "a mask that moved, or a load under a second, is never applied")
        self.assertFalse(self.r["wrongHelper"], "a load whose helper ran off its mask is not applied")
        none = self.r["none"]
        self.assertEqual(none[:3], [False, None, False])
        for code in ("corePartitionTopologyOk=helper-not-started", "appAffinityObserved=app-not-seen", "pkgWattsMean=feed-missing", "playbackWorkerThreads=run-log-missing"):
            self.assertIn(code, none[3])
        self.assertFalse(self.r["badTopo"][0])
        self.assertIn("corePartitionTopologyOk=topology-uniform", self.r["badTopo"][1])


# --- the runner's mapping (offline test mode, stub generator) -------------------------------------------------------------------------------
PARAMS_ANCHOR = "$AutomationSettingsDir,$GpuPreLaunches)"
CP_STUB_GENERATOR = STUB_GENERATOR.replace(PARAMS_ANCHOR, "$AutomationSettingsDir,$GpuPreLaunches,$BenchAffinityMask,$PartitionLoadMask,$PartitionLoadThreads,"
                                                          "[switch]$CorePartitionTelemetry,$CorePartitionHelperMask)")


@requires_windows_pwsh
class CorePartitionRunnerMappingTests(RunnerHarness, unittest.TestCase):
    def setUp(self) -> None:
        self.assertEqual(STUB_GENERATOR.count(PARAMS_ANCHOR), 1, "the stub generator's param list moved")
        self.make_harness()
        self.gen.write_text(CP_STUB_GENERATOR, encoding="utf-8")

    def spec_with(self, args: dict | None, leg_id: str) -> Path:
        path = self.write_spec(card="BACHELOR-CORE-PARTITION-AB-1", leg_id=leg_id)
        spec = json.loads(path.read_text(encoding="utf-8"))
        if args is not None:
            spec["generatorArgs"] = args
        path.write_text(json.dumps(spec), encoding="utf-8")
        return path

    def test_the_five_generator_args_are_mapped(self) -> None:
        spec = self.spec_with({"benchAffinityMask": "0x00FFF", "partitionLoadMask": "0xFF000", "partitionLoadThreads": 8,
                               "corePartitionTelemetry": True, "corePartitionHelperMask": "0xFF000"}, "cp-b2")
        proc, _receipt, _submitted = self.run_leg("bachelor", spec)
        calls = self.generator_calls()
        self.assertTrue(calls, proc.stdout[-2000:] + proc.stderr[-2000:])
        fields = dict(kv.split("=", 1) for kv in calls[0].split(";") if "=" in kv)
        self.assertEqual((fields.get("BenchAffinityMask"), fields.get("PartitionLoadMask"), fields.get("PartitionLoadThreads"),
                          fields.get("CorePartitionTelemetry"), fields.get("CorePartitionHelperMask")), ("0x00FFF", "0xFF000", "8", "True", "0xFF000"))

    def run_validating_leg(self, args: dict, leg: str) -> dict:
        """The full runner path (Invoke-VenueLeg -> generator -> leg receipt) with the REAL mask validator: the stub generator lifts
        Assert-CorePartitionArgs from the shipped generator by AST and runs it on the arguments the runner mapped."""
        lift = ("$ast = [System.Management.Automation.Language.Parser]::ParseFile('" + str(GENERATOR) + "', [ref]$null, [ref]$null)\n"
                "foreach ($f in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -like '*CorePartition*' }, $false)) "
                "{ . ([scriptblock]::Create($f.Extent.Text)) }\n"
                "[void](Assert-CorePartitionArgs $BenchAffinityMask $PartitionLoadMask ([int]$PartitionLoadThreads) ([bool]$CorePartitionTelemetry) $CorePartitionHelperMask)\n")
        anchor = "if ($cfg.genRefusal) { throw $cfg.genRefusal }\n"
        self.assertEqual(CP_STUB_GENERATOR.count(anchor), 1, "the stub generator's refusal line moved")
        self.gen.write_text(CP_STUB_GENERATOR.replace(anchor, anchor + lift), encoding="utf-8")
        proc, receipt, submitted = self.run_leg("bachelor", self.spec_with(args, leg))
        self.assertIsNotNone(receipt, proc.stdout[-2000:] + proc.stderr[-2000:])
        self.assertEqual(submitted, [], "a refused generation submits nothing")
        return receipt

    def test_a_malformed_mask_is_a_typed_leg_refusal_not_a_runner_error(self) -> None:
        # dot B3 (bus 9bdd4c4): CORE_PARTITION_* was missing from the runner's typed-refusal whitelist, so a correctly typed converter refusal was
        # recorded as UNRESOLVED / RUNNER_ERROR; sol r2 (A6): a trailing LF used to skip the typed refusal for an untyped converter throw.
        base = {"corePartitionTelemetry": True, "corePartitionHelperMask": "0xFF000", "benchAffinityMask": "0x00FFF", "partitionLoadMask": "0xFF000", "partitionLoadThreads": 8}
        for key in ("benchAffinityMask", "partitionLoadMask", "corePartitionHelperMask"):
            for eol, suffix in (("lf", "\n"), ("crlf", "\r\n"), ("none", "")):
                with self.subTest(field=key, eol=eol):
                    args = dict(base)
                    args[key] = "FFF" if eol == "none" else base[key] + suffix   # only the mask under test is malformed, so the format check is the first refusal
                    receipt = self.run_validating_leg(args, f"cp-{key}-{eol}")
                    self.assertEqual(receipt["refusal"], "GENERATOR_REFUSED_CORE_PARTITION_MASK_FORMAT", receipt["outcomeDetail"])
                    self.assertNotIn("RUNNER_ERROR", receipt["outcomeDetail"])
                    self.assertNotIn(receipt["outcome"], ("PASS", "FAIL"))

    def test_a_typed_non_format_core_partition_refusal_is_a_leg_refusal_too(self) -> None:
        args = {"corePartitionTelemetry": True, "corePartitionHelperMask": "0xFF000", "benchAffinityMask": "0x00FFF", "partitionLoadMask": "0x00F00", "partitionLoadThreads": 8}
        receipt = self.run_validating_leg(args, "cp-overlap")
        self.assertEqual(receipt["refusal"], "GENERATOR_REFUSED_CORE_PARTITION_MASK_OVERLAP", receipt["outcomeDetail"])

    def test_absent_args_map_nothing(self) -> None:
        for args, leg in ((None, "cp-none"), ({"corePartitionTelemetry": False}, "cp-off")):
            with self.subTest(leg=leg):
                proc, _receipt, _submitted = self.run_leg("bachelor", self.spec_with(args, leg))
                calls = self.generator_calls()
                self.assertTrue(calls, proc.stdout[-2000:] + proc.stderr[-2000:])
                for name in ("BenchAffinityMask", "PartitionLoadMask", "PartitionLoadThreads", "CorePartitionTelemetry", "CorePartitionHelperMask"):
                    self.assertNotIn(name + "=", calls[0])


# --- the shipped specs and the schema -------------------------------------------------------------------------------------------------------
class CorePartitionSpecTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            import jsonschema  # noqa: F401
        except ImportError:
            self.skipTest("jsonschema is not installed")
        self.schema = json.loads((DV / "leg-spec.schema.json").read_text(encoding="utf-8"))

    def load(self, arm: str) -> dict:
        return json.loads((DV / LEGS[arm]).read_text(encoding="utf-8"))

    def test_the_three_specs_validate_and_share_the_headroom_shape(self) -> None:
        import jsonschema
        base = json.loads((DV / "legs" / "m16-1243-headroom-unpaced-windowed-s1.json").read_text(encoding="utf-8"))
        for arm in LEGS:
            with self.subTest(arm=arm):
                spec = self.load(arm)
                jsonschema.validate(spec, self.schema)
                self.assertEqual((spec["legId"], spec["card"]), (Path(LEGS[arm]).stem, "BACHELOR-CORE-PARTITION-AB-1"))
                for key in ("legType", "clipId", "playSeconds", "backends", "scaleFactor", "displayMode", "visualCapture", "visualCaptureOptOutReason",
                            "presentTearingExpectedReason", "timeouts"):
                    self.assertEqual(spec[key], base[key], key)
                extra = {k: v for k, v in spec["generatorArgs"].items() if k not in base["generatorArgs"]}
                self.assertEqual({k: spec["generatorArgs"][k] for k in base["generatorArgs"]}, base["generatorArgs"])
                want = {"corePartitionTelemetry": True, "corePartitionHelperMask": "0xFF000"}
                if arm in ("b1", "b2"):
                    want["benchAffinityMask"] = "0x00FFF"
                if arm == "b2":
                    want.update(partitionLoadMask="0xFF000", partitionLoadThreads=8)
                self.assertEqual(extra, want)
                self.assertEqual(spec["criteria"]["acceptance"], spec["criteria"]["supplementary"], "identical criteria in both roles")
                for backend in ("cuda", "cpu"):
                    rows = spec["criteria"]["acceptance"][backend]
                    self.assertIn({"metric": "corePartitionTopologyOk", "op": "eq", "value": True}, rows)
                    self.assertEqual({"metric": "appAffinityObserved", "op": "eq", "value": "0xFFFFF"} in rows, arm == "a")
                    self.assertEqual({"metric": "benchAffinityApplied", "op": "eq", "value": True} in rows, arm != "a")
                    self.assertEqual({"metric": "partitionLoadApplied", "op": "eq", "value": True} in rows, arm == "b2")
                    self.assertEqual({"metric": "gpuFramesTotal", "op": "gt", "value": 0} in rows, backend == "cuda")

    def test_the_schema_refuses_a_bad_mask_or_thread_count(self) -> None:
        import jsonschema
        spec = self.load("b2")
        for bad in ({"benchAffinityMask": "0xFFFFFF"}, {"benchAffinityMask": "FFF"}, {"benchAffinityMask": "0x0"}, {"partitionLoadMask": "0x000"},
                    {"corePartitionHelperMask": "0xG0000"}, {"partitionLoadThreads": 0}, {"partitionLoadThreads": 17}, {"corePartitionTelemetry": "yes"}):
            broken = json.loads(json.dumps(spec))
            broken["generatorArgs"].update(bad)
            with self.subTest(bad=bad), self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(broken, self.schema)

    def test_the_schema_refuses_a_mask_with_a_trailing_line_ending(self) -> None:
        # sol r2 (A6): jsonschema searches with Python's re, where `$` also matches before a final LF, so "0x00FFF\n" validated.
        import jsonschema
        spec = self.load("b2")
        for key in ("benchAffinityMask", "partitionLoadMask", "corePartitionHelperMask"):
            for eol in ("\n", "\r\n", "\n\n"):
                broken = json.loads(json.dumps(spec))
                broken["generatorArgs"][key] = spec["generatorArgs"][key] + eol
                with self.subTest(field=key, eol=eol), self.assertRaises(jsonschema.ValidationError):
                    jsonschema.validate(broken, self.schema)


if __name__ == "__main__":
    unittest.main()
