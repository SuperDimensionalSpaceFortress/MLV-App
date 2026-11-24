"""VENUE-LEG-DISPLAY-PATH-RECEIPT-1: every venue leg receipt carries `venue.displayPath`, the display path the venue drove at leg time.

WHY. The m16-1457-native-fullscreen-s1 CUDA control on Bachelor went from 59.56 fps (display p50 16.67 ms) to 38.78 fps (p50 33.22 ms) with a byte-identical exe,
clip, spec, launch arguments and environment (BACHELOR-1457-NATIVE-GUI-LATE-ENV-DIFF-1 r1). The difference is venue state: Bachelor drives its only display through
the Intel Iris Xe at 3840x2400 / 59 Hz with the NVIDIA GPU headless, and Windows Update rebooted it. No receipt recorded any of that (`venue.driverVersion` is the first
video controller's driver only).

WHAT IS PINNED
  * the venue-side collector (Get-DvDisplayPathCollectorSource -> Get-DvDisplayPath) is READ-ONLY (CIM reads and one registry read, no write verb), bounded to 15 s for
    the whole block, and a failed / timed-out / denied probe is a typed `{"state":"unknown","reason":...}` for THAT field, never a throw;
  * the Bachelor shape reads right: the headless NVIDIA adapter has no active output, the Intel adapter is the one active display at 59 Hz;
  * the validator (Test-DvReceiptValid) reports `displayPath` = absent | present | malformed. A receipt without the block is still valid (older receipts exist); a
    present but malformed block is INVALID; a block whose every probe failed is valid and present;
  * Invoke-VenueLeg copies the probe's block into the receipt through ConvertTo-DvDisplayPath, which never lets a bad block change the leg's verdict.
"""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from tools.repo_hygiene.test_dual_venue_evidence import (
    DV, HEALTHY_PROBE, ROOT, STUB_UM_RUN, RunnerHarness, requires_windows_pwsh, run_pwsh,
)

MODULE = DV / "DualVenueRunner.psm1"
SCHEMA = "mlv-app/venue-display-path/v1"


def unknown(reason: str) -> dict:
    return {"state": "unknown", "reason": reason}


def good_block() -> dict:
    """What the collector writes on Bachelor after the 2026-11-21 reboot: Intel Iris Xe drives the display, the NVIDIA RTX 3060 is headless."""
    return {
        "schema": SCHEMA,
        "collectedUtc": "2026-11-24T16:40:00Z",
        "adapters": [
            {"name": "Intel(R) Iris(R) Xe Graphics", "driverVersion": "31.0.101.5186", "driverDate": "2024-06-12T00:00:00Z",
             "pnpDeviceId": "PCI\\VEN_8086&DEV_9A49\\3&11583659&0&10", "hasActiveOutput": True},
            {"name": "NVIDIA GeForce RTX 3060 Laptop GPU", "driverVersion": "32.0.15.6094", "driverDate": "2024-09-01T00:00:00Z",
             "pnpDeviceId": "PCI\\VEN_10DE&DEV_2560\\4&2C0D5D9&0&0008", "hasActiveOutput": False},
        ],
        "activeDisplays": [
            {"adapterName": "Intel(R) Iris(R) Xe Graphics", "refreshHz": 59, "horizontalResolution": 3840, "verticalResolution": 2400},
        ],
        "os": {"build": "26100", "ubr": 4770, "lastBootUtc": "2026-11-21T09:00:00Z", "hotfixCount": 14},
        "powerPlan": "Balanced",
    }


def all_unknown_block() -> dict:
    """Every probe failed: each field is a typed unknown, the block still has its shape."""
    return {
        "schema": SCHEMA,
        "collectedUtc": "2026-11-24T16:40:00Z",
        "adapters": unknown("RPC server unavailable"),
        "activeDisplays": unknown("adapters-unreadable"),
        "os": {"build": unknown("registry-unreadable"), "ubr": unknown("registry-unreadable"),
               "lastBootUtc": unknown("RPC server unavailable"), "hotfixCount": unknown("budget-exhausted")},
        "powerPlan": unknown("access denied"),
    }


def receipt_with(block, outcome: str = "UNRESOLVED", **venue_over) -> dict:
    venue = {"name": "bachelor", "role": "supplementary", "declared": "bachelor", "hostName": "BACHELOR", "gpuNames": ["Intel(R) Iris(R) Xe Graphics"]}
    venue.update(venue_over)
    if block is not None:
        venue["displayPath"] = block
    return {"schema": "mlv-app/dual-venue-receipt/v1", "outcome": outcome, "venue": venue, "subject": {}}


def pwsh_json(script: str, env: dict | None = None) -> dict:
    proc = run_pwsh(["-Command", f"$ErrorActionPreference = 'Stop'\nImport-Module '{MODULE}' -Force\n" + script], env_extra=env or {})
    try:
        return json.loads(proc.stdout)
    except ValueError:
        raise AssertionError(proc.stdout + proc.stderr)


def validate(receipt: dict) -> dict:
    return pwsh_json("$r = $env:DVE_RECEIPT | ConvertFrom-Json\n$v = Test-DvReceiptValid -Receipt $r\n"
                     "[ordered]@{ status = $v.status; valid = $v.valid; displayPath = $v.displayPath; reasons = @($v.reasons); unbound = @($v.unbound) } | ConvertTo-Json -Compress -Depth 4",
                     {"DVE_RECEIPT": json.dumps(receipt)})


def block_state(block) -> dict:
    return pwsh_json("$b = $env:DVE_BLOCK | ConvertFrom-Json\n$v = Test-DvDisplayPathBlock -Block $b\n[ordered]@{ state = $v.state; problems = @($v.problems) } | ConvertTo-Json -Compress",
                     {"DVE_BLOCK": json.dumps(block)})


@requires_windows_pwsh
class ReceiptValidatorReportsTheDisplayPathTests(unittest.TestCase):
    def test_a_receipt_without_the_block_is_valid_and_says_absent(self) -> None:
        got = validate(receipt_with(None))
        self.assertEqual((got["status"], got["valid"], got["displayPath"]), ("NO_SIGNAL", True, "absent"), got)
        self.assertEqual(got["reasons"], [])

    def test_a_null_block_is_the_same_as_an_absent_one(self) -> None:
        rec = receipt_with(None)
        rec["venue"]["displayPath"] = None
        got = validate(rec)
        self.assertEqual((got["status"], got["valid"], got["displayPath"]), ("NO_SIGNAL", True, "absent"), got)

    def test_a_malformed_block_is_a_validator_error(self) -> None:
        bad = {}
        base = good_block()
        bad["a string"] = "Intel Iris Xe"
        bad["an empty object"] = {}
        bad["an array"] = [base]
        bad["wrong schema"] = dict(base, schema="mlv-app/other/v9")
        bad["adapters is an object"] = dict(base, adapters={"name": "x"})
        bad["adapter lacks hasActiveOutput"] = dict(base, adapters=[{k: v for k, v in base["adapters"][0].items() if k != "hasActiveOutput"}])
        bad["hasActiveOutput is a string"] = dict(base, adapters=[dict(base["adapters"][0], hasActiveOutput="yes")])
        bad["refreshHz is a string"] = dict(base, activeDisplays=[dict(base["activeDisplays"][0], refreshHz="59")])
        bad["refreshHz is negative"] = dict(base, activeDisplays=[dict(base["activeDisplays"][0], refreshHz=-1)])
        bad["driverDate is not a date"] = dict(base, adapters=[dict(base["adapters"][0], driverDate="last tuesday")])
        bad["os is missing"] = {k: v for k, v in base.items() if k != "os"}
        bad["os.ubr is a string"] = dict(base, os=dict(base["os"], ubr="4770"))
        bad["lastBootUtc is not a date"] = dict(base, os=dict(base["os"], lastBootUtc="yesterday"))
        bad["powerPlan is a number"] = dict(base, powerPlan=7)
        bad["powerPlan is empty"] = dict(base, powerPlan="")
        bad["unknown without a reason"] = dict(base, powerPlan={"state": "unknown"})
        bad["unknown with an empty reason"] = dict(base, powerPlan={"state": "unknown", "reason": ""})
        bad["a state that is not unknown"] = dict(base, powerPlan={"state": "known", "reason": "x"})
        bad["collectedUtc is missing"] = {k: v for k, v in base.items() if k != "collectedUtc"}
        for label, block in bad.items():
            with self.subTest(label):
                got = validate(receipt_with(block))
                self.assertEqual((got["status"], got["valid"], got["displayPath"]), ("INVALID", False, "malformed"), got)
                self.assertTrue(any(r.startswith("DISPLAY_PATH_MALFORMED") for r in got["reasons"]), got)

    def test_a_block_whose_every_probe_failed_is_valid_and_present(self) -> None:
        got = validate(receipt_with(all_unknown_block()))
        self.assertEqual((got["status"], got["valid"], got["displayPath"]), ("NO_SIGNAL", True, "present"), got)
        got = validate(receipt_with(unknown("collector-error")))   # the whole block failed
        self.assertEqual((got["status"], got["valid"], got["displayPath"]), ("NO_SIGNAL", True, "present"), got)

    def test_a_well_formed_block_is_valid_and_present(self) -> None:
        got = validate(receipt_with(good_block()))
        self.assertEqual((got["status"], got["valid"], got["displayPath"]), ("NO_SIGNAL", True, "present"), got)
        self.assertEqual(got["reasons"], [])

    def test_one_unknown_field_among_known_ones_is_valid(self) -> None:
        block = good_block()
        block["adapters"][1]["driverDate"] = unknown("not-reported")
        block["os"]["hotfixCount"] = unknown("budget-exhausted")
        block["powerPlan"] = unknown("access denied")
        self.assertEqual(block_state(block)["state"], "present")

    def test_a_malformed_block_also_spoils_a_pass_receipt_but_an_absent_one_adds_nothing(self) -> None:
        bad = validate(receipt_with(dict(good_block(), powerPlan=7), outcome="PASS"))
        self.assertEqual((bad["status"], bad["valid"], bad["displayPath"]), ("INVALID", False, "malformed"), bad)
        self.assertTrue(any(r.startswith("DISPLAY_PATH_MALFORMED") for r in bad["reasons"]), bad)
        absent = validate(receipt_with(None, outcome="PASS"))
        self.assertEqual(absent["displayPath"], "absent", absent)
        self.assertFalse(any("DISPLAY_PATH" in r for r in absent["reasons"]), absent)
        present = validate(receipt_with(good_block(), outcome="PASS"))
        self.assertEqual(present["displayPath"], "present", present)
        self.assertFalse(any(r.startswith("DISPLAY_PATH_MALFORMED") for r in present["reasons"]), present)
        # the block is collected by the venue probe and written to no hashed artifact: the validator says so, as it does for hostName / gpuNames
        self.assertTrue(any(u.startswith("DISPLAY_PATH_UNBOUND") for u in present["unbound"]), present)


COLLECTOR_HARNESS = r"""
$ErrorActionPreference = 'Stop'
Import-Module '__MODULE__' -Force
. ([scriptblock]::Create((Get-DvDisplayPathCollectorSource)))
# ::new(), never New-Object: on PowerShell 7.6 a List[object] made by New-Object throws "Argument types do not match" under @($list), and the final report below wraps it that way
$script:calls = [System.Collections.Generic.List[object]]::new()
$script:mode = $env:DVE_MODE
$script:sleepMs = [int]$env:DVE_SLEEP_MS
$script:fx = $env:DVE_FIXTURE | ConvertFrom-Json
function Get-CimInstance {
    [CmdletBinding()] param([string]$ClassName, [string]$Namespace = '', [string]$Filter = '', [int]$OperationTimeoutSec = 0)
    $script:calls.Add([pscustomobject]@{ class = $ClassName; ns = $Namespace; filter = $Filter; timeout = $OperationTimeoutSec })
    if ($script:sleepMs -gt 0) { Start-Sleep -Milliseconds $script:sleepMs }
    if ($script:mode -eq 'fail') { throw 'RPC server unavailable' }
    if ($script:mode -eq 'deny') { throw [System.UnauthorizedAccessException]::new('Access is denied') }
    switch ($ClassName) {
        'Win32_VideoController' { return @($script:fx.video) }
        'Win32_OperatingSystem' { return @([pscustomobject]@{ LastBootUpTime = [datetime]::Parse($script:fx.boot).ToLocalTime() }) }
        'Win32_PowerPlan' { return @([pscustomobject]@{ ElementName = $script:fx.plan; IsActive = $true }) }
        'Win32_QuickFixEngineering' { return @(1..[int]$script:fx.hotfixes | ForEach-Object { [pscustomobject]@{ HotFixID = "KB$_" } }) }
    }
    @()
}
function Get-ItemProperty {
    [CmdletBinding()] param([string]$LiteralPath, [string]$Path)
    if ($script:mode -eq 'fail' -or $script:mode -eq 'deny') { throw 'Requested registry access is not allowed' }
    [pscustomobject]@{ CurrentBuild = '26100'; UBR = 4770 }
}
$sw = [Diagnostics.Stopwatch]::StartNew()
$budget = if ($env:DVE_BUDGET) { [int]$env:DVE_BUDGET } else { 15 }
$block = Get-DvDisplayPath -BudgetSec $budget
$ms = $sw.ElapsedMilliseconds
$roundTrip = ($block | ConvertTo-Json -Depth 8 -Compress) | ConvertFrom-Json
$verdict = Test-DvDisplayPathBlock -Block $roundTrip
[ordered]@{ block = $block; calls = @($script:calls); elapsedMs = $ms; state = $verdict.state; problems = @($verdict.problems) } | ConvertTo-Json -Depth 12 -Compress
"""

BACHELOR_FIXTURE = {
    "video": [
        {"Name": "Intel(R) Iris(R) Xe Graphics", "DriverVersion": "31.0.101.5186", "DriverDate": "2024-06-12T00:00:00Z",
         "PNPDeviceID": "PCI\\VEN_8086&DEV_9A49\\3&11583659&0&10", "CurrentRefreshRate": 59, "CurrentHorizontalResolution": 3840, "CurrentVerticalResolution": 2400},
        {"Name": "NVIDIA GeForce RTX 3060 Laptop GPU", "DriverVersion": "32.0.15.6094", "DriverDate": "2024-09-01T00:00:00Z",
         "PNPDeviceID": "PCI\\VEN_10DE&DEV_2560\\4&2C0D5D9&0&0008", "CurrentRefreshRate": None, "CurrentHorizontalResolution": None, "CurrentVerticalResolution": None},
    ],
    "boot": "2026-11-21T09:00:00Z", "plan": "Balanced", "hotfixes": 14,
}


def run_collector(mode: str = "ok", sleep_ms: int = 0, budget: int | None = None, fixture: dict | None = None) -> dict:
    env = {"DVE_MODE": mode, "DVE_SLEEP_MS": str(sleep_ms), "DVE_FIXTURE": json.dumps(fixture or BACHELOR_FIXTURE)}
    if budget is not None:
        env["DVE_BUDGET"] = str(budget)
    with tempfile.TemporaryDirectory(prefix="dv-displaypath-") as d:
        script = Path(d) / "harness.ps1"
        script.write_text(COLLECTOR_HARNESS.replace("__MODULE__", str(MODULE)), encoding="utf-8")
        proc = run_pwsh(["-File", str(script)], env_extra=env)
    try:
        return json.loads(proc.stdout)
    except ValueError:
        raise AssertionError(proc.stdout + proc.stderr)


def source() -> str:
    got = pwsh_json("[ordered]@{ text = (Get-DvDisplayPathCollectorSource) } | ConvertTo-Json -Compress")
    return got["text"]


@requires_windows_pwsh
class CollectorReadsTheBachelorShapeTests(unittest.TestCase):
    def test_the_headless_nvidia_has_no_output_and_the_intel_adapter_is_the_one_active_display(self) -> None:
        got = run_collector()
        block = got["block"]
        self.assertEqual(got["state"], "present", got)
        self.assertEqual(block["schema"], SCHEMA)
        by_name = {a["name"]: a for a in block["adapters"]}
        self.assertIs(by_name["Intel(R) Iris(R) Xe Graphics"]["hasActiveOutput"], True)
        self.assertIs(by_name["NVIDIA GeForce RTX 3060 Laptop GPU"]["hasActiveOutput"], False)
        self.assertEqual(by_name["Intel(R) Iris(R) Xe Graphics"]["driverVersion"], "31.0.101.5186")
        self.assertEqual(by_name["Intel(R) Iris(R) Xe Graphics"]["driverDate"], "2024-06-12T00:00:00Z")
        self.assertEqual(by_name["NVIDIA GeForce RTX 3060 Laptop GPU"]["pnpDeviceId"], "PCI\\VEN_10DE&DEV_2560\\4&2C0D5D9&0&0008")
        self.assertEqual(block["activeDisplays"], [{"adapterName": "Intel(R) Iris(R) Xe Graphics", "refreshHz": 59, "horizontalResolution": 3840, "verticalResolution": 2400}])
        self.assertEqual(block["os"], {"build": "26100", "ubr": 4770, "lastBootUtc": "2026-11-21T09:00:00Z", "hotfixCount": 14})
        self.assertEqual(block["powerPlan"], "Balanced")
        self.assertRegex(block["collectedUtc"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_every_cim_call_is_bounded_and_none_asks_for_more_than_the_budget(self) -> None:
        got = run_collector()
        self.assertGreaterEqual(len(got["calls"]), 4, got["calls"])
        for call in got["calls"]:
            self.assertTrue(1 <= call["timeout"] <= 15, call)
        self.assertIn("Win32_PowerPlan", [c["class"] for c in got["calls"]])
        plan = [c for c in got["calls"] if c["class"] == "Win32_PowerPlan"][0]
        self.assertEqual(plan["ns"], "root\\cimv2\\power")

    def test_no_adapter_at_all_is_an_empty_list_not_a_failure(self) -> None:
        got = run_collector(fixture=dict(BACHELOR_FIXTURE, video=[]))
        self.assertEqual(got["state"], "present", got)
        self.assertEqual(got["block"]["adapters"], [])
        self.assertEqual(got["block"]["activeDisplays"], [])


@requires_windows_pwsh
class CollectorNeverFailsOrDelaysTheLegTests(unittest.TestCase):
    def test_every_probe_failing_gives_typed_unknowns_and_does_not_throw(self) -> None:
        for mode in ("fail", "deny"):
            with self.subTest(mode):
                got = run_collector(mode=mode)
                block = got["block"]
                self.assertEqual(got["state"], "present", got)
                self.assertEqual(block["adapters"]["state"], "unknown")
                self.assertEqual(block["activeDisplays"]["state"], "unknown")
                for field in ("build", "ubr", "lastBootUtc", "hotfixCount"):
                    self.assertEqual(block["os"][field]["state"], "unknown", (field, block["os"]))
                    self.assertTrue(block["os"][field]["reason"], (field, block["os"]))
                self.assertEqual(block["powerPlan"]["state"], "unknown")
                self.assertLess(len(block["powerPlan"]["reason"]), 200)

    def test_a_slow_host_stops_asking_once_the_budget_is_spent(self) -> None:
        # each call takes 1.1 s; the budget is 3 s: the adapters and boot-time reads fit, the later ones are skipped as budget-exhausted, the block returns near 3 s
        got = run_collector(sleep_ms=1100, budget=3)
        block = got["block"]
        self.assertLess(got["elapsedMs"], 6500, got)
        self.assertEqual(block["adapters"][0]["name"], "Intel(R) Iris(R) Xe Graphics")
        self.assertEqual(block["os"]["hotfixCount"], {"state": "unknown", "reason": "budget-exhausted"})
        self.assertEqual(block["powerPlan"], {"state": "unknown", "reason": "budget-exhausted"})
        for call in got["calls"]:
            self.assertGreaterEqual(call["timeout"], 1)
            self.assertLessEqual(call["timeout"], 3)
        self.assertEqual(got["state"], "present")

    def test_the_slowest_probe_runs_last_so_a_stall_in_it_costs_the_others_nothing(self) -> None:
        got = run_collector()
        classes = [c["class"] for c in got["calls"]]
        self.assertEqual(classes[-1], "Win32_QuickFixEngineering", classes)

    def test_the_real_host_answers_inside_the_bound(self) -> None:
        # the live read-only queries on this machine: a well-formed block, inside 15 s (+ process start), whatever the machine says
        script = (". ([scriptblock]::Create((Get-DvDisplayPathCollectorSource)))\n$sw = [Diagnostics.Stopwatch]::StartNew()\n$b = Get-DvDisplayPath\n$ms = $sw.ElapsedMilliseconds\n"
                  "$rt = ($b | ConvertTo-Json -Depth 8 -Compress) | ConvertFrom-Json\n$v = Test-DvDisplayPathBlock -Block $rt\n"
                  "[ordered]@{ ms = $ms; state = $v.state; problems = @($v.problems) } | ConvertTo-Json -Compress")
        got = pwsh_json(script)
        self.assertEqual(got["state"], "present", got)
        self.assertLess(got["ms"], 16500, got)


@requires_windows_pwsh
class CollectorChangesNothingTests(unittest.TestCase):
    WRITE_VERBS = re.compile(r"\b(Set|New|Remove|Add|Clear|Start|Stop|Restart|Invoke|Enable|Disable|Register|Unregister|Out|Move|Copy|Rename|Write)-(?!Output\b)[A-Za-z]+", re.IGNORECASE)

    def test_the_collector_source_uses_only_read_commands(self) -> None:
        text = source()
        # comments may name anything; strip them before looking for verbs
        code = "\n".join(re.sub(r"(?<![`'\"])#.*$", "", line) for line in text.splitlines())
        verbs = sorted({m.group(0) for m in self.WRITE_VERBS.finditer(code)})
        self.assertEqual(verbs, [], f"the display-path collector must be read-only; found {verbs}")
        self.assertNotRegex(code, r"(?i)powercfg|setactive|\bsc\.exe|netsh|reg\s+(add|delete)|bcdedit|wmic|Invoke-CimMethod|Set-CimInstance|Start-Process")
        # the commands it CALLS: the collector's own `function Get-DvDisplayPath` declaration names it, it is not a call, so drop declarations before looking
        calls_only = re.sub(r"(?im)^\s*function\s+[A-Za-z][\w-]*", "", code)
        called = sorted(set(re.findall(r"\b(Get-[A-Za-z]+)\b", calls_only)))
        self.assertEqual(called, ["Get-CimInstance", "Get-ItemProperty"], called)

    def test_the_source_parses_clean(self) -> None:
        got = pwsh_json("$t = Get-DvDisplayPathCollectorSource\n$tok = $null; $err = $null\n[void][System.Management.Automation.Language.Parser]::ParseInput($t, [ref]$tok, [ref]$err)\n"
                        "[ordered]@{ errors = @($err).Count; hasFunction = $t.Contains('function Get-DvDisplayPath') } | ConvertTo-Json -Compress")
        self.assertEqual(got, {"errors": 0, "hasFunction": True})


@requires_windows_pwsh
class HealthProbeCarriesTheCollectorTests(unittest.TestCase):
    def test_the_probe_job_embeds_the_collector_and_sets_displaypath_before_it_prints(self) -> None:
        got = pwsh_json("$p = New-DvHealthProbeJobText -AgentRoot 'C:\\mlvtmp\\mlv-agent'\n$fn = Get-DvDisplayPathCollectorSource\n"
                        "$tok = $null; $err = $null; [void][System.Management.Automation.Language.Parser]::ParseInput($p, [ref]$tok, [ref]$err)\n"
                        "[ordered]@{ embedsVerbatim = $p.Contains($fn); parseErrors = @($err).Count; fnAt = $p.IndexOf($fn); assignAt = $p.IndexOf('$probe.displayPath ='); printAt = $p.IndexOf('DVE_PROBE=');"
                        " depth = ([regex]::Match($p, 'ConvertTo-Json -Compress -Depth (\\d+)')).Groups[1].Value } | ConvertTo-Json -Compress")
        self.assertTrue(got["embedsVerbatim"])
        self.assertEqual(got["parseErrors"], 0)
        self.assertGreater(got["assignAt"], got["fnAt"])
        self.assertGreater(got["printAt"], got["assignAt"])
        # a block nests probe > displayPath > adapters > entry > unknown > reason: the probe's JSON depth must reach it or ConvertTo-Json flattens it to a string
        self.assertGreaterEqual(int(got["depth"]), 7, got)

    def test_a_collector_that_throws_cannot_stop_the_probe(self) -> None:
        got = pwsh_json("$p = New-DvHealthProbeJobText -AgentRoot 'C:\\mlvtmp\\mlv-agent'\n"
                        "[ordered]@{ guarded = [bool]($p -match '(?s)try \\{ Get-DvDisplayPath \\} catch') } | ConvertTo-Json -Compress")
        self.assertTrue(got["guarded"])


@requires_windows_pwsh
class RunnerNormalisesTheProbeBlockTests(unittest.TestCase):
    def normalise(self, probe: dict) -> dict:
        return pwsh_json("$p = $env:DVE_PROBE | ConvertFrom-Json\n$b = ConvertTo-DvDisplayPath -Probe $p\n"
                         "$rt = ($b | ConvertTo-Json -Depth 8 -Compress) | ConvertFrom-Json\n$v = Test-DvDisplayPathBlock -Block $rt\n"
                         "[ordered]@{ block = $b; state = $v.state } | ConvertTo-Json -Depth 10 -Compress", {"DVE_PROBE": json.dumps(probe)})

    def test_a_probe_without_the_block_is_typed_unknown_not_absent(self) -> None:
        got = self.normalise(dict(HEALTHY_PROBE))
        self.assertEqual(got["block"], unknown("probe-did-not-report"))
        self.assertEqual(got["state"], "present")

    def test_a_malformed_probe_block_becomes_typed_unknown(self) -> None:
        for label, block in (("a string", "Intel"), ("an array", [1]), ("bad powerPlan", dict(good_block(), powerPlan=7)), ("empty", {})):
            with self.subTest(label):
                got = self.normalise(dict(HEALTHY_PROBE, displayPath=block))
                self.assertEqual(got["block"], unknown("probe-block-malformed"))
                self.assertEqual(got["state"], "present")

    def test_a_good_block_survives_json_date_coercion_as_utc_text(self) -> None:
        got = self.normalise(dict(HEALTHY_PROBE, displayPath=good_block()))
        self.assertEqual(got["block"], good_block())
        self.assertEqual(got["state"], "present")


@requires_windows_pwsh
class VenueLegReceiptTests(RunnerHarness, unittest.TestCase):
    """End to end through Invoke-VenueLeg.ps1 (offline stubs): the probe's block lands in the receipt, and no block, however bad, changes the leg's verdict."""

    def setUp(self) -> None:
        self.make_harness()
        # the shared stub serialises the probe at ConvertTo-Json's default depth 2, which flattens a nested block: this one keeps it
        self.um.write_text(STUB_UM_RUN.replace("($cfg.probe | ConvertTo-Json -Compress)", "($cfg.probe | ConvertTo-Json -Compress -Depth 10)"), encoding="utf-8")

    def sick(self, **extra) -> dict:
        return dict(HEALTHY_PROBE, pwshColdStartMs=9000, **extra)   # a typed VENUE_UNHEALTHY terminal: the receipt is written after the venue block is filled

    def test_a_probe_block_is_written_to_the_receipt(self) -> None:
        proc, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), probe=self.sick(displayPath=good_block()))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(receipt["venue"]["displayPath"], good_block())
        self.assertEqual(receipt["outcome"], "VENUE_UNHEALTHY")

    def test_an_all_unknown_block_is_written_and_the_verdict_is_unchanged(self) -> None:
        _, plain, _ = self.run_leg("ultra-magnus", self.write_spec(), probe=self.sick())
        _, withblock, _ = self.run_leg("ultra-magnus", self.write_spec(), probe=self.sick(displayPath=all_unknown_block()))
        self.assertEqual(withblock["venue"]["displayPath"], all_unknown_block())
        for key in ("outcome", "outcomeDetail"):
            self.assertEqual(withblock[key], plain[key], key)
        self.assertEqual(withblock["health"], plain["health"])

    def test_a_missing_or_malformed_probe_block_is_typed_unknown_and_never_changes_the_verdict(self) -> None:
        _, plain, _ = self.run_leg("ultra-magnus", self.write_spec(), probe=self.sick())
        self.assertEqual(plain["venue"]["displayPath"], unknown("probe-did-not-report"))
        for label, block in (("a string", "x"), ("a number", 5), ("wrong shape", {"adapters": 3})):
            with self.subTest(label):
                proc, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), probe=self.sick(displayPath=block))
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(receipt["venue"]["displayPath"], unknown("probe-block-malformed"))
                self.assertEqual((receipt["outcome"], receipt["outcomeDetail"]), (plain["outcome"], plain["outcomeDetail"]))

    def test_a_host_mismatch_terminal_still_carries_the_block(self) -> None:
        wrong = dict(HEALTHY_PROBE, hostName="BACHELOR", displayPath=good_block())
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), probe=wrong)
        self.assertEqual(receipt["outcome"], "VENUE_HOST_MISMATCH")
        self.assertEqual(receipt["venue"]["displayPath"], good_block())


@requires_windows_pwsh
class SchemaAndDocsTests(unittest.TestCase):
    def test_the_receipt_skeleton_names_the_field(self) -> None:
        got = pwsh_json("$r = New-DvReceipt -Card 'c' -LegId 'l' -DeclaredVenue 'bachelor' -Role 'supplementary' -Actor 'a' -Method 'm' -MethodBlobId 'b' -Backend 'cuda' -LookFlavor $null\n"
                        "[ordered]@{ has = $r['venue'].Contains('displayPath'); value = $r['venue']['displayPath'] } | ConvertTo-Json -Compress")
        self.assertEqual(got, {"has": True, "value": None})

    def test_the_evidence_doc_describes_every_field(self) -> None:
        doc = (ROOT / "docs" / "dual-venue-evidence.md").read_text(encoding="utf-8")
        self.assertIn("venue.displayPath", doc)
        for token in ("adapters", "activeDisplays", "powerPlan", "hotfixCount", "lastBootUtc", "ubr", "driverDate", "hasActiveOutput", "displayPath=absent",
                      '"state":"unknown"', "15 s", "read-only"):
            with self.subTest(token):
                section = doc.split("venue.displayPath", 1)[1]
                self.assertIn(token, section)

    def test_the_sheet_pair_record_lists_the_block_as_unbound(self) -> None:
        text = (DV / "New-VenueSheetPair.ps1").read_text(encoding="utf-8")
        self.assertIn("'venue.displayPath'", text)


if __name__ == "__main__":
    unittest.main()
