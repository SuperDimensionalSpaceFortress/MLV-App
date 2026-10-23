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
import math
import os
import sys
import threading
import time
import unittest
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import TextIO

START_DIR = "tools/repo_hygiene"
PATTERN = "test_*.py"
TOP_LEVEL_DIR = "."
DEFAULT_TEST_SECONDS = 0.5
PROFILES = ("windows", "ubuntu")
# The `timeout-minutes` of the matching shard job in .github/workflows/tests.yml, pinned by
# test_ci_unittest_shard.JobCapPinTests. The job cap is the only time alarm on a shard, so a
# shard that uses more than BUDGET_WARN_SHARE of it warns, naming its slowest class, before a
# cancel leaves no test id behind.
JOB_CAP_MINUTES = {"ubuntu": 30, "windows": 50}
BUDGET_WARN_SHARE = 0.75
TOP_CLASSES = 10
CAP_ENV = "CI_SHARD_JOB_CAP_MINUTES"
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


class ClassTimingResult(unittest.TextTestResult):
    """``TextTestResult`` that also charges wall time to each test's ``module.Class``.

    Time is charged from a moving mark. A finished test is charged the time since the previous
    mark, so under a plain suite a class also carries its ``setUpClass``; ``ClassTimingSuite``
    moves the mark at every class and module fixture so each is charged to its owner.
    ``on_tick`` runs after every charge, outside the lock. Output and pass/fail semantics are
    those of ``TextTestResult``.
    """

    def __init__(
        self,
        *args: object,
        clock: Callable[[], float] = time.monotonic,
        on_tick: Callable[[], None] | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._clock = clock
        self._on_tick = on_tick
        self._lock = threading.Lock()
        self._mark = clock()
        self._current: str | None = None
        self.class_seconds: dict[str, float] = {}

    def startTestRun(self) -> None:  # noqa: N802
        super().startTestRun()
        with self._lock:
            self._mark = self._clock()

    def startTest(self, test: unittest.TestCase) -> None:  # noqa: N802
        self.enter(unit_key(test))
        super().startTest(test)

    def stopTest(self, test: unittest.TestCase) -> None:  # noqa: N802
        super().stopTest(test)
        self.charge(unit_key(test))

    def enter(self, key: str) -> None:
        """The class whose code is about to run, so a snapshot can charge the work in flight."""
        with self._lock:
            self._current = key

    def charge(self, key: str) -> None:
        """Charge the time since the last mark to ``key`` and move the mark."""
        now = self._clock()
        with self._lock:
            self.class_seconds[key] = self.class_seconds.get(key, 0.0) + (now - self._mark)
            self._mark = now
        if self._on_tick is not None:
            self._on_tick()

    def running(self) -> str | None:
        """The class last entered, so a nested fixture step can put it back."""
        with self._lock:
            return self._current

    def snapshot(self) -> dict[str, float]:
        """``class_seconds`` plus the time since the mark, charged to the class now running."""
        with self._lock:
            seconds = dict(self.class_seconds)
            if self._current is not None:
                seconds[self._current] = seconds.get(self._current, 0.0) + (self._clock() - self._mark)
        return seconds


def _class_key(cls: type | None) -> str | None:
    return None if cls is None else f"{cls.__module__}.{cls.__qualname__}"


def _timed(result: unittest.TestResult, key: str | None, step: Callable[..., None], *args: object) -> None:
    enter = getattr(result, "enter", None)
    charge = getattr(result, "charge", None)
    if key is None or enter is None or charge is None:
        step(*args)
        return
    enter(key)
    try:
        step(*args)
    finally:
        charge(key)


class ClassTimingSuite(unittest.TestSuite):
    """A flat suite that charges class and module fixtures to the class that owns them.

    ``unittest`` runs the previous class's ``tearDownClass`` (and ``tearDownModule``) only when
    the next test starts, so charging "time since the last test" blames the next class for
    them. These overrides wrap unittest's own fixture steps; a test in test_ci_unittest_shard
    fails if a CPython release renames one.
    """

    def _tearDownPreviousClass(self, test: object, result: unittest.TestResult) -> None:  # noqa: N802
        previous = getattr(result, "_previousTestClass", None)
        _timed(result, _class_key(previous), super()._tearDownPreviousClass, test, result)

    def _handleModuleTearDown(self, result: unittest.TestResult) -> None:  # noqa: N802
        # unittest calls this from _handleModuleFixture just before the next module's
        # setUpModule, so the class entered for that fixture is put back afterwards: a snapshot
        # taken during setUpModule must name the upcoming class, not the one being torn down.
        running = getattr(result, "running", None)
        outer = running() if running is not None else None
        previous = getattr(result, "_previousTestClass", None)
        _timed(result, _class_key(previous), super()._handleModuleTearDown, result)
        enter = getattr(result, "enter", None)
        if enter is not None and outer is not None:
            enter(outer)

    def _handleModuleFixture(self, test: object, result: unittest.TestResult) -> None:  # noqa: N802
        _timed(result, _class_key(type(test)), super()._handleModuleFixture, test, result)

    def _handleClassSetUp(self, test: object, result: unittest.TestResult) -> None:  # noqa: N802
        _timed(result, _class_key(type(test)), super()._handleClassSetUp, test, result)


def job_cap_minutes(profile: str, environ: Mapping[str, str]) -> float:
    """The profile's job cap; ``CAP_ENV`` overrides it (a short cap for tests)."""
    try:
        override = float(environ[CAP_ENV])
    except (KeyError, ValueError):
        return float(JOB_CAP_MINUTES[profile])
    return override if math.isfinite(override) and override > 0 else float(JOB_CAP_MINUTES[profile])


def slowest_block(
    profile: str, index: int, of: int, total: float, class_seconds: Mapping[str, float], limit: int = TOP_CLASSES
) -> list[str]:
    """The ``SHARD_SLOWEST`` header and the ``limit`` slowest classes, slowest first."""
    ranked = sorted(class_seconds.items(), key=lambda item: (-item[1], item[0]))[:limit]
    lines = [f"SHARD_SLOWEST profile={profile} shard={index}/{of} total={total:.1f}s"]
    lines.extend(f"  {seconds:.1f}s {key}" for key, seconds in ranked)
    return lines


def budget_warning(
    profile: str, index: int, of: int, total: float, class_seconds: Mapping[str, float], cap_minutes: float
) -> list[str]:
    """Empty unless the shard used more than ``BUDGET_WARN_SHARE`` of the job cap."""
    if not class_seconds or total <= cap_minutes * 60 * BUDGET_WARN_SHARE:
        return []
    used = total / (cap_minutes * 60) * 100
    slowest, slowest_seconds = min(class_seconds.items(), key=lambda item: (-item[1], item[0]))
    cap = f"{cap_minutes:g}"
    return [
        f"::warning title=Shard budget::shard {index}/{of} used {used:.0f}% of the {cap} min job cap; "
        f"slowest class {slowest} {slowest_seconds:.1f}s",
        f"SHARD_BUDGET_WARN profile={profile} shard={index}/{of} used={used:.0f}% cap_minutes={cap} "
        f"slowest={slowest} slowest_seconds={slowest_seconds:.1f}",
    ]


class BudgetWatch:
    """Prints the budget warning once, the moment the shard passes ``BUDGET_WARN_SHARE`` of its cap.

    ``check`` runs after every charge and from a daemon poll thread, so one test that runs past
    the threshold still warns while it runs, and a shard cancelled at the cap has already warned.
    """

    def __init__(
        self,
        profile: str,
        index: int,
        of: int,
        cap_minutes: float,
        begin: float,
        clock: Callable[[], float],
        snapshot: Callable[[], Mapping[str, float]],
        *,
        poll_seconds: float = 5.0,
        out: TextIO | None = None,
    ) -> None:
        self._profile = profile
        self._index = index
        self._of = of
        self._cap_minutes = cap_minutes
        self._begin = begin
        self._clock = clock
        self._snapshot = snapshot
        self._poll_seconds = poll_seconds
        self._out = out
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._poll, name="shard-budget-watch", daemon=True)
        self.warned = False

    def check(self) -> None:
        elapsed = self._clock() - self._begin
        if self.warned or elapsed <= self._cap_minutes * 60 * BUDGET_WARN_SHARE:
            return
        with self._lock:
            if self.warned:
                return
            lines = budget_warning(self._profile, self._index, self._of, elapsed, self._snapshot(), self._cap_minutes)
            if lines:
                self.warned = True
                print("\n".join(lines), file=self._out if self._out is not None else sys.stdout, flush=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        if self._thread.is_alive():
            self._thread.join()

    def _poll(self) -> None:
        while not self._stopped.wait(self._poll_seconds):
            self.check()


def run_shard(
    members: list[unittest.TestCase],
    profile: str,
    index: int,
    of: int,
    *,
    clock: Callable[[], float] = time.monotonic,
    environ: Mapping[str, str] | None = None,
    stream: TextIO | None = None,
    started: float | None = None,
    poll_seconds: float = 5.0,
) -> int:
    """Run the shard's tests, warning when it passes the budget share and reporting its slowest classes.

    The warning is printed during the run (``BudgetWatch``) and the report from a ``finally``,
    so a cancellation (``KeyboardInterrupt``) gets both before it propagates. Both are
    informational: the exit code is the test outcome alone.

    ``sys.stdout`` is captured once here, before any test runs. ``contextlib.redirect_stdout``
    swaps it for the whole process, so a warning printed by the poll thread to the live
    ``sys.stdout`` would land in whichever test happens to be capturing, and never reach the log.
    """
    out = sys.stdout
    begin = clock() if started is None else started
    made: list[ClassTimingResult] = []
    watch = BudgetWatch(
        profile,
        index,
        of,
        job_cap_minutes(profile, os.environ if environ is None else environ),
        begin,
        clock,
        lambda: made[0].snapshot() if made else {},
        poll_seconds=poll_seconds,
        out=out,
    )

    def make_result(*args: object, **kwargs: object) -> ClassTimingResult:
        result = ClassTimingResult(*args, clock=clock, on_tick=watch.check, **kwargs)
        made.append(result)
        return result

    runner = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=make_result)
    watch.start()
    try:
        result = runner.run(ClassTimingSuite(members))
    finally:
        watch.stop()
        seconds = made[0].snapshot() if made else {}
        if seconds:
            watch.check()
            print("\n".join(slowest_block(profile, index, of, clock() - begin, seconds)), file=out, flush=True)
    if result.testsRun == 0:
        print("ERROR: no tests ran", file=sys.stderr)
        return 5
    return 0 if result.wasSuccessful() else 1


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
    started = time.monotonic()
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
    return run_shard(members, args.profile, args.shard, args.of, started=started)


if __name__ == "__main__":
    sys.exit(main())
