"""Offline mirror of the pipeline_tests shard plan and part assignment in tests.yml.

The `Windows Product Oracles part N/3` jobs each run the step `Run pipeline_tests --check-golden
(bounded shards)`. The step discovers every gtest name, cuts the sorted names into windows of
`$testsPerShard`, runs the heavy fixture renders alone, and gives each part a share of those shards by
a longest-first greedy assignment seeded with the other work the part carries. The weights that
assignment uses are the measured per-test wall times in `tools/testing/pipeline_test_weights.json`.

This module repeats that discovery and assignment so a unit test can price every part without a hosted
runner (CI-FLAKE-PRODUCT-ORACLES-PART1-CHECK-GOLDEN-CAP-1: part 1/3 was cancelled at the 60-minute job
cap three times in 48 hours because its seed said 200 s of other work when it was about 1,000 s).
`test_ci_pipeline_part_budget.py` pins the workflow text to the constants used here, so the two cannot
drift apart silently.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WEIGHTS_PATH = REPO_ROOT / "tools" / "testing" / "pipeline_test_weights.json"
PIPELINE_TESTS_DIR = REPO_ROOT / "tests" / "pipeline"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "tests.yml"

TEST_DECLARATION = re.compile(r"TEST(?:_[FP])?\(\s*([A-Za-z0-9_]+)\s*,\s*([A-Za-z0-9_]+)")


def _powershell_sort_key(name: str) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Order of `Sort-Object` on a gtest name: culture-aware and case-insensitive first.

    Names hold only letters, digits, `_` and `.`. Punctuation sorts before digits, digits before
    letters, `_` before `.`, and a lowercase letter before the same uppercase letter on a tie.
    """
    primary: list[int] = []
    tertiary: list[int] = []
    for char in name:
        if char == "_":
            primary.append(0)
        elif char == ".":
            primary.append(1)
        elif char.isdigit():
            primary.append(2 + int(char))
        else:
            primary.append(100 + ord(char.lower()))
        tertiary.append(1 if char.isupper() else 0)
    return tuple(primary), tuple(tertiary)


def discover_test_names(tests_dir: Path = PIPELINE_TESTS_DIR) -> list[str]:
    names: set[str] = set()
    for source in tests_dir.glob("*.cpp"):
        text = source.read_text(encoding="utf-8", errors="replace")
        for match in TEST_DECLARATION.finditer(text):
            names.add(f"{match.group(1).strip()}.{match.group(2).strip()}")
    return sorted(names, key=_powershell_sort_key)


@dataclass(frozen=True)
class Shard:
    name: str
    tests: tuple[str, ...]
    index: int

    @property
    def filter(self) -> str:
        return ":".join(self.tests)


# A weight above the 3,600,000 ms job cap is a typo, and one past Int64 is a PowerShell `[long]`
# conversion error in the workflow that this loader would otherwise accept.
MAX_MILLIS = 3_600_000


def _is_whole(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def load_weights_from_text(text: str) -> dict:
    """Parse the weight table; every number is a whole millisecond, as `[long]` reads it in the workflow."""
    data = json.loads(text)
    if data.get("schema") != 1 or data.get("unit") != "milliseconds":
        raise ValueError("pipeline test weights: unexpected schema or unit")
    numbers = [data.get("defaultTestMillis"), *data.get("partSeedMillis", []), *data.get("testMillis", {}).values()]
    if not data.get("partSeedMillis") or not all(_is_whole(number) and 0 <= number <= MAX_MILLIS for number in numbers):
        raise ValueError(
            f"pipeline test weights: every weight and seed must be a whole number of milliseconds from 0 to {MAX_MILLIS}"
        )
    return data


def load_weights(path: Path = WEIGHTS_PATH) -> dict:
    return load_weights_from_text(path.read_text(encoding="utf-8"))


def build_shards(test_names: list[str], tests_per_shard: int, solo_pattern: str) -> list[Shard]:
    # PowerShell `-match` is case-insensitive; so is this.
    solo = re.compile(solo_pattern, re.IGNORECASE)
    shards: list[Shard] = []
    for offset in range(0, len(test_names), tests_per_shard):
        window = test_names[offset : offset + tests_per_shard]
        window_index = offset // tests_per_shard
        shared = [name for name in window if not solo.search(name)]
        if shared:
            shards.append(Shard(f"exact-{window_index:02d}", tuple(shared), len(shards)))
        for position, name in enumerate(name for name in window if solo.search(name)):
            shards.append(Shard(f"exact-{window_index:02d}-solo{position + 1}", (name,), len(shards)))
    return shards


def shard_millis(shard: Shard, weights: dict) -> int:
    table = weights["testMillis"]
    default = weights["defaultTestMillis"]
    return sum(table.get(name, default) for name in shard.tests)


def assign_parts(shards: list[Shard], weights: dict, part_count: int) -> tuple[dict[str, int], list[int]]:
    """Longest-first greedy onto the least-loaded part, seeded with each part's other work."""
    load = list(weights["partSeedMillis"])
    if len(load) != part_count:
        raise ValueError("partSeedMillis must have one entry per part")
    part_of: dict[str, int] = {}
    ordered = sorted(shards, key=lambda shard: (-shard_millis(shard, weights), shard.index))
    for shard in ordered:
        target = 0
        for candidate in range(1, part_count):
            if load[candidate] < load[target]:
                target = candidate
        part_of[shard.name] = target + 1
        load[target] += shard_millis(shard, weights)
    return part_of, load
