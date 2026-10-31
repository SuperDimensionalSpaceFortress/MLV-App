"""Tests for BACHELOR-LEASE-BATCH-LEGS-1: tools/profiling/dual-venue/Invoke-VenueLeaseBatch.ps1 (bench legs INSIDE one quiet lease per batch), its
VenueChain.psm1 helpers, and the job-id contract it shares with the Bachelor agent (tools/profiling/bachelor/ultra-magnus-agent.ps1, lease-batch block).

WHAT IS PINNED
  * LEASE FIRST: the batch's open job (the agent requests the lease for it) is submitted before any quiet probe or leg, and ONE open serves every leg.
  * IN-LEASE RETRY: a busy probe (COOLDOWN_UNMET, GATE_BUSY) or a leg whose own re-check failed (VENUE_NOT_QUIESCENT) is retried inside the same lease:
    no second open, no release.
  * A lease too short for the next leg ends the batch cleanly (LEASE_REMAINING_SHORT) and the rest go to the next batch; a leg that did not run on the
    batch's lease is LEASE_LOST (uncounted) and ends the batch; an agent with no lease batch stops the run before any probe or leg (exit 10).
  * Typed receipts per batch (batch-<nn>.json, batches.jsonl) and the close job that releases the lease.
  * r3 (sol r2 blockers): an open still in the agent's gate at the end of a batch is CANCELLED and reconciled by name before the claim is released, so
    it can never take a lease after the runner exits (or the run says UNRECONCILED, exit 11); a leg still running at the lease's end is killed with its
    whole process tree and typed LEASE_EXPIRED (uncounted, never re-run), and a leg the agent stopped at its lease's bound is typed by the agent's reason.
    Also the LEG_FAILED exit-7 path at run time, the busy-probe spin bound, and a leg's own exit-75 refusals in the batch receipt (fable r2).
  * The agent's own pieces (its RED/GREEN cooldown case lives in tools/testing/test-ultra-magnus-agent-quiet-lease.ps1): a leg job id built by
    Invoke-VenueLeg -LeaseBatch carries the token the agent reads, and its health probe stays a light job.

Offline only: Wait-VenueQuiet, Invoke-VenueLeg and um-run.ps1 are fake scripts passed through the runner's seams, the agent share is a temp directory and every
claim lives in a temp -ClaimDir. Nothing here reaches a venue. Windows-only (PowerShell tools).
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
DV = ROOT / "tools" / "profiling" / "dual-venue"
RUNNER = DV / "Invoke-VenueLeaseBatch.ps1"
MODULE = DV / "VenueChain.psm1"
LEG = DV / "Invoke-VenueLeg.ps1"
AGENT = ROOT / "tools" / "profiling" / "bachelor" / "ultra-magnus-agent.ps1"
PWSH = shutil.which("pwsh")
requires_windows_pwsh = unittest.skipIf(PWSH is None or sys.platform != "win32", "needs pwsh on Windows")

SHA = "a" * 40
MANIFEST = "b" * 64
# r13 (adversary r13 A1): the three artifact digests the leg job hashes the cached artifacts against (playback-attr-3-cuda-job.ps1 $manifestChecks), in every staged manifest
ARTIFACT_SHAS = {"packageZip": {"sha256": "a" * 64}, "exe": {"sha256": "b" * 64}, "dll": {"sha256": "c" * 64}}
ABSENT = object()   # a staged build manifest field left out (stage(): dllPairManifestSha256=ABSENT)

# Wait-VenueQuiet stand-in: mode per call from FAKE_QUIET_MODES (comma list, the last repeats), counted in the run dir; each call is logged with UTC ticks.
FAKE_QUIET = r"""
param([string]$Venue, [string]$WorkDir, [string]$GateLog, [double]$ThresholdPercent = 20, [int]$MaxWaitSec = 1800, [int]$RecheckSec = 90, [int]$MaxAdmissionWaitSec = 0)
$runDir = Split-Path -Parent $WorkDir
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
$countFile = Join-Path $runDir 'fake-quiet-count.txt'
$n = $(if (Test-Path -LiteralPath $countFile) { [int](Get-Content -LiteralPath $countFile) } else { 0 })
Set-Content -LiteralPath $countFile -Value ($n + 1)
$modes = @(([string]$env:FAKE_QUIET_MODES) -split ',' | Where-Object { $_ })
if ($modes.Count -eq 0) { $modes = @('QUIET') }
$mode = $modes[[Math]::Min($n, $modes.Count - 1)]
Add-Content -LiteralPath $env:FAKE_EVENTS -Value ("{0} QUIET {1} maxWait={2}" -f [DateTime]::UtcNow.Ticks, $mode, $MaxWaitSec)
switch ($mode) {
    'QUIET' { "QUIET mean=5.0% threshold=$ThresholdPercent% waitedSec=1"; exit 0 }
    'COOLDOWN' { "COOLDOWN_UNMET mean=32.0% threshold=$ThresholdPercent% waitedSec=1"; exit 0 }
    'FOREIGN' { 'FOREIGN_FLOOR owners=explorer:1,textinputhost:2,bloody7:3 floorMean=32.0% waitedSec=1 probes=3'; "COOLDOWN_UNMET mean=32.0% threshold=$ThresholdPercent% waitedSec=1"; exit 0 }
    'BUSY' { 'GATE_BUSY quiet-probe waitedSec=1'; exit 3 }
    'UNKNOWN' { "VENUE_QUIET_UNKNOWN mean=UNKNOWN threshold=$ThresholdPercent% waitedSec=1 reason=probe-failed"; exit 6 }
    'STEAL' {
        # r5b (Grok G1): another owner's claim replaces the run's mid-batch (another nonce; its owner pid is dead, so the next batch may break it). The probe
        # itself reads QUIET; it waits long enough for a 1 s claim keeper to see the claim NOT_OURS.
        for ($i = 0; $i -lt 20; $i++) {
            try { $t = [IO.File]::ReadAllText($env:FAKE_CLAIM_PATH); [IO.File]::WriteAllText($env:FAKE_CLAIM_PATH, ($t -replace '"nonce":"[^"]*"', '"nonce":"stolen00000000"' -replace '"ownerPid":\d+', '"ownerPid":99999999')); break } catch { Start-Sleep -Milliseconds 100 }
        }
        Start-Sleep -Seconds 3
        "QUIET mean=5.0% threshold=$ThresholdPercent% waitedSec=1"; exit 0
    }
}
"""

# um-run.ps1 stand-in, invoked IN-PROCESS by the runner exactly as um-run is. It plays the lease-batch agent for the open and close jobs: an open job gets a
# result whose admission.leaseBatch names lease-<n> (FAKE_OPEN_MODES per open: batch | nobatch | retracted; FAKE_LEASE_REMAIN per open: seconds left), and a
# close job appends the agent's receipt line to <share>\logs\lease-batch-<token>.jsonl.
FAKE_UMRUN = r"""
param([string]$ScriptPath, [string]$JobId, [string]$AgentShare, [int]$TimeoutSec, [int]$MaxQueueWaitSec, [int]$MaxClaimedWaitSec, [int]$MaxAdmissionWaitSec, [string]$NotAfterUtc = '<unset>')
if ($JobId -like 'venue-quiet-probe-caps-*') {
    # BACHELOR-DEDICATED-CAPS-RUNNER-1: the exempt light probe that prints Bachelor's heavy-slot.lease.json. Logged apart from the lease-batch events (FAKE_CAPS_EVENTS)
    # so every other case reads the same event order. FAKE_CAPS_MODE: fail (um-run threw: the default, so a case that does not care reads the fallback) | live
    # (stdout = FAKE_CAPS_JSON) | exit1 (the probe job exited 1 with the JSON on stdout).
    Add-Content -LiteralPath $env:FAKE_CAPS_EVENTS -Value ("{0} CAPS {1} claimHeld={2} notAfter={3} body={4}" -f [DateTime]::UtcNow.Ticks, $JobId, [bool]($env:FAKE_CLAIM_PATH -and (Test-Path -LiteralPath $env:FAKE_CLAIM_PATH)), $NotAfterUtc, ((Get-Content -LiteralPath $ScriptPath -Raw) -replace '\s+', ' ').Trim())
    $cm = $(if ($env:FAKE_CAPS_MODE) { $env:FAKE_CAPS_MODE } else { 'fail' })
    if ($cm -eq 'fail') { throw "UNRESOLVED: $JobId (fake) was not taken by the agent before -MaxQueueWaitSec" }
    return [pscustomobject]@{ jobId = $JobId; exitCode = $(if ($cm -eq 'exit1') { 1 } else { 0 }); endedUtc = [DateTimeOffset]::UtcNow.ToString('o'); admission = [pscustomobject]@{ label = 'exempt-light' }; stdout = [string]$env:FAKE_CAPS_JSON }
}
$stateFile = Join-Path $AgentShare 'fake-agent-state.json'
$st = $(if (Test-Path -LiteralPath $stateFile) { Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json } else { [pscustomobject]@{ opens = 0; leaseId = $null; token = $null } })
$held = [bool]($env:FAKE_CLAIM_PATH -and (Test-Path -LiteralPath $env:FAKE_CLAIM_PATH))
# r4: -NotAfterUtc as passed ('<unset>' when the runner did not pass it), which the real um-run writes as meta.json notAfterUtc
Add-Content -LiteralPath $env:FAKE_EVENTS -Value ("{0} UMRUN {1} claimHeld={2} notAfter={3} body={4}" -f [DateTime]::UtcNow.Ticks, $JobId, $held, $NotAfterUtc, ((Get-Content -LiteralPath $ScriptPath -Raw) -replace '\s+', ' ').Trim())
# r5b: the job is dropped into <share>\inbox\<jobId>.job.ps1 first, as the real um-run does (um-run.ps1 $jobFile)
New-Item -ItemType Directory -Force -Path (Join-Path $AgentShare 'inbox') | Out-Null
Copy-Item -LiteralPath $ScriptPath -Destination (Join-Path $AgentShare "inbox\$JobId.job.ps1")
$now = [DateTimeOffset]::UtcNow
$token = $JobId.Substring($JobId.Length - 12)
function Pick([string]$List, [int]$I, [string]$Default) { $v = @(([string]$List) -split ',' | Where-Object { $_ }); if ($v.Count -eq 0) { return $Default }; $v[[Math]::Min($I, $v.Count - 1)] }
if ($JobId -like 'lease-batch-open-*') {
    $n = [int]$st.opens
    $mode = Pick $env:FAKE_OPEN_MODES $n 'batch'
    $remain = [int](Pick $env:FAKE_LEASE_REMAIN $n '2700')
    if ($mode -eq 'retracted') { throw 'RETRACTED: fake queue ceiling' }
    $lid = "lease-$($n + 1)"
    # r13 (adversary r12 A1): FAKE_OPEN_LEASE_ID answers with that exact lease id (a timestamp-shaped one, which ConvertFrom-Json turns into a DateTime)
    if ($env:FAKE_OPEN_LEASE_ID) { $lid = $env:FAKE_OPEN_LEASE_ID }
    $adm = [ordered]@{ gate = 'fake'; mode = 'lease'; label = 'clear'; heavySlotHeld = "1:$(1001 + $n)"; slot = 'C:\t\.heavy-slots\1'; leaseMinutes = 45; waitedSec = 7; leaseRequests = 2
        leaseRefusals = @([ordered]@{ reason = 'refused'; rc = 75; why = 'quiet lease cooldown' }) }
    # r12 (adversary A2): FAKE_OPEN_TOKEN_SHY=1 answers with the batch's token plus a soft hyphen (U+00AD): culture-aware equality says it is the same token, ordinal says it is not
    $tokOut = $(if ($env:FAKE_OPEN_TOKEN_SHY -eq '1') { $token + [string][char]0x00AD } else { $token })
    if ($mode -ne 'nobatch') { $adm['leaseBatch'] = [ordered]@{ token = $tokOut; leaseId = $lid; seq = 1; reused = $false; untilEpoch = $now.ToUnixTimeSeconds() + $remain; remainingSec = $remain } }
    [pscustomobject]@{ opens = $n + 1; leaseId = $lid; token = $token } | ConvertTo-Json | Set-Content -LiteralPath $stateFile
    # r5: FAKE_OPEN_SLEEP per open: um-run blocks this long first, as it does while the agent re-asks a refused (exit 75) request for the open
    $sleep = [int](Pick $env:FAKE_OPEN_SLEEP $n '0')
    # r5b: FAKE_OPEN_CLAIM_READONLY per open (1 = the claim file is read-only for that wait, so every renewal comes back BUSY; writable again after it)
    $ro = (Pick $env:FAKE_OPEN_CLAIM_READONLY $n '0') -eq '1'
    if ($ro) { Set-ItemProperty -LiteralPath $env:FAKE_CLAIM_PATH -Name IsReadOnly -Value $true }
    # LEASE-BATCH-PLAN-ARTIFACT-PREFLIGHT-1 r8: FAKE_OPEN_RELEASE (a path) ends that wait the moment the file exists, with FAKE_OPEN_SLEEP its upper bound, so a case that acts
    # during the wait releases the open when it is done instead of sleeping out a fixed margin
    if ($sleep -gt 0) {
        Add-Content -LiteralPath $env:FAKE_EVENTS -Value ("{0} OPENWAIT {1} sec={2}" -f [DateTime]::UtcNow.Ticks, $JobId, $sleep)
        if ($env:FAKE_OPEN_RELEASE) { $until = [DateTime]::UtcNow.AddSeconds($sleep); while (-not (Test-Path -LiteralPath $env:FAKE_OPEN_RELEASE) -and [DateTime]::UtcNow -lt $until) { Start-Sleep -Milliseconds 50 } } else { Start-Sleep -Seconds $sleep }
        $now = [DateTimeOffset]::UtcNow
    }
    if ($ro) { Set-ItemProperty -LiteralPath $env:FAKE_CLAIM_PATH -Name IsReadOnly -Value $false }
    $res = [pscustomobject]@{ jobId = $JobId; exitCode = 0; endedUtc = $now.ToString('o'); admission = [pscustomobject]$adm; stdout = 'LEASE_BATCH_OPEN' }
    if ($mode -eq 'exit1') { $res.exitCode = 1 }   # r14: the open job's receipt says it failed (OPEN_FAILED)
    if ($mode -eq 'gatehold') {
        # r3: um-run gave up while the agent still holds the open in its gate, and NO result lands: the agent keeps re-asking until it is cancelled.
        throw "UNRESOLVED: $JobId was held unlaunched in the agent's heavy-slot admission gate (fake, held) for the full -MaxAdmissionWaitSec"
    }
    if ($mode -eq 'gatewait') {
        # um-run gave up while the agent still held the open in its gate (exit-75 refusals); the agent admits it later and its result lands by name.
        $outbox = Join-Path $AgentShare 'outbox'
        New-Item -ItemType Directory -Force -Path $outbox | Out-Null
        $res | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $outbox "$JobId.result.json")
        throw "UNRESOLVED: $JobId was held unlaunched in the agent's heavy-slot admission gate (fake) for the full -MaxAdmissionWaitSec"
    }
    if ($env:FAKE_OPEN_VIA_REAL_UMRUN -eq '1') {
        # r14 (adversary r13 A1): the open's result reaches the runner through the REAL um-run.ps1 reader, the way it does in production: the agent's result file is staged in the outbox
        # at um-run's first poll and um-run returns what it read. The fake's own drop of the job file is withdrawn first (the real um-run drops it itself).
        Remove-Item -LiteralPath (Join-Path $AgentShare "inbox\$JobId.job.ps1") -Force
        [IO.File]::WriteAllText((Join-Path $AgentShare 'heartbeat.txt'), 'alive')
        $json = $res | ConvertTo-Json -Depth 6
        $outbox = Join-Path $AgentShare 'outbox'
        $hook = { New-Item -ItemType Directory -Force -Path $outbox | Out-Null; [IO.File]::WriteAllText((Join-Path $outbox "$JobId.result.json"), $json) }.GetNewClosure()
        return (& $env:FAKE_REAL_UMRUN -ScriptPath $ScriptPath -JobId $JobId -AgentShare $AgentShare -TimeoutSec 60 -PollSeconds 1 -TestHookAtLoopTop $hook 6>$null)
    }
    return $res
}
if ($JobId -like 'lease-batch-close-*') {
    # r3: FAKE_CLOSE_MODES per close: ok | timeout (um-run's queue ceiling: the close never ran)
    $cf = Join-Path $AgentShare 'fake-close-count.txt'
    $c = $(if (Test-Path -LiteralPath $cf) { [int](Get-Content -LiteralPath $cf) } else { 0 })
    Set-Content -LiteralPath $cf -Value ($c + 1)
    # r5b: the cancel files already present when the close was submitted
    Add-Content -LiteralPath (Join-Path $AgentShare 'fake-close-cancels.txt') -Value ("{0} {1}" -f $JobId, (@(Get-ChildItem -LiteralPath (Join-Path $AgentShare 'cancel') -Filter '*.cancel' -Name) -join ','))
    if ((Pick $env:FAKE_CLOSE_MODES $c 'ok') -eq 'timeout') { throw "UNRESOLVED: $JobId (fake) was not taken by the agent before -MaxQueueWaitSec" }
    $logs = Join-Path $AgentShare 'logs'
    New-Item -ItemType Directory -Force -Path $logs | Out-Null
    if ($st.token -eq $token) {
        Add-Content -LiteralPath (Join-Path $logs "lease-batch-$token.jsonl") -Value (([ordered]@{ schema = 'mlv-agent-lease-batch.v1'; token = $token; leaseId = $st.leaseId; endReason = 'closed'; jobsRun = 1 }) | ConvertTo-Json -Compress)
    }
    return [pscustomobject]@{ jobId = $JobId; exitCode = 0; endedUtc = $now.ToString('o'); admission = [pscustomobject]@{ label = 'exempt-light' }; stdout = 'LEASE_BATCH_CLOSE' }
}
throw "fake um-run: unexpected job $JobId"
"""

# Invoke-VenueLeg stand-in: prints DVE_JOB_ID lines (the health probe, then the leg job: "<stem>-lb<token>"), writes the leg job's agent result into the fake
# share's outbox (admission.leaseBatch on the current lease, or another one when the call index is in FAKE_LEG_LOSE), a receipt and its summary.json
# (pre-load FAKE_LEG_PRELOADS), and DVE_OUTCOME from FAKE_LEG_OUTCOMES. Each call is logged with UTC ticks and its -LeaseBatch.
FAKE_LEG = r"""
param([string]$Venue, [string]$LegSpec, [string]$SourceCommit, [string]$BuildManifestSha256, [string]$Backend = '', [string]$SheetCopyDir = '', [string]$LeaseBatch = '', [string]$ReceiptRoot = '')
$share = $env:FAKE_SHARE
# LEASE-BATCH-RECEIPT-ROOT-1: one line per call saying whether -ReceiptRoot was passed at all (absent and empty are different: the batch must forward NOTHING when it has none)
Add-Content -LiteralPath (Join-Path $env:FAKE_LEG_ROOT 'leg-receiptroot.log') -Value ("bound={0} root={1}" -f $PSBoundParameters.ContainsKey('ReceiptRoot'), $ReceiptRoot)
$st = Get-Content -LiteralPath (Join-Path $share 'fake-agent-state.json') -Raw | ConvertFrom-Json
$cf = Join-Path $share 'fake-leg-count.txt'
$k = $(if (Test-Path -LiteralPath $cf) { [int](Get-Content -LiteralPath $cf) } else { 0 })
Set-Content -LiteralPath $cf -Value ($k + 1)
function Pick([string]$List, [int]$I, [string]$Default) { $v = @(([string]$List) -split ',' | Where-Object { $_ }); if ($v.Count -eq 0) { return $Default }; $v[[Math]::Min($I, $v.Count - 1)] }
$leaf = [IO.Path]::GetFileNameWithoutExtension($LegSpec)
$held = [bool]($env:FAKE_CLAIM_PATH -and (Test-Path -LiteralPath $env:FAKE_CLAIM_PATH))
Add-Content -LiteralPath $env:FAKE_EVENTS -Value ("{0} LEG {1} leaseBatch={2} claimHeld={3}" -f [DateTime]::UtcNow.Ticks, $leaf, $LeaseBatch, $held)
$jobId = "dve-$leaf-$Venue-cpu-2026102300000$k$(if ($LeaseBatch) { "-lb$LeaseBatch" })"
[Console]::Out.WriteLine("DVE_JOB_ID=$jobId-health"); [Console]::Out.WriteLine("DVE_JOB_ID=$jobId"); [Console]::Out.Flush()
# r3: FAKE_LEG_MODES per call: ok | exit3 (died without a receipt) | hang (still running at lease end: it starts a grandchild, records both pids, and sleeps)
$legMode = Pick $env:FAKE_LEG_MODES $k 'ok'
if ($legMode -eq 'exit3') { exit 3 }
if ($legMode -eq 'hang') {
    $gc = Start-Process -FilePath ([Environment]::ProcessPath) -ArgumentList '-NoProfile -NonInteractive -Command Start-Sleep -Seconds 300' -PassThru -NoNewWindow
    Add-Content -LiteralPath (Join-Path $share 'fake-leg-pids.txt') -Value "$PID $($gc.Id) $jobId"
    Add-Content -LiteralPath $env:FAKE_EVENTS -Value ("{0} LEGHANG {1} pid={2}" -f [DateTime]::UtcNow.Ticks, $leaf, $PID)
    Start-Sleep -Seconds 150
    exit 0
}
$lose =@(([string]$env:FAKE_LEG_LOSE) -split ',' | Where-Object { $_ } | ForEach-Object { [int]$_ })
# r12 (adversary A2): FAKE_LEG_LEASE_SHY=1 reports the batch's lease id plus a soft hyphen (U+00AD): culture-aware equality says it is the same lease, ordinal says it is not
$lid = $(if ($lose -contains ($k + 1)) { 'lease-somebody-else' } elseif ($env:FAKE_LEG_LEASE_SHY -eq '1') { $st.leaseId + [string][char]0x00AD } elseif ($env:FAKE_LEG_LEASE_ID) { $env:FAKE_LEG_LEASE_ID } else { $st.leaseId })
# r13 (adversary r12 A1): FAKE_LEG_LEASE_ID reports that exact lease id (a different spelling of the batch's instant), instead of the batch's own
$now = [DateTimeOffset]::UtcNow
$remain = [int](Pick $env:FAKE_LEG_REMAIN $k '2600')
$outbox = Join-Path $share 'outbox'
New-Item -ItemType Directory -Force -Path $outbox | Out-Null
$res = [ordered]@{ jobId = $jobId; exitCode = 0; endedUtc = $now.ToString('o'); admission = [ordered]@{ mode = 'lease'; label = 'clear'; leaseBatch = [ordered]@{ token = $LeaseBatch; leaseId = $lid; seq = $k + 2; reused = $true; untilEpoch = $now.ToUnixTimeSeconds() + $remain } } }
# the agent's renew record at the leg's end: okMaxGapSec from FAKE_LEG_GAPS (default 3 s; 'none' = no record)
$gap = Pick $env:FAKE_LEG_GAPS $k '3'
if ($gap -ne 'none') { $res.admission['leaseRenew'] = [ordered]@{ intervalSec = 60; ok = 5; okMaxGapSec = [double]$gap; staleSec = 120 } }
# r3: FAKE_LEG_REFUSALS per call: the exit-75 refusals of a fresh lease the leg's own job asked for; FAKE_LEG_STOPS per call: the agent stopped the job at its
# batch lease's bound (lease-end | demoted | lease-lost; 'none' = it ended on its own)
$nRef = [int](Pick $env:FAKE_LEG_REFUSALS $k '0')
if ($nRef -gt 0) { $res.admission['leaseRefusals'] = @(1..$nRef | ForEach-Object { [ordered]@{ reason = 'refused'; rc = 75; why = 'quiet lease cooldown' } }) }
$stop = Pick $env:FAKE_LEG_STOPS $k 'none'
if ($stop -ne 'none') { $res.exitCode = 124; $res.admission['slotContinuity'] = 'lost'; $res.admission['leaseStop'] = [ordered]@{ reason = $stop; why = 'fake' } }
# r5b: FAKE_LEG_MODES 'unresolved' = um-run said UNRESOLVED for the leg's job (um-run.ps1:480-501): no agent result lands by name, and Invoke-VenueLeg still
# writes its receipt (DVE_OUTCOME=UNRESOLVED) and exits 0
if ($legMode -ne 'unresolved') { $res | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $outbox "$jobId.result.json") }
$id = [guid]::NewGuid().ToString()
# LEASE-BATCH-RECEIPT-ROOT-1: like the real leg, the receipt goes under -ReceiptRoot when given and its evidence in the directory beside the root (the default root is <case>\receipts)
$rroot = $(if ($ReceiptRoot) { $ReceiptRoot } else { Join-Path $env:FAKE_LEG_ROOT 'receipts' })
$rdir = Join-Path $rroot "CARD-1\$leaf\$Venue"
New-Item -ItemType Directory -Force -Path $rdir | Out-Null
$rp = Join-Path $rdir "$id.json"; Set-Content -LiteralPath $rp -Value '{}'
$edir = Join-Path (Join-Path (Split-Path -Parent $rroot) 'evidence') $id; New-Item -ItemType Directory -Force -Path $edir | Out-Null
$pre = Pick $env:FAKE_LEG_PRELOADS $k '5'
if ($pre -ne 'none') { Set-Content -LiteralPath (Join-Path $edir 'summary.json') -Value ('{"cpuQuiescence":{"timeMeanPercent":' + $pre + '}}') }
"DVE_OUTCOME=$(if ($legMode -eq 'unresolved') { 'UNRESOLVED' } else { Pick $env:FAKE_LEG_OUTCOMES $k 'PASS' })"
"DVE_RECEIPT_PATH=$rp"
exit 0
"""


class _FakeAgentGate(threading.Thread):
    """r3 (sol r2 B1): the agent holding a batch's open in its admission gate, re-asking a refused (exit 75) lease request. It answers a cancel file for
    the open with the agent's cancelled-in-gate result (Publish-GateUnlaunchedResult). Told the runner has exited, it takes the lease it was asking for
    if the open was never cancelled: heavy-slot.py admits the pending request once the foreign holder clears."""

    def __init__(self, share: Path, claim_path: Path) -> None:
        super().__init__(daemon=True)
        self.share, self.claim_path = share, claim_path
        self.runner_exited = threading.Event()
        self.cancel_seen: tuple[str, bool] | None = None   # (open job id, was the runner's claim still held then)
        self.acquired_after_exit = False

    def run(self) -> None:
        while not self.runner_exited.is_set():
            for f in (self.share / "cancel").glob("lease-batch-open-*.cancel"):
                if self.cancel_seen is None:
                    job = f.name[: -len(".cancel")]
                    self.cancel_seen = (job, self.claim_path.exists())
                    outbox = self.share / "outbox"
                    outbox.mkdir(exist_ok=True)
                    res = {"jobId": job, "exitCode": 130, "cancelled": True, "endedUtc": utc_iso(datetime.now(timezone.utc)),
                           "admission": {"gate": "fake", "mode": "lease", "label": "cancelled-in-gate", "state": "cancelled", "why": "cancel file present before launch",
                                         "leaseRefusals": [{"reason": "refused", "rc": 75, "why": "quiet lease cooldown"}]}}
                    tmp = outbox / f"{job}.result.json.tmp"
                    tmp.write_text(json.dumps(res), encoding="utf-8")
                    os.replace(tmp, outbox / f"{job}.result.json")
            time.sleep(0.1)
        if self.cancel_seen is None:
            self.acquired_after_exit = True


def _pid_alive(pid: int) -> bool:
    import ctypes
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        k.GetExitCodeProcess(h, ctypes.byref(code))
        return code.value == 259   # STILL_ACTIVE
    finally:
        k.CloseHandle(h)


def _wait_pid_gone(pid: int, timeout_s: float) -> bool:
    end = time.monotonic() + timeout_s
    while _pid_alive(pid) and time.monotonic() < end:
        time.sleep(0.1)
    return not _pid_alive(pid)


PS_SINGLE_QUOTES = "'‘’‚‛"   # the five characters PowerShell's tokenizer treats as a single quote


def psq(value: object) -> str:
    """`value` for the inside of a PowerShell single-quoted literal: every embedded single-quote character (the ASCII one and the four typographic ones PowerShell also accepts) is
    doubled, so a temp dir or a message that carries an apostrophe cannot end the literal."""
    return "".join(c * 2 if c in PS_SINGLE_QUOTES else c for c in str(value))


def utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(text: str) -> datetime:
    """An ISO stamp as PowerShell's 'o' writes it (7 fraction digits, Z or an offset), as an aware UTC datetime."""
    body, frac = text.replace("Z", "+00:00"), ""
    if "." in body:
        head, rest = body.split(".", 1)
        digits = "".join(ch for ch in rest if ch.isdigit())
        frac, tz = "." + digits[:6], rest[len(digits):]
        body = head + frac + tz
    dt = datetime.fromisoformat(body)
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def read_claim_json(path: Path, timeout_s: float = 10.0, sleep_s: float = 0.1) -> dict:
    """A claim file read that waits out the runner's own write (CI-FLAKE-LEASE-BATCH-CLAIM-READ-RACE-1, merge_group 40211588250).

    The runner takes the claim with CreateNew/FileShare.None and renews it in place under FileShare.None (VenueChain.psm1 Request-/Update-VenueClaim), so on Windows a
    reader that lands in that window gets PermissionError, on a half-written file JSONDecodeError, before the take FileNotFoundError. Retries those three for
    timeout_s, then raises the last one with the path.
    """
    end = time.monotonic() + timeout_s
    while True:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (PermissionError, json.JSONDecodeError, FileNotFoundError) as e:
            if time.monotonic() >= end:
                if isinstance(e, json.JSONDecodeError):
                    raise json.JSONDecodeError(f"{path}: {e.msg}", e.doc, e.pos) from e
                raise type(e)(e.errno, f"{e.strerror} (still failing after {timeout_s}s): {path}") from e
            time.sleep(sleep_s)


class ExclusiveHold:
    """`path` held open with share mode 0 (what Request-/Update-VenueClaim's FileShare.None does) until release() closes it.

    No thread and no timer: the hold ends only when release() runs, on whichever thread calls it, synchronously. A hold that expires on its own can lapse
    before the assertion that needs it, and a release that waits for another thread to be scheduled can land after a reader's deadline; neither is possible
    here (CI-FLAKE-LEASE-BATCH-CLAIM-READ-RACE-HARDEN-1 r2, sol r1). release() is idempotent and thread-safe; callers put it in a finally."""

    def __init__(self, path: Path) -> None:
        import ctypes
        from ctypes import wintypes
        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._k32.CreateFileW.restype = wintypes.HANDLE
        self._k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        self._k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._k32.CloseHandle.restype = wintypes.BOOL
        self._handle = self._k32.CreateFileW(str(path), 0x80000000 | 0x40000000, 0, None, 3, 0x80, None)   # GENERIC_READ|GENERIC_WRITE, share none, OPEN_EXISTING
        if self._handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        self._lock = threading.Lock()
        self.closed = False

    def release(self) -> bool:
        """Close the handle; True only for the call that closed it. A CloseHandle that fails raises OSError and leaves closed False: the hold may still be up."""
        import ctypes
        with self._lock:
            if self.closed:
                return False
            if not self._k32.CloseHandle(self._handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self.closed = True
            return True


def batch_result(stdout: str) -> dict:
    lines = [l for l in stdout.splitlines() if l.startswith("LEASE_BATCH_RESULT=")]
    if not lines:
        raise AssertionError("no LEASE_BATCH_RESULT line in:\n" + stdout)
    return json.loads(lines[-1][len("LEASE_BATCH_RESULT="):])


# LEASE-BATCH-PLAN-ARTIFACT-PREFLIGHT-1 r10: numeric-contract scanners for the dual-venue PowerShell. Both are tested on their own spellings below, so a pin cannot go blind
# to the one adjacent spelling it was written against (fable key3 r9 P-02).
def _ps_balanced(text: str, open_at: int, open_ch: str, close_ch: str) -> int:
    """Index of the bracket that closes the one at open_at, or -1."""
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == open_ch:
            depth += 1
        elif text[i] == close_ch:
            depth -= 1
            if depth == 0:
                return i
    return -1


def _ps_balanced_back(text: str, close_at: int, open_ch: str, close_ch: str) -> int:
    """Index of the bracket that opens the one closing at close_at, or -1."""
    depth = 0
    for i in range(close_at, -1, -1):
        if text[i] == close_ch:
            depth += 1
        elif text[i] == open_ch:
            depth -= 1
            if depth == 0:
                return i
    return -1


def _ps_split_args(text: str) -> list[str]:
    out, depth, cur = [], 0, []
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur).strip())
    return out


# an operand that is (or contains) a [double]: a double literal, a [double] cast, a TimeSpan total, a monotonic clock, or the runner's own double budget names
_PS_DOUBLE_OPERAND = re.compile(r"Total(?:Seconds|Milliseconds|Minutes)\b|\.Elapsed\b|\[(?:system\.)?double\]|(?<![\w.$])\d+\.\d+\b|\$(?:left|share)\b|\.sec\b", re.I)


# a second operand that is whole by construction (an [int] / [long] cast, or a Floor / Ceiling / Truncate): the Int32 overload loses nothing on it
_PS_INTEGRAL_OPERAND = re.compile(r"\s*(?:\[(?:int|int32|long|int64)\]|\[(?:system\.)?math\]::(?:floor|ceiling|truncate)\()", re.I)


def ps_integer_first_minmax(text: str) -> list[tuple[int, str]]:
    """Every [Math]::Min/Max call whose first operand is not a double while its second one is: PowerShell picks the Int32 overload for it and ROUNDS the double (2.3 -> 2).
    Heuristic: a second operand is a double only when it names one (a TimeSpan total, a clock, a double literal or cast, the runner's budget names). An untyped variable product such as
    `$f * $BudgetBasisSec` is invisible to it (VenueChain.psm1 Get-VenueLeaseCaps, carried as VENUE-LEASE-CAPS-MINMAX-OVERLOAD-1)."""
    hits = []
    for m in re.finditer(r"\[(?:system\.)?math\]::(?:min|max)\(", text, re.I):
        line_start = text.rfind("\n", 0, m.start()) + 1
        if "#" in text[line_start:m.start()]:
            continue  # a comment
        close = _ps_balanced(text, m.end() - 1, "(", ")")
        args = _ps_split_args(text[m.end():close]) if close > 0 else []
        if len(args) == 2 and not _PS_DOUBLE_OPERAND.search(args[0]) and _PS_DOUBLE_OPERAND.search(args[1]) and not _PS_INTEGRAL_OPERAND.match(args[1]):
            hits.append((text.count("\n", 0, m.start()) + 1, text[m.start():close + 1]))
    return hits


_PS_FLOAT_TYPE = re.compile(r"(?:system\.)?(?:double|single|float|decimal)", re.I)


def ps_float_validaterange_params(text: str) -> list[tuple[int, str, bool]]:
    """Every [ValidateRange(..)] on a floating-point parameter, as (line, text, has_integer_endpoint). The type may follow the attribute, precede it ([double][ValidateRange(..)]),
    sit past other attributes ([Parameter()]) and the attribute may span lines."""
    found = []
    for m in re.finditer(r"\[ValidateRange\(", text, re.I):
        close = _ps_balanced(text, m.end() - 1, "(", ")")
        if close < 0:
            continue
        end = close + 1
        while end < len(text) and text[end].isspace():
            end += 1
        if text[end:end + 1] != "]":
            continue
        groups = []
        j = end + 1
        while True:
            while j < len(text) and text[j].isspace():
                j += 1
            if text[j:j + 1] != "[":
                break
            e = _ps_balanced(text, j, "[", "]")
            if e < 0:
                break
            groups.append(text[j + 1:e].strip())
            j = e + 1
        i = m.start() - 1
        while True:
            while i >= 0 and text[i].isspace():
                i -= 1
            if i < 0 or text[i] != "]":
                break
            b = _ps_balanced_back(text, i, "[", "]")
            if b < 0:
                break
            groups.append(text[b + 1:i].strip())
            i = b - 1
        if any(_PS_FLOAT_TYPE.fullmatch(g) for g in groups):
            ends = _ps_split_args(text[m.end():close])
            found.append((text.count("\n", 0, m.start()) + 1, text[m.start():end + 1], any(re.fullmatch(r"-?\d+", e) for e in ends)))
    return found


@requires_windows_pwsh
class VenueLeaseBatchRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="venue-lease-batch-"))
        for name, text in (("fake-quiet.ps1", FAKE_QUIET), ("fake-umrun.ps1", FAKE_UMRUN), ("fake-leg.ps1", FAKE_LEG)):
            (cls.tmp / name).write_text(text, encoding="utf-8")
        cls.specs = []
        for name in ("fx-a", "fx-b", "fx-c"):
            p = cls.tmp / f"{name}.json"
            # the shape Invoke-VenueLeg accepts (schema + the backends it may run + the scaleFactor every real leg spec carries and its receipt block reads), so a -LegPlan item can be checked against it before any lease
            # (card + legId are what Invoke-VenueLeg hands New-DvReceipt's Mandatory parameters; every committed leg spec carries both)
            p.write_text(json.dumps({"schema": "mlv-app/dual-venue-leg/v1", "card": "TEST-CARD", "legId": name, "backends": ["cpu", "cuda"], "scaleFactor": 1}), encoding="utf-8")
            cls.specs.append(p)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self) -> None:
        self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))

    def _init_case(self, case: Path) -> None:
        self.case = case
        self.share = self.case / "share"
        self.share.mkdir()
        (self.share / "cancel").mkdir()   # the agent's cooperative cancel folder (the agent creates it on the real share)
        (self.share / "notafter").mkdir()   # r5: the agent's runner-liveness sidecar folder (#663; the agent creates it on the real share)
        self.claims = self.case / "claims"
        self.claim_path = self.claims / "bachelor.claim.json"
        self.events = self.case / "events.log"
        self.caps_events = self.case / "caps-events.log"
        self.run_dir = self.case / "run"
        self.runner = RUNNER

    def run_batch(self, specs: list[Path] | None = None, *extra: str, **env: str) -> subprocess.CompletedProcess:
        args, e = self.batch_cmd(specs, *extra, **env)
        return subprocess.run(args, capture_output=True, text=True, timeout=600, env=e)

    def batch_cmd(self, specs: list[Path] | None = None, *extra: str, **env: str) -> tuple[list[str], dict]:
        e = dict(os.environ)
        e.update({"FAKE_EVENTS": str(self.events), "FAKE_SHARE": str(self.share), "FAKE_CLAIM_PATH": str(self.claim_path), "FAKE_LEG_ROOT": str(self.case),
                  "FAKE_CAPS_EVENTS": str(self.caps_events)})
        omit = [x for x in env.pop("OMIT_DEFAULTS", "").split(",") if x]
        e.update(env)
        # Bachelor's 30 min cooldown is waited out between batches; the offline cases (a stub um-run) relax it unless they test it. A case may override any default
        # (in extra) or drop one (OMIT_DEFAULTS=-LeaseCooldownSec), which is how a case reads the caps the runner takes from the live probe.
        defaults = {"-LegBudgetSec": "60", "-LeaseMarginSec": "0", "-ClaimPollSec": "1", "-RecheckSec": "1",
                    "-DeadlineUtc": utc_iso(datetime.now(timezone.utc) + timedelta(seconds=240)), "-LeaseCooldownSec": "0"}
        for k in omit:
            defaults.pop(k)
        args = [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(self.runner), "-LegSpec", ",".join(str(s) for s in (specs or self.specs[:2])),
                "-RunDir", str(self.run_dir), "-SourceCommit", SHA, "-BuildManifestSha256", MANIFEST, "-ClaimDir", str(self.claims),
                "-QuietScript", str(self.tmp / "fake-quiet.ps1"), "-LegScript", str(self.tmp / "fake-leg.ps1"), "-UmRunScript", str(self.tmp / "fake-umrun.ps1"),
                "-AgentShare", str(self.share), *[x for k, v in defaults.items() if k not in extra for x in (k, v)], *extra]
        return args, e

    def kinds(self) -> list[str]:
        """The event order: OPEN / CLOSE (um-run), QUIET:<mode>, LEG:<leaf>:<leaseBatch>."""
        rows = []
        for line in self.events.read_text(encoding="utf-8").splitlines() if self.events.exists() else []:
            ticks, kind, rest = line.split(" ", 2)
            if kind == "UMRUN":
                job = rest.split()[0]
                rows.append((int(ticks), "OPEN:" + job[-12:] if job.startswith("lease-batch-open-") else "CLOSE:" + job[-12:]))
            elif kind == "QUIET":
                rows.append((int(ticks), "QUIET:" + rest.split()[0]))
            elif kind == "LEG":
                leaf, lb, _held = rest.split()
                rows.append((int(ticks), f"LEG:{leaf}:{lb.split('=', 1)[1]}"))
        return [k for _, k in sorted(rows)]

    def events_raw(self) -> list[str]:
        return self.events.read_text(encoding="utf-8").splitlines()

    def batches(self) -> list[dict]:
        return [json.loads(l) for l in (self.run_dir / "batches.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]

    # ---------------------------------------------------------------- lease first, one lease for every leg
    def test_lease_first_and_one_lease_for_every_leg(self) -> None:
        r = self.run_batch(self.specs)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        k = self.kinds()
        opens = [x for x in k if x.startswith("OPEN:")]
        self.assertEqual(len(opens), 1, k)
        token = opens[0].split(":")[1]
        self.assertEqual(k, [f"OPEN:{token}", "QUIET:QUIET", f"LEG:fx-a:{token}", "QUIET:QUIET", f"LEG:fx-b:{token}", "QUIET:QUIET", f"LEG:fx-c:{token}", f"CLOSE:{token}"])
        # the claim is held from the open to the close, and released after
        held = [l for l in self.events_raw() if " UMRUN " in l or " LEG " in l]
        self.assertEqual(len(held), 5)
        self.assertTrue(all("claimHeld=True" in l for l in held), held)
        self.assertFalse(self.claim_path.exists(), "the claim was left behind")
        res = batch_result(r.stdout)
        self.assertEqual((res["ran"], res["counted"], res["notRun"]), (3, 3, 0))
        [b] = self.batches()
        self.assertEqual(b["schema"], "mlv-app/venue-lease-batch/v1")
        self.assertEqual(b["token"], token)
        self.assertEqual((b["leaseId"], b["held"], b["endReason"]), ("lease-1", "1:1001", "ALL_LEGS_DONE"))
        self.assertEqual((b["legsAttempted"], b["legsRan"], b["legsCounted"], b["retries"]), (3, 3, 3, 0))
        self.assertEqual((b["openWaitedSec"], b["openRequests"], len(b["refusals"])), (7, 2, 1))
        self.assertEqual(b["agent"]["endReason"], "closed", "the agent's own receipt for the lease is attached")
        self.assertTrue(b["startedUtc"] and b["endedUtc"] and b["leaseEndUtc"])
        self.assertTrue(all(l["onLease"] and l["jobLease"] == "lease-1" and l["class"] == "QUIET_PRELOAD_OK" for l in b["legs"]), b["legs"])
        self.assertTrue((self.run_dir / "batch-01.json").exists())
        log = (self.run_dir / "batch.log").read_text(encoding="utf-8")
        self.assertIn(" BATCH_OPEN batch=1 ", log)
        self.assertIn(" BATCH_END batch=1 ", log)
        # the open job is a trivial script and the close job too: nothing else rides on them
        bodies = [l for l in self.events_raw() if " UMRUN " in l]
        self.assertIn("LEASE_BATCH_OPEN token=", bodies[0])
        self.assertIn("LEASE_BATCH_CLOSE token=", bodies[-1])
        # r5: the open's runner-liveness sidecar was written while the open was owned and removed once its result was in hand; the claim had a keeper
        self.assertEqual((b["openLiveness"]["end"], b["openLiveness"]["writeFailures"]), ("REMOVED", 0), b["openLiveness"])
        self.assertEqual(list((self.share / "notafter").iterdir()), [], "the open's sidecar was left behind")
        self.assertIsNone(b["claimRenew"]["lost"], b["claimRenew"])
        self.assertIn(f" BATCH_OPEN_LIVENESS batch=1 job=lease-batch-open-", log)

    # ---------------------------------------------------------------- in-lease retry
    def test_a_busy_probe_is_retried_inside_the_same_lease(self) -> None:
        r = self.run_batch([self.specs[0]], FAKE_QUIET_MODES="COOLDOWN,BUSY,FOREIGN,QUIET")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        k = self.kinds()
        self.assertEqual([x.split(":")[0] for x in k], ["OPEN", "QUIET", "QUIET", "QUIET", "QUIET", "LEG", "CLOSE"], k)
        [b] = self.batches()
        # r5 (V-01): the FOREIGN probe among this leg's attempts sticks, so the leg ran but is not counted (Invoke-VenueChain's rule).
        self.assertEqual((b["probeRetries"], b["retries"], b["legsRan"], b["legsCounted"], b["endReason"]), (3, 3, 1, 0, "ALL_LEGS_DONE"))
        self.assertEqual((b["legs"][0]["class"], b["legs"][0]["foreignFloor"]), ("COOLDOWN_UNMET", True), b["legs"])
        log = (self.run_dir / "batch.log").read_text(encoding="utf-8")
        for reason in ("COOLDOWN_UNMET", "GATE_BUSY"):
            self.assertIn(f"RETRY_IN_LEASE leg=01-fx-a-spec batch=1 reason={reason}", log)

    def test_a_foreign_floor_then_a_quiet_retry_leaves_the_leg_uncounted_and_never_the_next_leg(self) -> None:
        # r5 (V-01, key3): a FOREIGN_FLOOR on the first in-lease probe, then QUIET on the retry. The leg runs on the lease, but the floor seen in an earlier
        # attempt of the SAME leg sticks: class COOLDOWN_UNMET, uncounted. The next leg's own QUIET probe is clean, so it counts. RED: the leg counted.
        r = self.run_batch(self.specs[:2], FAKE_QUIET_MODES="FOREIGN,QUIET,QUIET")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        k = self.kinds()
        token = k[0].split(":")[1]
        self.assertEqual(k, [f"OPEN:{token}", "QUIET:FOREIGN", "QUIET:QUIET", f"LEG:fx-a:{token}", "QUIET:QUIET", f"LEG:fx-b:{token}", f"CLOSE:{token}"])
        [b] = self.batches()
        a, nxt = b["legs"]
        self.assertEqual((a["status"], a["class"], a["counted"], a["quietVerdict"]), ("RAN", "COOLDOWN_UNMET", False, "QUIET"), a)
        self.assertEqual((b["probeRetries"], b["legsRan"], b["legsCounted"]), (1, 2, 1))
        self.assertEqual((nxt["class"], nxt["counted"]), ("QUIET_PRELOAD_OK", True), nxt)
        self.assertEqual((a["foreignFloor"], nxt["foreignFloor"]), (True, False), b["legs"])
        self.assertTrue(a["foreignFloorLine"].startswith("FOREIGN_FLOOR owners="), a)
        res = batch_result(r.stdout)
        self.assertEqual((res["ran"], res["counted"]), (2, 1))
        log = (self.run_dir / "batch.log").read_text(encoding="utf-8")
        self.assertIn("LEG 01-fx-a-spec RAN batch=1 ", log)
        self.assertIn("class=COOLDOWN_UNMET counted=False", log)

    def test_a_leg_whose_own_recheck_failed_is_retried_inside_the_same_lease(self) -> None:
        r = self.run_batch([self.specs[0]], FAKE_LEG_OUTCOMES="VENUE_NOT_QUIESCENT,PASS")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        k = self.kinds()
        token = k[0].split(":")[1]
        self.assertEqual(k, [f"OPEN:{token}", "QUIET:QUIET", f"LEG:fx-a:{token}", "QUIET:QUIET", f"LEG:fx-a:{token}", f"CLOSE:{token}"])
        [b] = self.batches()
        self.assertEqual((b["legRetries"], b["retries"], b["legsAttempted"], b["legsRan"], b["legsCounted"]), (1, 1, 2, 1, 1))
        self.assertEqual([l["status"] for l in b["legs"]], ["RETRY", "RAN"])
        res = batch_result(r.stdout)
        self.assertEqual([l["status"] for l in res["legs"]], ["RAN"])

    # ---------------------------------------------------------------- batch ends
    def test_a_lease_too_short_for_the_next_leg_ends_the_batch_and_the_rest_go_to_the_next(self) -> None:
        # lease 1 has room for one 60 s leg (70 s), then the leg's own result says 50 s are left: the second leg goes to batch 2.
        r = self.run_batch(self.specs[:2], FAKE_LEASE_REMAIN="70,2700", FAKE_LEG_REMAIN="50,2600")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        k = self.kinds()
        t1, t2 = [x.split(":")[1] for x in k if x.startswith("OPEN:")]
        self.assertNotEqual(t1, t2)
        self.assertEqual(k, [f"OPEN:{t1}", "QUIET:QUIET", f"LEG:fx-a:{t1}", f"CLOSE:{t1}", f"OPEN:{t2}", "QUIET:QUIET", f"LEG:fx-b:{t2}", f"CLOSE:{t2}"])
        b1, b2 = self.batches()
        self.assertEqual((b1["endReason"], b1["legsRan"], b2["endReason"], b2["legsRan"]), ("LEASE_REMAINING_SHORT", 1, "ALL_LEGS_DONE", 1))
        self.assertIn("goes to the next batch", b1["endDetail"])
        self.assertEqual((b1["leaseId"], b2["leaseId"]), ("lease-1", "lease-2"))

    def test_a_leg_that_did_not_run_on_the_batch_lease_is_uncounted_and_ends_the_batch(self) -> None:
        r = self.run_batch(self.specs[:2], FAKE_LEG_LOSE="1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        b1, b2 = self.batches()
        self.assertEqual((b1["endReason"], b1["legsRan"], b1["legsCounted"]), ("LEASE_LOST", 1, 0))
        self.assertEqual((b1["legs"][0]["class"], b1["legs"][0]["counted"], b1["legs"][0]["jobLease"]), ("LEASE_LOST", False, "lease-somebody-else"))
        self.assertEqual((b2["legsRan"], b2["legsCounted"]), (1, 1))
        res = batch_result(r.stdout)
        self.assertEqual((res["ran"], res["counted"]), (2, 1))

    def test_r12_adversary_a2_a_job_lease_that_differs_only_by_an_ignorable_character_is_another_lease(self) -> None:
        # adversary r11 A2 on e5ff0114 (sibling Invoke-VenueLeaseBatch.ps1:2013): jobLease -ceq $b.leaseId is culture-aware, so lease-1 + U+00AD read as the batch's own lease
        # and a leg that ran on ANOTHER lease was counted. Ordinal equality: it is LEASE_LOST and uncounted, exactly like a different lease id.
        r = self.run_batch(self.specs[:2], FAKE_LEG_LEASE_SHY="1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        b1 = self.batches()[0]
        self.assertEqual((b1["endReason"], b1["legsRan"], b1["legsCounted"], b1["legs"][0]["class"], b1["legs"][0]["onLease"]), ("LEASE_LOST", 1, 0, "LEASE_LOST", False), b1)

    def test_r12_adversary_a2_an_open_whose_token_differs_only_by_an_ignorable_character_is_not_this_batch(self) -> None:
        # sibling Invoke-VenueLeaseBatch.ps1:1762: [string]$adm.leaseBatch.token -cne $token is culture-aware, so token + U+00AD was accepted as this batch's token
        r = self.run_batch(self.specs[:2], FAKE_OPEN_TOKEN_SHY="1")
        b = self.batches()[0]
        self.assertEqual(b["endReason"], "AGENT_NOT_BATCH_CAPABLE", (r.stdout, r.stderr, b))

    def test_r12_adversary_a2_the_agents_batch_receipt_is_found_by_the_exact_lease_id_only(self) -> None:
        # sibling VenueChain.psm1:1087: [string]$rec.leaseId -ceq $LeaseId was culture-aware, and an empty or whitespace lease id matched a record with no lease id
        (self.share / "logs").mkdir()
        lines = [{"leaseId": "L1" + chr(0xAD), "endReason": "other"}, {"endReason": "noid"}, {"leaseId": "L1", "endReason": "closed"}]
        (self.share / "logs" / "lease-batch-0123456789ab.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines), encoding="utf-8")
        script = (
            f"Import-Module '{psq(MODULE)}' -Force -DisableNameChecking\n"
            f"$s = '{psq(self.share)}'; $shy = [string][char]0x00AD\n"
            "$rd = { param($id) $x = Read-VenueLeaseBatchReceipt -AgentShare $s -Token '0123456789ab' -LeaseId $id; \"$($x.state):$($x.record.endReason)\" }\n"
            "[ordered]@{ exact = (& $rd 'L1'); shyMid = (& $rd ('L' + $shy + '1')); lead = (& $rd ' L1'); empty = (& $rd ''); blank = (& $rd ' ') } | ConvertTo-Json -Compress\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(json.loads(out.stdout), {"exact": "FOUND:closed", "shyMid": "ABSENT:", "lead": "ABSENT:", "empty": "ABSENT:", "blank": "ABSENT:"})

    def test_r13_adversary_a1_a_job_lease_id_that_reads_as_the_batchs_instant_but_is_spelled_otherwise_is_another_lease(self) -> None:
        # adversary r12 A1 on 08c9aef5 (the sibling of VenueThermalGate.psm1:249 in the runner): the agent's results were read through ConvertFrom-Json, which turns a timestamp-shaped
        # leaseId into a DateTime, so the batch's lease '...:00Z' and a leg job's '...:00.0Z' both became the same culture-formatted string and the leg was counted on a lease it was not on.
        batch_lease = "2026-10-30T10:00:00Z"
        r = self.run_batch(self.specs[:2], FAKE_OPEN_LEASE_ID=batch_lease, FAKE_LEG_LEASE_ID="2026-10-30T10:00:00.0Z")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        b1 = self.batches()[0]
        self.assertEqual((b1["leaseId"], b1["endReason"], b1["legsRan"], b1["legsCounted"], b1["legs"][0]["class"], b1["legs"][0]["onLease"]), (batch_lease, "LEASE_LOST", 1, 0, "LEASE_LOST", False), b1)
        self.assertEqual(b1["legs"][0]["jobLease"], "2026-10-30T10:00:00.0Z", b1)

    def test_r13_adversary_a1_a_timestamp_shaped_lease_id_is_recorded_and_matched_as_written(self) -> None:
        # control for the test above: the same raw timestamp-shaped lease id is the batch's own lease, and the receipt keeps the id as the agent wrote it
        batch_lease = "2026-10-30T10:00:00Z"
        r = self.run_batch(self.specs[:2], FAKE_OPEN_LEASE_ID=batch_lease, FAKE_LEG_LEASE_ID=batch_lease)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        b1 = self.batches()[0]
        self.assertEqual((b1["leaseId"], b1["legsCounted"], b1["legs"][0]["onLease"], b1["legs"][0]["jobLease"]), (batch_lease, 2, True, batch_lease), b1)

    def test_r14_adversary_a1_the_open_read_by_the_real_um_run_and_a_leg_naming_the_same_timestamp_lease_id_count_on_lease(self) -> None:
        # adversary r13 A1 on 778fe1aa: um-run.ps1 still ConvertFrom-Json'd the open's result, so the batch held the lease as DateTime text ('10/30/2026 10:00:00') while the leg result's
        # raw reader returned '2026-10-30T10:00:00Z': onLease false, LEASE_LOST, valid work uncounted. Both now carry the raw string.
        batch_lease = "2026-10-30T10:00:00Z"
        real = str(RUNNER.parent.parent / "um-run.ps1")
        r = self.run_batch(self.specs[:2], FAKE_OPEN_LEASE_ID=batch_lease, FAKE_LEG_LEASE_ID=batch_lease, FAKE_OPEN_VIA_REAL_UMRUN="1", FAKE_REAL_UMRUN=real)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        b1 = self.batches()[0]
        self.assertEqual((b1["leaseId"], b1["legsCounted"], b1["legs"][0]["onLease"], b1["legs"][0]["jobLease"]), (batch_lease, 2, True, batch_lease), b1)

    def test_r14_adversary_a1_control_through_the_real_um_run_a_leg_on_another_spelling_of_the_same_instant_is_another_lease(self) -> None:
        batch_lease = "2026-10-30T10:00:00Z"
        real = str(RUNNER.parent.parent / "um-run.ps1")
        r = self.run_batch(self.specs[:2], FAKE_OPEN_LEASE_ID=batch_lease, FAKE_LEG_LEASE_ID="2026-10-30T10:00:00.0Z", FAKE_OPEN_VIA_REAL_UMRUN="1", FAKE_REAL_UMRUN=real)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        b1 = self.batches()[0]
        self.assertEqual((b1["leaseId"], b1["endReason"], b1["legsCounted"], b1["legs"][0]["onLease"]), (batch_lease, "LEASE_LOST", 0, False), b1)

    def test_r13_adversary_a1_the_agents_batch_receipt_and_the_lease_ledger_keep_the_lease_id_as_written(self) -> None:
        # siblings VenueChain.psm1 Read-VenueLeaseBatchReceipt / Read-VenueLeaseLedger: a timestamp-shaped lease id was a DateTime, so '...:00.0Z' was FOUND for '...:00Z' and the ledger row's id was reformatted
        (self.share / "logs").mkdir()
        lines = [{"leaseId": "2026-10-30T10:00:00.0Z", "endReason": "other"}, {"leaseId": "2026-10-30T10:00:00Z", "endReason": "closed"}]
        (self.share / "logs" / "lease-batch-0123456789ab.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
        claim_dir = self.share / "claims"
        script = (
            f"Import-Module '{psq(MODULE)}' -Force -DisableNameChecking\n"
            f"$s = '{psq(self.share)}'; $cd = '{psq(claim_dir)}'\n"
            "$rd = { param($id) $x = Read-VenueLeaseBatchReceipt -AgentShare $s -Token '0123456789ab' -LeaseId $id; \"$($x.state):$($x.record.endReason)\" }\n"
            "$p = Get-VenueLeaseLedgerPath -Venue 'v' -ClaimDir $cd; New-Item -ItemType Directory -Force -Path (Split-Path -Parent $p) | Out-Null\n"
            "[IO.File]::WriteAllText($p, '{\"startUtc\":\"2026-10-30T10:00:00Z\",\"endUtc\":\"2026-10-30T10:45:00Z\",\"leaseId\":\"2026-10-30T10:00:00.0Z\"}' + \"`n\")\n"
            "$led = @(Read-VenueLeaseLedger -Venue 'v' -ClaimDir $cd -SinceUtc '2000-01-01T00:00:00Z')\n"
            "[ordered]@{ exact = (& $rd '2026-10-30T10:00:00Z'); dotZero = (& $rd '2026-10-30T10:00:00.0Z'); offset = (& $rd '2026-10-30T10:00:00+00:00'); ledger = [string]$led[0].leaseId } | ConvertTo-Json -Compress\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(json.loads(out.stdout), {"exact": "FOUND:closed", "dotZero": "FOUND:other", "offset": "ABSENT:", "ledger": "2026-10-30T10:00:00.0Z"})

    def test_an_agent_without_lease_batches_stops_before_any_probe_or_leg(self) -> None:
        r = self.run_batch(self.specs[:2], FAKE_OPEN_MODES="nobatch")
        self.assertEqual(r.returncode, 10, r.stdout + r.stderr)
        k = self.kinds()
        self.assertEqual([x.split(":")[0] for x in k], ["OPEN", "CLOSE"], k)
        [b] = self.batches()
        self.assertEqual(b["endReason"], "AGENT_NOT_BATCH_CAPABLE")
        self.assertFalse(self.claim_path.exists())
        res = batch_result(r.stdout)
        self.assertEqual([l["status"] for l in res["legs"]], ["NOT_RUN", "NOT_RUN"])

    def test_an_open_that_never_got_its_lease_stops_with_exit_9(self) -> None:
        r = self.run_batch([self.specs[0]], FAKE_OPEN_MODES="retracted")
        self.assertEqual(r.returncode, 9, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["endReason"], b["openOutcome"]), ("OPEN_FAILED", "RETRACTED"))
        self.assertNotIn("QUIET:", " ".join(self.kinds()))

    def test_an_unmeasured_probe_stops_the_run_and_the_lease_is_still_closed(self) -> None:
        r = self.run_batch(self.specs[:2], FAKE_QUIET_MODES="UNKNOWN")
        self.assertEqual(r.returncode, 6, r.stdout + r.stderr)
        k = self.kinds()
        self.assertEqual([x.split(":")[0] for x in k], ["OPEN", "QUIET", "CLOSE"], k)
        [b] = self.batches()
        self.assertEqual(b["endReason"], "PROBE_VENUE_QUIET_UNKNOWN")
        self.assertFalse(self.claim_path.exists())

    def test_a_leg_budget_no_fresh_lease_can_hold_stops_instead_of_opening_lease_after_lease(self) -> None:
        r = self.run_batch([self.specs[0]], FAKE_LEASE_REMAIN="30")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["endReason"], b["legsAttempted"]), ("LEG_BUDGET_EXCEEDS_LEASE", 0))
        self.assertEqual(len([x for x in self.kinds() if x.startswith("OPEN:")]), 1)

    def test_a_high_preload_leg_ran_but_is_not_counted(self) -> None:
        r = self.run_batch([self.specs[0]], FAKE_LEG_PRELOADS="45")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["legs"][0]["class"], b["legs"][0]["counted"], b["legsRan"], b["legsCounted"]), ("QUIET_PRELOAD_HIGH", False, 1, 0))

    # ---------------------------------------------------------------- r2: Bachelor's lease caps (CAPS-ADDENDUM 2026-10-23), each a BLOCKER for the keys
    def test_a_renew_gap_over_120_s_demotes_the_lease_and_the_legs_after_it_are_uncounted(self) -> None:
        # leg 1 ends with 3 s between successful renews; by leg 2's end there was a 130 s gap (heavy-slot.py demoted the lease): leg 2 is typed
        # LEASE_DEMOTED, never counted, the batch ends, and leg 3 runs in a fresh batch.
        r = self.run_batch(self.specs, FAKE_LEG_GAPS="3,130,3")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        b1, b2 = self.batches()
        self.assertEqual([(l["class"], l["counted"]) for l in b1["legs"]], [("QUIET_PRELOAD_OK", True), ("LEASE_DEMOTED", False)])
        self.assertEqual((b1["endReason"], b1["legsRan"], b1["legsCounted"], b1["renewOkMaxGapSec"]), ("LEASE_DEMOTED", 2, 1, 130.0))
        self.assertEqual(b1["legs"][1]["renewOkMaxGapSec"], 130.0)
        self.assertEqual((b2["legsRan"], b2["legsCounted"], b2["endReason"]), (1, 1, "ALL_LEGS_DONE"))
        self.assertEqual((batch_result(r.stdout)["ran"], batch_result(r.stdout)["counted"]), (3, 2))
        # no JobId twice in one run (test_attr3_footage_stage.py's same-JobId premise for this um-run caller): two batches, four distinct jobs
        jobs = [l.split(" ", 3)[2] for l in self.events_raw() if " UMRUN " in l]
        self.assertEqual((len(jobs), len(set(jobs))), (4, 4), jobs)

    def test_a_leg_with_no_renew_record_is_never_counted(self) -> None:
        r = self.run_batch([self.specs[0]], FAKE_LEG_GAPS="none")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["legs"][0]["class"], b["legs"][0]["counted"], b["endReason"]), ("LEASE_RENEW_UNKNOWN", False, "LEASE_RENEW_UNKNOWN"))

    def test_exit_75_refusals_are_typed_wait_and_retry_not_a_failure(self) -> None:
        r = self.run_batch([self.specs[0]])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["exit75Refusals"], b["exit75Class"]), (1, "WAIT_RETRY"))
        self.assertEqual(b["legsCounted"], 1)

    def test_an_open_still_waiting_in_the_agent_gate_is_read_by_name_not_failed(self) -> None:
        # um-run stops UNRESOLVED while the agent still holds the open in its gate (exit-75 refusals); the runner waits for the open's result by
        # name instead of stopping with exit 9, and the batch runs.
        r = self.run_batch([self.specs[0]], FAKE_OPEN_MODES="gatewait")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["openOutcome"], b["openLeaseWait"], b["exit75Class"], b["legsCounted"]), ("RECEIPT_BY_NAME", "EXIT_75_WAIT", "WAIT_RETRY", 1))
        self.assertEqual(len([x for x in self.kinds() if x.startswith("OPEN:")]), 1, "the open is never resubmitted")

    def test_the_cooldown_is_waited_out_before_the_next_open_and_without_the_claim(self) -> None:
        r = self.run_batch(self.specs[:2], "-LeaseCooldownSec", "4", FAKE_LEASE_REMAIN="70,2700", FAKE_LEG_REMAIN="50,2600")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        b1, b2 = self.batches()
        self.assertEqual(b1["preOpenWait"]["reason"], "NONE")
        self.assertEqual(b2["preOpenWait"]["reason"], "COOLDOWN")
        self.assertGreaterEqual(b2["preOpenWait"]["waitSec"], 1)
        ticks = {}
        for line in self.events_raw():
            t, kind, rest = line.split(" ", 2)
            if kind == "UMRUN":
                ticks.setdefault(rest.split()[0].split("-")[2], []).append(int(t))
        close1, open2 = ticks["close"][0], ticks["open"][1]
        self.assertGreaterEqual((open2 - close1) / 1e7, 3.5, "the second open came inside the cooldown")
        log = (self.run_dir / "batch.log").read_text(encoding="utf-8")
        self.assertLess(log.index(" BATCH_WAIT batch=2 reason=COOLDOWN"), log.index(" CLAIM_TAKEN batch=2 "), "the claim was held through the cooldown")
        rows = [json.loads(l) for l in (self.claims / "bachelor.lease-ledger.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertEqual([x["leaseId"] for x in rows], ["lease-1", "lease-2"], "every lease this runner took is in the venue's ledger")

    def test_the_3_hour_budget_from_an_earlier_run_is_waited_out_or_typed_at_the_deadline(self) -> None:
        # an earlier run's two 45-min leases (ledger) leave no room for a third in this 3 h window: the runner never opens, and says why.
        self.claims.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        rows = [{"venue": "bachelor", "leaseId": "old-1", "startUtc": utc_iso(now - timedelta(minutes=105)), "endUtc": utc_iso(now - timedelta(minutes=60))},
                {"venue": "bachelor", "leaseId": "old-2", "startUtc": utc_iso(now - timedelta(minutes=45)), "endUtc": utc_iso(now - timedelta(minutes=1))}]
        (self.claims / "bachelor.lease-ledger.jsonl").write_text("".join(json.dumps(x) + "\n" for x in rows), encoding="utf-8")
        r = self.run_batch([self.specs[0]])
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["endReason"], b["preOpenWait"]["reason"]), ("DEADLINE", "BUDGET"))
        self.assertIn("lease-budget-wait", b["endDetail"])
        # the first lease leaves the window ending at open + 45 min only about 75 min from now; old-2 (44 min) is still in it then
        self.assertGreater(b["preOpenWait"]["waitSec"], 4000)
        self.assertGreater(b["preOpenWait"]["usedSec"], 2500)
        self.assertEqual(self.kinds(), [], "nothing is submitted when the budget has no room before the deadline")

    def test_the_caps_cannot_be_relaxed_against_the_real_um_run(self) -> None:
        base = [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(RUNNER), "-LegSpec", str(self.specs[0]), "-RunDir", str(self.run_dir),
                "-SourceCommit", SHA, "-BuildManifestSha256", MANIFEST, "-ClaimDir", str(self.claims), "-QuietScript", str(self.tmp / "fake-quiet.ps1"),
                "-LegScript", str(self.tmp / "fake-leg.ps1"), "-AgentShare", str(self.share)]
        # (a shortened -LeaseCooldownSec is judged against the LIVE caps after the probe: test_a_caller_may_only_tighten_the_live_caps)
        for extra, needle in ((["-LeaseBudgetWindowSec", "3600"], "LEASE_CAP_RELAXED"),
                              (["-LeaseCapSec", "2701"], "LeaseCapSec"), (["-RenewGapMaxSec", "121"], "RenewGapMaxSec"), (["-LeaseBudgetSec", "10801"], "LeaseBudgetSec")):
            with self.subTest(extra=extra):
                out = subprocess.run(base + extra, capture_output=True, text=True, timeout=120)
                self.assertNotEqual(out.returncode, 0, out.stdout + out.stderr)
                self.assertIn(needle, out.stdout + out.stderr)
        self.assertFalse(self.claim_path.exists())
        self.assertFalse(self.events.exists(), "nothing was submitted")

    def test_the_budget_wait_rule(self) -> None:
        # Get-VenueLeaseBudgetWait: a lease is opened only when the 3 h window ending at its LAST possible second (open + 45 min) holds at most
        # 90 min of leased time, and never within 30 min of the last lease's end.
        now = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
        def m(x: float) -> str:
            return utc_iso(now + timedelta(minutes=x))
        cases = {"none": [], "cooldown": [(m(-40), m(-10))], "budget": [(m(-120), m(-75)), (m(-45), m(0))], "old": [(m(-400), m(-355))]}
        script = f"Import-Module '{psq(MODULE)}' -Force -DisableNameChecking\n$now = '{utc_iso(now)}'\n$o = [ordered]@{{}}\n"
        for name, leases in cases.items():
            arr = ", ".join(f"[pscustomobject]@{{ startUtc = '{s}'; endUtc = '{e}' }}" for s, e in leases)
            script += f"$w = Get-VenueLeaseBudgetWait -Leases @({arr}) -NowUtc $now; $o['{name}'] = [ordered]@{{ reason = $w.reason; waitSec = $w.waitSec; usedSec = $w.usedSec }}\n"
        script += "$o | ConvertTo-Json -Compress -Depth 4\n"
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        got = json.loads(out.stdout)
        self.assertEqual(got["none"], {"reason": "NONE", "waitSec": 0, "usedSec": 0})
        self.assertEqual((got["cooldown"]["reason"], got["cooldown"]["waitSec"]), ("COOLDOWN", 1200))
        # leases at -120..-75 and -45..0 min: the cooldown ends at +30, and the first lease leaves the window ending at open + 45 only at +60
        self.assertEqual((got["budget"]["reason"], got["budget"]["waitSec"], got["budget"]["usedSec"]), ("BUDGET", 3600, 2700))
        self.assertEqual(got["old"], {"reason": "NONE", "waitSec": 0, "usedSec": 0})

    # ---------------------------------------------------------------- BACHELOR-DEDICATED-CAPS-RUNNER-1: the caps are read live, not mirrored
    LIVE_CAPS = {"enabled": True, "max_minutes": 45, "pending_max_minutes": 30, "cooldown_minutes": 5, "max_fraction_per_3h": 0.9, "renew_stale_s": 120}

    def caps_events_raw(self) -> list[str]:
        return self.caps_events.read_text(encoding="utf-8").splitlines() if self.caps_events.exists() else []

    def seed_ledger(self, *spans: tuple[float, float]) -> None:
        """Earlier leases of this venue, as (start, end) minutes from now (negative = past)."""
        self.claims.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        rows = [{"venue": "bachelor", "leaseId": f"old-{i}", "startUtc": utc_iso(now + timedelta(minutes=s)), "endUtc": utc_iso(now + timedelta(minutes=e))}
                for i, (s, e) in enumerate(spans, 1)]
        (self.claims / "bachelor.lease-ledger.jsonl").write_text("".join(json.dumps(x) + "\n" for x in rows), encoding="utf-8")

    def test_the_live_caps_come_from_one_exempt_probe_job_and_are_in_the_receipt(self) -> None:
        r = self.run_batch([self.specs[0]], FAKE_CAPS_MODE="live", FAKE_CAPS_JSON=json.dumps(self.LIVE_CAPS), OMIT_DEFAULTS="-LeaseCooldownSec")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        probes = self.caps_events_raw()
        self.assertEqual(len(probes), 1, probes)
        self.assertRegex(probes[0].split(" ", 3)[2], r"^venue-quiet-probe-caps-\d{8}T\d{6}Z-[0-9a-f]{8,}$")
        self.assertIn("heavy-slot.lease.json", probes[0], "the probe prints Bachelor's lease config")
        self.assertIn(r"C:\Users\layib\.claude\ops\heavy-slot.lease.json", probes[0])
        self.assertIn("notAfter=<unset>", probes[0])
        [b] = self.batches()
        self.assertEqual((b["capsSource"], b["capsCooldownSec"], b["capsBudgetSec"], b["capsCapSec"]), ("live", 300, 9720, 2700), b)
        self.assertEqual(batch_result(r.stdout)["capsSource"], "live")
        log = (self.run_dir / "batch.log").read_text(encoding="utf-8")
        self.assertRegex(log, r" CAPS source=live cooldownSec=300 budgetSec=9720 capSec=2700 ")
        self.assertLess(log.index(" CAPS source="), log.index(" BATCH_OPEN batch=1 "), "the caps are read before the first lease is requested")

    def test_a_live_5_minute_cooldown_is_waited_not_the_old_30(self) -> None:
        # the last lease ended 4 min 50 s ago: Bachelor's live cooldown (5 min) leaves about 10 s, the old mirrored 30 min would pass the 240 s deadline.
        self.seed_ledger((-50.0, -4.0 - 50 / 60))
        r = self.run_batch([self.specs[0]], FAKE_CAPS_MODE="live", FAKE_CAPS_JSON=json.dumps(self.LIVE_CAPS), OMIT_DEFAULTS="-LeaseCooldownSec")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual(b["preOpenWait"]["reason"], "COOLDOWN")
        self.assertTrue(1 <= b["preOpenWait"]["waitSec"] <= 300, b["preOpenWait"])
        self.assertEqual(len([x for x in self.kinds() if x.startswith("OPEN:")]), 1)

    def test_the_live_budget_fraction_paces_the_3_hour_window_not_50_percent(self) -> None:
        # two 45 min leases (90 min) in the window: 50 % (90 min) leaves no room for a third, live 0.9 (162 min) does, so the open is not delayed.
        self.seed_ledger((-130.0, -85.0), (-51.0, -6.0))
        r = self.run_batch([self.specs[0]], FAKE_CAPS_MODE="live", FAKE_CAPS_JSON=json.dumps(self.LIVE_CAPS), OMIT_DEFAULTS="-LeaseCooldownSec")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["preOpenWait"]["reason"], b["preOpenWait"]["waitSec"]), ("NONE", 0))
        self.assertEqual(b["capsBudgetSec"], 9720)
        self.assertEqual(b["preOpenWait"]["budgetRoomSec"], 2700)

    def test_live_caps_that_cannot_be_read_or_do_not_validate_fall_back_to_the_old_constants(self) -> None:
        bad = {
            "probe-failed": ("fail", ""),
            "file-missing": ("live", "HEAVY_SLOT_LEASE_JSON_MISSING"),
            "not-json": ("live", "{ not json"),
            "empty": ("live", ""),
            "not-an-object": ("live", "[1, 2]"),
            "probe-exit-1": ("exit1", json.dumps(self.LIVE_CAPS)),
            "disabled": ("live", json.dumps({**self.LIVE_CAPS, "enabled": False})),
            "enabled-missing": ("live", json.dumps({k: v for k, v in self.LIVE_CAPS.items() if k != "enabled"})),
            "negative-cooldown": ("live", json.dumps({**self.LIVE_CAPS, "cooldown_minutes": -1})),
            "zero-max-minutes": ("live", json.dumps({**self.LIVE_CAPS, "max_minutes": 0})),
            "text-fraction": ("live", json.dumps({**self.LIVE_CAPS, "max_fraction_per_3h": "0.9"})),
            "missing-renew": ("live", json.dumps({k: v for k, v in self.LIVE_CAPS.items() if k != "renew_stale_s"})),
        }
        for name, (mode, text) in bad.items():
            with self.subTest(name):
                self.setUp()
                r = self.run_batch([self.specs[0]], FAKE_CAPS_MODE=mode, FAKE_CAPS_JSON=text, OMIT_DEFAULTS="-LeaseCooldownSec")
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                [b] = self.batches()
                self.assertEqual((b["capsSource"], b["capsCooldownSec"], b["capsBudgetSec"], b["capsCapSec"]), ("fallback", 1800, 5400, 2700), b)
                self.assertTrue(b["capsFallbackReason"], b)
                self.assertEqual(batch_result(r.stdout)["capsSource"], "fallback")
                self.assertRegex((self.run_dir / "batch.log").read_text(encoding="utf-8"), r" CAPS source=fallback cooldownSec=1800 budgetSec=5400 capSec=2700 ")

    def test_the_old_constants_still_pace_a_fallback_run(self) -> None:
        # unreadable caps: a lease that ended 4 min ago still holds the run for the old 30 min cooldown, which passes the deadline.
        self.seed_ledger((-45.0, -4.0))
        r = self.run_batch([self.specs[0]], OMIT_DEFAULTS="-LeaseCooldownSec")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["endReason"], b["preOpenWait"]["reason"], b["capsSource"]), ("DEADLINE", "COOLDOWN", "fallback"))

    def test_live_caps_tighter_than_the_old_ones_bind_too(self) -> None:
        # the owner may also tighten: a 20 min lease cap and a 60 min cooldown read live bind the run the same way.
        caps = {**self.LIVE_CAPS, "max_minutes": 20, "cooldown_minutes": 60, "max_fraction_per_3h": 0.25}
        r = self.run_batch([self.specs[0]], FAKE_CAPS_MODE="live", FAKE_CAPS_JSON=json.dumps(caps), OMIT_DEFAULTS="-LeaseCooldownSec", FAKE_LEASE_REMAIN="1200", FAKE_LEG_REMAIN="1100")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["capsSource"], b["capsCooldownSec"], b["capsBudgetSec"], b["capsCapSec"]), ("live", 3600, 2700, 1200), b)

    def test_a_caller_may_only_tighten_the_live_caps(self) -> None:
        # Resolve-VenueLeaseCaps: against the real um-run an explicit cooldown below the live one, or a budget above it, is LEASE_CAP_RELAXED; tighter is kept;
        # only an offline stub may go looser. Get-VenueLeaseCaps reads the probe's text with heavy-slot.py's rules.
        live = json.dumps(self.LIVE_CAPS)
        script = f"""Import-Module '{psq(MODULE)}' -Force -DisableNameChecking
$live = Get-VenueLeaseCaps -Text '{live}'
$fb = Get-VenueLeaseCaps -Text 'HEAVY_SLOT_LEASE_JSON_MISSING'
$o = [ordered]@{{ live = "$($live.source) $($live.cooldownSec) $($live.budgetSec) $($live.capSec)"; fb = "$($fb.source) $($fb.cooldownSec) $($fb.budgetSec) $($fb.capSec)" }}
function Test-Caps($caps, [int]$cd, [bool]$cdx, [int]$bud, [bool]$budx, [bool]$relax) {{
    $r = Resolve-VenueLeaseCaps -Caps $caps -CooldownSec $cd -CooldownExplicit $cdx -BudgetSec $bud -BudgetExplicit $budx -CapSec 2700 -AllowRelax $relax
    "$($r.ok) $($r.cooldownSec) $($r.budgetSec) $($r.capSec) $($r.error)".Trim()
}}
$o['default'] = Test-Caps $live 1800 $false 5400 $false $false
$o['equal'] = Test-Caps $live 300 $true 9720 $true $false
$o['tighter'] = Test-Caps $live 600 $true 5000 $true $false
$o['cooldown-looser'] = Test-Caps $live 60 $true 5400 $false $false
$o['budget-looser'] = Test-Caps $live 1800 $false 10000 $true $false
$o['stub-looser'] = Test-Caps $live 0 $true 10000 $true $true
$o['fb-cooldown-looser'] = Test-Caps $fb 300 $true 5400 $false $false
$o['fb-budget-looser'] = Test-Caps $fb 1800 $false 9000 $true $false
$o['fb-tighter'] = Test-Caps $fb 2400 $true 3000 $true $false
$o | ConvertTo-Json -Compress
"""
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        got = json.loads(out.stdout)
        self.assertEqual(got["live"], "live 300 9720 2700")
        self.assertEqual(got["fb"], "fallback 1800 5400 2700")
        self.assertEqual(got["default"], "True 300 9720 2700", "not explicit: the live caps apply")
        self.assertEqual(got["equal"], "True 300 9720 2700")
        self.assertEqual(got["tighter"], "True 600 5000 2700")
        self.assertTrue(got["cooldown-looser"].startswith("False") and "LEASE_CAP_RELAXED" in got["cooldown-looser"], got["cooldown-looser"])
        self.assertTrue(got["budget-looser"].startswith("False") and "LEASE_CAP_RELAXED" in got["budget-looser"], got["budget-looser"])
        self.assertEqual(got["stub-looser"], "True 0 10000 2700", "the offline stub keeps the old shortening rule")
        self.assertIn("LEASE_CAP_RELAXED", got["fb-cooldown-looser"])
        self.assertIn("LEASE_CAP_RELAXED", got["fb-budget-looser"])
        self.assertEqual(got["fb-tighter"], "True 2400 3000 2700")

    def test_r2_b1_a_non_default_window_never_loosens_the_budget_of_the_fixed_3_hour_basis(self) -> None:
        # CoS/Dot B1 on affa0d1a: budget = max_fraction_per_3h * 10800 whatever -LeaseBudgetWindowSec says (it used to be fraction * the CALLER's window, so 21600 doubled
        # it). A longer window can only count MORE of the earlier leases against the same budget; a shorter one may only shrink the budget, never raise it.
        caps = json.dumps({**self.LIVE_CAPS, "cooldown_minutes": 0, "max_fraction_per_3h": 0.5})
        script = f"""Import-Module '{psq(MODULE)}' -Force -DisableNameChecking
$now = [DateTime]::UtcNow
$o = [ordered]@{{}}
foreach ($w in 10800, 21600, 86400, 3600, 600) {{
    $c = Get-VenueLeaseCaps -Text '{caps}' -WindowSec $w
    # 5400 s of leases that ended just now (the exact CoS example): a 45 min lease needs 5400 - 2700 or less already used in its window
    $leases = @([pscustomobject]@{{ startUtc = $now.AddSeconds(-5400).ToString('o'); endUtc = $now.ToString('o') }})
    $b = Get-VenueLeaseBudgetWait -Leases $leases -NowUtc $now -CapSec $c.capSec -CooldownSec $c.cooldownSec -WindowSec $w -BudgetSec $c.budgetSec
    $o["w$w"] = [ordered]@{{ source = $c.source; budgetSec = $c.budgetSec; reason = $b.reason; waitSec = $b.waitSec }}
}}
$o | ConvertTo-Json -Compress -Depth 4
"""
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        got = json.loads(out.stdout)
        self.assertEqual(got["w10800"]["budgetSec"], 5400)
        for w in (21600, 86400):
            with self.subTest(window=w):
                self.assertEqual(got[f"w{w}"]["budgetSec"], 5400, "a longer window must not raise the budget")
                self.assertEqual(got[f"w{w}"]["reason"], "BUDGET", "5400 s taken, fraction 0.5: the next lease WAITS")
                self.assertGreater(got[f"w{w}"]["waitSec"], 0)
        for w in (3600, 600):
            with self.subTest(window=w):
                self.assertLessEqual(got[f"w{w}"]["budgetSec"], 5400, "a shorter window may only tighten")
                self.assertEqual(got[f"w{w}"]["budgetSec"], w // 2, "the fraction of the shorter window")
        self.assertEqual(got["w10800"]["reason"], "BUDGET")

    def test_r2_b1b_a_configured_cooldown_is_never_silently_shortened(self) -> None:
        # CoS/Dot B1b on affa0d1a: cooldown_minutes 2880 (2 days) was clipped to 86400 s. The exact value is kept up to what an int of seconds holds; beyond that the
        # config is unrepresentable and is the FALLBACK with a typed reason (never a shorter cooldown than the one configured).
        def caps(minutes: float) -> str:
            return json.dumps({**self.LIVE_CAPS, "cooldown_minutes": minutes})
        script = f"""Import-Module '{psq(MODULE)}' -Force -DisableNameChecking
$o = [ordered]@{{}}
$i = 0
foreach ($t in '{caps(1440)}', '{caps(2880)}', '{caps(10080)}', '{caps(0.5)}', '{caps(1e9)}') {{
    $c = Get-VenueLeaseCaps -Text $t
    $o["c$i"] = "$($c.source) $($c.cooldownSec) $($c.reason)".Trim(); $i++
}}
$o | ConvertTo-Json -Compress
"""
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        got = json.loads(out.stdout)
        self.assertEqual(got["c0"], "live 86400")
        self.assertEqual(got["c1"], "live 172800", "2880 min is 172800 s, not the old 86400 clip")
        self.assertEqual(got["c2"], "live 604800")
        self.assertEqual(got["c3"], "live 30")
        self.assertTrue(got["c4"].startswith("fallback 1800 cooldown_minutes-unrepresentable"), got["c4"])

    def test_r2_i1_two_caps_probe_ids_built_in_the_same_second_differ_and_keep_the_agents_shapes(self) -> None:
        script = f"""Import-Module '{psq(MODULE)}' -Force -DisableNameChecking
$ids = @(1..40 | ForEach-Object {{ New-VenueCapsProbeId }})
[ordered]@{{ ids = $ids; distinct = @($ids | Select-Object -Unique).Count }} | ConvertTo-Json -Compress
"""
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        got = json.loads(out.stdout)
        self.assertEqual(got["distinct"], 40, "40 ids built back to back (the same second) must all differ")
        for i in got["ids"]:
            self.assertRegex(i, r"^venue-quiet-probe-caps-\d{8}T\d{6}Z-[0-9a-f]{8,}$")
            self.assertRegex(i, r"^(venue-quiet-probe|agent-swap)-", "the agent's GateExemptJobPattern")
            self.assertRegex(i, r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", "UmRunDrop's job id rule")

    def test_an_explicit_tighter_cooldown_and_budget_win_over_the_live_caps_in_a_run(self) -> None:
        r = self.run_batch([self.specs[0]], "-LeaseCooldownSec", "900", "-LeaseBudgetSec", "6000", FAKE_CAPS_MODE="live", FAKE_CAPS_JSON=json.dumps(self.LIVE_CAPS))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["capsSource"], b["capsCooldownSec"], b["capsBudgetSec"]), ("live", 900, 6000), b)

    # ---------------------------------------------------------------- r3: sol r2 B1 (no lease after the runner exits) and B2 (no leg past the lease's end)
    def test_r3_an_open_held_in_the_gate_through_the_deadline_is_cancelled_and_reconciled_before_the_claim_is_released(self) -> None:
        # The agent holds the open in its gate (exit-75 refusals) past the deadline, and the close job times out (um-run's queue ceiling: the agent never
        # took it). The runner must CANCEL the open and read the agent's answer by name before it releases the claim, so the pending request can never
        # take an unused lease once the runner is gone. This is also the gate-wait-reaches-deadline path (DEADLINE, exit 3, openLeaseWait EXIT_75_WAIT).
        agent = _FakeAgentGate(self.share, self.claim_path)
        agent.start()
        try:
            r = self.run_batch([self.specs[0]], "-DeadlineUtc", utc_iso(datetime.now(timezone.utc) + timedelta(seconds=12)),
                               FAKE_OPEN_MODES="gatehold", FAKE_CLOSE_MODES="timeout")
        finally:
            agent.runner_exited.set()
            agent.join(10)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertFalse(agent.acquired_after_exit, "the open was never cancelled: its pending request takes an unused lease after the runner exited")
        self.assertIsNotNone(agent.cancel_seen)
        [b] = self.batches()
        job, claim_held = agent.cancel_seen
        self.assertEqual(job, b["openJobId"])
        self.assertTrue(claim_held, "the open was cancelled only after the runner released its claim")
        self.assertEqual((b["endReason"], b["openOutcome"], b["openLeaseWait"], b["exit75Class"]), ("DEADLINE", "UNRESOLVED", "EXIT_75_WAIT", "WAIT_RETRY"))
        self.assertEqual((b["openCancel"]["requested"], b["openCancel"]["state"], b["openCancel"]["resultLabel"]), ("REQUESTED", "CANCELLED", "cancelled-in-gate"))
        self.assertEqual(b["closeOutcome"], "UNRESOLVED")
        self.assertEqual((b["leaseId"], b["ledger"]), (None, None), "a cancelled open took no lease, so the ledger has no row for it")
        self.assertFalse((self.claims / "bachelor.lease-ledger.jsonl").exists())
        log = (self.run_dir / "batch.log").read_text(encoding="utf-8")
        self.assertLess(log.index(" BATCH_OPEN_CANCEL batch=1 "), log.index(" BATCH_OPEN_RECONCILED batch=1 state=CANCELLED "))
        self.assertLess(log.index(" BATCH_OPEN_RECONCILED batch=1 "), log.index(" CLAIM_RELEASED batch=1 "), "the claim was released before the open was reconciled")
        self.assertNotIn("QUIET:", " ".join(self.kinds()))
        self.assertFalse(self.claim_path.exists())

    def test_r3_an_open_the_agent_never_answers_is_typed_unreconciled_and_exits_11(self) -> None:
        # Nothing answers the cancel: the run says so (exit 11, openCancel UNRECONCILED), its cancel file stays for the agent, and the ledger counts a full
        # lease for it. r4 (sol r3 B1): NO LEASE AFTER notAfterUtc. The open carries the batch deadline as notAfterUtc (um-run.ps1 -NotAfterUtc), so an agent
        # that never answers the cancel still never requests, keeps or hands out a lease for it past the deadline (proven against the agent's real gate by
        # the quiet-lease harness case batch-open-notafter). The close job carries no bound.
        deadline = utc_iso(datetime.now(timezone.utc) + timedelta(seconds=12))
        r = self.run_batch([self.specs[0]], "-DeadlineUtc", deadline, "-CancelReconcileSec", "3",
                           FAKE_OPEN_MODES="gatehold")
        self.assertEqual(r.returncode, 11, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["openCancel"]["requested"], b["openCancel"]["state"]), ("REQUESTED", "UNRECONCILED"))
        self.assertTrue((self.share / "cancel" / f"{b['openJobId']}.cancel").exists(), "the cancel request stays for the agent")
        self.assertEqual(b["ledger"]["basis"], "open-unreconciled")
        self.assertEqual(batch_result(r.stdout)["stopReason"], "OPEN_CANCEL_UNRECONCILED")
        bound = {l.split()[2]: l.split()[4] for l in self.events_raw() if " UMRUN " in l}
        want = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
        got = bound.get(b["openJobId"], "notAfter=<missing>").split("=", 1)[1]
        self.assertTrue(got.endswith("Z") and datetime.fromisoformat(got[:26].rstrip("Z") + "+00:00") == want,
                        f"the open's notAfterUtc is the batch deadline as an ISO-8601 UTC 'o' string: {got!r} != {deadline!r}")
        self.assertEqual(b["openNotAfterUtc"], got, "the batch receipt records the open's bound")
        self.assertEqual(bound.get(b["closeJobId"]), "notAfter=<unset>", "the close job carries no notAfterUtc")

    def test_r3_a_leg_still_running_at_lease_end_is_killed_as_a_whole_tree_and_uncounted(self) -> None:
        # sol r2 B2 at test scale: the leg is admitted with 63 s of lease left (>= the 60 s budget + 0 s margin; on Bachelor 360 + 120 = 480 s), then hangs
        # past the lease's end. It is killed by the lease's end with its whole process tree, typed LEASE_EXPIRED (uncounted), its agent job is asked to
        # cancel, and it is never re-run.
        self.addCleanup(self._kill_fake_leg_pids)
        r = self.run_batch([self.specs[0]], FAKE_LEASE_REMAIN="63", FAKE_LEG_MODES="hang")
        self.assertEqual(r.returncode, 7, r.stdout + r.stderr)
        [b] = self.batches()
        [leg] = b["legs"]
        self.assertEqual((leg["status"], leg["class"], leg["outcome"], leg["counted"], b["endReason"]), ("LEASE_EXPIRED", "LEASE_EXPIRED", "INVALID", False, "LEASE_EXPIRED"))
        self.assertLessEqual(leg["legSec"], 66, "the leg ran past the lease's end")
        self.assertEqual((b["legsAttempted"], b["legsRan"], b["legsCounted"]), (1, 0, 0))
        leg_pid, gc_pid, job = (self.share / "fake-leg-pids.txt").read_text(encoding="utf-8").split()
        for pid in (int(leg_pid), int(gc_pid)):
            self.assertTrue(_wait_pid_gone(pid, 5), f"pid {pid} of the leg's process tree outlived the lease")
        self.assertEqual((leg["jobId"], leg["cancel"]), (job, "REQUESTED"))
        self.assertTrue((self.share / "cancel" / f"{job}.cancel").exists(), "the leg's agent job was not asked to cancel")
        res = batch_result(r.stdout)
        self.assertEqual((res["ran"], res["counted"], res["notRun"], res["leaseExpired"]), (0, 0, 0, 1))
        self.assertEqual(len([k for k in self.kinds() if k.startswith("LEG:")]), 1, "a leg killed at the lease's end is never re-run")

    def test_r3_a_leg_the_agent_stopped_at_its_lease_bound_is_typed_and_uncounted(self) -> None:
        # The agent's own bound (lease-batch r3): a batch job still running at its lease's end, or on a demoted lease, is stopped and says why.
        r = self.run_batch(self.specs, FAKE_LEG_STOPS="lease-end,demoted,none")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        b1, b2, b3 = self.batches()
        self.assertEqual([(b["legs"][0]["class"], b["legs"][0]["counted"], b["endReason"]) for b in (b1, b2)], [("LEASE_EXPIRED", False, "LEASE_EXPIRED"), ("LEASE_DEMOTED", False, "LEASE_DEMOTED")])
        self.assertEqual((b3["legsCounted"], b3["endReason"]), (1, "ALL_LEGS_DONE"))

    def test_r3_a_leg_process_that_exits_non_zero_is_leg_failed_never_counted_and_the_run_exits_7(self) -> None:
        # fable r2 hardening: the LEG_FAILED path at run time, not only by the static scans.
        r = self.run_batch(self.specs[:2], FAKE_LEG_MODES="exit3,ok")
        self.assertEqual(r.returncode, 7, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual([l["status"] for l in b["legs"]], ["LEG_FAILED", "RAN"])
        self.assertEqual((b["legs"][0]["outcome"], b["legs"][0]["counted"], b["legs"][0]["legExit"]), ("INVALID", False, 3))
        self.assertEqual((b["legsRan"], b["legsCounted"]), (1, 1))
        res = batch_result(r.stdout)
        self.assertEqual((res["failed"], res["ran"], res["counted"]), (1, 1, 1))

    def test_r3_a_busy_probe_with_less_room_than_one_recheck_ends_the_batch_instead_of_spinning(self) -> None:
        # fable r2 note: once the room left after the leg's budget is under -RecheckSec, a busy probe ends the batch (the leg goes to the next one)
        # instead of re-probing back to back until the room runs out.
        r = self.run_batch([self.specs[0]], "-RecheckSec", "30", FAKE_QUIET_MODES="COOLDOWN,QUIET", FAKE_LEASE_REMAIN="70,2700")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        b1, b2 = self.batches()
        self.assertEqual((b1["endReason"], b1["probeRetries"], b1["legsAttempted"]), ("LEASE_REMAINING_SHORT", 1, 0))
        self.assertIn("recheckSec=30", b1["endDetail"])
        self.assertEqual((b2["legsCounted"], b2["endReason"]), (1, "ALL_LEGS_DONE"))
        self.assertEqual(len([k for k in self.kinds() if k.startswith("QUIET:")]), 2)

    def test_r3_a_legs_own_exit_75_refusals_are_counted_into_the_batch_receipt(self) -> None:
        # fable r2 hardening: a leg that took a fresh lease under the batch token carries its exit-75 refusals; the batch receipt counts them too.
        r = self.run_batch([self.specs[0]], FAKE_LEG_REFUSALS="2")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["openExit75Refusals"], b["legExit75Refusals"], b["exit75Refusals"], b["exit75Class"]), (1, 2, 3, "WAIT_RETRY"))
        self.assertEqual(b["legs"][0]["exit75Refusals"], 2)

    def _kill_fake_leg_pids(self) -> None:
        p = self.share / "fake-leg-pids.txt"
        if p.exists():
            for tok in p.read_text(encoding="utf-8").split()[:2]:
                try:
                    os.kill(int(tok), signal.SIGTERM)
                except (OSError, ValueError):
                    pass

    # ---------------------------------------------------------------- r5: the open's runner-liveness sidecar (sol B1 on an early runner exit)
    def _agent_verdict(self, notafter_dir: Path, job_id: str, not_after_utc: str) -> dict:
        """The agent's OWN reader and verdict (Read-JobNotAfterSidecar, Get-JobNotAfterVerdict, lifted from ultra-magnus-agent.ps1) on a sidecar folder."""
        script = (
            f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{psq(AGENT)}', [ref]$null, [ref]$null)\n"
            "foreach ($n in 'Get-JobNotAfterVerdict', 'Read-JobNotAfterSidecar') { . ([scriptblock]::Create($ast.Find({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq $n }, $true).Extent.Text)) }\n"
            f"$notAfterDir = '{psq(notafter_dir)}'\n"
            f"$v = Get-JobNotAfterVerdict -Metadata ([pscustomobject]@{{ notAfterUtc = '{not_after_utc}' }}) -Sidecar (Read-JobNotAfterSidecar -JobId '{job_id}')\n"
            "[ordered]@{ state = $v.state; source = $v.source; livenessState = $v.livenessState; livenessSeq = $v.livenessSeq; why = $v.why } | ConvertTo-Json -Compress\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        return json.loads(out.stdout)

    def test_a_runner_killed_while_its_open_waits_stops_renewing_the_sidecar_and_the_agent_reads_it_lapsed(self) -> None:
        # r5 (sol B1; #644 + #663 + this): um-run blocks on the open (the agent re-asking a refused request). The runner keeps the open's sidecar 6 s ahead,
        # rewritten every 1 s, in the agent's format. Killed (process tree, as a lane timeout or a closed console does), it renews no more: the last
        # aliveUntilUtc passes and the agent's own verdict for the open turns passed (source=sidecar), so its wait ends and its request is released (the
        # agent side of this is batch-runner-killed-liveness in test-ultra-magnus-agent-quiet-lease.ps1). RED: the runner writes no sidecar.
        args, e = self.batch_cmd([self.specs[0]], "-LivenessWindowSec", "6", "-LivenessRenewSec", "1", FAKE_OPEN_SLEEP="200")
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=e, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        side_dir = self.share / "notafter"
        try:
            def read_side() -> dict | None:
                files = [f for f in side_dir.glob("lease-batch-open-*.json")]
                if len(files) != 1:
                    return None
                try:
                    return json.loads(files[0].read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    return None
            end = time.monotonic() + 60
            s = None
            while time.monotonic() < end:
                s = read_side()
                if s is not None and int(s["seq"]) >= 3:
                    break
                time.sleep(0.2)
            self.assertTrue(s is not None and int(s["seq"]) >= 3, f"the runner keeps the open's sidecar renewed while um-run blocks: {s} {list(side_dir.iterdir())}")
            job = s["jobId"]
            self.assertEqual((s["schema"], s["writerPid"]), ("mlv-app/job-notafter-sidecar/v1", p.pid), s)
            self.assertTrue(job.startswith("lease-batch-open-") and (side_dir / f"{job}.json").exists(), job)
            far = utc_iso(datetime.now(timezone.utc) + timedelta(hours=1))
            # while the runner lives, the agent reads its open as live
            live = self._agent_verdict(side_dir, job, far)
            self.assertEqual((live["state"], live["livenessState"], live["source"]), ("open", "live", "meta"), live)
            self.assertIsNone(p.poll(), "the runner is still waiting on its open")
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, text=True, timeout=60)
            p.wait(timeout=60)
            killed_at = datetime.now(timezone.utc)
            time.sleep(1.5)   # a write in flight at the kill lands, or not
            s1 = read_side()
            until = parse_utc(s1["aliveUntilUtc"])
            self.assertLessEqual(until, killed_at + timedelta(seconds=7), f"the last aliveUntilUtc is within the window of the kill: {s1}")
            while datetime.now(timezone.utc) < until + timedelta(seconds=1):
                time.sleep(0.2)
            s2 = read_side()
            self.assertEqual(s2["seq"], s1["seq"], "nothing renews the sidecar after the runner is killed")
            dead = self._agent_verdict(side_dir, job, far)
            self.assertEqual((dead["state"], dead["livenessState"], dead["source"]), ("passed", "lapsed", "sidecar"), dead)
            self.assertFalse(_pid_alive(p.pid), "the killed runner is gone")
        finally:
            if p.poll() is None:
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, text=True, timeout=60)
                p.wait(timeout=60)
            p.stdout.close()
            p.stderr.close()

    # ---------------------------------------------------------------- CI-FLAKE-LEASE-BATCH-CLAIM-READ-RACE-1 (merge_group 40211588250)
    def _read_claim_while_writer_holds(self, *, reader_timeout_s: float, pause_after_start_s: float = 0.0, pause_before_direct_s: float = 0.0,
                                       reader=None, foreign_sleeper: bool = False) -> dict:
        """The race scenario with the parent's scheduling made injectable: the writer holds the claim, the direct read raises, and read_claim_json waits it out.

        The test's outcome must not depend on the parent being scheduled before any deadline, so the parent never releases the writer: the reader's own first
        retry sleep does, synchronously, on the reader's thread. The hold has no timer, so a pause before the direct read cannot end it, and a pause after the
        reader starts cannot make the release late against the reader's budget. pause_before_direct_s / pause_after_start_s are real parent pauses (the stress
        test passes values longer than reader_timeout_s to stand in for a descheduled runner). reader is the function under test (default read_claim_json);
        foreign_sleeper runs a time.sleep on another thread while the spy is installed and before the reader's first read, which must not count as the reader's
        retry. Returns the reader's {"body"|"error"} plus "retried", "held_at_retry", "reader_ident", "sleeps" ((thread ident, hold still held) per sleep the
        spy acted on) and "reads" (the reader thread's read_text outcomes in order: an exception's class name, or "ok")."""
        real_sleep = time.sleep
        real_read_text = Path.read_text
        reader_fn = read_claim_json if reader is None else reader
        hold = ExclusiveHold(self.claim_path)
        out: dict = {"held_at_retry": None, "retried": False, "reader_ident": None, "sleeps": [], "reads": []}
        reader_thread = None
        try:
            real_sleep(pause_before_direct_s)
            with self.assertRaises(PermissionError):
                self.claim_path.read_text(encoding="utf-8")

            def spy_sleep(s: float) -> None:
                # Runs on the reader's thread at its first retry: the file is still held here, and the release is this thread's own, so no parent step sits between
                # the reader's PermissionError and the file becoming readable.
                # time.sleep is patched process-wide, so only the reader's own thread may touch the scenario state: any other caller (a foreign thread, a timer)
                # goes straight to the real sleep (sol r2 on fefea29b).
                if threading.get_ident() != out["reader_ident"]:
                    real_sleep(s)
                    return
                out["sleeps"].append((threading.get_ident(), not hold.closed))
                if not out["retried"]:
                    out["held_at_retry"] = not hold.closed
                    out["retried"] = True
                    hold.release()
                real_sleep(s)

            def spy_read_text(path_self, *a, **k):
                if threading.get_ident() != out["reader_ident"]:
                    return real_read_text(path_self, *a, **k)
                try:
                    text = real_read_text(path_self, *a, **k)
                except BaseException as e:
                    out["reads"].append(type(e).__name__)
                    raise
                out["reads"].append("ok")
                return text

            def read() -> None:
                out["reader_ident"] = threading.get_ident()
                try:
                    out["body"] = reader_fn(self.claim_path, timeout_s=reader_timeout_s)
                except BaseException as e:   # surfaced on the test thread below
                    out["error"] = e

            reader_thread = threading.Thread(target=read, daemon=True)
            with mock.patch.object(time, "sleep", spy_sleep), mock.patch.object(Path, "read_text", spy_read_text):
                if foreign_sleeper:
                    foreign = threading.Thread(target=lambda: time.sleep(0.001), daemon=True)
                    foreign.start()
                    foreign.join(30)
                reader_thread.start()
                real_sleep(pause_after_start_s)
                reader_thread.join(reader_timeout_s + 30)
        finally:
            hold.release()   # a test that died above must not leave the claim held
            if reader_thread is not None:
                reader_thread.join(5)
        self.assertFalse(reader_thread.is_alive(), "the reader returned")
        self.assertTrue(hold.closed, "the writer's hold ended")
        return out

    def _assert_reader_waited_out(self, out: dict, body: dict) -> None:
        """The scenario's verdict: the reader itself met the held file (a PermissionError read, then a sleep on its own thread with the hold still up) and then
        returned the claim. A sleep on any other thread proves nothing about the reader (sol r2 on fefea29b)."""
        mine = [held for ident, held in out["sleeps"] if ident == out["reader_ident"]]
        self.assertTrue(out["retried"] and out["held_at_retry"], f"the reader hit PermissionError and went to sleep while the writer still held the file: {out}")
        self.assertTrue(mine and mine[0], f"the reader's own first sleep came while the writer held the file: {out}")
        self.assertEqual(out["reads"][:1], ["PermissionError"], f"the reader's first read was a locked read: {out}")
        self.assertEqual(out["reads"][-1:], ["ok"], f"the reader's last read succeeded: {out}")
        self.assertEqual({k: out[k] for k in ("body", "error") if k in out}, {"body": body}, f"returns the full JSON once the writer releases: {out}")

    def test_the_claim_reader_waits_out_a_writer_that_holds_the_file(self) -> None:
        # The race the merge_group flake hit: a reader that lands while the runner holds the claim (FileShare.None on the take or a renewal) got PermissionError;
        # one that lands on a half-written or not yet created file gets JSONDecodeError or FileNotFoundError. The direct read raises on each; read_claim_json waits.
        self.claims.mkdir()
        body = {"schema": "mlv-app/venue-claim/v1", "venue": "bachelor", "nonce": "n" * 32}
        full = json.dumps(body)
        self.claim_path.write_text(full, encoding="utf-8")

        self._assert_reader_waited_out(self._read_claim_while_writer_holds(reader_timeout_s=120.0), body)

        self.claim_path.write_text('{"schema": "mlv-app/ven', encoding="utf-8")   # a half-written claim
        with self.assertRaises(json.JSONDecodeError):
            json.loads(self.claim_path.read_text(encoding="utf-8"))
        threading.Timer(1.0, lambda: self.claim_path.write_text(full, encoding="utf-8")).start()
        self.assertEqual(read_claim_json(self.claim_path), body)

        self.claim_path.unlink()   # not created yet
        with self.assertRaises(FileNotFoundError):
            self.claim_path.read_text(encoding="utf-8")
        threading.Timer(1.0, lambda: self.claim_path.write_text(full, encoding="utf-8")).start()
        self.assertEqual(read_claim_json(self.claim_path), body)

        self.claim_path.write_text("not json at all", encoding="utf-8")   # a claim that never becomes valid: bounded, then the last error with the path
        with self.assertRaises(json.JSONDecodeError) as cm:
            read_claim_json(self.claim_path, timeout_s=0.5, sleep_s=0.1)
        self.assertIn(str(self.claim_path), str(cm.exception))

    def test_the_claim_reader_scenario_survives_a_descheduled_parent(self) -> None:
        # sol r1 on effee436: a parent paused longer than the reader's budget after the reader's first retry failed a valid test (the parent owned the release,
        # the reader's own deadline kept running), and a pause longer than the hold's 60 s wait before the direct read let the hold lapse. Scaled down: a 2 s
        # reader budget and 3 s parent pauses at both points. The outcome must be the same as with no pause.
        self.claims.mkdir()
        body = {"schema": "mlv-app/venue-claim/v1", "venue": "bachelor", "nonce": "n" * 32}
        self.claim_path.write_text(json.dumps(body), encoding="utf-8")
        self._assert_reader_waited_out(self._read_claim_while_writer_holds(reader_timeout_s=2.0, pause_after_start_s=3.0, pause_before_direct_s=3.0), body)

    def test_the_claim_reader_scenario_ignores_a_foreign_sleeper(self) -> None:
        # sol r2 on fefea29b: the sleep spy was process-wide, so another thread's time.sleep set retried/held_at_retry and released the hold before the reader's
        # first read; the reader then never met a locked file and the scenario still passed. A foreign sleep, before the reader starts, must change nothing.
        self.claims.mkdir()
        body = {"schema": "mlv-app/venue-claim/v1", "venue": "bachelor", "nonce": "n" * 32}
        self.claim_path.write_text(json.dumps(body), encoding="utf-8")
        self._assert_reader_waited_out(self._read_claim_while_writer_holds(reader_timeout_s=30.0, foreign_sleeper=True), body)

    def test_the_claim_reader_scenario_rejects_a_reader_that_does_not_retry(self) -> None:
        # The mutation the scenario exists to catch: a reader that reads once. It must fail the scenario's verdict with and without a foreign sleeper.
        self.claims.mkdir()
        body = {"schema": "mlv-app/venue-claim/v1", "venue": "bachelor", "nonce": "n" * 32}
        self.claim_path.write_text(json.dumps(body), encoding="utf-8")

        def read_once(path: Path, timeout_s: float = 0.0) -> dict:
            return json.loads(path.read_text(encoding="utf-8"))

        for foreign in (False, True):
            with self.subTest(foreign_sleeper=foreign):
                out = self._read_claim_while_writer_holds(reader_timeout_s=30.0, reader=read_once, foreign_sleeper=foreign)
                with self.assertRaises(AssertionError):
                    self._assert_reader_waited_out(out, body)

    def test_exclusive_hold_release_reports_a_close_handle_failure(self) -> None:
        # CloseHandle returns 0 on failure; the hold may then still be up while release() claimed it was over.
        target = self.case / "held.txt"
        target.write_text("x", encoding="utf-8")
        hold = ExclusiveHold(target)
        real_k32 = hold._k32

        class FailingK32:
            calls = 0

            def CloseHandle(self, handle) -> int:
                FailingK32.calls += 1
                return 0

        try:
            hold._k32 = FailingK32()
            with self.assertRaises(OSError):
                hold.release()
            self.assertFalse(hold.closed, "a failed CloseHandle is not a closed hold")
            self.assertEqual(FailingK32.calls, 1)
        finally:
            hold._k32 = real_k32
            hold.release()   # really close it so the file can be removed
        self.assertTrue(hold.closed)

    def test_the_flaky_test_reads_its_claims_through_read_claim_json(self) -> None:
        # CI-FLAKE-LEASE-BATCH-CLAIM-READ-RACE-HARDEN-1 (b): read_claim_json is only a fix while the flaky test calls it. Reverting its c0/c1 reads to
        # json.loads(path.read_text()) stayed green, so pin the call site by source: c0 and c1 are assigned from read_claim_json and the method has no raw read_text.
        name = "test_a_long_foreign_refusal_wait_does_not_let_the_live_claim_expire"
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        fns = [n for c in tree.body if isinstance(c, ast.ClassDef) and c.name == type(self).__name__ for n in c.body if isinstance(n, ast.FunctionDef) and n.name == name]
        self.assertEqual(len(fns), 1, f"{name} was renamed or removed: re-point this pin at the test that reads the live claim")
        fn = fns[0]
        via_helper = {t.id for a in ast.walk(fn) if isinstance(a, ast.Assign) and isinstance(a.value, ast.Call) and isinstance(a.value.func, ast.Name) and a.value.func.id == "read_claim_json"
                      for t in a.targets if isinstance(t, ast.Name)}
        self.assertEqual(via_helper, {"c0", "c1"}, "c0 and c1 are read with read_claim_json")
        raw = [ast.unparse(n) for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "read_text"]
        self.assertEqual(raw, [], "no raw read_text in the flaky test: a claim read there races the runner's FileShare.None write")

    # ---------------------------------------------------------------- r5: the claim is renewed for as long as it is held (dot 242171b3 B1)
    def test_a_long_foreign_refusal_wait_does_not_let_the_live_claim_expire(self) -> None:
        # -ClaimTtlSec 60 (its floor), and um-run blocks 80 s on the open while the agent re-asks a refused (exit 75) request: longer than the claim's TTL. At
        # TTL + 10 s VenueChain.psm1's own Get-VenueClaim (Get-VenueClaimStaleReason) still finds the claim LIVE, with the same nonce and a later expiresUtc,
        # and no waiting chain could break it. RED (no renewal): the claim reads STALE reason=expired while its owner is alive and holding the venue.
        # r8: the open is released as soon as the TTL + 10 s checks are made (FAKE_OPEN_RELEASE; 120 s is only its bound), not slept out to 80 s. The 70 s horizon itself stays:
        # -ClaimTtlSec has a 60 s floor, and a claim that survives its TTL can only be seen after it.
        release = self.case / "open.release"
        release.unlink(missing_ok=True)
        args, e = self.batch_cmd([self.specs[0]], "-ClaimTtlSec", "60", FAKE_OPEN_SLEEP="120", FAKE_OPEN_RELEASE=str(release))
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=e, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        try:
            end = time.monotonic() + 60
            while not self.claim_path.exists() and time.monotonic() < end:
                time.sleep(0.2)
            c0 = read_claim_json(self.claim_path)
            claimed = parse_utc(c0["claimedUtc"])
            while datetime.now(timezone.utc) < claimed + timedelta(seconds=70):
                time.sleep(0.5)
            self.assertIsNone(p.poll(), "the run is still in its open's wait at TTL + 10 s")
            script = f"Import-Module '{psq(MODULE)}' -Force -DisableNameChecking\n$c = Get-VenueClaim -Venue bachelor -ClaimDir '{psq(self.claims)}'\n[ordered]@{{ state = $c.state; reason = $c.reason; nonce = $c.claim.nonce }} | ConvertTo-Json -Compress\n"
            out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            seen = json.loads(out.stdout)
            self.assertEqual((seen["state"], seen["reason"], seen["nonce"]), ("LIVE", None, c0["nonce"]), seen)
            c1 = read_claim_json(self.claim_path)
            self.assertGreater(parse_utc(c1["expiresUtc"]), datetime.now(timezone.utc), c1)
            self.assertEqual({k: v for k, v in c1.items() if k != "expiresUtc"}, {k: v for k, v in c0.items() if k != "expiresUtc"}, "only expiresUtc changes")
            release.write_text("go", encoding="utf-8")
            so, se = p.communicate(timeout=300)
        finally:
            if p.poll() is None:
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, text=True, timeout=60)
                p.wait(timeout=60)
        self.assertEqual(p.returncode, 0, so + se)
        [b] = self.batches()
        self.assertEqual((b["endReason"], b["legsCounted"]), ("ALL_LEGS_DONE", 1))
        self.assertGreaterEqual(b["claimRenew"]["renews"], 4, b["claimRenew"])
        self.assertEqual((b["claimRenew"]["renewSec"], b["claimRenew"]["ttlSec"], b["claimRenew"]["lost"], b["claimRenew"]["errors"]), (15, 60, None, 0), b["claimRenew"])
        self.assertFalse(self.claim_path.exists(), "the claim is released at the end")

    def test_the_claim_renewal_never_takes_over_another_owners_claim(self) -> None:
        # Update-VenueClaim renews only the claim with its nonce: another owner's claim (a new one after a break) is NOT_OURS and left byte for byte.
        self.claims.mkdir()
        script = (
            f"Import-Module '{psq(MODULE)}' -Force -DisableNameChecking\n"
            f"$c = Request-VenueClaim -Venue bachelor -RunDir 'x' -TtlSec 60 -ClaimDir '{psq(self.claims)}'\n"
            f"$before = [IO.File]::ReadAllText('{psq(self.claim_path)}')\n"
            f"$other = Update-VenueClaim -Venue bachelor -Nonce 'not-the-nonce' -TtlSec 600 -ClaimDir '{psq(self.claims)}'\n"
            f"$same = ([IO.File]::ReadAllText('{psq(self.claim_path)}') -ceq $before)\n"
            f"$mine = Update-VenueClaim -Venue bachelor -Nonce $c.claim.nonce -TtlSec 600 -ClaimDir '{psq(self.claims)}'\n"
            f"$after = Get-VenueClaim -Venue bachelor -ClaimDir '{psq(self.claims)}'\n"
            f"$rel = Release-VenueClaim -Venue bachelor -Nonce $c.claim.nonce -ClaimDir '{psq(self.claims)}'\n"
            f"$gone = Update-VenueClaim -Venue bachelor -Nonce $c.claim.nonce -TtlSec 600 -ClaimDir '{psq(self.claims)}'\n"
            "[ordered]@{ other = $other.state; same = $same; mine = $mine.state; live = $after.state; ttlLeft = [int](($after.claim.expiresUtc.ToUniversalTime() - [DateTime]::UtcNow).TotalSeconds); rel = $rel; gone = $gone.state } | ConvertTo-Json -Compress\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        got = json.loads(out.stdout)
        self.assertEqual((got["other"], got["same"], got["mine"], got["live"], got["rel"], got["gone"]), ("NOT_OURS", True, "RENEWED", "LIVE", "RELEASED", "ABSENT"), got)
        self.assertGreater(got["ttlLeft"], 500, got)

    # ---------------------------------------------------------------- r5b: no sidecar, no open (dot ef5805a4 residual B1)
    def _deny_writes(self, folder: Path) -> None:
        """The first sidecar write fails: this user may not create files in the folder (a deny ACE). Removed again at cleanup."""
        who = f"{os.environ['USERDOMAIN']}\\{os.environ['USERNAME']}"
        subprocess.run(["icacls", str(folder), "/deny", f"{who}:(OI)(CI)(WD,AD)"], check=True, capture_output=True, text=True, timeout=60)
        self.addCleanup(subprocess.run, ["icacls", str(folder), "/remove:d", who], capture_output=True, text=True, timeout=60)

    def _assert_no_open_submitted(self, r: subprocess.CompletedProcess, first: str) -> dict:
        self.assertEqual(r.returncode, 9, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["endReason"], b["openOutcome"], b["leaseId"], b["openCancel"], b["closeJobId"], b["ledger"]), ("LIVENESS_UNAVAILABLE", "NOT_SUBMITTED", None, None, None, None), b)
        self.assertIn(f"first liveness write {first}", b["endDetail"])
        self.assertEqual(self.kinds(), [], "no open, close, probe or leg once the first sidecar write failed")
        self.assertEqual(list((self.share / "inbox").glob("*.job.ps1")) if (self.share / "inbox").exists() else [], [], "a job reached the inbox")
        self.assertEqual(b["openLiveness"]["end"], "ABSENT", b["openLiveness"])
        self.assertEqual(b["legsAttempted"], 0)
        res = batch_result(r.stdout)
        self.assertEqual((res["stopReason"], res["ran"], res["counted"], res["notRun"]), ("LIVENESS_UNAVAILABLE", 0, 0, 2))
        self.assertFalse(self.claim_path.exists(), "the claim was left behind")
        log = (self.run_dir / "batch.log").read_text(encoding="utf-8")
        self.assertIn(f" BATCH_OPEN_LIVENESS batch=1 job={b['openJobId']} first={first} ", log)
        return b

    def test_r5b_a_missing_notafter_folder_submits_no_open(self) -> None:
        # RED on 3da8db54: the open is submitted with no sidecar (the run goes on to its legs, exit 0).
        (self.share / "notafter").rmdir()
        b = self._assert_no_open_submitted(self.run_batch(self.specs[:2]), "NO_NOTAFTER_DIR")
        self.assertIn("no notafter folder", b["endDetail"])

    def test_r5b_a_failed_first_sidecar_write_submits_no_open(self) -> None:
        # The folder is there but the write is denied: UNKNOWN. RED on 3da8db54 twice over: the writer said WRITTEN for a denied write (a .NET exception in
        # try/finally with no catch ran on to '<WRITTEN>'), and the open was submitted anyway.
        self._deny_writes(self.share / "notafter")
        b = self._assert_no_open_submitted(self.run_batch(self.specs[:2]), "UNKNOWN")
        self.assertIn("denied", b["endDetail"])

    def test_r5b_a_runner_killed_right_after_a_failed_first_write_leaves_no_open_in_the_inbox(self) -> None:
        # The early-death case: the first write is denied, and the runner is killed (process tree) 2 s after it logs that write, while an open it had
        # submitted would be blocked in um-run (FAKE_OPEN_SLEEP). No OPEN job file is ever in the agent's inbox. RED on 3da8db54: the open is in the inbox.
        self._deny_writes(self.share / "notafter")
        args, e = self.batch_cmd([self.specs[0]], FAKE_OPEN_SLEEP="200")
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=e, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        try:
            log = self.run_dir / "batch.log"
            end = time.monotonic() + 60
            while time.monotonic() < end and not (log.exists() and " BATCH_OPEN_LIVENESS " in log.read_text(encoding="utf-8")):
                time.sleep(0.1)
            time.sleep(2)
            if p.poll() is None:
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, text=True, timeout=60)
            p.wait(timeout=60)
            time.sleep(2)   # anything in flight at the kill lands, or not
            inbox = self.share / "inbox"
            self.assertEqual(sorted(f.name for f in inbox.glob("lease-batch-open-*")) if inbox.exists() else [], [], "an OPEN reached the inbox after a failed first write")
            self.assertNotIn("OPEN:", " ".join(self.kinds()))
            self.assertIn(" first=UNKNOWN ", log.read_text(encoding="utf-8"))
            self.assertFalse(_pid_alive(p.pid))
        finally:
            if p.poll() is None:
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, text=True, timeout=60)
                p.wait(timeout=60)
            p.stdout.close()
            p.stderr.close()

    # ---------------------------------------------------------------- r6: the first sidecar write is read back with the agent's own parser (Grok G2 on #633)
    def _mirror_with_bad_sidecar(self, json_patch: str = "", writer_patch: tuple[str, str] | None = None,
                                 module_patches: tuple[tuple[str, str], ...] = (), agent_patches: tuple[tuple[str, str], ...] = (),
                                 runner_patches: tuple[tuple[str, str], ...] = ()) -> None:
        """A copy of tools/profiling/{dual-venue,bachelor/ultra-magnus-agent.ps1} in which the runner's sidecar writer still says WRITTEN but writes bytes the agent
        would not read as live. json_patch is PowerShell run on $json before it is written; writer_patch replaces a statement of the write itself. The runner under test
        is the copy's own Invoke-VenueLeaseBatch.ps1 (self.runner), reading the agent's functions from the copy's bachelor folder."""
        tree = self.case / "tree" / "tools" / "profiling"
        shutil.copytree(DV, tree / "dual-venue", ignore=shutil.ignore_patterns("look-receipts", "legs", "legsets"))
        (tree / "bachelor").mkdir()
        shutil.copy2(AGENT, tree / "bachelor" / AGENT.name)
        if agent_patches:
            agent_copy = tree / "bachelor" / AGENT.name
            agent_text = agent_copy.read_text(encoding="utf-8")
            for old, new in agent_patches:
                self.assertEqual(agent_text.count(old), 1, "the agent line moved: update this fixture")
                agent_text = agent_text.replace(old, new)
            agent_copy.write_text(agent_text, encoding="utf-8")
        mod = tree / "dual-venue" / "VenueChain.psm1"
        text = mod.read_text(encoding="utf-8")
        for old, new in module_patches:
            self.assertEqual(text.count(old), 1, "the module line moved: update this fixture")
            text = text.replace(old, new)
        anchor = "[pscustomobject]@{ path = ($AgentShare.TrimEnd('\\') + '\\notafter\\' + $JobId + '.json'); json = $json }"
        self.assertEqual(text.count(anchor), 1, "the sidecar write argument moved: update this fixture")
        text = text.replace(anchor, (json_patch + "\n    " if json_patch else "") + anchor)
        if writer_patch:
            self.assertEqual(text.count(writer_patch[0]), 1, "the sidecar write moved: update this fixture")
            text = text.replace(writer_patch[0], writer_patch[1])
        mod.write_text(text, encoding="utf-8")
        self.runner = tree / "dual-venue" / RUNNER.name
        if runner_patches:
            run_text = self.runner.read_text(encoding="utf-8")
            for old, new in runner_patches:
                self.assertEqual(run_text.count(old), 1, "the runner line moved: update this fixture")
                run_text = run_text.replace(old, new)
            self.runner.write_text(run_text, encoding="utf-8")

    def _assert_readback_refused(self, r: subprocess.CompletedProcess, want_state: str, end_reason: str = "LIVENESS_UNAVAILABLE", code: int = 9,
                                 detail: str = "agent's own parser does not read it as live") -> dict:
        """The first write said WRITTEN, the agent's parser disagrees: the open is never submitted and the batch ends exactly as a failed write ends it
        (or, r2, DEADLINE exit 3 when the deadline crossed under a live sidecar). Nothing reaches Submit-AgentJob: the fake um-run's call log and the inbox stay empty."""
        self.assertEqual(r.returncode, code, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["endReason"], b["openOutcome"], b["leaseId"], b["openCancel"], b["closeJobId"], b["ledger"]), (end_reason, "NOT_SUBMITTED", None, None, None, None), b)
        self.assertEqual(self.kinds(), [], "no open, close, probe or leg once the read-back was not live")
        self.assertFalse([l for l in (self.events_raw() if self.events.exists() else []) if " UMRUN " in l], "the fake um-run was called")
        self.assertEqual(list((self.share / "inbox").glob("*.job.ps1")) if (self.share / "inbox").exists() else [], [], "a job reached the inbox")
        self.assertIn("first liveness write WRITTEN", b["endDetail"])
        self.assertIn(detail, b["endDetail"])
        rb = b["openLivenessReadback"]
        self.assertEqual((rb["live"], rb["livenessState"]), (False, want_state), rb)
        self.assertTrue(rb["why"], rb)
        self.assertIn(rb["why"], b["endDetail"])
        self.assertEqual(list((self.share / "notafter").glob("lease-batch-open-*")), [], "the refused sidecar was left behind")
        self.assertEqual(b["legsAttempted"], 0)
        res = batch_result(r.stdout)
        self.assertEqual((res["stopReason"], res["ran"], res["counted"], res["notRun"]), (end_reason, 0, 0, 2))
        self.assertFalse(self.claim_path.exists(), "the claim was left behind")
        log = (self.run_dir / "batch.log").read_text(encoding="utf-8")
        self.assertIn(f" BATCH_OPEN_LIVENESS batch=1 job={b['openJobId']} first=WRITTEN ", log)
        self.assertIn(f" BATCH_OPEN_LIVENESS_READBACK batch=1 job={b['openJobId']} live=False livenessState={want_state} ", log)
        return b

    # (what the bytes are, PowerShell that makes them, the agent's livenessState for them; the agent lines are ultra-magnus-agent.ps1 Get-JobNotAfterVerdict / Read-JobNotAfterSidecar)
    BAD_SIDECARS = (
        ("no aliveUntilUtc (:803)", "$json = $json -replace '\"aliveUntilUtc\":\"[^\"]*\",', ''", "unparseable"),
        ("aliveUntilUtc that is no time (:803)", "$json = $json -replace '\"aliveUntilUtc\":\"[^\"]*\"', '\"aliveUntilUtc\":\"not a time\"'", "unparseable"),
        ("aliveUntilUtc already passed (:815)", "$json = $json -replace '\"aliveUntilUtc\":\"[^\"]*\"', '\"aliveUntilUtc\":\"2001-01-01T00:00:00.0000000Z\"'", "lapsed"),
        ("another job's id (:799)", "$json = $json -replace '\"jobId\":\"[^\"]*\"', '\"jobId\":\"lease-batch-open-someone-else\"'", "unparseable"),
        ("another schema (:798)", "$json = $json -replace 'mlv-app/job-notafter-sidecar/v1', 'mlv-app/job-notafter-sidecar/v0'", "unparseable"),
        ("cut-off JSON (:791)", "$json = $json.Substring(0, 40)", "unparseable"),
        ("an array root (:790)", "$json = '[' + $json + ']'", "unparseable"),
        ("over 4096 bytes (:858)", "$json = $json + (' ' * 5000)", "unparseable"),
    )

    def test_r6_a_written_sidecar_the_agent_would_not_read_as_live_submits_no_open(self) -> None:
        # Grok G2 on #633: the first write reports WRITTEN but the agent would parse the bytes as absent / unparseable / lapsed. RED on 13d65263: the open is
        # submitted (the batch runs on to its legs, exit 0), then refused or lapsed on the agent.
        for label, patch, want in self.BAD_SIDECARS:
            with self.subTest(label):
                self.setUp()   # a fresh share, claim dir and run dir per case
                self._mirror_with_bad_sidecar(json_patch=patch)
                self._assert_readback_refused(self.run_batch(self.specs[:2]), want)

    def test_r6_a_sidecar_that_vanishes_after_a_written_write_submits_no_open(self) -> None:
        # The write returned WRITTEN and the file is gone when read back: the agent reads no sidecar (absent), so the open would have no liveness bound at all.
        self._mirror_with_bad_sidecar(writer_patch=("[IO.File]::Move($tmp, $A.path, $true)", "[IO.File]::Move($tmp, $A.path, $true); Remove-Item -LiteralPath $A.path -Force"))
        self._assert_readback_refused(self.run_batch(self.specs[:2]), "absent")

    def test_r6_the_mirror_with_an_unchanged_writer_still_submits_the_open(self) -> None:
        # Control: the copied tree is faithful, so the refusals above come from the bytes, not from the copy. A live sidecar is read back live and the batch runs.
        self._mirror_with_bad_sidecar()
        r = self.run_batch(self.specs[:2])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["endReason"], b["openOutcome"], b["legsCounted"]), ("ALL_LEGS_DONE", "RECEIPT", 2), b)
        self.assertEqual((b["openLivenessReadback"]["live"], b["openLivenessReadback"]["livenessState"]), (True, "live"), b["openLivenessReadback"])
        self.assertEqual(len([k for k in self.kinds() if k.startswith("OPEN:")]), 1)

    def test_r6_the_readback_judges_with_the_agents_functions_and_fails_closed_without_them(self) -> None:
        # Test-VenueJobLivenessReadback lifts Get-JobNotAfterVerdict and Read-JobNotAfterSidecar from the agent script. An agent script that lacks them, or is
        # missing, gives a not-live answer rather than a guess.
        empty = self.case / "no-fns.ps1"
        empty.write_text("function Something-Else { 1 }\n", encoding="utf-8")
        script = (
            f"Import-Module '{psq(MODULE)}' -Force -DisableNameChecking\n"
            "[ordered]@{\n"
            f"  noFns = Test-VenueJobLivenessReadback -AgentShare '{psq(self.share)}' -JobId 'j1' -AgentScript '{psq(empty)}'\n"
            f"  noFile = Test-VenueJobLivenessReadback -AgentShare '{psq(self.share)}' -JobId 'j1' -AgentScript '{psq(self.case / 'missing.ps1')}'\n"
            f"  noSidecar = Test-VenueJobLivenessReadback -AgentShare '{psq(self.share)}' -JobId 'j1'\n"
            "} | ConvertTo-Json -Compress -Depth 4\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        got = json.loads(out.stdout)
        self.assertEqual((got["noFns"]["live"], got["noFile"]["live"], got["noSidecar"]["live"]), (False, False, False), got)
        self.assertIn("does not define both", got["noFns"]["error"])
        self.assertEqual((got["noSidecar"]["livenessState"], got["noSidecar"]["error"]), ("absent", None), got["noSidecar"])

    # ---------------------------------------------------------------- r2: the three regression cases hub ACK 6394063078 promised, the deadline typing, the agent-script sha
    def test_r2_a_stalled_readback_worker_is_not_live_and_submits_no_open(self) -> None:
        # (a) VenueChain.psm1 Invoke-VenueBoundedRead: the worker reading the sidecar back never answers (the lifted agent reader sleeps 120 s; the read-back's bound is
        # patched down to 2 s so the case stays fast). The bound expires -> not live (UNKNOWN) -> no Submit-AgentJob, exit 9 LIVENESS_UNAVAILABLE, keeper stopped, sidecar removed.
        # RED on 13d65263 (no read-back: the open is submitted, exit 0).
        self._mirror_with_bad_sidecar(
            module_patches=(("[string]$AgentScript = '', [ValidateRange(1, 600)][int]$TimeoutSec = 30)", "[string]$AgentScript = '', [ValidateRange(1, 600)][int]$TimeoutSec = 2)"),),
            agent_patches=(('$path = Join-Path $notAfterDir "$JobId.json"', '$path = Join-Path $notAfterDir "$JobId.json"; Start-Sleep -Seconds 120'),))
        started = time.monotonic()
        r = self.run_batch(self.specs[:2])
        elapsed = time.monotonic() - started
        b = self._assert_readback_refused(r, "UNKNOWN")
        self.assertEqual(b["openLivenessReadback"]["state"], "UNKNOWN")
        self.assertIn("no answer in 2 s", b["openLivenessReadback"]["why"])
        self.assertLess(elapsed, 100, "the abandoned worker held the runner (it sleeps 120 s)")

    def test_r2_the_readback_helper_gives_up_on_a_stalled_worker_within_its_timeout(self) -> None:
        # The helper on its own: an agent script whose reader never returns, -TimeoutSec 1 -> not live, UNKNOWN, 'no answer', in about a second (not the reader's 120 s).
        stall = self.case / "stalled-agent.ps1"
        stall.write_text("function Get-JobNotAfterVerdict { param($Metadata, $Sidecar) 1 }\nfunction Read-JobNotAfterSidecar { param($JobId) Start-Sleep -Seconds 120 }\n", encoding="utf-8")
        script = (
            f"Import-Module '{psq(MODULE)}' -Force -DisableNameChecking\n"
            f"$t = [Diagnostics.Stopwatch]::StartNew(); $r = Test-VenueJobLivenessReadback -AgentShare '{psq(self.share)}' -JobId 'j1' -AgentScript '{psq(stall)}' -TimeoutSec 1; $t.Stop()\n"
            "[ordered]@{ live = $r.live; state = $r.state; livenessState = $r.livenessState; error = $r.error; ms = $t.ElapsedMilliseconds } | ConvertTo-Json -Compress\n"
            "Complete-VenueProcess -ExitCode 0\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=100)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        got = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual((got["live"], got["state"], got["livenessState"]), (False, "UNKNOWN", "UNKNOWN"), got)
        self.assertIn("no answer in 1 s", got["error"])
        self.assertLess(got["ms"], 20000, got)

    def test_r2_a_sidecar_holding_invalid_utf8_is_not_live_and_submits_no_open(self) -> None:
        # (b) ultra-magnus-agent.ps1 Read-JobNotAfterSidecar :859 reads with a strict UTF-8 decoder, so bytes that are not UTF-8 are 'unreadable' and the verdict
        # fails closed (:782). The writer still says WRITTEN; the file then holds 0xFF 0xFE bytes. -> not live, refuse, no open. RED on 13d65263 (the open is submitted).
        bad = "[IO.File]::Move($tmp, $A.path, $true); [IO.File]::WriteAllBytes($A.path, [byte[]](0x7B, 0x22, 0xFF, 0xFE, 0x22, 0x7D))"
        self._mirror_with_bad_sidecar(writer_patch=("[IO.File]::Move($tmp, $A.path, $true)", bad))
        b = self._assert_readback_refused(self.run_batch(self.specs[:2]), "unparseable")
        self.assertIn("could not be read", b["openLivenessReadback"]["why"])

    def test_r2_a_deadline_crossed_under_a_live_sidecar_ends_deadline_and_submits_no_open(self) -> None:
        # (c) The runner's own deadline check (Invoke-VenueLeaseBatch.ps1 :349) passed, then the deadline crossed before the read-back: the agent's verdict is
        # state=passed with livenessState=live (ultra-magnus-agent.ps1 :818-825). Simulated by handing the read-back a notAfterUtc 5 s in the past, the sidecar live.
        # r1 typed this LIVENESS_UNAVAILABLE exit 9 with "(read-back live)"; r2 types it DEADLINE exit 3 (the runner's deadline code) and the open is still never submitted.
        anchor = "    $ast = [System.Management.Automation.Language.Parser]::ParseFile($AgentScript, [ref]$null, [ref]$null)"
        self._mirror_with_bad_sidecar(module_patches=((anchor, "    $NotAfterUtc = [DateTime]::UtcNow.AddSeconds(-5).ToString('o')\n" + anchor),))
        b = self._assert_readback_refused(self.run_batch(self.specs[:2]), "live", end_reason="DEADLINE", code=3, detail="the batch deadline passed first")
        rb = b["openLivenessReadback"]
        self.assertEqual((rb["state"], rb["livenessState"], rb["live"]), ("passed", "live", False), rb)
        self.assertNotIn("does not read it as live", b["endDetail"])

    def test_r2_the_readback_record_carries_the_agent_scripts_sha256(self) -> None:
        # Opus H3: the read-back judges with the checkout's agent script; its sha256 in openLivenessReadback makes a later drift from the deployed agent visible.
        import hashlib
        self._mirror_with_bad_sidecar()
        r = self.run_batch(self.specs[:2])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        sha = b["openLivenessReadback"]["agentScriptSha256"]
        self.assertRegex(sha, r"^[0-9a-f]{64}$")
        self.assertEqual(sha, hashlib.sha256(AGENT.read_bytes()).hexdigest(), "the mirror's agent script is the checkout's, byte for byte")

    # ---------------------------------------------------------------- r3: a read-back that THROWS before the open is submitted is not a sent open (Dot B1 on 46768a21)
    def test_r3_a_readback_that_throws_before_the_open_is_submitted_cancels_nothing_closes_nothing_and_charges_nothing(self) -> None:
        # VenueChain.psm1 Test-VenueJobLivenessReadback: Test-Path on the agent script succeeds, then the hash (Get-FileHash) or the parse (ParseFile) throws because
        # the file is locked or vanished. RED on 46768a21: the exception left $op null, the finally block read a null $op as "maybe sent", requested a cancel, submitted
        # a close and wrote a full-LeaseCapSec open-unreconciled ledger row for an open that was never submitted, and the exception then escaped past the summary.
        # (Parser::ParseFile itself does not throw on a missing file: it returns an error AST, which the helper already treats as "functions not defined".) Case 1: the
        # path Get-FileHash reads is swapped for one that does not exist, so the .NET call really throws. Case 2: a simulated throw where the helper reads the script.
        # The batch ends typed, with its summary.
        hash_old = "(Get-FileHash -LiteralPath $AgentScript -Algorithm SHA256)"
        read_old = "    $ast = [System.Management.Automation.Language.Parser]::ParseFile($AgentScript, [ref]$null, [ref]$null)"
        for label, old, new in (
            ("the hash throws", hash_old, "(Get-FileHash -LiteralPath ($AgentScript + '.vanished') -Algorithm SHA256)"),
            ("the read throws", read_old, "    throw 'vanished: simulated read failure, the agent script is locked'\n" + read_old),
        ):
            with self.subTest(label):
                self.setUp()   # a fresh share, claim dir and run dir per case
                self._mirror_with_bad_sidecar(module_patches=((old, new),))
                r = self.run_batch(self.specs[:2], "-CancelReconcileSec", "3")
                b = self._assert_readback_refused(r, "UNKNOWN")
                self.assertIn("vanished", b["openLivenessReadback"]["why"], b["openLivenessReadback"])
                self.assertEqual(list((self.share / "cancel").iterdir()), [], "a cancel was requested for an open that was never submitted")
                self.assertFalse((self.claims / "bachelor.lease-ledger.jsonl").exists(), "the ledger was charged for an open that was never submitted")
                self.assertIn("LEASE_BATCH_RESULT=", r.stdout, "the original exception escaped past the batch summary")
                log = (self.run_dir / "batch.log").read_text(encoding="utf-8")
                self.assertNotIn(" BATCH_OPEN_CANCEL ", log)
                self.assertNotIn(" BATCH_OPEN_RECONCILED ", log)

    def test_r3_a_submit_that_throws_is_an_uncertain_open_and_keeps_the_conservative_cancel_close_and_ledger_row(self) -> None:
        # Control for the case above: here the open IS (maybe) submitted. Submit-AgentJob throws for the open job before it reports anything; the
        # submitted flag, not $op, tells the finally block the open may be in the agent's gate: cancel requested, close submitted, open-unreconciled row charged.
        # r16 (sol r15 B1; hub ruling 08:18:44Z): "submitted" now starts when um-run.ps1 is called (a job file never written reached no one), so the throw is placed there.
        anchor = "        $script:umRunCalled[$JobId] = $true"
        self._mirror_with_bad_sidecar(runner_patches=((anchor, anchor + "\n        if ($JobId -like 'lease-batch-open-*') { throw 'simulated submit failure' }"),))
        r = self.run_batch([self.specs[0]], "-DeadlineUtc", utc_iso(datetime.now(timezone.utc) + timedelta(seconds=30)), "-CancelReconcileSec", "3")
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["openCancel"]["requested"], b["openCancel"]["state"]), ("REQUESTED", "UNRECONCILED"), b)
        self.assertTrue((self.share / "cancel" / f"{b['openJobId']}.cancel").exists(), "the cancel request stays for the agent")
        self.assertTrue(b["closeJobId"], "the close was not submitted")
        self.assertEqual(b["ledger"]["basis"], "open-unreconciled", b["ledger"])
        self.assertTrue((self.claims / "bachelor.lease-ledger.jsonl").exists())

    # ---------------------------------------------------------------- r16 (sol r15 B1): an open whose job file was never complete here never reached um-run
    # The local File.Open(CreateNew) of the open's job file, or the write into it, fails before um-run is called. RED on 5c73c842: $openSubmitted was already true, so
    # the finally requested a cancel, submitted a close, charged a full 2700 s open-unreconciled lease and ended OPEN_CANCEL_UNRECONCILED (exit 11).
    OPEN_JOB_CREATE = "$fs = [IO.File]::Open($f, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)"
    OPEN_JOB_WRITE = "$fs.Write($jb, 0, $jb.Length)"

    def _assert_open_never_submitted(self, r: subprocess.CompletedProcess) -> None:
        self.assertEqual(r.returncode, 9, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual(res["stopReason"], "OPEN_JOB_WRITE_FAILED", res)
        [b] = self.batches()
        self.assertEqual((b["endReason"], b["openOutcome"]), ("OPEN_JOB_WRITE_FAILED", "JOB_WRITE_FAILED"), b)
        self.assertEqual(self.kinds(), [], "um-run was called for the open or a close, or a probe or a leg ran")
        self.assertEqual(list((self.share / "cancel").iterdir()), [], "a cancel was requested for an open that never reached um-run")
        self.assertEqual((b["openCancel"], b["closeJobId"], b["ledger"]), (None, None, None), b)
        self.assertFalse((self.claims / "bachelor.lease-ledger.jsonl").exists(), "the lease budget was charged for an open that never reached um-run")
        self.assertEqual(list((self.share / "notafter").iterdir()), [], "the open's liveness sidecar was left behind")
        self.assertFalse(self.claim_path.exists(), "the claim was released")

    def test_r16_b1_an_open_job_file_that_cannot_be_created_is_never_submitted(self) -> None:
        seam = "if ($JobId -like 'lease-batch-open-*') { throw [IO.IOException]::new(\"simulated: the file '$f' exists\") }; " + self.OPEN_JOB_CREATE
        self._mirror_with_bad_sidecar(runner_patches=((self.OPEN_JOB_CREATE, seam),))
        self._assert_open_never_submitted(self.run_batch([self.specs[0]], "-CancelReconcileSec", "3"))

    def test_r16_b1_an_open_job_file_whose_write_fails_is_never_submitted(self) -> None:
        seam = "if ($JobId -like 'lease-batch-open-*') { throw [IO.IOException]::new('simulated: there is not enough space on the disk') }; " + self.OPEN_JOB_WRITE
        self._mirror_with_bad_sidecar(runner_patches=((self.OPEN_JOB_WRITE, seam),))
        self._assert_open_never_submitted(self.run_batch([self.specs[0]], "-CancelReconcileSec", "3"))

    def test_r16_b1_a_job_folder_that_refuses_new_files_submits_no_open(self) -> None:
        # the real failure, no seam: <RunDir>\batch-jobs denies this user "create files" (the caps probe fails there too and reads the fallback caps)
        jobs = self.run_dir / "batch-jobs"
        jobs.mkdir(parents=True)
        user = subprocess.run(["whoami"], capture_output=True, text=True, timeout=60).stdout.strip()
        deny = subprocess.run(["icacls", str(jobs), "/deny", f"{user}:(WD)"], capture_output=True, text=True, timeout=60)
        if deny.returncode != 0:
            self.skipTest(f"icacls could not deny file creation: {deny.stdout} {deny.stderr}")
        try:
            r = self.run_batch([self.specs[0]], "-CancelReconcileSec", "3")
        finally:
            subprocess.run(["icacls", str(jobs), "/remove:d", user], capture_output=True, text=True, timeout=60)
        self._assert_open_never_submitted(r)

    def test_r16_b1_control_an_open_that_reached_um_run_and_stays_unreconciled_is_still_charged_a_full_lease(self) -> None:
        # the hub's control (ruling 08:18:44Z): um-run WAS called for the open and gave up while the agent still holds it in its gate, and nothing answers the cancel
        r = self.run_batch([self.specs[0]], "-DeadlineUtc", utc_iso(datetime.now(timezone.utc) + timedelta(seconds=12)), "-CancelReconcileSec", "3", FAKE_OPEN_MODES="gatehold")
        self.assertEqual(r.returncode, 11, r.stdout + r.stderr)
        self.assertEqual(batch_result(r.stdout)["stopReason"], "OPEN_CANCEL_UNRECONCILED")
        [b] = self.batches()
        self.assertTrue([k for k in self.kinds() if k.startswith("OPEN:")], "um-run was not called for the open")
        self.assertEqual((b["openCancel"]["requested"], b["openCancel"]["state"]), ("REQUESTED", "UNRECONCILED"), b)
        self.assertTrue((self.share / "cancel" / f"{b['openJobId']}.cancel").exists())
        self.assertTrue(b["closeJobId"], "the close was not submitted")
        self.assertEqual(b["ledger"]["basis"], "open-unreconciled", b["ledger"])
        span = datetime.fromisoformat(b["ledger"]["endUtc"].replace("Z", "+00:00")) - datetime.fromisoformat(b["ledger"]["startUtc"].replace("Z", "+00:00"))
        self.assertEqual(round(span.total_seconds()), 2700, b["ledger"])
        self.assertTrue((self.claims / "bachelor.lease-ledger.jsonl").exists())

    # ---------------------------------------------------------------- r5b: no probe or leg without the claim (Grok G1, 6389240991)
    def test_r5b_a_claim_taken_over_mid_batch_ends_the_batch_claim_lost_and_keeps_the_legs_already_run(self) -> None:
        # Leg 1 runs and counts. Leg 2's probe replaces the claim file with another owner's (NOT_OURS); the 1 s keeper sees it, and the check before leg 2
        # ends batch 1 CLAIM_LOST with leg 1 kept. Batch 2 breaks the dead owner's claim and runs leg 2. RED on 3da8db54: leg 2 runs in batch 1 with no claim.
        r = self.run_batch(self.specs[:2], "-ClaimRenewSec", "1", FAKE_QUIET_MODES="QUIET,STEAL,QUIET")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        b1, b2 = self.batches()
        self.assertEqual((b1["endReason"], b1["legsRan"], b1["legsCounted"], b1["claimRenew"]["lost"]), ("CLAIM_LOST", 1, 1, "NOT_OURS"), b1)
        self.assertIn("claim=NOT_OURS", b1["endDetail"])
        self.assertTrue(b1["endDetail"].startswith("before leg 02-fx-b-spec:"), b1["endDetail"])
        self.assertEqual([(l["status"], l["counted"]) for l in b1["legs"]], [("RAN", True)], "the leg already run stays as measured")
        self.assertEqual((b2["endReason"], b2["legsCounted"]), ("ALL_LEGS_DONE", 1))
        legs = [k for k in self.kinds() if k.startswith("LEG:")]
        self.assertEqual(legs, [f"LEG:fx-a:{b1['token']}", f"LEG:fx-b:{b2['token']}"])
        log = (self.run_dir / "batch.log").read_text(encoding="utf-8")
        self.assertIn(" CLAIM_LOST batch=1 leg=02-fx-b-spec phase=leg claim=NOT_OURS ", log)
        self.assertIn(" CLAIM_STALE_BROKEN venue=bachelor reason=dead-pid ", log)

    def test_r5b_a_claim_past_its_last_good_expiry_ends_the_batch_claim_lost(self) -> None:
        # -ClaimTtlSec 60, renewed every 20 s, but the claim file is read-only while um-run blocks 66 s on the open: every renewal comes back BUSY and the
        # last good expiry (the one the claim was taken with) passes. The check before the first probe ends batch 1 CLAIM_LOST with nothing probed or run;
        # batch 2 takes a fresh claim and runs the leg. RED on 3da8db54: batch 1 probes and runs the leg on an expired claim.
        r = self.run_batch([self.specs[0]], "-ClaimTtlSec", "60", "-ClaimRenewSec", "20", FAKE_OPEN_SLEEP="66,0", FAKE_OPEN_CLAIM_READONLY="1,0")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        b1, b2 = self.batches()
        self.assertEqual((b1["endReason"], b1["legsAttempted"], b1["probeRetries"], b1["claimRenew"]["renews"], b1["claimRenew"]["lost"]), ("CLAIM_LOST", 0, 0, 0, None), b1)
        self.assertIn("claim=EXPIRED", b1["endDetail"])
        self.assertIn("lastRenew=BUSY", b1["endDetail"])
        self.assertTrue(b1["endDetail"].startswith("before the probe of leg 01-fx-a-spec:"), b1["endDetail"])
        self.assertEqual((b2["endReason"], b2["legsCounted"]), ("ALL_LEGS_DONE", 1))
        k = self.kinds()
        self.assertEqual(k, [f"OPEN:{b1['token']}", f"CLOSE:{b1['token']}", f"OPEN:{b2['token']}", "QUIET:QUIET", f"LEG:fx-a:{b2['token']}", f"CLOSE:{b2['token']}"])

    # ---------------------------------------------------------------- r5b: an UNRESOLVED leg is withdrawn (hub 13:21Z B2 clause, dot 242171b3)
    def test_r5b_a_leg_with_no_agent_result_is_withdrawn_before_the_close(self) -> None:
        # um-run said UNRESOLVED for the leg's job (no result by name) and Invoke-VenueLeg exited 0 with its receipt. The leg is LEASE_LOST (uncounted) and its
        # job is WITHDRAWN: its cancel file is written before the batch's close job is submitted. RED on 3da8db54: no cancel file, the job is left in place.
        r = self.run_batch([self.specs[0]], FAKE_LEG_MODES="unresolved")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        [leg] = b["legs"]
        self.assertEqual((leg["outcome"], leg["class"], leg["counted"], leg.get("withdraw"), b["endReason"]), ("UNRESOLVED", "LEASE_LOST", False, "REQUESTED", "LEASE_LOST"), leg)
        self.assertTrue((self.share / "cancel" / f"{leg['jobId']}.cancel").exists(), "the leg's job was left in place")
        [close] = (self.share / "fake-close-cancels.txt").read_text(encoding="utf-8").splitlines()
        self.assertIn(f"{leg['jobId']}.cancel", close.split(" ", 1)[1], "the leg was withdrawn only after the close was submitted")
        log = (self.run_dir / "batch.log").read_text(encoding="utf-8")
        self.assertIn(f" LEG_WITHDRAWN leg=01-fx-a-spec batch=1 jobId={leg['jobId']} result=ABSENT printedOutcome=UNRESOLVED cancel=REQUESTED", log)

    def test_r5b_a_leg_with_its_result_is_never_withdrawn(self) -> None:
        # The guard: a leg whose result is in hand gets no cancel file.
        r = self.run_batch([self.specs[0]])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertNotIn("withdraw", b["legs"][0])
        self.assertEqual(list((self.share / "cancel").iterdir()), [])

    # ---------------------------------------------------------------- the job-id contract with the agent
    def test_invoke_venue_leg_tags_its_job_with_the_batch_token_the_agent_reads(self) -> None:
        leg = LEG.read_text(encoding="utf-8")
        self.assertIn("[Parameter(ParameterSetName = 'Leg')][ValidatePattern('^[0-9a-f]{12}$')][string]$LeaseBatch = ''", leg)
        self.assertIn('$jobStem = "dve-$($spec.legId)-$Venue-$Backend-$runStamp$(if ($LeaseBatch) { "-lb$LeaseBatch" })"', leg)
        self.assertIn('$probeJobId = "$jobStem-health"', leg)
        stem = "dve-m16-1243-display-windowed-s1-bachelor-cpu-20261023184200-lb0123456789ab"
        script = (
            f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{psq(AGENT)}', [ref]$null, [ref]$null)\n"
            "foreach ($n in 'Get-LeaseBatchToken', 'Test-LeaseBatchCloseJobId') { . ([scriptblock]::Create($ast.Find({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq $n }, $true).Extent.Text)) }\n"
            "$p = ($ast.ParamBlock.Parameters | Where-Object { $_.Name.VariablePath.UserPath -eq 'GateExemptJobPattern' }).DefaultValue.Value\n"
            f"[ordered]@{{ leg = Get-LeaseBatchToken '{stem}'; health = Get-LeaseBatchToken '{stem}-health'; healthLight = ('{stem}-health' -match $p); legLight = ('{stem}' -match $p)\n"
            "  open = Get-LeaseBatchToken 'lease-batch-open-20261023T184200Z-lb0123456789ab'; close = Test-LeaseBatchCloseJobId 'lease-batch-close-20261023T184200Z-lb0123456789ab' } | ConvertTo-Json -Compress\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        got = json.loads(out.stdout)
        self.assertEqual(got, {"leg": "0123456789ab", "health": None, "healthLight": True, "legLight": False, "open": "0123456789ab", "close": True})

    def test_the_runner_names_its_open_and_close_jobs_in_the_agent_shapes(self) -> None:
        text = RUNNER.read_text(encoding="utf-8")
        self.assertIn('$b.openJobId = "lease-batch-open-$stamp-lb$token"', text)
        self.assertIn('$b.closeJobId = "lease-batch-close-$stamp-lb$token"', text)
        self.assertIn("'-LeaseBatch', $token", text)
        self.assertTrue(all(b < 128 for b in RUNNER.read_bytes()), "the runner stays ASCII-only")

    # ---------------------------------------------------------------- VenueChain.psm1 helpers
    def test_job_result_and_batch_receipt_are_read_by_name(self) -> None:
        (self.share / "outbox").mkdir()
        (self.share / "logs").mkdir()
        (self.share / "outbox" / "j1.result.json").write_text(json.dumps({"jobId": "j1", "admission": {"leaseBatch": {"leaseId": "L1"}}}), encoding="utf-8")
        (self.share / "logs" / "lease-batch-0123456789ab.jsonl").write_text(
            json.dumps({"leaseId": "L0", "endReason": "idle"}) + "\n" + json.dumps({"leaseId": "L1", "endReason": "closed"}) + "\n", encoding="utf-8")
        script = (
            f"Import-Module '{psq(MODULE)}' -Force -DisableNameChecking\n"
            f"$s = '{psq(self.share)}'\n"
            "[ordered]@{ found = (Get-VenueJobResult -AgentShare $s -JobId 'j1').result.admission.leaseBatch.leaseId; absent = (Get-VenueJobResult -AgentShare $s -JobId 'j2').state\n"
            "  bad = (Get-VenueJobResult -AgentShare $s -JobId '..\\x').state; rec = (Read-VenueLeaseBatchReceipt -AgentShare $s -Token '0123456789ab' -LeaseId 'L1').record.endReason\n"
            "  none = (Read-VenueLeaseBatchReceipt -AgentShare $s -Token '0123456789ab' -LeaseId 'L9').state; nofile = (Read-VenueLeaseBatchReceipt -AgentShare $s -Token 'ffffffffffff' -LeaseId 'L1').state } | ConvertTo-Json -Compress\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(json.loads(out.stdout), {"found": "L1", "absent": "ABSENT", "bad": "UNKNOWN", "rec": "closed", "none": "ABSENT", "nofile": "ABSENT"})


# LEASE-BATCH-MULTI-BUILD-1: a wrapper around fake-leg.ps1 that records the build, backend and spec each leg was called with, then runs the fake leg unchanged.
FAKE_LEG_ARGS = r"""
param([string]$Venue, [string]$LegSpec, [string]$SourceCommit, [string]$BuildManifestSha256, [string]$Backend = '', [string]$SheetCopyDir = '', [string]$LeaseBatch = '')
Add-Content -LiteralPath $env:FAKE_LEG_ARGS -Value (([ordered]@{ leaf = [IO.Path]::GetFileNameWithoutExtension($LegSpec); sourceCommit = $SourceCommit; buildManifestSha256 = $BuildManifestSha256; backend = $Backend; leaseBatch = $LeaseBatch }) | ConvertTo-Json -Compress)
& (Join-Path $PSScriptRoot 'fake-leg.ps1') @PSBoundParameters
exit $LASTEXITCODE
"""


@requires_windows_pwsh
class VenueLeaseBatchLegPlanTests(VenueLeaseBatchRunnerTests):
    """LEASE-BATCH-MULTI-BUILD-1: one batched lease carries legs of more than one build and backend (-LegPlan), validated before the lease is requested, and
    an interleaving order (A B A B) across arms. Same fakes and seams as the runner tests above; nothing reaches a venue."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        (cls.tmp / "fake-leg-args.ps1").write_text(FAKE_LEG_ARGS, encoding="utf-8")
        cls.commit_a, cls.commit_b = "1" * 40, "2" * 40

    def setUp(self) -> None:
        super().setUp()
        self._stage_case()

    def _stage_case(self) -> None:
        self.leg_args = self.case / "leg-args.jsonl"
        # two builds staged in the venue's cache, by the path Invoke-VenueLeg hashes
        self.manifests = {c: self.stage(c, f"build {c[:1]}") for c in (self.commit_a, self.commit_b)}

    def stage(self, commit: str, body: str, **override: object) -> str:
        # the three fields Assert-AttrCudaBuildManifest (the leg job's own validator) requires: the build's commit, its dll pair digest and a boolean symbol presence
        cache = self.share / "cache"
        cache.mkdir(exist_ok=True)
        p = cache / f"playback-attr-3-cuda-{commit[:12]}-build.json"
        import hashlib
        # LEASE-BATCH-PLAN-ARTIFACT-PREFLIGHT-1: the three cached artifacts the manifest digests name are staged beside it, each hashing to its digest (an override replaces the
        # digest and leaves that artifact unstaged, as the digest-shape cases below need)
        digests = {}
        for label in ARTIFACT_SHAS:
            content = f"{label}:{commit}:{body}".encode("utf-8")
            self.artifact_path(commit, label).write_bytes(content)
            digests[label] = {"sha256": hashlib.sha256(content).hexdigest()}
        doc = {"sourceCommit": commit, "dllPairManifestSha256": "f" * 64, "pendingSymbolPresence": False, **digests, "body": body}
        doc.update(override)
        p.write_text(json.dumps({k: v for k, v in doc.items() if v is not ABSENT}), encoding="utf-8")
        return hashlib.sha256(p.read_bytes()).hexdigest()

    def artifact_path(self, commit: str, label: str) -> Path:
        # the names the leg job reads from the agent cache (Get-AttrCudaArtifactNames)
        s = commit[:12]
        name = {"exe": f"MLVApp-playback-attr-3-cuda-{s}.exe", "dll": f"igpu_recon_cuda-playback-attr-3-cuda-{s}.dll", "packageZip": f"MLVApp-playback-attr-3-cuda-{s}-pkg.zip"}[label]
        return self.share / "cache" / name

    def item(self, spec: Path, arm: str, commit: str, backend: str = "cpu", manifest: str | None = None) -> dict:
        return {"legSpec": str(spec), "backend": backend, "sourceCommit": commit, "buildManifestSha256": manifest or self.manifests[commit], "arm": arm}

    def write_plan(self, items: list[dict]) -> Path:
        p = self.case / "plan.json"
        p.write_text(json.dumps({"schema": "mlv-app/venue-lease-leg-plan/v1", "legs": items}), encoding="utf-8")
        return p

    def plan_cmd(self, plan: Path, *extra: str, **env: str) -> tuple[list[str], dict]:
        e = dict(os.environ)
        e.update({"FAKE_EVENTS": str(self.events), "FAKE_SHARE": str(self.share), "FAKE_CLAIM_PATH": str(self.claim_path), "FAKE_LEG_ROOT": str(self.case),
                  "FAKE_LEG_ARGS": str(self.leg_args)})
        e.update(env)
        defaults = {"-LegBudgetSec": "60", "-LeaseMarginSec": "0", "-ClaimPollSec": "1", "-RecheckSec": "1",
                    "-DeadlineUtc": utc_iso(datetime.now(timezone.utc) + timedelta(seconds=240)), "-LeaseCooldownSec": "0"}
        args = [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(RUNNER), "-LegPlan", str(plan),
                "-RunDir", str(self.run_dir), "-ClaimDir", str(self.claims),
                "-QuietScript", str(self.tmp / "fake-quiet.ps1"), "-LegScript", str(self.tmp / "fake-leg-args.ps1"), "-UmRunScript", str(self.tmp / "fake-umrun.ps1"),
                "-AgentShare", str(self.share), *[x for k, v in defaults.items() if k not in extra for x in (k, v)], *extra]
        return args, e

    def run_plan(self, plan: Path, *extra: str, **env: str) -> subprocess.CompletedProcess:
        args, e = self.plan_cmd(plan, *extra, **env)
        return subprocess.run(args, capture_output=True, text=True, timeout=600, env=e)

    def leg_calls(self) -> list[dict]:
        return [json.loads(l) for l in self.leg_args.read_text(encoding="utf-8").splitlines() if l.strip()] if self.leg_args.exists() else []

    def ab_block_plan(self) -> Path:
        # block order on purpose (A A B B): -PlanOrder Interleave must run it A B A B
        a, b = self.commit_a, self.commit_b
        return self.write_plan([self.item(self.specs[0], "A", a, "cpu"), self.item(self.specs[1], "A", a, "cpu"),
                                self.item(self.specs[0], "B", b, "cuda"), self.item(self.specs[1], "B", b, "cuda")])

    def test_two_builds_interleaved_in_one_lease_each_row_carrying_its_own_build(self) -> None:
        # RED on 3723b76d: the runner has no -LegPlan (one build per call, so an A/B pair takes a lease per build).
        r = self.run_plan(self.ab_block_plan(), "-PlanOrder", "Interleave")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        k = self.kinds()
        opens = [x for x in k if x.startswith("OPEN:")]
        self.assertEqual(len(opens), 1, f"ONE lease request for both builds: {k}")
        token = opens[0].split(":")[1]
        self.assertEqual(k, [f"OPEN:{token}", "QUIET:QUIET", f"LEG:fx-a:{token}", "QUIET:QUIET", f"LEG:fx-a:{token}",
                             "QUIET:QUIET", f"LEG:fx-b:{token}", "QUIET:QUIET", f"LEG:fx-b:{token}", f"CLOSE:{token}"])
        a, b = self.commit_a, self.commit_b
        want = [("fx-a", "A", a, "cpu"), ("fx-a", "B", b, "cuda"), ("fx-b", "A", a, "cpu"), ("fx-b", "B", b, "cuda")]
        # each leg process was handed its own build and backend
        calls = self.leg_calls()
        self.assertEqual([(c["leaf"], c["sourceCommit"], c["buildManifestSha256"], c["backend"], c["leaseBatch"]) for c in calls],
                         [(leaf, c, self.manifests[c], be, token) for leaf, _arm, c, be in want])
        # each batch row and each run result records its own build, backend and arm, in A B A B order
        [bt] = self.batches()
        self.assertEqual((bt["endReason"], bt["legsRan"], bt["legsCounted"], bt["leaseId"]), ("ALL_LEGS_DONE", 4, 4, "lease-1"))
        self.assertEqual([(l["arm"], l["sourceCommit"], l["buildManifestSha256"], l["backend"]) for l in bt["legs"]],
                         [(arm, c, self.manifests[c], be) for _leaf, arm, c, be in want])
        res = batch_result(r.stdout)
        self.assertEqual((res["ran"], res["counted"], res["legPlan"]["order"]), (4, 4, "Interleave"))
        self.assertEqual([(l["name"], l["arm"], l["sourceCommit"], l["buildManifestSha256"], l["backend"]) for l in res["legs"]],
                         [(f"{i + 1:02d}-{leaf}-{be}-{arm}", arm, c, self.manifests[c], be) for i, (leaf, arm, c, be) in enumerate(want)])
        log = (self.run_dir / "batch.log").read_text(encoding="utf-8")
        self.assertIn(f"LEG 02-fx-a-cuda-B RUN batch=1 ", log)
        self.assertIn(f" arm=B sourceCommit={b} buildManifestSha256={self.manifests[b]}", log)
        # the validated, ordered plan is kept beside the receipts
        kept = json.loads((self.run_dir / "leg-plan.json").read_text(encoding="utf-8"))
        self.assertEqual([x["arm"] for x in kept["legs"]], ["A", "B", "A", "B"])
        self.assertEqual(sorted((s["sourceCommit"], s["state"]) for s in kept["builds"]), [(a, "STAGED"), (b, "STAGED")])

    def test_the_plan_runs_as_given_without_the_interleave_option(self) -> None:
        r = self.run_plan(self.ab_block_plan())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [bt] = self.batches()
        self.assertEqual([l["arm"] for l in bt["legs"]], ["A", "A", "B", "B"])
        self.assertEqual(batch_result(r.stdout)["legPlan"]["order"], "AsGiven")

    def test_a_build_that_is_not_staged_is_refused_before_the_lease_is_requested(self) -> None:
        unstaged = "3" * 40
        plan = self.write_plan([self.item(self.specs[0], "A", self.commit_a), self.item(self.specs[0], "B", unstaged, manifest="c" * 64)])
        r = self.run_plan(plan, "-PlanOrder", "Interleave")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=BUILD_NOT_STAGED item=2 ", r.stderr)
        self.assertEqual(self.kinds(), [], "nothing is submitted (no open, no probe, no leg) when one build is not staged")
        self.assertFalse((self.run_dir / "batches.jsonl").exists())
        self.assertFalse(self.claim_path.exists())

    def test_every_item_is_validated_before_the_lease(self) -> None:
        a = self.commit_a
        bad = {
            "BUILD_MANIFEST_MISMATCH": self.item(self.specs[0], "B", a, manifest="d" * 64),
            "BAD_BACKEND": self.item(self.specs[0], "B", a, backend="metal"),
            "BAD_SOURCE_COMMIT": self.item(self.specs[0], "B", "xyz", manifest=self.manifests[a]),
            "BAD_MANIFEST_SHA256": self.item(self.specs[0], "B", a, manifest="e" * 63),
            "BAD_ARM": self.item(self.specs[0], "", a),
            "LEG_SPEC_MISSING": self.item(self.case / "no-such-spec.json", "B", a),
        }
        for reason, item in bad.items():
            with self.subTest(reason=reason):
                r = self.run_plan(self.write_plan([self.item(self.specs[0], "A", a), item]))
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn(f"LEG_PLAN_REFUSED reason={reason} item=2 ", r.stderr)
                self.assertEqual(self.kinds(), [])

    def write_raw_plan(self, doc: object) -> Path:
        p = self.case / "plan.json"
        p.write_text(json.dumps(doc), encoding="utf-8")
        return p

    def assert_whole_plan_refused(self, plan: Path, want: str) -> None:
        # subtests share one fixture directory: clear what an earlier subtest's run left, so a run that wrongly proceeds cannot fail the next case instead of itself
        for p in (self.events, self.leg_args, self.claim_path):
            p.unlink(missing_ok=True)
        shutil.rmtree(self.run_dir, ignore_errors=True)
        r = self.run_plan(plan)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn(f"LEG_PLAN_REFUSED reason={want}", r.stderr)
        self.assertEqual(self.kinds(), [], "nothing is submitted: no open, no probe, no leg")
        self.assertEqual(self.leg_calls(), [])
        self.assertFalse((self.run_dir / "batches.jsonl").exists(), "no lease was opened")
        self.assertFalse(self.claim_path.exists(), "no claim was written")

    def test_a_null_or_non_object_entry_refuses_the_whole_plan_at_its_original_position(self) -> None:
        # Dot B1 (e2160c2d): the null was dropped BEFORE the shape check, so [A, null, B] ran as a two-item plan (B became item 2) and finished ALL_LEGS_DONE.
        a, b = self.commit_a, self.commit_b
        ok_a, ok_b = self.item(self.specs[0], "A", a), self.item(self.specs[1], "B", b, "cuda")
        cases = {
            "null in the middle": ([ok_a, None, ok_b], 2),
            "null first": ([None, ok_a, ok_b], 1),
            "null last": ([ok_a, ok_b, None], 3),
            "string in the middle": ([ok_a, "leg-b", ok_b], 2),
            "number in the middle": ([ok_a, 7, ok_b], 2),
            "array in the middle": ([ok_a, [ok_b], ok_b], 2),
            "only a null": ([None], 1),
        }
        for label, (legs, n) in cases.items():
            for container in ("object", "bare-array"):
                with self.subTest(case=label, container=container):
                    doc = {"schema": "mlv-app/venue-lease-leg-plan/v1", "legs": legs} if container == "object" else legs
                    self.assert_whole_plan_refused(self.write_raw_plan(doc), f"BAD_ITEM_SHAPE item={n} ")

    def test_a_legs_value_that_is_not_an_array_is_refused_not_wrapped_into_a_one_item_plan(self) -> None:
        one = self.item(self.specs[0], "A", self.commit_a)
        schema = "mlv-app/venue-lease-leg-plan/v1"
        cases = {
            "a single item object as legs": {"schema": schema, "legs": one},
            "legs is a string": {"schema": schema, "legs": "leg-a"},
            "legs is a number": {"schema": schema, "legs": 3},
            "legs is null": {"schema": schema, "legs": None},
            "legs is an empty array": {"schema": schema, "legs": []},
            "a bare item object with no legs container": one,
            "the document is a string": "plan",
            "the document is a number": 5,
            "the document is null": None,
            "the document is an empty array": [],
            "the legs container is named something else": {"schema": schema, "items": [one]},
        }
        for label, doc in cases.items():
            with self.subTest(case=label):
                self.assert_whole_plan_refused(self.write_raw_plan(doc), "LEG_PLAN_UNREADABLE")

    def test_r3_a1_a_plan_order_in_any_letter_case_still_interleaves(self) -> None:
        # sol r3 A1: -PlanOrder is bound case-insensitively but was dispatched case-sensitively, so 'interleave' was accepted and ran A A B B.
        r = self.run_plan(self.ab_block_plan(), "-PlanOrder", "interleave")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [bt] = self.batches()
        self.assertEqual([l["arm"] for l in bt["legs"]], ["A", "B", "A", "B"])
        self.assertEqual(batch_result(r.stdout)["legPlan"]["order"], "Interleave", "the receipt names the canonical order, not the spelling typed")

    def test_r3_a2_arms_that_differ_only_in_case_are_separate_arms(self) -> None:
        # sol r3 A2: the arm label is validated case-sensitively (A and a are both legal) but the interleave keyed its queues case-insensitively, merging them.
        script = (
            f"Import-Module '{psq(MODULE)}' -Force -DisableNameChecking\n"
            "$items = 'AAaa'.ToCharArray() | ForEach-Object -Begin { $i = 0 } -Process { $i++; [pscustomobject]@{ arm = [string]$_; n = $i } }\n"
            "(@(Get-VenueInterleavedLegPlan -Items @($items)) | ForEach-Object { \"$($_.arm)$($_.n)\" }) -join ' '\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(out.stdout.strip(), "A1 a3 A2 a4")

    def test_r3_a3_a_trailing_line_feed_never_passes_a_plan_item_pattern(self) -> None:
        # sol r3 A3: '$' also matches before a final LF, so arm "A\n" (an illegal Windows file name once it is in a leg output name) and a 40-hex commit or
        # 64-hex manifest with a trailing LF passed the item patterns. \z is the end of the string.
        a = self.commit_a
        bad = {
            "BAD_ARM": self.item(self.specs[0], "B\n", a),
            "BAD_SOURCE_COMMIT": self.item(self.specs[0], "B", a + "\n", manifest=self.manifests[a]),
            "BAD_MANIFEST_SHA256": self.item(self.specs[0], "B", a, manifest=self.manifests[a] + "\n"),
        }
        for reason, item in bad.items():
            with self.subTest(reason=reason):
                self.assert_whole_plan_refused(self.write_plan([self.item(self.specs[0], "A", a), item]), f"{reason} item=2 ")

    def test_r3_a3_a_spec_legid_with_a_trailing_line_feed_falls_back_to_the_file_name(self) -> None:
        spec = self.case / "fx-nl.json"
        spec.write_text(json.dumps({"schema": "mlv-app/dual-venue-leg/v1", "card": "TEST-CARD", "legId": "bad\n", "backends": ["cpu"], "scaleFactor": 1}), encoding="utf-8")
        r = self.run_plan(self.write_plan([self.item(spec, "A", self.commit_a)]))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([l["name"] for l in batch_result(r.stdout)["legs"]], ["01-fx-nl-cpu-A"])

    def test_r3_a4_an_item_the_leg_script_would_throw_on_is_refused_before_the_lease(self) -> None:
        # sol r3 A4: Invoke-VenueLeg throws DVE_LEG_SPEC_INVALID when the item's backend is not one the spec lists (or the spec is not a leg spec), AFTER the
        # lease is open, which ends as LEG_FAILED / exit 7 instead of the whole plan being refused with exit 2 and nothing submitted.
        a = self.commit_a

        def spec(name: str, text: str) -> Path:
            p = self.case / name
            p.write_text(text, encoding="utf-8")
            return p
        cpu_only = spec("cpu-only.json", json.dumps({"schema": "mlv-app/dual-venue-leg/v1", "legId": "cpu-only", "backends": ["cpu"]}))
        wrong_schema = spec("wrong-schema.json", json.dumps({"schema": "nope", "legId": "ws", "backends": ["cpu"]}))
        not_json = spec("not-json.json", "this is not json")
        no_backends = spec("no-backends.json", json.dumps({"schema": "mlv-app/dual-venue-leg/v1", "legId": "nb"}))
        cases = {
            "BAD_BACKEND": self.item(cpu_only, "B", a, backend="cuda"),
            "LEG_SPEC_INVALID wrong schema": self.item(wrong_schema, "B", a),
            "LEG_SPEC_INVALID not json": self.item(not_json, "B", a),
            "BAD_BACKEND no backends listed": self.item(no_backends, "B", a),
        }
        for label, item in cases.items():
            with self.subTest(case=label):
                self.assert_whole_plan_refused(self.write_plan([self.item(self.specs[0], "A", a), item]), f"{label.split(' ')[0]} item=2 ")

    def test_r3_a5_a_refusal_whose_log_line_cannot_be_written_still_says_so_on_stderr_and_exits_2(self) -> None:
        # sol r3 A5: Stop-PlanRefused wrote batch.log first; with the log held by another process that write threw, so the typed refusal never reached stderr and
        # the exit was 1, not 2.
        self.run_dir.mkdir(parents=True, exist_ok=True)
        log = self.run_dir / "batch.log"
        log.write_text("", encoding="utf-8")
        hold = ExclusiveHold(log)
        try:
            r = self.run_plan(self.case / "missing-plan.json")
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("BATCH_USAGE LEG_PLAN_REFUSED reason=LEG_PLAN_UNREADABLE", r.stderr)
            self.assertEqual(self.kinds(), [])
        finally:
            hold.release()

    def test_r4_a1_a_spec_the_leg_process_would_throw_on_is_refused_before_the_claim_and_the_lease(self) -> None:
        # sol r3 second pass A1: Invoke-VenueLeg also throws on a malformed visualCapture / opt-out pair, a playSeconds outside [int], a scaleFactor that is not a
        # number and a bad acceptedEffectiveScale. Preflight checked only the schema and the backend, so each of these opened a lease and ended LEG_FAILED / exit 7.
        a = self.commit_a
        base = {"schema": "mlv-app/dual-venue-leg/v1", "card": "TEST-CARD", "legId": "fx-bad", "backends": ["cpu"], "scaleFactor": 1}
        cases = {
            "visualCapture is a string": {"visualCapture": "true"},
            "visualCapture false without a reason": {"visualCapture": False},
            "visualCapture null": {"visualCapture": None},
            "a reason without the opt-out": {"visualCaptureOptOutReason": "because"},
            "playSeconds beyond int": {"playSeconds": 2147483648},
            "scaleFactor is not a number": {"scaleFactor": "bad"},
            "acceptedEffectiveScale is not a number": {"scaleFactor": 1, "acceptedEffectiveScale": {"cpu": "bad"}},
        }
        for label, extra in cases.items():
            with self.subTest(case=label):
                p = self.case / "fx-bad.json"
                p.write_text(json.dumps({**base, **extra}), encoding="utf-8")
                self.assert_whole_plan_refused(self.write_plan([self.item(self.specs[0], "A", a), self.item(p, "B", a)]), "LEG_SPEC_INVALID item=2 ")

    def test_r4_a1_a_valid_opt_out_and_scale_still_run(self) -> None:
        # the added preflight is the leg process's own predicates, no stricter: a well-formed opt-out, scale and playSeconds are accepted
        p = self.case / "fx-ok.json"
        p.write_text(json.dumps({"schema": "mlv-app/dual-venue-leg/v1", "card": "TEST-CARD", "legId": "fx-ok", "backends": ["cpu"], "visualCapture": False, "visualCaptureOptOutReason": "no window",
                                 "playSeconds": 30, "scaleFactor": 2, "acceptedEffectiveScale": {"cpu": 1}}), encoding="utf-8")
        r = self.run_plan(self.write_plan([self.item(p, "A", self.commit_a)]))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_r4_a2_a_plan_item_whose_path_cannot_be_probed_is_a_typed_refusal(self) -> None:
        # sol r3 second pass A2: Test-Path throws on a NUL in legSpec; the throw bypassed Stop-PlanRefused and exited 1 with no BATCH_USAGE line.
        a = self.commit_a
        for label, ref in {"NUL in the middle": "a\u0000b", "NUL only": "\u0000", "a wildcard-only name": "*", "a drive-only name": "Z:"}.items():
            with self.subTest(case=label):
                item = {"legSpec": ref, "backend": "cpu", "sourceCommit": a, "buildManifestSha256": self.manifests[a], "arm": "B"}
                self.assert_whole_plan_refused(self.write_plan([self.item(self.specs[0], "A", a), item]), "LEG_SPEC_MISSING item=2 ")

    def test_r4_a3_a_second_invocation_on_the_same_run_dir_never_overwrites_the_plan_before_it_holds_the_claim(self) -> None:
        # sol r3 second pass A3: leg-plan.json was written during validation, before the venue claim. A second invocation that is still waiting for another
        # run's claim replaced the first run's preserved plan. Here a live foreign claim holds the venue and the run ends DEADLINE claim-held: the file stays.
        self.run_dir.mkdir(parents=True, exist_ok=True)
        keep = self.run_dir / "leg-plan.json"
        keep.write_text('{"preserved":"first invocation"}\n', encoding="utf-8")
        script = (f"Import-Module '{psq(MODULE)}' -Force -DisableNameChecking\n"
                  f"$r = Request-VenueClaim -Venue bachelor -RunDir 'C:\\other-run' -Purpose foreign -TtlSec 3600 -ClaimDir '{psq(self.claims)}' -OwnerPid {os.getpid()}\n"
                  "$r.granted\n")
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.stdout.strip(), "True", out.stdout + out.stderr)
        r = self.run_plan(self.ab_block_plan(), "-DeadlineUtc", utc_iso(datetime.now(timezone.utc) + timedelta(seconds=8)))
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertEqual(self.kinds(), [], "no open: the venue claim was never held")
        self.assertEqual(keep.read_text(encoding="utf-8"), '{"preserved":"first invocation"}\n')

    def test_r4_a3_the_plan_is_kept_once_the_claim_is_held(self) -> None:
        r = self.run_plan(self.ab_block_plan(), "-PlanOrder", "Interleave")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        kept = json.loads((self.run_dir / "leg-plan.json").read_text(encoding="utf-8"))
        self.assertEqual([x["arm"] for x in kept["legs"]], ["A", "B", "A", "B"])

    def test_r5_f2_a_spec_without_a_card_or_a_legid_is_refused_before_the_claim_and_the_lease(self) -> None:
        # key3 F2 (fable, opus, sol, Dot B3): Invoke-VenueLeg binds [string]$spec.card and [string]$spec.legId to New-DvReceipt's Mandatory [string] parameters before it
        # submits anything; an empty one is a binding error. Preflight never read either, so such a spec opened a lease and ended LEG_FAILED / exit 7.
        a = self.commit_a
        base = {"schema": "mlv-app/dual-venue-leg/v1", "backends": ["cpu"], "scaleFactor": 1}
        cases = {
            "no card (the key3 repro)": {"legId": "fx-a"},
            "card is empty": {"card": "", "legId": "fx-a"},
            "card is null": {"card": None, "legId": "fx-a"},
            "card present, no legId": {"card": "TEST-CARD"},
            "card present, legId is empty": {"card": "TEST-CARD", "legId": ""},
            "card present, legId is null": {"card": "TEST-CARD", "legId": None},
            "neither": {},
        }
        for label, extra in cases.items():
            with self.subTest(case=label):
                p = self.case / "fx-noid.json"
                p.write_text(json.dumps({**base, **extra}), encoding="utf-8")
                self.assert_whole_plan_refused(self.write_plan([self.item(self.specs[0], "A", a), self.item(p, "B", a)]), "LEG_SPEC_INVALID item=2 ")

    def test_r5_f2_a_spec_with_a_card_and_a_legid_still_runs(self) -> None:
        r = self.run_plan(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_r5_f1_a_throw_after_the_claim_keeper_starts_stops_the_keeper_releases_the_claim_and_ends_the_process(self) -> None:
        # key3 F1 (fable, opus, sol, Dot B2): leg-plan.json was written after Start-VenueClaimKeeper and before the batch try, so a throw there skipped Stop-VenueKeeper and
        # Release-VenueClaim: the keeper runspace kept renewing the claim and the process never exited. A directory of that name makes the write throw.
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "leg-plan.json").mkdir()
        args, e = self.plan_cmd(self.ab_block_plan(), "-PlanOrder", "Interleave")
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=90, env=e)
        except subprocess.TimeoutExpired as ex:
            self.fail(f"the process did not end within 90 s (the claim keeper outlived the throw): {ex.stdout!r}")
        self.assertEqual(r.returncode, 12, r.stdout + r.stderr)
        self.assertFalse(self.claim_path.exists(), "the claim was left behind")
        self.assertEqual(self.kinds(), [], "no open was submitted: the throw came before the lease")
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"]), (12, "BATCH_ERROR"))
        self.assertEqual((res["ran"], res["notRun"]), (0, 4))
        log = (self.run_dir / "batch.log").read_text(encoding="utf-8")
        self.assertIn(" CLAIM_RELEASED batch=1 result=", log)
        self.assertIn(" BATCH_ERROR batch=1 ", log)
        [b] = self.batches()
        self.assertEqual(b["endReason"], "BATCH_ERROR")

    _EXPIRED_MARKER, _ACK_MARKER = "runner-pause-expired.marker", "runner-ack.marker"

    def _pause_snippet(self, message: str, pause_s: float) -> str:
        """The PowerShell the runner copy runs at its first log line starting `message` (see _runner_paused_at_claim_taken); its own method so a test can run it alone."""
        paused, locked = self.case / "runner-paused.marker", self.case / "log-locked.marker"
        expired, ack = self.case / self._EXPIRED_MARKER, self.case / self._ACK_MARKER
        return (f"    if (-not $script:PauseDone -and $Message.StartsWith('{psq(message)}', [StringComparison]::Ordinal)) {{ $script:PauseDone = $true; $n = [guid]::NewGuid().ToString('N'); Set-Content -LiteralPath '{psq(paused)}' -Value $n; "
                 f"$sw = [Diagnostics.Stopwatch]::StartNew(); $seen = $false; "
                 f"while ($sw.Elapsed.TotalSeconds -lt {pause_s}) {{ if (Test-Path -LiteralPath '{psq(locked)}') {{ $seen = $sw.Elapsed.TotalSeconds -lt {pause_s}; break }}; Start-Sleep -Milliseconds 20 }}\n"
                 f"        if ($seen) {{ $held = $false; try {{ [IO.File]::Open($batchLog, 'Open', 'Read', 'ReadWrite').Dispose() }} catch [IO.IOException] {{ $held = $true }}; "
                 f"if ($held -and $sw.Elapsed.TotalSeconds -lt {pause_s}) {{ Set-Content -LiteralPath '{psq(ack)}' -Value ($n + ' held') }} elseif ($held) {{ $seen = $false }} }} "
                 f"if (-not $seen) {{ Set-Content -LiteralPath '{psq(expired)}' -Value ($n + ' expired') }} }}\n")

    def _runner_paused_at_claim_taken(self, message: str = "CLAIM_TAKEN", release_at: str = "", more: tuple = (), pause_s: float = 60) -> tuple[Path, Path, Path]:
        """A copy of the runner that PAUSES when it is about to log a line starting `message` (CLAIM_TAKEN by default): it writes `paused` (holding an attempt nonce it generates) and waits (at most
        `pause_s`, 60 by default) for `locked` to appear. Seeing `locked` inside the window (the clock is read after the look, and again after the probe: adversary r4 A2), it probes batch.log itself and, only if the log really is held, writes `ack` = `<nonce> held`; a pause that
        runs out with no `locked` writes `expired` = `<nonce> expired` and the runner carries on. The pause happens once per run (a later matching line, e.g. batch 2's, never re-pauses or re-acks). A pause marker that survives the expiry is therefore never a handshake (sol r3 B1; Dot on #704).
        With `release_at`, the first line starting with it once `locked` exists writes `release-wanted` and waits (at most 60 s) for `released`: the lock holder lets go there.
        Returns (runner, paused, locked). Dot on #684: the lock helper used to poll for the claim file and hope to win the race with the runner's next log line; the handshake makes the order a fact.
        r3 (sol r2b hardening; adversary r2 A5): the prefix is compared ordinally (a `[` in it is no -like wildcard) and both waits run on a monotonic Stopwatch, not wall-clock UTC."""
        paused, locked = self.case / "runner-paused.marker", self.case / "log-locked.marker"
        wanted, released = self.case / "release-wanted.marker", self.case / "log-released.marker"
        anchor = "function L([string]$Message) {\n"
        pause = self._pause_snippet(message, pause_s)
        if release_at:
            pause += (f"    if ($Message.StartsWith('{psq(release_at)}', [StringComparison]::Ordinal) -and (Test-Path -LiteralPath '{psq(locked)}') -and -not (Test-Path -LiteralPath '{psq(released)}')) {{ "
                      f"Set-Content -LiteralPath '{psq(wanted)}' -Value 'release'; $sw = [Diagnostics.Stopwatch]::StartNew(); "
                      f"while (-not (Test-Path -LiteralPath '{psq(released)}') -and $sw.Elapsed.TotalSeconds -lt 60) {{ Start-Sleep -Milliseconds 20 }} }}\n")
        return self._patched_runner(anchor, anchor + pause, *more), paused, locked

    def _lock_log_while_the_runner_is_paused(self, log: Path, paused: Path, locked: Path, give_up_s: float = 120) -> tuple[threading.Event, threading.Thread, dict]:
        """Wait (at most give_up_s, monotonic) for the runner's `paused` marker, take `log` with share mode 0, write the `locked` marker (the runner resumes only now), and hold the lock
        until the returned event is set or the runner writes `release-wanted` (then the lock is let go and `log-released.marker` written). Windows only (CreateFileW). Returns
        (done, thread, state); state["outcome"] is locked / lock-failed (`locked` still written, so the runner never waits out its 60 s) / never-paused / cancelled, and
        state["released"] says whether the runner asked for the release. r3 (sol r2b B2): a wait that expired or was cancelled before `paused` appeared takes NO lock and writes NO
        `locked`: the caller fails on _assert_handshake's named message instead of a log locked ahead of the pause; a pause the runner already gave up on (its `expired` marker) takes no
        lock either (outcome pause-expired). The handle is closed in a finally."""
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        done = threading.Event()
        state: dict = {"outcome": "pending", "released": False, "give_up_s": give_up_s}
        wanted, released = paused.parent / "release-wanted.marker", paused.parent / "log-released.marker"
        expired, ack = paused.parent / self._EXPIRED_MARKER, paused.parent / self._ACK_MARKER
        state["markers"] = {"paused": paused, "expired": expired, "ack": ack}
        invalid = wintypes.HANDLE(-1).value

        def hold() -> None:
            # the decision is the loop's own observation (adversary r3 A1): a marker that appears only after the wait expired or was cancelled takes no lock either
            end, seen = time.monotonic() + give_up_s, False
            while not done.is_set() and time.monotonic() < end:
                if paused.exists():
                    seen = not done.is_set() and time.monotonic() < end   # adversary r3 pass 2 A2: a look that returned after the deadline does not count
                    break
                done.wait(0.01)
            if seen and expired.exists():   # the runner's own pause ran out and it carried on: a surviving `paused` marker is no handshake (sol r3 B1)
                state["outcome"] = "pause-expired"
                return
            if not seen:
                state["outcome"] = "cancelled" if done.is_set() else "never-paused"
                return
            h = k32.CreateFileW(str(log), 0x80000000 | 0x40000000, 0, None, 3, 0x80, None)   # GENERIC_READ|GENERIC_WRITE, share none, OPEN_EXISTING
            try:
                state["outcome"] = "locked" if h != invalid else "lock-failed"
                locked.write_text(state["outcome"], encoding="utf-8")
                if h != invalid:
                    end = time.monotonic() + give_up_s
                    while not done.is_set() and not wanted.exists() and time.monotonic() < end:
                        done.wait(0.01)
                    state["released"] = wanted.exists()
            finally:
                if h != invalid:
                    k32.CloseHandle(h)
            if state["released"]:
                released.write_text("released", encoding="utf-8")

        t = threading.Thread(target=hold, daemon=True)
        t.start()
        return done, t, state

    def _assert_handshake(self, state: dict, message: str, r: subprocess.CompletedProcess | None = None) -> None:
        """r3 (sol r2b B2): the lock was taken only after the runner paused at `message`; a runner that never paused (slow start, early death) fails here by name."""
        out = "" if r is None else "\n" + r.stdout + r.stderr
        self.assertNotEqual(state["outcome"], "never-paused", f"the runner never paused at {message!r} within {state['give_up_s']} s: the lock was not taken{out}")
        self.assertNotEqual(state["outcome"], "cancelled", f"the runner ended before it paused at {message!r}: the lock was not taken{out}")
        self.assertNotEqual(state["outcome"], "pause-expired", f"the runner's pause at {message!r} expired with no lock: the surviving pause marker is no handshake{out}")
        self.assertEqual(state["outcome"], "locked", f"the handshake: the runner paused at {message!r} and resumed only once the lock was held{out}")
        # sol r3 B1 / Dot on #704: the runner must itself have seen the lock WHILE still paused. Its `expired` marker (the pause ran out first; a helper delayed past it still locks) refuses the
        # attempt; so does an `ack` that is missing or carries another attempt's nonce (the runner writes `<nonce> held` only after probing batch.log and finding it held).
        mk = state["markers"]
        self.assertFalse(mk["expired"].exists(), f"the runner's pause at {message!r} expired before the lock was taken: no handshake{out}")
        nonce = mk["paused"].read_text(encoding="utf-8").strip() if mk["paused"].exists() else ""
        got = mk["ack"].read_text(encoding="utf-8").strip() if mk["ack"].exists() else None
        self.assertTrue(nonce and got == f"{nonce} held", f"no attempt-specific acknowledgment from the paused runner at {message!r} (want {nonce!r} + ' held', got {got!r}){out}")

    def test_r6_b5_a_batch_log_another_process_holds_after_the_claim_cannot_skip_the_release_the_receipt_or_the_summary(self) -> None:
        # Dot r5 B5: the finally logged CLAIM_RENEW before Release-VenueClaim; with batch.log held exclusively after the claim was taken, that log call threw out of the
        # finally, so the claim stayed, no receipt or summary was written and the typed exit 12 was lost (an unhandled exit 1). Every mandatory step runs, logging or not.
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "batch.log").write_text("", encoding="utf-8")
        args, e = self.plan_cmd(self.ab_block_plan(), "-PlanOrder", "Interleave")
        runner, paused, locked = self._runner_paused_at_claim_taken()
        args[args.index("-File") + 1] = str(runner)
        done, t, state = self._lock_log_while_the_runner_is_paused(self.run_dir / "batch.log", paused, locked)
        t0 = time.monotonic()
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=90, env=e)
        except subprocess.TimeoutExpired as ex:
            self.fail(f"the process did not end within 90 s: {ex.stdout!r}")
        finally:
            elapsed = time.monotonic() - t0
            done.set()
            t.join(5)
        self._assert_handshake(state, "CLAIM_TAKEN", r)
        self.assertLess(elapsed, 60, "bounded: the lock must not stretch the run")
        self.assertEqual(r.returncode, 12, r.stdout + r.stderr)
        self.assertFalse(self.claim_path.exists(), "the claim was left behind")
        self.assertEqual(self.kinds(), [], "no open was submitted")
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"]), (12, "BATCH_ERROR"))
        self.assertEqual(res["ran"], 0)
        [b] = self.batches()
        self.assertEqual(b["endReason"], "BATCH_ERROR")
        self.assertGreaterEqual(b["logWriteFailures"], 1, "the swallowed logging failures are recorded in the receipt")
        self.assertIn("BATCH_LOG_FAILED", r.stderr)

    def test_r6_b5_sibling_a_log_failure_right_after_a_stale_claim_is_broken_does_not_leave_the_new_claim_behind(self) -> None:
        # B5's class: Request-VenueClaim had granted the claim when CLAIM_STALE_BROKEN was logged, and that line sat outside the try, so a log failure there left the new
        # claim (and no keeper yet) behind with no release. The line is best effort now; the batch carries on and ends normally. The failure is injected into a copy of the runner.
        self.claims.mkdir(parents=True, exist_ok=True)
        seed = (f"Import-Module '{psq(MODULE)}' -Force -DisableNameChecking\n"
                f"$r = Request-VenueClaim -Venue bachelor -RunDir 'C:\\dead-run' -Purpose dead -TtlSec 3600 -ClaimDir '{psq(self.claims)}' -OwnerPid {os.getpid()}\n"
                "$r.granted\n")
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", seed], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.stdout.strip(), "True", out.stdout + out.stderr)
        # the owner is gone (a pid no process has): the next claim request breaks it as dead-pid, the way the STEAL fake leaves a foreign claim
        self.claim_path.write_text(re.sub(r'"ownerPid":\s*\d+', '"ownerPid":99999999', self.claim_path.read_text(encoding="utf-8")), encoding="utf-8")
        anchor = "function L([string]$Message) {\n"
        runner = self._patched_runner(anchor, anchor + "    if ($Message -like 'CLAIM_STALE_BROKEN*') { throw 'simulated log failure' }\n")
        args, e = self.plan_cmd(self.ab_block_plan(), "-PlanOrder", "Interleave")
        args[args.index("-File") + 1] = str(runner)
        r = subprocess.run(args, capture_output=True, text=True, timeout=120, env=e)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(self.claim_path.exists(), "the claim was left behind")
        res = batch_result(r.stdout)
        self.assertEqual((res["ran"], res["notRun"]), (4, 0))
        self.assertIn("BATCH_LOG_FAILED", r.stderr)

    def _patched_runner(self, old: str, new: str, *more: tuple[str, str]) -> Path:
        """A copy of the dual-venue tree whose runner has `old` (found exactly once) replaced by `new`, and each further (old, new) pair in `more` likewise; returns the copy's runner."""
        tree = self.case / "tree" / "tools" / "profiling"
        shutil.copytree(DV, tree / "dual-venue", ignore=shutil.ignore_patterns("look-receipts", "legs", "legsets"))
        (tree / "bachelor").mkdir()
        shutil.copy2(AGENT, tree / "bachelor" / AGENT.name)
        runner = tree / "dual-venue" / RUNNER.name
        text = runner.read_text(encoding="utf-8")
        for o, n in ((old, new), *more):
            self.assertEqual(text.count(o), 1, "the runner line moved: update this fixture")
            text = text.replace(o, n)
        runner.write_text(text, encoding="utf-8")
        return runner

    def test_r6_adv_a3_a_result_line_that_cannot_be_appended_to_the_log_is_still_said_on_stderr(self) -> None:
        # adversary r6 A3: the final LEASE_BATCH_RESULT append failed inside an empty catch, so no BATCH_LOG_FAILED diagnostic was ever emitted for it. The run still ends
        # 0 with the result on stdout; the failure is injected into a copy of the runner (the real cause is a batch.log another process holds at that instant).
        anchor = "try { Add-Content -LiteralPath $batchLog -Value $resultLine }"
        runner = self._patched_runner(anchor, "try { throw 'simulated result-line append failure'; Add-Content -LiteralPath $batchLog -Value $resultLine }")
        args, e = self.plan_cmd(self.ab_block_plan(), "-PlanOrder", "Interleave")
        args[args.index("-File") + 1] = str(runner)
        r = subprocess.run(args, capture_output=True, text=True, timeout=120, env=e)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(batch_result(r.stdout)["ran"], 4)
        self.assertIn("BATCH_LOG_FAILED the batch log cannot be written (simulated result-line append failure)", r.stderr)

    def test_r7_sol_a_final_result_append_that_fails_is_counted_in_the_logwritefailures_the_result_line_reports(self) -> None:
        # sol r6b blocker: logWriteFailures was serialized into the result line BEFORE the final append was attempted, so an IOException on that append left the live
        # counter at 1 and the stdout summary at 0 (exit 0, ran 4). The count the summary reports includes the append that just failed.
        anchor = "try { Add-Content -LiteralPath $batchLog -Value $resultLine }"
        runner = self._patched_runner(anchor, "try { throw [IO.IOException]::new('locked'); Add-Content -LiteralPath $batchLog -Value $resultLine }")
        args, e = self.plan_cmd(self.ab_block_plan(), "-PlanOrder", "Interleave")
        args[args.index("-File") + 1] = str(runner)
        r = subprocess.run(args, capture_output=True, text=True, timeout=120, env=e)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual(res["ran"], 4)
        self.assertEqual(res["logWriteFailures"], 1, "the failed final append is in the count the result line reports")
        self.assertIn("BATCH_LOG_FAILED the batch log cannot be written (locked)", r.stderr)

    def test_r7_sol_a_batch_end_log_failure_is_counted_in_the_batch_receipt_and_the_summary(self) -> None:
        # sol r6b sibling: the batch receipt was written BEFORE the BATCH_END line was logged, so a failure logging BATCH_END was missing from the receipt's logWriteFailures
        # (and the receipt claimed 0 failures for a batch that had one). The receipt and the summary both carry it.
        anchor = "function L([string]$Message) {\n"
        runner = self._patched_runner(anchor, anchor + "    if ($Message -like 'BATCH_END*') { throw [IO.IOException]::new('locked') }\n")
        args, e = self.plan_cmd(self.ab_block_plan(), "-PlanOrder", "Interleave")
        args[args.index("-File") + 1] = str(runner)
        r = subprocess.run(args, capture_output=True, text=True, timeout=120, env=e)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual(res["ran"], 4)
        [b] = self.batches()
        self.assertEqual(b["logWriteFailures"], 1, "the BATCH_END failure is in the receipt written for that batch")
        self.assertEqual(res["logWriteFailures"], 1)
        self.assertIn("BATCH_END", r.stdout, "the line still reaches stdout")
        self.assertIn("BATCH_LOG_FAILED the batch log cannot be written (locked)", r.stderr)

    def test_r7_sol_b_the_pre_claim_deadline_batch_end_failure_is_counted_in_the_receipt_written_for_that_batch(self) -> None:
        # sol r6b sibling class: the two pre-claim DEADLINE exits wrote the batch receipt BEFORE logging BATCH_END (and never carried logWriteFailures at all), so a
        # failure logging BATCH_END was missing from the receipt, and an unprotected L threw out of the batch. The receipt carries the count after the line is tried.
        self.claims.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        rows = [{"venue": "bachelor", "leaseId": "old-1", "startUtc": utc_iso(now - timedelta(minutes=105)), "endUtc": utc_iso(now - timedelta(minutes=60))},
                {"venue": "bachelor", "leaseId": "old-2", "startUtc": utc_iso(now - timedelta(minutes=45)), "endUtc": utc_iso(now - timedelta(minutes=1))}]
        (self.claims / "bachelor.lease-ledger.jsonl").write_text("".join(json.dumps(x) + "\n" for x in rows), encoding="utf-8")
        anchor = "function L([string]$Message) {\n"
        runner = self._patched_runner(anchor, anchor + "    if ($Message -like 'BATCH_END*') { throw [IO.IOException]::new('locked') }\n")
        args, e = self.plan_cmd(self.ab_block_plan(), "-PlanOrder", "Interleave")
        args[args.index("-File") + 1] = str(runner)
        r = subprocess.run(args, capture_output=True, text=True, timeout=120, env=e)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["endReason"], b["preOpenWait"]["reason"]), ("DEADLINE", "BUDGET"))
        self.assertEqual(b["logWriteFailures"], 1, "the BATCH_END failure is in the receipt written for that batch")
        self.assertEqual(batch_result(r.stdout)["logWriteFailures"], 1)

    # LEASE-BATCH-RECEIPT-PERSIST-FAILURE-REPORTING-1 (Dot B6 on #684, DISTINGUISHed to this card): a receipt file the run cannot write (batches.jsonl, batch-NN.json or leg-plan.json
    # occupied by a DIRECTORY) used to throw out of Write-BatchReceipt, which sits in the batch's finally (or, pre-claim, outside any try): the summary was skipped and the exit was
    # an unhandled 1 instead of the typed code the outcome had selected. The error evidence is kept, said once as RECEIPT_PERSIST_FAILED, and the typed code is still the exit.
    def _occupy_with_directory(self, *names: str) -> None:
        for n in names:
            (self.run_dir / n).mkdir(parents=True, exist_ok=True)

    def _assert_persist_failure_said(self, r: subprocess.CompletedProcess, res: dict, *names: str) -> None:
        for n in names:
            want = f"RECEIPT_PERSIST_FAILED file={self.run_dir / n} reason="
            self.assertIn(want, r.stdout, f"the typed line for {n}: {r.stdout}")
            self.assertTrue(any(f["file"] == str(self.run_dir / n) and f["reason"] for f in res["receiptPersistFailures"]), res.get("receiptPersistFailures"))
        self.assertEqual(len(res["receiptPersistFailures"]), len(names), res["receiptPersistFailures"])
        self.assertFalse(self.claim_path.exists(), "the claim was left behind")

    def test_persist_a_post_claim_batch_error_with_batches_jsonl_a_directory_still_emits_the_summary_and_exits_12(self) -> None:
        # leg-plan.json is a directory: the plan write after the claim throws (BATCH_ERROR, exit 12). batches.jsonl is a directory too, so the receipt append in the finally threw
        # out of it: no summary, exit 1. Now: the summary names the unwritten receipt, the in-memory results are kept, and the exit is the BATCH_ERROR 12.
        self._occupy_with_directory("leg-plan.json", "batches.jsonl")
        r = self.run_plan(self.ab_block_plan(), "-PlanOrder", "Interleave")
        self.assertEqual(r.returncode, 12, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["notRun"]), (12, "BATCH_ERROR", 0, 4))
        self._assert_persist_failure_said(r, res, "batches.jsonl")
        self.assertEqual([x["endReason"] for x in res["batches"]], ["BATCH_ERROR"], "the in-memory batch row is kept")
        self.assertTrue((self.run_dir / "batch-01.json").is_file(), "the receipt that could be written was written")

    def test_persist_a_post_claim_batch_error_with_only_leg_plan_json_a_directory_is_still_exit_12_with_a_summary(self) -> None:
        # the sibling that already held: leg-plan.json alone is the BATCH_ERROR trigger and every receipt writes, so nothing is reported as a persist failure
        self._occupy_with_directory("leg-plan.json")
        r = self.run_plan(self.ab_block_plan(), "-PlanOrder", "Interleave")
        self.assertEqual(r.returncode, 12, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"]), (12, "BATCH_ERROR"))
        self.assertEqual(res["receiptPersistFailures"], [])
        [b] = self.batches()
        self.assertEqual(b["endReason"], "BATCH_ERROR")

    def test_persist_a_pre_claim_deadline_with_batches_jsonl_and_leg_plan_json_directories_still_emits_the_summary_and_exits_3(self) -> None:
        # the pre-claim DEADLINE exit (the 3 h budget is spent) wrote its receipt outside any try: an unwritable batches.jsonl threw out of the script. leg-plan.json as a directory is
        # never touched on this path (it is written once the claim is held) and must change nothing.
        self.claims.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        rows = [{"venue": "bachelor", "leaseId": "old-1", "startUtc": utc_iso(now - timedelta(minutes=105)), "endUtc": utc_iso(now - timedelta(minutes=60))},
                {"venue": "bachelor", "leaseId": "old-2", "startUtc": utc_iso(now - timedelta(minutes=45)), "endUtc": utc_iso(now - timedelta(minutes=1))}]
        (self.claims / "bachelor.lease-ledger.jsonl").write_text("".join(json.dumps(x) + "\n" for x in rows), encoding="utf-8")
        self._occupy_with_directory("leg-plan.json", "batches.jsonl")
        r = self.run_plan(self.ab_block_plan(), "-PlanOrder", "Interleave")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["notRun"]), (3, "DEADLINE", 0, 4))
        self._assert_persist_failure_said(r, res, "batches.jsonl")
        self.assertEqual(self.kinds(), [], "no claim, no open")

    def test_persist_an_otherwise_successful_batch_whose_receipt_cannot_be_written_exits_13_with_the_summary(self) -> None:
        # nothing else went wrong: every leg ran. The receipt is not on disk, so the run must not claim success: exit 13, the legs still reported as RAN in the summary.
        self._occupy_with_directory("batches.jsonl")
        r = self.run_plan(self.ab_block_plan(), "-PlanOrder", "Interleave")
        self.assertEqual(r.returncode, 13, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["counted"], res["notRun"]), (13, "RECEIPT_PERSIST_FAILED", 4, 4, 0))
        self._assert_persist_failure_said(r, res, "batches.jsonl")
        self.assertEqual([x["endReason"] for x in res["batches"]], ["ALL_LEGS_DONE"])
        self.assertTrue((self.run_dir / "batch-01.json").is_file())

    def test_persist_a_batch_receipt_file_that_is_a_directory_is_reported_and_the_jsonl_line_is_still_written(self) -> None:
        # the two receipt writes are independent: batch-01.json failing must not skip the batches.jsonl line (the other copy of the same record)
        self._occupy_with_directory("batch-01.json")
        r = self.run_plan(self.ab_block_plan(), "-PlanOrder", "Interleave")
        self.assertEqual(r.returncode, 13, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["ran"]), (13, 4))
        self._assert_persist_failure_said(r, res, "batch-01.json")
        [b] = self.batches()
        self.assertEqual(b["endReason"], "ALL_LEGS_DONE")

    def test_persist_adv_a1_a_log_failure_in_the_persist_diagnostic_is_counted_in_the_receipt_written_after_it(self) -> None:
        # adversary r1 A1: the batch snapshots logWriteFailures before Write-BatchReceipt; the RECEIPT_PERSIST_FAILED line for batch-01.json (a directory) then failed to log (batch.log held),
        # which bumped the live counter, and the batches.jsonl line was serialized from the stale snapshot (0) while the summary said more. Each receipt is serialized with the live count.
        self._occupy_with_directory("batch-01.json")
        (self.run_dir / "batch.log").write_text("", encoding="utf-8")
        args, e = self.plan_cmd(self.ab_block_plan(), "-PlanOrder", "Interleave")
        runner, paused, locked = self._runner_paused_at_claim_taken("RECEIPT_PERSIST_FAILED")
        args[args.index("-File") + 1] = str(runner)
        done, t, state = self._lock_log_while_the_runner_is_paused(self.run_dir / "batch.log", paused, locked)
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=90, env=e)
        except subprocess.TimeoutExpired as ex:
            self.fail(f"the process did not end within 90 s: {ex.stdout!r}")
        finally:
            done.set()
            t.join(5)
        self._assert_handshake(state, "RECEIPT_PERSIST_FAILED", r)
        self.assertEqual(r.returncode, 13, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertGreaterEqual(res["logWriteFailures"], 1)
        [b] = self.batches()
        self.assertGreaterEqual(b["logWriteFailures"], 1, "the surviving batches.jsonl line carries the failure the diagnostic itself hit")

    def test_persist_a_leg_failure_keeps_its_exit_7_when_the_receipt_cannot_be_written_either(self) -> None:
        # 13 replaces only a clean 0: a run that already selected a failing code keeps it, and the summary still names the unwritten receipt
        self._occupy_with_directory("batches.jsonl")
        r = self.run_plan(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]), FAKE_LEG_MODES="exit3")
        self.assertEqual(r.returncode, 7, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["failed"]), (7, None, 1))
        self._assert_persist_failure_said(r, res, "batches.jsonl")

    def test_persist_a_clean_run_reports_no_persist_failure(self) -> None:
        r = self.run_plan(self.ab_block_plan(), "-PlanOrder", "Interleave")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["receiptPersistFailures"], res["logWriteFailures"]), (0, [], 0))
        self.assertNotIn("RECEIPT_PERSIST_FAILED", r.stdout)

    # r2 (Dot advisory on #704): the generated-runner pause helper embedded its paths unescaped in a PowerShell single-quoted literal (an apostrophe in the temp dir broke the runner),
    # and two receipt-write paths had no test: the batch-NN.json rewrite when only the diagnostic's own log write fails, and both receipt destinations failing at once.
    def _run_with_log_held_at(self, args: list[str], e: dict, message: str, release_at: str = "", give_up_s: float = 120, more: tuple = ()) -> tuple[subprocess.CompletedProcess, dict]:
        """Run `args` on a runner copy that pauses at the first log line starting `message`, with batch.log held exclusively from that moment (until the first line starting `release_at`
        once held, else until the run ends); `more` is further (old, new) runner patches. Returns (result, the lock helper's state); the caller asserts it with _assert_handshake."""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "batch.log").write_text("", encoding="utf-8")
        runner, paused, locked = self._runner_paused_at_claim_taken(message, release_at, more)
        args[args.index("-File") + 1] = str(runner)
        done, t, state = self._lock_log_while_the_runner_is_paused(self.run_dir / "batch.log", paused, locked, give_up_s)
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=90, env=e)
        except subprocess.TimeoutExpired as ex:
            self.fail(f"the process did not end within 90 s: {ex.stdout!r}")
        finally:
            done.set()
            t.join(5)
        return r, state

    def _run_plan_with_log_held_at(self, message: str, give_up_s: float = 120) -> tuple[subprocess.CompletedProcess, dict]:
        """The A/B plan under _run_with_log_held_at."""
        args, e = self.plan_cmd(self.ab_block_plan(), "-PlanOrder", "Interleave")
        return self._run_with_log_held_at(args, e, message, give_up_s=give_up_s)

    def test_r2_nit_a_case_dir_with_an_apostrophe_does_not_break_the_generated_runner_or_its_pause_markers(self) -> None:
        # Dot nit (source-confirmed at 1a880fee): `-LiteralPath '{paused}'` and `'{locked}'` were embedded unescaped, so a temp dir such as C:\...\case-o'brien-x made the generated runner
        # unparseable (the process died before it paused). Every path embedded in a generated PowerShell literal now goes through psq (the quote doubled).
        self._init_case(Path(tempfile.mkdtemp(prefix="case-o'brien-", dir=self.tmp)))
        self._stage_case()
        self.assertIn("'", str(self.case))
        self.assertEqual(psq("a'b''c"), "a''b''''c")
        # r3 (sol r2b B2): no short give-up any more; a slow start is waited out (the run's own 90 s bound ends the wait), and a runner that never pauses fails by name
        r, state = self._run_plan_with_log_held_at("CLAIM_TAKEN")
        self._assert_handshake(state, "CLAIM_TAKEN", r)
        self.assertEqual(r.returncode, 12, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"]), (12, "BATCH_ERROR"))
        self.assertFalse(self.claim_path.exists(), "the claim was left behind")

    def test_r2_adv_a1_psq_escapes_every_quote_character_powershell_treats_as_a_single_quote(self) -> None:
        # adversary r2 A1 (advisory, introduced by psq): PowerShell's tokenizer also takes U+2018, U+2019, U+201A and U+201B as single quotes, so a temp dir such as C:\temp\o<U+2019>Brien
        # ended the literal early even with the ASCII quote doubled. Each character, alone and inside a path, must parse and come back unchanged.
        samples = ["C:\\temp\\o'Brien", "C:\\temp\\o\u2018Brien", "C:\\temp\\o\u2019Brien", "C:\\temp\\o\u201aBrien", "C:\\temp\\o\u201bBrien", "\u2019\u2019", "plain"]
        lines = [f"$v = '{psq(s)}'; ($v.ToCharArray() | ForEach-Object {{ [int]$_ }}) -join ','" for s in samples]
        script = self.case / "psq-probe.ps1"
        script.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
        r = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script)], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.split(), [",".join(str(ord(c)) for c in s) for s in samples], r.stdout + r.stderr)

    def test_r2_cov_a_jsonl_append_failure_whose_diagnostic_cannot_be_logged_rewrites_batch_receipt_with_the_count_and_exits_13(self) -> None:
        # batch-01.json is written first (count 0) and succeeds; the batches.jsonl append fails (a directory); its RECEIPT_PERSIST_FAILED line cannot be logged (batch.log held), which bumps
        # logWriteFailures; so the already-written batch-01.json is rewritten with the live count (Write-BatchReceipt's second per-batch write). The typed record and exit 13 are unchanged.
        self._occupy_with_directory("batches.jsonl")
        r, state = self._run_plan_with_log_held_at("RECEIPT_PERSIST_FAILED")
        self._assert_handshake(state, "RECEIPT_PERSIST_FAILED", r)
        self.assertEqual(r.returncode, 13, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["counted"]), (13, "RECEIPT_PERSIST_FAILED", 4, 4))
        self._assert_persist_failure_said(r, res, "batches.jsonl")
        self.assertGreaterEqual(res["logWriteFailures"], 1, "the diagnostic's own log failure is counted")
        per_batch = json.loads((self.run_dir / "batch-01.json").read_text(encoding="utf-8"))
        self.assertEqual(per_batch["endReason"], "ALL_LEGS_DONE")
        self.assertGreaterEqual(per_batch["logWriteFailures"], 1, "batch-01.json was rewritten once the diagnostic's log failure was counted")
        self.assertLessEqual(per_batch["logWriteFailures"], res["logWriteFailures"], "lines logged after the rewrite (the result line) may add more, never fewer")

    def test_r2_cov_both_receipt_destinations_failing_are_both_named_and_the_clean_run_exits_13(self) -> None:
        self._occupy_with_directory("batch-01.json", "batches.jsonl")
        r = self.run_plan(self.ab_block_plan(), "-PlanOrder", "Interleave")
        self.assertEqual(r.returncode, 13, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["counted"], res["notRun"]), (13, "RECEIPT_PERSIST_FAILED", 4, 4, 0))
        self._assert_persist_failure_said(r, res, "batch-01.json", "batches.jsonl")
        self.assertEqual(sorted(Path(f["file"]).name for f in res["receiptPersistFailures"]), ["batch-01.json", "batches.jsonl"])
        self.assertEqual([x["endReason"] for x in res["batches"]], ["ALL_LEGS_DONE"], "the in-memory row is kept")

    def test_r2_cov_both_receipt_destinations_failing_keep_exit_12_for_a_batch_error(self) -> None:
        self._occupy_with_directory("leg-plan.json", "batch-01.json", "batches.jsonl")
        r = self.run_plan(self.ab_block_plan(), "-PlanOrder", "Interleave")
        self.assertEqual(r.returncode, 12, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["notRun"]), (12, "BATCH_ERROR", 0, 4))
        self._assert_persist_failure_said(r, res, "batch-01.json", "batches.jsonl")

    def test_r2_cov_both_receipt_destinations_failing_keep_exit_7_for_a_failed_leg(self) -> None:
        self._occupy_with_directory("batch-01.json", "batches.jsonl")
        r = self.run_plan(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]), FAKE_LEG_MODES="exit3")
        self.assertEqual(r.returncode, 7, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["failed"]), (7, 1))
        self._assert_persist_failure_said(r, res, "batch-01.json", "batches.jsonl")

    def test_r2_cov_both_receipt_destinations_failing_keep_exit_3_for_a_pre_claim_deadline(self) -> None:
        self.claims.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        rows = [{"venue": "bachelor", "leaseId": "old-1", "startUtc": utc_iso(now - timedelta(minutes=105)), "endUtc": utc_iso(now - timedelta(minutes=60))},
                {"venue": "bachelor", "leaseId": "old-2", "startUtc": utc_iso(now - timedelta(minutes=45)), "endUtc": utc_iso(now - timedelta(minutes=1))}]
        (self.claims / "bachelor.lease-ledger.jsonl").write_text("".join(json.dumps(x) + "\n" for x in rows), encoding="utf-8")
        self._occupy_with_directory("batch-01.json", "batches.jsonl")
        r = self.run_plan(self.ab_block_plan(), "-PlanOrder", "Interleave")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["notRun"]), (3, "DEADLINE", 0, 4))
        self._assert_persist_failure_said(r, res, "batch-01.json", "batches.jsonl")

    def test_r2_cov_both_receipt_destinations_failing_keep_exit_11_for_an_unreconciled_open(self) -> None:
        self._occupy_with_directory("batch-01.json", "batches.jsonl")
        deadline = utc_iso(datetime.now(timezone.utc) + timedelta(seconds=12))
        r = self.run_batch([self.specs[0]], "-DeadlineUtc", deadline, "-CancelReconcileSec", "3", FAKE_OPEN_MODES="gatehold")
        self.assertEqual(r.returncode, 11, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"]), (11, "OPEN_CANCEL_UNRECONCILED"))
        self._assert_persist_failure_said(r, res, "batch-01.json", "batches.jsonl")

    # r3 (sol r2b B1 = catalogue RPF-01): the receipt recovery returned, but the NEXT batch's BATCH_WAIT went through the throwing L outside the batch try, so a batch.log still held
    # after the first RECEIPT_PERSIST_FAILED ended the script with an untyped exit 1 and no LEASE_BATCH_RESULT. This is a FULL-RUNNER run (the real runner, a copy that only adds the
    # pause/release handshake to L), not an extracted replay: two batches (the first lease is too short for the second leg), a 4 s cooldown between them, batches.jsonl a directory,
    # batch.log held from batch 1's RECEIPT_PERSIST_FAILED through batch 2's BATCH_WAIT and let go at batch 2's CLAIM_TAKEN.
    def test_r3_b1_receipt_recovery_survives_a_later_batch_wait_whose_log_line_cannot_be_written(self) -> None:
        self._occupy_with_directory("batches.jsonl")
        args, e = self.batch_cmd(self.specs[:2], "-LeaseCooldownSec", "4", FAKE_LEASE_REMAIN="70,2700", FAKE_LEG_REMAIN="50,2600")
        r, state = self._run_with_log_held_at(args, e, "RECEIPT_PERSIST_FAILED", release_at="CLAIM_TAKEN batch=2 ")
        self._assert_handshake(state, "RECEIPT_PERSIST_FAILED", r)
        self.assertTrue(state["released"], "the lock was held through BATCH_WAIT and let go only at batch 2's CLAIM_TAKEN\n" + r.stdout + r.stderr)
        self.assertEqual(r.returncode, 13, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["notRun"]), (13, "RECEIPT_PERSIST_FAILED", 2, 0))
        self.assertEqual([(f["batch"], Path(f["file"]).name) for f in res["receiptPersistFailures"]], [(1, "batches.jsonl"), (2, "batches.jsonl")])
        self.assertEqual([x["endReason"] for x in res["batches"]], ["LEASE_REMAINING_SHORT", "ALL_LEGS_DONE"])
        self.assertIn(" BATCH_WAIT batch=2 reason=COOLDOWN", r.stdout, "the wait line still reaches stdout")
        log = (self.run_dir / "batch.log").read_text(encoding="utf-8")
        self.assertNotIn(" BATCH_WAIT batch=2 ", log, "the wait line was tried while batch.log was held (the case under test)")
        self.assertIn(" CLAIM_TAKEN batch=2 ", log)
        self.assertGreaterEqual(res["logWriteFailures"], 2, "the persist diagnostic and the wait line both failed to log")
        self.assertIn("BATCH_LOG_FAILED", r.stderr)
        self.assertFalse(self.claim_path.exists(), "the claim was left behind")

    def test_r3_b1_class_no_direct_log_call_sits_between_batches(self) -> None:
        # the class of B1 (siblings CLAIM_LAPSED_IN_GUARD and CLAIM_HELD): between the top of the batch loop and the batch try, a log line is best effort (LogBest), never the throwing L
        text = RUNNER.read_text(encoding="utf-8")
        start = text.index("while ($pending.Count -gt 0 -and $null -eq $stopReason) {")
        between = text[start:text.index('    try {\n        L "CLAIM_TAKEN', start)]
        self.assertEqual(re.findall(r'(?<![\w-])L "[A-Z_]+', between), [], "a direct L call outside the batch try throws out of the script on a held batch.log")
        self.assertIn('LogBest "BATCH_WAIT batch=', between)

    def _two_batch_plan(self, inject: str, occupy: tuple = ("batches.jsonl",), extra: tuple = ()) -> tuple[list[str], dict, tuple[str, str]]:
        """A two-leg plan whose first lease holds one leg (two batches), batches.jsonl a directory: (args, env, the runner patch that runs `inject` (PowerShell) in batch 2 just before
        the claim-time input guard)."""
        self._occupy_with_directory(*occupy)
        spec2 = self.case / "fx-b-copy.json"
        spec2.write_text(self.specs[1].read_text(encoding="utf-8"), encoding="utf-8")
        plan = self.write_plan([self.item(self.specs[0], "A", self.commit_a), self.item(spec2, "A", self.commit_a)])
        # the claim-time guard (r12, #705 merge: bounded by Invoke-InputGuard, found once); independent of what follows it, so the same fixture runs on a runner without the sticky hit
        anchor = "        try { $hit = Invoke-InputGuard }\n"
        args, e = self.plan_cmd(plan, *extra, FAKE_LEASE_REMAIN="70,2700", FAKE_LEG_REMAIN="50,2600")
        return args, e, (anchor, f"        if ($batchNo -eq 2) {{ {inject} }}\n" + anchor)

    def _two_batch_plan_refused_at_batch_2(self, inject: str) -> tuple[subprocess.CompletedProcess, Path]:
        args, e, patch = self._two_batch_plan(inject)
        args[args.index("-File") + 1] = str(self._patched_runner(*patch))
        return subprocess.run(args, capture_output=True, text=True, timeout=120, env=e), self.case / "fx-b-copy.json"

    def test_r3_adv_a1_a_refusal_line_that_cannot_be_logged_is_in_the_count_the_summary_reports(self) -> None:
        # adversary r3 pass 2 A1: the summary was printed BEFORE Stop-PlanRefused tried its own log line, and that append's failure was swallowed uncounted, so the authoritative
        # result under-reported logWriteFailures. batch.log is held from batch 1's RECEIPT_PERSIST_FAILED to the end: that diagnostic, the refusal line, the NOT_RUN line and the
        # result line all fail, and the summary, now the last thing written, counts all four.
        args, e, patch = self._two_batch_plan("Set-Content -LiteralPath '{}' -Value '{{}}'".format(psq(self.case / "fx-b-copy.json")))
        r, state = self._run_with_log_held_at(args, e, "RECEIPT_PERSIST_FAILED", more=(patch,))
        self._assert_handshake(state, "RECEIPT_PERSIST_FAILED", r)
        m = re.search(r"LEG_PLAN_REFUSED reason=(\S+) item=2 ", r.stderr)
        self.assertIsNotNone(m, r.stdout + r.stderr)
        self._assert_refused_at_batch_2_with_the_summary(r, m.group(1))
        self.assertEqual(batch_result(r.stdout)["logWriteFailures"], 4, "the diagnostic, the refusal line, the NOT_RUN line and the result line\n" + r.stdout + r.stderr)
        self.assertNotIn("LEG_PLAN_REFUSED", (self.run_dir / "batch.log").read_text(encoding="utf-8"), "the refusal line was tried while batch.log was held (the case under test)")

    def _assert_refused_at_batch_2_with_the_summary(self, r: subprocess.CompletedProcess, reason: str) -> None:
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn(f"LEG_PLAN_REFUSED reason={reason} item=2 ", r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["notRun"]), (2, reason, 1, 1))
        self.assertEqual([(f["batch"], Path(f["file"]).name) for f in res["receiptPersistFailures"]], [(1, "batches.jsonl")], "batch 1's lost receipt is summarized")
        self.assertEqual([x["endReason"] for x in res["batches"]], ["LEASE_REMAINING_SHORT"])
        self.assertEqual(len([k for k in self.kinds() if k.startswith("OPEN:")]), 1, "no lease is opened for batch 2")
        self.assertFalse(self.claim_path.exists(), "the claim was left behind")

    def test_r3_h3_an_input_refused_between_batches_after_a_persist_failure_still_prints_the_summary(self) -> None:
        # opus r2b H3: Stop-InputIsOutput (exit 2) after batch 1's receipt could not be written printed no LEASE_BATCH_RESULT, so receiptPersistFailures was never summarized.
        # A content refusal: the second leg's spec stops being a valid leg spec while batch 1 ran.
        r, spec2 = self._two_batch_plan_refused_at_batch_2("Set-Content -LiteralPath '{}' -Value '{{}}'".format(psq(self.case / "fx-b-copy.json")))
        m = re.search(r"LEG_PLAN_REFUSED reason=(\S+) item=2 ", r.stderr)
        self.assertIsNotNone(m, r.stdout + r.stderr)
        self._assert_refused_at_batch_2_with_the_summary(r, m.group(1))

    def test_r3_h3_an_input_that_became_a_run_output_between_batches_prints_the_summary_and_writes_nothing_into_the_run_dir(self) -> None:
        # the location refusal: the second leg's spec is now a hard link to batch-01.json. The summary is printed, and (Test-OpenOutputAlias's rule) nothing more is written into
        # -RunDir: batch.log and the linked receipt are byte-for-byte what they were when the guard ran.
        snap_log, snap_rec = self.case / "batch.log.snap", self.case / "batch-01.json.snap"
        spec2 = psq(self.case / "fx-b-copy.json")
        inject = (f"[IO.File]::Delete('{spec2}'); New-Item -ItemType HardLink -Path '{spec2}' -Value (Join-Path $RunDir 'batch-01.json') | Out-Null; "
                  f"Copy-Item -LiteralPath $batchLog -Destination '{psq(snap_log)}'; Copy-Item -LiteralPath (Join-Path $RunDir 'batch-01.json') -Destination '{psq(snap_rec)}'")
        r, _ = self._two_batch_plan_refused_at_batch_2(inject)
        self._assert_refused_at_batch_2_with_the_summary(r, "LEG_SPEC_IS_RUN_OUTPUT")
        self.assertEqual((self.run_dir / "batch.log").read_bytes(), snap_log.read_bytes(), "nothing was appended to batch.log after the alias was found")
        self.assertEqual((self.run_dir / "batch-01.json").read_bytes(), snap_rec.read_bytes(), "the linked input is byte-unchanged")

    def test_r4_adv_a1_the_batch_receipt_rewrite_does_not_overwrite_an_input_that_became_a_link_to_it_during_the_diagnostic(self) -> None:
        # adversary r4 A1: batch-01.json is rewritten when the batches.jsonl diagnostic's own log failure bumped logWriteFailures. The diagnostic is the window: the next leg's spec becomes
        # a hard link to batch-01.json there, after every alias guard has run, and the unconditional rewrite then overwrote the accepted input. The guard now runs right before the rewrite.
        # The diagnostic's log line is made to fail (counted) and to link the spec first, inside a copy of the runner; the linked receipt must stay what the first write left.
        snap = self.case / "batch-01.json.snap"
        spec2 = psq(self.case / "fx-b-copy.json")
        inject = (f"if (-not $script:LinkDone -and $Message.StartsWith('RECEIPT_PERSIST_FAILED', [StringComparison]::Ordinal)) {{ $script:LinkDone = $true; [IO.File]::Delete('{spec2}'); "
                  f"New-Item -ItemType HardLink -Path '{spec2}' -Value (Join-Path $RunDir 'batch-01.json') | Out-Null; Copy-Item -LiteralPath (Join-Path $RunDir 'batch-01.json') -Destination '{psq(snap)}'; "
                  "throw [IO.IOException]::new('locked') }")
        args, e, _ = self._two_batch_plan("")
        anchor_l = "function L([string]$Message) {\n"
        args[args.index("-File") + 1] = str(self._patched_runner(anchor_l, anchor_l + "    " + inject + "\n"))
        r = subprocess.run(args, capture_output=True, text=True, timeout=120, env=e)
        self.assertTrue(snap.exists(), "the link was made inside the diagnostic (the case under test)\n" + r.stdout + r.stderr)
        self.assertEqual((self.run_dir / "batch-01.json").read_bytes(), snap.read_bytes(), "the receipt rewrite went through the new link and overwrote the accepted input\n" + r.stdout + r.stderr)
        self.assertIn("BATCH_RECEIPT_WRITE_SKIPPED batch=1 ", r.stderr)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_SPEC_IS_RUN_OUTPUT", r.stderr + r.stdout)

    def _link_spec2_to_a_receipt_inside_the_first_diagnostic(self, target: str, occupy: tuple, restore: bool = False, extra: tuple = ()) -> tuple[subprocess.CompletedProcess, Path, Path]:
        """Two batches (batch 1 ran one leg); `occupy` receipt files are directories. The first RECEIPT_PERSIST_FAILED line, inside a copy of the runner, makes the second leg's spec a hard link to
        RunDir\\<target> (snapshotting the receipt `target` first when it exists); with `restore`, batch 2's claim guard first unlinks it and puts the original spec bytes back.
        Returns (result, the spec, the snapshot of the spec's original bytes)."""
        spec2, original = self.case / "fx-b-copy.json", self.case / "fx-b-copy.json.orig"
        ps2 = psq(spec2)
        args, e, (guard_old, guard_new) = self._two_batch_plan("", occupy, extra)
        original.write_bytes(spec2.read_bytes())
        # a receipt that does not exist yet (batches.jsonl, batch-02.json) is created AS the link to the spec; one that exists (batch-01.json) is linked from the spec's path
        make = (f"New-Item -ItemType HardLink -Path (Join-Path $RunDir '{target}') -Value '{ps2}' | Out-Null" if target != "batch-01.json"
                else f"[IO.File]::Delete('{ps2}'); New-Item -ItemType HardLink -Path '{ps2}' -Value (Join-Path $RunDir '{target}') | Out-Null")
        link = (f"if (-not $script:LinkDone -and $Message.StartsWith('RECEIPT_PERSIST_FAILED', [StringComparison]::Ordinal)) {{ $script:LinkDone = $true; {make}; "
                "throw [IO.IOException]::new('locked') }")
        anchor_l = "function L([string]$Message) {\n"
        patches = [(anchor_l, anchor_l + "    " + link + "\n")]
        if restore:
            undo = f"[IO.File]::Delete('{ps2}'); [IO.File]::Copy('{psq(original)}', '{ps2}')"
            patches.append((guard_old, f"        if ($batchNo -eq 2) {{ {undo} }}\n" + guard_old))
        args[args.index("-File") + 1] = str(self._patched_runner(*patches[0], *patches[1:]))
        return subprocess.run(args, capture_output=True, text=True, timeout=120, env=e), spec2, original

    def test_r4_adv2_a1_the_jsonl_line_is_not_appended_to_an_input_that_became_a_link_to_batches_jsonl_during_the_diagnostic(self) -> None:
        # adversary r4 pass 2 A1: the batches.jsonl append (and the pre-claim deadline path's receipt writes) had no alias guard, only the rewrite did. batch-01.json is a directory, so its
        # diagnostic runs first; in it the second leg's spec becomes batches.jsonl itself. Every receipt write now asks (Write-ReceiptFile), so the append is skipped.
        r, spec2, original = self._link_spec2_to_a_receipt_inside_the_first_diagnostic("batches.jsonl", ("batch-01.json",))
        self.assertEqual(spec2.read_bytes(), original.read_bytes(), "the receipt line was appended into the accepted input\n" + r.stdout + r.stderr)
        self.assertIn("BATCH_RECEIPT_WRITE_SKIPPED batch=1 ", r.stderr)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_SPEC_IS_RUN_OUTPUT", r.stderr + r.stdout)

    def test_r4_adv2_a1_the_pre_claim_deadline_receipt_is_not_written_through_an_input_that_became_a_link_to_it(self) -> None:
        # adversary r4 pass 2 A1, sibling (the pre-claim deadline path, which writes batch-02.json before any claim guard): the second leg's spec becomes batch-02.json inside batch 1's
        # diagnostic, and a 1800 s cooldown sends batch 2 to the deadline path, whose receipt write would overwrite the spec.
        r, spec2, original = self._link_spec2_to_a_receipt_inside_the_first_diagnostic("batch-02.json", ("batch-01.json",), extra=("-LeaseCooldownSec", "1800"))
        self.assertEqual(spec2.read_bytes(), original.read_bytes(), "the deadline path's receipt was written into the accepted input\n" + r.stdout + r.stderr)
        self.assertIn("BATCH_RECEIPT_WRITE_SKIPPED batch=1 ", r.stderr)   # batch-01.json is a directory (no rewrite guard behind it): the batches.jsonl append is the guarded write
        self.assertIn("BATCH_RECEIPT_NOT_WRITTEN batch=2 ", r.stderr)

    def test_r4_adv2_a2_an_alias_seen_while_a_receipt_was_written_stays_seen_after_the_link_is_gone(self) -> None:
        # adversary r4 pass 2 A2: the skipped rewrite turned the suppression on but saved no hit, so a link removed (and the spec restored) before batch 2's claim guard let batch 2 write
        # leg-plan.json, open a lease and run a probe and a leg despite the announced suppression. The hit is sticky: batch 2's guard re-reports it, so the run ends in the typed refusal.
        r, spec2, original = self._link_spec2_to_a_receipt_inside_the_first_diagnostic("batch-01.json", ("batches.jsonl",), restore=True)
        self.assertEqual(spec2.read_bytes(), original.read_bytes(), "the spec was restored by the test before batch 2's guard (the case under test)")
        m = re.search(r"LEG_PLAN_REFUSED reason=(\S+) item=2 ", r.stderr)
        self.assertIsNotNone(m, r.stdout + r.stderr)
        self.assertEqual(m.group(1), "LEG_SPEC_IS_RUN_OUTPUT")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(len([k for k in self.kinds() if k.startswith("OPEN:")]), 1, "no lease is opened for batch 2")
        self.assertFalse(self.claim_path.exists(), "the claim was left behind")
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["notRun"]), (2, "LEG_SPEC_IS_RUN_OUTPUT", 1, 1))

    def test_r4_adv_a1_the_batch_receipt_rewrite_still_happens_when_no_input_is_a_run_output(self) -> None:
        # the control: the same diagnostic log failure with no link rewrites batch-01.json with the count (r2's second per-batch write), so the new guard changes nothing else
        anchor_l = "function L([string]$Message) {\n"
        args, e, _ = self._two_batch_plan("")
        args[args.index("-File") + 1] = str(self._patched_runner(anchor_l, anchor_l + "    if ($Message.StartsWith('RECEIPT_PERSIST_FAILED', [StringComparison]::Ordinal)) { throw [IO.IOException]::new('locked') }\n"))
        r = subprocess.run(args, capture_output=True, text=True, timeout=120, env=e)
        self.assertEqual(r.returncode, 13, r.stdout + r.stderr)
        self.assertGreaterEqual(json.loads((self.run_dir / "batch-01.json").read_text(encoding="utf-8"))["logWriteFailures"], 1, "batch-01.json was rewritten with the diagnostic's log failure")
        self.assertNotIn("BATCH_RECEIPT_WRITE_SKIPPED", r.stderr)

    # r5 (opus r4b B1 = sol r4b B1 = adversary r4b OPEN A1; catalogue RPF-04, RPF-05; folds LEASE-BATCH-FINAL-RECEIPT-ALIAS-EXIT-1): a receipt write the input guard skipped recorded nothing,
    # and only the NEXT batch's claim guard read the saved hit, so on the final batch the run exited 0 with neither receipt on disk. And an input that could not be opened (a spec that already
    # ran, deleted) was taken for "an input is a runner output". Each case changes an input inside a copy of the runner, at the receipt write it names.
    _FINAL_RECEIPT = "        Write-BatchReceipt $b\n    }\n}\nWrite-RunSummary\n"
    _PRECLAIM_DEADLINE = "reason=DEADLINE detail=$($b.endDetail)\"   # r7 (sol r6b): the line is tried before the receipt snapshots logWriteFailures\n"
    _CLAIM_HELD_DEADLINE = "reason=DEADLINE detail=claim-held\"   # r7 (sol r6b): the line is tried before the receipt snapshots logWriteFailures\n"

    def _r5_spec(self, name: str, src: Path) -> Path:
        p = self.case / "r5" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(src.read_bytes())
        return p

    def _r5_link_to_batch_log(self, spec: Path) -> tuple[str, Path]:
        """PowerShell that makes `spec` a hard link to RunDir\\batch.log and snapshots batch.log; (the code, the snapshot)."""
        snap, s = self.case / "r5-batch.log.snap", psq(spec)
        return (f"[IO.File]::Delete('{s}'); New-Item -ItemType HardLink -Path '{s}' -Value (Join-Path $RunDir 'batch.log') | Out-Null; "
                f"Copy-Item -LiteralPath (Join-Path $RunDir 'batch.log') -Destination '{psq(snap)}'"), snap

    def _r5_run(self, args: list[str], e: dict, anchor: str, inject: str, batch: int = 0) -> subprocess.CompletedProcess:
        """Run `args` on a runner copy that runs `inject` at `anchor` (before the final receipt write, in batch `batch` when > 0; after a deadline anchor's line)."""
        if anchor == self._FINAL_RECEIPT:
            new = f"        if ({'$true' if batch <= 0 else f'$batchNo -eq {batch}'}) {{ {inject} }}\n" + anchor
        else:
            new = anchor + ("            " if anchor == self._PRECLAIM_DEADLINE else "        ") + inject + "\n"
        args[args.index("-File") + 1] = str(self._patched_runner(anchor, new))
        return subprocess.run(args, capture_output=True, text=True, timeout=240, env=e)

    def _r5_assert_suppressed(self, res: dict, reason: str, batches: tuple = (1,)) -> None:
        got = sorted((f["batch"], Path(f["file"]).name) for f in res["receiptPersistFailures"])
        self.assertEqual(got, sorted((b, n) for b in batches for n in (f"batch-{b:02d}.json", "batches.jsonl")), res["receiptPersistFailures"])
        for f in res["receiptPersistFailures"]:
            self.assertTrue(f["reason"].startswith(f"WRITE_SUPPRESSED {reason}"), f)
            self.assertEqual(f["suppressedBy"], reason, f)

    def test_r5_b1_rpf04_a_final_batch_alias_seen_at_the_receipt_write_is_a_typed_nonzero_exit_naming_both_receipts(self) -> None:
        for form in ("plan", "scalar"):
            with self.subTest(form=form):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                self._stage_case()
                spec = self._r5_spec("fx-a.json", self.specs[0])
                link, snap = self._r5_link_to_batch_log(spec)
                args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)])) if form == "plan" else self.batch_cmd([spec])
                r = self._r5_run(args, e, self._FINAL_RECEIPT, link)
                self.assertTrue(snap.exists(), "the link was made before the final receipt write (the case under test)\n" + r.stdout + r.stderr)
                self.assertEqual(r.returncode, 2, "a lost receipt was an exit 0\n" + r.stdout + r.stderr)
                res = batch_result(r.stdout)
                self.assertEqual((res["exitCode"], res["stopReason"], res["ran"]), (2, "LEG_SPEC_IS_RUN_OUTPUT", 1))
                self._r5_assert_suppressed(res, "LEG_SPEC_IS_RUN_OUTPUT")
                self.assertFalse((self.run_dir / "batch-01.json").exists())
                self.assertFalse((self.run_dir / "batches.jsonl").exists())
                self.assertEqual((self.run_dir / "batch.log").read_bytes(), snap.read_bytes(), "nothing was appended to the linked batch.log")
                self.assertEqual(res["logWriteFailures"], 0, "the suppressed result-line append is no log failure")
                self.assertIn("BATCH_RECEIPT_GUARD_STOP reason=LEG_SPEC_IS_RUN_OUTPUT", r.stderr)
                self.assertFalse(self.claim_path.exists(), "the claim was left behind")

    def test_r5_b1_rpf05_a_final_batch_spec_that_already_ran_and_was_deleted_is_input_vanished_with_the_receipts_written(self) -> None:
        spec = self._r5_spec("fx-a.json", self.specs[0])
        args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]))
        r = self._r5_run(args, e, self._FINAL_RECEIPT, f"[IO.File]::Delete('{psq(spec)}')")
        self.assertFalse(spec.exists(), "the spec was deleted (the case under test)")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["receiptPersistFailures"], res["logWriteFailures"]), (2, "INPUT_VANISHED", 1, [], 0))
        self.assertNotIn("IS_RUN_OUTPUT", r.stdout + r.stderr, "a deleted input is not a runner output")
        self.assertTrue((self.run_dir / "batch-01.json").is_file(), "a deleted input suppresses no receipt")
        [b] = self.batches()
        self.assertEqual(b["endReason"], "ALL_LEGS_DONE")
        self.assertIn("BATCH_RECEIPT_GUARD_STOP reason=INPUT_VANISHED item=1 ", r.stderr)

    def test_r5b_b1_a_final_batch_spec_that_is_there_but_will_not_open_is_input_unreadable_with_the_receipts_written(self) -> None:
        # r5b (item (c)'s other half): a spec that already ran is replaced by a junction to a folder that does not exist: the name is there, nothing opens through it. INPUT_UNREADABLE, not a
        # runner output: the receipts are written and the run still ends on it (exit 2), never exit 0.
        spec = self._r5_spec("fx-a.json", self.specs[0])
        args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]))
        nowhere = psq(self.case / "r5" / "no-such-folder")
        r = self._r5_run(args, e, self._FINAL_RECEIPT, f"[IO.File]::Delete('{psq(spec)}'); [void][IO.Directory]::CreateDirectory('{nowhere}'); "
                         f"New-Item -ItemType Junction -Path '{psq(spec)}' -Value '{nowhere}' | Out-Null; [IO.Directory]::Delete('{nowhere}')")
        self.assertTrue(os.path.lexists(spec) and not spec.exists(), "the spec is a dangling junction (the case under test)\n" + r.stdout + r.stderr)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        # r7 (sol r6 B2; RPF-09): an input that is there and will not resolve may reach any destination through a link on its side, so no existing receipt destination is written
        # any more: both receipts are typed suppressed (batch.log, which exists, turns the suppression on), never written. The typing r5b set is unchanged.
        self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["logWriteFailures"]), (2, "INPUT_UNREADABLE", 1, 0))
        self._r5_assert_suppressed(res, "INPUT_UNREADABLE")
        self.assertNotIn("IS_RUN_OUTPUT", r.stdout + r.stderr, "an input that will not open is not a runner output")
        self.assertFalse((self.run_dir / "batch-01.json").exists(), "no receipt is written while an input that is there will not resolve")
        self.assertIn("BATCH_RECEIPT_GUARD_STOP reason=INPUT_UNREADABLE item=1 ", r.stderr)

    def test_r5b_adv_a1_an_input_replaced_by_a_folder_during_the_final_batch_is_input_unreadable_never_exit_0(self) -> None:
        # adversary r5b A1: a folder opens like a file (BACKUP_SEMANTICS), is no runner output and (scalar form, the plan file) has no content check, so an accepted input replaced by an
        # empty folder after its leg ran was no hit at all and the run ended exit 0. Each input kind: the spec (both forms), the plan file, the staged build manifest.
        for form in ("scalar spec", "plan spec", "plan file", "build manifest"):
            with self.subTest(form=form):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                self._stage_case()
                spec = self._r5_spec("fx-a.json", self.specs[0])
                if form == "scalar spec":
                    args, e = self.batch_cmd([spec])
                else:
                    args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]))
                target = {"plan file": self.case / "plan.json", "build manifest": self._staged_manifest(self.commit_a)}.get(form, spec)
                r = self._r5_run(args, e, self._FINAL_RECEIPT, f"[IO.File]::Delete('{psq(target)}'); [void][IO.Directory]::CreateDirectory('{psq(target)}')")
                self.assertTrue(target.is_dir(), "the input is a folder now (the case under test)\n" + r.stdout + r.stderr)
                self.assertEqual(r.returncode, 2, "an input that is a folder now was an exit 0\n" + r.stdout + r.stderr)
                res = batch_result(r.stdout)
                self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["receiptPersistFailures"], res["logWriteFailures"]), (2, "INPUT_UNREADABLE", 1, [], 0))
                self.assertNotIn("IS_RUN_OUTPUT", r.stdout + r.stderr)
                self.assertTrue((self.run_dir / "batch-01.json").is_file(), "a folder at an input's name suppresses no receipt")
                self.assertEqual(len(self.batches()), 1)
                self.assertIn("BATCH_RECEIPT_GUARD_STOP reason=INPUT_UNREADABLE", r.stderr)

    def test_r5b_adv2_a1_a_staged_manifest_gone_after_the_final_leg_ends_the_run_on_it_never_exit_0(self) -> None:
        # adversary r5b pass 2 A1: a staged manifest that is gone skips the location guard (BUILD_NOT_STAGED is the content guard's), and the receipt guard kept only unresolved hits,
        # so that content hit was dropped and the run ended exit 0. Every hit the receipt guard sees is kept: the receipts are written and the run ends on it.
        spec = self._r5_spec("fx-a.json", self.specs[0])
        args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]))
        manifest = self._staged_manifest(self.commit_a)
        r = self._r5_run(args, e, self._FINAL_RECEIPT, f"[IO.File]::Delete('{psq(manifest)}')")
        self.assertFalse(manifest.exists(), "the staged manifest was deleted (the case under test)")
        self.assertEqual(r.returncode, 2, "a manifest gone after the final leg was an exit 0\n" + r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["receiptPersistFailures"]), (2, "BUILD_NOT_STAGED", 1, []))
        self.assertTrue((self.run_dir / "batch-01.json").is_file())
        self.assertEqual(len(self.batches()), 1)
        self.assertIn("BATCH_RECEIPT_INPUT_CHANGED batch=1 reason=BUILD_NOT_STAGED", r.stderr)
        self.assertIn("BATCH_RECEIPT_GUARD_STOP reason=BUILD_NOT_STAGED", r.stderr)

    def test_r5b_adv2_a2_a_spec_whose_data_cannot_be_read_is_input_unreadable_never_exit_0(self) -> None:
        # adversary r5b pass 2 A2: the guard's opens ask for no access, so a spec whose FILE_READ_DATA is denied (metadata still readable) passed them, and the scalar form has no
        # content check: exit 0. The input is now opened for reading: INPUT_UNREADABLE, the receipts written, exit 2.
        for form in ("scalar", "plan"):
            with self.subTest(form=form):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                self._stage_case()
                spec = self._r5_spec("fx-a.json", self.specs[0])
                self.addCleanup(subprocess.run, ["icacls", str(spec), "/reset"], capture_output=True)
                args, e = self.batch_cmd([spec]) if form == "scalar" else self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]))
                deny = f"$null = icacls '{psq(spec)}' /deny ([Security.Principal.WindowsIdentity]::GetCurrent().Name + ':(RD)')"
                r = self._r5_run(args, e, self._FINAL_RECEIPT, deny)
                with self.assertRaises(PermissionError, msg="the spec's data cannot be read (the case under test)"):
                    spec.read_bytes()
                self.assertEqual(r.returncode, 2, "an unreadable spec was an exit 0\n" + r.stdout + r.stderr)
                res = batch_result(r.stdout)
                self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["receiptPersistFailures"]), (2, "INPUT_UNREADABLE", 1, []))
                self.assertNotIn("IS_RUN_OUTPUT", r.stdout + r.stderr)
                self.assertTrue((self.run_dir / "batch-01.json").is_file())
                subprocess.run(["icacls", str(spec), "/reset"], capture_output=True)

    def test_r5b_adv_a2_a_plan_missing_only_while_one_guard_ran_is_no_alias_and_suppresses_nothing(self) -> None:
        # adversary r5b A2: the guard failed to open the plan (moved away in a save-as-rename), the plan was back before Get-InputUnresolvedCode probed it, and that stale failure was
        # labelled LEG_PLAN_IS_RUN_OUTPUT: both receipts suppressed, exit 2. Only a refusal reached with the input itself open is an alias; this one is asked again and is clean.
        spec = self._r5_spec("fx-a.json", self.specs[0])
        plan = self.write_plan([self.item(spec, "A", self.commit_a)])
        args, e = self.plan_cmd(plan)
        p, away = psq(plan), psq(plan) + ".away"
        # the probe's own signature, whichever form the runner has (r5b pass 2 added -SkipRead)
        anchor = next(a for a in ("function Get-InputUnresolvedCode([string]$Path, [switch]$SkipRead) {\n", "function Get-InputUnresolvedCode([string]$Path) {\n")
                      if a in RUNNER.read_text(encoding="utf-8"))
        restore = anchor + f"    if ([IO.File]::Exists('{away}')) {{ [IO.File]::Move('{away}', '{p}') }}\n"
        new = f"        if ($true) {{ [IO.File]::Move('{p}', '{away}') }}\n" + self._FINAL_RECEIPT
        args[args.index("-File") + 1] = str(self._patched_runner(self._FINAL_RECEIPT, new, (anchor, restore)))
        r = subprocess.run(args, capture_output=True, text=True, timeout=240, env=e)
        self.assertTrue(plan.is_file() and not Path(str(plan) + ".away").exists(), "the plan was moved away and back (the case under test)\n" + r.stdout + r.stderr)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["receiptPersistFailures"], res["logWriteFailures"]), (0, None, [], 0))
        self.assertNotIn("IS_RUN_OUTPUT", r.stdout + r.stderr)
        self.assertNotIn("BATCH_RECEIPT_WRITE_SKIPPED", r.stderr)
        self.assertTrue((self.run_dir / "batch-01.json").is_file())
        self.assertEqual(len(self.batches()), 1)

    def test_r5_b1_a_leg_plan_moved_away_during_the_final_batch_is_input_vanished_and_one_replaced_in_full_is_clean(self) -> None:
        spec = self._r5_spec("fx-a.json", self.specs[0])
        plan = self.write_plan([self.item(spec, "A", self.commit_a)])
        args, e = self.plan_cmd(plan)
        r = self._r5_run(args, e, self._FINAL_RECEIPT, f"[IO.File]::Move('{psq(plan)}', '{psq(plan)}.away')")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["stopReason"], res["receiptPersistFailures"]), ("INPUT_VANISHED", []))
        self.assertNotIn("IS_RUN_OUTPUT", r.stdout + r.stderr)
        self.assertTrue((self.run_dir / "batch-01.json").is_file())
        # the control: the plan replaced in one step (rename over it) is never missing, so the run is clean: exit 0, both receipts on disk
        self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
        self._stage_case()
        spec = self._r5_spec("fx-a.json", self.specs[0])
        plan = self.write_plan([self.item(spec, "A", self.commit_a)])
        args, e = self.plan_cmd(plan)
        r = self._r5_run(args, e, self._FINAL_RECEIPT, f"[IO.File]::Copy('{psq(plan)}', '{psq(plan)}.new'); [IO.File]::Move('{psq(plan)}.new', '{psq(plan)}', $true)")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["receiptPersistFailures"]), (0, None, []))
        self.assertTrue((self.run_dir / "batch-01.json").is_file())
        self.assertEqual(len(self.batches()), 1)

    # PR 704 r6 (sol r5b B1 = Dot A1 = adversary dc9b06a6 OPEN A1; card LEASE-BATCH-ZONE-UNRESOLVED-LABEL-1; catalogue RPF-06): an OUTPUT the guard could not inspect at the final receipt write
    # (a dangling runner-owned link in -RunDir, a link that will not resolve, a zone that will not list, an output that will not identify) was caught as "the input cannot be resolved" and
    # labelled an input alias: both writable receipts suppressed, stop LEG_*_IS_RUN_OUTPUT, though no input aliased an output. It is OUTPUT_ZONE_UNRESOLVED now, exit 2, the receipts written.
    _RPF06_ZONE_LIST = "            foreach ($c in @(Get-ChildItem -LiteralPath $z.root -Force -ErrorAction Stop | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint })) {\n"
    _RPF06_SCAN_ID = "                    if (((Get-FileIdentity $c.FullName).key -ceq $id.key)"
    _RPF06_RESOLVE = "[VenueLeaseBatchFinalPath]::Resolve([IO.Path]::GetFullPath($Path))"

    def _rpf06_dangling(self, at: Path) -> str:
        """PowerShell that makes `at` a junction to a folder that no longer exists (the link is there; nothing resolves through it)."""
        nowhere = psq(self.case / "rpf06-no-such-folder")
        return f"[void][IO.Directory]::CreateDirectory('{nowhere}'); New-Item -ItemType Junction -Path '{psq(at)}' -Value '{nowhere}' | Out-Null; [IO.Directory]::Delete('{nowhere}')"

    def _rpf06_run(self, args: list[str], e: dict, inject: str, *more: tuple[str, str]) -> subprocess.CompletedProcess:
        args[args.index("-File") + 1] = str(self._patched_runner(self._FINAL_RECEIPT, f"        if ($true) {{ {inject} }}\n" + self._FINAL_RECEIPT, *more))
        return subprocess.run(args, capture_output=True, text=True, timeout=240, env=e)

    def _rpf06_assert_typed_and_written(self, r: subprocess.CompletedProcess) -> dict:
        self.assertEqual(r.returncode, 2, "an output that could not be inspected was not its own typed stop\n" + r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["receiptPersistFailures"]), (2, "OUTPUT_ZONE_UNRESOLVED", 1, []), r.stderr)
        self.assertNotIn("IS_RUN_OUTPUT", r.stdout + r.stderr, "no input aliases an output")
        self.assertNotIn("BATCH_RECEIPT_WRITE_SKIPPED", r.stderr)
        self.assertTrue((self.run_dir / "batch-01.json").is_file(), "a receipt no real alias threatens is written")
        self.assertEqual(len(self.batches()), 1, "batches.jsonl has its line")
        self.assertIn("BATCH_RECEIPT_OUTPUT_UNRESOLVED batch=1 reason=OUTPUT_ZONE_UNRESOLVED", r.stderr)
        self.assertIn("BATCH_RECEIPT_GUARD_STOP reason=OUTPUT_ZONE_UNRESOLVED", r.stderr)
        self.assertFalse(self.claim_path.exists(), "the claim was left behind")
        return res

    def test_rpf06_b1_a_dangling_runner_owned_link_at_the_final_receipt_is_output_zone_unresolved_never_an_input_alias(self) -> None:
        # (a) scalar spec, (b) plan: RunDir\gate.log (runner-owned) becomes a link to nothing after the final BATCH_END; the inputs are untouched and open
        for form in ("scalar spec", "plan"):
            with self.subTest(form=form):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                self._stage_case()
                spec = self._r5_spec("fx-a.json", self.specs[0])
                args, e = self.batch_cmd([spec]) if form == "scalar spec" else self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]))
                gate = self.run_dir / "gate.log"
                r = self._rpf06_run(args, e, f"[IO.File]::Delete('{psq(gate)}'); " + self._rpf06_dangling(gate))
                self.assertTrue(os.path.lexists(gate) and not gate.exists(), "gate.log is a dangling link (the case under test)\n" + r.stdout + r.stderr)
                self._rpf06_assert_typed_and_written(r)

    def test_rpf06_c_an_output_that_will_not_identify_in_a_build_manifests_hard_link_scan_is_output_zone_unresolved(self) -> None:
        # (c) the staged build manifest has a second name (outside every zone), so only its guard scans the outputs by identity, and that scan throws: BUILD_MANIFEST_IS_RUN_OUTPUT before
        manifest = self._staged_manifest(self.commit_a)
        spec = self._r5_spec("fx-a.json", self.specs[0])
        args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]))
        other = self.case / "rpf06-manifest-second-name.json"
        r = self._rpf06_run(args, e, f"New-Item -ItemType HardLink -Path '{psq(other)}' -Value '{psq(manifest)}' | Out-Null; $script:rpf06ScanThrow = $true",
                            (self._RPF06_SCAN_ID, "                    if ($script:rpf06ScanThrow) { throw 'injected: an output will not identify' }\n" + self._RPF06_SCAN_ID))
        self.assertTrue(other.is_file(), "the manifest has a second name (the case under test)\n" + r.stdout + r.stderr)
        self._rpf06_assert_typed_and_written(r)
        self.assertIn("injected: an output will not identify", r.stderr, "the reason names the output's failure")

    def test_rpf06_d_an_output_link_whose_resolution_throws_after_the_inputs_open_is_output_zone_unresolved(self) -> None:
        # (d) gate.log is a junction to a folder that exists (outside every input), and resolving it throws: the inputs opened first, so the throw is the output's
        spec = self._r5_spec("fx-a.json", self.specs[0])
        args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]))
        gate, there = self.run_dir / "gate.log", self.case / "rpf06-gate-target"
        r = self._rpf06_run(args, e, f"[IO.File]::Delete('{psq(gate)}'); [void][IO.Directory]::CreateDirectory('{psq(there)}'); New-Item -ItemType Junction -Path '{psq(gate)}' -Value '{psq(there)}' | Out-Null; $script:rpf06ResolveThrow = $true",
                            (self._RPF06_RESOLVE, f"if ($script:rpf06ResolveThrow -and $Path -like '*\\gate.log') {{ throw 'injected: the output link will not resolve' }}; " + self._RPF06_RESOLVE))
        self.assertTrue(gate.is_dir(), "gate.log is a junction (the case under test)\n" + r.stdout + r.stderr)
        self._rpf06_assert_typed_and_written(r)

    def test_rpf06_e_an_output_zone_that_will_not_list_at_the_final_receipt_is_output_zone_unresolved(self) -> None:
        # (e) the -RunDir zone's listing throws (scalar and plan)
        for form in ("scalar spec", "plan"):
            with self.subTest(form=form):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                self._stage_case()
                spec = self._r5_spec("fx-a.json", self.specs[0])
                args, e = self.batch_cmd([spec]) if form == "scalar spec" else self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]))
                r = self._rpf06_run(args, e, "$script:rpf06ZoneThrow = $true",
                                    (self._RPF06_ZONE_LIST, "            if ($script:rpf06ZoneThrow) { throw 'injected: the zone will not list' }\n" + self._RPF06_ZONE_LIST))
                self._rpf06_assert_typed_and_written(r)

    def test_rpf06_f_a_dangling_link_at_a_receipts_own_name_suppresses_that_receipt_only(self) -> None:
        # the file-by-file check: batch-01.json itself is a link to nothing, so where its write lands is unknown: that receipt is suppressed (typed), batches.jsonl is still written
        spec = self._r5_spec("fx-a.json", self.specs[0])
        args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]))
        per = self.run_dir / "batch-01.json"
        r = self._rpf06_run(args, e, self._rpf06_dangling(per))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["ran"]), (2, "OUTPUT_ZONE_UNRESOLVED", 1))
        self.assertNotIn("IS_RUN_OUTPUT", r.stdout + r.stderr)
        self.assertEqual([Path(f["file"]).name for f in res["receiptPersistFailures"]], ["batch-01.json"], res["receiptPersistFailures"])
        [f] = res["receiptPersistFailures"]
        self.assertEqual(f["suppressedBy"], "OUTPUT_ZONE_UNRESOLVED")
        self.assertIn("this receipt file only", f["reason"])
        self.assertEqual(len(self.batches()), 1, "batches.jsonl is still written")

    def test_rpf06_g_a_real_alias_the_unlistable_zone_hides_still_suppresses_every_write(self) -> None:
        # the control the per-file check must keep: the zone will not list, so the guard cannot see that the spec is a hard link to batch.log; the batch.log check sees it and turns the
        # suppression on: no receipt is written, nothing is appended to batch.log, both lost receipts are named, exit 2
        # r9 (sol r8 B1; RPF-10): typed by batch.log's own hit (the spec IS batch.log: LEG_SPEC_IS_RUN_OUTPUT), no longer by the earlier OUTPUT_ZONE_UNRESOLVED that hid it
        spec = self._r5_spec("fx-a.json", self.specs[0])
        link, snap = self._r5_link_to_batch_log(spec)
        args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]))
        r = self._rpf06_run(args, e, link + "; $script:rpf06ZoneThrow = $true",
                            (self._RPF06_ZONE_LIST, "            if ($script:rpf06ZoneThrow) { throw 'injected: the zone will not list' }\n" + self._RPF06_ZONE_LIST))
        self.assertTrue(snap.exists(), "the link was made (the case under test)\n" + r.stdout + r.stderr)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"]), (2, "LEG_SPEC_IS_RUN_OUTPUT"))
        self._r5_assert_suppressed(res, "LEG_SPEC_IS_RUN_OUTPUT")
        self.assertFalse((self.run_dir / "batch-01.json").exists())
        self.assertFalse((self.run_dir / "batches.jsonl").exists())
        self.assertEqual((self.run_dir / "batch.log").read_bytes(), snap.read_bytes(), "nothing was appended to the linked batch.log")
        self.assertIn("BATCH_OUTPUT_UNVOUCHED batch=1 reason=LEG_SPEC_IS_RUN_OUTPUT", r.stderr)
        self.assertIn("overall guard hit OUTPUT_ZONE_UNRESOLVED", r.stderr)

    # PR 704 r9 (sol r8 B1 = producer OPEN r8p2-A1; catalogue RPF-10): with an output the guard could not inspect, the receipt guard asked batch.log about its own destination but kept
    # only the sentence of the hit it found (Get-ReceiptDestinationRefusal), and typed the suppression and the stop by the earlier OUTPUT_ZONE_UNRESOLVED hit: a batch.log that IS an
    # accepted input was reported as an output nobody could inspect. The destination's own typed hit (kind, code, item) is the suppression's and the stop's now.
    def test_r9_rpf10_a_batch_log_that_is_an_accepted_input_behind_an_unresolved_zone_is_typed_by_its_own_alias(self) -> None:
        for form, code in (("spec", "LEG_SPEC_IS_RUN_OUTPUT"), ("plan", "LEG_PLAN_IS_RUN_OUTPUT"), ("build", "BUILD_MANIFEST_IS_RUN_OUTPUT")):
            with self.subTest(form=form):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                self._stage_case()
                spec = self._r5_spec("fx-a.json", self.specs[0])
                plan = self.write_plan([self.item(spec, "A", self.commit_a)])
                args, e = self.plan_cmd(plan)
                target = {"spec": spec, "plan": plan, "build": self._staged_manifest(self.commit_a)}[form]
                gate, log, snap = self.run_dir / "gate.log", self.run_dir / "batch.log", self.case / "r9-target.snap"
                # after the final BATCH_END: gate.log (runner-owned) is a link to nothing, and batch.log is replaced by a hard link to the accepted input
                inject = (f"[IO.File]::Delete('{psq(gate)}'); " + self._rpf06_dangling(gate) + f"; [IO.File]::Delete('{psq(log)}'); "
                          f"New-Item -ItemType HardLink -Path '{psq(log)}' -Value '{psq(target)}' | Out-Null; Copy-Item -LiteralPath '{psq(target)}' -Destination '{psq(snap)}'")
                r = self._rpf06_run(args, e, inject)
                self.assertTrue(snap.exists() and os.path.lexists(gate) and not gate.exists(), "the case under test was set up\n" + r.stdout + r.stderr)
                self.assertEqual(os.stat(log).st_ino, os.stat(target).st_ino, "batch.log is the accepted input (the case under test)")
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                res = batch_result(r.stdout)
                self.assertEqual((res["exitCode"], res["stopReason"]), (2, code), r.stderr)
                self._r5_assert_suppressed(res, code)
                self.assertEqual(target.read_bytes(), snap.read_bytes(), "nothing was written into the input through batch.log")
                self.assertIn(f"BATCH_RECEIPT_GUARD_STOP reason={code}", r.stderr)
                self.assertIn(f"BATCH_WRITE_REFUSED file={log} reason={code}", r.stderr)

    def test_r9_adv_a1_an_earlier_input_that_will_not_resolve_never_hides_a_later_inputs_alias(self) -> None:
        # adversary r9 A1 (the RPF-10 class inside Get-WriteDestinationHit): the destination check returned INPUT_UNREADABLE for the first input that would not resolve (the plan) before
        # comparing a later input's accepted identity, so a batch.log that IS the accepted spec was typed INPUT_UNREADABLE and the stop stayed OUTPUT_ZONE_UNRESOLVED. RED on a01ce8f0.
        spec = self._r5_spec("fx-a.json", self.specs[0])
        plan = self.write_plan([self.item(spec, "A", self.commit_a)])
        args, e = self.plan_cmd(plan)
        gate, log, snap = self.run_dir / "gate.log", self.run_dir / "batch.log", self.case / "r9-spec.snap"
        inject = (f"[IO.File]::Delete('{psq(gate)}'); " + self._rpf06_dangling(gate) + f"; [IO.File]::Delete('{psq(log)}'); "
                  f"New-Item -ItemType HardLink -Path '{psq(log)}' -Value '{psq(spec)}' | Out-Null; Copy-Item -LiteralPath '{psq(spec)}' -Destination '{psq(snap)}'; $script:r9DenyPlan = $true")
        deny = (self._RPF06_RESOLVE, f"if ($script:r9DenyPlan -and $Path -ieq '{psq(plan)}') {{ throw [UnauthorizedAccessException]::new('injected: the plan path will not resolve') }}; " + self._RPF06_RESOLVE)
        r = self._rpf06_run(args, e, inject, deny)
        self.assertTrue(snap.exists() and os.path.lexists(gate) and not gate.exists(), "the case under test was set up\n" + r.stdout + r.stderr)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"]), (2, "LEG_SPEC_IS_RUN_OUTPUT"), r.stderr)
        self._r5_assert_suppressed(res, "LEG_SPEC_IS_RUN_OUTPUT")
        self.assertEqual(spec.read_bytes(), snap.read_bytes(), "nothing was written into the spec through batch.log")

    # PR 704 r9 (sol r8 B2 = producer OPEN r8p2-A2; catalogue RPF-11): a receipt destination that is there but will not resolve (renamed between Test-NothingAtName and Get-PhysicalPath)
    # says nothing about any input or any other file, yet with no overall hit (or a content one) the receipt guard asked the general write guard, which turned the suppression on for
    # every write: the healthy sibling receipt and the summary's batch.log line were lost too. Only that destination's write is suppressed now, whatever the overall hit; a destination
    # that IS an accepted input still suppresses every write. Injected I/O seam, no real race.
    _R9_RESOLVE = "    try { $phys = (Get-PhysicalPath $Path).TrimEnd('\\'); $di = Get-FileIdentity $Path }\n"
    _R9_JSONL_WRITE = "    Write-ReceiptFile $B.batch $jsonl { $rec.logWriteFailures"

    def test_r9_rpf11_a_receipt_destination_that_will_not_resolve_suppresses_that_receipt_only(self) -> None:
        # (form, the destination that will not resolve, made before the write, the overall hit's setup, expected exit and stop)
        forms = (("initial, no overall hit", "batch-01.json", True, "", 13, "RECEIPT_PERSIST_FAILED"),
                 ("jsonl, no overall hit", "batches.jsonl", True, "", 13, "RECEIPT_PERSIST_FAILED"),
                 ("rewrite, no overall hit", "batch-01.json", False, "", 13, "RECEIPT_PERSIST_FAILED"),
                 ("initial, content hit", "batch-01.json", True, "content", 2, "BUILD_NOT_STAGED"),
                 ("jsonl, content hit", "batches.jsonl", True, "content", 2, "BUILD_NOT_STAGED"),
                 ("initial, output hit (control)", "batch-01.json", True, "output", 2, "OUTPUT_ZONE_UNRESOLVED"))
        for form, leaf, made, overall, exit_code, stop in forms:
            with self.subTest(form=form):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                self._stage_case()
                spec = self._r5_spec("fx-a.json", self.specs[0])
                args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]))
                dest, gate = self.run_dir / leaf, self.run_dir / "gate.log"
                inject = f"$script:r9Throw = '{leaf}'"
                if made:
                    inject += f"; [IO.File]::WriteAllText('{psq(dest)}', '')"
                else:
                    inject += "; $script:r9Bump = $true"   # a log failure counted after the first write, so batch-NN.json is written once more (the rewrite destination)
                if overall == "content":
                    inject += f"; [IO.File]::Delete('{psq(self._staged_manifest(self.commit_a))}')"
                elif overall == "output":
                    inject += f"; [IO.File]::Delete('{psq(gate)}'); " + self._rpf06_dangling(gate)
                r = self._rpf06_run(args, e, inject,
                                    (self._R9_RESOLVE, "    try { if ($script:r9Throw -and [IO.Path]::GetFileName($Path) -ieq $script:r9Throw) { throw 'injected: the receipt was renamed between its observation and its resolution' }; $phys = (Get-PhysicalPath $Path).TrimEnd('\\'); $di = Get-FileIdentity $Path }\n"),
                                    (self._R9_JSONL_WRITE, "    if ($script:r9Bump) { $script:logWriteFailures++ }\n" + self._R9_JSONL_WRITE))
                self.assertEqual(r.returncode, exit_code, r.stdout + r.stderr)
                res = batch_result(r.stdout)
                self.assertEqual((res["exitCode"], res["stopReason"], res["ran"]), (exit_code, stop, 1), r.stderr)
                self.assertNotIn("IS_RUN_OUTPUT", r.stdout + r.stderr)
                self.assertEqual([Path(f["file"]).name for f in res["receiptPersistFailures"]], [leaf], res["receiptPersistFailures"])
                [f] = res["receiptPersistFailures"]
                self.assertEqual(f["suppressedBy"], "OUTPUT_ZONE_UNRESOLVED", f)
                self.assertIn("injected: the receipt was renamed", f["reason"], "the reason is the destination's own hit")
                self.assertIn("this receipt file only", f["reason"])
                if leaf == "batches.jsonl":
                    self.assertEqual(self.batches(), [], "the destination that will not resolve is not written")
                    self.assertTrue((self.run_dir / "batch-01.json").is_file(), "the healthy batch-01.json is written")
                else:
                    self.assertEqual(len(self.batches()), 1, "the healthy batches.jsonl is written")
                    if made:
                        self.assertEqual((self.run_dir / "batch-01.json").read_bytes(), b"", "the destination that will not resolve is not written")
                    else:
                        self.assertTrue((self.run_dir / "batch-01.json").is_file(), "the first write landed before its rewrite was refused")
                self.assertNotIn("BATCH_RESULT_NOT_LOGGED", r.stderr, "a destination-only refusal turned the suppression on for every write")
                self.assertIn("LEASE_BATCH_RESULT=", (self.run_dir / "batch.log").read_text(encoding="utf-8"), "the summary line is still appended to batch.log")

    def test_r6_adv_a1_an_input_whose_lookup_is_denied_is_input_unreadable_never_input_vanished(self) -> None:
        # adversary r6 A1: the input is still there, but its own ACL denies everything and its folder denies listing, so resolving it AND looking it up are access denied. Get-Item (and,
        # for a staged manifest, Test-Path) read that as "nothing there": INPUT_VANISHED / skipped as gone. Only "not found" is gone: INPUT_UNREADABLE, the receipts written, exit 2.
        for form in ("scalar spec", "build manifest"):
            with self.subTest(form=form):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                self._stage_case()
                spec = self._r5_spec("fx-a.json", self.specs[0])
                if form == "scalar spec":
                    args, e = self.batch_cmd([spec])
                    target = spec
                else:
                    args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]))
                    target = self._staged_manifest(self.commit_a)
                me = "([Security.Principal.WindowsIdentity]::GetCurrent().Name)"
                for p in (target, target.parent):
                    self.addCleanup(subprocess.run, ["icacls", str(p), "/remove:d", os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", "")], capture_output=True)
                deny = (f"$null = icacls '{psq(target)}' /deny ({me} + ':(F)'); $null = icacls '{psq(target.parent)}' /deny ({me} + ':(RD)')")
                r = self._rpf06_run(args, e, deny)
                try:
                    with self.assertRaises(PermissionError, msg="the input's lookup is denied (the case under test)"):
                        os.stat(target)
                    self.assertEqual(r.returncode, 2, "an input whose lookup was denied ended exit 0\n" + r.stdout + r.stderr)
                    res = batch_result(r.stdout)
                    # r7 (sol r6 B2; RPF-09): the input is there and will not resolve, so no existing destination is written (typed suppressed, never written); the typing is unchanged
                    self.assertEqual((res["exitCode"], res["stopReason"], res["ran"]), (2, "INPUT_UNREADABLE", 1), r.stderr)
                    self._r5_assert_suppressed(res, "INPUT_UNREADABLE")
                    self.assertNotIn("INPUT_VANISHED", r.stdout + r.stderr, "an input that is still there is not gone")
                    self.assertNotIn("IS_RUN_OUTPUT", r.stdout + r.stderr)
                    self.assertFalse((self.run_dir / "batch-01.json").exists())
                finally:
                    for p in (target.parent, target):
                        subprocess.run(["icacls", str(p), "/remove:d", os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", "")], capture_output=True)

    def test_r6_adv2_a1_a_log_that_is_an_input_whose_own_path_will_not_resolve_is_never_written(self) -> None:
        # adversary r6 pass 2 A1: the spec's own path stops resolving (as a share that now denies it; injected, since a share needs admin) while the file is batch.log by another name.
        # The guard has no answer for that input (INPUT_UNREADABLE, no alias), so nothing compared batch.log with it and the summary was appended INTO the spec. batch.log is now
        # asked on its own: as the input was accepted (batch.log re-linked to it), or, while the input does not resolve, any batch.log with another name: nothing is written into -RunDir.
        for form in ("input re-linked to batch.log", "batch.log re-linked to the input"):
            with self.subTest(form=form):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                self._stage_case()
                spec = self._r5_spec("fx-a.json", self.specs[0])
                args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]))
                s, log = psq(spec), psq(self.run_dir / "batch.log")
                snap = self.case / "rpf06-spec.snap"
                relink = (f"[IO.File]::Delete('{s}'); New-Item -ItemType HardLink -Path '{s}' -Value '{log}' | Out-Null" if form.startswith("input")
                          else f"[IO.File]::Copy('{log}', '{s}', $true); [IO.File]::Delete('{log}'); New-Item -ItemType HardLink -Path '{log}' -Value '{s}' | Out-Null")
                inject = f"{relink}; Copy-Item -LiteralPath '{s}' -Destination '{psq(snap)}'; $script:rpf06DenySpec = $true"
                deny = (self._RPF06_RESOLVE, f"if ($script:rpf06DenySpec -and $Path -ieq '{s}') {{ throw [UnauthorizedAccessException]::new('injected: access to the input path is denied') }}; " + self._RPF06_RESOLVE)
                r = self._rpf06_run(args, e, inject, deny)
                self.assertTrue(snap.exists(), "the case under test was set up\n" + r.stdout + r.stderr)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                res = batch_result(r.stdout)
                # r9 (sol r8 B1; RPF-10): typed by batch.log's own hit. Re-linked to the input, batch.log IS the spec as it was accepted (same file identity): LEG_SPEC_IS_RUN_OUTPUT,
                # which the earlier INPUT_UNREADABLE hid. With the input re-linked to batch.log, batch.log keeps its own identity (the accepted spec file is gone) and the spec's path will not resolve: INPUT_UNREADABLE.
                code = "INPUT_UNREADABLE" if form.startswith("input") else "LEG_SPEC_IS_RUN_OUTPUT"
                self.assertEqual((res["exitCode"], res["stopReason"]), (2, code), r.stderr)
                self.assertEqual(spec.read_bytes(), snap.read_bytes(), "nothing was written into the input through batch.log")
                self._r5_assert_suppressed(res, code)
                self.assertIn(f"BATCH_OUTPUT_UNVOUCHED batch=1 reason={code}", r.stderr)

    # PR 704 r7 (key3 opus r6 = sol r6 B1/B2 = Dot 16:48Z; catalogue RPF-08, RPF-09; folds LEASE-BATCH-INPUT-ALIAS-RESOLUTION-FAIL-REGRESSION-1): an accepted input that no longer resolves
    # (INPUT_VANISHED / INPUT_UNREADABLE) turned no write suppression on at the open or at a later claim, so the finally's log lines and the summary were appended into a batch.log that was
    # that input; and a destination with one name and no link was written while an input that is there would not resolve. Every writer into -RunDir now asks one guard right before it writes.
    def _r7_wait_for_event(self, p: subprocess.Popen, kind: str) -> None:
        end = time.monotonic() + 120
        while time.monotonic() < end and p.poll() is None and not (self.events.exists() and f" {kind} " in self.events.read_text(encoding="utf-8")):
            time.sleep(0.05)
        self.assertTrue(self.events.exists() and f" {kind} " in self.events.read_text(encoding="utf-8"), f"no {kind} event (the case under test)")

    def test_r7_dot_a_spec_renamed_and_batch_log_relinked_during_the_real_open_wait_is_never_appended_to(self) -> None:
        # Dot 16:48Z, no resolver mock: spec A is accepted; while the open waits in the agent's gate (FAKE_OPEN_SLEEP) A is renamed to A.old, batch.log is replaced by a hard link to
        # A.old and A stays absent. The guard reads A as INPUT_VANISHED, which suppressed nothing, so the finally's log lines went into A.old.
        for form in ("plan", "scalar"):
            with self.subTest(form=form):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                self._stage_case()
                spec = self._r5_spec("fx-a.json", self.specs[0])
                old = spec.with_name("fx-a.json.old")
                args, e = (self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]), FAKE_OPEN_SLEEP="8") if form == "plan"
                           else self.batch_cmd([spec], FAKE_OPEN_SLEEP="8"))
                p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=e, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
                try:
                    self._r7_wait_for_event(p, "OPENWAIT")
                    os.rename(spec, old)
                    log = self.run_dir / "batch.log"
                    log.unlink()
                    os.link(old, log)
                    snap = old.read_bytes()
                    out, err = p.communicate(timeout=300)
                finally:
                    if p.poll() is None:
                        subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, text=True, timeout=60)
                        p.communicate(timeout=60)
                self.assertFalse(spec.exists(), "A stays absent (the case under test)")
                self.assertEqual(old.read_bytes(), snap, "a write landed in the accepted input through batch.log\n" + out + err)
                self.assertEqual(p.returncode, 2, out + err)
                res = batch_result(out)
                self.assertEqual((res["exitCode"], res["stopReason"], res["ran"]), (2, "INPUT_VANISHED", 0), err)
                self.assertNotIn("IS_RUN_OUTPUT", out + err)

    def test_r7_sol_b2_a_one_name_receipt_is_not_written_while_an_input_that_is_there_will_not_resolve(self) -> None:
        # sol r6 B2 (RPF-09): the spec is accepted through a junction (cur -> orig). Before the final receipt the junction is retargeted at -RunDir, where a one-name copy of the spec sits
        # at the receipt's name, and resolving the input path is denied (injected, as a share that now denies it). links == 1 and no reparse point prove nothing about the input side.
        orig, cur, snap = self.case / "r7" / "orig", self.case / "r7" / "cur", self.case / "r7-spec.snap"
        orig.mkdir(parents=True)
        (orig / "batch-01.json").write_bytes(self.specs[0].read_bytes())
        subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", f"New-Item -ItemType Junction -Path '{psq(cur)}' -Value '{psq(orig)}' | Out-Null"], check=True, capture_output=True)
        spec = cur / "batch-01.json"
        args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]))
        s, c = psq(spec), psq(cur)
        retarget = (f"[IO.File]::Copy('{psq(orig / 'batch-01.json')}', (Join-Path $RunDir 'batch-01.json'), $true); [IO.Directory]::Delete('{c}'); "
                    f"New-Item -ItemType Junction -Path '{c}' -Value $RunDir | Out-Null; Copy-Item -LiteralPath '{s}' -Destination '{psq(snap)}'; $script:rpf06DenySpec = $true")
        deny = (self._RPF06_RESOLVE, f"if ($script:rpf06DenySpec -and $Path -ieq '{s}') {{ throw [UnauthorizedAccessException]::new('injected: access to the input path is denied') }}; " + self._RPF06_RESOLVE)
        r = self._rpf06_run(args, e, retarget, deny)
        self.assertTrue(snap.exists(), "the junction was retargeted (the case under test)\n" + r.stdout + r.stderr)
        self.assertEqual(spec.read_bytes(), snap.read_bytes(), "a receipt was written into the file the accepted input's path now reaches\n" + r.stdout + r.stderr)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"]), (2, "INPUT_UNREADABLE"), r.stderr)
        self._r5_assert_suppressed(res, "INPUT_UNREADABLE")
        self.assertNotIn("IS_RUN_OUTPUT", r.stdout + r.stderr)

    def test_r7_sol_b1_a_later_claims_unresolved_input_refusal_appends_no_summary_into_the_input(self) -> None:
        # sol r6 B1 = key3 opus r6 (RPF-08): batches.jsonl is a directory (a receipt-persistence failure), then before batch 2's claim guard batch.log becomes a hard link to the next
        # leg's spec and the spec's own path stops resolving (denied: injected) or the spec is renamed away (vanished: no mock). The refusal printed the summary into batch.log: the
        # NOT_RUN line and LEASE_BATCH_RESULT landed in the input.
        for how in ("denied", "vanished"):
            with self.subTest(how=how):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                self._stage_case()
                spec2 = self.case / "fx-b-copy.json"
                target = spec2 if how == "denied" else self.case / "fx-b-copy.json.old"
                s, t, log, snap = psq(spec2), psq(target), psq(self.run_dir / "batch.log"), self.case / "r7-b1.snap"
                move = "" if how == "denied" else f"[IO.File]::Move('{s}', '{t}'); "
                inject = (f"{move}[IO.File]::Delete('{log}'); New-Item -ItemType HardLink -Path '{log}' -Value '{t}' | Out-Null; Copy-Item -LiteralPath '{t}' -Destination '{psq(snap)}'"
                          + ("; $env:MLV_TEST_RPF06_DENY_SPEC = '1'" if how == "denied" else ""))
                args, e, patch = self._two_batch_plan(inject)
                patches = [patch]
                if how == "denied":
                    # r12 (#705 merge): the claim guard runs on Invoke-InputGuard's worker runspace, which gets the patched function but not this run's script variables, so the
                    # injected denial is a process-wide flag (as a real denied lookup reaches every thread)
                    patches.append((self._RPF06_RESOLVE, f"if ($env:MLV_TEST_RPF06_DENY_SPEC -and $Path -ieq '{s}') {{ throw [UnauthorizedAccessException]::new('injected: access to the input path is denied') }}; " + self._RPF06_RESOLVE))
                args[args.index("-File") + 1] = str(self._patched_runner(*patches[0], *patches[1:]))
                r = subprocess.run(args, capture_output=True, text=True, timeout=240, env=e)
                self.assertTrue(snap.exists(), "batch.log was relinked to the input (the case under test)\n" + r.stdout + r.stderr)
                self.assertEqual(target.read_bytes(), snap.read_bytes(), "the summary was appended into the accepted input\n" + r.stdout + r.stderr)
                code = "INPUT_UNREADABLE" if how == "denied" else "INPUT_VANISHED"
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn(f"LEG_PLAN_REFUSED reason={code} item=2 ", r.stderr)
                res = batch_result(r.stdout)
                self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["notRun"]), (2, code, 1, 1))

    # r7: one case per writer into -RunDir (and the claim dir's ledger): right before the writer, its destination becomes a hard link to the second leg's spec (an accepted input not yet
    # run). No guard ran in between, so only the writer's own guard can see it. (anchor kept by the runner, PowerShell run once just before it)
    _R7_WRITERS = {
        "batch.log (L)": ('                L "LEG $($leg.name) RUN batch=',
                          "[IO.File]::Delete((Join-Path $RunDir 'batch.log')); New-Item -ItemType HardLink -Path (Join-Path $RunDir 'batch.log') -Value '{b}' | Out-Null"),
        "leg output": ("                try { $lp = Invoke-VenueBoundedProcess -FilePath $pwshExe -ArgumentList $lArgs -TimeoutSec $legBoundSec } catch {\n",
                       "New-Item -ItemType HardLink -Path $out -Value '{b}' | Out-Null"),
        "probe output": ("    $qArgs = @('-NoProfile', '-NonInteractive', '-File', $QuietScript,", "New-Item -ItemType HardLink -Path $out -Value '{b}' | Out-Null"),
        "leg-plan.json": ('        L "CLAIM_TAKEN batch=$batchNo',
                          "New-Item -ItemType HardLink -Path (Join-Path $RunDir 'leg-plan.json') -Value '{b}' | Out-Null"),
        "lease ledger": ("                $b.ledger = $row\n",
                         "$lg = Get-VenueLeaseLedgerPath -Venue $Venue -ClaimDir $ClaimDir; if (Test-Path -LiteralPath $lg) {{ [IO.File]::Delete($lg) }}; New-Item -ItemType HardLink -Path $lg -Value '{b}' | Out-Null"),
        # adversary r7 A2: a name inside the probe's work folder (Wait-VenueQuiet writes its job files there) is already a hard link to the spec
        "probe work folder (a name inside it)": ("    $qArgs = @('-NoProfile', '-NonInteractive', '-File', $QuietScript,",
                                                 "$null = New-Item -ItemType Directory -Force -Path (Join-Path $RunDir 'quiet'); New-Item -ItemType HardLink -Path (Join-Path $RunDir 'quiet\\venue-quiet-probe-r7.job.ps1') -Value '{b}' | Out-Null"),
        # adversary r7 pass 2 A1: the open's own job folder holds a hard link to the spec when the open is submitted
        "open job folder": ("            $op = Submit-AgentJob $b.openJobId ",
                            "New-Item -ItemType HardLink -Path (Join-Path $RunDir 'batch-jobs\\keep.json') -Value '{b}' | Out-Null"),
    }

    def test_r7_every_writer_asks_the_guard_right_before_it_writes(self) -> None:
        for name, (anchor, make) in self._R7_WRITERS.items():
            with self.subTest(writer=name):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                self._stage_case()
                a, b = self._r5_spec("fx-a.json", self.specs[0]), self._r5_spec("fx-b.json", self.specs[1])
                snap = self.case / "r7-writer.snap"
                args, e = self.plan_cmd(self.write_plan([self.item(a, "A", self.commit_a), self.item(b, "A", self.commit_a)]))
                indent = re.match(r"\s*", anchor).group(0)
                inject = f"{indent}if (-not $script:r7Done) {{ $script:r7Done = $true; {make.format(b=psq(b))}; Copy-Item -LiteralPath '{psq(b)}' -Destination '{psq(snap)}' }}\n"
                args[args.index("-File") + 1] = str(self._patched_runner(anchor, inject + anchor))
                r = subprocess.run(args, capture_output=True, text=True, timeout=240, env=e)
                self.assertTrue(snap.exists(), "the destination was linked to the spec (the case under test)\n" + r.stdout + r.stderr)
                self.assertEqual(b.read_bytes(), snap.read_bytes(), f"the {name} writer wrote into the accepted input\n" + r.stdout + r.stderr)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn("BATCH_WRITE_REFUSED ", r.stderr)
                self.assertEqual(batch_result(r.stdout)["stopReason"], "LEG_SPEC_IS_RUN_OUTPUT")
                self.assertFalse(self.claim_path.exists(), "the claim was left behind")
                if name in ("leg-plan.json", "open job folder"):
                    # adversary r7 A3 / pass 2 A1: a refusal before the open stopped the claim keeper, so no open is submitted after it (it could outlive the claim)
                    self.assertNotIn("OPEN:", " ".join(self.kinds()), "an open was submitted after a write was refused before it")

    # r8 (Dot residual B2 on 9bbfaf75): a write refused before the open, by whichever writer, means no open is sent. The open's dispatch asks one predicate (Get-OpenDispatchRefusal) right
    # before um-run is called, so a refusal found AFTER the pre-open check (a log line on the way to the open, the open's own job folder) ends the batch the same way.
    _R8_OPEN_ANCHOR = "            $op = Submit-AgentJob $b.openJobId "

    def _r8_run(self, anchor: str, make: str, *extra: str, legs: int = 1) -> tuple[subprocess.CompletedProcess, Path, Path]:
        """One plan run with `make` (PowerShell; {b} = the last spec) run once just before `anchor`; (the run, the last spec, its snapshot taken right after `make`)."""
        self._stage_case()
        specs = [self._r5_spec(n, s) for n, s in (("fx-a.json", self.specs[0]), ("fx-b.json", self.specs[1]))][:max(1, legs)]
        b, snap = specs[-1], self.case / "r8.snap"
        args, e = self.plan_cmd(self.write_plan([self.item(s, "A", self.commit_a) for s in specs]), *extra)
        indent = re.match(r"\s*", anchor).group(0)
        inject = f"{indent}if (-not $script:r8Done) {{ $script:r8Done = $true; {make.format(b=psq(b))}; Copy-Item -LiteralPath '{psq(b)}' -Destination '{psq(snap)}' }}\n"
        args[args.index("-File") + 1] = str(self._patched_runner(anchor, inject + anchor))
        return subprocess.run(args, capture_output=True, text=True, timeout=240, env=e), b, snap

    def _r8_open_jobs_written(self) -> list[str]:
        jobs = self.run_dir / "batch-jobs"
        return sorted(p.name for p in jobs.glob("lease-batch-open-*")) if jobs.is_dir() else []

    def test_r8_b2_a_log_refusal_after_the_pre_open_check_sends_no_open(self) -> None:
        # Dot B2 steps 1-3: batch.log becomes a hard link to the spec after the pre-open check, while the open's sidecar is written. The next log line refuses it (the keeper is
        # stopped, the TEMP redirect made), and Submit-AgentJob then accepted the redirect and sent the OPEN. RED on dae24318: OPEN in the um-run calls.
        relink = "[IO.File]::Delete((Join-Path $RunDir 'batch.log')); New-Item -ItemType HardLink -Path (Join-Path $RunDir 'batch.log') -Value '{b}' | Out-Null"
        for name, anchor in (("before BATCH_OPEN_SUBMIT", '        L "BATCH_OPEN_SUBMIT batch='),
                             ("while the sidecar is written (Dot's route)", '        L "BATCH_OPEN_LIVENESS batch='),
                             ("before the sidecar read-back line", '            L "BATCH_OPEN_LIVENESS_READBACK batch=')):
            with self.subTest(at=name):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                r, b, snap = self._r8_run(anchor, relink)
                out = r.stdout + r.stderr
                self.assertTrue(snap.exists(), "batch.log was relinked to the spec (the case under test)\n" + out)
                self.assertEqual(b.read_bytes(), snap.read_bytes(), "a log line was appended into the accepted input\n" + out)
                self.assertNotIn("OPEN:", " ".join(self.kinds()), "an OPEN was sent after a write was refused before it\n" + out)
                self.assertEqual(self._r8_open_jobs_written(), [], "the open's job file was written under -RunDir")
                self.assertIn("BATCH_WRITE_REFUSED ", r.stderr)
                self.assertFalse(self.claim_path.exists(), "the claim was left behind\n" + out)
                self.assertEqual(r.returncode, 2, out)
                res = batch_result(r.stdout)
                self.assertEqual((res["stopReason"], res["batches"][0]["endReason"], res["batches"][0]["leaseId"]), ("LEG_SPEC_IS_RUN_OUTPUT", "LEG_SPEC_IS_RUN_OUTPUT", None), res)

    def test_r8_b2_suppression_first_found_by_the_open_job_folder_guard_sends_no_open(self) -> None:
        # Dot B2 (the :925 continuation): the open's job folder holds a hard link to the spec when the open is submitted; Submit-AgentJob's own guard is the first to see it.
        # RED on 9bbfaf75 (the guard turned the suppression on and the open went out through the TEMP redirect); fixed on dae24318 by r7 pass 3 A1, kept by the r8 predicate.
        r, b, snap = self._r8_run(self._R8_OPEN_ANCHOR, "New-Item -ItemType HardLink -Path (Join-Path $RunDir 'batch-jobs\\keep.json') -Value '{b}' | Out-Null")
        out = r.stdout + r.stderr
        self.assertTrue(snap.exists(), out)
        self.assertEqual(b.read_bytes(), snap.read_bytes(), out)
        self.assertNotIn("OPEN:", " ".join(self.kinds()), "an OPEN was sent after its own job folder was refused\n" + out)
        self.assertEqual(self._r8_open_jobs_written(), [], out)
        self.assertFalse(self.claim_path.exists(), "the claim was left behind\n" + out)
        self.assertEqual(r.returncode, 2, out)
        res = batch_result(r.stdout)
        self.assertEqual((res["stopReason"], res["batches"][0]["endReason"]), ("LEG_SPEC_IS_RUN_OUTPUT", "LEG_SPEC_IS_RUN_OUTPUT"), res)

    def test_r8_a_suppression_after_the_open_was_sent_still_submits_the_close(self) -> None:
        # The close the run owes is kept: batch.log becomes a hard link to the spec once the open has returned with its lease. The guard refuses it, nothing is probed or run,
        # and the CLOSE still goes out (its job file under the TEMP redirect, never under -RunDir).
        r, b, snap = self._r8_run("        $adm = $(if ($null -ne $op.result) { $op.result.admission } else { $null })",
                                  "[IO.File]::Delete((Join-Path $RunDir 'batch.log')); New-Item -ItemType HardLink -Path (Join-Path $RunDir 'batch.log') -Value '{b}' | Out-Null")
        out = r.stdout + r.stderr
        self.assertTrue(snap.exists(), out)
        self.assertEqual(b.read_bytes(), snap.read_bytes(), out)
        k = [x.split(":")[0] for x in self.kinds()]
        self.assertEqual(k, ["OPEN", "CLOSE"], "the open was sent, so its close is owed (and nothing ran)\n" + out)
        jobs = self.run_dir / "batch-jobs"
        self.assertEqual(sorted(p.name for p in jobs.glob("lease-batch-close-*")) if jobs.is_dir() else [], [], "the close job was written under -RunDir")
        self.assertFalse(self.claim_path.exists(), "the claim was left behind\n" + out)
        self.assertEqual(r.returncode, 2, out)
        self.assertEqual(batch_result(r.stdout)["stopReason"], "LEG_SPEC_IS_RUN_OUTPUT")

    def test_r8_an_open_that_could_be_admitted_after_the_claim_expires_is_not_sent(self) -> None:
        # Dot B2 step 4: the agent may admit the open until its notAfterUtc (the batch deadline, ~240 s here) and -ClaimTtlSec is 60. That is sent only while the keeper renews
        # the claim on schedule; a keeper that is stopped or whose renewal failed refuses the open OPEN_OUTLIVES_CLAIM (exit 9), nothing sent, the claim released.
        # RED on dae24318: the open is sent. Controls: a renewal that came back RENEWED, and a stopped keeper whose claim outlasts the open's notAfterUtc, still open.
        stop = "$script:keeperStoppedEarly = $true; $script:keeperStoppedInTime = Stop-VenueKeeper -Keeper $claimKeeper; $script:keeperLive = $false"
        cases = {
            "keeper stopped": (stop, ("-ClaimTtlSec", "60"), False),
            "last renewal BUSY": ("$claimKeeper.state.runs = 1; $claimKeeper.state.ok = 1; $claimKeeper.state.last = 'BUSY'", ("-ClaimTtlSec", "60"), False),
            "a renewal threw": ("$claimKeeper.state.runs = 1; $claimKeeper.state.errors = 1; $claimKeeper.state.lastError = 'injected'", ("-ClaimTtlSec", "60"), False),
            "control: last renewal RENEWED": ("$claimKeeper.state.runs = 1; $claimKeeper.state.ok = 1; $claimKeeper.state.renews = 1; $claimKeeper.state.last = 'RENEWED'",
                                              ("-ClaimTtlSec", "60"), True),
            "control: keeper stopped, the claim outlasts notAfterUtc": (stop, (), True),
            # adversary r8 A2: a keeper that has not renewed yet (runs=0) or RENEWED long ago does not vouch for a claim already expired or within one renewal of it
            "first renewal overdue, the claim already expired": ("$claim.expiresUtc = [DateTime]::UtcNow.AddSeconds(-1).ToString('o')", ("-ClaimTtlSec", "60"), "CLAIM_LOST"),
            "RENEWED long ago, the expiry within one renewal": ("$claimKeeper.state.runs = 1; $claimKeeper.state.ok = 1; $claimKeeper.state.renews = 1; $claimKeeper.state.last = 'RENEWED'; "
                                                                "$claimKeeper.state.expiresUtc = [DateTime]::UtcNow.AddSeconds(5).ToString('o')", ("-ClaimTtlSec", "60"), False),
        }
        for name, (make, extra, sent) in cases.items():
            with self.subTest(case=name):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                r, _b, snap = self._r8_run(self._R8_OPEN_ANCHOR, make, *extra)
                out = r.stdout + r.stderr
                self.assertTrue(snap.exists(), out)
                self.assertFalse(self.claim_path.exists(), "the claim was left behind\n" + out)
                k = [x.split(":")[0] for x in self.kinds()]
                res = batch_result(r.stdout)
                if sent is True:
                    self.assertEqual((r.returncode, k[0], k[-1]), (0, "OPEN", "CLOSE"), out)
                    continue
                reason = sent or "OPEN_OUTLIVES_CLAIM"
                self.assertEqual(k, [], "an open that could be admitted after the claim's expiry was sent\n" + out)
                self.assertEqual(self._r8_open_jobs_written(), [], out)
                self.assertEqual(r.returncode, 9, out)
                [b1] = self.batches()
                self.assertEqual((res["stopReason"], b1["endReason"], b1["leaseId"]), (reason, reason, None), b1)
                self.assertIn("notAfterUtc=", b1["endDetail"])

    def test_r8_adv2_a3_the_claim_and_the_lease_are_asked_again_at_the_launch_of_the_probe_and_the_leg(self) -> None:
        # adversary r8 pass 2 A3: the guards between the top-of-loop checks and a launch (the leg's output and RUN line, the probe's gate log, work folder and output) can block on a
        # share, so the claim can be lost and the lease end while they run. Injected right at the launch: nothing starts in batch 1, and batch 2 takes a fresh claim and lease and runs
        # the leg. RED on f26a2f1c: the leg (or the probe) is started on a lost claim or an ended lease.
        leg_anchor = "                if ($script:RunOutputAliased) { $b.legsAttempted--; Stop-BatchOnSuppression $b; break }   # r7: the RUN line itself was refused: the leg is not started"
        cases = {
            "leg: claim lost while its guards ran": (leg_anchor, "$claimKeeper.state.lost = 'NOT_OURS'", "CLAIM_LOST", ["OPEN", "QUIET", "CLOSE", "OPEN", "QUIET", "LEG", "CLOSE"]),
            "leg: the lease ended while its guards ran": (leg_anchor, "$leaseEnd = [DateTime]::UtcNow.AddSeconds(-31)", "LEASE_REMAINING_SHORT", ["OPEN", "QUIET", "CLOSE", "OPEN", "QUIET", "LEG", "CLOSE"]),
            "probe: claim lost while its guards ran": ("    if (-not ($asked -and (Test-RunDirWrite $out))) {","$claimKeeper.state.lost = 'NOT_OURS'", "CLAIM_LOST", ["OPEN", "CLOSE", "OPEN", "QUIET", "LEG", "CLOSE"]),
            # r9 (adversary r9 A2 = OPEN r8p3-A1; LEASE-BATCH-PROBE-LAUNCH-LEASE-ROOM-1): the fourth combination. RED on a01ce8f0: the probe starts after the lease ended.
            "probe: the lease ended while its guards ran": ("    if (-not ($asked -and (Test-RunDirWrite $out))) {", "$leaseEnd = [DateTime]::UtcNow.AddSeconds(-31)", "LEASE_REMAINING_SHORT", ["OPEN", "CLOSE", "OPEN", "QUIET", "LEG", "CLOSE"]),
        }
        for name, (anchor, make, reason, kinds) in cases.items():
            with self.subTest(case=name):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                r, _b, _snap = self._r8_run(anchor, make)
                out = r.stdout + r.stderr
                self.assertEqual([x.split(":")[0] for x in self.kinds()], kinds, out)
                self.assertEqual(r.returncode, 0, out)
                b1, b2 = self.batches()
                self.assertEqual((b1["endReason"], b1["legsAttempted"], b2["endReason"], b2["legsCounted"]), (reason, 0, "ALL_LEGS_DONE", 1), b1)
                self.assertIn("launch", b1["endDetail"])

    def test_r8_adv_a1_an_input_that_cannot_be_identified_as_accepted_is_refused_before_the_claim(self) -> None:
        # adversary r8 A1: the spec is renamed away while its accepted identity is taken and put back after. The snapshot failure was swallowed (accepted = null), so every destination
        # check skipped that input as accepted, and once renamed and re-linked at the end, a receipt was written through it. Now: asked again, then refused before any claim.
        # RED on 81ad0482: the run goes on (exit 0) with an input no destination can be compared with.
        self._stage_case()
        a = self._r5_spec("fx-a.json", self.specs[0])
        away = a.with_name("fx-a.away")
        args, e = self.plan_cmd(self.write_plan([self.item(a, "A", self.commit_a)]))
        before = "# r6 pass 3 (adversary r6 pass 2 A1): each input's physical path and file identity as accepted"
        after = "if ($LegSet) { $hit = Get-InputOutputRefusal; if ($hit) { Stop-InputIsOutput $hit } }"
        args[args.index("-File") + 1] = str(self._patched_runner(before, f"Move-Item -LiteralPath '{psq(a)}' -Destination '{psq(away)}'\n" + before,
                                                                 (after, f"Move-Item -LiteralPath '{psq(away)}' -Destination '{psq(a)}'\n" + after)))
        r = subprocess.run(args, capture_output=True, text=True, timeout=240, env=e)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 2, out)
        self.assertIn("LEG_PLAN_REFUSED reason=INPUT_VANISHED item=1 ", r.stderr)
        self.assertIn("could not be identified as it was accepted", r.stderr)
        self.assertEqual(self.kinds(), [], "a claim, open or leg followed an input with no accepted identity\n" + out)
        self.assertFalse(self.claim_path.exists(), out)

    def test_r7_adv2_a4_a_preflight_refusal_with_an_uninspectable_zone_appends_nothing_to_the_plan(self) -> None:
        # adversary r7 pass 2 A4: the plan file IS RunDir\batch.log (one name) and RunDir\gate.log is a link to nothing, so the preflight guard types OUTPUT_ZONE_UNRESOLVED; before any input
        # is accepted nothing is compared, and the refusal line was appended into the plan
        self.run_dir.mkdir(parents=True, exist_ok=True)
        spec = self._r5_spec("fx-a.json", self.specs[0])
        plan = self.write_plan([self.item(spec, "A", self.commit_a)])
        log = self.run_dir / "batch.log"
        log.write_bytes(plan.read_bytes())
        snap = log.read_bytes()
        subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", self._rpf06_dangling(self.run_dir / "gate.log")], check=True, capture_output=True)
        args, e = self.plan_cmd(log)
        r = subprocess.run(args, capture_output=True, text=True, timeout=240, env=e)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=OUTPUT_ZONE_UNRESOLVED", r.stderr)
        self.assertEqual(log.read_bytes(), snap, "the refusal line was appended into the plan file\n" + r.stdout + r.stderr)

    def test_r7_adv_a1_a_receipt_file_that_is_an_accepted_input_as_accepted_suppresses_every_write(self) -> None:
        # adversary r7 A1: after the final leg the spec is renamed to a.old and RunDir\batch-01.json is made a hard link to a.old (batch.log plain, batches.jsonl absent). The guard reads the
        # spec as INPUT_VANISHED and asks file by file: batch-01.json is the accepted input as accepted, which suppressed that one receipt only and kept the writes on.
        for form in ("scalar spec", "plan"):
            with self.subTest(form=form):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                self._stage_case()
                spec = self._r5_spec("fx-a.json", self.specs[0])
                old = spec.with_name("fx-a.json.old")
                args, e = self.batch_cmd([spec]) if form == "scalar spec" else self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]))
                r = self._rpf06_run(args, e, f"[IO.File]::Move('{psq(spec)}', '{psq(old)}'); New-Item -ItemType HardLink -Path (Join-Path $RunDir 'batch-01.json') -Value '{psq(old)}' | Out-Null")
                self.assertTrue(old.is_file() and not spec.exists(), "the spec was renamed and linked (the case under test)\n" + r.stdout + r.stderr)
                self.assertEqual(old.read_bytes(), self.specs[0].read_bytes(), "a receipt was written into the accepted input")
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                res = batch_result(r.stdout)
                self.assertEqual((res["exitCode"], res["stopReason"]), (2, "LEG_SPEC_IS_RUN_OUTPUT"), r.stderr)
                self.assertFalse((self.run_dir / "batches.jsonl").exists(), "the writes stayed on after an alias was found")
                self._r5_assert_suppressed(res, "LEG_SPEC_IS_RUN_OUTPUT")

    # r7: the static half: every write primitive in the runner is either asked about first (Test-RunDirWrite on the same line or the two before it) or listed here with why it needs no
    # ask. A new writer that is neither fails this test, so the next unguarded writer is found here, not by a reviewer.
    _R7_UNGUARDED_OK = {
        "New-Item -ItemType Directory -Force -Path $RunDir": "creates -RunDir before any input is accepted",
        "New-Item -ItemType Directory -Force -Path $jobDir": "creates batch-jobs before any input is accepted",
        "$null = New-Item -ItemType Directory -Path $redirect": "the output redirect: a NEW folder under TEMP, never -RunDir",
        "[IO.File]::Open($f, [IO.FileMode]::CreateNew": "the job file: CreateNew in a folder the guard just asked about (or the TEMP redirect); it never opens an existing file",
        "[IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read": "a read",
        "Add-Content -LiteralPath $batchLog -Value $resultLine": "the summary line: asked ($logOk) before the summary is serialized, or the TEMP redirect",
        "$putPerBatch = {": "a receipt: run only by Write-ReceiptFile, right after Test-ReceiptWriteSuppressed asks about its file",
        "Write-ReceiptFile $B.batch $jsonl {": "a receipt: run only by Write-ReceiptFile, right after Test-ReceiptWriteSuppressed asks about its file",
        "$captured = @(& $pwshExe @qArgs *>&1": "the quiet probe's streams merged into the pipeline (BACHELOR-THERMAL-GATE-WAIT-1 r5 pass 1 A2): no file; its evidence write is asked about right before it",
    }

    def test_r7_every_write_primitive_in_the_runner_is_guarded_or_listed(self) -> None:
        lines = RUNNER.read_text(encoding="utf-8").splitlines()
        prim = re.compile(r"Add-Content|Set-Content|Out-File|WriteAllText|AppendAllText|WriteAllBytes|WriteAllLines|\[IO\.File\]::(Open|Create|Copy|Move)\(|New-Item\b|Copy-Item|Move-Item|\*>|(?<![<>-])>\s*\$(?!null)|Add-VenueLeaseLedger")
        bad = []
        for i, line in enumerate(lines):
            code = line.split("#", 1)[0] if not line.lstrip().startswith("#") else ""
            if not prim.search(code) or "Add-Type" in code:
                continue
            if any(k in code for k in self._R7_UNGUARDED_OK):
                continue
            if any("Test-RunDirWrite" in l for l in lines[max(0, i - 2):i + 1]):
                continue
            bad.append(f"{i + 1}: {line.strip()}")
        self.assertEqual(bad, [], "a write primitive with no Test-RunDirWrite right before it (guard it, or list it in _R7_UNGUARDED_OK with why)")

    def _r5_two_batch_args(self) -> tuple[list[str], dict, Path, Path]:
        a, b = self._r5_spec("fx-a.json", self.specs[0]), self._r5_spec("fx-b.json", self.specs[1])
        args, e = self.plan_cmd(self.write_plan([self.item(a, "A", self.commit_a), self.item(b, "A", self.commit_a)]), FAKE_LEASE_REMAIN="70,2700", FAKE_LEG_REMAIN="50,2600")
        return args, e, a, b

    def test_r5_b1_mid_run_an_alias_seen_at_batch_1s_receipt_write_names_both_lost_receipts_and_stops_typed(self) -> None:
        args, e, _a, b = self._r5_two_batch_args()
        link, snap = self._r5_link_to_batch_log(b)
        r = self._r5_run(args, e, self._FINAL_RECEIPT, link, batch=1)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["notRun"]), (2, "LEG_SPEC_IS_RUN_OUTPUT", 1, 1))
        self._r5_assert_suppressed(res, "LEG_SPEC_IS_RUN_OUTPUT")
        self.assertEqual(len([k for k in self.kinds() if k.startswith("OPEN:")]), 1, "no lease is opened for batch 2")
        self.assertEqual((self.run_dir / "batch.log").read_bytes(), snap.read_bytes())

    def test_r5_b1_mid_run_a_spec_that_already_ran_and_was_deleted_stops_input_vanished_not_as_a_run_output(self) -> None:
        args, e, a, _b = self._r5_two_batch_args()
        r = self._r5_run(args, e, self._FINAL_RECEIPT, f"[IO.File]::Delete('{psq(a)}')", batch=1)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=INPUT_VANISHED item=1 ", r.stderr)
        self.assertNotIn("IS_RUN_OUTPUT", r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["notRun"], res["receiptPersistFailures"]), (2, "INPUT_VANISHED", 1, 1, []))
        self.assertTrue((self.run_dir / "batch-01.json").is_file(), "batch 1's receipt is written")
        self.assertEqual(len([k for k in self.kinds() if k.startswith("OPEN:")]), 1)

    def test_r5_b1_the_pre_claim_deadline_path_records_a_receipt_the_guard_suppressed(self) -> None:
        self.claims.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        rows = [{"venue": "bachelor", "leaseId": "old-1", "startUtc": utc_iso(now - timedelta(minutes=105)), "endUtc": utc_iso(now - timedelta(minutes=60))},
                {"venue": "bachelor", "leaseId": "old-2", "startUtc": utc_iso(now - timedelta(minutes=45)), "endUtc": utc_iso(now - timedelta(minutes=1))}]
        (self.claims / "bachelor.lease-ledger.jsonl").write_text("".join(json.dumps(x) + "\n" for x in rows), encoding="utf-8")
        spec = self._r5_spec("fx-a.json", self.specs[0])
        link, snap = self._r5_link_to_batch_log(spec)
        args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]))
        r = self._r5_run(args, e, self._PRECLAIM_DEADLINE, link)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"]), (3, "DEADLINE"))
        self._r5_assert_suppressed(res, "LEG_SPEC_IS_RUN_OUTPUT")
        self.assertEqual((self.run_dir / "batch.log").read_bytes(), snap.read_bytes())

    def test_r5_b1_the_claim_held_deadline_path_records_a_receipt_the_guard_suppressed(self) -> None:
        self._foreign_live_claim()
        spec = self._r5_spec("fx-a.json", self.specs[0])
        link, snap = self._r5_link_to_batch_log(spec)
        args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]), "-DeadlineUtc", utc_iso(datetime.now(timezone.utc) + timedelta(seconds=12)))
        r = self._r5_run(args, e, self._CLAIM_HELD_DEADLINE, link)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"]), (3, "DEADLINE"))
        self._r5_assert_suppressed(res, "LEG_SPEC_IS_RUN_OUTPUT")
        self.assertEqual((self.run_dir / "batch.log").read_bytes(), snap.read_bytes())

    # r3 (sol r2b B2 = catalogue RPF-02): the lock helper took batch.log after its marker wait expired or was cancelled, whether or not the runner had paused, so a slow start was
    # locked ahead of the pause (and died on its next direct L), and 'locked' was written for a runner that never paused. These drive the helper alone: no runner, no pause marker.
    def test_r3_b2_an_expired_or_cancelled_marker_wait_takes_no_lock_and_signals_nothing(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        log = self.run_dir / "batch.log"
        log.write_text("", encoding="utf-8")
        paused, locked = self.case / "runner-paused.marker", self.case / "log-locked.marker"
        for how in ("expired", "cancelled"):
            with self.subTest(how=how):
                done, t, *rest = self._lock_log_while_the_runner_is_paused(log, paused, locked, give_up_s=0.5 if how == "expired" else 30)
                try:
                    if how == "cancelled":
                        done.set()
                    time.sleep(1.5)
                    self.assertFalse(locked.exists(), f"a {how} marker wait wrote the locked marker")
                    with open(log, "a", encoding="utf-8") as f:   # a sharing violation (PermissionError) here = the lock was taken with no pause
                        f.write("")
                    if rest:
                        self.assertEqual(rest[0]["outcome"], "never-paused" if how == "expired" else "cancelled")
                finally:
                    done.set()
                    t.join(5)
                    locked.unlink(missing_ok=True)

    def test_r3_adv_a1_a_pause_marker_that_appears_only_after_the_wait_expired_takes_no_lock(self) -> None:
        # adversary r3 A1: the helper re-checked the marker AFTER its wait loop, so a marker that appeared between the expiry and that check was locked on. The marker double is
        # absent on its first look and present on every later one; with give_up_s=0 the wait has expired before any later look.
        self.run_dir.mkdir(parents=True, exist_ok=True)
        log = self.run_dir / "batch.log"
        log.write_text("", encoding="utf-8")
        locked = self.case / "log-locked.marker"

        class LateMarker:
            parent = self.case
            looks = 0

            def exists(self) -> bool:
                LateMarker.looks += 1
                return LateMarker.looks > 1

        done, t, *rest = self._lock_log_while_the_runner_is_paused(log, LateMarker(), locked, give_up_s=0)
        try:
            t.join(5)
            self.assertFalse(locked.exists(), "a marker seen only after the wait expired was locked on")
            with open(log, "a", encoding="utf-8") as f:   # PermissionError here = the lock was taken
                f.write("")
            if rest:
                self.assertEqual(rest[0]["outcome"], "never-paused")
        finally:
            done.set()
            t.join(5)

    # r4 (sol r3 B1; Dot 6457592181 on #704): the pause marker survived the runner's 60 s pause, so a helper delayed past it still locked, recorded `locked`, and the test certified a handshake
    # the runner never took part in. The runner now records its own expiry and acknowledges only a lock it probed while still paused; _assert_handshake demands that acknowledgment.
    def _handshake_state(self, outcome: str = "locked", paused: str | None = "n-1", ack: str | None = "n-1 held", expired: str | None = None) -> dict:
        """A lock-helper state over real marker files (the runner and the helper are not run): the handshake assert is decided by what the markers say."""
        d = Path(tempfile.mkdtemp(prefix="hs-", dir=self.tmp))
        mk = {"paused": d / "p.marker", "expired": d / self._EXPIRED_MARKER, "ack": d / self._ACK_MARKER}
        for k, v in (("paused", paused), ("ack", ack), ("expired", expired)):
            if v is not None:
                mk[k].write_text(v, encoding="utf-8")
        return {"outcome": outcome, "released": False, "give_up_s": 1, "markers": mk}

    def test_r4_b1_the_handshake_assert_accepts_only_a_lock_the_paused_runner_acknowledged_with_its_own_nonce(self) -> None:
        self._assert_handshake(self._handshake_state(), "CLAIM_TAKEN")   # the control: locked, acknowledged with the runner's nonce, no expiry
        cases = {
            "the runner's pause expired and the helper locked after it (sol r3 B1)": self._handshake_state(expired="n-1 expired", ack=None),
            "the pause expired although an ack exists": self._handshake_state(expired="n-1 expired"),
            "locked with no acknowledgment at all": self._handshake_state(ack=None),
            "an ack carrying another attempt's nonce": self._handshake_state(ack="n-0 held"),
            "an ack that is not a held lock": self._handshake_state(ack="n-1 free"),
            "no pause marker to bind the ack to": self._handshake_state(paused=None),
            "the helper itself refused the expired pause": self._handshake_state(outcome="pause-expired", expired="n-1 expired", ack=None),
        }
        for label, st in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(AssertionError):
                    self._assert_handshake(st, "CLAIM_TAKEN")

    def test_r4_b1_a_lock_helper_that_sees_the_pause_marker_after_the_runners_pause_expired_takes_no_lock(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        log = self.run_dir / "batch.log"
        log.write_text("", encoding="utf-8")
        paused, locked = self.case / "runner-paused.marker", self.case / "log-locked.marker"
        paused.write_text("n-1", encoding="utf-8")
        (self.case / self._EXPIRED_MARKER).write_text("n-1 expired", encoding="utf-8")   # the runner gave up and carried on; its pause marker is left behind
        done, t, state = self._lock_log_while_the_runner_is_paused(log, paused, locked, give_up_s=5)
        try:
            t.join(10)
            self.assertEqual(state["outcome"], "pause-expired")
            self.assertFalse(locked.exists(), "a pause the runner had already given up on was locked on")
            with open(log, "a", encoding="utf-8") as f:   # PermissionError here = the lock was taken
                f.write("")
            with self.assertRaises(AssertionError):
                self._assert_handshake(state, "CLAIM_TAKEN")
        finally:
            done.set()
            t.join(5)

    def test_r4_b1_a_helper_delayed_past_the_runners_pause_is_refused_and_the_runner_resumes_without_a_lock(self) -> None:
        # the real runner pauses for 2 s here (60 s in the other tests); the helper first sees the pause marker at 12 s (the runner starts its pause a few seconds in), the case sol r3 B1 describes at t=61 s of 60 s. The runner has
        # recorded its expiry by then: no lock is taken, no acknowledgment exists, and the attempt is refused by name.
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "batch.log").write_text("", encoding="utf-8")
        args, e = self.plan_cmd(self.ab_block_plan(), "-PlanOrder", "Interleave")
        runner, paused, locked = self._runner_paused_at_claim_taken(pause_s=2)
        args[args.index("-File") + 1] = str(runner)
        t0 = time.monotonic()

        class Delayed:
            parent = paused.parent

            def exists(self) -> bool:
                return time.monotonic() - t0 >= 12 and paused.exists()

        done, t, state = self._lock_log_while_the_runner_is_paused(self.run_dir / "batch.log", Delayed(), locked, give_up_s=60)
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=90, env=e)
            t.join(30)
        finally:
            done.set()
            t.join(5)
        state["markers"]["paused"] = paused
        self.assertEqual(state["outcome"], "pause-expired", r.stdout + r.stderr)
        self.assertFalse(locked.exists(), "no lock was taken for an expired pause")
        self.assertTrue(paused.exists() and (self.case / self._EXPIRED_MARKER).read_text(encoding="utf-8").strip() == paused.read_text(encoding="utf-8").strip() + " expired",
                        "the runner recorded that its pause expired with no lock, under its own nonce")
        self.assertFalse((self.case / self._ACK_MARKER).exists(), "an expired pause is never acknowledged")
        with self.assertRaises(AssertionError):
            self._assert_handshake(state, "CLAIM_TAKEN", r)
        self.assertEqual(r.returncode, 0, "the runner resumed without a lock and the batch ran normally\n" + r.stdout + r.stderr)

    def test_r4_adv_a2_a_lock_the_runner_looks_at_after_its_pause_ran_out_is_expired_not_acknowledged(self) -> None:
        # adversary r4 A2: the pause loop checked the clock BEFORE the look, so a look that returned after the window (here 2 s into a 1 s pause) still counted as seen, was probed and
        # acknowledged, and no expiry was recorded. The clock is read after the look, and again after the probe. The pause snippet runs alone; Test-Path is a function that returns late.
        self.run_dir.mkdir(parents=True, exist_ok=True)
        log = self.run_dir / "batch.log"
        log.write_text("", encoding="utf-8")
        snippet = self._pause_snippet("CLAIM_TAKEN", 1)
        script = self.case / "pause-snippet.ps1"
        script.write_text("function Test-Path { param([string]$LiteralPath) Start-Sleep -Seconds 2; $true }\n"
                          f"$batchLog = '{psq(log)}'\n$Message = 'CLAIM_TAKEN batch=1'\n" + snippet + "\n'done'\n", encoding="utf-8-sig")
        r = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script)], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        expired, ack = self.case / self._EXPIRED_MARKER, self.case / self._ACK_MARKER
        self.assertTrue(expired.exists(), "a look that returned after the window is an expired pause")
        self.assertFalse(ack.exists(), "a late look is never acknowledged")

    def test_r4_b1_a_prompt_helper_is_acknowledged_by_the_runner_with_its_own_nonce_while_the_log_is_held(self) -> None:
        # the successful-handshake control on the real runner: paused, locked, the runner probes batch.log (held), writes `<nonce> held`, and only then resumes
        r, state = self._run_plan_with_log_held_at("CLAIM_TAKEN")
        self._assert_handshake(state, "CLAIM_TAKEN", r)
        mk = state["markers"]
        self.assertEqual(mk["ack"].read_text(encoding="utf-8").strip(), mk["paused"].read_text(encoding="utf-8").strip() + " held")
        self.assertFalse(mk["expired"].exists())
        self.assertEqual(r.returncode, 12, r.stdout + r.stderr)

    def test_r3_b2_the_lock_is_let_go_when_the_locked_marker_cannot_be_written(self) -> None:
        # sol r2b hardening: an exception writing the marker after CreateFileW skipped CloseHandle. The marker path is a directory, so the write raises; the handle is closed anyway.
        self.run_dir.mkdir(parents=True, exist_ok=True)
        log = self.run_dir / "batch.log"
        log.write_text("", encoding="utf-8")
        paused, locked = self.case / "runner-paused.marker", self.case / "log-locked.marker"
        paused.write_text("paused", encoding="utf-8")
        locked.mkdir()
        raised: list = []
        hook = threading.excepthook
        threading.excepthook = lambda a: raised.append(a.exc_type)
        try:
            done, t, *_ = self._lock_log_while_the_runner_is_paused(log, paused, locked, give_up_s=5)
            t.join(10)
        finally:
            threading.excepthook = hook
            done.set()
        self.assertTrue(raised, "the marker write raised (the case under test)")
        with open(log, "a", encoding="utf-8") as f:   # PermissionError here = the handle leaked
            f.write("")

    def test_r7_adv_a1_a_direct_log_line_that_cannot_be_appended_is_counted_and_said(self) -> None:
        # adversary r7 A1: L's own append threw into the batch catch without Register-LogFailure, so a held batch.log at CLAIM_TAKEN turned a healthy batch into
        # BATCH_ERROR / exit 12 (unchanged, r6's typed stop) with logWriteFailures=0 and no BATCH_LOG_FAILED line. Every append failure is counted and said, and counted once.
        anchor = "Add-Content -LiteralPath $batchLog -Value $line"
        runner = self._patched_runner(anchor, "if ($Message -like 'CLAIM_TAKEN*') { throw [IO.IOException]::new('locked') }; " + anchor)
        args, e = self.plan_cmd(self.ab_block_plan(), "-PlanOrder", "Interleave")
        args[args.index("-File") + 1] = str(runner)
        r = subprocess.run(args, capture_output=True, text=True, timeout=120, env=e)
        self.assertEqual(r.returncode, 12, r.stdout + r.stderr)
        res = batch_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"]), (12, "BATCH_ERROR"))
        self.assertEqual(res["logWriteFailures"], 1, "one failed append is one failure, not two")
        [b] = self.batches()
        self.assertEqual(b["logWriteFailures"], 1)
        self.assertIn("BATCH_LOG_FAILED the batch log cannot be written (locked)", r.stderr)

    def test_r7_adv_a2_a_leg_whose_process_cannot_be_created_stays_not_run(self) -> None:
        # adversary r7 A2: the in-flight marker was set before Process.Start, so a command line past Windows' 32767 characters (an unrestricted -SheetCopyDir) failed at
        # process creation and the leg that never started was recorded LEG_FAILED. A launch that throws a Win32Exception started nothing: the leg stays NOT_RUN.
        # The 40000-character -SheetCopyDir cannot be fed to the runner itself (its own command line is past the limit), so the launch is made to fail the same way:
        # Process.Start on a file that does not exist throws the same Win32Exception, before any child exists.
        anchor = "Invoke-VenueBoundedProcess -FilePath $pwshExe -ArgumentList $lArgs"
        runner = self._patched_runner(anchor, "Invoke-VenueBoundedProcess -FilePath (Join-Path $RunDir 'no-such-pwsh.exe') -ArgumentList $lArgs")
        args, e = self.plan_cmd(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]))
        args[args.index("-File") + 1] = str(runner)
        r = subprocess.run(args, capture_output=True, text=True, timeout=120, env=e)
        self.assertEqual(r.returncode, 12, r.stdout + r.stderr)
        self.assertEqual(self.leg_calls(), [], "no child was started")
        res = batch_result(r.stdout)
        self.assertEqual([(l["name"], l["status"]) for l in res["legs"]], [("01-fx-a-cpu-A", "NOT_RUN")], res["legs"])
        self.assertEqual((res["ran"], res["failed"], res["notRun"], res["stopReason"]), (0, 0, 1, "BATCH_ERROR"))

    def test_r7_adv2_a1_a_leg_spec_that_is_a_file_the_run_writes_is_refused_before_the_claim(self) -> None:
        # adversary r7 pass 2 A1: preflight accepted a spec at <RunDir>\leg-plan.json, the claim keeper then wrote the run plan over it, and the leg failed after the lease
        # opened with the accepted input destroyed. A spec that is one of the runner's own output files is refused with the whole plan, before any claim.
        self.run_dir.mkdir(parents=True, exist_ok=True)
        spec = self.run_dir / "leg-plan.json"
        spec.write_text(json.dumps({"schema": "mlv-app/dual-venue-leg/v1", "card": "TEST-CARD", "legId": "fx-a", "backends": ["cpu"], "scaleFactor": 1}), encoding="utf-8")
        before = spec.read_text(encoding="utf-8")
        r = self.run_plan(self.write_plan([self.item(spec, "A", self.commit_a)]))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=LEG_SPEC_IS_RUN_OUTPUT item=1 ", r.stderr)
        self.assertEqual(self.kinds(), [], "nothing is submitted: no open, no probe, no leg")
        self.assertFalse(self.claim_path.exists(), "no claim was written")
        self.assertEqual(spec.read_text(encoding="utf-8"), before, "the accepted input is untouched")

    # r9 (Sol r8 / Dot B7 / producer OPEN A1; card LEASE-BATCH-RUNDIR-OUTPUT-COLLISION-GUARD-1): the r7 guard named five of the runner's outputs and missed quiet-*.out.txt and
    # gate.log, so a valid spec there passed preflight and was overwritten after the lease opened. One case per output name the runner writes under -RunDir, plus the two
    # directories it owns (batch-jobs\, quiet\). A new output that is not in the runner's one list is a failing case here, not a quiet hole.
    RUN_OUTPUT_PATHS = (
        "quiet-01-fx-a-cpu-A-leg.out.txt", "quiet-caps.out.txt", "leg-01-fx-a-cpu-A-b01-a1.out.txt", "gate.log", "batch-01.json", "batches.jsonl", "batch.log",
        "leg-plan.json", "batch-jobs/lease-batch-open-x.job.ps1", "quiet/venue-quiet-probe-x.json",
    )

    def _valid_leg_spec_text(self) -> str:
        return json.dumps({"schema": "mlv-app/dual-venue-leg/v1", "card": "TEST-CARD", "legId": "fx-a", "backends": ["cpu"], "scaleFactor": 1})

    def test_r9_a_leg_spec_at_any_runner_output_path_is_refused_before_the_claim(self) -> None:
        for leaf in self.RUN_OUTPUT_PATHS:
            with self.subTest(output=leaf):
                for p in (self.events, self.leg_args, self.claim_path):
                    p.unlink(missing_ok=True)
                shutil.rmtree(self.run_dir, ignore_errors=True)
                spec = self.run_dir / leaf
                spec.parent.mkdir(parents=True, exist_ok=True)
                spec.write_text(self._valid_leg_spec_text(), encoding="utf-8")
                before = spec.read_text(encoding="utf-8")
                r = self.run_plan(self.write_plan([self.item(spec, "A", self.commit_a)]))
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn("LEG_PLAN_REFUSED reason=LEG_SPEC_IS_RUN_OUTPUT item=1 ", r.stderr)
                self.assertEqual(self.kinds(), [], "nothing is submitted: no open, no probe, no leg")
                self.assertEqual(self.leg_calls(), [])
                self.assertFalse(self.claim_path.exists(), "no claim was written")
                self.assertEqual(spec.read_text(encoding="utf-8"), before, "the accepted input is untouched")

    def test_r9_a_plan_file_at_a_runner_output_path_is_refused_before_the_claim(self) -> None:
        # the plan is an input too: leg-plan.json is written over it once the claim is held, and batch.log / gate.log are appended to
        for leaf in ("leg-plan.json", "batch.log", "gate.log", "quiet-01-fx-a-cpu-A-leg.out.txt", "batch-jobs/plan.json"):
            with self.subTest(output=leaf):
                for p in (self.events, self.leg_args, self.claim_path):
                    p.unlink(missing_ok=True)
                shutil.rmtree(self.run_dir, ignore_errors=True)
                plan = self.run_dir / leaf
                plan.parent.mkdir(parents=True, exist_ok=True)
                plan.write_text(json.dumps({"schema": "mlv-app/venue-lease-leg-plan/v1", "legs": [self.item(self.specs[0], "A", self.commit_a)]}), encoding="utf-8")
                before = plan.read_text(encoding="utf-8")
                r = self.run_plan(plan)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn("LEG_PLAN_REFUSED reason=LEG_PLAN_IS_RUN_OUTPUT", r.stderr)
                self.assertEqual(self.kinds(), [], "nothing is submitted: no open, no probe, no leg")
                self.assertFalse(self.claim_path.exists(), "no claim was written")
                self.assertEqual(plan.read_text(encoding="utf-8"), before, "the accepted input is untouched")

    def test_r9_a_scalar_leg_spec_at_a_runner_output_path_is_refused_before_the_claim(self) -> None:
        for leaf in ("gate.log", "quiet-01-fx-a-cpu-A-leg.out.txt", "leg-plan.json", "batch-03.json"):
            with self.subTest(output=leaf):
                for p in (self.events, self.claim_path):
                    p.unlink(missing_ok=True)
                shutil.rmtree(self.run_dir, ignore_errors=True)
                spec = self.run_dir / leaf
                spec.parent.mkdir(parents=True, exist_ok=True)
                spec.write_text(self._valid_leg_spec_text(), encoding="utf-8")
                before = spec.read_text(encoding="utf-8")
                r = self.run_batch([spec])
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn("BATCH_USAGE LEG_SPEC_IS_RUN_OUTPUT", r.stderr)
                self.assertEqual(self.kinds(), [], "nothing is submitted: no open, no probe, no leg")
                self.assertFalse(self.claim_path.exists(), "no claim was written")
                self.assertEqual(spec.read_text(encoding="utf-8"), before, "the accepted input is untouched")

    def test_r9_a_spec_in_the_run_dir_under_another_name_is_not_refused(self) -> None:
        # the guard names the runner's outputs, not the directory: a spec kept beside them under any other name still runs
        shutil.rmtree(self.run_dir, ignore_errors=True)
        self.run_dir.mkdir(parents=True)
        spec = self.run_dir / "gate.log.spec.json"
        spec.write_text(self._valid_leg_spec_text(), encoding="utf-8")
        r = self.run_plan(self.write_plan([self.item(spec, "A", self.commit_a)]))
        self.assertNotIn("LEG_SPEC_IS_RUN_OUTPUT", r.stderr)
        self.assertNotEqual(r.returncode, 2, r.stdout + r.stderr)

    # r10 (adversary r9 A1): the r9 guard compared LEXICAL parents, so an input reached through a junction that targets -RunDir (or an ancestor of it) passed preflight and was
    # overwritten after the claim. One case per call site of the one helper (the plan file, a plan item's spec, a scalar spec), the aliases that matter (the input, an ancestor
    # of it, -RunDir itself, a runner-owned directory that is a link) and the fail-closed case. Each asserts the physical file is byte-unchanged and nothing was submitted.
    def _junction(self, link: Path, target: Path) -> None:
        target.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def _assert_alias_refused(self, r: subprocess.CompletedProcess, physical: Path, before: str, marker: str) -> None:
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn(marker, r.stderr)
        self.assertEqual(self.kinds(), [], "nothing is submitted: no open, no probe, no leg")
        self.assertEqual(self.leg_calls(), [])
        self.assertFalse(self.claim_path.exists(), "no claim was written")
        self.assertEqual(physical.read_text(encoding="utf-8"), before, "the physical file is byte-unchanged")

    def _fresh_run_dir(self) -> None:
        for p in (self.events, self.leg_args, self.claim_path):
            p.unlink(missing_ok=True)
        shutil.rmtree(self.run_dir, ignore_errors=True)
        self.run_dir.mkdir(parents=True)

    def test_r10_a_leg_spec_reached_through_a_junction_to_the_run_dir_is_refused_before_the_claim(self) -> None:
        for leaf in ("leg-plan.json", "gate.log", "batch.log", "batch-jobs/x.json"):
            with self.subTest(output=leaf):
                self._fresh_run_dir()
                alias = self.case / "alias"
                if alias.exists():
                    os.rmdir(alias)
                self._junction(alias, self.run_dir)
                spec = self.run_dir / leaf
                spec.parent.mkdir(parents=True, exist_ok=True)
                spec.write_text(self._valid_leg_spec_text(), encoding="utf-8")
                before = spec.read_text(encoding="utf-8")
                r = self.run_plan(self.write_plan([self.item(alias / leaf, "A", self.commit_a)]))
                self._assert_alias_refused(r, spec, before, "LEG_PLAN_REFUSED reason=LEG_SPEC_IS_RUN_OUTPUT item=1 ")
                os.rmdir(alias)

    def test_r10_a_leg_spec_reached_through_a_junction_to_an_ancestor_of_the_run_dir_is_refused(self) -> None:
        self._fresh_run_dir()
        alias = self.tmp / f"up-{self.case.name}"
        self._junction(alias, self.case)
        try:
            spec = self.run_dir / "leg-plan.json"
            spec.write_text(self._valid_leg_spec_text(), encoding="utf-8")
            before = spec.read_text(encoding="utf-8")
            r = self.run_plan(self.write_plan([self.item(alias / "run" / "leg-plan.json", "A", self.commit_a)]))
            self._assert_alias_refused(r, spec, before, "LEG_PLAN_REFUSED reason=LEG_SPEC_IS_RUN_OUTPUT item=1 ")
        finally:
            os.rmdir(alias)

    def test_r10_a_plan_file_reached_through_a_junction_to_the_run_dir_is_refused_before_the_claim(self) -> None:
        for leaf in ("leg-plan.json", "batch.log", "batch-jobs/plan.json"):
            with self.subTest(output=leaf):
                self._fresh_run_dir()
                alias = self.case / "alias"
                if alias.exists():
                    os.rmdir(alias)
                self._junction(alias, self.run_dir)
                plan = self.run_dir / leaf
                plan.parent.mkdir(parents=True, exist_ok=True)
                plan.write_text(json.dumps({"schema": "mlv-app/venue-lease-leg-plan/v1", "legs": [self.item(self.specs[0], "A", self.commit_a)]}), encoding="utf-8")
                before = plan.read_text(encoding="utf-8")
                r = self.run_plan(alias / leaf)
                self._assert_alias_refused(r, plan, before, "LEG_PLAN_REFUSED reason=LEG_PLAN_IS_RUN_OUTPUT")
                os.rmdir(alias)

    def test_r10_a_scalar_leg_spec_reached_through_a_junction_to_the_run_dir_is_refused_before_the_claim(self) -> None:
        for leaf in ("gate.log", "leg-plan.json", "quiet/x.json"):
            with self.subTest(output=leaf):
                self._fresh_run_dir()
                alias = self.case / "alias"
                if alias.exists():
                    os.rmdir(alias)
                self._junction(alias, self.run_dir)
                spec = self.run_dir / leaf
                spec.parent.mkdir(parents=True, exist_ok=True)
                spec.write_text(self._valid_leg_spec_text(), encoding="utf-8")
                before = spec.read_text(encoding="utf-8")
                r = self.run_batch([alias / leaf])
                self._assert_alias_refused(r, spec, before, "BATCH_USAGE LEG_SPEC_IS_RUN_OUTPUT")
                os.rmdir(alias)

    def test_r10_a_run_dir_given_as_a_junction_alias_still_refuses_the_physical_output(self) -> None:
        # the other direction: -RunDir is the alias, the input is spelled with the physical path
        real = self.case / "real-run"
        real.mkdir()
        spec = real / "gate.log"
        spec.write_text(self._valid_leg_spec_text(), encoding="utf-8")
        before = spec.read_text(encoding="utf-8")
        alias = self.case / "run-alias"
        self._junction(alias, real)
        try:
            self.run_dir = alias
            r = self.run_plan(self.write_plan([self.item(spec, "A", self.commit_a)]))
            self._assert_alias_refused(r, spec, before, "LEG_PLAN_REFUSED reason=LEG_SPEC_IS_RUN_OUTPUT item=1 ")
        finally:
            os.rmdir(alias)

    def test_r10_a_spec_inside_a_runner_owned_directory_that_is_a_link_is_refused(self) -> None:
        # <RunDir>\quiet is a junction elsewhere: Wait-VenueQuiet -WorkDir writes into the target, so a spec there is a runner output
        self._fresh_run_dir()
        elsewhere = self.case / "elsewhere"
        self._junction(self.run_dir / "quiet", elsewhere)
        spec = elsewhere / "venue-quiet-probe-x.json"
        spec.write_text(self._valid_leg_spec_text(), encoding="utf-8")
        before = spec.read_text(encoding="utf-8")
        r = self.run_plan(self.write_plan([self.item(spec, "A", self.commit_a)]))
        self._assert_alias_refused(r, spec, before, "LEG_PLAN_REFUSED reason=LEG_SPEC_IS_RUN_OUTPUT item=1 ")

    # r10 second pass (adversary A1): a HARD link to a runner output has its own final path and is not a reparse point, so a path comparison, however physical, accepts it
    # while the runner overwrites the same file through the output's name. The guard also compares file identity (volume + file index).
    def _hard_linked(self, output: Path, name: Path) -> str:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(self._valid_leg_spec_text(), encoding="utf-8")
        name.parent.mkdir(parents=True, exist_ok=True)
        os.link(output, name)
        return output.read_text(encoding="utf-8")

    def test_r10_a_plan_item_spec_hard_linked_to_a_runner_output_is_refused_before_the_claim(self) -> None:
        for leaf in ("leg-plan.json", "gate.log", "batch-jobs/x.json", "quiet/x.json"):
            with self.subTest(output=leaf):
                self._fresh_run_dir()
                safe = self.case / "safe" / "fx-a.json"
                safe.unlink(missing_ok=True)
                before = self._hard_linked(self.run_dir / leaf, safe)
                r = self.run_plan(self.write_plan([self.item(safe, "A", self.commit_a)]))
                self._assert_alias_refused(r, safe, before, "LEG_PLAN_REFUSED reason=LEG_SPEC_IS_RUN_OUTPUT item=1 ")
                self.assertIn("is a hard link to", r.stderr)

    def test_r10_a_plan_file_hard_linked_to_a_runner_output_is_refused_before_the_claim(self) -> None:
        self._fresh_run_dir()
        plan = self.case / "safe-plan.json"
        plan.unlink(missing_ok=True)
        output = self.run_dir / "leg-plan.json"
        output.write_text(json.dumps({"schema": "mlv-app/venue-lease-leg-plan/v1", "legs": [self.item(self.specs[0], "A", self.commit_a)]}), encoding="utf-8")
        os.link(output, plan)
        before = plan.read_text(encoding="utf-8")
        r = self.run_plan(plan)
        self._assert_alias_refused(r, plan, before, "LEG_PLAN_REFUSED reason=LEG_PLAN_IS_RUN_OUTPUT")

    def test_r10_a_scalar_spec_hard_linked_to_a_runner_output_is_refused_before_the_claim(self) -> None:
        self._fresh_run_dir()
        safe = self.case / "safe" / "fx-a.json"
        safe.unlink(missing_ok=True)
        before = self._hard_linked(self.run_dir / "gate.log", safe)
        r = self.run_batch([safe])
        self._assert_alias_refused(r, safe, before, "BATCH_USAGE LEG_SPEC_IS_RUN_OUTPUT")

    def test_r10_a_hard_linked_spec_whose_other_name_is_not_a_runner_output_is_not_refused(self) -> None:
        # more than one name is not the test: the other name must be a file this run writes
        self._fresh_run_dir()
        safe = self.case / "safe" / "fx-a.json"
        safe.unlink(missing_ok=True)
        self._hard_linked(self.run_dir / "gate.log.spec.json", safe)
        r = self.run_plan(self.write_plan([self.item(safe, "A", self.commit_a)]))
        self.assertNotIn("IS_RUN_OUTPUT", r.stderr)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_r10_an_input_whose_physical_path_cannot_be_resolved_is_refused_before_the_claim(self) -> None:
        # fail closed: the resolver is made to fail (as it does for a path the file system will not open); the plan file, a plan item's spec and a scalar spec each refuse
        anchor = "[VenueLeaseBatchFinalPath]::Resolve([IO.Path]::GetFullPath($Path))"
        runner = self._patched_runner(anchor, "throw 'injected: the path will not open'")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        plan = self.write_plan([self.item(self.specs[0], "A", self.commit_a)])
        spec_text = self.specs[0].read_text(encoding="utf-8")
        args, e = self.plan_cmd(plan)
        args[args.index("-File") + 1] = str(runner)
        r = subprocess.run(args, capture_output=True, text=True, timeout=120, env=e)
        self._assert_alias_refused(r, self.specs[0], spec_text, "LEG_PLAN_REFUSED reason=LEG_PLAN_IS_RUN_OUTPUT")
        self.assertIn("cannot be resolved to a physical path", r.stderr)
        args, e = self.batch_cmd([self.specs[0]])
        args[args.index("-File") + 1] = str(runner)
        r = subprocess.run(args, capture_output=True, text=True, timeout=120, env=e)
        self._assert_alias_refused(r, self.specs[0], spec_text, "BATCH_USAGE LEG_SPEC_IS_RUN_OUTPUT")
        self.assertIn("cannot be resolved to a physical path", r.stderr)

    def test_r10_a_junction_that_does_not_reach_the_run_dir_is_not_refused(self) -> None:
        # the guard resolves paths, it does not distrust links: a spec kept behind a junction elsewhere still runs
        self._fresh_run_dir()
        kept = self.case / "kept-specs"
        self._junction(self.case / "spec-alias", kept)
        shutil.copy2(self.specs[0], kept / "fx-a.json")
        r = self.run_plan(self.write_plan([self.item(self.case / "spec-alias" / "fx-a.json", "A", self.commit_a)]))
        self.assertNotIn("IS_RUN_OUTPUT", r.stderr)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    # r11 (sol r10 blockers 1-3 = producer OPEN r10-A1..A3). Round one item 2: every input is validated BEFORE the lease opens, and no accepted input is overwritten, deleted or made
    # unusable later. A1: the runner's outputs are not only <RunDir>\ files: VenueChain.psm1 also writes <ClaimDir>\bachelor.claim.json (deleted as a stale claim when unparseable),
    # <ClaimDir>\bachelor.lease-ledger.jsonl (appended to) and <ClaimDir>\bachelor.tickets\ (ticket files). A2: every generated output name must be openable. A3: the guard runs again
    # once the claim is held, because an alias can appear while the batch waits on a foreign claim.
    CLAIM_DIR_OUTPUTS = ("bachelor.claim.json", "bachelor.lease-ledger.jsonl", "bachelor.tickets/0000000000000000001-p1-1.ticket.json")

    def _claim_dir_input(self, leaf: str, age_s: int = 31) -> tuple[Path, str]:
        spec = self.claims / leaf
        spec.parent.mkdir(parents=True, exist_ok=True)
        spec.write_text(self._valid_leg_spec_text(), encoding="utf-8")
        old = time.time() - age_s
        os.utime(spec, (old, old))
        return spec, spec.read_text(encoding="utf-8")

    def _assert_nothing_submitted_input_intact(self, r: subprocess.CompletedProcess, path: Path, before: str, marker: str) -> None:
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn(marker, r.stderr)
        self.assertEqual(self.kinds(), [], "nothing is submitted: no open, no probe, no leg")
        self.assertEqual(self.leg_calls(), [])
        self.assertTrue(path.exists(), "the accepted input was not removed")
        self.assertEqual(path.read_text(encoding="utf-8"), before, "the accepted input is byte-unchanged")

    def test_r11_a1_a_plan_item_spec_at_a_claim_dir_file_is_refused_when_run_dir_is_the_claim_dir(self) -> None:
        # sol r10 blocker 1, literal: RunDir=ClaimDir, a valid spec at bachelor.claim.json (age 31 s: past the 30 s grace, so the claim reader types it STALE/unreadable and
        # Invoke-VenueClaimAttempt removes it) and at its ledger sibling (Add-VenueLeaseLedger appends to it).
        for leaf in self.CLAIM_DIR_OUTPUTS:
            with self.subTest(output=leaf):
                self._fresh_run_dir()
                shutil.rmtree(self.claims, ignore_errors=True)
                self.run_dir = self.claims
                spec, before = self._claim_dir_input(leaf)
                r = self.run_plan(self.write_plan([self.item(spec, "A", self.commit_a)]))
                self._assert_nothing_submitted_input_intact(r, spec, before, "LEG_PLAN_REFUSED reason=LEG_SPEC_IS_RUN_OUTPUT item=1 ")

    def test_r11_a1_a_claim_dir_file_is_refused_whatever_the_run_dir_is(self) -> None:
        # the claim dir is a runner output location in its own right, not only when it is also -RunDir
        for leaf in self.CLAIM_DIR_OUTPUTS:
            with self.subTest(output=leaf):
                self._fresh_run_dir()
                shutil.rmtree(self.claims, ignore_errors=True)
                spec, before = self._claim_dir_input(leaf)
                r = self.run_plan(self.write_plan([self.item(spec, "A", self.commit_a)]))
                self._assert_nothing_submitted_input_intact(r, spec, before, "LEG_PLAN_REFUSED reason=LEG_SPEC_IS_RUN_OUTPUT item=1 ")
                shutil.rmtree(self.claims, ignore_errors=True)
                spec, before = self._claim_dir_input(leaf)
                r = self.run_batch([spec])
                self._assert_nothing_submitted_input_intact(r, spec, before, "BATCH_USAGE LEG_SPEC_IS_RUN_OUTPUT")

    def test_r11_a1_a_plan_file_at_a_claim_dir_file_is_refused_before_the_claim(self) -> None:
        for leaf in ("bachelor.claim.json", "bachelor.lease-ledger.jsonl"):
            with self.subTest(output=leaf):
                self._fresh_run_dir()
                shutil.rmtree(self.claims, ignore_errors=True)
                self.run_dir = self.claims
                plan = self.claims / leaf
                plan.parent.mkdir(parents=True, exist_ok=True)
                plan.write_text(json.dumps({"schema": "mlv-app/venue-lease-leg-plan/v1", "legs": [self.item(self.specs[0], "A", self.commit_a)]}), encoding="utf-8")
                old = time.time() - 31
                os.utime(plan, (old, old))
                before = plan.read_text(encoding="utf-8")
                r = self.run_plan(plan)
                self._assert_nothing_submitted_input_intact(r, plan, before, "LEG_PLAN_REFUSED reason=LEG_PLAN_IS_RUN_OUTPUT")

    def test_r11_a1_a_spec_hard_linked_to_a_claim_dir_file_is_refused_before_the_claim(self) -> None:
        for leaf in ("bachelor.claim.json", "bachelor.lease-ledger.jsonl"):
            with self.subTest(output=leaf):
                self._fresh_run_dir()
                shutil.rmtree(self.claims, ignore_errors=True)
                safe = self.case / "safe" / "fx-a.json"
                safe.unlink(missing_ok=True)
                before = self._hard_linked(self.claims / leaf, safe)
                r = self.run_plan(self.write_plan([self.item(safe, "A", self.commit_a)]))
                self._assert_nothing_submitted_input_intact(r, safe, before, "LEG_PLAN_REFUSED reason=LEG_SPEC_IS_RUN_OUTPUT item=1 ")
                self.assertIn("is a hard link to", r.stderr)

    def test_r11_a1_a_spec_in_the_claim_dir_under_another_name_is_not_refused(self) -> None:
        self._fresh_run_dir()
        shutil.rmtree(self.claims, ignore_errors=True)
        self.run_dir = self.claims
        spec, _ = self._claim_dir_input("fx-a.spec.json")
        r = self.run_plan(self.write_plan([self.item(spec, "A", self.commit_a)]))
        self.assertNotIn("IS_RUN_OUTPUT", r.stderr)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def _long_name_spec(self, basename_len: int) -> Path:
        # a spec whose legId is not a usable leg name (82 chars, over the 81 the runner takes), so the file's own basename becomes the leg name (sol r10 blocker 2, literal)
        # (kept off the deep test temp folder: the spec's own path must stay under MAX_PATH, so the only over-long name is the one the RUNNER generates from it)
        d = None
        for root in (os.environ.get("PUBLIC", ""), (os.environ.get("SystemDrive", "C:") + "\\"), tempfile.gettempdir()):
            try:
                cand = Path(tempfile.mkdtemp(prefix="lb", dir=root)) if root else None
            except OSError:
                continue
            if cand is not None and len(str(cand / ("a" * basename_len + ".json"))) < 259:
                d = cand
                break
            if cand is not None:
                shutil.rmtree(cand, ignore_errors=True)
        if d is None:
            self.skipTest("no folder is shallow enough to keep the spec's own path under MAX_PATH")
        self.addCleanup(shutil.rmtree, d, True)
        spec = d / ("a" * basename_len + ".json")
        spec.write_text(json.dumps({"schema": "mlv-app/dual-venue-leg/v1", "card": "TEST-CARD", "legId": "x" * 82, "backends": ["cpu"], "scaleFactor": 1}), encoding="utf-8")
        return spec

    def test_r11_a2_a_generated_output_name_over_the_volume_limit_is_refused_before_the_claim(self) -> None:
        # 205-char basename + 32-char arm: the quiet output is 266 characters, the volume's component limit 255; redirecting into it failed AFTER the lease opened.
        spec = self._long_name_spec(205)
        r = self.run_plan(self.write_plan([self.item(spec, "A" * 32, self.commit_a)]))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=OUTPUT_NAME_TOO_LONG item=1 ", r.stderr)
        self.assertEqual(self.kinds(), [], "nothing is submitted: no open, no probe, no leg")
        self.assertFalse(self.claim_path.exists(), "no claim was written")

    def test_r11_a2_the_scalar_form_refuses_a_generated_output_name_over_the_limit_too(self) -> None:
        spec = self._long_name_spec(220)   # no -Backend: the leg name ends in "-spec", and quiet-01-<220>-spec-b9999-a999999.out.txt is 256 characters
        r = self.run_batch([spec])
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("BATCH_USAGE OUTPUT_NAME_TOO_LONG", r.stderr)
        self.assertEqual(self.kinds(), [], "nothing is submitted: no open, no probe, no leg")
        self.assertFalse(self.claim_path.exists(), "no claim was written")

    def test_r11_a2_a_short_name_with_a_full_length_arm_still_runs(self) -> None:
        r = self.run_plan(self.write_plan([self.item(self.specs[0], "A" * 32, self.commit_a)]))
        self.assertNotIn("OUTPUT_NAME_TOO_LONG", r.stderr)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def _foreign_live_claim(self) -> None:
        # a LIVE claim of another owner: a real sleeping process, its real start time (Get-VenueClaimStaleReason finds a live owner and the claim not yet expired)
        holder = subprocess.Popen([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "Start-Sleep -Seconds 300"])
        self.addCleanup(lambda: holder.poll() is None and subprocess.run(["taskkill", "/PID", str(holder.pid), "/T", "/F"], capture_output=True, text=True, timeout=60))
        t = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", f"(Get-Process -Id {holder.pid}).StartTime.ToUniversalTime().ToString('o')"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(t.returncode, 0, t.stdout + t.stderr)
        now = datetime.now(timezone.utc)
        self.claims.mkdir(parents=True, exist_ok=True)
        self.claim_path.write_text(json.dumps({"schema": "mlv-app/venue-claim/v1", "venue": "bachelor", "ownerPid": holder.pid, "ownerCreatedUtc": t.stdout.strip(),
                                               "host": os.environ.get("COMPUTERNAME", ""), "runDir": str(self.case / "foreign"), "purpose": "foreign", "claimedUtc": utc_iso(now),
                                               "expiresUtc": utc_iso(now + timedelta(hours=1)), "nonce": "f" * 32}, separators=(",", ":")) + "\n", encoding="utf-8")

    def _wait_for_log(self, text: str, timeout_s: float = 90) -> None:
        end = time.monotonic() + timeout_s
        log = self.run_dir / "batch.log"
        while time.monotonic() < end:
            try:
                if text in log.read_text(encoding="utf-8"):
                    return
            except OSError:
                pass
            time.sleep(0.2)
        self.fail(f"{text!r} never reached {log}")

    def _open_release(self) -> Path:
        """The file whose existence ends the fake um-run's open wait (FAKE_OPEN_RELEASE); absent until the case releases the open."""
        rel = self.case / "open.release"
        rel.unlink(missing_ok=True)
        return rel

    def _wait_for_open_wait(self, timeout_s: float = 90) -> None:
        """Returns once the fake um-run is blocked in the open's wait (its OPENWAIT event, logged just before it blocks), so what the case does next lands inside that wait."""
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            try:
                if any(" OPENWAIT " in line for line in self.events_raw()):
                    return
            except OSError:
                pass
            time.sleep(0.05)
        self.fail("the open never reached its wait")

    def _alias_during_claim_wait(self, args: list[str], e: dict, spec: Path, alias: Path) -> tuple[int, str, str]:
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=e)
        try:
            self._wait_for_log("CLAIM_HELD")
            os.link(spec, alias)
            self.claim_path.unlink()   # the foreign owner lets go: this run takes the claim next poll
            so, se = p.communicate(timeout=240)
        finally:
            if p.poll() is None:
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, text=True, timeout=60)
                p.wait(timeout=60)
        return p.returncode, so, se

    def test_r11_a3_an_output_alias_added_while_the_batch_waits_on_a_foreign_claim_is_refused_after_the_wait(self) -> None:
        # sol r10 blocker 3, literal: ordinary single-link spec; while the runner waits (CLAIM_HELD) a hard link <RunDir>\leg-plan.json -> the spec appears; the foreign claim
        # is then released. The guard ran only before the wait, so leg-plan.json was written THROUGH the link, over the accepted spec, before the lease opened.
        self._fresh_run_dir()
        spec = self.case / "safe" / "fx-a.json"
        spec.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.specs[0], spec)
        before = spec.read_text(encoding="utf-8")
        self._foreign_live_claim()
        args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]))
        code, so, se = self._alias_during_claim_wait(args, e, spec, self.run_dir / "leg-plan.json")
        self.assertEqual(code, 2, so + se)
        self.assertIn("LEG_PLAN_REFUSED reason=LEG_SPEC_IS_RUN_OUTPUT item=1 ", se)
        self.assertEqual(self.kinds(), [], "no lease was opened")
        self.assertFalse(self.claim_path.exists(), "the claim taken after the wait was released")
        self.assertEqual(spec.read_text(encoding="utf-8"), before, "the accepted input is byte-unchanged")

    def test_r11_a3_a_scalar_spec_aliased_while_waiting_is_refused_after_the_wait(self) -> None:
        self._fresh_run_dir()
        spec = self.case / "safe" / "fx-a.json"
        spec.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.specs[0], spec)
        before = spec.read_text(encoding="utf-8")
        self._foreign_live_claim()
        args, e = self.batch_cmd([spec])
        code, so, se = self._alias_during_claim_wait(args, e, spec, self.run_dir / "gate.log")
        self.assertEqual(code, 2, so + se)
        self.assertIn("BATCH_USAGE LEG_SPEC_IS_RUN_OUTPUT", se)
        self.assertEqual(self.kinds(), [], "no lease was opened")
        self.assertFalse(self.claim_path.exists(), "the claim taken after the wait was released")
        self.assertEqual(spec.read_text(encoding="utf-8"), before, "the accepted input is byte-unchanged")

    # r12 (adversary r12 A1): the staged build manifest is an input too. A hard link <RunDir>\<output> -> the manifest made the runner write the output through it, over the accepted build.
    def _staged_manifest(self, commit: str) -> Path:
        return self.share / "cache" / f"playback-attr-3-cuda-{commit[:12]}-build.json"

    def test_r12_a1_a_staged_build_manifest_hard_linked_to_a_runner_output_is_refused_before_the_claim(self) -> None:
        for leaf in ("leg-plan.json", "gate.log", "batch.log", "batch-01.json", "batch-jobs/x.json", "quiet/x.json"):
            with self.subTest(output=leaf):
                self._fresh_run_dir()
                manifest = self._staged_manifest(self.commit_a)
                (self.run_dir / leaf).parent.mkdir(parents=True, exist_ok=True)
                os.link(manifest, self.run_dir / leaf)
                before = manifest.read_text(encoding="utf-8")
                r = self.run_plan(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]))
                self._assert_nothing_submitted_input_intact(r, manifest, before, "LEG_PLAN_REFUSED reason=BUILD_MANIFEST_IS_RUN_OUTPUT item=1 ")
                self.assertIn("is a hard link to", r.stderr)
                os.unlink(self.run_dir / leaf)

    def test_r12_a1_the_refusal_names_the_item_that_reads_the_build(self) -> None:
        self._fresh_run_dir()
        manifest = self._staged_manifest(self.commit_b)
        os.link(manifest, self.run_dir / "leg-plan.json")
        before = manifest.read_text(encoding="utf-8")
        r = self.run_plan(self.write_plan([self.item(self.specs[0], "A", self.commit_a), self.item(self.specs[1], "B", self.commit_b)]))
        self._assert_nothing_submitted_input_intact(r, manifest, before, "LEG_PLAN_REFUSED reason=BUILD_MANIFEST_IS_RUN_OUTPUT item=2 ")
        os.unlink(self.run_dir / "leg-plan.json")

    def test_r12_a1_a_staged_build_manifest_aliased_while_the_batch_waits_on_a_foreign_claim_is_refused_after_the_wait(self) -> None:
        self._fresh_run_dir()
        manifest = self._staged_manifest(self.commit_a)
        before = manifest.read_text(encoding="utf-8")
        self._foreign_live_claim()
        args, e = self.plan_cmd(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]))
        code, so, se = self._alias_during_claim_wait(args, e, manifest, self.run_dir / "leg-plan.json")
        self.assertEqual(code, 2, so + se)
        self.assertIn("LEG_PLAN_REFUSED reason=BUILD_MANIFEST_IS_RUN_OUTPUT item=1 ", se)
        self.assertEqual(self.kinds(), [], "no lease was opened")
        self.assertFalse(self.claim_path.exists(), "the claim taken after the wait was released")
        self.assertEqual(manifest.read_text(encoding="utf-8"), before, "the accepted build manifest is byte-unchanged")

    # r12 pass 2 (adversary r12 A1'): a link BELOW a runner-owned directory writes through to its target whatever that target's link count, so the root-only reparse listing missed it.
    def test_r12_a1b_a_spec_behind_a_junction_nested_in_a_runner_owned_directory_is_refused_before_the_claim(self) -> None:
        for owned in ("quiet", "batch-jobs"):
            with self.subTest(owned=owned):
                self._fresh_run_dir()
                (self.run_dir / owned).mkdir()
                elsewhere = self.case / f"elsewhere-{owned}"
                shutil.rmtree(elsewhere, ignore_errors=True)
                self._junction(self.run_dir / owned / "sub", elsewhere)
                spec = elsewhere / "fx-a.json"
                spec.write_text(self._valid_leg_spec_text(), encoding="utf-8")
                before = spec.read_text(encoding="utf-8")
                r = self.run_plan(self.write_plan([self.item(spec, "A", self.commit_a)]))
                self._assert_nothing_submitted_input_intact(r, spec, before, "LEG_PLAN_REFUSED reason=LEG_SPEC_IS_RUN_OUTPUT item=1 ")
                os.rmdir(self.run_dir / owned / "sub")

    def test_r12_a1b_a_spec_that_a_file_symlink_in_a_runner_owned_directory_points_at_is_refused_before_the_claim(self) -> None:
        self._fresh_run_dir()
        (self.run_dir / "quiet").mkdir()
        spec = self.case / "safe" / "fx-a.json"
        spec.parent.mkdir(parents=True, exist_ok=True)
        spec.write_text(self._valid_leg_spec_text(), encoding="utf-8")
        try:
            os.symlink(spec, self.run_dir / "quiet" / "venue-quiet-probe-20261026T180000000Z.job.ps1")
        except OSError as exc:
            self.skipTest(f"file symlinks need a privilege this account lacks here: {exc}")
        before = spec.read_text(encoding="utf-8")
        r = self.run_plan(self.write_plan([self.item(spec, "A", self.commit_a)]))
        self._assert_nothing_submitted_input_intact(r, spec, before, "LEG_PLAN_REFUSED reason=LEG_SPEC_IS_RUN_OUTPUT item=1 ")

    # r12 pass 2 (adversary r12 A2'): what the preflight validated must still hold when the claim is taken, or the leg throws / refuses after the lease opened.
    def _during_claim_wait(self, args: list[str], e: dict, change) -> tuple[int, str, str]:
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=e)
        try:
            self._wait_for_log("CLAIM_HELD")
            change()
            self.claim_path.unlink()   # the foreign owner lets go: this run takes the claim next poll
            so, se = p.communicate(timeout=240)
        finally:
            if p.poll() is None:
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, text=True, timeout=60)
                p.wait(timeout=60)
        return p.returncode, so, se

    def _assert_refused_after_the_wait(self, code: int, so: str, se: str, marker: str) -> None:
        self.assertEqual(code, 2, so + se)
        self.assertIn(marker, se)
        self.assertEqual(self.kinds(), [], "no lease was opened")
        self.assertEqual(self.leg_calls(), [])
        self.assertFalse(self.claim_path.exists(), "the claim taken after the wait was released")

    def test_r12_a2b_a_leg_spec_the_leg_would_throw_on_after_the_wait_is_refused_before_the_lease(self) -> None:
        self._fresh_run_dir()
        spec = self.case / "safe" / "fx-a.json"
        spec.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.specs[0], spec)
        self._foreign_live_claim()
        args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]))
        # playSeconds past Int32: Invoke-VenueLeg's [int] cast throws, which the preflight (Get-LegSpecRefusal) already refuses up front
        changed = json.loads(spec.read_text(encoding="utf-8"))
        changed["playSeconds"] = 2147483648
        code, so, se = self._during_claim_wait(args, e, lambda: spec.write_text(json.dumps(changed), encoding="utf-8"))
        self._assert_refused_after_the_wait(code, so, se, "LEG_PLAN_REFUSED reason=LEG_SPEC_INVALID item=1 ")

    def test_r12_a2b_a_staged_build_manifest_that_changed_during_the_wait_is_refused_before_the_lease(self) -> None:
        self._fresh_run_dir()
        manifest = self._staged_manifest(self.commit_a)
        self._foreign_live_claim()
        args, e = self.plan_cmd(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]))
        code, so, se = self._during_claim_wait(args, e, lambda: manifest.write_bytes(manifest.read_bytes() + b" "))
        self._assert_refused_after_the_wait(code, so, se, "LEG_PLAN_REFUSED reason=BUILD_MANIFEST_MISMATCH item=1 ")

    def test_r12_a2b_a_staged_build_manifest_removed_during_the_wait_is_refused_before_the_lease(self) -> None:
        self._fresh_run_dir()
        manifest = self._staged_manifest(self.commit_a)
        self._foreign_live_claim()
        args, e = self.plan_cmd(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]))
        code, so, se = self._during_claim_wait(args, e, lambda: manifest.unlink())
        self._assert_refused_after_the_wait(code, so, se, "LEG_PLAN_REFUSED reason=BUILD_NOT_STAGED item=1 ")

    def test_r12_a2b_an_unchanged_plan_still_runs_after_a_claim_wait(self) -> None:
        self._fresh_run_dir()
        self._foreign_live_claim()
        args, e = self.plan_cmd(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]))
        code, so, se = self._during_claim_wait(args, e, lambda: None)
        self.assertEqual(code, 0, so + se)
        self.assertNotIn("LEG_PLAN_REFUSED", se)

    # r13 (Dot A1''): a staged manifest whose bytes cannot be read after the wait (write-locked: the location and metadata still read) was "not a hit", so the lease opened on a build
    # nobody re-validated. Its digest cannot be checked, so it is refused before the lease, typed, like the preflight's own unreadable manifest.
    def _lock_exclusive(self, path: Path) -> None:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = ctypes.c_void_p
        k32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        # a writer that still lets others read: GENERIC_READ | GENERIC_WRITE, share READ, OPEN_EXISTING. Test-Path and the location still answer; ReadAllBytes (share READ, which a
        # handle with write access breaks) does not. (Share NONE hides the file from Test-Path too, which is the BUILD_NOT_STAGED refusal r12 already covers.)
        h = k32.CreateFileW(str(path), 0xC0000000, 1, None, 3, 0x80, None)
        self.assertNotIn(h, (None, ctypes.c_void_p(-1).value), f"could not lock {path}: {ctypes.get_last_error()}")
        self.addCleanup(k32.CloseHandle, h)

    def test_r13_a1_a_staged_build_manifest_write_locked_during_the_wait_is_refused_before_the_lease(self) -> None:
        self._fresh_run_dir()
        manifest = self._staged_manifest(self.commit_a)
        self._foreign_live_claim()
        args, e = self.plan_cmd(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]))
        code, so, se = self._during_claim_wait(args, e, lambda: self._lock_exclusive(manifest))
        self._assert_refused_after_the_wait(code, so, se, "LEG_PLAN_REFUSED reason=SHARE_UNREACHABLE item=1 ")
        self.assertIn("could not be read after the claim wait", se)

    # r13 (Dot A2''): one file, two spellings. A loopback UNC name (\\localhost\C$\...) and the local name reach the same single-link file, and the guard compared spellings, so the
    # UNC spelling of a runner output was accepted as an input. It now compares file and directory identity (volume serial + file index), which no spelling changes.
    def _loopback_unc(self, p: Path) -> Path:
        drive, rest = os.path.splitdrive(str(p.resolve()))
        if len(drive) != 2 or drive[1] != ":":
            self.skipTest(f"{p} is not on a drive letter")
        unc = Path("\\\\localhost\\" + drive[0] + "$" + rest)
        if not unc.exists():
            self.skipTest(f"no loopback admin share reaches {p} as {unc}")
        return unc

    def _plan_with_share(self, share: Path) -> subprocess.CompletedProcess:
        args, e = self.plan_cmd(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]))
        args[args.index("-AgentShare") + 1] = str(share)
        return subprocess.run(args, capture_output=True, text=True, timeout=600, env=e)

    def test_r13_a2_a_staged_build_manifest_reached_by_a_loopback_unc_agent_share_inside_a_runner_owned_directory_is_refused(self) -> None:
        # the share is <RunDir>\quiet (a directory the runner writes), named \\localhost\C$\...\quiet: the manifest's UNC path shares no prefix with the local -RunDir, one link, no reparse point
        self._fresh_run_dir()
        inside = self.run_dir / "quiet"
        shutil.copytree(self.share, inside)
        manifest = inside / "cache" / f"playback-attr-3-cuda-{self.commit_a[:12]}-build.json"
        before = manifest.read_text(encoding="utf-8")
        r = self._plan_with_share(self._loopback_unc(inside))
        self._assert_nothing_submitted_input_intact(r, manifest, before, "LEG_PLAN_REFUSED reason=BUILD_MANIFEST_IS_RUN_OUTPUT item=1 ")
        self.assertIn("inside quiet\\", r.stderr)

    def test_r13_a2_a_loopback_unc_agent_share_outside_the_run_still_runs(self) -> None:
        self._fresh_run_dir()
        r = self._plan_with_share(self._loopback_unc(self.share))
        self.assertNotIn("LEG_PLAN_REFUSED", r.stderr)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_r13_a2_a_leg_spec_named_by_a_loopback_unc_alias_of_a_runner_output_is_refused_before_the_claim(self) -> None:
        # (already refused on b9b2f710: Resolve-LegSpecPath hands a UNC spec back provider-qualified, which the guard cannot resolve; this pins that the refusal holds)
        for case in ("gate.log", "quiet/x.json", "junction quiet"):
            with self.subTest(output=case):
                self._fresh_run_dir()
                self.run_dir.mkdir(parents=True, exist_ok=True)
                if case == "junction quiet":
                    elsewhere = self.case / "elsewhere-unc"
                    shutil.rmtree(elsewhere, ignore_errors=True)
                    self._junction(self.run_dir / "quiet", elsewhere)
                    local = elsewhere / "x.json"
                else:
                    local = self.run_dir / case
                    local.parent.mkdir(parents=True, exist_ok=True)
                local.write_text(self._valid_leg_spec_text(), encoding="utf-8")
                before = local.read_text(encoding="utf-8")
                r = self.run_plan(self.write_plan([self.item(self._loopback_unc(local), "A", self.commit_a)]))
                self._assert_nothing_submitted_input_intact(r, local, before, "LEG_PLAN_REFUSED reason=LEG_SPEC_IS_RUN_OUTPUT item=1 ")
                if case == "junction quiet":
                    os.rmdir(self.run_dir / "quiet")

    # r13 (adversary r13 A1): the leg job also reads packageZip.sha256, exe.sha256 and dll.sha256 from the manifest and hashes the cached artifacts against them
    # (playback-attr-3-cuda-job.ps1 $manifestChecks); a manifest without one passed preflight and the job threw after the lease opened.
    def test_r13_adv_a1_a_staged_build_without_an_artifact_digest_is_refused_before_the_claim_and_the_lease(self) -> None:
        a, c = self.commit_a, "3" * 40
        cases = {
            "no packageZip": {"packageZip": ABSENT},
            "exe.sha256 blank": {"exe": {"sha256": ""}},
            "dll.sha256 a number": {"dll": {"sha256": 1}},
            "exe.sha256 not hex": {"exe": {"sha256": "z" * 64}},
            "dll is a string": {"dll": "c" * 64},
        }
        for label, override in cases.items():
            with self.subTest(case=label):
                manifest = self.stage(c, "bad build", **override)
                self.assert_whole_plan_refused(self.write_plan([self.item(self.specs[0], "A", a), self.item(self.specs[1], "B", c, manifest=manifest)]), "BUILD_MANIFEST_MISMATCH item=2 ")

    def test_r13_adv_a1_the_artifact_refusal_names_the_missing_digest(self) -> None:
        c = "3" * 40
        manifest = self.stage(c, "bad build", exe={"sha256": ""})
        r = self.run_plan(self.write_plan([self.item(self.specs[0], "A", c, manifest=manifest)]))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("ATTRCUDA_BUILD_MANIFEST_ARTIFACT_SHA_MISSING", r.stderr)
        self.assertIn("for exe ", r.stderr)

    # LEASE-BATCH-PLAN-ARTIFACT-PREFLIGHT-1 (Dot 6440180395 on #684): the preflight checked that the manifest's artifact digests were well-formed, never that the cached artifacts
    # exist and hash to them; the leg job found out after the lease opened (playback-attr-3-cuda-job.ps1 $manifestChecks), so the lease was spent and nothing was measured.
    def test_artifact_preflight_a_staged_build_whose_cached_artifact_is_absent_is_refused_before_the_claim_and_the_lease(self) -> None:
        a, c = self.commit_a, "3" * 40
        for label in ("exe", "dll", "packageZip"):
            with self.subTest(absent=label):
                manifest = self.stage(c, f"no {label}")
                self.artifact_path(c, label).unlink()
                self.assert_whole_plan_refused(self.write_plan([self.item(self.specs[0], "A", a), self.item(self.specs[1], "B", c, manifest=manifest)]), "BUILD_ARTIFACT_MISSING item=2 ")

    def test_artifact_preflight_a_staged_build_whose_cached_artifact_differs_from_its_digest_is_refused_before_the_claim_and_the_lease(self) -> None:
        a, c = self.commit_a, "3" * 40
        for label in ("exe", "dll", "packageZip"):
            with self.subTest(differs=label):
                manifest = self.stage(c, f"stale {label}")
                self.artifact_path(c, label).write_bytes(b"a different build's bytes")
                self.assert_whole_plan_refused(self.write_plan([self.item(self.specs[0], "A", a), self.item(self.specs[1], "B", c, manifest=manifest)]), "BUILD_ARTIFACT_MISMATCH item=2 ")

    # r8b adversary A1 (pass 1 on a2c32874): the verifier opened each cached artifact with FileShare.ReadWrite, but the leg job hashes it with FileShare.Read
    # (AttrCudaArtifacts.psm1 Get-Sha), which a retained writer handle refuses. A build with a writer open was STAGED here and threw after the lease opened. It is now read the way
    # the leg job reads it, so the writer makes the read fail and the plan is refused SHARE_UNREACHABLE (an unreadable file is not a missing or matching one) before any claim.
    def test_artifact_preflight_r8b_adv_a1_a_cached_artifact_a_writer_holds_open_is_refused_as_the_leg_job_would_fail_on_it(self) -> None:
        c = "3" * 40
        manifest = self.stage(c, "writer open")
        with open(self.artifact_path(c, "packageZip"), "r+b"):
            self.assert_whole_plan_refused(self.write_plan([self.item(self.specs[0], "A", c, manifest=manifest)]), "SHARE_UNREACHABLE item=1 ")

    # r8b adversary pass 2 on 29c63516, A1: the hash worker checked -ArtifactMinSec before the existence check only, so a Test-Path that used the time still opened and hashed the file
    # with less than the minimum left. The minimum is now checked again between the existence check and the open.
    def test_artifact_preflight_r8b_adv_a1_the_hash_worker_checks_the_minimum_again_after_the_existence_check(self) -> None:
        text = MODULE.read_text(encoding="utf-8")
        start = text.index("# a read that fails ends the read (UNREACHABLE), never \"absent\"")
        worker = text[start:text.index("Invoke-VenueBoundedRead -ScriptText $probe.ToString()", start)]
        exists, opened = worker.index("Test-Path -LiteralPath $i.path"), worker.index("[IO.File]::Open($i.path")
        checks = [m.start() for m in re.finditer(r"-lt \$i\.minSec\) \{ 'NOTIME:'", worker)]
        self.assertEqual(len(checks), 3, "the minimum is checked before the existence check, again before the open, and again once the file is open (r10 adversary A1)")
        self.assertTrue(checks[0] < exists < checks[1] < opened < checks[2] < worker.index("ComputeHash"), (checks, exists, opened))

    # r9 B1 (sol r8b = opus = fable H2 = Dot): `[ValidateRange(1, 600)]` on a `[double]` has Int32 endpoints, so PowerShell validates a ROUNDED INTEGER copy of the value: 0.6 and 600.5
    # bound while the original value went on to the helper (a 0.6 s bound under the 1 s the contract names, a 600.5 s one over the 600 s cap). Double endpoints validate the value as given.
    # Sites: Get-VenueStagedBuildManifest -TimeoutSec / -ArtifactMinSec and Invoke-VenueBoundedRead -TimeoutSec. Fractional budgets in range (2.8) must still bind.
    def _binding_outcomes(self, calls: dict[str, str]) -> dict[str, str]:
        lines = ["Import-Module '" + str(MODULE) + "' -Force -DisableNameChecking", "$out = [ordered]@{}"]
        for key, call in calls.items():
            lines.append("try { $null = " + call + "; $out['" + key + "'] = 'BOUND' } catch { $out['" + key + "'] = if ($_.FullyQualifiedErrorId -like 'ParameterArgumentValidationError*') { 'REFUSED' } else { 'OTHER:' + $_.FullyQualifiedErrorId } }")
        lines += ["ConvertTo-Json -Compress -InputObject $out", "Complete-VenueProcess -ExitCode 0"]
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "\n".join(lines)], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_artifact_preflight_r9_b1_a_fractional_timeout_outside_the_range_is_refused_at_binding_not_rounded_into_it(self) -> None:
        manifest = "Get-VenueStagedBuildManifest -AgentShare '" + str(self.share) + "' -SourceCommit ('a' * 40) -ProbeScript { param($p) '' } "
        bounded = "& (Get-Module VenueChain) { Invoke-VenueBoundedRead -ScriptText '1' -Argument 0 "
        got = self._binding_outcomes({
            "manifest_timeout_0.6": manifest + "-TimeoutSec 0.6",
            "manifest_timeout_600.5": manifest + "-TimeoutSec 600.5",
            "manifest_minsec_0.6": manifest + "-ArtifactMinSec 0.6",
            "manifest_minsec_600.5": manifest + "-ArtifactMinSec 600.5",
            "bounded_timeout_0.6": bounded + "-TimeoutSec 0.6 }",
            "bounded_timeout_600.5": bounded + "-TimeoutSec 600.5 }",
            # controls: the range's own ends and a fractional budget inside it still bind
            "manifest_timeout_1": manifest + "-TimeoutSec 1",
            "manifest_timeout_2.8": manifest + "-TimeoutSec 2.8",
            "manifest_timeout_600": manifest + "-TimeoutSec 600",
            "manifest_minsec_1": manifest + "-ArtifactMinSec 1",
            "manifest_minsec_2.8": manifest + "-ArtifactMinSec 2.8",
            "bounded_timeout_1": bounded + "-TimeoutSec 1 }",
            "bounded_timeout_2.8": bounded + "-TimeoutSec 2.8 }",
            "bounded_timeout_600": bounded + "-TimeoutSec 600 }",
        })
        refused = {k: v for k, v in got.items() if k.rsplit("_", 1)[1] in ("0.6", "600.5")}
        bound = {k: v for k, v in got.items() if k not in refused}
        self.assertEqual(set(refused.values()), {"REFUSED"}, got)
        self.assertEqual(set(bound.values()), {"BOUND"}, got)

    def test_artifact_preflight_r9_b1_no_double_parameter_of_the_dual_venue_scripts_has_integer_range_endpoints(self) -> None:
        # the class, not the three sites: any `[ValidateRange(<int>, <int>)]` on a floating-point parameter in the dual-venue scripts validates a rounded copy. r10 (fable key3 r9 P-02):
        # whatever order, spelling or layout the declaration takes ([Parameter()] between, [double] before the attribute, [Double] / [System.Double], a multi-line attribute)
        bad, seen = [], 0
        for path in sorted(DV.glob("*.ps*1")):
            for line, text, integer_end in ps_float_validaterange_params(path.read_text(encoding="utf-8")):
                seen += 1
                if integer_end:
                    bad.append(f"{path.name}:{line}: {text}")
        self.assertEqual(bad, [], "a floating-point parameter's ValidateRange endpoints must be doubles (1.0, 600.0)")
        self.assertGreaterEqual(seen, 3, "the scan found the r9 sites (Get-VenueStagedBuildManifest -TimeoutSec / -ArtifactMinSec, Invoke-VenueBoundedRead -TimeoutSec): it is not vacuous")

    def test_artifact_preflight_r10_the_validaterange_scanner_sees_every_declaration_spelling(self) -> None:
        bad = [
            "param([ValidateRange(1, 600)][double]$T)",
            "param([ValidateRange(1, 600)]  [Double]$T)",
            "param([ValidateRange(1, 600)][System.Double]$T)",
            "param([Parameter()][ValidateRange(1, 600)][double]$T)",
            "param([Parameter(Mandatory)]\n  [ValidateRange(1, 600)]\n  [double]\n  $T)",
            "param([double][ValidateRange(1, 600)]$T)",
            "param([Parameter()][double][ValidateRange(1, 600)]$T)",
            "param([ValidateRange(\n    1,\n    600.0)][double]$T)",
            "param([ValidateRange(0.5, 600)][single]$T)",
            "param([ValidateRange( 1 , 600 ) ][decimal]$T)",
        ]
        for text in bad:
            self.assertEqual([x[2] for x in ps_float_validaterange_params(text)], [True], text)
        good = [
            "param([ValidateRange(1.0, 600.0)][double]$T)",
            "param([double][ValidateRange(1.0, 600.0)]$T)",
            "param([Parameter()]\n[ValidateRange(\n  1.0,\n  600.0)]\n[double]$T)",
        ]
        for text in good:
            self.assertEqual([x[2] for x in ps_float_validaterange_params(text)], [False], text)
        for text in ("param([ValidateRange(1, 600)][int]$T)", "param([int][ValidateRange(1, 600)]$T)", "param([ValidateRange(1, 600)][string]$T)"):
            self.assertEqual(ps_float_validaterange_params(text), [], text)

    # r9 hardening (sol r7 + sol r8b + fable H3 + Dot P3): the per-file minimum re-check was pinned by source position only, so a label-gated mutation (the NOTIME check restricted to
    # packageZip) passed the pin. Behavioural: the production worker scriptblock (lifted from the module's AST) runs on three files with a fake clock (no real sleeps) and a Test-Path
    # shadow that spends fake time, window 4 s / minimum 2 s. Every file has the minimum checked before its existence check AND again after it, for EACH label; the wrong digest makes
    # a file that was opened and hashed visible as a MISMATCH line, so "no open" is an observable absence. The shadow also records which files had an existence read STARTED: the check
    # before it is what keeps a file from even being looked at with the window spent (a UNC Test-Path can itself block), and an absent file shows it as NOTIME, never MISSING.
    def _artifact_worker_run(self, case: str, end_sec: float = 4.0, start_now: float = 0.0, delay_label: str = "", consume_label: str = "", absent_label: str = "", open_label: str = "") -> tuple[list[str], list[str]]:
        d = self.case / ("worker-files-" + case)
        d.mkdir(exist_ok=True)
        for label in ("packageZip", "exe", "dll"):
            if label != absent_label:
                (d / f"{label}.bin").write_bytes(f"{label} bytes".encode("utf-8"))
        script = (
            "$ErrorActionPreference = 'Stop'\n"
            "$ast = [System.Management.Automation.Language.Parser]::ParseFile('" + str(MODULE) + "', [ref]$null, [ref]$null)\n"
            "$w = $ast.Find({ param($x) $x -is [System.Management.Automation.Language.ScriptBlockExpressionAst] -and $x.Extent.Text -match '^\\{\\s*param\\(\\$items\\)' }, $true).ScriptBlock.GetScriptBlock()\n"
            "$global:clk = [pscustomobject]@{ now = " + repr(start_now) + " }\n"
            "$clock = [pscustomobject]@{}\n"
            "Add-Member -InputObject $clock -MemberType ScriptProperty -Name Elapsed -Value { [pscustomobject]@{ TotalSeconds = $global:clk.now } }\n"
            "$global:delayPath = '" + (str(d / f"{delay_label}.bin") if delay_label else "") + "'\n"
            "$global:tp = [System.Collections.Generic.List[string]]::new()\n"
            "function Test-Path { param([string]$LiteralPath, [string]$PathType) $global:tp.Add((Split-Path -Leaf $LiteralPath)); if ($global:delayPath -and $LiteralPath -ceq $global:delayPath) { $global:clk.now += 3 }; Microsoft.PowerShell.Management\\Test-Path -LiteralPath $LiteralPath -PathType $PathType }\n"
            "$items = foreach ($label in 'packageZip', 'exe', 'dll') {\n"
            "    $it = [pscustomobject]@{ label = $label; name = \"$label.bin\"; path = '" + str(d) + "\\' + $label + '.bin'; clock = $clock; endSec = " + repr(end_sec) + "; minSec = 2.0 }\n"
            # r10 adversary A1: the open spends the window -- the 2nd read of .path is [IO.File]::Open's own argument (the 1st is Test-Path's), so the fake clock moves between the
            # existence check and the open's return, exactly where a slow SMB open spends it
            "    if ($label -ceq '" + open_label + "') { $p0 = $it.path; $it.PSObject.Properties.Remove('path'); Add-Member -InputObject $it -MemberType NoteProperty -Name _reads -Value 0; Add-Member -InputObject $it -MemberType NoteProperty -Name _p -Value $p0; Add-Member -InputObject $it -MemberType ScriptProperty -Name path -Value { $this._reads++; if ($this._reads -eq 2) { $global:clk.now += 3 }; $this._p } }\n"
            "    if ($label -ceq '" + consume_label + "') { Add-Member -InputObject $it -MemberType ScriptProperty -Name sha -Value { $global:clk.now += 3; ('0' * 64) } }\n"
            "    else { Add-Member -InputObject $it -MemberType NoteProperty -Name sha -Value ('0' * 64) }\n"
            "    $it\n"
            "}\n"
            "$lines = @(& $w $items | ForEach-Object { [string]$_ })\n"
            "ConvertTo-Json -Compress -InputObject @{ lines = $lines; tp = @($global:tp) }\n")
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, case + "\n" + out.stdout + out.stderr)
        got = json.loads(out.stdout.strip().splitlines()[-1])
        return [":".join(line.split(":")[:2]) for line in got["lines"]], [t.removesuffix(".bin") for t in got["tp"]]

    def test_artifact_preflight_r9_the_hash_worker_checks_the_minimum_behaviourally_before_and_after_the_existence_check_for_every_file(self) -> None:
        order = ["packageZip", "exe", "dll"]
        # control: nothing spends time, so every file is looked at, opened and hashed (the wrong digest shows as MISMATCH) and none is NOTIME
        self.assertEqual(self._artifact_worker_run("control"), ([f"MISMATCH:{k}" for k in order], order))
        # the existence check of file L takes 3 s of the 4 s window: L is NOTIME and was not opened (no MISMATCH:L); the files before it were hashed
        for i, label in enumerate(order):
            with self.subTest(existence_check_spends_the_window=label):
                self.assertEqual(self._artifact_worker_run("exists-" + label, delay_label=label), ([f"MISMATCH:{k}" for k in order[:i]] + [f"NOTIME:{label}"], order[:i + 1]))
        # the hash of file L spends the window: the NEXT file starts with 1 s left, under the 2 s minimum, so it is NOTIME with NO existence read started on it, and a file that is
        # also absent is still NOTIME (not MISSING: nothing was looked at)
        for i, label in enumerate(order[:-1]):
            nxt = order[i + 1]
            with self.subTest(hash_spends_the_window=label):
                self.assertEqual(self._artifact_worker_run("hash-" + label, consume_label=label), ([f"MISMATCH:{k}" for k in order[:i + 1]] + [f"NOTIME:{nxt}"], order[:i + 1]))
                self.assertEqual(self._artifact_worker_run("hash-" + label + "-next-absent", consume_label=label, absent_label=nxt), ([f"MISMATCH:{k}" for k in order[:i + 1]] + [f"NOTIME:{nxt}"], order[:i + 1]))
        # the first file with the window already spent is NOTIME and never looked at
        self.assertEqual(self._artifact_worker_run("late", start_now=3.0), (["NOTIME:packageZip"], []))
        # the contract's own boundary stays: under the minimum left (0.9 s of a 2 s minimum) is NOTIME and nothing is read; exactly the minimum left is read
        self.assertEqual(self._artifact_worker_run("boundary-under", end_sec=0.9), (["NOTIME:packageZip"], []))
        self.assertEqual(self._artifact_worker_run("boundary-at", end_sec=2.0), ([f"MISMATCH:{k}" for k in order], order))

    # r10 adversary A1 (pass 2 on 3c341d79): the minimum was checked before the existence check and again before the open, but the OPEN can take the time too (an SMB open): a file
    # whose open spent the window went on to ComputeHash with under the minimum left, and as the last matching artifact it was STAGED. The worker checks again once the file is open.
    def test_artifact_preflight_r10_adv_a1_the_hash_worker_checks_the_minimum_again_after_the_open_for_every_file(self) -> None:
        order = ["packageZip", "exe", "dll"]
        for i, label in enumerate(order):
            with self.subTest(open_spends_the_window=label):
                # file L was looked at and opened (its Test-Path ran), then its open spent 3 of the 4 s: NOTIME:L, no hash of it (no MISMATCH:L), nothing after it touched
                self.assertEqual(self._artifact_worker_run("open-" + label, open_label=label), ([f"MISMATCH:{k}" for k in order[:i]] + [f"NOTIME:{label}"], order[:i + 1]))

    def test_artifact_preflight_the_refusal_names_the_artifact_and_a_fully_staged_build_still_opens_the_lease(self) -> None:
        c = "3" * 40
        manifest = self.stage(c, "named")
        self.artifact_path(c, "dll").unlink()
        r = self.run_plan(self.write_plan([self.item(self.specs[0], "A", c, manifest=manifest)]))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("igpu_recon_cuda-playback-attr-3-cuda-333333333333.dll", r.stderr)
        # the control: every artifact staged and matching
        shutil.rmtree(self.run_dir, ignore_errors=True)
        self.events.unlink(missing_ok=True)
        ok = self.run_plan(self.write_plan([self.item(self.specs[0], "A", self.commit_a), self.item(self.specs[1], "B", self.commit_b, "cuda")]))
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self.assertEqual(len([k for k in self.kinds() if k.startswith("OPEN:")]), 1, self.kinds())

    def test_artifact_preflight_an_artifact_that_changes_while_the_batch_waits_on_a_foreign_claim_is_refused_after_the_wait(self) -> None:
        self._fresh_run_dir()
        exe = self.artifact_path(self.commit_a, "exe")
        original = exe.read_bytes()
        self._foreign_live_claim()
        args, e = self.plan_cmd(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]))
        try:
            code, so, se = self._during_claim_wait(args, e, lambda: exe.write_bytes(b"replaced while the batch waited"))
        finally:
            exe.write_bytes(original)
        self.assertEqual(code, 2, so + se)
        self.assertIn("LEG_PLAN_REFUSED reason=BUILD_ARTIFACT_MISMATCH item=1 ", se)
        self.assertEqual(self.kinds(), [], "no lease was opened")
        self.assertFalse(self.claim_path.exists(), "the claim taken after the wait was released")

    # adversary r1 A1: the supplementary route calls with -TimeoutSec 30, and the artifact hash had its own 300 s default, so a stalled share held the primary chain for five minutes.
    def test_artifact_preflight_adv_a1_the_artifact_hash_stays_inside_the_calls_own_timeout(self) -> None:
        c = "3" * 40
        self.stage(c, "slow share")
        script = (f"Import-Module '{MODULE}' -Force -DisableNameChecking\n"
                  "$sw = [Diagnostics.Stopwatch]::StartNew()\n"
                  f"$r = Get-VenueStagedBuildManifest -AgentShare '{self.share}' -SourceCommit '{c}' -TimeoutSec 3 -VerifyArtifacts -ArtifactProbeScript {{ param($i) [Threading.Thread]::Sleep(25000); 'late' }}\n"
                  "ConvertTo-Json -Compress -InputObject @{ state = $r.state; sec = [int]$sw.Elapsed.TotalSeconds; error = [string]$r.error }\n"
                  "Complete-VenueProcess -ExitCode 0\n")
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        got = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(got["state"], "UNREACHABLE", got)
        self.assertLessEqual(got["sec"], 6, f"the call (timeout 3 s) took {got['sec']} s: {got}")
        self.assertIn("could not be hashed", got["error"])

    # adversary r2 A2: with no explicit bound the hash gets what is left of -TimeoutSec, and a 1 s floor ran a 1 s call to 2 s (850 ms manifest read + 900 ms hash, then STAGED). Now less than a whole second
    # left is UNREACHABLE at once, so no call outlasts its own -TimeoutSec by more than the manifest read itself.
    def test_artifact_preflight_adv_r2_a2_a_call_with_under_a_second_left_for_the_hash_ends_unreachable_not_over_its_timeout(self) -> None:
        c = "3" * 40
        self.stage(c, "tight")
        manifest_probe = ("param($p) [Threading.Thread]::Sleep(850); ('a' * 64); 'C:" + c + "'; 'D:' + ('f' * 64); 'S:bool'; "
                          "'A:packageZip=ok'; 'H:packageZip=' + ('1' * 64); 'A:exe=ok'; 'H:exe=' + ('2' * 64); 'A:dll=ok'; 'H:dll=' + ('3' * 64)")
        script = (f"Import-Module '{MODULE}' -Force -DisableNameChecking\n"
                  "$sw = [Diagnostics.Stopwatch]::StartNew()\n"
                  f"$r = Get-VenueStagedBuildManifest -AgentShare '{self.share}' -SourceCommit '{c}' -TimeoutSec 1 -VerifyArtifacts "
                  f"-ProbeScript {{ {manifest_probe} }} -ArtifactProbeScript {{ param($i) [Threading.Thread]::Sleep(900) }}\n"
                  "ConvertTo-Json -Compress -InputObject @{ state = $r.state; ms = [int]$sw.Elapsed.TotalMilliseconds; error = [string]$r.error }\n"
                  "Complete-VenueProcess -ExitCode 0\n")
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        got = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(got["state"], "UNREACHABLE", got)
        self.assertLess(got["ms"], 1500, f"the call (timeout 1 s) ran {got['ms']} ms: {got}")
        self.assertIn("no time is left", got["error"])

    # adversary r1 A2: the post-claim guard runs between the grant and the claim keeper's start. A hash that outlasts ClaimTtlSec - ClaimRenewSec made every guard "lapse" the claim, release it and
    # ask again until the deadline. The runner has no seam that slows a real hash, so the budget's derivation is pinned in the source: set from the grant time, handed to the artifact read, cleared after.
    def test_artifact_preflight_adv_a2_the_post_claim_guard_hashes_inside_the_claim_guard_budget(self) -> None:
        text = RUNNER.read_text(encoding="utf-8")
        # r3: the budget is New-GuardBudget's (a Stopwatch started at the grant read), so the claim guard and the post-open guard count on one monotonic clock each
        # r7: less 1 s for the guard's own end, now that its reads get the exact remainder
        grant = text.index("$script:guardBudget = $(if ($req.granted) { New-GuardBudget ($ClaimTtlSec - $ClaimRenewSec - 1) ")
        self.assertLess(text.index("$grantReadUtc = [DateTime]::UtcNow", grant - 2000), grant)
        # r4 (sol r3 B1): the whole guard runs under that budget (Invoke-InputGuard), not only its manifest and artifact reads
        call, clear = text.index("try { $hit = Invoke-InputGuard }", grant), text.index("$script:guardBudget = $null", grant)
        self.assertLess(grant, call)
        self.assertLess(call, clear)
        self.assertLess(clear, text.index("if ($hit) {", grant))
        self.assertIn("clock = [Diagnostics.Stopwatch]::StartNew()", text[text.index("function New-GuardBudget"):text.index("function Get-InputContentRefusal")])
        guard = text[text.index("function Get-InputContentRefusal"):text.index("function Stop-InputIsOutput")]
        # adversary r2 A1: the manifest read AND the hash come out of ONE share of the budget, and a share too small for both is a typed refusal. r4 (hub ruling 17:18:14Z): ONE deadline
        # through both phases, so no fixed split: the timed r4 cases below hold the behaviour (Dot's literal case, key3 row L-04, the post-open 500 s room)
        self.assertIn("($script:guardBudget.sec - $script:guardBudget.clock.Elapsed.TotalSeconds)", guard)
        # r5 (key3 r4 B1): -VerifyArtifacts is off only under a budget that says so (the claim wait, no claim held)
        # r7 (sol r6 B1): the guard's minimum and the module's -ArtifactMinSec are ONE value, and the share is the unfloored remainder
        self.assertIn("-TimeoutSec $share -VerifyArtifacts:$verifyArtifacts -ArtifactMinSec $script:GuardReadMinSec -ProbeScript", guard)
        self.assertIn("$verifyArtifacts = -not ($null -ne $script:guardBudget -and $script:guardBudget.artifactsOff)", guard)
        self.assertNotIn("$manifestSec", guard)
        self.assertNotIn("-ArtifactTimeoutSec", guard)
        self.assertIn("if ($share -lt $script:GuardReadMinSec) { return", guard)
        self.assertNotIn("Floor", guard)
        self.assertIn("$script:GuardReadMinSec = 2\n", text)
        self.assertNotIn("-TimeoutSec 30", guard)
        # and the pre-claim preflight, which holds no claim, asks for its own explicit bound
        self.assertIn("-VerifyArtifacts -ArtifactTimeoutSec 300 }", text)

    # r2 (Dot fd08838 cos-feedback/mlv-app/pr-705.md): the test above pins the source; this one TIMES the guard through the runner's offline probe seams. With -ClaimTtlSec 60 and
    # -ClaimRenewSec 30 (their floors) the guard has 30 s from the grant. Inside the guard (the claim file exists; the preflight before it is fast) the manifest read takes 18 s and the
    # artifact hash 18 s more. On 22d5ad64 the hash had a whole share of the budget, taken BEFORE the manifest read: it answered at about 36 s, the claim lapsed in the guard and was
    # asked for again until DEADLINE (exit 3). The manifest read and the hash must share ONE bound: the hash runs out first and the plan is refused SHARE_UNREACHABLE, inside 30 s.
    def test_artifact_preflight_r2_a_slow_manifest_read_and_a_slow_hash_end_typed_inside_the_claim_guard_budget(self) -> None:
        c = self.commit_a
        doc = json.loads((self.share / "cache" / f"playback-attr-3-cuda-{c[:12]}-build.json").read_text(encoding="utf-8"))
        started = self.case / "guard-read-started.txt"
        in_guard = f"if (Test-Path -LiteralPath '{self.claim_path}' -PathType Leaf) {{ "
        facts = [self.manifests[c], "C:" + c, "D:" + "f" * 64, "S:bool"] + [x for k in ("packageZip", "exe", "dll") for x in (f"A:{k}=ok", f"H:{k}={doc[k]['sha256']}")]
        manifest_probe, artifact_probe = self.case / "manifest-probe.ps1", self.case / "artifact-probe.ps1"
        manifest_probe.write_text("param($p)\n" + in_guard + f"[IO.File]::AppendAllText('{started}', [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ss.ffffff', [Globalization.CultureInfo]::InvariantCulture) + [Environment]::NewLine); [Threading.Thread]::Sleep(18000) }}\n"
                                  + "".join(f"'{f}'\n" for f in facts), encoding="utf-8")
        artifact_probe.write_text("param($items)\n" + in_guard + "[Threading.Thread]::Sleep(18000) }\n", encoding="utf-8")
        r = self.run_plan(self.write_plan([self.item(self.specs[0], "A", c)]), "-ClaimTtlSec", "60", "-ClaimRenewSec", "30",
                          "-DeadlineUtc", utc_iso(datetime.now(timezone.utc) + timedelta(seconds=75)),
                          "-StagedBuildProbeScript", str(manifest_probe), "-ArtifactProbeScript", str(artifact_probe))
        ended = datetime.now(timezone.utc)
        log = self.run_dir / "batch.log"
        text = r.stdout + r.stderr + (log.read_text(encoding="utf-8", errors="replace") if log.exists() else "")
        self.assertNotIn("CLAIM_LAPSED_IN_GUARD", text, "the guard outlasted the claim's TTL less one renew interval and the claim was asked for again")
        self.assertEqual(r.returncode, 2, text)
        self.assertIn("LEG_PLAN_REFUSED reason=SHARE_UNREACHABLE item=1 ", r.stderr)
        self.assertIn("could not be hashed", r.stderr)
        self.assertEqual(self.kinds(), [], "no lease was opened")
        self.assertFalse(self.claim_path.exists(), "the claim taken for the guard was released")
        starts = started.read_text(encoding="utf-8").split()
        self.assertEqual(len(starts), 1, f"one guard read, no release-and-retry: {starts}")
        guard_sec = (ended - datetime.fromisoformat(starts[0]).replace(tzinfo=timezone.utc)).total_seconds()
        self.assertLess(guard_sec, 30, f"the guard (and the refusal after it) took {guard_sec:.1f} s of a 30 s claim-guard budget")

    def test_artifact_preflight_r2_the_probe_seams_are_refused_with_the_real_um_run(self) -> None:
        probe = self.case / "probe.ps1"
        probe.write_text("param($p) ''\n", encoding="utf-8")
        args, e = self.plan_cmd(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]), "-ArtifactProbeScript", str(probe))
        args[args.index("-UmRunScript") + 1] = str(RUNNER.parent.parent / "um-run.ps1")
        r = subprocess.run(args, capture_output=True, text=True, timeout=120, env=e)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("BATCH_USAGE -StagedBuildProbeScript and -ArtifactProbeScript are offline test seams", r.stderr)
        self.assertEqual(self.kinds(), [])

    # r3 (Dot 780783c4; hub ACK 6449516541): once the open returns, the guard re-reads and re-hashes every build on the LEASE's time. On f0e12522 it had a fixed 300 s for that while
    # the lease end is clamped (-LeaseCapSec), so a healthy but slow re-hash left less than the leg budget: LEG_BUDGET_EXCEEDS_LEASE, a lease charged with no leg. The guard is now
    # bounded by the lease's room less -LeaseMarginSec, the leg budget and 1 s, and a re-hash that cannot fit is refused typed while the leg budget is still left. The card's numbers
    # (cap 120, leg 60, margin 0, a 70 s hash) are scaled to cap 95 and a 40 s hash so the case stays near 30 s (-LegBudgetSec cannot go under 60). The hash is slow only once the
    # open was answered (the fake agent's state file exists), so the preflight and the claim guard stay fast.
    def _post_open_hash_run(self, hash_sec: float, *extra: str, cap: str = "95", leg: str = "60", margin: str = "0", items: list | None = None, post_open: str | None = None,
                            **env: str) -> tuple[subprocess.CompletedProcess, float]:
        # post_open: PowerShell run in the artifact probe once the open was answered, in place of the hash_sec sleep
        state = self.share / "fake-agent-state.json"
        self.assertFalse(state.exists())
        probe = self.case / "artifact-probe-post-open.ps1"
        body = post_open if post_open is not None else f"[Threading.Thread]::Sleep({int(hash_sec * 1000)})"
        probe.write_text("param($items)\n" + f"if (Test-Path -LiteralPath '{state}' -PathType Leaf) {{ {body} }}\n", encoding="utf-8")
        r = self.run_plan(self.write_plan(items or [self.item(self.specs[0], "A", self.commit_a)]), "-LeaseCapSec", cap, "-LegBudgetSec", leg, "-LeaseMarginSec", margin,
                          "-ArtifactProbeScript", str(probe), *extra, **env)
        ticks = {}
        for line in self.events_raw():
            t, kind, rest = line.split(" ", 2)
            if kind == "UMRUN":
                ticks.setdefault("open" if rest.startswith("lease-batch-open-") else "close", int(t))
        self.assertIn("open", ticks, r.stdout + r.stderr)
        if "close" not in ticks and env.get("NO_CLOSE_EXPECTED"):
            # r6: a close redirect that failed closed submits no close job (JOB_WRITE_FAILED); the hold then ends when the run does
            done = datetime.fromisoformat(batch_result(r.stdout)["finishedUtc"].replace("Z", "+00:00"))
            ticks["close"] = (done - datetime(1, 1, 1, tzinfo=timezone.utc)) // timedelta(microseconds=1) * 10
        self.assertIn("close", ticks, r.stdout + r.stderr)
        return r, (ticks["close"] - ticks["open"]) / 1e7

    # r8: the slow-rehash cases run in the cap 75 / leg 60 room (14 s) the guard-read cases already use, with the hash 6 s over the room as the 40 s hash was over the 34 s room of
    # cap 95: the refusal must land inside the room (held < 75 - 60 + 4), never after the hash has spent the leg budget (LEG_BUDGET_EXCEEDS_LEASE)
    def test_artifact_preflight_r3_a_slow_post_open_rehash_is_refused_typed_before_the_leg_budget_is_spent(self) -> None:
        r, held = self._post_open_hash_run(20, cap="75")
        log = self.run_dir / "batch.log"
        text = r.stdout + r.stderr + (log.read_text(encoding="utf-8", errors="replace") if log.exists() else "")
        self.assertEqual(r.returncode, 2, text)
        self.assertNotIn("LEG_BUDGET_EXCEEDS_LEASE", text, "the re-hash spent the lease's leg budget and the lease was charged with no leg")
        self.assertIn("LEG_PLAN_REFUSED reason=SHARE_UNREACHABLE item=1 ", r.stderr)
        self.assertIn("could not be hashed", r.stderr)
        self.assertIn("the lease's room after the open", r.stderr)
        # #705 r14 (adversary r14 A1 sibling): the hash given up on is a read still held on the manifest's share, so the manifest's location is left unknown and nothing more is
        # written into -RunDir (the receipt included); on 80df9163 the receipt was written through unbounded re-reads of every input's path
        self.assertIn("BATCH_OUTPUT_ALIASED", r.stderr)
        self.assertIn("an input whose location is unknown is refused", r.stderr)
        self.assertFalse((self.run_dir / "batches.jsonl").exists() and self.batches(), "no receipt was written into -RunDir after a share read was given up on")
        self.assertEqual(self.leg_calls(), [])
        # refused while a leg budget was still left in the 75 s lease, not after the 20 s hash
        self.assertLess(held, 75 - 60 + 4, f"the lease was held {held:.1f} s before the close: the re-hash ran into the leg budget")

    def test_artifact_preflight_r3_a_fast_post_open_rehash_still_runs_the_leg(self) -> None:
        r, held = self._post_open_hash_run(0)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["endReason"], b["legsAttempted"], b["legsRan"]), ("ALL_LEGS_DONE", 1, 1))
        self.assertEqual(len(self.leg_calls()), 1)

    def test_artifact_preflight_r3_a_lease_with_no_room_for_the_rehash_and_one_leg_is_refused_at_once(self) -> None:
        # a lease shorter than the leg budget cannot start the re-read: LEG_BUDGET_EXCEEDS_LEASE at once, typed by the guard, before any probe and without the 40 s re-hash
        r, held = self._post_open_hash_run(40, FAKE_LEASE_REMAIN="30")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=LEG_BUDGET_EXCEEDS_LEASE item=1 ", r.stderr)
        [b] = self.batches()
        self.assertEqual((b["endReason"], b["legsAttempted"]), ("LEG_BUDGET_EXCEEDS_LEASE", 0))
        self.assertEqual([k for k in self.kinds() if not k.startswith(("OPEN", "CLOSE"))], [], "no probe and no leg ran")
        self.assertLess(held, 20, f"the lease was held {held:.1f} s: the 40 s re-hash ran on a lease that could hold no leg")

    # r3 adversary A1 (pass 1 on 5c3407ff): the guard skipped the content re-check when the lease's room (a float) was under the leg budget, but the leg loop rounds the room to whole
    # seconds, so a room of 59.9 s with a 60 s budget skipped the check AND let a probe and a leg run on a manifest changed in the open wait. The content is never skipped now.
    def test_artifact_preflight_r3_adv_a1_a_lease_room_just_under_the_leg_budget_runs_no_probe_on_an_unchecked_build(self) -> None:
        manifest = self.share / "cache" / f"playback-attr-3-cuda-{self.commit_a[:12]}-build.json"
        args, e = self.plan_cmd(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]), "-LeaseCapSec", "95", "-LegBudgetSec", "60", "-LeaseMarginSec", "0",
                                FAKE_OPEN_SLEEP="6", FAKE_LEASE_REMAIN="60")
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=e)
        try:
            self._wait_for_log("BATCH_OPEN_LIVENESS_READBACK")
            self._wait_for_open_wait()   # r15: the open's wait itself, never a fixed sleep after the read-back line
            with open(manifest, "a", encoding="utf-8") as f:
                f.write("\n")
            so, se = p.communicate(timeout=240)
        finally:
            if p.poll() is None:
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, text=True, timeout=60)
                p.wait(timeout=60)
        self.assertEqual(p.returncode, 2, so + se)
        self.assertRegex(se, r"LEG_PLAN_REFUSED reason=(LEG_BUDGET_EXCEEDS_LEASE|BUILD_MANIFEST_MISMATCH) item=1 ")
        self.assertIn("found after the open", se)
        self.assertEqual([k for k in self.kinds() if not k.startswith(("OPEN", "CLOSE"))], [], "no probe and no leg ran")
        self.assertEqual(self.leg_calls(), [])

    # r3 adversary A2 (pass 1 on 5c3407ff): an open held in the agent's gate ran the guard at once with no budget (300 s), so a slow re-hash ran while the agent had already admitted
    # the open and published its result, and the lease was held through it before the lease-aware guard below ever started. That call now checks locations only.
    def test_artifact_preflight_r3_adv_a2_a_gate_held_open_is_not_rehashed_before_its_result_is_read(self) -> None:
        r, held = self._post_open_hash_run(20, cap="75", FAKE_OPEN_MODES="gatewait")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=SHARE_UNREACHABLE item=1 ", r.stderr)
        self.assertEqual(self.leg_calls(), [])
        self.assertLess(held, 75 - 60 + 4, f"the lease was held {held:.1f} s before the close: a re-hash ran outside the lease's room")

    # r4 ITEM 1 (hub ruling 2026-10-27T17:18:14Z: one absolute deadline through both phases; key3 and fable r3 B1). On 02612714 each build's share was split into a fixed manifest
    # part (min(30, share - 1)) and the rest for the hash, so read time the manifest did not use never reached the hash and a check that fits was refused SHARE_UNREACHABLE.
    def _timed_probes(self, manifest_ms: int, hash_ms: int, when: str = "$true", no_digests_when: str = "$false") -> tuple[Path, Path]:
        """Manifest and artifact probe seams for both staged builds: the manifest read sleeps manifest_ms and the hash hash_ms while `when` holds (a PowerShell condition). While
        `no_digests_when` holds the manifest read emits A:<k>=ok without the paired H:<k>= digest lines (key3 r3 H3's throw)."""
        manifest_probe, artifact_probe = self.case / "timed-manifest-probe.ps1", self.case / "timed-artifact-probe.ps1"
        body = ["param($p)", f"if ({when}) {{ [Threading.Thread]::Sleep({manifest_ms}) }}", f"$noDigests = {no_digests_when}"]
        for c in (self.commit_a, self.commit_b):
            doc = json.loads((self.share / "cache" / f"playback-attr-3-cuda-{c[:12]}-build.json").read_text(encoding="utf-8"))
            facts = [self.manifests[c], "C:" + c, "D:" + "f" * 64, "S:bool"] + [f"A:{k}=ok" for k in ("packageZip", "exe", "dll")]
            digests = [f"H:{k}={doc[k]['sha256']}" for k in ("packageZip", "exe", "dll")]
            body.append(f"if ($p -like '*{c[:12]}*') {{ " + "; ".join(f"'{f}'" for f in facts) + "; if (-not $noDigests) { " + "; ".join(f"'{f}'" for f in digests) + " } }")
        manifest_probe.write_text("\n".join(body) + "\n", encoding="utf-8")
        artifact_probe.write_text("param($items)\n" + f"if ({when}) {{ [Threading.Thread]::Sleep({hash_ms}) }}\n", encoding="utf-8")
        return manifest_probe, artifact_probe

    def test_artifact_preflight_r4_item1_dots_literal_case_two_builds_fast_manifest_and_a_2s_hash_runs_the_legs(self) -> None:
        # Dot's literal case: -ClaimTtlSec 60, -ClaimRenewSec 15, two builds, a 0.1 s manifest read and a 2 s hash. The claim guard has 45 s, 22 s per build. On 02612714: manifest
        # bound 21 s, hash bound 1 s, so the 2 s hash was refused SHARE_UNREACHABLE with ~41 s of the budget unused.
        manifest_probe, artifact_probe = self._timed_probes(100, 2000)
        r = self.run_plan(self.write_plan([self.item(self.specs[0], "A", self.commit_a), self.item(self.specs[1], "B", self.commit_b)]), "-ClaimTtlSec", "60", "-ClaimRenewSec", "15",
                          "-StagedBuildProbeScript", str(manifest_probe), "-ArtifactProbeScript", str(artifact_probe))
        self.assertNotIn("SHARE_UNREACHABLE", r.stdout + r.stderr)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["endReason"], b["legsAttempted"], b["legsRan"]), ("ALL_LEGS_DONE", 2, 2))

    def test_artifact_preflight_r4_item1_key3_row_l04_a_10s_post_open_hash_in_a_34s_room_runs_the_leg(self) -> None:
        # key3 row L-04: cap 95 / leg 60 / margin 0, a fast manifest read, a 10 s re-hash once the lease is open. The guard's room is 34 s. On 02612714: manifest bound 30 s, hash
        # bound 3 s, so SHARE_UNREACHABLE, exit 2, legsAttempted 0, the lease charged with no leg.
        r, held = self._post_open_hash_run(10)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["endReason"], b["legsAttempted"], b["legsRan"]), ("ALL_LEGS_DONE", 1, 1))
        self.assertEqual(len(self.leg_calls()), 1)

    def test_artifact_preflight_r4_item1_a_healthy_3s_rehash_in_a_500s_lease_runs_the_leg(self) -> None:
        # fable r3 B1's post-open sibling: a lease with 500 s of room, -LeaseMarginSec 120, -LegBudgetSec 360: the guard has 19 s. On 02612714 the manifest got 18 s and the hash 1 s,
        # so a healthy 3 s re-hash of the cached artifacts closed a lease charged with no leg. The leg runs, and the lease is not charged without one.
        r, held = self._post_open_hash_run(3, cap="2700", leg="360", margin="120", FAKE_LEASE_REMAIN="500")
        self.assertNotIn("SHARE_UNREACHABLE", r.stdout + r.stderr)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["endReason"], b["legsAttempted"], b["legsRan"]), ("ALL_LEGS_DONE", 1, 1))
        self.assertEqual(len(self.leg_calls()), 1)

    def test_artifact_preflight_r4_item1_control_a_hash_over_the_remainder_is_still_refused_inside_the_room(self) -> None:
        # the control: the one deadline still binds. A 0.1 s manifest read and a 40 s re-hash in the 34 s room end SHARE_UNREACHABLE while the leg budget is left (the r3 40 s case
        # above holds the same with a fast manifest read).
        r, held = self._post_open_hash_run(20, cap="75")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=SHARE_UNREACHABLE item=1 ", r.stderr)
        self.assertIn("could not be hashed", r.stderr)
        self.assertEqual(self.leg_calls(), [])
        self.assertLess(held, 75 - 60 + 4, f"the lease was held {held:.1f} s: the re-hash ran into the leg budget")

    # r4 ITEM 2 (sol r3 B1; Dot 6454756990): the guard's own file system reads -- a build manifest's existence (Test-Path), each input's physical path and identity
    # (Get-RunDirOutputRefusal), each leg spec's bytes (Get-LegSpecRefusal) -- ran OUTSIDE the bounded reader, so one that blocked spent the lease's leg reserve and the refusal came
    # late (sol: 45.01 s against a 34 s budget). Every guard read now runs under the guard's one deadline. -GuardReadDelay delays exactly one kind of read in one guard. The room is
    # cap 75 / leg 60 / margin 0: the post-open guard has 14 s, and the delay is 25 s.
    def _delayed_guard_read_run(self, read: str) -> tuple[subprocess.CompletedProcess, float]:
        return self._post_open_hash_run(0, "-GuardReadDelay", f"open:{read}=25", cap="75")

    def _assert_delayed_read_refused_inside_the_room(self, r: subprocess.CompletedProcess, held: float, extra: float = 0) -> None:
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=SHARE_UNREACHABLE ", r.stderr)
        self.assertIn("the guard's reads did not finish within the lease's room after the open", r.stderr)
        self.assertNotIn("LEG_BUDGET_EXCEEDS_LEASE", r.stdout + r.stderr, "the delayed read spent the leg budget before the refusal")
        self.assertEqual(self.leg_calls(), [])
        self.assertEqual([k for k in self.kinds() if not k.startswith(("OPEN", "CLOSE"))], [], "no probe and no leg ran")
        # the refusal came inside the 14 s room (plus the close), never after the 25 s read: the leg reserve was never spent
        # r5 (adversary r5 A1): a location left unknown adds the documented bounded re-read of -RunDir (at most 2 s) before the close
        self.assertLess(held, 75 - 60 + 4 + extra, f"the lease was held {held:.1f} s before the close: the delayed read ran past the guard's room")
        self.assertFalse(self.claim_path.exists(), "the claim was released (full cleanup)")

    def test_artifact_preflight_r4_item2_a_delayed_existence_read_after_the_open_ends_share_unreachable_inside_the_room(self) -> None:
        r, held = self._delayed_guard_read_run("existence")
        self._assert_delayed_read_refused_inside_the_room(r, held, extra=2)
        # where an input is was never verified: nothing more is written into -RunDir (fail closed, as an unresolvable location is)
        self.assertIn("BATCH_OUTPUT_ALIASED", r.stderr)
        self.assertIn("an input whose location is unknown is refused", r.stderr)

    def test_artifact_preflight_r4_item2_a_delayed_identity_and_path_read_after_the_open_ends_share_unreachable_inside_the_room(self) -> None:
        r, held = self._delayed_guard_read_run("identity")
        self._assert_delayed_read_refused_inside_the_room(r, held, extra=2)
        self.assertIn("BATCH_OUTPUT_ALIASED", r.stderr)
        self.assertIn("an input whose location is unknown is refused", r.stderr)

    def test_artifact_preflight_r4_item2_a_delayed_spec_read_after_the_open_ends_share_unreachable_inside_the_room(self) -> None:
        r, held = self._delayed_guard_read_run("spec")
        self._assert_delayed_read_refused_inside_the_room(r, held, extra=2)
        # #705 r14 (adversary r14 A1): every location was verified BEFORE the spec read, but the worker given up on is still in a read of the spec's share, so the spec's location
        # cannot be read again on the main thread inside the room (each later L and receipt write would, through Get-WriteDestinationHit): it is left unknown and nothing more is
        # written into -RunDir. On 80df9163 the suppression stayed off and the receipt was written through those unbounded reads.
        self.assertIn("BATCH_OUTPUT_ALIASED", r.stderr)
        self.assertIn("an input whose location is unknown is refused", r.stderr)
        self.assertFalse((self.run_dir / "batches.jsonl").exists() and self.batches(), "no receipt was written into -RunDir after the guard gave up on a share read")

    def test_artifact_preflight_r4_item2_fast_control_the_same_room_with_no_delay_runs_the_leg(self) -> None:
        r, held = self._post_open_hash_run(0, cap="75")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["endReason"], b["legsAttempted"], b["legsRan"]), ("ALL_LEGS_DONE", 1, 1))

    def test_artifact_preflight_r4_item2_no_room_control_a_lease_shorter_than_the_leg_is_refused_at_once(self) -> None:
        # no room: the location checks still run (alias detection kept), then LEG_BUDGET_EXCEEDS_LEASE before any content read, as on r3
        r, held = self._post_open_hash_run(40, cap="75", FAKE_LEASE_REMAIN="30")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=LEG_BUDGET_EXCEEDS_LEASE item=1 ", r.stderr)
        self.assertNotIn("BATCH_OUTPUT_ALIASED", r.stderr)
        self.assertLess(held, 20)
        self.assertFalse(self.claim_path.exists())

    def test_artifact_preflight_r4_adv_a1_no_room_refuses_before_a_slow_spec_read(self) -> None:
        # r4 adversary A1 (pass 1 on 31d1fdaf): cap 62 / leg 60 / margin 0 leaves the post-open guard about 1 s. Invoke-InputGuard's 10 s floor for that case (meant for the location
        # checks) let the content check read a spec that took 5 s before it noticed no time was left, so the refusal came after the leg reserve was spent. Now no content read starts.
        r, held = self._post_open_hash_run(0, "-GuardReadDelay", "open:spec=5", cap="62")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=LEG_BUDGET_EXCEEDS_LEASE ", r.stderr)
        self.assertEqual(self.leg_calls(), [])
        self.assertLess(held, 4.5, f"the lease was held {held:.1f} s: the 5 s spec read ran on a lease with no room for it")

    def test_artifact_preflight_r4_adv_p2_a1_no_room_bounds_a_slow_location_read_to_2s_not_10s(self) -> None:
        # r4 adversary pass 2 A1 (on 336d318d): with about 1 s of room the location checks were given 10 s, so a 5 s identity read ran to its end before the refusal. They get 2 s
        # now; one still running then fails closed (no more writes into -RunDir), typed with the no-room reason.
        r, held = self._post_open_hash_run(0, "-GuardReadDelay", "open:identity=5", cap="62")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=LEG_BUDGET_EXCEEDS_LEASE", r.stderr)
        self.assertIn("BATCH_OUTPUT_ALIASED", r.stderr)
        self.assertEqual(self.leg_calls(), [])
        self.assertLess(held, 4.5, f"the lease was held {held:.1f} s: the 5 s identity read ran on a lease with no room for it")

    def test_artifact_preflight_r4_adv_p2_a2_no_spec_read_starts_once_an_earlier_read_used_the_room(self) -> None:
        # r4 adversary pass 2 A2 (on 336d318d): cap 66 / leg 60 / margin 0 leaves about 5 s; two leg specs whose reads take 3 s each. The no-room check ran once, before the first
        # read, so the second started with under 2 s left. It is checked before each read now: the first read runs, the second is refused before it starts.
        items = [self.item(self.specs[0], "A", self.commit_a), self.item(self.specs[1], "A", self.commit_a)]
        r, held = self._post_open_hash_run(0, "-GuardReadDelay", "open:spec=3", cap="66", items=items)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=LEG_BUDGET_EXCEEDS_LEASE item=2 ", r.stderr)
        self.assertNotIn("BATCH_OUTPUT_ALIASED", r.stderr)
        self.assertEqual(self.leg_calls(), [])
        self.assertLess(held, 5.5, f"the lease was held {held:.1f} s: a second 3 s spec read started with under 2 s of room")

    def test_artifact_preflight_r4_item2_a_delayed_read_in_the_claim_guard_ends_typed_inside_the_claim_budget(self) -> None:
        # the claim guard's sibling: -ClaimTtlSec 60 / -ClaimRenewSec 30 gives 30 s; a 40 s identity read ends SHARE_UNREACHABLE inside it, the claim released, no lease opened,
        # never CLAIM_LAPSED_IN_GUARD and a release-and-retry
        t0 = time.monotonic()
        r = self.run_plan(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]), "-ClaimTtlSec", "60", "-ClaimRenewSec", "30", "-GuardReadDelay", "claim:identity=40")
        took = time.monotonic() - t0
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=SHARE_UNREACHABLE ", r.stderr)
        self.assertIn("the guard's reads did not finish within the claim guard", r.stderr)
        self.assertNotIn("CLAIM_LAPSED_IN_GUARD", r.stdout + r.stderr)
        self.assertEqual(self.kinds(), [], "no lease was opened")
        self.assertFalse(self.claim_path.exists(), "the claim taken for the guard was released")
        self.assertLess(took, 40, f"the run took {took:.1f} s: the 40 s read was waited for")

    # r5 ITEM 1 (key3 r4 B1, catalogue row L-05; Dot 6457724432): an attempt the claim was NOT granted for ran the guard inline with no budget, and the guard hashed every cached
    # artifact (-VerifyArtifacts) on the venue's share on every -ClaimPollSec poll while a foreign chain held the venue (and measured). Master's wait read the manifests only.
    def test_artifact_preflight_r5_l05_no_artifact_read_while_a_foreign_claim_is_waited_out_then_one_hash_under_the_claim(self) -> None:
        self._fresh_run_dir()
        state = self.share / "fake-agent-state.json"
        self.assertFalse(state.exists())
        manifest_calls, artifact_calls = self.case / "manifest-calls.txt", self.case / "artifact-calls.txt"
        manifest_probe, artifact_probe = self._timed_probes(0, 0)
        # every manifest read (one per build per guard, a denied poll's included) and every artifact hash (one per build per -VerifyArtifacts call) appends a line;
        # a hash says whether the open was answered yet (the fake agent's state file), so the claim guard's hash is told from the post-open guard's
        manifest_probe.write_text(manifest_probe.read_text(encoding="utf-8").replace("param($p)\n", f"param($p)\n[IO.File]::AppendAllText('{manifest_calls}', 'm' + [Environment]::NewLine)\n", 1),
                                  encoding="utf-8")
        artifact_probe.write_text("param($items)\n" + f"$w = $(if (Test-Path -LiteralPath '{state}' -PathType Leaf) {{ 'open' }} else {{ 'before-open' }})\n"
                                  + f"[IO.File]::AppendAllText('{artifact_calls}', $w + [Environment]::NewLine)\n", encoding="utf-8")

        def lines(p: Path) -> list[str]:
            return p.read_text(encoding="utf-8").split() if p.exists() else []

        self._foreign_live_claim()
        args, e = self.plan_cmd(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]),
                                "-StagedBuildProbeScript", str(manifest_probe), "-ArtifactProbeScript", str(artifact_probe))
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=e)
        try:
            self._wait_for_log("CLAIM_HELD")
            # the reset after the one-time preflight (Dot): CLAIM_HELD is logged after the first denied poll's guard, so the preflight's one hash is all there may be by now
            at_held = len(lines(artifact_calls))
            m0 = len(lines(manifest_calls))
            end = time.monotonic() + 120
            while len(lines(manifest_calls)) < m0 + 3 and time.monotonic() < end and p.poll() is None:
                time.sleep(0.2)
            denied_polls = len(lines(manifest_calls)) - m0
            during = len(lines(artifact_calls)) - at_held
            mark = len(lines(artifact_calls))
            self.claim_path.unlink()   # the foreign owner lets go: this run takes the claim next poll
            so, se = p.communicate(timeout=240)
        finally:
            if p.poll() is None:
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, text=True, timeout=60)
                p.wait(timeout=60)
        self.assertGreaterEqual(denied_polls, 3, f"three more denied polls re-read the manifest (the manifest check is kept while no claim is held): {so + se}")
        self.assertEqual(at_held, 1, f"only the one-time preflight hashed before the first denied poll ended; the denied poll hashed too: {lines(artifact_calls)}")
        self.assertEqual(during, 0, f"{during} artifact hashes during {denied_polls} denied polls while a foreign claim held the venue")
        self.assertEqual(p.returncode, 0, so + se)
        after = lines(artifact_calls)[mark:]
        self.assertEqual(after.count("before-open"), 1, f"exactly one hash under the claim guard's budget once the claim was granted: {after}")
        [b] = self.batches()
        self.assertEqual((b["endReason"], b["legsRan"]), ("ALL_LEGS_DONE", 1))

    # r12 (#704 merge): #704's receipt guard (Test-ReceiptWriteSuppressed) re-runs Get-InputOutputRefusal before each receipt file, with no guard budget, so on the merged tree it
    # hashed every cached artifact again (300 s bound) once per receipt file after the legs ran, and an artifact changed then would end a finished run in a typed stop. Neither parent
    # did that: the receipt guard keeps its location and manifest checks, and the artifacts are hashed only by the preflight, the claim guard and the post-open guard.
    def test_artifact_preflight_r12_the_receipt_guard_hashes_no_artifact(self) -> None:
        self._fresh_run_dir()
        state = self.share / "fake-agent-state.json"
        artifact_calls = self.case / "r12-artifact-calls.txt"
        manifest_probe, artifact_probe = self._timed_probes(0, 0)
        artifact_probe.write_text("param($items)\n" + f"$w = $(if (Test-Path -LiteralPath '{state}' -PathType Leaf) {{ 'open' }} else {{ 'before-open' }})\n"
                                  + f"[IO.File]::AppendAllText('{artifact_calls}', $w + [Environment]::NewLine)\n", encoding="utf-8")
        args, e = self.plan_cmd(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]),
                                "-StagedBuildProbeScript", str(manifest_probe), "-ArtifactProbeScript", str(artifact_probe))
        r = subprocess.run(args, capture_output=True, text=True, timeout=240, env=e)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        calls = artifact_calls.read_text(encoding="utf-8").split() if artifact_calls.exists() else []
        # the preflight and the claim guard (before the open), then the post-open guard: nothing at the receipts
        self.assertEqual(calls, ["before-open", "before-open", "open"], r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["endReason"], b["legsRan"]), ("ALL_LEGS_DONE", 1))

    # r5 ITEM 2 (sol r4 B1; key3 r4 H-B): once Invoke-InputGuard gave up on a location read (locationUnknown), Test-OpenOutputAlias re-resolved -RunDir (Get-PhysicalPath) and read
    # its identity on the main thread, unbounded, and the claim-time refusal probed batch.log: a -RunDir that stopped answering (a UNC share, a junction whose target went away) then
    # held the run before the typed refusal and the lease close. '<guard>:rundir=<sec>' stalls every read of -RunDir in that guard and after it; GUARD_READ_DELAY_SEAM on stderr names
    # each main-thread read that reached the stall.
    def test_artifact_preflight_r5_item2_a_stalled_rundir_after_the_open_reaches_the_typed_refusal_and_the_close_inside_the_room(self) -> None:
        # sol's repro: cap 75, leg budget 60, margin 0 (the post-open guard has 14 s); -RunDir's metadata reads stall 40 s once the open is answered
        r, held = self._post_open_hash_run(0, "-GuardReadDelay", "open:rundir=40", cap="75", NO_CLOSE_EXPECTED="1")
        self.assertNotIn("GUARD_READ_DELAY_SEAM", r.stderr, "a -RunDir read ran on the main thread after the guard gave up on that location")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=SHARE_UNREACHABLE ", r.stderr)
        self.assertEqual(self.leg_calls(), [])
        self.assertEqual([k for k in self.kinds() if not k.startswith(("OPEN", "CLOSE"))], [], "no probe and no leg ran")
        # the 14 s room, the close, and the documented 2 s bounded re-read of -RunDir (adversary r5 A1), never the 40 s stall
        self.assertLess(held, 75 - 60 + 4 + 2, f"the lease was held {held:.1f} s before the close: the stalled -RunDir was waited for")
        self.assertFalse(self.claim_path.exists(), "the claim was released (full cleanup)")
        self.assertIn("BATCH_OUTPUT_ALIASED", r.stderr)
        self.assertIn("an input whose location is unknown is refused", r.stderr)
        # r7 (sol r6 B2): a -RunDir re-read that does not answer leaves its location unknown, never the preflight's: the redirect fails closed and the close job is typed unwritable
        self.assertIn("BATCH_OUTPUT_REDIRECT_FAILED", r.stderr, "the redirect was made against -RunDir's preflight location")
        self.assertIn('"closeOutcome":"JOB_WRITE_FAILED"', r.stderr)

    def test_artifact_preflight_r5_item2_a_stalled_rundir_in_the_claim_guard_refuses_without_probing_batch_log(self) -> None:
        # the claim-time sibling (sol r4 B1, Stop-PlanRefused): -ClaimTtlSec 60 / -ClaimRenewSec 30 gives the claim guard 30 s; -RunDir stalls 40 s
        t0 = time.monotonic()
        r = self.run_plan(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]), "-ClaimTtlSec", "60", "-ClaimRenewSec", "30", "-GuardReadDelay", "claim:rundir=40")
        took = time.monotonic() - t0
        self.assertNotIn("GUARD_READ_DELAY_SEAM", r.stderr, "batch.log was probed after the guard gave up on -RunDir's location")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=SHARE_UNREACHABLE ", r.stderr)
        self.assertIn("an input whose location is unknown is refused", r.stderr)
        self.assertEqual(self.kinds(), [], "no lease was opened")
        self.assertFalse(self.claim_path.exists(), "the claim taken for the guard was released")
        self.assertLess(took, 40, f"the run took {took:.1f} s: the stalled -RunDir was read again after the 30 s guard")

    # r5 adversary A1 (pass 1 on 8559ef31): the location-unknown redirect check compared TEMP with -RunDir's preflight location only, so a -RunDir junction re-pointed AT TEMP before
    # the open returned got its close redirect (and the close job) made inside the current -RunDir. -RunDir is now re-read on a pool thread bounded at 2 s.
    def test_artifact_preflight_r5_adv_a1_a_rundir_junction_repointed_at_temp_gets_no_close_redirect_inside_it(self) -> None:
        old, new, link = self.case / "rd-old", self.case / "rd-new", self.case / "run-link"
        old.mkdir(parents=True, exist_ok=True)
        new.mkdir(parents=True, exist_ok=True)
        self._junction(link, old)
        self.addCleanup(lambda: os.path.lexists(link) and os.rmdir(link))
        self.run_dir = link
        args, e = self.plan_cmd(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]), "-LeaseCapSec", "75", "-LegBudgetSec", "60", "-LeaseMarginSec", "0",
                                "-GuardReadDelay", "open:identity=25", FAKE_OPEN_SLEEP="6", TEMP=str(new), TMP=str(new))
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=e)
        try:
            self._wait_for_log("BATCH_OPEN_LIVENESS_READBACK")
            # r15 (hosted CI 40963950528): the open's job file is written into -RunDir\batch-jobs AFTER the read-back line, so a repoint made at that line raced it and, on a slow
            # runner, the job write met the new target first (OUTPUT_ZONE_UNRESOLVED, the open never submitted). The repoint lands inside the open's wait, as the case means.
            self._wait_for_open_wait()
            os.rmdir(link)                 # the junction itself, not its target
            self._junction(link, new)      # -RunDir now IS TEMP
            so, se = p.communicate(timeout=240)
        finally:
            if p.poll() is None:
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, text=True, timeout=60)
                p.wait(timeout=60)
        made = [x.name for x in new.iterdir() if x.name.startswith("venue-lease-batch-close-")]
        self.assertEqual(made, [], f"the close redirect was made inside the current -RunDir: {made}\n{se}")
        self.assertIn("an input whose location is unknown is refused", se)
        self.assertIn("BATCH_OUTPUT_REDIRECT_FAILED", se)
        self.assertIn("is inside -RunDir", se)

    # r7 ITEM 2 (sol r6 B2 = adversary r6 A1 = fable/key3 r6 H1; new catalogue row): the same repoint, but the bounded re-read of -RunDir times out (open:rundir=40). The cleanup fell
    # back to where the PREFLIGHT found -RunDir (rd-old), TEMP (rd-new) was outside that, and the close redirect was MADE inside the current -RunDir while the run said it wrote nothing
    # more there. A re-read that does not answer leaves the location unknown: the redirect fails closed (FAILED, no job folder, the close typed JOB_WRITE_FAILED).
    def _repointed_rundir_run(self, guard_delay: str, repoint: bool) -> tuple[str, str, Path, int]:
        old, new, link = self.case / "rd-old", self.case / "rd-new", self.case / "run-link"
        old.mkdir(parents=True, exist_ok=True)
        new.mkdir(parents=True, exist_ok=True)
        self._junction(link, old)
        self.addCleanup(lambda: os.path.lexists(link) and os.rmdir(link))
        self.run_dir = link
        args, e = self.plan_cmd(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]), "-LeaseCapSec", "75", "-LegBudgetSec", "60", "-LeaseMarginSec", "0",
                                "-GuardReadDelay", guard_delay, FAKE_OPEN_SLEEP="6", TEMP=str(new), TMP=str(new))
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=e)
        try:
            self._wait_for_log("BATCH_OPEN_LIVENESS_READBACK")
            # r15: inside the open's wait, after its job file is written (see the r5 adversary A1 case above)
            self._wait_for_open_wait()
            if repoint:
                os.rmdir(link)                 # the junction itself, not its target
                self._junction(link, new)      # -RunDir now IS TEMP
            try:
                so, se = p.communicate(timeout=120)
            except subprocess.TimeoutExpired:
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, text=True, timeout=60)
                so, se = p.communicate(timeout=60)
                self.fail(f"the run did not end in 120 s:\n{so}\n{se}")
        finally:
            if p.poll() is None:
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, text=True, timeout=60)
                p.wait(timeout=60)
        return so, se, new, p.returncode

    def test_artifact_preflight_r7_b2_a_repointed_rundir_whose_reread_times_out_gets_no_close_redirect_inside_it(self) -> None:
        so, se, new, code = self._repointed_rundir_run("open:rundir=40", repoint=True)
        made = [x.name for x in new.iterdir() if x.name.startswith("venue-lease-batch-close-")]
        self.assertEqual(made, [], f"the close redirect was made inside the current -RunDir: {made}\n{se}")
        self.assertEqual(code, 2, so + se)
        self.assertIn("an input whose location is unknown is refused", se)
        self.assertIn("BATCH_OUTPUT_REDIRECT_FAILED", se)
        self.assertIn("did not answer within 2 s", se)
        res = batch_result(so)
        self.assertEqual(res["outputRedirect"]["state"], "FAILED", res)
        self.assertIn('"closeOutcome":"JOB_WRITE_FAILED"', se, "the failed redirect leaves the close job unwritable, typed")
        self.assertEqual(self.leg_calls(), [])

    def test_artifact_preflight_r7_b2_control_a_rundir_reread_that_answers_still_gets_the_redirect_in_an_independent_temp(self) -> None:
        # the guard gives up on a location (its identity reads stall 25 s in a 14 s room), but -RunDir's own re-read answers: TEMP (rd-new) is not -RunDir (rd-old), so the redirect is made
        so, se, new, code = self._repointed_rundir_run("open:identity=25", repoint=False)
        self.assertEqual(code, 2, so + se)
        self.assertIn("an input whose location is unknown is refused", se)
        self.assertNotIn("BATCH_OUTPUT_REDIRECT_FAILED", se, se)
        self.assertEqual(len([x for x in new.iterdir() if x.name.startswith("venue-lease-batch-close-")]), 1, se)
        self.assertEqual(batch_result(so)["outputRedirect"]["state"], "MADE")

    # r5 adversary pass 2 A2 (on 4a8e2e87): a -RunDir junction removed after the open was submitted made the worker's resolve FAIL (not time out), so the hit was a known location
    # hit and Test-OpenOutputAlias read -RunDir again on the main thread, unbounded. A location that cannot be resolved under a guard budget is a location left unknown.
    def test_artifact_preflight_r5_adv_p2_a2_a_rundir_junction_removed_after_the_open_is_not_read_again_on_the_main_thread(self) -> None:
        old, link = self.case / "rd-old", self.case / "run-link"
        self._junction(link, old)
        self.addCleanup(lambda: os.path.lexists(link) and os.rmdir(link))
        self.run_dir = link
        # r8: the open blocks until the junction is removed (FAKE_OPEN_RELEASE), not for a fixed 6 s; the 3 s -GuardReadDelay is only what a main-thread -RunDir read would stall on
        # (GUARD_READ_DELAY_SEAM on stderr says it did), so it does not have to outlast anything
        release = self._open_release()
        args, e = self.plan_cmd(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]), "-LeaseCapSec", "75", "-LegBudgetSec", "60", "-LeaseMarginSec", "0",
                                "-GuardReadDelay", "open:rundir=3", FAKE_OPEN_SLEEP="60", FAKE_OPEN_RELEASE=str(release))
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=e)
        try:
            self._wait_for_log("BATCH_OPEN_LIVENESS_READBACK")
            self._wait_for_open_wait()
            os.rmdir(link)   # the junction itself; its target stays
            release.write_text("go", encoding="utf-8")
            so, se = p.communicate(timeout=240)
        finally:
            if p.poll() is None:
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, text=True, timeout=60)
                p.wait(timeout=60)
        self.assertNotIn("GUARD_READ_DELAY_SEAM", se, "-RunDir was read again on the main thread after the guard could not resolve it")
        self.assertEqual(p.returncode, 2, so + se)
        # r12 (#704 merge, RPF-06): -RunDir's zone no longer resolves while the plan itself opens, so the hit is typed OUTPUT_ZONE_UNRESOLVED (no longer "cannot be resolved to a
        # physical path"), and under the guard budget it is still a location left unknown (no main-thread re-read, asserted above)
        self.assertIn("LEG_PLAN_REFUSED reason=OUTPUT_ZONE_UNRESOLVED ", se)
        self.assertIn("an output location could not be resolved", se)
        self.assertIn("BATCH_OUTPUT_ALIASED", se)
        self.assertEqual(self.leg_calls(), [])

    # r6 (sol r5 B1 = key3 r5 B1; new catalogue row: TEMP reaches a -RunDir that stalls after the open): after a location-unknown guard result -RunDir was re-read bounded, but TEMP
    # was not. The pair list resolved TEMP (CreateFileW) before the spelling check that rejects a nested TEMP at once, and the identity walk from TEMP up ran on the main thread too,
    # so a TEMP inside the stalled -RunDir held the typed refusal and the close. '<guard>:rundir=<sec>' now stalls every TEMP read that reaches -RunDir (TEMP_READ_DELAY_SEAM on
    # stderr says it was armed). RED on 52bd3050's logic with that seam: the 40 s stall is waited for before the close.
    def _stalled_rundir_temp_run(self, temp: Path, expect: str, armed: bool = True) -> None:
        r, held = self._post_open_hash_run(0, "-GuardReadDelay", "open:rundir=40", cap="75", TEMP=str(temp), TMP=str(temp), NO_CLOSE_EXPECTED="1")
        self.assertNotIn("GUARD_READ_DELAY_SEAM", r.stderr, "a -RunDir read ran on the main thread after the guard gave up on that location")
        self.assertIn('"closeOutcome":"JOB_WRITE_FAILED"', r.stderr, "the failed redirect leaves the close job unwritable, typed")
        if armed:
            self.assertIn("TEMP_READ_DELAY_SEAM", r.stderr, "the seam never stalled a TEMP read: this row would pass vacuously")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=SHARE_UNREACHABLE ", r.stderr)
        self.assertIn("an input whose location is unknown is refused", r.stderr)
        self.assertEqual(self.leg_calls(), [])
        # the 14 s room, the close, and the 2 s bound on the -RunDir re-read and the TEMP walk (raced, one bound), never the 40 s stall
        self.assertLess(held, 75 - 60 + 4 + 2, f"the lease was held {held:.1f} s before the close: a TEMP read that reached the stalled -RunDir was waited for")
        self.assertIn("BATCH_OUTPUT_REDIRECT_FAILED", r.stderr, "the redirect fails closed")
        self.assertIn(expect, r.stderr)
        self.assertEqual([str(x) for x in self.run_dir.rglob("venue-lease-batch-close-*")], [], "a close redirect was made inside -RunDir")
        self.assertFalse(self.claim_path.exists(), "the claim was released (full cleanup)")

    def test_artifact_preflight_r6_b1_a_temp_nested_in_a_stalled_rundir_fails_the_redirect_closed_by_spelling_before_any_temp_read(self) -> None:
        temp = self.run_dir / "tmp"
        temp.mkdir(parents=True, exist_ok=True)
        # spelling alone rejects it now, before any TEMP read, so the seam is never reached on the fixed code
        self._stalled_rundir_temp_run(temp, "is inside -RunDir", armed=False)

    def test_artifact_preflight_r6_b1_a_temp_junction_into_a_stalled_rundir_fails_the_redirect_closed_within_2s(self) -> None:
        alias = self.case / "r6-temp-junction"
        self._junction(alias, self.run_dir / "tmp")
        self.addCleanup(lambda: os.path.lexists(alias) and os.rmdir(alias))
        self._stalled_rundir_temp_run(alias, "did not answer within 2 s")

    def test_artifact_preflight_r6_b1_a_temp_whose_identity_read_stalls_fails_the_redirect_closed_within_2s(self) -> None:
        # TEMP's final path answers and is not -RunDir's (\\localhost\C$\<RunDir>\tmp); the identity read of the folder that IS -RunDir stalls with it
        (self.run_dir / "tmp").mkdir(parents=True, exist_ok=True)
        self._stalled_rundir_temp_run(self._loopback_unc(self.run_dir) / "tmp", "did not answer within 2 s")

    def test_artifact_preflight_r6_b1_control_an_independent_answering_temp_still_gets_the_redirect_fast(self) -> None:
        # sol r5 INV-4 row 1: TEMP elsewhere and answering gets the redirect. r7 (sol r6 B2): only when -RunDir's own re-read answers too (here the guard gives up on its identity
        # reads, open:identity=25 in the 14 s room); a -RunDir re-read that times out is no longer replaced by the preflight's location (the r7_b2 rows)
        temp = self.case / "r6-temp-elsewhere"
        temp.mkdir(parents=True, exist_ok=True)
        r, held = self._post_open_hash_run(0, "-GuardReadDelay", "open:identity=25", cap="75", TEMP=str(temp), TMP=str(temp))
        self.assertIn("an input whose location is unknown is refused", r.stderr)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertNotIn("TEMP_READ_DELAY_SEAM", r.stderr)
        self.assertNotIn("BATCH_OUTPUT_REDIRECT_FAILED", r.stderr, r.stderr)
        self.assertEqual(len(list(temp.glob("venue-lease-batch-close-*"))), 1, r.stderr)
        self.assertLess(held, 75 - 60 + 4 + 2, f"the lease was held {held:.1f} s before the close")

    # r5 adversary A2 (pass 1 on 8559ef31): no content read starts with under 2 s of the guard's room, but Get-VenueStagedBuildManifest started the hash with 1 s left of its call.
    # cap 66 / leg 60 / margin 0 leaves the post-open guard about 5 s (one build: a 4 s read); the manifest read takes 2.2 s once the open is answered, so the hash would start with 1.8 s.
    # r7 (sol r6 B1): the share is no longer floored (4.x s, not 4), so the read takes 3 s to leave the hash under 2 s, as 2.2 s did of a floored 4
    def test_artifact_preflight_r5_adv_a2_no_artifact_hash_starts_with_under_2s_of_the_guard_room(self) -> None:
        state = self.share / "fake-agent-state.json"
        manifest_probe, _ = self._timed_probes(3000, 0, when=f"(Test-Path -LiteralPath '{state}' -PathType Leaf)")
        r, held = self._post_open_hash_run(0, "-StagedBuildProbeScript", str(manifest_probe), cap="66")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=SHARE_UNREACHABLE item=1 ", r.stderr)
        self.assertIn("no time is left of -TimeoutSec", r.stderr)
        self.assertEqual(self.leg_calls(), [])

    # r5 adversary A3 (pass 1 on 8559ef31): each build got floor(room / builds), so N healthy fast builds in a room under 2N s were refused before any read and the opened lease charged
    # with no leg. Two builds, cap 65 / leg 60 / margin 0: about 4 s of room, 1 s each on 8559ef31; the real reads take well under a second.
    def test_artifact_preflight_r5_adv_a3_two_fast_builds_in_a_4s_room_are_read_not_refused(self) -> None:
        items = [self.item(self.specs[0], "A", self.commit_a), self.item(self.specs[1], "B", self.commit_b)]
        r, held = self._post_open_hash_run(0, cap="65", items=items)
        self.assertNotIn("to read and hash the staged build", r.stdout + r.stderr, "a build was refused for its split of the room before any read")
        self.assertNotIn("LEG_PLAN_REFUSED", r.stderr)
        self.assertGreaterEqual(len(self.leg_calls()), 1, r.stdout + r.stderr)

    # r7 ITEM 1 (sol r6 B1 = adversary r6 A2; new catalogue row): the guard floored its room to whole seconds (2.8 -> 2) and Get-VenueStagedBuildManifest floored the remainder after
    # the manifest read again (1.87 -> 1), under its -ArtifactMinSec 2: a room of 2.8 or 2.99 s, a 100 ms manifest read and a 100 ms matching hash were refused SHARE_UNREACHABLE
    # with no hash (after the open, a lease closed and charged with no leg). Run on the runner's own functions (New-GuardBudget, Get-InputContentRefusal, lifted from its AST) with
    # offline probes; each manifest read and each hash appends a line.
    def _guard_room_run(self, room: float, manifest_ms: int = 100, hash_ms: int = 100) -> dict:
        c = "3" * 40
        reads, hashes = self.case / f"room-{room}-{manifest_ms}-reads.txt", self.case / f"room-{room}-{manifest_ms}-hashes.txt"
        bounds = self.case / f"room-{room}-{manifest_ms}-bounds.txt"
        facts = ("('a' * 64); 'C:" + c + "'; 'D:' + ('f' * 64); 'S:bool'; "
                 "'A:packageZip=ok'; 'H:packageZip=' + ('1' * 64); 'A:exe=ok'; 'H:exe=' + ('2' * 64); 'A:dll=ok'; 'H:dll=' + ('3' * 64)")
        script = (f"Import-Module '{MODULE}' -Force -DisableNameChecking\n"
                  f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{RUNNER}', [ref]$null, [ref]$null)\n"
                  "foreach ($n in 'New-GuardBudget', 'Get-InputContentRefusal') { . ([scriptblock]::Create($ast.Find({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq $n }, $true).Extent.Text)) }\n"
                  # the runner's own top-level threshold, when it has one
                  "$min = $ast.Find({ param($x) $x -is [System.Management.Automation.Language.AssignmentStatementAst] -and $x.Left.Extent.Text -ceq '$script:GuardReadMinSec' }, $true)\n"
                  "if ($min) { . ([scriptblock]::Create($min.Extent.Text)) }\n"
                  f"$legPlanMode = $true; $AgentShare = '{self.share}'; $script:guardProgress = $null\n"
                  f"$script:guardedInputs = @([pscustomobject]@{{ kind = 'build'; item = 1; path = 'manifest'; commit = '{c}'; sha256 = ('a' * 64) }})\n"
                  f"$stagedBuildProbe = {{ param($p) [IO.File]::AppendAllText('{reads}', 'm' + [Environment]::NewLine); [Threading.Thread]::Sleep({manifest_ms}); {facts} }}\n"
                  f"$artifactProbe = {{ param($i) [IO.File]::AppendAllText('{hashes}', 'h' + [Environment]::NewLine); [IO.File]::AppendAllText('{bounds}', ([string]($i[0].endSec - $i[0].clock.Elapsed.TotalSeconds) + [Environment]::NewLine)); [Threading.Thread]::Sleep({hash_ms}) }}\n"
                  # r11 (hosted CI 40805584834): the room is measured on the real clock, and Invoke-VenueBoundedRead starts a runspace per read -- a cost the injected probes do not
                  # control (warming the first one, adversary r7 A3, left every later start). A 2.3 s room holds only 0.3 s of it above the hash's 2 s minimum, and a loaded hosted runner
                  # spent 190 ms there ("1.99 s left, under 2"). The room tests pin the BUDGET ARITHMETIC, so the reads run in-process here (the same script text, argument and result
                  # shape; the runspace itself and its timeout are pinned by the bounded-read tests) and the only time a read takes is the probe's own sleep.
                  "& (Get-Module VenueChain) { function script:Invoke-VenueBoundedRead { param([string]$ScriptText, $Argument, [double]$TimeoutSec) "
                  "[pscustomobject]@{ answered = $true; value = @(& ([scriptblock]::Create($ScriptText)) $Argument); error = $null } } }\n"
                  # r15 (hosted CI 40963950528: "bound 1.91397" in a 2.3 s room; reproduced on Virtual-Ten as "2.25 s share ... 1.93 s left"): the first pass through the guard, the
                  # module and the probes pays PowerShell's one-time parse and compile cost, up to 0.3 s on a loaded host, on the room's real clock. One identical pass in a 30 s room
                  # first, its probe lines discarded, so the measured pass is charged only what the probes sleep and the budget arithmetic is what the room tests pin.
                  "$script:guardBudget = New-GuardBudget 30 'the warm-up room' 'LEG_BUDGET_EXCEEDS_LEASE' $false 'open'\n"
                  "$null = Get-InputContentRefusal\n"
                  f"foreach ($f in '{reads}', '{hashes}', '{bounds}') {{ if (Test-Path -LiteralPath $f) {{ Remove-Item -LiteralPath $f }} }}\n"
                  f"$script:guardBudget = New-GuardBudget {room} 'the test room' 'LEG_BUDGET_EXCEEDS_LEASE' $false 'open'\n"
                  "$hit = Get-InputContentRefusal\n"
                  "ConvertTo-Json -Compress -InputObject @{ code = [string]$hit.code; why = [string]$hit.why; ms = [int]$script:guardBudget.clock.Elapsed.TotalMilliseconds }\n"
                  "Complete-VenueProcess -ExitCode 0\n")
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        got = json.loads(out.stdout.strip().splitlines()[-1])
        got["reads"] = len(reads.read_text(encoding="utf-8").split()) if reads.exists() else 0
        got["hashes"] = len(hashes.read_text(encoding="utf-8").split()) if hashes.exists() else 0
        # the bound the (first) hash was given, on the call's own clock: what was left of the room after the manifest read
        given = bounds.read_text(encoding="utf-8").split() if bounds.exists() else []
        got["bound"] = float(given[0]) if given else None
        return got

    def test_artifact_preflight_r7_b1_a_healthy_build_in_a_2_8s_guard_room_is_validated_with_one_hash(self) -> None:
        got = self._guard_room_run(2.8)
        self.assertEqual((got["code"], got["hashes"]), ("", 1), f"a 2.8 s room, a 100 ms read and a 100 ms hash: {got}")

    def test_artifact_preflight_r7_b1_a_healthy_build_in_a_2_99s_guard_room_is_validated_with_one_hash(self) -> None:
        got = self._guard_room_run(2.99)
        self.assertEqual((got["code"], got["hashes"]), ("", 1), f"a 2.99 s room, a 100 ms read and a 100 ms hash: {got}")

    def test_artifact_preflight_r7_b1_control_a_3_1s_guard_room_is_validated_with_one_hash(self) -> None:
        got = self._guard_room_run(3.1)
        self.assertEqual((got["code"], got["hashes"]), ("", 1), got)

    def test_artifact_preflight_r7_b1_control_a_room_under_the_minimum_is_refused_typed_before_any_read(self) -> None:
        got = self._guard_room_run(1.9)
        self.assertEqual((got["code"], got["reads"], got["hashes"]), ("LEG_BUDGET_EXCEEDS_LEASE", 0, 0), got)
        self.assertIn("no time is left in the test room", got["why"])

    # r10 B1 (sol r9 = Dot cc5b2a97): `[Math]::Min(330, <double>)` takes the Int32 overload (an integer first operand), so a guard room of 2.3 / 2.4 / 2.49 s became a 2 s share, the hash was
    # left under its 2 s minimum after a 100 ms manifest read and a healthy staged build was refused SHARE_UNREACHABLE with no hash (after the open, a lease closed and charged with no leg).
    # A room of 2.8 rounded UP to 3 and hid it. The share keeps its fraction, and the hash is given what is left of the room, never more.
    def _assert_fractional_room_validates(self, room: float) -> None:
        got = self._guard_room_run(room)
        self.assertEqual((got["code"], got["hashes"]), ("", 1), f"a {room} s room, a 100 ms read and a 100 ms hash: {got}")
        self.assertIsNotNone(got["bound"], got)
        self.assertGreaterEqual(got["bound"], 2.0, f"the hash started under its 2 s minimum: {got}")
        self.assertLessEqual(got["bound"], room - 0.1, f"the hash was given more than the room left after the 100 ms read: {got}")

    def test_artifact_preflight_r10_b1_a_healthy_build_in_a_2_3s_guard_room_is_validated_with_one_hash(self) -> None:
        self._assert_fractional_room_validates(2.3)

    def test_artifact_preflight_r10_b1_a_healthy_build_in_a_2_4s_guard_room_is_validated_with_one_hash(self) -> None:
        self._assert_fractional_room_validates(2.4)

    def test_artifact_preflight_r10_b1_a_healthy_build_in_a_2_49s_guard_room_is_validated_with_one_hash(self) -> None:
        self._assert_fractional_room_validates(2.49)

    def test_artifact_preflight_r10_b1_a_room_that_rounded_up_is_not_overshot_by_the_hash_bound(self) -> None:
        # the other direction of the same defect: 2.6 and 2.8 rounded UP to a 3 s share, and the hash was given more than the room held
        for room in (2.6, 2.8):
            got = self._guard_room_run(room)
            self.assertEqual((got["code"], got["hashes"]), ("", 1), got)
            self.assertLessEqual(got["bound"], room - 0.1, f"a {room} s room: the hash was given more than it held: {got}")

    def test_artifact_preflight_r10_b1_the_refusal_after_a_slow_read_names_the_fractional_bound_and_time_left(self) -> None:
        # room 2.4 admits the read (2 s minimum) but a 500 ms read leaves the hash about 1.9 s: refused typed, and the message carries the sub-second quantities it measured
        got = self._guard_room_run(2.4, manifest_ms=500)
        self.assertEqual((got["code"], got["hashes"]), ("SHARE_UNREACHABLE", 0), got)
        m = re.search(r"no time is left of -TimeoutSec (\d+(?:\.\d+)?) after the manifest read \((\d+(?:\.\d+)?) s left, under 2\)", got["why"])
        self.assertIsNotNone(m, got["why"])
        self.assertTrue(2.3 <= float(m.group(1)) <= 2.4, f"-TimeoutSec should name the 2.4 s room less the read start, not a rounded integer: {got['why']}")
        self.assertTrue(1.5 < float(m.group(2)) < 2.0 and "." in m.group(2), f"the time left is under 2 and fractional, never rounded up to '2' or down to '1': {got['why']}")

    def test_artifact_preflight_r10_b1_sibling_a_time_left_just_under_the_minimum_is_not_reported_as_the_minimum(self) -> None:
        # [Math]::Max(0, $left) with an integer first operand rounded 1.9 to "2 s left, under 2"; room 3.0 and a 1 s read leave the hash just under 2 s
        got = self._guard_room_run(3.0, manifest_ms=1000)
        self.assertEqual((got["code"], got["hashes"]), ("SHARE_UNREACHABLE", 0), got)
        m = re.search(r"\((\d+(?:\.\d+)?) s left, under 2\)", got["why"])
        self.assertIsNotNone(m, got["why"])
        self.assertLess(float(m.group(1)), 2.0, f"'{m.group(0)}' reports the time left rounded up to the minimum it is under: {got['why']}")

    def test_artifact_preflight_r7_b1_control_no_hash_starts_with_under_the_minimum_left_after_the_read(self) -> None:
        # the read is admitted (2.3 s left) but takes 600 ms: the hash would start with about 1.7 s, so none starts, and the call still ends inside the room
        got = self._guard_room_run(2.3, manifest_ms=600)
        self.assertEqual((got["code"], got["reads"], got["hashes"]), ("SHARE_UNREACHABLE", 1, 0), got)
        self.assertIn("no time is left of -TimeoutSec", got["why"])
        self.assertLess(got["ms"], 2300, got)

    # r7 adversary A2 (pass 1 on dbe8a7a0): -ArtifactMinSec was checked once, before the three-file hash worker, so after a slow packageZip the exe and the dll started with under
    # the minimum. The worker now checks it before EACH file. A hash bound (-ArtifactTimeoutSec 1) under the minimum (-ArtifactMinSec 2) starts no file at all; RED on dbe8a7a0,
    # which hashed all three (STAGED, no problem) inside that 1 s.
    def test_artifact_preflight_r7_adv_a2_no_artifact_file_starts_hashing_with_under_the_minimum_left(self) -> None:
        c = self.commit_a
        for k in ("packageZip", "exe", "dll"):
            self.assertTrue(self.artifact_path(c, k).exists(), k)
        script = (f"Import-Module '{MODULE}' -Force -DisableNameChecking\n"
                  f"$r = Get-VenueStagedBuildManifest -AgentShare '{self.share}' -SourceCommit '{c}' -TimeoutSec 30 -VerifyArtifacts -ArtifactTimeoutSec 1 -ArtifactMinSec 2\n"
                  "ConvertTo-Json -Compress -InputObject @{ state = $r.state; error = [string]$r.error; problem = [string]$r.artifactProblem }\n"
                  "Complete-VenueProcess -ExitCode 0\n")
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        got = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(got["state"], "UNREACHABLE", got)
        self.assertIn("to start hashing packageZip", got["error"])
        # control: the same build with room for every file is STAGED and matches
        script = script.replace(" -ArtifactTimeoutSec 1 -ArtifactMinSec 2", "")
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        got = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual((got["state"], got["problem"]), ("STAGED", ""), got)

    # r7 adversary A1 (pass 1 on dbe8a7a0): the guard capped a build's share at 300 s while the room was larger, so a 310 s post-open hash (one the preflight's 30 s + 300 s
    # bounds accept) in a 519 s room was refused SHARE_UNREACHABLE and the lease charged with no leg. The cap is now the preflight's own 330 s. The adversary's input, literally.
    # r8: the hash bound is read, not waited out. Invoke-VenueBoundedRead gives the post-open hash exactly (endSec - clock) seconds, so the probe records that bound at its start and
    # hashes at once: a 310 s hash is accepted iff the bound it was given is over 310 s. RED on a 300 s cap: the bound is at most 300 s and this fails, as the 310 s sleep did.
    def test_artifact_preflight_r7_adv_a1_a_310s_post_open_hash_in_a_519s_room_runs_the_leg(self) -> None:
        bounds = self.case / "post-open-hash-bounds.txt"
        bounds.unlink(missing_ok=True)
        record = f"[IO.File]::AppendAllText('{bounds}', ([string]($items[0].endSec - $items[0].clock.Elapsed.TotalSeconds) + [Environment]::NewLine))"
        r, held = self._post_open_hash_run(0, "-DeadlineUtc", utc_iso(datetime.now(timezone.utc) + timedelta(seconds=900)), cap="1000", leg="360", margin="120", post_open=record)
        self.assertNotIn("LEG_PLAN_REFUSED", r.stderr, r.stderr)
        [b] = self.batches()
        self.assertEqual((b["endReason"], b["legsRan"]), ("ALL_LEGS_DONE", 1), b)
        given = [float(x) for x in bounds.read_text(encoding="utf-8").split()]
        self.assertTrue(given, "the post-open hash probe ran")
        self.assertGreater(max(given), 310, f"the post-open hash was given at most {max(given):.1f} s: a 310 s hash the preflight accepts would be refused")
        self.assertLessEqual(max(given), 330, f"the post-open hash was given {max(given):.1f} s: over the preflight's own 330 s bounds")

    def test_artifact_preflight_r7_adv_a1_the_guard_share_cap_is_the_preflights_own_bounds(self) -> None:
        # r10: the cap is pinned by its VALUE and its type, not by one spelling: `Min(330, ...)` with an integer first operand was the r9 B1 defect (Int32 overload), and any double
        # spelling of the same cap (330.0, [double]330, 330.00) is the contract
        text = RUNNER.read_text(encoding="utf-8")
        guard = text[text.index("function Get-InputContentRefusal"):text.index("function Stop-InputIsOutput")]
        m = re.search(r"\$share = \[Math\]::Min\((\[double\]\s*)?(\d+(?:\.\d+)?)\s*,\s*\$script:guardBudget\.sec\s*-\s*\$script:guardBudget\.clock\.Elapsed\.TotalSeconds\)", guard)
        self.assertIsNotNone(m, "the guard's share is Min(<cap>, <budget> - <elapsed>)")
        self.assertEqual(float(m.group(2)), 330.0, "the cap is the preflight's own 30 s + 300 s bounds")
        self.assertTrue(m.group(1) or "." in m.group(2), "the cap is a double: an integer first operand takes the Int32 overload and rounds the room (2.3 s -> 2)")
        self.assertIn("-TimeoutSec 30 -VerifyArtifacts -ArtifactTimeoutSec 300 }", text, "the preflight's bounds the cap is the sum of")

    def test_artifact_preflight_r10_no_dual_venue_math_min_or_max_rounds_a_double_through_an_integer_first_operand(self) -> None:
        # the class, not the :771 instance: every [Math]::Min/Max in the dual-venue folder whose second operand is a double (a TimeSpan total, a clock, a budget) has a double first operand too
        bad = []
        for path in sorted(DV.glob("*.ps*1")):
            for line, call in ps_integer_first_minmax(path.read_text(encoding="utf-8")):
                bad.append(f"{path.name}:{line}: {call}")
        self.assertEqual(bad, [], "make the first operand a double (330.0, [double]$x): an integer one selects the Int32 overload and rounds the double")

    def test_artifact_preflight_r10_adv_a3_the_windows_pwsh_skip_stays_on_the_runner_class_and_no_helper_carries_it(self) -> None:
        # a helper block inserted between `@requires_windows_pwsh` and its class moved the skip onto the helper's def (the helper then raised SkipTest, and the class ran unguarded
        # on a host with no pwsh). The decorator belongs on VenueLeaseBatchRunnerTests; the scanners above it are plain functions.
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        decorated = {n.name: [ast.unparse(d) for d in n.decorator_list] for n in tree.body if isinstance(n, (ast.ClassDef, ast.FunctionDef))}
        self.assertIn("requires_windows_pwsh", decorated["VenueLeaseBatchRunnerTests"])
        self.assertEqual({name: d for name, d in decorated.items() if name.startswith(("ps_", "_ps_")) and d}, {})

    def test_artifact_preflight_r10_adv_a3_a_remainder_just_under_the_minimum_is_not_printed_as_the_minimum(self) -> None:
        # the module's own refusal line, run with a remainder of 1.999 s (Round(.., 2) printed "2 s left, under 2"); the line is lifted from the source, not re-spelled here
        line = next(l for l in MODULE.read_text(encoding="utf-8").splitlines() if "after the manifest read (" in l and "$left -lt $ArtifactMinSec" in l)
        script = ("$left = 1.999; $TimeoutSec = 3; $ArtifactMinSec = 2; $path = 'p'\n"
                  "$r = & { " + line.strip() + " }\n"
                  "ConvertTo-Json -Compress -InputObject @{ error = [string]$r.error }\n")
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        error = json.loads(out.stdout.strip().splitlines()[-1])["error"]
        m = re.search(r"\((\d+(?:\.\d+)?) s left, under 2\)", error)
        self.assertIsNotNone(m, error)
        self.assertLess(float(m.group(1)), 2.0, error)

    def test_artifact_preflight_r10_the_math_overload_scanner_sees_every_spelling(self) -> None:
        bad = [
            "[Math]::Min(330, $b.sec - $b.clock.Elapsed.TotalSeconds)",
            "[math]::MAX( 0 , $left )",
            "[System.Math]::Max(0, $left)",
            "[Math]::Min($Wait, ($d - [DateTime]::UtcNow).TotalSeconds)",
            "[int][Math]::Max(60, [Math]::Min($w, ($d - [DateTime]::UtcNow).TotalSeconds))",
            "$x = [Math]::Max(1000, ($deadline - [DateTime]::UtcNow).TotalMilliseconds + 10)",
            "[Math]::Min(2, [double]$remaining)",
        ]
        for text in bad:
            self.assertTrue(ps_integer_first_minmax(text), text)
        good = [
            "[Math]::Min(330.0, $b.sec - $b.clock.Elapsed.TotalSeconds)",
            "[Math]::Max(0.0, $left)",
            "[Math]::Min([double]$Wait, ($d - [DateTime]::UtcNow).TotalSeconds)",
            "[Math]::Min([double]330, $left)",
            "[Math]::Max($a.TotalSeconds, 0)",
            "[Math]::Min($LeaseCapSec, [Math]::Max(0, $bw.budgetRoomSec))",
            "[Math]::Min($ClaimPollSec, 5)",
            "[Math]::Min($Cap, [int][Math]::Floor(($d - [DateTime]::UtcNow).TotalSeconds))",
            "[Math]::Max(1, [Math]::Floor(($e - [DateTime]::UtcNow).TotalSeconds))",
            "# [Math]::Min(330, $b.sec - $b.clock.Elapsed.TotalSeconds) is the defect",
            "$x = 1 # [Math]::Max(0, $left)",
        ]
        for text in good:
            self.assertEqual(ps_integer_first_minmax(text), [], text)

    def test_artifact_preflight_r4_the_guard_read_delay_seam_is_refused_with_the_real_um_run_and_checked_for_shape(self) -> None:
        args, e = self.plan_cmd(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]), "-GuardReadDelay", "open:spec=5")
        args[args.index("-UmRunScript") + 1] = str(RUNNER.parent.parent / "um-run.ps1")
        r = subprocess.run(args, capture_output=True, text=True, timeout=120, env=e)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("BATCH_USAGE -GuardReadDelay is an offline test seam", r.stderr)
        # r4 adversary A3: a non-ASCII decimal digit (U+0661) is refused typed, not accepted by \d and thrown on by [int]
        for bad in ("open:hash=5", "open:spec=١"):
            r = self.run_plan(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]), "-GuardReadDelay", bad)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("is not <claim|open>:<existence|identity|spec|rundir>=<sec>", r.stderr)
        self.assertEqual(self.kinds(), [])

    # r4 ITEM 3 (key3 r3 H3): the claim guard runs before the batch try, so a throw in it once the claim was granted left the claim held, with no keeper, until its TTL (and the run
    # ended exit 1). Input: a manifest read that emits A:<k>=ok without the paired H:<k>= digest lines, only inside the guard (the claim file exists), so the preflight passes and
    # Get-VenueStagedBuildManifest's .Substring on the missing digest throws in the guard.
    def test_artifact_preflight_r4_item3_a_throw_in_the_claim_guard_releases_the_claim_and_ends_typed(self) -> None:
        in_guard = f"(Test-Path -LiteralPath '{self.claim_path}' -PathType Leaf)"
        manifest_probe, artifact_probe = self._timed_probes(0, 0, no_digests_when=in_guard)
        r = self.run_plan(self.write_plan([self.item(self.specs[0], "A", self.commit_a)]),
                          "-StagedBuildProbeScript", str(manifest_probe), "-ArtifactProbeScript", str(artifact_probe))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=LEG_PLAN_INVALID ", r.stderr)
        self.assertFalse(self.claim_path.exists(), "the claim granted before the throw was released")
        self.assertEqual(self.kinds(), [], "no lease was opened")

    def test_artifact_preflight_the_artifact_names_are_the_ones_the_leg_job_derives(self) -> None:
        script = (f"Import-Module '{ROOT / 'tools' / 'profiling' / 'bachelor' / 'AttrCudaArtifacts.psm1'}' -Force -DisableNameChecking\n"
                  "$n = Get-AttrCudaArtifactNames -SourceCommit ('3' * 40)\n"
                  "ConvertTo-Json -Compress -InputObject ([ordered]@{ exe = $n.exeName; dll = $n.reconName; packageZip = $n.packageZipName })\n")
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        for label, name in json.loads(out.stdout).items():
            self.assertEqual(self.artifact_path("3" * 40, label).name, name, label)

    # r13 (adversary r13 A2): the open can wait long in the agent's gate, and a link to an output made in that wait was written through by the first probe (gate.log).
    def test_r13_adv_a2_an_output_alias_added_while_the_open_waits_is_refused_before_any_probe(self) -> None:
        # gate.log: the probe's -GateLog, which Wait-VenueQuiet appends to. batch.log (r13 pass 2 A1): the runner's own log, which the finally wrote through after the refusal.
        for leaf in ("gate.log", "batch.log"):
            with self.subTest(output=leaf):
                self._open_wait_alias(leaf)

    def _open_wait_alias(self, leaf: str) -> None:
        self._fresh_run_dir()
        spec = self.case / "safe" / "fx-a.json"
        spec.parent.mkdir(parents=True, exist_ok=True)
        spec.unlink(missing_ok=True)
        shutil.copy2(self.specs[0], spec)
        before = spec.read_text(encoding="utf-8")
        args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]), FAKE_OPEN_SLEEP="12")
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=e)
        try:
            # the last line before um-run blocks (FAKE_OPEN_SLEEP); a link made before it is written through at once, the race no re-check closes
            self._wait_for_log("BATCH_OPEN_LIVENESS_READBACK")
            self._wait_for_open_wait()   # r15: the open's wait itself, never a fixed sleep after the read-back line
            (self.run_dir / leaf).unlink(missing_ok=True)
            os.link(spec, self.run_dir / leaf)
            so, se = p.communicate(timeout=240)
        finally:
            if p.poll() is None:
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, text=True, timeout=60)
                p.wait(timeout=60)
        self.assertEqual(p.returncode, 2, so + se)
        self.assertIn("LEG_PLAN_REFUSED reason=LEG_SPEC_IS_RUN_OUTPUT item=1 ", se)
        self.assertIn("found after the open", se)
        self.assertEqual([k for k in self.kinds() if not k.startswith(("OPEN", "CLOSE"))], [], "no probe and no leg ran")
        self.assertEqual(self.leg_calls(), [])
        self.assertTrue(any(k.startswith("CLOSE") for k in self.kinds()), "the lease was closed")
        self.assertFalse(self.claim_path.exists(), "the claim was released")
        self.assertEqual(spec.read_text(encoding="utf-8"), before, "the accepted input is byte-unchanged")

    # r14 (Dot 2e6eb21d; adversary r13 pass 3 A1, A2): the post-open guard ran only on a clean receipt, and its write suppression was set only in the refusal branch. An open that
    # failed (OPEN_FAILED), came back without a lease batch (AGENT_NOT_BATCH_CAPABLE), or was held in the agent's gate (logged before the guard; or held to the deadline, the guard
    # skipped) won the classification, and the cleanup log lines and the result line were appended through a batch.log that had become a hard link to the accepted spec.
    def test_r14_an_alias_made_in_the_open_wait_is_never_written_through_whatever_the_open_returned(self) -> None:
        cases = {
            # mode: (exit code, the typed reason the run still reports, extra args)
            "exit1": (9, "OPEN_FAILED", ()),
            "nobatch": (10, "AGENT_NOT_BATCH_CAPABLE", ()),
            "gatewait": (2, "LEG_SPEC_IS_RUN_OUTPUT", ()),
            # r8: the held open is waited for until the deadline, so the deadline is the case's whole run time: 20 s (it was 60) is still 6-7 times what the run needs to reach the open
            "gatehold": (11, "OPEN_CANCEL_UNRECONCILED", ("-CancelReconcileSec", "3", "-DeadlineUtc", "+20")),
        }
        for mode, (code, reason, extra) in cases.items():
            with self.subTest(open=mode):
                # the deadline is placed when this subtest starts, not when the table was built (the subtests before it take a minute or more under load)
                extra = tuple(utc_iso(datetime.now(timezone.utc) + timedelta(seconds=int(x[1:]))) if x.startswith("+") else x for x in extra)
                self._alias_in_open_wait(mode, code, reason, extra)

    def _alias_in_open_wait(self, mode: str, code: int, reason: str, extra: tuple[str, ...]) -> None:
        import hashlib
        self._fresh_run_dir()
        (self.share / "fake-agent-state.json").unlink(missing_ok=True)
        shutil.rmtree(self.share / "outbox", ignore_errors=True)
        spec = self.case / "safe" / "fx-a.json"
        spec.parent.mkdir(parents=True, exist_ok=True)
        spec.unlink(missing_ok=True)
        shutil.copy2(self.specs[0], spec)
        before = hashlib.sha256(spec.read_bytes()).hexdigest()
        # r8: the open blocks until the link is made (FAKE_OPEN_RELEASE) -- 60 s is only its bound -- instead of for a fixed 12 s
        release = self._open_release()
        args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]), *extra, FAKE_OPEN_SLEEP="60", FAKE_OPEN_RELEASE=str(release), FAKE_OPEN_MODES=mode)
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=e)
        try:
            self._wait_for_log("BATCH_OPEN_LIVENESS_READBACK")
            self._wait_for_open_wait()
            (self.run_dir / "batch.log").unlink(missing_ok=True)
            os.link(spec, self.run_dir / "batch.log")
            release.write_text("go", encoding="utf-8")
            so, se = p.communicate(timeout=300)
        finally:
            if p.poll() is None:
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, text=True, timeout=60)
                p.wait(timeout=60)
        self.assertEqual(hashlib.sha256(spec.read_bytes()).hexdigest(), before, "the accepted input is byte-unchanged: nothing was appended through the alias")
        self.assertEqual(p.returncode, code, so + se)
        self.assertEqual(batch_result(so)["stopReason"], reason, "the typed reason still reaches stdout")
        self.assertIn("BATCH_OUTPUT_ALIASED ", se, "the alias is said on stderr")
        self.assertEqual(self.leg_calls(), [], "no leg ran")
        self.assertFalse(self.claim_path.exists(), "the claim was released")
        self.assertTrue(any(k.startswith("CLOSE") for k in self.kinds()), "the lease was closed")

    # adversary r14 A1, A2, A3 (on 2ea93e0f): the redirect folder was named from the token alone and reused with -Force, so links prepared in it were written through; TEMP inside -RunDir
    # put the redirect back in it; a redirect that could not be made threw, and the close then threw out of the finally (no claim release, no result); and the claim keeper went on
    # rewriting a claim file that had become the input until the close returned.
    def _spawn_open_wait(self, mode: str, *extra: str, env: dict | None = None, claim_dir: Path | None = None):
        import hashlib
        self._fresh_run_dir()
        (self.share / "fake-agent-state.json").unlink(missing_ok=True)
        shutil.rmtree(self.share / "outbox", ignore_errors=True)
        spec = self.case / "safe" / "fx-a.json"
        spec.parent.mkdir(parents=True, exist_ok=True)
        spec.unlink(missing_ok=True)
        shutil.copy2(self.specs[0], spec)
        args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]), *extra, FAKE_OPEN_SLEEP="12", FAKE_OPEN_MODES=mode, **(env or {}))
        if claim_dir is not None:
            args[args.index("-ClaimDir") + 1] = str(claim_dir)
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=e)
        self._wait_for_log("BATCH_OPEN_LIVENESS_READBACK")
        self._wait_for_open_wait()   # r15: the open's wait itself, never a fixed sleep after the read-back line
        return p, spec, hashlib.sha256

    def _finish(self, p) -> tuple[str, str]:
        try:
            return p.communicate(timeout=300)
        finally:
            if p.poll() is None:
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, text=True, timeout=60)
                p.wait(timeout=60)

    def _open_token(self) -> str:
        [job] = [j.name for j in (self.share / "inbox").iterdir() if j.name.startswith("lease-batch-open-")]
        return job[: -len(".job.ps1")][-12:]

    def test_r14_adv_a1_a_redirect_folder_prepared_with_links_is_never_written_through(self) -> None:
        tmp = self.case / "r14-temp"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        shutil.rmtree(self.share / "inbox", ignore_errors=True)
        p, spec, sha = self._spawn_open_wait("exit1", env={"TEMP": str(tmp), "TMP": str(tmp)})
        before = sha(spec.read_bytes()).hexdigest()
        token = self._open_token()
        prepared = tmp / f"venue-lease-batch-close-{token}"
        prepared.mkdir()
        os.link(spec, prepared / "batch.log")
        [close] = [f"lease-batch-close-{j.name[len('lease-batch-open-'):]}" for j in (self.share / "inbox").iterdir() if j.name.startswith("lease-batch-open-")]
        os.link(spec, prepared / close)
        (self.run_dir / "batch.log").unlink(missing_ok=True)
        os.link(spec, self.run_dir / "batch.log")
        so, se = self._finish(p)
        self.assertEqual(sha(spec.read_bytes()).hexdigest(), before, "nothing was written through a link prepared in the redirect folder")
        self.assertEqual(p.returncode, 9, so + se)
        self.assertIn("BATCH_OUTPUT_ALIASED ", se)
        self.assertFalse(self.claim_path.exists(), "the claim was released")

    def test_r14_adv_a1_a_temp_inside_the_run_dir_gets_no_redirect_there(self) -> None:
        p, spec, sha = self._spawn_open_wait("exit1", env={"TEMP": str(self.run_dir), "TMP": str(self.run_dir)})
        before = sha(spec.read_bytes()).hexdigest()
        (self.run_dir / "batch.log").unlink(missing_ok=True)
        os.link(spec, self.run_dir / "batch.log")
        so, se = self._finish(p)
        self.assertEqual(sha(spec.read_bytes()).hexdigest(), before)
        self.assertEqual(p.returncode, 9, so + se)
        self.assertEqual([d.name for d in self.run_dir.iterdir() if d.name.startswith("venue-lease-batch-close-")], [], "the redirect was made inside -RunDir")
        self.assertIn("BATCH_OUTPUT_REDIRECT_FAILED ", se)
        self.assertIn("LEASE_BATCH_RESULT=", so)
        self.assertFalse(self.claim_path.exists(), "the claim was released")

    def test_r14_adv_a2_a_redirect_that_cannot_be_made_still_releases_the_claim_and_reports_a_typed_exit(self) -> None:
        blocker = self.case / "r14-not-a-dir"
        blocker.unlink(missing_ok=True)
        blocker.write_text("a file, so no folder can be made below it", encoding="utf-8")
        bad = blocker / "temp"
        p, spec, sha = self._spawn_open_wait("exit1", env={"TEMP": str(bad), "TMP": str(bad)})
        before = sha(spec.read_bytes()).hexdigest()
        (self.run_dir / "batch.log").unlink(missing_ok=True)
        os.link(spec, self.run_dir / "batch.log")
        so, se = self._finish(p)
        self.assertEqual(sha(spec.read_bytes()).hexdigest(), before)
        self.assertEqual(p.returncode, 9, so + se)
        self.assertEqual(batch_result(so)["stopReason"], "OPEN_FAILED")
        self.assertIn("BATCH_OUTPUT_REDIRECT_FAILED ", se)
        self.assertFalse(self.claim_path.exists(), "the claim was released")

    def test_r14_adv2_a2_a_temp_junction_into_the_run_dir_gets_no_redirect_there(self) -> None:
        alias = self.case / "r14-temp-junction"
        if alias.exists():
            os.rmdir(alias)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._junction(alias, self.run_dir)
        p, spec, sha = self._spawn_open_wait("exit1", env={"TEMP": str(alias), "TMP": str(alias)})
        before = sha(spec.read_bytes()).hexdigest()
        (self.run_dir / "batch.log").unlink(missing_ok=True)
        os.link(spec, self.run_dir / "batch.log")
        so, se = self._finish(p)
        self.assertEqual(sha(spec.read_bytes()).hexdigest(), before)
        self.assertEqual(p.returncode, 9, so + se)
        self.assertEqual([d.name for d in self.run_dir.iterdir() if d.name.startswith("venue-lease-batch-close-")], [], "the redirect was made inside -RunDir through the junction")
        self.assertIn("BATCH_OUTPUT_REDIRECT_FAILED ", se)

    def test_r14_adv2_a1_an_alias_found_in_the_gate_wait_still_refuses_after_the_link_is_removed(self) -> None:
        import hashlib
        self._fresh_run_dir()
        (self.share / "fake-agent-state.json").unlink(missing_ok=True)
        spec = self.case / "safe" / "fx-a.json"
        spec.parent.mkdir(parents=True, exist_ok=True)
        spec.unlink(missing_ok=True)
        shutil.copy2(self.specs[0], spec)
        before = hashlib.sha256(spec.read_bytes()).hexdigest()
        args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]), FAKE_OPEN_SLEEP="12", FAKE_OPEN_MODES="gatehold")
        err = self.case / "r14-adv2-a1.err.txt"
        with open(err, "w", encoding="utf-8") as ef:
            p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=ef, text=True, env=e)
            try:
                self._wait_for_log("BATCH_OPEN_LIVENESS_READBACK")
                self._wait_for_open_wait()   # r15: the open's wait itself, never a fixed sleep after the read-back line
                (self.run_dir / "batch.log").unlink(missing_ok=True)
                os.link(spec, self.run_dir / "batch.log")
                end = time.monotonic() + 90
                while "BATCH_OUTPUT_ALIASED " not in err.read_text(encoding="utf-8") and time.monotonic() < end:
                    time.sleep(0.2)
                (self.run_dir / "batch.log").unlink()   # the link is gone before the open's result lands by name
                [job] = [j.name[: -len(".job.ps1")] for j in (self.share / "inbox").iterdir() if j.name.startswith("lease-batch-open-")]
                token, now = job[-12:], datetime.now(timezone.utc)
                adm = {"gate": "fake", "mode": "lease", "label": "clear", "heavySlotHeld": "1:1001", "slot": "C:\\t\\.heavy-slots\\1", "leaseMinutes": 45, "waitedSec": 7,
                       "leaseRequests": 2, "leaseRefusals": [], "leaseBatch": {"token": token, "leaseId": "lease-1", "seq": 1, "reused": False,
                                                                                "untilEpoch": int(now.timestamp()) + 2700, "remainingSec": 2700}}
                (self.share / "outbox").mkdir(parents=True, exist_ok=True)
                (self.share / "outbox" / f"{job}.result.json").write_text(json.dumps({"jobId": job, "exitCode": 0, "endedUtc": utc_iso(now), "admission": adm, "stdout": "LEASE_BATCH_OPEN"}), encoding="utf-8")
                so, _ = p.communicate(timeout=300)
            finally:
                if p.poll() is None:
                    subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, text=True, timeout=60)
                    p.wait(timeout=60)
        se = err.read_text(encoding="utf-8")
        self.assertEqual(hashlib.sha256(spec.read_bytes()).hexdigest(), before)
        self.assertEqual(self.leg_calls(), [], "a leg ran after the alias was found: " + so + se)
        self.assertEqual([k for k in self.kinds() if not k.startswith(("OPEN", "CLOSE"))], [], "no probe ran")
        self.assertEqual(p.returncode, 2, so + se)
        self.assertIn("LEG_PLAN_REFUSED reason=LEG_SPEC_IS_RUN_OUTPUT item=1 ", se)

    def test_r14_adv_a3_the_claim_keeper_stops_writing_a_claim_file_that_became_the_input_once_the_alias_is_found(self) -> None:
        claim = self.run_dir / "bachelor.claim.json"
        p, spec, sha = self._spawn_open_wait("gatehold", "-ClaimTtlSec", "60", "-ClaimRenewSec", "1", "-CancelReconcileSec", "3",
                                             "-DeadlineUtc", utc_iso(datetime.now(timezone.utc) + timedelta(seconds=45)), claim_dir=self.run_dir)
        nonce = json.loads(claim.read_text(encoding="utf-8"))["nonce"]
        body = json.loads(spec.read_text(encoding="utf-8"))
        body.update({"nonce": nonce, "expiresUtc": "2026-10-26T12:00:00Z"})
        spec.write_text(json.dumps(body, separators=(",", ":")), encoding="utf-8")
        claim.unlink()
        os.link(spec, claim)
        so, se = self._finish(p)
        self.assertIn("BATCH_OUTPUT_ALIASED ", se, so + se)
        cancel = self.share / "cancel" / f"lease-batch-open-{[j.name for j in (self.share / 'inbox').iterdir() if j.name.startswith('lease-batch-open-')][0][len('lease-batch-open-'):-len('.job.ps1')]}.cancel"
        self.assertTrue(cancel.exists(), so + se)
        self.assertIn("close=RECEIPT", so + se, "the lease close was still submitted")
        # the keeper renews every second; stopped at the alias, its last write is long before the cancel the finally requests at the deadline
        self.assertLess(spec.stat().st_mtime, cancel.stat().st_mtime - 5, "the keeper went on rewriting the input after the alias was found")

    # r16 (sol r15 B2 = fable r15 H2 = adversary r14 A2): the redirect's path was built with Join-Path BEFORE its try, so TEMP on a drive that does not exist threw into the batch
    # catch: BATCH_ERROR (exit 12) replaced the open's typed stop, and the close job was still written into <RunDir>\batch-jobs. fable r15 H1: the containment test compared spellings
    # and final paths only, so a loopback UNC TEMP (\\localhost\C$\<RunDir>) put the redirect inside -RunDir.
    def _assert_redirect_refused_and_nothing_more_in_the_run_dir(self, temp: str) -> None:
        p, spec, sha = self._spawn_open_wait("exit1", env={"TEMP": temp, "TMP": temp})
        before = sha(spec.read_bytes()).hexdigest()
        jobs_before = sorted(j.name for j in (self.run_dir / "batch-jobs").iterdir())
        (self.run_dir / "batch.log").unlink(missing_ok=True)
        os.link(spec, self.run_dir / "batch.log")
        so, se = self._finish(p)
        self.assertEqual(sha(spec.read_bytes()).hexdigest(), before, "the accepted input is byte-unchanged")
        self.assertEqual(p.returncode, 9, so + se)
        self.assertEqual([d.name for d in self.run_dir.iterdir() if d.name.startswith("venue-lease-batch-close-")], [], "the redirect was made inside -RunDir")
        self.assertEqual(sorted(j.name for j in (self.run_dir / "batch-jobs").iterdir()), jobs_before, "a job file was written into -RunDir after the alias was found")
        self.assertIn("BATCH_OUTPUT_REDIRECT_FAILED ", se)
        res = batch_result(so)
        self.assertEqual(res["stopReason"], "OPEN_FAILED", "the open's typed stop is kept")
        self.assertEqual(res["outputRedirect"]["state"], "FAILED", res)
        self.assertTrue(res["outputRedirect"]["error"], res)
        self.assertFalse(self.claim_path.exists(), "the claim was released")

    # r16 (adversary r16 A1 on fd258854): the staged manifest's digest and content refusals ran BEFORE its location guard, so a manifest that IS <RunDir>\batch.log (a hard link) was
    # appended to by the BUILD_MANIFEST_MISMATCH refusal's own log line: the refused input changed. The guard now comes first, and no refusal appends through a linked batch.log.
    def test_r16_adv_a1_a_staged_manifest_that_is_batch_log_is_never_appended_to_by_its_own_refusal(self) -> None:
        import hashlib
        c = "7" * 40
        cases = {
            "a wrong (well-formed) digest": ({}, "0" * 64),
            "a content problem": ({"pendingSymbolPresence": "false"}, None),
        }
        for label, (override, digest) in cases.items():
            with self.subTest(case=label):
                staged = self.stage(c, "linked build", **override)
                manifest = self.share / "cache" / f"playback-attr-3-cuda-{c[:12]}-build.json"
                self._fresh_run_dir()
                os.link(manifest, self.run_dir / "batch.log")
                before = hashlib.sha256(manifest.read_bytes()).hexdigest()
                r = self.run_plan(self.write_plan([self.item(self.specs[0], "A", c, manifest=digest or staged)]))
                self.assertEqual(hashlib.sha256(manifest.read_bytes()).hexdigest(), before, "the refused staged manifest was appended to through batch.log: " + r.stderr)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn("LEG_PLAN_REFUSED reason=BUILD_MANIFEST_IS_RUN_OUTPUT item=1 ", r.stderr)
                self.assertEqual(self.kinds(), [], "nothing is submitted")

    def test_artifact_preflight_r14_adv_p2_a1_a_manifest_that_links_to_batch_log_is_not_appended_to_when_its_artifacts_cannot_be_hashed(self) -> None:
        # #705 r14 adversary pass 2 A1: the cached build.json is a symbolic link to a plain one-name batch.log, and its artifacts cannot be hashed (the read throws, as a package zip
        # held with FileShare.None does). On 31ba19ee the preflight refused SHARE_UNREACHABLE BEFORE the manifest's run-output guard, and that refusal's log line was appended to
        # batch.log, which is the manifest. The guard now comes first, as it did before the artifact hash existed. The hard-link case (two names, so never appended to) shows the same
        # order on any host: it was refused SHARE_UNREACHABLE, never named as the run output it is; the symbolic-link case needs the privilege to make one (hosted CI has it).
        import hashlib
        c = "7" * 40
        probe = self.case / "artifact-probe-throws.ps1"
        probe.write_text("param($items)\nthrow 'the package zip is held with FileShare.None'\n", encoding="utf-8")
        for how in ("hard link", "symbolic link"):
            with self.subTest(how=how):
                staged = self.stage(c, "linked build")
                manifest = self.share / "cache" / f"playback-attr-3-cuda-{c[:12]}-build.json"
                self._fresh_run_dir()
                log = self.run_dir / "batch.log"
                if how == "hard link":
                    os.link(manifest, log)
                else:
                    log.write_bytes(manifest.read_bytes())
                    manifest.unlink()
                    try:
                        os.symlink(log, manifest)
                    except OSError as e:
                        self.skipTest(f"file symlinks need the privilege here: {e}")
                before = hashlib.sha256(log.read_bytes()).hexdigest()
                r = self.run_plan(self.write_plan([self.item(self.specs[0], "A", c, manifest=staged)]), "-ArtifactProbeScript", str(probe))
                self.assertEqual(hashlib.sha256(log.read_bytes()).hexdigest(), before, "the staged manifest was appended to through batch.log: " + r.stderr)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn("LEG_PLAN_REFUSED reason=BUILD_MANIFEST_IS_RUN_OUTPUT item=1 ", r.stderr)
                self.assertEqual(self.kinds(), [], "nothing is submitted")

    def test_r16_b2_a_temp_on_a_drive_that_does_not_exist_keeps_the_open_s_stop_and_writes_no_job_into_the_run_dir(self) -> None:
        free = [c for c in "ZYXWVUTSRQPONMLKJIHGFED" if not os.path.exists(f"{c}:\\")]
        if not free:
            self.skipTest("every drive letter is in use")
        self._assert_redirect_refused_and_nothing_more_in_the_run_dir(f"{free[0]}:\\missing")

    def test_r16_h1_a_loopback_unc_temp_that_is_the_run_dir_gets_no_redirect_there(self) -> None:
        self._fresh_run_dir()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._assert_redirect_refused_and_nothing_more_in_the_run_dir(str(self._loopback_unc(self.run_dir)))

    # r12 (adversary r12 A2): TrimEnd('\') turned the drive root C:\ into C:, the drive's current directory, so a root zone was listed from the wrong folder. A real volume root cannot be
    # written to by a test, so the function that decides it is run on its own: its source is taken from the runner, so this cannot drift from what the runner does.
    def test_r12_a2_a_drive_root_keeps_its_backslash_when_trailing_separators_are_removed(self) -> None:
        text = RUNNER.read_text(encoding="utf-8")
        start = text.index("function Remove-TrailingSeparator")
        end = text.index("\nfunction ", start + 1)
        script = self.case / "trim-probe.ps1"
        script.write_text(text[start:end] + "\n" + "\n".join(
            f"'{raw}' + ' => ' + (Remove-TrailingSeparator '{raw}')" for raw in ("C:\\", "C:\\\\", "d:\\", "C:\\run\\", "C:\\run", "\\\\srv\\share\\", "C:\\a\\b\\\\")) + "\n", encoding="ascii")
        r = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script)], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        got = [l for l in r.stdout.splitlines() if " => " in l]
        self.assertEqual(got, ["C:\\ => C:\\", "C:\\\\ => C:\\", "d:\\ => d:\\", "C:\\run\\ => C:\\run", "C:\\run => C:\\run", "\\\\srv\\share\\ => \\\\srv\\share", "C:\\a\\b\\\\ => C:\\a\\b"])

    def test_r12_a2_no_zone_root_or_parent_is_trimmed_with_a_bare_trimend(self) -> None:
        text = RUNNER.read_text(encoding="utf-8")
        start = text.index("function Get-OutputZones")
        end = text.index("function Get-OutputNameRefusal")
        self.assertNotIn(".TrimEnd('\\')", text[start:end].replace(".root.TrimEnd('\\')", "").replace("$parent.TrimEnd('\\')", "").replace("$l.physical.TrimEnd('\\')", ""))

    def test_r5_adv_a1_a_throw_after_a_leg_ran_does_not_record_that_leg_as_not_run(self) -> None:
        # adversary r5 A1: the new batch catch left the leg whose child had completed (its output file could not be written) in pending, and the summary then said
        # NOT_RUN for a leg that ran. The leg is recorded LEG_FAILED / reason BATCH_ERROR (its result is unknown); only the legs never started are NOT_RUN.
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "leg-01-fx-a-cpu-A-b01-a1.out.txt").mkdir()
        a = self.commit_a
        r = self.run_plan(self.write_plan([self.item(self.specs[0], "A", a), self.item(self.specs[1], "A", a)]))
        self.assertEqual(r.returncode, 12, r.stdout + r.stderr)
        self.assertEqual(len(self.leg_calls()), 1, "the first leg's child ran")
        res = batch_result(r.stdout)
        self.assertEqual([(l["name"], l["status"]) for l in res["legs"]], [("01-fx-a-cpu-A", "LEG_FAILED"), ("02-fx-b-cpu-A", "NOT_RUN")], res["legs"])
        self.assertEqual((res["ran"], res["failed"], res["notRun"], res["stopReason"]), (0, 1, 1, "BATCH_ERROR"))
        self.assertEqual(res["legs"][0]["reason"], "BATCH_ERROR")
        [b] = self.batches()
        self.assertEqual([(l["name"], l["status"]) for l in b["legs"]], [("01-fx-a-cpu-A", "LEG_FAILED")])
        self.assertFalse(self.claim_path.exists(), "the claim was left behind")

    def test_r5_adv_a2_a_staged_build_the_leg_job_would_reject_is_refused_before_the_claim_and_the_lease(self) -> None:
        # adversary r5 A2: preflight read the staged manifest's sha256 only. The leg job's validator (Assert-AttrCudaBuildManifest) also needs the manifest to declare
        # the same sourceCommit, a well-formed dllPairManifestSha256 and a boolean pendingSymbolPresence, so a hash-matching manifest that fails any of them opened a lease first.
        a, c = self.commit_a, "3" * 40
        cases = {
            "declares another commit": {"sourceCommit": "4" * 40},
            "no sourceCommit": {"sourceCommit": ABSENT},
            "no dllPairManifestSha256": {"dllPairManifestSha256": ABSENT},
            "dllPairManifestSha256 malformed": {"dllPairManifestSha256": "xyz"},
            "no pendingSymbolPresence": {"pendingSymbolPresence": ABSENT},
            "pendingSymbolPresence is a string": {"pendingSymbolPresence": "false"},
        }
        for label, override in cases.items():
            with self.subTest(case=label):
                manifest = self.stage(c, "bad build", **override)
                self.assert_whole_plan_refused(self.write_plan([self.item(self.specs[0], "A", a), self.item(self.specs[1], "B", c, manifest=manifest)]), "BUILD_MANIFEST_MISMATCH item=2 ")

    def test_r5_adv_a2_a_staged_build_that_is_not_json_is_refused(self) -> None:
        c = "3" * 40
        cache = self.share / "cache"
        p = cache / f"playback-attr-3-cuda-{c[:12]}-build.json"
        p.write_text("this is not json", encoding="utf-8")
        import hashlib
        manifest = hashlib.sha256(p.read_bytes()).hexdigest()
        self.assert_whole_plan_refused(self.write_plan([self.item(self.specs[0], "A", self.commit_a), self.item(self.specs[1], "B", c, manifest=manifest)]), "BUILD_MANIFEST_MISMATCH item=2 ")

    def test_r5_adv2_a2_a_leg_is_marked_in_flight_at_the_launch_not_before_its_run_line_is_logged(self) -> None:
        # adversary r5 pass 2 A2: the marker was set before the "LEG .. RUN" log line, so a throw while that line was appended recorded a leg whose child never started as
        # LEG_FAILED. It is set on the line before the child is launched, after everything that can throw without having started it.
        text = RUNNER.read_text(encoding="utf-8")
        run_line, launch = text.index('L "LEG $($leg.name) RUN batch='), text.index("$lp = Invoke-VenueBoundedProcess -FilePath $pwshExe")
        marks = [i for i in range(len(text)) if text.startswith("$inFlightLeg = $leg", i)]
        self.assertEqual(len(marks), 1, marks)
        self.assertTrue(run_line < marks[0] < launch, (run_line, marks, launch))
        self.assertEqual(text[marks[0]:launch].count("\n"), 1, "nothing between the marker and the launch but the launch's own line")

    def test_r5_adv2_a3_the_staged_manifest_digest_and_facts_come_from_one_read(self) -> None:
        # adversary r5 pass 2 A3: Get-FileHash and a second read of the same file could pair one version's digest with another's content.
        text = MODULE.read_text(encoding="utf-8")
        body = text[text.index("function Get-VenueStagedBuildManifest"):text.index("$script:VenueAbandonedReads = 0")]
        self.assertEqual(body.count("ReadAllBytes"), 1, body)
        self.assertNotIn("Get-FileHash", body)
        self.assertNotIn("ReadAllText", body)

    def test_r5_adv2_a3_a_staged_manifest_with_a_byte_order_mark_is_still_read(self) -> None:
        c = "5" * 40
        p = self.share / "cache" / f"playback-attr-3-cuda-{c[:12]}-build.json"
        p.write_bytes(b"\xef\xbb\xbf" + json.dumps({"sourceCommit": c, "dllPairManifestSha256": "f" * 64, "pendingSymbolPresence": False, **ARTIFACT_SHAS}).encode("utf-8"))
        script = (f"Import-Module '{psq(MODULE)}' -Force -DisableNameChecking\n"
                  f"$r = Get-VenueStagedBuildManifest -AgentShare '{psq(self.share)}' -SourceCommit '{c}' -TimeoutSec 30\n"
                  "ConvertTo-Json -Compress -InputObject @{ state = $r.state; sha = $r.sha256; problem = $r.contentProblem }\n")
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        import hashlib
        self.assertEqual(json.loads(out.stdout), {"state": "STAGED", "sha": hashlib.sha256(p.read_bytes()).hexdigest(), "problem": ""})

    def test_r6_adv_a1_preflight_decodes_the_staged_manifest_exactly_as_the_leg_jobs_validator_does(self) -> None:
        # adversary r6 A1: preflight stripped every leading U+FEFF from a UTF-8 decode and never honoured a UTF-16 BOM, while Assert-AttrCudaBuildManifest reads with
        # [IO.File]::ReadAllText (one BOM, UTF-16/32 by BOM). A doubled BOM was accepted here and rejected by the leg job after the lease opened; a UTF-16 manifest the
        # job accepts was refused here. The oracle below is that very read; preflight must agree with it on every input.
        c = "6" * 40
        body = json.dumps({"sourceCommit": c, "dllPairManifestSha256": "f" * 64, "pendingSymbolPresence": False, **ARTIFACT_SHAS})
        cases = {
            "no BOM": (body.encode("utf-8"), ""),
            "one UTF-8 BOM": (b"\xef\xbb\xbf" + body.encode("utf-8"), ""),
            "two UTF-8 BOMs": (b"\xef\xbb\xbf\xef\xbb\xbf" + body.encode("utf-8"), "ATTRCUDA_BUILD_MANIFEST_UNPARSEABLE"),
            "UTF-16LE with a BOM": (b"\xff\xfe" + body.encode("utf-16-le"), ""),
            "UTF-16BE with a BOM": (b"\xfe\xff" + body.encode("utf-16-be"), ""),
        }
        p = self.share / "cache" / f"playback-attr-3-cuda-{c[:12]}-build.json"
        for label, (data, expected) in cases.items():
            with self.subTest(case=label):
                p.write_bytes(data)
                script = (f"Import-Module '{psq(MODULE)}' -Force -DisableNameChecking\n"
                          f"$r = Get-VenueStagedBuildManifest -AgentShare '{psq(self.share)}' -SourceCommit '{c}' -TimeoutSec 30\n"
                          f"$oracle = try {{ $null -ne ([IO.File]::ReadAllText('{psq(p)}') | ConvertFrom-Json) }} catch {{ $false }}\n"
                          "ConvertTo-Json -Compress -InputObject @{ state = $r.state; problem = [string]$r.contentProblem; validatorParses = $oracle }\n")
                out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
                self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
                got = json.loads(out.stdout)
                self.assertEqual(got["state"], "STAGED")
                self.assertEqual(got["validatorParses"], expected == "", "the oracle (the validator's own read) disagrees with the fixture's expectation")
                self.assertTrue(got["problem"].startswith(expected), got)
                self.assertEqual(got["problem"] == "", got["validatorParses"], "preflight and the validator disagree")

    def test_r5_h2_the_interleave_helper_keeps_an_array_valued_item_whole(self) -> None:
        # key3 H2 (sol): the bare Dequeue() output was enumerated, so one array-valued item came back as its elements.
        script = (
            f"Import-Module '{psq(MODULE)}' -Force -DisableNameChecking\n"
            "$items = [object[]]::new(2); $items[0] = @([pscustomobject]@{ arm = 'A' }, [pscustomobject]@{ arm = 'B' }); $items[1] = [pscustomobject]@{ arm = 'A' }\n"
            "$one = [object[]]::new(1); $one[0] = $items[0]\n"
            "[ordered]@{ one = @(Get-VenueInterleavedLegPlan -Items $one).Count; two = @(Get-VenueInterleavedLegPlan -Items $items).Count } | ConvertTo-Json -Compress\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(json.loads(out.stdout), {"one": 1, "two": 2})

    def test_the_plan_and_the_scalar_form_are_mutually_exclusive(self) -> None:
        args, e = self.plan_cmd(self.ab_block_plan(), "-SourceCommit", SHA)
        r = subprocess.run(args, capture_output=True, text=True, timeout=120, env=e)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.kinds(), [])

    def test_the_interleave_helper(self) -> None:
        script = (
            f"Import-Module '{psq(MODULE)}' -Force -DisableNameChecking\n"
            "function P([string]$arms) { @($arms.ToCharArray() | ForEach-Object -Begin { $i = 0 } -Process { $i++; [pscustomobject]@{ arm = [string]$_; n = $i } }) }\n"
            "function S($items) { (@(Get-VenueInterleavedLegPlan -Items $items) | ForEach-Object { \"$($_.arm)$($_.n)\" }) -join ' ' }\n"
            "[ordered]@{ block = S (P 'AABB'); uneven = S (P 'AAAB'); three = S (P 'AABBCC'); one = S (P 'AAA'); mixed = S (P 'ABBA') } | ConvertTo-Json -Compress\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(json.loads(out.stdout), {"block": "A1 B3 A2 B4", "uneven": "A1 B4 A2 A3", "three": "A1 B3 C5 A2 B4 C6", "one": "A1 A2 A3", "mixed": "A1 B2 A4 B3"})


# the runner cases are inherited only for their fixtures: they run once, in their own class (unittest collects callable test_* attributes only)
for _name in [n for n in vars(VenueLeaseBatchRunnerTests) if n.startswith("test_")]:
    setattr(VenueLeaseBatchLegPlanTests, _name, None)
del _name


# CI-WINDOWS-UNITTEST-HEAVY-PARTS-3-1: ci_unittest_shard places whole classes, and VenueLeaseBatchLegPlanTests measured 1039-1121 s on hosted windows (master
# runs 40858861385, 40864893269, 40871917397, 40878296653), past the 1074 s one windows shard or heavy part holds under ShardDurationBudgetTests. Its test_r1_* ..
# test_r8_* cases move, unchanged, into a sibling class on the same fixtures (the split #705 r13 staged), so no class is too heavy to place. Each case runs once.
# LEASE-BATCH-PLAN-ARTIFACT-PREFLIGHT-1 r14: the 69 test_artifact_preflight_* cases measured 545.9 s on hosted windows (run 40861273862), which with the 79 cases
# left in VenueLeaseBatchLegPlanTests is 1131 s, past the same 1074 s, so they move, unchanged, into a third sibling class on the same fixtures.
@requires_windows_pwsh
class VenueLeaseBatchArtifactPreflightTests(VenueLeaseBatchLegPlanTests):
    """The LegPlan cases named test_artifact_preflight_* (LEASE-BATCH-PLAN-ARTIFACT-PREFLIGHT-1)."""


@requires_windows_pwsh
class VenueLeaseBatchLegPlanEarlyRoundTests(VenueLeaseBatchLegPlanTests):
    """The LegPlan cases named test_r1_* through test_r8_* (letter suffixes included)."""


def _leg_plan_home(name: str) -> type:
    if name.startswith("test_artifact_preflight_"):
        return VenueLeaseBatchArtifactPreflightTests
    lettered = re.match(r"test_r(\d+)", name)
    if lettered and int(lettered.group(1)) <= 8:
        return VenueLeaseBatchLegPlanEarlyRoundTests
    return VenueLeaseBatchLegPlanTests


for _name, _case in [(n, f) for n, f in vars(VenueLeaseBatchLegPlanTests).items() if n.startswith("test_") and callable(f)]:
    for _cls in (VenueLeaseBatchLegPlanTests, VenueLeaseBatchArtifactPreflightTests, VenueLeaseBatchLegPlanEarlyRoundTests):
        setattr(_cls, _name, _case if _cls is _leg_plan_home(_name) else None)
del _name, _case, _cls


# 2026-10-25 LEASE-BATCH-LEASE-END-TZ-1: the agent's result reaches the runner through ConvertFrom-Json, which on PowerShell 7 turns "endedUtc": "...Z" into a
# [DateTime] of Kind Utc. Get-LeaseEndUtc used to run it through [string] + DateTimeOffset.Parse, which drops the Kind and assumes the HOST's local offset, so on a
# Central host every lease end landed 5 h early (roomSec=-15301) and no leg ever started. CI hosts are UTC (offset 0), where the old code passes, so the behavioural
# cases below only go red on a non-UTC host; the source case is the one that guards on UTC.
@requires_windows_pwsh
class VenueLeaseBatchReceiptRootTests(unittest.TestCase):
    """LEASE-BATCH-RECEIPT-ROOT-1: -ReceiptRoot reaches every Invoke-VenueLeg call, an empty one forwards nothing, and a root outside .claude-state is refused before any lease.
    Same fakes and harness as VenueLeaseBatchRunnerTests (borrowed, not inherited, so its cases are not run a second time); nothing reaches a venue."""

    setUpClass = VenueLeaseBatchRunnerTests.__dict__["setUpClass"]
    tearDownClass = VenueLeaseBatchRunnerTests.__dict__["tearDownClass"]
    setUp = VenueLeaseBatchRunnerTests.setUp
    _init_case = VenueLeaseBatchRunnerTests._init_case
    run_batch = VenueLeaseBatchRunnerTests.run_batch
    batch_cmd = VenueLeaseBatchRunnerTests.batch_cmd
    kinds = VenueLeaseBatchRunnerTests.kinds
    batches = VenueLeaseBatchRunnerTests.batches

    def scratch_root(self) -> Path:
        # a root the way a calibration round gives one: <scratch>\.claude-state\<dual-venue dir>\<receipts dir> (the evidence directory lands beside it, also under .claude-state;
        # the spelling avoids the literal path the receipt-reader inventory in test_dual_venue_evidence.py scans for: this file reads no real receipt, only the fake leg's)
        return self.case / "scratch" / ".claude-state" / "dual-venue" / "receipts"

    def clean_base(self) -> Path:
        """A fresh directory whose path has no .claude-state segment (the temp dir may have one: a lane runs with TEMP under its worktree's .claude-state)."""
        for parent in (Path(tempfile.gettempdir()), ROOT.parent, Path(os.environ.get("SystemDrive", "C:") + "\\")):
            if ".claude-state" in parent.resolve().parts:
                continue
            try:
                d = Path(tempfile.mkdtemp(prefix="venue-rr-clean-", dir=parent))
            except OSError:
                continue
            self.addCleanup(shutil.rmtree, d, True)
            return d
        self.skipTest("no writable directory without a .claude-state segment")

    def long_form(self, p: Path) -> Path:
        """The fully resolved long form of a path under the case dir. The script resolves -ReceiptRoot through [IO.Path]::GetFullPath, which on Windows expands 8.3 short names
        (a hosted runner's temp dir is C:/Users/RUNNER~1/..., the script forwards C:/Users/runneradmin/...): expect the long form, as the script and the real leg see it."""
        return Path(os.path.realpath(self.case)) / p.relative_to(self.case)

    def default_root(self) -> Path:
        return self.case / "receipts"   # the fake leg's default root (the real leg's is under the main checkout's .claude-state)

    def leg_calls(self) -> list[str]:
        f = self.case / "leg-receiptroot.log"
        return f.read_text(encoding="utf-8").splitlines() if f.exists() else []

    @staticmethod
    def files_under(root: Path) -> list[Path]:
        return sorted(p for p in root.rglob("*.json")) if root.exists() else []

    def assert_nothing_started(self, r: subprocess.CompletedProcess) -> None:
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(self.events_text(), "", "a refused root must reach no um-run, quiet probe or leg")
        self.assertEqual(self.leg_calls(), [])
        self.assertFalse(self.claim_path.exists(), "a refused root took the venue claim")
        self.assertFalse(self.run_dir.exists(), "a refused root created the run dir")

    def events_text(self) -> str:
        return self.events.read_text(encoding="utf-8") if self.events.exists() else ""

    # (a) RED on the old script: it has no -ReceiptRoot, so the leg wrote into the default root
    def test_a_receipt_root_sends_every_leg_receipt_to_it_and_none_to_the_default_root(self) -> None:
        root = self.scratch_root()
        r = self.run_batch(self.specs, "-ReceiptRoot", str(root))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.files_under(self.default_root()), [], "a receipt landed in the default root")
        self.assertFalse(self.default_root().exists(), "the default root was created")
        landed = self.files_under(root)
        self.assertEqual(len(landed), 3, landed)
        self.assertEqual(self.leg_calls(), [f"bound=True root={self.long_form(root)}"] * 3)
        [b] = self.batches()
        self.assertTrue(all(Path(os.path.realpath(l["receipt"])).parent.parent.parent.parent == self.long_form(root) for l in b["legs"]), [l["receipt"] for l in b["legs"]])
        # the read-back: the pre-load mean is found in the evidence directory beside THIS root (a default-root read would find none and leave the leg uncounted)
        self.assertEqual([l["class"] for l in b["legs"]], ["QUIET_PRELOAD_OK"] * 3, b["legs"])
        self.assertEqual((b["legsRan"], b["legsCounted"]), (3, 3))
        self.assertIn(f" receiptRoot={self.long_form(root)}", (self.run_dir / "batch.log").read_text(encoding="utf-8"))

    def test_a_receipt_root_is_forwarded_to_the_in_lease_retry_too(self) -> None:
        root = self.scratch_root()
        r = self.run_batch([self.specs[0]], "-ReceiptRoot", str(root), FAKE_LEG_OUTCOMES="VENUE_NOT_QUIESCENT,PASS")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual([l["status"] for l in b["legs"]], ["RETRY", "RAN"])
        self.assertEqual(self.leg_calls(), [f"bound=True root={self.long_form(root)}"] * 2)
        self.assertEqual(len(self.files_under(root)), 2)
        self.assertFalse(self.default_root().exists())

    def test_a_relative_receipt_root_is_forwarded_as_a_full_path(self) -> None:
        args, e = self.batch_cmd(None, "-ReceiptRoot", os.path.join("rel", ".claude-state", "dual-venue", "receipts"))
        r = subprocess.run(args, capture_output=True, text=True, timeout=600, env=e, cwd=str(self.case))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        want = self.long_form(self.case / "rel" / ".claude-state" / "dual-venue" / "receipts")
        self.assertEqual(self.leg_calls(), [f"bound=True root={want}"] * 2)

    def test_adversary_a1_a_relative_receipt_root_resolves_against_the_powershell_location_like_rundir_does(self) -> None:
        # adversary f1c42e80 A1: the process directory (what [IO.Path]::GetFullPath uses) and the PowerShell location (what Resolve-Path -RunDir uses) differ once a caller has
        # done Set-Location. The script is run in-process from a session whose location is cwd-b while the process directory is cwd-a: the root belongs under cwd-b.
        proc_dir, ps_dir = self.case / "cwd-a", self.case / "cwd-b"
        proc_dir.mkdir()
        ps_dir.mkdir()
        rel = os.path.join("rel", ".claude-state", "dual-venue", "receipts")
        args, e = self.batch_cmd(None, "-ReceiptRoot", rel)
        i = args.index("-File")
        runner, rest = args[i + 1], args[i + 2:]
        call = " ".join(x if re.fullmatch(r"-[A-Za-z][A-Za-z0-9]*", x) else f"'{psq(x)}'" for x in rest)
        command = f"Set-Location -LiteralPath '{psq(ps_dir)}'; & '{psq(runner)}' {call}; exit $LASTEXITCODE"
        r = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True, text=True, timeout=600, env=e, cwd=str(proc_dir))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        want = ps_dir / "rel" / ".claude-state" / "dual-venue" / "receipts"
        self.assertEqual(self.leg_calls(), [f"bound=True root={self.long_form(want)}"] * 2, r.stdout + r.stderr)
        self.assertEqual(len(self.files_under(want)), 2)
        self.assertFalse((proc_dir / "rel").exists(), "the root was resolved against the process directory")

    # (b) RED on the old script for the explicit empty/blank spellings (an unknown parameter); the omitted spelling is the byte-for-byte guard
    def test_an_empty_or_blank_receipt_root_forwards_no_argument(self) -> None:
        for spelling in (None, "", "   "):
            with self.subTest(spelling=spelling):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                r = self.run_batch(None, *(() if spelling is None else ("-ReceiptRoot", spelling)))
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual(self.leg_calls(), ["bound=False root="] * 2, "the leg was handed a -ReceiptRoot it should not have been")
                self.assertEqual(len(self.files_under(self.default_root())), 2)
                self.assertNotIn("receiptRoot=", (self.run_dir / "batch.log").read_text(encoding="utf-8"))

    # (c) RED on the old script: a root outside .claude-state is refused before the run dir, the claim, the lease or a leg
    def test_a_receipt_root_outside_claude_state_is_refused_before_any_lease_or_leg(self) -> None:
        # the case dirs sit in the temp dir, which on a lane (TEMP under the worktree's .claude-state) is itself under .claude-state: the refused roots live in a base with no such segment
        base = self.clean_base()
        cases = {
            "outside": base / "plain" / "dual-venue" / "receipts",
            # a root that IS .claude-state: the root passes, the evidence directory beside it (<parent>\evidence) does not
            "evidence-beside-it-outside": base / "scratch" / ".claude-state",
            # the segment must be a whole directory name, not a substring
            "substring-only": base / "x.claude-state" / "receipts",
        }
        for name, root in cases.items():
            with self.subTest(name=name):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                r = self.run_batch(None, "-ReceiptRoot", str(root))
                self.assert_nothing_started(r)
                self.assertIn("RECEIPT_ROOT_MUST_STAY_LOCAL", r.stderr)

    # Dot A1 (PR #729 comment 6540489428): a path component was compared with -ceq, which is culture-aware (InvariantCulture) and skips ignorable code points, so a directory
    # named ".claude-state<U+00AD>" passed as the gitignored ".claude-state". Each spelling below is one the old comparison accepts on this host (the first assertion proves it).
    IGNORABLE = {"soft-hyphen": "\u00ad", "zero-width-joiner": "\u200d", "zero-width-space": "\u200b", "bom": "\ufeff"}

    @staticmethod
    def old_comparison_accepts(component: str) -> bool:
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", f"[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); ('{psq(component)}' -ceq '.claude-state')"],
                             capture_output=True, text=True, encoding="utf-8", timeout=120)
        return out.stdout.strip() == "True"

    def ignorable_roots(self) -> dict:
        base = self.clean_base()
        roots = {}
        for name, cp in self.IGNORABLE.items():
            roots[f"{name}-suffix"] = (f".claude-state{cp}", base / f".claude-state{cp}" / "dual-venue" / "receipts")
            roots[f"{name}-prefix"] = (f"{cp}.claude-state", base / f"{cp}.claude-state" / "dual-venue" / "receipts")
        return roots

    def test_adversary_dot_a1_an_ignorable_code_point_in_the_boundary_name_is_not_claude_state_at_the_batch_entry(self) -> None:
        for name, (component, root) in self.ignorable_roots().items():
            with self.subTest(name=name):
                self.assertTrue(self.old_comparison_accepts(component), f"{name}: the culture-aware comparison no longer accepts it on this host, so this case proves nothing")
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                r = self.run_batch(None, "-ReceiptRoot", str(root))
                self.assert_nothing_started(r)
                self.assertIn("RECEIPT_ROOT_MUST_STAY_LOCAL", r.stderr)

    def test_adversary_dot_a1_an_ignorable_code_point_in_the_boundary_name_is_refused_at_the_real_leg_entry(self) -> None:
        # the real Invoke-VenueLeg.ps1 receipt-root check (DVE_RECEIPT_ROOT_MUST_STAY_LOCAL), reached by its own text: the shared helper it calls decides
        leg = DV / "Invoke-VenueLeg.ps1"
        for name, (component, root) in self.ignorable_roots().items():
            with self.subTest(name=name):
                script = (
                    f"Import-Module '{psq(DV / 'DualVenueRunner.psm1')}' -Force -DisableNameChecking\n"
                    f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{psq(leg)}', [ref]$null, [ref]$null)\n"
                    "$if = $ast.Find({ param($x) $x -is [System.Management.Automation.Language.IfStatementAst] -and $x.Extent.Text -match 'DVE_RECEIPT_ROOT_MUST_STAY_LOCAL -ReceiptRoot' }, $true)\n"
                    "if ($null -eq $if) { throw 'the leg receipt-root guard was not found' }\n"
                    f"$ReceiptRoot = '{psq(root)}'; $OfflineTestMode = $false\n"
                    "try { . ([scriptblock]::Create($if.Extent.Text)); 'ACCEPTED' } catch { 'REFUSED ' + $_.Exception.Message }\n"
                )
                out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, encoding="utf-8", timeout=120)
                self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
                self.assertTrue(out.stdout.strip().startswith("REFUSED DVE_RECEIPT_ROOT_MUST_STAY_LOCAL"), out.stdout + out.stderr)

    def test_adversary_dot_a1_a_plain_claude_state_root_still_passes_the_shared_helper(self) -> None:
        script = (
            f"Import-Module '{psq(DV / 'DualVenueRunner.psm1')}' -Force -DisableNameChecking\n"
            "[ordered]@{ plain = (Test-DvUnderClaudeState -Path 'C:\\x\\.claude-state\\dual-venue\\receipts'); fwd = (Test-DvUnderClaudeState -Path 'C:/x/.claude-state/a'); "
            "caseDiffers = (Test-DvUnderClaudeState -Path 'C:\\x\\.Claude-State\\a'); substring = (Test-DvUnderClaudeState -Path 'C:\\x\\x.claude-state\\a') } | ConvertTo-Json -Compress\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(json.loads(out.stdout), {"plain": True, "fwd": True, "caseDiffers": False, "substring": False})

    # Dot A2 (PR #729 comment 6542192087) + adversary A1: a path component named .claude-state is satisfied INSIDE a network share. The refusal must come before any write:
    # these roots name a share that does not exist for the test, and a UNC GetFullPath does no network I/O, so a regression here would fail on a missing share, never write one.
    NETWORK_ROOTS = {
        "production-agent-share": "\\\\bachelor\\mlv-agent\\.claude-state\\receipts",
        "admin-share-of-localhost": "\\\\localhost\\c$\\mlvtmp\\.claude-state\\receipts",
        "forward-slash-unc": "//bachelor/mlv-agent/.claude-state/receipts",
        "extended-unc": "\\\\?\\UNC\\bachelor\\mlv-agent\\.claude-state\\receipts",
        "agent-share-itself": "\\\\bachelor\\mlv-agent\\receipts\\.claude-state",
    }

    def test_adversary_dot_a2_a_receipt_root_on_a_network_share_is_refused_at_the_batch_entry(self) -> None:
        for name, root in self.NETWORK_ROOTS.items():
            with self.subTest(name=name):
                self._init_case(Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp)))
                r = self.run_batch(None, "-ReceiptRoot", root)
                self.assert_nothing_started(r)
                self.assertIn("RECEIPT_ROOT_MUST_STAY_LOCAL", r.stderr)

    def test_adversary_dot_a2_a_receipt_root_on_a_network_share_is_refused_at_the_real_leg_entry(self) -> None:
        leg = DV / "Invoke-VenueLeg.ps1"
        for name, root in self.NETWORK_ROOTS.items():
            with self.subTest(name=name):
                script = (
                    f"Import-Module '{psq(DV / 'DualVenueRunner.psm1')}' -Force -DisableNameChecking\n"
                    f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{psq(leg)}', [ref]$null, [ref]$null)\n"
                    "$if = $ast.Find({ param($x) $x -is [System.Management.Automation.Language.IfStatementAst] -and $x.Extent.Text -match 'DVE_RECEIPT_ROOT_MUST_STAY_LOCAL -ReceiptRoot' }, $true)\n"
                    "if ($null -eq $if) { throw 'the leg receipt-root guard was not found' }\n"
                    f"$ReceiptRoot = '{psq(root)}'; $OfflineTestMode = $false\n"
                    "try { . ([scriptblock]::Create($if.Extent.Text)); 'ACCEPTED' } catch { 'REFUSED ' + $_.Exception.Message }\n"
                )
                out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, encoding="utf-8", timeout=120)
                self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
                self.assertTrue(out.stdout.strip().startswith("REFUSED DVE_RECEIPT_ROOT_MUST_STAY_LOCAL"), out.stdout + out.stderr)

    def test_adversary_dot_a2_the_shared_locality_helper_refuses_a_venue_share_prefix_and_keeps_local_roots(self) -> None:
        script = (
            f"Import-Module '{psq(DV / 'DualVenueRunner.psm1')}' -Force -DisableNameChecking\n"
            "$shares = @('C:\\fake\\agent-share', '\\\\bachelor\\mlv-agent')\n"
            "function L($p) { Test-DvLocalReceiptRoot -Path $p -AgentShares $shares }\n"
            "[ordered]@{\n"
            "  localPlain = (L 'C:\\x\\.claude-state\\dual-venue\\receipts'); localFwd = (L 'C:/x/.claude-state/a'); localLongPrefix = (L '\\\\?\\C:\\x\\.claude-state\\a')\n"
            "  underShare = (L 'C:\\fake\\agent-share\\.claude-state\\receipts'); underShareCase = (L 'c:\\FAKE\\Agent-Share\\.claude-state\\receipts'); underShareFwd = (L 'C:/fake/agent-share/.claude-state/r')\n"
            "  shareItself = (L 'C:\\fake\\agent-share'); siblingOfShare = (L 'C:\\fake\\agent-share2\\.claude-state\\receipts')\n"
            "  unc = (L '\\\\bachelor\\mlv-agent\\.claude-state\\r'); uncOther = (L '\\\\localhost\\c$\\x\\.claude-state\\r'); deviceNonDrive = (L '\\\\?\\GLOBALROOT\\x\\.claude-state\\r')\n"
            "  noMarker = (L 'C:\\x\\receipts'); ignorable = (L \"C:\\x\\.claude-state$([char]0xAD)\\r\")\n"
            "  configured = (Test-DvLocalReceiptRoot -Path '\\\\bachelor\\mlv-agent\\.claude-state\\r')\n"
            "} | ConvertTo-Json -Compress\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(json.loads(out.stdout), {
            "localPlain": True, "localFwd": True, "localLongPrefix": True,
            "underShare": False, "underShareCase": False, "underShareFwd": False,
            "shareItself": False, "siblingOfShare": True,
            "unc": False, "uncOther": False, "deviceNonDrive": False,
            "noMarker": False, "ignorable": False, "configured": False,
        })

    def test_adversary_dot_a2_every_write_root_guard_goes_through_the_locality_helper(self) -> None:
        # the class, not the instance: no script outside the helper itself asks Test-DvUnderClaudeState alone about a root that is written to
        offenders = []
        for p in sorted(DV.glob("*.ps1")):
            for n, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if "Test-DvUnderClaudeState" in line and not line.lstrip().startswith("#") and "Test-DvLocalReceiptRoot" not in line and "Import-Module" not in line:
                    offenders.append(f"{p.name}:{n}: {line.strip()[:140]}")
        self.assertEqual(offenders, [], "a write-root guard that tests only for the marker directory name accepts the same name inside a network share")

    def test_adversary_dot_a1_no_boundary_directory_name_is_compared_with_a_culture_aware_operator(self) -> None:
        # the class, not the instance: every PowerShell file under tools/ that tests a path component against .claude-state goes through [string]::Equals(..., Ordinal)
        offenders = []
        for p in sorted((ROOT / "tools").rglob("*.ps*1")):
            if p.suffix not in (".ps1", ".psm1"):
                continue
            for n, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if re.search(r"-[ci]?(?:eq|ne|like|notlike|match|notmatch|contains|in|notin)\s+['\"]\.claude-state['\"]|['\"]\.claude-state['\"]\s+-[ci]?(?:eq|ne|like|contains)\b", line, re.I):
                    offenders.append(f"{p.relative_to(ROOT).as_posix()}:{n}: {line.strip()[:140]}")
        self.assertEqual(offenders, [], "a path component compared with .claude-state by a culture-aware operator accepts ignorable code points (U+00AD ...)")


@requires_windows_pwsh
class LeaseEndUtcTimezoneTests(unittest.TestCase):
    ENDED = datetime(2026, 10, 25, 1, 19, 14, 720994, tzinfo=timezone.utc)   # the r7 open result's endedUtc
    LEFT = 2699                                                              # a 45 min lease less the open job's run (the r7 figure)
    WANT = LEFT - (ENDED.timestamp() % 1)                                    # until - ended, to the sub-second

    @classmethod
    def lease_end_deltas(cls) -> dict:
        """Seconds from now to the lease end Get-LeaseEndUtc returns, per input shape. The function is the runner's own text (AST), not a copy."""
        until = int(cls.ENDED.timestamp()) + cls.LEFT
        script = (
            f"Import-Module '{psq(MODULE)}' -Force -DisableNameChecking\n"
            f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{psq(RUNNER)}', [ref]$null, [ref]$null)\n"
            "$fn = $ast.Find({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq 'Get-LeaseEndUtc' }, $true)\n"
            ". ([scriptblock]::Create($fn.Extent.Text))\n"
            f"$until = {until}\n"
            "function Delta($ended) {\n"
            "  $r = Get-LeaseEndUtc ([pscustomobject]@{ endedUtc = $ended; admission = [pscustomobject]@{ leaseBatch = [pscustomobject]@{ untilEpoch = $until } } })\n"
            "  if ($null -eq $r) { $null } else { [Math]::Round(($r - [DateTime]::UtcNow).TotalSeconds, 1) }\n"
            "}\n"
            # the way the runner receives it: the agent's result JSON through ConvertFrom-Json
            "$fromJson = ('{\"endedUtc\": \"2026-10-25T01:19:14.7209943Z\", \"admission\": {}}' | ConvertFrom-Json).endedUtc\n"
            "$utc = [DateTime]::SpecifyKind([DateTime]'2026-10-25T01:19:14.7209943', [DateTimeKind]::Utc)\n"
            "[ordered]@{\n"
            "  jsonKind = [string]$fromJson.Kind; json = Delta $fromJson\n"
            "  utcKind = Delta $utc\n"
            "  unspecifiedKind = Delta ([DateTime]::SpecifyKind($utc, [DateTimeKind]::Unspecified))\n"
            "  localKind = Delta $utc.ToLocalTime()\n"
            "  offset = Delta ([DateTimeOffset]::new($utc).ToOffset([TimeSpan]::FromHours(-5)))\n"
            "  stringZ = Delta '2026-10-25T01:19:14.7209943Z'\n"
            "  stringOffset = Delta '2026-10-25T03:19:14.7209943+02:00'\n"
            "  stringNoZone = Delta '2026-10-25T01:19:14.7209943'\n"
            "  garbage = Delta 'not a time'\n"
            "  missing = Delta $null\n"
            "} | ConvertTo-Json -Compress\n"
        )
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stdout + out.stderr
        return json.loads(out.stdout)

    def test_a_json_utc_timestamp_is_the_same_instant_on_any_host_zone(self) -> None:
        got = self.lease_end_deltas()
        self.assertEqual(got["jsonKind"], "Utc", "ConvertFrom-Json hands the runner a Kind=Utc DateTime for a 'Z' string")
        # the host's UTC offset must not enter (the bug read -15301 on a Central host); 3 s covers the fractional second and a slow host, no real offset is that small
        self.assertAlmostEqual(got["json"], self.WANT, delta=3, msg=f"roomSec on this host (UTC offset {datetime.now().astimezone().utcoffset()}): {got}")

    def test_every_input_shape_resolves_to_the_same_utc_instant(self) -> None:
        got = self.lease_end_deltas()
        for shape in ("utcKind", "unspecifiedKind", "localKind", "offset", "stringZ", "stringOffset", "stringNoZone"):
            with self.subTest(shape=shape):
                self.assertAlmostEqual(got[shape], self.WANT, delta=3, msg=f"{shape}: {got}")

    def test_an_unreadable_or_missing_endedutc_is_no_lease_end(self) -> None:
        got = self.lease_end_deltas()
        self.assertIsNone(got["garbage"])
        self.assertIsNone(got["missing"])

    # ---- host-independent guard (the one a UTC CI host can fail)
    def test_the_runner_never_parses_a_json_timestamp_through_string_and_the_host_offset(self) -> None:
        text = RUNNER.read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"(?<!Try)Parse\(\[string\]", text), "[string] drops the DateTime Kind and a bare Parse then assumes the host's local offset")
        fn = text[text.index("function Get-LeaseEndUtc"):]
        fn = fn[:fn.index("\n}\n") + 3]
        self.assertIn("-is [DateTime]", fn)
        self.assertIn(".ToUniversalTime()", fn)
        self.assertIn("AssumeUniversal", fn)
        self.assertIn("AdjustToUniversal", fn)
        self.assertNotIn("::Parse(", fn, "a zone-less string must go through TryParse with AssumeUniversal, never a bare Parse")


if __name__ == "__main__":
    unittest.main()
