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
        self.case = Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp))
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
        script = f"Import-Module '{MODULE}' -Force -DisableNameChecking\n$now = '{utc_iso(now)}'\n$o = [ordered]@{{}}\n"
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
        script = f"""Import-Module '{MODULE}' -Force -DisableNameChecking
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
        script = f"""Import-Module '{MODULE}' -Force -DisableNameChecking
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
        script = f"""Import-Module '{MODULE}' -Force -DisableNameChecking
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
        script = f"""Import-Module '{MODULE}' -Force -DisableNameChecking
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
            f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{AGENT}', [ref]$null, [ref]$null)\n"
            "foreach ($n in 'Get-JobNotAfterVerdict', 'Read-JobNotAfterSidecar') { . ([scriptblock]::Create($ast.Find({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq $n }, $true).Extent.Text)) }\n"
            f"$notAfterDir = '{notafter_dir}'\n"
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
    def _hold_exclusive(self, path: Path, hold_s: float) -> threading.Thread:
        """Hold `path` open with share mode 0 (what Request-/Update-VenueClaim's FileShare.None does) for hold_s seconds on a background thread."""
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = k32.CreateFileW(str(path), 0x80000000 | 0x40000000, 0, None, 3, 0x80, None)   # GENERIC_READ|GENERIC_WRITE, share none, OPEN_EXISTING
        self.assertNotEqual(handle, wintypes.HANDLE(-1).value, "could not take the exclusive handle")

        def hold() -> None:
            time.sleep(hold_s)
            k32.CloseHandle(handle)

        t = threading.Thread(target=hold, daemon=True)
        t.start()
        return t

    def test_the_claim_reader_waits_out_a_writer_that_holds_the_file(self) -> None:
        # The race the merge_group flake hit: a reader that lands while the runner holds the claim (FileShare.None on the take or a renewal) got PermissionError;
        # one that lands on a half-written or not yet created file gets JSONDecodeError or FileNotFoundError. The direct read raises on each; read_claim_json waits.
        self.claims.mkdir()
        body = {"schema": "mlv-app/venue-claim/v1", "venue": "bachelor", "nonce": "n" * 32}
        full = json.dumps(body)
        self.claim_path.write_text(full, encoding="utf-8")

        t = self._hold_exclusive(self.claim_path, 1.0)
        with self.assertRaises(PermissionError):
            self.claim_path.read_text(encoding="utf-8")
        self.assertEqual(read_claim_json(self.claim_path), body, "returns the full JSON once the writer releases")
        t.join(5)

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
            script = f"Import-Module '{MODULE}' -Force -DisableNameChecking\n$c = Get-VenueClaim -Venue bachelor -ClaimDir '{self.claims}'\n[ordered]@{{ state = $c.state; reason = $c.reason; nonce = $c.claim.nonce }} | ConvertTo-Json -Compress\n"
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
            f"Import-Module '{MODULE}' -Force -DisableNameChecking\n"
            f"$c = Request-VenueClaim -Venue bachelor -RunDir 'x' -TtlSec 60 -ClaimDir '{self.claims}'\n"
            f"$before = [IO.File]::ReadAllText('{self.claim_path}')\n"
            f"$other = Update-VenueClaim -Venue bachelor -Nonce 'not-the-nonce' -TtlSec 600 -ClaimDir '{self.claims}'\n"
            f"$same = ([IO.File]::ReadAllText('{self.claim_path}') -ceq $before)\n"
            f"$mine = Update-VenueClaim -Venue bachelor -Nonce $c.claim.nonce -TtlSec 600 -ClaimDir '{self.claims}'\n"
            f"$after = Get-VenueClaim -Venue bachelor -ClaimDir '{self.claims}'\n"
            f"$rel = Release-VenueClaim -Venue bachelor -Nonce $c.claim.nonce -ClaimDir '{self.claims}'\n"
            f"$gone = Update-VenueClaim -Venue bachelor -Nonce $c.claim.nonce -TtlSec 600 -ClaimDir '{self.claims}'\n"
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
            f"Import-Module '{MODULE}' -Force -DisableNameChecking\n"
            "[ordered]@{\n"
            f"  noFns = Test-VenueJobLivenessReadback -AgentShare '{self.share}' -JobId 'j1' -AgentScript '{empty}'\n"
            f"  noFile = Test-VenueJobLivenessReadback -AgentShare '{self.share}' -JobId 'j1' -AgentScript '{self.case / 'missing.ps1'}'\n"
            f"  noSidecar = Test-VenueJobLivenessReadback -AgentShare '{self.share}' -JobId 'j1'\n"
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
            f"Import-Module '{MODULE}' -Force -DisableNameChecking\n"
            f"$t = [Diagnostics.Stopwatch]::StartNew(); $r = Test-VenueJobLivenessReadback -AgentShare '{self.share}' -JobId 'j1' -AgentScript '{stall}' -TimeoutSec 1; $t.Stop()\n"
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
        # Control for the case above: here the open IS (maybe) submitted. Submit-AgentJob throws for the open job before it reports anything, so $op stays null; the
        # submitted flag, not $op, tells the finally block the open may be in the agent's gate: cancel requested, close submitted, open-unreconciled row charged.
        anchor = '    $f = Join-Path $jobDir "$JobId.job.ps1"'
        self._mirror_with_bad_sidecar(runner_patches=((anchor, "    if ($JobId -like 'lease-batch-open-*') { throw 'simulated submit failure' }\n" + anchor),))
        r = self.run_batch([self.specs[0]], "-DeadlineUtc", utc_iso(datetime.now(timezone.utc) + timedelta(seconds=30)), "-CancelReconcileSec", "3")
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["openCancel"]["requested"], b["openCancel"]["state"]), ("REQUESTED", "UNRECONCILED"), b)
        self.assertTrue((self.share / "cancel" / f"{b['openJobId']}.cancel").exists(), "the cancel request stays for the agent")
        self.assertTrue(b["closeJobId"], "the close was not submitted")
        self.assertEqual(b["ledger"]["basis"], "open-unreconciled", b["ledger"])
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
            f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{AGENT}', [ref]$null, [ref]$null)\n"
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
            f"Import-Module '{MODULE}' -Force -DisableNameChecking\n"
            f"$s = '{self.share}'\n"
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
        self.leg_args = self.case / "leg-args.jsonl"
        # two builds staged in the venue's cache, by the path Invoke-VenueLeg hashes
        self.manifests = {c: self.stage(c, f"build {c[:1]}") for c in (self.commit_a, self.commit_b)}

    def stage(self, commit: str, body: str, **override: object) -> str:
        # the three fields Assert-AttrCudaBuildManifest (the leg job's own validator) requires: the build's commit, its dll pair digest and a boolean symbol presence
        cache = self.share / "cache"
        cache.mkdir(exist_ok=True)
        p = cache / f"playback-attr-3-cuda-{commit[:12]}-build.json"
        doc = {"sourceCommit": commit, "dllPairManifestSha256": "f" * 64, "pendingSymbolPresence": False, "body": body}
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
            f"Import-Module '{MODULE}' -Force -DisableNameChecking\n"
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
        hold = self._hold_exclusive(log, 90)
        r = self.run_plan(self.case / "missing-plan.json")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("BATCH_USAGE LEG_PLAN_REFUSED reason=LEG_PLAN_UNREADABLE", r.stderr)
        self.assertEqual(self.kinds(), [])
        del hold

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
        script = (f"Import-Module '{MODULE}' -Force -DisableNameChecking\n"
                  f"$r = Request-VenueClaim -Venue bachelor -RunDir 'C:\\other-run' -Purpose foreign -TtlSec 3600 -ClaimDir '{self.claims}' -OwnerPid {os.getpid()}\n"
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

    def test_r5_h2_the_interleave_helper_keeps_an_array_valued_item_whole(self) -> None:
        # key3 H2 (sol): the bare Dequeue() output was enumerated, so one array-valued item came back as its elements.
        script = (
            f"Import-Module '{MODULE}' -Force -DisableNameChecking\n"
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
            f"Import-Module '{MODULE}' -Force -DisableNameChecking\n"
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
            f"Import-Module '{MODULE}' -Force -DisableNameChecking\n"
            f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{RUNNER}', [ref]$null, [ref]$null)\n"
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
