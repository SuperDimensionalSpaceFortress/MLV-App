"""FACTORY-ESCAPE-REGRESSION-DISCIPLINE-1: the defect-dispositions block is EVIDENCE-ONLY.

Every input is synthetic under a temp dir; nothing reads the real fleet-runs or .claude-state. The contract
these rows pin: render shows each demonstrated defect's regression evidence and never invents a value (a
missing field reads MISSING); splice is idempotent and leaves bytes outside the markers alone; check prints
one line and ALWAYS exits 0; escapes counts merged defects, reverts, non-flake merge_group failures and
repeat failures inside a window and also always exits 0.
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOL = os.path.join(ROOT, "tools", "coordination", "defect_ledger.py")
_spec = importlib.util.spec_from_file_location("defect_ledger", TOOL)
dl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dl)

PR = 700
HEAD = "a" * 40
OLD = "b" * 40
NEW = "c" * 40
NOW = "2026-10-24T20:00:00Z"


def rec(**kw):
    """A complete `fixed` record; override or delete (value None) fields per test."""
    base = {
        "schema": "defect-ledger.v1", "pr": PR, "head_sha": HEAD, "recorded_utc": "2026-10-24T12:00:00Z",
        "defect_id": "D1", "source": "key opus run review-pr700-opus-r1-20261024T0300Z",
        "claim": "splice drops text after a second marker",
        "regression_test": "tests/coordination/test_x.py::test_splice_keeps_tail",
        "old_red_sha": OLD, "new_green_sha": NEW,
        "combination_tests": ["tests/coordination/test_x.py::test_splice_two_markers_plus_crlf"],
        "disposition": "fixed",
    }
    base.update(kw)
    return {k: v for k, v in base.items() if v is not None}


def rows_of(block):
    return [ln for ln in block.splitlines() if ln.startswith("| ") and not ln.startswith("| Defect")]


def body_with(records, intro=b"intro\n"):
    return dl.splice(intro, dl.render(PR, HEAD, records).encode("utf-8"))


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.records = [
            rec(),
            rec(defect_id="D2", disposition="distinguished: the old path never reached this branch",
                regression_test="n/a: distinguished", old_red_sha="n/a", new_green_sha="n/a",
                combination_tests="none", combination_reason="no code change"),
            rec(defect_id="D3", disposition="deferred: PLAYBACK-EXAMPLE-1", regression_test="n/a: deferred",
                old_red_sha="n/a", new_green_sha="n/a", combination_tests=[], combination_reason="owned by the named card"),
            rec(defect_id="D4", regression_test=None, old_red_sha=None, combination_tests=None, claim=None),
        ]
        self.block = dl.render(PR, HEAD, self.records)

    def _row(self, needle):
        hits = [r for r in rows_of(self.block) if needle in r]
        self.assertEqual(len(hits), 1, (needle, self.block))
        return hits[0]

    def test_block_is_framed_and_titled(self):
        self.assertTrue(self.block.startswith(dl.BEGIN + "\n## Defect dispositions"))
        self.assertTrue(self.block.endswith(dl.END))
        self.assertIn(HEAD, self.block)

    def test_fixed_row_shows_every_field_and_sha8(self):
        row = self._row("| D1 |")
        self.assertIn("review-pr700-opus-r1-20261024T0300Z", row)
        self.assertIn("splice drops text after a second marker", row)
        self.assertIn("tests/coordination/test_x.py::test_splice_keeps_tail", row)
        self.assertIn("old: RED on %s / new: GREEN on %s" % (OLD[:8], NEW[:8]), row)
        self.assertIn("test_splice_two_markers_plus_crlf", row)
        self.assertTrue(row.rstrip().endswith("| fixed |"), row)
        self.assertNotIn(OLD, row)  # the evidence column is sha8, never the full hash

    def test_distinguished_and_deferred_rows_carry_their_reason_or_card(self):
        self.assertTrue(self._row("| D2 |").rstrip().endswith(
            "| distinguished: the old path never reached this branch |"))
        self.assertIn("none (reason: no code change)", self._row("| D2 |"))
        self.assertTrue(self._row("| D3 |").rstrip().endswith("| deferred: PLAYBACK-EXAMPLE-1 |"))
        self.assertIn("none (reason: owned by the named card)", self._row("| D3 |"))

    def test_missing_fields_render_MISSING_and_are_never_invented(self):
        row = self._row("| D4 |")
        self.assertEqual(row.count("MISSING"), 4, row)  # claim, regression test, old sha, combination tests
        self.assertIn("old: MISSING / new: GREEN on %s" % NEW[:8], row)
        bare = dl.render(PR, HEAD, [{"defect_id": "D9"}])
        row = [r for r in rows_of(bare) if "| D9 |" in r][0]
        self.assertGreaterEqual(row.count("MISSING"), 6, row)
        self.assertNotIn("fixed", row)

    def test_sha_in_evidence_column_is_shown_as_given_up_to_eight(self):
        short = dl.render(PR, HEAD, [rec(old_red_sha="abc1234", new_green_sha="def5678")])
        self.assertIn("old: RED on abc1234 / new: GREEN on def5678", short)
        full = dl.render(PR, HEAD, [rec()])
        hexes = set(re.findall(r"\b[0-9a-f]{8,}\b", full))
        self.assertEqual(hexes, {OLD[:8], NEW[:8], HEAD}, hexes)

    def test_pipe_and_newline_in_a_value_cannot_break_the_row(self):
        row = rows_of(dl.render(PR, HEAD, [rec(claim="a | b\nc")]))[0]
        self.assertEqual(len(dl.split_row(row)), 7, row)

    def test_malformed_records_render_unreadable_rows_not_crash(self):
        block = dl.render(PR, HEAD, ["not a dict", rec(claim=["list", "not", "str"]), rec()])
        rows = rows_of(block)
        self.assertEqual(len(rows), 3)
        self.assertEqual([r.count("unreadable") for r in rows[:2]], [7, 7])
        self.assertEqual(rows[2].count("unreadable"), 0)

    def test_empty_list_renders_an_empty_table(self):
        block = dl.render(PR, HEAD, [])
        self.assertEqual(rows_of(block), [])
        self.assertIn("| Defect |", block)

    def test_short_head_is_rejected(self):
        with self.assertRaises(ValueError):
            dl.render(PR, "abc123", [])

    def test_render_is_deterministic(self):
        self.assertEqual(self.block, dl.render(PR, HEAD, self.records))


class SpliceTests(unittest.TestCase):
    def setUp(self):
        self.block = dl.render(PR, HEAD, [rec()]).encode("utf-8")

    def test_append_when_absent(self):
        out = dl.splice(b"# Title\n\nbody text\n", self.block)
        self.assertTrue(out.startswith(b"# Title\n\nbody text\n"))
        self.assertEqual(out.count(dl.BEGIN.encode()), 1)
        self.assertTrue(out.endswith(dl.END.encode() + b"\n"))

    def test_append_to_empty_body(self):
        self.assertTrue(dl.splice(b"", self.block).startswith(dl.BEGIN.encode()))

    def test_replace_when_present_keeps_outside_bytes(self):
        before, after = b"head \xc3\xa9 text\r\n\r\n", b"\r\n\r\ntail  \t\r\nno newline"
        body = before + dl.BEGIN.encode() + b"\nOLD\n" + dl.END.encode() + after
        out = dl.splice(body, self.block)
        self.assertTrue(out.startswith(before))
        self.assertTrue(out.endswith(after))
        self.assertNotIn(b"OLD", out)
        self.assertEqual(out.count(dl.BEGIN.encode()), 1)

    def test_idempotent(self):
        once = dl.splice(b"intro\n", self.block)
        self.assertEqual(dl.splice(once, self.block), once)
        crlf = dl.splice(b"intro\r\n", self.block)
        self.assertEqual(dl.splice(crlf, self.block), crlf)
        self.assertNotIn(b"\r\r", crlf)
        self.assertEqual(crlf.count(b"\n"), crlf.count(b"\r\n"))

    def test_second_render_with_new_records_changes_only_the_block(self):
        first = dl.splice(b"A\n", self.block)
        newer = dl.render(PR, HEAD, [rec(defect_id="D77")]).encode("utf-8")
        second = dl.splice(first, newer)
        self.assertTrue(second.startswith(b"A\n"))
        self.assertIn(b"D77", second)
        self.assertNotIn(b"| D1 |", second)

    def test_block_file_with_no_pair_is_refused(self):
        with self.assertRaises(ValueError):
            dl.splice(b"x\n", b"no markers here")


class CheckTests(unittest.TestCase):
    def test_ok_counts_rows(self):
        self.assertEqual(dl.check(body_with([rec(), rec(defect_id="D2")])), "DEFECT_LEDGER ok n=2")

    def test_ok_for_an_empty_table(self):
        self.assertEqual(dl.check(body_with([])), "DEFECT_LEDGER ok n=0")

    def test_ok_for_distinguished_and_deferred_rows_that_state_their_reason(self):
        records = [rec(defect_id="D2", disposition="distinguished: not our code", regression_test="n/a",
                       old_red_sha="n/a", new_green_sha="n/a", combination_tests="none", combination_reason="n/a"),
                   rec(defect_id="D3", disposition="deferred: SOME-CARD-1", regression_test="n/a",
                       old_red_sha="n/a", new_green_sha="n/a", combination_tests="none", combination_reason="n/a")]
        self.assertEqual(dl.check(body_with(records)), "DEFECT_LEDGER ok n=2")

    def test_missing(self):
        self.assertEqual(dl.check(b"just a body"), "DEFECT_LEDGER WARN missing")
        self.assertEqual(dl.check(b""), "DEFECT_LEDGER WARN missing")

    def test_incomplete_row_with_MISSING(self):
        line = dl.check(body_with([rec(), rec(defect_id="D2", claim=None)]))
        self.assertEqual(line, "DEFECT_LEDGER WARN incomplete rows=D2")

    def test_incomplete_fixed_row_without_red_on_old_evidence(self):
        for kw in ({"old_red_sha": "n/a"}, {"old_red_sha": "not-a-sha"}, {"new_green_sha": "n/a"}):
            line = dl.check(body_with([rec(**kw)]))
            self.assertEqual(line, "DEFECT_LEDGER WARN incomplete rows=D1", (kw, line))

    def test_incomplete_fixed_row_without_combination_test_or_reason(self):
        for kw in ({"combination_tests": []}, {"combination_tests": "none"}, {"combination_tests": ["none"]}):
            line = dl.check(body_with([rec(**kw)]))
            self.assertEqual(line, "DEFECT_LEDGER WARN incomplete rows=D1", (kw, line))
        reasoned = dl.check(body_with([rec(combination_tests=[], combination_reason="pure rename, no neighbour edge")]))
        self.assertEqual(reasoned, "DEFECT_LEDGER ok n=1")

    def test_incomplete_names_every_bad_row(self):
        line = dl.check(body_with([rec(), rec(defect_id="D2", old_red_sha="n/a"), rec(defect_id="D3", source=None)]))
        self.assertEqual(line, "DEFECT_LEDGER WARN incomplete rows=D2,D3")

    def test_disposition_must_be_one_of_the_three_forms(self):
        for bad in ("wontfix", "distinguished", "deferred", "deferred: lowercase card", "distinguished:  "):
            line = dl.check(body_with([rec(disposition=bad)]))
            self.assertEqual(line, "DEFECT_LEDGER WARN incomplete rows=D1", (bad, line))

    def test_unreadable(self):
        self.assertTrue(dl.check(b"\xff\xfe").startswith("DEFECT_LEDGER WARN unreadable"))
        torn = (dl.BEGIN + "\n## Defect dispositions\n| Defect | x |\n|---|---|\n| only | three | cells |\n" + dl.END)
        self.assertTrue(dl.check(torn.encode()).startswith("DEFECT_LEDGER WARN unreadable"))
        self.assertTrue(dl.check(body_with(["junk"])).startswith("DEFECT_LEDGER WARN unreadable"))


class AdversarialTests(unittest.TestCase):
    """This card's own discipline applied to itself: each defect combined with a neighbouring edge."""

    def setUp(self):
        self.block = dl.render(PR, HEAD, [rec()]).encode("utf-8")
        self.good = dl.splice(b"keep me\n", self.block)

    def test_two_begin_markers_never_read_as_ok_and_splice_refuses(self):
        body = self.good + b"\n" + dl.BEGIN.encode() + b"\nstray\n"
        line = dl.check(body)
        self.assertTrue(line.startswith("DEFECT_LEDGER WARN"), line)
        self.assertNotEqual(line.split(" n=")[0], "DEFECT_LEDGER ok")
        with self.assertRaises(ValueError):
            dl.splice(body, self.block)

    def test_begin_without_end_warns_and_splice_neither_duplicates_nor_drops_text(self):
        body = b"prose\n" + dl.BEGIN.encode() + b"\nmore prose\n"
        self.assertTrue(dl.check(body).startswith("DEFECT_LEDGER WARN unreadable"))
        with self.assertRaises(ValueError):
            dl.splice(body, self.block)
        with tempfile.TemporaryDirectory() as tmp:
            bf, kf, out = (os.path.join(tmp, n) for n in ("body.md", "block.md", "out.md"))
            with open(bf, "wb") as fh:
                fh.write(body)
            with open(kf, "wb") as fh:
                fh.write(self.block)
            proc = subprocess.run([sys.executable, TOOL, "splice", "--body-file", bf, "--block-file", kf,
                                   "--out", out], capture_output=True)
            self.assertEqual(proc.returncode, 2)
            self.assertFalse(os.path.exists(out))
            with open(bf, "rb") as fh:
                self.assertEqual(fh.read(), body)

    def test_end_before_begin(self):
        body = (dl.END + "\n" + dl.BEGIN + "\n").encode()
        self.assertTrue(dl.check(body).startswith("DEFECT_LEDGER WARN unreadable"))

    def test_fixed_row_with_equal_old_and_new_sha_is_incomplete(self):
        self.assertEqual(dl.check(body_with([rec(old_red_sha=OLD, new_green_sha=OLD)])),
                         "DEFECT_LEDGER WARN incomplete rows=D1")
        short = dl.check(body_with([rec(old_red_sha=OLD, new_green_sha=OLD[:8])]))
        self.assertEqual(short, "DEFECT_LEDGER WARN incomplete rows=D1")

    def test_equal_shas_combined_with_a_missing_combination_still_names_the_row_once(self):
        line = dl.check(body_with([rec(old_red_sha=OLD, new_green_sha=OLD, combination_tests=[])]))
        self.assertEqual(line, "DEFECT_LEDGER WARN incomplete rows=D1")

    def test_a_marker_string_inside_a_claim_cannot_forge_a_second_block(self):
        forged = dl.render(PR, HEAD, [rec(claim="x " + dl.END + " y " + dl.BEGIN)])
        self.assertEqual(forged.count(dl.BEGIN), 1)
        self.assertEqual(forged.count(dl.END), 1)


def _run(*argv, **kw):
    return subprocess.run([sys.executable, TOOL] + list(argv), capture_output=True, text=True, **kw)


class CliTests(unittest.TestCase):
    def test_check_always_exits_zero_with_one_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = os.path.join(tmp, "good.md")
            with open(good, "wb") as fh:
                fh.write(body_with([rec()]))
            bad = os.path.join(tmp, "bad.md")
            with open(bad, "wb") as fh:
                fh.write(body_with([rec(old_red_sha="n/a")]))
            plain = os.path.join(tmp, "plain.md")
            write(plain, "nothing here")
            binary = os.path.join(tmp, "bin.md")
            with open(binary, "wb") as fh:
                fh.write(b"\xff\xfe\x00")
            cases = [(good, "DEFECT_LEDGER ok n=1"), (bad, "DEFECT_LEDGER WARN incomplete rows=D1"),
                     (plain, "DEFECT_LEDGER WARN missing"),
                     (os.path.join(tmp, "absent.md"), "DEFECT_LEDGER WARN unreadable"),
                     (binary, "DEFECT_LEDGER WARN unreadable")]
            for path, want in cases:
                proc = _run("check", "--body-file", path)
                self.assertEqual(proc.returncode, 0, (path, proc.stderr))
                self.assertEqual(len(proc.stdout.splitlines()), 1, proc.stdout)
                self.assertTrue(proc.stdout.startswith(want), (proc.stdout, want))
            for bad_argv in (["check"], ["check", "--bogus"], ["check", "--body-file"]):
                proc = _run(*bad_argv)
                self.assertEqual(proc.returncode, 0, bad_argv)
                self.assertTrue(proc.stdout.startswith("DEFECT_LEDGER WARN unreadable"), proc.stdout)

    def test_render_then_splice_then_check_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            recs, body, blk, out = (os.path.join(tmp, n) for n in ("recs.json", "body.md", "block.md", "out.md"))
            write(recs, json.dumps([rec(), rec(defect_id="D2")]))
            write(body, "## Summary\n\nhello\n")
            r = _run("render", "--pr", str(PR), "--head", HEAD, "--records", recs)
            self.assertEqual(r.returncode, 0, r.stderr)
            write(blk, r.stdout)
            s = _run("splice", "--body-file", body, "--block-file", blk, "--out", out)
            self.assertEqual(s.returncode, 0, s.stderr)
            self.assertEqual(_run("check", "--body-file", out).stdout.strip(), "DEFECT_LEDGER ok n=2")

    def test_render_of_an_unreadable_records_file_still_exits_zero_with_an_unreadable_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            recs = os.path.join(tmp, "recs.json")
            write(recs, "{not json")
            r = _run("render", "--pr", str(PR), "--head", HEAD, "--records", recs)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.count("unreadable"), 7, r.stdout)
            gone = _run("render", "--pr", str(PR), "--head", HEAD, "--records", os.path.join(tmp, "absent.json"))
            self.assertEqual(gone.returncode, 0)
            self.assertIn("unreadable", gone.stdout)


def _event(event, days_ago, **kw):
    utc = dl.fmt_utc(dl.parse_utc(NOW) - days_ago * 86400)
    return dict({"event": event, "utc": utc}, **kw)


class EscapesTests(unittest.TestCase):
    """Window = 7 days ending at NOW. 2 merged-defect records, 1 revert, 1 flake, 1 real merge_group failure."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.recdir = os.path.join(self.dir, "records")
        self.log = os.path.join(self.dir, "merged.jsonl")
        self.flakes = os.path.join(self.dir, "flakes.txt")
        write(self.flakes, "# known flakes\ntests.test_flaky::test_sometimes\n")
        self.events = [
            _event("merged", 2, pr=701), _event("merged", 3, pr=702), _event("merged", 9, pr=703),
            _event("revert", 1, sha="f" * 40, title='Revert "x"'),
            _event("merge_group_failure", 2, run_id="9001", test_id="tests.test_flaky::test_sometimes", pr=701),
            _event("merge_group_failure", 4, run_id="9002", test_id="tests.test_real::test_broken", pr=702),
            _event("merge_group_failure", 12, run_id="9003", test_id="tests.test_old::test_gone", pr=703),
        ]
        self._write_log()
        self.records = [
            rec(pr=701, defect_id="A", recorded_utc=dl.fmt_utc(dl.parse_utc(NOW) - 2 * 86400),
                regression_test="t.py::test_a", claim="claim alpha"),
            rec(pr=702, defect_id="B", recorded_utc=dl.fmt_utc(dl.parse_utc(NOW) - 3 * 86400),
                regression_test="t.py::test_b", claim="claim beta"),
            rec(pr=703, defect_id="OLD", recorded_utc=dl.fmt_utc(dl.parse_utc(NOW) - 8 * 86400),
                regression_test="t.py::test_old", claim="claim gamma"),
        ]
        self._write_records(self.records)

    def _write_log(self):
        write(self.log, "\n".join(json.dumps(e) for e in self.events) + "\n")

    def _write_records(self, records):
        write(os.path.join(self.recdir, "pr.json"), json.dumps(records))

    def _escapes(self, **kw):
        args = dict(merged_log=self.log, records_dir=self.recdir, since_days=7, known_flakes=self.flakes, now=NOW)
        args.update(kw)
        return dl.escapes(**args)

    def test_exact_counts_for_a_seven_day_window(self):
        lines = self._escapes()
        self.assertEqual(lines[0], "ESCAPES window=7d merged_defects=2 reverts=1 nonflake_merge_group_failures=1 "
                                   "repeat_failures=0", lines)

    def test_one_line_per_counted_item_with_its_source(self):
        text = "\n".join(self._escapes())
        self.assertEqual(len([ln for ln in self._escapes() if ln.startswith("ESCAPE merged_defect ")]), 2)
        self.assertIn("ESCAPE merged_defect A pr=701 src=", text)
        self.assertIn("ESCAPE merge_group_failure run=9002 test=tests.test_real::test_broken pr=702", text)
        self.assertIn("ESCAPE revert sha=%s" % ("f" * 8), text)
        self.assertNotIn("9001", text)  # the flake is excluded
        self.assertNotIn("9003", text)  # day 12 is outside the window
        self.assertNotIn("claim gamma", text)  # the day-8 record is outside the window

    def test_a_wider_window_admits_the_day_eight_record_and_the_day_twelve_failure(self):
        lines = self._escapes(since_days=9)
        self.assertIn("window=9d merged_defects=3 ", lines[0])
        lines = self._escapes(since_days=14)
        self.assertIn("merged_defects=3", lines[0])
        self.assertIn("nonflake_merge_group_failures=2", lines[0])

    def test_without_known_flakes_the_flake_counts(self):
        lines = self._escapes(known_flakes=None)
        self.assertIn("nonflake_merge_group_failures=2", lines[0])

    def test_a_record_for_a_pr_that_never_merged_is_not_a_merged_defect(self):
        self._write_records(self.records + [rec(pr=799, defect_id="UNMERGED", recorded_utc=dl.fmt_utc(
            dl.parse_utc(NOW) - 86400))])
        lines = self._escapes()
        self.assertIn("merged_defects=2", lines[0])
        self.assertNotIn("UNMERGED", "\n".join(lines))

    def test_same_test_id_on_two_prs_is_one_repeat_failure(self):
        shared = "t.py::test_shared"
        self._write_records([
            rec(pr=701, defect_id="A", recorded_utc=dl.fmt_utc(dl.parse_utc(NOW) - 2 * 86400),
                regression_test=shared, claim="alpha"),
            rec(pr=702, defect_id="B", recorded_utc=dl.fmt_utc(dl.parse_utc(NOW) - 3 * 86400),
                regression_test=shared, claim="beta")])
        lines = self._escapes()
        self.assertIn("repeat_failures=1", lines[0])
        self.assertTrue(any(ln.startswith("ESCAPE repeat ") and "701" in ln and "702" in ln for ln in lines), lines)

    def test_same_claim_key_on_two_prs_is_one_repeat_even_when_the_test_ids_differ(self):
        self._write_records([
            rec(pr=701, defect_id="A", recorded_utc=dl.fmt_utc(dl.parse_utc(NOW) - 2 * 86400),
                regression_test="t.py::test_a", claim="Splice  drops text."),
            rec(pr=702, defect_id="B", recorded_utc=dl.fmt_utc(dl.parse_utc(NOW) - 3 * 86400),
                regression_test="t.py::test_b", claim="splice drops text")])
        self.assertIn("repeat_failures=1", self._escapes()[0])

    def test_test_id_and_claim_both_shared_still_count_one_repeat(self):
        self._write_records([
            rec(pr=701, defect_id="A", recorded_utc=dl.fmt_utc(dl.parse_utc(NOW) - 2 * 86400)),
            rec(pr=702, defect_id="B", recorded_utc=dl.fmt_utc(dl.parse_utc(NOW) - 3 * 86400))])
        self.assertIn("repeat_failures=1", self._escapes()[0])

    def test_the_same_test_id_twice_on_one_pr_is_not_a_repeat(self):
        self._write_records([
            rec(pr=701, defect_id="A", recorded_utc=dl.fmt_utc(dl.parse_utc(NOW) - 2 * 86400)),
            rec(pr=701, defect_id="A2", recorded_utc=dl.fmt_utc(dl.parse_utc(NOW) - 2 * 86400))])
        self.assertIn("repeat_failures=0", self._escapes()[0])

    def test_a_repeat_outside_the_window_does_not_count(self):
        self._write_records([
            rec(pr=701, defect_id="A", recorded_utc=dl.fmt_utc(dl.parse_utc(NOW) - 2 * 86400)),
            rec(pr=702, defect_id="B", recorded_utc=dl.fmt_utc(dl.parse_utc(NOW) - 20 * 86400))])
        self.assertIn("repeat_failures=0", self._escapes()[0])

    def test_a_known_flake_failing_on_two_prs_is_not_a_repeat(self):
        self.events.append(_event("merge_group_failure", 1, run_id="9010", test_id="tests.test_flaky::test_sometimes",
                                  pr=702))
        self._write_log()
        self.assertIn("repeat_failures=0", self._escapes()[0])

    def test_real_merge_group_failure_matching_a_record_test_on_another_pr_is_a_repeat(self):
        self._write_records([rec(pr=701, defect_id="A", recorded_utc=dl.fmt_utc(dl.parse_utc(NOW) - 2 * 86400),
                                 regression_test="tests.test_real::test_broken", claim="alpha")])
        self.assertIn("repeat_failures=1", self._escapes()[0])

    def test_malformed_inputs_are_noted_and_never_crash(self):
        write(os.path.join(self.recdir, "broken.json"), "{nope")
        write(os.path.join(self.recdir, "scalar.json"), "42")
        with open(self.log, "a", encoding="utf-8") as fh:
            fh.write("this is not json\n[1, 2]\n")
        lines = self._escapes()
        self.assertTrue(lines[0].startswith("ESCAPES window=7d merged_defects=2 "), lines[0])
        self.assertGreaterEqual(len([ln for ln in lines if ln.startswith("ESCAPE unreadable")]), 3)

    def test_absent_inputs_give_zero_counts(self):
        lines = dl.escapes(merged_log=os.path.join(self.dir, "none.jsonl"), records_dir=os.path.join(self.dir, "nodir"),
                           since_days=7, known_flakes=None, now=NOW)
        self.assertTrue(lines[0].startswith("ESCAPES window=7d merged_defects=0 reverts=0 "), lines[0])

    def test_cli_exits_zero_and_prints_the_summary_first(self):
        proc = _run("escapes", "--merged-log", self.log, "--records-dir", self.recdir, "--since-days", "7",
                    "--known-flakes", self.flakes, "--now", NOW)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.splitlines()[0],
                         "ESCAPES window=7d merged_defects=2 reverts=1 nonflake_merge_group_failures=1 repeat_failures=0")
        junk = _run("escapes", "--merged-log", os.path.join(self.dir, "x"), "--since-days", "seven")
        self.assertEqual(junk.returncode, 0)
        self.assertTrue(junk.stdout.startswith("ESCAPES "), junk.stdout)

    def test_reverts_from_git_counts_revert_merges_inside_the_window(self):
        repo = os.path.join(self.dir, "repo")
        os.makedirs(repo)

        def git(*a):
            subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                            "-c", "commit.gpgsign=false"] + list(a), check=True, capture_output=True)
        try:
            git("init", "-q", "-b", "master")
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("git unavailable")
        write(os.path.join(repo, "a.txt"), "a")
        git("add", "a.txt")
        git("commit", "-q", "-m", "base")
        git("checkout", "-q", "-b", "side")
        write(os.path.join(repo, "b.txt"), "b")
        git("add", "b.txt")
        git("commit", "-q", "-m", "side work")
        git("checkout", "-q", "master")
        git("merge", "--no-ff", "-q", "-m", 'Revert "earlier change"', "side")
        no_log = os.path.join(self.dir, "no-log.jsonl")
        no_dir = os.path.join(self.dir, "nodir")
        lines = dl.escapes(merged_log=no_log, records_dir=no_dir, since_days=7, known_flakes=None,
                           now=dl.fmt_utc(time.time() + 60), git_repo=repo)
        self.assertIn("reverts=1", lines[0])
        self.assertEqual(len([ln for ln in lines if ln.startswith("ESCAPE revert ")]), 1, lines)
        later = dl.escapes(merged_log=no_log, records_dir=no_dir, since_days=7, known_flakes=None,
                           now=dl.fmt_utc(time.time() + 30 * 86400), git_repo=repo)
        self.assertIn("reverts=0", later[0])  # the same merge is outside a window that ended 30 days later


if __name__ == "__main__":
    unittest.main()
