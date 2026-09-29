"""PLAYBACK-HFR-CONFORM-DEFAULT-1: behavioural tests for the settings-job generator.

tools/profiling/bachelor/playback-hfr-settings-job.ps1 emits a Bachelor job that records, sets or
restores the saved QSettings values the live conform legs depend on. These tests EXECUTE pwsh: the
generator runs for real, the emitted job is parsed by PowerShell's own parser, and the per-mode
behaviour (what it will write, what it refuses) is asserted on the emitted text. The registry itself
is not touched here -- the emitted job only ever runs on the measurement host.

Skipped cleanly when pwsh is absent and on non-Windows platforms (the job addresses HKCU).
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "tools" / "profiling" / "bachelor" / "playback-hfr-settings-job.ps1"
PWSH = shutil.which("pwsh")

requires_pwsh = unittest.skipIf(PWSH is None, "pwsh is not on PATH")


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-File", *args],
        capture_output=True,
        text=True,
        timeout=120,
    )


@requires_pwsh
class SettingsJobGeneratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out = Path(self._tmp.name)

    def _generate(self, mode: str) -> str:
        target = self.out / f"{mode}.job.ps1"
        proc = _run([str(GENERATOR), "-Mode", mode, "-OutFile", str(target)])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return target.read_text(encoding="utf-8")

    def test_every_mode_emits_a_parse_clean_job_with_the_mode_baked_in(self) -> None:
        for mode in ("record", "setFpsOverride", "restore"):
            with self.subTest(mode=mode):
                text = self._generate(mode)
                self.assertIn(f"$Mode = '{mode}'", text)
                self.assertNotIn("__MODE__", text)
                self.assertNotIn("__AGENT_ROOT__", text)
                script = self.out / f"{mode}.check.ps1"
                script.write_text(
                    "$errs = $null\n"
                    f"[void][System.Management.Automation.Language.Parser]::ParseFile('{self.out / (mode + '.job.ps1')}', [ref]$null, [ref]$errs)\n"
                    "exit @($errs).Count\n",
                    encoding="utf-8",
                )
                proc = _run([str(script)])
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_an_unknown_mode_is_refused_at_parameter_binding(self) -> None:
        target = self.out / "bad.job.ps1"
        proc = _run([str(GENERATOR), "-Mode", "wipe", "-OutFile", str(target)])
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(target.exists())

    def test_set_mode_saves_the_export_override_and_requires_a_snapshot(self) -> None:
        text = self._generate("setFpsOverride")
        self.assertIn("-Name 'fpsOverride' -Value 'true'", text)
        self.assertIn("-Name 'frameRate' -Value '25'", text)
        self.assertIn("no original snapshot: run -Mode record first", text)

    def test_record_mode_keeps_the_first_snapshot_and_reports_per_core_load(self) -> None:
        text = self._generate("record")
        self.assertIn("STATE_FILE_EXISTS", text)
        self.assertIn("(original snapshot kept)", text)
        self.assertIn("PERCORE", text)
        for name in ("fpsOverride", "frameRate", "ConformEnabled", "ConformTargetFps", "ConformThresholdFps"):
            self.assertIn(f"name = '{name}'", text)

    def test_restore_mode_puts_values_back_and_removes_absent_ones(self) -> None:
        text = self._generate("restore")
        self.assertIn("Remove-ItemProperty", text)
        self.assertIn("Remove-Item -LiteralPath $stateFile -Force", text)
        self.assertIn("no original snapshot to restore", text)


if __name__ == "__main__":
    unittest.main()
