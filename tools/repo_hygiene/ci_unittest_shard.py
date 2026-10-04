"""Deterministic sharding of the repo-hygiene unittest suite for CI.

``python -m unittest discover -s tools/repo_hygiene -p "test_*.py" -t .`` ran as one
step: 24 minutes on the hosted ubuntu runner and 39-61 minutes on the hosted Windows
one. This splits that same discovery into N shards that run on parallel runners.

A shard is a set of whole test classes (``module.Class``), assigned by
longest-processing-time-first over the measured seconds in
``ci_unittest_shard_weights.json`` (one table per runner OS, derived from hosted CI
logs by ``ci_unittest_shard_timings``). Classes, not modules, are the unit because one
module (``test_dual_venue_evidence``) alone is about 850 s on Windows and cannot be
balanced whole. The split is stable, and a test class the table does not know is
weighted by its test count, so a new test file is picked up automatically instead of
being silently omitted.

Coverage is proven, not assumed: ``--verify-partition`` fails unless every shard's test
ids, taken together, are exactly the ids plain ``unittest discover`` collects, each
once, and no module failed to import.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import unittest
from collections.abc import Iterator
from pathlib import Path

START_DIR = "tools/repo_hygiene"
PATTERN = "test_*.py"
TOP_LEVEL_DIR = "."
DEFAULT_TEST_SECONDS = 0.5
PROFILES = ("windows", "ubuntu")
WEIGHTS_PATH = Path(__file__).with_name("ci_unittest_shard_weights.json")


def load_weights(profile: str, path: Path = WEIGHTS_PATH) -> dict[str, float]:
    """Measured seconds per ``module.Class`` for one runner OS."""
    table = json.loads(path.read_text(encoding="utf-8"))["seconds"]
    if profile not in table:
        raise ValueError(f"no weight table for profile {profile!r}; have {sorted(table)}")
    return {name: float(seconds) for name, seconds in table[profile].items()}


def _flatten(suite: unittest.TestSuite) -> Iterator[unittest.TestCase]:
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


def unit_key(test: unittest.TestCase) -> str:
    kind = type(test)
    return f"{kind.__module__}.{kind.__qualname__}"


def collect_tests() -> list[unittest.TestCase]:
    """Every test ``unittest discover`` collects, in its order. Run from the repo root."""
    suite = unittest.TestLoader().discover(START_DIR, pattern=PATTERN, top_level_dir=TOP_LEVEL_DIR)
    return list(_flatten(suite))


def import_failures(tests: list[unittest.TestCase]) -> list[str]:
    """Ids of modules that failed to import; ``discover`` reports them as failing tests."""
    return [test.id() for test in tests if type(test).__name__ == "_FailedTest"]


def group_units(tests: list[unittest.TestCase]) -> dict[str, list[unittest.TestCase]]:
    units: dict[str, list[unittest.TestCase]] = {}
    for test in tests:
        units.setdefault(unit_key(test), []).append(test)
    return units


def unit_weight(key: str, count: int, weights: dict[str, float]) -> float:
    return weights.get(key, DEFAULT_TEST_SECONDS * count)


def assign(units: dict[str, int], shards: int, weights: dict[str, float]) -> list[list[str]]:
    """Longest-processing-time-first split of ``units`` (key -> test count) into shards."""
    if shards < 1:
        raise ValueError("shards must be at least 1")
    ordered = sorted(units, key=lambda key: (-unit_weight(key, units[key], weights), key))
    loads = [0.0] * shards
    buckets: list[list[str]] = [[] for _ in range(shards)]
    for key in ordered:
        target = min(range(shards), key=lambda index: (loads[index], index))
        buckets[target].append(key)
        loads[target] += unit_weight(key, units[key], weights)
    return [sorted(bucket) for bucket in buckets]


def shard_tests(units: dict[str, list[unittest.TestCase]], bucket: list[str]) -> list[unittest.TestCase]:
    """The bucket's tests, class-contiguous and module-contiguous so fixtures run once."""
    return [test for key in sorted(bucket) for test in units[key]]


def plan(profile: str, shards: int) -> tuple[list[unittest.TestCase], dict[str, list[unittest.TestCase]], list[list[str]]]:
    tests = collect_tests()
    units = group_units(tests)
    buckets = assign({key: len(items) for key, items in units.items()}, shards, load_weights(profile))
    return tests, units, buckets


def verify_partition(profile: str, shards: int) -> int:
    tests, units, buckets = plan(profile, shards)
    weights = load_weights(profile)
    problems: list[str] = []
    for failed in import_failures(tests):
        problems.append(f"a test module failed to import: {failed}")
    expected = sorted(test.id() for test in tests)
    covered: list[str] = []
    for index, bucket in enumerate(buckets, start=1):
        members = shard_tests(units, bucket)
        covered.extend(test.id() for test in members)
        estimate = sum(unit_weight(key, len(units[key]), weights) for key in bucket)
        print(f"shard {index}/{shards}: {len(bucket)} classes, {len(members)} tests, ~{estimate:.0f} s estimated")
        if not members:
            problems.append(f"shard {index} holds no tests")
    print(f"discover: {len({unit_key(t) for t in tests})} classes, {len(expected)} tests; shards total {len(covered)}")
    if sorted(covered) != expected:
        missing = sorted(set(expected) - set(covered))
        extra = sorted(set(covered) - set(expected))
        problems.append(
            f"shards are not an exact cover of discover: {len(missing)} missing, "
            f"{len(extra)} extra, {len(covered) - len(set(covered))} repeated"
        )
    for problem in problems:
        print(f"ERROR: {problem}", file=sys.stderr)
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--profile", choices=PROFILES, required=True, help="runner OS weight table")
    parser.add_argument("--of", type=int, required=True, help="total number of shards")
    parser.add_argument("--shard", type=int, help="1-based shard to run")
    parser.add_argument("--verify-partition", action="store_true")
    parser.add_argument("--list", action="store_true", help="print the shard's classes and exit")
    parser.add_argument("--list-tests", action="store_true", help="print the shard's test ids and exit")
    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    sys.path.insert(0, str(repo_root))
    os.chdir(repo_root)

    if args.verify_partition:
        return verify_partition(args.profile, args.of)
    if args.shard is None or not 1 <= args.shard <= args.of:
        parser.error("--shard must be between 1 and --of")
    tests, units, buckets = plan(args.profile, args.of)
    failures = import_failures(tests)
    if failures:
        print("ERROR: a test module failed to import: " + ", ".join(failures), file=sys.stderr)
        return 1
    bucket = buckets[args.shard - 1]
    members = shard_tests(units, bucket)
    if not members:
        print(f"ERROR: shard {args.shard}/{args.of} holds no tests", file=sys.stderr)
        return 1
    if args.list:
        print("\n".join(bucket))
        return 0
    if args.list_tests:
        print("\n".join(test.id() for test in members))
        return 0
    print(f"shard {args.shard}/{args.of}: {len(bucket)} classes, {len(members)} tests", flush=True)
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(members))
    if result.testsRun == 0:
        print("ERROR: no tests ran", file=sys.stderr)
        return 5
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
