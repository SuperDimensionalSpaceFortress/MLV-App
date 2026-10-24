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
        "    if ($prop.Name -eq 'NowUtc') { $p['NowUtc'] = $(if ($prop.Value -is [datetime]) { $prop.Value } else { [DateTime]::Parse([string]$prop.Value, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind) }) }\n"
        "    elseif ($prop.Name -eq 'LaterHarvest') { $p['LaterHarvest'] = [bool]$prop.Value }\n"
        "    else { $p[$prop.Name] = [int]$prop.Value }\n"
        "  }\n"
        "  try { $r = Get-DvJobNotAfter @p; [ordered]@{ notAfterUtc = [string]$r.notAfterUtc; source = $r.source; boundSec = $r.boundSec; waitSec = $r.waitSec;\n"
        "        longestWaitSec = $r.longestWaitSec; maxAdmissionWaitSec = $r.maxAdmissionWaitSec; why = $r.why } }\n"
        "  catch { [ordered]@{ error = [string]$_.Exception.Message } }\n"
        "}\n"
        "ConvertTo-Json -InputObject @($out) -Depth 4 -Compress\n"
    )
    p = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], input=json.dumps(cases), capture_output=True, text=True,
                       timeout=120)
    assert p.returncode == 0, p.stdout + p.stderr
    answers = json.loads(p.stdout.strip().splitlines()[-1])
    return answers if isinstance(answers, list) else [answers]


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

    def run_leg(self) -> tuple[list[dict], datetime, datetime, str]:
        spec = self.h.write_spec()
        cfg = {"log": str(self.h.log), "genLog": str(self.h.gen_log), "probe": self.dve.HEALTHY_PROBE, "genRefusal": None,
               "clipContentSha256": self.dve.CLIP_CONTENT_SHA}
        self.h.stub_cfg.write_text(json.dumps(cfg), encoding="utf-8")
        before = datetime.now(timezone.utc)
        proc = self.dve.run_pwsh(["-File", str(DV / "Invoke-VenueLeg.ps1"), "-Venue", "ultra-magnus", "-LegSpec", str(spec), "-SourceCommit", self.h.sha,
                                  "-BuildManifestSha256", self.h.build_sha, "-VenueTablePath", str(self.h.table), "-ReceiptRoot", str(self.h.receipts),
                                  "-OfflineTestMode", "-UmRunScript", str(self.h.um), "-GeneratorScript", str(self.h.gen), "-WorkDir", str(self.h.tmp / "work"),
                                  "-ConsentPath", str(self.h.consent), "-RepoRoot", str(ROOT), "-Actor", "unit-test"],
                                 env_extra={"DVE_STUB": str(self.h.stub_cfg), "NOT_AFTER_LOG": str(self.record)})
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

    def test_the_submits_still_print_their_id_first_and_go_through_one_helper(self) -> None:
        text = (DV / "Invoke-VenueLeg.ps1").read_text(encoding="utf-8")
        fn = text[text.index("function Submit-VenueJob("):text.index("function ConvertTo-ShareSidePath(")]
        self.assertEqual(fn.count("Get-DvJobNotAfter"), 1, "the one helper computes the bound")
        self.assertIn("-NotAfterUtc", fn)
        self.assertIn("-MaxAdmissionWaitSec", fn)
        self.assertNotRegex(fn, r"AddSeconds|UtcNow", "no arithmetic of its own: the helper owns it")


if __name__ == "__main__":
    unittest.main()
