"""HOST-FILE-CHURN-POOL-GROWTH-1: the receipt scan opens only receipt files touched in the window.

When this tools/session-checkpoint.py runs its Stop event it walked fleet-runs and parsed every
receipt (about 4,700 opens and 100k directory entries per run on the board, 2026-10-22, measured
in-process) just to find none for an ordinary chat session. This project copy is not wired in any
settings file today and who launched it in the 2026-10-21 trace is UNKNOWN; the live per-turn hook
is the user-scope ~/.claude/hooks/session-checkpoint.py, which this file does not change. A lane's
receipt for THIS session is rewritten while the session is alive, so a receipt FILE untouched for
the window is not opened. The filter is the file's own mtime, never its directory's: on NTFS an
in-place rewrite does not move the parent directory's mtime (r2 B2).
"""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("session_checkpoint_window", ROOT / "tools/session-checkpoint.py")
CHECKPOINT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKPOINT)
SESSION = "12345678-1234-4321-aaaa-0123456789ab"
DAY = 24 * 3600


class ReceiptWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.fleet = self.repo / ".claude-state/fleet-runs"
        self.fleet.mkdir(parents=True)

    def run_dir(self, name, age_days):
        run = self.fleet / name
        run.mkdir()
        receipt = run / "luna-001.receipt.json"
        receipt.write_text(json.dumps(dict(
            sessionId=SESSION, lane="luna", card=name, exitCode=None,
            promptPath=str(run / "luna-001.prompt.txt"), workDir=str(self.repo))), encoding="utf-8")
        then = time.time() - age_days * DAY
        os.utime(receipt, (then, then))
        os.utime(run, (then, then))
        return run, receipt

    def test_recent_run_dir_is_found(self):
        _, receipt = self.run_dir("recent", age_days=0.1)
        result = CHECKPOINT.scan_fleet_runs(self.repo, SESSION)
        self.assertEqual(str(receipt), result["receipt"])

    def test_old_receipt_is_not_opened(self):
        self.run_dir("stale", age_days=30)
        opened = []
        real_read = Path.read_text

        def read_text(self_, *a, **k):
            opened.append(self_.name)
            return real_read(self_, *a, **k)

        with patch.object(Path, "read_text", read_text):
            result = CHECKPOINT.scan_fleet_runs(self.repo, SESSION)
        self.assertIsNone(result)
        self.assertEqual([], opened)

    def test_receipt_rewritten_in_an_old_run_dir_is_found(self):
        # r2 B2: on NTFS, rewriting an existing file does not move its parent directory's mtime, so
        # a lane finishing inside a run dir created days ago leaves the directory looking idle.
        run, receipt = self.run_dir("old-dir-fresh-receipt", age_days=10)
        now = time.time()
        os.utime(receipt, (now, now))
        result = CHECKPOINT.scan_fleet_runs(self.repo, SESSION)
        self.assertEqual(str(receipt), result["receipt"])
        self.assertEqual("IN_PROGRESS", result["status"])

    def test_receipt_older_than_the_window_is_not_surfaced_from_a_fresh_run_dir(self):
        # The per-file mtime check on its own: the directory is fresh, only the file is stale.
        run, receipt = self.run_dir("fresh-dir-stale-receipt", age_days=10)
        now = time.time()
        os.utime(run, (now, now))
        opened = []
        real_read = Path.read_text

        def read_text(self_, *a, **k):
            opened.append(self_.name)
            return real_read(self_, *a, **k)

        with patch.object(Path, "read_text", read_text):
            result = CHECKPOINT.scan_fleet_runs(self.repo, SESSION)
        self.assertIsNone(result)
        self.assertEqual([], opened)

    def test_receipts_below_depth_one_are_never_read(self):
        # The depth bound (#539): fleet-runs/<run>/<receipt> is the deepest place a receipt lives;
        # lane dirs hold full repo copies whose nested fixtures must not be walked.
        run = self.fleet / "recent-run"
        nested = run / "scratch" / "fixtures"
        nested.mkdir(parents=True)
        receipt = nested / "luna-001.receipt.json"
        receipt.write_text(json.dumps(dict(sessionId=SESSION, lane="luna", exitCode=None)), encoding="utf-8")
        listed = []
        real_scandir = os.scandir

        def scandir(path="."):
            listed.append(Path(path).relative_to(self.fleet).as_posix())
            return real_scandir(path)

        with patch.object(CHECKPOINT.os, "scandir", scandir):
            result = CHECKPOINT.scan_fleet_runs(self.repo, SESSION)
        self.assertIsNone(result)
        self.assertEqual([".", "recent-run"], sorted(listed))

    def receipt_at(self, directory, mtime):
        receipt = directory / "luna-001.receipt.json"
        receipt.write_text(json.dumps(dict(sessionId=SESSION, lane="luna", exitCode=None)), encoding="utf-8")
        os.utime(receipt, (mtime, mtime))
        return receipt

    def test_receipt_exactly_at_the_cutoff_is_found_and_one_second_older_is_not(self):
        # Dot advisory 13ef349b on #563: pin the boundary with a frozen clock, not a 10-day margin.
        # The filter is `mtime >= cutoff`, so a receipt exactly RECEIPT_WINDOW_SECONDS old is in.
        now = 2_000_000_000.0
        cutoff = now - CHECKPOINT.RECEIPT_WINDOW_SECONDS
        edge = self.fleet / "edge"
        edge.mkdir()
        receipt = self.receipt_at(edge, cutoff)
        with patch.object(CHECKPOINT.time, "time", lambda: now):
            self.assertEqual(str(receipt), CHECKPOINT.scan_fleet_runs(self.repo, SESSION)["receipt"])
        os.utime(receipt, (cutoff - 1, cutoff - 1))
        with patch.object(CHECKPOINT.time, "time", lambda: now):
            self.assertIsNone(CHECKPOINT.scan_fleet_runs(self.repo, SESSION))

    def test_receipt_directly_under_fleet_runs_is_found(self):
        # Depth 0: the receipt sits in fleet-runs itself, not in a run directory.
        receipt = self.receipt_at(self.fleet, time.time())
        result = CHECKPOINT.scan_fleet_runs(self.repo, SESSION)
        self.assertEqual(str(receipt), result["receipt"])
        self.assertEqual("IN_PROGRESS", result["status"])

    def test_stale_receipt_directly_under_fleet_runs_is_not_opened(self):
        self.receipt_at(self.fleet, time.time() - 30 * DAY)
        opened = []
        real_read = Path.read_text

        def read_text(self_, *a, **k):
            opened.append(self_.name)
            return real_read(self_, *a, **k)

        with patch.object(Path, "read_text", read_text):
            result = CHECKPOINT.scan_fleet_runs(self.repo, SESSION)
        self.assertIsNone(result)
        self.assertEqual([], opened)

    def test_recent_receipt_wins_over_an_old_one(self):
        self.run_dir("stale", age_days=30)
        _, receipt = self.run_dir("fresh", age_days=1)
        result = CHECKPOINT.scan_fleet_runs(self.repo, SESSION)
        self.assertEqual(str(receipt), result["receipt"])


if __name__ == "__main__":
    unittest.main()
