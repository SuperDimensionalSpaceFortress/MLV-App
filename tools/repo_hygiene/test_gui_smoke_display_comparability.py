"""Behavioural tests for UM-DISPLAY-SELECT-AND-LOG-1 round 1c's display-comparability refusal
(sol pre-review BLOCKER 1).

WHY. run-release-gui-smoke.ps1 published playbackFps but no display identity, so
compare-release-gui-smoke-ab.ps1 could publish a PASS with an FPS delta while comparing a 4K
leg against a degraded-fallback leg (or two different physical outputs). run-release-gui-smoke.ps1
now parses the app's own gui_smoke.window_placement/display_screen lines into a `display` block on
every result.json; compare-release-gui-smoke-ab.ps1's Get-DisplayComparability refuses the whole
comparison (FAIL, fps deltas withheld) whenever the two legs are not known to be the same display
identity and mode.

These tests EXTRACT the real function/span text (never a reimplementation) and execute it.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "tools" / "profiling" / "run-release-gui-smoke.ps1"
COMPARE_SCRIPT = ROOT / "tools" / "profiling" / "compare-release-gui-smoke-ab.ps1"
SHARED = ROOT / "tools" / "profiling" / "gui-smoke-display-identity.ps1"

PWSH = shutil.which("pwsh")
requires_pwsh = unittest.skipIf(PWSH is None, "pwsh is not on PATH")


def _extract_function(path: Path, name: str) -> str:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
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


def _extract_display_block_span() -> str:
    text = RUNNER.read_text(encoding="utf-8").replace("\r\n", "\n")
    start = text.index(
        "# UM-DISPLAY-SELECT-AND-LOG-1 round 1c (sol pre-review BLOCKER 1): this leg's own display"
    )
    end = text.index("\n$result = [pscustomobject]@{", start)
    return text[start:end]


class _ProbeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="gui-smoke-display-comparability-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def run_snippet(self, body: str) -> subprocess.CompletedProcess:
        script = self.tmp / "probe.ps1"
        script.write_text("$ErrorActionPreference = 'Stop'\n" + body, encoding="utf-8")
        return subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(script)],
            capture_output=True, text=True,
        )


@requires_pwsh
class DisplayComparabilityTests(_ProbeCase):
    def setUp(self) -> None:
        super().setUp()
        self.get_display_comparability = _extract_function(COMPARE_SCRIPT, "Get-DisplayComparability")

    def _compare(self, before: str, after: str) -> subprocess.CompletedProcess:
        return self.run_snippet(
            self.get_display_comparability + "\n"
            f"$before = {before}\n"
            f"$after = {after}\n"
            "$r = Get-DisplayComparability -BeforeDisplay $before -AfterDisplay $after\n"
            "Write-Host \"STATUS=$($r.status) REASON=$($r.reasonCode)\"\n"
            "Write-Host \"DETAIL=$($r.detail)\"\n"
        )

    SAME_A = (
        "[pscustomobject]@{ presentationScreenName='X'; presentationManufacturer='ASUS'; "
        "presentationModel='PA329C'; presentationSerial='ASUS-1'; physicalWidth=3840; physicalHeight=2160; "
        "refreshHzRounded=60; dpr=1.0; windowMode='fullscreen'; previewWidth=3840; previewHeight=2160; "
        "verified=$true; identityUnknownReason=$null }"
    )

    def test_a_reused_windows_ordinal_on_a_different_physical_monitor_is_different(self) -> None:
        # sol PRE-REVIEW #2 BLOCKER c, exact repro: same \\.\DISPLAYn ordinal, size, refresh, DPR
        # and preview but ASUS/PA329C/ASUS-1 before vs Generic/DENON/DENON-1 after used to compare
        # 'same' (the comparator never looked at model/serial) and publish FPS deltas.
        # MUTATION CAUGHT: dropping manufacturer/model/serial from the identity comparison.
        other = (self.SAME_A.replace("presentationManufacturer='ASUS'", "presentationManufacturer='Generic'")
                 .replace("presentationModel='PA329C'", "presentationModel='DENON'")
                 .replace("presentationSerial='ASUS-1'", "presentationSerial='DENON-1'"))
        proc = self._compare(self.SAME_A, other)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STATUS=different REASON=DISPLAY_MISMATCH", proc.stdout)

    def test_a_serial_alone_differing_is_different(self) -> None:
        other = self.SAME_A.replace("presentationSerial='ASUS-1'", "presentationSerial='ASUS-2'")
        proc = self._compare(self.SAME_A, other)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STATUS=different REASON=DISPLAY_MISMATCH", proc.stdout)

    def test_a_block_from_a_runner_that_predates_model_and_serial_is_unknown_never_same(self) -> None:
        # MUTATION CAUGHT: treating an absent identity property as equal-to-absent.
        legacy = (self.SAME_A.replace("presentationManufacturer='ASUS'; ", "")
                  .replace("presentationModel='PA329C'; ", "")
                  .replace("presentationSerial='ASUS-1'; ", ""))
        proc = self._compare(legacy, legacy)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STATUS=unknown REASON=DISPLAY_IDENTITY_UNKNOWN", proc.stdout)

    def test_empty_identity_fields_in_a_block_are_unknown_never_same(self) -> None:
        # sol r1 BLOCKER, defence in depth: a block carrying EMPTY manufacturer/model/serial (a
        # hand-built or legacy-runner result.json that never went through the shared parser's
        # identity-unknown state) must not compare 'same' on ordinal+mode alone. MUTATION CAUGHT:
        # comparing the three fields without requiring them non-empty.
        for prop in ("presentationManufacturer", "presentationModel", "presentationSerial"):
            with self.subTest(empty=prop):
                empty = re.sub(rf"{prop}='[^']*'", f"{prop}=''", self.SAME_A)
                self.assertNotEqual(empty, self.SAME_A)
                proc = self._compare(empty, empty)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn("STATUS=unknown REASON=DISPLAY_IDENTITY_UNKNOWN", proc.stdout)
                self.assertIn(prop, proc.stdout.split("DETAIL=")[1])

    def test_two_identical_verified_legs_are_same(self) -> None:
        proc = self._compare(self.SAME_A, self.SAME_A)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STATUS=same REASON=", proc.stdout)

    def test_different_presentation_screen_names_is_different(self) -> None:
        other = self.SAME_A.replace("presentationScreenName='X'", "presentationScreenName='Y'")
        proc = self._compare(self.SAME_A, other)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STATUS=different REASON=DISPLAY_MISMATCH", proc.stdout)

    def test_a_4k_leg_vs_a_degraded_fallback_leg_is_different(self) -> None:
        degraded = self.SAME_A.replace("physicalWidth=3840; physicalHeight=2160", "physicalWidth=2560; physicalHeight=1440")
        proc = self._compare(self.SAME_A, degraded)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STATUS=different REASON=DISPLAY_MISMATCH", proc.stdout)

    def test_windowed_vs_fullscreen_is_different(self) -> None:
        windowed = self.SAME_A.replace("windowMode='fullscreen'", "windowMode='windowed'")
        proc = self._compare(self.SAME_A, windowed)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STATUS=different REASON=DISPLAY_MISMATCH", proc.stdout)

    def test_either_leg_unverified_is_unknown_never_same(self) -> None:
        unverified = self.SAME_A.replace("verified=$true", "verified=$false")
        proc = self._compare(self.SAME_A, unverified)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STATUS=unknown REASON=PLACEMENT_UNVERIFIED", proc.stdout)

    def test_either_leg_with_identity_unknown_reason_is_unknown(self) -> None:
        # A legacy "before" binary that predates presentation_screen=.
        legacy = "[pscustomobject]@{ presentationScreenName=$null; verified=$false; identityUnknownReason='legacy binary' }"
        proc = self._compare(self.SAME_A, legacy)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STATUS=unknown REASON=DISPLAY_IDENTITY_UNKNOWN", proc.stdout)

    def test_a_null_display_block_on_either_leg_is_unknown_never_same(self) -> None:
        proc = self._compare(self.SAME_A, "$null")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STATUS=unknown REASON=DISPLAY_IDENTITY_UNKNOWN", proc.stdout)

    def test_a_dpr_mismatch_alone_is_different(self) -> None:
        other = self.SAME_A.replace("dpr=1.0", "dpr=1.5")
        proc = self._compare(self.SAME_A, other)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STATUS=different REASON=DISPLAY_MISMATCH", proc.stdout)


@requires_pwsh
class EmptyEdidLegsAreNotComparableTests(_ProbeCase):
    """sol r1 BLOCKER, exact repro, end to end through the REAL shared parser and the REAL
    comparator: two DIFFERENT physical 4K displays that reused \\\\.\\DISPLAY1 between legs, both
    with verified fullscreen placement, identical preview, and EMPTY Qt manufacturer/model/serial
    (no EDID descriptors). The parser used to publish a 'known' block of empty strings and the
    comparator returned 'same' -- an FPS delta across different physical displays. Now the legs
    are identity-unknown and the comparison is REFUSED, and the refusal says why."""

    D1 = "\\\\.\\DISPLAY1"

    def _placement(self) -> str:
        return (
            f'gui_smoke.window_placement mode=fullscreen screen="{self.D1}" verified=1 window=0,0 3840x2160 '
            f'preview=3840x2160 target_screen="{self.D1}" presentation_screen="{self.D1}" '
            "presentation_physical=3840x2160"
        )

    def _screen(self, manufacturer: str, model: str, serial: str) -> str:
        return (
            f'gui_smoke.display_screen index=0 name="{self.D1}" manufacturer="{manufacturer}" '
            f'model="{model}" serial="{serial}" geometry=0,0 3840x2160 physical=3840x2160 dpr=1.00 '
            "refresh_hz=59.997 primary=1"
        )

    def _compare_logs(self, before_screen: str, after_screen: str) -> subprocess.CompletedProcess:
        def literal(lines: list[str]) -> str:
            return "@(" + ",".join("'" + ln.replace("'", "''") + "'" for ln in lines) + ")"

        return self.run_snippet(
            f". '{SHARED}'\n"
            + _extract_function(COMPARE_SCRIPT, "Get-DisplayComparability") + "\n"
            f"$before = Get-GuiSmokeDisplayIdentity -Selection (ConvertFrom-GuiSmokeDisplayLog -LogText ({literal([before_screen, self._placement()])} -join \"`n\"))\n"
            f"$after = Get-GuiSmokeDisplayIdentity -Selection (ConvertFrom-GuiSmokeDisplayLog -LogText ({literal([after_screen, self._placement()])} -join \"`n\"))\n"
            "$r = Get-DisplayComparability -BeforeDisplay $before -AfterDisplay $after\n"
            "Write-Host \"STATUS=$($r.status) REASON=$($r.reasonCode)\"\n"
            "Write-Host \"BEFORE_REASON=$($before.identityUnknownReason)\"\n"
        )

    def test_two_edid_less_displays_sharing_a_device_name_are_refused_never_same(self) -> None:
        proc = self._compare_logs(self._screen("", "", ""), self._screen("", "", ""))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STATUS=unknown REASON=DISPLAY_IDENTITY_UNKNOWN", proc.stdout)
        # ...and the refusal says why.
        self.assertRegex(proc.stdout, r"BEFORE_REASON=.*empty.*manufacturer.*model.*serial")

    def test_two_fully_populated_identical_legs_are_still_same(self) -> None:
        # The refusal must not swallow the good case.
        proc = self._compare_logs(self._screen("ASUS", "PA329C", "S-9"), self._screen("ASUS", "PA329C", "S-9"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STATUS=same REASON=", proc.stdout)

    def test_two_populated_legs_that_differ_in_model_are_different(self) -> None:
        proc = self._compare_logs(self._screen("ASUS", "PA329C", "S-9"), self._screen("DON", "DENON-AVR", "S-9"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STATUS=different REASON=DISPLAY_MISMATCH", proc.stdout)


@requires_pwsh
class RunnerDisplayBlockExtractionTests(_ProbeCase):
    """Executes the ACTUAL runner span (extracted verbatim) that turns $recentLines into the
    `display` block published on result.json."""

    def setUp(self) -> None:
        super().setUp()
        # The runner dot-sources the shared parser as a closure sibling; the extracted span needs
        # the same functions in scope.
        self.span = f". '{SHARED}'\n" + _extract_display_block_span()

    def _probe(self, recent_lines_literal: str) -> subprocess.CompletedProcess:
        return self.run_snippet(
            f"$recentLines = {recent_lines_literal}\n"
            + self.span
            + "\nWrite-Host \"NAME=$($displayBlock.presentationScreenName) "
            "PW=$($displayBlock.physicalWidth) PH=$($displayBlock.physicalHeight) "
            "REFRESH=$($displayBlock.refreshHzRounded) DPR=$($displayBlock.dpr) "
            "MODE=$($displayBlock.windowMode) VERIFIED=$($displayBlock.verified) "
            "REASON=$($displayBlock.identityUnknownReason)\"\n"
        )

    def test_a_verified_fullscreen_line_with_matching_screen_line_produces_a_known_display_block(self) -> None:
        lines = (
            "@("
            "'gui_smoke.display_screen index=0 name=\"\\\\.\\DISPLAY1\" manufacturer=\"ASUS\" "
            "model=\"PA329C\" serial=\"S-9\" geometry=0,0 3840x2160 physical=3840x2160 dpr=1.00 "
            "refresh_hz=59.997 primary=0',"
            "'gui_smoke.window_placement mode=fullscreen screen=\"\\\\.\\DISPLAY1\" verified=1 "
            "window=0,0 3840x2160 preview=3840x2160 target_screen=\"\\\\.\\DISPLAY1\" "
            "presentation_screen=\"\\\\.\\DISPLAY1\" presentation_physical=3840x2160'"
            ")"
        )
        proc = self._probe(lines)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(
            r"NAME=\\.\DISPLAY1 PW=3840 PH=2160 REFRESH=60 DPR=1 MODE=fullscreen VERIFIED=True REASON=",
            proc.stdout,
        )

    def test_the_display_block_carries_manufacturer_model_and_serial_in_any_field_order(self) -> None:
        # sol PRE-REVIEW #2 BLOCKER c: identity is name+model+serial, not just the ordinal.
        # MUTATION CAUGHT: dropping the three fields from the published block; and (order
        # tolerance) the serial/model keys are read by key, not by position.
        lines = (
            "@("
            "'gui_smoke.display_screen index=0 name=\"\\\\.\\DISPLAY1\" serial=\"S-9\" model=\"PA329C\" "
            "manufacturer=\"ASUS\" geometry=0,0 3840x2160 physical=3840x2160 dpr=1.00 "
            "refresh_hz=59.997 primary=0',"
            "'gui_smoke.window_placement mode=fullscreen screen=\"\\\\.\\DISPLAY1\" verified=1 "
            "window=0,0 3840x2160 preview=3840x2160 target_screen=\"\\\\.\\DISPLAY1\" "
            "presentation_screen=\"\\\\.\\DISPLAY1\" presentation_physical=3840x2160'"
            ")"
        )
        proc = self.run_snippet(
            f"$recentLines = {lines}\n" + self.span +
            "\nWrite-Host \"IDENT=$($displayBlock.presentationManufacturer)|$($displayBlock.presentationModel)"
            "|$($displayBlock.presentationSerial) REASON=$($displayBlock.identityUnknownReason)\"\n")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("IDENT=ASUS|PA329C|S-9 REASON=", proc.stdout)

    def test_a_placement_whose_presentation_screen_has_no_display_screen_line_is_identity_unknown(self) -> None:
        # sol hardening: the runner used to build a KNOWN block with null refresh/DPR/identity,
        # and two such blocks compared 'same'. MUTATION CAUGHT: restoring the null-valued block.
        lines = (
            "@('gui_smoke.window_placement mode=fullscreen screen=\"\\\\.\\DISPLAY1\" verified=1 "
            "window=0,0 3840x2160 preview=3840x2160 target_screen=\"\\\\.\\DISPLAY1\" "
            "presentation_screen=\"\\\\.\\DISPLAY1\" presentation_physical=3840x2160')"
        )
        proc = self._probe(lines)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("REASON=presentation screen has no matching gui_smoke.display_screen line", proc.stdout)

    def test_a_rejected_move_reports_the_presentation_screen_not_the_target(self) -> None:
        lines = (
            "@("
            "'gui_smoke.display_screen index=0 name=\"\\\\.\\DISPLAY2\" manufacturer=\"GEN\" "
            "model=\"PNP\" serial=\"G-1\" geometry=0,0 2560x1440 physical=2560x1440 dpr=1.00 "
            "refresh_hz=60.000 primary=1',"
            "'gui_smoke.window_placement mode=fullscreen screen=\"\\\\.\\DISPLAY1\" verified=0 "
            "window=0,0 2560x1440 preview=2560x1440 target_screen=\"\\\\.\\DISPLAY1\" "
            "presentation_screen=\"\\\\.\\DISPLAY2\" presentation_physical=2560x1440'"
            ")"
        )
        proc = self._probe(lines)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(r"NAME=\\.\DISPLAY2", proc.stdout)
        self.assertIn("VERIFIED=False", proc.stdout)

    def test_no_window_placement_line_is_identity_unknown(self) -> None:
        proc = self._probe("@()")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("VERIFIED=False", proc.stdout)
        self.assertIn("REASON=no gui_smoke.window_placement line found", proc.stdout)

    def test_a_legacy_placement_line_with_no_presentation_field_is_identity_unknown(self) -> None:
        lines = (
            "@('gui_smoke.window_placement mode=fullscreen screen=\"\\\\.\\DISPLAY1\" verified=1 "
            "window=0,0 3840x2160 preview=3840x2160')"
        )
        proc = self._probe(lines)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("REASON=legacy binary", proc.stdout)


if __name__ == "__main__":
    unittest.main()
