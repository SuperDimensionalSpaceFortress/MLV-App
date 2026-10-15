"""KERNEL-INSTANCE-MAP-PROFILE-AGREEMENT-GUARD-1: the map's header profile agrees with its K3/K5 cut-over rows.

agents/factory-kernel-instance.md states its primary profile twice: once in the header prose
(``Profile: `code@rN` primary``) and again in the K3 and K5 table rows, which record the cut-over
to the newest ``code@rN``. PR #443 fixed a header that still said ``code@r10`` while those rows
recorded ``code@r11``; nothing tracked compared the two, so the contradiction could return silently.
This module is that comparison.

Rules (every one fails closed; "cannot find it" is a failure, never a pass):

- The header is the text before the first table row. It must carry exactly one distinct
  ``Profile: `code@rN` primary`` token.
- Rows whose first cell starts with K3 or K5 are the cut-over rows. They must name at least one
  ``code@rN``, and the NEWEST N among them must equal the header's N.

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
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MAP_FILE = "agents/factory-kernel-instance.md"

_HEADER_PROFILE = re.compile(r"Profile:\s*`(code@r(\d+))`\s+primary")
_ROW_PROFILE = re.compile(r"`?code@r(\d+)`?")
_CUTOVER_ROW = re.compile(r"^K[35]\b")


def check_agreement(document: str) -> tuple[list[str], str]:
    """Return (problems, summary). An empty problem list means the header and rows agree."""
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

    row_hits = []
    for number, raw in enumerate(lines, start=1):
        row = raw.strip()
        if not row.startswith("|"):
            continue
        cells = [c.strip() for c in row.strip("|").split("|")]
        if not cells or not _CUTOVER_ROW.match(cells[0]):
            continue
        for match in _ROW_PROFILE.finditer(row):
            row_hits.append((int(match.group(1)), cells[0].split()[0], number))
    if not row_hits:
        problems.append("no K3 or K5 table row names a `code@rN` profile: the cut-over record is missing or moved")

    if problems:
        return problems, "NOT_FOUND"

    header_n, header_token, header_line = header_hits[0]
    newest_n, newest_clause, newest_line = max(row_hits, key=lambda hit: (hit[0], -hit[2]))
    summary = f"header {header_token} (line {header_line}) vs {newest_clause} row code@r{newest_n} (line {newest_line})"
    if header_n != newest_n:
        problems.append(f"profile disagreement: header line {header_line} says code@r{header_n} primary, "
                        f"but the {newest_clause} row at line {newest_line} names code@r{newest_n}")
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


HEADER_R11 = (
    "# Map\n\nKernel r5. Profile: `code@r11` primary since the cut-over\n(previously `code@r10`), with a sub-instance.\n\n"
    "| Clause | MLV-App mechanism | Observable | Gap |\n|---|---|---|---|\n")
HEADER_R10 = HEADER_R11.replace("Profile: `code@r11` primary", "Profile: `code@r10` primary")
ROWS = (
    "| K3 identity | digest | from the stamp a receipt records the tree (code@r11 K3 default) | none |\n"
    "| K5 declared profile | gate | `profile: code@r11 kernel r5` | open |\n"
    "| K12 feedback | Bus. First filing (`code@r2`), refile `code@r10` | x | y |\n")


class ProfileAgreementTests(unittest.TestCase):
    def test_green_header_and_rows_agree(self) -> None:
        problems, summary = check_agreement(HEADER_R11 + ROWS)
        self.assertEqual(problems, [])
        self.assertTrue(summary.startswith("AGREE "), summary)

    def test_red_header_reverted_to_r10_names_both_values_and_lines(self) -> None:
        problems, summary = check_agreement(HEADER_R10 + ROWS)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("code@r10", problems[0])
        self.assertIn("code@r11", problems[0])
        self.assertIn("header line 3", problems[0])
        self.assertIn("K3 row at line 8", problems[0])
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

    def test_red_no_cutover_row_names_a_profile_fails_closed(self) -> None:
        problems, summary = check_agreement(HEADER_R11 + "| K12 feedback | Bus `code@r10` | x | y |\n")
        self.assertEqual(summary, "NOT_FOUND")
        self.assertIn("no K3 or K5 table row", problems[0])

    def test_red_empty_document_fails_closed(self) -> None:
        problems, summary = check_agreement("")
        self.assertEqual(summary, "NOT_FOUND")
        self.assertEqual(len(problems), 2, problems)

    def test_wrapped_header_token_is_found(self) -> None:
        document = HEADER_R11.replace("Profile: `code@r11` primary", "Profile:\n`code@r11` primary") + ROWS
        self.assertEqual(check_agreement(document)[0], [])


class ProfileAgreementLiveTreeTests(unittest.TestCase):
    def test_live_map_header_agrees_with_its_cutover_rows(self) -> None:
        document = (REPO_ROOT / MAP_FILE).read_text(encoding="utf-8")
        problems, summary = check_agreement(document)
        self.assertEqual(problems, [], summary)

    def test_cli_exit_codes(self) -> None:
        import subprocess
        import tempfile

        script = str(Path(__file__).resolve())
        live = (REPO_ROOT / MAP_FILE).read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            red = Path(tmp) / "reverted.md"
            red.write_text(live.replace("Profile: `code@r11` primary", "Profile: `code@r10` primary", 1), encoding="utf-8")
            run = lambda *args: subprocess.run([sys.executable, script, *args], capture_output=True,  # noqa: E731
                                               text=True, encoding="utf-8", timeout=120, check=False)
            green_proc, red_proc, missing_proc = run(), run(str(red)), run(str(Path(tmp) / "absent.md"))
        self.assertEqual(green_proc.returncode, 0, green_proc.stderr)
        self.assertTrue(green_proc.stdout.startswith("PROFILE_AGREEMENT GREEN"), green_proc.stdout)
        self.assertEqual(red_proc.returncode, 1, red_proc.stdout)
        self.assertIn("code@r10", red_proc.stderr)
        self.assertIn("code@r11", red_proc.stderr)
        self.assertEqual(missing_proc.returncode, 2, missing_proc.stderr)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
