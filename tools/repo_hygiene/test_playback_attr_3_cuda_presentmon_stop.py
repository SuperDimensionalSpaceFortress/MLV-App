"""UM-PRESENTMON-STOP-1: PresentMon capture ends cleanly and flushed once the app's exit is confirmed,
on every leg (owner and fixture), and never depends on PresentMon noticing the exit itself.

Evidence behind the card (lane-UM-PRESENTMON-TIMEOUT-1 r1 summary): on Ultra-Magnus, PresentMon 2.5.1
started with `--timed <long> --terminate_on_proc_exit` did not exit after MLVApp exited cleanly, so the
35 s wait killed it (PRESENTMON_TIMEOUT -> PRESENTMON_UNAVAILABLE) and the Kill() left a CSV cut off
mid-field; fixture legs hid the same dependence behind a fixed `--timed 55` that ended 22.9 s before
playback did.

Flags are those of the pinned PresentMon 2.5.1 `--help` (saved with the lane's evidence):
`--session_name`, `--terminate_existing_session`, `--stop_existing_session`.

Static tests pin the emitted template's text; the executed tests run the REAL extracted PowerShell
functions against REAL child processes, with only the PresentMon launcher / session-terminate helper
stubbed (no PresentMon binary, ETW rights or display exist in CI).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.repo_hygiene.test_playback_attr_3_cuda_presentmon_display_report import (
    CAPTURE_START_UTC,
    REAL_PRESENTMON_HEADER,
    TARGET_PID,
    _real_csv_row,
    _result_json,
)


ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "tools" / "profiling" / "bachelor" / "AttrCudaArtifacts.psm1"
ATTRIBUTION_GENERATOR = ROOT / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-job.ps1"

PWSH = shutil.which("pwsh")
requires_pwsh = unittest.skipIf(PWSH is None, "pwsh is not on PATH")


def _run_pwsh_file(script: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=180,
    )


def _generator_text() -> str:
    return ATTRIBUTION_GENERATOR.read_text(encoding="utf-8")


def _template() -> str:
    text = _generator_text()
    start = text.index("$template = @'")
    return text[start:text.index("\n'@", start)]


def _function_body(text: str, name: str) -> str:
    start = text.index(f"function {name}(")
    nxt = text.find("\nfunction ", start + 1)
    return text[start:nxt]


class StartArgumentsStaticTests(unittest.TestCase):
    def test_start_args_carry_a_per_job_session_name_before_stop_existing_session(self) -> None:
        start = _function_body(_template(), "Start-PresentMonCapture")
        self.assertIn("'--session_name', $PresentMonSessionName", start)
        self.assertLess(start.index("'--session_name'"), start.index("'--stop_existing_session'"))

    def test_the_session_name_is_derived_from_the_job_id_before_the_spawn(self) -> None:
        template = _template()
        assign = template.index("$PresentMonSessionName = Get-PresentMonSessionName $JobId")
        self.assertLess(assign, template.index("$presentMonProc = Start-PresentMonCapture $presentMonPath"))

    def test_the_wait_call_site_stops_the_named_session(self) -> None:
        self.assertIn("$presentMonDoneResult = Wait-PresentMonCapture $presentMonProc -SessionName $PresentMonSessionName", _template())

    def test_the_named_terminate_is_issued_before_any_kill_in_the_stop_path(self) -> None:
        wait = _function_body(_template(), "Wait-PresentMonCapture")
        self.assertLess(wait.index("Invoke-PresentMonSessionTerminate"), wait.index("$Proc.Kill()"))
        helper = _function_body(_template(), "Invoke-PresentMonSessionTerminate")
        self.assertIn("'--session_name', $SessionName, '--terminate_existing_session'", helper)

    def test_the_csv_tail_is_repaired_after_the_stop_and_before_the_report_reads_it(self) -> None:
        template = _template()
        repair = template.index("Repair-PresentMonCsvTail $presentMonPath")
        self.assertGreater(repair, template.index("$presentMonDoneResult = Wait-PresentMonCapture"))
        self.assertLess(repair, template.index("Get-AttrCudaPresentMonDisplayReport -CsvPath $presentMonPath"))

    def test_a_repaired_csv_is_what_is_parsed_and_the_raw_capture_is_published_beside_it(self) -> None:
        template = _template()
        repair = template.index("$presentMonTailTrim = Repair-PresentMonCsvTail $presentMonPath")
        block = template[repair:template.index("} catch {", repair)]
        self.assertIn("(Join-Path $Pub 'presentmon.raw.csv')", block)
        self.assertIn("$presentMonPath = $presentMonTailTrim.repairedPath", block)
        self.assertLess(block.index("presentmon.raw.csv"), block.index("$presentMonPath = $presentMonTailTrim.repairedPath"))

    def test_every_leg_sizes_the_capture_ceiling_from_the_derived_budget_and_asks_for_proc_exit(self) -> None:
        generator = _generator_text()
        self.assertIn(
            "PRESENTMON_TIMED_SECONDS = [string][int][math]::Ceiling($timeBudget.smokeProcessTimeoutMs / 1000.0)",
            generator,
        )
        self.assertIn("PRESENTMON_TERMINATE_ON_PROC_EXIT = '$true'", generator)
        self.assertNotIn("'55'", generator)


@requires_pwsh
class _ProbeCase(unittest.TestCase):
    def setUp(self) -> None:
        if os.name != "nt":
            self.skipTest("the ATTR-3 host jobs are Windows-only")
        self._tmp = tempfile.TemporaryDirectory(prefix="attr3-pmstop-")
        self.tmp = Path(os.path.realpath(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def _functions(self) -> str:
        text = _generator_text()
        start = text.index("function Start-PresentMonCapture(")
        return text[start:text.index("\nfunction Get-FrameRows(", start)]

    def run_probe(self, body: str) -> subprocess.CompletedProcess:
        script = self.tmp / "probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{MODULE}' -Force\n"
            "$Cache = 'C:\\no-such-cache'\n$PresentMonName = 'PresentMon-2.5.1-x64.exe'\n"
            "$ExeName = 'MLVApp.exe'\n$PresentMonTimedSeconds = 133\n$PresentMonTerminateOnProcExit = $true\n"
            "$PresentMonSessionName = ''\n"
            + self._functions() + "\n" + body,
            encoding="utf-8",
        )
        return _run_pwsh_file(script)


class StartExecutedTests(_ProbeCase):
    def test_start_passes_the_session_name_to_presentmon(self) -> None:
        out = self.tmp / "args.json"
        proc = self.run_probe(
            "function Start-Sleep { }\n"
            "function Start-Process {\n"
            "    param($FilePath, $ArgumentList, [switch]$PassThru, $WindowStyle)\n"
            f"    $ArgumentList | ConvertTo-Json | Set-Content -LiteralPath '{out}' -Encoding UTF8\n"
            "    [pscustomobject]@{ HasExited = $false; ExitCode = $null }\n"
            "}\n"
            "$PresentMonSessionName = Get-PresentMonSessionName 'playback-attr-3-cuda-abc-tiny_dual_iso-20261002-163000'\n"
            f"[void](Start-PresentMonCapture '{self.tmp / 'presentmon.csv'}')\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        args = json.loads(out.read_text(encoding="utf-8-sig"))
        self.assertIn("--session_name", args)
        self.assertEqual(
            args[args.index("--session_name") + 1],
            "MLVAttr3-playback-attr-3-cuda-abc-tiny_dual_iso-20261002-163000",
        )
        self.assertIn("--stop_existing_session", args)
        self.assertIn("--terminate_on_proc_exit", args)

    def test_the_session_name_is_charset_safe_and_keeps_the_unique_tail(self) -> None:
        out = self.tmp / "names.json"
        proc = self.run_probe(
            "$long = ('x' * 300) + '-STAMP-1234'\n"
            "@((Get-PresentMonSessionName 'job id/with:odd*chars'), (Get-PresentMonSessionName $long)) "
            f"| ConvertTo-Json | Set-Content -LiteralPath '{out}' -Encoding UTF8\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        odd, long = json.loads(out.read_text(encoding="utf-8-sig"))
        self.assertEqual(odd, "MLVAttr3-job-id-with-odd-chars")
        self.assertRegex(long, r"^MLVAttr3-[A-Za-z0-9_-]+$")
        self.assertLessEqual(len(long), 128)
        self.assertTrue(long.endswith("-STAMP-1234"))


class CleanStopExecutedTests(_ProbeCase):
    """Wait-PresentMonCapture against a REAL child process that exits (code 0) only when a sentinel
    file appears -- the stubbed session-terminate helper creates it, standing in for PresentMon
    reacting to its ETW session being stopped."""

    def _probe(self, *, terminate_stops_capture: bool, timeout: int = 5, pre_exited: bool = False) -> tuple[dict, list[str]]:
        out = self.tmp / "result.json"
        calls = self.tmp / "calls.log"
        sentinel = self.tmp / "stop.flag"
        body = (
            f"$sentinel = '{sentinel}'\n"
            f"$callLog = '{calls}'\n"
            f"$terminateStopsCapture = ${str(terminate_stops_capture).lower()}\n"
            "function Invoke-PresentMonSessionTerminate([string]$SessionName, [int]$TimeoutSeconds = 10) {\n"
            "    Add-Content -LiteralPath $callLog -Value \"terminate $SessionName\"\n"
            "    if ($terminateStopsCapture) { New-Item -ItemType File -Path $sentinel -Force | Out-Null }\n"
            "    [pscustomobject]@{ exitCode = 0; timedOut = $false; error = $null }\n"
            "}\n"
            + ("New-Item -ItemType File -Path $sentinel -Force | Out-Null\n" if pre_exited else "")
            + "$proc = Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile','-NonInteractive','-Command', "
            "\"while (-not (Test-Path -LiteralPath '$sentinel')) { Start-Sleep -Milliseconds 100 }; exit 0\") "
            "-PassThru -WindowStyle Hidden\n"
            + ("[void]$proc.WaitForExit(30000)\n" if pre_exited else "")
            + f"$r = Wait-PresentMonCapture $proc -SessionName 'MLVAttr3-test' -TimeoutSeconds {timeout}\n"
            f"$r | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath '{out}' -Encoding UTF8\n"
        )
        proc = self.run_probe(body)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        log = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
        return json.loads(out.read_text(encoding="utf-8-sig")), log

    def test_a_live_capture_is_stopped_by_the_named_terminate_without_a_kill(self) -> None:
        result, log = self._probe(terminate_stops_capture=True)
        self.assertEqual(log, ["terminate MLVAttr3-test"])
        self.assertEqual(result["stopMethod"], "session_terminate")
        self.assertFalse(result["exitedBeforeStop"])
        self.assertFalse(result["killUsed"])
        self.assertIsNone(result["killIssuedUtc"])
        self.assertIsNotNone(result["terminateIssuedUtc"])
        self.assertEqual(result["terminateExitCode"], 0)
        self.assertEqual(result["exitCode"], 0)

    def test_a_capture_that_ignores_the_terminate_is_killed_only_after_the_terminate(self) -> None:
        result, log = self._probe(terminate_stops_capture=False, timeout=1)
        self.assertEqual(log, ["terminate MLVAttr3-test"])
        self.assertEqual(result["stopMethod"], "kill_fallback")
        self.assertTrue(result["killUsed"])
        self.assertIsNone(result["killError"])
        self.assertIsNone(result["waitError"])
        # Both stamps are ISO-8601 'o' strings in UTC, so they order lexically.
        self.assertLess(result["terminateIssuedUtc"], result["killIssuedUtc"])

    def test_a_capture_that_already_exited_is_not_sent_a_terminate(self) -> None:
        result, log = self._probe(terminate_stops_capture=True, pre_exited=True)
        self.assertEqual(log, [])
        self.assertEqual(result["stopMethod"], "already_exited")
        self.assertTrue(result["exitedBeforeStop"])
        self.assertFalse(result["killUsed"])
        self.assertIsNone(result["terminateIssuedUtc"])

    def test_without_a_session_name_the_legacy_timeout_still_throws(self) -> None:
        proc = self.run_probe(
            "$proc = Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile','-NonInteractive','-Command','Start-Sleep -Seconds 120') "
            "-PassThru -WindowStyle Hidden\n"
            "try { [void](Wait-PresentMonCapture $proc -TimeoutSeconds 1); 'NO_THROW' } catch { 'THREW ' + $_.Exception.Message }\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("THREW PRESENTMON_TIMEOUT", proc.stdout)


class CsvTailRepairExecutedTests(_ProbeCase):
    def _csv_bytes(self, rows: int) -> bytes:
        lines = [",".join(REAL_PRESENTMON_HEADER)]
        for i in range(rows):
            row = _real_csv_row(time_in_ms=5000 + i * 1000)
            lines.append(",".join(row[name] for name in REAL_PRESENTMON_HEADER))
        return ("\r\n".join(lines) + "\r\n").encode("utf-8")

    def _repair(self, data: bytes) -> tuple[dict, bytes]:
        path = self.tmp / "presentmon.csv"
        path.write_bytes(data)
        out = self.tmp / "repair.json"
        proc = self.run_probe(
            f"Repair-PresentMonCsvTail '{path}' | ConvertTo-Json | Set-Content -LiteralPath '{out}' -Encoding UTF8\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(out.read_text(encoding="utf-8-sig")), path.read_bytes()

    def test_a_complete_csv_needs_no_repair_and_writes_nothing(self) -> None:
        data = self._csv_bytes(5)
        report, after = self._repair(data)
        self.assertFalse(report["trimmed"])
        self.assertIsNone(report["repairedPath"])
        self.assertEqual(after, data)
        self.assertFalse((self.tmp / "presentmon-repaired.csv").exists())

    def test_a_cut_off_last_row_is_dropped_into_a_repaired_copy_and_the_raw_file_is_kept(self) -> None:
        data = self._csv_bytes(5)
        fragment = b"MLVApp.exe,4242,0xCCC,DXGI,0,512,0,Composed: Fl"
        report, after = self._repair(data + fragment)
        self.assertTrue(report["trimmed"])
        self.assertEqual(after, data + fragment, "the raw capture must never be modified")
        self.assertEqual(report["droppedChars"], len(fragment))
        self.assertTrue(report["droppedText"].startswith("MLVApp.exe,4242"))
        repaired = Path(report["repairedPath"])
        self.assertEqual(repaired.name, "presentmon-repaired.csv")
        self.assertEqual(repaired.read_bytes(), data)

    def test_a_kill_fallback_csv_with_a_truncated_last_line_still_parses_into_a_report(self) -> None:
        raw = self.tmp / "presentmon.csv"
        raw.write_bytes(self._csv_bytes(10) + b"MLVApp.exe,4242,0xCCC,DXGI,0,512,0,Composed: Fl")
        result_path = self.tmp / "result.json"
        result_path.write_text(json.dumps(_result_json()), encoding="utf-8")
        out = self.tmp / "report.json"
        proc = self.run_probe(
            f"$trim = Repair-PresentMonCsvTail '{raw}'\n"
            "if (-not $trim.trimmed) { throw 'expected a repair' }\n"
            f"$start = [datetime]::Parse('{CAPTURE_START_UTC}', $null, [Globalization.DateTimeStyles]::RoundtripKind)\n"
            f"$resultJson = Get-Content -LiteralPath '{result_path}' -Raw | ConvertFrom-Json\n"
            "Get-AttrCudaPresentMonDisplayReport -CsvPath $trim.repairedPath -ResultJson $resultJson "
            "-EarliestCaptureStartUtc $start -LatestCaptureStartUtc $start "
            f"| ConvertTo-Json -Depth 10 | Set-Content -LiteralPath '{out}' -Encoding UTF8\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        report = json.loads(out.read_text(encoding="utf-8-sig"))
        self.assertEqual(report["status"], "OK", report)
        self.assertEqual(report["selectedChain"]["displayedCount"], 10)
        self.assertEqual(report["selectedChain"]["processId"], TARGET_PID)


if __name__ == "__main__":
    unittest.main()
