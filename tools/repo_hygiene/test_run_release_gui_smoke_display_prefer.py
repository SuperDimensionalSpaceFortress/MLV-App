"""Behavioural tests for UM-DISPLAY-SELECT-AND-LOG-1 round 1c's -DisplayPrefer forwarding in
run-release-gui-smoke.ps1.

WHY. opus design-review item 5: the app rejects an unknown CLI option outright
(QCommandLineParser::process()), so forwarding --display-prefer unconditionally to an older
"before" binary in an A/B would kill that leg rather than just skip the preference. The runner
feature-probes the target binary's own --help output before forwarding the flag.

This test EXTRACTS the actual forwarding span (text, not a reimplementation) from
run-release-gui-smoke.ps1 and executes it standalone against a stub $exe whose --help output is
controlled per test case, proving the probe's real behavior rather than merely pinning strings.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "tools" / "profiling" / "run-release-gui-smoke.ps1"

PWSH = shutil.which("pwsh")
requires_pwsh = unittest.skipIf(PWSH is None, "pwsh is not on PATH")


def _forwarding_span() -> str:
    text = RUNNER.read_text(encoding="utf-8").replace("\r\n", "\n")
    start_marker = "if (-not [string]::IsNullOrWhiteSpace($DisplayPrefer) and -not $LegacyGuiSmokeOptions) {"
    # PowerShell spells the boolean operator "-and", not "and" -- match the real source exactly.
    start_marker = start_marker.replace(" and ", " -and ")
    start = text.index(start_marker)
    end = text.index("\n}\n", start) + len("\n}")
    return text[start:end]


class _ProbeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="run-gui-smoke-display-prefer-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _write_stub_exe(self, help_text: str) -> Path:
        # A .ps1 standing in for $exe -- "& $exe --help" invokes it as a script, exactly as
        # "& $exe --help" would invoke a real .exe.
        stub = self.tmp / "stub.ps1"
        stub.write_text(
            "param([Parameter(ValueFromRemainingArguments=$true)]$Args)\n"
            f"Write-Output @'\n{help_text}\n'@\n",
            encoding="utf-8",
        )
        return stub

    def run_probe(self, *, exe: Path, display_prefer: str, legacy: bool) -> subprocess.CompletedProcess:
        script = self.tmp / "probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"$exe = '{exe}'\n"
            f"$DisplayPrefer = '{display_prefer}'\n"
            f"$LegacyGuiSmokeOptions = ${'true' if legacy else 'false'}\n"
            "$arguments = @('--gui-smoke-playback')\n"
            + _forwarding_span()
            + "\nWrite-Host \"ARGS=$($arguments -join '|')\"\n",
            encoding="utf-8",
        )
        return subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(script)],
            capture_output=True, text=True,
        )


@requires_pwsh
class DisplayPreferForwardingTests(_ProbeCase):
    def test_forwards_the_flag_when_the_binary_lists_it_in_help(self) -> None:
        exe = self._write_stub_exe(
            "Usage: MLVApp.exe [options]\n"
            "  --windowed          Place the window ...\n"
            "  --display-prefer <substring>  Tie-break among displays ...\n"
        )
        proc = self.run_probe(exe=exe, display_prefer="PA329C", legacy=False)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("--display-prefer=PA329C", proc.stdout)

    def test_never_forwards_to_a_legacy_binary_whose_help_does_not_list_it(self) -> None:
        # The exact failure this closes: an older "before" binary in an A/B does not register
        # --display-prefer, and forwarding it unconditionally would make the app reject the
        # whole command line.
        exe = self._write_stub_exe(
            "Usage: MLVApp.exe [options]\n"
            "  --windowed          Place the window ...\n"
        )
        proc = self.run_probe(exe=exe, display_prefer="PA329C", legacy=False)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("--display-prefer", proc.stdout)

    def test_never_forwards_under_legacy_gui_smoke_options_even_if_supported(self) -> None:
        exe = self._write_stub_exe("  --display-prefer <substring>  Tie-break ...\n")
        proc = self.run_probe(exe=exe, display_prefer="PA329C", legacy=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("--display-prefer", proc.stdout)

    def test_never_forwards_an_empty_preference_even_when_supported(self) -> None:
        exe = self._write_stub_exe("  --display-prefer <substring>  Tie-break ...\n")
        proc = self.run_probe(exe=exe, display_prefer="", legacy=False)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("--display-prefer", proc.stdout)

    def test_a_throwing_help_probe_is_treated_as_unsupported_not_an_error(self) -> None:
        # A stub that throws instead of printing help -- the probe's own try/catch must
        # swallow it and simply skip forwarding, never propagate.
        exe = self.tmp / "throws.ps1"
        exe.write_text(
            "param([Parameter(ValueFromRemainingArguments=$true)]$Args)\n"
            "throw [System.InvalidOperationException]::new('no --help support')\n",
            encoding="utf-8",
        )
        proc = self.run_probe(exe=exe, display_prefer="PA329C", legacy=False)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("--display-prefer", proc.stdout)


class RunnerParamDeclarationTests(unittest.TestCase):
    """Text-level pin: the -DisplayPrefer parameter itself is declared."""

    def test_display_prefer_param_is_declared(self) -> None:
        source = RUNNER.read_text(encoding="utf-8")
        self.assertIn('[string]$DisplayPrefer = "",', source)


if __name__ == "__main__":
    unittest.main()
