"""UM-SSH-STDIN-BOUND-1: run the um-ssh.ps1 stdin/wall-clock guard under unittest discovery.

The guard itself is tools/profiling/test-um-ssh-stdin-bound.ps1 (a fake remote, no host contact).
um-ssh.ps1 is Windows-only (ssh.exe, taskkill), so this wrapper skips elsewhere.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
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

    @unittest.skipUnless(sys.platform == "win32", "um-ssh.ps1 is Windows-only")
    def test_hung_ssh_g_probe_fails_at_the_deadline_and_is_reaped(self):
        """UM-SSH-G-PROBE-DEADLINE-1: a probe that holds stdout open must fail the guard, not hang it."""
        pwsh = shutil.which("pwsh")
        if not pwsh:
            self.skipTest("pwsh not installed")
        deadline = 3
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / "hung-probe.pid"
            env = dict(os.environ, MLV_HUNG_PROBE_PIDFILE=str(pidfile))
            started = time.monotonic()
            timed_out = False
            # Output goes to files, not pipes: a surviving probe holds inherited pipe handles open, so
            # draining them after a timeout would block this test as hard as it blocks the guard.
            with open(Path(tmp) / "out.txt", "w+", encoding="utf-8", errors="replace") as out:
                guard = subprocess.Popen(
                    [pwsh, "-NoProfile", "-NonInteractive", "-File", str(GUARD),
                     "-KnownHostsOnly", "-HungProbe", "-ProbeDeadlineSeconds", str(deadline)],
                    stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, env=env,
                )
                try:
                    guard.wait(timeout=45)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    subprocess.run(["taskkill.exe", "/T", "/F", "/PID", str(guard.pid)], capture_output=True)
                    guard.wait()
                elapsed = time.monotonic() - started
                out.seek(0)
                text = out.read()
            probe_pid = pidfile.read_text().strip() if pidfile.is_file() else ""
            still_alive = bool(probe_pid) and self._pid_alive(probe_pid)
            if probe_pid:
                subprocess.run(["taskkill.exe", "/T", "/F", "/PID", probe_pid], capture_output=True)
        self.assertFalse(timed_out, "the guard was still blocked on a hung ssh -G probe long after its %d s deadline" % deadline)
        self.assertTrue(probe_pid, "the hung probe never started:\n" + text)
        self.assertEqual(guard.returncode, 1, text)
        self.assertIn("ssh -G did not exit within %d s" % deadline, text)
        self.assertIn("OVERALL FAIL", text)
        self.assertLess(elapsed, 45, "the guard took %.1f s for a %d s deadline" % (elapsed, deadline))
        self.assertFalse(still_alive, "the hung probe (pid %s) was left running after the deadline" % probe_pid)

    @staticmethod
    def _pid_alive(pid):
        out = subprocess.run(["tasklist.exe", "/FI", "PID eq %s" % pid, "/NH"], capture_output=True, text=True).stdout
        return pid in out.split()


if __name__ == "__main__":
    unittest.main()
