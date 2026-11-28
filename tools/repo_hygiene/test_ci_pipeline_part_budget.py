"""Pins the part budget of `Windows Product Oracles part N/3` (CI-FLAKE-PRODUCT-ORACLES-PART1-CHECK-GOLDEN-CAP-1).

Part 1/3 was cancelled at the 60-minute job cap inside `Run pipeline_tests --check-golden (bounded
shards)` three times in 48 hours (#915, #924, the #925 merge_group). No shard hung: every shard was
bounded at 240 s and the part was still finishing shards two minutes before the cap. Part 1 simply
carried more than the others: its seed for the other work it owns (a console_tests build and
--check-golden, about 1,000 s) was 200 s, and the flat 30 s / 17 s shard weights hid that the
LookAssist fixture renders take 60-90 s each. The assignment now uses measured weights
(`tools/testing/pipeline_test_weights.json`), and this test prices every part with them.
"""

from __future__ import annotations

import re
import unittest

from tools.repo_hygiene import ci_pipeline_part_plan as plan

# What one part may weigh: its seed plus its shards, from the table, in seconds. The job cap is
# 3,600 s, so a part may run 1.5x its table weight before the cap cancels it. The balanced plan
# prices each part at about 2,050 s; the rest is headroom for new tests (each unlisted test costs
# `defaultTestMillis`). Raising this number is a decision to accept less slack against runner
# variance, not a way to make a red test green: rebalance, or split a 4th part, instead.
PART_BUDGET_SECONDS = 2400
JOB_CAP_SECONDS = 3600
MIN_SLACK_FACTOR = 1.5


def _read_workflow() -> str:
    return plan.WORKFLOW_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")


def _workflow_job(workflow: str, job_id: str) -> str:
    start = workflow.index(f"\n  {job_id}:\n")
    following = re.search(r"\n  [A-Za-z0-9_-]+:\n", workflow[start + 1 :])
    return workflow[start : start + 1 + following.start()] if following else workflow[start:]


# The planning logic of the step, comment and blank lines dropped and indentation stripped, from
# `$partCount` through the assignment loop. The mirror in ci_pipeline_part_plan.py repeats exactly
# this; a changed line (a scaled weight, another tie-break, another default) would make the budget
# below describe a plan CI no longer runs, so any edit here must be made there too.
EXPECTED_PLANNING_LOGIC = (
    "$partCount = 3",
    "$part = [int]'${{ matrix.part }}'",
    "$weightTable = Get-Content -LiteralPath 'tools\\testing\\pipeline_test_weights.json' -Raw | ConvertFrom-Json -AsHashtable",
    "if ($weightTable.schema -ne 1 -or $weightTable.unit -ne 'milliseconds') { throw \"pipeline test weights have an unexpected schema or unit\" }",
    "$partLoad = @($weightTable.partSeedMillis | ForEach-Object { [long]$_ })",
    "if ($partLoad.Count -ne $partCount) { throw \"pipeline part seeds must have one entry per part (seeds=$($partLoad.Count) parts=$partCount)\" }",
    "$defaultTestMillis = [long]$weightTable.defaultTestMillis",
    "foreach ($planned in $shards) {",
    "$weight = [long]0",
    "foreach ($plannedTest in ($planned.Filter -split ':')) {",
    "$weight += $(if ($weightTable.testMillis.ContainsKey($plannedTest)) { [long]$weightTable.testMillis[$plannedTest] } else { $defaultTestMillis })",
    "}",
    "$planned.Weight = $weight",
    "}",
    "$partOf = @{}",
    "$planOrder = @($shards | Sort-Object @{ Expression = { $_.Weight }; Descending = $true }, @{ Expression = { $_.Index }; Descending = $false })",
    "foreach ($planned in $planOrder) {",
    "$target = 0",
    "for ($candidate = 1; $candidate -lt $partCount; $candidate++) {",
    "if ($partLoad[$candidate] -lt $partLoad[$target]) { $target = $candidate }",
    "}",
    "$partOf[$planned.Name] = $target + 1",
    "$partLoad[$target] += $planned.Weight",
    "}",
)


def _planning_logic(part_job: str) -> tuple[str, ...]:
    lines = [line.strip() for line in part_job.splitlines()]
    start = next(i for i, line in enumerate(lines) if line.startswith("$partCount = "))
    end = next(i for i, line in enumerate(lines) if line.startswith("$partLoad[$target] +=")) + 1
    logic = [line for line in lines[start : end + 1] if line and not line.startswith("#")]
    return tuple(logic)


def _solo_pattern(part_job: str) -> str:
    match = re.search(r"(?m)^\s*\$soloPattern = '([^\n]*)'$", part_job)
    assert match, "the pipeline step lost its $soloPattern line"
    return match.group(1)


def _tests_per_shard(part_job: str) -> int:
    found = re.findall(r"(?m)^\s*\$testsPerShard = (\d+)$", part_job)
    assert len(found) == 1, "the pipeline step must set $testsPerShard exactly once"
    return int(found[0])


class PipelinePartBudgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = _read_workflow()
        cls.part_job = _workflow_job(cls.workflow, "windows-product-oracles-part")
        cls.weights = plan.load_weights()
        cls.part_count = int(re.findall(r"(?m)^\s*\$partCount = (\d+)$", cls.part_job)[0])
        cls.test_names = plan.discover_test_names()
        cls.shards = plan.build_shards(
            cls.test_names, _tests_per_shard(cls.part_job), _solo_pattern(cls.part_job)
        )
        cls.part_of, cls.load = plan.assign_parts(cls.shards, cls.weights, cls.part_count)

    def test_every_part_stays_inside_the_budget(self) -> None:
        for part, millis in enumerate(self.load, start=1):
            with self.subTest(part=part):
                self.assertLessEqual(
                    millis / 1000,
                    PART_BUDGET_SECONDS,
                    f"part {part}/{self.part_count} is priced at {millis / 1000:.0f} s "
                    f"(seed + measured shards) against a budget of {PART_BUDGET_SECONDS} s; "
                    f"all parts: {[round(m / 1000) for m in self.load]}. Add a part or move work, "
                    "do not widen the job timeout",
                )

    def test_the_budget_leaves_the_job_cap_real_slack(self) -> None:
        timeout = re.search(r"(?m)^    timeout-minutes: (\d+)$", self.part_job)
        self.assertIsNotNone(timeout)
        self.assertEqual(int(timeout.group(1)) * 60, JOB_CAP_SECONDS)
        self.assertGreaterEqual(JOB_CAP_SECONDS / PART_BUDGET_SECONDS, MIN_SLACK_FACTOR)

    def test_the_parts_are_balanced(self) -> None:
        # Part 1 was priced at 41 min against 28 min for part 3 before the measured weights.
        spread = (max(self.load) - min(self.load)) / 1000
        self.assertLessEqual(spread, 300, f"part loads {[round(m / 1000) for m in self.load]} differ by {spread:.0f} s")

    def test_the_parts_run_every_test_exactly_once(self) -> None:
        ran: list[str] = []
        for part in range(1, self.part_count + 1):
            for shard in self.shards:
                if self.part_of[shard.name] == part:
                    ran.extend(shard.tests)
        self.assertEqual(sorted(ran), sorted(self.test_names))
        self.assertEqual(len(set(ran)), len(ran))
        self.assertEqual({self.part_of[s.name] for s in self.shards}, set(range(1, self.part_count + 1)))

    def test_the_seeds_say_part_one_carries_the_console_oracle(self) -> None:
        seeds = self.weights["partSeedMillis"]
        self.assertEqual(len(seeds), self.part_count)
        # Part 1 builds console_tests and runs its --check-golden (about 11 min on a hosted runner)
        # on top of what every part does; the old seeds were 200 / 140 / 0 s.
        self.assertGreaterEqual(seeds[0] - seeds[2], 500_000)
        self.assertGreater(seeds[1], seeds[2])

    def test_an_unlisted_test_costs_the_default_and_a_listed_one_its_row(self) -> None:
        listed = next(iter(self.weights["testMillis"]))
        one = plan.Shard("x", (listed,), 0)
        unknown = plan.Shard("y", ("NoSuchSuite.NoSuchTest",), 1)
        two = plan.Shard("z", (listed, "NoSuchSuite.NoSuchTest"), 2)
        self.assertEqual(plan.shard_millis(one, self.weights), self.weights["testMillis"][listed])
        self.assertEqual(plan.shard_millis(unknown, self.weights), self.weights["defaultTestMillis"])
        self.assertEqual(plan.shard_millis(two, self.weights), self.weights["testMillis"][listed] + self.weights["defaultTestMillis"])

    def test_the_weight_table_is_well_formed(self) -> None:
        table = self.weights["testMillis"]
        self.assertTrue(all(isinstance(v, int) and v > 0 for v in table.values()))
        self.assertTrue(all(isinstance(v, int) and v > 0 for v in self.weights["partSeedMillis"]))
        self.assertGreater(self.weights["defaultTestMillis"], 0)
        # PowerShell reads this table into a case-insensitive hashtable; two keys that differ only
        # by case would be one key there and two here.
        self.assertEqual(len({name.lower() for name in table}), len(table))

    def test_a_heavy_fixture_render_is_priced_as_heavy(self) -> None:
        # The flat 30 s solo weight priced a 90 s LookAssist render like a 10 s one.
        table = self.weights["testMillis"]
        heavy = [v for name, v in table.items() if name.startswith(("LookAssistFixtureScene.", "LookAssistFlavorsFixture."))]
        self.assertTrue(heavy)
        self.assertGreaterEqual(max(heavy), 60_000)

    def test_the_workflow_uses_the_table_the_mirror_prices(self) -> None:
        # Every line below is one the mirror repeats; changing any of them without
        # ci_pipeline_part_plan.py would make this budget describe a plan CI no longer runs.
        for needed in (
            "$weightTable = Get-Content -LiteralPath 'tools\\testing\\pipeline_test_weights.json' -Raw | ConvertFrom-Json -AsHashtable",
            "$partLoad = @($weightTable.partSeedMillis | ForEach-Object { [long]$_ })",
            "$defaultTestMillis = [long]$weightTable.defaultTestMillis",
            "$weightTable.testMillis.ContainsKey($plannedTest)",
            "$planOrder = @($shards | Sort-Object @{ Expression = { $_.Weight }; Descending = $true }, @{ Expression = { $_.Index }; Descending = $false })",
            "$partLoad[$target] += $planned.Weight",
            "if ($partLoad[$candidate] -lt $partLoad[$target]) { $target = $candidate }",
            "$myShards = @($shards | Where-Object { $partOf[$_.Name] -eq $part })",
        ):
            self.assertIn(needed, self.part_job)
        self.assertEqual(_tests_per_shard(self.part_job), 12)
        self.assertNotIn("$partLoad = @(200.0, 140.0, 0.0)", self.part_job)
        # Shards carry their plan index, which breaks weight ties the same way here and there.
        self.assertEqual(self.part_job.count("Index = $shards.Count"), 2)

    def test_the_planning_logic_is_exactly_what_the_mirror_repeats(self) -> None:
        self.assertEqual(_planning_logic(self.part_job), EXPECTED_PLANNING_LOGIC)

    def test_a_scaled_weight_in_the_workflow_is_refused(self) -> None:
        # adversary A1: `$planned.Weight = $weight * 0.4` priced part 3 at 2,541 s while every other
        # pinned line, and the budget, still passed.
        for original, mutated in (
            ("$planned.Weight = $weight", "$planned.Weight = $weight * 0.4"),
            ("$weight += $(if", "$weight += 0.4 * $(if"),
            ("else { $defaultTestMillis })", "else { 0 })"),
            ("$partLoad[$target] += $planned.Weight", "$partLoad[$target] += $planned.Weight / 2"),
            ("Descending = $true }", "Descending = $false }"),
        ):
            with self.subTest(mutated=mutated):
                self.assertIn(original, self.part_job)
                self.assertNotEqual(_planning_logic(self.part_job.replace(original, mutated, 1)), EXPECTED_PLANNING_LOGIC)

    def test_a_fractional_default_is_refused(self) -> None:
        # adversary A2: PowerShell `[long]0.5` is 0 while Python kept 0.5; every number must be a
        # whole millisecond so both read the same value.
        for key in ("defaultTestMillis",):
            self.assertIsInstance(self.weights[key], int)
            self.assertNotIsInstance(self.weights[key], bool)
        for seed in self.weights["partSeedMillis"]:
            self.assertIsInstance(seed, int)
        for value in self.weights["testMillis"].values():
            self.assertIsInstance(value, int)
        with self.assertRaises(ValueError):
            plan.load_weights_from_text('{"schema": 1, "unit": "milliseconds", "partSeedMillis": [1, 2, 3], "defaultTestMillis": 0.5, "testMillis": {}}')

    def test_the_mirror_reproduces_the_window_cut(self) -> None:
        # Windows are consecutive slices of the sorted names; a solo test leaves its window's
        # shared shard, so the shard tests of one window are exactly that window's names.
        per_window: dict[str, list[str]] = {}
        for shard in self.shards:
            per_window.setdefault(shard.name.split("-solo")[0], []).extend(shard.tests)
        flat = [name for key in sorted(per_window) for name in per_window[key]]
        self.assertEqual(sorted(flat), sorted(self.test_names))
        self.assertTrue(all(len(v) <= 12 for v in per_window.values()))


if __name__ == "__main__":
    unittest.main()
