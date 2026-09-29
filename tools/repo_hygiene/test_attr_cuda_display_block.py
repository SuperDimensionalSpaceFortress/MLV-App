"""Behavioural tests for UM-DISPLAY-SELECT-AND-LOG-1 item 2's `display` block.

WHY. A UM number must never be presented without knowing the identity and mode of the display it
was benchmarked on (see .claude-state/project-memory/
um-display-topology-lg-tv-denon-fallback-20260926.md): Ultra-Magnus's primary is an LG TV through
a Denon AVR that falls back to a DEGRADED resolution when the TV is off. playback-attr-3-cuda-job.ps1
captures the Windows-API display inventory independently of Qt (Get-AttrCudaWindowsDisplayInventory),
parses the app's own gui_smoke.display_screen/display_target/window_placement lines
(Get-AttrCudaGuiSmokeDisplaySelection), classifies the venue (Get-AttrCudaMeasurementVenue) and
computes a three-state degraded verdict (Get-AttrCudaDisplayDegradedState) -- all in
AttrCudaArtifacts.psm1, embedded verbatim into the job like every other shared function (see
test_playback_attr_3_cuda_behaviour.py's EmbeddedFunctionContractTests).

These tests EXECUTE the real module functions (Import-Module, not a reimplementation).
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "tools" / "profiling" / "bachelor" / "AttrCudaArtifacts.psm1"
JOB_SCRIPT = ROOT / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-job.ps1"
SHARED_IDENTITY = ROOT / "tools" / "profiling" / "gui-smoke-display-identity.ps1"

PWSH = shutil.which("pwsh")
requires_pwsh = unittest.skipIf(PWSH is None, "pwsh is not on PATH")

SAMPLE_LOG_4K = (
    'gui_smoke.display_screen index=0 name="\\\\.\\DISPLAY1" manufacturer="LG" model="TV" '
    'serial="" geometry=0,0 3840x2160 physical=3840x2160 dpr=1.00 refresh_hz=60.000 primary=1\n'
    'gui_smoke.display_screen index=1 name="\\\\.\\DISPLAY2" manufacturer="Dell" model="U2720Q" '
    'serial="ABC123" geometry=3840,0 2560x1440 physical=2560x1440 dpr=1.00 refresh_hz=59.951 primary=0\n'
    'gui_smoke.display_target screen="\\\\.\\DISPLAY1" reason=max_physical_pixels candidates=2 fallback=0\n'
    'gui_smoke.window_placement mode=fullscreen screen="\\\\.\\DISPLAY1" verified=1 window=0,0 '
    '3840x2160 preview=3840x2160\n'
)

SAMPLE_LOG_DEGRADED_FALLBACK = (
    # TV off: the AVR's headless fallback is the only attached display, and it is what gets chosen
    # -- fallback=1, one candidate, a resolution below the 4K expectation.
    'gui_smoke.display_screen index=0 name="\\\\.\\DISPLAY1" manufacturer="Denon" model="AVR" '
    'serial="" geometry=0,0 2560x1440 physical=2560x1440 dpr=1.00 refresh_hz=60.000 primary=1\n'
    'gui_smoke.display_target screen="\\\\.\\DISPLAY1" reason=max_physical_pixels candidates=1 fallback=1\n'
    'gui_smoke.window_placement mode=windowed screen="\\\\.\\DISPLAY1" verified=1 window=0,0 '
    '2560x1440 preview=2560x1440\n'
)


class _ProbeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="attr3-display-block-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def run_snippet(self, body: str) -> subprocess.CompletedProcess:
        script = self.tmp / "probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{MODULE}' -Force\n" + body,
            encoding="utf-8",
        )
        return subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(script)],
            capture_output=True, text=True,
        )


@requires_pwsh
class MeasurementVenueTests(_ProbeCase):
    """Get-AttrCudaMeasurementVenue: only an ultra-magnus-named host carries an expectation."""

    def test_ultra_magnus_hostname_classifies_as_ultra_magnus(self) -> None:
        proc = self.run_snippet(
            "Write-Host (Get-AttrCudaMeasurementVenue -ComputerName 'ULTRA-MAGNUS')\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("ultra-magnus", proc.stdout)

    def test_hyphen_insensitive_and_case_insensitive_match(self) -> None:
        proc = self.run_snippet(
            "Write-Host (Get-AttrCudaMeasurementVenue -ComputerName 'ultramagnus')\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("ultra-magnus", proc.stdout)

    def test_bachelor_hostname_classifies_as_bachelor(self) -> None:
        proc = self.run_snippet(
            "Write-Host (Get-AttrCudaMeasurementVenue -ComputerName 'BACHELOR01')\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("bachelor", proc.stdout)

    def test_an_unrecognized_hostname_defaults_to_bachelor_never_asserts_an_expectation(self) -> None:
        proc = self.run_snippet(
            "Write-Host (Get-AttrCudaMeasurementVenue -ComputerName 'SOME-OTHER-BOX')\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("bachelor", proc.stdout)


@requires_pwsh
class DisplayDegradedStateTests(_ProbeCase):
    """Get-AttrCudaDisplayDegradedState: the three states pinned in the UM-DISPLAY-SELECT-AND-LOG-1
    round 1 brief -- a below-expectation target, an at-expectation target, and unreadable."""

    def test_below_expectation_target_is_degraded(self) -> None:
        proc = self.run_snippet(
            "$r = Get-AttrCudaDisplayDegradedState -TargetWidth 2560 -TargetHeight 1440 "
            "-ExpectedWidth 3840 -ExpectedHeight 2160\nWrite-Host \"R=$r\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("R=True", proc.stdout)

    def test_at_expectation_target_is_not_degraded(self) -> None:
        proc = self.run_snippet(
            "$r = Get-AttrCudaDisplayDegradedState -TargetWidth 3840 -TargetHeight 2160 "
            "-ExpectedWidth 3840 -ExpectedHeight 2160\nWrite-Host \"R=$r\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("R=False", proc.stdout)

    def test_an_unreadable_target_is_the_third_unknown_state_not_false(self) -> None:
        proc = self.run_snippet(
            "$r = Get-AttrCudaDisplayDegradedState -TargetWidth $null -TargetHeight $null "
            "-ExpectedWidth 3840 -ExpectedHeight 2160\nWrite-Host \"R=$r\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("R=unknown", proc.stdout)

    def test_no_expectation_for_this_venue_is_unknown_never_folded_into_not_degraded(self) -> None:
        # Bachelor (or any host with no pinned expected resolution): record only, assert nothing.
        proc = self.run_snippet(
            "$r = Get-AttrCudaDisplayDegradedState -TargetWidth 1920 -TargetHeight 1080 "
            "-ExpectedWidth $null -ExpectedHeight $null\nWrite-Host \"R=$r\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("R=unknown", proc.stdout)
        self.assertNotIn("R=False", proc.stdout)

    # UM-DISPLAY-SELECT-AND-LOG-1 round 1c (sol BLOCKER 2): an unreadable INDEPENDENT Windows
    # inventory must make the verdict unknown even when the app's own (Qt) target reads exactly
    # at-expectation -- never "not degraded" purely on the app's own say-so.

    def test_windows_inventory_not_collected_is_unknown_even_at_expectation(self) -> None:
        proc = self.run_snippet(
            "$r = Get-AttrCudaDisplayDegradedState -TargetWidth 3840 -TargetHeight 2160 "
            "-ExpectedWidth 3840 -ExpectedHeight 2160 -WindowsCollected $false\nWrite-Host \"R=$r\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("R=unknown", proc.stdout)
        self.assertNotIn("R=False", proc.stdout)

    def test_windows_inventory_with_no_readable_mode_is_unknown_even_at_expectation(self) -> None:
        # collected=true with zero devices, or every device unreadable, is the sibling gap opus's
        # design review flagged: same as not collected at all.
        proc = self.run_snippet(
            "$r = Get-AttrCudaDisplayDegradedState -TargetWidth 3840 -TargetHeight 2160 "
            "-ExpectedWidth 3840 -ExpectedHeight 2160 -WindowsCollected $true "
            "-WindowsAnyModeCollected $false\nWrite-Host \"R=$r\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("R=unknown", proc.stdout)
        self.assertNotIn("R=False", proc.stdout)

    def test_windows_inventory_collected_with_a_readable_mode_still_compares_normally(self) -> None:
        proc = self.run_snippet(
            "$r = Get-AttrCudaDisplayDegradedState -TargetWidth 2560 -TargetHeight 1440 "
            "-ExpectedWidth 3840 -ExpectedHeight 2160 -WindowsCollected $true "
            "-WindowsAnyModeCollected $true\nWrite-Host \"R=$r\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("R=True", proc.stdout)


@requires_pwsh
class GuiSmokeDisplaySelectionParsingTests(_ProbeCase):
    """Get-AttrCudaGuiSmokeDisplaySelection: parses the app's own log lines, third-state on
    absence, never guesses a value that was not actually logged."""

    def _write_log(self, text: str) -> Path:
        path = self.tmp / "smoke-run.log"
        path.write_text(text, encoding="utf-8")
        return path

    def test_full_happy_path_log_parses_every_field(self) -> None:
        log_path = self._write_log(SAMPLE_LOG_4K)
        proc = self.run_snippet(
            f"$log = [IO.File]::ReadAllText('{log_path}')\n"
            "$sel = Get-AttrCudaGuiSmokeDisplaySelection -LogText $log\n"
            "Write-Host \"SCREENS=$($sel.screensCollected) COUNT=$($sel.screens.Count)\"\n"
            "Write-Host \"TARGET_NAME=$($sel.target.name) FALLBACK=$($sel.target.fallback) "
            "REASON=$($sel.target.reason) CANDIDATES=$($sel.target.candidates)\"\n"
            "Write-Host \"MODE=$($sel.placement.mode) PW=$($sel.placement.previewWidth) "
            "PH=$($sel.placement.previewHeight)\"\n"
            "$target = $sel.screens | Where-Object { $_.name -eq $sel.target.name }\n"
            "Write-Host \"TARGET_PHYSICAL=$($target.physicalWidth)x$($target.physicalHeight)"
            "@$($target.refreshHz)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("SCREENS=True COUNT=2", proc.stdout)
        self.assertIn(r"TARGET_NAME=\\.\DISPLAY1 FALLBACK=False REASON=max_physical_pixels CANDIDATES=2",
                      proc.stdout)
        self.assertIn("MODE=fullscreen PW=3840 PH=2160", proc.stdout)
        self.assertIn("TARGET_PHYSICAL=3840x2160@60", proc.stdout)

    def test_degraded_fallback_log_parses_the_fallback_flag_and_single_candidate(self) -> None:
        log_path = self._write_log(SAMPLE_LOG_DEGRADED_FALLBACK)
        proc = self.run_snippet(
            f"$log = [IO.File]::ReadAllText('{log_path}')\n"
            "$sel = Get-AttrCudaGuiSmokeDisplaySelection -LogText $log\n"
            "Write-Host \"FALLBACK=$($sel.target.fallback) CANDIDATES=$($sel.target.candidates) "
            "MODE=$($sel.placement.mode)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("FALLBACK=True CANDIDATES=1 MODE=windowed", proc.stdout)

    def test_an_empty_log_is_the_third_unknown_state_with_a_reason_for_all_three_lines(self) -> None:
        proc = self.run_snippet(
            "$sel = Get-AttrCudaGuiSmokeDisplaySelection -LogText ''\n"
            "Write-Host \"SCREENS=$($sel.screensCollected) TARGET=$($null -eq $sel.target) "
            "PLACEMENT=$($null -eq $sel.placement)\"\n"
            "Write-Host \"SE=$($sel.screensError) TE=$($sel.targetError) PE=$($sel.placementError)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("SCREENS=False TARGET=True PLACEMENT=True", proc.stdout)
        self.assertIn("SE=no gui_smoke.display_screen lines found", proc.stdout)
        self.assertIn("TE=no gui_smoke.display_target line found", proc.stdout)
        self.assertIn("PE=no gui_smoke.window_placement line found", proc.stdout)

    def test_display_target_parses_the_appended_preferred_fields(self) -> None:
        log_path = self._write_log(
            'gui_smoke.display_target screen="\\\\.\\DISPLAY2" reason=max_physical_pixels_tie_preferred '
            'candidates=2 fallback=1 preferred="PA329C" preferred_matched=matched\n'
        )
        proc = self.run_snippet(
            f"$log = [IO.File]::ReadAllText('{log_path}')\n"
            "$sel = Get-AttrCudaGuiSmokeDisplaySelection -LogText $log\n"
            "Write-Host \"PREFERRED=$($sel.target.preferred) MATCHED=$($sel.target.preferredMatched)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PREFERRED=PA329C MATCHED=matched", proc.stdout)

    def test_display_target_without_the_appended_preferred_fields_reads_them_as_null(self) -> None:
        log_path = self._write_log(SAMPLE_LOG_4K)
        proc = self.run_snippet(
            f"$log = [IO.File]::ReadAllText('{log_path}')\n"
            "$sel = Get-AttrCudaGuiSmokeDisplaySelection -LogText $log\n"
            "Write-Host \"PREFERRED_NULL=$($null -eq $sel.target.preferred) "
            "MATCHED_NULL=$($null -eq $sel.target.preferredMatched)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PREFERRED_NULL=True MATCHED_NULL=True", proc.stdout)

    def test_window_placement_parses_the_appended_target_and_presentation_fields(self) -> None:
        # UM-DISPLAY-SELECT-AND-LOG-1 round 1c (sol BLOCKER 3): the appended fields report the
        # ACTUAL presentation screen separately from the intended target.
        log_path = self._write_log(
            'gui_smoke.window_placement mode=fullscreen screen="\\\\.\\DISPLAY1" verified=0 '
            'window=0,0 3840x2160 preview=3840x2160 target_screen="\\\\.\\DISPLAY1" '
            'presentation_screen="\\\\.\\DISPLAY2" presentation_physical=2560x1440\n'
        )
        proc = self.run_snippet(
            f"$log = [IO.File]::ReadAllText('{log_path}')\n"
            "$sel = Get-AttrCudaGuiSmokeDisplaySelection -LogText $log\n"
            "Write-Host \"TARGET_SCREEN=$($sel.placement.targetScreenName) "
            "PRESENTATION_SCREEN=$($sel.placement.presentationScreenName) "
            "PW=$($sel.placement.presentationPhysicalWidth) PH=$($sel.placement.presentationPhysicalHeight)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(
            r"TARGET_SCREEN=\\.\DISPLAY1 PRESENTATION_SCREEN=\\.\DISPLAY2 PW=2560 PH=1440",
            proc.stdout,
        )

    def test_window_placement_without_the_appended_fields_reads_them_as_null_never_guessed(self) -> None:
        # A legacy log line (predates round 1c) must still parse on its pre-existing fields.
        log_path = self._write_log(SAMPLE_LOG_4K)
        proc = self.run_snippet(
            f"$log = [IO.File]::ReadAllText('{log_path}')\n"
            "$sel = Get-AttrCudaGuiSmokeDisplaySelection -LogText $log\n"
            "Write-Host \"TARGET_SCREEN_NULL=$($null -eq $sel.placement.targetScreenName) "
            "PRESENTATION_SCREEN_NULL=$($null -eq $sel.placement.presentationScreenName) "
            "MODE=$($sel.placement.mode) VERIFIED=$($sel.placement.verified)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(
            "TARGET_SCREEN_NULL=True PRESENTATION_SCREEN_NULL=True MODE=fullscreen VERIFIED=True",
            proc.stdout,
        )

    def test_a_log_with_screens_but_no_target_line_reports_target_unknown_not_the_first_screen(self) -> None:
        # A build that logs the inventory but never reaches the target-selection line (e.g. it
        # threw first) must never be silently treated as "chose the first screen".
        log_path = self._write_log(
            'gui_smoke.display_screen index=0 name="\\\\.\\DISPLAY1" manufacturer="" model="" '
            'serial="" geometry=0,0 1920x1080 physical=1920x1080 dpr=1.00 refresh_hz=60.000 primary=1\n'
        )
        proc = self.run_snippet(
            f"$log = [IO.File]::ReadAllText('{log_path}')\n"
            "$sel = Get-AttrCudaGuiSmokeDisplaySelection -LogText $log\n"
            "Write-Host \"SCREENS=$($sel.screensCollected) TARGET_NULL=$($null -eq $sel.target)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("SCREENS=True TARGET_NULL=True", proc.stdout)


@requires_pwsh
class WindowsDisplayInventoryRealExecutionTests(_ProbeCase):
    """Get-AttrCudaWindowsDisplayInventory: a real-execution smoke test, mirroring
    ProcessCpuSnapshotTests' pattern in test_attr_cuda_cpu_quiescence.py. Hub-smoke-tested on
    Ultra-Magnus hardware separately (collected=True, \\\\.\\DISPLAY1 3840x2400); this only proves
    the function runs cleanly and returns the documented shape on whatever host runs the test."""

    def test_real_inventory_collects_and_has_the_documented_shape(self) -> None:
        proc = self.run_snippet(
            "$inv = Get-AttrCudaWindowsDisplayInventory\n"
            "Write-Host \"COLLECTED=$($inv.collected) HAS_DEVICES_PROP=$($null -ne $inv.PSObject.Properties['devices'])\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("HAS_DEVICES_PROP=True", proc.stdout)
        # collected is a real boolean either way (third state on failure is $false, never $null).
        self.assertRegex(proc.stdout, r"COLLECTED=(True|False)")


@requires_pwsh
class TemplateDisplayBlockExecutionTests(_ProbeCase):
    """Executes the job template's own Build-AttrCudaDisplayBlock and
    Get-AttrCudaDisplayResultTail functions (extracted verbatim from the generator's source, not
    reimplemented) end to end, proving the RESULT-line field FORMAT itself --
    "DISPLAY=<name> RES=<w>x<h>@<hz> DEGRADED=<0|1|unknown> PREVIEW=<w>x<h>" -- not just that the
    call sites exist."""

    def setUp(self) -> None:
        super().setUp()
        text = JOB_SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")
        build_at = text.index("function Build-AttrCudaDisplayBlock")
        tail_at = text.index("function Get-AttrCudaDisplayResultTail")
        tail_end = text.index("\n\nforeach ($item in @(", tail_at)
        self.functions = text[build_at:tail_end]

    def run_probe(self, body: str) -> subprocess.CompletedProcess:
        # The job embeds the shared display-identity functions (Build-AttrCudaDisplayBlock calls
        # Get-GuiSmokeDisplayIdentity); a probe of the extracted function needs them in scope.
        return self.run_snippet(f". '{SHARED_IDENTITY}'\n" + self.functions + "\n" + body)

    def test_a_block_built_before_the_smoke_log_exists_is_marked_pre_smoke(self) -> None:
        # UM-DISPLAY-SELECT-AND-LOG-1 round 3 (sol hardening): SMOKE_RUN_FAILED (and every other
        # refusal that fires before the smoke log exists) publishes the pre-smoke block; it must
        # say so, so nothing in it can be read as measured on the presented screen.
        # MUTATION CAUGHT: dropping the phase marker.
        proc = self.run_probe(
            "$inv = [pscustomobject]@{ collected = $false; devices = @(); error = 'x' }\n"
            "$pre = Build-AttrCudaDisplayBlock -WindowsInventory $inv -Venue 'bachelor' -AppSelection $null\n"
            "$sel = [pscustomobject]@{ screensCollected = $false; screens = @(); screensError = 'e'; target = $null; "
            "targetError = 'e'; placement = $null; placementError = 'e' }\n"
            "$post = Build-AttrCudaDisplayBlock -WindowsInventory $inv -Venue 'bachelor' -AppSelection $sel\n"
            "Write-Host \"PRE=$($pre.phase) POST=$($post.phase)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PRE=pre-smoke POST=post-smoke", proc.stdout)

    def test_smoke_run_failed_publishes_the_pre_smoke_marked_block(self) -> None:
        text = JOB_SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")
        template = text[text.index("$template = @'"):]
        at = template.index("result='SMOKE_RUN_FAILED'")
        self.assertIn("display=$displayBlock", template[at:at + 900])
        self.assertIn("phase = $(if ($appKnown) { 'post-smoke' } else { 'pre-smoke' })", template)

    def test_full_happy_path_produces_the_documented_result_tail(self) -> None:
        proc = self.run_probe(
            "$inv = [pscustomobject]@{ collected = $true; devices = @([pscustomobject]@{ "
            "deviceName='\\\\.\\DISPLAY1'; modeCollected=$true; width=3840; height=2160 }); error = $null }\n"
            "$appSel = [pscustomobject]@{\n"
            "    screensCollected = $true\n"
            "    screens = @([ordered]@{ name='\\\\.\\DISPLAY1'; physicalWidth=3840; physicalHeight=2160; refreshHz=60.0 })\n"
            "    screensError = $null\n"
            "    target = [ordered]@{ name='\\\\.\\DISPLAY1'; reason='max_physical_pixels'; candidates=2; fallback=$false }\n"
            "    targetError = $null\n"
            "    placement = [ordered]@{ mode='fullscreen'; previewWidth=3840; previewHeight=2160 }\n"
            "    placementError = $null\n"
            "}\n"
            "$block = Build-AttrCudaDisplayBlock -WindowsInventory $inv -Venue 'ultra-magnus' "
            "-ExpectedWidth 3840 -ExpectedHeight 2160 -AppSelection $appSel\n"
            "Write-Host (Get-AttrCudaDisplayResultTail $block)\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("DISPLAY=\\\\.\\DISPLAY1 RES=3840x2160@60 DEGRADED=0 PREVIEW=3840x2160", proc.stdout)
        # UM-DISPLAY-SELECT-AND-LOG-1 round 1c: recorded, never gated -- 'unknown' when the
        # app never logged a preferred_matched= field (this fixture's target has none).
        self.assertIn("PREFERRED=unknown", proc.stdout)

    def test_result_tail_reports_the_preferred_match_status_when_the_app_logged_it(self) -> None:
        proc = self.run_probe(
            "$inv = [pscustomobject]@{ collected = $true; devices = @([pscustomobject]@{ "
            "deviceName='\\\\.\\DISPLAY1'; modeCollected=$true; width=3840; height=2160 }); error = $null }\n"
            "$appSel = [pscustomobject]@{\n"
            "    screensCollected = $true\n"
            "    screens = @([ordered]@{ name='\\\\.\\DISPLAY1'; physicalWidth=3840; physicalHeight=2160; refreshHz=60.0 })\n"
            "    screensError = $null\n"
            "    target = [ordered]@{ name='\\\\.\\DISPLAY1'; reason='max_physical_pixels_tie_preferred'; candidates=2; "
            "fallback=$true; preferred='PA329C'; preferredMatched='matched' }\n"
            "    targetError = $null\n"
            "    placement = [ordered]@{ mode='fullscreen'; previewWidth=3840; previewHeight=2160 }\n"
            "    placementError = $null\n"
            "}\n"
            "$block = Build-AttrCudaDisplayBlock -WindowsInventory $inv -Venue 'ultra-magnus' "
            "-ExpectedWidth 3840 -ExpectedHeight 2160 -AppSelection $appSel\n"
            "Write-Host (Get-AttrCudaDisplayResultTail $block)\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PREFERRED=matched", proc.stdout)

    def test_degraded_fallback_produces_degraded_one(self) -> None:
        proc = self.run_probe(
            "$inv = [pscustomobject]@{ collected = $true; devices = @([pscustomobject]@{ "
            "deviceName='\\\\.\\DISPLAY1'; modeCollected=$true; width=2560; height=1440 }); error = $null }\n"
            "$appSel = [pscustomobject]@{\n"
            "    screensCollected = $true\n"
            "    screens = @([ordered]@{ name='\\\\.\\DISPLAY1'; physicalWidth=2560; physicalHeight=1440; refreshHz=60.0 })\n"
            "    screensError = $null\n"
            "    target = [ordered]@{ name='\\\\.\\DISPLAY1'; reason='max_physical_pixels'; candidates=1; fallback=$true }\n"
            "    targetError = $null\n"
            "    placement = [ordered]@{ mode='windowed'; previewWidth=2560; previewHeight=1440 }\n"
            "    placementError = $null\n"
            "}\n"
            "$block = Build-AttrCudaDisplayBlock -WindowsInventory $inv -Venue 'ultra-magnus' "
            "-ExpectedWidth 3840 -ExpectedHeight 2160 -AppSelection $appSel\n"
            "Write-Host (Get-AttrCudaDisplayResultTail $block)\n"
            "Write-Host \"FALLBACK=$($block.selectionFallback)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("DISPLAY=\\\\.\\DISPLAY1 RES=2560x1440@60 DEGRADED=1 PREVIEW=2560x1440", proc.stdout)
        self.assertIn("FALLBACK=True", proc.stdout)

    def test_uncollected_windows_inventory_is_degraded_unknown_even_with_a_4k_qt_target(self) -> None:
        # sol's exact repro (pre-review BLOCKER 2): WindowsInventory.collected=false plus a Qt
        # target that itself reads 3840x2160 used to produce DEGRADED=0 -- the independent
        # Windows-API check that DEGRADED exists to provide was never actually consulted.
        proc = self.run_probe(
            "$inv = [pscustomobject]@{ collected = $false; devices = @(); "
            "error = 'UnauthorizedAccessException' }\n"
            "$appSel = [pscustomobject]@{\n"
            "    screensCollected = $true\n"
            "    screens = @([ordered]@{ name='\\\\.\\DISPLAY1'; physicalWidth=3840; physicalHeight=2160; refreshHz=60.0 })\n"
            "    screensError = $null\n"
            "    target = [ordered]@{ name='\\\\.\\DISPLAY1'; reason='max_physical_pixels'; candidates=1; fallback=$false }\n"
            "    targetError = $null\n"
            "    placement = [ordered]@{ mode='fullscreen'; previewWidth=3840; previewHeight=2160 }\n"
            "    placementError = $null\n"
            "}\n"
            "$block = Build-AttrCudaDisplayBlock -WindowsInventory $inv -Venue 'ultra-magnus' "
            "-ExpectedWidth 3840 -ExpectedHeight 2160 -AppSelection $appSel\n"
            "Write-Host (Get-AttrCudaDisplayResultTail $block)\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("DISPLAY=\\\\.\\DISPLAY1 RES=3840x2160@60 DEGRADED=unknown PREVIEW=3840x2160", proc.stdout)

    def test_windows_inventory_collected_with_zero_devices_is_degraded_unknown(self) -> None:
        # opus design-review sibling gap: collected=true with an empty device list (the adapter
        # loop breaking immediately, plausible headless/Session-0) must be as unknown as
        # collected=false, never "not degraded" purely on the app's own Qt-reported size.
        proc = self.run_probe(
            "$inv = [pscustomobject]@{ collected = $true; devices = @(); error = $null }\n"
            "$appSel = [pscustomobject]@{\n"
            "    screensCollected = $true\n"
            "    screens = @([ordered]@{ name='\\\\.\\DISPLAY1'; physicalWidth=3840; physicalHeight=2160; refreshHz=60.0 })\n"
            "    screensError = $null\n"
            "    target = [ordered]@{ name='\\\\.\\DISPLAY1'; reason='max_physical_pixels'; candidates=1; fallback=$false }\n"
            "    targetError = $null\n"
            "    placement = [ordered]@{ mode='fullscreen'; previewWidth=3840; previewHeight=2160 }\n"
            "    placementError = $null\n"
            "}\n"
            "$block = Build-AttrCudaDisplayBlock -WindowsInventory $inv -Venue 'ultra-magnus' "
            "-ExpectedWidth 3840 -ExpectedHeight 2160 -AppSelection $appSel\n"
            "Write-Host (Get-AttrCudaDisplayResultTail $block)\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("DEGRADED=unknown", proc.stdout)

    def test_windows_inventory_with_every_device_mode_unreadable_is_degraded_unknown(self) -> None:
        proc = self.run_probe(
            "$inv = [pscustomobject]@{ collected = $true; devices = @([pscustomobject]@{ "
            "deviceName='\\\\.\\DISPLAY1'; modeCollected=$false; width=$null; height=$null }); "
            "error = $null }\n"
            "$appSel = [pscustomobject]@{\n"
            "    screensCollected = $true\n"
            "    screens = @([ordered]@{ name='\\\\.\\DISPLAY1'; physicalWidth=3840; physicalHeight=2160; refreshHz=60.0 })\n"
            "    screensError = $null\n"
            "    target = [ordered]@{ name='\\\\.\\DISPLAY1'; reason='max_physical_pixels'; candidates=1; fallback=$false }\n"
            "    targetError = $null\n"
            "    placement = [ordered]@{ mode='fullscreen'; previewWidth=3840; previewHeight=2160 }\n"
            "    placementError = $null\n"
            "}\n"
            "$block = Build-AttrCudaDisplayBlock -WindowsInventory $inv -Venue 'ultra-magnus' "
            "-ExpectedWidth 3840 -ExpectedHeight 2160 -AppSelection $appSel\n"
            "Write-Host (Get-AttrCudaDisplayResultTail $block)\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("DEGRADED=unknown", proc.stdout)

    def test_a_rejected_move_reports_the_actual_presentation_screen_not_the_intended_target(self) -> None:
        # UM-DISPLAY-SELECT-AND-LOG-1 round 1c (sol BLOCKER 3 job-side gap / opus design-review
        # item 3): a rejected move leaves target and presentation different (verified=0) --
        # DISPLAY=/RES=/DEGRADED= must report where playback actually ran (the Denon fallback,
        # 2560x1440), never the intended 4K target, or sol's blocker moves one layer down into
        # this job.
        proc = self.run_probe(
            "$inv = [pscustomobject]@{ collected = $true; devices = @([pscustomobject]@{ "
            "deviceName='\\\\.\\DISPLAY2'; modeCollected=$true; width=2560; height=1440 }); error = $null }\n"
            "$appSel = [pscustomobject]@{\n"
            "    screensCollected = $true\n"
            "    screens = @(\n"
            "        [ordered]@{ name='TARGET'; physicalWidth=3840; physicalHeight=2160; refreshHz=60.0 },\n"
            "        [ordered]@{ name='\\\\.\\DISPLAY2'; physicalWidth=2560; physicalHeight=1440; refreshHz=60.0 }\n"
            "    )\n"
            "    screensError = $null\n"
            "    target = [ordered]@{ name='TARGET'; reason='max_physical_pixels'; candidates=2; fallback=$false }\n"
            "    targetError = $null\n"
            "    placement = [ordered]@{ mode='fullscreen'; previewWidth=2560; previewHeight=1440; "
            "verified=$false; targetScreenName='TARGET'; presentationScreenName='\\\\.\\DISPLAY2'; "
            "presentationPhysicalWidth=2560; presentationPhysicalHeight=1440 }\n"
            "    placementError = $null\n"
            "}\n"
            "$block = Build-AttrCudaDisplayBlock -WindowsInventory $inv -Venue 'ultra-magnus' "
            "-ExpectedWidth 3840 -ExpectedHeight 2160 -AppSelection $appSel\n"
            "Write-Host (Get-AttrCudaDisplayResultTail $block)\n"
            "Write-Host \"TARGET_NAME=$($block.target.name) PRESENTATION_NAME=$($block.presentation.name) "
            "PLACEMENT_VERIFIED=$($block.placementVerified)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("DISPLAY=\\\\.\\DISPLAY2 RES=2560x1440@60 DEGRADED=1 PREVIEW=2560x1440", proc.stdout)
        self.assertIn("TARGET_NAME=TARGET PRESENTATION_NAME=\\\\.\\DISPLAY2 PLACEMENT_VERIFIED=False", proc.stdout)

    def test_a_verified_move_reports_the_same_presentation_as_target(self) -> None:
        proc = self.run_probe(
            "$inv = [pscustomobject]@{ collected = $true; devices = @([pscustomobject]@{ "
            "deviceName='\\\\.\\DISPLAY1'; modeCollected=$true; width=3840; height=2160 }); error = $null }\n"
            "$appSel = [pscustomobject]@{\n"
            "    screensCollected = $true\n"
            "    screens = @([ordered]@{ name='\\\\.\\DISPLAY1'; physicalWidth=3840; physicalHeight=2160; refreshHz=60.0 })\n"
            "    screensError = $null\n"
            "    target = [ordered]@{ name='\\\\.\\DISPLAY1'; reason='max_physical_pixels'; candidates=1; fallback=$false }\n"
            "    targetError = $null\n"
            "    placement = [ordered]@{ mode='fullscreen'; previewWidth=3840; previewHeight=2160; "
            "verified=$true; targetScreenName='X'; presentationScreenName='X'; "
            "presentationPhysicalWidth=3840; presentationPhysicalHeight=2160 }\n"
            "    placementError = $null\n"
            "}\n"
            "$block = Build-AttrCudaDisplayBlock -WindowsInventory $inv -Venue 'ultra-magnus' "
            "-ExpectedWidth 3840 -ExpectedHeight 2160 -AppSelection $appSel\n"
            "Write-Host (Get-AttrCudaDisplayResultTail $block)\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("DISPLAY=\\\\.\\DISPLAY1 RES=3840x2160@60 DEGRADED=0 PREVIEW=3840x2160", proc.stdout)

    def test_a_legacy_placement_with_no_presentation_field_falls_back_to_the_target(self) -> None:
        # Pre-round-1c behavior preserved for a legacy binary/log that never logged
        # presentation_screen=.
        proc = self.run_probe(
            "$inv = [pscustomobject]@{ collected = $true; devices = @([pscustomobject]@{ "
            "deviceName='\\\\.\\DISPLAY1'; modeCollected=$true; width=3840; height=2160 }); error = $null }\n"
            "$appSel = [pscustomobject]@{\n"
            "    screensCollected = $true\n"
            "    screens = @([ordered]@{ name='\\\\.\\DISPLAY1'; physicalWidth=3840; physicalHeight=2160; refreshHz=60.0 })\n"
            "    screensError = $null\n"
            "    target = [ordered]@{ name='\\\\.\\DISPLAY1'; reason='max_physical_pixels'; candidates=1; fallback=$false }\n"
            "    targetError = $null\n"
            "    placement = [ordered]@{ mode='fullscreen'; previewWidth=3840; previewHeight=2160 }\n"
            "    placementError = $null\n"
            "}\n"
            "$block = Build-AttrCudaDisplayBlock -WindowsInventory $inv -Venue 'ultra-magnus' "
            "-ExpectedWidth 3840 -ExpectedHeight 2160 -AppSelection $appSel\n"
            "Write-Host (Get-AttrCudaDisplayResultTail $block)\n"
            "Write-Host \"PRESENTATION_NULL=$($null -eq $block.presentation) "
            "REASON=$($block.presentationUnknownReason)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("DISPLAY=\\\\.\\DISPLAY1 RES=3840x2160@60 DEGRADED=0 PREVIEW=3840x2160", proc.stdout)
        self.assertIn("PRESENTATION_NULL=True", proc.stdout)

    def test_before_the_smoke_log_is_available_every_field_reads_unknown(self) -> None:
        proc = self.run_probe(
            "$inv = [pscustomobject]@{ collected = $true; devices = @(); error = $null }\n"
            "$block = Build-AttrCudaDisplayBlock -WindowsInventory $inv -Venue 'ultra-magnus' "
            "-ExpectedWidth 3840 -ExpectedHeight 2160 -AppSelection $null\n"
            "Write-Host (Get-AttrCudaDisplayResultTail $block)\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("DISPLAY=unknown RES=unknown DEGRADED=unknown PREVIEW=unknown", proc.stdout)

    def test_bachelor_venue_with_no_expectation_reads_degraded_unknown_never_zero(self) -> None:
        proc = self.run_probe(
            "$inv = [pscustomobject]@{ collected = $true; devices = @(); error = $null }\n"
            "$appSel = [pscustomobject]@{\n"
            "    screensCollected = $true\n"
            "    screens = @([ordered]@{ name='\\\\.\\DISPLAY1'; physicalWidth=1920; physicalHeight=1080; refreshHz=144.0 })\n"
            "    screensError = $null\n"
            "    target = [ordered]@{ name='\\\\.\\DISPLAY1'; reason='max_physical_pixels'; candidates=1; fallback=$false }\n"
            "    targetError = $null\n"
            "    placement = [ordered]@{ mode='fullscreen'; previewWidth=1920; previewHeight=1080 }\n"
            "    placementError = $null\n"
            "}\n"
            "$block = Build-AttrCudaDisplayBlock -WindowsInventory $inv -Venue 'bachelor' "
            "-ExpectedWidth $null -ExpectedHeight $null -AppSelection $appSel\n"
            "Write-Host (Get-AttrCudaDisplayResultTail $block)\n"
            "Write-Host \"EXPECTED_NULL=$($null -eq $block.expected)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("DISPLAY=\\\\.\\DISPLAY1 RES=1920x1080@144 DEGRADED=unknown PREVIEW=1920x1080", proc.stdout)
        self.assertIn("EXPECTED_NULL=True", proc.stdout)


@requires_pwsh
class PresentationWindowsMappingTests(_ProbeCase):
    """UM-DISPLAY-SELECT-AND-LOG-1 round 2 (sol PRE-REVIEW #2 BLOCKERs a and b).

    (b) DEGRADED may be asserted only when the screen the leg ACTUALLY presented on maps to a
    Windows device whose mode was readable -- "some other device was readable" is not
    corroboration. (a) The venue's preferred monitor is named by its Windows monitorName
    ('ASUS PA329C'), but Qt reports GDI device names (\\\\.\\DISPLAYn) with no model, so the job
    must resolve monitorName -> deviceName from the Windows inventory before the app sees it.
    """

    def setUp(self) -> None:
        super().setUp()
        text = JOB_SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")
        build_at = text.index("function Build-AttrCudaDisplayBlock")
        tail_end = text.index("\n\nforeach ($item in @(", text.index("function Get-AttrCudaDisplayResultTail"))
        self.functions = text[build_at:tail_end]

    def run_probe(self, body: str) -> subprocess.CompletedProcess:
        # The job embeds the shared display-identity functions (Build-AttrCudaDisplayBlock calls
        # Get-GuiSmokeDisplayIdentity); a probe of the extracted function needs them in scope.
        return self.run_snippet(f". '{SHARED_IDENTITY}'\n" + self.functions + "\n" + body)

    D1 = "\\\\.\\DISPLAY1"
    D2 = "\\\\.\\DISPLAY2"

    def _app_sel_on_display1(self) -> str:
        return (
            "$appSel = [pscustomobject]@{\n"
            "    screensCollected = $true\n"
            f"    screens = @([ordered]@{{ name='{self.D1}'; physicalWidth=3840; physicalHeight=2160; refreshHz=60.0 }})\n"
            "    screensError = $null\n"
            f"    target = [ordered]@{{ name='{self.D1}'; reason='max_physical_pixels'; candidates=1; fallback=$false }}\n"
            "    targetError = $null\n"
            "    placement = [ordered]@{ mode='fullscreen'; previewWidth=3840; previewHeight=2160; verified=$true; "
            f"targetScreenName='{self.D1}'; presentationScreenName='{self.D1}'; "
            "presentationPhysicalWidth=3840; presentationPhysicalHeight=2160 }\n"
            "    placementError = $null\n"
            "}\n"
        )

    def _block_probe(self, devices: str) -> subprocess.CompletedProcess:
        return self.run_probe(
            f"$inv = [pscustomobject]@{{ collected = $true; devices = @({devices}); error = $null }}\n"
            + self._app_sel_on_display1() +
            "$block = Build-AttrCudaDisplayBlock -WindowsInventory $inv -Venue 'ultra-magnus' "
            "-ExpectedWidth 3840 -ExpectedHeight 2160 -AppSelection $appSel\n"
            "Write-Host (Get-AttrCudaDisplayResultTail $block)\n"
            "Write-Host \"MAPPING=$($block.presentationWindowsDevice.status)\"\n"
        )

    def test_an_unrelated_readable_windows_device_does_not_corroborate_an_unmapped_presentation_screen(self) -> None:
        # MUTATION CAUGHT: reverting Build-AttrCudaDisplayBlock to "any device with a readable
        # mode" (the r1c rule). Sol's exact repro: only DISPLAY2 was readable, the app presented
        # on DISPLAY1, and the leg published DEGRADED=0.
        proc = self._block_probe(
            f"[pscustomobject]@{{ deviceName='{self.D2}'; modeCollected=$true; width=3840; height=2160 }}")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("DEGRADED=unknown", proc.stdout)
        self.assertNotIn("DEGRADED=0", proc.stdout)
        self.assertIn("MAPPING=unmapped", proc.stdout)

    def test_a_presentation_screen_that_maps_to_a_readable_windows_device_still_gets_a_verdict(self) -> None:
        # Guards the fix against over-refusal: the mapped, readable device keeps the comparison.
        proc = self._block_probe(
            f"[pscustomobject]@{{ deviceName='{self.D1}'; modeCollected=$true; width=3840; height=2160 }}")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("DEGRADED=0", proc.stdout)
        self.assertIn("MAPPING=mapped", proc.stdout)

    def test_a_mapped_but_unreadable_device_is_unknown_and_the_reason_is_published(self) -> None:
        # MUTATION CAUGHT: dropping the modeCollected requirement on the MAPPED device.
        proc = self._block_probe(
            f"[pscustomobject]@{{ deviceName='{self.D1}'; modeCollected=$false; width=$null; height=$null }},"
            f"[pscustomobject]@{{ deviceName='{self.D2}'; modeCollected=$true; width=3840; height=2160 }}")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("DEGRADED=unknown", proc.stdout)
        self.assertIn("MAPPING=unreadable", proc.stdout)

    # ---- (a) preferred monitor -> Windows device name -> Qt screen name -------------------

    def _measured_um_inventory(self) -> str:
        return (
            "$inv = [pscustomobject]@{ collected = $true; devices = @("
            f"[pscustomobject]@{{ deviceName='{self.D1}'; monitorName='ASUS PA329C'; isPrimary=$false; "
            "modeCollected=$true; width=3840; height=2160; refreshHz=60 },"
            f"[pscustomobject]@{{ deviceName='{self.D2}'; monitorName='Generic PnP Monitor'; isPrimary=$true; "
            "modeCollected=$true; width=3840; height=2160; refreshHz=60 }); error = $null }\n"
        )

    def test_the_preferred_monitor_name_resolves_to_its_windows_device_name(self) -> None:
        # MUTATION CAUGHT: passing the raw 'PA329C' substring straight to the app (r1c). Qt
        # reports name=\\.\DISPLAYn with empty model/manufacturer on the measured UM topology,
        # so PA329C matched neither Qt screen and the refresh tie fell to the primary (the
        # Denon/LG) -- sol's blocker (a).
        proc = self.run_snippet(
            self._measured_um_inventory() +
            "$r = Resolve-AttrCudaPreferredDisplay -WindowsInventory $inv -Substring 'PA329C'\n"
            "Write-Host \"ARG=$($r.argument) STATUS=$($r.status) DEVICE=$($r.deviceName) MONITOR=$($r.monitorName)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"ARG={self.D1} STATUS=mapped DEVICE={self.D1} MONITOR=ASUS PA329C", proc.stdout)

    def test_an_ambiguous_or_absent_or_unreadable_preference_keeps_the_substring_and_says_why(self) -> None:
        proc = self.run_snippet(
            "$inv2 = [pscustomobject]@{ collected = $true; devices = @("
            f"[pscustomobject]@{{ deviceName='{self.D1}'; monitorName='ASUS PA329C' }},"
            f"[pscustomobject]@{{ deviceName='{self.D2}'; monitorName='ASUS PA329C' }}); error = $null }}\n"
            "$absent = [pscustomobject]@{ collected = $true; devices = @("
            f"[pscustomobject]@{{ deviceName='{self.D1}'; monitorName='Generic PnP Monitor' }}); error = $null }}\n"
            "$bad = [pscustomobject]@{ collected = $false; devices = @(); error = 'X' }\n"
            "foreach ($case in @(@('ambiguous',$inv2),@('absent',$absent),@('unknown',$bad))) {\n"
            "    $r = Resolve-AttrCudaPreferredDisplay -WindowsInventory $case[1] -Substring 'PA329C'\n"
            "    Write-Host \"CASE=$($case[0]) STATUS=$($r.status) ARG=$($r.argument)\"\n"
            "}\n"
            "$n = Resolve-AttrCudaPreferredDisplay -WindowsInventory $absent -Substring ''\n"
            "Write-Host \"CASE=none STATUS=$($n.status) ARG=[$($n.argument)]\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for line in ("CASE=ambiguous STATUS=ambiguous ARG=PA329C", "CASE=absent STATUS=absent ARG=PA329C",
                     "CASE=unknown STATUS=unknown ARG=PA329C", "CASE=none STATUS=none ARG=[]"):
            self.assertIn(line, proc.stdout)

    def test_the_job_passes_the_resolved_device_name_not_the_raw_substring_to_the_runner(self) -> None:
        text = JOB_SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")
        self.assertIn("Resolve-AttrCudaPreferredDisplay -WindowsInventory $windowsDisplayInventory", text)
        self.assertIn("-DisplayPrefer $(ConvertTo-PsSingleQuoted $displayPreferArgument)", text)
        self.assertNotIn("-DisplayPrefer $(ConvertTo-PsSingleQuoted $displayPreferSubstring)", text)
        self.assertIn("'Resolve-AttrCudaPreferredDisplay'", text)


@requires_pwsh
class JobTemplateDisplayBlockWiringTests(unittest.TestCase):
    """Text-level pin on playback-attr-3-cuda-job.ps1's embedded template: the display block is
    captured before the CPU-quiescence measurement, recomputed once the smoke log is available,
    and published on every summary.json / the evidence manifest from leg start onward -- including
    every refusal path. RESULT-line DISPLAY=/RES=/DEGRADED=/PREVIEW= fields appear only from the
    point the app's own info is knowable (after the smoke log is parsed), never on an earlier
    refusal whose RESULT= text is pinned unchanged by test_attr_cuda_cpu_quiescence.py."""

    def setUp(self) -> None:
        self.source = JOB_SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")
        start = self.source.index("$template = @'")
        end = self.source.index("\n'@", start)
        self.template = self.source[start:end]

    def test_the_four_new_functions_are_embedded_and_called_after_the_placeholder(self) -> None:
        placeholder_at = self.source.index("__EMBEDDED_FUNCTIONS__")
        for name in (
            "Get-AttrCudaWindowsDisplayInventory",
            "Get-AttrCudaMeasurementVenue",
            "Get-AttrCudaDisplayDegradedState",
            "Get-AttrCudaGuiSmokeDisplaySelection",
        ):
            with self.subTest(function=name):
                self.assertIn(f"'{name}'", self.source[:placeholder_at])
                self.assertGreater(self.template.find(name), -1)

    def test_windows_inventory_and_venue_are_captured_before_the_quiescence_sample(self) -> None:
        inventory_at = self.template.index("$windowsDisplayInventory = Get-AttrCudaWindowsDisplayInventory")
        venue_at = self.template.index("$measurementVenue = Get-AttrCudaMeasurementVenue")
        first_display_block_at = self.template.index("$displayBlock = Build-AttrCudaDisplayBlock", inventory_at)
        quiescence_sample_at = self.template.index("$cpuProcessBefore = Get-AttrCudaProcessCpuSnapshot")
        self.assertGreater(venue_at, inventory_at)
        self.assertGreater(first_display_block_at, venue_at)
        self.assertGreater(quiescence_sample_at, first_display_block_at)

    def test_only_ultra_magnus_venue_gets_an_expected_resolution(self) -> None:
        venue_at = self.template.index("$measurementVenue = Get-AttrCudaMeasurementVenue")
        tail = self.template[venue_at:venue_at + 500]
        self.assertIn("if ($measurementVenue -eq 'ultra-magnus') {", tail)
        self.assertIn("$expectedDisplayWidth = 3840", tail)
        self.assertIn("$expectedDisplayHeight = 2160", tail)

    def test_only_ultra_magnus_venue_gets_a_preferred_display_substring(self) -> None:
        # UM-DISPLAY-SELECT-AND-LOG-1 round 1c: kept in the same venue table, next to the
        # expected resolution.
        venue_at = self.template.index("$measurementVenue = Get-AttrCudaMeasurementVenue")
        tail = self.template[venue_at:venue_at + 500]
        self.assertIn("$displayPreferSubstring = ''", tail)
        self.assertIn("$displayPreferSubstring = 'PA329C'", tail)

    def test_app_selection_is_reparsed_once_the_smoke_log_is_available(self) -> None:
        raw_log_at = self.template.index("$rawLog = [IO.File]::ReadAllText($logPath)")
        reparse_at = self.template.index(
            "$appDisplaySelection = Get-AttrCudaGuiSmokeDisplaySelection -LogText $rawLog", raw_log_at)
        rebuild_at = self.template.index("$displayBlock = Build-AttrCudaDisplayBlock", reparse_at)
        get_frame_rows_at = self.template.index("$rows = Get-FrameRows $rawLog")
        self.assertGreater(reparse_at, raw_log_at)
        self.assertGreater(rebuild_at, reparse_at)
        # Reparsed BEFORE frame rows are pulled, so a display-parsing failure can never be masked
        # by a later frame-row failure that would exit first.
        self.assertGreater(get_frame_rows_at, rebuild_at)

    def test_display_block_is_published_on_every_refusal_path_including_before_the_smoke_log(self) -> None:
        anchors = (
            "result='VENUE_NOT_QUIESCENT'",
            "result='SMOKE_RUN_FAILED'",
            "result='SMOKE_LOG_UNAVAILABLE'",
            "result='PRESENTMON_UNAVAILABLE'",
            "result='BACKEND_NOT_AVAILABLE'",
            "result='GPU_RECON_FRAMES_ZERO'",
            "result='CPU_FALLBACK_DETECTED'",
        )
        for anchor in anchors:
            with self.subTest(anchor=anchor):
                at = self.template.index(anchor)
                tail = self.template[at:at + 700]
                self.assertIn("display=$displayBlock", tail)

    def test_display_block_is_published_on_the_presentmon_display_report_failure_path(self) -> None:
        at = self.template.index("result=$displayReport.status")
        # window widened 500 -> 900: fork/master's CUDA-PERF-DISPLAY-WAKE-1 comment + displayWake
        # line now sit between the anchor and display=$displayBlock.
        tail = self.template[at:at + 900]
        self.assertIn("display=$displayBlock", tail)

    def test_display_block_is_published_in_the_evidence_manifest_and_success_summary(self) -> None:
        manifest_at = self.template.index("$manifest = [ordered]@{")
        manifest_end = self.template.index("Save-Json $manifest", manifest_at)
        self.assertIn("display = $displayBlock", self.template[manifest_at:manifest_end])

        summary_at = self.template.index("result = $(if ($FixtureRehearsal)")
        summary_end = self.template.index("(Join-Path $Pub 'summary.json')", summary_at)
        self.assertIn("display = $displayBlock", self.template[summary_at:summary_end])

    def test_result_lines_carry_the_display_tail_from_the_point_it_is_known_onward(self) -> None:
        for anchor in (
            'Write-Output "RESULT=PRESENTMON_UNAVAILABLE',
            'Write-Output "RESULT=BACKEND_NOT_AVAILABLE',
            'Write-Output "RESULT=GPU_RECON_FRAMES_ZERO',
            'Write-Output "RESULT=CPU_FALLBACK_DETECTED',
            'Write-Output "RESULT=$($displayReport.status)',
        ):
            with self.subTest(anchor=anchor):
                at = self.template.index(anchor)
                line_end = self.template.index("\n", at)
                self.assertIn("Get-AttrCudaDisplayResultTail $displayBlock", self.template[at:line_end])
        # The final success RESULT line also carries it.
        final_at = self.template.index('Write-Output "RESULT=$resultVerb')
        final_end = self.template.index("\n", final_at)
        self.assertIn("Get-AttrCudaDisplayResultTail $displayBlock", self.template[final_at:final_end])

    def test_earlier_refusal_result_lines_are_unchanged_by_this_round(self) -> None:
        # These three RESULT= lines are pinned verbatim by test_attr_cuda_cpu_quiescence.py /
        # existing owner-footage tests -- the display tail must NOT be added to them, since the
        # app's own display info is not knowable before the smoke log is parsed.
        self.assertIn(
            'Write-Output "RESULT=VENUE_NOT_QUIESCENT CPU_TIME=$cpuTimeField CPU_UTILITY=$avgUtility ARTIFACTS=$Pub"',
            self.template,
        )
        smoke_run_failed_at = self.template.index('Write-Output "RESULT=SMOKE_RUN_FAILED')
        smoke_run_failed_end = self.template.index("\n", smoke_run_failed_at)
        self.assertNotIn("Get-AttrCudaDisplayResultTail", self.template[smoke_run_failed_at:smoke_run_failed_end])

        smoke_log_at = self.template.index('Write-Output "RESULT=SMOKE_LOG_UNAVAILABLE')
        smoke_log_end = self.template.index("\n", smoke_log_at)
        self.assertNotIn("Get-AttrCudaDisplayResultTail", self.template[smoke_log_at:smoke_log_end])

    def test_build_display_block_never_folds_missing_target_into_not_degraded(self) -> None:
        build_at = self.template.index("function Build-AttrCudaDisplayBlock")
        build_end = self.template.index("\nfunction Get-AttrCudaDisplayResultTail", build_at)
        body = self.template[build_at:build_end]
        self.assertIn("Get-AttrCudaDisplayDegradedState", body)
        # The result field's own third state is used unmodified -- no local override that could
        # collapse 'unknown' into $false.
        self.assertIn("displayDegraded = $degraded", body)


if __name__ == "__main__":
    unittest.main()
