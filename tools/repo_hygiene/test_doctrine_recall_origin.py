"""Guard: fold debt and recall must not read a bus clone whose HEAD trails origin/master.

DOCTRINE-BUS-CLONE-CURRENT-1. The shared bus clone's HEAD only moves when a heartbeat writer pulls, so
`--fold-debt` priced `since..HEAD` and missed commits already on origin (measured: HEAD 7 behind, one
owed RECEIPT invisible), and recall could not return their text. Every scenario here is a throwaway
bare origin, a seed repo that pushes to it, and a clone playing the shared bus.
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.doctrine import doctrine_recall as recall

BASE_TRAP = "- 2026-10-01 (fleet): base trap about a watchdog restart storm\n"
NEW_TRAP = BASE_TRAP + "- 2026-10-11 (fleet): quokka wedge in the spooler needs a jitter remedy\n"


class OriginAheadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.origin, self.seed, self.bus = root / "origin.git", root / "seed", root / "bus"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "master", str(self.origin)], check=True)
        self.seed.mkdir()
        self.git(self.seed, "init", "-q", "-b", "master")
        self.git(self.seed, "remote", "add", "origin", str(self.origin))
        self.cursor = self.commit(self.seed, "cursor", {"TRAPS.md": BASE_TRAP})
        self.git(self.seed, "push", "-q", "origin", "master")
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.bus)], check=True)
        self.assertEqual(self.git(self.bus, "rev-parse", "HEAD"), self.cursor)

    @staticmethod
    def git(repo, *args):
        done = subprocess.run(
            ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
             "-c", "commit.gpgsign=false", *args],
            check=True, capture_output=True, text=True,
        )
        return done.stdout.strip()

    def commit(self, repo, subject, files):
        for rel, text in files.items():
            path = Path(repo) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            self.git(repo, "add", "--", rel)
        self.git(repo, "commit", "-q", "-m", subject)
        return self.git(repo, "rev-parse", "HEAD")

    def publish_trap(self):
        """One TRAPS commit lands on origin; the bus clone has not pulled it and has not fetched it."""
        sha = self.commit(self.seed, "trap after the cursor", {"TRAPS.md": NEW_TRAP})
        self.git(self.seed, "push", "-q", "origin", "master")
        self.assertEqual(self.git(self.bus, "rev-parse", "--verify", "origin/master"), self.cursor)  # not fetched
        return sha

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = recall.main(list(argv))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def break_remote(self):
        self.git(self.bus, "remote", "set-url", "origin", str(self.origin.parent / "no-such-origin.git"))

    # -- fold debt -------------------------------------------------------------------------------

    def test_fold_debt_counts_a_commit_that_is_only_on_origin(self):
        new = self.publish_trap()
        code, out, err = self.run_main("--bus", str(self.bus), "--fold-debt", "--since", self.cursor)
        self.assertEqual((code, err), (0, ""))
        data = json.loads(out)
        self.assertEqual([c["sha"] for c in data["commits"]], [new])
        self.assertEqual((data["count"], data["fetch"], data["behind"]), (1, "ok", 1))
        self.assertEqual(data["priced"], ["HEAD", "origin/master"])
        self.assertEqual(self.git(self.bus, "rev-parse", "HEAD"), self.cursor)  # never pulled

    def test_fold_debt_resolves_a_cursor_the_clone_has_not_pulled_yet(self):
        new = self.publish_trap()
        code, out, err = self.run_main("--bus", str(self.bus), "--fold-debt", "--since", new)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["count"], 0)

    def test_fold_debt_still_counts_local_head_commits_that_origin_lacks(self):
        local = self.commit(self.bus, "unpushed export", {"RECEIPTS.md": "- 2026-10-11 (fleet): local receipt\n"})
        new = self.publish_trap()
        _, out, _ = self.run_main("--bus", str(self.bus), "--fold-debt", "--since", self.cursor)
        data = json.loads(out)
        self.assertEqual({c["sha"] for c in data["commits"]}, {local, new})
        self.assertEqual((data["ahead"], data["behind"]), (1, 1))

    def test_fold_debt_fetch_failure_is_loud_and_prices_head(self):
        self.publish_trap()
        local = self.commit(self.bus, "unpushed export", {"RECEIPTS.md": "- 2026-10-11 (fleet): local receipt\n"})
        self.break_remote()
        code, out, err = self.run_main("--bus", str(self.bus), "--fold-debt", "--since", self.cursor)
        self.assertEqual(code, 0)
        self.assertRegex(err, r"(?m)^FETCH_FAILED \S")
        data = json.loads(out)
        self.assertTrue(data["fetch"].startswith("FETCH_FAILED"))
        self.assertEqual(([c["sha"] for c in data["commits"]], data["priced"]), ([local], ["HEAD"]))

    def test_fold_debt_without_a_fetch_is_labelled_skipped(self):
        self.publish_trap()
        code, out, err = self.run_main("--bus", str(self.bus), "--fold-debt", "--since", self.cursor, "--no-fetch")
        self.assertEqual((code, err), (0, ""))
        data = json.loads(out)
        self.assertEqual((data["count"], data["fetch"], data["behind"]), (0, "skipped", 0))

    def test_a_local_only_bus_has_nothing_to_fetch_and_is_not_a_failure(self):
        lonely = Path(self.tmp.name) / "lonely"
        lonely.mkdir()
        self.git(lonely, "init", "-q", "-b", "master")
        cursor = self.commit(lonely, "cursor", {"TRAPS.md": BASE_TRAP})
        self.commit(lonely, "trap", {"TRAPS.md": NEW_TRAP})
        code, out, err = self.run_main("--bus", str(lonely), "--fold-debt", "--since", cursor)
        self.assertEqual((code, err), (0, ""))
        data = json.loads(out)
        self.assertEqual((data["count"], data["fetch"]), (1, "no-origin"))

    def test_fetch_timeout_is_bounded_to_thirty_seconds(self):
        for bad in ("0", "-1", "31"):
            with self.subTest(bad=bad):
                code, _, err = self.run_main("--bus", str(self.bus), "--fold-debt", "--since", self.cursor, f"--fetch-timeout={bad}")
                self.assertEqual(code, 2)
                self.assertIn("--fetch-timeout", err)

    def test_fetch_only_moves_the_remote_tracking_ref(self):
        self.publish_trap()
        self.run_main("--bus", str(self.bus), "--fold-debt", "--since", self.cursor)
        self.assertEqual(self.git(self.bus, "status", "--porcelain"), "")
        self.assertEqual(self.git(self.bus, "symbolic-ref", "--short", "HEAD"), "master")
        self.assertFalse((self.bus / ".git" / "FETCH_HEAD").exists())

    # -- recall ------------------------------------------------------------------------------------

    def search(self, *extra):
        return self.run_main("--bus", str(self.bus), "--project-memory", str(self.bus / "none"), *extra,
                             "quokka", "wedge", "spooler")

    def test_recall_returns_the_text_of_a_commit_that_is_only_on_origin(self):
        new = self.publish_trap()
        code, out, err = self.search("--json")
        self.assertEqual((code, err), (0, ""))
        data = json.loads(out)
        self.assertEqual((data["bus_state"]["behind"], data["bus_state"]["read"]), (1, "origin/master"))
        hit = data["hits"][0]
        self.assertIn("quokka wedge", hit["heading"])
        self.assertEqual((hit["commit_status"], hit["commit"]["sha"]), ("ok", new[:7]))  # blamed at origin, not the working tree
        self.assertEqual(self.git(self.bus, "rev-parse", "HEAD"), self.cursor)

    def test_recall_text_output_prints_behind(self):
        self.publish_trap()
        code, out, _ = self.search()
        self.assertEqual(code, 0)
        self.assertRegex(out, r"(?m)^BEHIND=1 \(")
        self.assertIn("quokka wedge", out)

    def test_recall_on_a_current_clone_prints_neither_behind_nor_fetch_failed(self):
        code, out, err = self.run_main("--bus", str(self.bus), "--project-memory", str(self.bus / "none"), "watchdog")
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn("BEHIND", out)
        self.assertNotIn("FETCH_FAILED", out)

    def test_recall_fetch_failure_prints_fetch_failed_and_searches_what_it_has(self):
        self.break_remote()
        code, out, err = self.run_main("--bus", str(self.bus), "--project-memory", str(self.bus / "none"), "watchdog")
        self.assertEqual((code, err), (0, ""))
        self.assertRegex(out.splitlines()[0], r"^FETCH_FAILED \S")
        self.assertIn("watchdog restart storm", out)

    def test_recall_with_a_stale_origin_ref_after_a_failed_fetch_says_so(self):
        self.publish_trap()
        self.git(self.bus, "fetch", "-q", "origin", "master")  # origin/master now known locally, then the remote dies
        self.break_remote()
        code, out, _ = self.search()
        self.assertEqual(code, 0)
        self.assertRegex(out, r"(?m)^FETCH_FAILED \S")
        self.assertRegex(out, r"(?m)^BEHIND=1 \(.*last good fetch")
        self.assertIn("quokka wedge", out)

    def test_recall_reads_head_when_the_clone_has_diverged_from_origin(self):
        self.commit(self.bus, "unpushed export", {"TRAPS.md": BASE_TRAP + "- 2026-10-11 (fleet): axolotl local only\n"})
        self.publish_trap()
        code, out, _ = self.run_main("--bus", str(self.bus), "--project-memory", str(self.bus / "none"), "axolotl")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"(?m)^BEHIND=1 \(AHEAD=1: diverged")
        self.assertIn("axolotl local only", out)

    def test_recall_no_fetch_does_not_touch_origin(self):
        self.publish_trap()
        code, out, _ = self.search("--no-fetch")
        self.assertEqual(code, 0)
        self.assertNotIn("BEHIND", out)  # origin/master was never fetched: nothing newer is known
        self.assertEqual(self.git(self.bus, "rev-parse", "--verify", "--quiet", "origin/master"),
                         self.cursor)


if __name__ == "__main__":
    unittest.main()
