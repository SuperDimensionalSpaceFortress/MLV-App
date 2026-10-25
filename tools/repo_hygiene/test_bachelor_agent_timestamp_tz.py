"""AGENT-TIMESTAMP-PARSE-TZ-1 r1: the Bachelor agent (tools/profiling/bachelor/ultra-magnus-agent.ps1) reads a
marker's or meta.json's timestamp as UTC on any host.

PowerShell 7's ConvertFrom-Json returns an ISO "...Z" string as a [DateTime] of Kind Utc; `[string]` on it drops the Kind and
`Parse` then assumes the host's offset, so on the Central-time Bachelor host a time moved by 5 h (after a mid-lease agent restart
an adopted job's deadline moved with it). Windows PowerShell 5.1 returns the string, so the fix is right on both editions.

The behaviour is proven by tools/testing/test-ultra-magnus-agent-timestamp-tz.ps1 (the agent's real functions, lifted from its AST,
with their disk and process steps stubbed); the shift only shows when the host is off UTC, so the text pins below keep the shape
on a UTC runner too: no `[string]`-then-Parse of a marker field, and one helper at every site.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AGENT = ROOT / "tools" / "profiling" / "bachelor" / "ultra-magnus-agent.ps1"
TZ_TEST = ROOT / "tools" / "testing" / "test-ultra-magnus-agent-timestamp-tz.ps1"
PWSH = shutil.which("pwsh")


def _code(text: str) -> str:
    """Agent lines with full-line comments dropped (pins are about code, not prose)."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


class BachelorAgentTimestampTzPins(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.code = _code(AGENT.read_bytes().decode("ascii"))

    def test_no_string_cast_then_parse_of_a_marker_or_meta_timestamp(self) -> None:
        # The shape that moved a time by the host offset: Parse([string]$x) where $x came back from ConvertFrom-Json.
        offenders = re.findall(r"\[(?:DateTimeOffset|DateTime|datetime)\]::(?:Parse|ParseExact)\(\s*\[string\]", self.code)
        self.assertEqual(offenders, [], "a JSON timestamp is read by ConvertTo-UtcDateTime, never cast to [string] and parsed")

    def test_the_restart_and_adopt_path_uses_the_helper(self) -> None:
        body = self.code[self.code.index("function Invoke-AdoptStartedJob"):]
        body = body[:body.index("\n}\n")]
        self.assertIn("$startedUtc = ConvertTo-UtcDateTime $Marker.startedUtc", body)
        # An unreadable startedUtc still stops the adopt before it waits or kills (no deadline can be computed).
        self.assertRegex(body, r"if \(\$null -eq \$startedUtc\) \{ throw ")

    def test_queue_age_and_launched_child_use_the_helper(self) -> None:
        age = self.code[self.code.index("function Get-QueueAgeSeconds"):]
        age = age[:age.index("\n}\n")]
        self.assertIn("$submitUtc = ConvertTo-UtcDateTime $Metadata.submitUtc", age)
        child = self.code[self.code.index("function Find-LaunchedJobChild"):]
        child = child[:child.index("\n}\n")]
        self.assertIn("$launchingAt = ConvertTo-UtcDateTime $Marker.launchingUtc", child)
        self.assertIn('if ($null -eq $launchingAt) { return @{ state = "unknown"', child)

    def test_helper_reads_by_kind_and_parses_a_bare_string_as_utc(self) -> None:
        helper = self.code[self.code.index("function ConvertTo-UtcDateTime"):]
        helper = helper[:helper.index("\n}\n")]
        for piece in (
            "$Value -is [DateTimeOffset]",
            "$Value -is [DateTime]",
            "[DateTimeKind]::Local",
            "[DateTime]::SpecifyKind($Value, [DateTimeKind]::Utc)",
            "[DateTimeOffset]::TryParse($Value, [System.Globalization.CultureInfo]::InvariantCulture, [System.Globalization.DateTimeStyles]::AssumeUniversal",
        ):
            self.assertIn(piece, helper)
        self.assertNotIn("[string]$", helper, "the helper never casts a value through [string]")


@unittest.skipIf(PWSH is None, "pwsh is not on PATH")
@unittest.skipUnless(os.name == "nt", "runs the Windows agent's PowerShell functions")
class BachelorAgentTimestampTzBehaviour(unittest.TestCase):
    def test_every_shape_reads_as_utc_at_every_site(self) -> None:
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(TZ_TEST)],
                              capture_output=True, text=True, timeout=300)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["status"], "PASS")
        self.assertEqual(out["helper"]["kinds"], "ok")
        # The restart-and-adopt path: past its deadline a job is killed at once, whatever shape its startedUtc came back in.
        for shape in ("json-readback", "z-string", "plus0-string", "no-offset"):
            self.assertEqual(out["adopt"][f"expired-{shape}"], "adopted/killed=True", shape)
            self.assertEqual(out["adopt"][f"inside-{shape}"], "adopted/killed=False", shape)
        self.assertEqual(out["adopt"]["missing"], "threw/killed=False")
        self.assertEqual(out["findChild"]["bad-"], "unknown")
        self.assertEqual(out["queueAge"]["missing"], 400)


if __name__ == "__main__":
    unittest.main()
