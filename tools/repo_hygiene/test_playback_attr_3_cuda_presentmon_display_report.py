"""CUDA-PERF-DISPLAY-IDENTITY-HARNESS-1/2: Get-AttrCudaPresentMonDisplayReport, EXECUTED against
real presentmon.csv fixtures -- the shared runtime function tools/profiling/bachelor/
AttrCudaArtifacts.psm1 exports and playback-attr-3-cuda-job.ps1 embeds verbatim, so a pass here is
a statement about the exact characters that run unattended on Bachelor.

WHY THIS FILE EXISTS. Baseline defects, reproduced against the provisional bachelor harness
before HARNESS-1: (1) 3 of 8 attempts ran a full smoke then died at an unguarded Import-Csv of a
missing out\\diagnostic\\presentmon.csv, publishing nothing; (2) "no positive
MsBetweenDisplayChange samples" was an uncaught throw AFTER the smoke run had already passed,
destroying every artifact already produced; (3) PresentMon runs --timed 55 against a --seconds 40
playback, so idle desktop/startup presents outside the measured window could inflate or deflate
whichever swap chain happened to look busiest. Get-AttrCudaPresentMonDisplayReport closes all
three: a missing/unreadable/columnless/empty csv, or a window with zero displayed samples for the
MLVApp chain, is a typed 'PRESENTMON_UNAVAILABLE' / 'DISPLAY_ASLEEP' return -- never a throw -- and
every row is grouped by (ProcessID, SwapChainAddress) and restricted to
[process.startedAtUtc, process.endedAtUtc] before any rate is computed.

HARNESS-2 (sol BLOCKER 3) found that these fixtures were themselves a defect: they carried a
`DisplayedTime` column the pinned PresentMon 2.5.1 legacy launch never emits, which made the
required-column check pass here while failing on every real capture. Every fixture in this file
now uses only real columns (MsUntilDisplayed replaces DisplayedTime), and
RealPresentMon251HeaderFixtureTests below pins the exact 28-column real header, read directly
from a Bachelor capture, so a future column-set drift is caught here instead of on Bachelor.
HARNESS-2 (sol HARDENING) also changed chain selection: the MLVApp preview is now the sum of
every swap chain address the target PID used in the window, not just the busiest one -- see
TwoSwapChainFixtureTests.test_a_second_mlvapp_swap_chain_is_summed_into_the_logical_preview.

The sibling test_playback_attr_3_cuda_presentmon_publish_ordering.py asserts, on the generator's
own template text, that the job-level ORDERING this round requires is also still there: smoke
artifacts published before PresentMon is even waited on, and the old unguarded throws are gone.

SKIPS. Everything is skipped cleanly when pwsh is absent, and on non-Windows platforms (the
module's own callers validate drive-letter paths); Windows CI runs it all.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "tools" / "profiling" / "bachelor" / "AttrCudaArtifacts.psm1"
ATTRIBUTION_GENERATOR = ROOT / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-job.ps1"

PWSH = shutil.which("pwsh")
requires_pwsh = unittest.skipIf(PWSH is None, "pwsh is not on PATH")


def _run_pwsh_file(script: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(script)],
        capture_output=True,
        text=True,
    )


# A fixed, deterministic window shared by every test below: PresentMon capture "starts" at this
# instant, and the MLVApp process is reported (by the stand-in result.json) to have run from +2s
# to +42s -- mirroring the real --timed 55 vs --seconds 40 gap the round's evidence describes,
# with idle time on both sides of the measured window.
CAPTURE_START_UTC = "2026-01-01T00:00:00.0000000Z"
WINDOW_START_UTC = "2026-01-01T00:00:02.0000000Z"
WINDOW_END_UTC = "2026-01-01T00:00:42.0000000Z"
TARGET_PID = 4242


def _result_json(pid: object = TARGET_PID, start: object = WINDOW_START_UTC, end: object = WINDOW_END_UTC) -> dict:
    process: dict[str, object] = {}
    if pid is not None:
        process["id"] = pid
    if start is not None:
        process["startedAtUtc"] = start
    if end is not None:
        process["endedAtUtc"] = end
    return {"process": process}


def _csv_row(
    *, application: str = "MLVApp.exe", process_id: object = TARGET_PID, swap_chain: str = "0xCCC",
    present_mode: str = "Hardware: Independent Flip", between_presents: str = "16.6",
    between_display_change: str = "16.6", until_displayed: str = "16.6", time_in_ms: object = 5000,
) -> dict[str, str]:
    # CUDA-PERF-DISPLAY-IDENTITY-HARNESS-2 (sol BLOCKER 3): these are the columns the PINNED
    # PresentMon 2.5.1 legacy launch actually emits -- confirmed against real Bachelor captures.
    # There is no DisplayedTime column in that schema; MsUntilDisplayed is a real one instead.
    return {
        "Application": application,
        "ProcessID": str(process_id),
        "SwapChainAddress": swap_chain,
        "PresentMode": present_mode,
        "MsBetweenPresents": between_presents,
        "MsBetweenDisplayChange": between_display_change,
        "MsUntilDisplayed": until_displayed,
        "TimeInMs": str(time_in_ms),
    }


# CUDA-PERF-DISPLAY-IDENTITY-HARNESS-2 (sol BLOCKER 3): the EXACT 28-column header the pinned
# PresentMon 2.5.1 legacy launch emits, read directly from a real Bachelor capture -- never a
# synthetic column list. Module-level (not just RealPresentMon251HeaderFixtureTests's own copy)
# so IntervalStatsFilterFixtureTests below can build the same real-shaped fixture.
REAL_PRESENTMON_HEADER = [
    "Application", "ProcessID", "SwapChainAddress", "PresentRuntime", "SyncInterval",
    "PresentFlags", "AllowsTearing", "PresentMode", "TimeInMs", "MsBetweenSimulationStart",
    "MsBetweenPresents", "MsBetweenDisplayChange", "MsInPresentAPI", "MsRenderPresentLatency",
    "MsUntilDisplayed", "CPUStartTimeInMs", "MsBetweenAppStart", "MsCPUBusy", "MsCPUWait",
    "MsGPULatency", "MsGPUTime", "MsGPUBusy", "MsGPUWait", "MsAnimationError", "AnimationTime",
    "MsFlipDelay", "MsAllInputToPhotonLatency", "MsClickToPhotonLatency",
]


def _real_csv_row(
    *, process_id: object = TARGET_PID, swap_chain: str = "0xCCC",
    time_in_ms: object, between_display_change: str = "16.6", until_displayed: str = "8.3",
) -> dict[str, str]:
    values = {name: "NA" for name in REAL_PRESENTMON_HEADER}
    values.update({
        "Application": "MLVApp.exe",
        "ProcessID": str(process_id),
        "SwapChainAddress": swap_chain,
        "PresentRuntime": "DXGI",
        "SyncInterval": "0",
        "PresentFlags": "512",
        "AllowsTearing": "0",
        "PresentMode": "Composed: Flip",
        "TimeInMs": str(time_in_ms),
        "MsBetweenPresents": "16.6",
        "MsBetweenDisplayChange": between_display_change,
        "MsUntilDisplayed": until_displayed,
    })
    return values


class _ReportCase(unittest.TestCase):
    def setUp(self) -> None:
        if PWSH is None:
            self.skipTest("pwsh is not on PATH")
        self._tmp = tempfile.TemporaryDirectory(prefix="attr3-pmreport-")
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _write_csv(self, rows: list[dict[str, str]]) -> Path:
        path = self.tmp / "presentmon.csv"
        if not rows:
            path.write_text("", encoding="utf-8")
            return path
        header = list(rows[0].keys())
        lines = [",".join(header)]
        for row in rows:
            lines.append(",".join(row[key] for key in header))
        path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
        return path

    def call(
        self, csv_path: Path | None, result_json: dict | None, *,
        capture_start: str = CAPTURE_START_UTC,
        latest_capture_start: str | None = None,
    ) -> dict:
        # CUDA-PERF-DISPLAY-IDENTITY-HARNESS-3: the function now windows under TWO bracket
        # endpoints. Every sibling test below that only cares about single-anchor behaviour
        # passes latest_capture_start=None, which degenerates the bracket to one point (both
        # endpoints equal) -- identical windowing to before this round, so none of those
        # assertions needed to change. Only the dedicated bracket tests further down pass two
        # distinct endpoints.
        out_path = self.tmp / "report-out.json"
        csv_literal = "$null" if csv_path is None else "'" + str(csv_path) + "'"
        result_json_expr = "$null"
        if result_json is not None:
            result_json_path = self.tmp / "result.json"
            result_json_path.write_text(json.dumps(result_json), encoding="utf-8")
            result_json_expr = (
                "(Get-Content -LiteralPath '" + str(result_json_path) + "' -Raw | ConvertFrom-Json)"
            )
        latest = latest_capture_start if latest_capture_start is not None else capture_start
        script = self.tmp / "probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{MODULE}' -Force\n"
            f"$earliestCaptureStart = [datetime]::Parse('{capture_start}', $null, "
            "[Globalization.DateTimeStyles]::RoundtripKind)\n"
            f"$latestCaptureStart = [datetime]::Parse('{latest}', $null, "
            "[Globalization.DateTimeStyles]::RoundtripKind)\n"
            f"$csvPath = {csv_literal}\n"
            f"$resultJson = {result_json_expr}\n"
            "$report = Get-AttrCudaPresentMonDisplayReport -CsvPath $csvPath "
            "-ResultJson $resultJson -EarliestCaptureStartUtc $earliestCaptureStart "
            "-LatestCaptureStartUtc $latestCaptureStart\n"
            f"$report | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath '{out_path}' -Encoding UTF8\n"
            "Write-Output 'PROBE_DONE'\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PROBE_DONE", proc.stdout, proc.stdout + proc.stderr)
        return json.loads(out_path.read_text(encoding="utf-8"))

    def _write_real_csv(self, rows: list[dict[str, str]]) -> Path:
        path = self.tmp / "presentmon.csv"
        lines = [",".join(REAL_PRESENTMON_HEADER)]
        for row in rows:
            lines.append(",".join(row[name] for name in REAL_PRESENTMON_HEADER))
        path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
        return path


@requires_pwsh
class MissingCsvFixtureTests(_ReportCase):
    """Defect 1: a missing/unreadable presentmon.csv is a typed refusal, never a throw."""

    def test_a_missing_csv_is_presentmon_unavailable_not_a_throw(self) -> None:
        missing = self.tmp / "does-not-exist.csv"
        report = self.call(missing, _result_json())
        self.assertEqual(report["status"], "PRESENTMON_UNAVAILABLE")
        self.assertIn("does not exist", report["reason"])
        self.assertEqual(report["selectedChainRows"], [])

    def test_an_empty_csv_is_presentmon_unavailable(self) -> None:
        empty = self._write_csv([])
        report = self.call(empty, _result_json())
        self.assertEqual(report["status"], "PRESENTMON_UNAVAILABLE")
        self.assertIn("no rows", report["reason"])

    def test_a_csv_missing_a_required_column_is_presentmon_unavailable(self) -> None:
        path = self.tmp / "presentmon.csv"
        # No TimeInMs column at all -- the exact shape a PresentMon build whose CSV schema
        # differs from what this job expects would produce; must fail closed with a clear
        # reason, never crash on an absent property.
        path.write_text(
            "Application,ProcessID,SwapChainAddress,PresentMode,MsBetweenPresents,"
            "MsBetweenDisplayChange,MsUntilDisplayed\r\n"
            "MLVApp.exe,4242,0xCCC,Hardware: Independent Flip,16.6,16.6,16.6\r\n",
            encoding="utf-8",
        )
        report = self.call(path, _result_json())
        self.assertEqual(report["status"], "PRESENTMON_UNAVAILABLE")
        self.assertIn("TimeInMs", report["reason"])

    def test_missing_process_identity_is_presentmon_unavailable(self) -> None:
        path = self._write_csv([_csv_row()])
        report = self.call(path, _result_json(pid=None))
        self.assertEqual(report["status"], "PRESENTMON_UNAVAILABLE")
        self.assertIn("process.id", report["reason"])

    def test_malformed_window_timestamps_are_presentmon_unavailable(self) -> None:
        path = self._write_csv([_csv_row()])
        report = self.call(path, _result_json(start="not-a-timestamp"))
        self.assertEqual(report["status"], "PRESENTMON_UNAVAILABLE")

    def test_a_header_only_csv_with_zero_data_rows_is_presentmon_unavailable(self) -> None:
        # PRESENTMON-HARNESS-ROBUSTNESS-1: distinct from test_an_empty_csv_is_presentmon_unavailable
        # above (a genuinely 0-byte file, no header at all) -- this is the shape PresentMon itself
        # would actually write if it captured a valid session but the process never presented a
        # single frame inside its own --timed window: a real, well-formed header, zero data rows.
        path = self.tmp / "presentmon.csv"
        path.write_text(
            "Application,ProcessID,SwapChainAddress,PresentMode,MsBetweenPresents,"
            "MsBetweenDisplayChange,MsUntilDisplayed,TimeInMs\r\n",
            encoding="utf-8",
        )
        report = self.call(path, _result_json())
        self.assertEqual(report["status"], "PRESENTMON_UNAVAILABLE")
        self.assertIn("no rows", report["reason"])

    def test_a_columnless_csv_is_presentmon_unavailable_not_a_throw(self) -> None:
        # A degenerate capture with no header row at all (just blank/garbage lines) -- Import-Csv
        # itself may throw or hand back rows with no properties; either way this must still be a
        # typed refusal, never an uncaught exception reaching the job's own outer try/finally.
        path = self.tmp / "presentmon.csv"
        path.write_text("\r\n\r\n\r\n", encoding="utf-8")
        report = self.call(path, _result_json())
        self.assertEqual(report["status"], "PRESENTMON_UNAVAILABLE")


@requires_pwsh
class ZeroDisplayedFixtureTests(_ReportCase):
    """Defect 2: zero displayed samples for the MLVApp chain is DISPLAY_ASLEEP, never the old
    unguarded 'no positive MsBetweenDisplayChange samples' throw -- and the zero rows are not
    silently discarded: presentedCount must still reflect every one of them."""

    def test_all_zero_display_change_rows_in_window_is_display_asleep(self) -> None:
        rows = [
            _csv_row(between_display_change="0", until_displayed="0", time_in_ms=5000 + i * 1000)
            for i in range(5)
        ]
        path = self._write_csv(rows)
        report = self.call(path, _result_json())
        self.assertEqual(report["status"], "DISPLAY_ASLEEP")
        self.assertIn("displayed 0", report["reason"])
        self.assertEqual(len(report["chains"]), 1)
        chain = report["selectedChain"]
        self.assertEqual(chain["processId"], TARGET_PID)
        # The zero rows were kept, not discarded: presentedCount counts all 5, displayedCount none.
        self.assertEqual(chain["presentedCount"], 5)
        self.assertEqual(chain["displayedCount"], 0)
        self.assertEqual(report["selectedChainRows"], [])

    def test_no_rows_at_all_inside_the_window_is_presentmon_unavailable(self) -> None:
        # Every row sits before the window opens (process.startedAtUtc is +2s) -- idle
        # desktop presents from the --timed 55 vs --seconds 40 gap, never mistaken for a
        # display sample.
        rows = [_csv_row(time_in_ms=t) for t in (100, 500, 900)]
        path = self._write_csv(rows)
        report = self.call(path, _result_json())
        self.assertEqual(report["status"], "PRESENTMON_UNAVAILABLE")
        self.assertIn("playback window", report["reason"])
        # CUDA-PERF-DISPLAY-IDENTITY-HARNESS-3: clockBracket is carried on this typed refusal
        # too, not only on OK/DISPLAY_ASLEEP -- a reader refused here still learns both endpoints
        # genuinely found nothing, not just one of them.
        self.assertIsNotNone(report["clockBracket"])
        self.assertEqual(report["clockBracket"]["earliest"]["presentedCount"], 0)


@requires_pwsh
class TwoSwapChainFixtureTests(_ReportCase):
    """Defect 3 (and the naming requirement): with two (ProcessID, SwapChainAddress) chains in
    the window, the MLVApp one -- matched by result.json's own process.id -- is selected, its
    rates are computed only from its own rows, and rows outside the playback window are excluded
    from every chain regardless of which process wrote them."""

    def test_the_mlvapp_chain_is_selected_over_a_busier_foreign_chain(self) -> None:
        rows = []
        # A foreign/desktop compositor chain: busier than MLVApp's, must NOT be selected.
        for i in range(20):
            rows.append(_csv_row(
                application="dwm.exe", process_id=999, swap_chain="0xBBB",
                present_mode="Composed: Flip", time_in_ms=3000 + i * 100,
            ))
        # The MLVApp chain: 10 rows in-window (2 with MsBetweenDisplayChange == 0), plus 2 rows
        # outside the window that must be excluded from both presentedCount and displayedCount.
        rows.append(_csv_row(time_in_ms=500))  # before the window opens
        for i in range(10):
            disp = "0" if i < 2 else "16.6"
            rows.append(_csv_row(between_display_change=disp, until_displayed=disp, time_in_ms=5000 + i * 1000))
        rows.append(_csv_row(time_in_ms=50000))  # after the window closes
        path = self._write_csv(rows)

        report = self.call(path, _result_json())

        self.assertEqual(report["status"], "OK")
        self.assertEqual(len(report["chains"]), 2)
        selected = report["selectedChain"]
        self.assertEqual(selected["processId"], TARGET_PID)
        self.assertEqual(selected["swapChainAddress"], "0xCCC")
        self.assertTrue(selected["isMlvAppChain"])
        # Only the 10 in-window MLVApp rows count -- the 2 outside the window are excluded.
        self.assertEqual(selected["presentedCount"], 10)
        self.assertEqual(selected["displayedCount"], 8)
        # windowSeconds is exactly 40 (WINDOW_END_UTC - WINDOW_START_UTC): rates are counts /
        # window duration, not per-row derived means.
        self.assertAlmostEqual(selected["presentedFps"], 10 / 40.0)
        self.assertAlmostEqual(selected["displayedFps"], 8 / 40.0)
        self.assertEqual(len(report["selectedChainRows"]), 8)

        foreign = next(c for c in report["chains"] if not c["isMlvAppChain"])
        self.assertEqual(foreign["processId"], 999)
        self.assertEqual(foreign["swapChainAddress"], "0xBBB")

    def test_a_second_mlvapp_swap_chain_is_summed_into_the_logical_preview(self) -> None:
        # CUDA-PERF-DISPLAY-IDENTITY-HARNESS-2 (sol HARDENING): a swap chain recreated mid-run
        # for the SAME pid (e.g. a window resize tearing one down and recreating it) is one
        # continuous logical MLVApp preview, not two competing chains -- the old "prefer the
        # busier one" behaviour silently dropped the shorter-lived chain's frames from both the
        # count and the rate.
        rows = [_csv_row(swap_chain="0xDDD", time_in_ms=5000 + i * 1000) for i in range(2)]
        rows += [_csv_row(swap_chain="0xCCC", time_in_ms=10000 + i * 1000) for i in range(8)]
        path = self._write_csv(rows)

        report = self.call(path, _result_json())

        self.assertEqual(report["status"], "OK")
        self.assertEqual(report["selectedChain"]["presentedCount"], 10)
        self.assertEqual(report["selectedChain"]["displayedCount"], 10)
        self.assertCountEqual(report["selectedChain"]["swapChainAddresses"], ["0xDDD", "0xCCC"])
        self.assertEqual(len(report["chains"]), 2)
        self.assertEqual(len(report["selectedChainRows"]), 10)
        self.assertAlmostEqual(report["selectedChain"]["presentedFps"], 10 / 40.0)

    def test_no_mlvapp_rows_in_window_is_presentmon_unavailable_even_with_other_chains(self) -> None:
        rows = [_csv_row(application="dwm.exe", process_id=999, swap_chain="0xBBB", time_in_ms=5000 + i * 1000) for i in range(4)]
        path = self._write_csv(rows)

        report = self.call(path, _result_json())

        self.assertEqual(report["status"], "PRESENTMON_UNAVAILABLE")
        self.assertIn(str(TARGET_PID), report["reason"])
        self.assertEqual(len(report["chains"]), 1)
        # CUDA-PERF-DISPLAY-IDENTITY-HARNESS-3: clockBracket is carried here too -- both
        # endpoints agree MLVApp presented 0 rows in this fixture (only the foreign chain is in
        # the window), so both read 0, not just the headline one.
        self.assertIsNotNone(report["clockBracket"])
        self.assertEqual(report["clockBracket"]["earliest"]["presentedCount"], 0)
        self.assertEqual(report["clockBracket"]["latest"]["presentedCount"], 0)


@requires_pwsh
class RealPresentMon251HeaderFixtureTests(_ReportCase):
    """CUDA-PERF-DISPLAY-IDENTITY-HARNESS-2 (sol BLOCKER 3): the sibling tests above all use a
    convenient 8-column subset. A fixture carrying a column the pinned tool never emits (the old
    DisplayedTime requirement) hid exactly this defect, so this class instead uses the EXACT
    28-column header the pinned PresentMon 2.5.1 legacy launch emits, confirmed by directly
    reading a real presentmon.csv header from a Bachelor capture
    (tools/profiling/bachelor/playback-attr-3-cuda-*.artifacts/presentmon.csv) -- never a
    synthetic column list. Non-essential columns carry 'NA', exactly as PresentMon itself writes
    for a metric it could not compute (observed for MsBetweenDisplayChange/MsUntilDisplayed on
    the very first present of a real capture)."""

    def test_the_real_28_column_header_is_accepted_and_produces_a_report(self) -> None:
        rows = [
            _real_csv_row(
                time_in_ms=5000 + i * 1000,
                between_display_change=("NA" if i == 0 else "16.6"),
                until_displayed=("8.3" if i == 0 else "16.6"),
            )
            for i in range(10)
        ]
        path = self._write_real_csv(rows)

        report = self.call(path, _result_json())

        self.assertEqual(report["status"], "OK", report)
        self.assertEqual(report["selectedChain"]["processId"], TARGET_PID)
        # All 10 rows displayed: row 0 via MsUntilDisplayed (MsBetweenDisplayChange reads NA, as
        # PresentMon reports for the very first present of a capture, with no prior display
        # change to diff against), the rest via MsBetweenDisplayChange.
        self.assertEqual(report["selectedChain"]["displayedCount"], 10)
        self.assertEqual(len(report["selectedChainRows"]), 10)
        # CUDA-PERF-DISPLAY-IDENTITY-HARNESS-3 (sol BLOCKER): row 0's interval is genuinely
        # absent, never a zero one -- msBetweenDisplayChange and displayFpsEquivalent are both
        # $null on the wire (Export-Csv later writes that as an empty cell, which
        # refresh_period_histogram.py already skips). See IntervalStatsFilterFixtureTests below
        # for the job-level Get-Stats consumer that must filter this out itself.
        self.assertIsNone(report["selectedChainRows"][0]["msBetweenDisplayChange"])
        self.assertIsNone(report["selectedChainRows"][0]["displayFpsEquivalent"])
        for row in report["selectedChainRows"][1:]:
            self.assertAlmostEqual(row["msBetweenDisplayChange"], 16.6)

    def test_a_row_with_na_display_metrics_is_presented_but_not_displayed(self) -> None:
        # PresentMon legitimately writes NA for metrics it cannot compute yet -- never mistaken
        # for a schema mismatch, and never counted as a display when both signals are absent.
        row = _real_csv_row(time_in_ms=5000, between_display_change="NA", until_displayed="NA")
        path = self._write_real_csv([row])

        report = self.call(path, _result_json())

        self.assertEqual(report["status"], "DISPLAY_ASLEEP")
        self.assertEqual(report["selectedChain"]["presentedCount"], 1)
        self.assertEqual(report["selectedChain"]["displayedCount"], 0)


@requires_pwsh
class BracketWindowingFixtureTests(_ReportCase):
    """CUDA-PERF-DISPLAY-IDENTITY-HARNESS-3 (sol+fable HARDENING): rows are now windowed under
    BOTH endpoints of the caller's capture-start bracket, never just the earlier one -- the
    earlier-only anchor's direction argument was inverted (see the corrected comment in the
    module and playback-attr-3-cuda-job.ps1). WINDOW_START_UTC is +2s of CAPTURE_START_UTC; a
    LatestCaptureStartUtc 500ms after EarliestCaptureStartUtc shifts windowStartMs 500ms earlier
    (windowSeconds -- the real playback duration -- is unaffected, since both window bounds shift
    together), so a row placed in that 500ms sliver is IN the window only under the later
    endpoint -- exactly the front-edge-exclusion failure direction the corrected comment
    describes."""

    _EARLIEST = "2026-01-01T00:00:00.0000000Z"
    _LATEST = "2026-01-01T00:00:00.5000000Z"

    def test_a_front_edge_row_admitted_only_under_the_later_endpoint_heads_the_report(self) -> None:
        rows = [
            _csv_row(time_in_ms=1600),  # in-window only if anchored on the later endpoint
            _csv_row(time_in_ms=10000),  # in-window under both endpoints
        ]
        path = self._write_csv(rows)

        report = self.call(path, _result_json(), capture_start=self._EARLIEST, latest_capture_start=self._LATEST)

        self.assertEqual(report["status"], "OK", report)
        bracket = report["clockBracket"]
        self.assertEqual(bracket["earliest"]["presentedCount"], 1)
        self.assertEqual(bracket["latest"]["presentedCount"], 2)
        # The later endpoint admits strictly more genuinely-in-window rows, so it heads the
        # report -- selectedChainRows/selectedChain reflect it, not the earlier endpoint.
        self.assertEqual(bracket["headline"], "latest")
        self.assertEqual(report["selectedChain"]["presentedCount"], 2)
        self.assertEqual(len(report["selectedChainRows"]), 2)
        # Exactly the one front-edge row disagrees on window membership between the endpoints.
        self.assertEqual(bracket["rowsDifferingInWindowMembership"], 1)

    def test_a_degenerate_bracket_has_no_disagreement_and_heads_earliest(self) -> None:
        rows = [_csv_row(time_in_ms=5000 + i * 1000) for i in range(3)]
        path = self._write_csv(rows)

        report = self.call(path, _result_json(), capture_start=self._EARLIEST)

        self.assertEqual(report["status"], "OK", report)
        bracket = report["clockBracket"]
        self.assertEqual(bracket["earliest"], bracket["latest"])
        self.assertEqual(bracket["rowsDifferingInWindowMembership"], 0)
        # Tie-break: equal endpoints keep the earlier one, deterministically.
        self.assertEqual(bracket["headline"], "earliest")

    def test_sol_repro_displayed_front_edge_row_beats_an_undisplayed_tail_row(self) -> None:
        # HARNESS-4 (sol BLOCKER on #163): earliest admits only the undisplayed tail row (41550 ms), latest admits only the
        # displayed front-edge row (1600 ms). Ranking by presented count alone headed the report with earliest -> a false
        # DISPLAY_ASLEEP. Displayed-first ranking heads it with latest -> OK.
        rows = [
            _csv_row(time_in_ms=1600),
            _csv_row(time_in_ms=41550, between_display_change="NA", until_displayed="NA"),
        ]
        path = self._write_csv(rows)

        report = self.call(path, _result_json(), capture_start=self._EARLIEST, latest_capture_start=self._LATEST)

        self.assertEqual(report["status"], "OK", report)
        bracket = report["clockBracket"]
        self.assertEqual(bracket["earliest"]["displayedCount"], 0)
        self.assertEqual(bracket["latest"]["displayedCount"], 1)
        self.assertEqual(bracket["headline"], "latest")

    def test_fable_repro_more_presented_but_undisplayed_endpoint_does_not_head(self) -> None:
        # HARNESS-4 (fable HARDENING on #163): earliest admits more PRESENTED rows (all undisplayed), latest admits fewer but
        # displayed ones -> not DISPLAY_ASLEEP; latest heads.
        rows = [
            # 41600/41700/41800 ms: inside the earliest window [2000, 42000] only (latest is [1500, 41500]).
            _csv_row(time_in_ms=41600, between_display_change="NA", until_displayed="NA"),
            _csv_row(time_in_ms=41700, between_display_change="NA", until_displayed="NA"),
            _csv_row(time_in_ms=41800, between_display_change="NA", until_displayed="NA"),
            _csv_row(time_in_ms=1600),
            _csv_row(time_in_ms=1800),
        ]
        path = self._write_csv(rows)

        report = self.call(path, _result_json(), capture_start=self._EARLIEST, latest_capture_start=self._LATEST)

        self.assertEqual(report["status"], "OK", report)
        bracket = report["clockBracket"]
        self.assertGreater(bracket["earliest"]["presentedCount"], bracket["latest"]["presentedCount"])
        self.assertEqual(bracket["earliest"]["displayedCount"], 0)
        self.assertEqual(bracket["headline"], "latest")

    def test_display_asleep_still_reports_the_bracket(self) -> None:
        rows = [_csv_row(between_display_change="0", until_displayed="0", time_in_ms=5000)]
        path = self._write_csv(rows)

        report = self.call(path, _result_json(), capture_start=self._EARLIEST, latest_capture_start=self._LATEST)

        self.assertEqual(report["status"], "DISPLAY_ASLEEP")
        self.assertIsNotNone(report["clockBracket"])
        self.assertEqual(report["clockBracket"]["earliest"]["presentedCount"], 1)


@requires_pwsh
class IntervalStatsFilterFixtureTests(_ReportCase):
    """CUDA-PERF-DISPLAY-IDENTITY-HARNESS-3 (sol BLOCKER): a row displayed only via
    MsUntilDisplayed (MsBetweenDisplayChange NA) carries msBetweenDisplayChange=$null in
    .selectedChainRows. [double]$null coerces to 0.0 in PowerShell, so feeding that array
    straight into the job's own Get-Stats turned a row with NO interval into a spuriously fast
    (0ms) one, inflating fpsEquivalentMean and counting a non-interval row as a positive sample
    (sol repro: [null,16.6] published meanMs=8.3/fpsEquivalentMean=120.48 instead of
    meanMs=16.6/fpsEquivalentMean=60.24). This class EXECUTES the job template's own
    Get-Mean/Get-SampleSd/Get-Percentile/Get-Stats source and its own $pmIntervalRows filter
    expression -- extracted verbatim from playback-attr-3-cuda-job.ps1, never hand-reimplemented
    -- against the module's real output for the exact real-28-column NA-first-row fixture, so a
    regression in either file's actual text fails this test."""

    @classmethod
    def setUpClass(cls) -> None:
        text = ATTRIBUTION_GENERATOR.read_text(encoding="utf-8")
        stats_start = text.index("function Get-Mean(")
        stats_end = text.index("function Start-PresentMonCapture(")
        cls.stats_source = text[stats_start:stats_end]
        filter_start = text.index("$pmIntervalRows = @(")
        filter_end = text.index("\n", filter_start)
        cls.filter_line = text[filter_start:filter_end]
        assert "function Get-Stats(" in cls.stats_source, cls.stats_source
        assert "$null -ne $_.msBetweenDisplayChange -and $_.msBetweenDisplayChange -gt 0" in cls.filter_line, cls.filter_line

    def _run_stats(self, csv_path: Path, result_json: dict) -> dict:
        out_path = self.tmp / "stats-out.json"
        result_json_path = self.tmp / "result.json"
        result_json_path.write_text(json.dumps(result_json), encoding="utf-8")
        script = self.tmp / "probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{MODULE}' -Force\n"
            f"{self.stats_source}\n"
            f"$captureStart = [datetime]::Parse('{CAPTURE_START_UTC}', $null, "
            "[Globalization.DateTimeStyles]::RoundtripKind)\n"
            f"$csvPath = '{csv_path}'\n"
            f"$resultJson = (Get-Content -LiteralPath '{result_json_path}' -Raw | ConvertFrom-Json)\n"
            "$report = Get-AttrCudaPresentMonDisplayReport -CsvPath $csvPath -ResultJson $resultJson "
            "-EarliestCaptureStartUtc $captureStart -LatestCaptureStartUtc $captureStart\n"
            "$pmRows = @($report.selectedChainRows)\n"
            f"{self.filter_line}\n"
            "$pmStats = Get-Stats @($pmIntervalRows | ForEach-Object { [double]$_.msBetweenDisplayChange })\n"
            "[pscustomobject]@{ pmRowsCount = $pmRows.Count; pmIntervalRowsCount = $pmIntervalRows.Count; "
            "pmStats = $pmStats } | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath "
            f"'{out_path}' -Encoding UTF8\n"
            "Write-Output 'PROBE_DONE'\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PROBE_DONE", proc.stdout, proc.stdout + proc.stderr)
        return json.loads(out_path.read_text(encoding="utf-8"))

    def test_the_na_first_row_interval_is_excluded_before_get_stats(self) -> None:
        # sol's exact repro shape: two in-window displayed rows, row 0 NA/MsUntilDisplayed=8.3,
        # row 1 a real 16.6ms interval.
        rows = [
            _real_csv_row(time_in_ms=5000, between_display_change="NA", until_displayed="8.3"),
            _real_csv_row(time_in_ms=5017, between_display_change="16.6", until_displayed="16.6"),
        ]
        path = self._write_real_csv(rows)

        result = self._run_stats(path, _result_json())

        self.assertEqual(result["pmRowsCount"], 2, result)
        # The NA-first row is excluded from the interval population -- only the one real
        # interval remains, never coerced to a spurious 0.0.
        self.assertEqual(result["pmIntervalRowsCount"], 1, result)
        stats = result["pmStats"]
        self.assertEqual(stats["count"], 1)
        self.assertAlmostEqual(stats["meanMs"], 16.6)
        self.assertAlmostEqual(stats["fpsEquivalentMean"], 1000.0 / 16.6)
        # Pre-fix behaviour (regression guard): [double]$null coercing to 0.0 would have produced
        # meanMs=8.3 and fpsEquivalentMean~120.48 from count=2 -- assert those are NOT what a
        # future regression could silently reproduce.
        self.assertNotAlmostEqual(stats["meanMs"], 8.3)
        self.assertNotAlmostEqual(stats["fpsEquivalentMean"], 1000.0 / 8.3)


@requires_pwsh
class PresentModeBreakdownFixtureTests(_ReportCase):
    """PRESENTMON-HARNESS-ROBUSTNESS-1: every chain -- the audit list and the MLVApp PID-level
    aggregate alike -- records every PresentMode it observed with its own row count. Counts
    alone cannot explain WHY a leg lost most of its PresentMon samples (the "very thin
    admitted-row count" gap disclosed on CUDA-PLAYBACK-FULLSCREEN-UI-1 r2b); a PresentMode drop
    (e.g. Hardware: Independent Flip falling to Composed: Flip mid-capture) is a genuine signal
    PresentMon itself already reports, so this makes it visible in the evidence a future
    full-screen leg publishes instead of needing a live repro to diagnose."""

    def test_the_mlvapp_chain_reports_every_present_mode_with_its_own_count(self) -> None:
        rows = [
            _csv_row(present_mode="Hardware: Independent Flip", time_in_ms=5000 + i * 1000)
            for i in range(3)
        ]
        rows += [
            _csv_row(present_mode="Composed: Flip", time_in_ms=9000 + i * 1000)
            for i in range(2)
        ]
        path = self._write_csv(rows)

        report = self.call(path, _result_json())

        self.assertEqual(report["status"], "OK", report)
        modes = {m["presentMode"]: m["count"] for m in report["selectedChain"]["presentModes"]}
        self.assertEqual(modes, {"Hardware: Independent Flip": 3, "Composed: Flip": 2})
        # The per-chain audit entry (report["chains"]) must agree with the aggregate -- there is
        # only one chain here, so its breakdown is identical.
        chain_modes = {m["presentMode"]: m["count"] for m in report["chains"][0]["presentModes"]}
        self.assertEqual(chain_modes, modes)

    def test_a_foreign_chain_carries_its_own_present_modes_independently(self) -> None:
        rows = [_csv_row(time_in_ms=5000)]  # MLVApp's own default mode, 1 row
        rows += [
            _csv_row(
                application="dwm.exe", process_id=999, swap_chain="0xBBB",
                present_mode="Composed: Copy with GPU GDI", time_in_ms=6000 + i * 1000,
            )
            for i in range(4)
        ]
        path = self._write_csv(rows)

        report = self.call(path, _result_json())

        self.assertEqual(report["status"], "OK", report)
        foreign = next(c for c in report["chains"] if not c["isMlvAppChain"])
        self.assertEqual(
            foreign["presentModes"], [{"presentMode": "Composed: Copy with GPU GDI", "count": 4}]
        )
        mlvapp_modes = {m["presentMode"]: m["count"] for m in report["selectedChain"]["presentModes"]}
        self.assertEqual(mlvapp_modes, {"Hardware: Independent Flip": 1})

    def test_a_thin_single_row_leg_still_reports_its_one_present_mode(self) -> None:
        # The exact shape of the disclosed full-screen evidence gap: presentedCount=1,
        # displayedCount=1 -- this is the one piece of context that could have explained it.
        # (No comma in the mode string: this fixture's own writer is a plain unquoted CSV join,
        # so a comma inside a field value would corrupt the column count -- unrelated to the
        # function under test.)
        path = self._write_csv([_csv_row(present_mode="Hardware: Legacy Flip Independent Flip", time_in_ms=5000)])

        report = self.call(path, _result_json())

        self.assertEqual(report["status"], "OK", report)
        self.assertEqual(report["selectedChain"]["presentedCount"], 1)
        self.assertEqual(
            report["selectedChain"]["presentModes"],
            [{"presentMode": "Hardware: Legacy Flip Independent Flip", "count": 1}],
        )


@requires_pwsh
class PresentMonStatusFixtureTests(_ReportCase):
    """PRESENTMON-HARNESS-ROBUSTNESS-1/2(r1)/2(r1b): the job's own presentMonStatus/-Reason
    assignment, EXECUTED verbatim from playback-attr-3-cuda-job.ps1 (never hand-reimplemented)
    against the module's real output. 'ok' requires THREE independent arms together: count,
    app-swap coverage (against an app-side swap/frame count read from the run log --
    Get-AttrCudaAppSwapTelemetry -- never PresentMon's own presentedCount), and temporal (no gap
    between positive-interval rows, including the window's own head/tail bounds, exceeds a stated
    ceiling). Each arm is isolated in its own fixture below, alongside round 1's pre-existing
    zero-interval and thin-count cases."""

    @classmethod
    def setUpClass(cls) -> None:
        text = ATTRIBUTION_GENERATOR.read_text(encoding="utf-8")
        start = text.index("$presentMonSufficiencyMinIntervalCount = 30")
        end = text.index("\n\n$dllSha256Lower", start)
        cls.status_source = text[start:end]
        assert "'degraded'" in cls.status_source, cls.status_source
        assert "'ok'" in cls.status_source, cls.status_source

    @staticmethod
    def _gpu_window_swaps_line(swaps: int) -> str:
        # platform/qt/MainWindow.cpp's real field set (GpuDisplayWindow::swapTelemetrySnapshot) --
        # only telemetry_enabled/window_active/swaps are read by Get-AttrCudaAppSwapTelemetry, but
        # every field is included for realism.
        return (
            "playback_smoke.gpu_window_swaps session=1 window_active=1 telemetry_enabled=1 "
            f"swaps={swaps} swap_fps=1.0 max_gap_ms=1.0 max_gap_before_serial=1 "
            f"max_gap_after_serial=2 frames_presented={swaps} swaps_minus_frames_presented=0 "
            "head_gap_ms=1.0 tail_gap_ms=1.0 first_swap_utc=2026-01-01T00:00:02.0000000Z "
            "last_swap_utc=2026-01-01T00:00:42.0000000Z"
        )

    @staticmethod
    def _gate_line(frames_presented: int) -> str:
        return (
            "playback_smoke.gate session=1 verdict=0 "
            f"frames_presented={frames_presented} decode_requests_issued={frames_presented} "
            f"parity_match_count={frames_presented} frames_expected={frames_presented}"
        )

    @staticmethod
    def _evenly_spaced_times(count: int, *, start_ms: float = 2000.0, end_ms: float = 42000.0) -> list[float]:
        # Spans the whole [start_ms, end_ms] window (WINDOW_START_UTC/WINDOW_END_UTC below), so
        # every internal AND head/tail gap is span / (count - 1) -- comfortably under the 5000ms
        # temporal ceiling for any count used in this class's "clears everything but coverage/
        # count" fixtures.
        step = (end_ms - start_ms) / (count - 1)
        return [start_ms + i * step for i in range(count)]

    def _run_status(self, csv_path: Path, result_json: dict, *, raw_log: str = "") -> dict:
        out_path = self.tmp / "status-out.json"
        result_json_path = self.tmp / "result.json"
        result_json_path.write_text(json.dumps(result_json), encoding="utf-8")
        log_path = self.tmp / "raw.log"
        log_path.write_text(raw_log, encoding="utf-8")
        script = self.tmp / "probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{MODULE}' -Force\n"
            f"$captureStart = [datetime]::Parse('{CAPTURE_START_UTC}', $null, "
            "[Globalization.DateTimeStyles]::RoundtripKind)\n"
            f"$csvPath = '{csv_path}'\n"
            f"$resultJson = (Get-Content -LiteralPath '{result_json_path}' -Raw | ConvertFrom-Json)\n"
            # PRESENTMON-HARNESS-ROBUSTNESS-2 r1b: the status_source below now also reads $rawLog
            # (Get-AttrCudaAppSwapTelemetry) and $displayReport.windowStartMs/windowEndMs (Get-
            # AttrCudaTemporalCoverage) -- Get-Content -Raw on an empty file returns $null, so an
            # empty-string fixture is normalized back to '' rather than letting $rawLog go null.
            f"$rawLog = Get-Content -LiteralPath '{log_path}' -Raw\n"
            "if ($null -eq $rawLog) { $rawLog = '' }\n"
            # Named $displayReport, matching the real job's own variable name -- the extracted
            # status_source below (PRESENTMON-HARNESS-ROBUSTNESS-2) dot-accesses
            # $displayReport.selectedChain.presentedCount, so this probe must use the same name,
            # not the $report alias the sibling stats-only probe above uses.
            "$displayReport = Get-AttrCudaPresentMonDisplayReport -CsvPath $csvPath -ResultJson $resultJson "
            "-EarliestCaptureStartUtc $captureStart -LatestCaptureStartUtc $captureStart\n"
            "$pmRows = @($displayReport.selectedChainRows)\n"
            "$pmIntervalRows = @($pmRows | Where-Object "
            "{ $null -ne $_.msBetweenDisplayChange -and $_.msBetweenDisplayChange -gt 0 })\n"
            f"{self.status_source}\n"
            "[pscustomobject]@{ presentMonStatus = $presentMonStatus; "
            "presentMonStatusReason = $presentMonStatusReason } | ConvertTo-Json -Depth 10 | "
            f"Set-Content -LiteralPath '{out_path}' -Encoding UTF8\n"
            "Write-Output 'PROBE_DONE'\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PROBE_DONE", proc.stdout, proc.stdout + proc.stderr)
        return json.loads(out_path.read_text(encoding="utf-8"))

    def test_degraded_when_the_only_displayed_row_has_no_interval(self) -> None:
        # sol's exact repro shape (see IntervalStatsFilterFixtureTests above), but this class
        # asserts on the STATUS the job now derives from it, not just the stats.
        row = _real_csv_row(time_in_ms=5000, between_display_change="NA", until_displayed="8.3")
        path = self._write_real_csv([row])

        result = self._run_status(path, _result_json())

        self.assertEqual(result["presentMonStatus"], "degraded", result)
        self.assertIsNotNone(result["presentMonStatusReason"])
        self.assertIn("no interval to compute cadence", result["presentMonStatusReason"])
        self.assertIn("display itself is still confirmed", result["presentMonStatusReason"])

    def test_a_thin_full_screen_style_single_row_leg_is_degraded_not_ok(self) -> None:
        # The disclosed CUDA-PLAYBACK-FULLSCREEN-UI-1 r2b evidence shape: presentedCount=1,
        # displayedCount=1, first present of the capture (MsBetweenDisplayChange NA).
        row = _real_csv_row(time_in_ms=5000, between_display_change="NA", until_displayed="16.6")
        path = self._write_real_csv([row])

        result = self._run_status(path, _result_json())

        self.assertEqual(result["presentMonStatus"], "degraded", result)

    def test_degraded_when_only_a_single_positive_interval_sample_exists(self) -> None:
        # PRESENTMON-HARNESS-ROBUSTNESS-2 (sol BLOCKER, PR #174 r1): this is exactly the
        # round-1 fixture that used to read 'ok' -- a single positive interval over a whole leg
        # cannot establish cadence, and must now read 'degraded' under the sufficiency gate
        # (1 positive-interval row is far below the >= 30 minimum count). No raw log is supplied,
        # so the app-swap coverage arm fails too (no independent telemetry) -- both arms are named.
        rows = [
            _real_csv_row(time_in_ms=5000, between_display_change="NA", until_displayed="8.3"),
            _real_csv_row(time_in_ms=5017, between_display_change="16.6", until_displayed="16.6"),
        ]
        path = self._write_real_csv(rows)

        result = self._run_status(path, _result_json())

        self.assertEqual(result["presentMonStatus"], "degraded", result)
        self.assertIsNotNone(result["presentMonStatusReason"])
        self.assertIn("count: only 1 positive-interval row(s), below the minimum of 30", result["presentMonStatusReason"])

    def test_degraded_when_a_handful_of_displayed_rows_all_carry_real_intervals(self) -> None:
        # Every displayed row here DOES carry a genuine interval (unlike the two tests above) --
        # this isolates the count-threshold arm of the gate: 5 real samples still is not enough
        # to corroborate cadence over a whole leg (round-1's own fixture used to read 'ok' here).
        rows = [_csv_row(time_in_ms=5000 + i * 1000) for i in range(5)]
        path = self._write_csv(rows)

        result = self._run_status(path, _result_json())

        self.assertEqual(result["presentMonStatus"], "degraded", result)
        self.assertIn("count: only 5 positive-interval row(s), below the minimum of 30", result["presentMonStatusReason"])

    def test_ok_once_all_three_sufficiency_arms_are_cleared(self) -> None:
        # PRESENTMON-HARNESS-ROBUSTNESS-2 r1b: a healthy windowed leg shape -- 40 positive-interval
        # rows (well above the count floor) spread evenly across the whole 40s window (so every
        # gap, including head/tail, is far under the 5000ms temporal ceiling) with an app-side
        # swap count (42) close enough to clear the 50% coverage floor (40/42 ~= 95.2%).
        times = self._evenly_spaced_times(40)
        rows = [_csv_row(time_in_ms=t) for t in times]
        path = self._write_csv(rows)
        raw_log = self._gpu_window_swaps_line(42)

        result = self._run_status(path, _result_json(), raw_log=raw_log)

        self.assertEqual(result["presentMonStatus"], "ok", result)
        self.assertIsNone(result["presentMonStatusReason"])

    def test_degraded_when_a_captured_prefix_is_followed_by_silence(self) -> None:
        # PRESENTMON-HARNESS-ROBUSTNESS-2 r1b (sol BLOCKER, pre-review): count and app-swap
        # coverage alone cannot catch this shape -- 30 rows (clears the count floor) captured in
        # the first ~1.45s of a 40s window, matched exactly by an app-side swap count of 30
        # (coverage = 30/30 = 100%, clearing that floor too), then silence for the remaining ~38.5s
        # of the leg. Only the temporal arm fails, and it is named alone.
        times = [2000.0 + i * 50.0 for i in range(30)]
        rows = [_csv_row(time_in_ms=t) for t in times]
        path = self._write_csv(rows)
        raw_log = self._gpu_window_swaps_line(30)

        result = self._run_status(path, _result_json(), raw_log=raw_log)

        self.assertEqual(result["presentMonStatus"], "degraded", result)
        reason = result["presentMonStatusReason"]
        self.assertIn("temporal:", reason)
        self.assertIn("s tail gap between positive-interval rows exceeds the 5s ceiling", reason)
        self.assertNotIn("count:", reason)
        self.assertNotIn("app-swap coverage:", reason)

    def test_degraded_when_count_and_temporal_clear_but_app_swap_coverage_does_not(self) -> None:
        # PRESENTMON-HARNESS-ROBUSTNESS-2 r1b (sol BLOCKER, pre-review): 35 positive-interval rows
        # (clears count) spread evenly across the whole window (clears temporal) against an
        # app-side swap count of 200 -- far more real on-screen swaps than PresentMon captured
        # (coverage = 35/200 = 17.5%, below the 50% floor). Only the coverage arm fails.
        times = self._evenly_spaced_times(35)
        rows = [_csv_row(time_in_ms=t) for t in times]
        path = self._write_csv(rows)
        raw_log = self._gpu_window_swaps_line(200)

        result = self._run_status(path, _result_json(), raw_log=raw_log)

        self.assertEqual(result["presentMonStatus"], "degraded", result)
        reason = result["presentMonStatusReason"]
        self.assertIn(
            "app-swap coverage: only 35 positive-interval row(s) out of 200 app-side gpu_window_swaps (17.5%)",
            reason,
        )
        self.assertNotIn("count:", reason)
        self.assertNotIn("temporal:", reason)

    def test_degraded_when_no_app_side_telemetry_is_in_the_log_at_all(self) -> None:
        # PRESENTMON-HARNESS-ROBUSTNESS-2 r1b: count and temporal both clear, but the log carries
        # NEITHER playback_smoke.gpu_window_swaps NOR playback_smoke.gate -- coverage is
        # unmeasurable, which fails the gate rather than being treated as 0% or 100% coverage.
        times = self._evenly_spaced_times(40)
        rows = [_csv_row(time_in_ms=t) for t in times]
        path = self._write_csv(rows)

        result = self._run_status(path, _result_json(), raw_log="")

        self.assertEqual(result["presentMonStatus"], "degraded", result)
        reason = result["presentMonStatusReason"]
        self.assertIn(
            "app-swap coverage: no independent app-side swap or frame telemetry found in the run log "
            "(neither playback_smoke.gpu_window_swaps nor playback_smoke.gate)",
            reason,
        )
        self.assertNotIn("count:", reason)
        self.assertNotIn("temporal:", reason)

    def test_ok_via_the_gate_frames_presented_fallback_when_swap_telemetry_is_absent(self) -> None:
        # PRESENTMON-HARNESS-ROBUSTNESS-2 r1b: no playback_smoke.gpu_window_swaps line at all
        # (swap telemetry off/unavailable) but playback_smoke.gate's frames_presented (always
        # logged, LIGHT arm included) is -- Get-AttrCudaAppSwapTelemetry falls back to it, and a
        # leg that otherwise clears count/temporal can still read 'ok' through that fallback.
        times = self._evenly_spaced_times(40)
        rows = [_csv_row(time_in_ms=t) for t in times]
        path = self._write_csv(rows)
        raw_log = self._gate_line(42)

        result = self._run_status(path, _result_json(), raw_log=raw_log)

        self.assertEqual(result["presentMonStatus"], "ok", result)
        self.assertIsNone(result["presentMonStatusReason"])


@requires_pwsh
class DisplayFailurePresentMonStatusFixtureTests(_ReportCase):
    """PRESENTMON-HARNESS-ROBUSTNESS-2 (fable note): the display-failure branch's own
    presentMonStatus derivation, EXECUTED verbatim from playback-attr-3-cuda-job.ps1 -- never
    hand-reimplemented. DISPLAY_ASLEEP (PresentMon AFFIRMATIVELY measuring zero displayed frames,
    a verified negative) must read 'verified_zero_displayed', while PRESENTMON_UNAVAILABLE (and
    any other non-OK status) keeps the coarse 'unavailable' the other two refusal branches
    (spawn, wait) still tag literally."""

    @classmethod
    def setUpClass(cls) -> None:
        text = ATTRIBUTION_GENERATOR.read_text(encoding="utf-8")
        cls.derivation = (
            "$displayFailurePresentMonStatus = if ($displayReport.status -eq 'DISPLAY_ASLEEP') "
            "{ 'verified_zero_displayed' } else { 'unavailable' }"
        )
        assert cls.derivation in text, "the generator's display-failure status derivation moved or changed"

    def _run(self, status: str) -> str:
        out_path = self.tmp / "status.txt"
        script = self.tmp / "probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"$displayReport = [pscustomobject]@{{ status = '{status}' }}\n"
            f"{self.derivation}\n"
            f"Set-Content -LiteralPath '{out_path}' -Value $displayFailurePresentMonStatus -NoNewline\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return out_path.read_text(encoding="utf-8")

    def test_display_asleep_reads_verified_zero_displayed(self) -> None:
        self.assertEqual(self._run("DISPLAY_ASLEEP"), "verified_zero_displayed")

    def test_presentmon_unavailable_keeps_the_coarse_unavailable_status(self) -> None:
        self.assertEqual(self._run("PRESENTMON_UNAVAILABLE"), "unavailable")


if __name__ == "__main__":
    unittest.main()
