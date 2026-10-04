"""Derive per-test-class seconds for ``ci_unittest_shard`` from hosted CI job logs.

``unittest -v`` prints one ``name (module.Class.name) ... ok`` line per test and the
Actions log stamps every line, so the gap to the previous stamped line is the seconds
that test (and anything it printed) took. Summed per class and averaged over several
runs, that is the weight table ``ci_unittest_shard_weights.json`` carries.

Re-derive after a rebalance is wanted (download job logs with
``gh api --allow-escape-sequences repos/<owner>/<repo>/actions/jobs/<id>/logs``)::

    python -m tools.repo_hygiene.ci_unittest_shard_timings \\
        --windows win-run1-shard1.log win-run1-shard2.log ... \\
        --ubuntu ubuntu-run1.log ubuntu-run2.log ... \\
        --out tools/repo_hygiene/ci_unittest_shard_weights.json

Every log of one profile is one job; a class's weight is its summed seconds in a job,
averaged over the jobs of that profile that ran it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

STAMP = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)\.(\d+)Z (.*)$")
TEST_LINE = re.compile(r"^\S+ \(([A-Za-z0-9_.]+)\) \.\.\. (?:ok|FAIL|ERROR|skipped.*|expected failure)$")
KEY_PREFIX = "tools.repo_hygiene.test_"


def _seconds(stamp: re.Match[str]) -> float:
    whole = datetime.strptime(stamp.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return whole.timestamp() + float("0." + stamp.group(2))


def class_seconds(log_text: str) -> dict[str, float]:
    """Seconds per ``module.Class`` in one job log."""
    totals: dict[str, float] = defaultdict(float)
    previous: float | None = None
    for raw in log_text.splitlines():
        stamp = STAMP.match(raw)
        if not stamp:
            continue
        now = _seconds(stamp)
        test = TEST_LINE.match(stamp.group(3))
        if test and previous is not None and test.group(1).startswith(KEY_PREFIX):
            totals[test.group(1).rsplit(".", 1)[0]] += max(0.0, now - previous)
        previous = now
    return dict(totals)


def average(jobs: list[dict[str, float]]) -> dict[str, float]:
    """Mean seconds per class over the jobs that ran it, rounded to 0.1 s."""
    seen: dict[str, list[float]] = defaultdict(list)
    for job in jobs:
        for name, seconds in job.items():
            seen[name].append(seconds)
    return {name: round(sum(values) / len(values), 1) for name, values in sorted(seen.items())}


def build(profiles: dict[str, list[Path]]) -> dict:
    table = {}
    for profile, paths in profiles.items():
        if paths:
            jobs = [class_seconds(path.read_text(encoding="utf-8", errors="replace")) for path in paths]
            table[profile] = average(jobs)
    return {"schema": 1, "unit": "module.Class", "seconds": table}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--windows", nargs="*", default=[], type=Path)
    parser.add_argument("--ubuntu", nargs="*", default=[], type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    result = build({"windows": args.windows, "ubuntu": args.ubuntu})
    if not result["seconds"]:
        print("ERROR: no logs given", file=sys.stderr)
        return 2
    args.out.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    for profile, rows in result["seconds"].items():
        print(f"{profile}: {len(rows)} classes, {sum(rows.values()):.0f} s total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
