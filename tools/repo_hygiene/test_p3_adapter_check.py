"""Offline test of the P3 validation script's adapter assertion (GPU-PREF-REGISTRY-STOP-AND-PRUNE-1 r1c).

``tools/profiling/run-ultramagnus-p3-validation.ps1`` used to pin the discrete GPU in the registry; it now asserts the
adapter the app actually got, from the GL_RENDERER the wrapper collects. ``-SpeedLeg`` turns output validation off, which
is the only source of those renderer lines, so a successful speed run has an empty list. This test extracts the real
``Get-AdapterCheckVerdict`` function out of the script (the same spliced-verbatim precedent as
test_playback_host_load_gate.py), runs it under pwsh with a fake renderer list, and pins that the script wires the
verdict into its failure list and its summary. No MLVApp is launched and nothing touches the registry.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "profiling" / "run-ultramagnus-p3-validation.ps1"
PWSH = shutil.which("pwsh")

SKIP_TAG = "skipped-speedleg-no-renderer-evidence"


def _extract_function(source: str, name: str) -> str:
    match = re.search(r"function " + re.escape(name) + r" \{.*?\n\}\r?\n", source, re.DOTALL)
    if not match:
        raise AssertionError(f"could not extract function {name} from {SCRIPT}")
    return match.group(0)


@unittest.skipIf(PWSH is None, "pwsh is not on PATH")
class AdapterCheckVerdict(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="p3adapter-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.function = _extract_function(SCRIPT.read_text(encoding="utf-8"), "Get-AdapterCheckVerdict")

    def verdict(self, adapters, *, speed, pattern="4090"):
        data = self.tmp / "in.json"
        data.write_text(json.dumps({"adapters": list(adapters)}), encoding="utf-8")
        probe = self.tmp / "probe.ps1"
        probe.write_text(
            self.function
            + "\n$in = Get-Content -LiteralPath '" + str(data).replace("'", "''") + "' -Raw | ConvertFrom-Json\n"
            + "$v = Get-AdapterCheckVerdict -AdapterUsed @($in.adapters) -RequiredGpuNamePattern '" + pattern + "' "
            + "-SpeedLeg $" + ("true" if speed else "false") + "\n"
            + "ConvertTo-Json -InputObject $v -Depth 3\n",
            encoding="utf-8",
        )
        proc = subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(probe)],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def test_a_successful_speed_leg_with_no_renderer_evidence_is_skipped_not_failed(self) -> None:
        verdict = self.verdict([], speed=True)
        self.assertFalse(verdict["failed"])
        self.assertEqual(verdict["adapterCheck"], SKIP_TAG)
        self.assertTrue(verdict["reason"], "the skip must carry a recorded reason")

    def test_a_non_speed_run_with_no_matching_adapter_fails_closed(self) -> None:
        for adapters in ([], ["Intel(R) UHD Graphics 770"]):
            verdict = self.verdict(adapters, speed=False)
            self.assertTrue(verdict["failed"], adapters)
            self.assertEqual(verdict["adapterCheck"], "mismatch")

    def test_a_speed_leg_that_reports_only_the_wrong_adapter_still_fails(self) -> None:
        verdict = self.verdict(["Intel(R) UHD Graphics 770"], speed=True)
        self.assertTrue(verdict["failed"])
        self.assertEqual(verdict["adapterCheck"], "mismatch")

    def test_a_matching_adapter_passes_in_both_modes(self) -> None:
        for speed in (True, False):
            verdict = self.verdict(["NVIDIA GeForce RTX 4090/PCIe/SSE2"], speed=speed)
            self.assertFalse(verdict["failed"])
            self.assertEqual(verdict["adapterCheck"], "matched")

    def test_blank_renderer_entries_count_as_no_evidence(self) -> None:
        self.assertEqual(self.verdict(["", "  "], speed=True)["adapterCheck"], SKIP_TAG)
        self.assertTrue(self.verdict(["", "  "], speed=False)["failed"])


class ScriptWiring(unittest.TestCase):
    """Reads the script as text; nothing here runs it."""

    def setUp(self) -> None:
        self.text = SCRIPT.read_text(encoding="utf-8")

    def test_the_verdict_feeds_the_failure_list_and_the_summary(self) -> None:
        self.assertIn("Get-AdapterCheckVerdict -AdapterUsed $adapterUsed", self.text)
        self.assertIn("-SpeedLeg ([bool]$SpeedLeg)", self.text)
        self.assertIn("if ($adapterVerdict.failed)", self.text)
        self.assertIn("Add-Failure $failures $adapterVerdict.reason", self.text)
        self.assertIn("adapterCheck = $adapterCheck", self.text)
        self.assertIn("adapterCheckReason = $adapterCheckReason", self.text)

    def test_the_script_parses_without_errors(self) -> None:
        if PWSH is None:
            self.skipTest("pwsh is not on PATH")
        probe = (
            "$e = $null; $t = $null; "
            f"[System.Management.Automation.Language.Parser]::ParseFile('{SCRIPT}', [ref]$t, [ref]$e) | Out-Null; "
            "if ($e -and $e.Count) { $e | ForEach-Object { $_.Message }; exit 1 }"
        )
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", probe], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
