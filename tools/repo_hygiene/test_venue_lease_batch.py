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
    $adm = [ordered]@{ gate = 'fake'; mode = 'lease'; label = 'clear'; heavySlotHeld = "1:$(1001 + $n)"; slot = 'C:\t\.heavy-slots\1'; leaseMinutes = 45; waitedSec = 7; leaseRequests = 2
        leaseRefusals = @([ordered]@{ reason = 'refused'; rc = 75; why = 'quiet lease cooldown' }) }
    if ($mode -ne 'nobatch') { $adm['leaseBatch'] = [ordered]@{ token = $token; leaseId = $lid; seq = 1; reused = $false; untilEpoch = $now.ToUnixTimeSeconds() + $remain; remainingSec = $remain } }
    [pscustomobject]@{ opens = $n + 1; leaseId = $lid; token = $token } | ConvertTo-Json | Set-Content -LiteralPath $stateFile
    # r5: FAKE_OPEN_SLEEP per open: um-run blocks this long first, as it does while the agent re-asks a refused (exit 75) request for the open
    $sleep = [int](Pick $env:FAKE_OPEN_SLEEP $n '0')
    # r5b: FAKE_OPEN_CLAIM_READONLY per open (1 = the claim file is read-only for that wait, so every renewal comes back BUSY; writable again after it)
    $ro = (Pick $env:FAKE_OPEN_CLAIM_READONLY $n '0') -eq '1'
    if ($ro) { Set-ItemProperty -LiteralPath $env:FAKE_CLAIM_PATH -Name IsReadOnly -Value $true }
    if ($sleep -gt 0) { Add-Content -LiteralPath $env:FAKE_EVENTS -Value ("{0} OPENWAIT {1} sec={2}" -f [DateTime]::UtcNow.Ticks, $JobId, $sleep); Start-Sleep -Seconds $sleep; $now = [DateTimeOffset]::UtcNow }
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
param([string]$Venue, [string]$LegSpec, [string]$SourceCommit, [string]$BuildManifestSha256, [string]$Backend = '', [string]$SheetCopyDir = '', [string]$LeaseBatch = '')
$share = $env:FAKE_SHARE
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
$lid = $(if ($lose -contains ($k + 1)) { 'lease-somebody-else' } else { $st.leaseId })
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
$rdir = Join-Path $env:FAKE_LEG_ROOT "receipts\CARD-1\$leaf\$Venue"
New-Item -ItemType Directory -Force -Path $rdir | Out-Null
$rp = Join-Path $rdir "$id.json"; Set-Content -LiteralPath $rp -Value '{}'
$edir = Join-Path $env:FAKE_LEG_ROOT "evidence\$id"; New-Item -ItemType Directory -Force -Path $edir | Out-Null
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
        args, e = self.batch_cmd([self.specs[0]], "-ClaimTtlSec", "60", FAKE_OPEN_SLEEP="80")
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
        doc = {"sourceCommit": commit, "dllPairManifestSha256": "f" * 64, "pendingSymbolPresence": False, **ARTIFACT_SHAS, "body": body}
        doc.update(override)
        p.write_text(json.dumps({k: v for k, v in doc.items() if v is not ABSENT}), encoding="utf-8")
        import hashlib
        return hashlib.sha256(p.read_bytes()).hexdigest()

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
        anchor = "        $hit = Get-InputOutputRefusal\n"   # the claim-time guard (the only 8-space one); independent of what follows it, so the same fixture runs on a runner without the sticky hit
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
                          + ("; $script:rpf06DenySpec = $true" if how == "denied" else ""))
                args, e, patch = self._two_batch_plan(inject)
                patches = [patch]
                if how == "denied":
                    patches.append((self._RPF06_RESOLVE, f"if ($script:rpf06DenySpec -and $Path -ieq '{s}') {{ throw [UnauthorizedAccessException]::new('injected: access to the input path is denied') }}; " + self._RPF06_RESOLVE))
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
            time.sleep(2)
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
            "gatehold": (11, "OPEN_CANCEL_UNRECONCILED", ("-CancelReconcileSec", "3", "-DeadlineUtc", "+60")),
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
        args, e = self.plan_cmd(self.write_plan([self.item(spec, "A", self.commit_a)]), *extra, FAKE_OPEN_SLEEP="12", FAKE_OPEN_MODES=mode)
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=e)
        try:
            self._wait_for_log("BATCH_OPEN_LIVENESS_READBACK")
            time.sleep(2)
            (self.run_dir / "batch.log").unlink(missing_ok=True)
            os.link(spec, self.run_dir / "batch.log")
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
        time.sleep(2)
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
                time.sleep(2)
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


# 2026-10-25 LEASE-BATCH-LEASE-END-TZ-1: the agent's result reaches the runner through ConvertFrom-Json, which on PowerShell 7 turns "endedUtc": "...Z" into a
# [DateTime] of Kind Utc. Get-LeaseEndUtc used to run it through [string] + DateTimeOffset.Parse, which drops the Kind and assumes the HOST's local offset, so on a
# Central host every lease end landed 5 h early (roomSec=-15301) and no leg ever started. CI hosts are UTC (offset 0), where the old code passes, so the behavioural
# cases below only go red on a non-UTC host; the source case is the one that guards on UTC.
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
