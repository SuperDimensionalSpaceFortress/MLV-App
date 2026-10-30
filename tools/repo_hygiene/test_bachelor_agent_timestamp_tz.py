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
        # The same loss spelled with an interpolated string: [datetime]"$x" and Parse("$($x)"). The behaviour test below is the
        # backstop for any other spelling (it swaps four of them into the helper and requires the site cases to fail).
        spelled = re.findall(r"\[(?:datetime|DateTime)\]\s*\"|\[(?:DateTimeOffset|DateTime|datetime)\]::(?:Parse|ParseExact)\(\s*\"", self.code)
        self.assertEqual(spelled, [], "no timestamp is read through an interpolated string either")

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
        # AGENT-LAUNCH-MARKER-MINVALUE-GUARD-1: the 2 s slack is taken off only after the MinValue guard.
        self.assertLess(child.index("[DateTime]::MinValue.AddSeconds(2)"), child.index("$launchingAt.AddSeconds(-2)"))

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


POWERSHELL_51 = shutil.which("powershell.exe")
FORCED_OFFSETS = {"utc-5": -300, "utc+5:30": 330}
SPELLING_MUTANTS = ("dto-parse-string-cast", "datetime-parse-string-cast", "datetime-cast-interpolated", "dto-parse-interpolated")


@unittest.skipUnless(os.name == "nt", "runs the Windows agent's PowerShell functions")
class BachelorAgentTimestampTzBehaviour(unittest.TestCase):
    def _run_matrix(self, exe: str) -> None:
        # The .ps1 forces the zone of its own child processes (UTC-5 and UTC+5:30) and fails if the effective offset is zero,
        # so this holds on a UTC runner too.
        proc = subprocess.run([exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(TZ_TEST)],
                              capture_output=True, text=True, timeout=600)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["status"], "PASS")
        self.assertEqual(sorted(out["offsets"]), sorted(FORCED_OFFSETS))
        for name, minutes in FORCED_OFFSETS.items():
            one = out["offsets"][name]
            self.assertEqual(one["forcedOffsetMinutes"], minutes)
            self.assertEqual(one["effectiveOffsetMinutes"], minutes, "the forced zone is the one in effect, not the host's")
            self.assertEqual(one["helper"]["kinds"], "ok")
            # The restart-and-adopt path: past its deadline a job is killed at once, whatever shape its startedUtc came back in.
            for shape in ("json-readback", "z-string", "plus0-string", "no-offset"):
                self.assertEqual(one["adopt"][f"expired-{shape}"], "adopted/killed=True", f"{name} {shape}")
                self.assertEqual(one["adopt"][f"inside-{shape}"], "adopted/killed=False", f"{name} {shape}")
            self.assertEqual(one["adopt"]["missing"], "threw/killed=False")
            self.assertEqual(one["findChild"]["bad-"], "unknown")
            # AGENT-LAUNCH-MARKER-MINVALUE-GUARD-1: a time at DateTime.MinValue or MaxValue never throws out of these sites (a
            # throw there unwinds the agent's whole try and ends it), under the agent's 'Continue' preference and under 'Stop'.
            for shape in ("json-readback", "z-string", "plus0-string", "no-offset"):
                for pref in ("Continue", "Stop"):
                    for edge in ("minvalue+0s", "minvalue+1s"):
                        self.assertEqual(one["findChild"][f"{edge}-{shape}-{pref}"], "unknown", f"{name} {edge} {shape} {pref}")
                    # An age past an [int] is clamped: as old as it can be, or 0 for a future submitUtc.
                    self.assertEqual(one["queueAge"][f"minvalue-{shape}-{pref}"], 2147483647, f"{name} {shape} {pref}")
                    self.assertEqual(one["queueAge"][f"maxvalue-{shape}-{pref}"], 0, f"{name} {shape} {pref}")
                    self.assertEqual(one["adopt"][f"maxvalue-{shape}-{pref}"], "adopted/killed=True", f"{name} {shape} {pref}")
            # r1 adversary A1/A2: an age the cast could hold keeps its old answer, and an old file time never throws.
            for pref in ("Continue", "Stop"):
                for shape in ("json-readback", "z-string"):
                    self.assertEqual(one["queueAge"][f"a1-posmax-{shape}-{pref}"], 2147483647, f"{name} {shape} {pref}")
                    self.assertEqual(one["queueAge"][f"a1-negmin-{shape}-{pref}"], 0, f"{name} {shape} {pref}")
                    self.assertEqual(one["queueAge"][f"a1-old-file-{shape}-{pref}"], 0, f"{name} {shape} {pref}")
                self.assertEqual(one["queueAge"][f"a2-missing-old-file-{pref}"], 2147483647, f"{name} {pref}")
            # The fixture creates the job 400 s old and the case reads its age a moment later, so the age is 400 plus the
            # time the run took in between (a slow runner or an edition start-up adds seconds); the .ps1 itself allows 15 s.
            self.assertAlmostEqual(one["queueAge"]["missing"], 400, delta=15)
        # Each wrong spelling swapped into the agent's helper is failed by the site cases at both offsets.
        for key in (f"{spelling}@{name}" for spelling in SPELLING_MUTANTS for name in FORCED_OFFSETS):
            self.assertEqual(out["spellingMutants"][key], "caught", key)

    @unittest.skipIf(PWSH is None, "pwsh is not on PATH")
    def test_every_shape_reads_as_utc_at_every_site_pwsh7(self) -> None:
        self._run_matrix(PWSH)

    @unittest.skipIf(POWERSHELL_51 is None, "powershell.exe is not on PATH")
    def test_every_shape_reads_as_utc_at_every_site_windows_powershell_51(self) -> None:
        self._run_matrix(POWERSHELL_51)


if __name__ == "__main__":
    unittest.main()
