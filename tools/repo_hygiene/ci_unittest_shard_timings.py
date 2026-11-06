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
RESULT = r"(?:ok|FAIL|ERROR|skipped.*|expected failure|unexpected success)"
TEST_LINE = re.compile(rf"^\S+ \(([A-Za-z0-9_.]+)\) \.\.\. {RESULT}$")
KEY_PREFIX = "tools.repo_hygiene.test_"
# A test with a docstring prints ``name (module.Class.name)`` on its own line and
# ``<first docstring line> ... ok`` on the next, so TEST_LINE alone never sees it.
DOC_HEAD = re.compile(r"^(\S+) \(([A-Za-z0-9_.]+)\)$")
DOC_RESULT = re.compile(rf"^.* \.\.\. {RESULT}$")
# The shard tool's own per-class figure, printed after every job's `SHARD_SLOWEST profile=... total=...s`
# header (its TOP_CLASSES slowest classes, setUpClass included): `  316.2s tools.repo_hygiene.test_x.Cls`.
SLOWEST_HEAD = re.compile(r"^SHARD_SLOWEST profile=\S+ shard=\S+ total=[0-9]+(?:\.[0-9]+)?s$")
# The runner prints one decimal; a bounded digit count keeps float() finite (400 digits is inf).
SLOWEST_ROW = re.compile(r"^\s+([0-9]{1,7}(?:\.[0-9]{1,3})?)s ([A-Za-z0-9_.]+)$")


def _seconds(stamp: re.Match[str]) -> float:
    whole = datetime.strptime(stamp.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return whole.timestamp() + float("0." + stamp.group(2))


def _doc_head(text: str) -> str | None:
    """The test id of a docstring id line, or None for any other line.

    A logged line such as ``Running (1.2)`` has the same shape, so the id must be under
    ``KEY_PREFIX`` or the displayed name must be the id's last component.
    """
    head = DOC_HEAD.match(text)
    if head and (head.group(2).startswith(KEY_PREFIX) or head.group(2).rsplit(".", 1)[-1] == head.group(1)):
        return head.group(2)
    return None


def class_seconds(log_text: str) -> dict[str, float]:
    """Seconds per ``module.Class`` in one job log."""
    totals: dict[str, float] = defaultdict(float)
    previous: float | None = None
    pending: str | None = None
    for raw in log_text.splitlines():
        stamp = STAMP.match(raw)
        if not stamp:
            continue
        now = _seconds(stamp)
        text = stamp.group(3)
        test = TEST_LINE.match(text)
        doc_head = None if test else _doc_head(text)
        if test:
            # A plain result line while a docstring test is pending: that test never printed
            # its result, so it keeps head -> this line and the plain test is not charged.
            name, pending = (pending or test.group(1)), None
        elif doc_head:
            # unittest -v prints a docstring test's id line when the test STARTS, so the gap from the
            # previous stamped line to it is the class's setUpClass (and the previous test's teardown),
            # which a plain one-line test is charged too. The clock therefore stays on the previous
            # line and the result line is charged previous -> result. An id line that replaces a
            # still-pending one abandons it (its interval is not separable) and restarts the clock;
            # so does an id line with no earlier stamped line or one stamped before the previous line.
            if pending is not None or previous is None or now < previous:
                previous = now
            name, pending = None, doc_head
            continue
        elif pending is not None and DOC_RESULT.match(text):
            name, pending = pending, None
        elif pending is not None:
            # A line the pending docstring test logged: it stays pending, and its clock keeps
            # running from its id line, so the result line is charged head -> result.
            continue
        else:
            name = None
        if name and previous is not None and name.startswith(KEY_PREFIX):
            totals[name.rsplit(".", 1)[0]] += max(0.0, now - previous)
        previous = now
    # The tool's own figure for a class it names REPLACES the gap estimate, in both directions. The gap
    # estimate is low where a class logs lines without a result (git warnings; CandidateAcceptanceTests
    # read 8.8 s here against 62.7 s in its own job's SHARD_SLOWEST) and high where a silent
    # tearDownClass of the previous class lands in the gap before a docstring test (that gap is charged
    # to the next class as its setUpClass, so the teardown is counted twice). A class the block does not
    # name was outside the job's top ten, so it ran no longer than the block's smallest figure: that is
    # the ceiling for its gap estimate (a looser one if a log holds several blocks, and one a truncated
    # block only loosens, because rows are sorted slowest first). Below the ceiling the estimate stands.
    in_slowest = False
    named: dict[str, float] = {}
    block_mins: list[float] = []
    for raw in log_text.splitlines():
        stamp = STAMP.match(raw)
        if not stamp:
            continue
        text = stamp.group(3)
        if SLOWEST_HEAD.match(text):
            in_slowest = True
            block_mins.append(float("inf"))
            continue
        row = SLOWEST_ROW.match(text) if in_slowest else None
        if row is None:
            in_slowest = False
            continue
        value = float(row.group(1))
        block_mins[-1] = min(block_mins[-1], value)
        if row.group(2).startswith(KEY_PREFIX):
            named[row.group(2)] = value
    ceiling = max((m for m in block_mins if m != float("inf")), default=None)  # a block with no row says nothing
    if ceiling is not None:
        for name in totals:
            if name not in named:
                totals[name] = min(totals[name], ceiling)
    totals.update(named)
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
