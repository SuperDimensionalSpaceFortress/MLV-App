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

from tools.repo_hygiene.venue_fixture_modules import copy_with_siblings, sibling_modules

ROOT = Path(__file__).resolve().parents[2]
DV = ROOT / "tools" / "profiling" / "dual-venue"
RUNNER =DV / "Invoke-VenueChain.ps1"
MODULE = DV / "VenueChain.psm1"
PWSH = shutil.which("pwsh")
requires_windows_pwsh = unittest.skipIf(PWSH is None or sys.platform != "win32", "needs pwsh on Windows")

SHA = "a" * 40
MANIFEST = "b" * 64

# Wait-VenueQuiet stand-in. Mode per call from FAKE_QUIET_MODES (comma list, the last repeats), counted per run dir. Records each call with whether
# THIS run's claim was held at that moment (the claim file exists and names this run dir).
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
if ($MaxWaitSec -gt 0 -and $env:FAKE_QUIET_SLEEP_PRE) { Start-Sleep -Milliseconds ([int]([double]$env:FAKE_QUIET_SLEEP_PRE * 1000)) }
$held = $false
if ($env:FAKE_CLAIM_PATH -and (Test-Path -LiteralPath $env:FAKE_CLAIM_PATH)) {
    try { $c = Get-Content -LiteralPath $env:FAKE_CLAIM_PATH -Raw | ConvertFrom-Json; $held = ([string]$c.runDir -eq $runDir) } catch { $held = $false }
}
if ($env:FAKE_QUIET_LOG) { Add-Content -LiteralPath $env:FAKE_QUIET_LOG -Value ("{0} {1} maxWait={2} ownClaimHeld={3} mode={4}" -f [DateTime]::UtcNow.Ticks, (Split-Path -Leaf $runDir), $MaxWaitSec, $held, $mode) }
switch ($mode) {
    'QUIET' { 'PROBE venue-quiet-probe-x bachelor samples=5.0/5.0/5.0 mean=5.0% state=QUIET'; "QUIET mean=5.0% threshold=$ThresholdPercent% waitedSec=1"; exit 0 }
    'COOLDOWN' { "COOLDOWN_UNMET mean=32.0% threshold=$ThresholdPercent% waitedSec=544"; exit 0 }
    'FOREIGN' { 'FOREIGN_FLOOR owners=explorer:17204,textinputhost:23596,bloody7:21612 floorMean=32.0% waitedSec=180 probes=3'; "COOLDOWN_UNMET mean=32.0% threshold=$ThresholdPercent% waitedSec=180"; exit 0 }
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
@requires_windows_pwsh
class VenueFixtureModuleCopyTests(unittest.TestCase):
    """BACHELOR-THERMAL-GATE-WAIT-1 r14 (CI-B1): the scratch copies the mutation fixtures run must carry every sibling module VenueChain.psm1 imports."""

    def import_copy(self, folder: Path) -> subprocess.CompletedProcess:
        script = (f"$ErrorActionPreference = 'Stop'; Import-Module '{folder / MODULE.name}' -Force -DisableNameChecking\n"
                  "& (Get-Module VenueChain) { (Get-Command ConvertFrom-VenueJsonRawIdentity).Module.Path }")
        return subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)

    def test_ci_b1_the_derived_sibling_set_names_the_raw_json_module(self) -> None:
        self.assertIn("VenueRawJson.psm1", [p.name for p in sibling_modules(MODULE)])

    def test_ci_b1_a_scratch_copy_with_its_siblings_imports_and_resolves_them_from_the_scratch_dir(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ci-b1-") as tmp:
            folder = Path(os.path.realpath(tmp))
            copy_with_siblings(MODULE, folder)
            r = self.import_copy(folder)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(Path(r.stdout.strip()), folder / "VenueRawJson.psm1")

    def test_ci_b1_control_a_scratch_copy_without_its_siblings_stops_on_the_import(self) -> None:
        # the failure the hosted run hit: with only the module copied, the import stops before any mutation under test
        with tempfile.TemporaryDirectory(prefix="ci-b1-") as tmp:
            folder = Path(os.path.realpath(tmp))
            shutil.copy2(MODULE, folder / MODULE.name)
            r = self.import_copy(folder)
            self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("VenueRawJson.psm1", r.stdout + r.stderr)


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
        copy_with_siblings(MODULE, d)
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
        self.assertEqual(first_leg, ["QUIET", "TICKET_TAKEN", "CLAIM_TAKEN", "QUIET", "TICKET_ENDED", "LEG", "LEG", "CLAIM_RELEASED"], order)   # VENUE-CLAIM-FIFO-1: the ticket brackets the claim

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

    # BACHELOR-VENUE-FOREIGN-FLOOR-FAST-FAIL-1: Wait-VenueQuiet's FOREIGN_FLOOR early exit prints a typed line and then the same COOLDOWN_UNMET line; the chain must
    # still classify the leg COOLDOWN_UNMET (contended, never counted), never QUIET_PRELOAD_* and never VENUE_QUIET_UNKNOWN.
    def test_a_foreign_floor_early_exit_is_still_a_cooldown_unmet_leg(self) -> None:
        rd = self.case / "lane-FF"
        env = self.env(FAKE_QUIET_MODES="FOREIGN,FOREIGN,FOREIGN,FOREIGN,FOREIGN,FOREIGN", FAKE_LEG_PRELOADS="5.0,5.0,5.0")
        r = self.run_chain(rd, self.specs, env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        res = chain_result(r.stdout)
        got = [(l["verdict"], l["quietMean"], l["class"]) for l in res["legs"]]
        self.assertEqual(got, [("COOLDOWN_UNMET", 32.0, "COOLDOWN_UNMET")] * 3, "a low preLoad must not promote an early-exit leg to a counted class")
        ran = [l for l in self.chain_log(rd) if " RAN " in l]
        self.assertIn("verdict=COOLDOWN_UNMET quietMean=32.0 preLoadMean=5.0 class=COOLDOWN_UNMET", ran[0])
        self.assertNotIn("VENUE_QUIET_UNKNOWN", r.stdout)

    # r3 (Sol r2 blocker): a PRE-claim FOREIGN_FLOOR keeps the leg contended even when the later under-claim probe reads QUIET. Probe order per leg is pre-claim then under-claim.
    def test_a_pre_claim_foreign_floor_keeps_the_leg_contended_when_the_under_claim_probe_is_quiet(self) -> None:
        rd = self.case / "lane-FFQ"
        env = self.env(FAKE_QUIET_MODES="FOREIGN,QUIET,FOREIGN,QUIET,FOREIGN,QUIET", FAKE_LEG_PRELOADS="5.0,5.0,5.0")
        r = self.run_chain(rd, self.specs, env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        res = chain_result(r.stdout)
        got = [(l["verdict"], bool(l.get("preClaimForeignFloor")), l["class"]) for l in res["legs"]]
        self.assertEqual(got, [("QUIET", True, "COOLDOWN_UNMET")] * 3, "a pre-claim FOREIGN_FLOOR must not be promoted to a counted class by a later QUIET probe")
        ran = [l for l in self.chain_log(rd) if " RAN " in l]
        self.assertIn("verdict=QUIET quietMean=5.0 preLoadMean=5.0 class=COOLDOWN_UNMET", ran[0])
        self.assertIn("preClaimForeignFloor=1", ran[0])

    # r4 (Sol r3 blocker): the FOREIGN_FLOOR marker is sticky across a GATE_BUSY retry of the SAME leg. Probe order: attempt 1 pre-claim FOREIGN, under-claim BUSY (claim
    # released, leg retried); attempt 2 pre-claim QUIET, under-claim QUIET with a low preLoad. The leg stays contended; the next leg starts clean.
    def test_a_foreign_floor_survives_a_gate_busy_retry_of_the_same_leg_and_does_not_leak_to_the_next_leg(self) -> None:
        rd = self.case / "lane-FFB"
        env = self.env(FAKE_QUIET_MODES="FOREIGN,BUSY,QUIET,QUIET,QUIET,QUIET", FAKE_LEG_PRELOADS="5.0,5.0")
        r = self.run_chain(rd, self.specs[:2], env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        res = chain_result(r.stdout)
        got = [(l["verdict"], bool(l.get("preClaimForeignFloor")), l["class"]) for l in res["legs"]]
        self.assertEqual(got, [("QUIET", True, "COOLDOWN_UNMET"), ("QUIET", False, "QUIET_PRELOAD_OK")], "a FOREIGN_FLOOR seen on an attempt that ended GATE_BUSY must keep that leg (and only that leg) contended")
        ran = [l for l in self.chain_log(rd) if " RAN " in l]
        self.assertIn("class=COOLDOWN_UNMET", ran[0])
        self.assertIn("preClaimForeignFloor=1", ran[0])
        self.assertIn("class=QUIET_PRELOAD_OK", ran[1])
        self.assertNotIn("ForeignFloor=1", ran[1])

    def test_a_gate_busy_retry_that_never_saw_a_foreign_floor_is_still_a_counted_leg(self) -> None:
        rd = self.case / "lane-QBQ"
        env = self.env(FAKE_QUIET_MODES="QUIET,BUSY,QUIET,QUIET", FAKE_LEG_PRELOADS="5.0")
        r = self.run_chain(rd, self.specs[:1], env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        res = chain_result(r.stdout)
        self.assertEqual([(bool(l.get("preClaimForeignFloor")), l["class"]) for l in res["legs"]], [(False, "QUIET_PRELOAD_OK")])

    def test_a_quiet_pre_claim_and_quiet_under_claim_probe_is_still_a_counted_leg(self) -> None:
        rd = self.case / "lane-QQ"
        env = self.env(FAKE_QUIET_MODES="QUIET,QUIET,QUIET,QUIET,QUIET,QUIET", FAKE_LEG_PRELOADS="5.0,5.0,5.0")
        r = self.run_chain(rd, self.specs, env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        res = chain_result(r.stdout)
        self.assertEqual([(bool(l.get("preClaimForeignFloor")), l["class"]) for l in res["legs"]], [(False, "QUIET_PRELOAD_OK")] * 3)
        self.assertNotIn("ForeignFloor=1", "\n".join(self.chain_log(rd)))

    def test_a_plain_pre_claim_cooldown_timeout_without_a_foreign_floor_line_keeps_todays_class(self) -> None:
        rd = self.case / "lane-CQ"
        env = self.env(FAKE_QUIET_MODES="COOLDOWN,QUIET,COOLDOWN,QUIET,COOLDOWN,QUIET", FAKE_LEG_PRELOADS="5.0,5.0,5.0")
        r = self.run_chain(rd, self.specs, env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        res = chain_result(r.stdout)
        self.assertEqual([(bool(l.get("preClaimForeignFloor")), l["class"]) for l in res["legs"]], [(False, "QUIET_PRELOAD_OK")] * 3, "no FOREIGN_FLOOR line: no behaviour change")

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

    def test_probe_script_vanishing_under_a_live_chain_is_typed_and_stops_in_both_unknown_modes(self) -> None:
        # LANE-RETIRE-LIVE-DETACHED-DRIVER-GUARD-1: 2026-10-24 06:05-06:40Z a detached driver whose worktree was removed under it logged
        # VENUE_QUIET_UNKNOWN every 5 min for 40 min. A missing driver script is not "the venue was not measured": it is typed
        # VENUE_DRIVER_SCRIPT_MISSING with the path, and the chain STOPS whatever -OnUnknown says.
        for mode in ("Stop", "Continue"):
            with self.subTest(onUnknown=mode):
                quiet = self.case / f"vanishing-quiet-{mode}.ps1"
                shutil.copy2(self.fake_quiet, quiet)
                rd = self.case / f"lane-GONE-{mode}"
                before = self.started()
                # leg 1 runs, and deletes the probe script as it exits (the worktree was removed under the chain); leg 2's probe cannot launch
                r = self.run_chain(rd, self.specs, "-OnUnknown", mode, quiet=quiet, env=self.env(FAKE_LEG_DELETE_PATH=str(quiet)))
                self.assertEqual(r.returncode, 6, r.stdout + r.stderr)
                res = chain_result(r.stdout)
                self.assertEqual([l["status"] for l in res["legs"]], ["RAN", "NOT_RUN", "NOT_RUN"])
                self.assertEqual((res["legs"][1]["verdict"], res["legs"][1]["reason"]), ("VENUE_DRIVER_SCRIPT_MISSING", f"path={quiet}"))
                self.assertEqual(res["stopReason"], "VENUE_DRIVER_SCRIPT_MISSING")
                log = self.chain_log(rd)
                self.assertTrue(any(f"VENUE_DRIVER_SCRIPT_MISSING path={quiet}" in l for l in log), log)
                self.assertTrue(any(re.search(r"LEG 02-fx-b-spec NOT_RUN VENUE_DRIVER_SCRIPT_MISSING ", l) for l in log), log)
                self.assertFalse([l for l in log if "VENUE_QUIET_UNKNOWN" in l or "undocumented-exit" in l], log)
                self.assertEqual(self.started() - before, 1, "no leg and no retry after the probe script vanished")

    def test_module_names_a_missing_driver_script_and_passes_present_ones(self) -> None:
        present = self.case / "present.ps1"
        present.write_text("# x\n", encoding="utf-8")
        gone = self.case / "gone" / "Wait-VenueQuiet.ps1"
        cmd = (f"Import-Module '{MODULE}' -Force -DisableNameChecking; "
               f"$a = Get-VenueDriverScriptMissing -Path @('{present}'); $b = Get-VenueDriverScriptMissing -Path @('{present}', '{gone}'); "
               "\"A=[$a]\"; \"B=[$b]\"")
        r = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("A=[]", r.stdout)
        self.assertIn(f"B=[VENUE_DRIVER_SCRIPT_MISSING path={gone}]", r.stdout)

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


# ---------------------------------------------------------------------------------------------------------------------------------------------------
# VENUE-UM-SUPPLEMENTARY-WHEN-BACHELOR-HELD-1: a Bachelor chain also offers each leg on ultra-magnus as a SUPPLEMENTARY run. Offline like the class above: the
# probe and the leg are fakes that record WHICH VENUE they were called for, ultra-magnus's agent share is a temp directory (its staged-build cache), and every
# claim lives in a temp -ClaimDir. Nothing here reaches a venue.
# ---------------------------------------------------------------------------------------------------------------------------------------------------
FAKE_QUIET_V = r"""
param([string]$Venue, [string]$WorkDir, [string]$GateLog, [double]$ThresholdPercent = 20, [int]$MaxWaitSec = 1800, [int]$RecheckSec = 90, [int]$MaxAdmissionWaitSec = 0)
$runDir = Split-Path -Parent $WorkDir
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
if ($env:FAKE_QUIET_LOG) { Add-Content -LiteralPath $env:FAKE_QUIET_LOG -Value ("{0} venue={1} maxWait={2} adm={3}" -f [DateTime]::UtcNow.Ticks, $Venue, $MaxWaitSec, $MaxAdmissionWaitSec) }
if ($Venue -eq 'ultra-magnus') { $mode = $(if ($env:FAKE_UM_QUIET_MODE) { $env:FAKE_UM_QUIET_MODE } else { 'QUIET' }) }
else {
    $countFile = Join-Path $runDir 'fake-quiet-count.txt'
    $n = $(if (Test-Path -LiteralPath $countFile) { [int](Get-Content -LiteralPath $countFile) } else { 0 })
    Set-Content -LiteralPath $countFile -Value ($n + 1)
    $modes = @(([string]$env:FAKE_QUIET_MODES) -split ',' | Where-Object { $_ })
    if ($modes.Count -eq 0) { $modes = @('QUIET') }
    $mode = $modes[[Math]::Min($n, $modes.Count - 1)]
    if ($MaxWaitSec -gt 0 -and $env:FAKE_QUIET_SLEEP_PRE) { Start-Sleep -Milliseconds ([int]([double]$env:FAKE_QUIET_SLEEP_PRE * 1000)) }
    if ($MaxWaitSec -eq 0 -and $env:FAKE_QUIET_SLEEP_UNDER) { Start-Sleep -Milliseconds ([int]([double]$env:FAKE_QUIET_SLEEP_UNDER * 1000)) }   # the under-claim probe's own admission hold
}
switch ($mode) {
    'QUIET' { "QUIET mean=5.0% threshold=$ThresholdPercent% waitedSec=1"; exit 0 }
    'BUSY' { 'GATE_BUSY quiet-probe waitedSec=1'; exit 3 }
    'UNKNOWN' { "VENUE_QUIET_UNKNOWN mean=UNKNOWN threshold=$ThresholdPercent% waitedSec=1 reason=probe-failed note=fake"; exit 6 }
}
"""

FAKE_LEG_V = r"""
param([string]$Venue, [string]$LegSpec, [string]$SourceCommit, [string]$BuildManifestSha256, [string]$Backend = '', [string]$SheetCopyDir = '')
$leaf = Split-Path -Leaf $LegSpec
Add-Content -LiteralPath $env:FAKE_LEG_LOG -Value ("START {0} {1} {2} {3}" -f [DateTime]::UtcNow.Ticks, $leaf, $Venue, $BuildManifestSha256.Substring(0, 8))
if ($Venue -eq 'ultra-magnus' -and $env:FAKE_UM_LEG_SLEEP) { Start-Sleep -Milliseconds ([int]([double]$env:FAKE_UM_LEG_SLEEP * 1000)) }
# VENUE-UM-SUPP-REMOTE-IDENTITY-AND-CACHE-BOUND-1: a client that SUBMITTED a remote job (prints its id when FAKE_UM_JOB_ID is set) and then never returns: the chain's bounded
# child kills it, and the remote job it submitted is still out there. FAKE_JOB_DONE_ON_LEG: the leg of that name finishing is what lands the remote job's result in the share.
if ($Venue -eq 'ultra-magnus' -and $env:FAKE_UM_HANG_LEAF -and $leaf -eq $env:FAKE_UM_HANG_LEAF) {
    if ($env:FAKE_UM_JOB_ID) { "DVE_JOB_ID=$($env:FAKE_UM_JOB_ID)" }
    Start-Sleep -Seconds 300
    exit 0
}
if ($Venue -eq 'bachelor' -and $env:FAKE_JOB_DONE_ON_LEG -and $leaf -eq $env:FAKE_JOB_DONE_ON_LEG) {
    $ob = Join-Path $env:FAKE_UM_SHARE 'outbox'
    New-Item -ItemType Directory -Force -Path $ob | Out-Null
    Set-Content -LiteralPath (Join-Path $ob ($env:FAKE_UM_JOB_ID + '.result.json')) -Value '{}'
}
$rdir = Join-Path $env:FAKE_LEG_ROOT ('receipts\CARD-1\' + [IO.Path]::GetFileNameWithoutExtension($leaf) + '\' + $Venue)
New-Item -ItemType Directory -Force -Path $rdir | Out-Null
$rp = Join-Path $rdir ([guid]::NewGuid().ToString() + '.json')
Set-Content -LiteralPath $rp -Value ('{"venue":{"role":"' + $(if ($Venue -eq 'ultra-magnus') { 'supplementary' } else { 'acceptance' }) + '"}}')
if ($Venue -eq 'ultra-magnus' -and $env:FAKE_UM_LEG_MODE -eq 'crash') { 'DVE_OUTCOME=PASS'; exit 5 }
if ($Venue -eq 'ultra-magnus' -and $env:FAKE_UM_LEG_MODE -eq 'fail') { 'DVE_OUTCOME=FAIL'; "DVE_RECEIPT_PATH=$rp"; exit 0 }
'DVE_OUTCOME=PASS'
"DVE_RECEIPT_PATH=$rp"
exit 0
"""


@requires_windows_pwsh
class VenueChainSupplementaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="venue-supp-"))
        cls.fake_quiet = cls.tmp / "fake-quiet-v.ps1"
        cls.fake_quiet.write_text(FAKE_QUIET_V, encoding="utf-8")
        cls.fake_leg = cls.tmp / "fake-leg-v.ps1"
        cls.fake_leg.write_text(FAKE_LEG_V, encoding="utf-8")
        cls.specs = []        # carry criteria.supplementary for cpu: offered on ultra-magnus
        for name in ("sp-a", "sp-b"):
            p = cls.tmp / f"{name}.json"
            p.write_text(json.dumps({"legId": name, "backends": ["cpu"], "criteria": {"acceptance": {"cpu": []}, "supplementary": {"cpu": []}}}), encoding="utf-8")
            cls.specs.append(p)
        cls.bare = cls.tmp / "bare.json"   # no supplementary block
        cls.bare.write_text(json.dumps({"legId": "bare", "backends": ["cpu"], "criteria": {"acceptance": {"cpu": []}}}), encoding="utf-8")
        cls.staged_text = '{"fixture":"staged-on-ultra-magnus"}'

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self) -> None:
        import hashlib
        self.case = Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp))
        self.claims = self.case / "claims"
        self.quiet_log = self.case / "quiet-calls.log"
        self.leg_log = self.case / "leg-calls.log"
        self.um_share = self.case / "um-share"          # STAGED: the cache holds the build manifest for SHA
        (self.um_share / "cache").mkdir(parents=True)
        staged = self.um_share / "cache" / f"playback-attr-3-cuda-{SHA[:12]}-build.json"
        staged.write_text(self.staged_text, encoding="utf-8")
        self.staged_sha = hashlib.sha256(staged.read_bytes()).hexdigest()
        self.empty_share = self.case / "um-share-empty"  # NOT staged
        (self.empty_share / "cache").mkdir(parents=True)

    # ---------------------------------------------------------------- helpers
    def env(self, **extra: str) -> dict:
        e = dict(os.environ)
        e.update({"FAKE_QUIET_LOG": str(self.quiet_log), "FAKE_LEG_LOG": str(self.leg_log), "FAKE_LEG_ROOT": str(self.case)})
        e.update(extra)
        return e

    def chain_args(self, run_dir: Path, specs: list[Path], *extra: str, runner: Path = RUNNER, share: Path | None = None, venue: str = "bachelor", deadline_sec: int = 180) -> list[str]:
        return [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(runner), "-Venue", venue,
                "-LegSpec", ",".join(str(s) for s in specs), "-RunDir", str(run_dir), "-SourceCommit", SHA, "-BuildManifestSha256", MANIFEST,
                "-ClaimDir", str(self.claims), "-QuietScript", str(self.fake_quiet), "-LegScript", str(self.fake_leg),
                "-CooldownSec", "0", "-ClaimPollSec", "1", "-SupplementaryAgentShare", str(share or self.um_share),
                "-DeadlineUtc", utc_iso(datetime.now(timezone.utc) + timedelta(seconds=deadline_sec)), *extra]

    def run_chain(self, run_dir: Path, specs: list[Path], *extra: str, env: dict | None = None, runner: Path = RUNNER, share: Path | None = None, venue: str = "bachelor") -> subprocess.CompletedProcess:
        return subprocess.run(self.chain_args(run_dir, specs, *extra, runner=runner, share=share, venue=venue), capture_output=True, text=True, timeout=300, env=env or self.env())

    def starts(self) -> list[tuple[str, str, str]]:
        """(leaf, venue, manifest8) per START, in order."""
        if not self.leg_log.exists():
            return []
        return [(p[2], p[3], p[4]) for p in (l.split() for l in self.leg_log.read_text(encoding="utf-8").splitlines()) if p[0] == "START"]

    def quiet_calls(self) -> list[str]:
        return self.quiet_log.read_text(encoding="utf-8").splitlines() if self.quiet_log.exists() else []

    def chain_log(self, run_dir: Path) -> list[str]:
        return (run_dir / "chain.log").read_text(encoding="utf-8").splitlines()

    def write_um_claim(self) -> str:
        self.claims.mkdir(parents=True, exist_ok=True)
        text = json.dumps({"schema": "mlv-app/venue-claim/v1", "venue": "ultra-magnus", "ownerPid": os.getpid(), "ownerCreatedUtc": process_start_iso(os.getpid()),
                           "host": os.environ.get("COMPUTERNAME", ""), "runDir": "C:\\elsewhere\\lane-UM-OWNER", "purpose": "fixture",
                           "claimedUtc": utc_iso(datetime.now(timezone.utc)), "expiresUtc": utc_iso(datetime.now(timezone.utc) + timedelta(hours=3)), "nonce": "um-fixture-nonce"})
        (self.claims / "ultra-magnus.claim.json").write_text(text, encoding="utf-8")
        return text

    def mutated_runner(self, old: str, new: str, count: int) -> Path:
        d = Path(tempfile.mkdtemp(prefix="mut-", dir=self.case))
        text = RUNNER.read_text(encoding="utf-8")
        self.assertEqual(text.count(old), count, f"mutation anchor count changed: {old!r}")
        (d / RUNNER.name).write_text(text.replace(old, new), encoding="utf-8")
        copy_with_siblings(MODULE, d)
        return d / RUNNER.name

    # ---------------------------------------------------------------- (a) after each Bachelor leg, when ultra-magnus is idle
    def test_idle_ultra_magnus_runs_each_leg_as_supplementary_after_the_bachelor_leg(self) -> None:
        rd = self.case / "lane-IDLE"
        r = self.run_chain(rd, self.specs)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # Bachelor first, then the same leg on ultra-magnus, then the next Bachelor leg: the offer rides between legs
        self.assertEqual([(l, v) for l, v, _ in self.starts()], [("sp-a.json", "bachelor"), ("sp-a.json", "ultra-magnus"), ("sp-b.json", "bachelor"), ("sp-b.json", "ultra-magnus")])
        # the ultra-magnus run is handed ITS OWN staged manifest hash, never the Bachelor one
        um = [m for _, v, m in self.starts() if v == "ultra-magnus"]
        self.assertEqual(set(um), {self.staged_sha[:8]})
        self.assertNotEqual(self.staged_sha, MANIFEST)
        log = self.chain_log(rd)
        runs = [l for l in log if " SUPPLEMENTARY_RUN " in l]
        done = [l for l in log if " SUPPLEMENTARY_RAN " in l]
        self.assertEqual((len(runs), len(done)), (2, 2), log)
        for l in runs + done:
            self.assertIn("venue=ultra-magnus role=supplementary", l)
        self.assertIn("trigger=after-leg", runs[0])
        res = chain_result(r.stdout)
        # the result separates the two: Bachelor legs unchanged, the ultra-magnus runs only under `supplementary`
        self.assertEqual([l["status"] for l in res["legs"]], ["RAN", "RAN"])
        self.assertEqual((res["ran"], res["failed"], res["notRun"], res["exitCode"]), (2, 0, 0, 0))
        sup = res["supplementary"]
        self.assertEqual([(s["status"], s["venue"], s["role"], s["trigger"], s["outcome"], s["receiptRole"]) for s in sup], [("RAN", "ultra-magnus", "supplementary", "after-leg", "PASS", "supplementary")] * 2)
        self.assertTrue(all(s["receipt"] and Path(s["receipt"]).exists() for s in sup), "a supplementary run writes its own receipt")
        self.assertFalse(any("ultra-magnus" in l["receipt"] for l in res["legs"]), "no Bachelor leg may point at an ultra-magnus receipt")
        # the UM probe is one probe that never waits for admission long; the UM claim is gone afterwards
        um_probes = [c for c in self.quiet_calls() if "venue=ultra-magnus" in c]
        self.assertEqual(len(um_probes), 2)
        self.assertTrue(all("maxWait=0" in c and "adm=120" in c for c in um_probes), um_probes)
        self.assertFalse((self.claims / "ultra-magnus.claim.json").exists(), "the ultra-magnus claim was left behind")
        self.assertFalse((self.claims / "bachelor.claim.json").exists())

    # ---------------------------------------------------------------- (a) busy ultra-magnus: skip, Bachelor unaffected
    def test_busy_ultra_magnus_is_a_typed_skip_and_bachelor_is_unaffected(self) -> None:
        fixture = self.write_um_claim()
        rd = self.case / "lane-BUSY"
        r = self.run_chain(rd, self.specs)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([(l, v) for l, v, _ in self.starts()], [("sp-a.json", "bachelor"), ("sp-b.json", "bachelor")])
        res = chain_result(r.stdout)
        self.assertEqual([l["status"] for l in res["legs"]], ["RAN", "RAN"])
        self.assertEqual((res["ran"], res["exitCode"], res["stopReason"]), (2, 0, None))
        self.assertEqual([(s["status"], s["reason"]) for s in res["supplementary"]], [("SKIPPED", "UM_BUSY")] * 2)
        log = self.chain_log(rd)
        skip = [l for l in log if " SUPPLEMENTARY_SKIPPED " in l]
        self.assertEqual(len(skip), 2, log)
        self.assertRegex(skip[0], r"SUPPLEMENTARY_SKIPPED leg=01-sp-a-spec trigger=after-leg reason=UM_BUSY venue=ultra-magnus role=supplementary detail=holder pid=")
        self.assertEqual([c for c in self.quiet_calls() if "venue=ultra-magnus" in c], [], "a busy ultra-magnus is not even probed")
        self.assertEqual((self.claims / "ultra-magnus.claim.json").read_text(encoding="utf-8"), fixture, "another chain's live ultra-magnus claim was touched")

    def test_an_ultra_magnus_that_is_not_quiet_is_skipped_and_its_claim_released(self) -> None:
        for mode, reason_part in (("BUSY", "verdict=GATE_BUSY"), ("UNKNOWN", "verdict=VENUE_QUIET_UNKNOWN")):
            with self.subTest(mode=mode):
                self.leg_log.unlink(missing_ok=True)
                rd = self.case / f"lane-NQ-{mode}"
                r = self.run_chain(rd, self.specs[:1], env=self.env(FAKE_UM_QUIET_MODE=mode))
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual([v for _, v, _ in self.starts()], ["bachelor"])
                s = chain_result(r.stdout)["supplementary"]
                self.assertEqual((s[0]["status"], s[0]["reason"]), ("SKIPPED", "UM_NOT_QUIET"))
                self.assertIn(reason_part, s[0]["detail"])
                self.assertFalse((self.claims / "ultra-magnus.claim.json").exists())

    # ---------------------------------------------------------------- Dot 180e287f P2: a UM waiter that queues AFTER the idle check and BEFORE the claim
    IDLE_LINE = "    if (-not $idle.idle) { Write-SuppSkip $Leg $Trigger 'UM_BUSY' \"holder $($idle.holder) liveTickets=$($idle.liveTickets)\" $false; return }\n"

    def hooked_runner(self, hook: str) -> Path:
        """The real runner with `hook` run right after the supplementary idle check: the deterministic interleaving seam (no sleeps, no clock)."""
        return self.mutated_runner(self.IDLE_LINE, self.IDLE_LINE + "    " + hook + "\n", 1)

    def test_a_um_waiter_that_queues_between_the_idle_check_and_the_claim_is_served_first(self) -> None:
        waiter_rd = "C:\\elsewhere\\lane-UM-WAITER"
        hook = f"[void](New-VenueTicket -Venue $suppVenue -RunDir '{waiter_rd}' -Purpose 'um waiter' -StaleAfterSec 3600 -ClaimDir $ClaimDir -OwnerPid {os.getpid()})   # a normal UM waiter takes its ticket now"
        rd = self.case / "lane-RACE"
        r = self.run_chain(rd, self.specs[:1], runner=self.hooked_runner(hook))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # the supplementary leg does no work: no ultra-magnus probe, no ultra-magnus leg; Bachelor is unaffected
        self.assertEqual([v for _, v, _ in self.starts()], ["bachelor"], "the supplementary took the claim ahead of a live UM ticket and ran")
        self.assertEqual([c for c in self.quiet_calls() if "venue=ultra-magnus" in c], [], "a yielded offer must not even probe ultra-magnus")
        res = chain_result(r.stdout)
        self.assertEqual(([l["status"] for l in res["legs"]], res["exitCode"]), (["RAN"], 0))
        self.assertEqual([(s["status"], s["reason"]) for s in res["supplementary"]], [("SKIPPED", "YIELDED_TO_TICKET")])
        log = self.chain_log(rd)
        self.assertTrue(any("SUPPLEMENTARY_CLAIM_RELEASED" in l and "result=RELEASED" in l for l in log), log)
        # the claim ends FREE and the waiter's ticket, still first in line, is the one granted next
        self.assertFalse((self.claims / "ultra-magnus.claim.json").exists(), "the yielded claim was left behind")
        served = pwsh_text(
            f"Import-Module '{MODULE}' -Force -DisableNameChecking; $row = @(Get-VenueTickets -Venue ultra-magnus -ClaimDir '{self.claims}' | Where-Object {{ $_.state -cne 'STALE' }}); "
            f"$t = [pscustomobject]@{{ name = $row[0].name; path = $row[0].path; nonce = [string]$row[0].ticket.nonce }}; "
            f"$q = Request-VenueClaim -Venue ultra-magnus -RunDir '{waiter_rd}' -ClaimDir '{self.claims}' -OwnerPid {os.getpid()} -Ticket $t -TicketStaleAfterSec 3600; "
            f"[void](Release-VenueClaim -Venue ultra-magnus -Nonce $q.claim.nonce -ClaimDir '{self.claims}'); \"$($row.Count) $($q.granted) $($q.state) $($q.position) $($q.claim.runDir)\"")
        self.assertEqual(served, f"1 True TAKEN 1 {waiter_rd}")

    def test_control_no_ticket_between_the_idle_check_and_the_claim_runs_the_supplementary_as_before(self) -> None:
        rd = self.case / "lane-RACE-CTL"
        r = self.run_chain(rd, self.specs[:1], runner=self.hooked_runner("# no waiter queues"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([v for _, v, _ in self.starts()], ["bachelor", "ultra-magnus"])
        self.assertEqual([(s["status"], s["trigger"]) for s in chain_result(r.stdout)["supplementary"]], [("RAN", "after-leg")])
        self.assertFalse((self.claims / "ultra-magnus.claim.json").exists())

    # ---------------------------------------------------------------- (b) held more than -SupplementaryHeldSec
    def test_a_leg_held_past_the_threshold_is_offered_on_ultra_magnus_before_bachelor_runs_it(self) -> None:
        rd = self.case / "lane-HELD"
        # the first Bachelor probe takes 3 s and is BUSY (a gate that did not clear): the leg has now waited longer than the 2 s threshold
        r = self.run_chain(rd, self.specs[:1], "-SupplementaryHeldSec", "2", env=self.env(FAKE_QUIET_MODES="BUSY,QUIET,QUIET", FAKE_QUIET_SLEEP_PRE="3"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # ultra-magnus FIRST (the offer while Bachelor is held), Bachelor after; and the after-leg offer does not repeat it
        self.assertEqual([(l, v) for l, v, _ in self.starts()], [("sp-a.json", "ultra-magnus"), ("sp-a.json", "bachelor")])
        res = chain_result(r.stdout)
        self.assertEqual([(s["status"], s["trigger"]) for s in res["supplementary"]], [("RAN", "held")])
        self.assertEqual([l["status"] for l in res["legs"]], ["RAN"])
        log = self.chain_log(rd)
        order = [l.split(" ", 2)[1] for l in log if not l.startswith("CHAIN_RESULT=")]
        self.assertLess(order.index("SUPPLEMENTARY_CLAIM_TAKEN"), order.index("CLAIM_TAKEN"), "the offer came before the Bachelor claim")

    def test_a_leg_that_has_not_waited_long_is_offered_only_after_it_ran(self) -> None:
        rd = self.case / "lane-NOTHELD"
        r = self.run_chain(rd, self.specs[:1], env=self.env(FAKE_QUIET_MODES="BUSY,QUIET,QUIET", FAKE_QUIET_SLEEP_PRE="3"))   # default threshold 1800 s
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([(l, v) for l, v, _ in self.starts()], [("sp-a.json", "bachelor"), ("sp-a.json", "ultra-magnus")])
        self.assertEqual([s["trigger"] for s in chain_result(r.stdout)["supplementary"]], ["after-leg"])

    def test_held_offer_in_the_claim_line_does_not_lose_the_bachelor_place(self) -> None:
        # another chain holds the BACHELOR claim for ~6 s: our leg waits in the claim line past the 2 s threshold, runs on ultra-magnus meanwhile, then runs on Bachelor
        self.claims.mkdir(parents=True, exist_ok=True)
        bclaim = self.claims / "bachelor.claim.json"
        bclaim.write_text(json.dumps({"schema": "mlv-app/venue-claim/v1", "venue": "bachelor", "ownerPid": os.getpid(), "ownerCreatedUtc": process_start_iso(os.getpid()), "host": "",
                                      "runDir": "C:\\elsewhere\\lane-OTHER", "purpose": "fixture", "claimedUtc": utc_iso(datetime.now(timezone.utc)),
                                      "expiresUtc": utc_iso(datetime.now(timezone.utc) + timedelta(hours=3)), "nonce": "b-fixture"}), encoding="utf-8")
        rd = self.case / "lane-LINE"
        p = subprocess.Popen(self.chain_args(rd, self.specs[:1], "-SupplementaryHeldSec", "2"), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=self.env())
        t0 = time.time()
        while time.time() - t0 < 90 and not any(" SUPPLEMENTARY_RAN " in l for l in (self.chain_log(rd) if (rd / "chain.log").exists() else [])):
            time.sleep(0.25)
        bclaim.unlink()
        out, _ = p.communicate(timeout=180)
        self.assertEqual(p.returncode, 0, out)
        self.assertEqual([(l, v) for l, v, _ in self.starts()], [("sp-a.json", "ultra-magnus"), ("sp-a.json", "bachelor")])
        res = chain_result(out)
        self.assertEqual([l["status"] for l in res["legs"]], ["RAN"])
        self.assertEqual([(s["status"], s["trigger"]) for s in res["supplementary"]], [("RAN", "held")])
        self.assertFalse([l for l in self.chain_log(rd) if "TICKET_STALE_REMOVED" in l], "the supplementary run cost the chain its place in the line")

    # ---------------------------------------------------------------- (b) held inside ONE blocking probe (heavy-slot admission), r3 / sol r2 BLOCKER
    def test_a_probe_that_ends_not_measured_after_a_long_hold_still_gets_the_held_offer_before_the_chain_stops(self) -> None:
        # The Bachelor pre-claim probe blocks past the 2 s threshold (a stand-in for a heavy-slot admission hold that runs to -ProbeAdmissionWaitSec) and then returns
        # VENUE_QUIET_UNKNOWN (um-run's UNRESOLVED at the admission ceiling). The chain never came round its wait loop in between, so the offer must be made when the probe returns,
        # BEFORE the OnUnknown Stop path sets stopReason (a stopped chain skips every offer as CHAIN_STOPPED).
        rd = self.case / "lane-ADMISSION"
        r = self.run_chain(rd, self.specs[:1], "-SupplementaryHeldSec", "2", env=self.env(FAKE_QUIET_MODES="UNKNOWN", FAKE_QUIET_SLEEP_PRE="3"))
        # fail-closed and unchanged: the Bachelor leg is NOT_RUN VENUE_QUIET_UNKNOWN, exit 6, the chain stopped, and Bachelor never ran the leg
        self.assertEqual(r.returncode, 6, r.stdout + r.stderr)
        res = chain_result(r.stdout)
        self.assertEqual([(l["status"], l["verdict"]) for l in res["legs"]], [("NOT_RUN", "VENUE_QUIET_UNKNOWN")])
        self.assertEqual((res["ran"], res["failed"], res["notRun"], res["exitCode"], res["stopReason"]), (0, 0, 1, 6, "VENUE_QUIET_UNKNOWN"))
        # the offer: the same leg ran on ultra-magnus, as the held offer, and only there
        self.assertEqual([(l, v) for l, v, _ in self.starts()], [("sp-a.json", "ultra-magnus")])
        self.assertEqual([(s["status"], s["trigger"], s["venue"], s["role"]) for s in res["supplementary"]], [("RAN", "held", "ultra-magnus", "supplementary")])
        log = self.chain_log(rd)
        offered = [i for i, l in enumerate(log) if " SUPPLEMENTARY_CLAIM_TAKEN " in l]
        stopped = [i for i, l in enumerate(log) if " NOT_RUN VENUE_QUIET_UNKNOWN " in l]
        self.assertTrue(offered and stopped, log)
        self.assertLess(offered[0], stopped[0], "the offer came before the Bachelor leg was stopped")
        self.assertFalse((self.claims / "ultra-magnus.claim.json").exists())
        self.assertFalse((self.claims / "bachelor.claim.json").exists())

    def test_a_probe_that_ends_not_measured_after_a_short_wait_gets_no_offer(self) -> None:
        # the default 1800 s threshold is not reached by a 3 s probe: the leg is NOT_RUN exactly as before and ultra-magnus is never touched
        rd = self.case / "lane-ADMISSION-SHORT"
        r = self.run_chain(rd, self.specs[:1], env=self.env(FAKE_QUIET_MODES="UNKNOWN", FAKE_QUIET_SLEEP_PRE="3"))
        self.assertEqual(r.returncode, 6, r.stdout + r.stderr)
        res = chain_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["supplementary"]), (6, "VENUE_QUIET_UNKNOWN", []))
        self.assertEqual(self.starts(), [])
        self.assertEqual([c for c in self.quiet_calls() if "venue=ultra-magnus" in c], [])

    def test_the_offer_after_a_not_measured_probe_never_moves_the_bachelor_verdict_when_the_chain_goes_on(self) -> None:
        # -OnUnknown Continue: leg 1's probe is not measured after a long hold (offered on ultra-magnus), leg 2 is measured and runs on Bachelor. Exit 6 stays; legs stays Bachelor-only.
        rd = self.case / "lane-ADMISSION-CONT"
        r = self.run_chain(rd, self.specs, "-SupplementaryHeldSec", "2", "-OnUnknown", "Continue", env=self.env(FAKE_QUIET_MODES="UNKNOWN,QUIET,QUIET,QUIET", FAKE_QUIET_SLEEP_PRE="3"))
        self.assertEqual(r.returncode, 6, r.stdout + r.stderr)
        res = chain_result(r.stdout)
        self.assertEqual([l["status"] for l in res["legs"]], ["NOT_RUN", "RAN"])
        self.assertEqual((res["ran"], res["notRun"], res["exitCode"], res["stopReason"]), (1, 1, 6, None))
        self.assertEqual([(l, v) for l, v, _ in self.starts()], [("sp-a.json", "ultra-magnus"), ("sp-b.json", "bachelor"), ("sp-b.json", "ultra-magnus")])
        self.assertEqual([(s["name"], s["trigger"], s["status"]) for s in res["supplementary"]], [("01-sp-a-spec", "held", "RAN"), ("02-sp-b-spec", "after-leg", "RAN")])

    # ---------------------------------------------------------------- (b) held inside the UNDER-CLAIM probe (heavy-slot admission after the claim), r4 / sol r3 BLOCKER
    def test_an_under_claim_probe_that_ends_not_measured_after_a_long_hold_still_gets_the_held_offer(self) -> None:
        # Pre-claim probe QUIET (no hold), the Bachelor claim is granted, then the under-claim probe blocks past the 2 s threshold and returns VENUE_QUIET_UNKNOWN (admission
        # exhausted). The leg never ran, so the after-leg offer cannot happen: the held offer must be made here, after the Bachelor claim is released and before OnUnknown
        # Stop sets stopReason. Both OnUnknown modes. The Bachelor verdict stays fail-closed: NOT_RUN phase=under-claim, exit 6.
        for mode in ("Stop", "Continue"):
            with self.subTest(onUnknown=mode):
                self.leg_log.unlink(missing_ok=True)
                self.quiet_log.unlink(missing_ok=True)
                rd = self.case / f"lane-UNDERCLAIM-{mode}"
                r = self.run_chain(rd, self.specs[:1], "-SupplementaryHeldSec", "2", "-OnUnknown", mode, env=self.env(FAKE_QUIET_MODES="QUIET,UNKNOWN", FAKE_QUIET_SLEEP_UNDER="3"))
                self.assertEqual(r.returncode, 6, r.stdout + r.stderr)
                res = chain_result(r.stdout)
                self.assertEqual([(l["status"], l["verdict"], l["phase"]) for l in res["legs"]], [("NOT_RUN", "VENUE_QUIET_UNKNOWN", "under-claim")])
                self.assertEqual((res["ran"], res["failed"], res["notRun"], res["exitCode"], res["stopReason"]), (0, 0, 1, 6, "VENUE_QUIET_UNKNOWN" if mode == "Stop" else None))
                # one offer, the held one, and only on ultra-magnus: Bachelor never ran the leg
                self.assertEqual([(l, v) for l, v, _ in self.starts()], [("sp-a.json", "ultra-magnus")])
                self.assertEqual([(s["status"], s["trigger"], s["venue"], s["role"]) for s in res["supplementary"]], [("RAN", "held", "ultra-magnus", "supplementary")])
                log = self.chain_log(rd)
                tokens = [l.split(" ", 2)[1] for l in log if not l.startswith("CHAIN_RESULT=")]
                self.assertEqual(tokens.count("CLAIM_TAKEN"), 1, log)
                self.assertEqual(tokens.count("CLAIM_RELEASED"), 1, "the Bachelor claim is released exactly once")
                self.assertLess(tokens.index("CLAIM_RELEASED"), tokens.index("SUPPLEMENTARY_CLAIM_TAKEN"), "the Bachelor claim was released before the offer")
                stopped = [i for i, l in enumerate(log) if " NOT_RUN VENUE_QUIET_UNKNOWN " in l and "phase=under-claim" in l]
                offered = [i for i, l in enumerate(log) if " SUPPLEMENTARY_CLAIM_TAKEN " in l]
                self.assertLess(offered[0], stopped[0], "the offer came before the Bachelor leg line")
                self.assertFalse((self.claims / "bachelor.claim.json").exists())
                self.assertFalse((self.claims / "ultra-magnus.claim.json").exists())

    def test_an_under_claim_probe_that_ends_not_measured_after_a_short_hold_gets_no_offer(self) -> None:
        # control: the same shape with the default 1800 s threshold, which a 3 s hold does not reach: NOT_RUN exactly as before, ultra-magnus never touched
        for mode in ("Stop", "Continue"):
            with self.subTest(onUnknown=mode):
                self.leg_log.unlink(missing_ok=True)
                self.quiet_log.unlink(missing_ok=True)
                rd = self.case / f"lane-UNDERCLAIM-SHORT-{mode}"
                r = self.run_chain(rd, self.specs[:1], "-OnUnknown", mode, env=self.env(FAKE_QUIET_MODES="QUIET,UNKNOWN", FAKE_QUIET_SLEEP_UNDER="3"))
                self.assertEqual(r.returncode, 6, r.stdout + r.stderr)
                res = chain_result(r.stdout)
                self.assertEqual([(l["status"], l["phase"]) for l in res["legs"]], [("NOT_RUN", "under-claim")])
                self.assertEqual((res["exitCode"], res["supplementary"]), (6, []))
                self.assertEqual(self.starts(), [])
                self.assertEqual([c for c in self.quiet_calls() if "venue=ultra-magnus" in c], [])
                self.assertFalse((self.claims / "bachelor.claim.json").exists())

    # ---------------------------------------------------------------- (e) the opt-out
    def test_no_supplementary_switch_runs_nothing_on_ultra_magnus(self) -> None:
        rd = self.case / "lane-OPTOUT"
        r = self.run_chain(rd, self.specs, "-NoSupplementary", "-SupplementaryHeldSec", "0")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([v for _, v, _ in self.starts()], ["bachelor", "bachelor"])
        self.assertEqual([c for c in self.quiet_calls() if "venue=ultra-magnus" in c], [])
        log = self.chain_log(rd)
        self.assertFalse([l for l in log if "SUPPLEMENTARY_" in l], log)   # every supplementary log token is upper-case with an underscore; CHAIN_START's own fields are lower-case
        self.assertIn("supplementary=False", log[0])
        self.assertEqual(chain_result(r.stdout)["supplementary"], [])
        self.assertFalse((self.claims / "ultra-magnus.claim.json").exists())

    def test_default_is_on(self) -> None:
        rd = self.case / "lane-DEFAULT"
        r = self.run_chain(rd, self.specs[:1])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("supplementary=True", self.chain_log(rd)[0])
        self.assertEqual([v for _, v, _ in self.starts()], ["bachelor", "ultra-magnus"])

    def test_an_ultra_magnus_chain_never_offers_a_supplementary_leg(self) -> None:
        rd = self.case / "lane-UMCHAIN"
        r = self.run_chain(rd, self.specs[:1], venue="ultra-magnus")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([v for _, v, _ in self.starts()], ["ultra-magnus"])
        self.assertIn("supplementary=False", self.chain_log(rd)[0])
        self.assertEqual(chain_result(r.stdout)["supplementary"], [])

    # ---------------------------------------------------------------- (c)/(d) a failing supplementary leg never touches the chain's result
    def test_a_crashing_supplementary_leg_leaves_the_bachelor_verdict_and_exit_code_alone(self) -> None:
        rd = self.case / "lane-CRASH"
        r = self.run_chain(rd, self.specs, env=self.env(FAKE_UM_LEG_MODE="crash"))
        self.assertEqual(r.returncode, 0, "a supplementary crash must not move the chain exit code: " + r.stdout + r.stderr)
        res = chain_result(r.stdout)
        self.assertEqual((res["exitCode"], res["stopReason"], res["ran"], res["failed"], res["notRun"]), (0, None, 2, 0, 0))
        self.assertEqual([(l["status"], l["outcome"]) for l in res["legs"]], [("RAN", "PASS"), ("RAN", "PASS")])
        self.assertEqual([(s["status"], s["outcome"], s["printedOutcome"], s["legExit"]) for s in res["supplementary"]], [("FAILED", "INVALID", "PASS", 5)] * 2)
        log = self.chain_log(rd)
        self.assertEqual(len([l for l in log if " SUPPLEMENTARY_FAILED venue=ultra-magnus role=supplementary" in l]), 2)
        self.assertFalse([l for l in log if " LEG_FAILED " in l], "a supplementary failure must never read as a Bachelor LEG_FAILED")
        self.assertEqual([v for _, v, _ in self.starts()], ["bachelor", "ultra-magnus", "bachelor", "ultra-magnus"], "the chain went on to its next Bachelor leg")

    def test_a_supplementary_fail_outcome_never_becomes_a_bachelor_outcome(self) -> None:
        r = self.run_chain(self.case / "lane-UMFAIL", self.specs[:1], env=self.env(FAKE_UM_LEG_MODE="fail"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        res = chain_result(r.stdout)
        self.assertEqual([(l["status"], l["outcome"]) for l in res["legs"]], [("RAN", "PASS")])
        self.assertEqual([(s["status"], s["outcome"]) for s in res["supplementary"]], [("RAN", "FAIL")])
        self.assertEqual(res["exitCode"], 0)

    def test_a_bachelor_failure_keeps_its_own_exit_code_whatever_ultra_magnus_does(self) -> None:
        # Bachelor probe UNKNOWN stops the chain with exit 6; no supplementary run follows a leg that never ran, and the exit code is Bachelor's
        rd = self.case / "lane-BU"
        r = self.run_chain(rd, self.specs, env=self.env(FAKE_QUIET_MODES="UNKNOWN", FAKE_UM_QUIET_MODE="QUIET"))
        self.assertEqual(r.returncode, 6, r.stdout + r.stderr)
        self.assertEqual(self.starts(), [])
        self.assertEqual(chain_result(r.stdout)["supplementary"], [])

    # ---------------------------------------------------------------- (f) build not staged on ultra-magnus: typed skip, nothing staged or built
    def test_build_not_staged_on_ultra_magnus_is_a_typed_skip(self) -> None:
        rd = self.case / "lane-NOSTAGE"
        r = self.run_chain(rd, self.specs, share=self.empty_share)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([v for _, v, _ in self.starts()], ["bachelor", "bachelor"])
        log = self.chain_log(rd)
        skip = [l for l in log if " SUPPLEMENTARY_SKIPPED " in l]
        self.assertEqual(len(skip), 2, log)
        self.assertRegex(skip[0], r"SUPPLEMENTARY_SKIPPED leg=01-sp-a-spec trigger=after-leg reason=BUILD_NOT_STAGED venue=ultra-magnus role=supplementary detail=no staged build manifest for aaaaaaaaaaaa at ")
        self.assertEqual([s["reason"] for s in chain_result(r.stdout)["supplementary"]], ["BUILD_NOT_STAGED"] * 2)
        self.assertEqual([c for c in self.quiet_calls() if "venue=ultra-magnus" in c], [], "nothing is probed or claimed on a venue the build is not staged on")
        self.assertFalse((self.claims / "ultra-magnus.claim.json").exists())
        self.assertEqual(list((self.empty_share).rglob("*")), [self.empty_share / "cache"], "the chain staged something on the venue share")

    # LEASE-BATCH-PLAN-ARTIFACT-PREFLIGHT-1: a staged manifest whose digests are sound but whose cached exe/dll/zip is absent or differs would fail in the leg job after the claim and the probe
    def _um_share_with_build(self, absent: str = "", differs: str = "") -> Path:
        import hashlib
        share = self.case / "um-share-built"
        (share / "cache").mkdir(parents=True)
        s = SHA[:12]
        names = {"exe": f"MLVApp-playback-attr-3-cuda-{s}.exe", "dll": f"igpu_recon_cuda-playback-attr-3-cuda-{s}.dll", "packageZip": f"MLVApp-playback-attr-3-cuda-{s}-pkg.zip"}
        doc = {"sourceCommit": SHA, "dllPairManifestSha256": "f" * 64, "pendingSymbolPresence": False}
        for label, name in names.items():
            content = f"{label}-bytes".encode("utf-8")
            doc[label] = {"sha256": hashlib.sha256(content).hexdigest()}
            if label != absent:
                (share / "cache" / name).write_bytes(b"other bytes" if label == differs else content)
        (share / "cache" / f"playback-attr-3-cuda-{s}-build.json").write_text(json.dumps(doc), encoding="utf-8")
        return share

    def test_a_staged_build_with_a_missing_or_stale_cached_artifact_is_a_typed_skip_and_a_complete_one_still_runs(self) -> None:
        for label, kwargs, reason in (("missing exe", {"absent": "exe"}, "BUILD_ARTIFACT_MISSING"), ("stale dll", {"differs": "dll"}, "BUILD_ARTIFACT_MISMATCH")):
            with self.subTest(case=label):
                share = self._um_share_with_build(**kwargs)
                rd = self.case / f"lane-ART-{kwargs and list(kwargs)[0]}"
                r = self.run_chain(rd, self.specs, share=share)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual([v for _, v, _ in self.starts()], ["bachelor", "bachelor"])
                self.assertEqual([s["reason"] for s in chain_result(r.stdout)["supplementary"]], [reason] * 2)
                self.assertEqual([c for c in self.quiet_calls() if "venue=ultra-magnus" in c], [], "nothing is probed or claimed on a venue whose staged build cannot run")
                self.assertFalse((self.claims / "ultra-magnus.claim.json").exists())
                shutil.rmtree(share, ignore_errors=True)
                for p in (self.quiet_log, self.leg_log):
                    p.unlink(missing_ok=True)
        share = self._um_share_with_build()
        r = self.run_chain(self.case / "lane-ART-ok", self.specs, share=share)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ultra-magnus", [v for _, v, _ in self.starts()], "a complete staged build still runs the supplementary leg")

    def test_a_spec_without_a_supplementary_block_is_skipped_once_and_never_touches_ultra_magnus(self) -> None:
        rd = self.case / "lane-NOCRIT"
        r = self.run_chain(rd, [self.bare], "-SupplementaryHeldSec", "0")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([v for _, v, _ in self.starts()], ["bachelor"])
        skip = [l for l in self.chain_log(rd) if " SUPPLEMENTARY_SKIPPED " in l]
        self.assertEqual(len(skip), 1, "a permanent skip is logged once, not at every checkpoint")
        self.assertIn("reason=NO_SUPPLEMENTARY_CRITERIA", skip[0])
        self.assertEqual([c for c in self.quiet_calls() if "venue=ultra-magnus" in c], [])

    # ---------------------------------------------------------------- module helpers
    def test_spec_skip_reasons(self) -> None:
        both = self.case / "both.json"
        both.write_text(json.dumps({"legId": "x", "backends": ["cuda", "cpu"], "criteria": {"acceptance": {"cuda": [], "cpu": []}, "supplementary": {"cpu": []}}}), encoding="utf-8")
        junk = self.case / "junk.json"
        junk.write_text("{not json", encoding="utf-8")
        def skip(path: Path, backend: str) -> str:
            return pwsh_text(f"Import-Module '{MODULE}' -Force -DisableNameChecking; $r = Get-VenueSupplementarySpecSkip -SpecPath '{path}' -Backend '{backend}'; if ($null -eq $r) {{ 'NONE' }} else {{ $r }}")
        self.assertEqual(skip(both, "cpu"), "NONE")                                  # an EMPTY list is still a block: the leg is informational there
        self.assertEqual(skip(both, "cuda"), "NO_SUPPLEMENTARY_CRITERIA")
        self.assertEqual(skip(both, ""), "NO_SUPPLEMENTARY_CRITERIA")                # '' means the spec's first backend (cuda), as Invoke-VenueLeg does
        self.assertEqual(skip(both, "gpu"), "BACKEND_NOT_IN_SPEC")
        self.assertEqual(skip(junk, "cpu"), "SPEC_UNREADABLE")
        self.assertEqual(skip(self.bare, "cpu"), "NO_SUPPLEMENTARY_CRITERIA")
        real = DV / "legs" / "m16-1243-look.json"
        spec = json.loads(real.read_text(encoding="utf-8"))
        for b in spec["backends"]:
            want = "NONE" if b in spec["criteria"].get("supplementary", {}) else "NO_SUPPLEMENTARY_CRITERIA"
            self.assertEqual(skip(real, b), want, b)

    def test_bounded_process_kills_a_child_past_its_timeout(self) -> None:
        t0 = time.time()
        out = pwsh_text(f"Import-Module '{MODULE}' -Force -DisableNameChecking; $r = Invoke-VenueBoundedProcess -FilePath '{PWSH}' -ArgumentList @('-NoProfile','-Command','Start-Sleep 60') -TimeoutSec 2; \"$($r.timedOut) [$($r.exit)]\"")
        self.assertEqual(out, "True []")
        self.assertLess(time.time() - t0, 45)
        ok = pwsh_text(f"Import-Module '{MODULE}' -Force -DisableNameChecking; $r = Invoke-VenueBoundedProcess -FilePath '{PWSH}' -ArgumentList @('-NoProfile','-Command','Write-Output hi; exit 3') -TimeoutSec 60; \"$($r.timedOut) $($r.exit) $($r.stdout.Trim())\"")
        self.assertEqual(ok, "False 3 hi")

    def test_staged_manifest_states(self) -> None:
        q = f"Import-Module '{MODULE}' -Force -DisableNameChecking; $r = Get-VenueStagedBuildManifest -AgentShare '{{}}' -SourceCommit '{SHA}' -TimeoutSec 20; \"$($r.state) $($r.sha256)\""
        self.assertEqual(pwsh_text(q.format(self.um_share)), f"STAGED {self.staged_sha}")
        self.assertEqual(pwsh_text(q.format(self.empty_share)), "NOT_STAGED")   # pwsh_text strips the trailing space of an empty sha

    def test_idle_check_reads_the_claim_and_the_line_only(self) -> None:
        q = f"Import-Module '{MODULE}' -Force -DisableNameChecking; $r = Get-VenueSupplementaryIdle -Venue ultra-magnus -ClaimDir '{self.claims}'; \"$($r.idle) $($r.claimState) $($r.liveTickets)\""
        self.assertEqual(pwsh_text(q), "True FREE 0")
        self.write_um_claim()
        self.assertEqual(pwsh_text(q), "False LIVE 0")
        own = q.replace("-ClaimDir", "-OwnNonce um-fixture-nonce -ClaimDir")   # the revalidation under our own claim: our claim is not "held", only a live ticket is
        self.assertEqual(pwsh_text(own), "True LIVE 0")
        self.assertEqual(pwsh_text(q.replace("-ClaimDir", "-OwnNonce someone-else -ClaimDir")), "False LIVE 0")

    # ---------------------------------------------------------------- VENUE-UM-SUPP-REMOTE-IDENTITY-AND-CACHE-BOUND-1 (a): a killed client's remote job outlives it
    JOB_ID = "dve-sp-a-ultra-magnus-cpu-20261021000000"

    def hang_env(self, **extra: str) -> dict:
        return self.env(FAKE_UM_HANG_LEAF="sp-a.json", FAKE_UM_SHARE=str(self.um_share), **extra)

    def test_a_timed_out_client_leaves_the_venue_busy_with_its_job_id_until_the_result_lands(self) -> None:
        rd = self.case / "lane-RJBUSY"
        # -SupplementaryHeldSec 0: each leg is offered on ultra-magnus BEFORE Bachelor runs it too, so two legs give three offers: sp-a (held: its client hangs and is killed),
        # sp-b (held: its remote job unproven -> busy), sp-b again after its Bachelor leg, which is what lands sp-a's remote job result in the share (-> reconciled, runs).
        r = self.run_chain(rd, self.specs, "-SupplementaryHeldSec", "0", "-TestSupplementaryCapSec", "3", env=self.hang_env(FAKE_UM_JOB_ID=self.JOB_ID, FAKE_JOB_DONE_ON_LEG="sp-b.json"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)   # a supplementary never moves the chain exit code
        self.assertEqual([(l, v) for l, v, _ in self.starts()], [("sp-a.json", "ultra-magnus"), ("sp-a.json", "bachelor"), ("sp-b.json", "bachelor"), ("sp-b.json", "ultra-magnus")])
        res = chain_result(r.stdout)
        self.assertEqual((res["ran"], res["exitCode"]), (2, 0))
        sup = res["supplementary"]
        self.assertEqual([s["status"] for s in sup], ["TIMEOUT", "SKIPPED", "RAN"])
        # the killed client's remote job: identity kept, venue not idle -- the released claim proved nothing
        self.assertEqual((sup[0]["trigger"], sup[0]["remoteJobIds"], sup[0]["remoteJobState"], sup[0]["venueIdle"]), ("held", [self.JOB_ID], "NOT_FINISHED", False))
        self.assertEqual((sup[1]["trigger"], sup[1]["reason"]), ("held", "UM_REMOTE_JOB_UNRESOLVED"))
        self.assertIn(f"jobId={self.JOB_ID} state=NOT_FINISHED", sup[1]["detail"])
        self.assertEqual((sup[2]["trigger"], sup[2]["name"]), ("after-leg", "02-sp-b-spec"))
        log = "\n".join(self.chain_log(rd))
        self.assertRegex(log, r"SUPPLEMENTARY_REMOTE_JOB leg=01-sp-a-spec jobId=" + self.JOB_ID + r" state=NOT_FINISHED venueIdle=False")
        self.assertIn(f"SUPPLEMENTARY_REMOTE_JOB_RECONCILED leg=02-sp-b-spec jobId={self.JOB_ID} state=FINISHED", log)
        self.assertFalse((self.claims / "ultra-magnus.claim.json").exists(), "the claim itself is still released")

    # V-01 (sol r1): the guard is armed at the timeout, before any fallible step. Writing the client's output file is one: a directory squatting on its path makes WriteAllText throw
    # AFTER the client timed out and BEFORE the identity was kept.
    def run_timeout_with_unwritable_output(self, name: str, **env_extra: str) -> tuple:
        rd = self.case / name
        (rd / "leg-01-sp-a-spec-ultra-magnus-supplementary.out.txt").mkdir(parents=True)
        r = self.run_chain(rd, self.specs, "-SupplementaryHeldSec", "0", "-TestSupplementaryCapSec", "3", env=self.hang_env(**env_extra))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return rd, chain_result(r.stdout)["supplementary"], "\n".join(self.chain_log(rd))

    def test_v01_timeout_then_unwritable_output_keeps_the_job_identity_and_refuses_the_next_leg_until_finished(self) -> None:
        rd, sup, log = self.run_timeout_with_unwritable_output("lane-V01", FAKE_UM_JOB_ID=self.JOB_ID, FAKE_JOB_DONE_ON_LEG="sp-b.json")
        self.assertEqual((sup[0]["status"], sup[0]["reason"]), ("SKIPPED", "SUPPLEMENTARY_ERROR"), "the output write failed after the timeout")
        # the unresolved identity is retained: the next leg is refused (not run) and names the killed client's job
        self.assertEqual((sup[1]["status"], sup[1]["reason"]), ("SKIPPED", "UM_REMOTE_JOB_UNRESOLVED"))
        self.assertIn(f"jobId={self.JOB_ID} state=NOT_FINISHED", sup[1]["detail"])
        # ... and it runs only once the result has landed
        self.assertEqual([(l, v) for l, v, _ in self.starts()], [("sp-a.json", "ultra-magnus"), ("sp-a.json", "bachelor"), ("sp-b.json", "bachelor"), ("sp-b.json", "ultra-magnus")])
        self.assertEqual((sup[2]["status"], sup[2]["name"]), ("RAN", "02-sp-b-spec"))
        self.assertIn(f"SUPPLEMENTARY_REMOTE_JOB_RECONCILED leg=02-sp-b-spec jobId={self.JOB_ID} state=FINISHED", log)
        self.assertFalse((self.claims / "ultra-magnus.claim.json").exists(), "the claim itself is still released")

    def test_v01_timeout_then_unwritable_output_with_no_identity_stays_busy(self) -> None:
        rd, sup, log = self.run_timeout_with_unwritable_output("lane-V01NOID")   # the client printed no DVE_JOB_ID before it hung: nothing to look up
        self.assertEqual((sup[0]["status"], sup[0]["reason"]), ("SKIPPED", "SUPPLEMENTARY_ERROR"))
        self.assertTrue(all(s["status"] == "SKIPPED" for s in sup), [s["status"] for s in sup])
        self.assertTrue(all(s["reason"] == "UM_REMOTE_JOB_UNRESOLVED" for s in sup[1:]) and len(sup) >= 2, [s.get("reason") for s in sup])
        self.assertNotIn("ultra-magnus", [v for l, v, _ in self.starts() if l == "sp-b.json"], "a missing identity must remain busy: no later leg runs on ultra-magnus")
        self.assertNotIn("SUPPLEMENTARY_REMOTE_JOB_RECONCILED", log)

    # ---------------------------------------------------------------- VENUE-LEG-PRINT-DVE-JOB-ID-1: the REAL leg prints the id it submits under, so the reconcile above has something to read
    LEG = DV / "Invoke-VenueLeg.ps1"

    def test_the_leg_prints_dve_job_id_before_each_submit_with_the_id_it_submits_under(self) -> None:
        text = self.LEG.read_text(encoding="utf-8")
        code = [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]
        calls = [k for k, l in enumerate(code) if re.match(r"\$\w+ = Submit-VenueJob\b", l)]
        self.assertEqual(len(calls), 2, "the health probe and the leg: a third submit must print its id too, and extend this row")
        for k in calls:
            submitted = re.search(r"-JobId\s+(\"[^\"]*\"|\$\w+)", code[k])
            printed = re.match(r"Write-Output\s+\"DVE_JOB_ID=(.*?)\"\s*(?:#.*)?$", code[k - 1])
            self.assertIsNotNone(submitted, code[k])
            self.assertIsNotNone(printed, f"the statement before the submit must print the job id on stdout: {code[k]}")
            self.assertEqual(printed.group(1), submitted.group(1).strip('"'), "the printed id is the submitted one")
        self.assertEqual(len([l for l in code if "DVE_JOB_ID=" in l]), 2, "one print per submit")
        # um-run's own output stays suppressed: only the id line is added. Since LEASE-NOT-AFTER-SUBMITTER-DEADLINE-TIMING-1 the information stream is
        # collected (6>&1 into the submit's records, to measure the drop) instead of discarded (6>$null); either way it is never written out.
        fn = text[text.index("function Submit-VenueJob("):text.index("function Write-NotAfterVerdict(")]
        self.assertIn("6>&1 |", fn)
        # (r2: only um-run's 'submitted' and 'job not-after:' records are kept, every output object still is)
        self.assertIn("$records.Add($_)", fn)
        self.assertNotRegex(fn, r"Write-(Host|Output|Information)\b", "the collected records are never written out")

    def test_a_real_leg_killed_at_the_chain_timeout_leaves_its_printed_id_to_reconcile_not_unknown(self) -> None:
        # The real Invoke-VenueLeg.ps1 (offline test mode, stub generator, a stub um-run that submits and then never returns) is the ultra-magnus leg; the fake leg stays Bachelor's.
        # The chain's bounded child kills it at the cap; the id it printed before the submit must be what the chain reconciles (NOT_FINISHED: no result landed), never UNKNOWN.
        from tools.repo_hygiene import test_dual_venue_evidence as dve
        h = dve.RunnerHarness()
        h.addCleanup = self.addCleanup
        h.make_harness()
        spec = h.write_spec(leg_id="sp-real")
        body = json.loads(spec.read_text(encoding="utf-8"))
        body["backends"] = ["cpu"]
        body["criteria"] = {"acceptance": {"cpu": []}, "supplementary": {"cpu": []}}
        spec.write_text(json.dumps(body), encoding="utf-8")
        stub_cfg = h.tmp / "stub.json"
        stub_cfg.write_text(json.dumps({"genLog": str(h.tmp / "gen.log"), "genRefusal": None, "clipContentSha256": dve.CLIP_CONTENT_SHA}), encoding="utf-8")
        um_log = h.tmp / "um-submits.log"
        um_stub = h.tmp / "um-run-hangs.ps1"
        um_stub.write_text("param([string]$ScriptPath,[string]$JobId,[string]$AgentShare,[int]$TimeoutSec,[int]$MaxQueueWaitSec,[int]$MaxClaimedWaitSec)\n"
                           "Add-Content -LiteralPath $env:FAKE_UM_STUB_LOG -Value $JobId\nStart-Sleep -Seconds 120\n", encoding="utf-8")
        wrapper = h.tmp / "leg-wrapper.ps1"
        wrapper.write_text(
            "param([string]$Venue, [string]$LegSpec, [string]$SourceCommit, [string]$BuildManifestSha256, [string]$Backend = '', [string]$SheetCopyDir = '')\n"
            f"if ($Venue -ne 'ultra-magnus') {{ & '{self.fake_leg}' -Venue $Venue -LegSpec $LegSpec -SourceCommit $SourceCommit -BuildManifestSha256 $BuildManifestSha256; exit 0 }}\n"
            "$be = @(); if ($Backend) { $be = @('-Backend', $Backend) }   # the chain passes -Backend only when the spec lists more than one\n"
            f"& '{self.LEG}' -Venue $Venue -LegSpec $LegSpec -SourceCommit '{h.sha}' -BuildManifestSha256 $BuildManifestSha256 @be -VenueTablePath '{h.table}' "
            f"-ReceiptRoot '{h.receipts}' -OfflineTestMode -UmRunScript '{um_stub}' -GeneratorScript '{h.gen}' -WorkDir '{h.tmp / 'work'}' -ConsentPath '{h.consent}' "
            f"-RepoRoot '{ROOT}' -Actor unit-test\nexit 0\n", encoding="utf-8")
        rd = self.case / "lane-REALLEG"
        args = self.chain_args(rd, [spec], "-SupplementaryHeldSec", "0", "-TestSupplementaryCapSec", "10")
        args[args.index("-LegScript") + 1] = str(wrapper)
        r = subprocess.run(args, capture_output=True, text=True, timeout=300, env=self.env(DVE_STUB=str(stub_cfg), FAKE_UM_STUB_LOG=str(um_log), FAKE_UM_SHARE=str(self.um_share)))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        sup = chain_result(r.stdout)["supplementary"]
        leg_out = next(rd.glob("leg-*-ultra-magnus-supplementary.out.txt"), None)
        self.assertEqual(sup[0]["status"], "TIMEOUT", f"{[s.get('status') for s in sup]}: {leg_out.read_text(encoding='utf-8') if leg_out else 'no leg output'}")
        submitted = [l.strip() for l in um_log.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertEqual(len(submitted), 1, f"the health probe is the one submit the stub never answers: {submitted}")
        self.assertRegex(submitted[0], r"^dve-sp-real-ultra-magnus-cpu-\d{14}-health$")
        self.assertEqual((sup[0]["remoteJobIds"], sup[0]["remoteJobState"], sup[0]["venueIdle"]), ([submitted[0]], "NOT_FINISHED", False))

    # ---------------------------------------------------------------- the pieces, and (b): the cache read keeps its -TimeoutSec bound
    def test_remote_job_ids_and_states_are_read_by_name_and_a_stalled_cache_read_keeps_its_bound(self) -> None:
        # One process (the wall-time budget). Part 1: ids come only from printed well-formed lines; a state is FINISHED only when every named result landed. Part 2: a read
        # that never answers ([Threading.Thread]::Sleep cannot be interrupted by a pipeline stop, like a UNC read blocked in the kernel) returns UNREACHABLE at TimeoutSec: an
        # asynchronous stop followed by a synchronous Dispose() would wait the whole 20 s, and the host would wait for the abandoned thread at exit unless
        # Complete-VenueProcess leaves first. The bound is TimeoutSec (2) plus a small fixed margin.
        share = self.um_share
        script = self.case / "pieces.ps1"
        script.write_text(
            f"Import-Module '{MODULE}' -Force -DisableNameChecking\n"
            "function ids([string]$t) { $v = @(Get-VenueRemoteJobIds -Text $t); \"n=$($v.Count)[$($v -join '|')]\" }\n"
            "ids \"DVE_LEGSET=x`nDVE_JOB_ID=dve-1`nDVE_JOB_ID=dve-2`r`nDVE_JOB_ID=dve-1`n\"; ids ''; ids \"DVE_OUTCOME=PASS`n\"\n"
            "ids \"DVE_JOB_ID=..\\..\\x`n\"; ids \"DVE_JOB_ID=a b`n\"; ids \"DVE_JOB_ID=a/b`n\"; ids \"DVE_JOB_ID=-rf`nDVE_JOB_ID=`n\"\n"
            f"function st($j) {{ (Get-VenueRemoteJobState -AgentShare '{share}' -JobId $j -TimeoutSec 20).state }}\n"
            "st @(); st @('dve-1'); st @('dve-1', '')\n"
            f"New-Item -ItemType Directory -Force -Path '{share}\\inbox','{share}\\outbox' | Out-Null; Set-Content -LiteralPath '{share}\\inbox\\dve-1.job.ps1' -Value x; st @('dve-1')\n"
            f"Set-Content -LiteralPath '{share}\\outbox\\dve-1.result.json' -Value '{{}}'; st @('dve-1'); st @('dve-1', 'dve-2')\n"
            f"Set-Content -LiteralPath '{share}\\outbox\\dve-2.result.json' -Value '{{}}'; st @('dve-1', 'dve-2')\n"
            "$t0 = [DateTime]::UtcNow\n"
            f"$r = Get-VenueStagedBuildManifest -AgentShare '{share}' -SourceCommit '{SHA}' -TimeoutSec 2 -ProbeScript {{ param($p) [Threading.Thread]::Sleep(20000); 'late' }}\n"
            "\"$($r.state) [$($r.sha256)] $($r.error) inside=$([int][Math]::Floor(([DateTime]::UtcNow - $t0).TotalSeconds))\"\n"
            "Complete-VenueProcess -ExitCode 7\nexit 7\n", encoding="utf-8")
        t0 = time.time()
        r = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script)], capture_output=True, text=True, timeout=90)
        elapsed = time.time() - t0
        lines = r.stdout.splitlines()
        self.assertRegex(lines[-1], r"^UNREACHABLE \[\] no answer in 2 s inside=[23]$", r.stdout + r.stderr)
        self.assertLess(elapsed, 14, f"the stalled read broke the bound: the whole process took {elapsed:.1f}s with a 20 s stuck read and -TimeoutSec 2")
        self.assertEqual(r.returncode, 7)
        self.assertEqual(lines[:-1], [
            "n=2[dve-1|dve-2]",  # every printed id, in order, distinct (the client may have submitted a health probe job before the leg job)
            "n=0[]", "n=0[]",    # nothing printed = no id
            "n=1[]", "n=1[]", "n=1[]", "n=1[]",   # an id that is not a plain file name (it becomes a file name on the venue share) stays as ONE empty entry: named, never looked up
            "UNKNOWN", "NOT_FINISHED", "UNKNOWN",   # no id; an id whose result is absent; an unusable id
            "NOT_FINISHED",                          # the job file is still in the inbox: queued or running
            "FINISHED",                              # its result landed
            "NOT_FINISHED",                          # one of two results landed
            "FINISHED",                              # both
        ], r.stdout + r.stderr)

    # ---------------------------------------------------------------- mutations: the guards are what the scenarios rely on
    def test_mutation_opt_out_ignored_runs_ultra_magnus_anyway(self) -> None:
        mut = self.mutated_runner("-not $NoSupplementary", "$true", 1)
        self.run_chain(self.case / "lane-MOPT", self.specs[:1], "-NoSupplementary", runner=mut)
        self.assertIn("ultra-magnus", [v for _, v, _ in self.starts()], "the mutant should have run the supplementary leg despite -NoSupplementary")

    def test_mutation_held_check_removed_leaves_only_the_after_leg_offer(self) -> None:
        mut = self.mutated_runner("-ge $SupplementaryHeldSec) { Invoke-Supplementary $leg 'held' }", "-ge 999999) { Invoke-Supplementary $leg 'held' }", 2)
        self.run_chain(self.case / "lane-MHELD", self.specs[:1], "-SupplementaryHeldSec", "2", runner=mut, env=self.env(FAKE_QUIET_MODES="BUSY,QUIET,QUIET", FAKE_QUIET_SLEEP_PRE="3"))
        self.assertEqual([(l, v) for l, v, _ in self.starts()], [("sp-a.json", "bachelor"), ("sp-a.json", "ultra-magnus")], "without the held check the offer can only come after the leg")

    def test_mutation_offer_after_a_not_measured_probe_removed_reproduces_sol_r2(self) -> None:
        mut = self.mutated_runner("\n            if ($suppEnabled -and ([DateTime]::UtcNow - $legWaitStart).TotalSeconds -ge $SupplementaryHeldSec) {\n", "\n            if ($false) {\n", 1)   # the 12-space pre-claim block only (r4 added a 16-space twin under the claim)
        r = self.run_chain(self.case / "lane-MADM", self.specs[:1], "-SupplementaryHeldSec", "2", runner=mut, env=self.env(FAKE_QUIET_MODES="UNKNOWN", FAKE_QUIET_SLEEP_PRE="3"))
        self.assertEqual(r.returncode, 6)
        self.assertEqual(self.starts(), [], "without the offer on a not-measured probe the admission-exhaustion path makes no offer at all (the defect)")

    def test_mutation_offer_after_an_under_claim_not_measured_probe_removed_reproduces_sol_r3(self) -> None:
        mut = self.mutated_runner("\n                if ($suppEnabled -and ([DateTime]::UtcNow - $legWaitStart).TotalSeconds -ge $SupplementaryHeldSec) {\n", "\n                if ($false) {\n", 1)
        r = self.run_chain(self.case / "lane-MUNDER", self.specs[:1], "-SupplementaryHeldSec", "2", runner=mut, env=self.env(FAKE_QUIET_MODES="QUIET,UNKNOWN", FAKE_QUIET_SLEEP_UNDER="3"))
        self.assertEqual(r.returncode, 6)
        self.assertEqual(self.starts(), [], "without the offer on an under-claim not-measured probe the leg gets no offer at all (the defect)")

    def test_mutation_supplementary_failure_leaking_into_the_chain_exit_is_caught(self) -> None:
        # a mutant that lets a supplementary crash set the chain exit code to 7 reproduces the defect test_a_crashing_supplementary_leg_... guards against
        mut = self.mutated_runner("$sStatus = $(if ($sr.timedOut) { 'TIMEOUT' } elseif ($sr.exit -ne 0) { 'FAILED' } else { 'RAN' })",
                                  "$sStatus = $(if ($sr.timedOut) { 'TIMEOUT' } elseif ($sr.exit -ne 0) { 'FAILED' } else { 'RAN' }); if ($sStatus -ceq 'FAILED') { $script:exitCode = 7 }", 1)
        r = self.run_chain(self.case / "lane-MLEAK", self.specs[:1], runner=mut, env=self.env(FAKE_UM_LEG_MODE="crash"))
        self.assertEqual(r.returncode, 7, "the mutant must reproduce the defect the real runner is tested against")

    def test_mutation_supplementary_result_counted_as_a_bachelor_leg_is_caught(self) -> None:
        # a mutant that files the supplementary run in the chain's own result list moves `ran` and `legs`: the idle test pins both
        mut = self.mutated_runner("    $suppResults.Add([pscustomobject]$r)\n", "    $suppResults.Add([pscustomobject]$r); $results.Add([pscustomobject]$r)\n", 1)
        r = self.run_chain(self.case / "lane-MCOUNT", self.specs[:1], runner=mut)
        res = chain_result(r.stdout)
        self.assertNotEqual([l["status"] for l in res["legs"]], ["RAN"], "the mutant must reproduce the defect: a supplementary entry inside legs")

    # ---------------------------------------------------------------- the contract stays in the file
    def test_runner_documents_the_supplementary_contract_and_stays_ascii(self) -> None:
        text = RUNNER.read_text(encoding="utf-8")
        header = "\n".join(l for l in text.splitlines() if l.startswith("#"))
        for token in ("SUPPLEMENTARY LEGS (VENUE-UM-SUPPLEMENTARY-WHEN-BACHELOR-HELD-1", "-NoSupplementary", "SUPPLEMENTARY_SKIPPED leg=<n> trigger=<t> reason=<R>", "BUILD_NOT_STAGED", "never in `legs`",
                      "REMOTE JOB IDENTITY (VENUE-UM-SUPP-REMOTE-IDENTITY-AND-CACHE-BOUND-1)", "UM_REMOTE_JOB_UNRESOLVED", "SUPPLEMENTARY_REMOTE_JOB_RECONCILED", "DVE_JOB_ID=<id>"):
            self.assertIn(token, header)
        self.assertIn("Complete-VenueProcess -ExitCode $exitCode", text)
        self.assertIn("[switch]$NoSupplementary", text)
        self.assertIn("[ValidateRange(0, 86400)][int]$SupplementaryHeldSec = 1800", text)
        self.assertTrue(all(b < 128 for b in MODULE.read_bytes()) and all(b < 128 for b in RUNNER.read_bytes()))
        for needle in ("Get-CimInstance", "Win32_Process", "Get-Process -Name", "Get-WmiObject"):
            self.assertNotIn(needle, text, f"{needle}: the idle check reads the claim and the line, never a process scan")


if __name__ == "__main__":
    unittest.main()
