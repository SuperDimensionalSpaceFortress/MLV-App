"""Behavioural tests for PLAYBACK-MEASURE-HOST-LOAD-GATE-1.

BACHELOR is also the owner's interactive workstation: an fps number measured while it is loaded
is not a property of the build (measured 2026-09-22 -- CPU_LOAD_PCT=96 turned a 4.8fps CPU
reference into 1.2fps with identical route counters; see
.claude-state/project-memory/bachelor-is-an-interactive-workstation-load-invalidates-fps-20260922.md).
These tests EXECUTE pwsh against the real functions, not a reimplementation of their logic --
tools/profiling/run-release-gui-smoke.ps1, tools/profiling/compare-release-gui-smoke-ab.ps1 and
tools/profiling/run-release-cuda-playback-ab.ps1 are procedural .ps1 scripts (not modules), so
each test extracts the target function's exact source text out of the real file by regex and
dot-sources it into a throwaway probe script -- the same "spliced verbatim" precedent
test_playback_attr_3_cuda_behaviour.py documents: a pass here is a statement about the code that
actually runs the gate, not a copy of it. What is NOT exercised: a full GUI-smoke launch (needs a
real MLVApp.exe and clip) and a full CUDA A/B run (needs a 4090) -- those are covered by the
fixture-driven unit level only. Everything is skipped cleanly when pwsh is not on PATH.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

ROOT = Path(__file__).resolve().parents[2]
SMOKE_SCRIPT = ROOT / "tools" / "profiling" / "run-release-gui-smoke.ps1"
COMPARE_SCRIPT = ROOT / "tools" / "profiling" / "compare-release-gui-smoke-ab.ps1"
# VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-1 r6 (sol r5 B1): the comparator judges a host-load block by VenueChain.psm1's strict rule; its probe imports the same module.
VENUE_CHAIN_MODULE = ROOT / "tools" / "profiling" / "dual-venue" / "VenueChain.psm1"
COMPARE_PREAMBLE = (f"Import-Module '{VENUE_CHAIN_MODULE}' -Function ConvertFrom-VenueSmokeHostLoad, Get-VenueReferenceHostLoadRefusal,"
                    " ConvertFrom-VenueSmokeResultText -DisableNameChecking\n")
# The shipped smoke writes before/after snapshots with collected = a real boolean (run-release-gui-smoke.ps1); a fully measured block carries them.
SNAPS = "before = [pscustomobject]@{ collected = $true }; after = [pscustomobject]@{ collected = $true }"
CUDA_AB_SCRIPT = ROOT / "tools" / "profiling" / "run-release-cuda-playback-ab.ps1"
COMPARE_MACHINE_PERF_SCRIPT = ROOT / "tools" / "profiling" / "compare-machine-perf.ps1"
P3_VALIDATION_SCRIPT = ROOT / "tools" / "profiling" / "run-ultramagnus-p3-validation.ps1"
CUDA_PROOF_SUMMARIZER_SCRIPT = ROOT / "tools" / "profiling" / "summarize-local-cuda-proof.ps1"
ULTRA_MAGNUS_PROFILE_SCRIPT = ROOT / "tools" / "profiling" / "run-ultra-magnus-profile.ps1"
LOCAL_GPU_CAPABILITY_SCRIPT = ROOT / "tools" / "profiling" / "run-local-gpu-capability.ps1"

PWSH = shutil.which("pwsh")
requires_pwsh = unittest.skipIf(PWSH is None, "pwsh is not on PATH")


def _extract_functions(script: Path, names: list[str]) -> str:
    """Regex-extract the exact `function <name> { ... }` bodies out of a real .ps1 file."""
    source = script.read_text(encoding="utf-8")
    chunks = []
    for name in names:
        pattern = re.compile(
            r"function " + re.escape(name) + r" \{.*?\n\}\r?\n", re.DOTALL
        )
        match = pattern.search(source)
        if not match:
            raise AssertionError(f"could not extract function {name} from {script}")
        chunks.append(match.group(0))
    return "\n".join(chunks)


def _extract_block(source: str, start_marker: str, end_marker: str, trailing_lines: int = 1) -> str:
    """Literal-text-search extraction of a script-body block that is NOT inside a named function
    (so _extract_functions above cannot find it) -- e.g. a guard clause living directly in a
    foreach loop at script scope. Finds `start_marker`, then `end_marker` after it, then includes
    `trailing_lines` more full lines past the line containing `end_marker` (to capture closing
    braces). round 6: used to make the P3-import and local-proof-summarizer host-load guards
    EXECUTABLE regression targets instead of only string-index-order checks -- a real operator/
    literal mutation in the live source changes what gets extracted and run, the same way it would
    change what actually runs in production."""
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    for _ in range(1 + trailing_lines):
        end = source.index("\n", end) + 1
    return source[start:end]


class _ProbeCase(unittest.TestCase):
    """Base: writes an extracted-function probe script into a temp dir and runs snippets against it."""

    script: Path
    functions: list[str]
    # VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-1 r6: lines run before the extracted functions (the comparator imports VenueChain.psm1's strict host-load rule at script top)
    preamble: str = ""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="hostload-probe-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.probe = self.tmp / "probe.ps1"
        self.probe.write_text(self.preamble + _extract_functions(self.script, self.functions), encoding="utf-8")

    def run_snippet(self, body: str) -> subprocess.CompletedProcess:
        script = self.tmp / "run.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f". '{self.probe}'\n" + body,
            encoding="utf-8",
        )
        return subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(script)],
            capture_output=True, text=True, creationflags=_NO_WINDOW,
        )


@requires_pwsh
class HostLoadSnapshotTests(_ProbeCase):
    """tools/profiling/run-release-gui-smoke.ps1's Get-HostLoadSnapshot and
    Get-HostLoadSystemTimes -- round 7's replacement for the round 4-6 per-process CPU
    accounting mechanism."""

    script = SMOKE_SCRIPT
    functions = ["Get-HostLoadSystemTimes", "Get-HostLoadSnapshot"]

    @unittest.skipUnless(
        os.name == "nt",
        "GetSystemTimes and Win32_Processor/Win32_OperatingSystem CIM collection are "
        "Windows-only; on other hosts collected/systemTimesCollected correctly report False "
        "(see HostLoadSnapshotUnknownWhereCollectionIsImpossibleTests below).",
    )
    def test_real_snapshot_collects_system_times_and_never_leaks_paths_or_command_lines(self) -> None:
        # round 7: unlike CIM (which a locked-down/sandboxed host can legitimately block), a plain
        # user-mode kernel32.dll syscall has no such failure mode, so this can assert
        # SYSTEM_TIMES_COLLECTED=True deterministically on any real Windows host, not merely
        # "either outcome" -- collected is now systemTimesCollected verbatim (see
        # Get-HostLoadSnapshot), so COLLECTED tracks it exactly.
        proc = self.run_snippet(
            "$s = Get-HostLoadSnapshot -TopProcessCount 6\n"
            "Write-Host \"COLLECTED=$($s.collected)\"\n"
            "Write-Host \"SYSTEM_TIMES_COLLECTED=$($s.systemTimesCollected)\"\n"
            "Write-Host \"IDLE=$($s.systemIdleSeconds) KERNEL=$($s.systemKernelSeconds) USER=$($s.systemUserSeconds)\"\n"
            "Write-Host \"TOP_COUNT=$($s.topCpuConsumers.Count)\"\n"
            "Write-Host \"TOP=$($s.topCpuConsumers -join '|')\"\n"
            "Write-Host \"ERROR=$($s.error)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("COLLECTED=True", proc.stdout)
        self.assertIn("SYSTEM_TIMES_COLLECTED=True", proc.stdout)
        self.assertRegex(proc.stdout, r"IDLE=\d")
        self.assertRegex(proc.stdout, r"KERNEL=\d")
        top_line = next(l for l in proc.stdout.splitlines() if l.startswith("TOP="))
        # Top consumers are recorded BY NAME ONLY: no path separators, no drive letters, no
        # command-line argument text -- this is host-load evidence, not process forensics, and it
        # no longer feeds the quiet/exceeded decision at all (see Get-HostLoadNonSubjectCpuLoadPercent).
        for name in [n for n in top_line[len("TOP="):].split("|") if n]:
            self.assertNotIn("\\", name)
            self.assertNotIn("/", name)
            self.assertNotIn(":", name)

    @unittest.skipUnless(
        os.name == "nt",
        "asserts SYSTEM_TIMES_COLLECTED=True, which needs the Windows-only GetSystemTimes syscall; "
        "on other hosts systemTimesCollected is correctly False.",
    )
    def test_a_throwing_get_process_during_top_consumers_gathering_does_not_block_collection(self) -> None:
        # round 7: the per-process ENUMERATION that used to drive the load decision itself
        # (rounds 4-6's totalCpuSeconds/processCpuSecondsById summing loop) is retired entirely --
        # the only remaining Get-Process call in this function gathers topCpuConsumers, purely
        # evidentiary, wrapped in its own try/catch. A throwing Get-Process here must NOT prevent
        # collection of the load-relevant data (systemTimesCollected, cpuLoadPercent).
        proc = self.run_snippet(
            "function Get-Process { param($ErrorAction, $Id) throw [System.InvalidOperationException]::new('synthetic') }\n"
            "$s = Get-HostLoadSnapshot -TopProcessCount 6\n"
            "Write-Host \"COLLECTED=$($s.collected)\"\n"
            "Write-Host \"SYSTEM_TIMES_COLLECTED=$($s.systemTimesCollected)\"\n"
            "Write-Host \"TOP_COUNT=$($s.topCpuConsumers.Count)\"\n"
            "Write-Host \"PROC_COUNT=$($s.processCount)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("COLLECTED=True", proc.stdout)
        self.assertIn("SYSTEM_TIMES_COLLECTED=True", proc.stdout)
        self.assertIn("TOP_COUNT=0", proc.stdout)
        self.assertIn("PROC_COUNT=", proc.stdout)
        proc_count_line = next(l for l in proc.stdout.splitlines() if l.startswith("PROC_COUNT="))
        self.assertEqual(proc_count_line, "PROC_COUNT=")

    @unittest.skipUnless(
        os.name == "nt",
        "asserts SYSTEM_TIMES_COLLECTED=True, which needs the Windows-only GetSystemTimes syscall; "
        "on other hosts systemTimesCollected is correctly False.",
    )
    def test_skip_evidence_collection_calls_neither_cim_nor_get_process(self) -> None:
        # round 8 (sol MAJOR item 2): -SkipEvidenceCollection must skip the ENTIRE evidence block
        # -- both CIM calls and Get-Process -- so a during-leg sample really is just the syscall
        # plus the subject-handle read. Overrides Get-CimInstance/Get-Process to record whether
        # they were invoked at all, rather than merely tolerating a throw from them.
        proc = self.run_snippet(
            "$script:cimCalled = $false\n"
            "$script:getProcessCalled = $false\n"
            "function Get-CimInstance { param($ClassName, $Filter, $ErrorAction, $OperationTimeoutSec) "
            "$script:cimCalled = $true; throw [System.InvalidOperationException]::new('must not be called') }\n"
            "function Get-Process { param($ErrorAction, $Id) "
            "$script:getProcessCalled = $true; throw [System.InvalidOperationException]::new('must not be called') }\n"
            "$s = Get-HostLoadSnapshot -TopProcessCount 6 -SkipEvidenceCollection -SubjectNotYetStarted\n"
            "Write-Host \"CIM_CALLED=$script:cimCalled\"\n"
            "Write-Host \"GET_PROCESS_CALLED=$script:getProcessCalled\"\n"
            "Write-Host \"COLLECTED=$($s.collected)\"\n"
            "Write-Host \"SYSTEM_TIMES_COLLECTED=$($s.systemTimesCollected)\"\n"
            "Write-Host \"ERROR=$($s.error)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("CIM_CALLED=False", proc.stdout)
        self.assertIn("GET_PROCESS_CALLED=False", proc.stdout)
        self.assertIn("SYSTEM_TIMES_COLLECTED=True", proc.stdout)
        self.assertIn("COLLECTED=True", proc.stdout)
        self.assertIn("ERROR=", proc.stdout)
        error_line = next(l for l in proc.stdout.splitlines() if l.startswith("ERROR="))
        self.assertEqual(error_line, "ERROR=", "skipping evidence collection must not itself be an error")

    def test_without_the_switch_evidence_collection_still_runs(self) -> None:
        # Regression guard on the guard: omitting -SkipEvidenceCollection (the before/after bracket
        # calls' shape) must still exercise the full evidence path.
        proc = self.run_snippet(
            "$script:cimCalled = $false\n"
            "$script:getProcessCalled = $false\n"
            "function Get-CimInstance { param($ClassName, $Filter, $ErrorAction, $OperationTimeoutSec) "
            "$script:cimCalled = $true; [pscustomobject]@{ LoadPercentage = 5; FreePhysicalMemory = 1024; "
            "TotalVisibleMemorySize = 2048 } }\n"
            "function Get-Process { param($ErrorAction, $Id) $script:getProcessCalled = $true; @() }\n"
            "$s = Get-HostLoadSnapshot -TopProcessCount 6 -SubjectNotYetStarted\n"
            "Write-Host \"CIM_CALLED=$script:cimCalled\"\n"
            "Write-Host \"GET_PROCESS_CALLED=$script:getProcessCalled\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("CIM_CALLED=True", proc.stdout)
        self.assertIn("GET_PROCESS_CALLED=True", proc.stdout)

    def test_subject_not_yet_started_records_zero_not_unreadable(self) -> None:
        # round 7: the BEFORE snapshot is captured before the subject process exists -- its
        # cumulative CPU time is definitionally zero, a distinct input from "unreadable" (which
        # must stay $null, see the next test).
        proc = self.run_snippet(
            "$s = Get-HostLoadSnapshot -TopProcessCount 1 -SubjectNotYetStarted\n"
            "Write-Host \"SUBJECT=$($s.subjectCpuSeconds)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("SUBJECT=0", proc.stdout)

    @unittest.skipUnless(os.name == "nt", "spawns a real Windows child process")
    def test_subject_process_handle_is_read_directly_and_reports_unreadable_once_disposed(self) -> None:
        # round 7: -SubjectProcess reads the CALLER's own held [System.Diagnostics.Process] handle
        # directly (never a fresh Get-Process -Id lookup, which would fail once the subject has
        # exited and dropped out of the live process table). Proven two ways in one process
        # lifecycle: (1) a live, real handle reads a real non-negative cumulative CPU value; (2)
        # once that SAME handle is explicitly Dispose()d, the next read throws and
        # subjectCpuSeconds must fall back to $null (third state), never a stale or zero value.
        proc = self.run_snippet(
            "$p = Start-Process -FilePath 'powershell.exe' "
            "-ArgumentList '-NoProfile','-Command','Start-Sleep -Milliseconds 300' -WindowStyle Hidden -PassThru\n"
            "Start-Sleep -Milliseconds 150\n"
            "$live = Get-HostLoadSnapshot -TopProcessCount 1 -SubjectProcess $p\n"
            "Write-Host \"LIVE_SUBJECT=$($live.subjectCpuSeconds)\"\n"
            "$p.WaitForExit()\n"
            "$p.Dispose()\n"
            "$disposed = Get-HostLoadSnapshot -TopProcessCount 1 -SubjectProcess $p\n"
            "Write-Host \"DISPOSED_SUBJECT=$($disposed.subjectCpuSeconds)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        live_line = next(l for l in proc.stdout.splitlines() if l.startswith("LIVE_SUBJECT="))
        self.assertNotEqual(live_line, "LIVE_SUBJECT=", "a live handle must report a real value, not null")
        self.assertIn("DISPOSED_SUBJECT=", proc.stdout)
        disposed_line = next(l for l in proc.stdout.splitlines() if l.startswith("DISPOSED_SUBJECT="))
        self.assertEqual(
            disposed_line, "DISPOSED_SUBJECT=",
            "an unreadable subject handle must stay null (unknown), never guess zero",
        )

    def test_snapshot_records_a_timestamp(self) -> None:
        proc = self.run_snippet(
            "$s = Get-HostLoadSnapshot -TopProcessCount 1\n"
            "Write-Host \"CAPTURED=$($s.capturedAtUtc)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"CAPTURED=\d{4}-\d{2}-\d{2}T")


@requires_pwsh
@unittest.skipUnless(
    os.name != "nt",
    "exercises the real Get-HostLoadSnapshot on a host where its Windows-only CIM "
    "collection is expected to fail -- on Windows itself collection normally succeeds, "
    "so that path is covered by HostLoadSnapshotTests above instead.",
)
class HostLoadSnapshotUnknownWhereCollectionIsImpossibleTests(_ProbeCase):
    """A host that cannot collect telemetry must classify as UNKNOWN/PROVISIONAL, never
    'quiet' -- this is the round-1 card's own third-state rule, exercised end to end
    against the real Get-HostLoadSnapshot + Get-HostLoadVerdict functions rather than a
    fixture standing in for a collection failure."""

    script = SMOKE_SCRIPT
    functions = [
        "Get-HostLoadSystemTimes",
        "Get-HostLoadSnapshot",
        "Get-HostLoadNonSubjectCpuLoadPercent",
        "Get-HostLoadVerdict",
    ]

    def test_real_uncollectable_snapshot_is_classified_unknown_and_provisional(self) -> None:
        proc = self.run_snippet(
            "$before = Get-HostLoadSnapshot -TopProcessCount 1 -SubjectNotYetStarted\n"
            "$after = Get-HostLoadSnapshot -TopProcessCount 1\n"
            "Write-Host \"BEFORE_COLLECTED=$($before.collected)\"\n"
            "Write-Host \"AFTER_COLLECTED=$($after.collected)\"\n"
            "$v = Get-HostLoadVerdict -Before $before -After $after -Bar 75\n"
            "Write-Host \"STATE=$($v.state) PROVISIONAL=$($v.provisional)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("BEFORE_COLLECTED=False", proc.stdout)
        self.assertIn("AFTER_COLLECTED=False", proc.stdout)
        self.assertIn("STATE=unknown PROVISIONAL=True", proc.stdout)
        self.assertNotIn("STATE=quiet", proc.stdout)
        self.assertNotIn("STATE=exceeded", proc.stdout)

    def test_uncollectable_snapshot_error_is_sanitized_not_raw_exception_text(self) -> None:
        # round 4 (sol minor, fable minor): $_.Exception.Message can embed a path, server name,
        # or namespace. Record only the exception's TYPE -- this test exercises the real catch
        # path (CIM is unavailable outside Windows) and asserts the exact narrow shape, which by
        # construction cannot contain a path separator or drive letter.
        proc = self.run_snippet(
            "$s = Get-HostLoadSnapshot -TopProcessCount 1\n"
            "Write-Host \"ERROR=$($s.error)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        error_line = next(l for l in proc.stdout.splitlines() if l.startswith("ERROR="))
        error_text = error_line[len("ERROR="):]
        self.assertRegex(error_text, r"^collection failed: [A-Za-z0-9_.]+$")
        self.assertNotIn("\\", error_text)
        self.assertNotIn("/", error_text)


@requires_pwsh
class HostLoadSnapshotPrivacyTests(_ProbeCase):
    """round 4 (sol minor, fable minor): the privacy test previously inspected only
    topCpuConsumers; sol's finding is that the FULLY SERIALIZED snapshot (where an unsanitized
    exception message would actually surface in a receipt) was never checked."""

    script = SMOKE_SCRIPT
    functions = ["Get-HostLoadSystemTimes", "Get-HostLoadSnapshot"]

    def test_full_serialized_snapshot_carries_no_path_or_drive_text(self) -> None:
        proc = self.run_snippet(
            "$s = Get-HostLoadSnapshot -TopProcessCount 6\n"
            "$s | ConvertTo-Json -Depth 6 -Compress\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        serialized = json.dumps(payload)
        self.assertNotIn("\\", serialized)
        self.assertNotIn("C:", serialized)
        self.assertNotIn("/", serialized)


@requires_pwsh
class WaitSystemCpuSettleSanitizationTests(_ProbeCase):
    """round 9 (sol/fable minor, item 3): round 8 sanitized this catch (type name only, matching
    every other sanitized catch in the file) but it was left with no executable regression test --
    both reviewers named it, and the pattern already existed for Get-HostLoadSnapshot's own catch
    at test_uncollectable_snapshot_error_is_sanitized_not_raw_exception_text above. Overrides
    Get-SystemCpuPercent to throw with a message that embeds a path/host identifier and asserts
    only the exception's bare type name reaches $result.failure."""

    script = SMOKE_SCRIPT
    functions = ["Get-SystemCpuPercent", "Wait-SystemCpuSettle"]

    def test_a_throwing_cpu_sample_records_type_name_only_not_raw_message_text(self) -> None:
        proc = self.run_snippet(
            "function Get-SystemCpuPercent {\n"
            "    throw [System.Management.Automation.RuntimeException]::new("
            "'CIM connection to \\\\BACHELOR\\root\\cimv2 failed')\n"
            "}\n"
            "$r = Wait-SystemCpuSettle -ThresholdPercent 5 -StableMs 100 -MaxMs 1000\n"
            "Write-Host \"FAILURE=$($r.failure)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        failure_line = next(l for l in proc.stdout.splitlines() if l.startswith("FAILURE="))
        failure_text = failure_line[len("FAILURE="):]
        self.assertRegex(failure_text, r"^[A-Za-z0-9_.]+$")
        self.assertNotIn("BACHELOR", failure_text)
        self.assertNotIn("\\", failure_text)
        self.assertNotIn("cimv2", failure_text)


@requires_pwsh
class GuiSmokeScreenshotCatchSanitizationTests(unittest.TestCase):
    """round 9 (sol/fable minor, item 3): the screenshot and window-screenshot validation catches
    (both sanitized at round 8 to record the exception's type name only) live in the main script
    body, not inside a named function, so _extract_functions cannot splice them -- extracted here by
    literal text search instead (same technique as the P3/summarizer guard-execution tests), then
    executed against a throwing Get-ScreenshotImageMetadata whose message embeds a path, proving
    only the type name -- never the message -- reaches $captureBindingFailures."""

    # round 9: NOT "if ($CaptureScreenshot) {" -- that literal string appears multiple times
    # earlier in this same script (argument-building sections), so a naive .index() search would
    # splice the wrong block entirely. These inner-if markers are unique.
    SCREENSHOT_START_MARKER = "if (-not (Test-Path -LiteralPath $screenshotPath)) {"
    SCREENSHOT_END_MARKER = 'GUI smoke screenshot validation failed: $($_.Exception.GetType().Name)"'
    WINDOW_START_MARKER = "if (-not (Test-Path -LiteralPath $windowScreenshotPath)) {"
    WINDOW_END_MARKER = 'GUI smoke window-screenshot validation failed: $($_.Exception.GetType().Name)"'

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="hostload-screenshot-catch-probe-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.source = SMOKE_SCRIPT.read_text(encoding="utf-8")

    def _run(self, script_text: str) -> subprocess.CompletedProcess:
        script = self.tmp / "run.ps1"
        script.write_text(script_text, encoding="utf-8")
        return subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(script)],
            capture_output=True, text=True, creationflags=_NO_WINDOW,
        )

    def test_a_throwing_screenshot_metadata_read_records_type_name_only(self) -> None:
        guard_block = _extract_block(
            self.source, self.SCREENSHOT_START_MARKER, self.SCREENSHOT_END_MARKER, trailing_lines=2)
        screenshot_path = self.tmp / "shot.png"
        screenshot_path.write_bytes(b"not a real png, just needs to exist")
        proc = self._run(
            "$ErrorActionPreference = 'Stop'\n"
            "function Get-ScreenshotImageMetadata {\n"
            "    param($Path)\n"
            "    throw [System.InvalidOperationException]::new('failed reading C:\\Users\\secret\\footage.dat')\n"
            "}\n"
            "function Get-ScreenshotColorArtifactScan { param($Path) $null }\n"
            "$CaptureScreenshot = $true\n"
            f"$screenshotPath = '{screenshot_path}'\n"
            "$ScreenshotDelayMs = 0; $ScreenshotWindowWaitMs = 0; $ScreenshotCaptureTimeoutMs = 0\n"
            "$screenshotCapture = $null\n"
            "$colorArtifactScan = $null\n"
            "$captureBindingFailures = @()\n"
            f"{guard_block}\n"
            "Write-Host \"FAILURES=$($captureBindingFailures -join '|')\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        failures_line = next(l for l in proc.stdout.splitlines() if l.startswith("FAILURES="))
        self.assertIn("InvalidOperationException", failures_line)
        self.assertNotIn("secret", failures_line)
        self.assertNotIn("\\", failures_line)
        self.assertNotIn("footage.dat", failures_line)

    def test_a_throwing_window_screenshot_metadata_read_records_type_name_only(self) -> None:
        guard_block = _extract_block(
            self.source, self.WINDOW_START_MARKER, self.WINDOW_END_MARKER, trailing_lines=2)
        window_screenshot_path = self.tmp / "window-shot.png"
        window_screenshot_path.write_bytes(b"not a real png, just needs to exist")
        proc = self._run(
            "$ErrorActionPreference = 'Stop'\n"
            "function Get-ScreenshotImageMetadata {\n"
            "    param($Path)\n"
            "    throw [System.InvalidOperationException]::new('failed reading C:\\Users\\secret\\footage.dat')\n"
            "}\n"
            f"$windowScreenshotPath = '{window_screenshot_path}'\n"
            "$fpsStatusCropPath = $null\n"
            "$windowScreenshotCapture = $null\n"
            "$fpsStatusCropCapture = $null\n"
            "$captureBindingFailures = @()\n"
            f"{guard_block}\n"
            "Write-Host \"FAILURES=$($captureBindingFailures -join '|')\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        failures_line = next(l for l in proc.stdout.splitlines() if l.startswith("FAILURES="))
        self.assertIn("InvalidOperationException", failures_line)
        self.assertNotIn("secret", failures_line)
        self.assertNotIn("\\", failures_line)
        self.assertNotIn("footage.dat", failures_line)


@requires_pwsh
class HostLoadVerdictTests(_ProbeCase):
    """tools/profiling/run-release-gui-smoke.ps1's Get-HostLoadVerdict -- the bar and the three
    outcomes (quiet / exceeded / unknown), never two.

    round 7 (both keys BLOCKER -- replace per-process accounting BY CONSTRUCTION): fixtures are
    now Get-HostLoadSnapshot-shaped (systemIdleSeconds/systemKernelSeconds/systemUserSeconds/
    subjectCpuSeconds), not bare cpuLoadPercent numbers -- Get-HostLoadVerdict computes each
    consecutive pair's non-subject load ITSELF now (via Get-HostLoadNonSubjectCpuLoadPercent),
    rather than reading a pre-attached property or falling back to a raw cpuLoadPercent number
    (that raw fallback is exactly what rounds 4-6 relied on and what let an honest-looking LOW
    number actually be an INCOMPLETE one)."""

    script = SMOKE_SCRIPT
    functions = ["Get-HostLoadNonSubjectCpuLoadPercent", "Get-HostLoadVerdict"]

    PROCESSOR_COUNT = 4

    @staticmethod
    def _snap(
        offset_seconds: float,
        kernel_seconds: float,
        subject_seconds,
        idle_seconds: float = 0.0,
        user_seconds: float = 0.0,
        collected: bool = True,
        system_times_collected: bool = True,
        system_times_offset_seconds: float | None = None,
    ) -> str:
        # subject_seconds=None means untracked/unreadable (the third-state input this round
        # exists to refuse); pass 0.0 explicitly for "not yet started", a legitimate zero.
        # round 8 (item 1 -- window misalignment): systemTimesCapturedAtUtc defaults to the same
        # instant as the general capturedAtUtc (matching the production Get-HostLoadSnapshot
        # contract, where they diverge only by pre-syscall collection latency) -- pass
        # system_times_offset_seconds explicitly to model that divergence, per
        # HostLoadWindowAlignmentTests below.
        collected_literal = "$true" if collected else "$false"
        stc_literal = "$true" if system_times_collected else "$false"
        subject_literal = "$null" if subject_seconds is None else repr(float(subject_seconds))
        stc_offset = offset_seconds if system_times_offset_seconds is None else system_times_offset_seconds
        return (
            "[pscustomobject]@{ "
            f"collected = {collected_literal}; "
            f"capturedAtUtc = (Get-Date '2026-01-01T00:00:00Z').AddSeconds({offset_seconds}).ToString('o'); "
            f"systemTimesCollected = {stc_literal}; "
            f"systemTimesCapturedAtUtc = (Get-Date '2026-01-01T00:00:00Z').AddSeconds({stc_offset}).ToString('o'); "
            f"systemIdleSeconds = {idle_seconds!r}; systemKernelSeconds = {kernel_seconds!r}; "
            f"systemUserSeconds = {user_seconds!r}; subjectCpuSeconds = {subject_literal}; "
            "cpuLoadPercent = 0.0 }"
        )

    def _flat(self, offset_seconds: float) -> str:
        """A snapshot whose cumulative counters never change relative to any other _flat() call
        -- any two of these always produce a 0% ("quiet") interval, regardless of elapsed time."""
        return self._snap(offset_seconds, kernel_seconds=0.0, subject_seconds=0.0)

    def _verdict(
        self, before: str, after: str, during: str = "@()", bar: float = 75,
        sample_interval_ms=None, observed_gap_ms=None,
    ) -> subprocess.CompletedProcess:
        extra = f" -ProcessorCount {self.PROCESSOR_COUNT}"
        if sample_interval_ms is not None:
            extra += f" -SampleIntervalMs {sample_interval_ms}"
        if observed_gap_ms is not None:
            extra += f" -ObservedMaxSampleGapMs {observed_gap_ms}"
        return self.run_snippet(
            f"$before = {before}\n"
            f"$after = {after}\n"
            f"$during = {during}\n"
            f"$v = Get-HostLoadVerdict -Before $before -After $after -During $during -Bar {bar}{extra}\n"
            "Write-Host \"STATE=$($v.state) PROVISIONAL=$($v.provisional) "
            "MAX=$($v.maxCpuLoadPercent) REASON=$($v.reason)\"\n"
        )

    def test_quiet_host_is_not_provisional(self) -> None:
        proc = self._verdict(self._flat(0), self._flat(4))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STATE=quiet PROVISIONAL=False", proc.stdout)

    def test_fixture_mirroring_the_measured_96_percent_sample_exceeds_the_bar(self) -> None:
        # 2026-09-22T17:55Z evidence: CPU_LOAD_PCT=96 on BACHELOR turned a 4.8fps reference into
        # 1.2fps. The declared bar must fail this sample -- that is the card's own acceptance
        # test. 4 elapsed seconds * 4 processors = 16 processor-seconds capacity; 15.36 busy
        # processor-seconds is 96% of that, all non-subject.
        before = self._snap(0, kernel_seconds=0.0, subject_seconds=0.0)
        after = self._snap(4, kernel_seconds=15.36, subject_seconds=0.0)
        proc = self._verdict(before, after, bar=75)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STATE=exceeded PROVISIONAL=True", proc.stdout)
        self.assertIn("MAX=96", proc.stdout)
        self.assertIn("is not a property of the build", proc.stdout)

    def test_unknown_telemetry_is_provisional_never_quiet(self) -> None:
        # RESUME.md STEP 4: "when you add a check, name its three outcomes." Telemetry that could
        # not be collected must never silently read as a passing "quiet" host.
        before = self._flat(0)
        after = self._snap(4, kernel_seconds=0.0, subject_seconds=None, collected=False, system_times_collected=False)
        proc = self._verdict(before, after)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STATE=unknown PROVISIONAL=True", proc.stdout)
        self.assertNotIn("STATE=quiet", proc.stdout)

    def test_both_snapshots_unknown_is_still_unknown_not_exceeded(self) -> None:
        snap = self._snap(0, kernel_seconds=0.0, subject_seconds=None, collected=False, system_times_collected=False)
        proc = self._verdict(snap, snap)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STATE=unknown PROVISIONAL=True", proc.stdout)

    def test_mid_leg_burst_is_missed_by_bracketing_alone_but_caught_by_sampling(self) -> None:
        # PLAYBACK-MEASURE-HOST-LOAD-GATE-1 round 3: this is the exact case in point. A burst
        # confined to the interior is invisible to bracketing alone (a single [before,after]
        # interval averages it away over the whole 24s span: 15.36 busy processor-seconds over
        # 24s*4proc=96 capacity is only 16%) but must be caught once interior samples split that
        # span into sub-intervals (round 3's behaviour). The rule is PEAK across every measured
        # INTERVAL, not average or sustained-for-N: one 96% interval marks the whole leg
        # exceeded/provisional even though the two flanking intervals are both quiet.
        before = self._snap(0, kernel_seconds=0.0, subject_seconds=0.0)
        during1 = self._snap(10, kernel_seconds=0.0, subject_seconds=0.0)  # [0,10]: quiet
        during2 = self._snap(14, kernel_seconds=15.36, subject_seconds=0.0)  # [10,14]: 96% burst
        after = self._snap(24, kernel_seconds=15.36, subject_seconds=0.0)  # [14,24]: quiet
        during_literal = f"@({during1}, {during2})"
        proc = self.run_snippet(
            f"$before = {before}\n"
            f"$after = {after}\n"
            f"$during = {during_literal}\n"
            f"$bracketedOnly = Get-HostLoadVerdict -Before $before -After $after -Bar 75 "
            f"-ProcessorCount {self.PROCESSOR_COUNT}\n"
            "Write-Host \"BRACKETED_STATE=$($bracketedOnly.state) "
            "BRACKETED_PROVISIONAL=$($bracketedOnly.provisional) BRACKETED_MAX=$($bracketedOnly.maxCpuLoadPercent)\"\n"
            f"$sampled = Get-HostLoadVerdict -Before $before -After $after -During $during -Bar 75 "
            f"-ProcessorCount {self.PROCESSOR_COUNT}\n"
            "Write-Host \"SAMPLED_STATE=$($sampled.state) "
            "SAMPLED_PROVISIONAL=$($sampled.provisional) SAMPLED_MAX=$($sampled.maxCpuLoadPercent)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("BRACKETED_STATE=quiet BRACKETED_PROVISIONAL=False", proc.stdout)
        self.assertIn("BRACKETED_MAX=16", proc.stdout)
        self.assertIn("SAMPLED_STATE=exceeded SAMPLED_PROVISIONAL=True", proc.stdout)
        self.assertIn("SAMPLED_MAX=96", proc.stdout)

    def test_an_uncollected_interior_sample_is_unknown_not_silently_dropped(self) -> None:
        before = self._flat(0)
        after = self._flat(4)
        during_sample = self._snap(2, kernel_seconds=0.0, subject_seconds=None, collected=False, system_times_collected=False)
        proc = self._verdict(before, after, during=f"@({during_sample})")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STATE=unknown PROVISIONAL=True", proc.stdout)

    def test_sampling_disabled_forces_unknown_even_when_bracket_is_quiet(self) -> None:
        # round 4 (sol BLOCKER), repro case 1: "Run a leg with HostLoadSampleIntervalMs=0 ...
        # while before/after snapshots remain below 75%; ... Get-HostLoadVerdict returns
        # quiet/provisional=false." Declaring SampleIntervalMs=0 must now force unknown/provisional
        # even though bracketing alone reads quiet.
        proc = self._verdict(self._flat(0), self._flat(4), sample_interval_ms=0)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STATE=unknown PROVISIONAL=True", proc.stdout)
        self.assertIn("sampling was disabled", proc.stdout)

    def test_sampling_enabled_but_zero_interior_samples_forces_unknown(self) -> None:
        # round 4 (sol BLOCKER), repro case 2: a leg short enough that not even one interior tick
        # occurred never rose above bracket-only coverage either, even though sampling was
        # nominally enabled.
        proc = self._verdict(self._flat(0), self._flat(4), sample_interval_ms=4000)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STATE=unknown PROVISIONAL=True", proc.stdout)
        self.assertIn("collected zero samples", proc.stdout)

    def test_sampling_enabled_with_interior_samples_stays_quiet_when_actually_quiet(self) -> None:
        # Regression guard: once sampling is enabled AND it actually produced interior coverage,
        # a genuinely quiet leg must still read quiet -- the new coverage rule must not turn every
        # leg provisional.
        during = f"@({self._flat(2)})"
        proc = self._verdict(self._flat(0), self._flat(4), during=during, sample_interval_ms=4000)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STATE=quiet PROVISIONAL=False", proc.stdout)

    def test_sampling_enabled_still_catches_a_covered_burst(self) -> None:
        # Regression guard: the coverage rule must not weaken the round-3 peak-of-intervals catch
        # for a burst that WAS covered by an interior tick. 2s * 4proc = 8 capacity; 15.36 busy
        # processor-seconds vastly exceeds it (clamped to 100%), well past the 75% bar.
        before = self._snap(0, kernel_seconds=0.0, subject_seconds=0.0)
        during = f"@({self._snap(2, kernel_seconds=15.36, subject_seconds=0.0)})"
        after = self._snap(4, kernel_seconds=15.36, subject_seconds=0.0)
        proc = self._verdict(before, after, during=during, sample_interval_ms=4000)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STATE=exceeded PROVISIONAL=True", proc.stdout)

    def test_callers_that_omit_sample_interval_ms_are_unaffected(self) -> None:
        # Backward compatibility: every caller/fixture that predates round 4 never passes
        # -SampleIntervalMs at all; the new coverage rule must stay inert for them, preserving
        # bracket-only round-2/3 semantics exactly.
        proc = self._verdict(self._flat(0), self._flat(4), sample_interval_ms=None)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STATE=quiet PROVISIONAL=False", proc.stdout)

    def test_observed_sample_gap_far_exceeding_declared_cadence_forces_unknown(self) -> None:
        # round 5 (sol MAJOR), exact repro: a 4000ms wait followed by a 3000ms callback puts
        # ~7000ms between snapshot starts while the declared cadence was 4000ms -- a 1.75x
        # overrun. Coverage cannot certify quiet when the caller's own disclosed bound was not
        # honored.
        during = f"@({self._flat(2)})"
        proc = self._verdict(
            self._flat(0), self._flat(4), during=during, sample_interval_ms=4000, observed_gap_ms=7000
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STATE=unknown PROVISIONAL=True", proc.stdout)
        self.assertIn("did not actually meet the declared cadence", proc.stdout)

    def test_observed_sample_gap_within_tolerance_of_declared_cadence_stays_quiet(self) -> None:
        # Regression guard: ordinary scheduling jitter (well under the 1.5x tolerance) must not
        # turn every real leg provisional.
        during = f"@({self._flat(2)})"
        proc = self._verdict(
            self._flat(0), self._flat(4), during=during, sample_interval_ms=4000, observed_gap_ms=4200
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STATE=quiet PROVISIONAL=False", proc.stdout)

    def test_spacing_tolerance_is_pinned_at_one_point_five_x_not_looser(self) -> None:
        # round 6 (sol MAJOR, exact repro): "Pin the 1.5x spacing tolerance with a test that fails
        # if it is loosened (sol: 1.5 -> 1.7 passed)." Neither test above pins the MULTIPLIER
        # itself -- 7000/4000=1.75x is caught by any tolerance below 1.75x (including a loosened
        # 1.7x), and 4200/4000=1.05x passes under any reasonable tolerance at all, so a mutation
        # widening 1.5x to 1.7x would leave both existing tests green. This uses a gap strictly
        # BETWEEN the two: 6400/4000 = 1.6x declared cadence. Under the correct 1.5x tolerance
        # (bar: 6000ms), 6400ms must already read unknown; under a loosened 1.7x tolerance (bar:
        # 6800ms), it would incorrectly still read quiet.
        during = f"@({self._flat(2)})"
        proc = self._verdict(
            self._flat(0), self._flat(4), during=during, sample_interval_ms=4000, observed_gap_ms=6400
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STATE=unknown PROVISIONAL=True", proc.stdout)

    def test_missing_observed_sample_gap_is_backward_compatible(self) -> None:
        # Every test above this one, and every caller written before round 5, never passes
        # -ObservedMaxSampleGapMs at all -- the new spacing rule must stay inert for them.
        during = f"@({self._flat(2)})"
        proc = self._verdict(
            self._flat(0), self._flat(4), during=during, sample_interval_ms=4000, observed_gap_ms=None
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STATE=quiet PROVISIONAL=False", proc.stdout)

    def test_cpu_heavy_subject_on_a_quiet_host_is_not_provisional(self) -> None:
        # round 4 (fable minor), required test 1: "a CPU-heavy SUBJECT on a quiet host must NOT
        # be provisional." 4s*4proc=16 capacity; busy=14 (87.5% raw) but the subject itself
        # accounts for 13 of that 14 -- non-subject is only (14-13)/16=6.25%, must read quiet.
        before = self._snap(0, kernel_seconds=0.0, subject_seconds=0.0)
        during = f"@({self._snap(4, kernel_seconds=14.0, subject_seconds=13.0)})"
        after = self._snap(8, kernel_seconds=14.0, subject_seconds=13.0)  # no further change: quiet
        proc = self._verdict(before, after, during=during, sample_interval_ms=4000)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STATE=quiet PROVISIONAL=False", proc.stdout)

    def test_busy_host_is_still_provisional_even_with_a_quiet_subject(self) -> None:
        # round 4 (fable minor), required test 2: "a busy host must be [provisional]." Same 14
        # busy processor-seconds, but the subject itself only accounts for 1 of them -- non-subject
        # is (14-1)/16=81.25%, must exceed the 75% bar despite the subject being quiet.
        before = self._snap(0, kernel_seconds=0.0, subject_seconds=0.0)
        during = f"@({self._snap(4, kernel_seconds=14.0, subject_seconds=1.0)})"
        after = self._snap(8, kernel_seconds=14.0, subject_seconds=1.0)  # no further change: quiet
        proc = self._verdict(before, after, during=during, sample_interval_ms=4000)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STATE=exceeded PROVISIONAL=True", proc.stdout)


@requires_pwsh
class HostLoadNonSubjectCpuLoadPercentTests(_ProbeCase):
    """tools/profiling/run-release-gui-smoke.ps1's Get-HostLoadNonSubjectCpuLoadPercent -- round 7
    (both keys BLOCKER) replaces the round 4-6 per-process-sum mechanism with system-wide busy
    time (Win32 GetSystemTimes: kernel INCLUDES idle, so busy = (kernel-idle)+user) minus the
    subject's own CPU time, both as cumulative deltas over the identical window. There is no
    process list anywhere in this function's inputs, so throwing per-process getters, a process
    present in only one snapshot, and PID reuse are not merely fixed here -- they are not
    expressible as inputs to this function at all."""

    script = SMOKE_SCRIPT
    functions = ["Get-HostLoadNonSubjectCpuLoadPercent"]

    @staticmethod
    def _snap(
        offset_seconds: float,
        idle_seconds: float,
        kernel_seconds: float,
        user_seconds: float,
        subject_seconds,
        system_times_collected: bool = True,
        system_times_offset_seconds: float | None = None,
    ) -> str:
        # round 8 (item 1): systemTimesCapturedAtUtc defaults to offset_seconds (the pre-round-8
        # behaviour, where the two timestamps coincided) -- pass system_times_offset_seconds
        # explicitly to model the misalignment this round fixes.
        stc_literal = "$true" if system_times_collected else "$false"
        subject_literal = "$null" if subject_seconds is None else repr(float(subject_seconds))
        stc_offset = offset_seconds if system_times_offset_seconds is None else system_times_offset_seconds
        return (
            "[pscustomobject]@{ "
            f"capturedAtUtc = (Get-Date '2026-01-01T00:00:00Z').AddSeconds({offset_seconds}).ToString('o'); "
            f"systemTimesCollected = {stc_literal}; "
            f"systemTimesCapturedAtUtc = (Get-Date '2026-01-01T00:00:00Z').AddSeconds({stc_offset}).ToString('o'); "
            f"systemIdleSeconds = {idle_seconds!r}; systemKernelSeconds = {kernel_seconds!r}; "
            f"systemUserSeconds = {user_seconds!r}; subjectCpuSeconds = {subject_literal} }}"
        )

    def _percent(self, current: str, previous: str, processor_count: int = 4) -> subprocess.CompletedProcess:
        return self.run_snippet(
            f"$current = {current}\n"
            f"$previous = {previous}\n"
            f"$p = Get-HostLoadNonSubjectCpuLoadPercent -CurrentSnapshot $current "
            f"-PreviousSnapshot $previous -ProcessorCount {processor_count}\n"
            "Write-Host \"PERCENT=$(if ($null -eq $p) { 'NULL' } else { $p })\"\n"
        )

    def test_subject_consuming_all_the_load_leaves_non_subject_near_zero(self) -> None:
        # Over 4 elapsed seconds on a 4-core host, capacity is 16 processor-seconds; the subject
        # alone accounts for all 16 busy processor-seconds (idle/user held flat), so non-subject
        # must read ~0%.
        previous = self._snap(0, idle_seconds=0.0, kernel_seconds=0.0, user_seconds=0.0, subject_seconds=0.0)
        current = self._snap(4, idle_seconds=0.0, kernel_seconds=16.0, user_seconds=0.0, subject_seconds=16.0)
        proc = self._percent(current, previous, processor_count=4)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PERCENT=0", proc.stdout)

    def test_missing_system_times_on_current_snapshot_returns_unknown_not_raw(self) -> None:
        # round 7: there is no raw cpuLoadPercent fallback anywhere in this function -- a snapshot
        # whose system times could not be collected (GetSystemTimes failed) makes the WHOLE
        # interval unknown, per the brief: "it must not feed the quiet decision on its own."
        previous = self._snap(0, idle_seconds=0.0, kernel_seconds=0.0, user_seconds=0.0, subject_seconds=0.0)
        current = self._snap(
            4, idle_seconds=0.0, kernel_seconds=0.0, user_seconds=0.0, subject_seconds=0.0,
            system_times_collected=False,
        )
        proc = self._percent(current, previous)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PERCENT=NULL", proc.stdout)

    def test_missing_system_times_on_previous_snapshot_also_returns_unknown(self) -> None:
        previous = self._snap(
            0, idle_seconds=0.0, kernel_seconds=0.0, user_seconds=0.0, subject_seconds=0.0,
            system_times_collected=False,
        )
        current = self._snap(4, idle_seconds=0.0, kernel_seconds=16.0, user_seconds=0.0, subject_seconds=0.0)
        proc = self._percent(current, previous)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PERCENT=NULL", proc.stdout)

    def test_unreadable_subject_on_current_snapshot_returns_unknown_not_zero(self) -> None:
        # round 7 (third state): the subject having exited mid-interval without a final reading
        # must never silently read as "0 subject CPU used" (which would UNDERCOUNT the subject and
        # OVERCOUNT non-subject load) -- this is the exact contract Get-HostLoadSnapshot's own
        # $null-on-unreadable default is designed to trip here.
        previous = self._snap(0, idle_seconds=0.0, kernel_seconds=0.0, user_seconds=0.0, subject_seconds=0.0)
        current = self._snap(4, idle_seconds=0.0, kernel_seconds=16.0, user_seconds=0.0, subject_seconds=None)
        proc = self._percent(current, previous)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PERCENT=NULL", proc.stdout)

    def test_unreadable_subject_on_previous_snapshot_also_returns_unknown(self) -> None:
        previous = self._snap(0, idle_seconds=0.0, kernel_seconds=0.0, user_seconds=0.0, subject_seconds=None)
        current = self._snap(4, idle_seconds=0.0, kernel_seconds=16.0, user_seconds=0.0, subject_seconds=0.0)
        proc = self._percent(current, previous)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PERCENT=NULL", proc.stdout)

    def test_non_positive_elapsed_time_returns_unknown(self) -> None:
        previous = self._snap(0, idle_seconds=0.0, kernel_seconds=0.0, user_seconds=0.0, subject_seconds=0.0)
        current = self._snap(0, idle_seconds=0.0, kernel_seconds=4.0, user_seconds=0.0, subject_seconds=0.0)
        proc = self._percent(current, previous)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PERCENT=NULL", proc.stdout)

    def test_kernel_time_includes_idle_and_is_subtracted_out(self) -> None:
        # 4s * 8 processors = 32 processor-seconds capacity. Idle climbs by 20 (mostly-idle host);
        # kernel (which INCLUDES idle) climbs by 24; user climbs by 4. Busy = (24-20)+4 = 8. The
        # subject contributed 0. nonSubject = 8/32*100 = 25%. A mutation that forgot to subtract
        # idle from kernel would instead compute (24+4)/32=87.5%, so this pins that subtraction.
        previous = self._snap(0, idle_seconds=0.0, kernel_seconds=0.0, user_seconds=0.0, subject_seconds=0.0)
        current = self._snap(4, idle_seconds=20.0, kernel_seconds=24.0, user_seconds=4.0, subject_seconds=0.0)
        proc = self._percent(current, previous, processor_count=8)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PERCENT=25", proc.stdout)

    def test_a_process_that_exited_between_snapshots_is_still_correctly_accounted(self) -> None:
        # round 7: this is what round 6's "a process that exited between snapshots cannot cancel a
        # survivor's usage" repro becomes under the new mechanism -- it is no longer a special case
        # requiring its own guard, because system-wide counters are monotonic OS-maintained totals,
        # never rebuilt from a per-process list that could lose an exited process's contribution.
        # 80% background (from a mix of surviving and now-exited processes, indistinguishable and
        # irrelevant at the system-wide level) plus 20% subject over 4s*4proc=16 capacity: busy=16,
        # subject=3.2 -> nonSubject=(16-3.2)/16*100=80%, correctly exceeding a 75% bar.
        previous = self._snap(0, idle_seconds=0.0, kernel_seconds=0.0, user_seconds=0.0, subject_seconds=0.0)
        current = self._snap(4, idle_seconds=0.0, kernel_seconds=16.0, user_seconds=0.0, subject_seconds=3.2)
        proc = self._percent(current, previous, processor_count=4)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PERCENT=80", proc.stdout)


@requires_pwsh
class HostLoadWindowAlignmentTests(_ProbeCase):
    """round 8 (sol BLOCKER = fable minor 1): elapsed time must be taken from the SAME instants as
    the two GetSystemTimes calls it is dividing a busy-delta by, never from the caller's general
    capturedAtUtc (stamped before that snapshot's own CIM/Get-Process evidence work). Reproduces
    sol's exact repro against the real Get-HostLoadNonSubjectCpuLoadPercent: a continuously
    100%-busy 4-logical-CPU host whose PREVIOUS snapshot's evidence collection was slow (its
    systemTimesCapturedAtUtc lands well after its general capturedAtUtc), while the CURRENT
    snapshot's collection was fast (the two coincide). Before this round's fix, dividing by
    (current.capturedAtUtc - previous.capturedAtUtc) -- the wider, timestamp-to-timestamp window --
    diluted a genuinely 100%-busy interval below the 75% bar; after the fix, dividing by
    (current.systemTimesCapturedAtUtc - previous.systemTimesCapturedAtUtc) -- the narrower,
    syscall-to-syscall window the busy-delta actually spans -- reads the true 100%."""

    script = SMOKE_SCRIPT
    functions = ["Get-HostLoadNonSubjectCpuLoadPercent"]

    @staticmethod
    def _snap(
        general_offset_seconds: float,
        system_times_offset_seconds: float,
        kernel_seconds: float,
        subject_seconds: float = 0.0,
    ) -> str:
        subject_literal = repr(float(subject_seconds))
        return (
            "[pscustomobject]@{ "
            f"capturedAtUtc = (Get-Date '2026-01-01T00:00:00Z').AddSeconds({general_offset_seconds}).ToString('o'); "
            "systemTimesCollected = $true; "
            f"systemTimesCapturedAtUtc = (Get-Date '2026-01-01T00:00:00Z').AddSeconds({system_times_offset_seconds}).ToString('o'); "
            f"systemIdleSeconds = 0.0; systemKernelSeconds = {kernel_seconds!r}; systemUserSeconds = 0.0; "
            f"subjectCpuSeconds = {subject_literal} }}"
        )

    def _percent(self, current: str, previous: str, processor_count: int = 4) -> subprocess.CompletedProcess:
        return self.run_snippet(
            f"$current = {current}\n"
            f"$previous = {previous}\n"
            f"$p = Get-HostLoadNonSubjectCpuLoadPercent -CurrentSnapshot $current "
            f"-PreviousSnapshot $previous -ProcessorCount {processor_count}\n"
            "Write-Host \"PERCENT=$(if ($null -eq $p) { 'NULL' } else { $p })\"\n"
        )

    def test_sols_exact_repro_slow_previous_collection_no_longer_dilutes_a_fully_busy_interval(self) -> None:
        # sol's repro: previous snapshot's evidence collection (CIM + Get-Process, ahead of its
        # GetSystemTimes call) took long enough that its systemTimesCapturedAtUtc lands 1.112s
        # after its general capturedAtUtc (mirroring the 2.127s-vs-1.015s gap sol measured); the
        # current snapshot's collection was fast, so its two timestamps coincide. The syscalls
        # genuinely bracket a 1.015s, fully-busy (4 logical CPUs) window: busy = 1.015*4 = 4.06
        # processor-seconds. Pre-round-8, dividing by the general-timestamp gap (2.127s) yields
        # 4.06/(2.127*4)*100 = 47.72% (quiet); post-round-8, dividing by the systemTimesCapturedAtUtc
        # gap (1.015s) yields 4.06/(1.015*4)*100 = 100% (correctly exceeded).
        previous = self._snap(
            general_offset_seconds=0.0, system_times_offset_seconds=1.112, kernel_seconds=0.0)
        current = self._snap(
            general_offset_seconds=2.127, system_times_offset_seconds=2.127, kernel_seconds=4.06)
        proc = self._percent(current, previous, processor_count=4)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PERCENT=100", proc.stdout)
        self.assertNotIn("PERCENT=47", proc.stdout)

    def test_falling_load_shape_is_not_diluted_either(self) -> None:
        # fable's disclosed residual direction: a falling-load pattern where the PREVIOUS sample's
        # collection was slow. Busy is 3.0 processor-seconds over a true 1.0s syscall-to-syscall
        # window on a 4-core host = 75% exactly at the bar; the general-timestamp window is wider
        # (3.0s), which would previously have diluted this to 25% (quiet) instead of exceeded.
        previous = self._snap(
            general_offset_seconds=0.0, system_times_offset_seconds=2.0, kernel_seconds=0.0)
        current = self._snap(
            general_offset_seconds=3.0, system_times_offset_seconds=3.0, kernel_seconds=3.0)
        proc = self._percent(current, previous, processor_count=4)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PERCENT=75", proc.stdout)

    def test_missing_system_times_captured_at_utc_returns_unknown_not_raw(self) -> None:
        previous = self._snap(0.0, 0.0, kernel_seconds=0.0)
        current_missing = self.run_snippet(
            f"$previous = {previous}\n"
            "$current = [pscustomobject]@{ capturedAtUtc = (Get-Date '2026-01-01T00:00:04Z').ToString('o'); "
            "systemTimesCollected = $true; systemTimesCapturedAtUtc = $null; systemIdleSeconds = 0.0; "
            "systemKernelSeconds = 4.0; systemUserSeconds = 0.0; subjectCpuSeconds = 0.0 }\n"
            "$p = Get-HostLoadNonSubjectCpuLoadPercent -CurrentSnapshot $current "
            "-PreviousSnapshot $previous -ProcessorCount 4\n"
            "Write-Host \"PERCENT=$(if ($null -eq $p) { 'NULL' } else { $p })\"\n"
        )
        self.assertEqual(current_missing.returncode, 0, current_missing.stdout + current_missing.stderr)
        self.assertIn("PERCENT=NULL", current_missing.stdout)


@requires_pwsh
class HostLoadSnapshotProducerAlignmentTests(_ProbeCase):
    """round 9 (sol BLOCKER item 1 + fable major): HostLoadWindowAlignmentTests above proves the
    CONSUMER side (Get-HostLoadNonSubjectCpuLoadPercent) reads the right fields correctly, but --
    exactly as fable's round-8 review found -- every one of its fixtures is a synthetic
    [pscustomobject] literal; none of them ever calls the real Get-HostLoadSnapshot or
    Get-HostLoadSystemTimes, so a producer-side regression (e.g. reverting
    Get-HostLoadSnapshot to independently re-stamp capturedAtUtc, or to read the subject's CPU
    itself instead of taking it from Get-HostLoadSystemTimes) would pass every existing test. These
    tests EXECUTE the real producer chain instead."""

    script = SMOKE_SCRIPT
    functions = ["Get-HostLoadSystemTimes", "Get-HostLoadSnapshot"]

    # CPU-seconds the spinner must burn inside EACH mocked CIM call of the real-subject test below (two
    # calls per snapshot), and the wall-clock cap that turns a never-scheduled spinner into a clear error
    # rather than a hang. The assertion margin is 0.3, so 0.4 leaves 0.1 CPU-s of slack on a counter that
    # advances in ~15.6ms scheduler ticks.
    SLOW_EVIDENCE_CPU_SECONDS = 0.4
    SLOW_EVIDENCE_WALL_CAP_SECONDS = 120

    def test_snapshot_wires_system_times_and_subject_cpu_from_get_host_load_system_times_verbatim(self) -> None:
        # Overrides Get-HostLoadSystemTimes (the SAME redefine-after-dot-source technique the
        # existing Get-CimInstance/Get-Process mocks already use) to return a sentinel object whose
        # capturedAtUtc, idle/kernel/user seconds, AND subjectCpuSeconds are all far from anything
        # the real Get-HostLoadSnapshot would independently compute. Runs the REAL, unmodified
        # Get-HostLoadSnapshot and asserts every one of those fields on the resulting snapshot is
        # the sentinel value verbatim -- proving they are WIRED from the one call to
        # Get-HostLoadSystemTimes, not independently re-derived. Reverting Get-HostLoadSnapshot to
        # take its own `[datetime]::UtcNow` for capturedAtUtc (fable's exact round-8 mutant at the
        # old :484), or to read $SubjectProcess.TotalProcessorTime itself instead of taking
        # subjectCpuSeconds off $systemTimes (sol's round-8 blocker, reintroduced), each make a
        # DIFFERENT one of these assertions fail.
        proc = self.run_snippet(
            "function Get-HostLoadSystemTimes {\n"
            "    param($SubjectProcess, [switch]$SubjectNotYetStarted)\n"
            "    $script:receivedSubjectNotYetStarted = $SubjectNotYetStarted.IsPresent\n"
            "    [pscustomobject]@{\n"
            "        idleSeconds = 111.0; kernelSeconds = 222.0; userSeconds = 333.0\n"
            "        capturedAtUtc = [datetime]::new(2026, 6, 1, 0, 0, 0, [DateTimeKind]::Utc)\n"
            "        subjectCpuSeconds = 77.5\n"
            "    }\n"
            "}\n"
            "$s = Get-HostLoadSnapshot -TopProcessCount 1 -SkipEvidenceCollection -SubjectNotYetStarted\n"
            "Write-Host \"CAPTURED=$($s.capturedAtUtc)\"\n"
            "Write-Host \"SYSTEM_TIMES_CAPTURED=$($s.systemTimesCapturedAtUtc)\"\n"
            "Write-Host \"IDLE=$($s.systemIdleSeconds)\"\n"
            "Write-Host \"KERNEL=$($s.systemKernelSeconds)\"\n"
            "Write-Host \"USER=$($s.systemUserSeconds)\"\n"
            "Write-Host \"SUBJECT=$($s.subjectCpuSeconds)\"\n"
            "Write-Host \"PASSED_SUBJECT_NOT_YET_STARTED=$script:receivedSubjectNotYetStarted\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        captured_line = next(l for l in proc.stdout.splitlines() if l.startswith("CAPTURED="))
        system_times_captured_line = next(l for l in proc.stdout.splitlines() if l.startswith("SYSTEM_TIMES_CAPTURED="))
        self.assertIn("2026-06-01", captured_line)
        self.assertIn("2026-06-01", system_times_captured_line)
        self.assertIn("IDLE=111", proc.stdout)
        self.assertIn("KERNEL=222", proc.stdout)
        self.assertIn("USER=333", proc.stdout)
        self.assertIn("SUBJECT=77.5", proc.stdout)
        self.assertIn("PASSED_SUBJECT_NOT_YET_STARTED=True", proc.stdout)

    @unittest.skipUnless(os.name == "nt", "exercises the real GetSystemTimes syscall")
    def test_slow_evidence_collection_does_not_delay_the_syscall_timestamp(self) -> None:
        # Overrides Get-CimInstance to sleep for 900ms before returning (simulating sol/fable's
        # "slow evidence collection" repro), then calls the REAL, unmodified Get-HostLoadSnapshot
        # (evidence collection enabled, i.e. the before/after BRACKET shape, not the interior-sample
        # -SkipEvidenceCollection shape). Asserts the returned systemTimesCapturedAtUtc is stamped
        # BEFORE the first slow evidence call is entered, not after it -- proving the syscall (and, by
        # construction, the subject-CPU read that immediately follows it inside
        # Get-HostLoadSystemTimes) is not waiting on the slow evidence work. Also asserts the call
        # really did take >= 900ms end to end, so a mock that silently never ran cannot make this
        # test pass vacuously.
        #
        # De-flake (CI-FLAKE-HOST-LOAD-GATE-SYSCALL-TIMESTAMP-1): the assertion is an ORDERING, not an
        # elapsed-time bound. The old shape bounded systemTimesCapturedAtUtc - callStartUtc below 1200ms,
        # but everything between the call start and the syscall that is NOT evidence collection (the
        # one-time Add-Type JIT in Get-HostLoadSystemTimes, first-use cmdlet resolution, a starved
        # runner) counts toward that lag too, so a slow runner failed correct production code
        # (merge_group 38281549555: 5340ms lag of a 7265ms call, of which the two mocked CIM calls were
        # only 1800ms; PR #208: 1714.8ms). No wall-clock bound can separate "the syscall waited on
        # evidence" from "the runner was slow before the syscall". The first mocked Get-CimInstance
        # entry is recorded on the same clock, and the contract -- the syscall precedes ALL evidence
        # work -- is exactly systemTimesCapturedAtUtc <= first CIM entry, at any load. The round-8
        # ordering (evidence before the syscall) puts the first CIM entry BEFORE the syscall stamp and
        # fails it.
        proc = self.run_snippet(
            "$script:firstCimEntryUtc = $null; $script:cimCalls = 0\n"
            "function Get-CimInstance {\n"
            "    param($ClassName, $Filter, $ErrorAction, $OperationTimeoutSec)\n"
            "    if ($null -eq $script:firstCimEntryUtc) { $script:firstCimEntryUtc = [datetime]::UtcNow }\n"
            "    $script:cimCalls++\n"
            "    Start-Sleep -Milliseconds 900\n"
            "    if ($ClassName -eq 'Win32_Processor') { [pscustomobject]@{ LoadPercentage = 5 } }\n"
            "    else { [pscustomobject]@{ FreePhysicalMemory = 1024; TotalVisibleMemorySize = 2048 } }\n"
            "}\n"
            "function Get-Process { param($ErrorAction, $Id) @() }\n"
            # hub (sol r9 contract BLOCKER): the syscall-first order is the AFTER-bracket contract, so this
            # test now drives the after shape (-SubjectProcess on a real, idle child). The BEFORE shape
            # (-SubjectNotYetStarted) is pinned to the OPPOSITE order by
            # test_before_bracket_takes_system_times_after_slow_evidence below.
            "$subject = Start-Process -FilePath 'powershell.exe' -ArgumentList "
            "'-NoProfile','-NonInteractive','-Command','Start-Sleep -Seconds 60' -WindowStyle Hidden -PassThru\n"
            "try {\n"
            "$callStartUtc = [datetime]::UtcNow\n"
            "$s = Get-HostLoadSnapshot -TopProcessCount 1 -SubjectProcess $subject\n"
            "$callEndUtc = [datetime]::UtcNow\n"
            "} finally { $subject | Stop-Process -Force -ErrorAction SilentlyContinue }\n"
            # round 9 test-harness note: a plain [datetime]<iso-string> cast in PowerShell does not
            # reliably preserve the "Z"/UTC kind the "o"-format string encodes -- production code
            # never notices because it only ever subtracts two such casts from EACH OTHER (any
            # kind-handling quirk cancels out identically on both sides), but comparing against a
            # native [datetime]::UtcNow here needs an explicit RoundtripKind parse to avoid a bogus
            # multi-hour "offset" masking the real ordering this test measures.
            "$syscallCapturedAtUtc = [datetime]::Parse($s.systemTimesCapturedAtUtc, "
            "[System.Globalization.CultureInfo]::InvariantCulture, "
            "[System.Globalization.DateTimeStyles]::RoundtripKind)\n"
            "$totalCallMs = ($callEndUtc - $callStartUtc).TotalMilliseconds\n"
            "Write-Host \"SYSTEM_TIMES_COLLECTED=$($s.systemTimesCollected)\"\n"
            "Write-Host \"CIM_CALLS=$script:cimCalls\"\n"
            # Positive = the syscall stamp precedes the first evidence call (the contract); negative = the
            # evidence work started first. Exact tick arithmetic on one clock, no tolerance term.
            "Write-Host \"CIM_ENTRY_MINUS_SYSCALL_TICKS=$($script:firstCimEntryUtc.Ticks - $syscallCapturedAtUtc.Ticks)\"\n"
            "Write-Host \"TOTAL_CALL_MS=$totalCallMs\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("SYSTEM_TIMES_COLLECTED=True", proc.stdout)
        # Non-vacuity: the ordering check below is meaningless unless the mocked evidence work ran.
        self.assertIn("CIM_CALLS=2", proc.stdout, "both mocked Get-CimInstance calls must have run")
        entry_minus_syscall_ticks = int(next(
            l for l in proc.stdout.splitlines() if l.startswith("CIM_ENTRY_MINUS_SYSCALL_TICKS="))[
                len("CIM_ENTRY_MINUS_SYSCALL_TICKS="):])
        total_ms = float(next(l for l in proc.stdout.splitlines() if l.startswith("TOTAL_CALL_MS="))[len("TOTAL_CALL_MS="):])
        self.assertGreaterEqual(total_ms, 850.0, "the mocked CIM sleep must actually have run")
        self.assertGreaterEqual(
            entry_minus_syscall_ticks, 0,
            f"the first slow evidence call began {-entry_minus_syscall_ticks / 10000.0}ms BEFORE "
            "systemTimesCapturedAtUtc was stamped -- the syscall is waiting on slow evidence collection "
            "again (round-8 ordering)",
        )

    @unittest.skipUnless(os.name == "nt", "exercises the real GetSystemTimes syscall")
    def test_before_bracket_takes_system_times_after_slow_evidence(self) -> None:
        # hub (sol r9 contract BLOCKER): the BEFORE bracket (-SubjectNotYetStarted, evidence enabled)
        # marks the START of the first interval and its caller starts the subject right after it
        # returns, so the syscall must land at the END of the call, after the slow evidence work --
        # not at its start, where the evidence latency would be pre-leg time folded into the first
        # interval (sol: a 96% early-leg burst read quiet). Same two 900ms CIM mocks as the after-shape
        # test above; the >=1700ms guard proves the mocks ran.
        proc = self.run_snippet(
            "function Get-CimInstance {\n"
            "    param($ClassName, $Filter, $ErrorAction, $OperationTimeoutSec)\n"
            "    Start-Sleep -Milliseconds 900\n"
            "    if ($ClassName -eq 'Win32_Processor') { [pscustomobject]@{ LoadPercentage = 5 } }\n"
            "    else { [pscustomobject]@{ FreePhysicalMemory = 1024; TotalVisibleMemorySize = 2048 } }\n"
            "}\n"
            "function Get-Process { param($ErrorAction, $Id) @() }\n"
            "$callStartUtc = [datetime]::UtcNow\n"
            "$s = Get-HostLoadSnapshot -TopProcessCount 1 -SubjectNotYetStarted\n"
            "$callEndUtc = [datetime]::UtcNow\n"
            "$syscallCapturedAtUtc = [datetime]::Parse($s.systemTimesCapturedAtUtc, "
            "[System.Globalization.CultureInfo]::InvariantCulture, "
            "[System.Globalization.DateTimeStyles]::RoundtripKind)\n"
            "Write-Host \"SYSTEM_TIMES_COLLECTED=$($s.systemTimesCollected)\"\n"
            "Write-Host \"SUBJECT=$($s.subjectCpuSeconds)\"\n"
            "Write-Host \"END_GAP_MS=$(($callEndUtc - $syscallCapturedAtUtc).TotalMilliseconds)\"\n"
            "Write-Host \"TOTAL_CALL_MS=$(($callEndUtc - $callStartUtc).TotalMilliseconds)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("SYSTEM_TIMES_COLLECTED=True", proc.stdout)
        self.assertIn("SUBJECT=0", proc.stdout)
        end_gap_ms = float(next(l for l in proc.stdout.splitlines() if l.startswith("END_GAP_MS="))[len("END_GAP_MS="):])
        total_ms = float(next(l for l in proc.stdout.splitlines() if l.startswith("TOTAL_CALL_MS="))[len("TOTAL_CALL_MS="):])
        self.assertGreaterEqual(total_ms, 1700.0, "both mocked CIM sleeps must actually have run")
        self.assertLess(
            end_gap_ms, 400.0,
            f"the before bracket's syscall landed {end_gap_ms}ms before the call returned (of {total_ms}ms) -- "
            "it is running before the slow evidence collection, diluting the first interval",
        )

    def test_before_bracket_evidence_failure_does_not_lose_the_timing_capture(self) -> None:
        # hub (hosted ubuntu red on 36d4804e + fable HARDENING): with evidence first on the before path, a
        # throwing Get-CimInstance (always the case on non-Windows pwsh, where the cmdlet does not exist)
        # must not skip the syscall/subject capture that follows it. subjectCpuSeconds stays the
        # definitional 0 on every platform, and on Windows the system times are still collected, exactly
        # as at b1a4ab55 where the syscall ran first. collected stays False: evidence did fail.
        proc = self.run_snippet(
            "function Get-CimInstance { param($ClassName, $Filter, $ErrorAction, $OperationTimeoutSec) "
            "throw [System.InvalidOperationException]::new('synthetic CIM failure') }\n"
            "$s = Get-HostLoadSnapshot -TopProcessCount 1 -SubjectNotYetStarted\n"
            "Write-Host \"SUBJECT=$($s.subjectCpuSeconds)\"\n"
            "Write-Host \"SYSTEM_TIMES_COLLECTED=$($s.systemTimesCollected)\"\n"
            "Write-Host \"COLLECTED=$($s.collected)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("SUBJECT=0", proc.stdout)
        self.assertIn("COLLECTED=False", proc.stdout)
        if os.name == "nt":
            self.assertIn("SYSTEM_TIMES_COLLECTED=True", proc.stdout)

    def test_the_before_bracket_call_site_uses_the_evidence_first_shape(self) -> None:
        # The ordering above is keyed on -SubjectNotYetStarted; pin that the one production before-bracket
        # call passes it and keeps evidence enabled, so dropping either silently restores the dilution.
        source = Path(SMOKE_SCRIPT).read_text(encoding="utf-8")
        calls = [l for l in source.splitlines() if l.lstrip().startswith("$hostLoadBefore = Get-HostLoadSnapshot")]
        self.assertEqual(len(calls), 1, calls)
        self.assertIn("-SubjectNotYetStarted", calls[0])
        self.assertNotIn("-SkipEvidenceCollection", calls[0])

    @unittest.skipUnless(os.name == "nt", "spawns a real Windows child process and needs the real syscall")
    def test_slow_evidence_collection_does_not_inflate_a_real_subjects_recorded_cpu(self) -> None:
        # sol's exact round-8 blocker, reproduced end to end against the real producer instead of a
        # synthetic fixture: a real, continuously CPU-spinning child process stands in for the
        # subject; Get-CimInstance is mocked to sleep 900ms (simulating slow evidence collection).
        # If the subject read happened AFTER evidence collection (the round-8 ordering sol's repro
        # depended on), Get-HostLoadSnapshot's recorded subjectCpuSeconds would include most of the
        # CPU the spinner burns DURING that 900ms sleep. Post-round-9, the subject is read
        # immediately after the syscall, before evidence collection even starts, so the recorded
        # value must be far below what a SEPARATE reading taken right after the call returns (which
        # necessarily includes the full 900ms of spin) would show.
        #
        # De-flake (CI-FLAKE-HOST-LOAD-GATE-SLOW-EVIDENCE-1): the mocked "slow evidence collection" is
        # driven by the spinner's CONSUMED CPU TIME, not by a wall-clock sleep. The old shape slept a fixed
        # 900ms per CIM call and then required the spinner to have burned > 0.3 CPU-seconds in that
        # wall-clock window -- an unstated assumption that the spinner gets >= ~17% of a core. On a loaded
        # shared runner the spinner is starved (merge_group 37531801692: 0.421875 vs 0.425; 7 of 8 runs
        # fail with 32 busy loops on 16 cores) and the margin shrinks to nothing with correct production
        # code. Now each mocked CIM call waits (polling, hard wall-clock cap) until the spinner's own
        # TotalProcessorTime has advanced by SLOW_EVIDENCE_CPU_SECONDS past the value at that call's entry, so
        # "evidence collection took long enough for the subject to burn N CPU-seconds" is true by
        # construction at any load. The invariants are then exact comparisons on the same monotonic
        # counter, with no timer-granularity term: recorded <= cpu_at_first_cim_entry (the subject read
        # precedes all evidence work) and cpu_at_last_cim_exit - recorded >= SLOW_EVIDENCE_CPU_SECONDS.
        # Reverting to the round-8 ordering (subject read AFTER evidence) makes recorded >= the exit value
        # and fails both.
        burn = self.SLOW_EVIDENCE_CPU_SECONDS
        proc = self.run_snippet(
            "$script:firstEntry = $null; $script:lastExit = $null\n"
            "$script:capHit = ''; $script:cimCallsStarted = 0; $script:cimCallsCompleted = 0\n"
            "function Get-CimInstance {\n"
            "    param($ClassName, $Filter, $ErrorAction, $OperationTimeoutSec)\n"
            "    $script:cimCallsStarted++\n"
            "    $callNumber = $script:cimCallsStarted\n"
            "    $spinner.Refresh()\n"
            "    $entry = $spinner.TotalProcessorTime.TotalSeconds\n"
            "    if ($null -eq $script:firstEntry) { $script:firstEntry = $entry }\n"
            f"    $deadline = [datetime]::UtcNow.AddSeconds({self.SLOW_EVIDENCE_WALL_CAP_SECONDS})\n"
            f"    while ($spinner.TotalProcessorTime.TotalSeconds -lt $entry + {burn}) {{\n"
            # Get-HostLoadSnapshot wraps its evidence collection in its own try/catch, so a throw from
            # here is swallowed (collected=false) and the process still exits 0. Record the cap hit in a
            # script-scope flag, which survives that catch, so the test can fail with the cap as the reason.
            "        if ([datetime]::UtcNow -gt $deadline) {\n"
            "            $script:capHit = \"CIM call $callNumber\"\n"
            "            throw 'spinner never burned the target CPU (wall cap)'\n"
            "        }\n"
            "        Start-Sleep -Milliseconds 20\n"
            "        $spinner.Refresh()\n"
            "    }\n"
            "    $script:lastExit = $spinner.TotalProcessorTime.TotalSeconds\n"
            "    $script:cimCallsCompleted++\n"
            "    if ($ClassName -eq 'Win32_Processor') { [pscustomobject]@{ LoadPercentage = 5 } }\n"
            "    else { [pscustomobject]@{ FreePhysicalMemory = 1024; TotalVisibleMemorySize = 2048 } }\n"
            "}\n"
            "function Get-Process { param($ErrorAction, $Id) @() }\n"
            "$spinner = Start-Process -FilePath 'powershell.exe' -ArgumentList "
            "'-NoProfile','-NonInteractive','-Command','while ($true) { [Math]::Sqrt(12345) | Out-Null }' "
            "-WindowStyle Hidden -PassThru\n"
            "try {\n"
            "    $before = Get-HostLoadSnapshot -TopProcessCount 1 -SubjectProcess $spinner\n"
            "    $spinner.Refresh()\n"
            "    $actualAfterCallSeconds = $spinner.TotalProcessorTime.TotalSeconds\n"
            "    Write-Host \"COLLECTED=$($before.collected)\"\n"
            "    Write-Host \"CAPHIT=$script:capHit\"\n"
            "    Write-Host \"CIM_CALLS_COMPLETED=$script:cimCallsCompleted\"\n"
            "    Write-Host \"RECORDED=$($before.subjectCpuSeconds)\"\n"
            "    Write-Host \"FIRST_CIM_ENTRY=$script:firstEntry\"\n"
            "    Write-Host \"LAST_CIM_EXIT=$script:lastExit\"\n"
            "    Write-Host \"ACTUAL_AFTER_CALL=$actualAfterCallSeconds\"\n"
            "} finally {\n"
            "    $spinner | Stop-Process -Force -ErrorAction SilentlyContinue\n"
            "}\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        def _text(name: str) -> str:
            return next(l for l in proc.stdout.splitlines() if l.startswith(name + "="))[len(name) + 1:].strip()

        def _value(name: str) -> float:
            return float(_text(name))

        # Checked BEFORE any CPU value is parsed: a wall-cap throw inside the mocked Get-CimInstance is
        # swallowed by Get-HostLoadSnapshot's own try/catch, so the process still exits 0. Without these
        # three checks a cap hit on the first call dies below as a bare ValueError (empty LAST_CIM_EXIT)
        # and a cap hit on the second call passes silently on the first call's retained LAST_CIM_EXIT.
        cap_hit = _text("CAPHIT")
        self.assertEqual(
            cap_hit, "",
            f"the mocked slow evidence collection hit its {self.SLOW_EVIDENCE_WALL_CAP_SECONDS}s wall cap "
            f"({cap_hit}): the spinner never burned {burn} CPU-seconds inside the mock, so this run proves nothing")
        self.assertEqual(
            _text("COLLECTED"), "True",
            "Get-HostLoadSnapshot did not report collected=True -- a mocked evidence call threw "
            f"(the {self.SLOW_EVIDENCE_WALL_CAP_SECONDS}s wall cap or otherwise) and the snapshot swallowed it")
        self.assertEqual(
            _text("CIM_CALLS_COMPLETED"), "2",
            "expected both mocked Get-CimInstance calls (Win32_Processor, Win32_OperatingSystem) to complete "
            f"their {burn} CPU-second burn; a fewer count means a call hit the "
            f"{self.SLOW_EVIDENCE_WALL_CAP_SECONDS}s wall cap and the retained LAST_CIM_EXIT is stale")
        recorded = _value("RECORDED")
        first_cim_entry = _value("FIRST_CIM_ENTRY")
        last_cim_exit = _value("LAST_CIM_EXIT")
        actual_after_call = _value("ACTUAL_AFTER_CALL")
        # The mock really ran (not vacuous) and the spinner really burned the target CPU inside it.
        self.assertGreaterEqual(
            last_cim_exit - first_cim_entry, burn,
            "the mocked slow evidence collection did not span the target subject CPU time")
        self.assertGreaterEqual(actual_after_call, last_cim_exit)
        # The subject read precedes all evidence collection ...
        self.assertLessEqual(
            recorded, first_cim_entry,
            f"recorded subjectCpuSeconds ({recorded}) is above the subject's CPU at the start of evidence "
            f"collection ({first_cim_entry}) -- the subject read happened after evidence collection began")
        # ... so none of the CPU burned during it is in the recorded value.
        self.assertGreater(
            last_cim_exit, recorded + 0.3,
            f"recorded subjectCpuSeconds ({recorded}) is too close to the subject's CPU at the end of "
            f"evidence collection ({last_cim_exit}) -- the subject read is not happening before the slow "
            "evidence collection, so it is picking up CPU the spinner burned during the mocked delay",
        )


@requires_pwsh
class HostLoadPartialCollectionEndToEndVerdictTests(_ProbeCase):
    """round 7 (both keys BLOCKER): rerun of both keys' round-6 repros -- adapted to the new
    mechanism -- chained through the real Get-HostLoadNonSubjectCpuLoadPercent -> Get-HostLoadVerdict
    wiring, proving neither one reads quiet under the new system-wide mechanism. PID reuse (round
    6's third repro) has no equivalent here at all: there is no PID-keyed map anywhere in this
    computation for a reused PID to corrupt -- see the class docstring below for why that is a
    structural property, not merely a fix."""

    script = SMOKE_SCRIPT
    functions = ["Get-HostLoadNonSubjectCpuLoadPercent", "Get-HostLoadVerdict"]

    @staticmethod
    def _snap(offset_seconds: float, kernel_seconds: float, subject_seconds, collected: bool = True) -> str:
        # round 8: systemTimesCapturedAtUtc coincides with capturedAtUtc here -- none of this
        # class's repros are about window alignment (that is HostLoadWindowAlignmentTests below).
        collected_literal = "$true" if collected else "$false"
        subject_literal = "$null" if subject_seconds is None else repr(float(subject_seconds))
        return (
            "[pscustomobject]@{ "
            f"collected = {collected_literal}; "
            f"capturedAtUtc = (Get-Date '2026-01-01T00:00:00Z').AddSeconds({offset_seconds}).ToString('o'); "
            "systemTimesCollected = $true; systemIdleSeconds = 0.0; "
            f"systemTimesCapturedAtUtc = (Get-Date '2026-01-01T00:00:00Z').AddSeconds({offset_seconds}).ToString('o'); "
            f"systemKernelSeconds = {kernel_seconds!r}; systemUserSeconds = 0.0; "
            f"subjectCpuSeconds = {subject_literal}; cpuLoadPercent = 0.0 }}"
        )

    def _verdict_via_real_wiring(self, before: str, after: str, bar: float = 75) -> subprocess.CompletedProcess:
        # A single [before, after] interval is enough to prove each repro -- both keys' original
        # findings were about the SUBTRACTION itself reading falsely clean, not about multi-sample
        # sequencing, so this stays minimal rather than reusing `before` as a bogus "after" that
        # would create a chronologically-backward (negative-elapsed) second interval.
        return self.run_snippet(
            f"$before = {before}\n"
            f"$after = {after}\n"
            f"$v = Get-HostLoadVerdict -Before $before -After $after -Bar {bar} -ProcessorCount 4\n"
            "Write-Host \"STATE=$($v.state) PROVISIONAL=$($v.provisional) MAX=$($v.maxCpuLoadPercent)\"\n"
        )

    def test_astras_exact_repro_a_new_loader_contributing_15_36_cpu_seconds_no_longer_reads_quiet(self) -> None:
        # astra's round-7 exact repro: "a new loader contributing 15.36 CPU-seconds over 4 s x 4
        # procs" -- under the OLD per-process mechanism, a loader present only in the CURRENT
        # snapshot contributed nothing to the matched-by-PID sum (round 6's own residual, closed
        # here by construction: system-wide busy time counts it automatically, with no per-process
        # matching step to omit it from). 15.36 busy processor-seconds over 16 capacity = 96%.
        before = self._snap(0, kernel_seconds=0.0, subject_seconds=0.0)
        after = self._snap(4, kernel_seconds=15.36, subject_seconds=0.0)
        proc = self._verdict_via_real_wiring(before, after)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("MAX=96", proc.stdout)
        self.assertIn("STATE=exceeded PROVISIONAL=True", proc.stdout)
        self.assertNotIn("STATE=quiet", proc.stdout)

    def test_sols_exact_repro_a_raw_96_percent_load_with_the_subject_unreadable_no_longer_reads_quiet(self) -> None:
        # sol's round-7 exact repro: "a raw-96 loader with a throwing getter." Re-expressed for the
        # new mechanism: the subject's own CPU time is unreadable this sample (its process-handle
        # read failed -- see Get-HostLoadSnapshot's third-state handling), while the SYSTEM-WIDE
        # busy time genuinely reflects 96% load (15.36 of 16 processor-seconds). Under the OLD
        # mechanism a throwing per-process getter could be silently counted as zero, undercounting
        # the total; here, an unreadable SUBJECT correctly refuses to compute a value at all
        # (Get-HostLoadNonSubjectCpuLoadPercent returns $null), which Get-HostLoadVerdict must
        # treat as unknown/provisional -- never quiet, and never silently substituting a raw number.
        before = self._snap(0, kernel_seconds=0.0, subject_seconds=0.0)
        after = self._snap(4, kernel_seconds=15.36, subject_seconds=None)
        proc = self._verdict_via_real_wiring(before, after)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STATE=unknown PROVISIONAL=True", proc.stdout)
        self.assertNotIn("STATE=quiet", proc.stdout)

    def test_pid_reuse_has_no_expressible_repro_under_the_new_mechanism(self) -> None:
        # round 6's third repro -- "reusing a PID whose previous cumulative value is higher
        # produced the same false-clean result" -- required a PID-keyed map of per-process
        # cumulative seconds that a stale/reused key could corrupt. Get-HostLoadNonSubjectCpuLoadPercent
        # accepts exactly four numeric system-wide fields and one subject-handle-derived number; no
        # PID, process name, or process identity of any kind is an input to this function. This
        # test proves the ABSENCE of the field itself: constructing a fixture that HAS no PID-keyed
        # property at all still produces the correct, unambiguous answer -- there is no field left
        # for a reused PID to have corrupted in the first place.
        previous = self._snap(0, kernel_seconds=0.0, subject_seconds=0.0)
        current = self._snap(4, kernel_seconds=15.36, subject_seconds=0.0)
        proc = self.run_snippet(
            f"$previous = {previous}\n"
            f"$current = {current}\n"
            "$hasPidKeyedProperty = $null -ne $current.PSObject.Properties['processCpuSecondsById']\n"
            "Write-Host \"HAS_PID_KEYED_PROPERTY=$hasPidKeyedProperty\"\n"
            "$p = Get-HostLoadNonSubjectCpuLoadPercent -CurrentSnapshot $current "
            "-PreviousSnapshot $previous -ProcessorCount 4\n"
            "Write-Host \"PERCENT=$p\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("HAS_PID_KEYED_PROPERTY=False", proc.stdout)
        self.assertIn("PERCENT=96", proc.stdout)


@requires_pwsh
class GuiSmokeResultCarriesHostLoadTests(unittest.TestCase):
    """Static structural checks: the GUI-smoke JSON result must carry hostLoad end to end."""

    def test_result_object_declares_a_hostload_block_with_before_after_bar(self) -> None:
        source = SMOKE_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("hostLoad = [pscustomobject]@{", source)
        self.assertIn("mlvapp-gui-smoke-host-load.v1", source)
        self.assertIn("before = $hostLoadBefore", source)
        self.assertIn("after = $hostLoadAfter", source)
        self.assertIn("provisional = [bool]$hostLoadVerdict.provisional", source)

    def test_hostload_capture_brackets_the_launched_process_not_just_the_script(self) -> None:
        source = SMOKE_SCRIPT.read_text(encoding="utf-8")
        before_capture = source.index("$hostLoadBefore = Get-HostLoadSnapshot")
        process_start = source.index("$process = [System.Diagnostics.Process]::Start($startInfo)")
        after_capture = source.index("$hostLoadAfter = Get-HostLoadSnapshot")
        self.assertLess(before_capture, process_start, "hostLoad 'before' must be captured before launch")
        self.assertLess(process_start, after_capture, "hostLoad 'after' must be captured after the process ends")

    def test_leg_wait_is_wired_to_sample_host_load_during_the_leg(self) -> None:
        # round 3: bracketing alone (before Process::Start, after WaitForExit) is not enough --
        # interior snapshots must be taken WHILE the leg runs, and the verdict must actually
        # consume them.
        #
        # round 7 (sol + astra MAJOR -- "a non-returning sampler prevents both timeout handling
        # and receipt publication"): the sampling loop moved OUT of Wait-GuiSmokeProcessBounded
        # (which no longer takes -SampleIntervalMs/-OnSample at all -- see
        # test_wait_call_no_longer_passes_sampling_parameters below) and INTO this script's own
        # scope, sampling inline between chunked waits it drives itself.
        source = SMOKE_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("if ($process.WaitForExit($hostLoadChunkMs)) {", source)
        self.assertIn(
            "$newSample = Get-HostLoadSnapshot -TopProcessCount $HostLoadTopProcessCount -SubjectProcess $process",
            source,
        )
        self.assertIn("$hostLoadDuringSamples.Add($newSample)", source)
        self.assertIn("-During @($hostLoadDuringSamples)", source)
        self.assertIn("during = @($hostLoadDuringSamples)", source)

    def test_interior_samples_skip_evidence_collection_but_brackets_do_not(self) -> None:
        # round 8 (sol MAJOR item 2): the interior sampling-loop call must pass
        # -SkipEvidenceCollection (so a during-leg sample is the syscall plus the subject-handle
        # read only, per Get-HostLoadSnapshot's own round-8 contract) -- the before/after bracket
        # calls must NOT pass it, since the receipt still needs their full CIM/Get-Process evidence.
        source = SMOKE_SCRIPT.read_text(encoding="utf-8")
        interior_call_start = source.index("$newSample = Get-HostLoadSnapshot -TopProcessCount $HostLoadTopProcessCount -SubjectProcess $process")
        interior_call_end = source.index("\n        $hostLoadDuringSamples.Add($newSample)", interior_call_start)
        interior_call = source[interior_call_start:interior_call_end]
        self.assertIn("-SkipEvidenceCollection", interior_call)

        before_call_start = source.index("$hostLoadBefore = Get-HostLoadSnapshot")
        before_call_end = source.index("\n", before_call_start)
        self.assertNotIn("-SkipEvidenceCollection", source[before_call_start:before_call_end])

        after_call_start = source.index("$hostLoadAfter = Get-HostLoadSnapshot")
        after_call_end = source.index("\n", after_call_start)
        self.assertNotIn("-SkipEvidenceCollection", source[after_call_start:after_call_end])

    def test_wait_call_no_longer_passes_sampling_parameters(self) -> None:
        # round 7: proves the removal is real at the CALL SITE, not merely that the module's
        # signature changed elsewhere -- the actual $processBoundary = Wait-GuiSmokeProcessBounded
        # invocation here must not pass -SampleIntervalMs or -OnSample (both parameters no longer
        # exist on that function at all -- see gui-smoke-process-boundary.psm1).
        source = SMOKE_SCRIPT.read_text(encoding="utf-8")
        call_start = source.index("$processBoundary = Wait-GuiSmokeProcessBounded")
        call_end = source.index("\n$stdout = $processBoundary.stdout", call_start)
        call_text = source[call_start:call_end]
        self.assertNotIn("-SampleIntervalMs", call_text)
        self.assertNotIn("-OnSample", call_text)
        # VENUE-SMOKE-PROCESS-TIMEOUT-BOUND-2 r1: the finalize wait is no longer a remainder precomputed before the call (a runner descheduled
        # between that computation and the call spent it twice); the boundary computes it from the launch stamp and the whole budget itself.
        self.assertIn("-TimeoutMs $effectiveProcessTimeoutMs", call_text)
        self.assertIn("-LifetimeBudgetMs $effectiveProcessTimeoutMs", call_text)
        self.assertIn("-LaunchTimestamp $startTimestamp", call_text)
        self.assertNotIn("$hostLoadFinalizeTimeoutMs", source, "the precomputed finalize remainder is gone")

    def test_verdict_call_is_wired_with_the_observed_max_sample_gap(self) -> None:
        # round 6 (astra MAJOR -- "enforcement tests check TOKENS, not behaviour"): astra's exact
        # repro against this class -- "Remove the production ObservedMaxSampleGapMs argument; all
        # three smoke wiring tests pass and the mutant parses." None of the three tests above ever
        # looked at the actual Get-HostLoadVerdict call site, so a caller that silently dropped
        # -ObservedMaxSampleGapMs (quietly reverting to round-4's declared-cadence-only coverage
        # rule) would sail through. Pin the real call site directly: the -Bar argument and the
        # -ObservedMaxSampleGapMs argument (fed from the real observed-gap computation, not the
        # declared cadence) must both be present on the SAME Get-HostLoadVerdict invocation.
        source = SMOKE_SCRIPT.read_text(encoding="utf-8")
        verdict_call_start = source.index("$hostLoadVerdict = Get-HostLoadVerdict -Before $hostLoadBefore")
        verdict_call_end = source.index("\n\n", verdict_call_start)
        verdict_call = source[verdict_call_start:verdict_call_end]
        self.assertIn("-Bar $HostLoadCpuPercentBar", verdict_call)
        self.assertIn("-SampleIntervalMs $HostLoadSampleIntervalMs", verdict_call)
        self.assertIn("-ObservedMaxSampleGapMs $(if ($null -ne $hostLoadObservedMaxSampleGapMs)", verdict_call)
        # And the value fed in must trace back to the REAL observed gap computation (the max
        # spacing actually measured between capturedAtUtc timestamps), not merely be present as a
        # parameter with some other, unbound source.
        observed_gap_computation = source.index("$hostLoadObservedMaxSampleGapMs = $null")
        self.assertLess(
            observed_gap_computation, verdict_call_start,
            "hostLoadObservedMaxSampleGapMs must be computed from real sample timestamps before "
            "the verdict call consumes it",
        )
        self.assertIn("$gapMs = ([datetime]$hostLoadSampleSequence[$i].capturedAtUtc -", source)


@requires_pwsh
class GuiSmokeObservedMaxSampleGapGuardExecutesTests(_ProbeCase):
    """round 7 (astra MAJOR, exact repro): "Flipping max-gap '-gt' to '-lt' ... passes all four
    GUI wiring tests, while timestamps 0/4/11/15 seconds change UNKNOWN to quiet." The tests in
    GuiSmokeResultCarriesHostLoadTests above only check that certain substrings (including the
    literal text "-gt") are PRESENT in the source, in the right relative order -- a mutation that
    keeps every one of those substrings intact (there is no "-gt" token check at all; the only
    thing asserted is that the computation and the call site exist) sails through unnoticed. This
    extracts the REAL max-gap-tracking loop out of the live script by literal text search and
    EXECUTES it against astra's exact timestamps, so a `-gt` -> `-lt` mutation changes what this
    test actually observes (7000ms, the true max gap) rather than what substrings are present."""

    script = SMOKE_SCRIPT
    functions: list[str] = []

    START_MARKER = "$hostLoadSampleSequence = @($hostLoadBefore)"
    END_MARKER = "}\n$hostLoadVerdict = Get-HostLoadVerdict"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="hostload-maxgap-probe-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        source = self.script.read_text(encoding="utf-8")
        self.guard_block = _extract_block(source, self.START_MARKER, self.END_MARKER, trailing_lines=0)
        self.assertIn("-gt $hostLoadObservedMaxSampleGapMs", self.guard_block)

    def _run(self, offsets_seconds: list[float]) -> subprocess.CompletedProcess:
        def snap(offset: float) -> str:
            return (
                "[pscustomobject]@{ capturedAtUtc = "
                f"(Get-Date '2026-01-01T00:00:00Z').AddSeconds({offset}).ToString('o') }}"
            )
        script = self.tmp / "run.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"$hostLoadBefore = {snap(offsets_seconds[0])}\n"
            "$hostLoadDuringSamples = @(" +
            ", ".join(snap(o) for o in offsets_seconds[1:-1]) +
            ")\n"
            f"$hostLoadAfter = {snap(offsets_seconds[-1])}\n"
            f"{self.guard_block}\n"
            "Write-Host \"MAXGAP=$hostLoadObservedMaxSampleGapMs\"\n",
            encoding="utf-8",
        )
        return subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(script)],
            capture_output=True, text=True, creationflags=_NO_WINDOW,
        )

    def test_astras_exact_repro_the_true_maximum_gap_is_7000ms_not_4000ms(self) -> None:
        # Timestamps at 0, 4, 11, 15 seconds -> gaps of 4000, 7000, 4000 ms. The correct algorithm
        # (running max, "-gt") must report 7000. A "-lt" mutant instead ends up reporting 4000 (the
        # first gap, since 7000 is not "-lt" the running value and never replaces it) -- astra's
        # own description: "changes UNKNOWN to quiet", because 4000ms no longer exceeds a 4000ms
        # cadence's 1.5x (6000ms) threshold while the true 7000ms gap does.
        proc = self._run([0, 4, 11, 15])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("MAXGAP=7000", proc.stdout)

    def test_a_single_gap_still_reports_correctly(self) -> None:
        # Regression guard / other side of the boundary: with only one gap, "running max" and a
        # "running min" mutant would coincidentally agree (there is nothing to compare against on
        # the first iteration) -- this alone would NOT catch the mutation, which is exactly why the
        # test above uses three gaps of differing size instead of just before/after.
        proc = self._run([0, 4])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("MAXGAP=4000", proc.stdout)


@requires_pwsh
class CompareGuiSmokeAbInconclusiveVerdictTests(_ProbeCase):
    """VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-1 r3 (sol r2 B1, its literal): a known-CIM-failed BeforeSmoke (the control) and a quiet AfterSmoke
    made the host-load refusal join $failures and the comparator returned FAIL. A provisional or unknown leg is now never counted: the
    verdict is INCONCLUSIVE (never PASS, never FAIL) with the typed line REFERENCE_PROVISIONAL leg=<before|after> reason=hostload-<state>.
    Row (c): two quiet legs still PASS."""

    script = COMPARE_SCRIPT
    functions = ["Get-NestedValue", "Get-HostLoadComparisonEvidence", "Get-GuiSmokeAbVerdict"]
    preamble = COMPARE_PREAMBLE
    CIM = ("[pscustomobject]@{ hostLoad = [pscustomobject]@{ provisional = $true; state = 'unknown'; reason = 'collection failed: CimException';"
           " before = [pscustomobject]@{ collected = $false }; after = [pscustomobject]@{ collected = $false } } }")
    QUIET = "[pscustomobject]@{ hostLoad = [pscustomobject]@{ provisional = $false; state = 'quiet'; reason = $null; " + SNAPS + " } }"

    def _verdict(self, before: str, after: str, failures: str = "@()") -> subprocess.CompletedProcess:
        return self.run_snippet(
            f"$before = {before}\n$after = {after}\n"
            "$h = Get-HostLoadComparisonEvidence -BeforeSmoke $before -AfterSmoke $after\n"
            f"$v = Get-GuiSmokeAbVerdict -Failures {failures} -HostLoadEvidence $h.evidence\n"
            "Write-Host \"VERDICT=$($v.verdict)\"\n"
            "$v.referenceProvisional | ForEach-Object { Write-Host $_ }\n"
        )

    def test_a_cim_failed_control_is_inconclusive_with_the_typed_line(self) -> None:
        proc = self._verdict(self.CIM, self.QUIET)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("VERDICT=INCONCLUSIVE", proc.stdout)
        self.assertIn("REFERENCE_PROVISIONAL leg=before reason=hostload-unknown", proc.stdout)
        self.assertNotIn("leg=after", proc.stdout)

    def test_r6_b1_a_quiet_false_block_beside_an_uncollected_snapshot_is_inconclusive(self) -> None:
        # sol r5 B1, its input: BeforeSmoke hostLoad {state quiet, provisional false, before.collected=false (CimException), after.collected=true}, AfterSmoke
        # clean, no other failure -> the comparator returned PASS. The venue reader's rule reads it unknown: INCONCLUSIVE with the typed refusal.
        uncollected = ("[pscustomobject]@{ hostLoad = [pscustomobject]@{ provisional = $false; state = 'quiet'; reason = $null;"
                       " before = [pscustomobject]@{ collected = $false; error = 'collection failed: CimException' }; after = [pscustomobject]@{ collected = $true } } }")
        proc = self._verdict(uncollected, self.QUIET)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("VERDICT=INCONCLUSIVE", proc.stdout, proc.stdout + proc.stderr)
        self.assertIn("REFERENCE_PROVISIONAL leg=before reason=hostload-unknown", proc.stdout)
        self.assertNotIn("leg=after", proc.stdout)

    def test_r6_b1_coerced_provisional_and_case_folded_states_are_inconclusive(self) -> None:
        # sol r5 B1 (and opus r5 H1 / fable key3 A3): provisional=0, provisional=[false], provisional='false' and state 'QUIET' / 'Quiet' read clean.
        for prov, state in (("0", "'quiet'"), ("@($false)", "'quiet'"), ("'false'", "'quiet'"), ("$false", "'QUIET'"), ("$false", "'Quiet'")):
            with self.subTest(provisional=prov, state=state):
                before = f"[pscustomobject]@{{ hostLoad = [pscustomobject]@{{ provisional = {prov}; state = {state}; reason = $null; {SNAPS} }} }}"
                proc = self._verdict(before, self.QUIET)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn("VERDICT=INCONCLUSIVE", proc.stdout, proc.stdout + proc.stderr)
                self.assertIn("REFERENCE_PROVISIONAL leg=before reason=hostload-", proc.stdout)

    def test_r6_a1_an_array_wrapped_or_repeated_member_smoke_is_inconclusive(self) -> None:
        # adversary-de8e3799.json A1, its input: before.hostLoad = [clean] (and a singleton root array, and state exceeded then quiet) read PASS.
        clean = '{"state":"quiet","provisional":false,"before":{"collected":true},"after":{"collected":true}}'
        for text in ('{"hostLoad":[' + clean + ']}', '[{"hostLoad":' + clean + '}]',
                     '{"hostLoad":{"state":"exceeded","state":"quiet","provisional":false,"before":{"collected":true},"after":{"collected":true}}}'):
            with self.subTest(text=text):
                proc = self.run_snippet(
                    f"$t = '{text}'\n$before = $t | ConvertFrom-Json\n$after = {self.QUIET}\n"
                    "$h = Get-HostLoadComparisonEvidence -BeforeSmoke $before -AfterSmoke $after -BeforeText $t\n"
                    "$v = Get-GuiSmokeAbVerdict -Failures @() -HostLoadEvidence $h.evidence\n"
                    "Write-Host \"VERDICT=$($v.verdict)\"\n")
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn("VERDICT=INCONCLUSIVE", proc.stdout, proc.stdout + proc.stderr)
        # without text, a hostLoad array member is still refused (it is read as it is, never through the pipeline)
        proc = self._verdict("('{\"hostLoad\":[" + clean + "]}' | ConvertFrom-Json)", self.QUIET)
        self.assertIn("VERDICT=INCONCLUSIVE", proc.stdout, proc.stdout + proc.stderr)

    def test_r6_b1_the_comparator_uses_the_venue_readers_one_rule(self) -> None:
        # The class guard: the comparator has no second host-load parser; it imports and calls VenueChain.psm1's.
        text = COMPARE_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("Import-Module (Join-Path $PSScriptRoot 'dual-venue\\VenueChain.psm1') -Function ConvertFrom-VenueSmokeHostLoad, Get-VenueReferenceHostLoadRefusal", text)
        body = _extract_functions(COMPARE_SCRIPT, ["Get-HostLoadComparisonEvidence"])
        self.assertIn("ConvertFrom-VenueSmokeHostLoad -HostLoad $legHostLoad", body)
        self.assertNotIn("[bool]$legProvisionalRaw", body, "no [bool] coercion of a declared provisional")
        self.assertIn("Get-VenueReferenceHostLoadRefusal -LegId $legName -Standing $standing", _extract_functions(COMPARE_SCRIPT, ["Get-GuiSmokeAbVerdict"]))
        # fable key3 r5 (GUI-SMOKE-AB-INCONCLUSIVE-WITHHOLD-DELTA-1): an INCONCLUSIVE verdict withholds the cross-leg fps deltas
        self.assertIn("if ($displayComparability.status -ne 'same' -or [string]::Equals([string]$result.verdict, 'INCONCLUSIVE', [StringComparison]::Ordinal)) {", text)

    def _run_comparator(self, before_text: str, after_text: str) -> tuple[subprocess.CompletedProcess, dict | None]:
        with tempfile.TemporaryDirectory(prefix="gui-ab-entry-") as tmp:
            before, after, out = Path(tmp) / "before.json", Path(tmp) / "after.json", Path(tmp) / "out" / "compare.json"
            before.write_text(before_text, encoding="utf-8", newline="")
            after.write_text(after_text, encoding="utf-8", newline="")
            proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(COMPARE_SCRIPT),
                                   "-Before", str(before), "-After", str(after), "-Output", str(out)],
                                  capture_output=True, text=True, creationflags=_NO_WINDOW, timeout=300)
            report = json.loads(out.read_text(encoding="utf-8-sig")) if out.is_file() else None
        return proc, report

    def test_residue_b2_a_case_ambiguous_host_load_member_is_a_typed_inconclusive_not_a_throw(self) -> None:
        # VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-RESIDUE-1 r1 (sol r6 B2, its catalogue addition): a control JSON object with both hostLoad and HostLoad threw
        # KeysWithDifferentCasingInJsonString at the entry point's first parse, before the strict reader (which reads those bytes unknown) ran, so the
        # comparison aborted with no verdict and no report. Now: a typed failure, REFERENCE_PROVISIONAL leg=before, verdict INCONCLUSIVE, exit 1.
        quiet = '{"hostLoad":{"state":"quiet","provisional":false,"before":{"collected":true},"after":{"collected":true}}}'
        for name, control in (("null-and-object", '{"hostLoad":null,"HostLoad":{}}'),
                              ("two-quiet-spellings", '{"hostLoad":{"state":"quiet","provisional":false,"before":{"collected":true},"after":{"collected":true}},'
                                                      '"HostLoad":{"state":"quiet","provisional":false,"before":{"collected":true},"after":{"collected":true}}}')):
            with self.subTest(control=name):
                proc, report = self._run_comparator(control, quiet)
                self.assertNotIn("KeysWithDifferentCasing", proc.stderr, proc.stdout + proc.stderr)
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertIn("REFERENCE_PROVISIONAL leg=before reason=hostload-unknown", proc.stdout, proc.stdout + proc.stderr)
                self.assertIn("GUI-SMOKE-AB verdict=INCONCLUSIVE", proc.stdout, proc.stdout + proc.stderr)
                self.assertIsNotNone(report, "the report is written, not aborted")
                self.assertEqual(report["verdict"], "INCONCLUSIVE")
                self.assertTrue(any("before smoke JSON could not be parsed" in f for f in report["failures"]), report["failures"])

    def test_adversary_a4_the_same_file_twice_is_read_once_and_refused_in_the_report(self) -> None:
        # adversary-ac6205bd.json A4: -Before X -After X opened X twice (a lock between the reads aborted the report) and the distinct-path refusal was
        # never reached. The same path is read once and the report records the same-input refusal.
        quiet = '{"hostLoad":{"state":"quiet","provisional":false,"before":{"collected":true},"after":{"collected":true}}}'
        with tempfile.TemporaryDirectory(prefix="gui-ab-same-") as tmp:
            smoke, out = Path(tmp) / "smoke.json", Path(tmp) / "compare.json"
            smoke.write_text(quiet, encoding="utf-8", newline="")
            proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(COMPARE_SCRIPT),
                                   "-Before", str(smoke), "-After", str(smoke), "-Output", str(out)],
                                  capture_output=True, text=True, creationflags=_NO_WINDOW, timeout=300)
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            report = json.loads(out.read_text(encoding="utf-8-sig"))
        self.assertTrue(any("same smoke JSON file" in f for f in report["failures"]), report["failures"])
        # residue r2 (adversary-07b6a932.json A6): "the same file" is the open handle's identity, never the path spelling
        self.assertIn("$afterSource = Read-SmokeSourceOnce -Path $After -SameFileAs $beforeSource", COMPARE_SCRIPT.read_text(encoding="utf-8"))

    def test_adversary_a5_a_utf16_smoke_result_is_read_by_its_bom(self) -> None:
        # adversary-ac6205bd.json A5: a valid quiet result saved as UTF-16LE with its BOM was force-decoded as UTF-8 (replacement characters and NULs) and the
        # before arm read unknown. It is decoded by its BOM now, as Get-Content did, so its host load reads quiet.
        quiet = '{"hostLoad":{"state":"quiet","provisional":false,"before":{"collected":true},"after":{"collected":true}}}'
        with tempfile.TemporaryDirectory(prefix="gui-ab-utf16-") as tmp:
            before, after, out = Path(tmp) / "before.json", Path(tmp) / "after.json", Path(tmp) / "compare.json"
            before.write_bytes(b"\xff\xfe" + quiet.encode("utf-16-le"))
            after.write_text(quiet, encoding="utf-8", newline="")
            proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(COMPARE_SCRIPT),
                                   "-Before", str(before), "-After", str(after), "-Output", str(out)],
                                  capture_output=True, text=True, creationflags=_NO_WINDOW, timeout=300)
            report = json.loads(out.read_text(encoding="utf-8-sig"))
        self.assertEqual(report["hostLoadEvidence"]["before"]["state"], "quiet", proc.stdout + proc.stderr)
        self.assertNotIn("REFERENCE_PROVISIONAL leg=before", proc.stdout)
        self.assertFalse(any("before smoke JSON could not be parsed" in f for f in report["failures"]), report["failures"])
        # the venue readers decode receipts, specs and results by their BOM too (ConvertFrom-VenueBomBytes)
        probe = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
                                f"$m = Import-Module '{ROOT / 'tools' / 'profiling' / 'dual-venue' / 'VenueChain.psm1'}' -DisableNameChecking -PassThru; "
                                "$b = [byte[]](@(0xFF, 0xFE) + [Text.Encoding]::Unicode.GetBytes('{\"legType\":\"export\"}')); "
                                "Write-Output ('T=' + (& $m { param($x) ConvertFrom-VenueBomBytes $x } $b))"],
                               capture_output=True, text=True, creationflags=_NO_WINDOW, timeout=120)
        self.assertIn('T={"legType":"export"}', probe.stdout, probe.stdout + probe.stderr)
        venue = (ROOT / "tools" / "profiling" / "dual-venue" / "VenueChain.psm1").read_text(encoding="utf-8")
        self.assertNotIn("[Text.Encoding]::UTF8.GetString($bytes)", venue)
        self.assertNotIn("[Text.Encoding]::UTF8.GetString($rcBytes)", venue)
        baseline = (ROOT / "tools" / "profiling" / "dual-venue" / "Invoke-VenueBaseline.ps1").read_text(encoding="utf-8")
        # adversary-ac6205bd.json A7: an unclean chain's receipt is never opened for its host load either
        self.assertIn("$hlReceipt = $(if ($chainClean -and $leg.PSObject.Properties['receipt'] -and $leg.receipt)", baseline)

    def test_residue_b2_the_entry_point_reads_each_smoke_file_once(self) -> None:
        # sol r6 B2: the entry point opened each smoke file twice (Read-SmokeJson, then ReadAllText), and the evidence binding hashed it a third time,
        # so a lock taken between the reads aborted the report. One ReadAllBytes (Read-SmokeSourceOnce) now feeds the parse, the strict host-load
        # reader and the JSON's evidence binding.
        text = COMPARE_SCRIPT.read_text(encoding="utf-8")
        main = text[text.index("$beforeSource = Read-SmokeSourceOnce -Path $Before"):]
        self.assertEqual(main.count("Read-SmokeSourceOnce -Path"), 2)
        for reread in ("Read-SmokeJson -Path", "[IO.File]::ReadAllText(", "Get-Content -LiteralPath $before", "ConvertFrom-Json -Depth 100"):
            self.assertNotIn(reread, main, f"the entry point re-reads a smoke file: {reread}")
        self.assertIn("-JsonPath $beforeJsonPath -JsonSource $beforeSource", main)
        self.assertIn("-JsonPath $afterJsonPath -JsonSource $afterSource", main)
        once = _extract_functions(COMPARE_SCRIPT, ["Read-SmokeSourceOnce"])
        # residue r2 (A6): the one read is the open handle whose identity was judged (FileStream + CopyTo), never a second open by path
        self.assertEqual(once.count("[IO.FileStream]::new("), 1)
        self.assertEqual(once.count("$stream.CopyTo("), 1)
        self.assertNotIn("[IO.File]::ReadAllBytes(", once)

    def test_a_missing_or_exceeded_block_is_inconclusive_even_beside_other_failures(self) -> None:
        exceeded ="[pscustomobject]@{ hostLoad = [pscustomobject]@{ provisional = $true; state = 'exceeded'; reason = 'loaded'; " + SNAPS + " } }"
        proc = self._verdict("[pscustomobject]@{ }", exceeded, failures="@('before smoke did not independently pass validation.')")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("VERDICT=INCONCLUSIVE", proc.stdout, "never FAIL: a provisional comparison is no regression signal")
        self.assertIn("REFERENCE_PROVISIONAL leg=before reason=hostload-unknown", proc.stdout)
        self.assertIn("REFERENCE_PROVISIONAL leg=after reason=hostload-exceeded", proc.stdout)

    def test_c_two_quiet_legs_still_pass_and_a_real_failure_still_fails(self) -> None:
        proc = self._verdict(self.QUIET, self.QUIET)
        self.assertIn("VERDICT=PASS", proc.stdout, proc.stdout + proc.stderr)
        self.assertNotIn("REFERENCE_PROVISIONAL", proc.stdout)
        proc = self._verdict(self.QUIET, self.QUIET, failures="@('after smoke did not present at least two frames.')")
        self.assertIn("VERDICT=FAIL", proc.stdout, proc.stdout + proc.stderr)

    def test_the_script_takes_its_verdict_from_the_shared_function_and_no_longer_fails_on_host_load(self) -> None:
        text = COMPARE_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("$abVerdict = Get-GuiSmokeAbVerdict -Failures $failures -HostLoadEvidence $hostLoadEvidence", text)
        self.assertIn("verdict = $abVerdict.verdict", text)
        self.assertNotIn("$failures += @($hostLoadComparison.failures)", text, "a host-load refusal must not read FAIL")


@requires_pwsh
class CompareGuiSmokeAbHostLoadRefusalTests(_ProbeCase):
    """tools/profiling/compare-release-gui-smoke-ab.ps1's Get-HostLoadComparisonEvidence."""

    script = COMPARE_SCRIPT
    functions = ["Get-NestedValue", "Get-HostLoadComparisonEvidence"]
    preamble = COMPARE_PREAMBLE

    def _compare(self, before: str, after: str) -> subprocess.CompletedProcess:
        return self.run_snippet(
            f"$before = {before}\n"
            f"$after = {after}\n"
            "$r = Get-HostLoadComparisonEvidence -BeforeSmoke $before -AfterSmoke $after\n"
            "Write-Host \"FAILURES=$($r.failures.Count)\"\n"
            "Write-Host \"BEFORE_STATE=$($r.evidence.before.state)\"\n"
            "Write-Host \"AFTER_STATE=$($r.evidence.after.state)\"\n"
            "$r.failures | ForEach-Object { Write-Host \"FAILURE: $_\" }\n"
        )

    def test_two_quiet_legs_are_compared(self) -> None:
        quiet = "[pscustomobject]@{ hostLoad = [pscustomobject]@{ provisional = $false; state = 'quiet'; reason = $null; " + SNAPS + " } }"
        proc = self._compare(quiet, quiet)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("FAILURES=0", proc.stdout)

    def test_provisional_leg_vs_nonprovisional_leg_is_refused(self) -> None:
        exceeded = "[pscustomobject]@{ hostLoad = [pscustomobject]@{ provisional = $true; state = 'exceeded'; reason = 'CPU 96% on before' } }"
        quiet = "[pscustomobject]@{ hostLoad = [pscustomobject]@{ provisional = $false; state = 'quiet'; reason = $null; " + SNAPS + " } }"
        proc = self._compare(exceeded, quiet)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("FAILURES=0", proc.stdout)
        self.assertIn("before smoke host load is PROVISIONAL", proc.stdout)
        self.assertIn("refused", proc.stdout)

    def test_both_legs_provisional_is_also_refused(self) -> None:
        # Req: a provisional run is never usable as a regression signal -- full stop, regardless
        # of what it is being compared against, not only when paired with a non-provisional run.
        exceeded = "[pscustomobject]@{ hostLoad = [pscustomobject]@{ provisional = $true; state = 'exceeded'; reason = 'loaded' } }"
        proc = self._compare(exceeded, exceeded)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("FAILURES=2", proc.stdout)

    def test_missing_hostload_field_reads_as_unknown_not_quiet(self) -> None:
        # A run recorded before this gate existed (or an old schema) never measured load.
        legacy = "[pscustomobject]@{ schema = 'mlvapp-gui-smoke-result.v2' }"
        quiet = "[pscustomobject]@{ hostLoad = [pscustomobject]@{ provisional = $false; state = 'quiet'; reason = $null; " + SNAPS + " } }"
        proc = self._compare(legacy, quiet)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("BEFORE_STATE=unknown", proc.stdout)
        self.assertNotIn("FAILURES=0", proc.stdout)

    def test_hostload_block_present_but_missing_provisional_reads_as_provisional(self) -> None:
        # round 3 item 3: a hostLoad block that EXISTS but lacks the "provisional" property (a
        # degenerate/partial write, or a future schema drift) must not coerce [bool]$null to
        # $false and read as clean -- that is fail-toward-clean, backwards from this gate.
        degenerate = "[pscustomobject]@{ hostLoad = [pscustomobject]@{ state = 'exceeded' } }"
        quiet = "[pscustomobject]@{ hostLoad = [pscustomobject]@{ provisional = $false; state = 'quiet'; reason = $null; " + SNAPS + " } }"
        proc = self._compare(degenerate, quiet)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("FAILURES=0", proc.stdout)
        self.assertIn("before smoke host load is PROVISIONAL", proc.stdout)

    def test_provisional_false_with_missing_state_is_still_refused_not_read_as_clean_unknown(self) -> None:
        # round 4 (sol major), exact repro: "Pass before.hostLoad={provisional:false} and a normal
        # quiet after leg ...; the observed result is before.state=unknown, before.provisional=
        # false, failures=0." "provisional" and "state" were derived independently, so an explicit
        # provisional=false paired with a missing state read as an inconsistent, clean-reading
        # combination. state=unknown must now always force provisional=true.
        inconsistent = "[pscustomobject]@{ hostLoad = [pscustomobject]@{ provisional = $false } }"
        quiet = "[pscustomobject]@{ hostLoad = [pscustomobject]@{ provisional = $false; state = 'quiet'; reason = $null; " + SNAPS + " } }"
        proc = self._compare(inconsistent, quiet)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("BEFORE_STATE=unknown", proc.stdout)
        self.assertNotIn("FAILURES=0", proc.stdout)
        self.assertIn("before smoke host load is PROVISIONAL", proc.stdout)

    def test_provisional_false_with_exceeded_state_is_still_refused_not_read_as_clean(self) -> None:
        # round 8 (sol MAJOR item 3), exact repro: an internally inconsistent (or fabricated)
        # exceeded/false leg previously read clean here -- only state=="unknown" forced
        # provisional=true, so state=="exceeded" alongside provisional=false fell through as
        # FAILURES=0. Clean now requires state=="quiet" AND provisional==false.
        contradictory = "[pscustomobject]@{ hostLoad = [pscustomobject]@{ provisional = $false; state = 'exceeded'; reason = 'CPU 96%'; " + SNAPS + " } }"
        quiet = "[pscustomobject]@{ hostLoad = [pscustomobject]@{ provisional = $false; state = 'quiet'; reason = $null; " + SNAPS + " } }"
        proc = self._compare(contradictory, quiet)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("BEFORE_STATE=exceeded", proc.stdout)
        self.assertNotIn("FAILURES=0", proc.stdout)
        self.assertIn("before smoke host load is PROVISIONAL", proc.stdout)


@requires_pwsh
class SmokeSummaryHostLoadFieldsTests(_ProbeCase):
    """tools/profiling/run-release-cuda-playback-ab.ps1's Get-SmokeSummaryHostLoadFields --
    factored out of Read-SmokeSummary round 3 so the schema-drift-safety fix (item 3) is
    independently testable rather than only reachable through a full JSON-file read."""

    script = CUDA_AB_SCRIPT
    functions = ["Get-NestedValue", "Get-SmokeSummaryHostLoadFields"]

    def _fields(self, host_load: str) -> subprocess.CompletedProcess:
        return self.run_snippet(
            f"$hostLoad = {host_load}\n"
            "$f = Get-SmokeSummaryHostLoadFields -HostLoad $hostLoad\n"
            "Write-Host \"PROVISIONAL=$($f.provisional) STATE=$($f.state) REASON=$($f.reason)\"\n"
        )

    def test_missing_hostload_reads_as_unknown_provisional(self) -> None:
        proc = self._fields("$null")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PROVISIONAL=True STATE=unknown", proc.stdout)

    def test_hostload_present_but_missing_provisional_property_reads_as_provisional(self) -> None:
        proc = self._fields("[pscustomobject]@{ state = 'exceeded' }")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PROVISIONAL=True", proc.stdout)

    def test_hostload_present_but_missing_state_property_reads_as_unknown(self) -> None:
        proc = self._fields("[pscustomobject]@{ provisional = $false }")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STATE=unknown", proc.stdout)

    def test_well_formed_quiet_hostload_reads_through_untouched(self) -> None:
        # residue r1 (sol r6 B1): a well-formed quiet block carries its collected snapshots (the smoke always writes them)
        proc = self._fields(
            "[pscustomobject]@{ provisional = $false; state = 'quiet'; reason = $null; " + SNAPS + " }"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PROVISIONAL=False STATE=quiet", proc.stdout)

    def test_provisional_false_with_missing_state_reads_as_provisional_not_clean_unknown(self) -> None:
        # round 4 (sol major): an explicit provisional=false paired with a missing state used to
        # read as the inconsistent, clean-reading STATE=unknown PROVISIONAL=False.
        proc = self._fields("[pscustomobject]@{ provisional = $false }")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PROVISIONAL=True STATE=unknown", proc.stdout)

    def test_provisional_false_with_exceeded_state_reads_as_provisional_not_clean(self) -> None:
        # round 8 (sol MAJOR item 3), exact repro: an exceeded/false leg previously read clean here
        # -- only state=="unknown" forced provisional=true. Clean now requires state=="quiet" AND
        # provisional==false.
        proc = self._fields("[pscustomobject]@{ provisional = $false; state = 'exceeded' }")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PROVISIONAL=True STATE=exceeded", proc.stdout)

    def test_residue_b1_uncollected_coerced_and_case_folded_blocks_are_provisional(self) -> None:
        # VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-RESIDUE-1 r1 (sol r6 B1, its catalogue addition): state quiet, provisional false, before.collected=false
        # (CimException), after.collected=true read quiet/false here, so the A/B control produced no host-load refusal and its speed was evaluated.
        # Also provisional 0 / [false] / 'false' and state 'QUIET'. Each is provisional; only the strict quiet block (SNAPS) is clean.
        cases = {
            "uncollected": "[pscustomobject]@{ provisional = $false; state = 'quiet'; before = [pscustomobject]@{ collected = $false; error = 'collection failed: CimException' }; after = [pscustomobject]@{ collected = $true } }",
            "no-snapshots": "[pscustomobject]@{ provisional = $false; state = 'quiet' }",
            "collected-1": "[pscustomobject]@{ provisional = $false; state = 'quiet'; before = [pscustomobject]@{ collected = 1 }; after = [pscustomobject]@{ collected = $true } }",
            "zero": "[pscustomobject]@{ provisional = 0; state = 'quiet'; " + SNAPS + " }",
            "array": "[pscustomobject]@{ provisional = @($false); state = 'quiet'; " + SNAPS + " }",
            "string": "[pscustomobject]@{ provisional = 'false'; state = 'quiet'; " + SNAPS + " }",
            "upper": "[pscustomobject]@{ provisional = $false; state = 'QUIET'; " + SNAPS + " }",
            # adversary-ac6205bd.json A1: an uncollected interior sample, a null during and a during that is not a list
            "during-uncollected": "[pscustomobject]@{ provisional = $false; state = 'quiet'; " + SNAPS + "; during = @([pscustomobject]@{ collected = $false }) }",
            "during-null": "[pscustomobject]@{ provisional = $false; state = 'quiet'; " + SNAPS + "; during = $null }",
            "during-object": "[pscustomobject]@{ provisional = $false; state = 'quiet'; " + SNAPS + "; during = [pscustomobject]@{ collected = $true } }",
        }
        for name, block in cases.items():
            with self.subTest(case=name):
                proc = self._fields(block)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn("PROVISIONAL=True", proc.stdout, (name, proc.stdout))
        proc = self._fields("[pscustomobject]@{ provisional = $false; state = 'quiet'; " + SNAPS + "; during = @([pscustomobject]@{ collected = $true }) }")
        self.assertIn("PROVISIONAL=False", proc.stdout, "a collected during list is still clean")

    def test_adversary_a2_the_smoke_text_is_judged_by_the_venue_reader_and_read_without_unrolling(self) -> None:
        # adversary-ac6205bd.json A2: a singleton root array or a repeated member (before {"collected":false,"collected":true}) was parsed into a clean
        # object (ConvertFrom-Json unrolls the root and keeps the last duplicate). Read-SmokeSummary now parses with -NoEnumerate and also judges the TEXT
        # with VenueChain.psm1 ConvertFrom-VenueSmokeResultText; a standing it does not read clean makes the leg provisional and uncollected.
        source = CUDA_AB_SCRIPT.read_text(encoding="utf-8")
        body = _extract_functions(CUDA_AB_SCRIPT, ["Read-SmokeSummary"])
        self.assertIn("$json = ConvertFrom-Json -InputObject $smokeText -Depth 100 -NoEnumerate", body)
        self.assertIn("$textStanding = ConvertFrom-VenueSmokeResultText -Text $smokeText", body)
        self.assertIn("if ($textProvisional) {", body)
        self.assertNotIn("Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json", body)
        self.assertIn("-Function ConvertFrom-VenueSmokeResultText, Find-VenueJsonDuplicateMember", source)
        clean = '{"hostLoad":{"state":"quiet","provisional":false,"before":{"collected":true},"after":{"collected":true}}}'
        dup = '{"hostLoad":{"state":"quiet","provisional":false,"before":{"collected":false,"collected":true},"after":{"collected":true}}}'
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
                               f"Import-Module '{ROOT / 'tools' / 'profiling' / 'dual-venue' / 'VenueChain.psm1'}' -DisableNameChecking; "
                               f"foreach ($t in @('[{clean}]', '{dup}', '{clean}')) {{ $s = ConvertFrom-VenueSmokeResultText -Text $t; Write-Output ('S=' + $s.state + ',' + $s.provisional) }}"],
                              capture_output=True, text=True, creationflags=_NO_WINDOW, timeout=120)
        self.assertEqual([l for l in proc.stdout.splitlines() if l.startswith("S=")], ["S=unknown,True", "S=unknown,True", "S=quiet,False"], proc.stdout + proc.stderr)
        p3 = P3_VALIDATION_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("$resultTextStanding = ConvertFrom-VenueSmokeResultText -Text $resultText", p3)
        self.assertIn("$result = ConvertFrom-Json -InputObject $resultText -NoEnumerate", p3)
        self.assertIn("Find-VenueJsonDuplicateMember -Text $summaryText -All", p3, "an imported P3 packet that repeats a member is refused")
        self.assertIn("Find-VenueJsonDuplicateMember -Text $raw -All", COMPARE_MACHINE_PERF_SCRIPT.read_text(encoding="utf-8"))
        self.assertIn("Find-VenueJsonDuplicateMember -Text $summaryText -All", CUDA_PROOF_SUMMARIZER_SCRIPT.read_text(encoding="utf-8"))


@requires_pwsh
class CudaAbHostLoadProofFailuresStrictTests(_ProbeCase):
    """VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-RESIDUE-1 r1 (sol r6 B1): run-release-cuda-playback-ab.ps1's Get-HostLoadProofFailures (the A/B proof's host-load
    refusal, and since r1 the guard in front of the -RequireCandidateImprovesPresentedFps comparison) reads each leg summary through the ONE strict
    predicate: hostLoadState exactly 'quiet', hostLoadProvisional a real [bool] $false, hostLoadCollected a real [bool] $true. A truthy test of the flag
    let provisional 0 / [false] and an uncollected control through, and the speed comparison was evaluated on it."""

    script = CUDA_AB_SCRIPT
    functions = ["Get-HostLoadProofFailures"]
    CLEAN = "[pscustomobject]@{ hostLoadProvisional = $false; hostLoadState = 'quiet'; hostLoadCollected = $true }"

    def _failures(self, baseline: str) -> subprocess.CompletedProcess:
        return self.run_snippet(
            f"$b = {baseline}\n$c = {self.CLEAN}\n"
            "$f = @(Get-HostLoadProofFailures -BaselineSummary $b -CandidateSummary $c)\n"
            "Write-Host \"COUNT=$($f.Count)\"\n"
            "$f | ForEach-Object { Write-Host $_ }\n"
        )

    def test_a_clean_pair_has_no_refusal(self) -> None:
        proc = self._failures(self.CLEAN)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("COUNT=0", proc.stdout)

    def test_residue_b1_an_uncollected_coerced_or_case_folded_control_is_refused(self) -> None:
        cases = {
            "uncollected": "[pscustomobject]@{ hostLoadProvisional = $false; hostLoadState = 'quiet'; hostLoadCollected = $false }",
            "no-collected": "[pscustomobject]@{ hostLoadState = 'quiet'; hostLoadProvisional = $false }",
            "upper": "[pscustomobject]@{ hostLoadProvisional = $false; hostLoadState = 'QUIET'; hostLoadCollected = $true }",
            "zero": "[pscustomobject]@{ hostLoadProvisional = 0; hostLoadState = 'quiet'; hostLoadCollected = $true }",
            "array": "[pscustomobject]@{ hostLoadProvisional = @($false); hostLoadState = 'quiet'; hostLoadCollected = $true }",
        }
        for name, leg in cases.items():
            with self.subTest(case=name):
                proc = self._failures(leg)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn("COUNT=1", proc.stdout, (name, proc.stdout))
                self.assertIn("baseline-host-load-provisional", proc.stdout)

    def test_residue_b1_the_speed_comparison_is_guarded_by_the_same_strict_refusal(self) -> None:
        source = CUDA_AB_SCRIPT.read_text(encoding="utf-8")
        block_start = source.index("if ($RequireCandidateImprovesPresentedFps) {")
        block = source[block_start:source.index("\n}\n", block_start)]
        guard = block.index("Get-HostLoadProofFailures -BaselineSummary $baselineSummary -CandidateSummary $candidateForSpeed")
        self.assertLess(guard, block.index("$candidateFps -le $baselineFps"))
        self.assertNotIn("if ($baselineSummary.hostLoadProvisional -or", block, "no truthy test of the flag guards the comparison")
        # the summary carries the collected bit the strict predicate needs
        self.assertIn("hostLoadCollected = $hostLoadCollected", source)
        self.assertIn("$hostLoadCollected = $hostLoadFields.collected", source)


@requires_pwsh
class CudaPlaybackAbHostLoadGateTests(_ProbeCase):
    """tools/profiling/run-release-cuda-playback-ab.ps1's Get-HostLoadProofFailures."""

    script = CUDA_AB_SCRIPT
    functions = ["Get-HostLoadProofFailures"]

    def _summary(self, provisional: bool, state: str, reason: str = "null") -> str:
        reason_literal = "$null" if reason == "null" else f"'{reason}'"
        provisional_literal = "$true" if provisional else "$false"
        return (
            "[pscustomobject]@{ hostLoadProvisional = " + provisional_literal +
            "; hostLoadState = '" + state + "'; hostLoadReason = " + reason_literal +
            # residue r1 (sol r6 B1): Read-SmokeSummary now carries the collected bit the strict predicate needs
            "; hostLoadCollected = $true }"
        )

    def test_all_quiet_legs_pass_the_gate(self) -> None:
        quiet = self._summary(False, "quiet")
        proc = self.run_snippet(
            f"$baseline = {quiet}\n$candidate = {quiet}\n"
            "$f = Get-HostLoadProofFailures -BaselineSummary $baseline -CandidateSummary $candidate -CandidateSpeedSummary $null\n"
            "Write-Host \"FAILURES=$($f.Count)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("FAILURES=0", proc.stdout)

    def test_provisional_baseline_refuses_the_ab_proof(self) -> None:
        exceeded = self._summary(True, "exceeded", "CPU 96% on baseline")
        quiet = self._summary(False, "quiet")
        proc = self.run_snippet(
            f"$baseline = {exceeded}\n$candidate = {quiet}\n"
            "$f = Get-HostLoadProofFailures -BaselineSummary $baseline -CandidateSummary $candidate -CandidateSpeedSummary $null\n"
            "Write-Host \"FAILURES=$($f.Count)\"\n"
            "$f | ForEach-Object { Write-Host \"FAILURE: $_\" }\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("FAILURES=1", proc.stdout)
        self.assertIn("baseline-host-load-provisional", proc.stdout)

    def test_provisional_candidate_speed_leg_is_checked_only_when_requested(self) -> None:
        quiet = self._summary(False, "quiet")
        exceeded = self._summary(True, "exceeded", "loaded")
        proc = self.run_snippet(
            f"$baseline = {quiet}\n$candidate = {quiet}\n$speed = {exceeded}\n"
            "$fSkipped = Get-HostLoadProofFailures -BaselineSummary $baseline -CandidateSummary $candidate -CandidateSpeedSummary $speed\n"
            "Write-Host \"NOT_REQUESTED_FAILURES=$($fSkipped.Count)\"\n"
            "$fChecked = Get-HostLoadProofFailures -BaselineSummary $baseline -CandidateSummary $candidate -CandidateSpeedSummary $speed -SeparateCandidateSpeedRun\n"
            "Write-Host \"REQUESTED_FAILURES=$($fChecked.Count)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("NOT_REQUESTED_FAILURES=0", proc.stdout)
        self.assertIn("REQUESTED_FAILURES=1", proc.stdout)


@requires_pwsh
class PlaybackAbAnalysisHostLoadRefusalTests(_ProbeCase):
    """tools/profiling/run-release-cuda-playback-ab.ps1's New-PlaybackAbAnalysis -- round 3: this
    is the AUTHORITATIVE analysis writer embedded in the summary JSON and trusted verbatim
    downstream (compare-machine-perf.ps1's Get-ProofSummarySuggestion reads
    .analysis.suggestedOptimization directly), so the refusal must live here too, not only in
    compare-machine-perf.ps1's own recompute path."""

    script = CUDA_AB_SCRIPT
    functions = [
        "Convert-ToNullableDouble",
        "Get-NestedValue",
        "Get-CompareDeltaPercent",
        "Test-DeltaAtLeast",
        "New-PlaybackAbAnalysis",
    ]

    def test_host_load_provisional_proof_failure_refuses_the_bottleneck_diagnosis(self) -> None:
        # A large, clearly-actionable-looking fps regression must still be refused when the
        # ProofFailures say the baseline leg was host-load provisional -- the diagnosis would
        # otherwise be derived from noise, not from the candidate build.
        compare = (
            "[pscustomobject]@{ "
            "presentedFps = [pscustomobject]@{ deltaPercent = -40.0 }; "
            "avgQueueWaitMs = [pscustomobject]@{ deltaPercent = 30.0; candidate = 20.0 }; "
            "avgDrawTotalMs = [pscustomobject]@{ deltaPercent = 20.0; candidate = 5.0 } "
            "}"
        )
        proc = self.run_snippet(
            f"$compare = {compare}\n"
            "$proofFailures = @('baseline-host-load-provisional state=exceeded reason=CPU 96%')\n"
            "$a = New-PlaybackAbAnalysis -Compare $compare -ProofFailures $proofFailures -ComparisonBasis 'candidate'\n"
            "Write-Host \"DOMINANT=$($a.dominantBottleneck) SUGGESTION=$($a.suggestedOptimization) "
            "CONFIDENCE=$($a.confidence)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("DOMINANT=host-load-provisional", proc.stdout)
        self.assertIn("SUGGESTION=rerun_playback_ab_with_quiet_host", proc.stdout)
        self.assertNotIn("DOMINANT=present-bound", proc.stdout)

    def test_non_host_load_proof_failure_does_not_trigger_the_host_load_refusal(self) -> None:
        # A different kind of proof failure (e.g. GL parity) must not be mislabeled as a
        # host-load problem -- the delta-based diagnosis should still run normally.
        compare = "[pscustomobject]@{ presentedFps = [pscustomobject]@{ deltaPercent = 20.0 } }"
        proc = self.run_snippet(
            f"$compare = {compare}\n"
            "$proofFailures = @('candidate-gl-parity-mismatches=3')\n"
            "$a = New-PlaybackAbAnalysis -Compare $compare -ProofFailures $proofFailures -ComparisonBasis 'candidate'\n"
            "Write-Host \"DOMINANT=$($a.dominantBottleneck)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("DOMINANT=host-load-provisional", proc.stdout)


class RunReleaseCudaPlaybackAbWiresTheGateInTests(unittest.TestCase):
    """Static check: the main script body must actually call the extracted gate function."""

    def test_proof_failures_are_extended_with_the_host_load_gate(self) -> None:
        source = CUDA_AB_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("$proofFailures += @(Get-HostLoadProofFailures", source)
        self.assertIn("-BaselineSummary $baselineSummary", source)
        self.assertIn("-CandidateSummary $candidateSummary", source)


@requires_pwsh
class CompareMachinePerfPlaybackAbHostLoadRefusalTests(_ProbeCase):
    """tools/profiling/compare-machine-perf.ps1's Get-PlaybackAbHostLoadRefusal and
    Get-PlaybackAbAnalysis -- round 3 item 2: this cross-machine/cross-run comparison tool must
    refuse a derived bottleneck diagnosis for a provisional leg, not just display the numbers
    next to a status flag nobody is required to read."""

    script = COMPARE_MACHINE_PERF_SCRIPT
    functions = [
        "Convert-ToNullableDouble",
        "Get-PlaybackAbLegHostLoadProvisional",
        "Get-PlaybackAbHostLoadRefusal",
        "Get-CompareDeltaPercent",
        "Test-DeltaAtLeast",
        "Get-PlaybackAbAnalysis",
    ]

    def _leg(self, provisional: bool, state: str = None) -> str:
        # round 5: legs are now built WITH hostLoadState by default (as every real round-4+
        # producer emits it alongside hostLoadProvisional) -- state defaults to the value implied
        # by provisional so callers that only care about the boolean keep working unchanged.
        resolved_state = state if state is not None else ("exceeded" if provisional else "quiet")
        provisional_literal = "$true" if provisional else "$false"
        return (
            "[pscustomobject]@{ hostLoadProvisional = " + provisional_literal +
            "; hostLoadState = '" + resolved_state + "'; hostLoadCollected = $true }"
        )

    def _record(self, baseline_provisional: bool, candidate_provisional: bool, fps_delta_pct: float = -40.0) -> str:
        baseline = self._leg(baseline_provisional)
        candidate = self._leg(candidate_provisional)
        return (
            "[pscustomobject]@{ "
            f"baseline = {baseline}; candidate = {candidate}; candidateSpeed = $null; "
            f"compare = [pscustomobject]@{{ presentedFps = [pscustomobject]@{{ deltaPercent = {fps_delta_pct} }} }} "
            "}"
        )

    def test_provisional_baseline_refuses_the_bottleneck_diagnosis(self) -> None:
        proc = self.run_snippet(
            f"$record = {self._record(True, False)}\n"
            "$a = Get-PlaybackAbAnalysis -Record $record\n"
            "Write-Host \"DOMINANT=$($a.dominantBottleneck) SUGGESTION=$($a.suggestedOptimization)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("DOMINANT=host-load-provisional", proc.stdout)
        self.assertIn("SUGGESTION=rerun_playback_ab_with_quiet_host", proc.stdout)

    def test_provisional_candidate_also_refuses(self) -> None:
        proc = self.run_snippet(
            f"$record = {self._record(False, True)}\n"
            "$a = Get-PlaybackAbAnalysis -Record $record\n"
            "Write-Host \"DOMINANT=$($a.dominantBottleneck)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("DOMINANT=host-load-provisional", proc.stdout)

    def test_quiet_legs_still_get_a_real_diagnosis(self) -> None:
        proc = self.run_snippet(
            f"$record = {self._record(False, False)}\n"
            "$a = Get-PlaybackAbAnalysis -Record $record\n"
            "Write-Host \"DOMINANT=$($a.dominantBottleneck)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("DOMINANT=host-load-provisional", proc.stdout)

    def test_leg_missing_hostloadprovisional_property_is_treated_as_provisional(self) -> None:
        # Same fail-toward-provisional stance as item 3: a leg object present but lacking the
        # property entirely (older/degenerate record) must not coerce to clean.
        record = (
            "[pscustomobject]@{ baseline = [pscustomobject]@{}; "
            f"candidate = {self._leg(False)}; candidateSpeed = $null; "
            "compare = [pscustomobject]@{ presentedFps = [pscustomobject]@{ deltaPercent = -40.0 } } }"
        )
        proc = self.run_snippet(
            f"$record = {record}\n"
            "$r = Get-PlaybackAbHostLoadRefusal -Record $record\n"
            "Write-Host \"PROVISIONAL=$($r.provisional) BASELINE=$($r.baselineProvisional) CANDIDATE=$($r.candidateProvisional)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PROVISIONAL=True BASELINE=True CANDIDATE=False", proc.stdout)

    def test_provisional_false_with_unknown_state_is_still_refused_not_read_as_clean(self) -> None:
        # round 5 (sol BLOCKER), exact repro: "a baseline leg with hostLoadProvisional=false and
        # hostLoadState='unknown' plus a quiet candidate gives Get-PlaybackAbHostLoadRefusal ->
        # refusal False." round 4 fixed this same inconsistency at the three PRODUCER
        # normalizers; this consumer read the flag alone, so a legacy/degenerate leg still entered
        # a cross-machine comparison as clean.
        record = (
            "[pscustomobject]@{ "
            "baseline = [pscustomobject]@{ hostLoadProvisional = $false; hostLoadState = 'unknown' }; "
            f"candidate = {self._leg(False)}; candidateSpeed = $null; "
            "compare = [pscustomobject]@{ presentedFps = [pscustomobject]@{ deltaPercent = -40.0 } } }"
        )
        proc = self.run_snippet(
            f"$record = {record}\n"
            "$r = Get-PlaybackAbHostLoadRefusal -Record $record\n"
            "Write-Host \"PROVISIONAL=$($r.provisional) BASELINE=$($r.baselineProvisional)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PROVISIONAL=True BASELINE=True", proc.stdout)

    def test_provisional_false_with_missing_state_property_is_still_refused(self) -> None:
        # Same repro, but hostLoadState is entirely ABSENT (not the string 'unknown') -- the
        # legacy-record case round 4's producer-side fix and this consumer-side fix both name.
        record = (
            "[pscustomobject]@{ "
            "baseline = [pscustomobject]@{ hostLoadProvisional = $false }; "
            f"candidate = {self._leg(False)}; candidateSpeed = $null; "
            "compare = [pscustomobject]@{ presentedFps = [pscustomobject]@{ deltaPercent = -40.0 } } }"
        )
        proc = self.run_snippet(
            f"$record = {record}\n"
            "$r = Get-PlaybackAbHostLoadRefusal -Record $record\n"
            "Write-Host \"PROVISIONAL=$($r.provisional) BASELINE=$($r.baselineProvisional)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PROVISIONAL=True BASELINE=True", proc.stdout)

    def test_provisional_false_with_exceeded_state_is_still_refused_not_read_as_clean(self) -> None:
        # round 8 (sol MAJOR item 3), exact repro: an internally inconsistent exceeded/false leg
        # previously read clean here -- only hostLoadState=="unknown" forced provisional=true.
        # Clean now requires hostLoadState=="quiet" AND hostLoadProvisional==false.
        record = (
            "[pscustomobject]@{ "
            "baseline = [pscustomobject]@{ hostLoadProvisional = $false; hostLoadState = 'exceeded' }; "
            f"candidate = {self._leg(False)}; candidateSpeed = $null; "
            "compare = [pscustomobject]@{ presentedFps = [pscustomobject]@{ deltaPercent = -40.0 } } }"
        )
        proc = self.run_snippet(
            f"$record = {record}\n"
            "$r = Get-PlaybackAbHostLoadRefusal -Record $record\n"
            "Write-Host \"PROVISIONAL=$($r.provisional) BASELINE=$($r.baselineProvisional)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PROVISIONAL=True BASELINE=True", proc.stdout)


@requires_pwsh
class CompareMachinePerfPlaybackAbFpsDeltaRefusalTests(_ProbeCase):
    """tools/profiling/compare-machine-perf.ps1's New-PlaybackAbSummaryRow -- round 7 (both keys
    MAJOR -- "a provisional pair must not publish an fps delta"): playback_fps_delta_pct is a
    CROSS-LEG comparison, unlike baseline_presented_fps/presented_fps (each a single leg's OWN
    measurement, not a comparison, and deliberately left visible for either leg alone -- round 3's
    original stance, unchanged here). A pair where either leg's host load was provisional
    (exceeded or unmeasurable) must refuse the delta to $null with an explicit reason, the same
    way Get-PlaybackAbAnalysis already refuses the derived dominant_bottleneck."""

    script = COMPARE_MACHINE_PERF_SCRIPT
    functions = [
        "Convert-ToNullableDouble",
        "Get-MachineLabel",
        "Assert-MachineFingerprint",
        "Get-PlaybackAbLegHostLoadProvisional",
        "Get-PlaybackAbHostLoadRefusal",
        "Get-CompareDeltaPercent",
        "Test-DeltaAtLeast",
        "Get-PlaybackAbAnalysis",
        "New-PlaybackAbSummaryRow",
    ]

    def _leg(self, provisional: bool, state: str = None) -> str:
        resolved_state = state if state is not None else ("exceeded" if provisional else "quiet")
        provisional_literal = "$true" if provisional else "$false"
        return (
            "[pscustomobject]@{ hostLoadProvisional = " + provisional_literal +
            "; hostLoadState = '" + resolved_state + "'; hostLoadCollected = $true }"
        )

    def _record(
        self, baseline_provisional: bool, candidate_provisional: bool,
        baseline_fps: float = 10.0, candidate_fps: float = 20.0, delta_pct: float = 100.0,
    ) -> str:
        baseline_leg = self._leg(baseline_provisional)
        candidate_leg = self._leg(candidate_provisional)
        return (
            "[pscustomobject]@{ schema = 'mlvapp-cuda-playback-ab.v1'; status = 'success'; "
            "machineFingerprint = [pscustomobject]@{ schema = 'machine-fingerprint.v1'; "
            "hostname = 'H'; cpu = 'x'; gpu = 'y'; os = 'z'; build_sha = 'abc1234' }; "
            f"baseline = {baseline_leg}; candidate = {candidate_leg}; candidateSpeed = $null; "
            "compare = [pscustomobject]@{ presentedFps = [pscustomobject]@{ "
            f"baseline = {baseline_fps}; candidate = {candidate_fps}; deltaPercent = {delta_pct} }} }} }}"
        )

    def _row(self, record: str) -> subprocess.CompletedProcess:
        return self.run_snippet(
            f"$record = {record}\n"
            "$r = New-PlaybackAbSummaryRow -Record $record -Source 'test'\n"
            "Write-Host \"BASELINE_FPS=$($r.baseline_presented_fps) CANDIDATE_FPS=$($r.presented_fps) "
            "DELTA=$($r.playback_fps_delta_pct) REASON=$($r.playback_fps_delta_refused_reason) "
            "PROVISIONAL=$($r.host_load_provisional)\"\n"
        )

    def test_provisional_baseline_refuses_the_delta_but_keeps_per_leg_fps(self) -> None:
        proc = self._row(self._record(baseline_provisional=True, candidate_provisional=False))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("BASELINE_FPS=10", proc.stdout)
        self.assertIn("CANDIDATE_FPS=20", proc.stdout)
        self.assertIn("DELTA= REASON=host_load_provisional:", proc.stdout)
        self.assertIn("PROVISIONAL=True", proc.stdout)

    def test_provisional_candidate_also_refuses_the_delta(self) -> None:
        proc = self._row(self._record(baseline_provisional=False, candidate_provisional=True))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("DELTA= REASON=host_load_provisional:", proc.stdout)

    def test_clean_pair_still_publishes_a_real_delta(self) -> None:
        proc = self._row(self._record(baseline_provisional=False, candidate_provisional=False))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("DELTA=100", proc.stdout)
        self.assertIn("REASON=", proc.stdout)
        self.assertNotIn("REASON=host_load_provisional", proc.stdout)
        self.assertIn("PROVISIONAL=False", proc.stdout)

    def test_exceeded_false_baseline_still_refuses_the_delta(self) -> None:
        # round 8 (sol MAJOR item 3), exact repro: "baseline hostLoadState='exceeded',
        # hostLoadProvisional=false; candidate quiet/false; FPS 10 vs 20, deltaPercent=100" used to
        # publish host_load_provisional=false, playback_fps_delta_pct=100, no refusal reason.
        record = (
            "[pscustomobject]@{ schema = 'mlvapp-cuda-playback-ab.v1'; status = 'success'; "
            "machineFingerprint = [pscustomobject]@{ schema = 'machine-fingerprint.v1'; "
            "hostname = 'H'; cpu = 'x'; gpu = 'y'; os = 'z'; build_sha = 'abc1234' }; "
            "baseline = [pscustomobject]@{ hostLoadProvisional = $false; hostLoadState = 'exceeded' }; "
            f"candidate = {self._leg(False)}; candidateSpeed = $null; "
            "compare = [pscustomobject]@{ presentedFps = [pscustomobject]@{ "
            "baseline = 10.0; candidate = 20.0; deltaPercent = 100.0 } } }"
        )
        proc = self._row(record)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("DELTA= REASON=host_load_provisional:", proc.stdout)
        self.assertIn("PROVISIONAL=True", proc.stdout)


class CompareMachinePerfHostLoadWiringTests(unittest.TestCase):
    """Static structural checks: the human-facing table and JSON rows must actually carry
    host_load_provisional, not just the underlying analysis function."""

    def test_playback_ab_and_local_proof_rows_carry_host_load_provisional(self) -> None:
        source = COMPARE_MACHINE_PERF_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("host_load_provisional = $hostLoadRefusal.provisional", source)
        self.assertIn("host_load_provisional = $hostLoadProvisional", source)
        self.assertIn("host_load_provisional,", source)

    def test_proof_summary_suggestion_refuses_ahead_of_trusting_embedded_analysis(self) -> None:
        source = COMPARE_MACHINE_PERF_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("rerun_playback_ab_with_quiet_host", source)
        refusal_check = source.index("(Get-PlaybackAbHostLoadRefusal -Record $playbackAb).provisional")
        trusts_embedded_analysis = source.index("$playbackAb.analysis -and $playbackAb.analysis.suggestedOptimization")
        self.assertLess(
            refusal_check, trusts_embedded_analysis,
            "the host-load refusal must be checked before trusting the record's own cached analysis",
        )


class CompareMachinePerfRowHostLoadCensusTests(unittest.TestCase):
    """round 5 (sol MAJOR #4), fourth round of this finding: "stop enumerating and start
    enforcing." Prior rounds fixed each unbound New-*Row function individually as it was found
    (a nine-row sweep written up in prose in a fleet-run summary, per sol's round-4 review) --
    nothing stopped a TENTH row function from making the same mistake. This test scans every
    New-*Row function actually defined in compare-machine-perf.ps1 (not a hand-maintained list of
    names) and fails if any of them can emit a real (non-hardcoded-null) presented_fps without
    also carrying host_load_provisional in the same object literal, unless the function is named
    in TELEMETRY_ONLY_ROW_FUNCTIONS with a reason.

    Binds today: New-RemoteP3SummaryRow, New-ProfileRow, New-FieldLogRow, New-LocalProofSummaryRow,
    New-PlaybackAbSummaryRow (every New-*Row function that can emit a real presented_fps).
    Allowlisted: New-RemoteCdngSummaryRow (hardcodes presented_fps = $null -- CDNG export has no
    playback-fps signal to rank at all).

    Cannot bind: a brand-new STANDALONE script elsewhere under tools/profiling that independently
    parses smoke JSON and computes its own fps ranking outside this file entirely -- that would
    need a repo-wide PowerShell-aware scan this regex-based census does not attempt. fable's Q3
    named exactly this class of exception for run-local-cuda-playback-dng-smoke.ps1 and
    detect-playback-artifacts.ps1 (both project telemetry only, never rank or gate).

    round 6 (sol + astra MAJOR -- "enforcement tests check TOKENS, not behaviour"): astra's exact
    repro against the round-5 census -- "append a New-*Row with presented_fps=99 and
    host_load_provisional=$false, or only a comment containing host_load_provisional; the actual
    census passes both." A truly general executable test is not possible here (the whole point of
    this census is to catch an UNKNOWN future row function, so there is no fixed signature to call
    it with) -- instead the detector itself is hardened against exactly astra's two repro shapes:
    comment lines are stripped before any match (a mention inside a `#...` comment no longer counts
    as binding), and a LITERAL `host_load_provisional = $false` is treated as UNBOUND rather than
    bound (every real row in this file that hardcodes the flag hardcodes $true -- "always
    unrecorded", the safe direction -- never $false; see New-ProfileRow/New-FieldLogRow below).
    Both new detection rules are proven against synthetic fixtures below, the same way the
    round-5 tautology check already proved the base case."""

    # Extraction pattern deliberately mirrors _extract_functions above (same file, same
    # column-0-closing-brace convention this codebase already relies on for verbatim splicing).
    ROW_FUNCTION_PATTERN = re.compile(r"function (New-\w*Row) \{.*?\n\}\r?\n", re.DOTALL)
    REAL_FPS_PATTERN = re.compile(r"presented_fps\s*=(?!\s*\$null\b)")
    # round 6: a literal `= $false` (not a variable/expression) is the exact hardcoded-clean-flag
    # shape astra's repro used -- every legitimate hardcode in this file uses $true (fail-toward-
    # provisional), so a hardcoded $false is itself evidence of an unbound/fabricated row.
    #
    # round 8 (sol MAJOR item 4, carried from round-7's disclosed residual): the literal-`$false`
    # match was itself only ONE textual shape -- sol's exact repro replaces the assignment with
    # `host_load_provisional = ($false)` (a parenthesized wrap) and the bare-`\$false\b` regex does
    # not match it (the character immediately after `=\s*` is `(`, not `$`), so the row still
    # censused as bound. HARDCODED_FALSE_PATTERN is kept below only as the OLD naive detector, for
    # the contrast assertion in test_a_parenthesized_hardcoded_false_would_fail_this_census (proving
    # the new detector catches what the old one missed) -- it is no longer used by _is_bound itself.
    #
    # round 9 (sol MAJOR item 2): sol's exact repro against the round-8 detector -- "replace the
    # real `host_load_provisional = $true` with `host_load_provisional = 0`" -- still passed,
    # because round 8 only BLACKLISTED specific known-false shapes and let everything else through.
    # A bare `0`, `$null`, `''`, or `'false'` are none of those shapes, so all four sailed through
    # as "derived". INVERTED per the round-9 brief: a row is bound only if its RHS, once normalized
    # (balanced-outer-parens stripped, whitespace removed, case-folded), is EXACTLY the literal
    # `$true`, or is not recognized as any kind of hardcoded constant at all (assumed derived --
    # a variable, property access, function call, or ternary that genuinely reads host-load state).
    # Any RECOGNIZED constant that is not `$true` -- $false, $null, an empty or non-empty quoted
    # string, a bare number, a `[bool]` numeric cast, or a negation of $true/$false -- is unbound,
    # whatever its value. This closes the whole family in one move instead of enumerating more
    # false-shaped literals one repro at a time.
    HARDCODED_FALSE_PATTERN = re.compile(r"host_load_provisional\s*=\s*\$false\b")
    ASSIGNMENT_RHS_PATTERN = re.compile(r"host_load_provisional\s*=\s*([^\n]*)")
    _TRUE_LITERAL = "$true"
    _CONSTANT_SIMPLE_TOKENS = {"$false", "$null", "-not$true", "-not$false", "!$true", "!$false"}
    _CONSTANT_NUMERIC_PATTERN = re.compile(r"^-?\d+(\.\d+)?$")
    _CONSTANT_BOOL_CAST_PATTERN = re.compile(r"^\[bool\]-?\d+(\.\d+)?$")
    # Single-quoted strings never interpolate in PowerShell, so any content is a genuine constant.
    # Double-quoted strings are only treated as constant when they contain no `$` -- a `"$var"`
    # shape is (potentially) derived via interpolation and must not be misclassified as hardcoded.
    _CONSTANT_SINGLE_QUOTED_STRING_PATTERN = re.compile(r"^'[^']*'$")
    _CONSTANT_DOUBLE_QUOTED_STRING_PATTERN = re.compile(r'^"[^"$]*"$')

    TELEMETRY_ONLY_ROW_FUNCTIONS = {"New-RemoteCdngSummaryRow"}

    @staticmethod
    def _strip_comments(text: str) -> str:
        # round 6: strips a `#...` trailing or whole-line comment from every line so a mention of
        # host_load_provisional that exists ONLY in a comment (astra's second repro shape) cannot
        # satisfy the census.
        return "\n".join(re.sub(r"#.*$", "", line) for line in text.splitlines())

    @staticmethod
    def _strip_balanced_outer_parens(text: str) -> str:
        # round 8: "($false)" -> "$false", "((-not $true))" -> "-not $true" -- but "(a)+(b)" is
        # left alone (its outer characters are parens, but they do not wrap the WHOLE expression as
        # one balanced group, so stripping them would change meaning).
        while text.startswith("(") and text.endswith(")"):
            depth = 0
            wraps_whole_expression = True
            for ch in text[:-1]:
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        wraps_whole_expression = False
                        break
            if not wraps_whole_expression:
                break
            text = text[1:-1].strip()
        return text

    def _classify_rhs(self, rhs: str) -> str:
        """Returns "true" (the one hardcode this census accepts), "constant" (any OTHER
        hardcoded literal -- unbound regardless of its value), or "derived" (not a recognized
        constant shape at all -- trusted as genuine host-load provenance, same as round 8)."""
        stripped = self._strip_balanced_outer_parens(rhs.strip())
        compact = re.sub(r"\s+", "", stripped).lower()
        if compact == self._TRUE_LITERAL:
            return "true"
        if (
            compact in self._CONSTANT_SIMPLE_TOKENS
            or self._CONSTANT_NUMERIC_PATTERN.match(compact)
            or self._CONSTANT_BOOL_CAST_PATTERN.match(compact)
            or self._CONSTANT_SINGLE_QUOTED_STRING_PATTERN.match(stripped)
            or self._CONSTANT_DOUBLE_QUOTED_STRING_PATTERN.match(stripped)
        ):
            return "constant"
        return "derived"

    def _is_bound(self, body: str) -> bool:
        code_only = self._strip_comments(body)
        if "host_load_provisional" not in code_only:
            return False
        match = self.ASSIGNMENT_RHS_PATTERN.search(code_only)
        if not match:
            return False
        return self._classify_rhs(match.group(1)) in ("true", "derived")

    def test_every_row_function_with_a_real_presented_fps_carries_host_load_provenance(self) -> None:
        source = COMPARE_MACHINE_PERF_SCRIPT.read_text(encoding="utf-8")
        matches = list(self.ROW_FUNCTION_PATTERN.finditer(source))
        self.assertGreater(len(matches), 0, "no New-*Row functions found -- the census pattern is stale")
        found_names = {match.group(1) for match in matches}
        bound = []
        unbound = []
        for match in matches:
            name = match.group(1)
            body = match.group(0)
            if name in self.TELEMETRY_ONLY_ROW_FUNCTIONS:
                self.assertIn(
                    "presented_fps = $null", body,
                    f"{name} is on TELEMETRY_ONLY_ROW_FUNCTIONS but its presented_fps is no "
                    "longer hardcoded null -- it now needs host_load_provisional, or removal "
                    "from the allowlist.",
                )
                continue
            if self.REAL_FPS_PATTERN.search(self._strip_comments(body)):
                (bound if self._is_bound(body) else unbound).append(name)
        self.assertEqual(
            unbound, [],
            "New-*Row function(s) in compare-machine-perf.ps1 emit a real presented_fps without "
            f"genuine host_load_provisional provenance: {unbound}. Either add host_load_provisional "
            "to the row (not a hardcoded $false, and not just a comment), or add the function name "
            "to TELEMETRY_ONLY_ROW_FUNCTIONS with a reason if it genuinely never carries an fps "
            "signal to gate.",
        )
        # Round-5 fix regression guard: New-ProfileRow and New-FieldLogRow were the two unbound
        # consumers sol's finding named -- if the census pattern stops matching them, this test
        # would silently stop covering the exact defect it exists to catch.
        self.assertIn("New-ProfileRow", found_names)
        self.assertIn("New-FieldLogRow", found_names)
        self.assertIn("New-ProfileRow", bound)
        self.assertIn("New-FieldLogRow", bound)

    def test_a_hypothetical_unbound_consumer_would_fail_this_census(self) -> None:
        # Proves the census is a real detector, not a tautology: a synthetic row function shaped
        # exactly like the round-5 defect (real presented_fps, no host_load_provisional) must be
        # caught by the same regex this test class applies to the real file.
        fixture = (
            "function New-HypotheticalRow {\n"
            "    param([object]$Record)\n"
            "    [pscustomobject]@{\n"
            "        presented_fps = if ($null -ne $Record.fps) { $Record.fps } else { $null }\n"
            "        source = 'test'\n"
            "    }\n"
            "}\n"
        )
        matches = list(self.ROW_FUNCTION_PATTERN.finditer(fixture))
        self.assertEqual(len(matches), 1)
        body = matches[0].group(0)
        self.assertTrue(self.REAL_FPS_PATTERN.search(body))
        self.assertFalse(self._is_bound(body))

    def test_a_hardcoded_false_provisional_flag_would_fail_this_census(self) -> None:
        # round 6 (astra's exact repro): "append a New-*Row with presented_fps=99 and
        # host_load_provisional=$false" -- the OLD census (bare substring presence) would have
        # classified this as bound purely because the token appears; it must now be caught as
        # UNBOUND because the flag is a hardcoded clean claim, not derived provenance.
        fixture = (
            "function New-BypassRow {\n"
            "    param([object]$Record)\n"
            "    [pscustomobject]@{\n"
            "        presented_fps = 99.0\n"
            "        host_load_provisional = $false\n"
            "    }\n"
            "}\n"
        )
        matches = list(self.ROW_FUNCTION_PATTERN.finditer(fixture))
        self.assertEqual(len(matches), 1)
        body = matches[0].group(0)
        self.assertTrue(self.REAL_FPS_PATTERN.search(body))
        self.assertIn("host_load_provisional", body)  # the OLD substring-presence check would pass
        self.assertFalse(self._is_bound(body))  # the round-6 detector must still refuse it

    def test_a_parenthesized_hardcoded_false_would_fail_this_census(self) -> None:
        # round 8 (sol MAJOR item 4, exact repro): "replace the first host_load_provisional = $true
        # with host_load_provisional = ($false)" -- the round-6 literal-`\$false\b` regex does not
        # match text starting with `(`, so this shape passed the prior census unbound.
        fixture = (
            "function New-BypassRow {\n"
            "    param([object]$Record)\n"
            "    [pscustomobject]@{\n"
            "        presented_fps = 99.0\n"
            "        host_load_provisional = ($false)\n"
            "    }\n"
            "}\n"
        )
        matches = list(self.ROW_FUNCTION_PATTERN.finditer(fixture))
        self.assertEqual(len(matches), 1)
        body = matches[0].group(0)
        self.assertTrue(self.REAL_FPS_PATTERN.search(body))
        self.assertFalse(self.HARDCODED_FALSE_PATTERN.search(self._strip_comments(body)))  # old regex misses it
        self.assertFalse(self._is_bound(body))  # the round-8 expression-aware detector must catch it

    def test_a_not_true_hardcoded_false_would_fail_this_census(self) -> None:
        # round 8 (sol MAJOR item 4): `(-not $true)` is another constant-false shape with no
        # literal `$false` token anywhere in it.
        fixture = (
            "function New-BypassRow {\n"
            "    param([object]$Record)\n"
            "    [pscustomobject]@{\n"
            "        presented_fps = 99.0\n"
            "        host_load_provisional = (-not $true)\n"
            "    }\n"
            "}\n"
        )
        matches = list(self.ROW_FUNCTION_PATTERN.finditer(fixture))
        self.assertEqual(len(matches), 1)
        body = matches[0].group(0)
        self.assertTrue(self.REAL_FPS_PATTERN.search(body))
        self.assertFalse(self._is_bound(body))

    def test_a_bool_cast_zero_hardcoded_false_would_fail_this_census(self) -> None:
        # round 8 (sol MAJOR item 4): `[bool]0` is a third constant-false shape with no `$false`
        # token in it either.
        fixture = (
            "function New-BypassRow {\n"
            "    param([object]$Record)\n"
            "    [pscustomobject]@{\n"
            "        presented_fps = 99.0\n"
            "        host_load_provisional = [bool]0\n"
            "    }\n"
            "}\n"
        )
        matches = list(self.ROW_FUNCTION_PATTERN.finditer(fixture))
        self.assertEqual(len(matches), 1)
        body = matches[0].group(0)
        self.assertTrue(self.REAL_FPS_PATTERN.search(body))
        self.assertFalse(self._is_bound(body))

    def test_a_bare_zero_hardcoded_false_would_fail_this_census(self) -> None:
        # round 9 (sol MAJOR item 2), sol's EXACT repro: "replace the real
        # `host_load_provisional = $true` with `host_load_provisional = 0`" -- a smaller edit than
        # any round-8-caught shape, and it passed the round-8 blacklist-based detector because a
        # bare `0` matched none of round 8's specific known-false patterns.
        fixture = (
            "function New-BypassRow {\n"
            "    param([object]$Record)\n"
            "    [pscustomobject]@{\n"
            "        presented_fps = 99.0\n"
            "        host_load_provisional = 0\n"
            "    }\n"
            "}\n"
        )
        matches = list(self.ROW_FUNCTION_PATTERN.finditer(fixture))
        self.assertEqual(len(matches), 1)
        body = matches[0].group(0)
        self.assertTrue(self.REAL_FPS_PATTERN.search(body))
        self.assertFalse(self._is_bound(body))

    def test_a_null_hardcoded_false_would_fail_this_census(self) -> None:
        # round 9 (sol MAJOR item 2): $null is truthy-adjacent nonsense for a boolean-shaped flag,
        # but it is still a hardcoded constant, not derived provenance -- must be unbound.
        fixture = (
            "function New-BypassRow {\n"
            "    param([object]$Record)\n"
            "    [pscustomobject]@{\n"
            "        presented_fps = 99.0\n"
            "        host_load_provisional = $null\n"
            "    }\n"
            "}\n"
        )
        matches = list(self.ROW_FUNCTION_PATTERN.finditer(fixture))
        self.assertEqual(len(matches), 1)
        body = matches[0].group(0)
        self.assertTrue(self.REAL_FPS_PATTERN.search(body))
        self.assertFalse(self._is_bound(body))

    def test_an_empty_string_hardcoded_false_would_fail_this_census(self) -> None:
        # round 9 (sol MAJOR item 2): an empty string is another falsy-in-PowerShell constant with
        # no `$false`/numeric/`[bool]` token in it at all.
        fixture = (
            "function New-BypassRow {\n"
            "    param([object]$Record)\n"
            "    [pscustomobject]@{\n"
            "        presented_fps = 99.0\n"
            "        host_load_provisional = ''\n"
            "    }\n"
            "}\n"
        )
        matches = list(self.ROW_FUNCTION_PATTERN.finditer(fixture))
        self.assertEqual(len(matches), 1)
        body = matches[0].group(0)
        self.assertTrue(self.REAL_FPS_PATTERN.search(body))
        self.assertFalse(self._is_bound(body))

    def test_a_string_literal_false_hardcoded_false_would_fail_this_census(self) -> None:
        # round 9 (sol MAJOR item 2): the STRING 'false' is truthy in PowerShell (any non-empty
        # string is $true in a boolean context) -- exactly the kind of surprising hardcode that
        # only a constant-vs-derived classifier (not a value-truthiness check) catches correctly.
        fixture = (
            "function New-BypassRow {\n"
            "    param([object]$Record)\n"
            "    [pscustomobject]@{\n"
            "        presented_fps = 99.0\n"
            "        host_load_provisional = 'false'\n"
            "    }\n"
            "}\n"
        )
        matches = list(self.ROW_FUNCTION_PATTERN.finditer(fixture))
        self.assertEqual(len(matches), 1)
        body = matches[0].group(0)
        self.assertTrue(self.REAL_FPS_PATTERN.search(body))
        self.assertFalse(self._is_bound(body))

    def test_bool_cast_of_one_would_also_fail_this_census(self) -> None:
        # round 9 regression guard on the inversion itself: [bool]1 is a hardcoded TRUE-valued
        # constant, but it is not the literal token `$true` this census accepts -- under the
        # inverted rule, any hardcoded constant other than exactly `$true` is unbound, whatever its
        # truthiness. (Round 8's blacklist approach would never have caught this at all: it is not
        # false-shaped.)
        fixture = (
            "function New-BypassRow {\n"
            "    param([object]$Record)\n"
            "    [pscustomobject]@{\n"
            "        presented_fps = 99.0\n"
            "        host_load_provisional = [bool]1\n"
            "    }\n"
            "}\n"
        )
        matches = list(self.ROW_FUNCTION_PATTERN.finditer(fixture))
        self.assertEqual(len(matches), 1)
        body = matches[0].group(0)
        self.assertTrue(self.REAL_FPS_PATTERN.search(body))
        self.assertFalse(self._is_bound(body))

    def test_the_real_hardcoded_true_used_by_two_production_rows_still_passes(self) -> None:
        # Regression guard: New-ProfileRow and New-FieldLogRow both legitimately hardcode
        # `host_load_provisional = $true` ("always unrecorded", the safe fail-toward-provisional
        # direction) -- the ONE hardcoded shape this census must keep accepting.
        fixture = (
            "function New-AlwaysUnrecordedRow {\n"
            "    param([object]$Record)\n"
            "    [pscustomobject]@{\n"
            "        presented_fps = 99.0\n"
            "        host_load_provisional = $true\n"
            "    }\n"
            "}\n"
        )
        matches = list(self.ROW_FUNCTION_PATTERN.finditer(fixture))
        self.assertEqual(len(matches), 1)
        self.assertTrue(self._is_bound(matches[0].group(0)))

    def test_a_derived_expression_still_passes_this_census(self) -> None:
        # round 8 regression guard: the expression-aware detector must not become so broad it
        # rejects genuine derived provenance -- a variable, a property access, and the real file's
        # own `if (...) { $true } else { $null }` shape must all still pass.
        for rhs in ("$hostLoadProvisional", "$hostLoadRefusal.provisional",
                    'if ($kind -eq "playback") { $true } else { $null }'):
            fixture = (
                "function New-DerivedRow {\n"
                "    param([object]$Record)\n"
                "    [pscustomobject]@{\n"
                "        presented_fps = 99.0\n"
                f"        host_load_provisional = {rhs}\n"
                "    }\n"
                "}\n"
            )
            matches = list(self.ROW_FUNCTION_PATTERN.finditer(fixture))
            self.assertEqual(len(matches), 1)
            self.assertTrue(self._is_bound(matches[0].group(0)), f"rhs={rhs!r} must still be treated as bound")

    def test_a_comment_only_mention_would_fail_this_census(self) -> None:
        # round 6 (astra's exact repro): "...or only a comment containing host_load_provisional."
        fixture = (
            "function New-CommentOnlyRow {\n"
            "    param([object]$Record)\n"
            "    # host_load_provisional is intentionally not tracked for this row yet\n"
            "    [pscustomobject]@{\n"
            "        presented_fps = 99.0\n"
            "    }\n"
            "}\n"
        )
        matches = list(self.ROW_FUNCTION_PATTERN.finditer(fixture))
        self.assertEqual(len(matches), 1)
        body = matches[0].group(0)
        self.assertTrue(self.REAL_FPS_PATTERN.search(self._strip_comments(body)))
        self.assertIn("host_load_provisional", body)  # the OLD substring-presence check would pass
        self.assertFalse(self._is_bound(body))  # the round-6 detector must still refuse it


class HostLoadReaderPredicateCensusTests(unittest.TestCase):
    """round 9 (fable minor, item 3): sol/fable round 8 accepted six-way duplication of the
    canonical quiet-AND-not-provisional predicate across compare-machine-perf.ps1,
    compare-release-gui-smoke-ab.ps1, run-release-cuda-playback-ab.ps1,
    run-ultramagnus-p3-validation.ps1 (twice), and summarize-local-cuda-proof.ps1 -- each site
    individually protected by its own exceeded/false behavioural test, but nothing structural
    pinned either the SET of sites or the SHAPE they must share. fable's own words: "nothing
    structural catches a seventh reader added later without the predicate." This is the reader-side
    analogue of CompareMachinePerfRowHostLoadCensusTests above (which does the same job for
    PRODUCER rows): it scans every reader script for a predicate-DEFINING line -- one that combines
    a provisional-named identifier with an -or plus an -eq/-ne comparison against the quiet/
    unknown/exceeded state literals, in EITHER polarity, so a near-miss shape (wrong operator,
    wrong comparand) cannot silently evade the locator the way it evaded the exact-shape regex
    alone -- and asserts (a) there are exactly the six known sites today, re-derived from the live
    files, and (b) every single one of them matches the canonical
    `($xDeclared -or $yState -ne "quiet")` shape verbatim, not merely a related one.

    run-local-gpu-capability.ps1 and run-ultra-magnus-profile.ps1 are excluded (not silently
    ignored): fable's round-8 repo-wide enumeration confirmed both only ever WRITE
    host_load_provisional = $true/$null and never derive cleanliness from state, so they are
    producers, not readers, of this predicate."""

    READER_SCRIPTS = {
        "compare-machine-perf.ps1": COMPARE_MACHINE_PERF_SCRIPT,
        "compare-release-gui-smoke-ab.ps1": COMPARE_SCRIPT,
        "run-release-cuda-playback-ab.ps1": CUDA_AB_SCRIPT,
        "run-ultramagnus-p3-validation.ps1": P3_VALIDATION_SCRIPT,
        "summarize-local-cuda-proof.ps1": CUDA_PROOF_SUMMARIZER_SCRIPT,
    }
    # VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-1 r6 (sol r5 B1): compare-release-gui-smoke-ab.ps1 no longer defines its own predicate; it judges each block with
    # VenueChain.psm1's ConvertFrom-VenueSmokeHostLoad (the stricter venue rule: collected snapshots too). Five sites remain; the comparator stays in the
    # scanned set, so a loose predicate re-added there makes the count six and fails here (test_the_gui_ab_comparator_defines_no_predicate_of_its_own).
    # VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-RESIDUE-1 r1 (sol r6 B1): the five loose sites are gone. Every reader site is now the ONE strict predicate
    # (STRICT_PREDICATE_PATTERN: state ordinal 'quiet', provisional a real [bool] $false, collected a real [bool] $true), and the loose shape is
    # located nowhere. Six strict sites: compare-machine-perf.ps1 (1), run-release-cuda-playback-ab.ps1 (2: Get-HostLoadProofFailures and
    # Get-SmokeSummaryHostLoadFields), run-ultramagnus-p3-validation.ps1 (2: Get-SmokeSummaryHostLoadFields and the per-clip guard),
    # summarize-local-cuda-proof.ps1 (1); the GUI comparator still defines none of its own.
    # adversary-ac6205bd.json A2: +2, the smoke-TEXT standing (VenueChain.psm1 ConvertFrom-VenueSmokeResultText) judged by the same strict line in
    # run-release-cuda-playback-ab.ps1 Read-SmokeSummary and run-ultramagnus-p3-validation.ps1's per-clip read.
    EXPECTED_SITE_COUNT = 8
    # A predicate-site line (comment-stripped): mentions a provisional-named identifier and the 'quiet' literal (either quote).
    _SITE_LOCATOR_PATTERN = re.compile(r"""^(?=.*[Pp]rovisional)(?=.*['"]quiet['"]).+$""", re.MULTILINE)
    STRICT_PREDICATE_PATTERN = re.compile(
        r"-not \(\$(?P<p>[A-Za-z0-9_]*[Pp]rovisional[A-Za-z0-9_]*) -is \[bool\] -and -not \$(?P=p) -and "
        r"\$(?P<s>[A-Za-z0-9_]*[Ss]tate[A-Za-z0-9_]*) -is \[string\] -and \[string\]::Equals\(\$(?P=s), 'quiet', \[StringComparison\]::Ordinal\) -and "
        r"\$(?P<c>[A-Za-z0-9_]*[Cc]ollected[A-Za-z0-9_]*) -is \[bool\] -and \$(?P=c)\)"
    )

    def test_residue_b1_no_reader_keeps_the_loose_predicate_and_every_site_is_strict(self) -> None:
        sites: list[tuple[str, str]] = []
        for name, path in self.READER_SCRIPTS.items():
            code_only = self._strip_comments(path.read_text(encoding="utf-8"))
            self.assertEqual(self._LOOSE_LOCATOR_PATTERN.findall(code_only), [], f"{name} still has a loose host-load predicate")
            sites += [(name, line.strip()) for line in self._SITE_LOCATOR_PATTERN.findall(code_only)]
        self.assertEqual(len(sites), self.EXPECTED_SITE_COUNT, sites)
        self.assertEqual([s for s in sites if not self.STRICT_PREDICATE_PATTERN.search(s[1])], [], "every reader site is the strict predicate verbatim")

    def test_residue_b1_the_strict_pattern_refuses_each_loose_variant(self) -> None:
        strict = ("$xProvisional = -not ($xProvisionalRaw -is [bool] -and -not $xProvisionalRaw -and $xStateRaw -is [string] -and "
                  "[string]::Equals($xStateRaw, 'quiet', [StringComparison]::Ordinal) -and $xCollectedRaw -is [bool] -and $xCollectedRaw)")
        self.assertRegex(strict, self.STRICT_PREDICATE_PATTERN)
        self.assertRegex(strict, self._SITE_LOCATOR_PATTERN)
        for loose in (strict.replace(" -and $xCollectedRaw -is [bool] -and $xCollectedRaw", ""),
                      strict.replace("[StringComparison]::Ordinal", "[StringComparison]::OrdinalIgnoreCase"),
                      strict.replace("$xProvisionalRaw -is [bool] -and ", ""),
                      '$xProvisional = ($xProvisionalDeclared -or $xState -ne "quiet")'):
            with self.subTest(loose=loose):
                self.assertIsNone(self.STRICT_PREDICATE_PATTERN.search(loose))

    def test_the_gui_ab_comparator_defines_no_predicate_of_its_own(self) -> None:
        code_only = self._strip_comments(COMPARE_SCRIPT.read_text(encoding="utf-8"))
        self.assertEqual(self._LOOSE_LOCATOR_PATTERN.findall(code_only), [])
        self.assertIn("ConvertFrom-VenueSmokeHostLoad -HostLoad $legHostLoad", code_only)

    # A predicate-DEFINING line: mentions a provisional-named identifier, an "-or", and an
    # "-eq"/"-ne" comparison against one of the three host-load state literals, all on the same
    # (comment-stripped) line. Deliberately broader than the canonical shape itself (which only
    # ever uses "-ne \"quiet\"") so a drifted near-miss -- e.g. the pre-round-7 `-eq "unknown"`
    # shape, which misses an exceeded/false leg -- is still located as a site to check, not missed
    # entirely because it doesn't look like the fix yet.
    _LOOSE_LOCATOR_PATTERN = re.compile(
        r'^(?=.*[Pp]rovisional)(?=.*-or)(?=.*(?:-eq|-ne)\s*"(?:quiet|unknown|exceeded)").+$',
        re.MULTILINE,
    )
    # The canonical shape itself: <ident-containing-Provisional> -or <ident-containing-State> -ne
    # "quiet", whatever variable-name prefix each site uses (legProvisionalDeclared,
    # clipHostLoadProvisionalDeclared, ...).
    CANONICAL_PREDICATE_PATTERN = re.compile(
        r'\(\s*\$[A-Za-z0-9_]*[Pp]rovisional[A-Za-z0-9_]*\s*-or\s*'
        r'\$[A-Za-z0-9_]*[Ss]tate[A-Za-z0-9_]*\s*-ne\s*"quiet"\s*\)'
    )

    @staticmethod
    def _strip_comments(text: str) -> str:
        return "\n".join(re.sub(r"#.*$", "", line) for line in text.splitlines())

    def test_every_reader_predicate_line_is_the_canonical_shape_and_the_set_is_complete(self) -> None:
        matches: list[tuple[str, str]] = []
        for name, path in self.READER_SCRIPTS.items():
            code_only = self._strip_comments(path.read_text(encoding="utf-8"))
            # residue r1 (sol r6 B1): the loose round-8 shape is located nowhere; every site is the strict predicate
            self.assertEqual(self._LOOSE_LOCATOR_PATTERN.findall(code_only), [], name)
            for line in self._SITE_LOCATOR_PATTERN.findall(code_only):
                matches.append((name, line.strip()))
        self.assertEqual(
            len(matches), self.EXPECTED_SITE_COUNT,
            f"expected exactly {self.EXPECTED_SITE_COUNT} reader-predicate sites (the six-way "
            f"duplication accepted at round 8), found {len(matches)}: {matches}. A different count "
            "means either a new reader appeared (verify it uses the canonical shape below, then "
            "update this pin deliberately) or an existing one was removed.",
        )
        non_canonical = [
            (name, line) for name, line in matches
            if not self.STRICT_PREDICATE_PATTERN.search(line)
        ]
        self.assertEqual(
            non_canonical, [],
            "reader predicate line(s) do not match the canonical quiet-AND-not-provisional "
            f"expression verbatim: {non_canonical}",
        )

    def test_the_pre_round_7_eq_unknown_shape_would_be_located_and_rejected(self) -> None:
        # fable's named historical defect (round 5/6, closed at round 7): checking
        # state=="unknown" instead of state!="quiet" misses an exceeded/false leg entirely
        # (state="exceeded", provisional=false slips through). Proves the LOOSE locator still
        # FINDS this drifted shape (it doesn't require the canonical operator/literal to match) and
        # that the CANONICAL pattern correctly refuses it -- the census does not merely accept
        # anything that mentions provisional and state together.
        drifted = '$provisional = ($provisionalDeclared -or $state -eq "unknown")'
        self.assertRegex(drifted, self._LOOSE_LOCATOR_PATTERN)
        self.assertIsNone(self.CANONICAL_PREDICATE_PATTERN.search(drifted))
        self.assertIsNone(self.STRICT_PREDICATE_PATTERN.search(drifted))

    def test_a_seventh_reader_with_a_fresh_variable_prefix_would_still_be_recognized_as_canonical(self) -> None:
        # Sanity check on the patterns themselves: a synthetic seventh site using a variable-name
        # prefix that appears nowhere in the real sites still matches the locator and the strict pattern,
        # proving this census is not accidentally keyed to the existing identifier names.
        # residue r1 (sol r6 B1): the round-8 loose shape is still LOCATED (and refused); the strict shape is the canonical one.
        loose = '$xProvisional = ($xProvisionalDeclared -or $xState -ne "quiet")'
        self.assertRegex(loose, self._LOOSE_LOCATOR_PATTERN)
        self.assertIsNone(self.STRICT_PREDICATE_PATTERN.search(loose))
        synthetic = ("$yProvisional = -not ($yProvisionalRaw -is [bool] -and -not $yProvisionalRaw -and $yStateRaw -is [string] -and "
                     "[string]::Equals($yStateRaw, 'quiet', [StringComparison]::Ordinal) -and $yCollectedRaw -is [bool] -and $yCollectedRaw)")
        self.assertRegex(synthetic, self._SITE_LOCATOR_PATTERN)
        self.assertRegex(synthetic, self.STRICT_PREDICATE_PATTERN)

    def test_a_comment_only_mention_is_not_located_as_a_site(self) -> None:
        # Mirrors the row census's comment-stripping guard: a mention of provisional/state/quiet
        # that exists only in a comment (this file's own round-8 rationale comments say exactly
        # this phrase) must not be counted as a predicate-defining site.
        commented = '# note): clean iff state=="quiet" AND provisional==false; every other combo\n'
        code_only = self._strip_comments(commented)
        self.assertEqual(self._LOOSE_LOCATOR_PATTERN.findall(code_only), [])


@requires_pwsh
class CompareMachinePerfProfileAndFieldLogHostLoadTests(_ProbeCase):
    """round 5 (sol MAJOR #4): New-ProfileRow (mlvapp.playback_profile.v1) and New-FieldLogRow
    (mlvapp.perf-field-log.v1) used to emit presented_fps with NO host-load provenance at all --
    these two schemas have never carried host-load telemetry, so their fps was never checked
    against it. Fail toward provisional/unrecorded, same stance as every other reader."""

    script = COMPARE_MACHINE_PERF_SCRIPT
    functions = [
        "Convert-ToNullableInt64",
        "Get-TotalPipelineFrames",
        "Assert-MachineFingerprint",
        "Get-MachineLabel",
        "New-ProfileRow",
        "New-FieldLogRow",
    ]

    def _profile_record(self, cadence_ms: float) -> str:
        return (
            "[pscustomobject]@{ schema = 'mlvapp.playback_profile.v1'; "
            "machineFingerprint = [pscustomobject]@{ schema = 'machine-fingerprint.v1'; "
            "hostname = 'H'; cpu = 'x'; gpu = 'y'; "
            "os = 'z'; build_sha = 'abc1234' }; "
            "metadata = [pscustomobject]@{ average_cadence_ms = " + str(cadence_ms) + " }; "
            "summary = [pscustomobject]@{ schema = 'mlvapp.playback-profile-summary.v1'; "
            "pipeline_counts = [pscustomobject]@{ gpu_texture_no_readback = 5 }; "
            "fallback_count = 0; "
            "bottleneck = [pscustomobject]@{ limiting_stage = 'decode'; suggested_optimization = 'x' } } }"
        )

    def test_profile_row_reports_host_load_provisional(self) -> None:
        record = self._profile_record(20.0)
        proc = self.run_snippet(
            f"$record = {record}\n"
            "$r = New-ProfileRow -Record $record -Source 'test'\n"
            "Write-Host \"FPS=$($r.presented_fps) HOST_LOAD_PROVISIONAL=$($r.host_load_provisional)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("HOST_LOAD_PROVISIONAL=True", proc.stdout)
        # A real (non-null) fps must be present -- this is the exact repro shape (a real ranking
        # number with unrecorded host-load provenance), not a degenerate always-null row.
        self.assertNotIn("FPS= ", proc.stdout)
        self.assertNotIn("FPS=\n", proc.stdout)

    def _field_log_record(self, kind: str, presented_fps) -> str:
        fps_literal = "$null" if presented_fps is None else str(presented_fps)
        return (
            "[pscustomobject]@{ schema = 'mlvapp.perf-field-log.v1'; kind = '" + kind + "'; "
            "machineFingerprint = [pscustomobject]@{ schema = 'machine-fingerprint.v1'; "
            "hostname = 'H'; cpu = 'x'; gpu = 'y'; "
            "os = 'z'; build_sha = 'abc1234' }; "
            "pipeline_counts = [pscustomobject]@{ gpu_texture_no_readback = 5 }; "
            "fallback_count = 0; "
            f"presented_fps = {fps_literal}; no_readback_percent = 1.0; "
            "frame_count = 100; "
            "bottleneck = [pscustomobject]@{ limiting_stage = 'decode' }; "
            "suggested_optimization = 'x' }"
        )

    def test_playback_field_log_row_reports_host_load_provisional(self) -> None:
        record = self._field_log_record("playback", 24.0)
        proc = self.run_snippet(
            f"$record = {record}\n"
            "$r = New-FieldLogRow -Record $record -Source 'test'\n"
            "Write-Host \"FPS=$($r.presented_fps) HOST_LOAD_PROVISIONAL=$($r.host_load_provisional)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("FPS=24", proc.stdout)
        self.assertIn("HOST_LOAD_PROVISIONAL=True", proc.stdout)

    def test_export_field_log_row_has_no_fps_signal_to_gate(self) -> None:
        record = self._field_log_record("export", None)
        proc = self.run_snippet(
            f"$record = {record}\n"
            "$r = New-FieldLogRow -Record $record -Source 'test'\n"
            "Write-Host \"FPS=$($r.presented_fps) HOST_LOAD_PROVISIONAL=$($r.host_load_provisional)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("FPS= HOST_LOAD_PROVISIONAL=", proc.stdout)


@requires_pwsh
class CompareMachinePerfP3HostLoadRowTests(_ProbeCase):
    """tools/profiling/compare-machine-perf.ps1's New-RemoteP3SummaryRow -- round 3 folds in
    sol's finding: p3 rows previously carried presented_fps with no host-load provenance at all
    (fable's finding #2 also named this gap). Get-PlaybackAbLegHostLoadProvisional is reused here
    even though it is named for playback-ab legs, because its absent-property-safe check on a
    bare "hostLoadProvisional" boolean applies to any leg-shaped object, p3 clips included."""

    script = COMPARE_MACHINE_PERF_SCRIPT
    functions = [
        "Convert-ToNullableDouble",
        "Convert-ToNullableInt64",
        "Get-AverageNullableDouble",
        "Get-PlaybackAbLegHostLoadProvisional",
        "Get-FirstPropertyValue",
        "Convert-NvidiaSmiMemoryToMb",
        "Get-ImportedP3RunMetadata",
        "New-MachineFingerprintObject",
        "New-RemoteP3MachineFingerprint",
        "Assert-MachineFingerprint",
        "Get-MachineLabel",
        "Get-ClipPresentedFrames",
        "Get-TotalPipelineFrames",
        "New-RemoteP3SummaryRow",
    ]

    def _clip(self, presented_fps: float, host_load_provisional, host_load_state: str = None) -> str:
        extra = ""
        if host_load_provisional is not None:
            extra = "; hostLoadProvisional = " + ("$true" if host_load_provisional else "$false")
            # round 5: default hostLoadState to what the boolean implies, unless the caller wants
            # to test a specific (e.g. inconsistent) state explicitly.
            resolved_state = host_load_state if host_load_state is not None else (
                "exceeded" if host_load_provisional else "quiet")
            extra += f"; hostLoadState = '{resolved_state}'"
            # residue r1 (sol r6 B1): a P3 clip row carries hostLoadCollected (run-ultramagnus-p3-validation.ps1 writes it)
            extra += "; hostLoadCollected = $true"
        return (
            "[pscustomobject]@{ "
            f"presentedFps = {presented_fps}; gpuTextureNoReadbackFrames = 10; "
            f"fallbackFrameCount = 0; validationOk = $true{extra} }}"
        )

    def _record(self, clips: list) -> str:
        clips_literal = ", ".join(clips)
        return (
            "[pscustomobject]@{ schema = 'mlvapp-ultramagnus-p3-validation.v1'; status = 'success'; "
            "machineFingerprint = [pscustomobject]@{ hostname = 'ULTRAMAGNUS'; cpu = 'x'; gpu = 'y'; "
            "os = 'z'; build_sha = 'abc1234' }; "
            f"clipResults = @({clips_literal}); "
            "proof = [pscustomobject]@{ correctnessValidated = $true } }"
        )

    def test_all_quiet_clips_report_host_load_provisional_false(self) -> None:
        record = self._record([self._clip(24.0, False), self._clip(25.0, False)])
        proc = self.run_snippet(
            f"$record = {record}\n"
            "$r = New-RemoteP3SummaryRow -Record $record -Source 'test'\n"
            "Write-Host \"HOST_LOAD_PROVISIONAL=$($r.host_load_provisional)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("HOST_LOAD_PROVISIONAL=False", proc.stdout)

    def test_one_provisional_clip_marks_the_whole_row_provisional(self) -> None:
        record = self._record([self._clip(24.0, False), self._clip(4.0, True)])
        proc = self.run_snippet(
            f"$record = {record}\n"
            "$r = New-RemoteP3SummaryRow -Record $record -Source 'test'\n"
            "Write-Host \"HOST_LOAD_PROVISIONAL=$($r.host_load_provisional)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("HOST_LOAD_PROVISIONAL=True", proc.stdout)

    def test_clip_missing_hostloadprovisional_property_is_treated_as_provisional(self) -> None:
        # A p3 record from before this fix never carried the property at all -- "cannot know" is
        # the same disposition as "provisional" under this card's fail-toward-provisional stance.
        record = self._record([self._clip(24.0, None)])
        proc = self.run_snippet(
            f"$record = {record}\n"
            "$r = New-RemoteP3SummaryRow -Record $record -Source 'test'\n"
            "Write-Host \"HOST_LOAD_PROVISIONAL=$($r.host_load_provisional)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("HOST_LOAD_PROVISIONAL=True", proc.stdout)

    def test_clip_with_provisional_false_and_unknown_state_is_still_provisional(self) -> None:
        # round 5 (sol BLOCKER): Get-PlaybackAbLegHostLoadProvisional is reused for p3 clips
        # (docstring above); this is the same "state=unknown, provisional=false" repro as
        # CompareMachinePerfPlaybackAbHostLoadRefusalTests, exercised through the p3 row path.
        record = self._record([self._clip(24.0, False, host_load_state="unknown")])
        proc = self.run_snippet(
            f"$record = {record}\n"
            "$r = New-RemoteP3SummaryRow -Record $record -Source 'test'\n"
            "Write-Host \"HOST_LOAD_PROVISIONAL=$($r.host_load_provisional)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("HOST_LOAD_PROVISIONAL=True", proc.stdout)


@requires_pwsh
class P3ValidationSmokeSummaryHostLoadFieldsTests(_ProbeCase):
    """tools/profiling/run-ultramagnus-p3-validation.ps1's Get-SmokeSummaryHostLoadFields --
    duplicated (this script has no shared module with the CUDA A/B script) but same behaviour:
    fail toward provisional/unknown on a missing or degenerate hostLoad block."""

    script = P3_VALIDATION_SCRIPT
    functions = ["Get-SmokeSummaryHostLoadFields"]

    def _fields(self, host_load: str) -> subprocess.CompletedProcess:
        return self.run_snippet(
            f"$hostLoad = {host_load}\n"
            "$f = Get-SmokeSummaryHostLoadFields -HostLoad $hostLoad\n"
            "Write-Host \"PROVISIONAL=$($f.provisional) STATE=$($f.state)\"\n"
        )

    def test_missing_hostload_reads_as_unknown_provisional(self) -> None:
        proc = self._fields("$null")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PROVISIONAL=True STATE=unknown", proc.stdout)

    def test_hostload_present_but_missing_provisional_property_reads_as_provisional(self) -> None:
        proc = self._fields("[pscustomobject]@{ state = 'exceeded' }")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PROVISIONAL=True", proc.stdout)

    def test_well_formed_quiet_hostload_reads_through_untouched(self) -> None:
        # residue r1 (sol r6 B1): a well-formed quiet block carries its collected snapshots (the smoke always writes them)
        proc = self._fields(
            "[pscustomobject]@{ provisional = $false; state = 'quiet'; reason = $null; " + SNAPS + " }"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PROVISIONAL=False STATE=quiet", proc.stdout)

    def test_residue_b1_uncollected_coerced_and_case_folded_blocks_are_provisional(self) -> None:
        # VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-RESIDUE-1 r1 (sol r6 B1, sibling run-ultramagnus-p3-validation.ps1:237-249): the same strict predicate as the
        # CUDA A/B reader. A singleton-array block is not an object and reads provisional too.
        cases = {
            "uncollected": "[pscustomobject]@{ provisional = $false; state = 'quiet'; before = [pscustomobject]@{ collected = $false }; after = [pscustomobject]@{ collected = $true } }",
            "no-snapshots": "[pscustomobject]@{ provisional = $false; state = 'quiet' }",
            "zero": "[pscustomobject]@{ provisional = 0; state = 'quiet'; " + SNAPS + " }",
            "array": "[pscustomobject]@{ provisional = @($false); state = 'quiet'; " + SNAPS + " }",
            "upper": "[pscustomobject]@{ provisional = $false; state = 'QUIET'; " + SNAPS + " }",
            "singleton-block": ", @([pscustomobject]@{ provisional = $false; state = 'quiet'; " + SNAPS + " })",
            # adversary-ac6205bd.json A1 (sibling run-ultramagnus-p3-validation.ps1:252)
            "during-uncollected": "[pscustomobject]@{ provisional = $false; state = 'quiet'; " + SNAPS + "; during = @([pscustomobject]@{ collected = $false }) }",
            "during-null": "[pscustomobject]@{ provisional = $false; state = 'quiet'; " + SNAPS + "; during = $null }",
            "during-object": "[pscustomobject]@{ provisional = $false; state = 'quiet'; " + SNAPS + "; during = [pscustomobject]@{ collected = $true } }",
        }
        for name, block in cases.items():
            with self.subTest(case=name):
                proc = self._fields(block)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn("PROVISIONAL=True", proc.stdout, (name, proc.stdout))

    def test_provisional_false_with_missing_state_reads_as_provisional_not_clean_unknown(self) -> None:
        # round 4 (sol major): an explicit provisional=false paired with a missing state used to
        # read as the inconsistent, clean-reading STATE=unknown PROVISIONAL=False.
        proc = self._fields("[pscustomobject]@{ provisional = $false }")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PROVISIONAL=True STATE=unknown", proc.stdout)

    def test_provisional_false_with_exceeded_state_reads_as_provisional_not_clean(self) -> None:
        # round 8 (sol MAJOR item 3), exact repro: an exceeded/false leg previously read clean here
        # -- only state=="unknown" forced provisional=true. Clean now requires state=="quiet" AND
        # provisional==false.
        proc = self._fields("[pscustomobject]@{ provisional = $false; state = 'exceeded' }")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PROVISIONAL=True STATE=exceeded", proc.stdout)


class P3ValidationSpeedValidatedWiringTests(unittest.TestCase):
    """Static structural check: speedValidated (the card's own "acceptance signal" language,
    applied to the speed leg's fps floor) must consult per-clip host-load provenance, and each
    clip result must carry it -- otherwise the mark computed above is never reachable."""

    def test_clip_results_carry_host_load_fields(self) -> None:
        source = P3_VALIDATION_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("hostLoadProvisional = $hostLoadFields.provisional", source)
        self.assertIn("$hostLoadFields = Get-SmokeSummaryHostLoadFields -HostLoad", source)

    def test_speed_validated_excludes_host_load_provisional_clips(self) -> None:
        source = P3_VALIDATION_SCRIPT.read_text(encoding="utf-8")
        speed_validated_start = source.index("$speedValidated =")
        speed_validated_block = source[speed_validated_start:speed_validated_start + 1600]
        self.assertIn("[bool]$_.hostLoadProvisional", speed_validated_block)

    def test_speed_floor_check_computes_host_load_fields_before_it_runs(self) -> None:
        # round 4 (sol major): the per-clip fps floor used to run BEFORE $hostLoadFields existed,
        # so a loaded/uncollectable host that dragged presented_fps under the floor was recorded
        # as a genuine clipFailures entry -- conflating "we couldn't get a clean signal" with "the
        # build is broken". $hostLoadFields must now be computed ahead of the floor check.
        source = P3_VALIDATION_SCRIPT.read_text(encoding="utf-8")
        host_load_fields_index = source.index(
            "$hostLoadFields = Get-SmokeSummaryHostLoadFields -HostLoad "
            # round 6 (astra MAJOR / production crash): the bare "(if ...)" form below was being
            # executed by PowerShell as an attempt to invoke a command literally named "if"
            # (CommandNotFoundException) -- the fix wraps it as a subexpression, "$(if ...)". See
            # P3ValidationHostLoadFieldsAssignmentExecutesTests below for the executable regression.
            # residue r1 (sol r6 B1): ", $result.hostLoad" so a one-element hostLoad array is passed as it is (not unrolled into a clean object)
            "$(if ($result) { , $result.hostLoad } else { $null })"
        )
        floor_check_index = source.index('was below hard floor {1:N3} fps."')
        self.assertLess(
            host_load_fields_index, floor_check_index,
            "$hostLoadFields must be computed before the speed-leg fps floor check consumes it",
        )

    def test_speed_floor_breach_is_a_warning_not_a_failure_when_host_load_is_provisional(self) -> None:
        source = P3_VALIDATION_SCRIPT.read_text(encoding="utf-8")
        speed_leg_start = source.index("$presentedFpsValue = if ($result)")
        speed_leg_block = source[speed_leg_start:speed_leg_start + 1000]
        self.assertIn("if ($hostLoadFields.provisional)", speed_leg_block)
        self.assertIn("$warnings.Add(", speed_leg_block)
        # the raw Add-Failure floor breach must only fire in the non-provisional branch
        provisional_branch_index = speed_leg_block.index("if ($hostLoadFields.provisional)")
        else_index = speed_leg_block.index("else {", provisional_branch_index)
        add_failure_index = speed_leg_block.index(
            'Add-Failure $clipFailures ("Speed leg presented_fps'
        )
        self.assertGreater(
            add_failure_index, else_index,
            "the floor-breach Add-Failure must live in the non-provisional else branch",
        )


@requires_pwsh
class P3ValidationSpeedValidatedGuardExecutesTests(_ProbeCase):
    """round 7 (astra MAJOR, exact repro): "adding -not before P3's [bool]$_.hostLoadProvisional
    ... passes all four P3 wiring tests." test_speed_validated_excludes_host_load_provisional_clips
    above only checks that the substring "[bool]$_.hostLoadProvisional" is PRESENT in the source --
    a mutation that inverts the guard by prepending "-not" still contains that exact substring, so
    the static check stays green while the guard's actual behaviour flips. This extracts the real
    $speedValidated Where-Object clause by literal text search and EXECUTES it against a clip that
    is otherwise clean on every OTHER disqualifying condition, on both sides of the
    hostLoadProvisional boundary -- a real "-not" mutation changes what this test observes
    (speedValidated flips to True for a provisional clip), not just what substrings are present."""

    script = P3_VALIDATION_SCRIPT
    functions: list[str] = []

    START_MARKER = "$speedValidated =\n"
    END_MARKER = "}).Count -eq 0)"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="hostload-p3-speedvalidated-probe-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        source = self.script.read_text(encoding="utf-8")
        self.guard_block = _extract_block(source, self.START_MARKER, self.END_MARKER, trailing_lines=0)
        self.assertIn("[bool]$_.hostLoadProvisional", self.guard_block)
        self.assertNotIn("-not [bool]$_.hostLoadProvisional", self.guard_block)

    def _clip(self, host_load_provisional: bool) -> str:
        # Clean on every OTHER disqualifying condition -- the only thing under test is
        # hostLoadProvisional itself.
        provisional_literal = "$true" if host_load_provisional else "$false"
        return (
            "[pscustomobject]@{ status = 'success'; presentedFps = 30.0; "
            "gpuTextureNoReadbackFrames = 10; fallbackFrameCount = 0; "
            "cudaAmazeAcceptedTextureSourceFrameCount = 10; activeNoReadbackFrameCount = 10; "
            f"hostLoadProvisional = {provisional_literal} }}"
        )

    def _run(self, host_load_provisional: bool) -> subprocess.CompletedProcess:
        script = self.tmp / "run.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            "$receiptRawFixesEnabled = $true\n"
            "$SpeedLeg = $true\n"
            "$status = 'success'\n"
            "$DryRun = $false\n"
            "$MinPresentedFps = 1.0\n"
            f"$clipResults = @({self._clip(host_load_provisional)})\n"
            f"{self.guard_block}\n"
            "Write-Host \"SPEED_VALIDATED=$speedValidated\"\n",
            encoding="utf-8",
        )
        return subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(script)],
            capture_output=True, text=True, creationflags=_NO_WINDOW,
        )

    def test_provisional_clip_that_is_otherwise_clean_is_not_speed_validated(self) -> None:
        proc = self._run(host_load_provisional=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("SPEED_VALIDATED=False", proc.stdout)

    def test_non_provisional_clean_clip_is_speed_validated(self) -> None:
        # Other side of the boundary: the fix must not turn every clean clip unvalidated.
        proc = self._run(host_load_provisional=False)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("SPEED_VALIDATED=True", proc.stdout)


class P3ValidationImportIndependentlyChecksHostLoadTests(unittest.TestCase):
    """Static structural check: packet import for a speed proof must independently verify each
    clip's host-load provenance rather than trusting a possibly-stale summary.proof.speedValidated
    alone -- round 4 (sol major), closing the last of three PARTIAL findings this round."""

    def test_import_speed_floor_block_independently_checks_host_load_provisional(self) -> None:
        source = P3_VALIDATION_SCRIPT.read_text(encoding="utf-8")
        import_speed_start = source.index('if ($isSpeedProof) {\n                $minSpeedFps')
        # round 5: widened from 1800 to fit the state-check addition below the flag-only read.
        # round 8: widened again from 3200 to fit the item-3 canonical-predicate comment.
        # residue r1: widened from 3600 to fit the strict-predicate lines (sol r6 B1)
        import_speed_block = source[import_speed_start:import_speed_start + 5200]
        self.assertIn('$clip.PSObject.Properties["hostLoadProvisional"]', import_speed_block)
        self.assertIn("host load was PROVISIONAL or unrecorded", import_speed_block)
        # round 5 (sol BLOCKER): the flag alone is not enough -- hostLoadState must also gate.
        self.assertIn('$clip.PSObject.Properties["hostLoadState"]', import_speed_block)
        # the independent host-load Add-Failure must not be gated behind the floor comparison
        host_load_check_index = import_speed_block.index("$clipHostLoadProvisionalProperty")
        floor_check_index = import_speed_block.index("[double]$clip.presentedFps -lt $minSpeedFps")
        self.assertLess(host_load_check_index, floor_check_index)


@requires_pwsh
class P3ValidationImportSpeedFloorGuardExecutesTests(_ProbeCase):
    """round 6 (sol + astra MAJOR -- "enforcement tests check TOKENS, not behaviour"):
    P3ValidationImportIndependentlyChecksHostLoadTests above only asserts that certain substrings
    appear, in the right relative order, in the source text -- astra's exact repro: "Change the
    P3/summarizer state/flag guards from -or to -and; their respective tests remain green." This
    class extracts the SAME guard block by literal text search (the boundaries the static test
    above already locates via source.index) and EXECUTES it against a mocked $clip/$importFailures/
    Add-Failure, so a real -or/-and or $true/$false mutation in the live source changes what this
    test actually observes, not just what substrings are present."""

    script = P3_VALIDATION_SCRIPT
    functions: list[str] = []

    START_MARKER = '$clipHostLoadProvisionalProperty = $clip.PSObject.Properties["hostLoadProvisional"]'
    END_MARKER = 'an fps number measured under provisional host load is not proof of speed, regardless of the floor."'

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="hostload-p3-import-guard-probe-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        source = self.script.read_text(encoding="utf-8")
        self.guard_block = _extract_block(source, self.START_MARKER, self.END_MARKER, trailing_lines=1)
        self.probe = self.tmp / "probe.ps1"
        self.probe.write_text(
            "function Add-Failure {\n"
            "    param($FailureList, [string]$Message)\n"
            "    [void]$FailureList.Add($Message)\n"
            "}\n",
            encoding="utf-8",
        )

    def _run(self, clip_snippet: str) -> subprocess.CompletedProcess:
        script = self.tmp / "run.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f". '{self.probe}'\n"
            f"$clip = {clip_snippet}\n"
            "$clipName = 'probe-clip'\n"
            "$importFailures = [System.Collections.Generic.List[string]]::new()\n"
            f"{self.guard_block}\n"
            "Write-Host \"REFUSED=$($importFailures.Count -gt 0)\"\n",
            encoding="utf-8",
        )
        return subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(script)],
            capture_output=True, text=True, creationflags=_NO_WINDOW,
        )

    def test_provisional_false_with_unknown_state_is_refused(self) -> None:
        # sol's exact round-5 repro, executed against the real guard block instead of searched
        # for as source text.
        proc = self._run("[pscustomobject]@{ hostLoadProvisional = $false; hostLoadState = 'unknown' }")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("REFUSED=True", proc.stdout)

    def test_provisional_false_with_missing_state_property_is_refused(self) -> None:
        proc = self._run("[pscustomobject]@{ hostLoadProvisional = $false }")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("REFUSED=True", proc.stdout)

    def test_provisional_true_is_refused_regardless_of_state(self) -> None:
        proc = self._run("[pscustomobject]@{ hostLoadProvisional = $true; hostLoadState = 'quiet' }")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("REFUSED=True", proc.stdout)

    def test_provisional_false_with_quiet_state_is_accepted(self) -> None:
        # Regression guard: the fix must not turn every clean clip provisional.
        proc = self._run("[pscustomobject]@{ hostLoadProvisional = $false; hostLoadState = 'quiet'; hostLoadCollected = $true }")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("REFUSED=False", proc.stdout)

    def test_provisional_false_with_exceeded_state_is_refused(self) -> None:
        # round 8 (sol MAJOR item 3), exact repro: a SIXTH reader site sharing the same
        # exceeded/false bug -- only hostLoadState=="unknown" forced provisional=true. Clean now
        # requires hostLoadState=="quiet" AND hostLoadProvisional==false.
        proc = self._run("[pscustomobject]@{ hostLoadProvisional = $false; hostLoadState = 'exceeded' }")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("REFUSED=True", proc.stdout)


@requires_pwsh
class P3ValidationHostLoadFieldsAssignmentExecutesTests(_ProbeCase):
    """round 6 (astra MAJOR, production crash): the $hostLoadFields assignment above lives in
    run-ultramagnus-p3-validation.ps1's top-level script body (not inside a named function), so it
    cannot be regex-extracted via _extract_functions like the rest of this file's probes. The old
    form -- `-HostLoad (if ($result) { $result.hostLoad } else { $null })` -- PARSES cleanly
    (single-line if/else blocks are valid PowerShell), which is exactly why the round-5 static
    token/regex checks above stayed green while every real P3 validation run threw
    CommandNotFoundException at runtime: PowerShell parses a bare parenthesized `(if ...)` passed
    as a command argument as an attempt to *invoke* a command literally named `if`. This class
    extracts the exact production assignment line out of the live file by literal text search (the
    same technique the structural tests above already use to locate it) and actually EXECUTES it
    under pwsh against a mocked Get-SmokeSummaryHostLoadFields and both a populated and a null
    $result -- it fails against the pre-round-6 source (CommandNotFoundException, non-zero exit)
    and passes against the fix."""

    script = P3_VALIDATION_SCRIPT
    functions: list[str] = []

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="hostload-p3-assign-probe-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        source = self.script.read_text(encoding="utf-8")
        marker = "$hostLoadFields = Get-SmokeSummaryHostLoadFields -HostLoad "
        start = source.index(marker)
        end = source.index("\n", start)
        self.production_line = source[start:end]
        self.probe = self.tmp / "probe.ps1"
        self.probe.write_text(
            "function Get-SmokeSummaryHostLoadFields {\n"
            "    param($HostLoad)\n"
            "    [pscustomobject]@{ provisional = $false; state = 'quiet'; reason = $null; "
            "hostLoadWasNull = ($null -eq $HostLoad) }\n"
            "}\n",
            encoding="utf-8",
        )

    def _run(self, result_snippet: str) -> subprocess.CompletedProcess:
        script = self.tmp / "run.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f". '{self.probe}'\n"
            f"{result_snippet}\n"
            f"{self.production_line}\n"
            "$hostLoadFields | ConvertTo-Json -Compress\n",
            encoding="utf-8",
        )
        return subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(script)],
            capture_output=True, text=True, creationflags=_NO_WINDOW,
        )

    def test_production_assignment_executes_with_a_populated_result(self) -> None:
        proc = self._run(
            "$result = [pscustomobject]@{ hostLoad = [pscustomobject]@{ provisional = $false; "
            "state = 'quiet' } }"
        )
        self.assertEqual(proc.returncode, 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        payload = json.loads(proc.stdout)
        self.assertFalse(payload["hostLoadWasNull"])

    def test_production_assignment_executes_when_result_is_null(self) -> None:
        proc = self._run("$result = $null")
        self.assertEqual(proc.returncode, 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["hostLoadWasNull"])


class RequireCandidateImprovesPresentedFpsHostLoadGuardTests(unittest.TestCase):
    """Static structural check: tools/profiling/run-release-cuda-playback-ab.ps1's optional
    -RequireCandidateImprovesPresentedFps proof must refuse the fps comparison itself when either
    leg is host-load provisional, rather than computing a "not improved" claim from noisy data --
    round 4 (sol major)."""

    def test_fps_improvement_requirement_refuses_before_comparing_when_host_load_is_provisional(self) -> None:
        source = CUDA_AB_SCRIPT.read_text(encoding="utf-8")
        block_start = source.index("if ($RequireCandidateImprovesPresentedFps) {")
        block_end = source.index("\n}\n", block_start)
        block = source[block_start:block_end]
        self.assertIn("hostLoadProvisional", block)
        self.assertIn("candidate-presented-fps-improvement-not-evaluated", block)
        # residue r1 (sol r6 B1): the guard is the strict Get-HostLoadProofFailures refusal, not a truthy test of the flag
        guard_index = block.index(
            "Get-HostLoadProofFailures -BaselineSummary $baselineSummary -CandidateSummary $candidateForSpeed"
        )
        comparison_index = block.index("$candidateFps -le $baselineFps")
        self.assertLess(
            guard_index, comparison_index,
            "the host-load-provisional guard must be checked before the raw fps comparison",
        )


class LocalCudaProofSummarizerHostLoadGuardTests(unittest.TestCase):
    """Static structural check: tools/profiling/summarize-local-cuda-proof.ps1 must independently
    verify playback A/B host-load provenance rather than relying only on $playbackAb.proofFailures
    (which a legacy/degenerate packet lacking the field would never populate) -- round 4 (sol
    major)."""

    def test_summarizer_checks_host_load_provisional_independently_of_proof_failures(self) -> None:
        source = CUDA_PROOF_SUMMARIZER_SCRIPT.read_text(encoding="utf-8")
        # residue r1 (sol r6 B1): read with PSObject.Properties (Get-Field's return unrolled [false])
        self.assertIn('$legEntry.Leg.PSObject.Properties["hostLoadProvisional"]', source)
        self.assertIn("PLAYBACK_AB_HOST_LOAD_PROVISIONAL", source)
        host_load_guard_index = source.index("$playbackAbHostLoadLegs = @(")
        # "PLAYBACK_PRESENTED_FPS_NOT_IMPROVED" also appears earlier in the file as a reason-code
        # string in an unrelated aggregation helper -- search from the guard onward for the
        # diagnostic emission this test actually cares about.
        fps_not_improved_index = source.index("PLAYBACK_PRESENTED_FPS_NOT_IMPROVED", host_load_guard_index)
        self.assertLess(
            host_load_guard_index, fps_not_improved_index,
            "the host-load-provisional guard must be checked before the fps-not-improved diagnostic",
        )

    def test_summarizer_also_checks_host_load_state_not_just_the_flag(self) -> None:
        # round 5 (sol BLOCKER): reading hostLoadProvisional alone repeats the exact inconsistency
        # round 4 closed at the producer -- a leg with hostLoadProvisional=false and
        # hostLoadState='unknown' (or missing) must still be treated as provisional here.
        # round 8 (sol MAJOR item 3): state is now checked against the canonical predicate
        # (clean iff state=="quiet") rather than only excluding state=="unknown" -- see
        # LocalCudaProofSummarizerGuardExecutesTests for the executable exceeded/false repro.
        source = CUDA_PROOF_SUMMARIZER_SCRIPT.read_text(encoding="utf-8")
        host_load_guard_index = source.index("$playbackAbHostLoadLegs = @(")
        guard_block = source[host_load_guard_index:host_load_guard_index + 3200]
        # residue r1 (sol r6 B1): the strict predicate (ordinal 'quiet', real bools, collected)
        self.assertIn('$legEntry.Leg.PSObject.Properties["hostLoadState"]', guard_block)
        self.assertIn("[string]::Equals($legStateRaw, 'quiet', [StringComparison]::Ordinal)", guard_block)
        self.assertIn('$legEntry.Leg.PSObject.Properties["hostLoadCollected"]', guard_block)

    def test_fps_not_improved_diagnostic_is_skipped_when_host_load_is_provisional(self) -> None:
        source = CUDA_PROOF_SUMMARIZER_SCRIPT.read_text(encoding="utf-8")
        guard_start = source.index("if (-not $playbackAbHostLoadProvisional -and")
        guard_block = source[guard_start:guard_start + 500]
        self.assertIn("PLAYBACK_PRESENTED_FPS_NOT_IMPROVED", guard_block)


@requires_pwsh
class LocalCudaProofSummarizerGuardExecutesTests(_ProbeCase):
    """round 6 (sol + astra MAJOR -- "enforcement tests check TOKENS, not behaviour"): the
    LocalCudaProofSummarizerHostLoadGuardTests class above only searches source text for
    substrings/index ordering -- astra's exact repro: changing the summarizer's -or to -and leaves
    its test green. This class extracts the same per-leg guard block (the foreach loop deciding
    $playbackAbHostLoadProvisional) by literal text search and EXECUTES it, spliced together with
    the real Get-Field/New-Diagnostic/Add-Diagnostic functions it actually calls, against a mocked
    leg list -- so a real -or/-and or $true/$false mutation changes what this test observes."""

    script = CUDA_PROOF_SUMMARIZER_SCRIPT
    functions = ["Get-Field", "New-Diagnostic", "Add-Diagnostic"]

    START_MARKER = "$playbackAbCandidateLeg = if ($playbackAbComparisonBasis -eq \"candidateSpeed\")"
    END_MARKER = (
        "an fps number measured under provisional or unrecorded host load is not a property "
        "of the build."
    )

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="hostload-summarizer-guard-probe-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        source = self.script.read_text(encoding="utf-8")
        guard_block = _extract_block(source, self.START_MARKER, self.END_MARKER, trailing_lines=2)
        probe_source = _extract_functions(self.script, self.functions)
        self.probe = self.tmp / "probe.ps1"
        self.probe.write_text(probe_source, encoding="utf-8")
        self.guard_block = guard_block

    def _run(self, baseline_snippet: str, candidate_snippet: str) -> subprocess.CompletedProcess:
        script = self.tmp / "run.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f". '{self.probe}'\n"
            "$playbackAbComparisonBasis = 'candidate'\n"
            f"$playbackAb = [pscustomobject]@{{ baseline = {baseline_snippet}; "
            f"candidate = {candidate_snippet} }}\n"
            "$playbackAbBlockers = [System.Collections.Generic.List[string]]::new()\n"
            "$diagnostics = [System.Collections.Generic.List[object]]::new()\n"
            f"{self.guard_block}\n"
            "Write-Host \"REFUSED=$playbackAbHostLoadProvisional BLOCKERS=$($playbackAbBlockers.Count)\"\n",
            encoding="utf-8",
        )
        return subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(script)],
            capture_output=True, text=True, creationflags=_NO_WINDOW,
        )

    def test_baseline_provisional_false_with_unknown_state_is_refused(self) -> None:
        # sol's exact round-5 repro, executed against the real guard block instead of searched
        # for as source text.
        proc = self._run(
            "[pscustomobject]@{ hostLoadProvisional = $false; hostLoadState = 'unknown' }",
            "[pscustomobject]@{ hostLoadProvisional = $false; hostLoadState = 'quiet'; hostLoadCollected = $true }",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("REFUSED=True BLOCKERS=1", proc.stdout)

    def test_both_legs_quiet_and_non_provisional_are_accepted(self) -> None:
        proc = self._run(
            "[pscustomobject]@{ hostLoadProvisional = $false; hostLoadState = 'quiet'; hostLoadCollected = $true }",
            "[pscustomobject]@{ hostLoadProvisional = $false; hostLoadState = 'quiet'; hostLoadCollected = $true }",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("REFUSED=False BLOCKERS=0", proc.stdout)

    def test_missing_state_property_on_either_leg_is_refused(self) -> None:
        proc = self._run(
            "[pscustomobject]@{ hostLoadProvisional = $false }",
            "[pscustomobject]@{ hostLoadProvisional = $false; hostLoadState = 'quiet'; hostLoadCollected = $true }",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("REFUSED=True BLOCKERS=1", proc.stdout)

    def test_baseline_provisional_false_with_exceeded_state_is_refused(self) -> None:
        # round 8 (sol MAJOR item 3), exact repro: an exceeded/false leg previously read clean here
        # -- only hostLoadState=="unknown" forced provisional=true. Clean now requires
        # hostLoadState=="quiet" AND hostLoadProvisional==false.
        proc = self._run(
            "[pscustomobject]@{ hostLoadProvisional = $false; hostLoadState = 'exceeded' }",
            "[pscustomobject]@{ hostLoadProvisional = $false; hostLoadState = 'quiet'; hostLoadCollected = $true }",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("REFUSED=True BLOCKERS=1", proc.stdout)


GUI_SMOKE_PROCESS_BOUNDARY_MODULE = ROOT / "tools" / "profiling" / "gui-smoke-process-boundary.psm1"


class ConsumerSweepUltraMagnusAndLocalGpuCapabilityRowsTests(unittest.TestCase):
    """round 6 (astra MAJOR -- "consumer sweep omits..."): run-ultra-magnus-profile.ps1 and
    run-local-gpu-capability.ps1 each publish a multi-run fps table (console Format-Table plus a
    summary.json) with NO PLAYBACK-MEASURE-HOST-LOAD-GATE-1 telemetry at all -- neither file calls
    Get-HostLoadSnapshot, and neither is a New-*Row function inside compare-machine-perf.ps1, so
    CompareMachinePerfRowHostLoadCensusTests above cannot see them (that census's own docstring
    already discloses this exact class of gap). Per the round-6 brief's own fallback ("mark
    provisional/unrecorded like New-ProfileRow, or bring under the census, and say which"): both
    rows are now marked host_load_provisional = $true (run-ultra-magnus-profile.ps1's single row
    kind; run-local-gpu-capability.ps1's "ok" row kind) or $null where there is no fps signal at
    all (run-local-gpu-capability.ps1's "missing-json" row kind) -- the same stance
    compare-machine-perf.ps1's New-ProfileRow/New-FieldLogRow/New-RemoteCdngSummaryRow already
    take, rather than bringing either script under real host-load sampling this round.

    Boundary disclosed, not silently assumed away: this does not extend the machine-perf census's
    regex scan to a repo-wide sweep of tools/profiling/*.ps1 for fps-emitting row/table producers.
    A repo-wide PowerShell-aware scan (distinguishing a genuine ranking/gating row from unrelated
    telemetry-only output across an open set of files with no shared row-function naming
    convention) was judged infeasible to do soundly within this round's smallest-diff scope; these
    two named files are handled directly instead, matching fable's round-4 line already drawn
    around run-local-cuda-playback-dng-smoke.ps1 and detect-playback-artifacts.ps1 (telemetry-only,
    never rank or gate, therefore out of scope entirely). A THIRD script astra did not name could
    still be missed by both this test and the census; that residual boundary is the same one the
    census's own docstring already discloses."""

    def test_ultra_magnus_profile_row_carries_host_load_provisional(self) -> None:
        source = ULTRA_MAGNUS_PROFILE_SCRIPT.read_text(encoding="utf-8")
        row_start = source.index("$rows += [pscustomobject]@{")
        row_end = source.index("\n    }\n", row_start)
        row_block = source[row_start:row_end]
        self.assertIn("fps            =", row_block)
        self.assertIn("host_load_provisional = $true", row_block)

    def test_local_gpu_capability_ok_row_carries_host_load_provisional(self) -> None:
        source = LOCAL_GPU_CAPABILITY_SCRIPT.read_text(encoding="utf-8")
        row_start = source.index('status = "ok"')
        row_end = source.index("\n            }\n", row_start)
        row_block = source[row_start:row_end]
        self.assertIn("fps = if ($cadenceMs", row_block)
        self.assertIn("host_load_provisional = $true", row_block)

    def test_local_gpu_capability_missing_json_row_has_no_fps_signal_to_gate(self) -> None:
        source = LOCAL_GPU_CAPABILITY_SCRIPT.read_text(encoding="utf-8")
        row_start = source.index('status = "missing-json"')
        row_end = source.index("\n                }\n", row_start)
        row_block = source[row_start:row_end]
        self.assertNotIn("fps =", row_block)
        self.assertIn("host_load_provisional = $null", row_block)

    @requires_pwsh
    def test_local_gpu_capability_console_projection_carries_host_load_provisional(self) -> None:
        # round 7 (both keys MAJOR -- "console projection drops host_load_provisional"): the row
        # object has carried host_load_provisional since round 6 (proven above), but the ONE place
        # a human actually reads these fps numbers -- the console Select-Object | Format-Table --
        # silently dropped it. A source-text token check ("does 'host_load_provisional' appear
        # anywhere near Select-Object") would not have caught this: the property genuinely existed
        # on the row object the whole time, just not in the PROJECTION. This EXECUTES the real
        # extracted Select-Object/Format-Table pipeline against a synthetic row and inspects the
        # actual rendered console text, proving the property survives the real projection, not
        # merely that its name appears somewhere in the file.
        #
        # round 7 (continued): the first fix attempt appended host_load_provisional at the END of
        # the Select-Object list, right after fps -- and this exact live test caught that
        # Format-Table -AutoSize silently drops BOTH trailing columns on a normal 120-column
        # console (verified: -Wrap does not help). The production fix moves both columns earlier
        # in the projection; this test's assertions below pin that they render together, not
        # merely that the property name is present in source.
        #
        # round 8 (fable minor 4): the round-7 fix rendered this probe at `Out-String -Width 4096`,
        # which pins projection MEMBERSHIP only -- re-appending both columns at the end of the
        # Select-Object list (the exact truncation defect this test exists to catch) would still
        # pass at 4096 columns wide while genuinely truncating on a real 120-column console. Render
        # at the real width the defect was found at instead.
        source = LOCAL_GPU_CAPABILITY_SCRIPT.read_text(encoding="utf-8")
        block = _extract_block(
            source,
            "$allRows |\n    Select-Object label,",
            "Format-Table -AutoSize",
            trailing_lines=0,
        )
        self.assertIn("host_load_provisional", block, "extraction marker drifted from the real source")
        with tempfile.TemporaryDirectory(prefix="local-gpu-capability-probe-") as tmp:
            script = Path(tmp) / "run.ps1"
            script.write_text(
                "$ErrorActionPreference = 'Stop'\n"
                "$allRows = @([pscustomobject]@{ label = 'current'; run = 'cpu_baseline'; "
                "renderer_verdict = 'nvidia-hardware-gl'; gpu_proc_active = $true; "
                "gpu_deb_active = $true; llrawproc_ms = 1.0; processing_ms = 1.0; "
                "debayer_ms = 1.0; render_ms = 1.0; cadence_ms = 10.0; fps = 100.0; "
                "host_load_provisional = $true })\n"
                f"{block} | Out-String -Width 120\n",
                encoding="utf-8",
            )
            proc = subprocess.run(
                [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-File", str(script)],
                capture_output=True, text=True, creationflags=_NO_WINDOW,
            )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("host_load_provisional", proc.stdout)
        self.assertIn("fps", proc.stdout)


def _one_pixel_png(rgb: tuple[int, int, int]) -> bytes:
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00" + bytes(rgb))) + chunk(b"IEND", b""))


def _run_ps_file(script: Path, *args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script), *args],
                          capture_output=True, text=True, creationflags=_NO_WINDOW, timeout=timeout)


def _run_ps_command(command: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
                          capture_output=True, text=True, creationflags=_NO_WINDOW, timeout=timeout)


QUIET_SMOKE = '{"hostLoad":{"state":"quiet","provisional":false,"before":{"collected":true},"after":{"collected":true}}}'


@requires_pwsh
class ResidueR2CudaAbReadSmokeSummaryTests(_ProbeCase):
    """VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-RESIDUE-1 r2: adversary-07b6a932.json A8 and A9, each fed its own `input`, through the real Read-SmokeSummary."""

    script = CUDA_AB_SCRIPT
    functions = ["Convert-PlaybackLogLineToObject", "Get-ScopedPlaybackLogLines", "Add-NullableDouble", "Get-AverageOrNull", "Convert-ToNullableDouble",
                 "Convert-ToNullableInt64", "Get-NestedValue", "Get-SmokeSummaryHostLoadFields", "Read-SmokeSummary"]
    preamble = f"Import-Module '{VENUE_CHAIN_MODULE}' -Function ConvertFrom-VenueSmokeResultText, Find-VenueJsonDuplicateMember -DisableNameChecking\n"

    def _summary(self, text: str) -> subprocess.CompletedProcess:
        smoke = self.tmp / "smoke.json"
        smoke.write_text(text, encoding="utf-8", newline="")
        return self.run_snippet(
            f"$s = Read-SmokeSummary -Path '{smoke}' -ExitCode 0\n"
            "Write-Host \"PROVISIONAL=$($s.hostLoadProvisional) STATE=$($s.hostLoadState) COLLECTED=$($s.hostLoadCollected) VALID=$($s.validationOk)\"\n"
            "Write-Host \"REASON=$($s.hostLoadReason)\"\n")

    def test_a8_an_exceeded_fully_collected_run_keeps_collected_true(self) -> None:
        # A8, its input: an exceeded (provisional) but fully collected host load. RED: collected was overwritten to false because the text was provisional.
        proc = self._summary('{"hostLoad":{"state":"exceeded","provisional":true,"before":{"collected":true},"during":[],"after":{"collected":true}}}')
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PROVISIONAL=True STATE=exceeded COLLECTED=True", proc.stdout, proc.stdout + proc.stderr)
        # an uncollected snapshot still reads collected false, and the strict quiet block stays clean
        proc = self._summary('{"hostLoad":{"state":"quiet","provisional":false,"before":{"collected":false},"after":{"collected":true}}}')
        self.assertIn("PROVISIONAL=True", proc.stdout, proc.stdout + proc.stderr)
        self.assertIn("COLLECTED=False", proc.stdout, proc.stdout + proc.stderr)
        proc = self._summary(QUIET_SMOKE)
        self.assertIn("PROVISIONAL=False STATE=quiet COLLECTED=True", proc.stdout, proc.stdout + proc.stderr)

    def test_a9_a_case_ambiguous_smoke_result_is_a_recorded_refusal_not_a_throw(self) -> None:
        # A9, its input: hostLoad beside HostLoad. RED: ConvertFrom-Json threw KeysWithDifferentCasingInJsonString before the text reader's refusal was recorded.
        proc = self._summary('{"hostLoad":{"state":"quiet","provisional":false,"before":{"collected":true},"after":{"collected":true}},"HostLoad":{}}')
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PROVISIONAL=True STATE=unknown COLLECTED=False VALID=False", proc.stdout, proc.stdout + proc.stderr)
        self.assertIn("could not be parsed", proc.stdout, proc.stdout + proc.stderr)


@requires_pwsh
class ResidueR2SiblingReaderTests(unittest.TestCase):
    """VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-RESIDUE-1 r2: adversary-07b6a932.json A2, A3, A4, A6, A7 and the P3 siblings of A8/A9, each fed its own `input`."""

    def test_a2_a_comment_between_repeated_members_is_refused_by_machine_perf_and_the_summarizer(self) -> None:
        # A2, its input: hostLoadCollected false, a comment, hostLoadCollected true. ConvertFrom-Json accepts it and keeps true; the strict scanner cannot
        # read it, and that scanner error was swallowed. RED: machine-perf accepted the row, the summarizer summarized it.
        baseline = '{"hostLoadState":"quiet","hostLoadProvisional":false,"hostLoadCollected":false,/* imported note */"hostLoadCollected":true}'
        clean = '{"hostLoadState":"quiet","hostLoadProvisional":false,"hostLoadCollected":true}'
        with tempfile.TemporaryDirectory(prefix="residue-r2-a2-") as tmp:
            record = Path(tmp) / "ab.json"
            record.write_text('{"schema":"release-cuda-playback-ab.v1","baseline":' + baseline + ',"candidate":' + clean + '}', encoding="utf-8", newline="")
            proc = _run_ps_file(COMPARE_MACHINE_PERF_SCRIPT, str(record))
            self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("is not strict JSON", proc.stdout + proc.stderr)
            summary = Path(tmp) / "summary.json"
            summary.write_text('{"status":"PASS","proof":{"playbackAb":{"baseline":' + baseline + ',"candidate":' + clean + '}}}', encoding="utf-8", newline="")
            proc = _run_ps_file(CUDA_PROOF_SUMMARIZER_SCRIPT, "-SummaryPath", str(summary))
            self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("is not strict JSON", proc.stdout + proc.stderr)

    def test_a3_the_planned_leg_type_comes_from_the_planner_never_a_second_read(self) -> None:
        # A3: the matrix parent and the chain re-read each spec path for its type (a spec swapped between the plan and that read changed it), and the chain sent
        # its LegSet child no -ExpectSpecSha256. The planner now records plannedLegType from the bytes it hashed; nobody re-reads the path.
        runner = ROOT / "tools" / "profiling" / "dual-venue" / "DualVenueRunner.psm1"
        proc = _run_ps_command(f"Import-Module '{runner}' -Force -DisableNameChecking; "
                               f"$p = Get-DvLegSetPlan -LegSetPath '{ROOT / 'tools' / 'profiling' / 'dual-venue' / 'legsets' / 'display-matrix.json'}' -Repeats 1 -Backend ''; "
                               "foreach ($e in $p.plan) { Write-Output ('T=' + $e.plannedLegType + ' H=' + $e.specSha256.Length) }")
        types = [l for l in proc.stdout.splitlines() if l.startswith("T=")]
        self.assertTrue(types, proc.stdout + proc.stderr)
        self.assertEqual(set(types), {"T=playback H=64"}, proc.stdout + proc.stderr)
        leg = (ROOT / "tools" / "profiling" / "dual-venue" / "Invoke-VenueLeg.ps1").read_text(encoding="utf-8")
        chain = (ROOT / "tools" / "profiling" / "dual-venue" / "Invoke-VenueChain.ps1").read_text(encoding="utf-8")
        self.assertNotIn("Get-VenuePlannedLegType -SpecPath ([string]$pe.specPath)", leg)
        self.assertNotIn("Get-VenuePlannedLegType -SpecPath ([string]$e.specPath)", chain)
        self.assertIn("$lArgs += @('-ExpectSpecSha256', [string]$leg.specSha256)", chain)

    def test_a4_an_export_receipt_with_invented_tokens_or_playback_bindings_is_not_an_export_leg(self) -> None:
        # A4, its inputs: outcome NONEXISTENT with export.verdict NONEXISTENT, visual {requested:true}, a smoke stderr binding; and outcome PASS with a null verdict.
        # RED: both returned true. The writer's own shapes (a PASS verdict on a PASS receipt; a null verdict on a pre-export INVALID) still do.
        base = ('"schema":"mlv-app/dual-venue-receipt/v1","scale":null,"display":null,"export":{"format":"prores","receiptSha256":"' + "0" * 64 + '","verdict":%s}')
        cases = {
            "invented": '{' + base % '"NONEXISTENT"' + ',"outcome":"NONEXISTENT","visual":{"requested":true},"evidence":{"smokeStderrSha256":"' + "1" * 64 + '"}}',
            "invented-no-bindings": '{' + base % '"NONEXISTENT"' + ',"outcome":"NONEXISTENT","evidence":{}}',
            "visual": '{' + base % '"PASS"' + ',"outcome":"PASS","visual":{"requested":true},"evidence":{}}',
            "stderr": '{' + base % '"PASS"' + ',"outcome":"PASS","evidence":{"smokeStderrSha256":"' + "1" * 64 + '"}}',
            "pass-null-verdict": '{' + base % 'null' + ',"outcome":"PASS","evidence":{}}',
            "writer-pass": '{' + base % '"PASS"' + ',"outcome":"PASS","evidence":{}}',
            "writer-pre-export": '{' + base % 'null' + ',"outcome":"INVALID","evidence":{}}',
            "writer-downgraded": '{' + base % '"PASS"' + ',"outcome":"INVALID","evidence":{}}',
            # adversary-c2bbe5f8.json A7, its input: playback-branch-only bindings (evidenceManifestSha256, artifactIndexPath) on an otherwise export-shaped PASS
            "manifest-binding": '{' + base % '"PASS"' + ',"outcome":"PASS","evidence":{"evidenceManifestSha256":"' + "b" * 64 + '","artifactIndexPath":"C:\\\\evidence\\\\artifact-index.json"}}',
            "unknown-binding": '{' + base % '"PASS"' + ',"outcome":"PASS","evidence":{"someNewSha256":"' + "c" * 64 + '"}}',
            "writer-export-evidence": '{' + base % '"PASS"' + ',"outcome":"PASS","evidence":{"umRunJsonSha256":"' + "d" * 64 + '","summaryJsonSha256":"' + "e" * 64 + '","localEvidenceDir":"C:\\\\ev","umRunOutcome":"PASS","resultJsonSha256":null}}',
        }
        with tempfile.TemporaryDirectory(prefix="residue-r2-a4-") as tmp:
            lines = []
            for name, text in cases.items():
                (Path(tmp) / f"{name}.json").write_text(text, encoding="utf-8", newline="")
                lines.append(f"$rc = Get-Content -Raw -LiteralPath '{Path(tmp) / f'{name}.json'}' | ConvertFrom-Json; Write-Output ('{name}=' + (& $m {{ param($r) Test-VenueReceiptIsExportLeg $r }} $rc))")
            proc = _run_ps_command(f"$m = Import-Module '{VENUE_CHAIN_MODULE}' -DisableNameChecking -PassThru; " + "; ".join(lines))
        got = dict(l.split("=", 1) for l in proc.stdout.splitlines() if "=" in l)
        self.assertEqual(got, {"invented": "False", "invented-no-bindings": "False", "visual": "False", "stderr": "False", "pass-null-verdict": "False",
                               "writer-pass": "True", "writer-pre-export": "True", "writer-downgraded": "True",
                               "manifest-binding": "False", "unknown-binding": "False", "writer-export-evidence": "True"}, proc.stdout + proc.stderr)

    def test_r2_pass4_static_pins(self) -> None:
        # adversary-c2bbe5f8.json: A1 (the failure text withholds the screenshot figures), A2 (the report is written through the identity-checked handle),
        # A3 (a manifest with a null or no artifact is refused), A5 (an unreadable result or summary is a recorded refusal), A8 (identity from the open fd).
        cmp = COMPARE_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("'Screenshot delta exceeded threshold (figures withheld: the verdict is INCONCLUSIVE).'", cmp)
        self.assertIn("$outStream = [IO.FileStream]::new($resolvedOutput, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)", cmp)
        self.assertNotIn("Set-Content -LiteralPath $resolvedOutput", cmp)
        self.assertIn('$fdPath = "/proc/$PID/fd/$fd"', cmp)
        p3 = P3_VALIDATION_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('Add-Failure $importFailures "The manifest lists a null artifact entry."', p3)
        self.assertIn('Add-Failure $importFailures "The manifest lists no artifact; a packet with no checked artifact is not proof."', p3)
        self.assertIn("try { $summaryText = [string](Get-Content -LiteralPath $summaryPath -Raw) }", p3)
        self.assertIn('catch { Add-Failure $clipFailures ("The smoke result JSON could not be read: "', p3)
        cuda = _extract_functions(CUDA_AB_SCRIPT, ["Read-SmokeSummary"])
        self.assertIn("try { $smokeBytes = [IO.File]::ReadAllBytes($Path) }", cuda)

    def test_r2_pass4_a1_the_inconclusive_failure_text_carries_no_screenshot_figure(self) -> None:
        # adversary-c2bbe5f8.json A1, its input: black and white PNGs, before hostLoad null, -FailOnScreenshotDelta. RED: failures kept meanAbsRgbDelta=255.
        with tempfile.TemporaryDirectory(prefix="residue-r2-p4a1-") as tmp:
            black, white = Path(tmp) / "black.png", Path(tmp) / "white.png"
            black.write_bytes(_one_pixel_png((0, 0, 0)))
            white.write_bytes(_one_pixel_png((255, 255, 255)))
            before, after, out = Path(tmp) / "before.json", Path(tmp) / "after.json", Path(tmp) / "compare.json"
            before.write_text(json.dumps({"hostLoad": None, "screenshot": {"path": str(black)}}), encoding="utf-8", newline="")
            after.write_text(json.dumps({"hostLoad": json.loads(QUIET_SMOKE)["hostLoad"], "screenshot": {"path": str(white)}}), encoding="utf-8", newline="")
            proc = _run_ps_file(COMPARE_SCRIPT, "-Before", str(before), "-After", str(after), "-Output", str(out), "-FailOnScreenshotDelta")
            report = json.loads(out.read_text(encoding="utf-8-sig")) if out.is_file() else None
        self.assertIsNotNone(report, proc.stdout + proc.stderr)
        self.assertEqual(report["verdict"], "INCONCLUSIVE")
        self.assertFalse(any("meanAbsRgbDelta=" in f or "changedSampleRatio=" in f for f in report["failures"]), report["failures"])
        self.assertIn("Screenshot delta exceeded threshold (figures withheld: the verdict is INCONCLUSIVE).", report["failures"])

    def test_a4_the_outcome_list_is_the_writers(self) -> None:
        runner = ROOT / "tools" / "profiling" / "dual-venue" / "DualVenueRunner.psm1"
        proc = _run_ps_command(f"Import-Module '{runner}' -Force -DisableNameChecking; $v = Import-Module '{VENUE_CHAIN_MODULE}' -DisableNameChecking -PassThru; "
                               "Write-Output ('W=' + ((Get-DvOutcomeEnum) -join ',')); Write-Output ('V=' + ((& $v { $script:VenueReceiptOutcomeEnum }) -join ','))")
        w = [l[2:] for l in proc.stdout.splitlines() if l.startswith("W=")]
        v = [l[2:] for l in proc.stdout.splitlines() if l.startswith("V=")]
        self.assertTrue(w and v, proc.stdout + proc.stderr)
        self.assertEqual(v, w, "HostLoadReceiptOutcomeEnumParityTests: VenueChain.psm1's outcome list is DualVenueRunner.psm1's $script:OutcomeEnum")

    def _compare(self, before: Path, after: Path, out: Path) -> tuple[subprocess.CompletedProcess, dict | None]:
        proc = _run_ps_file(COMPARE_SCRIPT, "-Before", str(before), "-After", str(after), "-Output", str(out))
        return proc, (json.loads(out.read_text(encoding="utf-8-sig")) if out.is_file() else None)

    def test_a6_a_hard_link_to_the_before_file_is_the_same_file(self) -> None:
        # A6, its input: -After is a hard link to -Before. RED: the paths differ in spelling, so the one file was opened twice and no same-input refusal fired.
        with tempfile.TemporaryDirectory(prefix="residue-r2-a6-") as tmp:
            before, alias, out = Path(tmp) / "before.json", Path(tmp) / "alias.json", Path(tmp) / "compare.json"
            before.write_text(QUIET_SMOKE, encoding="utf-8", newline="")
            os.link(before, alias)
            proc, report = self._compare(before, alias, out)
            self.assertIsNotNone(report, proc.stdout + proc.stderr)
            self.assertTrue(any("same smoke JSON file" in f for f in report["failures"]), report["failures"])
            # a case variant of one file on a case-insensitive directory is that same file too
            out2 = Path(tmp) / "compare2.json"
            proc, report = self._compare(before, Path(tmp) / "BEFORE.JSON", out2)
            self.assertTrue(any("same smoke JSON file" in f for f in report["failures"]), report["failures"])
            # two distinct files are read independently: no same-input refusal and no "paths must be distinct"
            other, out3 = Path(tmp) / "other.json", Path(tmp) / "compare3.json"
            other.write_text(QUIET_SMOKE, encoding="utf-8", newline="")
            proc, report = self._compare(before, other, out3)
            self.assertFalse(any("same smoke JSON file" in f or "paths must be distinct" in f for f in report["failures"]), report["failures"])
        once = _extract_functions(COMPARE_SCRIPT, ["Read-SmokeSourceOnce"])
        self.assertIn("Get-SmokeFileIdentity -Stream $stream", once)
        self.assertNotIn("OrdinalIgnoreCase", once)

    @unittest.skipIf(os.name != "nt", "the handle identity is GetFileInformationByHandle on Windows")
    def test_r2_pass3_a1_the_identity_reads_the_native_volume_serial_and_file_index(self) -> None:
        # adversary-88890049.json A1: the struct was declared with default alignment, so VolumeSerialNumber was read at offset 32 (SizeHigh: 0) and the index
        # from the wrong words; two distinct files on two volumes with the same low index collided. RED: the volume part read 00000000, not the volume's serial.
        with tempfile.TemporaryDirectory(prefix="residue-r2-id-") as tmp:
            probe = Path(tmp) / "probe.ps1"
            target = Path(tmp) / "f.json"
            target.write_text("{}", encoding="utf-8")
            probe.write_text(_extract_functions(COMPARE_SCRIPT, ["Get-SmokeFileIdentity"]) +
                             f"$s = [IO.FileStream]::new('{target}', [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)\n"
                             f"try {{ Write-Output ('ID=' + (Get-SmokeFileIdentity -Stream $s -ResolvedPath '{target}')) }} finally {{ $s.Dispose() }}\n"
                             "Write-Output ('L=' + [MlvSmokeFileIdentityV2]::VolumeSerialOffset() + ',' + [MlvSmokeFileIdentityV2]::FileIndexHighOffset() + ',' + [MlvSmokeFileIdentityV2]::Size())\n",
                             encoding="utf-8")
            proc = _run_ps_file(probe)
            vol = subprocess.run(["cmd", "/c", "vol", str(target)[:2]], capture_output=True, text=True, creationflags=_NO_WINDOW, timeout=60)
        ident = [l[3:] for l in proc.stdout.splitlines() if l.startswith("ID=")]
        self.assertTrue(ident, proc.stdout + proc.stderr)
        self.assertIn("L=28,44,52", proc.stdout, "the declared layout is the native BY_HANDLE_FILE_INFORMATION layout")
        serial = re.search(r"([0-9A-F]{4})-([0-9A-F]{4})", vol.stdout)
        self.assertIsNotNone(serial, vol.stdout + vol.stderr)
        self.assertEqual(ident[0].split(":")[0], serial.group(1) + serial.group(2), (ident, vol.stdout))

    def test_a6_sibling_an_output_that_is_a_hard_link_to_an_input_is_refused(self) -> None:
        # A6 sibling (the output-overwrite guard compared spellings too): -Output names a hard link to -Before. RED: the report overwrote the before input.
        with tempfile.TemporaryDirectory(prefix="residue-r2-a6o-") as tmp:
            before, after, alias = Path(tmp) / "before.json", Path(tmp) / "after.json", Path(tmp) / "out-alias.json"
            before.write_text(QUIET_SMOKE, encoding="utf-8", newline="")
            after.write_text(QUIET_SMOKE, encoding="utf-8", newline="")
            os.link(before, alias)
            proc = _run_ps_file(COMPARE_SCRIPT, "-Before", str(before), "-After", str(after), "-Output", str(alias))
            self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("Output must not overwrite either smoke JSON input.", proc.stdout + proc.stderr)
            self.assertEqual(before.read_text(encoding="utf-8"), QUIET_SMOKE, "the before input is untouched")

    def test_a7_an_inconclusive_comparison_publishes_no_screenshot_delta(self) -> None:
        # A7, its input: 1x1 screenshots RGB (0,0,0) before and (255,255,255) after, the before host load uncollected, default SampleStep, no FailOnScreenshotDelta.
        # RED: verdict INCONCLUSIVE but the report kept meanAbsRgbDelta=255 / changedSampleRatio=1 and the summary line printed them.
        with tempfile.TemporaryDirectory(prefix="residue-r2-a7-") as tmp:
            black, white = Path(tmp) / "black.png", Path(tmp) / "white.png"
            black.write_bytes(_one_pixel_png((0, 0, 0)))
            white.write_bytes(_one_pixel_png((255, 255, 255)))
            uncollected = '{"state":"quiet","provisional":false,"before":{"collected":false},"after":{"collected":true}}'
            before, after, out = Path(tmp) / "before.json", Path(tmp) / "after.json", Path(tmp) / "compare.json"
            before.write_text(json.dumps({"hostLoad": json.loads(uncollected), "screenshot": {"path": str(black)}}), encoding="utf-8", newline="")
            after.write_text(json.dumps({"hostLoad": json.loads(QUIET_SMOKE)["hostLoad"], "screenshot": {"path": str(white)}}), encoding="utf-8", newline="")
            proc, report = self._compare(before, after, out)
        self.assertIsNotNone(report, proc.stdout + proc.stderr)
        self.assertEqual(report["verdict"], "INCONCLUSIVE")
        shot = report["screenshot"]
        self.assertEqual(shot["status"], "withheld-inconclusive", shot)
        for field in ("meanAbsRgbDelta", "maxAbsRgbDelta", "changedSampleRatio", "sameSha256", "sampledPixels"):
            self.assertIsNone(shot[field], (field, shot))
        line = [l for l in proc.stdout.splitlines() if l.startswith("GUI-SMOKE-AB verdict=")]
        self.assertTrue(line, proc.stdout)
        self.assertIn("screenshot_status=withheld-inconclusive mean_abs_rgb_delta= changed_sample_ratio= ", line[0])

    def test_p3_siblings_of_a8_and_a9(self) -> None:
        # A8 (P3 :1308): collected was replaced by false when the text was provisional. A9 (P3 :589, :1182): the object parse threw before the refusal was recorded.
        p3 = P3_VALIDATION_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("collected = $bothCollected; reason = \"smoke result text: $($resultTextStanding.reason)\"", p3)
        self.assertNotIn("collected = $false; reason = \"smoke result text:", p3)
        self.assertIn("try { $result = ConvertFrom-Json -InputObject $resultText -NoEnumerate }", p3)
        self.assertIn("try { $summary = $summaryText | ConvertFrom-Json }", p3)
        # (A9 sibling: the packet manifest's parse is a recorded import failure too)
        self.assertIn("try { $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json }", p3)
        self.assertNotIn("        $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json\n", p3)
        # adversary-88890049.json A2: a null manifest lists no artifact (@($null) is one null entry whose empty path named the extraction directory)
        self.assertIn("foreach ($file in @(@($manifest.files) | Where-Object { $null -ne $_ })) {", p3)
        self.assertIn("if (!(Test-Path -LiteralPath $artifactPath -PathType Leaf)) {", p3)
        self.assertNotIn("foreach ($file in @($manifest.files)) {", p3)
        # the reduced execution of that loop: a null manifest and an entry with no relativePath record failures and never throw
        loop = _extract_block(p3, "        foreach ($file in @(@($manifest.files) | Where-Object { $null -ne $_ })) {", "Add-Failure $importFailures \"Artifact hash mismatch", trailing_lines=3)
        with tempfile.TemporaryDirectory(prefix="residue-r2-p3m-") as tmp:
            script = Path(tmp) / "loop.ps1"
            script.write_text("$ErrorActionPreference = 'Stop'\n$importFailures = [System.Collections.Generic.List[string]]::new()\n"
                              "function Add-Failure($List, [string]$Message) { $List.Add($Message) }\nfunction Get-FileSha256([string]$Path) { throw 'hash of a directory' }\n"
                              f"$extractRoot = '{tmp}'\n"
                              "foreach ($m in @($null, ('{\"files\":[{}]}' | ConvertFrom-Json))) { $manifest = $m\n" + loop + "}\n"
                              "Write-Output ('F=' + ($importFailures -join '|'))\n", encoding="utf-8")
            proc = _run_ps_file(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("F=The manifest lists an artifact with no relativePath.", proc.stdout, proc.stdout + proc.stderr)
        self.assertLess(p3.index("$summaryDuplicate = try { Find-VenueJsonDuplicateMember -Text $summaryText -All }"), p3.index("try { $summary = $summaryText | ConvertFrom-Json }"))
        self.assertLess(p3.index("$resultTextStanding = ConvertFrom-VenueSmokeResultText -Text $resultText"), p3.index("try { $result = ConvertFrom-Json -InputObject $resultText -NoEnumerate }"))


@requires_pwsh
class WholeFileParseSafetyNetTests(unittest.TestCase):
    """round 5: fable's round-3 finding was that the splice-based extraction technique this whole
    test module relies on (_extract_functions) is BLIND to a syntax error outside any spliced
    function -- a top-level break in run-ultramagnus-p3-validation.ps1 or any sibling script could
    pass all 63 tests here silently, because every test only ever compiles the ONE named function
    it spliced out, never the whole file. The round-4 producer explicitly DEFERRED this ("no parse
    safety net exists" -- fable round-4 minor); the round-4 HUB ADDENDUM then wrongly told both
    review keys it HAD been added (filed as HUB-ADDENDUM-CLAIMED-A-FIX-THAT-WAS-DEFERRED-1). This
    closes it for real, using the same technique as test_candidate_acceptance.py:1417
    (System.Management.Automation.Language.Parser]::ParseFile), swept over every .ps1/.psm1 file
    this card touches."""

    FILES = (
        SMOKE_SCRIPT,
        COMPARE_SCRIPT,
        CUDA_AB_SCRIPT,
        COMPARE_MACHINE_PERF_SCRIPT,
        P3_VALIDATION_SCRIPT,
        CUDA_PROOF_SUMMARIZER_SCRIPT,
        GUI_SMOKE_PROCESS_BOUNDARY_MODULE,
        # round 6 (astra MAJOR, consumer sweep): both scripts newly carry a
        # host_load_provisional literal in a row object -- added to the parse safety net
        # alongside that change, same as every other file this card touches.
        ULTRA_MAGNUS_PROFILE_SCRIPT,
        LOCAL_GPU_CAPABILITY_SCRIPT,
    )

    @staticmethod
    def _parse_check_command(path: Path) -> str:
        literal = str(path).replace("'", "''")
        return (
            "$errors=$null; $tokens=$null; "
            "[System.Management.Automation.Language.Parser]::ParseFile("
            f"'{literal}',[ref]$tokens,[ref]$errors) | Out-Null; "
            "if($errors.Count){$errors | ForEach-Object {$_.Message}; exit 1}"
        )

    @requires_pwsh
    def test_every_host_load_gate_script_parses_cleanly(self) -> None:
        self.assertEqual(len(self.FILES), 9, "the file list drifted -- update it alongside the card's file set")
        for path in self.FILES:
            self.assertTrue(path.is_file(), f"missing file: {path}")
            proc = subprocess.run(
                [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", self._parse_check_command(path)],
                capture_output=True, text=True, creationflags=_NO_WINDOW,
            )
            self.assertEqual(proc.returncode, 0, f"{path} failed to parse:\n{proc.stdout}{proc.stderr}")

    @requires_pwsh
    def test_a_top_level_syntax_error_outside_any_function_is_actually_caught(self) -> None:
        # Proves this is a real detector, not a tautology: a top-level syntax error OUTSIDE any
        # function definition (exactly the class of break the splice-based tests above cannot
        # see, since they only ever compile one named function body at a time) must fail here.
        with tempfile.TemporaryDirectory(prefix="parse-sweep-negative-") as tmp:
            broken = Path(tmp) / "broken.ps1"
            broken.write_text(
                "function Foo {\n    Write-Host 'ok'\n}\n"
                "$leftoverTopLevelBreak = 1 +\n",
                encoding="utf-8",
            )
            proc = subprocess.run(
                [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", self._parse_check_command(broken)],
                capture_output=True, text=True, creationflags=_NO_WINDOW,
            )
            self.assertNotEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
