from __future__ import annotations

import contextlib
import io
import json
import linecache
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest import mock

from tools.repo_hygiene import ci_unittest_shard as shard
from tools.repo_hygiene import ci_unittest_shard_timings as timings
from tools.repo_hygiene.hidden_spawn import no_window_flags

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

    def test_a_malformed_weight_table_is_refused(self) -> None:
        # Adversary A1 sibling (r1, 980a921c): a boolean or non-finite weight is not seconds.
        bad = (
            {"schema": 1, "seconds": {"windows": {"a.A": True}}},
            {"schema": 1, "seconds": {"windows": {"a.A": "3"}}},
            {"schema": 1, "seconds": {"windows": {"a.A": None}}},
            {"schema": 1, "seconds": {"windows": {"a.A": -1.0}}},
            {"schema": 1, "seconds": {"windows": ["a.A"]}},
            {"schema": 1, "seconds": ["windows"]},
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "weights.json"
            for table in bad:
                with self.subTest(table=table):
                    path.write_text(json.dumps(table), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        shard.load_weights("windows", path)
            path.write_text('{"schema": 1, "seconds": {"windows": {"a.A": Infinity}}}', encoding="utf-8")
            with self.assertRaises(ValueError):
                shard.load_weights("windows", path)
            path.write_text(json.dumps({"schema": 1, "seconds": {"windows": {"a.A": 2, "b.B": 0.5}}}), encoding="utf-8")
            self.assertEqual(shard.load_weights("windows", path), {"a.A": 2.0, "b.B": 0.5})

    def test_worker_count_fails_closed_to_one(self) -> None:
        # Unreadable is 1 (today's serial run), never a guess; a real count is capped.
        for raw in (None, "", " ", "1", "0", "-2", "abc", "2.5", "2e1", "٣", "9" * 5000):
            with self.subTest(raw=raw):
                env = {} if raw is None else {shard.WORKERS_ENV: raw}
                self.assertEqual(shard.worker_count(env), 1)
        self.assertEqual(shard.worker_count({shard.WORKERS_ENV: " 3 "}), 3)
        self.assertEqual(shard.worker_count({shard.WORKERS_ENV: "999"}), shard.MAX_WORKERS)

    @staticmethod
    def _module_of(key: str) -> str:
        return type(_UNITS[key][0]).__module__

    def test_plan_workers_covers_each_class_once_keeps_modules_whole_and_pins_together(self) -> None:
        weights = shard.load_weights("windows")
        for workers in (2, 4, shard.MAX_WORKERS):
            with self.subTest(workers=workers):
                groups = shard.plan_workers(_UNITS, workers, weights, shard.SERIAL_PINS)
                self.assertLessEqual(len(groups), workers)
                self.assertTrue(all(groups))
                self.assertEqual(sorted(key for group in groups for key in group), sorted(_UNITS))
                owners: dict[str, set[int]] = {}
                for number, group in enumerate(groups):
                    for key in group:
                        owners.setdefault(self._module_of(key), set()).add(number)
                self.assertTrue(all(len(seen) == 1 for seen in owners.values()), "a module was split")
                pinned = {next(iter(seen)) for module, seen in owners.items() if module in shard.SERIAL_PINS}
                self.assertEqual(len(pinned), 1, "pinned modules must share one serial worker")

    def test_every_serial_pin_is_a_discovered_module_with_a_reason(self) -> None:
        modules = {self._module_of(key) for key in _UNITS}
        self.assertTrue(shard.SERIAL_PINS)
        for module, reason in shard.SERIAL_PINS.items():
            with self.subTest(module=module):
                self.assertIn(module, modules, "a stale pin: discover no longer collects this module")
                self.assertTrue(reason.strip(), "a pin names why the module must not run beside others")


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

    # r12 sol r11 A2: a package and its child module interleave when classes sort by dotted string.
    def test_a_package_and_its_child_module_run_each_module_fixture_once(self) -> None:
        calls: list[str] = []
        modules: dict[str, types.ModuleType] = {}
        for name in ("pkg", "pkg.child", "other"):
            module = types.ModuleType(name)
            module.setUpModule = lambda name=name: calls.append(name)  # type: ignore[attr-defined]
            modules[name] = module
        cases = []
        for module_name, class_name in (("pkg", "A"), ("pkg", "z"), ("pkg.child", "B"), ("other", "D")):
            cls = type(class_name, (unittest.TestCase,), {"test_one": lambda self: None, "__module__": module_name})
            cases.append(cls("test_one"))
        units = shard.group_units(cases)
        weights = {"pkg.A": 0.1, "pkg.z": 0.1, "pkg.child.B": 0.1, "other.D": 10.0}
        groups = shard.plan_workers(units, 2, weights, {})
        self.assertEqual(sorted(key for group in groups for key in group), sorted(units))
        orders = [shard.shard_tests(units, group) for group in groups] + [shard.shard_tests(units, sorted(units))]
        for members in orders:
            with self.subTest(classes=[shard.unit_key(test) for test in members]):
                calls.clear()
                with mock.patch.dict(sys.modules, modules):
                    result = unittest.TextTestRunner(stream=io.StringIO(), resultclass=shard.ClassTimingResult).run(
                        shard.ClassTimingSuite(members)
                    )
                self.assertTrue(result.wasSuccessful())
                self.assertEqual(len(calls), len(set(calls)), f"a module fixture ran twice: {calls}")

    def test_no_collected_module_failed_to_import(self) -> None:
        self.assertEqual(shard.import_failures(_TESTS), [])

    def test_the_planned_jobs_are_an_exact_cover_of_discover(self) -> None:
        # CI-WINDOWS-UNITTEST-SHARD-8-1: windows runs the 7-way partition plus the heavy job's
        # parts; together they must hold every discovered id exactly once. Ubuntu has no heavy
        # set, so its 3-way partition alone must still cover everything.
        expected = sorted(test.id() for test in _TESTS)
        for profile, shards in (("windows", 7), ("ubuntu", 3)):
            with self.subTest(profile=profile):
                partition, heavy = shard.plan_buckets(_COUNTS, profile, shards)
                self.assertEqual(len(partition), shards)
                covered = sorted(
                    test.id() for bucket in [*partition, *heavy] for test in shard.shard_tests(_UNITS, bucket)
                )
                self.assertEqual(covered, expected)
                if profile == "ubuntu":
                    self.assertEqual(heavy, [])

    def test_the_partition_excludes_exactly_the_heavy_set(self) -> None:
        classes, parts = shard.load_heavy_set("windows")
        partition, heavy = shard.plan_buckets(_COUNTS, "windows", 7)
        self.assertEqual(len(heavy), parts)
        self.assertEqual(sorted(key for bucket in heavy for key in bucket), sorted(classes))
        self.assertEqual({key for bucket in partition for key in bucket} & set(classes), set())


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
WINDOWS_HEAVY_JOB = "repo-hygiene-python-windows-unittest-heavy"
WINDOWS_AGGREGATE_JOB = "repo-hygiene-python-windows"
HEAVY_RUN = "python -m tools.repo_hygiene.ci_unittest_shard --profile windows --heavy-part ${{ matrix.part }}"
# ...and no windows unittest job may be expected above this share of that budget: at least 15 %
# headroom, so a PR that adds tests does not tip a shard over it. Master sat at 1439 of 1440 s
# (99.9 %) and merge_group run 40233119003 failed at 1445 s.
MAX_CAP_SHARE_USED = 0.85


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
        buckets, _heavy = shard.plan_buckets(_COUNTS, "windows", int(of.group(1)))
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

    def test_every_windows_unittest_shard_keeps_headroom_under_the_cap_share(self) -> None:
        job = _windows_shard_job()
        cap = re.search(r"(?m)^    timeout-minutes: (\d+)$", job)
        of = re.search(r"ci_unittest_shard --profile windows --of (\d+) --shard", job)
        self.assertIsNotNone(cap)
        self.assertIsNotNone(of)
        budget = int(cap.group(1)) * 60 * CAP_SHARE
        limit = budget * MAX_CAP_SHARE_USED
        weights = shard.load_weights("windows")
        buckets, _heavy = shard.plan_buckets(_COUNTS, "windows", int(of.group(1)))
        for index, bucket in enumerate(buckets, start=1):
            seconds = SETUP_SECONDS + sum(shard.unit_weight(key, _COUNTS[key], weights) for key in bucket)
            self.assertLessEqual(
                seconds,
                limit,
                f"windows shard {index}/{of.group(1)} is expected at {seconds:.1f} s, "
                f"{seconds / budget:.1%} of its cap share ({budget:.0f} s); the limit is "
                f"{MAX_CAP_SHARE_USED:.0%} ({limit:.0f} s) so new tests have headroom; move a measured "
                "class into ci_unittest_heavy_set.json or re-derive the weights from hosted logs",
            )

    def test_every_heavy_part_is_expected_within_its_cap_share_with_headroom(self) -> None:
        job = _workflow_job(WINDOWS_HEAVY_JOB)
        cap = re.search(r"(?m)^    timeout-minutes: (\d+)$", job)
        self.assertIsNotNone(cap)
        budget = int(cap.group(1)) * 60 * CAP_SHARE
        self.assertLessEqual(budget, MAX_BUDGET_SECONDS + 1e-6)
        limit = budget * MAX_CAP_SHARE_USED
        weights = shard.load_weights("windows")
        _partition, heavy = shard.plan_buckets(_COUNTS, "windows", 7)
        self.assertTrue(heavy)
        for index, bucket in enumerate(heavy, start=1):
            seconds = SETUP_SECONDS + sum(shard.unit_weight(key, _COUNTS[key], weights) for key in bucket)
            self.assertLessEqual(
                seconds,
                limit,
                f"windows heavy part {index}/{len(heavy)} is expected at {seconds:.1f} s, over "
                f"{MAX_CAP_SHARE_USED:.0%} of its {cap.group(1)} min cap share ({limit:.0f} s); "
                "raise the heavy set's parts",
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


# A discovered class may go without a weight row only with a reason written here. Empty today: a
# class with no row is silently weighted DEFAULT_TEST_SECONDS per test, which is how five
# test_venue_thermal_gate classes sat unweighted while the windows shard they landed on drifted
# to ~1259 s against its 1224 s limit (CI-SHARD-WEIGHTS-THERMAL-PARITY-CLASSES-1).
UNWEIGHTED_EXEMPT: dict[str, str] = {}


class EveryClassHasWeightRowTests(unittest.TestCase):
    def test_every_discovered_class_has_a_weight_row_in_every_profile(self) -> None:
        for profile in shard.PROFILES:
            with self.subTest(profile=profile):
                weights = shard.load_weights(profile)
                unweighted = sorted(key for key in _COUNTS if key not in weights and key not in UNWEIGHTED_EXEMPT)
                self.assertFalse(
                    unweighted,
                    f"{profile}: {len(unweighted)} discovered classes have no row in "
                    "ci_unittest_shard_weights.json, so each is weighted at DEFAULT_TEST_SECONDS per "
                    "test and the shard headroom guards pass only because the class is unweighted; "
                    "measure each (ci_unittest_shard_timings.class_seconds over a stamped "
                    "`unittest -v` run) and add its row: " + ", ".join(unweighted),
                )

    def test_every_exemption_is_a_discovered_unweighted_class_with_a_reason(self) -> None:
        for key, reason in UNWEIGHTED_EXEMPT.items():
            with self.subTest(key=key):
                self.assertIn(key, _COUNTS, "a stale exemption: discover no longer collects this class")
                self.assertTrue(reason.strip(), "an exemption names its reason")
                for profile in shard.PROFILES:
                    self.assertNotIn(key, shard.load_weights(profile), f"{profile} now weights it: drop the exemption")


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

    def test_the_heavy_job_cap_equals_the_windows_runner_cap(self) -> None:
        # run_shard warns against JOB_CAP_MINUTES["windows"] in the heavy job too.
        cap = re.search(r"(?m)^    timeout-minutes: (\d+)$", _workflow_job(WINDOWS_HEAVY_JOB))
        self.assertIsNotNone(cap)
        self.assertEqual(shard.JOB_CAP_MINUTES["windows"], int(cap.group(1)))

    def test_every_profile_has_a_cap(self) -> None:
        self.assertEqual(set(shard.JOB_CAP_MINUTES), set(shard.PROFILES))

    def test_the_warn_threshold_is_three_quarters_of_the_cap(self) -> None:
        self.assertEqual(shard.BUDGET_WARN_SHARE, 0.75)
        self.assertEqual(shard.TOP_CLASSES, 10)


class JobStartEpochStepPinTests(unittest.TestCase):
    """CI-SHARD-JOB-START-EPOCH-WORKFLOW-STEP-1: the runner reads ``CI_SHARD_JOB_START_EPOCH``, so
    each unittest shard job's FIRST step must export it, before checkout, or the budget clock
    silently starts at main() again and the 75 % warning fires late."""

    # job name -> the command that reads the clock on that runner's default (ubuntu) or declared (windows) shell
    CLOCKS = {
        UBUNTU_SHARD_JOB: "date +%s",
        WINDOWS_SHARD_JOB: "[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()",
        WINDOWS_HEAVY_JOB: "[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()",
    }

    def _first_step(self, job_name: str) -> str:
        job = _workflow_job(job_name)
        steps = re.search(r"(?m)^    steps:\n", job)
        self.assertIsNotNone(steps, job_name)
        body = job[steps.end() :]
        following = re.search(r"(?m)^      - ", body[len("      - ") :])
        return body[: following.start() + len("      - ") if following else len(body)]

    def test_each_unittest_shard_job_starts_by_exporting_the_job_start_epoch(self) -> None:
        for job_name, clock in self.CLOCKS.items():
            with self.subTest(job=job_name):
                first = self._first_step(job_name)
                self.assertTrue(first.startswith("      - name: "), first)
                self.assertNotIn("uses:", first, "the epoch step must run before checkout")
                self.assertIn(shard.JOB_START_ENV, first)
                self.assertIn("GITHUB_ENV", first)
                self.assertIn(clock, first)
                self.assertNotIn("continue-on-error", first)

    def test_the_epoch_step_precedes_checkout(self) -> None:
        for job_name in self.CLOCKS:
            with self.subTest(job=job_name):
                job = _workflow_job(job_name)
                self.assertIn(shard.JOB_START_ENV, job)
                self.assertLess(job.index(shard.JOB_START_ENV), job.index("actions/checkout@"))


class HeavySetTests(unittest.TestCase):
    """CI-WINDOWS-UNITTEST-SHARD-8-1: the heaviest windows classes run in their own job.

    The 7-way partition excludes the tracked set in ci_unittest_heavy_set.json and the heavy
    job runs it, split into the set's ``parts``. Both read the one ``HEAVY_SET_PATH``, so the
    exclusion and the job that runs what was excluded cannot name different sets.
    """

    def _strip_comments(self, text: str) -> str:
        return re.sub(r"(?m)^\s*#.*$", "", text)

    def test_every_heavy_class_is_discovered_and_measured(self) -> None:
        # An unmeasured class would be sized by test count, and a stale one would vanish from
        # both the partition and the heavy job; --verify-partition also refuses the latter.
        classes, parts = shard.load_heavy_set("windows")
        self.assertTrue(classes)
        self.assertGreaterEqual(parts, 1)
        weights = shard.load_weights("windows")
        for key in classes:
            with self.subTest(key=key):
                self.assertIn(key, _COUNTS)
                self.assertIn(key, weights)

    def test_the_ubuntu_profile_has_no_heavy_set(self) -> None:
        self.assertEqual(shard.load_heavy_set("ubuntu"), ([], 0))

    def test_a_malformed_heavy_set_is_refused(self) -> None:
        bad = (
            {"schema": 1, "profiles": {"windows": {"parts": 0, "classes": ["a.A"]}}},
            {"schema": 1, "profiles": {"windows": {"parts": 2, "classes": ["a.A", "a.A"]}}},
            {"schema": 1, "profiles": {"windows": {"parts": 2, "classes": []}}},
            {"schema": 2, "profiles": {}},
            {"schema": 1, "profiles": {"solaris": {"parts": 1, "classes": ["a.A"]}}},
            # Adversary A1 (r1, 980a921c): a wrong shape must not be coerced into a valid one.
            {"schema": 1, "profiles": {"windows": {"parts": 1, "classes": {"a.A": False}}}},
            {"schema": 1, "profiles": {"windows": {"parts": 1, "classes": "a.A"}}},
            {"schema": 1, "profiles": {"windows": {"parts": 1, "classes": [None]}}},
            {"schema": 1, "profiles": {"windows": {"parts": 1, "classes": [""]}}},
            {"schema": True, "profiles": {"windows": {"parts": 1, "classes": ["a.A"]}}},
            {"schema": 1, "profiles": ["windows"]},
            {"schema": 1, "profiles": {"windows": ["a.A"]}},
            {"schema": 1, "profiles": {"windows": {"parts": True, "classes": ["a.A"]}}},
            [],
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "heavy.json"
            for table in bad:
                with self.subTest(table=table):
                    path.write_text(json.dumps(table), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        shard.load_heavy_set("windows", path)

    def test_a_heavy_class_that_discover_does_not_collect_fails_the_partition_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "heavy.json"
            path.write_text(
                json.dumps({"schema": 1, "profiles": {"windows": {"parts": 1, "classes": ["tools.repo_hygiene.test_gone.Gone"]}}}),
                encoding="utf-8",
            )
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                code = shard.verify_partition("windows", 7, heavy_path=path)
        self.assertEqual(code, 1)
        self.assertIn("tools.repo_hygiene.test_gone.Gone", err.getvalue())

    def test_the_partition_proof_covers_the_heavy_parts(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = shard.verify_partition("windows", 7)
        self.assertEqual(code, 0, out.getvalue())
        _classes, parts = shard.load_heavy_set("windows")
        for part in range(1, parts + 1):
            self.assertIn(f"heavy part {part}/{parts}:", out.getvalue())
        self.assertIn(f"shards and heavy parts total {len(_TESTS)}", out.getvalue())

    def test_the_heavy_part_cli_lists_its_part_and_refuses_a_bad_part(self) -> None:
        classes, parts = shard.load_heavy_set("windows")
        listed: list[str] = []
        for part in range(1, parts + 1):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = shard.main(["--repo-root", str(REPO_ROOT), "--profile", "windows", "--heavy-part", str(part), "--list"])
            self.assertEqual(code, 0)
            listed.extend(out.getvalue().split())
        self.assertEqual(sorted(listed), sorted(classes))
        for argv in (
            ["--profile", "windows", "--heavy-part", str(parts + 1)],
            ["--profile", "windows", "--heavy-part", "0"],
            ["--profile", "ubuntu", "--heavy-part", "1"],
            ["--profile", "windows", "--heavy-part", "1", "--shard", "1", "--of", "7"],
            ["--profile", "windows", "--heavy-part", "1", "--verify-partition", "--of", "7"],
            ["--profile", "windows", "--shard", "1"],
        ):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                shard.main(["--repo-root", str(REPO_ROOT), *argv])

    def test_the_heavy_job_runs_each_part_of_the_tracked_set_once(self) -> None:
        _classes, parts = shard.load_heavy_set("windows")
        job = _workflow_job(WINDOWS_HEAVY_JOB)
        plain = self._strip_comments(job)
        matrix = re.search(r"(?m)^        part: \[([0-9, ]+)\]$", job)
        self.assertIsNotNone(matrix)
        self.assertEqual([int(n) for n in matrix.group(1).split(",")], list(range(1, parts + 1)))
        self.assertIn(f"    name: Repo Hygiene Python windows unittest heavy ${{{{ matrix.part }}}}/{parts}\n", job)
        self.assertIn("      fail-fast: false\n", job)
        # One step runs the tool, on the matrix's own part, unconditionally.
        self.assertEqual(plain.count("ci_unittest_shard"), 1)
        self.assertIn(f"        run: {HEAVY_RUN}\n", job)
        self.assertNotRegex(plain, r"(?m)^\s+if:")
        self.assertNotRegex(plain, r"--heavy-part\s+(?!\$\{\{ matrix\.part \}\})")
        self.assertIn("    runs-on: windows-latest\n", job)

    def test_the_shard_job_never_runs_a_heavy_part(self) -> None:
        self.assertNotIn("--heavy-part", _windows_shard_job())

    def test_the_heavy_job_has_the_shard_jobs_setup(self) -> None:
        def setup_steps(job: str, first_own_step: str) -> str:
            span = job[job.index("      - uses: actions/checkout@") : job.index(first_own_step)]
            # Comment lines directly above the job's own first step explain that step, not the setup
            # (the heavy step's "/6" label is stale on purpose, see the comment above it in tests.yml).
            return re.sub(r"(?:      #[^\n]*\n)+\Z", "", span)

        self.assertEqual(
            setup_steps(_workflow_job(WINDOWS_HEAVY_JOB), "      - name: Run repo hygiene heavy unittest part"),
            setup_steps(_windows_shard_job(), "      - name: Prove the unittest shards cover every discovered test"),
        )

    def test_the_required_windows_check_needs_the_heavy_job(self) -> None:
        aggregate = _workflow_job(WINDOWS_AGGREGATE_JOB)
        needs = re.search(r"(?m)^    needs: \[([a-z0-9, -]+)\]$", aggregate)
        self.assertIsNotNone(needs)
        self.assertIn(WINDOWS_HEAVY_JOB, [name.strip() for name in needs.group(1).split(",")])
        self.assertIn("    if: ${{ always() }}\n", aggregate)
        step = aggregate[aggregate.index("      - name: Require the heavy unittest job to have passed") :]
        step = step.split("\n      - ")[0]
        self.assertIn(f"UNITTEST_HEAVY_RESULT: ${{{{ needs.{WINDOWS_HEAVY_JOB}.result }}}}", step)
        self.assertIn('test "$UNITTEST_HEAVY_RESULT" = "success"', step)


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

    def test_a_worker_env_that_is_not_a_count_above_one_keeps_the_serial_path(self) -> None:
        buckets, heavy = shard.plan_buckets(_COUNTS, "windows", 7)
        planned = (_TESTS, _UNITS, buckets, heavy)
        members = shard.shard_tests(_UNITS, buckets[0])
        argv = ["--repo-root", str(REPO_ROOT), "--profile", "windows", "--of", "7", "--shard", "1"]
        for raw, parallel in ((None, False), ("", False), ("1", False), ("0", False), ("-1", False), ("abc", False), ("3", True)):
            env = {} if raw is None else {shard.WORKERS_ENV: raw}
            with self.subTest(raw=raw), mock.patch.dict(os.environ, env):
                os.environ.pop(shard.MANIFEST_ENV, None)
                if raw is None:
                    os.environ.pop(shard.WORKERS_ENV, None)
                with (
                    mock.patch.object(shard, "plan", return_value=planned),
                    mock.patch.object(shard, "run_shard", return_value=0) as serial,
                    mock.patch.object(shard, "run_parallel", return_value=0) as pool,
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(shard.main(argv), 0)
                self.assertEqual(serial.call_count, 0 if parallel else 1)
                self.assertEqual(pool.call_count, 1 if parallel else 0)
                if not parallel:
                    self.assertEqual(serial.call_args.args[0], members)
                    self.assertIsNone(serial.call_args.kwargs.get("manifest"))

    def _fixture(
        self, extras: dict[str, str] | None = None, sources: dict[str, str] | None = None
    ) -> tuple[Path, list[unittest.TestCase], str]:
        """Three tiny test modules in a temp dir, collected here and re-collectable by a worker.

        ``sources`` (module letter to full source) replaces the three modules with exactly those.
        """
        tag = uuid.uuid4().hex[:8]
        root = Path(tempfile.mkdtemp(prefix="fxw-"))
        self.addCleanup(shutil.rmtree, root, True)
        for name, source in (sources or {}).items():
            (root / f"fxw{tag}_{name}.py").write_text(source, encoding="utf-8")
        for name in () if sources else ("a", "b", "c"):
            lines = [
                "import json, os, tempfile, unittest",
                "from pathlib import Path",
                f"SEEN = Path(__file__).with_name('seen-{name}.json')",
                f"class Fx{name.upper()}Tests(unittest.TestCase):",
                "    def test_one(self):",
                "        SEEN.write_text(json.dumps({'pid': os.getpid(), 'temp': tempfile.gettempdir()}), encoding='utf-8')",
                "    def test_two(self):",
                "        pass",
                (extras or {}).get(name, ""),
            ]
            (root / f"fxw{tag}_{name}.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
        tests = shard.collect_tests(str(root), f"fxw{tag}_*.py", str(root))
        self.assertEqual(len(tests), len(sources) if sources else 6 + len(extras or {}))

        def forget() -> None:
            for module in [m for m in sys.modules if m.startswith(f"fxw{tag}_")]:
                del sys.modules[module]
            while str(root) in sys.path:
                sys.path.remove(str(root))

        self.addCleanup(forget)
        return root, tests, tag

    def _parallel(
        self, root: Path, tests: list[unittest.TestCase], tag: str, workers: int, *, pins: dict[str, str] | None = None,
        timeout: float = 120.0, environ: dict[str, str] | None = None, poll_seconds: float | None = None,
    ) -> tuple[int, str, str, Path]:
        out, err = io.StringIO(), io.StringIO()
        manifest = root / "manifest-parallel.txt"
        extra = {} if poll_seconds is None else {"poll_seconds": poll_seconds}
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = shard.run_parallel(
                tests, "ubuntu", 1, 1, workers, start_dir=str(root), pattern=f"fxw{tag}_*.py", top_level_dir=str(root),
                repo_root=REPO_ROOT, pins=pins or {}, environ=environ or {}, manifest=manifest, timeout=timeout, **extra,
            )
        return code, out.getvalue(), err.getvalue(), manifest

    @staticmethod
    def _seen(root: Path, name: str) -> dict:
        return json.loads((root / f"seen-{name}.json").read_text(encoding="utf-8"))

    def test_parallel_workers_run_the_serial_id_set_exactly_once_each_with_their_own_temp(self) -> None:
        root, tests, tag = self._fixture()
        serial_manifest = root / "manifest-serial.txt"
        with contextlib.redirect_stdout(io.StringIO()):
            code = shard.run_shard(
                tests, "ubuntu", 1, 1, clock=time.monotonic, environ={}, stream=io.StringIO(), manifest=serial_manifest
            )
        self.assertEqual(code, 0)
        expected = sorted(test.id() for test in tests)
        self.assertEqual(serial_manifest.read_text(encoding="utf-8").splitlines(), expected)
        for name in ("a", "b", "c"):
            (root / f"seen-{name}.json").unlink()
        code, out, err, manifest = self._parallel(root, tests, tag, 3)
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(manifest.read_text(encoding="utf-8").splitlines(), expected)
        seen = [self._seen(root, name) for name in ("a", "b", "c")]
        self.assertEqual(len({one["pid"] for one in seen}), 3, "each module ran in its own worker process")
        self.assertEqual(len({one["temp"] for one in seen}), 3, "each worker has its own TEMP")
        self.assertNotIn(tempfile.gettempdir(), {one["temp"] for one in seen})
        self.assertIn("SHARD_SLOWEST profile=ubuntu shard=1/1", out)

    def test_pinned_modules_share_one_serial_worker(self) -> None:
        root, tests, tag = self._fixture()
        code, out, err, _manifest = self._parallel(
            root, tests, tag, 3, pins={f"fxw{tag}_a": "fixed path", f"fxw{tag}_b": "pwsh"}
        )
        self.assertEqual((code, err), (0, ""), out)
        one, two, three = (self._seen(root, name) for name in ("a", "b", "c"))
        self.assertEqual(one["pid"], two["pid"])
        self.assertNotEqual(one["pid"], three["pid"])
        self.assertEqual(one["temp"], two["temp"])

    def test_a_failing_test_in_a_worker_fails_the_shard_and_is_printed(self) -> None:
        root, tests, tag = self._fixture({"b": "    def test_fails(self):\n        self.fail('fx-boom')"})
        code, out, err, _manifest = self._parallel(root, tests, tag, 3)
        self.assertEqual(code, 1)
        self.assertIn("fx-boom", out)
        self.assertIn("FAIL", out)

    def test_a_worker_crash_is_a_failure_not_a_silent_pass(self) -> None:
        root, tests, tag = self._fixture({"b": "    def test_crash(self):\n        os._exit(3)"})
        code, out, err, manifest = self._parallel(root, tests, tag, 3)
        self.assertEqual(code, 1)
        self.assertIn("ERROR: worker", err)

    def test_a_worker_that_outlives_the_timeout_is_killed_and_fails_the_shard(self) -> None:
        root, tests, tag = self._fixture({"b": "    def test_hang(self):\n        import time\n        time.sleep(60)"})
        started = time.monotonic()
        code, out, err, _manifest = self._parallel(root, tests, tag, 3, timeout=3.0)
        self.assertEqual(code, 1)
        self.assertIn("timed out", err)
        self.assertLess(time.monotonic() - started, 30.0)

    def test_a_class_a_worker_cannot_find_fails_the_shard(self) -> None:
        root, tests, tag = self._fixture()
        (root / f"fxw{tag}_c.py").unlink()
        code, out, err, _manifest = self._parallel(root, tests, tag, 3)
        self.assertEqual(code, 1)
        self.assertIn("ERROR", err)

    # r3 adversary A1: the parent's worker spawn is a console spawn like any other tool spawn.
    def test_the_worker_spawn_carries_the_hidden_window_flags(self) -> None:
        root, tests, tag = self._fixture()
        real = subprocess.Popen
        seen: list[dict] = []

        def spy(*args: object, **kwargs: object) -> subprocess.Popen:
            seen.append(dict(kwargs))
            return real(*args, **kwargs)

        with mock.patch.object(shard.subprocess, "Popen", spy):
            code, out, err, _manifest = self._parallel(root, tests, tag, 2)
        self.assertEqual((code, err), (0, ""), out)
        self.assertTrue(seen)
        for kwargs in seen:
            self.assertIn("creationflags", kwargs)
            self.assertEqual(kwargs["creationflags"], no_window_flags())

    # r3 adversary A2 (pass 1) and A1 (pass 2): a worker is in time only if its exit was seen BEFORE the
    # deadline; the deadline starts before the first launch, so a slow launch or a late look counts.
    @staticmethod
    def _late(polls: list[list[int | None]], times: list[float], deadline: float = 1.0) -> set[int]:
        class FakeProc:
            def __init__(self, seq: list[int | None]) -> None:
                self._seq = list(seq)

            def poll(self) -> int | None:
                return self._seq.pop(0) if len(self._seq) > 1 else self._seq[0]

            def wait(self, timeout: float | None = None) -> int:
                raise subprocess.TimeoutExpired("fake", timeout)

        ticks = list(times)  # the last reading repeats once the list is used up
        return shard.late_workers(
            [FakeProc(seq) for seq in polls], deadline, lambda: ticks.pop(0) if len(ticks) > 1 else ticks[0]
        )

    def test_a_worker_seen_to_exit_before_the_deadline_is_in_time(self) -> None:
        self.assertEqual(self._late([[0], [0]], [0.5]), set())

    def test_a_worker_whose_exit_is_first_seen_after_the_deadline_is_late(self) -> None:
        # launching took longer than the whole budget: both children are already gone when the parent looks
        self.assertEqual(self._late([[0], [0]], [3.0]), {1, 2})

    def test_a_wait_that_ran_out_is_a_timeout_even_if_the_worker_exits_a_moment_later(self) -> None:
        self.assertEqual(self._late([[None, 0]], [0.5, 1.5]), {1})

    def test_a_worker_still_running_at_the_deadline_is_late_and_the_others_are_not(self) -> None:
        self.assertEqual(self._late([[0], [None]], [0.5, 2.0]), {2})

    # r4 adversary A1 (pass 3): the clock is read AFTER a look finds a worker gone, and the wait slice
    # comes from a fresh reading, so a poll() that itself crosses the deadline is not mistaken for in time.
    @staticmethod
    def _slow_proc(now: list[float], reading: float, code: int | None, waits: list[float | None] | None = None):
        class SlowProc:
            def poll(self) -> int | None:
                now[0] = max(now[0], reading)  # the look itself takes the clock to `reading`
                return code

            def wait(self, timeout: float | None = None) -> int:
                if waits is not None:
                    waits.append(timeout)
                    if len(waits) > 3:  # a runaway loop must fail the test, not eat the host
                        raise AssertionError(f"late_workers keeps waiting: {waits}")
                now[0] = max(now[0], 1.2)  # a wait that ran its slice leaves the clock past the deadline
                raise subprocess.TimeoutExpired("fake", timeout)

        return SlowProc()

    def test_an_exit_first_seen_while_the_poll_itself_crossed_the_deadline_is_late(self) -> None:
        now = [0.9]  # deadline 1.0; each poll() returns 0 at 1.1
        procs = [self._slow_proc(now, 1.1, 0), self._slow_proc(now, 1.1, 0)]
        self.assertEqual(shard.late_workers(procs, 1.0, lambda: now[0]), {1, 2})

    def test_the_wait_slice_is_cut_to_the_time_left_on_a_fresh_reading(self) -> None:
        now, waits = [0.0], []
        proc = self._slow_proc(now, 0.95, None, waits)  # the look takes 0.95 s of a 1.0 s deadline
        self.assertEqual(shard.late_workers([proc], 1.0, lambda: now[0]), {1})
        self.assertEqual(len(waits), 1, waits)
        self.assertAlmostEqual(waits[0], 0.05)

    def test_no_wait_starts_once_a_poll_has_taken_the_clock_past_the_deadline(self) -> None:
        now, waits = [0.0], []
        proc = self._slow_proc(now, 1.5, None, waits)
        self.assertEqual(shard.late_workers([proc], 1.0, lambda: now[0]), {1})
        self.assertEqual(waits, [])

    # r4 adversary A2 (pass 3): the deadline covers launching, so no worker is started once it has passed.
    def test_no_worker_is_launched_after_the_deadline_passed_while_launching(self) -> None:
        root, tests, tag = self._fixture()
        now = [0.0]
        real_write = Path.write_text

        def slow_write(path: Path, data: str, *args: object, **kwargs: object) -> int:
            written = real_write(path, data, *args, **kwargs)
            if path.name == "spec.json":
                now[0] = 2.0  # the first spec write completes after the 1 s deadline
            return written

        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(shard.Path, "write_text", slow_write),
            mock.patch.object(shard.subprocess, "Popen") as popen,
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = shard.run_parallel(
                tests, "ubuntu", 1, 1, 2, start_dir=str(root), pattern=f"fxw{tag}_*.py", top_level_dir=str(root),
                repo_root=REPO_ROOT, pins={}, environ={shard.WORKER_TIMEOUT_ENV: "1"}, clock=lambda: now[0],
            )
        self.assertEqual(popen.call_count, 0)
        self.assertEqual(code, 1)
        for number in (1, 2):
            self.assertIn(f"worker {number}/2 not launched: the deadline passed while launching", err.getvalue())

    # r4 adversary A1 (pass 4): the deadline check is the LAST step before Popen; the status line and the
    # log open are blocking too, so a clock that crosses the deadline there must stop that launch.
    def _launch_times(self, slow: str) -> list[float]:
        root, tests, tag = self._fixture()
        now = [0.0]
        launched: list[float] = []
        real_open = Path.open

        class SlowOut(io.StringIO):
            def write(self, text: str) -> int:
                if slow == "status" and text.startswith("worker "):
                    now[0] = 2.0  # printing the status line takes the clock past the 1 s deadline
                return super().write(text)

        def slow_open(path: Path, *args: object, **kwargs: object):
            handle = real_open(path, *args, **kwargs)
            if slow == "log" and path.name == "log.txt":
                now[0] = 2.0  # creating the log file takes the clock past the deadline
            return handle

        def fake_popen(*args: object, **kwargs: object) -> mock.MagicMock:
            launched.append(now[0])
            return mock.MagicMock()

        with (
            mock.patch.object(shard.Path, "open", slow_open),
            mock.patch.object(shard.subprocess, "Popen", side_effect=fake_popen),
            contextlib.redirect_stdout(SlowOut()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            shard.run_parallel(
                tests, "ubuntu", 1, 1, 2, start_dir=str(root), pattern=f"fxw{tag}_*.py", top_level_dir=str(root),
                repo_root=REPO_ROOT, pins={}, environ={shard.WORKER_TIMEOUT_ENV: "1"}, clock=lambda: now[0],
            )
        return launched

    def test_no_worker_starts_after_the_status_line_took_the_clock_past_the_deadline(self) -> None:
        self.assertEqual([time for time in self._launch_times("status") if time >= 1.0], [])

    def test_no_worker_starts_after_opening_its_log_took_the_clock_past_the_deadline(self) -> None:
        self.assertEqual([time for time in self._launch_times("log") if time >= 1.0], [])

    # r5 (adversary pass 6 A1): the base file ran by path (`python tools/repo_hygiene/ci_unittest_shard.py`)
    # because it imported nothing from the repo; the sibling import must not take that entry point away.
    def test_the_file_runs_by_path_and_prints_usage(self) -> None:
        done = subprocess.run(
            [sys.executable, "-B", str(Path("tools") / "repo_hygiene" / "ci_unittest_shard.py"), "--help"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=120, creationflags=no_window_flags(),
        )
        self.assertEqual((done.returncode, done.stderr), (0, ""), done.stdout)
        self.assertIn("usage:", done.stdout)
        self.assertIn("--profile", done.stdout)

    # r5 (adversary pass 5 A2): the parent can sit in a blocking launch step (the next worker's spec write)
    # while a worker it already started passes the deadline; that worker must be killed AT the deadline
    # (on the injected clock), by a watchdog, not whenever the parent next looks.
    def test_a_launched_worker_is_killed_at_the_deadline_while_the_parent_is_stalled_launching(self) -> None:
        root, tests, tag = self._fixture()
        now = [0.0]
        killed_at: list[float] = []
        killed = threading.Event()
        stalled: list[bool] = []
        real_write = Path.write_text

        class FakeProc:
            returncode: int | None = None

            def poll(self) -> int | None:
                return self.returncode

            def wait(self, timeout: float | None = None) -> int:
                if self.returncode is None:
                    raise subprocess.TimeoutExpired("fake", timeout)
                return self.returncode

            def kill(self) -> None:
                if self.returncode is None:
                    killed_at.append(now[0])
                    self.returncode = -9
                    killed.set()

        def stalling_write(path: Path, data: str, *args: object, **kwargs: object) -> int:
            written = real_write(path, data, *args, **kwargs)
            if path.name == "spec.json" and path.parent.name == "w2":
                now[0] = 2.0  # worker 2's spec write stalls the parent past the 1 s deadline...
                # ...and, while it is stalled, the watchdog must already have killed worker 1
                stalled.append(killed.wait(timeout=5.0))
            return written

        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(shard.Path, "write_text", stalling_write),
            mock.patch.object(shard.subprocess, "Popen", side_effect=lambda *a, **k: FakeProc()),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = shard.run_parallel(
                tests, "ubuntu", 1, 1, 2, start_dir=str(root), pattern=f"fxw{tag}_*.py", top_level_dir=str(root),
                repo_root=REPO_ROOT, pins={}, environ={shard.WORKER_TIMEOUT_ENV: "1"}, clock=lambda: now[0],
            )
        self.assertEqual(stalled, [True], "worker 1 was not killed while the parent was stalled launching worker 2")
        self.assertEqual(killed_at, [2.0], "the kill happened once, on the injected clock, at or after the deadline")
        self.assertEqual(code, 1)
        self.assertIn("worker 1/1 timed out", err.getvalue())

    # r6 (adversary pass 7 B1): a kill that raises OSError for one worker must not stop the kill of the
    # others, in any of the three places the parent ends a worker, and the failure must be reported.
    class _RunningProc:
        def __init__(self, kill_error: OSError | None = None) -> None:
            self.kill_error = kill_error
            self.kill_calls = 0
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            if self.returncode is None:
                raise subprocess.TimeoutExpired("fake", timeout)
            return self.returncode

        def kill(self) -> None:
            self.kill_calls += 1
            if self.kill_error is not None:
                raise self.kill_error
            self.returncode = -9

    def _run_with_fake_workers(self, procs: list, *, launch_error_after: int | None = None, past_deadline: bool = False,
                               killer: object | None = None) -> tuple[int | None, str, BaseException | None]:
        root, tests, tag = self._fixture()
        now = [0.0]
        calls: list[int] = []

        def popen(*args: object, **kwargs: object) -> object:
            calls.append(1)
            if launch_error_after is not None and len(calls) > launch_error_after:
                raise OSError("launch failed")
            if past_deadline and len(calls) == len(procs):
                now[0] = 2.0  # the last launch ends past the 1 s deadline
            return procs[len(calls) - 1]

        err = io.StringIO()
        code: int | None = None
        raised: BaseException | None = None
        patches = [mock.patch.object(shard.subprocess, "Popen", side_effect=popen)]
        if killer is not None:
            patches.append(mock.patch.object(shard, "DeadlineKiller", killer))
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(err))
            try:
                code = shard.run_parallel(
                    tests, "ubuntu", 1, 1, 3, start_dir=str(root), pattern=f"fxw{tag}_*.py", top_level_dir=str(root),
                    repo_root=REPO_ROOT, pins={}, clock=lambda: now[0],
                    environ={shard.WORKER_TIMEOUT_ENV: "1" if past_deadline else "600"},
                )
            except OSError as error:
                raised = error
        for kept in re.findall(r"kept at (.+)$", err.getvalue(), re.MULTILINE):  # a worker that could not be ended keeps its dir
            self.addCleanup(shutil.rmtree, kept, True)
        return code, err.getvalue(), raised

    def test_a_failed_kill_does_not_leave_later_workers_running_after_a_launch_error(self) -> None:
        procs = [self._RunningProc(PermissionError("denied")), self._RunningProc()]
        code, err, raised = self._run_with_fake_workers(procs, launch_error_after=2)
        self.assertIsNotNone(raised, "the launch error still propagates")
        self.assertEqual(procs[1].returncode, -9, "worker 2 was never killed after worker 1's kill raised")
        self.assertIn("worker 1/2 could not be terminated", err)

    def test_a_failed_kill_at_the_timeout_does_not_leave_later_workers_running(self) -> None:
        class NoKiller:  # only the parent's own timeout loop is under test here
            killed: set[int] = set()
            failed: dict[int, str] = {}

            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def start(self) -> None:
                pass

            def add(self, proc: object) -> None:
                pass

            def stop(self) -> None:
                pass

        procs = [self._RunningProc(PermissionError("denied")), self._RunningProc()]
        code, err, raised = self._run_with_fake_workers(procs, past_deadline=True, killer=NoKiller)
        self.assertIsNone(raised, "a failed kill is reported as a failure, not raised")
        self.assertEqual(procs[1].returncode, -9, "worker 2 was never killed after worker 1's kill raised")
        self.assertEqual(code, 1)
        self.assertEqual(err.count("worker 1/2 could not be terminated"), 1, err)

    # r10 adversary pass 16 A1: a worker the parent could not end may still write its log, so the work dir is kept
    # (and its path reported) rather than deleted from under it.
    def test_a_worker_that_could_not_be_ended_keeps_its_log_dir_and_reports_it(self) -> None:
        procs = [self._RunningProc(PermissionError("denied")), self._RunningProc()]
        code, err, raised = self._run_with_fake_workers(procs, launch_error_after=2)
        kept = re.search(r"kept at (.+)$", err, re.MULTILINE)
        if kept:
            self.addCleanup(shutil.rmtree, kept[1], True)
        self.assertIsNotNone(raised)
        self.assertIsNotNone(kept, "a log dir under a worker that was still running was deleted without a word: " + err)
        self.assertTrue(Path(kept[1], "w1").is_dir(), "the kept path does not hold the worker's log dir")

    def test_a_failed_kill_in_the_watchdog_does_not_skip_the_later_workers(self) -> None:
        killer = shard.DeadlineKiller(1.0, lambda: 2.0, slice_seconds=3600)
        first, second = self._RunningProc(PermissionError("denied")), self._RunningProc()
        killer.add(first)  # must not raise
        killer.add(second)
        self.assertEqual(second.returncode, -9, "worker 2 was never killed after worker 1's kill raised")
        self.assertEqual(killer.killed, {2})
        self.assertEqual(sorted(killer.failed), [1])
        self.assertGreaterEqual(first.kill_calls, 2, "the watchdog retries a worker it could not kill")

    # r6 adversary pass 8 A1: killing only the worker leaves what its tests launched (a bounded test's
    # child and grandchild) running; ending a worker must end its whole process tree.
    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if os.name == "nt":
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                return bool(ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        # a zombie (killed, not yet reaped by init) is not running
        status = Path(f"/proc/{pid}/stat")
        return not (status.exists() and status.read_text().rsplit(")", 1)[1].split()[0] == "Z")

    def test_ending_a_worker_ends_the_process_tree_it_started(self) -> None:
        tree = (  # short sleeps: a red run leaves nothing behind for long, and no cleanup kills a bare PID
            "import subprocess, sys, time\n"
            "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(40)'])\n"
            "print(c.pid, flush=True)\n"
            "time.sleep(40)\n"
        )
        worker = subprocess.Popen(
            [sys.executable, "-c", tree], stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
            creationflags=no_window_flags(), **shard.session_kwargs(),
        )
        self.addCleanup(worker.stdout.close)
        grandchild = int(worker.stdout.readline())

        self.addCleanup(worker.kill)
        self.assertIsNone(shard.terminate(worker))
        deadline = time.monotonic() + 15.0
        while self._pid_alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(self._pid_alive(grandchild), "the worker's own child survived the worker's termination")

    # r6 adversary pass 9 A2/A3: a tree kill that fails (or raises anything at all) is reported, and the
    # worker itself and the other workers are still killed; the watchdog thread must survive it.
    def _tree_kill_fails(self, error: BaseException) -> contextlib.ExitStack:
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(shard.subprocess, "run", side_effect=error))
        stack.enter_context(mock.patch.object(shard.os, "killpg", side_effect=error, create=True))
        self.addCleanup(stack.close)
        return stack

    def _pid_proc(self) -> "ShardExitCodeTests._RunningProc":
        proc = self._RunningProc()
        proc.pid = 424242  # only ever seen by the patched tree kill in these tests
        return proc

    def test_a_tree_kill_that_fails_is_reported_and_the_worker_is_still_killed(self) -> None:
        for error in (PermissionError(5, "Access is denied"), RuntimeError("policy denies taskkill")):
            with self.subTest(error=type(error).__name__), self._tree_kill_fails(error):
                proc = self._pid_proc()
                result = shard.terminate(proc)
                self.assertIsNotNone(result, "a tree that was not ended must not read as a clean kill")
                self.assertIn("process tree", result)
                self.assertEqual(proc.returncode, -9, "the worker itself is still killed")

    def test_a_tree_kill_that_raises_does_not_kill_the_watchdog_or_skip_later_workers(self) -> None:
        with self._tree_kill_fails(RuntimeError("policy denies taskkill")):
            killer = shard.DeadlineKiller(1.0, lambda: 2.0, slice_seconds=3600)
            first, second = self._pid_proc(), self._pid_proc()
            killer.add(first)  # must not raise
            killer.add(second)
        self.assertEqual((first.returncode, second.returncode), (-9, -9))
        self.assertEqual(sorted(killer.failed), [1, 2], "the failures stay on record after the workers died")

    def test_a_run_reports_a_tree_kill_failure_the_watchdog_met(self) -> None:
        with self._tree_kill_fails(PermissionError(5, "Access is denied")):
            procs = [self._pid_proc(), self._pid_proc()]
            code, err, raised = self._run_with_fake_workers(procs, past_deadline=True)
        self.assertIsNone(raised)
        self.assertEqual(code, 1)
        self.assertIn("could not be terminated", err)
        self.assertIn("process tree", err)

    # r7 adversary pass 10 A1: an exception whose own __str__ raises must not escape any kill path.
    class _BadDetail(RuntimeError):
        def __str__(self) -> str:
            raise self

    def test_an_exception_that_cannot_be_rendered_is_reported_by_type_and_the_worker_is_killed(self) -> None:
        with self._tree_kill_fails(self._BadDetail("x")):
            proc = self._pid_proc()
            result = shard.terminate(proc)
        self.assertIsNotNone(result)
        self.assertIn("_BadDetail", result)
        self.assertEqual(proc.returncode, -9, "the worker itself is still killed")
        direct = self._RunningProc(self._BadDetail("x"))  # the worker's own kill raising it
        self.assertIn("_BadDetail", shard.terminate(direct) or "")

    def test_the_watchdog_survives_an_unrenderable_kill_error_and_kills_the_others(self) -> None:
        bad, good = self._RunningProc(self._BadDetail("x")), self._RunningProc()
        killer = shard.DeadlineKiller(1.0, lambda: 2.0, slice_seconds=3600)
        killer.add(bad)  # must not raise
        killer.add(good)
        self.assertEqual(good.returncode, -9)
        self.assertEqual(sorted(killer.failed), [1])
        self.assertIn("_BadDetail", killer.failed[1])

    def test_the_watchdog_survives_a_terminate_that_raises_an_unrenderable_error(self) -> None:
        first, second = self._RunningProc(), self._RunningProc()

        def flaky(proc: object, *, reap: bool = True) -> str | None:
            if proc is first:
                raise self._BadDetail("x")
            proc.kill()
            return None

        killer = shard.DeadlineKiller(1.0, lambda: 2.0, slice_seconds=3600)
        with mock.patch.object(shard, "terminate", flaky):
            killer.add(first)  # must not raise
            killer.add(second)
        self.assertEqual(second.returncode, -9)
        self.assertIn("_BadDetail", killer.failed[1])

    # r7 adversary pass 10 A2: the watchdog never holds its lock across a blocking tree kill, and the
    # workers' trees are ended at the same time, not one after the other.
    def test_the_watchdog_ends_the_trees_of_two_workers_at_the_same_time(self) -> None:
        both_in = threading.Barrier(2)
        reached: list[bool] = []

        def tree(proc: object) -> str | None:
            try:
                both_in.wait(timeout=3.0)  # a serial kill never gets the second one here
                reached.append(True)
            except threading.BrokenBarrierError:
                reached.append(False)
            return None

        killer = shard.DeadlineKiller(1.0, lambda: 2.0, slice_seconds=3600)
        killer._procs.extend([self._RunningProc(), self._RunningProc()])
        with mock.patch.object(shard, "_kill_tree", tree):
            killer._kill_due()
        self.assertEqual(reached, [True, True], "the second worker's tree was only ended after the first one's returned")

    def test_a_worker_registered_while_another_tree_kill_blocks_is_not_held_up(self) -> None:
        release = threading.Event()
        first, second = self._RunningProc(), self._RunningProc()

        def tree(proc: object) -> str | None:
            if proc is first:
                release.wait(timeout=10.0)  # a taskkill that takes its whole bound
            return None

        killer = shard.DeadlineKiller(1.0, lambda: 2.0, slice_seconds=3600)
        self.addCleanup(release.set)
        with mock.patch.object(shard, "_kill_tree", tree):
            blocked = threading.Thread(target=killer.add, args=(first,), daemon=True)
            blocked.start()
            _wait_until(lambda: bool(killer._inflight), 5.0)  # bounded, never an unbounded poll (r8 pass 14 A2)
            registered =threading.Thread(target=killer.add, args=(second,), daemon=True)
            registered.start()
            registered.join(timeout=3.0)
            held_up = registered.is_alive()
            release.set()
            blocked.join(timeout=10.0)
            registered.join(timeout=10.0)
        self.assertFalse(held_up, "add() blocked behind another worker's tree kill")
        self.assertEqual(second.returncode, -9)

    def test_the_parents_own_kill_loops_end_the_trees_at_the_same_time(self) -> None:
        both_in = threading.Barrier(2)
        reached: list[bool] = []

        def tree(proc: object) -> str | None:
            try:
                both_in.wait(timeout=3.0)
                reached.append(True)
            except threading.BrokenBarrierError:
                reached.append(False)
            return None

        procs = [self._RunningProc(), self._RunningProc()]
        with mock.patch.object(shard, "_kill_tree", tree):
            results = shard.terminate_all(procs)
        self.assertEqual(results, [None, None])
        self.assertEqual(reached, [True, True])
        self.assertEqual([proc.returncode for proc in procs], [-9, -9])

    # r7 adversary pass 10 A3: taskkill exit 128 (the root is gone) means its descendants were not walked.
    def test_a_taskkill_that_finds_no_root_is_reported_as_the_tree_not_confirmed(self) -> None:
        done = subprocess.CompletedProcess(["taskkill"], 128, b"", b"")
        proc = self._pid_proc()
        with (
            mock.patch.object(shard.os, "name", "nt"),
            mock.patch.object(shard.subprocess, "run", return_value=done),
        ):
            result = shard.terminate(proc)
        self.assertIsNotNone(result, "128 read as a clean kill although the root's descendants may survive")
        self.assertIn("not confirmed", result)

    # r7 adversary pass 11 A1: SystemExit / KeyboardInterrupt raised inside a kill (an audit hook can raise
    # either) must neither stop the worker being killed nor read as a successful kill.
    def test_a_system_exit_from_a_kill_is_reported_and_the_worker_is_still_killed(self) -> None:
        class ExitInStr(RuntimeError):
            def __str__(self) -> str:
                raise SystemExit(77)

        for error in (SystemExit(77), KeyboardInterrupt(), ExitInStr("x")):
            with self.subTest(error=type(error).__name__), self._tree_kill_fails(error):
                proc = self._pid_proc()
                result = shard.terminate(proc)
                self.assertIsNotNone(result, "a kill that raised read as a clean kill")
                self.assertEqual(proc.returncode, -9, "the worker itself is still killed")

    def test_the_watchdog_does_not_record_a_kill_that_exited_the_thread_as_a_success(self) -> None:
        killer = shard.DeadlineKiller(1.0, lambda: 2.0, slice_seconds=3600)
        with self._tree_kill_fails(SystemExit(77)):
            proc = self._pid_proc()
            killer.add(proc)
        self.assertNotIn(1, killer.killed)
        self.assertIn(1, killer.failed)

    def test_a_termination_thread_that_dies_reads_as_a_failure(self) -> None:
        def dies(proc: object, *, reap: bool = True) -> str | None:
            raise SystemExit(77)

        with mock.patch.object(shard, "terminate", dies):
            results = shard.terminate_all([self._RunningProc()])
        self.assertEqual(len(results), 1)
        self.assertIsNotNone(results[0])

    # r7 adversary pass 11 A2: nothing in a kill waits without a bound.
    def test_a_worker_that_never_reaps_after_its_kill_is_reported_not_waited_on_forever(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)

        class Unreapable(self._RunningProc):
            def wait(self, timeout: float | None = None) -> int:
                if timeout is None:
                    release.wait(timeout=5.0)  # pending I/O: the process object is never signalled
                raise subprocess.TimeoutExpired("fake", timeout)

        proc = Unreapable()
        with mock.patch.object(shard, "WORKER_REAP_SECONDS", 0.05):
            started = time.monotonic()
            result = shard.terminate(proc)
        self.assertLess(time.monotonic() - started, 3.0, "terminate waited on the worker without a bound")
        self.assertIsNotNone(result)
        self.assertIn("did not exit", result)

    def test_terminate_all_gives_up_on_a_termination_that_never_returns(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)
        first, second = self._RunningProc(), self._RunningProc()

        def hangs(proc: object, *, reap: bool = True) -> str | None:
            if proc is first:
                release.wait(timeout=8.0)
            return None

        with mock.patch.object(shard, "terminate", hangs), mock.patch.object(shard, "TERMINATE_BOUND_SECONDS", 0.3):
            started = time.monotonic()
            results = shard.terminate_all([first, second])
        self.assertLess(time.monotonic() - started, 4.0, "terminate_all joined a thread without a bound")
        self.assertIsNotNone(results[0], "a termination that did not finish read as a clean kill")
        self.assertIn("did not complete", results[0])
        self.assertIsNone(results[1])

    # r7 adversary pass 11 A3: a worker that finished keeps its output when a later launch raises.
    def test_a_launch_error_still_prints_the_output_of_workers_that_finished(self) -> None:
        root, tests, tag = self._fixture()
        calls: list[int] = []

        class Finished:
            returncode = 1

            def poll(self) -> int:
                return 1

            def wait(self, timeout: float | None = None) -> int:
                return 1

            def kill(self) -> None:
                pass

        def popen(*args: object, **kwargs: object) -> object:
            calls.append(1)
            if len(calls) == 2:
                raise PermissionError(5, "second launch denied")
            kwargs["stdout"].write(b"FAIL: test_x\nfx-boom\n")
            kwargs["stdout"].flush()
            return Finished()

        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(shard.subprocess, "Popen", side_effect=popen),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
            self.assertRaises(PermissionError),
        ):
            shard.run_parallel(
                tests, "ubuntu", 1, 1, 2, start_dir=str(root), pattern=f"fxw{tag}_*.py", top_level_dir=str(root),
                repo_root=REPO_ROOT, pins={}, environ={shard.WORKER_TIMEOUT_ENV: "600"},
            )
        self.assertIn("fx-boom", out.getvalue(), "worker 1's finished output was deleted unprinted")

    # r6 adversary pass 8 A2: a log opened for a worker whose launch then fails is closed, not leaked.
    def test_a_launch_that_fails_closes_the_log_it_opened(self) -> None:
        root, tests, tag = self._fixture()
        handles: list[object] = []

        def failing_popen(*args: object, **kwargs: object) -> object:
            handles.append(kwargs["stdout"])
            raise FileNotFoundError("python vanished")

        with (
            mock.patch.object(shard.subprocess, "Popen", side_effect=failing_popen),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(FileNotFoundError),
        ):
            shard.run_parallel(
                tests, "ubuntu", 1, 1, 2, start_dir=str(root), pattern=f"fxw{tag}_*.py", top_level_dir=str(root),
                repo_root=REPO_ROOT, pins={}, environ={shard.WORKER_TIMEOUT_ENV: "600"},
            )
        self.assertEqual(len(handles), 1)
        self.assertTrue(handles[0].closed, "the log handle of the worker whose launch failed was left open")

    # r8 sol r7 B1: a termination thread that cannot be started must not stretch the cleanup past its total bound.
    def test_termination_thread_exhaustion_keeps_the_total_cleanup_bound(self) -> None:
        now = [0.0]

        def tree(proc: object) -> str | None:
            now[0] += 3.0  # each tree termination takes 3 s of the (fake) clock
            return None

        procs = [self._pid_proc() for _ in range(7)]
        with (
            mock.patch.object(shard.os, "name", "nt"),
            mock.patch.object(shard, "time", types.SimpleNamespace(monotonic=lambda: now[0])),
            mock.patch.object(shard.threading.Thread, "start", side_effect=RuntimeError("can't start new thread")),
            mock.patch.object(shard, "_kill_tree", mock.Mock(side_effect=tree)) as kill_tree,
            mock.patch.object(shard.subprocess, "run", side_effect=AssertionError("taskkill launched inline")) as run,
        ):
            results = shard.terminate_all(procs)
        self.assertLessEqual(now[0], shard.TERMINATE_BOUND_SECONDS, "seven serial inline kills overran the total bound")
        self.assertEqual([proc.returncode for proc in procs], [-9] * 7, "every worker is still killed")
        self.assertEqual(len(results), 7)
        # r8 adversary A1: a taskkill launch (process creation) is outside its own timeout, so no blocking tree
        # kill runs inline at all; the tree is reported as not confirmed.
        self.assertEqual((kill_tree.call_count, run.call_count), (0, 0))
        self.assertTrue(all(result and "tree is not confirmed" in result for result in results), results)

    # r8 adversary A2: a worker whose exit the inline fallback could not confirm is reported, not read as clean.
    def test_the_inline_fallback_reports_a_worker_whose_exit_it_could_not_confirm(self) -> None:
        class Slow(self._RunningProc):
            def kill(self) -> None:
                self.kill_calls += 1  # accepted, but the process object is not signalled yet

        for name in ("posix", "nt"):
            with self.subTest(os=name):
                proc = Slow()
                proc.pid = 424242
                with (
                    mock.patch.object(shard.os, "name", name),
                    mock.patch.object(shard.threading.Thread, "start", side_effect=RuntimeError("no thread")),
                    mock.patch.object(shard.os, "killpg", create=True),
                    mock.patch.object(shard.signal, "SIGKILL", 9, create=True),
                ):
                    results = shard.terminate_all([proc], reap=True)
                self.assertIsNotNone(results[0], "an unconfirmed exit read as a clean termination")
                self.assertIn("exit was not confirmed", results[0])
        reaped = self._pid_proc()  # a worker that is gone after the kill and a tree kill that cannot block
        with (
            mock.patch.object(shard.os, "name", "posix"),
            mock.patch.object(shard.threading.Thread, "start", side_effect=RuntimeError("no thread")),
            mock.patch.object(shard.os, "killpg", create=True),
            mock.patch.object(shard.signal, "SIGKILL", 9, create=True),
        ):
            self.assertEqual(shard.terminate_all([reaped], reap=True), [None])
        self.assertEqual(reaped.returncode, -9)

    # r8 adversary pass 14 A1: stdout closing (BrokenPipeError) before the first grouped header must not delete
    # the unprinted logs: they are kept and reported, and the run still ends with an exit code.
    def test_a_stdout_that_closes_before_the_grouped_logs_keeps_every_log(self) -> None:
        root, tests, tag = self._fixture()
        calls: list[int] = []
        real = self._finished_popen(calls)

        class ClosingOut(io.StringIO):
            broken = False

            def write(self, text: str) -> int:
                if self.broken:
                    raise BrokenPipeError(32, "the consumer closed the pipe")
                return super().write(text)

        out, err = ClosingOut(), io.StringIO()

        def popen(*args: object, **kwargs: object) -> object:
            proc = real(*args, **kwargs)
            out.broken = len(calls) == 2  # closed once the last launch is done, before any grouped header
            return proc

        with (
            mock.patch.object(shard.subprocess, "Popen", side_effect=popen),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = shard.run_parallel(
                tests, "ubuntu", 1, 1, 2, start_dir=str(root), pattern=f"fxw{tag}_*.py", top_level_dir=str(root),
                repo_root=REPO_ROOT, pins={}, environ={shard.WORKER_TIMEOUT_ENV: "600"},
            )
        kept = re.search(r"kept at (.+)$", err.getvalue(), re.MULTILINE)
        if kept:
            self.addCleanup(shutil.rmtree, kept[1], True)
        self.assertEqual(code, 1)
        self.assertIsNotNone(kept, "the unprinted logs were deleted: " + err.getvalue())
        for name in ("w1", "w2"):
            self.assertTrue(Path(kept[1], name, "log.txt").exists(), f"{name}'s unprinted log is gone")

    # r8 adversary A3: a log the parent's stdout cannot encode must not raise, lose the other logs or be deleted.
    def test_a_log_that_stdout_cannot_encode_does_not_lose_the_other_logs(self) -> None:
        root, tests, tag = self._fixture()
        calls: list[int] = []
        real = self._finished_popen(calls)

        def popen(*args: object, **kwargs: object) -> object:
            proc = real(*args, **kwargs)
            if len(calls) == 1:
                kwargs["stdout"].write(b"caf\xe9\n")  # cp1252 bytes: not UTF-8, so decoded to U+FFFD
                kwargs["stdout"].flush()
            return proc

        raw = io.BytesIO()
        out = io.TextIOWrapper(raw, encoding="cp1252", errors="strict", write_through=True)
        err = io.StringIO()
        with (
            mock.patch.object(shard.subprocess, "Popen", side_effect=popen),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = shard.run_parallel(
                tests, "ubuntu", 1, 1, 2, start_dir=str(root), pattern=f"fxw{tag}_*.py", top_level_dir=str(root),
                repo_root=REPO_ROOT, pins={}, environ={shard.WORKER_TIMEOUT_ENV: "600"},
            )
        out.flush()
        printed = raw.getvalue().decode("cp1252")
        self.assertEqual(code, 1)
        self.assertIn("fx-boom-2", printed, "worker 2's output was lost to worker 1's unencodable log")
        self.assertIn("caf", printed, "worker 1's encodable text was dropped with its unencodable character")

    # r8 sol r7 B2: one unreadable worker log must not discard the readable failure logs of the others.
    @staticmethod
    def _log_one_unreadable() -> object:
        real = Path.read_text

        def read_text(self: Path, *args: object, **kwargs: object) -> str:
            if self.name == "log.txt" and self.parent.name == "w1":
                raise PermissionError(5, "log is locked")
            return real(self, *args, **kwargs)

        return mock.patch.object(Path, "read_text", read_text)

    @staticmethod
    def _finished_popen(calls: list[int], raise_on: int | None = None) -> Callable[..., object]:
        class Finished:
            returncode = 1

            def poll(self) -> int:
                return 1

            def wait(self, timeout: float | None = None) -> int:
                return 1

            def kill(self) -> None:
                pass

        def popen(*args: object, **kwargs: object) -> object:
            calls.append(1)
            if raise_on is not None and len(calls) == raise_on:
                raise PermissionError(5, "launch denied")
            kwargs["stdout"].write(b"FAIL: test_bad\nfx-boom-%d\n" % len(calls))
            kwargs["stdout"].flush()
            return Finished()

        return popen

    def test_one_unreadable_worker_log_does_not_discard_the_other_workers_logs(self) -> None:
        root, tests, tag = self._fixture()
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(shard.subprocess, "Popen", side_effect=self._finished_popen([])),
            self._log_one_unreadable(),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = shard.run_parallel(
                tests, "ubuntu", 1, 1, 2, start_dir=str(root), pattern=f"fxw{tag}_*.py", top_level_dir=str(root),
                repo_root=REPO_ROOT, pins={}, environ={shard.WORKER_TIMEOUT_ENV: "600"},
            )
        kept = re.search(r"kept at (.+)$", err.getvalue(), re.MULTILINE)
        if kept:
            self.addCleanup(shutil.rmtree, kept[1], True)
        self.assertEqual(code, 1)
        self.assertIn("fx-boom-2", out.getvalue(), "worker 2's readable failure output was discarded")
        self.assertIn("worker 1/2 log could not be read", err.getvalue(), "the unreadable log went unreported")
        self.assertIsNotNone(kept, "the unread evidence was deleted instead of kept: " + err.getvalue())
        self.assertTrue(Path(kept[1], "w1", "log.txt").exists(), "the unread log is gone")

    def test_a_launch_error_with_one_unreadable_log_still_prints_the_others_and_names_the_unread_one(self) -> None:
        root, tests, tag = self._fixture()
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(shard.subprocess, "Popen", side_effect=self._finished_popen([], raise_on=3)),
            self._log_one_unreadable(),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
            self.assertRaises(PermissionError),
        ):
            shard.run_parallel(
                tests, "ubuntu", 1, 1, 3, start_dir=str(root), pattern=f"fxw{tag}_*.py", top_level_dir=str(root),
                repo_root=REPO_ROOT, pins={}, environ={shard.WORKER_TIMEOUT_ENV: "600"},
            )
        kept = re.search(r"kept at (.+)$", err.getvalue(), re.MULTILINE)
        if kept:
            self.addCleanup(shutil.rmtree, kept[1], True)
        self.assertIn("fx-boom-2", out.getvalue(), "worker 2's readable output was discarded")
        self.assertIn("worker 1/2 log could not be read", err.getvalue(), "the unread log was deleted silently")
        self.assertIsNotNone(kept, "the unread evidence was deleted: " + err.getvalue())

    # r9 sol r8 A2: a SystemExit(0) (an audit hook can raise one anywhere) while a worker log is read must not turn a
    # failing shard green or delete evidence nobody has seen.
    @staticmethod
    def _read_raises(name: str, worker: str, error: Callable[[], BaseException]) -> object:
        real = Path.read_text

        def read_text(self: Path, *args: object, **kwargs: object) -> str:
            if self.name == name and self.parent.name == worker:
                raise error()
            return real(self, *args, **kwargs)

        return mock.patch.object(Path, "read_text", read_text)

    class _ClosingOut(io.StringIO):
        broken = False

        def write(self, text: str) -> int:
            if self.broken:
                raise BrokenPipeError(32, "the consumer closed the pipe")
            return super().write(text)

    def test_a_system_exit_zero_while_a_log_is_read_is_a_failure_that_keeps_the_log(self) -> None:
        root, tests, tag = self._fixture()
        out, err = io.StringIO(), io.StringIO()
        code, raised = None, None
        with (
            mock.patch.object(shard.subprocess, "Popen", side_effect=self._finished_popen([])),
            self._read_raises("log.txt", "w1", lambda: SystemExit(0)),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            try:
                code = shard.run_parallel(
                    tests, "ubuntu", 1, 1, 2, start_dir=str(root), pattern=f"fxw{tag}_*.py", top_level_dir=str(root),
                    repo_root=REPO_ROOT, pins={}, environ={shard.WORKER_TIMEOUT_ENV: "600"},
                )
            except SystemExit as error:
                raised = error
        kept = re.search(r"kept at (.+)$", err.getvalue(), re.MULTILINE)
        if kept:
            self.addCleanup(shutil.rmtree, kept[1], True)
        self.assertIsNone(raised, "a SystemExit(0) from a log read escaped as a green exit")
        self.assertEqual(code, 1)
        self.assertIn("fx-boom-2", out.getvalue(), "worker 2's log was not attempted after worker 1's read exited")
        self.assertIsNotNone(kept, "the unread evidence was deleted: " + err.getvalue())
        self.assertTrue(Path(kept[1], "w1", "log.txt").exists(), "the unread log is gone")

    def test_a_system_exit_zero_in_the_fallback_log_read_keeps_the_log_and_the_others(self) -> None:
        root, tests, tag = self._fixture()
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(shard.subprocess, "Popen", side_effect=self._finished_popen([], raise_on=3)),
            self._read_raises("log.txt", "w1", lambda: SystemExit(0)),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
            self.assertRaises(PermissionError),
        ):
            shard.run_parallel(
                tests, "ubuntu", 1, 1, 3, start_dir=str(root), pattern=f"fxw{tag}_*.py", top_level_dir=str(root),
                repo_root=REPO_ROOT, pins={}, environ={shard.WORKER_TIMEOUT_ENV: "600"},
            )
        kept = re.search(r"kept at (.+)$", err.getvalue(), re.MULTILINE)
        if kept:
            self.addCleanup(shutil.rmtree, kept[1], True)
        self.assertIn("fx-boom-2", out.getvalue())
        self.assertIsNotNone(kept, "the unread evidence was deleted: " + err.getvalue())

    def test_a_system_exit_zero_reading_a_result_is_a_missing_result_not_a_green_exit(self) -> None:
        root, tests, tag = self._fixture()
        out, err = io.StringIO(), io.StringIO()
        code, raised = None, None
        with (
            mock.patch.object(shard.subprocess, "Popen", side_effect=self._finished_popen([])),
            self._read_raises("result.json", "w1", lambda: SystemExit(0)),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            try:
                code = shard.run_parallel(
                    tests, "ubuntu", 1, 1, 2, start_dir=str(root), pattern=f"fxw{tag}_*.py", top_level_dir=str(root),
                    repo_root=REPO_ROOT, pins={}, environ={shard.WORKER_TIMEOUT_ENV: "600"},
                )
            except SystemExit as error:
                raised = error
        self.assertIsNone(raised, "a SystemExit(0) from a result read escaped as a green exit")
        self.assertEqual(code, 1)
        self.assertIn("fx-boom-1", out.getvalue())
        self.assertIn("worker 1/2 wrote no readable result", err.getvalue())

    def test_a_system_exit_zero_from_anywhere_in_the_run_leaves_a_nonzero_exit_and_the_logs(self) -> None:
        root, tests, tag = self._fixture()
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(shard.subprocess, "Popen", side_effect=self._finished_popen([])),
            mock.patch.object(shard, "late_workers", side_effect=SystemExit(0)),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
            self.assertRaises(SystemExit) as raised,
        ):
            shard.run_parallel(
                tests, "ubuntu", 1, 1, 2, start_dir=str(root), pattern=f"fxw{tag}_*.py", top_level_dir=str(root),
                repo_root=REPO_ROOT, pins={}, environ={shard.WORKER_TIMEOUT_ENV: "600"},
            )
        self.assertTrue(raised.exception.code, "an interrupted run exited 0")
        self.assertIn("fx-boom-1", out.getvalue(), "the workers' output was lost with the interruption")
        self.assertIn("fx-boom-2", out.getvalue())
        self.assertIn("ERROR:", err.getvalue())

    # r9 sol r8 A1: a stdout that closes before the final SHARD_SLOWEST block must not stop the retained log
    # locations and the other failures from being reported, nor the exit code from being returned.
    def test_a_stdout_closed_before_the_slowest_block_still_reports_the_kept_logs_and_fails(self) -> None:
        root, tests, tag = self._fixture()
        calls: list[int] = []
        real = self._finished_popen(calls)

        def popen(*args: object, **kwargs: object) -> object:
            command = args[0]
            spec = json.loads(Path(command[command.index("--worker-spec") + 1]).read_text(encoding="utf-8"))
            Path(spec["result"]).write_text(
                json.dumps({"ids": [], "skipped_ids": [], "seconds": {spec["classes"][0]: 1.0}, "tests_run": 0, "ok": False}),
                encoding="utf-8",
            )
            proc = real(*args, **kwargs)
            out.broken = len(calls) == 2  # closed once the last launch is done, before any grouped header
            return proc

        out, err = self._ClosingOut(), io.StringIO()
        code, raised = None, None
        with (
            mock.patch.object(shard.subprocess, "Popen", side_effect=popen),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            try:
                code = shard.run_parallel(
                    tests, "ubuntu", 1, 1, 2, start_dir=str(root), pattern=f"fxw{tag}_*.py", top_level_dir=str(root),
                    repo_root=REPO_ROOT, pins={}, environ={shard.WORKER_TIMEOUT_ENV: "600"},
                )
            except BrokenPipeError as error:
                raised = error
        kept = re.search(r"kept at (.+)$", err.getvalue(), re.MULTILINE)
        if kept:
            self.addCleanup(shutil.rmtree, kept[1], True)
        self.assertIsNone(raised, "the SHARD_SLOWEST print raised before any problem was reported")
        self.assertEqual(code, 1)
        self.assertIsNotNone(kept, "the retained log location was never reported: " + err.getvalue())
        self.assertIn("SHARD_SLOWEST could not be printed", err.getvalue())

    def test_a_stdout_closed_before_the_serial_report_keeps_the_test_outcome(self) -> None:
        clock = FakeClock()
        out, err = self._ClosingOut(), io.StringIO()

        class Probe(unittest.TestCase):
            def test_one(self) -> None:
                clock.advance(1.0)
                out.broken = True

        Probe.__module__ = "fake_mod"
        Probe.__qualname__ = "Probe"
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = shard.run_shard([Probe("test_one")], "ubuntu", 1, 1, clock=clock, environ={}, stream=io.StringIO())
        self.assertEqual(code, 0, "the test outcome was replaced by the diagnostic print's error")
        self.assertIn("SHARD_SLOWEST could not be printed", err.getvalue())

    def test_a_budget_warning_that_cannot_be_printed_does_not_raise_and_goes_to_stderr(self) -> None:
        closed, err = self._ClosingOut(), io.StringIO()
        closed.broken = True
        watch = shard.BudgetWatch("ubuntu", 1, 3, 1.0, 0.0, lambda: 100.0, lambda: {"a.A": 1.0}, out=closed)
        with contextlib.redirect_stderr(err):
            watch.check()
        self.assertTrue(watch.warned)
        self.assertIn("::warning title=Shard budget::", err.getvalue())

    # r8 sol r7 B3: a test identity that holds a line break is kept whole in the manifest and in fixture accounting.
    def test_a_manifest_keeps_one_line_per_id_whatever_the_id_holds(self) -> None:
        sink = Path(tempfile.mkdtemp(prefix="fxw-m-"))
        self.addCleanup(shutil.rmtree, sink, True)

        def written(ids: list[str]) -> str:
            path = sink / "m.txt"
            shard.write_manifest(path, ids)
            return path.read_text(encoding="utf-8")

        self.assertNotEqual(written(["a\nb"]), written(["a", "b"]), "one id with a newline equals two ids")
        for odd in ("a\nb", "a\r\nb", "a\x85b", "a b", "a\x0bb", "a\\nb"):
            with self.subTest(odd=odd):
                self.assertEqual(len(written([odd, "z"]).splitlines()), 2)
        self.assertNotEqual(written(["a\nb"]), written(["a\\nb"]), "an escaped id collides with a literal backslash id")
        self.assertEqual(written(["tools.x.Y.test_b", "tools.x.Y.test_a"]), "tools.x.Y.test_a\ntools.x.Y.test_b\n")

    def test_a_fixture_skip_of_a_class_whose_name_holds_a_newline_is_accounted_for(self) -> None:
        odd = (
            "import unittest\n"
            "def _skip(cls):\n    raise unittest.SkipTest('accepted fixture skip')\n"
            "Odd = type('Odd\\nName', (unittest.TestCase,), "
            "{'__module__': __name__, 'setUpClass': classmethod(_skip), 'test_it': lambda self: None})\n"
        )
        root, tests, tag = self._fixture(sources={"a": self._GOOD, "b": odd})
        serial_manifest = root / "manifest-serial.txt"
        with contextlib.redirect_stdout(io.StringIO()):
            serial = shard.run_shard(
                tests, "ubuntu", 1, 1, clock=time.monotonic, environ={}, stream=io.StringIO(), manifest=serial_manifest
            )
        self.assertEqual(serial, 0)
        code, out, err, manifest = self._parallel(root, tests, tag, 2)
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(manifest.read_text(encoding="utf-8"), serial_manifest.read_text(encoding="utf-8"))

    def test_a_fixture_skip_holder_with_a_newline_in_its_class_is_parsed(self) -> None:
        clock = FakeClock()
        case = _fake_case(clock, "Odd\nName", 0.0)
        case.__class__.__module__ = "mod_nl"
        result = shard.ClassTimingResult(io.StringIO(), True, 0, clock=clock)
        holder = f"setUpClass (mod_nl.{type(case).__qualname__})"
        result.addSkip(types.SimpleNamespace(id=lambda: holder), "class fixture")
        self.assertEqual(result.skipped_by_fixture([case]), [case.id()])

    # r4 adversary A2 (pass 4): a worker whose only class skips in a fixture but whose cleanup fails
    # runs no test; serial fails it (exit 1 with the others' tests run), so the parallel shard must too.
    _GOOD ="import unittest\nclass FxGoodTests(unittest.TestCase):\n    def test_ok(self):\n        pass\n"
    _BAD_CLASS_CLEANUP = (
        "import unittest\n"
        "def _boom():\n    raise RuntimeError('cleanup-boom')\n"
        "class FxSkipTests(unittest.TestCase):\n"
        "    @classmethod\n    def setUpClass(cls):\n        cls.addClassCleanup(_boom)\n"
        "        raise unittest.SkipTest('fixture skip')\n"
        "    def test_never(self):\n        pass\n"
    )
    _BAD_MODULE_CLEANUP = (
        "import unittest\n"
        "def _boom():\n    raise RuntimeError('cleanup-boom')\n"
        "def setUpModule():\n    unittest.addModuleCleanup(_boom)\n    raise unittest.SkipTest('fixture skip')\n"
        "class FxSkipTests(unittest.TestCase):\n    def test_never(self):\n        pass\n"
    )

    def test_a_fixture_skip_whose_cleanup_fails_fails_the_shard_as_serial_does(self) -> None:
        for label, bad in (("class cleanup", self._BAD_CLASS_CLEANUP), ("module cleanup", self._BAD_MODULE_CLEANUP)):
            with self.subTest(label):
                root, tests, tag = self._fixture(sources={"a": self._GOOD, "b": bad})
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    serial = shard.run_shard(tests, "ubuntu", 1, 1, clock=time.monotonic, environ={}, stream=io.StringIO())
                self.assertEqual(serial, 1, "serial fails a failed fixture cleanup")
                code, out, err, _manifest = self._parallel(root, tests, tag, 2)
                self.assertEqual(code, 1, out + err)

    def test_a_worker_that_records_a_failed_run_but_exits_zero_or_five_fails_the_shard(self) -> None:
        for exit_code in (0, 5):
            with self.subTest(exit_code=exit_code):
                root, tests, tag = self._fixture()

                class DonePopen:
                    def __init__(self, command: list[str], **kwargs: object) -> None:
                        spec = json.loads(Path(command[command.index("--worker-spec") + 1]).read_text(encoding="utf-8"))
                        record = {"ids": [], "skipped_ids": [], "seconds": {}, "tests_run": 0, "ok": False}
                        Path(spec["result"]).write_text(json.dumps(record), encoding="utf-8")
                        self.returncode = exit_code

                    def poll(self) -> int:
                        return exit_code

                    def wait(self, timeout: float | None = None) -> int:
                        return exit_code

                    def kill(self) -> None:
                        pass

                out, err = io.StringIO(), io.StringIO()
                with (
                    mock.patch.object(shard.subprocess, "Popen", DonePopen),
                    contextlib.redirect_stdout(out),
                    contextlib.redirect_stderr(err),
                ):
                    code = shard.run_parallel(
                        tests, "ubuntu", 1, 1, 2, start_dir=str(root), pattern=f"fxw{tag}_*.py", top_level_dir=str(root),
                        repo_root=REPO_ROOT, pins={}, environ={}, timeout=120.0,
                    )
                self.assertEqual(code, 1)
                self.assertIn(f"recorded a failed run but exited {exit_code}", err.getvalue())

    # r4 adversary A1 (pass 5): exit 5 means "no tests ran"; a child that ran tests and still exits 5
    # (an atexit hook calling os._exit(5), say) must not pass just because its record says ok.
    def test_a_worker_that_ran_tests_and_exits_five_fails_the_shard(self) -> None:
        root, tests, tag = self._fixture()

        class AtExitFivePopen:
            def __init__(self, command: list[str], **kwargs: object) -> None:
                spec = json.loads(Path(command[command.index("--worker-spec") + 1]).read_text(encoding="utf-8"))
                ids = [test.id() for test in tests if f"{type(test).__module__}.{type(test).__qualname__}" in spec["classes"]]
                record = {"ids": ids, "skipped_ids": [], "seconds": {}, "tests_run": len(ids), "ok": True}
                Path(spec["result"]).write_text(json.dumps(record), encoding="utf-8")
                self.returncode = 5

            def poll(self) -> int:
                return 5

            def wait(self, timeout: float | None = None) -> int:
                return 5

            def kill(self) -> None:
                pass

        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(shard.subprocess, "Popen", AtExitFivePopen),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = shard.run_parallel(
                tests, "ubuntu", 1, 1, 2, start_dir=str(root), pattern=f"fxw{tag}_*.py", top_level_dir=str(root),
                repo_root=REPO_ROOT, pins={}, environ={}, timeout=120.0,
            )
        self.assertEqual(code, 1)
        self.assertIn("exited 5 after running", err.getvalue())

    # r10 sol A2: a zero-exit worker whose record names its ids but a contradictory tests_run (the real
    # worker always has tests_run == len(ids), unittest counts a test at its start) must not pass.
    def test_a_zero_exit_worker_with_a_contradictory_tests_run_fails_the_shard(self) -> None:
        for bad in (-1, 0, 2, 999):
            with self.subTest(tests_run=bad):
                root, tests, tag = self._fixture()

                class CountLiesPopen:
                    def __init__(self, command: list[str], **kwargs: object) -> None:
                        spec = json.loads(Path(command[command.index("--worker-spec") + 1]).read_text(encoding="utf-8"))
                        ids = [t.id() for t in tests if f"{type(t).__module__}.{type(t).__qualname__}" in spec["classes"]]
                        record = {"ids": ids, "skipped_ids": [], "seconds": {}, "tests_run": bad, "ok": True}
                        Path(spec["result"]).write_text(json.dumps(record), encoding="utf-8")
                        self.returncode = 0

                    def poll(self) -> int:
                        return 0

                    def wait(self, timeout: float | None = None) -> int:
                        return 0

                    def kill(self) -> None:
                        pass

                out, err = io.StringIO(), io.StringIO()
                with (
                    mock.patch.object(shard.subprocess, "Popen", CountLiesPopen),
                    contextlib.redirect_stdout(out),
                    contextlib.redirect_stderr(err),
                ):
                    code = shard.run_parallel(
                        tests, "ubuntu", 1, 1, 2, start_dir=str(root), pattern=f"fxw{tag}_*.py", top_level_dir=str(root),
                        repo_root=REPO_ROOT, pins={}, environ={}, timeout=120.0,
                    )
                self.assertEqual(code, 1, err.getvalue())
                self.assertIn("wrote no readable result", err.getvalue())

    # r11 adversary A1 (pass 18): a record must also agree with what THIS worker was assigned, not only with itself.
    def test_a_worker_record_naming_classes_or_ids_it_was_not_assigned_fails_the_shard(self) -> None:
        def foreign_timing(record: dict, _other_ids: list[str]) -> None:
            record["seconds"] = {"other.D": 900.0}

        def foreign_id(record: dict, other_ids: list[str]) -> None:
            record["ids"] = record["ids"] + other_ids[:1]
            record["tests_run"] = len(record["ids"])

        for label, mutate in (("a timing for an unassigned class", foreign_timing), ("an id of another worker", foreign_id)):
            with self.subTest(label):
                root, tests, tag = self._fixture()
                assigned: dict[str, list[str]] = {}

                class LiarPopen:
                    def __init__(self, command: list[str], **kwargs: object) -> None:
                        spec = json.loads(Path(command[command.index("--worker-spec") + 1]).read_text(encoding="utf-8"))
                        key = spec["result"]
                        assigned[key] = [t.id() for t in tests if f"{type(t).__module__}.{type(t).__qualname__}" in spec["classes"]]
                        others = [i for k, ids in assigned.items() if k != key for i in ids]
                        record = {"ids": list(assigned[key]), "skipped_ids": [], "seconds": {}, "tests_run": len(assigned[key]), "ok": True}
                        mutate(record, others or [t.id() for t in tests])
                        Path(key).write_text(json.dumps(record), encoding="utf-8")
                        self.returncode = 0

                    def poll(self) -> int:
                        return 0

                    def wait(self, timeout: float | None = None) -> int:
                        return 0

                    def kill(self) -> None:
                        pass

                out, err = io.StringIO(), io.StringIO()
                with (
                    mock.patch.object(shard.subprocess, "Popen", LiarPopen),
                    contextlib.redirect_stdout(out),
                    contextlib.redirect_stderr(err),
                ):
                    code = shard.run_parallel(
                        tests, "ubuntu", 1, 1, 2, start_dir=str(root), pattern=f"fxw{tag}_*.py", top_level_dir=str(root),
                        repo_root=REPO_ROOT, pins={}, environ={}, timeout=120.0,
                    )
                self.assertEqual(code, 1, out.getvalue() + err.getvalue())
                self.assertNotIn("other.D", out.getvalue(), "an unassigned class's timing reached the report")

    # r11 adversary A2 (pass 18): two members sharing one id() must both be accounted when their fixture skips.
    def test_a_fixture_skip_is_accounted_per_member_not_per_id(self) -> None:
        class SharedA(unittest.TestCase):
            def id(self) -> str:  # noqa: A003 - the point of the test: two distinct members, one id
                return "shared"

            def test_one(self) -> None:
                pass

        class SharedB(unittest.TestCase):
            @classmethod
            def setUpClass(cls) -> None:
                raise unittest.SkipTest("fixture")

            def id(self) -> str:  # noqa: A003
                return "shared"

            def test_one(self) -> None:
                pass

        members = [SharedA("test_one"), SharedB("test_one")]
        result = unittest.TextTestRunner(stream=io.StringIO(), resultclass=shard.ClassTimingResult).run(
            shard.ClassTimingSuite(members)
        )
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(result.started_ids, ["shared"])
        self.assertEqual(result.skipped_by_fixture(members), ["shared"], "the fixture-skipped member was dropped by id")

    # r11 adversary A2 (pass 19): the zero-exit guard must not call methods of the code it is judging.
    def test_the_zero_exit_guard_does_not_run_methods_of_an_exotic_exit_code(self) -> None:
        class NonzeroThatExitsZeroWhenTested(int):
            def __bool__(self) -> bool:
                raise SystemExit(0)

        class ZeroThatExitsZeroWhenShown(int):
            def __repr__(self) -> str:
                raise SystemExit(0)

        for label, code in (
            ("a nonzero code whose __bool__ raises SystemExit(0)", NonzeroThatExitsZeroWhenTested(1)),
            ("a zero code whose __repr__ raises SystemExit(0)", ZeroThatExitsZeroWhenShown(0)),
        ):
            with self.subTest(label):
                def ended() -> int:
                    raise SystemExit(code)

                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                    shard.never_exits_zero(ended)()
                self.assertFalse(int.__eq__(caught.exception.code, 0), "a run cut short ended with a zero exit")

    # r12 sol r11 A1: an exit code whose __class__ raises SystemExit(0) (isinstance reads __class__) must not end green.
    def test_the_zero_exit_guard_does_not_read_the_class_attribute_of_an_exit_code(self) -> None:
        def explode(self: object) -> type:
            raise SystemExit(0)

        class ClassAttributeExits:
            __class__ = property(explode)  # type: ignore[assignment]

        code = ClassAttributeExits()

        def ended() -> int:
            raise SystemExit(code)

        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            shard.never_exits_zero(ended)()
        raised = caught.exception.code
        zero = issubclass(type(raised), int) and int.__eq__(raised, 0) is True
        self.assertFalse(zero, "a run cut short ended with a zero exit")
        self.assertIsNotNone(raised, "a non-integer exit code is nonzero (exit 1), never None")

    # r12 sibling sweep of A1: the ``isinstance(error, KeyboardInterrupt)`` handlers read the error's __class__ too.
    def test_a_handler_does_not_read_the_class_attribute_of_the_error_it_contains(self) -> None:
        def explode(self: object) -> type:
            raise SystemExit(0)

        class ClassAttributeExits(SystemExit):
            __class__ = property(explode)  # type: ignore[assignment]

        text = unreadable = emitted = None
        escaped = False
        try:
            with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
                Path, "read_text", side_effect=ClassAttributeExits(0)
            ):
                text, unreadable = shard.read_log(Path(tmp) / "log.txt")
            with mock.patch("builtins.print", side_effect=ClassAttributeExits(0)):
                emitted = shard.emit_text("x", io.StringIO())
        except SystemExit:
            escaped = True  # failed below, outside the handler: the runner must not chain to the hostile error
        self.assertFalse(escaped, "a SystemExit escaped a handler that was meant to contain it")
        self.assertEqual(text, "")
        self.assertIsNotNone(unreadable)
        self.assertIsNotNone(emitted)
        self.assertFalse(shard.is_interrupt(ClassAttributeExits(0)))
        self.assertTrue(shard.is_interrupt(KeyboardInterrupt()))

    # r12 adversary pass 21 A1: a SystemExit subclass whose ``code`` property raises SystemExit(0) must not end the run green.
    def test_the_zero_exit_guard_does_not_read_a_code_property_of_the_exit_it_judges(self) -> None:
        def explode(self: object) -> int:
            raise SystemExit(0)

        class CodeGetterExits(SystemExit):
            code = property(explode)  # type: ignore[assignment]

        for label, raised, nonzero in (("nonzero", CodeGetterExits(1), True), ("zero", CodeGetterExits(0), False)):
            def ended() -> int:
                raise raised

            outcome: SystemExit | None = None
            with contextlib.redirect_stderr(io.StringIO()):
                try:
                    shard.never_exits_zero(ended)()
                except SystemExit as error:
                    outcome = error
            # asserted outside the handler: a failure here must not chain to the hostile exception
            with self.subTest(label):
                self.assertIsNotNone(outcome, "the guard let the run return normally")
                final = SystemExit.code.__get__(outcome, SystemExit)
                self.assertIs(issubclass(type(final), int) and int.__eq__(final, 0) is True, False, "ended with a zero exit")
                self.assertEqual(final, 1)

    # r12 adversary pass 21 A2: an interrupt inside Popen after the OS created the child leaves nothing returned to end.
    def test_an_interrupt_inside_the_launch_after_the_child_exists_still_ends_the_worker(self) -> None:
        root, tests, tag = self._fixture()
        made: list[object] = []
        running = self._RunningProc

        class HalfBuilt(running):  # type: ignore[valid-type, misc]
            def __init__(self, *args: object, **kwargs: object) -> None:
                super().__init__()
                self.pid = 424242
                self._handle = 7  # a real Windows ``Popen`` stores its process handle with the pid
                made.append(self)
                raise KeyboardInterrupt  # the child exists; the constructor never returns it

        out, err = io.StringIO(), io.StringIO()
        raised: BaseException | None = None
        with (
            mock.patch.object(shard.subprocess, "Popen", HalfBuilt),
            mock.patch.object(shard, "_kill_tree", return_value=None),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            try:
                shard.run_parallel(
                    tests, "ubuntu", 1, 1, 2, start_dir=str(root), pattern=f"fxw{tag}_*.py", top_level_dir=str(root),
                    repo_root=REPO_ROOT, pins={}, environ={shard.WORKER_TIMEOUT_ENV: "600"},
                )
            except BaseException as error:  # noqa: BLE001
                raised = error
        for kept in re.findall(r"kept at (.+)$", err.getvalue(), re.MULTILINE):
            self.addCleanup(shutil.rmtree, kept, True)
        self.assertIsInstance(raised, KeyboardInterrupt, repr(raised) + err.getvalue())
        self.assertEqual(len(made), 1)
        self.assertEqual(made[0].returncode, -9, "the child the launch created was left running")

    def test_half_built_worker_is_none_when_no_child_was_created(self) -> None:
        def build() -> None:
            self = subprocess.Popen.__new__(subprocess.Popen)  # noqa: F841 - a Popen with no pid: nothing was created
            raise KeyboardInterrupt

        try:
            build()
        except KeyboardInterrupt as error:
            found = shard.half_built_worker(error)
        self.assertIsNone(found)

    # r13 adversary pass 22 A1: CreateProcess returned (hp, pid) but Popen was cut short before storing self.pid.
    def test_half_built_worker_takes_the_child_identity_from_create_process_when_popen_never_stored_it(self) -> None:
        class CutShort(subprocess.Popen):  # type: ignore[type-arg]
            def _execute_child(self, *args: object, **kwargs: object) -> None:
                hp, ht, pid, tid = 111, 112, 424242, 1  # noqa: F841 - what CreateProcess returned
                raise KeyboardInterrupt

        found = None
        try:
            CutShort([sys.executable, "-c", "pass"])
        except KeyboardInterrupt as error:
            found = shard.half_built_worker(error)
        if found is not None:
            self.addCleanup(setattr, found, "_child_created", False)  # nothing real stands behind pid 424242
        self.assertIsNotNone(found, "the child CreateProcess made was not handed over")
        self.assertEqual(found.pid, 424242)  # type: ignore[union-attr]

    # r13 adversary pass 22 A2: the nearest Popen is this launch's; a live one deeper in the stack is not its child.
    def test_half_built_worker_never_adopts_an_unrelated_live_popen_from_a_deeper_frame(self) -> None:
        unrelated = subprocess.Popen.__new__(subprocess.Popen)
        unrelated.pid = 99
        unrelated.returncode = None

        def audit_callback(self: object) -> None:
            raise KeyboardInterrupt

        class NothingCreated(subprocess.Popen):  # type: ignore[type-arg]
            def __init__(self, *args: object, **kwargs: object) -> None:
                self.pid = None  # type: ignore[assignment]
                self.returncode = None
                audit_callback(unrelated)

        found: object = "unset"
        try:
            NothingCreated([sys.executable])
        except KeyboardInterrupt as error:
            found = shard.half_built_worker(error)
        self.assertIsNone(found, "an unrelated running Popen was taken for the launch's child")

    # r16 sol r15 A1: a recovery that fails on the nearest constructor must stop there, never walk on to a deeper Popen.
    def test_half_built_worker_never_adopts_a_deeper_popen_when_the_nearest_recovery_fails(self) -> None:
        unrelated = subprocess.Popen.__new__(subprocess.Popen)
        unrelated.pid = 99
        unrelated.returncode = None
        real_created = shard._created_child
        calls: list[object] = []

        def created_child(candidate: object, link: object) -> object:
            calls.append(candidate)
            if len(calls) == 1:
                raise RuntimeError("an audit hook denied the frame read")  # the nearest constructor's recovery
            return real_created(candidate, link)  # type: ignore[arg-type]

        def audit_callback(self: object) -> None:
            raise KeyboardInterrupt

        class NothingCreated(subprocess.Popen):  # type: ignore[type-arg]
            def __init__(self, *args: object, **kwargs: object) -> None:
                self.pid = None  # type: ignore[assignment]
                self.returncode = None
                audit_callback(unrelated)

        found: object = "unset"
        try:
            NothingCreated([sys.executable])
        except KeyboardInterrupt as error:
            with mock.patch.object(shard, "_created_child", created_child):
                found = shard.half_built_worker(error)
        self.assertIsNot(found, unrelated, "a Popen from a deeper frame was taken for the launch's child")
        self.assertIsNone(found)

    def test_half_built_worker_still_hands_over_a_child_whose_pid_was_stored_when_the_recovery_fails(self) -> None:
        class StoredPid(subprocess.Popen):  # type: ignore[type-arg]
            def __init__(self, *args: object, **kwargs: object) -> None:
                self.pid = 424242  # type: ignore[assignment]
                self._handle = 7  # a real Windows ``Popen`` stores its process handle with the pid
                self.returncode = None
                raise KeyboardInterrupt

        found: object = None
        try:
            StoredPid([sys.executable])
        except KeyboardInterrupt as error:
            with mock.patch.object(shard, "_created_child", side_effect=RuntimeError("denied")):
                found = shard.half_built_worker(error)
        self.assertIsNotNone(found, "the child whose pid the constructor had stored was dropped")
        self.assertEqual(found.pid, 424242)  # type: ignore[union-attr]

    # r17 sol r16 A3: when the constructor's own frames cannot be read, a deeper callback's Popen is still not the child.
    def test_half_built_worker_never_adopts_a_deeper_popen_when_the_constructor_frames_cannot_be_read(self) -> None:
        unrelated = subprocess.Popen.__new__(subprocess.Popen)
        unrelated.pid = 99
        unrelated.returncode = None

        def audit_callback(self: object) -> None:
            raise KeyboardInterrupt

        class NothingCreated(subprocess.Popen):  # type: ignore[type-arg]
            def __init__(self, *args: object, **kwargs: object) -> None:
                self.pid = None  # type: ignore[assignment]
                self.returncode = None
                audit_callback(unrelated)

        class Link:
            """A traceback link whose frame read is denied for the constructor's own frame (an audit hook's doing)."""

            def __init__(self, real: types.TracebackType) -> None:
                self._real = real

            @property
            def tb_frame(self) -> types.FrameType:
                if self._real.tb_frame.f_code.co_name == "__init__":
                    raise RuntimeError("an audit hook denied the frame read")
                return self._real.tb_frame

            @property
            def tb_next(self) -> object:
                return None if self._real.tb_next is None else Link(self._real.tb_next)

        class Slot:
            def __get__(self, error: BaseException, owner: type) -> object:
                return Link(error.__traceback__)  # type: ignore[arg-type]

        found: object = "unset"
        try:
            NothingCreated([sys.executable])
        except KeyboardInterrupt as error:
            with mock.patch.object(shard, "_TRACEBACK_SLOT", Slot()):
                found = shard.half_built_worker(error)
        self.assertIsNot(found, unrelated, "a Popen from a deeper frame was taken for the launch's child")
        self.assertIsNone(found)

    # r17 sol r16 A1: the first interrupt met in the final census is kept when the retry then completes.
    def test_the_first_census_interrupt_is_raised_when_the_retry_of_the_census_succeeds(self) -> None:
        procs = [self._RunningProc(), self._RunningProc()]
        results = [KeyboardInterrupt("first"), ({}, None)]  # the first call is interrupted, the retry completes normally
        with (
            mock.patch.object(shard, "end_running_workers", side_effect=results),
            self.assertRaises(KeyboardInterrupt) as caught,
        ):
            self._run_with_fake_workers(procs, past_deadline=True)
        self.assertEqual(str(caught.exception), "first")

    # r17 sol r16 A3 (second form): an unreadable outer constructor frame must not let an inner constructor's Popen through.
    def test_half_built_worker_never_adopts_a_callbacks_own_constructor_when_the_outer_frames_cannot_be_read(self) -> None:
        class Inner(subprocess.Popen):  # type: ignore[type-arg]
            """Keeps the stdlib ``Popen.__init__`` frame; only the OS launch is replaced."""

            def _execute_child(self, *args: object, **kwargs: object) -> None:
                self.pid = 99  # type: ignore[assignment]
                raise KeyboardInterrupt

        def callback() -> None:
            Inner(["callback-owned-child"])

        class Outer(subprocess.Popen):  # type: ignore[type-arg]
            def __init__(self, *args: object, **kwargs: object) -> None:
                self.pid = None  # type: ignore[assignment]
                self.returncode = None
                callback()

        class Link:
            """The OUTER constructor's frame read is denied (an audit hook's doing); every other frame reads."""

            def __init__(self, real: types.TracebackType, state: dict[str, bool]) -> None:
                self._real, self._state = real, state

            @property
            def tb_frame(self) -> types.FrameType:
                if self._real.tb_frame.f_code.co_name == "__init__" and not self._state["denied"]:
                    self._state["denied"] = True
                    raise RuntimeError("an audit hook denied the frame read")
                return self._real.tb_frame

            @property
            def tb_next(self) -> object:
                return None if self._real.tb_next is None else Link(self._real.tb_next, self._state)

        class Slot:
            def __get__(self, error: BaseException, owner: type) -> object:
                return Link(error.__traceback__, {"denied": False})  # type: ignore[arg-type]

        found: object = "unset"
        try:
            Outer(["worker"])
        except KeyboardInterrupt as error:
            with mock.patch.object(shard, "_TRACEBACK_SLOT", Slot()):
                found = shard.half_built_worker(error)
        self.assertIsNone(found, "a callback's own Popen was taken for the launch's child")

    # r17 sol r16 A2: truth-testing a saved exception must not be how the first one is kept.
    def test_end_running_workers_keeps_the_first_exception_whatever_its_truth_value(self) -> None:
        class Hostile(KeyboardInterrupt):
            def __bool__(self) -> bool:
                raise SystemExit(0)

        class Falsy(KeyboardInterrupt):
            def __bool__(self) -> bool:
                return False

        for first_type in (Hostile, Falsy):
            polls = [first_type("first"), OSError("second")]

            class Poller(self._RunningProc):  # type: ignore[valid-type, misc]
                def poll(self) -> int | None:
                    if polls and self.kill_calls == 0:
                        raise polls.pop(0)
                    return super().poll()

            procs = [Poller(), Poller()]
            with self.subTest(first=first_type.__name__), mock.patch.object(shard, "_kill_tree", return_value=None):
                ended, interrupt = shard.end_running_workers(procs)  # type: ignore[arg-type]
                self.assertEqual([proc.returncode for proc in procs], [-9, -9], "a worker was left running")
                self.assertIsInstance(interrupt, first_type, "the first exception met was replaced")

    # r17 sol r16 A1: an interrupt met while a watchdog is stopped is raised once the cleanup is done, never dropped.
    def test_an_interrupt_met_while_the_serial_watchdog_is_stopped_is_raised(self) -> None:
        clock = FakeClock()

        class Probe(unittest.TestCase):
            def test_one(self) -> None:
                clock.advance(1.0)

        Probe.__module__ = "fake_mod"
        Probe.__qualname__ = "Probe"
        real_stop = shard.BudgetWatch.stop

        def stop(self: object) -> BaseException | None:
            real_stop(self)  # type: ignore[arg-type]
            return KeyboardInterrupt("cancel during stop")

        with (
            mock.patch.object(shard.BudgetWatch, "stop", stop),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(KeyboardInterrupt),
        ):
            shard.run_shard([Probe("test_one")], "ubuntu", 1, 1, clock=clock, environ={}, stream=io.StringIO())

    def test_the_watchdog_stops_hand_back_the_interrupt_they_meet_instead_of_swallowing_it(self) -> None:
        class DeadThread:
            def is_alive(self) -> bool:
                raise KeyboardInterrupt("cancel during stop")

        for make in (
            lambda: shard.BudgetWatch("ubuntu", 1, 1, 10.0, 0.0, time.monotonic, dict),
            lambda: shard.DeadlineKiller(1e12, time.monotonic),
        ):
            watchdog = make()
            watchdog._thread = DeadThread()  # type: ignore[attr-defined]
            with self.subTest(kind=type(watchdog).__name__):
                self.assertIsInstance(watchdog.stop(), KeyboardInterrupt)

    def test_an_interrupt_met_while_a_watchdog_is_stopped_is_raised_by_run_parallel_after_the_cleanup(self) -> None:
        procs = [self._RunningProc(), self._RunningProc()]
        real_stop = shard.DeadlineKiller.stop

        def stop(self: object) -> BaseException | None:
            real_stop(self)  # type: ignore[arg-type]
            return KeyboardInterrupt("cancel during stop")

        with (
            mock.patch.object(shard.DeadlineKiller, "stop", stop),
            mock.patch.object(shard, "_kill_tree", return_value=None),
            self.assertRaises(KeyboardInterrupt),
        ):
            self._run_with_fake_workers(procs, past_deadline=True)
        self.assertEqual([proc.returncode for proc in procs], [-9, -9], "a worker was left running")

    # r16 sol r15 A2: an interrupt at the final cleanup census must not skip ending the owned workers.
    def test_an_interrupt_in_the_final_census_still_ends_every_owned_worker(self) -> None:
        class InterruptedOnce(self._RunningProc):  # type: ignore[valid-type, misc]
            fired = False

            def poll(self) -> int | None:
                if not InterruptedOnce.fired:
                    InterruptedOnce.fired = True
                    raise KeyboardInterrupt  # the first look at any worker: the final census (a launch failed before any other)
                return super().poll()

        procs = [InterruptedOnce(), InterruptedOnce(), self._RunningProc()]
        raised: BaseException | None = None
        with mock.patch.object(shard, "_kill_tree", return_value=None):
            try:
                _code, _err, raised = self._run_with_fake_workers(procs, launch_error_after=2)
            except BaseException as error:  # noqa: BLE001 - the interrupt itself, on a head that lets it out
                raised = error
        self.assertTrue(InterruptedOnce.fired, "the census interrupt never fired: the test does not exercise the cleanup")
        self.assertEqual([proc.returncode for proc in procs[:2]], [-9, -9], f"a worker was left running (raised {raised!r})")
        self.assertIsInstance(raised, OSError, "the launch's own error is the one that propagates")

    def test_end_running_workers_ends_every_worker_when_a_look_at_one_is_interrupted(self) -> None:
        class PollRaises(self._RunningProc):  # type: ignore[valid-type, misc]
            def poll(self) -> int | None:
                if self.kill_calls == 0:
                    raise KeyboardInterrupt
                return super().poll()

        procs = [PollRaises(), self._RunningProc()]
        with mock.patch.object(shard, "_kill_tree", return_value=None):
            ended, interrupt = shard.end_running_workers(procs)  # type: ignore[arg-type]
        self.assertEqual([proc.returncode for proc in procs], [-9, -9])
        self.assertEqual(sorted(ended), [1, 2])
        self.assertIsInstance(interrupt, KeyboardInterrupt, "the interrupt is handed back, not swallowed")

    def test_end_running_workers_goes_again_when_terminate_all_is_interrupted(self) -> None:
        procs = [self._RunningProc(), self._RunningProc()]
        real = shard.terminate_all
        calls: list[int] = []

        def terminate_all(group: list, **kwargs: object) -> list:
            calls.append(1)
            if len(calls) == 1:
                raise KeyboardInterrupt
            return real(group, **kwargs)

        with mock.patch.object(shard, "_kill_tree", return_value=None), mock.patch.object(shard, "terminate_all", terminate_all):
            ended, interrupt = shard.end_running_workers(procs)  # type: ignore[arg-type]
        self.assertEqual([proc.returncode for proc in procs], [-9, -9], "a worker was left running")
        self.assertEqual(ended, {1: None, 2: None})
        self.assertIsInstance(interrupt, KeyboardInterrupt)

    def test_an_interrupt_met_in_the_final_cleanup_is_raised_once_the_workers_are_ended(self) -> None:
        procs = [self._RunningProc(), self._RunningProc(), self._RunningProc()]
        with (
            mock.patch.object(shard, "end_running_workers", return_value=({}, KeyboardInterrupt("census"))),
            self.assertRaises(KeyboardInterrupt),
        ):
            self._run_with_fake_workers(procs, past_deadline=True)

    # r16 sol r15 A3: a stale process-group id (the worker is already reaped) must never be signalled.
    class _GroupProc:
        def __init__(self, returncode: int | None) -> None:
            self.pid = 424242
            self.returncode = returncode

    def _posix_kill(self, proc: object) -> tuple[str | None, list[tuple[int, int]]]:
        sent: list[tuple[int, int]] = []
        with (
            mock.patch.object(shard.os, "name", "posix"),
            mock.patch.object(shard.os, "killpg", lambda pgid, sig: sent.append((pgid, sig)), create=True),
            mock.patch.object(shard.signal, "SIGKILL", 9, create=True),
        ):
            result = shard._kill_tree(proc)  # type: ignore[arg-type]
        return result, sent

    def test_a_reaped_worker_gets_no_group_signal(self) -> None:
        result, sent = self._posix_kill(self._GroupProc(-9))
        self.assertEqual(sent, [], "a group id that may belong to a stranger now was signalled")
        self.assertIsNotNone(result, "a reaped leader's unobserved descendants were reported ended")  # r18 sol r17 A2
        self.assertIn("not confirmed", result or "")

    def test_an_unreaped_worker_still_gets_its_group_signal(self) -> None:
        result, sent = self._posix_kill(self._GroupProc(None))
        self.assertEqual(sent, [(424242, 9)])
        self.assertIsNone(result)

    def test_a_reap_running_while_the_group_is_killed_is_seen_before_any_signal(self) -> None:
        proc = self._GroupProc(None)
        proc._waitpid_lock = threading.Lock()  # type: ignore[attr-defined]
        proc._waitpid_lock.acquire()  # type: ignore[attr-defined]  # the other thread is inside its reap
        outcome: list[tuple[str | None, list[tuple[int, int]]]] = []
        sent: list[tuple[int, int]] = []

        def kill() -> None:
            outcome.append((shard._kill_tree(proc), sent))  # type: ignore[arg-type]

        with (
            mock.patch.object(shard.os, "name", "posix"),
            mock.patch.object(shard.os, "killpg", lambda pgid, sig: sent.append((pgid, sig)), create=True),
            mock.patch.object(shard.signal, "SIGKILL", 9, create=True),
        ):
            thread = threading.Thread(target=kill)
            thread.start()
            time.sleep(0.1)  # the kill is now waiting on the lock
            proc.returncode = -9  # the reap finishes ...
            proc._waitpid_lock.release()  # type: ignore[attr-defined]  # ... and lets go of the lock
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(sent, [], "the group was signalled after the other thread had reaped the worker")

    # r17 sol r16 A2: a lock that cannot be had is no proof of "unreaped": no group signal goes out unlocked.
    def test_no_group_signal_goes_out_when_the_reap_lock_cannot_be_taken(self) -> None:
        proc = self._GroupProc(None)
        proc._waitpid_lock = threading.Lock()  # type: ignore[attr-defined]
        proc._waitpid_lock.acquire()  # type: ignore[attr-defined]  # another thread keeps it past the bound
        self.addCleanup(proc._waitpid_lock.release)  # type: ignore[attr-defined]
        with mock.patch.object(shard, "GROUP_KILL_LOCK_SECONDS", 0.05):
            result, sent = self._posix_kill(proc)
        self.assertEqual(sent, [], "the group was signalled with the reap state unread under its lock")
        self.assertIsNotNone(result, "the unsignalled tree was reported as ended")
        self.assertIn("not confirmed", result or "")

    # r18 sol r17 A2: a leader reaped before the kill reaches its group says nothing about its descendants.
    class _ReapedLeader:
        def __init__(self) -> None:
            self.pid = 424242
            self.returncode: int | None = 0  # reaped by the other thread after the watchdog chose it
            self.kill_calls = 0

        def poll(self) -> int | None:
            return None  # the look that selected it, before the reap

        def kill(self) -> None:
            self.kill_calls += 1

        def wait(self, timeout: float | None = None) -> int:
            return 0

    def test_terminate_does_not_report_a_clean_tree_kill_after_the_leader_was_reaped(self) -> None:
        sent: list[tuple[int, int]] = []
        with (
            mock.patch.object(shard.os, "name", "posix"),
            mock.patch.object(shard.os, "killpg", lambda pgid, sig: sent.append((pgid, sig)), create=True),
            mock.patch.object(shard.signal, "SIGKILL", 9, create=True),
        ):
            result = shard.terminate(self._ReapedLeader())  # type: ignore[arg-type]
            threaded = shard.terminate_all([self._ReapedLeader()])  # type: ignore[list-item]
        self.assertEqual(sent, [], "a possibly recycled group id was signalled")
        self.assertIsNotNone(result, "terminate called a reaped leader's unobserved tree ended")
        self.assertIn("not confirmed", result or "")
        self.assertIsNotNone(threaded[0], "terminate_all called a reaped leader's unobserved tree ended")

    def test_the_deadline_watchdog_does_not_record_a_reaped_leaders_tree_as_killed(self) -> None:
        proc = self._ReapedLeader()
        killer = shard.DeadlineKiller(0.0, lambda: 1.0)
        with (
            mock.patch.object(shard.os, "name", "posix"),
            mock.patch.object(shard.os, "killpg", lambda pgid, sig: None, create=True),
            mock.patch.object(shard.signal, "SIGKILL", 9, create=True),
        ):
            killer.add(proc)  # type: ignore[arg-type]
        self.assertEqual(killer.killed, set(), "the watchdog recorded an unconfirmed tree as killed")
        self.assertIn(1, killer.failed)

    # r18 sol r17 A1: an interrupt while the Windows identity is restored must never leave a child accepted on its pid alone.
    class _FakeHandle(int):
        """Stands in for ``subprocess.Handle``, so the Windows branch is exercised on any host."""

    def _interrupted_restore(self, *, always: bool) -> tuple[object, object]:
        armed = [False]
        fired = [0]

        class CutShort(subprocess.Popen):  # type: ignore[type-arg]
            @property
            def _child_created(self) -> bool:  # type: ignore[override]
                return bool(self.__dict__.get("created", False))

            @_child_created.setter
            def _child_created(self, value: bool) -> None:
                if armed[0] and (always or fired[0] == 0):
                    fired[0] += 1
                    raise KeyboardInterrupt("cut short while the identity was restored")
                self.__dict__["created"] = value

            def _execute_child(self, *args: object, **kwargs: object) -> None:
                hp, ht, pid, tid = 123, 124, 424242, 1  # noqa: F841 - what CreateProcess returned
                armed[0] = True
                raise KeyboardInterrupt("the launch")

        found: object = "unset"
        built: list[object] = []
        try:
            CutShort([sys.executable, "-c", "pass"])
        except KeyboardInterrupt as error:
            with mock.patch.object(shard, "_windows_handle_type", return_value=self._FakeHandle):
                found = shard.half_built_worker(error)
            built.append(error)
        if isinstance(found, subprocess.Popen):
            self.addCleanup(found.__dict__.pop, "created", None)
        return found, built

    def test_a_one_shot_interrupt_while_the_windows_identity_is_restored_still_yields_a_killable_child(self) -> None:
        found, _ = self._interrupted_restore(always=False)
        self.assertIsNotNone(found, "the live child CreateProcess made was dropped")
        self.assertTrue(hasattr(found, "_handle"), "the child was accepted on its pid alone: no direct kill is possible")
        self.assertEqual(found.pid, 424242)  # type: ignore[union-attr]

    def test_a_repeated_interrupt_in_the_windows_identity_restore_never_hands_over_a_pid_only_child(self) -> None:
        found, _ = self._interrupted_restore(always=True)
        self.assertTrue(
            found is None or hasattr(found, "_handle"),
            "a child holding only a pid (no handle) was accepted: its kill raises AttributeError",
        )

    def test_a_stored_pid_without_a_windows_handle_is_not_an_accepted_child(self) -> None:
        class StoredPid(subprocess.Popen):  # type: ignore[type-arg]
            def __init__(self, *args: object, **kwargs: object) -> None:
                self.pid = 424242  # type: ignore[assignment]
                self.returncode = None
                raise KeyboardInterrupt

        found: object = "unset"
        try:
            StoredPid([sys.executable])
        except KeyboardInterrupt as error:
            with (
                mock.patch.object(shard, "_windows_handle_type", return_value=self._FakeHandle),
                mock.patch.object(shard, "_created_child", side_effect=RuntimeError("denied")),
            ):
                found = shard.half_built_worker(error)
        self.assertIsNone(found, "a pid-only child was accepted on Windows")

    # r18 sol r17 A3: a cleanup interrupt that is raised after the run must not swallow what the run recorded.
    def _run_to_exception(
        self, popen: Callable[..., object], workers: int = 2, extra: object | None = None,
        err: io.StringIO | None = None, **patches: object,
    ) -> tuple[BaseException | None, str]:
        root, tests, tag = self._fixture()
        err = io.StringIO() if err is None else err
        raised: BaseException | None = None
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(shard.subprocess, "Popen", side_effect=popen))
            for name, value in patches.items():
                stack.enter_context(mock.patch.object(shard, name, value))
            stack.enter_context(self._log_one_unreadable())
            if extra is not None:
                stack.enter_context(extra)  # type: ignore[arg-type]
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(err))
            try:
                shard.run_parallel(
                    tests, "ubuntu", 1, 1, workers, start_dir=str(root), pattern=f"fxw{tag}_*.py", top_level_dir=str(root),
                    repo_root=REPO_ROOT, pins={}, environ={shard.WORKER_TIMEOUT_ENV: "600"},
                )
            except BaseException as error:  # noqa: BLE001
                raised = error
        for kept in re.findall(r"kept at (.+)$", err.getvalue(), re.MULTILINE):
            self.addCleanup(shutil.rmtree, kept, True)
        return raised, err.getvalue()

    def test_a_killer_stop_interrupt_still_reports_the_unread_log_and_where_it_is_kept(self) -> None:
        class StopInterrupted(shard.DeadlineKiller):
            def stop(self) -> BaseException | None:
                super().stop()
                return KeyboardInterrupt("stop interrupted")

        raised, err = self._run_to_exception(self._finished_popen([]), DeadlineKiller=StopInterrupted)
        self.assertIsInstance(raised, KeyboardInterrupt, repr(raised))
        self.assertIn("log could not be read", err, "the recorded unread-log problem was dropped with the interrupt")
        self.assertIn("kept at", err, "the retained evidence's location was never reported")

    def test_a_budget_watch_stop_interrupt_still_reports_the_unread_log_and_where_it_is_kept(self) -> None:
        class StopInterrupted(shard.BudgetWatch):
            def stop(self) -> BaseException | None:
                super().stop()
                return KeyboardInterrupt("watch stop interrupted")

        raised, err = self._run_to_exception(self._finished_popen([]), BudgetWatch=StopInterrupted)
        self.assertIsInstance(raised, KeyboardInterrupt, repr(raised))
        self.assertIn("log could not be read", err)
        self.assertIn("kept at", err)

    def test_a_census_interrupt_still_reports_a_worker_that_could_not_be_ended_and_its_kept_dir(self) -> None:
        raised, err = self._run_to_exception(
            self._finished_popen([]),
            end_running_workers=lambda procs: ({1: "kill denied"}, KeyboardInterrupt("census")),
        )
        self.assertIsInstance(raised, KeyboardInterrupt, repr(raised))
        self.assertIn("worker 1/2 could not be terminated: kill denied", err)
        self.assertIn("kept at", err, "the location of a log a live worker may still write to was never reported")

    # Sibling: the fallback's own interrupt used to leave the finally before anything was said or the dir was judged.
    def test_an_interrupt_while_the_fallback_reads_a_worker_still_reports_the_problems_and_the_kept_log(self) -> None:
        polls = [0]

        class PollInterrupted(self._RunningProc):  # type: ignore[valid-type, misc]
            def poll(self) -> int | None:
                polls[0] += 1
                if polls[0] == 2:  # 1: the census; 2: the fallback's header
                    raise KeyboardInterrupt("fallback poll")
                return super().poll()

        proc = PollInterrupted()
        calls: list[int] = []

        def popen(*args: object, **kwargs: object) -> object:
            calls.append(1)
            if len(calls) > 1:
                raise OSError("launch failed")
            return proc

        raised, err = self._run_to_exception(popen)
        self.assertIsInstance(raised, KeyboardInterrupt, repr(raised))
        self.assertIn("kept at", err, "an interrupt in the fallback left without reporting the kept log")

    # r18 adversary pass 30 A1: an interrupt met reading, printing or closing a log in the cleanup must not leave it early.
    class _CountingKiller(shard.DeadlineKiller):
        stops: list[int] = []

        def stop(self) -> BaseException | None:
            self.stops.append(1)
            return super().stop()

    def _counting_killer(self) -> type:
        class Killer(self._CountingKiller):
            stops: list[int] = []

        return Killer

    def test_an_interrupt_reading_a_fallback_log_still_reports_every_log_and_stops_the_watchdogs(self) -> None:
        real = Path.read_text

        def read_text(self: Path, *args: object, **kwargs: object) -> str:
            if self.name == "log.txt" and self.parent.name == "w1":
                raise PermissionError(5, "log is locked")  # recorded first, so a later interrupt must not lose it
            if self.name == "log.txt" and self.parent.name == "w2":
                raise KeyboardInterrupt("log read interrupted")
            return real(self, *args, **kwargs)

        killer = self._counting_killer()
        raised, err = self._run_to_exception(
            self._finished_popen([], raise_on=3), workers=3, extra=mock.patch.object(Path, "read_text", read_text),
            DeadlineKiller=killer,
        )
        self.assertIsInstance(raised, KeyboardInterrupt, repr(raised))
        self.assertIn("worker 1/2 log could not be read", err)
        self.assertIn("worker 2/2 log could not be read", err, "the interrupted read left the cleanup before reporting")
        self.assertIn("kept at", err)
        self.assertEqual(killer.stops, [1], "the deadline watchdog was never stopped")

    def test_an_interrupt_printing_a_fallback_log_still_reports_every_log_and_stops_the_watchdogs(self) -> None:
        real_emit = shard.emit_log

        def emit_log(text: str) -> str | None:
            if "fx-boom-2" in text:
                raise KeyboardInterrupt("log print interrupted")
            return real_emit(text)

        killer = self._counting_killer()
        raised, err = self._run_to_exception(
            self._finished_popen([], raise_on=3), workers=3, emit_log=emit_log, DeadlineKiller=killer,
        )
        self.assertIsInstance(raised, KeyboardInterrupt, repr(raised))
        self.assertIn("worker 2/2 log could not be printed", err, "the interrupted print left the cleanup before reporting")
        self.assertIn("kept at", err)
        self.assertEqual(killer.stops, [1], "the deadline watchdog was never stopped")

    def test_an_interrupt_closing_a_log_still_closes_the_rest_reports_and_stops_the_watchdogs(self) -> None:
        real_open = Path.open
        closes: list[str] = []

        class InterruptingLog:
            def __init__(self, real: object, name: str) -> None:
                self._real, self._name = real, name

            def close(self) -> None:
                closes.append(self._name)
                self._real.close()  # type: ignore[attr-defined]
                if self._name == "w1" and closes.count("w1") == 2:  # the cleanup's close, not the run's own
                    raise KeyboardInterrupt("log close interrupted")

            def __getattr__(self, name: str) -> object:
                return getattr(self._real, name)

        def path_open(self: Path, *args: object, **kwargs: object) -> object:
            opened = real_open(self, *args, **kwargs)
            if self.name == "log.txt" and args[:1] == ("wb",):
                return InterruptingLog(opened, self.parent.name)
            return opened

        killer = self._counting_killer()
        raised, err = self._run_to_exception(
            self._finished_popen([]), extra=mock.patch.object(Path, "open", path_open), DeadlineKiller=killer,
        )
        self.assertIsInstance(raised, KeyboardInterrupt, repr(raised))
        self.assertEqual(closes.count("w2"), 2, "a log after the interrupted close was never closed by the cleanup")
        self.assertIn("worker 1/2 log could not be read", err, "the recorded problem was lost to the interrupt")
        self.assertIn("kept at", err)
        self.assertEqual(killer.stops, [1], "the deadline watchdog was never stopped")

    # r18 adversary pass 31 A1: setting a watchdog's stop event is a call an interrupt can land in, like the join after it.
    class _FlakyEvent(threading.Event):
        def __init__(self) -> None:
            super().__init__()
            self.raised = False

        def set(self) -> None:
            super().set()
            if not self.raised:
                self.raised = True
                raise KeyboardInterrupt("stop-set")

    def test_an_interrupt_setting_the_deadline_watchdogs_stop_event_is_returned_not_raised(self) -> None:
        killer = shard.DeadlineKiller(1e9, lambda: 0.0)
        killer._stopped = self._FlakyEvent()  # type: ignore[assignment]
        killer.start()
        returned = killer.stop()
        self.assertIsInstance(returned, KeyboardInterrupt, "the interrupt was raised out of stop()")

    def test_an_interrupt_setting_the_budget_watchs_stop_event_is_returned_not_raised(self) -> None:
        watch = shard.BudgetWatch("ubuntu", 1, 1, 40.0, 0.0, lambda: 0.0, lambda: {})
        watch._stopped = self._FlakyEvent()  # type: ignore[assignment]
        watch.start()
        returned = watch.stop()
        self.assertIsInstance(returned, KeyboardInterrupt, "the interrupt was raised out of stop()")

    def test_a_watchdog_stop_event_interrupt_still_stops_the_other_watchdog_and_reports_the_problems(self) -> None:
        stops: list[int] = []

        class FlakyKiller(shard.DeadlineKiller):
            def __init__(self, *args: object, **kwargs: object) -> None:
                super().__init__(*args, **kwargs)  # type: ignore[arg-type]
                self._stopped = ShardExitCodeTests._FlakyEvent()  # type: ignore[assignment]

        class CountingWatch(shard.BudgetWatch):
            def stop(self) -> BaseException | None:
                stops.append(1)
                return super().stop()

        raised, err = self._run_to_exception(self._finished_popen([]), DeadlineKiller=FlakyKiller, BudgetWatch=CountingWatch)
        self.assertIsInstance(raised, KeyboardInterrupt, repr(raised))
        self.assertEqual(stops, [1], "the budget watch was never stopped after the deadline watchdog's stop was interrupted")
        self.assertIn("log could not be read", err)
        self.assertIn("kept at", err)

    # r18 adversary pass 31 A2: an interrupt met saying one problem must not cost the others (the kept paths) their line.
    class _FlakyStderr(io.StringIO):
        def __init__(self) -> None:
            super().__init__()
            self.raised = False

        def write(self, text: str) -> int:
            if text.startswith("ERROR:") and not self.raised:
                self.raised = True
                raise KeyboardInterrupt("report-write")
            return super().write(text)

    def test_an_interrupt_while_a_problem_is_reported_still_reports_the_others_and_the_kept_path(self) -> None:
        real = Path.read_text

        def read_text(self: Path, *args: object, **kwargs: object) -> str:
            if self.name == "log.txt" and self.parent.name in {"w1", "w2"}:
                raise PermissionError(5, "log is locked")
            return real(self, *args, **kwargs)

        raised, err = self._run_to_exception(
            self._finished_popen([], raise_on=3), workers=3, extra=mock.patch.object(Path, "read_text", read_text),
            err=self._FlakyStderr(),
        )
        self.assertIsInstance(raised, KeyboardInterrupt, repr(raised))
        self.assertIn("worker 2/2 log could not be read", err, "the problem after the interrupted write was never said")
        self.assertIn("kept at", err)

    def test_an_interrupt_while_the_final_problems_are_reported_still_reports_the_rest(self) -> None:
        real = Path.read_text

        def read_text(self: Path, *args: object, **kwargs: object) -> str:
            if self.name == "log.txt":
                raise PermissionError(5, "log is locked")
            return real(self, *args, **kwargs)

        raised, err = self._run_to_exception(
            self._finished_popen([]), extra=mock.patch.object(Path, "read_text", read_text), err=self._FlakyStderr(),
        )
        self.assertIsInstance(raised, KeyboardInterrupt, repr(raised))
        self.assertIn("worker 2/2 log could not be read", err, "the problem after the interrupted write was never said")
        self.assertIn("kept at", err)

    # r20 pass 32 A1: an interrupt met BEFORE the stop flag is set (not after it, as in pass 31) must not leave the watchdog running.
    class _EarlyFlakyEvent(threading.Event):
        def __init__(self, times: int = 1) -> None:
            super().__init__()
            self.left = times

        def set(self) -> None:
            if self.left > 0:
                self.left -= 1
                raise KeyboardInterrupt("stop-set before the flag")
            super().set()

    def _release_on_cleanup(self, watchdog: object) -> None:
        def release() -> None:
            threading.Event.set(watchdog._stopped)  # type: ignore[attr-defined]
            watchdog._thread.join(5)  # type: ignore[attr-defined]

        self.addCleanup(release)

    def test_an_interrupt_before_the_stop_flag_is_set_still_stops_both_watchdog_threads(self) -> None:
        clock = time.monotonic
        for times in (1, 2):
            for make in (
                lambda: shard.BudgetWatch("ubuntu", 1, 1, 40.0, clock(), clock, lambda: {}, poll_seconds=0.05, out=io.StringIO()),
                lambda: shard.DeadlineKiller(clock() + 600, clock, 0.05),
            ):
                watchdog = make()
                watchdog._stopped = self._EarlyFlakyEvent(times)  # type: ignore[assignment]
                watchdog.start()
                self._release_on_cleanup(watchdog)
                with self.subTest(kind=type(watchdog).__name__, interrupts=times):
                    returned = watchdog.stop()
                    self.assertIsInstance(returned, KeyboardInterrupt, "the interrupt met was not handed back")
                    self.assertTrue(watchdog._stopped.is_set(), "stop() returned without ever setting the stop flag")  # type: ignore[attr-defined]
                    self.assertFalse(watchdog._thread.is_alive(), "the watchdog thread is still running after stop()")  # type: ignore[attr-defined]

    def test_run_parallel_leaves_no_watchdog_running_after_an_interrupt_before_a_stop_flag(self) -> None:
        made: list[object] = []

        def early(base: type) -> type:
            class Early(base):  # type: ignore[valid-type, misc]
                def __init__(self, *args: object, **kwargs: object) -> None:
                    super().__init__(*args, **kwargs)
                    self._stopped = ShardExitCodeTests._EarlyFlakyEvent()
                    made.append(self)
                    ShardExitCodeTests._release_on_cleanup(test, self)

            return Early

        test = self
        raised, err = self._run_to_exception(
            self._finished_popen([]), DeadlineKiller=early(shard.DeadlineKiller), BudgetWatch=early(shard.BudgetWatch),
        )
        self.assertIsInstance(raised, KeyboardInterrupt, repr(raised))
        self.assertEqual(len(made), 2)
        for watchdog in made:
            self.assertFalse(watchdog._thread.is_alive(), f"{type(watchdog).__mro__[1].__name__} was left running")  # type: ignore[attr-defined]
        self.assertIn("log could not be read", err)
        self.assertIn("kept at", err)

    def test_run_shard_leaves_no_watchdog_running_after_an_interrupt_before_the_stop_flag(self) -> None:
        clock = FakeClock()
        made: list[object] = []

        class Probe(unittest.TestCase):
            def test_one(self) -> None:
                clock.advance(1.0)

        Probe.__module__ = "fake_mod"
        Probe.__qualname__ = "Probe"

        class Early(shard.BudgetWatch):
            def __init__(self, *args: object, **kwargs: object) -> None:
                super().__init__(*args, **kwargs)
                self._stopped = ShardExitCodeTests._EarlyFlakyEvent()
                made.append(self)

        with (
            mock.patch.object(shard, "BudgetWatch", Early),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(KeyboardInterrupt),
        ):
            try:
                shard.run_shard([Probe("test_one")], "ubuntu", 1, 1, clock=clock, environ={}, stream=io.StringIO())
            finally:
                for watchdog in made:
                    self._release_on_cleanup(watchdog)
        self.assertEqual(len(made), 1)
        self.assertFalse(made[0]._thread.is_alive(), "the serial budget watch was left running")  # type: ignore[attr-defined]

    # r20 pass 32 A2: every step of the final cleanup is guarded, so an interrupt there still says what was recorded.
    def _run_all_launches_refused(self, extra: object) -> tuple[BaseException | None, str]:
        root, tests, tag = self._fixture()
        err = io.StringIO()
        raised: BaseException | None = None
        with contextlib.ExitStack() as stack:
            stack.enter_context(extra)  # type: ignore[arg-type]
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(err))
            try:
                shard.run_parallel(
                    tests, "ubuntu", 1, 1, 2, start_dir=str(root), pattern=f"fxw{tag}_*.py", top_level_dir=str(root),
                    repo_root=REPO_ROOT, pins={}, environ={}, timeout=-1.0,
                )
            except BaseException as error:  # noqa: BLE001
                raised = error
        return raised, err.getvalue()

    def test_an_interrupt_removing_the_work_dir_still_reports_the_recorded_problems(self) -> None:
        real = shutil.rmtree
        removed: list[str] = []

        def rmtree(path: object, *args: object, **kwargs: object) -> None:
            if "mlv-ci-workers-" in str(path):
                removed.append(str(path))
                if len(removed) == 1:
                    raise KeyboardInterrupt("rmtree interrupted")
            real(path, *args, **kwargs)  # type: ignore[arg-type]

        raised, err = self._run_all_launches_refused(mock.patch.object(shard.shutil, "rmtree", rmtree))
        for path in removed:
            self.addCleanup(real, path, True)
        self.assertIsInstance(raised, KeyboardInterrupt, repr(raised))
        self.assertIn("not launched: the deadline passed while launching", err, "the recorded problems were lost to the interrupt")
        self.assertEqual(len(removed), 2, "the removal was not retried after the interrupt")
        self.assertFalse(Path(removed[0]).exists(), "the work dir was left behind")

    def test_an_interrupt_reporting_the_unended_workers_still_reports_the_recorded_problems(self) -> None:
        reads = [0]

        class FlakyFailed(dict):  # type: ignore[type-arg]
            def items(self) -> object:
                reads[0] += 1
                if reads[0] == 2:  # 1: the run's own report; 2: the cleanup's
                    raise KeyboardInterrupt("report_unended interrupted")
                return super().items()

        class Killer(shard.DeadlineKiller):
            def __init__(self, *args: object, **kwargs: object) -> None:
                super().__init__(*args, **kwargs)  # type: ignore[arg-type]
                self.failed = FlakyFailed()

        raised, err = self._run_all_launches_refused(mock.patch.object(shard, "DeadlineKiller", Killer))
        self.assertIsInstance(raised, KeyboardInterrupt, repr(raised))
        self.assertIn("not launched: the deadline passed while launching", err, "the recorded problems were lost to the interrupt")

    # r20 pass 32 A3: on Windows the exit status is a 32-bit word, so 256 is a real failing code there.
    def test_the_zero_exit_guard_keeps_256_where_the_exit_status_is_a_32_bit_word(self) -> None:
        for code in (256, -256, 512, 2**32 + 256):
            def ended() -> int:
                raise SystemExit(code)

            with (
                self.subTest(code=code),
                mock.patch.object(shard, "_EXIT_STATUS_MODULUS", 2**32),
                self.assertRaises(SystemExit) as caught,
            ):
                shard.never_exits_zero(ended)()
            self.assertEqual(caught.exception.code, code)

    def test_the_exit_status_modulus_is_the_platform_s(self) -> None:
        self.assertEqual(shard._EXIT_STATUS_MODULUS, 2**32 if os.name == "nt" else 256)

    # r20 pass 31 advisory: a lone surrogate in a test id must not stop the manifest from being written.
    def test_write_manifest_survives_a_lone_surrogate_in_a_test_id(self) -> None:
        folder = Path(tempfile.mkdtemp(prefix="fxm-"))
        self.addCleanup(shutil.rmtree, folder, True)
        target = folder / "ids.txt"
        shard.write_manifest(target, ["pkg.T.test_ok", "pkg.T.test_\ud800_odd"])
        lines = target.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[1], "pkg.T.test_\\ud800_odd")

    # r16 sol r15 A4: an exit code congruent to zero is a zero exit however large it is.
    def test_the_zero_exit_guard_fails_an_integer_code_that_the_os_reads_as_zero(self) -> None:
        class ModRaises(int):
            def __mod__(self, other: object) -> int:
                raise SystemExit(0)

        for code in (2**32, -(2**32), 2**33, 256, -256, 10**5000, ModRaises(256)):
            def ended() -> int:
                raise SystemExit(code)

            outcome: SystemExit | None = None
            with contextlib.redirect_stderr(io.StringIO()), mock.patch.object(shard, "_EXIT_STATUS_MODULUS", 256):  # the POSIX reading
                try:
                    shard.never_exits_zero(ended)()
                except SystemExit as error:
                    outcome = error
            with self.subTest(code_bits=int.bit_length(code)):
                self.assertIsNotNone(outcome, "the guard let the run return normally")
                final = SystemExit.code.__get__(outcome, SystemExit)
                self.assertEqual(final, 1, "a code that exits as 0 was preserved")

    def test_the_zero_exit_guard_keeps_every_real_failing_code(self) -> None:
        for code in (1, 2, 3, 5, 255, 257, 0xC0000005, -1, True):
            def ended() -> int:
                raise SystemExit(code)

            with self.subTest(code=code), self.assertRaises(SystemExit) as caught:
                shard.never_exits_zero(ended)()
            self.assertEqual(caught.exception.code, code)

    # r13 adversary pass 22 A3: an exit raised where terminate_all starts a thread must not stop the other kills.
    def test_terminate_all_still_ends_every_worker_when_a_thread_start_raises_an_exit(self) -> None:
        procs = [self._RunningProc(), self._RunningProc()]
        real_start = threading.Thread.start
        starts: list[int] = []

        def start(thread: threading.Thread) -> None:
            starts.append(1)
            if len(starts) == 1:
                raise SystemExit(0)
            real_start(thread)

        raised: BaseException | None = None
        with mock.patch.object(shard, "_kill_tree", return_value=None), mock.patch.object(threading.Thread, "start", start):
            try:
                shard.terminate_all(procs)  # type: ignore[arg-type]
            except BaseException as error:  # noqa: BLE001
                raised = error
        self.assertIsNone(raised, "terminate_all let an exit out")
        self.assertEqual([proc.returncode for proc in procs], [-9, -9], "a worker was left running")

    # r15 adversary pass 24 A1: the Thread OBJECT can fail to build (MemoryError), and a join can be interrupted.
    def test_terminate_all_still_ends_every_worker_when_a_thread_cannot_even_be_built(self) -> None:
        procs = [self._RunningProc(), self._RunningProc()]
        real_thread = threading.Thread
        built: list[int] = []

        def thread(*args: object, **kwargs: object) -> threading.Thread:
            built.append(1)
            if len(built) == 1:
                raise MemoryError("thread object allocation failed")
            return real_thread(*args, **kwargs)

        raised: BaseException | None = None
        with mock.patch.object(shard, "_kill_tree", return_value=None), mock.patch.object(shard.threading, "Thread", thread):
            try:
                results = shard.terminate_all(procs)  # type: ignore[arg-type]
            except BaseException as error:  # noqa: BLE001
                raised = error
        self.assertIsNone(raised, f"terminate_all let {raised!r} out")
        self.assertEqual([proc.returncode for proc in procs], [-9, -9], "a worker was left running")
        self.assertEqual(len(results), 2)

    def test_terminate_all_still_attempts_every_worker_when_a_join_is_interrupted(self) -> None:
        procs = [self._RunningProc(), self._RunningProc(), self._RunningProc()]
        real_join = threading.Thread.join
        joins: list[int] = []

        def join(thread: threading.Thread, timeout: float | None = None) -> None:
            joins.append(1)
            if len(joins) == 1:
                raise KeyboardInterrupt
            real_join(thread, timeout)

        raised: BaseException | None = None
        with mock.patch.object(shard, "_kill_tree", return_value=None), mock.patch.object(threading.Thread, "join", join):
            try:
                shard.terminate_all(procs)  # type: ignore[arg-type]
            except BaseException as error:  # noqa: BLE001
                raised = error
        self.assertIsNone(raised, f"terminate_all let {raised!r} out")
        self.assertEqual(len(joins), 3, "a thread after the interrupted join was never waited on")
        self.assertEqual([proc.returncode for proc in procs], [-9, -9, -9], "a worker was left running")

    # r15 adversary pass 25 A1/A2: the launch error's own attribute can lie, and a failing log close must not outrank the adoption.
    def _launch_cut_short(self, patches: list[object], raises: type[BaseException]) -> tuple[list[object], BaseException | None]:
        root, tests, tag = self._fixture()
        made: list[object] = []
        running = self._RunningProc

        class HalfBuilt(running):  # type: ignore[valid-type, misc]
            def __init__(self, *args: object, **kwargs: object) -> None:
                super().__init__()
                self.pid = 424242
                self._handle = 7  # a real Windows ``Popen`` stores its process handle with the pid
                made.append(self)
                raise raises  # the child exists; the constructor never returns it

        out, err = io.StringIO(), io.StringIO()
        raised: BaseException | None = None
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(shard.subprocess, "Popen", HalfBuilt))
            stack.enter_context(mock.patch.object(shard, "_kill_tree", return_value=None))
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            for patch in patches:
                stack.enter_context(patch)  # type: ignore[arg-type]
            try:
                shard.run_parallel(
                    tests, "ubuntu", 1, 1, 2, start_dir=str(root), pattern=f"fxw{tag}_*.py", top_level_dir=str(root),
                    repo_root=REPO_ROOT, pins={}, environ={shard.WORKER_TIMEOUT_ENV: "600"},
                )
            except BaseException as error:  # noqa: BLE001
                raised = error
        for kept in re.findall(r"kept at (.+)$", err.getvalue(), re.MULTILINE):
            self.addCleanup(shutil.rmtree, kept, True)
        return made, raised

    def test_a_launch_error_whose_traceback_attribute_lies_still_hands_the_child_to_the_cleanup(self) -> None:
        class Masked(KeyboardInterrupt):
            __traceback__ = property(lambda self: None)  # type: ignore[assignment]

        made, raised = self._launch_cut_short([], Masked)
        self.assertIsInstance(raised, KeyboardInterrupt, repr(raised))
        self.assertEqual(len(made), 1)
        self.assertEqual(made[0].returncode, -9, "the child the launch created was left running")

    def test_a_log_that_will_not_close_does_not_outrank_ending_the_created_child(self) -> None:
        real_open = Path.open

        class StubbornLog:
            def __init__(self, real: object) -> None:
                self._real, self._tried = real, False

            def close(self) -> None:
                if not self._tried:
                    self._tried = True
                    raise OSError("log close failed")
                self._real.close()  # type: ignore[attr-defined]

            def __getattr__(self, name: str) -> object:
                return getattr(self._real, name)

        def opened(path: Path, *args: object, **kwargs: object) -> object:
            real = real_open(path, *args, **kwargs)  # type: ignore[arg-type]
            if path.name != "log.txt":
                return real
            self.addCleanup(real.close)  # the stub only pretends the close failed; the handle is released here
            return StubbornLog(real)

        made, raised = self._launch_cut_short([mock.patch.object(shard.Path, "open", opened)], KeyboardInterrupt)
        self.assertIsInstance(raised, KeyboardInterrupt, repr(raised))
        self.assertEqual(len(made), 1)
        self.assertEqual(made[0].returncode, -9, "the child the launch created was left running")

    # r15 adversary pass 25 A3: is_alive is one more call an interrupt can land in.
    def test_terminate_all_ends_every_worker_when_is_alive_itself_is_interrupted(self) -> None:
        procs = [self._RunningProc(), self._RunningProc()]

        def start(thread: threading.Thread) -> None:
            raise RuntimeError("cannot start new thread")

        def is_alive(thread: threading.Thread) -> bool:
            raise KeyboardInterrupt

        raised: BaseException | None = None
        with (
            mock.patch.object(shard, "_kill_tree", return_value=None),
            mock.patch.object(threading.Thread, "start", start),
            mock.patch.object(threading.Thread, "is_alive", is_alive),
        ):
            try:
                shard.terminate_all(procs)  # type: ignore[arg-type]
            except BaseException as error:  # noqa: BLE001
                raised = error
        self.assertIsNone(raised, f"terminate_all let {raised!r} out")
        self.assertEqual([proc.returncode for proc in procs], [-9, -9], "a worker was left running")

    def test_the_watch_and_the_killer_stop_even_when_is_alive_is_interrupted(self) -> None:
        clock = time.monotonic
        watch = shard.BudgetWatch("ubuntu", 1, 1, 10.0, clock(), clock, lambda: {}, poll_seconds=0.05, out=io.StringIO())
        killer = shard.DeadlineKiller(clock() + 600, clock, 0.05)
        for stoppable in (watch, killer):
            stoppable.start()
        real_join = threading.Thread.join
        self.addCleanup(lambda: [real_join(thread, 5) for thread in (watch._thread, killer._thread)])

        def is_alive(thread: threading.Thread) -> bool:
            raise KeyboardInterrupt

        raised: BaseException | None = None
        with mock.patch.object(threading.Thread, "is_alive", is_alive):
            for stoppable in (watch, killer):
                try:
                    stoppable.stop()
                except BaseException as error:  # noqa: BLE001
                    raised = error
        self.assertIsNone(raised, f"stop() let {raised!r} out")

    # r15 adversary pass 24 A1 sibling: the watchdog threads' own stop() waits are not a place an interrupt may escape from.
    def test_the_watch_and_the_killer_stop_even_when_the_wait_for_their_thread_is_interrupted(self) -> None:
        clock = time.monotonic
        watch = shard.BudgetWatch("ubuntu", 1, 1, 10.0, clock(), clock, lambda: {}, poll_seconds=0.05, out=io.StringIO())
        killer = shard.DeadlineKiller(clock() + 600, clock, 0.05)
        for stoppable in (watch, killer):
            stoppable.start()
        real_join = threading.Thread.join
        self.addCleanup(lambda: [real_join(thread, 5) for thread in (watch._thread, killer._thread)])

        def join(thread: threading.Thread, timeout: float | None = None) -> None:
            raise KeyboardInterrupt

        raised: BaseException | None = None
        with mock.patch.object(threading.Thread, "join", join):
            for stoppable in (watch, killer):
                try:
                    stoppable.stop()
                except BaseException as error:  # noqa: BLE001
                    raised = error
        self.assertIsNone(raised, f"stop() let {raised!r} out")

    # r11 adversary A1 (pass 19): an interrupt after the worker exists but before it is registered must not leave it running.
    def test_an_interrupt_between_the_launch_and_its_registration_still_ends_the_worker(self) -> None:
        import inspect

        run = inspect.unwrap(shard.run_parallel)
        lines, first = inspect.getsourcelines(run)
        for label, before in (("before the lists", "procs.append((proc, log))"), ("before the pairing", "logs.append(home / \"log.txt\")")):
            with self.subTest(label):
                target = first + next(n for n, text in enumerate(lines) if text.strip() == before)
                root, tests, tag = self._fixture()
                procs = [self._RunningProc(), self._RunningProc()]
                calls: list[int] = []
                fired: list[int] = []

                def popen(*args: object, **kwargs: object) -> object:
                    calls.append(1)
                    return procs[len(calls) - 1]

                def local(frame: object, event: str, arg: object) -> object:
                    if event == "line" and frame.f_lineno == target and not fired:
                        fired.append(1)
                        raise KeyboardInterrupt
                    return local

                def tracer(frame: object, event: str, arg: object) -> object:
                    return local if frame.f_code is run.__code__ else None

                out, err = io.StringIO(), io.StringIO()
                raised: BaseException | None = None
                with (
                    mock.patch.object(shard.subprocess, "Popen", side_effect=popen),
                    contextlib.redirect_stdout(out),
                    contextlib.redirect_stderr(err),
                ):
                    sys.settrace(tracer)
                    try:
                        shard.run_parallel(
                            tests, "ubuntu", 1, 1, 2, start_dir=str(root), pattern=f"fxw{tag}_*.py", top_level_dir=str(root),
                            repo_root=REPO_ROOT, pins={}, environ={shard.WORKER_TIMEOUT_ENV: "600"},
                        )
                    except BaseException as error:  # noqa: BLE001
                        raised = error
                    finally:
                        sys.settrace(None)
                for kept in re.findall(r"kept at (.+)$", err.getvalue(), re.MULTILINE):
                    self.addCleanup(shutil.rmtree, kept, True)
                self.assertIsInstance(raised, KeyboardInterrupt, repr(raised) + err.getvalue())
                self.assertEqual(procs[0].returncode, -9, "the launched worker was left running")

    def test_a_worker_timing_too_large_for_float_is_unreadable_not_an_exception(self) -> None:
        path = Path(tempfile.mkdtemp(prefix="fxw-")) / "result.json"
        self.addCleanup(shutil.rmtree, path.parent, True)
        text = '{"ids": ["m.C.test_a"], "skipped_ids": [], "seconds": {"m.C": 1' + "0" * 309 + '}, "tests_run": 1, "ok": true}'
        path.write_text(text, encoding="utf-8")
        self.assertIsNone(shard._worker_record(path))

    def test_a_worker_result_whose_counts_or_timings_contradict_itself_is_unreadable(self) -> None:
        path = Path(tempfile.mkdtemp(prefix="fxw-")) / "result.json"
        self.addCleanup(shutil.rmtree, path.parent, True)
        good = {"ids": ["m.C.test_a"], "skipped_ids": [], "seconds": {"m.C": 1.5}, "tests_run": 1, "ok": True}
        path.write_text(json.dumps(good), encoding="utf-8")
        self.assertIsNotNone(shard._worker_record(path), "the consistent record is the control")
        for label, changes in (
            ("tests_run below the ids", {"tests_run": 0}),
            ("tests_run above the ids", {"tests_run": 2}),
            ("negative tests_run", {"tests_run": -1}),
            ("a string timing", {"seconds": {"m.C": "slow"}}),
            ("a null timing", {"seconds": {"m.C": None}}),
            ("a boolean timing", {"seconds": {"m.C": True}}),
            ("a negative timing", {"seconds": {"m.C": -1.0}}),
        ):
            with self.subTest(label):
                path.write_text(json.dumps({**good, **changes}), encoding="utf-8")
                self.assertIsNone(shard._worker_record(path))
        path.write_text(json.dumps(good).replace("1.5", "NaN"), encoding="utf-8")
        self.assertIsNone(shard._worker_record(path), "a NaN timing is not a duration")
        path.write_text(json.dumps(good).replace("1.5", "Infinity"), encoding="utf-8")
        self.assertIsNone(shard._worker_record(path), "an infinite timing is not a duration")

    def test_a_worker_result_without_a_boolean_ok_is_unreadable(self) -> None:
        path = Path(tempfile.mkdtemp(prefix="fxw-")) / "result.json"
        self.addCleanup(shutil.rmtree, path.parent, True)
        record = {"ids": [], "skipped_ids": [], "seconds": {}, "tests_run": 0}
        path.write_text(json.dumps(record), encoding="utf-8")
        self.assertIsNone(shard._worker_record(path))
        path.write_text(json.dumps({**record, "ok": True}), encoding="utf-8")
        self.assertIsNotNone(shard._worker_record(path))

    # r3 adversary A2 (pass 2): a budget already crossed when the workers launch is reported before any log.
    def test_a_budget_already_crossed_at_launch_warns_before_the_worker_logs(self) -> None:
        root, tests, tag = self._fixture()
        code, out, err, _manifest = self._parallel(
            root, tests, tag, 3, environ={shard.JOB_START_ENV: str(time.time() - 1700.0)}  # 94 % of the 30 min cap
        )
        self.assertEqual((code, err), (0, ""), out)
        lines = out.splitlines()
        warned = [number for number, line in enumerate(lines) if line.startswith("SHARD_BUDGET_WARN")]
        first_log = next(number for number, line in enumerate(lines) if line.startswith("===== worker 1/"))
        self.assertEqual(len(warned), 1, out)
        self.assertLess(warned[0], first_log, "the warning came only after the workers had finished")

    # r3 adversary A3: a class or module fixture that skips never starts its tests; serial passes, so must parallel.
    def test_a_fixture_level_skip_is_accounted_for_and_the_manifest_matches_serial(self) -> None:
        skipped = (
            "class FxSkipTests(unittest.TestCase):\n"
            "    @classmethod\n"
            "    def setUpClass(cls):\n"
            "        raise unittest.SkipTest('accepted fixture skip')\n"
            "    def test_it(self):\n"
            "        pass"
        )
        root, tests, tag = self._fixture({"c": skipped})
        serial_manifest = root / "manifest-serial.txt"
        with contextlib.redirect_stdout(io.StringIO()):
            serial = shard.run_shard(
                tests, "ubuntu", 1, 1, clock=time.monotonic, environ={}, stream=io.StringIO(), manifest=serial_manifest
            )
        self.assertEqual(serial, 0)
        code, out, err, manifest = self._parallel(root, tests, tag, 3)
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(manifest.read_text(encoding="utf-8"), serial_manifest.read_text(encoding="utf-8"))

    def test_a_module_or_class_fixture_skip_names_only_the_tests_that_never_started(self) -> None:
        clock = FakeClock()
        started, under_class, under_module, other = (_fake_case(clock, name, 0.0) for name in ("A", "B", "C", "D"))
        under_class.__class__.__module__ = "mod_one"
        under_module.__class__.__module__ = "mod_two"
        other.__class__.__module__ = "mod_three"
        started.__class__.__module__ = "mod_four"
        result = shard.ClassTimingResult(io.StringIO(), True, 0, clock=clock)
        result.startTest(started)
        result.addSkip(types.SimpleNamespace(id=lambda: "setUpClass (mod_one.B)"), "class fixture")
        result.addSkip(types.SimpleNamespace(id=lambda: "setUpModule (mod_two)"), "module fixture")
        result.addSkip(started, "an ordinary skip is not a fixture skip")
        members = [started, under_class, under_module, other]
        self.assertEqual(result.skipped_by_fixture(members), [under_class.id(), under_module.id()])

    # r3 adversary A4: the 75 % warning is printed while the workers run, before any cancellation can lose it.
    def test_the_budget_warning_is_printed_while_the_workers_run(self) -> None:
        root, tests, tag = self._fixture({"b": "    def test_slow(self):\n        import time\n        time.sleep(3)"})
        code, out, err, _manifest = self._parallel(  # a 1.2 s cap: 0.9 s is the 75 % mark
            root, tests, tag, 3, environ={shard.CAP_ENV: "0.02"}, poll_seconds=0.05
        )
        self.assertEqual((code, err), (0, ""), out)
        lines = out.splitlines()
        warned = [number for number, line in enumerate(lines) if line.startswith("SHARD_BUDGET_WARN")]
        first_log = next(number for number, line in enumerate(lines) if line.startswith("===== worker 1/"))
        self.assertEqual(len(warned), 1, out)
        self.assertEqual(sum(line.startswith("::warning") for line in lines), 1, out)
        self.assertLess(warned[0], first_log, "the warning came only after the workers had finished")

    # ---- r21, sol r20 A1/A2 (adversary pass 33): an interrupt landing in, or at the entry of, a cleanup step. ----
    # Closed by ONE mechanism, ``run_cleanup``; these tests inject a one-shot KeyboardInterrupt at the FIRST line of a
    # chosen frame through ``sys.settrace`` (the way the sol key reproduced both findings).
    @staticmethod
    @contextlib.contextmanager
    def _traced(tracer: Callable[..., object]) -> Iterator[None]:
        sys.settrace(tracer)  # type: ignore[arg-type]
        try:
            yield
        finally:
            sys.settrace(None)

    def _interrupt_at_entry(self, pick: Callable[[types.FrameType], bool], marker: str = "step-entry") -> object:
        """At the first line of the first frame ``pick`` accepts, raise ``KeyboardInterrupt(marker)``, once."""
        fired: list[int] = []

        def local(frame: types.FrameType, event: str, arg: object) -> object:
            if event == "line" and not fired:
                fired.append(1)
                raise KeyboardInterrupt(marker)
            return None

        def tracer(frame: types.FrameType, event: str, arg: object) -> object:
            if event == "call" and not fired and pick(frame):
                return local
            return None

        return self._traced(tracer)

    def _recorded_watchdogs(self) -> tuple[list[object], dict[str, type]]:
        made: list[object] = []
        test = self

        def record(base: type) -> type:
            class Recorded(base):  # type: ignore[valid-type, misc]
                def __init__(self, *args: object, **kwargs: object) -> None:
                    super().__init__(*args, **kwargs)
                    made.append(self)
                    ShardExitCodeTests._release_on_cleanup(test, self)

            return Recorded

        return made, {"DeadlineKiller": record(shard.DeadlineKiller), "BudgetWatch": record(shard.BudgetWatch)}

    def _assert_watchdogs_stopped(self, made: list[object], count: int) -> None:
        self.assertEqual(len(made), count)
        for watchdog in made:
            name = type(watchdog).__mro__[1].__name__
            self.assertTrue(watchdog._stopped.is_set(), f"{name}'s stop flag was never set")  # type: ignore[attr-defined]
            self.assertFalse(watchdog._thread.is_alive(), f"{name} was left running")  # type: ignore[attr-defined]

    def _parallel_with(self, extra: object | None = None, **patches: object) -> tuple[BaseException | None, str, list[object]]:
        """``run_parallel`` over two finished workers, worker 1's log unreadable (so its dir is kept and the kept path
        is the evidence), with the watchdogs recorded."""
        made, watchdogs = self._recorded_watchdogs()
        raised, err = self._run_to_exception(self._finished_popen([]), extra=extra, **{**watchdogs, **patches})
        return raised, err, made

    def _serial_with(self, extra: object) -> tuple[BaseException | None, str, list[object]]:
        """``run_shard`` over one passing test, with its budget watch recorded."""
        clock = FakeClock()

        class Probe(unittest.TestCase):
            def test_one(self) -> None:
                clock.advance(1.0)

        Probe.__module__ = "fake_mod"
        Probe.__qualname__ = "Probe"
        made, watchdogs = self._recorded_watchdogs()
        out = io.StringIO()
        raised: BaseException | None = None
        with (
            mock.patch.object(shard, "BudgetWatch", watchdogs["BudgetWatch"]),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(io.StringIO()),
            extra,  # type: ignore[attr-defined]
        ):
            try:
                shard.run_shard([Probe("test_one")], "ubuntu", 1, 1, clock=clock, environ={}, stream=io.StringIO())
            except BaseException as error:  # noqa: BLE001
                raised = error
        return raised, out.getvalue(), made

    def _assert_parallel_reported(self, raised: BaseException | None, err: str, made: list[object], marker: str) -> None:
        self.assertIsInstance(raised, KeyboardInterrupt, repr(raised) + err)
        self.assertEqual(str(raised), marker)
        self._assert_watchdogs_stopped(made, 2)
        self.assertIn("worker 1/2 log could not be read", err, "the recorded problem was never said")
        self.assertIn("kept at", err, "the location of the retained evidence was never said")

    def test_r21_sol_a1_an_interrupted_write_of_the_sole_kept_path_is_written_again(self) -> None:
        raised, err, made = self._parallel_with(err=self._FlakyStderr())
        self._assert_parallel_reported(raised, err, made, "report-write")

    def test_r21_sol_a2_an_interrupt_at_either_watchdog_stops_entry_still_stops_both_and_reports(self) -> None:
        for nth in (1, 2):  # the deadline watchdog's stop, then the budget watch's
            with self.subTest(stop=nth):
                calls = [0]

                def pick(frame: types.FrameType, nth: int = nth) -> bool:
                    if frame.f_code is shard.stop_thread.__code__:
                        calls[0] += 1
                        return calls[0] == nth
                    return False

                raised, err, made = self._parallel_with(self._interrupt_at_entry(pick))
                self._assert_parallel_reported(raised, err, made, "step-entry")

    def test_r21_sol_a2_an_interrupt_at_the_entry_of_recording_the_unended_workers_still_stops_and_reports(self) -> None:
        driver = getattr(getattr(shard, "run_cleanup", None), "__code__", None)  # None before r21 (no driver)

        def pick(frame: types.FrameType) -> bool:
            code, back = frame.f_code, frame.f_back
            if code is shard.attempt.__code__:  # e25b06df: ``attempt(report_unended)``
                return getattr(frame.f_locals.get("step"), "__name__", "") == "report_unended"
            return code.co_name == "report_unended" and back is not None and driver is not None and back.f_code is driver

        raised, err, made = self._parallel_with(self._interrupt_at_entry(pick))
        self._assert_parallel_reported(raised, err, made, "step-entry")

    def test_r21_sol_a2_an_interrupt_at_the_report_entry_still_says_every_problem(self) -> None:
        raised, err, made = self._parallel_with(
            self._interrupt_at_entry(lambda frame: frame.f_code is shard.report_problems.__code__)
        )
        self._assert_parallel_reported(raised, err, made, "step-entry")

    def test_r21_sol_a2_the_serial_path_stops_its_watch_and_prints_the_slowest_after_an_interrupt_at_the_stop(self) -> None:
        raised, out, made = self._serial_with(self._interrupt_at_entry(lambda frame: frame.f_code is shard.stop_thread.__code__))
        self.assertIsInstance(raised, KeyboardInterrupt, repr(raised))
        self.assertEqual(str(raised), "step-entry")
        self._assert_watchdogs_stopped(made, 1)
        self.assertIn("SHARD_SLOWEST", out, "the serial report was skipped by the interrupt")

    def _driver_steps(self, run: Callable[[object], tuple[BaseException | None, str, list[object]]]) -> list[str]:
        """The names of the steps the cleanup driver ran in a clean run, read from the driver's own frame, and proven
        to be exactly the calls the driver made itself (one per step), so injecting at the k-th such call is step k."""
        driver = shard.run_cleanup.__code__
        names: list[str] = []
        direct = [0]

        def tracer(frame: types.FrameType, event: str, arg: object) -> object:
            if event == "call":
                if frame.f_code is driver:
                    names.extend(name for name, _step in frame.f_locals["steps"])
                elif frame.f_back is not None and frame.f_back.f_code is driver:
                    direct[0] += 1
            return None

        raised, _text, _made = run(self._traced(tracer))
        self.assertIsNone(raised, "the clean control run raised")
        self.assertGreater(len(names), 0, "the cleanup was not run by the driver")
        self.assertEqual(direct[0], len(names), "the driver made a call that is not one of its steps")
        return names

    def _at_driver_step(self, k: int) -> object:
        driver = shard.run_cleanup.__code__
        seen = [0]

        def pick(frame: types.FrameType) -> bool:
            if frame.f_back is not None and frame.f_back.f_code is driver:
                seen[0] += 1
                return seen[0] == k + 1
            return False

        return self._interrupt_at_entry(pick)

    def test_r21_an_interrupt_at_the_entry_of_every_parallel_cleanup_step_skips_no_later_step_or_report(self) -> None:
        names = self._driver_steps(self._parallel_with)  # the control: two finished workers that exit 1 return, never raise
        self.assertEqual(names[-1], "report the problems", "the report is not the driver's last step")
        for k, name in enumerate(names):
            with self.subTest(step=name):
                raised, err, made = self._parallel_with(self._at_driver_step(k))
                self._assert_parallel_reported(raised, err, made, "step-entry")

    def test_r21_an_interrupt_at_the_entry_of_every_serial_cleanup_step_skips_no_later_step_or_report(self) -> None:
        names = self._driver_steps(self._serial_with)
        for k, name in enumerate(names):
            with self.subTest(step=name):
                raised, out, made = self._serial_with(self._at_driver_step(k))
                self.assertIsInstance(raised, KeyboardInterrupt, repr(raised))
                self.assertEqual(str(raised), "step-entry")
                self._assert_watchdogs_stopped(made, 1)
                self.assertIn("SHARD_SLOWEST", out, f"an interrupt at {name!r} skipped the serial report")

    def test_r21_run_cleanup_runs_every_step_and_raises_the_first_exception_after_the_last(self) -> None:
        ran: list[str] = []
        tries = {"b": 0}

        def a() -> None:
            ran.append("a")
            raise KeyboardInterrupt("first")

        def b() -> None:
            tries["b"] += 1
            ran.append("b")
            if tries["b"] < 3:
                raise OSError("again")

        def c() -> BaseException:
            ran.append("c")
            return KeyboardInterrupt("returned")

        met: list[BaseException] = []
        with self.assertRaises(KeyboardInterrupt) as caught:
            shard.run_cleanup((("a", a), ("b", b), ("c", c), ("d", lambda: ran.append("d"))), met)
        self.assertEqual(str(caught.exception), "first")
        self.assertEqual(ran, ["a"] * shard.CLEANUP_ATTEMPTS + ["b", "b", "b", "c", "d"], "a step was skipped or not made again")
        self.assertEqual([str(error) for error in met], ["first"] * shard.CLEANUP_ATTEMPTS + ["again", "again", "returned"])
        shard.run_cleanup((("quiet", lambda: None),))  # nothing met: nothing raised

    # ---- r22, adversary pass 34 A1: a once-only marker (``stuck``, ``unread``) set before the problem it guards. ----
    # The interrupt lands BETWEEN the two statements of the guarded block, whichever order they are in, so the same
    # test is RED on 53c1491d (marker first: the problem is lost for good) and GREEN once the problem comes first.
    def _interrupt_inside_block(self, function: str, guard: str, marker: str = "record-gap") -> object:
        """In the first frame of ``shard``'s ``function``, once the block under the line starting with ``guard`` has
        run its first statement, raise ``KeyboardInterrupt(marker)`` before its second, once."""
        state = [0]  # 0: looking for the guard; 1: the guard ran; 2: the block's first statement is running

        def local(frame: types.FrameType, event: str, arg: object) -> object:
            if state[0] == 3 or event != "line":
                return None if state[0] == 3 else local
            line = linecache.getline(frame.f_code.co_filename, frame.f_lineno)
            text, indent = line.strip(), len(line) - len(line.lstrip())
            if state[0] == 0 and text.startswith(guard):
                state[:] = [1, indent]  # type: ignore[list-item]
            elif state[0] == 1:
                state[0] = 2 if indent > state[1] else 0  # a guard that was False is not the block
            elif state[0] == 2:
                state[0] = 3
                raise KeyboardInterrupt(marker)
            return local

        def tracer(frame: types.FrameType, event: str, arg: object) -> object:
            code = frame.f_code
            if event == "call" and state[0] == 0 and code.co_name == function and code.co_filename == shard.__file__:
                return local
            return None

        return self._traced(tracer)

    @staticmethod
    def _running_popen() -> Callable[..., object]:
        class Running:  # never seen to exit: the body's own kill (``terminate_all``) is the one that is refused
            returncode = 1

            def poll(self) -> None:
                return None

            def wait(self, timeout: float | None = None) -> int:
                return 1

            def kill(self) -> None:
                pass

        def popen(*args: object, **kwargs: object) -> object:
            kwargs["stdout"].write(b"fx-running\n")
            kwargs["stdout"].flush()
            return Running()

        return popen

    def test_r22_adv34_a1_an_interrupt_between_a_problem_and_its_marker_never_loses_the_problem(self) -> None:
        class FailedKiller(shard.DeadlineKiller):
            def __init__(self, *args: object, **kwargs: object) -> None:
                super().__init__(*args, **kwargs)
                self.failed[1] = "tree kill denied"

        cases = (  # (where, popen, patches, what must be said)
            (
                "print_left_logs", "if unreadable is not None and number not in unread",
                self._finished_popen([], raise_on=2), {}, "worker 1/1 log could not be read",
            ),
            (
                "run_parallel", "if unreadable is not None:  # the other workers' logs",
                self._finished_popen([]), {}, "worker 1/2 log could not be read",
            ),
            (
                "close_logs", "if error is not None and number not in stuck",
                self._finished_popen([]), {"end_running_workers": lambda procs: ({1: "kill denied"}, None)},
                "worker 1/2 could not be terminated: kill denied",
            ),
            (
                "report_unended", "if number not in stuck",
                self._finished_popen([]), {"DeadlineKiller": FailedKiller},
                "worker 1/2 could not be terminated: tree kill denied",
            ),
            (
                "run_parallel", "if error is not None:  # the other workers were still killed",
                self._running_popen(),
                {
                    "terminate_all": lambda procs, **_kw: ["kill denied"] * len(procs),
                    "late_workers": lambda procs, deadline, monotonic=None: set(),
                    "end_running_workers": lambda procs: ({}, None),
                },
                "worker 1/2 could not be terminated: kill denied",
            ),
        )
        for function, guard, popen, patches, said in cases:
            with self.subTest(at=f"{function}: {guard}"):
                raised, err = self._run_to_exception(popen, extra=self._interrupt_inside_block(function, guard), **patches)
                self.assertIsInstance(raised, KeyboardInterrupt, repr(raised) + err)
                self.assertEqual(str(raised), "record-gap", "the injected interrupt never landed in the block")
                self.assertIn(said, err, "the problem was lost with its marker already set")
                self.assertIn("kept at", err, "the work dir was kept without its location being said")
                self.assertEqual(err.count(said), 1, "a problem made again was said twice")

    def test_r22_a_work_dir_a_problem_says_is_kept_is_never_removed(self) -> None:
        # The class guard (``keeping``): worker 1's log is locked once, the body records it and is interrupted before
        # its marker, and the cleanup's second read then succeeds. The problem already says "kept at": the dir stays.
        real = Path.read_text
        reads = [0]

        def read_text(path: Path, *args: object, **kwargs: object) -> str:
            if path.name == "log.txt" and path.parent.name == "w1":
                reads[0] += 1
                if reads[0] == 1:
                    raise PermissionError(5, "log is locked once")
            return real(path, *args, **kwargs)

        removed: list[object] = []

        @contextlib.contextmanager
        def scene() -> Iterator[None]:
            with (
                mock.patch.object(Path, "read_text", read_text),
                mock.patch.object(shard.shutil, "rmtree", side_effect=lambda path, *a, **k: removed.append(path)),
                self._interrupt_inside_block("run_parallel", "if unreadable is not None:  # the other workers' logs"),
            ):
                yield

        raised, err = self._run_to_exception(self._finished_popen([]), extra=scene())
        self.assertIsInstance(raised, KeyboardInterrupt, repr(raised) + err)
        self.assertEqual(str(raised), "record-gap")
        self.assertEqual(reads[0], 2, "the cleanup did not read the log again")
        self.assertIn("worker 1/2 log could not be read (", err)
        self.assertIn("kept at", err)
        self.assertEqual(removed, [], "a work dir a problem says is kept was removed")

    # ---- r22, adversary pass 34 advisories in scope: A2 (serial watch started outside the try), A3 (attempts=0). ----
    def test_r22_adv34_a2_an_interrupt_right_after_the_serial_watch_starts_still_stops_it(self) -> None:
        original = shard.BudgetWatch.start

        def start(self: object) -> None:
            original(self)  # type: ignore[arg-type]
            raise KeyboardInterrupt("after-start")

        raised, _out, made = self._serial_with(mock.patch.object(shard.BudgetWatch, "start", start))
        self.assertIsInstance(raised, KeyboardInterrupt, repr(raised))
        self.assertEqual(str(raised), "after-start")
        self._assert_watchdogs_stopped(made, 1)

    def test_r22_adv34_a3_no_attempts_count_skips_a_cleanup_step_or_a_problem(self) -> None:
        ran: list[str] = []
        shard.run_cleanup((("one", lambda: ran.append("one")), ("two", lambda: ran.append("two"))), attempts=0)
        self.assertEqual(ran, ["one", "two"], "a cleanup step was skipped")
        self.assertIsNone(shard.attempt(lambda: ran.append("three"), attempts=0))
        self.assertEqual(ran[-1], "three", "attempt skipped its step")
        err, cursor = io.StringIO(), [0]
        with contextlib.redirect_stderr(err):
            self.assertIsNone(shard.report_problems(["kept at C:\\evidence"], cursor, attempts=-1))
        self.assertIn("ERROR: kept at C:\\evidence", err.getvalue(), "the problem was skipped")
        self.assertEqual(cursor, [1])


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
            {"tools.repo_hygiene.test_x.AlphaTests": 4.5, "tools.repo_hygiene.test_x.BetaTests": 0.5},
        )

    def test_the_time_before_a_classs_first_docstring_test_is_its_own(self) -> None:
        # adversary r2 A1 on CI-WINDOWS-UNITTEST-SHARD-HEADROOM-2: unittest -v prints a docstring
        # test's id line when the test STARTS, so the gap from the previous stamped line to it is
        # the class's setUpClass (187.5 s for CorePartitionGeneratorTests, run 42021571373: the
        # class weighed 128.2 s here and 316.2 s in the job's own SHARD_SLOWEST). That gap is
        # charged to the class, as for a plain one-line test.
        log = (
            "2026-10-04T00:00:00.0000000Z test_z (tools.repo_hygiene.test_x.PriorTests.test_z) ... ok\n"
            "2026-10-04T00:03:08.0000000Z test_a (tools.repo_hygiene.test_x.FixtureTests.test_a)\n"
            "2026-10-04T00:03:29.0000000Z the docstring first line ... ok\n"
            "2026-10-04T00:03:30.0000000Z test_b (tools.repo_hygiene.test_x.FixtureTests.test_b)\n"
            "2026-10-04T00:03:31.0000000Z another docstring ... ok\n"
        )
        self.assertEqual(timings.class_seconds(log), {"tools.repo_hygiene.test_x.FixtureTests": 211.0})

    def test_the_tools_own_slowest_figure_replaces_the_gap_estimate(self) -> None:
        # adversary r2 A1 sibling: lines a class logs without a result hide its time from the gaps
        # (CandidateAcceptanceTests: 8.8 s here, 62.7 s in its job's SHARD_SLOWEST). The tool's own
        # per-class figure is exact, so it replaces the gap estimate in both directions (r3: a floor
        # kept a gap that charged a neighbour's teardown to the class, see the next test).
        log = (
            "2026-10-04T00:00:00.0000000Z started\n"
            "2026-10-04T00:00:09.0000000Z test_a (tools.repo_hygiene.test_x.Quiet.test_a) ... ok\n"
            "2026-10-04T00:00:10.0000000Z test_b (tools.repo_hygiene.test_x.Loud.test_b) ... ok\n"
            "2026-10-04T00:00:11.0000000Z OK\n"
            "2026-10-04T00:00:11.0000000Z SHARD_SLOWEST profile=windows shard=1/7 total=70.0s\n"
            "2026-10-04T00:00:11.0000000Z   62.7s tools.repo_hygiene.test_x.Loud\n"
            "2026-10-04T00:00:11.0000000Z   3.0s tools.repo_hygiene.test_x.Quiet\n"
            "2026-10-04T00:00:11.0000000Z   2.0s somewhere.else.Tests\n"
            "2026-10-04T00:00:12.0000000Z the block ended\n"
            "2026-10-04T00:00:12.0000000Z   99.0s tools.repo_hygiene.test_x.NotInTheBlock\n"
        )
        self.assertEqual(
            timings.class_seconds(log),
            {"tools.repo_hygiene.test_x.Quiet": 3.0, "tools.repo_hygiene.test_x.Loud": 62.7},
        )

    def test_a_classs_teardown_is_not_charged_to_the_next_docstring_class(self) -> None:
        # adversary r2 pass 3 A1 on CI-WINDOWS-UNITTEST-SHARD-HEADROOM-2: A = a 1 s test then a 100 s
        # tearDownClass (silent), B = a 1 s docstring test. The gap estimate gives B the 100 s as its
        # setUpClass (B = 101.0, A's teardown counted twice); both classes are in the job's own
        # SHARD_SLOWEST, whose figure is exact: A = 101.0, B = 1.0.
        log = (
            "2026-10-04T00:00:00.0000000Z started\n"
            "2026-10-04T00:00:01.0000000Z test_a (tools.repo_hygiene.test_x.A.test_a) ... ok\n"
            "2026-10-04T00:01:41.0000000Z test_b (tools.repo_hygiene.test_x.B.test_b)\n"
            "2026-10-04T00:01:42.0000000Z the docstring first line ... ok\n"
            "2026-10-04T00:01:42.0000000Z OK\n"
            "2026-10-04T00:01:42.0000000Z SHARD_SLOWEST profile=windows shard=1/7 total=102.0s\n"
            "2026-10-04T00:01:42.0000000Z  101.0s tools.repo_hygiene.test_x.A\n"
            "2026-10-04T00:01:42.0000000Z    1.0s tools.repo_hygiene.test_x.B\n"
        )
        self.assertEqual(
            timings.class_seconds(log),
            {"tools.repo_hygiene.test_x.A": 101.0, "tools.repo_hygiene.test_x.B": 1.0},
        )

    def test_an_unnamed_class_is_capped_at_the_slowest_blocks_smallest_figure(self) -> None:
        # adversary r3 pass 4 A1 on CI-WINDOWS-UNITTEST-SHARD-HEADROOM-2: A = a 1 s test then a silent
        # 100 s tearDownClass, B = a 1 s docstring test that the block omits (nine C classes of 2 s
        # fill the top ten with A). B's gap estimate is 101.0 (A's teardown charged again as B's
        # setUpClass), yet a class outside the top ten cannot have run longer than the block's
        # smallest figure (2.0 s), so that is B's ceiling and the job no longer sums to 220 s.
        lines = [
            "2026-10-04T00:00:00.0000000Z started",
            "2026-10-04T00:00:01.0000000Z test_a (tools.repo_hygiene.test_x.A.test_a) ... ok",
            "2026-10-04T00:01:41.0000000Z test_b (tools.repo_hygiene.test_x.B.test_b)",
            "2026-10-04T00:01:42.0000000Z the docstring first line ... ok",
        ]
        for i in range(9):
            at = 104 + 2 * i
            lines.append(f"2026-10-04T00:{at // 60:02d}:{at % 60:02d}.0000000Z test_c (tools.repo_hygiene.test_x.C{i}.test_c) ... ok")
        lines.append("2026-10-04T00:02:00.0000000Z SHARD_SLOWEST profile=windows shard=1/7 total=120.0s")
        lines.append("2026-10-04T00:02:00.0000000Z  101.0s tools.repo_hygiene.test_x.A")
        lines.extend(f"2026-10-04T00:02:00.0000000Z    2.0s tools.repo_hygiene.test_x.C{i}" for i in range(9))
        got = timings.class_seconds("\n".join(lines) + "\n")
        self.assertEqual(got["tools.repo_hygiene.test_x.B"], 2.0)
        self.assertEqual(got["tools.repo_hygiene.test_x.A"], 101.0)
        self.assertLessEqual(sum(got.values()), 101.0 + 2.0 * 10)

    def test_a_log_with_two_slowest_blocks_is_read_by_its_gaps_alone(self) -> None:
        # adversary r3 pass 5 A1: the sums are per job log and a job log holds one SHARD_SLOWEST block (all
        # 130 hosted windows logs do). Two blocks mean concatenated or foreign output, whose figures
        # are neither this job's nor each other's, so none is used: the last block's A = 1.0 must not
        # replace the 101.0 the gaps accumulated.
        log = (
            "2026-10-04T00:00:00.0000000Z started\n"
            "2026-10-04T00:01:40.0000000Z test_a (tools.repo_hygiene.test_x.A.test_a) ... ok\n"
            "2026-10-04T00:01:40.0000000Z SHARD_SLOWEST profile=windows shard=1/7 total=100.0s\n"
            "2026-10-04T00:01:40.0000000Z   100.0s tools.repo_hygiene.test_x.A\n"
            "2026-10-04T00:01:40.0000000Z started\n"
            "2026-10-04T00:01:41.0000000Z test_a (tools.repo_hygiene.test_x.A.test_a) ... ok\n"
            "2026-10-04T00:01:41.0000000Z SHARD_SLOWEST profile=windows shard=1/7 total=1.0s\n"
            "2026-10-04T00:01:41.0000000Z   1.0s tools.repo_hygiene.test_x.A\n"
        )
        self.assertEqual(timings.class_seconds(log), {"tools.repo_hygiene.test_x.A": 101.0})

    def test_an_empty_slowest_block_gives_another_block_no_ceiling_over_its_classes(self) -> None:
        # adversary r3 pass 5 A2: the second block is empty, so the first block's 1.0 s minimum must
        # not cap B (100 s by the gaps). Two blocks: no SHARD_SLOWEST figure is used at all.
        log = (
            "2026-10-04T00:00:00.0000000Z started\n"
            "2026-10-04T00:00:01.0000000Z test_a (tools.repo_hygiene.test_x.A.test_a) ... ok\n"
            "2026-10-04T00:00:01.0000000Z SHARD_SLOWEST profile=windows shard=1/7 total=1.0s\n"
            "2026-10-04T00:00:01.0000000Z   1.0s tools.repo_hygiene.test_x.A\n"
            "2026-10-04T00:00:01.0000000Z started\n"
            "2026-10-04T00:01:41.0000000Z test_b (tools.repo_hygiene.test_x.B.test_b) ... ok\n"
            "2026-10-04T00:01:41.0000000Z SHARD_SLOWEST profile=windows shard=2/7 total=100.0s\n"
        )
        self.assertEqual(
            timings.class_seconds(log),
            {"tools.repo_hygiene.test_x.A": 1.0, "tools.repo_hygiene.test_x.B": 100.0},
        )

    def test_unittests_stderr_summary_between_the_header_and_the_rows_does_not_end_the_block(self) -> None:
        # adversary r3 pass 6 A1: hosted run 41960424018, heavy part 4: the stderr summary lines reach
        # the log between the stdout header and its rows (blank line, "OK"). The block is still read.
        log = (
            "2026-10-04T00:00:00.0000000Z started\n"
            "2026-10-04T00:00:05.0000000Z test_a (tools.repo_hygiene.test_x.A.test_a) ... ok\n"
            "2026-10-04T00:00:05.0000000Z Ran 1 test in 5.000s\n"
            "2026-10-04T00:00:05.0000000Z SHARD_SLOWEST profile=windows shard=4/6 total=5.0s\n"
            "2026-10-04T00:00:05.0000000Z \n"
            "2026-10-04T00:00:05.0000000Z OK\n"
            "2026-10-04T00:00:05.0000000Z   9.0s tools.repo_hygiene.test_x.A\n"
            "2026-10-04T00:00:05.0000000Z OK (skipped=1)\n"
            "2026-10-04T00:00:05.0000000Z   2.0s somewhere.else.Tests\n"
            "2026-10-04T00:00:06.0000000Z Post job cleanup.\n"
        )
        self.assertEqual(timings.class_seconds(log), {"tools.repo_hygiene.test_x.A": 9.0})

    def test_unittests_ran_line_and_dash_separator_inside_the_block_do_not_end_it(self) -> None:
        # adversary r4 A1: the rest of unittest's stderr summary (the 70-dash separator and the
        # "Ran N tests in Xs" line) can sit between the header and the rows too. The block is read.
        log = (
            "2026-10-04T00:00:00.0000000Z started\n"
            "2026-10-04T00:00:05.0000000Z test_a (tools.repo_hygiene.test_x.A.test_a) ... ok\n"
            "2026-10-04T00:00:09.0000000Z SHARD_SLOWEST profile=windows shard=1/7 total=9.0s\n"
            "2026-10-04T00:00:09.0000000Z Ran 1 test in 9.000s\n"
            "2026-10-04T00:00:09.0000000Z \n"
            "2026-10-04T00:00:09.0000000Z OK\n"
            "2026-10-04T00:00:09.0000000Z    9.0s tools.repo_hygiene.test_x.A\n"
        )
        self.assertEqual(timings.class_seconds(log), {"tools.repo_hygiene.test_x.A": 9.0})
        dashed = log.replace(
            "Ran 1 test in 9.000s\n", "----------------------------------------------------------------------\n"
            "2026-10-04T00:00:09.0000000Z Ran 1 test in 9.000s\n"
        )
        self.assertEqual(timings.class_seconds(dashed), {"tools.repo_hygiene.test_x.A": 9.0})
        # ... and a class named twice across such a line still makes the block unusable (gap-only 5.0).
        twice = log + (
            "2026-10-04T00:00:09.0000000Z Ran 2 tests in 9.000s\n"
            "2026-10-04T00:00:09.0000000Z    1.0s tools.repo_hygiene.test_x.A\n"
        )
        self.assertEqual(timings.class_seconds(twice), {"tools.repo_hygiene.test_x.A": 5.0})

    def test_a_malformed_row_between_rows_neither_ends_the_block_nor_hides_a_repeated_class(self) -> None:
        # adversary r3 pass 6 A1: "1.2.3s" is not a measurement but it is a row, so the scan goes on
        # and sees class A named twice, which makes the block unusable (gap-only A = 5.0).
        log = (
            "2026-10-04T00:00:00.0000000Z started\n"
            "2026-10-04T00:00:05.0000000Z test_a (tools.repo_hygiene.test_x.A.test_a) ... ok\n"
            "2026-10-04T00:00:05.0000000Z SHARD_SLOWEST profile=windows shard=1/7 total=5.0s\n"
            "2026-10-04T00:00:05.0000000Z   9.0s tools.repo_hygiene.test_x.A\n"
            "2026-10-04T00:00:05.0000000Z   1.2.3s ignored.Row\n"
            "2026-10-04T00:00:05.0000000Z   1.0s tools.repo_hygiene.test_x.A\n"
        )
        self.assertEqual(timings.class_seconds(log), {"tools.repo_hygiene.test_x.A": 5.0})

    def test_a_class_with_an_unreadable_row_is_not_capped_at_the_blocks_smallest_figure(self) -> None:
        # The unreadable row names a top-ten class, which a ceiling for classes OUTSIDE the top ten
        # must not lower: its gap estimate (50 s) stands, the readable row of C is exact.
        log = (
            "2026-10-04T00:00:00.0000000Z started\n"
            "2026-10-04T00:00:50.0000000Z test_u (tools.repo_hygiene.test_x.U.test_u) ... ok\n"
            "2026-10-04T00:00:52.0000000Z test_c (tools.repo_hygiene.test_x.C.test_c) ... ok\n"
            "2026-10-04T00:00:52.0000000Z SHARD_SLOWEST profile=windows shard=1/7 total=52.0s\n"
            "2026-10-04T00:00:52.0000000Z   2.0s tools.repo_hygiene.test_x.C\n"
            "2026-10-04T00:00:52.0000000Z   1.2.3s tools.repo_hygiene.test_x.U\n"
        )
        self.assertEqual(
            timings.class_seconds(log),
            {"tools.repo_hygiene.test_x.U": 50.0, "tools.repo_hygiene.test_x.C": 2.0},
        )

    def test_a_class_named_twice_in_the_slowest_block_makes_the_block_unusable(self) -> None:
        log = (
            "2026-10-04T00:00:00.0000000Z started\n"
            "2026-10-04T00:00:05.0000000Z test_a (tools.repo_hygiene.test_x.A.test_a) ... ok\n"
            "2026-10-04T00:00:05.0000000Z SHARD_SLOWEST profile=windows shard=1/7 total=5.0s\n"
            "2026-10-04T00:00:05.0000000Z   9.0s tools.repo_hygiene.test_x.A\n"
            "2026-10-04T00:00:05.0000000Z   1.0s tools.repo_hygiene.test_x.A\n"
        )
        self.assertEqual(timings.class_seconds(log), {"tools.repo_hygiene.test_x.A": 5.0})

    def test_an_unnamed_class_below_the_ceiling_keeps_its_gap_estimate(self) -> None:
        log = (
            "2026-10-04T00:00:00.0000000Z started\n"
            "2026-10-04T00:00:01.0000000Z test_a (tools.repo_hygiene.test_x.Quiet.test_a) ... ok\n"
            "2026-10-04T00:00:11.0000000Z test_b (tools.repo_hygiene.test_x.Named.test_b) ... ok\n"
            "2026-10-04T00:00:11.0000000Z SHARD_SLOWEST profile=windows shard=1/7 total=11.0s\n"
            "2026-10-04T00:00:11.0000000Z   10.0s tools.repo_hygiene.test_x.Named\n"
        )
        self.assertEqual(
            timings.class_seconds(log),
            {"tools.repo_hygiene.test_x.Quiet": 1.0, "tools.repo_hygiene.test_x.Named": 10.0},
        )

    def test_a_class_outside_the_slowest_block_keeps_its_gap_estimate(self) -> None:
        log = (
            "2026-10-04T00:00:00.0000000Z started\n"
            "2026-10-04T00:00:04.0000000Z test_a (tools.repo_hygiene.test_x.A.test_a) ... ok\n"
            "2026-10-04T00:00:09.0000000Z test_b (tools.repo_hygiene.test_x.B.test_b) ... ok\n"
            "2026-10-04T00:00:09.0000000Z SHARD_SLOWEST profile=windows shard=1/7 total=9.0s\n"
            "2026-10-04T00:00:09.0000000Z    5.0s tools.repo_hygiene.test_x.B\n"
        )
        self.assertEqual(
            timings.class_seconds(log),
            {"tools.repo_hygiene.test_x.A": 4.0, "tools.repo_hygiene.test_x.B": 5.0},
        )

    def test_a_slowest_row_of_hundreds_of_digits_is_not_a_measurement(self) -> None:
        # adversary r2 pass 3 A2: float("9" * 400) is inf, which would poison the class's table row.
        # The runner prints one decimal, so a row is at most 7 integer digits; anything longer is
        # ignored and the gap estimate stands.
        log = (
            "2026-10-04T00:00:00.0000000Z started\n"
            "2026-10-04T00:00:02.0000000Z test_a (tools.repo_hygiene.test_x.A.test_a) ... ok\n"
            "2026-10-04T00:00:02.0000000Z SHARD_SLOWEST profile=windows shard=1/7 total=2.0s\n"
            + "2026-10-04T00:00:02.0000000Z   " + "9" * 400 + "s tools.repo_hygiene.test_x.A\n"
        )
        self.assertEqual(timings.class_seconds(log), {"tools.repo_hygiene.test_x.A": 2.0})
        for value in timings.class_seconds(log).values():
            self.assertLess(value, float("inf"))

    def test_a_docstring_id_line_with_no_earlier_stamped_line_starts_the_clock(self) -> None:
        # adversary r2 pass 2 A1: the id line is the first stamped line (a log cut after the setup
        # steps), so there is no previous line to charge from; the test keeps head -> result.
        log = (
            "2026-10-04T00:00:01.0000000Z test_a (tools.repo_hygiene.test_x.A.test_a)\n"
            "2026-10-04T00:00:04.0000000Z doc ... ok\n"
        )
        self.assertEqual(timings.class_seconds(log), {"tools.repo_hygiene.test_x.A": 3.0})

    def test_a_docstring_id_line_stamped_before_the_previous_line_keeps_head_to_result(self) -> None:
        # adversary r2 pass 2 A1: stamps out of order (interleaved job output); a negative gap is not
        # a setUpClass, so the clock restarts at the id line instead of clamping the test to 0 s.
        log = (
            "2026-10-04T00:00:03.0000000Z test_p (tools.repo_hygiene.test_x.P.test_p) ... ok\n"
            "2026-10-04T00:00:01.0000000Z test_a (tools.repo_hygiene.test_x.A.test_a)\n"
            "2026-10-04T00:00:02.0000000Z doc ... ok\n"
        )
        self.assertEqual(timings.class_seconds(log), {"tools.repo_hygiene.test_x.A": 1.0})

    def test_a_malformed_slowest_row_is_not_a_measurement(self) -> None:
        # adversary r2 pass 2 A2: "1.2.3s" matched the row pattern and raised ValueError in float().
        log = (
            "2026-10-04T00:00:00.0000000Z SHARD_SLOWEST profile=windows shard=1/7 total=1.0s\n"
            "2026-10-04T00:00:00.0000000Z   1.2.3s tools.repo_hygiene.test_x.A\n"
        )
        self.assertEqual(timings.class_seconds(log), {})

    def test_a_docstring_test_that_logs_a_line_keeps_its_whole_runtime(self) -> None:
        # CoS on #449: a stamped line between the id line and "<docstring> ... ok" (a logged
        # warning) cleared the pending test, so its result line was never counted. The test
        # keeps previous line -> result, the warning's line included; only a new id line replaces it
        # (and that replacement restarts the clock at the new id line).
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
            {"tools.repo_hygiene.test_x.AlphaTests": 6.0, "tools.repo_hygiene.test_x.GammaTests": 1.5},
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
            {"tools.repo_hygiene.test_x.A": 3.0, "tools.repo_hygiene.test_x.B": 2.0},
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
        # fable on #629: a docstring id line, then a plain "... ok" line with no result line
        # for the first test (crash, interleaved output). The plain line closes the pending
        # test: previous line -> plain line is charged to the PENDING class, as for a result line, and
        # the plain test is not charged (its own time is not separable from the pending one's).
        log = (
            "2026-10-04T00:00:00.0000000Z started\n"
            "2026-10-04T00:00:01.0000000Z test_a (tools.repo_hygiene.test_x.A.test_a)\n"
            "2026-10-04T00:00:05.0000000Z test_b (tools.repo_hygiene.test_x.B.test_b) ... ok\n"
            "2026-10-04T00:00:06.0000000Z test_c (tools.repo_hygiene.test_x.C.test_c) ... ok\n"
        )
        self.assertEqual(
            timings.class_seconds(log),
            {"tools.repo_hygiene.test_x.A": 5.0, "tools.repo_hygiene.test_x.C": 1.0},
        )

    def test_a_logged_line_shaped_like_an_id_head_is_not_a_pending_test(self) -> None:
        # Dot R18 on #634: "Running (1.2)" matches DOC_HEAD's shape. It set pending to "1.2",
        # so the next plain result was charged to that foreign id and dropped. Only a head
        # whose id is under KEY_PREFIX, or whose displayed name is the id's last component,
        # is a test; any other line is an ordinary logged line.
        log = (
            "2026-10-04T00:00:00.0000000Z started\n"
            "2026-10-04T00:00:01.0000000Z test_a (tools.repo_hygiene.test_x.A.test_a) ... ok\n"
            "2026-10-04T00:00:03.0000000Z Running (1.2)\n"
            "2026-10-04T00:00:06.0000000Z test_b (tools.repo_hygiene.test_x.B.test_b) ... ok\n"
        )
        self.assertEqual(
            timings.class_seconds(log),
            {"tools.repo_hygiene.test_x.A": 1.0, "tools.repo_hygiene.test_x.B": 3.0},
        )

    def test_a_logged_id_shaped_line_does_not_replace_a_pending_docstring_test(self) -> None:
        log = (
            "2026-10-04T00:00:00.0000000Z started\n"
            "2026-10-04T00:00:01.0000000Z test_a (tools.repo_hygiene.test_x.A.test_a)\n"
            "2026-10-04T00:00:03.0000000Z Running (1.2)\n"
            "2026-10-04T00:00:06.0000000Z the docstring first line ... ok\n"
        )
        self.assertEqual(timings.class_seconds(log), {"tools.repo_hygiene.test_x.A": 6.0})

    def test_a_docstring_head_outside_the_key_prefix_is_still_pending(self) -> None:
        # The displayed name equals the id's last component, so this is a real test whose
        # class is merely not ours: its result line must still close it (and is dropped).
        log = (
            "2026-10-04T00:00:00.0000000Z started\n"
            "2026-10-04T00:00:01.0000000Z unrelated (somewhere.else.Tests.unrelated)\n"
            "2026-10-04T00:00:04.0000000Z the docstring first line ... ok\n"
            "2026-10-04T00:00:05.0000000Z test_b (tools.repo_hygiene.test_x.B.test_b) ... ok\n"
        )
        self.assertEqual(timings.class_seconds(log), {"tools.repo_hygiene.test_x.B": 1.0})

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
