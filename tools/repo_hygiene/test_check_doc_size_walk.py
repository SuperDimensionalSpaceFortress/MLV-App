"""HOST-FILE-CHURN-POOL-GROWTH-1: the Stop-hook doc-size check must not open frozen evidence trees.

tools/check-doc-size.py runs at every turn end. Its EXTRA_ROOTS walk of the git-ignored
.claude-state tree used to descend into fleet-runs (75k markdown files, most of them full repo
copies) and the exempt worktree/profiling trees, and only discarded them AFTER opening every
directory. A 3-minute FILE_IO trace put that walk among the top sources feeding the kernel
paged-pool churn. These fixtures pin that the walk never enters a directory whose every file is
exempt, still reports the coordination ledgers the `hub` tier exists to govern, and (r2) never
enumerates an ignored tree that is not on the explicit EXTRA_ROOTS allowlist.
"""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("check_doc_size", ROOT / "tools/check-doc-size.py")
DOCSIZE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DOCSIZE)
UNLISTED_ROOT = "UNLISTED_ROOT"  # the tier name a report row carries for an unlisted top-level state dir


class DocSizeWalkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        state = self.repo / ".claude-state"
        for rel in (
            "coordination/ledger.md",
            "continuity/handoff.md",
            "fleet-runs/lane-X-r1/summary.md",
            "fleet-runs/lane-X-r1/repo-copy/docs/guide.md",
            "worktrees/wt1/README.md",
            "profiling/kit/notes.md",
            "tmp/scratch/archive/old.md",
        ):
            path = state / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# fixture\n", encoding="utf-8")

    def walk(self):
        """Run the untracked walk and report (markdown paths found, directories it listed)."""
        listed = []
        real = os.scandir

        def recording(path="."):
            listed.append(Path(path).resolve().relative_to(self.repo.resolve()).as_posix())
            return real(path)

        with patch.object(DOCSIZE.os, "scandir", recording):
            found = DOCSIZE.untracked_markdown(self.repo)
        return sorted(found), listed

    def test_walk_never_lists_an_exempt_tree(self):
        _, listed = self.walk()
        for tree in (".claude-state/fleet-runs", ".claude-state/worktrees", ".claude-state/profiling"):
            self.assertEqual([], [d for d in listed if d == tree or d.startswith(tree + "/")], tree)

    def test_walk_still_reports_governed_coordination_ledgers(self):
        found, _ = self.walk()
        self.assertIn(".claude-state/coordination/ledger.md", found)
        self.assertIn(".claude-state/continuity/handoff.md", found)

    def scan_rows(self):
        """The full report for the fixture repo (no tracked files), plus the directories listed."""
        listed = []
        real = os.scandir

        def recording(path="."):
            listed.append(Path(path).resolve().relative_to(self.repo.resolve()).as_posix())
            return real(path)

        with patch.object(DOCSIZE, "tracked_markdown", lambda repo: []), \
                patch.object(DOCSIZE.os, "scandir", recording):
            rows = DOCSIZE.scan(self.repo)
        return rows, listed

    def test_every_file_the_walk_skips_is_exempt_or_in_an_unlisted_tree(self):
        # r3 B1: pruning is sound only when the skipped file could never have produced a finding.
        # A skipped file must be matched by an EXEMPT pattern, or sit under a top-level state dir
        # that the report names as an UNLISTED_ROOT row. Nothing may vanish without a name.
        (self.repo / ".claude-state/brand-new-home/notes").mkdir(parents=True)
        (self.repo / ".claude-state/brand-new-home/notes/ledger.md").write_text("# x\n", encoding="utf-8")
        rows, _ = self.scan_rows()
        reported = {r["path"] for r in rows}
        unlisted = {r["path"] for r in rows if r["tier"] == UNLISTED_ROOT}
        every = {p.relative_to(self.repo).as_posix() for p in self.repo.rglob("*.md")}
        for rel in sorted(every - reported):
            if DOCSIZE.classify(rel) is None:
                continue
            top = "/".join(rel.split("/")[:2])
            self.assertIn(top, unlisted, rel + " skipped without an EXEMPT pattern or an UNLISTED_ROOT row")

    def test_a_new_top_level_state_dir_is_reported_once_and_never_walked(self):
        home = self.repo / ".claude-state/brand-new-home"
        (home / "deep/er").mkdir(parents=True)
        (home / "top.md").write_text("# top\n", encoding="utf-8")
        (home / "deep/er/ledger.md").write_text("# deep\n", encoding="utf-8")
        rows, listed = self.scan_rows()
        mine = [r for r in rows if r["path"].startswith(".claude-state/brand-new-home")]
        self.assertEqual([".claude-state/brand-new-home"], [r["path"] for r in mine])
        self.assertEqual("WARN", mine[0]["state"])
        self.assertEqual(UNLISTED_ROOT, mine[0]["tier"])
        self.assertEqual([], [d for d in listed if d.startswith(".claude-state/brand-new-home")])

    def test_allowlisted_and_exempt_roots_raise_no_unlisted_row(self):
        rows, _ = self.scan_rows()
        self.assertEqual([], [r["path"] for r in rows if r["tier"] == UNLISTED_ROOT])

    def test_an_unlisted_ignored_tree_is_enumerated_zero_times(self):
        # r2 B1: pruning only the exempt trees still walked every OTHER ignored tree. An ignored
        # .claude-state/<anything-else>/ holding thousands of markdown files must cost nothing.
        bulk = self.repo / ".claude-state/unlisted-bulk/copy/docs"
        bulk.mkdir(parents=True)
        for index in range(50):
            (bulk / f"doc{index}.md").write_text("# bulk\n", encoding="utf-8")
        found, listed = self.walk()
        self.assertEqual([], [p for p in found if "unlisted-bulk" in p])
        self.assertEqual([], [d for d in listed if "unlisted-bulk" in d])
        self.assertIn(".claude-state/coordination/ledger.md", found)

    def test_depth_zero_state_markdown_is_governed_without_descending(self):
        (self.repo / ".claude-state/SESSION-HANDOFF.md").write_text("# handoff\n", encoding="utf-8")
        (self.repo / ".claude-state/unlisted-bulk").mkdir()
        (self.repo / ".claude-state/unlisted-bulk/deep.md").write_text("# deep\n", encoding="utf-8")
        found, listed = self.walk()
        self.assertIn(".claude-state/SESSION-HANDOFF.md", found)
        self.assertNotIn(".claude-state/unlisted-bulk/deep.md", found)
        self.assertNotIn(".claude-state/unlisted-bulk", listed)

    def depth_zero_scan_with_failing_entry(self, failing):
        """Run untracked_scan with the named depth-zero entry raising OSError from its metadata call.

        The failing entry is yielded FIRST, so a scan that abandons the directory on one bad
        entry loses everything that follows it.
        """
        real = os.scandir

        class Entry:
            def __init__(self, inner):
                self.inner = inner
                self.name = inner.name
                self.path = inner.path

            def is_file(self, *a, **k):
                if self.name == failing:
                    raise PermissionError(13, "Access is denied", self.path)
                return self.inner.is_file(*a, **k)

            def is_dir(self, *a, **k):
                return self.inner.is_dir(*a, **k)

        class Entries:
            def __init__(self, inner):
                found = sorted(inner, key=lambda e: (e.name != failing, e.name))
                self.found = [Entry(e) for e in found]

            def __enter__(self):
                return iter(self.found)

            def __exit__(self, *exc):
                return False

        def scandir(path="."):
            if Path(path).resolve() == (self.repo / ".claude-state").resolve():
                return Entries(real(path))
            return real(path)

        state = self.repo / ".claude-state"
        (state / failing).write_text("# locked\n", encoding="utf-8")
        (state / "SESSION-HANDOFF.md").write_text("# handoff\n", encoding="utf-8")
        (state / "brand-new-home").mkdir()
        stderr = io.StringIO()
        with patch.object(DOCSIZE.os, "scandir", scandir), contextlib.redirect_stderr(stderr):
            found, unlisted = DOCSIZE.untracked_scan(self.repo)
        return found, unlisted, stderr.getvalue()

    def test_one_inaccessible_depth_zero_entry_does_not_abandon_the_rest(self):
        # Dot advisory 13ef349b on #563: the outer OSError catch left the whole directory loop on
        # the first entry whose metadata call raised, so every later depth-zero entry vanished.
        found, unlisted, _ = self.depth_zero_scan_with_failing_entry("aaa-locked.md")
        self.assertIn(".claude-state/SESSION-HANDOFF.md", found)
        self.assertIn(".claude-state/brand-new-home", unlisted)
        self.assertIn(".claude-state/coordination/ledger.md", found)

    def test_the_inaccessible_depth_zero_entry_is_reported_not_dropped(self):
        found, _, stderr = self.depth_zero_scan_with_failing_entry("aaa-locked.md")
        self.assertNotIn(".claude-state/aaa-locked.md", found)
        self.assertIn("unreadable: .claude-state/aaa-locked.md", stderr)

    def test_trace_governed_count_excludes_unlisted_root_rows(self):
        # The text summary counts governed files as the rows that are not synthetic UNLISTED_ROOT
        # warnings; the --trace record must say the same number.
        def row(path, tier, state="ok"):
            return {"path": path, "bytes": 10 if tier != UNLISTED_ROOT else 0, "tier": tier,
                    "soft": 0, "hard": 0, "state": state}

        rows = [row("a.md", "entry"), row("b.md", "entry"), row(".claude-state/x", UNLISTED_ROOT, "WARN")]
        trace = self.repo / "trace.log"
        argv = ["check-doc-size.py", "--repo", str(self.repo), "--warn-only", "--trace", str(trace)]
        with patch.object(DOCSIZE, "scan", lambda repo: rows), patch.object(DOCSIZE.sys, "argv", argv), \
                contextlib.redirect_stdout(io.StringIO()):
            DOCSIZE.main()
        self.assertIn(" governed=2 ", trace.read_text(encoding="utf-8"))

    def test_the_allowlist_never_names_the_state_tree_whole(self):
        for root in DOCSIZE.EXTRA_ROOTS:
            self.assertTrue(root.startswith(".claude-state/"), root)
            self.assertNotEqual(".claude-state", root.rstrip("/"))
            self.assertIsNotNone(DOCSIZE.classify(root + "/x.md"), root)

    def test_exempt_directory_test_is_not_fooled_by_file_patterns(self):
        self.assertTrue(DOCSIZE.dir_is_exempt(".claude-state/fleet-runs"))
        self.assertTrue(DOCSIZE.dir_is_exempt(".claude-state/fleet-runs/lane-X-r1/repo-copy"))
        self.assertTrue(DOCSIZE.dir_is_exempt(".claude-state/worktrees"))
        self.assertFalse(DOCSIZE.dir_is_exempt(".claude-state/coordination"))
        self.assertFalse(DOCSIZE.dir_is_exempt(".claude-state"))
        # `**/*.bak` is a file pattern: a directory that merely ends in .bak is not wholly exempt.
        self.assertFalse(DOCSIZE.dir_is_exempt(".claude-state/coordination/rotated.bak"))

    def test_only_a_directory_pattern_can_exempt_a_whole_directory(self):
        # DOC-SIZE-DIR-EXEMPT-GUARD-TEST-VACUOUS-1: the `/**` restriction in dir_is_exempt was not
        # pinned. `**/x` is a file-name pattern; because fnmatch lets `*` cross `/`, the probe
        # path `<dir>/x` matches it for EVERY directory, so without the restriction the walk would
        # prune the whole state tree.
        with patch.object(DOCSIZE, "EXEMPT", DOCSIZE.EXEMPT + ["**/x"]):
            self.assertTrue(DOCSIZE.classify("anything/x") is None)
            self.assertFalse(DOCSIZE.dir_is_exempt(".claude-state/coordination"))
            self.assertFalse(DOCSIZE.dir_is_exempt(".claude-state/continuity/bus-drafts"))
            self.assertTrue(DOCSIZE.dir_is_exempt(".claude-state/fleet-runs"))

    def test_no_allowlisted_root_is_also_exempt(self):
        for root in DOCSIZE.EXTRA_ROOTS:
            self.assertFalse(DOCSIZE.dir_is_exempt(root), root)

    def test_every_exempt_state_dir_pattern_names_one_directory(self):
        # The hub refused a single `.claude-state/**`-wide exemption: every exempt state dir is
        # exempt by its own name (or the llm-playback-* corpus glob), never wholesale.
        for pattern in DOCSIZE.EXEMPT:
            if pattern.startswith(".claude-state/") and pattern.endswith("/**"):
                segment = pattern[len(".claude-state/"):-len("/**")]
                self.assertTrue(segment, pattern)
                self.assertNotIn(segment, ("*", "**"), pattern)


if __name__ == "__main__":
    unittest.main()
