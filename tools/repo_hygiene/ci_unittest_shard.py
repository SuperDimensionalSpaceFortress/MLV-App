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
import contextlib
import functools
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import IO, TextIO

try:
    from .hidden_spawn import no_window_flags
except ImportError:  # run by path (`python tools/repo_hygiene/ci_unittest_shard.py`): no parent package
    from hidden_spawn import no_window_flags

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
# In-job worker processes. Default 1 = the single in-process run every shard has always been; an
# unreadable value is 1 too (fail closed). Above 1 the shard's classes are split by MODULE across
# that many child processes, each with its own TEMP/TMP/TMPDIR.
WORKERS_ENV = "MLV_CI_UNITTEST_WORKERS"
MAX_WORKERS = 8
# A path that receives the sorted ids of every test that actually ran (one per line). Off by default.
MANIFEST_ENV = "MLV_CI_UNITTEST_MANIFEST"
# A worker that has not exited by then is killed and fails the shard (a hang is never a pass).
WORKER_TIMEOUT_ENV = "MLV_CI_UNITTEST_WORKER_TIMEOUT_SECONDS"
DEFAULT_WORKER_TIMEOUT_SECONDS = 2700.0
# Modules that must not be split across workers or run beside one another. All pinned modules in a
# shard share ONE worker, run serially; each worker has its own TEMP/TMP, so a fixed temp name cannot
# collide with another worker's tests either. Each reason is the evidence (file:line in the survey of 807069e7).
SERIAL_PINS: dict[str, str] = {
    # writes FIXED file names under tempfile.gettempdir() (:1370-1525) and spawns pwsh (:2762)
    "tools.repo_hygiene.test_bachelor_agent_heavy_slot_gate": "fixed temp file names; heavy-slot machinery spawns pwsh",
    # os.chdir is process-global (:729) and a run root is gettempdir() itself (:781)
    "tools.repo_hygiene.test_runtime_floor_suites": "os.chdir and a shared temp root",
    # makes a directory junction with mklink /J and asserts a cross-agent root-identity lock (:1088)
    "tools.repo_hygiene.test_playback_attr_3_cuda_behaviour": "junction plus cross-agent shared-lock semantics",
}
PINNED_GROUP = "<serial-pins>"
# What the budget warning names as the slowest class while the workers are still running (their
# per-class seconds only exist once each has exited).
RUNNING_WORKERS_LABEL = "<workers still running>"


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


def collect_tests(
    start_dir: str = START_DIR, pattern: str = PATTERN, top_level_dir: str = TOP_LEVEL_DIR
) -> list[unittest.TestCase]:
    """Every test ``unittest discover`` collects, in its order. Run from the repo root."""
    suite = unittest.TestLoader().discover(start_dir, pattern=pattern, top_level_dir=top_level_dir)
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


def module_order(units: dict[str, list[unittest.TestCase]], keys: Iterable[str]) -> list[str]:
    """``keys`` ordered by module, then by class key, so every module's classes are adjacent.

    Sorting the dotted keys alone is not enough: ``pkg.A``, ``pkg.child.B`` and ``pkg.z`` sort in that
    order, which puts module ``pkg.child`` between two classes of module ``pkg``, and unittest runs
    ``pkg``'s module fixtures twice.
    """
    return sorted(keys, key=lambda key: (type(units[key][0]).__module__, key))


def shard_tests(units: dict[str, list[unittest.TestCase]], bucket: list[str]) -> list[unittest.TestCase]:
    """The bucket's tests, class-contiguous and module-contiguous so fixtures run once."""
    return [test for key in module_order(units, bucket) for test in units[key]]


def worker_count(environ: Mapping[str, str]) -> int:
    """Worker processes for one shard: ``WORKERS_ENV`` as a whole number, capped at ``MAX_WORKERS``.

    Anything else (unset, empty, zero, negative, fractional, non-ASCII digits, absurdly long) is 1,
    the serial run: an unreadable setting never changes what a shard does.
    """
    raw = environ.get(WORKERS_ENV, "").strip()
    if not raw.isascii() or not raw.isdigit() or len(raw) > 6:
        return 1
    return min(max(int(raw), 1), MAX_WORKERS)


def worker_timeout(environ: Mapping[str, str]) -> float:
    """Seconds a worker may run; ``WORKER_TIMEOUT_ENV`` overrides it when it is a positive finite number."""
    try:
        seconds = float(environ[WORKER_TIMEOUT_ENV])
    except (KeyError, ValueError):
        return DEFAULT_WORKER_TIMEOUT_SECONDS
    return seconds if math.isfinite(seconds) and seconds > 0 else DEFAULT_WORKER_TIMEOUT_SECONDS


def plan_workers(
    units: dict[str, list[unittest.TestCase]],
    workers: int,
    weights: Mapping[str, float],
    pins: Mapping[str, str] = SERIAL_PINS,
) -> list[list[str]]:
    """Split a shard's classes into at most ``workers`` groups, whole MODULES per group.

    A module is never split, so its module fixtures run once and its classes stay contiguous. Every
    pinned module present goes into ONE group (they run serially in one worker). Longest-processing-
    time-first over the summed class weights, as ``assign`` does for shards; empty groups are dropped.
    """
    items: dict[str, list[str]] = {}
    for key, tests in units.items():
        module = type(tests[0]).__module__
        items.setdefault(PINNED_GROUP if module in pins else module, []).append(key)
    counts = {name: sum(len(units[key]) for key in keys) for name, keys in items.items()}
    item_weights = {name: sum(unit_weight(key, len(units[key]), weights) for key in keys) for name, keys in items.items()}
    return [
        module_order(units, [key for name in bucket for key in items[name]])
        for bucket in assign(counts, workers, item_weights)
        if bucket
    ]


_LINE_BREAKS = re.compile("[\\\\\r\n\x0b\x0c\x1c\x1d\x1e\x85  ]")


def one_line(test_id: str) -> str:
    """``test_id`` on a single line, reversibly: a backslash or any character ``str.splitlines`` breaks
    on becomes ``\\uXXXX`` (a backslash too, so an id holding a literal ``\\n`` stays distinct from one
    holding a newline). An id with none of them, which is every ordinary one, is returned unchanged."""
    return _LINE_BREAKS.sub(lambda match: f"\\u{ord(match[0]):04x}", test_id)


def write_manifest(path: Path, ids: list[str]) -> None:
    """The ids that ran, sorted, one per line (``one_line``): two runs of one shard are equal iff the files are."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{one_line(test_id)}\n" for test_id in sorted(ids)), encoding="utf-8", newline="\n")


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


# DOTALL and \Z: a class name may hold a line break, which `.` and `$` would not span
_FIXTURE_SKIP = re.compile(r"\A(setUpClass|setUpModule) \((.+)\)\Z", re.DOTALL)


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
        self.started_ids: list[str] = []
        self._started_members: set[int] = set()  # by identity: two members may share one id()
        self.fixture_skips: list[tuple[str, str]] = []

    def addSkip(self, test: unittest.TestCase, reason: str) -> None:  # noqa: N802
        # A setUpClass / setUpModule that raises SkipTest reports ONE skip against a holder named
        # "setUpClass (module.Class)" / "setUpModule (module)" and never starts the tests under it.
        match = _FIXTURE_SKIP.match(test.id())
        if match:
            self.fixture_skips.append((match[1], match[2]))
        super().addSkip(test, reason)

    def skipped_by_fixture(self, members: list[unittest.TestCase]) -> list[str]:
        """Ids of ``members`` that never started because their class or module fixture skipped."""
        classes = {name for kind, name in self.fixture_skips if kind == "setUpClass"}
        modules = {name for kind, name in self.fixture_skips if kind == "setUpModule"}
        return [
            test.id()
            for test in members
            if id(test) not in self._started_members and (unit_key(test) in classes or type(test).__module__ in modules)
        ]

    def startTestRun(self) -> None:  # noqa: N802
        super().startTestRun()
        with self._lock:
            self._mark = self._clock()

    def startTest(self, test: unittest.TestCase) -> None:  # noqa: N802
        self.started_ids.append(test.id())
        self._started_members.add(id(test))  # the member is held by the suite for the whole run: no id reuse
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
                text = "\n".join(lines) + "\n"
                if emit_text(text, self._out) is not None:  # a closed stdout: the warning goes to stderr instead
                    emit_text(text, sys.stderr)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        try:
            if self._thread.is_alive():
                self._thread.join()
        except BaseException:  # noqa: BLE001 - a daemon already told to stop; an interrupt while waiting must not skip the cleanup after it
            pass

    def _poll(self) -> None:
        while not self._stopped.wait(self._poll_seconds):
            self.check()


_TRACEBACK_SLOT = BaseException.__traceback__  # the member descriptor: reads the stored traceback, whatever a subclass defines


def half_built_worker(error: BaseException) -> subprocess.Popen[bytes] | None:
    """The ``Popen`` whose constructor ``error`` cut short after the OS had already created the child.

    ``Popen(...)`` never returns in that case, so the caller holds nothing to end. The object is still
    ``self`` in the constructor's frames, which the error's traceback keeps alive; it carries the child's
    ``pid`` (and, on Windows, its handle) as soon as the child exists. ``None`` when there is no such child.

    Known limit: between ``CreateProcess`` returning and the statement that unpacks its result, the child's
    identity lives only on the interpreter's evaluation stack, in no frame local and on no object. An interrupt
    landing exactly there (a profile hook that raises on the C-return event does it) leaves nothing here to read,
    and no wrapper around the call can close that window, since the same one opens after the wrapper's own call.
    """
    # Read from ``BaseException``'s own slot, never through ``error.__traceback__``: a subclass can turn that
    # attribute into a property that answers ``None`` while the real traceback is there.
    link = _TRACEBACK_SLOT.__get__(error, BaseException)
    while link is not None:
        try:
            candidate = link.tb_frame.f_locals.get("self")
            if isinstance(candidate, subprocess.Popen):
                # The first ``Popen`` met walking inward is the constructor the launch called: every frame
                # below it (an audit hook, a callback) can hold some OTHER live ``Popen`` as its own ``self``,
                # which is not this launch's child and must never be adopted, so the search ends here.
                return _created_child(candidate, link)
        except BaseException:  # noqa: BLE001 - a frame that cannot be read is skipped; recovery never replaces the launch's own error
            pass
        link = link.tb_next
    return None


def _windows_handle_type() -> type[int] | None:
    """``subprocess.Handle`` (the int subclass ``Popen`` keeps a child's process handle in), ``None`` off Windows.

    Looked up in ``sys.modules`` rather than read as ``subprocess.Handle``: the hidden-spawn scan allows only
    constants and a short list of data types as attributes of ``subprocess``, ``Handle`` is not on it, and
    it is no spawn API (this reads a class, it starts nothing)."""
    handle_type = getattr(sys.modules.get("subprocess"), "Handle", None)
    return handle_type if isinstance(handle_type, type) and issubclass(handle_type, int) else None


def _created_child(candidate: subprocess.Popen[bytes], link: types.TracebackType) -> subprocess.Popen[bytes] | None:
    """``candidate`` when its launch had created the child, else ``None``.

    On Windows ``CreateProcess`` returns the child's handle and pid as locals of ``_execute_child``, and
    ``Popen`` stores them on ``self`` a few statements later (after the pipe cleanup): an interrupt in
    between leaves ``self.pid`` unset although the child runs. Those two values are taken from that frame
    (only when it is the constructor's own ``_execute_child`` for this ``candidate``) so it can be ended.
    """
    if candidate.pid is None:
        walk: types.TracebackType | None = link
        while walk is not None:
            frame = walk.tb_frame
            if frame.f_code.co_name == "_execute_child" and frame.f_locals.get("self") is candidate:
                pid, handle = frame.f_locals.get("pid"), frame.f_locals.get("hp")
                if issubclass(type(pid), int) and not issubclass(type(pid), bool):
                    candidate.pid = pid
                    candidate._child_created = True  # type: ignore[attr-defined]
                    handle_type = _windows_handle_type()
                    if handle_type is not None and issubclass(type(handle), int) and not hasattr(candidate, "_handle"):
                        candidate._handle = handle_type(handle)  # type: ignore[attr-defined]
                break
            walk = walk.tb_next
    if candidate.pid is not None and candidate.returncode is None:
        return candidate
    return None


def is_interrupt(error: BaseException) -> bool:
    """Whether ``error`` is a ``KeyboardInterrupt``, decided on its real type.

    ``isinstance`` falls back to ``error.__class__`` when the type test fails, and an exception can make
    that attribute raise (an audit hook's ``SystemExit(0)`` is enough). ``type()`` never reads it.
    """
    return issubclass(type(error), KeyboardInterrupt)


_EXIT_CODE_SLOT = SystemExit.code  # the member descriptor: reads the stored code, whatever a subclass defines


def never_exits_zero(function: Callable[..., int]) -> Callable[..., int]:
    """A ``SystemExit`` with a falsy code (an audit hook can raise one anywhere) is not a pass: it is
    reported and becomes ``SystemExit(1)``, so a run that was cut short cannot end green. A ``SystemExit``
    with a real code, and every other exception, passes through untouched."""

    @functools.wraps(function)
    def guarded(*args: object, **kwargs: object) -> int:
        try:
            return function(*args, **kwargs)
        except SystemExit as error:
            # Decided on the code's own value, never through its methods or attributes: an int subclass can
            # make ``bool()``, ``==``, ``repr()`` or ``__class__`` (which ``isinstance`` reads) raise a
            # ``SystemExit(0)`` from inside this handler. ``type()`` and ``int.__eq__`` read none of them.
            # The code is read from ``SystemExit``'s own slot, not through ``error.code``: a subclass can
            # turn that attribute into a property that raises ``SystemExit(0)``.
            code = _EXIT_CODE_SLOT.__get__(error, SystemExit)
            if not (code is None or (issubclass(type(code), int) and int.__eq__(code, 0) is True)):
                if type(error) is SystemExit:
                    raise
                # A subclass's own ``code`` is read again by the interpreter at exit: end with a plain one.
                raise SystemExit(code) from error
            emit_error(f"{function.__name__} was ended by {describe_error(error)} (code {'None' if code is None else 0}), which is not a pass")
            raise SystemExit(1) from error

    return guarded


@never_exits_zero
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
    manifest: Path | None = None,
) -> int:
    """Run the shard's tests, warning when it passes the budget share and reporting its slowest classes.

    ``manifest``, when given, receives the sorted ids of the tests that ran (``write_manifest``).

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
            failed = emit_text("\n".join(slowest_block(profile, index, of, clock() - begin, seconds)) + "\n", out)
            if failed is not None:  # informational: the exit code stays the test outcome
                emit_text(f"SHARD_SLOWEST could not be printed ({failed})\n", sys.stderr)
    if manifest is not None and made:
        write_manifest(manifest, made[0].started_ids)
    if result.testsRun == 0:
        emit_error("no tests ran")
        return 5
    return 0 if result.wasSuccessful() else 1


def run_worker(spec_path: Path) -> int:
    """One worker process: run the classes named in the spec file, record what ran, exit 0/1/5.

    The parent wrote ``{"classes", "result", "start_dir", "pattern", "top_level_dir"}``. The worker
    re-collects (the same discover), so a class it cannot find fails it rather than dropping tests.
    """
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    units = group_units(collect_tests(spec["start_dir"], spec["pattern"], spec["top_level_dir"]))
    missing = [key for key in spec["classes"] if key not in units]
    if missing:
        emit_error("a class the shard assigned is not collected by discover: " + ", ".join(missing))
        return 1
    members = shard_tests(units, spec["classes"])
    result = unittest.TextTestRunner(verbosity=2, resultclass=ClassTimingResult).run(ClassTimingSuite(members))
    ran = result.testsRun
    record = {
        "ids": result.started_ids,
        "skipped_ids": result.skipped_by_fixture(members),
        "seconds": result.class_seconds,
        "tests_run": ran,
        "ok": result.wasSuccessful(),
    }
    Path(spec["result"]).write_text(json.dumps(record), encoding="utf-8")
    if not result.wasSuccessful():  # a failed fixture cleanup fails a worker that ran no test, as it does serially
        return 1
    if ran == 0:
        emit_error("no tests ran")
        return 5
    return 0


MAX_CLASS_SECONDS = 10_000_000.0  # about 115 days: past any job cap, and a bound that NaN, inf and a huge int all fail


def _worker_record(path: Path, assigned: Sequence[unittest.TestCase] | None = None) -> dict | None:
    """The worker's result record, or ``None`` when it is missing, malformed or contradicts itself.

    With ``assigned`` (the members this worker was given) the record must also agree with them: it may
    time only their classes and name only their ids, so one worker cannot report another's work."""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except BaseException as error:  # noqa: BLE001 - unreadable, not JSON, too deep, or an audit hook's SystemExit: no result
        if is_interrupt(error):
            raise
        return None
    ok = (
        isinstance(record, dict)
        and isinstance(record.get("ids"), list)
        and all(isinstance(item, str) for item in record["ids"])
        and isinstance(record.get("skipped_ids"), list)
        and all(isinstance(item, str) for item in record["skipped_ids"])
        and isinstance(record.get("seconds"), dict)
        and all(type(value) in (int, float) and 0 <= value <= MAX_CLASS_SECONDS for value in record["seconds"].values())
        and type(record.get("tests_run")) is int
        # unittest counts a test when it starts, and ``started_ids`` is appended at the same moment, so a
        # real worker's count is exactly its id count: a record that says otherwise is not one it wrote.
        and record["tests_run"] == len(record["ids"])
        and type(record.get("ok")) is bool
    )
    if ok and assigned is not None:
        owned = {unit_key(test) for test in assigned}
        left = Counter(record["ids"]) + Counter(record["skipped_ids"])
        left.subtract(Counter(test.id() for test in assigned))
        ok = set(record["seconds"]) <= owned and not any(count > 0 for count in left.values())
    return record if ok else None


def read_log(path: Path) -> tuple[str, str | None]:
    """A worker's log and ``None``, or ``""`` and the error text when it cannot be read: one unreadable
    log must not cost the others their output, so a read error is returned, never raised.

    Everything but a ``KeyboardInterrupt`` is contained, ``SystemExit`` included: an audit hook can raise
    one from a read, and ``SystemExit(0)`` let through would end the shard green with the log deleted."""
    try:
        return path.read_text(encoding="utf-8", errors="replace"), None
    except BaseException as error:  # noqa: BLE001 - a lock, a vanished file, a bad device, an audit hook's SystemExit
        if is_interrupt(error):
            raise
        return "", describe_error(error)


def emit_text(text: str, stream: TextIO | None = None) -> str | None:
    """Print ``text`` (no newline added) to ``stream`` (else the live stdout); ``None``, or the error text
    when it cannot be printed. It never raises, so a diagnostic cannot replace the exit code.

    A worker may write bytes that decode to characters the stream cannot encode (a cp1252 console, a
    child with its own ``PYTHONIOENCODING``): those are replaced, never allowed to raise, and whatever is
    raised anyway (a closed or broken pipe, an unknown codec, an audit hook's ``SystemExit``) is returned.
    Only a ``KeyboardInterrupt`` passes.
    """
    try:
        target = sys.stdout if stream is None else stream
        encoding = getattr(target, "encoding", None) or "utf-8"
        print(text.encode(encoding, errors="replace").decode(encoding, errors="replace"), end="", file=target, flush=True)
    except BaseException as error:  # noqa: BLE001 - see above
        if is_interrupt(error):
            raise
        return describe_error(error)
    return None


def emit_log(text: str) -> str | None:
    """Print a worker's log to stdout (``emit_text``): ``None``, or the error text when it cannot be."""
    return emit_text(text)


def emit_error(problem: str) -> None:
    """``ERROR: <problem>`` to stderr; a stderr that cannot be written leaves nothing else to try."""
    emit_text(f"ERROR: {problem}\n", sys.stderr)


WORKER_WATCH_SLICE_SECONDS = 0.2


def late_workers(
    procs: Sequence[subprocess.Popen[bytes]], deadline: float, monotonic: Callable[[], float] = time.monotonic
) -> set[int]:
    """The 1-based numbers of workers not seen to have exited BEFORE ``deadline`` (on ``monotonic``).

    A worker is in time only when the clock, read AFTER a look at it found it gone, is still before
    the deadline (a ``poll()`` can itself take the clock past it, so a reading taken before the look
    proves nothing). A worker first seen gone at or after the deadline is late (a wait that ran out,
    or a parent that was held up while launching, cannot tell an early exit from a late one), and one
    still running then is late too: the caller kills it. Every look is at most
    ``WORKER_WATCH_SLICE_SECONDS`` after the last, and a wait is cut to the time left on a fresh
    reading and never starts once none is left.
    """
    pending = set(range(1, len(procs) + 1))
    late: set[int] = set()
    while pending:
        for number in sorted(pending):
            if procs[number - 1].poll() is not None:
                pending.discard(number)
                if monotonic() >= deadline:
                    late.add(number)
        if not pending:
            break
        remaining = deadline - monotonic()
        if remaining <= 0:
            late |= pending
            break
        try:
            procs[min(pending) - 1].wait(timeout=min(WORKER_WATCH_SLICE_SECONDS, remaining))
        except subprocess.TimeoutExpired:
            pass
    return late


def session_kwargs() -> dict[str, object]:
    """On POSIX a worker gets its own process group, so ending it can end what its tests launched
    (``terminate``); on Windows ``taskkill /T`` walks the tree and nothing is needed at launch."""
    return {} if os.name == "nt" else {"start_new_session": True}


TREE_KILL_SECONDS = 5.0


def describe_error(error: BaseException) -> str:
    """``Type: message`` for an exception, or just its type when rendering the message itself raises.

    A kill error is reported, never allowed to escape, so rendering it must not be able to fail: an audit
    hook or a patched call can raise an exception whose ``__str__`` raises.
    """
    try:
        return f"{type(error).__name__}: {error}"
    except BaseException:  # noqa: BLE001 - __str__ (or __repr__) of the exception raised, even a SystemExit
        try:
            return type(error).__name__
        except BaseException:  # noqa: BLE001 - not even the type name is readable
            return "unprintable exception"


def _kill_tree(proc: subprocess.Popen[bytes]) -> str | None:
    """End every process ``proc`` started (a bounded test's child and grandchild); the error text if not.

    ``taskkill /T`` on Windows, the worker's process group elsewhere. Anything that goes wrong, whatever
    its type, comes back as text and never as an exception: ``terminate`` still kills the worker itself and
    reports both. (A test that starts its own session or group on purpose is out of reach of either.)

    ``taskkill`` exit 128 means the root was already gone, and a tree cannot be walked from a dead root, so
    its descendants may be running: that is reported as "not confirmed", never as a clean kill. (Only a Job
    Object would contain a tree that orphans itself before the kill; the worker is killed through here
    while it is still running, so the window is the one between the poll and the ``taskkill``.)
    """
    pid = getattr(proc, "pid", None)
    if not issubclass(type(pid), int):
        return None
    try:
        if os.name == "nt":
            done = subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)], stdin=subprocess.DEVNULL, capture_output=True,
                timeout=TREE_KILL_SECONDS, creationflags=no_window_flags(), check=False,
            )
            if done.returncode == 128:  # no such process: the root exited first, its descendants were not walked
                return "taskkill found no process 128: the worker had already exited, its process tree is not confirmed ended"
            if done.returncode != 0:
                return f"taskkill exited {done.returncode}"
        else:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:  # the group is already gone
                pass
    except BaseException as error:  # noqa: BLE001 - an audit hook may raise any type, SystemExit included
        return describe_error(error)
    return None


WORKER_REAP_SECONDS = 10.0  # how long a killed worker may take to be reaped before it is reported unended
TERMINATE_BOUND_SECONDS = TREE_KILL_SECONDS + WORKER_REAP_SECONDS + 5.0  # one worker's whole termination


def terminate(proc: subprocess.Popen[bytes], *, reap: bool = True) -> str | None:
    """Kill ``proc`` and its process tree (and reap it when ``reap``); the error text when either the
    worker or its tree could not be ended, else ``None``. It never raises, and nothing in it waits without
    a bound.

    Callers end every owned worker through this, so one worker that cannot be killed (``PermissionError``,
    a ``SystemExit`` from an audit hook, anything) never stops the kill of the others; a worker that could
    not be killed is not waited on, and the worker is killed even when its tree could not be.
    """
    tree_error = _kill_tree(proc)
    tree_note = f"its process tree was not ended ({tree_error})" if tree_error else None
    try:
        proc.kill()
        if reap:
            proc.wait(timeout=WORKER_REAP_SECONDS)
    except subprocess.TimeoutExpired:
        return f"the worker did not exit {WORKER_REAP_SECONDS:g} s after its kill" + (f"; {tree_note}" if tree_note else "")
    except BaseException as error:  # noqa: BLE001 - nothing a kill raises may stop the others being killed
        return describe_error(error) + (f"; {tree_note}" if tree_note else "")
    return tree_note


def _alive(thread: threading.Thread) -> bool:
    """Whether ``thread`` is running; ``False`` when that cannot be read (an interrupt can land in the call
    too), so the caller falls back to its no-thread path instead of letting the interrupt out of a cleanup."""
    try:
        return thread.is_alive()
    except BaseException:  # noqa: BLE001
        return False


def terminate_all(procs: list[subprocess.Popen[bytes]], *, reap: bool = True) -> list[str | None]:
    """``terminate`` every worker at the same time (one thread each); the error text or ``None`` per worker.

    Each ``terminate`` can block for ``TREE_KILL_SECONDS`` in ``taskkill``, so ending the workers one
    after the other would kill the last one that many seconds per worker late. It never raises and never
    waits longer than ``TERMINATE_BOUND_SECONDS`` in all: a worker whose termination has not finished
    (or whose thread died) reads as unended, never as a clean kill.
    """
    results: list[str | None] = ["its termination did not complete"] * len(procs)
    end = time.monotonic() + TERMINATE_BOUND_SECONDS  # the whole bound, from before the first thread is started

    def one(position: int) -> None:
        try:
            results[position] = terminate(procs[position], reap=reap)
        except BaseException as error:  # noqa: BLE001 - terminate never raises; a patched one might
            results[position] = describe_error(error)

    def inline(position: int) -> None:
        """No thread to be had: end the worker here WITHOUT anything that can wait. On Windows the tree kill
        is a ``taskkill`` launch (process creation sits outside its own timeout) and the reap is a wait,
        so neither runs: the worker is killed and the tree is reported as not confirmed. On POSIX the tree
        kill is a ``killpg``, which never waits. A requested reap is one look (``poll``): a worker not seen
        gone is reported. So the total never exceeds the bound however many threads fail."""
        proc = procs[position]
        notes: list[str] = []
        try:
            if os.name == "nt":
                notes.append("no thread was available to end its process tree, so the tree is not confirmed ended")
            else:
                tree_error = _kill_tree(proc)
                if tree_error:
                    notes.append(f"its process tree was not ended ({tree_error})")
            proc.kill()
            if reap and proc.poll() is None:
                notes.append("its exit was not confirmed (no thread was available to wait for it)")
        except BaseException as error:  # noqa: BLE001 - nothing a kill raises may stop the others being killed
            notes.append(describe_error(error))
        results[position] = "; ".join(notes) or None

    threads: list[threading.Thread] = []
    for position in range(len(procs)):
        thread: threading.Thread | None = None
        try:
            thread = threading.Thread(target=one, args=(position,), name="shard-terminate", daemon=True)
            thread.start()
        except BaseException:  # noqa: BLE001 - no thread to be had (the OS refused one, or not even the object could be built), or an interrupt inside the start
            # A ``SystemExit`` or ``KeyboardInterrupt`` here must not abort the cleanup of every worker still
            # running. An interrupt can land after the thread began: that one is kept and joined, not killed twice.
            if thread is not None and _alive(thread):
                threads.append(thread)
            else:
                inline(position)
        else:
            threads.append(thread)
    for thread in threads:
        try:
            thread.join(max(0.0, end - time.monotonic()))
        except BaseException:  # noqa: BLE001 - an interrupt while waiting must not leave the other workers unwaited and unreported
            continue
    return results


class DeadlineKiller:
    """Kills every registered worker once ``clock`` reaches ``deadline``, whatever the parent is doing.

    The parent blocks while launching (a spec write, the status line, a log open, ``Popen``), and
    ``late_workers`` only looks once the launch loop is over, so a worker that is already running could
    pass the deadline unseen. This thread looks every ``WORKER_WATCH_SLICE_SECONDS`` on the same
    injectable clock and kills what is still running at the deadline; ``add`` does the same check at once
    for a worker registered after it. It only kills: reaping and the failure message stay with the parent.
    """

    def __init__(
        self, deadline: float, clock: Callable[[], float], slice_seconds: float = WORKER_WATCH_SLICE_SECONDS
    ) -> None:
        self._deadline = deadline
        self._clock = clock
        self._slice = slice_seconds
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._procs: list[subprocess.Popen[bytes]] = []
        self._inflight: set[int] = set()  # being ended by some thread right now
        self.killed: set[int] = set()  # 1-based, in registration order
        # Workers (or their trees) it could not end; a running one is retried every slice, the record stays.
        self.failed: dict[int, str] = {}
        self._thread = threading.Thread(target=self._run, name="shard-deadline-killer", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def add(self, proc: subprocess.Popen[bytes]) -> None:
        with self._lock:
            if not any(known is proc for known in self._procs):  # a registration finished after an interrupt is not doubled
                self._procs.append(proc)
        self._kill_due()

    def _kill_due(self) -> None:
        """Kill every running worker that is due. The lock is held only to pick them and to record the
        result, never across a kill (a ``taskkill`` can block for its whole bound): ``add`` and the other
        workers' kills are never held up behind it, and the due workers are ended at the same time."""
        with self._lock:
            if self._clock() < self._deadline:
                return
            due: list[tuple[int, subprocess.Popen[bytes]]] = []
            for number, proc in enumerate(self._procs, start=1):
                if number in self.killed or number in self._inflight:
                    continue  # ended already, or another thread is ending it right now
                try:
                    if proc.poll() is not None:
                        continue  # a failure already on record stays: the parent reports it
                except BaseException as exc:  # noqa: BLE001 - the watchdog thread must survive anything
                    self.failed[number] = describe_error(exc)
                    continue
                due.append((number, proc))
                self._inflight.add(number)
        if not due:
            return
        errors: list[str | None] = ["the kill did not complete"] * len(due)
        try:
            errors = terminate_all([proc for _number, proc in due], reap=False)
        finally:
            with self._lock:
                for (number, _proc), error in zip(due, errors, strict=True):
                    self._inflight.discard(number)
                    if error is None:
                        self.killed.add(number)
                        self.failed.pop(number, None)
                    else:
                        self.failed[number] = error  # the others are still killed; the parent reports it

    def _run(self) -> None:
        while not self._stopped.wait(self._slice):
            try:
                self._kill_due()
            except BaseException:  # noqa: BLE001 - one bad slice must not end the watchdog; the next one retries
                pass

    def stop(self) -> None:
        self._stopped.set()
        try:
            if self._thread.is_alive():
                self._thread.join()
        except BaseException:  # noqa: BLE001 - a daemon already told to stop; an interrupt while waiting must not skip the cleanup after it
            pass


@never_exits_zero
def run_parallel(
    members: list[unittest.TestCase],
    profile: str,
    index: int,
    of: int,
    workers: int,
    *,
    start_dir: str = START_DIR,
    pattern: str = PATTERN,
    top_level_dir: str = TOP_LEVEL_DIR,
    repo_root: Path | None = None,
    pins: Mapping[str, str] = SERIAL_PINS,
    weights: Mapping[str, float] | None = None,
    environ: Mapping[str, str] | None = None,
    manifest: Path | None = None,
    timeout: float | None = None,
    started: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    wall_clock: Callable[[], float] = time.time,
    poll_seconds: float = 5.0,
) -> int:
    """Run the shard's tests in up to ``workers`` child processes and fail closed on anything odd.

    Each child is ``python -m tools.repo_hygiene.ci_unittest_shard --worker-spec ...`` with its own
    TEMP/TMP/TMPDIR; its output goes to a log that is printed, grouped, once it has exited. The shard
    exits non-zero on a failing test, a child that exits non-zero, crashes, writes no result or runs
    past ``timeout`` (a wait that ran out is a timeout even if the child exits a moment later; the
    timeout starts before the first launch, and a child whose launch would begin after it is not
    started and fails the shard), or
    when the ids the children ran or skipped in a class or module fixture are not exactly
    ``members``' ids, each once. The budget warning is printed while the children run (a
    ``BudgetWatch``; the class it names is a placeholder until their seconds are known); the merged
    ``SHARD_SLOWEST`` follows, from the children's per-class seconds (inflated by contention: derive
    weights from serial logs only).
    """
    out = sys.stdout
    begin = clock() if started is None else started
    env = os.environ if environ is None else environ
    root_dir = Path.cwd() if repo_root is None else repo_root
    limit = worker_timeout(env) if timeout is None else timeout
    units = group_units(members)
    groups = plan_workers(units, workers, load_weights(profile) if weights is None else weights, pins)
    work = Path(tempfile.mkdtemp(prefix="mlv-ci-workers-"))
    problems: list[str] = []
    records: list[dict | None] = []
    logs: list[Path] = []
    codes: list[int | None] = []
    procs: list[tuple[subprocess.Popen[bytes], IO[bytes]]] = []
    handled = 0  # how many of the grouped worker logs (in order) are fully dealt with: the rest are the fallback's
    unread: list[int] = []  # 1-based workers whose log could not be read: the work dir is kept for them
    stuck: set[int] = set()  # 1-based workers the OS would not let the parent kill
    merged: dict[str, float] = {}
    cap_minutes = job_cap_minutes(profile, env)
    budget_begin, origin = budget_origin(env, clock, begin, wall_clock, cap_minutes)
    watch = BudgetWatch(
        profile,
        index,
        of,
        cap_minutes,
        budget_begin,
        clock,
        lambda: merged or {RUNNING_WORKERS_LABEL: clock() - begin},
        poll_seconds=poll_seconds,
        out=out,
        origin=origin,
    )
    deadline = clock() + limit  # from before the first launch: launching is part of the budget
    killer = DeadlineKiller(deadline, clock)

    def report_unended() -> None:  # what the watchdog could not end (a worker or its tree), once each
        for number, error in sorted(killer.failed.items()):
            if number not in stuck:
                stuck.add(number)
                problems.append(f"worker {number}/{len(procs)} could not be terminated: {error}")

    try:
        watch.start()  # inside the try: a thread that cannot be started still gets the cleanup below
        killer.start()
        for number, classes in enumerate(groups, start=1):
            home = work / f"w{number}"
            temp = home / "tmp"
            temp.mkdir(parents=True)
            spec = {
                "classes": classes,
                "result": str(home / "result.json"),
                "start_dir": start_dir,
                "pattern": pattern,
                "top_level_dir": top_level_dir,
            }
            (home / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
            child_env = {k: v for k, v in os.environ.items() if k != MANIFEST_ENV}
            child_env.update(TEMP=str(temp), TMP=str(temp), TMPDIR=str(temp), **{WORKERS_ENV: "1"})
            tests_in = sum(len(units[key]) for key in classes)
            print(f"worker {number}/{len(groups)}: {len(classes)} classes, {tests_in} tests", flush=True)
            log = (home / "log.txt").open("wb")
            command = [
                sys.executable, "-m", "tools.repo_hygiene.ci_unittest_shard", "--repo-root", str(root_dir),
                "--profile", profile, "--worker-spec", str(home / "spec.json"),
            ]
            # The last step before the launch, after every blocking one (spec write, status line, log
            # open): a worker is never started once the deadline has passed.
            if clock() >= deadline:
                log.close()
                problems.append(f"worker {number}/{len(groups)} not launched: the deadline passed while launching")
                continue
            proc = None
            try:
                proc = subprocess.Popen(
                    command, cwd=root_dir, env=child_env, stdin=subprocess.DEVNULL, stdout=log,
                    stderr=subprocess.STDOUT, creationflags=no_window_flags(), **session_kwargs(),
                )
                procs.append((proc, log))
                logs.append(home / "log.txt")
                killer.add(proc)  # from here the deadline is enforced even while the next launch blocks
            except BaseException as launch_error:
                if proc is None:
                    # Popen never returned, but an interrupt after the OS created the child leaves a built
                    # ``self`` in its own frames: take that over, or nothing would ever end the child.
                    proc = half_built_worker(launch_error)
                if proc is not None:
                    # The worker exists but its registration was cut short (an interrupt between the launch and
                    # the lists): finish it, so the cleanup below kills it, reports its log and keeps the lists paired.
                    if not any(known is proc for known, _known_log in procs):
                        procs.append((proc, log))
                    while len(logs) < len(procs):
                        logs.append(home / "log.txt")
                    killer.add(proc)
                else:
                    # A launch that failed with no child is not in procs: the cleanup below would never close its
                    # log. Closed only after the child (if any) is adopted, so a close that fails cannot outrank
                    # that, and never replaces the launch's own error.
                    with contextlib.suppress(Exception):
                        log.close()
                raise
        watch.check()  # a budget already spent when the workers launch is reported at once
        late = late_workers([proc for proc, _log in procs], deadline, clock)
        running = [number for number, (proc, _log) in enumerate(procs, start=1) if proc.poll() is None]
        ended = dict(zip(running, terminate_all([procs[number - 1][0] for number in running]), strict=True))
        for number, (proc, log) in enumerate(procs, start=1):
            killed = number in ended
            error = ended.get(number)
            if error is not None:  # the other workers were still killed with it
                stuck.add(number)
                problems.append(f"worker {number}/{len(procs)} could not be terminated: {error}")
            killed = killed or number in killer.killed
            if killed or number in late:
                problems.append(
                    f"worker {number}/{len(procs)} timed out after {limit:g} s"
                    + (" and was killed" if killed and number not in stuck else "")
                )
            log.close()
            codes.append(proc.returncode)
        report_unended()
        for path, classes in zip(logs, groups, strict=False):  # logs is a prefix of groups: a launch can stop early
            records.append(_worker_record(path.with_name("result.json"), [test for key in classes for test in units[key]]))
        for record in records:
            if record:
                merged.update({key: float(value) for key, value in record["seconds"].items()})
        watch.check()  # a share crossed since the last poll is reported before any worker log
        for number, (path, code, record) in enumerate(zip(logs, codes, records, strict=True), start=1):
            header_error = emit_log(f"===== worker {number}/{len(logs)} exit {code} =====\n")  # a closed stdout raises here too
            text, unreadable = read_log(path)
            verb = "read"
            if unreadable is None:
                unreadable, verb = emit_log(text) or header_error, "printed"
            if unreadable is not None:  # the other workers' logs are still printed, and this one is kept (see the cleanup)
                unread.append(number)
                problems.append(f"worker {number}/{len(logs)} log could not be {verb} ({unreadable}); kept at {work}")
            if code not in (0, 1, 5):
                problems.append(f"worker {number}/{len(logs)} exited {code}")
            elif record is None:
                problems.append(f"worker {number}/{len(logs)} wrote no readable result (exit {code})")
            elif code != 1 and not record["ok"]:
                problems.append(f"worker {number}/{len(logs)} recorded a failed run but exited {code}")
            elif code == 5 and record["tests_run"] != 0:  # 5 is "no tests ran", never the exit of a run that ran some
                problems.append(f"worker {number}/{len(logs)} exited 5 after running {record['tests_run']} tests")
            handled = number  # a failure later prints only the logs after this one, never one twice
    finally:
        # An exception above must not leave a worker running: every owned worker is attempted, one that
        # cannot be killed is reported, and the watchdog keeps enforcing the deadline until they are done.
        leftover = [number for number, (proc, _log) in enumerate(procs, start=1) if proc.poll() is None]
        ended_late = dict(zip(leftover, terminate_all([procs[number - 1][0] for number in leftover]), strict=True))
        for number, (proc, log) in enumerate(procs, start=1):
            error = ended_late.get(number)
            if error is not None and number not in stuck:
                stuck.add(number)
                problems.append(f"worker {number}/{len(procs)} could not be terminated: {error}")
            with contextlib.suppress(OSError):
                log.close()
        if sys.exc_info()[0] is not None:
            # An exception (a launch that raised, say) skipped the grouped logs, or cut them short: every
            # worker from the first one not fully dealt with keeps its output, which is deleted with the
            # work dir just below.
            for number, (path, (proc, _log)) in enumerate(zip(logs, procs, strict=True), start=1):
                if number <= handled:
                    continue
                header_error = None
                try:
                    header_error = emit_log(f"===== worker {number}/{len(logs)} exit {proc.poll()} =====\n")
                except BaseException as error:  # noqa: BLE001 - poll() itself raised (SystemExit included)
                    if is_interrupt(error):
                        raise
                    header_error = describe_error(error)
                text, unreadable = read_log(path)
                verb = "read"
                if unreadable is None:
                    unreadable, verb = emit_log(text) or header_error, "printed"
                if unreadable is not None:
                    unread.append(number)
                    problems.append(f"worker {number}/{len(logs)} log could not be {verb} ({unreadable}); kept at {work}")
        killer.stop()
        report_unended()
        watch.stop()
        if stuck and not unread:  # a worker still running can write to its log after this: say where it stays
            problems.append(
                "worker(s) " + ", ".join(str(number) for number in sorted(stuck))
                + f" could not be ended and may still write to their logs; kept at {work}"
            )
        if not unread and not stuck:  # unseen output (unread log, live worker) is evidence: never deleted
            shutil.rmtree(work, ignore_errors=True)
        if sys.exc_info()[0] is not None:  # no exit code to carry them: say it before the traceback
            for problem in problems:
                emit_error(problem)
    ran =sorted(item for record in records if record for item in record["ids"])
    # A class or module fixture that skips never starts its tests (serial counts them as skipped, not run).
    accounted = sorted(ran + [item for record in records if record for item in record["skipped_ids"]])
    expected = sorted(test.id() for test in members)
    if accounted != expected:
        # By multiplicity: two members may share one id(), so a set difference would say 0/0/0 over a real gap.
        want, got = Counter(expected), Counter(accounted)
        missing = sum((want - got).values())
        extra = sum(count for item, count in got.items() if item not in want)
        repeated = sum(count - want[item] for item, count in got.items() if item in want and count > want[item])
        problems.append(
            f"the workers' ids are not exactly the shard's: {missing} missing, {extra} extra, {repeated} repeated"
        )
    if manifest is not None and not problems:
        write_manifest(manifest, ran)
    total = clock() - begin
    if merged:
        failed = emit_text("\n".join(slowest_block(profile, index, of, total, merged)) + "\n", out)
        if failed is not None:  # the queued problems (the kept-log paths) are still reported below
            problems.append(f"SHARD_SLOWEST could not be printed ({failed})")
    for problem in problems:
        emit_error(problem)
    if problems or any(code == 1 for code in codes):
        return 1
    return 5 if not ran else 0


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
    parser.add_argument("--manifest", help=f"write the sorted ids that ran to this file (else ${MANIFEST_ENV}; off by default)")
    parser.add_argument("--worker-spec", help=argparse.SUPPRESS)  # internal: one worker of a parallel shard
    args = parser.parse_args(argv)
    started = time.monotonic()
    if args.worker_spec is not None:
        sys.path.insert(0, str(Path(args.repo_root).resolve()))
        os.chdir(Path(args.repo_root).resolve())
        return run_worker(Path(args.worker_spec))
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
        print("\n".join(one_line(key) for key in bucket))
        return 0
    if args.list_tests:
        print("\n".join(one_line(test.id()) for test in members))
        return 0
    print(f"{label} {index}/{of}: {len(bucket)} classes, {len(members)} tests", flush=True)
    manifest_name = args.manifest or os.environ.get(MANIFEST_ENV, "")
    manifest = Path(manifest_name) if manifest_name else None
    workers = worker_count(os.environ)
    if workers > 1:
        return run_parallel(members, args.profile, index, of, workers, manifest=manifest, started=started)
    return run_shard(members, args.profile, index, of, started=started, manifest=manifest)


if __name__ == "__main__":
    sys.exit(main())
