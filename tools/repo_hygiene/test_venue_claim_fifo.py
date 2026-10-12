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

DV = Path(os.environ["VENUE_CLAIM_FIFO_DV_DIR"]) if os.environ.get("VENUE_CLAIM_FIFO_DV_DIR") else base.DV
RUNNER = DV / "Invoke-VenueChain.ps1"
MODULE = DV / "VenueChain.psm1"
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
        mut = Path(tempfile.mkdtemp(prefix="mut-", dir=self.case)) / MODULE.name
        text = MODULE.read_text(encoding="utf-8")
        anchor = "if ($position -ne 1) {"
        self.assertEqual(text.count(anchor), 1, "mutation anchor moved")
        mut.write_text(text.replace(anchor, "if ($false) {"), encoding="utf-8")
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
        (d / MODULE.name).write_text(MODULE.read_text(encoding="utf-8"), encoding="utf-8")
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
        mut = Path(tempfile.mkdtemp(prefix="mut-", dir=self.case)) / MODULE.name
        text = MODULE.read_text(encoding="utf-8")
        anchor = "Sort-Object -Property key, pidKey, startKey, name"
        self.assertEqual(text.count(anchor), 1, "mutation anchor moved")
        mut.write_text(text.replace(anchor, "Sort-Object -Property key"), encoding="utf-8")
        tick = "0000000000000000100"
        got = self.tie_break_order([f"{tick}-p9-5.ticket.json", f"{tick}-p10-5.ticket.json"], module=mut)
        self.assertEqual(got, [f"{tick}-p10-5.ticket.json", f"{tick}-p9-5.ticket.json"], "the mutant orders by file name text: p10 before p9")

    def test_the_contract_is_documented_in_both_headers(self) -> None:
        mod = MODULE.read_text(encoding="utf-8")
        run = RUNNER.read_text(encoding="utf-8")
        for needle in ("TICKET QUEUE (VENUE-CLAIM-FIFO-1)", "BACK-COMPAT", "Ticketing orders ONLY ticket holders", "IO.FileMode]::CreateNew"):
            self.assertIn(needle, mod)
        for needle in ("2b. FIFO (VENUE-CLAIM-FIFO-1)", "-NoTicket", "-ClaimStatus", "-TicketPollSec", "-TicketKeepMax", "-ProbeAdmissionWaitSec"):
            self.assertIn(needle, run)
        for f in (RUNNER, MODULE):
            self.assertTrue(all(b < 128 for b in f.read_bytes()), f"{f.name} is not ASCII")


if __name__ == "__main__":
    unittest.main()
