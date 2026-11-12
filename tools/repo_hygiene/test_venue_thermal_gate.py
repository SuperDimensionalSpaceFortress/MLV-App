"""Tests for BACHELOR-THERMAL-GATE-WAIT-1: tools/profiling/dual-venue/VenueThermalGate.psm1 and its use in Invoke-VenueLeaseBatch.ps1 (item 10).

WHAT IS PINNED
  * The hot rule is the Bachelor agent's own (ultra-magnus-agent.ps1 Get-SustainedHeatVerdict: throttled > GateMaxThrottledRows, or mean >= GateMeanMaxC),
    and the module's constants equal the agent's defaults.
  * The reading is the in-lease quiet probe's "tele heat" line (Wait-VenueQuiet.ps1 prints the agent's feed, read with the agent's reader, before launch).
  * WAIT FOR COOL, with a stub thermal source through the runner's offline seams: hot -> wait -> cool -> run; hot past the cap -> THERMAL_HOT_SKIPPED and
    re-queued (the next leg runs first, the skipped one after); re-queues exhausted -> THERMAL_HOT_SKIPPED for good (never run, exit 7); unknown heat runs.
    Every RAN row records thermalWaitSec.
  * SPREAD: heavy leg keys are measured from earlier receipts (a key whose next leg on the same lease read hot), and -PlanOrder ThermalSpread never puts two
    heavy legs back to back. Replayed on the PR493-CONTROL-ARM-QUIET-AB-1 r1 batch plans (fixture below, taken from that run's batch receipts).

Offline only (the runner tests reuse test_venue_lease_batch.py's fakes and seams). Nothing reaches a venue. Windows-only (PowerShell tools).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_venue_lease_batch as tvlb  # noqa: E402  (the runner's offline fakes; only names, never its TestCase classes, are bound here)

ROOT = tvlb.ROOT
DV = ROOT / "tools" / "profiling" / "dual-venue"
GATE = DV / "VenueThermalGate.psm1"
AGENT = ROOT / "tools" / "profiling" / "bachelor" / "ultra-magnus-agent.ps1"
QUIET = DV / "Wait-VenueQuiet.ps1"
PWSH = tvlb.PWSH
requires_windows_pwsh = tvlb.requires_windows_pwsh


def canonical_path(path: str | Path) -> str:
    """One spelling of a path for an equality check. r7 (hosted CI run 40867081400): the windows runner's TEMP is the 8.3 short name C:\\Users\\RUNNER~1\\...,
    which Python 3.13's tempfile keeps and .NET's GetFullPath expands, so two spellings of one directory compared unequal. realpath expands the short names
    of the parts that exist (on both sides), normcase folds the case."""
    import os
    return os.path.normcase(os.path.realpath(str(path)))

# The stub thermal source: the fake quiet probe prints a "tele heat" line in Wait-VenueQuiet's format. FAKE_HEAT is a comma list per probe call (the last
# repeats): a throttled row count (mean 70 C), NA (an unreadable feed), or HOT_UNTIL_SKIP (hot until batch.log carries a THERMAL_HOT_SKIPPED line).
HEAT_STUB = r"""
# FAKE_PROBE_SLEEP: seconds this probe call takes (a comma list per call, the last repeats): the probe's own duration, so a later probe can be slower than the earlier ones
$sleepSpec = @(([string]$env:FAKE_PROBE_SLEEP) -split ',' | Where-Object { $_ })
if ($sleepSpec.Count -gt 0) { Start-Sleep -Seconds ([int]$sleepSpec[[Math]::Min($n, $sleepSpec.Count - 1)]) }
$heatSpec = @(([string]$env:FAKE_HEAT) -split ',' | Where-Object { $_ })
if ($heatSpec.Count -gt 0) {
    $hv = $heatSpec[[Math]::Min($n, $heatSpec.Count - 1)]
    if ($hv -ceq 'HOT_UNTIL_SKIP') {
        $bl = Join-Path $runDir 'batch.log'
        $hv = $(if ((Test-Path -LiteralPath $bl) -and (Select-String -LiteralPath $bl -SimpleMatch 'THERMAL_HOT_SKIPPED' -Quiet)) { '0' } else { '9' })
    }
    if ($hv -ceq 'NA') { '  tele heat source=unavailable age=n/a hottest=n/a max15=n/a mean=n/a throttled=n/a/n/a W=n/a' }
    else { "  tele heat source=C:\temp\hwinfo-log-adapter.csv age=1.7s hottest=50.0C max15=64.0C mean=70.0C throttled=$hv/60 W=12.3" }
}
"""
FAKE_QUIET_HEAT = tvlb.FAKE_QUIET.replace("switch ($mode) {", HEAT_STUB + "switch ($mode) {", 1)
assert FAKE_QUIET_HEAT != tvlb.FAKE_QUIET, "the stub heat line could not be placed in the fake quiet probe"

# PR493-CONTROL-ARM-QUIET-AB-1 r1 (fleet-runs\lane-PR493-CONTROL-ARM-QUIET-AB-1-r1-20261027T1545Z): each batch's legs in run order as (cell, arm), and the
# agent's admission.heat at grant per leg (batch-<nn>\agent-result-<jobId>.json). Cell -> leg key is the driver's cellDefs (bench-ab.ps1:25-29).
CELL = {"OWN": "fullscreen-s1|cuda", "10": "windowed-s1|cuda", "11": "fullscreen-s2|cuda", "12": "windowed-s2|cuda", "13": "fullscreen-s4|cuda",
        "14": "windowed-s4|cuda", "15": "fullscreen-s1|cpu", "16": "windowed-s1|cpu", "17": "fullscreen-s2|cpu", "18": "windowed-s2|cpu",
        "19": "fullscreen-s4|cpu", "20": "windowed-s4|cpu"}
R1 = {
    "01": "OWN C clear,OWN H clear,OWN C clear,OWN H clear,10 C clear,10 H clear,11 C clear,11 H clear",
    "02": "15 C clear,15 H hot,17 C hot,17 H clear,19 C clear,19 H clear,20 C clear,20 H clear",
    "03": "15 C clear,15 H clear,17 C clear,17 H clear",
    "04": "OWN H clear,OWN C clear,10 H clear,10 C clear,11 H clear,11 C clear,15 H clear,15 C hot",
    "05": "17 H clear,17 C clear,19 H clear,19 C clear,20 H clear,20 C clear,15 H clear,15 C clear",
    "06": "12 C clear,12 H clear,13 C clear,13 H clear,14 C clear,14 H clear,16 C clear,16 H clear",
    "07": "18 C clear,18 H clear,16 C clear,16 H clear",
}


def r1_rows() -> list[dict]:
    rows = []
    for nn, legs in R1.items():
        for leg in legs.split(","):
            cell, arm, heat = leg.split()
            rows.append({"batch": nn, "cell": cell, "arm": arm, "key": "m16-1243-display-" + CELL[cell], "status": "RAN", "heat": heat})
    return rows


def ps(script: str) -> str:
    r = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", f"$ErrorActionPreference='Stop'; Import-Module '{GATE}' -Force; {script}"],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise AssertionError(r.stdout + r.stderr)
    return r.stdout.strip()


def ps_json(script: str):
    return json.loads(ps(f"ConvertTo-Json -Depth 6 -Compress -InputObject $({script})"))


def unmounted_drive_path() -> str:
    """A path on a drive letter that is not mounted on this machine (Join-Path on it throws DriveNotFoundException)."""
    import os
    for letter in "QRSTUVWXYZ":
        if not os.path.exists(f"{letter}:\\"):
            return f"{letter}:\\past-run"
    raise unittest.SkipTest("every candidate drive letter is mounted")


@requires_windows_pwsh
class ThermalGateModuleTests(unittest.TestCase):
    def test_hot_rule_and_constants_are_the_agents(self) -> None:
        agent = AGENT.read_text(encoding="utf-8")
        thr = int(re.search(r"\[int\]\$GateMaxThrottledRows = (\d+)", agent).group(1))
        mean = float(re.search(r"\[double\]\$GateMeanMaxC = ([0-9.]+)", agent).group(1))
        self.assertIn("$Metrics.throttled -gt $GateMaxThrottledRows -or $Metrics.meanC -ge $GateMeanMaxC", agent)
        mod = GATE.read_text(encoding="utf-8")
        self.assertIn(f"$script:HeatMaxThrottledRows = {thr}\n", mod)
        self.assertIn(f"$script:HeatMeanMaxC = {mean:.1f}\n", mod)
        got = ps_json(f"@(Get-VenueHeatVerdict -Throttled {thr} -MeanC 70; Get-VenueHeatVerdict -Throttled {thr + 1} -MeanC 70; "
                      f"Get-VenueHeatVerdict -Throttled 0 -MeanC {mean}; Get-VenueHeatVerdict -Throttled 0 -MeanC {mean - 0.1}; "
                      "Get-VenueHeatVerdict -Throttled $null -MeanC 70; Get-VenueHeatVerdict -Throttled 'n/a' -MeanC 70)")
        self.assertEqual(got, ["clear", "hot", "hot", "clear", "unknown", "unknown"])

    def test_the_reading_is_the_quiet_probes_tele_heat_line(self) -> None:
        # Wait-VenueQuiet prints the agent's feed in exactly this shape (the r1 hot 17C leg's probe, batch-02 quiet-03 ... .out.txt)
        self.assertIn('"  tele heat source={0} age={1} hottest={2} max15={3} mean={4} throttled={5}/{6} W={7}"', QUIET.read_text(encoding="utf-8"))
        line = "  tele heat source=C:\\temp\\hwinfo-log-adapter.csv age=1.7s hottest=50.0C max15=64.0C mean=75.9C throttled=12/60 W=12.3"
        got = ps_json(f"ConvertFrom-VenueProbeHeatLine -Lines @('QUIET mean=7.0% threshold=20% waitedSec=50', '{line}')")
        self.assertEqual((got["heat"], got["throttled"], got["window"], got["meanC"]), ("hot", 12, 60, 75.9))
        na = ps_json("ConvertFrom-VenueProbeHeatLine -Lines @('  tele heat source=unavailable age=n/a hottest=n/a max15=n/a mean=n/a throttled=n/a/n/a W=n/a')")
        self.assertEqual(na["heat"], "unknown")
        self.assertEqual(ps_json("ConvertFrom-VenueProbeHeatLine -Lines @('QUIET mean=5.0%')")["heat"], "unknown")

    def test_adversary_a2_a3_a10_raw_json_first_decimal_comma_and_overflow(self) -> None:
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="thermal-raw-"))
        raw = d / "venue-quiet-probe-x.json"
        # A2: the agent's mean 87.96 is clear; the display line rounds it to 88.0C, which alone would read hot. The raw JSON is read first.
        raw.write_text(json.dumps({"heat": {"mean_c": 87.96, "throttled": 0, "window_rows": 60}}), encoding="utf-8")
        got = ps_json(f"ConvertFrom-VenueProbeHeatLine -Lines @('  raw {raw}', '  tele heat source=x age=1.0s hottest=50.0C max15=60.0C mean=88.0C throttled=0/60 W=1')")
        self.assertEqual((got["heat"], got["source"], got["meanC"]), ("clear", "raw-json", 87.96))
        # A3: a probe in a decimal-comma culture prints mean=89,0C: hot, not unknown
        got = ps_json("ConvertFrom-VenueProbeHeatLine -Lines @('  tele heat source=x age=1,0s hottest=50,0C max15=60,0C mean=89,0C throttled=0/60 W=1')")
        self.assertEqual(got["heat"], "hot")
        # A10: an overflowing count is unknown, never a throw
        got = ps_json("ConvertFrom-VenueProbeHeatLine -Lines @('  tele heat mean=70.0C throttled=2147483648/60 W=1')")
        self.assertEqual(got["heat"], "unknown")

    def test_adversary_a5_a7_a8_history_is_lease_scoped_and_never_throws(self) -> None:
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="thermal-hist-"))
        # A8: a reused run dir numbers its batches from 1 again: two leases (tokens) with batch=1 never chain a key across them
        recs = [{"batch": 1, "token": "t1", "legs": [{"name": "01-a-cpu-A", "backend": "cpu", "arm": "A", "status": "RAN", "heatAtProbe": "clear"}]},
                {"batch": 1, "token": "t2", "legs": [{"name": "01-b-cpu-A", "backend": "cpu", "arm": "A", "status": "RAN", "heatAtProbe": "hot"}]},
                # A7: rows missing batch / status are skipped, not a strict-mode throw
                {"legs": [{"name": "01-c-cpu-A", "backend": "cpu", "arm": "A"}]},
                # A5: a scalar run's rows carry no name: named in skipped, not silently dropped
                {"batch": 3, "token": "t3", "legs": [{"status": "RAN", "class": "QUIET_PRELOAD_OK"}, {"status": "RAN"}]}]
        (d / "batches.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
        got = ps_json(f"$h = Read-VenueLegHeatHistory -RunDir @('{d}'); $k = Get-VenueHeavyLegKeys -Rows @($h.rows); [pscustomobject]@{{ rows = @($h.rows | Where-Object {{ $_.key }}).Count; heavy = @($k.Keys); skipped = @($h.skipped) }}")
        self.assertEqual((got["rows"], got["heavy"]), (2, []), got)
        self.assertTrue(any("2 leg rows with no leg name" in s for s in got["skipped"]), got["skipped"])
        self.assertTrue(any("1 leg rows with no leg name or status" in s for s in got["skipped"]), got["skipped"])

    def test_adversary_r1b_a3_a8_a7_a5_final_probe_raw_overflow_dates_and_barriers(self) -> None:
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="thermal-r1b-"))
        old = d / "p1.json"
        old.write_text(json.dumps({"heat": {"mean_c": 70, "throttled": 0, "window_rows": 60}}), encoding="utf-8")
        # A3: the final probe's raw write failed: its own tele line (hot) is read, never the earlier probe's clear file
        got = ps_json(f"ConvertFrom-VenueProbeHeatLine -Lines @('  raw {old}', '  raw UNWRITABLE Access denied', 'QUIET mean=7.0%', "
                      "'  tele heat source=x age=1.0s hottest=89.0C max15=89.0C mean=89.0C throttled=0/60 W=10')")
        self.assertEqual((got["heat"], got["source"]), ("hot", "tele-line"))
        # A8: an overflowing raw count falls back to the line, never a throw
        ovf = d / "ovf.json"
        ovf.write_text(json.dumps({"heat": {"mean_c": 70, "throttled": 2147483648, "window_rows": 60}}), encoding="utf-8")
        got = ps_json(f"ConvertFrom-VenueProbeHeatLine -Lines @('  raw {ovf}', '  tele heat mean=70.0C throttled=0/60 W=1')")
        self.assertEqual((got["heat"], got["source"]), ("clear", "tele-line"))
        # A7: extreme accepted dates are past the cap, never an Int32 overflow
        got = ps("(Get-VenueThermalGateStep -Heat hot -FirstHotUtc ([datetime]::MinValue) -NowUtc ([datetime]::MaxValue) -CapSec 180).step")
        self.assertEqual(got, "SKIP")
        # A5: a LEG_FAILED leg launched, so it is the predecessor; a barrier row (unknown identity) charges nobody
        rows = json.dumps([{"batch": "1.t", "key": "light|cpu", "status": "RAN", "heat": "clear"}, {"batch": "1.t", "key": "failed|cpu", "status": "LEG_FAILED", "heat": "clear"},
                           {"batch": "1.t", "key": "next|cpu", "status": "RAN", "heat": "hot"}, {"batch": "1.t", "key": None, "status": "UNKNOWN", "heat": "unknown"},
                           {"batch": "1.t", "key": "after|cpu", "status": "RAN", "heat": "hot"}])
        got = ps_json(f"$h = Get-VenueHeavyLegKeys -Rows @(ConvertFrom-Json -InputObject '{rows}'); [pscustomobject]$h")
        self.assertEqual(got, {"failed|cpu": 1})

    def test_adversary_r2b_a4_the_thermal_clock_is_monotonic_not_the_wall_clock(self) -> None:
        # a wall clock that stepped back 120 s between probes used to read the elapsed wait as 0 (cap not consumed, overshoot 0): the elapsed time now comes
        # from the Stopwatch timestamps alone, so 100 s of them are 100 s whatever UtcNow says
        got = ps("$f = [datetime]::new(2026,10,28,0,0,0,[DateTimeKind]::Utc); $fq = [Diagnostics.Stopwatch]::Frequency; "
                 "(Get-VenueThermalNowUtc -FirstHotUtc $f -FirstHotTimestamp 1000 -NowTimestamp (1000 + 100 * $fq)).ToString('o'); "
                 "(Get-VenueThermalNowUtc -FirstHotUtc $f -FirstHotTimestamp 1000 -NowTimestamp 500).ToString('o')")
        self.assertEqual(got.splitlines(), ["2026-10-28T00:01:40.0000000Z", "2026-10-28T00:00:00.0000000Z"], got)
        runner = (DV / "Invoke-VenueLeaseBatch.ps1").read_text(encoding="utf-8")
        self.assertNotIn("[DateTime]::UtcNow - $thw.firstHotUtc", runner, "the probe-wait clamp must not time the wait on the wall clock")
        for line in runner.splitlines():
            if "Get-VenueThermalGateStep -Heat" in line:
                self.assertNotIn("-NowUtc ([DateTime]::UtcNow)", line, "the gate step must not be fed the wall clock once a first hot reading exists")
        self.assertGreaterEqual(runner.count("Get-VenueThermalNowUtc"), 1)
        # r6 (adversary r5 A1): the probe-wait clamp moved from the loop top to the dispatch, where it reads the same monotonic elapsed time as the admission
        self.assertIn("$wait = [int][Math]::Max(0, [Math]::Min($wait, $ThermalWaitCapSec - $el))", runner)

    def test_adversary_r3_a2_the_heat_rule_is_applied_to_the_exact_mean_not_the_rounded_one(self) -> None:
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="thermal-exact-"))
        exact = 87.99833333333333   # 60 rows: 59 at 88.0 C and one at 87.9 C; the agent's rule (mean >= 88) calls this CLEAR
        raw = d / "venue-quiet-probe-x.json"
        raw.write_text(json.dumps({"heat": {"mean_c": 88.0, "mean_c_exact": exact, "throttled": 0, "window_rows": 60}}), encoding="utf-8")
        old = d / "venue-quiet-probe-old.json"   # a probe that predates mean_c_exact: only the rounded value exists, and 88.0 reads hot
        old.write_text(json.dumps({"heat": {"mean_c": 88.0, "throttled": 0, "window_rows": 60}}), encoding="utf-8")
        tele = "  tele heat source=x age=1.0s hottest=50.0C max15=60.0C mean=88.0C throttled=0/60 W=1"
        got = ps_json(
            f"$g = {{ param($l) $r = ConvertFrom-VenueProbeHeatLine -Lines $l; \"$($r.heat)|$($r.source)|$([math]::Round($r.meanC, 6))\" }}; @("
            f"(& $g @('  raw {raw}')), (& $g @('  raw {old}')), "
            f"(& $g @('{tele} mean_exact={exact!r}')), (& $g @('{tele} mean_exact=88')), (& $g @('{tele}')), (& $g @('{tele} mean_exact=87,99833')), (& $g @('{tele} mean_exact=junk')))")
        self.assertEqual(got, ["clear|raw-json|87.998333", "hot|raw-json|88", "clear|tele-line|87.998333", "hot|tele-line|88", "hot|tele-line|88",
                               "clear|tele-line|87.99833", "hot|tele-line|88"], got)
        # the producer writes both: mean_c (rounded, unchanged) and mean_c_exact, and prints mean_exact= on the display line
        q = QUIET.read_text(encoding="utf-8")
        self.assertIn("$telHeat.mean_c_exact = (ConvertTo-JsonSafeDouble $r.m.meanC)", q)
        self.assertIn("' mean_exact=' + $mx", q)   # r4 A1: printed through the round-trip formatter ($rt), never 'R' (5.1 misses the round trip) or [string]
        self.assertIn("'mean_c_exact'", q.split("$heatFields = ", 1)[1].split("\n", 1)[0])

    def test_adversary_r3_a3_a_history_dir_that_cannot_be_resolved_is_skipped_and_named(self) -> None:
        import os
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="thermal-hist-ok-"))
        (d / "batches.jsonl").write_text(json.dumps({"batch": 1, "token": "t1", "legs": [{"name": "01-a-cpu-A", "backend": "cpu", "arm": "A", "status": "RAN", "heatAtProbe": "hot"}]}) + "\n", encoding="utf-8")
        bad = unmounted_drive_path()
        # Join-Path threw DriveNotFoundException for an unmounted drive (and for an empty entry) before the protected read: the whole history, usable dirs included, was refused
        got = ps_json(f"$h = Read-VenueLegHeatHistory -RunDir @('{bad}', '{d}', '', '{bad}\\agent'); [pscustomobject]@{{ rows = @($h.rows).Count; skipped = @($h.skipped) }}")
        self.assertEqual(got["rows"], 1, got)
        self.assertEqual(len([s for s in got["skipped"] if "no batches.jsonl" in s]), 3, got)
        self.assertTrue(any(bad in s for s in got["skipped"]), got["skipped"])
        self.assertFalse(os.path.exists(bad))

    def test_adversary_r3p1_a1_a2_history_rows_read_the_agents_heat_and_a_relative_dir_resolves_against_the_powershell_location(self) -> None:
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="thermal-hist-rel-"))
        row = {"name": "01-a-cpu-A", "backend": "cpu", "arm": "A", "status": "RAN", "jobId": "job-1"}
        row2 = {"name": "02-b-cpu-A", "backend": "cpu", "arm": "A", "status": "RAN", "jobId": "job-2"}
        (d / "batches.jsonl").write_text(json.dumps({"batch": 1, "token": "t1", "legs": [row, row2]}) + "\n", encoding="utf-8")
        (d / "agent-result-job-1.json").write_text(json.dumps({"admission": {"heat": "clear"}}), encoding="utf-8")
        (d / "agent-result-job-2.json").write_text(json.dumps({"admission": {"heat": "hot"}}), encoding="utf-8")
        tail = "[pscustomobject]@{ heat = @($h.rows | ForEach-Object { $_.heat }); skipped = @($h.skipped) }"
        # r3 pass 1 A1: a row with a jobId and no heatAtProbe takes the agent's admission.heat; the reader used `(try { } catch { })` as an expression, which PowerShell runs as a COMMAND
        got = ps_json(f"$h = Read-VenueLegHeatHistory -RunDir @('{d}'); {tail}")
        self.assertEqual((got["heat"], got["skipped"]), (["clear", "hot"], []), got)
        # r3 pass 1 A2: -HeatFrom '.' is relative to the POWERSHELL location; File.ReadAllLines resolved it against the process directory and reported the dir unreadable
        got = ps_json(f"Set-Location -LiteralPath '{d}'; $h = Read-VenueLegHeatHistory -RunDir @('.'); {tail}")
        self.assertEqual((got["heat"], got["skipped"]), (["clear", "hot"], []), got)
        # a relative dir with a sub-path and a dir that does not exist are both resolved the same way (the second is skipped and named, never a throw)
        got = ps_json(f"Set-Location -LiteralPath '{d.parent}'; $h = Read-VenueLegHeatHistory -RunDir @('{d.name}', 'no-such-dir'); {tail}")
        self.assertEqual((got["heat"], len(got["skipped"])), (["clear", "hot"], 1), got)
        # r3 pass 1 A4 (minor): a requested sleep below one second is refused at the parameter, not widened to one
        with self.assertRaises(AssertionError) as cm:
            ps("Get-VenueThermalProbeBudget -CapSec 1 -ElapsedSec 0 -SlowestReprobeSec 0 -WantSleepSec 0")
        self.assertIn("WantSleepSec", str(cm.exception))

    def test_adversary_r3p2_a2_a4_a5_excess_heavy_legs_run_back_to_back_only_at_the_end_a_signed_mean_reads_and_a_nul_path_is_not_a_throw(self) -> None:
        # A2: with H heavy and L light items the least possible adjacency is max(0, H - L - 1) for every pattern (all 510 non-empty patterns up to 8 items), and the
        # light items keep their given order relative to the unspread input as far as the spread allows
        got = ps_json(
            "$bad = 0; $n = 0; for ($len = 1; $len -le 8; $len++) { for ($m = 0; $m -lt [Math]::Pow(2, $len); $m++) { "
            "$flags = [bool[]]@(for ($i = 0; $i -lt $len; $i++) { [bool](($m -shr $i) -band 1) }); $h = @($flags | Where-Object { $_ }).Count; "
            "$s = Get-VenueThermalSpreadOrder -Items @(0..($len - 1)) -Heavy $flags; $n++; "
            "if ($s.heavyAdjacent -ne [Math]::Max(0, $h - ($len - $h) - 1) -or @($s.items).Count -ne $len -or @($s.items | Sort-Object -Unique).Count -ne $len) { $bad++ } } }; "
            "$e = Get-VenueThermalSpreadOrder -Items @('L1','H1','H2','H3') -Heavy ([bool[]]@($false,$true,$true,$true)); [pscustomobject]@{ bad = $bad; n = $n; items = @($e.items); adj = $e.heavyAdjacent }")
        self.assertEqual((got["bad"], got["items"], got["adj"]), (0, ["H1", "L1", "H2", "H3"], 1), got)
        # A4: the display fallback accepts a signed mean (the feed parser does), so throttled=7/60 stays hot instead of reading unknown
        tele = "  tele heat source=x age=1.0s hottest=-1.0C max15=-1.0C mean=-1.0C throttled=7/60 W=10"
        got = ps_json(f"$a = ConvertFrom-VenueProbeHeatLine -Lines @('  raw UNWRITABLE sharing violation', '{tele} mean_exact=-1'); $b = ConvertFrom-VenueProbeHeatLine -Lines @('{tele}'); @($a.heat, $a.source, $b.heat)")
        self.assertEqual(got, ["hot", "tele-line", "hot"], got)
        # A5: a -HeatFrom entry with a NUL in it makes Test-Path throw; the normalizing line treats it as not-a-path (split, then skipped and named by the reader)
        runner = (DV / "Invoke-VenueLeaseBatch.ps1").read_text(encoding="utf-8")
        line = [l for l in runner.splitlines() if re.match(r"^\$HeatFrom = @\(\$HeatFrom \| ForEach-Object", l)][0]
        script = "$ErrorActionPreference='Stop'; $HeatFrom = @(('C:\\bad' + [char]0 + 'history'), '.'); " + line + "; \"$($HeatFrom.Count)|$($HeatFrom[1])\""
        r = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "2|."), r.stdout + r.stderr)

    def test_r5_sol_b1_a4_a_non_finite_mean_never_masks_a_hot_throttled_count_on_either_parse_path(self) -> None:
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="thermal-nonfinite-"))
        # the verdict is the agent's rule in its order (ultra-magnus-agent.ps1:2436): the count first, +Infinity is >= 88 (hot), NaN / -Infinity / a missing mean never decide alone
        got = ps_json(
            "$nan = [double]::NaN; $pi = [double]::PositiveInfinity; $ni = [double]::NegativeInfinity; "
            "@((Get-VenueHeatVerdict -Throttled 7 -MeanC $nan), (Get-VenueHeatVerdict -Throttled 0 -MeanC $pi), (Get-VenueHeatVerdict -Throttled 7 -MeanC $ni), "
            "(Get-VenueHeatVerdict -Throttled 0 -MeanC $nan), (Get-VenueHeatVerdict -Throttled 0 -MeanC $ni), (Get-VenueHeatVerdict -Throttled 7 -MeanC $null), "
            "(Get-VenueHeatVerdict -Throttled 7 -MeanC 'n/a'), (Get-VenueHeatVerdict -Throttled 0 -MeanC $null), "
            "(Get-VenueHeatVerdict -Throttled 6 -MeanC 70), (Get-VenueHeatVerdict -Throttled 7 -MeanC 70), (Get-VenueHeatVerdict -Throttled 0 -MeanC 88), "
            "(Get-VenueHeatVerdict -Throttled 0 -MeanC 87.9), (Get-VenueHeatVerdict -Throttled $nan -MeanC 90), (Get-VenueHeatVerdict -Throttled $pi -MeanC 90))")
        self.assertEqual(got, ["hot", "hot", "hot", "unknown", "unknown", "hot", "hot", "unknown", "clear", "hot", "hot", "clear", "unknown", "unknown"], got)

        def raw(thr: int, mean: str) -> str:
            p = d / f"raw-{thr}-{mean}.json"
            p.write_text(json.dumps({"heat": {"mean_c": mean, "mean_c_exact": mean, "throttled": thr, "window_rows": 60}}), encoding="utf-8")
            return f"ConvertFrom-VenueProbeHeatLine -Lines @('  raw {p}')"

        # the raw JSON path (the probe writes a non-finite double as the string "NaN" / "Infinity" / "-Infinity"), sol's 60-row case = throttled 7
        for thr, mean, want in ((7, "NaN", "hot"), (7, "Infinity", "hot"), (7, "-Infinity", "hot"), (0, "Infinity", "hot"), (0, "NaN", "unknown"), (0, "-Infinity", "unknown")):
            self.assertEqual(ps_json(raw(thr, mean))["heat"], want, (thr, mean))
        self.assertEqual(ps_json(raw(7, "NaN"))["source"], "raw-json")

        # the display-line fallback: NaN, the culture's infinity symbol (U+221E) or Infinity in mean=, NaN / Infinity in mean_exact=
        # the line is double-quoted PowerShell so the infinity symbol can be passed as a [char] (the command line stays ASCII)
        tele = lambda thr, disp, exact="": (  # noqa: E731
            f'ConvertFrom-VenueProbeHeatLine -Lines @("  tele heat source=x age=1.0s hottest=50.0C max15=60.0C mean={disp}C throttled={thr}/60 W=10{exact}")')
        sym = "$([char]0x221E)"
        cases = [(7, "NaN", " mean_exact=NaN", "hot"), (7, "NaN", "", "hot"), (7, sym, "", "hot"), (7, "-" + sym, "", "hot"), (7, sym, " mean_exact=Infinity", "hot"),
                 (7, "-" + sym, " mean_exact=-Infinity", "hot"), (0, sym, " mean_exact=Infinity", "hot"), (0, sym, "", "hot"), (0, "NaN", " mean_exact=NaN", "unknown"),
                 (0, "-" + sym, " mean_exact=-Infinity", "unknown"), (7, "70.0", "", "hot"), (0, "70.0", "", "clear"), (0, "89,0", "", "hot"), (7, "-1.0", "", "hot")]
        for thr, disp, exact, want in cases:
            got_line = ps_json(tele(thr, disp, exact))
            self.assertEqual(got_line["heat"], want, (thr, disp, exact, got_line))

    def test_r5p1_a1_a_display_line_with_mean_n_a_and_a_readable_count_still_decides_on_the_count(self) -> None:
        # adversary r5 pass 1 A1: after the probe's JSON round trip a NaN / Infinity mean is a string, which Format-TelNum prints as 'n/a' WITHOUT the C unit and with no mean_exact:
        # "mean=n/a throttled=7/60" matched no pattern, so the whole line read unknown and the gate ran the leg while the agent read hot
        quiet = QUIET.read_text(encoding="utf-8")
        self.assertIn("if ($null -eq $v -or -not ($v -is [ValueType])) { 'n/a' }", quiet)
        line = lambda thr, rest="": f'"  tele heat source=x age=1.0s hottest=70.0C max15=70.0C mean=n/a throttled={thr}/60 W=10.0{rest}"'  # noqa: E731
        got = ps_json(f"@((ConvertFrom-VenueProbeHeatLine -Lines @('  raw UNWRITABLE Access denied', {line(7)})), (ConvertFrom-VenueProbeHeatLine -Lines @({line(0)})), (ConvertFrom-VenueProbeHeatLine -Lines @({line(6)})), "
                      "(ConvertFrom-VenueProbeHeatLine -Lines @('  tele heat source=unavailable age=n/a hottest=n/a max15=n/a mean=n/a throttled=n/a/n/a W=n/a')))")
        self.assertEqual([g["heat"] for g in got], ["hot", "unknown", "unknown", "unknown"], got)
        self.assertEqual((got[0]["throttled"], got[0]["window"], got[0]["source"]), (7, 60, "tele-line"))

    def test_r5p1_a3_a_heat_from_entry_that_is_an_existing_path_is_kept_as_written(self) -> None:
        # adversary r5 pass 1 A3: the normalizing line trimmed EVERY entry, an existing directory ' prior' included, so it became 'prior' (absent): the history was skipped and the
        # plan kept its adjacent heavy legs. Only the pieces of a comma split are trimmed; an entry that is an existing path is left exactly as given.
        import tempfile
        root = Path(tempfile.mkdtemp(prefix="thermal-heatfrom-"))
        (root / " prior").mkdir()
        runner = (DV / "Invoke-VenueLeaseBatch.ps1").read_text(encoding="utf-8")
        line = [l for l in runner.splitlines() if re.match(r"^\$HeatFrom = @\(\$HeatFrom \| ForEach-Object", l)][0]
        script = (f"$ErrorActionPreference='Stop'; Set-Location -LiteralPath '{root}'; $HeatFrom = @(' prior', ' no-such-a , no-such-b ', '.'); " + line
                  + "; ConvertTo-Json -Compress -InputObject @($HeatFrom)")
        r = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(json.loads(r.stdout), [" prior", "no-such-a", "no-such-b", "."], r.stdout)

    def test_r5p1_a2_nothing_but_the_process_start_follows_the_thermal_dispatch_admission(self) -> None:
        # adversary r5 pass 1 A2: `& $pwshExe @qArgs *> $out` created the output file AFTER the admission, and that open can stall (a UNC RunDir): a probe was bought past the
        # deadline. The probe's output is captured in the pipeline, and the file is written once it has returned.
        runner = (DV / "Invoke-VenueLeaseBatch.ps1").read_text(encoding="utf-8").replace("\r\n", "\n")
        fn = runner[runner.index("function Invoke-QuietProbe("):runner.index("function Submit-AgentJob(")]
        self.assertNotIn("*> $out", fn, "the redirect opens the output file after the admission")
        marker = "return [pscustomobject]@{ expiredBeforeDispatch = $true }\n    }\n"
        after = [l for l in fn[fn.index(marker) + len(marker):].splitlines() if l.strip() and not l.strip().startswith("#")]
        self.assertTrue(after[0].lstrip().startswith("$captured = @(& $pwshExe @qArgs *>&1"), after[0])
        self.assertIn("Set-Content -LiteralPath $out -Value $captured", fn)

    def test_r6p1_a2_only_a_launched_row_is_a_heavy_predecessor_and_an_unknown_status_is_a_barrier(self) -> None:
        # adversary r6 A2 on 4dbd043e: a history row with status null (read as '') or NOT_RUN was taken as the launched predecessor, so the hot leg after it made its key heavy
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="thermal-status-"))
        for status in (None, "NOT_RUN", "", "SOMETHING_NEW"):
            with self.subTest(status=status):
                (d / "batches.jsonl").write_text(json.dumps({"schema": "mlv-app/venue-lease-batch/v1", "batch": 1, "token": "t1", "legs": [
                    {"name": "01-a-cpu-A", "backend": "cpu", "arm": "A", "status": status, "heatAtProbe": "clear"},
                    {"name": "02-b-cpu-A", "backend": "cpu", "arm": "A", "status": "RAN", "heatAtProbe": "hot"}]}) + "\n", encoding="utf-8")
                got = ps_json(f"$h = Read-VenueLegHeatHistory -RunDir @('{d}'); [pscustomobject](Get-VenueHeavyLegKeys -Rows $h.rows)")
                self.assertEqual(got, {}, f"a row that is not known to have launched made its key heavy: {got}")
        # controls: each launched status is a predecessor; a known never-launched row (a thermal skip, a retry, NOT_RUN) is passed over to the launched row before it
        rows = lambda st: f"[pscustomobject]@{{ batch = '1'; key = 'a|cpu'; status = '{st}'; heat = 'clear' }}, [pscustomobject]@{{ batch = '1'; key = 'x|cpu'; status = 'NOT_RUN'; heat = 'clear' }}, [pscustomobject]@{{ batch = '1'; key = 'b|cpu'; status = 'RAN'; heat = 'hot' }}"  # noqa: E731
        for st in ("RAN", "LEG_FAILED", "LEASE_EXPIRED"):
            self.assertEqual(ps_json(f"[pscustomobject](Get-VenueHeavyLegKeys -Rows @({rows(st)}))"), {"a|cpu": 1}, st)

    def test_r7_a2_a_row_whose_probe_heat_is_unknown_reads_its_own_recorded_grant_heat(self) -> None:
        # adversary r7 A2 on f558e2e2: the runner writes heatAtGrant (the agent's admission.heat) on every RAN row, but the reader took heatAtProbe and then only the
        # agent-result-<jobId>.json sidecar. With the probe unreadable and no sidecar beside batches.jsonl, the hot grant was read unknown and a|cpu was never heavy.
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="thermal-grant-"))

        def history(second: dict) -> None:
            (d / "batches.jsonl").write_text(json.dumps({"schema": "mlv-app/venue-lease-batch/v1", "batch": 1, "token": "t", "leaseId": "L", "legs": [
                {"name": "01-a-cpu-A", "backend": "cpu", "arm": "A", "status": "RAN", "jobId": "j1", "jobLease": "L", "onLease": True, "heatAtProbe": "clear", "heatAtGrant": "clear"},
                {"name": "02-b-cpu-A", "backend": "cpu", "arm": "A", "status": "RAN", "jobId": "j2", "jobLease": "L", "onLease": True, **second}]}) + "\n", encoding="utf-8")

        spread = ("$h = Read-VenueLegHeatHistory -RunDir @('<D>'); $hk = Get-VenueHeavyLegKeys -Rows @($h.rows); $items = @('a-A', 'a-B', 'b-A', 'b-B'); "
                  "$flags = [bool[]]@($items | ForEach-Object { $hk.Contains(($_ -replace '-[AB]$', '') + '|cpu') }); "
                  "[pscustomobject]@{ heat = @($h.rows | ForEach-Object { $_.heat }); heavy = @($hk.Keys); order = (@((Get-VenueThermalSpreadOrder -Items $items -Heavy $flags).items) -join ',') }")
        run = lambda: ps_json(spread.replace("<D>", str(d)))  # noqa: E731
        history({"heatAtProbe": "unknown", "heatAtGrant": "hot"})
        got = run()
        self.assertEqual((got["heat"], got["heavy"], got["order"]), (["clear", "hot"], ["a|cpu"], "a-A,b-A,a-B,b-B"), got)
        # the probe's own reading still comes first, and the sidecar is still read when the row carries no usable grant heat
        history({"heatAtProbe": "clear", "heatAtGrant": "hot"})
        self.assertEqual(run()["heat"], ["clear", "clear"])
        history({"heatAtProbe": "unknown", "heatAtGrant": "unknown"})
        (d / "agent-result-j2.json").write_text(json.dumps({"admission": {"heat": "hot"}}), encoding="utf-8")
        self.assertEqual(run()["heat"], ["clear", "hot"])
        # a grant heat that is not hot or clear is never taken: the row stays unknown without a sidecar
        (d / "agent-result-j2.json").unlink()
        history({"heatAtProbe": "unknown", "heatAtGrant": "warm"})
        self.assertEqual(run()["heat"], ["clear", "unknown"])

    def test_r11_sol_r10_b1_adv_a2_distinct_leases_never_share_a_predecessor_when_receipt_identities_collide(self) -> None:
        # sol r10 B1 / adversary A2 on 16a8a2ff: a whitespace receipt token was accepted as an identity, and a generated "line1" fallback shared a namespace with a literal
        # token "line1", so two receipts on DISTINCT leases (L1, L2) got the same batch identity and L2's hot first leg was charged to L1's last: a|cpu became heavy and
        # the order a-A,a-B,b-A,b-B was spread to a-A,b-A,a-B,b-B. Controls: distinct tokens and two missing tokens keep the original order.
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="thermal-identity-"))
        spread = ("$h = Read-VenueLegHeatHistory -RunDir @('<D>'); $hk = Get-VenueHeavyLegKeys -Rows @($h.rows); $items = @('a-A', 'a-B', 'b-A', 'b-B'); "
                  "$flags = [bool[]]@($items | ForEach-Object { $hk.Contains(($_ -replace '-[AB]$', '') + '|cpu') }); "
                  "[pscustomobject]@{ batches = @($h.rows | ForEach-Object { $_.batch }); heavy = @($hk.Keys); order = (@((Get-VenueThermalSpreadOrder -Items $items -Heavy $flags).items) -join ',') }")

        def leg(name: str, heat: str) -> dict:
            return {"name": f"01-{name}-cpu-A", "backend": "cpu", "arm": "A", "status": "RAN", "heatAtProbe": heat}

        def receipt(ident: dict, legs: list) -> str:
            return json.dumps({"schema": "mlv-app/venue-lease-batch/v1", "batch": 1, **ident, "legs": legs}) + "\n"

        def run(i1: dict, i2: dict) -> dict:
            (d / "batches.jsonl").write_text(receipt(i1, [leg("a", "clear")]) + receipt(i2, [leg("b", "hot")]), encoding="utf-8")
            return ps_json(spread.replace("<D>", str(d)))

        original = "a-A,a-B,b-A,b-B"
        # (a) two whitespace tokens on distinct leases
        got = run({"token": " ", "leaseId": "L1"}, {"token": " ", "leaseId": "L2"})
        self.assertEqual((got["heavy"], got["order"]), ([], original), got)
        self.assertEqual(len(set(got["batches"])), 2, got)
        # (b) a missing token on line 1 and a literal token "line1" on line 2 (the generated fallback must not equal a literal token)
        got = run({"leaseId": "L1"}, {"token": "line1", "leaseId": "L2"})
        self.assertEqual((got["heavy"], got["order"]), ([], original), got)
        self.assertEqual(len(set(got["batches"])), 2, got)
        # sweep: the same literal token on distinct leases, and an empty or non-string token, are not one lease either
        for t1, t2 in (("t", "t"), ("", ""), (None, None), (7, 7), ("  ", "\t")):
            got = run({"token": t1, "leaseId": "L1"}, {"token": t2, "leaseId": "L2"})
            self.assertEqual((got["heavy"], got["order"]), ([], original), (t1, t2, got))
        # controls: distinct tokens, and two missing tokens, keep the original order
        for i1, i2 in (({"token": "t1", "leaseId": "L1"}, {"token": "t2", "leaseId": "L2"}), ({"leaseId": "L1"}, {"leaseId": "L2"}), ({}, {})):
            got = run(i1, i2)
            self.assertEqual((got["heavy"], got["order"]), ([], original), (i1, i2, got))
        # the real thing still works: ONE receipt (one lease) whose second leg reads hot makes the first heavy, whatever the token looks like
        for tok in ("t", " t ", "line1"):
            (d / "batches.jsonl").write_text(receipt({"token": tok, "leaseId": "L1"}, [leg("a", "clear"), leg("b", "hot")]), encoding="utf-8")
            got = ps_json(spread.replace("<D>", str(d)))
            self.assertEqual((got["heavy"], got["order"]), (["a|cpu"], "a-A,b-A,a-B,b-B"), (tok, got))

    def test_r11_sol_r10_b1_direct_helper_rows_with_an_empty_or_whitespace_batch_are_leases_of_their_own(self) -> None:
        # the class sweep of sol r10 B1: Get-VenueHeavyLegKeys joined two rows whose batch was '' or '  ' (equal strings) into one lease, as it never did for $null
        def rows(b1, b2) -> str:
            lit = lambda v: "$null" if v is None else f"'{v}'"  # noqa: E731
            return (f"$k = Get-VenueHeavyLegKeys -Rows @([pscustomobject]@{{ batch = {lit(b1)}; key = 'a|cpu'; status = 'RAN'; heat = 'clear' }}, "
                    f"[pscustomobject]@{{ batch = {lit(b2)}; key = 'b|cpu'; status = 'RAN'; heat = 'hot' }}); (@($k.Keys) -join ',')")
        for b1, b2 in ((None, None), ("", ""), (" ", " "), ("\t", "\t"), ("", None)):
            self.assertEqual(ps_json(rows(b1, b2)), "", (b1, b2))
        # controls: the same real batch is one lease (heavy), distinct batches are not
        self.assertEqual(ps_json(rows("1.t", "1.t")), "a|cpu")
        self.assertEqual(ps_json(rows("1.t", "1.u")), "")

    def test_r12_adversary_a1_a2_lease_identity_is_compared_ordinally_on_the_raw_lease_id(self) -> None:
        # adversary r11 A1 / A2 on e5ff0114: the identity trimmed the leaseId (' L1' == 'L1'), and PowerShell -eq / -ceq compare culture-aware, so 'L<SHY>1' == 'L1<SHY>' (U+00AD is
        # ignorable). Both made two DISTINCT leases one thermal sequence: b's hot first leg was charged to a|cpu (heavy), and a-A,a-B,b-A,b-B was spread to a-A,b-A,a-B,b-B.
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="thermal-raw-lease-"))
        shy = chr(0xAD)
        spread = ("$h = Read-VenueLegHeatHistory -RunDir @('<D>'); $hk = Get-VenueHeavyLegKeys -Rows @($h.rows); $items = @('a-A', 'a-B', 'b-A', 'b-B'); "
                  "$flags = [bool[]]@($items | ForEach-Object { $hk.Contains(($_ -replace '-[AB]$', '') + '|cpu') }); "
                  "[pscustomobject]@{ batches = @($h.rows | ForEach-Object { $_.batch }); heat = @($h.rows | ForEach-Object { $_.heat }); heavy = @($hk.Keys); "
                  "order = (@((Get-VenueThermalSpreadOrder -Items $items -Heavy $flags).items) -join ',') }")

        def leg(name: str, heat: str, **more) -> dict:
            return {"name": f"01-{name}-cpu-A", "backend": "cpu", "arm": "A", "status": "RAN", "heatAtProbe": heat, **more}

        def receipt(ident: dict, legs: list) -> str:
            return json.dumps({"schema": "mlv-app/venue-lease-batch/v1", "batch": 1, **ident, "legs": legs}) + "\n"

        def run(i1: dict, i2: dict) -> dict:
            (d / "batches.jsonl").write_text(receipt(i1, [leg("a", "clear")]) + receipt(i2, [leg("b", "hot")]), encoding="utf-8")
            return ps_json(spread.replace("<D>", str(d)))

        original, chained = "a-A,a-B,b-A,b-B", "a-A,b-A,a-B,b-B"
        # A1: ids that differ only by surrounding whitespace are distinct leases; A2: ids that differ only by an ignorable character are distinct leases
        for l1, l2 in (("L1", " L1"), ("L1", "L1 "), ("L1", "L1\t"), (" L1", "L1"), ("L" + shy + "1", "L1" + shy), ("L1", "L1" + shy), ("L1", "L" + shy + "1")):
            got = run({"token": "t", "leaseId": l1}, {"token": "t", "leaseId": l2})
            self.assertEqual((got["heavy"], got["order"]), ([], original), (l1, l2, got))
            self.assertEqual(len(set(got["batches"])), 2, (l1, l2, got))
        # control: the SAME exact lease id (and token) across two receipts is one lease and chains; so does a lease id that is itself spelled with a soft hyphen or spaces
        for lid in ("L1", "L1" + shy, " L1 ", "L" + shy + "1"):
            got = run({"token": "t", "leaseId": lid}, {"token": "t", "leaseId": lid})
            self.assertEqual((got["heavy"], got["order"]), (["a|cpu"], chained), (lid, got))
        # a whitespace-only lease id is a barrier, not a match; an absent or empty one is a receipt older than the lease id and is still identified by its token
        for lid in (" ", "\t", "   "):
            got = run({"token": "t", "leaseId": lid}, {"token": "t", "leaseId": lid})
            self.assertEqual((got["heavy"], got["order"]), ([], original), (lid, got))
        for ident in ({"token": "t"}, {"token": "t", "leaseId": ""}, {"token": "t", "leaseId": None}):
            got = run(ident, ident)
            self.assertEqual((got["heavy"], got["order"]), (["a|cpu"], chained), (ident, got))
        # A2 sibling: a leg row with no onLease field takes the sidecar's heat only when the sidecar names the batch's EXACT lease id
        (d / "batches.jsonl").write_text(receipt({"token": "t", "leaseId": "L1"}, [leg("a", "clear"), {"name": "02-b-cpu-A", "backend": "cpu", "arm": "A", "status": "LEG_FAILED", "jobId": "j2", "heatAtProbe": "unknown"}]), encoding="utf-8")
        for sid, want in (("L1", "hot"), ("L1" + shy, "unknown"), ("L" + shy + "1", "unknown"), (" L1", "unknown"), ("L1 ", "unknown")):
            (d / "agent-result-j2.json").write_text(json.dumps({"admission": {"heat": "hot", "leaseBatch": {"leaseId": sid}, "slotContinuity": "kept"}}), encoding="utf-8")
            self.assertEqual(ps_json(spread.replace("<D>", str(d)))["heat"], ["clear", want], sid)
        # a whitespace-only batch lease id can never be proven by a sidecar
        (d / "batches.jsonl").write_text(receipt({"token": "t", "leaseId": " "}, [leg("a", "clear"), {"name": "02-b-cpu-A", "backend": "cpu", "arm": "A", "status": "LEG_FAILED", "jobId": "j2", "heatAtProbe": "unknown"}]), encoding="utf-8")
        (d / "agent-result-j2.json").write_text(json.dumps({"admission": {"heat": "hot", "leaseBatch": {"leaseId": " "}, "slotContinuity": "kept"}}), encoding="utf-8")
        self.assertEqual(ps_json(spread.replace("<D>", str(d)))["heat"], ["clear", "unknown"])

    def test_r12_adversary_a2_heavy_leg_keys_compares_the_batch_ordinally(self) -> None:
        # the direct helper (VenueThermalGate.psm1:189 -cne): two rows whose batch differs only by an ignorable character are two leases
        def rows(b1: str, b2: str) -> str:
            return (f"$shy = [string][char]0x00AD; $k = Get-VenueHeavyLegKeys -Rows @([pscustomobject]@{{ batch = {b1}; key = 'a|cpu'; status = 'RAN'; heat = 'clear' }}, "
                    f"[pscustomobject]@{{ batch = {b2}; key = 'b|cpu'; status = 'RAN'; heat = 'hot' }}); (@($k.Keys) -join ',')")
        self.assertEqual(ps_json(rows("('1.t' + $shy)", "'1.t'")), "")
        self.assertEqual(ps_json(rows("('1.' + $shy + 't')", "('1.t' + $shy)")), "")
        self.assertEqual(ps_json(rows("'1.t'", "'1.t'")), "a|cpu")
        self.assertEqual(ps_json(rows("('1.t' + $shy)", "('1.t' + $shy)")), "a|cpu")

    def _r13_chain(self, d: Path, ident1: dict, ident2: dict) -> dict:
        """Two lease-batch receipts in d/batches.jsonl: lease 1 runs a (clear), lease 2 runs b (hot at its first probe). Returns the heavy keys and the spread order."""
        def leg(name: str, heat: str) -> dict:
            return {"name": f"01-{name}-cpu-A", "backend": "cpu", "arm": "A", "status": "RAN", "heatAtProbe": heat}

        def receipt(ident: dict, legs: list) -> str:
            return json.dumps({"schema": "mlv-app/venue-lease-batch/v1", "batch": 1, **ident, "legs": legs}) + "\n"

        return self._chain_text(d, receipt(ident1, [leg("a", "clear")]) + receipt(ident2, [leg("b", "hot")]))

    @staticmethod
    def _chain_text(d: Path, text: str) -> dict:
        """d/batches.jsonl is exactly `text` (lines of lease-batch receipts); returns the batch ids read, the heavy keys and the spread order."""
        spread = ("$h = Read-VenueLegHeatHistory -RunDir @('<D>'); $hk = Get-VenueHeavyLegKeys -Rows @($h.rows); $items = @('a-A', 'a-B', 'b-A', 'b-B'); "
                  "$flags = [bool[]]@($items | ForEach-Object { $hk.Contains(($_ -replace '-[AB]$', '') + '|cpu') }); "
                  "[pscustomobject]@{ batches = @($h.rows | ForEach-Object { $_.batch }); heavy = @($hk.Keys); order = (@((Get-VenueThermalSpreadOrder -Items $items -Heavy $flags).items) -join ',') }")
        (d / "batches.jsonl").write_text(text, encoding="utf-8")
        return ps_json(spread.replace("<D>", str(d)))

    # r14 (adversary r13 A2/A3/A4, Dot I1): the raw lease identity is recovered exactly or the receipt is a barrier with no identity (never a legacy token-only receipt)
    _R14_LEASES = ("2026-10-30T10:00:00Z", "2026-10-30T10:00:00.0Z")

    def _r14_lines(self, shape: str, lease1: str, lease2: str) -> str:
        def leg(name: str, heat: str) -> dict:
            return {"name": f"01-{name}-cpu-A", "backend": "cpu", "arm": "A", "status": "RAN", "heatAtProbe": heat}

        def rec(lease: str, name: str, heat: str) -> str:
            body = {"schema": "mlv-app/venue-lease-batch/v1", "batch": 1, "leaseId": lease, "token": "t", "legs": [leg(name, heat)]}
            text = json.dumps(body)
            if shape == "case":
                return text.replace('"leaseId"', '"LeaseId"')
            if shape == "unwrapped":
                return "[" + text + "]"
            if shape == "single-quoted":
                return text.replace('"', "'")
            return text
        return rec(lease1, "a", "clear") + "\n" + rec(lease2, "b", "hot") + "\n"

    def test_r14_adversary_a2_a3_a4_distinct_timestamp_leases_in_any_accepted_shape_are_never_chained(self) -> None:
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="thermal-r14-shapes-"))
        original = "a-A,a-B,b-A,b-B"
        for shape in ("plain", "case", "unwrapped", "single-quoted"):
            got = self._chain_text(d, self._r14_lines(shape, *self._R14_LEASES))
            self.assertEqual((got["heavy"], got["order"]), ([], original), (shape, got))

    def test_r14_same_lease_control_a_plain_or_case_variant_receipt_still_chains_but_an_unrecoverable_one_is_a_barrier(self) -> None:
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="thermal-r14-control-"))
        original, chained = "a-A,a-B,b-A,b-B", "a-A,b-A,a-B,b-B"
        lease = self._R14_LEASES[0]
        for shape in ("plain", "case"):
            got = self._chain_text(d, self._r14_lines(shape, lease, lease))
            self.assertEqual((got["heavy"], got["order"]), (["a|cpu"], chained), (shape, got))
        # the raw identity of these cannot be recovered exactly, so even the SAME lease is two barriers (fail closed: no predecessor, never legacy token-only)
        for shape in ("unwrapped", "single-quoted"):
            got = self._chain_text(d, self._r14_lines(shape, lease, lease))
            self.assertEqual((got["heavy"], got["order"]), ([], original), (shape, got))

    def test_r14_dot_i1_an_unrecoverable_receipt_still_needs_the_on_lease_proof_for_a_recorded_grant_heat(self) -> None:
        # a strict-parser rejection (single quotes) used to null the lease id, which removed the missing-onLease proof: the row's recorded grant heat was then trusted
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="thermal-r14-proof-"))
        a = {"name": "01-a-cpu-A", "backend": "cpu", "arm": "A", "status": "RAN", "onLease": True, "heatAtProbe": "clear"}
        b = {"name": "02-b-cpu-A", "backend": "cpu", "arm": "A", "status": "LEG_FAILED", "heatAtGrant": "hot"}   # no onLease field
        script = "$h = Read-VenueLegHeatHistory -RunDir @('<D>'); [pscustomobject]@{ heat = @($h.rows | ForEach-Object { $_.heat }) }"

        def heat(text: str) -> list:
            (d / "batches.jsonl").write_text(text + "\n", encoding="utf-8")
            return ps_json(script.replace("<D>", str(d)))["heat"]
        receipt = {"schema": "mlv-app/venue-lease-batch/v1", "batch": 1, "token": "t", "legs": [a, b]}
        self.assertEqual(heat(json.dumps({**receipt, "leaseId": "L1"})), ["clear", "unknown"])                       # the proof is needed with a lease id
        self.assertEqual(heat(json.dumps({**receipt, "leaseId": "2026-10-30T10:00:00Z"}).replace('"', "'")), ["clear", "unknown"])   # and when the lease id is unrecoverable
        self.assertEqual(heat(json.dumps(receipt)), ["clear", "hot"])                                              # a receipt older than the lease id reads as before

    def test_r14_a_line_that_is_not_a_readable_receipt_is_a_barrier_row_named_in_skipped(self) -> None:
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="thermal-r14-unreadable-"))
        both_cases = '{"leaseId": "L1", "LeaseId": "L2", "token": "t", "legs": []}'   # ConvertFrom-Json refuses two properties that differ only by case
        (d / "batches.jsonl").write_text(both_cases + "\n", encoding="utf-8")
        got = ps_json("$h = Read-VenueLegHeatHistory -RunDir @('" + str(d) + "'); [pscustomobject]@{ batches = @($h.rows | ForEach-Object { $_.batch }); keys = @($h.rows | ForEach-Object { $_.key }); skipped = @($h.skipped) }")
        self.assertEqual(got["batches"], ["1.unreadable:1"], got)
        self.assertEqual(len(got["skipped"]), 1, got)
        self.assertIn("unreadable line", got["skipped"][0])

    def test_r13_adversary_a1_timestamp_shaped_lease_ids_that_read_as_one_datetime_are_distinct_leases(self) -> None:
        # adversary r12 A1 on 08c9aef5 (VenueThermalGate.psm1:249): `$line | ConvertFrom-Json` turns a timestamp-shaped leaseId / token into a DateTime BEFORE the [string] cast, so two
        # DISTINCT lease ids that read as one instant became one identity and b's hot first leg was charged to a|cpu. The identity is the RAW string the receipt carries.
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="thermal-datetime-lease-"))
        original, chained = "a-A,a-B,b-A,b-B", "a-A,b-A,a-B,b-B"
        same_instant = ("2026-10-30T10:00:00Z", "2026-10-30T10:00:00.0Z"), ("2026-10-30T10:00:00Z", "2026-10-30T10:00:00+00:00"), ("2026-10-30T10:00:00Z", "2026-10-30T05:00:00-05:00")
        for l1, l2 in same_instant:
            got = self._r13_chain(d, {"token": "t", "leaseId": l1}, {"token": "t", "leaseId": l2})
            self.assertEqual((got["heavy"], got["order"]), ([], original), (l1, l2, got))
            # the token is an identity field too: the same lease id with two timestamp-shaped tokens is two leases
            got = self._r13_chain(d, {"token": l1, "leaseId": "L1"}, {"token": l2, "leaseId": "L1"})
            self.assertEqual((got["heavy"], got["order"]), ([], original), (l1, l2, got))
        # controls: the SAME raw lease id (and token) is one lease and chains
        for ident in ({"token": "t", "leaseId": "2026-10-30T10:00:00Z"}, {"token": "2026-10-30T10:00:00Z", "leaseId": "L1"}, {"token": "t", "leaseId": "L1"}):
            got = self._r13_chain(d, ident, dict(ident))
            self.assertEqual((got["heavy"], got["order"]), (["a|cpu"], chained), (ident, got))

    def test_r13_adversary_a1_the_agent_result_sidecar_proves_the_batch_lease_by_the_raw_lease_id(self) -> None:
        # sibling (VenueThermalGate.psm1:293/298): the sidecar's admission.leaseBatch.leaseId was read through ConvertFrom-Json too, so a sidecar whose lease id only READS as the
        # batch's instant proved a hot grant that ran on ANOTHER lease and charged it to this lease's predecessor
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="thermal-datetime-sidecar-"))
        lease = "2026-10-30T10:00:00Z"
        a = {"name": "01-a-cpu-A", "backend": "cpu", "arm": "A", "status": "RAN", "onLease": True, "heatAtProbe": "clear"}
        b = {"name": "02-b-cpu-A", "backend": "cpu", "arm": "A", "status": "LEG_FAILED", "jobId": "j2"}   # no onLease field: the sidecar must show the job on the batch's lease
        (d / "batches.jsonl").write_text(json.dumps({"schema": "mlv-app/venue-lease-batch/v1", "batch": 1, "token": "t", "leaseId": lease, "legs": [a, b]}) + "\n", encoding="utf-8")
        script = "$h = Read-VenueLegHeatHistory -RunDir @('<D>'); [pscustomobject]@{ heat = @($h.rows | ForEach-Object { $_.heat }) }"

        def heat(sidecar_lease: str) -> list:
            (d / "agent-result-j2.json").write_text(json.dumps({"admission": {"heat": "hot", "leaseBatch": {"leaseId": sidecar_lease}}}), encoding="utf-8")
            return ps_json(script.replace("<D>", str(d)))["heat"]

        self.assertEqual(heat("2026-10-30T10:00:00.0Z"), ["clear", "unknown"])
        self.assertEqual(heat("2026-10-30T05:00:00-05:00"), ["clear", "unknown"])
        self.assertEqual(heat(lease), ["clear", "hot"])   # control: the same raw lease id is proof

    def test_r13_dot_residual2_the_token_is_the_raw_string_never_trimmed(self) -> None:
        # Dot residual 2 / Grok G1 (VenueThermalGate.psm1:257): the token was .Trim()ed, so receipts with no leaseId and tokens 't' and ' t' collapsed to one identity
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="thermal-raw-token-"))
        original, chained = "a-A,a-B,b-A,b-B", "a-A,b-A,a-B,b-B"
        for t1, t2 in (("t", " t"), ("t", "t "), (" t", "t"), ("t", "t\t"), ("t", "\tt")):
            got = self._r13_chain(d, {"token": t1}, {"token": t2})
            self.assertEqual((got["heavy"], got["order"]), ([], original), (t1, t2, got))
            got = self._r13_chain(d, {"token": t1, "leaseId": "L1"}, {"token": t2, "leaseId": "L1"})
            self.assertEqual((got["heavy"], got["order"]), ([], original), (t1, t2, got))
        # controls: the same raw token chains (with and without a lease id), and a whitespace-only token is no token: a barrier, as before
        for ident in ({"token": " t"}, {"token": "t"}, {"token": " t ", "leaseId": "L1"}):
            got = self._r13_chain(d, ident, dict(ident))
            self.assertEqual((got["heavy"], got["order"]), (["a|cpu"], chained), (ident, got))
        for blank in ("", " ", "\t"):
            got = self._r13_chain(d, {"token": blank}, {"token": blank})
            self.assertEqual((got["heavy"], got["order"]), ([], original), (blank, got))

    def test_r13_adversary_a2_heavy_leg_keys_are_case_sensitive(self) -> None:
        # adversary r12 A2 on 08c9aef5 (VenueThermalGate.psm1:180): [ordered]@{} is case-insensitive, so a heavy key 'A|cpu' also answered .Contains('a|cpu') (the runner's lookup, Invoke-VenueLeaseBatch.ps1:1149)
        # and 'A|cpu' / 'a|cpu' were one key. Leg ids are case-sensitive names (the leg spec's legId allows both cases), so the keys are compared ordinally.
        def keys(*pairs: tuple[str, str]) -> str:
            rows = ", ".join(f"[pscustomobject]@{{ batch = 'L'; key = '{k}'; status = 'RAN'; heat = '{h}' }}" for k, h in pairs)
            return f"$k = Get-VenueHeavyLegKeys -Rows @({rows}); [pscustomobject]@{{ keys = @($k.Keys); upper = [bool]$k.Contains('A|cpu'); lower = [bool]$k.Contains('a|cpu') }}"
        got = ps_json(keys(("A|cpu", "clear"), ("x|cpu", "hot")))
        self.assertEqual((got["keys"], got["upper"], got["lower"]), (["A|cpu"], True, False), got)
        got = ps_json(keys(("a|cpu", "clear"), ("x|cpu", "hot")))
        self.assertEqual((got["keys"], got["upper"], got["lower"]), (["a|cpu"], False, True), got)
        # two distinct keys stay two (the insensitive dictionary merged them and counted two successors on one key)
        got = ps_json(keys(("A|cpu", "clear"), ("x|cpu", "hot"), ("a|cpu", "clear"), ("y|cpu", "hot")))
        self.assertEqual((sorted(got["keys"]), got["upper"], got["lower"]), (["A|cpu", "a|cpu"], True, True), got)

    def test_r7p2_a1_an_agent_heat_read_on_another_lease_is_never_charged_to_this_leases_predecessor(self) -> None:
        # adversary r7 A1 on 83fd79de: b ran on a replacement lease (jobLease L2, onLease false, LEASE_LOST); its grant heat is L2's, yet the reader took it and charged it
        # to a on L1, so a|cpu became heavy. The same holds for the agent-result sidecar's admission.heat (sibling): only a reading on the batch's own lease counts.
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="thermal-offlease-"))
        a = {"name": "01-a-cpu-A", "backend": "cpu", "arm": "A", "status": "RAN", "jobLease": "L1", "onLease": True, "heatAtProbe": "clear", "heatAtGrant": "clear"}

        def history(b: dict) -> None:
            (d / "batches.jsonl").write_text(json.dumps({"schema": "mlv-app/venue-lease-batch/v1", "batch": 1, "token": "t", "leaseId": "L1", "legs": [a, b]}) + "\n", encoding="utf-8")

        spread = ("$h = Read-VenueLegHeatHistory -RunDir @('<D>'); $hk = Get-VenueHeavyLegKeys -Rows @($h.rows); $items = @('a-A', 'a-B', 'b-A', 'b-B'); "
                  "$flags = [bool[]]@($items | ForEach-Object { $hk.Contains(($_ -replace '-[AB]$', '') + '|cpu') }); "
                  "[pscustomobject]@{ heat = @($h.rows | ForEach-Object { $_.heat }); heavy = @($hk.Keys); order = (@((Get-VenueThermalSpreadOrder -Items $items -Heavy $flags).items) -join ',') }")
        run = lambda: ps_json(spread.replace("<D>", str(d)))  # noqa: E731
        off = {"name": "02-b-cpu-A", "backend": "cpu", "arm": "A", "status": "RAN", "class": "LEASE_LOST", "counted": False, "jobLease": "L2", "onLease": False, "heatAtProbe": "unknown"}
        history({**off, "heatAtGrant": "hot"})
        got = run()
        self.assertEqual((got["heat"], got["heavy"], got["order"]), (["clear", "unknown"], [], "a-A,a-B,b-A,b-B"), got)
        # sibling: the sidecar's admission.heat of an off-lease job is not taken either
        history({**off, "jobId": "j2", "heatAtGrant": "unknown"})
        (d / "agent-result-j2.json").write_text(json.dumps({"admission": {"heat": "hot"}}), encoding="utf-8")
        self.assertEqual(run()["heat"], ["clear", "unknown"])
        # an onLease that is not exactly true (a string, null) is not proof of the same lease
        for v in ("true", None):
            history({**off, "onLease": v, "heatAtGrant": "hot"})
            self.assertEqual(run()["heat"], ["clear", "unknown"], v)
        # controls: on the batch's lease the grant and the sidecar are read; a row with no onLease field (LEG_FAILED, older receipts) keeps the sidecar read
        history({**off, "jobLease": "L1", "onLease": True, "class": "QUIET_PRELOAD_OK", "heatAtGrant": "hot"})
        self.assertEqual(run()["heat"], ["clear", "hot"])
        # r8 (adversary r8 A2 on 598b6e6b): a row with no onLease field (LEG_FAILED, LEASE_EXPIRED) takes the sidecar's heat only when the sidecar shows the job on the
        # batch's lease, the runner's own onLease rule (Invoke-VenueLeaseBatch.ps1: jobLease -ceq leaseId and slotContinuity not 'lost')
        failed = {"name": "02-b-cpu-A", "backend": "cpu", "arm": "A", "status": "LEG_FAILED", "jobId": "j2", "heatAtProbe": "unknown"}
        history(failed)
        (d / "agent-result-j2.json").write_text(json.dumps({"admission": {"heat": "hot", "leaseBatch": {"leaseId": "L1"}, "slotContinuity": "kept"}}), encoding="utf-8")
        self.assertEqual(run()["heat"], ["clear", "hot"])
        for adm in ({"heat": "hot", "leaseBatch": {"leaseId": "L2"}, "slotContinuity": "kept"}, {"heat": "hot", "leaseBatch": {"leaseId": "L1"}, "slotContinuity": "lost"},
                    {"heat": "hot"}, {"heat": "hot", "leaseBatch": None}, {"heat": "hot", "leaseBatch": {"leaseId": "l1"}}):
            (d / "agent-result-j2.json").write_text(json.dumps({"admission": adm}), encoding="utf-8")
            got = run()
            self.assertEqual((got["heat"], got["heavy"]), (["clear", "unknown"], []), adm)
        # a recorded grant heat on a row with no onLease field is not proof of the batch's lease either
        history({**failed, "heatAtGrant": "hot"})
        self.assertEqual(run()["heat"], ["clear", "unknown"])
        # the probe's own reading is the batch lease's, so it is kept whatever the job's lease
        history({**off, "heatAtProbe": "hot"})
        self.assertEqual(run()["heat"], ["clear", "hot"])

    def test_r8_a3_a_row_with_no_status_or_key_property_is_a_barrier_not_a_throw(self) -> None:
        # adversary r8 advisory A3: Get-VenueHeavyLegKeys read .status/.key/.heat/.batch under strict mode, so a row missing one threw instead of being a barrier
        got = ps_json("[pscustomobject](Get-VenueHeavyLegKeys -Rows @([pscustomobject]@{ batch = '1'; key = 'a|cpu'; status = 'RAN'; heat = 'clear' }, "
                      "[pscustomobject]@{ batch = '1'; key = 'z|cpu'; heat = 'clear' }, [pscustomobject]@{ batch = '1'; key = 'b|cpu'; status = 'RAN'; heat = 'hot' }))")
        self.assertEqual(got, {}, "a row with no status is not known to have launched: it is a barrier")
        got = ps_json("[pscustomobject](Get-VenueHeavyLegKeys -Rows @([pscustomobject]@{ batch = '1'; key = 'a|cpu'; status = 'RAN'; heat = 'clear' }, "
                      "[pscustomobject]@{ batch = '1'; status = 'RAN' }, [pscustomobject]@{ key = 'b|cpu'; status = 'RAN'; heat = 'hot' }))")
        self.assertEqual(got, {}, got)
        # adversary efe7699b A3: two rows with no batch were both batch '' and so "the same lease"; each is a lease of its own
        got = ps_json("[pscustomobject](Get-VenueHeavyLegKeys -Rows @([pscustomobject]@{ key = 'a|cpu'; status = 'RAN'; heat = 'clear' }, [pscustomobject]@{ key = 'b|cpu'; status = 'RAN'; heat = 'hot' }))")
        self.assertEqual(got, {}, got)
        # control: the same two rows on one named batch make a|cpu heavy
        got = ps_json("[pscustomobject](Get-VenueHeavyLegKeys -Rows @([pscustomobject]@{ batch = '1'; key = 'a|cpu'; status = 'RAN'; heat = 'clear' }, [pscustomobject]@{ batch = '1'; key = 'b|cpu'; status = 'RAN'; heat = 'hot' }))")
        self.assertEqual(got, {"a|cpu": 1}, got)

    def test_r9_sol_b3_a_null_key_row_is_a_barrier_whatever_its_status(self) -> None:
        # sol r8 B3 on fc0412d8: a null-key RETRY, THERMAL_HOT_SKIPPED or NOT_RUN row was passed over as a known never-launched row, so a|cpu was charged across a row of
        # unknown identity although the helper promises a null-key barrier. Every null-key (or empty-key) row is a barrier, for every status.
        for key in ("$null", "''"):
            for status in ("RETRY", "THERMAL_HOT_SKIPPED", "NOT_RUN", "RAN", "LEG_FAILED", "UNKNOWN"):
                with self.subTest(key=key, status=status):
                    got = ps_json("[pscustomobject](Get-VenueHeavyLegKeys -Rows @([pscustomobject]@{ batch = 'L'; key = 'a|cpu'; status = 'RAN'; heat = 'clear' }, "
                                  f"[pscustomobject]@{{ batch = 'L'; key = {key}; status = '{status}'; heat = 'unknown' }}, [pscustomobject]@{{ batch = 'L'; key = 'b|cpu'; status = 'RAN'; heat = 'hot' }}))")
                    self.assertEqual(got, {}, f"heat was charged across a row of unknown identity: {got}")
        # control: a KEYED never-launched row is still passed over to the launched row before it
        for status in ("RETRY", "THERMAL_HOT_SKIPPED", "NOT_RUN"):
            got = ps_json("[pscustomobject](Get-VenueHeavyLegKeys -Rows @([pscustomobject]@{ batch = 'L'; key = 'a|cpu'; status = 'RAN'; heat = 'clear' }, "
                          f"[pscustomobject]@{{ batch = 'L'; key = 'x|cpu'; status = '{status}'; heat = 'unknown' }}, [pscustomobject]@{{ batch = 'L'; key = 'b|cpu'; status = 'RAN'; heat = 'hot' }}))")
            self.assertEqual(got, {"a|cpu": 1}, status)

    def test_r8_efe7699b_a2_two_receipts_with_no_token_are_two_leases_not_one_batch_number(self) -> None:
        # adversary efe7699b A2: receipts with no token were grouped by batch number, so L1's batch 1 and L2's batch 1 became one sequence and L2's hot first leg
        # was charged to L1's last: a|cpu heavy from a reading it never preceded
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="thermal-notoken-"))
        l1 = {"batch": 1, "leaseId": "L1", "legs": [{"name": "01-a-cpu-A", "backend": "cpu", "arm": "A", "status": "RAN", "heatAtProbe": "clear"}]}
        l2 = {"batch": 1, "leaseId": "L2", "legs": [{"name": "01-b-cpu-A", "backend": "cpu", "arm": "A", "status": "RAN", "heatAtProbe": "hot"}]}
        q = f"$h = Read-VenueLegHeatHistory -RunDir @('{d}'); [pscustomobject](Get-VenueHeavyLegKeys -Rows @($h.rows))"
        (d / "batches.jsonl").write_text(json.dumps(l1) + "\n" + json.dumps(l2) + "\n", encoding="utf-8")
        self.assertEqual(ps_json(q), {}, "two token-less receipts were read as one lease")
        # control: one token-less receipt holding both legs is one lease
        (d / "batches.jsonl").write_text(json.dumps({**l1, "legs": l1["legs"] + l2["legs"]}) + "\n", encoding="utf-8")
        self.assertEqual(ps_json(q), {"a|cpu": 1})

    def test_r6_a3_a_positive_infinity_mean_reaches_the_gate_as_hot_without_the_raw_json(self) -> None:
        # adversary r5 A3 on 09f9f657: a feed of 60 rows '2026,a,b,Infinity,10,No,No' is hot to the agent (+Infinity >= 88). The probe's JSON round trip turns the mean into the string
        # 'Infinity', the display printed 'mean=n/a' with no mean_exact, and with the raw JSON unwritable (a sharing violation) the gate read unknown and ran the leg.
        # The probe's own functions are taken from Wait-VenueQuiet.ps1 (its feed reader, the telemetry line) and its JSON round trip is reproduced; the gate parses what it prints.
        quiet = QUIET.read_text(encoding="utf-8")
        self.assertIn("$telHeat.mean_c = [math]::Round([double]$r.m.meanC, 2); $telHeat.mean_c_exact = (ConvertTo-JsonSafeDouble $r.m.meanC)", quiet, "the probe's heat block moved: update this test")
        lines = quiet.splitlines()

        def fn(name: str) -> str:
            i = next(k for k, l in enumerate(lines) if l.startswith(f"function {name}"))
            out, depth = [], 0
            for l in lines[i:]:
                out.append(l)
                depth += l.count("{") - l.count("}")
                if depth == 0:
                    break
            return "\n".join(out)

        funcs = "\n".join(fn(n) for n in ("ConvertTo-IdentToken", "ConvertFrom-TelFeedRows", "Format-TelNum", "Get-TelReasonSuffix", "Write-TelemetryLines"))
        script = (
            "$HeatWindowRows = 60; $telemetrySchemaV1 = 'mlv-app/venue-quiet-probe-telemetry/v1'; function Write-Gate([string]$s) { $s }\n" + funcs + "\n"
            "function Get-Case([string]$mean) {\n"
            "  $r = ConvertFrom-TelFeedRows @(1..60 | ForEach-Object { \"2026,a,b,$mean,10,No,No\" })\n"
            "  $h = [ordered]@{ source = 'C:\\temp\\hwinfo-log-adapter.csv'; sample_age_s = 1.0; hottest_core_c = [double]$r.m.lastC; max15_c = [double]$r.m.max15C; mean_c = [math]::Round([double]$r.m.meanC, 2)\n"
            "    mean_c_exact = [double]$r.m.meanC; throttled = [int]$r.m.throttled; window_rows = [int]$r.m.window; package_w = [double]$r.m.watts30; reasons = [ordered]@{} }\n"
            "  $p = [ordered]@{ telemetry_schema = $telemetrySchemaV1; heat = $h; gpu = [ordered]@{ state = 'absent' }; last_lease = [ordered]@{ event = 'none' } } | ConvertTo-Json -Depth 6 | ConvertFrom-Json\n"
            "  $out = @('  raw UNWRITABLE The process cannot access the file because it is being used by another process.') + @(Write-TelemetryLines $p)\n"
            "  [pscustomobject]@{ heat = (ConvertFrom-VenueProbeHeatLine -Lines $out).heat; line = [string](@($out | Where-Object { $_ -match '^\\s*tele heat ' })[0]) }\n"
            "}\n"
            "@((Get-Case 'Infinity'), (Get-Case 'NaN'), (Get-Case '-Infinity'), (Get-Case '70.5'), (Get-Case '90'))")
        got = ps_json(script)
        self.assertEqual([g["heat"] for g in got], ["hot", "unknown", "unknown", "clear", "hot"], got)
        self.assertRegex(got[0]["line"], r" mean=n/a throttled=0/60 W=10\.0 mean_exact=Infinity$", "the probe names the non-finite mean it holds")
        self.assertTrue(got[1]["line"].endswith(" mean_exact=NaN") and got[2]["line"].endswith(" mean_exact=-Infinity"), got)
        self.assertTrue(got[3]["line"].endswith(" mean=70.5C throttled=0/60 W=10.0 mean_exact=70.5"), "a finite mean prints as before: " + got[3]["line"])
        # the line parser reads a signed +Infinity as the agent does (hot); NaN and -Infinity never decide alone
        tele = "(ConvertFrom-VenueProbeHeatLine -Lines @('  tele heat source=x age=1.0s hottest=n/a max15=n/a mean=n/a throttled=0/60 W=10.0 mean_exact={0}')).heat"
        self.assertEqual(ps_json("@(" + ", ".join("(" + tele.format(v) + ")" for v in ("+Infinity", "Infinity", "NaN", "-Infinity")) + ")"), ["hot", "hot", "unknown", "unknown"])

    def test_r5_sol_b2_a6_a_drive_relative_history_dir_resolves_against_the_powershell_location_not_the_process_directory(self) -> None:
        import os
        import tempfile
        root = Path(tempfile.mkdtemp(prefix="thermal-driverel-"))
        proc, psloc = root / "proc", root / "psloc"
        proc.mkdir()
        psloc.mkdir()

        def history(where: Path, second_heat: str) -> None:
            legs = [{"name": "01-fx-a-cpu-A", "backend": "cpu", "arm": "A", "status": "RAN", "heatAtProbe": "clear"},
                    {"name": "02-fx-b-cpu-A", "backend": "cpu", "arm": "A", "status": "RAN", "heatAtProbe": second_heat}]
            (where / "batches.jsonl").write_text(json.dumps({"batch": 1, "token": "t1", "legs": legs}) + "\n", encoding="utf-8")

        history(psloc, "hot")     # the intended history: the leg after fx-a read hot, so fx-a is heavy
        history(proc, "clear")    # the decoy at the process directory: nothing heavy
        drive = os.path.splitdrive(str(root))[0]
        tail = ("$h = Read-VenueLegHeatHistory -RunDir @(<DIRS>); $hk = Get-VenueHeavyLegKeys -Rows @($h.rows); $items = @('fx-a-A', 'fx-a-B', 'fx-b-A', 'fx-b-B'); "
                "$flags = [bool[]]@($items | ForEach-Object { $hk.Contains(($_ -replace '-[AB]$', '') + '|cpu') }); $sp = Get-VenueThermalSpreadOrder -Items $items -Heavy $flags; "
                "[pscustomobject]@{ order = (@($sp.items) -join ','); skipped = @($h.skipped).Count; resolved = (& (Get-Module VenueThermalGate) { Resolve-VenueHistoryChild -Dir '<DRIVE>.' -Child 'batches.jsonl' }) }")

        def run(dirs: str, location: Path) -> dict:
            script = f"$ErrorActionPreference='Stop'; Import-Module '{GATE}' -Force; Set-Location -LiteralPath '{location}'; ConvertTo-Json -Compress -InputObject $({tail.replace('<DIRS>', dirs).replace('<DRIVE>', drive)})"
            r = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120, cwd=str(proc))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            return json.loads(r.stdout)

        # the process directory is proc (the spawn cwd); the PowerShell location is psloc. A plain '.' and the drive-relative 'X:.' must both read psloc's history.
        want_order = "fx-a-A,fx-b-A,fx-a-B,fx-b-B"
        for spelling in ("'.'", f"'{drive}.'"):
            got = run(spelling, psloc)
            self.assertEqual(got["order"], want_order, (spelling, got))
            self.assertEqual(got["skipped"], 0, got)
        self.assertEqual(canonical_path(run("'.'", psloc)["resolved"]), canonical_path(psloc / "batches.jsonl"))
        # control: with the PowerShell location AT the process directory the decoy (nothing heavy) is what is read, so the spread keeps the given order
        self.assertEqual(run(f"'{drive}.'", proc)["order"], "fx-a-A,fx-a-B,fx-b-A,fx-b-B")
        # a drive-relative spelling with a sub-path, and an absolute path, resolve the same way
        (psloc / "sub").mkdir()
        history(psloc / "sub", "hot")
        self.assertEqual(run(f"'{drive}sub'", psloc)["order"], want_order)
        self.assertEqual(run(f"'{psloc}'", proc)["order"], want_order)

    def test_adversary_r3_a1_b2_one_function_decides_whether_another_probe_may_start(self) -> None:
        got = ps_json(
            "$b = { param($cap, $e, $slow, $want) $r = Get-VenueThermalProbeBudget -CapSec $cap -ElapsedSec $e -SlowestReprobeSec $slow -WantSleepSec $want; \"$($r.mayStartProbe)|$($r.fitSec)|$($r.sleepSec)\" }; "
            # Dot B2: the first hot reading has no re-probe behind it, so it always gets a cooling sleep (the long INITIAL probe is not an input at all)
            "$out = @((& $b 180 0 0 30), "
            # adversary A1: elapsed 3, slowest 3, a 300 s busy sleep: the sleep is cut to what fits, so the next probe starts at 177 and is predicted to end at 180
            "(& $b 180 3 3 300), "
            # the deadline minus the slowest re-probe has been reached: no further probe, whatever the want
            "(& $b 180 177 3 300), (& $b 180 180 0 30), (& $b 180 400 3 30), "
            # a cap of 0 never starts one
            "(& $b 0 0 0 30)); "
            # the invariant over a grid: when a probe may start, it starts no later than the deadline less the slowest re-probe
            "$bad = 0; foreach ($cap in 1, 5, 180) { foreach ($e in 0, 1, 4, 100, 179, 180, 200) { foreach ($s in 0, 1, 3, 50, 255) { foreach ($w in 1, 30, 300) { "
            "$r = Get-VenueThermalProbeBudget -CapSec $cap -ElapsedSec $e -SlowestReprobeSec $s -WantSleepSec $w; "
            "if ($r.mayStartProbe -and (($e + $r.sleepSec + $s -gt $cap) -or $r.sleepSec -lt 1 -or $r.sleepSec -gt $w)) { $bad++ }; "
            "if (-not $r.mayStartProbe -and ($e + 1 + $s -le $cap)) { $bad++ } } } } }; $out + \"bad=$bad\"")
        self.assertEqual(got, ["True|180|30", "True|174|174", "False|0|0", "False|0|0", "False|-223|0", "False|0|0", "bad=0"], got)
        # advisory A4: a first-hot date at DateTime.MaxValue plus a second used to throw an unrepresentable-date exception
        self.assertEqual(ps("(Get-VenueThermalNowUtc -FirstHotUtc ([datetime]::MaxValue) -FirstHotTimestamp 0 -NowTimestamp ([Diagnostics.Stopwatch]::Frequency)).Year"), "9999")
        runner = (DV / "Invoke-VenueLeaseBatch.ps1").read_text(encoding="utf-8")
        # every probe start of an active thermal wait goes through the function: the busy sleep, the poll sleep, and the busy branch's own guard
        self.assertGreaterEqual(runner.count("Get-VenueThermalProbeBudget"), 3)
        self.assertNotIn("$capRanOut", runner)
        self.assertNotIn("$sleepFit", runner)
        self.assertNotIn("Start-Sleep -Seconds ([int][Math]::Max(1, [Math]::Min($ClaimPollSec, $room - $legBudget)))", runner)

    def test_adversary_r2b_a5_a_null_list_parameter_is_empty_not_a_startup_crash(self) -> None:
        # -HeatFrom $null / -LegSpec $null (or a null entry) bind fine, and Test-Path -LiteralPath $null THROWS; the normalizing line must drop nulls first
        runner = (DV / "Invoke-VenueLeaseBatch.ps1").read_text(encoding="utf-8")
        lines = [l for l in runner.splitlines() if re.match(r"^\$(HeatFrom|LegSpec) = @\(", l)]
        self.assertEqual(len(lines), 4, lines)   # a null filter and the pinned normalizing line, for each of the two
        script = "$ErrorActionPreference='Stop'; $HeatFrom = $null; $LegSpec = @($null, 'a.json,b.json'); " + "; ".join(lines) + "; \"$($HeatFrom.Count)|$($LegSpec -join ';')\""
        r = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "0|a.json;b.json"), r.stdout + r.stderr)

    def test_gate_step_paths_wait_cool_run_and_cap_skip(self) -> None:
        # a stub clock and a stub heat sequence through the pure step: wait (hot, under the cap), cool (clear -> run, waitSec kept), cap skip (hot at 180 s)
        got = ps_json(
            "$t0 = [DateTime]::new(2026,10,28,0,0,0,[DateTimeKind]::Utc); $f = $null; $out = @(); "
            "foreach ($s in @(@('hot',0), @('hot',60), @('clear',95))) { $g = Get-VenueThermalGateStep -Heat $s[0] -FirstHotUtc $f -NowUtc $t0.AddSeconds($s[1]) -CapSec 180; $f = $g.firstHotUtc; $out += \"$($g.step):$($g.waitSec)\" }; "
            "$f = $null; foreach ($s in @(@('hot',0), @('hot',179), @('hot',180))) { $g = Get-VenueThermalGateStep -Heat $s[0] -FirstHotUtc $f -NowUtc $t0.AddSeconds($s[1]) -CapSec 180; $f = $g.firstHotUtc; $out += \"$($g.step):$($g.waitSec)\" }; "
            "$out += (Get-VenueThermalGateStep -Heat 'unknown' -FirstHotUtc $null -NowUtc $t0 -CapSec 180).step; $out")
        self.assertEqual(got, ["WAIT:0", "WAIT:60", "RUN:95", "WAIT:0", "WAIT:179", "SKIP:180", "RUN"])

    def test_heavy_keys_are_measured_from_the_r1_receipts(self) -> None:
        rows = json.dumps([{k: r[k] for k in ("batch", "key", "status", "heat")} for r in r1_rows()])
        got = ps_json(f"$h = Get-VenueHeavyLegKeys -Rows @(ConvertFrom-Json -InputObject '{rows}'); [pscustomobject]$h")
        self.assertEqual(got, {"m16-1243-display-fullscreen-s1|cpu": 3}, "only cell 15 preceded a hot leg in r1 (B02 15C->15H, 15H->17C; B04 15H->15C)")

    def test_r1_replay_spread_puts_no_two_cell_15_legs_adjacent(self) -> None:
        heavy_key = "m16-1243-display-fullscreen-s1|cpu"
        rows = r1_rows()
        before_total = after_total = 0
        for nn in R1:
            legs = [r for r in rows if r["batch"] == nn]
            items = json.dumps([f"{r['cell']}{r['arm']}" for r in legs])
            flags = ",".join("$true" if r["key"] == heavy_key else "$false" for r in legs)
            got = ps_json(f"$s = Get-VenueThermalSpreadOrder -Items @(ConvertFrom-Json -InputObject '{items}') -Heavy ([bool[]]@({flags})); "
                          "[pscustomobject]@{ items = @($s.items); before = $s.heavyAdjacentBefore; after = $s.heavyAdjacent }")
            order = got["items"]
            self.assertEqual(sorted(order), sorted(json.loads(items)), f"B{nn}: every leg once, none dropped")
            self.assertEqual(got["after"], 0, f"B{nn}: {order}")
            adj15 = sum(1 for a, b in zip(order, order[1:]) if a.startswith("15") and b.startswith("15"))
            self.assertEqual(adj15, 0, f"B{nn}: {order}")
            before_total += got["before"]; after_total += got["after"]
            if nn == "04":
                self.assertEqual(order, ["OWNH", "OWNC", "10H", "10C", "11H", "15H", "11C", "15C"])
            if nn == "02":
                self.assertEqual(order, ["15C", "17C", "15H", "17H", "19C", "19H", "20C", "20H"])
        self.assertEqual((before_total, after_total), (4, 0), "r1 had 4 adjacent cell-15 pairs (B02, B03, B04, B05); the spread leaves none")

    def test_spread_keeps_the_given_order_when_nothing_is_heavy_and_counts_what_cannot_be_spread(self) -> None:
        got = ps_json("$s = Get-VenueThermalSpreadOrder -Items @('a','b','c') -Heavy ([bool[]]@($false,$false,$false)); @($s.items)")
        self.assertEqual(got, ["a", "b", "c"])
        got = ps_json("$s = Get-VenueThermalSpreadOrder -Items @('H1','H2','H3','L1') -Heavy ([bool[]]@($true,$true,$true,$false)); [pscustomobject]@{ i = @($s.items); n = $s.heavyAdjacent }")
        self.assertEqual((got["i"], got["n"]), (["H1", "L1", "H2", "H3"], 1))


@requires_windows_pwsh
class ThermalGateRunnerTests(unittest.TestCase):
    """The runner with the stub thermal source: wait, cool, run; cap skip with re-queue; re-queues exhausted; unknown heat; ThermalSpread from receipts."""

    setUp = tvlb.VenueLeaseBatchRunnerTests.setUp
    _init_case = tvlb.VenueLeaseBatchRunnerTests._init_case   # the shared setUp builds each case through it (#704)
    batch_cmd = tvlb.VenueLeaseBatchRunnerTests.batch_cmd
    kinds = tvlb.VenueLeaseBatchRunnerTests.kinds
    batches = tvlb.VenueLeaseBatchRunnerTests.batches
    events_raw = tvlb.VenueLeaseBatchRunnerTests.events_raw
    _mirror_with_bad_sidecar = tvlb.VenueLeaseBatchRunnerTests._mirror_with_bad_sidecar

    @classmethod
    def setUpClass(cls) -> None:
        tvlb.VenueLeaseBatchRunnerTests.setUpClass.__func__(cls)
        (cls.tmp / "fake-quiet.ps1").write_text(FAKE_QUIET_HEAT, encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        tvlb.VenueLeaseBatchRunnerTests.tearDownClass.__func__(cls)

    def run_batch(self, specs, *extra: str, **env: str) -> subprocess.CompletedProcess:
        args, e = self.batch_cmd(specs, *(() if "-ThermalPollSec" in extra else ("-ThermalPollSec", "1")), *extra, **env)
        return subprocess.run(args, capture_output=True, text=True, timeout=600, env=e)

    def log(self) -> str:
        return (self.run_dir / "batch.log").read_text(encoding="utf-8")

    def test_wait_then_cool_then_run_records_wait_sec(self) -> None:
        r = self.run_batch([self.specs[0]], "-ThermalWaitCapSec", "120", FAKE_HEAT="9,9,0")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        k = self.kinds()
        self.assertEqual([x.split(":")[0] for x in k], ["OPEN", "QUIET", "QUIET", "QUIET", "LEG", "CLOSE"], k)
        [b] = self.batches()
        [row] = b["legs"]
        self.assertEqual((row["status"], row["counted"], row["heatAtProbe"], row["heatAtLaunch"], row["thermalSkips"]), ("RAN", True, "hot", "clear", 0), row)
        self.assertGreaterEqual(row["thermalWaitSec"], 2, row)
        self.assertEqual(row["thermalWaitOvershootSec"], 0, "a wait that ends inside the cap records no overshoot")
        self.assertEqual((b["thermalWaits"], b["thermalSkips"]), (2, 0))
        self.assertEqual(self.log().count(" THERMAL_WAIT leg=01-fx-a-spec "), 2)
        self.assertIn("heat=hot (throttled 9/60, mean 70.0 C)", self.log())

    def test_cap_skip_requeues_the_hot_leg_and_runs_the_next_one_first(self) -> None:
        r = self.run_batch(self.specs[:2], "-ThermalWaitCapSec", "2", FAKE_HEAT="HOT_UNTIL_SKIP")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        legs = [x.split(":")[1] for x in self.kinds() if x.startswith("LEG:")]
        self.assertEqual(legs, ["fx-b", "fx-a"], "fx-a skipped hot and re-queued: fx-b ran first, fx-a after, and neither ran hot")
        [b] = self.batches()
        skip = [l for l in b["legs"] if l["status"] == "THERMAL_HOT_SKIPPED"]
        self.assertEqual(len(skip), 1, b["legs"])
        self.assertEqual((skip[0]["requeued"], skip[0]["outcome"], skip[0]["counted"], skip[0]["heatAtProbe"]), (True, "NOT_RUN", False, "hot"))
        # adversary r1 A9: the skip never waits past the cap (a re-probe that could not end inside it is not started)
        self.assertLessEqual(skip[0]["thermalWaitSec"], 2 + 8, skip[0])
        ran = [l for l in b["legs"] if l["status"] == "RAN"]
        self.assertEqual([l["heatAtLaunch"] for l in ran], ["clear", "clear"])
        # adversary r1 A6: the re-queued fx-a's first reading is taken again after its new predecessor (fx-b), not kept from before the skip
        self.assertEqual(b["thermalSkips"], 1)
        res = tvlb.batch_result(r.stdout)
        self.assertEqual([(l["name"], l["heatAtProbe"]) for l in res["legs"]], [("02-fx-b-spec", "clear"), ("01-fx-a-spec", "clear")], res["legs"])
        self.assertEqual((res["ran"], res["thermalSkipped"], res["notRun"]), (2, 0, 0))
        self.assertIn("LEG 01-fx-a-spec THERMAL_HOT_SKIPPED batch=1 heat=hot", self.log())

    def test_requeues_exhausted_the_leg_stays_skipped_never_run(self) -> None:
        r = self.run_batch([self.specs[0]], "-ThermalWaitCapSec", "1", "-ThermalRequeueMax", "0", FAKE_HEAT="9")
        self.assertEqual(r.returncode, 7, r.stdout + r.stderr)
        self.assertEqual([x for x in self.kinds() if x.startswith("LEG:")], [], "a leg past its cap is never launched")
        res = tvlb.batch_result(r.stdout)
        self.assertEqual((res["ran"], res["thermalSkipped"]), (0, 1))
        self.assertEqual(res["legs"][0]["status"], "THERMAL_HOT_SKIPPED")
        self.assertFalse(res["legs"][0]["requeued"])

    def test_adversary_r1b_a2_a_long_poll_shortens_the_sleep_instead_of_skipping(self) -> None:
        # a poll longer than the cap used to turn the first hot reading into an immediate permanent skip (exit 7); the sleep is shortened to fit instead
        r = self.run_batch([self.specs[0]], "-ThermalWaitCapSec", "3", "-ThermalPollSec", "600", "-ThermalRequeueMax", "0", FAKE_HEAT="9,0")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["legs"][0]["status"], b["legs"][0]["heatAtLaunch"], b["thermalWaits"], b["thermalSkips"]), ("RAN", "clear", 1, 0), b["legs"])

    # BACHELOR-THERMAL-GATE-WAIT-1 r2 (Dot P2 on a523d461): the cap is a SOFT budget, cap + one probe. The runner bounds when a re-probe STARTS (from the
    # previous probe's duration) and the probe's wait for quiet, but not the probe's own sample window, so a later probe slower than the earlier ones ends past
    # the cap. The receipt names the overshoot (thermalWaitOvershootSec = max(0, thermalWaitSec - cap)); a leg still hot then is skipped, never run hot.
    # Cap 11 s, poll 2 s, probes of about 1 s (plus the stub's start-up, which grows on a loaded machine: the cap leaves room for a third probe even then) then one of 7 s: the third probe was predicted to end inside the cap and ends past it.
    SLOW_LATER = dict(FAKE_PROBE_SLEEP="1,1,7")

    def test_dot_p2_a_slower_later_probe_ends_past_the_cap_and_the_skip_names_the_overshoot(self) -> None:
        r = self.run_batch([self.specs[0]], "-ThermalWaitCapSec", "11", "-ThermalPollSec", "2", "-ThermalRequeueMax", "0", FAKE_HEAT="9", **self.SLOW_LATER)
        self.assertEqual(r.returncode, 7, r.stdout + r.stderr)
        self.assertEqual([x for x in self.kinds() if x.startswith("LEG:")], [], "a leg still hot after the slow probe is never launched")
        [b] = self.batches()
        [row] = b["legs"]
        self.assertEqual(row["status"], "THERMAL_HOT_SKIPPED", row)
        self.assertGreater(row["thermalWaitSec"], 11, "the slow probe really ended past the cap (else this test proves nothing)")
        self.assertEqual(row["thermalWaitOvershootSec"], row["thermalWaitSec"] - 11, row)
        # soft budget = cap + ONE probe: the overshoot is no more than the slow probe (7 s + stub start-up), never a second one
        self.assertLessEqual(row["thermalWaitOvershootSec"], 7 + 7, row)
        self.assertIn(f"overshootSec={row['thermalWaitOvershootSec']}", self.log())
        self.assertIn("softBudget=cap+oneProbe", self.log())

    def test_dot_p2_a_slow_probe_that_reads_clear_runs_the_leg_and_records_the_overshoot(self) -> None:
        r = self.run_batch([self.specs[0]], "-ThermalWaitCapSec", "11", "-ThermalPollSec", "2", FAKE_HEAT="9,9,0", **self.SLOW_LATER)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        [row] = b["legs"]
        self.assertEqual((row["status"], row["heatAtLaunch"]), ("RAN", "clear"), row)
        self.assertGreater(row["thermalWaitSec"], 11, row)
        self.assertEqual(row["thermalWaitOvershootSec"], row["thermalWaitSec"] - 11, row)

    def test_adversary_r2b_a1_a_hot_cooldown_probe_during_a_thermal_wait_is_held_to_the_cap(self) -> None:
        # first probe QUIET and hot (the thermal wait starts), every later probe COOLDOWN_UNMET and hot: these used to retry in the lease with no limit, past the cap,
        # with no skip and no receipt; now the cap governs them and the leg is skipped hot, never run. A short deadline bounds the pre-fix run.
        from datetime import datetime, timedelta, timezone
        deadline = tvlb.utc_iso(datetime.now(timezone.utc) + timedelta(seconds=60))
        r = self.run_batch([self.specs[0]], "-ThermalWaitCapSec", "4", "-ThermalPollSec", "1", "-ThermalRequeueMax", "0", "-DeadlineUtc", deadline,
                           FAKE_HEAT="9", FAKE_QUIET_MODES="QUIET,COOLDOWN", FAKE_PROBE_SLEEP="1")
        self.assertEqual(r.returncode, 7, r.stdout + r.stderr)
        [b] = self.batches()
        [row] = b["legs"]
        self.assertEqual(row["status"], "THERMAL_HOT_SKIPPED", row)
        self.assertGreater(row["thermalWaitSec"], 0, row)
        self.assertLess(len([x for x in self.kinds() if x.startswith("QUIET")]), 10, "the probes stop at the cap")

    def test_adversary_r2b2_a1_busy_probes_that_read_clear_cannot_run_the_wait_past_the_cap(self) -> None:
        # first probe QUIET and hot, then COOLDOWN_UNMET probes that read CLEAR: these used to retry in the lease with no limit and no cap accounting (a probe
        # started at every 31 s past the cap, the final skip at 151 s under cap 90); once the cap has run out the next busy probe ends the wait: skipped, never run
        from datetime import datetime, timedelta, timezone
        deadline = tvlb.utc_iso(datetime.now(timezone.utc) + timedelta(seconds=60))
        r = self.run_batch([self.specs[0]], "-ThermalWaitCapSec", "4", "-ThermalPollSec", "1", "-ThermalRequeueMax", "0", "-DeadlineUtc", deadline,
                           FAKE_HEAT="9,0", FAKE_QUIET_MODES="QUIET,COOLDOWN", FAKE_PROBE_SLEEP="1")
        self.assertEqual(r.returncode, 7, r.stdout + r.stderr)
        [b] = self.batches()
        [row] = b["legs"]
        self.assertEqual(row["status"], "THERMAL_HOT_SKIPPED", row)
        self.assertLess(len([x for x in self.kinds() if x.startswith("QUIET")]), 10, "the probes stop once the cap has run out")

    def test_adversary_r2b2_a3_the_first_known_heat_reading_counts_even_from_a_busy_probe(self) -> None:
        # the leg's first probe is COOLDOWN_UNMET and reads hot, the next QUIET and clear: heatAtProbe is the FIRST reading (hot), which the spread measures
        # heavy keys from; it used to be overwritten by the later clear one because the state was only made after a QUIET probe
        r = self.run_batch([self.specs[0]], FAKE_HEAT="9,0", FAKE_QUIET_MODES="COOLDOWN,QUIET")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        [row] = b["legs"]
        self.assertEqual((row["status"], row["heatAtProbe"], row["heatAtLaunch"]), ("RAN", "hot", "clear"), row)

    def test_adversary_r2b2_a2_the_probe_duration_is_timed_on_the_monotonic_clock(self) -> None:
        runner = (DV / "Invoke-VenueLeaseBatch.ps1").read_text(encoding="utf-8")
        self.assertNotIn("[DateTime]::UtcNow - $tProbe", runner, "a wall-clock step made probeSec negative and the next sleep too long")
        self.assertIn("$probeSec = [int][Math]::Ceiling([Math]::Max(0.0, ([Diagnostics.Stopwatch]::GetTimestamp() - $tProbeTs)", runner)

    def test_adversary_r3_a1_a_busy_probe_with_unknown_heat_cannot_sleep_past_the_cap(self) -> None:
        # first probe QUIET and hot (the wait starts), every later probe GATE_BUSY with no heat reading, -ClaimPollSec 25: the busy branch slept the whole 25 s and then
        # started another probe far past the cap 4 (waitSec ~27 before the skip); now the sleep is cut to what fits and no probe starts after the deadline less one probe
        r = self.run_batch([self.specs[0]], "-ThermalWaitCapSec", "4", "-ClaimPollSec", "25", "-ThermalRequeueMax", "0", FAKE_HEAT="9,NA", FAKE_QUIET_MODES="QUIET,BUSY")
        self.assertEqual(r.returncode, 7, r.stdout + r.stderr)
        [b] = self.batches()
        [row] = b["legs"]
        self.assertEqual(row["status"], "THERMAL_HOT_SKIPPED", row)
        self.assertLess(row["thermalWaitSec"], 4 + 10, f"the busy sleep must be bounded by the cap (cap + one probe), not ClaimPollSec: {row}")
        self.assertLess(len([x for x in self.kinds() if x.startswith("QUIET")]), 10)

    def test_dot_b2_a_first_probe_longer_than_the_cap_still_gets_a_cooling_attempt(self) -> None:
        # the INITIAL probe takes 5 s under cap 3 and reads hot; it used to be charged against the first cooling sleep (sleepFit < 1), so the leg was skipped at once with
        # zero thermal waiting (and, re-queued, again). Now the first hot reading always gets a cooling sleep and a re-probe: that re-probe (1 s) reads clear and the leg runs.
        r = self.run_batch([self.specs[0]], "-ThermalWaitCapSec", "3", "-ThermalRequeueMax", "0", FAKE_HEAT="9,0", FAKE_PROBE_SLEEP="5,1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        [row] = b["legs"]
        self.assertEqual((row["status"], row["heatAtProbe"], row["heatAtLaunch"], b["thermalWaits"], b["thermalSkips"]), ("RAN", "hot", "clear", 1, 0), row)
        self.assertGreaterEqual(row["thermalWaitSec"], 1, row)

    def test_r6p2_a2_every_launched_row_keeps_the_heat_read_before_its_wait(self) -> None:
        # adversary r6 A2 on dd425d1d: a leg that waited hot, cooled, launched and then failed (LEG_FAILED), or was re-tried (RETRY), recorded no heatAtProbe, so the history fell back to
        # the clear heat at grant and its predecessor's key was never measured heavy. Every launched row carries the first reading (the one the spread measures heavy keys from).
        for env, status in ((dict(FAKE_LEG_MODES="exit3"), "LEG_FAILED"), (dict(FAKE_LEG_OUTCOMES="VENUE_NOT_QUIESCENT,PASS"), "RETRY")):
            with self.subTest(status=status):
                tvlb.VenueLeaseBatchRunnerTests.setUp(self)
                self.run_batch([self.specs[0]], "-ThermalWaitCapSec", "120", FAKE_HEAT="9,0", **env)
                [b] = self.batches()
                row = [l for l in b["legs"] if l["status"] == status]
                self.assertEqual(len(row), 1, b["legs"])
                self.assertEqual(row[0].get("heatAtProbe"), "hot", row[0])

    def test_r9_sol_b1_an_unknown_first_reading_never_masks_a_later_hot_one_on_a_same_lease_retry(self) -> None:
        # sol r8 B1 on fc0412d8 (its repro): one lease, fx-a runs (clear); fx-b's first probe reads unknown, it launches and returns VENUE_NOT_QUIESCENT (it never played), its
        # retry's probe reads hot, it waits, then runs clear. firstHeat was fixed at that unknown, so fx-b's RAN row said heatAtProbe=unknown, the history fell back to the clear
        # grant and fx-a was never measured heavy. The first KNOWN heat is kept across the retry; the RETRY row keeps the reading it was written with.
        r = self.run_batch(self.specs[:2], "-ThermalWaitCapSec", "120", FAKE_HEAT="0,NA,9,0", FAKE_LEG_OUTCOMES="PASS,VENUE_NOT_QUIESCENT,PASS")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        got = [(l["status"], l.get("heatAtProbe")) for l in b["legs"]]
        self.assertEqual(got, [("RAN", "clear"), ("RETRY", "unknown"), ("RAN", "hot")], b["legs"])
        self.assertEqual(b["thermalWaits"], 1, b)
        # the spread reads fx-a as heavy from these rows (the scalar rows carry no leg name, so they are keyed here by their place in the run)
        rows = ", ".join(f"[pscustomobject]@{{ batch = '1'; key = '{k}|cpu'; status = '{l['status']}'; heat = '{l['heatAtProbe']}' }}" for k, l in zip("abb", b["legs"]))
        self.assertEqual(ps_json(f"[pscustomobject](Get-VenueHeavyLegKeys -Rows @({rows}))"), {"a|cpu": 1})

    def test_unknown_heat_is_not_gated(self) -> None:
        r = self.run_batch([self.specs[0]], FAKE_HEAT="NA")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["legs"][0]["status"], b["legs"][0]["heatAtProbe"], b["legs"][0]["thermalWaitSec"], b["thermalWaits"]), ("RAN", "unknown", 0, 0))

    def test_cap_zero_turns_the_gate_off(self) -> None:
        r = self.run_batch([self.specs[0]], "-ThermalWaitCapSec", "0", FAKE_HEAT="9")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["legs"][0]["status"], b["legs"][0]["heatAtLaunch"], b["thermalWaits"]), ("RAN", "hot", 0))


@requires_windows_pwsh
class ThermalDeadlineAdmissionTests(unittest.TestCase):
    """BACHELOR-THERMAL-GATE-WAIT-1 r4: the thermal deadline is checked right before every re-probe dispatch, so runner delay (a stalled log append, a late wakeup, a stalled
    probe-script check) never buys another probe. Its own class, outside the heavy set, so the regular shards carry its (real, scaled-down) waits."""

    setUp = tvlb.VenueLeaseBatchRunnerTests.setUp
    _init_case = tvlb.VenueLeaseBatchRunnerTests._init_case   # the shared setUp builds each case through it (#704)
    batch_cmd = tvlb.VenueLeaseBatchRunnerTests.batch_cmd
    kinds = tvlb.VenueLeaseBatchRunnerTests.kinds
    batches = tvlb.VenueLeaseBatchRunnerTests.batches
    events_raw = tvlb.VenueLeaseBatchRunnerTests.events_raw
    _mirror_with_bad_sidecar = tvlb.VenueLeaseBatchRunnerTests._mirror_with_bad_sidecar

    @classmethod
    def setUpClass(cls) -> None:
        tvlb.VenueLeaseBatchRunnerTests.setUpClass.__func__(cls)
        (cls.tmp / "fake-quiet.ps1").write_text(FAKE_QUIET_HEAT, encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        tvlb.VenueLeaseBatchRunnerTests.tearDownClass.__func__(cls)

    def run_batch(self, specs, *extra: str, **env: str) -> subprocess.CompletedProcess:
        args, e = self.batch_cmd(specs, *(() if "-ThermalPollSec" in extra else ("-ThermalPollSec", "1")), *extra, **env)
        return subprocess.run(args, capture_output=True, text=True, timeout=600, env=e)

    def log(self) -> str:
        return (self.run_dir / "batch.log").read_text(encoding="utf-8")

    # BACHELOR-THERMAL-GATE-WAIT-1 r4 (adversary r3 pass 2 A1, Dot c5946322 on 528221bd): the probe budget was decided BEFORE the THERMAL_WAIT / RETRY_IN_LEASE log line and the
    # sleep, and the next iteration never rejected an expired thermal deadline, so a log append that stalled (a UNC RunDir) or a wakeup that came late started another probe past
    # the deadline. Dot's numbers (elapsed 100, cap 180, sleep 30, a 200 s stall) are scaled here (first hot reading at 0 s, cap 2, a 3 s stall): the runner copy under
    # test has the stall injected as a Start-Sleep at the exact site, so the delay is in the runner and the test stays cheap.
    LOG_THERMAL_WAIT = 'L "THERMAL_WAIT leg=$($leg.name)'
    LOG_RETRY = 'L "RETRY_IN_LEASE leg=$($leg.name) batch=$batchNo reason=$($q.verdict)'

    def _expired_wait_run(self, patch: tuple[str, str], cap: str = "2", **env: str) -> tuple[subprocess.CompletedProcess, dict]:
        self._mirror_with_bad_sidecar(runner_patches=(patch,))
        r = self.run_batch([self.specs[0]], "-ThermalWaitCapSec", cap, "-ThermalPollSec", "1", "-ClaimPollSec", "1", "-ThermalRequeueMax", "0", **env)
        [b] = self.batches()
        return r, b

    def _assert_typed_skip_without_another_probe(self, r: subprocess.CompletedProcess, b: dict, quiet_before: int = 1, marker: bool = True, cap: str = "2") -> None:
        self.assertEqual(r.returncode, 7, r.stdout + r.stderr)
        self.assertEqual([x for x in self.kinds() if x.startswith("LEG:")], [], "a leg whose wait expired is never launched")
        [row] = b["legs"]
        self.assertEqual((row["status"], row["outcome"], row["counted"]), ("THERMAL_HOT_SKIPPED", "NOT_RUN", False), row)
        self.assertGreater(row["thermalWaitSec"], int(cap), "the injected stall really carried the wait past the cap (else this test proves nothing)")
        self.assertEqual(len([x for x in self.kinds() if x.startswith("QUIET")]), quiet_before, f"runner delay must never buy another probe: {self.kinds()}")
        if marker:
            self.assertIn("expiredBeforeProbe=1", self.log())

    def test_adversary_r4_a2_a_stalled_quiet_log_after_the_first_hot_reading_counts_against_the_cap(self) -> None:
        # the first hot reading is when its probe RETURNED, not when the runner got around to stamping it after the QUIET log line: a 6 s stall there (cap 4) used to be
        # left out of the wait (waitSec 0), so another sleep and another probe were allowed; now the cap is already spent and the leg is skipped with that one probe
        r, b = self._expired_wait_run(('L "QUIET leg=$($leg.name) batch=$batchNo phase=in-lease', 'Start-Sleep -Seconds 3; L "QUIET leg=$($leg.name) batch=$batchNo phase=in-lease'), FAKE_HEAT="9")
        self._assert_typed_skip_without_another_probe(r, b, marker=False)

    def test_adversary_r4p2_a2_a_stall_inside_the_probe_function_before_its_dispatch_starts_no_further_probe(self) -> None:
        # the admission at the loop top passes (elapsed 1 of 4), then the probe function's own Test-Path of the probe script stalls 6 s (a share): the deadline is checked
        # again as the last statement before the process starts
        r, b = self._expired_wait_run(("if (-not (Test-Path -LiteralPath $QuietScript -PathType Leaf)) {", "Start-Sleep -Seconds 3; if (-not (Test-Path -LiteralPath $QuietScript -PathType Leaf)) {"), FAKE_HEAT="9")
        self._assert_typed_skip_without_another_probe(r, b)

    def test_adversary_r4p2_a1_the_sleep_is_planned_on_the_floored_elapsed_time_and_only_the_admission_reads_the_ceiling(self) -> None:
        # a cap of 1 s: a first hot reading 0.1 s old is 1 s by the ceiling, which left no sleep at all (an immediate re-probe with no cooling); the sleep sites plan on the floor
        runner = (DV / "Invoke-VenueLeaseBatch.ps1").read_text(encoding="utf-8")
        self.assertNotIn("(Get-ThermalElapsedSec $th -Ceiling)", runner)
        # the busy-retry sleep is re-planned after its log write, and slept only if something is left (its late wakeup is then caught by the admission, as the poll sleep's is)
        self.assertIn("(Get-ThermalElapsedSec $th) -SlowestReprobeSec $th.slowProbeSec -WantSleepSec $busySleep).sleepSec }\n                        if ($busySleep -gt 0) { Start-Sleep -Seconds $busySleep }", runner.replace("\r\n", "\n"))
        self.assertEqual(runner.count("Get-ThermalElapsedSec $thw -Ceiling"), 1, "the loop-top admission reads the ceiling")
        self.assertEqual(runner.count("Get-ThermalElapsedSec $ThermalState -Ceiling"), 1, "so does the check inside the probe function")

    def test_adversary_r4_a1_a5_a_leg_retry_after_a_finished_wait_starts_from_no_wait_and_the_receipt_keeps_the_wait(self) -> None:
        # hot twice (a wait of 1-2 s), then clear: the leg launches, which ends the thermal wait, and returns VENUE_NOT_QUIESCENT; the retry log stalls past the old deadline.
        # A1: the retry's probe used to inherit the finished wait (firstHotUtc kept): dispatched with no admission and a quiet wait clamped to 0 (maxWaitSec=0).
        # A5: the retry's fresh gate used to overwrite the leg's thermalWaitSec with 0, so the final receipt lost the wait it had come after.
        self._mirror_with_bad_sidecar(runner_patches=(('L "RETRY_IN_LEASE leg=$($leg.name) batch=$batchNo reason=LEG_NOT_QUIESCENT', 'Start-Sleep -Seconds 6; L "RETRY_IN_LEASE leg=$($leg.name) batch=$batchNo reason=LEG_NOT_QUIESCENT'),))
        r = self.run_batch([self.specs[0]], "-ThermalWaitCapSec", "4", "-ThermalPollSec", "1", "-ThermalRequeueMax", "0", FAKE_HEAT="9,9,0", FAKE_LEG_OUTCOMES="VENUE_NOT_QUIESCENT,PASS")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        quiet = [l for l in self.log().splitlines() if " QUIET leg=" in l]
        self.assertEqual(len(quiet), 4, quiet)
        self.assertNotIn("maxWaitSec=0 ", quiet[3], "the retry probe still belongs to the finished thermal wait: " + quiet[3])
        self.assertEqual([x.split(":")[0] for x in self.kinds() if x.startswith("LEG")], ["LEG", "LEG"], self.kinds())
        [b] = self.batches()
        retry = [l for l in b["legs"] if l["status"] == "RETRY"]
        ran = [l for l in b["legs"] if l["status"] == "RAN"]
        self.assertEqual((len(retry), len(ran)), (1, 1), b["legs"])
        self.assertGreaterEqual(retry[0]["thermalWaitSec"], 1, retry[0])
        self.assertGreaterEqual(ran[0]["thermalWaitSec"], retry[0]["thermalWaitSec"], "the final row keeps the wait the retry came after: " + str(ran[0]))
        self.assertEqual(ran[0]["thermalWaitOvershootSec"], 0, ran[0])

    def test_adversary_r4_a3_the_dispatch_check_reads_the_ceiling_of_the_elapsed_time_so_the_grace_is_at_most_one_second(self) -> None:
        got = ps_json(
            "$f = [Diagnostics.Stopwatch]::Frequency; "
            "$d = { param($secs) $e = Get-VenueThermalElapsedSec -FirstHotTimestamp 0 -NowTimestamp ([long]($secs * $f)) -Ceiling; "
            "\"$e|$((Get-VenueThermalProbeBudget -CapSec 180 -ElapsedSec $e -SlowestReprobeSec 0 -WantSleepSec 30).mayDispatchNow)\" }; "
            # 181.5 s actual: the floor (181) plus a grace of 1 admitted it, almost 2 s after the deadline; the ceiling (182) does not
            "$out = @((& $d 181.5), (& $d 181.0), (& $d 180.2), (& $d 181.01), (& $d 0)); "
            "$out + (Get-VenueThermalElapsedSec -FirstHotTimestamp 0 -NowTimestamp ([long](181.5 * $f))) + (Get-VenueThermalElapsedSec -FirstHotTimestamp 100 -NowTimestamp 0)")
        self.assertEqual(got, ["182|False", "181|True", "181|True", "182|False", "0|True", 181, 0], got)

    def test_adversary_r3p2_a1_dot_a_log_append_that_stalls_past_the_deadline_starts_no_further_probe(self) -> None:
        r, b = self._expired_wait_run((self.LOG_THERMAL_WAIT, "Start-Sleep -Seconds 3; " + self.LOG_THERMAL_WAIT), FAKE_HEAT="9")
        self._assert_typed_skip_without_another_probe(r, b)

    def test_adversary_r3p2_a1_dot_a_sleep_that_wakes_late_past_the_deadline_starts_no_further_probe(self) -> None:
        r, b = self._expired_wait_run(("Start-Sleep -Seconds $sleep", "Start-Sleep -Seconds ($sleep + 3)"), FAKE_HEAT="9")
        self._assert_typed_skip_without_another_probe(r, b)

    def test_adversary_r3p2_a1_dot_the_busy_retry_path_log_stall_starts_no_further_probe(self) -> None:
        # first probe QUIET and hot, every later probe GATE_BUSY: the RETRY_IN_LEASE log stalls past the deadline
        # (cap 4, stall 6: at cap 2 the wait ends at the budget check before the busy branch is reached)
        r, b = self._expired_wait_run((self.LOG_RETRY, "Start-Sleep -Seconds 6; " + self.LOG_RETRY), "4", FAKE_HEAT="9,NA", FAKE_QUIET_MODES="QUIET,BUSY")
        self._assert_typed_skip_without_another_probe(r, b, quiet_before=2, cap="4")

    def test_adversary_r3p2_a1_the_admission_check_is_part_of_the_one_budget_function_and_keeps_the_first_cooling_attempt(self) -> None:
        got = ps_json(
            "$d = { param($cap, $e, $slow) (Get-VenueThermalProbeBudget -CapSec $cap -ElapsedSec $e -SlowestReprobeSec $slow -WantSleepSec 30).mayDispatchNow }; "
            # Dot: elapsed 100 + sleep 30, then a 200 s stall: 330 s on a cap of 180 is expired
            "@((& $d 180 330 0), (& $d 180 181 0), (& $d 180 183 0), "
            # a sleep cut to fit starts the probe AT the deadline less the slowest re-probe (plus a second of timer granularity): that probe is admitted
            "(& $d 180 180 0), (& $d 3 3 0), (& $d 180 177 3), (& $d 180 178 3), "
            # past it, or with the slowest re-probe no longer fitting, it is not; a cap of 0 never starts one
            "(& $d 180 179 3), (& $d 180 150 40), (& $d 0 0 0))")
        self.assertEqual(got, [False, True, False, True, True, True, True, False, False, False], got)
        runner = (DV / "Invoke-VenueLeaseBatch.ps1").read_text(encoding="utf-8")
        self.assertIn("mayDispatchNow", runner, "the dispatch check is the budget function's, not a second copy of the rule")

    # r9 (sol r8 B2): every thermal-helper call of the runner, taken from its own AST and evaluated with the runner's own grace assignment
    RUNNER_CALLS = (
        "$ast = [System.Management.Automation.Language.Parser]::ParseFile('{runner}', [ref]$null, [ref]$null); "
        "$calls = @($ast.FindAll({{ param($n) $n -is [System.Management.Automation.Language.CommandAst] -and $n.GetCommandName() -ceq '{name}' }}, $true)); "
        "foreach ($a in @($ast.FindAll({{ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and $n.Left.Extent.Text -ceq '$ThermalDispatchGraceSec' }}, $true))) {{ . ([scriptblock]::Create($a.Extent.Text)) }}; ")

    def test_r9_sol_b2_an_ambient_default_never_widens_the_runners_dispatch_grace_or_freezes_its_thermal_clock(self) -> None:
        # sol r8 B2 on fc0412d8: $PSDefaultParameterValues = @{ 'Get-VenueThermalProbeBudget:DispatchGraceSec' = 60 } reached every budget call of the runner (none passed the
        # grace), so with elapsed 230 on cap 180 the admission said mayDispatchNow (a probe 50 s past the deadline). Each call is taken from the runner's AST and run with that
        # ambient default: none may admit it. The same class: an ambient -NowTimestamp froze the thermal clock (elapsed 0, the deadline never reached).
        runner = str(DV / "Invoke-VenueLeaseBatch.ps1").replace("'", "''")
        got = ps_json(
            self.RUNNER_CALLS.format(runner=runner, name="Get-VenueThermalProbeBudget")
            + "$PSDefaultParameterValues = @{ 'Get-VenueThermalProbeBudget:DispatchGraceSec' = 60 }; "
            "function Get-ThermalElapsedSec($Th, [switch]$Ceiling) { 230 }; "
            "$ThermalWaitCapSec = 180; $ThermalPollSec = 30; $el = 230; $thElapsed = 230; $busySleep = 30; $sleep = 30; "
            "$st = [pscustomobject]@{ slowProbeSec = 0 }; $ThermalState = $st; $thw = $st; $th = $st; $gs = [pscustomobject]@{ waitSec = 230 }; "
            "@($calls | ForEach-Object { [bool](. ([scriptblock]::Create($_.Extent.Text))).mayDispatchNow })")
        self.assertGreaterEqual(len(got), 6, got)
        self.assertEqual(got, [False] * len(got), "an ambient default widened the runner's one-second dispatch grace")
        # control: the helper itself still honours an explicit grace (the parameter is the helper's, the runner's value is its own)
        self.assertTrue(ps_json("(Get-VenueThermalProbeBudget -CapSec 180 -ElapsedSec 230 -SlowestReprobeSec 0 -WantSleepSec 30 -DispatchGraceSec 60).mayDispatchNow"))
        # the runner's thermal clock: its Get-ThermalElapsedSec and its Get-VenueThermalNowUtc call, with ambient -NowTimestamp defaults of 0, still read the 230 s that passed
        fn = ("$ast = [System.Management.Automation.Language.Parser]::ParseFile('" + runner + "', [ref]$null, [ref]$null); "
              ". ([scriptblock]::Create($ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq 'Get-ThermalElapsedSec' }, $true).Extent.Text)); ")
        got = ps_json(
            fn + self.RUNNER_CALLS.format(runner=runner, name="Get-VenueThermalNowUtc")
            + "$PSDefaultParameterValues = @{ 'Get-VenueThermalElapsedSec:NowTimestamp' = [long]0; 'Get-VenueThermalNowUtc:NowTimestamp' = [long]0 }; "
            "$gFirstTs = [Diagnostics.Stopwatch]::GetTimestamp() - [long](230 * [Diagnostics.Stopwatch]::Frequency); $gFirstUtc = [DateTime]::UtcNow.AddSeconds(-230); "
            "$th = [pscustomobject]@{ firstHotUtc = $gFirstUtc; firstHotTs = $gFirstTs }; "
            "@((Get-ThermalElapsedSec $th), (Get-ThermalElapsedSec $th -Ceiling)) + @($calls | ForEach-Object { [int][Math]::Floor(((. ([scriptblock]::Create($_.Extent.Text))) - $gFirstUtc).TotalSeconds) })")
        self.assertEqual(len(got), 3, got)
        self.assertTrue(all(230 <= v <= 240 for v in got), f"an ambient -NowTimestamp froze the runner's thermal clock: {got}")

    def test_r6_a1_a_reprobe_quiet_wait_is_cut_to_the_cap_left_at_its_dispatch(self) -> None:
        # adversary r5 A1 on 09f9f657 (cap 180, poll 30, -ProbeMaxWaitSec 600, a 100 s probe-script Test-Path): the re-probe's quiet-wait allowance was fixed at the loop top (150) and
        # dispatched after the stall although 50 s of the cap were left. Scaled: cap 10, poll 1, a 4 s stall before every probe. The fake probe records the -MaxWaitSec it was given:
        # the re-probe's is at most what was left of the cap at its dispatch (10 - 4 - the 1 s sleep), never what was left at the loop top (about 9).
        self._mirror_with_bad_sidecar(runner_patches=(("if (-not (Test-Path -LiteralPath $QuietScript -PathType Leaf)) {", "Start-Sleep -Seconds 4; if (-not (Test-Path -LiteralPath $QuietScript -PathType Leaf)) {"),))
        r = self.run_batch([self.specs[0]], "-ThermalWaitCapSec", "10", "-ThermalPollSec", "1", "-ProbeMaxWaitSec", "600", "-ThermalRequeueMax", "0", FAKE_HEAT="9,0")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        waits = [int(l.rsplit("maxWait=", 1)[1]) for l in self.events_raw() if " QUIET " in l]
        self.assertEqual(len(waits), 2, self.events_raw())
        self.assertGreater(waits[0], 10, "the first probe is no re-probe: the cap does not bound it")
        self.assertLessEqual(waits[1], 10 - 4, f"the re-probe's quiet wait outlives the cap: {waits}")
        [b] = self.batches()
        self.assertEqual((b["legs"][0]["status"], b["legs"][0]["heatAtLaunch"]), ("RAN", "clear"), b["legs"])
        self.assertIn(f"maxWaitSec={waits[1]} ", [l for l in self.log().splitlines() if " QUIET leg=" in l][1], "the QUIET line names the wait the probe was given")

    def test_r6_a2_a_stalled_probe_evidence_write_after_the_first_hot_reading_counts_against_the_cap(self) -> None:
        # adversary r5 A2 on 09f9f657 (a UNC RunDir whose captured-output Set-Content stalls 200 s, cap 180, -ThermalRequeueMax 0): the first hot reading was stamped after that write,
        # so the stall was left out of the wait (waitSec 0) and bought another sleep and probe. It is stamped when the child's result is observed, before any evidence write.
        r, b = self._expired_wait_run(("Set-Content -LiteralPath $out -Value $captured", "Start-Sleep -Seconds 3; Set-Content -LiteralPath $out -Value $captured"), FAKE_HEAT="9")
        self._assert_typed_skip_without_another_probe(r, b, marker=False)


@requires_windows_pwsh
class ThermalSpreadPlanTests(unittest.TestCase):
    stage =tvlb.VenueLeaseBatchLegPlanTests.stage
    artifact_path = tvlb.VenueLeaseBatchLegPlanTests.artifact_path   # stage() stages the three cached artifacts through it (#705)
    item = tvlb.VenueLeaseBatchLegPlanTests.item
    write_plan = tvlb.VenueLeaseBatchLegPlanTests.write_plan
    plan_cmd = tvlb.VenueLeaseBatchLegPlanTests.plan_cmd
    run_plan = tvlb.VenueLeaseBatchLegPlanTests.run_plan
    leg_calls = tvlb.VenueLeaseBatchLegPlanTests.leg_calls
    kinds = tvlb.VenueLeaseBatchRunnerTests.kinds
    batches = tvlb.VenueLeaseBatchRunnerTests.batches
    _init_case = tvlb.VenueLeaseBatchRunnerTests._init_case   # the shared setUp builds each case through it (#704)

    # the plan tests' own setUpClass / setUp, spelled out (theirs call super() on their class)
    @classmethod
    def setUpClass(cls) -> None:
        tvlb.VenueLeaseBatchRunnerTests.setUpClass.__func__(cls)
        (cls.tmp / "fake-leg-args.ps1").write_text(tvlb.FAKE_LEG_ARGS, encoding="utf-8")
        cls.commit_a, cls.commit_b = "1" * 40, "2" * 40

    def setUp(self) -> None:
        tvlb.VenueLeaseBatchRunnerTests.setUp(self)
        self.leg_args = self.case / "leg-args.jsonl"
        self.manifests = {c: self.stage(c, f"build {c[:1]}") for c in (self.commit_a, self.commit_b)}

    @classmethod
    def tearDownClass(cls) -> None:
        tvlb.VenueLeaseBatchRunnerTests.tearDownClass.__func__(cls)

    def test_thermal_spread_orders_heavy_legs_apart_from_earlier_receipts(self) -> None:
        # an earlier run's receipts: on one lease fx-a ran, then fx-b read hot at its first probe -> fx-a|cpu is heavy
        prior = self.case / "prior-run"
        prior.mkdir()
        rows = [{"name": "01-fx-a-cpu-A", "backend": "cpu", "arm": "A", "status": "RAN", "heatAtProbe": "clear"},
                {"name": "02-fx-b-cpu-A", "backend": "cpu", "arm": "A", "status": "RAN", "heatAtProbe": "hot"}]
        (prior / "batches.jsonl").write_text(json.dumps({"schema": "mlv-app/venue-lease-batch/v1", "batch": 1, "legs": rows}) + "\n", encoding="utf-8")
        a, b = self.commit_a, self.commit_b
        plan = self.write_plan([self.item(self.specs[0], "A", a), self.item(self.specs[0], "B", b), self.item(self.specs[1], "A", a), self.item(self.specs[1], "B", b)])
        # adversary r3 A3: a history dir on a drive that is not mounted is skipped and named; it used to throw out of Join-Path and refuse the plan (LEG_PLAN_INVALID, exit 2)
        gone = unmounted_drive_path()
        r = self.run_plan(plan, "-PlanOrder", "ThermalSpread", "-HeatFrom", f"{gone},{prior}")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([(c["leaf"], c["sourceCommit"]) for c in self.leg_calls()], [("fx-a", a), ("fx-b", a), ("fx-a", b), ("fx-b", b)])
        kept = json.loads((self.run_dir / "leg-plan.json").read_text(encoding="utf-8"))
        ts = kept["thermalSpread"]
        self.assertEqual((ts["heavyKeys"], ts["heavyAdjacentBefore"], ts["heavyAdjacent"], ts["heatRows"]), (["fx-a|cpu"], 1, 0, 2))
        self.assertEqual(ts["skipped"], [f"{gone} (no batches.jsonl)"], ts)
        self.assertEqual(tvlb.batch_result(r.stdout)["legPlan"]["order"], "ThermalSpread")

    def test_r13_adversary_a2_a_heavy_key_in_another_case_is_not_this_legs_key(self) -> None:
        # adversary r12 A2 on 08c9aef5 (Invoke-VenueLeaseBatch.ps1:1149): the history's heavy key 'FX-A|cpu' (a different leg whose id differs only by case) answered .Contains('fx-a|cpu'),
        # so fx-a was spread as heavy. Leg ids are case-sensitive: nothing here is heavy, and the plan keeps its order.
        prior = self.case / "prior-run"
        prior.mkdir()
        rows = [{"name": "01-FX-A-cpu-A", "backend": "cpu", "arm": "A", "status": "RAN", "heatAtProbe": "clear"},
                {"name": "02-fx-b-cpu-A", "backend": "cpu", "arm": "A", "status": "RAN", "heatAtProbe": "hot"}]
        (prior / "batches.jsonl").write_text(json.dumps({"schema": "mlv-app/venue-lease-batch/v1", "batch": 1, "legs": rows}) + "\n", encoding="utf-8")
        a, b = self.commit_a, self.commit_b
        plan = self.write_plan([self.item(self.specs[0], "A", a), self.item(self.specs[0], "B", b), self.item(self.specs[1], "A", a), self.item(self.specs[1], "B", b)])
        r = self.run_plan(plan, "-PlanOrder", "ThermalSpread", "-HeatFrom", str(prior))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        ts = json.loads((self.run_dir / "leg-plan.json").read_text(encoding="utf-8"))["thermalSpread"]
        self.assertEqual((ts["heavyKeys"], ts["heavyAdjacentBefore"], ts["heavyAdjacent"]), (["FX-A|cpu"], 0, 0), ts)
        self.assertEqual([(c["leaf"], c["sourceCommit"]) for c in self.leg_calls()], [("fx-a", a), ("fx-a", b), ("fx-b", a), ("fx-b", b)])

    def test_heat_from_needs_thermal_spread(self) -> None:
        a = self.commit_a
        r = self.run_plan(self.write_plan([self.item(self.specs[0], "A", a)]), "-HeatFrom", str(self.case))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("-HeatFrom is read only by -PlanOrder ThermalSpread", r.stderr)


def agent_fn_ps(name: str, params: str, call: str) -> str:
    """Run the agent's own function `name` (extracted by AST, never restated) with the agent's script-scope params set to `params` (PowerShell assignments)."""
    return (f"$t = $null; $e = $null; $ast = [System.Management.Automation.Language.Parser]::ParseFile('{AGENT}', [ref]$t, [ref]$e); "
            f"$fn = $ast.Find({{ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq '{name}' }}, $true); "
            f"if ($null -eq $fn) {{ throw 'the agent does not define {name}' }}; . ([scriptblock]::Create($fn.Extent.Text)); {params}; {call}")


def quiet_fn_ps(name: str, setup: str, call: str) -> str:
    """Run Wait-VenueQuiet.ps1's own function `name`, taken from its text (the probe job is a here-string, so it is no AST function of the script, and the script itself
    samples for 24 s and cannot be dot-sourced)."""
    lines = QUIET.read_text(encoding="utf-8").splitlines()
    i = next(k for k, l in enumerate(lines) if l.startswith(f"function {name}"))
    out, depth = [], 0
    for l in lines[i:]:
        out.append(l)
        depth += l.count("{") - l.count("}")
        if depth == 0:
            break
    return "\n".join(out) + f"\n{setup}; {call}"


def tele_line(throttled: int, mean: float, rule: str = "") -> str:
    return (f"  tele heat source=C:\\temp\\hwinfo-log-adapter.csv age=1.7s hottest=50.0C max15=64.0C mean={mean:.1f}C throttled={throttled}/60 W=12.3{rule}")


def agent_rule(thr, mean, rows=60, stale=120) -> str:
    return f" rule_source=agent rule_max_throttled={thr} rule_mean_max_c={mean} rule_rows={rows} rule_stale_s={stale}"


@requires_windows_pwsh
class AgentThresholdParityTests(unittest.TestCase):
    """BACHELOR-THERMAL-GATE-AGENT-THRESHOLD-PARITY-1 (adversary r5 A4 / r11 A3): the gate decides on the heat rule the RUNNING agent publishes, not on a copy of its defaults.
    The agent publishes its effective rule (admission.heatRule and the job env MLV_AGENT_HEAT_RULE); the quiet probe copies it into heat{}.rule and the 'tele heat' line;
    the gate reads it from there. Absent: the defaults, thresholds_source=default. Present but unusable: 'unknown' with THERMAL_THRESHOLDS_INVALID."""

    def line(self, thr: int, mean: float, rule: str = "") -> dict:
        return ps_json(f"ConvertFrom-VenueProbeHeatLine -Lines @('{tele_line(thr, mean, rule)}')")

    def test_a_published_stricter_rule_makes_a_default_clear_reading_hot(self) -> None:
        # throttled 4 and mean 82: clear under the defaults (> 6, >= 88), hot under the published 3 / 80
        base = self.line(4, 82.0)
        self.assertEqual((base["heat"], base["thresholdsSource"]), ("clear", "default"), base)
        got = self.line(4, 82.0, agent_rule(3, 80))
        self.assertEqual((got["heat"], got["thresholdsSource"]), ("hot", "agent"), got)
        # each threshold alone decides: the count (4 > 3 with a cool mean), then the mean (82 >= 80 with a low count)
        self.assertEqual(self.line(4, 70.0, agent_rule(3, 80))["heat"], "hot")
        self.assertEqual(self.line(1, 82.0, agent_rule(3, 80))["heat"], "hot")
        self.assertEqual(self.line(3, 79.9, agent_rule(3, 80))["heat"], "clear")   # the boundaries are the agent's: > 3 and >= 80

    def test_a_published_looser_rule_makes_a_default_hot_reading_clear(self) -> None:
        base = self.line(8, 90.0)
        self.assertEqual((base["heat"], base["thresholdsSource"]), ("hot", "default"), base)
        got = self.line(8, 90.0, agent_rule(10, 95))
        self.assertEqual((got["heat"], got["thresholdsSource"]), ("clear", "agent"), got)
        self.assertEqual(self.line(11, 90.0, agent_rule(10, 95))["heat"], "hot")
        self.assertEqual(self.line(8, 95.0, agent_rule(10, 95))["heat"], "hot")

    def test_the_raw_json_path_reads_the_published_rule_too(self) -> None:
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="thermal-rule-raw-"))

        def raw(name: str, heat: dict) -> str:
            p = d / f"{name}.json"
            p.write_text(json.dumps({"heat": heat}), encoding="utf-8")
            return f"ConvertFrom-VenueProbeHeatLine -Lines @('  raw {p}')"

        rule = {"source": "agent", "maxThrottledRows": 3, "meanMaxC": 80, "sustainedRows": 60, "feedStaleSec": 120}
        hot = ps_json(raw("hot", {"mean_c": 82, "mean_c_exact": 82.0, "throttled": 4, "window_rows": 60, "rule": rule}))
        self.assertEqual((hot["heat"], hot["thresholdsSource"], hot["source"]), ("hot", "agent", "raw-json"), hot)
        clear = ps_json(raw("clear", {"mean_c": 90, "mean_c_exact": 90.0, "throttled": 8, "window_rows": 60, "rule": {**rule, "maxThrottledRows": 10, "meanMaxC": 95}}))
        self.assertEqual((clear["heat"], clear["thresholdsSource"]), ("clear", "agent"), clear)
        absent = ps_json(raw("absent", {"mean_c": 82, "mean_c_exact": 82.0, "throttled": 4, "window_rows": 60}))
        self.assertEqual((absent["heat"], absent["thresholdsSource"]), ("clear", "default"), absent)
        bad = ps_json(raw("bad", {"mean_c": 82, "mean_c_exact": 82.0, "throttled": 9, "window_rows": 60, "rule": {**rule, "meanMaxC": -1}}))
        self.assertEqual((bad["heat"], bad["reason"], bad["thresholdsSource"]), ("unknown", "THERMAL_THRESHOLDS_INVALID", "invalid"), bad)

    def test_an_absent_record_gives_the_defaults_and_says_so(self) -> None:
        got = self.line(7, 70.0)
        self.assertEqual((got["heat"], got["thresholdsSource"], got["reason"]), ("hot", "default", None), got)
        self.assertEqual(self.line(6, 87.9)["heat"], "clear")
        # a probe with no heat line at all is still unknown, and its source is the default (nothing was published)
        none = ps_json("ConvertFrom-VenueProbeHeatLine -Lines @('QUIET mean=5.0%')")
        self.assertEqual((none["heat"], none["thresholdsSource"]), ("unknown", "default"), none)
        # the module's own defaults are unchanged
        self.assertEqual(ps_json("Resolve-VenueHeatRule -Rule $null"),
                         {"valid": True, "source": "default", "maxThrottledRows": 6, "meanMaxC": 88.0, "sustainedRows": 60, "feedStaleSec": 120.0, "reason": None})

    def test_a_malformed_non_finite_or_negative_published_rule_is_unknown_with_a_typed_reason(self) -> None:
        bad = ["rule_source=agent rule_max_throttled=NaN rule_mean_max_c=88 rule_rows=60 rule_stale_s=120",
               "rule_source=agent rule_max_throttled=6 rule_mean_max_c=NaN rule_rows=60 rule_stale_s=120",
               "rule_source=agent rule_max_throttled=6 rule_mean_max_c=Infinity rule_rows=60 rule_stale_s=120",
               "rule_source=agent rule_max_throttled=-1 rule_mean_max_c=88 rule_rows=60 rule_stale_s=120",
               "rule_source=agent rule_max_throttled=6 rule_mean_max_c=-5 rule_rows=60 rule_stale_s=120",
               "rule_source=agent rule_max_throttled=6.5 rule_mean_max_c=88 rule_rows=60 rule_stale_s=120",
               "rule_source=agent rule_max_throttled=6 rule_mean_max_c=88 rule_rows=0 rule_stale_s=120",          # a window the probe cannot honour
               "rule_source=agent rule_max_throttled=6 rule_mean_max_c=88 rule_rows=151 rule_stale_s=120",        # more rows than the probe reads (150)
               "rule_source=agent rule_max_throttled=6 rule_mean_max_c=88 rule_rows=60 rule_stale_s=0",
               "rule_source=agent rule_max_throttled=6 rule_mean_max_c=88 rule_rows=60 rule_stale_s=-3",
               "rule_source=agent rule_max_throttled=6 rule_mean_max_c=88 rule_rows=60 rule_stale_s=NaN",
               "rule_source=agent rule_max_throttled=abc rule_mean_max_c=88 rule_rows=60 rule_stale_s=120",
               "rule_source=agent rule_max_throttled=6 rule_mean_max_c=88 rule_rows=60",                          # a field missing
               "rule_source=invalid",                                                                              # the probe could not read the agent's record
               "rule_source=bogus rule_max_throttled=6 rule_mean_max_c=88 rule_rows=60 rule_stale_s=120"]
        for tail in bad:
            # throttled 9 would be hot under any rule: an unusable rule must not decide, nor fall back to the defaults
            got = self.line(9, 95.0, " " + tail)
            self.assertEqual((got["heat"], got["reason"], got["thresholdsSource"]), ("unknown", "THERMAL_THRESHOLDS_INVALID", "invalid"), (tail, got))
        for rule in ({"source": "agent", "maxThrottledRows": "NaN", "meanMaxC": 88, "sustainedRows": 60, "feedStaleSec": 120},
                     {"source": "agent", "maxThrottledRows": 6, "meanMaxC": "Infinity", "sustainedRows": 60, "feedStaleSec": 120},
                     {"source": "agent", "maxThrottledRows": -2, "meanMaxC": 88, "sustainedRows": 60, "feedStaleSec": 120},
                     {"source": "agent", "maxThrottledRows": 6, "meanMaxC": 88, "sustainedRows": 0, "feedStaleSec": 120},
                     {"source": "agent", "maxThrottledRows": 6, "meanMaxC": 88, "sustainedRows": 60},
                     {"source": "malformed"}):
            got = ps_json(f"Resolve-VenueHeatRule -Rule ('{json.dumps(rule)}' | ConvertFrom-Json)")
            self.assertEqual((got["valid"], got["reason"]), (False, "THERMAL_THRESHOLDS_INVALID"), (rule, got))

    def test_the_heat_rule_itself_is_unchanged_count_first_and_non_finite_means_still_decide_as_before(self) -> None:
        # the count is tested first under a published rule too; +Infinity is hot, NaN / -Infinity alone are unknown (the r5 behaviour, now under a non-default rule)
        got = ps_json("$nan = [double]::NaN; $pi = [double]::PositiveInfinity; "
                      "@((Get-VenueHeatVerdict -Throttled 4 -MeanC $nan -MaxThrottledRows 3 -MeanMaxC 80), (Get-VenueHeatVerdict -Throttled 0 -MeanC $pi -MaxThrottledRows 3 -MeanMaxC 80), "
                      "(Get-VenueHeatVerdict -Throttled 0 -MeanC $nan -MaxThrottledRows 3 -MeanMaxC 80), (Get-VenueHeatVerdict -Throttled 3 -MeanC 79.9 -MaxThrottledRows 3 -MeanMaxC 80), "
                      "(Get-VenueHeatVerdict -Throttled -1 -MeanC 10 -MaxThrottledRows 3 -MeanMaxC 80))")
        self.assertEqual(got, ["hot", "hot", "unknown", "clear", "unknown"], got)

    # ---- the agent publishes what it actually runs with
    def test_e_the_agent_publishes_its_effective_params_defaults_and_non_default(self) -> None:
        defaults = ps_json(agent_fn_ps("Get-AgentHeatRule", "$GateMaxThrottledRows = 6; $GateMeanMaxC = 88.0; $GateSustainedRows = 60; $GateFeedStaleSec = 120", "Get-AgentHeatRule"))
        # r5 (adversary r4 A1): meanMaxCBits, the threshold's exact IEEE-754 bits, rides beside the human meanMaxC (forced companion of the r5 publisher change)
        self.assertEqual(defaults, {"maxThrottledRows": 6, "meanMaxC": 88.0, "meanMaxCBits": double_bits(88.0), "sustainedRows": 60, "feedStaleSec": 120})
        custom = ps_json(agent_fn_ps("Get-AgentHeatRule", "$GateMaxThrottledRows = 3; $GateMeanMaxC = 80.5; $GateSustainedRows = 30; $GateFeedStaleSec = 45", "Get-AgentHeatRule"))
        self.assertEqual(custom, {"maxThrottledRows": 3, "meanMaxC": 80.5, "meanMaxCBits": double_bits(80.5), "sustainedRows": 30, "feedStaleSec": 45})
        # a non-finite double is published as its name (a JSON number cannot carry it), so the gate sees it and refuses it
        nan = ps_json(agent_fn_ps("Get-AgentHeatRule", "$GateMaxThrottledRows = 6; $GateMeanMaxC = [double]::NaN; $GateSustainedRows = 60; $GateFeedStaleSec = 120", "Get-AgentHeatRule"))
        self.assertEqual(nan["meanMaxC"], "NaN")
        # the published values, fed to the gate, are accepted as the rule
        self.assertEqual(ps_json("Resolve-VenueHeatRule -Rule ('" + json.dumps({**custom, "source": "agent"}) + "' | ConvertFrom-Json)")["maxThrottledRows"], 3)

    def test_e_the_agent_wires_the_rule_into_the_admission_record_and_the_job_env(self) -> None:
        agent = AGENT.read_text(encoding="utf-8")
        self.assertIn("function Get-AgentHeatRule", agent)
        self.assertIn("$env:MLV_AGENT_HEAT_RULE = ($heatRule | ConvertTo-Json -Compress)", agent)
        self.assertIn("$heatRule = Get-AgentHeatRule", agent)
        self.assertIn("-NotePropertyName heatRule", agent)
        # the env var is the agent's own constant, overwritten for every admitted job: it needs no Remove-Item (test_owner_footage_delete_class.py pins the exact Remove-Item lines)
        self.assertNotIn("Env:\\MLV_AGENT_HEAT_RULE", agent)
        # the decision still reads the very params that are published
        self.assertIn("$Metrics.throttled -gt $GateMaxThrottledRows -or $Metrics.meanC -ge $GateMeanMaxC", agent)

    # ---- the probe copies the agent's record, and reads the feed with the agent's window and staleness
    def test_the_probe_reads_the_agents_env_rule_and_honours_its_window_and_staleness(self) -> None:
        ok = ps_json(quiet_fn_ps("Read-TelAgentHeatRule", "", "Read-TelAgentHeatRule -Json '{\"maxThrottledRows\":3,\"meanMaxC\":80,\"sustainedRows\":30,\"feedStaleSec\":45}' -DefaultRows 60 -DefaultStaleSec 120"))
        self.assertEqual((ok["source"], ok["maxThrottledRows"], ok["meanMaxC"], ok["sustainedRows"], ok["feedStaleSec"], ok["windowRows"], ok["staleSec"]), ("agent", 3, 80, 30, 45, 30, 45), ok)
        absent = ps_json(quiet_fn_ps("Read-TelAgentHeatRule", "", "Read-TelAgentHeatRule -Json '' -DefaultRows 60 -DefaultStaleSec 120"))
        self.assertEqual((absent["source"], absent["windowRows"], absent["staleSec"]), ("default", 60, 120), absent)
        for text in ("{not json", '{"maxThrottledRows":3}', '{"maxThrottledRows":3,"meanMaxC":80,"sustainedRows":0,"feedStaleSec":45}',
                     '{"maxThrottledRows":3,"meanMaxC":"NaN","sustainedRows":30,"feedStaleSec":45}', "[1,2]", '"text"'):
            bad = ps_json(quiet_fn_ps("Read-TelAgentHeatRule", "", f"Read-TelAgentHeatRule -Json '{text}' -DefaultRows 60 -DefaultStaleSec 120"))
            self.assertEqual((bad["source"], bad["windowRows"], bad["staleSec"]), ("invalid", 60, 120), (text, bad))
        quiet = QUIET.read_text(encoding="utf-8")
        self.assertIn("$env:MLV_AGENT_HEAT_RULE", quiet)
        self.assertIn("rule_source=", quiet)
        self.assertIn("$telHeat['rule'] = ", quiet)

    def test_the_probes_printed_line_round_trips_the_published_rule_into_the_gate(self) -> None:
        # the probe's own printer (Write-TelemetryLines, taken from Wait-VenueQuiet.ps1) -> the gate's line parser, through the probe's JSON round trip
        funcs = "\n".join(quiet_fn_ps(n, "", "") for n in ("ConvertTo-IdentToken", "Format-TelNum", "Get-TelReasonSuffix", "Write-TelemetryLines"))
        script = (
            "$telemetrySchemaV1 = 'mlv-app/venue-quiet-probe-telemetry/v1'; function Write-Gate([string]$s) { $s }\n" + funcs + "\n"
            "function Get-Case($rule, [int]$thr, [double]$mean) {\n"
            "  $h = [ordered]@{ source = 'C:\\temp\\hwinfo-log-adapter.csv'; sample_age_s = 1.0; hottest_core_c = 50.0; max15_c = 60.0; mean_c = $mean; mean_c_exact = $mean; throttled = $thr; window_rows = 60; package_w = 10.0; reasons = [ordered]@{} }\n"
            "  if ($null -ne $rule) { $h['rule'] = $rule }\n"
            "  $p = [ordered]@{ telemetry_schema = $telemetrySchemaV1; heat = $h; gpu = [ordered]@{ state = 'absent' }; last_lease = [ordered]@{ event = 'none' } } | ConvertTo-Json -Depth 6 | ConvertFrom-Json\n"
            "  $out = @('  raw UNWRITABLE sharing violation') + @(Write-TelemetryLines $p)\n"
            "  $r = ConvertFrom-VenueProbeHeatLine -Lines $out\n"
            "  [pscustomobject]@{ heat = $r.heat; src = $r.thresholdsSource; reason = $r.reason; line = [string](@($out | Where-Object { $_ -match '^\\s*tele heat ' })[0]) }\n"
            "}\n"
            "$strict = [ordered]@{ source = 'agent'; maxThrottledRows = 3; meanMaxC = 80.5; sustainedRows = 60; feedStaleSec = 120 }\n"
            "$loose = [ordered]@{ source = 'agent'; maxThrottledRows = 10; meanMaxC = 95; sustainedRows = 30; feedStaleSec = 45 }\n"
            "@((Get-Case $strict 4 82.0), (Get-Case $null 4 82.0), (Get-Case $loose 8 90.0), (Get-Case $null 8 90.0), (Get-Case ([ordered]@{ source = 'invalid' }) 9 95.0))")
        got = ps_json(script)
        self.assertEqual([(g["heat"], g["src"], g["reason"]) for g in got],
                         [("hot", "agent", None), ("clear", "default", None), ("clear", "agent", None), ("hot", "default", None), ("unknown", "invalid", "THERMAL_THRESHOLDS_INVALID")], got)
        self.assertTrue(got[0]["line"].endswith(" rule_source=agent rule_max_throttled=3 rule_mean_max_c=80.5 rule_rows=60 rule_stale_s=120"), got[0]["line"])
        self.assertNotIn("rule_source", got[1]["line"], "nothing published prints nothing")
        self.assertTrue(got[4]["line"].endswith(" rule_source=invalid"), got[4]["line"])

    # ---- adversary r1 A1 (introduced major) and its siblings: a number is never squeezed through [string] (15 significant digits) on its way to a comparison
    def test_r1_a1_a_threshold_that_needs_every_digit_is_published_and_applied_exactly(self) -> None:
        exact = "80.00000000000001"   # the agent: mean 80 is CLEAR (80 >= 80.00000000000001 is false); the gate must agree on every path
        pub = ps(agent_fn_ps("Get-AgentHeatRule", f"$GateMaxThrottledRows = 6; $GateMeanMaxC = {exact}; $GateSustainedRows = 60; $GateFeedStaleSec = 120", "(Get-AgentHeatRule | ConvertTo-Json -Compress)"))
        # r4 A1: a threshold 15 digits cannot carry is published as its round-trip TEXT (Windows PowerShell 5.1 would cut a number to 15 digits); the gate reads the text invariant
        self.assertRegex(pub, r'"meanMaxC":"?' + re.escape(exact) + r'"?[,}]', "the agent publishes the threshold it runs with, every digit")
        probe_json = '{"maxThrottledRows":6,"meanMaxC":' + exact + ',"sustainedRows":60,"feedStaleSec":120}'
        rd = ps_json(quiet_fn_ps("Read-TelAgentHeatRule", "", f"Read-TelAgentHeatRule -Json '{probe_json}' -DefaultRows 60 -DefaultStaleSec 120"))
        self.assertEqual((rd["source"], rd["meanMaxC"]), ("agent", float(exact)), rd)
        # the probe's printer keeps the digits on the line; the gate reads them back
        funcs = "\n".join(quiet_fn_ps(n, "", "") for n in ("ConvertTo-IdentToken", "Format-TelNum", "Get-TelReasonSuffix", "Write-TelemetryLines"))
        printed = ps(
            "$telemetrySchemaV1 = 'mlv-app/venue-quiet-probe-telemetry/v1'; function Write-Gate([string]$s) { $s }\n" + funcs + "\n"
            f"$p = [ordered]@{{ telemetry_schema = $telemetrySchemaV1; heat = [ordered]@{{ source = 'x'; sample_age_s = 1.0; hottest_core_c = 50.0; max15_c = 60.0; mean_c = 80.0; mean_c_exact = 80.0; throttled = 0; window_rows = 60; package_w = 10.0; reasons = [ordered]@{{}}; "
            f"rule = [ordered]@{{ source = 'agent'; maxThrottledRows = 6; meanMaxC = {exact}; sustainedRows = 60; feedStaleSec = 120 }} }}; gpu = [ordered]@{{ state = 'absent' }}; last_lease = [ordered]@{{ event = 'none' }} }} | ConvertTo-Json -Depth 6 | ConvertFrom-Json\n"
            "@(Write-TelemetryLines $p)[0]")
        self.assertIn(f" rule_mean_max_c={exact} ", printed + " ", printed)
        # the gate: the printed line and the raw JSON both read mean 80.0 as clear, and 80.00000000000001 as hot (>=)
        for mean, want in ((80.0, "clear"), (float(exact), "hot")):
            line = f"  tele heat source=x age=1.0s hottest=50.0C max15=60.0C mean={mean:.1f}C throttled=0/60 W=10.0 mean_exact={mean!r} rule_source=agent rule_max_throttled=6 rule_mean_max_c={exact} rule_rows=60 rule_stale_s=120"
            got = ps_json(f"ConvertFrom-VenueProbeHeatLine -Lines @('{line}')")
            self.assertEqual((got["heat"], got["thresholdsSource"]), (want, "agent"), (mean, got))
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="thermal-a1-"))
        for mean, want in ((80.0, "clear"), (float(exact), "hot")):
            p = d / f"raw-{mean!r}.json"
            p.write_text('{"heat": {"mean_c": 80.0, "mean_c_exact": ' + repr(mean) + ', "throttled": 0, "window_rows": 60, "rule": {"source": "agent", "maxThrottledRows": 6, "meanMaxC": ' + exact
                         + ', "sustainedRows": 60, "feedStaleSec": 120}}}', encoding="utf-8")
            got = ps_json(f"ConvertFrom-VenueProbeHeatLine -Lines @('  raw {p}')")
            self.assertEqual((got["heat"], got["source"]), (want, "raw-json"), (mean, got))
        # the sibling: the largest finite double is a finite threshold (the lossy string form overflowed it), and an exact mean one ulp under the DEFAULT 88 is clear
        big = ps_json("Resolve-VenueHeatRule -Rule ([pscustomobject]@{ source = 'agent'; maxThrottledRows = 6; meanMaxC = [double]::MaxValue; sustainedRows = 60; feedStaleSec = 120 })")
        self.assertEqual((big["valid"], big["meanMaxC"]), (True, 1.7976931348623157e308), big)
        under = "87.99999999999999"
        self.assertEqual(ps_json(f"ConvertFrom-VenueProbeHeatLine -Lines @('  tele heat source=x age=1.0s hottest=50.0C max15=60.0C mean=88.0C throttled=0/60 W=10.0 mean_exact={under}')")["heat"], "clear")
        self.assertEqual(ps_json(f"(Get-VenueHeatVerdict -Throttled 0 -MeanC ([double]{under}))"), "clear")

    def test_r1_a2_an_array_is_not_a_published_rule(self) -> None:
        # a numeric array coerces through [string]; the record must be one object of scalar numbers
        for text in ('[{"maxThrottledRows":6,"meanMaxC":88,"sustainedRows":60,"feedStaleSec":120}]', '{"maxThrottledRows":[3],"meanMaxC":[80],"sustainedRows":[60],"feedStaleSec":[120]}'):
            bad = ps_json(quiet_fn_ps("Read-TelAgentHeatRule", "", f"Read-TelAgentHeatRule -Json '{text}' -DefaultRows 60 -DefaultStaleSec 120"))
            self.assertEqual(bad["source"], "invalid", (text, bad))
        got = ps_json("Resolve-VenueHeatRule -Rule ('{\"source\":\"agent\",\"maxThrottledRows\":[3],\"meanMaxC\":[80],\"sustainedRows\":[60],\"feedStaleSec\":[120]}' | ConvertFrom-Json)")
        self.assertEqual((got["valid"], got["reason"]), (False, "THERMAL_THRESHOLDS_INVALID"), got)
        # a raw heat.rule that is a one-element array is no object either (a $(...) would have unwrapped it into the object inside)
        import tempfile
        p = Path(tempfile.mkdtemp(prefix="thermal-a2-")) / "raw.json"
        p.write_text('{"heat": {"mean_c": 82, "mean_c_exact": 82.0, "throttled": 4, "window_rows": 60, "rule": [{"source": "agent", "maxThrottledRows": 3, "meanMaxC": 80, "sustainedRows": 60, "feedStaleSec": 120}]}}', encoding="utf-8")
        got = ps_json(f"ConvertFrom-VenueProbeHeatLine -Lines @('  raw {p}')")
        self.assertEqual((got["heat"], got["reason"]), ("unknown", "THERMAL_THRESHOLDS_INVALID"), got)

    # ---- the leg receipt says where the thresholds came from
    def test_the_runner_receipt_carries_thresholds_source(self) -> None:
        src = (DV / "Invoke-VenueLeaseBatch.ps1").read_text(encoding="utf-8")
        self.assertIn("thresholds_source", src)
        self.assertIn("thresholdsSource = $ht.thresholdsSource", src)


POWERSHELL_51 = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
requires_windows_powershell_51 = unittest.skipUnless(sys.platform == "win32" and POWERSHELL_51.exists(), "needs Windows PowerShell 5.1")
RULE_JSON = '{"maxThrottledRows":6,"meanMaxC":88,"sustainedRows":60,"feedStaleSec":120}'


def shell_json(exe, script: str):
    r = subprocess.run([str(exe), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", f"$ErrorActionPreference='Stop'; ConvertTo-Json -Depth 6 -Compress -InputObject $({script})"],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise AssertionError(r.stdout + r.stderr)
    return json.loads(r.stdout.strip())


@requires_windows_pwsh
class AgentThresholdParityR3Tests(unittest.TestCase):
    """BACHELOR-THERMAL-GATE-AGENT-THRESHOLD-PARITY-1 r3 (adversary r2 A1-A4). The agent runs under Windows PowerShell 5.1 when pwsh is not installed on the venue, so the probe's reader must
    work there (5.1 has no ConvertFrom-Json -NoEnumerate), and what the agent's binder accepts must be exactly what the gate accepts."""

    def reader(self, exe, text: str):
        return shell_json(exe, quiet_fn_ps("Read-TelAgentHeatRule", "", f"Read-TelAgentHeatRule -Json '{text}' -DefaultRows 60 -DefaultStaleSec 120"))

    # ---- A1: the published-rule reader on Windows PowerShell 5.1
    @requires_windows_powershell_51
    def test_r3_a1_the_reader_applies_a_published_rule_under_windows_powershell_5_1(self) -> None:
        ok = self.reader(POWERSHELL_51, RULE_JSON)
        self.assertEqual((ok["source"], ok["maxThrottledRows"], ok["meanMaxC"], ok["sustainedRows"], ok["feedStaleSec"]), ("agent", 6, 88, 60, 120), ok)
        # the whole chain on 5.1: a hot reading (throttled 7 of 60, mean 90) under the published 6 / 88 is hot, not unknown
        rule = {"source": ok["source"], "maxThrottledRows": ok["maxThrottledRows"], "meanMaxC": ok["meanMaxC"], "sustainedRows": ok["sustainedRows"], "feedStaleSec": ok["feedStaleSec"]}
        self.assertEqual(ps_json("Resolve-VenueHeatRule -Rule ('" + json.dumps(rule) + "' | ConvertFrom-Json)")["valid"], True)

    def test_r3_a1_a_root_array_is_no_rule_on_either_shell(self) -> None:
        # 5.1 and 7 both unroll a one-element root array into the object inside it, so the root is refused from the text, as the agent's own sidecar reader does
        shells = [PWSH] + ([POWERSHELL_51] if POWERSHELL_51.exists() and sys.platform == "win32" else [])
        for exe in shells:
            for text in ('[' + RULE_JSON + ']', ' \t[' + RULE_JSON + ']', '[' + RULE_JSON + ',' + RULE_JSON + ']', "[1,2]", '"text"', "7", "null"):
                self.assertEqual(self.reader(exe, text)["source"], "invalid", (str(exe), text))
            self.assertEqual(self.reader(exe, '  \r\n' + RULE_JSON)["source"], "agent", str(exe))   # JSON whitespace before the object is fine

    @requires_windows_powershell_51
    def test_r3_a1_the_offline_samples_reader_has_no_noenumerate_either(self) -> None:
        self.assertNotIn("-NoEnumerate", "\n".join(l for l in QUIET.read_text(encoding="utf-8").splitlines() if not l.lstrip().startswith("#")))
        for exe in (POWERSHELL_51, PWSH):
            for samples, want in (("[10,10,10]", "DECISION QUIET mean=10.0%"), ("[[10,10,10]]", "DECISION UNKNOWN mean=UNKNOWN"), ("[]", "DECISION UNKNOWN mean=UNKNOWN")):
                r = subprocess.run([str(exe), "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(QUIET), "-SamplesJson", samples], capture_output=True, text=True, timeout=120)
                self.assertEqual((r.stdout.strip(), r.stderr.strip()), (want, ""), (str(exe), samples))

    # ---- A2: the binder refuses what the gate refuses
    def binder(self, exe, name: str, value: str) -> bool:
        """True when the agent's OWN declaration of parameter `name` (its attributes included, taken from the agent's AST) binds `value`."""
        script = (f"$t = $null; $e = $null; $ast = [System.Management.Automation.Language.Parser]::ParseFile('{AGENT}', [ref]$t, [ref]$e); "
                  f"$p = $ast.ParamBlock.Parameters | Where-Object {{ $_.Name.VariablePath.UserPath -eq '{name}' }}; if ($null -eq $p) {{ throw 'no such param' }}; "
                  f"$f = [scriptblock]::Create('param(' + $p.Extent.Text + ') $' + '{name}'); try {{ & $f -{name} ({value}) | Out-Null; $true }} catch {{ $false }}")
        return shell_json(exe, script)

    def test_r3_a2_the_binder_accepts_no_heat_threshold_the_gate_refuses(self) -> None:
        candidates = {"GateMeanMaxC": ["-1", "-0.5", "-88", "[double]::NaN", "[double]::PositiveInfinity", "[double]::NegativeInfinity", "0", "0.5", "88", "[double]::MaxValue"],
                      "GateMaxThrottledRows": ["-1", "0", "6", "150", "151"],
                      "GateSustainedRows": ["-1", "0", "1", "14", "15", "60", "150", "151"],
                      "GateFeedStaleSec": ["-1", "0", "1", "9", "10", "120", "3600", "3601"]}
        shells = [PWSH] + ([POWERSHELL_51] if POWERSHELL_51.exists() and sys.platform == "win32" else [])
        for exe in shells:
            for name, values in candidates.items():
                for v in values:
                    if not self.binder(exe, name, v):
                        continue
                    pub = {"GateMaxThrottledRows": "6", "GateMeanMaxC": "88.0", "GateSustainedRows": "60", "GateFeedStaleSec": "120"}
                    pub[name] = v
                    sets = "; ".join(f"${k} = {x}" for k, x in pub.items())
                    rule = ps_json(agent_fn_ps("Get-AgentHeatRule", sets, "Get-AgentHeatRule"))
                    got = ps_json("Resolve-VenueHeatRule -Rule ('" + json.dumps({**rule, "source": "agent"}) + "' | ConvertFrom-Json)")
                    self.assertTrue(got["valid"], f"{exe}: the agent binds {name}={v} and publishes {rule}, which the gate refuses ({got})")
        # and the one the adversary named is refused at the source
        self.assertFalse(self.binder(PWSH, "GateMeanMaxC", "-1"))
        self.assertTrue(self.binder(PWSH, "GateMeanMaxC", "88"))

    # ---- A3: a padded numeric string is stored as the number it parses to
    def test_r3_a3_a_padded_numeric_text_is_stored_as_its_number_and_round_trips_to_the_gate(self) -> None:
        # r6 (sol r5 B2): a COUNT is an integer token, so a padded count is refused (AgentThresholdParityR6Tests); the doubles may still be padded text
        got = self.reader(PWSH, '{"maxThrottledRows":"6","meanMaxC":" 80 ","sustainedRows":"60","feedStaleSec":" 120"}')
        self.assertEqual((got["source"], got["maxThrottledRows"], got["meanMaxC"], got["sustainedRows"], got["feedStaleSec"]), ("agent", 6, 80, 60, 120), got)
        for v in (got["maxThrottledRows"], got["meanMaxC"], got["sustainedRows"], got["feedStaleSec"]):
            self.assertIsInstance(v, (int, float), got)

    # ---- A4: every admission record carries the rule, the early exits included
    def test_r3_a4_the_heat_rule_is_on_the_admission_before_any_early_exit_publishes_it(self) -> None:
        agent = AGENT.read_text(encoding="utf-8")
        body = agent[agent.index("function Invoke-QueuedJob"):]
        body = body[body.index("Wait-QuietLeaseAdmission -JobId $jobId"):]   # from the admission's creation on
        add = body.index("-NotePropertyName heatRule")
        for needle in ('label -eq "queue-expired"', 'label -eq "cancelled-in-gate"', 'label -eq "expired-not-after"', "Publish-GateUnlaunchedResult", "Invoke-QueueExpiredJob", "Publish-NotAfterExpiredResult"):
            self.assertLess(add, body.index(needle), f"admission.heatRule is added before {needle}")
        self.assertEqual(body.count("-NotePropertyName heatRule"), 1)
        self.assertIn("$env:MLV_AGENT_HEAT_RULE = ($heatRule | ConvertTo-Json -Compress)", body)


SHELLS_R4 = [PWSH] + ([POWERSHELL_51] if POWERSHELL_51.exists() and sys.platform == "win32" else [])


@requires_windows_pwsh
class AgentThresholdParityR4Tests(unittest.TestCase):
    """BACHELOR-THERMAL-GATE-AGENT-THRESHOLD-PARITY-1 r4 (adversary r3 A1, A2). Windows PowerShell 5.1 (the agent's shell when pwsh is absent on the venue) writes a [double] into JSON with
    15 significant digits, or through 'R', which on .NET Framework is documented to miss the round trip for some values: a threshold or a mean that needs every digit reaches the gate as a
    neighbouring double, so the agent says clear and the gate says hot. Every double on the path agent -> job env -> probe -> probe JSON -> printed line -> gate must arrive bit-identical."""

    # Doubles are built from their BIT PATTERNS, so neither shell's literal parser can move them; the verdict is compared in Python, whose float() is correctly rounded.
    THR_F = float("0.84551240822557006")    # the adversary's threshold (GateMeanMaxC)
    MEAN_F = float("0.84551240822556994")   # the double one ulp under it: the agent's own comparison (mean >= threshold) is FALSE, so the agent says clear

    @staticmethod
    def bits(f: float) -> str:
        import struct
        return "[BitConverter]::Int64BitsToDouble(" + str(struct.unpack("<q", struct.pack("<d", f))[0]) + ")"

    @staticmethod
    def as_float(v) -> float:
        return float(v)   # a JSON number or the round-trip text the probe publishes: both come back through Python's correctly rounded parser

    def chain(self, exe) -> dict:
        """The whole path, in the shell under test, with the project's own functions (lifted from their files, never restated)."""
        funcs = "\n".join(quiet_fn_ps(n, "", "") for n in ("ConvertTo-JsonSafeDouble", "Read-TelAgentHeatRule", "ConvertTo-IdentToken", "Format-TelNum", "Get-TelReasonSuffix", "Write-TelemetryLines"))
        script = (
            f"Import-Module '{GATE}' -Force\n" + agent_fn_ps("Get-AgentHeatRule", f"$GateMaxThrottledRows = 6; $GateMeanMaxC = {self.bits(self.THR_F)}; $GateSustainedRows = 15; $GateFeedStaleSec = 120", "") + "\n"
            "$telemetrySchemaV1 = 'mlv-app/venue-quiet-probe-telemetry/v1'; function Write-Gate([string]$s) { $s }\n" + funcs + "\n"
            f"$thr = {self.bits(self.THR_F)}; $mean = {self.bits(self.MEAN_F)}\n"
            "$agentSays = $(if ($mean -ge $thr) { 'hot' } else { 'clear' })\n"
            "$envJson = (Get-AgentHeatRule | ConvertTo-Json -Compress)\n"                                  # the job env, as the agent writes it
            "$ar = Read-TelAgentHeatRule -Json $envJson -DefaultRows 60 -DefaultStaleSec 120\n"            # the probe reads it
            "$heat = [ordered]@{ source = 'x'; sample_age_s = 1.0; hottest_core_c = 50.0; max15_c = 60.0; mean_c = [math]::Round($mean, 2); mean_c_exact = (ConvertTo-JsonSafeDouble $mean); throttled = 0; window_rows = 15; package_w = 10.0; reasons = [ordered]@{}\n"
            "  rule = [ordered]@{ source = $ar.source; maxThrottledRows = $ar.maxThrottledRows; meanMaxC = (ConvertTo-JsonSafeDouble $ar.meanMaxC); sustainedRows = $ar.sustainedRows; feedStaleSec = (ConvertTo-JsonSafeDouble $ar.feedStaleSec) } }\n"
            "$probeJson = ([ordered]@{ telemetry_schema = $telemetrySchemaV1; heat = $heat; gpu = [ordered]@{ state = 'absent' }; last_lease = [ordered]@{ event = 'none' } } | ConvertTo-Json -Compress -Depth 6)\n"
            "$p = $probeJson | ConvertFrom-Json\n"
            "$lines = @(Write-TelemetryLines $p)\n"
            "$viaLine = ConvertFrom-VenueProbeHeatLine -Lines (@('  raw UNWRITABLE sharing violation') + $lines)\n"      # the printed line
            "$f = Join-Path ([IO.Path]::GetTempPath()) ('thermal-r4-' + [guid]::NewGuid().ToString('N') + '.json'); [IO.File]::WriteAllText($f, $probeJson)\n"
            "$viaRaw = ConvertFrom-VenueProbeHeatLine -Lines @(\"  raw $f\"); Remove-Item -LiteralPath $f -Force\n"             # the raw JSON
            "[pscustomobject]@{ agent = $agentSays; line = $viaLine.heat; raw = $viaRaw.heat; src = $viaLine.thresholdsSource; text = [string](@($lines | Where-Object { $_ -match '^\\s*tele heat ' })[0]); envJson = $envJson }"
        )
        return shell_json(exe, script)

    def test_r4_a1_a_threshold_and_a_mean_that_need_every_digit_reach_the_gate_exactly_on_both_shells(self) -> None:
        for exe in SHELLS_R4:
            got = self.chain(exe)
            self.assertEqual(got["agent"], "clear", got)
            self.assertEqual((got["line"], got["raw"], got["src"]), ("clear", "clear", "agent"), f"{exe}: agent says clear, the gate must too: {got}")
            thr = re.search(r" rule_mean_max_c=(\S+) ", got["text"] + " ")
            mean = re.search(r" mean_exact=(\S+) ", got["text"] + " ")
            self.assertTrue(thr and mean, got["text"])
            self.assertEqual((self.as_float(thr.group(1)), self.as_float(mean.group(1))), (self.THR_F, self.MEAN_F), f"{exe}: the printed line must carry both doubles bit-identical: {got['text']}")

    def test_r4_a1_the_published_form_round_trips_bit_identical_on_both_shells(self) -> None:
        cases = [self.THR_F, self.MEAN_F, float("80.00000000000001"), 88.0, 0.1, 88.5, 1e300, 1.7976931348623157e308, 5e-324, 1e-5, 0.0, 123456789.12345678]
        for exe in SHELLS_R4:
            for f in cases:
                out = shell_json(exe, quiet_fn_ps("ConvertTo-JsonSafeDouble", "", f"[pscustomobject]@{{ x = (ConvertTo-JsonSafeDouble ({self.bits(f)})) }}"))
                self.assertEqual(self.as_float(out["x"]), f, f"{exe}: {f!r} published as {out['x']!r}")
            # the threshold needs more than 15 digits: it can only be carried as text, never as a number a 5.1 writer would cut to 15 digits
            self.assertIsInstance(shell_json(exe, quiet_fn_ps("ConvertTo-JsonSafeDouble", "", f"[pscustomobject]@{{ x = (ConvertTo-JsonSafeDouble ({self.bits(self.THR_F)})) }}"))["x"], str, str(exe))
            for f in (88.0, 0.1, 88.5):
                self.assertIsInstance(shell_json(exe, quiet_fn_ps("ConvertTo-JsonSafeDouble", "", f"[pscustomobject]@{{ x = (ConvertTo-JsonSafeDouble ({self.bits(f)})) }}"))["x"], (int, float), f"{exe}: {f} stays a JSON number")
            for v in ("[double]::NaN", "[double]::PositiveInfinity", "[double]::NegativeInfinity"):
                out = shell_json(exe, quiet_fn_ps("ConvertTo-JsonSafeDouble", "", f"[pscustomobject]@{{ x = (ConvertTo-JsonSafeDouble ({v})) }}"))
                self.assertIsInstance(out["x"], str, f"{exe}: a non-finite double is published by name")
            self.assertIsNone(shell_json(exe, quiet_fn_ps("ConvertTo-JsonSafeDouble", "", "[pscustomobject]@{ x = (ConvertTo-JsonSafeDouble $null) }"))["x"], "no value, no number")

    def test_r4_a1_the_agent_publishes_the_threshold_exactly_under_either_shell(self) -> None:
        for exe in SHELLS_R4:
            for f in (self.THR_F, self.MEAN_F, float("80.00000000000001"), 88.0, 0.1):
                out = shell_json(exe, agent_fn_ps("Get-AgentHeatRule", f"$GateMaxThrottledRows = 6; $GateMeanMaxC = {self.bits(f)}; $GateSustainedRows = 60; $GateFeedStaleSec = 120",
                                                  "$j = (Get-AgentHeatRule | ConvertTo-Json -Compress); [pscustomobject]@{ json = $j }"))
                published = json.loads(out["json"])["meanMaxC"]   # exactly what the job env carries, parsed by Python
                self.assertEqual(self.as_float(published), f, f"{exe}: published {out['json']} for {f!r}")

    def test_r4_a5_a_source_with_a_soft_hyphen_inside_agent_is_not_agent(self) -> None:
        # adversary r3 A5: -ceq / -cne ignore a soft hyphen (U+00AD) inside the word, so "ag<SHY>ent" passed as the agent's record; the compare is ordinal
        for exe in SHELLS_R4:
            out = shell_json(exe, f"Import-Module '{GATE}' -Force; $s = 'ag' + [char]0xAD + 'ent'; "
                                  "[pscustomobject]@{ shy = (Resolve-VenueHeatRule -Rule ([pscustomobject]@{ source = $s; maxThrottledRows = 6; meanMaxC = 88; sustainedRows = 60; feedStaleSec = 120 })).valid; "
                                  "ok = (Resolve-VenueHeatRule -Rule ([pscustomobject]@{ source = 'agent'; maxThrottledRows = 6; meanMaxC = 88; sustainedRows = 60; feedStaleSec = 120 })).valid }")
            self.assertEqual((out["shy"], out["ok"]), (False, True), f"{exe}: {out}")

    # ---- A2: the in-gate queue-expired exit publishes the admission (and so the rule)
    def test_r4_a2_a_queue_expired_result_carries_the_admission_and_its_heat_rule(self) -> None:
        out = ps_json(agent_fn_ps("Invoke-QueueExpiredJob", "",
                                  "$script:Pub = $null; function Publish-JobResult { param($JobId, $Result) $script:Pub = $Result; 'x' }; function Move-JobArtifacts { param($JobId, $JobPath, $DestinationDirectory) }; "
                                  "function Add-AgentLogLine { param($m) }; $processed = 'p'; "
                                  "$job = [pscustomobject]@{ BaseName = 'j1.job'; FullName = 'C:\\x\\j1.job' }; $md = [pscustomobject]@{ timeoutSec = 60; maxQueueAgeSec = 600 }; "
                                  "$adm = [pscustomobject]@{ label = 'queue-expired'; queueAgeSec = 700; heatRule = [ordered]@{ maxThrottledRows = 6; meanMaxC = 88.0; sustainedRows = 60; feedStaleSec = 120 } }; "
                                  "Invoke-QueueExpiredJob -Job $job -Metadata $md -QueueAgeSec 700 -Admission $adm; $script:Pub"))
        self.assertEqual((out["queueExpired"], out["admission"]["label"], out["admission"]["heatRule"]["meanMaxC"]), (True, "queue-expired", 88.0), out)

    def test_r4_a2_every_exit_after_the_admission_exists_publishes_it(self) -> None:
        agent = AGENT.read_text(encoding="utf-8")
        body = agent[agent.index("function Invoke-QueuedJob"):]
        body = body[body.index("-NotePropertyName heatRule"):]   # from the point the rule is on the admission record
        calls = re.findall(r"^\s*(Invoke-QueueExpiredJob|Publish-GateUnlaunchedResult|Publish-NotAfterExpiredResult)\b(.*)$", body, flags=re.M)
        self.assertGreaterEqual(len(calls), 5, calls)
        for name, rest in calls:
            self.assertIn("-Admission $script:Admission", rest, f"{name} after the admission exists must carry it: {rest}")
        self.assertNotIn("(expired before or inside the gate)", agent, "the stale 'shape unchanged inside the gate' comment is gone")


def quiet_fn_opt(name: str) -> str:
    """Wait-VenueQuiet.ps1's function `name` if the script defines it, else nothing (a RED run on a head that predates it falls back to that head's own inline form)."""
    return quiet_fn_ps(name, "", "") if any(l.startswith(f"function {name}") for l in QUIET.read_text(encoding="utf-8").splitlines()) else ""


def double_bits(f: float) -> int:
    import struct
    return struct.unpack("<q", struct.pack("<d", f))[0]


def bits_double(b: int) -> float:
    import struct
    return struct.unpack("<d", struct.pack("<q", b))[0]


# The probe side of the chain, as the probe job builds its heat{} block: the r5 functions when present, the r4 inline form otherwise (so a RED run on eb9a08f2 measures that head)
R5_HEAT_BLOCK = (
    "function Get-R5HeatBlock($ar, $meanC) {\n"
    "  $heat = [ordered]@{ source = 'x'; sample_age_s = 1.0; hottest_core_c = 50.0; max15_c = 60.0; mean_c = [math]::Round([double]$meanC, 2); mean_c_exact = (ConvertTo-JsonSafeDouble $meanC); throttled = 0; window_rows = 15; package_w = 10.0; reasons = [ordered]@{} }\n"
    "  if (Get-Command ConvertTo-TelDoubleBits -ErrorAction SilentlyContinue) { $heat['mean_c_exact_bits'] = (ConvertTo-TelDoubleBits $meanC) }\n"
    "  if (Get-Command New-TelHeatRuleBlock -ErrorAction SilentlyContinue) { $heat['rule'] = (New-TelHeatRuleBlock $ar) }\n"
    "  else { $heat['rule'] = [ordered]@{ source = $ar.source; maxThrottledRows = $ar.maxThrottledRows; meanMaxC = (ConvertTo-JsonSafeDouble $ar.meanMaxC); sustainedRows = $ar.sustainedRows; feedStaleSec = (ConvertTo-JsonSafeDouble $ar.feedStaleSec) } }\n"
    "  $heat\n"
    "}\n")


def r5_quiet_funcs() -> str:
    return "\n".join([quiet_fn_ps(n, "", "") for n in ("ConvertTo-JsonSafeDouble", "Read-TelAgentHeatRule", "ConvertTo-IdentToken", "Format-TelNum", "Get-TelReasonSuffix", "Write-TelemetryLines")]
                     + [quiet_fn_opt(n) for n in ("ConvertTo-TelDoubleBits", "New-TelHeatRuleBlock")])


@requires_windows_pwsh
class AgentThresholdParityR5Tests(unittest.TestCase):
    """BACHELOR-THERMAL-GATE-AGENT-THRESHOLD-PARITY-1 r5 (adversary r4 A1, hub ruling: EXACT BITS). Windows PowerShell 5.1's [double]::Parse (and so its ConvertFrom-Json) is not correctly
    rounded: the G15 text 79.3224899793614 of bits 4635282240941762887 parses to ...887 there and ...886 on pwsh 7, so r4's 'G15 gives it back' self-check passed on 5.1 and the threshold left
    as a JSON number that the probe read one ulp low. Every rule double and the exact mean now travel beside their IEEE-754 bits (a JSON integer / a tele-line token), and every reader that
    finds the bits decodes them (Int64BitsToDouble, exact on every shell) and ignores the number/text: no [double]::Parse is on the decision path in any shell."""

    THR_BITS = 4635282240941762887   # the adversary's GateMeanMaxC
    FEED_ROW = "2026,0,0,79.322489979361398,10,No,No"

    def chain(self, exe) -> dict:
        """The adversary's input, end to end in the shell under test, with the project's own functions (lifted, never restated): the agent's feed reader and verdict, its publisher and
        job env, the probe's env reader and feed reader, the probe JSON, the printed line, and the gate on both the line and the raw JSON."""
        script = (
            f"Import-Module '{GATE}' -Force\n"
            + agent_fn_ps("ConvertFrom-HeavySlotFeedRows", "", "") + "\n" + agent_fn_ps("Get-SustainedHeatVerdict", "", "") + "\n"
            + agent_fn_ps("Get-AgentHeatRule", f"$GateMaxThrottledRows = 6; $GateMeanMaxC = [BitConverter]::Int64BitsToDouble({self.THR_BITS}); $GateSustainedRows = 15; $GateFeedStaleSec = 120", "") + "\n"
            "$telemetrySchemaV1 = 'mlv-app/venue-quiet-probe-telemetry/v1'; function Write-Gate([string]$s) { $s }\n" + r5_quiet_funcs() + "\n" + R5_HEAT_BLOCK +
            f"$rows = @(1..15 | ForEach-Object {{ '{self.FEED_ROW}' }})\n"
            "$am = ConvertFrom-HeavySlotFeedRows -Lines $rows\n"
            "$agentSays = (Get-SustainedHeatVerdict -Metrics $am).state\n"                                   # the agent's own verdict on its own feed
            "$envJson = (Get-AgentHeatRule | ConvertTo-Json -Compress)\n"                                    # the job env, as the agent writes it
            "$ar = Read-TelAgentHeatRule -Json $envJson -DefaultRows 60 -DefaultStaleSec 120\n"              # the probe reads it
            "$HeatWindowRows = $ar.windowRows\n"
            + quiet_fn_ps("ConvertFrom-TelFeedRows", "", "") + "\n"
            "$pm = (ConvertFrom-TelFeedRows $rows).m\n"                                                       # the probe's own reading of the same feed
            "$heat = Get-R5HeatBlock $ar $pm.meanC\n"
            "$probeJson = ([ordered]@{ telemetry_schema = $telemetrySchemaV1; heat = $heat; gpu = [ordered]@{ state = 'absent' }; last_lease = [ordered]@{ event = 'none' } } | ConvertTo-Json -Compress -Depth 6)\n"
            "$p = $probeJson | ConvertFrom-Json\n"                                                            # the lease-batch side reads the probe's line
            "$lines = @(Write-TelemetryLines $p)\n"
            "$viaLine = ConvertFrom-VenueProbeHeatLine -Lines (@('  raw UNWRITABLE sharing violation') + $lines)\n"
            "$f = Join-Path ([IO.Path]::GetTempPath()) ('thermal-r5-' + [guid]::NewGuid().ToString('N') + '.json'); [IO.File]::WriteAllText($f, $probeJson)\n"
            "$viaRaw = ConvertFrom-VenueProbeHeatLine -Lines @(\"  raw $f\"); Remove-Item -LiteralPath $f -Force\n"
            "$tl = [string](@($lines | Where-Object { $_ -match '^\\s*tele heat ' })[0])\n"
            "[pscustomobject]@{ agent = $agentSays; agentMeanBits = [string][BitConverter]::DoubleToInt64Bits($am.meanC); probeMeanBits = [string][BitConverter]::DoubleToInt64Bits($pm.meanC)\n"
            "  probeThrBits = [string][BitConverter]::DoubleToInt64Bits([double]$ar.meanMaxC); lineThrBits = [string][BitConverter]::DoubleToInt64Bits([double](& (Get-Module VenueThermalGate) { param($l) Resolve-VenueHeatRule -Rule (Get-VenueRuleFromTeleLine -Line $l) } $tl).meanMaxC)\n"
            "  rawThrBits = [string][BitConverter]::DoubleToInt64Bits([double](Resolve-VenueHeatRule -Rule $p.heat.rule).meanMaxC)\n"
            "  lineMeanBits = [string][BitConverter]::DoubleToInt64Bits([double]$viaLine.meanC); rawMeanBits = [string][BitConverter]::DoubleToInt64Bits([double]$viaRaw.meanC)\n"
            "  line = $viaLine.heat; raw = $viaRaw.heat; src = $viaLine.thresholdsSource; rawSrc = $viaRaw.thresholdsSource; text = $tl; envJson = $envJson }"
        )
        return shell_json(exe, script)

    def test_r5_a1_the_adversarys_input_agent_clear_gate_clear_on_both_shells(self) -> None:
        for exe in SHELLS_R4:
            got = self.chain(exe)
            self.assertEqual(got["agent"], "clear", f"{exe}: the precondition (the agent says clear on its own feed): {got}")
            self.assertEqual(got["probeMeanBits"], got["agentMeanBits"], f"{exe}: the probe reads the agent's feed as the agent does: {got}")
            want = str(self.THR_BITS)
            self.assertEqual((got["probeThrBits"], got["lineThrBits"], got["rawThrBits"]), (want, want, want), f"{exe}: the threshold arrives bit-identical at every hop: {got}")
            self.assertEqual((got["lineMeanBits"], got["rawMeanBits"]), (got["agentMeanBits"], got["agentMeanBits"]), f"{exe}: the exact mean arrives bit-identical: {got}")
            self.assertEqual((got["line"], got["raw"], got["src"], got["rawSrc"]), ("clear", "clear", "agent", "agent"), f"{exe}: agent says clear, the gate must too: {got}")
            # the bits travel beside the human field, on the env, on the probe's line, and in its JSON
            self.assertIn(f'"meanMaxCBits":{self.THR_BITS}', got["envJson"], got["envJson"])
            self.assertIn(f" rule_mean_max_c_bits={self.THR_BITS}", got["text"], got["text"])
            self.assertIn(f" mean_exact_bits={got['agentMeanBits']}", got["text"], got["text"])

    def test_r5_a1_bits_outrank_the_number_and_bad_bits_fail_closed_on_both_shells(self) -> None:
        b80, b88 = double_bits(80.0), double_bits(88.0)
        nan, inf, neg = double_bits(float("nan")), double_bits(float("inf")), double_bits(-1.0)
        base = {"source": "agent", "maxThrottledRows": 6, "meanMaxC": 88, "sustainedRows": 60, "feedStaleSec": 120}
        good = [({**base, "meanMaxCBits": b80}, 80.0, 120.0),                                        # the bits decide; the number beside them is ignored
                ({**base, "meanMaxC": "garbage", "meanMaxCBits": b80}, 80.0, 120.0),
                ({**base, "meanMaxCBits": str(b80)}, 80.0, 120.0),                                     # a digit string (the tele line's token) is bits too
                ({**base, "feedStaleSecBits": double_bits(45.5)}, 88.0, 45.5),
                (base, 88.0, 120.0)]                                                                    # no bits: the number, as before
        bad = [{**base, "meanMaxCBits": "abc"}, {**base, "meanMaxCBits": 1.5}, {**base, "meanMaxCBits": None}, {**base, "meanMaxCBits": [b80]},
               {**base, "meanMaxCBits": True}, {**base, "meanMaxCBits": "9223372036854775808"}, {**base, "meanMaxCBits": " 123"}, {**base, "meanMaxCBits": "4.6e18"},
               {**base, "meanMaxCBits": nan}, {**base, "meanMaxCBits": inf}, {**base, "meanMaxCBits": neg}, {**base, "feedStaleSecBits": double_bits(0.0)},
               {**base, "feedStaleSecBits": "x"}]
        raw_overflow = '{"source":"agent","maxThrottledRows":6,"meanMaxC":88,"sustainedRows":60,"feedStaleSec":120,"meanMaxCBits":9223372036854775808}'   # Decimal (5.1) / BigInteger (7)
        raw_float = '{"source":"agent","maxThrottledRows":6,"meanMaxC":88,"sustainedRows":60,"feedStaleSec":120,"meanMaxCBits":4.635282240941762887e18}'   # a double: never bits
        for exe in SHELLS_R4:
            for rule, mean, stale in good:
                got = shell_json(exe, f"Import-Module '{GATE}' -Force; Resolve-VenueHeatRule -Rule ('{json.dumps(rule)}' | ConvertFrom-Json)")
                self.assertEqual((got["valid"], got["meanMaxC"], got["feedStaleSec"]), (True, mean, stale), f"{exe}: {rule} -> {got}")
            for rule in bad:
                got = shell_json(exe, f"Import-Module '{GATE}' -Force; Resolve-VenueHeatRule -Rule ('{json.dumps(rule)}' | ConvertFrom-Json)")
                self.assertEqual((got["valid"], got["reason"]), (False, "THERMAL_THRESHOLDS_INVALID"), f"{exe}: {rule} -> {got}")
            for text in (raw_overflow, raw_float):
                got = shell_json(exe, f"Import-Module '{GATE}' -Force; Resolve-VenueHeatRule -Rule ('{text}' | ConvertFrom-Json)")
                self.assertEqual(got["valid"], False, f"{exe}: {text} -> {got}")
            # the probe's env reader: the same rule
            for rule, mean, stale in good:
                env = {k: v for k, v in rule.items() if k != "source"}
                got = shell_json(exe, quiet_fn_ps("Read-TelAgentHeatRule", "", f"Read-TelAgentHeatRule -Json '{json.dumps(env)}' -DefaultRows 60 -DefaultStaleSec 120"))
                self.assertEqual((got["source"], got["meanMaxC"], got["feedStaleSec"]), ("agent", mean, stale), f"{exe}: {env} -> {got}")
            for rule in bad:
                env = {k: v for k, v in rule.items() if k != "source"}
                got = shell_json(exe, quiet_fn_ps("Read-TelAgentHeatRule", "", f"Read-TelAgentHeatRule -Json '{json.dumps(env)}' -DefaultRows 60 -DefaultStaleSec 120"))
                self.assertEqual(got["source"], "invalid", f"{exe}: {env} -> {got}")
            for text in (raw_overflow, raw_float):
                got = shell_json(exe, quiet_fn_ps("Read-TelAgentHeatRule", "", f"Read-TelAgentHeatRule -Json '{text}' -DefaultRows 60 -DefaultStaleSec 120"))
                self.assertEqual(got["source"], "invalid", f"{exe}: {text} -> {got}")
            # the tele line: bits tokens outrank the printed number; a junk token is invalid, never a silent fallback to the number
            def line(rule_tail: str, mean_tail: str = "") -> dict:
                ln = f"  tele heat source=x age=1.0s hottest=50.0C max15=60.0C mean=80.0C throttled=0/60 W=10.0{mean_tail} rule_source=agent rule_max_throttled=6 rule_mean_max_c=88 rule_rows=60 rule_stale_s=120{rule_tail}"
                return shell_json(exe, f"Import-Module '{GATE}' -Force; ConvertFrom-VenueProbeHeatLine -Lines @('{ln}')")
            self.assertEqual((line(f" rule_mean_max_c_bits={b80}")["heat"]), "hot", f"{exe}: 80.0 >= the bits' 80")
            self.assertEqual((line("")["heat"]), "clear", f"{exe}: 80.0 < the number's 88")
            for junk in ("abc", "n/a", "1.5", "9223372036854775808", str(nan), str(neg)):
                got = line(f" rule_mean_max_c_bits={junk}")
                self.assertEqual((got["heat"], got["reason"]), ("unknown", "THERMAL_THRESHOLDS_INVALID"), f"{exe}: {junk} -> {got}")
            # the mean's bits outrank mean_exact: mean_exact says 80 (clear under 88), the bits say 88 (hot)
            self.assertEqual(line("", f" mean_exact=80 mean_exact_bits={b88}")["heat"], "hot", str(exe))
            got = line("", " mean_exact=95 mean_exact_bits=abc")   # unreadable bits are no mean (never the text beside them): the count (0) does not decide, so unknown
            self.assertEqual(got["heat"], "unknown", f"{exe}: {got}")

    def test_r5_a1_the_raw_json_mean_bits_outrank_mean_c_exact(self) -> None:
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="thermal-r5-raw-"))
        for exe in SHELLS_R4:
            for name, extra, want in (("bits88", {"mean_c_exact_bits": double_bits(88.0)}, "hot"), ("nobits", {}, "clear"), ("junk", {"mean_c_exact_bits": "abc"}, "unknown"),
                                      ("float", {"mean_c_exact_bits": 4.6e18}, "unknown"), ("inf", {"mean_c_exact_bits": double_bits(float("inf"))}, "hot")):
                p = d / f"{name}.json"
                p.write_text(json.dumps({"heat": {"mean_c": 80.0, "mean_c_exact": 80.0, "throttled": 0, "window_rows": 60, **extra}}), encoding="utf-8")
                got = shell_json(exe, f"Import-Module '{GATE}' -Force; ConvertFrom-VenueProbeHeatLine -Lines @('  raw {p}', '  tele heat source=x age=1.0s hottest=50.0C max15=60.0C mean=n/a throttled=n/a/n/a W=10.0')")
                self.assertEqual(got["heat"], want, f"{exe}: {name} -> {got}")

    def test_r5_a3_a_non_scalar_source_is_not_agent(self) -> None:
        # adversary r4 A3: [string] coerced a one-element array ["agent"] to "agent"; the source must BE the string agent
        for exe in SHELLS_R4:
            got = shell_json(exe, f"Import-Module '{GATE}' -Force; Resolve-VenueHeatRule -Rule ('{{\"source\":[\"agent\"],\"maxThrottledRows\":6,\"meanMaxC\":88,\"sustainedRows\":60,\"feedStaleSec\":120}}' | ConvertFrom-Json)")
            self.assertEqual((got["valid"], got["reason"]), (False, "THERMAL_THRESHOLDS_INVALID"), f"{exe}: {got}")
            funcs = "\n".join(quiet_fn_ps(n, "", "") for n in ("ConvertTo-IdentToken", "Format-TelNum", "Get-TelReasonSuffix", "Write-TelemetryLines"))
            line = shell_json(exe, "$telemetrySchemaV1 = 'mlv-app/venue-quiet-probe-telemetry/v1'; function Write-Gate([string]$s) { $s }\n" + funcs + "\n"
                              "$p = '{\"telemetry_schema\":\"mlv-app/venue-quiet-probe-telemetry/v1\",\"heat\":{\"source\":\"x\",\"mean_c\":80.0,\"mean_c_exact\":80.0,\"throttled\":0,\"window_rows\":60,\"reasons\":{},"
                              "\"rule\":{\"source\":[\"agent\"],\"maxThrottledRows\":6,\"meanMaxC\":88,\"sustainedRows\":60,\"feedStaleSec\":120}},\"gpu\":{\"state\":\"absent\"},\"last_lease\":{\"event\":\"none\"}}' | ConvertFrom-Json\n"
                              "[string]@(Write-TelemetryLines $p)[0]")
            self.assertTrue(line.endswith(" rule_source=invalid"), f"{exe}: {line}")

    def test_r5_pass_a3_an_array_rule_or_array_fields_print_as_invalid_on_both_shells(self) -> None:
        # adversary r5 pass A3: the printer scalarized rule=[{..}] and fields [3]/[80] into a line the gate applied (hot, thresholdsSource=agent); the structured reader refuses both
        funcs = "\n".join(quiet_fn_ps(n, "", "") for n in ("ConvertTo-IdentToken", "Format-TelNum", "Get-TelReasonSuffix", "Write-TelemetryLines"))
        rule_obj = '{"source":"agent","maxThrottledRows":3,"meanMaxC":80,"sustainedRows":60,"feedStaleSec":120}'
        rule_arr_fields = '{"source":"agent","maxThrottledRows":[3],"meanMaxC":[80],"sustainedRows":[60],"feedStaleSec":[120]}'
        rule_arr_bits = '{"source":"agent","maxThrottledRows":3,"meanMaxC":88,"sustainedRows":60,"feedStaleSec":120,"meanMaxCBits":[' + str(double_bits(80.0)) + ']}'
        for exe in SHELLS_R4:
            for rule in ("[" + rule_obj + "]", rule_arr_fields, rule_arr_bits):
                probe = ('{"telemetry_schema":"mlv-app/venue-quiet-probe-telemetry/v1","heat":{"source":"x","mean_c":82,"mean_c_exact":82,"throttled":4,"window_rows":60,"reasons":{},'
                         '"rule":' + rule + '},"gpu":{"state":"absent"},"last_lease":{"event":"none"}}')
                got = shell_json(exe, f"Import-Module '{GATE}' -Force; $telemetrySchemaV1 = 'mlv-app/venue-quiet-probe-telemetry/v1'; function Write-Gate([string]$s) {{ $s }}\n" + funcs + "\n"
                                 f"$p = '{probe}' | ConvertFrom-Json\n"
                                 "$r = ConvertFrom-VenueProbeHeatLine -Lines (@('  raw UNWRITABLE x') + @(Write-TelemetryLines $p)); [pscustomobject]@{ heat = $r.heat; src = $r.thresholdsSource; reason = $r.reason }")
                self.assertEqual((got["heat"], got["src"], got["reason"]), ("unknown", "invalid", "THERMAL_THRESHOLDS_INVALID"), f"{exe}: {rule} -> {got}")

    def test_r5_pass_a4_a_present_but_empty_token_is_refused_never_read_as_absent(self) -> None:
        # adversary r5 pass A4: \S+ read an empty rule_source= / *_bits= / mean_exact_bits= as absent (defaults, the human threshold, the mean_exact text)
        rule = " rule_source=agent rule_max_throttled=6 rule_mean_max_c=88 rule_rows=60 rule_stale_s=120"
        for exe in SHELLS_R4:
            def line(tail: str, thr: int = 0, mean: str = "82C") -> dict:
                return shell_json(exe, f"Import-Module '{GATE}' -Force; ConvertFrom-VenueProbeHeatLine -Lines @('  tele heat source=x mean={mean} throttled={thr}/60{tail}')")
            got = line(" rule_source= rule_max_throttled=3 rule_mean_max_c=80 rule_rows=60 rule_stale_s=120", thr=4)
            self.assertEqual((got["heat"], got["thresholdsSource"], got["reason"]), ("unknown", "invalid", "THERMAL_THRESHOLDS_INVALID"), f"{exe}: {got}")
            for tail in (rule + " rule_mean_max_c_bits=", rule + " rule_stale_s_bits=", rule + " rule_mean_max_c_bits= rule_stale_s_bits=" + str(double_bits(120.0))):
                got = line(tail)
                self.assertEqual((got["heat"], got["reason"]), ("unknown", "THERMAL_THRESHOLDS_INVALID"), f"{exe}: {tail} -> {got}")
            got = line(" mean_exact=95 mean_exact_bits=")   # an empty mean token is no mean, never the 95 beside it: the count (0) does not decide
            self.assertEqual(got["heat"], "unknown", f"{exe}: {got}")

    def test_r5_a1_no_double_parse_decides_when_the_bits_are_present(self) -> None:
        # the publisher, the probe and the gate carry/read the bits; the r4 G15 self-check only shapes the human field now
        agent = AGENT.read_text(encoding="utf-8")
        self.assertIn("meanMaxCBits", agent)
        self.assertIn("[BitConverter]::DoubleToInt64Bits", agent)
        quiet = QUIET.read_text(encoding="utf-8")
        for token in ("mean_c_exact_bits", "rule_mean_max_c_bits=", "rule_stale_s_bits=", "mean_exact_bits=", "[BitConverter]::Int64BitsToDouble"):
            self.assertIn(token, quiet)
        gate = GATE.read_text(encoding="utf-8")
        for token in ("rule_mean_max_c_bits", "rule_stale_s_bits", "mean_exact_bits", "mean_c_exact_bits", "[BitConverter]::Int64BitsToDouble"):
            self.assertIn(token, gate)

    # ---- the sweep: >= 12000 seeded finite non-negative doubles, the 70-100 band and every value's bit-neighbours, through the whole chain on both shells
    SWEEP_SCRIPT = (
        "param([string]$In, [string]$Out)\n"
        "$ErrorActionPreference = 'Stop'\n"
        "Import-Module '{gate}' -Force\n"
        "{agentfn}\n"
        "$telemetrySchemaV1 = 'mlv-app/venue-quiet-probe-telemetry/v1'; function Write-Gate([string]$s) {{ $s }}\n"
        "{quietfns}\n{block}\n"
        "$bad = [Collections.Generic.List[string]]::new()\n"
        "foreach ($tok in [IO.File]::ReadAllLines($In)) {{\n"
        "  $want = [long]::Parse($tok, [Globalization.CultureInfo]::InvariantCulture); $x = [BitConverter]::Int64BitsToDouble($want)\n"
        "  $GateMaxThrottledRows = 6; $GateMeanMaxC = $x; $GateSustainedRows = 60; $GateFeedStaleSec = 120\n"
        "  $envJson = (Get-AgentHeatRule | ConvertTo-Json -Compress)\n"
        "  $ar = Read-TelAgentHeatRule -Json $envJson -DefaultRows 60 -DefaultStaleSec 120\n"
        "  $heat = Get-R5HeatBlock $ar $x\n"
        "  $probeJson = ([ordered]@{{ telemetry_schema = $telemetrySchemaV1; heat = $heat; gpu = [ordered]@{{ state = 'absent' }}; last_lease = [ordered]@{{ event = 'none' }} }} | ConvertTo-Json -Compress -Depth 6)\n"
        "  $p = $probeJson | ConvertFrom-Json\n"
        "  $tl = [string](@(Write-TelemetryLines $p)[0])\n"
        "  $lr = & (Get-Module VenueThermalGate) {{ param($l) Resolve-VenueHeatRule -Rule (Get-VenueRuleFromTeleLine -Line $l) }} $tl; $rr = Resolve-VenueHeatRule -Rule $p.heat.rule\n"
        "  $lm = (ConvertFrom-VenueProbeHeatLine -Lines @('  raw UNWRITABLE x', $tl)).meanC\n"
        "  $got = @($ar.meanMaxC, $lr.meanMaxC, $rr.meanMaxC, $lm) | ForEach-Object {{ if ($null -eq $_) {{ 'null' }} else {{ [string][BitConverter]::DoubleToInt64Bits([double]$_) }} }}\n"
        "  foreach ($g in $got) {{ if ($g -cne $tok) {{ $bad.Add($tok + ' -> ' + ($got -join ',')); break }} }}\n"
        "}}\n"
        "[IO.File]::WriteAllLines($Out, [string[]]@('COUNT ' + [IO.File]::ReadAllLines($In).Count) + $bad.ToArray())\n"
    )

    @staticmethod
    def sweep_values() -> list[int]:
        import random
        rng = random.Random(20261112)
        top = double_bits(1.7976931348623157e308)
        centers = [rng.randrange(0, top + 1) for _ in range(3000)]
        centers += [double_bits(rng.uniform(70.0, 100.0)) for _ in range(1000)]
        centers += [double_bits(v) for v in (0.0, 5e-324, 1.7976931348623157e308, 88.0, 80.00000000000001, 0.84551240822557006, 79.322489979361398, 70.0, 100.0, 0.1)]
        centers.append(4635282240941762887)
        out = []
        for c in centers:
            for n in (c - 1, c, c + 1):
                if 0 <= n <= top and n not in out:
                    out.append(n)
        return out

    def test_r5_a1_the_sweep_arrives_bit_identical_on_both_shells(self) -> None:
        import tempfile
        from concurrent.futures import ThreadPoolExecutor
        vals = self.sweep_values()
        self.assertGreaterEqual(len(vals), 12000)
        self.assertGreaterEqual(sum(1 for v in vals if 70.0 <= bits_double(v) <= 100.0), 3000, "the 70-100 band (and its neighbours) is in the sweep")
        d = Path(tempfile.mkdtemp(prefix="thermal-r5-sweep-"))
        script = d / "sweep.ps1"
        script.write_text(self.SWEEP_SCRIPT.format(gate=GATE, agentfn=agent_fn_ps("Get-AgentHeatRule", "", ""), quietfns=r5_quiet_funcs(), block=R5_HEAT_BLOCK), encoding="utf-8-sig")
        parts = 6
        jobs = []
        for k, exe in enumerate(SHELLS_R4):
            for i in range(parts):
                inp = d / f"in-{k}-{i}.txt"
                inp.write_text("\n".join(str(v) for v in vals[i::parts]) + "\n", encoding="ascii")
                jobs.append((exe, inp, inp.with_suffix(".out")))

        def run(job):
            exe, inp, out = job
            r = subprocess.run([str(exe), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script), "-In", str(inp), "-Out", str(out)],
                               capture_output=True, text=True, timeout=1500)
            return exe, r, out

        with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
            results = list(ex.map(run, jobs))
        total = {}
        for exe, r, out in results:
            self.assertEqual(r.returncode, 0, f"{exe}: {r.stdout}{r.stderr}")
            lines = out.read_text(encoding="utf-8-sig").splitlines()
            self.assertTrue(lines[0].startswith("COUNT "), lines[:3])
            total[str(exe)] = total.get(str(exe), 0) + int(lines[0].split()[1])
            self.assertEqual(lines[1:], [], f"{exe}: values that did not arrive bit-identical (want -> env reader, line rule, raw rule, line mean): {lines[1:20]}")
        for exe in SHELLS_R4:
            self.assertEqual(total[str(exe)], len(vals), str(exe))


R6_SCRIPT = (
    "param([string]$In)\n"
    "$ErrorActionPreference = 'Stop'\n"
    "Import-Module '{gate}' -Force\n"
    "$telemetrySchemaV1 = 'mlv-app/venue-quiet-probe-telemetry/v1'; function Write-Gate([string]$s) {{ $s }}\n"
    "{quietfns}\n"
    "$tmp = Join-Path ([IO.Path]::GetTempPath()) ('thermal-r6-' + [guid]::NewGuid().ToString('N') + '.json')\n"
    "function Read-Both($lines) {{ $h = ConvertFrom-VenueProbeHeatLine -Lines $lines; $h.heat + '/' + $h.thresholdsSource }}\n"
    "$all = [IO.File]::ReadAllText($In, [Text.Encoding]::UTF8) | ConvertFrom-Json\n"   # assigned first: 5.1 writes a root array to the pipeline as ONE object, so @(... | ConvertFrom-Json) would wrap it
    "$out = foreach ($c in $all) {{\n"
    "  if ($c.kind -ceq 'gate') {{ $g = Resolve-VenueHeatRule -Rule ($c.text | ConvertFrom-Json); if ($g.valid) {{ 'valid:' + $g.maxThrottledRows + ':' + $g.sustainedRows }} else {{ 'invalid' }} }}\n"
    "  elseif ($c.kind -ceq 'probe') {{ $q = Read-TelAgentHeatRule -Json $c.text -DefaultRows 60 -DefaultStaleSec 120; $q.source + ':' + $q.windowRows }}\n"
    "  elseif ($c.kind -ceq 'line') {{ Read-Both @($c.text) }}\n"
    "  elseif ($c.kind -ceq 'chain' -or $c.kind -ceq 'probejson') {{\n"
    "    if ($c.kind -ceq 'chain') {{\n"
    # the job env text -> the probe's reader -> its heat{} block (rule block only when not default, as the probe does) -> its JSON -> its printed line -> the gate
    "      $ar = Read-TelAgentHeatRule -Json $c.text -DefaultRows 60 -DefaultStaleSec 120\n"
    "      $heat = [ordered]@{{ source = 'x'; sample_age_s = 1.0; hottest_core_c = 50.0; max15_c = 60.0; mean_c = 82.0; mean_c_exact = 82.0; throttled = 4; window_rows = 60; package_w = 10.0; reasons = [ordered]@{{}} }}\n"
    "      if ($ar.source -cne 'default') {{ $heat['rule'] = (New-TelHeatRuleBlock $ar) }}\n"
    "      $pj = ([ordered]@{{ telemetry_schema = $telemetrySchemaV1; heat = $heat; gpu = [ordered]@{{ state = 'absent' }}; last_lease = [ordered]@{{ event = 'none' }} }} | ConvertTo-Json -Compress -Depth 6)\n"
    "    }} else {{ $pj = [string]$c.text }}\n"
    "    $lines = @(Write-TelemetryLines ($pj | ConvertFrom-Json))\n"
    "    [IO.File]::WriteAllText($tmp, $pj)\n"
    # line = the printed line alone (the raw file unwritable); both = what the runner reads (the raw JSON first, the line as its fallback)
    "    (Read-Both (@('  raw UNWRITABLE x') + $lines)) + '|' + (Read-Both (@(\"  raw $tmp\") + $lines)) + '|' + [string]$lines[0]\n"
    "  }}\n"
    "  else {{ throw ('unknown kind ' + $c.kind) }}\n"
    "}}\n"
    "if (Test-Path -LiteralPath $tmp) {{ Remove-Item -LiteralPath $tmp -Force }}\n"
    "ConvertTo-Json -Compress -InputObject @($out)\n"
)


@requires_windows_pwsh
class AgentThresholdParityR6Tests(unittest.TestCase):
    """BACHELOR-THERMAL-GATE-AGENT-THRESHOLD-PARITY-1 r6 (sol r5 B1, B2). B1: a published rule that is PRESENT but blank (a space, a tab, a line break) is malformed, so it is
    THERMAL_THRESHOLDS_INVALID, never the defaults; only an ABSENT rule ('' -- Windows cannot hold an empty environment variable) selects them. B2: a count (maxThrottledRows, sustainedRows,
    and a reading's throttled / window_rows) is judged on its TOKEN, before any conversion to double: '6.0000000000000001', '0.99999999999999999' and '150.00000000000000001' rounded to
    6 / 1 / 150 and passed. Both shells give a JSON integer token as Int32 / Int64 and a fractional or exponent one as Decimal (5.1) / Double (7), so only an integer type or an
    all-ASCII-digit text is a count. Every case runs in one process per shell, on Windows PowerShell 5.1 and pwsh 7."""

    BASE = {"source": "agent", "maxThrottledRows": 6, "meanMaxC": 88, "sustainedRows": 60, "feedStaleSec": 120}
    BAD_TEXT = ["6.0000000000000001", "0.99999999999999999", "150.00000000000000001", "6.0", "6e0", " 6 ", "6 ", "+6", "-0", "0x6", "٦", "", "6\n"]
    BAD_NUMBER = ["6.0000000000000001", "0.99999999999999999", "150.00000000000000001", "6.0", "6e0", "60.0", "1E2"]
    LINE = "  tele heat source=x mean=82C throttled=4/60 rule_source=agent rule_max_throttled={m} rule_mean_max_c=88 rule_rows={r} rule_stale_s=120"

    def run_cases(self, exe, cases: list) -> list:
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="thermal-r6-"))
        script = d / "r6.ps1"
        funcs = "\n".join(quiet_fn_ps(n, "", "") for n in ("ConvertTo-JsonSafeDouble", "ConvertTo-TelDoubleBits", "New-TelHeatRuleBlock", "Read-TelAgentHeatRule", "ConvertTo-IdentToken",
                                                           "Format-TelNum", "Get-TelReasonSuffix", "Write-TelemetryLines"))
        script.write_text(R6_SCRIPT.format(gate=GATE, quietfns=funcs), encoding="utf-8-sig")
        inp = d / "cases.json"
        inp.write_text(json.dumps([{"kind": k, "text": t} for k, t in cases]), encoding="utf-8")
        r = subprocess.run([str(exe), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script), "-In", str(inp)], capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 0, f"{exe}: {r.stdout}{r.stderr}")
        got = json.loads(r.stdout.strip())
        got = got if isinstance(got, list) else [got]
        self.assertEqual(len(got), len(cases), f"{exe}: {got}")
        return got

    def check(self, cases: list, want: list) -> None:
        for exe in SHELLS_R4:
            got = self.run_cases(exe, cases)
            bad = [(c, g, w) for c, g, w in zip(cases, got, want) if not (g.startswith(w) if w.endswith("|") else g == w)]
            self.assertEqual(bad, [], f"{exe}: (case, got, want)")

    def rule_json(self, field: str, token: str, *, text: bool, source: bool = True) -> str:
        body = {k: v for k, v in self.BASE.items() if source or k != "source"}
        s = json.dumps(body, separators=(",", ":"))
        return s.replace(f'"{field}":{self.BASE[field]}', f'"{field}":' + (json.dumps(token) if text else token))

    # ---- B1: present but blank is invalid, never the defaults; absent is the defaults
    def test_r6_b1_blank_rule_text_is_invalid_and_only_absent_selects_defaults(self) -> None:
        blanks = [" ", "\t", "  \r\n ", "\r\n", " "]
        cases = [("probe", b) for b in blanks] + [("probe", "")]
        want = ["invalid:60"] * len(blanks) + ["default:60"]
        # the whole probe path: the probe prints rule_source=invalid, so the gate reads unknown / THERMAL_THRESHOLDS_INVALID on the line AND on the raw JSON (sol's repro read clear/default)
        cases += [("chain", b) for b in blanks] + [("chain", "")]
        want += ["unknown/invalid|unknown/invalid|"] * len(blanks) + ["clear/default|clear/default|"]
        # the same class in the probe JSON: a rule key that is present but null was published empty, so the printer and the raw reader both refuse it (they read it as the defaults);
        # an absent key is the defaults
        heat = '{"source": "x", "sample_age_s": 1.0, "hottest_core_c": 50.0, "max15_c": 60.0, "mean_c": 82.0, "mean_c_exact": 82.0, "throttled": 4, "window_rows": 60, "package_w": 10.0, "reasons": {}%s}'
        probe = '{"telemetry_schema": "mlv-app/venue-quiet-probe-telemetry/v1", "heat": %s, "gpu": {"state": "absent"}, "last_lease": {"event": "none"}}'
        cases += [("probejson", probe % (heat % ', "rule": null')), ("probejson", probe % (heat % ""))]
        want += ["unknown/invalid|unknown/invalid|", "clear/default|clear/default|"]
        self.check(cases, want)

    # ---- B2: a fractional count is refused before any rounding, in the gate, the probe's reader and the tele line
    def test_r6_b2_fractional_count_text_and_numbers_are_refused_by_both_readers(self) -> None:
        cases, want = [], []
        for field in ("maxThrottledRows", "sustainedRows"):
            for tok in self.BAD_TEXT:
                cases += [("gate", self.rule_json(field, tok, text=True)), ("probe", self.rule_json(field, tok, text=True, source=False))]
                want += ["invalid", "invalid:60"]
            for tok in self.BAD_NUMBER:
                cases += [("gate", self.rule_json(field, tok, text=False)), ("probe", self.rule_json(field, tok, text=False, source=False))]
                want += ["invalid", "invalid:60"]
        self.check(cases, want)

    def test_r6_b2_fractional_count_tokens_on_the_tele_line_are_refused(self) -> None:
        cases, want = [], []
        for tok in ("6.0000000000000001", "0.99999999999999999", "150.00000000000000001", "6.0", "6e0", "+6", "-0", "0x6", "٦"):
            cases += [("line", self.LINE.format(m=tok, r=60)), ("line", self.LINE.format(m=6, r=tok))]
            want += ["unknown/invalid", "unknown/invalid"]
        self.check(cases, want)

    def test_r6_b2_the_valid_neighbours_stay_valid(self) -> None:
        cases = [("gate", self.rule_json("maxThrottledRows", t, text=False)) for t in ("6", "0", "150")]
        want = ["valid:6:60", "valid:0:60", "valid:150:60"]
        cases += [("gate", self.rule_json("sustainedRows", t, text=False)) for t in ("1", "60", "150")]
        want += ["valid:6:1", "valid:6:60", "valid:6:150"]
        cases += [("gate", self.rule_json("maxThrottledRows", "6", text=True)), ("gate", self.rule_json("sustainedRows", "150", text=True)), ("gate", self.rule_json("sustainedRows", "060", text=True))]
        want += ["valid:6:60", "valid:6:150", "valid:6:60"]
        cases += [("probe", self.rule_json("sustainedRows", t, text=False, source=False)) for t in ("1", "60", "150")]
        want += ["agent:1", "agent:60", "agent:150"]
        cases += [("probe", self.rule_json("sustainedRows", "150", text=True, source=False)), ("probe", self.rule_json("maxThrottledRows", "6", text=True, source=False))]
        want += ["agent:150", "agent:60"]
        cases += [("line", self.LINE.format(m=6, r=60)), ("line", self.LINE.format(m=6, r=150)), ("line", self.LINE.format(m=3, r=60))]
        want += ["clear/agent", "clear/agent", "hot/agent"]
        # the whole path with the agent's own published rule: clear under 6 / 88 at throttled 4/60, mean 82
        cases += [("chain", json.dumps({k: v for k, v in self.BASE.items() if k != "source"}))]
        want += ["clear/agent|clear/agent|"]
        self.check(cases, want)
        # sustainedRows out of range stays refused (the integer check does not widen the window)
        self.check([("gate", self.rule_json("sustainedRows", t, text=False)) for t in ("0", "151", "2147483648")], ["invalid"] * 3)

    def test_r6_b2_the_printer_never_rounds_a_count_and_the_raw_reader_agrees_on_both_shells(self) -> None:
        # a probe JSON whose rule count is a fractional token: the printer printed it as 6 (via [double]) on both shells; it prints 'invalid' now, so the line and the raw JSON both refuse
        def probe(rule_extra: dict | None, **heat) -> str:
            h = {"source": "x", "sample_age_s": 1.0, "hottest_core_c": 50.0, "max15_c": 60.0, "mean_c": 82.0, "mean_c_exact": 82.0, "throttled": 4, "window_rows": 60, "package_w": 10.0, "reasons": {}}
            h.update(heat)
            s = json.dumps({"telemetry_schema": "mlv-app/venue-quiet-probe-telemetry/v1", "heat": h, "gpu": {"state": "absent"}, "last_lease": {"event": "none"}})
            if rule_extra is not None:
                s = s.replace('"reasons": {}', '"reasons": {}, "rule": ' + json.dumps(rule_extra))
            return s
        cases, want = [], []
        for field in ("maxThrottledRows", "sustainedRows"):
            for tok in ("6.0000000000000001", "0.99999999999999999", "150.00000000000000001", "6.0"):
                cases.append(("probejson", probe(None).replace('"reasons": {}', '"reasons": {}, "rule": ' + self.rule_json(field, tok, text=False))))
                want.append("unknown/invalid|unknown/invalid|")
        # a reading's own count from a fractional token: pwsh 7 read the Double 4.0 as 4 (clear) and 5.1 its Decimal as no count (unknown); both shells now refuse it on both paths
        for heat in ({"throttled": "__4.0__"}, {"window_rows": "__60.0__"}, {"throttled": "__7.0__"}):
            k, v = next(iter(heat.items()))
            cases.append(("probejson", probe(None).replace(f'"{k}": {4 if k == "throttled" else 60}', f'"{k}": {v.strip("_")}')))
            want.append("unknown/default|unknown/default|")
        # the integer neighbours are read
        cases += [("probejson", probe(None)), ("probejson", probe(None, throttled=7)), ("probejson", probe(dict(self.BASE, maxThrottledRows=3)))]
        want += ["clear/default|clear/default|", "hot/default|hot/default|", "hot/agent|hot/agent|"]
        self.check(cases, want)


@requires_windows_pwsh
class AgentHeavySlotFeedForwardTests(unittest.TestCase):
    """VENUE-PROBE-HEAVY-SLOT-FEED-FORWARD-1 (key3 opus r6 on #800 0b4e4ed3). The agent judges heat on its own $HeavySlotFeed, and the in-job quiet probe (Wait-VenueQuiet.ps1) picks its feed
    from $env:HEAVY_SLOT_FEED, else the default csv. The agent used to put the heat RULE in the job env (PARITY-1) but not the FEED, so an agent started with -HeavySlotFeed X (and no
    HEAVY_SLOT_FEED in its own env) judged X while the job's gate read the default csv and still labelled its thresholds `agent`. The job env must carry the agent's EFFECTIVE feed.
    The agent's own PARITY-1 block (taken from its text between two fixed lines, never restated) runs in a child shell and a grandchild stands for the job."""

    START = "$env:MLV_AGENT_HEAT_RULE = ($heatRule | ConvertTo-Json -Compress)"
    END = "$holderLossLogged = $false"
    DEFAULT_FEED = r"C:\temp\hwinfo-log-adapter.csv"

    def block(self) -> str:
        agent = AGENT.read_text(encoding="utf-8")
        body = agent[agent.index("function Invoke-QueuedJob"):]
        a = body.index(self.START)
        return body[a:body.index(self.END, a)]

    @staticmethod
    def quote(text: str) -> str:
        return "'" + text.replace("'", "''") + "'"

    NORMALIZE_HEAD = "if ([string]::IsNullOrWhiteSpace($HeavySlotFeed)) { $HeavySlotFeed = "

    def normalize_line(self) -> str:
        """The agent's own start-up line that gives an explicit empty/blank -HeavySlotFeed a value (taken from its text, never restated; '' when the agent has no such line)."""
        agent = AGENT.read_text(encoding="utf-8")
        return next((ln.strip() for ln in agent.splitlines() if ln.startswith(self.NORMALIZE_HEAD)), "")

    def long_temp_dir(self, prefix: str) -> Path:
        """A fresh temp dir spelled by its LONG name. A hosted Windows runner's TEMP is C:\\Users\\RUNNER~1\\...; the agent's GetFullPath (and the cwd it is handed) yields the long spelling of the
        same directory, so a fixture path kept in the short spelling would differ from the agent's output as TEXT only. os.path.realpath resolves the 8.3 segments (the same fix as
        test_attr3_footage_stage). The assertions still compare exact strings: a job handed the default csv, or a path that resolves elsewhere, still fails them."""
        import os
        import tempfile
        tmp = tempfile.TemporaryDirectory(prefix=prefix)
        self.addCleanup(tmp.cleanup)
        return Path(os.path.realpath(tmp.name))

    def job_env(self, feed: str, *, agent_env_feed: str | None, tail: str, cwd: str | None = None) -> str:
        """Run the agent's job-env block with $HeavySlotFeed = feed (the agent's own process env holding agent_env_feed as HEAVY_SLOT_FEED, or none, and its current directory `cwd`), then a child
        standing for the job runs `tail`."""
        import os
        env = {k: v for k, v in os.environ.items() if k != "HEAVY_SLOT_FEED"}
        if agent_env_feed is not None:
            env["HEAVY_SLOT_FEED"] = agent_env_feed
        script = ("$ErrorActionPreference = 'Stop'\n"
                  "$heatRule = [ordered]@{ maxThrottledRows = 6; meanMaxC = 88.0; sustainedRows = 60; feedStaleSec = 120 }\n"
                  "$script:Admission = [pscustomobject]@{ label = 'clear'; thermal = 'clear'; waitedSec = 0; heavySlotHeld = $null }\n"
                  f"$HeavySlotFeed = {self.quote(feed)}\n" + self.normalize_line() + "\n" + self.block() + "\n"
                  f"& {self.quote(str(PWSH))} -NoLogo -NoProfile -NonInteractive -Command {self.quote(tail)}\n")
        r = subprocess.run([str(PWSH), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=180, env=env, cwd=cwd)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout.strip()

    def test_the_job_env_carries_the_agents_effective_feed(self) -> None:
        tail = "[Console]::Out.Write([string]$env:HEAVY_SLOT_FEED)"
        # the agent got -HeavySlotFeed explicitly and its own env has no HEAVY_SLOT_FEED: the job must not fall back to the default csv
        self.assertEqual(self.job_env(r"C:\temp\hot.csv", agent_env_feed=None, tail=tail), r"C:\temp\hot.csv")
        # the agent's own env names another feed than the one it runs with (the param wins in the agent): the job gets the one the agent decides on
        self.assertEqual(self.job_env(r"C:\temp\hot.csv", agent_env_feed=r"C:\temp\stale-env.csv", tail=tail), r"C:\temp\hot.csv")
        # the agent on its default feed: the job gets that feed, spelled out
        self.assertEqual(self.job_env(self.DEFAULT_FEED, agent_env_feed=None, tail=tail), self.DEFAULT_FEED)

    def test_the_card_repro_the_gate_reads_the_agents_hot_feed_never_the_default(self) -> None:
        d = self.long_temp_dir("feed-forward-")
        hot = d / "hot.csv"
        hot.write_text("\n".join(["Date,Time,Tick,PackageC,PackageW,CoreThrottling,RingThrottling"]
                                 + [f"10/24/2026,08:00:{i:02d},{i},90.0,40.0,No,No" for i in range(15)]) + "\n", encoding="utf-8")   # 15 rows at 90 C: hot under the default rule (mean >= 88)
        # the agent's own feed reader and verdict decide on the feed path the JOB hands the probe: the probe's ONE feed-path line (taken from Wait-VenueQuiet.ps1) run in the job's env
        feed_line = next(ln.strip() for ln in QUIET.read_text(encoding="utf-8").splitlines() if ln.startswith("$HeatFeedPath = "))
        tail = (agent_fn_ps("ConvertFrom-HeavySlotFeedRows", "", "") + "\n" + agent_fn_ps("Get-SustainedHeatVerdict", "$GateSustainedRows = 60; $GateMaxThrottledRows = 6; $GateMeanMaxC = 88.0", "") + "\n"
                + feed_line + "\n"
                "$m = $null; if (Test-Path -LiteralPath $HeatFeedPath) { $m = ConvertFrom-HeavySlotFeedRows -Lines @(Get-Content -LiteralPath $HeatFeedPath) }\n"
                "[Console]::Out.Write('' + $HeatFeedPath + '|' + (Get-SustainedHeatVerdict -Metrics $m).state)")
        got = self.job_env(str(hot), agent_env_feed=None, tail=tail)
        self.assertEqual(got, f"{hot}|hot", "the job's gate must read the agent's feed (hot), not the default csv")

    def test_a_relative_feed_is_forwarded_as_the_file_the_agent_reads_never_as_a_path_the_job_resolves_elsewhere(self) -> None:
        # adversary r1 A1 (introduced major): the agent opens a relative -HeavySlotFeed under ITS current directory; the job (and the slot holder) start in $Root, so the raw text would name another
        # file (or none) there. The job gets the absolute path the agent itself reads.
        d = self.long_temp_dir("feed-forward-rel-")
        tail = "[Console]::Out.Write([string]$env:HEAVY_SLOT_FEED)"
        self.assertEqual(self.job_env("hot.csv", agent_env_feed=None, tail=tail, cwd=str(d)), str(d / "hot.csv"))
        # an inherited absolute HEAVY_SLOT_FEED does not matter: the effective (relative) param wins in the agent, and it is the absolute form of THAT that the job gets
        self.assertEqual(self.job_env(r"sub\hot.csv", agent_env_feed=r"C:\temp\hot.csv", tail=tail, cwd=str(d)), str(d / "sub" / "hot.csv"))
        # an absolute path (with an apostrophe in it: adversary r1 A2) and a UNC path pass through unchanged
        self.assertEqual(self.job_env(r"C:\O'Brien\hot.csv", agent_env_feed=None, tail=tail, cwd=str(d)), r"C:\O'Brien\hot.csv")
        self.assertEqual(self.job_env(r"\\heat-trace.invalid\share\hwinfo-log-adapter.csv", agent_env_feed=None, tail=tail, cwd=str(d)), r"\\heat-trace.invalid\share\hwinfo-log-adapter.csv")

    def test_an_explicit_empty_or_blank_feed_is_never_forwarded_as_nothing_so_the_job_cannot_fall_back_to_the_default_csv(self) -> None:
        # adversary r2 A1 (introduced major): -HeavySlotFeed '' makes the agent's own reader see no file (feed unknown), but an empty value in the job env is the same as no variable, so the job's
        # gate picked the default csv (a different sensor) and labelled its thresholds `agent`. An empty/blank feed now becomes ONE unreadable local path at start-up, forwarded like any other.
        probe_line = next(ln.strip() for ln in QUIET.read_text(encoding="utf-8").splitlines() if ln.startswith("$HeatFeedPath = "))
        tail = (probe_line + "\n[Console]::Out.Write([string]$env:HEAVY_SLOT_FEED + '|' + $HeatFeedPath + '|' + (Test-Path -LiteralPath $HeatFeedPath))")
        for blank in ("", " ", "   \t"):
            got = self.job_env(blank, agent_env_feed=None, tail=tail)
            fwd, probe_path, exists = got.split("|")
            self.assertNotEqual(fwd, "", f"feed {blank!r} was forwarded as an empty value (the job falls back to the default csv)")
            self.assertNotEqual(fwd.lower(), self.DEFAULT_FEED.lower(), f"feed {blank!r} must not become the default csv")
            self.assertEqual(probe_path, fwd, "the job's gate reads exactly what the agent forwarded")
            self.assertEqual(exists, "False", "the stand-in path is unreadable, so the job reads 'feed missing' (unknown) like the agent does")
            # adversary r2 pass 3 A1: a fixed, well-formed path could be PLANTED as a readable file (and then read as a cool feed): the stand-in carries a character no Win32 file name can hold
            self.assertTrue(any(c in Path(fwd).name for c in '<>"|?*'), f"the stand-in {fwd!r} must be a name no file can have, so it can never be planted")
        # the line runs once, at start-up, before any function (so the agent's own reader and all five forwards below see the same value)
        agent = AGENT.read_text(encoding="utf-8")
        self.assertEqual(agent.count("\n" + self.NORMALIZE_HEAD), 1)
        self.assertLess(agent.index("\n" + self.NORMALIZE_HEAD), agent.index("\nfunction "))

    def test_every_heavy_slot_child_the_agent_starts_forwards_the_feed_it_decides_on(self) -> None:
        # the class: each child launch that reads the heat feed (heavy-slot.py status / lease request / lease renew: they start in the agent's own directory, so the raw path is the one it reads) sets
        # HEAVY_SLOT_FEED from $HeavySlotFeed; the two that start in $Root (the job, and the slot holder) get the absolute path; the job takes it from the process env, set in the PARITY-1 block
        agent = AGENT.read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r'\$si\.Environment\["HEAVY_SLOT_FEED"\] = \$HeavySlotFeed', agent)), 3)
        absolute = "$(try { [System.IO.Path]::GetFullPath($HeavySlotFeed) } catch { $HeavySlotFeed })"
        self.assertEqual(agent.count("HEAVY_SLOT_DIR = $HeavySlotDir; HEAVY_SLOT_FEED = " + absolute + "; HEAVY_SLOT_HELD = $null"), 1)
        self.assertEqual(self.block().count("$env:HEAVY_SLOT_FEED = " + absolute), 1)
        self.assertNotIn("$env:HEAVY_SLOT_FEED = $HeavySlotFeed", agent)


if __name__ == "__main__":
    unittest.main()
