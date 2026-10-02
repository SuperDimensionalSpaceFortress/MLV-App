"""Behavioural tests for DISPLAY-SMOKE-FAILED-LOG-PRESERVE-1 (origin: PR #191, UM-DISPLAY-SELECT-AND-LOG-1).

WHY. On SMOKE_RUN_FAILED the attribution job (playback-attr-3-cuda-job.ps1) used to publish the
display block it captured BEFORE the smoke run, so a failed leg still carried a display block that
looked like a measurement. Two changes, both pinned here:

  1. The published block says so on its own face: `measured = $false` next to `phase = 'pre-smoke'`,
     and its `presentationIdentity` is the shared parser's identity-UNKNOWN record (never $null).
     compare-release-gui-smoke-ab.ps1's Get-DisplayComparability treats a block that says
     pre-smoke / measured=false as identity UNKNOWN for A/B purposes, even when every other
     identity field happens to be populated -- never 'same'.
  2. When the failed smoke DID leave the display log (run-release-gui-smoke.ps1 creates a nonced
     `logs-<stem>-<nonce>` directory next to result.json and the app writes mlvapp-*.log into it
     BEFORE result.json exists), Find-AttrCudaFailedSmokeDisplayLog locates it WITHOUT result.json
     and hands it to the SAME shared parser, so the block becomes post-smoke / measured. If the log
     cannot be located unambiguously, the block stays pre-smoke. Never guess.

These tests EXECUTE the real module function, the job template's real Build-AttrCudaDisplayBlock and
its real SMOKE_RUN_FAILED branch (extracted, never reimplemented), and the comparator's real
Get-DisplayComparability. Each test names the mutation it catches.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "tools" / "profiling" / "bachelor" / "AttrCudaArtifacts.psm1"
JOB_SCRIPT = ROOT / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-job.ps1"
COMPARE_SCRIPT = ROOT / "tools" / "profiling" / "compare-release-gui-smoke-ab.ps1"
SHARED = ROOT / "tools" / "profiling" / "gui-smoke-display-identity.ps1"

PWSH = shutil.which("pwsh")
requires_pwsh = unittest.skipIf(PWSH is None, "pwsh is not on PATH")

D1 = "\\\\.\\DISPLAY1"
SCREEN = (
    f'gui_smoke.display_screen index=0 name="{D1}" manufacturer="ASUS" model="PA329C" serial="S-9" '
    "geometry=0,0 3840x2160 physical=3840x2160 dpr=1.00 refresh_hz=59.997 primary=1"
)
TARGET = f'gui_smoke.display_target screen="{D1}" reason=max_physical_pixels candidates=1 fallback=0'
PLACEMENT = (
    f'gui_smoke.window_placement mode=fullscreen screen="{D1}" verified=1 window=0,0 3840x2160 '
    f'preview=3840x2160 target_screen="{D1}" presentation_screen="{D1}" presentation_physical=3840x2160'
)
DISPLAY_LINES = (SCREEN, TARGET, PLACEMENT)
NONCE_A = "a" * 32
NONCE_B = "b" * 32


def _job_text() -> str:
    return JOB_SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")


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


def _build_block_function() -> str:
    text = _job_text()
    start = text.index("function Build-AttrCudaDisplayBlock")
    end = text.index("\nfunction Get-AttrCudaDisplayResultTail", start)
    return text[start:end]


def _failure_branch() -> str:
    """The job template's real SMOKE_RUN_FAILED if-block, from its condition to its `exit 18`."""
    text = _job_text()
    template = text[text.index("$template = @'"):]
    start = template.index("if ($null -ne $smokeLaunchException -or $smokeRc -ne 0 -or -not (Test-Path")
    end = template.index("    exit 18\n}", start) + len("    exit 18\n}")
    return template[start:end]


def _ps_lines(lines: list[str]) -> str:
    """A PowerShell array literal of single-quoted strings (the payloads carry no single quotes)."""
    return "@(" + ",".join("'" + line.replace("'", "''") + "'" for line in lines) + ")"


class _ProbeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="display-smoke-failed-log-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def run_snippet(self, body: str) -> subprocess.CompletedProcess:
        script = self.tmp / "probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            # $env:TEMP is null on Linux: hand the probe the test's own temp dir instead.
            f"$TestTmp = '{self.tmp}'\n"
            f"Import-Module '{MODULE}' -Force\n" + body,
            encoding="utf-8",
        )
        return subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(script)],
            capture_output=True, text=True,
        )


# PowerShell fragment: a leg output dir, a launch instant a second in the past, and a helper that
# writes an app log (timestamped NOW, i.e. after the launch instant) into a nonced logs dir.
_LEG_SETUP = r"""
$legOut = Join-Path $TestTmp ('leg-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $legOut | Out-Null
$launched = (Get-Date).ToUniversalTime().AddSeconds(-1)
function New-AppLog([string]$Nonce, [string[]]$Payloads, [string]$Name = 'mlvapp-1.log', [double]$AgeSeconds = 0) {
    $dir = Join-Path $legOut ('logs-result-' + $Nonce)
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
    $stamp = (Get-Date).ToUniversalTime().AddSeconds(-$AgeSeconds).ToString('yyyy-MM-dd HH:mm:ss.fff')
    $lines = @($Payloads | ForEach-Object { "[$stamp] pid=42 $_" })
    [IO.File]::WriteAllLines((Join-Path $dir $Name), [string[]]$lines)
    $dir
}
"""


@requires_pwsh
class FindFailedSmokeDisplayLogTests(_ProbeCase):
    """Find-AttrCudaFailedSmokeDisplayLog: locate + parse the display log with no result.json."""

    def find(self, body: str) -> dict:
        proc = self.run_snippet(
            _LEG_SETUP + body
            + "\n$r = Find-AttrCudaFailedSmokeDisplayLog -LegOut $legOut -LaunchedAtUtc $launched\n"
            "[pscustomobject]@{ found = $r.found; reason = $r.reason; logPath = $r.logPath; "
            "hasSelection = ($null -ne $r.selection); "
            "placementName = $(if ($r.selection -and $r.selection.placement) { $r.selection.placement.presentationScreenName } else { $null }) "
            "} | ConvertTo-Json -Compress\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_a_log_left_by_the_failed_run_is_located_without_a_result_json_and_parsed(self) -> None:
        # MUTATION CAUGHT: the function never locating/parsing the log (recovery stubbed out).
        out = self.find(f"[void](New-AppLog '{NONCE_A}' {_ps_lines(list(DISPLAY_LINES))})\n")
        self.assertTrue(out["found"], out)
        self.assertTrue(out["hasSelection"], out)
        self.assertEqual(out["placementName"], D1)
        self.assertTrue(out["logPath"].endswith("mlvapp-1.log"), out)

    def test_no_logs_directory_is_not_found(self) -> None:
        out = self.find("")
        self.assertFalse(out["found"], out)
        self.assertFalse(out["hasSelection"], out)
        self.assertTrue(out["reason"], out)

    def test_two_candidate_log_directories_are_ambiguous_never_guessed(self) -> None:
        # MUTATION CAUGHT: taking the newest/first of several logs-* directories.
        body = (f"[void](New-AppLog '{NONCE_A}' {_ps_lines(list(DISPLAY_LINES))})\n"
                f"[void](New-AppLog '{NONCE_B}' {_ps_lines(list(DISPLAY_LINES))})\n")
        out = self.find(body)
        self.assertFalse(out["found"], out)
        self.assertIn("ambiguous", out["reason"])

    def test_a_directory_that_predates_the_launch_is_stale_never_used(self) -> None:
        # MUTATION CAUGHT: dropping the creation-time-after-launch guard. The launch instant is moved
        # two hours AFTER the directory was created (setting CreationTimeUtc is a no-op on Linux, so
        # the directory's own time is never edited). The reason text pins WHICH guard refused it: with
        # the directory guard gone the per-line window would still refuse, but with a different reason.
        body = (f"[void](New-AppLog '{NONCE_A}' {_ps_lines(list(DISPLAY_LINES))})\n"
                "$launched = (Get-Date).ToUniversalTime().AddHours(2)\n")
        out = self.find(body)
        self.assertFalse(out["found"], out)
        self.assertIn("no logs-", out["reason"])

    def test_log_lines_older_than_the_launch_are_excluded(self) -> None:
        # MUTATION CAUGHT: dropping the per-line timestamp window (the app log can rotate/aggregate).
        out = self.find(f"[void](New-AppLog '{NONCE_A}' {_ps_lines(list(DISPLAY_LINES))} -AgeSeconds 3600)\n")
        self.assertFalse(out["found"], out)

    def test_a_located_log_with_no_display_lines_is_not_found_and_stays_pre_smoke(self) -> None:
        # MUTATION CAUGHT: reporting found=$true for a log that says nothing about the display.
        out = self.find(f"[void](New-AppLog '{NONCE_A}' {_ps_lines(['playback_smoke.start something=1'])})\n")
        self.assertFalse(out["found"], out)
        self.assertIn("display", out["reason"])

    def test_a_directory_whose_name_is_not_a_run_nonce_is_ignored(self) -> None:
        body = (f"[void](New-AppLog 'not-a-nonce' {_ps_lines(list(DISPLAY_LINES))})\n")
        out = self.find(body)
        self.assertFalse(out["found"], out)


@requires_pwsh
class DisplayBlockMeasuredMarkerTests(_ProbeCase):
    """Build-AttrCudaDisplayBlock (the job template's own) states measured / not measured."""

    def run_block(self, body: str) -> subprocess.CompletedProcess:
        return self.run_snippet(f". '{SHARED}'\n" + _build_block_function() + "\n" + body)

    def test_pre_smoke_block_is_marked_not_measured_with_unknown_identity(self) -> None:
        # MUTATION CAUGHT: dropping `measured`, or leaving presentationIdentity $null in the
        # pre-smoke block (a consumer keying on identityUnknownReason would see no third state).
        proc = self.run_block(
            "$inv = [pscustomobject]@{ collected = $false; devices = @(); error = 'x' }\n"
            "$b = Build-AttrCudaDisplayBlock -WindowsInventory $inv -Venue 'bachelor' -AppSelection $null\n"
            "[pscustomobject]@{ phase = $b.phase; measured = $b.measured; "
            "reason = $b.presentationIdentity.identityUnknownReason } | ConvertTo-Json -Compress\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(out["phase"], "pre-smoke")
        self.assertIs(out["measured"], False)
        self.assertTrue(out["reason"], out)

    def test_post_smoke_block_from_a_parsed_log_is_measured_with_known_identity(self) -> None:
        proc = self.run_block(
            "$inv = [pscustomobject]@{ collected = $false; devices = @(); error = 'x' }\n"
            f"$sel = ConvertFrom-GuiSmokeDisplayLog -LogText ({_ps_lines(list(DISPLAY_LINES))} -join \"`n\")\n"
            "$b = Build-AttrCudaDisplayBlock -WindowsInventory $inv -Venue 'bachelor' -AppSelection $sel\n"
            "[pscustomobject]@{ phase = $b.phase; measured = $b.measured; "
            "reason = $b.presentationIdentity.identityUnknownReason; "
            "model = $b.presentationIdentity.presentationModel } | ConvertTo-Json -Compress\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(out["phase"], "post-smoke")
        self.assertIs(out["measured"], True)
        self.assertIsNone(out["reason"])
        self.assertEqual(out["model"], "PA329C")


@requires_pwsh
class SmokeRunFailedBranchTests(_ProbeCase):
    """The job template's real SMOKE_RUN_FAILED branch, executed end to end (stubs only for the
    PresentMon stop and the JSON writer)."""

    def run_branch(self, setup_log: str, compare: bool = False) -> tuple[subprocess.CompletedProcess, dict]:
        pub = self.tmp / "pub"
        pub.mkdir()
        body = (
            f". '{SHARED}'\n"
            + _build_block_function() + "\n"
            + _LEG_SETUP
            + "$resultPath = Join-Path $legOut 'result.json'\n"  # never created: this is the failure
            + f"$Pub = '{pub}'\n"
            + "$FixtureRehearsal = $true\n$SourceCommit = ('1' * 40)\n$ClipId = 'tiny'\n"
            + "$smokeRc = 5\n$smokeLaunchException = $null\n$presentMonProc = $null\n"
            + "$smokeLaunchedAtUtc = $launched\n"
            + "$displayWake = [ordered]@{}\n"
            + "$windowsDisplayInventory = [pscustomobject]@{ collected = $false; devices = @(); error = 'x' }\n"
            + "$measurementVenue = 'bachelor'\n$expectedDisplayWidth = $null\n$expectedDisplayHeight = $null\n"
            + "$displayPreferResolution = $null\n"
            + "$displayBlock = Build-AttrCudaDisplayBlock -WindowsInventory $windowsDisplayInventory "
            "-Venue $measurementVenue -ExpectedWidth $expectedDisplayWidth -ExpectedHeight $expectedDisplayHeight "
            "-PreferredResolution $displayPreferResolution\n"
            + "function Stop-PresentMonCapture($Proc, [string]$SessionName = '') { [pscustomobject]@{ confirmedExited = $true; killError = $null; waitError = $null } }\n"
            + "function Save-Json($Object, [string]$Path) { $Object | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath $Path -Encoding UTF8 }\n"
            + setup_log
            + _failure_branch() + "\n"
            + "Write-Output 'RESULT=NO_FAILURE_BRANCH_TAKEN'\n"
        )
        proc = self.run_snippet(body)
        summary_path = pub / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
        return proc, summary

    def test_a_failed_smoke_that_left_a_display_log_publishes_a_post_smoke_measured_block(self) -> None:
        # MUTATION CAUGHT: the branch still publishing the pre-smoke block regardless of the log.
        proc, summary = self.run_branch(f"[void](New-AppLog '{NONCE_A}' {_ps_lines(list(DISPLAY_LINES))})\n")
        self.assertEqual(proc.returncode, 18, proc.stdout + proc.stderr)
        display = summary["display"]
        self.assertEqual(display["phase"], "post-smoke", display)
        self.assertIs(display["measured"], True)
        self.assertEqual(display["presentationIdentity"]["presentationModel"], "PA329C")
        self.assertIs(summary["displayLogRecovery"]["found"], True)

    def test_a_failed_smoke_with_no_recoverable_log_stays_pre_smoke_not_measured(self) -> None:
        # MUTATION CAUGHT: guessing a block (or marking it measured) when nothing was located.
        proc, summary = self.run_branch("")
        self.assertEqual(proc.returncode, 18, proc.stdout + proc.stderr)
        display = summary["display"]
        self.assertEqual(display["phase"], "pre-smoke", display)
        self.assertIs(display["measured"], False)
        self.assertTrue(display["presentationIdentity"]["identityUnknownReason"])
        self.assertIs(summary["displayLogRecovery"]["found"], False)
        self.assertTrue(summary["displayLogRecovery"]["reason"])

    def test_ambiguous_logs_on_a_failed_smoke_stay_pre_smoke(self) -> None:
        proc, summary = self.run_branch(
            f"[void](New-AppLog '{NONCE_A}' {_ps_lines(list(DISPLAY_LINES))})\n"
            f"[void](New-AppLog '{NONCE_B}' {_ps_lines(list(DISPLAY_LINES))})\n")
        self.assertEqual(proc.returncode, 18, proc.stdout + proc.stderr)
        self.assertEqual(summary["display"]["phase"], "pre-smoke")
        self.assertIs(summary["display"]["measured"], False)

    def test_the_result_line_is_unchanged_and_carries_no_display_tail(self) -> None:
        proc, _ = self.run_branch(f"[void](New-AppLog '{NONCE_A}' {_ps_lines(list(DISPLAY_LINES))})\n")
        line = [l for l in proc.stdout.splitlines() if l.startswith("RESULT=SMOKE_RUN_FAILED")]
        self.assertEqual(len(line), 1, proc.stdout)
        self.assertNotIn("DISPLAY=", line[0])


@requires_pwsh
class ComparatorTreatsUnmeasuredLegAsUnknownTests(_ProbeCase):
    """Get-DisplayComparability (compare-release-gui-smoke-ab.ps1)."""

    KNOWN = (
        "[pscustomobject]@{ presentationScreenName = 'D1'; presentationManufacturer = 'ASUS'; "
        "presentationModel = 'PA329C'; presentationSerial = 'S'; physicalWidth = 3840; physicalHeight = 2160; "
        "refreshHzRounded = 60; dpr = 1.0; windowMode = 'fullscreen'; previewWidth = 3840; previewHeight = 2160; "
        "verified = $true; identityUnknownReason = $null%s }"
    )

    def compare(self, before_extra: str, after_extra: str) -> dict:
        proc = self.run_snippet(
            _extract_function(COMPARE_SCRIPT, "Get-DisplayComparability") + "\n"
            f"$b = {self.KNOWN % before_extra}\n$a = {self.KNOWN % after_extra}\n"
            "$r = Get-DisplayComparability -BeforeDisplay $b -AfterDisplay $a\n"
            "[pscustomobject]@{ status = $r.status; reasonCode = $r.reasonCode; detail = $r.detail } | ConvertTo-Json -Compress\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_control_two_identical_known_legs_are_same(self) -> None:
        self.assertEqual(self.compare("", "")["status"], "same")

    def test_a_pre_smoke_leg_with_populated_identity_fields_is_unknown_never_same(self) -> None:
        # MUTATION CAUGHT: the comparator ignoring `phase` (a pre-smoke block wearing identity fields).
        out = self.compare("; phase = 'pre-smoke'", "")
        self.assertEqual(out["status"], "unknown", out)
        self.assertEqual(out["reasonCode"], "DISPLAY_IDENTITY_UNKNOWN")
        self.assertIn("not measured", out["detail"])

    def test_a_measured_false_leg_is_unknown_never_same_on_either_side(self) -> None:
        # MUTATION CAUGHT: the comparator ignoring `measured` (or checking only the before side).
        for before, after in (("; measured = $false", ""), ("", "; measured = $false")):
            with self.subTest(before=before, after=after):
                out = self.compare(before, after)
                self.assertEqual(out["status"], "unknown", out)

    def test_two_pre_smoke_legs_with_identical_fields_are_unknown_never_same(self) -> None:
        out = self.compare("; phase = 'pre-smoke'; measured = $false", "; phase = 'pre-smoke'; measured = $false")
        self.assertEqual(out["status"], "unknown", out)

    def test_a_post_smoke_measured_leg_still_compares_normally(self) -> None:
        out = self.compare("; phase = 'post-smoke'; measured = $true", "; phase = 'post-smoke'; measured = $true")
        self.assertEqual(out["status"], "same", out)


class TemplateWiringTests(unittest.TestCase):
    """Static pins on the generator (the lint/coverage tests cover the embedded-function contract)."""

    def test_the_recovery_function_is_embedded_into_the_job(self) -> None:
        text = _job_text()
        embedded_at = text.index("'Get-AttrCudaGuiSmokeDisplaySelection'")
        self.assertIn("'Find-AttrCudaFailedSmokeDisplayLog'", text[embedded_at - 200:embedded_at + 400])

    def test_the_launch_instant_is_recorded_before_the_smoke_launch_and_recovery_precedes_the_write(self) -> None:
        text = _job_text()
        template = text[text.index("$template = @'"):]
        stamp_at = template.index("$smokeLaunchedAtUtc = ")
        launch_at = template.index("& \"$env:ProgramFiles\\PowerShell\\7\\pwsh.exe\"")
        self.assertLess(stamp_at, launch_at)
        branch = _failure_branch()
        self.assertLess(branch.index("Find-AttrCudaFailedSmokeDisplayLog"), branch.index("Save-Json $smokeFailure"))
        self.assertIn("display=$displayBlock", branch)
        self.assertIn("displayLogRecovery=", branch)


if __name__ == "__main__":
    unittest.main()
