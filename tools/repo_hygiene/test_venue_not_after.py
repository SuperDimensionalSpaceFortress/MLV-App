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

Offline only: the leg runs in -OfflineTestMode with a stub um-run (Invoke-VenueLeg refuses the real um-run.ps1 there), through the shared RunnerHarness of
test_dual_venue_evidence.py. Nothing reaches a venue. Windows-only (PowerShell tools).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DV = ROOT / "tools" / "profiling" / "dual-venue"
RUNNER_MODULE = DV / "DualVenueRunner.psm1"
UM_RUN = ROOT / "tools" / "profiling" / "um-run.ps1"
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
        "        dropToleranceSec = $r.dropToleranceSec; why = $r.why } }\n"
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
    """Each case: {'bound': <Get-DvJobNotAfter parameters, NowUtc as 'o' text>, 'dropAfterSec': <seconds after the bound's anchor, or None>}. The drop is
    placed that many seconds after the anchor (the delayed-drop seam) and Test-DvNotAfterCoversDrop judges it; each answer is its result plus the bound."""
    script = (
        "$cases = [Console]::In.ReadToEnd() | ConvertFrom-Json\n"
        "$out = foreach ($c in $cases) {\n"
        "  $p = @{}\n"
        "  foreach ($prop in $c.bound.PSObject.Properties) {\n"
        "    if ($prop.Name -in @('NowUtc', 'CallerDeadlineUtc')) { $p[$prop.Name] = $(if ($prop.Value -is [datetime]) { $prop.Value } else { [DateTime]::Parse([string]$prop.Value, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind) }) }\n"
        "    elseif ($prop.Name -eq 'LaterHarvest') { $p['LaterHarvest'] = [bool]$prop.Value }\n"
        "    else { $p[$prop.Name] = [int]$prop.Value }\n"
        "  }\n"
        "  try {\n"
        "    $b = Get-DvJobNotAfter @p\n"
        "    $drop = $(if ($null -eq $c.dropAfterSec) { $null } else { [DateTime]::Parse([string]$b.anchorUtc, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind).AddSeconds([double]$c.dropAfterSec) })\n"
        "    $v = Test-DvNotAfterCoversDrop -NotAfter $b -DropVisibleUtc $drop -JobId 'job-x'\n"
        "    [ordered]@{ jobId = $v.jobId; notAfterUtc = [string]$v.notAfterUtc; source = $v.source; anchorUtc = [string]$v.anchorUtc; waitSec = $v.waitSec;\n"
        "      dropToleranceSec = $v.dropToleranceSec; dropVisibleUtc = [string]$v.dropVisibleUtc; dropDelaySec = $v.dropDelaySec; marginSec = $v.marginSec;\n"
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

    def test_the_drop_judge_and_the_record_reader_are_pure_and_ascii(self) -> None:
        text = RUNNER_MODULE.read_text(encoding="utf-8")
        for fn in ("Test-DvNotAfterCoversDrop", "Get-DvUmRunDropVisibleUtc"):
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

    def test_covered_exactly_when_not_after_is_at_least_the_drop_plus_every_wait(self) -> None:
        cases = [0, 2.5, 30, 59.9, 60, 60.5, 61, 300, 3600]
        answers = run_cover_cases([{"bound": dict(self.UNCAPPED, NowUtc=self.NOW), "dropAfterSec": d} for d in cases])
        for d, r in zip(cases, answers):
            with self.subTest(dropAfterSec=d):
                self.assertNotIn("error", r, r)
                na = parse_o(r["notAfterUtc"])
                drop = parse_o(r["dropVisibleUtc"])
                # (.NET's AddSeconds(double) can land one tick short of a decimal fraction)
                self.assertLess(abs(drop - (parse_o(self.NOW) + timedelta(seconds=d))), timedelta(milliseconds=1))
                self.assertAlmostEqual(r["dropDelaySec"], d, places=3)
                waits = timedelta(seconds=r["waitSec"])
                # item 1: notAfterUtc never earlier than the moment the job is visible plus every um-run wait -- or the judge says it is
                self.assertEqual(r["covered"], na >= drop + waits, r)
                self.assertAlmostEqual(r["marginSec"], (na - (drop + waits)).total_seconds(), places=3)
                self.assertEqual(r["covered"], d <= SLACK_SEC)
                if not r["covered"]:
                    self.assertIn("DVE_NOT_AFTER_DROP_LATE", r["why"])

    def test_an_unmeasured_drop_is_never_covered(self) -> None:
        r = self.cover(self.UNCAPPED, None)
        self.assertIsNone(r["covered"], "unknown is never covered")
        self.assertEqual(r["dropVisibleUtc"], "")
        self.assertIn("unmeasured", r["why"])

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


# The leg's um-run stand-in: records, per submit, every wait and the -NotAfterUtc it was handed, then answers like the shared STUB_UM_RUN (a healthy probe;
# the leg itself ends UNRESOLVED, which is enough: the argv is what is pinned). A non-advanced param block, like every other leg stub, so an unknown name
# would not fail binding: the record says what arrived.
RECORDING_UM_RUN = r"""
param([string]$ScriptPath,[string]$JobId,[string]$AgentShare,[int]$TimeoutSec,[int]$MaxQueueWaitSec,[int]$MaxClaimedWaitSec,[int]$MaxAdmissionWaitSec = -1,[string]$NotAfterUtc = '<absent>')
$cfg = Get-Content -LiteralPath $env:DVE_STUB -Raw | ConvertFrom-Json
$rec = [ordered]@{ jobId = $JobId; calledUtc = [DateTime]::UtcNow.ToString('o'); timeoutSec = $TimeoutSec; maxQueueWaitSec = $MaxQueueWaitSec
    maxClaimedWaitSec = $MaxClaimedWaitSec; maxAdmissionWaitSec = $MaxAdmissionWaitSec; notAfterUtc = $NotAfterUtc; extra = @($args | ForEach-Object { [string]$_ }) }
Add-Content -LiteralPath $env:NOT_AFTER_LOG -Value ($rec | ConvertTo-Json -Compress)
if ($JobId -like '*-health') { return [pscustomobject]@{ exitCode = 0; stdout = ('DVE_PROBE=' + ($cfg.probe | ConvertTo-Json -Compress)) } }
throw 'UNRESOLVED: stub agent may still own the job'
"""

# The delayed-drop stand-in (LEASE-NOT-AFTER-SUBMITTER-DEADLINE-TIMING-1): like RECORDING_UM_RUN, but its drop takes time. It sleeps DROP_DELAY_MS (um-run's
# own setup and share I/O) and then prints the line real um-run prints once the job file is in place ('submitted <id> -> <path>', Write-Host).
# DROP_STAMP_AFTER_SEC stamps that record (the leg's, not the probe's) that many seconds after the call instead: a drop later than a test should sleep for.
# Advanced, so it can write the stamped record itself; the record says when the job became visible (visibleUtc, never later than the record's own time).
DROP_UM_RUN = r"""
[CmdletBinding()] param([string]$ScriptPath, [string]$JobId, [string]$AgentShare, [int]$TimeoutSec, [int]$MaxQueueWaitSec, [int]$MaxClaimedWaitSec,
    [int]$MaxAdmissionWaitSec = -1, [string]$NotAfterUtc = '<absent>')
$cfg = Get-Content -LiteralPath $env:DVE_STUB -Raw | ConvertFrom-Json
$called = [DateTime]::UtcNow
if ($env:DROP_DELAY_MS) { Start-Sleep -Milliseconds ([int]$env:DROP_DELAY_MS) }
$line = "submitted $JobId -> $AgentShare\inbox\$JobId.job.ps1"
if ($env:DROP_STAMP_AFTER_SEC -and $JobId -notlike '*-health') {
    $visible = $called.AddSeconds([double]$env:DROP_STAMP_AFTER_SEC)
    $rec = [System.Management.Automation.InformationRecord]::new([System.Management.Automation.HostInformationMessage]@{ Message = $line }, 'um-run.ps1')
    $rec.TimeGenerated = $visible.ToLocalTime()
    $PSCmdlet.WriteInformation($rec)
} else {
    $visible = [DateTime]::UtcNow
    Write-Host $line
}
$rec = [ordered]@{ jobId = $JobId; calledUtc = $called.ToString('o'); visibleUtc = $visible.ToString('o'); timeoutSec = $TimeoutSec; maxQueueWaitSec = $MaxQueueWaitSec
    maxClaimedWaitSec = $MaxClaimedWaitSec; maxAdmissionWaitSec = $MaxAdmissionWaitSec; notAfterUtc = $NotAfterUtc }
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
                self.assertNotEqual(r["notAfterUtc"], "<absent>", "the submitter passes -NotAfterUtc")
                self.assertRegex(r["notAfterUtc"], O_UTC)
                na = parse_o(r["notAfterUtc"])
                called = parse_o(r["calledUtc"])
                waits = r["timeoutSec"] + r["maxQueueWaitSec"] + r["maxAdmissionWaitSec"] + r["maxClaimedWaitSec"]
                self.assertGreater(na, called, "later than now")
                self.assertGreaterEqual(na, called + timedelta(seconds=waits), "never earlier than the client still waits (queue + admission + timeout + claimed)")
                # equal to the helper's value: the sum plus the slack, from an instant inside this run
                self.assertGreaterEqual(na, before + timedelta(seconds=waits + SLACK_SEC) - timedelta(milliseconds=1))
                self.assertLessEqual(na, after + timedelta(seconds=waits + SLACK_SEC))
        # this stub prints no 'submitted' line: the drop is unmeasured, which is reported as such and never as covered
        verdicts = not_after_lines(out)
        self.assertEqual([v["jobId"] for v in verdicts], [r["jobId"] for r in recs], out)
        for v in verdicts:
            self.assertIsNone(v["covered"], v)
            self.assertEqual(v["dropVisibleUtc"], "")

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
                self.assertEqual(na, parse_o(r["notAfterUtc"]), "the verdict judges the value um-run was handed")
                waits = r["timeoutSec"] + r["maxQueueWaitSec"] + r["maxAdmissionWaitSec"] + r["maxClaimedWaitSec"]
                self.assertEqual(v["waitSec"], waits)
                self.assertGreaterEqual(na, drop + timedelta(seconds=waits), "never earlier than the drop plus every um-run wait")
                self.assertTrue(v["covered"], v)
        self.assertNotIn("DVE_NOT_AFTER_DROP_LATE", out)

    def test_a_drop_later_than_the_tolerance_is_named_never_silent(self) -> None:
        self.h.um.write_text(DROP_UM_RUN, encoding="utf-8")
        recs, before, after, out = self.run_leg(env={"DROP_STAMP_AFTER_SEC": "90"})
        self.assertIn("DVE_OUTCOME=UNRESOLVED", out)
        probe, leg = not_after_lines(out)
        self.assertTrue(probe["covered"], probe)
        self.assertFalse(leg["covered"], leg)
        self.assertGreaterEqual(leg["dropDelaySec"], 90)
        self.assertLessEqual(leg["marginSec"], SLACK_SEC - 90)
        late = [l for l in out.splitlines() if l.startswith("DVE_NOT_AFTER_DROP_LATE ")]
        self.assertEqual(len(late), 1, out)
        self.assertIn(f"job={recs[1]['jobId']} ", late[0] + " ")
        self.assertRegex(late[0], r" shortBySec=\d+(\.\d+)? ")
        # named BEFORE the leg's outcome, so a reader of a refused job (the agent's notAfterExpired) sees why first
        lines = out.splitlines()
        self.assertLess(lines.index(late[0]), next(i for i, l in enumerate(lines) if l.startswith("DVE_OUTCOME=")))

    def test_the_submits_still_print_their_id_first_and_go_through_one_helper(self) -> None:
        text = (DV / "Invoke-VenueLeg.ps1").read_text(encoding="utf-8")
        fn = text[text.index("function Submit-VenueJob("):text.index("function ConvertTo-ShareSidePath(")]
        self.assertEqual(fn.count("Get-DvJobNotAfter"), 1, "the one helper computes the bound")
        self.assertIn("-NotAfterUtc", fn)
        self.assertIn("-MaxAdmissionWaitSec", fn)
        self.assertNotRegex(fn, r"AddSeconds|UtcNow", "no arithmetic of its own: the helper owns it")


if __name__ == "__main__":
    unittest.main()
