"""Tests for VENUE-CHAIN-RUNNER-1: the tracked venue chain runner tools/profiling/dual-venue/Invoke-VenueChain.ps1 and its VenueChain.psm1.

WHAT IS PINNED (the three ways the hand-written fleet-runs chains failed on 2026-10-09)
  * MUTUAL WAIT: the old rules are reproduced in a fixture and shown to block each other (FILM-FLAVOR-2 venue-wait-then-chain.ps1:16-34 waits
    while another fleet-runs chain*/leg* process lives; BACKLOG-1 r1c quiet.ps1:27-46 waits while a live lane's prompt names bachelor). Two
    runner chains in the same situation never block each other: the second runs after the first releases its claim.
  * HOLD WHILE WAITING: a chain holds the venue claim only around a leg. Its quiet wait runs with no claim; the re-probe under the claim is one probe.
  * EXIT 6: Wait-VenueQuiet exit 6 (VENUE_QUIET_UNKNOWN = not measured) means the leg is NOT run; -OnUnknown Stop (default) stops the chain.
  * the claim: atomic, a stale claim (dead pid, reused pid, expired) is broken exactly once, a live claim blocks until the deadline.
  * the per-hour QUIET-rate table from gate logs, and -PreferQuietWindows' choice of hour.

Offline only: Wait-VenueQuiet and Invoke-VenueLeg are replaced by fake scripts passed with -QuietScript / -LegScript, and every claim lives in a
temp -ClaimDir. Nothing here reaches a venue, the board's claim path, or a process other than the ones the test starts.
Mutation tests: the guarding statement is taken out of a COPY of the runner and the scenario must change.
Windows-only (PowerShell tools).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DV = ROOT / "tools" / "profiling" / "dual-venue"
RUNNER = DV / "Invoke-VenueChain.ps1"
MODULE = DV / "VenueChain.psm1"
PWSH = shutil.which("pwsh")
requires_windows_pwsh = unittest.skipIf(PWSH is None or sys.platform != "win32", "needs pwsh on Windows")

SHA = "a" * 40
MANIFEST = "b" * 64

# Wait-VenueQuiet stand-in. Mode per call from FAKE_QUIET_MODES (comma list, the last repeats), counted per run dir. Records each call with whether
# THIS run's claim was held at that moment (the claim file exists and names this run dir).
FAKE_QUIET = r"""
param([string]$Venue, [string]$WorkDir, [string]$GateLog, [double]$ThresholdPercent = 20, [int]$MaxWaitSec = 1800, [int]$RecheckSec = 90)
$runDir = Split-Path -Parent $WorkDir
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
$countFile = Join-Path $runDir 'fake-quiet-count.txt'
$n = $(if (Test-Path -LiteralPath $countFile) { [int](Get-Content -LiteralPath $countFile) } else { 0 })
Set-Content -LiteralPath $countFile -Value ($n + 1)
$modes = @(([string]$env:FAKE_QUIET_MODES) -split ',' | Where-Object { $_ })
if ($modes.Count -eq 0) { $modes = @('QUIET') }
$mode = $modes[[Math]::Min($n, $modes.Count - 1)]
if ($MaxWaitSec -gt 0 -and $env:FAKE_QUIET_SLEEP_PRE) { Start-Sleep -Milliseconds ([int]([double]$env:FAKE_QUIET_SLEEP_PRE * 1000)) }
$held = $false
if ($env:FAKE_CLAIM_PATH -and (Test-Path -LiteralPath $env:FAKE_CLAIM_PATH)) {
    try { $c = Get-Content -LiteralPath $env:FAKE_CLAIM_PATH -Raw | ConvertFrom-Json; $held = ([string]$c.runDir -eq $runDir) } catch { $held = $false }
}
if ($env:FAKE_QUIET_LOG) { Add-Content -LiteralPath $env:FAKE_QUIET_LOG -Value ("{0} {1} maxWait={2} ownClaimHeld={3} mode={4}" -f [DateTime]::UtcNow.Ticks, (Split-Path -Leaf $runDir), $MaxWaitSec, $held, $mode) }
switch ($mode) {
    'QUIET' { 'PROBE venue-quiet-probe-x bachelor samples=5.0/5.0/5.0 mean=5.0% state=QUIET'; "QUIET mean=5.0% threshold=$ThresholdPercent% waitedSec=1"; exit 0 }
    'COOLDOWN' { "COOLDOWN_UNMET mean=32.0% threshold=$ThresholdPercent% waitedSec=544"; exit 0 }
    'UNKNOWN' { "VENUE_QUIET_UNKNOWN mean=UNKNOWN threshold=$ThresholdPercent% waitedSec=1 reason=probe-failed note=fake"; exit 6 }
    'NOVERDICT' { 'GATE quiet-probe bachelor queued=0 running=0'; exit 0 }
    'BUSY' { 'GATE_BUSY quiet-probe waitedSec=1'; exit 3 }
    'UNREADABLE' { 'GATE_UNREADABLE quiet-probe bachelor'; 'DECISION UNKNOWN mean=UNKNOWN'; exit 4 }
    'ODD' { 'something'; exit 9 }
}
"""

# Invoke-VenueLeg stand-in: records START/END (UTC ticks) to FAKE_LEG_LOG, sleeps FAKE_LEG_SLEEP, and lays out a receipt path + run summary like
# Invoke-VenueLeg does when FAKE_LEG_PRELOADS (comma list, one per call) is set.
FAKE_LEG = r"""
param([string]$Venue, [string]$LegSpec, [string]$SourceCommit, [string]$BuildManifestSha256, [string]$Backend = '', [string]$SheetCopyDir = '')
$leaf = Split-Path -Leaf $LegSpec
if ($env:FAKE_LEG_LOG) { Add-Content -LiteralPath $env:FAKE_LEG_LOG -Value ("START {0} {1} {2}" -f [DateTime]::UtcNow.Ticks, $leaf, $PID) }
if ($env:FAKE_LEG_SLEEP) { Start-Sleep -Milliseconds ([int]([double]$env:FAKE_LEG_SLEEP * 1000)) }
# r1 (VENUE-CHAIN-UNHEALTHY-STREAK-STOP-1): FAKE_LEG_OUTCOMES = comma list of OUTCOME or OUTCOME:DETAIL, one per call (the last repeats), counted in FAKE_LEG_ROOT;
# the leg prints DVE_OUTCOME / DVE_DETAIL / DVE_RECEIPT_PATH (<root>\receipts\CARD-1\<leg>\<venue>\rcpt-<n>.json) as Invoke-VenueLeg does. FAKE_LEG_DELETE_PATH is
# removed by the leg (a file vanishing under a live chain).
if ($env:FAKE_LEG_DELETE_PATH) { Remove-Item -LiteralPath $env:FAKE_LEG_DELETE_PATH -Force -ErrorAction SilentlyContinue }
if ($env:FAKE_LEG_OUTCOMES) {
    $oc = Join-Path $env:FAKE_LEG_ROOT 'fake-leg-outcome-count.txt'
    $k = $(if (Test-Path -LiteralPath $oc) { [int](Get-Content -LiteralPath $oc) } else { 0 })
    Set-Content -LiteralPath $oc -Value ($k + 1)
    $items = @($env:FAKE_LEG_OUTCOMES -split ',')
    $parts = ($items[[Math]::Min($k, $items.Count - 1)] -split ':', 2)
    "DVE_OUTCOME=$($parts[0])"
    if ($parts.Count -gt 1) { "DVE_DETAIL=$($parts[1])" }
    "DVE_RECEIPT_PATH=$(Join-Path $env:FAKE_LEG_ROOT ('receipts\CARD-1\' + [IO.Path]::GetFileNameWithoutExtension($leaf) + '\' + $Venue + '\rcpt-' + ($k + 1) + '.json'))"
} else { 'DVE_OUTCOME=PASS' }
if ($env:FAKE_LEG_PRELOADS) {
    $root = $env:FAKE_LEG_ROOT
    $countFile = Join-Path $root 'fake-leg-count.txt'
    $n = $(if (Test-Path -LiteralPath $countFile) { [int](Get-Content -LiteralPath $countFile) } else { 0 })
    Set-Content -LiteralPath $countFile -Value ($n + 1)
    $vals = @($env:FAKE_LEG_PRELOADS -split ',')
    $id = [guid]::NewGuid().ToString()
    $rdir = Join-Path $root "receipts\CARD-1\$([IO.Path]::GetFileNameWithoutExtension($leaf))\$Venue"
    New-Item -ItemType Directory -Force -Path $rdir | Out-Null
    $rp = Join-Path $rdir "$id.json"; Set-Content -LiteralPath $rp -Value '{}'
    $edir = Join-Path $root "evidence\$id"; New-Item -ItemType Directory -Force -Path $edir | Out-Null
    $v = $vals[[Math]::Min($n, $vals.Count - 1)]
    if ($v -ne 'none') { Set-Content -LiteralPath (Join-Path $edir 'summary.json') -Value ('{"cpuQuiescence":{"timeMeanPercent":' + $v + '}}') }
    "DVE_RECEIPT_PATH=$rp"
}
if ($env:FAKE_LEG_LOG) { Add-Content -LiteralPath $env:FAKE_LEG_LOG -Value ("END {0} {1} {2}" -f [DateTime]::UtcNow.Ticks, $leaf, $PID) }
# r2: the leg whose spec file name equals FAKE_LEG_FAIL_LEAF dies with FAKE_LEG_FAIL_CODE AFTER printing DVE_OUTCOME=PASS (a crash that leaves a stale verdict line).
if ($env:FAKE_LEG_FAIL_LEAF -and $env:FAKE_LEG_FAIL_LEAF -eq $leaf) { exit ([int]$env:FAKE_LEG_FAIL_CODE) }
exit 0
"""

# The OLD rules, reproduced as fixtures scoped to a temp fleet-runs root (so no real process on this machine can satisfy them).
# A = FILM-FLAVOR-2 venue-wait-then-chain.ps1:16-22 + 29-34: wait while any OTHER fleet-runs chain*/leg*/run-leg* .ps1 process is alive.
OLD_WAITER_A = r"""
param([string]$Root, [string]$Self, [int]$MaxSec = 10)
$lane = Join-Path $Root "fleet-runs\$Self"
$me = Get-Process -Id $PID
@{ state = 'running'; containment = @{ runnerPid = $PID; runnerCreatedUtc = $me.StartTime.ToUniversalTime().ToString('o') } } | ConvertTo-Json | Set-Content (Join-Path $lane 'opus-001.receipt.json')
Set-Content (Join-Path $Root "$Self.started") 'x'
while (@(Get-ChildItem $Root -Filter '*.started').Count -lt 2) { Start-Sleep -Milliseconds 200 }
function Other-Chains {
    @(Get-CimInstance Win32_Process -Filter "Name='pwsh.exe' OR Name='powershell.exe'" -ErrorAction SilentlyContinue | Where-Object {
        $c = [string]$_.CommandLine
        $c -and $c.Contains($Root) -and $c -notmatch [regex]::Escape($Self) -and
        ($c -match '\\fleet-runs\\[^"]*\\(chain[^\\"]*|leg[^\\"]*|run-leg[^\\"]*)\.ps1' -or $c -match 'Invoke-VenueLeg\.ps1')
    })
}
$t0 = Get-Date
while (((Get-Date) - $t0).TotalSeconds -lt $MaxSec) {
    $o = Other-Chains
    if ($o.Count -gt 0) { "OTHER_CHAIN_LIVE " + (($o | ForEach-Object { "pid=$($_.ProcessId)" }) -join ' '); Start-Sleep -Seconds 1; continue }
    'LEG_RAN'; exit 0
}
"OLD_RULE_TIMEOUT blockedSec=$([int]((Get-Date) - $t0).TotalSeconds)"
exit 9
"""

# B = BACKLOG-1 r1c quiet.ps1:19-49 (Test-AliveAt + Get-LaneScan) and 65-76: wait while any OTHER live lane (receipt state=running, pid alive at its
# created time) has a prompt naming bachelor.
OLD_WAITER_B = r"""
param([string]$Root, [string]$Self, [int]$MaxSec = 10)
$lane = Join-Path $Root "fleet-runs\$Self"
$me = Get-Process -Id $PID
@{ state = 'running'; containment = @{ runnerPid = $PID; runnerCreatedUtc = $me.StartTime.ToUniversalTime().ToString('o') } } | ConvertTo-Json | Set-Content (Join-Path $lane 'opus-001.receipt.json')
Set-Content (Join-Path $Root "$Self.started") 'x'
while (@(Get-ChildItem $Root -Filter '*.started').Count -lt 2) { Start-Sleep -Milliseconds 200 }
function Test-AliveAt([object]$ProcId, [object]$CreatedUtc) {
    if (-not $ProcId -or -not $CreatedUtc) { return $false }
    $p = Get-Process -Id ([int]$ProcId) -EA SilentlyContinue
    if (-not $p) { return $false }
    try { $c = ([DateTime]$CreatedUtc).ToUniversalTime() } catch { return $false }
    [Math]::Abs(($p.StartTime.ToUniversalTime() - $c).TotalSeconds) -lt 10
}
function Get-LaneScan {
    $rows = @()
    foreach ($d in Get-ChildItem (Join-Path $Root 'fleet-runs') -Directory -Filter 'lane-*') {
        if ($d.Name -eq $Self) { continue }
        foreach ($rc in Get-ChildItem $d.FullName -Filter '*.receipt.json' -EA SilentlyContinue) {
            try { $j = Get-Content $rc.FullName -Raw | ConvertFrom-Json } catch { continue }
            if ($j.state -ne 'running') { continue }
            if (-not (Test-AliveAt $j.containment.runnerPid $j.containment.runnerCreatedUtc)) { continue }
            $pf = Get-ChildItem $d.FullName -Filter 'prompt.md' -EA SilentlyContinue | Select-Object -First 1
            $names = $pf -and ((Get-Content $pf.FullName -Raw) -match '(?i)bachelor')
            $rows += [pscustomobject]@{ lane = $d.Name; blocking = [bool]$names }
        }
    }
    $rows
}
$t0 = Get-Date
while (((Get-Date) - $t0).TotalSeconds -lt $MaxSec) {
    $lb = @(Get-LaneScan | Where-Object blocking).Count
    if ($lb -gt 0) { "LANES blocking=$lb"; Start-Sleep -Seconds 1; continue }
    'LEG_RAN'; exit 0
}
"OLD_RULE_TIMEOUT blockedSec=$([int]((Get-Date) - $t0).TotalSeconds)"
exit 9
"""


def utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pwsh_text(command: str) -> str:
    return subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True, text=True, timeout=60, check=True).stdout.strip()


def process_start_iso(pid: int) -> str:
    return pwsh_text(f"(Get-Process -Id {pid}).StartTime.ToUniversalTime().ToString('o')")


def chain_result(stdout: str) -> dict:
    lines = [l for l in stdout.splitlines() if l.startswith("CHAIN_RESULT=")]
    if not lines:
        raise AssertionError("no CHAIN_RESULT line in:\n" + stdout)
    return json.loads(lines[-1][len("CHAIN_RESULT="):])


@requires_windows_pwsh
class VenueChainRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="venue-chain-"))
        cls.fake_quiet = cls.tmp / "fake-quiet.ps1"
        cls.fake_quiet.write_text(FAKE_QUIET, encoding="utf-8")
        cls.fake_leg = cls.tmp / "fake-leg.ps1"
        cls.fake_leg.write_text(FAKE_LEG, encoding="utf-8")
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
        self.claims = self.case / "claims"
        self.claim_path = self.claims / "bachelor.claim.json"
        self.quiet_log = self.case / "quiet-calls.log"
        self.leg_log = self.case / "leg-calls.log"

    # ---------------------------------------------------------------- helpers
    def env(self, **extra: str) -> dict:
        e = dict(os.environ)
        e.update({"FAKE_QUIET_LOG": str(self.quiet_log), "FAKE_LEG_LOG": str(self.leg_log), "FAKE_CLAIM_PATH": str(self.claim_path), "FAKE_LEG_ROOT": str(self.case)})
        e.update(extra)
        return e

    def chain_args(self, run_dir: Path, specs: list[Path], *extra: str, runner: Path = RUNNER, deadline_sec: int = 120, quiet: Path | None = None) -> list[str]:
        return [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(runner), "-Venue", "bachelor",
                "-LegSpec", ",".join(str(s) for s in specs), "-RunDir", str(run_dir), "-SourceCommit", SHA, "-BuildManifestSha256", MANIFEST,
                "-ClaimDir", str(self.claims), "-QuietScript", str(quiet or self.fake_quiet), "-LegScript", str(self.fake_leg),
                "-CooldownSec", "0", "-ClaimPollSec", "1", "-DeadlineUtc", utc_iso(datetime.now(timezone.utc) + timedelta(seconds=deadline_sec)), *extra]

    def run_chain(self, run_dir: Path, specs: list[Path], *extra: str, env: dict | None = None, runner: Path = RUNNER, deadline_sec: int = 120, quiet: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(self.chain_args(run_dir, specs, *extra, runner=runner, deadline_sec=deadline_sec, quiet=quiet), capture_output=True, text=True, timeout=300, env=env or self.env())

    def leg_calls(self) -> list[str]:
        return self.leg_log.read_text(encoding="utf-8").splitlines() if self.leg_log.exists() else []

    def quiet_calls(self) -> list[str]:
        return self.quiet_log.read_text(encoding="utf-8").splitlines() if self.quiet_log.exists() else []

    def chain_log(self, run_dir: Path) -> list[str]:
        return (run_dir / "chain.log").read_text(encoding="utf-8").splitlines()

    def mutated_runner(self, old: str, new: str, count: int | None = None) -> Path:
        d = Path(tempfile.mkdtemp(prefix="mut-", dir=self.case))
        text = RUNNER.read_text(encoding="utf-8")
        found = text.count(old)
        self.assertGreater(found, 0, f"mutation anchor not in the runner: {old!r}")
        if count is not None:
            self.assertEqual(found, count, f"mutation anchor count changed: {old!r}")
        (d / RUNNER.name).write_text(text.replace(old, new), encoding="utf-8")
        shutil.copy2(MODULE, d / MODULE.name)
        return d / RUNNER.name

    def write_claim(self, pid: int, created_iso: str, expires: datetime, run_dir: str = "C:\\elsewhere\\lane-OTHER") -> str:
        self.claims.mkdir(parents=True, exist_ok=True)
        text = json.dumps({"schema": "mlv-app/venue-claim/v1", "venue": "bachelor", "ownerPid": pid, "ownerCreatedUtc": created_iso, "host": os.environ.get("COMPUTERNAME", ""),
                           "runDir": run_dir, "purpose": "fixture", "claimedUtc": utc_iso(datetime.now(timezone.utc)), "expiresUtc": utc_iso(expires), "nonce": "fixture-nonce"})
        self.claim_path.write_text(text, encoding="utf-8")
        return text

    # ---------------------------------------------------------------- MUTUAL WAIT
    def test_old_mutual_wait_rules_block_each_other_RED_fixture(self) -> None:
        root = self.case / "old"
        for lane in ("lane-A", "lane-B"):
            (root / "fleet-runs" / lane / "tools").mkdir(parents=True)
            (root / "fleet-runs" / lane / "prompt.md").write_text("run the legs on Bachelor", encoding="utf-8")
        a = root / "fleet-runs" / "lane-A" / "tools" / "venue-wait-then-chain.ps1"
        a.write_text(OLD_WAITER_A, encoding="utf-8")
        b = root / "fleet-runs" / "lane-B" / "tools" / "chain-b.ps1"   # a chain*.ps1 under fleet-runs: what waiter A's scan matches
        b.write_text(OLD_WAITER_B, encoding="utf-8")
        base = [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File"]
        pa = subprocess.Popen([*base, str(a), "-Root", str(root), "-Self", "lane-A", "-MaxSec", "10"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        pb = subprocess.Popen([*base, str(b), "-Root", str(root), "-Self", "lane-B", "-MaxSec", "10"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        out_a, _ = pa.communicate(timeout=120)
        out_b, _ = pb.communicate(timeout=120)
        # each saw the other as the venue owner, and neither ever ran its leg
        self.assertEqual((pa.returncode, pb.returncode), (9, 9), out_a + "\n----\n" + out_b)
        self.assertIn("OTHER_CHAIN_LIVE", out_a)
        self.assertIn("LANES blocking=1", out_b)
        self.assertNotIn("LEG_RAN", out_a + out_b)

    def test_two_concurrent_chains_never_deadlock_and_the_second_runs_after_the_first_releases(self) -> None:
        rd_a, rd_b = self.case / "lane-A", self.case / "lane-B"
        env = self.env(FAKE_QUIET_SLEEP_PRE="1", FAKE_LEG_SLEEP="5")
        pa = subprocess.Popen(self.chain_args(rd_a, [self.specs[0]]), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
        pb = subprocess.Popen(self.chain_args(rd_b, [self.specs[1]]), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
        out_a, _ = pa.communicate(timeout=240)
        out_b, _ = pb.communicate(timeout=240)
        self.assertEqual((pa.returncode, pb.returncode), (0, 0), out_a + "\n----\n" + out_b)
        for out in (out_a, out_b):
            r = chain_result(out)
            self.assertEqual([l["status"] for l in r["legs"]], ["RAN"])
        calls = self.leg_calls()
        spans = {}
        for line in calls:
            kind, ticks, leaf, _pid = line.split()
            spans.setdefault(leaf, {})[kind] = int(ticks)
        self.assertEqual(set(spans), {"fx-a.json", "fx-b.json"}, calls)
        first, second = sorted(spans.values(), key=lambda s: s["START"])
        self.assertGreaterEqual(second["START"], first["END"], "the two legs overlapped: the claim did not serialise them")
        logs = self.chain_log(rd_a) + self.chain_log(rd_b)
        self.assertTrue(any(" CLAIM_HELD " in l and "(no claim held)" in l for l in logs), "the later chain never waited on the earlier one's claim")
        self.assertFalse(self.claim_path.exists(), "a claim was left behind")

    def test_a_waiting_chain_holds_no_claim_and_reprobes_once_under_it(self) -> None:
        rd = self.case / "lane-W"
        r = self.run_chain(rd, [self.specs[0], self.specs[1]], env=self.env(FAKE_QUIET_SLEEP_PRE="0.5"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        calls = self.quiet_calls()
        self.assertEqual(len(calls), 4, calls)   # per leg: one pre-claim wait, one re-probe
        pre = [c for c in calls if "maxWait=600" in c]
        under = [c for c in calls if "maxWait=0" in c]
        self.assertEqual(len(pre), 2); self.assertEqual(len(under), 2)
        self.assertTrue(all("ownClaimHeld=False" in c for c in pre), pre)
        self.assertTrue(all("ownClaimHeld=True" in c for c in under), under)
        log = self.chain_log(rd)
        order = [l.split(" ", 2)[1] for l in log if not l.startswith("CHAIN_RESULT=")]
        first_leg = order[order.index("QUIET"):order.index("CLAIM_RELEASED") + 1]
        self.assertEqual(first_leg, ["QUIET", "CLAIM_TAKEN", "QUIET", "LEG", "LEG", "CLAIM_RELEASED"], order)

    def test_mutation_claim_taken_before_the_quiet_wait_is_caught(self) -> None:
        mut = self.mutated_runner("        # (b) the quiet wait, WITHOUT the claim\n",
                                  "        $hold = Request-VenueClaim -Venue $Venue -RunDir $RunDir -Purpose 'mutant' -ClaimDir $ClaimDir\n", count=1)
        rd = self.case / "lane-M"
        self.run_chain(rd, [self.specs[0]], runner=mut, deadline_sec=8)
        pre = [c for c in self.quiet_calls() if "maxWait=600" in c]
        self.assertTrue(pre and all("ownClaimHeld=True" in c for c in pre), pre)

    # ---------------------------------------------------------------- EXIT 6
    def test_exit6_not_measured_leg_is_not_run_and_the_chain_stops(self) -> None:
        rd = self.case / "lane-U"
        r = self.run_chain(rd, [self.specs[0], self.specs[1]], env=self.env(FAKE_QUIET_MODES="UNKNOWN"))
        self.assertEqual(r.returncode, 6, r.stdout + r.stderr)
        self.assertEqual(self.leg_calls(), [], "a leg ran on a venue that was not measured")
        log = self.chain_log(rd)
        self.assertTrue(any(re.match(r"^\S+Z LEG 01-fx-a-spec NOT_RUN VENUE_QUIET_UNKNOWN reason=probe-failed phase=pre-claim", l) for l in log), log)
        self.assertTrue(any(re.match(r"^\S+Z LEG 02-fx-b-spec NOT_RUN VENUE_QUIET_UNKNOWN reason=chain-stopped", l) for l in log), log)
        res = chain_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["notRun"]), (6, "VENUE_QUIET_UNKNOWN", 0, 2))
        self.assertFalse(self.claim_path.exists())

    def test_exit6_under_the_claim_and_on_unknown_continue(self) -> None:
        rd = self.case / "lane-C"
        # leg 1: pre-claim QUIET, under-claim UNKNOWN -> NOT_RUN; leg 2: QUIET, QUIET -> RAN; exit stays 6
        r = self.run_chain(rd, [self.specs[0], self.specs[1]], "-OnUnknown", "Continue", env=self.env(FAKE_QUIET_MODES="QUIET,UNKNOWN,QUIET,QUIET"))
        self.assertEqual(r.returncode, 6, r.stdout + r.stderr)
        res = chain_result(r.stdout)
        self.assertEqual([(l["status"], l["verdict"]) for l in res["legs"]], [("NOT_RUN", "VENUE_QUIET_UNKNOWN"), ("RAN", "QUIET")])
        self.assertEqual([c.split()[2] for c in self.leg_calls() if c.startswith("START")], ["fx-b.json"])
        self.assertTrue(any("LEG 01-fx-a-spec NOT_RUN VENUE_QUIET_UNKNOWN reason=probe-failed phase=under-claim" in l for l in self.chain_log(rd)))
        self.assertFalse(self.claim_path.exists(), "the claim was not released after an under-claim UNKNOWN")

    def test_unmeasured_shapes_other_than_exit6_are_not_run_either(self) -> None:
        for mode, reason in (("NOVERDICT", "exit0-without-verdict"), ("ODD", "undocumented-exit-9")):
            with self.subTest(mode=mode):
                self.leg_log.unlink(missing_ok=True)
                rd = self.case / f"lane-{mode}"
                r = self.run_chain(rd, [self.specs[0]], env=self.env(FAKE_QUIET_MODES=mode))
                self.assertEqual(r.returncode, 6, r.stdout + r.stderr)
                self.assertEqual(self.leg_calls(), [])
                self.assertTrue(any(f"NOT_RUN VENUE_QUIET_UNKNOWN reason={reason}" in l for l in self.chain_log(rd)))

    def test_mutation_exit6_not_honoured_runs_the_leg(self) -> None:
        mut = self.mutated_runner(".verdict -ceq 'VENUE_QUIET_UNKNOWN') {", ".verdict -ceq 'NEVER') {", count=2)
        rd = self.case / "lane-M6"
        self.run_chain(rd, [self.specs[0]], runner=mut, env=self.env(FAKE_QUIET_MODES="UNKNOWN"))
        self.assertTrue(any(c.startswith("START") for c in self.leg_calls()), "the mutant should have run the leg on an unmeasured venue")

    def test_gate_unreadable_stops_with_exit4(self) -> None:
        rd = self.case / "lane-G"
        r = self.run_chain(rd, [self.specs[0]], env=self.env(FAKE_QUIET_MODES="UNREADABLE"))
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertEqual(self.leg_calls(), [])

    # ---------------------------------------------------------------- COOLDOWN_UNMET + preLoad classification
    def test_cooldown_unmet_still_runs_and_each_leg_line_carries_verdict_and_preload(self) -> None:
        rd = self.case / "lane-P"
        env = self.env(FAKE_QUIET_MODES="COOLDOWN,COOLDOWN,QUIET,QUIET,QUIET,QUIET", FAKE_LEG_PRELOADS="29.6,20.2,43.9")
        r = self.run_chain(rd, self.specs, env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        res = chain_result(r.stdout)
        got = [(l["verdict"], l["preLoadMean"], l["class"]) for l in res["legs"]]
        self.assertEqual(got, [("COOLDOWN_UNMET", 29.6, "COOLDOWN_UNMET"), ("QUIET", 20.2, "QUIET_PRELOAD_OK"), ("QUIET", 43.9, "QUIET_PRELOAD_HIGH")])
        ran = [l for l in self.chain_log(rd) if " RAN " in l]
        self.assertIn("verdict=COOLDOWN_UNMET quietMean=32.0 preLoadMean=29.6 class=COOLDOWN_UNMET", ran[0])

    # ---------------------------------------------------------------- r2: a leg that exits non-zero is never a successful leg
    def test_a_leg_that_exits_nonzero_is_leg_failed_invalid_never_ran(self) -> None:
        rd = self.case / "lane-F"
        r = self.run_chain(rd, self.specs, env=self.env(FAKE_LEG_FAIL_LEAF="fx-b.json", FAKE_LEG_FAIL_CODE="3"))
        self.assertEqual(r.returncode, 7, r.stdout + r.stderr)
        res = chain_result(r.stdout)
        self.assertEqual(res["exitCode"], 7)
        self.assertEqual([l["status"] for l in res["legs"]], ["RAN", "LEG_FAILED", "RAN"])
        bad = res["legs"][1]
        self.assertEqual((bad["outcome"], bad["printedOutcome"], bad["legExit"], bad["receipt"]), ("INVALID", "PASS", 3, None))
        self.assertEqual((res["ran"], res["failed"], res["notRun"]), (2, 1, 0))
        log = self.chain_log(rd)
        self.assertEqual(len([l for l in log if " LEG_FAILED exit=3 outcome=INVALID printedOutcome=PASS " in l]), 1, log)
        self.assertFalse([l for l in log if " RAN " in l and "02-fx-b" in l], "a leg that exited non-zero must never get a RAN line")
        self.assertEqual(len([l for l in self.leg_calls() if l.startswith("START")]), 3, "the chain goes on to the next leg")

    def test_exit_zero_legs_are_unchanged_by_the_leg_exit_rule(self) -> None:
        r = self.run_chain(self.case / "lane-F0", self.specs, env=self.env(FAKE_LEG_FAIL_LEAF="no-such-leaf.json", FAKE_LEG_FAIL_CODE="3"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        res = chain_result(r.stdout)
        self.assertEqual(([l["status"] for l in res["legs"]], res["failed"]), (["RAN", "RAN", "RAN"], 0))

    def test_a_failed_leg_does_not_mask_exit_6(self) -> None:
        env = self.env(FAKE_QUIET_MODES="UNKNOWN,QUIET,QUIET", FAKE_LEG_FAIL_LEAF="fx-b.json", FAKE_LEG_FAIL_CODE="2")
        r = self.run_chain(self.case / "lane-F6", self.specs[:2], "-OnUnknown", "Continue", env=env)
        self.assertEqual(r.returncode, 6, r.stdout + r.stderr)
        self.assertEqual([l["status"] for l in chain_result(r.stdout)["legs"]], ["NOT_RUN", "LEG_FAILED"])

    def test_mutation_leg_exit_code_ignored_records_the_dead_leg_as_ran(self) -> None:
        runner = self.mutated_runner("if ($legCode -ne 0) {", "if ($false) {", count=1)
        r = self.run_chain(self.case / "lane-FM", self.specs[:2], runner=runner, env=self.env(FAKE_LEG_FAIL_LEAF="fx-b.json", FAKE_LEG_FAIL_CODE="3"))
        res = chain_result(r.stdout)
        self.assertEqual(r.returncode, 0, "the mutant must reproduce the defect: a dead leg and a clean chain exit")
        self.assertEqual([l["status"] for l in res["legs"]], ["RAN", "RAN"])
        self.assertEqual((res["legs"][1]["outcome"], res["legs"][1]["legExit"]), ("PASS", 3))

    # ---------------------------------------------------------------- the claim
    def _stale_case(self, reason: str, pid: int, created: str, expires: datetime) -> None:
        fixture = self.write_claim(pid, created, expires)
        rd = self.case / f"lane-S-{reason}"
        r = self.run_chain(rd, [self.specs[0]])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        broken = [l for l in self.chain_log(rd) if " CLAIM_STALE_BROKEN " in l]
        self.assertEqual(len(broken), 1, broken)
        self.assertIn(f"reason={reason} ownerPid={pid}", broken[0])
        self.assertEqual(chain_result(r.stdout)["legs"][0]["status"], "RAN")
        self.assertFalse(self.claim_path.exists() and self.claim_path.read_text(encoding="utf-8") == fixture)

    def test_stale_claim_dead_pid_is_broken_once(self) -> None:
        p = subprocess.Popen(["cmd", "/c", "exit", "0"]); p.wait()
        self._stale_case("dead-pid", p.pid, "2026-01-01T00:00:00Z", datetime.now(timezone.utc) + timedelta(hours=3))

    def test_stale_claim_reused_pid_is_broken_once(self) -> None:
        created = datetime.fromisoformat(process_start_iso(os.getpid()).replace("Z", "+00:00")[:26] + "+00:00") - timedelta(minutes=10)
        self._stale_case("pid-reused", os.getpid(), utc_iso(created), datetime.now(timezone.utc) + timedelta(hours=3))

    def test_stale_claim_expired_is_broken_once(self) -> None:
        self._stale_case("expired", os.getpid(), process_start_iso(os.getpid()), datetime.now(timezone.utc) - timedelta(minutes=1))

    def test_live_claim_blocks_until_the_deadline_and_is_not_broken(self) -> None:
        fixture = self.write_claim(os.getpid(), process_start_iso(os.getpid()), datetime.now(timezone.utc) + timedelta(hours=3))
        rd = self.case / "lane-L"
        r = self.run_chain(rd, [self.specs[0]], deadline_sec=8)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertEqual(self.leg_calls(), [])
        log = self.chain_log(rd)
        self.assertTrue(any(f"CLAIM_HELD leg=01-fx-a-spec state=LIVE holder pid={os.getpid()}" in l for l in log), log)
        self.assertTrue(any("LEG 01-fx-a-spec NOT_RUN DEADLINE reason=claim-held" in l for l in log), log)
        self.assertFalse(any("CLAIM_STALE_BROKEN" in l for l in log))
        self.assertEqual(self.claim_path.read_text(encoding="utf-8"), fixture, "a live claim was touched")

    def test_live_claim_released_by_its_owner_lets_the_waiter_run(self) -> None:
        self.write_claim(os.getpid(), process_start_iso(os.getpid()), datetime.now(timezone.utc) + timedelta(hours=3))
        rd = self.case / "lane-R"
        p = subprocess.Popen(self.chain_args(rd, [self.specs[0]], deadline_sec=90), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=self.env())
        t0 = time.time()
        while time.time() - t0 < 60 and not ((rd / "chain.log").exists() and "CLAIM_HELD" in (rd / "chain.log").read_text(encoding="utf-8")):
            time.sleep(0.25)
        self.claim_path.unlink()   # the owner releases
        out, _ = p.communicate(timeout=120)
        self.assertEqual(p.returncode, 0, out)
        self.assertEqual(chain_result(out)["legs"][0]["status"], "RAN")

    def test_claim_module_is_atomic_and_release_checks_the_nonce(self) -> None:
        d = self.case / "mod"
        out = pwsh_text(
            f"Import-Module '{MODULE}' -Force -DisableNameChecking; $d='{d}'; "
            "$a = Request-VenueClaim -Venue bachelor -RunDir 'A' -ClaimDir $d; $b = Request-VenueClaim -Venue bachelor -RunDir 'B' -ClaimDir $d; "
            "\"$($a.granted) $($b.granted) $($b.state) $((Get-VenueClaim -Venue bachelor -ClaimDir $d).state) "
            "$(Release-VenueClaim -Venue bachelor -Nonce 'other' -ClaimDir $d) $(Release-VenueClaim -Venue bachelor -Nonce $a.claim.nonce -ClaimDir $d) "
            "$((Get-VenueClaim -Venue bachelor -ClaimDir $d).state)\"")
        self.assertEqual(out, "True False LIVE LIVE NOT_OURS RELEASED FREE")

    # ---------------------------------------------------------------- QUIET-rate table and windows
    GATE_A = "\n".join([
        "GATE quiet-probe bachelor queued=0 running=0 07:00:01Z",
        "PROBE venue-quiet-probe-20261008T070012345Z bachelor samples=10.0/10.0/10.0 mean=10.0% state=QUIET 07:00:50Z",
        "PROBE venue-quiet-probe-20261008T071012345Z bachelor samples=30.0/30.0/30.0 mean=30.0% state=BUSY 07:10:50Z",
        "PROBE venue-quiet-probe-20261008T072012345Z bachelor samples=12.0/12.0/12.0 mean=12.0% state=QUIET 07:20:50Z",
        "PROBE venue-quiet-probe-20261009T070512345Z bachelor samples=40.0/40.0/40.0 mean=40.0% state=BUSY 07:05:50Z",
        "COOLDOWN_UNMET mean=40.0% threshold=20% waitedSec=600",
        "PROBE venue-quiet-probe-20261008T090012345Z bachelor samples=50.0/50.0/50.0 mean=50.0% state=BUSY 09:00:50Z",
        "PROBE venue-quiet-probe-20261008T090512345Z bachelor samples=null/50.0/50.0 mean=UNKNOWN state=UNKNOWN 09:05:50Z",
        "PROBE venue-quiet-probe-20261008T091012345Z ultra-magnus samples=1.0/1.0/1.0 mean=1.0% state=QUIET 09:10:50Z",
    ]) + "\n"
    GATE_B = "\n".join([
        "PROBE venue-quiet-probe-20261008T070012345Z bachelor samples=10.0/10.0/10.0 mean=10.0% state=QUIET 07:00:50Z",   # the same job id: counted once
        "PROBE venue-quiet-probe-20261008T090812345Z bachelor samples=15.0/15.0/15.0 mean=15.0% state=QUIET 09:08:50Z",
        "PROBE venue-quiet-probe-20261008T091512345Z bachelor samples=45.0/45.0/45.0 mean=45.0% state=BUSY 09:15:50Z",
        "PROBE venue-quiet-probe-20261008T232012345Z bachelor samples=45.0/45.0/45.0 mean=45.0% state=BUSY 23:20:50Z",
        "QUIET mean=15.0% threshold=20% waitedSec=10",
    ]) + "\n"

    def table(self, now: str, *extra: str) -> tuple[dict, dict]:
        a, b = self.case / "a-gate.log", self.case / "b-gate.log"
        a.write_text(self.GATE_A, encoding="utf-8"); b.write_text(self.GATE_B, encoding="utf-8")
        r = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(RUNNER), "-QuietRateTable", "-Venue", "bachelor",
                            "-GateLogPath", f"{a},{b}", "-NowUtc", now, *extra], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rows = {}
        for m in re.finditer(r"^QUIET_RATE hour=(\d\d)Z probes=(\d+) quiet=(\d+) unknown=(\d+) rate=(\S+) days=(\d+)$", r.stdout, re.M):
            rows[int(m.group(1))] = (int(m.group(2)), int(m.group(3)), int(m.group(4)), m.group(5), int(m.group(6)))
        choice = json.loads(re.search(r"^WINDOW_CHOICE=(.+)$", r.stdout, re.M).group(1))
        return rows, choice

    def test_quiet_rate_table_matches_hand_computed_rates(self) -> None:
        rows, _ = self.table("2026-10-09T05:30:00Z")
        # hand-computed: 07Z = QUIET, BUSY, QUIET (10-08) + BUSY (10-09) -> 2/4 = 50.0 %, 2 days; 09Z = BUSY, QUIET, BUSY measured + 1 UNKNOWN
        # (ultra-magnus ignored) -> 1/3 = 33.3 %; 23Z = 0/1. The duplicate job id in the second log is counted once.
        self.assertEqual(rows, {7: (4, 2, 0, "50.0%", 2), 9: (3, 1, 1, "33.3%", 1), 23: (1, 0, 0, "0.0%", 1)})

    def test_prefer_quiet_windows_picks_the_right_hour(self) -> None:
        # 23Z has 1 probe (< -MinWindowProbes 3): unmeasured, run now
        _, c = self.table("2026-10-09T23:10:00Z")
        self.assertEqual((c["action"], c["reason"]), ("RUN_NOW", "current-hour-unmeasured"))
        # with -MinWindowProbes 1, 23Z is 0 %: the nearest hour at >= 30 % within 6 h is 07Z? no: 23+6 = 05Z, so nothing in the horizon
        _, c = self.table("2026-10-09T23:10:00Z", "-MinWindowProbes", "1")
        self.assertEqual((c["action"], c["reason"]), ("RUN_NOW", "no-window-in-horizon"))
        # ... and with a 9 h horizon it waits for 07Z TOMORROW (wraps midnight), skipping nothing better before it
        _, c = self.table("2026-10-09T23:10:00Z", "-MinWindowProbes", "1", "-WindowHorizonHours", "9")
        self.assertEqual((c["action"], c["targetHour"], c["targetRate"]), ("WAIT", 7, 50.0))
        self.assertTrue(c["targetUtc"].startswith("2026-10-10T07:00:00"), c)
        self.assertEqual(c["waitSec"], 7 * 3600 + 50 * 60)
        # at 07Z the current hour is 50 %: run now
        _, c = self.table("2026-10-09T07:40:00Z")
        self.assertEqual((c["action"], c["reason"]), ("RUN_NOW", "current-rate-ok"))
        # raise the bar to 40 %: 09Z (33.3 %) is below it too, and nothing later qualifies
        _, c = self.table("2026-10-09T09:20:00Z", "-PreferRatePercent", "40")
        self.assertEqual((c["action"], c["reason"]), ("RUN_NOW", "no-window-in-horizon"))
        # at 09Z with a 22 h horizon and a 40 % bar, the next qualifying hour is 07Z the next day
        _, c = self.table("2026-10-09T09:20:00Z", "-PreferRatePercent", "40", "-WindowHorizonHours", "22")
        self.assertEqual((c["action"], c["targetHour"]), ("WAIT", 7))

    def test_window_choice_respects_the_deadline(self) -> None:
        out = pwsh_text(
            f"Import-Module '{MODULE}' -Force -DisableNameChecking; "
            "$t = @([pscustomobject]@{hourUtc=5;probes=9;quiet=0;unknown=0;ratePercent=0.0;days=1}, [pscustomobject]@{hourUtc=7;probes=9;quiet=5;unknown=0;ratePercent=55.6;days=1}); "
            "$now=[DateTime]::new(2026,10,9,5,30,0,[DateTimeKind]::Utc); "
            "$a = Select-VenueQuietWindow -Table $t -NowUtc $now; $b = Select-VenueQuietWindow -Table $t -NowUtc $now -DeadlineUtc $now.AddMinutes(60); "
            "\"$($a.action) $($a.targetHour) $($a.waitSec) | $($b.action) $($b.reason)\"")
        self.assertEqual(out, "WAIT 7 5400 | RUN_NOW window-after-deadline")

    def test_chain_with_prefer_quiet_windows_logs_its_choice_and_never_waits_past_the_deadline(self) -> None:
        now = datetime.now(timezone.utc)
        if now.minute == 59 and now.second > 30:
            self.skipTest("too close to the top of the hour for a deterministic window")
        cur = now.strftime("%Y%m%dT%H")
        nxt = (now + timedelta(hours=1)).strftime("%Y%m%dT%H")
        log = self.case / "w-gate.log"
        log.write_text("".join(f"PROBE venue-quiet-probe-{cur}{i:02d}00000Z bachelor samples=40/40/40 mean=40.0% state=BUSY x\n" for i in range(4)) +
                       "".join(f"PROBE venue-quiet-probe-{nxt}{i:02d}00000Z bachelor samples=5/5/5 mean=5.0% state=QUIET x\n" for i in range(4)), encoding="utf-8")
        rd = self.case / "lane-WIN"
        r = self.run_chain(rd, [self.specs[0]], "-PreferQuietWindows", "-GateLogPath", str(log), deadline_sec=20)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        win = [l for l in self.chain_log(rd) if " WINDOW " in l]
        self.assertTrue(win and "action=RUN_NOW reason=window-after-deadline" in win[0], win)

    # ---------------------------------------------------------------- r1: VENUE-CHAIN-UNHEALTHY-STREAK-STOP-1 (+ folded VENUE-CHAIN-PROBE-SCRIPT-MISSING-NAMED-1)
    K = "VENUE_UNHEALTHY:KEEPALIVE_FAILED"

    def streak_chain(self, name: str, outcomes: list[str], *extra: str, specs: list[Path] | None = None, **env: str) -> tuple[subprocess.CompletedProcess, dict, list[str], Path]:
        rd = self.case / name
        legs = specs or self.specs
        reps = ["-Repeats", str(max(1, -(-len(outcomes) // len(legs))))] if len(outcomes) > len(legs) else []
        r = self.run_chain(rd, legs, *reps, *extra, env=self.env(FAKE_LEG_OUTCOMES=",".join(outcomes), **env))
        return r, chain_result(r.stdout), self.chain_log(rd), rd

    def started(self) -> int:
        return len([c for c in self.leg_calls() if c.startswith("START")])

    def test_three_identical_unhealthy_details_stop_the_chain_after_leg_3_with_exit_8(self) -> None:
        r, res, log, _ = self.streak_chain("lane-K3", [self.K] * 6)
        self.assertEqual(r.returncode, 8, r.stdout + r.stderr)
        self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["notRun"]), (8, "VENUE_UNHEALTHY_STREAK", 3, 3))
        self.assertEqual([l["status"] for l in res["legs"]], ["RAN"] * 3 + ["NOT_RUN"] * 3)
        self.assertEqual([l["outcomeDetail"] for l in res["legs"][:3]], ["KEEPALIVE_FAILED"] * 3)
        self.assertTrue(all((l["verdict"], l["reason"]) == ("VENUE_UNHEALTHY_STREAK", "chain-stopped") for l in res["legs"][3:]), res["legs"])
        self.assertEqual(self.started(), 3, "a leg ran after the streak stop")
        self.assertTrue(any(re.search(r" LEG 01-fx-a-spec RAN .* detail=KEEPALIVE_FAILED$", l) for l in log), log)
        stop = [l for l in log if " CHAIN_STOP " in l]
        self.assertEqual(len(stop), 1, stop)
        self.assertRegex(stop[0], r"CHAIN_STOP VENUE_UNHEALTHY_STREAK detail=KEEPALIVE_FAILED n=3 threshold=3 receipts=rcpt-1,rcpt-2,rcpt-3$")
        for n in (4, 5, 6):
            self.assertTrue(any(re.search(rf" LEG 0{n}-fx-\w-spec NOT_RUN VENUE_UNHEALTHY_STREAK reason=chain-stopped$", l) for l in log), log)
        self.assertFalse(self.claim_path.exists())

    def test_the_streak_threshold_is_the_parameter(self) -> None:
        r, res, log, _ = self.streak_chain("lane-K2", [self.K] * 4, "-UnhealthyStreakStop", "2")
        self.assertEqual((r.returncode, [l["status"] for l in res["legs"]]), (8, ["RAN", "RAN"] + ["NOT_RUN"] * 4), r.stdout)
        self.assertEqual(self.started(), 2)
        self.assertTrue(any("CHAIN_STOP VENUE_UNHEALTHY_STREAK detail=KEEPALIVE_FAILED n=2 threshold=2 receipts=rcpt-1,rcpt-2" in l for l in log), log)

    def test_alternating_details_never_stop(self) -> None:
        d = ["VENUE_UNHEALTHY:KEEPALIVE_FAILED", "VENUE_UNHEALTHY:DISPLAY_ASLEEP"] * 3
        r, res, log, _ = self.streak_chain("lane-ALT", d)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(([l["status"] for l in res["legs"]], res["stopReason"]), (["RAN"] * 6, None))
        self.assertEqual([l["outcomeDetail"] for l in res["legs"]], ["KEEPALIVE_FAILED", "DISPLAY_ASLEEP"] * 3)
        self.assertFalse([l for l in log if " CHAIN_STOP " in l])

    def test_a_different_detail_restarts_the_count_at_one(self) -> None:
        d = ["VENUE_UNHEALTHY:KEEPALIVE_FAILED"] * 2 + ["VENUE_UNHEALTHY:DISPLAY_ASLEEP"] * 4
        r, res, log, _ = self.streak_chain("lane-RST", d)
        self.assertEqual(r.returncode, 8, r.stdout + r.stderr)
        self.assertEqual(self.started(), 5)   # 2 x K, then 3 x DISPLAY_ASLEEP
        self.assertTrue(any("detail=DISPLAY_ASLEEP n=3 threshold=3 receipts=rcpt-3,rcpt-4,rcpt-5" in l for l in log), log)

    def test_an_intervening_pass_resets_the_count(self) -> None:
        r, res, log, _ = self.streak_chain("lane-PASS", [self.K, self.K, "PASS", self.K, self.K, "PASS"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([l["status"] for l in res["legs"]], ["RAN"] * 6)
        self.assertEqual(res["stopReason"], None)
        self.assertEqual([l.get("outcomeDetail") for l in res["legs"]], ["KEEPALIVE_FAILED", "KEEPALIVE_FAILED", None, "KEEPALIVE_FAILED", "KEEPALIVE_FAILED", None])
        self.assertFalse([l for l in log if " CHAIN_STOP " in l])

    def test_owner_only_details_stop_at_the_first_occurrence_whatever_the_threshold(self) -> None:
        for detail in ("SCREENSAVER_SECURE_OWNER_ONLY", "SESSION_LOCKED", "SESSION_LOCKED_OWNER_ONLY"):
            for extra in ((), ("-UnhealthyStreakStop", "50")):
                with self.subTest(detail=detail, extra=extra):
                    self.leg_log.unlink(missing_ok=True)
                    (self.case / "fake-leg-outcome-count.txt").unlink(missing_ok=True)
                    r, res, log, _ = self.streak_chain(f"lane-O-{detail}-{len(extra)}", [f"VENUE_UNHEALTHY:{detail}"] * 3, *extra)
                    self.assertEqual(r.returncode, 8, r.stdout + r.stderr)
                    self.assertEqual(([l["status"] for l in res["legs"]], self.started()), (["RAN", "NOT_RUN", "NOT_RUN"], 1))
                    stop = [l for l in log if " CHAIN_STOP " in l]
                    self.assertEqual(len(stop), 1, stop)
                    self.assertIn(f"CHAIN_STOP VENUE_UNHEALTHY_STREAK detail={detail} n=1 ", stop[0])
                    self.assertIn("receipts=rcpt-1 ownerOnly=1", stop[0])

    def test_unhealthy_streak_stop_0_never_stops(self) -> None:
        r, res, log, _ = self.streak_chain("lane-OFF", [self.K] * 6, "-UnhealthyStreakStop", "0")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(([l["status"] for l in res["legs"]], res["stopReason"], self.started()), (["RAN"] * 6, None, 6))
        self.assertFalse([l for l in log if " CHAIN_STOP " in l])
        # r1 of VENUE-CHAIN-STREAK-DETAIL-NORMALISE-1: 0 switches off the COUNT rule only; the owner-only first-occurrence stop still applies
        # (test_owner_only_first_occurrence_stops_even_with_the_count_rule_off)

    # ---------------------------------------------------------------- r1: VENUE-CHAIN-STREAK-DETAIL-NORMALISE-1
    DISK = "VENUE_UNHEALTHY:leg not submitted: freeDiskGiB {} < 50"
    COLD = "VENUE_UNHEALTHY:leg not submitted: pwshColdStartMs {} > 800"
    DISK_KEY = "leg not submitted: freeDiskGiB # < #"

    def reset_legs(self) -> None:
        self.leg_log.unlink(missing_ok=True)
        (self.case / "fake-leg-outcome-count.txt").unlink(missing_ok=True)

    def test_probe_details_that_differ_only_in_a_measured_number_form_one_streak(self) -> None:
        d = [self.DISK.format(v) for v in ("42.4", "41.9", "41.7")] + [self.DISK.format("41.5")] * 3
        r, res, log, _ = self.streak_chain("lane-NUM", d)
        self.assertEqual(r.returncode, 8, r.stdout + r.stderr)
        self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["notRun"]), (8, "VENUE_UNHEALTHY_STREAK", 3, 3))
        self.assertEqual(self.started(), 3, "a leg ran after the streak stop")
        # the legs and the stop keep the newest RAW detail, plus the key
        self.assertEqual([l["outcomeDetail"] for l in res["legs"][:3]], [self.DISK.split(":", 1)[1].format(v) for v in ("42.4", "41.9", "41.7")])
        stop = [l for l in log if " CHAIN_STOP " in l]
        self.assertEqual(len(stop), 1, stop)
        self.assertIn("detail=leg not submitted: freeDiskGiB 41.7 < 50 n=3 threshold=3 receipts=rcpt-1,rcpt-2,rcpt-3", stop[0])
        self.assertTrue(stop[0].endswith(f" key=[{self.DISK_KEY}]"), stop[0])
        us = res["unhealthyStop"]
        self.assertEqual((us["detail"], us["key"], us["n"], us["threshold"], us["ownerOnly"]), ("leg not submitted: freeDiskGiB 41.7 < 50", self.DISK_KEY, 3, 3, False))

    def test_two_different_probe_reasons_never_stop(self) -> None:
        alt = [self.DISK.format("42.4"), self.COLD.format("1234")] * 3
        r, res, log, _ = self.streak_chain("lane-REASONS", alt)
        self.assertEqual((r.returncode, [l["status"] for l in res["legs"]], res["stopReason"]), (0, ["RAN"] * 6, None), r.stdout + r.stderr)
        self.assertFalse([l for l in log if " CHAIN_STOP " in l])
        # two of one reason then another: the count restarts at 1, never reaching 3 (3 outcomes = the 3 specs; a longer list would repeat its last item)
        self.reset_legs()
        r, res, log, _ = self.streak_chain("lane-REASONS2", [self.DISK.format("42.4"), self.DISK.format("41.0"), self.COLD.format("1234")])
        self.assertEqual((r.returncode, res["stopReason"], self.started()), (0, None, 3), r.stdout + r.stderr)

    def test_the_key_rule_on_the_shapes_the_emitters_print(self) -> None:
        # (details that must share a key) -> a streak of 3 at default N; (details that must not) -> no stop
        same = {
            "timeout-with-number": ["health probe ended TIMEOUT: waited 120 s", "health probe ended TIMEOUT: waited 118 s", "health probe ended TIMEOUT: waited 7 s"],
            "decimal-and-signed": ["leg not submitted: commit used 12.5 of 32 GiB exceeds fraction 0.3", "leg not submitted: commit used -1 of 32.25 GiB exceeds fraction 0.3",
                                   "leg not submitted: commit used 1e3 of 31 GiB exceeds fraction 0.3"],
            "reason-token-then-text": ["KEEPALIVE_FAILED: attempt 1", "KEEPALIVE_FAILED: attempt 2", "KEEPALIVE_FAILED"],
        }
        for name, details in same.items():
            with self.subTest(shape=name):
                self.reset_legs()
                r, res, _, _ = self.streak_chain(f"lane-S-{name}", [f"VENUE_UNHEALTHY:{x}" for x in details] * 2)
                self.assertEqual((r.returncode, res["ran"]), (8, 3), r.stdout + r.stderr)
        self.reset_legs()
        # the same words with a different non-numeric part are different faults, whatever the numbers
        r, res, _, _ = self.streak_chain("lane-S-diff", [f"VENUE_UNHEALTHY:{x}" for x in (
            "leg not submitted: freeDiskGiB 42.4 < 50", "leg not submitted: smallHashMs 42.4 > 50", "leg not submitted: freeDiskGiB unknown")] * 2)
        self.assertEqual((r.returncode, res["stopReason"]), (0, None), r.stdout + r.stderr)

    def test_owner_only_first_occurrence_stops_even_with_the_count_rule_off(self) -> None:
        for detail in ("SCREENSAVER_SECURE_OWNER_ONLY", "SESSION_LOCKED", "SESSION_LOCKED_OWNER_ONLY"):
            with self.subTest(detail=detail):
                self.reset_legs()
                r, res, log, _ = self.streak_chain(f"lane-O0-{detail}", [f"VENUE_UNHEALTHY:{detail}"] * 3, "-UnhealthyStreakStop", "0")
                self.assertEqual(r.returncode, 8, r.stdout + r.stderr)
                self.assertEqual(([l["status"] for l in res["legs"]], self.started(), res["stopReason"]), (["RAN", "NOT_RUN", "NOT_RUN"], 1, "VENUE_UNHEALTHY_STREAK"))
                stop = [l for l in log if " CHAIN_STOP " in l]
                self.assertEqual(len(stop), 1, stop)
                self.assertIn(f"detail={detail} n=1 threshold=0 receipts=rcpt-1 ownerOnly=1", stop[0])
        # owner-only matching stays exact on the token: a different token that merely starts with one is not owner-only
        self.reset_legs()
        r, res, _, _ = self.streak_chain("lane-O0-near", ["VENUE_UNHEALTHY:SESSION_LOCKED_SOON"] * 3, "-UnhealthyStreakStop", "0")
        self.assertEqual((r.returncode, [l["status"] for l in res["legs"]]), (0, ["RAN"] * 3), r.stdout)

    def test_count_rule_off_with_numeric_only_differences_does_not_stop(self) -> None:
        d = [self.DISK.format(v) for v in ("42.4", "41.9", "41.7", "41.5", "41.1", "40.9")]
        r, res, log, _ = self.streak_chain("lane-NUM0", d, "-UnhealthyStreakStop", "0")
        self.assertEqual((r.returncode, [l["status"] for l in res["legs"]], res["stopReason"], self.started()), (0, ["RAN"] * 6, None, 6), r.stdout + r.stderr)
        self.assertNotIn("unhealthyStop", res)
        self.assertFalse([l for l in log if " CHAIN_STOP " in l])

    def test_owner_only_list_is_pinned_to_the_tokens_the_emitters_print(self) -> None:
        runner = RUNNER.read_text(encoding="utf-8")
        m = re.search(r"\$ownerOnlyDetails = @\(([^)]*)\)", runner)
        self.assertTrue(m, "the runner's $ownerOnlyDetails list is gone")
        listed = set(re.findall(r"'([A-Z0-9_]+)'", m.group(1)))
        # the emitters' own source: the VENUE_UNHEALTHY result tokens (DualVenueRunner.psm1 Resolve-DvJobOutcome via $script:VenueConditionResults) and the job /
        # artifact module that print them. An owner-only token is one the emitters name *_OWNER_ONLY.
        dv = (DV / "DualVenueRunner.psm1").read_text(encoding="utf-8")
        cond = re.search(r"\$script:VenueConditionResults = @\(([^)]*)\)", dv)
        self.assertTrue(cond, "DualVenueRunner.psm1 no longer defines $script:VenueConditionResults")
        emitted = set(re.findall(r"'([A-Z0-9_]+)'", cond.group(1)))
        job = (ROOT / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-job.ps1").read_text(encoding="utf-8")
        art = (ROOT / "tools" / "profiling" / "bachelor" / "AttrCudaArtifacts.psm1").read_text(encoding="utf-8")
        printed = set(re.findall(r"RESULT=([A-Z0-9_]+OWNER_ONLY)\b", job))
        self.assertTrue(printed, "the job no longer prints an *_OWNER_ONLY RESULT token")
        owner_emitted = {t for t in emitted | printed if t.endswith("OWNER_ONLY")}
        self.assertIn("SCREENSAVER_SECURE_OWNER_ONLY", owner_emitted)
        self.assertTrue(printed <= emitted, f"the job prints {printed - emitted}, which Resolve-DvJobOutcome does not map to VENUE_UNHEALTHY")
        self.assertTrue(owner_emitted <= listed, f"emitted owner-only tokens missing from the chain's $ownerOnlyDetails: {owner_emitted - listed}")
        # Every listed token either has an emitter in the source above, or is OWED to a peer change not yet on master. SESSION_LOCKED /
        # SESSION_LOCKED_OWNER_ONLY are owed to VENUE-SESSION-LOCKED-REFUSAL-1 (PR #345). Once #345 merges its emitter appears in the sources and this set shrinks to empty.
        owed = {"SESSION_LOCKED", "SESSION_LOCKED_OWNER_ONLY"}
        sources = dv + job + art
        unemitted = {t for t in listed if t not in sources}
        self.assertTrue(unemitted <= owed, f"owner-only tokens listed in the chain but printed by no emitter: {unemitted - owed}")

    def test_leg_failed_and_not_run_legs_neither_count_nor_reset(self) -> None:
        extra_spec = self.case / "fx-d.json"
        extra_spec.write_text(json.dumps({"legId": "fx-d"}), encoding="utf-8")
        four = [*self.specs, extra_spec]
        # K, K, [fx-c exits 3: LEG_FAILED], K -> the third K is the third in a row, so the chain stops at leg 4 and exit 8 replaces 7
        r, res, log, _ = self.streak_chain("lane-LF", [self.K, self.K, "PASS", self.K], specs=four, FAKE_LEG_FAIL_LEAF="fx-c.json", FAKE_LEG_FAIL_CODE="3")
        self.assertEqual(r.returncode, 8, r.stdout + r.stderr)
        self.assertEqual([l["status"] for l in res["legs"]], ["RAN", "RAN", "LEG_FAILED", "RAN"])
        self.assertEqual(res["stopReason"], "VENUE_UNHEALTHY_STREAK")
        self.assertTrue(any("detail=KEEPALIVE_FAILED n=3 threshold=3 receipts=rcpt-1,rcpt-2,rcpt-4" in l for l in log), log)
        # K, K, [NOT_RUN: the venue was not measured, -OnUnknown Continue], K -> stops at leg 4; the higher-priority exit 6 is kept
        self.leg_log.unlink(missing_ok=True)
        (self.case / "fake-leg-outcome-count.txt").unlink(missing_ok=True)
        r, res, log, _ = self.streak_chain("lane-NR", [self.K], "-OnUnknown", "Continue", specs=four, FAKE_QUIET_MODES="QUIET,QUIET,QUIET,QUIET,UNKNOWN,QUIET,QUIET")
        self.assertEqual(r.returncode, 6, r.stdout + r.stderr)
        self.assertEqual([l["status"] for l in res["legs"]], ["RAN", "RAN", "NOT_RUN", "RAN"])
        self.assertEqual((res["exitCode"], res["stopReason"]), (6, "VENUE_UNHEALTHY_STREAK"))
        self.assertTrue(any("n=3 threshold=3 receipts=rcpt-1,rcpt-2,rcpt-3" in l for l in log), log)

    def test_unhealthy_without_a_detail_is_not_comparable_and_resets(self) -> None:
        r, res, _, _ = self.streak_chain("lane-ND", ["VENUE_UNHEALTHY"] * 4, specs=[*self.specs, self.specs[0]])
        self.assertEqual((r.returncode, [l["status"] for l in res["legs"]]), (0, ["RAN"] * 4), r.stdout)
        self.assertTrue(all("outcomeDetail" not in l for l in res["legs"]))

    def test_chains_with_no_unhealthy_legs_are_unchanged(self) -> None:
        rd = self.case / "lane-PLAIN"
        r = self.run_chain(rd, self.specs)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        res = chain_result(r.stdout)
        self.assertEqual(([l["status"] for l in res["legs"]], res["stopReason"]), (["RAN"] * 3, None))
        self.assertTrue(all("outcomeDetail" not in l for l in res["legs"]))
        log = self.chain_log(rd)
        self.assertFalse([l for l in log if " CHAIN_STOP " in l or " detail=" in l])

    def test_mutation_streak_threshold_off_by_one_is_caught(self) -> None:
        mut = self.mutated_runner("$streakIds.Count -ge $UnhealthyStreakStop", "$streakIds.Count -gt $UnhealthyStreakStop", count=1)
        r = self.run_chain(self.case / "lane-KM", self.specs, env=self.env(FAKE_LEG_OUTCOMES=self.K), runner=mut)
        self.assertEqual(r.returncode, 0, "the mutant must reproduce the defect: three identical unhealthy legs and no stop")
        self.assertEqual(self.started(), 3)

    def test_probe_script_vanishing_under_a_live_chain_is_named_not_undocumented_exit_64(self) -> None:
        quiet = self.case / "vanishing-quiet.ps1"
        shutil.copy2(self.fake_quiet, quiet)
        rd = self.case / "lane-GONE"
        # leg 1 runs, and deletes the probe script as it exits (the worktree was removed under the chain); leg 2's probe cannot launch
        r = self.run_chain(rd, self.specs, quiet=quiet, env=self.env(FAKE_LEG_DELETE_PATH=str(quiet)))
        self.assertEqual(r.returncode, 6, r.stdout + r.stderr)
        res = chain_result(r.stdout)
        self.assertEqual([l["status"] for l in res["legs"]], ["RAN", "NOT_RUN", "NOT_RUN"])
        self.assertEqual((res["legs"][1]["verdict"], res["legs"][1]["reason"]), ("VENUE_QUIET_UNKNOWN", "PROBE_SCRIPT_MISSING"))
        self.assertEqual(res["stopReason"], "VENUE_QUIET_UNKNOWN")
        log = self.chain_log(rd)
        self.assertTrue(any(re.search(r"LEG 02-fx-b-spec NOT_RUN VENUE_QUIET_UNKNOWN reason=PROBE_SCRIPT_MISSING phase=pre-claim", l) for l in log), log)
        self.assertFalse([l for l in log if "undocumented-exit" in l], log)
        self.assertEqual(self.started(), 1, "no leg and no retry after the probe script vanished")

    # ---------------------------------------------------------------- the contract stays in the file
    def test_runner_has_no_process_or_lane_scan_and_documents_its_exit_codes(self) -> None:
        text = RUNNER.read_text(encoding="utf-8")
        mod = MODULE.read_text(encoding="utf-8")
        for needle in ("Get-CimInstance", "Win32_Process", "receipt.json", "prompt.md"):
            self.assertNotIn(needle, text + mod, f"{needle}: a waiter must never treat another chain's WAITING as venue use; only a live claim blocks")
        header = "\n".join(l for l in text.splitlines() if l.startswith("#"))
        for token in ("0 every leg ran", "2 usage", "3 DEADLINE", "4 GATE_UNREADABLE", "5 HOST_MISMATCH", "6 at least one leg was NOT_RUN VENUE_QUIET_UNKNOWN", "7 (r2) at least one leg process exited", "8 (r1, VENUE-CHAIN-UNHEALTHY-STREAK-STOP-1)", "CHAIN_RESULT=<json>"):
            self.assertIn(token, header)
        self.assertIn("[ValidateRange(0, 50)][int]$UnhealthyStreakStop = 3", text)
        self.assertIn("IO.FileMode]::CreateNew", mod)

    def test_new_powershell_files_parse_and_are_ascii(self) -> None:
        for f in (RUNNER, MODULE):
            self.assertTrue(all(b < 128 for b in f.read_bytes()), f"{f.name} is not ASCII")
            out = pwsh_text(f"$t=$null; $e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile('{f}', [ref]$t, [ref]$e); $e.Count")
            self.assertEqual(out, "0", f.name)


if __name__ == "__main__":
    unittest.main()
