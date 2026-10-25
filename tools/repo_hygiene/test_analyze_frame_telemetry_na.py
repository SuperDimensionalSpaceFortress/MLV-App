"""PROCESSED8-MS-TELEMETRY-STUCK-1 r2: analyze-frame-telemetry.py never scores `na` as zero.

The app prints `processed8_ms=na` (and the processed8/processed16 siblings) on a
playback_smoke.frame row whose render did not run that stage. The analyzer used to drop
the non-numeric cell and then substitute 0.0, so an all-`na` field reported 0.0 ms and a
mixed log was biased toward zero. A `na` cell is UNMEASURED: percentiles, means and the
slowest-decile stage split use measured rows only, per field; a field with no measured row
prints `na`; and a log with any `na` cell names each field's measured row count. A log with
no `na` cell prints exactly what it printed before.
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ANALYZER = REPO_ROOT / "tools" / "profiling" / "analyze-frame-telemetry.py"

PROCESSED_FIELDS = (
    "processed8_ms",
    "processed8_setup_ms",
    "processed8_core_math_ms",
    "processed8_local_tone_ms",
    "processed8_threading_overhead_ms",
)


def frame_line(index: int, cells: dict[str, str]) -> str:
    body = " ".join(f"{key}={value}" for key, value in cells.items())
    return (
        f"2026-10-21T18:00:{index % 60:02d}Z [info] playback_smoke.frame session=1 "
        f"index={index} {body}\n"
    )


def base_cells(index: int) -> dict[str, str]:
    return {
        "interval_ms": f"{16.0 + index % 5:.3f}",
        "render_total_ms": f"{10.0 + index:.3f}",
        "render_work_ms": f"{5.0 + index * 0.5:.3f}",
        "queue_wait_ms": f"{index % 3:.3f}",
        "llrawproc_ms": f"{2.0 + index * 0.1:.3f}",
        "draw_total_ms": "1.500",
    }


def all_na_log() -> str:
    lines = []
    for index in range(20):
        cells = base_cells(index)
        for field in PROCESSED_FIELDS:
            cells[field] = "na"
        cells["processed8_measured"] = "0"
        lines.append(frame_line(index, cells))
    return "".join(lines)


def mixed_log() -> str:
    # 100 rows; every tenth row measured processed8 at 20 ms, the other 90 are `na`.
    # render_total grows with the index, so the slowest decile is rows 90..99, which
    # holds exactly one measured row (index 90).
    lines = []
    for index in range(100):
        cells = base_cells(index)
        measured = index % 10 == 0
        for field in PROCESSED_FIELDS:
            cells[field] = "20.000" if measured else "na"
        cells["processed8_measured"] = "1" if measured else "0"
        lines.append(frame_line(index, cells))
    return "".join(lines)


def numeric_only_log() -> str:
    lines = []
    for index in range(20):
        cells = base_cells(index)
        cells["processed8_ms"] = f"{3.0 + index % 4:.3f}"
        cells["processed8_core_math_ms"] = f"{1.0 + index % 2:.3f}"
        lines.append(frame_line(index, cells))
    lines.insert(5, "2026-10-21T18:00:05Z [info] playback_smoke.summary session=1 frames=20\n")
    return "".join(lines)


# Output of the pre-r2 analyzer (ac98569b) on numeric_only_log(), path replaced by <LOG>.
NUMERIC_ONLY_EXPECTED = (
    "\n"
    "=== <LOG>  (frames=20) ===\n"
    "field                                    p50     p90     p99     max\n"
    "interval_ms                             18.0    20.0    20.0    20.0\n"
    "render_total_ms                         19.5    27.1    28.8    29.0\n"
    "render_work_ms                           9.8    13.6    14.4    14.5\n"
    "queue_wait_ms                            1.0     2.0     2.0     2.0\n"
    "llrawproc_ms                             3.0     3.7     3.9     3.9\n"
    "processed8_ms                            4.5     6.0     6.0     6.0\n"
    "draw_total_ms                            1.5     1.5     1.5     1.5\n"
    "processed8_core_math_ms                  1.5     2.0     2.0     2.0\n"
    "slowest-decile (n=2) render_total avg 28.5 ms split:\n"
    "  decode+debayer+scale (residual)    4.9 ms   17.2%\n"
    "  llrawproc (recon etc.)             3.8 ms   13.5%\n"
    "  processed8 (processing+pack)       5.5 ms   19.3%\n"
    "  queue_wait                         0.5 ms\n"
    "  draw_total (present side)          1.5 ms\n"
    "  worst interval in decile          20.0 ms\n"
)


def run_analyzer(log_text: str) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "mlvapp.log"
        log.write_text(log_text, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-I", str(ANALYZER), str(log)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(f"analyzer rc={result.returncode}\n{result.stderr}")
        return result.stdout.replace(str(log), "<LOG>")


def table_row(output: str, field: str) -> str:
    rows = [line for line in output.splitlines() if line.split()[:1] == [field]]
    if len(rows) != 1:
        raise AssertionError(f"expected one table row for {field}, got {rows!r}\n{output}")
    return rows[0]


def split_row(output: str, label: str) -> str:
    rows = [line for line in output.splitlines() if line.startswith(f"  {label}")]
    if len(rows) != 1:
        raise AssertionError(f"expected one split row for {label!r}, got {rows!r}\n{output}")
    return rows[0]


class AllNaFieldTests(unittest.TestCase):
    def setUp(self) -> None:
        self.output = run_analyzer(all_na_log())

    def test_all_na_field_prints_na_with_zero_measured_rows(self) -> None:
        for field in PROCESSED_FIELDS:
            row = table_row(self.output, field)
            self.assertRegex(row, rf"^{field}\s+na\s+na\s+na\s+na\s+\(measured 0/20\)$")

    def test_decile_split_never_scores_unmeasured_processed8_as_zero(self) -> None:
        row = split_row(self.output, "processed8 (processing+pack)")
        self.assertNotIn("0.0", row)
        self.assertRegex(row, r"\sna\s+\(measured 0/2\)$")
        residual = split_row(self.output, "decode+debayer+scale (residual)")
        self.assertRegex(residual, r"\sna\s+\(measured 0/2\)$")

    def test_measured_fields_name_their_full_count(self) -> None:
        self.assertRegex(table_row(self.output, "render_total_ms"), r"\(measured 20/20\)$")
        self.assertRegex(
            split_row(self.output, "llrawproc (recon etc.)"), r"%\s+\(measured 2/2\)$"
        )


class MixedMeasuredFieldTests(unittest.TestCase):
    def setUp(self) -> None:
        self.output = run_analyzer(mixed_log())

    def test_percentiles_use_measured_rows_only(self) -> None:
        row = table_row(self.output, "processed8_ms")
        self.assertRegex(
            row, r"^processed8_ms\s+20\.0\s+20\.0\s+20\.0\s+20\.0\s+\(measured 10/100\)$"
        )

    def test_decile_split_uses_measured_rows_only(self) -> None:
        row = split_row(self.output, "processed8 (processing+pack)")
        # One measured row (index 90, 20 ms) out of the ten slowest; render_total avg 104.5.
        self.assertRegex(row, r"^\s+processed8 \(processing\+pack\)\s+20\.0 ms\s+19\.1%\s+\(measured 1/10\)$")

    def test_residual_subtracts_only_rows_where_every_term_is_measured(self) -> None:
        row = split_row(self.output, "decode+debayer+scale (residual)")
        # Row 90: render_work 50.0 - llrawproc 11.0 - processed8 20.0 = 19.0.
        self.assertRegex(row, r"\s19\.0 ms\s+18\.2%\s+\(measured 1/10\)$")


RATIO_DISCLOSURE = (
    "  note: % = ratio of measured-only mean to all-frame render mean; "
    "not additive when coverage < 100 %\n"
)


class RatioDisclosureTests(unittest.TestCase):
    def test_mixed_coverage_split_discloses_the_percentages_are_ratios(self) -> None:
        output = run_analyzer(mixed_log())
        self.assertIn(RATIO_DISCLOSURE, output)
        # The note closes the split block, after its last row.
        self.assertTrue(output.endswith(RATIO_DISCLOSURE), output)
        # Disclosure is label-only: the percentage cells keep their pre-note values.
        self.assertRegex(
            split_row(output, "processed8 (processing+pack)"), r"\s20\.0 ms\s+19\.1%\s+\(measured 1/10\)$"
        )

    def test_full_coverage_logs_carry_no_disclosure(self) -> None:
        self.assertNotIn("note:", run_analyzer(numeric_only_log()))
        self.assertNotIn("note:", run_analyzer(all_na_log()))


class NumericOnlyLogUnchangedTests(unittest.TestCase):
    def test_numeric_only_output_is_byte_identical_to_the_pre_na_analyzer(self) -> None:
        self.assertEqual(NUMERIC_ONLY_EXPECTED, run_analyzer(numeric_only_log()))
        self.assertNotIn("measured", NUMERIC_ONLY_EXPECTED)


if __name__ == "__main__":
    unittest.main()
