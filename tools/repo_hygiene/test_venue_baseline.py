"""Tests for UM-REGULAR-BASELINE-1: the tracked regular baseline runner tools/profiling/dual-venue/Invoke-VenueBaseline.ps1.

Owner directive 2026-10-18 (Layi): "I'm curious how playback performs on ultramagnus so id like regular benchmarking there as well when possible". Ultra-Magnus
becomes a regular benchmark venue; Bachelor stays the ACCEPTANCE venue (venues.json roles are not touched); every Ultra-Magnus number is supplementary and carries the
hub-VM load at run time (the hub VM is a guest on Ultra-Magnus).

WHAT IS PINNED
  * the plan (-WhatIf): both standard legs, both backends, the chain command line with -PreferQuietWindows; nothing is created, read or run;
  * the role in every row/plan line comes from venues.json (a changed table changes the role; Bachelor is acceptance for the display-matrix card);
  * one trend line per leg that touched the venue, with utc, venue, role, source sha, leg, backend, the receipt's own fps / drop / stall figures, the receipt path and
    hubLoad {commitGiB, cpuPercent} sampled at the start AND the end;
  * typed non-zero exits: a busy agent (20), an unreachable share (21), a build that is not staged (22), a gate refusal (23), a leg that died (25), a chain with no
    result (27), an unresolvable master (26), a trend file outside .claude-state (2);
  * the total wall-clock cap (24): a chain that never returns is killed at the cap and the run exits typed instead of waiting.

Offline only: the chain and the hub-load sampler are stubs passed with -ChainScript / -HubLoadScript, the agent share and the claim directory are temp directories.
Nothing here reaches a venue, starts MLVApp, or touches the board's claim path. Windows-only (PowerShell tools).
"""

from __future__ import annotations

import hashlib
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
BASELINE = DV / "Invoke-VenueBaseline.ps1"
VENUES = DV / "venues.json"
PWSH = shutil.which("pwsh")
requires_windows_pwsh = unittest.skipIf(PWSH is None or sys.platform != "win32", "needs pwsh on Windows")

SHA = "a" * 40
SHORT = SHA[:12]
MANIFEST_BYTES = b'{"sourceCommit": "' + SHA.encode() + b'"}\n'
MANIFEST_SHA = hashlib.sha256(MANIFEST_BYTES).hexdigest()
LEG_IDS = ("m16-1243-look", "m16-1243-display-fullscreen-s1")
EXIT = {"OK": 0, "USAGE": 2, "AGENT_BUSY": 20, "SHARE_UNREACHABLE": 21, "BUILD_NOT_STAGED": 22, "GATE_REFUSED": 23,
        "BASELINE_CAP_EXCEEDED": 24, "LEG_FAILED": 25, "SOURCE_COMMIT_UNRESOLVED": 26, "CHAIN_FAILED": 27}

# Invoke-VenueChain.ps1 stand-in. FAKE_CHAIN_MODE: ok | legfail | deadline | gate | noresult | sleep. It logs each call to FAKE_CHAIN_LOG and, for ok, writes a
# receipt per leg with the metrics the real receipt carries and prints CHAIN_RESULT= exactly as the real chain does.
FAKE_CHAIN = r"""
[CmdletBinding()]
param([string]$Venue, [string]$LegSpec, [string]$Backend = '', [string]$RunDir, [string]$SourceCommit, [string]$BuildManifestSha256, [switch]$PreferQuietWindows,
      [int]$QuietMaxWaitSec = 0, [string]$DeadlineUtc = '', [string]$Purpose = '')
$mode = $(if ($env:FAKE_CHAIN_MODE) { $env:FAKE_CHAIN_MODE } else { 'ok' })
Add-Content -LiteralPath $env:FAKE_CHAIN_LOG -Value ("CALL backend=$Backend venue=$Venue commit=$SourceCommit manifest=$BuildManifestSha256 prefer=$($PreferQuietWindows.IsPresent) deadline=$DeadlineUtc legspec=$LegSpec pid=$PID")
New-Item -ItemType Directory -Force -Path $RunDir | Out-Null
$specs = @($LegSpec -split ',' | Where-Object { $_ })
function Emit($legs, [int]$code, $stop, $procExit = $null) {
    $r = [ordered]@{ schema = 'mlv-app/venue-chain-result/v1'; venue = $Venue; runDir = $RunDir; exitCode = $code; stopReason = $stop; legs = @($legs)
        ran = @($legs | Where-Object { $_.status -ceq 'RAN' }).Count; failed = @($legs | Where-Object { $_.status -ceq 'LEG_FAILED' }).Count; notRun = @($legs | Where-Object { $_.status -ceq 'NOT_RUN' }).Count }
    'CHAIN_RESULT=' + ($r | ConvertTo-Json -Compress -Depth 6)
    exit $(if ($null -ne $procExit) { $procExit } else { $code })
}
$n = 0
$legs = foreach ($s in $specs) {
    $n++
    $legId = [IO.Path]::GetFileNameWithoutExtension($s)
    $name = ('{0:00}-{1}-{2}' -f $n, $legId, $Backend)
    switch ($mode) {
        { $_ -in 'ok', 'mismatch', 'undocumented', 'noreceipt' } {
            $rd = Join-Path $env:FAKE_ROOT "receipts\CARD-1\$legId\$Venue"
            New-Item -ItemType Directory -Force -Path $rd | Out-Null
            $rp = Join-Path $rd ("rcpt-$Backend-$n.json")
            $rc = [ordered]@{ schema = 'mlv-app/dual-venue-receipt/v1'; receiptId = "rcpt-$Backend-$n"; legId = $legId; outcome = 'PASS'
                venue = [ordered]@{ name = $Venue; role = 'supplementary' }
                metrics = [ordered]@{ cpuFrames = 389; gpuFramesTotal = $(if ($Backend -eq 'cuda') { 600 } else { 0 }); skippedOrUnpresentedRatio = 0.35058430717863104; cpuPaceTimelineFps = $(if ($Backend -eq 'cpu') { 11.185 } else { $null })
                    presentMonTemporalMaxGapMs = 41.7; presentMonTemporalGapKind = 'positive-interval-rows'; presentMonStatus = 'ok'
                    presentMonStats = [ordered]@{ count = 12; meanMs = 16.7; p99Ms = 33.4; fpsEquivalentMean = $(if ($Backend -eq 'cuda') { 59.9 } else { $null }) } }
                playback = [ordered]@{ sourceAdvanced = 600; requiredSourceFrames = 600; nativeFps = 23.976; paceFps = 23.976 }
                display = [ordered]@{ requestedMode = 'fullscreen' }; scale = [ordered]@{ requestedScale = 1 } }
            Set-Content -LiteralPath $rp -Value ($rc | ConvertTo-Json -Depth 8)
            if ($mode -ceq 'noreceipt') { Remove-Item -LiteralPath $rp -Force }
            [pscustomobject]@{ name = $name; spec = $s; backend = $Backend; status = 'RAN'; verdict = 'QUIET'; outcome = 'PASS'; legExit = 0; receipt = $rp }
        }
        'legfail' { [pscustomobject]@{ name = $name; spec = $s; backend = $Backend; status = 'LEG_FAILED'; verdict = 'QUIET'; outcome = 'INVALID'; printedOutcome = 'PASS'; legExit = 5; receipt = $null } }
        default { [pscustomobject]@{ name = $name; spec = $s; backend = $Backend; status = 'NOT_RUN'; verdict = 'DEADLINE'; reason = 'deadline-before-leg' } }
    }
}
switch ($mode) {
    'ok' { Emit $legs 0 $null }
    'noreceipt' { Emit $legs 0 $null }
    'mismatch' { Emit $legs 0 $null 7 }
    'undocumented' { Emit $legs 9 $null }
    'legfail' { Emit $legs 7 $null }
    'deadline' { Emit $legs 3 'DEADLINE' }
    'gate' { Emit $legs 4 'GATE_UNREADABLE' }
    'noresult' { 'the chain died'; exit 0 }
    'sleep' { Start-Sleep -Seconds 300; exit 0 }
}
"""

# Hub-load sampler stand-in: the first call reads one value, every later call another, so a row's start and end differ.
FAKE_HUB_LOAD = r"""
$f = $env:FAKE_HUB_COUNT
$n = $(if (Test-Path -LiteralPath $f) { [int](Get-Content -LiteralPath $f) } else { 0 })
Set-Content -LiteralPath $f -Value ($n + 1)
if ($n -eq 0) { '{"commitGiB":10.5,"cpuPercent":12.5}' } else { '{"commitGiB":11.5,"cpuPercent":40.0}' }
"""


def utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f0Z")


def process_start_iso(pid: int) -> str:
    out = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", f"(Get-Process -Id {pid}).StartTime.ToUniversalTime().ToString('o')"],
                         capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    return out.stdout.strip()


def pid_alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    return str(pid) in out.stdout


@requires_windows_pwsh
class VenueBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="um-baseline-")
        self.addCleanup(tmp.cleanup)
        self.case = Path(tmp.name)
        self.state = self.case / ".claude-state"
        self.trend = self.state / "dual-venue" / "baseline-trend.jsonl"
        self.run_dir = self.state / "dual-venue" / "baseline-runs" / "run1"
        self.claims = self.case / "claims"
        self.share = self.case / "share"
        (self.share / "cache").mkdir(parents=True)
        (self.share / "cache" / f"playback-attr-3-cuda-{SHORT}-build.json").write_bytes(MANIFEST_BYTES)
        self.chain = self.case / "fake-chain.ps1"
        self.chain.write_text(FAKE_CHAIN, encoding="utf-8")
        self.hub = self.case / "fake-hub-load.ps1"
        self.hub.write_text(FAKE_HUB_LOAD, encoding="utf-8")
        self.chain_log = self.case / "chain-calls.log"
        self.fake_root = self.case / "fake-root"
        self.fake_root.mkdir()

    def env(self, **extra: str) -> dict[str, str]:
        e = dict(os.environ)
        e.update({"FAKE_CHAIN_LOG": str(self.chain_log), "FAKE_ROOT": str(self.fake_root), "FAKE_HUB_COUNT": str(self.case / "hub-count.txt")})
        e.update(extra)
        return e

    def args(self, *extra: str, share: Path | None = None, commit: str | None = SHA, stub_chain: bool = True) -> list[str]:
        a = [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(BASELINE), "-TrendFile", str(self.trend), "-RunDir", str(self.run_dir),
             "-AgentShare", str(share or self.share), "-ClaimDir", str(self.claims), "-HubLoadScript", str(self.hub)]
        if stub_chain:       # the plan test leaves the real chain script in place: that is the path the plan must print
            a += ["-ChainScript", str(self.chain)]
        if commit:
            a += ["-SourceCommit", commit]
        return a + list(extra)

    def run_baseline(self, *extra: str, mode: str = "ok", timeout: int = 240, **kw) -> subprocess.CompletedProcess:
        return subprocess.run(self.args(*extra, **kw), capture_output=True, text=True, timeout=timeout, env=self.env(FAKE_CHAIN_MODE=mode), stdin=subprocess.DEVNULL)

    def chain_calls(self) -> list[str]:
        return self.chain_log.read_text(encoding="utf-8").splitlines() if self.chain_log.exists() else []

    def trend_rows(self) -> list[dict]:
        return [json.loads(line) for line in self.trend.read_text(encoding="utf-8").splitlines() if line.strip()] if self.trend.exists() else []

    def result(self, stdout: str) -> dict:
        line = [l for l in stdout.splitlines() if l.startswith("BASELINE_RESULT=")]
        self.assertTrue(line, stdout)
        return json.loads(line[-1][len("BASELINE_RESULT="):])

    # ---------------------------------------------------------------- the plan (-WhatIf)
    def test_whatif_plan_names_both_legs_and_both_backends_and_runs_nothing(self) -> None:
        missing_share = self.case / "no-such-share"
        p = self.run_baseline("-WhatIf", share=missing_share, stub_chain=False)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        legs = re.findall(r"BASELINE_LEG (\d)/4 leg=(\S+) card=(\S+) backend=(\S+) role=(\S+)", p.stdout)
        self.assertEqual(len(legs), 4, p.stdout)
        self.assertEqual({(l[1], l[3]) for l in legs}, {(leg, b) for leg in LEG_IDS for b in ("cuda", "cpu")})
        cmds = [l for l in p.stdout.splitlines() if l.startswith("BASELINE_CMD")]
        self.assertEqual(len(cmds), 2, p.stdout)
        for backend, cmd in zip(("cuda", "cpu"), cmds):
            self.assertIn(f"backend={backend}", cmd)
            self.assertIn(f"-Backend {backend}", cmd)
            self.assertIn("Invoke-VenueChain.ps1", cmd)
            self.assertIn("-Venue ultra-magnus", cmd)
            self.assertIn("-PreferQuietWindows", cmd)
            for leg in LEG_IDS:
                self.assertIn(f"{leg}.json", cmd)
        # nothing was created, run or read: no trend file, no run dir, no chain call, no hub-load sample, and a missing share did not matter
        self.assertFalse(self.trend.exists())
        self.assertFalse(self.run_dir.exists())
        self.assertEqual(self.chain_calls(), [])
        self.assertFalse((self.case / "hub-count.txt").exists())

    def test_whatif_default_venue_is_ultra_magnus(self) -> None:
        p = self.run_baseline("-WhatIf")
        self.assertIn("BASELINE_PLAN venue=ultra-magnus", p.stdout)

    # ---------------------------------------------------------------- roles come from venues.json
    def test_role_for_ultra_magnus_is_supplementary_from_venues_json(self) -> None:
        table = json.loads(VENUES.read_text(encoding="utf-8"))
        self.assertEqual(table["roles"]["DUAL-VENUE-DISPLAY-MATRIX-1"]["ultra-magnus"], "supplementary")
        p = self.run_baseline("-WhatIf")
        roles = {m for m in re.findall(r"role=(\S+)", p.stdout)}
        self.assertEqual(roles, {"supplementary"}, p.stdout)

    def test_role_is_read_from_the_table_not_hardcoded(self) -> None:
        # Bachelor is ACCEPTANCE for the display-matrix card and falls back to defaultRole (supplementary) for the look card: the table says so, the runner follows.
        p = self.run_baseline("-WhatIf", "-Venue", "bachelor")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        by_leg: dict[tuple[str, str], str] = {}   # (leg id, backend) -> the role the plan printed for it, filled from the BASELINE_LEG lines just below
        for leg, backend, role in re.findall(r"leg=(\S+) card=\S+ backend=(\S+) role=(\S+)", p.stdout):
            by_leg[(leg, backend)] = role
        self.assertEqual(by_leg[("m16-1243-display-fullscreen-s1", "cuda")], "acceptance")
        self.assertEqual(by_leg[("m16-1243-look", "cuda")], "supplementary")
        # and a table that turns ultra-magnus into the acceptance venue for that card changes the plan
        table = json.loads(VENUES.read_text(encoding="utf-8"))
        table["roles"]["DUAL-VENUE-DISPLAY-MATRIX-1"]["ultra-magnus"] = "acceptance"
        alt = self.case / "venues-alt.json"
        alt.write_text(json.dumps(table), encoding="utf-8")
        p2 = self.run_baseline("-WhatIf", "-VenueTablePath", str(alt))
        self.assertIn("leg=m16-1243-display-fullscreen-s1 card=DUAL-VENUE-DISPLAY-MATRIX-1 backend=cuda role=acceptance", p2.stdout)

    def test_the_standard_legs_are_unchanged_and_venue_roles_untouched(self) -> None:
        table = json.loads(VENUES.read_text(encoding="utf-8"))
        for card, roles in table["roles"].items():
            self.assertEqual(roles, {"bachelor": "acceptance", "ultra-magnus": "supplementary"}, card)
        self.assertEqual(table["defaultRole"], "supplementary")

    # ---------------------------------------------------------------- the source commit default
    def test_default_source_commit_is_fork_master(self) -> None:
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
        unresolved = self.run_baseline("-WhatIf", "-RepoRoot", str(repo), commit=None)
        self.assertEqual(unresolved.returncode, EXIT["SOURCE_COMMIT_UNRESOLVED"], unresolved.stdout + unresolved.stderr)
        self.assertIn("reason=SOURCE_COMMIT_UNRESOLVED", unresolved.stdout)
        git("update-ref", "refs/remotes/fork/master", head)
        ok = self.run_baseline("-WhatIf", "-RepoRoot", str(repo), commit=None)
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self.assertIn(f"sourceCommit={head}", ok.stdout)

    # ---------------------------------------------------------------- the trend lines
    def test_a_run_appends_one_line_per_leg_with_every_field(self) -> None:
        p = self.run_baseline()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        rows = self.trend_rows()
        self.assertEqual(len(rows), 4, p.stdout)
        self.assertEqual({(r["leg"], r["backend"]) for r in rows}, {(leg, b) for leg in LEG_IDS for b in ("cuda", "cpu")})
        for r in rows:
            for key in ("utc", "venue", "role", "sourceCommit", "leg", "backend", "fps", "drops", "stall", "receiptPath", "hubLoad", "status", "outcome"):
                self.assertIn(key, r, key)
            self.assertEqual(r["venue"], "ultra-magnus")
            self.assertEqual(r["role"], "supplementary")
            self.assertEqual(r["sourceCommit"], SHA)
            self.assertRegex(r["utc"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
            self.assertEqual(r["status"], "RAN")
            self.assertEqual(r["outcome"], "PASS")
            self.assertTrue(Path(r["receiptPath"]).is_file(), r["receiptPath"])
            # the receipt went through the evidence-bearing validator and the row carries its verdict: the stub's receipt has no admission or evidence, so it re-derives to nothing
            self.assertIn(r["receiptVerification"]["status"], ("INVALID", "INCOMPLETE"), r["receiptVerification"])
            self.assertIs(r["receiptVerification"]["valid"], False)
            # the hub-VM load, sampled at the start AND the end of the run (the stub reads 10.5/12.5 first, 11.5/40.0 after)
            hl = r["hubLoad"]
            self.assertEqual((hl["start"]["commitGiB"], hl["start"]["cpuPercent"]), (10.5, 12.5))
            self.assertEqual((hl["end"]["commitGiB"], hl["end"]["cpuPercent"]), (11.5, 40.0))
        # the figures are the receipt's own, verbatim
        cpu = next(r for r in rows if r["backend"] == "cpu")
        cuda = next(r for r in rows if r["backend"] == "cuda")
        self.assertEqual(cpu["fps"]["cpuPaceTimelineFps"], 11.185)
        self.assertEqual(cuda["fps"]["presentMonFpsEquivalentMean"], 59.9)
        self.assertEqual(cuda["fps"]["nativeFps"], 23.976)
        self.assertEqual(cpu["drops"]["skippedOrUnpresentedRatio"], 0.35058430717863104)
        self.assertEqual(cuda["drops"]["gpuFramesTotal"], 600)
        self.assertEqual(cpu["drops"]["sourceAdvanced"], 600)
        self.assertEqual(cpu["stall"]["presentMonTemporalMaxGapMs"], 41.7)
        # the chain was called once per backend, with the staged manifest's sha, the commit, -PreferQuietWindows and a deadline
        calls = self.chain_calls()
        self.assertEqual([c.split()[1] for c in calls], ["backend=cuda", "backend=cpu"])
        for c in calls:
            self.assertIn(f"manifest={MANIFEST_SHA}", c)
            self.assertIn(f"commit={SHA}", c)
            self.assertIn("prefer=True", c)
            self.assertIn("venue=ultra-magnus", c)
            self.assertRegex(c, r"deadline=\d{4}-\d\d-\d\dT")
        res = self.result(p.stdout)
        self.assertEqual((res["exitCode"], res["reason"], res["rowsAppended"]), (0, "OK", 4))

    def test_a_second_run_appends_instead_of_overwriting(self) -> None:
        self.assertEqual(self.run_baseline().returncode, 0)
        (self.case / "hub-count.txt").unlink()
        self.assertEqual(self.run_baseline().returncode, 0)
        self.assertEqual(len(self.trend_rows()), 8)

    def test_a_dead_leg_still_gets_a_row_with_no_figures(self) -> None:
        p = self.run_baseline(mode="legfail")
        self.assertEqual(p.returncode, EXIT["LEG_FAILED"], p.stdout + p.stderr)
        rows = self.trend_rows()
        self.assertEqual(len(rows), 4)       # both chains still ran: a dead leg does not stop the other backend
        self.assertTrue(all(r["status"] == "LEG_FAILED" and r["outcome"] == "INVALID" and r["receiptPath"] is None and r["fps"] is None for r in rows), rows)
        self.assertIn("reason=LEG_FAILED", p.stdout)

    # ---------------------------------------------------------------- the chain's exit code gates every row (evidence-completeness guard)
    def assert_no_ran_row_and_no_figures(self, rows: list[dict], expected: int) -> None:
        self.assertEqual(len(rows), expected, rows)
        for r in rows:
            self.assertEqual(r["status"], "EVIDENCE_INCOMPLETE", r)
            self.assertEqual(r["outcome"], "INVALID", r)
            self.assertTrue(r["failure"], r)
            self.assertTrue(r["fps"] is None and r["drops"] is None and r["stall"] is None, r)

    def test_a_chain_exit_that_disagrees_with_its_chain_result_records_no_ran_row(self) -> None:
        p = self.run_baseline(mode="mismatch")      # CHAIN_RESULT says exitCode 0, the process exits 7
        self.assertEqual(p.returncode, EXIT["CHAIN_FAILED"], p.stdout + p.stderr)
        self.assertIn("reason=CHAIN_FAILED", p.stdout)
        self.assert_no_ran_row_and_no_figures(self.trend_rows(), 2)
        self.assertEqual(len(self.chain_calls()), 1)      # an untrusted chain stops the plan: the cpu chain never started

    def test_an_undocumented_chain_exit_records_no_ran_row(self) -> None:
        p = self.run_baseline(mode="undocumented")      # CHAIN_RESULT and the process agree on 9, which the chain never documents
        self.assertEqual(p.returncode, EXIT["CHAIN_FAILED"], p.stdout + p.stderr)
        self.assert_no_ran_row_and_no_figures(self.trend_rows(), 2)

    def test_a_ran_leg_with_no_readable_receipt_is_an_incomplete_row_and_a_failed_run(self) -> None:
        p = self.run_baseline(mode="noreceipt")
        self.assertEqual(p.returncode, EXIT["LEG_FAILED"], p.stdout + p.stderr)
        self.assertIn("reason=LEG_FAILED", p.stdout)
        self.assert_no_ran_row_and_no_figures(self.trend_rows(), 4)      # a dead receipt does not stop the other backend

    def test_a_clean_chain_rows_carry_no_failure_text(self) -> None:
        self.assertEqual(self.run_baseline().returncode, 0)
        self.assertTrue(all(r["failure"] is None and r["status"] == "RAN" for r in self.trend_rows()))

    # ---------------------------------------------------------------- typed refusals
    def test_busy_agent_exits_typed_and_never_calls_the_chain(self) -> None:
        self.claims.mkdir(parents=True)
        expires = datetime.now(timezone.utc) + timedelta(hours=3)
        (self.claims / "ultra-magnus.claim.json").write_text(json.dumps({
            "schema": "mlv-app/venue-claim/v1", "venue": "ultra-magnus", "ownerPid": os.getpid(), "ownerCreatedUtc": process_start_iso(os.getpid()),
            "host": os.environ.get("COMPUTERNAME", ""), "runDir": "C:\\elsewhere\\lane-OTHER", "purpose": "fixture", "claimedUtc": utc_iso(datetime.now(timezone.utc)),
            "expiresUtc": utc_iso(expires), "nonce": "fixture-nonce"}), encoding="utf-8")
        p = self.run_baseline()
        self.assertEqual(p.returncode, EXIT["AGENT_BUSY"], p.stdout + p.stderr)
        self.assertIn("reason=AGENT_BUSY", p.stdout)
        self.assertEqual(self.chain_calls(), [])
        self.assertFalse(self.trend.exists())
        self.assertEqual(self.result_file()["reason"], "AGENT_BUSY")

    def test_a_stale_claim_is_not_busy(self) -> None:
        self.claims.mkdir(parents=True)
        (self.claims / "ultra-magnus.claim.json").write_text(json.dumps({
            "schema": "mlv-app/venue-claim/v1", "venue": "ultra-magnus", "ownerPid": 999999, "ownerCreatedUtc": "2026-01-01T00:00:00.0000000Z",
            "host": os.environ.get("COMPUTERNAME", ""), "runDir": "C:\\elsewhere\\lane-DEAD", "purpose": "fixture", "claimedUtc": "2026-01-01T00:00:00.0000000Z",
            "expiresUtc": utc_iso(datetime.now(timezone.utc) + timedelta(hours=3)), "nonce": "dead"}), encoding="utf-8")
        p = self.run_baseline()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_unreachable_share_exits_typed(self) -> None:
        p = self.run_baseline(share=self.case / "no-such-share")
        self.assertEqual(p.returncode, EXIT["SHARE_UNREACHABLE"], p.stdout + p.stderr)
        self.assertIn("reason=SHARE_UNREACHABLE", p.stdout)
        self.assertEqual(self.chain_calls(), [])

    def test_unstaged_build_exits_typed_and_a_given_manifest_skips_the_lookup(self) -> None:
        (self.share / "cache" / f"playback-attr-3-cuda-{SHORT}-build.json").unlink()
        p = self.run_baseline()
        self.assertEqual(p.returncode, EXIT["BUILD_NOT_STAGED"], p.stdout + p.stderr)
        self.assertIn("reason=BUILD_NOT_STAGED", p.stdout)
        self.assertEqual(self.chain_calls(), [])
        given = "c" * 64
        p2 = self.run_baseline("-BuildManifestSha256", given)
        self.assertEqual(p2.returncode, 0, p2.stdout + p2.stderr)
        self.assertTrue(all(f"manifest={given}" in c for c in self.chain_calls()))

    def test_gate_refusal_exits_typed_and_stops_before_the_second_backend(self) -> None:
        p = self.run_baseline(mode="gate")
        self.assertEqual(p.returncode, EXIT["GATE_REFUSED"], p.stdout + p.stderr)
        self.assertIn("reason=GATE_REFUSED", p.stdout)
        self.assertEqual(len(self.chain_calls()), 1)
        self.assertFalse(self.trend.exists())

    def test_venue_never_quiet_with_no_leg_run_is_busy(self) -> None:
        p = self.run_baseline(mode="deadline")
        self.assertEqual(p.returncode, EXIT["AGENT_BUSY"], p.stdout + p.stderr)
        self.assertEqual(len(self.chain_calls()), 1)
        self.assertFalse(self.trend.exists())

    def test_chain_with_no_result_is_chain_failed(self) -> None:
        p = self.run_baseline(mode="noresult")
        self.assertEqual(p.returncode, EXIT["CHAIN_FAILED"], p.stdout + p.stderr)
        self.assertIn("reason=CHAIN_FAILED", p.stdout)

    def test_trend_file_outside_claude_state_is_refused(self) -> None:
        outside = self.case / "elsewhere" / "trend.jsonl"
        a = self.args()
        a[a.index("-TrendFile") + 1] = str(outside)
        p = subprocess.run(a, capture_output=True, text=True, timeout=120, env=self.env(), stdin=subprocess.DEVNULL)
        self.assertEqual(p.returncode, EXIT["USAGE"], p.stdout + p.stderr)
        self.assertIn(".claude-state", p.stdout)
        self.assertEqual(self.chain_calls(), [])

    # ---------------------------------------------------------------- the cap
    def test_total_wall_clock_cap_kills_a_chain_that_never_returns(self) -> None:
        t0 = time.monotonic()
        p = self.run_baseline("-MaxTotalSec", "10", "-KillGraceSec", "0", mode="sleep", timeout=120)
        elapsed = time.monotonic() - t0
        self.assertEqual(p.returncode, EXIT["BASELINE_CAP_EXCEEDED"], p.stdout + p.stderr)
        self.assertIn("reason=BASELINE_CAP_EXCEEDED", p.stdout)
        self.assertLess(elapsed, 90, "the runner waited past its cap; the stub chain sleeps 300 s")
        calls = self.chain_calls()
        self.assertEqual(len(calls), 1, calls)     # the cap also ends the plan: the cpu chain never started
        pid = int(re.search(r"pid=(\d+)", calls[0]).group(1))
        deadline = time.monotonic() + 15
        while pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.5)
        self.assertFalse(pid_alive(pid), "the chain process outlived the cap")
        self.assertFalse(self.trend.exists())

    # ---------------------------------------------------------------- static guards
    def test_the_runner_never_starts_the_app_or_a_scheduled_task_or_edits_the_autopilot(self) -> None:
        text = BASELINE.read_text(encoding="utf-8")
        code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        for forbidden in ("MLVApp.exe", "Register-ScheduledTask", "schtasks", "hub-autopilot", "Start-Process"):
            self.assertNotIn(forbidden, code, forbidden)
        self.assertTrue(text.isascii(), "ASCII-only .ps1")
        self.assertIn("-PreferQuietWindows", code)
        self.assertIn("Get-DvVenueRole", code)

    def result_file(self) -> dict:
        return json.loads((self.run_dir / "baseline-result.json").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
