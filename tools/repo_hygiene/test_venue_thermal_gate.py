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

# The stub thermal source: the fake quiet probe prints a "tele heat" line in Wait-VenueQuiet's format. FAKE_HEAT is a comma list per probe call (the last
# repeats): a throttled row count (mean 70 C), NA (an unreadable feed), or HOT_UNTIL_SKIP (hot until batch.log carries a THERMAL_HOT_SKIPPED line).
HEAT_STUB = r"""
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
    batch_cmd = tvlb.VenueLeaseBatchRunnerTests.batch_cmd
    kinds = tvlb.VenueLeaseBatchRunnerTests.kinds
    batches = tvlb.VenueLeaseBatchRunnerTests.batches
    events_raw = tvlb.VenueLeaseBatchRunnerTests.events_raw

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
        self.assertEqual((b["thermalWaits"], b["thermalSkips"]), (2, 0))
        self.assertEqual(self.log().count(" THERMAL_WAIT leg=01-fx-a-spec "), 2)
        self.assertIn("heat=hot (throttled 9/60, mean 70.0 C)", self.log())

    def test_cap_skip_requeues_the_hot_leg_and_runs_the_next_one_first(self) -> None:
        r = self.run_batch(self.specs[:2], "-ThermalWaitCapSec", "3", FAKE_HEAT="HOT_UNTIL_SKIP")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        legs = [x.split(":")[1] for x in self.kinds() if x.startswith("LEG:")]
        self.assertEqual(legs, ["fx-b", "fx-a"], "fx-a skipped hot and re-queued: fx-b ran first, fx-a after, and neither ran hot")
        [b] = self.batches()
        skip = [l for l in b["legs"] if l["status"] == "THERMAL_HOT_SKIPPED"]
        self.assertEqual(len(skip), 1, b["legs"])
        self.assertEqual((skip[0]["requeued"], skip[0]["outcome"], skip[0]["counted"], skip[0]["heatAtProbe"]), (True, "NOT_RUN", False, "hot"))
        # adversary r1 A9: the skip never waits past the cap (a re-probe that could not end inside it is not started)
        self.assertLessEqual(skip[0]["thermalWaitSec"], 3, skip[0])
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
        r = self.run_batch([self.specs[0]], "-ThermalWaitCapSec", "8", "-ThermalPollSec", "600", "-ThermalRequeueMax", "0", FAKE_HEAT="9,0")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [b] = self.batches()
        self.assertEqual((b["legs"][0]["status"], b["legs"][0]["heatAtLaunch"], b["thermalWaits"], b["thermalSkips"]), ("RAN", "clear", 1, 0), b["legs"])

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
class ThermalSpreadPlanTests(unittest.TestCase):
    stage =tvlb.VenueLeaseBatchLegPlanTests.stage
    item = tvlb.VenueLeaseBatchLegPlanTests.item
    write_plan = tvlb.VenueLeaseBatchLegPlanTests.write_plan
    plan_cmd = tvlb.VenueLeaseBatchLegPlanTests.plan_cmd
    run_plan = tvlb.VenueLeaseBatchLegPlanTests.run_plan
    leg_calls = tvlb.VenueLeaseBatchLegPlanTests.leg_calls
    kinds = tvlb.VenueLeaseBatchRunnerTests.kinds
    batches = tvlb.VenueLeaseBatchRunnerTests.batches

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
        r = self.run_plan(plan, "-PlanOrder", "ThermalSpread", "-HeatFrom", str(prior))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([(c["leaf"], c["sourceCommit"]) for c in self.leg_calls()], [("fx-a", a), ("fx-b", a), ("fx-a", b), ("fx-b", b)])
        kept = json.loads((self.run_dir / "leg-plan.json").read_text(encoding="utf-8"))
        ts = kept["thermalSpread"]
        self.assertEqual((ts["heavyKeys"], ts["heavyAdjacentBefore"], ts["heavyAdjacent"], ts["heatRows"]), (["fx-a|cpu"], 1, 0, 2))
        self.assertEqual(tvlb.batch_result(r.stdout)["legPlan"]["order"], "ThermalSpread")

    def test_heat_from_needs_thermal_spread(self) -> None:
        a = self.commit_a
        r = self.run_plan(self.write_plan([self.item(self.specs[0], "A", a)]), "-HeatFrom", str(self.case))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("-HeatFrom is read only by -PlanOrder ThermalSpread", r.stderr)


if __name__ == "__main__":
    unittest.main()
