"""ADOPT-REQUIRES-GUARD-1: every tracked fold ADOPT names a guard or says `Guard: none yet (reason)`.

agents/doctrine-consumer.md says "Adopting a trap means adding a guard to
tools/repo_hygiene/test_doctrine_guards.py (or writing 'Guard: none yet' with the reason)", and bus
RECEIPT 79f616c measured that an acked trap without a guard does not prevent recurrence. Nothing
enforced the sentence. This module does.

The tracked fold-record grammar is a table row of agents/factory-kernel-instance.md whose second
("MLV-App mechanism") cell starts with the word ADOPT (rows R14 and R15 at the time of writing).
Every such record must carry at least one ``Guard: ...`` clause, and every clause is one of:

- a registry id of tools/repo_hygiene/test_doctrine_guards.py (REGISTRY is imported, never copied),
  bare or in backticks;
- a guard file path in backticks that git tracks, optionally followed by ``:line`` and bare
  `` `:line` `` references (``Guard: `tests/x/test_y.py:12`, `:40` ``);
- ``none yet (<reason>)`` with a non-empty parenthesised reason. Such a record is OWED.

A clause that names a path or id that does not exist is a violation: a guard that is not there is
worse than an honest ``none yet``. ``Guard for <what>: ...`` is accepted as a clause start so one
record can be guarded in part and owed in part (R14).

``py -3 tools/repo_hygiene/test_doctrine_adopt_guard.py --owed`` prints ``ADOPT_GUARD_OWED n=<count>``
(the records carrying a ``none yet`` clause), then any violation on stderr, and exits 1 on a violation.

Collected by the Repo Hygiene Python jobs through
``unittest discover -s tools/repo_hygiene -p "test_*.py" -t .``; no workflow step names this file.

NON-PROMISES:
- Only the fold record in agents/factory-kernel-instance.md is checked. Fold decisions written to
  .claude-state/kernel/subject-ledger.md are untracked (hosted CI cannot read them) and are not
  counted here; docs/definitive-fix-plan-20260906.md ADOPT rows decide plan gaps, not bus traps.
- Only a row whose second cell STARTS with ADOPT is a record; an ADOPT written mid-sentence, or
  ADOPTED, is not.
- A named guard file is checked to exist and be tracked, not to test the trap it is cited for, and a
  line number after a path is not checked.
- The reason after ``none yet`` is checked to be non-empty text, not to be true.
- Existence is read from ``git ls-files`` through test_doctrine_guards.tracked_files (the index).
"""

from __future__ import annotations

import re
import subprocess
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):  # run as a script: put the repo root on sys.path for the import below
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.repo_hygiene import test_doctrine_guards as dg  # module import: no test class is re-collected here
from tools.repo_hygiene.test_doctrine_guards import Violation

REPO_ROOT = dg.REPO_ROOT
GUARD_ID = "ADOPT-REQUIRES-GUARD-1"
RECORD_FILE = "agents/factory-kernel-instance.md"

_ADOPT_CELL = re.compile(r"^ADOPT\b")
_CLAUSE_START = re.compile(r"\bGuard(?: for [^:|`]*?)?:\s*")
_LINE_REF = re.compile(r"^:\d+(?:-\d+)?$")
_LINE_SUFFIX = re.compile(r":\d+(?:-\d+)?$")
_REF_ITEM = re.compile(r"\s*(?:`([^`]+)`|(\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+\b))(?:\s*(?:,|;|\band\b))?")
_NONE_YET = re.compile(r"none yet\b", re.IGNORECASE)


@dataclass(frozen=True)
class AdoptRecord:
    id: str
    line: int
    text: str


def parse_adopt_records(document: str) -> list[AdoptRecord]:
    """Table rows whose second cell starts with ADOPT."""
    records = []
    for number, raw in enumerate(document.splitlines(), start=1):
        row = raw.strip()
        if not row.startswith("|"):
            continue
        cells = [c.strip() for c in row.strip("|").split("|")]
        if len(cells) >= 2 and _ADOPT_CELL.match(cells[1]):
            records.append(AdoptRecord(cells[0], number, " | ".join(cells[1:])))
    return records


def _balanced_reason(text: str) -> str | None:
    """The text inside a leading (...) group, nested parentheses included; None if there is none."""
    text = text.lstrip()
    if not text.startswith("("):
        return None
    depth = 0
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[1:index]
    return None


def _leading_refs(body: str) -> list[str]:
    refs, position = [], 0
    while True:
        match = _REF_ITEM.match(body, position)
        if match is None:
            return refs
        item = (match.group(1) or match.group(2)).strip()
        if not _LINE_REF.match(item):
            refs.append(_LINE_SUFFIX.sub("", item))
        position = match.end()


def check_record(record: AdoptRecord, registry_ids: set[str], tracked: set[str]) -> tuple[list[str], bool]:
    """Return (problems, owed): owed is True when a valid ``none yet`` clause is present."""
    starts = list(_CLAUSE_START.finditer(record.text))
    if not starts:
        return ["ADOPT record names no guard: add a REGISTRY id, a tracked guard file, or 'Guard: none yet (reason)'"], False
    problems, owed = [], False
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(record.text)
        body = record.text[start.end():end]
        none_yet = _NONE_YET.match(body)
        if none_yet:
            reason = _balanced_reason(body[none_yet.end():])
            if reason is None or not re.search(r"\w", reason):
                problems.append("'Guard: none yet' needs a non-empty reason in parentheses: 'Guard: none yet (reason)'")
            else:
                owed = True
            continue
        refs = _leading_refs(body)
        unknown = [r for r in refs if r not in registry_ids and r not in tracked]
        if unknown:
            problems.append("names a guard that does not exist (not a REGISTRY id, not a tracked file): " + ", ".join(unknown))
        elif not refs:
            problems.append("'" + start.group(0).strip() + "' names no REGISTRY id, no tracked guard file and no 'none yet (reason)'")
    return problems, owed


def check_document(document: str, registry_ids: set[str], tracked: set[str], rel: str = RECORD_FILE) -> tuple[list[Violation], int]:
    violations, owed = [], 0
    for record in parse_adopt_records(document):
        problems, is_owed = check_record(record, registry_ids, tracked)
        owed += int(is_owed)
        violations.extend(Violation(GUARD_ID, rel, record.line, f"{record.id}: {problem}") for problem in problems)
    return violations, owed


def check_live(root: Path = REPO_ROOT) -> tuple[list[Violation], int, int]:
    document = (root / RECORD_FILE).read_text(encoding="utf-8")
    violations, owed = check_document(document, set(dg.GUARDS), set(dg.tracked_files(root)))
    return violations, owed, len(parse_adopt_records(document))


def owed_line(owed: int) -> str:
    return f"ADOPT_GUARD_OWED n={owed}"


def main(argv: list[str]) -> int:
    violations, owed, _records = check_live()
    print(owed_line(owed))
    for violation in violations:
        print(violation.render(), file=sys.stderr)
    return 1 if violations else 0


TRACKED = {"tests/x/test_y.py", "tools/repo_hygiene/test_doctrine_guards.py"}
IDS = {"DG-FAKE-ONE", "PS-ONE-TRAP"}
HEADER = "| Clause | MLV-App mechanism | Observable | Gap |\n|---|---|---|---|\n"

RED_NO_GUARD = HEADER + "| R90 | ADOPT. Files a card. | the card | nothing else |\n"
RED_NONE_YET_NO_REASON = HEADER + "| R91 | ADOPT. Files a card. | the card | Guard: none yet. Prose is not a reason |\n"
RED_NONE_YET_EMPTY_REASON = HEADER + "| R92 | ADOPT. Files a card. | the card | Guard: none yet ( ) |\n"
RED_UNKNOWN_FILE = HEADER + "| R93 | ADOPT. Files a card. | the card | Guard: `tests/x/test_missing.py:3` |\n"
RED_UNKNOWN_ID = HEADER + "| R94 | ADOPT. Files a card. | the card | Guard: DG-NOT-REGISTERED |\n"
RED_PROSE_ONLY = HEADER + "| R95 | ADOPT. Files a card. | the card | Guard: see the outbox |\n"
RED_ONE_GOOD_ONE_BAD_CLAUSE = HEADER + (
    "| R96 | ADOPT. Files a card. | the card | Guard: `tests/x/test_y.py:1`. Guard for part B: none yet |\n")

GREEN_REGISTRY_ID = HEADER + "| R80 | ADOPT. Files a card. | the card | Guard: DG-FAKE-ONE (R80.1) |\n"
GREEN_BACKTICKED_ID = HEADER + "| R81 | ADOPT. Files a card. | the card | Guard: `PS-ONE-TRAP` |\n"
GREEN_FILE_WITH_LINES = HEADER + "| R82 | ADOPT. Files a card. | the card | Guard: `tests/x/test_y.py:12`, `:40`, `:41-44` (R82.1) |\n"
GREEN_NONE_YET = HEADER + "| R83 | ADOPT, not yet met. Files a card. | the card | Guard: none yet (no tick exists, so nothing to test). R83.2 holds |\n"
GREEN_NESTED_PARENS = HEADER + "| R84 | ADOPT. Files a card. | the card | Guard: none yet (packet 5 unlanded (see `TARGETS`); no target) |\n"
GREEN_PARTLY_GUARDED_PARTLY_OWED = HEADER + (
    "| R85 | ADOPT. Files a card. | the card | Guard: `tests/x/test_y.py:1`. "
    "Guard for R85.3 dispositions: none yet (packet 5 unlanded) |\n")
GREEN_NOT_A_RECORD = HEADER + (
    "| K1 | NONE. We ADOPT nothing here | x | Guard: nothing |\n"
    "| K12 | ADOPTED in the harvest: 3 | x | y |\n"
    "| K4 | FIXED. Guard: `tests/x/test_missing.py:1` | x | y |\n"
    "Not a table row: | R99 | ADOPT. x | y | z |\n")


class AdoptRecordParsingTests(unittest.TestCase):
    def test_only_a_second_cell_starting_with_adopt_is_a_record(self) -> None:
        self.assertEqual(parse_adopt_records(GREEN_NOT_A_RECORD), [])
        records = parse_adopt_records(GREEN_NONE_YET)
        self.assertEqual([(r.id, r.line) for r in records], [("R83", 3)])

    def test_header_and_separator_rows_are_not_records(self) -> None:
        self.assertEqual(parse_adopt_records(HEADER), [])


class AdoptGuardFixtureTests(unittest.TestCase):
    def _check(self, document: str) -> tuple[list[Violation], int]:
        return check_document(document, IDS, TRACKED)

    def _assert_red(self, document: str, record_id: str, needle: str) -> None:
        violations, _owed = self._check(document)
        self.assertEqual(len(violations), 1, [v.render() for v in violations])
        self.assertIn(record_id, violations[0].detail)
        self.assertIn(needle, violations[0].detail)
        self.assertEqual(violations[0].path, RECORD_FILE)
        self.assertEqual(violations[0].line, 3)

    def _assert_green(self, document: str, expected_owed: int) -> None:
        violations, owed = self._check(document)
        self.assertEqual([v.render() for v in violations], [])
        self.assertEqual(owed, expected_owed)

    def test_red_adopt_with_no_guard_and_no_reason_fails(self) -> None:
        self._assert_red(RED_NO_GUARD, "R90", "names no guard")

    def test_red_none_yet_without_parenthesised_reason_fails(self) -> None:
        self._assert_red(RED_NONE_YET_NO_REASON, "R91", "non-empty reason")

    def test_red_none_yet_with_empty_reason_fails(self) -> None:
        self._assert_red(RED_NONE_YET_EMPTY_REASON, "R92", "non-empty reason")

    def test_red_guard_file_that_is_not_tracked_fails(self) -> None:
        self._assert_red(RED_UNKNOWN_FILE, "R93", "tests/x/test_missing.py")

    def test_red_guard_id_that_is_not_in_the_registry_fails(self) -> None:
        self._assert_red(RED_UNKNOWN_ID, "R94", "DG-NOT-REGISTERED")

    def test_red_clause_that_names_nothing_fails(self) -> None:
        self._assert_red(RED_PROSE_ONLY, "R95", "names no REGISTRY id")

    def test_red_one_valid_clause_does_not_excuse_a_bad_one(self) -> None:
        self._assert_red(RED_ONE_GOOD_ONE_BAD_CLAUSE, "R96", "non-empty reason")

    def test_green_registry_id_passes(self) -> None:
        self._assert_green(GREEN_REGISTRY_ID, 0)

    def test_green_backticked_registry_id_passes(self) -> None:
        self._assert_green(GREEN_BACKTICKED_ID, 0)

    def test_green_tracked_file_with_line_refs_passes(self) -> None:
        self._assert_green(GREEN_FILE_WITH_LINES, 0)

    def test_green_none_yet_with_reason_passes_and_is_owed(self) -> None:
        self._assert_green(GREEN_NONE_YET, 1)

    def test_green_nested_parentheses_in_the_reason_pass(self) -> None:
        self._assert_green(GREEN_NESTED_PARENS, 1)

    def test_green_partly_guarded_partly_owed_counts_once(self) -> None:
        self._assert_green(GREEN_PARTLY_GUARDED_PARTLY_OWED, 1)

    def test_green_rows_that_are_not_adopt_records_are_ignored(self) -> None:
        self._assert_green(GREEN_NOT_A_RECORD, 0)

    def test_owed_counts_records_not_clauses(self) -> None:
        document = GREEN_NONE_YET + GREEN_PARTLY_GUARDED_PARTLY_OWED.split("\n", 2)[2] + GREEN_REGISTRY_ID.split("\n", 2)[2]
        self._assert_green(document, 2)

    def test_owed_line_format(self) -> None:
        self.assertEqual(owed_line(2), "ADOPT_GUARD_OWED n=2")


class AdoptGuardLiveTreeTests(unittest.TestCase):
    """The tracked fold record at HEAD is compliant, and the CLI reports its owed count."""

    def test_registry_is_imported_not_copied(self) -> None:
        violations, owed = check_document(
            HEADER + "| R70 | ADOPT. x | y | Guard: DG-GIT-PATHLIST |\n", set(dg.GUARDS), set())
        self.assertEqual((violations, owed), ([], 0))

    def test_live_tree_every_adopt_record_names_a_guard_or_none_yet_with_reason(self) -> None:
        violations, _owed, records = check_live()
        self.assertGreater(records, 0, f"{RECORD_FILE} has no ADOPT record: the grammar moved or the file did")
        self.assertEqual([v.render() for v in violations], [])

    def test_live_tree_owed_count_is_consistent_with_the_records(self) -> None:
        _violations, owed, records = check_live()
        self.assertTrue(0 <= owed <= records)

    def test_cli_owed_prints_the_count_line(self) -> None:
        proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--owed"],
                              capture_output=True, text=True, encoding="utf-8", timeout=300, check=False)
        first = proc.stdout.splitlines()[0] if proc.stdout else ""
        self.assertRegex(first, r"^ADOPT_GUARD_OWED n=\d+$", proc.stdout + proc.stderr)
        self.assertEqual(first, owed_line(check_live()[1]))
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    if "--owed" in sys.argv[1:]:
        sys.exit(main(sys.argv[1:]))
    unittest.main()
