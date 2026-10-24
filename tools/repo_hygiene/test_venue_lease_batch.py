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
}
"""

# um-run.ps1 stand-in, invoked IN-PROCESS by the runner exactly as um-run is. It plays the lease-batch agent for the open and close jobs: an open job gets a
# result whose admission.leaseBatch names lease-<n> (FAKE_OPEN_MODES per open: batch | nobatch | retracted; FAKE_LEASE_REMAIN per open: seconds left), and a
# close job appends the agent's receipt line to <share>\logs\lease-batch-<token>.jsonl.
FAKE_UMRUN = r"""
param([string]$ScriptPath, [string]$JobId, [string]$AgentShare, [int]$TimeoutSec, [int]$MaxQueueWaitSec, [int]$MaxClaimedWaitSec, [int]$MaxAdmissionWaitSec)
$stateFile = Join-Path $AgentShare 'fake-agent-state.json'
$st = $(if (Test-Path -LiteralPath $stateFile) { Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json } else { [pscustomobject]@{ opens = 0; leaseId = $null; token = $null } })
$held = [bool]($env:FAKE_CLAIM_PATH -and (Test-Path -LiteralPath $env:FAKE_CLAIM_PATH))
Add-Content -LiteralPath $env:FAKE_EVENTS -Value ("{0} UMRUN {1} claimHeld={2} body={3}" -f [DateTime]::UtcNow.Ticks, $JobId, $held, ((Get-Content -LiteralPath $ScriptPath -Raw) -replace '\s+', ' ').Trim())
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
$res | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $outbox "$jobId.result.json")
$id = [guid]::NewGuid().ToString()
$rdir = Join-Path $env:FAKE_LEG_ROOT "receipts\CARD-1\$leaf\$Venue"
New-Item -ItemType Directory -Force -Path $rdir | Out-Null
$rp = Join-Path $rdir "$id.json"; Set-Content -LiteralPath $rp -Value '{}'
$edir = Join-Path $env:FAKE_LEG_ROOT "evidence\$id"; New-Item -ItemType Directory -Force -Path $edir | Out-Null
$pre = Pick $env:FAKE_LEG_PRELOADS $k '5'
if ($pre -ne 'none') { Set-Content -LiteralPath (Join-Path $edir 'summary.json') -Value ('{"cpuQuiescence":{"timeMeanPercent":' + $pre + '}}') }
"DVE_OUTCOME=$(Pick $env:FAKE_LEG_OUTCOMES $k 'PASS')"
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
            p.write_text(json.dumps({"legId": name}), encoding="utf-8")
            cls.specs.append(p)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self) -> None:
        self.case = Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp))
        self.share = self.case / "share"
        self.share.mkdir()
        (self.share / "cancel").mkdir()   # the agent's cooperative cancel folder (the agent creates it on the real share)
        self.claims = self.case / "claims"
        self.claim_path = self.claims / "bachelor.claim.json"
        self.events = self.case / "events.log"
        self.run_dir = self.case / "run"

    def run_batch(self, specs: list[Path] | None = None, *extra: str, **env: str) -> subprocess.CompletedProcess:
        e = dict(os.environ)
        e.update({"FAKE_EVENTS": str(self.events), "FAKE_SHARE": str(self.share), "FAKE_CLAIM_PATH": str(self.claim_path), "FAKE_LEG_ROOT": str(self.case)})
        e.update(env)
        # Bachelor's 30 min cooldown is waited out between batches; the offline cases (a stub um-run) relax it unless they test it. A case may override any default.
        defaults = {"-LegBudgetSec": "60", "-LeaseMarginSec": "0", "-ClaimPollSec": "1", "-RecheckSec": "1",
                    "-DeadlineUtc": utc_iso(datetime.now(timezone.utc) + timedelta(seconds=240)), "-LeaseCooldownSec": "0"}
        args = [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(RUNNER), "-LegSpec", ",".join(str(s) for s in (specs or self.specs[:2])),
                "-RunDir", str(self.run_dir), "-SourceCommit", SHA, "-BuildManifestSha256", MANIFEST, "-ClaimDir", str(self.claims),
                "-QuietScript", str(self.tmp / "fake-quiet.ps1"), "-LegScript", str(self.tmp / "fake-leg.ps1"), "-UmRunScript", str(self.tmp / "fake-umrun.ps1"),
                "-AgentShare", str(self.share), *[x for k, v in defaults.items() if k not in extra for x in (k, v)], *extra]
        return subprocess.run(args, capture_output=True, text=True, timeout=600, env=e)

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

    # ---------------------------------------------------------------- in-lease retry
    def test_a_busy_probe_is_retried_inside_the_same_lease(self) -> None:
        r = self.run_batch([self.specs[0]], FAKE_QUIET_MODES="COOLDOWN,BUSY,FOREIGN,QUIET")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        k = self.kinds()
        self.assertEqual([x.split(":")[0] for x in k], ["OPEN", "QUIET", "QUIET", "QUIET", "QUIET", "LEG", "CLOSE"], k)
        [b] = self.batches()
        self.assertEqual((b["probeRetries"], b["retries"], b["legsRan"], b["legsCounted"], b["endReason"]), (3, 3, 1, 1, "ALL_LEGS_DONE"))
        log = (self.run_dir / "batch.log").read_text(encoding="utf-8")
        for reason in ("COOLDOWN_UNMET", "GATE_BUSY"):
            self.assertIn(f"RETRY_IN_LEASE leg=01-fx-a-spec batch=1 reason={reason}", log)

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
        for extra, needle in ((["-LeaseCooldownSec", "0"], "LEASE_CAP_RELAXED"), (["-LeaseBudgetWindowSec", "3600"], "LEASE_CAP_RELAXED"),
                              (["-LeaseCapSec", "2701"], "LeaseCapSec"), (["-RenewGapMaxSec", "121"], "RenewGapMaxSec"), (["-LeaseBudgetSec", "5401"], "LeaseBudgetSec")):
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
        # lease for it (the open may yet take one before the agent reads the cancel).
        r = self.run_batch([self.specs[0]], "-DeadlineUtc", utc_iso(datetime.now(timezone.utc) + timedelta(seconds=12)), "-CancelReconcileSec", "3",
                           FAKE_OPEN_MODES="gatehold")
        self.assertEqual(r.returncode, 11, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["openCancel"]["requested"], b["openCancel"]["state"]), ("REQUESTED", "UNRECONCILED"))
        self.assertTrue((self.share / "cancel" / f"{b['openJobId']}.cancel").exists(), "the cancel request stays for the agent")
        self.assertEqual(b["ledger"]["basis"], "open-unreconciled")
        self.assertEqual(batch_result(r.stdout)["stopReason"], "OPEN_CANCEL_UNRECONCILED")

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


if __name__ == "__main__":
    unittest.main()
