"""Hosted-CI-safe half of plan step 0.18's parity check.

Asserts `docs/roadmap.md` (the tracked product-backlog view) keeps the shape that
`tools/coordination/render_roadmap.py` gives it - NEVER against `.claude-state/queue.json`
or `card-queue.json`, which are gitignored and absent from every hosted checkout
(O97/O106). Freshness against the board's card-queue.json is `render_roadmap.py --check`,
run locally by the hub. Until ROADMAP-REFRESH-FROM-CARD-QUEUE-1 the roadmap mirrored the
procedure files at `docs/lane-prompts/v2/`; it no longer does, and those checks are gone.

Also unit-tests `check_roadmap_queue_parity.check_queue`'s LOGIC directly, against
fixture queue data written to a temp file — never the real gitignored queue.json, which
cannot be content-addressed to a reviewed sha (sol's PR #79 round-2 review, BLOCKER: the
claimed 6-test acceptance suite never imported or exercised `check_queue`,
`procedureSha256`, or `min_count` at all, so reverting the round-2 fix would have left
this suite green). Each fixture below reproduces one of sol's own round-1/round-2
findings by construction.

`tools/coordination/check_roadmap_queue_parity.py` is the other half: it validates the
REAL `queue.json` and is local-only by necessity; this suite tests its function in
isolation instead.
"""

from __future__ import annotations

import importlib.util
import json
import re
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ROADMAP_PATH = REPO_ROOT / "docs" / "roadmap.md"
CHECKER_PATH = REPO_ROOT / "tools" / "coordination" / "check_roadmap_queue_parity.py"

ROADMAP_ROW_RX = re.compile(r"^\|\s*\d+\s*\|\s*(\S+)\s*\|\s*(product|playback)\s*\|", re.MULTILINE)
GENERATED_FROM_RX = re.compile(r"^generated_from: \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", re.MULTILINE)
# docs/22 working tier is 25000 soft; the rendered roadmap grows with the open backlog.
ROADMAP_BUDGET_BYTES = 20000


def _roadmap_rows() -> list[tuple[str, str]]:
    """(card_id, kind) for every open-card row in the roadmap table."""
    text = ROADMAP_PATH.read_text(encoding="utf-8")
    return ROADMAP_ROW_RX.findall(text)


def _load_checker_module():
    spec = importlib.util.spec_from_file_location(
        "check_roadmap_queue_parity", CHECKER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RoadmapQueueParityTests(unittest.TestCase):
    def test_no_case_references_the_gitignored_queue(self) -> None:
        """Any mention of the gitignored queue must be disclaimed as non-hosted, somewhere in the file."""
        text = ROADMAP_PATH.read_text(encoding="utf-8")
        for forbidden in (".claude-state", "queue.json"):
            if forbidden in text:
                self.assertIn(
                    "gitignored",
                    text,
                    f"roadmap.md references {forbidden!r} without disclaiming it as gitignored/non-hosted",
                )

    def test_roadmap_size_budget(self) -> None:
        self.assertLessEqual(
            ROADMAP_PATH.stat().st_size,
            ROADMAP_BUDGET_BYTES,
            "docs/roadmap.md is over budget; shorten the parked notes or split the parked section out",
        )

    def test_roadmap_is_rendered_from_the_card_queue(self) -> None:
        text = ROADMAP_PATH.read_text(encoding="utf-8")
        self.assertEqual(len(GENERATED_FROM_RX.findall(text)), 1, "exactly one generated_from: line")
        self.assertIn("tools/coordination/render_roadmap.py", text)
        for heading in ("## Open product and playback cards", "## Drain", "## Parked with reason"):
            self.assertIn(heading, text)

    def test_roadmap_states_macos_compilation_is_not_deferred(self) -> None:
        """sol r1 blocker on #670: the rendered roadmap must say compilation is NOT deferred."""
        text = ROADMAP_PATH.read_text(encoding="utf-8")
        self.assertIn("Not deferred: macOS compilation stays green", text)

    def test_every_table_row_is_a_product_or_playback_card(self) -> None:
        section = ROADMAP_PATH.read_text(encoding="utf-8").split("## Drain")[0]
        table_lines = [l for l in section.splitlines() if re.match(r"^\|\s*\d+\s*\|", l)]
        self.assertEqual(len(table_lines), len(ROADMAP_ROW_RX.findall(section)), table_lines)

    def test_no_duplicate_card_ids_on_the_roadmap(self) -> None:
        ids = [card_id for card_id, _kind in _roadmap_rows()]
        self.assertEqual(len(ids), len(set(ids)), f"duplicate card id(s) on the roadmap: {ids}")

    def test_usecase_1_is_never_listed(self) -> None:
        """USECASE-1 is a closure receipt only, never seeded as dispatchable (plan step 0.18)."""
        ids = {card_id for card_id, _kind in _roadmap_rows()}
        self.assertNotIn("USECASE-1", ids)


class CheckRoadmapQueueParityLogicTests(unittest.TestCase):
    """Unit tests for check_queue() itself, against FIXTURE queue data — never the real,
    gitignored queue.json, which cannot be content-addressed to a reviewed sha. Each
    fixture reproduces one of sol's PR #79 findings by construction, so reverting either
    round's fix fails one of these deterministically.
    """

    def setUp(self) -> None:
        self.module = _load_checker_module()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        # A real, valid procedure file the fixtures can point at and hash correctly.
        self.procedure_path = REPO_ROOT / "docs" / "lane-prompts" / "v2" / "hub-procedure.md"
        self.procedure_rel = "docs/lane-prompts/v2/hub-procedure.md"
        self.procedure_sha256 = self.module._sha256_of(self.procedure_path)

    def _write_queue(self, items: list[dict]) -> Path:
        path = Path(self.tmpdir.name) / "queue.json"
        path.write_text(json.dumps({"items": items}), encoding="utf-8")
        return path

    def _valid_card(self, **overrides) -> dict:
        card = {
            "id": "FIXTURE-1",
            "kind": "product",
            "track": "product",
            "scope": "some/path.h",
            "procedure": self.procedure_rel,
            "procedureSha256": self.procedure_sha256,
        }
        card.update(overrides)
        return card

    def test_a_fully_valid_card_passes(self) -> None:
        ok, lines = self.module.check_queue(self._write_queue([self._valid_card()]))
        self.assertTrue(ok, lines)

    def test_zero_cards_fails_with_default_min_count(self) -> None:
        """sol round-2 finding: an empty queue must never report a vacuous OK."""
        ok, lines = self.module.check_queue(self._write_queue([]))
        self.assertFalse(ok, lines)

    def test_min_count_enforces_an_exact_floor(self) -> None:
        ok, lines = self.module.check_queue(self._write_queue([self._valid_card()]), min_count=15)
        self.assertFalse(ok, lines)
        ok15, _lines15 = self.module.check_queue(
            self._write_queue([self._valid_card(id=f"F-{i}") for i in range(15)]), min_count=15
        )
        self.assertTrue(ok15, _lines15)

    def test_missing_procedure_sha256_fails_not_skips(self) -> None:
        """sol round-1 finding: a missing hash must be a FAIL, never a silent skip."""
        card = self._valid_card()
        del card["procedureSha256"]
        ok, lines = self.module.check_queue(self._write_queue([card]))
        self.assertFalse(ok, lines)
        self.assertTrue(any("procedureSha256 missing" in line for line in lines), lines)

    def test_track_not_equal_kind_fails(self) -> None:
        ok, lines = self.module.check_queue(self._write_queue([self._valid_card(track="playback")]))
        self.assertFalse(ok, lines)

    def test_empty_scope_fails(self) -> None:
        ok, lines = self.module.check_queue(self._write_queue([self._valid_card(scope="")]))
        self.assertFalse(ok, lines)

    def test_stale_procedure_sha256_fails(self) -> None:
        ok, lines = self.module.check_queue(self._write_queue([self._valid_card(procedureSha256="0" * 64)]))
        self.assertFalse(ok, lines)
        self.assertTrue(any("stale" in line for line in lines), lines)

    def test_missing_procedure_file_fails(self) -> None:
        ok, lines = self.module.check_queue(
            self._write_queue([self._valid_card(procedure="docs/lane-prompts/v2/does-not-exist.md")])
        )
        self.assertFalse(ok, lines)

    def test_a_non_product_playback_kind_is_ignored(self) -> None:
        """A factory/UNSET-kind item is out of scope for this checker entirely."""
        ok, lines = self.module.check_queue(
            self._write_queue([{"id": "OTHER", "kind": "factory", "track": "factory"}]), min_count=0
        )
        self.assertTrue(ok, lines)
        self.assertIn("0 product/playback card(s) checked", lines[-1])


if __name__ == "__main__":
    unittest.main()
