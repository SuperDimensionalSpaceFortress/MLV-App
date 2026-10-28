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
[IO.File]::WriteAllText($env:MLV_CP_READY + '.len', [string](New-Object IO.FileInfo $f).Length)
# r8 A4: the marker the real helper writes, the HMAC of "ready:<its pid>" under the job's key
$u8 = [Text.Encoding]::UTF8
[IO.File]::WriteAllText($env:MLV_CP_READY, [Convert]::ToHexString([Security.Cryptography.HMACSHA256]::new($u8.GetBytes([string]$env:MLV_CP_NONCE)).ComputeHash($u8.GetBytes("ready:$PID"))))
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
    for ($i = 0; $i -lt 40 -and -not $ready; $i++) { $ready = (Get-Content -LiteralPath ($corePartitionReadyFile + '.len') -Raw -ErrorAction SilentlyContinue); if (-not $ready) { Start-Sleep -Milliseconds 100 } }
    [IO.File]::AppendAllText($corePartitionFeedPath, "26.10.2026,01:00:02,x,70.0,45.0,No,No`n26.10.2026,01:00:03,x,80.0,55.0,No,No`n26.10.2026,01:00:04,x,75.0,50.0,No,No`n")
    $end = Read-CorePartitionFeedSince $corePartitionFeedPath ([int64]$corePartitionFeedStart.length)
    $fb = @{}; if ((Get-Command Get-CorePartitionFeedSummary).Parameters.ContainsKey('Base')) { $fb.Base = $corePartitionFeedPreHelper }
    # r10 B2: the window ends as this end read starts (the job reads the feed right after the smoke run ends)
    if ((Get-Command Get-CorePartitionFeedSummary).Parameters.ContainsKey('WindowEndMs')) { $fb.WindowEndMs = $end.monoMs }
    # r11 B1: and the app's last observed-alive poll at it too (the window end lags no exit)
    if ((Get-Command Get-CorePartitionFeedSummary).Parameters.ContainsKey('AppAliveMs')) { $fb.AppAliveMs = $end.monoMs }
    $sum = Get-CorePartitionFeedSummary $corePartitionFeedStart $end 120 @fb
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

# r8 A3 / A4 (adversary r7 6fac125a A3, A4): the job's pre-launch region, cleanup block and scalar line run for real (job "a", no bench mask), with a
# stand-in helper whose ready marker is per CP_MARKER: a directory, a file carrying another key's HMAC, the pre-r8 'ready' text, an empty file, or the
# right HMAC (control). 'start-throws' points ProgramFiles at a missing folder, so the helper's Process.Start throws. A fresh complete feed row is
# appended inside the window each time; a helper that was not ready (or never started) must publish no package-feed value in either output.
MARKER_HELPER = r"""
$m = $env:CP_MARKER; $r = $env:MLV_CP_READY; $u8 = [Text.Encoding]::UTF8
function Get-Mac([string]$Key) { [Convert]::ToHexString([Security.Cryptography.HMACSHA256]::new($u8.GetBytes($Key)).ComputeHash($u8.GetBytes("ready:$PID"))) }
if ($m -eq 'directory') { [void](New-Item -ItemType Directory -Path $r) }
elseif ($m -eq 'foreign-key') { [IO.File]::WriteAllText($r, (Get-Mac 'a-key-the-job-never-handed-out')) }
elseif ($m -eq 'pre-r8-text') { [IO.File]::WriteAllText($r, 'ready') }
elseif ($m -eq 'empty') { [IO.File]::WriteAllText($r, '') }
elseif ($m -like 'good*') { [IO.File]::WriteAllText($r, (Get-Mac ([string]$env:MLV_CP_NONCE))) }
Start-Sleep -Seconds 2
"""
MARKER_DRIVER = r"""
$ErrorActionPreference = 'Stop'
$job = [IO.File]::ReadAllText($env:CP_JOB) -replace "`r`n", "`n"
$regions = @(); $at = 0
while (($s = $job.IndexOf($env:CP_OPEN, $at)) -ge 0) { $e = $job.IndexOf($env:CP_CLOSE, $s); $regions += $job.Substring($s, $e - $s); $at = $e + 1 }
$fake = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($env:CP_FAKE_HELPER))
$pre = [regex]::Replace($regions[0], "(?m)^\`$CorePartitionHelperEncoded = '[^']*'", { "`$CorePartitionHelperEncoded = '$fake'" })
$pre = [regex]::Replace($pre, '(?m)^\$CorePartitionHelperAffinity = \[int64\]\d+', '$CorePartitionHelperAffinity = [int64]1')
function Write-JobTrace([string]$Message) { }
function Publish-AttrCudaText { param([string]$Path, [string]$Value) [IO.File]::WriteAllText($Path, $Value) }
function New-AttrCudaDirectory { param([string]$Path) }
# r13c (Dot 6456731376): each publication is captured BY VALUE (serialized as the real Save-Json does, at call time), so a later change to the
# record object cannot make an earlier publication look republished; $global:cpSaveThrowOn makes that call (1-based) throw instead of publishing
function Save-Json { param($Object, $Path) $global:cpSaveCalls++; if ($global:cpSaveThrowOn -eq $global:cpSaveCalls) { throw 'save-json-refused-by-test' }
    $global:cpSaved = $Object | ConvertTo-Json -Depth 30 | ConvertFrom-Json -AsHashtable; $global:cpSaves.Add($global:cpSaved) }
function Resolve-AttrCudaSmokeRunLog { param($ResultJsonPath, $ContainingRoot) $p = Join-Path $ContainingRoot 'run.log'; if (-not (Test-Path -LiteralPath $p -PathType Leaf)) { throw 'run log unresolvable' }; [pscustomobject]@{ path = $p; sha256 = (Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash.ToLowerInvariant() } }
$exePath = 'C:\no\such\MLVApp.exe'
$SmokeProcessTimeoutMs = 60000
$out = [ordered]@{}
foreach ($mode in ($env:CP_MODES -split ',')) {
  try {
    $Work = Join-Path $env:CP_WORK $mode; [void](New-Item -ItemType Directory -Path $Work); $Pub = $Work
    $env:HEAVY_SLOT_FEED = Join-Path $Work 'feed.csv'
    [IO.File]::WriteAllText($env:HEAVY_SLOT_FEED, "26.10.2026,00:59:59,x,20.0,5.0,No,No`n")
    $env:CP_MARKER = $mode
    $pf = $env:ProgramFiles
    if ($mode -eq 'start-throws') { $env:ProgramFiles = Join-Path $Work 'no-such-program-files' }
    try { . ([scriptblock]::Create($pre)) } finally { $env:ProgramFiles = $pf }
    # r13c (rule (a)): the window row carries its HWiNFO stamp (local time), 400 ms after the measured launch, after the playback start (100 ms)
    [IO.File]::AppendAllText($env:HEAVY_SLOT_FEED, $corePartitionMeasuredLaunchUtc.AddMilliseconds(400).ToLocalTime().ToString('d.M.yyyy,H:mm:ss.fff', [Globalization.CultureInfo]::InvariantCulture) + ",x,70.0,45.0,No,No`n")
    $resultPath = Join-Path $Work 'result.json'
    if ($mode -like 'good*') {
        # r11 B1: the stand-in helper writes no record, so the driver writes the genuine helper's (signed under the job's key, the runner's pid,
        # the app's last alive poll 110 ms ago and its observed exit on the job's monotonic base); without it the window end is unverified
        $now = [DateTime]::UtcNow
        [IO.File]::WriteAllText($resultPath, ([ordered]@{ process = [ordered]@{ id = 5000; startedAtUtc = $now.AddSeconds(-5).ToString('o'); endedAtUtc = $now.ToString('o') } } | ConvertTo-Json -Depth 3))
        $mb = ((Get-CorePartitionMonoMs) - 150).ToString([Globalization.CultureInfo]::InvariantCulture)
        $inst = '{"pid":5000,"startUtcTicks":' + $now.AddSeconds(-5).Ticks + ',"firstSeenUtcTicks":' + $now.AddSeconds(-4.9).Ticks + ',"firstMs":10,"aliveMs":40,"goneUtcTicks":' + $now.Ticks + ',"goneMs":50}'
        $rb = [Text.Encoding]::UTF8.GetBytes('{"schema":"mlv-app/core-partition-helper/v1","pid":' + $corePartitionHelperLaunch.pid + ',"monoBaseMs":' + $mb + ',"app":' + $inst + ',"instances":[' + $inst + '],"others":[],"clockSkewMaxMs":0}')
        [IO.File]::WriteAllBytes($corePartitionHelperOut, $rb)
        [IO.File]::WriteAllText("$corePartitionHelperOut.mac", [Convert]::ToHexString([Security.Cryptography.HMACSHA256]::new([Text.Encoding]::UTF8.GetBytes($corePartitionHelperNonce)).ComputeHash($rb)))
        # r13 (CP-FEED-START-ANCHOR): the run log the result names; the measured session's playback starts 100 ms after the measured launch
        # r13b (A1): and its summary, whose stamp less the start's equals its monotonic elapsed_ms
        [IO.File]::WriteAllText((Join-Path $Work 'run.log'), '[' + $corePartitionMeasuredLaunchUtc.AddMilliseconds(100).ToString('yyyy-MM-ddTHH:mm:ss.fffZ') + "] [info] [0x1] playback_smoke.start session=1 worker_threads=16`n[" + $corePartitionMeasuredLaunchUtc.AddMilliseconds(110).ToString('yyyy-MM-ddTHH:mm:ss.fffZ') + "] [info] [0x1] playback_smoke.measured_session id=1`n[" + $corePartitionMeasuredLaunchUtc.AddMilliseconds(150).ToString('yyyy-MM-ddTHH:mm:ss.fffZ') + "] [info] [0x1] playback_smoke.summary session=1 reason=play-stop elapsed_ms=50.000 presented_frames=1`n")
    }
    $global:cpSaved = $null; $global:cpSaves = [Collections.Generic.List[object]]::new(); $global:cpSaveCalls = 0
    $global:cpSaveThrowOn = $(if ($mode -eq 'good-rebound-republish-throws') { 2 } else { 0 })
    . ([scriptblock]::Create($regions[1]))
    # r13b (A2 binding): the metrics read the same run log the start verdict read
    $rawLog = $(if (Test-Path -LiteralPath (Join-Path $Work 'run.log')) { [IO.File]::ReadAllText((Join-Path $Work 'run.log')) } else { "playback_smoke.start session=1 worker_threads=16`n" })
    # r13b (adversary r13b e02b23e3 A1): good-rebound's metrics read a different snapshot than the start verdict did
    if ($mode -like 'good-rebound*') { $rawLog = $rawLog.Replace('worker_threads=16', 'worker_threads=8') }
    . ([scriptblock]::Create($regions[2]))
    $ps = $global:cpSaved.feed.summary; $sc = $corePartitionScalars
    $saves = @(foreach ($sv in $global:cpSaves) { $ss = $sv.feed.summary
        , @($ss.rows, $ss.pkgWattsMean, $ss.pkgTempCMean, [string]$sv.feed.startWhy, @(foreach ($fw in @('rows', 'pkgWattsMean', 'pkgWattsMax', 'pkgTempCMean', 'pkgTempCMax', 'pkgThrottledRows')) { [string]$ss.reasons[$fw] })) })
    $out[$mode] = [ordered]@{
        saveCalls = $global:cpSaveCalls; saves = $saves; publishWhy = [string]$corePartitionPublishWhy
        started = ($null -ne $corePartitionHelper); ready = [bool]$corePartitionHelperLaunch.ready; readyWhy = [string]$corePartitionHelperLaunch['readyWhy']
        notReady = [bool]$corePartitionHelperNotReady; helperWhy = [string]$corePartitionHelperWhy; publishedNotReady = [bool]$global:cpSaved.helperNotReady
        published = @($ps.rows, $ps.pkgWattsMean, $ps.pkgTempCMean, [string]$ps.reasons['pkgWattsMean']); startWhy = [string]$global:cpSaved.feed.startWhy; pkg = @($sc.pkgWattsMean, $sc.pkgWattsMax, $sc.pkgTempCMean, $sc.pkgTempCMax)
        topo = $sc.corePartitionTopologyOk; why = [string]$sc.corePartitionNullReasons; errors = @($corePartitionErrors)
    }
    if ($null -ne $corePartitionHelper -and -not $corePartitionHelper.HasExited) { $corePartitionHelper.Kill() }
  } catch { $out[$mode] = [ordered]@{ threw = [string]$_.Exception.Message + ' @ ' + [string]$_.InvocationInfo.PositionMessage } }
}
$out | ConvertTo-Json -Compress -Depth 8
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
    $psi.Environment['MLV_CP_MEASURED'] = Join-Path $dir 'measured'
    $psi.Environment['MLV_CP_BENCH'] = '0'; $psi.Environment['MLV_CP_LOAD'] = '0'; $psi.Environment['MLV_CP_HELPER'] = '1'; $psi.Environment['MLV_CP_THREADS'] = '0'
    $psi.Environment['MLV_CP_DEADLINE_MS'] = '90000'
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
    [IO.File]::WriteAllText((Join-Path $dir 'measured'), [string]$post.Id)
    Set-Content -LiteralPath (Join-Path $dir 'stop') -Value 'stop'
    if (-not $h.WaitForExit(20000)) { $h.Kill() }
    $r = Get-Content -LiteralPath (Join-Path $dir 'helper.json') -Raw | ConvertFrom-Json
    [ordered]@{ prePid = $pre.Id; midPid = $mid.Id; postPid = $post.Id; seen = $r.app.pid; instances = @($r.instances | ForEach-Object { $_.pid }) } | ConvertTo-Json -Compress
} finally {
    if (-not $pre.HasExited) { $pre.Kill() }
    if ($null -ne $mid -and -not $mid.HasExited) { $mid.Kill() }
}
"""


# sol r4 A2: the smoke runner starts a short same-exe probe (--help) after the measured-launch instant and before the measured app. The decoded helper,
# with its load on, runs against a stand-in probe (~5 s) and then the stand-in measured app (~8 s): only the measured instance's samples are app samples
# and the load totals are its own.
PROBE_DRIVER = r"""
$ErrorActionPreference = 'Stop'
$dir = $env:CP_DIR
$exe = Join-Path $dir 'mlvfake.exe'
Copy-Item -LiteralPath 'C:\Windows\System32\PING.EXE' -Destination $exe
$psi = [Diagnostics.ProcessStartInfo]::new("$env:ProgramFiles\PowerShell\7\pwsh.exe", "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand $($env:CP_ENCODED)")
$psi.UseShellExecute = $false; $psi.CreateNoWindow = $true
$psi.Environment['MLV_CP_EXE'] = $exe
$psi.Environment['MLV_CP_STOP'] = Join-Path $dir 'stop'
$psi.Environment['MLV_CP_READY'] = Join-Path $dir 'ready'
$psi.Environment['MLV_CP_OUT'] = Join-Path $dir 'helper.json'
$psi.Environment['MLV_CP_LAUNCH'] = Join-Path $dir 'launch'
$psi.Environment['MLV_CP_MEASURED'] = Join-Path $dir 'measured'
$psi.Environment['MLV_CP_BENCH'] = '0'; $psi.Environment['MLV_CP_LOAD'] = '1'; $psi.Environment['MLV_CP_HELPER'] = '1'; $psi.Environment['MLV_CP_THREADS'] = '1'
$psi.Environment['MLV_CP_DEADLINE_MS'] = '120000'
$h = [Diagnostics.Process]::Start($psi)
try {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while (-not (Test-Path (Join-Path $dir 'ready')) -and -not $h.HasExited -and $sw.ElapsedMilliseconds -lt 60000) { Start-Sleep -Milliseconds 100 }
    [IO.File]::WriteAllText((Join-Path $dir 'launch'), [string][DateTime]::UtcNow.Ticks)
    Start-Sleep -Milliseconds 300
    $probe = Start-Process -FilePath $exe -ArgumentList @('-n', '6', '127.0.0.1') -PassThru -WindowStyle Hidden
    [void]$probe.WaitForExit(30000)
    Start-Sleep -Milliseconds 800
    $measured = Start-Process -FilePath $exe -ArgumentList @('-n', '9', '127.0.0.1') -PassThru -WindowStyle Hidden
    [void]$measured.WaitForExit(30000)
    Start-Sleep -Seconds 1
    [IO.File]::WriteAllText((Join-Path $dir 'measured'), [string]$measured.Id)
    Set-Content -LiteralPath (Join-Path $dir 'stop') -Value 'stop'
    if (-not $h.WaitForExit(30000)) { $h.Kill() }
    $r = Get-Content -LiteralPath (Join-Path $dir 'helper.json') -Raw | ConvertFrom-Json
    $life = @{}; foreach ($i in @($r.instances)) { $life[[string]$i.pid] = ([int64]$i.lastSeenUtcTicks - [int64]$i.firstSeenUtcTicks) / 1e7 }
    [ordered]@{ probePid = $probe.Id; measuredPid = $measured.Id; app = $r.app.pid; instances = @($r.instances | ForEach-Object { $_.pid }); measuredLifeSec = $life[[string]$measured.Id]
        loadSec = $r.load.seconds; loadBytes = $r.load.bytes; sessions = $r.load.sessions; appSamples = @($r.samples | Where-Object { $_.app }).Count; errors = @($r.errors) } | ConvertTo-Json -Compress
} finally {
    if (-not $h.HasExited) { $h.Kill() }
}
"""


# sol r4 CP-02: an unrelated same-path process starts right after the measured-launch instant and lives throughout; the measured app starts later.
# The r4 helper bound the first one and never looked again. The decoded helper is run for real (stand-in exe, never MLVApp), handed the measured
# pid the way the job hands it over (before the stop file), and its record goes through the generator's own Get-CorePartitionScalars (lifted).
SAME_PATH_DRIVER = r"""
$ErrorActionPreference = 'Stop'
$dir = $env:CP_DIR
$exe = Join-Path $dir 'mlvfake.exe'
Copy-Item -LiteralPath 'C:\Windows\System32\PING.EXE' -Destination $exe
$psi = [Diagnostics.ProcessStartInfo]::new("$env:ProgramFiles\PowerShell\7\pwsh.exe", "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand $($env:CP_ENCODED)")
$psi.UseShellExecute = $false; $psi.CreateNoWindow = $true
$psi.Environment['MLV_CP_EXE'] = $exe
$psi.Environment['MLV_CP_STOP'] = Join-Path $dir 'stop'
$psi.Environment['MLV_CP_READY'] = Join-Path $dir 'ready'
$psi.Environment['MLV_CP_OUT'] = Join-Path $dir 'helper.json'
$psi.Environment['MLV_CP_LAUNCH'] = Join-Path $dir 'launch'
$psi.Environment['MLV_CP_MEASURED'] = Join-Path $dir 'measured'
$psi.Environment['MLV_CP_BENCH'] = '0'; $psi.Environment['MLV_CP_LOAD'] = '1'; $psi.Environment['MLV_CP_HELPER'] = '1'; $psi.Environment['MLV_CP_THREADS'] = '1'
$psi.Environment['MLV_CP_DEADLINE_MS'] = '120000'
$h = [Diagnostics.Process]::Start($psi)
$other = $null; $pre = $null
try {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while (-not (Test-Path (Join-Path $dir 'ready')) -and -not $h.HasExited -and $sw.ElapsedMilliseconds -lt 60000) { Start-Sleep -Milliseconds 100 }
    # adversary r5 A1: a same-path process started BEFORE the launch instant and still alive through the measurement
    $pre = Start-Process -FilePath $exe -ArgumentList @('-n', '30', '127.0.0.1') -PassThru -WindowStyle Hidden
    Start-Sleep -Milliseconds 700
    [IO.File]::WriteAllText((Join-Path $dir 'launch'), [string][DateTime]::UtcNow.Ticks)
    Start-Sleep -Milliseconds 50
    $other = Start-Process -FilePath $exe -ArgumentList @('-n', '30', '127.0.0.1') -PassThru -WindowStyle Hidden
    Start-Sleep -Milliseconds 1500
    $measured = Start-Process -FilePath $exe -ArgumentList @('-n', '5', '127.0.0.1') -PassThru -WindowStyle Hidden
    $measuredStart = $measured.StartTime.ToUniversalTime().Ticks - 5000000
    [void]$measured.WaitForExit(30000)
    $measuredEnd = [DateTime]::UtcNow.Ticks
    Start-Sleep -Seconds 1
    [IO.File]::WriteAllText((Join-Path $dir 'measured'), [string]$measured.Id)
    Set-Content -LiteralPath (Join-Path $dir 'stop') -Value 'stop'
    if (-not $h.WaitForExit(30000)) { $h.Kill() }
    $r = Get-Content -LiteralPath (Join-Path $dir 'helper.json') -Raw | ConvertFrom-Json
    $bench = [int64]@($r.instances)[0].affinityFirst
    $mp = @{}; if ((Get-Command Get-CorePartitionScalars).Parameters.ContainsKey('MeasuredPid')) { $mp.MeasuredPid = [int64]$measured.Id; $mp.MeasuredStartTicks = $measuredStart; $mp.MeasuredEndTicks = $measuredEnd }
    $s = Get-CorePartitionScalars $r ([ordered]@{ reasons = [ordered]@{} }) $bench 0 0 @{ value = 1; why = $null } $null $false @mp
    [ordered]@{ prePid = $pre.Id; otherPid = $other.Id; measuredPid = $measured.Id; app = $r.app.pid; instances = @($r.instances | ForEach-Object { $_.pid }); others = @($r.others | ForEach-Object { $_.pid }); recordedMeasuredPid = $r.measuredPid
        appSamples = @($r.samples | Where-Object { $_.app -and $_.appPid -ne $measured.Id }).Count; bench = $s.benchAffinityApplied; observed = $s.appAffinityObserved; why = [string]$s.corePartitionNullReasons } | ConvertTo-Json -Compress
} finally {
    if (-not $h.HasExited) { $h.Kill() }
    if ($null -ne $other -and -not $other.HasExited) { $other.Kill() }
    if ($null -ne $pre -and -not $pre.HasExited) { $pre.Kill() }
}
"""


# sol r6 CP-06: the decoded helper run for real, told to stop at once. Clean: it exits 0 and its record's HMAC under the job's key verifies (the
# generator's own Test-CorePartitionHelperMac, lifted). Planted: a file already sits at its record name, so its CreateNew refuses and it exits non-zero.
EXIT_DRIVER = r"""
$ErrorActionPreference = 'Stop'
function Start-CpHelper([string]$Sub, [bool]$Plant) {
    $d = Join-Path $env:CP_DIR $Sub
    [void](New-Item -ItemType Directory -Path $d)
    [IO.File]::WriteAllText((Join-Path $d 'stop'), 'stop')
    if ($Plant) { [IO.File]::WriteAllText((Join-Path $d 'helper.json'), '{"schema":"mlv-app/core-partition-helper/v1","pid":1}') }
    $psi = [Diagnostics.ProcessStartInfo]::new("$env:ProgramFiles\PowerShell\7\pwsh.exe", "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand $($env:CP_ENCODED)")
    $psi.UseShellExecute = $false; $psi.CreateNoWindow = $true
    $psi.Environment['MLV_CP_EXE'] = Join-Path $d 'no-such-app.exe'
    $psi.Environment['MLV_CP_STOP'] = Join-Path $d 'stop'
    $psi.Environment['MLV_CP_READY'] = Join-Path $d 'ready'
    $psi.Environment['MLV_CP_OUT'] = Join-Path $d 'helper.json'
    $psi.Environment['MLV_CP_LAUNCH'] = Join-Path $d 'launch'
    $psi.Environment['MLV_CP_MEASURED'] = Join-Path $d 'measured'
    $psi.Environment['MLV_CP_BENCH'] = '0'; $psi.Environment['MLV_CP_LOAD'] = '0'; $psi.Environment['MLV_CP_HELPER'] = '1'; $psi.Environment['MLV_CP_THREADS'] = '0'
    $psi.Environment['MLV_CP_DEADLINE_MS'] = '60000'
    $psi.Environment['MLV_CP_NONCE'] = 'exit-driver-key-0123456789abcdef'
    $h = [Diagnostics.Process]::Start($psi)
    if (-not $h.WaitForExit(60000)) { $h.Kill(); [void]$h.WaitForExit(5000) }
    $mac = Join-Path $d 'helper.json.mac'
    $bytes = [IO.File]::ReadAllBytes((Join-Path $d 'helper.json'))
    $macText = $(if (Test-Path -LiteralPath $mac) { [IO.File]::ReadAllText($mac) })
    $ok = $(if (Get-Command Test-CorePartitionHelperMac -ErrorAction SilentlyContinue) { @((Test-CorePartitionHelperMac $bytes $macText 'exit-driver-key-0123456789abcdef'), (Test-CorePartitionHelperMac $bytes $macText 'another-key')) } else { @('absent') })
    # r8 A4: its ready marker verifies for this key and its own pid only; r8 A1: its record carries its largest wall/monotonic clock gap
    $rf = Join-Path $d 'ready'
    $readyOk = $(if (Get-Command Test-CorePartitionReadyMarker -ErrorAction SilentlyContinue) { @((Test-CorePartitionReadyMarker $rf (Get-CorePartitionReadyToken 'exit-driver-key-0123456789abcdef' $h.Id)),
        (Test-CorePartitionReadyMarker $rf (Get-CorePartitionReadyToken 'another-key' $h.Id)), (Test-CorePartitionReadyMarker $rf (Get-CorePartitionReadyToken 'exit-driver-key-0123456789abcdef' ($h.Id + 1)))) } else { @('absent') })
    $skew = $(try { ([Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json).clockSkewMaxMs } catch { 'unparsed' })
    @{ exit = $h.ExitCode; macFile = (Test-Path -LiteralPath $mac); mac = $ok; nonceInRecord = ([Text.Encoding]::UTF8.GetString($bytes)).Contains('exit-driver-key'); ready = $readyOk; skew = $skew }
}
$clean = Start-CpHelper 'clean' $false
$planted = Start-CpHelper 'planted' $true
[ordered]@{ clean = $clean; planted = $planted } | ConvertTo-Json -Compress -Depth 4
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


# r9 Dot 1 (= adversary r8 aeacec83 A1), the real helper (stand-in exe, never MLVApp). stop-while-alive: the measured pid and the stop file are
# written while the measured app still runs (an early stop), so the helper's watch ends before the runner's exit. stop-right-after-exit (control):
# both are written the instant the measured app exits, as the job does after the runner; the helper must still observe that exit.
COVERAGE_DRIVER = r"""
$ErrorActionPreference = 'Stop'
$out = [ordered]@{}
foreach ($mode in @('stop-while-alive', 'stop-right-after-exit')) {
    $dir = Join-Path $env:CP_DIR $mode
    [void](New-Item -ItemType Directory -Path $dir)
    $exe = Join-Path $dir 'mlvfake.exe'
    Copy-Item -LiteralPath 'C:\Windows\System32\PING.EXE' -Destination $exe
    $psi = [Diagnostics.ProcessStartInfo]::new("$env:ProgramFiles\PowerShell\7\pwsh.exe", "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand $($env:CP_ENCODED)")
    $psi.UseShellExecute = $false; $psi.CreateNoWindow = $true
    $psi.Environment['MLV_CP_EXE'] = $exe
    $psi.Environment['MLV_CP_STOP'] = Join-Path $dir 'stop'
    $psi.Environment['MLV_CP_READY'] = Join-Path $dir 'ready'
    $psi.Environment['MLV_CP_OUT'] = Join-Path $dir 'helper.json'
    $psi.Environment['MLV_CP_LAUNCH'] = Join-Path $dir 'launch'
    $psi.Environment['MLV_CP_MEASURED'] = Join-Path $dir 'measured'
    $psi.Environment['MLV_CP_BENCH'] = '0'; $psi.Environment['MLV_CP_LOAD'] = '0'; $psi.Environment['MLV_CP_HELPER'] = '1'; $psi.Environment['MLV_CP_THREADS'] = '0'
    $psi.Environment['MLV_CP_DEADLINE_TICKS'] = [string]([DateTime]::UtcNow.AddSeconds(120).Ticks); $psi.Environment['MLV_CP_DEADLINE_MS'] = '120000'
    $h = [Diagnostics.Process]::Start($psi)
    $measured = $null
    try {
        $sw = [Diagnostics.Stopwatch]::StartNew()
        while (-not (Test-Path (Join-Path $dir 'ready')) -and -not $h.HasExited -and $sw.ElapsedMilliseconds -lt 60000) { Start-Sleep -Milliseconds 100 }
        [IO.File]::WriteAllText((Join-Path $dir 'launch'), [string][DateTime]::UtcNow.Ticks)
        Start-Sleep -Milliseconds 300
        $measured = Start-Process -FilePath $exe -ArgumentList @('-n', $(if ($mode -eq 'stop-while-alive') { '12' } else { '4' }), '127.0.0.1') -PassThru -WindowStyle Hidden
        $measuredStart = $measured.StartTime.ToUniversalTime().Ticks - 5000000
        if ($mode -eq 'stop-while-alive') { Start-Sleep -Seconds 2 } else { [void]$measured.WaitForExit(30000) }
        [IO.File]::WriteAllText((Join-Path $dir 'measured'), [string]$measured.Id)
        [IO.File]::WriteAllText((Join-Path $dir 'stop'), 'stop')
        if (-not $h.WaitForExit(30000)) { $h.Kill() }
        [void]$measured.WaitForExit(30000)
        $measuredEnd = [DateTime]::UtcNow.Ticks
        $r = Get-Content -LiteralPath (Join-Path $dir 'helper.json') -Raw | ConvertFrom-Json
        $inst = @($r.instances | Where-Object { $_.pid -eq $measured.Id })
        $bench = [int64]@($inst)[0].affinityFirst
        $s = Get-CorePartitionScalars $r ([ordered]@{ reasons = [ordered]@{} }) $bench 0 0 @{ value = 1; why = $null } $null $false -MeasuredPid ([int64]$measured.Id) -MeasuredStartTicks $measuredStart -MeasuredEndTicks $measuredEnd
        $out[$mode] = [ordered]@{ endedBy = $r.endedBy; app = $r.app.pid; measuredPid = $measured.Id; gone = @($inst)[0].goneUtcTicks; goneMs = @($inst)[0].goneMs; aliveMs = @($inst)[0].aliveMs
            bench = $s.benchAffinityApplied; observed = $s.appAffinityObserved; why = [string]$s.corePartitionNullReasons }
    } finally {
        if (-not $h.HasExited) { $h.Kill() }
        if ($null -ne $measured -and -not $measured.HasExited) { $measured.Kill() }
    }
}
$out | ConvertTo-Json -Compress -Depth 4
"""


# r9 Dot 3 (= adversary r8 aeacec83 A3), the real helper with no stop file and a lost parent. A backward wall-clock step makes a wall-clock deadline
# lie far in the future: the seam hands the helper a wall deadline an hour away (MLV_CP_DEADLINE_TICKS, what an aeacec83 helper reads) and a 4 s
# monotonic budget (MLV_CP_DEADLINE_MS). The helper must end itself at the monotonic budget (endedBy=deadline), not run on for the hour.
DEADLINE_DRIVER = r"""
$ErrorActionPreference = 'Stop'
$dir = $env:CP_DIR
$psi = [Diagnostics.ProcessStartInfo]::new("$env:ProgramFiles\PowerShell\7\pwsh.exe", "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand $($env:CP_ENCODED)")
$psi.UseShellExecute = $false; $psi.CreateNoWindow = $true
$psi.Environment['MLV_CP_EXE'] = Join-Path $dir 'no-such-app.exe'
$psi.Environment['MLV_CP_STOP'] = Join-Path $dir 'stop'
$psi.Environment['MLV_CP_READY'] = Join-Path $dir 'ready'
$psi.Environment['MLV_CP_OUT'] = Join-Path $dir 'helper.json'
$psi.Environment['MLV_CP_LAUNCH'] = Join-Path $dir 'launch'
$psi.Environment['MLV_CP_MEASURED'] = Join-Path $dir 'measured'
$psi.Environment['MLV_CP_BENCH'] = '0'; $psi.Environment['MLV_CP_LOAD'] = '0'; $psi.Environment['MLV_CP_HELPER'] = '1'; $psi.Environment['MLV_CP_THREADS'] = '0'
$psi.Environment['MLV_CP_DEADLINE_TICKS'] = [string]([DateTime]::UtcNow.AddSeconds(3600).Ticks)
$psi.Environment['MLV_CP_DEADLINE_MS'] = '4000'
$sw = [Diagnostics.Stopwatch]::StartNew()
$h = [Diagnostics.Process]::Start($psi)
$ended = $h.WaitForExit(45000)
$sec = $sw.Elapsed.TotalSeconds
if (-not $ended) { $h.Kill(); [void]$h.WaitForExit(5000) }
$p = Join-Path $dir 'helper.json'
$endedBy = $(if (Test-Path -LiteralPath $p) { ([IO.File]::ReadAllText($p) | ConvertFrom-Json).endedBy } else { 'no-record' })
[ordered]@{ endedItself = $ended; seconds = [math]::Round($sec, 1); endedBy = $endedBy } | ConvertTo-Json -Compress
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

    def test_a_short_same_exe_probe_after_the_launch_is_not_the_measured_apps_evidence(self) -> None:
        """sol r4 A2: the probe is bound first (it starts after the launch instant), but its samples and load seconds must not be the app's."""
        m = re.search(r"(?m)^\$CorePartitionHelperEncoded = '([A-Za-z0-9+/=]+)'$", self.job("a"))
        self.assertIsNotNone(m)
        work = self.tmp / "probe-work"
        work.mkdir()
        driver = self.tmp / "probe-driver.ps1"
        driver.write_text(PROBE_DRIVER, encoding="ascii")
        proc = run_pwsh(["-File", str(driver)], env_extra={"CP_DIR": str(work), "CP_ENCODED": m.group(1)})
        self.assertEqual(proc.returncode, 0, proc.stdout[-2000:] + proc.stderr[-2000:])
        got = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(got["instances"], [got["probePid"], got["measuredPid"]], f"the driver did not reproduce the probe-then-app shape: {got}")
        self.assertEqual(got["app"], got["measuredPid"], got)
        self.assertEqual(got["sessions"], 2, got)
        life = got["measuredLifeSec"]
        self.assertGreater(got["loadSec"], 0, got)
        self.assertGreater(got["loadBytes"], 0, got)
        self.assertLessEqual(got["loadSec"], life + 1.5, f"the probe's load seconds were counted as the measured app's: {got}")
        self.assertLessEqual(got["appSamples"], life + 1.5, f"the probe's samples were counted as app samples: {got}")

    def test_an_unrelated_same_path_process_started_after_the_launch_never_supplies_the_measured_apps_evidence(self) -> None:
        """sol r4 CP-02 (= adversary A1): the helper binds the pid the job hands over, records the other instance beside it, and the scalars refuse."""
        m = re.search(r"(?m)^\$CorePartitionHelperEncoded = '([A-Za-z0-9+/=]+)'$", self.job("a"))
        self.assertIsNotNone(m)
        work = self.tmp / "same-path-work"
        work.mkdir()
        driver = self.tmp / "same-path-driver.ps1"
        driver.write_text(LIFT + SAME_PATH_DRIVER, encoding="ascii")
        proc = run_pwsh(["-File", str(driver)], env_extra={"CP_DIR": str(work), "CP_ENCODED": m.group(1), "CP_GEN": str(GENERATOR)})
        self.assertEqual(proc.returncode, 0, proc.stdout[-2000:] + proc.stderr[-2000:])
        got = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(got["app"], got["measuredPid"], f"the helper bound another same-path instance as the measured app: {got}")
        self.assertEqual(got["recordedMeasuredPid"], got["measuredPid"], got)
        self.assertEqual(sorted(got["instances"]), sorted([got["otherPid"], got["measuredPid"]]), f"the concurrent instance is not on the record: {got}")
        self.assertEqual(got["others"], [got["prePid"]], f"adversary r5 A1: a same-path process alive from before the launch is not on the record: {got}")
        self.assertEqual(got["appSamples"], 0, f"a sample of another instance was tagged as the app's: {got}")
        self.assertFalse(got["bench"], f"a concurrent same-path instance's affinity was accepted: {got}")
        self.assertIsNone(got["observed"], got)
        self.assertIn("appAffinityObserved=same-path-instance-overlap", got["why"])

    def test_the_job_hands_the_measured_pid_to_the_helper_before_the_stop_file(self) -> None:
        """sol r4 CP-02 / CP-05 wiring: the runner's pid reaches the helper first, and the scalar line carries the pid and the publish reason."""
        for key in ("a", "b1", "b2", "cuda-b2"):
            with self.subTest(job=key):
                text = lf(self.job(key))
                self.assertIn("$cpPsi.Environment['MLV_CP_MEASURED'] = $corePartitionMeasuredFile\n", text)
                self.assertIn("([IO.File]::ReadAllText($resultPath) | ConvertFrom-Json).process", text)
                measured = text.index("Publish-AttrCudaText -Path $corePartitionMeasuredFile")
                self.assertLess(measured, text.index("Publish-AttrCudaText -Path $corePartitionStopFile"), "the measured pid must reach the helper before it stops")
                # r8 A1: and the job's own wall/monotonic clock gap
                self.assertIn("$corePartitionHelperWhy $corePartitionHelperNotReady $corePartitionMeasuredPid $corePartitionPublishWhy $corePartitionMeasuredStartTicks $corePartitionMeasuredEndTicks $corePartitionJobClockSkewMs\n", text)
                self.assertIn("ConvertTo-CorePartitionUtcTicks $cpRunnerProcess.startedAtUtc", text)
                for fn in ("function Test-CorePartitionMeasuredApp", "function Get-CorePartitionFeedHandleInfo", "function ConvertTo-CorePartitionUtcTicks", "function Get-CorePartitionClockSkewMs",
                           "function Test-CorePartitionReadyMarker", "function Get-CorePartitionReadyToken", "function Get-CorePartitionMonoMs"):
                    self.assertIn(fn, text)

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

    def test_the_helper_vouches_for_its_record_by_exit_code_and_an_hmac_under_the_jobs_key(self) -> None:
        """sol r6 CP-06, the real helper: a clean run exits 0 with a record whose HMAC verifies under the key it was handed (and no other), and
        the key itself is never in the record; a planted record name makes its CreateNew refuse and it exits non-zero."""
        m = re.search(r"(?m)^\$CorePartitionHelperEncoded = '([A-Za-z0-9+/=]+)'$", self.job("a"))
        self.assertIsNotNone(m)
        work = self.tmp / "exit-work"
        work.mkdir()
        driver = self.tmp / "exit-driver.ps1"
        driver.write_text(LIFT + EXIT_DRIVER, encoding="ascii")
        proc = run_pwsh(["-File", str(driver)], env_extra={"CP_DIR": str(work), "CP_ENCODED": m.group(1), "CP_GEN": str(GENERATOR)})
        self.assertEqual(proc.returncode, 0, proc.stdout[-2000:] + proc.stderr[-2000:])
        got = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(got["clean"]["exit"], 0, got)
        self.assertTrue(got["clean"]["macFile"], got)
        self.assertEqual(got["clean"]["mac"], [True, False], got)
        self.assertFalse(got["clean"]["nonceInRecord"], "the job's key must never be written into the record")
        self.assertNotEqual(got["planted"]["exit"], 0, f"a helper whose record write was refused exited 0: {got}")
        self.assertFalse(got["planted"]["macFile"], got)

    def test_the_real_helpers_ready_marker_is_bound_to_the_jobs_key_and_its_pid_and_its_record_carries_its_clock_gap(self) -> None:
        """r8 A4: the marker verifies under the key the helper was handed and its own pid, and not under another key or pid. r8 A1: the record
        carries clockSkewMaxMs, its largest gap between wall-clock and monotonic elapsed time (a steady clock here: well under the 1000 ms tolerance)."""
        m = re.search(r"(?m)^\$CorePartitionHelperEncoded = '([A-Za-z0-9+/=]+)'$", self.job("a"))
        self.assertIsNotNone(m)
        work = self.tmp / "ready-token-work"
        work.mkdir()
        driver = self.tmp / "ready-token-driver.ps1"
        driver.write_text(LIFT + EXIT_DRIVER, encoding="ascii")
        proc = run_pwsh(["-File", str(driver)], env_extra={"CP_DIR": str(work), "CP_ENCODED": m.group(1), "CP_GEN": str(GENERATOR)})
        self.assertEqual(proc.returncode, 0, proc.stdout[-2000:] + proc.stderr[-2000:])
        clean = json.loads(proc.stdout.strip().splitlines()[-1])["clean"]
        self.assertEqual(clean["exit"], 0, clean)
        self.assertEqual(clean["ready"], [None, "ready-marker-foreign", "ready-marker-foreign"], clean)
        self.assertIsInstance(clean["skew"], (int, float), f"the helper's record carries no clock gap: {clean}")
        self.assertGreaterEqual(clean["skew"], 0)
        self.assertLess(clean["skew"], 1000, clean)

    def test_a_helper_that_never_started_or_whose_ready_marker_is_not_its_own_publishes_no_package_feed_value(self) -> None:
        """r8 A3 (adversary r7 6fac125a A3): Process.Start throws, so there is no helper: before r8 the not-ready guard needed a helper object and
        a fresh feed row was published beside helper-not-started. r8 A4 (6fac125a A4): a directory at the ready path passed Test-Path; a file with
        another key's HMAC, the pre-r8 'ready' text or no text is not this helper's marker either. Each is not-ready with a typed reason, and
        neither core-partition.json nor the summary carries a package-feed value. The right marker (control) still publishes the window."""
        self.job("a")
        work = self.tmp / "marker-work"
        work.mkdir()
        driver = self.tmp / "marker-driver.ps1"
        driver.write_text(LIFT + MARKER_DRIVER, encoding="ascii")
        modes = ["start-throws", "directory", "foreign-key", "pre-r8-text", "empty", "good", "good-rebound", "good-rebound-republish-throws"]
        proc = run_pwsh(["-File", str(driver)], env_extra={"CP_JOB": str(self.out["a"]), "CP_OPEN": OPEN, "CP_CLOSE": CLOSE, "CP_FAKE_HELPER": MARKER_HELPER,
                                                           "CP_WORK": str(work), "CP_MODES": ",".join(modes), "CP_GEN": str(GENERATOR)})
        self.assertEqual(proc.returncode, 0, proc.stdout[-3000:] + proc.stderr[-3000:])
        got = json.loads(proc.stdout.strip().splitlines()[-1])
        expect = {
            "start-throws": ("helper-start-failed", None),
            "directory": ("helper-not-ready-at-launch+ready-marker-not-a-file", "ready-marker-not-a-file"),
            "foreign-key": ("helper-not-ready-at-launch+ready-marker-foreign", "ready-marker-foreign"),
            "pre-r8-text": ("helper-not-ready-at-launch+ready-marker-foreign", "ready-marker-foreign"),
            "empty": ("helper-not-ready-at-launch+ready-marker-empty", "ready-marker-empty"),
        }
        for mode, (why, ready_why) in expect.items():
            with self.subTest(mode=mode):
                c = got[mode]
                self.assertNotIn("threw", c, c)
                self.assertFalse(c["ready"], c)
                self.assertTrue(c["notReady"], f"a helper that was not ready at the measured launch kept the feed open: {c}")
                self.assertTrue(c["publishedNotReady"], c)
                self.assertTrue(c["helperWhy"].startswith(why), c)
                if ready_why:
                    self.assertEqual(c["readyWhy"], ready_why, c)
                self.assertEqual(c["published"][:3], [None, None, None], f"core-partition.json published package-feed values: {c}")
                self.assertTrue(c["published"][3].startswith("helper-not-ready-at-launch"), c)
                self.assertEqual(c["pkg"], [None] * 4, f"the summary published package-feed values: {c}")
                self.assertIn("pkgWattsMean=helper-not-ready-at-launch", c["why"])
                self.assertIs(c["topo"], False, c)
        self.assertFalse(got["start-throws"]["started"], got["start-throws"])
        self.assertTrue(any(e.startswith("helper-start: ") for e in got["start-throws"]["errors"]), got["start-throws"])
        good = got["good"]
        self.assertNotIn("threw", good, good)
        self.assertEqual([good["ready"], good["notReady"], good["readyWhy"]], [True, False, ""], good)
        self.assertEqual(good["published"][:3], [1, 45.0, 70.0], f"the right marker no longer opens the feed: {good}")
        # r13c (Dot 6456731376 d): the good control publishes core-partition.json exactly once
        self.assertEqual([good["saveCalls"], len(good["saves"])], [1, 1], good)
        self.assertEqual(good["saves"][0][:4], [1, 45.0, 70.0, ""], good)
        # r13b (adversary r13b e02b23e3 A1): the same good run whose metrics read a different run-log text than the start verdict: on e02b23e3
        # core-partition.json kept rows 1 / 45 W / 70 C with startWhy null; now it is published again refused, as the summary is
        rebound = got["good-rebound"]
        self.assertNotIn("threw", rebound, rebound)
        self.assertEqual(rebound["published"], [None, None, None, "run-log-snapshot-mismatch"], rebound)
        self.assertEqual(rebound["startWhy"], "run-log-snapshot-mismatch", rebound)
        self.assertEqual(rebound["pkg"], [None] * 4, rebound)
        # r13c (Dot 6456731376 a/b): each publication captured by value. Two publications: the first is the good window, the second (the
        # republish) carries run-log-snapshot-mismatch on every window field and as startWhy. With the republish deleted there is one.
        self.assertEqual([rebound["saveCalls"], len(rebound["saves"])], [2, 2], f"a snapshot mismatch did not publish core-partition.json again: {rebound}")
        self.assertEqual(rebound["saves"][0][:4], [1, 45.0, 70.0, ""], rebound)
        self.assertEqual(rebound["saves"][1][:4], [None, None, None, "run-log-snapshot-mismatch"], rebound)
        self.assertEqual(rebound["saves"][1][4], ["run-log-snapshot-mismatch"] * 6, rebound)
        self.assertEqual(rebound["publishWhy"], "", rebound)
        # r13c (Dot 6456731376 c): the republish itself fails (the second Save-Json throws): the only publication on disk is the good window, so
        # the summary must not stand on it: core-partition-json-unpublished, the republish error recorded, and no package-feed value
        failed = got["good-rebound-republish-throws"]
        self.assertNotIn("threw", failed, failed)
        self.assertEqual([failed["saveCalls"], len(failed["saves"])], [2, 1], failed)
        self.assertEqual(failed["publishWhy"], "core-partition-json-unpublished", failed)
        self.assertIn("republish: save-json-refused-by-test", failed["errors"], failed)
        self.assertEqual(failed["pkg"], [None] * 4, failed)
        self.assertIn("pkgWattsMean=run-log-snapshot-mismatch", failed["why"], failed)
        self.assertIn("core-partition-json-unpublished", failed["why"], failed)

    def test_the_jobs_key_reaches_only_the_helper(self) -> None:
        """sol r6 CP-06: the per-job key is drawn at run time, handed to the helper by environment (never the command line), never traced or
        published, and the job checks the record with it, the helper's exit code and the launch pid."""
        for key in ("a", "b1", "b2", "cuda-b2"):
            with self.subTest(job=key):
                text = lf(self.job(key))
                uses = [ln.strip() for ln in text.splitlines() if "$corePartitionHelperNonce" in ln]
                self.assertEqual(len(uses), 4, uses)
                self.assertTrue(uses[0].startswith("$corePartitionHelperNonce = "), uses)
                self.assertIn("$cpKeyBytes", uses[0])
                self.assertIn("RandomNumberGenerator]::Create()", text)
                self.assertEqual(uses[1], "$cpPsi.Environment['MLV_CP_NONCE'] = $corePartitionHelperNonce")
                # r8 A4: the ready marker the job accepts is derived from the key (an HMAC), never the key itself
                self.assertEqual(uses[2], "$cpReadyToken = Get-CorePartitionReadyToken $corePartitionHelperNonce $corePartitionHelper.Id")
                self.assertIn("Test-CorePartitionHelperRecord ", uses[3])
                self.assertIn("$corePartitionHelperExit", uses[3])
                self.assertFalse([ln for ln in uses if "Write-JobTrace" in ln or "Save-Json" in ln], uses)
                self.assertIn("$corePartitionHelperExit = $corePartitionHelper.ExitCode", text)

    def test_the_jobs_and_the_encoded_helper_parse(self) -> None:
        files = [self.out[k] for k in ("a", "b1", "b2", "cuda-b2") if self.job(k)]
        helpers = []
        for f in files:
            m = re.search(r"(?m)^\$CorePartitionHelperEncoded = '([A-Za-z0-9+/=]+)'$", f.read_text(encoding="utf-8"))
            self.assertIsNotNone(m, f.name)
            src = base64.b64decode(m.group(1)).decode("utf-16-le")
            for name in ("function Get-CorePartitionTopology", "function Test-CorePartitionMaskClass", "function Format-CorePartitionMask", "MlvCorePartitionLoad",
                         "ReadCategory()", "'Parking Status'", "MLV_CP_DEADLINE_MS", "$cw.ElapsedMilliseconds -ge $deadline"):
                self.assertIn(name, src, f.name)
            self.assertLess(len(m.group(1)), 32000)
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

    def test_a_stop_before_the_helper_observed_the_measured_apps_exit_refuses_its_coverage(self) -> None:
        """r9 Dot 1 (= adversary r8 aeacec83 A1), the real helper: a stop file written while the measured app still runs ends the helper's watch
        before the runner's exit, so its genuinely authenticated record does not cover the window: refused with coverage-stop-before-runner-exit
        and bench acceptance is false. Control: the stop written the instant the app exits (as the job does) still finds the exit observed."""
        m = re.search(r"(?m)^\$CorePartitionHelperEncoded = '([A-Za-z0-9+/=]+)'$", self.job("a"))
        self.assertIsNotNone(m)
        work = self.tmp / "coverage-work"
        work.mkdir()
        driver = self.tmp / "coverage-driver.ps1"
        driver.write_text(LIFT + COVERAGE_DRIVER, encoding="ascii")
        proc = run_pwsh(["-File", str(driver)], env_extra={"CP_DIR": str(work), "CP_ENCODED": m.group(1), "CP_GEN": str(GENERATOR)})
        self.assertEqual(proc.returncode, 0, proc.stdout[-2000:] + proc.stderr[-2000:])
        got = json.loads(proc.stdout.strip().splitlines()[-1])
        early = got["stop-while-alive"]
        self.assertEqual(early["app"], early["measuredPid"], early)
        self.assertEqual(early["endedBy"], "stop-file", early)
        self.assertIsNone(early["gone"], f"the driver did not stop the helper while the app ran: {early}")
        self.assertFalse(early["bench"], f"a record whose watch ended before the runner's exit was accepted: {early}")
        self.assertIsNone(early["observed"], early)
        self.assertIn("appAffinityObserved=coverage-stop-before-runner-exit", early["why"])
        control = got["stop-right-after-exit"]
        self.assertEqual(control["app"], control["measuredPid"], control)
        self.assertIsNotNone(control["gone"], f"the helper ended on the stop file without polling the exited app first: {control}")
        self.assertIsNotNone(control["goneMs"], control)
        self.assertLessEqual(control["aliveMs"], control["goneMs"], control)
        # r10 B1: aliveMs is now stamped before the liveness query, so the alive-to-gone gap is the real one, a poll cycle (about 250 ms here) plus
        # whatever a loaded host adds (one counter read can take most of a second); over the 1000 ms bound the record is refused typed, never accepted
        if control["goneMs"] - control["aliveMs"] > 1000:
            self.assertFalse(control["bench"], control)
            self.assertIn("appAffinityObserved=coverage-endpoint-gap", control["why"])
        else:
            self.assertTrue(control["bench"], f"a full-window record was refused: {control}")

    def test_the_helpers_self_deadline_is_monotonic_not_the_wall_clock(self) -> None:
        """r9 Dot 3 (= adversary r8 aeacec83 A3): with its parent lost and a wall-clock deadline an hour away (what a backward step makes of it), the
        real helper still ends itself at its 4 s monotonic budget with endedBy=deadline."""
        m = re.search(r"(?m)^\$CorePartitionHelperEncoded = '([A-Za-z0-9+/=]+)'$", self.job("a"))
        self.assertIsNotNone(m)
        work = self.tmp / "deadline-work"
        work.mkdir()
        driver = self.tmp / "deadline-driver.ps1"
        driver.write_text(DEADLINE_DRIVER, encoding="ascii")
        proc = run_pwsh(["-File", str(driver)], env_extra={"CP_DIR": str(work), "CP_ENCODED": m.group(1)})
        self.assertEqual(proc.returncode, 0, proc.stdout[-2000:] + proc.stderr[-2000:])
        got = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertTrue(got["endedItself"], f"the helper ran on past its monotonic deadline: {got}")
        self.assertEqual(got["endedBy"], "deadline", got)
        self.assertLess(got["seconds"], 30, got)
        for key in ("a", "b1", "b2", "cuda-b2"):
            with self.subTest(job=key):
                text = lf(self.job(key))
                self.assertIn("$cpPsi.Environment['MLV_CP_DEADLINE_MS'] = [string][int64](30000 + [double]$SmokeProcessTimeoutMs + 60000)\n", text)
                self.assertNotIn("MLV_CP_DEADLINE_TICKS", text)
                # r9 Dot 4 + adversary r9 (5592421d) A2: core-partition.json names the residual endpoint error bound, the measured app's observed
                # alive-to-gone gap (the measured-app verdict's endpointMs), never a constant
                self.assertIn("endpointErrorBoundMs = $cpAppVerdict.endpointMs }", text)
                self.assertIn("measuredApp = $cpAppVerdict.why\n", text)
                # r10 B2: the feed window ends at the smoke run's monotonic end, with the declared 2000 ms slack for the end read; r11 B1: that end is
                # stamped before the affinity restore and cross-checked against the app's last observed-alive poll, with the declared 10000 ms slack
                self.assertIn("$corePartitionSmokeEndMonoMs = Get-CorePartitionMonoMs\n", text)
                # r13c (rule (a)): and its window opens at the verified playback start ($cpStartAnchor)
                self.assertIn("$cpFeedWindow = Get-CorePartitionFeedSummary $corePartitionFeedStart $corePartitionFeedEnd 120 $corePartitionFeedPreHelper $corePartitionSmokeEndMonoMs 2000 $corePartitionAppAliveMonoMs 10000 $cpStartAnchor\n", text)
                self.assertLess(text.index("$corePartitionSmokeEndMonoMs = Get-CorePartitionMonoMs\n"), text.index("$corePartitionJobProc.ProcessorAffinity = [IntPtr][Convert]::ToInt64("))
                self.assertLess(text.index("$corePartitionSmokeEndMonoMs = Get-CorePartitionMonoMs\n"), text.index("$corePartitionFeedEnd = Read-CorePartitionFeedSince"))
        # adversary r9 (5592421d) A1: the final pass enumerates same-path processes before it looks for the stop file
        src = base64.b64decode(re.search(r"(?m)^\$CorePartitionHelperEncoded = '([A-Za-z0-9+/=]+)'$", self.job("a")).group(1)).decode("utf-16-le")
        loop = src[src.index("while ($true) {"):]
        self.assertLess(loop.index("$q.r.goneMs = "), loop.index("Test-Path -LiteralPath $stop"), "the stop file is looked for before the pass polls the tracked processes")
        self.assertLess(loop.index("Get-Process -Name $appName"), loop.index("Test-Path -LiteralPath $stop"), "the stop file is looked for before the pass enumerates same-path processes")


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
# sol r4 CP-02: the measured app's pid (Get-CorePartitionScalars -MeasuredPid). A pre-r5 generator has no such parameter; the splat is then empty so
# a RED run reaches the assertions and reports the defect instead of a binding error.
$hasMp = (Get-Command Get-CorePartitionScalars).Parameters.ContainsKey('MeasuredPid')
$mp = @{}; if ($hasMp) { $mp.MeasuredPid = [int64]5000; $mp.MeasuredStartTicks = [int64]639286128030000000; $mp.MeasuredEndTicks = [int64]639286128230000000 }
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
# sol r6 CP-09: every mark carries its read's monotonic time (monoMs), wall time (readUtcTicks) and the feed's last-write stamp. The pre-helper
# mark ($base) is the start mark's baseline (Get-CorePartitionFeedSummary -Base), the default of the wrapper below; a pre-r7 generator ignores the 4th argument.
$T0 = [int64]639286128000000000
$hasBase = (Get-Command Get-CorePartitionFeedSummary).Parameters.ContainsKey('Base')
$base = @{ exists = $true; length = 400; ageSec = 1.0; monoMs = [int64]5000000; readUtcTicks = $T0 - 50000000; lastWriteUtcTicks = $T0 - 60000000 }
$cpFeedSummaryImpl = ${function:Get-CorePartitionFeedSummary}
# r10 B2: a reader's mark carries the time its read completed (doneMs) and the job passes the window's monotonic end. A hand-built mark below
# without doneMs is a read that completed at its stamp (a copy gets doneMs = monoMs), and its end read completed at the window's end; the r10
# cases (CorePartitionR10Tests) exercise a delayed read and a missing doneMs directly.
function Get-CorePartitionFeedSummary($Start, $End, [double]$StaleSec, $Base = $base) {
    $s0 = $Start; $e0 = $End
    if ($s0 -is [hashtable] -and $s0.ContainsKey('monoMs') -and -not $s0.ContainsKey('doneMs')) { $s0 = $s0.Clone(); $s0.doneMs = $s0.monoMs }
    if ($e0 -is [hashtable] -and $e0.ContainsKey('monoMs') -and -not $e0.ContainsKey('doneMs')) { $e0 = $e0.Clone(); $e0.doneMs = $e0.monoMs }
    # r11 B1: the app's last observed-alive poll is at the window end (a pre-r11 generator ignores the extra arguments)
    $we = $(if ($null -ne $e0 -and $e0.doneMs -is [ValueType]) { $e0.doneMs } else { 0 })
    & $cpFeedSummaryImpl $s0 $e0 $StaleSec $Base $we 2000 $we
}
$start = @{ exists = $true; length = 500; ageSec = 1.0; before = "26.10.2026,01:00:00,x,61.0,20.5,No,No`n26.10.2026,01:00:01,x,62.5,21.0,No,No`n"; window = ''; windowStartsMidRow = $false; monoMs = [int64]5005000; readUtcTicks = $T0; lastWriteUtcTicks = $T0 - 10000000 }
# r9 (Dot 2): a start mark's text is the bytes its read returned, from beforeOffset to length (Read-CorePartitionFeedMark); each hand-built start mark
# below says where its text starts
function Set-CpOffset($Mark) { $Mark.beforeOffset = [int64]$Mark.length - [Text.Encoding]::UTF8.GetByteCount([string]$Mark.before) }
Set-CpOffset $start
$end = @{ exists = $true; length = 900; ageSec = 0.5; before = ''; window = "5,00,x,99.0,99.0,Yes,Yes`n26.10.2026,01:00:02,x,70,45,No,No`r`n26.10.2026,01:00:03,x,80,55,Yes,No`nDate,Time,a,b,c,d,e`n26.10.2026,01:00:04,x,75,50,No,No`n"; windowStartsMidRow = $true; monoMs = [int64]5035000; readUtcTicks = $T0 + 300000000; lastWriteUtcTicks = $T0 + 295000000 }
$feed = Get-CorePartitionFeedSummary $start $end 120
$r.feed = @($feed.rows, $feed.pkgWattsMean, $feed.pkgWattsMax, $feed.pkgTempCMean, $feed.pkgTempCMax, $feed.pkgThrottledRows, $feed.pkgTempCAtStart, $feed.reasons.Count)
$whole = $end.Clone(); $whole.windowStartsMidRow = $false
$r.feedWhole = (Get-CorePartitionFeedSummary $start $whole 120).rows
$rot = $end.Clone(); $rot.length = 100
$r.rotated = @((Get-CorePartitionFeedSummary $start $rot 120).reasons['pkgWattsMean'], (Get-CorePartitionFeedSummary $start $rot 120).pkgTempCAtStart)
# a 200 s window (monotonic and wall agree) whose feed last wrote 150 s before the end read
$stale = $end.Clone(); $stale.ageSec = 500; $stale.monoMs = $start.monoMs + 200000; $stale.readUtcTicks = $T0 + 2000000000; $stale.lastWriteUtcTicks = $T0 + 500000000
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
$one = '"instances":[{"pid":5000,"startUtcTicks":639286128040000000,"firstSeenUtcTicks":639286128041000000,"firstMs":4100,"lastSeenUtcTicks":639286128200000000,"goneUtcTicks":639286128202000000,"aliveMs":16000,"goneMs":16200}],"clockSkewMaxMs":0'
$helper = ('{"topologyCheck":{"ok":true,"reason":null,"pMask":4095,"eMask":1044480},"helperAffinityObserved":1044480,"app":{"pid":5000,"startUtcTicks":639286128040000000,"affinityFirst":4095,"affinityLast":4095},"load":{"seconds":10.0,"bytes":20000000000,"joined":true},"samples":[{"app":true,"m":1000,"inst":["0,0","0,12"],"freq":[2500,2500],"perf":[160,80],"park":[0,0]}],' + $one + '}') | ConvertFrom-Json
$w = @{ value = 16; why = $null }
$b2 = Get-CorePartitionScalars $helper $feed 4095 1044480 8 $w $null @mp
$r.b2 = @($b2.corePartitionTopologyOk, $b2.appAffinityObserved, $b2.benchAffinityRequested, $b2.benchAffinityApplied, $b2.partitionLoadMask, $b2.partitionLoadApplied, $b2.partitionLoadGBps,
    $b2.pcoreMHzMean, $b2.ecoreMHzMean, $b2.pcoreParkedSampleFraction, $b2.playbackWorkerThreads, $b2.pkgWattsMean, $b2.corePartitionNullReasons, @($b2.Keys).Count)
$helperA = ('{"topologyCheck":{"ok":true,"reason":null,"pMask":4095,"eMask":1044480},"helperAffinityObserved":1044480,"app":{"pid":5000,"startUtcTicks":639286128040000000,"affinityFirst":1048575,"affinityLast":1048575},"load":{"seconds":0.0,"bytes":0,"joined":true},"samples":[],' + $one + '}') | ConvertFrom-Json
$a = Get-CorePartitionScalars $helperA $feed 0 0 0 $w $null @mp
$r.a = @($a.appAffinityObserved, $a.benchAffinityApplied, $a.benchAffinityRequested, $a.partitionLoadApplied, $a.partitionLoadGBps, $a.pcoreMHzMean, $a.corePartitionNullReasons)
$helperMoved = ('{"topologyCheck":{"ok":true,"reason":null,"pMask":4095,"eMask":1044480},"helperAffinityObserved":1044480,"app":{"pid":5000,"startUtcTicks":639286128040000000,"affinityFirst":4095,"affinityLast":1048575},"load":{"seconds":0.5,"bytes":10,"joined":true},"samples":[],' + $one + '}') | ConvertFrom-Json
$m = Get-CorePartitionScalars $helperMoved $feed 4095 1044480 8 $w $null @mp
$r.moved = @($m.appAffinityObserved, $m.benchAffinityApplied, $m.partitionLoadApplied)
$wrongHelper = ('{"topologyCheck":{"ok":true,"reason":null,"pMask":4095,"eMask":1044480},"helperAffinityObserved":1048575,"app":{"pid":5000,"startUtcTicks":639286128040000000,"affinityFirst":4095,"affinityLast":4095},"load":{"seconds":10,"bytes":10,"joined":true},"samples":[],' + $one + '}') | ConvertFrom-Json
$r.wrongHelper = (Get-CorePartitionScalars $wrongHelper $feed 4095 1044480 8 $w $null @mp).partitionLoadApplied
$none = Get-CorePartitionScalars $null $mf 4095 0 0 @{ value = $null; why = 'run-log-missing' } 'helper-not-started'
$r.none = @($none.corePartitionTopologyOk, $none.appAffinityObserved, $none.benchAffinityApplied, $none.corePartitionNullReasons)
$badTopo = '{"topologyCheck":{"ok":false,"reason":"topology-uniform (1 efficiency class(es))","pMask":null,"eMask":null},"app":{"affinityFirst":4095,"affinityLast":4095},"samples":[]}' | ConvertFrom-Json
$r.badTopo = @((Get-CorePartitionScalars $badTopo $feed 4095 0 0 $w $null).corePartitionTopologyOk, (Get-CorePartitionScalars $badTopo $feed 4095 0 0 $w $null).corePartitionNullReasons)
# adversary r2 A3 / A4: a replaced or over-cap feed is no evidence (real files, the I/O readers)
$fa = Join-Path $env:CP_TMP 'feed-a.csv'
[IO.File]::WriteAllText($fa, "26.10.2026,00:59:59,x,30,10,No,No`n")
$pa = Read-CorePartitionFeedMark $fa
[IO.File]::AppendAllText($fa, "26.10.2026,01:00:00,x,30,10,No,No`n")
$ma = Read-CorePartitionFeedMark $fa
[IO.File]::AppendAllText($fa, "26.10.2026,01:00:01,x,90,99,Yes,No`n")
$ea = Read-CorePartitionFeedSince $fa ([int64]$ma.length) ([string]$ma.before)
$fbase = @{}; if ($hasBase) { $fbase.Base = $pa }
$sa = Get-CorePartitionFeedSummary $ma $ea 120 @fbase
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
$helperGood = ('{"topologyCheck":{"ok":true,"reason":null,"pMask":4095,"eMask":1044480},"helperAffinityObserved":1044480,"app":{"pid":5000,"startUtcTicks":639286128040000000,"affinityFirst":4095,"affinityLast":4095},"load":{"seconds":15,"bytes":30000000000,"joined":true},"samples":[],' + $one + '}') | ConvertFrom-Json
$nr = Get-CorePartitionScalars $helperGood $feed 4095 1044480 8 $w 'helper-not-ready-at-launch' $true @mp
$okc = Get-CorePartitionScalars $helperGood $feed 4095 1044480 8 $w $null $false @mp
$r.notReady = @($nr.corePartitionTopologyOk, $nr.benchAffinityApplied, $nr.partitionLoadApplied, $nr.corePartitionNullReasons, $okc.corePartitionTopologyOk, $okc.benchAffinityApplied, $okc.partitionLoadApplied)
# sol r3 CP-01: the guard is the dedicated flag, so a reason text that no longer says "not ready" cannot re-open the criteria
$nrText = Get-CorePartitionScalars $helperGood $feed 4095 1044480 8 $w 'helper-killed-after-stop-timeout' $true @mp
$r.notReadyFlag = @($nrText.corePartitionTopologyOk, $nrText.benchAffinityApplied, $nrText.partitionLoadApplied)
# opus H2 (CORE-PARTITION-FEED-NOT-READY-REASON-1): with a not-ready helper every package-feed field is null and names the not-ready reason
$r.notReadyFeed = @($nr.pkgWattsMean, $nr.pkgWattsMax, $nr.pkgTempCMean, $nr.pkgTempCMax, $nr.pkgThrottledRows, $nr.pkgTempCAtStart, $okc.pkgWattsMean, $okc.pkgTempCAtStart)
# sol r4 A3: a row the writer has not finished (no newline yet) is not a row, at the window's end or at the start mark
$part = $end.Clone(); $part.window = "26.10.2026,01:00:02,x,70,45,No,Y"; $part.windowStartsMidRow = $false
$partTwo = $end.Clone(); $partTwo.window = "26.10.2026,01:00:01,x,60,40,No,No`n26.10.2026,01:00:02,x,70,45,No,Y"; $partTwo.windowStartsMidRow = $false
$startPart = $start.Clone(); $startPart.before = "26.10.2026,01:00:00,x,61.0,20.5,No,No`n26.10.2026,01:00:01,x,6,20,No,N"; Set-CpOffset $startPart
$r.partialRow = @((Get-CorePartitionFeedSummary $start $part 120).reasons['rows'], (Get-CorePartitionFeedSummary $start $partTwo 120).rows, (Get-CorePartitionFeedSummary $start $partTwo 120).pkgWattsMax,
    (Get-CorePartitionFeedSummary $startPart $whole 120).pkgTempCAtStart)
# sol r4 A4: NaN, Infinity and an overflowing exponent are not measured values
$r.nonFinite = @(foreach ($row in @("26.10.2026,01:00:02,x,NaN,45,No,No`n", "26.10.2026,01:00:02,x,70,1e309,No,No`n", "26.10.2026,01:00:02,x,Infinity,45,No,No`n", "26.10.2026,01:00:02,x,70,-Infinity,No,No`n")) {
    $nf = $end.Clone(); $nf.window = $row; $nf.windowStartsMidRow = $false; (Get-CorePartitionFeedSummary $start $nf 120).reasons['pkgWattsMean'] })
$nfs = $start.Clone(); $nfs.before = "26.10.2026,01:00:00,x,NaN,20.5,No,No`n"; Set-CpOffset $nfs
$r.nonFiniteStart = (Get-CorePartitionFeedSummary $nfs $whole 120).reasons['pkgTempCAtStart']
# sol r4 CP-02 (literal): launch mark 2026-10-26T12:00:00Z; unrelated same-path pid 4242 starts at +50 ms with affinity 4095 and lives throughout;
# the measured pid 5000 starts later. One record as the r4 helper wrote it (4242 bound), one as an r5 helper binds it (5000, 4242 still alive).
$L = 639286128000000000
function New-CpInst([int]$ProcId, [double]$Start, [double]$First, [double]$Last, $Gone) {
    $g = if ($null -eq $Gone) { 'null' } else { [string]($L + [int64]($Gone * 1e7)) }
    '{"pid":' + $ProcId + ',"startUtcTicks":' + ($L + [int64]($Start * 1e7)) + ',"firstSeenUtcTicks":' + ($L + [int64]($First * 1e7)) + ',"firstMs":' + [int64]($First * 1000) + ',"lastSeenUtcTicks":' + ($L + [int64]($Last * 1e7)) + ',"goneUtcTicks":' + $g + ',"aliveMs":' + [int64]($Last * 1000) + ',"goneMs":' + $(if ($null -eq $Gone) { 'null' } else { [int64]($Gone * 1000) }) + ',"affinityFirst":4095,"affinityLast":4095,"loadSec":15.0,"loadBytes":30000000000}'
}
# seconds after the launch mark: start, first seen, last seen, seen gone ($null = still alive when the helper ended)
$i4242 = New-CpInst 4242 0.05 0.06 20 $null
$i5000 = New-CpInst 5000 3 3.1 19 19.05
$i4100 = New-CpInst 4100 0.1 0.2 2 2.05
$i5000early = New-CpInst 5000 0.1 0.2 2 2.05
$i5000a = New-CpInst 5000 3 3.1 6 6.05
$i5000b = New-CpInst 5000 10 10.1 19 19.05
# adversary r5 A1 (literal): 4242 alive 11:59:55Z..12:00:30Z, started BEFORE the launch; measured 5000 alive 12:00:03Z..12:00:23Z
$i4242pre = New-CpInst 4242 -5 0.06 30 30.05
$i5000A1 = New-CpInst 5000 3 3.1 23 23.05
# adversary r5 A2 (literal): measured 5000 start +3 s, first +3.1 s, last +10 s; other 4242 start +9.9 s, first +10.2 s, last +20 s
$i5000A2 = New-CpInst 5000 3 3.1 10 10.2
$i4242A2 = New-CpInst 4242 9.9 10.2 20 20.2
# adversary r5 A3 (literal): the runner's 5000 ran 12:00:03Z..12:00:23Z unseen; the helper saw only a replacement 5000 at 12:00:24Z..12:00:28Z
$i5000A3 = New-CpInst 5000 24 24.1 28 28.1
$hb = '"topologyCheck":{"ok":true,"reason":null,"pMask":4095,"eMask":1044480},"helperAffinityObserved":1044480,"samples":[{"app":true,"appPid":5000,"m":1000,"inst":["0,0","0,12"],"freq":[2500,2500],"perf":[160,80],"park":[0,0]}],"clockSkewMaxMs":0'
function New-CpRecord([string]$App, [string[]]$Inst, [string[]]$Others = @()) { ('{' + $hb + ',"app":' + $App + ',"load":{"seconds":15.0,"bytes":30000000000,"joined":true},"instances":[' + ($Inst -join ',') + '],"others":[' + ($Others -join ',') + ']}') | ConvertFrom-Json }
# record, measured pid, the runner's launch / exit (seconds after the launch mark)
$cases = [ordered]@{
    'r4-bound-4242' = @((New-CpRecord $i4242 @($i4242, $i5000)), 5000, 2.9, 19.1)
    'bound-5000-beside-4242' = @((New-CpRecord $i5000 @($i4242, $i5000)), 5000, 2.9, 19.1)
    'pid-unknown' = @((New-CpRecord $i5000 @($i4100, $i5000)), 0, 2.9, 19.1)
    'window-unknown' = @((New-CpRecord $i5000 @($i4100, $i5000)), 5000, $null, 19.1)
    'pid-not-observed' = @((New-CpRecord $i5000 @($i4100, $i5000)), 6000, 2.9, 19.1)
    'pid-reused' = @((New-CpRecord $i5000b @($i5000a, $i5000b)), 5000, 2.9, 19.1)
    'helper-bound-another' = @((New-CpRecord $i4100 @($i4100, $i5000)), 5000, 2.9, 19.1)
    'pre-launch-alive' = @((New-CpRecord $i5000A1 @($i5000A1) @($i4242pre)), 5000, 3, 23)
    'start-time-overlap' = @((New-CpRecord $i5000A2 @($i5000A2, $i4242A2)), 5000, 2.9, 10.5)
    'replacement-after-exit' = @((New-CpRecord $i5000A3 @($i5000A3)), 5000, 3, 23)
    # adversary r5 (40038f92) A3 (literal): only a predecessor 5000 (start +2.1 s, gone +3.9 s) is recorded; the runner launched its 5000 at +4 s
    'predecessor-gone-before-launch' = @((New-CpRecord (New-CpInst 5000 2.1 2.2 3.8 3.9) @((New-CpInst 5000 2.1 2.2 3.8 3.9))), 5000, 4, 24)
    'probe-then-app-control' = @((New-CpRecord $i5000 @($i4100, $i5000)), 5000, 2.9, 19.1)
    'pid-reused-before-window-control' = @((New-CpRecord $i5000 @($i5000early, $i5000)), 5000, 2.9, 19.1)
}
$r.measured = [ordered]@{}
foreach ($k in $cases.Keys) {
    $mpk = @{}
    if ($hasMp) { $mpk.MeasuredPid = [int64]$cases[$k][1]; $mpk.MeasuredStartTicks = $(if ($null -eq $cases[$k][2]) { [int64]0 } else { $L + [int64]($cases[$k][2] * 1e7) }); $mpk.MeasuredEndTicks = $L + [int64]($cases[$k][3] * 1e7) }
    $sc = Get-CorePartitionScalars $cases[$k][0] $feed 4095 1044480 8 $w $null $false @mpk
    $r.measured[$k] = @($sc.corePartitionTopologyOk, $sc.benchAffinityApplied, $sc.partitionLoadApplied, $sc.appAffinityObserved, $sc.partitionLoadGBps, $sc.pcoreMHzMean, [string]$sc.corePartitionNullReasons)
}
# r8 A1 (adversary r7 6fac125a A1 and the r8 brief): the wall clock stepped while the helper watched. Another same-path pid 4242 starts at real -5 s
# and is seen gone at real +5 s; the measured 5000 runs +3..+23 s. A 100 s backward step stamps that gone time L-95 s (6fac125a's -6 s step, restored at
# +6 s, stamps it L-1 s), so the wall-clock overlap test passes. The helper's record carries its largest wall/monotonic gap (clockSkewMaxMs) and the job
# passes its own (ClockSkewMs); over the 1000 ms tolerance, or unrecorded, the overlap verdict is refused. Control: 999 ms, 4242 truly gone at +2 s.
$hasClock = (Get-Command Get-CorePartitionScalars).Parameters.ContainsKey('ClockSkewMs')
$i5000r8 = New-CpInst 5000 3 3.1 23 23.05
function New-CpClockRecord($Skew, [string[]]$Others) {
    $o = New-CpRecord $i5000r8 @($i5000r8) $Others
    if ($null -eq $Skew) { $o.PSObject.Properties.Remove('clockSkewMaxMs') } else { $o.clockSkewMaxMs = $Skew }
    $o
}
$clockCases = [ordered]@{
    'step-100s-back' = @((New-CpClockRecord 100000.0 @((New-CpInst 4242 -5 0.06 4 (-95)))), 0)
    'step-6s-restored' = @((New-CpClockRecord 6000.0 @((New-CpInst 4242 -5 0.06 4 (-1)))), 0)
    'unrecorded' = @((New-CpClockRecord $null @((New-CpInst 4242 -5 0.06 4 (-95)))), 0)
    'job-step' = @((New-CpClockRecord 0 @((New-CpInst 4242 -5 0.06 4 (-95)))), 100000.0)
    'job-unverified' = @((New-CpClockRecord 0 @((New-CpInst 4242 -5 0.06 4 (-95)))), $null)
    # adversary r8 (e1ce216c) A1 (literal): an 800 ms backward step at real +3.3 s stamps 4242's gone time L+2.6 s (it lived to +3.35 s); the app
    # started at +3.0 s. Under the tolerance, but the overlap test must widen 4242's interval by twice the gap, so the overlap is still found.
    'sub-tolerance-step-overlap' = @((New-CpClockRecord 800.0 @((New-CpInst 4242 -5 0.06 2.5 2.6))), 0)
    # control: a 999 ms gap and 4242 truly gone at +0.9 s, more than twice the gap before the app's start
    'under-tolerance-control' = @((New-CpClockRecord 999.0 @((New-CpInst 4242 -5 0.06 0.8 0.9))), 0)
}
$r.clock = [ordered]@{}
foreach ($k in $clockCases.Keys) {
    $mpk = @{}
    if ($hasMp) { $mpk.MeasuredPid = [int64]5000; $mpk.MeasuredStartTicks = $L + 30000000; $mpk.MeasuredEndTicks = $L + 230000000 }
    if ($hasClock) { $mpk.ClockSkewMs = $clockCases[$k][1] }
    $sc = Get-CorePartitionScalars $clockCases[$k][0] $feed 4095 1044480 8 $w $null $false @mpk
    $r.clock[$k] = @($sc.corePartitionTopologyOk, $sc.benchAffinityApplied, $sc.partitionLoadApplied, $sc.appAffinityObserved, $sc.partitionLoadGBps, $sc.pcoreMHzMean, [string]$sc.corePartitionNullReasons)
}
# r8 A1: the job's own gap over its (wall ticks, monotonic ms) pairs: steady; a 100 s backward step; one pair; a non-numeric or missing reading
$r.clockSkew = 'absent'
if (Get-Command Get-CorePartitionClockSkewMs -ErrorAction SilentlyContinue) {
    $r.clockSkew = @((Get-CorePartitionClockSkewMs @(@($T0, 1000.0), @(($T0 + 200000000), 21000.0))),
        (Get-CorePartitionClockSkewMs @(@($T0, 1000.0), @(($T0 + 50000000), 6000.0), @(($T0 - 800000000), 21000.0))),
        (Get-CorePartitionClockSkewMs @(, @($T0, 1000.0))), (Get-CorePartitionClockSkewMs @(@($T0, 1000.0), @('oops', 2000.0))), (Get-CorePartitionClockSkewMs @(@($T0, 1000.0), @($T0, $null))))
}
# adversary r5 (40038f92) A4 (literal): the end read after a 600 s backward clock step computes ageSec = -401 for a feed really 199 s old
# (start read at real 0, last write at real +1 s, end read at real +200 s: monotonic span 200 s, wall span -400 s)
$negEnd = $end.Clone(); $negEnd.ageSec = -401; $negEnd.window = "26.10.2026,12:00:01,x,70,45,No,No`n"; $negEnd.windowStartsMidRow = $false
$negEnd.monoMs = $start.monoMs + 200000; $negEnd.readUtcTicks = $T0 - 4000000000; $negEnd.lastWriteUtcTicks = $T0 + 10000000
# a feed whose last-write stamp is 401 s in the future, the wall clock steady over a 200 s window
$fwdEnd = $negEnd.Clone(); $fwdEnd.readUtcTicks = $T0 + 2000000000; $fwdEnd.lastWriteUtcTicks = $T0 + 6010000000
$smallNeg = $negEnd.Clone(); $smallNeg.ageSec = -0.5; $smallNeg.monoMs = $start.monoMs + 30000; $smallNeg.readUtcTicks = $T0 + 300000000; $smallNeg.lastWriteUtcTicks = $T0 + 305000000
$r.negativeAge = @([string](Get-CorePartitionFeedSummary $start $negEnd 120).reasons['rows'], (Get-CorePartitionFeedSummary $start $negEnd 120).pkgWattsMean,
    [string](Get-CorePartitionFeedSummary $start $fwdEnd 120).reasons['rows'], (Get-CorePartitionFeedSummary $start $smallNeg 120).rows)
# sol r6 CP-09 (literal): the feed last updated at real +1 s, read at real +200 s after a 100 s backward UTC step (computed age 99 s), limit 120 s
$cpS = @{ exists = $true; length = 32; ageSec = 0.0; created = 123; fileId = 'same'; before = "26.10.2026,12:00:00,x,30,10,No,No`n"; window = ''; windowStartsMidRow = $false; monoMs = [int64]9000000; readUtcTicks = $T0; lastWriteUtcTicks = $T0 }
$cpE = @{ exists = $true; length = 64; ageSec = 99.0; created = 123; fileId = 'same'; before = ''; window = "26.10.2026,12:00:01,x,70,45,No,No`n"; windowStartsMidRow = $false; replaced = $false; truncated = $false; monoMs = [int64]9200000; readUtcTicks = $T0 + 1000000000; lastWriteUtcTicks = $T0 + 10000000 }
$cp9 = Get-CorePartitionFeedSummary $cpS $cpE 120
# a 30 s monotonic window is fresh whatever the wall clock did (here it stepped back 100 s: computed age -71 s)
$cpShort = $cpE.Clone(); $cpShort.monoMs = [int64]9030000; $cpShort.readUtcTicks = $T0 - 700000000; $cpShort.ageSec = -71.0
$cpSh = Get-CorePartitionFeedSummary $cpS $cpShort 120
# no monotonic baseline (marks without monoMs), a monotonic time that runs backwards, a non-numeric one
$noMono = @(foreach ($k in @('none', 'backwards', 'oops')) {
    $s0 = $cpS.Clone(); $e0 = $cpE.Clone(); $e0.ageSec = 1.0
    if ($k -eq 'none') { $s0.Remove('monoMs'); $e0.Remove('monoMs') } elseif ($k -eq 'backwards') { $e0.monoMs = [int64]8000000 } else { $e0.monoMs = 'oops' }
    try { $x = Get-CorePartitionFeedSummary $s0 $e0 120; , @($x.rows, [string]$x.reasons['rows']) } catch { , @('threw', [string]$_.Exception.Message) } })
# the start temperature: the feed unchanged since the pre-helper mark 200 s (monotonic) earlier is stale; unchanged over 5 s proves nothing
$stBase = @{ exists = $true; length = 500; monoMs = [int64]4805000; readUtcTicks = $T0 - 2000000000; lastWriteUtcTicks = $T0 - 10000000 }
$stShort = $stBase.Clone(); $stShort.monoMs = [int64]5000000
$stc = @(foreach ($b in @($stBase, $stShort)) { $sb2 = @{}; if ($hasBase) { $sb2.Base = $b }; [string](Get-CorePartitionFeedSummary $start $end 120 @sb2).reasons['pkgTempCAtStart'] })
$r.monotonic = @($cp9.rows, $cp9.pkgWattsMean, [string]$cp9.reasons['rows'], $cpSh.rows, $cpSh.pkgWattsMean, [string]$cpSh.reasons['rows'], $noMono[0], $noMono[1], $noMono[2]) + @($stc)
# adversary r7 (15b3bbf7) A1 (literal): the clock steps +100 s before the write at real +1 s and -100 s before the end read at real +200 s, so the
# endpoint wall and monotonic spans agree (200 s) and the stamp says 99 s; only the 200 s monotonic span counts
$a1S = @{ exists = $true; length = 34; created = 639284400000000000; fileId = '0000ABCD-0000000000000001'; monoMs = [int64]9000000; readUtcTicks = $T0; lastWriteUtcTicks = $T0; ageSec = 0.0; before = "26.10.2026,12:00:00,x,30,10,No,No`n"; window = ''; windowStartsMidRow = $false }
$a1E = @{ exists = $true; length = 68; created = 639284400000000000; fileId = '0000ABCD-0000000000000001'; monoMs = [int64]9200000; readUtcTicks = $T0 + 2000000000; lastWriteUtcTicks = $T0 + 1010000000; ageSec = 99.0; before = ''; window = "26.10.2026,12:01:41,x,70,45,No,No`n"; windowStartsMidRow = $false; replaced = $false; truncated = $false }
$a1 = Get-CorePartitionFeedSummary $a1S $a1E 120 $a1S
# adversary r7 (15b3bbf7) A2 (literal): the feed replaced between the pre-helper and start reads by a file whose newest row is a day old
$a2B = @{ exists = $true; length = 34; created = $T0 - 1728000000000; fileId = '0000ABCD-0000000000000001'; monoMs = [int64]8995000; readUtcTicks = $T0 - 50000000; lastWriteUtcTicks = $T0 - 864000000000 }
$a2S = @{ exists = $true; length = 34; created = $T0 - 864000000000; fileId = '0000ABCD-0000000000000002'; monoMs = [int64]9000000; readUtcTicks = $T0; lastWriteUtcTicks = $T0 - 864000000000; before = "25.10.2026,12:00:00,x,20,10,No,No`n"; window = ''; windowStartsMidRow = $false }; Set-CpOffset $a2S
$a2E = @{ exists = $true; length = 68; created = $T0 - 864000000000; fileId = '0000ABCD-0000000000000002'; monoMs = [int64]9020000; readUtcTicks = $T0 + 200000000; lastWriteUtcTicks = $T0 + 200000000; before = ''; window = "26.10.2026,12:00:20,x,70,45,No,No`n"; windowStartsMidRow = $false; replaced = $false; truncated = $false }
$a2 = Get-CorePartitionFeedSummary $a2S $a2E 120 $a2B
# the same file grown after the pre-helper read only by an unfinished row: the newest complete row ends before the pre-helper offset (183 < 190)
$pB = @{ exists = $true; length = 190; monoMs = [int64]8995000 }
$pS = @{ exists = $true; length = 200; monoMs = [int64]9000000; before = "26.10.2026,12:00:00,x,20,10,No,No`n26.10.2026,12:00"; window = ''; windowStartsMidRow = $false }; Set-CpOffset $pS
# adversary r8 (e1ce216c) A2: the row must START at or after the pre-helper offset (here it starts at 150), not merely end after it
$pBok = $pB.Clone(); $pBok.length = 150
$r.adversaryR7 = @([string]$a1.reasons['rows'], $a1.rows, $a1.pkgWattsMean, $a2.rows, $a2.pkgWattsMean, $a2.pkgTempCAtStart, [string]$a2.reasons['pkgTempCAtStart'],
    [string](Get-CorePartitionFeedSummary $pS $a2E 120 $pB).reasons['pkgTempCAtStart'], (Get-CorePartitionFeedSummary $pS $a2E 120 $pBok).pkgTempCAtStart)# adversary r7 (6fac125a) A2 (literal): the same file, a day-old data row before the pre-helper read; only a bare newline (length 35) or a header
# line (length 77) appended after it; and the control, a new data row appended after it
$dayOld = "25.10.2026,01:00:00,x,75,50,No,No`n"
$b2B = @{ exists = $true; length = 34; monoMs = [int64]1000 }
$b2Nl = @{ exists = $true; length = 35; monoMs = [int64]2000; before = $dayOld + "`n"; window = ''; windowStartsMidRow = $false }
$b2Hd = @{ exists = $true; length = 77; monoMs = [int64]2000; before = $dayOld + "Date,Time,x,Temp,Power,Throttle1,Throttle2`n"; window = ''; windowStartsMidRow = $false }
$b2Ok = @{ exists = $true; length = 68; monoMs = [int64]2000; before = $dayOld + "26.10.2026,01:00:00,x,61,40,No,No`n"; window = ''; windowStartsMidRow = $false }
foreach ($sm in @($b2Nl, $b2Hd, $b2Ok)) { Set-CpOffset $sm }
$b2E = @{ exists = $true; length = 200; monoMs = [int64]22000; before = ''; window = "26.10.2026,01:00:20,x,70,45,No,No`n"; windowStartsMidRow = $false; replaced = $false; truncated = $false }
# adversary r8 (e1ce216c) A2 (literal): the pre-helper read saw a complete-looking row without its LF (length 33); only the LF was appended (34)
$r8B = @{ exists = $true; length = 33; monoMs = [int64]1000 }
$r8S = @{ exists = $true; length = 34; monoMs = [int64]2000; before = "25.10.2026,01:00:00,x,30,10,No,No`n"; window = ''; windowStartsMidRow = $false }; Set-CpOffset $r8S
$r8x = Get-CorePartitionFeedSummary $r8S $b2E.Clone() 120 $r8B
$r.adversaryR8 = @($r8x.rows, $r8x.pkgTempCAtStart, [string]$r8x.reasons['pkgTempCAtStart'])
$r.adversaryR7b = @(foreach ($sm in @($b2Nl, $b2Hd, $b2Ok)) { $e2 = $b2E.Clone(); $x = Get-CorePartitionFeedSummary $sm $e2 120 $b2B; $x.pkgTempCAtStart; [string]$x.reasons['pkgTempCAtStart'] })# sol r6 CP-07: the one guarded number parse (absent in a pre-r7 generator)
$r.number = 'absent'
if (Get-Command ConvertFrom-CorePartitionNumber -ErrorAction SilentlyContinue) {
    $big = ('{"v":99999999999999999999}' | ConvertFrom-Json).v
    $tick = ('{"v":639286128040000000}' | ConvertFrom-Json).v
    $r.number = @(foreach ($p in @(@(4242, 'int64'), @('4242', 'int64'), @('oops', 'int64'), @($true, 'int64'), @(1.5, 'int64'), @($tick, 'int64'), @(1e19, 'int64'), @($big, 'int64'),
            @(, @(1, 2)), @('1e309', 'double'), @('NaN', 'double'), @($null, 'double'), @(2.5, 'double'), @('oops', 'double'), @(9.21e18, 'int64'))) {
        if ($p.Count -eq 1) { $p = @(@(1, 2), 'int64') }
        $n = ConvertFrom-CorePartitionNumber $p[0] $p[1]; , @($n.ok, $n.value) })
}
# sol r6 CP-07 (sites :4299 :4399 :4404 :4473 :4498 :4501 :4512 :4514): a non-numeric value reaching each consumer directly is no throw
$cpTry = { param($Block) try { & $Block } catch { 'threw: ' + [string]$_.Exception.Message } }
$r.consumers = @(
    (& $cpTry { [string](Get-CorePartitionEffectiveMHz 'oops' 100) }),
    (& $cpTry { [string](Get-CorePartitionEffectiveMHz 2500 'oops') }),
    (& $cpTry { [string](Get-CorePartitionCounterSummary @(@{ app = $true; inst = @('0,99999999999'); freq = @(2500); perf = @(100); park = @(0) }) 1 0).pcoreMHzMean }),
    (& $cpTry { [string](Get-CorePartitionCounterSummary @(@{ app = $true; inst = @('0,0'); freq = @(2500); perf = @(100); park = @('oops') }) 1 0).pcoreParkedSampleFraction }),
    (& $cpTry { [string](Get-CorePartitionCounterSummary @(@{ app = 'false'; inst = @('0,0'); freq = @(2500); perf = @(100); park = @(0) }) 1 0).appSamples }),
    (& $cpTry { [string](Test-CorePartitionMeasuredApp ((New-CpRecord $i5000 @($i5000.Replace('"pid":5000', '"pid":"oops"')))) 5000 ($L + 29000000) ($L + 191000000)).why }),
    (& $cpTry { $x = Get-CorePartitionScalars ($helper.PSObject.Copy() | ForEach-Object { $_.helperAffinityObserved = 'oops'; $_ }) $feed 4095 1044480 8 $w $null $false @mp; '{0}|{1}|{2}' -f $x.partitionLoadApplied, $x.benchAffinityApplied, $x.corePartitionNullReasons }),
    (& $cpTry { $x = Get-CorePartitionScalars ('{"topologyCheck":{"ok":"false","pMask":"oops","eMask":1044480},"app":{"pid":5000,"affinityFirst":4095,"affinityLast":4095},"samples":[]}' | ConvertFrom-Json) $feed 4095 0 0 $w $null $false @mp; '{0}|{1}' -f $x.corePartitionTopologyOk, $x.corePartitionNullReasons }))
# sol r6 CP-06: the record's HMAC under the per-job key (absent in a pre-r7 generator)
$r.mac = 'absent'
if (Get-Command Test-CorePartitionHelperMac -ErrorAction SilentlyContinue) {
    $mb2 = [Text.Encoding]::UTF8.GetBytes('{"schema":"mlv-app/core-partition-helper/v1","pid":4242}')
    $good = [BitConverter]::ToString([Security.Cryptography.HMACSHA256]::new([Text.Encoding]::UTF8.GetBytes('k-0123456789')).ComputeHash($mb2)).Replace('-', '')
    $r.mac = @((Test-CorePartitionHelperMac $mb2 $good 'k-0123456789'), (Test-CorePartitionHelperMac $mb2 $good.ToLowerInvariant() 'k-0123456789'), (Test-CorePartitionHelperMac $mb2 ($good + "`r`n") 'k-0123456789'),
        (Test-CorePartitionHelperMac $mb2 $good 'k-other'), (Test-CorePartitionHelperMac $mb2 $null 'k-0123456789'), (Test-CorePartitionHelperMac $mb2 $good ''), (Test-CorePartitionHelperMac $mb2 'zz' 'k-0123456789'))
}
# the runner's result.json times as the job reads them: a DateTime (pwsh 7 ConvertFrom-Json), an ISO string, absent, unreadable
$r.utcTicks = 'absent'
if (Get-Command ConvertTo-CorePartitionUtcTicks -ErrorAction SilentlyContinue) {
    $iso = [DateTime]::new([int64]639286128030000000, [DateTimeKind]::Utc).ToString('o')
    $r.utcTicks = @((ConvertTo-CorePartitionUtcTicks ('{"t":"' + $iso + '"}' | ConvertFrom-Json).t), (ConvertTo-CorePartitionUtcTicks $iso), (ConvertTo-CorePartitionUtcTicks $null), (ConvertTo-CorePartitionUtcTicks 'garbage'))
}
# sol r4 CP-03 (literal): fresh unchanged marks, two complete rows of finite 1e308 values; a watts-only overflow too
$s308 = $start.Clone(); $s308.before = "26.10.2026,01:00:00,x,30,10,No,No`n"; Set-CpOffset $s308
$w308 = $end.Clone(); $w308.window = "26.10.2026,01:00:01,x,1e308,1e308,No,No`n26.10.2026,01:00:02,x,1e308,1e308,No,No`n"; $w308.windowStartsMidRow = $false
$f308 = Get-CorePartitionFeedSummary $s308 $w308 120
$r.aggregate = @($f308.rows, $f308.pkgWattsMean, $f308.pkgWattsMax, $f308.pkgTempCMean, $f308.pkgTempCMax, $f308.pkgThrottledRows, [string]$f308.reasons['pkgWattsMean'], [string]$f308.reasons['pkgTempCMean'], [string]$f308.reasons['rows'], $f308.pkgTempCAtStart)
$r.aggregateJsonInfinity = ([string]($f308 | ConvertTo-Json -Compress -Depth 4)).Contains('Infinity')
$ww = $w308.Clone(); $ww.window = "26.10.2026,01:00:01,x,70,1e308,No,No`n26.10.2026,01:00:02,x,70,1e308,No,No`n"
$fw = Get-CorePartitionFeedSummary $s308 $ww 120
$r.aggregateWatts = @($fw.pkgWattsMean, $fw.pkgTempCMean, [string]$fw.reasons['pkgWattsMean'], [string]$fw.reasons['pkgTempCMax'])
$sc308 = Get-CorePartitionScalars $helper $f308 4095 1044480 8 $w $null $false @mp
$r.aggregateScalars = @($sc308.pkgWattsMean, $sc308.pkgTempCMean, [string]$sc308.corePartitionNullReasons)
# sol r4 CP-03 (class): a clock reading past the double range, and finite readings whose mean overflows
$r.mhzOverflow = @((Get-CorePartitionEffectiveMHz 1.7e308 100), (Get-CorePartitionEffectiveMHz ([double]::PositiveInfinity) 100), (Get-CorePartitionEffectiveMHz 2500 ([double]::PositiveInfinity)))
$bigS = @{ app = $true; inst = @(0..62 | ForEach-Object { "0,$_" }); freq = @(0..62 | ForEach-Object { 1.7e308 }); perf = @(0..62 | ForEach-Object { 1 }); park = @(0..62 | ForEach-Object { 0 }) }
$csBig = Get-CorePartitionCounterSummary @($bigS, $bigS) ([int64]::MaxValue) 0
$r.counterOverflow = @($csBig.appSamples, $csBig.pcoreMHzMean, ([string]($csBig | ConvertTo-Json -Compress)).Contains('Infinity'))
# sol r4 CP-04: each mark carries its opened handle's file id; a mismatch, or one mark without it (or without a creation time), is a replaced file
$idS = $start.Clone(); $idS.fileId = '0000ABCD-0000000000000001'
$idE = $end.Clone(); $idE.fileId = '0000ABCD-0000000000000002'
$idSame = $end.Clone(); $idSame.fileId = '0000ABCD-0000000000000001'
$crE = $end.Clone(); $crE.created = [int64]639286056000000000
$r.fileId = @([string](Get-CorePartitionFeedSummary $idS $idE 120).reasons['rows'], [string](Get-CorePartitionFeedSummary $start $idSame 120).reasons['rows'],
    (Get-CorePartitionFeedSummary $idS $idSame 120).rows, [string](Get-CorePartitionFeedSummary $start $crE 120).reasons['rows'])
# sol r4 CP-04 (executed): the feed deleted and re-created under its name with the identical prefix plus a replayed 99 W row. NTFS name tunneling
# hands the new file the old creation time, so only the file id tells the two apart.
$ft = Join-Path $env:CP_TMP 'feed-swap.csv'
[IO.File]::WriteAllText($ft, "26.10.2026,01:00:00,x,30,10,No,No`n")
$mt = Read-CorePartitionFeedMark $ft
Remove-Item -LiteralPath $ft
[IO.File]::WriteAllText($ft, "26.10.2026,01:00:00,x,30,10,No,No`n25.10.2026,01:00:01,x,90,99,Yes,No`n")
$et = Read-CorePartitionFeedSince $ft ([int64]$mt.length) ([string]$mt.before)
$st = Get-CorePartitionFeedSummary $mt $et 120
$r.swap = @(([int64]$mt.created -eq [int64]$et.created), $et.exists, $st.rows, $st.pkgWattsMean, [string]$st.reasons['rows'])
$nope = Join-Path $env:CP_TMP 'no-such-feed.csv'
$rm = Read-CorePartitionFeedMark $nope; $rs = Read-CorePartitionFeedSince $nope 0 ''
$r.readerMissing = @($rm.exists, $rs.exists, $rm.ContainsKey('error'), $rs.ContainsKey('error'))
$r | ConvertTo-Json -Depth 6 -Compress
"""

# sol r3 CP-01: the generator's own job text, not a copy. The readiness lines (the helper-reason/flag initialisation and the not-ready rule) come from
# the pre-launch region, the cleanup block is the finally region verbatim, the scalar line is the success region's; the helper is a process double.
CLEANUP_DRIVER = r"""
$src = [IO.File]::ReadAllText($env:CP_GEN) -replace "`r`n", "`n"
$postAt = $src.IndexOf('$cpPost = $cpOpen')
if ($postAt -lt 0) { throw 'no $cpPost region in the generator' }
$b = $src.IndexOf("@'`n", $postAt) + 3
$e = $src.IndexOf("`n'@", $b)
$cleanup = $src.Substring($b, $e - $b)
$preLines = @([regex]::Matches($src.Substring(0, $postAt), '(?m)^(\$corePartitionHelper(Why|NotReady) = .*|if \(\$null -(ne|eq) \$corePartitionHelper -(and|or) -not \$corePartitionHelperLaunch\.ready\).*)$') | ForEach-Object { $_.Value })
if (@($preLines | Where-Object { $_.StartsWith('if ') }).Count -ne 1) { throw "the not-ready rule is not one line: $($preLines -join ' | ')" }
$scalarLine = [regex]::Match($src, '(?m)^\$corePartitionScalars = Get-CorePartitionScalars .*$').Value
if (-not $scalarLine) { throw 'no scalar line in the generator' }
function Write-JobTrace([string]$Message) { }
function Publish-AttrCudaText { param([string]$Path, [string]$Value) }
function New-AttrCudaDirectory { param([string]$Path) }
# sol r4 CP-05: the publisher double throws as the job's would for an injected IOException, or for a directory sitting at core-partition.json.
function Save-Json {
    param($Object, $Path)
    if ($global:cpPublish -eq 'throw') { throw [IO.IOException]::new('injected publisher IOException') }
    if (Test-Path -LiteralPath $Path -PathType Container) { throw [IO.IOException]::new("'$Path' is a directory") }
    $global:cpSaved = $Object
}
# sol r6 CP-06: the double's exit as the job reads it: exit -> 0, exit-nonzero -> 1, a killed helper -> -1, a failed wait -> not exited (unread).
function New-HelperDouble([string]$Mode) {
    $d = [pscustomobject]@{ Id = 4242; Mode = $Mode; Killed = $false }
    $d | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value { param([int]$Ms) if ($this.Mode -eq 'throw') { throw 'wait failed' }; if ($this.Mode -eq 'timeout' -and -not $this.Killed) { return $false }; return $true }
    $d | Add-Member -MemberType ScriptMethod -Name Kill -Value { $this.Killed = $true }
    $d | Add-Member -MemberType ScriptProperty -Name HasExited -Value { $this.Mode -ne 'throw' -and ($this.Mode -ne 'timeout' -or $this.Killed) }
    $d | Add-Member -MemberType ScriptProperty -Name ExitCode -Value { if ($this.Mode -eq 'exit-nonzero') { 1 } elseif ($this.Killed) { -1 } else { 0 } }
    $d
}
# sol r6 CP-06: the per-job key the job hands only to the helper, and the record's HMAC under it (what the helper writes beside the record)
$corePartitionHelperNonce = 'driver-key-00112233445566778899aabbccddeeff'
function Get-CpMac([string]$Json, [string]$Key) { [BitConverter]::ToString([Security.Cryptography.HMACSHA256]::new([Text.Encoding]::UTF8.GetBytes($Key)).ComputeHash([Text.Encoding]::UTF8.GetBytes($Json))).Replace('-', '') }
# sol r6 CP-07: the valid record with one numeric (or flag) field replaced by a non-number, HMAC'd under the job's key
function Set-CpField([string]$Json, [scriptblock]$Edit) { $o = $Json | ConvertFrom-Json; & $Edit $o; $o | ConvertTo-Json -Depth 8 -Compress }
$CorePartitionBenchMask = [int64]4095
$CorePartitionLoadMask = [int64]1044480
$CorePartitionLoadThreads = 8
$CorePartitionHelperMask = [int64]1044480
$record = '{"schema":"mlv-app/core-partition-helper/v1","pid":4242,"topologyCheck":{"ok":true,"reason":null,"pMask":4095,"eMask":1044480},"helperAffinityObserved":1044480,"app":{"pid":5000,"startUtcTicks":639286128040000000,"affinityFirst":4095,"affinityLast":4095},"load":{"seconds":15,"bytes":30000000000,"joined":true},"samples":[{"app":true,"m":1000,"inst":["0,0","0,12"],"freq":[2500,2500],"perf":[160,80],"park":[0,0]}],"instances":[{"pid":5000,"startUtcTicks":639286128040000000,"firstSeenUtcTicks":639286128041000000,"firstMs":4100,"lastSeenUtcTicks":639286128200000000,"goneUtcTicks":639286128202000000,"aliveMs":16000,"goneMs":16200}],"clockSkewMaxMs":0}'
# the runner's result.json: the launched pid and its launch..exit window, as the runner writes them (ISO 'o' strings)
$runnerResult = '{"process":{"id":5000,"startedAtUtc":"' + [DateTime]::new([int64]639286128030000000, [DateTimeKind]::Utc).ToString('o') + '","endedAtUtc":"' + [DateTime]::new([int64]639286128230000000, [DateTimeKind]::Utc).ToString('o') + '"}}'
$cases = @(
    @{ name = 'stop-timeout'; mode = 'timeout'; record = 'valid'; ready = $false },
    @{ name = 'normal-exit'; mode = 'exit'; record = 'valid'; ready = $false },
    @{ name = 'wait-exception'; mode = 'throw'; record = 'valid'; ready = $false },
    @{ name = 'record-unparseable'; mode = 'exit'; record = 'garbage'; ready = $false },
    @{ name = 'record-missing'; mode = 'exit'; record = 'none'; ready = $false },
    @{ name = 'stop-timeout-record-unparseable'; mode = 'timeout'; record = 'garbage'; ready = $false },
    @{ name = 'ready-stop-timeout-control'; mode = 'timeout'; record = 'valid'; ready = $true },
    @{ name = 'ready-normal-exit-control'; mode = 'exit'; record = 'valid'; ready = $true },
    @{ name = 'publish-io-exception'; mode = 'exit'; record = 'valid'; ready = $true; publish = 'throw' },
    @{ name = 'publish-directory'; mode = 'exit'; record = 'valid'; ready = $true; publish = 'directory' },
    @{ name = 'measured-pid-missing'; mode = 'exit'; record = 'valid'; ready = $true; result = 'none' },
    @{ name = 'record-not-the-helpers'; mode = 'exit'; record = 'planted'; ready = $true },
    # sol r6 CP-06 (= adversary A1, literal): the helper's schema and its live pid 4242 (printed on the job's trace line), no HMAC / an HMAC under
    # another key; a genuine record whose bytes were replaced after the helper wrote them; the helper never started (launch pid null) and a record
    # with pid 0; an otherwise valid record from a helper that exited non-zero.
    @{ name = 'planted-live-pid-no-mac'; mode = 'exit'; record = 'planted-live'; ready = $true },
    @{ name = 'planted-live-pid-wrong-key'; mode = 'exit'; record = 'planted-live-wrong-key'; ready = $true },
    @{ name = 'replaced-after-the-helper-wrote'; mode = 'exit'; record = 'replaced'; ready = $true },
    @{ name = 'launch-pid-null-record-pid-0'; mode = 'none'; record = 'pid0'; ready = $true; launchPid = 'null' },
    @{ name = 'helper-exit-nonzero'; mode = 'exit-nonzero'; record = 'valid'; ready = $true },
    # sol r6 CP-07 (= adversary A2, literal): {"schema":...,"pid":"oops"} as planted (no HMAC) and as the helper's own (HMAC'd)
    @{ name = 'pid-oops-planted'; mode = 'exit'; record = 'pid-oops'; ready = $true },
    @{ name = 'pid-oops-macd'; mode = 'exit'; record = 'pid-oops-mac'; ready = $true },
    # r8 A3 (adversary r7 6fac125a A3): Process.Start threw, so no helper object and launch.ready false; the feed has fresh complete rows
    @{ name = 'start-failed-fresh-feed'; mode = 'none'; record = 'none'; ready = $false; launchPid = 'null' })
# sol r6 CP-07: one non-numeric value in each numeric (or flag) field class the job reads from the record
$fieldEdits = [ordered]@{
    'topology-ok' = { param($o) $o.topologyCheck.ok = 'false' }
    'topology-pmask' = { param($o) $o.topologyCheck.pMask = 'oops' }
    'topology-emask' = { param($o) $o.topologyCheck.eMask = 'oops' }
    'helper-affinity' = { param($o) $o.helperAffinityObserved = 'oops' }
    'app-pid' = { param($o) $o.app.pid = 'oops' }
    'app-start' = { param($o) $o.app.startUtcTicks = 'oops' }
    'app-affinity-first' = { param($o) $o.app.affinityFirst = 'oops' }
    'app-affinity-last' = { param($o) $o.app.affinityLast = 'oops' }
    'instance-pid' = { param($o) $o.instances[0].pid = 'oops' }
    'instance-start' = { param($o) $o.instances[0].startUtcTicks = 'oops' }
    'instance-gone' = { param($o) $o.instances[0].goneUtcTicks = 'oops' }
    'load-seconds' = { param($o) $o.load.seconds = 'oops' }
    'load-bytes' = { param($o) $o.load.bytes = 'oops' }
    'sample-app' = { param($o) $o.samples[0].app = 'yes' }
    'sample-freq' = { param($o) $o.samples[0].freq[0] = 'oops' }
    'sample-perf' = { param($o) $o.samples[0].perf[0] = 'oops' }
    'sample-park' = { param($o) $o.samples[0].park[0] = 'oops' }
}
foreach ($k in $fieldEdits.Keys) { $cases += @{ name = "field-$k"; mode = 'exit'; record = 'field'; edit = $fieldEdits[$k]; ready = $true } }
# adversary r5 (40038f92) A2 (literal): another writer's record at the helper's output name
$planted = '{"schema":"not-the-helper-schema","pid":999999,"topologyCheck":{"ok":true,"pMask":4095,"eMask":1044480},"helperAffinityObserved":1044480,"app":{"pid":5000,"startUtcTicks":639286128040000000,"affinityFirst":4095,"affinityLast":4095},"instances":[{"pid":5000,"startUtcTicks":639286128040000000}],"others":[],"load":{"seconds":19,"bytes":38000000000},"samples":[]}'
# adversary r6 A1 (literal): the helper's schema and live pid 4242 with forged app, instances and load
$plantedLive = '{"schema":"mlv-app/core-partition-helper/v1","pid":4242,"topologyCheck":{"ok":true,"pMask":4095,"eMask":1044480},"helperAffinityObserved":1044480,"app":{"pid":5000,"startUtcTicks":639286128040000000,"affinityFirst":4095,"affinityLast":4095},"instances":[{"pid":5000,"startUtcTicks":639286128040000000,"goneUtcTicks":639286128202000000}],"others":[],"load":{"seconds":15,"bytes":30000000000},"samples":[]}'
$out = [ordered]@{}
foreach ($c in $cases) {
  try {
    $dir = Join-Path $env:CP_TMP $c.name
    [void](New-Item -ItemType Directory -Path $dir)
    $Work = $dir; $Pub = $dir
    $corePartitionHelperOut = Join-Path $dir 'core-partition-helper.json'
    $mac = Join-Path $dir 'core-partition-helper.json.mac'
    if ($c.record -eq 'valid') { [IO.File]::WriteAllText($corePartitionHelperOut, $record); [IO.File]::WriteAllText($mac, (Get-CpMac $record $corePartitionHelperNonce)) }
    elseif ($c.record -eq 'garbage') { [IO.File]::WriteAllText($corePartitionHelperOut, '{"topologyCheck":') }
    elseif ($c.record -eq 'planted') { [IO.File]::WriteAllText($corePartitionHelperOut, $planted) }
    elseif ($c.record -eq 'planted-live') { [IO.File]::WriteAllText($corePartitionHelperOut, $plantedLive) }
    elseif ($c.record -eq 'planted-live-wrong-key') { [IO.File]::WriteAllText($corePartitionHelperOut, $plantedLive); [IO.File]::WriteAllText($mac, (Get-CpMac $plantedLive 'a-key-the-job-never-handed-out')) }
    elseif ($c.record -eq 'replaced') { [IO.File]::WriteAllText($mac, (Get-CpMac $record $corePartitionHelperNonce)); [IO.File]::WriteAllText($corePartitionHelperOut, $plantedLive) }
    elseif ($c.record -eq 'pid0') { $j = $record.Replace('"pid":4242', '"pid":0'); [IO.File]::WriteAllText($corePartitionHelperOut, $j); [IO.File]::WriteAllText($mac, (Get-CpMac $j $corePartitionHelperNonce)) }
    elseif ($c.record -eq 'pid-oops') { [IO.File]::WriteAllText($corePartitionHelperOut, '{"schema":"mlv-app/core-partition-helper/v1","pid":"oops"}') }
    elseif ($c.record -eq 'pid-oops-mac') { $j = '{"schema":"mlv-app/core-partition-helper/v1","pid":"oops"}'; [IO.File]::WriteAllText($corePartitionHelperOut, $j); [IO.File]::WriteAllText($mac, (Get-CpMac $j $corePartitionHelperNonce)) }
    elseif ($c.record -eq 'field') { $j = Set-CpField $record $c.edit; [IO.File]::WriteAllText($corePartitionHelperOut, $j); [IO.File]::WriteAllText($mac, (Get-CpMac $j $corePartitionHelperNonce)) }
    $corePartitionStopFile = Join-Path $dir 'core-partition.stop'
    $corePartitionMeasuredFile = Join-Path $dir 'core-partition.measured'
    # sol r4 CP-02: the smoke runner's result.json names the process it launched (the record's bound pid)
    $resultPath = Join-Path $dir 'result.json'
    if ($c.result -ne 'none') { [IO.File]::WriteAllText($resultPath, $runnerResult) }
    $global:cpPublish = $c.publish
    if ($c.publish -eq 'directory') { [void](New-Item -ItemType Directory -Path (Join-Path $dir 'core-partition.json')) }
    $corePartitionFeedPath = Join-Path $dir 'feed.csv'
    [IO.File]::WriteAllText($corePartitionFeedPath, "26.10.2026,01:00:00,x,30,10,No,No`n")
    $corePartitionFeedPreHelper = Read-CorePartitionFeedMark $corePartitionFeedPath
    $corePartitionFeedStart = Read-CorePartitionFeedMark $corePartitionFeedPath
    [IO.File]::AppendAllText($corePartitionFeedPath, "26.10.2026,01:00:01,x,70,45,No,No`n26.10.2026,01:00:02,x,80,55,No,No`n")
    $corePartitionPreHelperUtc = (Get-Date).ToUniversalTime()
    $corePartitionMeasuredLaunchUtc = $corePartitionPreHelperUtc
    $corePartitionErrors = [Collections.Generic.List[string]]::new()
    $corePartitionJob = [ordered]@{ affinityBefore = $null; affinityRequested = '0x00FFF'; affinityApplied = '0x00FFF'; affinityRestored = $null }
    $corePartitionHelperNotReady = $null
    $corePartitionHelperExit = $null; $corePartitionHelperRecord = $null
    # r8 A1: the pre-launch region's (wall, monotonic) pairs at the pre-helper and measured-launch reads (absent before r8)
    $corePartitionClockPairs = [Collections.Generic.List[object]]::new()
    if (Get-Command Get-CorePartitionMonoMs -ErrorAction SilentlyContinue) { foreach ($n in 1, 2) { $corePartitionClockPairs.Add(@([DateTime]::UtcNow.Ticks, (Get-CorePartitionMonoMs))) } }
    $global:cpSaved = $null
    foreach ($l in $preLines) { if (-not $l.StartsWith('if ')) { . ([scriptblock]::Create($l)) } }
    $corePartitionHelper = $(if ($c.mode -ne 'none') { New-HelperDouble $c.mode })
    $corePartitionHelperLaunch = [ordered]@{ pid = $(if ($c.launchPid -ne 'null') { 4242 }); ready = $c.ready; readyWaitMs = 30000 }
    foreach ($l in $preLines) { if ($l.StartsWith('if ')) { . ([scriptblock]::Create($l)) } }
    . ([scriptblock]::Create($cleanup))
    $rawLog = "playback_smoke.start session=1 worker_threads=16`n"
    . ([scriptblock]::Create($scalarLine))
    $s = $corePartitionScalars
    $out[$c.name] = [ordered]@{
        topo = $s.corePartitionTopologyOk; bench = $s.benchAffinityApplied; load = $s.partitionLoadApplied; why = [string]$s.corePartitionNullReasons
        helperWhy = $corePartitionHelperWhy; notReady = [bool]$corePartitionHelperNotReady; killed = $corePartitionHelper.Killed
        pkg = @($s.pkgWattsMean, $s.pkgWattsMax, $s.pkgTempCMean, $s.pkgTempCMax, $s.pkgThrottledRows, $s.pkgTempCAtStart)
        published = $(if ($null -ne $global:cpSaved) { $ps = $global:cpSaved.feed.summary; @($ps.rows, $ps.pkgWattsMean, $ps.pkgWattsMax, $ps.pkgTempCMean, $ps.pkgTempCMax, $ps.pkgThrottledRows, $ps.pkgTempCAtStart, [string]$ps.reasons['pkgWattsMean']) } else { @('not-published') })
        errors = @($corePartitionErrors)
        # core-partition.json carries no refused record, and the summary scalars were computed (the summary line's input)
        publishedHelper = $(if ($null -ne $global:cpSaved) { $null -ne $global:cpSaved.helper } else { 'not-published' })
        scalars = ($null -ne $s -and @($s.Keys).Count -eq 20)
    }
  } catch { $out[$c.name] = [ordered]@{ threw = [string]$_.Exception.Message } }
}
$out | ConvertTo-Json -Depth 6 -Compress
"""


@requires_windows_pwsh
class CorePartitionPureHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="core-partition-feed-")
        cls.addClassCleanup(cls._tmp.cleanup)
        # a file, not -Command: the fixtures outgrew the 32767-char command line in r7
        script = Path(cls._tmp.name) / "fixtures.ps1"
        script.write_text(LIFT + FIXTURES, encoding="utf-8")
        proc = run_pwsh(["-File", str(script)], env_extra={"CP_GEN": str(GENERATOR), "CP_TMP": cls._tmp.name})
        cls.raw = proc
        cls.r = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.returncode == 0 and proc.stdout.strip() else None

    def setUp(self) -> None:
        self.assertIsNotNone(self.r, f"the lifted helpers did not run: {self.raw.stdout[-3000:]}{self.raw.stderr[-3000:]}")

    def cleanup_cases(self) -> dict:
        """CLEANUP_DRIVER's per-case results, run once per class (the job's own cleanup block and scalar line against process and I/O doubles)."""
        cls = type(self)
        if getattr(cls, "_cleanup", None) is None:
            driver = Path(cls._tmp.name) / "cleanup-driver.ps1"
            driver.write_text(LIFT + CLEANUP_DRIVER, encoding="ascii")
            work = Path(cls._tmp.name) / "cleanup"
            work.mkdir(exist_ok=True)
            proc = run_pwsh(["-File", str(driver)], env_extra={"CP_GEN": str(GENERATOR), "CP_TMP": str(work)})
            self.assertEqual(proc.returncode, 0, proc.stdout[-3000:] + proc.stderr[-3000:])
            cls._cleanup = json.loads(proc.stdout.strip().splitlines()[-1])
        return cls._cleanup

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
        self.assertEqual(self.r["stale"], "feed-freshness-unverified", "sol r6 CP-09: rows appended inside a 200 s span may be older than the 120 s limit")
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
        self.assertEqual(self.r["notReadyFlag"], [False, False, False], "the not-ready flag, not the reason text, holds the criteria closed")

    def test_a_helper_not_ready_at_launch_publishes_no_package_feed_values(self) -> None:
        """opus H2: the late helper's load ran inside the window, so the feed fields are null with a per-field not-ready reason."""
        self.assertEqual(self.r["notReadyFeed"][:6], [None] * 6)
        self.assertEqual(self.r["notReadyFeed"][6:], [50.0, 62.5], "a ready helper still publishes the feed")
        for f in ("pkgWattsMean", "pkgWattsMax", "pkgTempCMean", "pkgTempCMax", "pkgThrottledRows", "pkgTempCAtStart"):
            self.assertIn(f"{f}=helper-not-ready-at-launch", self.r["notReady"][3])

    def test_an_unfinished_last_feed_row_is_not_a_row(self) -> None:
        """sol r4 A3: the end read can land between a row's text and its newline; that row (here its throttle flag cut to 'Y') is not evidence."""
        self.assertEqual(self.r["partialRow"], ["feed-no-rows-in-window", 1, 40.0, 61.0])

    def test_a_non_finite_feed_value_is_unparseable(self) -> None:
        """sol r4 A4: double.TryParse accepts NaN, Infinity and 1e309; the summary must not publish them as package values."""
        self.assertEqual(self.r["nonFinite"], ["feed-unparseable"] * 4)
        self.assertEqual(self.r["nonFiniteStart"], "feed-unparseable")

    def test_launch_readiness_rejection_survives_every_cleanup_outcome(self) -> None:
        """sol r3 CP-01: the job's exact cleanup block (lifted from the generator) run against a helper process double that was not ready at the
        measured launch, then the job's exact scalar line. A late helper that publishes a valid B2 record must never regain the criteria."""
        got = self.cleanup_cases()
        # sol r6 CP-06: a killed helper (exit -1) or one whose exit was never read cannot vouch for its record; that reason is appended too
        first = {
            "stop-timeout": "helper-not-ready-at-launch+helper-killed-after-stop-timeout+helper-exit-nonzero",
            "normal-exit": "helper-not-ready-at-launch",
            "wait-exception": "helper-not-ready-at-launch+helper-exit-unread",
            "record-unparseable": "helper-not-ready-at-launch+helper-record-unparseable",
            "record-missing": "helper-not-ready-at-launch",
            "stop-timeout-record-unparseable": "helper-not-ready-at-launch+helper-killed-after-stop-timeout+helper-record-unparseable",
        }
        for case, why in first.items():
            with self.subTest(case=case):
                c = got[case]
                self.assertEqual([c["topo"], c["bench"], c["load"]], [False, False, False], f"a not-ready helper regained a criterion: {c}")
                self.assertEqual(c["helperWhy"], why, "the first reason is kept, a later cleanup reason is appended")
                self.assertTrue(c["notReady"], c)
                self.assertIn("corePartitionTopologyOk=helper-not-ready-at-launch", c["why"])
                self.assertEqual(c["pkg"], [None] * 6, f"a not-ready helper published package-feed values: {c}")
                self.assertIn("pkgWattsMean=helper-not-ready-at-launch", c["why"])
                # sol r4 A1: core-partition.json's feed summary is held to the same rule as the success-summary scalars. r11 B1: no accepted record
                # carries the app's observed exit, so the window end is unverified too, appended after the not-ready reason
                self.assertEqual(c["published"], [None] * 7 + ["helper-not-ready-at-launch+feed-window-end-unverified"], f"core-partition.json published feed values: {c}")
        self.assertTrue(got["stop-timeout"]["killed"], "the stop-timeout double was killed (the branch under test ran)")
        # sol r6 CP-06: a ready helper the job had to kill no longer vouches for its record (exit -1). r11 B1 (CP-FEED-END-ANCHOR): nor for the
        # app's observed exit, the only independent bound on the feed window's end, so the window rows are unverified evidence (until r11 the
        # window was still published here)
        killed = got["ready-stop-timeout-control"]
        self.assertEqual([killed["topo"], killed["bench"], killed["load"]], [False, False, False], f"a killed helper's record was accepted: {killed}")
        self.assertEqual(killed["helperWhy"], "helper-killed-after-stop-timeout+helper-exit-nonzero")
        self.assertFalse(killed["notReady"])
        self.assertEqual(killed["pkg"][:4], [None] * 4, f"a window end no observed exit bounds was published: {killed}")
        self.assertIn("pkgWattsMean=feed-window-end-unverified", killed["why"])
        self.assertEqual(killed["published"][:3], [None] * 3, f"a window end no observed exit bounds was published: {killed}")
        control = got["ready-normal-exit-control"]
        self.assertEqual([control["topo"], control["bench"], control["load"]], [True, True, True], f"a ready helper that exited 0 with its HMAC'd record is accepted: {control}")
        self.assertIsNone(control["helperWhy"], control)

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

    def test_the_evidence_is_the_measured_pids_and_a_concurrent_same_path_instance_refuses_it(self) -> None:
        """sol r4 CP-02 (= adversary A1), from its literal: binding, the bound instance's affinity, load totals, samples and scalar acceptance."""
        refused = {
            "r4-bound-4242": "same-path-instance-overlap",
            "bound-5000-beside-4242": "same-path-instance-overlap",
            "pid-unknown": "measured-pid-unknown",
            "window-unknown": "measured-window-unknown",
            "pid-not-observed": "measured-app-not-observed",
            "pid-reused": "measured-pid-ambiguous",
            "helper-bound-another": "helper-app-not-measured-pid",
            # adversary r5 (7760ad24) A1-A3, their literal inputs
            "pre-launch-alive": "same-path-instance-overlap",
            "start-time-overlap": "same-path-instance-overlap",
            "replacement-after-exit": "measured-app-not-observed",
            # adversary r5 (40038f92) A3
            "predecessor-gone-before-launch": "measured-app-not-observed",
        }
        for case, code in refused.items():
            with self.subTest(case=case):
                topo, bench, load, observed, gbps, pcore, why = self.r["measured"][case]
                self.assertEqual([bench, load, observed, gbps, pcore], [False, False, None, None, None], f"evidence not bound to the measured pid was accepted: {why}")
                self.assertTrue(topo, "the host topology is not the app's evidence and stays as checked")
                for field in ("appAffinityObserved", "partitionLoadGBps", "pcoreMHzMean", "ecoreMHzMean", "pcoreParkedSampleFraction"):
                    self.assertIn(f"{field}={code}", why)
        for case in ("probe-then-app-control", "pid-reused-before-window-control"):
            with self.subTest(case=case):
                control = self.r["measured"][case]
                self.assertEqual(control[:6], [True, True, True, "0x00FFF", 2.0, 4000.0], f"a same-path process gone before the measured app started is no overlap: {control}")

    def test_a_wall_clock_step_while_the_helper_watched_refuses_the_overlap_verdict(self) -> None:
        """r8 A1 (adversary r7 6fac125a A1 + the r8 brief's 100 s literal): a backward wall-clock step stamps another same-path process's gone time
        before the measured app's start, so the wall-clock overlap test passes. A wall/monotonic gap over the 1000 ms tolerance (the helper's or the
        job's), or a gap nobody recorded, refuses the verdict with a typed reason; bench and load acceptance are then false."""
        refused = {"step-100s-back": "clock-discontinuity", "step-6s-restored": "clock-discontinuity", "job-step": "clock-discontinuity",
                   "unrecorded": "clock-unverified", "job-unverified": "clock-unverified",
                   # adversary r8 (e1ce216c) A1: under the tolerance the gap widens the overlap test instead
                   "sub-tolerance-step-overlap": "same-path-instance-overlap"}
        for case, code in refused.items():
            with self.subTest(case=case):
                topo, bench, load, observed, gbps, pcore, why = self.r["clock"][case]
                self.assertEqual([bench, load, observed, gbps, pcore], [False, False, None, None, None], f"an overlap verdict under a stepped clock was trusted: {why}")
                for field in ("appAffinityObserved", "partitionLoadGBps", "pcoreMHzMean", "ecoreMHzMean", "pcoreParkedSampleFraction"):
                    self.assertIn(f"{field}={code}", why)
        control = self.r["clock"]["under-tolerance-control"]
        self.assertEqual(control[:6], [True, True, True, "0x00FFF", 2.0, 4000.0], f"a gap under the tolerance with no real overlap is accepted: {control}")
        self.assertEqual(self.r["clockSkew"], [0, 100000, None, None, None], self.r["clockSkew"])

    def test_a_helper_that_never_started_publishes_no_package_feed_value_through_the_job_cleanup(self) -> None:
        """r8 A3 (adversary r7 6fac125a A3) through the job's own not-ready rule, cleanup block and scalar line: no helper object, fresh feed rows."""
        c = self.cleanup_cases()["start-failed-fresh-feed"]
        self.assertNotIn("threw", c, c)
        self.assertTrue(c["notReady"], f"a helper that never started left the feed open: {c}")
        self.assertEqual(c["helperWhy"], "helper-start-failed+helper-not-started", c)
        self.assertEqual(c["pkg"], [None] * 6, f"the summary published package-feed values: {c}")
        self.assertEqual(c["published"], [None] * 7 + ["helper-not-ready-at-launch+feed-window-end-unverified"], f"core-partition.json published package-feed values: {c}")
        self.assertEqual([c["topo"], c["bench"], c["load"]], [False, False, False], c)

    def test_a_negative_feed_age_is_no_freshness(self) -> None:
        """adversary r5 (40038f92) A4: a backward clock step must not turn a 199 s old feed into package values. r7: freshness is monotonic only,
        so a 200 s window (stepped clock or a future stamp) proves nothing and a 30 s window reads whatever the stamps say."""
        self.assertEqual(self.r["negativeAge"], ["feed-freshness-unverified", None, "feed-freshness-unverified", 1])

    def test_feed_freshness_is_measured_on_the_monotonic_clock(self) -> None:
        """sol r6 CP-09 (= adversary r6 A4), its literal: last write at real +1 s, end read at real +200 s after a 100 s backward UTC step, limit 120 s."""
        m = self.r["monotonic"]
        self.assertEqual(m[:3], [None, None, "feed-freshness-unverified"], f"a feed 199 s old was published as fresh after a backward clock step: {m}")
        self.assertEqual(m[3:6], [1, 45.0, ""], f"a 30 s monotonic window is fresh whatever the wall clock did: {m}")
        for got in m[6:9]:
            self.assertEqual(got, [None, "feed-freshness-unverified"], f"a mark without a usable monotonic baseline proved freshness: {m}")
        self.assertEqual(m[9:], ["feed-stale", "feed-freshness-unverified"], "a feed unchanged for 200 s monotonic is stale; unchanged over 5 s proves nothing")

    def test_no_wall_clock_shape_or_file_swap_makes_an_old_row_fresh(self) -> None:
        """adversary r7 (15b3bbf7) A1 and A2, their literals: a two-way clock step whose endpoints agree, and a feed replaced by a day-old file between
        the pre-helper and start reads; plus a start mark grown only by an unfinished row since the pre-helper read, and its control."""
        a = self.r["adversaryR7"]
        self.assertEqual(a[:3], ["feed-freshness-unverified", None, None], f"A1: a 199 s old row was published: {a}")
        self.assertEqual(a[3:5], [1, 45.0], f"A2: the 20 s window itself is fresh: {a}")
        self.assertEqual(a[5:7], [None, "feed-freshness-unverified"], f"A2: a replaced file's day-old row was the start temperature: {a}")
        self.assertEqual(a[7:], ["feed-freshness-unverified", 20.0], f"only a complete row ending after the pre-helper offset is fresh: {a}")
        # adversary r7 (6fac125a) A2: a bare newline or a header line appended after the pre-helper read proves no new data row
        self.assertEqual(self.r["adversaryR7b"], [None, "feed-freshness-unverified", None, "feed-freshness-unverified", 61.0, ""], self.r["adversaryR7b"])
        # adversary r8 (e1ce216c) A2: an LF that only completes a row already there at the pre-helper read proves no new data
        self.assertEqual(self.r["adversaryR8"], [1, None, "feed-freshness-unverified"], self.r["adversaryR8"])

    def test_one_guarded_parse_reads_every_record_number(self) -> None:
        """sol r6 CP-07: JSON numbers and invariant numeric text parse; text, flags, arrays, fractions, out-of-range and non-finite values do not."""
        self.assertEqual(self.r["number"], [[True, 4242], [True, 4242], [False, None], [False, None], [False, None], [True, 639286128040000000], [False, None],
                                            [False, None], [False, None], [False, None], [False, None], [True, None], [True, 2.5], [False, None],
                                            [True, 9210000000000000000]])

    def test_a_non_numeric_record_value_reaching_a_consumer_never_throws(self) -> None:
        """sol r6 CP-07 sites :4299 :4399 :4404 :4473 :4498 :4501 :4512 :4514, each called directly with a non-number."""
        c = self.r["consumers"]
        self.assertFalse([x for x in c if str(x).startswith("threw")], f"a consumer threw on a non-numeric record value: {c}")
        self.assertEqual(c[:5], ["", "", "", "", "0"], c)
        self.assertEqual(c[5], "helper-record-malformed")
        self.assertTrue(c[6].startswith("False|False|"), c[6])
        self.assertIn("corePartitionTopologyOk=helper-record-malformed", c[6])
        self.assertTrue(c[7].startswith("False|"), c[7])
        self.assertIn("corePartitionTopologyOk=helper-record-malformed", c[7])

    def test_the_record_hmac_is_checked_under_the_jobs_key(self) -> None:
        """sol r6 CP-06: the right key verifies (any hex case, trailing line ending); another key, no MAC, no key or non-hex text does not."""
        self.assertEqual(self.r["mac"], [True, True, True, False, False, False, False])

    def test_a_record_not_provably_the_helpers_own_is_refused(self) -> None:
        """sol r6 CP-06 (= adversary r6 A1), through the job's cleanup block and scalar line: no HMAC or another key's HMAC on a record with the
        helper's schema and live pid, a genuine record's bytes replaced after the helper wrote them, a null launch pid with record pid 0, and a
        non-zero helper exit. Each is refused with a typed reason, every acceptance flag false, core-partition.json still published without it."""
        got = self.cleanup_cases()
        expect = {
            "planted-live-pid-no-mac": "helper-record-not-the-helpers",
            "planted-live-pid-wrong-key": "helper-record-not-the-helpers",
            "replaced-after-the-helper-wrote": "helper-record-not-the-helpers",
            # r8 A3: no helper object at the measured launch is itself not-ready (helper-start-failed), whatever launch.ready says
            "launch-pid-null-record-pid-0": "helper-start-failed+helper-not-started+helper-record-not-the-helpers",
            "helper-exit-nonzero": "helper-exit-nonzero",
        }
        for case, why in expect.items():
            with self.subTest(case=case):
                c = got[case]
                self.assertNotIn("threw", c, c)
                self.assertEqual([c["topo"], c["bench"], c["load"]], [False, False, False], f"a record not provably the helper's was accepted: {c}")
                self.assertEqual(c["helperWhy"], why, c)
                self.assertIn(f"corePartitionTopologyOk={why}", c["why"])
                self.assertIs(c["publishedHelper"], False, f"core-partition.json carried the refused record or was not published: {c}")
                self.assertTrue(c["scalars"], c)

    def test_a_malformed_number_in_the_record_is_a_typed_refusal_and_everything_still_publishes(self) -> None:
        """sol r6 CP-07 (= adversary r6 A2), its literal {"schema":...,"pid":"oops"} (planted and HMAC'd) and one non-number in each numeric (or
        flag) field class of an otherwise valid HMAC'd record: a typed refusal, all flags false, core-partition.json and the summary scalars present."""
        got = self.cleanup_cases()
        cases = {"pid-oops-planted": "helper-record-not-the-helpers", "pid-oops-macd": "helper-record-malformed"}
        cases.update({k: "helper-record-malformed" for k in got if k.startswith("field-")})
        self.assertEqual(len([k for k in cases if k.startswith("field-")]), 17, sorted(got))
        for case, why in cases.items():
            with self.subTest(case=case):
                c = got[case]
                self.assertNotIn("threw", c, f"a malformed record aborted the cleanup: {c}")
                self.assertEqual([c["topo"], c["bench"], c["load"]], [False, False, False], c)
                self.assertEqual(c["helperWhy"], why, c)
                self.assertIs(c["publishedHelper"], False, f"core-partition.json missing or carrying the malformed record: {c}")
                self.assertTrue(c["scalars"], c)

    def test_a_record_another_writer_put_at_the_helpers_name_is_not_the_helpers(self) -> None:
        """adversary r5 (40038f92) A2, its literal record, through the job's cleanup block and scalar line."""
        c = self.cleanup_cases()["record-not-the-helpers"]
        self.assertEqual([c["topo"], c["bench"], c["load"]], [False, False, False], f"a planted helper record was accepted: {c}")
        self.assertEqual(c["helperWhy"], "helper-record-not-the-helpers")
        self.assertIn("corePartitionTopologyOk=helper-record-not-the-helpers", c["why"])

    def test_the_runners_launch_and_exit_times_read_as_utc_ticks(self) -> None:
        """adversary r5 A3: the measured window comes from result.json; a DateTime and its ISO string give the same ticks, absent or unreadable is 0."""
        self.assertEqual(self.r["utcTicks"], [639286128030000000, 639286128030000000, 0, 0])

    def test_finite_feed_rows_whose_mean_overflows_are_a_typed_refusal(self) -> None:
        """sol r4 CP-03 (= adversary A2), its literal rows: both package means, and the success-summary scalars."""
        self.assertEqual(self.r["aggregate"][:6], [None] * 6, f"an overflowing mean published window values: {self.r['aggregate']}")
        self.assertEqual(self.r["aggregate"][6:9], ["feed-aggregate-non-finite"] * 3)
        self.assertEqual(self.r["aggregate"][9], 30.0, "the start temperature (one finite row before the launch) is unaffected")
        self.assertFalse(self.r["aggregateJsonInfinity"], "core-partition.json would carry the string Infinity")
        self.assertEqual(self.r["aggregateWatts"], [None, None, "feed-aggregate-non-finite", "feed-aggregate-non-finite"], "a watts-only overflow refuses the whole window")
        self.assertEqual(self.r["aggregateScalars"][:2], [None, None])
        for f in ("pkgWattsMean", "pkgTempCMean"):
            self.assertIn(f"{f}=feed-aggregate-non-finite", self.r["aggregateScalars"][2])

    def test_counter_readings_never_aggregate_to_a_non_finite_mean(self) -> None:
        """sol r4 CP-03 (class): the clock math and the P/E means."""
        self.assertEqual(self.r["mhzOverflow"], [None, None, None])
        self.assertEqual(self.r["counterOverflow"], [2, None, False])

    def test_a_feed_mark_without_the_same_handle_identity_is_a_replaced_file(self) -> None:
        """sol r4 CP-04 (= adversary A3): file id mismatch, one-sided file id and one-sided creation time are each feed-replaced."""
        self.assertEqual(self.r["fileId"], ["feed-replaced", "feed-replaced", 3, "feed-replaced"])

    def test_a_feed_deleted_and_recreated_under_its_name_supplies_no_window_even_when_its_creation_time_tunnels(self) -> None:
        """sol r4 CP-04, executed through both real readers: the replayed 99 W row is never evidence."""
        tunneled, exists, rows, watts, reason = self.r["swap"]
        self.assertTrue(exists)
        self.assertEqual([rows, watts, reason], [None, None, "feed-replaced"], f"a re-created feed supplied window evidence (creation time tunneled: {tunneled})")
        self.assertEqual(self.r["readerMissing"], [False, False, False, False], "a missing feed is exists=false without an error, from both readers")

    def test_the_feed_readers_take_their_metadata_from_the_opened_handle(self) -> None:
        """sol r4 CP-04: no metadata by path in either reader (the swap between a path lookup and the open is not reachable in a test)."""
        src = lf(GENERATOR.read_text(encoding="utf-8"))
        for name in ("Read-CorePartitionFeedMark", "Read-CorePartitionFeedSince"):
            with self.subTest(reader=name):
                m = re.search(r"(?ms)^function " + re.escape(name) + r"\(.*?^\}$", src)
                self.assertIsNotNone(m)
                body = m.group(0)
                for banned in ("FileInfo", "Get-Item", "Test-Path", "GetCreationTime", "GetLastWriteTime", ".Exists", "LastWriteTimeUtc", "CreationTimeUtc"):
                    self.assertNotIn(banned, body, f"{name} reads metadata by path ({banned})")
                self.assertLess(body.index("[IO.File]::Open("), body.index("Get-CorePartitionFeedHandleInfo $fs"), "the identity is the opened stream's")
                self.assertIn("created = $id.created; fileId = $id.fileId", body)

    def test_a_core_partition_json_publish_failure_clears_every_acceptance_flag(self) -> None:
        """sol r4 CP-05 (= adversary A4): an injected publisher IOException, and a directory at Pub/core-partition.json, through the scalar line."""
        got = self.cleanup_cases()
        control = got["ready-normal-exit-control"]
        self.assertEqual([control["topo"], control["bench"], control["load"]], [True, True, True], f"a published ready run is accepted: {control}")
        for case in ("publish-io-exception", "publish-directory"):
            with self.subTest(case=case):
                c = got[case]
                self.assertEqual(c["published"], "not-published", c)
                self.assertEqual([c["topo"], c["bench"], c["load"]], [False, False, False], f"an unpublished core-partition.json left a flag true: {c}")
                for flag in ("corePartitionTopologyOk", "benchAffinityApplied", "partitionLoadApplied"):
                    self.assertIn(f"{flag}=core-partition-json-unpublished", c["why"])
                self.assertTrue(any(e.startswith("publish: ") for e in c["errors"]), c)

    def test_an_unknown_measured_pid_through_the_job_cleanup_binds_nothing(self) -> None:
        """sol r4 CP-02 through the job's cleanup block: no runner result.json, so no measured pid, so no app evidence is accepted."""
        c = self.cleanup_cases()["measured-pid-missing"]
        self.assertEqual([c["bench"], c["load"]], [False, False], c)
        self.assertIn("appAffinityObserved=measured-pid-unknown", c["why"])
        self.assertTrue(any(e.startswith("measured-pid: ") for e in c["errors"]), c)


# --- the runner's mapping (offline test mode, stub generator) -------------------------------------------------------------------------------
# r9: the Dot items' literals against the generator's own pure consumers (lifted). Each RED case fails on aeacec83; each control passes on both.
R9_PURE = r"""
$L = 639286128000000000
$r = [ordered]@{}
$feed = [ordered]@{ reasons = [ordered]@{} }
$w = @{ value = 16; why = $null }
# r10 B2: as in FIXTURES, a hand-built mark without doneMs completed its read at its stamp, and the end read at the window's end
$cpFeedSummaryImpl = ${function:Get-CorePartitionFeedSummary}
function Get-CorePartitionFeedSummary($Start, $End, [double]$StaleSec, $Base = $null) {
    $s0 = $Start; $e0 = $End
    if ($s0 -is [hashtable] -and $s0.ContainsKey('monoMs') -and -not $s0.ContainsKey('doneMs')) { $s0 = $s0.Clone(); $s0.doneMs = $s0.monoMs }
    if ($e0 -is [hashtable] -and $e0.ContainsKey('monoMs') -and -not $e0.ContainsKey('doneMs')) { $e0 = $e0.Clone(); $e0.doneMs = $e0.monoMs }
    # r11 B1: the app's last observed-alive poll is at the window end (a pre-r11 generator ignores the extra arguments)
    $we = $(if ($null -ne $e0 -and $e0.doneMs -is [ValueType]) { $e0.doneMs } else { 0 })
    & $cpFeedSummaryImpl $s0 $e0 $StaleSec $Base $we 2000 $we
}
# Dot 1 (= adversary r8 aeacec83 A1, literal): B2 masks bench 0x00FFF, load/helper 0xFF000, 8 threads. Helper 4242; app 5000 starts 12:00:03Z, runner
# window 12:00:02.9Z..12:00:23Z. A stop file at 12:00:04.95Z ends the genuine helper's watch; it signs at 12:00:05Z with affinityFirst = affinityLast
# = 4095, load 2 s and positive bytes, the app's goneUtcTicks null, clockSkewMaxMs 0. Control: the same run stopped after the observed exit.
function New-R9Record([string]$Gone, [string]$GoneMs, [string]$AliveMs, [string]$EndedBy, [string]$Samples = '[]') {
    ('{"schema":"mlv-app/core-partition-helper/v1","pid":4242,"topologyCheck":{"ok":true,"reason":null,"pMask":4095,"eMask":1044480},"helperAffinityObserved":1044480,' +
        '"app":{"pid":5000,"startUtcTicks":' + ($L + 30000000) + ',"affinityFirst":4095,"affinityLast":4095},"load":{"seconds":2.0,"bytes":4000000000,"joined":true},"samples":' + $Samples + ',' +
        '"instances":[{"pid":5000,"startUtcTicks":' + ($L + 30000000) + ',"firstSeenUtcTicks":' + ($L + 31000000) + ',"firstMs":3100,"lastSeenUtcTicks":' + ($L + 49000000) + ',"goneUtcTicks":' + $Gone +
        ',"aliveMs":' + $AliveMs + ',"goneMs":' + $GoneMs + ',"affinityFirst":4095,"affinityLast":4095}],"others":[],"clockSkewMaxMs":0,"endedBy":"' + $EndedBy + '"}') | ConvertFrom-Json
}
$mp = @{ MeasuredPid = [int64]5000; MeasuredStartTicks = $L + 29000000; MeasuredEndTicks = $L + 230000000 }
$r.dot1 = [ordered]@{}
foreach ($k in @('stop-while-alive', 'deadline-while-alive', 'endpoint-gap-3400ms', 'full-window-control')) {
    $rec = switch ($k) {
        'stop-while-alive' { New-R9Record 'null' 'null' '4900' 'stop-file' }
        'deadline-while-alive' { New-R9Record 'null' 'null' '4900' 'deadline' }
        # adversary r9 (5592421d) A2 (literal): last alive poll 20000 ms, exit seen at 23400 ms (a descheduled helper); steady clocks
        'endpoint-gap-3400ms' { New-R9Record ([string]($L + 234000000)) '23400' '20000' 'stop-file' }
        'full-window-control' { New-R9Record ([string]($L + 230500000)) '23050' '22950' 'stop-file' }
    }
    $s = Get-CorePartitionScalars $rec $feed 4095 1044480 8 $w $null $false @mp
    $r.dot1[$k] = @($s.benchAffinityApplied, $s.partitionLoadApplied, $s.appAffinityObserved, [string]$s.corePartitionNullReasons, (Test-CorePartitionMeasuredApp $rec 5000 $mp.MeasuredStartTicks $mp.MeasuredEndTicks 0).endpointMs)
}
# Dot 4 (= adversary r8 aeacec83 A4, literal): measured 5000 runs +3..+23 s; the helper saw it alive at 22.95 s (aliveMs), was descheduled, took a sample at
# 24 s still tagged with 5000 (inst 0,0, 2500 MHz at 80 %), and saw it gone at 24.2 s. Control: the same sample taken at 20 s.
$r.dot4 = [ordered]@{}
# The literal's exit seen at 24.2 s is a 1250 ms alive-to-gone gap, which r9 refuses outright (adversary r9 5592421d A2: coverage-endpoint-gap); the
# sub-tolerance variant (exit seen at 23.9 s, the sample at 23.5 s) isolates the sample clip.
foreach ($k in @('post-exit-sample', 'literal-gone-24200', 'in-window-control')) {
    $m = $(switch ($k) { 'post-exit-sample' { 23500 } 'literal-gone-24200' { 24000 } default { 20000 } })
    $gm = $(if ($k -eq 'literal-gone-24200') { 24200 } else { 23900 })
    $smp = '[{"tMs":' + $m + ',"m":' + $m + ',"app":true,"appPid":5000,"inst":["0,0"],"freq":[2500],"perf":[80],"park":[0]}]'
    $s = Get-CorePartitionScalars (New-R9Record ([string]($L + [int64]$gm * 10000)) ([string]$gm) '22950' 'stop-file' $smp) $feed 4095 1044480 8 $w $null $false @mp
    $r.dot4[$k] = @($s.pcoreMHzMean, [string]$s.corePartitionNullReasons)
}
# Dot 2 (Dot literal): an old 34-byte row at byte 0; the pre-helper read saw 100 bytes. The start read stat 200 bytes, the file was truncated under it,
# and the read returned only the old row: an aeacec83 mark keeps length 200 and invents row start 166 (>= 100), so its 99 C start temperature is published.
$old = "25.10.2026,12:00:00,x,99,10,No,No`n"
$base = @{ exists = $true; length = 100; monoMs = [int64]1000; created = 123; fileId = 'same' }
$end = @{ exists = $true; length = 300; monoMs = [int64]9000; created = 123; fileId = 'same'; before = ''; window = "26.10.2026,12:00:08,x,70,45,No,No`n"; windowStartsMidRow = $false; replaced = $false; truncated = $false }
$aeacec83Mark = @{ exists = $true; length = 200; monoMs = [int64]6000; created = 123; fileId = 'same'; before = $old; window = ''; windowStartsMidRow = $false }
# the same short read as an r9 reader reports it: the bytes read (length 34 from offset 0) and shortRead
$r9Mark = @{ exists = $true; length = 34; beforeOffset = 0; shortRead = $true; monoMs = [int64]6000; created = 123; fileId = 'same'; before = $old; window = ''; windowStartsMidRow = $false }
# control: a complete full-length read (134 bytes): the old row, a non-data line, then a new row starting at byte 100
$full = $old + ('x' * 65) + "`n" + "26.10.2026,12:00:05,x,61,40,No,No`n"
$fullMark = @{ exists = $true; length = 134; beforeOffset = 0; shortRead = $false; monoMs = [int64]6000; created = 123; fileId = 'same'; before = $full; window = ''; windowStartsMidRow = $false }
$r.dot2 = [ordered]@{}
foreach ($k in @('aeacec83-mark', 'r9-short-read', 'full-read-control')) {
    $sm = switch ($k) { 'aeacec83-mark' { $aeacec83Mark } 'r9-short-read' { $r9Mark } 'full-read-control' { $fullMark } }
    $x = Get-CorePartitionFeedSummary $sm $end.Clone() 120 $base
    $r.dot2[$k] = @($x.pkgTempCAtStart, [string]$x.reasons['pkgTempCAtStart'])
}
# Dot 2, the real reader: a short read through its byte-read seam (the file truncated to the old row between the stat and the read), and complete
# reads of a small and a >4 KiB file, whose text must be exactly the bytes from beforeOffset to length
$r.dot2Reader = 'absent'
if (Get-Command Read-CorePartitionFeedBytes -ErrorAction SilentlyContinue) {
    $fp = Join-Path $env:CP_TMP 'r9-feed.csv'
    [IO.File]::WriteAllText($fp, $full)
    $impl = ${function:Read-CorePartitionFeedBytes}
    function Read-CorePartitionFeedBytes($Stream, $Buffer, $Count) {
        $tw = [IO.File]::Open($fp, [IO.FileMode]::Open, [IO.FileAccess]::Write, [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete); try { $tw.SetLength(34) } finally { $tw.Dispose() }
        & $impl $Stream $Buffer $Count
    }
    $short = Read-CorePartitionFeedMark $fp
    Remove-Item function:Read-CorePartitionFeedBytes; Set-Item function:Read-CorePartitionFeedBytes $impl
    [IO.File]::WriteAllText($fp, $full)
    $whole = Read-CorePartitionFeedMark $fp
    $sb = [Text.StringBuilder]::new(); for ($i = 0; $i -lt 200; $i++) { [void]$sb.Append("26.10.2026,12:00:00,x,30,10,No,No`n") }
    [IO.File]::WriteAllText($fp, $sb.ToString())
    $big = Read-CorePartitionFeedMark $fp
    $shortSince = $null
    [IO.File]::WriteAllText($fp, $full)
    function Read-CorePartitionFeedBytes($Stream, $Buffer, $Count) {
        $tw = [IO.File]::Open($fp, [IO.FileMode]::Open, [IO.FileAccess]::Write, [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete); try { $tw.SetLength(60) } finally { $tw.Dispose() }
        & $impl $Stream $Buffer $Count
    }
    $shortSince = Read-CorePartitionFeedSince $fp 34 $old
    Remove-Item function:Read-CorePartitionFeedBytes; Set-Item function:Read-CorePartitionFeedBytes $impl
    $r.dot2Reader = [ordered]@{
        short = @($short.length, $short.beforeOffset, $short.shortRead, [string](Test-CorePartitionFeedMarkAccounted $short), [string](Get-CorePartitionFeedSummary $short $end.Clone() 120 $base).reasons['pkgTempCAtStart'])
        whole = @($whole.length, $whole.beforeOffset, $whole.shortRead, [string](Test-CorePartitionFeedMarkAccounted $whole))
        big = @($big.length, $big.beforeOffset, $big.shortRead, [string](Test-CorePartitionFeedMarkAccounted $big), ($big.beforeOffset % 34))
        since = @($shortSince.length, $shortSince.shortRead, [string](Get-CorePartitionFeedSummary $whole $shortSince 120 $base).reasons['rows'])
    }
}
$r | ConvertTo-Json -Depth 6 -Compress
"""


@requires_windows_pwsh
class CorePartitionR9Tests(unittest.TestCase):
    """r9 (Dot record a65f1786 = adversary r8 aeacec83 A1, A2, A4): the literals against the generator's own pure consumers and readers."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="core-partition-r9-")
        cls.addClassCleanup(cls._tmp.cleanup)
        script = Path(cls._tmp.name) / "r9.ps1"
        script.write_text(LIFT + R9_PURE, encoding="utf-8")
        proc = run_pwsh(["-File", str(script)], env_extra={"CP_GEN": str(GENERATOR), "CP_TMP": cls._tmp.name})
        cls.raw = proc
        cls.r = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.returncode == 0 and proc.stdout.strip() else None

    def setUp(self) -> None:
        self.assertIsNotNone(self.r, f"the lifted helpers did not run: {self.raw.stdout[-3000:]}{self.raw.stderr[-3000:]}")

    def test_a_record_whose_watch_ended_before_the_runners_exit_is_refused_typed(self) -> None:
        """Dot 1 (= adversary r8 aeacec83 A1): a genuinely authenticated record whose app exit the helper never observed does not cover the runner's
        window, so bench and load acceptance are false with a typed coverage reason; the full-window control stays accepted."""
        for case, code in (("stop-while-alive", "coverage-stop-before-runner-exit"), ("deadline-while-alive", "coverage-ended-before-runner-exit"),
                           ("endpoint-gap-3400ms", "coverage-endpoint-gap")):
            with self.subTest(case=case):
                bench, load, observed, why, _endpoint = self.r["dot1"][case]
                self.assertEqual([bench, load, observed], [False, False, None], f"a record that did not cover the window was accepted: {why}")
                for field in ("appAffinityObserved", "partitionLoadGBps", "pcoreMHzMean"):
                    self.assertIn(f"{field}={code}", why)
        self.assertEqual(self.r["dot1"]["full-window-control"][:3], [True, True, "0x00FFF"], self.r["dot1"]["full-window-control"])
        # adversary r9 (5592421d) A2: the published endpoint bound is the observed gap (100 ms here), and 3400 ms is reported, not hidden
        self.assertEqual(self.r["dot1"]["full-window-control"][4], 100, self.r["dot1"])
        self.assertEqual(self.r["dot1"]["endpoint-gap-3400ms"][4], 3400, self.r["dot1"])

    def test_a_sample_taken_after_the_last_poll_that_saw_the_app_alive_is_not_the_apps(self) -> None:
        """Dot 4 (= adversary r8 aeacec83 A4): a sample after the 22.95 s alive poll is excluded (23.5 s, exit seen at 23.9 s); the literal (sample 24 s,
        exit seen 24.2 s) is refused outright by its 1250 ms alive-to-gone gap; the in-window control counts."""
        self.assertEqual(self.r["dot4"]["post-exit-sample"][0], None, self.r["dot4"])
        self.assertIn("pcoreMHzMean=no-app-samples", self.r["dot4"]["post-exit-sample"][1])
        self.assertEqual(self.r["dot4"]["literal-gone-24200"][0], None, self.r["dot4"])
        self.assertIn("pcoreMHzMean=coverage-endpoint-gap", self.r["dot4"]["literal-gone-24200"][1])
        self.assertEqual(self.r["dot4"]["in-window-control"][0], 2000.0, self.r["dot4"])

    def test_a_short_or_torn_feed_read_is_a_typed_not_ready_never_a_row_position(self) -> None:
        """Dot 2 (= adversary r8 aeacec83 A2, the Dot literal): no start temperature from a mark whose text does not reach its length; the r9 reader's
        short read is typed; a complete full-length read still publishes."""
        self.assertEqual(self.r["dot2"]["aeacec83-mark"], [None, "feed-mark-torn"], self.r["dot2"])
        self.assertEqual(self.r["dot2"]["r9-short-read"], [None, "feed-short-read"], self.r["dot2"])
        self.assertEqual(self.r["dot2"]["full-read-control"], [61.0, ""], self.r["dot2"])

    def test_the_feed_readers_describe_only_the_bytes_they_read(self) -> None:
        """Dot 2 siblings (Read-CorePartitionFeedMark / Read-CorePartitionFeedSince): a file truncated between the stat and the read gives a mark of the
        bytes read, flagged shortRead and typed feed-short-read; complete reads account for every byte from beforeOffset to length."""
        rd = self.r["dot2Reader"]
        self.assertNotEqual(rd, "absent", "the readers have no byte-read seam")
        self.assertEqual(rd["short"], [34, 0, True, "feed-short-read", "feed-short-read"], rd)
        self.assertEqual(rd["whole"], [134, 0, False, ""], rd)
        self.assertEqual(rd["big"][0], 6800, rd)
        self.assertEqual(rd["big"][2:], [False, "", 0], f"a >4 KiB mark's text does not start on a row boundary or does not add up: {rd}")
        self.assertEqual(rd["since"][:2], [60, True], rd)
        self.assertEqual(rd["since"][2], "feed-short-read", rd)


# r10 (sol r9 key on 8cf7e883, B1-B4; hub ruling HUB-TICK 20261027T1112Z): each blocker's literal against the generator's own consumers, readers,
# helper body and load class. Each RED case fails on 8cf7e883; each control passes on both.
R10_PURE = r"""
$L = 639286128000000000
$r = [ordered]@{}
$feed = [ordered]@{ reasons = [ordered]@{} }
$w = @{ value = 16; why = $null }
$mp = @{ MeasuredPid = [int64]5000; MeasuredStartTicks = $L + 29000000; MeasuredEndTicks = $L + 230000000 }
# seconds after L: start, first seen, last alive, seen gone (monotonic ms = seconds x 1000; no clock step unless a case says so)
function New-R10Inst([int]$ProcId, $Start, [double]$First, [double]$Last, [double]$Gone, $FirstMs = 'auto') {
    $fm = $(if ($FirstMs -eq 'auto') { [string][int64]($First * 1000) } elseif ($null -eq $FirstMs) { 'null' } else { [string]$FirstMs })
    '{"pid":' + $ProcId + ',"startUtcTicks":' + ($L + [int64]($Start * 1e7)) + ',"firstSeenUtcTicks":' + ($L + [int64]($First * 1e7)) + ',"firstMs":' + $fm + ',"lastSeenUtcTicks":' + ($L + [int64]($Last * 1e7)) +
        ',"goneUtcTicks":' + ($L + [int64]($Gone * 1e7)) + ',"aliveMs":' + [int64]($Last * 1000) + ',"goneMs":' + [int64]($Gone * 1000) + ',"affinityFirst":4095,"affinityLast":4095}'
}
$app = New-R10Inst 5000 3 3.1 22.95 23.05
function New-R10Record([string[]]$Inst, [string]$Load = '{"seconds":19.8,"bytes":39600000000,"joined":true}', [string]$App = $app) {
    ('{"schema":"mlv-app/core-partition-helper/v1","pid":4242,"topologyCheck":{"ok":true,"reason":null,"pMask":4095,"eMask":1044480},"helperAffinityObserved":1044480,' +
        '"app":' + $App + ',"load":' + $Load + ',"samples":[],"instances":[' + ($Inst -join ',') + '],"others":[],"clockSkewMaxMs":0,"endedBy":"stop-file"}') | ConvertFrom-Json
}
# B3 (sol r9 B3, literal): measured 5000 runs real 3..23 s. Between the real 4.9 / 5.1 s polls UTC is advanced 3600 s, PID 6000 starts at real 5 s, and UTC
# is restored, so 6000's start stamp is L+3605 s while the 5.1 s poll first saw it at L+5.1 s; it stays observed until real 10 s. Sampled skew 0.
$r.b3 = [ordered]@{}
$b3 = [ordered]@{
    'utc-step-hides-overlap' = @($app, (New-R10Inst 6000 3605 5.1 9.9 10))
    # wall stamps all consistent and after the app, but the monotonic first sight and last alive poll say 6000 was observed while the app was alive
    'monotonic-concurrent-wall-disjoint' = @($app, (New-R10Inst 6000 24 24.1 29.9 30 5100).Replace('"aliveMs":29900', '"aliveMs":9900').Replace('"goneMs":30000', '"goneMs":10000'))
    'first-sight-unrecorded' = @($app, (New-R10Inst 6000 0.5 0.6 1.9 2 $null))
    'no-step-no-overlap-control' = @($app, (New-R10Inst 6000 0.5 0.6 1.9 2))
}
foreach ($k in $b3.Keys) {
    $s = Get-CorePartitionScalars (New-R10Record $b3[$k]) $feed 4095 1044480 8 $w $null $false @mp
    $r.b3[$k] = @($s.benchAffinityApplied, $s.partitionLoadApplied, [string]$s.corePartitionNullReasons)
}
# B4 (sol r9 B4): the measured app's load session as the helper's Halt() reported it
$r.b4 = [ordered]@{}
foreach ($k in @('not-joined', 'join-unrecorded', 'joined-control')) {
    $ld = switch ($k) { 'not-joined' { '{"seconds":19.8,"bytes":39600000000,"joined":false}' } 'join-unrecorded' { '{"seconds":19.8,"bytes":39600000000}' } default { '{"seconds":19.8,"bytes":39600000000,"joined":true}' } }
    $s = Get-CorePartitionScalars (New-R10Record @($app) $ld) $feed 4095 1044480 8 $w $null $false @mp
    $r.b4[$k] = @($s.benchAffinityApplied, $s.partitionLoadApplied, $s.partitionLoadGBps, [string]$s.corePartitionNullReasons)
}
# B2 (sol r9 B2, literal): same file; start mark read at mono 1000; the smoke ends at 21000; the end reader stamps 21000 but its Length and read are
# delayed until 141000, and a 99 W / 99 C row appended at 140000 is in its window. Variants: the window row appended at 2000 (139 s old at the read);
# a 40 s delay (inside the 120 s staleness limit). Control: the read completes 500 ms after the smoke end. r11: the helper saw the app alive at 20900
# (a pre-r11 generator ignores that argument).
$row = "26.10.2026,12:02:20,x,99,99,Yes,Yes`n"
$s2 = @{ exists = $true; length = 34; beforeOffset = 0; shortRead = $false; monoMs = [int64]1000; doneMs = [int64]1000; created = 123; fileId = 'same'; before = "26.10.2026,12:00:00,x,30,10,No,No`n"; window = ''; windowStartsMidRow = $false }
$b2b = @{ exists = $true; length = 0; monoMs = [int64]500; doneMs = [int64]500; created = 123; fileId = 'same' }
function New-R10End([int64]$Done, [string]$Window = $row) { @{ exists = $true; length = 34 + $Window.Length; monoMs = [int64]21000; doneMs = $Done; created = 123; fileId = 'same'; before = ''; window = $Window; windowStartsMidRow = $false; replaced = $false; truncated = $false; shortRead = $false } }
$r.b2 = [ordered]@{}
$b2 = [ordered]@{
    'read-delayed-to-141000' = (New-R10End 141000)
    'stale-row-read-at-141000' = (New-R10End 141000 "26.10.2026,12:00:01,x,99,99,Yes,Yes`n")
    'read-delayed-40s' = (New-R10End 61000)
    'in-window-control' = (New-R10End 21500 "26.10.2026,12:00:10,x,61,40,No,No`n")
}
foreach ($k in $b2.Keys) {
    $x = Get-CorePartitionFeedSummary $s2 $b2[$k] 120 $b2b 21000 2000 20900
    $r.b2[$k] = @($x.rows, $x.pkgWattsMean, $x.pkgTempCMax, [string]$x.reasons['rows'])
}
# the start mark's freshness runs to its read's completion too: stamped at 2000 (row appended after the pre-helper read), completed at 200000
$late = $s2.Clone(); $late.monoMs = [int64]2000; $late.doneMs = [int64]200000; $late.length = 34; $late.beforeOffset = 0
$lb = @{ exists = $true; length = 0; monoMs = [int64]1000; created = 123; fileId = 'same' }
$x = Get-CorePartitionFeedSummary $late (New-R10End 21500 "26.10.2026,12:00:10,x,61,40,No,No`n") 120 $lb 21000 2000 20900
$ok = $late.Clone(); $ok.doneMs = [int64]2100
$y = Get-CorePartitionFeedSummary $ok (New-R10End 21500 "26.10.2026,12:00:10,x,61,40,No,No`n") 120 $lb 21000 2000 20900
$r.b2Start = @($x.pkgTempCAtStart, [string]$x.reasons['pkgTempCAtStart'], $y.pkgTempCAtStart, [string]$y.reasons['pkgTempCAtStart'])
# the readers: a read delayed 1500 ms after its stamp (through the byte-read seam) carries a completion time at least 1500 ms after the stamp
$r.b2Reader = 'absent'
$fp = Join-Path $env:CP_TMP 'r10-feed.csv'
[IO.File]::WriteAllText($fp, "26.10.2026,12:00:00,x,30,10,No,No`n26.10.2026,12:00:01,x,31,11,No,No`n")
$impl = ${function:Read-CorePartitionFeedBytes}
function Read-CorePartitionFeedBytes($Stream, $Buffer, $Count) { Start-Sleep -Milliseconds 1500; & $impl $Stream $Buffer $Count }
$m1 = Read-CorePartitionFeedMark $fp
$m2 = Read-CorePartitionFeedSince $fp 34 "26.10.2026,12:00:00,x,30,10,No,No`n"
$r.b2Reader = @(@($m1.ContainsKey('doneMs'), ($m1.doneMs - $m1.monoMs)), @($m2.ContainsKey('doneMs'), ($m2.doneMs - $m2.monoMs)))
$r | ConvertTo-Json -Depth 6 -Compress
"""

# The helper body and its three spliced functions, lifted from the generator by AST (the job's own source; no job is generated).
HELPER_SOURCE = r"""
$tk = $null; $er = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:CP_GEN, [ref]$tk, [ref]$er)
$fn = @{}; foreach ($f in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) { $fn[$f.Name] = $f.Extent.Text }
$hb = ($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and $n.Left.Extent.Text -eq '$corePartitionHelperBody' }, $true) | Select-Object -First 1).Right.Expression.Value
"""

# r10 B1 (sol r9 B1), the real helper body with a process double for the measured app (never MLVApp) and a stub counter category (the counters are not
# under test; a real read can take hundreds of ms inside a poll). 2.5 s after its first sighting the double's affinity read, which the helper makes after
# the liveness query that saw it alive, either returns at once (tight: the control) or stalls 1500 ms (delayed: a descheduled helper); the app exits
# 100 ms into that read. Its exit, once a poll observes it, writes the measured pid and the stop file, as the job does after the runner.
B1_DRIVER = HELPER_SOURCE + r"""
$cat = "[Diagnostics.PerformanceCounterCategory]::new('Processor Information')"
if (-not $hb.Contains($cat)) { throw 'the helper no longer reads the Processor Information category (the stub anchor)' }
$hb = $hb.Replace($cat, '$cpFakeCat')
$prelude = @'
$cpFakeCat = [pscustomobject]@{}; $cpFakeCat | Add-Member -MemberType ScriptMethod -Name ReadCategory -Value { @{} }
$global:cpF = @{ sw = [Diagnostics.Stopwatch]::StartNew(); firstAt = $null; exitAt = [long]::MaxValue; stalled = $false; stopped = $false; start = [DateTime]::UtcNow }
$global:cpP = [pscustomobject]@{ Id = 5000; Path = $env:MLV_CP_EXE; StartTime = $global:cpF.start; PriorityClass = 'Normal' }
$global:cpP | Add-Member -MemberType ScriptMethod -Name Refresh -Value { }
$global:cpP | Add-Member -MemberType ScriptProperty -Name HasExited -Value {
    $e = $global:cpF.sw.ElapsedMilliseconds -ge $global:cpF.exitAt
    if ($e -and -not $global:cpF.stopped) { $global:cpF.stopped = $true; [IO.File]::WriteAllText($env:MLV_CP_MEASURED, '5000'); [IO.File]::WriteAllText($env:MLV_CP_STOP, 'stop') }
    $e }
$global:cpP | Add-Member -MemberType ScriptProperty -Name ProcessorAffinity -Value {
    $t = $global:cpF.sw.ElapsedMilliseconds
    if ($null -eq $global:cpF.firstAt) { $global:cpF.firstAt = $t }
    elseif (-not $global:cpF.stalled -and $t -ge $global:cpF.firstAt + 2500) { $global:cpF.stalled = $true; $global:cpF.exitAt = $t + 100; if ($env:CP_MODE -eq 'delayed') { Start-Sleep -Milliseconds 1500 } }
    [int64]4095 }
function Get-Process { [CmdletBinding()] param([string]$Name) if (-not $global:cpF.stopped -and $global:cpF.sw.ElapsedMilliseconds -lt $global:cpF.exitAt) { $global:cpP } }
'@
$src = $prelude + "`n" + ((@('Format-CorePartitionMask', 'Get-CorePartitionTopology', 'Test-CorePartitionMaskClass') | ForEach-Object { $fn[$_] }) -join "`n")
$src = $src + "`n" + $hb
$out = [ordered]@{}
foreach ($mode in @('delayed', 'tight')) {
    $dir = Join-Path $env:CP_DIR $mode
    [void](New-Item -ItemType Directory -Path $dir)
    $hp = Join-Path $dir 'helper.ps1'; [IO.File]::WriteAllText($hp, $src)
    $launch = [DateTime]::UtcNow.AddSeconds(-1).Ticks
    [IO.File]::WriteAllText((Join-Path $dir 'launch'), [string]$launch)
    $psi = [Diagnostics.ProcessStartInfo]::new("$env:ProgramFiles\PowerShell\7\pwsh.exe", "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$hp`"")
    $psi.UseShellExecute = $false; $psi.CreateNoWindow = $true
    $psi.Environment['CP_MODE'] = $mode
    $psi.Environment['MLV_CP_EXE'] = Join-Path $dir 'mlvfake.exe'
    $psi.Environment['MLV_CP_STOP'] = Join-Path $dir 'stop'
    $psi.Environment['MLV_CP_READY'] = Join-Path $dir 'ready'
    $psi.Environment['MLV_CP_OUT'] = Join-Path $dir 'helper.json'
    $psi.Environment['MLV_CP_LAUNCH'] = Join-Path $dir 'launch'
    $psi.Environment['MLV_CP_MEASURED'] = Join-Path $dir 'measured'
    $psi.Environment['MLV_CP_BENCH'] = '4095'; $psi.Environment['MLV_CP_LOAD'] = '0'; $psi.Environment['MLV_CP_HELPER'] = '1'; $psi.Environment['MLV_CP_THREADS'] = '1'
    $psi.Environment['MLV_CP_NONCE'] = 'r10-b1'; $psi.Environment['MLV_CP_DEADLINE_MS'] = '60000'
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $h = [Diagnostics.Process]::Start($psi)
    if (-not $h.WaitForExit(90000)) { $h.Kill(); throw "the $mode helper did not end" }
    $end = [DateTime]::UtcNow.Ticks
    $rec = [IO.File]::ReadAllText((Join-Path $dir 'helper.json')) | ConvertFrom-Json
    $i = @($rec.instances | Where-Object { $_.pid -eq 5000 })[0]
    $start = [int64]$i.startUtcTicks
    $s = Get-CorePartitionScalars $rec ([ordered]@{ reasons = [ordered]@{} }) 4095 ([int64]$rec.helperAffinityObserved) 1 @{ value = 1; why = $null } $null $false -MeasuredPid 5000 -MeasuredStartTicks ($start - 5000000) -MeasuredEndTicks $end
    $v = Test-CorePartitionMeasuredApp $rec 5000 ($start - 5000000) $end 0
    $out[$mode] = [ordered]@{ endedBy = $rec.endedBy; aliveMs = $i.aliveMs; goneMs = $i.goneMs; endpointMs = $v.endpointMs; bench = $s.benchAffinityApplied; load = $s.partitionLoadApplied; why = [string]$s.corePartitionNullReasons; exit = $h.ExitCode }
}
$out | ConvertTo-Json -Compress -Depth 4
"""

# r10 B4 (sol r9 B4), the helper's own load class (lifted from the helper body), compiled with a test hold: the hold marks the first loop pass of the
# first worker of session A and keeps that worker parked past Halt()'s 5 s Join (a descheduled worker); session B (8 workers, 32 MiB) then starts and
# the parked worker is released inside it. Every loop pass is counted per thread, so B's bytes can be checked against B's own workers' passes.
B4_DRIVER = HELPER_SOURCE + r"""
$m = [regex]::Match($hb, '(?s)Add-Type -TypeDefinition @"\r?\n(.*?)\r?\n"@')
if (-not $m.Success) { throw 'no load class in the helper body' }
$cs = $m.Groups[1].Value
$loop = [regex]::Matches($cs, 'while \(!(\w+)\) \{')
if ($loop.Count -ne 1) { throw "the worker loop is not one 'while (!flag) {' ($($loop.Count))" }
$cs = $cs.Substring(0, $loop[0].Index + $loop[0].Length) + ' CpTestHold.Pass();' + $cs.Substring($loop[0].Index + $loop[0].Length)
$cs += @'

public static class CpTestHold {
    public static readonly System.Threading.ManualResetEventSlim Gate = new System.Threading.ManualResetEventSlim(true);
    public static volatile bool HoldNext; public static volatile int HeldId = -1; public static volatile bool Counting;
    public static readonly System.Collections.Concurrent.ConcurrentDictionary<int, long> Passes = new System.Collections.Concurrent.ConcurrentDictionary<int, long>();
    static readonly object gate = new object();
    public static void Pass() {
        int id = System.Threading.Thread.CurrentThread.ManagedThreadId;
        if (HoldNext) { lock (gate) { if (HoldNext) { HoldNext = false; HeldId = id; Gate.Reset(); } } }
        if (id == HeldId) { Gate.Wait(); }
        if (Counting) { Passes.AddOrUpdate(id, 1, (k, v) => v + 1); }
    }
}
'@
Add-Type -TypeDefinition $cs
$static = $null -ne [MlvCorePartitionLoad].GetMethod('Begin')
$res = [ordered]@{ api = $(if ($static) { 'static' } else { 'session' }) }
try {
    [CpTestHold]::HoldNext = $true
    if ($static) { [MlvCorePartitionLoad]::Begin(1, 1) } else { $A = [MlvCorePartitionLoad]::new(1, 1) }
    $sw = [Diagnostics.Stopwatch]::StartNew(); while ([CpTestHold]::HeldId -lt 0 -and $sw.ElapsedMilliseconds -lt 10000) { Start-Sleep -Milliseconds 20 }
    $held = [CpTestHold]::HeldId
    $sw.Restart()
    if ($static) { [MlvCorePartitionLoad]::End(); $res.aJoined = $null } else { $res.aJoined = $A.Halt() }
    $res.aEndMs = $sw.ElapsedMilliseconds
    $b0 = $(if ($static) { [MlvCorePartitionLoad]::Bytes } else { 0 })
    [CpTestHold]::Counting = $true
    if ($static) { [MlvCorePartitionLoad]::Begin(8, 32) } else { $B = [MlvCorePartitionLoad]::new(8, 32) }
    Start-Sleep -Milliseconds 300
    [CpTestHold]::Gate.Set()
    Start-Sleep -Milliseconds 700
    if ($static) { [MlvCorePartitionLoad]::End(); $res.bJoined = $null; $bBytes = [MlvCorePartitionLoad]::Bytes - $b0 } else { $res.bJoined = $B.Halt(); $bBytes = $B.Bytes }
    [CpTestHold]::Counting = $false
    $own = @([CpTestHold]::Passes.Keys | Where-Object { $_ -ne $held })
    $ownPasses = [int64]0; foreach ($k in $own) { $ownPasses += [CpTestHold]::Passes[$k] }
    $res.heldWorkerPassesInB = $(if ([CpTestHold]::Passes.ContainsKey($held)) { [CpTestHold]::Passes[$held] } else { 0 })
    $res.bWorkers = $own.Count
    $res.bBytes = $bBytes
    $res.bOwnWorkersBytes = $ownPasses * 32 * 1048576
} finally { [CpTestHold]::Gate.Set() }
$res | ConvertTo-Json -Compress
"""


@requires_windows_pwsh
class CorePartitionR10Tests(unittest.TestCase):
    """r10 (sol r9 B1-B4 on 8cf7e883): the literals against the generator's own consumers, readers, helper body and load class."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="core-partition-r10-")
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.tmp = Path(cls._tmp.name)
        script = cls.tmp / "r10.ps1"
        script.write_text(LIFT + R10_PURE, encoding="utf-8")
        proc = run_pwsh(["-File", str(script)], env_extra={"CP_GEN": str(GENERATOR), "CP_TMP": cls._tmp.name})
        cls.raw = proc
        cls.r = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.returncode == 0 and proc.stdout.strip() else None

    def setUp(self) -> None:
        self.assertIsNotNone(self.r, f"the lifted helpers did not run: {self.raw.stdout[-3000:]}{self.raw.stderr[-3000:]}")

    def drive(self, name: str, body: str, lift: bool) -> dict:
        work = self.tmp / name
        work.mkdir()
        driver = self.tmp / f"{name}.ps1"
        driver.write_text((LIFT if lift else "") + body, encoding="utf-8")
        proc = run_pwsh(["-File", str(driver)], env_extra={"CP_GEN": str(GENERATOR), "CP_DIR": str(work)})
        self.assertEqual(proc.returncode, 0, proc.stdout[-3000:] + proc.stderr[-3000:])
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_b1_a_helper_descheduled_after_its_alive_query_reports_the_real_observation_gap(self) -> None:
        """B1 (sol r9 B1 = adversary r9 8cf7e883 A1), the real helper body: the alive stamp is taken before the liveness query, so a helper stalled
        1500 ms after the query that saw the app alive (the app exiting 100 ms into the stall) reports the whole alive-to-gone gap and the run is
        refused coverage-endpoint-gap; on 8cf7e883 the late stamp shrinks the gap to one poll and bench and load are accepted. Control: a tight poll."""
        got = self.drive("b1", B1_DRIVER, True)
        delayed, tight = got["delayed"], got["tight"]
        for case in (delayed, tight):
            self.assertEqual(case["endedBy"], "stop-file", got)
            self.assertEqual(case["exit"], 0, got)
        self.assertGreater(delayed["goneMs"] - delayed["aliveMs"], 1000, f"the alive stamp hid the stall: {delayed}")
        self.assertEqual([delayed["bench"], delayed["load"]], [False, False], f"a stalled helper's understated endpoint was accepted: {delayed}")
        self.assertIn("appAffinityObserved=coverage-endpoint-gap", delayed["why"])
        self.assertIn("partitionLoadGBps=coverage-endpoint-gap", delayed["why"])
        self.assertLessEqual(tight["endpointMs"], 1000, tight)
        self.assertEqual([tight["bench"], tight["load"]], [True, True], f"a tight-poll exit was refused: {tight}")

    def test_b2_a_feed_read_completed_after_the_window_publishes_no_row(self) -> None:
        """B2 (sol r9 B2 = adversary r9 8cf7e883 A2, literal): an end read that completed 120 s (or 40 s) after the smoke end may hold rows written
        after the run, so none is published (feed-read-after-window), for a post-run 99 W row and for a stale one; the in-window control publishes."""
        for case in ("read-delayed-to-141000", "stale-row-read-at-141000", "read-delayed-40s"):
            with self.subTest(case=case):
                self.assertEqual(self.r["b2"][case], [None, None, None, "feed-read-after-window"], self.r["b2"])
        self.assertEqual(self.r["b2"]["in-window-control"], [1, 40.0, 61.0, ""], self.r["b2"])
        # the start mark's freshness span ends at its read's completion: 199 s is past the 120 s limit, 1.1 s is not
        self.assertEqual(self.r["b2Start"], [None, "feed-freshness-unverified", 30.0, ""], self.r["b2Start"])

    def test_b2_the_feed_readers_stamp_the_completion_of_their_read(self) -> None:
        """B2 siblings (Read-CorePartitionFeedMark / Read-CorePartitionFeedSince): a read delayed 1500 ms after its stamp carries doneMs >= 1500 ms
        after monoMs."""
        rd = self.r["b2Reader"]
        for mark in rd:
            self.assertTrue(mark[0], f"a feed mark has no completion time: {rd}")
            self.assertGreaterEqual(mark[1], 1500, rd)

    def test_b3_a_process_chronology_that_cannot_be_ordered_refuses_attribution(self) -> None:
        """B3 (sol r9 B3 = adversary r9 8cf7e883 A3, literal): a UTC step between two polls stamps PID 6000's start after its own first sighting, so
        the wall-clock overlap test misses it; the run is refused (process-chronology-inconsistent). A process the monotonic clock proves alive beside
        the app is an overlap whatever its wall stamps say; a record without first-sight times proves no order. Control: no step, no overlap."""
        expected = {"utc-step-hides-overlap": "process-chronology-inconsistent", "monotonic-concurrent-wall-disjoint": "same-path-instance-overlap",
                    "first-sight-unrecorded": "process-chronology-unverified"}
        for case, code in expected.items():
            with self.subTest(case=case):
                bench, load, why = self.r["b3"][case]
                self.assertEqual([bench, load], [False, False], f"{case}: {why}")
                self.assertIn(f"appAffinityObserved={code}", why)
        self.assertEqual(self.r["b3"]["no-step-no-overlap-control"][:2], [True, True], self.r["b3"])

    def test_b4_a_load_session_whose_worker_did_not_join_is_refused(self) -> None:
        """B4 (sol r9 B4): the measured app's load session counts only when Halt() joined every worker; not joined is load-worker-not-joined, an
        unrecorded outcome load-join-unverified; the joined control is accepted at 2.0 GB/s."""
        for case, code in (("not-joined", "load-worker-not-joined"), ("join-unrecorded", "load-join-unverified")):
            with self.subTest(case=case):
                bench, load, gbps, why = self.r["b4"][case]
                self.assertEqual([bench, load, gbps], [True, False, None], f"{case}: {why}")
                self.assertIn(f"partitionLoadGBps={code}", why)
        self.assertEqual(self.r["b4"]["joined-control"][:3], [True, True, 2.0], self.r["b4"])

    def test_b4_a_worker_held_past_its_join_never_enters_the_next_load_session(self) -> None:
        """B4 (sol r9 B4), the helper's own load class: a session-A worker parked past Halt()'s Join(5000) is released inside session B (8 workers,
        32 MiB). Halt() reports A as not joined; the released worker finishes its one pass and stops (it reads A's stop flag); B's bytes are exactly its
        own 8 workers' passes. On 8cf7e883 Begin resets the shared flag, so the old worker keeps running in B and its bytes are counted as B's."""
        got = self.drive("b4", B4_DRIVER, False)
        self.assertEqual(got["api"], "session", f"the load class is not one object per session: {got}")
        self.assertGreaterEqual(got["aEndMs"], 4500, f"the held worker was not held past Join: {got}")
        self.assertIs(got["aJoined"], False, got)
        self.assertIs(got["bJoined"], True, got)
        self.assertEqual(got["bWorkers"], 8, got)
        self.assertLessEqual(got["heldWorkerPassesInB"], 1, f"the old worker kept running inside session B: {got}")
        self.assertEqual(got["bBytes"], got["bOwnWorkersBytes"], f"session B's bytes are not its own workers': {got}")


# r11 B1 (sol r10 B1 = key3 r10 on d5233ef9, CP-FEED-END-ANCHOR): the job's own cleanup region (the body of the generator's $cpPost, lifted by AST; no
# job is generated, so the same driver runs against any generator) on a fake monotonic clock. The smoke call returned at mono 21000; 'entry' later
# than that is a stall before the region's first statement. The job's affinity restore stalls 'restore' ms (its setter advances the clock). The feed's
# 99 W / 99 C row was appended just before the end read, which completes 500 ms after it starts. The helper's signed record (accepted: this job's key,
# its launch pid, exit 0) puts the measured app's last alive poll at 'alive' and the poll that saw it gone at 'gone' on the job's monotonic base;
# 'gone' $null is a record without that base. adversary r11 (5f75e044) A1: a helper descheduled 40 s after the app exited sees it gone late.
# The wall clock runs with the fake monotonic one (the end stamp's wall read is "now"), so the measured-app verdict sees consistent clocks; the
# runner launched pid 5000 at mono 1500 and saw it exit at 20850; the recorded instance with that pid started at 'start' (adversary r11 4078c9a7 A1:
# a later instance reusing the pid, started after the runner's exit).
R11_CLEANUP = r"""
$asg = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and $n.Left.Extent.Text -eq '$cpPost' }, $true) | Select-Object -First 1
if ($null -eq $asg) { throw 'the generator has no $cpPost cleanup region' }
$post = @($asg.Right.FindAll({ param($n) $n -is [System.Management.Automation.Language.StringConstantExpressionAst] -and $n.StringConstantType -eq 'SingleQuotedHereString' }, $true))[0].Value
function Write-JobTrace([string]$Message) { }
function Publish-AttrCudaText { param([string]$Path, [string]$Value) }
function New-AttrCudaDirectory { param([string]$Path) }
function Save-Json { param($Object, $Path) $global:cpSaved = $Object }
function Get-CorePartitionMonoMs { [double]$global:cpClk }
# r13 (CP-FEED-START-ANCHOR): the run log the result names; the R13 driver sets $global:cpLog per case ($null: the resolver throws, as the real one
# does for a result without a verified log)
# r13b (A2): like the real one, it returns the path and the sha256 it checked; $global:cpSwap, when set, is written over the file AFTER that
# check (a same-length rewrite between the resolver's check and the caller's read of .path)
function Resolve-AttrCudaSmokeRunLog { param($ResultJsonPath, $ContainingRoot) if (-not $global:cpLog) { throw 'run log unresolvable' }; $h = (Get-FileHash -LiteralPath $global:cpLog -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($null -ne $global:cpSwap) { [IO.File]::WriteAllText($global:cpLog, $global:cpSwap) }
    [pscustomobject]@{ path = $global:cpLog; sha256 = $h } }
$row = "26.10.2026,12:01:00,x,99,99,Yes,Yes`n"
function Read-CorePartitionFeedSince($Path, $Offset, $Before) {
    $m = $global:cpClk; $global:cpClk += 500
    @{ exists = $true; length = 34 + $row.Length; monoMs = [int64]$m; doneMs = [int64]$global:cpClk; created = 123; fileId = 'same'; before = ''; window = $row; windowStartsMidRow = $false
        replaced = $false; truncated = $false; shortRead = $false; readUtcTicks = 0; lastWriteUtcTicks = 0 }
}
$cases = [ordered]@{
    'zero-delay-control' = @(21000, 0, 20900, 20750, 2000)
    'restore-stall-40s' = @(21000, 40000, 20900, 20750, 2000)
    'restore-stall-80s' = @(21000, 80000, 20900, 20750, 2000)
    'stall-before-the-end-stamp-40s' = @(61000, 0, 20900, 20750, 2000)
    'no-observed-exit' = @(21000, 0, $null, 20750, 2000)
    'helper-saw-the-exit-40s-late' = @(61000, 0, 61000, 20800, 2000)
    'a-later-instance-with-the-measured-pid' = @(61000, 0, 60200, 60000, 22000)
}
$u8 = [Text.Encoding]::UTF8
$out = [ordered]@{}
foreach ($k in $cases.Keys) {
  try {
    $c = $cases[$k]; $dir = Join-Path $env:CP_DIR $k; [void](New-Item -ItemType Directory -Path $dir)
    $global:cpClk = [double]$c[0]; $global:cpDelay = [double]$c[1]
    $corePartitionJob = [ordered]@{ affinityRequested = '0x00FFF'; affinityApplied = '0x00FFF'; affinityBefore = '0xFFFFF'; affinityRestored = $null }
    $corePartitionJobProc = [pscustomobject]@{}
    $corePartitionJobProc | Add-Member -MemberType ScriptProperty -Name ProcessorAffinity -Value { [int64]1048575 } -SecondValue { $global:cpClk += $global:cpDelay }
    $corePartitionJobProc | Add-Member -MemberType ScriptMethod -Name Refresh -Value { }
    $now = [DateTime]::UtcNow
    $L = $now.AddMilliseconds(1000 - $c[0])
    $W = { param([double]$m) $L.AddMilliseconds($m - 1000).Ticks }
    # r13c (CP-FEED-START-ANCHOR, rule (a)): a feed row's HWiNFO stamp (local d.M.yyyy,H:mm:ss.fff) for monotonic $m; the window row is stamped
    # at 2000, after the measured session's playback start (1500)
    $H = { param([double]$m) [DateTime]::new((& $W $m), [DateTimeKind]::Utc).ToLocalTime().ToString('d.M.yyyy,H:mm:ss.fff', [Globalization.CultureInfo]::InvariantCulture) }
    $row = (& $H 2000) + ",x,99,99,Yes,Yes`n"
    $corePartitionMeasuredLaunchUtc = $L; $corePartitionPreHelperUtc = $L.AddSeconds(-1)
    $corePartitionClockPairs = [Collections.Generic.List[object]]::new(); $corePartitionClockPairs.Add(@($corePartitionMeasuredLaunchUtc.Ticks, [double]1000))
    $corePartitionMeasuredLaunchMonoMs = [double]1000
    # r13: the measured session's playback starts 500 ms after the start mark, inside the start slack, so the start bound passes these cases
    # r13b (A1): the session's summary at 20000, its stamp less the start's equal to its monotonic elapsed_ms
    $global:cpLog = Join-Path $dir 'run.log'; $global:cpSwap = $null
    $S = { param([double]$m) [DateTime]::new((& $W $m), [DateTimeKind]::Utc).ToString('yyyy-MM-ddTHH:mm:ss.fffZ') }
    [IO.File]::WriteAllText($global:cpLog, '[' + (& $S 1500) + "] [info] [0x1] playback_smoke.start session=1 position=0`n[" + (& $S 1600) + "] [info] [0x1] playback_smoke.measured_session id=1`n[" + (& $S 20000) + "] [info] [0x1] playback_smoke.summary session=1 reason=play-stop elapsed_ms=18500.000 presented_frames=1`n")
    $corePartitionFeedPath = Join-Path $dir 'feed.csv'
    $corePartitionFeedPreHelper = @{ exists = $true; length = 0; beforeOffset = 0; before = ''; shortRead = $false; monoMs = [int64]500; doneMs = [int64]500; created = 123; fileId = 'same'; readUtcTicks = 0; lastWriteUtcTicks = 0 }
    $corePartitionFeedStart = @{ exists = $true; length = 34; beforeOffset = 0; shortRead = $false; monoMs = [int64]1000; doneMs = [int64]1000; created = 123; fileId = 'same'
        before = "26.10.2026,12:00:00,x,30,10,No,No`n"; window = ''; windowStartsMidRow = $false; readUtcTicks = 0; lastWriteUtcTicks = 0 }
    $resultPath = Join-Path $dir 'result.json'
    [IO.File]::WriteAllText($resultPath, ([ordered]@{ process = [ordered]@{ id = 5000; startedAtUtc = [DateTime]::new((& $W 1500), [DateTimeKind]::Utc).ToString('o'); endedAtUtc = [DateTime]::new((& $W 20850), [DateTimeKind]::Utc).ToString('o') } } | ConvertTo-Json -Depth 3))
    $corePartitionHelper = [pscustomobject]@{ HasExited = $true; ExitCode = 0 }
    $corePartitionHelper | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value { $true }
    $corePartitionHelper | Add-Member -MemberType ScriptMethod -Name Kill -Value { }
    $corePartitionHelperLaunch = [ordered]@{ pid = 4242; ready = $true }
    $corePartitionHelperNonce = 'r11-end-anchor'; $corePartitionHelperOut = Join-Path $dir 'helper.json'
    $corePartitionMeasuredFile = Join-Path $dir 'measured'; $corePartitionStopFile = Join-Path $dir 'stop'
    $mb = $(if ($null -ne $c[2]) { ',"monoBaseMs":800' } else { '' })
    $gone = $(if ($null -ne $c[2]) { $c[2] } else { 20900 })
    $inst = '{"pid":5000,"startUtcTicks":' + (& $W $c[4]) + ',"firstSeenUtcTicks":' + (& $W ($c[4] + 100)) + ',"firstMs":' + ($c[4] + 100 - 800) + ',"aliveMs":' + ($c[3] - 800) + ',"goneUtcTicks":' + (& $W $gone) + ',"goneMs":' + ($gone - 800) + '}'
    $rb = $u8.GetBytes('{"schema":"mlv-app/core-partition-helper/v1","pid":4242' + $mb + ',"app":' + $inst + ',"instances":[' + $inst + '],"others":[],"clockSkewMaxMs":0}')
    [IO.File]::WriteAllBytes($corePartitionHelperOut, $rb)
    [IO.File]::WriteAllText("$corePartitionHelperOut.mac", [Convert]::ToHexString([Security.Cryptography.HMACSHA256]::new($u8.GetBytes($corePartitionHelperNonce)).ComputeHash($rb)))
    $corePartitionHelperNotReady = $false; $corePartitionHelperWhy = $null; $corePartitionHelperExit = $null
    $corePartitionErrors = [Collections.Generic.List[string]]::new()
    $Pub = $dir; $CorePartitionBenchMask = [int64]4095; $CorePartitionLoadMask = [int64]1044480; $CorePartitionLoadThreads = 8; $CorePartitionHelperMask = [int64]1044480
    $global:cpSaved = $null
    . ([scriptblock]::Create($post))
    $f = $corePartitionFeedSummary; $pf = $global:cpSaved.feed
    $out[$k] = [ordered]@{ end = $corePartitionSmokeEndMonoMs; endRead = @($corePartitionFeedEnd.monoMs, $corePartitionFeedEnd.doneMs); rows = $f.rows; watts = $f.pkgWattsMax; temp = $f.pkgTempCMax
        why = [string]$f.reasons['rows']; helperWhy = [string]$corePartitionHelperWhy; published = @($pf.windowEndMonoMs, $pf.endReadSlackMs, $pf.appLastAliveMonoMs, $pf.windowEndLagMs, $pf.endLagSlackMs) }
  } catch { $out[$k] = [ordered]@{ threw = [string]$_.Exception.Message + ' @ ' + [string]$_.InvocationInfo.PositionMessage } }
}
$out | ConvertTo-Json -Compress -Depth 5
"""


def _r11_helper_base_driver() -> str:
    """B1_DRIVER's real helper body and process double, tight poll only, reporting the helper's monoBaseMs beside the driver's own reads of the
    shared monotonic clock before the helper starts (t0) and after it exits (t1)."""
    d = B1_DRIVER
    for old, new in (("foreach ($mode in @('delayed', 'tight')) {", "foreach ($mode in @('tight')) {"),
                     ("    $sw = [Diagnostics.Stopwatch]::StartNew()\n    $h = [Diagnostics.Process]::Start($psi)\n",
                      "    $t0 = [Diagnostics.Stopwatch]::GetTimestamp() / [double][Diagnostics.Stopwatch]::Frequency * 1000\n"
                      "    $sw = [Diagnostics.Stopwatch]::StartNew()\n    $h = [Diagnostics.Process]::Start($psi)\n"),
                     ("    $end = [DateTime]::UtcNow.Ticks\n",
                      "    $end = [DateTime]::UtcNow.Ticks\n    $t1 = [Diagnostics.Stopwatch]::GetTimestamp() / [double][Diagnostics.Stopwatch]::Frequency * 1000\n"),
                     ("    $out[$mode] = [ordered]@{ endedBy = $rec.endedBy;", "    $out[$mode] = [ordered]@{ t0 = $t0; t1 = $t1; monoBaseMs = $rec.monoBaseMs; endedBy = $rec.endedBy;")):
        if d.count(old) != 1:
            raise AssertionError(f"B1_DRIVER changed shape near {old!r}")
        d = d.replace(old, new)
    return d


R11_HELPER_BASE_DRIVER = _r11_helper_base_driver()


@requires_windows_pwsh
class CorePartitionR11Tests(unittest.TestCase):
    """r11 (sol r10 B1 = key3 r10 on d5233ef9, CP-FEED-END-ANCHOR): the feed window's end is bound independently of the job's cleanup."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="core-partition-r11-")
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.tmp = Path(cls._tmp.name)
        work = cls.tmp / "cleanup"
        work.mkdir()
        script = cls.tmp / "r11-cleanup.ps1"
        script.write_text(LIFT + R11_CLEANUP, encoding="utf-8")
        proc = run_pwsh(["-File", str(script)], env_extra={"CP_GEN": str(GENERATOR), "CP_DIR": str(work)})
        cls.raw = proc
        cls.r = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.returncode == 0 and proc.stdout.strip() else None

    def setUp(self) -> None:
        self.assertIsNotNone(self.r, f"the lifted cleanup region did not run: {self.raw.stdout[-3000:]}{self.raw.stderr[-3000:]}")
        for case, got in self.r.items():
            self.assertNotIn("threw", got, f"{case}: {got}")

    def test_b1_a_stall_in_cleanup_after_the_smoke_returns_does_not_move_the_window_end(self) -> None:
        """B1 (sol r10 B1, literal): the smoke ends at 21000 and the job's affinity restore stalls 40 s (or 80 s); the 99 W / 99 C row appended
        after the run is in the end read, which completes 500 ms after the restore. On d5233ef9 the end is stamped after the restore (61000 or
        101000), the read passes the 2000 ms slack and the row is published (rows 1, 99 W, 99 C, no reason). The end is now stamped as the smoke
        call returns (21000), so the late read is feed-read-after-window. Control: no stall, the in-window row is published."""
        for case in ("restore-stall-40s", "restore-stall-80s"):
            with self.subTest(case=case):
                got = self.r[case]
                self.assertEqual([got["rows"], got["watts"], got["temp"], got["why"]], [None, None, None, "feed-read-after-window"], got)
                self.assertEqual(got["end"], 21000, f"a cleanup stall moved the window end: {got}")
        control = self.r["zero-delay-control"]
        self.assertEqual([control["rows"], control["watts"], control["temp"], control["why"]], [1, 99.0, 99.0, ""], control)
        self.assertEqual(control["end"], 21000, control)

    def test_b1_a_window_end_that_lags_the_apps_observed_exit_is_refused(self) -> None:
        """B1 (cross-check): a stall before the end stamp itself (the job resumes 40 s after the smoke returned) moves the end and the read
        together, so only independent evidence can catch it: the helper last saw the app alive at 20750, 40250 ms before that end, over the
        declared 10000 ms slack: feed-window-end-late. A record without the observed exit on the job's clock leaves the end unverifiable:
        feed-window-end-unverified. On d5233ef9 both publish the post-run row. adversary r11 (5f75e044) A1 (literal shape): the helper itself was
        descheduled after the exit and saw the app gone 40 s late, at the stalled end stamp; its last alive poll (20800) still bounds the exit,
        so the end is not bounded: the measured-app verdict refuses that gap (coverage-endpoint-gap) and the end is unverified. On 5f75e044 (the gone
        poll as the anchor) the row is published. adversary r11 (4078c9a7) A1 (literal shape): the helper recorded only a later instance reusing the
        runner's pid 5000 (started 22000, after the runner saw its app exit at 20850), alive at 60000; the verdict binds no instance
        (measured-app-not-observed), so its alive poll bounds nothing. On 4078c9a7 (the helper's pid-only app) the row is published."""
        for case, code in (("stall-before-the-end-stamp-40s", "feed-window-end-late"), ("no-observed-exit", "feed-window-end-unverified"),
                           ("helper-saw-the-exit-40s-late", "feed-window-end-unverified"), ("a-later-instance-with-the-measured-pid", "feed-window-end-unverified")):
            with self.subTest(case=case):
                got = self.r[case]
                self.assertEqual([got["rows"], got["watts"], got["temp"], got["why"]], [None, None, None, code], got)

    def test_the_feed_window_boundary_is_published(self) -> None:
        """CORE-PARTITION-FEED-WINDOW-PROVENANCE-1: core-partition.json carries the window end, the end read's slack, the app's last observed-alive
        poll, the lag between them and its slack (r10's PR body claimed the first two; d5233ef9 published neither)."""
        self.assertEqual(self.r["zero-delay-control"]["published"], [21000, 2000, 20750, 250, 10000], self.r["zero-delay-control"])
        self.assertEqual(self.r["no-observed-exit"]["published"], [21000, 2000, None, None, 10000], self.r["no-observed-exit"])

    def test_the_helper_records_its_stopwatch_origin_on_the_shared_monotonic_clock(self) -> None:
        """The real helper body (the B1 process double, a tight poll): monoBaseMs is read on the clock the job reads (Stopwatch.GetTimestamp), so
        the app's observed exit, monoBaseMs + goneMs, falls between the driver's own clock reads before the helper started and after it exited."""
        work = self.tmp / "helper-base"
        work.mkdir()
        driver = self.tmp / "helper-base.ps1"
        driver.write_text(LIFT + R11_HELPER_BASE_DRIVER, encoding="utf-8")
        proc = run_pwsh(["-File", str(driver)], env_extra={"CP_GEN": str(GENERATOR), "CP_DIR": str(work)})
        self.assertEqual(proc.returncode, 0, proc.stdout[-3000:] + proc.stderr[-3000:])
        got = json.loads(proc.stdout.strip().splitlines()[-1])["tight"]
        self.assertEqual([got["endedBy"], got["exit"]], ["stop-file", 0], got)
        self.assertIsInstance(got["monoBaseMs"], (int, float), f"the helper's record carries no monotonic origin: {got}")
        self.assertLessEqual(got["t0"], got["monoBaseMs"], got)
        self.assertLessEqual(got["t0"], got["monoBaseMs"] + got["goneMs"], got)
        self.assertLessEqual(got["monoBaseMs"] + got["goneMs"], got["t1"], got)


# r13 (Dot 6452868627 = hub ruling HUB-TICK 20261027T2058Z, CP-FEED-START-ANCHOR): the same lifted cleanup region with a 90 s window whose rows
# are $c[6] idle rows (10 W / 30 C) then $c[7] playback rows (100 W / 80 C), and a run log whose measured session starts playback at monotonic
# $c[5] ($null: the log has no playback_smoke.start line; -1: the result names no resolvable log). The start mark is read at 1000.
# r13b: the session's summary is logged at 90500 with elapsed_ms = 90500 - $c[5] (the app's monotonic clock). $c[8], when set, is where the wall
# clock STAMPED the start line (adversary r13 A1: a backward step between two skew samples); $c[9] true rewrites the log after the resolver read it
# with both stamps 57000 ms earlier, same length and consistent with each other (adversary r13 A2: swap after validate).
def _r13_start_driver() -> str:
    d = R11_CLEANUP
    write_at = d.index("    [IO.File]::WriteAllText($global:cpLog, '[' + (& $S 1500)")
    write_line = d[write_at:d.index("\n", write_at) + 1]
    for old, new in (
            ("length = 34 + $row.Length", "length = 34 + $global:cpWindow.Length"),
            ("window = $row;", "window = $global:cpWindow;"),
            ("(& $W 20850)", "(& $W 90850)"),
            ("$global:cpLog = Join-Path $dir 'run.log'; $global:cpSwap = $null\n",
             "$global:cpLog = $(if ($c[5] -eq -1) { $null } else { Join-Path $dir 'run.log' }); $global:cpSwap = $null\n"
             "    $pbAt = $(if ($null -eq $c[5] -or $c[5] -eq -1) { 61000 } else { $c[5] })\n"
             "    $global:cpWindow = (@(for ($i = 0; $i -lt $c[6]; $i++) { (& $H (1500 + 1000 * $i)) + ',x,30,10,No,No' }) + @(for ($j = 0; $j -lt $c[7]; $j++) { (& $H ($pbAt + 500 + 1000 * $j)) + ',x,80,100,No,No' }) | ForEach-Object { $_ + \"`n\" }) -join ''\n"),
            (write_line,
             "    $Body = { param($ps, $pe) $(if ($null -eq $c[5]) { '' } else { '[' + (& $S $ps) + \"] [info] [0x1] playback_smoke.start session=1 position=0`n\" }) + '[' + (& $S 1600) + \"] [info] [0x1] playback_smoke.measured_session id=1`n\""
             " + $(if ($null -eq $c[5]) { '' } else { '[' + (& $S $pe) + \"] [info] [0x1] playback_smoke.summary session=1 reason=play-stop elapsed_ms=\" + (90500 - $c[5]).ToString('0.000', [Globalization.CultureInfo]::InvariantCulture) + \" presented_frames=1`n\" }) }\n"
             "    if ($null -ne $global:cpLog) {\n"
             "        [IO.File]::WriteAllText($global:cpLog, (& $Body $(if ($c.Count -gt 8 -and $null -ne $c[8]) { $c[8] } else { $c[5] }) 90500))\n"
             "        if ($c.Count -gt 9 -and $c[9]) { $global:cpSwap = & $Body ($c[5] - 57000) (90500 - 57000) }\n"
             "    }\n"),
            ("    $out[$k] = [ordered]@{ end = $corePartitionSmokeEndMonoMs;",
             "    $out[$k] = [ordered]@{ wattsMean = $f.pkgWattsMean; tempMean = $f.pkgTempCMean; start = @($pf.startGapMs, [string]$pf.startWhy, $pf.playbackSession, ($null -ne $pf.playbackStartUtcTicks)); start2 = @($pf.startStampDisagreeMs, $pf.startGapMonoMaxMs); excluded = $pf.startExcludedRows; slackPublished = $pf.Contains('startSlackMs'); publishedRows = $pf.summary.rows; errors = @($corePartitionErrors); swapped = $(if ($null -ne $global:cpLog) { [IO.File]::ReadAllText($global:cpLog) -eq $global:cpSwap } else { $false }); end = $corePartitionSmokeEndMonoMs;")):
        if d.count(old) != 1:
            raise AssertionError(f"R11_CLEANUP changed shape near {old!r}")
        d = d.replace(old, new)
    at = d.index("$cases = [ordered]@{")
    to = d.index("}\n", d.index("'a-later-instance-with-the-measured-pid'")) + 2
    return d[:at] + """$cases = [ordered]@{
    'dot-60s-idle-then-30s-playback' = @(91000, 0, 90900, 90750, 2000, 61000, 60, 30)
    'playback-within-slack-control' = @(91000, 0, 90900, 90750, 2000, 4000, 3, 87)
    'real-shape-first-line-to-start-23600' = @(91000, 0, 90900, 90750, 2000, 24600, 23, 66)
    'no-playback-start-line' = @(91000, 0, 90900, 90750, 2000, $null, 60, 30)
    'no-run-log' = @(91000, 0, 90900, 90750, 2000, -1, 60, 30)
    'adversary-r13-a1-utc-step-at-the-start-line' = @(91000, 0, 90900, 90750, 2000, 61000, 60, 30, 4000)
    'adversary-r13-a2-swap-after-validate' = @(91000, 0, 90900, 90750, 2000, 61000, 60, 30, $null, $true)
}
""" + d[to:]


R13_START_DRIVER = _r13_start_driver()

R13_PURE = r"""
$L = [DateTime]::new(2026, 10, 27, 21, 0, 0, [DateTimeKind]::Utc)
function Ln([double]$AfterMs, [string]$Msg) { '[' + $L.AddMilliseconds($AfterMs).ToString('yyyy-MM-ddTHH:mm:ss.fffZ') + '] [info] [0x1a2b] ' + $Msg }
function Pick($v) { @([string]$v.why, $v.gapMs, $v.session) }
# r13b (A1): a session's summary line, stamped $At ms after L, carrying the app's monotonic elapsed_ms
function Sm([double]$At, [string]$Elapsed, [int]$Sid = 1) { Ln $At ('playback_smoke.summary session=' + $Sid + ' reason=play-stop elapsed_ms=' + $Elapsed + ' presented_frames=1') }
function Pick2($v) { @([string]$v.why, $v.gapMs, $v.stampDisagreeMs, $v.gapMonoMaxMs) }
$m1 = (Ln 9000 'playback_smoke.start session=1 position=0') + "`n" + (Ln 9100 'playback_smoke.measured_session id=1') + "`n" + (Sm 39000 '30000.000')
$r = [ordered]@{}
$r.withinSlackNoSkew = Pick (Get-CorePartitionPlaybackStart $m1 $L.Ticks 1000 1000 0 41000)
$r.skewCountsAgainstTheBound = Pick (Get-CorePartitionPlaybackStart $m1 $L.Ticks 1000 1000 1500 41000)
$r.startMarkReadBeforeTheLaunch = Pick (Get-CorePartitionPlaybackStart $m1 $L.Ticks 2500 1000 0 42500)
$r.beforeTheWindow = Pick (Get-CorePartitionPlaybackStart ((Ln -5000 'playback_smoke.start session=1 position=0') + "`n" + (Ln 100 'playback_smoke.measured_session id=1') + "`n" + (Sm 25000 '30000.000')) $L.Ticks 1000 1000 100 41000)
$r.warmupSessionIgnored = Pick (Get-CorePartitionPlaybackStart (@((Ln 1000 'playback_smoke.start session=1 position=0'), (Sm 29000 '28000.000' 1), (Ln 30000 'playback_smoke.start session=2 position=0'), (Ln 30100 'playback_smoke.measured_session id=2'), (Sm 60000 '30000.000' 2)) -join "`n") $L.Ticks 1000 1000 0 62000)
$r.sessionPrefixIsNotTheSession = Pick (Get-CorePartitionPlaybackStart (@((Ln 1000 'playback_smoke.start session=12 position=0'), (Ln 1100 'playback_smoke.measured_session id=1'), (Sm 31000 '30000.000')) -join "`r`n") $L.Ticks 1000 1000 0 41000)
$r.summaryFallback = Pick (Get-CorePartitionPlaybackStart (@((Ln 2000 'playback_smoke.start session=3 position=0'), (Sm 40000 '38000.000' 3)) -join "`n") $L.Ticks 1000 1000 0 42000)
$r.unknownSkew = Pick (Get-CorePartitionPlaybackStart $m1 $L.Ticks 1000 1000 $null 41000)
$r.noSinkStamp = Pick (Get-CorePartitionPlaybackStart "playback_smoke.start session=1 position=0`nplayback_smoke.measured_session id=1" $L.Ticks 1000 1000 0 41000)
$r.unparseableStamp = Pick (Get-CorePartitionPlaybackStart "[not-a-time] [info] [0x1] playback_smoke.start session=1 position=0`nplayback_smoke.measured_session id=1" $L.Ticks 1000 1000 0 41000)
$r.noSession = Pick (Get-CorePartitionPlaybackStart (Ln 1000 'playback_smoke.start session=1 position=0') $L.Ticks 1000 1000 0 41000)
$r.emptyLog = Pick (Get-CorePartitionPlaybackStart '' $L.Ticks 1000 1000 0 41000)
# r13b (adversary r13 028f4619 A1): the stamp is trusted only when the app's monotonic elapsed_ms agrees with it, and the stamped gap is under the
# job's monotonic ceiling. The adversary's input: playback really starts at L+60000 (mono 61000); a backward step between two polls stamps it L+3000.
$a1 = @((Ln 3000 'playback_smoke.start session=1 position=0 worker_threads=8'), (Ln 3010 'playback_smoke.measured_session id=1'), (Sm 89500 '29500.000')) -join "`n"
$r.a1StepAtTheStartLine = Pick2 (Get-CorePartitionPlaybackStart $a1 $L.Ticks 1000 1000 0 91000)
$r.a1StepAtTheSummaryLine = Pick2 (Get-CorePartitionPlaybackStart (@((Ln 9000 'playback_smoke.start session=1 position=0'), (Ln 9100 'playback_smoke.measured_session id=1'), (Sm 20000 '30000.000')) -join "`n") $L.Ticks 1000 1000 0 41000)
$r.a1NoSummary = Pick2 (Get-CorePartitionPlaybackStart ((Ln 9000 'playback_smoke.start session=1 position=0') + "`n" + (Ln 9100 'playback_smoke.measured_session id=1')) $L.Ticks 1000 1000 0 41000)
$r.a1SummaryWithoutElapsed = Pick2 (Get-CorePartitionPlaybackStart ((Ln 9000 'playback_smoke.start session=1 position=0') + "`n" + (Ln 9100 'playback_smoke.measured_session id=1') + "`n" + (Ln 39000 'playback_smoke.summary session=1 reason=play-stop paced_elapsed_ms=30000.000')) $L.Ticks 1000 1000 0 41000)
$r.a1AnotherSessionsSummary = Pick2 (Get-CorePartitionPlaybackStart ((Ln 9000 'playback_smoke.start session=1 position=0') + "`n" + (Ln 9100 'playback_smoke.measured_session id=1') + "`n" + (Sm 39000 '30000.000' 2)) $L.Ticks 1000 1000 0 41000)
$r.a1WithinTolerance = Pick2 (Get-CorePartitionPlaybackStart ((Ln 9000 'playback_smoke.start session=1 position=0') + "`n" + (Ln 9100 'playback_smoke.measured_session id=1') + "`n" + (Sm 39000 '30200.000')) $L.Ticks 1000 1000 0 41000)
$r.a1PastTolerance = Pick2 (Get-CorePartitionPlaybackStart ((Ln 9000 'playback_smoke.start session=1 position=0') + "`n" + (Ln 9100 'playback_smoke.measured_session id=1') + "`n" + (Sm 39000 '30300.000')) $L.Ticks 1000 1000 0 41000)
$r.a1SkewWidensTolerance = Pick2 (Get-CorePartitionPlaybackStart ((Ln 9000 'playback_smoke.start session=1 position=0') + "`n" + (Ln 9100 'playback_smoke.measured_session id=1') + "`n" + (Sm 39000 '30300.000')) $L.Ticks 1000 1000 100 41000)
$r.a1NoWindowEnd = Pick2 (Get-CorePartitionPlaybackStart $m1 $L.Ticks 1000 1000 0 $null)
$r.a1StampPastTheMonotonicCeiling = Pick2 (Get-CorePartitionPlaybackStart $m1 $L.Ticks 1000 1000 0 35000)
$r.a1Consistent = Pick2 (Get-CorePartitionPlaybackStart $m1 $L.Ticks 1000 1000 0 41000)
# adversary r13b (82510ec1) A1, its exact input: a -60000 ms excursion covers BOTH stamps (start T+3000, summary T+23000, elapsed_ms 20000, so
# they agree); launch mono 100000, start mark 99990, the smoke returned at mono 185000. The ceiling (65010 ms) is past slack + teardown (30000).
$a1b = @((Ln 3000 'playback_smoke.start session=1 position=0'), (Ln 3010 'playback_smoke.measured_session id=1'), (Sm 23000 '20000.000')) -join "`n"
$r.a1bExcursionCoversBothStamps = Pick2 (Get-CorePartitionPlaybackStart $a1b $L.Ticks 100000 99990 0 185000)
$r.a1bNamedTeardownSlack = Pick2 (Get-CorePartitionPlaybackStart $a1b $L.Ticks 100000 99990 0 185000 250 70000)
# r13c (HUB RULING 20261028T0021Z, rule (a)): the real venue shape, playback 23600 ms after the start mark (the app's first log line to
# playback_smoke.start, 23.6 s in 95ccf29f), and the start anchor the window opens at (startMonoMs, on the job's clock)
$real = @((Ln 23600 'playback_smoke.start session=1 position=0'), (Ln 23610 'playback_smoke.measured_session id=1'), (Sm 53600 '30000.000')) -join "`n"
$rs = Get-CorePartitionPlaybackStart $real $L.Ticks 1000 1000 0 64000
$r.realShape = @([string]$rs.why, $rs.gapMs, $rs.gapMonoMaxMs, $rs.startMonoMs, $rs.Contains('slackMs'))
# r13c: a stamp-consistent start whose clock-proof ceiling exceeds the stamped gap by more than the teardown slack (an unsampled excursion over
# both stamps) is still unverified; within it, the start is trusted however late it is
$r.ceilingOverGapPastTeardown = Pick2 (Get-CorePartitionPlaybackStart $real $L.Ticks 1000 1000 0 86000)
$r.ceilingOverGapWithinTeardown = Pick2 (Get-CorePartitionPlaybackStart $real $L.Ticks 1000 1000 0 74000)
# r13c: Select-CorePartitionFeedRowsFromStart, pure. A row's HWiNFO stamp (local d.M.yyyy,H:mm:ss[.fff]) is placed on the job's monotonic clock at
# the measured launch's clock pair (launch UTC $L, mono 1000), as the playback start is; a row counts only at or after the anchor plus the skew
function Hw([double]$Mono) { $L.AddMilliseconds($Mono - 1000).ToLocalTime().ToString('d.M.yyyy,H:mm:ss.fff', [Globalization.CultureInfo]::InvariantCulture) }
$anchor = @{ startMonoMs = 24600.0; launchUtcTicks = $L.Ticks; launchMonoMs = 1000.0; skewMs = 0.0 }
$rows = @('Date,Time,Total CPU Usage [%],CPU Package [C],CPU Package Power [W],Core Thermal Throttling (avg) [Yes/No],Package/Ring Thermal Throttling [Yes/No]', ((Hw 24000) + ',5,30,10,No,No'), ((Hw 24600) + ',5,80,100,No,No'), ((Hw 25600) + ',5,81,101,No,No'), '')
function PickSel($v) { @([string]$v.why, $v.excluded, @($v.lines).Count) }
$r.selAtAndAfter = PickSel (Select-CorePartitionFeedRowsFromStart $rows $anchor 26000)
$sk = $anchor.Clone(); $sk.skewMs = 700.0
$r.selSkewExcludesTheBoundaryRow = PickSel (Select-CorePartitionFeedRowsFromStart $rows $sk 26000)
$r.selSecondsOnly = PickSel (Select-CorePartitionFeedRowsFromStart @(($L.AddSeconds(-60).ToLocalTime().ToString('d.M.yyyy,H:mm:ss', [Globalization.CultureInfo]::InvariantCulture) + ',5,30,10,No,No'), ($L.AddSeconds(60).ToLocalTime().ToString('d.M.yyyy,H:mm:ss', [Globalization.CultureInfo]::InvariantCulture) + ',5,80,100,No,No')) $anchor 62000)
$r.selUnparseableStamp = PickSel (Select-CorePartitionFeedRowsFromStart @('10/24/2026,08:00:02,5,80,100,No,No') $anchor 26000)
$r.selRowFromTheFuture = PickSel (Select-CorePartitionFeedRowsFromStart @(((Hw 24600) + ',5,80,100,No,No'), ((Hw 40000) + ',5,80,100,No,No')) $anchor 26000)
$r.selNoAnchor = PickSel (Select-CorePartitionFeedRowsFromStart $rows $null 26000)
$r.selNoReadEnd = PickSel (Select-CorePartitionFeedRowsFromStart $rows $anchor $null)
$feed = [ordered]@{ rows = 3; pkgWattsMean = 40.0; pkgWattsMax = 100.0; pkgTempCMean = 46.67; pkgTempCMax = 80.0; pkgThrottledRows = 0; pkgTempCAtStart = 30.0; reasons = [ordered]@{ pkgThrottledRows = 'feed-unparseable' } }
$late = Get-CorePartitionStartBoundFeed $feed @{ why = 'feed-window-start-unverified' }
$r.bound = @($late.rows, $late.pkgWattsMean, $late.pkgTempCMean, $late.pkgTempCAtStart, $late.reasons['rows'], $late.reasons['pkgTempCMean'], $late.reasons['pkgThrottledRows'], $late.reasons.Contains('pkgTempCAtStart'))
$r.boundTwice = @((Get-CorePartitionStartBoundFeed $late @{ why = 'feed-window-start-unverified' }).reasons['rows'])
$r.missingVerdict = @((Get-CorePartitionStartBoundFeed $feed $null).reasons['pkgWattsMean'])
$ok = Get-CorePartitionStartBoundFeed $feed @{ why = $null }
$r.passThrough = @($ok.rows, $ok.pkgWattsMean, $ok.reasons.Contains('rows'))
$r | ConvertTo-Json -Compress -Depth 5
"""


# r13c (hosted CI run 40605556724, windows shard 4/8): the feed readers' monoMs is the Stopwatch read taken BEFORE the read, so it can never be
# later than a clock read taken after the reader returns. 52282f47 rounds it to the nearest ms, up to 0.5 ms into the future; the measured
# launch's mono read right after the start mark then sits before it, and Get-CorePartitionPlaybackStart refuses ($LaunchMonoMs < $StartMarkMonoMs).
R13C_MONO_READ = r"""
$f = Join-Path $env:CP_DIR 'feed.csv'
[IO.File]::WriteAllText($f, "26.10.2026,12:00:00,x,30,10,No,No`n")
$n = 400; $markLate = 0; $sinceLate = 0
for ($i = 0; $i -lt $n; $i++) {
    $m = Read-CorePartitionFeedMark $f; $after = Get-CorePartitionMonoMs
    if ($m.monoMs -gt $after) { $markLate++ }
    $e = Read-CorePartitionFeedSince $f 0 ''; $after2 = Get-CorePartitionMonoMs
    if ($e.monoMs -gt $after2) { $sinceLate++ }
}
[ordered]@{ n = $n; markLate = $markLate; sinceLate = $sinceLate } | ConvertTo-Json -Compress
"""


@requires_windows_pwsh
class CorePartitionR13cMonoStampTests(unittest.TestCase):
    """r13c (Item 3): the red hosted shard 4/8 was this diff's own race, not a flake."""

    def test_r13c_a_feed_reads_monotonic_stamp_is_never_later_than_the_next_clock_read(self) -> None:
        """Run 40605556724 job 122544486886: the marker suite's good mode published feed-window-start-unverified on the hosted runner (errors
        empty). Read-CorePartitionFeedMark stamped monoMs with [int64] (round to nearest), so on a fast read the start mark landed up to 0.5 ms
        after the measured launch's mono read that follows it. 400 reads each: no reader's monoMs is ever past a clock read taken after it
        returns. RED on 52282f47."""
        with tempfile.TemporaryDirectory(prefix="core-partition-r13c-mono-") as tmp:
            script = Path(tmp) / "mono.ps1"
            script.write_text(LIFT + R13C_MONO_READ, encoding="utf-8")
            proc = run_pwsh(["-File", str(script)], env_extra={"CP_GEN": str(GENERATOR), "CP_DIR": tmp})
            self.assertEqual(proc.returncode, 0, proc.stdout[-3000:] + proc.stderr[-3000:])
            got = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual([got["markLate"], got["sinceLate"]], [0, 0], got)


@requires_windows_pwsh
class CorePartitionR13StartTests(unittest.TestCase):
    """r13 (Dot 6452868627, CP-FEED-START-ANCHOR): the feed window's start is bound to the measured session's playback start, or refused typed."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="core-partition-r13-")
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.tmp = Path(cls._tmp.name)
        work = cls.tmp / "cleanup"
        work.mkdir()
        script = cls.tmp / "r13-cleanup.ps1"
        script.write_text(LIFT + R13_START_DRIVER, encoding="utf-8")
        proc = run_pwsh(["-File", str(script)], env_extra={"CP_GEN": str(GENERATOR), "CP_DIR": str(work)})
        cls.raw = proc
        cls.r = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.returncode == 0 and proc.stdout.strip() else None

    def setUp(self) -> None:
        self.assertIsNotNone(self.r, f"the lifted cleanup region did not run: {self.raw.stdout[-3000:]}{self.raw.stderr[-3000:]}")
        for case, got in self.r.items():
            self.assertNotIn("threw", got, f"{case}: {got}")

    def test_r13c_dot_counterexample_reports_the_playback_means_not_a_refusal(self) -> None:
        """Dot 6452868627 (literal): 60 s of startup rows (10 W / 30 C) then 30 s of playback rows (100 W / 80 C) in a 90 s window that passes the
        120 s freshness bound and every END bound. On a751394f the window means are 40 W / 46.67 C (rows 90, no reason); on 52282f47 every window
        field is refused feed-window-start-late. r13c (HUB RULING 20261028T0021Z, rule (a)): the verified playback start is the window START, so the
        60 startup rows stamped before it are excluded and the window reports the PLAYBACK means, 100 W / 80 C over 30 rows; core-partition.json
        publishes the gap (telemetry, no longer a refusal), the excluded row count and a null start verdict, and no start slack."""
        got = self.r["dot-60s-idle-then-30s-playback"]
        self.assertEqual([got["rows"], got["wattsMean"], got["tempMean"], got["why"]], [30, 100.0, 80.0, ""], got)
        gap, why, session, stamped = got["start"]
        self.assertEqual([why, session, stamped, got["excluded"], got["publishedRows"], got["slackPublished"]], ["", 1, True, 60, 30, False], got)
        self.assertAlmostEqual(gap, 60000, delta=1, msg=got)

    def test_r13c_the_real_venue_start_gap_publishes_the_window(self) -> None:
        """r13c (HUB RULING 20261028T0021Z): the real shape. In the 2026-10-27 venue logs (95ccf29f / 705ca6f7) the app's first log line to
        playback_smoke.start is 23.6 s / 23.0 s, past r13b's 10000 ms slack, so 52282f47 refuses EVERY real run feed-window-start-late and the
        opt-in leg never publishes a window. Here playback starts 23600 ms after the start mark: the window publishes the 66 playback rows'
        means (100 W / 80 C), the 23 startup rows are excluded, and startGapMs (about 23600) is published as telemetry."""
        got = self.r["real-shape-first-line-to-start-23600"]
        self.assertEqual([got["rows"], got["wattsMean"], got["tempMean"], got["why"]], [66, 100.0, 80.0, ""], got)
        gap, why, _session, _stamped = got["start"]
        self.assertEqual([why, got["excluded"]], ["", 23], got)
        self.assertAlmostEqual(gap, 23600, delta=1, msg=got)

    def test_r13_control_playback_within_the_slack_keeps_todays_means(self) -> None:
        """No-delay control: playback starts 3000 ms after the start mark. r13c: the 3 startup rows stamped before it are excluded too (before
        r13c they were averaged in: rows 90, 97 W / 78.33 C); the 87 playback rows publish 100 W / 80 C, with the gap and a null start verdict."""
        got = self.r["playback-within-slack-control"]
        self.assertEqual([got["rows"], got["wattsMean"], got["tempMean"], got["why"]], [87, 100.0, 80.0, ""], got)
        gap, why, _session, _stamped = got["start"]
        self.assertEqual([why, got["excluded"]], ["", 3], got)
        self.assertAlmostEqual(gap, 3000, delta=1, msg=got)

    def test_r13_no_playback_start_signal_is_a_typed_refusal_never_a_measured_value(self) -> None:
        """A run log without the measured session's playback_smoke.start line, or no resolvable run log at all: feed-window-start-unverified on
        every window field, and no gap. On a751394f both publish the 40 W / 46.67 C means."""
        for case in ("no-playback-start-line", "no-run-log"):
            with self.subTest(case=case):
                got = self.r[case]
                self.assertEqual([got["rows"], got["wattsMean"], got["tempMean"], got["why"]], [None, None, None, "feed-window-start-unverified"], got)
                self.assertEqual(got["start"][:2], [None, "feed-window-start-unverified"], got)
                self.assertIsNone(got["excluded"], got)

    def test_r13_the_start_rule(self) -> None:
        """Get-CorePartitionPlaybackStart and Get-CorePartitionStartBoundFeed, pure: the gap runs from the start mark (not the launch stamp), only
        the measured session's start line counts (session=12 is not session=1; a warmup session's earlier start is not the measured one), the
        summary-session fallback, and every missing or unusable signal is feed-window-start-unverified. The bound nulls the window fields only,
        keeps a reason already there and leaves pkgTempCAtStart. r13c (rule (a)): a late start is no refusal (no slack; the skew no longer counts
        against one); the verdict carries the anchor (startMonoMs); the clock-proof ceiling may exceed the stamped gap by at most the teardown slack.
        Select-CorePartitionFeedRowsFromStart: rows stamped before the anchor (plus the skew) are excluded; an unparseable stamp, a row from after
        the end read, or no anchor / no end read is a typed refusal."""
        script = self.tmp / "r13-pure.ps1"
        script.write_text(LIFT + R13_PURE, encoding="utf-8")
        proc = run_pwsh(["-File", str(script)], env_extra={"CP_GEN": str(GENERATOR)})
        self.assertEqual(proc.returncode, 0, proc.stdout[-3000:] + proc.stderr[-3000:])
        r = json.loads(proc.stdout.strip().splitlines()[-1])
        unverified = "feed-window-start-unverified"
        self.assertEqual(r["withinSlackNoSkew"], ["", 9000, 1])
        self.assertEqual(r["skewCountsAgainstTheBound"], ["", 9000, 1])
        self.assertEqual(r["startMarkReadBeforeTheLaunch"], ["", 10500, 1])
        self.assertNotIn("namedSlack", r)
        self.assertEqual(r["beforeTheWindow"], [unverified, -5000, 1])
        self.assertEqual(r["warmupSessionIgnored"], ["", 30000, 2])
        # r13c: the real venue shape (23600 ms) is trusted and anchored; no slack is published
        self.assertEqual(r["realShape"], ["", 23600, 33000, 24600, False])
        self.assertEqual(r["ceilingOverGapPastTeardown"], [unverified, 23600, 0, 55000])
        self.assertEqual(r["ceilingOverGapWithinTeardown"], ["", 23600, 0, 43000])
        self.assertEqual(r["selAtAndAfter"], ["", 1, 2])
        self.assertEqual(r["selSkewExcludesTheBoundaryRow"], ["", 2, 1])
        self.assertEqual(r["selSecondsOnly"], ["", 1, 1])
        self.assertEqual(r["selUnparseableStamp"], ["feed-row-stamp-unparseable", None, 0])
        self.assertEqual(r["selRowFromTheFuture"], ["feed-row-stamp-unverified", None, 0])
        self.assertEqual(r["selNoAnchor"], [unverified, None, 0])
        self.assertEqual(r["selNoReadEnd"], [unverified, None, 0])
        self.assertEqual(r["sessionPrefixIsNotTheSession"], [unverified, None, 1])
        self.assertEqual(r["summaryFallback"], ["", 2000, 3])
        self.assertEqual(r["unknownSkew"], [unverified, None, 1])
        self.assertEqual(r["noSinkStamp"], [unverified, None, 1])
        self.assertEqual(r["unparseableStamp"], [unverified, None, 1])
        self.assertEqual(r["noSession"], [unverified, None, None])
        self.assertEqual(r["emptyLog"], [unverified, None, None])
        self.assertEqual(r["bound"], [None, None, None, 30.0, unverified, unverified, "feed-unparseable", False])
        self.assertEqual(r["boundTwice"], [unverified])
        self.assertEqual(r["missingVerdict"], [unverified])
        self.assertEqual(r["passThrough"], [3, 40.0, False])
        # r13b (A1): the stamp agrees with the app's monotonic elapsed_ms and sits under the job's monotonic ceiling, or it is unverified
        self.assertEqual(r["a1StepAtTheStartLine"], [unverified, 3000, 57000, None])
        self.assertEqual(r["a1StepAtTheSummaryLine"], [unverified, 9000, 19000, None])
        self.assertEqual(r["a1NoSummary"], [unverified, 9000, None, None])
        self.assertEqual(r["a1SummaryWithoutElapsed"], [unverified, 9000, None, None])
        self.assertEqual(r["a1AnotherSessionsSummary"], [unverified, 9000, None, None])
        self.assertEqual(r["a1WithinTolerance"], ["", 9000, 200, 9800])
        self.assertEqual(r["a1PastTolerance"], [unverified, 9000, 300, None])
        self.assertEqual(r["a1SkewWidensTolerance"], ["", 9000, 300, 9700])
        self.assertEqual(r["a1NoWindowEnd"], [unverified, 9000, 0, None])
        self.assertEqual(r["a1StampPastTheMonotonicCeiling"], [unverified, 9000, 0, 4000])
        self.assertEqual(r["a1Consistent"], ["", 9000, 0, 10000])
        # adversary r13b (82510ec1) A1: an excursion over both stamps; the clock-proof ceiling (start gap + teardown) bounds it
        self.assertEqual(r["a1bExcursionCoversBothStamps"], [unverified, 3010, 0, 65010])
        self.assertEqual(r["a1bNamedTeardownSlack"], ["", 3010, 0, 65010])
        # r13c: no publication of feed-window-start-late is left for any input above
        self.assertNotIn("feed-window-start-late", json.dumps(r))

    def test_r13b_adversary_a1_a_backward_utc_step_at_the_start_line_is_refused_not_averaged(self) -> None:
        """adversary r13 (028f4619) A1, its exact input: playback really starts at mono 61000 (60000 ms after the start mark), but a backward UTC
        step between two helper polls stamps the start line L+3000; job and helper skew are zero. On 028f4619 the start verdict accepts a
        3000 ms gap and the feed publishes 40 W / 46.67 C. Now the stamp disagrees with the session's monotonic elapsed_ms (29500 ms against a
        86500 ms stamp span) by 57000 ms: every window field is null with feed-window-start-unverified, and the disagreement is published."""
        got = self.r["adversary-r13-a1-utc-step-at-the-start-line"]
        self.assertEqual([got["rows"], got["wattsMean"], got["tempMean"], got["why"]], [None, None, None, "feed-window-start-unverified"], got)
        self.assertEqual(got["start"][1], "feed-window-start-unverified", got)
        self.assertAlmostEqual(got["start2"][0], 57000, delta=1, msg=got)

    def test_r13b_adversary_a2_a_swap_after_validate_is_never_read(self) -> None:
        """adversary r13 (028f4619) A2: the resolver checks the genuine log (playback 60000 ms after the start mark), then a same-length rewrite
        moves both the start and the summary stamps 57000 ms earlier (consistent with each other, so the A1 check alone would pass it). On
        028f4619 the job reopens the path, reads the rewrite and publishes 40 W / 46.67 C. Now the bytes it reads are hashed again and do not
        match what the resolver checked: a typed refusal (feed-window-start-unverified, error run-log-changed-after-check), never a value."""
        got = self.r["adversary-r13-a2-swap-after-validate"]
        self.assertTrue(got["swapped"], f"the rewrite must be on disk when the job reads: {got}")
        self.assertEqual([got["rows"], got["wattsMean"], got["tempMean"], got["why"]], [None, None, None, "feed-window-start-unverified"], got)
        self.assertEqual(got["start"][0], None, got)
        self.assertIn("playback-start-log: run-log-changed-after-check", got["errors"], got)


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
