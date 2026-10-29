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
        self.assertIn("$telHeat.mean_c_exact = [double]$r.m.meanC", q)
        self.assertIn("' mean_exact=' + ([double]$h.mean_c_exact).ToString('R'", q)
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
        history({"name": "02-b-cpu-A", "backend": "cpu", "arm": "A", "status": "LEG_FAILED", "jobId": "j2", "heatAtProbe": "unknown"})
        self.assertEqual(run()["heat"], ["clear", "hot"])
        # the probe's own reading is the batch lease's, so it is kept whatever the job's lease
        history({**off, "heatAtProbe": "hot"})
        self.assertEqual(run()["heat"], ["clear", "hot"])

    def test_r6_a3_a_positive_infinity_mean_reaches_the_gate_as_hot_without_the_raw_json(self) -> None:
        # adversary r5 A3 on 09f9f657: a feed of 60 rows '2026,a,b,Infinity,10,No,No' is hot to the agent (+Infinity >= 88). The probe's JSON round trip turns the mean into the string
        # 'Infinity', the display printed 'mean=n/a' with no mean_exact, and with the raw JSON unwritable (a sharing violation) the gate read unknown and ran the leg.
        # The probe's own functions are taken from Wait-VenueQuiet.ps1 (its feed reader, the telemetry line) and its JSON round trip is reproduced; the gate parses what it prints.
        quiet = QUIET.read_text(encoding="utf-8")
        self.assertIn("$telHeat.mean_c = [math]::Round([double]$r.m.meanC, 2); $telHeat.mean_c_exact = [double]$r.m.meanC", quiet, "the probe's heat block moved: update this test")
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

    def test_heat_from_needs_thermal_spread(self) -> None:
        a = self.commit_a
        r = self.run_plan(self.write_plan([self.item(self.specs[0], "A", a)]), "-HeatFrom", str(self.case))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("-HeatFrom is read only by -PlanOrder ThermalSpread", r.stderr)


if __name__ == "__main__":
    unittest.main()
