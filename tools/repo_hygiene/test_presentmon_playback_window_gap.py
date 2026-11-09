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

from tools.repo_hygiene.test_dual_venue_evidence import requires_windows_pwsh, run_pwsh, run_pwsh_json

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
               "first_swap_utc=2026-11-09T00:00:01Z last_swap_utc=2026-11-09T00:00:30Z\n"
               "[2026-11-09T00:00:31Z] [INFO] [0x1] harmless record (C:/b playback_smoke.gpu_window_swaps first_swap_utc=2026-11-08 "
               "last_swap_utc=2026-11-09 x/MainWindow.cpp:1)\n")
        r = coverage([1000.0 * i for i in range(1, 31)], log, early="2026-11-09T00:00:00.0000000Z", late="2026-11-09T00:00:00.0000000Z")["r"]
        self.assertEqual(r["windowSource"], "app_swap_window", r)
        self.assertTrue(r["sufficient"], r)
        self.assertEqual(r["maxGapMs"], 1000.0, r)

    def test_a2_an_endpoint_with_zero_rows_is_never_outvoted_by_a_sufficient_one(self) -> None:
        log = ("playback_smoke.gpu_window_swaps session=1 window_active=1 telemetry_enabled=1 swaps=30 "
               "first_swap_utc=2026-11-09T00:00:01Z last_swap_utc=2026-11-09T00:00:02Z")
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
            "first_swap_utc=2026-11-09T00:00:01Z last_swap_utc=2026-11-09T00:00:30Z")
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
               "first_swap_utc=2026-11-09T00:01:00Z last_swap_utc=2026-11-09T00:01:25Z")
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
        self.assertIn("(Get-AttrCudaEmbeddedFunctionSource -Name @(\n    'Get-AttrCudaPlaybackTemporalCoverage'\n))", self.job)
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


def verdict(metrics: dict) -> dict:
    proc, out = run_pwsh_json("$m = $env:PMG_M | ConvertFrom-Json\n(Get-DvPresentMonGapVerdict -Metrics $m) | ConvertTo-Json -Compress", PMG_M=json.dumps(metrics))
    if out is None:
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

    def test_the_bound_is_the_jobs_own_temporal_ceiling(self) -> None:
        job = JOB.read_text(encoding="utf-8")
        ceiling = float(re.search(r"^\$presentMonSufficiencyMaxGapMs = ([\d.]+)$", job, re.M).group(1))
        self.assertEqual(verdict({})["boundMs"], ceiling)


class ReceiptGuardWiringTests(unittest.TestCase):
    def test_invoke_venue_leg_records_the_verdict_and_fails_closed(self) -> None:
        leg = LEG.read_text(encoding="utf-8")
        self.assertIn("$presentMonGapVerdict = Get-DvPresentMonGapVerdict -Metrics $receipt.metrics", leg)
        self.assertIn("$receipt['presentMonGap'] = $presentMonGapVerdict", leg)
        self.assertIn("if ($outcome -in @('PASS', 'FAIL') -and $presentMonGapVerdict.blocks) {", leg)
        gate = leg.index("if ($outcome -in @('PASS', 'FAIL') -and $presentMonGapVerdict.blocks) {")
        self.assertIn("$outcome = 'INVALID'", leg[gate:gate + 600])
        self.assertLess(leg.index("$receipt.metrics = Get-DvVerbatimMetrics"), leg.index("$presentMonGapVerdict = Get-DvPresentMonGapVerdict"))


if __name__ == "__main__":
    unittest.main()
