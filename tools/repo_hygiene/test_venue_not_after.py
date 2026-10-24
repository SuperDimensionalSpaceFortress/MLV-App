"""Tests for LEASE-NOT-AFTER-SINGLE-LEG-SUBMITTERS-1: the single-leg submitter (tools/profiling/dual-venue/Invoke-VenueLeg.ps1) passes um-run.ps1 a
-NotAfterUtc that matches how long it waits for the result, computed by ONE pure helper (DualVenueRunner.psm1 Get-DvJobNotAfter) that the stage driver
(Invoke-VenueStageNightly.ps1, pinned in test_venue_stage_nightly.py) calls too.

notAfterUtc (LEASE-REQUEST-CHILD-NOT-AFTER-1, #644) is the instant after which the Bachelor agent never leases or launches the job. A submitter must not set
it earlier than it still waits (the job would be refused while someone wants it), nor later than the last reader of the result (the job would take a lease
after everyone has gone). WHAT IS PINNED
  * the helper's bounds: the sum of um-run's four waits plus a small slack when the caller has no kill bound; the caller's cap (the stage driver's remaining
    -MaxTotalSec) when it has one; never earlier than any single wait it passes; 'o' UTC text ending in Z, later than now; none for a later harvest;
  * the admission wait the helper assumes is um-run.ps1's own default, read from um-run.ps1's param block (so the two cannot drift);
  * end to end, offline: both Invoke-VenueLeg submits (the health probe and the leg) reach the um-run stub with that -NotAfterUtc and with the admission
    wait it assumed passed by name.

LEASE-NOT-AFTER-SUBMITTER-DEADLINE-TIMING-1 (CoS Dot 07bd1383 on #650) adds WHERE THE BOUND COUNTS FROM. um-run.ps1 writes notAfterUtc into the claim inside
its own drop and starts its waits only after it, so no submitter can compute the bound after the drop (um-run is not changed here). The bound therefore
carries its anchor (the instant it counts from) and the drop delay it tolerates, and the drop is MEASURED:
  * the delayed-drop fixture: Test-DvNotAfterCoversDrop says covered exactly when notAfterUtc >= the drop + every um-run wait, for drops inside, at and
    past the tolerance; an unmeasured drop is never covered; a caller that kills um-run at the bound (the stage driver) is covered whenever the job appears;
  * the drop instant is um-run's own 'submitted <id> -> ...' line (printed after the job file is in place), read from the information record's time;
  * end to end: a slow drop is measured and still covered; a drop later than the tolerance (its record stamped later through the stub) is named
    DVE_NOT_AFTER_DROP_LATE, never silent;
  * the scheduling-delay fixture for the caller deadline (-CallerDeadlineUtc): a delay between the caller's remaining-budget sample and the call only ever
    shortens the bound, never past the deadline (re-deriving it from the sample plus a later now overshoots by the delay).

r2 (hub ruling on sol's r1 blocker: um-run.ps1 and UmRunDrop.psm1 are in this repo, so the a priori bound belongs where meta.json is written). The submitters
pass the bound DROP-RELATIVE (um-run.ps1 -NotAfterBudgetSec, optionally capped by -NotAfterCapUtc), and the drop writes min(its own now + budget, cap):
  * sol's repro through the REAL drop (UmRunDrop.psm1 Invoke-UmRunDrop on a temp inbox, its clock injected): anchor 05:00:00Z, waits 120/120/7200/60, a 300 s
    delay before the drop -> notAfterUtc >= drop + every wait (r1's absolute value fell 240 s short); a cap is never exceeded however late the drop; a cap
    already passed (or passing before the claim) refuses before any share write; the absolute -NotAfterUtc form is byte-for-byte unchanged; both forms is a
    typed error; um-run.ps1 forwards the budget;
  * the witness (Test-DvNotAfterCoversDrop) judges the value the drop reported (um-run's 'job not-after:' line): covered for every drop delay on the uncapped
    path, and DVE_NOT_AFTER_DROP_LATE only for a drop that did not count the budget from itself (a defect signal);
  * end to end: a drop far later than the slack is still covered, never named late; a defective drop is named before the outcome.

r3 (sol r2 blocker): the clock read at the claim still came before the side-file and job copies, so slow share placement took time out of the waits. The
drop now reads its clock again immediately before the job's rename and re-publishes the re-derived value into its own claim first. PlacementDelayNotAfterTests
drives the real drop with the existing Copier seam moving a simulated clock on during the copies (sol's repro: 300 s during the job copy): the meta.json the
agent can read at visibility covers visibility + every wait, never past the cap; a cap passing during placement refuses with the job never visible.

Offline only: the leg runs in -OfflineTestMode with a stub um-run (Invoke-VenueLeg refuses the real um-run.ps1 there), through the shared RunnerHarness of
test_dual_venue_evidence.py. Nothing reaches a venue. Windows-only (PowerShell tools).
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
RUNNER_MODULE = DV / "DualVenueRunner.psm1"
UM_RUN = ROOT / "tools" / "profiling" / "um-run.ps1"
DROP_MODULE = ROOT / "tools" / "profiling" / "UmRunDrop.psm1"
PWSH = shutil.which("pwsh")
requires_windows_pwsh = unittest.skipIf(PWSH is None or sys.platform != "win32", "needs pwsh on Windows")

O_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{7}Z$")
SLACK_SEC = 60          # Get-DvJobNotAfter's slack for an uncapped caller (the drop and one um-run poll)
UMRUN_ADMISSION_DEFAULT = 7200


def parse_o(text: str) -> datetime:
    """A .NET round-trip UTC string ('2026-10-24T05:00:00.1234567Z') as an aware datetime (Python keeps 6 fraction digits)."""
    m = re.match(r"^(.*T\d{2}:\d{2}:\d{2})\.(\d{7})Z$", text)
    assert m, text
    return datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc) + timedelta(microseconds=int(m.group(2)[:6]))


def utc_o(dt: datetime) -> str:
    """An aware datetime as .NET round-trip UTC text (7 fraction digits, Z)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "0Z"


def run_helper_cases(cases: list[dict]) -> list[dict]:
    """Run each case through Get-DvJobNotAfter in ONE pwsh: every case is a dict of its named parameters (NowUtc as 'o' text); each answer is the helper's
    result, or {'error': <message>} when it threw."""
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        f"Import-Module '{RUNNER_MODULE}' -Force -DisableNameChecking\n"
        "$cases = [Console]::In.ReadToEnd() | ConvertFrom-Json\n"
        "$out = foreach ($c in $cases) {\n"
        "  $p = @{}\n"
        "  foreach ($prop in $c.PSObject.Properties) {\n"
        # (pwsh 7's ConvertFrom-Json may already have made the ISO text a [DateTime]: pass that as it is, never re-parsed from its culture text)
        "    if ($prop.Name -in @('NowUtc', 'CallerDeadlineUtc')) { $p[$prop.Name] = $(if ($prop.Value -is [datetime]) { $prop.Value } else { [DateTime]::Parse([string]$prop.Value, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind) }) }\n"
        "    elseif ($prop.Name -eq 'LaterHarvest') { $p['LaterHarvest'] = [bool]$prop.Value }\n"
        "    else { $p[$prop.Name] = [int]$prop.Value }\n"
        "  }\n"
        "  try { $r = Get-DvJobNotAfter @p; [ordered]@{ notAfterUtc = [string]$r.notAfterUtc; source = $r.source; boundSec = $r.boundSec; waitSec = $r.waitSec;\n"
        "        longestWaitSec = $r.longestWaitSec; maxAdmissionWaitSec = $r.maxAdmissionWaitSec; anchorUtc = [string]$r.anchorUtc;\n"
        "        dropToleranceSec = $r.dropToleranceSec; budgetSec = $r.budgetSec; capUtc = [string]$r.capUtc; why = $r.why } }\n"
        "  catch { [ordered]@{ error = [string]$_.Exception.Message } }\n"
        "}\n"
        "ConvertTo-Json -InputObject @($out) -Depth 4 -Compress\n"
    )
    p = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], input=json.dumps(cases), capture_output=True, text=True,
                       timeout=120)
    assert p.returncode == 0, p.stdout + p.stderr
    answers = json.loads(p.stdout.strip().splitlines()[-1])
    return answers if isinstance(answers, list) else [answers]


def run_pwsh_json(script: str, payload: object) -> list[dict]:
    """Run a script (the runner module already imported) that reads the JSON payload from stdin and prints ONE compressed JSON array as its last line."""
    full = "$ErrorActionPreference = 'Stop'\n" f"Import-Module '{RUNNER_MODULE}' -Force -DisableNameChecking\n" + script
    p = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", full], input=json.dumps(payload), capture_output=True, text=True,
                       timeout=120)
    assert p.returncode == 0, p.stdout + p.stderr
    answers = json.loads(p.stdout.strip().splitlines()[-1])
    return answers if isinstance(answers, list) else [answers]


def run_cover_cases(cases: list[dict]) -> list[dict]:
    """Each case: {'bound': <Get-DvJobNotAfter parameters, NowUtc as 'o' text>, 'dropAfterSec': <seconds after the bound's anchor, or None>,
    'dropReports': 'relative' (default) | 'anchor' | None}. The drop is placed that many seconds after the anchor (the delayed-drop seam) and
    Test-DvNotAfterCoversDrop judges it. For a drop-relative bound (budgetSec set) the value the drop reports is: 'relative', what UmRunDrop.psm1 writes,
    min(drop + budgetSec, capUtc), with the drop's clock read at the drop; 'anchor', a DEFECTIVE drop that counted the budget from the bound's anchor (r1's
    absolute value); None, no 'job not-after:' line. r6 (Grok G7): 'sampledAfterSec' (optional, 'relative' only) puts the drop's clock read that many
    seconds after the anchor instead of at the drop, the value counted from it: an honest drop whose 'submitted' line came later (a stall after
    visibility). Each answer is the judge's result."""
    script = (
        "$cases = [Console]::In.ReadToEnd() | ConvertFrom-Json\n"
        "$o = { param($t) [DateTime]::Parse([string]$t, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime() }\n"
        "$out = foreach ($c in $cases) {\n"
        "  $p = @{}\n"
        "  foreach ($prop in $c.bound.PSObject.Properties) {\n"
        "    if ($prop.Name -in @('NowUtc', 'CallerDeadlineUtc')) { $p[$prop.Name] = $(if ($prop.Value -is [datetime]) { $prop.Value } else { [DateTime]::Parse([string]$prop.Value, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind) }) }\n"
        "    elseif ($prop.Name -eq 'LaterHarvest') { $p['LaterHarvest'] = [bool]$prop.Value }\n"
        "    else { $p[$prop.Name] = [int]$prop.Value }\n"
        "  }\n"
        "  try {\n"
        "    $b = Get-DvJobNotAfter @p\n"
        "    $drop = $(if ($null -eq $c.dropAfterSec) { $null } else { (& $o $b.anchorUtc).AddSeconds([double]$c.dropAfterSec) })\n"
        "    $mode = $(if ($c.PSObject.Properties.Name -contains 'dropReports') { $c.dropReports } else { 'relative' })\n"
        "    $dna = $null\n"
        "    if ($null -ne $b.budgetSec -and $null -ne $mode -and $null -ne $drop) {\n"
        "      $read = $(if ($c.PSObject.Properties.Name -contains 'sampledAfterSec') { (& $o $b.anchorUtc).AddSeconds([double]$c.sampledAfterSec) } else { $drop })\n"
        "      $v0 = $(if ($mode -eq 'anchor') { & $o $b.notAfterUtc } else { $read.AddSeconds([double]$b.budgetSec) })\n"
        "      if ($mode -ne 'anchor' -and $b.capUtc -and (& $o $b.capUtc) -lt $v0) { $v0 = & $o $b.capUtc }\n"
        "      $dna = [pscustomobject]@{ notAfterUtc = $v0; sampledUtc = $read; budgetSec = $b.budgetSec; capUtc = [string]$b.capUtc }\n"
        "    }\n"
        "    $v = Test-DvNotAfterCoversDrop -NotAfter $b -DropVisibleUtc $drop -DropNotAfter $dna -JobId 'job-x'\n"
        "    [ordered]@{ jobId = $v.jobId; notAfterUtc = [string]$v.notAfterUtc; form = $v.form; boundNotAfterUtc = [string]$b.notAfterUtc; source = $v.source;\n"
        "      anchorUtc = [string]$v.anchorUtc; waitSec = $v.waitSec; budgetSec = $v.budgetSec; capUtc = [string]$v.capUtc; dropToleranceSec = $v.dropToleranceSec;\n"
        "      dropSampledUtc = [string]$v.dropSampledUtc; dropVisibleUtc = [string]$v.dropVisibleUtc; dropDelaySec = $v.dropDelaySec; marginSec = $v.marginSec;\n"
        "      covered = $v.covered; why = $v.why }\n"
        "  } catch { [ordered]@{ error = [string]$_.Exception.Message } }\n"
        "}\n"
        "ConvertTo-Json -InputObject @($out) -Depth 4 -Compress\n"
    )
    return run_pwsh_json(script, cases)


@requires_windows_pwsh
class NotAfterHelperTests(unittest.TestCase):
    NOW = "2026-10-24T05:00:00.0000000Z"

    def one(self, **params) -> dict:
        params.setdefault("NowUtc", self.NOW)
        return run_helper_cases([params])[0]

    def assert_ok(self, r: dict) -> None:
        self.assertNotIn("error", r, r)

    def test_the_module_exports_the_helper(self) -> None:
        r = self.one(TimeoutSec=120, MaxQueueWaitSec=120, MaxClaimedWaitSec=60)
        self.assertNotIn("is not recognized", r.get("error", ""), "DualVenueRunner.psm1 must define and export Get-DvJobNotAfter")
        self.assert_ok(r)

    def test_an_uncapped_caller_gets_the_sum_of_the_four_waits_plus_the_slack(self) -> None:
        # Invoke-VenueLeg's health probe: -TimeoutSec 120 -MaxQueueWaitSec 120 -MaxClaimedWaitSec 60 and um-run's default admission wait.
        r = self.one(TimeoutSec=120, MaxQueueWaitSec=120, MaxClaimedWaitSec=60)
        self.assert_ok(r)
        self.assertEqual(r["maxAdmissionWaitSec"], UMRUN_ADMISSION_DEFAULT)
        self.assertEqual(r["waitSec"], 120 + 120 + UMRUN_ADMISSION_DEFAULT + 60)
        self.assertEqual(r["boundSec"], r["waitSec"] + SLACK_SEC)
        self.assertEqual(r["source"], "um-run-waits")
        self.assertRegex(r["notAfterUtc"], O_UTC)
        self.assertEqual(parse_o(r["notAfterUtc"]), parse_o(self.NOW) + timedelta(seconds=r["boundSec"]))

    def test_an_explicit_admission_wait_is_used_and_echoed(self) -> None:
        r = self.one(TimeoutSec=720, MaxQueueWaitSec=900, MaxAdmissionWaitSec=600, MaxClaimedWaitSec=600)
        self.assert_ok(r)
        self.assertEqual((r["maxAdmissionWaitSec"], r["waitSec"], r["longestWaitSec"]), (600, 720 + 900 + 600 + 600, 900))

    def test_the_remaining_budget_case_is_bounded_by_the_callers_cap(self) -> None:
        # Invoke-VenueStageNightly: remain = 900 s left of -MaxTotalSec; um-run gets min(3600, remain), remain, min(7200, remain), remain and is KILLED at
        # now + remain, so nobody waits past the cap whatever the sum: notAfter is the driver's deadline, never later.
        remain = 900
        r = self.one(TimeoutSec=min(3600, remain), MaxQueueWaitSec=remain, MaxAdmissionWaitSec=min(7200, remain), MaxClaimedWaitSec=remain, CallerCapSec=remain)
        self.assert_ok(r)
        self.assertEqual((r["source"], r["boundSec"]), ("caller-cap", remain))
        self.assertEqual(parse_o(r["notAfterUtc"]), parse_o(self.NOW) + timedelta(seconds=remain))
        self.assertGreaterEqual(r["boundSec"], r["longestWaitSec"], "never earlier than a wait the caller passes")

    def test_a_cap_above_the_waits_leaves_the_uncapped_bound(self) -> None:
        # The client ends itself before the cap: the cap is not the last read, the waits are.
        r = self.one(TimeoutSec=30, MaxQueueWaitSec=30, MaxAdmissionWaitSec=30, MaxClaimedWaitSec=30, CallerCapSec=7200)
        self.assert_ok(r)
        self.assertEqual((r["source"], r["boundSec"]), ("um-run-waits", 120 + SLACK_SEC))

    def test_a_wait_longer_than_the_cap_is_refused(self) -> None:
        # A caller that passes um-run a wait it will not honour (it kills um-run first) has its arithmetic wrong: refused before anything is submitted.
        r = self.one(TimeoutSec=600, MaxQueueWaitSec=1200, MaxAdmissionWaitSec=600, MaxClaimedWaitSec=600, CallerCapSec=900)
        self.assertIn("DVE_NOT_AFTER_WAIT_OVER_CAP", r.get("error", ""), r)

    def test_a_later_harvest_gets_no_not_after(self) -> None:
        r = self.one(TimeoutSec=900, MaxQueueWaitSec=900, MaxAdmissionWaitSec=900, MaxClaimedWaitSec=900, CallerCapSec=900, LaterHarvest=True)
        self.assert_ok(r)
        self.assertEqual((r["notAfterUtc"], r["source"]), ("", "later-harvest"))

    def test_the_value_is_utc_z_and_later_than_now_for_a_local_now(self) -> None:
        local_now = datetime.now().astimezone().replace(microsecond=0)
        r = self.one(TimeoutSec=1, MaxQueueWaitSec=0, MaxAdmissionWaitSec=0, MaxClaimedWaitSec=0, NowUtc=local_now.isoformat())
        self.assert_ok(r)
        self.assertRegex(r["notAfterUtc"], O_UTC)
        self.assertEqual(parse_o(r["notAfterUtc"]), local_now.astimezone(timezone.utc) + timedelta(seconds=1 + SLACK_SEC))

    def test_the_default_now_is_the_wall_clock(self) -> None:
        before = datetime.now(timezone.utc)
        r = run_helper_cases([{"TimeoutSec": 10, "MaxQueueWaitSec": 10, "MaxAdmissionWaitSec": 10, "MaxClaimedWaitSec": 10}])[0]
        after = datetime.now(timezone.utc)
        self.assert_ok(r)
        na = parse_o(r["notAfterUtc"])
        self.assertGreaterEqual(na, before + timedelta(seconds=40 + SLACK_SEC) - timedelta(milliseconds=1))
        self.assertLessEqual(na, after + timedelta(seconds=40 + SLACK_SEC))

    def test_out_of_range_waits_are_refused(self) -> None:
        for bad in ({"TimeoutSec": 0, "MaxQueueWaitSec": 1, "MaxClaimedWaitSec": 1}, {"TimeoutSec": 1, "MaxQueueWaitSec": -1, "MaxClaimedWaitSec": 1},
                    {"TimeoutSec": 1, "MaxQueueWaitSec": 1, "MaxClaimedWaitSec": 86401}):
            with self.subTest(bad=bad):
                self.assertIn("Cannot validate argument", self.one(**bad).get("error", ""))

    def test_the_assumed_admission_wait_is_um_runs_own_default(self) -> None:
        text = UM_RUN.read_text(encoding="utf-8")
        m = re.search(r"(?m)^\s*\[int\]\$MaxAdmissionWaitSec = (\d+),\s*$", text)
        self.assertIsNotNone(m, "um-run.ps1 declares [int]$MaxAdmissionWaitSec = <n>")
        r = self.one(TimeoutSec=1, MaxQueueWaitSec=1, MaxClaimedWaitSec=1)
        self.assert_ok(r)
        self.assertEqual(r["maxAdmissionWaitSec"], int(m.group(1)), "Get-DvJobNotAfter's default admission wait must be um-run.ps1's default")

    def test_the_helper_is_pure_and_ascii(self) -> None:
        text = RUNNER_MODULE.read_text(encoding="utf-8")
        self.assertTrue(text.isascii())
        start = text.index("function Get-DvJobNotAfter {")
        body = text[start:text.index("\n}\n", start)]
        body = body[body.index("#>"):]   # the code, not its help text
        for verb in ("Set-Content", "Out-File", "WriteAll", "Test-Path", "Get-Content", "New-Item", "Remove-Item", "Start-Process", "Invoke-"):
            self.assertNotIn(verb, body, f"Get-DvJobNotAfter is pure arithmetic: {verb}")

    # ---------------------------------------------------------------- LEASE-NOT-AFTER-SUBMITTER-DEADLINE-TIMING-1: where the bound counts from
    def test_the_bound_carries_its_anchor_and_the_drop_delay_it_tolerates(self) -> None:
        # um-run counts its waits from its drop, which comes AFTER this call: the bound says which instant it counted from and how late the job may
        # become visible with every um-run wait still inside it (the slack, for an uncapped caller).
        r = self.one(TimeoutSec=120, MaxQueueWaitSec=120, MaxClaimedWaitSec=60)
        self.assert_ok(r)
        self.assertEqual(parse_o(r["anchorUtc"]), parse_o(self.NOW))
        self.assertEqual(r["dropToleranceSec"], r["boundSec"] - r["waitSec"])
        self.assertEqual(r["dropToleranceSec"], SLACK_SEC)
        # a caller that kills um-run at the bound waits no longer than the bound whenever the job appears: no drop tolerance applies
        capped = self.one(TimeoutSec=900, MaxQueueWaitSec=900, MaxAdmissionWaitSec=900, MaxClaimedWaitSec=900, CallerCapSec=900)
        self.assert_ok(capped)
        self.assertEqual(capped["source"], "caller-cap")
        self.assertIsNone(capped["dropToleranceSec"])

    def test_the_bound_says_its_drop_relative_form(self) -> None:
        # r2 (hub ruling): wherever the bound is not an absolute kill instant it is handed to um-run DROP-RELATIVE: budgetSec (-NotAfterBudgetSec) and capUtc
        # (-NotAfterCapUtc, the caller's deadline or ''). 'caller-cap' counts from the caller's own start and stays absolute; a later harvest has neither.
        uncapped = self.one(TimeoutSec=120, MaxQueueWaitSec=120, MaxClaimedWaitSec=60)
        self.assert_ok(uncapped)
        self.assertEqual((uncapped["source"], uncapped["budgetSec"], uncapped["capUtc"]), ("um-run-waits", uncapped["waitSec"] + SLACK_SEC, ""))
        far = utc_o(parse_o(self.NOW) + timedelta(seconds=90000))
        capped_far = self.one(TimeoutSec=120, MaxQueueWaitSec=120, MaxClaimedWaitSec=60, CallerDeadlineUtc=far)
        self.assert_ok(capped_far)
        self.assertEqual((capped_far["source"], capped_far["budgetSec"], capped_far["capUtc"]), ("um-run-waits", capped_far["waitSec"] + SLACK_SEC, far),
                         "a late drop could push drop + budget past the deadline, so the cap travels even when the deadline is beyond the waits")
        near = utc_o(parse_o(self.NOW) + timedelta(seconds=900))
        deadline = self.one(TimeoutSec=900, MaxQueueWaitSec=900, MaxAdmissionWaitSec=900, MaxClaimedWaitSec=900, CallerCapSec=900, CallerDeadlineUtc=near)
        self.assert_ok(deadline)
        self.assertEqual((deadline["source"], deadline["budgetSec"], deadline["capUtc"]), ("caller-deadline", 4 * 900 + SLACK_SEC, near))
        cap = self.one(TimeoutSec=900, MaxQueueWaitSec=900, MaxAdmissionWaitSec=900, MaxClaimedWaitSec=900, CallerCapSec=900)
        self.assert_ok(cap)
        self.assertEqual((cap["source"], cap["budgetSec"], cap["capUtc"]), ("caller-cap", None, ""))
        later = self.one(TimeoutSec=900, MaxQueueWaitSec=900, MaxClaimedWaitSec=900, LaterHarvest=True)
        self.assert_ok(later)
        self.assertEqual((later["budgetSec"], later["capUtc"]), (None, ""))

    def test_the_drop_judge_and_the_record_reader_are_pure_and_ascii(self) -> None:
        text = RUNNER_MODULE.read_text(encoding="utf-8")
        for fn in ("Test-DvNotAfterCoversDrop", "Get-DvUmRunDropVisibleUtc", "Get-DvUmRunDropNotAfter"):
            with self.subTest(fn=fn):
                start = text.index(f"function {fn} {{")
                body = text[start:text.index("\n}\n", start)]
                body = body[body.index("#>"):]
                for verb in ("Set-Content", "Out-File", "WriteAll", "Test-Path", "Get-Content", "New-Item", "Remove-Item", "Start-Process", "Invoke-", "UtcNow",
                             "Get-Date"):
                    self.assertNotIn(verb, body, f"{fn} is pure: {verb}")
        export = text[text.index("Export-ModuleMember -Function"):]
        self.assertIn("Test-DvNotAfterCoversDrop", export)
        self.assertIn("Get-DvUmRunDropVisibleUtc", export)
        self.assertIn("Get-DvUmRunDropNotAfter", export)


@requires_windows_pwsh
class DelayedDropTests(unittest.TestCase):
    """The delayed-drop fixture (Dot 07bd1383 (a)): the job file becomes visible some time AFTER the bound was computed (um-run's own setup and share I/O).
    The drop is injected at a chosen delay after the bound's anchor; nothing reaches a share."""
    NOW = "2026-10-24T05:00:00.0000000Z"
    UNCAPPED = {"TimeoutSec": 720, "MaxQueueWaitSec": 60, "MaxClaimedWaitSec": 60}   # Invoke-VenueLeg's leg (um-run's default admission wait)

    def cover(self, bound: dict, drop_after: float | None) -> dict:
        b = dict(bound)
        b.setdefault("NowUtc", self.NOW)
        r = run_cover_cases([{"bound": b, "dropAfterSec": drop_after}])[0]
        self.assertNotIn("error", r, r)
        return r

    def test_a_drop_relative_bound_covers_every_wait_however_late_the_drop(self) -> None:
        # r2 (hub ruling on sol's r1 blocker): the drop counts the budget from its own clock, so the uncapped path is covered for EVERY drop delay -- r1
        # accepted covered=false past the 60 s slack; that is now a defect, never an expected outcome.
        cases = [0, 2.5, 30, 59.9, 60, 60.5, 61, 300, 3600]
        answers = run_cover_cases([{"bound": dict(self.UNCAPPED, NowUtc=self.NOW), "dropAfterSec": d} for d in cases])
        for d, r in zip(cases, answers):
            with self.subTest(dropAfterSec=d):
                self.assertNotIn("error", r, r)
                self.assertEqual(r["form"], "drop-relative")
                na = parse_o(r["notAfterUtc"])
                drop = parse_o(r["dropVisibleUtc"])
                # (.NET's AddSeconds(double) can land one tick short of a decimal fraction)
                self.assertLess(abs(drop - (parse_o(self.NOW) + timedelta(seconds=d))), timedelta(milliseconds=1))
                self.assertAlmostEqual(r["dropDelaySec"], d, places=3)
                waits = timedelta(seconds=r["waitSec"])
                # item 1: notAfterUtc never earlier than the moment the job is visible plus every um-run wait
                self.assertGreaterEqual(na, drop + waits, r)
                self.assertTrue(r["covered"], r)
                self.assertAlmostEqual(r["marginSec"], SLACK_SEC, places=3)
                self.assertNotIn("DVE_NOT_AFTER_DROP_LATE", r["why"])

    def test_covered_exactly_when_not_after_is_at_least_the_drop_plus_every_wait(self) -> None:
        # The judge itself, fed a DEFECTIVE drop that counted the budget from the bound's anchor (r1's absolute value): covered exactly when notAfterUtc >=
        # drop + every wait, and DVE_NOT_AFTER_DROP_LATE names the defect otherwise. This is the witness that must never fire on the real drop.
        cases = [0, 2.5, 30, 59.9, 60, 60.5, 61, 300, 3600]
        answers = run_cover_cases([{"bound": dict(self.UNCAPPED, NowUtc=self.NOW), "dropAfterSec": d, "dropReports": "anchor"} for d in cases])
        for d, r in zip(cases, answers):
            with self.subTest(dropAfterSec=d):
                self.assertNotIn("error", r, r)
                na = parse_o(r["notAfterUtc"])
                self.assertEqual(na, parse_o(r["boundNotAfterUtc"]))
                drop = parse_o(r["dropVisibleUtc"])
                waits = timedelta(seconds=r["waitSec"])
                self.assertEqual(r["covered"], na >= drop + waits, r)
                self.assertAlmostEqual(r["marginSec"], (na - (drop + waits)).total_seconds(), places=3)
                self.assertEqual(r["covered"], d <= SLACK_SEC)
                if not r["covered"]:
                    self.assertIn("DVE_NOT_AFTER_DROP_LATE (defect", r["why"])

    def test_grok_g7_a_stall_after_visibility_longer_than_the_slack_is_no_false_alarm(self) -> None:
        # r6 (Grok G7 on 4ea77b88): the drop reads its clock (sampledUtc) and makes the job visible; um-run's 'submitted' line comes d s later (the drop's
        # return, a stalled share). um-run r5 counts its queue and admission waits from sampledUtc, so no wait moved and the value covers them all. At
        # 4ea77b88 the witness judged from the 'submitted' line and named every d > the slack DVE_NOT_AFTER_DROP_LATE (marginSec = slack - d): a false alarm.
        cases = [0, 30, 61, 300, 3600]
        answers = run_cover_cases([{"bound": dict(self.UNCAPPED, NowUtc=self.NOW), "dropAfterSec": d, "sampledAfterSec": 0} for d in cases])
        for d, r in zip(cases, answers):
            with self.subTest(dropAfterSec=d):
                self.assertNotIn("error", r, r)
                self.assertEqual(parse_o(r["dropSampledUtc"]), parse_o(self.NOW))
                self.assertAlmostEqual(r["dropDelaySec"], d, places=3)
                self.assertTrue(r["covered"], r)
                self.assertAlmostEqual(r["marginSec"], SLACK_SEC, places=3)
                self.assertGreaterEqual(parse_o(r["notAfterUtc"]), parse_o(r["dropSampledUtc"]) + timedelta(seconds=r["waitSec"]))
                self.assertNotIn("DVE_NOT_AFTER_DROP_LATE", r["why"])
        # the witness stays a witness: a value counted from earlier than the read the drop reports is still named, by its distance from that read
        defect = run_cover_cases([{"bound": dict(self.UNCAPPED, NowUtc=self.NOW), "dropAfterSec": 300, "sampledAfterSec": 300, "dropReports": "anchor"}])[0]
        self.assertNotIn("error", defect, defect)
        self.assertFalse(defect["covered"], defect)
        self.assertAlmostEqual(defect["marginSec"], SLACK_SEC - 300, places=3)
        self.assertIn("DVE_NOT_AFTER_DROP_LATE (defect: the drop did not count the budget from the clock read it reports", defect["why"])

    def test_an_unmeasured_drop_is_never_covered(self) -> None:
        r = self.cover(self.UNCAPPED, None)
        self.assertIsNone(r["covered"], "unknown is never covered")
        self.assertEqual(r["dropVisibleUtc"], "")
        self.assertIn("no notAfterUtc", r["why"])
        # a measured drop whose value um-run never reported is unknown too, never covered
        b = dict(self.UNCAPPED, NowUtc=self.NOW)
        unreported = run_cover_cases([{"bound": b, "dropAfterSec": 5, "dropReports": None}])[0]
        self.assertNotIn("error", unreported, unreported)
        self.assertIsNone(unreported["covered"], unreported)
        self.assertEqual(unreported["notAfterUtc"], "")
        self.assertIn("no 'job not-after:' line", unreported["why"])

    def test_a_drop_relative_value_at_its_cap_is_covered_whenever_the_job_appears(self) -> None:
        # The deadline lies beyond the waits (source um-run-waits, capped): a drop late enough that drop + budget passes the deadline gets the deadline, and
        # the caller kills um-run there, so nobody waits past it.
        deadline = utc_o(parse_o(self.NOW) + timedelta(seconds=9000))
        b = dict(self.UNCAPPED, NowUtc=self.NOW, CallerDeadlineUtc=deadline)
        for d in (0, 600, 3600):
            with self.subTest(dropAfterSec=d):
                r = run_cover_cases([{"bound": b, "dropAfterSec": d}])[0]
                self.assertNotIn("error", r, r)
                self.assertEqual(r["source"], "um-run-waits")
                self.assertEqual(r["capUtc"], deadline)
                self.assertLessEqual(parse_o(r["notAfterUtc"]), parse_o(deadline), "never past the cap")
                self.assertTrue(r["covered"], r)
        late = run_cover_cases([{"bound": b, "dropAfterSec": 3600}])[0]
        self.assertEqual(parse_o(late["notAfterUtc"]), parse_o(deadline))
        self.assertIn("capped notAfterUtc at the caller's deadline", late["why"])

    def test_the_drop_value_is_um_runs_own_not_after_line(self) -> None:
        # um-run.ps1 echoes UmRunDrop's 'job not-after: <id>.meta.json notAfterUtc=<o> sampledUtc=<o> budgetSec=<n> capUtc=<o|none>' line on the information
        # stream. The first such record for the id; the absolute form's short line carries notAfterUtc only; another id, a plain string, nothing: unreported.
        script = (
            "$null = [Console]::In.ReadToEnd()\n"
            "function rec([string]$text) { [System.Management.Automation.InformationRecord]::new([System.Management.Automation.HostInformationMessage]@{ Message = $text }, 'um-run') }\n"
            "$sets = @(\n"
            "  @((rec 'job claimed: job-x.meta.json (no budget requested)'), (rec 'job not-after: job-x.meta.json notAfterUtc=2026-10-24T07:11:00.0000000Z sampledUtc=2026-10-24T05:05:00.0000000Z budgetSec=7560 capUtc=none'), (rec 'job not-after: job-x.meta.json notAfterUtc=2026-10-24T09:00:00.0000000Z')),\n"
            "  @((rec 'job not-after: job-x.meta.json notAfterUtc=2026-10-24T06:00:00.0000000Z sampledUtc=2026-10-24T05:00:00.0000000Z budgetSec=8100 capUtc=2026-10-24T06:00:00.0000000Z')),\n"
            "  @((rec 'job not-after: job-x.meta.json notAfterUtc=2026-10-24T06:00:00.0000000Z')),\n"
            "  @((rec 'job not-after: job-xx.meta.json notAfterUtc=2026-10-24T06:00:00.0000000Z'), 'job not-after: job-x.meta.json notAfterUtc=2026-10-24T06:00:00.0000000Z'),\n"
            "  @()\n"
            ")\n"
            "$out = foreach ($s in $sets) { $v = Get-DvUmRunDropNotAfter -Records $s -JobId 'job-x'\n"
            "  if ($null -eq $v) { [ordered]@{ none = $true } } else { [ordered]@{ none = $false; notAfterUtc = $v.notAfterUtc.ToString('o'); kind = [string]$v.notAfterUtc.Kind;\n"
            "    sampledUtc = $(if ($null -eq $v.sampledUtc) { '' } else { $v.sampledUtc.ToString('o') }); budgetSec = $v.budgetSec; capUtc = $v.capUtc } } }\n"
            "ConvertTo-Json -InputObject @($out) -Compress\n"
        )
        a = run_pwsh_json(script, {})
        self.assertEqual(a[0], {"none": False, "notAfterUtc": "2026-10-24T07:11:00.0000000Z", "kind": "Utc", "sampledUtc": "2026-10-24T05:05:00.0000000Z",
                                "budgetSec": 7560, "capUtc": ""}, "the first 'job not-after:' record; capUtc=none is ''")
        self.assertEqual((a[1]["budgetSec"], a[1]["capUtc"]), (8100, "2026-10-24T06:00:00.0000000Z"))
        self.assertEqual((a[2]["notAfterUtc"], a[2]["sampledUtc"], a[2]["budgetSec"], a[2]["capUtc"]), ("2026-10-24T06:00:00.0000000Z", "", None, ""),
                         "the absolute form's line carries notAfterUtc only")
        self.assertEqual(a[3], {"none": True}, "another id's record and a plain output string are not this job's")
        self.assertEqual(a[4], {"none": True})

    def test_a_caller_that_kills_um_run_at_the_bound_is_covered_whenever_the_job_appears(self) -> None:
        # Invoke-VenueStageNightly kills um-run at its deadline: nobody waits past the bound, so a late drop shortens the waits, never the bound.
        deadline = utc_o(parse_o(self.NOW) + timedelta(seconds=900))
        for bound in ({"TimeoutSec": 900, "MaxQueueWaitSec": 900, "MaxAdmissionWaitSec": 900, "MaxClaimedWaitSec": 900, "CallerCapSec": 900},
                      {"TimeoutSec": 900, "MaxQueueWaitSec": 900, "MaxAdmissionWaitSec": 900, "MaxClaimedWaitSec": 900, "CallerCapSec": 900,
                       "CallerDeadlineUtc": deadline}):
            for d in (0, 61, 600, 5000):
                with self.subTest(source=bound.get("CallerDeadlineUtc", "cap"), dropAfterSec=d):
                    r = self.cover(bound, d)
                    self.assertIn(r["source"], ("caller-cap", "caller-deadline"))
                    self.assertTrue(r["covered"], r)

    def test_a_later_harvest_has_nothing_to_cover(self) -> None:
        r = self.cover(dict(self.UNCAPPED, LaterHarvest=True), 5000)
        self.assertEqual((r["notAfterUtc"], r["source"], r["covered"]), ("", "later-harvest", True))

    def test_the_drop_instant_is_um_runs_own_submitted_line(self) -> None:
        # um-run.ps1 prints UmRunDrop's 'submitted <id> -> <path>' line (Write-Host) only after the job file is in place, so that record's time is never
        # earlier than the drop. Another id's line, an output string with the same text, and a missing line are not the drop.
        script = (
            "$c = [Console]::In.ReadToEnd() | ConvertFrom-Json\n"
            "function rec([string]$text, $o) { $r = [System.Management.Automation.InformationRecord]::new([System.Management.Automation.HostInformationMessage]@{ Message = $text }, 'um-run');\n"
            # (pwsh 7's ConvertFrom-Json may already have made the ISO text a [DateTime]; um-run's records carry LOCAL time, like Write-Host's own)
            "  $t = $(if ($o -is [datetime]) { $o } else { [DateTime]::Parse([string]$o, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind) })\n"
            "  $r.TimeGenerated = $t.ToUniversalTime().ToLocalTime(); $r }\n"
            "$sets = @(\n"
            "  @((rec 'job metadata placed: job-x.meta.json timeoutSec=1' $c.t0), (rec 'submitted job-x -> \\\\share\\inbox\\job-x.job.ps1' $c.t1), 'out', (rec 'submitted job-x -> again' $c.t2)),\n"
            "  @((rec 'submitted job-y -> \\\\share\\inbox\\job-y.job.ps1' $c.t1), 'submitted job-x -> \\\\share\\inbox\\job-x.job.ps1'),\n"
            "  @(),\n"
            "  @((rec 'submitted job-xx -> \\\\share\\inbox\\job-xx.job.ps1' $c.t1))\n"
            ")\n"
            "$out = foreach ($s in $sets) { $v = Get-DvUmRunDropVisibleUtc -Records $s -JobId 'job-x'; [ordered]@{ utc = $(if ($null -eq $v) { '' } else { $v.ToString('o') }); kind = $(if ($null -eq $v) { '' } else { [string]$v.Kind }) } }\n"
            "ConvertTo-Json -InputObject @($out) -Compress\n"
        )
        t0, t1, t2 = "2026-10-24T05:00:01.0000000Z", "2026-10-24T05:00:07.5000000Z", "2026-10-24T05:00:09.0000000Z"
        answers = run_pwsh_json(script, {"t0": t0, "t1": t1, "t2": t2})
        self.assertEqual(answers[0], {"utc": t1, "kind": "Utc"}, "the first 'submitted <id>' record, in UTC")
        self.assertEqual(answers[1]["utc"], "", "another id's record and a plain output string are not this job's drop")
        self.assertEqual(answers[2]["utc"], "", "no record: unmeasured")
        self.assertEqual(answers[3]["utc"], "", "an id that merely starts with this one is another job")


@requires_windows_pwsh
class CallerDeadlineTests(unittest.TestCase):
    """The scheduling-delay fixture (Dot 07bd1383 (b)): Invoke-VenueStageNightly samples what is left of its deadline, then calls the helper. The bound comes
    from the ORIGINAL absolute deadline (-CallerDeadlineUtc), so time passing between the sample and the call can only shorten it."""
    SAMPLE = "2026-10-24T05:00:00.0000000Z"

    def test_a_delay_between_the_sample_and_the_call_never_moves_the_bound_past_the_deadline(self) -> None:
        sample = parse_o(self.SAMPLE)
        deadline = sample + timedelta(seconds=900, milliseconds=500)
        remain = 900                                  # Get-RemainingSec: floor(deadline - sample)
        waits = {"TimeoutSec": min(3600, remain), "MaxQueueWaitSec": remain, "MaxAdmissionWaitSec": min(7200, remain), "MaxClaimedWaitSec": remain}
        delays = [0, 0.4, 0.6, 2.5, 30, 600]
        new = run_helper_cases([dict(waits, CallerCapSec=remain, CallerDeadlineUtc=utc_o(deadline), NowUtc=utc_o(sample + timedelta(seconds=d))) for d in delays])
        old = run_helper_cases([dict(waits, CallerCapSec=remain, NowUtc=utc_o(sample + timedelta(seconds=d))) for d in delays])
        for d, r, o in zip(delays, new, old):
            with self.subTest(delaySec=d):
                self.assertNotIn("error", r, r)
                na = parse_o(r["notAfterUtc"])
                self.assertLessEqual(na, deadline, "never later than the caller's own deadline")
                self.assertEqual(na, deadline, "the deadline itself: um-run is killed there, so nobody waits past it and nobody waits less")
                self.assertEqual(r["source"], "caller-deadline")
                # the defect this replaces: the sampled remaining budget counted again from a later now overshoots the deadline by the delay
                if d >= 0.6:
                    self.assertGreater(parse_o(o["notAfterUtc"]), deadline, "re-deriving from the sample plus a later now overshoots")

    def test_a_deadline_beyond_the_waits_leaves_the_uncapped_bound(self) -> None:
        now = parse_o(self.SAMPLE)
        r = run_helper_cases([{"TimeoutSec": 30, "MaxQueueWaitSec": 30, "MaxAdmissionWaitSec": 30, "MaxClaimedWaitSec": 30, "NowUtc": self.SAMPLE,
                               "CallerDeadlineUtc": utc_o(now + timedelta(seconds=7200))}])[0]
        self.assertNotIn("error", r, r)
        self.assertEqual((r["source"], r["boundSec"]), ("um-run-waits", 120 + SLACK_SEC))

    def test_a_passed_deadline_is_refused(self) -> None:
        now = parse_o(self.SAMPLE)
        for past in (0, 1):
            with self.subTest(pastSec=past):
                r = run_helper_cases([{"TimeoutSec": 30, "MaxQueueWaitSec": 30, "MaxClaimedWaitSec": 30, "NowUtc": self.SAMPLE,
                                       "CallerDeadlineUtc": utc_o(now - timedelta(seconds=past))}])[0]
                self.assertIn("DVE_NOT_AFTER_DEADLINE_PASSED", r.get("error", ""), r)

    def test_a_local_deadline_is_the_same_instant(self) -> None:
        now = parse_o(self.SAMPLE)
        local = (now + timedelta(seconds=600)).astimezone().isoformat()
        r = run_helper_cases([{"TimeoutSec": 600, "MaxQueueWaitSec": 600, "MaxAdmissionWaitSec": 600, "MaxClaimedWaitSec": 600, "CallerCapSec": 600,
                               "NowUtc": self.SAMPLE, "CallerDeadlineUtc": local}])[0]
        self.assertNotIn("error", r, r)
        self.assertEqual(parse_o(r["notAfterUtc"]), now + timedelta(seconds=600))


def ms(dt: datetime) -> int:
    """Epoch milliseconds (the drop fixture's clock unit: numbers survive ConvertFrom-Json unchanged, ISO text does not)."""
    return int(round(dt.timestamp() * 1000))


@requires_windows_pwsh
class DropRelativeNotAfterTests(unittest.TestCase):
    """r2 (hub ruling on sol's r1 blocker): the a priori bound, through the REAL drop. UmRunDrop.psm1 Invoke-UmRunDrop places a job into a temp inbox (never a
    real share, never an agent) with its clock injected (-TestUtcNow: each call takes the next instant, the last repeating), and the bound comes from
    Get-DvJobNotAfter at the anchor. The judge reads the drop's own 'job not-after:' line back through Get-DvUmRunDropNotAfter."""
    ANCHOR = parse_o("2026-10-24T05:00:00.0000000Z")
    SOL = {"TimeoutSec": 120, "MaxQueueWaitSec": 120, "MaxAdmissionWaitSec": 7200, "MaxClaimedWaitSec": 60}   # sol's repro waits (7500 s)

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="dv-na-drop-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(os.path.realpath(tmp.name))
        self.share = self.tmp / "agent"
        self.inbox = self.share / "inbox"
        self.outbox = self.share / "outbox"
        self.inbox.mkdir(parents=True)
        self.outbox.mkdir()
        local = self.tmp / "local"
        local.mkdir()
        self.job = local / "job-x.job.ps1"
        self.job.write_text("Write-Output 'hi'\n", encoding="utf-8")
        self.log = self.tmp / "copies.log"

    def names(self) -> list[str]:
        return sorted(p.name for p in self.inbox.iterdir())

    def drop(self, *, clock: list[datetime] | None = None, bound: dict | None = None, budget_from_bound: bool = False,
             not_after_utc: str | None = None, budget: int | None = None, cap: datetime | str | None = None, drop_at: datetime | None = None,
             advance: dict | None = None, side_file: Path | None = None, repub: dict | None = None) -> dict:
        """One Invoke-UmRunDrop for job-x. Returns {threw, lines, meta (raw text or ''), names, copied, bound, judge, seen}; judge is
        Test-DvNotAfterCoversDrop on the bound, the drop at drop_at and the drop's own reported value (only when bound and drop_at are given).
        r3 (sol r2 blocker): advance = {'job': s, 'side': s} turns the clock into a SIMULATED one that starts at clock[0] and that the existing Copier seam
        moves on by s seconds while it copies the job's temporary (or a side-file's .sidepart): share I/O after the drop's first clock read. The job then
        becomes visible at the simulated instant once the drop returns (the judge's drop instant, unless drop_at is given), and seen is what the agent could
        read at visibility: the simulated instant and meta.json's text at the job's rename (TestHookBeforeJobVisible).
        r4 (Dot B1): advance 'meta': s moves the simulated clock on by s seconds in TestHookBeforeJobVisible's FIRST call, 'metaEvery': s in every call: share
        I/O after the drop's final clock read (the re-publish's write, read-back, claim read and replace), which no seam inside it existed for at 6b542fe8.
        seen.calls counts the hook's calls; seen is the LAST call's view (the one the rename follows). repub = {'advance': s} or {'fault': 'corrupt'} or
        {'fault': 'steal', 'other': <meta.json text>} acts in TestHookAfterNotAfterRepublishTmpWritten's first call (the re-publish's temp just written):
        the clock moves on, the temp is torn, or another submission's claim replaces meta.json."""
        script = (
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{DROP_MODULE}' -Force\n"
            f"Import-Module '{RUNNER_MODULE}' -Force -DisableNameChecking\n"
            "$c = [Console]::In.ReadToEnd() | ConvertFrom-Json\n"
            f"$log = '{self.log}'\n"
            "$sim = $null\n"
            "$copier = { param($s, $d) Add-Content -LiteralPath $log -Value $d; Copy-Item -LiteralPath $s -Destination $d\n"
            "  if ($null -ne $sim) { $k = $(if ($d -like '*.job.tmp') { 'job' } elseif ($d -like '*.sidepart') { 'side' } else { '' })\n"
            "    if ($k -and $null -ne $c.advance.$k) { $sim.now = $sim.now.AddSeconds([double]$c.advance.$k) } } }\n"
            "$clock = [Collections.Generic.Queue[DateTimeOffset]]::new()\n"
            "foreach ($t in @($c.clock)) { if ($null -ne $t) { $clock.Enqueue([DateTimeOffset]::FromUnixTimeMilliseconds([long]$t)) } }\n"
            "$tick = { if ($clock.Count -gt 1) { $clock.Dequeue() } else { $clock.Peek() } }\n"
            "if ($null -ne $c.advance) { $sim = @{ now = $clock.Peek() }; $tick = { $sim.now } }\n"
            "$seen = @{}\n"
            "$b = $null\n"
            "if ($null -ne $c.bound) { $bp = @{ NowUtc = [DateTimeOffset]::FromUnixTimeMilliseconds([long]$c.anchor).UtcDateTime }\n"
            "  foreach ($prop in $c.bound.PSObject.Properties) { $bp[$prop.Name] = [int]$prop.Value }\n"
            "  $b = Get-DvJobNotAfter @bp }\n"
            f"$p = @{{ Inbox = '{self.inbox}'; Outbox = '{self.outbox}'; ScriptPath = '{self.job}'; JobId = 'job-x'; Copier = $copier }}\n"
            "if ($clock.Count -gt 0) { $p.TestUtcNow = $tick }\n"
            # (instants travel as '#<text>': pwsh 7's ConvertFrom-Json would turn ISO text into a [DateTime], and these must reach the drop verbatim)
            "if ($null -ne $c.notAfterUtc) { $p.NotAfterUtc = ([string]$c.notAfterUtc).Substring(1) }\n"
            "if ($c.budgetFromBound) { $p.NotAfterBudgetSec = [int]$b.budgetSec } elseif ($null -ne $c.budget) { $p.NotAfterBudgetSec = [int]$c.budget }\n"
            "if ($null -ne $c.cap) { $p.NotAfterCapUtc = ([string]$c.cap).Substring(1) }\n"
            "if ($null -ne $c.side) { $p.SideFile = @([string]$c.side) }\n"
            f"$metaPath = Join-Path '{self.inbox}' 'job-x.meta.json'\n"
            "if ($null -ne $sim) { $p.TestHookBeforeJobVisible = { $seen.calls = 1 + [int]$seen.calls\n"
            "  if ($null -ne $c.advance.metaEvery) { $sim.now = $sim.now.AddSeconds([double]$c.advance.metaEvery) }\n"
            "  elseif ($seen.calls -eq 1 -and $null -ne $c.advance.meta) { $sim.now = $sim.now.AddSeconds([double]$c.advance.meta) }\n"
            "  $seen.atUtc = $sim.now.UtcDateTime.ToString('o'); $seen.meta = [IO.File]::ReadAllText($metaPath) } }\n"
            "if ($null -ne $c.repub) { $p.TestHookAfterNotAfterRepublishTmpWritten = { param($tmpPath) $seen.repubCalls = 1 + [int]$seen.repubCalls\n"
            "  if ($seen.repubCalls -eq 1) {\n"
            "    if ($null -ne $c.repub.advance) { $sim.now = $sim.now.AddSeconds([double]$c.repub.advance) }\n"
            "    if ($c.repub.fault -eq 'corrupt') { [IO.File]::WriteAllText($tmpPath, '{\"torn\":') }\n"
            "    if ($c.repub.fault -eq 'steal') { [IO.File]::WriteAllText($metaPath, [string]$c.repub.other) } } } }\n"
            "$threw = ''; $lines = @()\n"
            "try { $lines = @(Invoke-UmRunDrop @p | ForEach-Object { [string]$_ }) } catch { $threw = [string]$_.Exception.Message }\n"
            "$meta = $(if (Test-Path -LiteralPath $metaPath) { [IO.File]::ReadAllText($metaPath) } else { '' })\n"
            "$dropAt = $(if ($null -ne $c.dropAt) { [DateTimeOffset]::FromUnixTimeMilliseconds([long]$c.dropAt).UtcDateTime } elseif ($null -ne $sim) { $sim.now.UtcDateTime } else { $null })\n"
            "$judge = $null\n"
            "if ($null -ne $b -and $null -ne $dropAt) {\n"
            "  $recs = @($lines | ForEach-Object { [System.Management.Automation.InformationRecord]::new([System.Management.Automation.HostInformationMessage]@{ Message = $_ }, 'um-run.ps1') })\n"
            "  $v = Test-DvNotAfterCoversDrop -NotAfter $b -DropVisibleUtc $dropAt `\n"
            "    -DropNotAfter (Get-DvUmRunDropNotAfter -Records $recs -JobId 'job-x') -JobId 'job-x'\n"
            "  $judge = [ordered]@{ notAfterUtc = [string]$v.notAfterUtc; form = $v.form; dropSampledUtc = [string]$v.dropSampledUtc; dropVisibleUtc = [string]$v.dropVisibleUtc;\n"
            "    marginSec = $v.marginSec; covered = $v.covered; why = $v.why }\n"
            "}\n"
            "$bo = $(if ($null -eq $b) { $null } else { [ordered]@{ notAfterUtc = [string]$b.notAfterUtc; waitSec = $b.waitSec; budgetSec = $b.budgetSec; capUtc = [string]$b.capUtc } })\n"
            "ConvertTo-Json -Compress -Depth 4 -InputObject ([ordered]@{ threw = $threw; lines = @($lines); meta = $meta; bound = $bo; judge = $judge; seen = $seen;\n"
            f"  copied = (Test-Path -LiteralPath '{self.log}') }})\n"
        )
        cap_text = utc_o(cap) if isinstance(cap, datetime) else cap
        payload = {"clock": [ms(t) for t in (clock or [])], "anchor": ms(self.ANCHOR), "bound": bound, "budgetFromBound": budget_from_bound,
                   "notAfterUtc": None if not_after_utc is None else "#" + not_after_utc, "budget": budget,
                   "cap": None if cap_text is None else "#" + cap_text,
                   "dropAt": ms(drop_at) if drop_at is not None else None, "advance": advance,
                   "side": None if side_file is None else str(side_file), "repub": repub}
        p = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], input=json.dumps(payload), capture_output=True, text=True,
                           timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        r = json.loads(p.stdout.strip().splitlines()[-1])
        r["names"] = self.names()
        return r

    def test_sols_repro_a_300_s_late_drop_still_covers_every_wait_a_priori(self) -> None:
        # sol r1: anchor 05:00:00Z, timeout 120, queue 120, admission 7200, claimed 60. r1 computed notAfterUtc 07:06:00Z before um-run (the waits plus the
        # 60 s slack from the anchor); a 300 s setup/share delay put the drop at 05:05:00Z, 240 s short of drop + waits. Through the real drop the value now
        # counts from the drop: 05:05:00Z + 7560 s = 07:11:00Z >= drop + 7500 s.
        drop_at = self.ANCHOR + timedelta(seconds=300)
        r = self.drop(clock=[drop_at], bound=self.SOL, budget_from_bound=True, drop_at=drop_at)
        self.assertEqual(r["threw"], "", r)
        self.assertEqual(r["bound"]["notAfterUtc"], "2026-10-24T07:06:00.0000000Z", "r1's value, computed at the anchor")
        self.assertEqual((r["bound"]["waitSec"], r["bound"]["budgetSec"], r["bound"]["capUtc"]), (7500, 7500 + SLACK_SEC, ""))
        meta = json.loads(r["meta"])
        na = parse_o(meta["notAfterUtc"])
        self.assertEqual(meta["notAfterUtc"], "2026-10-24T07:11:00.0000000Z")
        self.assertGreaterEqual(na, drop_at + timedelta(seconds=7500), "notAfterUtc >= drop + every um-run wait, a priori")
        self.assertLess(parse_o(r["bound"]["notAfterUtc"]), drop_at + timedelta(seconds=7500), "the r1 value really fell short (the repro holds)")
        self.assertIn("job not-after: job-x.meta.json notAfterUtc=2026-10-24T07:11:00.0000000Z sampledUtc=2026-10-24T05:05:00.0000000Z budgetSec=7560 "
                      "capUtc=none", r["lines"])
        self.assertEqual(r["names"], ["job-x.job.ps1", "job-x.meta.json"])
        # the witness reads that line back: covered, by the slack
        self.assertEqual((r["judge"]["form"], r["judge"]["covered"], r["judge"]["marginSec"]), ("drop-relative", True, SLACK_SEC), r["judge"])
        self.assertEqual(r["judge"]["notAfterUtc"], meta["notAfterUtc"])
        self.assertEqual(r["judge"]["dropSampledUtc"], "2026-10-24T05:05:00.0000000Z")

    def test_every_drop_delay_is_covered_on_the_uncapped_path(self) -> None:
        for d in (0, 61, 3600):
            with self.subTest(dropDelaySec=d):
                for leftover in self.inbox.iterdir():
                    leftover.unlink()
                drop_at = self.ANCHOR + timedelta(seconds=d)
                r = self.drop(clock=[drop_at], bound=self.SOL, budget_from_bound=True, drop_at=drop_at)
                self.assertEqual(r["threw"], "", r)
                na = parse_o(json.loads(r["meta"])["notAfterUtc"])
                self.assertEqual(na, drop_at + timedelta(seconds=7500 + SLACK_SEC))
                self.assertTrue(r["judge"]["covered"], r["judge"])

    def test_a_late_drop_never_writes_past_the_cap(self) -> None:
        cap = self.ANCHOR + timedelta(seconds=600)
        for d, budget, expected in ((300, 7560, cap), (590, 7560, cap), (30, 100, self.ANCHOR + timedelta(seconds=130))):
            with self.subTest(dropDelaySec=d, budget=budget):
                for leftover in self.inbox.iterdir():
                    leftover.unlink()
                r = self.drop(clock=[self.ANCHOR + timedelta(seconds=d)], budget=budget, cap=cap)
                self.assertEqual(r["threw"], "", r)
                na = parse_o(json.loads(r["meta"])["notAfterUtc"])
                self.assertLessEqual(na, cap, "never later than the cap")
                self.assertEqual(na, expected, "min(drop + budget, cap)")
                self.assertTrue(any(l.startswith(f"job not-after: job-x.meta.json notAfterUtc={utc_o(expected)} ") and l.endswith(f" capUtc={utc_o(cap)}")
                                    for l in r["lines"]), r["lines"])

    def test_a_cap_already_passed_refuses_before_any_share_write(self) -> None:
        for name, clock, cap in (("passed before the call", [self.ANCHOR], self.ANCHOR - timedelta(seconds=1)),
                                 ("equal to now", [self.ANCHOR], self.ANCHOR),
                                 # the first clock read (the up-front check) is before the cap, the second (at the claim) after it
                                 ("passed before the claim", [self.ANCHOR, self.ANCHOR + timedelta(seconds=20)], self.ANCHOR + timedelta(seconds=10))):
            with self.subTest(case=name):
                r = self.drop(clock=clock, budget=7560, cap=cap)
                self.assertIn("UMRUN_NOT_AFTER_CAP_PASSED", r["threw"], r)
                self.assertEqual(r["names"], [], "no claim, no job, no temporary")
                self.assertFalse(r["copied"], "no copy")
                self.assertEqual(r["meta"], "")

    def test_the_absolute_form_is_unchanged(self) -> None:
        instant = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(microsecond=0)
        r = self.drop(not_after_utc=instant.strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertEqual(r["threw"], "", r)
        meta = json.loads(r["meta"])
        self.assertEqual(r["meta"], json.dumps({"jobId": "job-x", "nonce": meta["nonce"], "notAfterUtc": utc_o(instant)}, separators=(",", ":")),
                         "the claim's bytes are exactly the pre-r2 absolute form")
        self.assertIn(f"job not-after: job-x.meta.json notAfterUtc={utc_o(instant)}", r["lines"], "the same line, with nothing appended")
        # an injected clock does not reach the absolute form (it is checked against the wall clock, as before)
        r2 = self.drop(clock=[datetime(2099, 1, 1, tzinfo=timezone.utc)], not_after_utc=instant.strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertIn("UMRUN_JOBID_IN_USE", r2["threw"], "the first claim still holds job-x: the absolute value was accepted, not refused as past")

    def test_both_forms_or_a_cap_alone_is_a_typed_error_before_anything_is_placed(self) -> None:
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(microsecond=0)
        for name, kw, token in (("absolute + budget", {"not_after_utc": utc_o(future), "budget": 600}, "UMRUN_NOT_AFTER_CONFLICT"),
                                ("absolute + cap", {"not_after_utc": utc_o(future), "cap": future}, "UMRUN_NOT_AFTER_CONFLICT"),
                                ("cap alone", {"cap": future}, "UMRUN_NOT_AFTER_INVALID -NotAfterCapUtc"),
                                ("negative budget", {"budget": -5}, "UMRUN_NOT_AFTER_INVALID -NotAfterBudgetSec -5"),
                                ("budget too large", {"budget": 604801}, "UMRUN_NOT_AFTER_INVALID -NotAfterBudgetSec 604801"),
                                ("offset-less cap", {"budget": 600, "cap": future.strftime("%Y-%m-%dT%H:%M:%S")}, "no Z or +hh:mm offset")):
            with self.subTest(case=name):
                r = self.drop(**kw)
                self.assertIn(token, r["threw"], r)
                self.assertEqual(r["names"], [])
                self.assertFalse(r["copied"])

    def test_um_run_forwards_the_budget_to_the_drop(self) -> None:
        # um-run.ps1 -NotAfterBudgetSec reaches the claim drop-relative (a fake share with a heartbeat and no agent: the client is killed once the claim is
        # read, as test_um_run_sidefiles.py does for -NotAfterUtc)
        (self.share / "heartbeat.txt").write_text("alive", encoding="utf-8")
        before = datetime.now(timezone.utc)
        proc = subprocess.Popen([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(UM_RUN), "-ScriptPath", str(self.job), "-AgentShare",
                                 str(self.share), "-TimeoutSec", "3600", "-PollSeconds", "1", "-JobId", "job-x", "-NotAfterBudgetSec", "5000"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        meta_path = self.inbox / "job-x.meta.json"
        try:
            deadline = time.monotonic() + 60
            meta = None
            while time.monotonic() < deadline and proc.poll() is None:
                if (self.inbox / "job-x.job.ps1").exists() and meta_path.exists():
                    meta = json.loads(meta_path.read_text(encoding="ascii"))
                    break
                time.sleep(0.2)
        finally:
            if proc.poll() is None:
                proc.kill()
            stdout, stderr = proc.communicate()
        after = datetime.now(timezone.utc)
        self.assertIsNotNone(meta, (stdout or "") + (stderr or ""))
        na = parse_o(meta["notAfterUtc"])
        self.assertGreaterEqual(na, before + timedelta(seconds=5000) - timedelta(milliseconds=1))
        self.assertLessEqual(na, after + timedelta(seconds=5000))

    def test_um_run_refuses_both_forms_and_places_nothing(self) -> None:
        (self.share / "heartbeat.txt").write_text("alive", encoding="utf-8")
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        p = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(UM_RUN), "-ScriptPath", str(self.job), "-AgentShare",
                            str(self.share), "-TimeoutSec", "60", "-PollSeconds", "1", "-JobId", "job-x", "-NotAfterUtc", future, "-NotAfterBudgetSec", "600"],
                           capture_output=True, text=True, timeout=120)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("UMRUN_NOT_AFTER_CONFLICT", p.stdout + p.stderr)
        self.assertEqual(self.names(), [])


@requires_windows_pwsh
class PlacementDelayNotAfterTests(unittest.TestCase):
    """r3 (sol r2 blocker, new catalogue row: delay after claim-clock sampling during share metadata/job placement, with admission pending -> expiry covers
    the actual visible-job waits, capped by the caller deadline). r2 read the drop's clock before the claim, and the side-file and job copies that follow
    took that much time out of the waits. Through the REAL drop, the existing Copier seam now moves a simulated clock on while it copies (share I/O after
    the first clock read); the job becomes visible at the simulated instant, and the meta.json the agent can read at that instant must carry a notAfterUtc
    >= visibility + every um-run wait, never later than the cap. The drop's fixture (temp inbox, never a share or an agent) is DropRelativeNotAfterTests'."""
    ANCHOR = DropRelativeNotAfterTests.ANCHOR
    SOL = DropRelativeNotAfterTests.SOL
    setUp = DropRelativeNotAfterTests.setUp
    names = DropRelativeNotAfterTests.names
    drop = DropRelativeNotAfterTests.drop

    def test_sols_r2_repro_a_300_s_job_copy_after_the_clock_read_still_covers_every_wait_from_visibility(self) -> None:
        # sol r2: sample 05:00:00Z, waits 120/120/7200/60 (7500 s), budget 7560; the Copier seam advances 300 s before the job is visible. r2 wrote
        # 07:06:00Z (sampled + budget), visible 05:05:00Z: covered=false marginSec=-240. The value the agent reads at visibility must be >= 05:05:00Z + 7500 s.
        r = self.drop(clock=[self.ANCHOR], bound=self.SOL, budget_from_bound=True, advance={"job": 300})
        self.assertEqual(r["threw"], "", r)
        visible = parse_o(r["seen"]["atUtc"])
        self.assertEqual(visible, self.ANCHOR + timedelta(seconds=300), "the job copy took 300 s of simulated share I/O")
        seen = parse_o(json.loads(r["seen"]["meta"])["notAfterUtc"])
        self.assertGreaterEqual(seen, visible + timedelta(seconds=7500), "the metadata the agent reads at visibility covers every wait from visibility")
        self.assertEqual(seen, visible + timedelta(seconds=7500 + SLACK_SEC), "re-derived from the clock read after the copy: 07:11:00Z")
        self.assertEqual(json.loads(r["meta"])["notAfterUtc"], json.loads(r["seen"]["meta"])["notAfterUtc"], "nothing changes it after visibility")
        self.assertEqual(r["names"], ["job-x.job.ps1", "job-x.meta.json"], "no temporary left")
        self.assertEqual((r["judge"]["covered"], r["judge"]["marginSec"]), (True, SLACK_SEC), r["judge"])
        self.assertEqual(r["judge"]["dropSampledUtc"], utc_o(visible), "the anchor the drop reports is the instant it counted from")
        self.assertEqual([l for l in r["lines"] if l.startswith("job not-after:")],
                         [f"job not-after: job-x.meta.json notAfterUtc={utc_o(seen)} sampledUtc={utc_o(visible)} budgetSec=7560 capUtc=none"],
                         "one line, the value the visible job carries")

    def test_a_slow_side_file_copy_after_the_clock_read_is_covered_too(self) -> None:
        side = self.tmp / "local" / "pkg.txt"
        side.write_text("side\n", encoding="ascii")
        r = self.drop(clock=[self.ANCHOR], bound=self.SOL, budget_from_bound=True, advance={"side": 900, "job": 30}, side_file=side)
        self.assertEqual(r["threw"], "", r)
        visible = parse_o(r["seen"]["atUtc"])
        self.assertEqual(visible, self.ANCHOR + timedelta(seconds=930))
        self.assertGreaterEqual(parse_o(json.loads(r["seen"]["meta"])["notAfterUtc"]), visible + timedelta(seconds=7500))
        self.assertTrue(r["judge"]["covered"], r["judge"])
        self.assertEqual(r["names"], ["job-x.job.ps1", "job-x.meta.json", "pkg.txt"])

    def test_placement_delay_never_moves_the_value_past_the_cap(self) -> None:
        cap = self.ANCHOR + timedelta(seconds=600)
        r = self.drop(clock=[self.ANCHOR], budget=7560, cap=cap, advance={"job": 300})
        self.assertEqual(r["threw"], "", r)
        self.assertEqual(parse_o(json.loads(r["seen"]["meta"])["notAfterUtc"]), cap, "min(visibility + budget, cap) is the cap")
        self.assertTrue(any(l.startswith(f"job not-after: job-x.meta.json notAfterUtc={utc_o(cap)} sampledUtc={utc_o(self.ANCHOR + timedelta(seconds=300))} ")
                            for l in r["lines"]), r["lines"])

    def test_a_cap_that_passes_during_placement_refuses_and_the_job_never_becomes_visible(self) -> None:
        # the cap is later than the claim's clock read and earlier than the end of the job copy: the job would appear with an expiry already passed
        cap = self.ANCHOR + timedelta(seconds=200)
        r = self.drop(clock=[self.ANCHOR], budget=7560, cap=cap, advance={"job": 300})
        self.assertIn("UMRUN_NOT_AFTER_CAP_PASSED", r["threw"], r)
        self.assertEqual(r["names"], [], "the claim is rolled back and no job or temporary is left")
        self.assertNotIn("atUtc", r["seen"], "the job never reached its rename")

    def test_the_metadata_re_publish_keeps_the_claims_nonce_and_fields(self) -> None:
        r = self.drop(clock=[self.ANCHOR], budget=600, advance={"job": 45})
        self.assertEqual(r["threw"], "", r)
        meta = json.loads(r["meta"])
        self.assertEqual(list(meta), ["jobId", "nonce", "notAfterUtc"])
        self.assertEqual(meta["jobId"], "job-x")
        self.assertTrue(any(l == f"UMRUN_NONCE={meta['nonce']}" for l in r["lines"]), "the claim's own nonce, never a new one")
        self.assertEqual(parse_o(meta["notAfterUtc"]), self.ANCHOR + timedelta(seconds=645))


STALL_SEC = 15          # UmRunDrop.psm1 $script:UmRunNotAfterPlacementStallSec: a quarter of SLACK_SEC
STALL_RETRIES = 3       # UmRunDrop.psm1 $script:UmRunNotAfterPlacementRetries


@requires_windows_pwsh
class PostSampleStallNotAfterTests(unittest.TestCase):
    """r4 (Dot B1 on 6b542fe8, accepted by the hub as a defect): r3's final clock read is followed by the re-publish's own share I/O (the metadata write, its
    read-back, the claim read, the replace) and the job's rename, and r3's comment bounded their COUNT, not their DURATION. Dot's trace: final sample
    05:00:00Z, budget 7560, a 300 s metadata-I/O stall -> visible 05:05:00Z carrying 07:06:00Z, while queue + admission (120 + 7200) can keep um-run waiting
    to 07:07:00Z. The visible job must carry a notAfterUtc covering every wait from the VISIBLE instant, or the drop must refuse typed with the job never
    visible. Same real-drop fixture as DropRelativeNotAfterTests."""
    ANCHOR = DropRelativeNotAfterTests.ANCHOR
    SOL = DropRelativeNotAfterTests.SOL
    setUp = DropRelativeNotAfterTests.setUp
    names = DropRelativeNotAfterTests.names
    drop = DropRelativeNotAfterTests.drop

    def test_dots_trace_a_300_s_stall_after_the_final_sample_still_covers_every_wait_from_visibility(self) -> None:
        # the claim at 04:59:00Z, a 60 s job copy, so r3's final sample is 05:00:00Z and it re-publishes 07:06:00Z; then 300 s of metadata I/O before the
        # rename. RED at 6b542fe8: visible 05:05:00Z with 07:06:00Z, 240 s short of visibility + 7500 s.
        r = self.drop(clock=[self.ANCHOR - timedelta(seconds=60)], bound=self.SOL, budget_from_bound=True, advance={"job": 60, "meta": 300})
        self.assertEqual(r["threw"], "", r)
        visible = parse_o(r["seen"]["atUtc"])
        self.assertEqual(visible, self.ANCHOR + timedelta(seconds=300), "Dot's visible instant: 05:05:00Z")
        seen = parse_o(json.loads(r["seen"]["meta"])["notAfterUtc"])
        self.assertGreaterEqual(seen, visible + timedelta(seconds=120 + 7200), "queue + admission from visibility (Dot: pending to 07:07:00Z)")
        self.assertGreaterEqual(seen, visible + timedelta(seconds=7500), "every um-run wait from visibility")
        self.assertEqual(seen, visible + timedelta(seconds=7500 + SLACK_SEC), "re-derived from the clock read after the stall: 07:11:00Z")
        self.assertEqual(r["seen"]["calls"], 2, "one stalled pass, then one re-derived and re-published pass")
        self.assertEqual(json.loads(r["meta"])["notAfterUtc"], json.loads(r["seen"]["meta"])["notAfterUtc"], "nothing changes it after visibility")
        self.assertEqual(r["names"], ["job-x.job.ps1", "job-x.meta.json"], "no temporary left")
        self.assertEqual((r["judge"]["covered"], r["judge"]["marginSec"]), (True, SLACK_SEC), r["judge"])
        self.assertEqual([l for l in r["lines"] if l.startswith("job not-after:")],
                         [f"job not-after: job-x.meta.json notAfterUtc={utc_o(seen)} sampledUtc={utc_o(visible)} budgetSec=7560 capUtc=none"])

    def test_a_stall_inside_the_re_publish_write_itself_is_measured_too(self) -> None:
        # the same 300 s, spent between the re-publish's temp write and its read-back (TestHookAfterNotAfterRepublishTmpWritten, new in r4)
        r = self.drop(clock=[self.ANCHOR - timedelta(seconds=60)], bound=self.SOL, budget_from_bound=True, advance={"job": 60}, repub={"advance": 300})
        self.assertEqual(r["threw"], "", r)
        visible = parse_o(r["seen"]["atUtc"])
        self.assertEqual(visible, self.ANCHOR + timedelta(seconds=300))
        self.assertEqual(parse_o(json.loads(r["seen"]["meta"])["notAfterUtc"]), visible + timedelta(seconds=7500 + SLACK_SEC))
        self.assertEqual((r["seen"]["calls"], r["seen"]["repubCalls"]), (2, 2), "re-published twice: the stalled value never reached the rename")
        self.assertTrue(r["judge"]["covered"], r["judge"])

    def test_a_stall_within_the_stated_bound_proceeds_on_the_first_pass(self) -> None:
        # STALL_SEC is the stated bound: a post-sample stall of at most that long is absorbed by the slack, not re-published
        r = self.drop(clock=[self.ANCHOR - timedelta(seconds=60)], bound=self.SOL, budget_from_bound=True, advance={"job": 60, "meta": STALL_SEC})
        self.assertEqual(r["threw"], "", r)
        self.assertEqual(r["seen"]["calls"], 1)
        visible = parse_o(r["seen"]["atUtc"])
        seen = parse_o(json.loads(r["seen"]["meta"])["notAfterUtc"])
        self.assertEqual(seen, self.ANCHOR + timedelta(seconds=7500 + SLACK_SEC), "the final sample's value (05:00:00Z + 7560 s)")
        self.assertEqual(seen - visible, timedelta(seconds=7500 + SLACK_SEC - STALL_SEC), "the slack minus the stall still covers every wait")

    def test_a_stall_that_never_ends_refuses_typed_and_the_job_never_becomes_visible(self) -> None:
        r = self.drop(clock=[self.ANCHOR], bound=self.SOL, budget_from_bound=True, advance={"job": 60, "metaEvery": 300})
        self.assertIn("UMRUN_NOT_AFTER_PLACEMENT_STALL", r["threw"], r)
        self.assertEqual(r["seen"]["calls"], 1 + STALL_RETRIES, "bounded: the first pass and STALL_RETRIES re-derivations")
        self.assertEqual(r["names"], [], "the claim is rolled back and no job or temporary is left")
        self.assertFalse(any(l.startswith(("job not-after:", "submitted ")) for l in r["lines"]), r["lines"])

    def test_a_cap_that_passes_during_the_post_sample_stall_refuses_and_the_job_never_becomes_visible(self) -> None:
        # min(05:00:00Z + 7560 s, cap 05:03:00Z) is the cap; 300 s of metadata I/O after the final sample passes it. RED at 6b542fe8: visible with 05:03:00Z.
        r = self.drop(clock=[self.ANCHOR], budget=7560, cap=self.ANCHOR + timedelta(seconds=180), advance={"meta": 300})
        self.assertIn("UMRUN_NOT_AFTER_CAP_PASSED", r["threw"], r)
        self.assertEqual(r["names"], [], "the claim is rolled back and no job or temporary is left")

    def test_the_stall_bound_is_a_stated_fraction_of_the_submitters_slack(self) -> None:
        drop_src = DROP_MODULE.read_text(encoding="ascii")
        runner_src = RUNNER_MODULE.read_text(encoding="utf-8")
        stall = int(re.search(r"^\$script:UmRunNotAfterPlacementStallSec = (\d+)", drop_src, re.M).group(1))
        retries = int(re.search(r"^\$script:UmRunNotAfterPlacementRetries = (\d+)", drop_src, re.M).group(1))
        slack = int(re.search(r"^\$script:DvNotAfterSlackSec = (\d+)", runner_src, re.M).group(1))
        self.assertEqual((stall, retries, slack), (STALL_SEC, STALL_RETRIES, SLACK_SEC))
        self.assertEqual(stall * 4, slack, "a quarter of the slack: the rest covers the single unmeasured rename and um-run's poll")


@requires_windows_pwsh
class RepublishRefusalTests(unittest.TestCase):
    """r4 (Grok G4 on 6b542fe8): the re-publish's two refusals had no tests. Each must leave the job never visible; a nonce mismatch must leave the OTHER
    submission's claim exactly as it found it (the rollback's nonce rule), never removed or rewritten."""
    ANCHOR = DropRelativeNotAfterTests.ANCHOR
    setUp = DropRelativeNotAfterTests.setUp
    names = DropRelativeNotAfterTests.names
    drop = DropRelativeNotAfterTests.drop

    def test_a_torn_re_publish_write_refuses_and_the_job_never_becomes_visible(self) -> None:
        r = self.drop(clock=[self.ANCHOR], budget=7560, advance={"job": 30}, repub={"fault": "corrupt"})
        self.assertIn("UMRUN_JOB_METADATA_VERIFY_FAILED job-x.meta.json did not round-trip to the share (not-after re-publish)", r["threw"], r)
        self.assertNotIn("atUtc", r["seen"], "the job never reached its rename")
        self.assertEqual(r["names"], [], "this submission's own claim is rolled back; no job, no temporary")
        self.assertFalse(any(l.startswith(("job not-after:", "submitted ")) for l in r["lines"]), r["lines"])

    def test_a_nonce_mismatch_refuses_and_the_other_submissions_claim_survives_the_rollback_untouched(self) -> None:
        other = '{"jobId":"job-x","nonce":"0123456789abcdef0123456789abcdef","notAfterUtc":"2026-10-24T09:00:00.0000000Z"}'
        r = self.drop(clock=[self.ANCHOR], budget=7560, advance={"job": 30}, repub={"fault": "steal", "other": other})
        self.assertIn("UMRUN_JOBID_IN_USE inbox\\job-x.meta.json is no longer this submission's own claim (nonce mismatch); refusing to re-publish it",
                      r["threw"], r)
        self.assertIn("was left untouched, not removed during rollback", r["threw"], "the rollback saw the other nonce and kept its hands off")
        self.assertNotIn("atUtc", r["seen"], "the job never reached its rename")
        self.assertEqual(r["names"], ["job-x.meta.json"], "only the other submission's claim: no job, no temporary")
        self.assertEqual(r["meta"], other, "byte for byte the other submission's claim")


@requires_windows_pwsh
class NotAfterInputOrderTests(unittest.TestCase):
    """r4 (Grok G2): passing both forms is UMRUN_NOT_AFTER_CONFLICT whatever the absolute value is. At 6b542fe8 the absolute value was validated first, so a
    past or malformed -NotAfterUtc with -NotAfterBudgetSec reported UMRUN_NOT_AFTER_INVALID and hid the real mistake."""
    ANCHOR = DropRelativeNotAfterTests.ANCHOR
    setUp = DropRelativeNotAfterTests.setUp
    names = DropRelativeNotAfterTests.names
    drop = DropRelativeNotAfterTests.drop

    def test_both_forms_is_a_conflict_even_when_the_absolute_value_is_invalid(self) -> None:
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for name, kw in (("past absolute + budget", {"not_after_utc": past, "budget": 600}),
                         ("unparseable absolute + budget", {"not_after_utc": "not-a-time", "budget": 600}),
                         ("offset-less absolute + cap", {"not_after_utc": "2099-01-01T00:00:00", "budget": 600, "cap": "2099-01-01T00:00:00Z"})):
            with self.subTest(case=name):
                r = self.drop(**kw)
                self.assertIn("UMRUN_NOT_AFTER_CONFLICT", r["threw"], r)
                self.assertEqual(r["names"], [])
                self.assertFalse(r["copied"])


@requires_windows_pwsh
class PostVisibilityTailNotAfterTests(unittest.TestCase):
    """r5 (Dot 180721e3 on 4f6c17cb, accepted by the hub as a defect): r4 bounded everything up to the job's rename, but the rename is followed by the drop's
    own cleanup (UmRunDrop.psm1 at 4f6c17cb: the job temporary's handle dispose and a Test-Path on it, :976-979) and um-run starts its relative waits only
    after Invoke-UmRunDrop returns (um-run.ps1 :187 buffers the call, :224 starts the clocks). Dot's trace: sample 05:00, budget 7560, a 300 s stall in that
    tail -> notAfterUtc stays 07:06 while queue 120 + admission 7200 counted from 05:05 run to 07:07.
    The REAL um-run.ps1 and the REAL drop, on a temp share with a heartbeat and no agent. um-run has no clock seam, so the trace is SCALED onto the wall
    clock with Dot's ordering kept (stall > slack, stall > queue): queue QUEUE_SEC, admission ADMIT_SEC, budget = those plus TAIL_SLACK_SEC, and a
    STALL_SEC sleep. The stall is a global function shadowing Test-Path and Write-Host (PowerShell resolves a function before a cmdlet, from the module and the
    script alike), so it fires at the FIRST such call after the job file is visible, wherever that is: at 4f6c17cb the Test-Path at :979; the test needs no
    seam the old code lacks. The admission-wait marker lands at um-run's queue deadline (its existing TestHookAtQueueDeadline seam), the latest an
    admission wait can start. The assertion is the caller's: the last wait um-run runs (its admission ceiling, UNRESOLVED) ends at or before the visible job's
    notAfterUtc."""
    QUEUE_SEC = 6
    ADMIT_SEC = 8
    TAIL_SLACK_SEC = 6
    STALL_SEC = 12

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="dv-na-tail-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(os.path.realpath(tmp.name))
        self.share = self.tmp / "agent"
        self.inbox = self.share / "inbox"
        for d in (self.inbox, self.share / "outbox", self.share / "running"):
            d.mkdir(parents=True)
        (self.share / "heartbeat.txt").write_text("alive", encoding="utf-8")
        local = self.tmp / "local"
        local.mkdir()
        self.job = local / "job-x.job.ps1"
        self.job.write_text("Write-Output 'hi'\n", encoding="utf-8")

    def run_tail(self, *, mode: str, stall_sec: float, marker: bool = True) -> dict:
        """mode 'um-run': um-run.ps1 end to end (budget form, the waits above); 'drop': Invoke-UmRunDrop alone with the same budget. marker=False (r6, G8):
        no admission-wait marker ever lands. Returns {threw, endUtc (the instant the call returned or threw), meta, fired (the command the stall fired in,
        '' if none), stallAtUtc, markerAtUtc, tmpChecksAfterVisible (Test-Path calls naming the job's temporary after the job was visible), loops (um-run
        wait-loop iterations, its TestHookAtLoopTop seam)}."""
        budget = self.QUEUE_SEC + self.ADMIT_SEC + self.TAIL_SLACK_SEC
        script = (
            "$ErrorActionPreference = 'Stop'\n"
            "$c = [Console]::In.ReadToEnd() | ConvertFrom-Json\n"
            f"$global:TailStall = @{{ sec = [double]$c.stall; job = '{self.inbox / 'job-x.job.ps1'}'; fired = ''; atUtc = ''; markerAtUtc = ''; tmpChecks = 0; loops = 0 }}\n"
            "function global:Invoke-TailStall([string]$Command, [object[]]$ArgList) {\n"
            "  $s = $global:TailStall\n"
            "  if (-not [IO.File]::Exists($s.job)) { return }\n"
            "  if ($Command -eq 'Test-Path' -and @($ArgList | Where-Object { [string]$_ -like '*.job.tmp' }).Count -gt 0) { $s.tmpChecks = 1 + [int]$s.tmpChecks }\n"
            "  if ($s.fired) { return }\n"
            "  $s.fired = $Command; $s.atUtc = [DateTime]::UtcNow.ToString('o')\n"
            "  if ($s.sec -gt 0) { Start-Sleep -Milliseconds ([int]($s.sec * 1000)) } }\n"
            "function global:Test-Path { Invoke-TailStall 'Test-Path' $args; Microsoft.PowerShell.Management\\Test-Path @args }\n"
            "function global:Write-Host { Invoke-TailStall 'Write-Host' $args; Microsoft.PowerShell.Utility\\Write-Host @args }\n"
            f"$marker = '{self.share / 'running' / 'job-x.started.json'}'\n"
            "$threw = ''\n"
            "try {\n"
            "  if ($c.mode -eq 'um-run') {\n"
            f"    $p = @{{ ScriptPath = '{self.job}'; AgentShare = '{self.share}'; JobId = 'job-x'; TimeoutSec = 60; PollSeconds = 1; MaxClaimedWaitSec = 60\n"
            "      MaxQueueWaitSec = [int]$c.queue; MaxAdmissionWaitSec = [int]$c.admit; NotAfterBudgetSec = [int]$c.budget\n"
            "      TestHookAtLoopTop = { $global:TailStall.loops = 1 + [int]$global:TailStall.loops } }\n"
            "    if ($c.marker) { $p.TestHookAtQueueDeadline = { if (-not [IO.File]::Exists($marker)) { [IO.File]::WriteAllText($marker, '{\"jobId\":\"job-x\",\"phase\":\"admission-wait\",\"childPid\":null}')\n"
            "        $global:TailStall.markerAtUtc = [DateTime]::UtcNow.ToString('o') } } }\n"
            f"    $null = & '{UM_RUN}' @p\n"
            "  } else {\n"
            f"    Import-Module '{DROP_MODULE}' -Force\n"
            f"    $null = Invoke-UmRunDrop -Inbox '{self.inbox}' -Outbox '{self.share / 'outbox'}' -ScriptPath '{self.job}' -JobId 'job-x' -NotAfterBudgetSec ([int]$c.budget)\n"
            "  }\n"
            "} catch { $threw = [string]$_.Exception.Message }\n"
            "$endUtc = [DateTime]::UtcNow.ToString('o')\n"
            f"$metaPath = '{self.inbox / 'job-x.meta.json'}'\n"
            "$meta = $(if ([IO.File]::Exists($metaPath)) { [IO.File]::ReadAllText($metaPath) } else { '' })\n"
            "$s = $global:TailStall\n"
            "[Console]::Out.WriteLine('TAIL_RESULT=' + (ConvertTo-Json -Compress -InputObject ([ordered]@{ threw = $threw; endUtc = $endUtc; meta = $meta;\n"
            "  fired = $s.fired; stallAtUtc = $s.atUtc; markerAtUtc = $s.markerAtUtc; tmpChecksAfterVisible = [int]$s.tmpChecks; loops = [int]$s.loops })))\n"
        )
        payload = {"mode": mode, "stall": stall_sec, "queue": self.QUEUE_SEC, "admit": self.ADMIT_SEC, "budget": budget, "marker": marker}
        p = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], input=json.dumps(payload), capture_output=True, text=True,
                           timeout=180)
        lines = [l for l in p.stdout.splitlines() if l.startswith("TAIL_RESULT=")]
        self.assertEqual(len(lines), 1, p.stdout + p.stderr)
        r = json.loads(lines[0][len("TAIL_RESULT="):])
        r["stdout"] = p.stdout
        return r

    def test_dots_tail_a_stall_after_visibility_never_runs_a_wait_past_the_visible_jobs_not_after(self) -> None:
        r = self.run_tail(mode="um-run", stall_sec=self.STALL_SEC)
        self.assertTrue(r["threw"].startswith("UNRESOLVED:") and "-MaxAdmissionWaitSec" in r["threw"], r)
        self.assertNotEqual(r["fired"], "", "the stall fired after the job became visible")
        self.assertNotEqual(r["markerAtUtc"], "", "the admission-wait marker landed at um-run's queue deadline")
        budget = self.QUEUE_SEC + self.ADMIT_SEC + self.TAIL_SLACK_SEC
        not_after = parse_o(json.loads(r["meta"])["notAfterUtc"])
        sampled = not_after - timedelta(seconds=budget)
        stalled = parse_o(r["stallAtUtc"])
        self.assertLess(stalled - sampled, timedelta(seconds=self.TAIL_SLACK_SEC), "the stall began after visibility, inside the r4 bound")
        end = parse_o(r["endUtc"])
        self.assertLessEqual(end, not_after, f"the admission wait ended {(end - not_after).total_seconds():.3f} s AFTER notAfterUtc ({utc_o(not_after)}): "
                                             f"the agent stops taking the job while um-run still waits (stall in {r['fired']} at {r['stallAtUtc']})")
        self.assertGreaterEqual(end, sampled + timedelta(seconds=self.QUEUE_SEC + self.ADMIT_SEC),
                                "the waits still run to their absolute ceiling (sampledUtc + queue + admission), never cut shorter")

    def test_the_drops_success_path_does_no_share_io_on_its_temporary_after_the_job_is_visible(self) -> None:
        # the bound on the post-visibility tail: the rename moved the temporary away, so the success path never checks or removes it again (4f6c17cb: one
        # Test-Path at :979, unbounded share I/O after visibility)
        r = self.run_tail(mode="drop", stall_sec=0)
        self.assertEqual(r["threw"], "", r)
        self.assertEqual(r["tmpChecksAfterVisible"], 0, r)
        self.assertEqual(sorted(p.name for p in self.inbox.iterdir()), ["job-x.job.ps1", "job-x.meta.json"])

    def test_grok_g8_a_stall_after_visibility_past_the_queue_wait_retracts_on_the_first_poll_before_not_after(self) -> None:
        # r6 (Grok G8, escaped-defect test): the other branch r5 relies on. A stall after visibility longer than MaxQueueWaitSec, and no claim marker ever:
        # the queue deadline (sampledUtc + queue) has already passed when um-run starts waiting, so its FIRST poll takes the existing RETRACTED path and
        # returns before notAfterUtc, never polling on past the deadline.
        self.assertGreater(self.STALL_SEC, self.QUEUE_SEC)
        r = self.run_tail(mode="um-run", stall_sec=self.STALL_SEC, marker=False)
        self.assertTrue(r["threw"].startswith("RETRACTED:"), r)
        self.assertNotEqual(r["fired"], "", "the stall fired after the job became visible")
        self.assertEqual(r["markerAtUtc"], "", "no claim marker")
        self.assertEqual(r["loops"], 1, f"um-run retracted on wait-loop iteration {r['loops']}, not its first poll")
        # the claim is withdrawn with the job, so the visible value is read from the drop's own 'job not-after:' line (um-run echoes it)
        m = re.search(r"(?m)^job not-after: job-x\.meta\.json notAfterUtc=(\S+) sampledUtc=(\S+) budgetSec=", r["stdout"])
        self.assertIsNotNone(m, r["stdout"])
        not_after, sampled = parse_o(m.group(1)), parse_o(m.group(2))
        end = parse_o(r["endUtc"])
        self.assertGreater(parse_o(r["stallAtUtc"]) + timedelta(seconds=self.STALL_SEC), sampled + timedelta(seconds=self.QUEUE_SEC),
                           "the stall ran past the queue deadline before um-run started waiting")
        self.assertLess(end, not_after, f"um-run returned {(end - not_after).total_seconds():.3f} s after notAfterUtc ({utc_o(not_after)})")
        self.assertEqual(sorted(p.name for p in self.inbox.iterdir()), [], "the job and its claim were withdrawn")

    def test_r6_each_drop_temp_removal_or_move_takes_only_the_name_this_call_created(self) -> None:
        # r6 (Dot 77250bdf): the reasons the owner-footage route guard (test_dual_venue_owner_footage_gate.py ROUTE_DELETE_ALLOWED) records for the three
        # UmRunDrop.psm1 sites hold BY CONSTRUCTION: each target variable is assigned exactly once, from this call's own nonce name in its own inbox.
        code = "\n".join(l for l in DROP_MODULE.read_text(encoding="utf-8").splitlines() if not l.lstrip().startswith("#"))
        self.assertIn("if ($JobId -and $JobId -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$') { throw \"UMRUN_JOBID_INVALID '$JobId'\" }", code)
        self.assertEqual(code.count("$nonce = [guid]::NewGuid().ToString('N')"), 1)
        for var, rhs in (("$tmp", 'Join-Path $Inbox "$id.$nonce.job.tmp"'), ("$metaTmp", 'Join-Path $Inbox "$id.$nonce.meta.tmp"'),
                         ("$repubTmp", "$metaTmp"), ("$metaFinal", 'Join-Path $Inbox "$id.meta.json"')):
            with self.subTest(var=var):
                assigns = re.findall(r"(?m)^\s*" + re.escape(var) + r"\s*=\s*(.+?)\s*$", code)
                self.assertEqual(assigns, [rhs], f"{var} is assigned once, from the created name")
        self.assertEqual(code.count("Remove-Item -LiteralPath $tmp "), 1, "one removal site for the job temp")


def run_um_run_args_cases(cases: list[dict]) -> list[dict]:
    """Each case is a Get-DvJobNotAfter-shaped result (budgetSec, notAfterUtc, capUtc, source); each answer is Get-DvUmRunNotAfterArgs' splat (sorted keys),
    or {'error': <message>} when it threw."""
    script = (
        "$cases = [Console]::In.ReadToEnd() | ConvertFrom-Json\n"
        "$out = foreach ($c in $cases) {\n"
        "  try { $a = Get-DvUmRunNotAfterArgs -NotAfter ([pscustomobject]@{ budgetSec = $c.budgetSec; notAfterUtc = ([string]$c.notAfterUtc).TrimStart('#');\n"
        "          capUtc = ([string]$c.capUtc).TrimStart('#'); source = [string]$c.source })\n"
        "        $o = [ordered]@{}; foreach ($k in @($a.Keys | Sort-Object)) { $o[$k] = $(if ($a[$k] -is [int]) { $a[$k] } else { [string]$a[$k] }) }; $o }\n"
        "  catch { [ordered]@{ error = [string]$_.Exception.Message } }\n"
        "}\n"
        "ConvertTo-Json -InputObject @($out) -Depth 4 -Compress\n"
    )
    return run_pwsh_json(script, cases)


@requires_windows_pwsh
class UmRunNotAfterArgsTests(unittest.TestCase):
    """r4 (Grok G1, card NOT-AFTER-BUDGET-NULL-FAIL-CLOSED-1): Invoke-VenueLeg.ps1 passed '-NotAfterBudgetSec $notAfter.budgetSec' unconditionally, so a
    bound with budgetSec $null bound to [int] 0, which um-run reads as 'no budget given': the job went out with NO notAfterUtc, silently. The submitter's
    um-run arguments now come from one helper that fails closed (DVE_NOT_AFTER_BUDGET_MISSING) when a bound would be dropped."""
    CAP = "#2026-10-24T07:00:00.0000000Z"

    def test_a_null_budget_with_no_absolute_value_fails_closed(self) -> None:
        for source in ("um-run-waits", "caller-deadline", ""):
            with self.subTest(source=source):
                (r,) = run_um_run_args_cases([{"budgetSec": None, "notAfterUtc": "", "capUtc": "", "source": source}])
                self.assertIn("DVE_NOT_AFTER_BUDGET_MISSING", r.get("error", ""), r)

    def test_a_zero_budget_fails_closed_too(self) -> None:
        (r,) = run_um_run_args_cases([{"budgetSec": 0, "notAfterUtc": "", "capUtc": "", "source": "um-run-waits"}])
        self.assertIn("DVE_NOT_AFTER_BUDGET_MISSING", r.get("error", ""), r)

    def test_each_form_maps_to_its_own_um_run_arguments(self) -> None:
        rs = run_um_run_args_cases([
            {"budgetSec": 7560, "notAfterUtc": "", "capUtc": "", "source": "um-run-waits"},
            {"budgetSec": 7560, "notAfterUtc": "", "capUtc": self.CAP, "source": "caller-deadline"},
            {"budgetSec": None, "notAfterUtc": self.CAP, "capUtc": "", "source": "caller-cap"},
            {"budgetSec": None, "notAfterUtc": "", "capUtc": "", "source": "later-harvest"},
        ])
        self.assertEqual(rs[0], {"NotAfterBudgetSec": 7560})
        self.assertEqual(rs[1], {"NotAfterBudgetSec": 7560, "NotAfterCapUtc": self.CAP[1:]})
        self.assertEqual(rs[2], {"NotAfterUtc": self.CAP[1:]})
        self.assertEqual(rs[3], {}, "later-harvest passes no bound, by design")

    def test_the_leg_takes_its_um_run_not_after_arguments_from_the_helper(self) -> None:
        leg = (DV / "Invoke-VenueLeg.ps1").read_text(encoding="ascii")
        self.assertTrue("Get-DvUmRunNotAfterArgs -NotAfter $notAfter" in leg, "Invoke-VenueLeg.ps1 Submit-VenueJob uses the fail-closed helper")
        self.assertFalse("-NotAfterBudgetSec $notAfter.budgetSec" in leg, "never the unconditional [int] binding again")


# The leg's um-run stand-in: records, per submit, every wait and the -NotAfterUtc it was handed, then answers like the shared STUB_UM_RUN (a healthy probe;
# the leg itself ends UNRESOLVED, which is enough: the argv is what is pinned). A non-advanced param block, like every other leg stub, so an unknown name
# would not fail binding: the record says what arrived.
RECORDING_UM_RUN = r"""
param([string]$ScriptPath,[string]$JobId,[string]$AgentShare,[int]$TimeoutSec,[int]$MaxQueueWaitSec,[int]$MaxClaimedWaitSec,[int]$MaxAdmissionWaitSec = -1,[string]$NotAfterUtc = '<absent>',
    [int]$NotAfterBudgetSec = -1,[string]$NotAfterCapUtc = '<absent>')
$cfg = Get-Content -LiteralPath $env:DVE_STUB -Raw | ConvertFrom-Json
$rec = [ordered]@{ jobId = $JobId; calledUtc = [DateTime]::UtcNow.ToString('o'); timeoutSec = $TimeoutSec; maxQueueWaitSec = $MaxQueueWaitSec
    maxClaimedWaitSec = $MaxClaimedWaitSec; maxAdmissionWaitSec = $MaxAdmissionWaitSec; notAfterUtc = $NotAfterUtc; notAfterBudgetSec = $NotAfterBudgetSec
    notAfterCapUtc = $NotAfterCapUtc; extra = @($args | ForEach-Object { [string]$_ }) }
Add-Content -LiteralPath $env:NOT_AFTER_LOG -Value ($rec | ConvertTo-Json -Compress)
if ($JobId -like '*-health') { return [pscustomobject]@{ exitCode = 0; stdout = ('DVE_PROBE=' + ($cfg.probe | ConvertTo-Json -Compress)) } }
throw 'UNRESOLVED: stub agent may still own the job'
"""

# The delayed-drop stand-in (LEASE-NOT-AFTER-SUBMITTER-DEADLINE-TIMING-1): like RECORDING_UM_RUN, but its drop takes time. It sleeps DROP_DELAY_MS (um-run's
# own setup and share I/O) and then prints the lines real um-run prints once the job file is in place (Write-Host): r2, the drop's 'job not-after:' line, its
# notAfterUtc counted from the drop's own clock as UmRunDrop.psm1 does (the visible instant + -NotAfterBudgetSec), then 'submitted <id> -> <path>'.
# DROP_STAMP_AFTER_SEC places that drop (the leg's, not the probe's) that many seconds after the call instead and stamps both records then: a drop later than
# a test should sleep for. DROP_IGNORES_BUDGET=1 makes the leg's drop DEFECTIVE: it counts the budget from the call, not from the clock read it reports
# (r6: sampledUtc stays the visible instant). r6 (Grok G7): DROP_SAMPLED_AT_CALL=1 is an HONEST drop that read its clock at the call and counted from it,
# its stamped records coming later (a stall after visibility).
# Advanced, so it can write the stamped records itself; the record says when the job became visible (visibleUtc, never later than the records' own time).
DROP_UM_RUN = r"""
[CmdletBinding()] param([string]$ScriptPath, [string]$JobId, [string]$AgentShare, [int]$TimeoutSec, [int]$MaxQueueWaitSec, [int]$MaxClaimedWaitSec,
    [int]$MaxAdmissionWaitSec = -1, [string]$NotAfterUtc = '<absent>', [int]$NotAfterBudgetSec = -1)
$cfg = Get-Content -LiteralPath $env:DVE_STUB -Raw | ConvertFrom-Json
$called = [DateTime]::UtcNow
if ($env:DROP_DELAY_MS) { Start-Sleep -Milliseconds ([int]$env:DROP_DELAY_MS) }
$stamped = $env:DROP_STAMP_AFTER_SEC -and $JobId -notlike '*-health'
$visible = $(if ($stamped) { $called.AddSeconds([double]$env:DROP_STAMP_AFTER_SEC) } else { [DateTime]::UtcNow })
$sampled = $(if ($env:DROP_SAMPLED_AT_CALL -and $JobId -notlike '*-health') { $called } else { $visible })
$from = $(if ($env:DROP_IGNORES_BUDGET -and $JobId -notlike '*-health') { $called } else { $sampled })
$dropNotAfter = $from.AddSeconds($NotAfterBudgetSec).ToString('o')
$lines = @("job not-after: $JobId.meta.json notAfterUtc=$dropNotAfter sampledUtc=$($sampled.ToString('o')) budgetSec=$NotAfterBudgetSec capUtc=none",
    "submitted $JobId -> $AgentShare\inbox\$JobId.job.ps1")
foreach ($line in $lines) {
    if ($stamped) {
        $rec = [System.Management.Automation.InformationRecord]::new([System.Management.Automation.HostInformationMessage]@{ Message = $line }, 'um-run.ps1')
        $rec.TimeGenerated = $visible.ToLocalTime()
        $PSCmdlet.WriteInformation($rec)
    } else { Write-Host $line }
}
$rec = [ordered]@{ jobId = $JobId; calledUtc = $called.ToString('o'); visibleUtc = $visible.ToString('o'); timeoutSec = $TimeoutSec; maxQueueWaitSec = $MaxQueueWaitSec
    maxClaimedWaitSec = $MaxClaimedWaitSec; maxAdmissionWaitSec = $MaxAdmissionWaitSec; notAfterUtc = $NotAfterUtc; notAfterBudgetSec = $NotAfterBudgetSec
    dropNotAfterUtc = $dropNotAfter }
Add-Content -LiteralPath $env:NOT_AFTER_LOG -Value ($rec | ConvertTo-Json -Compress)
if ($JobId -like '*-health') { return [pscustomobject]@{ exitCode = 0; stdout = ('DVE_PROBE=' + ($cfg.probe | ConvertTo-Json -Compress)) } }
throw 'UNRESOLVED: stub agent may still own the job'
"""


def not_after_lines(out: str) -> list[dict]:
    """The leg's DVE_NOT_AFTER=<json> lines, one per submit, in order."""
    return [json.loads(l[len("DVE_NOT_AFTER="):]) for l in out.splitlines() if l.startswith("DVE_NOT_AFTER=")]


@requires_windows_pwsh
class VenueLegNotAfterTests(unittest.TestCase):
    def setUp(self) -> None:
        from tools.repo_hygiene import test_dual_venue_evidence as dve
        self.dve = dve
        self.h = dve.RunnerHarness()
        self.h.addCleanup = self.addCleanup
        self.h.make_harness()
        self.h.um.write_text(RECORDING_UM_RUN, encoding="utf-8")
        self.record = self.h.tmp / "not-after.jsonl"

    def run_leg(self, env: dict[str, str] | None = None) -> tuple[list[dict], datetime, datetime, str]:
        spec = self.h.write_spec()
        cfg = {"log": str(self.h.log), "genLog": str(self.h.gen_log), "probe": self.dve.HEALTHY_PROBE, "genRefusal": None,
               "clipContentSha256": self.dve.CLIP_CONTENT_SHA}
        self.h.stub_cfg.write_text(json.dumps(cfg), encoding="utf-8")
        before = datetime.now(timezone.utc)
        proc = self.dve.run_pwsh(["-File", str(DV / "Invoke-VenueLeg.ps1"), "-Venue", "ultra-magnus", "-LegSpec", str(spec), "-SourceCommit", self.h.sha,
                                  "-BuildManifestSha256", self.h.build_sha, "-VenueTablePath", str(self.h.table), "-ReceiptRoot", str(self.h.receipts),
                                  "-OfflineTestMode", "-UmRunScript", str(self.h.um), "-GeneratorScript", str(self.h.gen), "-WorkDir", str(self.h.tmp / "work"),
                                  "-ConsentPath", str(self.h.consent), "-RepoRoot", str(ROOT), "-Actor", "unit-test"],
                                 env_extra={"DVE_STUB": str(self.h.stub_cfg), "NOT_AFTER_LOG": str(self.record), **(env or {})})
        after = datetime.now(timezone.utc)
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, out)
        recs = [json.loads(l) for l in self.record.read_text(encoding="utf-8").splitlines() if l.strip()] if self.record.exists() else []
        return recs, before, after, out

    def test_both_submits_pass_a_not_after_that_covers_every_wait_they_pass(self) -> None:
        recs, before, after, out = self.run_leg()
        self.assertIn("DVE_OUTCOME=UNRESOLVED", out)
        self.assertEqual([r["jobId"].endswith("-health") for r in recs], [True, False], recs)
        probe, leg = recs
        # the waits each submit hands um-run (the probe's are constants; the leg's come from the spec's timeouts and the generator's job budget)
        self.assertEqual((probe["timeoutSec"], probe["maxQueueWaitSec"], probe["maxClaimedWaitSec"]), (120, 120, 60))
        self.assertEqual((leg["timeoutSec"], leg["maxQueueWaitSec"], leg["maxClaimedWaitSec"]), (600 + 120, 60, 60))
        for r in recs:
            with self.subTest(job=r["jobId"]):
                self.assertEqual(r["extra"], [], "every argument binds by name")
                self.assertEqual(r["maxAdmissionWaitSec"], UMRUN_ADMISSION_DEFAULT, "the admission wait the bound assumes is passed by name (um-run's default)")
                # r2 (hub ruling): the bound is handed DROP-RELATIVE, the waits plus the slack, for the drop to count from its own clock; never absolute
                waits = r["timeoutSec"] + r["maxQueueWaitSec"] + r["maxAdmissionWaitSec"] + r["maxClaimedWaitSec"]
                self.assertEqual(r["notAfterBudgetSec"], waits + SLACK_SEC, "the submitter passes -NotAfterBudgetSec: every wait it passes plus the slack")
                self.assertEqual(r["notAfterUtc"], "<absent>", "no absolute -NotAfterUtc: it would count from before the drop")
                self.assertEqual(r["notAfterCapUtc"], "<absent>", "the leg has no kill instant of its own")
        # this stub prints neither the 'job not-after:' nor the 'submitted' line: the value is unreported, which is said and never counted as covered
        verdicts = not_after_lines(out)
        self.assertEqual([v["jobId"] for v in verdicts], [r["jobId"] for r in recs], out)
        for v in verdicts:
            self.assertIsNone(v["covered"], v)
            self.assertEqual((v["form"], v["notAfterUtc"], v["dropVisibleUtc"]), ("drop-relative", "", ""), v)

    # ---------------------------------------------------------------- LEASE-NOT-AFTER-SUBMITTER-DEADLINE-TIMING-1: the drop comes after the bound
    def test_a_slow_drop_is_measured_from_um_runs_own_line_and_still_covered(self) -> None:
        self.h.um.write_text(DROP_UM_RUN, encoding="utf-8")
        recs, before, after, out = self.run_leg(env={"DROP_DELAY_MS": "1500"})
        self.assertIn("DVE_OUTCOME=UNRESOLVED", out)
        verdicts = not_after_lines(out)
        self.assertEqual([v["jobId"] for v in verdicts], [r["jobId"] for r in recs], out)
        self.assertEqual(len(verdicts), 2)
        for r, v in zip(recs, verdicts):
            with self.subTest(job=r["jobId"]):
                drop = parse_o(v["dropVisibleUtc"])
                visible = parse_o(r["visibleUtc"])
                # the record is written after the job file is in place: never earlier than the drop, and not long after it
                self.assertGreaterEqual(drop, visible - timedelta(milliseconds=1))
                self.assertLess(drop, visible + timedelta(seconds=5))
                self.assertGreaterEqual(v["dropDelaySec"], 1.5 - 0.05, "the 1.5 s drop is measured from the bound's own anchor")
                self.assertGreaterEqual(parse_o(v["dropVisibleUtc"]), parse_o(v["anchorUtc"]))
                na = parse_o(v["notAfterUtc"])
                self.assertEqual(na, parse_o(r["dropNotAfterUtc"]), "the verdict judges the value the drop wrote, read from its own line")
                self.assertEqual(v["form"], "drop-relative")
                waits = r["timeoutSec"] + r["maxQueueWaitSec"] + r["maxAdmissionWaitSec"] + r["maxClaimedWaitSec"]
                self.assertEqual(v["waitSec"], waits)
                self.assertGreaterEqual(na, drop + timedelta(seconds=waits), "never earlier than the drop plus every um-run wait")
                self.assertTrue(v["covered"], v)
        self.assertNotIn("DVE_NOT_AFTER_DROP_LATE", out)

    def test_a_drop_far_later_than_the_slack_is_still_covered_a_priori(self) -> None:
        # r1 accepted covered=false here (a 90 s drop named DVE_NOT_AFTER_DROP_LATE). r2 (hub ruling on sol's blocker): the drop counts the budget from its
        # own clock, so a drop 300 s late (sol's repro delay) is still covered, and nothing is named late.
        self.h.um.write_text(DROP_UM_RUN, encoding="utf-8")
        recs, before, after, out = self.run_leg(env={"DROP_STAMP_AFTER_SEC": "300"})
        self.assertIn("DVE_OUTCOME=UNRESOLVED", out)
        probe, leg = not_after_lines(out)
        self.assertTrue(probe["covered"], probe)
        self.assertTrue(leg["covered"], leg)
        self.assertGreaterEqual(leg["dropDelaySec"], 300)
        self.assertAlmostEqual(leg["marginSec"], SLACK_SEC, places=2)
        self.assertGreaterEqual(parse_o(leg["notAfterUtc"]), parse_o(leg["dropVisibleUtc"]) + timedelta(seconds=leg["waitSec"]))
        self.assertNotIn("DVE_NOT_AFTER_DROP_LATE", out)

    def test_a_drop_that_does_not_count_from_itself_is_named_a_defect_never_silent(self) -> None:
        # The witness stays: a drop that counted the budget from before it (here, from the call, 300 s before the job became visible) leaves the bound short
        # of um-run's waits, and that is now a DEFECT signal, named before the outcome.
        self.h.um.write_text(DROP_UM_RUN, encoding="utf-8")
        recs, before, after, out = self.run_leg(env={"DROP_STAMP_AFTER_SEC": "300", "DROP_IGNORES_BUDGET": "1"})
        self.assertIn("DVE_OUTCOME=UNRESOLVED", out)
        probe, leg = not_after_lines(out)
        self.assertTrue(probe["covered"], probe)
        self.assertFalse(leg["covered"], leg)
        self.assertLessEqual(leg["marginSec"], SLACK_SEC - 300 + 0.01)
        self.assertIn("DVE_NOT_AFTER_DROP_LATE (defect", leg["why"])
        late = [l for l in out.splitlines() if l.startswith("DVE_NOT_AFTER_DROP_LATE ")]
        self.assertEqual(len(late), 1, out)
        self.assertIn(f"job={recs[1]['jobId']} ", late[0] + " ")
        self.assertRegex(late[0], r" shortBySec=\d+(\.\d+)? ")
        # named BEFORE the leg's outcome, so a reader of a refused job (the agent's notAfterExpired) sees why first
        lines = out.splitlines()
        self.assertLess(lines.index(late[0]), next(i for i, l in enumerate(lines) if l.startswith("DVE_OUTCOME=")))

    def test_grok_g7_a_stall_after_visibility_is_no_false_alarm_end_to_end(self) -> None:
        # r6 (Grok G7): an honest drop read its clock at the call and counted from it; um-run's lines are stamped 300 s later (a stall after visibility,
        # longer than the slack). um-run r5 counts its waits from that read, so nothing overran: covered, margin = the slack, nothing named late. At
        # 4ea77b88 the leg printed DVE_NOT_AFTER_DROP_LATE here (shortBySec 240).
        self.h.um.write_text(DROP_UM_RUN, encoding="utf-8")
        recs, before, after, out = self.run_leg(env={"DROP_STAMP_AFTER_SEC": "300", "DROP_SAMPLED_AT_CALL": "1"})
        self.assertIn("DVE_OUTCOME=UNRESOLVED", out)
        probe, leg = not_after_lines(out)
        self.assertTrue(probe["covered"], probe)
        self.assertGreaterEqual(leg["dropDelaySec"], 300)
        self.assertEqual(parse_o(leg["dropSampledUtc"]), parse_o(recs[1]["calledUtc"]))
        self.assertTrue(leg["covered"], leg)
        self.assertAlmostEqual(leg["marginSec"], SLACK_SEC, places=2)
        self.assertNotIn("DVE_NOT_AFTER_DROP_LATE", out)

    def test_the_submits_still_print_their_id_first_and_go_through_one_helper(self) -> None:
        text = (DV / "Invoke-VenueLeg.ps1").read_text(encoding="utf-8")
        fn = text[text.index("function Submit-VenueJob("):text.index("function ConvertTo-ShareSidePath(")]
        code = "\n".join(l for l in fn.splitlines() if not l.strip().startswith("#"))
        self.assertEqual(code.count("Get-DvJobNotAfter"), 1, "the one helper computes the bound")
        # r2: handed drop-relative; r4 (Grok G1): through the fail-closed helper, which maps the leg's 'um-run-waits' bound to -NotAfterBudgetSec
        self.assertIn("$notAfterArgs = Get-DvUmRunNotAfterArgs -NotAfter $notAfter", code)
        self.assertIn("@notAfterArgs", code)
        self.assertNotIn("-NotAfterUtc", code, "r2: never the absolute form, which counts from before the drop")
        self.assertIn("-MaxAdmissionWaitSec", code)
        self.assertIn("Get-DvUmRunDropNotAfter", code, "the witness judges the drop's own value")
        self.assertNotRegex(code, r"AddSeconds|UtcNow", "no arithmetic of its own: the helper and the drop own it")


if __name__ == "__main__":
    unittest.main()
