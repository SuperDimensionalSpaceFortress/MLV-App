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

A profile may name a heavy set in ``ci_unittest_heavy_set.json``: those classes leave the
N-way partition and run in their own job instead, split into the set's ``parts`` by the same
assignment (``--heavy-part``). The partition and the heavy job read the one file, so what
one excludes the other runs.

Coverage is proven, not assumed: ``--verify-partition`` fails unless every shard's and
every heavy part's test ids, taken together, are exactly the ids plain ``unittest discover``
collects, each once, no heavy class is missing from discover, and no module failed to import.
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
# Unix epoch seconds the workflow's first step exports, so checkout, pip and --verify-partition
# count toward the budget. Optional: without it (or with a value that cannot be right) the clock
# starts at main(), as it always did. A runner that has been up longer than the job cap plus this
# margin cannot be the same job.
JOB_START_ENV = "CI_SHARD_JOB_START_EPOCH"
JOB_START_MARGIN_MINUTES = 10
ORIGIN_JOB_START = "job_start"
ORIGIN_MAIN = "main"
ORIGIN_MAIN_INVALID = "main_invalid_job_start"
WEIGHTS_PATH = Path(__file__).with_name("ci_unittest_shard_weights.json")
HEAVY_SET_PATH = Path(__file__).with_name("ci_unittest_heavy_set.json")


def load_weights(profile: str, path: Path = WEIGHTS_PATH) -> dict[str, float]:
    """Measured seconds per ``module.Class`` for one runner OS."""
    table = json.loads(path.read_text(encoding="utf-8"))["seconds"]
    if not isinstance(table, dict):
        raise ValueError(f"{path.name}: seconds must be an object")
    if profile not in table:
        raise ValueError(f"no weight table for profile {profile!r}; have {sorted(table)}")
    rows = table[profile]
    if not isinstance(rows, dict):
        raise ValueError(f"{path.name}: {profile} must be an object of class -> seconds")
    for name, seconds in rows.items():
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
            raise ValueError(f"{path.name}: {profile} {name} is {seconds!r}, not a finite number of seconds")
    return {name: float(seconds) for name, seconds in rows.items()}


def load_heavy_set(profile: str, path: Path = HEAVY_SET_PATH) -> tuple[list[str], int]:
    """The profile's heavy classes, sorted, and how many parts the heavy job runs; ``([], 0)`` if none."""
    table = json.loads(path.read_text(encoding="utf-8"))
    schema = table.get("schema") if isinstance(table, dict) else None
    if type(schema) is not int or schema != 1:
        raise ValueError(f"{path.name}: unknown schema {schema!r}")
    profiles = table.get("profiles", {})
    if not isinstance(profiles, dict):
        raise ValueError(f"{path.name}: profiles must be an object")
    unknown = sorted(set(profiles) - set(PROFILES))
    if unknown:
        raise ValueError(f"{path.name}: unknown profile(s) {unknown}")
    if profile not in profiles:
        return [], 0
    entry = profiles[profile]
    if not isinstance(entry, dict):
        raise ValueError(f"{path.name}: {profile} must be an object")
    classes = entry.get("classes")
    parts = entry.get("parts")
    if (
        not isinstance(classes, list)
        or not classes
        or not all(isinstance(key, str) and key for key in classes)
        or len(set(classes)) != len(classes)
    ):
        raise ValueError(f"{path.name}: {profile} classes must be a non-empty list of unique class names")
    if not isinstance(parts, int) or isinstance(parts, bool) or not 1 <= parts <= len(classes):
        raise ValueError(f"{path.name}: {profile} parts must be an integer from 1 to {len(classes)}")
    return sorted(classes), parts


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


def plan_buckets(
    counts: dict[str, int], profile: str, shards: int, heavy_path: Path = HEAVY_SET_PATH
) -> tuple[list[list[str]], list[list[str]]]:
    """The ``shards``-way partition of every class outside the heavy set, and the heavy parts.

    A heavy class that ``counts`` does not hold is in neither; ``stale_heavy`` names it.
    """
    weights = load_weights(profile)
    classes, parts = load_heavy_set(profile, heavy_path)
    heavy = set(classes)
    partition = assign({key: n for key, n in counts.items() if key not in heavy}, shards, weights)
    present = {key: counts[key] for key in classes if key in counts}
    return partition, assign(present, parts, weights) if parts else []


def stale_heavy(counts: dict[str, int], profile: str, heavy_path: Path = HEAVY_SET_PATH) -> list[str]:
    """Heavy classes that discover did not collect: renamed or deleted, so nothing would run them."""
    return [key for key in load_heavy_set(profile, heavy_path)[0] if key not in counts]


def plan(
    profile: str, shards: int, heavy_path: Path = HEAVY_SET_PATH
) -> tuple[list[unittest.TestCase], dict[str, list[unittest.TestCase]], list[list[str]], list[list[str]]]:
    tests = collect_tests()
    units = group_units(tests)
    buckets, heavy = plan_buckets({key: len(items) for key, items in units.items()}, profile, shards, heavy_path)
    return tests, units, buckets, heavy


def verify_partition(profile: str, shards: int, heavy_path: Path = HEAVY_SET_PATH) -> int:
    tests, units, buckets, heavy = plan(profile, shards, heavy_path)
    weights = load_weights(profile)
    problems: list[str] = []
    for failed in import_failures(tests):
        problems.append(f"a test module failed to import: {failed}")
    for key in stale_heavy({key: len(items) for key, items in units.items()}, profile, heavy_path):
        problems.append(f"a heavy-set class is not collected by discover: {key}")
    expected = sorted(test.id() for test in tests)
    covered: list[str] = []
    for label, group in (("shard", buckets), ("heavy part", heavy)):
        for index, bucket in enumerate(group, start=1):
            members = shard_tests(units, bucket)
            covered.extend(test.id() for test in members)
            estimate = sum(unit_weight(key, len(units[key]), weights) for key in bucket)
            print(f"{label} {index}/{len(group)}: {len(bucket)} classes, {len(members)} tests, ~{estimate:.0f} s estimated")
            if not members:
                problems.append(f"{label} {index} holds no tests")
    print(
        f"discover: {len({unit_key(t) for t in tests})} classes, {len(expected)} tests; "
        f"shards and heavy parts total {len(covered)}"
    )
    if sorted(covered) != expected:
        missing = sorted(set(expected) - set(covered))
        extra = sorted(set(covered) - set(expected))
        problems.append(
            f"shards and heavy parts are not an exact cover of discover: {len(missing)} missing, "
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


def job_elapsed_seconds(environ: Mapping[str, str], wall_now: float, cap_minutes: float) -> float | None:
    """Seconds since the job start named by ``JOB_START_ENV``, or None when absent or not believable.

    Not believable: not a finite number, in the future, or older than the cap plus
    ``JOB_START_MARGIN_MINUTES`` (a millisecond epoch lands in the future; zero lands far in the past).
    """
    try:
        epoch = float(environ[JOB_START_ENV])
    except (KeyError, ValueError):
        return None
    if not math.isfinite(epoch):
        return None
    elapsed = wall_now - epoch
    if elapsed < 0 or elapsed > (cap_minutes + JOB_START_MARGIN_MINUTES) * 60:
        return None
    return elapsed


def budget_origin(
    environ: Mapping[str, str],
    clock: Callable[[], float],
    main_begin: float,
    wall_clock: Callable[[], float],
    cap_minutes: float,
) -> tuple[float, str]:
    """The budget clock's start on ``clock`` and which origin supplied it.

    The job start when ``JOB_START_ENV`` is believable and not later than ``main_begin`` (a job
    cannot start after the process that runs in it); otherwise ``main_begin``, labelled
    ``ORIGIN_MAIN_INVALID`` if a value was given. Never raises.
    """
    if JOB_START_ENV not in environ:
        return main_begin, ORIGIN_MAIN
    elapsed = job_elapsed_seconds(environ, wall_clock(), cap_minutes)
    if elapsed is not None:
        job_begin = clock() - elapsed
        if job_begin <= main_begin:
            return job_begin, ORIGIN_JOB_START
    return main_begin, ORIGIN_MAIN_INVALID


def slowest_block(
    profile: str, index: int, of: int, total: float, class_seconds: Mapping[str, float], limit: int = TOP_CLASSES
) -> list[str]:
    """The ``SHARD_SLOWEST`` header and the ``limit`` slowest classes, slowest first."""
    ranked = sorted(class_seconds.items(), key=lambda item: (-item[1], item[0]))[:limit]
    lines = [f"SHARD_SLOWEST profile={profile} shard={index}/{of} total={total:.1f}s"]
    lines.extend(f"  {seconds:.1f}s {key}" for key, seconds in ranked)
    return lines


def budget_warning(
    profile: str,
    index: int,
    of: int,
    total: float,
    class_seconds: Mapping[str, float],
    cap_minutes: float,
    origin: str = ORIGIN_MAIN,
) -> list[str]:
    """Empty unless the shard used more than ``BUDGET_WARN_SHARE`` of the job cap.

    ``origin`` names where ``total`` was measured from (``budget_origin``).
    """
    if not class_seconds or total <= cap_minutes * 60 * BUDGET_WARN_SHARE:
        return []
    used = total / (cap_minutes * 60) * 100
    slowest, slowest_seconds = min(class_seconds.items(), key=lambda item: (-item[1], item[0]))
    cap = f"{cap_minutes:g}"
    return [
        f"::warning title=Shard budget::shard {index}/{of} used {used:.0f}% of the {cap} min job cap; "
        f"slowest class {slowest} {slowest_seconds:.1f}s; clock origin {origin}",
        f"SHARD_BUDGET_WARN profile={profile} shard={index}/{of} used={used:.0f}% cap_minutes={cap} "
        f"slowest={slowest} slowest_seconds={slowest_seconds:.1f} origin={origin}",
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
        origin: str = ORIGIN_MAIN,
    ) -> None:
        self._origin = origin
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
            lines = budget_warning(
                self._profile, self._index, self._of, elapsed, self._snapshot(), self._cap_minutes, self._origin
            )
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
    wall_clock: Callable[[], float] = time.time,
) -> int:
    """Run the shard's tests, warning when it passes the budget share and reporting its slowest classes.

    The budget clock starts at the job start when the workflow exported ``JOB_START_ENV`` (read
    through ``wall_clock``) and the value is believable, else at ``started`` (``main()``); the
    warning names which. ``SHARD_SLOWEST`` totals stay measured from ``started``.

    The warning is printed during the run (``BudgetWatch``) and the report from a ``finally``,
    so a cancellation (``KeyboardInterrupt``) gets both before it propagates. Both are
    informational: the exit code is the test outcome alone.

    ``sys.stdout`` is captured once here, before any test runs. ``contextlib.redirect_stdout``
    swaps it for the whole process, so a warning printed by the poll thread to the live
    ``sys.stdout`` would land in whichever test happens to be capturing, and never reach the log.
    """
    out = sys.stdout
    begin = clock() if started is None else started
    env = os.environ if environ is None else environ
    cap_minutes = job_cap_minutes(profile, env)
    budget_begin, origin = budget_origin(env, clock, begin, wall_clock, cap_minutes)
    made: list[ClassTimingResult] = []
    watch = BudgetWatch(
        profile,
        index,
        of,
        cap_minutes,
        budget_begin,
        clock,
        lambda: made[0].snapshot() if made else {},
        poll_seconds=poll_seconds,
        out=out,
        origin=origin,
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
    parser.add_argument("--of", type=int, help="total number of shards (not with --heavy-part)")
    parser.add_argument("--shard", type=int, help="1-based shard to run")
    parser.add_argument("--heavy-part", type=int, help="1-based part of the profile's heavy set to run")
    parser.add_argument("--verify-partition", action="store_true")
    parser.add_argument("--list", action="store_true", help="print the shard's classes and exit")
    parser.add_argument("--list-tests", action="store_true", help="print the shard's test ids and exit")
    args = parser.parse_args(argv)
    started = time.monotonic()
    if args.heavy_part is not None:
        if args.of is not None or args.shard is not None or args.verify_partition:
            parser.error("--heavy-part takes no --of, --shard or --verify-partition")
        parts = load_heavy_set(args.profile)[1]
        if not 1 <= args.heavy_part <= parts:
            parser.error(f"--heavy-part must be between 1 and {parts}, the {args.profile} heavy set's parts")
    elif args.of is None:
        parser.error("--of is required unless --heavy-part is given")
    repo_root = Path(args.repo_root).resolve()
    sys.path.insert(0, str(repo_root))
    os.chdir(repo_root)

    if args.verify_partition:
        return verify_partition(args.profile, args.of)
    if args.heavy_part is None and (args.shard is None or not 1 <= args.shard <= args.of):
        parser.error("--shard must be between 1 and --of")
    tests, units, buckets, heavy = plan(args.profile, args.of or 1)
    failures = import_failures(tests)
    if failures:
        print("ERROR: a test module failed to import: " + ", ".join(failures), file=sys.stderr)
        return 1
    if args.heavy_part is not None:
        stale = stale_heavy({key: len(items) for key, items in units.items()}, args.profile)
        if stale:
            print("ERROR: a heavy-set class is not collected by discover: " + ", ".join(stale), file=sys.stderr)
            return 1
        label, index, of, bucket = "heavy part", args.heavy_part, len(heavy), heavy[args.heavy_part - 1]
    else:
        label, index, of, bucket = "shard", args.shard, args.of, buckets[args.shard - 1]
    members = shard_tests(units, bucket)
    if not members:
        print(f"ERROR: {label} {index}/{of} holds no tests", file=sys.stderr)
        return 1
    if args.list:
        print("\n".join(bucket))
        return 0
    if args.list_tests:
        print("\n".join(test.id() for test in members))
        return 0
    print(f"{label} {index}/{of}: {len(bucket)} classes, {len(members)} tests", flush=True)
    return run_shard(members, args.profile, index, of, started=started)


if __name__ == "__main__":
    sys.exit(main())
