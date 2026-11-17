"""Tests for UM-REGULAR-BASELINE-STAGE-1: the tracked nightly venue stage driver tools/profiling/dual-venue/Invoke-VenueStageNightly.ps1 and the
promoted single-use JobId reuse helpers tools/profiling/dual-venue/VenueStageReuse.psm1.

Owner directive 2026-10-18 (Layi): "I'm curious how playback performs on ultramagnus so id like regular benchmarking there as well when possible". The baseline
(Invoke-VenueBaseline.ps1) only FINDS a staged build (BUILD_NOT_STAGED, exit 22, when it is not there); this driver puts fork/master there at most once per night by the
four tracked staging steps (DLL pair on Ultra-Magnus, local assemble under build-guard, stage job, smoke-runner stage job).

WHAT IS PINNED
  * idempotency: a sha whose cache build.json is a SOUND staged build (the shared soundness read, artifacts verified; r4) and whose smoke-runner dir is on the share is STAGE_SKIP_ALREADY;
    nothing is generated (except the local smoke-runner generator that names the runner dir), assembled or submitted, and the row carries the by-name cache hash;
  * the #508 shape: a default stage JobId whose outbox result says STAGE_OK while the cache does not hold the expected manifest is re-staged under a -JobIdSuffix,
    never reported "reused"; after every STAGE_OK the cache file is re-hashed BY NAME, and a mismatch is STAGE_VERIFY_MISMATCH, never success;
  * capacity refusals (disk floor, commit ceiling, build-guard) exit 3 and print the gate's own text verbatim (hub-autopilot re-arms on that text);
  * typed refusals: a missing gate script, a live claim or queued ticket, an unreachable share, an UNRESOLVED um-run (no retry), a second build in one night;
  * the reservation (CoS Dot b21fb3be): a STAGE_RESERVED row is persisted before the first staging step and holds the night for its sha whether or not the
    call ever finished; another sha that night is NIGHT_RESERVED (52), the same sha resumes and names the reservation it reconciled; a receipt append or read
    that fails is RECEIPT_IO_FAILED (51), never STAGE_OK/0; the venue mutex does not depend on the caller's receipt path, and the real venue share takes only
    the canonical receipt;
  * a DLL pair that is already built is harvested, not re-submitted; every child is bounded by -MaxTotalSec and killed past it;
  * -WhatIf prints the plan and builds, submits, probes and writes nothing.

Offline only: um-run, the three generators, the assembler and the two gate scripts are stubs passed through the driver's seams; the agent share, the claim directory,
the build root and the receipt file are temp directories. Each stub appends what it was called with to one calls log, so a test can assert "submitted nothing".
Nothing here reaches a venue, builds, starts MLVApp or touches the board's claim path. Windows-only (PowerShell tools).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DV = ROOT / "tools" / "profiling" / "dual-venue"
# STAGE_NIGHTLY_DRIVER lets the red-first run point the SAME tests at a fixture driver (the copied pre-card behaviour); CI never sets it.
DRIVER = Path(os.environ.get("STAGE_NIGHTLY_DRIVER") or (DV / "Invoke-VenueStageNightly.ps1"))
REUSE_MODULE = DV / "VenueStageReuse.psm1"
PWSH = shutil.which("pwsh")
requires_windows_pwsh = unittest.skipIf(PWSH is None or sys.platform != "win32", "needs pwsh on Windows")

SHA = "a" * 40
OTHER_SHA = "c" * 40
S12 = SHA[:12]
RUNNER_DIR = "smoke-runner-" + "b" * 16
EXIT = {"OK": 0, "USAGE": 2, "CAPACITY": 3, "AGENT_BUSY": 40, "SHARE_UNREACHABLE": 41, "SOURCE_COMMIT_UNRESOLVED": 42, "GATE_UNAVAILABLE": 43,
        "DLLPAIR_FAILED": 44, "ASSEMBLE_FAILED": 45, "STAGE_FAILED": 46, "STAGE_VERIFY_MISMATCH": 47, "SMOKE_RUNNER_STAGE_FAILED": 48,
        "STAGE_UNRESOLVED": 49, "STAGE_CAP_EXCEEDED": 50, "RECEIPT_IO_FAILED": 51, "NIGHT_RESERVED": 52}
NOW = "2026-10-19T21:00:00Z"      # the night key of this instant is 2026-10-20 (the next 08:00Z)
NIGHT = "2026-10-20"


def names(sha: str = SHA) -> dict[str, str]:
    s = sha[:12]
    return {"exe": f"MLVApp-playback-attr-3-cuda-{s}.exe", "dll": f"igpu_recon_cuda-playback-attr-3-cuda-{s}.dll",
            "pkg": f"MLVApp-playback-attr-3-cuda-{s}-pkg.zip", "build": f"playback-attr-3-cuda-{s}-build.json"}


# ---- stubs. Every one appends "<ROLE> k=v ..." to $env:FAKE_CALLS. --------------------------------------------------------------------------------------------
FAKE_DLLGEN = r"""
[CmdletBinding(PositionalBinding=$false)] param([string]$SourceCommit, [string]$OutDir, [string]$JobIdSuffix = '')
# NIGHTLY-DLLGEN-ARGV-PIN-1: no positional binding (a stray argument is a binding error, never swallowed) and the process's own argv is recorded verbatim after
# the script path, '|'-joined, so a test pins the exact argument list the driver handed the generator (an empty-string element shows as '||').
$raw = [Environment]::GetCommandLineArgs(); $fi = [array]::IndexOf($raw, '-File')
$argvTail = $(if ($fi -ge 0 -and $raw.Count -gt $fi + 2) { $raw[($fi + 2)..($raw.Count - 1)] } else { @() })
Add-Content -LiteralPath $env:FAKE_CALLS -Value "DLLGEN sha=$SourceCommit out=$OutDir suffix=$JobIdSuffix argv=$($argvTail -join '|')"
$id = "playback-attr-3-cuda-dllpair-$($SourceCommit.Substring(0, 12))"
if ($JobIdSuffix) { $id = "$id-$JobIdSuffix" }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
Set-Content -LiteralPath (Join-Path $OutDir "$id.job.ps1") -Value '# fixture dll job'
Set-Content -LiteralPath (Join-Path $OutDir "$id-source.zip") -Value 'fixture zip'
"RESULT=DLL_JOB_EMITTED $id"
"""

FAKE_STAGEGEN = r"""
[CmdletBinding()] param([string]$SourceCommit, [string]$BuildDir, [string]$OutDir, [string]$AgentRoot, [string]$JobIdSuffix = '')
Add-Content -LiteralPath $env:FAKE_CALLS -Value "STAGEGEN sha=$SourceCommit build=$BuildDir out=$OutDir root=$AgentRoot suffix=$JobIdSuffix"
$id = "playback-attr-3-cuda-stage-$($SourceCommit.Substring(0, 12))"
if ($JobIdSuffix) { $id = "$id-$JobIdSuffix" }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
Set-Content -LiteralPath (Join-Path $OutDir "$id.job.ps1") -Value '# fixture stage job'
"""

FAKE_SMOKEGEN = r"""
[CmdletBinding()] param([string]$SourceCommit, [string]$OutDir, [string]$AgentRoot)
Add-Content -LiteralPath $env:FAKE_CALLS -Value "SMOKEGEN sha=$SourceCommit out=$OutDir root=$AgentRoot"
$id = "attr3-stage-smoke-runner-$($SourceCommit.Substring(0, 12))-bbbbbbbbbbbb"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$job = Join-Path $OutDir "$id.job.ps1"
Set-Content -LiteralPath $job -Value '# fixture smoke-runner job'
"RESULT=SMOKE_RUNNER_STAGE_JOB_EMITTED SOURCE=$SourceCommit CLOSURE_DIGEST=$('b' * 64) CACHE_DIR_NAME=smoke-runner-$('b' * 16) JOB=$job"
"""

FAKE_ASSEMBLE = r"""
[CmdletBinding()] param([string]$SourceCommit, [string]$DllPairDir, [string]$OutDir)
Add-Content -LiteralPath $env:FAKE_CALLS -Value "ASSEMBLE sha=$SourceCommit dll=$DllPairDir out=$OutDir pid=$PID hsdeadline=$env:MLV_HEAVY_SLOT_DEADLINE_UTC"
switch ($env:FAKE_ASSEMBLE) {
    'guard' { "BUILD_MEM_REFUSED label=attr3-assemble:$($SourceCommit.Substring(0, 12)) freePhysGB=1.2 freeCommitGB=2.3 need=6/8 waitedSec=1200 utc=fixture"; exit 3 }
    'requeue' { "BUILD_HEAVY_SLOT_REQUEUE label=attr3-assemble:$($SourceCommit.Substring(0, 12)) code=$env:FAKE_REQUEUE_CODE ticket=t-1 reason=timed out utc=2026-10-29T10:00:00Z"; exit 3 }
    'slot-busy' { "BUILD_SLOT_BUSY label=attr3-assemble:$($SourceCommit.Substring(0, 12)) heldBy=[pid=1 label=peer] waitedSec=1200 utc=fixture"; exit 3 }
    'fail' { 'RESULT=ASSEMBLE_FAILED STEP=make EXIT=15'; exit 15 }
    'sleep' { Start-Sleep -Seconds 300; exit 0 }
}
$s = $SourceCommit.Substring(0, 12)
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
foreach ($n in "MLVApp-playback-attr-3-cuda-$s.exe", "igpu_recon_cuda-playback-attr-3-cuda-$s.dll", "MLVApp-playback-attr-3-cuda-$s-pkg.zip") {
    Set-Content -LiteralPath (Join-Path $OutDir $n) -Value "fixture $n"
}
$bj = Join-Path $OutDir "playback-attr-3-cuda-$s-build.json"
# assembledAtUtc makes every assemble a NEW manifest, exactly like the real assembler (assemble.ps1:411). r4 fix iteration (adversary pass 7 A1): the driver
# records STAGE_OK only for a SOUND staged build, so the assembled manifest carries what the leg reads (scalar digests of the three artifacts written above).
$own = [ordered]@{}
foreach ($pair in @(@('exe', "MLVApp-playback-attr-3-cuda-$s.exe"), @('dll', "igpu_recon_cuda-playback-attr-3-cuda-$s.dll"), @('packageZip', "MLVApp-playback-attr-3-cuda-$s-pkg.zip"))) {
    $own[$pair[0]] = [ordered]@{ name = $pair[1]; sha256 = (Get-FileHash -LiteralPath (Join-Path $OutDir $pair[1]) -Algorithm SHA256).Hash.ToLowerInvariant() }
}
[IO.File]::WriteAllText($bj, ([ordered]@{ sourceCommit = $SourceCommit; assembledAtUtc = [DateTime]::UtcNow.ToString('o'); nonce = [guid]::NewGuid().ToString('N')
    dllPairManifestSha256 = ('b' * 64); pendingSymbolPresence = $true; packageZip = $own.packageZip; exe = $own.exe; dll = $own.dll } | ConvertTo-Json -Compress -Depth 4))
$h = (Get-FileHash -LiteralPath $bj -Algorithm SHA256).Hash.ToLowerInvariant()
# racestage (VENUE-STAGE-SAME-SHA-RESTAGE-GUARD-1): while this assemble ran, ANOTHER stager staged this sha (its own assemble) into $env:FAKE_RACE_CACHE,
# a build the leg job accepts (manifest fields + artifact digests). racestage-corrupt: the same, then the cached exe no longer hashes to its digest.
if ($env:FAKE_ASSEMBLE -like 'racestage*') {
    $claims = @{}
    foreach ($pair in @(@('exe', "MLVApp-playback-attr-3-cuda-$s.exe"), @('dll', "igpu_recon_cuda-playback-attr-3-cuda-$s.dll"), @('packageZip', "MLVApp-playback-attr-3-cuda-$s-pkg.zip"))) {
        $p = Join-Path $env:FAKE_RACE_CACHE $pair[1]
        [IO.File]::WriteAllText($p, "racer $($pair[1])")
        $claims[$pair[0]] = [ordered]@{ name = $pair[1]; sha256 = (Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash.ToLowerInvariant() }
    }
    if ($env:FAKE_ASSEMBLE -eq 'racestage-corrupt') { [IO.File]::WriteAllText((Join-Path $env:FAKE_RACE_CACHE "MLVApp-playback-attr-3-cuda-$s.exe"), 'corrupt') }
    # racestage-missing: the racer's cached exe is gone afterwards; racestage-upper: the racer's manifest spells the commit in upper case (the leg accepts it)
    if ($env:FAKE_ASSEMBLE -eq 'racestage-missing') { Remove-Item -LiteralPath (Join-Path $env:FAKE_RACE_CACHE "MLVApp-playback-attr-3-cuda-$s.exe") }
    $rm = [ordered]@{ sourceCommit = $(if ($env:FAKE_ASSEMBLE -eq 'racestage-upper') { $SourceCommit.ToUpperInvariant() } else { $SourceCommit }); assembledAtUtc = 'racer'; dllPairManifestSha256 = ('b' * 64); pendingSymbolPresence = $true; packageZip = $claims.packageZip; exe = $claims.exe; dll = $claims.dll }
    # racestage-array (r2, adversary A1 on 90b1c58e): the exe claim is a one-element ARRAY [{name,sha256}], a shape the leg job and the host guard accept
    if ($env:FAKE_ASSEMBLE -eq 'racestage-array') { $rm.exe = @($claims.exe) }
    # FAKE_ASSEMBLE_SLEEP: the assemble takes this many seconds more (the racer's stage lands meanwhile)
    if ($env:FAKE_ASSEMBLE_SLEEP) { Start-Sleep -Seconds ([int]$env:FAKE_ASSEMBLE_SLEEP) }
    [IO.File]::WriteAllText((Join-Path $env:FAKE_RACE_CACHE "playback-attr-3-cuda-$s-build.json"), ($rm | ConvertTo-Json -Compress -Depth 4))
    # racestage-locked (r3, adversary A2 on ce5a0380): the racer's cached exe is held open with FileShare.None for FAKE_LOCK_SEC s, so the soundness read's
    # artifact hash fails AT ONCE with nearly all of the cap left. The holder marks <FAKE_CALLS>.lockheld once it holds the file and .lockreleased after.
    if ($env:FAKE_ASSEMBLE -eq 'racestage-locked') {
        $exe = Join-Path $env:FAKE_RACE_CACHE "MLVApp-playback-attr-3-cuda-$s.exe"
        $hold = "`$fs = [IO.File]::Open('$exe', 'Open', 'Read', 'None'); [IO.File]::WriteAllText('$($env:FAKE_CALLS).lockheld', ''); Start-Sleep -Seconds $env:FAKE_LOCK_SEC; `$fs.Dispose(); [IO.File]::WriteAllText('$($env:FAKE_CALLS).lockreleased', '')"
        Start-Process -FilePath (Get-Process -Id $PID).Path -ArgumentList @('-NoProfile', '-NonInteractive', '-Command', $hold) -WindowStyle Hidden | Out-Null
        $t = [Diagnostics.Stopwatch]::StartNew()
        while (-not (Test-Path -LiteralPath "$($env:FAKE_CALLS).lockheld") -and $t.Elapsed.TotalSeconds -lt 60) { Start-Sleep -Milliseconds 100 }
    }
}
"RESULT=ASSEMBLE_OK SOURCE=$SourceCommit EXE=x DLL=x PKG=x MANIFEST=playback-attr-3-cuda-$s-build.json MANIFEST_SHA256=$h OUT=$OutDir"
exit 0
"""

# um-run.ps1 stand-in: the agent is simulated in-process. A JobId whose outbox result exists is refused exactly as UmRunDrop refuses it (UMRUN_JOBID_IN_USE).
# FAKE_UMRUN_<DLLPAIR|STAGE|SMOKE>: ok (default) | unresolved | retracted | fail | wrongcache (stage only: STAGE_OK, but the cache keeps other bytes) | inuse | inuse-all.
# The line also records the other two waits, the notAfterUtc the drop would write (LEASE-NOT-AFTER-SINGLE-LEG-SUBMITTERS-1; empty = none) and when it was
# called. LEASE-NOT-AFTER-SUBMITTER-DEADLINE-TIMING-1 r2: for the drop-relative form it computes that value the way UmRunDrop.psm1 does, min(now + budget,
# cap), refusing a passed cap (UMRUN_NOT_AFTER_CAP_PASSED, also forced by FAKE_UMRUN_<kind>=cappassed), and records the budget and cap it was handed.
FAKE_UMRUN = r"""
[CmdletBinding()] param([string]$ScriptPath, [string[]]$SideFile = @(), [string]$JobId = '', [string]$AgentShare = '', [int]$TimeoutSec = 0,
    [int]$MaxQueueWaitSec = 0, [int]$MaxClaimedWaitSec = 0, [int]$MaxAdmissionWaitSec = 0, [string]$NotAfterUtc = '', [int]$NotAfterBudgetSec = 0,
    [string]$NotAfterCapUtc = '')
$sides = @($SideFile | ForEach-Object { ([string]$_) -split ';' } | Where-Object { $_ })
$calledAt = [DateTime]::UtcNow
$na = $NotAfterUtc
if ($NotAfterBudgetSec -gt 0) {
    $naDt = $calledAt.AddSeconds($NotAfterBudgetSec)
    if ($NotAfterCapUtc) {
        $capDt = [DateTime]::Parse($NotAfterCapUtc, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime()
        if ($capDt -le $calledAt) { throw "UMRUN_NOT_AFTER_CAP_PASSED -NotAfterCapUtc '$NotAfterCapUtc' is not later than now; nothing was placed" }
        if ($capDt -lt $naDt) { $naDt = $capDt }
    }
    $na = $naDt.ToString('o')
}
Add-Content -LiteralPath $env:FAKE_CALLS -Value "UMRUN job=$JobId share=$AgentShare script=$ScriptPath side=$($sides -join ';') timeout=$TimeoutSec queue=$MaxQueueWaitSec admission=$MaxAdmissionWaitSec claimed=$MaxClaimedWaitSec notAfter=$na budget=$NotAfterBudgetSec cap=$NotAfterCapUtc called=$($calledAt.ToString('o'))"
$outbox = Join-Path $AgentShare 'outbox'; $cache = Join-Path $AgentShare 'cache'
New-Item -ItemType Directory -Force -Path $outbox, $cache | Out-Null
$res = Join-Path $outbox "$JobId.result.json"
if (Test-Path -LiteralPath $res) { throw "UMRUN_JOBID_IN_USE outbox already holds $JobId.result.json" }
$kind = $(if ($JobId -like 'playback-attr-3-cuda-dllpair-*') { 'DLLPAIR' } elseif ($JobId -like 'playback-attr-3-cuda-stage-*') { 'STAGE' } else { 'SMOKE' })
# FAKE_RECEIPT_READONLY=<kind>: this submission makes the driver's receipt file read-only, so every later append of that call fails (FAKE_RECEIPT names it)
if ($env:FAKE_RECEIPT_READONLY -ceq $kind) {
    if (-not (Test-Path -LiteralPath $env:FAKE_RECEIPT)) { New-Item -ItemType Directory -Force -Path (Split-Path -Parent $env:FAKE_RECEIPT) | Out-Null; [IO.File]::WriteAllText($env:FAKE_RECEIPT, '') }
    (Get-Item -LiteralPath $env:FAKE_RECEIPT).IsReadOnly = $true
}
$mode = [Environment]::GetEnvironmentVariable("FAKE_UMRUN_$kind"); if (-not $mode) { $mode = 'ok' }
# inuse: the unsuffixed JobId turns out spent at submission (a racing submitter won it); inuse-all: every JobId does
if ($mode -eq 'inuse-all' -or ($mode -eq 'inuse' -and $JobId -notmatch '-n\d{12}$')) { throw "UMRUN_JOBID_IN_USE outbox already holds $JobId.result.json" }
$sha = $env:FAKE_SHA
if ($mode -eq 'unresolved') { throw "UNRESOLVED: $JobId was claimed by the agent, but it stopped proving liveness (fixture) -- this client is stopping, not failing" }
if ($mode -eq 'retracted') { throw "RETRACTED: $JobId reached its queue wait ceiling (fixture); not a failure, retry with a new -JobId if desired" }
if ($mode -eq 'cappassed') { throw "UMRUN_NOT_AFTER_CAP_PASSED -NotAfterCapUtc '$NotAfterCapUtc' is not later than now (fixture); nothing was placed" }
if ($mode -eq 'placementstall') { throw "UMRUN_NOT_AFTER_PLACEMENT_STALL 300 s passed between the not-after clock read and the job's rename, more than 15 s on each of 4 reads (fixture); the job was not made visible" }
$code = 0
if ($mode -eq 'fail') { $code = 5; $out = 'RESULT=STAGE_FAILED STEP=fixture EXIT=5' }
elseif ($kind -eq 'DLLPAIR') {
    $art = Join-Path $outbox "$JobId.artifacts"
    New-Item -ItemType Directory -Force -Path $art | Out-Null
    [IO.File]::WriteAllText((Join-Path $art 'dll-pair-manifest.json'), (@{ sourceCommit = $sha; jobId = $JobId } | ConvertTo-Json -Compress))
    $out = "RESULT=DLL_PAIR_OK SOURCE=$sha ARCH=sm_86 RECON=x AMAZE=x ARTIFACTS=$art"
} elseif ($kind -eq 'STAGE') {
    # racestage-locked: the agent runs the stage once the racer's lock holder has let go (the copy below replaces the held exe)
    if (Test-Path -LiteralPath "$($env:FAKE_CALLS).lockheld") {
        $t = [Diagnostics.Stopwatch]::StartNew()
        while (-not (Test-Path -LiteralPath "$($env:FAKE_CALLS).lockreleased") -and $t.Elapsed.TotalSeconds -lt 60) { Start-Sleep -Milliseconds 100 }
    }
    foreach ($f in $sides) { Copy-Item -LiteralPath $f -Destination (Join-Path $cache (Split-Path -Leaf $f)) -Force }
    if ($mode -eq 'wrongcache') {
        $bj = @($sides | Where-Object { $_ -like '*-build.json' })[0]
        [IO.File]::WriteAllText((Join-Path $cache (Split-Path -Leaf $bj)), '{"sourceCommit":"' + $sha + '","assembledAtUtc":"an-earlier-assemble"}')
    }
    $out = "RESULT=STAGE_OK SOURCE=$sha FILES=4 CACHE=$cache ARTIFACTS=x"
} else {
    New-Item -ItemType Directory -Force -Path (Join-Path $cache "smoke-runner-$('b' * 16)") | Out-Null
    # already: the job found this exact closure in the cache (attr3-stage-smoke-runner-job.ps1 Complete-AlreadyStaged) and printed its ALREADY=1 form
    if ($mode -eq 'already') { $out = "RESULT=SMOKE_RUNNER_STAGE_OK ALREADY=1 CACHE_DIR=smoke-runner-$('b' * 16) PATH=x" }
    else { $out = "RESULT=SMOKE_RUNNER_STAGE_OK CACHE_DIR=smoke-runner-$('b' * 16) PATH=x FILES=8" }
}
$r = [ordered]@{ jobId = $JobId; exitCode = $code; stdout = $out; stderr = ''; timedOut = $false; cancelled = $false; queueExpired = $false; orphaned = $false
    startedUtc = [DateTime]::UtcNow.ToString('o'); endedUtc = [DateTime]::UtcNow.ToString('o') }
[IO.File]::WriteAllText($res, ($r | ConvertTo-Json -Compress))
[pscustomobject]$r
"""

FAKE_DISK_FLOOR = r"""
function Test-DiskFloor {
    param([double]$FreeGiB = -1, [double]$FloorGiB = 50, [string]$Caller = '', [string]$Card = '', [string]$ExemptReason = '')
    Add-Content -LiteralPath $env:FAKE_CALLS -Value "DISKFLOOR caller=$Caller card=$Card"
    if ($env:FAKE_DISK -eq 'refuse') {
        return [pscustomobject]@{ Ok = $false; FreeGiB = 42.1; FloorGiB = 50; Message = "DISK_FLOOR_REFUSED caller=$Caller card=$Card freeGiB=42.1 floorGiB=50 utc=fixture -- C: is under the long-run floor; free space first and re-dispatch" }
    }
    [pscustomobject]@{ Ok = $true; FreeGiB = 61.5; FloorGiB = 50; Message = "DISK_FLOOR_OK caller=$Caller card=$Card freeGiB=61.5 floorGiB=50 utc=fixture" }
}
"""

FAKE_COMMIT_GATE = r"""
function Test-HostCommitGate {
    param([double]$CommitGiB = -1, [double]$LimitGiB = -1, [double]$RamGiB = -1, [string]$Caller = '', [string]$Card = '', [string]$ExemptReason = '', [scriptblock]$Probe = $null)
    Add-Content -LiteralPath $env:FAKE_CALLS -Value "COMMITGATE caller=$Caller card=$Card"
    if ($env:FAKE_COMMIT -eq 'refuse') {
        return [pscustomobject]@{ Ok = $false; Code = 'HOST_COMMIT_REFUSED'; CommitGiB = 58.2; LimitGiB = 60; CeilingGiB = 56; Message = "HOST_COMMIT_REFUSED caller=$Caller card=$Card commitGiB=58.2 limitGiB=60 ceilingGiB=56 utc=fixture -- shared host over the commit ceiling; REFUSED: commit 97 % of limit" }
    }
    [pscustomobject]@{ Ok = $true; Code = 'HOST_COMMIT_OK'; CommitGiB = 20.0; LimitGiB = 60; CeilingGiB = 56; Message = "HOST_COMMIT_OK caller=$Caller card=$Card commitGiB=20 limitGiB=60 ceilingGiB=56 utc=fixture" }
}
"""


def ok_emissions(text: str, token: str) -> tuple[list[str], int]:
    """SMOKE-RUNNER-STAGE-OK-ALREADY-TOKEN-1 contract pin: the RESULT=<token> lines a job prints as plain double-quoted Write-Output (which the pin renders),
    and a census of every bare <token> in its code whatever the quoting, writer or concatenation (adversary r1 pass 1 A1 and pass 2 A1). A census above
    the line count is an emission the pin cannot render and check."""
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    lines = re.findall(r'^\s*Write-Output "(RESULT=' + token + r'(?:\s[^"]*)?)"\s*$', code, re.M)
    return lines, len(re.findall(r"(?<![A-Z_])" + token + r"(?![A-Z_])", code))


def psq(value: object) -> str:
    """A PowerShell single-quoted literal body: a quote in a temp path (C:\\Temp\\O'Neil) doubles, never ends the string (adversary r1 pass 2 A2)."""
    return str(value).replace("'", "''")


def utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f0Z")


def process_start_iso(pid: int) -> str:
    out = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", f"(Get-Process -Id {pid}).StartTime.ToUniversalTime().ToString('o')"],
                         capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    return out.stdout.strip()


def pid_alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    return str(pid) in out.stdout


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@requires_windows_pwsh
class VenueStageNightlyTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="um-stage-")
        self.addCleanup(tmp.cleanup)
        self.case = Path(tmp.name)
        self.receipt = self.case / ".claude-state" / "dual-venue" / "stage-nightly.jsonl"
        self.claims = self.case / "claims"
        self.share = self.case / "share"
        (self.share / "cache").mkdir(parents=True)
        (self.share / "outbox").mkdir()
        self.build_root = self.case / "lane-um-nightly-stage"
        self.gates = self.case / "gates"
        self.gates.mkdir()
        (self.gates / "disk-floor.ps1").write_text(FAKE_DISK_FLOOR, encoding="utf-8")
        (self.gates / "host-commit-gate.ps1").write_text(FAKE_COMMIT_GATE, encoding="utf-8")
        self.stubs = self.case / "stubs"
        self.stubs.mkdir()
        for name, text in (("um-run.ps1", FAKE_UMRUN), ("dll-gen.ps1", FAKE_DLLGEN), ("assemble.ps1", FAKE_ASSEMBLE), ("stage-gen.ps1", FAKE_STAGEGEN),
                           ("smoke-gen.ps1", FAKE_SMOKEGEN)):
            (self.stubs / name).write_text(text, encoding="utf-8")
        self.calls_log = self.case / "calls.log"
        # The driver refuses a -BuildRoot inside its checkout. A lane's TEMP can sit inside the worktree (.claude-state\ls), so every case passes its own
        # empty git checkout through the -RepoRoot seam: the refusal is then judged against that checkout, never against where TEMP happens to live.
        self.checkout = self.case / "checkout"
        self.checkout.mkdir()
        subprocess.run(["git", "-C", str(self.checkout), "init", "-q"], check=True, capture_output=True, timeout=60, stdin=subprocess.DEVNULL)

    # ---------------------------------------------------------------- harness
    def args(self, *extra: str, share: Path | None = None, commit: str | None = SHA, now: str | None = NOW, driver: Path | None = None,
             repo: Path | None = None, probe: str = "20") -> list[str]:
        a = [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(driver or DRIVER), "-RepoRoot", str(repo or self.checkout),
             "-ReceiptFile", str(self.receipt), "-BuildRoot", str(self.build_root),
             "-AgentShare", str(share or self.share), "-ClaimDir", str(self.claims), "-GateDir", str(self.gates),
             "-UmRunScript", str(self.stubs / "um-run.ps1"), "-DllJobScript", str(self.stubs / "dll-gen.ps1"), "-AssembleScript", str(self.stubs / "assemble.ps1"),
             "-StageJobScript", str(self.stubs / "stage-gen.ps1"), "-SmokeRunnerJobScript", str(self.stubs / "smoke-gen.ps1"), "-ProbeTimeoutSec", probe]
        if commit:
            a += ["-SourceCommit", commit]
        if now:
            a += ["-NowUtc", now]
        return a + list(extra)

    def run_driver(self, *extra: str, timeout: int = 300, env: dict[str, str] | None = None, **kw) -> subprocess.CompletedProcess:
        e = dict(os.environ)
        e.update({"FAKE_CALLS": str(self.calls_log), "FAKE_SHA": kw.get("commit") or SHA})
        e.update(env or {})
        return subprocess.run(self.args(*extra, **kw), capture_output=True, text=True, timeout=timeout, env=e, stdin=subprocess.DEVNULL)

    def calls(self, role: str | None = None) -> list[str]:
        lines = self.calls_log.read_text(encoding="utf-8").splitlines() if self.calls_log.exists() else []
        return [l for l in lines if role is None or l.startswith(role + " ")]

    def submissions(self) -> list[str]:
        return self.calls("UMRUN")

    def submitted_jobs(self) -> list[str]:
        return [re.search(r"job=(\S+)", c).group(1) for c in self.submissions()]

    def submitted_sides(self) -> dict[str, list[str]]:
        return {re.search(r"job=(\S+)", c).group(1): [s for s in re.search(r" side=(.*?) timeout=", c).group(1).split(";") if s] for c in self.submissions()}

    def assert_no_jobid_twice(self) -> None:
        # SameJobIdRulingPremiseTests (test_attr3_footage_stage.py) trusts this driver as a um-run caller because it never submits one JobId twice in a call.
        jobs = self.submitted_jobs()
        self.assertEqual(len(jobs), len(set(jobs)), jobs)

    def rows(self) -> list[dict]:
        return [json.loads(l) for l in self.receipt.read_text(encoding="utf-8").splitlines() if l.strip()] if self.receipt.exists() else []

    def result(self, stdout: str) -> dict:
        line = [l for l in stdout.splitlines() if l.startswith("STAGE_NIGHTLY_RESULT=")]
        self.assertTrue(line, stdout)
        return json.loads(line[-1][len("STAGE_NIGHTLY_RESULT="):])

    def assert_exit(self, p: subprocess.CompletedProcess, token: str) -> None:
        code = 0 if token in ("STAGE_OK", "STAGE_SKIP_ALREADY", "NIGHT_ALREADY_BUILT", "PLAN_ONLY") else \
            3 if token in ("DISK_FLOOR_REFUSED", "HOST_COMMIT_REFUSED", "BUILD_GUARD_REFUSED") else EXIT[token]
        self.assertEqual(p.returncode, code, p.stdout + p.stderr)
        tail = [l for l in p.stdout.splitlines() if l.strip()][-2:]
        self.assertTrue(tail[0].startswith(f"STAGE_NIGHTLY_EXIT code={code} reason={token} "), tail)
        self.assertTrue(tail[1].startswith("STAGE_NIGHTLY_RESULT="), tail)

    def nothing_built_or_submitted(self) -> None:
        for role in ("DLLGEN", "ASSEMBLE", "STAGEGEN", "UMRUN"):
            self.assertEqual(self.calls(role), [], role)

    def stage_cache(self, sha: str = SHA, runner: bool = True, artifacts: bool = True, body: bytes | None = None, exe_claim: object = None) -> Path:
        # r4 (adversary pass 6 A2 on 3bec2622): step 4 decides "already in the cache" by the shared soundness read (Get-VenueStagedBuildManifest
        # -VerifyArtifacts), so the default fixture is a SOUND staged build: the fields the leg job's validator reads and a scalar 64-hex digest per artifact
        # that the cached bytes (b"x") hash to. -exe_claim replaces the exe claim (a shape the leg refuses); -body replaces the whole manifest.
        n = names(sha)
        bj = self.share / "cache" / n["build"]
        if body is None:
            digest = hashlib.sha256(b"x").hexdigest()
            claim = {k: {"name": n[f], "sha256": digest} for k, f in (("packageZip", "pkg"), ("exe", "exe"), ("dll", "dll"))}
            if exe_claim is not None:
                claim["exe"] = exe_claim
            body = json.dumps({"sourceCommit": sha, "assembledAtUtc": "2026-10-19T01:02:03Z", "dllPairManifestSha256": "b" * 64, "pendingSymbolPresence": True,
                               **claim}).encode()
        bj.write_bytes(body)
        if artifacts:
            for k in ("exe", "dll", "pkg"):
                (self.share / "cache" / n[k]).write_bytes(b"x")
        if runner:
            (self.share / "cache" / RUNNER_DIR).mkdir(exist_ok=True)
        return bj

    def built_dll_pair(self, sha: str = SHA) -> Path:
        jid = f"playback-attr-3-cuda-dllpair-{sha[:12]}"
        art = self.share / "outbox" / f"{jid}.artifacts"
        art.mkdir(parents=True)
        (art / "dll-pair-manifest.json").write_text(json.dumps({"sourceCommit": sha, "jobId": jid}), encoding="utf-8")
        (self.share / "outbox" / f"{jid}.result.json").write_text(json.dumps({"jobId": jid, "exitCode": 0, "stdout": f"RESULT=DLL_PAIR_OK SOURCE={sha} ARCH=sm_86",
                                                                              "timedOut": False, "endedUtc": "2026-10-18T10:00:00Z"}), encoding="utf-8")
        return art

    def assembled_manifest_sha(self, stdout_or_calls: str | None = None) -> str:
        bj = list(self.build_root.glob(f"build-{S12}*/playback-attr-3-cuda-{S12}-build.json"))
        self.assertEqual(len(bj), 1, bj)
        return sha256(bj[0])

    # ---------------------------------------------------------------- the plan (-WhatIf)
    def test_whatif_prints_the_four_steps_and_touches_nothing(self) -> None:
        p = self.run_driver("-WhatIf", share=self.case / "no-such-share")
        self.assert_exit(p, "PLAN_ONLY")
        self.assertIn(f"STAGE_NIGHTLY_PLAN venue=ultra-magnus sourceCommit={SHA} nightKey={NIGHT}", p.stdout)
        steps = re.findall(r"^STAGE_NIGHTLY_PLAN_STEP (\d)/4 (\S+)", p.stdout, re.M)
        self.assertEqual(steps, [("1", "dll-pair"), ("2", "assemble"), ("3", "stage"), ("4", "smoke-runner")], p.stdout)
        self.assertIn(str(self.build_root / f"build-{S12}"), p.stdout)
        self.assertIn(f"playback-attr-3-cuda-{S12}-build.json", p.stdout)
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.receipt.exists())
        self.assertFalse(self.build_root.exists())

    def test_default_source_commit_is_the_local_fork_master_ref(self) -> None:
        repo = self.case / "repo"
        repo.mkdir()
        git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL).stdout.strip()
        git("init", "-q")
        git("config", "user.email", "unit@example.invalid")
        git("config", "user.name", "unit")
        (repo / "f.txt").write_text("x", encoding="utf-8")
        git("add", "f.txt")
        git("commit", "-q", "-m", "one")
        head = git("rev-parse", "HEAD")
        unresolved = self.run_driver("-WhatIf", repo=repo, commit=None)
        self.assert_exit(unresolved, "SOURCE_COMMIT_UNRESOLVED")
        git("update-ref", "refs/remotes/fork/master", head)
        ok = self.run_driver("-WhatIf", repo=repo, commit=None)
        self.assert_exit(ok, "PLAN_ONLY")
        self.assertIn(f"sourceCommit={head}", ok.stdout)

    def test_receipt_file_outside_claude_state_is_refused(self) -> None:
        if ".claude-state" in self.case.resolve().parts:
            self.skipTest("TEMP sits under a .claude-state directory (lane scratch), so no temp path is outside one; hosted CI runs this case")
        a = self.args()
        a[a.index("-ReceiptFile") + 1] = str(self.case / "elsewhere" / "stage.jsonl")
        p = subprocess.run(a, capture_output=True, text=True, timeout=120, env=dict(os.environ, FAKE_CALLS=str(self.calls_log)), stdin=subprocess.DEVNULL)
        self.assertEqual(p.returncode, EXIT["USAGE"], p.stdout + p.stderr)
        self.assertIn(".claude-state", p.stdout)
        self.assertEqual(self.calls(), [])

    # ---------------------------------------------------------------- a fresh night: the four tracked steps
    def test_a_fresh_sha_runs_the_four_steps_and_records_the_by_name_cache_hash(self) -> None:
        p = self.run_driver()
        self.assert_exit(p, "STAGE_OK")
        self.assertEqual(len(self.calls("DLLGEN")), 1)
        self.assertEqual(len(self.calls("ASSEMBLE")), 1)
        jobs = [re.search(r"job=(\S+)", c).group(1) for c in self.submissions()]
        self.assertEqual(jobs, [f"playback-attr-3-cuda-dllpair-{S12}", f"playback-attr-3-cuda-stage-{S12}", f"attr3-stage-smoke-runner-{S12}-bbbbbbbbbbbb"])
        for c in self.submissions():
            self.assertIn(f"share={self.share}", c)
        stage_call = self.submissions()[1]
        for n in names().values():
            self.assertIn(n, stage_call)      # the four inboxSideFiles travel with the stage job
        # the assembler built into <BuildRoot>\build-<sha12> from the DLL pair the agent published
        self.assertIn(f"out={self.build_root / ('build-' + S12)}", self.calls("ASSEMBLE")[0])
        self.assertIn(f"dll={self.share / 'outbox' / ('playback-attr-3-cuda-dllpair-' + S12 + '.artifacts')}", self.calls("ASSEMBLE")[0])
        cache_hash = sha256(self.share / "cache" / names()["build"])
        self.assertEqual(cache_hash, self.assembled_manifest_sha())
        rows = self.rows()
        # the reservation is persisted before the first staging step; the call's own row closes it
        self.assertEqual([x["outcome"] for x in rows], ["STAGE_RESERVED", "STAGE_OK"], rows)
        self.assertEqual((rows[0]["sourceCommit"], rows[0]["nightKey"], rows[0]["exitCode"]), (SHA, NIGHT, None))
        self.assertEqual(rows[1]["reservationId"], rows[0]["reservationId"])
        r = rows[1]
        self.assertEqual(r["schema"], "mlv-app/venue-stage-nightly/v1")
        for key in ("utc", "nightKey", "venue", "sourceCommit", "outcome", "exitCode", "buildManifestSha256", "smokeRunnerCacheDir", "jobIds", "timings",
                    "cFreeGiB", "commitGiB", "buildDir", "detail"):
            self.assertIn(key, r, key)
        self.assertEqual((r["outcome"], r["exitCode"], r["venue"], r["sourceCommit"], r["nightKey"]), ("STAGE_OK", 0, "ultra-magnus", SHA, NIGHT))
        self.assertEqual(r["buildManifestSha256"], cache_hash)
        self.assertEqual(r["smokeRunnerCacheDir"], RUNNER_DIR)
        self.assertEqual(r["jobIds"]["stage"], f"playback-attr-3-cuda-stage-{S12}")
        for k in ("dllPairSec", "assembleSec", "stageSec", "runnerSec", "totalSec"):
            self.assertIn(k, r["timings"], k)
        self.assertIn("start", r["cFreeGiB"])
        self.assertIn("end", r["cFreeGiB"])
        self.assertIn(f"manifest={cache_hash}", p.stdout)
        self.assertEqual(self.result(p.stdout)["buildManifestSha256"], cache_hash)
        # both floors were asked, before the first build step
        self.assertEqual(len(self.calls("DISKFLOOR")), 1)
        self.assertEqual(len(self.calls("COMMITGATE")), 1)
        log = self.calls()
        self.assertLess(log.index(self.calls("COMMITGATE")[0]), log.index(self.calls("ASSEMBLE")[0]))
        self.assert_no_jobid_twice()

    def test_a_smoke_runner_job_that_finds_its_closure_cached_is_staged_not_failed(self) -> None:
        # SMOKE-RUNNER-STAGE-OK-ALREADY-TOKEN-1: the job prints RESULT=SMOKE_RUNNER_STAGE_OK ALREADY=1 CACHE_DIR=... when the cache already holds its
        # closure (#813 r5 ended SMOKE_RUNNER_STAGE_FAILED on that line although the build was staged). That is a staged runner, never a failure.
        p = self.run_driver(env={"FAKE_UMRUN_SMOKE": "already"})
        self.assert_exit(p, "STAGE_OK")
        self.assertIn(f"attr3-stage-smoke-runner-{S12}-bbbbbbbbbbbb", self.submitted_jobs())
        self.assertIn(f"smoke-runner result: RESULT=SMOKE_RUNNER_STAGE_OK ALREADY=1 CACHE_DIR={RUNNER_DIR}", p.stdout)
        self.assertEqual(self.rows()[-1]["smokeRunnerCacheDir"], RUNNER_DIR)

    # ---------------------------------------------------------------- LEASE-NOT-AFTER-SINGLE-LEG-SUBMITTERS-1: the notAfterUtc each submit hands um-run
    def test_the_stage_job_not_after_is_the_drivers_deadline_and_the_harvested_jobs_get_none(self) -> None:
        # The driver kills um-run at its -MaxTotalSec deadline, so the stage job (re-stageable under a suffix by a later call) gets that deadline as its
        # notAfterUtc: the remaining budget, never earlier than a wait it passes. The DLL pair and the smoke runner are harvested BY NAME by a later call
        # that cannot re-submit their single-use JobId, so they get the later bound: none (Get-DvJobNotAfter -LaterHarvest).
        t0 = datetime.now(timezone.utc)
        p = self.run_driver("-MaxTotalSec", "600")
        t1 = datetime.now(timezone.utc)
        self.assert_exit(p, "STAGE_OK")
        o_utc = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{7}Z$")

        def parse_o(text: str) -> datetime:
            m = re.match(r"^(.*T\d{2}:\d{2}:\d{2})\.(\d{7})Z$", text)
            self.assertIsNotNone(m, text)
            return datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc) + timedelta(microseconds=int(m.group(2)[:6]))

        seen = {}
        for c in self.submissions():
            m = re.search(r" job=(\S+) .* timeout=(\d+) queue=(\d+) admission=(\d+) claimed=(\d+) notAfter=(\S*) budget=(\d+) cap=(\S*) called=(\S+)$", c)
            self.assertIsNotNone(m, c)
            seen[m.group(1)] = (int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5)), m.group(6), int(m.group(7)), m.group(8), parse_o(m.group(9)))
        stage = seen[f"playback-attr-3-cuda-stage-{S12}"]
        timeout, queue, admission, claimed, not_after, budget, cap, called = stage
        # LEASE-NOT-AFTER-SUBMITTER-DEADLINE-TIMING-1 r2: the bound is handed DROP-RELATIVE (-NotAfterBudgetSec), capped by the deadline (-NotAfterCapUtc)
        self.assertRegex(cap, o_utc, "the stage submit passes its deadline as -NotAfterCapUtc in UTC 'o' form")
        self.assertGreaterEqual(budget, timeout + queue + admission + claimed, "the budget is every wait it passes (plus the slack)")
        self.assertRegex(not_after, o_utc, "the drop writes notAfterUtc in UTC 'o' form")
        na = parse_o(not_after)
        self.assertEqual(na, parse_o(cap), "the waits outlast the deadline, so the drop writes the deadline: um-run is killed there")
        self.assertGreater(na, called)
        for w in (timeout, queue, admission, claimed):
            self.assertGreaterEqual(na, called + timedelta(seconds=w) - timedelta(seconds=1), f"never earlier than the {w} s wait it passes")
        # the deadline: startedUtc + 600, with startedUtc inside [t0, t1]; the submit floors what is left of it to whole seconds
        self.assertGreaterEqual(na, t0 + timedelta(seconds=600 - 1))
        self.assertLessEqual(na, t1 + timedelta(seconds=600))
        self.assertLessEqual(abs((na - called).total_seconds() - queue), 1.5, "notAfter is the remaining budget the queue wait is also given")
        for jid in (f"playback-attr-3-cuda-dllpair-{S12}", f"attr3-stage-smoke-runner-{S12}-bbbbbbbbbbbb"):
            self.assertEqual((seen[jid][4], seen[jid][5], seen[jid][6]), ("", 0, ""),
                             f"{jid} is harvested by name by a later call that cannot re-submit it: no notAfter in either form")

    def test_a_scheduling_delay_before_the_bound_never_lets_it_pass_the_deadline(self) -> None:
        # LEASE-NOT-AFTER-SUBMITTER-DEADLINE-TIMING-1 (CoS Dot 07bd1383 (b)): the driver samples what is left of its deadline, then computes the bound.
        # -TestNotAfterSampleDelayMs holds it between the two (a scheduling delay); the bound is still the ORIGINAL deadline, never the sample counted
        # again from a later now (which would overshoot by the delay).
        p = self.run_driver("-MaxTotalSec", "600", "-TestNotAfterSampleDelayMs", "2500")
        self.assert_exit(p, "STAGE_OK")
        o_utc = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{7}Z"

        def parse_o(text: str) -> datetime:
            m = re.match(r"^(.*T\d{2}:\d{2}:\d{2})\.(\d{7})Z$", text)
            self.assertIsNotNone(m, text)
            return datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc) + timedelta(microseconds=int(m.group(2)[:6]))

        stage_id = f"playback-attr-3-cuda-stage-{S12}"
        said = [l for l in p.stdout.splitlines() if f" submit stage {stage_id} " in l]
        self.assertEqual(len(said), 1, p.stdout)
        # r2: the driver says the drop-relative form it hands um-run: drop + budget, capped at the deadline
        m = re.search(rf" notAfter=drop\+(\d+)s cap=({o_utc}) \(caller-deadline\) deadline=({o_utc})$", said[0])
        self.assertIsNotNone(m, said[0])
        cap, deadline = parse_o(m.group(2)), parse_o(m.group(3))
        self.assertEqual(cap, deadline, "the cap is the ORIGINAL deadline")
        handed = [c for c in self.submissions() if f" job={stage_id} " in c]
        self.assertEqual(len(handed), 1)
        self.assertIn(f" budget={m.group(1)} cap={m.group(2)} ", handed[0], "um-run is handed exactly that budget and cap")
        na = parse_o(re.search(rf" notAfter=({o_utc}) ", handed[0]).group(1))
        self.assertLessEqual(na, deadline, "never later than the driver's own deadline")
        self.assertEqual(na, deadline, "the deadline itself: um-run is killed there")
        # the delay really sat between the sample and um-run: the queue wait is floor(deadline - sample) and um-run was called at least 2.5 s after the
        # sample, so the queue wait outlasts what was left at the call by more than 1.5 s (the old bound, sample + later now, lands past the deadline)
        called = parse_o(re.search(rf" called=({o_utc})$", handed[0]).group(1))
        queue = int(re.search(r" queue=(\d+) ", handed[0]).group(1))
        self.assertGreaterEqual(queue, (deadline - called).total_seconds() + 1.5 - 0.01)

    def test_the_driver_bounds_each_submit_through_the_one_helper(self) -> None:
        text = DRIVER.read_text(encoding="utf-8")
        fn = text[text.index("function Invoke-UmRun("):text.index("# ------------------------------------------------------------------ 6. DLL pair")]
        self.assertEqual(fn.count("Get-DvJobNotAfter"), 1)
        self.assertIn("-CallerCapSec $remain", fn)
        # the bound is the ORIGINAL absolute deadline (LEASE-NOT-AFTER-SUBMITTER-DEADLINE-TIMING-1), not the sampled remaining counted from a later now
        self.assertIn("-CallerDeadlineUtc $script:Deadline", fn)
        # r2: handed drop-relative (budget and cap from the helper), the absolute form only when the helper gives no budget
        self.assertIn("-NotAfterBudgetSec $($notAfter.budgetSec)", fn)
        self.assertIn("' -NotAfterCapUtc ' + (& $lit $notAfter.capUtc)", fn)
        self.assertIn("-NotAfterUtc", fn)
        self.assertEqual(len(re.findall(r"Invoke-UmRun '(?:dllpair|smoke-runner)' .*-LaterHarvest", text)), 2, "the two harvested submits say so")
        self.assertEqual(len(re.findall(r"Invoke-UmRun 'stage' .*-LaterHarvest", text)), 0)

    def test_a_cap_the_drop_finds_passed_is_a_typed_cap_exit(self) -> None:
        # LEASE-NOT-AFTER-SUBMITTER-DEADLINE-TIMING-1 r2: the drop refuses a -NotAfterCapUtc (the deadline) that passed before its claim, before any share
        # write. The driver maps that to STAGE_CAP_EXCEEDED, never the step's generic failure.
        p = self.run_driver(env={"FAKE_UMRUN_STAGE": "cappassed"})
        self.assert_exit(p, "STAGE_CAP_EXCEEDED")
        # r5 (Grok G5): since r3/r4 the cap can also pass during placement, so the text says what the drop guarantees in both cases
        self.assertIn("deadline passed before the job was made visible; nothing was submitted", p.stdout + p.stderr)
        self.assertIn("UMRUN_NOT_AFTER_CAP_PASSED", p.stdout + p.stderr)

    def test_a_placement_stall_the_drop_refuses_is_a_typed_nothing_submitted_exit(self) -> None:
        # LEASE-NOT-AFTER-SUBMITTER-DEADLINE-TIMING-1 r5 (Grok G5): UMRUN_NOT_AFTER_PLACEMENT_STALL (r4) means the share stalled through every re-derivation and
        # the job never became visible, so it is SHARE_UNREACHABLE (nothing was submitted), never the step's generic failure
        p = self.run_driver(env={"FAKE_UMRUN_STAGE": "placementstall"})
        self.assert_exit(p, "SHARE_UNREACHABLE")
        self.assertIn("the share stalled while the job was being placed; the job was never made visible, nothing was submitted", p.stdout + p.stderr)
        self.assertIn("UMRUN_NOT_AFTER_PLACEMENT_STALL", p.stdout + p.stderr)

    # ---------------------------------------------------------------- the owner-footage gate: side files are the build package, never anything else
    def test_side_files_are_only_this_shas_build_package_under_the_build_root(self) -> None:
        p = self.run_driver()
        self.assert_exit(p, "STAGE_OK")
        sides = self.submitted_sides()
        dll_id = f"playback-attr-3-cuda-dllpair-{S12}"
        self.assertEqual([Path(s).name for s in sides[dll_id]], [f"{dll_id}-source.zip"])
        stage = sides[f"playback-attr-3-cuda-stage-{S12}"]
        self.assertEqual(sorted(Path(s).name for s in stage), sorted(names().values()))
        for s in stage:
            self.assertEqual(Path(s).parent, self.build_root / f"build-{S12}", s)
        self.assertEqual(sides[f"attr3-stage-smoke-runner-{S12}-bbbbbbbbbbbb"], [])
        for s in sides[dll_id] + stage:
            self.assertTrue(Path(s).resolve().is_relative_to(self.build_root.resolve()), s)

    SIDES_LINE = "$sides = @($names.packageZipName, $names.exeName, $names.reconName, $names.buildManifestName | ForEach-Object { Join-Path $buildDir $_ })"

    def assert_widened_side_files_are_refused(self, widened: str) -> None:
        # The driver's own refusal, exercised on a copy whose side-file list was widened (the copy loads the real modules from the real dual-venue dir).
        text = DRIVER.read_text(encoding="utf-8")
        here = "$here = $PSScriptRoot"
        self.assertEqual((text.count(here), text.count(self.SIDES_LINE)), (1, 1))
        mutated = self.case / "mutated-driver" / "Invoke-VenueStageNightly.ps1"
        mutated.parent.mkdir()
        mutated.write_text(text.replace(here, f"$here = '{DV}'").replace(self.SIDES_LINE, widened), encoding="utf-8")
        self.built_dll_pair()
        p = self.run_driver(driver=mutated)
        self.assert_exit(p, "STAGE_FAILED")
        self.assertIn("refused to hand um-run the side file", p.stdout)
        self.assertEqual(self.submissions(), [])

    def test_a_side_file_with_a_name_outside_the_build_package_is_refused_before_um_run(self) -> None:
        self.assert_widened_side_files_are_refused(self.SIDES_LINE + "; $sides += (Join-Path $buildDir 'not-the-build-package.txt')")

    def test_a_build_package_name_outside_the_build_root_is_refused_before_um_run(self) -> None:
        self.assert_widened_side_files_are_refused(self.SIDES_LINE.replace("Join-Path $buildDir $_", f"Join-Path '{self.case / 'elsewhere'}' $_"))

    # ---------------------------------------------------------------- the same-JobId premise: never one JobId twice in a call
    def test_a_stage_jobid_in_use_at_submission_is_restaged_once_under_a_new_id(self) -> None:
        self.built_dll_pair()
        p = self.run_driver(env={"FAKE_UMRUN_STAGE": "inuse"})
        self.assert_exit(p, "STAGE_OK")
        stage_jobs = [j for j in self.submitted_jobs() if j.startswith("playback-attr-3-cuda-stage-")]
        self.assertEqual(len(stage_jobs), 2, stage_jobs)
        self.assertEqual(stage_jobs[0], f"playback-attr-3-cuda-stage-{S12}")
        self.assertRegex(stage_jobs[1], rf"^playback-attr-3-cuda-stage-{S12}-n\d{{12}}$")
        self.assert_no_jobid_twice()

    def test_a_second_in_use_refusal_is_stage_failed_with_no_third_submission(self) -> None:
        self.built_dll_pair()
        p = self.run_driver(env={"FAKE_UMRUN_STAGE": "inuse-all"})
        self.assert_exit(p, "STAGE_FAILED")
        stage_jobs = [j for j in self.submitted_jobs() if j.startswith("playback-attr-3-cuda-stage-")]
        self.assertEqual(len(stage_jobs), 2, stage_jobs)
        self.assert_no_jobid_twice()
        self.assertFalse(any("smoke-runner" in j for j in self.submitted_jobs()))

    def test_a_dll_pair_already_built_is_harvested_not_resubmitted(self) -> None:
        art = self.built_dll_pair()
        p = self.run_driver()
        self.assert_exit(p, "STAGE_OK")
        self.assertEqual(self.calls("DLLGEN"), [])
        self.assertFalse(any("dllpair" in c for c in self.submissions()), self.submissions())
        self.assertIn(f"dll={art}", self.calls("ASSEMBLE")[0])

    def test_a_suffixed_dll_pair_is_harvested_through_read_share_and_never_resubmitted(self) -> None:
        # UM-DLLPAIR-VCVARS-PATH-OVERFLOW-1 r2 (Grok G3 on #819): the end-to-end path. The default id holds a FAILED result (it refuses that sha's pair for
        # good) and <default>-r2 holds a clean pair. `-DllJobIdSuffix r2` must reach Get-DllPairReuse through Read-Share and $DllReuseByName
        # (Invoke-VenueStageNightly.ps1 :547 / :561) as (base id, suffix): a swapped or dropped argument looks up the failed default id (DLLPAIR_FAILED)
        # or a doubled <default>-r2-r2 (result-missing, so a resubmit). Either reds this case.
        base = f"playback-attr-3-cuda-dllpair-{S12}"
        failed = self.share / "outbox" / f"{base}.result.json"
        failed.write_text(json.dumps({"jobId": base, "exitCode": 9, "stdout": "FAIL step=msvcEnvironment exit=9 reason=INCLUDE_LACKS_UCRT",
                                      "timedOut": False, "endedUtc": "2026-11-14T10:00:00Z"}), encoding="utf-8")
        failed_bytes = failed.read_bytes()
        jid = f"{base}-r2"
        art = self.share / "outbox" / f"{jid}.artifacts"
        art.mkdir(parents=True)
        (art / "dll-pair-manifest.json").write_text(json.dumps({"sourceCommit": SHA, "jobId": jid}), encoding="utf-8")
        (self.share / "outbox" / f"{jid}.result.json").write_text(json.dumps({"jobId": jid, "exitCode": 0, "stdout": f"RESULT=DLL_PAIR_OK SOURCE={SHA} ARCH=sm_86",
                                                                              "timedOut": False, "endedUtc": "2026-11-15T03:00:00Z"}), encoding="utf-8")
        p = self.run_driver("-DllJobIdSuffix", "r2")
        self.assert_exit(p, "STAGE_OK")
        self.assertEqual(self.calls("DLLGEN"), [], "a harvested pair is never regenerated")
        self.assertFalse(any("dllpair" in c for c in self.submissions()), self.submissions())
        self.assertIn(f"dll={art}", self.calls("ASSEMBLE")[0])
        self.assertIn(f"DLL pair harvested: RESULT=DLL_PAIR_OK SOURCE={SHA}", p.stdout)
        r = self.rows()[-1]
        self.assertEqual((r["jobIds"]["dllPair"], r["steps"]["dllPair"]), (jid, "harvested"), r)
        self.assertEqual(failed.read_bytes(), failed_bytes, "the failed default id's result is never rewritten")

    def dllgen_argv(self) -> list[str]:
        # NIGHTLY-DLLGEN-ARGV-PIN-1: the exact argument list (after the script path) the driver handed the DLL-pair generator, as the stub recorded it.
        calls = self.calls("DLLGEN")
        self.assertEqual(len(calls), 1, calls)
        return calls[0].split(" argv=", 1)[1].split("|")

    def test_dllgen_argv_without_a_suffix_is_exactly_source_commit_and_out_dir(self) -> None:
        # fable key3 r8 B1 (refuted by execution, closed anyway): no -DllJobIdSuffix means no -JobIdSuffix and no stray (empty) element before -OutDir.
        p = self.run_driver()
        self.assert_exit(p, "STAGE_OK")
        argv = self.dllgen_argv()
        self.assertEqual(argv[:2], ["-SourceCommit", SHA], argv)
        self.assertEqual(len(argv), 4, argv)
        self.assertEqual(argv[2], "-OutDir", argv)
        self.assertTrue(argv[3].endswith("dllpair"), argv)

    def test_dllgen_argv_with_a_suffix_is_exactly_source_commit_job_id_suffix_and_out_dir(self) -> None:
        p = self.run_driver("-DllJobIdSuffix", "r2")
        self.assert_exit(p, "STAGE_OK")
        argv = self.dllgen_argv()
        self.assertEqual(argv[:4], ["-SourceCommit", SHA, "-JobIdSuffix", "r2"], argv)
        self.assertEqual(len(argv), 6, argv)
        self.assertEqual(argv[4], "-OutDir", argv)
        self.assertTrue(argv[5].endswith("dllpair"), argv)
        self.assertIn(f"playback-attr-3-cuda-dllpair-{S12}-r2", self.submitted_jobs())

    # ---------------------------------------------------------------- idempotency
    def test_an_already_staged_sha_is_skipped_with_no_build_and_no_submission(self) -> None:
        bj = self.stage_cache()
        p = self.run_driver()
        self.assert_exit(p, "STAGE_SKIP_ALREADY")
        self.nothing_built_or_submitted()
        self.assertEqual(len(self.calls("SMOKEGEN")), 1)       # the local generator only names the runner dir
        self.assertEqual(self.calls("DISKFLOOR"), [])          # nothing to build, so no floor is asked
        r = self.rows()[-1]
        self.assertEqual((r["outcome"], r["exitCode"]), ("STAGE_SKIP_ALREADY", 0))
        self.assertEqual(r["buildManifestSha256"], sha256(bj))
        self.assertEqual(r["smokeRunnerCacheDir"], RUNNER_DIR)

    def test_a_staged_build_without_its_runner_stages_only_the_runner(self) -> None:
        bj = self.stage_cache(runner=False)
        before = sha256(bj)
        p = self.run_driver()
        self.assert_exit(p, "STAGE_OK")
        for role in ("DLLGEN", "ASSEMBLE", "STAGEGEN"):
            self.assertEqual(self.calls(role), [], role)
        self.assertEqual([re.search(r"job=(\S+)", c).group(1) for c in self.submissions()], [f"attr3-stage-smoke-runner-{S12}-bbbbbbbbbbbb"])
        self.assertEqual(self.rows()[-1]["buildManifestSha256"], before)

    # ---------------------------------------------------------------- the #508 shape (RED first)
    def assert_restaged_under_a_suffix(self, p: subprocess.CompletedProcess) -> None:
        self.assert_exit(p, "STAGE_OK")
        stage_jobs = [re.search(r"job=(\S+)", c).group(1) for c in self.submissions() if "job=playback-attr-3-cuda-stage-" in c]
        self.assertEqual(len(stage_jobs), 1, self.submissions())
        self.assertRegex(stage_jobs[0], rf"^playback-attr-3-cuda-stage-{S12}-n\d{{12}}$")
        self.assertRegex(self.calls("STAGEGEN")[-1], r"suffix=n\d{12}$")
        self.assertNotIn("reused", p.stdout.lower().replace("not reused", ""))
        cache_hash = sha256(self.share / "cache" / names()["build"])
        self.assertEqual(cache_hash, self.assembled_manifest_sha())
        r = self.rows()[-1]
        self.assertEqual((r["outcome"], r["buildManifestSha256"], r["jobIds"]["stage"]), ("STAGE_OK", cache_hash, stage_jobs[0]))

    def spend_default_stage_jobid(self) -> None:
        jid = f"playback-attr-3-cuda-stage-{S12}"
        (self.share / "outbox" / f"{jid}.result.json").write_text(json.dumps({"jobId": jid, "exitCode": 0, "timedOut": False,
                                                                              "stdout": f"RESULT=STAGE_OK SOURCE={SHA} FILES=4", "endedUtc": "2026-10-18T15:40:00Z"}), encoding="utf-8")

    def test_508_default_jobid_spent_and_cache_missing_restages_under_a_suffix(self) -> None:
        self.built_dll_pair()
        self.spend_default_stage_jobid()
        self.assert_restaged_under_a_suffix(self.run_driver())

    def test_508_default_jobid_spent_and_cache_holds_another_commit_restages_under_a_suffix(self) -> None:
        self.built_dll_pair()
        self.spend_default_stage_jobid()
        self.stage_cache(runner=False, body=json.dumps({"sourceCommit": OTHER_SHA}).encode())
        self.assert_restaged_under_a_suffix(self.run_driver())

    # ---------------------------------------------------------------- VENUE-STAGE-SAME-SHA-RESTAGE-GUARD-1 (RED first)
    def assert_racer_reused(self, mode: str) -> None:
        p = self.run_driver(env={"FAKE_ASSEMBLE": mode, "FAKE_RACE_CACHE": str(self.share / "cache")})
        self.assert_exit(p, "STAGE_OK")
        bj = self.share / "cache" / names()["build"]
        self.assertEqual(json.loads(bj.read_text(encoding="utf-8"))["assembledAtUtc"], "racer", "the racer's staged manifest was replaced")
        racer = sha256(bj)
        self.assertEqual(self.calls("STAGEGEN"), [])
        self.assertFalse([c for c in self.submissions() if "job=playback-attr-3-cuda-stage-" in c], self.submissions())
        self.assertRegex(p.stdout, rf"(?m)STAGE_REUSED_EXISTING sha={S12} manifest={racer[:8]} ")
        r = self.rows()[-1]
        self.assertEqual((r["outcome"], r["buildManifestSha256"], r["steps"]["stage"]), ("STAGE_OK", racer, "reused-existing"))

    def test_a_build_another_stager_staged_during_the_assemble_is_reused_never_replaced(self) -> None:
        # 2026-11-13: two stagers both found 23a07757 NOT_STAGED and both assembled; the later one's stage replaced the first one's
        # manifest under a running lease batch. The cache is read again right before the stage submission, and a staged build is reused.
        self.assert_racer_reused("racestage")

    def test_a_raced_in_build_with_an_upper_case_commit_is_reused(self) -> None:
        # adversary A2 (72577e87): the leg (and the batch's reader) compare sourceCommit case-insensitively, so this is a sound build; a
        # case-sensitive re-check sent it to the stage job, which refuses to replace a sound build (STAGE_FAILED).
        self.assert_racer_reused("racestage-upper")

    def test_a_raced_in_build_missing_an_artifact_is_restaged_not_refused(self) -> None:
        # adversary A3 (72577e87): a build.json whose exe is gone is not a build a leg can run; this call already assembled one, so it stages it
        # (the stage job replaces an unsound build) instead of stopping STAGE_FAILED for the hub.
        p = self.run_driver(env={"FAKE_ASSEMBLE": "racestage-missing", "FAKE_RACE_CACHE": str(self.share / "cache")})
        self.assert_exit(p, "STAGE_OK")
        self.assertEqual(len([c for c in self.submissions() if "job=playback-attr-3-cuda-stage-" in c]), 1, self.submissions())
        self.assertEqual(sha256(self.share / "cache" / names()["build"]), self.assembled_manifest_sha())
        self.assertIn("BUILD_ARTIFACT_MISSING", p.stdout)

    def test_the_pre_submission_soundness_read_is_bounded_by_the_probe_timeout(self) -> None:
        # adversary A4 (72577e87): every share probe of this driver is bounded by -ProbeTimeoutSec; the soundness read too (a read that does not
        # answer in time is not a reuse, and the stage job decides on the host). r2 (A3 on 90b1c58e): by min(-ProbeTimeoutSec, what is left of the cap).
        text = DRIVER.read_text(encoding="utf-8")
        calls = re.findall(r"Get-VenueStagedBuildManifest -AgentShare [^\r\n}]*", text)
        self.assertEqual(len(calls), 1, calls)
        self.assertIn("-TimeoutSec $soundSec ", calls[0])
        self.assertIn('$soundSec = Get-ProbeSec ', text)
        # r6 (Grok G6/G7 on #814): the artifact hash has its own budget, the plan preflight's 300 s (Invoke-VenueLeaseBatch.ps1), bounded by what is left of the cap
        self.assertIn("-ArtifactTimeoutSec 300 -CallCapSec $capSec ", calls[0])

    def test_a_raced_in_build_the_leg_job_would_refuse_is_not_reused(self) -> None:
        # adversary A2 (b47fcfbc): the staged build.json names the sha and every artifact name is there, but the cached exe does not hash to its digest.
        # Reusing it would record STAGE_OK for a build every leg refuses; it goes to the stage job instead (which replaces only an unsound build).
        p = self.run_driver(env={"FAKE_ASSEMBLE": "racestage-corrupt", "FAKE_RACE_CACHE": str(self.share / "cache")})
        self.assert_exit(p, "STAGE_OK")
        self.assertNotIn("STAGE_REUSED_EXISTING", p.stdout)
        self.assertEqual(len([c for c in self.submissions() if "job=playback-attr-3-cuda-stage-" in c]), 1, self.submissions())
        self.assertEqual(sha256(self.share / "cache" / names()["build"]), self.assembled_manifest_sha())
        self.assertIn("BUILD_ARTIFACT_MISMATCH", p.stdout)

    # ---------------------------------------------------------------- r2 (adversary A1-A3 on 90b1c58e)
    def test_a_raced_in_build_whose_exe_claim_is_a_one_element_array_is_reused(self) -> None:
        # A1: the leg job reads $buildManifest.exe.sha256 (member access) and so does the host guard, so exe=[{name,sha256}] is a sound build. The shared
        # reader (Get-VenueStagedBuildManifest, which the lease batch guards with too) required a single object, so the nightly re-staged over it.
        self.assert_racer_reused("racestage-array")
        self.assertIsInstance(json.loads((self.share / "cache" / names()["build"]).read_text(encoding="utf-8"))["exe"], list)

    def test_a_soundness_read_given_up_on_does_not_hold_the_process(self) -> None:
        # A2: the soundness read is stuck (a blocking SMB read: Thread.Sleep, which a stop request cannot interrupt) past -ProbeTimeoutSec. It is given up on
        # (not reused; this call's build is staged), and the process ends when the driver does, never when the abandoned read finally returns.
        stuck = 60
        t0 = time.monotonic()
        p = self.run_driver("-TestSoundnessReadDelayMs", str(stuck * 1000), probe="5",
                            env={"FAKE_ASSEMBLE": "racestage", "FAKE_RACE_CACHE": str(self.share / "cache")})
        took = time.monotonic() - t0
        self.assert_exit(p, "STAGE_OK")
        self.assertNotIn("STAGE_REUSED_EXISTING", p.stdout)
        self.assertRegex(p.stdout, r"is not reused: state=UNREACHABLE ")
        self.assertLess(took, stuck - 15, f"the process waited {took:.1f} s for the abandoned {stuck} s soundness read")

    def test_the_soundness_read_never_runs_past_the_total_cap(self) -> None:
        # A3: -MaxTotalSec is the total wall-clock cap. The soundness read starts a few seconds before it and would answer (sound racer, reusable) after it;
        # it gets what is LEFT of the cap, never the full -ProbeTimeoutSec, so the call ends STAGE_CAP_EXCEEDED at the cap, never STAGE_OK past it.
        cap, slow = 25, 15
        # the finding's input: the DLL pair is reusable and the smoke-runner dir is already in the cache (no submission needs its 30 s floor)
        self.built_dll_pair()
        (self.share / "cache" / RUNNER_DIR).mkdir()
        t0 = time.monotonic()
        p = self.run_driver("-MaxTotalSec", str(cap), "-TestSoundnessReadDelayMs", str(slow * 1000),
                            env={"FAKE_ASSEMBLE": "racestage", "FAKE_ASSEMBLE_SLEEP": "10", "FAKE_RACE_CACHE": str(self.share / "cache")})
        took = time.monotonic() - t0
        # the racer landed and the soundness read was asked (the call reached step 8 inside the cap: the test's own premise)
        self.assertIn("ASSEMBLE ", "\n".join(self.calls("ASSEMBLE")), p.stdout)
        self.assertTrue((self.share / "cache" / names()["build"]).exists(), p.stdout)
        self.assert_exit(p, "STAGE_CAP_EXCEEDED")
        self.assertNotIn("STAGE_REUSED_EXISTING", p.stdout)
        self.assertFalse([c for c in self.submissions() if "job=playback-attr-3-cuda-stage-" in c], self.submissions())
        self.assertEqual(json.loads((self.share / "cache" / names()["build"]).read_text(encoding="utf-8"))["assembledAtUtc"], "racer")
        self.assertLess(took, cap + 8, f"the call ran {took:.1f} s against a {cap} s cap")

    def test_an_unreadable_soundness_answer_with_budget_left_goes_to_the_stage_job(self) -> None:
        # r3 (adversary A2 on ce5a0380): -MaxTotalSec 120, -ProbeTimeoutSec 300, and the racer's cached exe is held with FileShare.None, so the soundness
        # read's hash fails AT ONCE with about 100 s of the cap left. r2 called that STAGE_CAP_EXCEEDED only because the read was GIVEN less than
        # -ProbeTimeoutSec; the cap had not run out, so the read is an ordinary unreadable answer and the stage job decides on the host.
        def lock_released() -> None:
            # the holder lets go after FAKE_LOCK_SEC whatever the driver did, so the case directory can be removed
            held, released, t0 = Path(f"{self.calls_log}.lockheld"), Path(f"{self.calls_log}.lockreleased"), time.monotonic()
            while held.exists() and not released.exists() and time.monotonic() - t0 < 60:
                time.sleep(0.2)
            time.sleep(0.5)
        self.addCleanup(lock_released)
        self.built_dll_pair()
        (self.share / "cache" / RUNNER_DIR).mkdir()
        p = self.run_driver("-MaxTotalSec", "120", probe="300",
                            env={"FAKE_ASSEMBLE": "racestage-locked", "FAKE_LOCK_SEC": "8", "FAKE_RACE_CACHE": str(self.share / "cache")})
        self.assertNotIn("STAGE_CAP_EXCEEDED", p.stdout)
        self.assert_exit(p, "STAGE_OK")
        self.assertRegex(p.stdout, r"is not reused: state=UNREACHABLE ")
        self.assertEqual(len([c for c in self.submissions() if "job=playback-attr-3-cuda-stage-" in c]), 1, self.submissions())

    def test_the_shared_soundness_read_takes_every_claim_shape_the_leg_takes(self) -> None:
        # A1 siblings (Invoke-VenueLeaseBatch.ps1 plan preflight and post-claim guard take the same read): a one-element array claim is the leg's digest
        # ($buildManifest.exe.sha256 enumerates to it); a two-element array is not one digest, and the leg refuses it, so the read does too.
        cache = self.share / "cache"
        n = names()
        claims = {}
        for k, f in (("packageZip", n["pkg"]), ("exe", n["exe"]), ("dll", n["dll"])):
            (cache / f).write_bytes(b"a")
            claims[k] = {"name": f, "sha256": hashlib.sha256(b"a").hexdigest()}
        out = {}
        for label, exe in (("one", [claims["exe"]]), ("two", [claims["exe"], claims["exe"]])):
            (cache / n["build"]).write_text(json.dumps({"sourceCommit": SHA, "dllPairManifestSha256": "b" * 64, "pendingSymbolPresence": True,
                                                        "packageZip": claims["packageZip"], "exe": exe, "dll": claims["dll"]}), encoding="utf-8")
            ps = (f"Import-Module '{DRIVER.parent / 'VenueChain.psm1'}' -Force -DisableNameChecking; "
                  f"$r = Get-VenueStagedBuildManifest -AgentShare '{self.share}' -SourceCommit '{SHA}' -VerifyArtifacts -TimeoutSec 30; "
                  "'STATE=' + $r.state; 'CONTENT=[' + $r.contentProblem + ']'; 'ARTIFACTS=[' + $r.artifactProblem + ']'")
            p = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
            out[label] = p.stdout
        self.assertIn("STATE=STAGED\nCONTENT=[]\nARTIFACTS=[]", out["one"].replace("\r\n", "\n"))
        self.assertIn("ATTRCUDA_BUILD_MANIFEST_ARTIFACT_SHA_MISSING", out["two"])
        self.assertIn("for exe ", out["two"])

    def test_every_share_probe_is_bounded_by_what_is_left_of_the_cap(self) -> None:
        # A3 siblings: every by-name read (Read-Share, the reachability probe, the soundness read) takes min(-ProbeTimeoutSec, what is left of -MaxTotalSec).
        text = DRIVER.read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"Invoke-Bounded [^\r\n]*\$ProbeTimeoutSec", text), [])
        self.assertEqual(re.findall(r"-TimeoutSec \(\[double\]\$ProbeTimeoutSec\)", text), [])
        # and an abandoned read is counted, so the one exit ends the process instead of waiting for it (A2)
        self.assertIn("Complete-VenueProcess -ExitCode $code", text)

    # ---------------------------------------------------------------- r4 (adversary pass 6 A2 on 3bec2622, RED first)
    def assert_step4_unsound_build_restaged(self, runner: bool) -> None:
        # A2: step 4 (STAGE_SKIP_ALREADY, and the in-cache branch that stages only the runner) decided "in the cache" from a build.json that parses and
        # names the sha plus three artifact NAMES. A staged build whose exe claim is sha256=[h] (an array, which the narrowed leg refuses) was reported
        # staged. Step 4 now takes the shared soundness read: this build is not in the cache, so the call assembles and stages its own.
        self.built_dll_pair()
        self.stage_cache(runner=runner, exe_claim={"name": names()["exe"], "sha256": [hashlib.sha256(b"x").hexdigest()]})
        p = self.run_driver()
        self.assert_exit(p, "STAGE_OK")
        self.assertIn("ATTRCUDA_BUILD_MANIFEST_ARTIFACT_SHA_MISSING", p.stdout)
        self.assertEqual(len(self.calls("ASSEMBLE")), 1, p.stdout)
        self.assertEqual(len([c for c in self.submissions() if "job=playback-attr-3-cuda-stage-" in c]), 1, self.submissions())
        cache_hash = sha256(self.share / "cache" / names()["build"])
        self.assertEqual(cache_hash, self.assembled_manifest_sha())
        r = self.rows()[-1]
        self.assertEqual((r["outcome"], r["buildManifestSha256"], r["steps"]["stage"]), ("STAGE_OK", cache_hash, "staged"))

    def test_step4_a_staged_build_the_leg_refuses_is_not_skipped_as_already_staged(self) -> None:
        self.assert_step4_unsound_build_restaged(runner=True)

    def test_step4_a_staged_build_the_leg_refuses_is_not_kept_when_only_the_runner_is_missing(self) -> None:
        self.assert_step4_unsound_build_restaged(runner=False)

    def test_step4_a_sound_build_with_an_upper_case_commit_is_skipped(self) -> None:
        # the leg compares sourceCommit case-insensitively (adversary A2 on 72577e87), so step 4's in-cache answer does too: one predicate, every reader
        bj = self.stage_cache()
        m = json.loads(bj.read_text(encoding="utf-8"))
        m["sourceCommit"] = SHA.upper()
        bj.write_text(json.dumps(m), encoding="utf-8")
        p = self.run_driver()
        self.assert_exit(p, "STAGE_SKIP_ALREADY")
        self.nothing_built_or_submitted()
        self.assertEqual(self.rows()[-1]["buildManifestSha256"], sha256(bj))

    def test_step4_soundness_read_that_does_not_answer_is_typed_and_builds_nothing(self) -> None:
        # bounded like every share probe: min(-ProbeTimeoutSec, what is left of the cap). A read that does not answer with time left is SHARE_UNREACHABLE
        # (nothing reserved, built or submitted); one that got the rest of the cap is STAGE_CAP_EXCEEDED.
        self.stage_cache()
        for extra, probe, token in ((("-TestSoundnessReadDelayMs", "60000"), "5", "SHARE_UNREACHABLE"),
                                    (("-TestSoundnessReadDelayMs", "60000", "-MaxTotalSec", "12"), "60", "STAGE_CAP_EXCEEDED")):
            with self.subTest(token=token):
                self.calls_log.unlink(missing_ok=True)
                t0 = time.monotonic()
                p = self.run_driver(*extra, probe=probe)
                took = time.monotonic() - t0
                self.assert_exit(p, token)
                self.nothing_built_or_submitted()
                self.assertNotIn("STAGE_RESERVED", [r["outcome"] for r in self.rows()])
                self.assertLess(took, 40, f"the call ran {took:.1f} s")

    # ---------------------------------------------------------------- r4 fix iteration (adversary pass 7 A1-A2 on 24f85549, RED first)
    def test_an_unsound_build_the_cache_already_holds_is_never_recorded_staged(self) -> None:
        # A1: step 4 refuses the [H] exe claim, the local build dir holds the same manifest (an earlier call assembled it), step 8 refuses it again,
        # and the spent default stage JobId's "cache holds the expected hash" fallback skipped the submission: the hash-only final verify recorded STAGE_OK
        # for a build every leg refuses. The final verify takes the one soundness read: an unsound final build is STAGE_VERIFY_MISMATCH, never STAGE_OK.
        h = hashlib.sha256(b"x").hexdigest()
        bj = self.stage_cache(exe_claim={"name": names()["exe"], "sha256": [h]})
        local = self.build_root / f"build-{S12}"
        local.mkdir(parents=True)
        shutil.copy2(bj, local / names()["build"])
        for k in ("exe", "dll", "pkg"):
            (local / names()[k]).write_bytes(b"x")
        self.built_dll_pair()
        self.spend_default_stage_jobid()
        p = self.run_driver()
        self.assert_exit(p, "STAGE_VERIFY_MISMATCH")
        self.assertIn("ATTRCUDA_BUILD_MANIFEST_ARTIFACT_SHA_MISSING", p.stdout)
        self.assertNotIn("STAGE_OK", [r["outcome"] for r in self.rows()])
        self.assertIsNone(self.rows()[-1]["buildManifestSha256"])

    def test_the_probe_floor_leaves_a_healthy_soundness_read_room_to_hash(self) -> None:
        # A2: -ProbeTimeoutSec 1 was accepted, but the soundness read needs the manifest read plus -ArtifactMinSec (1 s) to start hashing, so a healthy
        # staged build was SHARE_UNREACHABLE (41) at every run. The floor is 5 s: 1 is refused at binding (nothing probed or written), 5 skips the healthy build.
        self.stage_cache()
        low = self.run_driver(probe="1")
        self.assertNotEqual(low.returncode, EXIT["SHARE_UNREACHABLE"], low.stdout)
        self.assertNotIn("STAGE_NIGHTLY_EXIT", low.stdout)
        self.assertIn("ProbeTimeoutSec", low.stdout + low.stderr)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.rows(), [])
        ok = self.run_driver(probe="5")
        self.assert_exit(ok, "STAGE_SKIP_ALREADY")

    # ---------------------------------------------------------------- r6 (Grok G6/G7 on #814, RED first)
    def test_the_final_verify_artifact_hash_past_the_probe_timeout_with_cap_left_is_stage_ok(self) -> None:
        # G7: the final verify takes the one soundness read, whose artifact hash shared min(-ProbeTimeoutSec, cap left) with the manifest read. A real stage
        # landed, the share needed longer than -ProbeTimeoutSec to hash the cached package, and the night ended SHARE_UNREACHABLE with its manifest dropped.
        # The hash has its own budget (the plan preflight's 300 s), bounded by what is left of -MaxTotalSec: STAGE_OK, with the manifest recorded.
        p = self.run_driver("-TestSoundnessArtifactDelayMs", "8000", "-TestSoundnessArtifactDelayAt", "final", probe="5")
        self.assert_exit(p, "STAGE_OK")
        cache_hash = sha256(self.share / "cache" / names()["build"])
        r = self.rows()[-1]
        self.assertEqual((r["outcome"], r["buildManifestSha256"]), ("STAGE_OK", cache_hash))
        self.assertIn(f"manifest={cache_hash}", p.stdout)

    def test_step4_artifact_hash_past_the_probe_timeout_with_cap_left_is_skip_already(self) -> None:
        # G6: an already soundly staged sha whose cached package hashes slower than -ProbeTimeoutSec over the share is STAGE_SKIP_ALREADY, never SHARE_UNREACHABLE
        bj = self.stage_cache()
        p = self.run_driver("-TestSoundnessArtifactDelayMs", "8000", probe="5")
        self.assert_exit(p, "STAGE_SKIP_ALREADY")
        self.nothing_built_or_submitted()
        self.assertEqual(self.rows()[-1]["buildManifestSha256"], sha256(bj))

    def test_the_artifact_hash_budget_is_still_bounded_by_the_cap(self) -> None:
        # G6/G7: the hash's own budget never runs past -MaxTotalSec. A hash the cap cuts short is STAGE_CAP_EXCEEDED (as a manifest read the cap cuts short
        # is), whether the cap is under -ProbeTimeoutSec or over it; nothing is reserved, built or submitted, and the call ends near the cap.
        self.stage_cache()
        for cap, probe in (("12", "60"), ("20", "5")):
            with self.subTest(cap=cap, probe=probe):
                self.calls_log.unlink(missing_ok=True)
                t0 = time.monotonic()
                p = self.run_driver("-TestSoundnessArtifactDelayMs", "60000", "-MaxTotalSec", cap, probe=probe)
                took = time.monotonic() - t0
                self.assert_exit(p, "STAGE_CAP_EXCEEDED")
                self.nothing_built_or_submitted()
                self.assertNotIn("STAGE_RESERVED", [r["outcome"] for r in self.rows()])
                self.assertLess(took, int(cap) + 10, f"the call ran {took:.1f} s against a {cap} s cap")

    # ---------------------------------------------------------------- r9 (Grok G9/G14 on #814, adversary A1/A2 on be203fe6)
    STARTS = "the soundness read of the staged build of {sha} {when} starts"

    def test_r9_g14_a_cap_under_1s_at_the_step4_soundness_read_ends_the_call_before_any_read(self) -> None:
        # G14: the cap has 0.5 s left after the read's probe bound was taken (-TestSoundnessCapLeftSec). The guard ends the call STAGE_CAP_EXCEEDED (exit 50)
        # before Get-VenueStagedBuildManifest is called (its "starts" line is never printed), with the step-4 wording; nothing is built or submitted.
        self.stage_cache()
        p = self.run_driver("-TestSoundnessCapLeftSec", "0.5", "-TestSoundnessCapAt", "first", probe="5")
        self.assert_exit(p, "STAGE_CAP_EXCEEDED")
        self.assertEqual(p.returncode, 50)
        self.assertRegex(p.stdout, rf"cap ran out before the soundness read of the staged build of {SHA} before anything was built could start \(0(\.\d+)? s left\); nothing was submitted")
        self.assertNotIn(self.STARTS.format(sha=SHA, when="before anything was built"), p.stdout, "Get-VenueStagedBuildManifest was called")
        self.assertNotIn("ran to the end of", p.stdout)
        self.nothing_built_or_submitted()

    def test_r9_g14_a_cap_under_1s_at_the_final_verify_soundness_read_ends_the_call_before_any_read(self) -> None:
        # G14 + G13: the same at the final verify of a call that submitted its stage job; the refusal says the stage job was submitted and nothing is recorded as staged
        p = self.run_driver("-TestSoundnessCapLeftSec", "0.5", "-TestSoundnessCapAt", "final", probe="5")
        self.assert_exit(p, "STAGE_CAP_EXCEEDED")
        self.assertEqual(p.returncode, 50)
        self.assertRegex(p.stdout, rf"cap ran out before the soundness read of the staged build of {SHA} at the final verify could start \(0(\.\d+)? s left\); the stage job was submitted; not recorded as staged")
        self.assertNotIn(self.STARTS.format(sha=SHA, when="at the final verify"), p.stdout, "Get-VenueStagedBuildManifest was called")
        self.assertEqual(len(self.calls("STAGEGEN")), 1, "the stage job was emitted and submitted before the final verify")
        self.assertNotIn("STAGE_OK", [r["outcome"] for r in self.rows()])

    def test_r9_a2_a_final_verify_refusal_of_a_call_that_submitted_no_stage_job_says_so(self) -> None:
        # adversary A2 on be203fe6: the build was already in the cache (only its runner was missing), so no stage job was submitted; the final verify's refusal never
        # claims one was
        self.stage_cache(runner=False)
        p = self.run_driver("-TestSoundnessCapLeftSec", "0.5", "-TestSoundnessCapAt", "final", probe="5")
        self.assert_exit(p, "STAGE_CAP_EXCEEDED")
        self.assertRegex(p.stdout, r"at the final verify could start \(0(\.\d+)? s left\); this call submitted no stage job; not recorded as staged")
        self.assertNotIn("the stage job was submitted", p.stdout)

    def test_r9_a1_a_staged_answer_that_arrives_after_the_deadline_is_never_acted_on(self) -> None:
        # adversary A1 on be203fe6: the read's cap sample left 4 s, then a 5 s scheduling delay (-TestSoundnessPostCapDelayMs) came before the call. The module bounds
        # the read on its own clock from the call, so a SOUND build answered STAGED after the deadline, and step 4 recorded STAGE_SKIP_ALREADY (exit 0) past the cap.
        self.stage_cache()
        p = self.run_driver("-TestSoundnessCapLeftSec", "4", "-TestSoundnessPostCapDelayMs", "5000", "-TestSoundnessCapAt", "first", probe="5")
        self.assert_exit(p, "STAGE_CAP_EXCEEDED")
        # r12 (adversary A1 on 7754d16e): the nightly now hands the deadline itself over (-CallDeadlineUtc), so the call starts with the deadline already past and makes
        # no read at all; before r12 it read and answered STAGED after the deadline, and only the late-answer check below the call refused it.
        self.assertRegex(p.stdout, rf"the soundness read of the staged build of {SHA} before anything was built ran to the end of the \d+ s cap .*the staged manifest could not be read: no time is left of -CallCapSec [0-9.]+ \(-CallDeadlineUtc [0-9T:.-]+Z\).*; nothing was submitted")
        self.assertNotIn("answered STAGED", p.stdout)
        self.assertNotIn("STAGE_SKIP_ALREADY", [r["outcome"] for r in self.rows()])
        self.nothing_built_or_submitted()

    def test_r12_the_cap_seam_never_moves_the_deadline_later(self) -> None:
        # r12 fix iteration 2 (adversary A1 on 95274c68, Invoke-VenueStageNightly.ps1:287): -TestSoundnessCapLeftSec SET the deadline to now + its value, so 30 s
        # under a 10 s -MaxTotalSec moved the cap ~20 s later and a 15 s artifact hash ended STAGE_SKIP_ALREADY (exit 0) past the real cap. The seam may only bring
        # the deadline nearer: the read runs into the 10 s cap and the call ends STAGE_CAP_EXCEEDED.
        self.stage_cache()
        t0 = time.monotonic()
        p = self.run_driver("-MaxTotalSec", "10", "-TestSoundnessCapLeftSec", "30", "-TestSoundnessCapAt", "first",
                            "-TestSoundnessArtifactDelayMs", "15000", "-TestSoundnessArtifactDelayAt", "first", probe="5")
        took = time.monotonic() - t0
        self.assert_exit(p, "STAGE_CAP_EXCEEDED")
        self.assertNotIn("STAGE_SKIP_ALREADY", [r["outcome"] for r in self.rows()])
        self.assertLess(took, 25.0, p.stdout)

    def test_r9_g9_the_artifact_delay_seam_takes_its_read_in_any_case(self) -> None:
        # G9: ValidateSet is case-insensitive, so FINAL and First bind; with -ceq the seam silently dropped the delay. Each spelling delays its read (the call takes
        # the 8 s) and ends with the lowercase run's verdict (test_the_final_verify_artifact_hash_past_the_probe_timeout_with_cap_left_is_stage_ok, STAGE_OK).
        t0 = time.monotonic()
        p = self.run_driver("-TestSoundnessArtifactDelayMs", "8000", "-TestSoundnessArtifactDelayAt", "FINAL", probe="5")
        took = time.monotonic() - t0
        self.assert_exit(p, "STAGE_OK")
        self.assertGreaterEqual(took, 8.0, f"FINAL dropped the delay: the call took {took:.1f} s")

    def test_r9_g9_the_artifact_delay_seam_takes_its_first_read_in_any_case(self) -> None:
        # G9: the same for First at step 4 (the lowercase run is test_step4_artifact_hash_past_the_probe_timeout_with_cap_left_is_skip_already, STAGE_SKIP_ALREADY)
        self.stage_cache()
        t0 = time.monotonic()
        p = self.run_driver("-TestSoundnessArtifactDelayMs", "8000", "-TestSoundnessArtifactDelayAt", "First", probe="5")
        took = time.monotonic() - t0
        self.assert_exit(p, "STAGE_SKIP_ALREADY")
        self.assertGreaterEqual(took, 8.0, f"First dropped the delay: the call took {took:.1f} s")
        self.nothing_built_or_submitted()

    # ---------------------------------------------------------------- r6 fix iteration (adversary pass on 424df284, A1-A2, RED first)
    def test_a_source_commit_with_a_trailing_newline_is_refused_at_binding_everywhere(self) -> None:
        # A1: ValidatePattern('^[0-9a-f]{40}$') accepted '<40 hex>\n' ('$' matches before a final newline). The stage job then derived the same cache
        # file name as the clean sha but another stage-lock key, and its never-replace guard read the resident clean manifest as COMMIT_MISMATCH (unsound)
        # and replaced it. Every sha40 parameter in the venue chain anchors with \z, and the lock itself validates its sha.
        roots = [ROOT / "tools" / "profiling" / d for d in ("bachelor", "dual-venue", "ultramagnus")]
        loose = [f"{p.relative_to(ROOT)}:{i}" for r in roots for p in sorted(r.rglob("*.ps*1"))
                 for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1) if "ValidatePattern('^[0-9a-f]{40}$')" in line]
        self.assertEqual(loose, [])
        lf = SHA + "`n"
        ps = (f"Import-Module '{DRIVER.parent / 'VenueChain.psm1'}' -Force -DisableNameChecking; "
              f"Import-Module '{DRIVER.parent.parent / 'bachelor' / 'AttrCudaArtifacts.psm1'}' -Force -DisableNameChecking; "
              f"foreach ($c in @({{ Enter-AttrCudaStageLock -Root '{self.case}' -SourceCommit \"{lf}\" -WaitSec 1 }}, {{ Get-AttrCudaArtifactNames -SourceCommit \"{lf}\" }}, "
              f"{{ Get-VenueStagedBuildManifest -AgentShare '{self.share}' -SourceCommit \"{lf}\" -TimeoutSec 5 }})) {{ "
              "try { $null = & $c; 'ACCEPTED' } catch { 'REFUSED ' + ($_.FullyQualifiedErrorId -split ',')[0] } }")
        p = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        self.assertEqual(p.stdout.split(), ["REFUSED", "ParameterArgumentValidationError"] * 3, p.stdout + p.stderr)

    def test_call_cap_sec_bounds_the_manifest_read_too(self) -> None:
        # A2: -CallCapSec bounded only the artifact hash. With an absent agent root, -TimeoutSec 3 and -CallCapSec 1, a manifest read stuck 2.2 s answered
        # NOT_STAGED after the whole-call cap. The manifest read now gets min(-TimeoutSec, -CallCapSec): it is given up on at the cap, UNREACHABLE.
        ps = (f"Import-Module '{DRIVER.parent / 'VenueChain.psm1'}' -Force -DisableNameChecking; $sw = [Diagnostics.Stopwatch]::StartNew(); "
              f"$r = Get-VenueStagedBuildManifest -AgentShare '{self.case / 'adversary-empty-root'}' -SourceCommit '{SHA}' -TimeoutSec 3 -ArtifactTimeoutSec 300 "
              "-CallCapSec 1 -VerifyArtifacts -TestReadDelayMs 2200; 'STATE=' + $r.state; 'SEC=' + $sw.Elapsed.TotalSeconds.ToString('0.000', [Globalization.CultureInfo]::InvariantCulture)")
        p = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        self.assertIn("STATE=UNREACHABLE", p.stdout, p.stdout + p.stderr)
        sec = float(re.search(r"SEC=([0-9.]+)", p.stdout).group(1))
        self.assertLess(sec, 1.8, p.stdout)

    def test_r10_a3_the_time_before_the_read_is_counted_once(self) -> None:
        # r10 (adversary A3 on e9e6c210, VenueChain.psm1:749): with -ArtifactTimeoutSec 0 and -CallCapSec, the hash got $readSec - elapsed, and $readSec was
        # already min(-TimeoutSec, what was left of -CallCapSec), so the time spent before the read was subtracted twice. The test-only -TestPreReadDelayMs
        # spends 4 s before the read: 20 - 4 - read is left, over -ArtifactMinSec 12; the double count left ~11 (RED: "11.79 s left, under 12").
        share = self.case / "a3-share"
        (share / "cache").mkdir(parents=True)
        claim = {k: {"name": f"{k}.bin", "sha256": c * 64} for k, c in (("packageZip", "a"), ("exe", "b"), ("dll", "c"))}
        (share / "cache" / f"playback-attr-3-cuda-{SHA[:12]}-build.json").write_text(
            json.dumps({"sourceCommit": SHA, "dllPairManifestSha256": "d" * 64, "pendingSymbolPresence": True, **claim}), encoding="utf-8")
        ps = (f"Import-Module '{DRIVER.parent / 'VenueChain.psm1'}' -Force -DisableNameChecking; "
              f"$r = Get-VenueStagedBuildManifest -AgentShare '{share}' -SourceCommit '{SHA}' -TimeoutSec 20 -CallCapSec 20 -ArtifactMinSec 12 -VerifyArtifacts "
              "-TestPreReadDelayMs 4000 -ArtifactProbeScript { param($items) }; 'STATE=' + $r.state + ' ARTIFACT=[' + $r.artifactProblem + '] ERROR=[' + $r.error + ']'")
        p = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        self.assertIn("STATE=STAGED ARTIFACT=[] ERROR=[]", p.stdout, p.stdout + p.stderr)

    def test_r13_a1_the_manifest_read_ends_at_the_bounds_end_not_after_the_time_before_it(self) -> None:
        # r13 (adversary A1 on 5fe750fa, VenueChain.psm1:720): the hash's implicit end is -TimeoutSec on the call clock (the whole call inside -TimeoutSec), but the
        # manifest read ended at readStartSec + -TimeoutSec, so time spent before the read moved the call's end later: -TimeoutSec 1 after 1.5 s of preparation
        # answered NOT_STAGED at ~2.2 s (with or without -CallCapSec 60). The read now ends at the bound's end: no time left is UNREACHABLE at once, and a read
        # that runs past the bound's end is given up on there.
        absent = self.case / "a1-absent-root"
        for label, args, limit in (("no cap", "-TimeoutSec 1 -TestPreReadDelayMs 1500 -TestReadDelayMs 500", 1.9),
                                   ("cap 60", "-TimeoutSec 1 -CallCapSec 60 -TestPreReadDelayMs 1500 -TestReadDelayMs 500", 1.9),
                                   ("read past the end", "-TimeoutSec 3 -TestPreReadDelayMs 1500 -TestReadDelayMs 2500", 3.6)):
            with self.subTest(case=label):
                ps = (f"Import-Module '{DRIVER.parent / 'VenueChain.psm1'}' -Force -DisableNameChecking; $sw = [Diagnostics.Stopwatch]::StartNew(); "
                      f"$r = Get-VenueStagedBuildManifest -AgentShare '{absent}' -SourceCommit '{SHA}' -VerifyArtifacts {args}; "
                      "'STATE=' + $r.state + ' ERROR=[' + $r.error + ']'; 'SEC=' + $sw.Elapsed.TotalSeconds.ToString('0.000', [Globalization.CultureInfo]::InvariantCulture); "
                      "Complete-VenueProcess -ExitCode 0")
                p = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
                both = p.stdout + p.stderr
                self.assertIn("STATE=UNREACHABLE", p.stdout, both)
                self.assertLess(float(re.search(r"SEC=([0-9.]+)", p.stdout).group(1)), limit, both)

    def test_r11_g18_the_predicate_source_is_read_inside_the_calls_bounded_read(self) -> None:
        # r10 fix iteration (adversary A1 on a4b659de): the predicate read must never hold a capped call past its cap. r11 (Grok G17/G18 on #814): it is read on
        # every call INSIDE the bounded read, so the cap bounds it. The predicate's module path is pointed at a named pipe this script serves and never writes to:
        # a read of it blocks, as a read on a stalled share does. The 3 s call ends UNREACHABLE inside its cap, and the pipe saw the read (the seam fired).
        pipe = f"mlv-r11-g18-{os.getpid()}"
        ps = (f"Import-Module '{DRIVER.parent / 'VenueChain.psm1'}' -Force -DisableNameChecking; "
              f"$srv = [IO.Pipes.NamedPipeServerStream]::new('{pipe}', [IO.Pipes.PipeDirection]::Out, 1, [IO.Pipes.PipeTransmissionMode]::Byte, [IO.Pipes.PipeOptions]::Asynchronous); "
              "$hit = $srv.WaitForConnectionAsync(); "
              f"& (Get-Module VenueChain) {{ $script:VenueClaimShapeModulePath = '\\\\.\\pipe\\{pipe}' }}; $sw = [Diagnostics.Stopwatch]::StartNew(); "
              f"$r = try {{ Get-VenueStagedBuildManifest -AgentShare '{self.case / 'a1-absent-root'}' -SourceCommit '{SHA}' -TimeoutSec 3 -CallCapSec 3 -VerifyArtifacts }} "
              "catch { [pscustomobject]@{ state = 'THREW ' + $_.Exception.Message } }; "
              "'STATE=' + $r.state; 'SEC=' + $sw.Elapsed.TotalSeconds.ToString('0.000', [Globalization.CultureInfo]::InvariantCulture); 'READ_HIT=' + $hit.IsCompleted; $srv.Dispose()")
        p = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        self.assertIn("READ_HIT=True", p.stdout, "the call never read the predicate's module at all: " + p.stdout + p.stderr)
        self.assertIn("STATE=UNREACHABLE", p.stdout, p.stdout + p.stderr)
        self.assertLess(float(re.search(r"SEC=([0-9.]+)", p.stdout).group(1)), 3.6, p.stdout)

    def test_r11_g17_a_failed_predicate_read_clears_on_a_later_call(self) -> None:
        # r11 (Grok G17 on #814): r10 read the predicate once at import and kept a failure for the module's life, so a scanner holding AttrCudaArtifacts.psm1 for a
        # moment refused every later call in a long-lived importer. The scratch tree's copy is held open with no sharing from BEFORE the import: the first call is the
        # typed refusal, and once the file is released a later call in the same process reads it and answers.
        from tools.repo_hygiene.venue_fixture_modules import copy_with_siblings
        folder = Path(os.path.realpath(self.case)) / "g17-tree" / "dual-venue"
        copy_with_siblings(DRIVER.parent / "VenueChain.psm1", folder)
        held = folder.parent / "bachelor" / "AttrCudaArtifacts.psm1"
        self.assertTrue(held.is_file())
        call = (f"$x = try {{ (Get-VenueStagedBuildManifest -AgentShare '{self.case / 'g17-absent-root'}' -SourceCommit '{SHA}' -TimeoutSec 10).state }} "
                "catch { 'REFUSED ' + ($_.Exception.Message -split ' ')[0] }; ")
        ps = (f"$f = [IO.File]::Open('{held}', [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::None); "
              f"Import-Module '{folder / 'VenueChain.psm1'}' -Force -DisableNameChecking; "
              + call + "'FIRST=' + $x; $f.Dispose(); " + call + "'SECOND=' + $x")
        p = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        self.assertIn("FIRST=REFUSED VENUE_CLAIM_PREDICATE_UNREADABLE", p.stdout, p.stdout + p.stderr)
        self.assertIn("SECOND=NOT_STAGED", p.stdout, "a failed predicate read was kept past the release: " + p.stdout + p.stderr)

    def _r11_sound_share(self, name: str) -> Path:
        share = self.case / name
        (share / "cache").mkdir(parents=True)
        claim = {k: {"name": f"{k}.bin", "sha256": c * 64} for k, c in (("packageZip", "a"), ("exe", "b"), ("dll", "c"))}
        (share / "cache" / f"playback-attr-3-cuda-{SHA[:12]}-build.json").write_text(
            json.dumps({"sourceCommit": SHA, "dllPairManifestSha256": "d" * 64, "pendingSymbolPresence": True, **claim}), encoding="utf-8")
        return share

    def _r11_timed_call(self, share: Path, args: str) -> tuple[str, float, str]:
        ps = (f"Import-Module '{DRIVER.parent / 'VenueChain.psm1'}' -Force -DisableNameChecking; $sw = [Diagnostics.Stopwatch]::StartNew(); "
              f"$r = Get-VenueStagedBuildManifest -AgentShare '{share}' -SourceCommit '{SHA}' -VerifyArtifacts {args} "
              "-ArtifactProbeScript { param($items) [Threading.Thread]::Sleep(4000) }; "
              "'STATE=' + $r.state + ' ARTIFACT=[' + $r.artifactProblem + '] ERROR=[' + $r.error + ']'; 'SEC=' + $sw.Elapsed.TotalSeconds.ToString('0.000', [Globalization.CultureInfo]::InvariantCulture)")
        p = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        m = re.search(r"SEC=([0-9.]+)", p.stdout)
        self.assertIsNotNone(m, p.stdout + p.stderr)
        return p.stdout, float(m.group(1)), p.stdout + p.stderr

    def test_r11_a1_a_stall_before_the_hash_starts_never_moves_its_end_past_the_cap(self) -> None:
        # r11 (adversary A1 on 7754d16e, VenueChain.psm1:754/:769): the hash's budget was sampled as "what is left of -CallCapSec" and then re-anchored at the hash's
        # start (hashEnd = elapsed + budget), so a caller descheduled in between moved the end past the cap. -TestPreHashDelayMs stalls 3 s there; the hash takes 4 s.
        # RED: STAGED at about 7.2 s under a 6 s cap. The end is now fixed on the call clock, and the bounded wait ends at it: UNREACHABLE inside the cap.
        out, sec, both = self._r11_timed_call(self._r11_sound_share("a1-cap-share"), "-TimeoutSec 5 -ArtifactTimeoutSec 300 -CallCapSec 6 -TestPreHashDelayMs 3000")
        self.assertIn("STATE=UNREACHABLE", out, both)
        self.assertLess(sec, 6.6, both)

    def test_r11_a1_the_implicit_hash_budget_also_ends_at_the_bound(self) -> None:
        # the same stall with -ArtifactTimeoutSec 0 (the hash gets what is left of -TimeoutSec): the bound's end is kept, never re-anchored at the hash start
        out, sec, both = self._r11_timed_call(self._r11_sound_share("a1-bound-share"), "-TimeoutSec 6 -TestPreHashDelayMs 3000")
        self.assertIn("STATE=UNREACHABLE", out, both)
        self.assertLess(sec, 6.6, both)

    def test_r11_a1_the_deadline_handed_over_is_the_calls_end(self) -> None:
        # sibling (Invoke-VenueStageNightly.ps1:287/:300): the nightly handed over "what is left of the cap" as a number, measured before the call, and the call
        # counted it from its own later start. It now hands over the deadline itself (-CallDeadlineUtc), so the call ends at it whatever ran in between.
        out, sec, both = self._r11_timed_call(self._r11_sound_share("a1-deadline-share"),
                                              "-TimeoutSec 20 -ArtifactTimeoutSec 300 -CallCapSec 20 -CallDeadlineUtc ([DateTime]::UtcNow.AddSeconds(3.5)) -TestPreHashDelayMs 1000")
        self.assertIn("STATE=UNREACHABLE", out, both)
        self.assertLess(sec, 4.1, both)
        # r12: and the call really ran to the deadline (the hash was started and given up on there), never a deadline that failed to parse ending it at once
        self.assertGreater(sec, 3.0, both)
        self.assertIn("could not be hashed: no answer in", out + both, both)
        # r13 (Grok G20): the wait the hash was given (under the 3.5 s deadline, never the 300 s -ArtifactTimeoutSec), and that the call's end cut it short
        m = re.search(r"could not be hashed: no answer in ([0-9.]+) s \(the call's end cut the ([0-9.]+) s bound short\)", out)
        self.assertIsNotNone(m, both)
        self.assertLess(float(m.group(1)), 3.5, both)
        # r14 (Grok G21 on #814, VenueChain.psm1:814): the bound the clause names is the hash's configured budget (-ArtifactTimeoutSec 300), which -EndSec clamped,
        # never a remainder of it ("cut the 3.4 s bound short")
        self.assertEqual(m.group(2), "300", both)

    def test_r14_a1_the_least_accepted_bound_still_reads(self) -> None:
        # r14 fix iteration 1 (adversary A1 on f7408e9f, VenueChain.psm1:723, sibling :716): -TimeoutSec 1 and -CallCapSec 1 are the least the call accepts, and
        # the read refused under 1 s left, so ANY preparation time made them UNREACHABLE ("0.99 s left, under 1") before a read. RED: UNREACHABLE for both.
        absent = self.case / "r14-a1-no-cache"
        for args in ("-TimeoutSec 1", "-TimeoutSec 5 -CallCapSec 1"):
            with self.subTest(args=args):
                ps = (f"Import-Module '{DRIVER.parent / 'VenueChain.psm1'}' -Force -DisableNameChecking; "
                      f"$r = Get-VenueStagedBuildManifest -AgentShare '{absent}' -SourceCommit '{SHA}' {args}; 'STATE=' + $r.state + ' ERROR=[' + $r.error + ']'")
                p = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
                self.assertIn("STATE=NOT_STAGED ERROR=[]", p.stdout, p.stdout + p.stderr)

    def _r12_seamed_call(self, share: Path, seam: str, args: str, probe: str) -> tuple[str, float, str]:
        ps = (f"Import-Module '{DRIVER.parent / 'VenueChain.psm1'}' -Force -DisableNameChecking; & (Get-Module VenueChain) {{ {seam} }}; "
              "$sw = [Diagnostics.Stopwatch]::StartNew(); "
              f"$r = Get-VenueStagedBuildManifest -AgentShare '{share}' -SourceCommit '{SHA}' {args} {probe}; "
              "'STATE=' + $r.state + ' ERROR=[' + $r.error + ']'; 'SEC=' + $sw.Elapsed.TotalSeconds.ToString('0.000', [Globalization.CultureInfo]::InvariantCulture)")
        p = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        m = re.search(r"SEC=([0-9.]+)", p.stdout)
        self.assertIsNotNone(m, p.stdout + p.stderr)
        return p.stdout, float(m.group(1)), p.stdout + p.stderr

    def test_r12_a1_a_stall_between_the_deadline_samples_never_moves_the_end_later(self) -> None:
        # r12 fix iteration (adversary A1 on 09421b77, VenueChain.psm1:655): the deadline's remainder was taken from UtcNow, then the call clock read AFTER it, so a
        # caller descheduled between the two added the stall to the cap. A 3 s stall there under a 4 s deadline: RED STAGED at ~6 s, past the deadline.
        out, sec, both = self._r12_seamed_call(self._r11_sound_share("r12-a1-share"), "$script:VenueTestDeadlineSampleDelayMs = 3000",
                                               "-VerifyArtifacts -TimeoutSec 20 -ArtifactTimeoutSec 300 -CallCapSec 20 -CallDeadlineUtc ([DateTime]::UtcNow.AddSeconds(4))",
                                               "-ArtifactProbeScript { param($items) [Threading.Thread]::Sleep(3000) }")
        self.assertIn("STATE=UNREACHABLE", out, both)
        self.assertLess(sec, 4.6, both)

    def test_r12_a2_an_answer_that_lands_after_the_fixed_end_is_never_acted_on(self) -> None:
        # r12 fix iteration (adversary A2 on 09421b77, VenueChain.psm1:870): a caller descheduled between the launch and the wait found the worker done, WaitOne(0)
        # was true and the late answer was accepted. Every bounded read is stalled 5 s there under a 4 s cap: RED STAGED at ~5 s; now UNREACHABLE, past its end.
        out, sec, both = self._r12_seamed_call(self._r11_sound_share("r12-a2-share"), "$script:VenueTestPreWaitDelayMs = 5000",
                                               "-TimeoutSec 20 -CallCapSec 4", "")
        self.assertIn("STATE=UNREACHABLE", out, both)
        self.assertIn("after the call's end", out, both)

    def test_r13_g19_sibling_a_cache_manifest_that_cannot_be_read_is_share_unreachable(self) -> None:
        # r13 (Grok G19 class, Invoke-VenueStageNightly.ps1 $HashByName): the by-name manifest read ran on a worker runspace under its default Continue, so a
        # build.json a writer held open (FileShare.None) failed its byte read silently and came back present=$false: step 4 never asked the soundness read and the
        # call built as if the cache were empty (and the post-stage verifies printed "hashes MISSING" for a file that is there). Under EAP Stop, as VenueChain.psm1's
        # canonical read, the read fails with its own error and Read-Share types it SHARE_UNREACHABLE (:503's rule: a read that does not answer builds nothing).
        import ctypes
        from ctypes import wintypes
        bj = self.stage_cache()
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        h = k32.CreateFileW(str(bj), 0x80000000 | 0x40000000, 0, None, 3, 0x80, None)   # GENERIC_READ|GENERIC_WRITE, share none, OPEN_EXISTING
        self.assertNotEqual(h, wintypes.HANDLE(-1).value, f"CreateFileW failed: {ctypes.get_last_error()}")
        try:
            p = self.run_driver()
        finally:
            k32.CloseHandle(h)
        self.assert_exit(p, "SHARE_UNREACHABLE")
        self.assertRegex(p.stdout, r"reading [^\r\n]*-build\.json failed: [^\r\n]*another process", p.stdout)
        self.nothing_built_or_submitted()

    def test_r13_g20_a_wait_the_calls_end_clamped_reports_the_wait_it_was_given(self) -> None:
        # r13 (Grok G20 on #814, VenueChain.psm1:884): Invoke-VenueBoundedRead said "no answer in <TimeoutSec> s" even when -Clock/-EndSec had clamped the wait
        # (:879), so a 2 s wait under a 20 s bound was reported as 20 s. It reports the wait it actually gave and says the call's end cut it short (as the nightly's
        # capCut does); an unclamped wait still reports its own -TimeoutSec with no such clause.
        mod = DRIVER.parent / "VenueChain.psm1"
        ps = (f"Import-Module '{mod}' -Force -DisableNameChecking; & (Get-Module VenueChain) {{ "
              "$c = [Diagnostics.Stopwatch]::StartNew(); "
              "$r = Invoke-VenueBoundedRead -ScriptText 'param($p) [Threading.Thread]::Sleep(8000); 1' -Argument 'x' -TimeoutSec 20 -Clock $c -EndSec 2; "
              "'CLAMPED=[' + $r.answered + '|' + $r.error + ']'; "
              "$r = Invoke-VenueBoundedRead -ScriptText 'param($p) [Threading.Thread]::Sleep(8000); 1' -Argument 'x' -TimeoutSec 1.5; "
              "'PLAIN=[' + $r.answered + '|' + $r.error + ']'; [Environment]::Exit(0) }")
        p = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        both = p.stdout + p.stderr
        m = re.search(r"CLAMPED=\[False\|no answer in ([0-9.]+) s \(the call's end cut the 20 s bound short\)\]", p.stdout)
        self.assertIsNotNone(m, both)
        self.assertLessEqual(float(m.group(1)), 2.0, both)
        self.assertGreater(float(m.group(1)), 1.0, both)
        self.assertIn("PLAIN=[False|no answer in 1.5 s]", p.stdout, both)

    def test_r16_g22_the_end_cut_clause_is_a_cap_never_the_bound_itself(self) -> None:
        # r16 (Grok G22 on #814, VenueChain.psm1:891): the clause fired whenever the wait's end was under -TimeoutSec, which a read measured from its own end always
        # is, so every plain timeout said "the call's end cut ... short". It is printed only when -CallCapSec lowered the end. The hash: an explicit budget with no cap
        # (-ArtifactTimeoutSec 2) and the implicit one (what is left of -TimeoutSec 3) time out with NO clause; the capped hash (-CallCapSec, the 300 s budget) still
        # names it (test_r11_a1_the_deadline_handed_over_is_the_calls_end). RED: both rows below carried the clause.
        for args, bound in (("-TimeoutSec 20 -ArtifactTimeoutSec 2", "2"), ("-TimeoutSec 3", "3")):
            with self.subTest(args=args):
                out, sec, both = self._r11_timed_call(self._r11_sound_share(f"r16-g22-{bound}"), args)
                self.assertIn("STATE=UNREACHABLE", out, both)
                self.assertRegex(out, r"ERROR=\[the cached artifacts could not be hashed: no answer in [0-9.]+ s\]", both)
                self.assertNotIn("bound short", out, both)
        out, sec, both = self._r11_timed_call(self._r11_sound_share("r16-g22-capped"), "-TimeoutSec 20 -ArtifactTimeoutSec 300 -CallCapSec 3")
        self.assertRegex(out, r"ERROR=\[the cached artifacts could not be hashed: no answer in [0-9.]+ s \(the call's end cut the 300 s bound short\)\]", both)

    def test_r11_a1_the_nightly_hands_its_deadline_to_the_soundness_read(self) -> None:
        text = DRIVER.read_text(encoding="utf-8")
        calls = [l for l in text.splitlines() if "Get-VenueStagedBuildManifest -AgentShare" in l]
        self.assertTrue(calls)
        self.assertTrue(all("-CallDeadlineUtc $script:Deadline" in l for l in calls), calls)

    def test_a_cache_hash_that_differs_after_stage_ok_is_a_verify_mismatch(self) -> None:
        p = self.run_driver(env={"FAKE_UMRUN_STAGE": "wrongcache"})
        self.assert_exit(p, "STAGE_VERIFY_MISMATCH")
        rows = self.rows()
        self.assertEqual([r["outcome"] for r in rows], ["STAGE_RESERVED", "STAGE_VERIFY_MISMATCH"])
        self.assertIsNone(rows[-1]["buildManifestSha256"])
        self.assertIn("manifest=none", p.stdout)

    # ---------------------------------------------------------------- capacity (exit 3, the gate's text verbatim)
    def test_disk_floor_refusal_exits_3_with_the_gate_text_and_builds_nothing(self) -> None:
        p = self.run_driver(env={"FAKE_DISK": "refuse"})
        self.assert_exit(p, "DISK_FLOOR_REFUSED")
        self.assertIn("DISK_FLOOR_REFUSED caller=Invoke-VenueStageNightly card=venue-stage-nightly freeGiB=42.1 floorGiB=50 utc=fixture -- C: is under the long-run floor; free space first and re-dispatch", p.stdout.splitlines())
        self.nothing_built_or_submitted()
        self.assertEqual(self.rows()[-1]["outcome"], "DISK_FLOOR_REFUSED")

    def test_commit_ceiling_refusal_exits_3_with_the_gate_text_and_builds_nothing(self) -> None:
        p = self.run_driver(env={"FAKE_COMMIT": "refuse"})
        self.assert_exit(p, "HOST_COMMIT_REFUSED")
        line = [l for l in p.stdout.splitlines() if l.startswith("HOST_COMMIT_REFUSED ")]
        self.assertEqual(len(line), 1, p.stdout)
        self.assertTrue(line[0].endswith("REFUSED: commit 97 % of limit"), line)
        self.nothing_built_or_submitted()

    def test_a_missing_gate_script_is_a_typed_refusal_never_an_admit(self) -> None:
        for gone in ("disk-floor.ps1", "host-commit-gate.ps1"):
            with self.subTest(gone=gone):
                (self.gates / "disk-floor.ps1").write_text(FAKE_DISK_FLOOR, encoding="utf-8")
                (self.gates / "host-commit-gate.ps1").write_text(FAKE_COMMIT_GATE, encoding="utf-8")
                (self.gates / gone).unlink()
                self.calls_log.unlink(missing_ok=True)
                p = self.run_driver()
                self.assert_exit(p, "GATE_UNAVAILABLE")
                self.assertIn(gone, p.stdout)
                self.nothing_built_or_submitted()

    def test_a_build_guard_refusal_exits_3_with_its_line(self) -> None:
        self.built_dll_pair()
        p = self.run_driver(env={"FAKE_ASSEMBLE": "guard"})
        self.assert_exit(p, "BUILD_GUARD_REFUSED")
        self.assertTrue(any(l.startswith(f"BUILD_MEM_REFUSED label=attr3-assemble:{S12} ") for l in p.stdout.splitlines()), p.stdout)
        self.assertFalse(any("stage" in c for c in self.submissions()), self.submissions())
        self.assertEqual(self.rows()[-1]["outcome"], "BUILD_GUARD_REFUSED")

    def test_a_heavy_slot_requeue_is_a_build_guard_refusal_never_an_assembly_failure(self) -> None:
        # MLV-VM-HEAVY-SLOT-ADOPT-1 r13 (sol r12 B1): the machine heavy slot's REQUEUE (heavy-slot code 75/76/77) is a capacity
        # refusal like BUILD_MEM_REFUSED and BUILD_SLOT_BUSY (the controls), never ASSEMBLE_FAILED (exit 45).
        self.built_dll_pair()
        for mode, code, token in (("requeue", "75", "BUILD_HEAVY_SLOT_REQUEUE"), ("requeue", "76", "BUILD_HEAVY_SLOT_REQUEUE"),
                                  ("requeue", "77", "BUILD_HEAVY_SLOT_REQUEUE"), ("guard", "", "BUILD_MEM_REFUSED"),
                                  ("slot-busy", "", "BUILD_SLOT_BUSY")):
            with self.subTest(mode=mode, code=code):
                p = self.run_driver(env={"FAKE_ASSEMBLE": mode, "FAKE_REQUEUE_CODE": code})
                self.assert_exit(p, "BUILD_GUARD_REFUSED")
                lines = [l for l in p.stdout.splitlines() if l.startswith(f"{token} label=attr3-assemble:{S12} ")]
                self.assertEqual(len(lines), 1, p.stdout)
                if code:
                    self.assertIn(f" code={code} ticket=t-1 reason=timed out ", lines[0])
                self.assertNotIn("ASSEMBLE_FAILED", p.stdout)
                self.assertFalse(any("stage" in c for c in self.submissions()), self.submissions())
                self.assertEqual(self.rows()[-1]["outcome"], "BUILD_GUARD_REFUSED")

    def test_the_assembler_is_given_this_calls_deadline_as_its_heavy_slot_wait_bound(self) -> None:
        # STAGE-BUILD-HEAVY-SLOT-TIMEOUT-LOSES-TURN-1: without it the assembler waits the 60 min default on the machine slot, is refused,
        # and the caller's retry is the youngest ticket. The bound is this call's own deadline (start + -MaxTotalSec), via the environment.
        self.built_dll_pair()
        before = datetime.now(timezone.utc)
        p = self.run_driver("-MaxTotalSec", "3600", env={"MLV_HEAVY_SLOT_DEADLINE_UTC": "2000-01-01T00:00:00Z"})
        after = datetime.now(timezone.utc)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        [call] = self.calls("ASSEMBLE")
        raw = re.search(r" hsdeadline=(\S+)$", call).group(1)
        self.assertNotEqual(raw, "2000-01-01T00:00:00Z", "the ambient value is replaced by this call's own deadline")
        m = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)\.(\d+)Z", raw)
        self.assertIsNotNone(m, raw)
        got = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc) + timedelta(microseconds=int(m.group(2)[:6]))
        self.assertLessEqual(before + timedelta(seconds=3600 - 5), got)
        self.assertLessEqual(got, after + timedelta(seconds=3600 + 5))

    def test_an_assembler_failure_is_typed(self) -> None:
        self.built_dll_pair()
        p = self.run_driver(env={"FAKE_ASSEMBLE": "fail"})
        self.assert_exit(p, "ASSEMBLE_FAILED")
        self.assertEqual(self.submissions(), [])

    # ---------------------------------------------------------------- the venue is in use / unreachable / unresolved
    def test_a_live_claim_is_agent_busy_and_nothing_is_submitted(self) -> None:
        self.claims.mkdir(parents=True)
        (self.claims / "ultra-magnus.claim.json").write_text(json.dumps({
            "schema": "mlv-app/venue-claim/v1", "venue": "ultra-magnus", "ownerPid": os.getpid(), "ownerCreatedUtc": process_start_iso(os.getpid()),
            "host": os.environ.get("COMPUTERNAME", ""), "runDir": "C:\\elsewhere\\lane-OTHER", "purpose": "fixture", "claimedUtc": utc_iso(datetime.now(timezone.utc)),
            "expiresUtc": utc_iso(datetime.now(timezone.utc) + timedelta(hours=3)), "nonce": "fixture-nonce"}), encoding="utf-8")
        p = self.run_driver()
        self.assert_exit(p, "AGENT_BUSY")
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.rows()[-1]["outcome"], "AGENT_BUSY")

    def test_a_queued_ticket_is_agent_busy(self) -> None:
        mod = DV / "VenueChain.psm1"
        cmd = (f"Import-Module '{mod}' -DisableNameChecking; New-VenueTicket -Venue ultra-magnus -RunDir 'C:\\elsewhere\\lane-OTHER' -Purpose fixture "
               f"-ClaimDir '{self.claims}' -OwnerPid {os.getpid()} -StaleAfterSec 3600 | Out-Null")
        subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", cmd], check=True, capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
        p = self.run_driver()
        self.assert_exit(p, "AGENT_BUSY")
        self.assertEqual(self.calls(), [])

    def test_an_unreachable_share_is_typed_and_bounded(self) -> None:
        t0 = time.monotonic()
        p = self.run_driver(share=self.case / "no-such-share")
        self.assert_exit(p, "SHARE_UNREACHABLE")
        self.assertLess(time.monotonic() - t0, 90)
        self.assertEqual(self.calls(), [])

    def test_an_unresolved_um_run_is_typed_and_never_retried(self) -> None:
        p = self.run_driver(env={"FAKE_UMRUN_DLLPAIR": "unresolved"})
        self.assert_exit(p, "STAGE_UNRESOLVED")
        self.assertEqual(len(self.submissions()), 1, self.submissions())
        self.assertEqual(self.calls("ASSEMBLE"), [])
        self.assertEqual(self.rows()[-1]["outcome"], "STAGE_UNRESOLVED")

    def test_an_unresolved_stage_submission_is_typed_and_never_retried(self) -> None:
        self.built_dll_pair()
        p = self.run_driver(env={"FAKE_UMRUN_STAGE": "unresolved"})
        self.assert_exit(p, "STAGE_UNRESOLVED")
        self.assertEqual(len([c for c in self.submissions() if "job=playback-attr-3-cuda-stage-" in c]), 1)
        self.assertFalse(any("smoke-runner" in c for c in self.submissions()))

    def test_a_failed_stage_result_is_stage_failed(self) -> None:
        self.built_dll_pair()
        p = self.run_driver(env={"FAKE_UMRUN_STAGE": "fail"})
        self.assert_exit(p, "STAGE_FAILED")

    # ---------------------------------------------------------------- once per night
    def seed_row(self, sha: str, outcome: str = "STAGE_OK", night: str = NIGHT, reservation_id: str | None = None) -> None:
        self.receipt.parent.mkdir(parents=True, exist_ok=True)
        row = {"schema": "mlv-app/venue-stage-nightly/v1", "utc": "2026-10-19T20:00:00Z", "nightKey": night, "venue": "ultra-magnus",
               "sourceCommit": sha, "outcome": outcome, "exitCode": None if outcome == "STAGE_RESERVED" else EXIT.get(outcome, 0)}
        if reservation_id:
            row["reservationId"] = reservation_id
        with self.receipt.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")

    def test_a_second_build_in_one_night_is_refused_for_another_sha(self) -> None:
        self.seed_row(OTHER_SHA)
        p = self.run_driver()
        self.assert_exit(p, "NIGHT_ALREADY_BUILT")
        self.assertEqual(self.calls(), [])
        r = self.rows()[-1]
        self.assertEqual((r["outcome"], r["exitCode"], r["nightKey"]), ("NIGHT_ALREADY_BUILT", 0, NIGHT))
        self.assertIn(OTHER_SHA, r["detail"])

    def test_the_same_sha_in_the_same_night_is_a_skip(self) -> None:
        self.seed_row(SHA)
        self.stage_cache()
        p = self.run_driver()
        self.assert_exit(p, "STAGE_SKIP_ALREADY")
        self.nothing_built_or_submitted()

    def test_a_build_from_an_earlier_night_does_not_block(self) -> None:
        self.seed_row(OTHER_SHA, night="2026-10-19")
        self.built_dll_pair()
        p = self.run_driver()
        self.assert_exit(p, "STAGE_OK")

    def test_the_night_key_is_the_date_of_the_next_0800z(self) -> None:
        for now, night in (("2026-10-19T07:59:59Z", "2026-10-19"), ("2026-10-19T08:00:00Z", "2026-10-20"), ("2026-10-19T23:30:00Z", "2026-10-20")):
            with self.subTest(now=now):
                p = self.run_driver("-WhatIf", now=now)
                self.assertIn(f"nightKey={night}", p.stdout)

    # ---------------------------------------------------------------- the reservation and the fail-closed receipt (CoS Dot b21fb3be, PR #526)
    def clear_readonly(self) -> None:
        if self.receipt.is_file():
            os.chmod(self.receipt, stat.S_IREAD | stat.S_IWRITE)

    def receipt_goes_readonly_at_the_runner_submission(self) -> dict[str, str]:
        # the stage has landed in the cache by then; only the receipt append of the call's end can fail
        self.addCleanup(self.clear_readonly)
        return {"FAKE_RECEIPT_READONLY": "SMOKE", "FAKE_RECEIPT": str(self.receipt)}

    def test_a_receipt_append_failure_after_staging_is_a_typed_nonzero_exit_never_stage_ok(self) -> None:
        self.built_dll_pair()
        p = self.run_driver(env=self.receipt_goes_readonly_at_the_runner_submission())
        self.assert_exit(p, "RECEIPT_IO_FAILED")
        self.assertNotIn("reason=STAGE_OK", p.stdout)
        self.assertEqual(self.result(p.stdout)["unpersistedOutcome"], "STAGE_OK")
        self.clear_readonly()
        rows = self.rows()
        # the reservation written before the first staging step is the row that survives
        self.assertEqual([r["outcome"] for r in rows], ["STAGE_RESERVED"], rows)
        self.assertEqual((rows[0]["sourceCommit"], rows[0]["nightKey"], rows[0]["venue"]), (SHA, NIGHT, "ultra-magnus"))
        self.assertTrue(rows[0]["reservationId"])

    def test_a_lost_stage_ok_row_still_holds_the_night_against_a_second_sha(self) -> None:
        self.built_dll_pair()
        first = self.run_driver(env=self.receipt_goes_readonly_at_the_runner_submission())
        self.assertNotEqual(first.returncode, 0, first.stdout + first.stderr)
        self.clear_readonly()
        self.calls_log.unlink(missing_ok=True)
        second = self.run_driver(commit=OTHER_SHA)
        self.assertNotEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assert_exit(second, "NIGHT_RESERVED")
        self.assertEqual(self.calls(), [])
        r = self.rows()[-1]
        self.assertEqual((r["outcome"], r["exitCode"], r["sourceCommit"]), ("NIGHT_RESERVED", EXIT["NIGHT_RESERVED"], OTHER_SHA))
        self.assertIn(SHA, r["detail"])

    def test_an_incomplete_reservation_for_the_same_sha_resumes_and_is_reconciled(self) -> None:
        self.seed_row(SHA, outcome="STAGE_RESERVED", reservation_id="r-incomplete-1")
        self.built_dll_pair()
        p = self.run_driver()
        self.assert_exit(p, "STAGE_OK")
        rows = self.rows()
        self.assertEqual([r["outcome"] for r in rows], ["STAGE_RESERVED", "STAGE_RESERVED", "STAGE_OK"], rows)
        new_id = rows[1]["reservationId"]
        self.assertNotEqual(new_id, "r-incomplete-1")
        self.assertEqual(rows[2]["reservationId"], new_id)
        self.assertEqual([(x["reservationId"], x["state"]) for x in rows[2]["reconciled"]], [("r-incomplete-1", "incomplete")])

    def test_an_incomplete_reservation_for_another_sha_holds_the_night(self) -> None:
        self.seed_row(OTHER_SHA, outcome="STAGE_RESERVED", reservation_id="r-incomplete-1")
        p = self.run_driver()
        self.assert_exit(p, "NIGHT_RESERVED")
        self.assertEqual(self.calls(), [])
        r = self.rows()[-1]
        self.assertIn(OTHER_SHA, r["detail"])
        self.assertIn("r-incomplete-1", r["detail"])
        self.assertEqual([(x["reservationId"], x["state"]) for x in r["reconciled"]], [("r-incomplete-1", "incomplete")])

    def test_a_failed_reservation_for_another_sha_still_holds_the_night(self) -> None:
        self.seed_row(OTHER_SHA, outcome="STAGE_RESERVED", reservation_id="r-failed-1")
        self.seed_row(OTHER_SHA, outcome="STAGE_UNRESOLVED", reservation_id="r-failed-1")
        p = self.run_driver()
        self.assert_exit(p, "NIGHT_RESERVED")
        self.assertEqual(self.calls(), [])
        self.assertIn("STAGE_UNRESOLVED", self.rows()[-1]["detail"])

    def test_a_reservation_from_an_earlier_night_does_not_block(self) -> None:
        self.seed_row(OTHER_SHA, outcome="STAGE_RESERVED", night="2026-10-19", reservation_id="r-yesterday")
        self.built_dll_pair()
        self.assert_exit(self.run_driver(), "STAGE_OK")

    def test_a_reservation_that_cannot_be_written_fails_closed_before_staging(self) -> None:
        self.receipt.parent.mkdir(parents=True)
        self.receipt.write_text("", encoding="utf-8")
        os.chmod(self.receipt, stat.S_IREAD)
        self.addCleanup(self.clear_readonly)
        self.built_dll_pair()
        p = self.run_driver()
        self.assert_exit(p, "RECEIPT_IO_FAILED")
        for role in ("DLLGEN", "ASSEMBLE", "STAGEGEN", "UMRUN"):
            self.assertEqual(self.calls(role), [], role)
        self.assertIn("staging did not start", p.stdout)

    # ---------------------------------------------------------------- venue exclusion does not depend on the caller's receipt path
    def test_a_second_receipt_path_cannot_take_the_venue_mutex(self) -> None:
        self.built_dll_pair()
        env = dict(os.environ, FAKE_CALLS=str(self.calls_log), FAKE_SHA=SHA, FAKE_ASSEMBLE="sleep")
        holder = subprocess.Popen(self.args("-MaxTotalSec", "45"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, stdin=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 120
            while not self.calls("ASSEMBLE") and holder.poll() is None and time.monotonic() < deadline:
                time.sleep(0.5)
            self.assertTrue(self.calls("ASSEMBLE"), "the holder never reached its assemble")
            a = self.args("-MaxTotalSec", "20")
            a[a.index("-ReceiptFile") + 1] = str(self.case / "other" / ".claude-state" / "stage-nightly.jsonl")
            p = subprocess.run(a, capture_output=True, text=True, timeout=120, env=env, stdin=subprocess.DEVNULL)
            self.assert_exit(p, "AGENT_BUSY")
            self.assertIn("MLV-VenueStageNightly-ultra-magnus", p.stdout)
            self.assertEqual(len(self.calls("ASSEMBLE")), 1, self.calls("ASSEMBLE"))
        finally:
            try:
                holder.communicate(timeout=150)
            except subprocess.TimeoutExpired:
                holder.kill()
                holder.communicate()

    def test_the_real_venue_share_takes_only_the_canonical_receipt(self) -> None:
        # No -AgentShare: the venues.json share. -WhatIf, so neither this driver nor an older one probes, builds or submits anything.
        a = self.args("-WhatIf")
        i = a.index("-AgentShare")
        del a[i:i + 2]
        p = subprocess.run(a, capture_output=True, text=True, timeout=120, env=dict(os.environ, FAKE_CALLS=str(self.calls_log)), stdin=subprocess.DEVNULL)
        self.assertEqual(p.returncode, EXIT["USAGE"], p.stdout + p.stderr)
        self.assertIn("canonical", p.stdout)
        self.assertIn(str(Path(".claude-state") / "dual-venue" / "stage-nightly.jsonl"), p.stdout)
        self.assertEqual(self.calls(), [])

    # ---------------------------------------------------------------- the cap
    def test_a_child_that_outlives_the_cap_is_killed_and_typed(self) -> None:
        self.built_dll_pair()
        t0 = time.monotonic()
        p = self.run_driver("-MaxTotalSec", "25", env={"FAKE_ASSEMBLE": "sleep"}, timeout=200)
        self.assertLess(time.monotonic() - t0, 120, "the driver waited past its cap; the assembler stub sleeps 300 s")
        self.assert_exit(p, "STAGE_CAP_EXCEEDED")
        pid = int(re.search(r"pid=(\d+)", self.calls("ASSEMBLE")[0]).group(1))
        deadline = time.monotonic() + 15
        while pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.5)
        self.assertFalse(pid_alive(pid), "the assembler outlived the cap")
        self.assertEqual(self.submissions(), [])

    # ---------------------------------------------------------------- static guards
    def test_the_driver_never_deletes_enumerates_schedules_or_starts_the_app(self) -> None:
        text = DRIVER.read_text(encoding="utf-8")
        self.assertTrue(text.isascii(), "ASCII-only .ps1")
        code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        for forbidden in ("MLVApp.exe", "Register-ScheduledTask", "schtasks", "Start-Process", "Start-Job", "Remove-Item", "Get-ChildItem", "hub-autopilot.ps1",
                          "pending-actions", "-Recurse"):
            self.assertNotIn(forbidden, code, forbidden)
        for needed in ("Test-DiskFloor", "Test-HostCommitGate", "Get-VenueClaimStatus", "Get-DllPairReuse", "Get-StageReuse", "Test-UmRunJobIdInUseMessage",
                       "-JobIdSuffix", "mlv-app/venue-stage-nightly/v1", "STAGE_RESERVED", "reservationId"):
            self.assertIn(needed, code, needed)


class SoundnessReadCapNeverZeroTests(unittest.TestCase):
    # r9 (Grok G12 on #814): source-text tests, so no @requires_windows_pwsh; they run on every runner
    def test_r9_g13_the_soundness_read_refusals_name_what_was_submitted_per_path(self) -> None:
        # Grok G13: the final verify runs after the stage job was submitted, so its refusals never say "nothing was submitted"; step 4 and step 8 still do.
        text = DRIVER.read_text(encoding="utf-8")
        body = re.search(r"(?ms)^function Read-StagedSoundness\(.*?^\}", text).group(0)
        self.assertIn("$notDone = $(if (-not $FinalVerify) { 'nothing was submitted' } elseif ($script:Row.steps.stage -cin @('staged', 'staged-suffix')) "
                      "{ 'the stage job was submitted; not recorded as staged' } else { 'this call submitted no stage job; not recorded as staged' })", body)
        self.assertNotIn("nothing was submitted\"", body, "a refusal in the soundness read hard-codes the step-4 wording")
        refusals = re.findall(r"Stop-Nightly 'STAGE_CAP_EXCEEDED' \"[^\"]*\"", body)
        self.assertEqual(len(refusals), 3, refusals)  # r9 fix iteration (adversary A1 on be203fe6): an answer past the cap is the third
        for r in refusals:
            self.assertTrue(r.endswith("; $notDone\""), r)
        self.assertLess(body.index("$notDone = "), body.index("if ($capSec -lt 1.0)"))
        # the final verify's own cap refusal (after the read) carries the same tail
        self.assertIn("could be recorded; not recorded as staged\" }", text)

    def test_r8_a_spent_cap_is_never_handed_to_the_soundness_read_as_call_cap_zero(self) -> None:
        # r8 fix iteration 2 (adversary A1 on ad703c59): Get-VenueStagedBuildManifest reads -CallCapSec 0 as NO cap (VenueChain.psm1 `if ($CallCapSec -gt 0)`), so a
        # cap that ran out between Get-ProbeSec and the call must end the call (STAGE_CAP_EXCEEDED) before it, never become an unbounded 300 s artifact hash.
        text = DRIVER.read_text(encoding="utf-8")
        body = re.search(r"(?ms)^function Read-StagedSoundness\(.*?^\}", text).group(0)
        cap, guard, call = (body.index("$capSec = [Math]::Max(0.0, "), body.index("if ($capSec -lt 1.0) { Stop-Nightly 'STAGE_CAP_EXCEEDED' "),
                            body.index("Get-VenueStagedBuildManifest -AgentShare $share"))
        self.assertLess(cap, guard)
        self.assertLess(guard, call)
        self.assertIn("-CallCapSec $capSec ", body[call:])
        # the module side of the sentinel this guard exists for
        self.assertIn("if ($CallCapSec -gt 0) {", (DV / "VenueChain.psm1").read_text(encoding="utf-8"))


@requires_windows_pwsh
class VenueStageReuseModuleTests(unittest.TestCase):
    """The three promoted functions, logic unchanged from the untracked venue-reuse-check.ps1 the lanes copied word for word."""

    def ps(self, body: str) -> str:
        p = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", f"Import-Module '{psq(REUSE_MODULE)}' -Force; {body}"],
                           capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p.stdout.strip()

    def test_the_module_names_its_origin_and_is_ascii(self) -> None:
        text = REUSE_MODULE.read_text(encoding="utf-8")
        self.assertTrue(text.isascii())
        self.assertIn("venue-reuse-check.ps1", text)
        for fn in ("Test-UmRunJobIdInUseMessage", "Get-StageReuse", "Get-DllPairReuse"):
            self.assertIn(f"function {fn}", text)

    def test_in_use_message_matches_only_the_outbox_refusal_for_that_jobid(self) -> None:
        out = self.ps("Test-UmRunJobIdInUseMessage -Message 'UMRUN_JOBID_IN_USE outbox already holds j-1.result.json' -JobId 'j-1'; "
                      "Test-UmRunJobIdInUseMessage -Message 'UMRUN_JOBID_IN_USE inbox already holds j-1.job.ps1' -JobId 'j-1'; "
                      "Test-UmRunJobIdInUseMessage -Message 'UMRUN_JOBID_IN_USE outbox already holds j-2.result.json' -JobId 'j-1'")
        self.assertEqual(out.split(), ["True", "False", "False"])

    def test_stage_reuse_reads_a_stage_ok_result_and_refuses_another_source(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            res = Path(d) / "r.json"
            res.write_text(json.dumps({"jobId": "j", "exitCode": 0, "stdout": f"RESULT=STAGE_OK SOURCE={SHA} FILES=4", "endedUtc": "2026-10-18T00:00:00Z"}), encoding="utf-8")
            ok = self.ps(f"(Get-StageReuse -ResultPath '{psq(res)}' -JobId 'j' -Sha '{SHA}' -Kind Stage).Staged")
            other = self.ps(f"(Get-StageReuse -ResultPath '{psq(res)}' -JobId 'j' -Sha '{OTHER_SHA}' -Kind Stage).Reason")
        self.assertEqual(ok, "True")
        self.assertTrue(other.startswith("SOURCE-mismatch"), other)

    # ---------------------------------------------------------------- SMOKE-RUNNER-STAGE-OK-ALREADY-TOKEN-1
    SMOKE_JOB = f"attr3-stage-smoke-runner-{S12}-bbbbbbbbbbbb"

    def reuse(self, stdout: str, kind: str, job: str, sha: str = SHA) -> tuple[str, str, str]:
        with tempfile.TemporaryDirectory() as d:
            res = Path(d) / "r.json"
            res.write_text(json.dumps({"jobId": job, "exitCode": 0, "stdout": stdout, "endedUtc": "2026-11-15T00:00:00Z"}), encoding="utf-8")
            out = self.ps(f"$r = Get-StageReuse -ResultPath '{psq(res)}' -JobId '{psq(job)}' -Sha '{psq(sha)}' -Kind {kind}; "
                          "[Console]::Out.WriteLine([string]$r.Staged); [Console]::Out.WriteLine([string]$r.Reason); [Console]::Out.WriteLine([string]$r.Line)")
        lines = out.splitlines() + ["", "", ""]
        return lines[0], lines[1], lines[2]

    def test_smoke_runner_reuse_accepts_the_already_staged_form(self) -> None:
        # attr3-stage-smoke-runner-job.ps1 Complete-AlreadyStaged prints this when the cache already holds the closure; the checker used to answer
        # no-RESULT=SMOKE_RUNNER_STAGE_OK-line and the nightly ended SMOKE_RUNNER_STAGE_FAILED (#813 r5).
        staged, reason, line = self.reuse(f"RESULT=SMOKE_RUNNER_STAGE_OK ALREADY=1 CACHE_DIR={RUNNER_DIR} PATH=x", "SmokeRunner", self.SMOKE_JOB)
        self.assertEqual((staged, reason), ("True", "staged-already"))
        self.assertEqual(line, f"RESULT=SMOKE_RUNNER_STAGE_OK ALREADY=1 CACHE_DIR={RUNNER_DIR}")
        fresh = self.reuse(f"RESULT=SMOKE_RUNNER_STAGE_OK CACHE_DIR={RUNNER_DIR} PATH=x FILES=8", "SmokeRunner", self.SMOKE_JOB)
        self.assertEqual(fresh[:2], ("True", "staged-already"))

    def test_smoke_runner_reuse_still_refuses_every_other_shape(self) -> None:
        # the pin is widened by exactly one optional token in one place; the closure check is unchanged
        other_dir = "smoke-runner-" + "c" * 16
        for stdout, why in (
            (f"RESULT=SMOKE_RUNNER_STAGE_OK ALREADY=1 CACHE_DIR={other_dir} PATH=x", "CACHE_DIR-closure-mismatch"),
            (f"RESULT=SMOKE_RUNNER_STAGE_OK ALREADY=2 CACHE_DIR={RUNNER_DIR} PATH=x", "no-RESULT=SMOKE_RUNNER_STAGE_OK-line"),
            (f"RESULT=SMOKE_RUNNER_STAGE_OK ALREADY=1  CACHE_DIR={RUNNER_DIR} PATH=x", "no-RESULT=SMOKE_RUNNER_STAGE_OK-line"),
            (f"RESULT=SMOKE_RUNNER_STAGE_OK ALREADY=1 ALREADY=1 CACHE_DIR={RUNNER_DIR} PATH=x", "no-RESULT=SMOKE_RUNNER_STAGE_OK-line"),
            (f"RESULT=SMOKE_RUNNER_STAGE_OK X=1 CACHE_DIR={RUNNER_DIR} PATH=x", "no-RESULT=SMOKE_RUNNER_STAGE_OK-line"),
            (f"RESULT=SMOKE_RUNNER_STAGE_OKALREADY=1 CACHE_DIR={RUNNER_DIR} PATH=x", "no-RESULT=SMOKE_RUNNER_STAGE_OK-line"),
            (f"RESULT=SMOKE_RUNNER_STAGE_OK ALREADY=1 CACHE_DIR={RUNNER_DIR}0 PATH=x", "no-RESULT=SMOKE_RUNNER_STAGE_OK-line"),
            (f" RESULT=SMOKE_RUNNER_STAGE_OK ALREADY=1 CACHE_DIR={RUNNER_DIR} PATH=x", "no-RESULT=SMOKE_RUNNER_STAGE_OK-line"),
            (f"RESULT=SMOKE_RUNNER_STAGE_FAILED ALREADY=1 CACHE_DIR={RUNNER_DIR} PATH=x", "no-RESULT=SMOKE_RUNNER_STAGE_OK-line"),
        ):
            staged, reason, _ = self.reuse(stdout, "SmokeRunner", self.SMOKE_JOB)
            self.assertEqual(staged, "False", stdout)
            self.assertTrue(reason.startswith(why), (stdout, reason))

    def test_the_contract_census_sees_an_emission_the_pin_cannot_render(self) -> None:
        # adversary A1 (SMOKE-RUNNER-STAGE-OK-ALREADY-TOKEN-1 r1, advisory): a single-quoted OK line inserted before the fresh emission was invisible to the
        # pin. Each writer form below is one more RESULT=<token> than the pin renders, so the contract test reds on it.
        text = (ROOT / "tools" / "profiling" / "bachelor" / "attr3-stage-smoke-runner-job.ps1").read_text(encoding="utf-8")
        fresh = 'Write-Output "RESULT=SMOKE_RUNNER_STAGE_OK CACHE_DIR='
        self.assertEqual(text.count(fresh), 1)
        lines, census = ok_emissions(text, "SMOKE_RUNNER_STAGE_OK")
        self.assertEqual((len(lines), census), (2, 2))
        for extra in ("if ($env:EXTRA_OK -eq '1') { Write-Output 'RESULT=SMOKE_RUNNER_STAGE_OK ALREADY=2 CACHE_DIR=smoke-runner-bbbbbbbbbbbbbbbb PATH=x'; exit 0 }",
                      "[Console]::Out.WriteLine(\"RESULT=SMOKE_RUNNER_STAGE_OK ALREADY=2 CACHE_DIR=$CacheDirName\")",
                      "\"RESULT=SMOKE_RUNNER_STAGE_OK ALREADY=2 CACHE_DIR=$CacheDirName\" | Write-Output",
                      "Write-Output (\"RESULT=SMOKE_RUNNER_STAGE_OK\" + ' ALREADY=2')",
                      # pass 2 A1: the token concatenated after a separate 'RESULT=' literal
                      "if ($env:EXTRA_OK -eq '1') { Write-Output ('RESULT=' + \"SMOKE_RUNNER_STAGE_OK ALREADY=2 CACHE_DIR=$CacheDirName PATH=x\"); exit 0 }"):
            lines, census = ok_emissions(text.replace(fresh, extra + "\n" + fresh, 1), "SMOKE_RUNNER_STAGE_OK")
            self.assertEqual((len(lines), census), (2, 3), extra)

    def test_a_further_plain_stage_ok_form_is_rendered_and_checked_not_a_count_red(self) -> None:
        # #814 adds 'RESULT=STAGE_OK SOURCE=$SourceCommit FILES=0 CACHE=$Cache REUSED=1' to the stage job; the pin renders it (census == lines) and the
        # Stage checker accepts it, so whichever PR merges second stays green
        path = ROOT / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-stage-job.ps1"
        text = path.read_text(encoding="utf-8")
        anchor = 'Write-Output "RESULT=STAGE_OK SOURCE='
        extra = 'Write-Output "RESULT=STAGE_OK SOURCE=$SourceCommit FILES=0 CACHE=$Cache REUSED=1"'
        # both merge orders (pass 3 A1): the job as it is here, and the job once #814 has landed its second form
        for base in (text, text.replace(anchor, extra + "\n" + anchor, 1)):
            self.assertGreaterEqual(base.count(anchor), 1)    # never an exact count
            lines0, census0 = ok_emissions(base, "STAGE_OK")
            self.assertEqual(len(lines0), census0)
            lines, census = ok_emissions(base.replace(anchor, extra + "\n" + anchor, 1), "STAGE_OK")
            self.assertEqual((len(lines), census), (len(lines0) + 1, census0 + 1))
        staged, reason, _ = self.reuse(f"RESULT=STAGE_OK SOURCE={SHA} FILES=0 CACHE=x REUSED=1", "Stage", f"playback-attr-3-cuda-stage-{S12}")
        self.assertEqual((staged, reason), ("True", "staged-already"))

    def test_a_quote_in_the_temp_path_reaches_the_checker(self) -> None:
        # adversary r1 pass 2 A2: a TEMP such as C:\Temp\O'Neil ended the single-quoted -ResultPath literal and the command never parsed
        with tempfile.TemporaryDirectory(prefix="O'Neil-") as d:
            res = Path(d) / "r.json"
            res.write_text(json.dumps({"jobId": self.SMOKE_JOB, "exitCode": 0, "stdout": f"RESULT=SMOKE_RUNNER_STAGE_OK ALREADY=1 CACHE_DIR={RUNNER_DIR} PATH=x"}),
                           encoding="utf-8")
            out = self.ps(f"(Get-StageReuse -ResultPath '{psq(res)}' -JobId '{self.SMOKE_JOB}' -Sha '{SHA}' -Kind SmokeRunner).Staged")
        self.assertEqual(out, "True")

    def test_every_emitted_ok_shape_is_accepted_by_its_checker(self) -> None:
        # CONTRACT PIN: every RESULT=..._OK line the three staging jobs can print, rendered with sample values, passes the reuse check that reads it. A new or
        # changed emitter shape reds this test instead of a venue night (SMOKE-RUNNER-STAGE-OK-ALREADY-TOKEN-1: the ALREADY=1 form had no checker row).
        bachelor = ROOT / "tools" / "profiling" / "bachelor"
        dll_job = f"playback-attr-3-cuda-dllpair-{S12}"

        def emitted(path: Path, token: str) -> list[str]:
            lines, census = ok_emissions(path.read_text(encoding="utf-8"), token)
            self.assertEqual(len(lines), census, f"{path.name}: a RESULT={token} emission the contract pin cannot render; give it a checker row")
            return lines

        def render(line: str, subs: dict[str, str]) -> str:
            for var, val in subs.items():
                line = re.sub(r"\$" + var + r"\b", val, line)
            line = re.sub(r"\$\([^()]*\)", "8", line)        # $($x.Count) and the like
            return re.sub(r"\$\w+", "x", line)                # any other path / label variable

        smoke = emitted(bachelor / "attr3-stage-smoke-runner-job.ps1", "SMOKE_RUNNER_STAGE_OK")
        self.assertEqual(len(smoke), 2, smoke)               # the fresh form and the ALREADY=1 form; a third shape needs its own checker row
        for raw in smoke:
            line = render(raw, {"CacheDirName": RUNNER_DIR})
            staged, reason, _ = self.reuse(line, "SmokeRunner", self.SMOKE_JOB)
            self.assertEqual((staged, reason), ("True", "staged-already"), (raw, line))

        # stage and DLL pair: at least one form, never a fixed count, so a further form another PR adds (#814: REUSED=1) is checked here, not a merge-order red;
        # the census above still reds any form the pin cannot render
        stage = emitted(bachelor / "playback-attr-3-cuda-stage-job.ps1", "STAGE_OK")
        self.assertGreaterEqual(len(stage), 1, stage)
        for raw in stage:
            line = render(raw, {"SourceCommit": SHA})
            staged, reason, _ = self.reuse(line, "Stage", f"playback-attr-3-cuda-stage-{S12}")
            self.assertEqual((staged, reason), ("True", "staged-already"), (raw, line))

        dll = emitted(ROOT / "tools" / "profiling" / "ultramagnus" / "playback-attr-3-cuda-dll-job.ps1", "DLL_PAIR_OK")
        self.assertGreaterEqual(len(dll), 1, dll)
        for raw in dll:
            line = render(raw, {"SourceCommit": SHA})
            with tempfile.TemporaryDirectory() as d:
                outbox = Path(d)
                (outbox / f"{dll_job}.artifacts").mkdir()
                (outbox / f"{dll_job}.artifacts" / "dll-pair-manifest.json").write_text(json.dumps({"sourceCommit": SHA, "jobId": dll_job}), encoding="utf-8")
                (outbox / f"{dll_job}.result.json").write_text(json.dumps({"jobId": dll_job, "exitCode": 0, "stdout": line, "endedUtc": "2026-11-15T00:00:00Z"}),
                                                               encoding="utf-8")
                ok = self.ps(f"(Get-DllPairReuse -Outbox '{psq(outbox)}' -JobId '{dll_job}' -Sha '{SHA}').Reusable")
            self.assertEqual(ok, "True", (raw, line))


@requires_windows_pwsh
class StageNotAfterTypedExitTests(unittest.TestCase):
    """LEASE-NOT-AFTER-SUBMITTER-DEADLINE-TIMING-1 r3 (STAGE-NOTAFTER-THROW-TYPED-EXIT-1, sol/fable r2 hardening): the driver's Invoke-UmRun, lifted out of the
    driver as it is, with its collaborators stubbed. The remaining-budget sample says 600 s are left, but the absolute deadline has already passed when the
    not-after helper runs (a scheduling delay between the two): the helper's DVE_NOT_AFTER_DEADLINE_PASSED is the typed STAGE_CAP_EXCEEDED exit, never an
    untyped throw, and nothing reaches um-run."""

    def lifted(self, deadline_offset_sec: int, later_harvest: bool = False) -> subprocess.CompletedProcess:
        text = DRIVER.read_text(encoding="utf-8")
        fn = text[text.index("function Invoke-UmRun("):text.index("# ------------------------------------------------------------------ 6. DLL pair")]
        script = (
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{DV / 'DualVenueRunner.psm1'}' -Force -DisableNameChecking\n"
            "function Assert-VenueFree { param($w) }\n"
            "function Assert-StagePackageSideFile { param($Side, $JobId, $FailToken) }\n"
            "function Get-RemainingSec { 600 }\n"
            "function Say { param($m) [Console]::Out.WriteLine(\"SAY $m\") }\n"
            "function Invoke-Child { [Console]::Out.WriteLine('CHILD_STARTED'); [pscustomobject]@{ timedOut = $false; exit = 0; out = 'UMRUN_RETURNED'; err = '' } }\n"
            "function Stop-Nightly([string]$Reason, [string]$Detail = '') { [Console]::Out.WriteLine(\"STOP $Reason $Detail\"); exit 50 }\n"
            "$UmRun = 'C:\\nowhere\\um-run.ps1'; $MaxTotalSec = 600; $TestNotAfterSampleDelayMs = 0\n"
            f"$script:Deadline = [DateTime]::UtcNow.AddSeconds({deadline_offset_sec})\n"
            + fn +
            f"\n$r = Invoke-UmRun 'stage' 'C:\\nowhere\\job.ps1' @() 'stage-job' 'C:\\nowhere\\share' 3600 'STAGE_FAILED'{' -LaterHarvest' if later_harvest else ''}\n"
            "[Console]::Out.WriteLine(\"RETURNED $r\")\n"
        )
        return subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120,
                              stdin=subprocess.DEVNULL)

    def test_a_deadline_passed_at_the_bound_is_the_typed_cap_exit_and_submits_nothing(self) -> None:
        p = self.lifted(-5)
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 50, out)
        self.assertRegex(p.stdout, r"(?m)^STOP STAGE_CAP_EXCEEDED .*deadline passed before the stage submission's bound was computed; nothing was submitted")
        self.assertIn("DVE_NOT_AFTER_DEADLINE_PASSED", p.stdout)
        self.assertNotIn("CHILD_STARTED", out, "um-run is never started")

    def test_a_live_deadline_still_submits(self) -> None:
        p = self.lifted(3600)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("CHILD_STARTED", p.stdout)
        self.assertIn("RETURNED OK", p.stdout)


@requires_windows_pwsh
class NonFiniteBudgetEndpointTests(unittest.TestCase):
    """VENUE-STAGE-SAME-SHA-RESTAGE-GUARD-1 r28 (sol r27 blocker VENUE-STAGE-NONFINITE-ENDPOINT-ERASES-CAP-1, catalogue B-03): a caller-budget endpoint that is not a
    finite number (NaN, +Inf, -Inf) is REFUSED with the typed VENUE_BUDGET_ENDPOINT_NONFINITE before any read starts, at every site that takes one
    (Get-VenueStagedBuildManifest -CallBudgetEndSec, Invoke-VenueBoundedRead -EndSec). RED on 89bb7c35: a NaN endpoint went through [Math]::Max/[Math]::Min,
    replaced -CallCapSec 1 with NaN, and the read ran to -TimeoutSec 5 (STAGED after 1.5 s); Invoke-VenueBoundedRead took -EndSec NaN as no end at all."""

    SHA64 = "a" * 64
    ENDPOINTS = (("nan", "[double]::NaN"), ("posinf", "[double]::PositiveInfinity"), ("neginf", "[double]::NegativeInfinity"))

    def run_ps(self, body: str) -> tuple[str, str]:
        mod = DV / "VenueChain.psm1"
        ps = f"Import-Module '{psq(mod)}' -Force -DisableNameChecking; & (Get-Module VenueChain) {{ {body} }}; [Environment]::Exit(0)"
        p = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL)
        return p.stdout, p.stdout + p.stderr

    def rows(self, out: str) -> dict[str, tuple[str, float]]:
        got = {}
        for m in re.finditer(r"(?m)^ROW=([a-z0-9-]+)\|(.*?)\|SEC=([0-9.]+)\r?$", out):
            got[m.group(1)] = (m.group(2), float(m.group(3)))
        return got

    def test_staged_manifest_refuses_a_non_finite_call_budget_end_and_never_widens_the_cap(self) -> None:
        cases = [(f"{label}-cap1", f"-CallCapSec 1 -CallBudgetEndSec ({expr})") for label, expr in self.ENDPOINTS]
        # the budget alone (no explicit cap): NaN erased the budget itself on 89bb7c35 (-CallCapSec became NaN = no cap)
        cases.append(("nan-cap0", "-CallBudgetEndSec ([double]::NaN)"))
        body = "".join(
            f"$sw = [Diagnostics.Stopwatch]::StartNew(); "
            f"try {{ $r = Get-VenueStagedBuildManifest -AgentShare 'C:\\unused' -SourceCommit ('a' * 40) -TimeoutSec 5 {args} "
            f"-CallBudgetClock ([Diagnostics.Stopwatch]::StartNew()) -ProbeScript {{ param($p) [Threading.Thread]::Sleep(1500); ('a' * 64) }}; $o = 'STATE=' + $r.state }} "
            f"catch {{ $o = 'THROW=' + ($_.Exception.Message -replace '\\s+', ' ') }}; "
            f"'ROW={label}|' + $o + '|SEC=' + $sw.Elapsed.TotalSeconds.ToString('0.000', [Globalization.CultureInfo]::InvariantCulture); "
            for label, args in cases)
        out, both = self.run_ps(body)
        got = self.rows(out)
        for label, _ in cases:
            with self.subTest(endpoint=label):
                self.assertIn(label, got, both)
                what, sec = got[label]
                self.assertTrue(what.startswith("THROW=VENUE_BUDGET_ENDPOINT_NONFINITE "), (label, what, both))
                self.assertIn("-CallBudgetEndSec", what, both)
                self.assertNotIn("STATE=STAGED", what, both)
                # refused before the read: the 1.5 s probe never ran
                self.assertLess(sec, 1.0, (label, what, both))

    def test_staged_manifest_cap_and_timeout_refuse_non_finite_values_at_binding(self) -> None:
        # the sweep's claim for the two seconds parameters that are not endpoints: [ValidateRange] on a [double] already refuses NaN and +/-Inf at binding
        cases = [(f"{param.lower()}-{label}", f"-{param} ({expr})") for param in ("CallCapSec", "TimeoutSec") for label, expr in self.ENDPOINTS]
        body = "".join(
            f"$sw = [Diagnostics.Stopwatch]::StartNew(); "
            f"try {{ $r = Get-VenueStagedBuildManifest -AgentShare 'C:\\unused' -SourceCommit ('a' * 40) {args} "
            f"-ProbeScript {{ param($p) [Threading.Thread]::Sleep(1500); ('a' * 64) }}; $o = 'STATE=' + $r.state }} "
            f"catch {{ $o = 'THROW=' + ($_.Exception.Message -replace '\\s+', ' ') }}; "
            f"'ROW={label}|' + $o + '|SEC=' + $sw.Elapsed.TotalSeconds.ToString('0.000', [Globalization.CultureInfo]::InvariantCulture); "
            for label, args in cases)
        out, both = self.run_ps(body)
        got = self.rows(out)
        for label, _ in cases:
            with self.subTest(value=label):
                self.assertIn(label, got, both)
                what, sec = got[label]
                self.assertTrue(what.startswith("THROW="), (label, what, both))
                self.assertIn("Cannot validate argument on parameter", what, both)
                self.assertLess(sec, 1.0, (label, what, both))

    def test_bounded_read_refuses_a_non_finite_end_and_never_widens_the_wait(self) -> None:
        body = "".join(
            f"$sw = [Diagnostics.Stopwatch]::StartNew(); "
            f"try {{ $r = Invoke-VenueBoundedRead -ScriptText 'param($p) [Threading.Thread]::Sleep(1500); 1' -Argument 'x' -TimeoutSec 5 "
            f"-Clock ([Diagnostics.Stopwatch]::StartNew()) -EndSec ({expr}); $o = 'ANSWERED=' + $r.answered + ' ' + $r.error }} "
            f"catch {{ $o = 'THROW=' + ($_.Exception.Message -replace '\\s+', ' ') }}; "
            f"'ROW={label}|' + $o + '|SEC=' + $sw.Elapsed.TotalSeconds.ToString('0.000', [Globalization.CultureInfo]::InvariantCulture); "
            for label, expr in self.ENDPOINTS)
        out, both = self.run_ps(body)
        got = self.rows(out)
        for label, _ in self.ENDPOINTS:
            with self.subTest(endpoint=label):
                self.assertIn(label, got, both)
                what, sec = got[label]
                self.assertTrue(what.startswith("THROW=VENUE_BUDGET_ENDPOINT_NONFINITE "), (label, what, both))
                self.assertIn("-EndSec", what, both)
                self.assertNotIn("ANSWERED=True", what, both)
                self.assertLess(sec, 1.0, (label, what, both))
        # a finite end still clamps as before (control: no regression of the r13 behaviour)
        out, both = self.run_ps("$r = Invoke-VenueBoundedRead -ScriptText 'param($p) [Threading.Thread]::Sleep(3000); 1' -Argument 'x' -TimeoutSec 5 "
                                "-Clock ([Diagnostics.Stopwatch]::StartNew()) -EndSec 1; 'CTRL=' + $r.answered")
        self.assertIn("CTRL=False", out, both)


if __name__ == "__main__":
    unittest.main()
