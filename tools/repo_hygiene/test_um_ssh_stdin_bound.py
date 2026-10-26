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


TASKLIST_DEADLINE = 10  # seconds the wrapper gives one tasklist liveness probe before it reports the pid as not shown gone


def _taskkill_tree(pid, deadline=WRAPPER_KILL_DEADLINE):
    subprocess.run(["taskkill.exe", "/T", "/F", "/PID", str(pid)], capture_output=True, timeout=deadline)


def _best_effort_taskkill(pid, taskkill=_taskkill_tree):
    """Fixture cleanup kill: bounded, and a taskkill that hangs becomes a note instead of a hung or masked test.

    Returns "" when taskkill returned, else a one-line note for the test output.
    """
    try:
        taskkill(pid, WRAPPER_KILL_DEADLINE)
    except subprocess.TimeoutExpired:
        return "taskkill of pid %s did not return within %d s (fixture cleanup unconfirmed)" % (pid, WRAPPER_KILL_DEADLINE)
    return ""


def _pid_alive(pid, run=subprocess.run):
    """True if tasklist lists pid. A tasklist that does not answer in time cannot show the pid gone, so it counts as alive."""
    try:
        out = run(["tasklist.exe", "/FI", "PID eq %s" % pid, "/NH"], capture_output=True, text=True, timeout=TASKLIST_DEADLINE).stdout
    except subprocess.TimeoutExpired:
        return True
    return str(pid) in out.split()


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


FIXTURE_TMP_REMOVE_DEADLINE = 10  # seconds _FixtureTmp keeps retrying the removal of its temp dir before it reports the dir as left behind


def _remove_tree_bounded(path, deadline=FIXTURE_TMP_REMOVE_DEADLINE):
    """UM-SSH-FIXTURE-DIAG-PRESERVE-1: remove path, retrying for up to deadline s (a dying child may still hold a file).

    Returns "" when the dir is gone, else a one-line note naming the last error.
    """
    end = time.monotonic() + deadline
    while True:
        try:
            shutil.rmtree(path)
            return ""
        except OSError as exc:
            if not os.path.exists(path):
                return ""
            last = exc
        if time.monotonic() >= end:
            return "temp dir %s was not removed within %d s: %s: %s" % (path, deadline, type(last).__name__, last)
        time.sleep(0.25)


class _FixtureTmp:
    """Replacement for tempfile.TemporaryDirectory in this file's fixtures.

    TemporaryDirectory.__exit__ raises PermissionError when a Windows file is still open, and that REPLACES the
    failure in flight (the guard output, the stop/cleanup notes, the real assertion). Here the removal is bounded,
    and a removal that fails is APPENDED to the failure in flight; with no failure in flight it is raised itself,
    so a leaked handle is never swallowed. Assertions that read the guard output belong INSIDE the with block.
    """

    def __init__(self, deadline=FIXTURE_TMP_REMOVE_DEADLINE, remove=_remove_tree_bounded):
        self._deadline = deadline
        self._remove = remove
        self.path = None

    def __enter__(self):
        self.path = tempfile.mkdtemp(prefix="um-ssh-fixture-")
        return self.path

    def __exit__(self, exc_type, exc, tb):
        note = self._remove(self.path, self._deadline)
        if not note:
            return False
        line = "[fixture cleanup] " + note
        if exc is None:
            raise AssertionError(line)
        # Only AssertionError renders its args[0] as str(); other types (TimeoutExpired, OSError) would drop the note.
        if isinstance(exc, AssertionError) and exc.args and isinstance(exc.args[0], str):
            exc.args = (exc.args[0] + "\n" + line,) + exc.args[1:]
            return False
        raise AssertionError("%s\n(while handling %s: %s)" % (line, exc_type.__name__, exc)) from exc


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
        # The assertions sit inside the with: a teardown failure is then appended to them (see _FixtureTmp).
        with _FixtureTmp() as tmp:
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
            cleanup_note = _best_effort_taskkill(probe_pid) if probe_pid else ""
            if cleanup_note:
                text += "\n[fixture cleanup] " + cleanup_note
            self.assertFalse(timed_out, "the guard was still blocked on a hung ssh -G probe long after its %d s deadline (%s)" % (deadline, stop_note))
            self.assertTrue(probe_pid, "the hung probe never started:\n" + text)
            self.assertEqual(guard.returncode, 1, text)
            self.assertIn("ssh -G did not exit within %d s" % deadline, text)
            self.assertIn("OVERALL FAIL", text)
            self.assertLess(elapsed, allowance, "the guard took %.1f s for a %d s deadline (allowance %d s)" % (elapsed, deadline, allowance))
            self.assertFalse(still_alive, "the hung probe (pid %s) was left running after the deadline (or tasklist did not answer within %d s)\n%s" % (probe_pid, TASKLIST_DEADLINE, text))

    @unittest.skipUnless(sys.platform == "win32", "um-ssh.ps1 is Windows-only")
    def test_guard_without_ssh_exe_on_path_takes_the_argv_fallback(self):
        """UM-SSH-G-PROBE-DEADLINE-1 r2: no ssh.exe on PATH must reach the argv check, not throw under strict mode."""
        pwsh = shutil.which("pwsh")
        if not pwsh:
            self.skipTest("pwsh not installed")
        with _FixtureTmp() as tmp:
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
                cleanup_note = _best_effort_taskkill(pid)
                if cleanup_note:
                    text += "\n[fixture cleanup] " + cleanup_note
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
        with _FixtureTmp() as tmp:
            probe = self._make_fake_probe(tmp, "drain-probe", self._DRAIN_PROBE)
            rc, text, timed_out, _ = self._run_guard(
                tmp, ["-KnownHostsOnly", "-ProbeSshPath", str(probe), "-ProbeDeadlineSeconds", str(deadline)],
                pidfile=Path(tmp) / "drain.pids", allowance=deadline + 25)
            self.assertFalse(timed_out, text)
            self.assertEqual(rc, 1, text)
            self.assertIn("OVERALL FAIL", text)
            self.assertIn("drain did not finish within %d s" % deadline, text)
            # A likely cause, not an observed one: descendants are not tracked once the root exits.
            self.assertIn("a descendant may still hold the pipe open (descendants are not tracked once the root exits)", text)
            self.assertNotIn("descendant still holds", text)
            self.assertNotIn("the probe tree was killed", text)
            self.assertNotIn("did not exit within", text)  # the root DID exit; that is the process-timeout wording

    @unittest.skipUnless(sys.platform == "win32", "um-ssh.ps1 is Windows-only")
    def test_hung_probe_reports_kill_attempted_and_reap_confirmed(self):
        """(b, GREEN half) A real kill of a hung probe is reported as attempted AND confirmed reaped."""
        if not shutil.which("pwsh"):
            self.skipTest("pwsh not installed")
        deadline = 5
        with _FixtureTmp() as tmp:
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
        with _FixtureTmp() as tmp:
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
        with _FixtureTmp() as tmp:
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
            with self.subTest(deadline=bad), _FixtureTmp() as tmp:
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
            with self.subTest(deadline=ok), _FixtureTmp() as tmp:
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
            sleeper.wait(timeout=WRAPPER_KILL_DEADLINE)

    @staticmethod
    def _pid_alive(pid):
        return _pid_alive(pid)


_BOUNDED_SUBPROCESS_FUNCS = {"run", "check_output", "check_call", "call"}


def _unbounded_calls(source):
    """UM-SSH-FIXTURE-CLEANUP-BOUND-1: list '<line>: <call>' for every blocking call in source with no timeout=.

    Blocking calls are subprocess.run / check_output / check_call / call and any .wait( (a Popen reap).
    A **kwargs splat does not count as a timeout: the pin wants it spelled out where the call is read.
    """
    import ast

    found = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        func = node.func
        is_subprocess = (
            func.attr in _BOUNDED_SUBPROCESS_FUNCS
            and isinstance(func.value, ast.Name) and func.value.id == "subprocess"
        )
        if not (is_subprocess or func.attr == "wait"):
            continue
        if not any(kw.arg == "timeout" for kw in node.keywords):
            found.append("%d: %s" % (node.lineno, ast.unparse(node.func)))
    return sorted(found, key=lambda entry: int(entry.split(":")[0]))


class FixtureCleanupBoundTests(unittest.TestCase):
    """UM-SSH-FIXTURE-CLEANUP-BOUND-1: the wrapper's own fixture cleanup never blocks, and says so when it gives up."""

    def test_every_blocking_call_in_this_file_carries_a_timeout(self):
        unbounded = _unbounded_calls(Path(__file__).read_text(encoding="utf-8"))
        self.assertEqual(unbounded, [], "blocking calls with no timeout= (a hung child would hang the suite): " + ", ".join(unbounded))

    def test_the_pin_flags_each_blocking_shape_and_passes_bounded_ones(self):
        flagged = _unbounded_calls(
            "import subprocess\n"
            "subprocess.run(['x'])\n"
            "subprocess.check_output(['x'])\n"
            "subprocess.call(['x'])\n"
            "subprocess.check_call(['x'])\n"
            "p.wait()\n"
            "subprocess.run(['x'], **opts)\n"
        )
        self.assertEqual([entry.split(":")[0] for entry in flagged], ["2", "3", "4", "5", "6", "7"], flagged)
        self.assertEqual(_unbounded_calls(
            "import subprocess\n"
            "subprocess.run(['x'], timeout=1)\n"
            "subprocess.check_output(['x'], timeout=1)\n"
            "p.wait(timeout=1)\n"
            "p.poll()\n"
        ), [])

    def test_a_taskkill_that_times_out_is_named_not_raised(self):
        def stuck(pid, deadline):
            raise subprocess.TimeoutExpired("taskkill", deadline)
        note = _best_effort_taskkill("4242", taskkill=stuck)
        self.assertIn("taskkill of pid 4242 did not return within %d s" % WRAPPER_KILL_DEADLINE, note)
        self.assertIn("fixture cleanup unconfirmed", note)

    def test_a_taskkill_that_returns_leaves_no_note(self):
        calls = []
        self.assertEqual(_best_effort_taskkill("4242", taskkill=lambda pid, deadline: calls.append((pid, deadline))), "")
        self.assertEqual(calls, [("4242", WRAPPER_KILL_DEADLINE)])

    def test_a_tasklist_that_times_out_counts_as_still_alive(self):
        """If tasklist will not answer, the pid cannot be shown gone; report alive rather than hang or claim dead."""
        seen = {}

        def stuck(cmd, **kwargs):
            seen.update(kwargs)
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
        self.assertTrue(_pid_alive("4242", run=stuck))
        self.assertEqual(seen.get("timeout"), TASKLIST_DEADLINE)

    def test_a_tasklist_answer_is_read_as_before(self):
        def answer(text):
            return lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, stdout=text, stderr="")
        self.assertTrue(_pid_alive("4242", run=answer("python.exe   4242 Console   1   10,000 K\n")))
        self.assertFalse(_pid_alive("4242", run=answer("INFO: No tasks are running which match the specified criteria.\n")))

    # ---- UM-SSH-FIXTURE-DIAG-PRESERVE-1 --------------------------------------------------------------
    # tempfile.TemporaryDirectory raises PermissionError from __exit__ when a Windows file is still open, which
    # replaces the guard output and the real assertion. _FixtureTmp keeps the original failure and appends the note.

    @staticmethod
    def _failing_remove(path, deadline):
        return "temp dir %s was not removed within %d s: PermissionError: simulated open handle" % (path, deadline)

    def test_a_cleanup_failure_is_appended_to_the_failure_in_flight(self):
        with self.assertRaises(AssertionError) as caught:
            with _FixtureTmp(remove=self._failing_remove) as tmp:
                self.assertEqual(1, 2, "GUARD-OUTPUT-MARKER for " + tmp)
        message = str(caught.exception)
        self.assertIn("GUARD-OUTPUT-MARKER", message)
        self.assertIn("[fixture cleanup] temp dir", message)
        self.assertIn("simulated open handle", message)
        shutil.rmtree(tmp)

    def test_a_cleanup_failure_after_a_passing_body_is_raised_not_swallowed(self):
        with self.assertRaises(AssertionError) as caught:
            with _FixtureTmp(remove=self._failing_remove) as tmp:
                pass
        self.assertIn("[fixture cleanup]", str(caught.exception))
        self.assertIn("simulated open handle", str(caught.exception))
        shutil.rmtree(tmp)

    def test_a_non_assertion_failure_is_chained_with_the_cleanup_note(self):
        with self.assertRaises(AssertionError) as caught:
            with _FixtureTmp(remove=self._failing_remove) as tmp:
                raise subprocess.TimeoutExpired("guard", 5)
        self.assertIn("[fixture cleanup]", str(caught.exception))
        self.assertIn("TimeoutExpired", str(caught.exception))
        self.assertIsInstance(caught.exception.__cause__, subprocess.TimeoutExpired)
        shutil.rmtree(tmp)

    def test_a_clean_teardown_removes_the_dir_and_adds_nothing(self):
        with self.assertRaises(AssertionError) as caught:
            with _FixtureTmp() as tmp:
                (Path(tmp) / "f.txt").write_text("x")
                self.fail("ONLY-THE-ORIGINAL")
        self.assertEqual(str(caught.exception), "ONLY-THE-ORIGINAL")
        self.assertFalse(Path(tmp).exists())

    def test_no_fixture_in_this_file_uses_the_raw_temporary_directory(self):
        import ast

        raw = [
            node.lineno
            for node in ast.walk(ast.parse(Path(__file__).read_text(encoding="utf-8")))
            if isinstance(node, ast.Attribute) and node.attr == "TemporaryDirectory"
        ]
        self.assertEqual(raw, [], "tempfile.TemporaryDirectory raises PermissionError from __exit__ and hides the guard output; use _FixtureTmp (lines %s)" % raw)

    @unittest.skipUnless(sys.platform == "win32", "an open file blocks directory removal on Windows only")
    def test_an_open_handle_at_teardown_keeps_the_guard_diagnostic(self):
        held = None
        tmp = None
        try:
            with self.assertRaises(AssertionError) as caught:
                with _FixtureTmp(deadline=1) as tmp:
                    held = open(Path(tmp) / "held-by-fixture.txt", "w")
                    self.fail("GUARD-OUTPUT-MARKER: OVERALL FAIL")
        finally:
            if held:
                held.close()
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
        message = str(caught.exception)
        self.assertIn("GUARD-OUTPUT-MARKER: OVERALL FAIL", message)
        self.assertIn("[fixture cleanup]", message)
        self.assertIn("held-by-fixture.txt", message)
        self.assertNotIsInstance(caught.exception, PermissionError)

    @unittest.skipUnless(sys.platform == "win32", "an open file blocks directory removal on Windows only")
    def test_a_handle_released_within_the_deadline_is_retried_not_reported(self):
        import threading

        held = {}
        with _FixtureTmp(deadline=10) as tmp:
            held["f"] = open(Path(tmp) / "late-release.txt", "w")
            timer = threading.Timer(1.0, held["f"].close)
            timer.start()
        timer.join(timeout=15)
        self.assertFalse(Path(tmp).exists())


if __name__ == "__main__":
    unittest.main()
