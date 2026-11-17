from __future__ import annotations

import contextlib
import io
import json
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
from collections.abc import Callable
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

    def test_a_failed_kill_in_the_watchdog_does_not_skip_the_later_workers(self) -> None:
        killer = shard.DeadlineKiller(1.0, lambda: 2.0, slice_seconds=3600)
        first, second = self._RunningProc(PermissionError("denied")), self._RunningProc()
        killer.add(first)  # must not raise
        killer.add(second)
        self.assertEqual(second.returncode, -9, "worker 2 was never killed after worker 1's kill raised")
        self.assertEqual(killer.killed, {2})
        self.assertEqual(sorted(killer.failed), [1])
        self.assertGreaterEqual(first.kill_calls, 2, "the watchdog retries a worker it could not kill")

    # r4 adversary A2 (pass 4): a worker whose only class skips in a fixture but whose cleanup fails
    # runs no test; serial fails it (exit 1 with the others' tests run), so the parallel shard must too.
    _GOOD = "import unittest\nclass FxGoodTests(unittest.TestCase):\n    def test_ok(self):\n        pass\n"
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
