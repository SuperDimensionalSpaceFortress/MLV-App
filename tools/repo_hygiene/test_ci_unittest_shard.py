from __future__ import annotations

import unittest
from pathlib import Path

from tools.repo_hygiene import ci_unittest_shard as shard

REPO_ROOT = Path(__file__).resolve().parents[2]


class AssignTests(unittest.TestCase):
    def test_every_module_lands_in_exactly_one_shard(self) -> None:
        modules = shard.discover_modules(REPO_ROOT)
        for shards in (1, 2, 4, 7):
            with self.subTest(shards=shards):
                buckets = shard.assign(modules, shards)
                self.assertEqual(len(buckets), shards)
                self.assertEqual(sorted(name for bucket in buckets for name in bucket), modules)

    def test_assignment_is_deterministic_and_independent_of_input_order(self) -> None:
        modules = shard.discover_modules(REPO_ROOT)
        self.assertEqual(shard.assign(modules, 4), shard.assign(list(reversed(modules)), 4))

    def test_a_module_the_table_does_not_know_is_still_assigned(self) -> None:
        modules = [*shard.discover_modules(REPO_ROOT), "tools.repo_hygiene.test_brand_new_file"]
        buckets = shard.assign(modules, 4)
        self.assertEqual(sum("tools.repo_hygiene.test_brand_new_file" in b for b in buckets), 1)

    def test_the_heaviest_modules_are_spread_across_shards(self) -> None:
        heaviest = sorted(shard.MEASURED_SECONDS, key=shard.MEASURED_SECONDS.get, reverse=True)[:4]
        buckets = shard.assign(shard.discover_modules(REPO_ROOT), 4)
        homes = {index for name in heaviest for index, b in enumerate(buckets) if name in b}
        self.assertEqual(len(homes), 4)

    def test_zero_shards_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            shard.assign(["tools.repo_hygiene.test_x"], 0)

    def test_every_measured_module_still_exists(self) -> None:
        # A renamed or deleted module would leave a stale weight that skews the balance.
        modules = set(shard.discover_modules(REPO_ROOT))
        self.assertEqual(set(shard.MEASURED_SECONDS) - modules, set())


if __name__ == "__main__":
    unittest.main()
