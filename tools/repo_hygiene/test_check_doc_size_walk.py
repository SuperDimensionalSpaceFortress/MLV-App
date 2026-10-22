"""HOST-FILE-CHURN-POOL-GROWTH-1: the Stop-hook doc-size check must not open frozen evidence trees.

tools/check-doc-size.py runs at every turn end. Its EXTRA_ROOTS walk of the git-ignored
.claude-state tree used to descend into fleet-runs (75k markdown files, most of them full repo
copies) and the exempt worktree/profiling trees, and only discarded them AFTER opening every
directory. A 3-minute FILE_IO trace put that walk among the top sources feeding the kernel
paged-pool churn. These fixtures pin that the walk never enters a directory whose every file is
exempt, while still reporting the coordination ledgers the `hub` tier exists to govern.
"""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("check_doc_size", ROOT / "tools/check-doc-size.py")
DOCSIZE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DOCSIZE)


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

    def test_every_file_the_walk_skips_is_exempt(self):
        # Pruning is only sound if a skipped directory could never have produced a finding.
        found, _ = self.walk()
        every = {p.relative_to(self.repo).as_posix() for p in self.repo.rglob("*.md")}
        for rel in every - set(found):
            self.assertIsNone(DOCSIZE.classify(rel), rel)

    def test_exempt_directory_test_is_not_fooled_by_file_patterns(self):
        self.assertTrue(DOCSIZE.dir_is_exempt(".claude-state/fleet-runs"))
        self.assertTrue(DOCSIZE.dir_is_exempt(".claude-state/fleet-runs/lane-X-r1/repo-copy"))
        self.assertTrue(DOCSIZE.dir_is_exempt(".claude-state/worktrees"))
        self.assertFalse(DOCSIZE.dir_is_exempt(".claude-state/coordination"))
        self.assertFalse(DOCSIZE.dir_is_exempt(".claude-state"))
        # `**/*.bak` is a file pattern: a directory that merely ends in .bak is not wholly exempt.
        self.assertFalse(DOCSIZE.dir_is_exempt(".claude-state/coordination/rotated.bak"))


if __name__ == "__main__":
    unittest.main()
