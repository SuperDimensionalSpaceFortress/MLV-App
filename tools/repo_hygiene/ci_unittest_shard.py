"""Deterministic module-level sharding of the repo-hygiene unittest suite for CI.

``python -m unittest discover -s tools/repo_hygiene -p "test_*.py" -t .`` ran as one
step and took 39-61 minutes on the hosted Windows runner. This splits that same
discovery into N shards that run on parallel runners. A shard is a set of whole test
modules, assigned by longest-processing-time-first over the measured per-module
seconds below, so the split is stable and a new test file is picked up automatically
(it is weighted at ``DEFAULT_WEIGHT_SECONDS``) instead of being silently omitted.

Coverage is proven, not assumed: ``--verify-partition`` loads every shard and fails
unless the shards are disjoint, together cover every discovered module, and together
hold exactly the number of tests that plain ``unittest discover`` collects.
"""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path

START_DIR = "tools/repo_hygiene"
PATTERN = "test_*.py"
TOP_LEVEL_DIR = "."
DEFAULT_WEIGHT_SECONDS = 10

# Seconds per module on hosted windows-latest (PR #225, run 36992317962). Only the
# modules that matter for balancing are listed; the rest take DEFAULT_WEIGHT_SECONDS.
MEASURED_SECONDS = {
    "tools.repo_hygiene.test_dual_venue_evidence": 786,
    "tools.repo_hygiene.test_brokered_closeout": 546,
    "tools.repo_hygiene.test_playback_attr_3_cuda_behaviour": 468,
    "tools.repo_hygiene.test_playback_clip_length_gate": 372,
    "tools.repo_hygiene.test_attr3_footage_stage": 210,
    "tools.repo_hygiene.test_playback_evidence_completeness": 126,
    "tools.repo_hygiene.test_playback_launcher_receipt_oracle": 114,
    "tools.repo_hygiene.test_um_run_sidefiles": 108,
    "tools.repo_hygiene.test_playback_receipt_run_binding": 72,
    "tools.repo_hygiene.test_playback_host_load_gate": 66,
    "tools.repo_hygiene.test_attr3_owner_clip_stage_stall": 60,
    "tools.repo_hygiene.test_playback_attr_3_cuda_contact_sheet": 54,
}


def discover_modules(repo_root: Path) -> list[str]:
    """Dotted names of the test modules ``unittest discover`` finds, sorted."""
    start = repo_root / START_DIR
    return sorted(
        ".".join((*Path(START_DIR).parts, path.stem))
        for path in start.glob(PATTERN)
        if path.is_file()
    )


def assign(modules: list[str], shards: int) -> list[list[str]]:
    """Longest-processing-time-first split of ``modules`` into ``shards`` lists."""
    if shards < 1:
        raise ValueError("shards must be at least 1")
    ordered = sorted(
        modules, key=lambda name: (-MEASURED_SECONDS.get(name, DEFAULT_WEIGHT_SECONDS), name)
    )
    loads = [0] * shards
    buckets: list[list[str]] = [[] for _ in range(shards)]
    for name in ordered:
        target = min(range(shards), key=lambda index: (loads[index], index))
        buckets[target].append(name)
        loads[target] += MEASURED_SECONDS.get(name, DEFAULT_WEIGHT_SECONDS)
    return [sorted(bucket) for bucket in buckets]


def load(names: list[str]) -> unittest.TestSuite:
    loader = unittest.TestLoader()
    return unittest.TestSuite(loader.loadTestsFromName(name) for name in names)


def verify_partition(repo_root: Path, shards: int) -> int:
    modules = discover_modules(repo_root)
    buckets = assign(modules, shards)
    problems: list[str] = []
    flat = [name for bucket in buckets for name in bucket]
    if sorted(flat) != modules:
        problems.append("shards are not a disjoint cover of the discovered modules")
    expected = unittest.TestLoader().discover(
        START_DIR, pattern=PATTERN, top_level_dir=TOP_LEVEL_DIR
    ).countTestCases()
    total = 0
    for index, bucket in enumerate(buckets, start=1):
        count = load(bucket).countTestCases()
        total += count
        print(f"shard {index}/{shards}: {len(bucket)} modules, {count} tests")
        if not bucket or count == 0:
            problems.append(f"shard {index} holds no tests")
    print(f"discover: {len(modules)} modules, {expected} tests; shards total {total}")
    if total != expected:
        problems.append(f"shards hold {total} tests but discover collects {expected}")
    for problem in problems:
        print(f"ERROR: {problem}", file=sys.stderr)
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--of", type=int, required=True, help="total number of shards")
    parser.add_argument("--shard", type=int, help="1-based shard to run")
    parser.add_argument("--verify-partition", action="store_true")
    parser.add_argument("--list", action="store_true", help="print the shard's modules and exit")
    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root).resolve()

    if args.verify_partition:
        return verify_partition(repo_root, args.of)
    if args.shard is None or not 1 <= args.shard <= args.of:
        parser.error("--shard must be between 1 and --of")
    bucket = assign(discover_modules(repo_root), args.of)[args.shard - 1]
    if not bucket:
        print(f"ERROR: shard {args.shard}/{args.of} holds no test modules", file=sys.stderr)
        return 1
    if args.list:
        print("\n".join(bucket))
        return 0
    print(f"shard {args.shard}/{args.of}: {len(bucket)} modules", flush=True)
    result = unittest.TextTestRunner(verbosity=2).run(load(bucket))
    if result.testsRun == 0:
        print("ERROR: no tests ran", file=sys.stderr)
        return 5
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
