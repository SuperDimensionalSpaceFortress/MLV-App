"""UM-SSH-STDIN-BOUND-1: run the um-ssh.ps1 stdin/wall-clock guard under unittest discovery.

The guard itself is tools/profiling/test-um-ssh-stdin-bound.ps1 (a fake remote, no host contact).
um-ssh.ps1 is Windows-only (ssh.exe, taskkill), so this wrapper skips elsewhere.
"""
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GUARD = REPO / "tools" / "profiling" / "test-um-ssh-stdin-bound.ps1"


class UmSshStdinBoundTests(unittest.TestCase):
    def test_guard_files_exist(self):
        self.assertTrue(GUARD.is_file(), GUARD)
        self.assertTrue((REPO / "tools" / "profiling" / "um-ssh.ps1").is_file())

    @unittest.skipUnless(sys.platform == "win32", "um-ssh.ps1 is Windows-only")
    def test_um_ssh_closes_stdin_and_is_wall_clock_bounded(self):
        pwsh = shutil.which("pwsh")
        if not pwsh:
            self.skipTest("pwsh not installed")
        result = subprocess.run(
            [pwsh, "-NoProfile", "-NonInteractive", "-File", str(GUARD)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=600,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OVERALL PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
