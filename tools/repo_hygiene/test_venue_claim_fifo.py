"""Tests for VENUE-CLAIM-FIFO-1: the ticketed claim queue in tools/profiling/dual-venue/VenueChain.psm1 and its use in Invoke-VenueChain.ps1.

WHAT IS PINNED (the claim was a 30 s polling race with no FIFO: PLAYBACK-CPU-PACE-GAP-1 r5 q02 lost 5 handoffs in a row, 60 min, with four lanes on Bachelor)
  * (a) two ticket holders, A ticketed before B: when the claim frees, A gets it even though B polls first.
  * (b) a ticket whose owner is dead (dead pid) or silent (heartbeat) does not hold the line: the next waiter removes it, and gets the claim.
  * (c) a chain that releases the claim on GATE_BUSY keeps its ticket (the same ticket file, still ahead of the waiter behind it).
  * (d) BACK-COMPAT: a legacy (ticketless) claimant still takes a free claim; a ticket holder proceeds after it releases and never waits on a legacy waiter.
  * the claim state is visible: -ClaimStatus lists every ticket's owner run dir, pid and age; the chain logs the line on CLAIM_HELD.
  * the chain wires it: first-come-first-served across real chain processes, the ticket kept across a GATE_BUSY under claim, a dead ticket removed and logged.
  * r2 (keys on d40e8664): (B1) the keep is counted: ONE kept cycle (-TicketKeepMax), then the next GATE_BUSY under the claim drops the ticket so a ready follower
    takes the claim; (B2) a ticket's probe-phase window is derived from the probe's real upper bound (the chain passes -MaxAdmissionWaitSec to the probe), so a live
    head 700 s into a long probe is not stale while a dead pid still is at once; equal-tick tickets sort by pid then start ticks, never by enumeration order.
  * r3 (hub-found blocker on 65f46ac2, r8v on Bachelor): the chain's -MaxAdmissionWaitSec reached only the FAKE probe; the real Wait-VenueQuiet.ps1 declared no such parameter, so
    every quiet probe failed binding. Now: every -Name the chain passes is a declared Live parameter of the real probe (PowerShell AST on both sides), the probe forwards the
    bound to um-run.ps1 (AST over the call), and the real script binds it (and range-checks it) when run against a nonexistent scratch share.

Offline only: the claim and ticket folders live in a temp dir, Wait-VenueQuiet and Invoke-VenueLeg are the fakes of test_venue_chain_runner.py, and the clock of
the ticket logic is injected (-NowUtc) so no test waits for a real heartbeat to expire. The module tests are deterministic. The chain-process FIFO test races
real processes: on the pre-FIFO code it failed 4 runs of 4 (the later arrival polls every 1 s, the earlier one every 5-6 s, so the later one takes the claim); on this code it passed every run.
VENUE_CLAIM_FIFO_DV_DIR points the whole file at another copy of tools/profiling/dual-venue (used to record the RED runs against fork/master).
Windows-only (PowerShell tools).
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.repo_hygiene import test_venue_chain_runner as base
from tools.repo_hygiene.venue_fixture_modules import copy_with_siblings

DV = Path(os.environ["VENUE_CLAIM_FIFO_DV_DIR"]) if os.environ.get("VENUE_CLAIM_FIFO_DV_DIR") else base.DV
RUNNER = DV / "Invoke-VenueChain.ps1"
MODULE = DV / "VenueChain.psm1"
QUIET = DV / "Wait-VenueQuiet.ps1"
UM_RUN = DV.parent / "um-run.ps1"
PWSH = base.PWSH
requires_windows_pwsh = base.requires_windows_pwsh


def utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@requires_windows_pwsh
class VenueClaimFifoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="venue-fifo-"))
        cls.fake_quiet = cls.tmp / "fake-quiet.ps1"
        cls.fake_quiet.write_text(base.FAKE_QUIET, encoding="utf-8")
        cls.fake_leg = cls.tmp / "fake-leg.ps1"
        cls.fake_leg.write_text(base.FAKE_LEG, encoding="utf-8")
        cls.specs = []
        for name in ("fx-a", "fx-b", "fx-c"):
            p = cls.tmp / f"{name}.json"
            p.write_text(json.dumps({"legId": name}), encoding="utf-8")
            cls.specs.append(p)

    @classmethod
    def tearDownClass(cls) -> None:
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self) -> None:
        self.case = Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp))
        self.claims = self.case / "claims"
        self.claim_path = self.claims / "bachelor.claim.json"
        self.ticket_dir = self.claims / "bachelor.tickets"
        self.quiet_log = self.case / "quiet-calls.log"
        self.leg_log = self.case / "leg-calls.log"
        self._children: list[subprocess.Popen] = []

    def tearDown(self) -> None:
        for p in self._children:
            if p.poll() is None:
                p.kill()
            p.wait()

    # ---------------------------------------------------------------- helpers
    def ps(self, script: str, module: Path | None = None) -> dict:
        """Run a PowerShell scenario that imports the module and prints one JSON line last."""
        f = self.case / f"scenario-{len(list(self.case.glob('scenario-*.ps1')))}.ps1"
        f.write_text(f"$ErrorActionPreference = 'Stop'\nImport-Module '{module or MODULE}' -Force -DisableNameChecking\n$d = '{self.claims}'\n{script}\n", encoding="utf-8")
        r = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(f)], capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise AssertionError(f"scenario failed ({r.returncode}):\n{r.stdout}\n{r.stderr}")
        return json.loads([l for l in r.stdout.splitlines() if l.strip()][-1])

    def sleeper(self) -> subprocess.Popen:
        p = subprocess.Popen([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "Start-Sleep -Seconds 120"])
        self._children.append(p)
        time.sleep(1.0)   # let the process exist with a readable start time
        return p

    def env(self, **extra: str) -> dict:
        e = dict(os.environ)
        e.update({"FAKE_QUIET_LOG": str(self.quiet_log), "FAKE_LEG_LOG": str(self.leg_log), "FAKE_CLAIM_PATH": str(self.claim_path), "FAKE_LEG_ROOT": str(self.case)})
        e.update(extra)
        return e

    def chain_args(self, run_dir: Path, spec: Path, claim_poll: int = 1, deadline_sec: int = 120, runner: Path | None = None, quiet: Path | None = None, extra: tuple[str, ...] = ()) -> list[str]:
        return [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(runner or RUNNER), "-Venue", "bachelor", "-LegSpec", str(spec), "-RunDir", str(run_dir),
                "-SourceCommit", base.SHA, "-BuildManifestSha256", base.MANIFEST, "-ClaimDir", str(self.claims), "-QuietScript", str(quiet or self.fake_quiet), "-LegScript", str(self.fake_leg),
                "-CooldownSec", "0", "-ClaimPollSec", str(claim_poll), "-DeadlineUtc", utc_iso(datetime.now(timezone.utc) + timedelta(seconds=deadline_sec)), *extra]

    def start_chain(self, run_dir: Path, spec: Path, claim_poll: int = 1, runner: Path | None = None, deadline_sec: int = 120, **env: str) -> subprocess.Popen:
        # stdout goes to a FILE, never a pipe: the chain echoes every log line (long paths, one line per change in the queue) and a chain whose pipe is not being drained
        # blocks on write once it is full (4 KB on Windows), so it would stall while the test waits on another chain's pipe
        out_path = self.case / f"stdout-{run_dir.name}.txt"
        with open(out_path, "w", encoding="utf-8") as out_file:
            p = subprocess.Popen(self.chain_args(run_dir, spec, claim_poll, deadline_sec=deadline_sec, runner=runner), stdout=out_file, stderr=subprocess.STDOUT, text=True, env=self.env(**env))
        p.out_path = out_path   # type: ignore[attr-defined]
        self._children.append(p)
        return p

    def finish(self, p: subprocess.Popen, timeout: float) -> str:
        """Wait for a chain started by start_chain and return everything it printed."""
        p.wait(timeout=timeout)
        return Path(p.out_path).read_text(encoding="utf-8", errors="replace")   # type: ignore[attr-defined]

    def wait_for(self, predicate, what: str, timeout: float = 60.0) -> None:
        t0 = time.time()
        while time.time() - t0 < timeout:
            if predicate():
                return
            time.sleep(0.2)
        self.fail(f"timed out waiting for {what}")

    def log_has(self, run_dir: Path, needle: str) -> bool:
        f = run_dir / "chain.log"
        return f.exists() and needle in f.read_text(encoding="utf-8")

    def chain_log(self, run_dir: Path) -> list[str]:
        return (run_dir / "chain.log").read_text(encoding="utf-8").splitlines()

    def leg_starts(self) -> list[str]:
        if not self.leg_log.exists():
            return []
        return [l.split()[2] for l in self.leg_log.read_text(encoding="utf-8").splitlines() if l.startswith("START ")]

    # ---------------------------------------------------------------- (a) first come, first served
    A_FIRST = """
$A = New-VenueTicket -Venue bachelor -RunDir 'run-A' -ClaimDir $d
Start-Sleep -Milliseconds 30
$B = New-VenueTicket -Venue bachelor -RunDir 'run-B' -ClaimDir $d
$b1 = Request-VenueClaim -Venue bachelor -RunDir 'run-B' -ClaimDir $d -Ticket $B
$claimAfterB1 = Test-Path -LiteralPath (Get-VenueClaimPath -Venue bachelor -ClaimDir $d)
$a1 = Request-VenueClaim -Venue bachelor -RunDir 'run-A' -ClaimDir $d -Ticket $A
$b2 = Request-VenueClaim -Venue bachelor -RunDir 'run-B' -ClaimDir $d -Ticket $B
$rel = Release-VenueClaim -Venue bachelor -Nonce $a1.claim.nonce -ClaimDir $d
$endA = Remove-VenueTicket $A
$b3 = Request-VenueClaim -Venue bachelor -RunDir 'run-B' -ClaimDir $d -Ticket $B
[pscustomobject]@{ b1 = $b1.state; b1Granted = [bool]$b1.granted; b1Pos = $b1.position; claimAfterB1 = $claimAfterB1; a1 = $a1.state; a1Granted = [bool]$a1.granted
  b2 = $b2.state; b2Holder = $b2.claim.runDir; b2Pos = $b2.position; rel = $rel; endA = $endA; b3 = $b3.state; b3Granted = [bool]$b3.granted } | ConvertTo-Json -Compress
"""

    def test_a_the_oldest_ticket_gets_the_claim_even_when_the_younger_polls_first(self) -> None:
        r = self.ps(self.A_FIRST)
        self.assertEqual((r["b1"], r["b1Granted"], r["b1Pos"]), ("QUEUED", False, 2), "B polled first on a FREE claim and must wait behind A's older ticket")
        self.assertFalse(r["claimAfterB1"], "B took the claim although A's ticket is older")
        self.assertEqual((r["a1"], r["a1Granted"]), ("TAKEN", True))
        self.assertEqual((r["b2"], r["b2Holder"], r["b2Pos"]), ("QUEUED", "run-A", 2), "B must see A as the holder while A's ticket is ahead")
        self.assertEqual((r["rel"], r["endA"]), ("RELEASED", "REMOVED"))
        self.assertEqual((r["b3"], r["b3Granted"]), ("TAKEN", True), "once A is gone the next ticket gets the claim")

    def test_mutation_without_the_head_check_the_younger_waiter_wins(self) -> None:
        text = MODULE.read_text(encoding="utf-8")
        anchor = "if ($head.Count -eq 0 -or $head[0].name -cne $t.name) {"   # the head check: only the oldest READY live ticket may try the claim
        after = "if ($headAfter.Count -gt 0 -and $headAfter[0].name -cne $t.name) {"   # r7 A1: the same check made again after a grant (the claim given back when an older ready ticket is ahead): the mutant has neither
        self.assertEqual(text.count(anchor), 1, "mutation anchor moved")
        self.assertEqual(text.count(after), 1, "mutation anchor (after a grant) moved")
        mut = copy_with_siblings(MODULE, Path(tempfile.mkdtemp(prefix="mut-", dir=self.case)), text.replace(anchor, "if ($false) {").replace(after, "if ($false) {"))
        r = self.ps(self.A_FIRST, module=mut)
        self.assertTrue(r["b1Granted"], "the mutant must reproduce the race: the younger waiter that polls first takes the claim")

    # ---------------------------------------------------------------- (b) a dead or silent ticket does not hold the line
    def test_b_a_dead_owner_ticket_is_removed_and_the_next_waiter_gets_the_claim(self) -> None:
        dead, alive = self.sleeper(), self.sleeper()   # each scenario is its own short-lived pwsh, so B's ticket needs a live owner of its own
        self.ps(f"$A = New-VenueTicket -Venue bachelor -RunDir 'run-A' -ClaimDir $d -OwnerPid {dead.pid}\nStart-Sleep -Milliseconds 30\n"
                f"$B = New-VenueTicket -Venue bachelor -RunDir 'run-B' -ClaimDir $d -OwnerPid {alive.pid}\n'{{}}'")
        waiting = self.ps("$B = Get-VenueTickets -Venue bachelor -ClaimDir $d | Where-Object { $_.ticket.runDir -eq 'run-B' } | Select-Object -First 1\n"
                          "$r = Request-VenueClaim -Venue bachelor -RunDir 'run-B' -ClaimDir $d -Ticket ([pscustomobject]@{ name = $B.name; path = $B.path; nonce = $B.ticket.nonce })\n"
                          "[pscustomobject]@{ state = $r.state; stale = @($r.staleTickets).Count } | ConvertTo-Json -Compress")
        self.assertEqual((waiting["state"], waiting["stale"]), ("QUEUED", 0), "A is alive: B must wait")
        dead.kill(); dead.wait()
        r = self.ps("$B = Get-VenueTickets -Venue bachelor -ClaimDir $d | Where-Object { $_.ticket.runDir -eq 'run-B' } | Select-Object -First 1\n"
                    "$r = Request-VenueClaim -Venue bachelor -RunDir 'run-B' -ClaimDir $d -Ticket ([pscustomobject]@{ name = $B.name; path = $B.path; nonce = $B.ticket.nonce })\n"
                    "[pscustomobject]@{ state = $r.state; granted = [bool]$r.granted; reasons = @($r.staleTickets | ForEach-Object { $_.reason }); "
                    "left = @(Get-VenueTickets -Venue bachelor -ClaimDir $d | ForEach-Object { $_.ticket.runDir }) } | ConvertTo-Json -Compress")
        self.assertEqual((r["state"], r["granted"]), ("TAKEN", True), r)
        self.assertEqual(r["reasons"], ["dead-pid"], "the dead waiter's ticket is removed once, with its reason")
        self.assertEqual(r["left"], ["run-B"], "the stale ticket file must be gone")

    def test_b_a_silent_owner_ticket_goes_stale_after_its_heartbeat_window(self) -> None:
        r = self.ps("""
$A = New-VenueTicket -Venue bachelor -RunDir 'run-A' -ClaimDir $d -StaleAfterSec 90
Start-Sleep -Milliseconds 30
$B = New-VenueTicket -Venue bachelor -RunDir 'run-B' -ClaimDir $d -StaleAfterSec 90
$t0 = [DateTime]::UtcNow
$early = Request-VenueClaim -Venue bachelor -RunDir 'run-B' -ClaimDir $d -Ticket $B -TicketStaleAfterSec 90 -NowUtc $t0.AddSeconds(60).ToString('o')
$late = Request-VenueClaim -Venue bachelor -RunDir 'run-B' -ClaimDir $d -Ticket $B -TicketStaleAfterSec 90 -NowUtc $t0.AddSeconds(200).ToString('o')
[pscustomobject]@{ early = $early.state; earlyStale = @($early.staleTickets).Count; late = $late.state; lateGranted = [bool]$late.granted
  reasons = @($late.staleTickets | ForEach-Object { $_.reason }); aGone = -not (Test-Path -LiteralPath $A.path) } | ConvertTo-Json -Compress
""")
        self.assertEqual((r["early"], r["earlyStale"]), ("QUEUED", 0), "A's heartbeat is 60 s old of 90 s: still live")
        self.assertEqual((r["late"], r["lateGranted"], r["reasons"]), ("TAKEN", True, ["heartbeat"]))
        self.assertTrue(r["aGone"])

    def test_b_a_heartbeat_keeps_a_waiting_ticket_alive(self) -> None:
        r = self.ps("""
$A = New-VenueTicket -Venue bachelor -RunDir 'run-A' -ClaimDir $d -StaleAfterSec 90
$t0 = [DateTime]::UtcNow
$beat = Update-VenueTicket -Ticket $A -StaleAfterSec 90 -NowUtc $t0.AddSeconds(80).ToString('o')
$rows = @(Get-VenueTickets -Venue bachelor -ClaimDir $d -NowUtc $t0.AddSeconds(150).ToString('o'))
[pscustomobject]@{ beat = $beat; state = $rows[0].state; gone = (Update-VenueTicket -Ticket ([pscustomobject]@{ path = 'nope'; nonce = 'x' })) } | ConvertTo-Json -Compress
""")
        self.assertEqual((r["beat"], r["state"], r["gone"]), ("OK", "LIVE", "LOST"))

    # ---------------------------------------------------------------- (c) a release on GATE_BUSY keeps the place
    def test_c_a_release_on_gate_busy_keeps_the_ticket_ahead_of_the_waiter_behind(self) -> None:
        r = self.ps("""
$A = New-VenueTicket -Venue bachelor -RunDir 'run-A' -ClaimDir $d
Start-Sleep -Milliseconds 30
$B = New-VenueTicket -Venue bachelor -RunDir 'run-B' -ClaimDir $d
$a1 = Request-VenueClaim -Venue bachelor -RunDir 'run-A' -ClaimDir $d -Ticket $A
$rel = Release-VenueClaim -Venue bachelor -Nonce $a1.claim.nonce -ClaimDir $d     # GATE_BUSY under claim: release, keep the ticket
$b1 = Request-VenueClaim -Venue bachelor -RunDir 'run-B' -ClaimDir $d -Ticket $B
$a2 = Request-VenueClaim -Venue bachelor -RunDir 'run-A' -ClaimDir $d -Ticket $A
[pscustomobject]@{ a1 = $a1.state; rel = $rel; b1 = $b1.state; b1Pos = $b1.position; a2 = $a2.state; a2Granted = [bool]$a2.granted; sameTicket = ($a2.ticket.name -ceq $A.name); aPos = $a2.position } | ConvertTo-Json -Compress
""")
        self.assertEqual((r["a1"], r["rel"]), ("TAKEN", "RELEASED"))
        self.assertEqual((r["b1"], r["b1Pos"]), ("QUEUED", 2), "A released on GATE_BUSY and still holds the older ticket: B must not jump ahead")
        self.assertEqual((r["a2"], r["a2Granted"], r["sameTicket"], r["aPos"]), ("TAKEN", True, True, 1), "A got the claim again on its ORIGINAL ticket")

    def test_a_lost_ticket_is_retaken_at_the_back_of_the_line(self) -> None:
        r = self.ps("""
$A = New-VenueTicket -Venue bachelor -RunDir 'run-A' -ClaimDir $d
Start-Sleep -Milliseconds 30
$B = New-VenueTicket -Venue bachelor -RunDir 'run-B' -ClaimDir $d
Remove-Item -LiteralPath $A.path -Force
$a = Request-VenueClaim -Venue bachelor -RunDir 'run-A' -ClaimDir $d -Ticket $A
[pscustomobject]@{ state = $a.state; pos = $a.position; renamed = ($a.ticket.name -cne $A.name) } | ConvertTo-Json -Compress
""")
        self.assertEqual((r["state"], r["pos"], r["renamed"]), ("QUEUED", 2, True))

    # ---------------------------------------------------------------- (d) back-compat with chains on the old code
    def test_d_a_legacy_ticketless_claimant_takes_a_free_claim_and_the_ticket_holder_proceeds_after_it(self) -> None:
        r = self.ps("""
$A = New-VenueTicket -Venue bachelor -RunDir 'run-A' -ClaimDir $d
$legacy = Request-VenueClaim -Venue bachelor -RunDir 'run-L' -ClaimDir $d
$a1 = Request-VenueClaim -Venue bachelor -RunDir 'run-A' -ClaimDir $d -Ticket $A
$rel = Release-VenueClaim -Venue bachelor -Nonce $legacy.claim.nonce -ClaimDir $d
$a2 = Request-VenueClaim -Venue bachelor -RunDir 'run-A' -ClaimDir $d -Ticket $A
$plain = Request-VenueClaim -Venue bachelor -RunDir 'run-L2' -ClaimDir $d
[pscustomobject]@{ legacy = $legacy.state; legacyGranted = [bool]$legacy.granted; legacyTicket = ($null -eq $legacy.ticket); a1 = $a1.state; a1Pos = $a1.position; a1Holder = $a1.claim.runDir
  rel = $rel; a2 = $a2.state; a2Granted = [bool]$a2.granted; plain = $plain.state; plainHolder = $plain.claim.runDir } | ConvertTo-Json -Compress
""")
        self.assertEqual((r["legacy"], r["legacyGranted"], r["legacyTicket"]), ("TAKEN", True, True), "the ticketless call is the old one-shot attempt and takes a free claim")
        self.assertEqual((r["a1"], r["a1Pos"], r["a1Holder"]), ("LIVE", 1, "run-L"), "the ticket holder waits for the legacy claim, no more")
        self.assertEqual((r["rel"], r["a2"], r["a2Granted"]), ("RELEASED", "TAKEN", True), "and proceeds when the legacy chain releases")
        self.assertEqual((r["plain"], r["plainHolder"]), ("LIVE", "run-A"), "a legacy waiter is blocked by a live claim exactly as before")

    def test_d_the_claim_file_name_and_shape_are_unchanged(self) -> None:
        r = self.ps("$t = New-VenueTicket -Venue bachelor -RunDir 'run-A' -ClaimDir $d\n$c = Request-VenueClaim -Venue bachelor -RunDir 'run-A' -ClaimDir $d -Ticket $t\n"
                    "[pscustomobject]@{ path = $c.path; keys = @((Get-Content -LiteralPath $c.path -Raw | ConvertFrom-Json).PSObject.Properties.Name) } | ConvertTo-Json -Compress")
        self.assertEqual(Path(r["path"]), self.claim_path)
        self.assertEqual(sorted(r["keys"]), sorted(["schema", "venue", "ownerPid", "ownerCreatedUtc", "host", "runDir", "purpose", "claimedUtc", "expiresUtc", "nonce"]))

    # ---------------------------------------------------------------- the line is visible
    def test_claim_status_lists_every_ticket_with_run_dir_pid_and_age(self) -> None:
        live = self.sleeper()
        self.ps(f"$A = New-VenueTicket -Venue bachelor -RunDir 'C:\\runs\\lane-A' -Purpose 'leg one' -ClaimDir $d -OwnerPid {live.pid}\nStart-Sleep -Milliseconds 30\n"
                f"$B = New-VenueTicket -Venue bachelor -RunDir 'C:\\runs\\lane-B' -ClaimDir $d -OwnerPid {live.pid}\n"
                f"$c = Request-VenueClaim -Venue bachelor -RunDir 'C:\\runs\\lane-A' -ClaimDir $d -Ticket $A -OwnerPid {live.pid}\n'{{}}'")
        r = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(RUNNER), "-ClaimStatus", "-Venue", "bachelor", "-ClaimDir", str(self.claims)],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        lines = r.stdout.splitlines()
        self.assertTrue(any(l.startswith("CLAIM_STATUS venue=bachelor state=LIVE") and "runDir=C:\\runs\\lane-A" in l and "holderPid=" in l and "ageSec=" in l for l in lines), lines)
        self.assertIn("TICKET_QUEUE venue=bachelor live=2 stale=0", lines)
        tickets = [l for l in lines if l.startswith("TICKET position=")]
        self.assertEqual(len(tickets), 2, lines)
        for want, line in zip(("lane-A", "lane-B"), tickets):
            self.assertIn(f"runDir=C:\\runs\\{want}", line)
            self.assertRegex(line, r"pid=\d+ .*ageSec=\d+ heartbeatAgeSec=\d+")
        self.assertTrue(tickets[0].startswith("TICKET position=1 ") and tickets[1].startswith("TICKET position=2 "), tickets)

    # ---------------------------------------------------------------- the chain uses it
    def test_chains_are_served_in_arrival_order_not_in_poll_order(self) -> None:
        rd_x, rd_a, rd_b = self.case / "lane-X", self.case / "lane-A", self.case / "lane-B"
        px = self.start_chain(rd_x, self.specs[2], claim_poll=1, FAKE_LEG_SLEEP="16")
        self.wait_for(lambda: self.claim_path.exists() and self.leg_starts() == ["fx-c.json"], "X holding the claim and running its leg")
        pa = self.start_chain(rd_a, self.specs[0], claim_poll=6)    # arrives first, polls its turn every 5-6 s
        self.wait_for(lambda: self.log_has(rd_a, "CLAIM_HELD"), "A waiting")
        pb = self.start_chain(rd_b, self.specs[1], claim_poll=1)    # arrives second, polls every second
        self.wait_for(lambda: self.log_has(rd_b, "CLAIM_HELD"), "B waiting")
        outs = [self.finish(p, 240) for p in (px, pa, pb)]
        self.assertEqual([p.returncode for p in (px, pa, pb)], [0, 0, 0], "\n----\n".join(outs))
        self.assertEqual(self.leg_starts(), ["fx-c.json", "fx-a.json", "fx-b.json"], "B, the later arrival, polled faster and took the claim first: no FIFO")
        self.assertTrue(any("queue=[#1" in l and "#2" in l for l in self.chain_log(rd_b)), self.chain_log(rd_b))
        self.assertFalse(self.claim_path.exists(), "a claim was left behind")
        self.assertEqual(list(self.ticket_dir.glob("*.ticket.json")), [], "a ticket was left behind")

    def test_a_gate_busy_under_claim_keeps_the_ticket_and_the_leg_still_runs(self) -> None:
        rd = self.case / "lane-K"
        p = subprocess.run(self.chain_args(rd, self.specs[0]), capture_output=True, text=True, timeout=240, env=self.env(FAKE_QUIET_MODES="QUIET,BUSY,QUIET,QUIET"))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        log = self.chain_log(rd)
        taken = [l for l in log if " TICKET_TAKEN " in l]
        self.assertEqual(len(taken), 1, "the ticket must be taken once and kept across the GATE_BUSY release: " + "\n".join(log))
        name = taken[0].split("ticket=")[1].split()[0]
        kept = [l for l in log if " TICKET_KEPT " in l]
        self.assertEqual(len(kept), 1, log)
        self.assertIn(f"ticket={name}", kept[0])
        ended = [l for l in log if " TICKET_ENDED " in l]
        self.assertEqual(len(ended), 1, log)
        self.assertIn(f"ticket={name} result=REMOVED reason=leg-starting", ended[0])
        self.assertEqual(len([l for l in log if " CLAIM_RELEASED " in l]), 2, "released on GATE_BUSY, then again after the leg")
        self.assertEqual(base.chain_result(p.stdout)["legs"][0]["status"], "RAN")

    def test_a_chain_that_is_not_quiet_after_its_one_kept_probe_gives_its_place_up(self) -> None:
        rd = self.case / "lane-D"
        p = subprocess.run(self.chain_args(rd, self.specs[0]), capture_output=True, text=True, timeout=240, env=self.env(FAKE_QUIET_MODES="QUIET,BUSY,BUSY,QUIET,QUIET"))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        log = self.chain_log(rd)
        self.assertEqual(len([l for l in log if " TICKET_TAKEN " in l]), 2, "after the second GATE_BUSY the place is given up and a new ticket taken: " + "\n".join(log))
        self.assertEqual(len([l for l in log if " TICKET_DROPPED " in l and "reason=not-quiet-after-one-probe" in l]), 1, log)
        self.assertEqual(list(self.ticket_dir.glob("*.ticket.json")), [])

    def test_a_dead_waiters_ticket_is_removed_by_the_next_chain_and_logged(self) -> None:
        dead = self.sleeper()
        self.ps(f"$A = New-VenueTicket -Venue bachelor -RunDir 'run-dead' -ClaimDir $d -OwnerPid {dead.pid}\n'{{}}'")
        dead.kill(); dead.wait()
        rd = self.case / "lane-S"
        p = subprocess.run(self.chain_args(rd, self.specs[0]), capture_output=True, text=True, timeout=240, env=self.env())
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        removed = [l for l in self.chain_log(rd) if " TICKET_STALE_REMOVED " in l]
        self.assertEqual(len(removed), 1, removed)
        self.assertIn(f"reason=dead-pid ownerPid={dead.pid} runDir=run-dead", removed[0])
        self.assertEqual(base.chain_result(p.stdout)["legs"][0]["status"], "RAN")
        self.assertEqual(list(self.ticket_dir.glob("*.ticket.json")), [])

    def test_a_chain_never_waits_on_a_legacy_claim_longer_than_its_owner_holds_it(self) -> None:
        """A legacy (old code) chain holds the claim; a ticketed chain waits behind it, then runs. Nothing ticketless is ever in the line to wait on."""
        started = datetime.now(timezone.utc)
        legacy = self.sleeper()
        self.claims.mkdir(parents=True, exist_ok=True)
        start_iso = base.pwsh_text(f"(Get-Process -Id {legacy.pid}).StartTime.ToUniversalTime().ToString('o')")
        self.claim_path.write_text(json.dumps({
            "schema": "mlv-app/venue-claim/v1", "venue": "bachelor", "ownerPid": legacy.pid, "ownerCreatedUtc": start_iso, "host": "H", "runDir": "legacy-chain", "purpose": "old code",
            "claimedUtc": utc_iso(started), "expiresUtc": utc_iso(started + timedelta(hours=3)), "nonce": "legacy"}) + "\n", encoding="utf-8")
        rd = self.case / "lane-T"
        p = self.start_chain(rd, self.specs[0], claim_poll=1)
        self.wait_for(lambda: self.log_has(rd, "CLAIM_HELD"), "the ticketed chain waiting on the legacy claim")
        self.assertEqual(self.leg_starts(), [], "the legacy claim is live: no leg may start")
        self.claim_path.unlink()   # the legacy chain releases
        out = self.finish(p, 120)
        self.assertEqual(p.returncode, 0, out)
        self.assertEqual(self.leg_starts(), ["fx-a.json"])

    # ---------------------------------------------------------------- r2: a kept place is bounded, a probe window is derived, equal ticks have a tie-break
    def mutant_runner(self, old: str, new: str) -> Path:
        """A copy of the runner + module in a scratch dir with ONE source line replaced (the replaced text must occur exactly once)."""
        d = Path(tempfile.mkdtemp(prefix="mut-", dir=self.case))
        text = RUNNER.read_text(encoding="utf-8")
        self.assertEqual(text.count(old), 1, f"mutation anchor moved: {old}")
        (d / RUNNER.name).write_text(text.replace(old, new), encoding="utf-8")
        copy_with_siblings(MODULE, d)
        return d / RUNNER.name

    def run_kept_head_scenario(self, runner: Path | None) -> tuple[list[str], list[str], list[str]]:
        """X holds the claim; A (head, QUIET/BUSY/QUIET/BUSY/QUIET probes) and a ready follower B queue behind it. Returns (leg starts, A's log, B's log)."""
        rd_x, rd_a, rd_b = self.case / "lane-X", self.case / "lane-A", self.case / "lane-B"
        px = self.start_chain(rd_x, self.specs[2], claim_poll=1, runner=runner, deadline_sec=300, FAKE_LEG_SLEEP="12")
        self.wait_for(lambda: self.claim_path.exists() and self.leg_starts() == ["fx-c.json"], "X holding the claim and running its leg")
        pa = self.start_chain(rd_a, self.specs[0], claim_poll=1, runner=runner, deadline_sec=300, FAKE_QUIET_MODES="QUIET,BUSY,QUIET,BUSY,QUIET")
        self.wait_for(lambda: self.log_has(rd_a, "CLAIM_HELD"), "A (the head) waiting")
        pb = self.start_chain(rd_b, self.specs[1], claim_poll=1, runner=runner, deadline_sec=300)
        self.wait_for(lambda: self.log_has(rd_b, "CLAIM_HELD"), "B (the follower) waiting behind A")
        try:
            outs = [self.finish(p, 240) for p in (px, pa, pb)]
        except subprocess.TimeoutExpired:
            logs = {rd.name: (rd / "chain.log").read_text(encoding="utf-8") if (rd / "chain.log").exists() else "(no chain.log)" for rd in (rd_x, rd_a, rd_b)}
            tickets = sorted(f.name for f in self.ticket_dir.glob("*.ticket.json")) if self.ticket_dir.exists() else []
            self.fail("a chain did not finish in 240 s\n" + "\n----\n".join(f"{k}:\n{v}" for k, v in logs.items()) + f"\nclaim exists={self.claim_path.exists()} tickets={tickets} legs={self.leg_starts()}")
        self.assertEqual([p.returncode for p in (px, pa, pb)], [0, 0, 0], "\n----\n".join(outs))
        return self.leg_starts(), self.chain_log(rd_a), self.chain_log(rd_b)

    def test_r2_b1_a_head_that_keeps_failing_under_the_claim_loses_its_place_to_a_ready_follower(self) -> None:
        starts, log_a, log_b = self.run_kept_head_scenario(None)
        kept = [l for l in log_a if " TICKET_KEPT " in l]
        dropped = [l for l in log_a if " TICKET_DROPPED " in l]
        self.assertEqual(len(kept), 1, "the ONE allowed keep: " + "\n".join(log_a))
        self.assertIn("keeps=1/1", kept[0])
        self.assertEqual(len(dropped), 1, "the next GATE_BUSY under the claim gives the place up: " + "\n".join(log_a))
        self.assertIn("reason=gate-busy-under-claim-after-kept-cycles keeps=1/1", dropped[0])
        self.assertEqual(starts, ["fx-c.json", "fx-b.json", "fx-a.json"], "the ready follower B must take the claim once A has used its one keep: " + "\n".join(log_b))
        self.assertEqual(len([l for l in log_a if " TICKET_TAKEN " in l]), 2, "A rejoins with a new ticket at the back: " + "\n".join(log_a))

    def test_r2_b1_mutation_without_the_keep_counter_the_head_keeps_its_place_forever(self) -> None:
        mut = self.mutant_runner("if ($ticketKeeps -lt $TicketKeepMax) {", "if ($true) {")
        starts, log_a, _ = self.run_kept_head_scenario(mut)
        self.assertEqual(len([l for l in log_a if " TICKET_DROPPED " in l]), 0, "the mutant must never drop: " + "\n".join(log_a))
        self.assertEqual(starts, ["fx-c.json", "fx-a.json", "fx-b.json"], "the mutant reproduces the starvation: A runs ahead of the ready follower B")

    PROBE_DUMP_FAKE = base.FAKE_QUIET.replace(
        "$held = $false",
        "if ($env:FAKE_TICKET_DUMP) { $tk = Join-Path (Split-Path -Parent $env:FAKE_CLAIM_PATH) 'bachelor.tickets'; foreach ($f in @(Get-ChildItem -LiteralPath $tk -Filter '*.ticket.json' -ErrorAction SilentlyContinue)) { "
        "Add-Content -LiteralPath $env:FAKE_TICKET_DUMP -Value ('{0} {1} {2}' -f $MaxWaitSec, $MaxAdmissionWaitSec, ([IO.File]::ReadAllText($f.FullName).Trim())) } }\n$held = $false", 1)

    def probe_phase_ticket(self, runner: Path | None = None, extra: tuple[str, ...] = ()) -> tuple[int, dict]:
        """Run one chain whose first under-claim probe is GATE_BUSY (the ticket is kept) and whose second PRE-CLAIM probe is the kept head's probe: returns the
        admission bound the chain passed to that probe and the ticket file as it stood while the probe ran."""
        fake = self.case / "fake-quiet-dump.ps1"
        fake.write_text(self.PROBE_DUMP_FAKE, encoding="utf-8")
        dump = self.case / "ticket-dump.log"
        rd = self.case / "lane-P"
        p = subprocess.run(self.chain_args(rd, self.specs[0], runner=runner, quiet=fake, extra=extra), capture_output=True, text=True, timeout=240,
                           env=self.env(FAKE_QUIET_MODES="QUIET,BUSY,QUIET,QUIET", FAKE_TICKET_DUMP=str(dump)))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        rows = [l.split(" ", 2) for l in dump.read_text(encoding="utf-8").splitlines()]
        pre_claim = [r for r in rows if r[0] == "600"]   # the kept head's pre-claim probe runs with -MaxWaitSec 600 (QuietMaxWaitSec); the under-claim probe runs with 0
        self.assertEqual(len(pre_claim), 1, rows)
        return int(pre_claim[0][1]), json.loads(pre_claim[0][2])

    def staleness_scenario(self, stale_after_sec: int, heartbeat_age_sec: int) -> dict:
        """A head ticket with the given staleAfterSec whose owner is live (a real process, its true start time) and one whose owner is dead, judged heartbeat_age_sec
        after their last heartbeat."""
        live, dead = self.sleeper(), self.sleeper()
        self.ps(f"$L = New-VenueTicket -Venue bachelor -RunDir 'run-live' -ClaimDir $d -OwnerPid {live.pid} -StaleAfterSec {stale_after_sec}\n"
                f"Start-Sleep -Milliseconds 30\n$D = New-VenueTicket -Venue bachelor -RunDir 'run-dead' -ClaimDir $d -OwnerPid {dead.pid} -StaleAfterSec {stale_after_sec}\n'{{}}'")
        dead.kill(); dead.wait()
        return self.ps(f"$rows = @(Get-VenueTickets -Venue bachelor -ClaimDir $d -NowUtc ([DateTime]::UtcNow.AddSeconds({heartbeat_age_sec})).ToString('o'))\n"
                       "[pscustomobject]@{ rows = @($rows | ForEach-Object { [pscustomobject]@{ run = $_.ticket.runDir; state = $_.state; reason = $_.reason } }) } | ConvertTo-Json -Compress -Depth 4")

    def test_r2_b2_a_live_head_in_a_long_probe_is_not_stale_at_700_s_but_a_dead_one_is_at_once(self) -> None:
        admission, ticket = self.probe_phase_ticket()
        self.assertEqual(admission, 7200, "the chain must pass the probe an explicit admission bound")
        self.assertEqual(ticket["phase"], "probing")
        self.assertEqual(ticket["staleAfterSec"], 600 + 300 + 7200 + 120 + 120 + 10,
                         "the window is derived from QuietMaxWaitSec + um-run's waits + the admission bound + a margin (max(10, 3 x ClaimPollSec 1))")
        r = self.staleness_scenario(int(ticket["staleAfterSec"]), heartbeat_age_sec=700)
        by_run = {row["run"]: row for row in r["rows"]}
        self.assertEqual((by_run["run-live"]["state"], by_run["run-live"]["reason"]), ("LIVE", None), "a live head whose probe is still pending 700 s in must keep its place")
        self.assertEqual((by_run["run-dead"]["state"], by_run["run-dead"]["reason"]), ("STALE", "dead-pid"), "a dead owner is stale at once whatever the window")

    def test_r2_b2_a_tighter_admission_bound_gives_a_proportionally_shorter_window(self) -> None:
        admission, ticket = self.probe_phase_ticket(extra=("-ProbeAdmissionWaitSec", "100", "-QuietMaxWaitSec", "600"))
        self.assertEqual(admission, 100)
        self.assertEqual(ticket["staleAfterSec"], 600 + 300 + 100 + 120 + 120 + 10)

    def test_r2_b2_mutation_with_the_old_derivation_a_live_head_goes_stale_at_700_s(self) -> None:
        mut = self.mutant_runner("$ticketProbeStaleSec = Get-VenueTicketProbeStaleSec -QuietMaxWaitSec $QuietMaxWaitSec -AdmissionWaitSec $ProbeAdmissionWaitSec -MarginSec $ticketPollStaleSec",
                                 "$ticketProbeStaleSec = $QuietMaxWaitSec + $ticketPollStaleSec")
        _, ticket = self.probe_phase_ticket(runner=mut)
        r = self.staleness_scenario(int(ticket["staleAfterSec"]), heartbeat_age_sec=700)
        live = [row for row in r["rows"] if row["run"] == "run-live"][0]
        self.assertEqual((live["state"], live["reason"]), ("STALE", "heartbeat"), "the mutant reproduces sol B2: the live head is removed as heartbeat-stale")

    TIE_BREAK = """
$dir = Get-VenueTicketDir -Venue bachelor -ClaimDir $d
New-Item -ItemType Directory -Force -Path $dir | Out-Null
foreach ($n in @@NAMES@@) { Set-Content -LiteralPath (Join-Path $dir $n) -Value '{}' }
[pscustomobject]@{ order = @(Get-VenueTickets -Venue bachelor -ClaimDir $d | ForEach-Object { $_.name }) } | ConvertTo-Json -Compress
"""

    def tie_break_order(self, names: list[str], module: Path | None = None) -> list[str]:
        """The line's order for ticket files created in the order given (each unparseable and fresh, so it counts as live 'being-written')."""
        return self.ps(self.TIE_BREAK.replace("@@NAMES@@", "@(" + ",".join(f"'{n}'" for n in names) + ")"), module=module)["order"]

    def test_r2_equal_tick_tickets_order_by_pid_then_start_not_by_enumeration(self) -> None:
        tick = "0000000000000000100"
        # string order of the names is NOT numeric order: p10 < p9 and 40 < 5 as text
        want = [f"{tick}-p9-5.ticket.json", f"{tick}-p9-40.ticket.json", f"{tick}-p10-5.ticket.json", f"{tick}-p10-40.ticket.json", f"0000000000000000101-p1-1.ticket.json"]
        self.assertEqual(self.tie_break_order(list(reversed(want))), want, "equal ticks order by pid then start ticks as NUMBERS, whatever order the files were created in")

    def test_r2_mutation_without_the_tie_break_equal_ticks_follow_enumeration_order(self) -> None:
        text = MODULE.read_text(encoding="utf-8")
        anchor = "Sort-Object -Property key, pidKey, startKey, name"
        self.assertEqual(text.count(anchor), 1, "mutation anchor moved")
        mut = copy_with_siblings(MODULE, Path(tempfile.mkdtemp(prefix="mut-", dir=self.case)), text.replace(anchor, "Sort-Object -Property key"))
        tick = "0000000000000000100"
        got = self.tie_break_order([f"{tick}-p9-5.ticket.json", f"{tick}-p10-5.ticket.json"], module=mut)
        self.assertEqual(got, [f"{tick}-p10-5.ticket.json", f"{tick}-p9-5.ticket.json"], "the mutant orders by file name text: p10 before p9")

    # ---------------------------------------------------------------- r3: the chain's argument list is bound against the REAL probe (hub-found blocker, PR #416)
    # r2 passed the chain's -MaxAdmissionWaitSec only to the FAKE probe, which declares it; the real Wait-VenueQuiet.ps1 did not, so pwsh failed parameter binding and
    # every quiet probe launched through the chain was VENUE_QUIET_UNKNOWN (r8v on Bachelor, "FIFO DEFECT"). Nothing compared the two sides; these tests do.
    AST_PRELUDE = r"""
$ErrorActionPreference = 'Stop'
function Get-Ast([string]$Path) {
    $t = $null; $e = $null
    $a = [System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$t, [ref]$e)
    if ($e.Count) { throw "parse errors in ${Path}: $($e[0].Message)" }
    $a
}
function Get-ParamNames($Ast, [string]$Set) {
    # the parameters bound in parameter set $Set: those with no ParameterSetName at all (every set) and those naming it ('*' = every declared parameter)
    $names = @()
    foreach ($p in $Ast.ParamBlock.Parameters) {
        $sets = @()
        foreach ($attr in $p.Attributes) {
            if ($attr -is [System.Management.Automation.Language.AttributeAst] -and $attr.TypeName.Name -ceq 'Parameter') {
                foreach ($na in $attr.NamedArguments) { if ($na.ArgumentName -ceq 'ParameterSetName') { $sets += [string]$na.Argument.Value } }
            }
        }
        if ($Set -ceq '*' -or $sets.Count -eq 0 -or $sets -contains $Set) { $names += $p.Name.VariablePath.UserPath }
    }
    $names
}
"""
    CHAIN_ARGS_PS = r"param([string]$Runner, [string]$Quiet)" + AST_PRELUDE + r"""
$r = Get-Ast $Runner
$fn = $r.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq 'Invoke-QuietProbe' }, $true)
if ($null -eq $fn) { throw 'Invoke-QuietProbe not found' }
$asg = $fn.Body.Find({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and $n.Left.Extent.Text -ceq '$qArgs' }, $true)
if ($null -eq $asg) { throw '$qArgs assignment not found' }
$consts = @($asg.Right.FindAll({ param($n) $n -is [System.Management.Automation.Language.StringConstantExpressionAst] }, $true) | Sort-Object { $_.Extent.StartOffset } | ForEach-Object { [string]$_.Value })
$i = [array]::IndexOf($consts, '-File')
if ($i -lt 0) { throw '-File not found in $qArgs' }
$passed = @($consts[($i + 1)..($consts.Count - 1)] | Where-Object { $_ -cmatch '^-[A-Za-z]' })
$live = @(Get-ParamNames (Get-Ast $Quiet) 'Live')
[ordered]@{ passed = $passed; live = $live; missing = @($passed | Where-Object { $live -notcontains $_.TrimStart('-') }) } | ConvertTo-Json -Compress
"""
    UMRUN_CALL_PS = r"param([string]$Quiet, [string]$UmRun)" + AST_PRELUDE + r"""
$q = Get-Ast $Quiet
$calls = @($q.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] -and $n.InvocationOperator -eq 'Ampersand' -and $n.CommandElements[0] -is [System.Management.Automation.Language.VariableExpressionAst] -and $n.CommandElements[0].VariablePath.UserPath -ceq 'umRun' }, $true))
if ($calls.Count -ne 1) { throw "expected exactly one & `$umRun call, found $($calls.Count)" }
$els = @($calls[0].CommandElements)
$pairs = @()
for ($k = 1; $k -lt $els.Count; $k++) {
    if ($els[$k] -is [System.Management.Automation.Language.CommandParameterAst]) {
        $v = $(if ($null -ne $els[$k].Argument) { $els[$k].Argument.Extent.Text } elseif ($k + 1 -lt $els.Count -and $els[$k + 1] -isnot [System.Management.Automation.Language.CommandParameterAst]) { $els[$k + 1].Extent.Text } else { '' })
        $pairs += "$($els[$k].ParameterName)=$v"
    }
}
$declared = @(Get-ParamNames (Get-Ast $UmRun) '*')
[ordered]@{ pairs = $pairs; declared = $declared; missing = @($pairs | ForEach-Object { ($_ -split '=')[0] } | Where-Object { $declared -notcontains $_ }) } | ConvertTo-Json -Compress
"""

    def ast_json(self, script: str, *args: str) -> dict:
        f = self.case / f"ast-{len(list(self.case.glob('ast-*.ps1')))}.ps1"
        f.write_text(script, encoding="utf-8")
        r = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(f), *args], capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise AssertionError(f"AST scenario failed ({r.returncode}):\n{r.stdout}\n{r.stderr}")
        return json.loads([l for l in r.stdout.splitlines() if l.strip()][-1])

    def mutant_copy(self, src: Path, old: str, new: str) -> Path:
        text = src.read_text(encoding="utf-8")
        self.assertEqual(text.count(old), 1, f"mutation anchor moved in {src.name}")
        out = self.case / f"mutant-{src.name}"
        out.write_text(text.replace(old, new), encoding="utf-8")
        return out

    def test_r3_every_argument_the_chain_passes_the_probe_is_a_declared_live_parameter(self) -> None:
        r = self.ast_json(self.CHAIN_ARGS_PS, str(RUNNER), str(QUIET))
        self.assertIn("-MaxAdmissionWaitSec", r["passed"], "the AST walk must see the admission bound the chain passes (else this guard is blind)")
        self.assertIn("-Venue", r["passed"])
        self.assertGreaterEqual(len(r["passed"]), 7)
        self.assertEqual(r["missing"], [], "the chain passes parameters Wait-VenueQuiet.ps1 does not declare in its Live set: pwsh fails binding and every probe is NOT_RUN")

    def test_r3_mutation_a_probe_without_the_admission_parameter_is_caught(self) -> None:
        mut = self.mutant_copy(QUIET, "    [Parameter(ParameterSetName = 'Live')][ValidateRange(1, 86400)][int]$MaxAdmissionWaitSec = 7200,\n", "")
        r = self.ast_json(self.CHAIN_ARGS_PS, str(RUNNER), str(mut))
        self.assertEqual(r["missing"], ["-MaxAdmissionWaitSec"], "the r2 state: the chain passes a parameter the probe lacks")

    def test_r3_mutation_a_chain_that_passes_an_unknown_parameter_is_caught(self) -> None:
        mut = self.mutant_copy(RUNNER, "'-MaxAdmissionWaitSec', [string]$ProbeAdmissionWaitSec)", "'-MaxAdmissionWaitSec', [string]$ProbeAdmissionWaitSec, '-NoSuchProbeParameter', '1')")
        r = self.ast_json(self.CHAIN_ARGS_PS, str(mut), str(QUIET))
        self.assertEqual(r["missing"], ["-NoSuchProbeParameter"])

    def test_r3_the_probe_forwards_its_admission_bound_to_um_run(self) -> None:
        r = self.ast_json(self.UMRUN_CALL_PS, str(QUIET), str(UM_RUN))
        self.assertIn("MaxAdmissionWaitSec=$MaxAdmissionWaitSec", r["pairs"], "the um-run call must pass the probe's own -MaxAdmissionWaitSec through (um-run's default is 7200 s)")
        self.assertIn("MaxAdmissionWaitSec", r["declared"])
        self.assertEqual(r["missing"], [], "the um-run call passes a parameter um-run.ps1 does not declare")

    def test_r3_mutation_a_probe_that_does_not_forward_the_bound_is_caught(self) -> None:
        call = "-MaxClaimedWaitSec 120 -MaxAdmissionWaitSec $MaxAdmissionWaitSec 6>$null)"
        for new in ("-MaxClaimedWaitSec 120 6>$null)", "-MaxClaimedWaitSec 120 -MaxAdmissionWaitSec 7200 6>$null)"):
            mut = self.mutant_copy(QUIET, call, new)
            r = self.ast_json(self.UMRUN_CALL_PS, str(mut), str(UM_RUN))
            self.assertNotIn("MaxAdmissionWaitSec=$MaxAdmissionWaitSec", r["pairs"], f"mutant {new!r} must not look like a forward")

    def run_real_probe(self, *extra: str) -> subprocess.CompletedProcess:
        """The REAL Wait-VenueQuiet.ps1, Live set, against a share that does not exist: it stops at GATE_UNREADABLE (exit 4) before any probe job is written or
        submitted, so no venue, no claim and no um-run is involved. WorkDir and share are scratch paths under the test's temp dir."""
        share = self.case / "no-such-agent-share"
        r = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(QUIET), "-Venue", "bachelor", "-WorkDir", str(self.case / "quiet-work"),
                            "-AgentShare", str(share), "-AllowShareOverride", "-MaxWaitSec", "5", "-MaxGateSec", "5", "-ReadAttempts", "1", "-ReadBackoffSec", "0", *extra],
                           capture_output=True, text=True, timeout=120)
        self.assertFalse(share.exists(), "the probe must not create the share")
        return r

    def test_r3_the_real_probe_binds_the_admission_parameter_the_chain_passes(self) -> None:
        r = self.run_real_probe("-ThresholdPercent", "20", "-RecheckSec", "90", "-MaxAdmissionWaitSec", "7200")
        text = r.stdout + r.stderr
        self.assertNotIn("parameter cannot be found", text, text)
        self.assertNotIn("Cannot validate argument", text, text)
        self.assertEqual(r.returncode, 4, text)
        self.assertIn("GATE_UNREADABLE", text)

    def test_r3_the_real_probe_validates_the_admission_range_so_the_binding_is_the_declared_one(self) -> None:
        for bad in ("0", "86401"):
            r = self.run_real_probe("-MaxAdmissionWaitSec", bad)
            text = r.stdout + r.stderr
            self.assertNotEqual(r.returncode, 4, text)
            self.assertIn("Cannot validate argument on parameter 'MaxAdmissionWaitSec'", text)

    # ---------------------------------------------------------------- LEASE-BATCH-IDLE-END-UNSEEN-1 r6 (Grok G10 on #817; adversary-848f6559.json A3): -WaitUntilUtc, run for real
    # The real Wait-VenueQuiet.ps1, Live set, against a scratch share whose inbox holds one queued job: the queue gate stays BUSY, so the script never submits a probe (no um-run,
    # no venue). DST wrapper: the same script run in a process whose local zone is the host's own offset shifted by an hour, which is what a DST fall-back (or spring-forward)
    # between the cutoff instant and now does to a cutoff read as LocalDateTime. The host clock is never touched.
    DST_WRAPPER = r"""
param([string]$Quiet, [string]$ParamsJson, [double]$ShiftHours, [string]$WaitUntilUtc)
$cd = [TimeZoneInfo].GetField('s_cachedData', [Reflection.BindingFlags]'NonPublic,Static').GetValue($null)
$os = [DateTimeOffset]::Now.Offset
$z = [TimeZoneInfo]::CreateCustomTimeZone('test-shifted', $os.Add([TimeSpan]::FromHours($ShiftHours)), 'test-shifted', 'test-shifted')
$cd.GetType().GetField('_localTimeZone', [Reflection.BindingFlags]'NonPublic,Instance').SetValue($cd, $z)
if ([TimeZoneInfo]::Local.BaseUtcOffset -ne $os.Add([TimeSpan]::FromHours($ShiftHours))) { [Console]::Error.WriteLine('WRAPPER could not shift the local zone'); exit 99 }
$h = @{}; foreach ($p in (ConvertFrom-Json $ParamsJson).PSObject.Properties) { $h[$p.Name] = $p.Value }
$h['WaitUntilUtc'] = $WaitUntilUtc   # passed as its own string: ConvertFrom-Json turns an ISO string into a DateTime
& $Quiet @h
exit $LASTEXITCODE
"""

    def busy_share(self) -> Path:
        share = self.case / "busy-share"
        (share / "inbox").mkdir(parents=True)
        (share / "running").mkdir()
        (share / "inbox" / "someone-else.job.ps1").write_text("# queued by another lane\n", encoding="utf-8")
        return share

    def run_real_gate(self, share: Path, until: str, max_sec: int = 30, shift_hours: float | None = None) -> tuple[subprocess.CompletedProcess, float]:
        params = {"Venue": "bachelor", "WorkDir": str(self.case / "quiet-work"), "AgentShare": str(share), "AllowShareOverride": True, "MaxWaitSec": max_sec,
                  "MaxGateSec": max_sec, "GatePollSec": 1, "ReadAttempts": 1, "ReadBackoffSec": 0, "WaitUntilUtc": until}
        if shift_hours is None:
            argv = [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(QUIET)]
            for k, v in params.items():
                argv += [f"-{k}"] if v is True else [f"-{k}", str(v)]
        else:
            wrapper = self.case / "dst-wrapper.ps1"
            wrapper.write_text(self.DST_WRAPPER, encoding="utf-8")
            rest = {k: v for k, v in params.items() if k != "WaitUntilUtc"}
            argv = [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(wrapper), "-Quiet", str(QUIET), "-ParamsJson", json.dumps(rest), "-ShiftHours", str(shift_hours),
                    "-WaitUntilUtc", until]
        t = time.monotonic()
        r = subprocess.run(argv, capture_output=True, text=True, timeout=180)
        return r, time.monotonic() - t

    def assert_nothing_dispatched(self, share: Path) -> None:
        self.assertEqual(sorted(p.name for p in (share / "inbox").iterdir()), ["someone-else.job.ps1"], "the probe submitted a job")
        self.assertEqual(list((share / "running").iterdir()), [])
        self.assertFalse((share / "outbox").exists())

    @staticmethod
    def waited(text: str) -> int:
        import re
        m = re.search(r"^GATE_BUSY \S+ waitedSec=(\d+)$", text, re.M)
        assert m, text
        return int(m.group(1))

    def test_r6_g10_a_busy_queue_gate_ends_at_wait_until_utc_not_at_max_wait(self) -> None:
        # Grok G10 (a): -MaxWaitSec 30 -MaxGateSec 30 and a cutoff 2 s out: GATE_BUSY (exit 3) after about 2 s, nothing dispatched. Both ISO spellings the runner and a
        # person write (the 'o' round-trip form with 7 fractional digits, and seconds with Z) are read.
        share = self.busy_share()
        for name, fmt in (("round-trip", lambda d: d.strftime("%Y-%m-%dT%H:%M:%S.%f") + "0Z"), ("seconds", lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ"))):
            with self.subTest(form=name):
                until = fmt(datetime.now(timezone.utc) + timedelta(seconds=3 if name == "seconds" else 2))
                r, took = self.run_real_gate(share, until)
                text = r.stdout + r.stderr
                self.assertEqual(r.returncode, 3, text)
                self.assertLessEqual(self.waited(text), 3, text)
                self.assertLess(took, 20, f"the gate ran {took:.1f} s past a cutoff about 2 s out: {text}")
                self.assert_nothing_dispatched(share)

    def test_r6_g10_a_wait_until_utc_that_is_not_an_iso_instant_is_refused_before_anything_is_read(self) -> None:
        # Grok G10 (b)/(c): 'nope', and forms [DateTimeOffset]::TryParse with InvariantCulture accepted although the USAGE text says "is not an ISO 8601 instant" (11/14/2026, a
        # long date): exit 2, the USAGE line, no gate line, nothing dispatched.
        share = self.busy_share()
        for bad in ("nope", "11/14/2026", "11/14/2026 18:00:00", "Saturday, November 14, 2026", "2026-11-14"):
            with self.subTest(value=bad):
                r, _ = self.run_real_gate(share, bad)
                text = r.stdout + r.stderr
                self.assertEqual(r.returncode, 2, text)
                self.assertIn(f"USAGE -WaitUntilUtc '{bad}' is not an ISO 8601 instant", text)
                self.assertNotIn("GATE ", text)
                self.assert_nothing_dispatched(share)

    def test_r6_adv_a3_the_cutoff_holds_when_the_local_offset_moved_between_it_and_now(self) -> None:
        # adversary-848f6559.json A3: the clamp read the cutoff as LocalDateTime and compared it with local Get-Date values, which compare by ticks only. When the cutoff's local
        # offset differs from now's (a DST fall-back between them), an EXPIRED cutoff read an hour late and the gate waited its whole -MaxWaitSec past it; a cutoff a few seconds out
        # read an hour early and the gate gave up at once (a false GATE_BUSY). Every clock in the script is UTC now.
        share = self.busy_share()
        r, took = self.run_real_gate(share, utc_iso(datetime.now(timezone.utc) - timedelta(seconds=5)), max_sec=10, shift_hours=1)
        text = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 99, text)
        self.assertEqual(r.returncode, 3, text)
        self.assertLessEqual(self.waited(text), 1, f"an expired cutoff (read an hour late) did not end the gate: {text}")
        self.assert_nothing_dispatched(share)
        r, took = self.run_real_gate(share, utc_iso(datetime.now(timezone.utc) + timedelta(seconds=5)), max_sec=20, shift_hours=-1)
        text = r.stdout + r.stderr
        self.assertEqual(r.returncode, 3, text)
        self.assertGreaterEqual(self.waited(text), 3, f"a cutoff 5 s out (read an hour early) ended the gate at once: {text}")
        self.assertLessEqual(self.waited(text), 6, text)
        self.assert_nothing_dispatched(share)

    @staticmethod
    def run_reaped(p: subprocess.Popen, timeout: float) -> tuple[str, bool]:
        try:
            out, err = p.communicate(timeout=timeout)
            return out + err, False
        except subprocess.TimeoutExpired:
            p.kill()
            out, err = p.communicate()
            return out + err, True

    def test_r11_adv_d38e2ab8_a1_a_gate_that_waited_past_the_cutoff_submits_no_probe(self) -> None:
        # adversary-d38e2ab8.json A1: the cutoffs clamp $deadline / $gateEnd (Wait-VenueQuiet.ps1 :285-301), but the queue gate returned CLEAR BEFORE any expiry check, and the
        # main loop then wrote and submitted a probe (:1602-1608) after the caller's end. The case that wait makes: a busy queue, the gate polls up to the cutoff (-GatePollSec 30
        # sleeps straight to $gateEnd), and the queue is empty at the read just past it. That probe is one this script WAITED for: no job is written or submitted, GATE_CUTOFF, exit 3
        # (the GATE_BUSY code every caller reads as "no probe ran"). The queued job is removed only after the gate log shows the busy read, so the ordering is not a timing guess.
        for name in ("wait-until-utc", "wait-until-ts"):
            with self.subTest(cutoff=name):
                share = self.case / f"busy-then-empty-{name}"
                (share / "inbox").mkdir(parents=True)
                (share / "running").mkdir()
                queued = share / "inbox" / "someone-else.job.ps1"
                queued.write_text("# queued by another lane\n", encoding="utf-8")
                work = self.case / f"quiet-work-{name}"
                work.mkdir()
                log = work / "gate.log"
                cutoff_sec = 10
                if name == "wait-until-utc":
                    extra = ["-WaitUntilUtc", utc_iso(datetime.now(timezone.utc) + timedelta(seconds=cutoff_sec))]
                else:
                    hz = self.qpc_hz()
                    extra = ["-WaitUntilTs", str(self.qpc_now() + cutoff_sec * hz)]
                argv = [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(QUIET), "-Venue", "bachelor", "-WorkDir", str(work), "-GateLog", str(log), "-AgentShare",
                        str(share), "-AllowShareOverride", "-MaxWaitSec", "240", "-MaxGateSec", "240", "-GatePollSec", "30", "-ReadAttempts", "1", "-ReadBackoffSec", "0", *extra]
                p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                seen_busy = False
                t = time.monotonic()
                while time.monotonic() - t < cutoff_sec - 1 and p.poll() is None:
                    if log.exists() and "queued=1 running=0" in log.read_text(encoding="utf-8", errors="replace"):
                        seen_busy = True
                        break
                    time.sleep(0.1)
                queued.unlink()
                text, hung = self.run_reaped(p, 60)
                self.assertTrue(seen_busy, f"the gate never logged the busy read before the cutoff (host too slow for this case): {text}")
                submitted = sorted(q.name for q in (share / "inbox").iterdir())
                written = sorted(q.name for q in work.glob("venue-quiet-probe-*.job.ps1"))
                self.assertFalse(hung, f"still running 60 s after the cutoff (a probe was submitted and waited on the agent): inbox={submitted} {text}")
                self.assertEqual(submitted, [], f"a probe job was submitted after the cutoff: {text}")
                self.assertEqual(written, [], f"a probe job was written after the cutoff: {text}")
                self.assertEqual(p.returncode, 3, text)
                self.assertRegex(text, r"(?m)^GATE quiet-probe bachelor queued=0 running=0 ", "the gate must have read the queue empty past the cutoff, else this is not A1's case")
                self.assertRegex(text, r"(?m)^GATE_CUTOFF quiet-probe bachelor waitedSec=\d+ ", text)

    def qpc_now(self) -> int:
        r = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "[Diagnostics.Stopwatch]::GetTimestamp()"], capture_output=True, text=True, timeout=60)
        return int(r.stdout.strip())

    def qpc_hz(self) -> int:
        r = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "[Diagnostics.Stopwatch]::Frequency"], capture_output=True, text=True, timeout=60)
        return int(r.stdout.strip())

    def test_r11_adv_7fdbd64d_a1_a_zero_wait_cutoff_keeps_the_first_measurement(self) -> None:
        # adversary-7fdbd64d.json A1: -ProbeMaxWaitSec 0 (accepted) makes the runner's cutoff the dispatch itself, so a cutoff that refused EVERY probe past it refused the
        # first measurement too: every probe GATE_BUSY, no leg ever run, LEASE_IDLE_CHURN. The first probe of a gate that is CLEAR at its first read is the caller's own dispatch and
        # is submitted, as -MaxWaitSec 0 always was. Here um-run finds no agent heartbeat at the scratch share and refuses at once (VENUE_QUIET_UNKNOWN, exit 6): the job file was
        # written and submission was attempted, which is the behaviour pinned. No GATE_CUTOFF line.
        past = utc_iso(datetime.now(timezone.utc) - timedelta(seconds=1))
        for name, extra in (("wait-until-utc", ["-WaitUntilUtc", past]), ("wait-until-ts", ["-WaitUntilTs", "1"]), ("both", ["-WaitUntilUtc", past, "-WaitUntilTs", "1"])):
            with self.subTest(cutoff=name):
                share = self.case / f"empty-share-{name}"
                (share / "inbox").mkdir(parents=True)
                (share / "running").mkdir()
                work = self.case / f"quiet-work-zero-{name}"
                argv = [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(QUIET), "-Venue", "bachelor", "-WorkDir", str(work), "-AgentShare", str(share),
                        "-AllowShareOverride", "-MaxWaitSec", "0", "-MaxGateSec", "240", "-GatePollSec", "1", "-ReadAttempts", "1", "-ReadBackoffSec", "0", *extra]
                text, hung = self.run_reaped(subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True), 120)
                self.assertFalse(hung, text)
                written = sorted(q.name for q in work.glob("venue-quiet-probe-*.job.ps1")) if work.exists() else []
                self.assertEqual(len(written), 1, f"the first measurement of a zero wait was refused: {text}")
                self.assertNotIn("GATE_CUTOFF", text)
                self.assertIn("No agent heartbeat", text, "um-run must have been called for the probe (the scratch share has no agent)")

    def test_r11_adv_d38e2ab8_a1_every_probe_submission_is_behind_the_cutoff_check(self) -> None:
        # the class (A1's siblings :1602, :1605, :1608): the cutoff is asked right after the gate's CLEAR (before the job file is written) and again as the last statement before
        # um-run is called, so a stalled job-file write (a UNC -WorkDir) cannot carry a submission past it; a busy probe already measured ends on its own reading (break), none
        # measured yet ends exit 3. 7fdbd64d A1: the first probe of a gate CLEAR at its first read is not refused (GateWaited is set before every gate sleep, read or poll).
        import re
        src = QUIET.read_text(encoding="utf-8")
        gate = src[src.index("function Read-GateCounts"):src.index("$probeText = @'")]
        self.assertEqual(gate.count("$script:GateWaited = $true"), 2, "both gate sleeps (the read retry and the busy poll) must mark the gate as having waited")
        for sleep in ("if ($delay -gt 0) { Start-Sleep", "        Start-Sleep -Milliseconds"):
            self.assertLess(gate.index("$script:GateWaited = $true"), gate.index(sleep))
        loop = src[src.index("    Wait-QueueGate 'quiet-probe'"):]
        write_at = loop.index("[IO.File]::WriteAllText($jobFile")
        call_at = loop.index("& $umRun -ScriptPath $jobFile")
        checks = [m.start() for m in re.finditer(re.escape("if (Test-WaitCutoffRefusesProbe) {"), loop)]
        self.assertTrue(any(c < write_at for c in checks), "no cutoff check between the gate's CLEAR and the job-file write")
        self.assertTrue(any(write_at < c < call_at for c in checks), "no cutoff check between the job-file write and the um-run submission")
        self.assertEqual(loop.count("& $umRun "), 1, "a second submission site needs its own cutoff check")

    # ---------------------------------------------------------------- r12 (adversary-7fdbd64d.json A2): a um-run stall after the last cutoff check
    # The real Wait-VenueQuiet.ps1, copied with venues.json and its sibling modules, beside a FAKE um-run.ps1 (the script resolves '..\um-run.ps1' from its own folder). The
    # fake records when each call starts, then follows its control file: return a reading at once, or stall until a given wall instant past the caller's cutoff and then return
    # one. That is A2's input: the cutoff check passes before um-run is entered, um-run (a heartbeat Test-Path, a slow placement) takes the submission past the cutoff, and the
    # job is published. A waited-for probe whose reading arrives after the cutoff must not be published as the gate's measurement.
    FAKE_UMRUN = r"""
param([string]$ScriptPath, [string]$JobId, [string]$AgentShare, [int]$TimeoutSec, [int]$MaxQueueWaitSec, [int]$MaxClaimedWaitSec, [int]$MaxAdmissionWaitSec)
$c = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'fake-umrun-control.json')) | ConvertFrom-Json
$calls = Join-Path $PSScriptRoot 'fake-umrun-calls.txt'
$n = $(if (Test-Path -LiteralPath $calls) { @([IO.File]::ReadAllLines($calls)).Count } else { 0 }) + 1
[IO.File]::AppendAllText($calls, ('{0} {1}' -f $n, [DateTime]::UtcNow.ToString('o')) + "`n")
$step = @($c.steps)[[Math]::Min($n, @($c.steps).Count) - 1]
if ($step.stallUntilUtc) {
    $until = [DateTimeOffset]::Parse([string]$step.stallUntilUtc, [Globalization.CultureInfo]::InvariantCulture).UtcDateTime
    $ms = ($until - [DateTime]::UtcNow).TotalMilliseconds
    if ($ms -gt 0) { Start-Sleep -Milliseconds ([int]$ms) }
}
$probe = [ordered]@{ schema = 'mlv-app/venue-quiet-probe/v1'; host = 'BACHELOR'; samples = @($step.samples); top = @() } | ConvertTo-Json -Compress
[pscustomobject]@{ stdout = "VENUE_QUIET=$probe"; exitCode = 0 }
"""

    def fake_umrun_tree(self, name: str, steps: list[dict]) -> tuple[Path, Path]:
        root = self.case / f"fake-tree-{name}"
        dv = root / "dual-venue"
        quiet = copy_with_siblings(QUIET, dv)
        (dv / "venues.json").write_bytes((QUIET.parent / "venues.json").read_bytes())
        (root / "um-run.ps1").write_text(self.FAKE_UMRUN, encoding="utf-8")
        (root / "fake-umrun-control.json").write_text(json.dumps({"steps": steps}), encoding="utf-8")
        return quiet, root

    @staticmethod
    def umrun_call_starts(root: Path) -> list[datetime]:
        calls = root / "fake-umrun-calls.txt"
        if not calls.exists():
            return []
        out = []
        for line in calls.read_text(encoding="utf-8").splitlines():
            stamp = line.split(" ", 1)[1].strip()
            out.append(datetime.fromisoformat(stamp[:26] + "+00:00") if "." in stamp else datetime.fromisoformat(stamp.replace("Z", "+00:00")))
        return out

    def test_r12_adv_7fdbd64d_a2_a_reprobe_whose_reading_arrives_past_the_cutoff_is_not_published(self) -> None:
        # A re-probe ($last set): the first probe reads BUSY at once, the re-probe is submitted BEFORE the cutoff (asserted from the fake's own call log, else this is not A2's
        # case) and its um-run stalls until 3 s past it, then answers QUIET. Before r12 that QUIET was published (exit 0, `QUIET mean=1.0%`): a waited-for reading taken after the
        # caller's end. Now: `GATE_CUTOFF ... phase=after-probe`, and the gate ends on the reading it already had (COOLDOWN_UNMET mean=50.0%, exit 0), as the before-write check does.
        for name in ("wait-until-utc", "wait-until-ts"):
            with self.subTest(cutoff=name):
                share = self.case / f"empty-share-a2-reprobe-{name}"
                (share / "inbox").mkdir(parents=True)
                (share / "running").mkdir()
                cutoff_sec = 15
                if name == "wait-until-ts":
                    hz = self.qpc_hz()
                    ts = self.qpc_now() + cutoff_sec * hz
                cutoff = datetime.now(timezone.utc) + timedelta(seconds=cutoff_sec)
                extra = ["-WaitUntilUtc", utc_iso(cutoff)] if name == "wait-until-utc" else ["-WaitUntilTs", str(ts)]
                if name == "wait-until-utc":
                    cutoff = cutoff.replace(microsecond=0)   # utc_iso truncates to whole seconds
                quiet, root = self.fake_umrun_tree(f"reprobe-{name}", [{"samples": [50, 50, 50]}, {"samples": [1, 1, 1], "stallUntilUtc": (cutoff + timedelta(seconds=3)).isoformat()}])
                argv = [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(quiet), "-Venue", "bachelor", "-WorkDir", str(self.case / f"quiet-work-a2-{name}"),
                        "-AgentShare", str(share), "-AllowShareOverride", "-MaxWaitSec", "240", "-MaxGateSec", "240", "-GatePollSec", "1", "-RecheckSec", "1",
                        "-ReadAttempts", "1", "-ReadBackoffSec", "0", "-ForeignFloorProbes", "0", *extra]
                text, hung = self.run_reaped(subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True), 120)
                self.assertFalse(hung, text)
                starts = self.umrun_call_starts(root)
                self.assertGreaterEqual(len(starts), 2, f"the re-probe was never submitted: {text}")
                self.assertLess(starts[1], cutoff - timedelta(seconds=1), f"the re-probe started at or past the cutoff (host too slow for this case): {starts} {text}")
                self.assertNotRegex(text, r"(?m)^QUIET mean=", f"a re-probe reading that arrived past the cutoff was published: {text}")
                self.assertRegex(text, r"(?m)^GATE_CUTOFF quiet-probe bachelor waitedSec=\d+ phase=after-probe ", text)
                self.assertRegex(text, r"(?m)^COOLDOWN_UNMET mean=50\.0% ", text)

    def test_r12_adv_7fdbd64d_a2_a_first_probe_after_a_gate_wait_whose_reading_arrives_past_the_cutoff_is_gate_cutoff(self) -> None:
        # A2's own input: the gate polled a busy queue (GateWaited), the queue empties before the cutoff, the probe is submitted before it, and um-run stalls past it. Nothing
        # was measured before, so the end is GATE_CUTOFF, exit 3 (the lease batch's GATE_BUSY: the anchor stays, which is the earlier and so the safe side of the agent's clock).
        share = self.case / "busy-then-empty-a2"
        (share / "inbox").mkdir(parents=True)
        (share / "running").mkdir()
        queued = share / "inbox" / "someone-else.job.ps1"
        queued.write_text("# queued by another lane\n", encoding="utf-8")
        work = self.case / "quiet-work-a2-first"
        work.mkdir()
        log = work / "gate.log"
        cutoff = (datetime.now(timezone.utc) + timedelta(seconds=15)).replace(microsecond=0)
        quiet, root = self.fake_umrun_tree("first", [{"samples": [1, 1, 1], "stallUntilUtc": (cutoff + timedelta(seconds=3)).isoformat()}])
        argv = [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(quiet), "-Venue", "bachelor", "-WorkDir", str(work), "-GateLog", str(log), "-AgentShare",
                str(share), "-AllowShareOverride", "-MaxWaitSec", "240", "-MaxGateSec", "240", "-GatePollSec", "1", "-ReadAttempts", "1", "-ReadBackoffSec", "0",
                "-WaitUntilUtc", utc_iso(cutoff)]
        p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        seen_busy = False
        t = time.monotonic()
        while time.monotonic() - t < 10 and p.poll() is None:
            if log.exists() and "queued=1 running=0" in log.read_text(encoding="utf-8", errors="replace"):
                seen_busy = True
                break
            time.sleep(0.1)
        queued.unlink()
        text, hung = self.run_reaped(p, 120)
        self.assertTrue(seen_busy, f"the gate never logged the busy read (host too slow for this case): {text}")
        self.assertFalse(hung, text)
        starts = self.umrun_call_starts(root)
        self.assertEqual(len(starts), 1, f"exactly one probe must have been submitted: {text}")
        self.assertLess(starts[0], cutoff - timedelta(seconds=1), f"the probe started at or past the cutoff (host too slow for this case): {starts} {text}")
        self.assertNotRegex(text, r"(?m)^QUIET mean=", f"a waited-for reading that arrived past the cutoff was published: {text}")
        self.assertEqual(p.returncode, 3, text)
        self.assertRegex(text, r"(?m)^GATE_CUTOFF quiet-probe bachelor waitedSec=\d+ phase=after-probe ", text)

    def test_r12_adv_7fdbd64d_a2_a_zero_wait_first_probe_is_published_even_when_it_ends_past_the_cutoff(self) -> None:
        # 7fdbd64d A1 kept: the first probe of a gate CLEAR at its first read is the caller's own dispatch, so its reading is published even when it arrives after the cutoff
        # (here the cutoff is already past at the start and um-run stalls a few seconds). QUIET, exit 0, no GATE_CUTOFF line.
        share = self.case / "empty-share-a2-zero"
        (share / "inbox").mkdir(parents=True)
        (share / "running").mkdir()
        quiet, root = self.fake_umrun_tree("zero", [{"samples": [1, 1, 1], "stallUntilUtc": (datetime.now(timezone.utc) + timedelta(seconds=6)).isoformat()}])
        argv = [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(quiet), "-Venue", "bachelor", "-WorkDir", str(self.case / "quiet-work-a2-zero"),
                "-AgentShare", str(share), "-AllowShareOverride", "-MaxWaitSec", "0", "-MaxGateSec", "240", "-GatePollSec", "1", "-ReadAttempts", "1", "-ReadBackoffSec", "0",
                "-WaitUntilUtc", utc_iso(datetime.now(timezone.utc) - timedelta(seconds=1))]
        text, hung = self.run_reaped(subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True), 120)
        self.assertFalse(hung, text)
        self.assertEqual(len(self.umrun_call_starts(root)), 1, text)
        self.assertNotIn("GATE_CUTOFF", text)
        self.assertRegex(text, r"(?m)^QUIET mean=1\.0% ", text)

    def test_r6_adv_a3_no_wait_in_the_probe_script_reads_a_local_clock(self) -> None:
        # the class (A3's siblings :277, :278, :289, :306, :1628): outside the probe job's own text, the script reads no local clock (Get-Date, [DateTime]::Now, LocalDateTime,
        # ToLocalTime), so no deadline, gate end, retry check or recheck can compare local DateTimes again
        src = QUIET.read_text(encoding="utf-8")
        start = src.index("$probeText = @'")
        outside = src[:start] + src[src.index("\n'@", start):]
        import re
        self.assertEqual(re.findall(r"(?i)Get-Date|\[DateTime\]::Now\b|\.LocalDateTime\b|ToLocalTime\(", outside), [])

    def test_the_contract_is_documented_in_both_headers(self) -> None:
        mod = MODULE.read_text(encoding="utf-8")
        run = RUNNER.read_text(encoding="utf-8")
        for needle in ("TICKET QUEUE (VENUE-CLAIM-FIFO-1)", "BACK-COMPAT", "Ticketing orders ONLY ticket holders", "IO.FileMode]::CreateNew"):
            self.assertIn(needle, mod)
        for needle in ("2b. FIFO (VENUE-CLAIM-FIFO-1)", "-NoTicket", "-ClaimStatus", "-TicketPollSec", "-TicketKeepMax", "-ProbeAdmissionWaitSec"):
            self.assertIn(needle, run)
        for f in (RUNNER, MODULE):
            self.assertTrue(all(b < 128 for b in f.read_bytes()), f"{f.name} is not ASCII")


LEASE_RUNNER = DV / "Invoke-VenueLeaseBatch.ps1"


@requires_windows_pwsh
class VenueLeaseBatchFifoTests(unittest.TestCase):
    """VENUE-FIFO-TICKET-BEFORE-PRECLAIM-1 r2: a lease batch (Invoke-VenueLeaseBatch.ps1) joins the venue's line like a chain does.

    THE 2026-11-20 OVERTAKE, simulated: LJ92 r8 had waited for the Bachelor claim since 08:17:03Z (CLAIM_HELD on a 30 s poll); PLAYBACK-BASELINE-RANGE-1 r3's second batch
    first asked at 09:01:53Z, and when BENCH-CLOCKS r5 released at 09:24:54Z its poll landed inside the 16 s the claim was free (taken 09:25:10Z) and LJ92 r8's did not: it ended
    DEADLINE claim-held with 0 of 3 legs run. Lease batches took no ticket (Request-VenueClaim without -UseTicket), so nothing ordered them.
      * the process case: a foreign claim stands; batch A (a 5 s poll) is waiting; batch B (a 1 s poll) arrives later; the claim frees. A must take it first.
      * the readiness case (module): a ticket whose holder is in a not-ready wait (phase 'cooldown') never blocks a ready waiter behind it, and keeps its place
        (when its holder turns ready it is again ahead of every later arrival).
    """

    @classmethod
    def setUpClass(cls) -> None:
        from tools.repo_hygiene import test_venue_lease_batch as lease
        cls.lease = lease
        cls.tmp = Path(tempfile.mkdtemp(prefix="venue-lease-fifo-"))
        for name, text in (("fake-quiet.ps1", lease.FAKE_QUIET), ("fake-umrun.ps1", lease.FAKE_UMRUN), ("fake-leg.ps1", lease.FAKE_LEG)):
            (cls.tmp / name).write_text(text, encoding="utf-8")
        cls.spec = cls.tmp / "fx-a.json"
        cls.spec.write_text(json.dumps({"schema": "mlv-app/dual-venue-leg/v1", "card": "TEST-CARD", "legId": "fx-a", "backends": ["cpu", "cuda"], "scaleFactor": 1}), encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self) -> None:
        self.case = Path(tempfile.mkdtemp(prefix="case-", dir=self.tmp))
        self.claims = self.case / "claims"
        self.claim_path = self.claims / "bachelor.claim.json"
        self.ticket_dir = self.claims / "bachelor.tickets"
        self._children: list[subprocess.Popen] = []

    def tearDown(self) -> None:
        for p in self._children:
            if p.poll() is None:
                p.kill()
            p.wait()

    def sleeper(self) -> subprocess.Popen:
        # r8: a live process of its own for a ticket owner that is not the scenario's pwsh (the same helper as VenueClaimFifoTests.sleeper)
        p = subprocess.Popen([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "Start-Sleep -Seconds 120"])
        self._children.append(p)
        time.sleep(1.0)
        return p

    def ps(self, script: str, module: Path | None = None) -> dict:
        f = self.case / f"scenario-{len(list(self.case.glob('scenario-*.ps1')))}.ps1"
        f.write_text(f"$ErrorActionPreference = 'Stop'\nImport-Module '{module or MODULE}' -Force -DisableNameChecking\n$d = '{self.claims}'\n{script}\n", encoding="utf-8")
        r = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(f)], capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise AssertionError(f"scenario failed ({r.returncode}):\n{r.stdout}\n{r.stderr}")
        return json.loads([l for l in r.stdout.splitlines() if l.strip()][-1])

    def start_batch(self, name: str, claim_poll: int, specs: str = "", extra: tuple = (), cooldown: str = "0", runner: Path | None = None, deadline_sec: float = 200.0, deadline_utc: datetime | None = None, **env: str) -> subprocess.Popen:
        run_dir, share = self.case / f"run-{name}", self.case / f"share-{name}"
        share.mkdir()
        (share / "cancel").mkdir()
        (share / "notafter").mkdir()
        e = dict(os.environ)
        e.update({"FAKE_EVENTS": str(self.case / f"events-{name}.log"), "FAKE_SHARE": str(share), "FAKE_CLAIM_PATH": str(self.claim_path), "FAKE_LEG_ROOT": str(self.case / f"root-{name}"),
                  "FAKE_CAPS_EVENTS": str(self.case / f"caps-{name}.log")})
        e.update(env)
        args = [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(runner or LEASE_RUNNER), "-LegSpec", specs or str(self.spec), "-RunDir", str(run_dir), "-SourceCommit", "a" * 40,
                "-BuildManifestSha256", "b" * 64, "-ClaimDir", str(self.claims), "-QuietScript", str(self.tmp / "fake-quiet.ps1"), "-LegScript", str(self.tmp / "fake-leg.ps1"),
                "-UmRunScript", str(self.tmp / "fake-umrun.ps1"), "-AgentShare", str(share), "-LegBudgetSec", "60", "-LeaseMarginSec", "0", "-ClaimPollSec", str(claim_poll),
                "-RecheckSec", "1", "-LeaseCooldownSec", cooldown, "-DeadlineUtc", utc_iso(deadline_utc or datetime.now(timezone.utc) + timedelta(seconds=deadline_sec)), *extra]
        out_path = self.case / f"stdout-{name}.txt"
        with open(out_path, "w", encoding="utf-8") as out_file:
            p = subprocess.Popen(args, stdout=out_file, stderr=subprocess.STDOUT, text=True, env=e)
        p.out_path = out_path   # type: ignore[attr-defined]
        p.run_dir = run_dir     # type: ignore[attr-defined]
        self._children.append(p)
        return p

    @staticmethod
    def batch_log(p: subprocess.Popen) -> str:
        f = Path(p.run_dir) / "batch.log"   # type: ignore[attr-defined]
        return f.read_text(encoding="utf-8", errors="replace") if f.exists() else ""

    def wait_for(self, predicate, what: str, timeout: float = 90.0) -> None:
        t0 = time.time()
        while time.time() - t0 < timeout:
            if predicate():
                return
            time.sleep(0.2)
        self.fail(f"timed out waiting for {what}")

    def claim_taken_utc(self, p: subprocess.Popen) -> datetime:
        import re
        m = re.search(r"CLAIM_TAKEN batch=1 nonce=\S+ expiresUtc=(\S+)", self.batch_log(p))
        self.assertIsNotNone(m, f"no CLAIM_TAKEN line:\n{self.batch_log(p)}\n{Path(p.out_path).read_text(encoding='utf-8', errors='replace')}")   # type: ignore[attr-defined]
        return datetime.fromisoformat(m.group(1).replace("Z", "+00:00")[:26] + "+00:00" if "+" not in m.group(1)[-6:] else m.group(1))

    def test_a_lease_batch_that_waited_longer_takes_the_claim_before_a_later_one_that_polls_faster(self) -> None:
        sleeper = subprocess.Popen([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "Start-Sleep -Seconds 180"])
        self._children.append(sleeper)
        time.sleep(1.0)
        foreign = self.ps(f"$r = Request-VenueClaim -Venue bachelor -RunDir 'run-foreign' -Purpose foreign -TtlSec 3600 -ClaimDir $d -OwnerPid {sleeper.pid}\n"
                          "[pscustomobject]@{ granted = [bool]$r.granted; nonce = $r.claim.nonce } | ConvertTo-Json -Compress")
        self.assertTrue(foreign["granted"])
        pa = self.start_batch("A", claim_poll=5)      # arrives first, polls its turn every 5 s
        self.wait_for(lambda: "CLAIM_HELD" in self.batch_log(pa), "A waiting behind the foreign claim")
        pb = self.start_batch("B", claim_poll=1)      # arrives later, polls every second
        self.wait_for(lambda: "CLAIM_HELD" in self.batch_log(pb), "B waiting behind the foreign claim")
        rel = self.ps(f"[pscustomobject]@{{ rel = (Release-VenueClaim -Venue bachelor -Nonce '{foreign['nonce']}' -ClaimDir $d) }} | ConvertTo-Json -Compress")
        self.assertEqual(rel["rel"], "RELEASED")
        for p in (pa, pb):
            p.wait(timeout=180)
        outs = [Path(p.out_path).read_text(encoding="utf-8", errors="replace") for p in (pa, pb)]   # type: ignore[attr-defined]
        self.assertEqual([p.returncode for p in (pa, pb)], [0, 0], "\n----\n".join(outs))
        self.assertLess(self.claim_taken_utc(pa), self.claim_taken_utc(pb), "B, the later arrival, polled faster and took the claim first: the lease batch is not first-come-first-served")
        self.assertFalse(self.claim_path.exists(), "a claim was left behind")
        self.assertEqual(list(self.ticket_dir.glob("*.ticket.json")) if self.ticket_dir.exists() else [], [], "a ticket was left behind")
        self.assertIn("TICKET_TAKEN", self.batch_log(pa))

    def test_tail1_a6_an_explicit_no_ticket_batch_leaves_no_ticket_whatever_the_callers_ambient_defaults_are(self) -> None:
        # pass 11 A6: -NoTicket omitted -UseTicket from Request-VenueClaim instead of binding it to false, so a caller that sets $PSDefaultParameterValues['Request-VenueClaim:UseTicket'] = $true (a profile, a wrapper)
        # gave the opt-out batch a ticket that nothing closed (TicketOn is false, so the ordinary close skips it). The wrapper sets that default and runs the real batch with -NoTicket against a free claim.
        wrapper = self.case / "wrap-ambient-default.ps1"
        wrapper.write_text("$PSDefaultParameterValues['Request-VenueClaim:UseTicket'] = $true\n& '" + str(LEASE_RUNNER) + "' @args\nexit $LASTEXITCODE\n", encoding="utf-8")
        p = self.start_batch("N", claim_poll=1, runner=wrapper, extra=("-NoTicket",))
        p.wait(timeout=180)
        out = Path(p.out_path).read_text(encoding="utf-8", errors="replace")   # type: ignore[attr-defined]
        self.assertEqual(p.returncode, 0, out)
        self.assertEqual(list(self.ticket_dir.glob("*.ticket.json")) if self.ticket_dir.exists() else [], [], f"an opt-out batch left a ticket behind:\n{self.batch_log(p)}")
        self.assertNotIn("TICKET_TAKEN", self.batch_log(p), "an opt-out batch took a ticket")

    READY_FIRST = """
$A = New-VenueTicket -Venue bachelor -RunDir 'run-A' -ClaimDir $d -Phase cooldown -StaleAfterSec 600
Start-Sleep -Milliseconds 30
$B = New-VenueTicket -Venue bachelor -RunDir 'run-B' -ClaimDir $d
$b1 = Request-VenueClaim -Venue bachelor -RunDir 'run-B' -ClaimDir $d -Ticket $B
$rel = Release-VenueClaim -Venue bachelor -Nonce $b1.claim.nonce -ClaimDir $d
$endB = Remove-VenueTicket $B
Start-Sleep -Milliseconds 30
$C = New-VenueTicket -Venue bachelor -RunDir 'run-C' -ClaimDir $d
$phaseBefore = (Get-VenueTickets -Venue bachelor -ClaimDir $d | Where-Object { $_.ticket.runDir -eq 'run-A' }).ticket.phase
$a1 = Request-VenueClaim -Venue bachelor -RunDir 'run-A' -ClaimDir $d -Ticket $A
$phaseAfter = (Get-VenueTickets -Venue bachelor -ClaimDir $d | Where-Object { $_.ticket.runDir -eq 'run-A' }).ticket.phase
$c1 = Request-VenueClaim -Venue bachelor -RunDir 'run-C' -ClaimDir $d -Ticket $C
$relA = Release-VenueClaim -Venue bachelor -Nonce $a1.claim.nonce -ClaimDir $d
[pscustomobject]@{ b1 = $b1.state; b1Granted = [bool]$b1.granted; b1Pos = $b1.position; rel = $rel; endB = $endB; c1 = $c1.state; c1Granted = [bool]$c1.granted; phaseBefore = $phaseBefore
  a1 = $a1.state; a1Granted = [bool]$a1.granted; phaseAfter = $phaseAfter; relA = $relA } | ConvertTo-Json -Compress
"""

    def test_a_not_ready_ticket_never_blocks_a_ready_waiter_and_keeps_its_place(self) -> None:
        r = self.ps(self.READY_FIRST)
        self.assertEqual((r["b1"], r["b1Granted"], r["b1Pos"]), ("TAKEN", True, 2), "A is in a not-ready wait (cooldown): the ready B behind it takes the free claim")
        self.assertEqual((r["rel"], r["endB"]), ("RELEASED", "REMOVED"))
        self.assertEqual(r["phaseBefore"], "cooldown")
        self.assertEqual((r["a1"], r["a1Granted"]), ("TAKEN", True), "A kept its place through B's turn: once A is ready it is ahead of C, which arrived after it, and takes the claim")
        self.assertEqual(r["phaseAfter"], "waiting", "a request refreshes the caller's ticket to ready")
        self.assertEqual((r["c1"], r["c1Granted"]), ("QUEUED", False), "C arrived after A and A is now ready ahead of it: C waits behind A")
        self.assertEqual(r["relA"], "RELEASED")

    CASE_SPELLING = """
$A = New-VenueTicket -Venue bachelor -RunDir 'run-A' -ClaimDir $d -Phase COOLDOWN -StaleAfterSec 600
Start-Sleep -Milliseconds 30
$B = New-VenueTicket -Venue bachelor -RunDir 'run-B' -ClaimDir $d
$phaseA = (Get-VenueTickets -Venue bachelor -ClaimDir $d | Where-Object { $_.ticket.runDir -eq 'run-A' }).ticket.phase
$b1 = Request-VenueClaim -Venue bachelor -RunDir 'run-B' -ClaimDir $d -Ticket $B
[pscustomobject]@{ phaseA = $phaseA; b1 = $b1.state; b1Granted = [bool]$b1.granted } | ConvertTo-Json -Compress
"""

    def test_a_cooldown_phase_in_any_letter_case_is_stored_as_cooldown_and_never_blocks_a_ready_waiter(self) -> None:
        # adversary A2 (r3 pass 1): ValidateSet accepts COOLDOWN and kept it, the case-sensitive readiness test then took the not-ready ticket for the ready head, and B was QUEUED behind it
        r = self.ps(self.CASE_SPELLING)
        self.assertEqual(r["phaseA"], "cooldown", "the phase is stored in its one spelling")
        self.assertEqual((r["b1"], r["b1Granted"]), ("TAKEN", True), "a cooldown ticket, however spelled, never blocks a ready waiter behind it")

    def wait_for_cooldown_ticket(self, p: subprocess.Popen) -> None:
        # BATCH_WAIT is logged BEFORE the ticket is taken (the ticket call is a bounded read on its own thread), so wait for the TICKET_TAKEN line: the ticket file exists once it is logged
        try:
            self.wait_for(lambda: "TICKET_TAKEN batch=2" in self.batch_log(p), "batch 2's cooldown ticket taken")
        except AssertionError as exc:
            self.fail(f"{exc}\n{self.batch_log(p)}")

    def test_a_slow_ticket_worker_leaves_no_ticket_at_batch_wait_and_the_helper_waits_for_ticket_taken(self) -> None:
        # Grok CoS G2(b) (#878): the r4 race was a heartbeat test that waited for BATCH_WAIT, logged before the cooldown ticket is taken. The ticket worker is made SLOW here (a copy of
        # the module whose New-VenueTicket sleeps 2.5 s, inside Invoke-BatchTicketCall's 5 s bound): at BATCH_WAIT there is no ticket yet, and wait_for_cooldown_ticket (the wait the heartbeat
        # test uses) returns only once the ticket file exists. A helper that waited for BATCH_WAIT would find none here.
        text = MODULE.read_text(encoding="utf-8")
        anchor = "    New-Item -ItemType Directory -Force -Path $dir | Out-Null\n    $ownerStart = Get-VenueProcessStartUtc $OwnerPid"
        self.assertEqual(text.count(anchor), 1, "slow-worker anchor moved")
        runner = copy_with_siblings(LEASE_RUNNER, Path(tempfile.mkdtemp(prefix="slow-", dir=self.case)))
        (runner.parent / MODULE.name).write_text(text.replace(anchor, "    Start-Sleep -Milliseconds 2500\n" + anchor), encoding="utf-8")
        import shutil   # the runner reads the agent's own script at ..\bachelor\ultra-magnus-agent.ps1 for the liveness read-back
        (runner.parent.parent / "bachelor").mkdir(exist_ok=True)
        shutil.copy2(DV.parent / "bachelor" / "ultra-magnus-agent.ps1", runner.parent.parent / "bachelor" / "ultra-magnus-agent.ps1")
        spec_b = self.tmp / "fx-b.json"
        spec_b.write_text(json.dumps({"schema": "mlv-app/dual-venue-leg/v1", "card": "TEST-CARD", "legId": "fx-b", "backends": ["cpu", "cuda"], "scaleFactor": 1}), encoding="utf-8")
        p = self.start_batch("S", claim_poll=1, specs=f"{self.spec},{spec_b}", extra=("-TicketBeatSec", "1"), cooldown="14", runner=runner, FAKE_LEASE_REMAIN="70,2700", FAKE_LEG_REMAIN="50,2600")
        def tickets() -> list:
            return list(self.ticket_dir.glob("*.ticket.json")) if self.ticket_dir.exists() else []
        try:
            self.wait_for(lambda: "BATCH_WAIT batch=2" in self.batch_log(p), "batch 2's BATCH_WAIT line")
        except AssertionError as exc:
            self.fail(f"{exc}\n{self.batch_log(p)}")
        self.assertEqual(tickets(), [], f"a ticket exists at BATCH_WAIT although the worker is slow: the slow-worker seam did not bite\n{self.batch_log(p)}")
        self.wait_for_cooldown_ticket(p)
        self.assertEqual(len(tickets()), 1, f"TICKET_TAKEN is logged but the ticket file is not there\n{self.batch_log(p)}")
        log = self.batch_log(p)
        self.assertLess(log.index("BATCH_WAIT batch=2"), log.index("TICKET_TAKEN batch=2"))
        p.wait(timeout=180)

    ATOMIC_PHASE = """
$A = New-VenueTicket -Venue bachelor -RunDir 'run-A' -ClaimDir $d
$r1 = Update-VenueTicket -Ticket $A -Phase 'CoolDown' -StaleAfterSec 600
$p1 = (Get-Content -LiteralPath $A.path -Raw | ConvertFrom-Json).phase
$r2 = Update-VenueTicket -Ticket $A -Phase 'PROBING'
$p2 = (Get-Content -LiteralPath $A.path -Raw | ConvertFrom-Json).phase
$r3 = Update-VenueTicket -Ticket $A
$p3 = (Get-Content -LiteralPath $A.path -Raw | ConvertFrom-Json).phase
$bad = 'none'; try { [void](Update-VenueTicket -Ticket $A -Phase 'bogus') } catch { $bad = 'refused' }
$p4 = (Get-Content -LiteralPath $A.path -Raw | ConvertFrom-Json).phase
$left = @(Get-ChildItem -LiteralPath (Split-Path -Parent $A.path) -Force | Where-Object { $_.Name -notlike '*.ticket.json' -and $_.Name -ne '.ticket.lock' }).Count   # .ticket.lock is the ticket folder's lock file, left in place by design (never listed)
[pscustomobject]@{ r1 = $r1; p1 = $p1; r2 = $r2; p2 = $p2; r3 = $r3; p3 = $p3; bad = $bad; p4 = $p4; left = $left } | ConvertTo-Json -Compress
"""

    def test_update_venue_ticket_stores_one_lower_case_spelling_of_a_known_phase_and_refuses_an_unknown_one(self) -> None:
        # Grok CoS G5 (#878): New-VenueTicket validates and lower-cases the phase; Update-VenueTicket stored any string as given. The set is every phase a caller passes (the chain
        # sets probing and supplementary, Invoke-VenueChain.ps1; the lease batch cooldown), wider than the CoS's suggested three.
        r = self.ps(self.ATOMIC_PHASE)
        self.assertEqual((r["r1"], r["p1"]), ("OK", "cooldown"), "a mixed-case phase is stored lower-case")
        self.assertEqual((r["r2"], r["p2"]), ("OK", "probing"))
        self.assertEqual((r["r3"], r["p3"]), ("OK", "probing"), "no -Phase keeps the stored phase")
        self.assertEqual((r["bad"], r["p4"]), ("refused", "probing"), "an unknown phase is refused and the ticket is unchanged")
        self.assertEqual(r["left"], 0, "a temp file was left beside the tickets")

    def test_a_cooldown_ticket_is_kept_alive_by_a_heartbeat_not_a_lifetime_as_long_as_the_wait(self) -> None:
        # adversary A3 (r3 pass 1): the ticket lived waitSec + 120 s, clamped to 86400 s, so a 172800 s cooldown (2880 min) let a ready waiter reap the live holder's ticket mid-wait
        # and the holder came back behind it. Now the lifetime is short and the wait refreshes the heartbeat (-TicketBeatSec).
        spec_b = self.tmp / "fx-b.json"
        spec_b.write_text(json.dumps({"schema": "mlv-app/dual-venue-leg/v1", "card": "TEST-CARD", "legId": "fx-b", "backends": ["cpu", "cuda"], "scaleFactor": 1}), encoding="utf-8")
        p = self.start_batch("W", claim_poll=1, specs=f"{self.spec},{spec_b}", extra=("-TicketBeatSec", "1"), cooldown="14", FAKE_LEASE_REMAIN="70,2700", FAKE_LEG_REMAIN="50,2600")
        self.wait_for_cooldown_ticket(p)
        def ticket() -> dict:
            files = list(self.ticket_dir.glob("*.ticket.json")) if self.ticket_dir.exists() else []
            self.assertEqual(len(files), 1, f"one cooldown ticket expected, found {files}\n{self.batch_log(p)}")
            return json.loads(files[0].read_text(encoding="utf-8"))
        t1 = ticket()
        self.assertEqual(t1["phase"], "cooldown")
        self.assertLessEqual(t1["staleAfterSec"], 900, "the ticket's lifetime must not grow with the wait")
        time.sleep(4.0)
        t2 = ticket()
        self.assertNotEqual(t1["heartbeatUtc"], t2["heartbeatUtc"], "the wait refreshed no heartbeat: a long cooldown outlives its ticket")
        p.wait(timeout=180)

    def test_the_ticket_is_never_removed_by_unbounded_io_on_a_deadline_bound_path(self) -> None:
        # adversary A1 (r3 pass 1): Close-BatchTicket ran Remove-VenueTicket inline in the input-guard refusal and the DEADLINE break, ahead of the bounded release and the typed refusal;
        # a claim folder that stalls held them past -DeadlineUtc. Those paths leave the ticket to be reaped as dead-pid (the process ends right after); the grant path removes it bounded.
        import re
        run = LEASE_RUNNER.read_text(encoding="utf-8")
        func = re.search(r"function Close-BatchTicket.*?\n}\n", run, re.S)
        self.assertIsNotNone(func)
        self.assertIn("Invoke-BatchTicketCall", func.group(0), "the ticket removal is not bounded")
        helper = re.search(r"function Invoke-BatchTicketCall.*?\n}\n", run, re.S)
        self.assertIsNotNone(helper)
        self.assertIn("Invoke-VenueBoundedRead", helper.group(0), "the ticket helper is not a bounded read")
        self.assertNotRegex(func.group(0), r"Remove-VenueTicket \$t\)\s*\}\s*catch", "the ticket is removed inline")
        hit_block = run[run.index("        if ($hit) {"):run.index("        if ($hit) {") + 700]
        self.assertNotIn("Close-BatchTicket", hit_block, "the input-guard refusal removes the ticket inline")
        dl_block = run[run.index("    if ($null -eq $claim) {"):run.index("    if ($null -eq $claim) {") + 400]
        self.assertNotIn("Close-BatchTicket", dl_block, "the DEADLINE break removes the ticket inline")

    BUSY_HOLDER = """
$A = New-VenueTicket -Venue bachelor -RunDir 'run-A' -ClaimDir $d -Phase cooldown -StaleAfterSec 600
Start-Sleep -Milliseconds 30
$B = New-VenueTicket -Venue bachelor -RunDir 'run-B' -ClaimDir $d
$fs = [IO.File]::Open($A.path, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
try { $b1 = Request-VenueClaim -Venue bachelor -RunDir 'run-B' -ClaimDir $d -Ticket $B } finally { $fs.Dispose() }
[pscustomobject]@{ b1 = $b1.state; b1Granted = [bool]$b1.granted } | ConvertTo-Json -Compress
"""

    def test_a_ticket_being_rewritten_whose_phase_cannot_be_read_never_blocks_a_ready_waiter(self) -> None:
        # adversary pass 2 A2: a cooldown ticket held by a writer reads as BUSY with no parsed ticket; its missing phase passed the readiness filter and QUEUED the ready B on a free claim
        r = self.ps(self.BUSY_HOLDER)
        self.assertEqual((r["b1"], r["b1Granted"]), ("TAKEN", True), "an unreadable ticket's phase is unknown: it is not ready, so it cannot block")

    def test_an_older_ready_ticket_being_heartbeat_rewritten_is_never_overtaken_by_a_younger_waiter(self) -> None:
        # Grok CoS G4 (#878, 678e5fad): Update-VenueTicket rewrote the ticket IN PLACE (open ReadWrite + FileShare.Read, SetLength(0), Write), so a younger waiter that read the older
        # READY ticket inside that window got BUSY or a truncated body, took it for not ready, made itself head and took the claim. The REAL writer runs in one process while
        # a younger waiter requests the claim in another, over and over: the younger one must never be granted while the older ready ticket stands in the line.
        ready, done = self.case / "updater-ready.txt", self.case / "updater-done.txt"
        updater = self.case / "updater.ps1"
        updater.write_text(   # the updater process OWNS the older ticket (a ticket whose owner pid is dead is reaped as stale), so it must stay alive while the race runs
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{MODULE}' -Force -DisableNameChecking\n"
            f"$A = New-VenueTicket -Venue bachelor -RunDir 'run-A' -ClaimDir '{self.claims}' -StaleAfterSec 600\n"
            f"[IO.File]::WriteAllText('{ready}', '1')\n"
            "$end = [DateTime]::UtcNow.AddSeconds(10); $ok = 0; $other = 0\n"
            "while ([DateTime]::UtcNow -lt $end) { if ((Update-VenueTicket -Ticket $A -Phase 'waiting') -ceq 'OK') { $ok++ } else { $other++ } }\n"
            f"[IO.File]::WriteAllText('{done}', ('{{0}} {{1}}' -f $ok, $other))\n", encoding="utf-8")
        p = subprocess.Popen([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(updater)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._children.append(p)
        t0 = time.time()
        while not ready.exists() and time.time() - t0 < 60:
            time.sleep(0.1)
        self.assertTrue(ready.exists(), "the updater process never started")
        r = self.ps(f"""
$B = New-VenueTicket -Venue bachelor -RunDir 'run-B' -ClaimDir $d
$n = 0; $wins = 0; $stop = [DateTime]::UtcNow.AddSeconds(40)
while (-not (Test-Path -LiteralPath '{done}') -and [DateTime]::UtcNow -lt $stop) {{
  $r = Request-VenueClaim -Venue bachelor -RunDir 'run-B' -ClaimDir $d -Ticket $B
  if ($r.granted) {{ if (-not (Test-Path -LiteralPath '{done}')) {{ $wins++ }}; [void](Release-VenueClaim -Venue bachelor -Nonce $r.claim.nonce -ClaimDir $d) }}   # adversary A11: a win counts only while the older owner still runs (it may end between this request and the check: it is then reaped as dead-pid, legitimately)
  $n++
}}
[pscustomobject]@{{ requests = $n; wins = $wins }} | ConvertTo-Json -Compress
""")
        p.wait(timeout=60)
        ok = int(done.read_text(encoding="utf-8").split()[0])
        self.assertGreater(ok, 20, "the older ticket was hardly rewritten: the race was not exercised")
        self.assertGreater(r["requests"], 10, "the younger waiter hardly asked: the race was not exercised")
        self.assertEqual(r["wins"], 0, f"the younger waiter took the claim {r['wins']} times in {r['requests']} requests while the older READY ticket was being rewritten")

    FLIP_BUSY = """
$A = New-VenueTicket -Venue bachelor -RunDir 'run-A' -ClaimDir $d -Phase @@PHASE@@ -StaleAfterSec 600
Start-Sleep -Milliseconds 30
$B = New-VenueTicket -Venue bachelor -RunDir 'run-B' -ClaimDir $d
[IO.File]::WriteAllText('@@READY@@', $A.path)
$t0 = [DateTime]::UtcNow
while (-not (Test-Path -LiteralPath '@@HELD@@') -and ([DateTime]::UtcNow - $t0).TotalSeconds -lt 30) { Start-Sleep -Milliseconds 50 }
$sw = [Diagnostics.Stopwatch]::StartNew()
$r = Request-VenueClaim -Venue bachelor -RunDir 'run-A' -ClaimDir $d -Ticket $A
$elapsed = $sw.Elapsed.TotalSeconds
$phase = (Get-Content -LiteralPath $A.path -Raw | ConvertFrom-Json).phase
[pscustomobject]@{ state = $r.state; granted = [bool]$r.granted; phase = $phase; note = [string]$r.note; ticket = $A.name; elapsed = $elapsed } | ConvertTo-Json -Compress
"""

    def run_flip_with_a_foreign_handle(self, release_after_sec: float | None, phase: str = "cooldown", template: str | None = None) -> dict:
        # a handle opened WITHOUT FileShare.Delete (CPython's open() on Windows shares read and write, not delete) on the older waiter's ticket makes the heartbeat's rename-over fail
        import threading
        ready, held = self.case / "flip-ready.txt", self.case / "flip-held.txt"
        stop = threading.Event()
        def holder() -> None:
            t0 = time.time()
            while not ready.exists() and time.time() - t0 < 60:
                time.sleep(0.02)
            path = ready.read_text(encoding="utf-8").strip()
            fh = open(path, "rb")
            try:
                held.write_text("1", encoding="utf-8")
                if release_after_sec is None:
                    stop.wait(60)
                else:
                    time.sleep(release_after_sec)
            finally:
                fh.close()
        th = threading.Thread(target=holder, daemon=True)
        th.start()
        try:
            return self.ps((template or self.FLIP_BUSY).replace("@@READY@@", str(ready)).replace("@@HELD@@", str(held)).replace("@@PHASE@@", phase))
        finally:
            stop.set()
            th.join(timeout=70)

    def test_a_waiter_whose_phase_flip_stays_busy_is_queued_and_never_goes_on_as_if_it_landed(self) -> None:
        # Grok CoS G7 (#878, 75d63036): Request-VenueClaim acted only on LOST from the cooldown -> waiting flip. On BUSY it went on to the head check while its ticket still read cooldown, so
        # every other waiter's ready check skipped it, and it took the claim on a ticket the line saw as not ready. On a flip that stays BUSY it must return QUEUED (not granted), with a typed note.
        r = self.run_flip_with_a_foreign_handle(None)
        self.assertEqual((r["state"], r["granted"]), ("QUEUED", False), f"the flip did not land but the request went on: {r}")
        self.assertEqual(r["phase"], "cooldown", "the ticket was never rewritten while the foreign handle held it")
        self.assertIn("TICKET_PHASE_BUSY", r["note"], "the typed line is missing")
        self.assertIn(r["ticket"], r["note"], "the typed line does not name the ticket")
        self.assertIn("waiting", r["note"], "the typed line does not name the phase")

    def test_a_phase_flip_that_is_busy_for_a_moment_is_retried_and_lands(self) -> None:
        # G7: a BUSY that clears within the short bound (about 2 s) is retried, not given up on: the flip lands and the request goes on as a ready waiter.
        r = self.run_flip_with_a_foreign_handle(0.9)
        self.assertEqual(r["phase"], "waiting", f"a transient BUSY was not retried until the flip landed: {r}")
        self.assertEqual((r["state"], r["granted"]), ("TAKEN", True), r)
        self.assertEqual(r["note"], "")

    ORPHAN_TMP = """
$A = New-VenueTicket -Venue bachelor -RunDir 'run-A' -ClaimDir $d
$dir = Split-Path -Parent $A.path
$old = Join-Path $dir ($A.name + '.' + ('a' * 32) + '.tmp')
$new = Join-Path $dir ($A.name + '.' + ('b' * 32) + '.tmp')
$odd = Join-Path $dir 'notes.tmp'
foreach ($p in @($old, $new, $odd)) { [IO.File]::WriteAllText($p, 'x') }
(Get-Item -LiteralPath $old).LastWriteTimeUtc = [DateTime]::UtcNow.AddMinutes(-10)
(Get-Item -LiteralPath $odd).LastWriteTimeUtc = [DateTime]::UtcNow.AddMinutes(-10)
$r = Request-VenueClaim -Venue bachelor -RunDir 'run-A' -ClaimDir $d -Ticket $A
[pscustomobject]@{ oldGone = -not (Test-Path -LiteralPath $old); newKept = (Test-Path -LiteralPath $new); oddKept = (Test-Path -LiteralPath $odd); ticketKept = (Test-Path -LiteralPath $A.path); state = $r.state } | ConvertTo-Json -Compress
"""

    def test_an_orphaned_ticket_temp_file_older_than_five_minutes_is_reaped_and_a_fresh_one_is_kept(self) -> None:
        # Grok CoS G8 (#878): Write-VenueTicketAtomic leaves <ticket>.<guid>.tmp behind when the process dies between CreateNew and the rename. The stale sweep reaps those older than 5 minutes.
        r = self.ps(self.ORPHAN_TMP)
        self.assertTrue(r["oldGone"], "an orphaned ticket temp file older than 5 minutes was not reaped")
        self.assertTrue(r["newKept"], "a fresh temp file (a heartbeat in flight) was removed")
        self.assertTrue(r["oddKept"], "a file that is not a ticket temp name was removed")
        self.assertTrue(r["ticketKept"])

    # ---- r7: the adversary pass on fb635c0b (A1 A2 A3 A4 A5 A6 A7 A8 A9 A10 A13) ----

    def seam_module(self, anchor: str, replacement: str) -> Path:
        text = MODULE.read_text(encoding="utf-8")
        self.assertEqual(text.count(anchor), 1, f"seam anchor moved: {anchor!r}")
        return copy_with_siblings(MODULE, Path(tempfile.mkdtemp(prefix="seam-", dir=self.case)), text.replace(anchor, replacement))

    SEAM_A1 = """
$A = New-VenueTicket -Venue bachelor -RunDir 'run-A' -ClaimDir $d -Phase cooldown -StaleAfterSec 600
Start-Sleep -Milliseconds 30
$B = New-VenueTicket -Venue bachelor -RunDir 'run-B' -ClaimDir $d
$job = Start-Job -ArgumentList $B, $d, '@@SEAMMOD@@', '@@MARK@@' -ScriptBlock { param($B, $d, $m, $mark) $env:SEAM_MARK = $mark; Import-Module $m -Force -DisableNameChecking; Request-VenueClaim -Venue bachelor -RunDir 'run-B' -ClaimDir $d -Ticket $B }
$t0 = [DateTime]::UtcNow
while (-not (Test-Path -LiteralPath '@@MARK@@') -and ([DateTime]::UtcNow - $t0).TotalSeconds -lt 60) { Start-Sleep -Milliseconds 50 }
$flip = Update-VenueTicket -Ticket $A -Phase 'waiting'
$r = Receive-Job $job -Wait -AutoRemoveJob
[pscustomobject]@{ flip = $flip; state = [string]$r.state; granted = [bool]$r.granted; claimExists = (Test-Path -LiteralPath (Join-Path $d 'bachelor.claim.json')) } | ConvertTo-Json -Compress
"""

    def test_a_waiter_that_picked_itself_head_gives_the_claim_back_when_an_older_ticket_became_ready_meanwhile(self) -> None:
        # r7 adversary A1 (blocker): B chose itself head from a snapshot in which the older A was still in cooldown, A's flip to waiting landed, and B then took the claim from that old
        # snapshot. B is paused between its head check and its claim attempt (a seam in a module copy); A's flip lands meanwhile; B must not keep a claim an older ready ticket is owed.
        mark = self.case / "seam-a1-mark.txt"
        mod = self.seam_module("    $r = Invoke-VenueClaimAttempt @attemptArgs\n", "    [IO.File]::WriteAllText($env:SEAM_MARK, '1'); Start-Sleep -Milliseconds 2500\n    $r = Invoke-VenueClaimAttempt @attemptArgs\n")
        r = self.ps(self.SEAM_A1.replace("@@SEAMMOD@@", str(mod)).replace("@@MARK@@", str(mark)))
        self.assertEqual(r["flip"], "OK")
        self.assertEqual((r["state"], r["granted"]), ("QUEUED", False), f"the younger waiter kept the claim although the older ticket became ready before it was taken: {r}")
        self.assertFalse(r["claimExists"], "the claim was not given back")

    SEAM_A2 = """
$A = New-VenueTicket -Venue bachelor -RunDir 'run-A' -ClaimDir $d -StaleAfterSec 600
$job = Start-Job -ArgumentList $A, '@@SEAMMOD@@', '@@MARK@@' -ScriptBlock { param($A, $m, $mark) $env:SEAM_MARK = $mark; Import-Module $m -Force -DisableNameChecking; Update-VenueTicket -Ticket $A -Phase 'waiting' }
$t0 = [DateTime]::UtcNow
while (-not (Test-Path -LiteralPath '@@MARK@@') -and ([DateTime]::UtcNow - $t0).TotalSeconds -lt 60) { Start-Sleep -Milliseconds 50 }
$removed = Remove-VenueTicket -Ticket $A
$beat = Receive-Job $job -Wait -AutoRemoveJob
[pscustomobject]@{ removed = [string]$removed; beat = [string]$beat; exists = (Test-Path -LiteralPath $A.path) } | ConvertTo-Json -Compress
"""

    def test_a_heartbeat_in_flight_cannot_bring_back_a_ticket_that_was_just_removed(self) -> None:
        # r7 adversary A2: the heartbeat read the nonce and closed its handle; its rename-over then RE-CREATED a ticket a reaper or its owner had removed in between (File.Move with overwrite
        # publishes to an absent name), so a reaped or ended ticket came back. The heartbeat now holds its read handle (share Read + Delete) across the check and the rename: a remove cannot open the file.
        mark = self.case / "seam-a2-mark.txt"
        anchor = "try { Write-VenueTicketAtomic -Path $Ticket.path "   # a prefix: the call's later arguments (the budget) may change
        mod = self.seam_module(anchor, "[IO.File]::WriteAllText($env:SEAM_MARK, '1'); Start-Sleep -Milliseconds 2500; " + anchor)
        r = self.ps(self.SEAM_A2.replace("@@SEAMMOD@@", str(mod)).replace("@@MARK@@", str(mark)))
        self.assertNotEqual(r["removed"], "REMOVED", f"the ticket was removed while its heartbeat sat between the nonce check and the rename, and the heartbeat then brought it back: {r}")
        self.assertEqual((r["beat"], r["exists"]), ("OK", True))

    SEAM_A3 = """
$A = New-VenueTicket -Venue bachelor -RunDir 'run-A' -ClaimDir $d -OwnerPid @@S@@
Start-Sleep -Milliseconds 30
$B = New-VenueTicket -Venue bachelor -RunDir 'run-B' -ClaimDir $d
$job = Start-Job -ArgumentList $A.path, '@@MARK@@' -ScriptBlock { param($p, $mark) $fs = [IO.File]::Open($p, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None); [IO.File]::WriteAllText($mark, '1'); Start-Sleep -Milliseconds 800; $fs.Dispose() }
$t0 = [DateTime]::UtcNow
while (-not (Test-Path -LiteralPath '@@MARK@@') -and ([DateTime]::UtcNow - $t0).TotalSeconds -lt 60) { Start-Sleep -Milliseconds 20 }
$r = Request-VenueClaim -Venue bachelor -RunDir 'run-B' -ClaimDir $d -Ticket $B
Receive-Job $job -Wait -AutoRemoveJob | Out-Null
[pscustomobject]@{ state = [string]$r.state; granted = [bool]$r.granted } | ConvertTo-Json -Compress
"""

    def test_an_older_ready_ticket_that_is_unreadable_for_a_moment_is_read_again_before_a_younger_waiter_may_pass_it(self) -> None:
        # r7 adversary A3: an older READY ticket whose file was busy for a moment (no parsed phase) was skipped by the head filter and the younger waiter took the free claim. A busy older ticket is
        # read again for a short bound first; one still unreadable after it stays "phase unknown, not ready" (test_a_ticket_being_rewritten_whose_phase_cannot_be_read pins that: a ticket held for good must not block the line).
        mark = self.case / "seam-a3-mark.txt"
        r = self.ps(self.SEAM_A3.replace("@@S@@", str(self.sleeper().pid)).replace("@@MARK@@", str(mark)))   # A belongs to another process: a ticket of this process never blocks its owner (r8 rule 1)
        self.assertEqual((r["state"], r["granted"]), ("QUEUED", False), f"the younger waiter passed an older ready ticket that was only briefly unreadable: {r}")

    def test_an_already_ready_waiter_whose_heartbeat_rename_is_refused_still_tries_the_claim(self) -> None:
        # r7 adversary A4: the G7 guard queued a waiter whose heartbeat could not land even when its ticket ALREADY read waiting (a read-only observer without FileShare.Delete), and the younger
        # waiter queued behind it: both waited on a free claim. Only a flip that left the ticket NOT ready is refused.
        r = self.run_flip_with_a_foreign_handle(None, phase="waiting")
        self.assertEqual((r["state"], r["granted"]), ("TAKEN", True), f"a ready head was kept from a free claim by a refused heartbeat: {r}")

    BUDGET_BUSY = """
$A = New-VenueTicket -Venue bachelor -RunDir 'run-A' -ClaimDir $d -StaleAfterSec 600
[IO.File]::WriteAllText('@@READY@@', $A.path)
$t0 = [DateTime]::UtcNow
while (-not (Test-Path -LiteralPath '@@HELD@@') -and ([DateTime]::UtcNow - $t0).TotalSeconds -lt 30) { Start-Sleep -Milliseconds 50 }
$sw = [Diagnostics.Stopwatch]::StartNew()
$r = Update-VenueTicket -Ticket $A -Phase 'waiting' -BudgetSec 0.3
[pscustomobject]@{ result = [string]$r; elapsed = $sw.Elapsed.TotalSeconds } | ConvertTo-Json -Compress
"""

    def test_a_ticket_update_with_a_budget_starts_no_attempt_and_sleeps_no_back_off_past_it(self) -> None:
        # r7 adversary A6: Request-VenueClaim admitted a whole five-attempt update while less than one attempt's budget remained, so a flip could land after its 2 s. Update-VenueTicket
        # now takes -BudgetSec: with the rename refused for good, it answers BUSY inside the budget (plus one attempt), never the full five attempts and their back-offs.
        r = self.run_flip_with_a_foreign_handle(None, template=self.BUDGET_BUSY)
        self.assertEqual(r["result"], "BUSY")
        self.assertLess(r["elapsed"], 0.5, f"an update given 0.3 s ran on past it: {r}")

    def test_the_phase_flip_retry_of_a_request_stays_inside_its_two_second_bound(self) -> None:
        # r7 adversary A6 (the request level): a flip that cannot land is still QUEUED, within about 2 s (the unbounded reads after it are not part of the bound).
        r = self.run_flip_with_a_foreign_handle(None)
        self.assertEqual(r["state"], "QUEUED")
        self.assertLess(r["elapsed"], 2.6, f"the retry ran well past its 2 s bound: {r}")

    SEAM_A9 = """
$A = New-VenueTicket -Venue bachelor -RunDir 'run-A' -ClaimDir $d
$files = @(Get-ChildItem -LiteralPath (Split-Path -Parent $A.path) -File | ForEach-Object { $_.Name })
$rows = @(Get-VenueTickets -Venue bachelor -ClaimDir $d)
[pscustomobject]@{ files = $files.Count; rows = $rows.Count; returned = $A.name; names = ($rows | ForEach-Object { $_.name }) -join ',' } | ConvertTo-Json -Compress
"""

    def test_a_ticket_whose_write_failed_after_its_create_leaves_no_second_ticket_of_the_same_owner(self) -> None:
        # r7 adversary A9: New-VenueTicket created the file, its write or flush then threw, and the next tick's ticket was returned while the first stayed in the line: the batch queued behind itself.
        mod = self.seam_module("try { $fs.Write($bytes, 0, $bytes.Length); $fs.Flush($true) } finally { $fs.Dispose() }\n        } catch {",
                               "try { $fs.Write($bytes, 0, $bytes.Length); if (-not $script:SeamFlushed) { $script:SeamFlushed = $true; throw 'flush boom' }; $fs.Flush($true) } finally { $fs.Dispose() }\n        } catch {")
        r = self.ps(self.SEAM_A9, module=mod)
        self.assertEqual((r["files"], r["rows"]), (1, 1), f"the failed first ticket was left in the line: {r}")

    UNICODE_DIGITS = """
$A = New-VenueTicket -Venue bachelor -RunDir 'run-A' -ClaimDir $d
$dir = Split-Path -Parent $A.path
$arabic = [string]::new([char]0x0661, 19)
$tmp = Join-Path $dir ($arabic + '-p123-456.ticket.json.' + ('a' * 32) + '.tmp')
$odd = Join-Path $dir ($arabic + '-p123-456.ticket.json')
foreach ($p in @($tmp, $odd)) { [IO.File]::WriteAllText($p, 'x') }
(Get-Item -LiteralPath $tmp).LastWriteTimeUtc = [DateTime]::UtcNow.AddMinutes(-10)
(Get-Item -LiteralPath $odd).LastWriteTimeUtc = [DateTime]::UtcNow.AddMinutes(-10)
$threw = ''; $r = $null
try { $r = Request-VenueClaim -Venue bachelor -RunDir 'run-A' -ClaimDir $d -Ticket $A } catch { $threw = $_.Exception.Message -replace '\\s+', ' ' }
[pscustomobject]@{ threw = $threw; state = [string]$r.state; tmpKept = (Test-Path -LiteralPath $tmp) } | ConvertTo-Json -Compress
"""

    def test_ticket_and_temp_names_match_ascii_digits_only(self) -> None:
        # r7 adversary A10 (advisory): \\d matches Unicode digits in .NET: a file named with Arabic-Indic digits passed the temp sweeper (and was deleted) and made the ticket listing throw on [int64].
        r = self.ps(self.UNICODE_DIGITS)
        self.assertEqual(r["threw"], "", "a foreign file named with non-ASCII digits broke the ticket listing")
        self.assertTrue(r["tmpKept"], "the sweeper deleted a file whose name is not the writer's ASCII shape")

    UNKNOWN_PHASE = """
$A = New-VenueTicket -Venue bachelor -RunDir 'run-A' -ClaimDir $d
Start-Sleep -Milliseconds 30
$B = New-VenueTicket -Venue bachelor -RunDir 'run-B' -ClaimDir $d
$out = [ordered]@{}
foreach ($shape in @('null', 'missing', 'trailing-space', 'other', 'array', 'soft-hyphen')) {
  $text = [IO.File]::ReadAllText($A.path)
  $new = switch ($shape) { 'null' { $text -replace '"phase":"waiting"', '"phase":null' } 'missing' { $text -replace ',"phase":"waiting"', '' } 'trailing-space' { $text -replace '"phase":"waiting"', '"phase":"cooldown "' } 'array' { $text -replace '"phase":"waiting"', '"phase":["waiting"]' } 'soft-hyphen' { $text -replace '"phase":"waiting"', '"phase":"wait­ing"' } default { $text -replace '"phase":"waiting"', '"phase":"later"' } }
  [IO.File]::WriteAllText($A.path, $new)
  $r = Request-VenueClaim -Venue bachelor -RunDir 'run-B' -ClaimDir $d -Ticket $B
  $out[$shape] = [string]$r.state
  if ($r.granted) { [void](Release-VenueClaim -Venue bachelor -Nonce $r.claim.nonce -ClaimDir $d) }
  [IO.File]::WriteAllText($A.path, $text)
}
[pscustomobject]$out | ConvertTo-Json -Compress
"""

    def test_a_ticket_whose_stored_phase_is_not_a_known_ready_phase_does_not_block_a_ready_waiter(self) -> None:
        # r7 adversary A13 (advisory): the readiness filter excluded only the string cooldown, so a null, missing or unknown stored phase counted as ready and blocked the line.
        r = self.ps(self.UNKNOWN_PHASE)
        # r7 A10: also a phase that is an array of the ready one, or the ready one with a soft hyphen inside (culture-aware comparison called it equal)
        self.assertEqual(r, {"null": "TAKEN", "missing": "TAKEN", "trailing-space": "TAKEN", "other": "TAKEN", "array": "TAKEN", "soft-hyphen": "TAKEN"}, "an unknown phase counted as ready")

    def test_the_cooldown_wait_is_measured_on_a_monotonic_clock_and_no_claim_request_starts_past_the_deadline(self) -> None:
        # r7 adversary A7: the wait and its heartbeat cadence differenced the wall clock ([DateTime]::UtcNow - $waitEndUtc), so a runner clock step of +1600 s ended a 1800 s cooldown early (the claim
        # taken with the agent's cooldown still running) and a -600 s step lengthened it. A5: ticket work that carried the clock to -DeadlineUtc must not be followed by a claim request.
        run = LEASE_RUNNER.read_text(encoding="utf-8")
        wait = run[run.index("$beatStaleSec = "):run.index("# (1) the claim, once per batch")]
        self.assertIn("$waitClock = [Diagnostics.Stopwatch]::StartNew()", wait, "the wait is not measured on a monotonic clock")
        self.assertNotIn("$waitEndUtc", wait, "the wait end is still a wall-clock instant")
        self.assertNotRegex(wait, r"\[DateTime\]::UtcNow\s*-\s*\$|\$(?!deadline)[A-Za-z]+\s*-\s*\[DateTime\]::UtcNow", "a wall-clock difference measures the wait (only the absolute -DeadlineUtc is compared with it)")
        self.assertRegex(wait, r"if \(\[DateTime\]::UtcNow -ge \$deadline\) \{\s*\$b\.endReason = 'DEADLINE'", "no deadline check after the wait and its ticket work")
        mod = MODULE.read_text(encoding="utf-8")
        flip = mod[mod.index("$flipClock = "):mod.index("if ($flip -ceq 'LOST')")]
        self.assertNotIn("[DateTime]::UtcNow", flip, "the 2 s flip retry reads the wall clock")

    def test_a_ticket_call_that_was_given_up_on_is_part_of_the_cooldown_wait_not_added_to_it(self) -> None:
        # r7 adversary A5: creation of the cooldown ticket that gave no answer in its 5 s was followed by the whole wait again (Start-Sleep -Seconds $bw.waitSec), so the first claim request
        # came waitSec + 5 s after BATCH_WAIT. Here creation of the cooldown ticket takes 8 s (a seam in a module copy; the call is given up on at 5 s): the claim is asked for about waitSec after BATCH_WAIT.
        import re
        import shutil
        text = MODULE.read_text(encoding="utf-8")
        anchor = "    New-Item -ItemType Directory -Force -Path $dir | Out-Null\n    $ownerStart = Get-VenueProcessStartUtc $OwnerPid"
        self.assertEqual(text.count(anchor), 1, "slow-creation anchor moved")
        runner = copy_with_siblings(LEASE_RUNNER, Path(tempfile.mkdtemp(prefix="slow-", dir=self.case)))
        (runner.parent / MODULE.name).write_text(text.replace(anchor, "    if ($Phase -ceq 'cooldown' -and $env:SEAM_SLOW_COOLDOWN_TICKET) { Start-Sleep -Seconds 8 }\n" + anchor), encoding="utf-8")
        (runner.parent.parent / "bachelor").mkdir(exist_ok=True)
        shutil.copy2(DV.parent / "bachelor" / "ultra-magnus-agent.ps1", runner.parent.parent / "bachelor" / "ultra-magnus-agent.ps1")
        spec_b = self.tmp / "fx-b.json"
        spec_b.write_text(json.dumps({"schema": "mlv-app/dual-venue-leg/v1", "card": "TEST-CARD", "legId": "fx-b", "backends": ["cpu", "cuda"], "scaleFactor": 1}), encoding="utf-8")
        p = self.start_batch("G", claim_poll=1, specs=f"{self.spec},{spec_b}", extra=("-TicketBeatSec", "1"), cooldown="22", runner=runner, FAKE_LEASE_REMAIN="70,2700", FAKE_LEG_REMAIN="50,2600", SEAM_SLOW_COOLDOWN_TICKET="1")
        try:
            self.wait_for(lambda: "CLAIM_TAKEN batch=2" in self.batch_log(p), "batch 2's claim", timeout=170)
        except AssertionError as exc:
            self.fail(f"{exc}\n{self.batch_log(p)}")
        log = self.batch_log(p)
        self.assertIn("TICKET_UNAVAILABLE batch=2", log, f"the slow-creation seam did not bite\n{log}")
        def at(pattern: str) -> datetime:
            m = re.search(pattern, log)
            self.assertIsNotNone(m, f"{pattern}\n{log}")
            return datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ")
        wait_sec = float(re.search(r"BATCH_WAIT batch=2 reason=\S+ waitSec=([\d.]+)", log).group(1))
        waited = (at(r"(?m)^(\S+) CLAIM_TAKEN batch=2 ") - at(r"(?m)^(\S+) BATCH_WAIT batch=2 ")).total_seconds()
        self.assertLessEqual(waited, wait_sec + 3, f"the claim was asked for {waited} s after BATCH_WAIT, the wait being {wait_sec} s: the ticket call that gave no answer was slept on top of it\n{log}")
        p.wait(timeout=180)

    def test_the_claim_keeper_is_stopped_at_once_when_a_refusal_was_recorded_before_it_started(self) -> None:
        # r7 adversary A8: Enable-WriteSuppression stops a keeper only when $script:keeperLive; the TICKET_TAKEN log (before the keeper) and the TICKET_ENDED log (before keeperLive was set) can find an
        # output alias, so the keeper then started (or ran) with the refusal recorded and rewrote the claim in place until the finally. keeperLive is now set before the ticket log, and a refusal already on stops the keeper at once.
        # r7 A11: and with a refusal already recorded no keeper is started at all (a started one could renew the claim once before any check)
        run = LEASE_RUNNER.read_text(encoding="utf-8")
        start = run.index("if ($script:RunOutputAliased) {\n        $claimKeeper = $null")
        region = run[start:start + 1800]
        self.assertLess(region.index("$claimKeeper = Start-VenueClaimKeeper"), region.index("$script:keeperLive = $true"))
        self.assertLess(region.index("$script:keeperLive = $true"), region.index("Close-BatchTicket 'claim-granted'"), "keeperLive is set after the ticket log that can find the alias")
        self.assertLess(region.index("$script:RunOutputAliased"), region.index("$claimKeeper = Start-VenueClaimKeeper"), "the keeper is started before the recorded refusal is asked")
        self.assertEqual(run.count("Start-VenueClaimKeeper -Venue"), 1, "a second keeper start site")

    # ---- r7, second iteration: the adversary pass on 7786b1d3 (A1 .. A15, A17) ----

    def seam_text(self, *pairs: tuple[str, str]) -> Path:
        text = MODULE.read_text(encoding="utf-8")
        for anchor, replacement in pairs:
            self.assertEqual(text.count(anchor), 1, f"seam anchor moved: {anchor!r}")
            text = text.replace(anchor, replacement)
        return copy_with_siblings(MODULE, Path(tempfile.mkdtemp(prefix="seam-", dir=self.case)), text)

    SEAM_A2B = """
$A = New-VenueTicket -Venue bachelor -RunDir 'run-A' -ClaimDir $d -Phase cooldown -StaleAfterSec 600 -OwnerPid @@S@@
Start-Sleep -Milliseconds 30
$B = New-VenueTicket -Venue bachelor -RunDir 'run-B' -ClaimDir $d
$job = Start-Job -ArgumentList $B, $d, '@@SEAMMOD@@', '@@MARK@@' -ScriptBlock { param($B, $d, $m, $mark) $env:SEAM_MARK = $mark; Import-Module $m -Force -DisableNameChecking; Request-VenueClaim -Venue bachelor -RunDir 'run-B' -ClaimDir $d -Ticket $B }
$t0 = [DateTime]::UtcNow
while (-not (Test-Path -LiteralPath '@@MARK@@') -and ([DateTime]::UtcNow - $t0).TotalSeconds -lt 60) { Start-Sleep -Milliseconds 50 }
$flip = Update-VenueTicket -Ticket $A -Phase 'waiting'
$fs = [IO.File]::Open($A.path, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
Start-Sleep -Milliseconds 3000
$fs.Dispose()
$r = Receive-Job $job -Wait -AutoRemoveJob
[pscustomobject]@{ flip = $flip; state = [string]$r.state; granted = [bool]$r.granted; claimExists = (Test-Path -LiteralPath (Join-Path $d 'bachelor.claim.json')) } | ConvertTo-Json -Compress
"""

    def test_an_older_ticket_that_became_ready_is_found_even_when_its_file_is_busy_at_the_check_after_a_grant(self) -> None:
        # r7 pass 1 A2 (blocker): the census made after a grant had no re-read of a briefly unreadable older ticket, so it skipped A (no parsed phase) and kept the grant that A is owed.
        mark = self.case / "seam-a2b-mark.txt"
        mod = self.seam_module("    $r = Invoke-VenueClaimAttempt @attemptArgs\n", "    [IO.File]::WriteAllText($env:SEAM_MARK, '1'); Start-Sleep -Milliseconds 2500\n    $r = Invoke-VenueClaimAttempt @attemptArgs\n")
        r = self.ps(self.SEAM_A2B.replace("@@S@@", str(self.sleeper().pid)).replace("@@SEAMMOD@@", str(mod)).replace("@@MARK@@", str(mark)))   # A belongs to another process (r8 rule 1)
        self.assertEqual((r["state"], r["granted"], r["claimExists"]), ("QUEUED", False, False), f"the grant was kept although the older ticket was ready (its file only busy for a moment): {r}")

    SEAM_A3B = """
$A = New-VenueTicket -Venue bachelor -RunDir 'run-A' -ClaimDir $d -Phase cooldown -StaleAfterSec 600
Start-Sleep -Milliseconds 30
$B = New-VenueTicket -Venue bachelor -RunDir 'run-B' -ClaimDir $d
$job = Start-Job -ArgumentList $B, $d, '@@SEAMMOD@@', '@@MARK@@' -ScriptBlock { param($B, $d, $m, $mark) $env:SEAM_MARK = $mark; Import-Module $m -Force -DisableNameChecking; Request-VenueClaim -Venue bachelor -RunDir 'run-B' -ClaimDir $d -Ticket $B }
$t0 = [DateTime]::UtcNow
while (-not (Test-Path -LiteralPath '@@MARK@@') -and ([DateTime]::UtcNow - $t0).TotalSeconds -lt 60) { Start-Sleep -Milliseconds 50 }
$flip = Update-VenueTicket -Ticket $A -Phase 'waiting'
$cp = Join-Path $d 'bachelor.claim.json'
$hold = $null
while ($null -eq $hold -and ([DateTime]::UtcNow - $t0).TotalSeconds -lt 60) { try { $hold = [IO.File]::Open($cp, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read) } catch { Start-Sleep -Milliseconds 2 } }
Start-Sleep -Milliseconds 9000
$hold.Dispose()
$r = Receive-Job $job -Wait -AutoRemoveJob
$claim = $null; if (Test-Path -LiteralPath $cp) { $claim = (Get-Content -LiteralPath $cp -Raw | ConvertFrom-Json).nonce }
$pending = [string]$r.ticket.pendingReleaseNonce
# A13: the next poll (made with the ticket the first answer returned) lets go of the stranded claim before anything else
$r2 = Request-VenueClaim -Venue bachelor -RunDir 'run-B' -ClaimDir $d -Ticket $r.ticket
$after = $null; if (Test-Path -LiteralPath $cp) { $after = (Get-Content -LiteralPath $cp -Raw | ConvertFrom-Json).nonce }
[pscustomobject]@{ flip = $flip; state = [string]$r.state; granted = [bool]$r.granted; claimExists = [bool]$claim; ours = [bool]($claim -and $pending -and $claim -ceq $pending); pending = [bool]$pending
  state2 = [string]$r2.state; granted2 = [bool]$r2.granted; claimAfterPoll = [bool]$after; pendingAfterPoll = [string]$r2.ticket.pendingReleaseNonce } | ConvertTo-Json -Compress
"""

    def test_a_grant_that_cannot_be_given_back_is_queued_with_its_nonce_on_the_ticket_and_the_next_poll_releases_it(self) -> None:
        # r7 pass 1 A3 (blocker): the give-back's answer was discarded, so a reader of the claim file that refused the release left the claim naming B while B was told QUEUED (and nobody owned its nonce).
        mark = self.case / "seam-a3b-mark.txt"
        # the seam keeps the claim standing for 1.5 s after its creation (so the observer reliably opens it) and the observer holds it for 9 s, longer than every give-back attempt together
        mod = self.seam_module("    $r = Invoke-VenueClaimAttempt @attemptArgs\n", "    [IO.File]::WriteAllText($env:SEAM_MARK, '1'); Start-Sleep -Milliseconds 2500\n    $r = Invoke-VenueClaimAttempt @attemptArgs\n    if ($r.granted) { Start-Sleep -Milliseconds 1500 }\n")
        r = self.ps(self.SEAM_A3B.replace("@@SEAMMOD@@", str(mod)).replace("@@MARK@@", str(mark)))
        # TAIL-1 A13 (= #878 sol r10 B1), replaces r7 pass 1 A3's contract (TAKEN after five BUSY give-backs): the caller is not handed a claim it was owed to an older ticket. The answer is QUEUED, the claim that could
        # not be given back is still standing and named by the ticket (pendingReleaseNonce, so it is not leaked), and the NEXT poll lets go of it before anything else.
        self.assertEqual((r["state"], r["granted"], r["claimExists"], r["ours"], r["pending"]), ("QUEUED", False, True, True, True), f"a claim that could not be given back must be QUEUED with its nonce kept on the ticket: {r}")
        self.assertEqual((r["state2"], r["granted2"], r["claimAfterPoll"], r["pendingAfterPoll"]), ("QUEUED", False, False, ""), f"the next poll did not release the stranded claim: {r}")

    SEAM_T1A7 = """
$A = New-VenueTicket -Venue bachelor -RunDir 'run-A' -ClaimDir $d -Phase cooldown -StaleAfterSec 600
Start-Sleep -Milliseconds 30
$B = New-VenueTicket -Venue bachelor -RunDir 'run-B' -ClaimDir $d
$job = Start-Job -ArgumentList $B, $d, '@@SEAMMOD@@', '@@MARK@@' -ScriptBlock { param($B, $d, $m, $mark) $env:SEAM_MARK = $mark; Import-Module $m -Force -DisableNameChecking; Request-VenueClaim -Venue bachelor -RunDir 'run-B' -ClaimDir $d -Ticket $B }
$t0 = [DateTime]::UtcNow
while (-not (Test-Path -LiteralPath '@@MARK@@') -and ([DateTime]::UtcNow - $t0).TotalSeconds -lt 60) { Start-Sleep -Milliseconds 50 }
$flip = Update-VenueTicket -Ticket $A -Phase 'waiting'
$r = Receive-Job $job -Wait -AutoRemoveJob
[pscustomobject]@{ flip = $flip; state = [string]$r.state; granted = [bool]$r.granted; position = [int]$r.position; queueCount = @($r.queue).Count; queueNames = @($r.queue | ForEach-Object { $_.name }) } | ConvertTo-Json -Compress
"""

    def test_tail1_a7_a_give_back_after_the_callers_own_ticket_was_reaped_answers_position_zero_with_the_census_it_made(self) -> None:
        # pass 11 A7 (introduced by r8 rule 7): B takes the claim as the head (A is still in cooldown), A flips to waiting while B holds it, and B's own ticket is reaped (the seam removes the file right after the grant,
        # as A's stale sweep would for a heartbeat B did not make in time). The second census is [A]; B gives the claim back. The queue returned is that census, so B's place in it is 0 (not in it), never the 2 B held in the
        # first census: the batch and the chain print the place as "<position>/<queue length>", and 2/1 is a place past the end of its own line.
        mark = self.case / "seam-a7-mark.txt"
        mod = self.seam_module("    $r = Invoke-VenueClaimAttempt @attemptArgs\n", "    [IO.File]::WriteAllText($env:SEAM_MARK, '1'); Start-Sleep -Milliseconds 2500\n    $r = Invoke-VenueClaimAttempt @attemptArgs\n    if ($r.granted) { Remove-Item -LiteralPath $t.path -Force }\n")
        r = self.ps(self.SEAM_T1A7.replace("@@SEAMMOD@@", str(mod)).replace("@@MARK@@", str(mark)))
        self.assertEqual((r["state"], r["granted"], r["queueCount"]), ("QUEUED", False, 1), f"B must give the claim back and be queued behind A (a census of one row): {r}")
        self.assertEqual(r["position"], 0, f"the place returned is not in the census returned (a place past the end of the line): {r}")
        self.assertFalse(self.claim_path.exists(), "the claim B gave back is still standing")

    LOCK_JOB = """
Set-Location -LiteralPath '@@CASE@@'
$job = Start-Job -ArgumentList '@@MOD@@', '@@ABS@@', '@@MARK@@' -ScriptBlock { param($m, $abs, $mark) Import-Module $m -Force -DisableNameChecking; $got = & (Get-Module VenueChain) { param($p) $l = Enter-VenueTicketLock -TicketPath $p -TimeoutMs 400; $ok = ($null -ne $l); Exit-VenueTicketLock $l; $ok } $abs; [IO.File]::WriteAllText($mark, [string]$got) }
$held = & (Get-Module VenueChain) { param($p) Enter-VenueTicketLock -TicketPath $p -TimeoutMs 500 } '.\\claims\\bachelor.tickets\\x.ticket.json'
Wait-Job $job | Out-Null; Receive-Job $job | Out-Null; Remove-Job $job
& (Get-Module VenueChain) { param($l) Exit-VenueTicketLock $l } $held
[pscustomobject]@{ held = ($null -ne $held); otherGotIt = ([IO.File]::ReadAllText('@@MARK@@') -eq 'True') } | ConvertTo-Json -Compress
"""

    def test_the_ticket_lock_is_the_same_for_a_relative_and_an_absolute_spelling_of_the_claim_folder(self) -> None:
        # r7 pass 1 A4 (blocker): the mutex was keyed on the spelling of the directory, so a heartbeat given a relative ClaimDir and a reaper that spells it absolutely (FileInfo.FullName) did not exclude each other.
        mark = self.case / "lock-mark.txt"
        absolute = self.claims / "bachelor.tickets" / "x.ticket.json"
        (self.claims / "bachelor.tickets").mkdir(parents=True, exist_ok=True)   # the lock file lives in the ticket folder, which exists once a ticket does
        script = self.LOCK_JOB.replace("@@CASE@@", str(self.case)).replace("@@MOD@@", str(MODULE)).replace("@@ABS@@", str(absolute)).replace("@@MARK@@", str(mark))
        r = self.ps(script)
        self.assertTrue(r["held"], "the first spelling did not get the lock")
        self.assertFalse(r["otherGotIt"], "the other spelling of the same folder got the lock too: the two do not exclude each other")

    SEAM_A6B = """
$A = New-VenueTicket -Venue bachelor -RunDir 'run-A' -ClaimDir $d -Phase cooldown -StaleAfterSec 600
$r = Update-VenueTicket -Ticket $A -Phase 'waiting' -BudgetSec 0.3
$phase = (Get-Content -LiteralPath $A.path -Raw | ConvertFrom-Json).phase
[pscustomobject]@{ result = [string]$r; phase = $phase } | ConvertTo-Json -Compress
"""

    def test_an_update_whose_budget_ended_during_the_temp_write_publishes_nothing(self) -> None:
        # r7 pass 1 A6: the budget was sampled before the lock and not again, so a slow temp write ended with a publish after the budget (a flip that landed at 2.5 s of a 2 s request).
        mod = self.seam_text(("try { $fs.Write($Bytes, 0, $Bytes.Length); $fs.Flush($true) } finally { $fs.Dispose() }", "Start-Sleep -Milliseconds 600; try { $fs.Write($Bytes, 0, $Bytes.Length); $fs.Flush($true) } finally { $fs.Dispose() }"))
        r = self.ps(self.SEAM_A6B, module=mod)
        self.assertEqual((r["result"], r["phase"]), ("BUSY", "cooldown"), f"the update published after its budget: {r}")
        self.assertIn("[Math]::Floor", MODULE.read_text(encoding="utf-8")[MODULE.read_text(encoding="utf-8").index("function Get-VenueBackoffMs"):][:700], "the back-off is cast ([int]0.6 is 1), not floored")

    ADOPT = """
$A = New-VenueTicket -Venue bachelor -RunDir 'run-A' -Purpose 'P batch 1' -ClaimDir $d -Phase cooldown -StaleAfterSec 600
Start-Sleep -Milliseconds 30
$B = New-VenueTicket -Venue bachelor -RunDir 'run-B' -ClaimDir $d
$r = Request-VenueClaim -Venue bachelor -RunDir 'run-A' -Purpose 'P batch 1' -ClaimDir $d -UseTicket
$files = @(Get-ChildItem -LiteralPath (Split-Path -Parent $A.path) -Filter '*.ticket.json' -File | ForEach-Object { $_.Name }).Count
$phase = (Get-Content -LiteralPath $A.path -Raw | ConvertFrom-Json).phase
[pscustomobject]@{ adopted = ($r.ticket.name -ceq $A.name); state = [string]$r.state; files = $files; phase = $phase } | ConvertTo-Json -Compress
"""

    def test_a_ticket_this_owner_already_made_for_this_run_is_taken_over_not_queued_behind_by_a_second_one(self) -> None:
        # r7 pass 1 A7: the batch gave up on the creating call (5 s) after the ticket file was written, kept no reference, and its first claim request made a SECOND, later ticket: it queued behind a younger waiter
        # and behind itself. A request with no ticket now takes over the owner's own live ticket for the same run directory and purpose.
        r = self.ps(self.ADOPT)
        self.assertTrue(r["adopted"], f"a second ticket was made beside the owner's own: {r}")
        self.assertEqual((r["files"], r["phase"], r["state"]), (2, "waiting", "TAKEN"), r)

    SEAM_A9B = """
$job = Start-Job -ArgumentList '@@SEAMMOD@@', $d, '@@MARK@@' -ScriptBlock { param($m, $d, $mark) $env:SEAM_MARK = $mark; Import-Module $m -Force -DisableNameChecking; $A = New-VenueTicket -Venue bachelor -RunDir 'run-A' -ClaimDir $d; $A.name }
$t0 = [DateTime]::UtcNow
while (-not (Test-Path -LiteralPath '@@MARK@@') -and ([DateTime]::UtcNow - $t0).TotalSeconds -lt 60) { Start-Sleep -Milliseconds 10 }
$ticketFile = @(Get-ChildItem -LiteralPath (Join-Path $d 'bachelor.tickets') -Filter '*.ticket.json' -File)[0].FullName
$hold = [IO.File]::Open($ticketFile, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
Start-Sleep -Milliseconds 700
$hold.Dispose()
$name = Receive-Job $job -Wait -AutoRemoveJob
$files = @(Get-ChildItem -LiteralPath (Join-Path $d 'bachelor.tickets') -Filter '*.ticket.json' -File | ForEach-Object { $_.Name })
[pscustomobject]@{ files = $files.Count; returned = [string]$name; returnedExists = ($files -contains [string]$name) } | ConvertTo-Json -Compress
"""

    def test_a_ticket_whose_write_failed_is_taken_back_even_when_a_reader_holds_it_for_a_moment(self) -> None:
        # r7 pass 1 A9: the rollback's delete was tried once and its failure swallowed, so a reader holding the half-made file left it standing beside the next tick's ticket (a batch queued behind itself).
        mod = self.seam_text(("try { $fs.Write($bytes, 0, $bytes.Length); $fs.Flush($true) } finally { $fs.Dispose() }\n        } catch {",
                              "try { $fs.Write($bytes, 0, $bytes.Length); if (-not $script:SeamFlushed) { $script:SeamFlushed = $true; throw 'flush boom' }; $fs.Flush($true) } finally { $fs.Dispose() }\n        } catch { if ($made -and $env:SEAM_MARK -and -not (Test-Path -LiteralPath $env:SEAM_MARK)) { [IO.File]::WriteAllText($env:SEAM_MARK, '1'); Start-Sleep -Milliseconds 300 }"))
        r = self.ps(self.SEAM_A9B.replace("@@SEAMMOD@@", str(mod)).replace("@@MARK@@", str(self.case / "seam-a9b-mark.txt")))
        self.assertEqual(r["files"], 1, f"the failed first ticket stayed beside the next one: {r}")
        self.assertTrue(r["returnedExists"], r)

    def test_a_ticket_dated_far_in_the_past_does_not_break_the_listing_or_cost_a_grant(self) -> None:
        # r7 pass 1 A12 (and A17): the age of a ticket dated 1900 overflowed [int], and a file named with a 19-digit tick above Int64.MaxValue overflowed [int64]: either threw out of the census, after a claim was already granted.
        r = self.ps("""
$A = New-VenueTicket -Venue bachelor -RunDir 'run-A' -ClaimDir $d -NowUtc '1900-01-01T00:00:00Z'
$dir = Split-Path -Parent $A.path
[IO.File]::WriteAllText((Join-Path $dir '9999999999999999999-p1-1.ticket.json'), '')
[IO.File]::WriteAllText((Join-Path $dir '0000000000000000001-p79228162514264337593543950336-1.ticket.json'), '')
$threw = ''; $rows = 0
try { $rows = @(Get-VenueTickets -Venue bachelor -ClaimDir $d).Count } catch { $threw = $_.Exception.Message -replace '\\s+', ' ' }
$B = New-VenueTicket -Venue bachelor -RunDir 'run-B' -ClaimDir $d
$g = Request-VenueClaim -Venue bachelor -RunDir 'run-B' -ClaimDir $d -Ticket $B
[pscustomobject]@{ threw = $threw; rows = $rows; state = [string]$g.state; granted = [bool]$g.granted } | ConvertTo-Json -Compress
""")
        self.assertEqual(r["threw"], "", "the ticket listing threw")
        self.assertEqual((r["state"], r["granted"]), ("TAKEN", True), r)

    SWEEP_LOCK = """
$A = New-VenueTicket -Venue bachelor -RunDir 'run-A' -ClaimDir $d
$dir = Split-Path -Parent $A.path
$tmp = Join-Path $dir ($A.name + '.' + ('c' * 32) + '.tmp')
[IO.File]::WriteAllText($tmp, 'x')
(Get-Item -LiteralPath $tmp).LastWriteTimeUtc = [DateTime]::UtcNow.AddMinutes(-10)
$job = Start-Job -ArgumentList '@@MOD@@', $A.path, '@@MARK@@' -ScriptBlock { param($m, $p, $mark) Import-Module $m -Force -DisableNameChecking; $l = & (Get-Module VenueChain) { param($p) Enter-VenueTicketLock -TicketPath $p -TimeoutMs 1000 } $p; [IO.File]::WriteAllText($mark, '1'); Start-Sleep -Milliseconds 2500; & (Get-Module VenueChain) { param($l) Exit-VenueTicketLock $l } $l }
$t0 = [DateTime]::UtcNow
while (-not (Test-Path -LiteralPath '@@MARK@@') -and ([DateTime]::UtcNow - $t0).TotalSeconds -lt 60) { Start-Sleep -Milliseconds 20 }
& (Get-Module VenueChain) { param($d) Remove-VenueOrphanTicketTemps -Venue bachelor -ClaimDir $d } $d
$keptWhileHeld = Test-Path -LiteralPath $tmp
Wait-Job $job | Out-Null; Remove-Job $job -Force
& (Get-Module VenueChain) { param($d) Remove-VenueOrphanTicketTemps -Venue bachelor -ClaimDir $d } $d
[pscustomobject]@{ keptWhileHeld = $keptWhileHeld; goneAfter = -not (Test-Path -LiteralPath $tmp) } | ConvertTo-Json -Compress
"""

    def test_the_orphan_temp_sweep_leaves_a_temp_alone_while_a_publisher_holds_the_ticket_lock(self) -> None:
        # r7 pass 1 A14: the sweep judged a temp by its age alone, so a live heartbeat paused (or a wall clock stepped) between its temp and its rename lost its temp, and its update ended BUSY.
        r = self.ps(self.SWEEP_LOCK.replace("@@MOD@@", str(MODULE)).replace("@@MARK@@", str(self.case / "sweep-mark.txt")))
        self.assertTrue(r["keptWhileHeld"], "the sweep deleted a temp while a publisher held the ticket lock")
        self.assertTrue(r["goneAfter"], "the sweep no longer reaps a real orphan once the lock is free")

    def test_the_lease_batch_ticket_calls_keep_clear_of_the_deadline_and_a_failed_end_is_tried_again(self) -> None:
        # r7 pass 1 A5: a ticket call (a 5 s worker) is started only with more than 6 s left before -DeadlineUtc (creation, heartbeat and retake). A8: a ticket whose end was BUSY or unanswered is kept and ended again at the next batch.
        run = LEASE_RUNNER.read_text(encoding="utf-8")
        wait = run[run.index("$beatStaleSec = "):run.index("# (1) the claim, once per batch")]
        self.assertEqual(wait.count("($deadline - [DateTime]::UtcNow).TotalSeconds -gt 6.0") + wait.count("($deadline - [DateTime]::UtcNow).TotalSeconds -le 6.0"), 3, "a ticket call in the wait is not guarded by the room before the deadline")
        close = run[run.index("function Close-BatchTicket"):run.index("function Invoke-BatchTicketCall")]
        self.assertIn("-cnotin @('REMOVED', 'ABSENT', 'CHANGED')", close, "the reference is dropped whatever the end's answer was")
        self.assertIn("$script:strandedTickets.Add($t)", close, "a ticket whose end was not confirmed is dropped with the batch's reference")
        self.assertEqual(run.count("Invoke-StrandedTicketRetry '"), 3, "a stranded ticket is not tried again at the top of the batch loop, in the cooldown wait and in the claim poll")

    def test_a_ticket_phase_log_line_that_cannot_be_written_never_stops_the_chain(self) -> None:
        # r7 pass 1 A15: the new TICKET_PHASE_* and note log lines went through L, which throws when the chain log cannot be appended to (the pre-claim probe then never ran).
        chain = (DV / "Invoke-VenueChain.ps1").read_text(encoding="utf-8")
        for line in chain.splitlines():
            if 'L "TICKET_PHASE_$pf' in line or 'L "$($req.note)' in line:
                self.assertIn("try { L ", line, f"an unguarded log line: {line.strip()[:120]}")

    # ---- r7, second fix iteration: the adversary pass on 04d1455e (A1 A2 A3 A5 A7; A4 is DISTINGUISHED in the PR body) ----

    JUNCTION_LOCK = """
$real = Join-Path $d 'bachelor.tickets'
New-Item -ItemType Junction -Path '@@ALIAS@@' -Target $real | Out-Null
$job = Start-Job -ArgumentList '@@MOD@@', '@@VIAALIAS@@', '@@MARK@@' -ScriptBlock { param($m, $p, $mark) Import-Module $m -Force -DisableNameChecking; $got = & (Get-Module VenueChain) { param($p) $l = Enter-VenueTicketLock -TicketPath $p -TimeoutMs 400; $ok = ($null -ne $l); Exit-VenueTicketLock $l; $ok } $p; [IO.File]::WriteAllText($mark, [string]$got) }
$held = & (Get-Module VenueChain) { param($p) Enter-VenueTicketLock -TicketPath $p -TimeoutMs 500 } (Join-Path $real 'x.ticket.json')
Wait-Job $job | Out-Null; Receive-Job $job | Out-Null; Remove-Job $job
& (Get-Module VenueChain) { param($l) Exit-VenueTicketLock $l } $held
[pscustomobject]@{ held = ($null -ne $held); otherGotIt = ([IO.File]::ReadAllText('@@MARK@@') -eq 'True') } | ConvertTo-Json -Compress
"""

    def test_the_ticket_lock_is_the_same_through_a_junction_to_the_ticket_folder(self) -> None:
        # r7 pass 2 A3: the lock was keyed on the directory's spelling, so a ClaimDir that reaches the same folder through a junction did not exclude a heartbeat that used the real path. The lock is now a file
        # opened exclusively in the folder itself: the file system decides what is the same folder.
        (self.claims / "bachelor.tickets").mkdir(parents=True, exist_ok=True)
        alias = self.case / "alias"
        mark = self.case / "junction-mark.txt"
        script = self.JUNCTION_LOCK.replace("@@ALIAS@@", str(alias)).replace("@@VIAALIAS@@", str(alias / "x.ticket.json")).replace("@@MOD@@", str(MODULE)).replace("@@MARK@@", str(mark))
        r = self.ps(script)
        self.assertTrue(r["held"], "the real path did not get the lock")
        self.assertFalse(r["otherGotIt"], "the junction spelling of the same folder got the lock too")

    SEAM_A2C = """
$B = New-VenueTicket -Venue bachelor -RunDir 'run-B' -ClaimDir $d
$job = Start-Job -ArgumentList $B, $d, '@@SEAMMOD@@' -ScriptBlock { param($B, $d, $m) Import-Module $m -Force -DisableNameChecking; Request-VenueClaim -Venue bachelor -RunDir 'run-B' -ClaimDir $d -Ticket $B }
$cp = Join-Path $d 'bachelor.claim.json'
$t0 = [DateTime]::UtcNow
while (-not (Test-Path -LiteralPath $cp) -and ([DateTime]::UtcNow - $t0).TotalSeconds -lt 60) { Start-Sleep -Milliseconds 5 }
$first = (Get-Content -LiteralPath $cp -Raw -ErrorAction SilentlyContinue | ConvertFrom-Json -ErrorAction SilentlyContinue).nonce
Remove-Item -LiteralPath $cp -Force
$other = Request-VenueClaim -Venue bachelor -RunDir 'run-X' -Purpose replacement -ClaimDir $d
$r = Receive-Job $job -Wait -AutoRemoveJob
[pscustomobject]@{ state = [string]$r.state; granted = [bool]$r.granted; otherGranted = [bool]$other.granted; holder = [string]$r.claim.runDir } | ConvertTo-Json -Compress
"""

    def test_a_grant_older_than_two_seconds_is_checked_to_still_name_this_owner_before_it_is_handed_over(self) -> None:
        # r7 pass 2 A2: the post-grant census can take long on a slow claim folder, a short claim can expire and be replaced meanwhile, and the first request still returned granted=true with its obsolete nonce.
        # The census is slowed by a seam (2.5 s inside the post-grant block) while the claim is replaced by another owner's.
        mod = self.seam_module("            $after = @(Get-VenueSettledLive -Venue $Venue -ClaimDir $ClaimDir -NowUtc $now -MyName $t.name -MyRunDir $RunDir)", "            Start-Sleep -Milliseconds 2500\n            $after = @(Get-VenueSettledLive -Venue $Venue -ClaimDir $ClaimDir -NowUtc $now -MyName $t.name -MyRunDir $RunDir)")
        r = self.ps(self.SEAM_A2C.replace("@@SEAMMOD@@", str(mod)))
        self.assertTrue(r["otherGranted"], "the scenario did not replace the claim")
        self.assertEqual((r["state"], r["granted"]), ("QUEUED", False), f"a grant whose claim another owner replaced meanwhile was handed over: {r}")

    def test_the_cooldown_wait_is_anchored_before_the_ticket_work_and_every_ticket_call_is_bounded(self) -> None:
        # adversary pass 2 A1 + A4: the wait end was measured after the ticket was created (so slow ticket I/O pushed the claim request past -DeadlineUtc), and creation and the heartbeat
        # ran inline (a stalled claim folder held the batch; a heartbeat that failed was discarded, so a lost ticket was never retaken).
        run = LEASE_RUNNER.read_text(encoding="utf-8")
        wait = run[run.index("$beatStaleSec = "):run.index("# (1) the claim, once per batch")]
        self.assertLess(wait.index("$waitStartUtc"), wait.index("New-VenueTicket"), "the wait is not anchored before the ticket is created")
        self.assertNotRegex(wait, r"(?<!-)\bNew-VenueTicket -Venue", "the cooldown ticket is created inline, unbounded")
        self.assertNotIn("[void](Update-VenueTicket", wait, "the heartbeat runs inline and its result is discarded")
        self.assertIn("TICKET_RETAKEN", wait, "a lost cooldown ticket is not retaken")
        self.assertIn("function Invoke-BatchTicketCall", run)

    def test_the_ticket_is_removed_only_after_the_claim_keeper_started(self) -> None:
        # adversary pass 2 A3: the removal sat between the claim's lapse check and Start-VenueClaimKeeper, so its time came out of the claim's renewal margin
        run = LEASE_RUNNER.read_text(encoding="utf-8")
        loop_end = run.index("$claimKeeper = Start-VenueClaimKeeper")
        self.assertEqual(run[:loop_end].count("Close-BatchTicket 'claim-granted'"), 0, "the ticket is removed before the keeper starts")
        self.assertIn("Close-BatchTicket 'claim-granted'", run[loop_end:loop_end + 1200])

    def test_the_lease_batch_header_documents_the_ticket(self) -> None:
        run = LEASE_RUNNER.read_text(encoding="utf-8")
        for needle in ("VENUE-FIFO-TICKET-BEFORE-PRECLAIM-1", "-NoTicket", "TICKET_TAKEN", "-UseTicket"):
            self.assertIn(needle, run)
        self.assertTrue(all(b < 128 for b in LEASE_RUNNER.read_bytes()), f"{LEASE_RUNNER.name} is not ASCII")

    # ---- r8: the seven module rules that settle r7's open majors (A1-A7 of the pass on bba152c0) as ONE policy ----
    # rule 1 own tickets never block their owner (A2, A7)   rule 2 last-known phase (A1)   rule 3 BUSY is not loss (A4)   rule 4 renewal margin from the claim's claimedUtc (A5)
    # rule 5 one deadline gate before EACH ticket call (A3)   rule 6 -BudgetSec bound and asked before each lock attempt, read and temp write (A6)   rule 7 the give-back's position from the post-give-back census

    OWN_STRANDED = """
$A = New-VenueTicket -Venue bachelor -RunDir 'run-A' -Purpose 'P batch 1' -ClaimDir $d
Start-Sleep -Milliseconds 30
$B = New-VenueTicket -Venue bachelor -RunDir 'run-A' -Purpose 'P batch 2' -ClaimDir $d
$r = Request-VenueClaim -Venue bachelor -RunDir 'run-A' -Purpose 'P batch 2' -ClaimDir $d -Ticket $B
[pscustomobject]@{ state = [string]$r.state; granted = [bool]$r.granted } | ConvertTo-Json -Compress
"""

    def test_r8_rule1_an_earlier_ready_ticket_of_the_same_process_and_run_never_blocks_its_owner(self) -> None:
        # r8 rule 1 (r7 pass 3 A2, and the r6 A9 class "a batch queued behind itself"): a stranded ticket (its removal was BUSY) of this very process and run directory stays ready in the line; the
        # next batch's ticket queued behind it. The owner's pid and start ticks are in the file NAME, so a ticket of the caller's own process and run is never the head that blocks the caller.
        r = self.ps(self.OWN_STRANDED)
        self.assertEqual((r["state"], r["granted"]), ("TAKEN", True), f"a batch queued behind its own earlier ticket: {r}")

    def test_r8_rule1_a_ticket_of_the_same_process_for_another_run_still_keeps_its_place(self) -> None:
        # the FIFO contract is untouched: only the caller's own RUN is skipped, so two runs of one process (this test, the older chain tests) still queue in arrival order.
        r = self.ps(self.OWN_STRANDED.replace("-RunDir 'run-A' -Purpose 'P batch 2' -ClaimDir $d\n$r", "-RunDir 'run-B' -Purpose 'P batch 2' -ClaimDir $d\n$r").replace("-RunDir 'run-A' -Purpose 'P batch 2' -ClaimDir $d -Ticket", "-RunDir 'run-B' -Purpose 'P batch 2' -ClaimDir $d -Ticket"))
        self.assertEqual((r["state"], r["granted"]), ("QUEUED", False), f"a younger run of the same process passed an older ready ticket: {r}")

    ADOPT_BUSY = """
$A = New-VenueTicket -Venue bachelor -RunDir 'run-A' -Purpose 'P batch 1' -ClaimDir $d -Phase cooldown -StaleAfterSec 600
Start-Sleep -Milliseconds 30
$C = New-VenueTicket -Venue bachelor -RunDir 'run-C' -ClaimDir $d -OwnerPid @@S@@
$hold = [IO.File]::Open($A.path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::None)
$r1 = Request-VenueClaim -Venue bachelor -RunDir 'run-A' -Purpose 'P batch 1' -ClaimDir $d -UseTicket
$files = @(Get-ChildItem -LiteralPath (Split-Path -Parent $A.path) -Filter '*.ticket.json' -File).Count
$hold.Dispose()
$r2 = Request-VenueClaim -Venue bachelor -RunDir 'run-A' -Purpose 'P batch 1' -ClaimDir $d -UseTicket -Ticket $r1.ticket
$end = [string](Remove-VenueTicket -Ticket $r2.ticket)
$left = @(Get-ChildItem -LiteralPath (Split-Path -Parent $A.path) -Filter '*.ticket.json' -File).Count
[pscustomobject]@{ adopted = ($r1.ticket.name -ceq $A.name); s1 = [string]$r1.state; note = [string]$r1.note; files = $files; s2 = [string]$r2.state; g2 = [bool]$r2.granted; end = $end; left = $left } | ConvertTo-Json -Compress
"""

    def test_r8_rule1_an_own_ticket_whose_file_is_busy_is_adopted_by_its_name_never_replaced_by_a_second_one(self) -> None:
        # r8 rule 1 (r7 pass 3 A7): the original creation call is given up on, its file is busy (held exclusively), and the first claim request found no readable own ticket and made a SECOND, later one.
        # The busy file's name carries this process's pid and start ticks: it is adopted by name, kept in the line (QUEUED, the flip not landed), and once readable it is flipped and ended with its own nonce.
        s = self.sleeper()
        r = self.ps(self.ADOPT_BUSY.replace("@@S@@", str(s.pid)))
        self.assertTrue(r["adopted"], f"a second ticket was made beside the owner's own busy one: {r}")
        self.assertEqual((r["s1"], r["files"]), ("QUEUED", 2), r)
        self.assertIn("TICKET_PHASE_BUSY", r["note"], r)
        self.assertEqual((r["s2"], r["g2"], r["end"], r["left"]), ("TAKEN", True, "REMOVED", 1), f"the adopted ticket could not be flipped and ended once its file was readable: {r}")

    LAST_PHASE = """
$A = New-VenueTicket -Venue bachelor -RunDir 'run-A' -ClaimDir $d -OwnerPid @@S@@
Start-Sleep -Milliseconds 30
$B = New-VenueTicket -Venue bachelor -RunDir 'run-B' -ClaimDir $d
$seen = @(Get-VenueTickets -Venue bachelor -ClaimDir $d)
$hold = [IO.File]::Open($A.path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::None)
$r = Request-VenueClaim -Venue bachelor -RunDir 'run-B' -ClaimDir $d -Ticket $B
$hold.Dispose()
[pscustomobject]@{ state = [string]$r.state; granted = [bool]$r.granted; claimExists = (Test-Path -LiteralPath (Join-Path $d 'bachelor.claim.json')) } | ConvertTo-Json -Compress
"""

    def test_r8_rule2_an_older_ticket_last_seen_ready_keeps_that_phase_while_its_file_is_held(self) -> None:
        # r8 rule 2 (r7 pass 3 A1): an older READY ticket whose file is held open for longer than the settle window was skipped as "phase unknown" and a younger waiter took the claim. A ticket
        # this process has read keeps its last-known phase while busy. (One never read stays "phase unknown, not ready": test_a_ticket_being_rewritten_whose_phase_cannot_be_read pins that.)
        s = self.sleeper()
        r = self.ps(self.LAST_PHASE.replace("@@S@@", str(s.pid)))
        self.assertEqual((r["state"], r["granted"], r["claimExists"]), ("QUEUED", False, False), f"a younger waiter passed an older ticket last seen ready: {r}")

    def test_r8_rule2_a_cooldown_ticket_last_seen_not_ready_never_blocks_while_busy(self) -> None:
        s = self.sleeper()
        r = self.ps(self.LAST_PHASE.replace("@@S@@", str(s.pid)).replace("-ClaimDir $d -OwnerPid", "-ClaimDir $d -Phase cooldown -StaleAfterSec 600 -OwnerPid"))
        self.assertEqual((r["state"], r["granted"]), ("TAKEN", True), f"a ticket last seen in cooldown blocked a ready waiter: {r}")

    def test_r8_rule3_a_busy_ownership_read_after_a_failed_give_back_is_not_a_loss(self) -> None:
        # r8 rule 3 (r7 pass 3 A4): five give-backs answered BUSY (a reader holding the claim file with no share at all) and the ownership read that follows was BUSY too: B was told QUEUED while the file
        # still named B. Only a positive different nonce, or FREE / STALE, is a loss.
        mark = self.case / "seam-a4-mark.txt"
        mod = self.seam_module("    $r = Invoke-VenueClaimAttempt @attemptArgs\n", "    [IO.File]::WriteAllText($env:SEAM_MARK, '1'); Start-Sleep -Milliseconds 2500\n    $r = Invoke-VenueClaimAttempt @attemptArgs\n    if ($r.granted) { Start-Sleep -Milliseconds 1500 }\n")
        script = self.SEAM_A3B.replace("@@SEAMMOD@@", str(mod)).replace("@@MARK@@", str(mark)).replace("[IO.FileAccess]::Read, [IO.FileShare]::Read", "[IO.FileAccess]::Read, [IO.FileShare]::None")
        self.assertIn("[IO.FileShare]::None", script.split("$cp = ")[1], "the scenario's observer must hold the claim file with no share")
        r = self.ps(script)
        # TAIL-1 A13: the same five BUSY give-backs now end QUEUED too (see the test above); what r8 rule 3 pins stays: a BUSY ownership read is not the claim's LOSS, so the nonce is still kept on the ticket and the next poll releases it
        self.assertEqual((r["state"], r["granted"], r["ours"], r["pending"]), ("QUEUED", False, True, True), f"a BUSY ownership read was counted as the claim's loss (the nonce was not kept on the ticket): {r}")
        self.assertEqual((r["granted2"], r["claimAfterPoll"]), (False, False), f"the next poll did not release the stranded claim: {r}")

    SEAM_OWNERSHIP_BUSY = """
$B = New-VenueTicket -Venue bachelor -RunDir 'run-B' -ClaimDir $d
$job = Start-Job -ArgumentList $B, $d, '@@SEAMMOD@@', '@@MARK@@' -ScriptBlock { param($B, $d, $m, $mark) $env:SEAM_MARK = $mark; Import-Module $m -Force -DisableNameChecking; Request-VenueClaim -Venue bachelor -RunDir 'run-B' -ClaimDir $d -Ticket $B }
$t0 = [DateTime]::UtcNow
while (-not (Test-Path -LiteralPath '@@MARK@@') -and ([DateTime]::UtcNow - $t0).TotalSeconds -lt 60) { Start-Sleep -Milliseconds 20 }
$cp = Join-Path $d 'bachelor.claim.json'
$hold = $null
while ($null -eq $hold -and ([DateTime]::UtcNow - $t0).TotalSeconds -lt 60) { try { $hold = [IO.File]::Open($cp, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::None) } catch { Start-Sleep -Milliseconds 2 } }
$probe = [string](Get-VenueClaim -Venue bachelor -ClaimDir $d).state
Start-Sleep -Milliseconds 6000
$hold.Dispose()
$r = Receive-Job $job -Wait -AutoRemoveJob
$claim = $null; if (Test-Path -LiteralPath $cp) { $claim = (Get-Content -LiteralPath $cp -Raw | ConvertFrom-Json).nonce }
[pscustomobject]@{ probe = $probe; state = [string]$r.state; granted = [bool]$r.granted; ours = [bool]($claim -and $r.claim -and [string]$r.claim.nonce -ceq $claim) } | ConvertTo-Json -Compress
"""

    def test_tail1_r5_h1_a_busy_ownership_read_of_a_slow_grant_is_not_a_loss(self) -> None:
        # TAIL-1 r5 (sol r4 H1): since A13 the r8 rule 3 fixture above returns through the give-back branch, so it never reaches the post-grant ownership read (VenueChain.psm1: a grant more than 2 s old is checked
        # to still name its owner; only FREE, STALE or another nonce is a loss). Here B is the head (no older ticket), the seam makes the grant's census take 3 s, and a reader holds the claim file with no
        # share through the ownership read, so that read answers BUSY: the grant stands. The same scenario on a module that counts BUSY as a loss must answer QUEUED (the fixture reaches the predicate).
        seam = ("        $grantClock = [Diagnostics.Stopwatch]::StartNew()\n",
                "        $grantClock = [Diagnostics.Stopwatch]::StartNew()\n        if ($env:SEAM_MARK) { [IO.File]::WriteAllText($env:SEAM_MARK, '1'); Start-Sleep -Milliseconds 3000 }\n")
        mutant = ("$own.state -cin @('FREE', 'STALE')", "$own.state -cin @('FREE', 'STALE', 'BUSY')")
        out = {}
        for label, pairs in (("shipped", (seam,)), ("busyIsLoss", (seam, mutant))):
            mod = self.seam_text(*pairs)
            mark = self.case / f"seam-h1-{label}-mark.txt"
            out[label] = self.ps(self.SEAM_OWNERSHIP_BUSY.replace("@@SEAMMOD@@", str(mod)).replace("@@MARK@@", str(mark)))
            self.claim_path.unlink(missing_ok=True)
            for t in (self.claims / "bachelor.tickets").glob("*.ticket.json") if (self.claims / "bachelor.tickets").exists() else ():
                t.unlink(missing_ok=True)
        s, m = out["shipped"], out["busyIsLoss"]
        self.assertEqual(s["probe"], "BUSY", f"the seam did not make the ownership read BUSY: {out}")
        self.assertEqual((s["state"], s["granted"], s["ours"]), ("TAKEN", True, True), f"a BUSY ownership read of a slow grant was counted as the claim's loss: {out}")
        self.assertEqual((m["state"], m["granted"]), ("QUEUED", False), f"the fixture did not reach the post-grant ownership predicate (a module counting BUSY as a loss still granted): {out}")

    def test_r8_rule4_the_claim_age_is_read_from_the_claims_own_claimedutc(self) -> None:
        # r8 rule 4 (r7 pass 3 A5): the lease batch measured the renewal margin from AFTER Request-VenueClaim returned, so a long census inside the request ate the margin unseen.
        r = self.ps("""
$now = [DateTime]::UtcNow
$old = [pscustomobject]@{ claimedUtc = $now.AddSeconds(-63).ToString('o') }
$none = [pscustomobject]@{ claimedUtc = 'not a date' }
[pscustomobject]@{ age = [double](Get-VenueClaimAgeSec -Claim $old -NowUtc $now); bad = ($null -eq (Get-VenueClaimAgeSec -Claim $none -NowUtc $now)); nul = ($null -eq (Get-VenueClaimAgeSec -Claim $null -NowUtc $now)) } | ConvertTo-Json -Compress
""")
        self.assertAlmostEqual(r["age"], 63.0, delta=0.01)
        self.assertTrue(r["bad"] and r["nul"], "an unreadable claimedUtc must answer $null (the caller then keeps its own measure)")
        run = LEASE_RUNNER.read_text(encoding="utf-8")
        at = run.index("$guardSec = ([DateTime]::UtcNow - $grantReadUtc).TotalSeconds")
        self.assertIn("Get-VenueClaimAgeSec", run[at:at + 700], "the renewal margin is not measured from the claim's own claimedUtc")

    def test_r8_rule5_a_ticket_call_is_gated_on_the_deadline_immediately_before_it_starts(self) -> None:
        # r8 rule 5 (r7 pass 3 A3): the room before -DeadlineUtc was asked once per loop turn, so a stranded retry then the heartbeat both started inside one turn. The gate sits in Invoke-BatchTicketCall,
        # the one place every worker-run ticket call (create, heartbeat, end, stranded retry, retake) starts from.
        import re
        run = LEASE_RUNNER.read_text(encoding="utf-8")
        helper = re.search(r"function Invoke-BatchTicketCall.*?\n}\n", run, re.S)
        self.assertIsNotNone(helper)
        script = ("$ErrorActionPreference = 'Stop'\n" f"Import-Module '{MODULE}' -Force -DisableNameChecking\n$here = '{DV}'\n" + helper.group(0) + "\n"
                  "$out = [ordered]@{}\n"
                  "$deadline = [DateTime]::UtcNow.AddSeconds(3)\n$sw = [Diagnostics.Stopwatch]::StartNew()\n$near = Invoke-BatchTicketCall '1' @{}\n$out.nearMs = $sw.Elapsed.TotalMilliseconds; $out.nearAnswered = [bool]$near.answered; $out.nearError = [string]$near.error\n"
                  "$deadline = [DateTime]::UtcNow.AddSeconds(60)\n$far = Invoke-BatchTicketCall '1' @{}\n$out.farAnswered = [bool]$far.answered\n[pscustomobject]$out | ConvertTo-Json -Compress\n")
        f = self.case / "gate.ps1"
        f.write_text(script, encoding="utf-8")
        p = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(f)], capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        r = json.loads([l for l in p.stdout.splitlines() if l.strip()][-1])
        self.assertFalse(r["nearAnswered"], f"a ticket call started with 3 s left before the deadline: {r}")
        self.assertLess(r["nearMs"], 1500, r)
        self.assertIn("NO_ROOM", r["nearError"], r)
        self.assertTrue(r["farAnswered"], f"the gate refused a call with 60 s left: {r}")

    def test_r8_rule5_a_claim_granted_by_the_first_inline_request_at_or_after_the_deadline_is_released_not_used(self) -> None:
        run = LEASE_RUNNER.read_text(encoding="utf-8")
        self.assertEqual(run.count("$lateGrant = "), 1, "the late-grant gate before the guard is missing or doubled")
        at = run.index("$lateGrant = ")
        self.assertLess(at, run.index("$claimGuardSec = [double]($ClaimTtlSec"), "the late-grant gate must sit before the claim guard")
        self.assertIn("[DateTime]::UtcNow -ge $deadline", run[at:at + 200])

    def test_r8_rule6_the_ticket_update_budget_is_bounded_to_zero_through_3600_seconds(self) -> None:
        # r8 rule 6 (r7 pass 3 A8): -BudgetSec accepted NaN, -1 and Infinity, which supplied no finite cutoff (and NaN / -1 bypassed the positive-budget checks).
        r = self.ps("""
$A = New-VenueTicket -Venue bachelor -RunDir 'run-A' -ClaimDir $d
$out = [ordered]@{}
foreach ($v in @('NaN', '-1', 'Infinity', '3601', '0', '2.5', '3600')) {
  $bad = $false
  try { $null = Update-VenueTicket -Ticket $A -Phase 'waiting' -BudgetSec ([double]::Parse($v, [Globalization.CultureInfo]::InvariantCulture)) } catch { $bad = ($_.Exception.Message -match 'BudgetSec|range|Range|validat') }
  $out["v$v"] = $bad
}
[pscustomobject]$out | ConvertTo-Json -Compress
""")
        for refused in ("vNaN", "v-1", "vInfinity", "v3601"):
            self.assertTrue(r[refused], f"-BudgetSec {refused[1:]} was accepted: {r}")
        for accepted in ("v0", "v2.5", "v3600"):
            self.assertFalse(r[accepted], f"-BudgetSec {accepted[1:]} was refused: {r}")

    BUDGET_SEAM = """
$A = New-VenueTicket -Venue bachelor -RunDir 'run-A' -ClaimDir $d -Phase cooldown -StaleAfterSec 600
$env:SEAM_READ = '@@READ@@'; $env:SEAM_TMP = '@@TMP@@'
$r = Update-VenueTicket -Ticket $A -Phase 'waiting' -BudgetSec 0.2
$phase = (Get-Content -LiteralPath $A.path -Raw | ConvertFrom-Json).phase
[pscustomobject]@{ result = [string]$r; phase = $phase; read = (Test-Path -LiteralPath '@@READ@@'); tmp = (Test-Path -LiteralPath '@@TMP@@') } | ConvertTo-Json -Compress
"""

    def test_r8_rule6_no_ticket_read_starts_after_the_budget_ended_while_the_lock_was_taken(self) -> None:
        # r8 rule 6 (r7 pass 3 A6): the budget was asked at the top of an attempt and before the publish, not before the read that follows a lock taken late.
        mod = self.seam_text(("if ($null -eq $lock) { continue }", "if ($null -eq $lock) { continue }; Start-Sleep -Milliseconds 500"),
                             ("$fs = [IO.File]::Open($Ticket.path, [IO.FileMode]::Open, [IO.FileAccess]::Read,", "[IO.File]::WriteAllText($env:SEAM_READ, '1'); $fs = [IO.File]::Open($Ticket.path, [IO.FileMode]::Open, [IO.FileAccess]::Read,"))
        r = self.ps(self.BUDGET_SEAM.replace("@@READ@@", str(self.case / "seam-read.txt")).replace("@@TMP@@", str(self.case / "seam-tmp.txt")), module=mod)
        self.assertFalse(r["read"], f"the ticket was read after the budget ended: {r}")
        self.assertEqual((r["result"], r["phase"]), ("BUSY", "cooldown"), r)

    def test_r8_rule6_no_temp_write_starts_after_the_budget_ended(self) -> None:
        mod = self.seam_text(("$tmp = $Path + '.' + [guid]::NewGuid().ToString('N') + '.tmp'", "$tmp = $Path + '.' + [guid]::NewGuid().ToString('N') + '.tmp'; Start-Sleep -Milliseconds 500"),
                             ("$fs = [IO.File]::Open($tmp, [IO.FileMode]::CreateNew,", "[IO.File]::WriteAllText($env:SEAM_TMP, '1'); $fs = [IO.File]::Open($tmp, [IO.FileMode]::CreateNew,"))
        r = self.ps(self.BUDGET_SEAM.replace("@@READ@@", str(self.case / "seam-read.txt")).replace("@@TMP@@", str(self.case / "seam-tmp.txt")), module=mod)
        self.assertFalse(r["tmp"], f"a temp file was written after the budget ended: {r}")
        self.assertEqual((r["result"], r["phase"]), ("BUSY", "cooldown"), r)

    LOCK_AFTER_BUDGET = """
$A = New-VenueTicket -Venue bachelor -RunDir 'run-A' -ClaimDir $d -Phase cooldown -StaleAfterSec 600
$env:SEAM_READ = '@@READ@@'
$job = Start-Job -ArgumentList '@@MOD@@', $A.path, '@@MARK@@' -ScriptBlock { param($m, $p, $mark) Import-Module $m -Force -DisableNameChecking; $l = & (Get-Module VenueChain) { param($p) Enter-VenueTicketLock -TicketPath $p -TimeoutMs 1000 } $p; [IO.File]::WriteAllText($mark, '1'); Start-Sleep -Milliseconds 800; & (Get-Module VenueChain) { param($l) Exit-VenueTicketLock $l } $l }
$t0 = [DateTime]::UtcNow
while (-not (Test-Path -LiteralPath '@@MARK@@') -and ([DateTime]::UtcNow - $t0).TotalSeconds -lt 60) { Start-Sleep -Milliseconds 20 }
$r = Update-VenueTicket -Ticket $A -Phase 'waiting' -BudgetSec 0.1
Wait-Job $job | Out-Null; Remove-Job $job -Force
[pscustomobject]@{ result = [string]$r; read = (Test-Path -LiteralPath '@@READ@@') } | ConvertTo-Json -Compress
"""

    def test_r8_rule6_no_lock_attempt_starts_after_the_budget_ended_during_the_retry_sleep(self) -> None:
        # r8 rule 6 (r7 pass 3 A6, the lock half): the lock was released after the budget expired, during the 15 ms retry sleep, and one more attempt then took it and went on to read and write.
        # (TAIL-1 r2: the nap is cut to what is left of the timeout, so the seam makes THAT sleep overshoot the budget by 1.2 s, as a descheduled thread does)
        mod = self.seam_text(("if ($napMs -gt 0) { Start-Sleep -Milliseconds $napMs }\n", "if ($napMs -gt 0) { Start-Sleep -Milliseconds 1200 }\n"),
                             ("$fs = [IO.File]::Open($Ticket.path, [IO.FileMode]::Open, [IO.FileAccess]::Read,", "[IO.File]::WriteAllText($env:SEAM_READ, '1'); $fs = [IO.File]::Open($Ticket.path, [IO.FileMode]::Open, [IO.FileAccess]::Read,"))
        script = self.LOCK_AFTER_BUDGET.replace("@@MOD@@", str(mod)).replace("@@READ@@", str(self.case / "seam-read2.txt")).replace("@@MARK@@", str(self.case / "seam-lock-mark.txt"))
        r = self.ps(script, module=mod)
        self.assertFalse(r["read"], f"a lock attempt started after the budget ended and went on to read the ticket: {r}")
        self.assertEqual(r["result"], "BUSY", r)

    # ---- r9: the adversary pass on de591425 (the fix iteration: A4, A5, A7) ----

    # ---- r10 (Grok CoS G13, G14 on 2d3fda1d): the late grant, run for real ----

    LATE_GRANT_HOLDER = """
$ErrorActionPreference = 'Stop'
$cp = '@@CP@@'; $log = '@@LOG@@'
$t0 = [DateTime]::UtcNow; $hold = $null
while ($null -eq $hold -and ([DateTime]::UtcNow - $t0).TotalSeconds -lt 120) { try { $hold = [IO.File]::Open($cp, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read) } catch { Start-Sleep -Milliseconds 2 } }
if ($null -eq $hold) { [pscustomobject]@{ held = $false } | ConvertTo-Json -Compress; exit 0 }
$heldUtc = [DateTime]::UtcNow
$nonce = ''; try { $nonce = [string](Get-Content -LiteralPath $cp -Raw | ConvertFrom-Json).nonce } catch { }
$seenUtc = $null
while ($null -eq $seenUtc -and ([DateTime]::UtcNow - $t0).TotalSeconds -lt 120) {
  $txt = ''; try { $fs = [IO.File]::Open($log, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite); $sr = [IO.StreamReader]::new($fs); $txt = $sr.ReadToEnd(); $sr.Dispose() } catch { }
  if ($txt -match 'CLAIM_LATE_GRANT') { $seenUtc = [DateTime]::UtcNow } else { Start-Sleep -Milliseconds 20 }
}
$stillNamed = $false; try { $stillNamed = ([string](Get-Content -LiteralPath $cp -Raw | ConvertFrom-Json).nonce -ceq $nonce) } catch { }
$hold.Dispose()
Import-Module '@@MOD@@' -Force -DisableNameChecking
$rel = [string](Release-VenueClaim -Venue bachelor -Nonce $nonce -ClaimDir '@@D@@')
$logLines = @(Get-Content -LiteralPath $log | Where-Object { $_ -match 'CLAIM_LATE_GRANT' })
[pscustomobject]@{ held = $true; heldUtc = $heldUtc.ToString('o'); seenUtc = $(if ($seenUtc) { $seenUtc.ToString('o') } else { '' }); nonce = $nonce; stillNamed = $stillNamed; release = $rel; lines = $logLines } | ConvertTo-Json -Compress
"""

    # r11 (sol r10 blocker B2): the reader is the observer of a release already under way. It holds the claim file (FileShare.Read) from the grant, waits for the FIRST Release-VenueClaim call to be logged by the
    # seam (which does not change what the call does), lets go of the file 650 ms later, and then reports whether the claim file is still named by the batch.
    LATE_GRANT_OBSERVER = """
$ErrorActionPreference = 'Stop'
$cp = '@@CP@@'; $rl = '@@RL@@'; $closeMs = @@CLOSEMS@@
$t0 = [DateTime]::UtcNow; $hold = $null
while ($null -eq $hold -and ([DateTime]::UtcNow - $t0).TotalSeconds -lt 120) { try { $hold = [IO.File]::Open($cp, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read) } catch { Start-Sleep -Milliseconds 2 } }
if ($null -eq $hold) { [pscustomobject]@{ held = $false } | ConvertTo-Json -Compress; exit 0 }
$entry = $null
while ($null -eq $entry -and ([DateTime]::UtcNow - $t0).TotalSeconds -lt 120) { if ((Test-Path -LiteralPath $rl) -and ((Get-Item -LiteralPath $rl).Length -gt 0)) { $entry = [DateTime]::UtcNow } else { Start-Sleep -Milliseconds 2 } }
$sw = [Diagnostics.Stopwatch]::StartNew()
while ($sw.Elapsed.TotalMilliseconds -lt $closeMs) { Start-Sleep -Milliseconds 1 }
$hold.Dispose()
$closedUtc = [DateTime]::UtcNow
[pscustomobject]@{ held = $true; sawRelease = ($null -ne $entry); closedAfterMs = [int]$sw.Elapsed.TotalMilliseconds; closedUtc = $closedUtc.ToString('o') } | ConvertTo-Json -Compress
"""

    def late_grant_scenario(self, hold: bool, deadline_sec: float = 20.0, release_seam: bool = False, observe_close_ms: int = 0, slow_full_call_ms: int = 0, stall_sec: int = 0, ambient_defaults: str = "", stall_full_only: bool = False,
                            name: str = "L", receipt_delay_ms: int = 0) -> dict:
        # A real lease batch whose claim is GRANTED AFTER -DeadlineUtc: a copy of VenueChain.psm1 keeps the granted claim standing until 0.2 s past the deadline (the seam, nothing else changed), so the
        # batch takes the late-grant branch. With hold=True a reader (FileShare.Read, no Delete) holds the claim file from the grant until the batch has logged what it did, so every release is refused (BUSY).
        import shutil
        deadline = (datetime.now(timezone.utc) + timedelta(seconds=deadline_sec)).replace(microsecond=0)
        until = deadline + timedelta(milliseconds=200)
        text = MODULE.read_text(encoding="utf-8")
        anchor = "    $r = Invoke-VenueClaimAttempt @attemptArgs\n"
        self.assertEqual(text.count(anchor), 1, "late-grant seam anchor moved")
        seam = anchor + "    if ($r.granted -and $env:SEAM_UNTIL_UTC) { $u = [DateTime]::Parse($env:SEAM_UNTIL_UTC, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind); while ([DateTime]::UtcNow -lt $u) { Start-Sleep -Milliseconds 20 } }\n"
        text = text.replace(anchor, seam)
        env: dict[str, str] = {}
        rel_anchor = "    switch (Remove-VenueClaimIfUnchanged -Path $path -ExpectedNonce $Nonce -MaxAttempts $MaxAttempts) {\n"
        log_call = "[IO.File]::AppendAllText($env:SEAM_RELEASE_LOG, [DateTime]::UtcNow.ToString('o') + ' max=' + $MaxAttempts + [Environment]::NewLine)"
        release_log = str(self.case / ("release-calls.log" if name == "L" else f"release-calls-{name}.log"))
        if release_seam:
            # r10 adversary A1: every Release-VenueClaim call is logged with its start time and its -MaxAttempts; a full call (the default 5) takes 650 ms and answers BUSY (a release that takes most of the 750 ms
            # reserve, deterministically); r11 pass 1: the final single-open probe (-MaxAttempts 1) answers BUSY at once, so the claim is never released and the NOT_RELEASED line is what is asserted
            self.assertEqual(text.count(rel_anchor), 1, "release seam anchor moved")
            text = text.replace(rel_anchor, "    if ($env:SEAM_RELEASE_LOG) { " + log_call + "; if ($MaxAttempts -gt 1) { Start-Sleep -Milliseconds 650 }; return 'BUSY' }\n" + rel_anchor)
            env["SEAM_RELEASE_LOG"] = release_log
        elif stall_sec:
            # VENUE-FIFO-ADVERSARY-TAIL-1 (pass 11 A1, Grok G15/A1b): EVERY Release-VenueClaim call (the full ones and the final probe) never returns inside the test's window: it sleeps stall_sec and answers BUSY.
            self.assertEqual(text.count(rel_anchor), 1, "release seam anchor moved")
            stall_guard = "if ($MaxAttempts -gt 1) { Start-Sleep -Seconds %d }" % stall_sec if stall_full_only else "Start-Sleep -Seconds %d" % stall_sec   # pass 3 A3: only the FULL calls stall; the single-open probe answers BUSY at once
            text = text.replace(rel_anchor, "    if ($env:SEAM_RELEASE_LOG) { " + log_call + "; " + stall_guard + "; return 'BUSY' }\n" + rel_anchor)
            env["SEAM_RELEASE_LOG"] = release_log
        elif observe_close_ms:
            # r11 B2: the same log line at the start of every Release-VenueClaim call. Nothing else changed (the call runs as it ships), unless slow_full_call_ms is given (r11 pass 1 A1): then a FULL call
            # sleeps that long and answers BUSY without touching the file, and only the single-open probe runs for real
            self.assertEqual(text.count(rel_anchor), 1, "release seam anchor moved")
            slow = ("; if ($MaxAttempts -gt 1) { Start-Sleep -Milliseconds %d; return 'BUSY' }" % slow_full_call_ms) if slow_full_call_ms else ""
            text = text.replace(rel_anchor, "    if ($env:SEAM_RELEASE_LOG) { " + log_call + slow + " }\n" + rel_anchor)
            env["SEAM_RELEASE_LOG"] = release_log
        runner = copy_with_siblings(LEASE_RUNNER, Path(tempfile.mkdtemp(prefix="late-", dir=self.case)))
        if receipt_delay_ms:
            # TAIL-1 r5 (sol r4 B2): a delay at the batch's DEADLINE receipt boundary ONLY (between the BATCH_END line and Write-BatchReceipt of the claim-held end), so a batch-path cell can be shown to see it
            rtext = runner.read_text(encoding="utf-8")
            r_anchor = 'LogBest "BATCH_END batch=$batchNo token=$token reason=DEADLINE detail=claim-held"'
            self.assertEqual(rtext.count(r_anchor), 1, "receipt-delay seam anchor moved")
            runner.write_text(rtext.replace(r_anchor, r_anchor + "; Start-Sleep -Milliseconds %d" % receipt_delay_ms), encoding="utf-8")
        (runner.parent / MODULE.name).write_text(text, encoding="utf-8")
        (runner.parent.parent / "bachelor").mkdir(exist_ok=True)   # the runner reads the agent's own script at ..\\bachelor\\ultra-magnus-agent.ps1 for the liveness read-back
        shutil.copy2(DV.parent / "bachelor" / "ultra-magnus-agent.ps1", runner.parent.parent / "bachelor" / "ultra-magnus-agent.ps1")
        if ambient_defaults:
            # TAIL-1 r2 A5: the real (seamed) runner is run through a wrapper that sets the caller's ambient $PSDefaultParameterValues first
            wrapper = self.case / "wrap-late-ambient.ps1"
            wrapper.write_text(ambient_defaults + "\n& '" + str(runner) + "' @args\nexit $LASTEXITCODE\n", encoding="utf-8")
            runner = wrapper
        p = self.start_batch(name, claim_poll=1, runner=runner, deadline_utc=deadline, SEAM_UNTIL_UTC=until.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), **env)
        holder = None
        if hold or observe_close_ms:
            if observe_close_ms:
                script = self.LATE_GRANT_OBSERVER.replace("@@CP@@", str(self.claim_path)).replace("@@RL@@", env["SEAM_RELEASE_LOG"]).replace("@@CLOSEMS@@", str(observe_close_ms))
            else:
                script = (self.LATE_GRANT_HOLDER.replace("@@CP@@", str(self.claim_path)).replace("@@LOG@@", str(Path(p.run_dir) / "batch.log"))   # type: ignore[attr-defined]
                          .replace("@@MOD@@", str(MODULE)).replace("@@D@@", str(self.claims)))
            f = self.case / f"late-grant-holder-{name}.ps1"
            f.write_text(script, encoding="utf-8")
            holder = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(f)], capture_output=True, text=True, timeout=150)
            self.assertEqual(holder.returncode, 0, f"the claim-file holder failed:\n{holder.stdout}\n{holder.stderr}")
        p.wait(timeout=150)
        endUtc = datetime.now(timezone.utc)
        out = Path(p.out_path).read_text(encoding="utf-8", errors="replace")   # type: ignore[attr-defined]
        r = {"log": self.batch_log(p), "out": out, "deadline": deadline, "endUtc": endUtc, "claimLeft": self.claim_path.exists(), "exit": p.returncode,
             "releaseCalls": [l for l in Path(env["SEAM_RELEASE_LOG"]).read_text(encoding="utf-8").splitlines() if l.strip()] if "SEAM_RELEASE_LOG" in env and Path(env["SEAM_RELEASE_LOG"]).exists() else []}
        # TAIL-1 r5 (sol r4 B2): the batch's typed receipt and when it LANDED (the later of its own endedUtc and the file's write time), so a batch-path cell times the receipt, not the helper
        import re
        receipt = Path(p.run_dir) / "batch-01.json"   # type: ignore[attr-defined]
        r["receipt"], r["receiptUtc"] = None, None
        if receipt.exists():
            r["receipt"] = json.loads(receipt.read_text(encoding="utf-8-sig"))
            landed = datetime.fromtimestamp(receipt.stat().st_mtime, timezone.utc)
            ended = r["receipt"].get("endedUtc")
            if ended:
                landed = max(landed, datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", ended).replace("Z", "+00:00")))
            r["receiptUtc"] = landed
        if holder is not None:
            r["holder"] = json.loads([l for l in holder.stdout.splitlines() if l.strip()][-1])
            self.assertTrue(r["holder"]["held"], f"the claim file was never granted (no late grant to test):\n{r['log']}\n{out}")
        self.assertIn("CLAIM_LATE_GRANT", r["log"] + "\n" + out, f"the batch never reached its late-grant branch (the seam or the deadline is wrong):\n{r['log']}\n{out}")
        return r

    def test_r10_g14_a_late_grant_whose_release_is_refused_is_logged_not_released_and_the_claim_stays_until_it_is_freed(self) -> None:
        # r9 A7 / Grok CoS G14 (replaces r9's source-text test, which would pass with wrong logic and fail on a reformat): a REAL batch is granted the claim after -DeadlineUtc while a reader holds the
        # claim file for the whole window. Behaviour asserted: the batch logs CLAIM_LATE_GRANT_NOT_RELEASED (never "let go"), the claim still names the batch while the file is held, and the
        # release succeeds once it is freed (so the claim was never lost track of: its nonce was the grant's).
        r = self.late_grant_scenario(hold=True)
        h = r["holder"]
        self.assertTrue(any("CLAIM_LATE_GRANT_NOT_RELEASED" in l for l in h["lines"]), f"a release the reader refused was not logged as not released: {h['lines']}\n{r['log']}")
        self.assertFalse(any("CLAIM_LATE_GRANT batch=" in l for l in h["lines"]), f"a claim that was NOT let go was logged as let go (the 'CLAIM_LATE_GRANT batch=' line): {h['lines']}")
        self.assertTrue(h["stillNamed"], f"the claim did not still name the batch while the file was held: {h}")
        self.assertEqual(h["release"], "RELEASED", f"the claim could not be released once the file was freed: {h}")
        self.assertFalse(self.claim_path.exists(), "a claim was left behind after the release")

    def test_r10_g13_the_inline_release_retry_of_a_late_grant_stops_within_the_reserve(self) -> None:
        # Grok CoS G13 (r9 A7's retry): five outer tries of Release-VenueClaim (each 5 opens, 50-250 ms apart, about 0.75 s when BUSY) with a 100-500 ms sleep after each, the fifth too, ran up to about
        # 5.25 s past -DeadlineUtc. The bound is the reserve Invoke-ClaimRetryRelease uses ($script:ClaimRetryRequestReserveSec 1 s less $script:ClaimRetryRefusalTailSec 0.25 s = 0.75 s), measured from the
        # retry's start. A reader holds the claim file for the whole window; the seam makes the retry start 0.2 s past the deadline. Allowed: that 0.2 s, the 0.75 s reserve, one release call already in
        # flight when the reserve ends (about 0.75 s), and 1.3 s of tolerance for the log line and this poll = 3.0 s. The unbounded retry took 5.5 s or more.
        r = self.late_grant_scenario(hold=True)
        h = r["holder"]
        self.assertTrue(h["seenUtc"], f"no CLAIM_LATE_GRANT line was logged: {r['log']}")
        spent = (datetime.fromisoformat(h["seenUtc"].replace("Z", "+00:00")) - r["deadline"]).total_seconds()
        self.assertLess(spent, 3.0, f"the late grant's release retry ran {spent:.2f} s past -DeadlineUtc (bound: the 0.75 s reserve, one call in flight and tolerance = 3.0 s): {h['lines']}")
        self.assertTrue(any("CLAIM_LATE_GRANT_NOT_RELEASED" in l for l in h["lines"]), f"the reader held the file, so the release must be logged as not released: {h['lines']}")

    def test_r10_a1_no_release_try_starts_once_the_sleep_before_it_ran_to_the_end_of_the_reserve(self) -> None:
        # r10 adversary A1 (blocker, introduced by G13's loop): the sleep is clamped to what is left of the 0.75 s reserve, and the next try then started without asking the clock again, so a try began at the
        # reserve's end and ran a whole further release call past it. Here every release call takes 650 ms and answers BUSY (the seam): try 1 ends at about 0.65 s, the sleep is clamped to the
        # 0.1 s that is left, and the reserve is spent when it ends. Exactly ONE release call may have been made; the unfixed loop made a second.
        r = self.late_grant_scenario(hold=False, release_seam=True)
        self.assertTrue(any("CLAIM_LATE_GRANT_NOT_RELEASED" in l for l in r["log"].splitlines()), f"the always-BUSY release must be logged as not released:\n{r['log']}")
        # r11 pass 1: the final single-open probe (-MaxAttempts 1, the seam answers it BUSY at once) is not a release call and is not counted: ONE full call, and at most that one probe after it
        full = [c for c in r["releaseCalls"] if c.endswith("max=5")]
        probes = [c for c in r["releaseCalls"] if c.endswith("max=1")]
        self.assertEqual(len(full), 1, f"a release try started after the reserve was spent: calls at {r['releaseCalls']}")
        self.assertLessEqual(len(probes), 1, f"more than one final probe: {r['releaseCalls']}")
        self.assertEqual(len(full) + len(probes), len(r["releaseCalls"]), f"a call with an unexpected -MaxAttempts: {r['releaseCalls']}")

    def test_r11_pass1_a1_a_reader_that_lets_go_between_the_last_open_and_the_end_of_the_reserve_is_met_by_the_final_probe(self) -> None:
        # r11 adversary pass 1 A1 (blocker, introduced by the B2 change): with the trailing sleep gone a BUSY call answers at 500 ms, but one whose fifth open failed at 680 ms (slow opens) is followed by a 70 ms
        # sleep that spends the reserve, and no open follows it: a reader that lets go at 700 ms left the claim behind. The seam makes every FULL release call take 680 ms and answer BUSY without touching the
        # file; the observer lets go 650 ms after the first call starts (about 700 ms by the time it does). Expected: the one final single-open probe at the end of the reserve releases the claim.
        r = self.observed_late_grant(650, slow_full_call_ms=680)   # TAIL-1 r5 H2 sibling: a run whose observer let go after the last open (host load) is run again
        h = r["holder"]
        self.assertTrue(h["sawRelease"], f"the observer never saw a release call: {h}")
        lines = [l for l in r["log"].splitlines() if "CLAIM_LATE_GRANT" in l]
        self.assertTrue(any("CLAIM_LATE_GRANT batch=" in l and "let go" in l for l in lines), f"a reader that let go between the last open and the end of the reserve left the claim unreleased: {lines}\ncalls: {r['releaseCalls']}")
        self.assertFalse(r["claimLeft"], "the late grant's claim file was left behind")
        full = [c for c in r["releaseCalls"] if c.endswith("max=5")]
        self.assertEqual(len(full), 1, f"a whole release call started after the reserve was spent: {r['releaseCalls']}")
        self.assertEqual(r["exit"], 3, f"the batch must still end DEADLINE (exit 3):\n{r['log']}\n{r['out']}")
        self.assertNotRegex(r["log"], r"LEASE_OPEN|LEASE_CHARGED|LEG_START|LEG_PLAN_WRITTEN", f"a lease was opened or charged after a late grant:\n{r['log']}")

    def test_r11_b2_a_reader_that_lets_go_inside_the_reserve_does_not_leave_the_late_grants_claim_behind(self) -> None:
        # r11 sol r10 blocker B2 (introduced by r10's G13 bound): a Release-VenueClaim call's own opens fall at 0, 50, 150, 300 and 500 ms and it answers BUSY at 750 ms (a 250 ms sleep after the last open), which
        # is exactly the 0.75 s reserve, so r10 stopped after that one call. A reader that let go at 650 ms (after the last open, before the answer) left the claim file standing, which r9 would have released
        # at about 850 ms. Here the observer lets go 650 ms after the first release call starts. Expected: the claim is released and logged as let go, the batch ends DEADLINE (exit 3) with no leg
        # started (FAKE_LEG_LOG is not set, so the leg log would not exist) and no lease opened.
        r = self.observed_late_grant(650)   # TAIL-1 r5 (sol r4 H2): a run whose observer let go after the batch's last open (host load) is run again, never read as the batch's fault
        h = r["holder"]
        self.assertTrue(h["sawRelease"], f"the observer never saw a release call: {h}")
        lines = [l for l in r["log"].splitlines() if "CLAIM_LATE_GRANT" in l]
        self.assertTrue(any("CLAIM_LATE_GRANT batch=" in l and "let go" in l for l in lines), f"a reader that let go at 650 ms left the claim unreleased (r10 gave up after one BUSY call): {lines}")
        self.assertFalse(any("NOT_RELEASED" in l for l in lines), f"the claim was logged as not released although the reader let go inside the reserve: {lines}")
        self.assertFalse(r["claimLeft"], "the late grant's claim file was left behind")
        self.assertEqual(r["exit"], 3, f"the batch must still end DEADLINE (exit 3):\n{r['log']}\n{r['out']}")
        self.assertRegex(r["log"], r"BATCH_END batch=\d+ token=\S+ reason=DEADLINE", "the batch did not end DEADLINE")
        self.assertNotRegex(r["log"], r"LEASE_OPEN|LEASE_CHARGED|LEG_START|LEG_PLAN_WRITTEN", f"a lease was opened or charged after a late grant:\n{r['log']}")

    @staticmethod
    def utc_of(stamp: str) -> datetime:
        import re
        return datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", stamp).replace("Z", "+00:00"))

    def reader_let_go_before_an_open(self, r: dict, slow_full_call_ms: int) -> bool:
        # TAIL-1 r5 (sol r4 H2): True when the observer let go of the claim file BEFORE a release call that opens the file started (every call when the full calls run as they ship; only the single-open
        # probe when slow_full_call_ms makes a full call answer BUSY without touching the file). That call's opens all came after the let-go, so a claim left behind is then the batch's fault.
        closed = self.utc_of(r["holder"]["closedUtc"])
        opening = [c for c in r["releaseCalls"] if not slow_full_call_ms or c.endswith("max=1")]
        return any(self.utc_of(c.split()[0]) > closed for c in opening)

    def observed_late_grant(self, observe_close_ms: int, slow_full_call_ms: int = 0, prefix: str = "L", attempts: int = 3, **kw) -> dict:
        # TAIL-1 r5 (sol r4 H2): the observer lets go observe_close_ms after it SEES the first release call logged, and under concurrent suite load it can see it (or wake) late, so it let go after the batch's
        # last open: the bound measured from the retry's start was spent, and BUSY is then the contracted answer (r4's concurrent run: CLAIM_LATE_GRANT_NOT_RELEASED answer=BUSY, the case passing alone).
        # Such a run proves nothing either way and is run again (up to `attempts`); a run whose reader let go before an open, or whose claim was let go, is returned for the caller's assertions.
        runs = []
        for i in range(attempts):
            r = self.late_grant_scenario(hold=False, observe_close_ms=observe_close_ms, slow_full_call_ms=slow_full_call_ms, name=prefix if i == 0 else f"{prefix}{i}", **kw)
            if not r["claimLeft"] or self.reader_let_go_before_an_open(r, slow_full_call_ms):
                r["attempts"] = i + 1
                return r
            runs.append({"closedUtc": r["holder"]["closedUtc"], "releaseCalls": r["releaseCalls"]})
            self.claim_path.unlink(missing_ok=True)   # the batch has exited; its claim must not meet the next run's observer
        self.fail(f"inconclusive under load: in {attempts} runs the reader let go only after the batch's last open of the claim file: {runs}")

    # TAIL-1 r5 (sol r4 B2): the G18 matrix (LATE_RELEASE_SWEEP) times the helper only; these cells drive the REAL batch through the same late grant and time its typed DEADLINE receipt
    G18_BATCH_CELLS = ((650, 680), (650, 0), (725, 750))   # (reader lets go at L ms, full call lasts D ms; 0 = the call as it ships, which answers BUSY at 0.5 s)

    @staticmethod
    def deadline_receipt_bound_sec(d_ms: int) -> float:
        # past -DeadlineUtc: the 0.2 s the seam holds the grant, the 0.75 s reserve, one full call (D, or the shipped call's 0.5 s), and 2.5 s for the probe, the workers' start-up, the log line and the receipt
        return 0.2 + 0.75 + (d_ms or 500) / 1000.0 + 2.5

    def deadline_receipt_faults(self, r: dict, d_ms: int) -> list:
        faults = []
        if r["exit"] != 3:
            faults.append(f"exit {r['exit']} (expected 3, DEADLINE)")
        rec = r["receipt"]
        if not rec or rec.get("endReason") != "DEADLINE" or r["receiptUtc"] is None:
            faults.append(f"no typed DEADLINE receipt: {rec}")
        else:
            past = (r["receiptUtc"] - r["deadline"]).total_seconds()
            bound = self.deadline_receipt_bound_sec(d_ms)
            if past >= bound:
                faults.append(f"the DEADLINE receipt landed {past:.2f} s past -DeadlineUtc (bound {bound:.2f} s)")
        return faults

    def test_tail1_r5_b2_the_g18_batch_path_cells_land_their_deadline_receipt_inside_the_bound(self) -> None:
        # sol r4 B2 (round-1 item 1): the matrix must drive the batch path and time its DEADLINE receipt. Each cell is a real late-granted batch with the reader letting go at L ms and full calls of D ms.
        for k, (l_ms, d_ms) in enumerate(self.G18_BATCH_CELLS):
            with self.subTest(l_ms=l_ms, d_ms=d_ms):
                r = self.observed_late_grant(l_ms, slow_full_call_ms=d_ms, prefix=f"G{k}")
                self.assertEqual(self.deadline_receipt_faults(r, d_ms), [], f"L={l_ms} D={d_ms}:\n{r['log']}\n{r['out']}")
                self.assertFalse(r["claimLeft"], f"L={l_ms} D={d_ms}: the reader let go before an open and the claim was left behind: {r['releaseCalls']} {r['holder']}")

    def test_tail1_r5_b2_a_deadline_receipt_delayed_past_the_bound_fails_its_batch_path_cell(self) -> None:
        # the other half of B2: a delay confined to the batch's DEADLINE receipt boundary (the helper's answer is unchanged) must FAIL the L=650 ms, D=680 ms cell; the helper-only matrix stayed green under it
        bound = self.deadline_receipt_bound_sec(680)
        r = self.late_grant_scenario(hold=False, observe_close_ms=650, slow_full_call_ms=680, name="GD", receipt_delay_ms=int(bound * 1000) + 1000)
        faults = self.deadline_receipt_faults(r, 680)
        self.assertTrue(any("DEADLINE receipt landed" in f for f in faults), f"a DEADLINE receipt delayed past the bound was not seen by its cell: {faults}\n{r['log']}")

    def test_r10_g14_an_unheld_late_grant_is_let_go_and_logged_so(self) -> None:
        # the other half of the behaviour: nobody holds the claim file, the release answers at once, the claim is gone and the line says "let go"
        r = self.late_grant_scenario(hold=False)
        lines = [l for l in r["log"].splitlines() if "CLAIM_LATE_GRANT" in l]
        self.assertTrue(any("CLAIM_LATE_GRANT batch=" in l and "let go" in l for l in lines), f"a released late grant was not logged as let go: {lines}")
        self.assertFalse(any("NOT_RELEASED" in l for l in lines), f"a released late grant was logged as not released: {lines}")
        self.assertFalse(r["claimLeft"], "the late grant's claim was left behind")

    LATE_RELEASE_SWEEP = """
# VENUE-FIFO-ADVERSARY-TAIL-1 (Grok G18, TIMING-BOUND-SWEEP-TEST-BEFORE-LEGS-1): the late grant's release is driven with an INJECTED clock (no real sleep). A reader holds the claim file and lets go at L ms;
# a full release call lasts D ms (its opens spread over the call, the last one 20 ms before its answer) and answers BUSY unless an open falls at or after L; the single-open probe takes one open and ~0 ms.
# A call given a time limit (-TimeoutSec > 0) is cut at it: NO_ANSWER, and only the opens before the cut counted.
$cells = [System.Collections.Generic.List[object]]::new()
foreach ($D in @(0, 500, 680, 750)) {
  foreach ($step in 0..40) {
    $L = $step * 25
    $global:vt = 0.0; $global:released = $false; $global:calls = 0
    $call = {
      param([int]$MaxAttempts, [double]$TimeoutSec)
      $global:calls++
      $start = $global:vt
      $dur = $(if ($MaxAttempts -le 1) { 0.0 } else { $D / 1000.0 })
      $offsets = $(if ($MaxAttempts -le 1) { @(0.0) } elseif ($D -le 0) { @(0.0) } else { @(0.0, 0.05, 0.15, 0.3, [Math]::Max(0.0, $dur - 0.02)) })
      $cut = $(if ($TimeoutSec -gt 0) { $TimeoutSec } else { [double]::PositiveInfinity })
      foreach ($o in $offsets) {
        if ($o -ge $cut) { break }
        if ($o -gt $dur) { continue }
        if (($start + $o) * 1000.0 -ge $L) { $global:released = $true; $global:vt = $start + $o; return 'RELEASED' }
      }
      if ($dur -gt $cut) { $global:vt = $start + $cut; return 'NO_ANSWER' }
      $global:vt = $start + $dur
      return 'BUSY'
    }
    $r = Invoke-VenueLateRelease -Venue bachelor -Nonce 'n' -ClaimDir $d -BoundSec 0.75 -Call $call -Clock { $global:vt } -Sleep { param([int]$Ms) $global:vt += $Ms / 1000.0 }
    $cells.Add([pscustomobject]@{ d = $D; l = $L; released = $global:released; answer = $r.answer; end = [Math]::Round($global:vt, 4); tries = $r.tries; probed = $r.probed })
  }
}
@{ cells = $cells } | ConvertTo-Json -Depth 4 -Compress
"""

    def test_tail1_timing_sweep_the_late_grants_claim_is_released_whenever_the_reader_lets_go_before_the_bound(self) -> None:
        # Grok G18 sweep: reader let-go 0..1000 ms in 25 ms steps x full-call durations fast-BUSY, 500, 680, 750 ms (164 cells), injected clock. (1) a reader that lets go BEFORE the 0.75 s bound
        # never leaves the claim behind; (2) the retry ends within the bound plus one call plus the probe (0.05 s tolerance for the probe).
        r = self.ps(self.LATE_RELEASE_SWEEP)
        cells = r["cells"]
        self.assertEqual(len(cells), 164, len(cells))
        left_behind = [c for c in cells if c["l"] < 750 and not c["released"]]
        self.assertEqual(left_behind, [], f"{len(left_behind)} of {len(cells)} cells left the claim behind although the reader let go before the bound: {left_behind[:8]}")
        too_long = [c for c in cells if c["end"] > 0.75 + c["d"] / 1000.0 + 0.05]
        self.assertEqual(too_long, [], f"{len(too_long)} cells ended later than the bound plus one call plus the probe: {too_long[:8]}")

    def test_tail1_a1_a_release_call_that_never_returns_cannot_hold_the_batch_past_its_bound(self) -> None:
        # pass 11 A1 / G15 / A1b: one bounded worker/timeout seam covers the in-flight call AND the final probe. The seam makes EVERY Release-VenueClaim call sleep 25 s: the batch must still end DEADLINE
        # inside the bound: the 0.2 s the seam holds the grant, the 0.75 s reserve, one call (cap 1.5 s), the probe (cap 1.0 s) and 2.5 s for the workers' start-up and the exit = 6.0 s past -DeadlineUtc.
        # The unbounded inline call held the batch 25 s (and the probe another 25 s).
        r = self.late_grant_scenario(hold=False, stall_sec=25)
        past = (r["endUtc"] - r["deadline"]).total_seconds()
        self.assertLess(past, 6.0, f"the batch ended {past:.1f} s past -DeadlineUtc with a release call that never returns (bound 6.0 s): {r['releaseCalls']}\n{r['log']}")
        # TAIL-1 r2 A4: the release was NOT answered, so the line goes to stderr (batch.log is on the share that stopped answering and is not probed); it is in the batch's output, not in batch.log
        self.assertTrue(any("CLAIM_LATE_GRANT_NOT_RELEASED" in l for l in (r["log"] + "\n" + r["out"]).splitlines()), f"a release that never answered must be logged as not released:\n{r['log']}\n{r['out']}")
        self.assertEqual(r["exit"], 3, f"the batch must still end DEADLINE (exit 3):\n{r['log']}\n{r['out']}")
        # A9 (pass r1 A9): the stall run exercises BOTH the full release call and the final probe: both were started (the seam logs each call's -MaxAttempts at its start)
        self.assertTrue(any(c.endswith("max=5") for c in r["releaseCalls"]), f"no full release call was made: {r['releaseCalls']}")
        self.assertTrue(any(c.endswith("max=1") for c in r["releaseCalls"]), f"the final probe was never started after the full call did not answer: {r['releaseCalls']}")

    def test_tail1_r2_a4_a_late_release_that_was_not_answered_never_probes_batch_log_on_the_failed_share(self) -> None:
        # r1 adversary A4 (blocker, introduced by the bounded worker): both release workers timed out, and the line that follows (CLAIM_LATE_GRANT_NOT_RELEASED, written through L, whose write guard probes batch.log in
        # -RunDir, which may be on the same share) ran unbounded before the DEADLINE result. Invoke-ClaimRetryRelease turns the write suppression on for exactly this; the inline late release did not. With every
        # release call stalled the line must go to stderr (the batch's output) and batch.log must NOT carry it.
        r = self.late_grant_scenario(hold=False, stall_sec=25)
        self.assertNotIn("CLAIM_LATE_GRANT_NOT_RELEASED", r["log"], f"batch.log was written after a release nobody answered (the write suppression was not turned on):\n{r['log']}")
        self.assertIn("CLAIM_LATE_GRANT_NOT_RELEASED", r["out"], f"the line did not reach stderr/stdout:\n{r['out']}")

    def test_tail1_r2_a5_the_late_release_caps_cannot_be_widened_by_a_callers_ambient_defaults(self) -> None:
        # r1 adversary A5: Invoke-VenueLateRelease's -CallCapSec / -ProbeCapSec were left to the parameter defaults, so $PSDefaultParameterValues['Invoke-VenueLateRelease:ProbeCapSec'] = 30 gave the probe 30 s.
        # The batch passes both explicitly now: with every release call stalled it still ends inside the same 6.0 s past -DeadlineUtc.
        amb = "$PSDefaultParameterValues['Invoke-VenueLateRelease:ProbeCapSec'] = 30.0\n$PSDefaultParameterValues['Invoke-VenueLateRelease:CallCapSec'] = 30.0"
        r = self.late_grant_scenario(hold=False, stall_sec=25, ambient_defaults=amb)
        past = (r["endUtc"] - r["deadline"]).total_seconds()
        self.assertLess(past, 6.0, f"the batch ended {past:.1f} s past -DeadlineUtc under ambient cap defaults (bound 6.0 s): {r['releaseCalls']}\n{r['log']}")

    SEAM_R2A2 = """
# a claim the give-back could not let go of is named on B's ticket (pendingReleaseNonce); a reader holds it, B's ticket file is gone (reaped), and the next poll answers BUSY for the release and LOST for the flip
$g = Request-VenueClaim -Venue bachelor -RunDir 'run-B' -ClaimDir $d
$B = New-VenueTicket -Venue bachelor -RunDir 'run-B' -ClaimDir $d
$B | Add-Member -NotePropertyName pendingReleaseNonce -NotePropertyValue ([string]$g.claim.nonce) -Force
Remove-Item -LiteralPath $B.path -Force
$cp = Join-Path $d 'bachelor.claim.json'
$hold = [IO.File]::Open($cp, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
try { $r = Request-VenueClaim -Venue bachelor -RunDir 'run-B' -ClaimDir $d -Ticket $B } finally { $hold.Dispose() }
$pendingBefore = [string]$r.ticket.pendingReleaseNonce   # read now: the next poll clears it once it lets go
$r2 = Request-VenueClaim -Venue bachelor -RunDir 'run-B' -ClaimDir $d -Ticket $r.ticket
$after = $null; if (Test-Path -LiteralPath $cp) { $after = (Get-Content -LiteralPath $cp -Raw | ConvertFrom-Json).nonce }
[pscustomobject]@{ nonce = [string]$g.claim.nonce; newTicket = ($r.ticket.name -cne $B.name); pending = $pendingBefore; claimAfterNextPoll = [bool]$after; ours = [bool]($after -and $after -ceq [string]$g.claim.nonce) } | ConvertTo-Json -Compress
"""

    def test_tail1_r2_a2_a_pending_release_nonce_is_kept_across_a_ticket_that_is_re_taken(self) -> None:
        # r1 adversary A2 (blocker, introduced by A13): the poll's release was BUSY (a reader holds the claim) and the flip then answered LOST (the ticket was reaped), so the new ticket replaced the old one WITHOUT its
        # pendingReleaseNonce: the third poll skipped recovery and the claim stood behind QUEUED answers. The replacement ticket carries the name, and the poll after the reader lets go releases the claim.
        r = self.ps(self.SEAM_R2A2)
        self.assertTrue(r["newTicket"], f"the reaped ticket was not replaced: {r}")
        self.assertEqual(r["pending"], r["nonce"], f"the replacement ticket lost the pending release nonce: {r}")
        # the same poll may then take a FRESH claim as the head (a different nonce); what must be gone is the stranded one
        self.assertFalse(r["ours"], f"the stranded claim was not let go of by the poll after the reader let go: {r}")

    def test_tail1_r2_a1_an_explicit_no_ticket_batch_is_not_defeated_by_a_defaulted_ticket(self) -> None:
        # r1 adversary A1 (same class as pass 11 A6): a caller's $PSDefaultParameterValues['Request-VenueClaim:Ticket'] skipped the ticketless gate of an otherwise valid -NoTicket batch, which then judged the stale
        # ticket LOST and took a new one nothing closed. -Ticket is bound to $null as -UseTicket is.
        wrapper = self.case / "wrap-ambient-ticket.ps1"
        wrapper.write_text("$PSDefaultParameterValues['Request-VenueClaim:Ticket'] = @{ path = '" + str(self.ticket_dir / "stale.ticket.json") + "'; nonce = 'stale'; name = 'stale.ticket.json' }\n& '" + str(LEASE_RUNNER) + "' @args\nexit $LASTEXITCODE\n", encoding="utf-8")
        p = self.start_batch("N", claim_poll=1, runner=wrapper, extra=("-NoTicket",))
        p.wait(timeout=180)
        out = Path(p.out_path).read_text(encoding="utf-8", errors="replace")   # type: ignore[attr-defined]
        self.assertEqual(p.returncode, 0, out)
        self.assertEqual(list(self.ticket_dir.glob("*.ticket.json")) if self.ticket_dir.exists() else [], [], f"an opt-out batch left a ticket behind:\n{self.batch_log(p)}")
        self.assertNotRegex(self.batch_log(p), r"TICKET_(TAKEN|RETAKEN)", "an opt-out batch took or re-took a ticket")

    def test_tail1_r2_a1_the_chain_runner_binds_the_ticket_to_null_wherever_it_opts_out(self) -> None:
        # r1 adversary A1 siblings (Invoke-VenueChain.ps1:351, :476): both opt-out requests bind -Ticket to $null next to -UseTicket:$false
        text = RUNNER.read_text(encoding="utf-8")
        self.assertIn("-UseTicket:$false -Ticket $null", text, "the supplementary request does not bind -Ticket to $null")
        self.assertIn("$rq['UseTicket'] = $false; $rq['Ticket'] = $null", text, "the -NoTicket request does not bind -Ticket to $null")

    A3_SCRIPT = """
# a release call that never returns (the seam stalls Release-VenueClaim) behind a worker whose start-up takes 2 s (the test delay), with the caps under one second: BoundSec 0.75, CallCapSec 0, ProbeCapSec 0.25.
# ONE elapsed-time bound per call: the start-up is inside it (never added before each relative wait) and a cap under one second is honoured, never raised to the worker's 1 s floor.
& (Get-Module VenueChain) { $script:VenueTestPreWaitDelayMs = 2000 }
$env:SEAM_STALL = '1'
$sw = [Diagnostics.Stopwatch]::StartNew()
$r = Invoke-VenueLateRelease -Venue bachelor -Nonce 'n' -ClaimDir $d -BoundSec 0.75 -CallCapSec 0 -ProbeCapSec 0.25
$el = $sw.Elapsed.TotalSeconds
$nan = $false; try { $null = Invoke-VenueLateRelease -Venue bachelor -Nonce 'n' -ClaimDir $d -BoundSec ([double]::NaN) -Call { 'BUSY' } } catch { $nan = $true }
$neg = $false; try { $null = Invoke-VenueLateRelease -Venue bachelor -Nonce 'n' -ClaimDir $d -ProbeCapSec (-1) -Call { 'BUSY' } } catch { $neg = $true }
$big = $false; try { $null = Invoke-VenueLateRelease -Venue bachelor -Nonce 'n' -ClaimDir $d -BoundSec 3000000 -Call { 'BUSY' } } catch { $big = $true }
$json = [pscustomobject]@{ elapsed = $el; answer = [string]$r.answer; probed = [bool]$r.probed; nanRefused = $nan; negativeRefused = $neg; hugeRefused = $big } | ConvertTo-Json -Compress
[Console]::Out.WriteLine($json)
& (Get-Module VenueChain) { Complete-VenueProcess -ExitCode 0 }   # the abandoned (stalled) workers are live threads the host would wait 25 s for
"""

    def test_tail1_r2_a3_a6_the_worker_start_up_is_inside_one_bound_and_sub_second_caps_are_honoured_or_refused(self) -> None:
        # r1 adversary A3 + A6: the start-up delay was added before each relative wait (about 2 s + 1 s per call here, 6 s in all) and each cap under 1 s was raised to 1 s; a NaN bound bound and died in an integer
        # conversion. Fixed: one end per call on a stopwatch started before the worker (2 s + 2 s = 4 s here), and a non-finite or negative cap is refused.
        mod = self.seam_text(("    switch (Remove-VenueClaimIfUnchanged -Path $path -ExpectedNonce $Nonce -MaxAttempts $MaxAttempts) {\n",
                              "    if ($env:SEAM_STALL) { Start-Sleep -Seconds 25; return 'BUSY' }\n    switch (Remove-VenueClaimIfUnchanged -Path $path -ExpectedNonce $Nonce -MaxAttempts $MaxAttempts) {\n"))
        r = self.ps(self.A3_SCRIPT, module=mod)
        # pass 2 A3: ONE end for the whole function (bound + call cap + probe cap = 1.0 s here); the 2 s start-up of the first worker is unavoidable (a synchronous delay in the caller), but the probe is not started at all once that
        # end has passed, so the total is one start-up (2 s), not two (4 s)
        self.assertLess(r["elapsed"], 3.3, f"the stalled release took {r['elapsed']:.1f} s (one end for the whole release: one 2 s start-up, the probe not started past it; the old code added the start-up before each wait, 6 s): {r}")
        self.assertFalse(r["probed"], f"the final probe was started after the whole release's end had passed: {r}")
        self.assertTrue(r["hugeRefused"], f"a 3,000,000 s bound was accepted (it overflowed the sleep conversion): {r}")
        self.assertTrue(r["nanRefused"], f"a NaN -BoundSec was accepted: {r}")
        self.assertTrue(r["negativeRefused"], f"a negative -ProbeCapSec was accepted: {r}")

    A7_SCRIPT = """
# adversary A7: every call is instant (BUSY until 1.2 s on the injected clock); the five tries end at 1.0 s of a 2 s bound, and the final probe belongs at the END of the reserve, where a reader that let go at 1.2 s is met
$global:vt = 0.0; $global:probeAt = -1.0
$call = { param([int]$MaxAttempts, [double]$TimeoutSec); if ($MaxAttempts -le 1) { $global:probeAt = $global:vt }; if ($global:vt -ge 1.2) { return 'RELEASED' }; return 'BUSY' }
$r = Invoke-VenueLateRelease -Venue bachelor -Nonce 'n' -ClaimDir $d -BoundSec 2 -Call $call -Clock { $global:vt } -Sleep { param([int]$Ms) $global:vt += $Ms / 1000.0 }
[pscustomobject]@{ answer = [string]$r.answer; tries = $r.tries; probed = [bool]$r.probed; probeAt = $global:probeAt } | ConvertTo-Json -Compress
"""

    def test_tail1_r2_a7_the_final_probe_is_made_at_the_end_of_the_reserve_not_after_the_fifth_instant_try(self) -> None:
        r = self.ps(self.A7_SCRIPT)
        self.assertEqual(r["answer"], "RELEASED", f"a reader that let go at 1.2 s of a 2 s reserve was missed by the final probe: {r}")
        self.assertGreaterEqual(r["probeAt"], 2.0, f"the final probe was made before the end of the reserve: {r}")

    A9_SCRIPT = """
# A9: when every full call is NO_ANSWER the final probe is still made, as ONE single-open call limited to the probe cap (-MaxAttempts 1, -TimeoutSec = -ProbeCapSec), and no full call is given more than the bound plus its cap
$global:log = [System.Collections.Generic.List[string]]::new(); $global:vt = 0.0
$call = { param([int]$MaxAttempts, [double]$TimeoutSec); $global:log.Add("$MaxAttempts/$([Math]::Round($TimeoutSec, 3))"); $global:vt += 0.2; return 'NO_ANSWER' }
$r = Invoke-VenueLateRelease -Venue bachelor -Nonce 'n' -ClaimDir $d -BoundSec 0.75 -CallCapSec 1.5 -ProbeCapSec 1.0 -Call $call -Clock { $global:vt } -Sleep { param([int]$Ms) $global:vt += $Ms / 1000.0 }
[pscustomobject]@{ answer = [string]$r.answer; probed = [bool]$r.probed; calls = @($global:log) } | ConvertTo-Json -Compress
"""

    def test_tail1_r2_a9_a_full_call_that_does_not_answer_is_followed_by_one_probe_limited_to_the_probe_cap(self) -> None:
        r = self.ps(self.A9_SCRIPT)
        self.assertTrue(r["probed"], r)
        self.assertEqual(r["calls"][-1], "1/1", f"the last call is not the single-open probe at the probe cap: {r}")
        full = [c for c in r["calls"][:-1]]
        self.assertTrue(full and all(c.startswith("5/") and float(c.split("/")[1]) <= 0.75 + 1.5 + 1e-9 for c in full), f"a full call was given more than the bound plus its cap, or is not a full call: {r}")

    def test_tail1_r2_pass3_a1_the_late_release_seams_cannot_be_replaced_by_a_callers_ambient_defaults(self) -> None:
        # pass 3 A1: the test seams of Invoke-VenueLateRelease (-Call, -Clock, -Sleep) were left to $PSDefaultParameterValues, so a default -Call { 'RELEASED' } reported a claim let go that was never opened. They are bound to $null.
        # Every release call stalls (the seam); the ambient default claims RELEASED: the batch must still say the claim was NOT released.
        amb = "$PSDefaultParameterValues['Invoke-VenueLateRelease:Call'] = { param([int]$MaxAttempts, [double]$TimeoutSec) 'RELEASED' }"
        r = self.late_grant_scenario(hold=False, stall_sec=25, ambient_defaults=amb)
        text = r["log"] + "\n" + r["out"]
        self.assertIn("CLAIM_LATE_GRANT_NOT_RELEASED", text, f"an ambient default -Call replaced the release and the batch said the claim was let go:\n{text}")

    def test_tail1_r2_pass3_a3_a_late_release_where_the_full_call_did_not_answer_never_writes_batch_log_even_when_the_probe_did(self) -> None:
        # pass 3 A3: the probe's BUSY overwrote the full call's NO_ANSWER, so the suppression stayed off and batch.log (on the share that stopped answering) was probed. Only the FULL calls stall; the probe answers BUSY at once.
        r = self.late_grant_scenario(hold=False, stall_sec=25, stall_full_only=True)
        self.assertNotIn("CLAIM_LATE_GRANT_NOT_RELEASED", r["log"], f"batch.log was written after a release whose full call never answered:\n{r['log']}")
        self.assertIn("CLAIM_LATE_GRANT_NOT_RELEASED", r["out"], f"the line did not reach stderr/stdout:\n{r['out']}")

    def test_tail1_r2_pass3_a2_a_bounded_read_whose_end_has_passed_starts_no_worker(self) -> None:
        # pass 3 A2: BeginInvoke submitted the worker before the expired wait was computed, so a call with its end already past (a cap of 0, a descheduled caller) still ran the release or the create. Nothing is sent now.
        mark = self.case / "bounded-read-mark.txt"
        r = self.ps(f"""
& (Get-Module VenueChain) {{ $script:VenueTestPreWaitDelayMs = 500 }}   # the worker the old code submitted had half a second to run before it was abandoned
$clock = [Diagnostics.Stopwatch]::StartNew(); Start-Sleep -Milliseconds 300
$w = & (Get-Module VenueChain) {{ param($c) Invoke-VenueBoundedRead -ScriptText 'param($a) [IO.File]::WriteAllText($a, ''ran'')' -Argument '{mark}' -TimeoutSec 5 -Clock $c -EndSec 0.1 }} $clock
Start-Sleep -Milliseconds 800
[pscustomobject]@{{ answered = [bool]$w.answered; ran = (Test-Path -LiteralPath '{mark}') }} | ConvertTo-Json -Compress
""")
        self.assertEqual((r["answered"], r["ran"]), (False, False), f"a worker was started for a call whose end had passed: {r}")

    def test_tail1_r2_pass3_a3_the_late_release_result_says_when_any_call_did_not_answer(self) -> None:
        r = self.ps("""
$call = { param([int]$MaxAttempts, [double]$TimeoutSec); if ($MaxAttempts -gt 1) { return 'NO_ANSWER' }; return 'BUSY' }
$r = Invoke-VenueLateRelease -Venue bachelor -Nonce 'n' -ClaimDir $d -BoundSec 0.1 -Call $call
[pscustomobject]@{ answer = [string]$r.answer; any = [bool]$r.anyUnanswered } | ConvertTo-Json -Compress
""")
        self.assertEqual((r["answer"], r["any"]), ("BUSY", True), r)

    def test_tail1_r2_pass3_a6_the_chain_lets_go_of_a_pending_claim_before_it_drops_the_ticket(self) -> None:
        # pass 3 A6: Close-LegTicket removed the ticket and cleared the object, so the nonce kept on it for a claim a give-back could not let go of was lost, and the claim stood behind a live pid. Lifted from the real script by its AST.
        r = self.ps(f"""
$ast = [Management.Automation.Language.Parser]::ParseFile('{RUNNER}', [ref]$null, [ref]$null)
$fn = $ast.Find({{ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq 'Close-LegTicket' }}, $true)
Invoke-Expression $fn.Extent.Text
function L([string]$m) {{ }}
$Venue = 'bachelor'; $ClaimDir = $d; $TicketKeepMax = 2
$g = Request-VenueClaim -Venue bachelor -RunDir 'run-B' -ClaimDir $d
$script:ticket = New-VenueTicket -Venue bachelor -RunDir 'run-B' -ClaimDir $d
$script:ticket | Add-Member -NotePropertyName pendingReleaseNonce -NotePropertyValue ([string]$g.claim.nonce) -Force
$script:ticketKeeps = 0
Close-LegTicket 'leg-1' 'TICKET_ENDED' 'test'
[pscustomobject]@{{ claimLeft = (Test-Path -LiteralPath (Join-Path $d 'bachelor.claim.json')); ticketCleared = ($null -eq $script:ticket) }} | ConvertTo-Json -Compress
""")
        self.assertEqual((r["claimLeft"], r["ticketCleared"]), (False, True), f"the pending claim was left standing when the chain dropped its ticket: {r}")

    LOCK_BUDGET_SCRIPT = """
# a slow directory check (400 ms, the seam) inside Enter-VenueTicketLock with a 200 ms timeout: the budget is spent before the first open, so no open starts (the seam writes the mark at every open attempt)
$env:SEAM_LOCKTRY = '@@MARK@@'
New-Item -ItemType Directory -Force -Path (Join-Path $d 'bachelor.tickets') | Out-Null
$tp = Join-Path (Join-Path $d 'bachelor.tickets') 'x.ticket.json'
$l = & (Get-Module VenueChain) { param($p) Enter-VenueTicketLock -TicketPath $p -TimeoutMs 200 } $tp
$got = ($null -ne $l); if ($l) { $l.Dispose() }
[pscustomobject]@{ got = $got; tried = (Test-Path -LiteralPath '@@MARK@@') } | ConvertTo-Json -Compress
"""

    def test_tail1_r2_pass10_a4_no_lock_attempt_starts_after_the_timeout_was_spent_in_the_directory_check(self) -> None:
        # pass 10 A4 / pass 11 A3 / r9 A5 (pre-existing): the lock helper started its clock AFTER the directory check, so a slow check was free time and the first open started past the caller's budget. The clock starts at the call.
        mark = self.case / "seam-lockbudget-mark.txt"
        mod = self.seam_text(("    $lockPath = Join-Path $dir '.ticket.lock'\n", "    $lockPath = Join-Path $dir '.ticket.lock'; Start-Sleep -Milliseconds 400\n"),
                             ("        try { return [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate,", "        [IO.File]::WriteAllText($env:SEAM_LOCKTRY, '1'); try { return [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate,"))
        r = self.ps(self.LOCK_BUDGET_SCRIPT.replace("@@MARK@@", str(mark)), module=mod)
        self.assertFalse(r["tried"], f"a lock attempt started after the timeout was spent in the directory check: {r}")
        self.assertFalse(r["got"], r)

    def test_tail1_r2_pass10_a10_a_ticket_whose_created_time_is_null_does_not_crash_the_claim_request(self) -> None:
        # pass 10 A10 (pre-existing): the caller's own valid-looking ticket with createdUtc null: the refresh threw out of Update-VenueTicket (a method call on null) and through Request-VenueClaim. It is answered LOST now.
        r = self.ps("""
$A = New-VenueTicket -Venue bachelor -RunDir 'run-A' -Purpose 'P' -ClaimDir $d
$j = Get-Content -LiteralPath $A.path -Raw | ConvertFrom-Json
$j.createdUtc = $null
[IO.File]::WriteAllText($A.path, ($j | ConvertTo-Json -Compress))
$r = Request-VenueClaim -Venue bachelor -RunDir 'run-A' -Purpose 'P' -UseTicket -ClaimDir $d
$old = @(Get-VenueTickets -Venue bachelor -ClaimDir $d | Where-Object { $_.name -ceq $A.name })
[pscustomobject]@{ state = [string]$r.state; granted = [bool]$r.granted; ticket = [string]$r.ticket.name; replaced = ($r.ticket.name -cne $A.name); oldState = $(if ($old.Count) { [string]$old[0].state } else { 'GONE' }) } | ConvertTo-Json -Compress
""")
        self.assertTrue(r["ticket"], r)
        # pass 2 A1: the uncarryable ticket must not stay LIVE beside its replacement (one owner/run, one live place): it is stale (unreadable) or gone
        self.assertTrue(r["replaced"], r)
        self.assertIn(r["oldState"], ("STALE", "GONE"), f"the ticket that could not be refreshed stayed live beside its replacement: {r}")

    def test_tail1_r2_pass11_a4_a_claim_poll_sleep_that_no_longer_fits_after_the_stranded_ticket_retry_is_not_started(self) -> None:
        # pass 11 A4 / pass 10 A2 (pre-existing in #878's code): the room for the poll sleep was checked BEFORE Invoke-StrandedTicketRetry, which can spend up to 5 s, and the sleep then ran whole. A foreign claim stands; ClaimPollSec is 3;
        # the seam makes the retry take the batch to 1.5 s before -DeadlineUtc, so the 3 s sleep no longer fits: the batch must end DEADLINE at once, not 1.5 s past the deadline.
        import shutil
        s = self.sleeper()
        foreign = self.ps(f"$r = Request-VenueClaim -Venue bachelor -RunDir 'run-foreign' -Purpose foreign -TtlSec 3600 -ClaimDir $d -OwnerPid {s.pid}\n[pscustomobject]@{{ granted = [bool]$r.granted }} | ConvertTo-Json -Compress")
        self.assertTrue(foreign["granted"])
        deadline = (datetime.now(timezone.utc) + timedelta(seconds=14)).replace(microsecond=0)
        text = LEASE_RUNNER.read_text(encoding="utf-8")
        anchor = "        Invoke-StrandedTicketRetry 'claim-wait'\n"
        self.assertEqual(text.count(anchor), 1, "stranded-retry seam anchor moved")
        seam = anchor + "        if ($env:SEAM_STRAND_UNTIL_UTC) { $u = [DateTime]::Parse($env:SEAM_STRAND_UNTIL_UTC, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind); while ([DateTime]::UtcNow -lt $u) { Start-Sleep -Milliseconds 20 }; $env:SEAM_STRAND_UNTIL_UTC = '' }\n"
        runner = copy_with_siblings(LEASE_RUNNER, Path(tempfile.mkdtemp(prefix="strand-", dir=self.case)), text.replace(anchor, seam))
        (runner.parent.parent / "bachelor").mkdir(exist_ok=True)
        shutil.copy2(DV.parent / "bachelor" / "ultra-magnus-agent.ps1", runner.parent.parent / "bachelor" / "ultra-magnus-agent.ps1")
        until = deadline - timedelta(milliseconds=1500)
        p = self.start_batch("S", claim_poll=3, runner=runner, deadline_utc=deadline, SEAM_STRAND_UNTIL_UTC=until.strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
        p.wait(timeout=120)
        ended = datetime.now(timezone.utc)
        out = Path(p.out_path).read_text(encoding="utf-8", errors="replace")   # type: ignore[attr-defined]
        self.assertEqual(p.returncode, 3, f"the batch must end DEADLINE (exit 3):\n{self.batch_log(p)}\n{out}")
        past = (ended - deadline).total_seconds()
        self.assertLess(past, 1.0, f"the batch ended {past:.2f} s past -DeadlineUtc: a poll sleep that no longer fit was started after the stranded retry:\n{self.batch_log(p)}")

    def test_tail1_r2_pass10_a3_a_batch_ticket_calls_worker_start_up_is_inside_its_five_second_bound(self) -> None:
        # pass 10 A3 / r9 A6 (pre-existing): Invoke-BatchTicketCall gave the worker 5 s from AFTER its start-up, so a 3 s start-up (the module's test delay) made 8 s. One stopwatch started before the worker now ends the call at 5 s.
        # The function is lifted from the real script by its AST (the script cannot be dot-sourced).
        script = f"""
$ast = [Management.Automation.Language.Parser]::ParseFile('{LEASE_RUNNER}', [ref]$null, [ref]$null)
$fn = $ast.Find({{ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq 'Invoke-BatchTicketCall' }}, $true)
Invoke-Expression $fn.Extent.Text
$deadline = [DateTime]::UtcNow.AddSeconds(120); $here = '{DV}'
& (Get-Module VenueChain) {{ $script:VenueTestPreWaitDelayMs = 3000 }}
$sw = [Diagnostics.Stopwatch]::StartNew()
$r = Invoke-BatchTicketCall 'Start-Sleep -Seconds 20; ''x''' @{{ }}
$json = [pscustomobject]@{{ elapsed = $sw.Elapsed.TotalSeconds; answered = [bool]$r.answered }} | ConvertTo-Json -Compress
[Console]::Out.WriteLine($json)
& (Get-Module VenueChain) {{ Complete-VenueProcess -ExitCode 0 }}
"""
        r = self.ps(script)
        self.assertFalse(r["answered"], r)
        self.assertLess(r["elapsed"], 6.5, f"the ticket call took {r['elapsed']:.1f} s: the worker's 3 s start-up was added to its 5 s bound (8 s): {r}")

    def test_tail1_r2_pass10_a11_a_ticket_whose_lifetime_is_not_a_finite_number_is_stale_not_live_for_ever(self) -> None:
        # pass 10 A11 (pre-existing): staleAfterSec as the JSON string NaN / Infinity parsed, and "heartbeat age > NaN" is false, so a ticket whose heartbeat stopped 10 minutes ago stayed live and blocked the line for ever.
        r = self.ps("""
$old = [DateTime]::UtcNow.AddSeconds(-600)
$out = [ordered]@{}
foreach ($bad in @('NaN', 'Infinity', '-Infinity', '0', '1e20', '2147483648')) {   # pass 3 A7 + r4 K1: a lifetime past the Int32 range overflows the refresh's conversion (86401 is NOT in this list: the chain publishes lifetimes over a day, see the r4 K1 test)
  $t = New-VenueTicket -Venue bachelor -RunDir ('run-' + $bad) -ClaimDir $d -NowUtc $old -StaleAfterSec 90
  $j = Get-Content -LiteralPath $t.path -Raw | ConvertFrom-Json
  $j.staleAfterSec = $bad
  [IO.File]::WriteAllText($t.path, ($j | ConvertTo-Json -Compress))
  $row = @(Get-VenueTickets -Venue bachelor -ClaimDir $d | Where-Object { $_.name -ceq $t.name })[0]
  $out[$bad] = [string]$row.state
  Remove-Item -LiteralPath $t.path -Force
}
$out | ConvertTo-Json -Compress
""")
        for bad, state in r.items():
            self.assertEqual(state, "STALE", f"a ticket whose staleAfterSec is {bad!r} and whose heartbeat is 10 minutes old stayed live: {r}")

    def test_tail1_r4_k1_every_lifetime_a_supported_writer_can_publish_is_judged_by_its_heartbeat_age_never_unreadable(self) -> None:
        # r4 K1 (sol, opus, fable all agree): the r2 pass 3 A7 ceiling (staleAfterSec over 86400 is 'unreadable') said "the writers allow 1..86400", but Update-VenueTicket -StaleAfterSec is an unranged [int] and the chain
        # publishes Get-VenueTicketProbeStaleSec's value through it: -ProbeAdmissionWaitSec 86400 with the default 600 s quiet wait gives 87630. A live, freshly heartbeated ticket read STALE and another requester reaped it.
        # Every value the writer can carry (up to Int32) is a lifetime; the reader is bounded at what the refresh can carry, nothing narrower.
        r = self.ps("""
$cases = [ordered]@{
  chainProbeAdmission86400 = [int](Get-VenueTicketProbeStaleSec -QuietMaxWaitSec 600 -AdmissionWaitSec 86400)
  chainProbeMaxOperands = [int](Get-VenueTicketProbeStaleSec -QuietMaxWaitSec 86400 -AdmissionWaitSec 86400 -MarginSec 86400)
  dayAndOneSecond = 86401
  int32Max = 2147483647
}
$out = [ordered]@{}
foreach ($k in $cases.Keys) {
  $A = New-VenueTicket -Venue bachelor -RunDir ('run-' + $k) -ClaimDir $d -Phase waiting -StaleAfterSec 90
  $u = [string](Update-VenueTicket -Ticket $A -Phase 'probing' -StaleAfterSec $cases[$k])
  $fresh = @(Get-VenueTickets -Venue bachelor -ClaimDir $d | Where-Object { $_.name -ceq $A.name })[0]
  $late = @(Get-VenueTickets -Venue bachelor -ClaimDir $d -NowUtc ([DateTime]::UtcNow.AddSeconds([double]$cases[$k] + 60.0)) | Where-Object { $_.name -ceq $A.name })[0]
  $out[$k] = [pscustomobject]@{ secs = $cases[$k]; update = $u; freshState = [string]$fresh.state; freshReason = [string]$fresh.reason; lateState = [string]$late.state; lateReason = [string]$late.reason }
  Remove-Item -LiteralPath $A.path -Force
}
$out | ConvertTo-Json -Compress -Depth 4
""")
        self.assertEqual(r["chainProbeAdmission86400"]["secs"], 87630, r)
        for k, row in r.items():
            self.assertEqual(row["update"], "OK", f"{k}: the writer refused a lifetime the chain publishes: {r}")
            self.assertEqual((row["freshState"], row["freshReason"]), ("LIVE", ""), f"{k}: a live, freshly heartbeated ticket read stale, so another requester reaps it and the chain loses its place: {r}")
            self.assertEqual((row["lateState"], row["lateReason"]), ("STALE", "heartbeat"), f"{k}: a ticket whose heartbeat is older than its lifetime must still go stale by heartbeat age: {r}")

    def test_tail1_r4_k2_a_pending_claim_a_busy_close_could_not_let_go_of_is_kept_and_released_at_a_later_close(self) -> None:
        # r4 K2 (sol): Close-LegTicket released the claim a give-back could not let go of ONCE (three opens), logged "released as the ticket ends" whatever the answer, and dropped the ticket object with the nonce on it, so a reader
        # that held the claim file through those three opens left the claim standing behind this live chain with no reference to it. The nonce now outlives the ticket and is released at the next close. Lifted by its AST.
        r = self.ps(f"""
$ast = [Management.Automation.Language.Parser]::ParseFile('{RUNNER}', [ref]$null, [ref]$null)
$fn = $ast.Find({{ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq 'Close-LegTicket' }}, $true)
Invoke-Expression $fn.Extent.Text
$script:logs = [System.Collections.Generic.List[string]]::new()
function L([string]$m) {{ $script:logs.Add($m) }}
$Venue = 'bachelor'; $ClaimDir = $d; $TicketKeepMax = 2
$cp = Join-Path $d 'bachelor.claim.json'
$out = [ordered]@{{}}
# (1) a reader holds the claim file through every open of the close: the release answers BUSY
$g = Request-VenueClaim -Venue bachelor -RunDir 'run-B' -ClaimDir $d
$script:ticket = New-VenueTicket -Venue bachelor -RunDir 'run-B' -ClaimDir $d
$script:ticket | Add-Member -NotePropertyName pendingReleaseNonce -NotePropertyValue ([string]$g.claim.nonce) -Force
$script:ticketKeeps = 0
$hold = [IO.File]::Open($cp, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
Close-LegTicket 'leg-1' 'TICKET_ENDED' 'test'
$out['busyClaimLeft'] = (Test-Path -LiteralPath $cp)
$out['busyLog'] = ($script:logs -join ' | ')
$hold.Dispose()
Close-LegTicket 'leg-2' 'TICKET_ENDED' 'test'   # no ticket is left: the kept nonce is still let go of
$out['busyClaimAfterLaterClose'] = (Test-Path -LiteralPath $cp)
# (2) the release THROWS at the close (the claim open is refused)
$g2 = Request-VenueClaim -Venue bachelor -RunDir 'run-B' -ClaimDir $d
$script:ticket = New-VenueTicket -Venue bachelor -RunDir 'run-B' -ClaimDir $d
$script:ticket | Add-Member -NotePropertyName pendingReleaseNonce -NotePropertyValue ([string]$g2.claim.nonce) -Force
$script:ticketKeeps = 0
$script:refuseRelease = $true
function Release-VenueClaim([string]$Venue, [string]$Nonce, [string]$ClaimDir, [int]$MaxAttempts) {{
    if ($script:refuseRelease) {{ throw 'seam: the claim open is refused' }}
    & (Get-Module VenueChain) {{ param($v, $n, $c, $m) Release-VenueClaim -Venue $v -Nonce $n -ClaimDir $c -MaxAttempts $m }} $Venue $Nonce $ClaimDir $MaxAttempts
}}
Close-LegTicket 'leg-3' 'TICKET_ENDED' 'test'
$out['throwClaimLeft'] = (Test-Path -LiteralPath $cp)
$script:refuseRelease = $false
Close-LegTicket 'leg-4' 'TICKET_ENDED' 'test'
$out['throwClaimAfterLaterClose'] = (Test-Path -LiteralPath $cp)
$out | ConvertTo-Json -Compress
""")
        self.assertTrue(r["busyClaimLeft"], f"the seam did not hold the claim through the close: {r}")
        self.assertNotIn("released as the ticket ends", r["busyLog"], f"the close logged a release that did not happen: {r}")
        self.assertIn("result=BUSY", r["busyLog"], r)
        self.assertFalse(r["busyClaimAfterLaterClose"], f"the nonce of the claim a busy close could not let go of was dropped with the ticket: the claim stands behind this live chain: {r}")
        self.assertTrue(r["throwClaimLeft"], r)
        self.assertFalse(r["throwClaimAfterLaterClose"], f"the nonce was dropped when the release at the close threw: {r}")

    def test_tail1_r4_adv_a1_a_ticket_folder_that_goes_away_between_the_two_checks_of_a_strict_listing_throws(self) -> None:
        # r4 adversary A1 (introduced by K3): -ThrowOnListError checked the folder once to throw and then again to enumerate, so a folder that became unavailable between the two answered no rows without an error (an "empty line").
        # The module's Test-Path is shadowed so the second answer for the ticket folder is false; one check (or none shadowed) lists normally.
        r = self.ps("""
$A = New-VenueTicket -Venue bachelor -RunDir 'run-A' -ClaimDir $d
& (Get-Module VenueChain) {
    $script:dirChecks = 0
    function script:Test-Path {
        param([string]$LiteralPath, [string]$PathType)
        $real = Microsoft.PowerShell.Management\\Test-Path -LiteralPath $LiteralPath -PathType $PathType
        if ($LiteralPath -like '*bachelor.tickets') { $script:dirChecks++; if ($script:dirChecks -ge 2) { return $false } }
        $real
    }
}
$threw = $false; $rows = -1
try { $rows = @(Get-VenueTickets -Venue bachelor -ClaimDir $d -ThrowOnListError).Count } catch { $threw = $true }
[pscustomobject]@{ threw = $threw; rows = $rows } | ConvertTo-Json -Compress
""")
        self.assertNotEqual((r["threw"], r["rows"]), (False, 0), f"a folder that went away between the two checks answered an empty line without an error: {r}")

    def test_tail1_r4_adv_a5_a_close_whose_diagnostic_line_cannot_be_written_still_ends_the_ticket_and_keeps_the_nonce(self) -> None:
        # r4 adversary A5 (introduced by K2): the CLAIM_PENDING_RELEASE_NOT_RELEASED line is written before the ticket is removed; a chain.log that cannot be written (a handle held after the deadline line) made L throw there, so the close aborted
        # before the ticket was removed and before the chain's final retry and result. A diagnostic line is best effort (r7 A15's rule): the ticket still ends and the nonce is still kept.
        r = self.ps(f"""
$ast = [Management.Automation.Language.Parser]::ParseFile('{RUNNER}', [ref]$null, [ref]$null)
$fn = $ast.Find({{ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq 'Close-LegTicket' }}, $true)
Invoke-Expression $fn.Extent.Text
function L([string]$m) {{ if ($m -like 'CLAIM_PENDING_RELEASE*') {{ throw 'chain.log is held by another handle' }} }}
$Venue = 'bachelor'; $ClaimDir = $d; $TicketKeepMax = 2
$cp = Join-Path $d 'bachelor.claim.json'
$g = Request-VenueClaim -Venue bachelor -RunDir 'run-B' -ClaimDir $d
$script:ticket = New-VenueTicket -Venue bachelor -RunDir 'run-B' -ClaimDir $d
$tp = $script:ticket.path
$script:ticket | Add-Member -NotePropertyName pendingReleaseNonce -NotePropertyValue ([string]$g.claim.nonce) -Force
$script:ticketKeeps = 0
$hold = [IO.File]::Open($cp, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
$threw = $false; try {{ Close-LegTicket 'leg-1' 'TICKET_ENDED' 'test' }} catch {{ $threw = $true }}
$hold.Dispose()
[pscustomobject]@{{ threw = $threw; ticketFileLeft = (Test-Path -LiteralPath $tp); strandedKept = @($script:strandedClaimNonces).Count }} | ConvertTo-Json -Compress
""")
        self.assertFalse(r["threw"], f"an unwritable diagnostic line aborted the close: {r}")
        self.assertEqual((r["ticketFileLeft"], r["strandedKept"]), (False, 1), f"the close stopped before the ticket was removed or the nonce kept: {r}")

    def test_tail1_r5_b1_a_chain_log_held_through_every_line_of_the_close_still_reaches_the_final_nonce_retry(self) -> None:
        # r5 (sol r4 B1, the sibling of r4 A5): A5's fixture threw only for the CLAIM_PENDING_RELEASE* lines, so the TICKET_ENDED line on the SAME held chain.log still threw after the ticket file was gone,
        # leaving the ticket object set and ending the chain before its final nonce retry and CHAIN_RESULT. Here the REAL L and Close-LegTicket are lifted from the script, chain.log is held against
        # writes (FileShare.Read) for the whole test, the release answers BUSY at the close (the claim file is held too), and then the chain-end retry block (lifted by its condition) runs with the claim freed.
        r = self.ps(f"""
$ast = [Management.Automation.Language.Parser]::ParseFile('{RUNNER}', [ref]$null, [ref]$null)
foreach ($name in 'L', 'Close-LegTicket') {{
    $fn = $ast.Find({{ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq $name }}, $true)
    Invoke-Expression $fn.Extent.Text
}}
$tail = $ast.Find({{ param($n) $n -is [Management.Automation.Language.IfStatementAst] -and $n.Clauses[0].Item1.Extent.Text -ceq '$null -ne $script:strandedClaimNonces' }}, $true)
$Venue = 'bachelor'; $ClaimDir = $d; $TicketKeepMax = 2
$ChainLog = Join-Path (Split-Path -Parent $d) 'chain.log'; [IO.File]::WriteAllText($ChainLog, '')
$logHold = [IO.File]::Open($ChainLog, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
$cp = Join-Path $d 'bachelor.claim.json'
$g = Request-VenueClaim -Venue bachelor -RunDir 'run-B' -ClaimDir $d
$script:ticket = New-VenueTicket -Venue bachelor -RunDir 'run-B' -ClaimDir $d
$tp = $script:ticket.path
$script:ticket | Add-Member -NotePropertyName pendingReleaseNonce -NotePropertyValue ([string]$g.claim.nonce) -Force
$script:ticketKeeps = 0
$hold = [IO.File]::Open($cp, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
$threw = ''; $closeOut = @()
try {{ $closeOut = @(Close-LegTicket 'leg-1' 'TICKET_ENDED' 'test' 2>$null) }} catch {{ $threw = $_.Exception.Message }}
$hold.Dispose()
$o = [ordered]@{{ threw = $threw; ticketSet = ($null -ne $script:ticket); ticketFileLeft = (Test-Path -LiteralPath $tp); strandedAfterClose = @($script:strandedClaimNonces).Count; endedLine = [bool](@($closeOut) -match 'TICKET_ENDED leg=leg-1') }}
$tailThrew = ''
try {{ $null = Invoke-Expression $tail.Extent.Text 2>$null }} catch {{ $tailThrew = $_.Exception.Message }}
$o['tailThrew'] = $tailThrew; $o['strandedAfterTail'] = @($script:strandedClaimNonces).Count; $o['claimLeft'] = (Test-Path -LiteralPath $cp)
$logHold.Dispose()
$o | ConvertTo-Json -Compress
""")
        self.assertEqual(r["threw"], "", f"a held chain.log ended the close (the TICKET_ENDED line threw): {r}")
        self.assertFalse(r["ticketSet"], f"the ticket object was left set after its file was removed: {r}")
        self.assertEqual((r["ticketFileLeft"], r["strandedAfterClose"]), (False, 1), f"the close stopped before the ticket was removed or the nonce kept: {r}")
        self.assertTrue(r["endedLine"], f"the TICKET_ENDED line did not reach stdout although the log could not take it: {r}")
        self.assertEqual((r["tailThrew"], r["strandedAfterTail"], r["claimLeft"]), ("", 0, False), f"the chain-end retry did not let go of the kept nonce with the log still held: {r}")

    def test_tail1_r5_adv_a5_a_failed_chain_log_append_is_never_followed_by_a_second_one_and_never_holds_the_ticket_reset(self) -> None:
        # r5 adversary A5 (introduced by the r5 B1 retry): after a failed append L made up to two more synchronous appends, and one on a share can stall (30 s in the pass's input) on the control thread, at a
        # ticket close or before CHAIN_RESULT; the close also reset the ticket only AFTER its TICKET_ENDED line. Add-Content is shadowed: the first append fails, any later one would stall. One L call must make
        # ONE append and still put its line on stdout first; the close must drop the ticket object before its line is written.
        r = self.ps(f"""
$ast = [Management.Automation.Language.Parser]::ParseFile('{RUNNER}', [ref]$null, [ref]$null)
foreach ($name in 'L', 'Close-LegTicket') {{
    $fn = $ast.Find({{ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq $name }}, $true)
    Invoke-Expression $fn.Extent.Text
}}
$script:appends = 0; $script:outBeforeAppend = $null; $script:emitted = [System.Collections.Generic.List[string]]::new()
function Add-Content {{ param([string]$LiteralPath, $Value, $ErrorAction) $script:appends++; if ($script:appends -eq 1) {{ throw 'chain.log is held by another handle' }}; Start-Sleep -Milliseconds 2000 }}
$ChainLog = Join-Path (Split-Path -Parent $d) 'chain.log'
$sw = [Diagnostics.Stopwatch]::StartNew()
L 'ONE_LINE x=1' 2>$null | ForEach-Object {{ if ($null -eq $script:outBeforeAppend) {{ $script:outBeforeAppend = ($script:appends -eq 0) }}; $script:emitted.Add([string]$_) }}
$oneSec = $sw.Elapsed.TotalSeconds
$o = [ordered]@{{ appends = $script:appends; outBeforeAppend = $script:outBeforeAppend; emitted = @($script:emitted); oneSec = [Math]::Round($oneSec, 2) }}
# the close: a stub L records whether the ticket object was already dropped when its TICKET_ENDED line was written
$Venue = 'bachelor'; $ClaimDir = $d; $TicketKeepMax = 2
function L([string]$Message, [switch]$Raw) {{ if ($Message -like 'TICKET_ENDED*') {{ $script:ticketAtLine = ($null -ne $script:ticket) }} }}
$script:ticket = New-VenueTicket -Venue bachelor -RunDir 'run-B' -ClaimDir $d; $script:ticketKeeps = 1; $script:ticketAtLine = $null
Close-LegTicket 'leg-1' 'TICKET_ENDED' 'test'
$o['ticketSetAtLine'] = $script:ticketAtLine
$o | ConvertTo-Json -Compress
""")
        self.assertEqual(r["appends"], 1, f"a failed chain.log append was followed by another synchronous append (one that can stall on a share): {r}")
        self.assertTrue(r["outBeforeAppend"], f"the line reached stdout only after the append returned: {r}")
        self.assertEqual(len(r["emitted"]), 1, r)
        self.assertTrue(r["emitted"][0].endswith("ONE_LINE x=1"), r)
        self.assertIs(r["ticketSetAtLine"], False, f"the close still held the ticket reset behind its TICKET_ENDED line: {r}")

    def test_tail1_r5_pass2_a1_no_close_or_chain_end_diagnostic_is_written_before_every_release_and_the_reset(self) -> None:
        # r5 adversary pass 2 A1: a chain.log append on a share can STALL (not fail), and the close wrote its CLAIM_PENDING_RELEASE* line before the ticket's removal and reset, the chain end before its next
        # nonce's release. Every diagnostic line is now written only after every release and the reset. A stub L records, at its FIRST line, what had already been done.
        r = self.ps(f"""
$ast = [Management.Automation.Language.Parser]::ParseFile('{RUNNER}', [ref]$null, [ref]$null)
$fn = $ast.Find({{ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq 'Close-LegTicket' }}, $true)
Invoke-Expression $fn.Extent.Text
$tail = $ast.Find({{ param($n) $n -is [Management.Automation.Language.IfStatementAst] -and $n.Clauses[0].Item1.Extent.Text -ceq '$null -ne $script:strandedClaimNonces' }}, $true)
$Venue = 'bachelor'; $ClaimDir = $d; $TicketKeepMax = 2
$script:releases = 0; $script:first = $null
function Release-VenueClaim([string]$Venue, [string]$Nonce, [string]$ClaimDir, [int]$MaxAttempts) {{ $script:releases++; 'BUSY' }}
function L([string]$Message, [switch]$Raw) {{ if ($null -eq $script:first) {{ $script:first = [ordered]@{{ line = $Message; ticketSet = ($null -ne $script:ticket); releases = $script:releases }} }} }}
$script:strandedClaimNonces = [System.Collections.Generic.List[string]]::new(); $script:strandedClaimNonces.Add('n-old')
$script:ticket = New-VenueTicket -Venue bachelor -RunDir 'run-B' -ClaimDir $d
$script:ticket | Add-Member -NotePropertyName pendingReleaseNonce -NotePropertyValue 'n-new' -Force
$script:ticketKeeps = 0
Close-LegTicket 'leg-1' 'TICKET_ENDED' 'test'
$o = [ordered]@{{ close = $script:first }}
$script:releases = 0; $script:first = $null
$null = Invoke-Expression $tail.Extent.Text
$o['tail'] = $script:first
$o | ConvertTo-Json -Compress -Depth 4
""")
        self.assertEqual((r["close"]["ticketSet"], r["close"]["releases"]), (False, 2), f"the close wrote a diagnostic line before every release and the ticket's reset: {r}")
        self.assertEqual(r["tail"]["releases"], 2, f"the chain end wrote a diagnostic line before every kept nonce's release: {r}")

    def give_back_scenario(self, name: str, release_seam: str, env: dict) -> dict:
        # the A7 line-up (B takes the claim as head while A is in cooldown, A flips to waiting while B holds it, so B must give the claim back), with ONE seam inside the give-back
        from unittest import mock
        mark = self.case / f"seam-{name}-mark.txt"
        mod = self.seam_text(("    $r = Invoke-VenueClaimAttempt @attemptArgs\n", "    [IO.File]::WriteAllText($env:SEAM_MARK, '1'); Start-Sleep -Milliseconds 2500\n    $r = Invoke-VenueClaimAttempt @attemptArgs\n"), *self.give_back_pairs(release_seam))
        script = self.SEAM_T1A7.replace("@@SEAMMOD@@", str(mod)).replace("@@MARK@@", str(mark)).replace("queueCount = @($r.queue).Count;", "queueCount = @($r.queue).Count; pending = [string]$r.ticket.pendingReleaseNonce; claimExists = (Test-Path -LiteralPath (Join-Path $d 'bachelor.claim.json'));")
        with mock.patch.dict(os.environ, env):
            return self.ps(script)

    @staticmethod
    def give_back_pairs(release_seam: str) -> tuple:
        brk = "                    if ($back -cin @('RELEASED', 'ABSENT', 'NOT_OURS')) { break }\n"
        deny = ("            [IO.File]::Delete($Path)   # allowed while our handle is open because it shares Delete; the name is gone once the handle closes\n",
                "            if ($env:SEAM_DENY_DELETE) { throw 'Access to the path is denied (seam)' }\n            [IO.File]::Delete($Path)   # allowed while our handle is open because it shares Delete; the name is gone once the handle closes\n")
        if release_seam == "reapall":
            # pass 3 A5: every ticket file is gone after the release, as an unreadable folder looks to the listing (it swallows the error and answers no rows)
            return ((brk, "                    if ($env:SEAM_REAP_ALL -and $back -ceq 'RELEASED') { Get-ChildItem -LiteralPath (Split-Path -Parent $t.path) -Filter '*.ticket.json' | Remove-Item -Force }\n" + brk),)
        reap_all_loop = "                    if ($env:SEAM_REAP_ALL_IN_LOOP) { Get-ChildItem -LiteralPath (Split-Path -Parent $t.path) -Filter '*.ticket.json' | Remove-Item -Force }\n"
        fail_listing = ("        foreach ($file in @(Get-ChildItem -LiteralPath $dir -Filter '*.ticket.json' -File -ErrorAction $listAction)) {\n",
                        "        if ($env:SEAM_LIST_FAIL -and $ThrowOnListError) { throw 'seam: the ticket folder could not be listed' }\n        foreach ($file in @(Get-ChildItem -LiteralPath $dir -Filter '*.ticket.json' -File -ErrorAction $listAction)) {\n")
        if release_seam == "denyreapall":
            # r4 K3: the five give-backs all fail and EVERY ticket file is gone after them, from a folder that is still readable
            return (deny, (brk, reap_all_loop + brk))
        if release_seam == "reapallfail":
            # r4 K3 guard: as reapall, but the listing made after the release throws (an unreadable folder)
            return ((brk, "                    if ($env:SEAM_REAP_ALL -and $back -ceq 'RELEASED') { Get-ChildItem -LiteralPath (Split-Path -Parent $t.path) -Filter '*.ticket.json' | Remove-Item -Force; $env:SEAM_LIST_FAIL = '1' }\n" + brk), fail_listing)
        if release_seam == "denyreapallfail":
            return (deny, (brk, reap_all_loop + "                    if ($env:SEAM_REAP_ALL_IN_LOOP) { $env:SEAM_LIST_FAIL = '1' }\n" + brk), fail_listing)
        if release_seam == "denyreap":
            # pass 3 A4: the five give-backs all fail (the delete is refused) and B's own ticket is reaped inside them
            return (deny, (brk, "                    if ($env:SEAM_REAP_IN_LOOP) { Remove-Item -LiteralPath $t.path -Force -ErrorAction SilentlyContinue }\n" + brk))
        if release_seam == "reap":
            return (("                    if ($back -cin @('RELEASED', 'ABSENT', 'NOT_OURS')) { break }\n",
                     "                    if ($env:SEAM_REAP_AFTER_RELEASE -and $back -ceq 'RELEASED') { Remove-Item -LiteralPath $t.path -Force }\n                    if ($back -cin @('RELEASED', 'ABSENT', 'NOT_OURS')) { break }\n"),)
        return (("            [IO.File]::Delete($Path)   # allowed while our handle is open because it shares Delete; the name is gone once the handle closes\n",
                 "            if ($env:SEAM_DENY_DELETE) { throw 'Access to the path is denied (seam)' }\n            [IO.File]::Delete($Path)   # allowed while our handle is open because it shares Delete; the name is gone once the handle closes\n"),)

    def test_tail1_r2_a8_the_queue_and_place_a_released_give_back_returns_are_read_after_the_release(self) -> None:
        # r1 adversary A8 (pre-existing since r8 rule 7): the give-back returned the census made BEFORE its release; a ticket reaped during the retries (here B's own, right after the release) stayed in it and B kept a place at 2
        # of 2. The census is read again after the release: B is not in it (place 0), and the line is A alone.
        r = self.give_back_scenario("a8", "reap", {"SEAM_REAP_AFTER_RELEASE": "1"})
        self.assertEqual((r["state"], r["granted"]), ("QUEUED", False), r)
        self.assertEqual((r["queueCount"], r["position"]), (1, 0), f"the give-back returned the census made before the release: {r}")
        self.assertFalse(r["claimExists"], f"the claim B gave back is still standing: {r}")

    def test_tail1_r2_pass3_a4_a_give_back_that_could_not_let_go_returns_the_census_read_after_its_tries(self) -> None:
        # pass 3 A4: the released case read the census again, the five-BUSY case still returned the one made before the tries (1.5 s of sleeps, in which B's own ticket was reaped)
        r = self.give_back_scenario("p3a4", "denyreap", {"SEAM_DENY_DELETE": "1", "SEAM_REAP_IN_LOOP": "1"})
        self.assertEqual((r["state"], r["granted"]), ("QUEUED", False), r)
        self.assertEqual((r["queueCount"], r["position"]), (1, 0), f"the five-BUSY give-back returned the census made before its tries: {r}")

    def test_tail1_r4_k3_a_successful_census_that_finds_no_live_ticket_after_the_release_is_an_empty_line_not_the_earlier_one(self) -> None:
        # r4 K3 (sol; r2 pass 3 A5 pinned the opposite): both tickets legitimately left a READABLE folder before the second census, so the line returned is empty and the caller's place is 0, in the released and in the five-BUSY
        # branch alike. Only a read that FAILED (the listing throws) keeps the earlier census (test below).
        r = self.give_back_scenario("p3a5", "reapall", {"SEAM_REAP_ALL": "1"})
        self.assertEqual((r["state"], r["granted"]), ("QUEUED", False), r)
        self.assertEqual((r["queueCount"], r["position"]), (0, 0), f"a successful census of an empty line reported the earlier queue and place: {r}")
        r = self.give_back_scenario("k3busy", "denyreapall", {"SEAM_DENY_DELETE": "1", "SEAM_REAP_ALL_IN_LOOP": "1"})
        self.assertEqual((r["state"], r["granted"]), ("QUEUED", False), r)
        self.assertEqual((r["queueCount"], r["position"]), (0, 0), f"the five-BUSY give-back reported the earlier queue and place after an empty census: {r}")
        self.assertTrue(r["pending"], f"the claim that could not be given back is not named on the ticket: {r}")

    def test_tail1_r4_k3_a_census_whose_listing_failed_keeps_the_earlier_one(self) -> None:
        # r4 K3 guard: the listing that cannot be read (here it throws) is a failed read, never an empty line: the earlier census stands, for both branches
        r = self.give_back_scenario("k3fail", "reapallfail", {"SEAM_REAP_ALL": "1"})
        self.assertEqual((r["state"], r["granted"]), ("QUEUED", False), r)
        self.assertGreaterEqual(r["queueCount"], 1, f"a failed listing replaced the census: {r}")
        r = self.give_back_scenario("k3failbusy", "denyreapallfail", {"SEAM_DENY_DELETE": "1", "SEAM_REAP_ALL_IN_LOOP": "1"})
        self.assertEqual((r["state"], r["granted"]), ("QUEUED", False), r)
        self.assertGreaterEqual(r["queueCount"], 1, f"a failed listing replaced the census in the five-BUSY branch: {r}")

    def test_tail1_r2_a10_a_give_back_whose_delete_is_refused_does_not_hand_the_claim_to_a_caller_that_is_not_the_head(self) -> None:
        # r1 adversary A10 (pre-existing): a Release-VenueClaim that THROWS (File.Delete refused) left the give-back through the outer catch and the grant was returned as TAKEN to B although the older A is ready. It counts
        # as a release that did not let go: five tries, then QUEUED with the claim's nonce on B's ticket (the next poll tries again).
        r = self.give_back_scenario("a10", "deny", {"SEAM_DENY_DELETE": "1"})
        self.assertEqual((r["state"], r["granted"]), ("QUEUED", False), f"a younger claimant was handed a claim owed to an older ready ticket: {r}")
        self.assertTrue(r["pending"], f"the claim that could not be given back is not named on the ticket: {r}")

    def test_r9_a4_a_phase_with_a_character_the_culture_compare_ignores_is_refused_not_stored(self) -> None:
        # r9 A4: [ValidateSet('waiting', 'cooldown')] compares with the current culture and ignores U+00AD / U+200D, so 'wait<U+00AD>ing' bound and was stored with the extra character: a ticket no ordinal reader
        # knows as ready, so a younger waiter overtook it. Both writers (New-VenueTicket, Update-VenueTicket) refuse it; the canonical spelling and a different case still store the canonical lower-case word.
        r = self.ps("""
$A = New-VenueTicket -Venue bachelor -RunDir 'run-A' -ClaimDir $d -Phase cooldown -StaleAfterSec 600
$cases = [ordered]@{ softhyphen = ('wait' + [char]0x00AD + 'ing'); zwj = ('wait' + [char]0x200D + 'ing'); upper = 'WAITING'; mixed = 'Cooldown' }
$out = [ordered]@{}
foreach ($k in $cases.Keys) {
  $made = $null; $refused = $false
  try { $made = New-VenueTicket -Venue bachelor -RunDir ('run-' + $k) -ClaimDir $d -Phase $cases[$k] } catch { $refused = $true }
  $stored = $null; if ($made) { $stored = [string](Get-Content -LiteralPath $made.path -Raw | ConvertFrom-Json).phase }
  $out["new_$k"] = [pscustomobject]@{ refused = $refused; stored = $stored }
}
$upd = $false; try { $null = Update-VenueTicket -Ticket $A -Phase ('cool' + [char]0x00AD + 'down') } catch { $upd = $true }
$out['update_refused'] = $upd
$out['update_phase_after'] = [string](Get-Content -LiteralPath $A.path -Raw | ConvertFrom-Json).phase
$out['update_upper'] = [string](Update-VenueTicket -Ticket $A -Phase 'WAITING')
$out['update_upper_phase'] = [string](Get-Content -LiteralPath $A.path -Raw | ConvertFrom-Json).phase
$out | ConvertTo-Json -Compress -Depth 4
""")
        for k in ("softhyphen", "zwj"):
            self.assertEqual(r[f"new_{k}"], {"refused": True, "stored": None}, f"a phase with an ignored character was accepted by New-VenueTicket: {r}")
        self.assertEqual(r["new_upper"], {"refused": False, "stored": "waiting"}, r)
        self.assertEqual(r["new_mixed"], {"refused": False, "stored": "cooldown"}, r)
        self.assertTrue(r["update_refused"], f"Update-VenueTicket accepted a phase with an ignored character: {r}")
        self.assertEqual(r["update_phase_after"], "cooldown", r)
        self.assertEqual((r["update_upper"], r["update_upper_phase"]), ("OK", "waiting"), r)

    BUDGET_PATH_SEAM = """
$A = New-VenueTicket -Venue bachelor -RunDir 'run-A' -ClaimDir $d -Phase cooldown -StaleAfterSec 600
$env:SEAM_LOCK = '@@LOCK@@'; $env:SEAM_READ = '@@READ@@'
$r = Update-VenueTicket -Ticket $A -Phase 'waiting' -BudgetSec 0.2
$phase = (Get-Content -LiteralPath $A.path -Raw | ConvertFrom-Json).phase
[pscustomobject]@{ result = [string]$r; phase = $phase; lock = (Test-Path -LiteralPath '@@LOCK@@'); read = (Test-Path -LiteralPath '@@READ@@') } | ConvertTo-Json -Compress
"""

    def test_r9_a5_no_lock_attempt_starts_after_a_slow_path_check_spent_the_budget(self) -> None:
        # r9 A5: the budget was asked at the top of an attempt, but the path check right before the lock can itself be slow on a share; the lock attempt then started past the budget.
        mod = self.seam_text(("if (-not (Test-Path -LiteralPath $Ticket.path -PathType Leaf)) { return 'LOST' }   # no lock for a ticket that is not there", "if (-not (Test-Path -LiteralPath $Ticket.path -PathType Leaf)) { return 'LOST' }; Start-Sleep -Milliseconds 400   # no lock for a ticket that is not there"),
                             ("        $lock = Enter-VenueTicketLock -TicketPath $Ticket.path", "        [IO.File]::WriteAllText($env:SEAM_LOCK, '1'); $lock = Enter-VenueTicketLock -TicketPath $Ticket.path"))
        r = self.ps(self.BUDGET_PATH_SEAM.replace("@@LOCK@@", str(self.case / "seam-a5-lock.txt")).replace("@@READ@@", str(self.case / "seam-a5-read.txt")), module=mod)
        self.assertFalse(r["lock"], f"a lock attempt started after the budget ended in the path check: {r}")
        self.assertEqual((r["result"], r["phase"]), ("BUSY", "cooldown"), r)

    def test_r9_a5_no_ticket_read_starts_after_a_slow_path_check_under_the_lock_spent_the_budget(self) -> None:
        mod = self.seam_text(("            if (-not (Test-Path -LiteralPath $Ticket.path -PathType Leaf)) { return 'LOST' }\n", "            if (-not (Test-Path -LiteralPath $Ticket.path -PathType Leaf)) { return 'LOST' }; Start-Sleep -Milliseconds 400\n"),
                             ("$fs = [IO.File]::Open($Ticket.path, [IO.FileMode]::Open, [IO.FileAccess]::Read,", "[IO.File]::WriteAllText($env:SEAM_READ, '1'); $fs = [IO.File]::Open($Ticket.path, [IO.FileMode]::Open, [IO.FileAccess]::Read,"))
        r = self.ps(self.BUDGET_PATH_SEAM.replace("@@LOCK@@", str(self.case / "seam-a5b-lock.txt")).replace("@@READ@@", str(self.case / "seam-a5b-read.txt")), module=mod)
        self.assertFalse(r["read"], f"the ticket was read after the budget ended in the path check under the lock: {r}")
        self.assertEqual((r["result"], r["phase"]), ("BUSY", "cooldown"), r)

    SEAM_A7 = """
$X = New-VenueTicket -Venue bachelor -RunDir 'run-X' -ClaimDir $d -Phase cooldown -StaleAfterSec 600 -OwnerPid @@S@@
Start-Sleep -Milliseconds 30
$A = New-VenueTicket -Venue bachelor -RunDir 'run-A' -ClaimDir $d -Phase cooldown -StaleAfterSec 600
Start-Sleep -Milliseconds 30
$B = New-VenueTicket -Venue bachelor -RunDir 'run-B' -ClaimDir $d
$job = Start-Job -ArgumentList $B, $d, '@@SEAMMOD@@', '@@MARK@@' -ScriptBlock { param($B, $d, $m, $mark) $env:SEAM_MARK = $mark; Import-Module $m -Force -DisableNameChecking; Request-VenueClaim -Venue bachelor -RunDir 'run-B' -ClaimDir $d -Ticket $B }
$t0 = [DateTime]::UtcNow
while (-not (Test-Path -LiteralPath '@@MARK@@') -and ([DateTime]::UtcNow - $t0).TotalSeconds -lt 60) { Start-Sleep -Milliseconds 50 }
$gone = [string](Remove-VenueTicket -Ticket $X)
$flip = Update-VenueTicket -Ticket $A -Phase 'waiting'
$r = Receive-Job $job -Wait -AutoRemoveJob
$mine = @($r.queue | Where-Object { $_.name -ceq $B.name })
[pscustomobject]@{ gone = $gone; flip = [string]$flip; state = [string]$r.state; position = [int]$r.position; queueLen = @($r.queue).Count; truePosition = $(if ($mine.Count) { [int]$mine[0].position } else { 0 }) } | ConvertTo-Json -Compress
"""

    def test_r8_rule7_the_position_of_a_given_back_claim_is_the_one_in_the_returned_queue(self) -> None:
        # r8 rule 7 (r7 pass 3 A11): the give-back returned the census made after it as the queue but the position of the snapshot before it (a runner could log 3/2).
        s = self.sleeper()
        mark = self.case / "seam-a7-mark.txt"
        mod = self.seam_module("    $r = Invoke-VenueClaimAttempt @attemptArgs\n", "    [IO.File]::WriteAllText($env:SEAM_MARK, '1'); Start-Sleep -Milliseconds 2500\n    $r = Invoke-VenueClaimAttempt @attemptArgs\n")
        r = self.ps(self.SEAM_A7.replace("@@S@@", str(s.pid)).replace("@@SEAMMOD@@", str(mod)).replace("@@MARK@@", str(mark)))
        self.assertEqual((r["gone"], r["flip"], r["state"]), ("REMOVED", "OK", "QUEUED"), r)
        self.assertEqual(r["position"], r["truePosition"], f"the position is not the caller's place in the queue it returned: {r}")
        self.assertLessEqual(r["position"], r["queueLen"], r)


if __name__ == "__main__":
    unittest.main()
