"""Tests for VENUE-LEG-INLEASE-REQUALIFY-1: tools/profiling/dual-venue/VenueLegCounting.psm1.

WHAT IS PINNED (HUB RULING subject-ledger.md 2026-10-24T15:40:56Z action (1), pre-registered before r7)
  * rule selection: r1-r6 (and anything unrecognised) keep 'stale-discard-180'; r7 and later use 'inlease-requalify-1'.
  * under inlease-requalify-1 a leg whose admission wait exceeded 180 s counts only when it was already QUIET_PRELOAD_OK, its in-job,
    post-grant pre-load is <= 30.0 % (NaN refuses) and it was throttled 0 at the lease grant; every other stale leg is typed NOT_COUNTED_<reason>.
  * the <= 180 s path, the cooldown gate (any base class other than QUIET_PRELOAD_OK) and a missing admission wait are unchanged under both rules.
  * the Look Assist arm check mirrors the r6 lane analyze.ps1:100-106; a missing field is never proven.
  * purity: the module's code reads no file, path or share and starts nothing.

Offline only: every call goes through pwsh -NoProfile with JSON in and out. Nothing here reaches a venue, the share or a fleet-runs dir.
Windows-only (PowerShell tools).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "tools" / "profiling" / "dual-venue" / "VenueLegCounting.psm1"
PWSH = shutil.which("pwsh")
requires_windows_pwsh = unittest.skipIf(PWSH is None or sys.platform != "win32", "needs pwsh on Windows")

INLEASE = "inlease-requalify-1"
STALE180 = "stale-discard-180"
OK = "QUIET_PRELOAD_OK"
COOL = "throttled 0/60, mean 59.8 C, max15 84 C, 26 W"
HOT_S1 = "throttled 6/60, mean 67.2 C, max15 60 C, 9 W"

# One pwsh process per call list. A string 'NaN' argument becomes [double]::NaN (JSON has no NaN).
HARNESS = r"""
$ErrorActionPreference = 'Stop'
Import-Module -Name $env:VLC_MODULE -Force -DisableNameChecking
$ops = [Console]::In.ReadToEnd() | ConvertFrom-Json
$out = [System.Collections.Generic.List[object]]::new()
foreach ($op in @($ops)) {
    $a = @{}
    foreach ($p in $op.args.PSObject.Properties) {
        $v = $p.Value
        if ($v -is [string] -and $v -ceq 'NaN') { $v = [double]::NaN }
        if ($v -is [string] -and $v -ceq 'Infinity') { $v = [double]::PositiveInfinity }
        if ($v -is [string] -and $v -ceq '-Infinity') { $v = [double]::NegativeInfinity }
        $a[$p.Name] = $v
    }
    $out.Add((& $op.fn @a))
}
ConvertTo-Json -InputObject @($out.ToArray()) -Depth 6 -Compress
"""


def call(*ops: tuple[str, dict]) -> list:
    payload = json.dumps([{"fn": fn, "args": args} for fn, args in ops])
    env = dict(os.environ, VLC_MODULE=str(MODULE))
    r = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", HARNESS], input=payload, capture_output=True,
                       text=True, timeout=120, env=env)
    if r.returncode != 0:
        raise AssertionError(f"pwsh exit {r.returncode}\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}")
    return json.loads(r.stdout)


def req(rule: str, wait, thermal=None, pre=None, base: str = OK, kind: str = "chain", admission: dict | None = None) -> tuple[str, dict]:
    if admission is None:
        admission = None if thermal is None else {"thermal": thermal}
    return ("Get-VenueLegRequalification", {"Rule": rule, "Kind": kind, "BaseClass": base, "AdmissionWaitedSec": wait,
                                            "Admission": admission, "InLeasePreLoadMean": pre})


def la(arm: str, log, vq, arg, forced) -> tuple[str, dict]:
    return ("Test-VenueLegLookAssistArm", {"Arm": arm, "LogLookAssist": log, "VisualQualityLookAssist": vq, "ArgNoLookAssist": arg,
                                           "LookAssistForced": forced})


@requires_windows_pwsh
class InLeaseRequalificationTests(unittest.TestCase):
    def assertRow(self, row: dict, cls: str, eligible: bool, requalified: bool = False) -> None:
        self.assertEqual(row["class"], cls, row)
        self.assertIs(row["eligible"], eligible, row)
        self.assertIs(row["requalified"], requalified, row)
        self.assertEqual(row["notCountedReason"], None if eligible else cls, row)

    def test_01_stale_chain_leg_requalifies_in_lease(self) -> None:
        (r,) = call(req(INLEASE, 912, COOL, 15.4))
        self.assertRow(r, "QUIET_PRELOAD_REQUALIFIED_IN_LEASE", True, True)
        self.assertEqual(r["rule"], INLEASE)
        self.assertEqual(r["throttledAtGrant"], 0)
        self.assertIs(r["thermalKnown"], True)

    def test_02_r6_s1_shape_throttled_at_grant(self) -> None:
        # the hot lease form is prefixed 'sustained heat: ' (ultra-magnus-agent.ps1:1737); both forms must type THROTTLED, not UNKNOWN
        a, b = call(req(INLEASE, 912, HOT_S1, 15.438), req(INLEASE, 912, "sustained heat: " + HOT_S1, 15.438))
        for r in (a, b):
            self.assertRow(r, "NOT_COUNTED_THROTTLED_AT_GRANT", False)
            self.assertEqual(r["throttledAtGrant"], 6)

    def test_03_inlease_preload_high_and_nan_refuse(self) -> None:
        a, b, c = call(req(INLEASE, 912, COOL, 31.0), req(INLEASE, 912, COOL, "NaN"), req(INLEASE, 912, COOL, 30.0))
        self.assertRow(a, "NOT_COUNTED_INLEASE_PRELOAD_HIGH", False)
        self.assertRow(b, "NOT_COUNTED_INLEASE_PRELOAD_HIGH", False)
        self.assertRow(c, "QUIET_PRELOAD_REQUALIFIED_IN_LEASE", True, True)

    def test_04_missing_inlease_probe_by_kind(self) -> None:
        a, b, c = call(req(INLEASE, 912, COOL, None, kind="chain"), req(INLEASE, 912, COOL, None, kind="ref"),
                       req(INLEASE, 541, COOL, None, kind="ref"))
        self.assertRow(a, "NOT_COUNTED_INLEASE_PROBE_MISSING", False)
        self.assertRow(b, "NOT_COUNTED_STALE_NO_INLEASE_PROBE", False)
        self.assertRow(c, "NOT_COUNTED_STALE_NO_INLEASE_PROBE", False)

    def test_05_thermal_unknown_and_numeric_throttled(self) -> None:
        a, b, c, d, e = call(req(INLEASE, 912, None, 15.4, admission={}), req(INLEASE, 912, "feed unknown", 15.4),
                             req(INLEASE, 912, "", 15.4), req(INLEASE, 912, pre=15.4, admission={"throttled": 0}),
                             req(INLEASE, 912, None, 15.4, admission=None))
        for r in (a, b, c, e):
            self.assertRow(r, "NOT_COUNTED_THERMAL_UNKNOWN", False)
            self.assertIs(r["thermalKnown"], False)
            self.assertIsNone(r["throttledAtGrant"])
        self.assertRow(d, "QUIET_PRELOAD_REQUALIFIED_IN_LEASE", True, True)
        self.assertEqual(d["throttledAtGrant"], 0)

    def test_06_within_180_unchanged_under_both_rules(self) -> None:
        rows = call(*(req(rule, w, HOT_S1, 15.4) for rule in (INLEASE, STALE180) for w in (180, 0)))
        for r in rows:
            self.assertRow(r, OK, True)
            self.assertEqual(r["throttledAtGrant"], 6)

    def test_07_no_retroactive_recount_and_rule_selection(self) -> None:
        (r,) = call(req(STALE180, 912, COOL, 15.4))
        self.assertRow(r, "QUIET_PRELOAD_STALE", False)
        self.assertEqual(r["throttledAtGrant"], 0)
        rounds = ["r1", "r4", "r5", "r6", "", "x", "R7", "r7a", "r07", "r7", "r12"]
        got = call(*(("Get-VenueLegCountingRule", {"Round": x}) for x in rounds))
        self.assertEqual(got, [STALE180] * 8 + [INLEASE] * 3)

    def test_08_cooldown_unmet_passes_through(self) -> None:
        rows = call(*(req(rule, w, COOL, 6.1, base="COOLDOWN_UNMET") for rule in (INLEASE, STALE180) for w in (0, 912, None)))
        for r in rows:
            self.assertRow(r, "COOLDOWN_UNMET", False)

    def test_09_missing_wait_is_unverified(self) -> None:
        rows = call(req(INLEASE, None, COOL, 15.4), req(STALE180, None, COOL, 15.4), req(INLEASE, "NaN", COOL, 15.4))
        for r in rows:
            self.assertRow(r, "QUIET_PRELOAD_UNVERIFIED_ADMISSION", False)


@requires_windows_pwsh
class AdmissionThermalTests(unittest.TestCase):
    def test_numeric_wins_over_string(self) -> None:
        a, b, c = call(("ConvertFrom-VenueAdmissionThermal", {"Admission": {"throttled": 2, "rows": 60, "thermal": HOT_S1}}),
                       ("ConvertFrom-VenueAdmissionThermal", {"Admission": {"thermal": HOT_S1}}),
                       ("ConvertFrom-VenueAdmissionThermal", {"Admission": {"throttled": None, "thermal": "feed unknown"}}))
        self.assertEqual(a, {"throttled": 2, "window": 60, "known": True, "source": "numeric"})
        self.assertEqual(b, {"throttled": 6, "window": 60, "known": True, "source": "thermal-string"})
        self.assertIs(c["known"], False)
        self.assertIsNone(c["throttled"])


@requires_windows_pwsh
class LookAssistArmTests(unittest.TestCase):
    def test_10_look_assist_arm(self) -> None:
        on_off, on_ok, on_noforced, off_ok, off_vq1, on_arg, on_missing = call(
            la("on", "0", "1", False, True), la("on", "1", "1", False, True), la("on", "1", "1", False, None),
            la("off", "0", "0", True, None), la("off", "0", "1", True, None), la("on", "1", "1", True, True), la("on", None, "1", False, True))
        self.assertEqual((on_off["proven"], on_off["violation"], on_off["reason"]), (False, True, "LOOK_ASSIST_NOT_APPLIED"))
        self.assertEqual((on_ok["proven"], on_ok["violation"]), (True, False))
        self.assertEqual((on_noforced["proven"], on_noforced["violation"]), (False, False))
        self.assertEqual((off_ok["proven"], off_ok["violation"]), (True, False))
        self.assertEqual((off_vq1["proven"], off_vq1["violation"]), (False, True))
        self.assertEqual((on_arg["proven"], on_arg["violation"], on_arg["reason"]), (False, True, "LOOK_ASSIST_NOT_APPLIED"))
        self.assertEqual((on_missing["proven"], on_missing["violation"]), (False, False))
        (off_forced,) = call(la("off", "0", "0", True, True))
        self.assertIs(off_forced["proven"], False)


def call_raw(*ops: tuple[str, dict]) -> subprocess.CompletedProcess:
    """Like call() but returns the process, for cases where the parameter binder is expected to reject the call."""
    payload = json.dumps([{"fn": fn, "args": args} for fn, args in ops])
    env = dict(os.environ, VLC_MODULE=str(MODULE))
    return subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", HARNESS], input=payload, capture_output=True,
                          text=True, timeout=120, env=env)


@requires_windows_pwsh
class CaseExactEnumTests(unittest.TestCase):
    """Dot 80f0e297 B1: ValidateSet was case-insensitive while the branches use -ceq. Chosen fix: reject (IgnoreCase = $false)."""

    def assertRejected(self, op: tuple[str, dict]) -> None:
        r = call_raw(op)
        self.assertNotEqual(r.returncode, 0, f"a case variant must be refused, got stdout:\n{r.stdout}")
        self.assertIn("ValidateSet", r.stderr + r.stdout)

    def test_11_rule_case_variant_is_rejected_not_requalified(self) -> None:
        # counterexample 1: STALE-DISCARD-180 skipped the old-rule branch and returned eligible/requalified instead of stale
        for rule in ("STALE-DISCARD-180", "Stale-Discard-180", "INLEASE-REQUALIFY-1"):
            self.assertRejected(req(rule, 912, COOL, 15.4, base=OK))

    def test_12_kind_case_variant_is_rejected(self) -> None:
        for kind in ("REF", "Ref", "Chain"):
            self.assertRejected(req(INLEASE, 912, COOL, None, kind=kind))

    def test_13_arm_case_variant_is_rejected_not_the_off_branch(self) -> None:
        # counterexample 2: Arm=ON with log=0, vq=0, ArgNoLookAssist=true, forced=false took the OFF branch -> proven=true / violation=false
        for arm in ("ON", "On", "OFF", "Off"):
            self.assertRejected(la(arm, "0", "0", True, False))

    def test_14_canonical_values_still_bind(self) -> None:
        stale, on_arm = call(req(STALE180, 912, COOL, 15.4), la("on", "0", "0", True, False))
        self.assertEqual(stale["class"], "QUIET_PRELOAD_STALE")
        self.assertEqual((on_arm["proven"], on_arm["violation"]), (False, True))


@requires_windows_pwsh
class InvalidMeasurementTests(unittest.TestCase):
    def test_15_fractional_negative_or_nonfinite_throttled_is_thermal_unknown(self) -> None:
        cases = [{"throttled": 0.4}, {"throttled": 1.5}, {"throttled": -1}, {"throttled": "NaN"}, {"throttled": "Infinity"},
                 {"throttled": -0.0001}, {"throttled": 1e12}, {"throttled": 0.4, "thermal": COOL}, {"throttled": "0", "thermal": COOL}]
        rows = call(*(req(INLEASE, 912, pre=15.4, admission=a) for a in cases))
        thermal = call(*(("ConvertFrom-VenueAdmissionThermal", {"Admission": a}) for a in cases))
        for a, r, t in zip(cases, rows, thermal):
            self.assertEqual(r["class"], "NOT_COUNTED_THERMAL_UNKNOWN", (a, r))
            self.assertIs(r["eligible"], False, (a, r))
            self.assertIs(r["thermalKnown"], False, (a, r))
            self.assertIsNone(r["throttledAtGrant"], (a, r))
            self.assertIs(t["known"], False, (a, t))
            self.assertIsNone(t["throttled"], (a, t))

    def test_16_whole_number_throttled_still_known(self) -> None:
        zero, two, two_float = call(req(INLEASE, 912, pre=15.4, admission={"throttled": 0}), req(INLEASE, 912, pre=15.4, admission={"throttled": 2}),
                                    req(INLEASE, 912, pre=15.4, admission={"throttled": 2.0}))
        self.assertEqual(zero["class"], "QUIET_PRELOAD_REQUALIFIED_IN_LEASE")
        self.assertEqual((two["class"], two["throttledAtGrant"]), ("NOT_COUNTED_THROTTLED_AT_GRANT", 2))
        self.assertEqual((two_float["class"], two_float["throttledAtGrant"]), ("NOT_COUNTED_THROTTLED_AT_GRANT", 2))

    def test_17_negative_or_infinite_wait_is_never_eligible(self) -> None:
        waits = (-5, -0.001, "Infinity", "-Infinity")
        rows = call(*(req(rule, w, COOL, 15.4) for rule in (INLEASE, STALE180) for w in waits))
        for r in rows:
            self.assertEqual(r["class"], "NOT_COUNTED_INVALID_MEASUREMENT", r)
            self.assertIs(r["eligible"], False, r)
            self.assertIs(r["requalified"], False, r)
            self.assertEqual(r["notCountedReason"], "NOT_COUNTED_INVALID_MEASUREMENT", r)

    def test_18_negative_or_infinite_preload_is_never_eligible(self) -> None:
        rows = call(*(req(INLEASE, 912, COOL, p) for p in (-0.5, -100, "Infinity", "-Infinity")))
        for r in rows:
            self.assertEqual(r["class"], "NOT_COUNTED_INVALID_MEASUREMENT", r)
            self.assertIs(r["eligible"], False, r)
        # a within-180 s leg never consults the pre-load, and a non-OK base class still passes through
        ok180, cool = call(req(INLEASE, 100, COOL, -1), req(INLEASE, -5, COOL, 15.4, base="COOLDOWN_UNMET"))
        self.assertEqual(ok180["class"], OK)
        self.assertEqual(cool["class"], "COOLDOWN_UNMET")


class ModulePurityPinTests(unittest.TestCase):
    def test_module_is_pure_and_ascii(self) -> None:
        raw = MODULE.read_bytes()
        self.assertTrue(all(b < 0x80 for b in raw), "module must be ASCII only")
        text = raw.decode("ascii")
        self.assertNotIn("\\\\", text, "no UNC share path")
        code = re.sub(r"<#.*?#>", "", text, flags=re.S)
        code = "\n".join(line.split("#", 1)[0] for line in code.splitlines())
        for banned in (r"\bGet-Content\b", r"\bSet-Content\b", r"\bTest-Path\b", r"\bInvoke-\w+", r"\bStart-Process\b",
                       r"\[IO\.File\]", r"\[System\.IO\.", r"\bOut-File\b", r"\bAdd-Content\b"):
            self.assertIsNone(re.search(banned, code, flags=re.I), banned)
        self.assertIn("Export-ModuleMember", code)
        for fn in ("Get-VenueLegCountingRule", "ConvertFrom-VenueAdmissionThermal", "Get-VenueLegRequalification", "Test-VenueLegLookAssistArm"):
            self.assertIn(fn, code)


if __name__ == "__main__":
    unittest.main()
