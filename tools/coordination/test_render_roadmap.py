"""Tests for render_roadmap.py: docs/roadmap.md rendered from the board's card-queue.json.

The real card-queue.json is board-local (gitignored) and absent from hosted CI, so every
case here runs against `testdata/render_roadmap_card_queue.json` or rows built in memory.
Run: py -3 -m unittest tools.coordination.test_render_roadmap -v
"""

from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "render_roadmap.py"
FIXTURE = HERE / "testdata" / "render_roadmap_card_queue.json"

_spec = importlib.util.spec_from_file_location("render_roadmap", SCRIPT)
render_roadmap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(render_roadmap)


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def row(card: str, state: str, stamp: str, kind: str = "product", priority: int = 3, **extra) -> dict:
    base = {
        "card": card, "kind": kind, "priority": priority, "state": state,
        "source_ledger_stamp": stamp, "source_ledger_line": 1, "order_chain": None,
        "notes": "", "evidence": "",
    }
    base.update(extra)
    return base


def synthetic(open_n: int, landed_n: int, queued_n: int) -> dict:
    """Board with exactly open_n open rows, landed_n landings and queued_n intakes in the window.

    Open rows are stamped before the window so they add no intake; intakes are parked rows
    stamped inside it; landings are merged rows whose declared@ stamp is inside it.
    """
    old = "2026-09-01T00:00:00Z"
    rows = [row("OPEN-%d" % i, "queued", old) for i in range(open_n)]
    rows += [row("LANDED-%d" % i, "merged", old, evidence="declared@2026-10-22T09:00:00Z") for i in range(landed_n)]
    rows += [row("INTAKE-%d" % i, "parked", "2026-10-22T09:00:00Z") for i in range(queued_n)]
    return {"schema": "mlv-app/card-queue/v1", "generated_utc": "2026-10-23T12:00:00Z", "rows": rows}


def table_cards(text: str) -> list[str]:
    """Card column of the open-rows table, in rendered order."""
    out = []
    for line in text.split("## Drain")[0].splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("| ") and len(cells) == 6 and cells[0].isdigit():
            out.append(cells[1])
    return out


class OpenTableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = render_roadmap.render(load_fixture())

    def test_sorted_by_priority_then_stamp_then_card(self) -> None:
        # P1: C and D share a stamp (tie broken by card name), B is later; P2: A.
        self.assertEqual(
            table_cards(self.text), ["OPEN-C-P1-TIE", "OPEN-D-P1-TIE", "OPEN-B-P1-LATE", "OPEN-A-P2"]
        )

    def test_merged_parked_and_other_kinds_are_not_open_rows(self) -> None:
        cards = table_cards(self.text)
        for absent in ("PARKED-E", "PARKED-NOTELESS", "MERGED-F-DECLARED", "TOOLING-J-OPEN",
                       "PLAYBACK-NAMED-UNCLASSIFIED-K"):
            self.assertNotIn(absent, cards)

    def test_row_columns(self) -> None:
        self.assertIn("| P | card | kind | state | queued since | order_chain |", self.text)
        self.assertIn("| 1 | OPEN-C-P1-TIE | product | pr-open | 2026-10-18 | ORDER-20261022T080000Z #1 first |", self.text)
        self.assertIn(
            "| 1 | OPEN-B-P1-LATE | product | declared | 2026-10-22 | ORDER-20261022T080000Z #2 after OPEN-C-P1-TIE |",
            self.text,
        )
        self.assertIn("| 1 | OPEN-D-P1-TIE | playback | running | 2026-10-18 | - |", self.text)


class ParkedSectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = render_roadmap.render(load_fixture())
        self.section = self.text.split("## Parked with reason")[1]

    def test_lists_only_parked_product_rows(self) -> None:
        self.assertIn("PARKED-E", self.section)
        self.assertIn("PARKED-NOTELESS", self.section)
        self.assertNotIn("OPEN-A-P2", self.section)
        self.assertNotIn("MERGED-F-DECLARED", self.section)

    def test_note_is_truncated_to_140_chars(self) -> None:
        line = next(l for l in self.section.splitlines() if "PARKED-E" in l)
        note = line.split("PARKED-E", 1)[1].lstrip(" :-()P0123456789")
        self.assertLessEqual(len(note), 140)
        self.assertTrue(note.startswith("waiting on the owner clip."), note)
        self.assertNotIn("readable", line)

    def test_a_short_note_is_kept_whole_and_an_empty_one_says_so(self) -> None:
        data = load_fixture()
        data["rows"].append(row("PARKED-SHORT", "parked", "2026-10-02T00:00:00Z", notes="line one\nline two"))
        section = render_roadmap.render(data).split("## Parked with reason")[1]
        self.assertIn("line one line two", section)
        self.assertIn("(no note)", section)


class DrainTests(unittest.TestCase):
    def test_open_count_and_per_day_table_on_the_fixture(self) -> None:
        text = render_roadmap.render(load_fixture())
        self.assertIn("product+playback open: 4", text)
        self.assertIn("| day (UTC) | landed | queued |", text)
        # Landed: F by declared@ (10-21), H by pinned_utc (10-22), G by its own stamp (10-23).
        for line in ("| 2026-10-17 | 0 | 0 |", "| 2026-10-18 | 0 | 2 |", "| 2026-10-19 | 0 | 1 |",
                     "| 2026-10-20 | 0 | 1 |", "| 2026-10-21 | 1 | 0 |", "| 2026-10-22 | 1 | 1 |",
                     "| 2026-10-23 | 1 | 1 |", "| total | 3 | 6 |"):
            self.assertIn(line, text)

    def test_rows_outside_the_window_are_not_counted(self) -> None:
        # MERGED-I-OLD (10-02) and PARKED-NOTELESS (10-01) are both before 2026-10-17.
        self.assertIn("| total | 3 | 6 |", render_roadmap.render(load_fixture()))

    def test_does_not_drain_when_queued_reaches_landed(self) -> None:
        text = render_roadmap.render(load_fixture())
        self.assertIn("does not drain (queued >= landed)", text)
        self.assertNotIn("drains in", text)

    def test_equal_intake_and_landings_does_not_drain(self) -> None:
        self.assertIn("does not drain (queued >= landed)", render_roadmap.render(synthetic(4, 7, 7)))

    def test_drains_in_whole_days(self) -> None:
        # net +7 over 7 days = 1/day, 4 open -> 4 days.
        text = render_roadmap.render(synthetic(4, 14, 7))
        self.assertIn("at the 7-day mean, product drains in 4 days", text)

    def test_drain_days_round_up(self) -> None:
        # net +3 over 7 days; 5 open -> 35/3 = 11.67 -> 12 days.
        self.assertIn("at the 7-day mean, product drains in 12 days", render_roadmap.render(synthetic(5, 10, 7)))

    def test_nothing_open_is_drained_not_never(self) -> None:
        text = render_roadmap.render(synthetic(0, 1, 3))
        self.assertIn("product is drained (0 open)", text)
        self.assertNotIn("does not drain", text)

    def test_merged_row_with_no_timestamp_but_its_stamp_lands_on_that_day(self) -> None:
        data = synthetic(1, 0, 0)
        data["rows"].append(row("LANDED-BY-STAMP", "merged", "2026-10-20T05:00:00Z"))
        self.assertIn("| 2026-10-20 | 1 | 1 |", render_roadmap.render(data))


class OutputShapeTests(unittest.TestCase):
    def test_deterministic_and_dated_only_by_the_card_queue(self) -> None:
        a = render_roadmap.render(load_fixture())
        b = render_roadmap.render(load_fixture())
        self.assertEqual(a, b)
        self.assertIn("generated_from: 2026-10-23T12:00:00Z", a)
        # A different generated_utc is the only thing that may move the dated lines.
        other = load_fixture()
        other["generated_utc"] = "2026-10-24T12:00:00Z"
        self.assertNotEqual(a, render_roadmap.render(other))

    def test_row_order_in_the_input_does_not_matter(self) -> None:
        data = load_fixture()
        shuffled = copy.deepcopy(data)
        shuffled["rows"].reverse()
        self.assertEqual(render_roadmap.render(data), render_roadmap.render(shuffled))

    def test_header_names_card_queue_as_the_source_and_disclaims_it_as_gitignored(self) -> None:
        text = render_roadmap.render(load_fixture())
        self.assertIn("card-queue.json", text)
        self.assertIn("gitignored", text)
        self.assertNotIn("dual-lane/queue.json", text)

    def test_ends_with_one_newline_and_has_no_carriage_returns(self) -> None:
        text = render_roadmap.render(load_fixture())
        self.assertTrue(text.endswith("\n") and not text.endswith("\n\n"))
        self.assertNotIn("\r", text)


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "roadmap.md"

    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=60)

    def test_writes_the_rendered_file(self) -> None:
        proc = self.run_cli("--card-queue", str(FIXTURE), "--out", str(self.out))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.out.read_bytes().decode("utf-8"), render_roadmap.render(load_fixture()))

    def test_check_passes_on_a_fresh_file_and_fails_on_a_drifted_one(self) -> None:
        self.run_cli("--card-queue", str(FIXTURE), "--out", str(self.out))
        ok = self.run_cli("--card-queue", str(FIXTURE), "--out", str(self.out), "--check")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        before = self.out.read_bytes()
        self.out.write_text(self.out.read_text(encoding="utf-8") + "extra line\n", encoding="utf-8")
        bad = self.run_cli("--card-queue", str(FIXTURE), "--out", str(self.out), "--check")
        self.assertEqual(bad.returncode, 1)
        self.assertNotEqual(self.out.read_bytes(), before, "--check must not rewrite the file")

    def test_check_does_not_write(self) -> None:
        self.run_cli("--card-queue", str(FIXTURE), "--out", str(self.out), "--check")
        self.assertFalse(self.out.exists())

    def test_check_fails_when_the_file_is_missing(self) -> None:
        self.assertEqual(self.run_cli("--card-queue", str(FIXTURE), "--out", str(self.out), "--check").returncode, 1)

    def test_check_tolerates_a_crlf_checkout(self) -> None:
        self.run_cli("--card-queue", str(FIXTURE), "--out", str(self.out))
        self.out.write_bytes(self.out.read_bytes().replace(b"\n", b"\r\n"))
        self.assertEqual(self.run_cli("--card-queue", str(FIXTURE), "--out", str(self.out), "--check").returncode, 0)

    def test_drain_line_prints_one_line_and_needs_no_out(self) -> None:
        proc = self.run_cli("--card-queue", str(FIXTURE), "--drain-line")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout,
            "Product drain: 4 open, 3 landed vs 6 queued over 7 UTC days "
            "(card-queue 2026-10-23T12:00:00Z): does not drain (queued >= landed)\n",
        )

    def test_drain_line_writes_nothing_even_with_out(self) -> None:
        self.run_cli("--card-queue", str(FIXTURE), "--out", str(self.out), "--drain-line")
        self.assertFalse(self.out.exists())

    def test_drain_line_function_carries_the_days(self) -> None:
        self.assertIn("drains in 4 days", render_roadmap.drain_line(synthetic(4, 14, 7)))

    def test_out_is_required_unless_drain_line(self) -> None:
        self.assertEqual(self.run_cli("--card-queue", str(FIXTURE)).returncode, 2)

    def test_card_queue_is_required(self) -> None:
        self.assertEqual(self.run_cli("--out", str(self.out)).returncode, 2)

    def test_missing_or_foreign_card_queue_exits_2_without_touching_out(self) -> None:
        missing = self.run_cli("--card-queue", str(Path(self.tmp.name) / "nope.json"), "--out", str(self.out))
        self.assertEqual(missing.returncode, 2)
        foreign = Path(self.tmp.name) / "foreign.json"
        foreign.write_text(json.dumps({"schema": "something/else", "rows": []}), encoding="utf-8")
        self.assertEqual(self.run_cli("--card-queue", str(foreign), "--out", str(self.out)).returncode, 2)
        self.assertFalse(self.out.exists())


class InputValidationTests(unittest.TestCase):
    """Malformed card-queue values exit 2 naming file, row and field; they never traceback (exit 1)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.queue = Path(self.tmp.name) / "card-queue.json"
        self.out = Path(self.tmp.name) / "roadmap.md"

    def run_main(self, data: dict) -> tuple[int, str]:
        """main([...]) against a temp card-queue.json; returns (SystemExit code or return value, stderr)."""
        self.queue.write_text(json.dumps(data), encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                code = render_roadmap.main(["--card-queue", str(self.queue), "--out", str(self.out)])
            except SystemExit as exc:
                code = exc.code
        return code, err.getvalue()

    def assert_rejected(self, data: dict, *needles: str) -> None:
        code, err = self.run_main(data)
        self.assertEqual(code, 2, err)
        for needle in (str(self.queue), *needles):
            self.assertIn(needle, err)
        self.assertFalse(self.out.exists())

    def test_malformed_generated_utc_exits_2(self) -> None:
        data = synthetic(1, 0, 0)
        data["generated_utc"] = "not-a-date"
        self.assert_rejected(data, "generated_utc")

    def test_malformed_source_ledger_stamp_exits_2_naming_the_row(self) -> None:
        data = synthetic(2, 0, 0)
        data["rows"][1]["source_ledger_stamp"] = "2026-13-45T00:00:00Z"
        self.assert_rejected(data, "row 1", "source_ledger_stamp")

    def test_non_string_pinned_utc_exits_2_naming_the_row(self) -> None:
        data = synthetic(1, 1, 0)
        data["rows"][1]["pinned_utc"] = 20261023
        self.assert_rejected(data, "row 1", "pinned_utc")

    def test_order_chain_as_a_string_exits_2_naming_the_row(self) -> None:
        data = synthetic(2, 0, 0)
        data["rows"][1]["order_chain"] = "ORDER-1 #1 first"
        self.assert_rejected(data, "row 1", "order_chain")

    def test_order_chain_missing_index_exits_2_naming_the_row(self) -> None:
        data = synthetic(2, 0, 0)
        data["rows"][1]["order_chain"] = {"chain": "ORDER-1", "predecessor": None}
        self.assert_rejected(data, "row 1", "order_chain", "index")

    def test_other_order_chain_shapes_exit_2(self) -> None:
        for bad in ({"chain": 7, "index": 1}, {"chain": "C", "index": "1"}, {"chain": "C", "index": True},
                    {"chain": "C", "index": 1, "predecessor": 5}):
            with self.subTest(order_chain=bad):
                data = synthetic(1, 0, 0)
                data["rows"][0]["order_chain"] = bad
                self.assert_rejected(data, "row 0", "order_chain")

    def test_malformed_merged_utc_exits_2_even_on_an_unmerged_row(self) -> None:
        data = synthetic(1, 0, 0)
        data["rows"][0]["merged_utc"] = "yesterday"
        self.assert_rejected(data, "row 0", "merged_utc")

    def test_a_well_formed_queue_still_renders(self) -> None:
        data = synthetic(2, 1, 1)
        data["rows"][0]["order_chain"] = {"chain": "ORDER-1", "index": 2, "predecessor": "OPEN-1"}
        data["rows"][1]["pinned_utc"] = "2026-10-22T01:00:00Z"
        code, err = self.run_main(data)
        self.assertEqual(code, 0, err)
        self.assertIn("ORDER-1 #2 after OPEN-1", self.out.read_text(encoding="utf-8"))

    def test_merged_utc_beats_the_newest_other_stamp(self) -> None:
        # Declared 10-21 would win max(); the row's own merged_utc says it landed 10-19.
        data = synthetic(1, 0, 0)
        data["rows"].append(row("MERGED-OWN-STAMP", "merged", "2026-10-18T05:00:00Z",
                                evidence="declared@2026-10-21T05:00:00Z", pinned_utc="2026-10-22T05:00:00Z",
                                merged_utc="2026-10-19T08:00:00Z"))
        text = render_roadmap.render(data)
        self.assertIn("| 2026-10-19 | 1 | 0 |", text)
        self.assertIn("| 2026-10-21 | 0 | 0 |", text)
        self.assertIn("| 2026-10-22 | 0 | 0 |", text)

    def test_without_merged_utc_the_newest_stamp_is_still_the_approximation(self) -> None:
        data = synthetic(1, 0, 0)
        data["rows"].append(row("MERGED-NO-OWN-STAMP", "merged", "2026-10-18T05:00:00Z",
                                evidence="declared@2026-10-21T05:00:00Z", pinned_utc="2026-10-22T05:00:00Z"))
        self.assertIn("| 2026-10-22 | 1 | 0 |", render_roadmap.render(data))


class DeclaredTokenTests(unittest.TestCase):
    """A `declared@` token in `evidence` is validated as a whole token; none is silently skipped."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.queue = Path(self.tmp.name) / "card-queue.json"
        self.out = Path(self.tmp.name) / "roadmap.md"

    def run_main(self, data: dict) -> tuple[int, str]:
        self.queue.write_text(json.dumps(data), encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                code = render_roadmap.main(["--card-queue", str(self.queue), "--out", str(self.out)])
            except SystemExit as exc:
                code = exc.code
        return code, err.getvalue()

    def merged(self, evidence: str) -> dict:
        data = synthetic(1, 0, 0)
        data["rows"].append(row("MERGED-DECLARED", "merged", "2026-10-18T05:00:00Z", evidence=evidence))
        return data

    def test_malformed_declared_token_exits_2_naming_the_row_and_the_field(self) -> None:
        for bad in ("declared@garbage", "declared@2026-10-3T1:2:3Z", "declared@2026-10-21",
                    "declared@2026-10-21T05:00:00", "declared@2026-10-21T05:00:00Zjunk", "declared@"):
            with self.subTest(token=bad):
                code, err = self.run_main(self.merged("hub touched it; " + bad))
                self.assertEqual(code, 2, err)
                for needle in (str(self.queue), "row 1", "MERGED-DECLARED", "evidence", bad):
                    self.assertIn(needle, err)
                self.assertFalse(self.out.exists())

    def test_one_malformed_token_among_good_ones_still_exits_2(self) -> None:
        code, err = self.run_main(self.merged("declared@2026-10-20T05:00:00Z declared@bad declared@2026-10-21T05:00:00Z"))
        self.assertEqual(code, 2, err)
        self.assertIn("declared@bad", err)

    def test_well_formed_tokens_with_trailing_punctuation_still_render(self) -> None:
        code, err = self.run_main(self.merged("(declared@2026-10-20T05:00:00Z), then declared@2026-10-21T05:00:00Z."))
        self.assertEqual(code, 0, err)
        self.assertIn("| 2026-10-21 | 1 | 0 |", self.out.read_text(encoding="utf-8"))

    def test_non_zero_padded_stamp_would_win_the_string_max_so_it_is_rejected(self) -> None:
        # As strings "2026-10-9..." > "2026-10-21...", so max() would land the row on the wrong day.
        self.assertGreater("2026-10-9T05:00:00Z", "2026-10-21T05:00:00Z")
        code, err = self.run_main(self.merged("declared@2026-10-21T05:00:00Z declared@2026-10-9T05:00:00Z"))
        self.assertEqual(code, 2, err)
        self.assertIn("evidence", err)

    def test_zero_padded_stamps_land_on_the_newest_day_whatever_the_token_order(self) -> None:
        for evidence in ("declared@2026-10-09T05:00:00Z declared@2026-10-21T05:00:00Z",
                         "declared@2026-10-21T05:00:00Z declared@2026-10-09T05:00:00Z"):
            with self.subTest(evidence=evidence):
                self.assertIn("| 2026-10-21 | 1 | 0 |", render_roadmap.render(self.merged(evidence)))

    def test_same_priority_and_stamp_order_by_card_name_as_strings(self) -> None:
        data = synthetic(0, 0, 0)
        data["rows"] += [row("CARD-9", "queued", "2026-10-20T00:00:00Z"), row("CARD-10", "queued", "2026-10-20T00:00:00Z")]
        self.assertEqual(table_cards(render_roadmap.render(data)), ["CARD-10", "CARD-9"])

    def test_legend_says_merged_utc_is_preferred_and_the_fallback_is_approximate(self) -> None:
        legend = next(l for l in render_roadmap.render(load_fixture()).splitlines() if l.startswith("Landed = "))
        self.assertIn("merged_utc", legend)
        self.assertIn("approximate", legend)
        self.assertIn("queued = rows of any state by `source_ledger_stamp`", legend)


if __name__ == "__main__":
    unittest.main()
