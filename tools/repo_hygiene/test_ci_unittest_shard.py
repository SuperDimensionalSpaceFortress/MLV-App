from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import tempfile
import time
import types
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest import mock

from tools.repo_hygiene import ci_unittest_shard as shard
from tools.repo_hygiene import ci_unittest_shard_timings as timings

REPO_ROOT = Path(__file__).resolve().parents[2]

_TESTS: list[unittest.TestCase] = []
_UNITS: dict[str, list[unittest.TestCase]] = {}
_COUNTS: dict[str, int] = {}


def setUpModule() -> None:  # noqa: N802
    # Collecting imports every test module, so it is done once, and only when this module
    # runs: at import time this module would be collected while still half-defined.
    global _TESTS, _UNITS, _COUNTS
    _TESTS = shard.collect_tests()
    _UNITS = shard.group_units(_TESTS)
    _COUNTS = {key: len(items) for key, items in _UNITS.items()}


class AssignTests(unittest.TestCase):
    def test_every_class_lands_in_exactly_one_shard(self) -> None:
        for profile in shard.PROFILES:
            weights = shard.load_weights(profile)
            for shards in (1, 2, 4, 7):
                with self.subTest(profile=profile, shards=shards):
                    buckets = shard.assign(_COUNTS, shards, weights)
                    self.assertEqual(len(buckets), shards)
                    self.assertEqual(
                        sorted(name for bucket in buckets for name in bucket), sorted(_COUNTS)
                    )

    def test_assignment_is_deterministic_and_independent_of_input_order(self) -> None:
        weights = shard.load_weights("windows")
        reversed_counts = dict(reversed(list(_COUNTS.items())))
        self.assertEqual(shard.assign(_COUNTS, 6, weights), shard.assign(reversed_counts, 6, weights))

    def test_a_class_the_table_does_not_know_is_still_assigned(self) -> None:
        counts = {**_COUNTS, "tools.repo_hygiene.test_brand_new_file.BrandNewTests": 3}
        buckets = shard.assign(counts, 4, shard.load_weights("ubuntu"))
        self.assertEqual(
            sum("tools.repo_hygiene.test_brand_new_file.BrandNewTests" in b for b in buckets), 1
        )

    def test_an_unmeasured_class_is_weighted_by_its_test_count(self) -> None:
        self.assertEqual(shard.unit_weight("x.Y", 10, {}), 10 * shard.DEFAULT_TEST_SECONDS)
        self.assertEqual(shard.unit_weight("x.Y", 10, {"x.Y": 3.0}), 3.0)

    def test_the_heaviest_classes_are_spread_across_shards(self) -> None:
        for profile in shard.PROFILES:
            weights = shard.load_weights(profile)
            heaviest = sorted(weights, key=weights.get, reverse=True)[:4]
            buckets = shard.assign(_COUNTS, 4, weights)
            homes = {index for name in heaviest for index, b in enumerate(buckets) if name in b}
            self.assertEqual(len(homes), 4, profile)

    def test_no_shard_is_far_above_the_mean_load(self) -> None:
        # The point of weighting: the longest shard stays near the mean, so wall time is
        # the mean plus setup rather than the unlucky bucket.
        for profile, shards in (("windows", 7), ("ubuntu", 3)):
            weights = shard.load_weights(profile)
            buckets = shard.assign(_COUNTS, shards, weights)
            loads = [
                sum(shard.unit_weight(key, _COUNTS[key], weights) for key in bucket)
                for bucket in buckets
            ]
            mean = sum(loads) / shards
            self.assertLess(max(loads), mean * 1.15, f"{profile}: {loads}")

    def test_zero_shards_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            shard.assign({"tools.repo_hygiene.test_x.X": 1}, 0, {})

    def test_every_measured_class_still_exists(self) -> None:
        # A renamed or deleted class would leave a stale weight that skews the balance.
        for profile in shard.PROFILES:
            with self.subTest(profile=profile):
                self.assertEqual(set(shard.load_weights(profile)) - set(_COUNTS), set())

    def test_an_unknown_profile_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            shard.load_weights("solaris")


class CoverageTests(unittest.TestCase):
    def test_shards_are_an_exact_cover_of_discover(self) -> None:
        expected = sorted(test.id() for test in _TESTS)
        for profile, shards in (("windows", 7), ("ubuntu", 3)):
            with self.subTest(profile=profile):
                buckets = shard.assign(_COUNTS, shards, shard.load_weights(profile))
                covered = sorted(
                    test.id() for bucket in buckets for test in shard.shard_tests(_UNITS, bucket)
                )
                self.assertEqual(covered, expected)

    def test_a_shard_keeps_each_class_and_module_contiguous(self) -> None:
        # setUpClass/setUpModule fixtures run once per run of adjacent tests, so a shard
        # must not interleave classes or modules.
        buckets = shard.assign(_COUNTS, 7, shard.load_weights("windows"))
        for bucket in buckets:
            keys = [shard.unit_key(test) for test in shard.shard_tests(_UNITS, bucket)]
            collapsed = [key for index, key in enumerate(keys) if index == 0 or key != keys[index - 1]]
            self.assertEqual(len(collapsed), len(set(collapsed)))
            modules = [key.rsplit(".", 1)[0] for key in collapsed]
            collapsed_modules = [m for index, m in enumerate(modules) if index == 0 or m != modules[index - 1]]
            self.assertEqual(len(collapsed_modules), len(set(collapsed_modules)))

    def test_no_collected_module_failed_to_import(self) -> None:
        self.assertEqual(shard.import_failures(_TESTS), [])


WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tests.yml"
WINDOWS_SHARD_JOB = "repo-hygiene-python-windows-shard"
# Seconds a shard job spends outside its tests (checkout with fetch-depth 0, setup-python,
# the pip installs, --verify-partition): 90-130 s measured on hosted Windows, run 38710168638.
SETUP_SECONDS = 150
# A shard is expected at no more than this share of its job's timeout-minutes, so a runner
# twice as slow as the table still finishes. Shard 2/8 was expected at 26 % of its 40 min
# cap by a stale table, took 31 min (78 %), and was cancelled three times on PR #442.
# 50 min cap since 2026-10-17; budget kept at the former 24 min (40 * 60 * 0.6 = 50 * 60 * 0.48).
CAP_SHARE = 0.48
# The absolute expected-time budget per shard. The cap was raised for runner variance, not to
# admit a heavier shard, so growth past this still fails whatever CAP_SHARE and the cap say.
MAX_BUDGET_SECONDS = 24 * 60
# Tests of classes the table has no row for are weighted by count, not measured.
MAX_UNMEASURED_TEST_SHARE = 0.05


UBUNTU_SHARD_JOB = "repo-hygiene-python-ubuntu-shard"


def _workflow_job(job_name: str) -> str:
    text = WORKFLOW.read_text(encoding="utf-8").replace("\r\n", "\n")
    start = re.search(rf"(?m)^  {re.escape(job_name)}:$", text)
    assert start is not None, job_name
    following = re.search(r"(?m)^  [a-z0-9-]+:$", text[start.end() :])
    return text[start.start() : start.end() + following.start() if following else len(text)]


def _windows_shard_job() -> str:
    return _workflow_job(WINDOWS_SHARD_JOB)


class ShardDurationBudgetTests(unittest.TestCase):
    def test_every_windows_unittest_shard_is_expected_within_the_cap_share(self) -> None:
        job = _windows_shard_job()
        cap = re.search(r"(?m)^    timeout-minutes: (\d+)$", job)
        of = re.search(r"ci_unittest_shard --profile windows --of (\d+) --shard", job)
        self.assertIsNotNone(cap)
        self.assertIsNotNone(of)
        budget = int(cap.group(1)) * 60 * CAP_SHARE
        weights = shard.load_weights("windows")
        buckets = shard.assign(_COUNTS, int(of.group(1)), weights)
        expected = [
            SETUP_SECONDS + sum(shard.unit_weight(key, _COUNTS[key], weights) for key in bucket)
            for bucket in buckets
        ]
        for index, seconds in enumerate(expected, start=1):
            self.assertLessEqual(
                seconds,
                budget,
                f"windows shard {index}/{of.group(1)} is expected at {seconds:.0f} s, over "
                f"{CAP_SHARE:.0%} of its {cap.group(1)} min cap ({budget:.0f} s); re-derive "
                "ci_unittest_shard_weights.json from fresh hosted logs or add shards",
            )

    def test_the_expected_time_budget_stays_at_the_original_24_minutes(self) -> None:
        # Raising the job cap 40 -> 50 must not buy a looser per-shard budget: the share
        # constant is tightened so cap * share still comes to the former 24 min.
        cap = re.search(r"(?m)^    timeout-minutes: (\d+)$", _windows_shard_job())
        self.assertIsNotNone(cap)
        self.assertLessEqual(int(cap.group(1)) * 60 * CAP_SHARE, MAX_BUDGET_SECONDS + 1e-6)

    def test_the_table_measures_nearly_every_discovered_test(self) -> None:
        # An unmeasured class costs DEFAULT_TEST_SECONDS per test whatever it really takes;
        # 101 of them hid that shard 2 was at 31 min while the table claimed 10.6. Every
        # profile is pinned (CoS on #449: ubuntu was left out, 22.3 % unmeasured then).
        for profile in shard.PROFILES:
            with self.subTest(profile=profile):
                weights = shard.load_weights(profile)
                unmeasured = {key: n for key, n in _COUNTS.items() if key not in weights}
                share = sum(unmeasured.values()) / sum(_COUNTS.values())
                self.assertLessEqual(
                    share,
                    MAX_UNMEASURED_TEST_SHARE,
                    f"{profile}: {sum(unmeasured.values())} tests in {len(unmeasured)} classes have no "
                    f"measured weight ({share:.0%}); re-run ci_unittest_shard_timings on hosted logs",
                )


class JobCapPinTests(unittest.TestCase):
    def test_the_runner_job_caps_equal_the_workflow_timeout_minutes(self) -> None:
        # The 75 % warning is only a warning about the real cancel point if the runner's cap
        # constant is the job's `timeout-minutes`; a cap change on one side goes red here.
        for profile, job_name in (("ubuntu", UBUNTU_SHARD_JOB), ("windows", WINDOWS_SHARD_JOB)):
            with self.subTest(profile=profile):
                job = _workflow_job(job_name)
                cap = re.search(r"(?m)^    timeout-minutes: (\d+)$", job)
                self.assertIsNotNone(cap, job_name)
                self.assertIn(f"ci_unittest_shard --profile {profile} --of", job)
                self.assertEqual(shard.JOB_CAP_MINUTES[profile], int(cap.group(1)))

    def test_every_profile_has_a_cap(self) -> None:
        self.assertEqual(set(shard.JOB_CAP_MINUTES), set(shard.PROFILES))

    def test_the_warn_threshold_is_three_quarters_of_the_cap(self) -> None:
        self.assertEqual(shard.BUDGET_WARN_SHARE, 0.75)
        self.assertEqual(shard.TOP_CLASSES, 10)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _wait_until(predicate: Callable[[], bool], seconds: float = 10.0) -> None:
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)


def _fake_case(
    clock: FakeClock, name: str, seconds: float, *, fail: bool = False, setup: float = 0.0, teardown: float = 0.0
) -> unittest.TestCase:
    """One test of a class built here (not at module level, so discovery never collects it).

    ``setup`` and ``teardown`` are the fake seconds its setUpClass and tearDownClass take.
    """

    def test_it(self: unittest.TestCase) -> None:
        clock.advance(seconds)
        if fail:
            self.fail("boom")

    members: dict[str, object] = {"test_it": test_it, "__module__": "fake_mod"}
    if setup:
        members["setUpClass"] = classmethod(lambda cls: clock.advance(setup))
    if teardown:
        members["tearDownClass"] = classmethod(lambda cls: clock.advance(teardown))
    cls = type(name, (unittest.TestCase,), members)
    return cls("test_it")


def _run_fake_shard(
    cases: list[tuple[str, float]],
    *,
    profile: str = "ubuntu",
    fail: bool = False,
    environ: dict | None = None,
    fixtures: dict[str, dict[str, float]] | None = None,
    run_kwargs: dict | None = None,
) -> tuple[int, str]:
    clock = FakeClock()
    tests = [_fake_case(clock, name, seconds, fail=fail, **(fixtures or {}).get(name, {})) for name, seconds in cases]
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = shard.run_shard(
            tests, profile, 2, 8, clock=clock, environ=environ or {}, stream=io.StringIO(), **(run_kwargs or {})
        )
    return code, out.getvalue()


class ShardTimingReportTests(unittest.TestCase):
    def _cases_at(self, share: float) -> list[tuple[str, float]]:
        total = shard.JOB_CAP_MINUTES["ubuntu"] * 60 * share
        return [("Quick", 100.0), ("Slow", total - 100.0)]

    def test_74_percent_of_the_cap_prints_no_warning(self) -> None:
        code, out = _run_fake_shard(self._cases_at(0.74))
        self.assertEqual(code, 0)
        self.assertNotIn("SHARD_BUDGET_WARN", out)
        self.assertNotIn("::warning", out)

    def test_exactly_75_percent_prints_no_warning(self) -> None:
        _, out = _run_fake_shard(self._cases_at(0.75))
        self.assertNotIn("SHARD_BUDGET_WARN", out)

    def test_76_percent_warns_and_names_the_slowest_class(self) -> None:
        code, out = _run_fake_shard(self._cases_at(0.76))
        self.assertEqual(code, 0, "a budget warning never changes the exit code")
        self.assertIn("SHARD_BUDGET_WARN", out)
        annotation = [line for line in out.splitlines() if line.startswith("::warning")]
        self.assertEqual(len(annotation), 1, out)
        self.assertTrue(annotation[0].startswith("::warning title=Shard budget::shard 2/8 used 76% of the 30 min job cap;"))
        self.assertIn("slowest class fake_mod.Slow ", annotation[0])
        self.assertNotIn("fake_mod.Quick", annotation[0])

    def test_the_cap_can_be_overridden_by_env_for_a_short_test(self) -> None:
        _, quiet = _run_fake_shard([("Slow", 70.0)])
        self.assertNotIn("SHARD_BUDGET_WARN", quiet)
        _, loud = _run_fake_shard([("Slow", 70.0)], environ={shard.CAP_ENV: "1"})
        self.assertIn("used 117% of the 1 min job cap", loud)

    def test_a_non_finite_or_unusable_cap_override_falls_back_to_the_default(self) -> None:
        # r3 hardening F3: "inf" and "1e1000" parse to infinity, which would mute the warning forever.
        default = float(shard.JOB_CAP_MINUTES["ubuntu"])
        for raw in ("inf", "-inf", "Infinity", "1e1000", "nan", "", "abc", "0", "-5"):
            with self.subTest(raw=raw):
                self.assertEqual(shard.job_cap_minutes("ubuntu", {shard.CAP_ENV: raw}), default)
        self.assertEqual(shard.job_cap_minutes("ubuntu", {shard.CAP_ENV: "1.5"}), 1.5)
        self.assertEqual(shard.job_cap_minutes("ubuntu", {}), default)

    def test_the_windows_profile_uses_the_windows_cap(self) -> None:
        cases = [("Slow", shard.JOB_CAP_MINUTES["windows"] * 60 * 0.8)]
        _, out = _run_fake_shard(cases, profile="windows")
        self.assertIn("of the 50 min job cap", out)

    def test_the_slowest_block_has_a_header_then_classes_descending(self) -> None:
        _, out = _run_fake_shard([("Fast", 5.0), ("Slow", 20.0), ("Mid", 10.0)])
        lines = out.splitlines()
        start = lines.index(next(line for line in lines if line.startswith("SHARD_SLOWEST")))
        self.assertEqual(lines[start], "SHARD_SLOWEST profile=ubuntu shard=2/8 total=35.0s")
        self.assertEqual(
            lines[start + 1 : start + 4],
            ["  20.0s fake_mod.Slow", "  10.0s fake_mod.Mid", "  5.0s fake_mod.Fast"],
        )

    def test_only_the_top_ten_classes_are_listed(self) -> None:
        seconds = {f"fake_mod.C{n:02d}": float(n) for n in range(1, 13)}
        block = shard.slowest_block("windows", 3, 7, 78.0, seconds)
        self.assertEqual(block[0], "SHARD_SLOWEST profile=windows shard=3/7 total=78.0s")
        self.assertEqual(len(block), 1 + 10)
        self.assertEqual(block[1], "  12.0s fake_mod.C12")
        self.assertEqual(block[-1], "  3.0s fake_mod.C03")

    def test_equal_seconds_are_ordered_by_class_name(self) -> None:
        block = shard.slowest_block("ubuntu", 1, 3, 4.0, {"b.B": 2.0, "a.A": 2.0})
        self.assertEqual(block[1:], ["  2.0s a.A", "  2.0s b.B"])

    def test_a_class_is_charged_for_its_setup_gap(self) -> None:
        # Time between one test ending and the next ending, which includes setUpClass of the
        # next class, is charged to the class that owns the test that just finished.
        clock = FakeClock()
        calls: list[str] = []

        class Probe(unittest.TestCase):
            @classmethod
            def setUpClass(cls) -> None:
                clock.advance(7.0)

            def test_one(self) -> None:
                clock.advance(1.0)
                calls.append("ran")

        Probe.__module__ = "fake_mod"
        Probe.__qualname__ = "Probe"
        result = shard.ClassTimingResult(io.StringIO(), True, 0, clock=clock)
        result.startTestRun()
        unittest.TestSuite([Probe("test_one")]).run(result)
        result.stopTestRun()
        self.assertEqual(calls, ["ran"])
        self.assertEqual(result.class_seconds, {"fake_mod.Probe": 8.0})

    def test_class_teardown_is_charged_to_its_owning_class(self) -> None:
        # r2 blocker B2: CleanupHeavy's tearDownClass runs when the next class starts, so it was
        # charged to Quick (51 s) and the report blamed the wrong class.
        _, out = _run_fake_shard(
            [("CleanupHeavy", 1.0), ("Quick", 1.0)], fixtures={"CleanupHeavy": {"teardown": 50.0}}
        )
        lines = out.splitlines()
        start = next(index for index, line in enumerate(lines) if line.startswith("SHARD_SLOWEST"))
        self.assertEqual(
            lines[start + 1 : start + 3], ["  51.0s fake_mod.CleanupHeavy", "  1.0s fake_mod.Quick"]
        )

    def test_the_last_class_teardown_is_charged_to_that_class(self) -> None:
        _, out = _run_fake_shard(
            [("Quick", 1.0), ("CleanupHeavy", 1.0)], fixtures={"CleanupHeavy": {"teardown": 50.0}}
        )
        self.assertIn("  51.0s fake_mod.CleanupHeavy", out.splitlines())
        self.assertIn("  1.0s fake_mod.Quick", out.splitlines())

    def test_class_setup_is_charged_to_the_class_it_sets_up(self) -> None:
        _, out = _run_fake_shard(
            [("Quick", 1.0), ("SetupHeavy", 1.0)], fixtures={"SetupHeavy": {"setup": 30.0}}
        )
        self.assertIn("  31.0s fake_mod.SetupHeavy", out.splitlines())
        self.assertIn("  1.0s fake_mod.Quick", out.splitlines())

    def test_the_timing_suite_overrides_hooks_that_unittest_still_has(self) -> None:
        # ClassTimingSuite wraps unittest's private class/module fixture steps; a CPython that
        # renames one would silently drop the attribution, so it goes red here instead.
        for name in ("_tearDownPreviousClass", "_handleModuleFixture", "_handleModuleTearDown", "_handleClassSetUp"):
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(unittest.TestSuite, name, None)))
                self.assertIn(name, vars(shard.ClassTimingSuite))


class BudgetWarningDuringRunTests(unittest.TestCase):
    """r2 blocker B1: the warning is emitted when the threshold is crossed, not after the run.

    r3 adds where it is emitted (the job's real stdout) and which class it names.
    """

    def test_a_cancelled_shard_has_already_warned_and_still_reports(self) -> None:
        clock = FakeClock()
        out = io.StringIO()
        seen: list[str] = []

        class Cancelled(unittest.TestCase):
            def test_a_long(self) -> None:
                clock.advance(46.0)  # 77 % of the 60 s cap

            def test_b_cancel(self) -> None:
                seen.append(out.getvalue())
                clock.advance(15.0)
                raise KeyboardInterrupt

        Cancelled.__module__ = "fake_mod"
        Cancelled.__qualname__ = "Cancelled"
        tests = [Cancelled("test_a_long"), Cancelled("test_b_cancel")]
        with contextlib.redirect_stdout(out), self.assertRaises(KeyboardInterrupt):
            shard.run_shard(tests, "ubuntu", 2, 8, clock=clock, environ={shard.CAP_ENV: "1"}, stream=io.StringIO())
        self.assertEqual(len(seen), 1)
        self.assertIn("::warning title=Shard budget::shard 2/8 used 77% of the 1 min job cap;", seen[0])
        lines = out.getvalue().splitlines()
        self.assertEqual(sum(line.startswith("::warning") for line in lines), 1, out.getvalue())
        self.assertEqual(sum(line.startswith("SHARD_BUDGET_WARN") for line in lines), 1)
        self.assertIn("SHARD_SLOWEST profile=ubuntu shard=2/8 total=61.0s", lines)

    def test_the_watch_warns_once_at_the_moment_the_cap_share_is_crossed(self) -> None:
        clock = FakeClock()
        result = shard.ClassTimingResult(io.StringIO(), True, 0, clock=clock)
        watch = shard.BudgetWatch("ubuntu", 2, 8, 1.0, 0.0, clock, result.snapshot)
        out = io.StringIO()
        result.startTestRun()
        result.startTest(_fake_case(clock, "Slow", 0.0))
        with contextlib.redirect_stdout(out):
            clock.advance(44.0)
            watch.check()
            self.assertEqual(out.getvalue(), "")
            clock.advance(2.0)  # 46 s of a 60 s cap, still inside the running test
            watch.check()
            clock.advance(5.0)
            watch.check()
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 2, out.getvalue())
        self.assertIn("used 77% of the 1 min job cap; slowest class fake_mod.Slow 46.0s", lines[0])

    def test_the_poll_thread_warns_while_a_test_is_still_running(self) -> None:
        begin = time.monotonic() - 100.0  # already past 75 % of a 1 min cap
        watch = shard.BudgetWatch("ubuntu", 1, 3, 1.0, begin, time.monotonic, lambda: {"a.A": 1.0}, poll_seconds=0.01)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            watch.start()
            deadline = time.monotonic() + 10.0
            while not watch.warned and time.monotonic() < deadline:
                time.sleep(0.01)
            watch.stop()
        self.assertTrue(watch.warned)
        self.assertIn("::warning title=Shard budget::", out.getvalue())

    # r3 blockers F1 (warning swallowed by a test's redirect_stdout) and F2 (setUpModule time).
    def test_a_warning_fired_inside_a_tests_redirect_stdout_reaches_the_real_stdout(self) -> None:
        clock = FakeClock()
        real = io.StringIO()
        captured = io.StringIO()

        class Quiet(unittest.TestCase):
            def test_a_long(self) -> None:
                clock.advance(44.0)  # 73 % of the 60 s cap

            def test_b_capturing(self) -> None:
                with contextlib.redirect_stdout(captured):
                    clock.advance(12.0)  # crosses 75 % while sys.stdout is `captured`
                    _wait_until(lambda: "SHARD_BUDGET_WARN" in real.getvalue() + captured.getvalue())

        Quiet.__module__ = "fake_mod"
        Quiet.__qualname__ = "Quiet"
        tests = [Quiet("test_a_long"), Quiet("test_b_capturing")]
        with contextlib.redirect_stdout(real):
            code = shard.run_shard(
                tests, "ubuntu", 2, 8, clock=clock, environ={shard.CAP_ENV: "1"}, stream=io.StringIO(), poll_seconds=0.01
            )
        self.assertEqual(code, 0)
        self.assertEqual(captured.getvalue(), "", "the budget warning must not land in a test's captured stdout")
        lines = real.getvalue().splitlines()
        self.assertEqual(sum(line.startswith("::warning") for line in lines), 1, real.getvalue())
        self.assertEqual(sum(line.startswith("SHARD_BUDGET_WARN") for line in lines), 1, real.getvalue())
        self.assertTrue(any(line.startswith("SHARD_SLOWEST") for line in lines))

    def test_setup_module_time_is_charged_to_the_upcoming_class_in_the_warning(self) -> None:
        clock = FakeClock()
        out = io.StringIO()
        previous_module = types.ModuleType("key_previous")
        next_module = types.ModuleType("key_next")

        def set_up_module() -> None:
            clock.advance(46.0)  # 77 % of the 60 s cap, spent in the NEXT module's setUpModule
            _wait_until(lambda: "SHARD_BUDGET_WARN" in out.getvalue())

        next_module.setUpModule = set_up_module  # type: ignore[attr-defined]

        def make(module: str, name: str, seconds: float) -> unittest.TestCase:
            def test_it(self: unittest.TestCase) -> None:
                clock.advance(seconds)

            cls = type(name, (unittest.TestCase,), {"test_it": test_it, "__module__": module})
            return cls("test_it")

        tests = [make("key_previous", "Quick", 1.0), make("key_next", "SetupHeavy", 1.0)]
        with mock.patch.dict(sys.modules, {"key_previous": previous_module, "key_next": next_module}):
            with contextlib.redirect_stdout(out):
                shard.run_shard(
                    tests, "ubuntu", 2, 8, clock=clock, environ={shard.CAP_ENV: "1"}, stream=io.StringIO(), poll_seconds=0.01
                )
        lines = out.getvalue().splitlines()
        annotation = [line for line in lines if line.startswith("::warning")]
        self.assertEqual(len(annotation), 1, out.getvalue())
        self.assertIn("slowest class key_next.SetupHeavy 46.0s", annotation[0])
        self.assertNotIn("key_previous.Quick", annotation[0])
        self.assertIn("  47.0s key_next.SetupHeavy", lines)
        self.assertIn("  1.0s key_previous.Quick", lines)


class JobStartOriginTests(unittest.TestCase):
    """CI-SHARD-BUDGET-WARN-JOB-ELAPSED-1: the budget clock can start at the runner's job start.

    ``main()`` starts after checkout, pip and --verify-partition, so a shard with 2 minutes of
    setup used to warn at 82 % of its cap instead of 75 %. ``CI_SHARD_JOB_START_EPOCH`` (exported
    by the workflow's first step) moves the origin to the job start; ``main()`` stays the fallback.
    """

    def _warning_lines(self, out: str) -> tuple[list[str], list[str]]:
        lines = out.splitlines()
        return (
            [line for line in lines if line.startswith("::warning")],
            [line for line in lines if line.startswith("SHARD_BUDGET_WARN")],
        )

    def test_setup_time_before_main_counts_toward_the_warning(self) -> None:
        # 1 min cap: the tests use 20 s (33 %), the job started 30 s before run_shard (real clock).
        env = {shard.CAP_ENV: "1", shard.JOB_START_ENV: str(time.time() - 30.0)}
        code, out = _run_fake_shard([("Slow", 20.0)], environ=env)
        annotation, record = self._warning_lines(out)
        self.assertEqual(code, 0)
        self.assertEqual(len(annotation), 1, out)
        self.assertEqual(len(record), 1, out)
        self.assertRegex(annotation[0], r"used (8\d)% of the 1 min job cap")
        self.assertIn("origin=job_start", record[0])
        self.assertIn("clock origin job_start", annotation[0])

    def test_the_same_run_without_the_epoch_does_not_warn(self) -> None:
        _, out = _run_fake_shard([("Slow", 20.0)], environ={shard.CAP_ENV: "1"})
        self.assertNotIn("SHARD_BUDGET_WARN", out)

    def test_two_minutes_of_ubuntu_setup_warns_at_75_percent_of_the_job_not_82(self) -> None:
        # The card's example: 30 min cap, 2 min of setup. 21 min of tests is 70 % of the cap, 76.7 % of the job.
        env = {shard.JOB_START_ENV: "1000"}
        code, out = _run_fake_shard([("Slow", 21 * 60.0)], environ=env, run_kwargs={"wall_clock": lambda: 1120.0})
        annotation, record = self._warning_lines(out)
        self.assertEqual(code, 0)
        self.assertEqual(len(annotation), 1, out)
        self.assertIn("used 77% of the 30 min job cap", annotation[0])
        self.assertIn("origin=job_start", record[0])
        # The class table and its total stay what the tests themselves took.
        self.assertIn("SHARD_SLOWEST profile=ubuntu shard=2/8 total=1260.0s", out.splitlines())

    def test_without_the_epoch_main_is_the_origin_and_says_so(self) -> None:
        _, out = _run_fake_shard([("Slow", 46.0)], environ={shard.CAP_ENV: "1"})
        annotation, record = self._warning_lines(out)
        self.assertIn("used 77% of the 1 min job cap", annotation[0])
        self.assertIn("origin=main", record[0])
        self.assertNotIn("origin=main_invalid_job_start", record[0])
        self.assertIn("clock origin main", annotation[0])

    def test_an_unbelievable_epoch_falls_back_to_main_and_still_warns(self) -> None:
        now = time.time()
        bad = ["nan", "inf", "-inf", "1e1000", "abc", "", "0", "-5", str(now + 3600), str(now - 3600), str(now * 1000)]
        for raw in bad:
            with self.subTest(raw=raw):
                code, out = _run_fake_shard([("Slow", 46.0)], environ={shard.CAP_ENV: "1", shard.JOB_START_ENV: raw})
                annotation, record = self._warning_lines(out)
                self.assertEqual(code, 0)
                self.assertEqual(len(annotation), 1, out)
                self.assertIn("used 77% of the 1 min job cap", annotation[0])
                self.assertIn("origin=main_invalid_job_start", record[0])

    def test_an_epoch_after_main_started_is_not_believed(self) -> None:
        # main began 100 s ago on the monotonic clock; a job start only 10 s ago cannot be right.
        clock = FakeClock()
        clock.advance(100.0)
        tests = [_fake_case(clock, "Slow", 46.0)]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            shard.run_shard(
                tests,
                "ubuntu",
                2,
                8,
                clock=clock,
                environ={shard.CAP_ENV: "1", shard.JOB_START_ENV: "990"},
                stream=io.StringIO(),
                started=0.0,
                wall_clock=lambda: 1000.0,
            )
        annotation, record = self._warning_lines(out.getvalue())
        self.assertEqual(len(annotation), 1, out.getvalue())
        self.assertIn("origin=main_invalid_job_start", record[0])

    def test_job_elapsed_seconds_accepts_a_sane_epoch_only(self) -> None:
        self.assertEqual(shard.job_elapsed_seconds({shard.JOB_START_ENV: "1000"}, 1120.0, 30.0), 120.0)
        self.assertEqual(shard.job_elapsed_seconds({shard.JOB_START_ENV: "1000.5"}, 1120.5, 30.0), 120.0)
        self.assertIsNone(shard.job_elapsed_seconds({}, 1120.0, 30.0))
        self.assertIsNone(shard.job_elapsed_seconds({shard.JOB_START_ENV: "1121"}, 1120.0, 30.0), "future")
        limit = (30.0 + shard.JOB_START_MARGIN_MINUTES) * 60
        self.assertEqual(shard.job_elapsed_seconds({shard.JOB_START_ENV: "0"}, limit, 30.0), limit)
        self.assertIsNone(shard.job_elapsed_seconds({shard.JOB_START_ENV: "0"}, limit + 1, 30.0), "older than cap + margin")
        for raw in ("nan", "inf", "-inf", "1e1000", "abc", ""):
            with self.subTest(raw=raw):
                self.assertIsNone(shard.job_elapsed_seconds({shard.JOB_START_ENV: raw}, 1120.0, 30.0))

    def test_the_budget_warning_line_carries_its_origin(self) -> None:
        lines = shard.budget_warning("ubuntu", 1, 3, 100.0, {"a.A": 1.0}, 1.0, origin="job_start")
        self.assertTrue(lines[0].endswith("clock origin job_start"), lines[0])
        self.assertTrue(lines[1].endswith("origin=job_start"), lines[1])
        self.assertEqual(shard.budget_warning("ubuntu", 1, 3, 100.0, {"a.A": 1.0}, 1.0)[1].split()[-1], "origin=main")


class ShardExitCodeTests(unittest.TestCase):
    def test_a_failing_test_still_exits_one(self) -> None:
        code, out = _run_fake_shard([("Slow", 1.0)], fail=True)
        self.assertEqual(code, 1)
        self.assertIn("SHARD_SLOWEST", out)

    def test_a_failing_test_over_budget_still_exits_one(self) -> None:
        code, out = _run_fake_shard([("Slow", 1700.0)], fail=True)
        self.assertEqual(code, 1)
        self.assertIn("SHARD_BUDGET_WARN", out)

    def test_a_passing_test_over_budget_still_exits_zero(self) -> None:
        code, _ = _run_fake_shard([("Slow", 1700.0)])
        self.assertEqual(code, 0)

    def test_no_tests_ran_still_exits_five(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = shard.run_shard([], "ubuntu", 1, 3, clock=FakeClock(), environ={}, stream=io.StringIO())
        self.assertEqual(code, 5)


class TimingsTests(unittest.TestCase):
    LOG = (
        "2026-10-04T00:00:00.0000000Z started\n"
        "2026-10-04T00:00:10.5000000Z test_a (tools.repo_hygiene.test_x.AlphaTests.test_a) ... ok\n"
        "2026-10-04T00:00:12.0000000Z test_b (tools.repo_hygiene.test_x.AlphaTests.test_b) ... ok\n"
        "2026-10-04T00:00:20.0000000Z test_c (tools.repo_hygiene.test_x.BetaTests.test_c) ... skipped 'no'\n"
        "2026-10-04T00:00:21.0000000Z unrelated (somewhere.else.Tests.unrelated) ... ok\n"
        "garbage without a stamp\n"
    )

    def test_seconds_are_the_gap_to_the_previous_stamped_line_per_class(self) -> None:
        self.assertEqual(
            timings.class_seconds(self.LOG),
            {"tools.repo_hygiene.test_x.AlphaTests": 12.0, "tools.repo_hygiene.test_x.BetaTests": 8.0},
        )

    def test_a_test_with_a_docstring_is_counted_to_its_class(self) -> None:
        # unittest -v prints the id on one line and "<docstring line> ... ok" on the next.
        log = (
            "2026-10-04T00:00:00.0000000Z started\n"
            "2026-10-04T00:00:01.0000000Z test_a (tools.repo_hygiene.test_x.AlphaTests.test_a)\n"
            "2026-10-04T00:00:04.5000000Z the docstring first line ... ok\n"
            "2026-10-04T00:00:04.5000000Z test_b (tools.repo_hygiene.test_x.BetaTests.test_b)\n"
            "2026-10-04T00:00:05.0000000Z another docstring ... FAIL\n"
        )
        self.assertEqual(
            timings.class_seconds(log),
            {"tools.repo_hygiene.test_x.AlphaTests": 3.5, "tools.repo_hygiene.test_x.BetaTests": 0.5},
        )

    def test_a_docstring_test_that_logs_a_line_keeps_its_whole_runtime(self) -> None:
        # CoS on #449: a stamped line between the id line and "<docstring> ... ok" (a logged
        # warning) cleared the pending test, so its result line was never counted. The test
        # keeps head -> result, the warning's line included; only a new id line replaces it.
        log = (
            "2026-10-04T00:00:00.0000000Z started\n"
            "2026-10-04T00:00:01.0000000Z test_a (tools.repo_hygiene.test_x.AlphaTests.test_a)\n"
            "2026-10-04T00:00:03.0000000Z WARNING: something the test logged\n"
            "2026-10-04T00:00:06.0000000Z the docstring first line ... ok\n"
            "2026-10-04T00:00:06.0000000Z test_b (tools.repo_hygiene.test_x.BetaTests.test_b)\n"
            "2026-10-04T00:00:07.0000000Z WARNING: logged by a test whose id line is replaced\n"
            "2026-10-04T00:00:08.0000000Z test_c (tools.repo_hygiene.test_x.GammaTests.test_c)\n"
            "2026-10-04T00:00:09.5000000Z another docstring ... ok\n"
        )
        self.assertEqual(
            timings.class_seconds(log),
            {"tools.repo_hygiene.test_x.AlphaTests": 5.0, "tools.repo_hygiene.test_x.GammaTests": 1.5},
        )

    def test_an_unexpected_success_docstring_test_clears_pending(self) -> None:
        # fable r1-p1 on #629: "unexpected success" is a result line too. Left unmatched it
        # never cleared the pending test, so test_a went uncounted.
        log = (
            "2026-10-04T00:00:00.0000000Z started\n"
            "2026-10-04T00:00:01.0000000Z test_a (tools.repo_hygiene.test_x.A.test_a)\n"
            "2026-10-04T00:00:03.0000000Z x ... unexpected success\n"
            "2026-10-04T00:00:04.0000000Z test_b (tools.repo_hygiene.test_x.B.test_b)\n"
            "2026-10-04T00:00:05.0000000Z y ... ok\n"
        )
        self.assertEqual(
            timings.class_seconds(log),
            {"tools.repo_hygiene.test_x.A": 2.0, "tools.repo_hygiene.test_x.B": 1.0},
        )

    def test_a_one_line_unexpected_success_is_counted_to_its_class(self) -> None:
        log = (
            "2026-10-04T00:00:00.0000000Z started\n"
            "2026-10-04T00:00:03.0000000Z test_a (tools.repo_hygiene.test_x.A.test_a) ... unexpected success\n"
            "2026-10-04T00:00:04.0000000Z test_b (tools.repo_hygiene.test_x.B.test_b) ... ok\n"
        )
        self.assertEqual(
            timings.class_seconds(log),
            {"tools.repo_hygiene.test_x.A": 3.0, "tools.repo_hygiene.test_x.B": 1.0},
        )

    def test_a_pending_test_that_never_printed_a_result_keeps_its_interval(self) -> None:
        # Dot R18 on #629: a docstring id line, then a plain "... ok" line with no result line
        # for the first test (crash, interleaved output). The plain line closes the pending
        # test: head -> plain line is charged to the PENDING class, as for a result line, and
        # the plain test is not charged (its own time is not separable from the pending one's).
        log = (
            "2026-10-04T00:00:00.0000000Z started\n"
            "2026-10-04T00:00:01.0000000Z test_a (tools.repo_hygiene.test_x.A.test_a)\n"
            "2026-10-04T00:00:05.0000000Z test_b (tools.repo_hygiene.test_x.B.test_b) ... ok\n"
            "2026-10-04T00:00:06.0000000Z test_c (tools.repo_hygiene.test_x.C.test_c) ... ok\n"
        )
        self.assertEqual(
            timings.class_seconds(log),
            {"tools.repo_hygiene.test_x.A": 4.0, "tools.repo_hygiene.test_x.C": 1.0},
        )

    def test_average_is_over_the_jobs_that_ran_the_class(self) -> None:
        self.assertEqual(
            timings.average([{"a.A": 10.0, "b.B": 4.0}, {"a.A": 20.0}]), {"a.A": 15.0, "b.B": 4.0}
        )

    def test_main_writes_a_table_the_shard_tool_can_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "job.log"
            log.write_text(self.LOG, encoding="utf-8")
            out = Path(tmp) / "weights.json"
            self.assertEqual(timings.main(["--windows", str(log), "--out", str(out)]), 0)
            self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["schema"], 1)
            self.assertEqual(
                shard.load_weights("windows", out)["tools.repo_hygiene.test_x.AlphaTests"], 12.0
            )
            self.assertEqual(timings.main(["--out", str(out)]), 2)


if __name__ == "__main__":
    unittest.main()
