"""Tests for tools/doctrine/doctrine_recall.py (DOCTRINE-RECALL-1).

All fixtures are small texts written to a temp dir; the real bus is only touched by the one opt-in
test, which is skipped unless the bus checkout exists. The tests live here, not under
tools/doctrine, because the CI unittest shards discover tools/repo_hygiene/test_*.py only.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from tools.doctrine import doctrine_recall as recall

TODAY = date(2026, 10, 6)

TRAPS_FIXTURE = """# Traps (append-only; date + machine + the test)

- 2026-08-08 (fleet): codex 0.142.5 refused lane models with a misleading `requires newer Codex` 400. Test: check version before blaming auth.
- 2026-08-09 (fleet): scheduled tasks aligned on the same minute-marks silently skip.

## Appended by MLV-App, 2026-08-09
- **version-gate costume**: codex-cli plus a newer lane model gives a misleading 400.
  Fix is an upgrade, not a re-login.
- **timestamp costume**: a UTC value stamped with a local offset parses into the future.

## TRAPS

### TRAP 2026-10-06 (mlv-app, VIRTUAL-TEN): a PowerShell 5.1 probe over SSH leaks memory in session 0

- **What:** a probe from a tailnet host runs as powershell.exe in session 0 and leaks to 85 GiB.
- **Effect:** commit charge 96 percent and the pagefile grows.
- **Remedy (one line):** run remote probes under pwsh 7 with a hard wall time and a memory cap.
- **Re-derive:** `Get-Process powershell | Where-Object SessionId -eq 0`
- **Prior art:** the same signature was solved on 2026-09-10.
- **Guard:** host-pressure-watch.ps1 writes an escalation file.
- **Check:** list session 0 processes.

```powershell
# this comment must not be read as a heading
## nor this one
```

### TRAP 2026-09-01 (fleet): an unrelated robocopy mirror deleted a backup folder

- **Remedy:** never mirror into a populated directory.

## TRAPS

- 2026-10-01 (adobe): a single line bullet about a watchdog restart storm. Fix: add jitter.
"""

PROSE_SECTION = """## adobe-ingester (2026-08-09, virtual-ten)

A prose paragraph that opens the section.

- first bullet that belongs to the prose entry
- second bullet that belongs to the prose entry

## Next section
- 2026-08-10 (fleet): standalone bullet
"""


def write(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


class SplitEntriesTests(unittest.TestCase):
    def setUp(self):
        self.entries = recall.split_entries(TRAPS_FIXTURE, "bus", "TRAPS.md")
        self.headings = [e.heading for e in self.entries]

    def test_h3_entries_split_under_the_bare_traps_heading(self):
        leak = [e for e in self.entries if e.heading.startswith("TRAP 2026-10-06")]
        self.assertEqual(len(leak), 1)
        self.assertIn("Remedy (one line)", leak[0].text)
        self.assertEqual(leak[0].line, TRAPS_FIXTURE.splitlines().index(
            "### TRAP 2026-10-06 (mlv-app, VIRTUAL-TEN): a PowerShell 5.1 probe over SSH leaks memory in session 0") + 1)
        robocopy = [e for e in self.entries if "robocopy" in e.heading]
        self.assertEqual(len(robocopy), 1)
        self.assertNotIn("PowerShell", robocopy[0].text)

    def test_bare_traps_heading_is_not_an_entry(self):
        self.assertNotIn("TRAPS", self.headings)

    def test_top_level_bullets_are_their_own_entries_including_wrapped_ones(self):
        self.assertTrue(any(h.startswith("2026-08-08 (fleet): codex") for h in self.headings))
        costume = [e for e in self.entries if e.heading.startswith("version-gate costume")]
        self.assertEqual(len(costume), 1)
        self.assertIn("Fix is an upgrade", costume[0].text)
        self.assertNotIn("timestamp costume", costume[0].text)
        self.assertTrue(any(h.startswith("2026-10-01 (adobe): a single line bullet") for h in self.headings))

    def test_fenced_code_never_splits_an_entry(self):
        self.assertFalse(any("this comment must not" in h or h.startswith("nor this") for h in self.headings))
        leak = next(e for e in self.entries if e.heading.startswith("TRAP 2026-10-06"))
        self.assertIn("nor this one", leak.text)

    def test_prose_section_keeps_its_bullets_and_a_bulleted_section_splits(self):
        entries = recall.split_entries(PROSE_SECTION, "bus", "RECEIPTS.md")
        self.assertEqual([e.heading for e in entries][0], "adobe-ingester (2026-08-09, virtual-ten)")
        self.assertIn("second bullet", entries[0].text)
        self.assertEqual(len(entries), 2)
        self.assertTrue(entries[1].heading.startswith("2026-08-10 (fleet)"))

    def test_dates_and_project_tags(self):
        by_head = {e.heading[:12]: e for e in self.entries}
        leak = next(e for e in self.entries if e.heading.startswith("TRAP 2026-10-06"))
        self.assertEqual(recall.entry_date(leak, TODAY), date(2026, 10, 6))
        self.assertEqual(recall.entry_project(leak), "mlv-app")
        costume = by_head["version-gate"]
        self.assertEqual(recall.entry_date(costume, TODAY), date(2026, 8, 9))  # from the section heading
        self.assertEqual(recall.entry_project(costume), "MLV-App")
        prose = recall.split_entries(PROSE_SECTION, "bus", "RECEIPTS.md")[0]
        self.assertEqual(recall.entry_project(prose), "adobe-ingester")

    def test_oversized_entry_is_chunked_with_real_line_numbers(self):
        body = "\n\n".join(f"paragraph {i} " + "word " * 150 for i in range(20))
        entries = recall.split_entries("## TRAPS\n\n" + body + "\n", "bus", "RECEIPTS.md")
        self.assertGreater(len(entries), 1)
        self.assertTrue(all(len(e.text) <= recall.MAX_ENTRY_CHARS for e in entries))
        lines = ("## TRAPS\n\n" + body + "\n").splitlines()
        for entry in entries:
            self.assertEqual(lines[entry.line - 1], entry.lines[0])


class ExtractTests(unittest.TestCase):
    def test_remedy_family_lines_are_extracted_and_truncated(self):
        entries = recall.split_entries(TRAPS_FIXTURE, "bus", "TRAPS.md")
        leak = next(e for e in entries if e.heading.startswith("TRAP 2026-10-06"))
        found = recall.extract_lines(leak)
        self.assertEqual([f.split(":")[0].split(" (")[0] for f in found],
                         ["Remedy", "Re-derive", "Prior art", "Guard", "Check"])
        self.assertTrue(found[0].startswith("Remedy (one line):"))
        self.assertNotIn("**", " ".join(found))

    def test_long_extract_is_truncated(self):
        entry = recall.Entry("bus", "TRAPS.md", 1, "h", "", ["### h", "- **Remedy:** " + "x" * 1000])
        (line,) = recall.extract_lines(entry)
        self.assertEqual(len(line), recall.EXTRACT_MAX_CHARS)
        self.assertTrue(line.endswith("..."))

    def test_entry_without_extract_lines_gets_a_snippet_instead(self):
        entry = recall.Entry("bus", "TRAPS.md", 4, "2026-08-09 (fleet): headline", "",
                             ["- 2026-08-09 (fleet): headline and then the body of the lesson"])
        hit = recall.hit_record(1, 1.0, entry, TODAY)
        self.assertEqual(hit["extracts"], [])
        self.assertEqual(hit["snippet"], "and then the body of the lesson")


class RankingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        write(self.dir, "TRAPS.md", TRAPS_FIXTURE)

    def test_symptom_words_put_the_leak_entry_first(self):
        result = recall.search("powershell ssh session pagefile leak", self.dir, None, today=TODAY, blame_budget_s=0)
        self.assertTrue(result["hits"][0]["heading"].startswith("TRAP 2026-10-06"))
        self.assertEqual(result["hits"][0]["rank"], 1)
        self.assertIn("pwsh 7", " ".join(result["hits"][0]["extracts"]))

    def test_rare_term_outweighs_common_term(self):
        entries = [
            recall.Entry("bus", "TRAPS.md", 1, "alpha", "", ["- alpha common common common common"]),
            recall.Entry("bus", "TRAPS.md", 2, "beta", "", ["- beta rarity"]),
            recall.Entry("bus", "TRAPS.md", 3, "gamma", "", ["- gamma common"]),
            recall.Entry("bus", "TRAPS.md", 4, "delta", "", ["- delta common"]),
        ]
        _, ranked = recall.rank(entries, "common rarity", TODAY, 4)
        self.assertEqual(ranked[0][1].heading, "beta")

    def test_newer_entry_wins_a_tie(self):
        old = recall.Entry("bus", "TRAPS.md", 1, "2025-01-01 (fleet): spooler stall", "", ["- 2025-01-01 (fleet): spooler stall"])
        new = recall.Entry("bus", "TRAPS.md", 2, "2026-10-01 (fleet): spooler stall", "", ["- 2026-10-01 (fleet): spooler stall"])
        filler = [recall.Entry("bus", "TRAPS.md", 10 + i, f"f{i}", "", [f"- filler {i}"]) for i in range(5)]
        _, ranked = recall.rank([old, new, *filler], "spooler stall", TODAY, 2)
        self.assertEqual([e.line for _, e in ranked], [2, 1])

    def test_no_match_reports_no_prior_art(self):
        result = recall.search("zzzquux", self.dir, None, today=TODAY, blame_budget_s=0)
        self.assertEqual(result["hits"], [])
        self.assertIn("recall: no prior art", recall.render(result))

    def test_project_memory_is_searched_when_present(self):
        memory = self.dir / "memory"
        memory.mkdir()
        write(memory, "stale-binary.md", "# Stale binary\n\nThe deployed exe was stale; Fix: rebuild before the A/B.\n")
        result = recall.search("stale deployed exe", self.dir, memory, today=TODAY, blame_budget_s=0)
        self.assertEqual(result["hits"][0]["source"], "project-memory")
        self.assertEqual(result["hits"][0]["file"], "project-memory/stale-binary.md")

    def test_long_pasted_log_is_capped_to_the_rarest_terms(self):
        log = " ".join(f"token{i}" for i in range(200)) + " pagefile"
        result = recall.search(log, self.dir, None, today=TODAY, blame_budget_s=0)
        self.assertLessEqual(len(result["terms"]), recall.MAX_QUERY_TERMS)
        self.assertEqual(result["terms"], ["pagefile"])


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        write(self.dir, "TRAPS.md", TRAPS_FIXTURE)

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = recall.main(list(argv))
            except SystemExit as exc:  # argparse usage errors
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_missing_bus_exits_2(self):
        code, out, err = self.run_main("--bus", str(self.dir / "nope"), "--blame-timeout", "0", "anything")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("bus not found", err)

    def test_usage_errors_exit_2(self):
        self.assertEqual(self.run_main("--bus", str(self.dir))[0], 2)  # no query
        self.assertEqual(self.run_main("--bus", str(self.dir), "--top", "0", "x")[0], 2)
        self.assertEqual(self.run_main("--bus", str(self.dir), "   ")[0], 2)

    def test_search_without_hits_still_exits_0(self):
        code, out, _ = self.run_main("--bus", str(self.dir), "--project-memory", str(self.dir / "none"), "zzzquux")
        self.assertEqual(code, 0)
        self.assertIn("recall: no prior art", out)

    def test_json_output_shape(self):
        code, out, _ = self.run_main(
            "--bus", str(self.dir), "--project-memory", str(self.dir / "none"), "--json", "--top", "2",
            "powershell", "ssh", "session", "leak",
        )
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(set(data), {"query", "terms", "bus", "entries_searched", "elapsed_s", "hits"})
        self.assertEqual(data["query"], "powershell ssh session leak")
        self.assertLessEqual(len(data["hits"]), 2)
        hit = data["hits"][0]
        self.assertEqual(
            set(hit),
            {"rank", "score", "source", "file", "line", "date", "project", "heading", "extracts", "snippet", "commit"},
        )
        self.assertEqual((hit["file"], hit["date"], hit["project"]), ("TRAPS.md", "2026-10-06", "mlv-app"))
        self.assertIsNone(hit["commit"])  # the fixture bus is not a git checkout

    def test_text_output_names_file_line_date_project_and_remedy(self):
        code, out, _ = self.run_main("--bus", str(self.dir), "--project-memory", str(self.dir / "none"), "powershell ssh pagefile")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"#1 score [\d.]+  TRAPS\.md:\d+  2026-10-06 \[mlv-app\]")
        self.assertIn("Remedy (one line): run remote probes under pwsh 7", out)

    def test_query_can_be_read_from_stdin(self):
        stdin = io.StringIO("powershell ssh pagefile leak\n")
        with mock.patch.object(sys, "stdin", stdin):
            code, out, _ = self.run_main("--bus", str(self.dir), "--project-memory", str(self.dir / "none"), "-")
        self.assertEqual(code, 0)
        self.assertIn("TRAPS.md", out)

    def test_bus_default_is_read_from_the_consumer_doc(self):
        doc = write(self.dir, "consumer.md", "Local checkout: `C:\\somewhere\\softwarefactory-fleet-doctrine` (read-only)\n")
        self.assertEqual(recall.resolve_bus(None, doc), Path(r"C:\somewhere\softwarefactory-fleet-doctrine"))
        bare = write(self.dir, "bare.md", "no path here\n")
        self.assertEqual(recall.resolve_bus(None, bare), recall.DEFAULT_BUS)
        self.assertEqual(recall.resolve_bus(None, self.dir / "missing.md"), recall.DEFAULT_BUS)
        self.assertEqual(recall.resolve_bus("X:/explicit", doc), Path("X:/explicit"))

    def test_repo_consumer_doc_names_the_bus_path(self):
        self.assertEqual(recall.resolve_bus(None), Path(r"C:\!Layi Wkspc\softwarefactory-fleet-doctrine"))


class TimingGuardTests(unittest.TestCase):
    def test_multi_megabyte_corpus_searches_in_under_five_seconds(self):
        with tempfile.TemporaryDirectory() as tmp:
            bus = Path(tmp)
            bullets = [
                f"- 2026-09-{(i % 28) + 1:02d} (fleet): trap number {i} about host{i % 97} and worker{i % 31} "
                + "lorem ipsum dolor sit amet consectetur " * 12
                for i in range(9000)
            ]
            write(bus, "TRAPS.md", "\n".join(bullets) + "\n")
            self.assertGreater((bus / "TRAPS.md").stat().st_size, 3_000_000)
            started = time.monotonic()
            result = recall.search("host7 worker3 trap lorem", bus, None, today=TODAY, blame_budget_s=0)
            elapsed = time.monotonic() - started
        self.assertEqual(len(result["hits"]), recall.DEFAULT_TOP)
        self.assertLess(elapsed, 5.0)

    def test_blame_is_skipped_once_its_deadline_has_passed(self):
        self.assertIsNone(recall._run_git(["--version"], time.monotonic() - 1.0))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(recall.blame_commit(Path(tmp), "TRAPS.md", 1, time.monotonic() + 2.0))


@unittest.skipUnless(recall.DEFAULT_BUS.is_dir(), "real doctrine bus checkout not present")
class RealBusTests(unittest.TestCase):
    """Opt-in: runs only where the bus checkout exists. Guards the incident this tool was built for."""

    def test_bachelor_leak_entry_is_in_the_top_three(self):
        result = recall.search("session 0 powershell ssh commit pagefile leak", recall.DEFAULT_BUS, None, blame_budget_s=0)
        top = result["hits"][:3]
        self.assertTrue(
            any("bachelor" in h["heading"].lower() or "ssh" in h["heading"].lower() for h in top),
            [h["heading"] for h in top],
        )
        self.assertLess(result["elapsed_s"], 5.0)


if __name__ == "__main__":
    unittest.main()
