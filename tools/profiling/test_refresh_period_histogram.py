"""Falsifier tests for refresh_period_histogram.

Each test is written to FAIL if the matching rule quietly degrades to something
weaker -- treating a missing column as zeros, silently dropping an empty series to an
empty-but-successful report, or mis-rounding a refresh multiple at the bucket boundary.
"""
from __future__ import annotations

import csv
import json

import pytest

from refresh_period_histogram import (
    RefreshHistogramError,
    ROUNDING_RULE,
    bucket_label,
    build_report,
    compute_buckets,
    compute_refresh_period,
    compute_region_stats,
    main,
    parse_frame_log_rows,
    parse_presentmon_intervals,
    percentile,
    refresh_multiple,
    resolve_presentmon_status_from_artifacts,
)


def _write_presentmon_csv(path, rows, column="msBetweenDisplayChange"):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["ordinal", "timeInMs", column, "displayFpsEquivalent", "presentMode"])
        for i, value in enumerate(rows):
            writer.writerow([i, i * 16.67, value, 1000.0 / value if value else "", "Hardware: Independent Flip"])


def _frame_log_line(index: int, total_ms: float, **overrides) -> str:
    fields = {
        "prep_region_setup_ms": total_ms,
        "prep_region_gpu_ms": total_ms,
        "prep_region_image_ms": total_ms,
        "prep_region_present_ms": total_ms,
        "prep_region_finish_ms": total_ms,
        "prep_region_total_ms": total_ms,
        "prep_region_unattributed_ms": total_ms,
    }
    fields.update(overrides)
    kv = " ".join(f"{k}={v}" for k, v in fields.items())
    return (
        f"2026-09-16 00:00:00 qInfo() playback_smoke.frame session=1 index={index} "
        f"elapsed_ms=1.0 interval_ms=16.67 display_frame={index} serial={index} {kv}"
    )


def _write_frame_log(path, count=10, start=1):
    with open(path, "w", encoding="utf-8") as fh:
        for i in range(count):
            fh.write(_frame_log_line(i, float(start + i)))
            fh.write("\n")


# --- refresh multiple / bucket rounding -------------------------------------------

def test_rounding_rule_round_half_up_at_boundary():
    # exactly 1.5x the refresh period must round UP to 2 (round-half-up), not down to 1
    assert refresh_multiple(24.0, 16.0) == 2
    assert refresh_multiple(23.9, 16.0) == 1


def test_refresh_multiple_never_reports_zero():
    assert refresh_multiple(0.1, 16.6667) == 1


def test_bucket_label_groups_three_and_above():
    assert bucket_label(1) == "1"
    assert bucket_label(2) == "2"
    assert bucket_label(3) == "3+"
    assert bucket_label(7) == "3+"


def test_rounding_rule_is_stated_in_the_output():
    assert "round-half-up" in ROUNDING_RULE
    assert "3+" in ROUNDING_RULE


# --- known 1/2/3 mix ----------------------------------------------------------------

def test_known_1_2_3_mix_buckets_correctly(tmp_path):
    csv_path = tmp_path / "presentmon-series.csv"
    values = [16.67] * 12 + [33.34] * 5 + [50.01] * 3
    _write_presentmon_csv(csv_path, values)

    intervals = parse_presentmon_intervals(str(csv_path))
    assert len(intervals) == 20

    refresh = compute_refresh_period(intervals)
    assert refresh["measurement"] == "mode"
    assert refresh["refreshPeriodMs"] == pytest.approx(16.67, abs=0.01)

    buckets = compute_buckets(intervals, refresh["refreshPeriodMs"])
    assert buckets["1"]["count"] == 12
    assert buckets["2"]["count"] == 5
    assert buckets["3+"]["count"] == 3
    assert buckets["1"]["share"] == pytest.approx(0.6)
    assert buckets["2"]["share"] == pytest.approx(0.25)
    assert buckets["3+"]["share"] == pytest.approx(0.15)
    # shares must sum to 1 -- every sample lands in exactly one bucket
    total_share = buckets["1"]["share"] + buckets["2"]["share"] + buckets["3+"]["share"]
    assert total_share == pytest.approx(1.0)


def test_refresh_period_falls_back_to_median_without_a_clear_mode(tmp_path):
    # every value in the 1-refresh cluster is distinct -- no mode has a plurality
    intervals = [16.60, 16.65, 16.70, 16.75, 33.4, 33.4]
    refresh = compute_refresh_period(intervals)
    assert refresh["measurement"] == "median"


# --- histogram ambiguity (all-2-refresh) --------------------------------------------

def test_all_2_refresh_with_nominal_buckets_as_2(tmp_path):
    # every presented frame actually took 2 refreshes; with the nominal (true) refresh
    # period supplied, the estimator is bypassed entirely and every sample buckets "2".
    csv_path = tmp_path / "presentmon-series.csv"
    values = [33.34] * 20
    _write_presentmon_csv(csv_path, values)

    log_path = tmp_path / "mlvapp.log"
    _write_frame_log(log_path, count=10, start=1)

    report = build_report(str(csv_path), str(log_path), presentmon_status="ok", refresh_period_ms=16.67)
    assert report["presentMon"]["refreshPeriodSource"] == "nominal"
    assert report["presentMon"]["refreshPeriodMeasurement"] == "nominal"
    assert report["presentMon"]["refreshPeriodMs"] == pytest.approx(16.67)
    assert report["presentMon"]["buckets"]["2"]["count"] == 20
    assert report["presentMon"]["buckets"]["1"]["count"] == 0


def test_all_2_refresh_without_nominal_is_an_error(tmp_path):
    # without the true refresh period, an all-2-refresh capture is indistinguishable
    # from a healthy all-1-refresh one from the interval distribution alone -- this
    # must fail closed rather than silently report 100% "1".
    csv_path = tmp_path / "presentmon-series.csv"
    values = [33.34] * 20
    _write_presentmon_csv(csv_path, values)

    log_path = tmp_path / "mlvapp.log"
    _write_frame_log(log_path, count=10, start=1)

    with pytest.raises(RefreshHistogramError, match="ambiguous"):
        build_report(str(csv_path), str(log_path), presentmon_status="ok")

    with pytest.raises(RefreshHistogramError, match="ambiguous"):
        compute_refresh_period(values)


# --- --refresh-period-ms rejects non-finite / non-positive values ------------------

def test_refresh_period_ms_rejects_nan(tmp_path):
    csv_path = tmp_path / "presentmon-series.csv"
    _write_presentmon_csv(csv_path, [16.67] * 12 + [33.34] * 5 + [50.01] * 3)
    log_path = tmp_path / "mlvapp.log"
    _write_frame_log(log_path, count=10, start=1)

    with pytest.raises(RefreshHistogramError, match="finite"):
        build_report(str(csv_path), str(log_path), presentmon_status="ok", refresh_period_ms=float("nan"))


def test_refresh_period_ms_rejects_inf(tmp_path):
    csv_path = tmp_path / "presentmon-series.csv"
    _write_presentmon_csv(csv_path, [16.67] * 12 + [33.34] * 5 + [50.01] * 3)
    log_path = tmp_path / "mlvapp.log"
    _write_frame_log(log_path, count=10, start=1)

    with pytest.raises(RefreshHistogramError, match="finite"):
        build_report(str(csv_path), str(log_path), presentmon_status="ok", refresh_period_ms=float("inf"))
    with pytest.raises(RefreshHistogramError, match="finite"):
        build_report(str(csv_path), str(log_path), presentmon_status="ok", refresh_period_ms=float("-inf"))


def test_refresh_period_ms_rejects_non_positive(tmp_path):
    csv_path = tmp_path / "presentmon-series.csv"
    _write_presentmon_csv(csv_path, [16.67] * 12 + [33.34] * 5 + [50.01] * 3)
    log_path = tmp_path / "mlvapp.log"
    _write_frame_log(log_path, count=10, start=1)

    with pytest.raises(RefreshHistogramError, match="positive"):
        build_report(str(csv_path), str(log_path), presentmon_status="ok", refresh_period_ms=0.0)
    with pytest.raises(RefreshHistogramError, match="positive"):
        build_report(str(csv_path), str(log_path), presentmon_status="ok", refresh_period_ms=-5.0)


# --- missing column / empty series are errors, never zeros -------------------------

def test_missing_presentmon_column_is_an_error(tmp_path):
    csv_path = tmp_path / "bad.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["ordinal", "timeInMs", "presentMode"])
        writer.writerow([0, 0.0, "Hardware: Independent Flip"])

    with pytest.raises(RefreshHistogramError, match="missing required column"):
        parse_presentmon_intervals(str(csv_path))


def test_empty_presentmon_series_is_an_error(tmp_path):
    csv_path = tmp_path / "empty.csv"
    _write_presentmon_csv(csv_path, [])

    with pytest.raises(RefreshHistogramError, match="no positive"):
        parse_presentmon_intervals(str(csv_path))


def test_all_zero_presentmon_series_is_an_error(tmp_path):
    # zero/negative values are capture gaps, not zero-cost frames -- must not be
    # silently coerced into a "successful" empty-looking report
    csv_path = tmp_path / "zeros.csv"
    _write_presentmon_csv(csv_path, [0.0, 0.0, -1.0])

    with pytest.raises(RefreshHistogramError, match="no positive"):
        parse_presentmon_intervals(str(csv_path))


def test_empty_frame_log_is_an_error(tmp_path):
    log_path = tmp_path / "empty.log"
    log_path.write_text("nothing to see here\nno frame lines at all\n", encoding="utf-8")

    with pytest.raises(RefreshHistogramError, match="no playback_smoke.frame rows"):
        parse_frame_log_rows(str(log_path))


def test_too_few_frame_rows_is_an_error(tmp_path):
    log_path = tmp_path / "sparse.log"
    _write_frame_log(log_path, count=3)

    with pytest.raises(RefreshHistogramError, match="require >= 10"):
        parse_frame_log_rows(str(log_path))


def test_frame_row_missing_total_is_excluded_not_zeroed(tmp_path):
    log_path = tmp_path / "partial.log"
    with open(log_path, "w", encoding="utf-8") as fh:
        for i in range(24):
            # omit prep_region_total_ms/unattributed_ms on odd rows
            if i % 2 == 1:
                fh.write(
                    f"playback_smoke.frame session=1 index={i} prep_region_setup_ms=1.0\n"
                )
            else:
                fh.write(_frame_log_line(i, float(i + 1)))
                fh.write("\n")

    rows = parse_frame_log_rows(str(log_path))
    assert len(rows) == 12


def test_missing_region_field_is_an_error():
    # a row set that never carries prep_region_gpu_ms must fail, not report a 0.0
    rows = [
        {
            "prep_region_setup_ms": 1.0,
            "prep_region_image_ms": 1.0,
            "prep_region_present_ms": 1.0,
            "prep_region_finish_ms": 1.0,
            "prep_region_total_ms": 1.0,
            "prep_region_unattributed_ms": 0.1,
        }
        for _ in range(10)
    ]
    with pytest.raises(RefreshHistogramError, match="prep_region_gpu_ms"):
        compute_region_stats(rows)


# --- percentiles ---------------------------------------------------------------------

def test_percentile_matches_known_p50_p95():
    values = sorted(float(i) for i in range(1, 11))  # 1..10
    assert percentile(values, 0.50) == pytest.approx(5.5)
    assert percentile(values, 0.95) == pytest.approx(9.55)


def test_percentile_requires_at_least_one_value():
    with pytest.raises(RefreshHistogramError):
        percentile([], 0.5)


# --- presentmon_status refusal (PRESENTMON-HARNESS-ROBUSTNESS-2) -------------------

def test_degraded_presentmon_status_refuses_to_build_a_report(tmp_path):
    # The job's own sufficiency gate already found this leg's evidence too thin -- this
    # consumer must refuse before even reading either file, never compute a histogram
    # from evidence its own producer flagged as insufficient.
    csv_path = tmp_path / "presentmon-series.csv"
    _write_presentmon_csv(csv_path, [16.67] * 12 + [33.34] * 5 + [50.01] * 3)
    log_path = tmp_path / "mlvapp.log"
    _write_frame_log(log_path, count=10, start=1)

    with pytest.raises(RefreshHistogramError, match="degraded"):
        build_report(str(csv_path), str(log_path), presentmon_status="degraded")


def test_degraded_presentmon_status_reason_is_echoed_in_the_refusal(tmp_path):
    csv_path = tmp_path / "presentmon-series.csv"
    _write_presentmon_csv(csv_path, [16.67] * 12 + [33.34] * 5 + [50.01] * 3)
    log_path = tmp_path / "mlvapp.log"
    _write_frame_log(log_path, count=10, start=1)

    with pytest.raises(RefreshHistogramError, match="coverage too thin"):
        build_report(
            str(csv_path), str(log_path),
            presentmon_status="degraded",
            presentmon_status_reason="coverage too thin",
        )


def test_verified_zero_displayed_presentmon_status_also_refuses(tmp_path):
    # Any non-'ok' status refuses -- not just the literal string 'degraded' -- so a future
    # distinct status value (e.g. DISPLAY_ASLEEP's verified_zero_displayed) is covered too
    # without this consumer needing to enumerate every non-ok spelling.
    csv_path = tmp_path / "presentmon-series.csv"
    _write_presentmon_csv(csv_path, [16.67] * 12 + [33.34] * 5 + [50.01] * 3)
    log_path = tmp_path / "mlvapp.log"
    _write_frame_log(log_path, count=10, start=1)

    with pytest.raises(RefreshHistogramError, match="verified_zero_displayed"):
        build_report(str(csv_path), str(log_path), presentmon_status="verified_zero_displayed")


def test_ok_presentmon_status_builds_a_report_normally(tmp_path):
    csv_path = tmp_path / "presentmon-series.csv"
    _write_presentmon_csv(csv_path, [16.67] * 12 + [33.34] * 5 + [50.01] * 3)
    log_path = tmp_path / "mlvapp.log"
    _write_frame_log(log_path, count=10, start=1)

    report = build_report(str(csv_path), str(log_path), presentmon_status="ok")
    assert report["presentMon"]["sampleCount"] == 20


def test_omitted_presentmon_status_is_refused_by_the_library_function(tmp_path):
    # PRESENTMON-HARNESS-ROBUSTNESS-3 (sol pre-review HARDENING): build_report() itself fails closed
    # -- no direct caller can get a measured-looking report without stating the leg's status.
    csv_path = tmp_path / "presentmon-series.csv"
    _write_presentmon_csv(csv_path, [16.67] * 12 + [33.34] * 5 + [50.01] * 3)
    log_path = tmp_path / "mlvapp.log"
    _write_frame_log(log_path, count=10, start=1)

    with pytest.raises(RefreshHistogramError, match="presentmon_status is required"):
        build_report(str(csv_path), str(log_path))


@pytest.mark.parametrize("outside_option", ["--presentmon-csv", "--frame-log"])
def test_cli_refuses_each_data_path_outside_the_artifacts_dir(tmp_path, capsys, outside_option):
    # PRESENTMON-HARNESS-ROBUSTNESS-3 (sol pre-review BLOCKER): leg A's 'ok' status must not
    # authorize leg B's data. One outside path per case, so each binding is proven on its own
    # (sol pre-review #2: a combined case could not detect one binding being dropped).
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    leg_a = _write_artifacts_dir(tmp_path / "a", summary={"presentMonStatus": "ok"})
    leg_b = _write_artifacts_dir(tmp_path / "b", summary={"presentMonStatus": "degraded"})
    inside = {
        "--presentmon-csv": str(leg_a / "presentmon-series.csv"),
        "--frame-log": str(leg_a / "logs" / "smoke-run.log"),
    }
    outside = {
        "--presentmon-csv": str(leg_b / "presentmon-series.csv"),
        "--frame-log": str(leg_b / "logs" / "smoke-run.log"),
    }
    argv = ["--artifacts-dir", str(leg_a)]
    for option in ("--presentmon-csv", "--frame-log"):
        argv += [option, outside[option] if option == outside_option else inside[option]]

    rc = main(argv)

    assert rc == 1
    err = capsys.readouterr().err
    assert "outside --artifacts-dir" in err and outside_option in err


def test_cli_refuses_a_data_path_on_another_drive_as_a_typed_failure(tmp_path, capsys, monkeypatch):
    # sol pre-review #2 HARDENING: os.path.commonpath raises ValueError across drives; that must be
    # the CLI's normal typed refusal, not an escaping exception.
    leg = _write_artifacts_dir(tmp_path, summary={"presentMonStatus": "ok"})

    def _commonpath_across_drives(paths):
        raise ValueError("Paths don't have the same drive")

    monkeypatch.setattr("tools.profiling.refresh_period_histogram.os.path.commonpath", _commonpath_across_drives)

    rc = main(["--artifacts-dir", str(leg), "--presentmon-csv", str(leg / "presentmon-series.csv")])

    assert rc == 1
    assert "outside --artifacts-dir" in capsys.readouterr().err


def test_cli_accepts_explicit_data_paths_inside_the_artifacts_dir(tmp_path):
    leg = _write_artifacts_dir(tmp_path, summary={"presentMonStatus": "ok"})
    out_path = tmp_path / "out.json"

    rc = main([
        "--artifacts-dir", str(leg),
        "--presentmon-csv", str(leg / "presentmon-series.csv"),
        "--frame-log", str(leg / "logs" / "smoke-run.log"),
        "--out", str(out_path),
    ])

    assert rc == 0


# --- CLI enforcement: the tool itself must not be able to skip the status ----------------------
# PRESENTMON-HARNESS-ROBUSTNESS-2 r1b (sol BLOCKER, pre-review): r1 left the CLI's
# --presentmon-status flag optional with no alternative, so the documented command (which never
# passed it) produced a measured-looking histogram from a leg the job itself had already flagged
# degraded. The CLI now REQUIRES --presentmon-status or --artifacts-dir (which derives it) --
# never both omitted -- and never silently falls back to the old unconditional behaviour.

def _write_artifacts_dir(tmp_path, *, summary=None, evidence_manifest=None, csv_values=None, frame_log_count=10):
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    csv_path = artifacts_dir / "presentmon-series.csv"
    _write_presentmon_csv(csv_path, csv_values if csv_values is not None else [16.67] * 12 + [33.34] * 5 + [50.01] * 3)
    log_dir = artifacts_dir / "logs"
    log_dir.mkdir()
    _write_frame_log(log_dir / "smoke-run.log", count=frame_log_count, start=1)
    if summary is not None:
        (artifacts_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    if evidence_manifest is not None:
        (artifacts_dir / "evidence-manifest.json").write_text(json.dumps(evidence_manifest), encoding="utf-8")
    return artifacts_dir


def test_cli_without_status_or_artifacts_dir_fails_closed(tmp_path, capsys):
    csv_path = tmp_path / "presentmon-series.csv"
    _write_presentmon_csv(csv_path, [16.67] * 12 + [33.34] * 5 + [50.01] * 3)
    log_path = tmp_path / "mlvapp.log"
    _write_frame_log(log_path, count=10, start=1)

    rc = main(["--presentmon-csv", str(csv_path), "--frame-log", str(log_path)])

    assert rc == 1
    assert "--presentmon-status is required" in capsys.readouterr().err


def test_cli_with_explicit_status_and_paths_still_works(tmp_path):
    # Regression: the pre-existing explicit-flag path (no --artifacts-dir at all) is unchanged.
    csv_path = tmp_path / "presentmon-series.csv"
    _write_presentmon_csv(csv_path, [16.67] * 12 + [33.34] * 5 + [50.01] * 3)
    log_path = tmp_path / "mlvapp.log"
    _write_frame_log(log_path, count=10, start=1)
    out_path = tmp_path / "out.json"

    rc = main([
        "--presentmon-csv", str(csv_path), "--frame-log", str(log_path),
        "--presentmon-status", "ok", "--out", str(out_path),
    ])

    assert rc == 0
    assert json.loads(out_path.read_text(encoding="utf-8"))["presentMon"]["sampleCount"] == 20


def test_cli_with_artifacts_dir_derives_paths_and_refuses_a_degraded_summary(tmp_path, capsys):
    artifacts_dir = _write_artifacts_dir(
        tmp_path, summary={"presentMonStatus": "degraded", "presentMonStatusReason": "coverage too thin"}
    )

    rc = main(["--artifacts-dir", str(artifacts_dir)])

    assert rc == 1
    err = capsys.readouterr().err
    assert "degraded" in err
    assert "coverage too thin" in err


def test_cli_with_artifacts_dir_derives_paths_and_builds_when_ok(tmp_path):
    artifacts_dir = _write_artifacts_dir(tmp_path, summary={"presentMonStatus": "ok"})
    out_path = tmp_path / "out.json"

    rc = main(["--artifacts-dir", str(artifacts_dir), "--out", str(out_path)])

    assert rc == 0
    assert json.loads(out_path.read_text(encoding="utf-8"))["presentMon"]["sampleCount"] == 20


def test_cli_with_artifacts_dir_falls_back_to_evidence_manifest(tmp_path, capsys):
    # No summary.json at all -- evidence-manifest.json's nested presentMon.status/statusReason
    # is read instead.
    artifacts_dir = _write_artifacts_dir(
        tmp_path,
        evidence_manifest={"presentMon": {"status": "verified_zero_displayed", "statusReason": "no display change"}},
    )

    rc = main(["--artifacts-dir", str(artifacts_dir)])

    assert rc == 1
    assert "verified_zero_displayed" in capsys.readouterr().err


def test_cli_with_artifacts_dir_and_neither_file_present_fails_closed(tmp_path, capsys):
    artifacts_dir = _write_artifacts_dir(tmp_path)  # no summary.json, no evidence-manifest.json

    rc = main(["--artifacts-dir", str(artifacts_dir)])

    assert rc == 1
    assert "could not find presentMonStatus" in capsys.readouterr().err


def test_cli_explicit_status_contradicting_the_leg_is_refused(tmp_path, capsys):
    # PRESENTMON-HARNESS-ROBUSTNESS-3 (sol BLOCKER on #178): with --artifacts-dir, the leg's own
    # published status is authoritative; an explicit 'ok' over a producer-degraded leg must NOT
    # produce a measured-looking histogram.
    artifacts_dir = _write_artifacts_dir(tmp_path, summary={"presentMonStatus": "degraded"})
    out_path = tmp_path / "out.json"

    rc = main(["--artifacts-dir", str(artifacts_dir), "--presentmon-status", "ok", "--out", str(out_path)])

    assert rc == 1
    assert not out_path.exists()
    err = capsys.readouterr().err
    assert "contradicts" in err and "degraded" in err


def test_cli_explicit_status_agreeing_with_the_leg_builds(tmp_path):
    artifacts_dir = _write_artifacts_dir(tmp_path, summary={"presentMonStatus": "ok"})
    out_path = tmp_path / "out.json"

    rc = main(["--artifacts-dir", str(artifacts_dir), "--presentmon-status", "ok", "--out", str(out_path)])

    assert rc == 0
    report = json.loads(out_path.read_text(encoding="utf-8"))
    assert report["presentMon"]["sampleCount"] == 20
    assert report["presentMonStatus"] == "ok"


def test_cli_report_always_carries_the_status_it_was_built_under(tmp_path):
    artifacts_dir = _write_artifacts_dir(
        tmp_path, summary={"presentMonStatus": "ok", "presentMonStatusReason": "all arms passed"}
    )
    out_path = tmp_path / "out.json"

    rc = main(["--artifacts-dir", str(artifacts_dir), "--out", str(out_path)])

    assert rc == 0
    report = json.loads(out_path.read_text(encoding="utf-8"))
    assert report["presentMonStatus"] == "ok"
    assert report["presentMonStatusReason"] == "all arms passed"


# --- resolve_presentmon_status_from_artifacts ---------------------------------------------------

def test_resolve_status_reads_summary_json_first(tmp_path):
    (tmp_path / "summary.json").write_text(
        json.dumps({"presentMonStatus": "degraded", "presentMonStatusReason": "thin"}), encoding="utf-8"
    )
    (tmp_path / "evidence-manifest.json").write_text(
        json.dumps({"presentMon": {"status": "ok"}}), encoding="utf-8"
    )

    status, reason = resolve_presentmon_status_from_artifacts(str(tmp_path))

    assert (status, reason) == ("degraded", "thin")


def test_resolve_status_falls_back_to_evidence_manifest(tmp_path):
    (tmp_path / "evidence-manifest.json").write_text(
        json.dumps({"presentMon": {"status": "unavailable", "statusReason": None}}), encoding="utf-8"
    )

    status, reason = resolve_presentmon_status_from_artifacts(str(tmp_path))

    assert (status, reason) == ("unavailable", None)


def test_resolve_status_raises_when_neither_file_carries_it(tmp_path):
    (tmp_path / "summary.json").write_text(json.dumps({"result": "GPU_RECON_FRAMES_ZERO"}), encoding="utf-8")

    with pytest.raises(RefreshHistogramError, match="could not find presentMonStatus"):
        resolve_presentmon_status_from_artifacts(str(tmp_path))


def test_resolve_status_raises_when_no_files_exist(tmp_path):
    with pytest.raises(RefreshHistogramError, match="could not find presentMonStatus"):
        resolve_presentmon_status_from_artifacts(str(tmp_path))


def test_resolve_status_raises_a_typed_error_on_malformed_summary_json(tmp_path):
    # HARDENING (sol PRE-REVIEW #2): malformed JSON used to raise json.JSONDecodeError straight
    # through main()'s try/except RefreshHistogramError, producing an uncaught traceback instead
    # of the CLI's normal FAIL/exit-1 refusal.
    (tmp_path / "summary.json").write_text("{not valid json", encoding="utf-8")

    with pytest.raises(RefreshHistogramError, match="summary.json.*not valid JSON"):
        resolve_presentmon_status_from_artifacts(str(tmp_path))


def test_resolve_status_raises_a_typed_error_on_malformed_evidence_manifest_json(tmp_path):
    (tmp_path / "evidence-manifest.json").write_text("{not valid json", encoding="utf-8")

    with pytest.raises(RefreshHistogramError, match="evidence-manifest.json.*not valid JSON"):
        resolve_presentmon_status_from_artifacts(str(tmp_path))


def test_cli_with_malformed_summary_json_fails_closed_not_a_traceback(tmp_path, capsys):
    artifacts_dir = _write_artifacts_dir(tmp_path, summary={"presentMonStatus": "ok"})
    (artifacts_dir / "summary.json").write_text("{not valid json", encoding="utf-8")

    rc = main(["--artifacts-dir", str(artifacts_dir)])

    assert rc == 1
    err = capsys.readouterr().err
    assert "refresh_period_histogram: FAIL:" in err
    assert "not valid JSON" in err


def test_cli_with_malformed_evidence_manifest_json_fails_closed_not_a_traceback(tmp_path, capsys):
    artifacts_dir = _write_artifacts_dir(
        tmp_path, evidence_manifest={"presentMon": {"status": "ok"}}
    )
    (artifacts_dir / "evidence-manifest.json").write_text("{not valid json", encoding="utf-8")

    rc = main(["--artifacts-dir", str(artifacts_dir)])

    assert rc == 1
    err = capsys.readouterr().err
    assert "refresh_period_histogram: FAIL:" in err
    assert "not valid JSON" in err


# --- end-to-end build_report ----------------------------------------------------------

def test_build_report_end_to_end(tmp_path):
    csv_path = tmp_path / "presentmon-series.csv"
    values = [16.67] * 12 + [33.34] * 5 + [50.01] * 3
    _write_presentmon_csv(csv_path, values)

    log_path = tmp_path / "mlvapp.log"
    _write_frame_log(log_path, count=10, start=1)

    report = build_report(str(csv_path), str(log_path), presentmon_status="ok")
    assert report["schema"] == "mlvapp.refresh-period-histogram.v1"
    assert report["presentMon"]["sampleCount"] == 20
    assert report["presentMon"]["buckets"]["1"]["count"] == 12
    assert report["frameLog"]["presentedFrameCount"] == 10
    for region in (
        "prep_region_setup", "prep_region_gpu", "prep_region_image",
        "prep_region_present", "prep_region_finish", "prep_region_total",
        "prep_region_unattributed",
    ):
        assert region in report["frameLog"]["regions"]
        assert report["frameLog"]["regions"][region]["p50Ms"] == pytest.approx(5.5)
        assert report["frameLog"]["regions"][region]["p95Ms"] == pytest.approx(9.55)
