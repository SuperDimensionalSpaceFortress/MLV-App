"""PLAYBACK-ATTR-3-CUDA: within-run refresh-period histogram + prep-region attribution.

Bachelor cannot resolve a timing A/B below about 2x (A/A noise), so this run is judged
WITHIN ONE RUN rather than against a baseline: the refresh-period histogram (the share
of presented frames landing on 1, 2, or 3+ display refreshes), plus the presented-frame
count and per-sub-region prep timing (prep_region_setup/gpu/image/present/finish/total/
unattributed).

Inputs:
  - a PresentMon series CSV with a `msBetweenDisplayChange` column (the derived
    presentmon-series.csv the bachelor attribution job exports, one row per PresentMon
    sample; see tools/profiling/bachelor/playback-attr-3-cuda-job.ps1).
  - the raw MLVApp log containing `playback_smoke.frame ...` lines with the
    `prep_region_*_ms` probes (always compiled in; see platform/qt/MainWindow.cpp
    near the `playback_smoke.frame` qInfo() call).

A missing required column/field is always an error, never treated as zero -- a silent
zero would misreport a broken capture as "no cost" instead of "not measured".
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
from collections import Counter
from typing import Iterable, Sequence

SCHEMA = "mlvapp.refresh-period-histogram.v1"

PRESENTMON_INTERVAL_COLUMN = "msBetweenDisplayChange"

# PRESENTMON-HARNESS-ROBUSTNESS-2: the job's own sufficiency gate (playback-attr-3-cuda-job.ps1)
# is the single source of truth for whether a leg's PresentMon evidence is thin -- it alone knows
# the app-side denominator (how many times MLVApp itself swapped in the window) that the coverage
# fraction is computed against; this module only ever sees the exported interval CSV, which cannot
# reconstruct that denominator on its own. A caller who has the job's presentMonStatus (from
# summary.json/evidence-manifest.json) passes it through so this histogram -- the one downstream
# reader of the interval CSV -- refuses to present a degraded leg's numbers as measured, rather
# than silently re-deriving a weaker threshold from the CSV alone.
PRESENTMON_STATUS_OK = "ok"

# PRESENTMON-HARNESS-ROBUSTNESS-2 r1b (sol BLOCKER, pre-review): the CLI's own --presentmon-csv/
# --frame-log/--presentmon-status used to be three independent, all-optional flags -- the
# documented command passed the first two and omitted the third, and nothing refused. These are
# the standard paths a leg's own producer (playback-attr-3-cuda-job.ps1) publishes its artifacts
# under, so a caller who has the artifacts dir never has to spell out any of the three by hand.
DEFAULT_PRESENTMON_CSV_NAME = "presentmon-series.csv"
DEFAULT_FRAME_LOG_RELATIVE_PATH = os.path.join("logs", "smoke-run.log")
DEFAULT_SUMMARY_JSON_NAME = "summary.json"
DEFAULT_EVIDENCE_MANIFEST_JSON_NAME = "evidence-manifest.json"

PREP_REGIONS = (
    "prep_region_setup",
    "prep_region_gpu",
    "prep_region_image",
    "prep_region_present",
    "prep_region_finish",
    "prep_region_total",
    "prep_region_unattributed",
)

FRAME_LINE_RX = re.compile(r"playback_smoke\.frame\s")
KV_RX = re.compile(r"(?P<key>[A-Za-z0-9_]+)=(?P<value>[^\s]+)")

# Refresh-multiple rounding rule (stated explicitly per the card's requirement):
#   multiple = floor(interval_ms / refresh_period_ms + 0.5)   (round-half-up)
#   clamped to a minimum of 1 (an interval can never be < 1 refresh, only noisy)
#   multiples >= 3 are grouped into the "3+" bucket.
ROUNDING_RULE = (
    "multiple = floor(intervalMs / refreshPeriodMs + 0.5) [round-half-up], "
    "clamped to a minimum of 1; multiples >= 3 are grouped into the '3+' bucket"
)


class RefreshHistogramError(ValueError):
    """Raised when an input is malformed, missing a required column, or empty."""


def _round_half_up(value: float) -> int:
    return int(math.floor(value + 0.5))


def refresh_multiple(interval_ms: float, refresh_period_ms: float) -> int:
    if refresh_period_ms <= 0:
        raise RefreshHistogramError(f"refreshPeriodMs must be positive, got {refresh_period_ms!r}")
    multiple = _round_half_up(interval_ms / refresh_period_ms)
    return max(multiple, 1)


def bucket_label(multiple: int) -> str:
    if multiple <= 0:
        raise RefreshHistogramError(f"refresh multiple must be >= 1, got {multiple!r}")
    return "3+" if multiple >= 3 else str(multiple)


def percentile(sorted_values: Sequence[float], p: float) -> float:
    if not sorted_values:
        raise RefreshHistogramError("percentile requires at least one value")
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = (len(sorted_values) - 1) * p
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return float(sorted_values[int(lo)])
    frac = rank - lo
    return float(sorted_values[int(lo)] + (sorted_values[int(hi)] - sorted_values[int(lo)]) * frac)


def parse_presentmon_intervals(path: str) -> list[float]:
    """Read msBetweenDisplayChange from a PresentMon series CSV. Positive values only
    (mirrors the bachelor job's own filter -- non-positive/unparseable rows are gaps
    in the capture, not zero-cost frames)."""
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None or PRESENTMON_INTERVAL_COLUMN not in reader.fieldnames:
            raise RefreshHistogramError(
                f"PresentMon CSV {path!r} is missing required column "
                f"{PRESENTMON_INTERVAL_COLUMN!r}; found {reader.fieldnames!r}"
            )
        intervals: list[float] = []
        for row in reader:
            raw = row.get(PRESENTMON_INTERVAL_COLUMN)
            if raw is None or raw.strip() == "":
                continue
            try:
                value = float(raw)
            except ValueError:
                continue
            if value > 0:
                intervals.append(value)
    if not intervals:
        raise RefreshHistogramError(
            f"PresentMon CSV {path!r} has no positive {PRESENTMON_INTERVAL_COLUMN} samples"
        )
    return intervals


def parse_frame_log_rows(path: str, min_rows: int = 10) -> list[dict[str, float]]:
    """Read prep_region_* fields from playback_smoke.frame lines. A row is kept only
    when it carries prep_region_total_ms AND prep_region_unattributed_ms (mirrors the
    bachelor attribution job's own gate for a "high-resolution" frame row)."""
    rows: list[dict[str, float]] = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not FRAME_LINE_RX.search(line):
                continue
            values: dict[str, float] = {}
            for match in KV_RX.finditer(line):
                key = match.group("key")
                if key not in PREP_REGIONS and not key.startswith("prep_region_"):
                    continue
                try:
                    values[key] = float(match.group("value"))
                except ValueError:
                    continue
            if "prep_region_total_ms" in values and "prep_region_unattributed_ms" in values:
                rows.append(values)
    if not rows:
        raise RefreshHistogramError(
            f"frame log {path!r} has no playback_smoke.frame rows carrying prep_region_total_ms "
            "and prep_region_unattributed_ms"
        )
    if len(rows) < min_rows:
        raise RefreshHistogramError(
            f"frame log {path!r} has only {len(rows)} high-resolution frame rows; require >= {min_rows}"
        )
    return rows


# Ambiguity guard (histogram ambiguity fix): the minimum-interval estimate assumes the
# observed minimum IS one true refresh. That assumption is unverifiable -- and wrong --
# when the whole capture is uniformly N refreshes per frame (an all-2-refresh capture:
# every interval sits near the same value, so the "cluster" is the entire series and
# nothing in the interval distribution itself distinguishes that from a healthy
# all-1-refresh capture). The only interval-only signal available is whether a
# meaningfully-sized population of samples sits OUTSIDE the near-minimum cluster (i.e.
# genuinely different, higher-multiple frames) to cross-check the assumption against.
MIN_OUTSIDE_CLUSTER_SHARE = 0.05


def compute_refresh_period(intervals: Sequence[float]) -> dict:
    """Measure the true monitor refresh period from the interval distribution.

    Isolates the "1-refresh" cluster (samples within 50% of the observed minimum
    interval -- the cheapest, most common case on a healthy capture) and reports the
    MODE of that cluster (values rounded to 2 decimal places for grouping) when a
    clear plurality exists (the mode's count is strictly greater than 1 and strictly
    greater than the runner-up); otherwise falls back to the cluster's MEDIAN. Which
    method was actually used is reported in `measurement` so the number is never
    presented as more certain than it is.

    Raises when fewer than `MIN_OUTSIDE_CLUSTER_SHARE` of all intervals sit outside the
    near-minimum cluster: with too little of the distribution to contrast the cluster
    against, an all-N-refresh capture is indistinguishable from a healthy all-1-refresh
    one (the bug this guard exists to close -- see module docstring). Callers who can
    supply the display's actual nominal refresh period should bucket against that
    instead of calling this estimator at all.
    """
    if not intervals:
        raise RefreshHistogramError("compute_refresh_period requires at least one interval")
    min_interval = min(intervals)
    cluster = [v for v in intervals if v <= min_interval * 1.5]
    if not cluster:
        raise RefreshHistogramError("no samples found near the minimum observed interval")

    outside_cluster_share = 1.0 - (len(cluster) / len(intervals))
    if outside_cluster_share < MIN_OUTSIDE_CLUSTER_SHARE:
        raise RefreshHistogramError(
            f"refresh period is ambiguous: only {outside_cluster_share * 100:.1f}% of "
            f"{len(intervals)} intervals fall outside the near-minimum cluster (< "
            f"{MIN_OUTSIDE_CLUSTER_SHARE * 100:.0f}% required), so minObservedIntervalMs="
            f"{min_interval!r} cannot be confirmed as one true display refresh -- this "
            "capture may be uniformly N refreshes per frame (e.g. an all-2-refresh "
            "capture reads as 100% 1-refresh under the minimum-interval assumption). "
            "Pass --refresh-period-ms with the display's actual nominal refresh period "
            "to resolve the ambiguity."
        )

    rounded = [round(v, 2) for v in cluster]
    counts = Counter(rounded)
    ranked = counts.most_common(2)
    top_value, top_count = ranked[0]
    runner_up_count = ranked[1][1] if len(ranked) > 1 else 0
    if top_count > 1 and top_count > runner_up_count:
        refresh_period_ms = float(top_value)
        measurement = "mode"
    else:
        sorted_cluster = sorted(cluster)
        refresh_period_ms = percentile(sorted_cluster, 0.5)
        measurement = "median"

    return {
        "refreshPeriodMs": refresh_period_ms,
        "measurement": measurement,
        "clusterSampleCount": len(cluster),
        "minObservedIntervalMs": min_interval,
        "outsideClusterShare": outside_cluster_share,
    }


def compute_buckets(intervals: Sequence[float], refresh_period_ms: float) -> dict:
    counts: Counter[str] = Counter()
    for value in intervals:
        multiple = refresh_multiple(value, refresh_period_ms)
        counts[bucket_label(multiple)] += 1
    total = len(intervals)
    buckets = {}
    for label in ("1", "2", "3+"):
        count = counts.get(label, 0)
        buckets[label] = {"count": count, "share": count / total}
    return buckets


def compute_region_stats(frame_rows: Sequence[dict[str, float]]) -> dict:
    regions = {}
    for region in PREP_REGIONS:
        key = region + "_ms"
        values = sorted(row[key] for row in frame_rows if key in row)
        if not values:
            raise RefreshHistogramError(f"no frame rows carry {key!r}")
        regions[region] = {
            "p50Ms": percentile(values, 0.50),
            "p95Ms": percentile(values, 0.95),
            "count": len(values),
        }
    return regions


def resolve_presentmon_status_from_artifacts(artifacts_dir: str) -> tuple[str, str | None]:
    """Read presentMonStatus/presentMonStatusReason from a leg's own published summary.json
    (preferred -- it is the first file a reader opens) or evidence-manifest.json (presentMon.status/
    statusReason), so a caller who has the leg's artifacts dir never has to pass --presentmon-status
    by hand -- and never has the OPTION to silently omit it either: this raises, rather than
    returning a guessed/absent status, whenever neither file carries the field.
    """
    summary_path = os.path.join(artifacts_dir, DEFAULT_SUMMARY_JSON_NAME)
    if os.path.isfile(summary_path):
        with open(summary_path, encoding="utf-8") as fh:
            try:
                summary = json.load(fh)
            except json.JSONDecodeError as exc:
                raise RefreshHistogramError(f"{summary_path!r} is not valid JSON: {exc}") from exc
        if isinstance(summary, dict) and "presentMonStatus" in summary:
            return summary["presentMonStatus"], summary.get("presentMonStatusReason")

    manifest_path = os.path.join(artifacts_dir, DEFAULT_EVIDENCE_MANIFEST_JSON_NAME)
    if os.path.isfile(manifest_path):
        with open(manifest_path, encoding="utf-8") as fh:
            try:
                manifest = json.load(fh)
            except json.JSONDecodeError as exc:
                raise RefreshHistogramError(f"{manifest_path!r} is not valid JSON: {exc}") from exc
        present_mon = manifest.get("presentMon") if isinstance(manifest, dict) else None
        if isinstance(present_mon, dict) and "status" in present_mon:
            return present_mon["status"], present_mon.get("statusReason")

    raise RefreshHistogramError(
        f"could not find presentMonStatus in {summary_path!r} or {manifest_path!r} -- refusing "
        "to present a refresh-period histogram as measured without knowing the leg's PresentMon "
        "sufficiency status"
    )


def build_report(
    presentmon_csv_path: str,
    frame_log_path: str,
    min_frame_rows: int = 10,
    refresh_period_ms: float | None = None,
    presentmon_status: str | None = None,
    presentmon_status_reason: str | None = None,
) -> dict:
    # Checked BEFORE either file is even read: a caller that already knows the leg is degraded
    # (or verified_zero_displayed/unavailable) gets a clear, immediate refusal naming the status,
    # never a histogram computed from evidence its own producer already flagged as insufficient.
    # PRESENTMON-HARNESS-ROBUSTNESS-3 (sol pre-review HARDENING): the library itself fails closed -- no caller can
    # get a measured-looking report without stating the leg's PresentMon status.
    if presentmon_status is None:
        raise RefreshHistogramError(
            "presentmon_status is required: refusing to build a refresh-period histogram without the leg's "
            "PresentMon sufficiency status"
        )
    if presentmon_status != PRESENTMON_STATUS_OK:
        reason_suffix = f": {presentmon_status_reason}" if presentmon_status_reason else ""
        raise RefreshHistogramError(
            f"refusing to present a refresh-period histogram as measured: presentMonStatus="
            f"{presentmon_status!r} (not {PRESENTMON_STATUS_OK!r}){reason_suffix}"
        )
    intervals = parse_presentmon_intervals(presentmon_csv_path)
    frame_rows = parse_frame_log_rows(frame_log_path, min_rows=min_frame_rows)

    if refresh_period_ms is not None:
        # Nominal supplied (e.g. Bachelor's panel refresh, per the runbook): bucket
        # against it directly -- no min-interval estimate, no ambiguity guard, because
        # the true refresh period is now known rather than inferred.
        # NaN/inf must be rejected explicitly: `nan <= 0` is False and `inf <= 0` is
        # False, so a bare positivity check silently lets both through to
        # compute_buckets, where they would corrupt every bucket's refresh multiple.
        if math.isnan(refresh_period_ms) or math.isinf(refresh_period_ms):
            raise RefreshHistogramError(
                f"refreshPeriodMs must be a finite positive number, got {refresh_period_ms!r}"
            )
        if refresh_period_ms <= 0:
            raise RefreshHistogramError(f"refreshPeriodMs must be positive, got {refresh_period_ms!r}")
        refresh_period = {
            "refreshPeriodMs": float(refresh_period_ms),
            "measurement": "nominal",
            "clusterSampleCount": None,
            "minObservedIntervalMs": None,
            "refreshPeriodSource": "nominal",
        }
    else:
        refresh_period = dict(compute_refresh_period(intervals))
        refresh_period["refreshPeriodSource"] = "estimated-min-interval"

    buckets = compute_buckets(intervals, refresh_period["refreshPeriodMs"])
    regions = compute_region_stats(frame_rows)

    return {
        "schema": SCHEMA,
        "presentMon": {
            "sourceFile": presentmon_csv_path,
            "sampleCount": len(intervals),
            "refreshPeriodMs": refresh_period["refreshPeriodMs"],
            "refreshPeriodMeasurement": refresh_period["measurement"],
            "refreshPeriodSource": refresh_period["refreshPeriodSource"],
            "refreshPeriodClusterSampleCount": refresh_period["clusterSampleCount"],
            "minObservedIntervalMs": refresh_period["minObservedIntervalMs"],
            "roundingRule": ROUNDING_RULE,
            "buckets": buckets,
        },
        "frameLog": {
            "sourceFile": frame_log_path,
            "presentedFrameCount": len(frame_rows),
            "regions": regions,
        },
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--presentmon-csv", default=None,
        help="PresentMon series CSV (msBetweenDisplayChange column). Defaults to "
        f"{DEFAULT_PRESENTMON_CSV_NAME!r} inside --artifacts-dir when that is given.",
    )
    parser.add_argument(
        "--frame-log", default=None,
        help="Raw MLVApp log with playback_smoke.frame lines. Defaults to "
        f"{DEFAULT_FRAME_LOG_RELATIVE_PATH!r} inside --artifacts-dir when that is given.",
    )
    parser.add_argument(
        "--artifacts-dir",
        default=None,
        help=(
            "A leg's published artifacts directory (evidence-manifest.json's artifactRoot). "
            "--presentmon-csv/--frame-log default to the standard paths inside it, and "
            "the leg's presentMonStatus is ALWAYS read from its summary.json (preferred) or "
            "evidence-manifest.json and is authoritative: an explicit --presentmon-status must agree with it, "
            "and explicit --presentmon-csv/--frame-log must resolve inside this directory. Required whenever "
            "--presentmon-status is not given: this tool refuses to compute a histogram without "
            "knowing the leg's PresentMon sufficiency status, and this is the only way to supply "
            "it other than typing it by hand."
        ),
    )
    parser.add_argument("--out", default="", help="Write JSON here instead of stdout")
    parser.add_argument("--min-frame-rows", type=int, default=10, help="Minimum high-resolution frame rows required")
    parser.add_argument(
        "--refresh-period-ms",
        type=float,
        default=None,
        help=(
            "Nominal display refresh period in ms (the runbook requires supplying "
            "Bachelor's panel refresh). When given, buckets are computed against this "
            "value directly. When omitted, the refresh period is estimated from the "
            "interval distribution's near-minimum cluster, which fails closed if the "
            "capture is too ambiguous to trust (see compute_refresh_period)."
        ),
    )
    parser.add_argument(
        "--presentmon-status",
        default=None,
        help=(
            "The producing job's own presentMonStatus (summary.json/evidence-manifest.json). "
            "When given and not 'ok', this refuses to compute or present a histogram at all -- "
            "the job's sufficiency gate already found the evidence too thin/absent to measure."
        ),
    )
    parser.add_argument(
        "--presentmon-status-reason",
        default=None,
        help="The producing job's presentMonStatusReason, echoed into the refusal message.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    presentmon_csv = args.presentmon_csv
    frame_log = args.frame_log
    presentmon_status = args.presentmon_status
    presentmon_status_reason = args.presentmon_status_reason

    try:
        if args.artifacts_dir is not None:
            # PRESENTMON-HARNESS-ROBUSTNESS-3 (sol pre-review BLOCKER): the status and the data it authorizes must come
            # from the SAME leg. With --artifacts-dir, explicit --presentmon-csv/--frame-log must resolve inside that
            # directory; a path into another leg's artifacts is refused.
            artifacts_root = os.path.realpath(args.artifacts_dir)
            for label, supplied in (("--presentmon-csv", presentmon_csv), ("--frame-log", frame_log)):
                if supplied is not None:
                    resolved = os.path.realpath(supplied)
                    try:
                        inside = os.path.commonpath([artifacts_root, resolved]) == artifacts_root
                    except ValueError:  # different drive / path authority: certainly not inside
                        inside = False
                    if not inside:
                        raise RefreshHistogramError(
                            f"{label} {supplied!r} is outside --artifacts-dir {args.artifacts_dir!r}; the status and the "
                            "data it authorizes must come from the same leg -- refusing"
                        )
            if presentmon_csv is None:
                presentmon_csv = os.path.join(args.artifacts_dir, DEFAULT_PRESENTMON_CSV_NAME)
            if frame_log is None:
                frame_log = os.path.join(args.artifacts_dir, DEFAULT_FRAME_LOG_RELATIVE_PATH)
            # PRESENTMON-HARNESS-ROBUSTNESS-3 (sol BLOCKER on #178): the leg's OWN published status is
            # authoritative whenever its artifacts are given. An explicit --presentmon-status may only
            # agree with it; a contradicting value (e.g. 'ok' over a producer-degraded leg) is refused,
            # never allowed to turn a degraded leg into a measured-looking histogram.
            derived_status, derived_reason = resolve_presentmon_status_from_artifacts(args.artifacts_dir)
            if presentmon_status is not None and presentmon_status != derived_status:
                raise RefreshHistogramError(
                    f"--presentmon-status {presentmon_status!r} contradicts the leg's own published "
                    f"presentMonStatus {derived_status!r} in {args.artifacts_dir}; the leg's status is "
                    "authoritative -- refusing"
                )
            presentmon_status = derived_status
            if presentmon_status_reason is None:
                presentmon_status_reason = derived_reason

        # PRESENTMON-HARNESS-ROBUSTNESS-2 r1b (sol BLOCKER, pre-review): the CLI -- the tool anyone
        # actually runs, and the one the runbook documents -- must not be able to reach build_report
        # without a known presentMonStatus. (Since PRESENTMON-HARNESS-ROBUSTNESS-3 build_report() refuses a
        # missing status too; this CLI check stays so the refusal names the CLI's own options.)
        if presentmon_csv is None or frame_log is None:
            raise RefreshHistogramError(
                "--presentmon-csv and --frame-log are required unless --artifacts-dir is given"
            )
        if presentmon_status is None:
            raise RefreshHistogramError(
                "--presentmon-status is required unless --artifacts-dir is given -- this tool "
                "refuses to compute a refresh-period histogram without knowing the leg's "
                "PresentMon sufficiency status. Pass --artifacts-dir to read it from the leg's "
                "own summary.json/evidence-manifest.json, or pass --presentmon-status explicitly."
            )

        report = build_report(
            presentmon_csv,
            frame_log,
            min_frame_rows=args.min_frame_rows,
            refresh_period_ms=args.refresh_period_ms,
            presentmon_status=presentmon_status,
            presentmon_status_reason=presentmon_status_reason,
        )
    except RefreshHistogramError as exc:
        print(f"refresh_period_histogram: FAIL: {exc}", file=sys.stderr)
        return 1

    # The emitted report always states the status it was built under (sol, #178: a measured-looking
    # histogram must never be separable from the evidence status that permitted it).
    report["presentMonStatus"] = presentmon_status
    report["presentMonStatusReason"] = presentmon_status_reason

    text = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.write("\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
