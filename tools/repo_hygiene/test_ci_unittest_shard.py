from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

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


def _windows_shard_job() -> str:
    text = WORKFLOW.read_text(encoding="utf-8").replace("\r\n", "\n")
    start = re.search(rf"(?m)^  {re.escape(WINDOWS_SHARD_JOB)}:$", text)
    assert start is not None, WINDOWS_SHARD_JOB
    following = re.search(r"(?m)^  [a-z0-9-]+:$", text[start.end() :])
    return text[start.start() : start.end() + following.start() if following else len(text)]


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
        # 101 of them hid that shard 2 was at 31 min while the table claimed 10.6.
        for profile in ("windows",):
            weights = shard.load_weights(profile)
            unmeasured = {key: n for key, n in _COUNTS.items() if key not in weights}
            share = sum(unmeasured.values()) / sum(_COUNTS.values())
            self.assertLessEqual(
                share,
                MAX_UNMEASURED_TEST_SHARE,
                f"{profile}: {sum(unmeasured.values())} tests in {len(unmeasured)} classes have no "
                f"measured weight ({share:.0%}); re-run ci_unittest_shard_timings on hosted logs",
            )


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
