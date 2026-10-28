"""UM-SSH-STDIN-BOUND-1: run the um-ssh.ps1 stdin/wall-clock guard under unittest discovery.

The guard itself is tools/profiling/test-um-ssh-stdin-bound.ps1 (a fake remote, no host contact).
um-ssh.ps1 is Windows-only (ssh.exe, taskkill), so this wrapper skips elsewhere.
"""
import math
import os
import shutil
import stat
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
        return "taskkill of pid %s did not return within %r s (fixture cleanup unconfirmed)" % (pid, WRAPPER_KILL_DEADLINE)
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
        notes.append("taskkill did not return within %r s" % deadline)
    try:
        proc.wait(timeout=deadline)
    except subprocess.TimeoutExpired:
        notes.append("pid %d still running %r s after the kill (reap unconfirmed, wrapper deadline hit)" % (proc.pid, deadline))
    return "; ".join(notes) if notes else "killed and reaped"


FIXTURE_TMP_REMOVE_DEADLINE = 10  # seconds _FixtureTmp keeps retrying the removal of its temp dir before it reports the dir as left behind


def _rmtree_clearing_readonly(path):
    """shutil.rmtree that clears a Windows read-only attribute and retries the entry, as TemporaryDirectory does."""
    def clear(func, target, _error):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=clear)
    else:
        shutil.rmtree(path, onerror=clear)


def _check_remove_deadline(deadline):
    """Sol B1: a nan or infinite deadline never expires and a negative one is meaningless, so refuse them by name.

    H2: only int and float are accepted (a bool is an int by inheritance but never a duration); a Decimal used to pass
    here and then raise TypeError at monotonic() + deadline, so the type is refused up front instead.
    """
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        raise TypeError("deadline must be an int or float number of seconds, got %s %r" % (type(deadline).__name__, deadline))
    if not (math.isfinite(deadline) and deadline >= 0):
        raise ValueError("deadline must be a finite number of seconds >= 0, got %r" % (deadline,))


def _remove_tree_bounded(path, deadline=FIXTURE_TMP_REMOVE_DEADLINE):
    """UM-SSH-FIXTURE-DIAG-PRESERVE-1: remove path, retrying for up to deadline s (a dying child may still hold a file).

    Returns "" when the dir is gone, else a one-line note naming the last error. The deadline bounds the retries:
    one rmtree call cannot be interrupted, and a dir confirmed gone has nothing to report, so a removal that
    succeeds after the deadline is still "" (sol B1).
    """
    _check_remove_deadline(deadline)
    end = time.monotonic() + deadline
    while True:
        try:
            _rmtree_clearing_readonly(path)
            last = None
        except OSError as exc:
            last = exc
        # Adversary A1: rmtree returning is not proof (its error callback can retry only the first lstat and then
        # return without traversing), so the verdict is always a fresh stat. os.path.exists is not used because it
        # reads any stat error (PermissionError) as gone; only FileNotFoundError means gone.
        try:
            os.stat(path)
        except FileNotFoundError:
            return ""
        except OSError as exc:
            last = exc  # Sol B3: the stat error is the newest word; never claim "still present" when stat failed.
        else:
            last = last or OSError("shutil.rmtree returned but the directory is still present")
        remaining = end - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.25, remaining))
        # Sol r2: the sleep can cross expiry, and no removal attempt may start once the budget is spent.
        if time.monotonic() >= end:
            # %r, not %d: a fractional deadline must be reported as configured (sol r2 B1).
            return "temp dir %s was not removed within %r s: %s: %s" % (path, deadline, type(last).__name__, last)


class _FixtureTmp:
    """Replacement for tempfile.TemporaryDirectory in this file's fixtures.

    TemporaryDirectory.__exit__ raises PermissionError when a Windows file is still open, and that REPLACES the
    failure in flight (the guard output, the stop/cleanup notes, the real assertion). Here the removal retries are
    bounded (no wall-clock cap: one rmtree call cannot be interrupted), and a removal that fails is APPENDED to the failure in flight; with no failure in flight it is raised itself,
    so a leaked handle is never swallowed. Assertions that read the guard output belong INSIDE the with block.
    """

    def __init__(self, deadline=FIXTURE_TMP_REMOVE_DEADLINE, remove=_remove_tree_bounded):
        _check_remove_deadline(deadline)
        self._deadline = deadline
        self._remove = remove
        self.path = None

    def __enter__(self):
        self.path = tempfile.mkdtemp(prefix="um-ssh-fixture-")
        return self.path

    def __exit__(self, exc_type, exc, tb):
        # Sol B2: a remover that raises must not replace the failure in flight; its exception becomes the note.
        try:
            note = self._remove(self.path, self._deadline)
        except Exception as removal_error:
            note = "temp dir %s removal raised %s: %s" % (self.path, type(removal_error).__name__, removal_error)
        if not note:
            return False
        line = "[fixture cleanup] " + note
        if exc is None:
            raise AssertionError(line)
        # H1: Ctrl-C and sys.exit are not test failures; they pass through unchanged, the note rides along.
        if not isinstance(exc, Exception):
            if hasattr(exc, "add_note"):
                exc.add_note(line)
            return False
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
        return "temp dir %s was not removed within %r s: PermissionError: simulated open handle" % (path, deadline)

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

    def test_a_read_only_file_with_no_open_handle_is_still_removed(self):
        """Adversary A3: TemporaryDirectory clears the read-only attribute before it retries; plain rmtree does not."""
        with _FixtureTmp(deadline=2) as tmp:
            target = Path(tmp) / "readonly.txt"
            target.write_text("x")
            os.chmod(target, stat.S_IREAD)
        self.assertFalse(Path(tmp).exists())

    def test_an_rmtree_that_returns_with_the_dir_still_there_is_not_success(self):
        """Adversary A1: a rmtree that returns without removing (error callback retried only the first lstat)."""
        from unittest import mock

        path = tempfile.mkdtemp(prefix="um-ssh-fixture-")
        try:
            with mock.patch.object(shutil, "rmtree", lambda target, **kwargs: None):
                note = _remove_tree_bounded(path, deadline=0)
            still_there = Path(path).is_dir()
        finally:
            shutil.rmtree(path, ignore_errors=True)
        self.assertTrue(still_there)
        self.assertIn("was not removed", note)
        self.assertIn("still present", note)

    def test_a_dir_that_cannot_be_stat_ed_is_not_reported_as_gone(self):
        """Adversary A2: os.path.exists turns a PermissionError into False; only FileNotFoundError means gone."""
        path = tempfile.mkdtemp(prefix="um-ssh-fixture-")
        real_stat = os.stat

        def denied(target, *args, **kwargs):
            if os.fspath(target) == path:
                raise PermissionError(13, "Access is denied", path)
            return real_stat(target, *args, **kwargs)

        def refuse(target, *args, **kwargs):
            raise PermissionError(13, "Access is denied", target)

        try:
            from unittest import mock

            # os.path.exists reports False for ANY stat error; the stub models that, so only an explicit os.stat can tell.
            with mock.patch.object(shutil, "rmtree", refuse), mock.patch.object(os, "stat", denied), \
                    mock.patch.object(os.path, "exists", lambda target: False):
                note = _remove_tree_bounded(path, deadline=0)
        finally:
            shutil.rmtree(path, ignore_errors=True)
        self.assertIn("was not removed", note)
        self.assertIn("PermissionError", note)

    # ---- sol r1 B1-B3 ---------------------------------------------------------------------------------

    def test_a_deadline_that_is_not_a_finite_non_negative_number_is_refused_up_front(self):
        """Sol B1: nan and inf never expire, so a locked dir would retry forever. Refuse them, in the helper and the fixture."""
        for bad in (float("nan"), float("inf"), float("-inf"), -1):
            with self.subTest(deadline=bad):
                with self.assertRaises(ValueError) as helper:
                    _remove_tree_bounded("no-such-dir", deadline=bad)
                self.assertIn("deadline", str(helper.exception))
                with self.assertRaises(ValueError) as fixture:
                    _FixtureTmp(deadline=bad)
                self.assertIn("deadline", str(fixture.exception))

    def test_a_removal_that_succeeds_after_the_deadline_is_still_a_success(self):
        """Sol B1: the deadline bounds RETRIES. One rmtree call cannot be interrupted, and a dir that is confirmed
        gone has nothing left to report, so a slow success is a success (failing it would turn a clean teardown into noise)."""
        from unittest import mock

        now = [0.0]

        def slow_rmtree(target, **kwargs):
            now[0] = 20.0

        def gone(target, *args, **kwargs):
            raise FileNotFoundError(2, "gone", target)

        with mock.patch.object(time, "monotonic", lambda: now[0]), mock.patch.object(shutil, "rmtree", slow_rmtree), \
                mock.patch.object(os, "stat", gone):
            self.assertEqual(_remove_tree_bounded("slow-dir", deadline=10), "")

    def test_a_remover_that_raises_is_annotated_onto_the_failure_in_flight(self):
        """Sol B2: a raising remover must not replace the guard output; its exception becomes the cleanup note."""
        def raising_remove(path, deadline):
            raise PermissionError("REMOVE-RAISED")

        with self.assertRaises(AssertionError) as caught:
            with _FixtureTmp(remove=raising_remove) as tmp:
                raise AssertionError("GUARD-OUTPUT-MARKER")
        message = str(caught.exception)
        self.assertIn("GUARD-OUTPUT-MARKER", message)
        self.assertIn("[fixture cleanup]", message)
        self.assertIn("REMOVE-RAISED", message)
        self.assertNotIsInstance(caught.exception, PermissionError)
        shutil.rmtree(tmp)

    def test_a_remover_that_raises_after_a_passing_body_fails_with_the_cleanup_error(self):
        def raising_remove(path, deadline):
            raise PermissionError("REMOVE-RAISED")

        with self.assertRaises(AssertionError) as caught:
            with _FixtureTmp(remove=raising_remove) as tmp:
                pass
        self.assertIn("[fixture cleanup]", str(caught.exception))
        self.assertIn("REMOVE-RAISED", str(caught.exception))
        shutil.rmtree(tmp)

    def test_a_remover_that_raises_is_chained_with_a_non_assertion_failure(self):
        def raising_remove(path, deadline):
            raise PermissionError("REMOVE-RAISED")

        with self.assertRaises(AssertionError) as caught:
            with _FixtureTmp(remove=raising_remove) as tmp:
                raise subprocess.TimeoutExpired("guard", 5)
        self.assertIn("REMOVE-RAISED", str(caught.exception))
        self.assertIn("TimeoutExpired", str(caught.exception))
        self.assertIsInstance(caught.exception.__cause__, subprocess.TimeoutExpired)
        shutil.rmtree(tmp)

    def test_a_stat_error_after_rmtree_returns_is_the_reported_last_error(self):
        """Sol B3: when the final stat is what failed, the note names that error and never claims the dir is present."""
        from unittest import mock

        path = tempfile.mkdtemp(prefix="um-ssh-fixture-")

        def denied(target, *args, **kwargs):
            raise PermissionError(13, "STAT-ACCESS-DENIED", target)

        try:
            with mock.patch.object(shutil, "rmtree", lambda target, **kwargs: None), mock.patch.object(os, "stat", denied):
                note = _remove_tree_bounded(path, deadline=0)
        finally:
            shutil.rmtree(path, ignore_errors=True)
        self.assertIn("was not removed", note)
        self.assertIn("STAT-ACCESS-DENIED", note)
        self.assertNotIn("still present", note)

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

    # ---- sol r2 B1 + r3 fold-ins ---------------------------------------------------------------------

    @staticmethod
    def _fake_clock_persistent_lock(deadline):
        """Run _remove_tree_bounded against a lock that never lets go, on a fake clock where sleep advances time.

        Returns (note, start times of each removal attempt).
        """
        from unittest import mock

        now = [0.0]
        starts = []

        def locked(target, **kwargs):
            starts.append(now[0])
            raise PermissionError(13, "LOCKED", target)

        def present(target, *args, **kwargs):
            return object()

        def sleep(seconds):
            now[0] += seconds

        with mock.patch.object(time, "monotonic", lambda: now[0]), mock.patch.object(time, "sleep", sleep), \
                mock.patch.object(shutil, "rmtree", locked), mock.patch.object(os, "stat", present):
            return _remove_tree_bounded("locked-dir", deadline=deadline), starts

    def test_a_fractional_deadline_is_reported_as_configured_not_truncated(self):
        """Sol r2 B1: 0.1 and 0.9 printed 'within 0 s' and 1.5 printed 'within 1 s'; the budget must round-trip."""
        for deadline, shown in ((0.1, "0.1"), (0.9, "0.9"), (1.5, "1.5"), (10, "10"), (0, "0")):
            with self.subTest(deadline=deadline):
                note, _starts = self._fake_clock_persistent_lock(deadline)
                self.assertIn("within %s s:" % shown, note)

    def test_a_kill_and_reap_note_reports_a_fractional_deadline_as_configured(self):
        """Class sweep of sol r2 B1: the wrapper kill notes also rendered their deadline with %d."""
        def stuck(pid, deadline):
            raise subprocess.TimeoutExpired("taskkill", deadline)

        class Proc:
            pid = 4242

            @staticmethod
            def wait(timeout):
                raise subprocess.TimeoutExpired("proc", timeout)

        note = _stop_process_tree(Proc, deadline=0.5, taskkill=stuck)
        self.assertIn("taskkill did not return within 0.5 s", note)
        self.assertIn("pid 4242 still running 0.5 s after the kill", note)

    def test_no_removal_attempt_starts_after_the_deadline_has_expired(self):
        """Sol r2 blocker 1: the sleep after a failed attempt used to cross expiry and a further removal still ran."""
        note, starts = self._fake_clock_persistent_lock(0.1)
        self.assertEqual(starts, [0.0])
        self.assertIn("was not removed within 0.1 s", note)

    def test_a_removal_that_fails_late_does_not_start_another_after_expiry(self):
        from unittest import mock

        now = [0.0]
        starts = []

        def late_failure(target, **kwargs):
            starts.append(now[0])
            if len(starts) > 3:
                raise RuntimeError("retried again after expiry: starts %s" % starts)  # also stops a regressed loop from spinning
            now[0] = 9.9
            raise PermissionError(13, "LOCKED", target)

        def sleep(seconds):
            now[0] += seconds

        with mock.patch.object(time, "monotonic", lambda: now[0]), mock.patch.object(time, "sleep", sleep), \
                mock.patch.object(shutil, "rmtree", late_failure), mock.patch.object(os, "stat", lambda target, *a, **k: object()):
            note = _remove_tree_bounded("locked-dir", deadline=10)
        self.assertEqual(starts, [0.0])
        self.assertIn("was not removed within 10 s", note)

    def test_an_interruption_in_flight_passes_through_a_failed_cleanup_unchanged(self):
        """H1 FIXTURE-TMP-BASEEXC-PASSTHROUGH-1: Ctrl-C and sys.exit must stay themselves; the cleanup note rides along."""
        for raised, kind in ((KeyboardInterrupt(), KeyboardInterrupt), (SystemExit(7), SystemExit)):
            with self.subTest(raised=type(raised).__name__):
                with self.assertRaises(kind) as caught:
                    with _FixtureTmp(remove=self._failing_remove) as tmp:
                        raise raised
                self.assertIs(caught.exception, raised)
                if kind is SystemExit:
                    self.assertEqual(caught.exception.code, 7)
                if hasattr(raised, "add_note"):
                    self.assertTrue(any("[fixture cleanup]" in note for note in raised.__notes__), getattr(raised, "__notes__", None))
                shutil.rmtree(tmp)

    def test_an_interruption_with_a_clean_cleanup_is_untouched(self):
        with self.assertRaises(KeyboardInterrupt):
            with _FixtureTmp():
                raise KeyboardInterrupt()

    def test_a_deadline_that_is_not_an_int_or_float_is_refused_up_front_by_type(self):
        """H2 FIXTURE-TMP-DEADLINE-TYPE: a Decimal passed the validator and then died at monotonic() + deadline."""
        import decimal

        for bad, type_name in ((decimal.Decimal("0.1"), "Decimal"), (True, "bool"), ("5", "str"), (None, "NoneType")):
            with self.subTest(deadline=bad):
                with self.assertRaises((TypeError, ValueError)) as helper:
                    _remove_tree_bounded("no-such-dir", deadline=bad)
                self.assertIn(type_name, str(helper.exception))
                with self.assertRaises((TypeError, ValueError)) as fixture:
                    _FixtureTmp(deadline=bad)
                self.assertIn(type_name, str(fixture.exception))


if __name__ == "__main__":
    unittest.main()
