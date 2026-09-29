"""Behavioural tests for UM-DISPLAY-SELECT-AND-LOG-1's -DisplayPrefer forwarding in
run-release-gui-smoke.ps1 (round 1c feature probe; round 4 hang fix).

WHY. opus design-review item 5 (round 1c): the app rejects an unknown CLI option outright
(QCommandLineParser::process()), so forwarding --display-prefer unconditionally to an older
"before" binary in an A/B would kill that leg rather than just skip the preference. The runner
feature-probes the target binary's own help output before forwarding the flag.

ROUND 4, fable BLOCKER 1. The round-1c probe ran a BARE `MLVApp.exe --help`. main() treats any argv
without --batch/--trim-mlv/--profile-playback/--gui-smoke-playback as normal GUI mode
(MainWindow.show(); a.exec()), so the probe never exited, the runner never reached the smoke launch,
and the attribution job (which launches the runner synchronously, PresentMon already capturing)
hung on EVERY Ultra-Magnus leg. The probe now (1) passes `--gui-smoke-playback --help`, the only
path main() answers with exit 0 and no window, and (2) runs under a hard timeout that kills the
process tree by pid and returns a typed UNKNOWN, where UNKNOWN means the preference is not used and
the reason is logged.

These tests EXTRACT the real function and forwarding span (text, never a reimplementation) from
run-release-gui-smoke.ps1 and execute them against STUB EXECUTABLES (.cmd files, launched by the
same Process.Start the real app is) whose behaviour is controlled per test case -- including a stub
that never exits, and a stub that models the real app by hanging unless it is given exactly the
probe arguments main() answers.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "tools" / "profiling" / "run-release-gui-smoke.ps1"

PWSH = shutil.which("pwsh")
requires_pwsh = unittest.skipIf(PWSH is None, "pwsh is not on PATH")

SPAN_START = "# UM-DISPLAY-SELECT-AND-LOG-1 round 4 (fable BLOCKER 1): --display-prefer feature probe"

# Wall-clock ceiling for a probe test whose probe timeout is 3 s: kill + process start + pwsh
# startup must fit well inside it. A regression to an unbounded probe never returns at all, which
# subprocess.run's own timeout turns into a red test instead of a hung suite.
HANG_TEST_BOUND_SECONDS = 60


def _runner_text() -> str:
    return RUNNER.read_text(encoding="utf-8").replace("\r\n", "\n")


def _extract_function(text: str, name: str) -> str:
    start = text.index(f"function {name} {{")
    depth = 0
    i = text.index("{", start)
    j = i
    while True:
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    return text[start:j + 1]


def _forwarding_span() -> str:
    text = _runner_text()
    start = text.index(SPAN_START)
    end = text.index("\n}\n", start) + len("\n}")
    return text[start:end]


class _ProbeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="run-gui-smoke-display-prefer-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.marker = "HANGMARK" + uuid.uuid4().hex

    def _write_cmd(self, name: str, body: str) -> Path:
        stub = self.tmp / name
        stub.write_text("@echo off\r\n" + body.replace("\n", "\r\n") + "\r\n", encoding="ascii")
        return stub

    def _help_stub(self, help_lines: list[str], *, exit_code: int = 0) -> Path:
        body = "\n".join(f"echo {line}" for line in help_lines) + f"\nexit /b {exit_code}"
        return self._write_cmd("help_stub.cmd", body)

    def _hang_stub(self, name: str = "hang_stub.cmd") -> Path:
        # Never exits on its own (a 600 s sleep) and leaves a CHILD process behind (cmd -> pwsh),
        # so an implementation that kills only the direct process still leaks -- and a probe with
        # no timeout never returns. pwsh rather than ping.exe: this host runs several lanes at once
        # and a ping.exe sweep by another session killed the stub mid-test (observed 2026-09-29).
        return self._write_cmd(name, self._sleep_command())

    def _sleep_command(self) -> str:
        # The marker makes THIS test's sleeper identifiable by command line, so a test can prove
        # the whole process tree (not just the cmd parent) is gone.
        return (f'"{PWSH}" -NoLogo -NoProfile -NonInteractive -Command '
                f'"Start-Sleep -Seconds 600 # {self.marker}"')

    def _run_hang_probe(self, exe: Path) -> subprocess.CompletedProcess:
        """Runs the bounded probe against a never-exiting stub. The machine hosts several lanes
        and an outside process sweep has killed a stub before the probe's own timeout (the probe
        then correctly reports UNKNOWN with an exit code instead of a timeout); rerun, up to 3
        attempts, only when the reason shows it was NOT the timeout path -- every attempt still
        has to return inside the wall-clock bound, and the last attempt's result is what is
        asserted."""
        proc = None
        for _ in range(3):
            proc = self.run_span(exe=exe, display_prefer="PA329C", legacy=False, timeout_sec=3)
            if proc.returncode == 0 and "timed out after 3 s" in proc.stdout:
                break
        return proc

    def _gui_model_stub(self, help_lines: list[str]) -> Path:
        # Models the real app's main(): ONLY `--gui-smoke-playback --help` is answered (help text,
        # exit 0, no window); any other argv is "normal GUI mode" and never exits.
        echoes = "\n".join(f"  echo {line}" for line in help_lines)
        body = (
            'if "%~1"=="--gui-smoke-playback" if "%~2"=="--help" (\n'
            f"{echoes}\n"
            "  exit /b 0\n"
            ")\n"
            + self._sleep_command()
        )
        return self._write_cmd("gui_model_stub.cmd", body)

    def run_span(self, *, exe: Path, display_prefer: str, legacy: bool,
                 timeout_sec: int = 20) -> subprocess.CompletedProcess:
        text = _runner_text()
        script = self.tmp / "probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"$exe = '{exe}'\n"
            f"$DisplayPrefer = '{display_prefer}'\n"
            f"$LegacyGuiSmokeOptions = ${'true' if legacy else 'false'}\n"
            f"$displayPreferProbeTimeoutSec = {timeout_sec}\n"
            "$validationWarnings = @()\n"
            "$arguments = @('--gui-smoke-playback')\n"
            + _extract_function(text, "Test-GuiSmokeDisplayPreferSupport") + "\n"
            + _forwarding_span()
            + "\nWrite-Host \"ARGS=$($arguments -join '|')\"\n"
            "Write-Host \"PROBE=$($displayPreferRecord.probe) FORWARDED=$($displayPreferRecord.forwarded)\"\n"
            "Write-Host \"REASON=$($displayPreferRecord.reason)\"\n"
            "Write-Host \"WARNINGS=$(@($validationWarnings).Count)\"\n",
            encoding="utf-8",
        )
        return subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(script)],
            capture_output=True, text=True, timeout=HANG_TEST_BOUND_SECONDS,
        )


@requires_pwsh
class DisplayPreferForwardingTests(_ProbeCase):
    HELP = [
        "Usage: MLVApp.exe [options]",
        "  --windowed          Place the window ...",
        "  --display-prefer ^<substring^>  Tie-break among displays ...",
    ]

    def test_forwards_the_flag_when_the_binary_lists_it_in_help(self) -> None:
        proc = self.run_span(exe=self._help_stub(self.HELP), display_prefer="PA329C", legacy=False)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("--display-prefer=PA329C", proc.stdout)
        self.assertIn("PROBE=supported FORWARDED=True", proc.stdout)

    def test_never_forwards_to_a_legacy_binary_whose_help_does_not_list_it(self) -> None:
        # The exact failure round 1c closed: an older "before" binary in an A/B does not register
        # --display-prefer, and forwarding it unconditionally would make the app reject the whole
        # command line. The skip is RECORDED, never silent.
        exe = self._help_stub(["Usage: MLVApp.exe [options]", "  --windowed          Place the window ..."])
        proc = self.run_span(exe=exe, display_prefer="PA329C", legacy=False)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("--display-prefer", proc.stdout.split("ARGS=")[1].splitlines()[0])
        self.assertIn("PROBE=unsupported FORWARDED=False", proc.stdout)
        self.assertIn("WARNINGS=1", proc.stdout)

    def test_never_forwards_under_legacy_gui_smoke_options_even_if_supported(self) -> None:
        proc = self.run_span(exe=self._help_stub(self.HELP), display_prefer="PA329C", legacy=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("--display-prefer", proc.stdout.split("ARGS=")[1].splitlines()[0])
        self.assertIn("PROBE=not_requested", proc.stdout)

    def test_never_forwards_an_empty_preference_even_when_supported(self) -> None:
        proc = self.run_span(exe=self._help_stub(self.HELP), display_prefer="", legacy=False)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("--display-prefer", proc.stdout.split("ARGS=")[1].splitlines()[0])
        self.assertIn("PROBE=not_requested", proc.stdout)

    def test_an_exe_that_cannot_start_is_unknown_and_never_an_error(self) -> None:
        proc = self.run_span(exe=self.tmp / "does-not-exist.exe", display_prefer="PA329C", legacy=False)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("--display-prefer", proc.stdout.split("ARGS=")[1].splitlines()[0])
        self.assertIn("PROBE=unknown FORWARDED=False", proc.stdout)
        self.assertIn("WARNINGS=1", proc.stdout)

    def test_a_nonzero_exit_is_unknown_even_if_the_output_mentions_the_option(self) -> None:
        # An error banner that happens to print the option name is not a capability statement.
        exe = self._help_stub(["  --display-prefer ^<substring^>  Tie-break ..."], exit_code=2)
        proc = self.run_span(exe=exe, display_prefer="PA329C", legacy=False)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("--display-prefer", proc.stdout.split("ARGS=")[1].splitlines()[0])
        self.assertIn("PROBE=unknown FORWARDED=False", proc.stdout)
        self.assertIn("exit code 2", proc.stdout)


@requires_pwsh
class DisplayPreferProbeCannotHangTests(_ProbeCase):
    """fable BLOCKER 1, RED-FIRST: a stub exe that never exits."""

    def test_a_probe_that_never_exits_returns_within_the_bound_as_unknown(self) -> None:
        # MUTATION CAUGHT: removing the WaitForExit timeout (or going back to `& $exe ... | Out-String`).
        # Without the bound this test does not fail by assertion -- subprocess.run times out.
        started = time.monotonic()
        proc = self._run_hang_probe(self._hang_stub())
        elapsed = time.monotonic() - started
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertLess(elapsed, 3 * HANG_TEST_BOUND_SECONDS)
        self.assertIn("PROBE=unknown FORWARDED=False", proc.stdout)
        self.assertNotIn("--display-prefer", proc.stdout.split("ARGS=")[1].splitlines()[0])
        # The reason is logged, and it names the timeout and the killed pid.
        reason = next(line for line in proc.stdout.splitlines() if line.startswith("REASON="))
        self.assertRegex(reason, r"timed out after 3 s")
        self.assertRegex(reason, r"pid \d+")
        self.assertIn("WARNINGS=1", proc.stdout)

    def test_the_killed_probe_leaves_no_process_behind(self) -> None:
        # Kill by pid, tree included: the hang stub's pwsh sleeper (a CHILD of the cmd the probe
        # started) must die with its cmd parent. MUTATION CAUGHT: killing only the direct process.
        exe = self._hang_stub()
        proc = self._run_hang_probe(exe)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        reason = next(line for line in proc.stdout.splitlines() if line.startswith("REASON="))
        pid = int(re.search(r"pid (\d+)", reason).group(1))
        survivors = subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
             f"$parent = Get-Process -Id {pid} -ErrorAction SilentlyContinue\n"
             "$kids = @(Get-CimInstance Win32_Process | Where-Object "
             f"{{ $_.ProcessId -ne $PID -and $_.CommandLine -like '*{self.marker}*' }})\n"
             "\"PARENT=$(if ($parent) { 'ALIVE' } else { 'GONE' }) SLEEPERS=$($kids.Count)\""],
            capture_output=True, text=True, timeout=HANG_TEST_BOUND_SECONDS,
        )
        self.assertIn("PARENT=GONE SLEEPERS=0", survivors.stdout, survivors.stdout + survivors.stderr)

    def test_the_probe_uses_the_arguments_main_answers_not_a_bare_help(self) -> None:
        # The real app's main() only answers `--gui-smoke-playback --help`; a bare `--help` is
        # normal GUI mode and never exits. This stub models that: with the bare probe it hangs
        # (and the bound turns it into UNKNOWN); with the right probe it answers and is SUPPORTED.
        # MUTATION CAUGHT: reverting the probe to a bare `--help`.
        exe = self._gui_model_stub(DisplayPreferForwardingTests.HELP)
        proc = self.run_span(exe=exe, display_prefer="PA329C", legacy=False, timeout_sec=10)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PROBE=supported FORWARDED=True", proc.stdout)
        self.assertIn("--display-prefer=PA329C", proc.stdout)


class RunnerProbeSourcePinTests(unittest.TestCase):
    """Text-level pins that complement the behavioural tests above."""

    def test_display_prefer_param_is_declared(self) -> None:
        source = RUNNER.read_text(encoding="utf-8")
        self.assertIn('[string]$DisplayPrefer = "",', source)

    def test_no_bare_help_probe_remains_in_the_runner(self) -> None:
        # The hang's exact line. A bare `& $exe --help` starts the GUI.
        self.assertNotIn("& $exe --help", _runner_text())

    def test_the_probe_timeout_default_is_bounded(self) -> None:
        text = _runner_text()
        match = re.search(r"^\$displayPreferProbeTimeoutSec = (\d+)\s*$", text, re.MULTILINE)
        self.assertIsNotNone(match, "the runner must define $displayPreferProbeTimeoutSec at script level")
        self.assertLessEqual(int(match.group(1)), 30)
        self.assertIn("-TimeoutSec $displayPreferProbeTimeoutSec", _forwarding_span())

    def test_the_probe_outcome_is_published_in_result_json(self) -> None:
        # fable DISPLAY-PREFER-PROBE-RECORDED-1: a skipped preference must be visible downstream.
        text = _runner_text()
        self.assertIn("displayPrefer = $displayPreferRecord", text)


if __name__ == "__main__":
    unittest.main()
