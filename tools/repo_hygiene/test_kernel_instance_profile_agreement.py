"""KERNEL-INSTANCE-MAP-PROFILE-AGREEMENT-GUARD-1: the map's header profile agrees with its K3/K5 cut-over rows.

agents/factory-kernel-instance.md states its primary profile twice: once in the header prose
(``Profile: `code@rN` primary``) and again in the K3 and K5 table rows, which record the cut-over
to the newest ``code@rN``. PR #443 fixed a header that still said ``code@r10`` while those rows
recorded ``code@r11``; nothing tracked compared the two, so the contradiction could return silently.
This module is that comparison.

Rules (every one fails closed; "cannot find it" is a failure, never a pass):

- The header is the text before the first table row. It must carry exactly one distinct
  ``Profile: `code@rN` primary`` token.
- A K3 row and a K5 row must BOTH be present. A row is recognised by its first cell starting with
  the K id, optionally wrapped in markdown emphasis (``K5``, ``**K5**``, ``*K5*``, ``_K5_``). A row
  that is missing or no longer recognised fails the check and names the missing row.
- EACH of those rows is compared on its own: it must name at least one ``code@rN``, and the NEWEST N
  within that row must equal the header's N. A lagging row is reported by name and line number with
  both values, so one row that names the current profile can never mask another that lags.
- ``code@rN`` must sit on a word boundary at BOTH edges. On the left, ``xcode@r12`` and ``barcode@r3`` are
  not profiles. On the right the check is a fail-closed ALLOWLIST (CoS #451 comment 6216180890, then PR #467):
  a row token counts only when the digits are followed by the end of the row, whitespace, or one of
  `` ` ) , ; | ``, or by a period that itself ends the sentence (end of row or whitespace after it).
  ``code@r11beta``, ``code@r11.5``, ``code@r11-rc``, ``code@r11+``, ``code@r11~1``, ``code@r11/x``,
  ``code@r11#a`` and ``code@r11`` followed by an en dash are therefore not profiles, and neither is anything
  else the allowlist does not name. A sentence-final ``code@r11.`` IS read. The header token already needs
  its closing backtick right after the digits, so it rejects the same suffixes.

Prevention rule (adopted repo-local, from the r1 -> r2 CoS review of PR #451): an agreement guard
that reads more than one source row carries one RED fixture per row where THAT row alone lags, plus
one missing-row fail-closed fixture per named source. A guard whose only RED fixture moves every
source at once passes its own tests while leaving all but one source unguarded.

``py -3 tools/repo_hygiene/test_kernel_instance_profile_agreement.py [path]`` checks one file (the
tracked map by default), prints one line, and exits 0 (agree), 1 (disagree or not found) or 2
(unreadable).
The tests run with ``py -3 -m unittest tools.repo_hygiene.test_kernel_instance_profile_agreement``.

Collected by the Repo Hygiene Python jobs through
``unittest discover -s tools/repo_hygiene -p "test_*.py" -t .``; no workflow step names this file.

NON-PROMISES:
- Only the profile REVISION is compared. The profile name (``code``) is not, and the cut-over
  timestamp in the header is not compared with the one in the K5 row.
- Rows other than K3 and K5 (K12 names ``code@r2`` and ``code@r10`` historically) are not read.
- A K3/K5 row that names a profile newer than the header makes the check fail by design: either
  the header is stale or the row announces a profile the map has not adopted.
- A row whose first cell no longer starts with its K id at all (for example ``Declared profile (K5)``)
  is not recognised and therefore fails as a missing row; the fix is to restore the id, not to loosen
  the matcher.
"""

from __future__ import annotations

import contextlib
import io
import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MAP_FILE = "agents/factory-kernel-instance.md"

_HEADER_PROFILE = re.compile(r"Profile:\s*`(code@r(\d+))`\s+primary")
# Right edge is an ALLOWLIST (CoS PR #467): the digits must be followed by the end of the row, whitespace, one of
# the closing delimiters ` ) , ; |, or a period that itself ends the sentence (end of row or whitespace after it).
# Live map (agents/factory-kernel-instance.md) follows its tokens with a backtick or a space only; ) , ; | are kept
# because rows legitimately wrap a token in parentheses or list it in prose. Anything else (+ ~ / # : ' an en dash,
# letters, digits, - or a period that continues) does NOT end the token, so the row names no profile.
_ROW_PROFILE = re.compile(r"(?<![\w-])code@r(\d+)(?=$|[\s`),;|]|\.(?:$|\s))")
_CUTOVER_ROW = re.compile(r"^[*_`\s]*(K[35])(?![0-9A-Za-z])")
_REQUIRED_ROWS = ("K3", "K5")


def check_agreement(document: str) -> tuple[list[str], str]:
    """Return (problems, summary). An empty problem list means the header and every cut-over row agree."""
    lines = document.splitlines()
    first_row = next((i for i, raw in enumerate(lines) if raw.lstrip().startswith("|")), len(lines))
    header = "\n".join(lines[:first_row])

    problems: list[str] = []
    header_hits = []
    for match in _HEADER_PROFILE.finditer(header):
        line = header[:match.start()].count("\n") + 1
        header_hits.append((int(match.group(2)), match.group(1), line))
    if not header_hits:
        problems.append("header names no primary profile: expected 'Profile: `code@rN` primary' before the first table row")
    elif len({n for n, _tok, _line in header_hits}) > 1:
        problems.append("header names more than one primary profile: "
                        + ", ".join(f"{tok} at line {line}" for _n, tok, line in header_hits))

    cutover_rows: list[tuple[str, int, list[int]]] = []
    for number, raw in enumerate(lines, start=1):
        row = raw.strip()
        if not row.startswith("|"):
            continue
        cells = [c.strip() for c in row.strip("|").split("|")]
        found = _CUTOVER_ROW.match(cells[0]) if cells else None
        if not found:
            continue
        cutover_rows.append((found.group(1), number, [int(m.group(1)) for m in _ROW_PROFILE.finditer(row)]))
    for kid in _REQUIRED_ROWS:
        if not any(row_kid == kid for row_kid, _number, _ns in cutover_rows):
            problems.append(f"no {kid} table row found: the {kid} cut-over record is missing, moved, or its first cell "
                            f"no longer starts with '{kid}'")
    for kid, number, ns in cutover_rows:
        if not ns:
            problems.append(f"{kid} row at line {number} names no `code@rN` profile")

    if problems:
        return problems, "NOT_FOUND"

    header_n, header_token, header_line = header_hits[0]
    summary = f"header {header_token} (line {header_line}) vs " + ", ".join(
        f"{kid} row code@r{max(ns)} (line {number})" for kid, number, ns in cutover_rows)
    for kid, number, ns in cutover_rows:
        if max(ns) != header_n:
            problems.append(f"profile disagreement: header line {header_line} says code@r{header_n} primary, "
                            f"but the {kid} row at line {number} names code@r{max(ns)}")
    if problems:
        return problems, "DISAGREE " + summary
    return problems, "AGREE " + summary


def main(argv: list[str]) -> int:
    path = Path(argv[0]) if argv else REPO_ROOT / MAP_FILE
    try:
        document = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        print(f"PROFILE_AGREEMENT UNKNOWN cannot read {path}: {error}", file=sys.stderr)
        return 2
    problems, summary = check_agreement(document)
    if problems:
        for problem in problems:
            print(f"PROFILE_AGREEMENT RED {path.name}: {problem}", file=sys.stderr)
        return 1
    print(f"PROFILE_AGREEMENT GREEN {summary}")
    return 0


def red_copy_of(live: str) -> tuple[str, int, int]:
    """Return (text, live_n, red_n): ``live`` with only its header profile moved off the live revision.

    The live N is read from the header, so the copy stays RED after the next cut-over. Raises
    AssertionError when the replace changes nothing (the header no longer carries the token).
    """
    match = _HEADER_PROFILE.search(live)
    if not match:
        raise AssertionError("live map header names no 'Profile: `code@rN` primary' token to move off its revision")
    live_n = int(match.group(2))
    red_n = live_n - 1 if live_n > 1 else live_n + 1
    red = live.replace(match.group(0), match.group(0).replace(match.group(1), f"code@r{red_n}"), 1)
    if red == live:
        raise AssertionError(f"RED copy equals the live map: replacing code@r{live_n} with code@r{red_n} changed nothing")
    return red, live_n, red_n


def _run_main(document: str) -> tuple[int, str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "map.md"
        path.write_text(document, encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main([str(path)])
    return code, out.getvalue(), err.getvalue()


HEADER_R11 = (
    "# Map\n\nKernel r5. Profile: `code@r11` primary since the cut-over\n(previously `code@r10`), with a sub-instance.\n\n"
    "| Clause | MLV-App mechanism | Observable | Gap |\n|---|---|---|---|\n")
HEADER_R10 = HEADER_R11.replace("Profile: `code@r11` primary", "Profile: `code@r10` primary")
K3_ROW = "| K3 identity | digest | from the stamp a receipt records the tree (code@r11 K3 default) | none |\n"
K5_ROW = "| K5 declared profile | gate | `profile: code@r11 kernel r5` | open |\n"
K12_ROW = "| K12 feedback | Bus. First filing (`code@r2`), refile `code@r10` | x | y |\n"
ROWS = K3_ROW + K5_ROW + K12_ROW


class ProfileAgreementTests(unittest.TestCase):
    def test_green_header_and_rows_agree(self) -> None:
        problems, summary = check_agreement(HEADER_R11 + ROWS)
        self.assertEqual(problems, [])
        self.assertTrue(summary.startswith("AGREE "), summary)
        self.assertIn("K3 row code@r11 (line 8)", summary)
        self.assertIn("K5 row code@r11 (line 9)", summary)

    def test_red_header_reverted_to_r10_names_both_values_and_lines(self) -> None:
        problems, summary = check_agreement(HEADER_R10 + ROWS)
        self.assertEqual(len(problems), 2, problems)
        self.assertIn("code@r10", problems[0])
        self.assertIn("code@r11", problems[0])
        self.assertIn("header line 3", problems[0])
        self.assertIn("K3 row at line 8", problems[0])
        self.assertIn("K5 row at line 9", problems[1])
        self.assertTrue(summary.startswith("DISAGREE "), summary)

    def test_red_row_ahead_of_header_fails(self) -> None:
        problems, _summary = check_agreement(HEADER_R11 + ROWS.replace("code@r11 kernel r5", "code@r12 kernel r5"))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("code@r12", problems[0])

    def test_green_older_profiles_in_other_rows_are_ignored(self) -> None:
        problems, _summary = check_agreement(HEADER_R11 + ROWS + "| K9 resume | witness | x | `code@r99` |\n")
        self.assertEqual(problems, [])

    def test_green_older_profile_in_a_cutover_row_does_not_win(self) -> None:
        rows = ROWS.replace("| K5 declared profile | gate | `profile: code@r11 kernel r5`",
                            "| K5 declared profile | gate | was code@r10, now `profile: code@r11 kernel r5`")
        self.assertEqual(check_agreement(HEADER_R11 + rows)[0], [])

    def test_red_header_without_primary_profile_fails_closed(self) -> None:
        problems, summary = check_agreement(HEADER_R11.replace(" primary", "") + ROWS)
        self.assertEqual(summary, "NOT_FOUND")
        self.assertIn("header names no primary profile", problems[0])

    def test_red_header_token_below_the_first_table_row_is_not_the_header(self) -> None:
        document = HEADER_R11.replace("Profile: `code@r11` primary", "Profile") + ROWS + "\nProfile: `code@r11` primary\n"
        problems, summary = check_agreement(document)
        self.assertEqual(summary, "NOT_FOUND")
        self.assertIn("header names no primary profile", problems[0])

    def test_red_two_different_primary_profiles_in_the_header_fail(self) -> None:
        document = HEADER_R11.replace("(previously `code@r10`)", "Profile: `code@r10` primary") + ROWS
        problems, _summary = check_agreement(document)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("more than one primary profile", problems[0])

    def test_red_no_cutover_row_at_all_fails_closed_naming_both(self) -> None:
        problems, summary = check_agreement(HEADER_R11 + K12_ROW)
        self.assertEqual(summary, "NOT_FOUND")
        self.assertEqual(len(problems), 2, problems)
        self.assertIn("no K3 table row", problems[0])
        self.assertIn("no K5 table row", problems[1])

    def test_red_empty_document_fails_closed(self) -> None:
        problems, summary = check_agreement("")
        self.assertEqual(summary, "NOT_FOUND")
        self.assertEqual(len(problems), 3, problems)

    def test_wrapped_header_token_is_found(self) -> None:
        document = HEADER_R11.replace("Profile: `code@r11` primary", "Profile:\n`code@r11` primary") + ROWS
        self.assertEqual(check_agreement(document)[0], [])


class PerRowAgreementTests(unittest.TestCase):
    """One RED fixture per row where that row alone lags, plus one missing-row fixture per source row."""

    def test_red_k5_lags_while_k3_agrees(self) -> None:
        document = HEADER_R11 + K3_ROW + K5_ROW.replace("code@r11", "code@r10") + K12_ROW
        code, _out, err = _run_main(document)
        self.assertEqual(code, 1, err)
        problems, _summary = check_agreement(document)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("K5 row at line 9 names code@r10", problems[0])
        self.assertIn("header line 3 says code@r11", problems[0])
        self.assertNotIn("K3", problems[0])

    def test_red_k3_lags_while_k5_agrees(self) -> None:
        document = HEADER_R11 + K3_ROW.replace("code@r11", "code@r10") + K5_ROW + K12_ROW
        code, _out, err = _run_main(document)
        self.assertEqual(code, 1, err)
        problems, _summary = check_agreement(document)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("K3 row at line 8 names code@r10", problems[0])
        self.assertNotIn("K5", problems[0])

    def test_red_k5_row_missing_fails_closed_naming_k5(self) -> None:
        document = HEADER_R11 + K3_ROW + K12_ROW
        code, _out, err = _run_main(document)
        self.assertEqual(code, 1, err)
        problems, summary = check_agreement(document)
        self.assertEqual(summary, "NOT_FOUND")
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("no K5 table row", problems[0])

    def test_red_k3_row_missing_fails_closed_naming_k3(self) -> None:
        document = HEADER_R11 + K5_ROW + K12_ROW
        code, _out, err = _run_main(document)
        self.assertEqual(code, 1, err)
        problems, summary = check_agreement(document)
        self.assertEqual(summary, "NOT_FOUND")
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("no K3 table row", problems[0])

    def test_red_k5_renamed_with_emphasis_and_lagging_is_caught_not_dropped(self) -> None:
        for first_cell in ("**K5**", "*K5*", "_K5_", "**K5** declared profile"):
            with self.subTest(first_cell=first_cell):
                k5 = K5_ROW.replace("K5 declared profile", first_cell).replace("code@r11", "code@r10")
                document = HEADER_R11 + K3_ROW + k5 + K12_ROW
                code, _out, err = _run_main(document)
                self.assertEqual(code, 1, err)
                problems, _summary = check_agreement(document)
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("K5 row at line 9 names code@r10", problems[0])

    def test_green_k5_renamed_with_emphasis_is_recognised(self) -> None:
        document = HEADER_R11 + K3_ROW + K5_ROW.replace("K5 declared profile", "**K5**") + K12_ROW
        problems, summary = check_agreement(document)
        self.assertEqual(problems, [])
        self.assertIn("K5 row code@r11 (line 9)", summary)

    def test_red_k5_first_cell_without_the_id_is_a_missing_row(self) -> None:
        document = HEADER_R11 + K3_ROW + K5_ROW.replace("K5 declared profile", "Declared profile (K5)") + K12_ROW
        problems, summary = check_agreement(document)
        self.assertEqual(summary, "NOT_FOUND")
        self.assertIn("no K5 table row", problems[0])

    def test_red_k5_row_naming_no_profile_fails_closed(self) -> None:
        document = HEADER_R11 + K3_ROW + K5_ROW.replace("`profile: code@r11 kernel r5`", "`profile: none`") + K12_ROW
        problems, summary = check_agreement(document)
        self.assertEqual(summary, "NOT_FOUND")
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("K5 row at line 9 names no", problems[0])

    def test_red_second_k5_row_lagging_is_not_masked_by_the_first(self) -> None:
        document = HEADER_R11 + ROWS + "| K5 typed terminals | gate | `code@r10` | open |\n"
        problems, _summary = check_agreement(document)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("K5 row at line 11 names code@r10", problems[0])

    def test_green_k50_is_not_mistaken_for_k5(self) -> None:
        document = HEADER_R11 + ROWS + "| K50 other | gate | `code@r3` | open |\n"
        self.assertEqual(check_agreement(document)[0], [])


class LeftBoundaryTests(unittest.TestCase):
    def test_green_embedded_profile_words_do_not_count(self) -> None:
        for noise in ("xcode@r12", "barcode@r3", "my-code@r13"):
            with self.subTest(noise=noise):
                k5 = K5_ROW.replace("| open |", f"| open, see {noise} |")
                self.assertEqual(check_agreement(HEADER_R11 + K3_ROW + k5)[0], [])

    def test_red_row_whose_only_profile_is_embedded_names_no_profile(self) -> None:
        k5 = K5_ROW.replace("`profile: code@r11 kernel r5`", "`profile: xcode@r11 kernel r5`")
        problems, summary = check_agreement(HEADER_R11 + K3_ROW + k5)
        self.assertEqual(summary, "NOT_FOUND")
        self.assertIn("K5 row at line 9 names no", problems[0])

    def test_green_backticked_and_line_start_profiles_still_count(self) -> None:
        k5 = "| K5 declared profile | gate | `code@r11` kernel; (code@r11) too | open |\n"
        problems, summary = check_agreement(HEADER_R11 + K3_ROW + k5)
        self.assertEqual(problems, [])
        self.assertIn("K5 row code@r11", summary)


class RightBoundaryTests(unittest.TestCase):
    """CoS #451 comment 6216180890: ``code@r11beta`` is not the profile token ``code@r11``."""

    SUFFIXES = ("beta", ".5", "-rc")
    # What the r1 denylist ``(?![\w.-])`` still read as a bare r11 (CoS probe on PR #467): the plus, tilde,
    # slash, hash and en dash each continue the token, and ``..`` is not a sentence end.
    UNLISTED_DELIMITERS = ("+", "~1", "/x", "#a", "–rc", "..", ".x", ":", "'")

    def test_red_row_whose_only_profile_has_a_suffix_names_no_profile(self) -> None:
        for suffix in self.SUFFIXES:
            with self.subTest(suffix=suffix):
                k5 = K5_ROW.replace("`profile: code@r11 kernel r5`", f"`profile: code@r11{suffix} kernel r5`")
                problems, summary = check_agreement(HEADER_R11 + K3_ROW + k5)
                self.assertEqual(summary, "NOT_FOUND")
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("K5 row at line 9 names no", problems[0])

    def test_red_k3_row_whose_only_profile_has_a_suffix_names_no_profile(self) -> None:
        for suffix in self.SUFFIXES:
            with self.subTest(suffix=suffix):
                k3 = K3_ROW.replace("(code@r11 K3 default)", f"(code@r11{suffix} K3 default)")
                problems, summary = check_agreement(HEADER_R11 + k3 + K5_ROW)
                self.assertEqual(summary, "NOT_FOUND")
                self.assertIn("K3 row at line 8 names no", problems[0])

    def test_red_suffixed_row_token_does_not_agree_with_the_same_header_revision(self) -> None:
        header = HEADER_R11.replace("code@r11", "code@r12", 1)
        k5 = K5_ROW.replace("code@r11", "code@r12beta")
        problems, _summary = check_agreement(header + K3_ROW.replace("code@r11", "code@r12") + k5)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("K5 row at line 9 names no", problems[0])

    def test_red_embedded_left_and_suffixed_right_still_names_no_profile(self) -> None:
        for suffix in self.SUFFIXES + self.UNLISTED_DELIMITERS:
            with self.subTest(suffix=suffix):
                k5 = K5_ROW.replace("`profile: code@r11 kernel r5`", f"`profile: xcode@r11{suffix} kernel r5`")
                self.assertIn("K5 row at line 9 names no", check_agreement(HEADER_R11 + K3_ROW + k5)[0][0])

    def test_red_right_edge_only_suffix_names_no_profile(self) -> None:
        """No left-edge noise at all: the suffix alone must void the token (CoS nit on the :368 left-only test)."""
        for suffix in ("beta",) + self.UNLISTED_DELIMITERS:
            with self.subTest(suffix=suffix):
                k5 = K5_ROW.replace("`profile: code@r11 kernel r5`", f"`profile: code@r11{suffix} kernel r5`")
                problems, summary = check_agreement(HEADER_R11 + K3_ROW + k5)
                self.assertEqual(summary, "NOT_FOUND")
                self.assertIn("K5 row at line 9 names no", problems[0])

    def test_red_unlisted_delimiter_row_token_does_not_agree_with_the_same_header_revision(self) -> None:
        """A denylist reads code@r11+ as r11; the allowlist must not, so the row is never counted as agreeing."""
        for suffix in self.UNLISTED_DELIMITERS:
            with self.subTest(suffix=suffix):
                k3 = K3_ROW.replace("(code@r11 K3 default)", f"(code@r11{suffix} K3 default)")
                problems, summary = check_agreement(HEADER_R11 + k3 + K5_ROW)
                self.assertEqual(summary, "NOT_FOUND")
                self.assertIn("K3 row at line 8 names no", problems[0])

    def test_green_suffixed_noise_beside_a_real_token_does_not_count(self) -> None:
        for suffix in self.SUFFIXES:
            with self.subTest(suffix=suffix):
                k5 = K5_ROW.replace("| open |", f"| open, see code@r99{suffix} |")
                self.assertEqual(check_agreement(HEADER_R11 + K3_ROW + k5)[0], [])

    def test_green_delimited_row_tokens_still_count(self) -> None:
        for tail in ("`", ")", ",", ";", " ", "|"):
            with self.subTest(tail=tail):
                k5 = f"| K5 declared profile | gate | (code@r11{tail}kernel | open |\n"
                problems, summary = check_agreement(HEADER_R11 + K3_ROW + k5)
                self.assertEqual(problems, [])
                self.assertIn("K5 row code@r11", summary)

    def test_green_sentence_ending_period_counts(self) -> None:
        """A period counts only when it ends the sentence: whitespace or the end of the row follows it."""
        for tail in (". |", ".  next |", ". Next sentence |"):
            with self.subTest(tail=tail):
                k5 = f"| K5 declared profile | gate | now code@r11{tail}\n"
                problems, summary = check_agreement(HEADER_R11 + K3_ROW + k5)
                self.assertEqual(problems, [])
                self.assertIn("K5 row code@r11", summary)

    def test_green_period_at_the_very_end_of_the_row_counts(self) -> None:
        self.assertEqual(check_agreement(HEADER_R11 + K3_ROW + "| K5 declared profile | gate | now code@r11.")[0], [])

    def test_red_period_followed_by_text_is_not_a_sentence_end(self) -> None:
        for tail in (".5", ".x", "..", ".-", ".|", ".`"):
            with self.subTest(tail=tail):
                k5 = f"| K5 declared profile | gate | now code@r11{tail} | open |\n"
                problems, summary = check_agreement(HEADER_R11 + K3_ROW + k5)
                self.assertEqual(summary, "NOT_FOUND")
                self.assertIn("K5 row at line 9 names no", problems[0])

    def test_red_header_token_with_a_suffix_is_not_a_primary_profile(self) -> None:
        for suffix in self.SUFFIXES:
            with self.subTest(suffix=suffix):
                header = HEADER_R11.replace("`code@r11` primary", f"`code@r11{suffix}` primary")
                problems, summary = check_agreement(header + ROWS)
                self.assertEqual(summary, "NOT_FOUND")
                self.assertIn("header names no primary profile", problems[0])


class ProfileAgreementLiveTreeTests(unittest.TestCase):
    def test_live_map_header_agrees_with_its_cutover_rows(self) -> None:
        document = (REPO_ROOT / MAP_FILE).read_text(encoding="utf-8")
        problems, summary = check_agreement(document)
        self.assertEqual(problems, [], summary)

    def test_red_copy_is_derived_from_the_live_header_not_hard_coded(self) -> None:
        for live_n in (1, 11, 12, 99):
            with self.subTest(live_n=live_n):
                live = HEADER_R11.replace("code@r11", f"code@r{live_n}") + ROWS.replace("code@r11", f"code@r{live_n}")
                self.assertEqual(check_agreement(live)[0], [])
                red, got_live, got_red = red_copy_of(live)
                self.assertEqual(got_live, live_n)
                self.assertEqual(got_red, live_n - 1 if live_n > 1 else live_n + 1)
                self.assertEqual(_run_main(red)[0], 1)

    def test_red_copy_fails_loudly_when_the_header_token_is_gone(self) -> None:
        with self.assertRaises(AssertionError):
            red_copy_of(HEADER_R11.replace(" primary", "") + ROWS)

    def test_cli_exit_codes(self) -> None:
        import subprocess

        script = str(Path(__file__).resolve())
        live = (REPO_ROOT / MAP_FILE).read_text(encoding="utf-8")
        red_text, live_n, red_n = red_copy_of(live)
        with tempfile.TemporaryDirectory() as tmp:
            red = Path(tmp) / "reverted.md"
            red.write_text(red_text, encoding="utf-8")
            run = lambda *args: subprocess.run([sys.executable, script, *args], capture_output=True,  # noqa: E731
                                               text=True, encoding="utf-8", timeout=120, check=False)
            green_proc, red_proc, missing_proc = run(), run(str(red)), run(str(Path(tmp) / "absent.md"))
        self.assertEqual(green_proc.returncode, 0, green_proc.stderr)
        self.assertTrue(green_proc.stdout.startswith("PROFILE_AGREEMENT GREEN"), green_proc.stdout)
        self.assertEqual(red_proc.returncode, 1, red_proc.stdout)
        self.assertIn(f"code@r{red_n}", red_proc.stderr)
        self.assertIn(f"code@r{live_n}", red_proc.stderr)
        self.assertEqual(missing_proc.returncode, 2, missing_proc.stderr)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
