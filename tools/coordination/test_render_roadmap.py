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
from datetime import date
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


SCOPE_NOTE = {
    "title": "macOS scope (owner ruling 2026-10-25T00:44:41Z)",
    "text": "Deferred: macOS feature, performance and GPU work (DEFERRED-ONE, DEFERRED-TWO) until the "
            "portable path is solid. Not deferred: macOS compilation stays green (KEPT-ONE, PR #9001).",
}
SCOPE_NOTE_LINE = "**%s.** %s" % (SCOPE_NOTE["title"], SCOPE_NOTE["text"])


class ScopeNoteTests(unittest.TestCase):
    """A scope note (for example the owner's macOS ruling) renders only from card-queue data, never from code."""

    def with_notes(self, *notes: dict) -> dict:
        data = load_fixture()
        data["scope_notes"] = list(notes)
        return data

    def test_a_queue_without_scope_notes_renders_no_note(self) -> None:
        text = render_roadmap.render(load_fixture())
        self.assertNotIn("scope", text.lower().replace("scoped", ""))
        self.assertNotIn("Not deferred", text)
        self.assertNotRegex(text, r"PR #\d+")

    def test_a_note_never_names_a_pr_number_or_card_that_is_not_in_the_queue_data(self) -> None:
        text = render_roadmap.render(self.with_notes({"title": "Other ruling", "text": "Plain text only."}))
        self.assertNotRegex(text, r"PR #\d+")
        for card in ("MAC-LINUX-BUILD-RESTORE-1", "MAC-MINI-BUILD-HOST-1", "GPU-METAL-BACKEND-1"):
            self.assertNotIn(card, text)

    def test_the_renderer_source_holds_no_pr_number_or_ruling_card_literal(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotRegex(source, r"PR #\d+")
        for literal in ("MAC-LINUX-BUILD-RESTORE-1", "CPU-NEON-HOT-KERNELS-1", "2026-10-25T00:44:41Z"):
            self.assertNotIn(literal, source)

    def test_note_sits_beside_the_open_table_and_the_parked_section(self) -> None:
        text = render_roadmap.render(self.with_notes(SCOPE_NOTE))
        self.assertEqual(text.count(SCOPE_NOTE_LINE), 2)
        open_part, rest = text.split("## Drain")
        self.assertIn(SCOPE_NOTE_LINE, open_part)
        self.assertLess(open_part.index(SCOPE_NOTE_LINE), open_part.index("| P | card |"))
        self.assertIn(SCOPE_NOTE_LINE, rest.split("## Parked with reason")[1])

    def test_note_text_is_data_so_the_not_deferred_clause_survives_verbatim(self) -> None:
        text = render_roadmap.render(self.with_notes(SCOPE_NOTE))
        self.assertIn("Not deferred: macOS compilation stays green (KEPT-ONE, PR #9001).", text)

    def test_several_notes_render_in_queue_order(self) -> None:
        second = {"title": "Second", "text": "Later note."}
        text = render_roadmap.render(self.with_notes(SCOPE_NOTE, second))
        part = text.split("## Drain")[0]
        self.assertLess(part.index(SCOPE_NOTE_LINE), part.index("**Second.** Later note."))

    def test_empty_scope_notes_list_renders_no_note(self) -> None:
        self.assertEqual(render_roadmap.render(self.with_notes()), render_roadmap.render(load_fixture()))

    def test_a_note_is_rendered_by_the_cli_from_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue, out = Path(tmp) / "card-queue.json", Path(tmp) / "roadmap.md"
            queue.write_text(json.dumps(self.with_notes(SCOPE_NOTE)), encoding="utf-8")
            done = subprocess.run([sys.executable, str(SCRIPT), "--card-queue", str(queue), "--out", str(out)],
                                  capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(out.read_text(encoding="utf-8").count(SCOPE_NOTE_LINE), 2)


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

    def test_scope_notes_that_is_not_a_list_exits_2(self) -> None:
        data = synthetic(1, 0, 0)
        data["scope_notes"] = "a string"
        self.assert_rejected(data, "scope_notes")

    def test_scope_note_without_string_title_and_text_exits_2_naming_the_entry(self) -> None:
        for bad in ({"text": "no title"}, {"title": "no text"}, {"title": 1, "text": "x"},
                    {"title": "t", "text": 2}, {"title": "", "text": "x"}, "bare string"):
            with self.subTest(note=bad):
                data = synthetic(1, 0, 0)
                data["scope_notes"] = [bad]
                self.assert_rejected(data, "scope_notes", "entry 0")

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


class OwnerAskTests(unittest.TestCase):
    """OWNER-ASK-PARK-GUARD-1 (d): origin=owner rows count whatever their kind, and parked owner asks get their own section."""

    def owner_board(self) -> dict:
        data = load_fixture()
        data["rows"] += [
            row("OWNER-TOOL-OPEN", "queued", "2026-10-22T10:00:00Z", kind="hub-tooling", priority=2, origin="owner"),
            row("OWNER-UNC-PARKED", "parked", "2026-10-13T08:00:00Z", kind="unclassified", priority=3, origin="owner",
                notes="INTAKE-GUARD 2026-10-13: queued>landed 3 days; park, not drop"),
            row("OWNER-PROD-PARKED", "parked", "2026-10-01T08:00:00Z", kind="product", priority=6, origin="owner",
                notes="throughput swarm 3", pinned_utc="2026-10-20T08:00:00Z"),
            row("HUB-TOOL-PARKED", "parked", "2026-10-22T10:00:00Z", kind="hub-tooling", priority=3, origin="hub",
                notes="not an owner ask"),
        ]
        return data

    def test_an_open_owner_ask_of_any_kind_is_on_the_open_table_and_counted(self) -> None:
        text = render_roadmap.render(self.owner_board())
        self.assertIn("OWNER-TOOL-OPEN", table_cards(text))
        self.assertIn("| 2 | OWNER-TOOL-OPEN | hub-tooling (owner) | queued | 2026-10-22 | - |", text)
        self.assertIn("product+playback open: 5 (incl. 1 owner ask of another kind)", text)

    def test_a_non_owner_tooling_row_is_still_not_counted(self) -> None:
        text = render_roadmap.render(self.owner_board())
        self.assertNotIn("TOOLING-J-OPEN", table_cards(text))
        self.assertNotIn("HUB-TOOL-PARKED", text)

    def test_owner_intake_counts_in_the_queued_column(self) -> None:
        # fixture 10-22 has OPEN-B (1 queued); OWNER-TOOL-OPEN adds one more on 10-22, OWNER-UNC-PARKED is outside the window
        self.assertIn("| 2026-10-22 | 1 | 2 |", render_roadmap.render(self.owner_board()))

    def test_parked_owner_asks_section_lists_reason_and_age(self) -> None:
        text = render_roadmap.render(self.owner_board())
        self.assertIn("## Parked owner asks", text)
        section = text.split("## Parked owner asks")[1].split("## Parked with reason")[0]
        self.assertIn("- OWNER-UNC-PARKED (P3, unclassified): parked 10 d (since 2026-10-13): INTAKE-GUARD 2026-10-13", section)
        # the park age counts from pinned_utc when the row has one (the hub's -Set park), else from source_ledger_stamp
        self.assertIn("- OWNER-PROD-PARKED (P6, product): parked 3 d (since 2026-10-20): throughput swarm 3", section)
        self.assertLess(section.index("OWNER-UNC-PARKED"), section.index("OWNER-PROD-PARKED"), "the longest-parked ask comes first")

    def test_owner_ask_reason_is_cut_at_100_chars(self) -> None:
        data = self.owner_board()
        data["rows"].append(row("OWNER-LONG-PARKED", "parked", "2026-10-15T08:00:00Z", kind="hygiene", origin="owner", notes="x" * 300))
        line = next(l for l in render_roadmap.render(data).splitlines() if l.startswith("- OWNER-LONG-PARKED"))
        self.assertTrue(line.endswith("x" * 97 + "..."), line)
        self.assertNotIn("x" * 98, line)

    def test_an_owner_ask_is_not_listed_twice(self) -> None:
        reason = render_roadmap.render(self.owner_board()).split("## Parked with reason")[1]
        self.assertNotIn("OWNER-UNC-PARKED", reason)
        self.assertNotIn("OWNER-PROD-PARKED", reason)
        self.assertIn("PARKED-E", reason)

    def test_a_terminal_owner_park_is_not_an_undelivered_ask(self) -> None:
        # r2 A1: only the structured `resolution` field (written by Update-CardQueue.ps1 -Set -State parked -Resolution) closes an ask.
        data = self.owner_board()
        data["rows"] += [
            row("OWNER-DONE-PARKED", "parked", "2026-10-14T08:00:00Z", kind="factory", priority=2, origin="owner",
                notes="closed DONE by HUB-TICK 20261027T0931Z, no PR by design", resolution="done"),
            row("OWNER-SUPERSEDED-PARKED", "parked", "2026-10-14T08:00:00Z", kind="unclassified", priority=3, origin="owner",
                notes="superseded by OWNER-OTHER-3 r2", resolution="superseded"),
            row("OWNER-PRODUCT-NOGO", "parked", "2026-10-14T08:00:00Z", kind="product", priority=2, origin="owner",
                notes="design r1 says no", resolution="no_go"),
        ]
        text = render_roadmap.render(data)
        asks = text.split("## Parked owner asks")[1].split("## Parked with reason")[0]
        reason = text.split("## Parked with reason")[1]
        for card in ("OWNER-DONE-PARKED", "OWNER-SUPERSEDED-PARKED", "OWNER-PRODUCT-NOGO"):
            self.assertNotIn(card, asks)
        # a resolved owner ask of another kind is tooling churn like any other: counted on one line, not listed one by one
        self.assertNotIn("OWNER-DONE-PARKED", reason)
        self.assertIn("- 2 owner asks of another kind are closed with a resolution (done 1, superseded 1; card-queue.json)", reason)
        self.assertIn("- OWNER-PRODUCT-NOGO (P2, owner ask, no_go): design r1 says no", reason)
        self.assertIn("OWNER-UNC-PARKED", asks)

    # adversary pass 1 (review-adversary-OWNER-ASK-PARK-GUARD-1-r1-83d7e0ff) A1/A2/A3: each finding's input, verbatim.
    def one_owner(self, notes: str, stamp: str = "2026-10-22T23:59:00Z", **extra) -> dict:
        return {"schema": "mlv-app/card-queue/v1", "generated_utc": "2026-10-23T00:00:00Z",
                "rows": [row("OWNER-1", "parked", stamp, kind="product", origin="owner", notes=notes, **extra)]}

    def owed(self, data: dict) -> str:
        return render_roadmap.render(data).split("## Parked owner asks")[1].split("## Parked with reason")[0]

    def test_a1_an_unfinished_ask_naming_done_or_a_closed_card_stays_owed(self) -> None:
        for notes in ("waits for #123 to be done", "not DONE yet; waits for #123", "until it has shipped",
                      "stacked after CLOSED-CAPTIONS-1", "waits for NO-GO-GATE-1 and DUPLICATE-FRAMES-2 to land",
                      "never superseded by anything", "not closed by the hub"):
            with self.subTest(notes=notes):
                self.assertIn("OWNER-1", self.owed(self.one_owner(notes)))

    def test_r2_a1_a_terminal_park_word_without_a_resolution_stays_owed(self) -> None:
        # deny by default (fleet card mlv-app/spelling-chase-scanner-needs-a-deny-by-default-name): no note wording closes an owner ask
        for notes in ("closed DONE by HUB-TICK 20261027T0931Z", "r3 DONE verdict SPEEDUP", "design r1 NO_GO",
                      "parked DUPLICATE of OTHER-1", "superseded by OTHER-3 r2 DONE", "shipped as OTHER-2 r2",
                      "closed by HUB RULING 2026-11-05T01:19:19Z", "question answered, no successor", "not new work: legacy row"):
            with self.subTest(notes=notes):
                self.assertIn("OWNER-1", self.owed(self.one_owner(notes)))
                for resolution in render_roadmap.RESOLUTIONS:
                    self.assertNotIn("OWNER-1", self.owed(self.one_owner(notes, resolution=resolution)))

    def test_a2_age_is_elapsed_whole_days_not_a_date_difference(self) -> None:
        data = self.one_owner("waiting on #123", pinned_utc="2026-10-22T23:59:00Z")
        self.assertIn("- OWNER-1 (P3, product): parked 0 d (since 2026-10-22)", self.owed(data))
        data = self.one_owner("waiting on #123", stamp="2026-10-20T00:00:01Z")
        self.assertIn("parked 2 d (since 2026-10-20)", self.owed(data))

    def test_a3_longest_parked_first_compares_instants_not_strings(self) -> None:
        data = self.one_owner("waiting on #123")
        data["rows"] = [
            row("NEWER", "parked", "2026-10-22T23:59:00Z", kind="product", origin="owner", notes="waiting on #123",
                pinned_utc="2026-10-22T08:00:00.100Z"),
            row("OLDER", "parked", "2026-10-22T23:59:00Z", kind="product", origin="owner", notes="waiting on #123",
                pinned_utc="2026-10-22T08:00:00Z"),
        ]
        owed = self.owed(data)
        self.assertLess(owed.index("OLDER"), owed.index("NEWER"))

    def test_a3_sibling_open_rows_and_landing_compare_instants(self) -> None:
        data = synthetic(0, 0, 0)
        data["rows"] += [row("B-NEWER", "queued", "2026-10-20T08:00:00.100Z"), row("A-OLDER", "queued", "2026-10-20T08:00:00Z")]
        self.assertEqual(table_cards(render_roadmap.render(data)), ["A-OLDER", "B-NEWER"])
        landed = row("L", "merged", "2026-10-21T23:59:59.900Z", pinned_utc="2026-10-21T23:59:59Z")
        self.assertEqual(render_roadmap._landed_stamp(landed), "2026-10-21T23:59:59.900Z")

    # adversary pass 2 (review-adversary-OWNER-ASK-PARK-GUARD-1-r1-8d0a0bfd) A1-A4, A6, A7: each finding's input, verbatim.
    def test_p2_a1_a_condition_anywhere_earlier_in_the_clause_keeps_the_ask_owed(self) -> None:
        for notes in ("waits until #123 has shipped", "until BLOCKER-1 has shipped", "until the release branch has shipped"):
            with self.subTest(notes=notes):
                self.assertIn("OWNER-1", self.owed(self.one_owner(notes, stamp="2026-10-20T00:00:00Z")))

    def test_p2_a2_any_whitespace_inside_a_terminal_phrase_is_still_only_text(self) -> None:
        # r2 A1: the phrase no longer decides anything, however it is spaced; the resolution field does
        for notes in ("superseded  by OWNER-2", "superseded\nby OWNER-2"):
            with self.subTest(notes=notes):
                self.assertIn("OWNER-1", self.owed(self.one_owner(notes, stamp="2026-10-20T00:00:00Z")))
                self.assertNotIn("OWNER-1", self.owed(self.one_owner(notes, stamp="2026-10-20T00:00:00Z", resolution="superseded")))

    def test_p2_a3_a7_an_offset_stamp_counts_on_its_utc_day(self) -> None:
        data = {"schema": "mlv-app/card-queue/v1", "generated_utc": "2026-10-23T00:00:00Z",
                "rows": [row("OWNER-1", "merged", "2026-10-22T23:30:00-02:00", origin="owner", notes="waiting for #123",
                             pinned_utc="2026-10-23T00:30:00Z")]}
        text = render_roadmap.render(data)
        self.assertIn("| 2026-10-23 | 1 | 1 |", text)
        self.assertIn("| 2026-10-22 | 0 | 0 |", text)

    def test_p2_a4_seven_digit_fractions_are_not_truncated(self) -> None:
        data = {"schema": "mlv-app/card-queue/v1", "generated_utc": "2026-10-23T00:00:00.0000001Z",
                "rows": [row("OWNER-1", "parked", "2026-10-20T00:00:00Z", origin="owner", notes="waits for #123",
                             pinned_utc="2026-10-22T00:00:00.0000009Z")]}
        self.assertIn("parked 0 d (since 2026-10-22)", self.owed(data))
        data = synthetic(0, 0, 0)
        data["rows"] += [row("A-NEWER", "queued", "2026-10-22T08:00:00.0000009Z"), row("Z-OLDER", "queued", "2026-10-22T08:00:00.0000001Z")]
        self.assertEqual(table_cards(render_roadmap.render(data)), ["Z-OLDER", "A-NEWER"])
        landed = row("L", "merged", "2026-10-22T08:00:00.0000001Z", pinned_utc="2026-10-22T08:00:00.0000009Z")
        self.assertEqual(render_roadmap._landed_stamp(landed), "2026-10-22T08:00:00.0000009Z")

    def test_p2_a6_a_far_future_age_is_exact(self) -> None:
        data = {"schema": "mlv-app/card-queue/v1", "generated_utc": "9999-12-31T00:00:00Z",
                "rows": [row("OWNER-1", "parked", "2026-10-20T00:00:00Z", origin="owner", notes="waits for #123",
                             pinned_utc="7262-02-02T00:00:00.000001Z")]}
        days = (date(9999, 12, 31) - date(7262, 2, 2)).days - 1   # one microsecond short of a whole day
        self.assertIn("parked %d d (since 7262-02-02)" % days, self.owed(data))

    def test_no_parked_owner_ask_says_so(self) -> None:
        text = render_roadmap.render(load_fixture())
        self.assertIn("## Parked owner asks", text)
        self.assertIn("- (no owner ask is parked)", text.split("## Parked owner asks")[1])
        self.assertIn("product+playback open: 4\n", text)

    def test_non_string_origin_exits_2_naming_the_row(self) -> None:
        data = load_fixture()
        data["rows"][0]["origin"] = 1
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "q.json"
            p.write_text(json.dumps(data), encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as cm:
                render_roadmap._load(p)
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("origin", err.getvalue())
        self.assertIn("OPEN-A-P2", err.getvalue())


_parity_spec = importlib.util.spec_from_file_location(
    "test_roadmap_queue_parity", HERE.parent / "repo_hygiene" / "test_roadmap_queue_parity.py")
_parity = importlib.util.module_from_spec(_parity_spec)
_parity_spec.loader.exec_module(_parity)
ROADMAP_BUDGET_BYTES = _parity.ROADMAP_BUDGET_BYTES   # the budget docs/roadmap.md is held to (test_roadmap_queue_parity.py)


class R2AdversaryTests(unittest.TestCase):
    """OWNER-ASK-PARK-GUARD-1 r2: the three open majors of review-adversary-OWNER-ASK-PARK-GUARD-1-r1-8ba4f8ab, each on its input."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def board(self, *rows: dict, generated: str = "2026-10-23T00:00:00Z") -> dict:
        return {"schema": "mlv-app/card-queue/v1", "generated_utc": generated, "rows": list(rows)}

    def owner(self, card: str = "OWNER-1", notes: str = "waits for #123", **extra) -> dict:
        extra.setdefault("kind", "product")
        return row(card, "parked", extra.pop("stamp", "2026-10-20T00:00:00Z"), origin="owner", notes=notes, **extra)

    def owed(self, data: dict) -> str:
        return render_roadmap.render(data).split("## Parked owner asks")[1].split("## Parked with reason")[0]

    def load_code(self, data: dict) -> tuple[int, str]:
        p = Path(self.tmp.name) / "q.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                render_roadmap._load(p)
                return 0, err.getvalue()
            except SystemExit as exc:
                return exc.code, err.getvalue()

    # A1 (introduced, major): conditional or negated completion text is not a verdict; deny by default.
    def test_a1_the_adversary_notes_stay_owed(self) -> None:
        for notes in ("after #123 has shipped", "hasn't shipped", "blocker #123 shipped; still waiting for #456", "not\nDONE"):
            with self.subTest(notes=notes):
                self.assertIn("OWNER-1", self.owed(self.board(self.owner(notes=notes))))

    def test_a1_only_a_closed_resolution_vocabulary_is_accepted(self) -> None:
        self.assertEqual(render_roadmap.RESOLUTIONS, ("done", "duplicate", "superseded", "no_go", "refused"))
        for bad in ("DONE", "shipped", "no-go", "", " done", 1, True, ["done"]):
            with self.subTest(resolution=bad):
                code, err = self.load_code(self.board(self.owner(resolution=bad)))
                self.assertEqual(code, 2, err)
                self.assertIn("resolution", err)
                self.assertIn("OWNER-1", err)
        for good in render_roadmap.RESOLUTIONS:
            with self.subTest(resolution=good):
                self.assertEqual(self.load_code(self.board(self.owner(resolution=good)))[0], 0)

    def test_a1_a_resolution_closes_the_ask_whatever_the_note_says(self) -> None:
        data = self.board(self.owner(notes="waits for #123 until it ships", resolution="refused"))
        self.assertNotIn("OWNER-1", self.owed(data))
        self.assertIn("- OWNER-1 (P3, owner ask, refused): waits for #123 until it ships", render_roadmap.render(data))

    def test_a1_a_null_resolution_is_absent_and_the_ask_is_owed(self) -> None:
        self.assertIn("OWNER-1", self.owed(self.board(self.owner(resolution=None))))

    def test_a1_the_renderer_holds_no_terminal_word_list(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        for gone in ("TERMINAL_PARK_RXS", "_terminal_park", "_CONDITION_RX", "superseded\\s+by"):
            self.assertNotIn(gone, source)

    # A2 (introduced, major): fractional seconds are exact, with no Decimal context.
    def test_a2_a_19_digit_fraction_is_not_rounded_into_the_next_day(self) -> None:
        data = self.board(self.owner(pinned_utc="2026-10-22T00:00:00.0000000000000000001Z"))
        self.assertIn("- OWNER-1 (P3, product): parked 0 d (since 2026-10-22)", self.owed(data))

    def test_a2_instants_order_newest_stamp_and_utc_day_are_exact_at_any_length(self) -> None:
        tiny, tinier = "2026-10-22T08:00:00.00000000000000000000000000002Z", "2026-10-22T08:00:00.00000000000000000000000000001Z"
        self.assertGreater(render_roadmap._instant(tiny), render_roadmap._instant(tinier))
        self.assertGreater(render_roadmap._instant(tinier), render_roadmap._instant("2026-10-22T08:00:00Z"))
        data = synthetic(0, 0, 0)
        data["rows"] += [row("A-NEWER", "queued", tiny), row("Z-OLDER", "queued", tinier)]
        self.assertEqual(table_cards(render_roadmap.render(data)), ["Z-OLDER", "A-NEWER"])
        self.assertEqual(render_roadmap._landed_stamp(row("L", "merged", tinier, pinned_utc=tiny)), tiny)
        self.assertEqual(render_roadmap._utc_day("2026-10-22T23:59:59." + "9" * 40 + "Z"), date(2026, 10, 22))
        self.assertNotIn("decimal", SCRIPT.read_text(encoding="utf-8").lower().replace("non-decimal", ""))

    def test_a2_the_age_is_floor_of_exact_elapsed_days(self) -> None:
        data = self.board(self.owner(pinned_utc="2026-10-21T00:00:00.0000000000000000001Z"))
        self.assertIn("parked 1 d (since 2026-10-21)", self.owed(data))   # 2 d minus 1e-19 s
        data = self.board(self.owner(pinned_utc="2026-10-21T00:00:00Z"), generated="2026-10-23T00:00:00.0000000000000000001Z")
        self.assertIn("parked 2 d (since 2026-10-21)", self.owed(data))

    # A3 (introduced, major): the owner section is capped so docs/roadmap.md stays inside its byte budget.
    def adversary_a3_board(self, n: int = 112) -> dict:
        rows = [row("OWNER-%03d" % i, "parked", "2026-10-20T00:00:00Z", kind="hub-tooling", origin="owner",
                    notes="x" * (100 if i < n - 1 else 64)) for i in range(n)]
        return self.board(*rows)

    def test_a3_112_parked_owner_rows_with_100_char_notes_fit_the_budget(self) -> None:
        text = render_roadmap.render(self.adversary_a3_board())
        self.assertLessEqual(render_roadmap._budget_size(text), ROADMAP_BUDGET_BYTES)
        owed = text.split("## Parked owner asks")[1].split("## Parked with reason")[0]
        self.assertEqual(sum(1 for l in owed.splitlines() if l.startswith("- OWNER-")), render_roadmap.OWNER_LIST_MAX)
        self.assertIn("- ... and %d more owner asks parked (card-queue.json)" % (112 - render_roadmap.OWNER_LIST_MAX), owed)

    def test_a3_the_cap_keeps_the_longest_parked_rows(self) -> None:
        rows = [self.owner("OWNER-%03d" % i, stamp="2026-10-%02dT00:00:00Z" % (1 + i % 20)) for i in range(30)]
        owed = self.owed(self.board(*rows))
        listed = [l.split()[1] for l in owed.splitlines() if l.startswith("- OWNER-")]
        expect = sorted(rows, key=lambda r: (r["source_ledger_stamp"], r["priority"], r["card"]))[:render_roadmap.OWNER_LIST_MAX]
        self.assertEqual(listed, [r["card"] for r in expect])
        self.assertIn("- ... and 5 more owner asks parked (card-queue.json)", owed)

    def test_a3_at_the_cap_no_more_line_is_written(self) -> None:
        owed = self.owed(self.adversary_a3_board(render_roadmap.OWNER_LIST_MAX))
        self.assertNotIn("more owner asks parked", owed)

    def test_a3_sibling_open_owner_asks_of_another_kind_are_capped_too(self) -> None:
        rows = [row("OWNER-%03d" % i, "queued", "2026-10-20T00:00:00Z", kind="hub-tooling", origin="owner", notes="x" * 100)
                for i in range(400)]
        rows.append(row("PRODUCT-1", "queued", "2026-10-21T00:00:00Z"))
        text = render_roadmap.render(self.board(*rows))
        self.assertLessEqual(render_roadmap._budget_size(text), ROADMAP_BUDGET_BYTES)
        cards = table_cards(text)
        self.assertIn("PRODUCT-1", cards)   # a product row is never dropped
        self.assertEqual(sum(1 for c in cards if c.startswith("OWNER-")), render_roadmap.OWNER_LIST_MAX)
        self.assertIn("| - | ... and %d more open owner asks of another kind (card-queue.json) | - | - | - | - |"
                      % (400 - render_roadmap.OWNER_LIST_MAX), text)
        self.assertIn("product+playback open: 401 (incl. 400 owner asks of another kind)", text)

    def test_a3_sibling_resolved_owner_asks_of_another_kind_are_one_line(self) -> None:
        rows = [row("OWNER-%03d" % i, "parked", "2026-10-20T00:00:00Z", kind="factory", origin="owner", notes="x" * 140,
                    resolution=render_roadmap.RESOLUTIONS[i % 5]) for i in range(400)]
        text = render_roadmap.render(self.board(*rows))
        self.assertLessEqual(render_roadmap._budget_size(text), ROADMAP_BUDGET_BYTES)
        self.assertIn("- 400 owner asks of another kind are closed with a resolution "
                      "(done 80, duplicate 80, superseded 80, no_go 80, refused 80; card-queue.json)", text)


class R2Pass4Tests(R2AdversaryTests):
    """r2 pass 4 (review-adversary-OWNER-ASK-PARK-GUARD-1-r2-72580ab2): A1-A4, A6, A7 introduced; A5 (pre-existing) fixed with them.
    The class fix: a row the renderer cannot read exactly is bad input (exit 2), and owner lines never push the file over budget."""

    def assert_rejected(self, data: dict, *needles: str) -> None:
        code, err = self.load_code(data)
        self.assertEqual(code, 2, err)
        for needle in needles:
            self.assertIn(needle, err)

    def test_p4_a1_an_overlong_fraction_is_bad_input_not_a_crash(self) -> None:
        self.assert_rejected(self.board(self.owner(pinned_utc="2026-10-22T00:00:00." + "0" * 4300 + "1Z")), "pinned_utc")
        ok = self.board(self.owner(pinned_utc="2026-10-22T00:00:00." + "0" * 99 + "1Z"))
        self.assertEqual(self.load_code(ok)[0], 0)
        self.assertIn("parked 0 d (since 2026-10-22)", self.owed(ok))

    def test_p4_a2_owner_lines_never_push_the_file_over_budget(self) -> None:
        resolved = [row("OWNER-%03d" % i, "parked", "2026-10-20T00:00:00Z", origin="owner", resolution="done", notes="x" * 100)
                    for i in range(140)]
        self.assertLessEqual(render_roadmap._budget_size(render_roadmap.render(self.board(*resolved))), ROADMAP_BUDGET_BYTES)
        self.assertIn("more owner asks closed with a resolution (card-queue.json)", render_roadmap.render(self.board(*resolved)))
        self.assert_rejected(self.board(*[row("OWNER-%03d" % i, "queued", "2026-10-20T00:00:00Z", kind="qa-" + "x" * 800,
                                              origin="owner", notes="x" * 100) for i in range(25)]), "kind")
        big = [row(("O%03d-" % i) + "C" * 115, st, "2026-10-20T00:00:00Z", kind="k" * 40, origin="owner", notes="x" * 300,
                   **({"resolution": "done"} if st == "parked" and i % 2 else {}))
               for i in range(150) for st in ("queued", "parked")]
        big += [row("PRODUCT-%d" % i, "queued", "2026-10-21T00:00:00Z") for i in range(5)]
        big += [row(("P%03d-" % i) + "C" * 115, "parked", "2026-10-20T00:00:00Z", origin="owner", resolution="no_go", notes="x" * 300)
                for i in range(150)]
        text = render_roadmap.render(self.board(*big))
        self.assertLessEqual(render_roadmap._budget_size(text), ROADMAP_BUDGET_BYTES)
        for i in range(5):
            self.assertIn("PRODUCT-%d" % i, table_cards(text))   # a product row is never dropped to fit

    def test_p4_a2_the_renderer_and_the_parity_test_share_one_budget(self) -> None:
        self.assertEqual(render_roadmap.ROADMAP_BUDGET_BYTES, ROADMAP_BUDGET_BYTES)

    def test_p4_a3_a_kind_or_card_that_would_split_a_table_row_is_bad_input(self) -> None:
        for kind in ("qa|tooling", "qa\ntooling", "qa\rtooling", "", " lead"):
            with self.subTest(kind=kind):
                self.assert_rejected(self.board(row("OWNER-1", "queued", "2026-10-20T00:00:00Z", kind=kind, origin="owner")), "kind")
        for card in ("OWNER|1", "OWNER\n1", "", "C" * 121):
            with self.subTest(card=card):
                self.assert_rejected(self.board(row(card, "queued", "2026-10-20T00:00:00Z", kind="hub-tooling", origin="owner")), "card")

    def test_p4_a4_a_non_string_card_is_bad_input(self) -> None:
        for card in ([], {}, {"id": "x"}, 1, 1.5, True):
            with self.subTest(card=card):
                self.assert_rejected(self.board(row(card, "queued", "2026-10-20T00:00:00Z", kind="hub-tooling"),
                                                row("OWNER-2", "queued", "2026-10-20T00:00:00Z", kind="hub-tooling")), "card")

    def test_p4_a6_a_window_before_year_1_is_bad_input(self) -> None:
        self.assert_rejected(self.board(generated="0001-01-07T00:30:00+01:00"), "generated_utc")
        self.assertEqual(self.load_code(self.board(generated="0001-01-07T00:30:00Z"))[0], 0)

    def test_p4_a7_a_stamp_outside_the_utc_date_range_is_bad_input(self) -> None:
        self.assert_rejected(self.board(row("Q-1", "queued", "9999-12-31T23:30:00-01:00"), generated="9999-12-31T12:00:00Z"),
                             "source_ledger_stamp")
        self.assert_rejected(self.board(row("Q-1", "queued", "0001-01-01T00:30:00+01:00")), "source_ledger_stamp")

    def test_p4_a5_an_impossible_clock_is_bad_input(self) -> None:
        for stamp in ("2026-10-20T99:99:99Z", "2026-10-20T24:00:00Z", "2026-10-20T23:60:00Z", "2026-10-20T23:59:60Z",
                      "2026-10-20T10:00:00+24:00", "2026-10-20T10:00:00+05:60", "2026-10-20T10:00:00Z junk", "2026-10-20 junk"):
            with self.subTest(stamp=stamp):
                self.assert_rejected(self.board(row("Q-1", "merged", stamp)), "source_ledger_stamp")
        for stamp in ("2026-10-20", "2026-10-20T10:00Z", "2026-10-20 10:00:00", "2026-10-20T10:00:00.5+05:30"):
            with self.subTest(stamp=stamp):
                self.assertEqual(self.load_code(self.board(row("Q-1", "merged", stamp)))[0], 0)


class R2Pass5Tests(unittest.TestCase):
    """r2 pass 5 (review-adversary-OWNER-ASK-PARK-GUARD-1-r2-e59a30f8) A2/A3: each finding's input."""

    def load_code(self, data: dict) -> int:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "q.json"
            p.write_text(json.dumps(data), encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                try:
                    render_roadmap._load(p)
                    return 0
                except SystemExit as exc:
                    return exc.code

    def board(self, generated: str = "2026-10-23T00:00:00Z", notes: str = "waits for #123") -> dict:
        return {"schema": "mlv-app/card-queue/v1", "generated_utc": generated,
                "rows": [row("OWNER-1", "parked", "2026-10-20T00:00:00Z", kind="hub-tooling", origin="owner", notes=notes)]}

    def test_p5_a2_a_stamp_with_a_carriage_return_or_line_break_is_bad_input(self) -> None:
        for g in ("\r2026-10-23T00:00:00Z", "2026-10-23T00:00:00Z\r", "2026-10-23T00:00:00\nZ", "2026-10-23T00:00:00Z\n", " 2026-10-23T00:00:00Z"):
            with self.subTest(generated=g):
                self.assertEqual(self.load_code(self.board(generated=g)), 2)
        self.assertEqual(self.load_code(self.board()), 0)

    def test_p5_a3_a_lone_surrogate_is_bad_input_not_an_encode_crash(self) -> None:
        self.assertEqual(self.load_code(self.board(notes="\ud800")), 2)
        data = self.board()
        data["scope_notes"] = [{"title": "t", "text": "\udfff"}]
        self.assertEqual(self.load_code(data), 2)


class R3Tests(unittest.TestCase):
    """OWNER-ASK-PARK-GUARD-1 r3: the three open majors of review-adversary-OWNER-ASK-PARK-GUARD-1-r2-6c9167ce, each on its input.
    A1 owner rows add zero bytes once cap 0 is over budget; A2 every string in the queue must be UTF-8; A3 no whitespace in a stamp."""

    STAMP = "2026-10-20T00:00:00Z"
    _boundary_n: "int | None" = None   # boundary_note_length(), derived once

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def board(self, *rows: dict, generated: str = "2026-10-23T00:00:00Z") -> dict:
        return {"schema": "mlv-app/card-queue/v1", "generated_utc": generated, "rows": list(rows)}

    def write(self, data: dict) -> Path:
        p = Path(self.tmp.name) / "q.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return p

    def load_code(self, data: dict) -> tuple[int, str]:
        p = self.write(data)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                render_roadmap._load(p)
                return 0, err.getvalue()
            except SystemExit as exc:
                return exc.code, err.getvalue()

    def check_code(self, data: dict) -> int:
        """main --check on an existing stale output: must return a status (2 for bad input), never raise."""
        p = self.write(data)
        out = Path(self.tmp.name) / "roadmap.md"
        out.write_bytes(b"stale\n")
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                return render_roadmap.main(["--card-queue", str(p), "--out", str(out), "--check"])
            except SystemExit as exc:
                return exc.code

    # A1 (introduced, major): at cap 0 the owner count lines, the "(incl. N ...)" suffix and the owner stats still added bytes.
    def boundary_note_length(self) -> int:
        """The longest scope note that keeps the 300-product-row board within 2 bytes of the budget as the budget is measured (r4: the
        CRLF form). Before r4 the note was 700 characters, which put the LF text at 19998 bytes; derived now, not hard-coded."""
        if R3Tests._boundary_n is not None:
            return R3Tests._boundary_n
        rows = [row("PRODUCT-%03d" % i, "queued", self.STAMP) for i in range(300)]
        data = self.board(*rows)

        def size(n: int) -> int:
            data["scope_notes"] = [{"title": "S", "text": "x" * n}]
            return render_roadmap._budget_size(render_roadmap.render(data))

        n = 700 - max(0, size(700) - (ROADMAP_BUDGET_BYTES - 2))
        while size(n) > ROADMAP_BUDGET_BYTES - 2:
            n -= 1
        while size(n + 1) <= ROADMAP_BUDGET_BYTES - 2:
            n += 1
        R3Tests._boundary_n = n
        return n

    def a1_board(self, *extra: dict) -> dict:
        rows = [row("PRODUCT-%03d" % i, "queued", self.STAMP) for i in range(300)]
        data = self.board(*rows, *extra)
        data["scope_notes"] = [{"title": "S", "text": "x" * self.boundary_note_length()}]
        return data

    def size(self, data: dict) -> int:
        return render_roadmap._budget_size(render_roadmap.render(data))   # r4: the CRLF form, the larger one the budget is held to

    def test_a1_the_adversary_baseline_is_at_the_boundary(self) -> None:
        base = self.size(self.a1_board())
        self.assertLessEqual(base, ROADMAP_BUDGET_BYTES)
        self.assertGreaterEqual(base, ROADMAP_BUDGET_BYTES - 40)   # within one owner line of the budget, as the pass built it

    def test_a1_one_owner_ask_never_pushes_a_boundary_board_over_budget(self) -> None:
        variants = {
            "parked-tooling": row("OWNER-1", "parked", self.STAMP, kind="hub-tooling", origin="owner", notes="waiting"),
            "queued-tooling": row("OWNER-1", "queued", self.STAMP, kind="hub-tooling", origin="owner", notes="waiting"),
            "resolved-product": row("OWNER-1", "parked", self.STAMP, origin="owner", notes="waiting", resolution="done"),
            "resolved-tooling": row("OWNER-1", "parked", self.STAMP, kind="hub-tooling", origin="owner", notes="waiting",
                                    resolution="done"),
            "owed-product": row("OWNER-1", "parked", self.STAMP, origin="owner", notes="waiting"),
        }
        for name, owner in variants.items():
            with self.subTest(variant=name):
                text = render_roadmap.render(self.a1_board(owner))
                self.assertLessEqual(render_roadmap._budget_size(text), ROADMAP_BUDGET_BYTES)
                self.assertNotIn("owner ask", text.split("## Parked owner asks")[0].split("## Drain")[1])   # no (incl. ...) suffix
                self.assertNotIn("more owner asks", text)
                self.assertEqual(sum(1 for c in table_cards(text) if c.startswith("PRODUCT-")), 300)   # product rows never drop

    def test_a1_many_owner_asks_of_every_shape_add_zero_bytes_past_cap_0(self) -> None:
        owners = []
        for i in range(60):
            owners += [row("OT-%03d" % i, "queued", "2026-10-21T00:00:00Z", kind="hub-tooling", origin="owner", notes="n" * 100),
                       row("PT-%03d" % i, "parked", "2026-10-21T00:00:00Z", kind="factory", origin="owner", notes="n" * 100),
                       row("RT-%03d" % i, "parked", "2026-10-21T00:00:00Z", kind="factory", origin="owner", resolution="done"),
                       row("RP-%03d" % i, "parked", "2026-09-01T00:00:00Z", origin="owner", resolution="no_go"),
                       row("PP-%03d" % i, "parked", "2026-09-01T00:00:00Z", origin="owner", notes="n" * 100)]
        self.assertLessEqual(self.size(self.a1_board(*owners)), self.size(self.a1_board()))

    def test_a1_the_collapsed_owner_line_is_no_longer_than_the_placeholder(self) -> None:
        self.assertLessEqual(len(render_roadmap.OWNER_COLLAPSED_LINE), len("- (no owner ask is parked)"))
        text = render_roadmap.render(self.a1_board(row("OWNER-1", "parked", self.STAMP, kind="hub-tooling", origin="owner")))
        owed = text.split("## Parked owner asks")[1].split("## Parked with reason")[0]
        self.assertEqual([l for l in owed.splitlines() if l], [render_roadmap.OWNER_COLLAPSED_LINE])

    def test_a1_a_board_that_fits_still_lists_its_owner_asks(self) -> None:
        text = render_roadmap.render(self.board(row("OWNER-1", "parked", self.STAMP, kind="hub-tooling", origin="owner")))
        self.assertIn("- OWNER-1 (P3, hub-tooling): parked 3 d", text)
        self.assertNotIn(render_roadmap.OWNER_COLLAPSED_LINE, text)

    def test_a1_distinguish_queued_product_kind_owner_asks_are_product_rows(self) -> None:
        # The DISTINGUISH variant: queued owner asks of kind product were product rows before this card too, so they are listed.
        asks = [row("OP-%03d" % i, "queued", self.STAMP, origin="owner") for i in range(400)]
        cards = table_cards(render_roadmap.render(self.a1_board(*asks)))
        self.assertEqual(sum(1 for c in cards if c.startswith("OP-")), 400)

    # A2 (introduced, major): a lone surrogate anywhere in the queue is bad input (exit 2), never an encode crash.
    def a2_owner(self, **extra) -> dict:
        state = extra.pop("state", "queued")   # row() takes state positionally
        return row("OWNER-1", state, self.STAMP, origin="owner", **extra)

    def test_a2_a_lone_surrogate_in_state_or_order_chain_is_bad_input(self) -> None:
        cases = {
            "state": self.a2_owner(state="\ud800"),
            "order_chain.chain": self.a2_owner(order_chain={"chain": "\ud800", "index": 1}),
            "order_chain.predecessor": self.a2_owner(order_chain={"chain": "c", "index": 1, "predecessor": "\ud800"}),
        }
        for name, r in cases.items():
            with self.subTest(field=name):
                self.assertEqual(self.load_code(self.board(r))[0], 2)
                self.assertEqual(self.check_code(self.board(r)), 2)

    def test_a2_a_lone_surrogate_anywhere_in_the_queue_is_bad_input(self) -> None:
        def nest(data: dict) -> dict:
            return data
        cases = []
        d = self.board(self.a2_owner(extra_field="\udfff")); cases.append(("row value", d))
        d = self.board(self.a2_owner(**{"\ud800key": "v"})); cases.append(("row key", d))
        d = self.board(self.a2_owner(tags=["ok", {"deep": ["\ud801"]}])); cases.append(("nested list", d))
        d = self.board(self.a2_owner()); d["comment"] = "\udc00"; cases.append(("top-level value", d))
        d = self.board(self.a2_owner()); d["\udc00"] = 1; cases.append(("top-level key", d))
        d = self.board(self.a2_owner()); d["scope_notes"] = [{"title": "t", "text": "x", "by": "\ud800"}]; cases.append(("scope note", d))
        d = self.board(self.a2_owner(notes="\ud800")); cases.append(("notes", d))
        for name, data in cases:
            with self.subTest(where=name):
                code, err = self.load_code(nest(data))
                self.assertEqual(code, 2, err)
                self.assertIn("UTF-8", err)
                self.assertEqual(self.check_code(data), 2)

    def test_a2_valid_unicode_text_still_loads(self) -> None:
        self.assertEqual(self.load_code(self.board(self.a2_owner(notes="café \U0001f600", state="queued")))[0], 0)

    # A3 (introduced, major): no whitespace inside or around a stamp except the one T-or-space date/time separator.
    BAD_STAMPS = ("2026-10-20T00:00:00\rZ", "2026-10-20T00:00:00\tZ", "2026-10-20T00:00:00\vZ", "2026-10-20T00:00:00\fZ",
                  "2026-10-20T00:00:00 Z", "2026-10-20T00:00:00 ", "2026-10-20T00:00:00\r", "2026-10-20T00:00\tZ",
                  "2026-10-20T00:00:00\r+01:00", "2026-10-20T00:00:00.5 Z", "2026-10-20  00:00:00Z", "2026-10-20 00:00:00 Z")

    def test_a3_whitespace_in_every_stamp_field_is_bad_input(self) -> None:
        for stamp in self.BAD_STAMPS:
            for field in ("generated_utc", "source_ledger_stamp", "pinned_utc", "merged_utc"):
                with self.subTest(field=field, stamp=stamp):
                    if field == "generated_utc":
                        data = self.board(row("Q-1", "queued", self.STAMP), generated=stamp.replace("2026-10-20", "2026-10-23"))
                    elif field == "source_ledger_stamp":
                        data = self.board(row("Q-1", "merged", stamp))
                    else:
                        data = self.board(row("Q-1", "merged", self.STAMP, **{field: stamp}))
                    code, err = self.load_code(data)
                    self.assertEqual(code, 2, err)
                    self.assertIn(field, err)

    def test_a3_the_adversary_generated_utc_never_renders_a_carriage_return(self) -> None:
        for g in ("2026-10-23T00:00:00\rZ", "2026-10-23T00:00:00 "):
            with self.subTest(generated=g):
                self.assertEqual(self.load_code(self.board(generated=g))[0], 2)

    def test_a3_the_stamp_problem_names_whitespace(self) -> None:
        for stamp in self.BAD_STAMPS:
            with self.subTest(stamp=stamp):
                self.assertTrue(render_roadmap._stamp_problem(stamp))

    def test_a3_the_one_separator_and_plain_stamps_still_load(self) -> None:
        for stamp in ("2026-10-20 00:00:00Z", "2026-10-20 00:00:00", "2026-10-20T00:00Z", "2026-10-20T00:00:00.5+05:30", "2026-10-20"):
            with self.subTest(stamp=stamp):
                self.assertEqual(self.load_code(self.board(row("Q-1", "merged", stamp, pinned_utc=stamp)))[0], 0)

    def test_a3_instant_reads_the_stamp_as_written(self) -> None:
        self.assertIsNone(render_roadmap._STAMP_RX.fullmatch("2026-10-20T00:00:00 Z"))
        self.assertEqual(render_roadmap._instant("2026-10-20 00:00:00+01:00"), render_roadmap._instant("2026-10-19T23:00:00Z"))


class R3Pass7Tests(R3Tests):
    """r3 pass 7 (review-adversary-OWNER-ASK-PARK-GUARD-1-r3-9b0c0786) A1-A3, each on its input."""

    # A1: in the collapsed state a closed owner ask (parked or merged, any kind) must not reach the drain counts either.
    def p7_board(self, *extra: dict) -> dict:
        data = self.a1_board(*extra)
        data["scope_notes"] = [{"title": "S", "text": "x" * (self.boundary_note_length() + 1)}]
        return data

    def p7_owners(self, **extra) -> list:
        return [row("OWNER-%d-%03d" % (day, i), extra.get("state", "parked"), "2026-10-%02dT00:00:00Z" % day, origin="owner",
                    **{k: v for k, v in extra.items() if k != "state"}) for day in range(17, 24) for i in range(100)]

    def test_p7_a1_closed_product_owner_asks_add_no_count_digit_when_collapsed(self) -> None:
        base = self.size(self.p7_board())
        self.assertLessEqual(base, ROADMAP_BUDGET_BYTES)
        for name, extra in {"parked product": {}, "parked playback resolved": {"kind": "playback", "resolution": "done"},
                            "merged product": {"state": "merged", "merged_utc": "2026-10-22T00:00:00Z"}}.items():
            with self.subTest(variant=name):
                self.assertLessEqual(self.size(self.p7_board(*self.p7_owners(**extra))), base)

    # A2: a line break or pipe in a table cell would forge a card row; such text is bad input (exit 2).
    FORGE = "C\n| 3 | FORGED | product | queued | 2026-10-20 | - |\n"

    def p7_owner(self, **extra) -> dict:
        state = extra.pop("state", "queued")
        return row("OWNER-1", state, self.STAMP, kind="hub-tooling", origin="owner", **extra)

    def test_p7_a2_a_multiline_or_piped_table_cell_is_bad_input(self) -> None:
        for payload in (self.FORGE, "a|b", "a\rb", "a\tb", "a b", "a\x1bb", "x" * 121):
            cases = {"state": self.p7_owner(state=payload),
                     "order_chain.chain": self.p7_owner(order_chain={"chain": payload, "index": 1}),
                     "order_chain.predecessor": self.p7_owner(order_chain={"chain": "c", "index": 1, "predecessor": payload})}
            for field, r in cases.items():
                with self.subTest(field=field, payload=payload[:12]):
                    code, err = self.load_code(self.board(r))
                    self.assertEqual(code, 2, err)
                    self.assertIn(field.split(".")[0], err)

    def test_p7_a2_the_forged_row_never_renders(self) -> None:
        data = self.board(self.p7_owner(order_chain={"chain": self.FORGE, "index": 1}))
        self.assertEqual(self.check_code(data), 2)

    def test_p7_a2_a_non_string_state_is_bad_input(self) -> None:
        for state in ({"|": 1}, ["queued"], 3):
            with self.subTest(state=state):
                self.assertEqual(self.load_code(self.board(self.p7_owner(state=state)))[0], 2)

    def test_p7_a2_sibling_a_multiline_scope_note_is_bad_input(self) -> None:
        for key in ("title", "text"):
            with self.subTest(key=key):
                data = self.board(self.p7_owner())
                note = {"title": "t", "text": "x"}
                note[key] = "a\n| 3 | FORGED | product | queued | 2026-10-20 | - |"
                data["scope_notes"] = [note]
                self.assertEqual(self.load_code(data)[0], 2)

    def test_p7_a2_live_shaped_cells_still_load(self) -> None:
        r = self.p7_owner(state="pr-open", order_chain={"chain": "ORDER-20261016T035244Z", "index": 2, "predecessor": "CPU-17V9-FPS-GAP-AB-1"})
        self.assertEqual(self.load_code(self.board(r))[0], 0)

    # A3: rows whose sort keys tie (duplicate ids, equal instants) render the same whatever their input order.
    def test_p7_a3_input_row_order_does_not_change_the_output(self) -> None:
        for state in ("queued", "parked"):
            with self.subTest(state=state):
                a = row("SAME", state, "2026-10-20T00:00:00Z", order_chain={"chain": "A", "index": 1}, notes="a")
                b = row("SAME", state, "2026-10-20T01:00:00+01:00", order_chain={"chain": "B", "index": 1}, notes="b")
                self.assertEqual(render_roadmap.render(self.board(a, b)), render_roadmap.render(self.board(b, a)))
        a = row("SAME", "parked", "2026-10-20T00:00:00Z", origin="owner", notes="a")
        b = row("SAME", "parked", "2026-10-20T01:00:00+01:00", origin="owner", notes="b")
        self.assertEqual(render_roadmap.render(self.board(a, b)), render_roadmap.render(self.board(b, a)))


class R4DepthTests(unittest.TestCase):
    """OWNER-ASK-PARK-GUARD-1 r4: r3 OPEN A1 (review-adversary-OWNER-ASK-PARK-GUARD-1-r3-6739295e). A row with an extra field nested
    thousands of arrays deep passed _load and then raised RecursionError in json.dumps (_canonical and its siblings). Past
    MAX_NESTING levels the queue is bad input: exit 2 with a typed message, never a traceback."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def deep_text(self, depth: int, **row_extra: str) -> str:
        """The adversary's exact input shape, written as text (json.dumps itself cannot serialise it)."""
        extra = "".join(',"%s":%s' % (k, v) for k, v in row_extra.items())
        return ('{"schema":"mlv-app/card-queue/v1","generated_utc":"2026-10-23","rows":[{"card":"X","kind":"product","priority":1,'
                '"state":"queued","source_ledger_stamp":"2026-10-20"%s,"extra":%s0%s}]}' % (extra, "[" * depth, "]" * depth))

    def write_text(self, text: str) -> Path:
        p = Path(self.tmp.name) / "q.json"
        p.write_text(text, encoding="utf-8")
        return p

    def load_code(self, text: str) -> tuple[int, str]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                render_roadmap._load(self.write_text(text))
                return 0, err.getvalue()
            except SystemExit as exc:
                return exc.code, err.getvalue()

    def main_code(self, text: str, *flags: str) -> tuple[int, str]:
        """main on the text: a status (never an exception, RecursionError included)."""
        p = self.write_text(text)
        out = Path(self.tmp.name) / "roadmap.md"
        out.write_bytes(b"stale\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            try:
                return render_roadmap.main(["--card-queue", str(p), "--out", str(out), *flags]), err.getvalue()
            except SystemExit as exc:
                return exc.code, err.getvalue()

    def test_a1_a_deeply_nested_extra_field_is_bad_input_not_a_recursion_error(self) -> None:
        for depth in (render_roadmap.MAX_NESTING + 5, 3000, 5000):
            with self.subTest(depth=depth):
                code, err = self.load_code(self.deep_text(depth))
                self.assertEqual(code, 2, err)
                self.assertIn("nests", err)

    def test_a1_every_entry_point_returns_a_status_on_the_adversary_input(self) -> None:
        for state in ("queued", "parked"):
            text = self.deep_text(5000).replace('"state":"queued"', '"state":"%s"' % state)
            for flags in (("--check",), (), ("--drain-line",)):
                with self.subTest(state=state, flags=flags):
                    code, err = self.main_code(text, *flags)
                    self.assertEqual(code, 2, err)

    def test_a1_the_owner_origin_variant_is_bad_input_too(self) -> None:
        code, err = self.main_code(self.deep_text(5000, origin='"owner"'), "--check")
        self.assertEqual(code, 2, err)

    def test_a1_json_that_python_itself_cannot_parse_that_deep_is_bad_input(self) -> None:
        for depth in (20000, 100000):
            with self.subTest(depth=depth):
                code, err = self.load_code(self.deep_text(depth))
                self.assertEqual(code, 2, err)

    def test_a1_a_deep_chain_of_objects_is_bounded_too(self) -> None:
        deep_obj = '{"k":' * 5000 + "0" + "}" * 5000
        text = self.deep_text(1).replace('"extra":[0]', '"extra":' + deep_obj)
        code, err = self.load_code(text)
        self.assertEqual(code, 2, err)
        self.assertIn("nests", err)

    def test_a1_the_bound_is_exact(self) -> None:
        base = 3   # the queue object, its rows list and the row object take three levels before the extra field
        at_bound = self.deep_text(render_roadmap.MAX_NESTING - base)
        self.assertEqual(self.load_code(at_bound)[0], 0)
        code, err = self.main_code(at_bound, "--check")
        self.assertEqual(code, 1, err)   # the stale output differs: a comparison status, not an error
        self.assertEqual(self.load_code(self.deep_text(render_roadmap.MAX_NESTING - base + 1))[0], 2)

    def test_a1_live_shaped_rows_are_nowhere_near_the_bound(self) -> None:
        self.assertEqual(self.load_code(json.dumps(load_fixture()))[0], 0)
        self.assertLessEqual(render_roadmap.MAX_NESTING, 100)   # a bound that would not stop the 3,000-deep input is no bound


# (r5, review-pr835-opus-r4 blocker + hardening) master's renderer, embedded as it stood on fork/master before this card. The budget
# window tests measure "fits at base" against THIS copy, never against the renderer under test: a change to the shared header or to
# any other shared text moves the new renderer and this copy apart, which turns them red. Stamps in these boards are plain Z-stamps,
# where master's string sort and the new instant sort agree.
MASTER_HEADER = """\
# MLV-App product roadmap

Rendered by `tools/coordination/render_roadmap.py` from the board's `card-queue.json`
(schema `mlv-app/card-queue/v1`), the live dispatch surface. It replaces the earlier
mirror of the frozen dual-lane `queue.json`, whose 16 rows had mostly landed. The
card-queue file lives under `.claude-state/hub-autopilot/`, which is gitignored and absent
from every hosted checkout, so this file is the only place a reviewer without local state
can see the open product backlog. Where the two disagree, `card-queue.json` wins:
re-render, do not edit by hand. The hub checks freshness with `--check`.

Only rows with `kind` of `product` or `playback` are shown. Hub-tooling, factory,
hygiene and ci-flake cards are tooling churn and are not counted here. Rows the board has
not classified are not counted either, even when the card name says PLAYBACK.
"""


def master_render(data: dict) -> str:
    """fork/master's render_roadmap.render, verbatim apart from inlined helpers (_day, _landed_stamp, _stats, _note, _drain_line)."""
    import re as _re
    from datetime import datetime as _dt, timedelta as _td
    kinds = ("product", "playback")
    closed = ("merged", "parked")
    declared = _re.compile(r"declared@(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)")

    def day(stamp: str) -> date:
        return _dt.strptime(stamp[:10], "%Y-%m-%d").date()

    def landed_stamp(r: dict) -> str:
        if r.get("merged_utc"):
            return r["merged_utc"]
        stamps = [r["source_ledger_stamp"]]
        if r.get("pinned_utc"):
            stamps.append(r["pinned_utc"])
        stamps += declared.findall(r.get("evidence") or "")
        return max(stamps)

    def sort_key(r: dict) -> tuple:
        return (r["priority"], r["source_ledger_stamp"], r["card"])

    def chain(r: dict) -> str:
        oc = r.get("order_chain")
        if not oc:
            return "-"
        pred = oc.get("predecessor")
        return "%s #%s %s" % (oc["chain"], oc["index"], "after " + pred if pred else "first")

    def note(r: dict) -> str:
        text = " ".join((r.get("notes") or "").split())
        if not text:
            return "(no note)"
        return text if len(text) <= 140 else text[:137] + "..."

    def drain_line(open_n: int, landed_n: int, queued_n: int) -> str:
        if open_n == 0:
            return "product is drained (0 open)"
        if queued_n >= landed_n:
            return "does not drain (queued >= landed)"
        net = landed_n - queued_n
        return "at the 7-day mean, product drains in %d days" % (-(-open_n * 7 // net))

    rows = [r for r in data["rows"] if r["kind"] in kinds]
    open_rows = sorted((r for r in rows if r["state"] not in closed), key=sort_key)
    parked = sorted((r for r in rows if r["state"] == "parked"), key=sort_key)
    last = day(data["generated_utc"])
    days = [last - _td(days=6 - i) for i in range(7)]
    landed = {d: 0 for d in days}
    queued = {d: 0 for d in days}
    for r in rows:
        d = day(r["source_ledger_stamp"])
        if d in queued:
            queued[d] += 1
        if r["state"] == "merged":
            d = day(landed_stamp(r))
            if d in landed:
                landed[d] += 1
    landed_total, queued_total = sum(landed.values()), sum(queued.values())
    scope = []
    for n in data.get("scope_notes") or []:
        scope += ["**%s.** %s" % (n["title"], n["text"]), ""]
    out = [MASTER_HEADER, "generated_from: %s" % data["generated_utc"], "", "## Open product and playback cards", "", *scope]
    out.append("| P | card | kind | state | queued since | order_chain |")
    out.append("|---|---|---|---|---|---|")
    for r in open_rows:
        out.append("| %s | %s | %s | %s | %s | %s |" % (
            r["priority"], r["card"], r["kind"], r["state"], r["source_ledger_stamp"][:10], chain(r)))
    if not open_rows:
        out.append("| - | (none open) | - | - | - | - |")
    out += ["", "## Drain", "", "product+playback open: %d" % len(open_rows),
            "", "Window: the %d UTC days ending %s (the last day is partial)." % (7, last.isoformat()),
            "Landed = merged rows by `merged_utc` when the row has one, else by the newest stamp "
            "(approximate: a lower bound on the merge time, not a measurement); "
            "queued = rows of any state by `source_ledger_stamp`.", "",
            "| day (UTC) | landed | queued |", "|---|---|---|"]
    for d in days:
        out.append("| %s | %d | %d |" % (d.isoformat(), landed[d], queued[d]))
    out.append("| total | %d | %d |" % (landed_total, queued_total))
    out += ["", drain_line(len(open_rows), landed_total, queued_total), "", "## Parked with reason", "", *scope]
    for r in parked:
        out.append("- %s (P%s): %s" % (r["card"], r["priority"], note(r)))
    if not parked:
        out.append("- (none parked)")
    return "\n".join(out) + "\n"


class R5BaseWindowTests(unittest.TestCase):
    """OWNER-ASK-PARK-GUARD-1 r5 (review-pr835-opus-r4 blocker): the last tier claims master's output, so a product-only board that fit the
    20000-byte budget on master must fit here and, in the window where every owner-aware tier is over, be master's file byte for byte."""

    STAMP = "2026-10-20T00:00:00Z"

    def board(self, note_len: int, *extra: dict) -> dict:
        rows = [row("PRODUCT-%03d" % i, "queued", self.STAMP) for i in range(300)]
        return {"schema": "mlv-app/card-queue/v1", "generated_utc": "2026-10-23T00:00:00Z", "rows": rows + list(extra),
                "scope_notes": [{"title": "S", "text": "x" * note_len}]}

    def sweep(self) -> list[int]:
        """Note lengths whose board fits at master and is over budget as the new renderer's collapsed tier (header and owner section)."""
        found = []
        for n in range(300, 900):
            data = self.board(n)
            if (render_roadmap._budget_size(master_render(data)) <= ROADMAP_BUDGET_BYTES
                    and render_roadmap._budget_size(render_roadmap._render(data, 0, collapsed=True)) > ROADMAP_BUDGET_BYTES):
                found.append(n)
        return found

    def test_the_base_renderer_copy_matches_master_on_a_plain_board(self) -> None:
        # the embedded copy is the oracle: pin it to the renderer on a board where the owner features add nothing
        data = load_fixture()
        data["rows"] = [r for r in data["rows"] if r.get("origin") != "owner" and r["kind"] in ("product", "playback")]
        self.assertEqual(render_roadmap._render(data, 0, collapsed=True, bare=True), master_render(data))

    def test_a_board_that_fit_at_master_still_fits_and_is_master_s_file(self) -> None:
        window = self.sweep()
        self.assertTrue(window, "the window master fit and the owner-aware tiers did not must exist for this test to mean anything")
        for n in window:
            data = self.board(n)
            with self.subTest(note_len=n):
                text = render_roadmap.render(data)
                self.assertLessEqual(render_roadmap._budget_size(text), ROADMAP_BUDGET_BYTES)
                self.assertEqual(text, master_render(data))

    def test_every_board_that_fit_at_master_fits_here(self) -> None:
        for n in range(300, 900):
            data = self.board(n)
            if render_roadmap._budget_size(master_render(data)) <= ROADMAP_BUDGET_BYTES:
                with self.subTest(note_len=n):
                    self.assertLessEqual(render_roadmap._budget_size(render_roadmap.render(data)), ROADMAP_BUDGET_BYTES)

    def owner_variants(self) -> dict[str, tuple]:
        """Owner rows of a tooling kind (master never counted them): none, parked (owed), resolved, queued."""
        stamp = self.STAMP
        return {
            "none": (),
            "parked": (row("OWNER-P", "parked", stamp, kind="hub-tooling", origin="owner", notes="waiting"),),
            "resolved": (row("OWNER-R", "parked", stamp, kind="hub-tooling", origin="owner", notes="done", resolution="done"),),
            "queued": (row("OWNER-Q", "queued", stamp, kind="hub-tooling", origin="owner"),),
        }

    def test_r6_the_b596_board_is_master_s_file_with_every_owner_row_shape(self) -> None:
        # review-pr835-opus-r4 / review-pr835-sol-r4 blocker repro (sol INV-11): 300 queued product rows and one 596-char scope note
        # fit at master (19999 CRLF bytes) and rendered 20089 at 08a1edd4, the bare tier having carried the longer owner-aware header.
        base = self.board(596)
        self.assertEqual(render_roadmap._budget_size(master_render(base)), 19999)
        for name, extra in self.owner_variants().items():
            data = self.board(596, *extra)
            with self.subTest(owner_rows=name):
                text = render_roadmap.render(data)
                self.assertLessEqual(render_roadmap._budget_size(text), ROADMAP_BUDGET_BYTES)
                self.assertEqual(text, master_render(base))

    def a1_board(self, closed_owner_kind: str = "product", **closed_extra) -> dict:
        """review-adversary-OWNER-ASK-PARK-GUARD-1-r7-4757a720 A1: 300 product rows (one pr-open) and four merged product rows, two of them
        owner asks. Master has no origin concept and counts all four (drain 525 days); the collapsed tiers dropped the two closed owner asks
        (drain 1050 days), so the bare fallback was 20001 CRLF bytes against master's 20000."""
        stamp = "2026-09-01T00:00:00Z"
        rows = [row("PRODUCT-%03d" % i, "pr-open" if i == 299 else "queued", stamp) for i in range(300)]
        for i in range(4):
            extra = dict(closed_extra) if i >= 2 else {}
            if i >= 2:
                extra["origin"] = "owner"
            rows.append(row("MERGED-%d" % i, "merged", stamp, kind="product" if i < 2 else closed_owner_kind, merged_utc="2026-10-22T00:00:00Z", **extra))
        return {"schema": "mlv-app/card-queue/v1", "generated_utc": "2026-10-23T00:00:00Z", "rows": rows,
                "scope_notes": [{"title": "S", "text": "x" * 592}]}

    def test_r7_a1_the_bare_tier_counts_a_closed_product_owner_ask_as_master_does(self) -> None:
        data = self.a1_board()
        self.assertEqual(render_roadmap._budget_size(master_render(data)), ROADMAP_BUDGET_BYTES)
        text = render_roadmap.render(data)
        self.assertLessEqual(render_roadmap._budget_size(text), ROADMAP_BUDGET_BYTES)
        self.assertEqual(text, master_render(data))

    def test_r7_a1_any_base_fitting_board_with_product_owner_asks_stays_within_budget(self) -> None:
        # sibling shapes (the finding's siblings): product-kind owner asks that are queued, parked (owed), parked with a resolution or merged
        # are plain product rows to master, so a board that fit at master must fit here whatever the owner-aware tiers do with them
        stamp = self.STAMP
        shapes = {
            "queued": row("OWN-Q", "queued", stamp, origin="owner"),
            "owed": row("OWN-P", "parked", stamp, origin="owner", notes="waiting"),
            "resolved": row("OWN-R", "parked", stamp, origin="owner", notes="done", resolution="done"),
            "merged": row("OWN-M", "merged", stamp, origin="owner", merged_utc="2026-10-22T00:00:00Z"),
        }
        for n in range(300, 900, 5):
            for name, extra in shapes.items():
                data = self.board(n, extra)
                if render_roadmap._budget_size(master_render(data)) <= ROADMAP_BUDGET_BYTES:
                    with self.subTest(note_len=n, owner_ask=name):
                        self.assertLessEqual(render_roadmap._budget_size(render_roadmap.render(data)), ROADMAP_BUDGET_BYTES)

    def test_r6_any_base_fitting_board_plus_owner_rows_stays_within_budget(self) -> None:
        for n in range(300, 900, 7):
            if render_roadmap._budget_size(master_render(self.board(n))) > ROADMAP_BUDGET_BYTES:
                continue
            for name, extra in self.owner_variants().items():
                with self.subTest(note_len=n, owner_rows=name):
                    self.assertLessEqual(render_roadmap._budget_size(render_roadmap.render(self.board(n, *extra))), ROADMAP_BUDGET_BYTES)

    def test_the_header_text_is_master_s_in_the_bare_tier_only(self) -> None:
        data = self.board(100)
        self.assertTrue(render_roadmap._render(data, 0, collapsed=True, bare=True).startswith(MASTER_HEADER))
        self.assertFalse(render_roadmap._render(data, 25).startswith(MASTER_HEADER))   # the owner-aware header says owner asks are shown


class R4BudgetTests(unittest.TestCase):
    """OWNER-ASK-PARK-GUARD-1 r4 pass 1 A1 (review-adversary-OWNER-ASK-PARK-GUARD-1-r4-7304a0c2). test_roadmap_queue_parity.py holds
    docs/roadmap.md to 20000 bytes by st_size, so a Windows checkout (core.autocrlf=true) counts every line break as two bytes. The
    render budget is measured on that larger CRLF form, so the file is within budget in either checkout format."""

    def crlf_size(self, text: str) -> int:
        return len(text.replace("\n", "\r\n").encode("utf-8"))

    def adversary_board(self) -> dict:
        rows = [row("OWNER-%03d" % i, "parked", "2026-10-20T00:00:00Z", kind="hub-tooling", origin="owner", notes="n" * 100)
                for i in range(112)]
        return {"schema": "mlv-app/card-queue/v1", "generated_utc": "2026-10-23T00:00:00Z", "rows": rows,
                "scope_notes": [{"title": "S", "text": "x" * 8000}]}

    def test_a1_the_adversary_board_fits_the_budget_in_a_crlf_checkout(self) -> None:
        text = render_roadmap.render(self.adversary_board())
        self.assertLessEqual(self.crlf_size(text), ROADMAP_BUDGET_BYTES)
        self.assertLessEqual(render_roadmap._budget_size(text), ROADMAP_BUDGET_BYTES)

    # pass 2 A1 (review-adversary-OWNER-ASK-PARK-GUARD-1-r4-9e9184f9): the owner section's heading and placeholder cost bytes the base never
    # spent, so a product-only board that fit at base (19999 CRLF bytes) rendered at 20143. The last tier drops that section.
    def window_board(self, note_len: int, *extra: dict) -> dict:
        rows = [row("PRODUCT-%03d" % i, "queued", "2026-10-20T00:00:00Z") for i in range(300)]
        return {"schema": "mlv-app/card-queue/v1", "generated_utc": "2026-10-23T00:00:00Z", "rows": rows + list(extra),
                "scope_notes": [{"title": "S", "text": "x" * note_len}]}

    def window_lengths(self) -> list[int]:
        """Note lengths whose board fits the budget with no owner section but not with one: the window the base fit and head did not."""
        found = []
        for n in range(300, 900):
            data = self.window_board(n)
            # r5: "fits at base" is measured on the embedded copy of master's renderer, not on the renderer under test
            if (render_roadmap._budget_size(render_roadmap._render(data, 0, collapsed=True)) > ROADMAP_BUDGET_BYTES
                    and render_roadmap._budget_size(master_render(data)) <= ROADMAP_BUDGET_BYTES):
                found.append(n)
        return found

    def test_a1_p2_a_board_that_fit_without_the_owner_section_still_fits(self) -> None:
        window = self.window_lengths()
        self.assertTrue(window, "the window the base fit and head did not must exist for this test to mean anything")
        owner = row("OWNER-1", "parked", "2026-10-20T00:00:00Z", kind="hub-tooling", origin="owner", notes="waiting")
        for n in (window[0], window[len(window) // 2], window[-1]):
            for extra in ((), (owner,)):
                with self.subTest(note_len=n, owners=len(extra)):
                    text = render_roadmap.render(self.window_board(n, *extra))
                    self.assertLessEqual(render_roadmap._budget_size(text), ROADMAP_BUDGET_BYTES)
                    self.assertEqual(sum(1 for c in table_cards(text) if c.startswith("PRODUCT-")), 300)   # product rows never drop
                    if not extra:   # with an owner ask the one collapsed line can fit where the empty placeholder did not: then it stays
                        self.assertNotIn("Parked owner asks", text)

    def test_a1_p2_the_owner_section_is_back_whenever_it_fits(self) -> None:
        text = render_roadmap.render(self.window_board(100))
        self.assertIn("## Parked owner asks", text)
        self.assertIn("(no owner ask is parked)", text)

    def test_a3_check_on_an_output_that_is_not_utf8_is_a_comparison_status(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        q = Path(tmp.name) / "q.json"
        q.write_text(json.dumps({"schema": "mlv-app/card-queue/v1", "generated_utc": "2026-10-23T00:00:00Z", "rows": []}), encoding="utf-8")
        out = Path(tmp.name) / "roadmap.md"
        out.write_bytes(b"\xff")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(render_roadmap.main(["--card-queue", str(q), "--out", str(out), "--check"]), 1)

    def test_a1_the_budget_measure_is_the_crlf_size(self) -> None:
        for text in ("a\nb\n", "no break", "ae\xe9\n\n"):
            with self.subTest(text=text):
                self.assertEqual(render_roadmap._budget_size(text), self.crlf_size(text))



if __name__ == "__main__":
    unittest.main()
