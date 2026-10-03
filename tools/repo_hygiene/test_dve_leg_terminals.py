"""Tests for DVE-LEG-TERMINALS-1 (round 1): every owner leg's failure terminal yields a receipt and keeps its evidence.

CLASS: a leg that ran never ends without a receipt, and a failure terminal never discards the evidence that says what ran.
Source: the first real owner legs on Ultra-Magnus (VENUE-OWNER-LEGS-UM-1 r1, clip M16-1243 by id only) and fable's PR #225 r1 hardening
DVE-PRESENTMON-WAIT-FAILURE-UNRECEIPTABLE-1. The summary shapes below are copied from that evidence (keys and shapes only: no footage path, name or
frame is read or written here).

  1. a PresentMon WAIT failure carries the app run log's playback_smoke gpu summary counters into its summary.json, so the backend is derivable and the
     runner writes the advisory FAIL receipt; counters that are genuinely unavailable end as a typed no-signal (INVALID) receipt, never a refused write;
  2. a CPU leg's skipped/unpresented playback-quality limit does not fail the run (the ratio is a measured field); a CUDA leg keeps the gate;
  3. SMOKE_RUN_FAILED publishes the runner's full stdout and stderr, the launcher's result.json and the run log, and the receipt keeps them;
  4. a PresentMon wait failure after the measured playback still publishes the contact-sheet frames the app's un-timed seek pass captured.

Round 2 (the same class, one case it left open): a PASS/FAIL the production validator refuses -- e.g. BACKEND_MISMATCH, a cuda leg whose app fell back wholly to
the cpu path and then lost its PresentMon wait -- is written by Complete-Receipt as a typed INVALID that keeps the evidence and names the reasons, never exit 2 with no
receipt; and the wait-failure branch lists frames only when the run's own counters do not contradict the leg's backend (DVE-WAIT-FAILURE-FRAMES-BACKEND-GATE-1).

The job's failure branches are EXECUTED (the real text, sliced out of the generator's template, run in pwsh against stubs); the runner is executed with
the same stub um-run the sibling suite uses. Every rule has a mutation test.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from tools.repo_hygiene import test_dual_venue_evidence as dve
from tools.repo_hygiene.test_dual_venue_evidence import (
    DV,
    GENERATOR,
    OWNER_CLIP,
    ROOT,
    ModuleMutationMixin,
    RunnerHarness,
    lf,
    real_failure_summary,
    requires_windows_pwsh,
    run_pwsh,
)

MODULE = ROOT / "tools" / "profiling" / "bachelor" / "AttrCudaArtifacts.psm1"
DV_MODULE = DV / "DualVenueRunner.psm1"
TEMPLATE = lf(GENERATOR.read_text(encoding="utf-8"))


CPU_WAIT_SKIP = "if ($null -ne $presentMonWaitError -and $Backend -ne 'cpu') {"


def cpu_job_with_wait_branch(text: str) -> str:
    """DVE-PRESENTMON-EVIDENCE-1 item 3: a cpu job no longer enters the wait-failure branch (PresentMon is informational there). The cpu-variant edits INSIDE the branch
    (the backend field, the inverse frame gate) are still exercised, with that one skip neutralised."""
    assert text.count(CPU_WAIT_SKIP) == 1, "the cpu job carries the wait-failure skip exactly once"
    return text.replace(CPU_WAIT_SKIP, "if ($null -ne $presentMonWaitError) {", 1)


def _slice(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i)]


WAIT_START = "$presentMonWaitError = $null\n"
WAIT_END = "\n# CUDA-PERF-DISPLAY-WAKE-3 round 1b (sol BLOCKER)"
SMOKE_START = "$presentMonSpawnError = $null"
SMOKE_END = "$rawResult = [IO.File]::ReadAllText($resultPath)"


def wait_block(text: str = TEMPLATE) -> str:
    return _slice(text, WAIT_START, WAIT_END)


def smoke_block(text: str = TEMPLATE) -> str:
    return _slice(text, SMOKE_START, SMOKE_END)


def gpu_summary_fn(text: str = TEMPLATE) -> str:
    return _slice(text, "function Get-LastGpuSummary(", "\nfunction Build-AttrCudaDisplayBlock")


def presentmon_functions(text: str = TEMPLATE) -> str:
    return _slice(text, "function Start-PresentMonCapture(", "\nfunction Get-FrameRows(")


# --- the real Ultra-Magnus shapes (VENUE-OWNER-LEGS-UM-1 r1, evidence summary.json key lists; the display block is cut to what the validator reads) ----------------
PM_TIMEOUT_REASON = "PRESENTMON_TIMEOUT: did not exit within 35 s after playback (confirmedExited=True killError=<none> waitError=<none>)"
UM_GPU_SUMMARY_LINE = ("[2026-10-02T16:12:29.519Z] [INFO] [0xec40] playback_smoke.gpu_summary session=1 cpu_frames=0 gpu_preview_frames=0 "
                       "gpu_recon_readback_frames=0 gpu_texture_readback_frames=0 gpu_texture_no_readback_frames=478")
UM_CPU_GPU_SUMMARY_LINE = ("[2026-10-02T16:12:29.519Z] [INFO] [0xec40] playback_smoke.gpu_summary session=1 cpu_frames=520 gpu_preview_frames=0 "
                           "gpu_recon_readback_frames=0 gpu_texture_readback_frames=0 gpu_texture_no_readback_frames=0")


UM_PARTIAL_GPU_SUMMARY_LINE = ("[2026-10-02T16:12:29.519Z] [INFO] [0xec40] playback_smoke.gpu_summary session=1 cpu_frames=12 gpu_preview_frames=0 "
                               "gpu_recon_readback_frames=0 gpu_texture_readback_frames=0 gpu_texture_no_readback_frames=466")


def um_run_log(gpu_line: str | None = UM_GPU_SUMMARY_LINE) -> str:
    lines = ["[2026-10-02T16:12:03.758Z] [INFO] [0xec40] playback_smoke.measured_session id=1"]
    if gpu_line is not None:
        lines.append(gpu_line)
    return "\n".join(lines) + "\n"


def um_wait_failure_summary() -> dict:
    """The summary.json the job wrote for UM CUDA attempts 1 and 2 (evidence 9f84e0db / 863dcc55): top-level keys, in order, with NO counters."""
    return {"schema": "playback-attr-3-cuda-venue.v1", "result": "PRESENTMON_UNAVAILABLE", "fixtureRehearsal": False,
            "displayWake": {"attempted": True, "keepAliveNudgeState": {"count": 13, "failureCount": 0}},
            "reason": PM_TIMEOUT_REASON, "presentMonStatus": "unavailable", "chains": [],
            "presentMonCaptureStartUtc": "2026-10-02T16:11:26.9791787Z", "display": {"phase": "post-smoke", "measured": True, "venue": "ultra-magnus"},
            "frameRows": 0, "sourceCommit": "38ed2d8f96c29df273d1de60f2dfd8a03ab6873a", "clipId": OWNER_CLIP, "artifactRoot": "X:\\stub\\artifacts"}


def um_smoke_failed_summary(stderr_tail: str) -> dict:
    """The summary.json the job wrote for UM CPU attempts 1 and 2 (evidence 2ebc7613 / ef0e830b): exit 1, the result.json present, an empty tail on attempt 2."""
    return {"schema": "playback-attr-3-cuda-venue.v1", "result": "SMOKE_RUN_FAILED", "fixtureRehearsal": False,
            "displayWake": {"attempted": True}, "smokeExitCode": 1, "smokeResultPresent": True, "smokeRefusalReason": "NONE",
            "smokeStderrTail": stderr_tail, "smokeLaunchExceptionType": None, "smokeLaunchExceptionMessage": None,
            "presentMonConfirmedExited": True, "presentMonKillError": None, "presentMonWaitError": None,
            "display": {"phase": "post-smoke", "measured": True, "venue": "ultra-magnus"},
            "displayLogRecovery": {"found": True, "logPath": "X:\\stub\\logs-result-0\\mlvapp.log", "reason": None},
            "sourceCommit": "38ed2d8f96c29df273d1de60f2dfd8a03ab6873a", "clipId": OWNER_CLIP, "artifactRoot": "X:\\stub\\artifacts"}


class SliceHarness:
    """Runs a real slice of the job template in pwsh against stubs. The slices are the job's own text (CRLF-normalised), so what runs here is what runs on the venue."""

    def make_slice_dir(self) -> None:
        self._stmp = tempfile.TemporaryDirectory(prefix="dve-leg-")
        self.addCleanup(self._stmp.cleanup)
        self.stmp = Path(self._stmp.name)

    def run_wait_failure(self, name: str, run_log: str | None, backend: str | None = None, contact_frames: dict[str, bytes] | None = None,
                         enabled: bool = True, text: str | None = None, rows: int = 0, block_contact_dir: bool = False) -> tuple[int, dict, Path, str]:
        pub = self.stmp / name / "pub"
        pub.mkdir(parents=True)
        if block_contact_dir:
            (pub / "contact-sheet").write_bytes(b"a file where the publish needs a directory")
        cs = self.stmp / name / "contact-sheet"
        cs.mkdir()
        for fname, data in (contact_frames or {}).items():
            (cs / fname).write_bytes(data)
        log_file = self.stmp / name / "run.log"
        log_file.write_text(run_log if run_log is not None else "", encoding="utf-8")
        body = text if text is not None else TEMPLATE
        script = self.stmp / name / "probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{MODULE}' -Force\n"
            + gpu_summary_fn(body) + "\n"
            f"$Pub = '{pub}'\n$FixtureRehearsal = $false\n$SourceCommit = ('1' * 40)\n$ClipId = '{OWNER_CLIP}'\n"
            + (f"$Backend = '{backend}'\n" if backend else "")
            + "function Save-Json($Object, [string]$Path) { [void](Publish-AttrCudaText -Path $Path -Value ($Object | ConvertTo-Json -Depth 30)) }\n"
            "function Write-JobTrace([string]$Message) { }\n"
            "function Get-AttrCudaDisplayResultTail([object]$DisplayBlock) { '' }\n"
            f"function Wait-PresentMonCapture($Proc) {{ throw '{PM_TIMEOUT_REASON}' }}\n"
            "$presentMonProc = $null\n"
            f"$presentMonPath = '{self.stmp / name / 'presentmon.csv'}'\n"
            # DVE-PRESENTMON-EVIDENCE-1: the job defines these before it spawns PresentMon (the stream files and the readiness note the stop block reads)
            f"$presentMonStdoutPath = '{self.stmp / name / 'presentmon-stdout.txt'}'\n$presentMonStderrPath = '{self.stmp / name / 'presentmon-stderr.txt'}'\n"
            "$presentMonStreams = [ordered]@{ csvSeenDuringReadiness = $false }\n"
            "$presentMonCaptureStartUtc = [datetime]::UtcNow; $presentMonPreSpawnUtc = $presentMonCaptureStartUtc; $presentMonPostSpawnUtc = $presentMonCaptureStartUtc\n"
            "$presentMonProcessStartUtc = $presentMonCaptureStartUtc; $presentMonCaptureStartUncertaintyMs = 1.0\n"
            "$displayWake = [ordered]@{}\n$displayBlock = [ordered]@{ venue = 'ultra-magnus' }\n"
            + (f"$rows = @(1..{rows})\n" if rows else "$rows = @()\n")
        )
        with script.open("a", encoding="utf-8") as fh:
            fh.write(f"$rawLog = [IO.File]::ReadAllText('{log_file}')\n$measuredSmokeSessionId = '1'\n"
                     f"$ContactSheetEnabled = ${'true' if enabled else 'false'}\n$contactSheetDir = '{cs}'\n"
                     + wait_block(body) + "\nWrite-Output 'RESULT=NO_FAILURE_BRANCH_TAKEN'\n")
        proc = run_pwsh(["-File", str(script)])
        summary_path = pub / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
        return proc.returncode, summary, pub, proc.stdout + proc.stderr

    def derived_backend(self, summary: dict) -> str:
        proc = run_pwsh(["-Command", f"Import-Module '{DV_MODULE}' -Force\n$s = $env:DVE_S | ConvertFrom-Json\n$b = Get-DvDerivedBackend -Summary $s\n"
                                     "Write-Output ('BACKEND=' + $(if ($null -eq $b) { '<none>' } else { $b }))"], env_extra={"DVE_S": json.dumps(summary)})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return next(l for l in proc.stdout.splitlines() if l.startswith("BACKEND="))[len("BACKEND="):]

    def run_smoke_failure(self, name: str, cmd: str, result_json_with_log: str | None = None, stub_found_log: Path | None = None,
                          text: str | None = None) -> tuple[int, dict, Path, str]:
        """The SMOKE_RUN_FAILED branch against a REAL failing child pwsh (as the sibling suite does), with the launcher's result.json optionally present."""
        work = self.stmp / name / "work"
        leg_out = work / "legOut"
        leg_out.mkdir(parents=True)
        pub = self.stmp / name / "pub"
        pub.mkdir()
        result_path = leg_out / "result.json"
        if result_json_with_log is not None:
            log_path = leg_out / "result.json.run.log"
            log_bytes = result_json_with_log.encode("utf-8")
            log_path.write_bytes(log_bytes)
            result_path.write_text(json.dumps({"schema": "mlvapp-gui-smoke-result.v2", "log": {"path": str(log_path)},
                                               "evidence": {"runNonce": "n" * 32, "runLogSnapshot": {"sha256": hashlib.sha256(log_bytes).hexdigest(), "length": len(log_bytes)}},
                                               "validation": {"ok": False, "skippedOrUnpresentedRatio": 0.5617}}), encoding="utf-8")
        body = text if text is not None else TEMPLATE
        real_pwsh_dir = str(Path(shutil.which("pwsh")).resolve().parent)
        stub_root = self.stmp / name / "pf-stub"
        cmd_literal = "'" + cmd.replace("'", "''") + "'"
        found = (f"function Find-AttrCudaFailedSmokeDisplayLog {{ param($LegOut, $LaunchedAtUtc) [pscustomobject]@{{ found = $true; reason = $null; logPath = '{stub_found_log}'; selection = @{{}} }} }}\n"
                 "function Build-AttrCudaDisplayBlock { [ordered]@{ venue = 'ultra-magnus' } }\n$windowsDisplayInventory = $null; $measurementVenue = 'ultra-magnus'\n"
                 "$expectedDisplayWidth = 3840; $expectedDisplayHeight = 2160; $displayPreferResolution = $null\n") if stub_found_log else ""
        script = self.stmp / name / "probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{MODULE}' -Force\n$stubRoot = '{stub_root}'\n"
            "New-Item -ItemType Directory -Path (Join-Path $stubRoot 'PowerShell') -Force | Out-Null\n"
            f"New-Item -ItemType Junction -Path (Join-Path $stubRoot 'PowerShell\\7') -Target '{real_pwsh_dir}' | Out-Null\n"
            "$env:ProgramFiles = $stubRoot\n"
            f"$cmd = {cmd_literal}\n$Work = '{work}'\n$legOut = '{leg_out}'\n$resultPath = '{result_path}'\n"
            f"$presentMonPath = '{self.stmp / name / 'presentmon.csv'}'\n$FixtureRehearsal = $false\n$SourceCommit = ('1' * 40)\n$ClipId = '{OWNER_CLIP}'\n$Pub = '{pub}'\n"
            "function Save-Json($Object, [string]$Path) { [void](Publish-AttrCudaText -Path $Path -Value ($Object | ConvertTo-Json -Depth 30)) }\n"
            "function Write-JobTrace([string]$Message) { }\n"
            "$displayWake = [ordered]@{}\n$displayBlock = [ordered]@{ venue = 'ultra-magnus' }\n"
            "$displayWakeKeepAlive = [ordered]@{ setupError = $null; asyncResult = $null; stopEvent = $null; nudgeState = [ordered]@{ failureCount = 0; lastError = $null; lastFailureUtc = $null } }\n"
            + presentmon_functions(body) + "\n"
            "function Start-PresentMonCapture([string]$CsvPath) { Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile','-NonInteractive','-Command','Start-Sleep -Seconds 120') -PassThru -WindowStyle Hidden }\n"
            + found + smoke_block(body) + "\nWrite-Output 'RESULT=NO_FAILURE_BRANCH_TAKEN'\n", encoding="utf-8")
        proc = run_pwsh(["-File", str(script)])
        summary_path = pub / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
        return proc.returncode, summary, pub, proc.stdout + proc.stderr


FRAMES = {"frame-00.png": b"\x89PNG\r\n\x1a\nraw0", "frame-00.json": json.dumps({"index": 0, "saved": True, "path": "frame-00.png"}).encode("utf-8"),
          "frame-01.png": b"\x89PNG\r\n\x1a\nraw1", "frame-01.json": json.dumps({"index": 1, "saved": True, "path": "frame-01.png"}).encode("utf-8")}
LONG_STDERR_CMD = ("1..60 | ForEach-Object { [Console]::Error.WriteLine(('stderr line {0:D2} ' -f $_) + ('x' * 90)) }; [Console]::Out.WriteLine('stdout first line'); exit 1")


@requires_windows_pwsh
class PresentMonWaitFailureCarriesTheCountersTests(SliceHarness, unittest.TestCase):
    """Item 1 (hardening DVE-PRESENTMON-WAIT-FAILURE-UNRECEIPTABLE-1): the wait-failure summary carried no counters, so the backend was not derivable and the runner
    wrote NO receipt. It now carries the app run log's gpu summary the way every other failure terminal does."""

    def setUp(self) -> None:
        self.make_slice_dir()

    def test_the_wait_failure_summary_carries_the_gpu_counters_and_the_backend_is_derivable(self) -> None:
        code, summary, _pub, out = self.run_wait_failure("cuda", um_run_log())
        self.assertEqual(code, 23, out)
        self.assertEqual(summary["result"], "PRESENTMON_UNAVAILABLE")
        self.assertEqual(summary["gpuSummary"], {"cpuFrames": 0, "gpuPreviewFrames": 0, "gpuReconReadbackFrames": 0, "gpuTextureReadbackFrames": 0,
                                                 "gpuTextureNoReadbackFrames": 478})
        self.assertEqual(summary["gpuFramesTotal"], 478, "the sum the job computes everywhere else (recon + texture readback + texture no-readback)")
        self.assertEqual(self.derived_backend(summary), "cuda")
        # nothing the UM shape already carried was dropped
        for key, value in um_wait_failure_summary().items():
            if key not in ("displayWake", "display", "presentMonCaptureStartUtc", "sourceCommit", "artifactRoot", "frameRows", "reason"):
                self.assertEqual(summary.get(key), value, key)
        self.assertEqual(summary["reason"], PM_TIMEOUT_REASON)

    def test_a_cpu_leg_wait_failure_states_its_backend_and_derives_cpu(self) -> None:
        # the cpu variant's edit (below) writes `backend` here, as it does on the success summary and CPU_BACKEND_PATH_MISMATCH: a cpu run's backend is read from it
        text = self.cpu_job_text()
        code, summary, _pub, out = self.run_wait_failure("cpu", um_run_log(UM_CPU_GPU_SUMMARY_LINE), backend="cpu", text=text)
        self.assertEqual(code, 23, out)
        self.assertEqual(summary["backend"], "cpu")
        self.assertEqual(summary["gpuSummary"]["cpuFrames"], 520)
        self.assertEqual(summary["gpuFramesTotal"], 0)
        self.assertEqual(self.derived_backend(summary), "cpu")

    def cpu_job_text(self) -> str:
        return cpu_job_with_wait_branch(CpuGeneratedJobs.text("cpu"))

    def test_counters_that_are_genuinely_unavailable_still_end_in_a_typed_summary_not_a_crash(self) -> None:
        # a log with no playback_smoke.gpu_summary line: Get-LastGpuSummary throws. The branch must still write its typed summary and exit 23 (a raw throw
        # published nothing at all); the counters are null and the runner maps that to a typed no-signal receipt (tests below).
        code, summary, _pub, out = self.run_wait_failure("nocounters", um_run_log(None))
        self.assertEqual(code, 23, out)
        self.assertEqual(summary["result"], "PRESENTMON_UNAVAILABLE")
        self.assertIn("gpuSummary", summary)
        self.assertIsNone(summary["gpuSummary"])
        self.assertIsNone(summary["gpuFramesTotal"])
        self.assertEqual(self.derived_backend(summary), "<none>")

    def test_mutation_without_the_counters_the_backend_is_not_derivable_again(self) -> None:
        mutated = TEMPLATE.replace("        gpuSummary=$waitFailureGpuSummary\n", "", 1).replace("        gpuFramesTotal=$waitFailureGpuFramesTotal\n", "", 1)
        self.assertNotEqual(mutated, TEMPLATE, "the mutation anchor must exist")
        code, summary, _pub, out = self.run_wait_failure("mut1", um_run_log(), text=mutated)
        self.assertEqual(code, 23, out)
        self.assertNotIn("gpuSummary", summary)
        self.assertEqual(self.derived_backend(summary), "<none>", "the UM shape of 2026-10-02: no counters, no backend, no receipt")


@requires_windows_pwsh
class PresentMonWaitFailureKeepsTheContactFramesTests(SliceHarness, unittest.TestCase):
    """Item 4: the app's contact-sheet capture is a SEEK pass (--contact-sheet-seek-mode) that runs inside the smoke child AFTER the measured session's
    playback_smoke.summary line (UM evidence: the summary at 16:12:29.519, the six seeks from 29.523 to 39.5), and PresentMon is waited on only after the
    smoke child returned. So a PresentMon wait failure leaves the frames in the job's contact-sheet dir with nothing to re-run; the job only has to publish
    them (it never did, so the leg had no PNG at all). No measured number can change: the publish runs after the smoke child, after the log is resolved and
    the summary counters are read, and writes only under contact-sheet/."""

    def setUp(self) -> None:
        self.make_slice_dir()

    def test_the_captured_frames_and_a_compose_marker_are_published_on_a_wait_failure(self) -> None:
        code, summary, pub, out = self.run_wait_failure("frames", um_run_log(), contact_frames=FRAMES)
        self.assertEqual(code, 23, out)
        raw = pub / "contact-sheet" / "raw"
        self.assertEqual(sorted(p.name for p in raw.iterdir()), sorted(FRAMES))
        for name, data in FRAMES.items():
            self.assertEqual((raw / name).read_bytes(), data)
        marker = (pub / "contact-sheet" / "compose-status.txt").read_text(encoding="utf-8")
        self.assertTrue(marker.startswith("CONTACT_SHEET_COMPOSE_UNAVAILABLE"), marker)
        self.assertFalse((pub / "contact-sheet" / "sheet.png").exists(), "the sheet is composed locally from the published frames, never invented here")

    def test_no_contact_sheet_run_publishes_no_contact_sheet_directory(self) -> None:
        code, _summary, pub, out = self.run_wait_failure("off", um_run_log(), contact_frames=FRAMES, enabled=False)
        self.assertEqual(code, 23, out)
        self.assertFalse((pub / "contact-sheet").exists())

    def test_a_run_that_captured_no_frame_writes_no_marker(self) -> None:
        code, _summary, pub, out = self.run_wait_failure("empty", um_run_log(), contact_frames={})
        self.assertEqual(code, 23, out)
        self.assertFalse((pub / "contact-sheet" / "compose-status.txt").exists(), "a marker would make the runner list a frame set that is empty")

    def test_publishing_the_frames_changes_no_number_in_the_summary(self) -> None:
        # the same wait failure with the contact sheet off and on: summary.json is identical key for key, value for value (only the artifacts directory differs)
        _c1, off, _p1, _o1 = self.run_wait_failure("neq-off", um_run_log(), contact_frames=FRAMES, enabled=False, rows=7)
        _c2, on, _p2, _o2 = self.run_wait_failure("neq-on", um_run_log(), contact_frames=FRAMES, enabled=True, rows=7)
        strip = lambda s: {k: v for k, v in s.items() if k not in ("presentMonCaptureStartUtc", "artifactRoot")}
        self.assertEqual(strip(off), strip(on))
        self.assertEqual(on["frameRows"], 7)

    def test_a_frame_that_cannot_be_published_never_costs_the_typed_summary(self) -> None:
        code, summary, _pub, out = self.run_wait_failure("blocked", um_run_log(), contact_frames=FRAMES, block_contact_dir=True)
        self.assertEqual(code, 23, out)
        self.assertEqual(summary["result"], "PRESENTMON_UNAVAILABLE")
        self.assertEqual(summary["gpuFramesTotal"], 478)

    def test_the_publish_comes_after_the_wait_and_before_the_terminal_exit(self) -> None:
        block = wait_block()
        self.assertLess(block.index("Wait-PresentMonCapture $presentMonProc"), block.index("Publish-AttrCudaContactSheetRawCaptures"))
        self.assertLess(block.index("Publish-AttrCudaContactSheetRawCaptures"), block.rindex("exit 23"))
        # ... and the smoke child (the only thing that measures) is long over: it is launched before this block in the template
        self.assertLess(TEMPLATE.index("step smoke-launch done rc="), TEMPLATE.index(WAIT_START))

    def test_mutation_without_the_publish_call_the_leg_has_no_frame(self) -> None:
        call = "$waitFailureRawFrames = Publish-AttrCudaContactSheetRawCaptures -Enabled $ContactSheetEnabled -SourceDir $contactSheetDir -PubRoot $Pub"
        self.assertEqual(wait_block().count(call), 1, "the wait-failure branch publishes the frames exactly once")
        self.assertEqual(TEMPLATE.count(call), 1)
        mutated = TEMPLATE.replace(call, "$waitFailureRawFrames = $null", 1)
        code, _summary, pub, out = self.run_wait_failure("mut4", um_run_log(), contact_frames=FRAMES, text=mutated)
        self.assertEqual(code, 23, out)
        self.assertFalse((pub / "contact-sheet" / "raw").exists(), "the UM state of 2026-10-02: no PNG on the leg")


@requires_windows_pwsh
class WaitFailureFramesAreGatedOnTheLegsOwnCountersTests(SliceHarness, unittest.TestCase):
    """DVE-WAIT-FAILURE-FRAMES-BACKEND-GATE-1 (fable PR #231 r1): the wait failure ran before the job's own backend gates, so it listed the captured frames of a cuda
    look leg that had fallen back to the cpu path -- frames New-VenueSheetPair labels `cuda`. Master keeps no frame for CPU_FALLBACK_DETECTED; neither does this
    branch now, unless the run's own counters say the frames are the leg's backend's."""

    def setUp(self) -> None:
        self.make_slice_dir()

    def assert_published(self, pub: Path, published: bool, label: str) -> None:
        marker = pub / "contact-sheet" / "compose-status.txt"
        raw = pub / "contact-sheet" / "raw"
        if published:
            self.assertTrue(marker.exists() and raw.is_dir(), f"{label}: the frames and the marker are published")
        else:
            self.assertFalse(marker.exists(), f"{label}: no marker, so the runner lists no frame")
            self.assertFalse(raw.exists(), f"{label}: no frame is published")

    def test_a_cuda_leg_publishes_only_when_the_counters_are_gpu_only(self) -> None:
        cases = {"cuda-healthy": (UM_GPU_SUMMARY_LINE, True), "cuda-all-cpu": (UM_CPU_GPU_SUMMARY_LINE, False), "cuda-partial": (UM_PARTIAL_GPU_SUMMARY_LINE, False)}
        for name, (line, published) in cases.items():
            code, summary, pub, out = self.run_wait_failure(name, um_run_log(line), contact_frames=FRAMES)
            self.assertEqual(code, 23, out)
            self.assertEqual(summary["result"], "PRESENTMON_UNAVAILABLE")
            self.assertIsNotNone(summary["gpuSummary"], name)
            self.assert_published(pub, published, name)

    def test_a_cpu_leg_publishes_only_when_the_counters_are_cpu_only(self) -> None:
        text = cpu_job_with_wait_branch(CpuGeneratedJobs.text("cpu"))
        cases = {"cpu-healthy": (UM_CPU_GPU_SUMMARY_LINE, True), "cpu-reached-gpu": (UM_GPU_SUMMARY_LINE, False), "cpu-partial": (UM_PARTIAL_GPU_SUMMARY_LINE, False)}
        for name, (line, published) in cases.items():
            code, _summary, pub, out = self.run_wait_failure(name, um_run_log(line), backend="cpu", contact_frames=FRAMES, text=text)
            self.assertEqual(code, 23, out)
            self.assert_published(pub, published, name)

    def test_counters_that_are_unavailable_do_not_contradict_the_leg(self) -> None:
        # no gpu_summary line: the receipt is a typed INVALID (BACKEND_NOT_DERIVABLE), never a labelled PASS/FAIL, so the frames are kept as evidence
        code, summary, pub, out = self.run_wait_failure("nocounters", um_run_log(None), contact_frames=FRAMES)
        self.assertEqual(code, 23, out)
        self.assertIsNone(summary["gpuSummary"])
        self.assert_published(pub, True, "unavailable counters")

    def test_the_gating_changes_nothing_else_in_the_summary(self) -> None:
        _c1, withheld, _p1, _o1 = self.run_wait_failure("same-withheld", um_run_log(UM_CPU_GPU_SUMMARY_LINE), contact_frames=FRAMES, rows=7)
        _c2, off, _p2, _o2 = self.run_wait_failure("same-off", um_run_log(UM_CPU_GPU_SUMMARY_LINE), contact_frames=FRAMES, enabled=False, rows=7)
        strip = lambda s: {k: v for k, v in s.items() if k not in ("presentMonCaptureStartUtc", "artifactRoot")}
        self.assertEqual(strip(withheld), strip(off))

    def test_mutation_without_the_gate_a_cpu_fallen_back_cuda_leg_lists_frames_again(self) -> None:
        gate = "        if (-not $waitFailureCountersContradictLeg) {\n"
        self.assertEqual(TEMPLATE.count(gate), 1, "the wait-failure branch gates its frames exactly once")
        mutated = TEMPLATE.replace(gate, "        if ($true) {\n", 1)
        for name, line in (("mut-all-cpu", UM_CPU_GPU_SUMMARY_LINE), ("mut-partial", UM_PARTIAL_GPU_SUMMARY_LINE)):
            code, _summary, pub, out = self.run_wait_failure(name, um_run_log(line), contact_frames=FRAMES, text=mutated)
            self.assertEqual(code, 23, out)
            self.assert_published(pub, True, name + " (the old behaviour: a cuda label over cpu frames)")

    def test_mutation_without_the_cpu_variant_edit_a_cpu_leg_is_gated_by_the_cuda_rule(self) -> None:
        text = cpu_job_with_wait_branch(CpuGeneratedJobs.text("cpu"))
        edited = "-not ($waitFailureGpuFramesTotal -le 0 -and [int64]$waitFailureGpuSummary.cpuFrames -gt 0)"
        original = "-not ($waitFailureGpuFramesTotal -gt 0 -and [int64]$waitFailureGpuSummary.cpuFrames -le 0)"
        self.assertEqual(text.count(edited), 1, "the cpu variant swaps in the inverse rule")
        self.assertEqual(text.count(original), 0)
        mutated = text.replace(edited, original, 1)
        code, _summary, pub, out = self.run_wait_failure("mut-cpu", um_run_log(UM_CPU_GPU_SUMMARY_LINE), backend="cpu", contact_frames=FRAMES, text=mutated)
        self.assertEqual(code, 23, out)
        self.assert_published(pub, False, "a healthy cpu leg would lose its frames without the variant edit")


@requires_windows_pwsh
class SmokeRunFailedKeepsItsEvidenceTests(SliceHarness, unittest.TestCase):
    """Item 3: attempt 2 on UM failed with an empty stderr tail and no run log, so the receipt could not say why."""

    def setUp(self) -> None:
        self.make_slice_dir()

    def test_the_full_stderr_stdout_result_and_run_log_are_published(self) -> None:
        log = "[2026-10-02T16:12:03.758Z] [INFO] playback_smoke.measured_session id=1\n" + UM_GPU_SUMMARY_LINE + "\n"
        code, summary, pub, out = self.run_smoke_failure("full", LONG_STDERR_CMD, result_json_with_log=log)
        self.assertEqual(code, 18, out)
        self.assertEqual(summary["result"], "SMOKE_RUN_FAILED")
        stderr = (pub / "smoke-stderr.txt").read_text(encoding="utf-8", errors="replace")
        self.assertEqual(len([l for l in stderr.splitlines() if l.startswith("stderr line ")]), 60, "the FULL stderr, not the 40-line / 4000-char tail the summary keeps")
        self.assertIn("stdout first line", (pub / "smoke-stdout.txt").read_text(encoding="utf-8", errors="replace"))
        self.assertTrue((pub / "result.json").exists(), "the launcher's result.json says why the run was invalid (validation failures)")
        self.assertEqual((pub / "logs" / "smoke-run.log").read_text(encoding="utf-8"), log)
        ev = summary["smokeEvidence"]
        self.assertEqual((ev["stderrPublished"], ev["stdoutPublished"], ev["resultJsonPublished"], ev["runLogPublished"]), (True, True, True, True))
        self.assertEqual(ev["stderrBytes"], (pub / "smoke-stderr.txt").stat().st_size)
        self.assertEqual(ev["runLogSource"], "result.json log.path (run log snapshot)")
        self.assertLessEqual(len(summary["smokeStderrTail"]), 4000, "the typed tail is unchanged")

    def test_an_empty_stderr_still_publishes_stdout_where_the_runner_wrote_its_reason(self) -> None:
        # UM attempt 2: smoke exit 1, an EMPTY stderr tail -- the reason was not on stderr
        code, summary, pub, out = self.run_smoke_failure("empty", "[Console]::Out.WriteLine('validation failed: reason is on stdout'); exit 1")
        self.assertEqual(code, 18, out)
        self.assertEqual(summary["smokeStderrTail"], "")
        self.assertIn("reason is on stdout", (pub / "smoke-stdout.txt").read_text(encoding="utf-8", errors="replace"))
        self.assertEqual(summary["smokeEvidence"]["stdoutPublished"], True)
        self.assertEqual(summary["smokeEvidence"]["runLogPublished"], False)
        self.assertEqual(summary["smokeEvidence"]["runLogSource"], "none")
        self.assertTrue(summary["smokeEvidence"]["runLogReason"], "a run log that could not be published says why")

    def test_when_no_result_exists_the_located_app_log_is_published_instead(self) -> None:
        located = self.stmp / "located-app.log"
        located.write_text("[2026-10-02T16:12:03.758Z] [INFO] gui_smoke.display_screen index=0\n", encoding="utf-8")
        code, summary, pub, out = self.run_smoke_failure("located", "exit 1", stub_found_log=located)
        self.assertEqual(code, 18, out)
        self.assertEqual((pub / "logs" / "smoke-failed-app.log").read_text(encoding="utf-8"), located.read_text(encoding="utf-8"))
        self.assertEqual(summary["smokeEvidence"]["runLogSource"], "failed-run app log (display recovery)")

    def test_each_failure_artifact_copy_is_its_own_attempt(self) -> None:
        # sol PR #231 r1 hardening: one outer try made the first copy that fails skip every later one
        log = "[2026-10-02T16:12:03.758Z] [INFO] playback_smoke.measured_session id=1\n" + UM_GPU_SUMMARY_LINE + "\n"
        stderr_copy = "[void](Publish-AttrCudaFileCopy -Source $smokeStderrPath -Destination (Join-Path $Pub 'smoke-stderr.txt'))"
        self.assertEqual(TEMPLATE.count(stderr_copy), 1)
        mutated = TEMPLATE.replace(stderr_copy, "[void](Write-Error 'injected sharing violation on stderr' -ErrorAction Stop)", 1)
        code, summary, pub, out = self.run_smoke_failure("indep", LONG_STDERR_CMD, result_json_with_log=log, text=mutated)
        self.assertEqual(code, 18, out)
        self.assertFalse((pub / "smoke-stderr.txt").exists(), "the injected failure took stderr")
        self.assertTrue((pub / "smoke-stdout.txt").exists(), "stdout is still attempted")
        self.assertTrue((pub / "result.json").exists(), "result.json is still attempted")
        self.assertEqual((pub / "logs" / "smoke-run.log").read_text(encoding="utf-8"), log, "and so is the run log")
        ev = summary["smokeEvidence"]
        self.assertEqual((ev["stderrPublished"], ev["stdoutPublished"], ev["resultJsonPublished"], ev["runLogPublished"]), (False, True, True, True))

    def test_mutation_with_one_outer_try_the_failure_costs_every_later_copy(self) -> None:
        # the r1 shape: the copies share ONE try, so a failure in the first skips the rest (re-created here from the committed text)
        block = smoke_block()
        start = block.index("    $smokeStdoutPath = Join-Path $legOut 'smoke-stdout.txt'\n")
        end = block.index("    # DVE-LEG-TERMINALS-1 <<<")
        one_try = ("    $smokeStdoutPath = Join-Path $legOut 'smoke-stdout.txt'\n    try {\n"
                   "        [void](Write-Error 'injected sharing violation on stderr' -ErrorAction Stop)\n"
                   "        if (Test-Path -LiteralPath $smokeStdoutPath -PathType Leaf) { [void](Publish-AttrCudaFileCopy -Source $smokeStdoutPath -Destination (Join-Path $Pub 'smoke-stdout.txt')); $smokeStdoutPublished = $true }\n"
                   "    } catch { $smokeRunLogReason = 'stopped' }\n")
        mutated = TEMPLATE.replace(block[start:end], one_try, 1)
        self.assertNotEqual(mutated, TEMPLATE)
        code, _summary, pub, out = self.run_smoke_failure("one-try", LONG_STDERR_CMD, text=mutated)
        self.assertEqual(code, 18, out)
        self.assertFalse((pub / "smoke-stdout.txt").exists(), "with a single try the failure on stderr costs stdout too -- the independent copies above are what prevent it")

    def test_mutations_each_lost_piece_of_evidence_is_lost(self) -> None:
        log = UM_GPU_SUMMARY_LINE + "\n"
        for label, old, probe in (
                ("stderr", "Publish-AttrCudaFileCopy -Source $smokeStderrPath -Destination (Join-Path $Pub 'smoke-stderr.txt')", lambda pub: not (pub / "smoke-stderr.txt").exists()),
                ("result", "Publish-AttrCudaFileCopy -Source $resultPath -Destination (Join-Path $Pub 'result.json')", lambda pub: not (pub / "result.json").exists()),
                ("runlog", "Publish-AttrCudaFileCopy -Source $failedRunLog.path -Destination (Join-Path $Pub 'logs\\smoke-run.log')", lambda pub: not (pub / "logs" / "smoke-run.log").exists())):
            block = smoke_block()
            self.assertEqual(block.count(old), 1, f"{label}: the SMOKE_RUN_FAILED branch publishes it exactly once")
            mutated = TEMPLATE.replace(old, "$null", 1)
            code, _summary, pub, out = self.run_smoke_failure(f"mut-{label}", LONG_STDERR_CMD, result_json_with_log=log, text=mutated)
            self.assertEqual(code, 18, out)
            self.assertTrue(probe(pub), f"{label}: with the publish removed the evidence is gone (the UM state of 2026-10-02)")


class CpuGeneratedJobs:
    """Generated job text for the cpu / UM variants (the generator run against a sparse clone, as the sibling suite does). One generation per variant, shared."""

    _tmp = None
    _texts: dict[str, str] = {}
    _cls = None

    @classmethod
    def _boot(cls):
        if cls._cls is None:
            class Boot(dve.GeneratorByteIdentityAndVariantTests):
                def runTest(self):  # pragma: no cover - never run
                    pass
            dve.GeneratorByteIdentityAndVariantTests.setUpClass.__func__(Boot)
            atexit.register(Boot._tmp.cleanup)
            cls._cls = Boot
        return cls._cls

    @classmethod
    def text(cls, kind: str, generator: Path | None = None) -> str:
        key = f"{kind}|{generator}"
        if key not in cls._texts:
            boot = cls._boot()
            extra = {"cpu": ["-Backend", "cpu"], "um-cuda": ["-Venue", "ultra-magnus"], "default": [],
                     "um-cpu-look": ["-Venue", "ultra-magnus", "-Backend", "cpu", "-ForceLookAssist", "-ContactSheet"]}[kind]
            inst = boot("runTest")
            out = inst.generate(generator or GENERATOR, f"{kind}-{abs(hash(key))}.job.ps1", extra)
            cls._texts[key] = lf(out.read_text(encoding="utf-8"))
        return cls._texts[key]


@requires_windows_pwsh
class CpuLegSkippedRatioIsMeasuredNotGatedTests(unittest.TestCase):
    """Item 2: the smoke runner's skipped/unpresented limit (default 0.5) failed a CPU leg (SMOKE_RUN_FAILED, exit 18: 'Skipped/unpresented-frame ratio 56.17%
    exceeds the playback-quality limit 50.00%') although docs/dual-venue-evidence.md says CPU frame rate is informational and never gates."""

    @classmethod
    def tearDownClass(cls) -> None:
        pass

    def test_a_cpu_job_lifts_the_limit_and_a_cuda_job_keeps_it(self) -> None:
        cpu = CpuGeneratedJobs.text("cpu")
        smoke_line = next(l for l in cpu.splitlines() if l.startswith("$cmd = \"& $(ConvertTo-PsSingleQuoted $smoke)"))
        self.assertIn(" -MaxSkippedOrUnpresentedRatio 1 ", smoke_line, "the cpu leg passes the runner's own limit parameter at its ceiling (the ratio cannot exceed 1)")
        for kind in ("default", "um-cuda"):
            text = CpuGeneratedJobs.text(kind)
            self.assertNotIn("MaxSkippedOrUnpresentedRatio", text, f"{kind}: a CUDA leg keeps the runner's 50% gate exactly as is")

    def test_the_other_playback_gates_are_not_loosened(self) -> None:
        cpu = CpuGeneratedJobs.text("cpu")
        default = CpuGeneratedJobs.text("default")
        smoke_line = next(l for l in cpu.splitlines() if l.startswith("$cmd = \"& $(ConvertTo-PsSingleQuoted $smoke)"))
        # the clip-length, loop, nonce and settings-isolation gates live in the runner and the receipt oracle; the job passes none of them as a switch a cpu leg could relax
        for forbidden in ("-AllowZeroPresentedFrames", "-SkipLengthGate", "-AllowLoop", "-NoLoop:$false"):
            self.assertNotIn(forbidden, cpu)
        self.assertEqual(sum(1 for token in re.findall(r"-[A-Za-z]+", smoke_line) if token == "-MaxSkippedOrUnpresentedRatio"), 1)
        # the whole cpu command differs from the default's only by the inserted argument and the cpu variant's own environment/arguments already pinned elsewhere
        default_line = next(l for l in default.splitlines() if l.startswith("$cmd = \"& $(ConvertTo-PsSingleQuoted $smoke)"))
        self.assertEqual(smoke_line.replace(" -MaxSkippedOrUnpresentedRatio 1", ""), default_line)

    def test_a_cpu_success_summary_records_the_ratio_as_a_measured_field_and_a_cuda_one_does_not(self) -> None:
        cpu = CpuGeneratedJobs.text("cpu")
        self.assertIn("    skippedOrUnpresentedRatio = ", cpu)
        self.assertIn("$resultJson.validation.skippedOrUnpresentedRatio", cpu)
        self.assertNotIn("skippedOrUnpresentedRatio", CpuGeneratedJobs.text("um-cuda"))
        self.assertNotIn("skippedOrUnpresentedRatio", CpuGeneratedJobs.text("default"))

    def test_the_ratio_expression_reads_the_launchers_own_field(self) -> None:
        # the launcher writes validation.skippedOrUnpresentedRatio into result.json (run-release-gui-smoke.ps1 ~2978): the job reads exactly that
        launcher = (ROOT / "tools" / "profiling" / "run-release-gui-smoke.ps1").read_text(encoding="utf-8")
        self.assertIn("skippedOrUnpresentedRatio = $skippedOrUnpresentedRatio", launcher)
        self.assertIn("[ValidateRange(0.0, 1.0)]\r\n    [double]$MaxSkippedOrUnpresentedRatio = 0.5" if "\r\n" in launcher else "[ValidateRange(0.0, 1.0)]\n    [double]$MaxSkippedOrUnpresentedRatio = 0.5", launcher)

    def test_mutation_without_the_cpu_patch_the_limit_stays_and_the_leg_fails_again(self) -> None:
        mutated = self.mutated_generator(("$template = Edit-DualVenueTemplate $template ' -Scope none -FrameTelemetry' ' -Scope none -MaxSkippedOrUnpresentedRatio 1 -FrameTelemetry'", "$null = 0"))
        text = CpuGeneratedJobs.text("cpu", generator=mutated)
        self.assertNotIn("MaxSkippedOrUnpresentedRatio", text, "with the cpu patch removed the 50% gate is back (the UM state of 2026-10-02)")

    def mutated_generator(self, mutation: tuple[str, str]) -> Path:
        tmp = tempfile.TemporaryDirectory(prefix="dve-genmut-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for sub in ("tools/profiling", "tools/gates"):
            shutil.copytree(ROOT / sub, root / sub)
        path = root / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-job.ps1"
        text = lf(path.read_text(encoding="utf-8"))
        self.assertEqual(text.count(mutation[0]), 1, f"mutation anchor must occur exactly once: {mutation[0]!r}")
        path.write_text(text.replace(mutation[0], mutation[1]), encoding="utf-8")
        return path


def smoke_artifacts(h: RunnerHarness, summary: dict, stderr: str | None = None, stdout: str | None = None, **kw) -> None:
    h.write_artifacts(source_frames=False, exact_summary=summary, **kw)
    (h.artifacts / "evidence-manifest.json").unlink(missing_ok=True)
    if stderr is not None:
        (h.artifacts / "smoke-stderr.txt").write_bytes(stderr.encode("utf-8"))
    if stdout is not None:
        (h.artifacts / "smoke-stdout.txt").write_bytes(stdout.encode("utf-8"))


@requires_windows_pwsh
class RunnerWritesATypedReceiptForEveryTerminalTests(RunnerHarness, ModuleMutationMixin, unittest.TestCase):
    """The runner side: a terminal whose summary names no backend is a typed no-signal receipt (never exit 2 with no receipt), SMOKE_RUN_FAILED keeps its
    evidence in the receipt, and the frames a PresentMon wait failure published are kept."""

    def setUp(self) -> None:
        self.make_harness()

    # -- item 1 --------------------------------------------------------------------------------------------------------------------------------
    def test_the_counterless_wait_failure_of_the_um_evidence_is_a_typed_no_signal_receipt_not_a_refused_write(self) -> None:
        smoke_artifacts(self, um_wait_failure_summary())
        proc, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), token=("PRESENTMON_UNAVAILABLE", 23))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIsNotNone(receipt, "a leg that ran always ends with a receipt")
        self.assertEqual(receipt["outcome"], "INVALID")
        self.assertIn("BACKEND_NOT_DERIVABLE", receipt["outcomeDetail"])
        self.assertIn("PRESENTMON_UNAVAILABLE", receipt["outcomeDetail"])
        self.assertEqual(receipt["subject"]["backend"], "cuda")
        self.assertTrue(receipt["evidence"]["summaryJsonSha256"], "the evidence is kept and named")
        self.assertTrue(receipt["evidence"]["logSha256"])

    def test_a_wait_failure_that_carries_the_counters_is_the_advisory_fail_the_docs_promise(self) -> None:
        summary = um_wait_failure_summary()
        summary.update({"gpuSummary": {"cpuFrames": 0, "gpuPreviewFrames": 0, "gpuReconReadbackFrames": 0, "gpuTextureReadbackFrames": 0, "gpuTextureNoReadbackFrames": 478},
                        "gpuFramesTotal": 478})
        smoke_artifacts(self, summary)
        proc, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), token=("PRESENTMON_UNAVAILABLE", 23))
        self.assertEqual(receipt["outcome"], "FAIL", receipt["outcomeDetail"])
        self.assertIn("PRESENTMON_UNAVAILABLE", receipt["outcomeDetail"])

    def test_the_spawn_failure_that_ran_nothing_is_a_typed_no_signal_receipt(self) -> None:
        # ~2159: PresentMon failed to START, before the smoke run: no log, no counters, nothing ran
        summary = {"schema": "playback-attr-3-cuda-venue.v1", "result": "PRESENTMON_UNAVAILABLE", "fixtureRehearsal": False, "displayWake": {},
                   "reason": "PresentMon failed to start: access denied", "presentMonStatus": "unavailable", "chains": [], "display": {"venue": "ultra-magnus"},
                   "sourceCommit": "38ed2d8f96c29df273d1de60f2dfd8a03ab6873a", "clipId": OWNER_CLIP, "artifactRoot": "X:\\stub\\artifacts"}
        smoke_artifacts(self, summary, log=False, result=False)
        proc, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), token=("PRESENTMON_UNAVAILABLE", 23))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(receipt["outcome"], "INVALID")

    def test_a_terminal_that_does_carry_counters_is_unaffected(self) -> None:
        smoke_artifacts(self, real_failure_summary("CPU_FALLBACK_DETECTED", gpuReconReadbackFrames=888, cpuFrames=12))
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), token=("CPU_FALLBACK_DETECTED", 14))
        self.assertEqual(receipt["outcome"], "FAIL", receipt["outcomeDetail"])

    def test_mutation_without_the_no_signal_rule_the_runner_keeps_a_fail_it_cannot_prove(self) -> None:
        mutated = self.mutated_runner([("Invoke-VenueLeg.ps1", "$backendNotDerivable = Get-DvBackendNotDerivable", "$backendNotDerivable = $null; $null = Get-DvBackendNotDerivable")])
        smoke_artifacts(self, um_wait_failure_summary())
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), token=("PRESENTMON_UNAVAILABLE", 23), dv=mutated)
        self.assertEqual(receipt["outcome"], "FAIL", "the old behaviour: a FAIL the production writer then refuses (exit 2, no receipt)")

    # -- item 3 --------------------------------------------------------------------------------------------------------------------------------
    def test_smoke_run_failed_keeps_stderr_and_stdout_and_the_receipt_says_where(self) -> None:
        stderr = "".join(f"stderr line {i:02d}\n" for i in range(60))
        smoke_artifacts(self, um_smoke_failed_summary(""), stderr=stderr, stdout="validation failed\n")
        proc, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), token=("SMOKE_RUN_FAILED", 18))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(receipt["outcome"], "INVALID", "SMOKE_RUN_FAILED is no signal in any backend, with or without a run log")
        self.assertIn("SMOKE_RUN_FAILED", receipt["outcomeDetail"])
        self.assertIn("exit 1", receipt["outcomeDetail"])
        ev = Path(receipt["evidence"]["localEvidenceDir"])
        self.assertEqual((ev / "smoke-stderr.txt").read_text(encoding="utf-8"), stderr)
        self.assertEqual((ev / "smoke-stdout.txt").read_text(encoding="utf-8"), "validation failed\n")
        self.assertEqual(receipt["evidence"]["smokeStderrSha256"], hashlib.sha256(stderr.encode()).hexdigest())
        self.assertEqual(receipt["evidence"]["smokeStdoutSha256"], hashlib.sha256(b"validation failed\n").hexdigest())
        self.assertTrue((ev / "logs" / "smoke-run.log").exists(), "the run log the job now publishes is kept too")

    def test_smoke_run_failed_with_a_sound_run_log_is_still_invalid_not_a_product_fail(self) -> None:
        # with the run log published the playback proof is no longer absent; the outcome must still not become a FAIL it cannot back with counters
        smoke_artifacts(self, um_smoke_failed_summary("Write-Error: Skipped/unpresented-frame ratio 56.17% exceeds the playback-quality limit 50.00%."))
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), token=("SMOKE_RUN_FAILED", 18))
        self.assertTrue(receipt["playback"]["valid"], "the published log proves what ran")
        self.assertEqual(receipt["outcome"], "INVALID", receipt["outcomeDetail"])

    def test_mutation_without_the_smoke_run_failed_rule_a_run_with_a_log_reads_as_a_fail(self) -> None:
        mutated = self.mutated_module([("if ($ResultToken -eq 'SMOKE_RUN_FAILED') {", "if ($false) {")])
        # the module copy is exercised through its own resolver (the runner imports the sibling module by path)
        proc = run_pwsh(["-Command", f"Import-Module '{mutated}' -Force; (Resolve-DvJobOutcome -ResultToken 'SMOKE_RUN_FAILED' -ExitCode 18).outcome"])
        self.assertEqual(proc.stdout.strip(), "FAIL")
        proc = run_pwsh(["-Command", f"Import-Module '{DV_MODULE}' -Force; (Resolve-DvJobOutcome -ResultToken 'SMOKE_RUN_FAILED' -ExitCode 18).outcome"])
        self.assertEqual(proc.stdout.strip(), "INVALID")

    def test_mutation_without_the_evidence_copy_the_receipt_loses_the_stderr(self) -> None:
        mutated = self.mutated_runner([("Invoke-VenueLeg.ps1", "foreach ($smokeFile in 'smoke-stderr.txt', 'smoke-stdout.txt') {", "foreach ($smokeFile in @()) {")])
        smoke_artifacts(self, um_smoke_failed_summary(""), stderr="why\n", stdout="")
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), token=("SMOKE_RUN_FAILED", 18), dv=mutated)
        self.assertIsNone(receipt["evidence"]["smokeStderrSha256"])

    # -- item 4 --------------------------------------------------------------------------------------------------------------------------------
    def test_the_frames_a_wait_failure_published_are_kept_with_a_hashed_manifest_and_the_receipt_stays_a_fail(self) -> None:
        summary = um_wait_failure_summary()
        summary.update({"gpuSummary": {"cpuFrames": 0, "gpuPreviewFrames": 0, "gpuReconReadbackFrames": 0, "gpuTextureReadbackFrames": 0, "gpuTextureNoReadbackFrames": 478},
                        "gpuFramesTotal": 478})
        smoke_artifacts(self, summary, compose_marker="CONTACT_SHEET_COMPOSE_UNAVAILABLE the PresentMon wait failed after the measured playback", raw_frames=True)
        proc, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(leg_type="look"), token=("PRESENTMON_UNAVAILABLE", 23))
        self.assertEqual(receipt["outcome"], "FAIL", receipt["outcomeDetail"])
        self.assertTrue(receipt["evidence"]["contactFramesJsonSha256"], "the frames are listed by sha256 for the pair composer")
        self.assertTrue(Path(receipt["look"]["rawFramesDir"]).is_dir())
        self.assertIsNone(receipt["look"]["contactSheet"])
        self.assertTrue(receipt["look"]["composeStatus"].startswith("CONTACT_SHEET_COMPOSE_UNAVAILABLE"))

    def test_a_failure_terminal_that_published_frames_without_the_marker_still_keeps_none(self) -> None:
        # GPU_RECON_FRAMES_ZERO and CPU_FALLBACK_DETECTED publish raw frames too, but their frames were drawn by the WRONG path for the leg's label: no marker, so the
        # runner lists none and the pair composer can never put them under a cuda label (fable PR #225 r1 item 2)
        smoke_artifacts(self, real_failure_summary("GPU_RECON_FRAMES_ZERO", cpuFrames=900), raw_frames=True)
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(leg_type="look"), token=("GPU_RECON_FRAMES_ZERO", 13))
        self.assertIsNone(receipt["evidence"]["contactFramesJsonSha256"])


def um_wait_failure_with_counters(gpu: dict | None = None, backend: str | None = None) -> dict:
    """The wait-failure summary AFTER this card: the UM shape plus the two counter fields the job now writes (and `backend` on a variant job's cpu leg)."""
    counters = {"cpuFrames": 0, "gpuPreviewFrames": 0, "gpuReconReadbackFrames": 0, "gpuTextureReadbackFrames": 0, "gpuTextureNoReadbackFrames": 478}
    counters.update(gpu or {})
    summary = um_wait_failure_summary()
    summary["gpuSummary"] = counters
    summary["gpuFramesTotal"] = counters["gpuReconReadbackFrames"] + counters["gpuTextureReadbackFrames"] + counters["gpuTextureNoReadbackFrames"]
    if backend:
        summary["backend"] = backend
    return summary


@requires_windows_pwsh
class WaitFailureSummaryIsAnAdvisoryFailInProductionTests(dve.EvidenceFactory, ModuleMutationMixin, unittest.TestCase):
    """The production writer and validator against the wait-failure shape (DVE-PRESENTMON-WAIT-FAILURE-UNRECEIPTABLE-1): with the counters it is the advisory
    FAIL the docs promise; without them the validator still fails closed (the runner now ends such a leg as a typed no-signal receipt before it gets here)."""

    def setUp(self) -> None:
        self.make_harness()

    def failure(self, name: str, summary: dict, backend: str = "cuda") -> Path:
        ev = self.evidence(name, backend=backend, source_frames=False, exact_summary=summary, token="PRESENTMON_UNAVAILABLE", exit_code=23)
        (ev / "evidence-manifest.json").unlink()
        return ev

    def test_the_wait_failure_shape_with_counters_is_an_advisory_fail_for_cuda_and_cpu_and_without_them_is_not_derivable(self) -> None:
        repo = self.prod_repo()
        cases = [
            (self.receipt_for(repo, self.failure("wf-cuda", um_wait_failure_with_counters()), outcome="FAIL"), None),
            (self.receipt_for(repo, self.failure("wf-cpu", um_wait_failure_with_counters({"cpuFrames": 520, "gpuTextureNoReadbackFrames": 0}, backend="cpu"), backend="cpu"),
                              backend="cpu", outcome="FAIL"), None),
            (self.receipt_for(repo, self.failure("wf-none", um_wait_failure_summary()), outcome="FAIL"), None),
        ]
        got = self.status_batch(repo, cases)
        self.expect(got[0], "ADVISORY", "cuda wait failure with the gpu counters")
        self.expect(got[1], "ADVISORY", "cpu wait failure with its counters and backend field")
        self.expect(got[2], "INVALID", "the UM shape of 2026-10-02, no counters", "BACKEND_NOT_DERIVABLE")

    def test_the_production_writer_writes_the_wait_failure_receipt(self) -> None:
        repo = self.prod_repo()
        receipt = self.receipt_for(repo, self.failure("wf-write", um_wait_failure_with_counters()), outcome="FAIL")
        out = self.tmp / "written"
        script = (f"Import-Module '{DV_MODULE}' -Force\n$r = $env:DVE_RECEIPT | ConvertFrom-Json -AsHashtable\n"
                  f"try {{ Write-DvReceipt -Receipt $r -ReceiptRoot '{out}' -RepoRoot '{repo.root}' | Out-Null; 'WRITTEN' }} catch {{ 'REFUSED:' + $_.Exception.Message }}\n")
        proc = run_pwsh(["-Command", script], env_extra={"DVE_RECEIPT": json.dumps(receipt)})
        self.assertEqual(proc.stdout.strip(), "WRITTEN", proc.stdout + proc.stderr)
        written = json.loads(next(out.rglob(receipt["receiptId"] + ".json")).read_text(encoding="utf-8"))
        self.assertEqual((written["outcome"], written["verification"]["status"]), ("FAIL", "ADVISORY"))


@requires_windows_pwsh
class ProductionLegNeverEndsWithoutAReceiptTests(dve.EvidenceFactory, ModuleMutationMixin, unittest.TestCase):
    """DVE-LEG-TERMINALS-1 r2 (sol and fable PR #231 r1, the same blocker): a production cuda leg whose app fell back wholly to the cpu path (all gpu counters 0,
    cpuFrames > 0) and then lost its PresentMon wait derives `cpu`, the production validator refuses the cuda-labelled FAIL (BACKEND_MISMATCH), and the writer used
    to throw: Complete-Receipt printed DVE_RECEIPT_WRITE_FAILED and exited 2 -- no receipt, the end state of the Ultra-Magnus runs with a different reason token.

    These tests run the REAL Complete-Receipt (sliced out of Invoke-VenueLeg.ps1) against a PRODUCTION-admission receipt (committed consent, committed leg spec, hashed
    evidence files, the real Write-DvReceipt and Test-DvReceiptValid) -- the runner's own offline mode skips the validator's backend block, so it cannot show this."""

    def setUp(self) -> None:
        self.make_harness()
        self.repo = self.prod_repo()

    def complete(self, receipt: dict, outcome: str, dv: Path = DV, name: str = "w", precreate: bool = False) -> tuple[int, dict | None, str]:
        script_text = (dv / "Invoke-VenueLeg.ps1").read_text(encoding="utf-8").replace("\r\n", "\n")
        fn = script_text[script_text.index("function Complete-Receipt("):script_text.index("function Submit-VenueJob(")]
        out_root = self.tmp / name
        if precreate:   # the receipt id is already taken: the writer's CreateNew throws something that is NOT a validator refusal
            taken = out_root / receipt["card"] / receipt["legId"] / receipt["venue"]["name"]
            taken.mkdir(parents=True)
            (taken / (receipt["receiptId"] + ".json")).write_text("{}", encoding="utf-8")
        script = (f"$ErrorActionPreference = 'Stop'\nImport-Module '{dv / 'DualVenueRunner.psm1'}' -Force\n"
                  "$receipt = $env:DVE_RECEIPT | ConvertFrom-Json -AsHashtable\n"
                  "$BuildManifestSha256 = $receipt.subject.buildManifestSha256; $legSpecSha256 = $receipt.subject.legSpecSha256\n"
                  "$spec = [pscustomobject]@{ clipId = $receipt.subject.clipId }; $Backend = $receipt.subject.backend; $lookFlavor = $receipt.subject.lookFlavor\n"
                  f"$ReceiptRoot = '{out_root}'; $RepoRoot = '{self.repo.root}'; $OfflineTestMode = [switch]$false\n"
                  + fn + f"\nComplete-Receipt '{outcome}' 'the job result detail'\n")
        probe = self.tmp / f"{name}.ps1"
        probe.write_text(script, encoding="utf-8")
        proc = run_pwsh(["-File", str(probe)], env_extra={"DVE_RECEIPT": json.dumps(receipt)})
        written = sorted(out_root.rglob(receipt["receiptId"] + ".json")) if out_root.exists() else []
        body = json.loads(written[-1].read_text(encoding="utf-8")) if written and written[-1].stat().st_size > 2 else None
        return proc.returncode, body, proc.stdout + proc.stderr

    def failure(self, name: str, summary: dict, backend: str) -> Path:
        ev = self.evidence(name, backend=backend, source_frames=False, exact_summary=summary, token="PRESENTMON_UNAVAILABLE", exit_code=23)
        (ev / "evidence-manifest.json").unlink()
        return ev

    def all_cpu_cuda_receipt(self, name: str = "cuda-all-cpu") -> dict:
        # the wait-failure summary of a cuda leg whose app ran entirely on the cpu path (UM_CPU_GPU_SUMMARY_LINE's counters): no cuda frame, 520 cpu frames
        summary = um_wait_failure_with_counters({"cpuFrames": 520, "gpuTextureNoReadbackFrames": 0})
        return self.receipt_for(self.repo, self.failure(name, summary, "cuda"), backend="cuda", outcome="FAIL")

    def test_an_all_cpu_cuda_leg_that_lost_its_wait_is_a_written_invalid_that_names_the_reason_and_keeps_the_evidence(self) -> None:
        receipt = self.all_cpu_cuda_receipt()
        self.expect(self.status_batch(self.repo, [(receipt, None)])[0], "INVALID", "the validator refuses the cuda-labelled FAIL", "BACKEND_MISMATCH")
        code, written, out = self.complete(receipt, "FAIL")
        self.assertEqual(code, 0, out)
        self.assertIsNotNone(written, "a leg that ran always ends with a receipt")
        self.assertEqual(written["outcome"], "INVALID")
        self.assertIn("DVE_OUTCOME=INVALID", out)
        self.assertNotIn("DVE_RECEIPT_WRITE_FAILED", out)
        self.assertIn("BACKEND_MISMATCH", written["outcomeDetail"], "the validator's own reasons are named")
        self.assertIn("the job result was FAIL", written["outcomeDetail"])
        self.assertIn("the job result detail", written["outcomeDetail"])
        self.assertEqual(written["subject"]["backend"], "cuda", "the label is the leg's, never rewritten")
        for key in ("summaryJsonSha256", "logSha256", "resultJsonSha256", "umRunJsonSha256", "localEvidenceDir"):
            self.assertEqual(written["evidence"][key], receipt["evidence"][key], f"the evidence ({key}) is kept")
        self.assertIsNone(written.get("verification"), "an INVALID is no signal: no verification stamp")
        self.assertEqual(written["metrics"], receipt["metrics"])

    def test_the_symmetric_cpu_leg_whose_run_reached_a_gpu_path_is_a_written_invalid(self) -> None:
        summary = um_wait_failure_with_counters(backend="cpu")   # 478 gpu frames on a leg labelled cpu
        receipt = self.receipt_for(self.repo, self.failure("cpu-gpu", summary, "cpu"), backend="cpu", outcome="FAIL")
        self.expect(self.status_batch(self.repo, [(receipt, None)])[0], "INVALID", "the validator refuses the cpu-labelled FAIL", "BACKEND_MISMATCH")
        code, written, out = self.complete(receipt, "FAIL", name="w-cpu")
        self.assertEqual(code, 0, out)
        self.assertEqual(written["outcome"], "INVALID")
        self.assertIn("BACKEND_MISMATCH", written["outcomeDetail"])
        self.assertEqual(written["subject"]["backend"], "cpu")

    def test_any_other_validator_refusal_is_demoted_the_same_way(self) -> None:
        receipt = self.all_cpu_cuda_receipt("tamper")
        receipt["metrics"]["frameRows"] = 99999   # not the verbatim metrics of the hashed summary
        self.expect(self.status_batch(self.repo, [(receipt, None)])[0], "INVALID", "tampered metrics", "METRICS_NOT_FROM_EVIDENCE")
        code, written, out = self.complete(receipt, "FAIL", name="w-tamper")
        self.assertEqual(code, 0, out)
        self.assertEqual(written["outcome"], "INVALID")
        self.assertIn("METRICS_NOT_FROM_EVIDENCE", written["outcomeDetail"])

    def test_a_label_consistent_leg_is_unchanged_and_still_an_advisory_fail(self) -> None:
        receipt = self.receipt_for(self.repo, self.failure("cuda-ok", um_wait_failure_with_counters(), "cuda"), backend="cuda", outcome="FAIL")
        code, written, out = self.complete(receipt, "FAIL", name="w-ok")
        self.assertEqual(code, 0, out)
        self.assertEqual((written["outcome"], written["verification"]["status"]), ("FAIL", "ADVISORY"))
        self.assertIn("DVE_OUTCOME=FAIL", out)

    def test_a_write_that_fails_for_another_reason_still_ends_as_a_write_failure(self) -> None:
        receipt = self.receipt_for(self.repo, self.failure("cuda-dup", um_wait_failure_with_counters(), "cuda"), backend="cuda", outcome="FAIL")
        code, _written, out = self.complete(receipt, "FAIL", name="w-dup", precreate=True)
        self.assertEqual(code, 2, out)
        self.assertIn("DVE_RECEIPT_WRITE_FAILED", out, "a repeat receipt id is not a validator refusal: it is never papered over as an INVALID")

    def test_only_a_pass_or_fail_is_demoted(self) -> None:
        # an outcome that is not PASS/FAIL is never put to the validator, so it is written as it is and never relabelled
        receipt = self.all_cpu_cuda_receipt("nosig")
        code, written, out = self.complete(receipt, "VENUE_TOOLING", name="w-nosig")
        self.assertEqual(code, 0, out)
        self.assertEqual(written["outcome"], "VENUE_TOOLING")
        self.assertNotIn("the receipt writer refuses", written["outcomeDetail"])

    def test_mutation_without_the_demotion_the_leg_ends_with_no_receipt_again(self) -> None:
        guard = "if ($Outcome -cin @('PASS', 'FAIL') -and $refusal.StartsWith($invalidPrefix, [StringComparison]::Ordinal)) {"
        mutated = self.mutated_runner([("Invoke-VenueLeg.ps1", guard, "if ($false) {")])
        receipt = self.all_cpu_cuda_receipt("mut")
        code, written, out = self.complete(receipt, "FAIL", dv=mutated, name="w-mut")
        self.assertEqual(code, 2, out)
        self.assertIn("DVE_RECEIPT_WRITE_FAILED", out)
        self.assertIn("BACKEND_MISMATCH", out)
        self.assertIsNone(written, "the old behaviour: a leg that had played, no receipt at all (the Ultra-Magnus end state)")


@requires_windows_pwsh
class BackendNotDerivableRuleTests(ModuleMutationMixin, unittest.TestCase):
    """Get-DvBackendNotDerivable: the one rule the validator and the runner share."""

    def ask(self, cases: dict[str, tuple[dict, str, bool]], module: Path = DV_MODULE) -> dict[str, str]:
        payload = {k: {"summary": s, "backend": b, "cpuField": f} for k, (s, b, f) in cases.items()}
        script = (f"Import-Module '{module}' -Force\n$cases = $env:DVE_CASES | ConvertFrom-Json\nforeach ($p in $cases.PSObject.Properties) {{\n"
                  "  $m = Get-DvBackendNotDerivable -Summary $p.Value.summary -Backend $p.Value.backend -RequireCpuBackendField ([bool]$p.Value.cpuField)\n"
                  "  Write-Output ($p.Name + '=' + $(if ($null -eq $m) { '<derivable>' } else { ($m -split ':')[0] }))\n}\n")
        proc = run_pwsh(["-Command", script], env_extra={"DVE_CASES": json.dumps(payload)})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return dict(l.rsplit("=", 1) for l in proc.stdout.splitlines() if "=" in l)

    def test_the_rule(self) -> None:
        got = self.ask({
            "um shape, no counters": (um_wait_failure_summary(), "cuda", False),
            "counters": (um_wait_failure_with_counters(), "cuda", False),
            "cpu counters but no backend field, production": (um_wait_failure_with_counters({"cpuFrames": 520, "gpuTextureNoReadbackFrames": 0}), "cpu", True),
            "the same, offline": (um_wait_failure_with_counters({"cpuFrames": 520, "gpuTextureNoReadbackFrames": 0}), "cpu", False),
            "cpu with its backend field": (um_wait_failure_with_counters({"cpuFrames": 520, "gpuTextureNoReadbackFrames": 0}, backend="cpu"), "cpu", True),
            "null counters": ({**um_wait_failure_summary(), "gpuSummary": None, "gpuFramesTotal": None}, "cuda", False)})
        self.assertEqual(got, {"um shape, no counters": "BACKEND_NOT_DERIVABLE", "counters": "<derivable>",
                               "cpu counters but no backend field, production": "BACKEND_NOT_DERIVABLE", "the same, offline": "<derivable>",
                               "cpu with its backend field": "<derivable>", "null counters": "BACKEND_NOT_DERIVABLE"})

    def test_the_validator_uses_the_shared_messages(self) -> None:
        text = (DV_MODULE).read_text(encoding="utf-8")
        self.assertEqual(text.count("$invalid.Add($script:BackendNotDerivableMessage)"), 1)
        self.assertEqual(text.count("$invalid.Add($script:CpuBackendFieldMissingMessage)"), 1)

    def test_mutation_without_the_counter_check_every_summary_is_derivable(self) -> None:
        mutated = self.mutated_module([("if ($null -eq (Get-DvDerivedBackend -Summary $Summary)) { return $script:BackendNotDerivableMessage }", "")])
        got = self.ask({"um shape, no counters": (um_wait_failure_summary(), "cuda", False)}, module=mutated)
        self.assertEqual(got, {"um shape, no counters": "<derivable>"})


class DocsDoNotOverclaimTests(unittest.TestCase):
    def test_the_wait_failure_over_claim_is_gone_and_the_three_shapes_are_named(self) -> None:
        doc = (ROOT / "docs" / "dual-venue-evidence.md").read_text(encoding="utf-8")
        self.assertNotIn("`PRESENTMON_UNAVAILABLE` has a top-level `gpuFramesTotal` and is read the same", doc)
        for needle in ("three** summary shapes", "wait failure", "spawn failure", "Get-DvBackendNotDerivable", "skippedOrUnpresentedRatio", "MaxSkippedOrUnpresentedRatio",
                       "smokeStderrSha256", "smoke-stderr.txt", "contact-sheet\\raw"):
            self.assertIn(needle, doc, needle)
        row = next(r for r in doc.split("## Outcome mapping", 1)[1].splitlines() if "`SMOKE_RUN_FAILED` (the smoke runner exited non-zero" in r)
        self.assertIn("`INVALID`", row)

    def test_the_parse_failure_no_longer_claims_to_publish_no_frames_and_the_new_evidence_is_documented(self) -> None:
        doc = (ROOT / "docs" / "dual-venue-evidence.md").read_text(encoding="utf-8")
        self.assertNotIn("does not publish frames yet", doc)
        for needle in ("DVE-PRESENTMON-EVIDENCE-1", "presentmon-stdout.txt", "presentmon-stderr.txt", "csvEverExisted", "csvSizeAtStop", "same counter gate",
                       "does **not** take the wait-failure terminal"):
            self.assertIn(needle, doc, needle)


if __name__ == "__main__":
    unittest.main()
