"""HOST-FILE-CHURN-POOL-GROWTH-1: the per-turn receipt scan reads only recent run dirs.

session-checkpoint.py runs at every turn end. After #539 bounded the glob to two depths it still
listed every run dir under .claude-state/fleet-runs and parsed every receipt (about 4,700 opens and
100k directory entries per turn on the board, 2026-10-22) just to find none for an ordinary chat
session. A lane's receipt for THIS session is written while the session is alive, so receipts in
run dirs untouched for days cannot be this session's in-flight work and are not opened.
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

    def test_old_run_dir_is_not_opened_or_listed(self):
        self.run_dir("stale", age_days=30)
        opened, listed = [], []
        real_scandir, real_read = os.scandir, Path.read_text

        def scandir(path="."):
            listed.append(Path(path).name)
            return real_scandir(path)

        def read_text(self_, *a, **k):
            opened.append(self_.name)
            return real_read(self_, *a, **k)

        with patch.object(CHECKPOINT.os, "scandir", scandir), patch.object(Path, "read_text", read_text):
            result = CHECKPOINT.scan_fleet_runs(self.repo, SESSION)
        self.assertIsNone(result)
        self.assertEqual([], opened)
        self.assertNotIn("stale", listed)

    def test_recent_receipt_wins_over_an_old_one(self):
        self.run_dir("stale", age_days=30)
        _, receipt = self.run_dir("fresh", age_days=1)
        result = CHECKPOINT.scan_fleet_runs(self.repo, SESSION)
        self.assertEqual(str(receipt), result["receipt"])


if __name__ == "__main__":
    unittest.main()
