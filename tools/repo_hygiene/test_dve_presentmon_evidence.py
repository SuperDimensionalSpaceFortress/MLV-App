"""Tests for DVE-PRESENTMON-EVIDENCE-1 (round 1): a PresentMon failure never hides why, and never discards contact-sheet frames the app already wrote.

CLASS: a PresentMon failure never hides why, and never discards contact-sheet frames the app already wrote under counters that agree with the leg.
Source: the Ultra-Magnus owner CUDA leg of VENUE-OWNER-LEGS-UM-2 r1 (clip M16-1243, by id only): PRESENTMON_UNAVAILABLE "PresentMon output does not exist", a stop
that was clean and immediate (session_terminate, no kill) yet NO CSV written, six contact-sheet frames written by the app on the venue and published by nothing
(the display-report parse-failure branch never did), PresentMon's own output never captured (it ran hidden with no streams), and fable's PR #231 r1 hardening
DVE-CPU-PRESENTMON-WAIT-GATES-1. The summary shapes below are copied from that evidence (keys and shapes only: no footage path, name or frame is read or written here).

  1. PresentMon's stdout and stderr are redirected to files beside its CSV (bounded at publish) and published with the artifacts; presentmon-capture.json records
     csvEverExisted and the CSV's size at stop, so "the stop was clean but the CSV never appeared" is a fact in the evidence, not a guess;
  2. the PRESENTMON_UNAVAILABLE display-report (parse / output-missing) branch publishes the frames the app already captured, and the compose marker, under the SAME
     counter gate the wait-failure branch uses (cuda: gpu > 0 and cpu == 0; the cpu variant the inverse) -- never under counters that contradict the leg;
  3. a CPU leg's PresentMon is informational: a PresentMon WAIT failure no longer ends the leg as FAIL PRESENTMON_UNAVAILABLE (the cpu variant records
     presentMonStatus unavailable, with the reason, and the leg's outcome comes from its own gates). A CUDA leg is unchanged.

The job's branches are EXECUTED (the real text, sliced out of the generator's template, run in pwsh against stubs; the module's own functions are called directly).
Every rule has a mutation test.
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from tools.repo_hygiene import test_dual_venue_evidence as dve
from tools.repo_hygiene.test_dual_venue_evidence import GENERATOR, OWNER_CLIP, lf, requires_windows_pwsh, run_pwsh
from tools.repo_hygiene.test_dve_leg_terminals import (
    FRAMES,
    MODULE,
    PM_TIMEOUT_REASON,
    TEMPLATE,
    UM_CPU_GPU_SUMMARY_LINE,
    UM_GPU_SUMMARY_LINE,
    UM_PARTIAL_GPU_SUMMARY_LINE,
    CpuGeneratedJobs,
    SliceHarness,
    _slice,
    gpu_summary_fn,
    presentmon_functions,
    um_run_log,
    wait_block,
)

PARSE_START = "if ($displayReport.status -ne 'OK' -and -not $displayAsleepOverridden) {"
PARSE_END = "\n$pmRows = @($displayReport.selectedChainRows)"
OVERRIDE_START = "$displayAsleepForegroundVerification = Get-AttrCudaForegroundVerification -LogText $rawLog"
STATUS_OVERRIDE_START = "if ($displayAsleepOverridden) {\n    $presentMonStatus = 'unavailable'"
OVERRIDE_END = "\n$dllSha256Lower = "
CAPTURE_JSON_START = "Save-Json ([ordered]@{\n    schema='playback-attr-3-cuda-presentmon-capture.v2'"
CAPTURE_JSON_END = "\n# CUDA-PERF-DISPLAY-IDENTITY-HARNESS-3 (sol+fable HARDENING, direction-corrected anchor"
SPAWN_START = "if ($null -ne $presentMonSpawnError) {"
SPAWN_END = "\n$presentMonPostSpawnUtc = (Get-Date).ToUniversalTime()"

# The UM-2 evidence, shape only (VENUE-OWNER-LEGS-UM-2 r1: summary.json of job ...-M16-1243-...).
UM2_REASON = "PresentMon output does not exist: X:\\stub\\out\\diagnostic\\presentmon.csv"
UM2_GPU_SUMMARY = ("[2026-10-03T01:57:21.100Z] [INFO] [0xec40] playback_smoke.gpu_summary session=1 cpu_frames=0 gpu_preview_frames=0 "
                   "gpu_recon_readback_frames=0 gpu_texture_readback_frames=0 gpu_texture_no_readback_frames=474")
SIX_FRAMES = {}
for _i in range(6):
    SIX_FRAMES[f"frame-{_i:02d}.png"] = b"\x89PNG\r\n\x1a\nraw" + bytes([_i])
    SIX_FRAMES[f"frame-{_i:02d}.json"] = json.dumps({"index": _i, "saved": True, "path": f"frame-{_i:02d}.png"}).encode("utf-8")

STUB_EXE = "PresentMon-stub.cmd"


def ps(path: Path) -> str:
    return str(path).replace("'", "''")


def region_text(text: str, sentinel: str) -> str:
    """The lines a sentinel pair brackets (exclusive), joined: the only text the card adds to the default job."""
    out: list[str] = []
    inside = False
    for line in lf(text).split("\n"):
        if f"{sentinel} >>>" in line:
            inside = True
            continue
        if f"{sentinel} <<<" in line:
            inside = False
            continue
        if inside:
            out.append(line)
    return "\n".join(out)


@requires_windows_pwsh
class ModuleEvidenceFunctionTests(unittest.TestCase):
    """Item 1, the module's own functions: the bounded stream publish and the CSV existence record are called directly."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="dve-pm-mod-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        (self.tmp / "pub").mkdir()

    def call(self, body: str) -> dict:
        proc = run_pwsh(["-Command", f"$ErrorActionPreference = 'Stop'\nImport-Module '{ps(MODULE)}' -Force\n{body}"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        line = next(l for l in proc.stdout.splitlines() if l.startswith("JSON="))
        return json.loads(line[len("JSON="):])

    def test_a_small_stream_is_published_whole_and_reported(self) -> None:
        src = self.tmp / "so.txt"
        src.write_bytes(b"line one\nline two\n")
        got = self.call(f"$r = Publish-AttrCudaBoundedTextCopy -Source '{ps(src)}' -Destination '{ps(self.tmp / 'pub' / 'so.txt')}'\nWrite-Output ('JSON=' + ($r | ConvertTo-Json -Compress))")
        self.assertEqual((got["exists"], got["published"], got["truncated"], got["bytes"], got["error"]), (True, True, False, 18, None))
        self.assertIn("line one\nline two", (self.tmp / "pub" / "so.txt").read_text(encoding="utf-8").replace("\r\n", "\n"))

    def test_a_stream_over_the_bound_publishes_only_its_tail_and_says_so(self) -> None:
        src = self.tmp / "big.txt"
        src.write_bytes((b"HEAD-" + b"x" * 5000 + b"-TAIL-LINE"))
        got = self.call(f"$r = Publish-AttrCudaBoundedTextCopy -Source '{ps(src)}' -Destination '{ps(self.tmp / 'pub' / 'big.txt')}' -MaxBytes 100\nWrite-Output ('JSON=' + ($r | ConvertTo-Json -Compress))")
        self.assertTrue(got["truncated"])
        self.assertEqual(got["bytes"], 5015)
        published = (self.tmp / "pub" / "big.txt").read_text(encoding="utf-8")
        self.assertIn("truncated", published.splitlines()[0])
        self.assertTrue(published.rstrip().endswith("-TAIL-LINE"))
        self.assertNotIn("HEAD-", published)
        self.assertLess(len(published), 300, "the published stream is bounded")

    def test_an_empty_stream_is_published_as_an_empty_fact_not_omitted(self) -> None:
        src = self.tmp / "empty.txt"
        src.write_bytes(b"")
        got = self.call(f"$r = Publish-AttrCudaBoundedTextCopy -Source '{ps(src)}' -Destination '{ps(self.tmp / 'pub' / 'empty.txt')}'\nWrite-Output ('JSON=' + ($r | ConvertTo-Json -Compress))")
        self.assertEqual((got["exists"], got["published"], got["bytes"]), (True, True, 0))
        self.assertTrue((self.tmp / "pub" / "empty.txt").exists())

    def test_a_missing_stream_is_reported_missing_and_never_throws(self) -> None:
        got = self.call(f"$r = Publish-AttrCudaBoundedTextCopy -Source '{ps(self.tmp / 'nope.txt')}' -Destination '{ps(self.tmp / 'pub' / 'nope.txt')}'\nWrite-Output ('JSON=' + ($r | ConvertTo-Json -Compress))")
        self.assertEqual((got["exists"], got["published"], got["error"]), (False, False, None))
        self.assertFalse((self.tmp / "pub" / "nope.txt").exists())

    def evidence(self, csv: bytes | None, seen: bool, so: bytes | None = b"out\n", se: bytes | None = b"err\n") -> dict:
        csv_path, so_path, se_path = self.tmp / "presentmon.csv", self.tmp / "so.txt", self.tmp / "se.txt"
        for p, data in ((csv_path, csv), (so_path, so), (se_path, se)):
            if data is not None:
                p.write_bytes(data)
        return self.call(
            f"$r = Publish-AttrCudaPresentMonCaptureEvidence -CsvPath '{ps(csv_path)}' -CsvSeenDuringReadiness ${'true' if seen else 'false'} "
            f"-StdoutPath '{ps(so_path)}' -StderrPath '{ps(se_path)}' -PubRoot '{ps(self.tmp / 'pub')}'\nWrite-Output ('JSON=' + ($r | ConvertTo-Json -Depth 6 -Compress))")

    def test_a_csv_that_exists_at_stop_is_recorded_with_its_size(self) -> None:
        got = self.evidence(b"a,b\n1,2\n", seen=False)
        self.assertEqual((got["csvEverExisted"], got["csvSizeAtStop"]), (True, 8))
        self.assertEqual((self.tmp / "pub" / "presentmon-stdout.txt").read_text(encoding="utf-8").strip(), "out")
        self.assertEqual((self.tmp / "pub" / "presentmon-stderr.txt").read_text(encoding="utf-8").strip(), "err")

    def test_the_um2_shape_a_clean_stop_and_no_csv_ever_is_a_recorded_fact(self) -> None:
        got = self.evidence(None, seen=False, so=b"", se=b"")
        self.assertEqual((got["csvEverExisted"], got["csvSizeAtStop"]), (False, None))
        self.assertTrue(got["streams"]["stdout"]["published"] and got["streams"]["stderr"]["published"])
        self.assertEqual(got["streams"]["stdout"]["bytes"], 0)

    def test_a_csv_seen_during_readiness_and_gone_at_stop_is_ever_existed_with_no_size(self) -> None:
        got = self.evidence(None, seen=True)
        self.assertEqual((got["csvEverExisted"], got["csvSizeAtStop"]), (True, None))

    def test_streams_that_were_never_created_are_reported_missing(self) -> None:
        got = self.evidence(None, seen=False, so=None, se=None)
        self.assertFalse(got["streams"]["stdout"]["exists"])
        self.assertFalse(got["streams"]["stderr"]["published"])


@requires_windows_pwsh
class PresentMonStreamsAreCapturedAtLaunchTests(SliceHarness, unittest.TestCase):
    """Item 1: the REAL Start-PresentMonCapture is run against a stub exe (a .cmd that prints a line to each stream), with the job's own redirect variables."""

    def setUp(self) -> None:
        self.make_slice_dir()

    def run_start(self, name: str, stub_body: str, text: str | None = None, create_csv_in_probe: bool = False) -> tuple[int, dict, Path, str]:
        d = self.stmp / name
        (d / "cache").mkdir(parents=True)
        (d / "legOut").mkdir()
        (d / "cache" / STUB_EXE).write_text(stub_body, encoding="ascii")
        body = text if text is not None else TEMPLATE
        probe = ("function Test-AttrCudaPresentMonTraceReady($Proc) { "
                 + (f"[IO.File]::WriteAllText('{ps(d / 'legOut' / 'presentmon.csv')}', 'h'); " if create_csv_in_probe else "")
                 + "[pscustomobject]@{ ready = $true; detail = 'stub' } }\n")
        script = d / "probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{ps(MODULE)}' -Force\n"
            + probe
            + f"$Cache = '{ps(d / 'cache')}'\n$PresentMonName = '{STUB_EXE}'\n$ExeName = 'MLVApp.exe'\n$PresentMonTimedSeconds = 60\n"
            "$PresentMonSessionName = 'MLVAttr3-stub'\n$PresentMonTerminateOnProcExit = $true\n"
            f"$legOut = '{ps(d / 'legOut')}'\n"
            "$presentMonPath = Join-Path $legOut 'presentmon.csv'\n"
            "$presentMonStdoutPath = Join-Path $legOut 'presentmon-stdout.txt'\n$presentMonStderrPath = Join-Path $legOut 'presentmon-stderr.txt'\n"
            "$presentMonTraceReadiness = [ordered]@{}\n$presentMonStreams = [ordered]@{ csvSeenDuringReadiness = $false }\n"
            "function Write-JobTrace([string]$Message) { }\n"
            + presentmon_functions(body) + "\n"
            "$proc = Start-PresentMonCapture $presentMonPath\n"
            "[void]$proc.WaitForExit(20000)\n"
            "Write-Output ('SEEN=' + $presentMonStreams['csvSeenDuringReadiness'])\n", encoding="utf-8")
        proc = run_pwsh(["-File", str(script)])
        return proc.returncode, {}, d, proc.stdout + proc.stderr

    def test_the_streams_land_in_files_beside_the_csv(self) -> None:
        code, _s, d, out = self.run_start("streams", "@echo off\r\necho stub-stdout-line\r\necho stub-stderr-line 1>&2\r\nexit /b 0\r\n")
        self.assertEqual(code, 0, out)
        self.assertIn("stub-stdout-line", (d / "legOut" / "presentmon-stdout.txt").read_text(encoding="utf-8"))
        self.assertIn("stub-stderr-line", (d / "legOut" / "presentmon-stderr.txt").read_text(encoding="utf-8"))

    def test_a_csv_that_appears_while_readiness_is_awaited_is_noted(self) -> None:
        # a live stub, so the readiness loop runs; the probe stub creates the csv on its first answer
        code, _s, _d, out = self.run_start("seen", "@echo off\r\nping -n 4 127.0.0.1 >nul\r\nexit /b 0\r\n", create_csv_in_probe=True)
        self.assertEqual(code, 0, out)
        self.assertIn("SEEN=True", out)

    def test_a_csv_that_never_appears_is_not_noted(self) -> None:
        code, _s, _d, out = self.run_start("unseen", "@echo off\r\nping -n 4 127.0.0.1 >nul\r\nexit /b 0\r\n")
        self.assertEqual(code, 0, out)
        self.assertIn("SEEN=False", out)

    def test_mutation_without_the_redirect_nothing_is_captured_again(self) -> None:
        mutated = TEMPLATE.replace(" -RedirectStandardOutput $presentMonStdoutPath -RedirectStandardError $presentMonStderrPath", "", 1)
        self.assertNotEqual(mutated, TEMPLATE, "the mutation anchor must exist")
        code, _s, d, out = self.run_start("mut-redirect", "@echo off\r\necho stub-stdout-line\r\nexit /b 0\r\n", text=mutated)
        self.assertEqual(code, 0, out)
        self.assertFalse((d / "legOut" / "presentmon-stdout.txt").exists(), "the UM state of 2026-10-03: PresentMon ran hidden with no streams")

    def test_the_job_places_the_stream_files_under_its_own_diagnostic_dir(self) -> None:
        self.assertIn("$presentMonStdoutPath = Join-Path $legOut 'presentmon-stdout.txt'", TEMPLATE)
        self.assertIn("$presentMonStderrPath = Join-Path $legOut 'presentmon-stderr.txt'", TEMPLATE)


class _StopHarness(SliceHarness):
    """Runs the real stop block (and the capture-json statement) against stubs."""

    def run_stop(self, name: str, wait: str, csv: bytes | None, run_log: str | None = None, backend: str | None = None, text: str | None = None,
                 contact_frames: dict[str, bytes] | None = None, streams: tuple[bytes, bytes] | None = (b"", b"")) -> tuple[int, dict, Path, str]:
        d = self.stmp / name
        pub = d / "pub"
        pub.mkdir(parents=True)
        cs = d / "contact-sheet"
        cs.mkdir()
        for fname, data in (contact_frames or {}).items():
            (cs / fname).write_bytes(data)
        (d / "legOut").mkdir()
        if csv is not None:
            (d / "legOut" / "presentmon.csv").write_bytes(csv)
        if streams is not None:
            (d / "legOut" / "presentmon-stdout.txt").write_bytes(streams[0])
            (d / "legOut" / "presentmon-stderr.txt").write_bytes(streams[1])
        log_file = d / "run.log"
        log_file.write_text(run_log if run_log is not None else um_run_log(UM2_GPU_SUMMARY), encoding="utf-8")
        body = text if text is not None else TEMPLATE
        wait_stub = ("function Wait-PresentMonCapture($Proc) { throw '" + PM_TIMEOUT_REASON + "' }\n" if wait == "throw" else
                     "function Wait-PresentMonCapture($Proc) { [pscustomobject]@{ status = 'done'; exitCode = 0; stopMethod = 'session_terminate'; exitedBeforeStop = $false; "
                     "terminateExitCode = 0; killUsed = $false; terminateSucceeded = $true; stopCausedByJob = $true } }\n")
        script = d / "probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{ps(MODULE)}' -Force\n"
            + gpu_summary_fn(body) + "\n"
            f"$Pub = '{ps(pub)}'\n$FixtureRehearsal = $false\n$SourceCommit = ('1' * 40)\n$ClipId = '{OWNER_CLIP}'\n"
            + (f"$Backend = '{backend}'\n" if backend else "")
            + "function Save-Json($Object, [string]$Path) { [void](Publish-AttrCudaText -Path $Path -Value ($Object | ConvertTo-Json -Depth 30)) }\n"
            "function Write-JobTrace([string]$Message) { }\n"
            "function Get-AttrCudaDisplayResultTail([object]$DisplayBlock) { '' }\n"
            + wait_stub
            + "$presentMonProc = $null\n$PresentMonSessionName = 'MLVAttr3-stub'\n"
            f"$legOut = '{ps(d / 'legOut')}'\n"
            "$presentMonPath = Join-Path $legOut 'presentmon.csv'\n"
            "$presentMonStdoutPath = Join-Path $legOut 'presentmon-stdout.txt'\n$presentMonStderrPath = Join-Path $legOut 'presentmon-stderr.txt'\n"
            "$presentMonStreams = [ordered]@{ csvSeenDuringReadiness = $false }\n"
            "$presentMonCaptureStartUtc = [datetime]::UtcNow; $presentMonPreSpawnUtc = $presentMonCaptureStartUtc; $presentMonPostSpawnUtc = $presentMonCaptureStartUtc\n"
            "$presentMonProcessStartUtc = $presentMonCaptureStartUtc; $presentMonCaptureStartUncertaintyMs = 1.0\n"
            "$presentMonTraceReadyVerified = $true; $presentMonTraceReadiness = [ordered]@{ verified = $true }\n"
            "function Repair-PresentMonCsvTail([string]$Path) { [pscustomobject]@{ trimmed = $false; unterminatedTail = 'none'; repairedPath = $null } }\n"
            "$displayWake = [ordered]@{}\n$displayBlock = [ordered]@{ venue = 'ultra-magnus' }\n$rows = @()\n"
            f"$rawLog = [IO.File]::ReadAllText('{ps(log_file)}')\n$measuredSmokeSessionId = '1'\n"
            f"$ContactSheetEnabled = $true\n$contactSheetDir = '{ps(cs)}'\n"
            + wait_block(body) + "\nWrite-Output 'RESULT=NO_FAILURE_BRANCH_TAKEN'\n"
            "if ($null -ne $presentMonCaptureEvidence) { Write-Output ('EVIDENCE=' + ($presentMonCaptureEvidence | ConvertTo-Json -Depth 6 -Compress)) }\n",
            encoding="utf-8")
        proc = run_pwsh(["-File", str(script)])
        summary_path = pub / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
        return proc.returncode, summary, pub, proc.stdout + proc.stderr


@requires_windows_pwsh
class PresentMonStopEvidenceTests(_StopHarness, unittest.TestCase):
    """Item 1, in the job: whatever way the PresentMon stop ends, its streams are published and presentmon-capture.json says whether the CSV ever existed."""

    def setUp(self) -> None:
        self.make_slice_dir()

    def test_a_clean_stop_with_no_csv_the_um2_shape_publishes_the_streams_and_records_no_csv(self) -> None:
        code, _summary, pub, out = self.run_stop("um2", "done", csv=None, streams=(b"", b"ETW session started\n"))
        self.assertEqual(code, 0, out)
        self.assertIn("NO_FAILURE_BRANCH_TAKEN", out)
        evidence = json.loads(next(l for l in out.splitlines() if l.startswith("EVIDENCE="))[len("EVIDENCE="):])
        self.assertEqual((evidence["csvEverExisted"], evidence["csvSizeAtStop"]), (False, None))
        self.assertIn("ETW session started", (pub / "presentmon-stderr.txt").read_text(encoding="utf-8"))
        self.assertTrue((pub / "presentmon-stdout.txt").exists())

    def test_a_wait_failure_publishes_the_streams_and_its_capture_json_carries_the_existence_record(self) -> None:
        code, _summary, pub, out = self.run_stop("waitfail", "throw", csv=b"h,x\n1,2\n", streams=(b"out\n", b"access denied\n"))
        self.assertEqual(code, 23, out)
        capture = json.loads((pub / "presentmon-capture.json").read_text(encoding="utf-8"))
        self.assertEqual((capture["csvEverExisted"], capture["csvSizeAtStop"]), (True, 8))
        self.assertTrue(capture["streams"]["stderr"]["published"])
        self.assertIn("access denied", (pub / "presentmon-stderr.txt").read_text(encoding="utf-8"))

    def test_the_streams_are_published_after_the_stop_and_before_the_terminal(self) -> None:
        block = wait_block()
        self.assertLess(block.index("Wait-PresentMonCapture $presentMonProc"), block.index("Publish-AttrCudaPresentMonCaptureEvidence"))
        self.assertLess(block.index("Publish-AttrCudaPresentMonCaptureEvidence"), block.rindex("exit 23"))

    def test_the_second_capture_json_statement_carries_the_record_too(self) -> None:
        stmt = _slice(TEMPLATE, CAPTURE_JSON_START, CAPTURE_JSON_END)
        for key in ("csvEverExisted=$presentMonCaptureEvidence.csvEverExisted", "csvSizeAtStop=$presentMonCaptureEvidence.csvSizeAtStop", "streams=$presentMonCaptureEvidence.streams"):
            self.assertIn(key, stmt)
        self.assertIn("csvEverExisted=$presentMonCaptureEvidence.csvEverExisted", wait_block())

    def test_mutation_without_the_evidence_call_the_streams_are_lost_again(self) -> None:
        call = re.search(r"\$presentMonCaptureEvidence = Publish-AttrCudaPresentMonCaptureEvidence [^\n]*", TEMPLATE)
        self.assertIsNotNone(call, "the job calls the evidence function exactly once, after the stop")
        mutated = TEMPLATE.replace(call.group(0), "$presentMonCaptureEvidence = $null", 1)
        code, _summary, pub, out = self.run_stop("mut-evidence", "done", csv=None, streams=(b"", b"why"), text=mutated)
        self.assertEqual(code, 0, out)
        self.assertFalse((pub / "presentmon-stderr.txt").exists(), "the UM state of 2026-10-03: nothing says why")

    def test_the_spawn_failure_publishes_the_streams_too(self) -> None:
        # PresentMon exiting rc=6 at startup (ETW access denied) is the commonest "why": its message is on a stream
        d = self.stmp / "spawn"
        pub = d / "pub"
        pub.mkdir(parents=True)
        (d / "legOut").mkdir()
        (d / "legOut" / "presentmon-stdout.txt").write_bytes(b"")
        (d / "legOut" / "presentmon-stderr.txt").write_bytes(b"error: failed to start trace session (rc=6)\n")
        script = d / "probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{ps(MODULE)}' -Force\n"
            f"$Pub = '{ps(pub)}'\n$FixtureRehearsal = $false\n$SourceCommit = ('1' * 40)\n$ClipId = '{OWNER_CLIP}'\n"
            "function Save-Json($Object, [string]$Path) { [void](Publish-AttrCudaText -Path $Path -Value ($Object | ConvertTo-Json -Depth 30)) }\n"
            "function Write-JobTrace([string]$Message) { }\nfunction Get-AttrCudaDisplayResultTail([object]$DisplayBlock) { '' }\n"
            f"$legOut = '{ps(d / 'legOut')}'\n$presentMonPath = Join-Path $legOut 'presentmon.csv'\n"
            "$presentMonStdoutPath = Join-Path $legOut 'presentmon-stdout.txt'\n$presentMonStderrPath = Join-Path $legOut 'presentmon-stderr.txt'\n"
            "$presentMonStreams = [ordered]@{ csvSeenDuringReadiness = $false }\n"
            "$presentMonSpawnError = 'PRESENTMON_FAILED rc=6'\n$displayWake = [ordered]@{}\n$displayBlock = [ordered]@{ venue = 'ultra-magnus' }\n"
            + _slice(TEMPLATE, SPAWN_START, SPAWN_END) + "\nWrite-Output 'RESULT=NO_FAILURE_BRANCH_TAKEN'\n", encoding="utf-8")
        proc = run_pwsh(["-File", str(script)])
        self.assertEqual(proc.returncode, 23, proc.stdout + proc.stderr)
        self.assertIn("rc=6", (pub / "presentmon-stderr.txt").read_text(encoding="utf-8"))
        summary = json.loads((pub / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["result"], "PRESENTMON_UNAVAILABLE")
        self.assertTrue(summary["presentMonStreams"]["stderr"]["published"])


class _ParseFailureHarness(SliceHarness):
    """Runs the real display-report failure branch against the UM-2 shape."""

    def run_parse_failure(self, name: str, status: str = "PRESENTMON_UNAVAILABLE", gpu_line: str = UM2_GPU_SUMMARY, backend: str | None = None, text: str | None = None,
                          contact_frames: dict[str, bytes] | None = None, enabled: bool = True) -> tuple[int, dict, Path, str]:
        d = self.stmp / name
        pub = d / "pub"
        pub.mkdir(parents=True)
        cs = d / "contact-sheet"
        cs.mkdir()
        for fname, data in (contact_frames if contact_frames is not None else SIX_FRAMES).items():
            (cs / fname).write_bytes(data)
        log_file = d / "run.log"
        log_file.write_text(um_run_log(gpu_line), encoding="utf-8")
        body = text if text is not None else TEMPLATE
        script = d / "probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{ps(MODULE)}' -Force\n"
            + gpu_summary_fn(body) + "\n"
            f"$Pub = '{ps(pub)}'\n$FixtureRehearsal = $false\n$SourceCommit = ('1' * 40)\n$ClipId = '{OWNER_CLIP}'\n"
            + (f"$Backend = '{backend}'\n" if backend else "")
            + "function Save-Json($Object, [string]$Path) { [void](Publish-AttrCudaText -Path $Path -Value ($Object | ConvertTo-Json -Depth 30)) }\n"
            "function Write-JobTrace([string]$Message) { }\nfunction Get-AttrCudaDisplayResultTail([object]$DisplayBlock) { '' }\n"
            f"$rawLog = [IO.File]::ReadAllText('{ps(log_file)}')\n$measuredSmokeSessionId = '1'\n"
            "$gpuSummary = Get-LastGpuSummary $rawLog $measuredSmokeSessionId\n"
            "$gpuFramesTotal = $gpuSummary.gpuReconReadbackFrames + $gpuSummary.gpuTextureReadbackFrames + $gpuSummary.gpuTextureNoReadbackFrames\n"
            f"$displayReport = [pscustomobject]@{{ status = '{status}'; reason = '{UM2_REASON.replace(chr(39), chr(39) * 2)}'; chains = @(); clockBracket = $null }}\n"
            "$displayAsleepOverridden = $false\n$presentMonCaptureStartUtc = [datetime]::UtcNow\n$displayWake = [ordered]@{}\n$displayBlock = [ordered]@{ venue = 'ultra-magnus' }\n"
            "$diagnostics = [ordered]@{}\n$rows = @(1..3)\n$stats = [ordered]@{}\n"
            f"$ContactSheetEnabled = ${'true' if enabled else 'false'}\n$contactSheetDir = '{ps(cs)}'\n"
            + _slice(body, PARSE_START, PARSE_END) + "\nWrite-Output 'RESULT=NO_FAILURE_BRANCH_TAKEN'\n", encoding="utf-8")
        proc = run_pwsh(["-File", str(script)])
        summary_path = pub / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
        return proc.returncode, summary, pub, proc.stdout + proc.stderr


@requires_windows_pwsh
class ParseFailureKeepsTheFramesTests(_ParseFailureHarness, unittest.TestCase):
    """Item 2: the UM-2 owner CUDA leg wrote six frames on the venue and the display-report failure branch published none."""

    def setUp(self) -> None:
        self.make_slice_dir()

    def test_the_um2_shape_publishes_the_six_frames_and_the_compose_marker(self) -> None:
        code, summary, pub, out = self.run_parse_failure("um2", gpu_line=UM2_GPU_SUMMARY)
        self.assertEqual(code, 23, out)
        self.assertEqual(summary["result"], "PRESENTMON_UNAVAILABLE")
        self.assertEqual(summary["gpuFramesTotal"], 474)
        raw = pub / "contact-sheet" / "raw"
        self.assertEqual(sorted(p.name for p in raw.iterdir()), sorted(SIX_FRAMES))
        for name, data in SIX_FRAMES.items():
            self.assertEqual((raw / name).read_bytes(), data)
        marker = (pub / "contact-sheet" / "compose-status.txt").read_text(encoding="utf-8")
        self.assertTrue(marker.startswith("CONTACT_SHEET_COMPOSE_UNAVAILABLE"), marker)
        self.assertFalse((pub / "contact-sheet" / "sheet.png").exists())

    def test_publishing_the_frames_changes_no_number_in_the_summary(self) -> None:
        _c1, off, _p1, _o1 = self.run_parse_failure("neq-off", enabled=False)
        _c2, on, _p2, _o2 = self.run_parse_failure("neq-on", enabled=True)
        strip = lambda s: {k: v for k, v in s.items() if k not in ("presentMonCaptureStartUtc", "artifactRoot")}
        self.assertEqual(strip(off), strip(on))

    def test_no_contact_sheet_run_publishes_no_contact_sheet_directory(self) -> None:
        code, _s, pub, out = self.run_parse_failure("off", enabled=False)
        self.assertEqual(code, 23, out)
        self.assertFalse((pub / "contact-sheet").exists())

    def test_a_run_that_captured_no_frame_writes_no_marker(self) -> None:
        code, _s, pub, out = self.run_parse_failure("empty", contact_frames={})
        self.assertEqual(code, 23, out)
        self.assertFalse((pub / "contact-sheet" / "compose-status.txt").exists())

    def test_a_cuda_leg_that_partly_fell_back_publishes_no_frame(self) -> None:
        # gpu > 0 AND cpu > 0: the frames were not all drawn by the cuda path -- the label would lie (master keeps none for CPU_FALLBACK_DETECTED)
        code, summary, pub, out = self.run_parse_failure("partial", gpu_line=UM_PARTIAL_GPU_SUMMARY_LINE)
        self.assertEqual(code, 23, out)
        self.assertEqual(summary["result"], "PRESENTMON_UNAVAILABLE", "the typed terminal is unchanged")
        self.assertFalse((pub / "contact-sheet").exists())

    def test_a_cuda_leg_that_fell_back_wholly_publishes_no_frame(self) -> None:
        code, _s, pub, out = self.run_parse_failure("allcpu", gpu_line=UM_CPU_GPU_SUMMARY_LINE)
        self.assertEqual(code, 23, out)
        self.assertFalse((pub / "contact-sheet").exists())

    def test_display_asleep_is_a_different_terminal_and_keeps_its_receipt_shape(self) -> None:
        code, summary, pub, out = self.run_parse_failure("asleep", status="DISPLAY_ASLEEP")
        self.assertEqual(code, 24, out)
        self.assertEqual(summary["result"], "DISPLAY_ASLEEP")
        self.assertFalse((pub / "contact-sheet").exists(), "a verified-zero display result is not a PresentMon failure; its frames stay unlisted")

    def test_the_publish_is_inside_the_branch_before_its_exit_and_after_the_counters_are_read(self) -> None:
        branch = _slice(TEMPLATE, PARSE_START, PARSE_END)
        self.assertLess(branch.index("Publish-AttrCudaContactSheetRawCaptures"), branch.index("exit $displayExitCode"))
        self.assertLess(TEMPLATE.index("$gpuSummary = Get-LastGpuSummary $rawLog $measuredSmokeSessionId"), TEMPLATE.index(PARSE_START))

    def test_the_branch_uses_the_same_counter_gate_as_the_wait_failure_branch(self) -> None:
        wait_gate = re.search(r"\$waitFailureCountersContradictLeg = (-not \(.*\))\n", TEMPLATE)
        parse_gate = re.search(r"\$parseFailureCountersContradictLeg = (-not \(.*\))\n", TEMPLATE)
        self.assertIsNotNone(wait_gate)
        self.assertIsNotNone(parse_gate, "the parse-failure branch states its gate")
        norm = lambda s: s.replace("$waitFailureGpuFramesTotal", "$T").replace("$gpuFramesTotal", "$T").replace("$waitFailureGpuSummary", "$S").replace("$gpuSummary", "$S")
        self.assertEqual(norm(wait_gate.group(1)), norm(parse_gate.group(1)), "one rule, two sites: they must not drift apart")

    def test_the_cpu_variant_swaps_in_the_inverse_gate(self) -> None:
        text = CpuGeneratedJobs.text("cpu")
        self.assertIn("$parseFailureCountersContradictLeg = -not ($gpuFramesTotal -le 0 -and [int64]$gpuSummary.cpuFrames -gt 0)", text)
        self.assertNotIn("$parseFailureCountersContradictLeg = -not ($gpuFramesTotal -gt 0", text)
        # a cpu job's branch (cpu-run counters) lists the frames; a cpu-labelled job over cuda counters does not
        code, _s, pub, out = self.run_parse_failure("cpu-ok", gpu_line=UM_CPU_GPU_SUMMARY_LINE, backend="cpu", text=text)
        self.assertEqual(code, 23, out)
        self.assertTrue((pub / "contact-sheet" / "raw").exists())
        code, _s, pub, out = self.run_parse_failure("cpu-bad", gpu_line=UM2_GPU_SUMMARY, backend="cpu", text=text)
        self.assertEqual(code, 23, out)
        self.assertFalse((pub / "contact-sheet").exists())

    def test_mutation_without_the_publish_call_the_leg_has_no_frame_again(self) -> None:
        call = "$parseFailureRawFrames = Publish-AttrCudaContactSheetRawCaptures -Enabled $ContactSheetEnabled -SourceDir $contactSheetDir -PubRoot $Pub"
        self.assertEqual(TEMPLATE.count(call), 1)
        mutated = TEMPLATE.replace(call, "$parseFailureRawFrames = $null", 1)
        code, _s, pub, out = self.run_parse_failure("mut-pub", text=mutated)
        self.assertEqual(code, 23, out)
        self.assertFalse((pub / "contact-sheet" / "raw").exists(), "the UM state of 2026-10-03: six frames on the venue, none on the leg")

    def test_mutation_without_the_gate_a_partly_fallen_back_leg_lists_frames_again(self) -> None:
        gate = re.search(r"\$parseFailureCountersContradictLeg = -not \(.*\)\n", TEMPLATE).group(0)
        mutated = TEMPLATE.replace(gate, "$parseFailureCountersContradictLeg = $false\n", 1)
        code, _s, pub, out = self.run_parse_failure("mut-gate", gpu_line=UM_PARTIAL_GPU_SUMMARY_LINE, text=mutated)
        self.assertEqual(code, 23, out)
        self.assertTrue((pub / "contact-sheet" / "raw").exists(), "without the gate the frames of a leg that partly fell back are listed (and would be labelled cuda)")

    def test_mutation_without_the_status_restriction_display_asleep_lists_frames(self) -> None:
        self.assertIn("$displayReport.status -eq 'PRESENTMON_UNAVAILABLE'", region_text(TEMPLATE, "DVE-PRESENTMON-EVIDENCE-1"))
        mutated = TEMPLATE.replace("-not $parseFailureCountersContradictLeg -and $displayReport.status -eq 'PRESENTMON_UNAVAILABLE'", "-not $parseFailureCountersContradictLeg", 1)
        self.assertNotEqual(mutated, TEMPLATE)
        _c, _s, pub, _o = self.run_parse_failure("mut-status", status="DISPLAY_ASLEEP", text=mutated)
        self.assertTrue((pub / "contact-sheet" / "raw").exists())


@requires_windows_pwsh
class CpuLegPresentMonWaitFailureIsInformationalTests(_StopHarness, unittest.TestCase):
    """Item 3 (hardening DVE-CPU-PRESENTMON-WAIT-GATES-1): the docs say PresentMon is informational on cpu, yet a cpu leg whose PresentMon hung ended FAIL
    PRESENTMON_UNAVAILABLE exit 23 although its playback passed."""

    def setUp(self) -> None:
        self.make_slice_dir()

    def test_a_cpu_leg_whose_wait_failed_takes_no_terminal(self) -> None:
        text = CpuGeneratedJobs.text("cpu")
        code, summary, _pub, out = self.run_stop("cpu-wait", "throw", csv=None, run_log=um_run_log(UM_CPU_GPU_SUMMARY_LINE), backend="cpu", text=text)
        self.assertEqual(code, 0, out)
        self.assertIn("NO_FAILURE_BRANCH_TAKEN", out)
        self.assertEqual(summary, {}, "no PRESENTMON_UNAVAILABLE summary is written: the leg's own gates write the verdict")

    def test_the_um_cpu_look_job_is_the_same(self) -> None:
        text = CpuGeneratedJobs.text("um-cpu-look")
        code, _summary, _pub, out = self.run_stop("umcpu-wait", "throw", csv=None, run_log=um_run_log(UM_CPU_GPU_SUMMARY_LINE), backend="cpu", text=text)
        self.assertEqual(code, 0, out)
        self.assertIn("NO_FAILURE_BRANCH_TAKEN", out)

    def test_a_cuda_leg_whose_wait_failed_still_ends_in_the_typed_terminal(self) -> None:
        for kind in ("default", "um-cuda"):
            code, summary, _pub, out = self.run_stop(f"cuda-{kind}", "throw", csv=None, text=CpuGeneratedJobs.text(kind))
            self.assertEqual(code, 23, out)
            self.assertEqual(summary["result"], "PRESENTMON_UNAVAILABLE")

    def test_the_cpu_wait_failure_is_recorded_as_status_unavailable_with_its_reason(self) -> None:
        text = CpuGeneratedJobs.text("cpu")
        d = self.stmp / "override"
        d.mkdir()
        script = d / "probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            "function Get-AttrCudaForegroundVerification([string]$LogText) { [pscustomobject]@{ verified = $false; reason = 'stub' } }\n"
            "$Backend = 'cpu'\n$rawLog = ''\n$earlyAppSwapTelemetry = [pscustomobject]@{ newFrameSwapCount = 0 }\n$appSwapTelemetry = $earlyAppSwapTelemetry\n"
            f"$presentMonWaitError = '{PM_TIMEOUT_REASON}'\n"
            "$displayReport = [pscustomobject]@{ status = 'OK'; reason = $null }\n"
            "$presentMonStatus = 'degraded'; $presentMonStatusReason = $null\n"
            + _slice(text, OVERRIDE_START, PARSE_START) + "\n"
            + _slice(text, STATUS_OVERRIDE_START, OVERRIDE_END) + "\n"
            "Write-Output ('OVERRIDDEN=' + $displayAsleepOverridden)\nWrite-Output ('STATUS=' + $presentMonStatus)\nWrite-Output ('REASON=' + $presentMonStatusReason)\n", encoding="utf-8")
        proc = run_pwsh(["-File", str(script)])
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, out)
        self.assertIn("OVERRIDDEN=True", out)
        self.assertIn("STATUS=unavailable", out)
        self.assertIn("PRESENTMON_TIMEOUT", out, "the wait failure's own reason is in the status reason")

    def test_mutation_without_the_cpu_edit_a_cpu_leg_fails_on_presentmon_again(self) -> None:
        edit = "if ($null -ne $presentMonWaitError -and $Backend -ne 'cpu') {"
        text = CpuGeneratedJobs.text("cpu")
        self.assertIn(edit, text)
        mutated = text.replace(edit, "if ($null -ne $presentMonWaitError) {", 1)
        code, summary, _pub, out = self.run_stop("mut-cpu", "throw", csv=None, run_log=um_run_log(UM_CPU_GPU_SUMMARY_LINE), backend="cpu", text=mutated)
        self.assertEqual(code, 23, out)
        self.assertEqual(summary["result"], "PRESENTMON_UNAVAILABLE", "the DVE-CPU-PRESENTMON-WAIT-GATES-1 state: a played cpu leg ends FAIL on an informational tool")

    def test_the_default_job_text_is_not_touched_by_the_cpu_edit(self) -> None:
        self.assertIn("if ($null -ne $presentMonWaitError) {", TEMPLATE)
        self.assertNotIn("-and $Backend -ne 'cpu') {\n    # CUDA-PERF-DISPLAY-IDENTITY-HARNESS-3", TEMPLATE)


@requires_windows_pwsh
class ByteIdentityPinIsHonestTests(unittest.TestCase):
    """The default job's byte-identity pin: every line this card adds to the default job is bracketed by its own sentinel pair, the count is pinned, and the pin
    moved to the commit that carries the card's in-place item-1 edits (see test_dual_venue_evidence.BASELINE_COMMIT)."""

    def test_the_default_job_carries_exactly_the_pinned_number_of_bracketed_regions(self) -> None:
        emitted = CpuGeneratedJobs.text("default")
        self.assertEqual(emitted.count("DVE-PRESENTMON-EVIDENCE-1 >>>"), emitted.count("DVE-PRESENTMON-EVIDENCE-1 <<<"))
        self.assertEqual(emitted.count("DVE-PRESENTMON-EVIDENCE-1 >>>"), dve.GeneratorByteIdentityAndVariantTests.PRESENTMON_EVIDENCE_REGIONS)

    def test_the_regions_hold_the_item_2_publish_and_nothing_measured(self) -> None:
        text = region_text(TEMPLATE, "DVE-PRESENTMON-EVIDENCE-1")
        self.assertIn("Publish-AttrCudaContactSheetRawCaptures", text)
        for forbidden in ("Get-AttrCudaPresentMonDisplayReport", "Get-Stats", "Wait-PresentMonCapture", "Start-PresentMonCapture", "$presentMonStatus ="):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
