"""Tests for VENUE-LEG-INLEASE-REQUALIFY-1: tools/profiling/dual-venue/VenueLegCounting.psm1.

WHAT IS PINNED (HUB RULING subject-ledger.md 2026-10-24T15:40:56Z action (1), pre-registered before r7)
  * rule selection: r1-r6 (and anything unrecognised) keep 'stale-discard-180'; r7 and later use 'inlease-requalify-1'.
  * under inlease-requalify-1 a leg whose admission wait exceeded 180 s counts only when it was already QUIET_PRELOAD_OK, its in-job,
    post-grant pre-load is <= 30.0 % (NaN refuses) and it was throttled 0 at the lease grant; every other stale leg is typed NOT_COUNTED_<reason>.
  * the <= 180 s path, the cooldown gate (any base class other than QUIET_PRELOAD_OK) and a missing admission wait are unchanged under both rules.
  * the Look Assist arm check mirrors the r6 lane analyze.ps1:100-106; a missing field is never proven.
  * purity: the module's code reads no file, path or share and starts nothing.
  * VENUE-LEG-COUNTING-HARDENING-1: a NaN in-lease pre-load is NOT_COUNTED_INVALID_MEASUREMENT (G2); every numeric input is pinned
    against NaN, +Inf, -Inf, an empty string and a non-numeric string (a string never binds to 0 and counts); the module header and
    docs/cuda-playback-present-cadence.md say which analyzers import it and that stale-discard-180 also refuses invalid waits (G3).

Offline only: every call goes through pwsh -NoProfile with JSON in and out. Nothing here reaches a venue, the share or a fleet-runs dir.
Runs wherever pwsh is on PATH (the hosted Windows and ubuntu runners both have it), so every shard runs the pins (G1).
"""

from __future__ import annotations

import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "tools" / "profiling" / "dual-venue" / "VenueLegCounting.psm1"
DOC = ROOT / "docs" / "cuda-playback-present-cadence.md"
PWSH = shutil.which("pwsh")
# G1: gated on pwsh alone. The module is pure PowerShell, so the platform does not matter, and a win32 gate left the ubuntu shards with no pins.
requires_pwsh = unittest.skipIf(PWSH is None, "needs pwsh")

INLEASE = "inlease-requalify-1"
STALE180 = "stale-discard-180"
OK = "QUIET_PRELOAD_OK"
COOL = "throttled 0/60, mean 59.8 C, max15 84 C, 26 W"
HOT_S1 = "throttled 6/60, mean 67.2 C, max15 60 C, 9 W"

# One pwsh process per call list. A string 'NaN' / 'Infinity' / '-Infinity' becomes that [double] (JSON has none), at the top level and
# inside an object argument such as Admission, so a non-finite admission.throttled or .rows is a real double, not a string.
HARNESS = r"""
$ErrorActionPreference = 'Stop'
Import-Module -Name $env:VLC_MODULE -Force -DisableNameChecking
function ConvertTo-HarnessValue([object]$v) {
    if ($v -is [string] -and $v -cmatch '^typed:(\w+):(\d+)$') {
        # 'typed:<type>:<digits>' builds that exact .NET numeric type (JSON only has double and int64)
        $n = $Matches[2]
        switch ($Matches[1]) {
            'byte' { return , [byte]$n }
            'sbyte' { return , [sbyte]$n }
            'int16' { return , [int16]$n }
            'uint16' { return , [uint16]$n }
            'int32' { return , [int]$n }
            'uint32' { return , [uint32]$n }
            'int64' { return , [long]$n }
            'uint64' { return , [uint64]$n }
            'single' { return , [single]$n }
            'double' { return , [double]$n }
            'decimal' { return , [decimal]$n }
            'bigint' { return , [System.Numerics.BigInteger]::Parse($n) }
            default { throw "unknown typed value $v" }
        }
    }
    if ($v -is [string] -and $v -ceq 'NaN') { return [double]::NaN }
    if ($v -is [string] -and $v -ceq 'Infinity') { return [double]::PositiveInfinity }
    if ($v -is [string] -and $v -ceq '-Infinity') { return [double]::NegativeInfinity }
    if ($v -is [System.Management.Automation.PSCustomObject]) { foreach ($q in $v.PSObject.Properties) { $q.Value = ConvertTo-HarnessValue $q.Value } }
    , $v
}
$ops = [Console]::In.ReadToEnd() | ConvertFrom-Json
$out = [System.Collections.Generic.List[object]]::new()
foreach ($op in @($ops)) {
    $a = @{}
    foreach ($p in $op.args.PSObject.Properties) { $a[$p.Name] = ConvertTo-HarnessValue $p.Value }
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


@requires_pwsh
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
        # G2: a NaN pre-load is not a measurement, so it is typed INVALID, not HIGH (it refused before, under the wrong reason)
        a, b, c = call(req(INLEASE, 912, COOL, 31.0), req(INLEASE, 912, COOL, "NaN"), req(INLEASE, 912, COOL, 30.0))
        self.assertRow(a, "NOT_COUNTED_INLEASE_PRELOAD_HIGH", False)
        self.assertRow(b, "NOT_COUNTED_INVALID_MEASUREMENT", False)
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


@requires_pwsh
class AdmissionThermalTests(unittest.TestCase):
    def test_numeric_wins_over_string(self) -> None:
        a, b, c = call(("ConvertFrom-VenueAdmissionThermal", {"Admission": {"throttled": 2, "rows": 60, "thermal": HOT_S1}}),
                       ("ConvertFrom-VenueAdmissionThermal", {"Admission": {"thermal": HOT_S1}}),
                       ("ConvertFrom-VenueAdmissionThermal", {"Admission": {"throttled": None, "thermal": "feed unknown"}}))
        self.assertEqual(a, {"throttled": 2, "window": 60, "known": True, "source": "numeric"})
        self.assertEqual(b, {"throttled": 6, "window": 60, "known": True, "source": "thermal-string"})
        self.assertIs(c["known"], False)
        self.assertIsNone(c["throttled"])


@requires_pwsh
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


@requires_pwsh
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


@requires_pwsh
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


# VENUE-LEG-COUNTING-HARDENING-1 (Dot 258ad5e5 nit): direct fixtures for every numeric input. A string is not a measurement: before this
# card an empty string bound to 0 (a wait of "" was QUIET_PRELOAD_OK, a pre-load of "" REQUALIFIED) and "abc" threw at the binder.
NON_FINITE = ("NaN", "Infinity", "-Infinity")
NOT_A_NUMBER = ("", "   ", "abc", "912", True)


@requires_pwsh
class NonFiniteInputFixtureTests(unittest.TestCase):
    def assertInvalid(self, row: dict, what) -> None:
        self.assertEqual(row["class"], "NOT_COUNTED_INVALID_MEASUREMENT", (what, row))
        self.assertIs(row["eligible"], False, (what, row))
        self.assertIs(row["requalified"], False, (what, row))
        self.assertEqual(row["notCountedReason"], "NOT_COUNTED_INVALID_MEASUREMENT", (what, row))

    def test_19_admission_wait_non_finite_or_not_a_number(self) -> None:
        for rule in (INLEASE, STALE180):
            nan, pinf, ninf = call(*(req(rule, w, COOL, 15.4) for w in NON_FINITE))
            self.assertEqual((nan["class"], nan["eligible"]), ("QUIET_PRELOAD_UNVERIFIED_ADMISSION", False))   # a NaN wait is a missing wait (test_09)
            self.assertInvalid(pinf, (rule, "+Inf"))
            self.assertInvalid(ninf, (rule, "-Inf"))
            for w, r in zip(NOT_A_NUMBER, call(*(req(rule, w, COOL, 15.4) for w in NOT_A_NUMBER))):
                self.assertInvalid(r, (rule, w))

    def test_20_inlease_preload_non_finite_or_not_a_number(self) -> None:
        values = NON_FINITE + NOT_A_NUMBER
        for p, r in zip(values, call(*(req(INLEASE, 912, COOL, p) for p in values))):
            self.assertInvalid(r, p)
        # the pre-load is consulted only for a stale leg under inlease-requalify-1 (test_18): the old rule and the <= 180 s path ignore it
        for p, r in zip(values, call(*(req(STALE180, 912, COOL, p) for p in values))):
            self.assertEqual((r["class"], r["eligible"]), ("QUIET_PRELOAD_STALE", False), (p, r))
        for p, r in zip(values, call(*(req(INLEASE, 100, COOL, p) for p in values))):
            self.assertEqual((r["class"], r["eligible"]), (OK, True), (p, r))

    def test_21_throttled_non_finite_or_not_a_number(self) -> None:
        values = NON_FINITE + NOT_A_NUMBER + ("0",)
        cases = [{"throttled": v, "thermal": COOL} for v in values]
        rows = call(*(req(INLEASE, 912, pre=15.4, admission=a) for a in cases))
        thermal = call(*(("ConvertFrom-VenueAdmissionThermal", {"Admission": a}) for a in cases))
        for a, r, t in zip(cases, rows, thermal):
            self.assertEqual((r["class"], r["eligible"], r["throttledAtGrant"]), ("NOT_COUNTED_THERMAL_UNKNOWN", False, None), (a, r))
            self.assertEqual((t["known"], t["throttled"], t["source"]), (False, None, "numeric-invalid"), (a, t))

    def test_22_rows_non_finite_or_not_a_number_is_no_window(self) -> None:
        # rows is the heat window, informational only: an invalid one is dropped (window null) and never makes a valid throttled count unknown
        values = NON_FINITE + NOT_A_NUMBER + (-1, 1.5)
        cases = [{"throttled": 0, "rows": v} for v in values]
        thermal = call(*(("ConvertFrom-VenueAdmissionThermal", {"Admission": a}) for a in cases))
        rows = call(*(req(INLEASE, 912, pre=15.4, admission=a) for a in cases))
        for a, t, r in zip(cases, thermal, rows):
            self.assertEqual(t, {"throttled": 0, "window": None, "known": True, "source": "numeric"}, a)
            self.assertEqual(r["class"], "QUIET_PRELOAD_REQUALIFIED_IN_LEASE", (a, r))

    def test_23_finite_numeric_inputs_keep_their_verdicts(self) -> None:
        # hardening, not a defect fix: a finite, valid (numeric-typed) input classes exactly as before this card
        got = call(req(INLEASE, 0, COOL, 15.4), req(INLEASE, 180, COOL, 99.0), req(INLEASE, 180.0001, COOL, 30.0),
                   req(INLEASE, 912, COOL, 30.0001), req(STALE180, 181, COOL, 0), req(INLEASE, 912, None, 15.4, admission={"throttled": 1}))
        self.assertEqual([r["class"] for r in got], [OK, OK, "QUIET_PRELOAD_REQUALIFIED_IN_LEASE", "NOT_COUNTED_INLEASE_PRELOAD_HIGH",
                                                     "QUIET_PRELOAD_STALE", "NOT_COUNTED_THROTTLED_AT_GRANT"])

    def test_27_typed_numeric_inputs_follow_the_documented_type_list(self) -> None:
        # Dot 258ad5e5 nit: the module documents its supported numeric types (VenueLegCounting.psm1, Test-VenueLegCountingNumericType). Each
        # supported .NET type is a measurement; every other numeric type is not, and is refused rather than coerced. JSON has only double and
        # int64, so the harness builds the exact type from 'typed:<type>:<digits>'.
        supported = ("byte", "int16", "int32", "int64", "single", "double", "decimal")
        refused = ("sbyte", "uint16", "uint32", "uint64", "bigint")
        for rule in (INLEASE, STALE180):
            for t, r in zip(supported, call(*(req(rule, f"typed:{t}:100", COOL, 15.4) for t in supported))):
                self.assertEqual((r["class"], r["eligible"]), (OK, True), (rule, t, r))
            for t, r in zip(refused, call(*(req(rule, f"typed:{t}:100", COOL, 15.4) for t in refused))):
                self.assertInvalid(r, (rule, "wait", t))
        # the pre-load (a stale leg under inlease-requalify-1) and the throttled count use the same type list
        for t, r in zip(supported, call(*(req(INLEASE, 912, COOL, f"typed:{t}:15") for t in supported))):
            self.assertEqual(r["class"], "QUIET_PRELOAD_REQUALIFIED_IN_LEASE", (t, r))
        for t, r in zip(refused, call(*(req(INLEASE, 912, COOL, f"typed:{t}:15") for t in refused))):
            self.assertInvalid(r, ("preload", t))
        for t, r in zip(supported, call(*(req(INLEASE, 912, pre=15.4, admission={"throttled": f"typed:{t}:0"}) for t in supported))):
            self.assertEqual((r["class"], r["throttledAtGrant"]), ("QUIET_PRELOAD_REQUALIFIED_IN_LEASE", 0), (t, r))
        for t, r in zip(refused, call(*(req(INLEASE, 912, pre=15.4, admission={"throttled": f"typed:{t}:0"}) for t in refused))):
            self.assertEqual((r["class"], r["eligible"], r["throttledAtGrant"]), ("NOT_COUNTED_THERMAL_UNKNOWN", False, None), (t, r))


def _tracked_importers() -> list[str]:
    """Tracked tools/ scripts that import VenueLegCounting.psm1 (the module and this test excluded)."""
    hits = []
    for path in sorted((ROOT / "tools").rglob("*")):
        if path.suffix.lower() not in (".ps1", ".psm1") or path == MODULE:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "VenueLegCounting.psm1" in text and re.search(r"\bImport-Module\b", text, flags=re.I):
            hits.append(path.relative_to(ROOT).as_posix())
    return hits


class ImporterAndDocPinTests(unittest.TestCase):
    """Dot 258ad5e5 nit and Grok G3: the header and the doc state facts, re-derived here from the tree."""

    def test_24_header_names_exactly_its_importers(self) -> None:
        header = MODULE.read_text(encoding="ascii").splitlines()[0]
        self.assertNotIn("every bench analyzer", header)
        importers = _tracked_importers()
        if importers:
            for path in importers:
                self.assertIn(path, MODULE.read_text(encoding="ascii"), "the module header must name each tracked importer")
        else:
            self.assertIn("No tracked driver or analyzer imports it", MODULE.read_text(encoding="ascii"))
            self.assertIn("No tracked driver or analyzer imports it", DOC.read_text(encoding="utf-8"))

    def test_25_doc_says_stale_discard_180_refuses_invalid_waits(self) -> None:
        doc = DOC.read_text(encoding="utf-8").replace("\r\n", "\n")
        flat = " ".join(doc.split())
        self.assertIn("under `stale-discard-180` as under `inlease-requalify-1`", flat)
        self.assertIn("is typed `NOT_COUNTED_INVALID_MEASUREMENT`", flat)
        self.assertNotIn("no driver or analyzer imports it yet", flat)


class SkipGatePinTests(unittest.TestCase):
    def _run_gated(self, *, pwsh, hosted, on_class):
        """Run a throwaway TestCase gated by requires_pwsh with PWSH and GITHUB_ACTIONS forced; returns (result, bodies that ran)."""
        ran = []
        if on_class:
            @requires_pwsh
            class Probe(unittest.TestCase):
                def test_x(self):
                    ran.append(1)
        else:
            class Probe(unittest.TestCase):
                @requires_pwsh
                def test_x(self):
                    ran.append(1)
        result = unittest.TestResult()
        with mock.patch.object(sys.modules[__name__], "PWSH", pwsh), mock.patch.dict(os.environ):
            if hosted is None:
                os.environ.pop("GITHUB_ACTIONS", None)
            else:
                os.environ["GITHUB_ACTIONS"] = hosted
            unittest.defaultTestLoader.loadTestsFromTestCase(Probe).run(result)
        return result, ran

    def test_26_pins_are_gated_on_pwsh_only_and_fail_closed_in_hosted_ci(self) -> None:
        # G1 (platform): a platform gate left the ubuntu shards with no pins; the module is pure PowerShell, so the gate reads pwsh alone.
        # Grok G1 (fail closed): a hosted shard without pwsh must FAIL, not skip all the pins and go green. Same pattern as
        # test_safety_lint_parity.py _skip_or_fail_in_hosted_ci: GITHUB_ACTIONS=true -> fail, anywhere else -> skip. Pinned for the class
        # decorator (how every pin class uses it) and the method decorator.
        self.assertNotIn("platform", inspect.getsource(requires_pwsh))
        self.assertNotIn("skipIf", inspect.getsource(requires_pwsh))
        for on_class in (True, False):
            where = "class" if on_class else "method"
            res, ran = self._run_gated(pwsh=None, hosted="true", on_class=on_class)
            self.assertEqual((len(res.failures), len(res.skipped), len(res.errors), ran), (1, 0, 0, []), f"hosted CI, no pwsh, {where}")
            self.assertIn("hosted CI must not skip", res.failures[0][1])
            for hosted in (None, "", "false", "TRUE"):
                res, ran = self._run_gated(pwsh=None, hosted=hosted, on_class=on_class)
                self.assertEqual((len(res.failures), len(res.skipped), len(res.errors), ran), (0, 1, 0, []), f"local {hosted!r}, no pwsh, {where}")
            for hosted in (None, "true"):
                res, ran = self._run_gated(pwsh="pwsh", hosted=hosted, on_class=on_class)
                self.assertEqual((res.wasSuccessful(), len(res.skipped), ran), (True, 0, [1]), f"pwsh present {hosted!r}, {where}")


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
