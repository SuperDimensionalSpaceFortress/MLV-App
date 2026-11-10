"""PRESENTMON-BACHELOR-CUDA-INTERNAL-GAP-1: the PresentMon "internal gap" on Bachelor CUDA legs was the app's own pre-roll.

Evidence (offline, lane run dir diagnosis.md): on the r4 stalled leg (receipt 1a46f0d6) the 12.7 s "internal" gap sits from 19:44:18.5Z to 19:44:31.2Z,
exactly the Look Assist baseline analysis (gpu_recon set_clip 18.47, look_assist.apply.result 31.09), BEFORE playback_smoke.start (34.62). Inside the app's
own swap window (first_swap_utc 34.724 .. last_swap_utc 59.575) the longest PresentMon gap is 96.5 ms. PresentMon lost nothing: events lost 0, one pid,
monotonic rows, no CSV tail trim. The temporal arm measured over the WHOLE process lifetime (result.json process.startedAtUtc..endedAtUtc), so every
second the app spent loading, analysing or idling after Stop read as a capture gap: 296 of 325 Bachelor CUDA receipts since 2026-11-01 carry a > 5 s gap
(163 head, 124 internal, 9 tail); 12 sampled of them measure 70-184 ms inside the swap window.

  1. Get-AttrCudaPlaybackTemporalCoverage runs Get-AttrCudaTemporalCoverage (no second gap scan) over the app's swap window under BOTH ends of the
     capture-start bracket and keeps the worse; it is 'unavailable' (typed) when the window or a trusted bracket is missing, and the job then falls
     back to the process-lifetime window and says so (presentMonTemporalWindowSource).
  2. The job publishes the window source and the whole-capture gap beside it, so the pre-roll gap is still named, never silent.
  3. Receipt-level guard Get-DvPresentMonGapVerdict: a leg whose PresentMon gap exceeds 5000 ms (the job's own temporal ceiling) is INVALID with a typed
     reason (PRESENTMON_INTERNAL_GAP inside the swap window, PRESENTMON_GAP_UNLOCATED when the gap was measured over the process lifetime,
     PRESENTMON_GAP_UNMEASURED when a usable status carries no gap). A leg with no usable PresentMon status is NOT_APPLICABLE.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from tools.repo_hygiene.test_dual_venue_evidence import EvidenceFactory, requires_windows_pwsh, run_pwsh, run_pwsh_json

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "tools" / "profiling" / "bachelor" / "AttrCudaArtifacts.psm1"
JOB = ROOT / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-job.ps1"
LEG = ROOT / "tools" / "profiling" / "dual-venue" / "Invoke-VenueLeg.ps1"

EARLY = "2026-10-29T19:44:00.0000000Z"
LATE = "2026-10-29T19:44:00.1240000Z"
SWAPS = ("[2026-10-29T19:44:59.617Z] [INFO] [0xeaac] playback_smoke.gpu_window_swaps session=1 window_active=1 telemetry_enabled=1 swaps=602 "
         "swap_fps=24.183 max_gap_ms=96.372 frames_presented=596 head_gap_ms=101.831 tail_gap_ms=41.600 "
         "first_swap_utc=2026-10-29T19:44:34.050Z last_swap_utc=2026-10-29T19:44:59.000Z superseded_before_swap=0")


def stalled_leg_times(drop: tuple[float, float] | None = None) -> list[float]:
    """The r4 stalled leg's shape on PresentMon's clock: a few pre-roll presents, the 12.7 s Look Assist gap, a 3.2 s gap, 25 s of 24 fps playback, a 10 s post-Stop gap."""
    t = [10131.8, 14323.8, 16555.6, 16863.8, 17015.6, 17904.9, 30623.8, 30734.1, 30818.3]
    t += [34068.6 + i * 41.7 for i in range(600)]
    t += [69119.9]
    if drop:
        t = [x for x in t if not (drop[0] < x < drop[1])]
    return t


def coverage(times: list[float], log: str, early: str | None = EARLY, late: str | None = LATE, trusted: str = "1") -> dict:
    script = (f"Import-Module '{MODULE}' -Force\n"
              "$t = [double[]]@($env:PMG_T | ConvertFrom-Json)\n"
              "$e = if ($env:PMG_E) { [datetime]::Parse($env:PMG_E, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind) } else { $null }\n"
              "$l = if ($env:PMG_L) { [datetime]::Parse($env:PMG_L, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind) } else { $null }\n"
              "$r = Get-AttrCudaPlaybackTemporalCoverage -TimeInMsValues $t -LogText $env:PMG_LOG -EarliestAnchorUtc $e -LatestAnchorUtc $l "
              "-LatestAnchorTrusted ($env:PMG_TR -eq '1') -MaxGapMs 5000.0\n"
              "$w = Get-AttrCudaTemporalCoverage -TimeInMsValues $t -WindowStartMs 0 -WindowEndMs 70000 -MaxGapMs 5000.0\n"
              "[ordered]@{ r = $r; whole = $w } | ConvertTo-Json -Compress -Depth 5\n")
    proc = run_pwsh(["-Command", script], env_extra={"PMG_T": json.dumps(times), "PMG_LOG": log, "PMG_E": early or "", "PMG_L": late or "", "PMG_TR": trusted})
    try:
        return json.loads(proc.stdout)
    except ValueError:
        raise AssertionError(proc.stdout + proc.stderr)


@requires_windows_pwsh
class PlaybackWindowCoverageTests(unittest.TestCase):
    def test_the_stalled_leg_shape_reads_its_pre_roll_as_a_whole_capture_gap_but_not_a_playback_gap(self) -> None:
        out = coverage(stalled_leg_times(), SWAPS)
        self.assertFalse(out["whole"]["sufficient"])
        self.assertEqual(out["whole"]["gapKind"], "internal")
        self.assertAlmostEqual(out["whole"]["maxGapMs"], 12718.9, delta=1.0)
        r = out["r"]
        self.assertEqual(r["windowSource"], "app_swap_window", r)
        self.assertTrue(r["sufficient"], r)
        self.assertLess(r["maxGapMs"], 500.0, r)
        self.assertIsNone(r["unavailableReason"])

    def test_a_loss_inside_the_swap_window_still_fails(self) -> None:
        r = coverage(stalled_leg_times(drop=(40000.0, 46500.0)), SWAPS)["r"]
        self.assertEqual(r["windowSource"], "app_swap_window", r)
        self.assertFalse(r["sufficient"], r)
        self.assertEqual(r["gapKind"], "internal", r)
        self.assertGreater(r["maxGapMs"], 6000.0, r)

    def test_a_silent_tail_inside_the_swap_window_fails_as_tail(self) -> None:
        r = coverage(stalled_leg_times(drop=(50000.0, 69000.0)), SWAPS)["r"]
        self.assertFalse(r["sufficient"], r)
        self.assertEqual(r["gapKind"], "tail", r)

    def test_the_worse_end_of_the_bracket_is_the_one_reported(self) -> None:
        # rows start exactly at the window start under the early anchor; a late anchor 1 s later puts the window start 1 s earlier on PresentMon's clock
        times = [34050.0 + i * 41.7 for i in range(598)]
        r = coverage(times, SWAPS, late="2026-10-29T19:44:01.0000000Z")["r"]
        self.assertEqual(r["anchor"], "latest", r)
        self.assertAlmostEqual(r["maxGapMs"], 1000.0, delta=1.0)

    def test_no_swap_window_line_is_typed_unavailable(self) -> None:
        r = coverage(stalled_leg_times(), "[2026-10-29T19:44:59.617Z] [INFO] [0xeaac] playback_smoke.gate session=1 frames_presented=596")["r"]
        self.assertEqual(r["windowSource"], "unavailable", r)
        self.assertIn("gpu_window_swaps", r["unavailableReason"])
        self.assertIsNone(r["sufficient"])
        self.assertIsNone(r["maxGapMs"])

    def test_an_untrusted_late_anchor_is_typed_unavailable(self) -> None:
        r = coverage(stalled_leg_times(), SWAPS, trusted="0")["r"]
        self.assertEqual(r["windowSource"], "unavailable", r)
        self.assertIn("readiness", r["unavailableReason"])

    def test_a_missing_or_inverted_bracket_is_typed_unavailable(self) -> None:
        self.assertEqual(coverage(stalled_leg_times(), SWAPS, early=None, late=None)["r"]["windowSource"], "unavailable")
        self.assertEqual(coverage(stalled_leg_times(), SWAPS, early=LATE, late=EARLY)["r"]["windowSource"], "unavailable")

    def test_an_empty_or_reversed_swap_window_is_typed_unavailable(self) -> None:
        reversed_line = SWAPS.replace("first_swap_utc=2026-10-29T19:44:34.050Z last_swap_utc=2026-10-29T19:44:59.000Z",
                                      "first_swap_utc=2026-10-29T19:44:59.000Z last_swap_utc=2026-10-29T19:44:34.050Z")
        self.assertEqual(coverage(stalled_leg_times(), reversed_line)["r"]["windowSource"], "unavailable")


@requires_windows_pwsh
class AdversaryR1Tests(unittest.TestCase):
    """Adversary review-adversary-...-r1-10d7adaa findings A1-A3, each fed its own input."""

    def test_a1_a_swap_marker_quoted_in_another_records_location_suffix_is_never_the_window(self) -> None:
        log = ("[2026-11-09T00:00:30Z] [INFO] [0x1] playback_smoke.gpu_window_swaps session=1 window_active=1 telemetry_enabled=1 swaps=30 "
               "first_swap_utc=2026-11-09T00:00:01.000Z last_swap_utc=2026-11-09T00:00:30.000Z\n"
               "[2026-11-09T00:00:31Z] [INFO] [0x1] harmless record (C:/b playback_smoke.gpu_window_swaps first_swap_utc=2026-11-08 "
               "last_swap_utc=2026-11-09 x/MainWindow.cpp:1)\n")
        r = coverage([1000.0 * i for i in range(1, 31)], log, early="2026-11-09T00:00:00.0000000Z", late="2026-11-09T00:00:00.0000000Z")["r"]
        self.assertEqual(r["windowSource"], "app_swap_window", r)
        self.assertTrue(r["sufficient"], r)
        self.assertEqual(r["maxGapMs"], 1000.0, r)

    def test_a2_an_endpoint_with_zero_rows_is_never_outvoted_by_a_sufficient_one(self) -> None:
        log = ("playback_smoke.gpu_window_swaps session=1 window_active=1 telemetry_enabled=1 swaps=30 "
               "first_swap_utc=2026-11-09T00:00:01.000Z last_swap_utc=2026-11-09T00:00:02.000Z")
        r = coverage([2000.0 + i for i in range(30)], log, early="2026-11-09T00:00:00.0000000Z", late="2026-11-09T00:00:01.0000000Z")["r"]
        self.assertFalse(r["sufficient"], r)

    def test_a3_the_report_carries_every_target_positive_interval_row_unwindowed_and_the_job_uses_it(self) -> None:
        job = JOB.read_text(encoding="utf-8")
        self.assertIn("$presentMonPlaybackTemporal = Get-AttrCudaPlaybackTemporalCoverage -TimeInMsValues @($displayReport.targetPositiveIntervalTimesMs) ", job)
        module = MODULE.read_text(encoding="utf-8")
        self.assertIn("targetPositiveIntervalTimesMs = @($parsedRows | Where-Object { $_.processId -eq $targetPid -and $null -ne $_.msBetweenDisplayChange "
                      "-and $_.msBetweenDisplayChange -gt 0 } | ForEach-Object { [double]$_.timeInMs })", module)


@requires_windows_pwsh
class AdversaryR2Tests(unittest.TestCase):
    """Adversary review-adversary-...-r2-a63a0dd1 findings A1-A2 (both introduced, major), each fed its own input."""

    EARLY = "2026-11-09T00:00:00.0000000Z"
    GOOD = ("playback_smoke.gpu_window_swaps session=1 window_active=1 telemetry_enabled=1 swaps=30 "
            "first_swap_utc=2026-11-09T00:00:01.000Z last_swap_utc=2026-11-09T00:00:30.000Z")
    EMPTY = "playback_smoke.gpu_window_swaps session=2 window_active=1 telemetry_enabled=1 swaps=0 first_swap_utc= last_swap_utc= superseded_before_paint=0"

    def test_a1_a_last_record_without_stamps_is_unavailable_never_an_earlier_sessions_window(self) -> None:
        r = coverage([1000.0 * i for i in range(1, 31)], self.GOOD + "\n" + self.EMPTY, early=self.EARLY, late=self.EARLY)["r"]
        self.assertEqual(r["windowSource"], "unavailable", r)
        self.assertIsNone(r["sufficient"], r)

    def test_a1_stamps_quoted_in_the_last_records_location_suffix_are_never_its_window(self) -> None:
        log = self.EMPTY + " (C:/build first_swap_utc=2026-11-08 last_swap_utc=2026-11-09 x/MainWindow.cpp:31938)"
        r = coverage([1000.0 * i for i in range(1, 31)], log, early=self.EARLY, late=self.EARLY)["r"]
        self.assertEqual(r["windowSource"], "unavailable", r)

    def test_a2_a_gap_the_bracket_permits_between_the_endpoints_is_never_admitted(self) -> None:
        log = ("playback_smoke.gpu_window_swaps session=1 window_active=1 telemetry_enabled=1 swaps=162 "
               "first_swap_utc=2026-11-09T00:01:00.000Z last_swap_utc=2026-11-09T00:01:25.000Z")
        times = [38000.0 + 250.0 * i for i in range(81)] + [65000.0 + 250.0 * i for i in range(81)]
        r = coverage(times, log, early=self.EARLY, late="2026-11-09T00:00:22.0000000Z")["r"]
        # An origin 11 s into the bracket puts a 7 s hole inside the window: sufficient must be false (or the arm unavailable, never a pass).
        self.assertFalse(r.get("sufficient"), r)
        if r["windowSource"] == "app_swap_window":
            self.assertGreaterEqual(r["maxGapMs"], 7000.0, r)

    def test_a2_the_real_width_bracket_still_passes_the_healthy_leg(self) -> None:
        r = coverage(stalled_leg_times(), SWAPS)["r"]
        self.assertEqual(r["windowSource"], "app_swap_window", r)
        self.assertTrue(r["sufficient"], r)


class JobWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.job = JOB.read_text(encoding="utf-8")

    def test_the_job_embeds_and_calls_the_playback_window_coverage(self) -> None:
        # Its own bracketed splice (the dual-venue byte-identity strip removes the card's text with the region), not a name in the base -Name list.
        self.assertIn("(Get-AttrCudaEmbeddedFunctionSource -Name @(\n    'ConvertFrom-AttrCudaSwapStampUtc',\n    'Get-AttrCudaPlaybackTemporalCoverage'\n))", self.job)
        self.assertIn("# PRESENTMON-BACHELOR-CUDA-INTERNAL-GAP-1 >>>", self.job)
        self.assertIn("$presentMonPlaybackTemporal = Get-AttrCudaPlaybackTemporalCoverage -TimeInMsValues @($displayReport.targetPositiveIntervalTimesMs) "
                      "-LogText $rawLog -EarliestAnchorUtc $presentMonCaptureStartUtc -LatestAnchorUtc $presentMonPostSpawnUtc "
                      "-LatestAnchorTrusted $presentMonTraceReadyVerified -MaxGapMs $presentMonSufficiencyMaxGapMs", self.job)
        # The base process-lifetime line stays verbatim; the window result overrides it only when the window exists, inside the card's region.
        self.assertIn("if ($presentMonTemporalWindowSource -ceq 'app_swap_window') { $presentMonTemporal = $presentMonPlaybackTemporal }", self.job)
        self.assertIn("$presentMonCaptureTemporal = $presentMonTemporal\n", self.job)

    def test_the_job_publishes_the_window_source_and_the_whole_capture_gap(self) -> None:
        for field in ("presentMonTemporalWindowSource = $presentMonTemporalWindowSource",
                      "presentMonCaptureTemporalMaxGapMs = $presentMonCaptureTemporal.maxGapMs",
                      "presentMonCaptureTemporalGapKind = $presentMonCaptureTemporal.gapKind"):
            self.assertIn(field, self.job)

    def test_the_module_exports_the_function(self) -> None:
        exports = MODULE.read_text(encoding="utf-8").split("Export-ModuleMember", 1)[1]
        self.assertIn("Get-AttrCudaPlaybackTemporalCoverage", exports)


def verdict(metrics: dict, backend: str | None = "cuda") -> dict:
    return verdicts([metrics], [backend])[0]


def verdicts(metrics: list[dict], backends: list[str | None] | None = None) -> list[dict]:
    """Get-DvPresentMonGapVerdict over several metrics blocks in ONE pwsh process (backend 'cuda' unless given; None passes $null)."""
    cases = [{"m": m, "b": b} for m, b in zip(metrics, backends or ["cuda"] * len(metrics))]
    proc, out = run_pwsh_json("$o = foreach ($c in @($env:PMG_M | ConvertFrom-Json)) { Get-DvPresentMonGapVerdict -Metrics $c.m -Backend $c.b }\n"
                              "ConvertTo-Json -InputObject @($o) -Compress -Depth 4", PMG_M=json.dumps(cases))
    if out is None or len(out) != len(metrics):
        raise AssertionError(proc.stdout + proc.stderr)
    return out


@requires_windows_pwsh
class ReceiptGapGuardTests(unittest.TestCase):
    def test_the_r4_stalled_leg_metrics_are_unlocated_and_block(self) -> None:
        v = verdict({"presentMonStatus": "degraded", "presentMonTemporalMaxGapMs": 12718.931099999998, "presentMonTemporalGapKind": "internal"})
        self.assertEqual(v["verdict"], "PRESENTMON_GAP_UNLOCATED", v)
        self.assertTrue(v["blocks"])
        self.assertEqual(v["boundMs"], 5000.0)
        self.assertIn("12718.9", v["reason"])

    def test_a_gap_inside_the_swap_window_over_the_bound_blocks(self) -> None:
        v = verdict({"presentMonStatus": "degraded", "presentMonTemporalMaxGapMs": 6000.5, "presentMonTemporalGapKind": "internal",
                     "presentMonTemporalWindowSource": "app_swap_window"})
        self.assertEqual(v["verdict"], "PRESENTMON_INTERNAL_GAP", v)
        self.assertTrue(v["blocks"])
        self.assertIn("6000.5 ms internal", v["reason"])
        self.assertIn("5000 ms", v["reason"])

    def test_a_gap_at_or_under_the_bound_does_not_block(self) -> None:
        for gap in (183.7, 5000.0):
            v = verdict({"presentMonStatus": "ok", "presentMonTemporalMaxGapMs": gap, "presentMonTemporalGapKind": "tail",
                         "presentMonTemporalWindowSource": "app_swap_window"})
            self.assertEqual(v["verdict"], "WITHIN_BOUND", v)
            self.assertFalse(v["blocks"])

    def test_no_usable_presentmon_status_is_not_applicable(self) -> None:
        for m in ({"presentMonStatus": "unavailable", "presentMonTemporalMaxGapMs": 40000.0}, {}, {"presentMonStatus": None}):
            v = verdict(m)
            self.assertEqual(v["verdict"], "NOT_APPLICABLE", v)
            self.assertFalse(v["blocks"])

    def test_a_usable_status_without_a_gap_blocks_as_unmeasured(self) -> None:
        for gap in (None, "x", -1.0):
            v = verdict({"presentMonStatus": "ok", "presentMonTemporalMaxGapMs": gap, "presentMonTemporalWindowSource": "app_swap_window"})
            self.assertEqual(v["verdict"], "PRESENTMON_GAP_UNMEASURED", v)
            self.assertTrue(v["blocks"])

    def test_r4_78b6b9b2_a1_presentmon_never_gates_a_cpu_leg(self) -> None:
        # adversary review-adversary-...-r4-78b6b9b2 A1 (introduced, major): sol's literal cpu metrics (process-lifetime fallback, 10 s head gap)
        sol = {"presentMonStatus": "degraded", "presentMonTemporalWindowSource": "process_lifetime", "presentMonTemporalMaxGapMs": 10000,
               "presentMonTemporalGapKind": "head", "presentMonTemporalAppSwapMaxGapMs": None, "presentMonTemporalOverBoundGapCount": None}
        stall = {"presentMonStatus": "degraded", "presentMonTemporalMaxGapMs": 21359.5, "presentMonTemporalGapKind": "internal",
                 "presentMonTemporalWindowSource": "app_swap_window", "presentMonTemporalAppSwapMaxGapMs": 21359.466, "presentMonTemporalOverBoundGapCount": 1}
        unmeasured = {"presentMonStatus": "ok", "presentMonTemporalMaxGapMs": None, "presentMonTemporalWindowSource": "app_swap_window"}
        got = verdicts([sol, stall, unmeasured, sol, sol, sol, sol], ["cpu", "cpu", "cpu", "cuda", None, "", "CPU"])
        for v in got[:3]:
            self.assertEqual(v["verdict"], "NOT_APPLICABLE", v)
            self.assertFalse(v["blocks"], v)
            self.assertFalse(v["failsLeg"], v)
            self.assertIn("cpu backend", v["reason"])
        # a cuda leg, and a backend that is missing or not spelled exactly 'cpu', are still judged (fail closed)
        for v in got[3:]:
            self.assertEqual(v["verdict"], "PRESENTMON_GAP_UNLOCATED", v)
            self.assertTrue(v["blocks"], v)

    def test_the_backend_is_mandatory(self) -> None:
        proc, out = run_pwsh_json("$m = [pscustomobject]@{ presentMonStatus = 'ok' }\n"
                                  "try { $null = Get-DvPresentMonGapVerdict -Metrics $m -ErrorAction Stop; 'null' } catch { ConvertTo-Json 'REFUSED' }")
        self.assertEqual(out, "REFUSED", proc.stdout + proc.stderr)

    def test_the_bound_is_the_jobs_own_temporal_ceiling(self) -> None:
        job = JOB.read_text(encoding="utf-8")
        ceiling = float(re.search(r"^\$presentMonSufficiencyMaxGapMs = ([\d.]+)$", job, re.M).group(1))
        self.assertEqual(verdict({})["boundMs"], ceiling)


class ReceiptGuardWiringTests(unittest.TestCase):
    def test_invoke_venue_leg_records_the_verdict_and_fails_closed(self) -> None:
        leg = LEG.read_text(encoding="utf-8")
        self.assertIn("$presentMonGapVerdict = Get-DvPresentMonGapVerdict -Metrics $receipt.metrics -Backend $Backend", leg)
        # (adversary r4 78b6b9b2 A1: the leg's backend is resolved from the spec before the gate, so a cpu leg never reaches it with '')
        self.assertLess(leg.index("if ([string]::IsNullOrWhiteSpace($Backend)) { $Backend = [string]@($spec.backends)[0] }"),
                        leg.index("$presentMonGapVerdict = Get-DvPresentMonGapVerdict"))
        self.assertIn("$receipt['presentMonGap'] = $presentMonGapVerdict", leg)
        self.assertIn("if ($outcome -in @('PASS', 'FAIL') -and $presentMonGapVerdict.blocks) {", leg)
        gate = leg.index("if ($outcome -in @('PASS', 'FAIL') -and $presentMonGapVerdict.blocks) {")
        self.assertIn("$outcome = 'INVALID'", leg[gate:gate + 600])
        self.assertLess(leg.index("$receipt.metrics = Get-DvVerbatimMetrics"), leg.index("$presentMonGapVerdict = Get-DvPresentMonGapVerdict"))


def swap_stamps(values: list[str]) -> list[object]:
    """ConvertFrom-AttrCudaSwapStampUtc on each raw stamp, in ONE pwsh process: 'KIND=<kind> <ISO round-trip>' of the instant, or None."""
    script = (f"Import-Module '{MODULE}' -Force\n"
              "foreach ($v in @($env:PMG_S | ConvertFrom-Json)) { $d = ConvertFrom-AttrCudaSwapStampUtc -Value ([string]$v).Substring(2)\n"
              "  if ($null -eq $d) { 'NULL' } else { 'KIND=' + $d.Kind + ' ' + $d.ToString('o') } }\n")
    # ("s:" keeps ConvertFrom-Json from turning a bare ISO string into a DateTime before the reader sees the raw text)
    proc = run_pwsh(["-Command", script], env_extra={"PMG_S": json.dumps(["s:" + v for v in values])})
    out = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if len(out) != len(values):
        raise AssertionError(proc.stdout + proc.stderr)
    return [None if o == "NULL" else o for o in out]


def coverages(cases: list[tuple[list[float], str, str, str]]) -> list[dict]:
    """Get-AttrCudaPlaybackTemporalCoverage over several (times, log, early, late) cases (late end trusted) in ONE pwsh process (a pwsh start and a
    module import dominate a case's cost). The cases go through a file: a row list is larger than one environment variable may be."""
    import tempfile
    with tempfile.TemporaryDirectory(prefix="pmg-cov-") as tmp:
        path = Path(tmp) / "cases.json"
        # ("s:" keeps ConvertFrom-Json from turning a bare ISO string into a DateTime, whose [string] form drops the milliseconds)
        path.write_text(json.dumps([{"t": t, "log": log, "e": "s:" + e, "l": "s:" + l} for t, log, e, l in cases]), encoding="utf-8")
        script = (f"Import-Module '{MODULE}' -Force\n"
                  "$rt = [Globalization.DateTimeStyles]::RoundtripKind; $ic = [Globalization.CultureInfo]::InvariantCulture\n"
                  "$out = foreach ($c in @(Get-Content -LiteralPath $env:PMG_CASES -Raw | ConvertFrom-Json)) {\n"
                  "  Get-AttrCudaPlaybackTemporalCoverage -TimeInMsValues ([double[]]@($c.t)) -LogText ([string]$c.log) "
                  "-EarliestAnchorUtc ([datetime]::Parse(([string]$c.e).Substring(2), $ic, $rt)) -LatestAnchorUtc ([datetime]::Parse(([string]$c.l).Substring(2), $ic, $rt)) "
                  "-LatestAnchorTrusted $true -MaxGapMs 5000.0 }\n"
                  "ConvertTo-Json -InputObject @($out) -Compress -Depth 5\n")
        proc = run_pwsh(["-Command", script], env_extra={"PMG_CASES": str(path)})
    try:
        got = json.loads(proc.stdout)
    except ValueError:
        raise AssertionError(proc.stdout + proc.stderr)
    if len(got) != len(cases):
        raise AssertionError(proc.stdout + proc.stderr)
    return got


@requires_windows_pwsh
class StrictSwapStampTests(unittest.TestCase):
    """PR #788 r1 sol B1 (= opus/fable P-02 hardening): the swap-window stamps are read ONLY in the writer's shape, Qt::ISODateWithMs in UTC
    (GpuDisplayWindow.cpp:319, yyyy-MM-ddTHH:mm:ss.fffZ). Anything else is typed unavailable, never a shortened or widened window."""

    EARLY = "2026-11-09T23:59:00.0000000Z"
    ROWS = [30000.0 + 1000.0 * i for i in range(31)]

    def record(self, first: str, last: str) -> str:
        return ("playback_smoke.gpu_window_swaps session=1 window_active=1 telemetry_enabled=1 swaps=60 "
                f"first_swap_utc={first} last_swap_utc={last}")

    SHAPES = (("2026-11-09T23:59:30.000Z", "2026-11-10T00:00"),                        # sol's literal input: minute-truncated last stamp
              ("2026-11-09T23:59:30.000", "2026-11-10T00:00:29.000"),                  # zone-less
              ("2026-11-09", "2026-11-10"),                                            # date-only
              ("2026-11-09T23:59:30Z", "2026-11-10T00:00:29Z"),                        # no milliseconds
              ("2026-11-09T23:59:30.000+00:00", "2026-11-10T00:00:29.000+00:00"),      # an offset, not the writer's Z
              ("2026-11-09T23:59:30.000Z", "2026-11-10T00:00:29.000Z"))                # control: the writer's own shape

    def test_sol_b1_truncated_zone_less_and_date_only_stamps_are_unavailable_and_the_writer_shape_exposes_the_tail(self) -> None:
        got = coverages([(self.ROWS, self.record(first, last), self.EARLY, self.EARLY) for first, last in self.SHAPES])
        # sol's literal input: on 503dfe78 this read app_swap_window, sufficient=true, maxGapMs=1000 (WITHIN_BOUND) and hid a 29 s tail.
        self.assertEqual(got[0]["windowSource"], "unavailable", got[0])
        self.assertIsNone(got[0]["sufficient"], got[0])
        self.assertIn("yyyy-MM-ddTHH:mm:ss.fffZ", got[0]["unavailableReason"])
        for shape, r in zip(self.SHAPES[1:-1], got[1:-1]):
            self.assertEqual(r["windowSource"], "unavailable", (shape, r))
        control = got[-1]
        self.assertEqual(control["windowSource"], "app_swap_window", control)
        self.assertFalse(control["sufficient"], control)
        self.assertEqual(control["gapKind"], "tail", control)

    def test_the_stamp_reader_accepts_only_the_writer_shape(self) -> None:
        bad = ["2026-11-10T00:00", "2026-11-10", "2026-11-10T00:00:29Z", "2026-11-10T00:00:29.000", "2026-11-10T00:00:29.0000Z",
               "2026-11-10T00:00:29.000+01:00", "2026-13-10T00:00:29.000Z", " 2026-11-10T00:00:29.000Z", "2026-11-10t00:00:29.000z", "x", ""]
        got = swap_stamps(["2026-11-10T00:00:29.000Z"] + bad)
        self.assertEqual(got[0], "KIND=Utc 2026-11-10T00:00:29.0000000Z")
        for value, out in zip(bad, got[1:]):
            self.assertIsNone(out, value)

    def test_the_job_stop_sibling_re_reads_its_stamps_through_the_same_strict_reader(self) -> None:
        job = JOB.read_text(encoding="utf-8")
        self.assertIn("(Get-AttrCudaEmbeddedFunctionSource -Name @(\n    'ConvertFrom-AttrCudaSwapStampUtc',\n    'Get-AttrCudaPlaybackTemporalCoverage'\n))", job)
        base = job.index("            $swapWindowParsed = ($swapLastUtc -gt $swapFirstUtc)")
        region = job.index("# PRESENTMON-BACHELOR-CUDA-INTERNAL-GAP-1 >>>", base)
        body = job[region:job.index("# PRESENTMON-BACHELOR-CUDA-INTERNAL-GAP-1 <<<", region)]
        self.assertLess(region - base, 400, "the strict re-read follows the base parse directly")
        self.assertIn("$swapFirstUtc = ConvertFrom-AttrCudaSwapStampUtc -Value $swapWindowMatches[$swapWindowMatches.Count - 1].Groups['first'].Value", body)
        self.assertIn("$swapLastUtc = ConvertFrom-AttrCudaSwapStampUtc -Value $swapWindowMatches[$swapWindowMatches.Count - 1].Groups['last'].Value", body)
        self.assertIn("$swapWindowParsed = ($null -ne $swapFirstUtc -and $null -ne $swapLastUtc -and $swapLastUtc -gt $swapFirstUtc)", body)
        self.assertLess(job.index("# PRESENTMON-BACHELOR-CUDA-INTERNAL-GAP-1 <<<", region), job.index("$presentMonJobStopAnchorUncertaintyMs = ($presentMonPostSpawnUtc"))

    def test_the_module_exports_the_stamp_reader(self) -> None:
        exports = MODULE.read_text(encoding="utf-8").split("Export-ModuleMember", 1)[1]
        self.assertIn("ConvertFrom-AttrCudaSwapStampUtc", exports)


# evidence 3e1b442a smoke-run.log:130 (as quoted by opus r1; no owner footage read): the app's own swap record for a fullscreen CUDA look leg
STALL_RECORD = ("[2026-10-10T17:26:31.400Z] [INFO] [0x2a10] playback_smoke.gpu_window_swaps session=1 window_active=1 telemetry_enabled=1 swaps=551 "
                "swap_fps=10.981 max_gap_ms=21359.466 new_frame_max_gap_ms=21359.466 frames_presented=545 "
                "first_swap_utc=2026-10-10T17:25:41.176Z last_swap_utc=2026-10-10T17:26:31.355Z superseded_before_swap=0")
STALL_EARLY = "2026-10-10T17:25:00.0000000Z"
STALL_LATE = "2026-10-10T17:25:01.3910000Z"


def stall_rows(extra_hole: tuple[float, float] | None = None) -> list[float]:
    """PresentMon rows for that leg (origin at STALL_EARLY): presents every 41.7 ms over the swap window 41176..91355 ms, except a 21.36 s
    no-swap stretch from 60000 ms (the app's own stall); `extra_hole` drops more rows (a PresentMon loss the app's record does not explain)."""
    t = [40000.0 + 41.7 * i for i in range(int((60000.0 - 40000.0) / 41.7) + 1)]
    resume = t[-1] + 21359.466
    t += [resume + 41.7 * i for i in range(int((93000.0 - resume) / 41.7) + 1)]
    if extra_hole:
        t = [x for x in t if not (extra_hole[0] < x < extra_hole[1])]
    return t


def app_stall_metrics(**over: object) -> dict:
    m = {"presentMonStatus": "degraded", "presentMonTemporalMaxGapMs": 21359.5, "presentMonTemporalGapKind": "internal",
         "presentMonTemporalWindowSource": "app_swap_window", "presentMonTemporalAppSwapMaxGapMs": 21359.466, "presentMonTemporalOverBoundGapCount": 1}
    m.update(over)
    return {k: v for k, v in m.items() if v is not ...}


@requires_windows_pwsh
class AppStallAttributionTests(unittest.TestCase):
    """PR #788 r1 opus B3: an over-bound in-window PresentMon gap that the app's OWN swap record accounts for is a real playback stall
    (APP_SWAP_STALL: counted, the leg FAILs), never PRESENTMON_INTERNAL_GAP (INVALID, blamed on the instrument). Only a gap the app's record
    does not explain is PresentMon's."""

    def test_the_arm_carries_the_apps_swap_max_gap_from_the_record_it_selects_and_counts_every_over_bound_hole(self) -> None:
        no_max = STALL_RECORD.replace("max_gap_ms=21359.466 new_frame", "new_frame")
        got = coverages([
            (stall_rows(), STALL_RECORD, STALL_EARLY, STALL_LATE),
            (stall_rows(extra_hole=(82000.0, 88500.0)), STALL_RECORD, STALL_EARLY, STALL_LATE),
            (stall_rows(), no_max, STALL_EARLY, STALL_LATE),                                                       # absent
            (stall_rows(), STALL_RECORD.replace("max_gap_ms=21359.466 new_frame", "max_gap_ms=nan new_frame"), STALL_EARLY, STALL_LATE),
            (stall_rows(), STALL_RECORD.replace("max_gap_ms=21359.466 new_frame", "max_gap_ms=-1 new_frame"), STALL_EARLY, STALL_LATE),
            (stall_rows(), STALL_RECORD.replace("max_gap_ms=21359.466 new_frame", "max_gap_ms=1e4 new_frame"), STALL_EARLY, STALL_LATE),
        ])
        r = got[0]
        self.assertEqual(r["windowSource"], "app_swap_window", r)
        self.assertFalse(r["sufficient"], r)
        self.assertEqual(r["gapKind"], "internal", r)
        self.assertAlmostEqual(r["maxGapMs"], 21359.466, delta=1.0)
        self.assertEqual(r["appSwapMaxGapMs"], 21359.466, r)
        self.assertEqual(r["overBoundGapCount"], 1, r)
        self.assertEqual(got[1]["overBoundGapCount"], 2, got[1])
        # absent (new_frame_max_gap_ms is never read as the swap max gap), NaN, negative, exponent: $null, never assumed
        for case in got[2:]:
            self.assertEqual(case["windowSource"], "app_swap_window", case)
            self.assertIsNone(case["appSwapMaxGapMs"], case)

    def test_opus_b3_the_3e1b442a_shape_is_an_app_swap_stall_that_fails_the_leg_not_a_presentmon_gap(self) -> None:
        v = verdict(app_stall_metrics())
        self.assertEqual(v["verdict"], "APP_SWAP_STALL", v)
        self.assertFalse(v["blocks"], v)
        self.assertTrue(v["failsLeg"], v)
        self.assertIn("21359.5 ms", v["reason"])
        self.assertEqual(v["appSwapMaxGapMs"], 21359.466)

    def test_a_gap_the_apps_record_does_not_explain_stays_presentmon_internal_gap(self) -> None:
        unexplained = (("app max gap absent", app_stall_metrics(presentMonTemporalAppSwapMaxGapMs=...)),
                       ("app max gap null", app_stall_metrics(presentMonTemporalAppSwapMaxGapMs=None)),
                       ("app max gap a string", app_stall_metrics(presentMonTemporalAppSwapMaxGapMs="21359.466")),
                       ("app max gap negative", app_stall_metrics(presentMonTemporalAppSwapMaxGapMs=-1.0)),
                       ("healthy app (r4 leg)", app_stall_metrics(presentMonTemporalAppSwapMaxGapMs=96.372)),
                       ("PresentMon longer than the app's stall", app_stall_metrics(presentMonTemporalMaxGapMs=21359.466 + 250.1)),
                       ("PresentMon shorter than the app's stall", app_stall_metrics(presentMonTemporalMaxGapMs=21359.466 - 250.1)),
                       ("a tail gap is never an inter-swap gap", app_stall_metrics(presentMonTemporalGapKind="tail")),
                       ("two over-bound holes", app_stall_metrics(presentMonTemporalOverBoundGapCount=2)),
                       ("hole count absent", app_stall_metrics(presentMonTemporalOverBoundGapCount=...)),
                       ("hole count not an integer", app_stall_metrics(presentMonTemporalOverBoundGapCount=1.0)),
                       ("process-lifetime window", app_stall_metrics(presentMonTemporalWindowSource="process_lifetime")))
        explained = (app_stall_metrics(presentMonTemporalMaxGapMs=21359.466 + 249.9), app_stall_metrics(presentMonTemporalMaxGapMs=21359.466 - 249.9))
        quiet = (app_stall_metrics(presentMonTemporalMaxGapMs=96.4), {"presentMonStatus": "unavailable"})
        got = verdicts([m for _, m in unexplained] + list(explained) + list(quiet))
        for (label, _), v in zip(unexplained, got):
            self.assertTrue(v["blocks"], (label, v))
            self.assertFalse(v["failsLeg"], (label, v))
            self.assertIn(v["verdict"], ("PRESENTMON_INTERNAL_GAP", "PRESENTMON_GAP_UNLOCATED"), (label, v))
        for v in got[len(unexplained):len(unexplained) + len(explained)]:
            self.assertEqual(v["verdict"], "APP_SWAP_STALL", v)
        # within the bound, and no usable PresentMon status, never fail the leg
        for v in got[len(unexplained) + len(explained):]:
            self.assertFalse(v["blocks"], v)
            self.assertFalse(v["failsLeg"], v)

    def test_invoke_venue_leg_counts_an_app_stall_as_fail_and_names_it(self) -> None:
        leg = LEG.read_text(encoding="utf-8")
        self.assertNotIn("A leg whose PresentMon capture lost that span is not counted", leg)
        gate = leg.index("if ($outcome -in @('PASS', 'FAIL') -and $presentMonGapVerdict.blocks) {")
        tail = leg[gate:gate + 1400]
        self.assertIn("elseif ($outcome -in @('PASS', 'FAIL') -and $presentMonGapVerdict.failsLeg) {", tail)
        self.assertIn("$outcome = 'FAIL'", tail)

    def test_the_job_publishes_the_apps_swap_max_gap_and_the_hole_count(self) -> None:
        job = JOB.read_text(encoding="utf-8")
        for field in ("presentMonTemporalAppSwapMaxGapMs = $presentMonPlaybackTemporal.appSwapMaxGapMs",
                      "presentMonTemporalOverBoundGapCount = $presentMonPlaybackTemporal.overBoundGapCount"):
            self.assertIn(field, job)


@requires_windows_pwsh
class AdversaryR3Tests(unittest.TestCase):
    """Adversary review-adversary-...-r3-c81b5332 findings A1-A3 and ...-r4-d3887f21 A1 (introduced, major), each fed its literal input."""

    T0 = "2026-11-09T00:00:00.0000000Z"

    def test_a1_a_permitted_origin_with_an_edge_gap_is_never_explained_as_the_apps_stall(self) -> None:
        log = ("playback_smoke.gpu_window_swaps session=1 window_active=1 telemetry_enabled=1 swaps=126 max_gap_ms=21000 "
               "first_swap_utc=2026-11-09T00:00:20.000Z last_swap_utc=2026-11-09T00:00:45.000Z")
        r = coverages([([20000.0 + 40.0 * i for i in range(101)] + [45000.0], log, self.T0, "2026-11-09T00:00:01.0000000Z")])[0]
        self.assertGreaterEqual(r["overBoundGapCount"], 2, r)
        v = verdicts([{"presentMonStatus": "degraded", "presentMonTemporalMaxGapMs": r["maxGapMs"], "presentMonTemporalGapKind": r["gapKind"],
                       "presentMonTemporalWindowSource": r["windowSource"], "presentMonTemporalAppSwapMaxGapMs": r["appSwapMaxGapMs"],
                       "presentMonTemporalOverBoundGapCount": r["overBoundGapCount"]}])[0]
        self.assertNotEqual(v["verdict"], "APP_SWAP_STALL", (r, v))
        self.assertTrue(v["blocks"], v)

    def test_a2_the_count_covers_head_internal_and_tail_at_one_interior_origin(self) -> None:
        log = ("playback_smoke.gpu_window_swaps session=1 window_active=1 telemetry_enabled=1 swaps=102 max_gap_ms=6000 "
               "first_swap_utc=2026-11-09T00:00:05.100Z last_swap_utc=2026-11-09T00:00:25.120Z")
        r = coverages([([10000.0, 16000.0] + [16000.0 + 40.0 * i for i in range(1, 101)], log, self.T0, "2026-11-09T00:00:00.2200000Z")])[0]
        self.assertGreaterEqual(r["overBoundGapCount"], 3, r)

    def test_a3_an_origin_between_two_close_crossings_is_evaluated(self) -> None:
        log = ("playback_smoke.gpu_window_swaps session=1 window_active=1 telemetry_enabled=1 swaps=2 "
               "first_swap_utc=2026-11-09T00:00:01.000Z last_swap_utc=2026-11-09T00:00:02.000Z")
        r = coverages([([999.0, 1999.005], log, self.T0, "2026-11-09T00:00:00.0020000Z")])[0]
        self.assertEqual(r["windowSource"], "app_swap_window", r)
        self.assertFalse(r["sufficient"], r)

    # adversary review-adversary-...-r4-d3887f21 A1 (introduced, major): between two crossings the row set is constant but the edge gaps are not --
    # the head gap grows toward the interval's upper origin and the tail gap toward its lower one, and that supremum is reached at a crossing
    # where the row is already inside (inclusive edges), so it was sampled only as an internal gap. Sol's literal input and its mirror.
    def test_r4_a1_an_edge_gap_reached_only_at_an_interval_end_still_counts(self) -> None:
        late = "2026-11-09T00:00:05.0000000Z"
        dense = [26000.0 + 40.0 * k for k in range(451)]
        head = ("playback_smoke.gpu_window_swaps session=1 window_active=1 telemetry_enabled=1 swaps=452 max_gap_ms=6000 "
                "first_swap_utc=2026-11-09T00:00:24.000Z last_swap_utc=2026-11-09T00:00:49.000Z")
        tail = head.replace("00:00:24.000Z", "00:00:26.000Z").replace("00:00:49.000Z", "00:00:51.000Z")
        got = coverages([([20000.0] + dense, head, self.T0, late), ([50000.0] + dense, tail, self.T0, late)])
        for label, r in zip(("head (origin 3500 ms: 5500 ms head gap)", "tail (origin 1500 ms: 5500 ms tail gap)"), got):
            self.assertEqual(r["windowSource"], "app_swap_window", (label, r))
            self.assertFalse(r["sufficient"], (label, r))
            self.assertGreaterEqual(r["overBoundGapCount"], 2, (label, r))
        vs = verdicts([{"presentMonStatus": "degraded", "presentMonTemporalMaxGapMs": r["maxGapMs"], "presentMonTemporalGapKind": r["gapKind"],
                        "presentMonTemporalWindowSource": r["windowSource"], "presentMonTemporalAppSwapMaxGapMs": r["appSwapMaxGapMs"],
                        "presentMonTemporalOverBoundGapCount": r["overBoundGapCount"]} for r in got])
        for r, v in zip(got, vs):
            self.assertNotEqual(v["verdict"], "APP_SWAP_STALL", (r, v))
            self.assertTrue(v["blocks"], v)

    def test_r4_a1_an_edge_gap_at_the_ceiling_is_not_over_it(self) -> None:
        # the supremum is not attained (the open interval never reaches its crossing), so a limit EQUAL to the ceiling is no over-bound edge:
        # the same shape with the isolated row 5000 ms before the dense run keeps the app's one internal stall attributable
        late = "2026-11-09T00:00:05.0000000Z"
        dense = [26000.0 + 40.0 * k for k in range(451)]
        log = ("playback_smoke.gpu_window_swaps session=1 window_active=1 telemetry_enabled=1 swaps=452 max_gap_ms=5000 "
               "first_swap_utc=2026-11-09T00:00:24.000Z last_swap_utc=2026-11-09T00:00:49.000Z")
        r = coverages([([21000.0] + dense, log, self.T0, late)])[0]
        self.assertTrue(r["sufficient"], r)
        self.assertEqual(r["overBoundGapCount"], 0, r)


@requires_windows_pwsh
class SharedReaderGapTests(EvidenceFactory, unittest.TestCase):
    """PR #788 r1 sol B2 (= opus/fable A3 hardening): the shared validator Test-DvReceiptValid applies the same gap verdict the writer does, from the
    VERIFIED verbatim metrics, so a PASS/FAIL receipt whose PresentMon gap blocks is INVALID in every reader (Invoke-VenueBaseline, New-VenueSheetPair,
    New-VenueFlavorPair all gate on its status). A receipt that predates this card's job (no presentMonTemporalWindowSource) keeps today's classification."""

    def setUp(self) -> None:
        self.make_harness()

    def test_sol_b2_a_blocking_gap_is_invalid_in_the_validator_and_a_legacy_receipt_is_unchanged(self) -> None:
        repo = self.prod_repo()
        sol = {"presentMonStatus": "degraded", "presentMonTemporalMaxGapMs": 6000.5, "presentMonTemporalGapKind": "internal",
               "presentMonTemporalWindowSource": "app_swap_window"}
        legacy = {k: v for k, v in sol.items() if k != "presentMonTemporalWindowSource"}
        healthy = app_stall_metrics(presentMonStatus="ok", presentMonTemporalMaxGapMs=96.4, presentMonTemporalAppSwapMaxGapMs=96.372,
                                    presentMonTemporalOverBoundGapCount=0)
        ev_sol = self.evidence("sol", summary=sol)
        ev_legacy = self.evidence("legacy", summary=legacy)
        ev_stall = self.evidence("stall", summary=app_stall_metrics())
        ev_healthy = self.evidence("healthy", summary=healthy)
        got = self.status_batch(repo, [
            (self.receipt_for(repo, ev_sol, outcome="PASS"), None),
            (self.receipt_for(repo, ev_sol, outcome="FAIL"), None),
            (self.receipt_for(repo, ev_legacy, outcome="PASS"), None),
            (self.receipt_for(repo, ev_stall, outcome="PASS"), None),
            (self.receipt_for(repo, ev_stall, outcome="FAIL"), None),
            (self.receipt_for(repo, ev_healthy, outcome="PASS"), None),
        ])
        self.expect(got[0], "INVALID", "sol's literal receipt, PASS", "PRESENTMON_INTERNAL_GAP")
        self.expect(got[1], "INVALID", "sol's literal receipt, FAIL", "PRESENTMON_INTERNAL_GAP")
        self.expect(got[2], "ADVISORY", "a receipt that predates the window-source field keeps today's classification")
        self.expect(got[3], "INVALID", "an app swap stall claimed as a PASS", "OUTCOME_NOT_DERIVED")
        self.expect(got[4], "ADVISORY", "an app swap stall is a counted FAIL")
        self.expect(got[5], "ADVISORY", "a healthy leg of the new shape")

    def test_r4_78b6b9b2_a1_the_validator_never_gates_a_cpu_receipt_on_presentmon(self) -> None:
        # adversary r4 A1 (introduced, major): the shared validator judged a cpu receipt's PresentMon gap too, so every reader refused a cpu figure the
        # writer would (now) count. The backend is the one the HASHED summary derives (cpuFrames > 0, gpuFramesTotal 0), never a receipt field.
        repo = self.prod_repo()
        sol = {"presentMonStatus": "degraded", "presentMonTemporalWindowSource": "process_lifetime", "presentMonTemporalMaxGapMs": 10000.0,
               "presentMonTemporalGapKind": "head"}
        ev_cpu = self.evidence("cpu-gap", backend="cpu", summary=sol)
        ev_cuda = self.evidence("cuda-gap", summary=sol)
        got = self.status_batch(repo, [(self.receipt_for(repo, ev_cpu, backend="cpu", outcome="PASS"), None),
                                       (self.receipt_for(repo, ev_cuda, outcome="PASS"), None)])
        self.expect(got[0], "ADVISORY", "a cpu receipt's PresentMon gap is informational")
        self.expect(got[1], "INVALID", "the same gap on a cuda receipt still blocks", "PRESENTMON_GAP_UNLOCATED")

    def test_every_reader_gates_figures_on_the_validator_status(self) -> None:
        dv = ROOT / "tools" / "profiling" / "dual-venue"
        baseline = (dv / "Invoke-VenueBaseline.ps1").read_text(encoding="utf-8")
        self.assertIn("else { Test-DvReceiptValid -Receipt $rc -RepoRoot $repo }", baseline)
        self.assertIn("if ($f.verification.status -cnotin @('ADVISORY', 'NO_SIGNAL', 'VERIFIED', 'VERIFIED_OFFLINE_TEST')) { $f.rejected = $f.verification.status; return $f }", baseline)
        for name in ("New-VenueSheetPair.ps1", "New-VenueFlavorPair.ps1"):
            text = (dv / name).read_text(encoding="utf-8")
            self.assertIn("$validity = Test-DvReceiptValid -Receipt $r -RepoRoot $repoRoot", text)
            self.assertIn("if ($validity.status -cne 'ADVISORY') { throw", text)


if __name__ == "__main__":
    unittest.main()
