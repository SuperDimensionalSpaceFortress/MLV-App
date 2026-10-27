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
    $fb = @{}; if ((Get-Command Get-CorePartitionFeedSummary).Parameters.ContainsKey('Base')) { $fb.Base = $corePartitionFeedPreHelper }
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
$psi.Environment['MLV_CP_DEADLINE_TICKS'] = [string]([DateTime]::UtcNow.AddSeconds(120).Ticks)
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
$psi.Environment['MLV_CP_DEADLINE_TICKS'] = [string]([DateTime]::UtcNow.AddSeconds(120).Ticks)
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
    $psi.Environment['MLV_CP_DEADLINE_TICKS'] = [string]([DateTime]::UtcNow.AddSeconds(60).Ticks)
    $psi.Environment['MLV_CP_NONCE'] = 'exit-driver-key-0123456789abcdef'
    $h = [Diagnostics.Process]::Start($psi)
    if (-not $h.WaitForExit(60000)) { $h.Kill(); [void]$h.WaitForExit(5000) }
    $mac = Join-Path $d 'helper.json.mac'
    $bytes = [IO.File]::ReadAllBytes((Join-Path $d 'helper.json'))
    $macText = $(if (Test-Path -LiteralPath $mac) { [IO.File]::ReadAllText($mac) })
    $ok = $(if (Get-Command Test-CorePartitionHelperMac -ErrorAction SilentlyContinue) { @((Test-CorePartitionHelperMac $bytes $macText 'exit-driver-key-0123456789abcdef'), (Test-CorePartitionHelperMac $bytes $macText 'another-key')) } else { @('absent') })
    @{ exit = $h.ExitCode; macFile = (Test-Path -LiteralPath $mac); mac = $ok; nonceInRecord = ([Text.Encoding]::UTF8.GetString($bytes)).Contains('exit-driver-key') }
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
                self.assertIn("$corePartitionHelperWhy $corePartitionHelperNotReady $corePartitionMeasuredPid $corePartitionPublishWhy $corePartitionMeasuredStartTicks $corePartitionMeasuredEndTicks\n", text)
                self.assertIn("ConvertTo-CorePartitionUtcTicks $cpRunnerProcess.startedAtUtc", text)
                for fn in ("function Test-CorePartitionMeasuredApp", "function Get-CorePartitionFeedHandleInfo", "function ConvertTo-CorePartitionUtcTicks"):
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

    def test_the_jobs_key_reaches_only_the_helper(self) -> None:
        """sol r6 CP-06: the per-job key is drawn at run time, handed to the helper by environment (never the command line), never traced or
        published, and the job checks the record with it, the helper's exit code and the launch pid."""
        for key in ("a", "b1", "b2", "cuda-b2"):
            with self.subTest(job=key):
                text = lf(self.job(key))
                uses = [ln.strip() for ln in text.splitlines() if "$corePartitionHelperNonce" in ln]
                self.assertEqual(len(uses), 3, uses)
                self.assertTrue(uses[0].startswith("$corePartitionHelperNonce = "), uses)
                self.assertIn("$cpKeyBytes", uses[0])
                self.assertIn("RandomNumberGenerator]::Create()", text)
                self.assertEqual(uses[1], "$cpPsi.Environment['MLV_CP_NONCE'] = $corePartitionHelperNonce")
                self.assertIn("Test-CorePartitionHelperRecord ", uses[2])
                self.assertIn("$corePartitionHelperExit", uses[2])
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
                         "ReadCategory()", "'Parking Status'", "MLV_CP_DEADLINE_TICKS"):
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
function Get-CorePartitionFeedSummary($Start, $End, [double]$StaleSec, $Base = $base) { & $cpFeedSummaryImpl $Start $End $StaleSec $Base }
$start = @{ exists = $true; length = 500; ageSec = 1.0; before = "26.10.2026,01:00:00,x,61.0,20.5,No,No`n26.10.2026,01:00:01,x,62.5,21.0,No,No`n"; window = ''; windowStartsMidRow = $false; monoMs = [int64]5005000; readUtcTicks = $T0; lastWriteUtcTicks = $T0 - 10000000 }
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
$one = '"instances":[{"pid":5000,"startUtcTicks":639286128040000000,"firstSeenUtcTicks":639286128041000000,"lastSeenUtcTicks":639286128200000000,"goneUtcTicks":639286128202000000}]'
$helper = ('{"topologyCheck":{"ok":true,"reason":null,"pMask":4095,"eMask":1044480},"helperAffinityObserved":1044480,"app":{"pid":5000,"startUtcTicks":639286128040000000,"affinityFirst":4095,"affinityLast":4095},"load":{"seconds":10.0,"bytes":20000000000},"samples":[{"app":true,"inst":["0,0","0,12"],"freq":[2500,2500],"perf":[160,80],"park":[0,0]}],' + $one + '}') | ConvertFrom-Json
$w = @{ value = 16; why = $null }
$b2 = Get-CorePartitionScalars $helper $feed 4095 1044480 8 $w $null @mp
$r.b2 = @($b2.corePartitionTopologyOk, $b2.appAffinityObserved, $b2.benchAffinityRequested, $b2.benchAffinityApplied, $b2.partitionLoadMask, $b2.partitionLoadApplied, $b2.partitionLoadGBps,
    $b2.pcoreMHzMean, $b2.ecoreMHzMean, $b2.pcoreParkedSampleFraction, $b2.playbackWorkerThreads, $b2.pkgWattsMean, $b2.corePartitionNullReasons, @($b2.Keys).Count)
$helperA = ('{"topologyCheck":{"ok":true,"reason":null,"pMask":4095,"eMask":1044480},"helperAffinityObserved":1044480,"app":{"pid":5000,"startUtcTicks":639286128040000000,"affinityFirst":1048575,"affinityLast":1048575},"load":{"seconds":0.0,"bytes":0},"samples":[],' + $one + '}') | ConvertFrom-Json
$a = Get-CorePartitionScalars $helperA $feed 0 0 0 $w $null @mp
$r.a = @($a.appAffinityObserved, $a.benchAffinityApplied, $a.benchAffinityRequested, $a.partitionLoadApplied, $a.partitionLoadGBps, $a.pcoreMHzMean, $a.corePartitionNullReasons)
$helperMoved = ('{"topologyCheck":{"ok":true,"reason":null,"pMask":4095,"eMask":1044480},"helperAffinityObserved":1044480,"app":{"pid":5000,"startUtcTicks":639286128040000000,"affinityFirst":4095,"affinityLast":1048575},"load":{"seconds":0.5,"bytes":10},"samples":[],' + $one + '}') | ConvertFrom-Json
$m = Get-CorePartitionScalars $helperMoved $feed 4095 1044480 8 $w $null @mp
$r.moved = @($m.appAffinityObserved, $m.benchAffinityApplied, $m.partitionLoadApplied)
$wrongHelper = ('{"topologyCheck":{"ok":true,"reason":null,"pMask":4095,"eMask":1044480},"helperAffinityObserved":1048575,"app":{"pid":5000,"startUtcTicks":639286128040000000,"affinityFirst":4095,"affinityLast":4095},"load":{"seconds":10,"bytes":10},"samples":[],' + $one + '}') | ConvertFrom-Json
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
$helperGood = ('{"topologyCheck":{"ok":true,"reason":null,"pMask":4095,"eMask":1044480},"helperAffinityObserved":1044480,"app":{"pid":5000,"startUtcTicks":639286128040000000,"affinityFirst":4095,"affinityLast":4095},"load":{"seconds":15,"bytes":30000000000},"samples":[],' + $one + '}') | ConvertFrom-Json
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
$startPart = $start.Clone(); $startPart.before = "26.10.2026,01:00:00,x,61.0,20.5,No,No`n26.10.2026,01:00:01,x,6,20,No,N"
$r.partialRow = @((Get-CorePartitionFeedSummary $start $part 120).reasons['rows'], (Get-CorePartitionFeedSummary $start $partTwo 120).rows, (Get-CorePartitionFeedSummary $start $partTwo 120).pkgWattsMax,
    (Get-CorePartitionFeedSummary $startPart $whole 120).pkgTempCAtStart)
# sol r4 A4: NaN, Infinity and an overflowing exponent are not measured values
$r.nonFinite = @(foreach ($row in @("26.10.2026,01:00:02,x,NaN,45,No,No`n", "26.10.2026,01:00:02,x,70,1e309,No,No`n", "26.10.2026,01:00:02,x,Infinity,45,No,No`n", "26.10.2026,01:00:02,x,70,-Infinity,No,No`n")) {
    $nf = $end.Clone(); $nf.window = $row; $nf.windowStartsMidRow = $false; (Get-CorePartitionFeedSummary $start $nf 120).reasons['pkgWattsMean'] })
$nfs = $start.Clone(); $nfs.before = "26.10.2026,01:00:00,x,NaN,20.5,No,No`n"
$r.nonFiniteStart = (Get-CorePartitionFeedSummary $nfs $whole 120).reasons['pkgTempCAtStart']
# sol r4 CP-02 (literal): launch mark 2026-10-26T12:00:00Z; unrelated same-path pid 4242 starts at +50 ms with affinity 4095 and lives throughout;
# the measured pid 5000 starts later. One record as the r4 helper wrote it (4242 bound), one as an r5 helper binds it (5000, 4242 still alive).
$L = 639286128000000000
function New-CpInst([int]$ProcId, [double]$Start, [double]$First, [double]$Last, $Gone) {
    $g = if ($null -eq $Gone) { 'null' } else { [string]($L + [int64]($Gone * 1e7)) }
    '{"pid":' + $ProcId + ',"startUtcTicks":' + ($L + [int64]($Start * 1e7)) + ',"firstSeenUtcTicks":' + ($L + [int64]($First * 1e7)) + ',"lastSeenUtcTicks":' + ($L + [int64]($Last * 1e7)) + ',"goneUtcTicks":' + $g + ',"affinityFirst":4095,"affinityLast":4095,"loadSec":15.0,"loadBytes":30000000000}'
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
$hb = '"topologyCheck":{"ok":true,"reason":null,"pMask":4095,"eMask":1044480},"helperAffinityObserved":1044480,"samples":[{"app":true,"appPid":5000,"inst":["0,0","0,12"],"freq":[2500,2500],"perf":[160,80],"park":[0,0]}]'
function New-CpRecord([string]$App, [string[]]$Inst, [string[]]$Others = @()) { ('{' + $hb + ',"app":' + $App + ',"load":{"seconds":15.0,"bytes":30000000000},"instances":[' + ($Inst -join ',') + '],"others":[' + ($Others -join ',') + ']}') | ConvertFrom-Json }
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
$a2S = @{ exists = $true; length = 34; created = $T0 - 864000000000; fileId = '0000ABCD-0000000000000002'; monoMs = [int64]9000000; readUtcTicks = $T0; lastWriteUtcTicks = $T0 - 864000000000; before = "25.10.2026,12:00:00,x,20,10,No,No`n"; window = ''; windowStartsMidRow = $false }
$a2E = @{ exists = $true; length = 68; created = $T0 - 864000000000; fileId = '0000ABCD-0000000000000002'; monoMs = [int64]9020000; readUtcTicks = $T0 + 200000000; lastWriteUtcTicks = $T0 + 200000000; before = ''; window = "26.10.2026,12:00:20,x,70,45,No,No`n"; windowStartsMidRow = $false; replaced = $false; truncated = $false }
$a2 = Get-CorePartitionFeedSummary $a2S $a2E 120 $a2B
# the same file grown after the pre-helper read only by an unfinished row: the newest complete row ends before the pre-helper offset (183 < 190)
$pB = @{ exists = $true; length = 190; monoMs = [int64]8995000 }
$pS = @{ exists = $true; length = 200; monoMs = [int64]9000000; before = "26.10.2026,12:00:00,x,20,10,No,No`n26.10.2026,12:00"; window = ''; windowStartsMidRow = $false }
$pBok = $pB.Clone(); $pBok.length = 180
$r.adversaryR7 = @([string]$a1.reasons['rows'], $a1.rows, $a1.pkgWattsMean, $a2.rows, $a2.pkgWattsMean, $a2.pkgTempCAtStart, [string]$a2.reasons['pkgTempCAtStart'],
    [string](Get-CorePartitionFeedSummary $pS $a2E 120 $pB).reasons['pkgTempCAtStart'], (Get-CorePartitionFeedSummary $pS $a2E 120 $pBok).pkgTempCAtStart)# sol r6 CP-07: the one guarded number parse (absent in a pre-r7 generator)
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
$s308 = $start.Clone(); $s308.before = "26.10.2026,01:00:00,x,30,10,No,No`n"
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
$preLines = @([regex]::Matches($src.Substring(0, $postAt), '(?m)^(\$corePartitionHelper(Why|NotReady) = .*|if \(\$null -ne \$corePartitionHelper -and -not \$corePartitionHelperLaunch\.ready\).*)$') | ForEach-Object { $_.Value })
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
$record = '{"schema":"mlv-app/core-partition-helper/v1","pid":4242,"topologyCheck":{"ok":true,"reason":null,"pMask":4095,"eMask":1044480},"helperAffinityObserved":1044480,"app":{"pid":5000,"startUtcTicks":639286128040000000,"affinityFirst":4095,"affinityLast":4095},"load":{"seconds":15,"bytes":30000000000},"samples":[{"app":true,"inst":["0,0","0,12"],"freq":[2500,2500],"perf":[160,80],"park":[0,0]}],"instances":[{"pid":5000,"startUtcTicks":639286128040000000,"firstSeenUtcTicks":639286128041000000,"lastSeenUtcTicks":639286128200000000,"goneUtcTicks":639286128202000000}]}'
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
    @{ name = 'pid-oops-macd'; mode = 'exit'; record = 'pid-oops-mac'; ready = $true })
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
        proc = run_pwsh(["-Command", LIFT + FIXTURES], env_extra={"CP_GEN": str(GENERATOR), "CP_TMP": cls._tmp.name})
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
                # sol r4 A1: core-partition.json's feed summary is held to the same rule as the success-summary scalars
                self.assertEqual(c["published"], [None] * 7 + ["helper-not-ready-at-launch"], f"core-partition.json published feed values: {c}")
        self.assertTrue(got["stop-timeout"]["killed"], "the stop-timeout double was killed (the branch under test ran)")
        # sol r6 CP-06: a ready helper the job had to kill no longer vouches for its record (exit -1); the feed window is still published
        killed = got["ready-stop-timeout-control"]
        self.assertEqual([killed["topo"], killed["bench"], killed["load"]], [False, False, False], f"a killed helper's record was accepted: {killed}")
        self.assertEqual(killed["helperWhy"], "helper-killed-after-stop-timeout+helper-exit-nonzero")
        self.assertFalse(killed["notReady"])
        self.assertEqual(killed["pkg"][:4], [50.0, 55.0, 75.0, 80.0], "a ready helper's window still carries the feed")
        self.assertEqual(killed["published"][:3], [2, 50.0, 55.0], "a ready helper's core-partition.json still carries the feed")
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
            "launch-pid-null-record-pid-0": "helper-not-started+helper-record-not-the-helpers",
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
