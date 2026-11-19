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

Cleanup is one mechanism: the final cleanup of a run (``run_shard``, ``run_parallel``) is an ordered list of
named steps (stop a watchdog, end the workers, print a log, remove the work dir, report the problems) run by
``run_cleanup``. Every step runs; an exception met inside a step, or at its entry, is kept, the step is made
again (bounded), and the first exception met goes out only after the last step, the report, has run. The report
itself (``report_problems``) writes a problem whose write was interrupted again before it moves on.

NON-PROMISE: an exception injected inside the driver's own frame, or in its caller's frame around the call
(between bytecodes, outside any step), is not defended; CPython gives no way to defend a frame against itself.
The guarantee is per step: an interrupt anywhere inside a step, or at a step's entry, never skips a later step
or the reporting.
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
    # ``backslashreplace``: a lone surrogate in an id (a test named from arbitrary text) is written as ``\udXXX``, not a crash.
    path.write_text("".join(f"{one_line(test_id)}\n" for test_id in sorted(ids)), encoding="utf-8", errors="backslashreplace", newline="\n")


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


CLEANUP_ATTEMPTS = 3  # an interrupt lands once; a cleanup step is repeatable, so it is made again after one


def attempt(step: Callable[[], object], attempts: int = CLEANUP_ATTEMPTS) -> BaseException | None:
    """Run ``step`` until it completes (at most ``attempts`` times); the FIRST exception met, if any, is returned, never raised.

    A step of a final cleanup (stopping a thread, removing the work dir) can be interrupted anywhere inside the
    call, so one call is not enough and an interrupt must not leave the cleanup before the steps after it ran:
    the caller raises what comes back once every step is done. A step run again must be safe to run again.
    The first exception is kept with ``is None``: an exception's own truth value is its class's business.
    ``attempts`` below 1 still makes one try: a cleanup step is never skipped.
    """
    first: BaseException | None = None
    for _attempt in range(max(1, attempts)):
        try:
            step()
        except BaseException as error:  # noqa: BLE001 - see above
            if first is None:
                first = error
            continue
        return first
    return first


def stop_thread(stopped: threading.Event, thread: threading.Thread) -> BaseException | None:
    """Tell a watchdog thread to stop and wait for it; the first interrupt met (if any) is returned, never raised.

    Setting the event and the wait are both calls an interrupt can land in, the set BEFORE the flag is stored
    included, so the pair is made again after one: a ``stop`` that returns has set the flag and joined unless the
    interrupts outlast every attempt. The caller finishes its cleanup first and then lets the interrupt out, so a
    cancellation is not turned into a pass."""

    def once() -> None:
        stopped.set()
        if thread.is_alive():
            thread.join()

    return attempt(once)


def handed_back(met: BaseException | None) -> BaseException | None:
    """What a cleanup step hands the driver for an interrupt a helper met and contained (``BudgetWatch.stop``,
    ``end_running_workers``): that interrupt, or ``None`` while another exception is already in flight, which is
    the one that goes out (the run is not a pass either way). Called from inside the step, so ``sys.exc_info``
    is the exception the ``finally`` around the driver is handling, if any."""
    return met if sys.exc_info()[0] is None else None


def run_cleanup(
    steps: Sequence[tuple[str, Callable[[], object]]],
    met: list[BaseException] | None = None,
    attempts: int = CLEANUP_ATTEMPTS,
) -> None:
    """THE cleanup driver: run EVERY step, in order; the first exception met is raised once the last one has run.

    A step is a named zero-argument callable that is safe to make again. Each call is made inside this frame's
    ``try``, so an exception raised anywhere in the step, at its very first line included (and in anything it
    calls), is kept and the step is made again, at most ``attempts`` times; then the next step runs, whatever
    happened. A step may also RETURN an exception it met and contained (``BudgetWatch.stop`` hands back the
    interrupt it met): that is kept too, and the step is not made again. ``met`` is the list everything is kept
    in, in the order met, so a later step can see whether anything was (``run_parallel`` skips its accounting
    then); the first entry is what goes out. Every final cleanup in this module (``run_shard``,
    ``run_parallel``) is one call of this, so no cleanup step and no report can be skipped by one before it.

    NON-PROMISE: an exception injected inside this function's own frame, or in its caller's frame around the
    call, between bytecodes and outside any step, is not defended: CPython gives no way to defend a frame
    against itself. The guarantee is per step, as above. ``attempts`` below 1 still makes one try of each step.
    """
    kept: list[BaseException] = [] if met is None else met
    for _name, step in steps:
        for _attempt in range(max(1, attempts)):
            try:
                returned = step()
            except BaseException as error:  # noqa: BLE001 - kept, the step made again, and the next one still run
                kept.append(error)
                continue
            if issubclass(type(returned), BaseException):  # ``type()``: ``isinstance`` would read ``__class__``
                kept.append(returned)  # type: ignore[arg-type]
            break
    if kept:
        raise kept[0]


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

    def stop(self) -> BaseException | None:
        """Tell the thread to stop and wait for it; the interrupt met while waiting (if any), never raised here:
        the caller finishes its cleanup first and then lets it out, so a cancellation is not turned into a pass."""
        return stop_thread(self._stopped, self._thread)

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
        candidate = None
        try:
            found = link.tb_frame.f_locals.get("self")
            if isinstance(found, subprocess.Popen):
                candidate = found
        except BaseException:  # noqa: BLE001 - recovery never replaces the launch's own error
            # A frame that cannot be read may be the constructor's own, and every ``Popen`` below it (a
            # callback's, even another constructor's) can no longer be told from this launch's child: the
            # search ends here, on nothing, rather than adopt a process this launch did not create.
            return None
        if candidate is not None:
            # The first ``Popen`` met walking inward is the constructor the launch called: every frame
            # below it (an audit hook, a callback) can hold some OTHER live ``Popen`` as its own ``self``,
            # which is not this launch's child and must never be adopted, so the search ends here, whether
            # or not the recovery below could read it (a recovery that fails answers for THIS constructor
            # alone and never walks on to a deeper one).
            for _attempt in range(2):  # an interrupt lands once; the recovery is repeatable, so a second go finishes what it left half done
                try:
                    return _created_child(candidate, link)
                except BaseException:  # noqa: BLE001 - the recovery itself was denied or interrupted
                    continue
            return _stored_child(candidate)
        link = link.tb_next
    return None


def _has_kill_identity(candidate: subprocess.Popen[bytes]) -> bool:
    """Whether ``candidate`` holds a LIVE child it can be ended through: a pid, and on Windows the process handle too.

    ``Popen.kill`` on Windows is ``TerminateProcess(self._handle)``, so a pid alone (what ``taskkill`` takes) is no
    direct-kill identity: a child accepted on its pid alone would be reported ended by a kill that raises."""
    if candidate.pid is None or candidate.returncode is not None:
        return False
    return _windows_handle_type() is None or hasattr(candidate, "_handle")


def _stored_child(candidate: subprocess.Popen[bytes]) -> subprocess.Popen[bytes] | None:
    """``candidate`` when it already holds a live child it can be ended through, read without anything that can be denied; else ``None``."""
    try:
        if _has_kill_identity(candidate):
            return candidate
    except BaseException:  # noqa: BLE001 - not even the stored pid could be read: nothing here is known to be a child
        pass
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
    handle_type = _windows_handle_type()
    if candidate.pid is None or (handle_type is not None and not hasattr(candidate, "_handle")):
        walk: types.TracebackType | None = link
        while walk is not None:
            frame = walk.tb_frame
            if frame.f_code.co_name == "_execute_child" and frame.f_locals.get("self") is candidate:
                pid, handle = frame.f_locals.get("pid"), frame.f_locals.get("hp")
                if issubclass(type(pid), int) and not issubclass(type(pid), bool):
                    # ``Popen``'s own order (flag, handle, then pid): the pid, the value a caller tests, is stored
                    # last, so an interrupt in between leaves no pid-only child, and the repeat finishes the rest.
                    candidate._child_created = True  # type: ignore[attr-defined]
                    if handle_type is not None and issubclass(type(handle), int) and not hasattr(candidate, "_handle"):
                        candidate._handle = handle_type(handle)  # type: ignore[attr-defined]
                    if candidate.pid is None:
                        candidate.pid = pid
                break
            walk = walk.tb_next
    if _has_kill_identity(candidate):
        return candidate
    return None


def is_interrupt(error: BaseException) -> bool:
    """Whether ``error`` is a ``KeyboardInterrupt``, decided on its real type.

    ``isinstance`` falls back to ``error.__class__`` when the type test fails, and an exception can make
    that attribute raise (an audit hook's ``SystemExit(0)`` is enough). ``type()`` never reads it.
    """
    return issubclass(type(error), KeyboardInterrupt)


_EXIT_CODE_SLOT = SystemExit.code  # the member descriptor: reads the stored code, whatever a subclass defines
_EXIT_STATUS_MODULUS = 2**32 if os.name == "nt" else 256  # POSIX keeps the low 8 bits of an exit status; a Windows DWORD all 32 (256 is a real failing code there)


def _reads_as_success(code: object) -> bool:
    """Whether the process would exit 0 for ``SystemExit(code)``: ``None``, or an integer congruent to zero.

    A nonzero integer whose low bits are all zero still ends the process with status 0 (``4294967296`` as a
    Windows DWORD, ``256`` as a POSIX status), so it is no failure code: the test is the residue, not ``== 0``.
    Only ``type()``, ``issubclass`` and the ``int`` methods are used, never the code's own (an int subclass can
    make ``%``, ``==``, ``bool()`` or ``repr()`` raise a ``SystemExit(0)``)."""
    if code is None:
        return True
    return issubclass(type(code), int) and int.__eq__(int.__mod__(code, _EXIT_STATUS_MODULUS), 0) is True


def _code_text(code: object) -> str:
    """The exit code for the message, without calling the code's own ``__repr__`` (``int.__repr__`` also
    refuses an integer past the interpreter's digit limit, which is no reason to lose the message)."""
    if code is None:
        return "None"
    try:
        return int.__repr__(code)  # type: ignore[arg-type]
    except BaseException:  # noqa: BLE001 - a huge integer
        return "a multiple of 256"


def never_exits_zero(function: Callable[..., int]) -> Callable[..., int]:
    """A ``SystemExit`` that would end the process with status 0 (a ``None`` or falsy code, or an integer
    congruent to zero such as ``4294967296``; an audit hook can raise one anywhere) is not a pass: it is
    reported and becomes ``SystemExit(1)``, so a run that was cut short cannot end green. A ``SystemExit``
    with a real failing code, and every other exception, passes through untouched."""

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
            if not _reads_as_success(code):
                if type(error) is SystemExit:
                    raise
                # A subclass's own ``code`` is read again by the interpreter at exit: end with a plain one.
                raise SystemExit(code) from error
            emit_error(f"{function.__name__} was ended by {describe_error(error)} (code {_code_text(code)}), which is not a pass")
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

    def stop_watch() -> BaseException | None:  # the interrupt it met (if any) is handed back to the driver
        return handed_back(watch.stop())

    def report_slowest() -> None:
        seconds = made[0].snapshot() if made else {}
        if seconds:
            watch.check()
            failed = emit_text("\n".join(slowest_block(profile, index, of, clock() - begin, seconds)) + "\n", out)
            if failed is not None:  # informational: the exit code stays the test outcome
                emit_text(f"SHARD_SLOWEST could not be printed ({failed})\n", sys.stderr)

    # The final cleanup, run by ``run_cleanup``: both steps run whatever the other met, and an interrupt met in
    # either (or in the run) is not lost: the first one goes out once both are done, so the run is not a pass.
    cleanup = (("stop the budget watch", stop_watch), ("print the slowest classes", report_slowest))
    runner = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=make_result)
    try:
        watch.start()  # inside the try: an interrupt once the thread runs still gets the cleanup (it stops it)
        result = runner.run(ClassTimingSuite(members))
    finally:
        run_cleanup(cleanup)
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


def report_problems(
    problems: Sequence[str], cursor: list[int] | None = None, attempts: int = CLEANUP_ATTEMPTS
) -> BaseException | None:
    """``emit_error`` for EVERY problem, in order; the first exception met, if any, is returned and never raised.

    ``emit_text`` lets a ``KeyboardInterrupt`` through. A problem whose write it cuts short is written AGAIN
    (at most ``attempts`` tries) before the next one is said, because it may be the only line that says where
    the kept evidence is; after the last try the remaining problems are still said. The caller raises what
    comes back once all are said.

    ``cursor`` (a one-item list) is the index of the next problem to say, advanced past each one once it is
    dealt with: a caller that makes this call again after it was cut short (``run_cleanup``) goes on from
    there and never says the earlier ones twice. ``attempts`` below 1 still makes one try of each problem."""
    at = [0] if cursor is None else cursor
    first: BaseException | None = None
    while at[0] < len(problems):
        for _attempt in range(max(1, attempts)):
            try:
                emit_error(problems[at[0]])
            except BaseException as error:  # noqa: BLE001 - this problem is written again; the rest are still said
                if first is None:  # never ``first or error``: truth-testing an exception can itself raise
                    first = error
                continue
            break
        at[0] += 1
    return first


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
            unsignalled = _kill_group_of_unreaped(proc, pid)
            if unsignalled is not None:
                return unsignalled
    except BaseException as error:  # noqa: BLE001 - an audit hook may raise any type, SystemExit included
        return describe_error(error)
    return None


GROUP_KILL_LOCK_SECONDS = 0.5  # how long a kill waits for a reap that is running right now


def _kill_group_of_unreaped(proc: subprocess.Popen[bytes], pgid: int) -> str | None:
    """``SIGKILL`` the worker's process group, but only while the worker is provably UNREAPED; else the reason.

    The group id is the worker's pid, and the OS hands a pid out again once the worker has been reaped, so a
    signal sent after the reap can land in a stranger's group. Two threads end the same worker (the parent
    and the deadline watchdog), and one of them may have reaped it already: a worker with a ``returncode``
    owns no group any more and gets no signal. ``Popen`` keeps its reap under ``_waitpid_lock``, so the look
    at ``returncode`` and the signal are taken under it: a reap that runs at that moment finishes first and
    is seen. A lock that cannot be had within ``GROUP_KILL_LOCK_SECONDS`` leaves the reap state unreadable
    for as long as the signal would take, so no group signal is sent without it: the reason comes back (the
    tree is not confirmed ended) and ``terminate`` still kills the worker itself."""
    lock = getattr(proc, "_waitpid_lock", None)
    held = False
    try:
        if lock is not None:
            try:
                held = bool(lock.acquire(timeout=GROUP_KILL_LOCK_SECONDS))
            except BaseException:  # noqa: BLE001 - not a lock we can take: treated like one that stayed taken
                held = False
            if not held:
                return (
                    "the worker's reap lock could not be taken, so its process group was not signalled "
                    "(its process tree is not confirmed ended)"
                )
        if proc.returncode is not None:
            # Reaped: its pid, and so its group id, may belong to someone else now, so nothing is signalled. The
            # leader is gone but a descendant of it may not be, and nothing here saw it end: never a clean kill.
            return (
                "the worker had already been reaped, so its process group was not signalled "
                "(its process tree is not confirmed ended)"
            )
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:  # the group is already gone
            pass
        return None
    finally:
        if held:
            lock.release()  # type: ignore[union-attr]


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
        kill is a ``killpg``, which never waits (it takes ``_waitpid_lock`` for at most ``GROUP_KILL_LOCK_SECONDS``,
        and that lock is only ever held for the length of a reap here). A requested reap is one look (``poll``): a worker not seen
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


TERMINATE_ATTEMPTS = 2  # an interrupt lands once; a second go at the whole termination is enough to outlast it


def end_running_workers(procs: Sequence[subprocess.Popen[bytes]]) -> tuple[dict[int, str | None], BaseException | None]:
    """End every worker in ``procs`` still running: ``({1-based number: error text or None}, first interrupt)``.

    The look at who is still running is itself a call an interrupt (or an audit hook's exit) can land in, and
    nothing ahead of ``terminate_all`` may keep it from running. A worker that cannot be looked at is ended
    as if it were running, and a look that was cut short ends every worker. Whatever was raised is returned,
    never raised here: the caller finishes the cleanup first and then lets the first one out."""
    first: BaseException | None = None
    leftover: list[int] = []
    try:
        for number, proc in enumerate(procs, start=1):
            try:
                running = proc.poll() is None
            except BaseException as error:  # noqa: BLE001 - poll raised: its state is unknown, so the worker is ended
                if first is None:  # never ``first or error``: truth-testing an exception can itself raise
                    first = error
                running = True
            if running:
                leftover.append(number)
    except BaseException as error:  # noqa: BLE001 - the census loop itself was cut short: end every worker
        if first is None:
            first = error
        leftover = list(range(1, len(procs) + 1))
    results: list[str | None] = ["its termination did not complete"] * len(leftover)
    for _attempt in range(TERMINATE_ATTEMPTS):
        try:
            results = terminate_all([procs[number - 1] for number in leftover])
            break
        except BaseException as error:  # noqa: BLE001 - terminate_all never raises; an interrupt can still land in it
            if first is None:
                first = error
    return dict(zip(leftover, results, strict=True)), first


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

    def stop(self) -> BaseException | None:
        """Tell the thread to stop and wait for it; the interrupt met while waiting (if any), never raised here:
        the caller finishes its cleanup first and then lets it out, so a cancellation is not turned into a pass."""
        return stop_thread(self._stopped, self._thread)


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
    ended_late: dict[int, str | None] = {}  # the final cleanup's per-worker termination result
    met: list[BaseException] = []  # every exception the final cleanup met, in order; ``run_cleanup`` raises the first
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

    def note(problem: str) -> None:  # a cleanup step can be made again: what it records is recorded once
        if problem not in problems:
            problems.append(problem)

    # A once-only marker (``stuck``, ``unread``) is set only AFTER the problem it guards is recorded: one cut short
    # between the two then leaves the problem recorded (``note`` does not record it twice when the step is made
    # again), never a marker that makes every later try skip it. ``kept_at`` ties the work dir to what is said:
    # it is kept whenever a problem says it is, and a kept dir is always said (``note_kept``).
    kept_at = f"kept at {work}"

    def keeping() -> bool:  # unseen output (an unread or unprinted log, a live worker) is evidence: never deleted
        return bool(unread or stuck or handled < len(logs) or any(kept_at in problem for problem in problems))

    def report_unended() -> None:  # what the watchdog could not end (a worker or its tree), once each
        for number, error in sorted(killer.failed.items()):
            if number not in stuck:
                note(f"worker {number}/{len(procs)} could not be terminated: {error}")
                stuck.add(number)

    # ---- The final cleanup: one step each, run in this order by ``run_cleanup`` from the ``finally`` below. ----
    # Every step runs whatever an earlier one met; an exception met in a step (at its first line too) is kept and
    # the step made again; the first one met goes out only after the last step, the report, has run. Each step is
    # safe to make again: a cursor (``handled``, ``said``) or a once-only record (``stuck``, ``note``) says where
    # one that was cut short stops.
    ran: list[str] = []  # the sorted ids the workers ran (set by ``account``)
    tail = [False]  # whether the run's own accounting follows (``decide_tail``)
    said = [0]  # the report's cursor: the next problem to say

    def end_workers() -> BaseException | None:  # an exception left the run early: no worker may be left running
        nonlocal ended_late
        ended_late, returned = end_running_workers([proc for proc, _log in procs])
        return handed_back(returned)  # the interrupt it met and contained (if any): kept by the driver

    def close_logs() -> None:  # a worker the census could not end is recorded once; every log is closed
        for number, (_proc, log) in enumerate(procs, start=1):
            error = ended_late.get(number)
            if error is not None and number not in stuck:
                note(f"worker {number}/{len(procs)} could not be terminated: {error}")
                stuck.add(number)
            try:
                log.close()
            except OSError:
                pass
            except BaseException as close_error:  # noqa: BLE001 - kept; the other logs are still closed
                met.append(close_error)

    def print_left_logs() -> None:
        """An exception in flight (a launch that raised, say) skipped the grouped logs or cut them short: every
        worker from the first one not fully dealt with (``handled``, the cursor) has its log printed here, or
        recorded as kept, because the work dir is deleted below otherwise."""
        nonlocal handled
        if sys.exc_info()[0] is None:
            return
        for number, (path, (proc, _log)) in enumerate(zip(logs, procs, strict=True), start=1):
            if number <= handled:
                continue
            header_error = None
            try:
                header_error = emit_log(f"===== worker {number}/{len(logs)} exit {proc.poll()} =====\n")
            except BaseException as error:  # noqa: BLE001 - poll() itself raised (SystemExit included)
                if is_interrupt(error):
                    met.append(error)  # raised once the problems and the kept paths are reported
                header_error = describe_error(error)
            try:
                text, unreadable = read_log(path)  # re-raises an interrupt: the log is then unread, and kept
            except BaseException as error:  # noqa: BLE001
                met.append(error)
                text, unreadable = "", describe_error(error)
            verb = "read"
            if unreadable is None:
                try:
                    printed = emit_log(text)  # re-raises an interrupt: the log is then not fully printed, and kept
                except BaseException as error:  # noqa: BLE001
                    met.append(error)
                    printed = describe_error(error)
                unreadable, verb = printed or header_error, "printed"
            if unreadable is not None and number not in unread:
                note(f"worker {number}/{len(logs)} log could not be {verb} ({unreadable}); {kept_at}")
                unread.append(number)
            handled = number

    def stop_killer() -> BaseException | None:  # the watchdog kept enforcing the deadline until now
        return handed_back(killer.stop())

    def stop_watch() -> BaseException | None:
        return handed_back(watch.stop())

    def note_kept() -> None:  # where evidence nobody has seen stays is said, once
        if stuck and not unread:  # a worker still running can write to its log after this
            note(
                "worker(s) " + ", ".join(str(number) for number in sorted(stuck))
                + f" could not be ended and may still write to their logs; kept at {work}"
            )
        if handled < len(logs) and not unread and not stuck:  # the logs ``print_left_logs`` could not get to
            note(f"worker log(s) {handled + 1}-{len(logs)} could not all be printed; kept at {work}")
        if keeping() and not any(kept_at in problem for problem in problems):  # a kept dir is never left unsaid
            note(f"worker evidence {kept_at}")

    def remove_work() -> None:
        if not keeping():
            shutil.rmtree(work, ignore_errors=True)

    def decide_tail() -> None:  # the run's own accounting follows only a cleanup that met nothing, nothing in flight
        tail[0] = sys.exc_info()[0] is None and not met

    def account() -> None:
        if not tail[0]:
            return
        ran[:] = sorted(item for record in records if record for item in record["ids"])
        # A class or module fixture that skips never starts its tests (serial counts them as skipped, not run).
        accounted = sorted(ran + [item for record in records if record for item in record["skipped_ids"]])
        expected = sorted(test.id() for test in members)
        if accounted != expected:
            # By multiplicity: two members may share one id(), so a set difference would say 0/0/0 over a real gap.
            want, got = Counter(expected), Counter(accounted)
            missing = sum((want - got).values())
            extra = sum(count for item, count in got.items() if item not in want)
            repeated = sum(count - want[item] for item, count in got.items() if item in want and count > want[item])
            note(f"the workers' ids are not exactly the shard's: {missing} missing, {extra} extra, {repeated} repeated")

    def write_ids() -> None:
        if tail[0] and manifest is not None and not problems:
            write_manifest(manifest, ran)

    def report_slowest() -> None:
        if not (tail[0] and merged):
            return
        try:
            failed = emit_text("\n".join(slowest_block(profile, index, of, clock() - begin, merged)) + "\n", out)
        except BaseException as error:  # noqa: BLE001 - kept; the queued problems (the kept-log paths) are still said
            met.append(error)
            failed = describe_error(error)
        if failed is not None:
            note(f"SHARD_SLOWEST could not be printed ({failed})")

    def report() -> BaseException | None:  # last: everything recorded is said, from where a cut-short try stopped
        return report_problems(problems, said)

    cleanup = (
        ("end the workers still running", end_workers),
        ("close the logs", close_logs),
        ("print the logs an exception skipped", print_left_logs),
        ("stop the deadline watchdog", stop_killer),
        ("record the workers it could not end", report_unended),
        ("stop the budget watch", stop_watch),
        ("say where kept evidence stays", note_kept),
        ("remove the work dir", remove_work),
        ("decide the accounting", decide_tail),
        ("account for the ids", account),
        ("write the manifest", write_ids),
        ("print the slowest classes", report_slowest),
        ("report the problems", report),
    )

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
            if error is not None:  # the other workers were still killed with it (the problem first, then its marker)
                problems.append(f"worker {number}/{len(procs)} could not be terminated: {error}")
                stuck.add(number)
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
                problems.append(f"worker {number}/{len(logs)} log could not be {verb} ({unreadable}); {kept_at}")
                unread.append(number)
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
        # An exception above must not leave a worker running, a watchdog running or a recorded problem unsaid:
        # ``cleanup`` (above) is every step of that, and the driver runs each of them whatever the others met. With
        # nothing met and nothing in flight the run's own accounting and report ran as its last steps; otherwise the
        # first exception met goes out from here, once the problems and the kept paths are said.
        run_cleanup(cleanup, met)
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
