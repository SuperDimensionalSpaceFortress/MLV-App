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

WRAPPER_KILL_DEADLINE = 15  # seconds the wrapper gives taskkill and then the reap, each, before it says so and moves on


def _taskkill_tree(pid, deadline=WRAPPER_KILL_DEADLINE):
    subprocess.run(["taskkill.exe", "/T", "/F", "/PID", str(pid)], capture_output=True, timeout=deadline)


def _stop_process_tree(proc, deadline=WRAPPER_KILL_DEADLINE, taskkill=_taskkill_tree):
    """UM-SSH-G-PROBE-CLEANUP-HONESTY-1: kill proc's tree and reap it, each step bounded, and say what happened.

    The old wrapper called taskkill and then an unbounded proc.wait(), so a tree that would not die hung the
    wrapper itself. Returns a one-line message naming the step that hit its deadline, or confirming the reap.
    """
    notes = []
    try:
        taskkill(proc.pid, deadline)
    except subprocess.TimeoutExpired:
        notes.append("taskkill did not return within %d s" % deadline)
    try:
        proc.wait(timeout=deadline)
    except subprocess.TimeoutExpired:
        notes.append("pid %d still running %d s after the kill (reap unconfirmed, wrapper deadline hit)" % (proc.pid, deadline))
    return "; ".join(notes) if notes else "killed and reaped"


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
        deadline = 5  # cmd.exe + a cold pwsh must write the pidfile before it, or the test fails on a loaded host
        # Measured 6.4-6.8 s at a 3 s deadline on the loaded hub VM (~3.5 s of pwsh start-up and the guard's own
        # um-ssh scenario); 15 s of headroom is ~4x that and still far below a delayed-failure mutant (30 s).
        allowance = deadline + 15
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / "hung-probe.pid"
            env = dict(os.environ, MLV_HUNG_PROBE_PIDFILE=str(pidfile))
            started = time.monotonic()
            timed_out = False
            stop_note = ""
            # Output goes to files, not pipes: a surviving probe holds inherited pipe handles open, so
            # draining them after a timeout would block this test as hard as it blocks the guard.
            with open(Path(tmp) / "out.txt", "w+", encoding="utf-8", errors="replace") as out:
                guard = subprocess.Popen(
                    [pwsh, "-NoProfile", "-NonInteractive", "-File", str(GUARD),
                     "-KnownHostsOnly", "-HungProbe", "-ProbeDeadlineSeconds", str(deadline)],
                    stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, env=env,
                )
                try:
                    guard.wait(timeout=allowance)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    stop_note = _stop_process_tree(guard)
                elapsed = time.monotonic() - started
                out.seek(0)
                text = out.read()
            probe_pid = pidfile.read_text().strip() if pidfile.is_file() else ""
            still_alive = bool(probe_pid) and self._pid_alive(probe_pid)
            if probe_pid:
                subprocess.run(["taskkill.exe", "/T", "/F", "/PID", probe_pid], capture_output=True)
        self.assertFalse(timed_out, "the guard was still blocked on a hung ssh -G probe long after its %d s deadline (%s)" % (deadline, stop_note))
        self.assertTrue(probe_pid, "the hung probe never started:\n" + text)
        self.assertEqual(guard.returncode, 1, text)
        self.assertIn("ssh -G did not exit within %d s" % deadline, text)
        self.assertIn("OVERALL FAIL", text)
        self.assertLess(elapsed, allowance, "the guard took %.1f s for a %d s deadline (allowance %d s)" % (elapsed, deadline, allowance))
        self.assertFalse(still_alive, "the hung probe (pid %s) was left running after the deadline" % probe_pid)

    @unittest.skipUnless(sys.platform == "win32", "um-ssh.ps1 is Windows-only")
    def test_guard_without_ssh_exe_on_path_takes_the_argv_fallback(self):
        """UM-SSH-G-PROBE-DEADLINE-1 r2: no ssh.exe on PATH must reach the argv check, not throw under strict mode."""
        pwsh = shutil.which("pwsh")
        if not pwsh:
            self.skipTest("pwsh not installed")
        with tempfile.TemporaryDirectory() as tmp:
            # The seam is PATH itself: the guard looks ssh.exe up with Get-Command, and the only PATH entry
            # is an empty directory. pwsh is launched by absolute path, so it needs no PATH to start.
            env = dict(os.environ, PATH=tmp)
            result = subprocess.run(
                [pwsh, "-NoProfile", "-NonInteractive", "-File", str(GUARD), "-KnownHostsOnly"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120, env=env, cwd=tmp,
            )
        text = result.stdout + result.stderr
        self.assertNotIn("cannot be found on this object", text)
        self.assertEqual(result.returncode, 0, text)
        self.assertIn("PASS known_hosts path with space and ! stays one file", text)
        self.assertIn("OVERALL PASS", text)

    # ---- UM-SSH-G-PROBE-CLEANUP-HONESTY-1 ------------------------------------------------------------
    # Every fake below is made by the test itself in a temp dir; no lane, venue or host process is touched.

    def _make_fake_probe(self, tmp, name, ps1_body):
        """Write <name>.ps1 + <name>.cmd (the shape the guard's own -HungProbe fake uses); return the .cmd path."""
        ps1 = Path(tmp) / (name + ".ps1")
        cmd = Path(tmp) / (name + ".cmd")
        ps1.write_text(ps1_body, encoding="ascii")
        cmd.write_text('@"%s" -NoProfile -NonInteractive -File "%%~dp0%s.ps1" %%*\r\n' % (shutil.which("pwsh"), name), encoding="ascii")
        return cmd

    def _run_guard(self, tmp, args, pidfile=None, allowance=60):
        """Run the guard with output to a file (not a pipe); return (returncode, text, timed_out, elapsed)."""
        env = dict(os.environ)
        if pidfile:
            env["MLV_FAKE_PROBE_PIDFILE"] = str(pidfile)
        started = time.monotonic()
        timed_out = False
        with open(Path(tmp) / "guard-out.txt", "w+", encoding="utf-8", errors="replace") as out:
            guard = subprocess.Popen(
                [shutil.which("pwsh"), "-NoProfile", "-NonInteractive", "-File", str(GUARD)] + list(args),
                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, env=env,
            )
            try:
                guard.wait(timeout=allowance)
            except subprocess.TimeoutExpired:
                timed_out = True
                _stop_process_tree(guard)
            out.seek(0)
            text = out.read()
        if pidfile and Path(pidfile).is_file():
            pids = Path(pidfile).read_text().split()
            for pid in pids:
                subprocess.run(["taskkill.exe", "/T", "/F", "/PID", pid], capture_output=True)
            # A surviving fake holds the output file open; wait until each is really gone before the temp dir is removed.
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and any(self._pid_alive(pid) for pid in pids):
                time.sleep(0.25)
        return guard.returncode, text, timed_out, time.monotonic() - started

    _HUNG_PROBE = (
        "if ($env:MLV_FAKE_PROBE_PIDFILE) { Add-Content -Path $env:MLV_FAKE_PROBE_PIDFILE -Value $PID -Encoding ascii }\n"
        "[Console]::Out.Write('userknownhosts')\n[Console]::Out.Flush()\nStart-Sleep -Seconds 600\n"
    )
    # The root writes its partial line, starts a descendant that inherits the stdout pipe and sleeps, then EXITS.
    _DRAIN_PROBE = (
        "if ($env:MLV_FAKE_PROBE_PIDFILE) { Add-Content -Path $env:MLV_FAKE_PROBE_PIDFILE -Value $PID -Encoding ascii }\n"
        "$psi = New-Object System.Diagnostics.ProcessStartInfo\n"
        "$psi.FileName = (Get-Process -Id $PID).Path\n"
        "$psi.Arguments = '-NoProfile -NonInteractive -Command Start-Sleep -Seconds 600'\n"
        "$psi.UseShellExecute = $false\n"
        "$g = [System.Diagnostics.Process]::Start($psi)\n"
        "if ($env:MLV_FAKE_PROBE_PIDFILE) { Add-Content -Path $env:MLV_FAKE_PROBE_PIDFILE -Value $g.Id -Encoding ascii }\n"
        "[Console]::Out.Write('userknownhosts')\n[Console]::Out.Flush()\nexit 0\n"
    )

    @unittest.skipUnless(sys.platform == "win32", "um-ssh.ps1 is Windows-only")
    def test_exited_root_with_stdout_holding_descendant_reports_a_drain_timeout(self):
        """(a) The root exits but a descendant still holds stdout: say so, do not claim the probe tree was killed."""
        if not shutil.which("pwsh"):
            self.skipTest("pwsh not installed")
        deadline = 5
        with tempfile.TemporaryDirectory() as tmp:
            probe = self._make_fake_probe(tmp, "drain-probe", self._DRAIN_PROBE)
            rc, text, timed_out, _ = self._run_guard(
                tmp, ["-KnownHostsOnly", "-ProbeSshPath", str(probe), "-ProbeDeadlineSeconds", str(deadline)],
                pidfile=Path(tmp) / "drain.pids", allowance=deadline + 25)
        self.assertFalse(timed_out, text)
        self.assertEqual(rc, 1, text)
        self.assertIn("OVERALL FAIL", text)
        self.assertIn("drain did not finish within %d s" % deadline, text)
        self.assertIn("descendant still holds", text)
        self.assertNotIn("the probe tree was killed", text)
        self.assertNotIn("did not exit within", text)  # the root DID exit; that is the process-timeout wording

    @unittest.skipUnless(sys.platform == "win32", "um-ssh.ps1 is Windows-only")
    def test_hung_probe_reports_kill_attempted_and_reap_confirmed(self):
        """(b, GREEN half) A real kill of a hung probe is reported as attempted AND confirmed reaped."""
        if not shutil.which("pwsh"):
            self.skipTest("pwsh not installed")
        deadline = 5
        with tempfile.TemporaryDirectory() as tmp:
            probe = self._make_fake_probe(tmp, "hung-probe", self._HUNG_PROBE)
            rc, text, timed_out, _ = self._run_guard(
                tmp, ["-KnownHostsOnly", "-ProbeSshPath", str(probe), "-ProbeDeadlineSeconds", str(deadline)],
                pidfile=Path(tmp) / "hung.pids", allowance=deadline + 25)
        self.assertFalse(timed_out, text)
        self.assertEqual(rc, 1, text)
        self.assertIn("ssh -G did not exit within %d s" % deadline, text)
        self.assertIn("kill attempted", text)
        self.assertIn("reap confirmed", text)
        self.assertNotIn("reap unconfirmed", text)
        self.assertNotIn("the probe tree was killed", text)

    @unittest.skipUnless(sys.platform == "win32", "um-ssh.ps1 is Windows-only")
    def test_a_reap_that_cannot_be_confirmed_is_reported_as_unconfirmed(self):
        """(b) -ProbeKill Noop leaves the probe running, so the reap genuinely cannot be confirmed."""
        if not shutil.which("pwsh"):
            self.skipTest("pwsh not installed")
        deadline = 5
        with tempfile.TemporaryDirectory() as tmp:
            probe = self._make_fake_probe(tmp, "hung-probe", self._HUNG_PROBE)
            rc, text, timed_out, _ = self._run_guard(
                tmp, ["-KnownHostsOnly", "-ProbeSshPath", str(probe), "-ProbeDeadlineSeconds", str(deadline),
                      "-ProbeKill", "Noop"],
                pidfile=Path(tmp) / "noop.pids", allowance=deadline + 30)
        self.assertFalse(timed_out, text)
        self.assertEqual(rc, 1, text)
        self.assertIn("kill attempted", text)
        self.assertIn("reap unconfirmed", text)
        self.assertNotIn("reap confirmed", text)
        self.assertNotIn("the probe tree was killed", text)

    @unittest.skipUnless(sys.platform == "win32", "um-ssh.ps1 is Windows-only")
    def test_a_kill_exception_is_named_not_swallowed(self):
        """(b) -ProbeKill Throw makes the kill raise; the message names the exception text."""
        if not shutil.which("pwsh"):
            self.skipTest("pwsh not installed")
        deadline = 5
        with tempfile.TemporaryDirectory() as tmp:
            probe = self._make_fake_probe(tmp, "hung-probe", self._HUNG_PROBE)
            rc, text, timed_out, _ = self._run_guard(
                tmp, ["-KnownHostsOnly", "-ProbeSshPath", str(probe), "-ProbeDeadlineSeconds", str(deadline),
                      "-ProbeKill", "Throw"],
                pidfile=Path(tmp) / "throw.pids", allowance=deadline + 30)
        self.assertFalse(timed_out, text)
        self.assertEqual(rc, 1, text)
        self.assertIn("kill attempted but threw", text)
        self.assertIn("simulated kill failure", text)
        self.assertIn("reap unconfirmed", text)

    @unittest.skipUnless(sys.platform == "win32", "um-ssh.ps1 is Windows-only")
    def test_probe_deadline_must_be_positive_and_bounded(self):
        """(c) 0, -1 and values above 300 are refused with a clear message before any scenario runs."""
        if not shutil.which("pwsh"):
            self.skipTest("pwsh not installed")
        for bad in ("0", "-1", "301", "99999"):
            with self.subTest(deadline=bad), tempfile.TemporaryDirectory() as tmp:
                rc, text, timed_out, _ = self._run_guard(tmp, ["-KnownHostsOnly", "-ProbeDeadlineSeconds", bad], allowance=60)
                self.assertFalse(timed_out, text)
                self.assertEqual(rc, 2, text)
                self.assertIn("ProbeDeadlineSeconds must be between 1 and 300", text)
                self.assertIn("got %s" % bad, text)
                self.assertNotIn("== GREEN", text)  # refused up front, no scenario ran

    @unittest.skipUnless(sys.platform == "win32", "um-ssh.ps1 is Windows-only")
    def test_probe_deadline_bounds_are_inclusive(self):
        """(c) 1 and 300 pass the validator (a fast fake probe exits at once, so the scenario runs)."""
        if not shutil.which("pwsh"):
            self.skipTest("pwsh not installed")
        for ok in ("1", "300"):
            with self.subTest(deadline=ok), tempfile.TemporaryDirectory() as tmp:
                probe = self._make_fake_probe(tmp, "quick-probe", "[Console]::Out.Write('userknownhostsfile x')\nexit 0\n")
                rc, text, timed_out, _ = self._run_guard(
                    tmp, ["-KnownHostsOnly", "-ProbeSshPath", str(probe), "-ProbeDeadlineSeconds", ok], allowance=60)
                self.assertFalse(timed_out, text)
                self.assertNotIn("must be between", text)
                self.assertIn("== GREEN", text)  # got past the validator and ran the scenario

    def test_wrapper_kill_and_reap_are_bounded_and_say_so(self):
        """Item 2: the wrapper's taskkill + reap never wait forever and name the step that hit its deadline."""
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        try:
            # A kill stand-in that does nothing leaves the fake alive: the reap deadline must fire and say so.
            note = _stop_process_tree(sleeper, deadline=1, taskkill=lambda pid, deadline: None)
            self.assertIn("reap unconfirmed", note)
            self.assertIn("wrapper deadline hit", note)
            self.assertIn("pid %d" % sleeper.pid, note)

            # A kill stand-in that outlives its own deadline is named too.
            def slow_taskkill(pid, deadline):
                raise subprocess.TimeoutExpired("taskkill", deadline)
            note = _stop_process_tree(sleeper, deadline=1, taskkill=slow_taskkill)
            self.assertIn("taskkill did not return within 1 s", note)

            # Once the fake is really dead the reap is confirmed.
            sleeper.kill()
            self.assertEqual(_stop_process_tree(sleeper, deadline=5, taskkill=lambda pid, deadline: None), "killed and reaped")
        finally:
            if sleeper.poll() is None:
                sleeper.kill()
            sleeper.wait()

    @staticmethod
    def _pid_alive(pid):
        out = subprocess.run(["tasklist.exe", "/FI", "PID eq %s" % pid, "/NH"], capture_output=True, text=True).stdout
        return pid in out.split()


if __name__ == "__main__":
    unittest.main()
