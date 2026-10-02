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
from datetime import datetime, timedelta, timezone
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

    def _probe(
        self, *, terminate_stops_capture: bool, timeout: int = 5, pre_exited: bool = False,
        terminate_exit_code: int = 0, capture_exit_code: int = 0,
    ) -> tuple[dict, list[str]]:
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
            f"    [pscustomobject]@{{ exitCode = {terminate_exit_code}; timedOut = $false; error = $null }}\n"
            "}\n"
            + ("New-Item -ItemType File -Path $sentinel -Force | Out-Null\n" if pre_exited else "")
            + "$proc = Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile','-NonInteractive','-Command', "
            f"\"while (-not (Test-Path -LiteralPath '$sentinel')) {{ Start-Sleep -Milliseconds 100 }}; exit {capture_exit_code}\") "
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

    def test_a_successful_terminate_makes_the_stop_job_caused_and_says_so(self) -> None:
        result, _ = self._probe(terminate_stops_capture=True)
        self.assertTrue(result["terminateSucceeded"])
        self.assertTrue(result["stopCausedByJob"])

    def test_a_kill_the_job_issued_is_job_caused_even_when_the_terminate_failed(self) -> None:
        result, _ = self._probe(terminate_stops_capture=False, timeout=1, terminate_exit_code=1)
        self.assertEqual(result["stopMethod"], "kill_fallback")
        self.assertFalse(result["terminateSucceeded"])
        self.assertTrue(result["stopCausedByJob"])

    def test_a_failed_terminate_followed_by_a_capture_crash_is_not_job_caused(self) -> None:
        # sol r1 BLOCKER (judge 2): helper rc=1, then the capture dies with an access violation. The
        # old code labelled the stop session_terminate and the call site then accepted rc -1073741819.
        result, log = self._probe(terminate_stops_capture=True, terminate_exit_code=1, capture_exit_code=-1073741819)
        self.assertEqual(log, ["terminate MLVAttr3-test"])
        self.assertEqual(result["stopMethod"], "exited_after_failed_terminate")
        self.assertFalse(result["terminateSucceeded"])
        self.assertFalse(result["stopCausedByJob"])
        self.assertEqual(result["terminateExitCode"], 1)
        self.assertEqual(result["exitCode"], -1073741819)
        self.assertFalse(result["killUsed"])

    def test_the_call_site_accepts_a_nonzero_exit_only_for_a_job_caused_stop(self) -> None:
        template = _template()
        start = template.index("$presentMonStoppedByJob = ")
        block = template[start:template.index("# The CSV is final once PresentMon has exited", start)]
        self.assertIn("$presentMonStoppedByJob = [bool]$presentMonDoneResult.stopCausedByJob", block)
        self.assertIn("(-not $presentMonStoppedByJob -and [int]$presentMonDoneResult.exitCode -ne 0)", block)

    # UM-PRESENTMON-STOP-2 item 2: the job is credited with ending PresentMon only if the process was
    # OBSERVED ALIVE immediately before the job's own terminate or Kill(). A fake process object models the
    # race (a real one cannot be made to exit between two statements): `exits_on_read` / `exits_on_wait`
    # pick the instant the capture dies on its own; Kill() is a recorded no-op, as .NET's is on Windows
    # for a process that has already exited.
    def _race_probe(
        self, *, exits_on_read: int | None = None, exits_on_wait: int | None = None,
        terminate_exit_code: int = 1, capture_exit_code: int = -1073741819,
    ) -> tuple[dict, list[str]]:
        out = self.tmp / "race.json"
        calls = self.tmp / "calls.log"
        body = (
            f"$global:callLog = '{calls}'\n"
            "$global:st = @{ reads = 0; waits = 0; exited = $false }\n"
            f"$global:exitsOnRead = {'$null' if exits_on_read is None else exits_on_read}\n"
            f"$global:exitsOnWait = {'$null' if exits_on_wait is None else exits_on_wait}\n"
            "function Invoke-PresentMonSessionTerminate([string]$SessionName, [int]$TimeoutSeconds = 10) {\n"
            "    Add-Content -LiteralPath $global:callLog -Value \"terminate $SessionName\"\n"
            f"    [pscustomobject]@{{ exitCode = {terminate_exit_code}; timedOut = $false; error = $null }}\n"
            "}\n"
            "$proc = [pscustomobject]@{}\n"
            "$proc | Add-Member -MemberType ScriptProperty -Name HasExited -Value {\n"
            "    $global:st.reads++\n"
            "    if ($null -ne $global:exitsOnRead -and $global:st.reads -ge $global:exitsOnRead) { $global:st.exited = $true }\n"
            "    $global:st.exited\n"
            "}\n"
            f"$proc | Add-Member -MemberType ScriptProperty -Name ExitCode -Value {{ if ($global:st.exited) {{ {capture_exit_code} }} else {{ $null }} }}\n"
            "$proc | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value {\n"
            "    param([int]$ms)\n"
            "    $global:st.waits++\n"
            "    if ($global:st.exited) { return $true }\n"
            "    if ($null -ne $global:exitsOnWait -and $global:st.waits -ge $global:exitsOnWait) { $global:st.exited = $true; return $false }\n"
            "    return $false\n"
            "}\n"
            "$proc | Add-Member -MemberType ScriptMethod -Name Kill -Value { Add-Content -LiteralPath $global:callLog -Value 'kill' }\n"
            "$r = Wait-PresentMonCapture $proc -SessionName 'MLVAttr3-test' -TimeoutSeconds 1 -KillWaitTimeoutSeconds 1\n"
            "$r | Add-Member -NotePropertyName callSiteRejectsExit -NotePropertyValue "
            "([bool]($r.status -ne 'done' -or (-not $r.stopCausedByJob -and [int]$r.exitCode -ne 0)))\n"
            f"$r | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath '{out}' -Encoding UTF8\n"
        )
        proc = self.run_probe(body)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        log = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
        return json.loads(out.read_text(encoding="utf-8-sig")), log

    def test_sol_repro_a_crash_between_the_timed_out_wait_and_the_kill_is_not_labelled_kill_fallback(self) -> None:
        # sol r2 BLOCKER: the terminate helper fails (rc=1), the first wait times out, and the capture then
        # dies on its own (access violation) BEFORE the job's Kill() executes. Kill() returns without
        # effect, the second wait confirms the exit, and the old code called it kill_fallback / job-caused
        # and accepted the crash code. The job must check the process is still alive before Kill().
        result, log = self._race_probe(exits_on_wait=1)
        self.assertEqual(log, ["terminate MLVAttr3-test"], "no Kill() may be issued at an already-exited process")
        self.assertFalse(result["killUsed"])
        self.assertNotEqual(result["stopMethod"], "kill_fallback")
        self.assertEqual(result["stopMethod"], "exited_after_failed_terminate")
        self.assertFalse(result["stopCausedByJob"])
        self.assertEqual(result["exitCode"], -1073741819)
        self.assertTrue(result["callSiteRejectsExit"], "the crash code must reach PRESENTMON_UNAVAILABLE")

    def test_a_capture_that_dies_between_the_entry_check_and_the_terminate_is_not_sent_one(self) -> None:
        # The terminate is also gated on a fresh liveness read immediately before it is issued: a capture
        # that crashed in between is a self-exit, and a "successful" helper after the fact credits the
        # job with a stop it did not cause.
        result, log = self._race_probe(exits_on_read=2, terminate_exit_code=0)
        self.assertEqual(log, [], "the terminate must not be issued at a process observed dead")
        self.assertEqual(result["stopMethod"], "already_exited")
        self.assertTrue(result["exitedBeforeStop"])
        self.assertFalse(result["stopCausedByJob"])
        self.assertEqual(result["exitCode"], -1073741819)
        self.assertTrue(result["callSiteRejectsExit"])

    def test_a_clean_exit_between_the_wait_and_the_kill_after_a_successful_terminate_stays_job_caused(self) -> None:
        # The terminate WAS issued at a process observed alive and succeeded; the capture then exited on
        # its own timing before the Kill(). That is the terminate's doing: no Kill(), job-caused, rc 0.
        result, log = self._race_probe(exits_on_wait=1, terminate_exit_code=0, capture_exit_code=0)
        self.assertEqual(log, ["terminate MLVAttr3-test"])
        self.assertEqual(result["stopMethod"], "session_terminate")
        self.assertFalse(result["killUsed"])
        self.assertTrue(result["stopCausedByJob"])
        self.assertFalse(result["callSiteRejectsExit"])

    def test_the_stop_record_says_whether_the_process_was_alive_before_the_terminate_and_before_the_kill(self) -> None:
        result, _ = self._probe(terminate_stops_capture=False, timeout=1)
        self.assertTrue(result["aliveBeforeTerminate"])
        self.assertTrue(result["aliveBeforeKill"])
        self.assertEqual(result["stopMethod"], "kill_fallback")
        result, _ = self._race_probe(exits_on_wait=1)
        self.assertTrue(result["aliveBeforeTerminate"])
        self.assertFalse(result["aliveBeforeKill"])

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
        self.assertEqual(report["unterminatedTail"], "dropped_incomplete")
        self.assertIn("field(s)", report["tailReason"])
        self.assertEqual(after, data + fragment, "the raw capture must never be modified")
        self.assertEqual(report["droppedChars"], len(fragment))
        self.assertTrue(report["droppedText"].startswith("MLVApp.exe,4242"))
        repaired = Path(report["repairedPath"])
        self.assertEqual(repaired.name, "presentmon-repaired.csv")
        self.assertEqual(repaired.read_bytes(), data)

    @staticmethod
    def _row_text(**kwargs) -> str:
        row = _real_csv_row(**kwargs)
        return ",".join(row[name] for name in REAL_PRESENTMON_HEADER)

    def test_a_complete_last_row_without_its_newline_is_kept_not_dropped(self) -> None:
        # sol r1 BLOCKER (judge 3): PresentMon 2.5.1 writes the newline separately from the fields, so a
        # row can be complete in the file while its newline is not. 28 fields, all parseable -> keep it.
        data = self._csv_bytes(5)
        last = self._row_text(time_in_ms=42000, between_display_change="4000").encode("utf-8")
        report, after = self._repair(data + last)
        self.assertFalse(report["trimmed"])
        self.assertEqual(report["unterminatedTail"], "kept_complete")
        self.assertEqual(report["droppedChars"], 0)
        self.assertEqual(report["headerFieldCount"], 28)
        self.assertEqual(report["tailFieldCount"], 28)
        self.assertIsNone(report["repairedPath"])
        self.assertEqual(after, data + last, "the raw capture must never be modified")
        self.assertFalse((self.tmp / "presentmon-repaired.csv").exists())

    def test_a_full_field_count_last_row_with_an_unparsable_field_is_dropped(self) -> None:
        data = self._csv_bytes(5)
        fields = self._row_text(time_in_ms=42000).split(",")
        fields[-1] = "N"  # a numeric column cut inside its own NA
        report, after = self._repair(data + ",".join(fields).encode("utf-8"))
        self.assertTrue(report["trimmed"])
        self.assertEqual(report["unterminatedTail"], "dropped_incomplete")
        self.assertEqual(report["tailFieldCount"], 28)
        self.assertIn("numeric column", report["tailReason"])
        self.assertEqual(Path(report["repairedPath"]).read_bytes(), data)

    def test_a_last_row_cut_inside_a_quoted_field_is_dropped(self) -> None:
        data = self._csv_bytes(3)
        report, _ = self._repair(data + b'MLVApp.exe,4242,0xCCC,"DXGI,0')
        self.assertTrue(report["trimmed"])
        self.assertEqual(report["unterminatedTail"], "dropped_incomplete")
        self.assertIn("quoted", report["tailReason"])

    def test_a_kept_unterminated_row_is_parsed_into_the_report_with_the_row_present(self) -> None:
        raw = self.tmp / "presentmon.csv"
        raw.write_bytes(self._csv_bytes(10) + self._row_text(time_in_ms=15000).encode("utf-8"))
        result_path = self.tmp / "result.json"
        result_path.write_text(json.dumps(_result_json()), encoding="utf-8")
        out = self.tmp / "report.json"
        proc = self.run_probe(
            f"$trim = Repair-PresentMonCsvTail '{raw}'\n"
            "if ($trim.unterminatedTail -ne 'kept_complete') { throw 'expected the row to be kept' }\n"
            f"$start = [datetime]::Parse('{CAPTURE_START_UTC}', $null, [Globalization.DateTimeStyles]::RoundtripKind)\n"
            f"$resultJson = Get-Content -LiteralPath '{result_path}' -Raw | ConvertFrom-Json\n"
            f"Get-AttrCudaPresentMonDisplayReport -CsvPath '{raw}' -ResultJson $resultJson "
            "-EarliestCaptureStartUtc $start -LatestCaptureStartUtc $start "
            f"| ConvertTo-Json -Depth 10 | Set-Content -LiteralPath '{out}' -Encoding UTF8\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        report = json.loads(out.read_text(encoding="utf-8-sig"))
        self.assertEqual(report["status"], "OK", report)
        self.assertEqual(report["selectedChain"]["displayedCount"], 11)

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


class FailurePathStopExecutedTests(_ProbeCase):
    """fable r1 hardening 1: a failure-path stop terminates the job's NAMED session before any Kill()."""

    def _stop(self, *, terminate_stops_capture: bool, session: str = "MLVAttr3-test") -> tuple[dict, list[str]]:
        out = self.tmp / "stop-result.json"
        calls = self.tmp / "calls.log"
        sentinel = self.tmp / "stop.flag"
        session_arg = f" -SessionName '{session}'" if session else ""
        body = (
            f"$sentinel = '{sentinel}'\n"
            f"$callLog = '{calls}'\n"
            f"$terminateStopsCapture = ${str(terminate_stops_capture).lower()}\n"
            "function Invoke-PresentMonSessionTerminate([string]$SessionName, [int]$TimeoutSeconds = 10) {\n"
            "    Add-Content -LiteralPath $callLog -Value \"terminate $SessionName\"\n"
            "    if ($terminateStopsCapture) { New-Item -ItemType File -Path $sentinel -Force | Out-Null }\n"
            "    [pscustomobject]@{ exitCode = 0; timedOut = $false; error = $null }\n"
            "}\n"
            "$proc = Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile','-NonInteractive','-Command', "
            "\"while (-not (Test-Path -LiteralPath '$sentinel')) { Start-Sleep -Milliseconds 100 }; exit 0\") "
            "-PassThru -WindowStyle Hidden\n"
            f"$r = Stop-PresentMonCapture -Proc $proc{session_arg}\n"
            f"$r | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath '{out}' -Encoding UTF8\n"
        )
        proc = self.run_probe(body)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        log = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
        return json.loads(out.read_text(encoding="utf-8-sig")), log

    def test_a_failure_path_stop_terminates_the_named_session_and_needs_no_kill(self) -> None:
        result, log = self._stop(terminate_stops_capture=True)
        self.assertEqual(log, ["terminate MLVAttr3-test"])
        self.assertTrue(result["confirmedExited"])
        self.assertIsNone(result["killError"])

    def test_a_failure_path_stop_kills_after_the_terminate_when_the_capture_ignores_it(self) -> None:
        result, log = self._stop(terminate_stops_capture=False)
        self.assertEqual(log, ["terminate MLVAttr3-test"])
        self.assertTrue(result["confirmedExited"], "the Kill() fallback must still end the capture")

    def test_without_a_session_name_the_failure_path_stop_is_the_bare_kill_it_always_was(self) -> None:
        result, log = self._stop(terminate_stops_capture=True, session="")
        self.assertEqual(log, [])
        self.assertTrue(result["confirmedExited"])

    def test_every_failure_path_stop_call_in_the_job_passes_the_session_name(self) -> None:
        template = _template()
        for name in ("$presentMonStopOnKeepAliveFailure", "$presentMonStop"):
            self.assertIn(f"{name} = Stop-PresentMonCapture -Proc $presentMonProc -SessionName $PresentMonSessionName", template)
        self.assertEqual(template.count("Stop-PresentMonCapture -Proc $presentMonProc -SessionName $PresentMonSessionName"), 3)
        self.assertNotIn("Stop-PresentMonCapture -Proc $presentMonProc\n", template)


_BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _iso(ms: float) -> str:
    """UTC instant `ms` milliseconds after 2026-01-01T00:00:00Z, in the 7-digit form the app logs."""
    return (_BASE + timedelta(milliseconds=ms)).strftime("%Y-%m-%dT%H:%M:%S.%f") + "0Z"


def _swap_window_log(swaps: int, *, first: str = "2026-01-01T00:00:02.0000000Z", last: str = "2026-01-01T00:00:42.0000000Z") -> str:
    # The real playback_smoke.gpu_window_swaps field set (MainWindow.cpp). `swaps` is the app's count of
    # swaps INSIDE first_swap_utc..last_swap_utc (the session window), not a process-lifetime count.
    return (
        "playback_smoke.gpu_window_swaps session=1 window_active=1 telemetry_enabled=1 "
        f"swaps={swaps} swap_fps=1.0 max_gap_ms=1.0 max_gap_before_serial=1 max_gap_after_serial=2 "
        f"frames_presented={swaps} swaps_minus_frames_presented=0 head_gap_ms=1.0 tail_gap_ms=1.0 "
        f"first_swap_utc={first} last_swap_utc={last}"
    )


def _span(start: float, stop: float, step: float) -> list[float]:
    """start, start+step, ... up to and including stop (all in ms)."""
    count = int(round((stop - start) / step)) + 1
    return [start + i * step for i in range(count)]


# A venue-shaped timeline (UM owner legs: PresentMon rows begin many seconds before playback and run past
# it). All instants are UTC ms after the base; PresentMon's TimeInMs is relative to its trace origin, which
# is only known to lie inside the bracket [capture_start, post_spawn] = [0 ms, 3000 ms] (the job's own
# `Start-Sleep -Seconds 3` makes that bracket at least 3 s wide). The TRUE origin is VENUE_ANCHOR_MS.
VENUE_CAPTURE_START = _iso(0)
VENUE_POST_SPAWN = _iso(3000)
VENUE_ANCHOR_MS = 1000.0
VENUE_PROCESS = _result_json(start=_iso(5000), end=_iso(60000))
VENUE_FIRST_SWAP = 20000.0
VENUE_LAST_SWAP = 45000.0
VENUE_SWAPS = 501  # one swap per 50 ms present, first through last inclusive


def _venue_times(*, pre_step: float = 200.0, play_end: float = VENUE_LAST_SWAP, post: bool = True) -> list[float]:
    utc = _span(6000.0, 19800.0, pre_step) + _span(VENUE_FIRST_SWAP, play_end, 50.0)
    if post:
        utc += _span(45200.0, 57800.0, 200.0)
    return [u - VENUE_ANCHOR_MS for u in utc]


@requires_pwsh
class JobStopSufficiencyExecutedTests(unittest.TestCase):
    """After a stop THIS JOB caused, the capture must prove by POSITION that it covers the app's measured
    swap window (UM-PRESENTMON-STOP-2). The presentMonStatus block is EXECUTED verbatim from the generator
    against the module's real report output."""

    @classmethod
    def setUpClass(cls) -> None:
        text = _generator_text()
        start = text.index("$presentMonSufficiencyMinIntervalCount = 30")
        cls.status_source = text[start:text.index("\n\n$dllSha256Lower", start)]

    def setUp(self) -> None:
        if os.name != "nt":
            self.skipTest("the ATTR-3 host jobs are Windows-only")
        self._tmp = tempfile.TemporaryDirectory(prefix="attr3-pmsuff-")
        self.tmp = Path(os.path.realpath(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def _csv(self, times: list[float], *, between: dict[float, str] | None = None) -> Path:
        lines = [",".join(REAL_PRESENTMON_HEADER)]
        for t in times:
            row = _real_csv_row(time_in_ms=t, between_display_change=(between or {}).get(t, "50"))
            lines.append(",".join(row[name] for name in REAL_PRESENTMON_HEADER))
        path = self.tmp / "presentmon.csv"
        path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
        return path

    def _status(
        self, times: list[float], raw_log: str, *, stopped_by_job: bool, between: dict[float, str] | None = None,
        capture_start: str = CAPTURE_START_UTC, post_spawn: str | None = None, result_json: dict | None = None,
    ) -> dict:
        csv_path = self._csv(times, between=between)
        result_path = self.tmp / "result.json"
        result_path.write_text(json.dumps(result_json if result_json is not None else _result_json()), encoding="utf-8")
        log_path = self.tmp / "raw.log"
        log_path.write_text(raw_log, encoding="utf-8")
        out = self.tmp / "status.json"
        script = self.tmp / "probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{MODULE}' -Force\n"
            "$rt = [Globalization.DateTimeStyles]::RoundtripKind\n"
            f"$presentMonCaptureStartUtc = [datetime]::Parse('{capture_start}', $null, $rt)\n"
            f"$presentMonPostSpawnUtc = [datetime]::Parse('{post_spawn or capture_start}', $null, $rt)\n"
            f"$resultJson = (Get-Content -LiteralPath '{result_path}' -Raw | ConvertFrom-Json)\n"
            f"$rawLog = Get-Content -LiteralPath '{log_path}' -Raw\n"
            "if ($null -eq $rawLog) { $rawLog = '' }\n"
            f"$presentMonStoppedByJob = ${str(stopped_by_job).lower()}\n"
            f"$displayReport = Get-AttrCudaPresentMonDisplayReport -CsvPath '{csv_path}' -ResultJson $resultJson "
            "-EarliestCaptureStartUtc $presentMonCaptureStartUtc -LatestCaptureStartUtc $presentMonPostSpawnUtc\n"
            "$pmRows = @($displayReport.selectedChainRows)\n"
            "$pmIntervalRows = @($pmRows | Where-Object { $null -ne $_.msBetweenDisplayChange -and $_.msBetweenDisplayChange -gt 0 })\n"
            f"{self.status_source}\n"
            "[pscustomobject]@{ presentMonStatus = $presentMonStatus; presentMonStatusReason = $presentMonStatusReason; "
            "presented = $presentMonPresentedCount; headGapMs = $presentMonJobStopHeadGapMs; "
            "tailGapMs = $presentMonJobStopTailGapMs; anchorUncertaintyMs = $presentMonJobStopAnchorUncertaintyMs; "
            "presentsMin = $presentMonJobStopPresentsMin; presentsMax = $presentMonJobStopPresentsMax; "
            "inWindowSwaps = $presentMonJobStopWindowSwaps; "
            "countDeviation = $presentMonJobStopCountDeviation } | ConvertTo-Json -Depth 5 | "
            f"Set-Content -LiteralPath '{out}' -Encoding UTF8\n"
            "Write-Output 'PROBE_DONE'\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PROBE_DONE", proc.stdout, proc.stdout + proc.stderr)
        return json.loads(out.read_text(encoding="utf-8-sig"))

    def _venue(self, times: list[float], swaps: int = VENUE_SWAPS, *, stopped_by_job: bool = True) -> dict:
        return self._status(
            times, _swap_window_log(swaps, first=_iso(VENUE_FIRST_SWAP), last=_iso(VENUE_LAST_SWAP)),
            stopped_by_job=stopped_by_job, capture_start=VENUE_CAPTURE_START, post_spawn=VENUE_POST_SPAWN,
            result_json=VENUE_PROCESS,
        )

    @staticmethod
    def _rows_through(last_ms: float, *, count: int) -> list[float]:
        return [last_ms - (count - 1 - i) * 50.0 for i in range(count)]

    # ---- the two reviewers' repros -------------------------------------------------------------------

    def test_sol_r1_repro_lost_tail_after_a_job_caused_stop_is_not_ok(self) -> None:
        # 720 rows at 50 ms through 38000 ms in a 2000-42000 ms window; the app reports 724 swaps; the
        # tail gap is 4000 ms.
        times = self._rows_through(38000.0, count=720)
        for stopped in (False, True):
            result = self._status(times, _swap_window_log(724), stopped_by_job=stopped)
            if not stopped:
                self.assertEqual(result["presentMonStatus"], "ok", "a capture that ended on its own keeps the old arms")
            else:
                self.assertEqual(result["presentMonStatus"], "degraded", result)
                self.assertIn("job-stop tail", result["presentMonStatusReason"])
                self.assertGreaterEqual(result["tailGapMs"], 3900.0)

    def test_fable_r2_repro_rows_before_a_late_first_swap_do_not_hide_an_empty_tail(self) -> None:
        # fable r2 hardening 1, his exact numbers: sol's lost-tail data with first_swap moved to 00:00:07.
        # The old length difference read -950 ms and 0.55% and said ok with the last 4 s of the swap
        # window empty; by position the last row (38000) is 4000 ms short of last_swap (42000).
        times = self._rows_through(38000.0, count=720)
        log = _swap_window_log(724, first="2026-01-01T00:00:07.0000000Z")
        result = self._status(times, log, stopped_by_job=True)
        self.assertEqual(result["presentMonStatus"], "degraded", result)
        self.assertIn("job-stop tail", result["presentMonStatusReason"])
        self.assertAlmostEqual(result["tailGapMs"], 4000.0, delta=1.0)
        self.assertEqual(self._status(times, log, stopped_by_job=False)["presentMonStatus"], "ok")

    def test_sol_r2_repro_a_lost_slow_tail_inside_both_old_allowances_is_not_ok(self) -> None:
        # sol r2 blocker 1: 781 rows at 50 ms through 41000 ms, then 13 rows at 70 ms to 41910 ms; the app
        # reports 794 swaps over 2000..41910 ms. The kill loses the 13 tail rows. The old gate said ok
        # (shortfall 960 ms < 1000 ms, deviation 1.6% < 2%) and the reported p99 fell from 70 ms to 50 ms.
        full = _span(2000.0, 41000.0, 50.0) + [41000.0 + 70.0 * k for k in range(1, 14)]
        self.assertEqual(len(full), 794)
        log = _swap_window_log(794, first=_iso(2000), last=_iso(41910))
        complete = self._status(full, log, stopped_by_job=True)
        self.assertEqual(complete["presentMonStatus"], "ok", complete)
        lost = self._status(full[:781], log, stopped_by_job=True)
        self.assertEqual(lost["presentMonStatus"], "degraded", lost)
        self.assertIn("job-stop tail", lost["presentMonStatusReason"])
        self.assertAlmostEqual(lost["tailGapMs"], 910.0, delta=1.0)

    def test_sol_r2_calibration_a_complete_capture_with_startup_and_shutdown_presents_stays_ok(self) -> None:
        # sol r2 hardening / fable r2 hardening 2: 70 presents before the first swap and 64 after the last
        # are legitimate, so a count over the process lifetime (635) against the in-window swaps (501) read
        # 27% apart and degraded a COMPLETE capture. The count is now taken inside the swap window.
        result = self._venue(_venue_times())
        self.assertEqual(result["presented"], 635, "the lifetime count is still reported")
        self.assertEqual(result["presentMonStatus"], "ok", result)
        self.assertIsNone(result["presentMonStatusReason"])
        self.assertEqual(result["inWindowSwaps"], VENUE_SWAPS)
        self.assertGreaterEqual(result["presentsMax"], VENUE_SWAPS)
        self.assertEqual(result["countDeviation"], 0.0)
        self.assertAlmostEqual(result["anchorUncertaintyMs"], 3000.0, delta=1.0)

    def test_a_venue_shaped_lost_tail_is_not_ok_even_with_a_3_second_wide_anchor_bracket(self) -> None:
        # Startup presents (47 of them) cancel the lost 2.5 s of tail in the old length arm: shortfall is
        # negative, the lifetime count 498 is within 1% of 501 swaps, the temporal arm sees 4000 ms.
        times = _venue_times(pre_step=300.0, play_end=42500.0, post=False)
        log = _swap_window_log(VENUE_SWAPS, first=_iso(VENUE_FIRST_SWAP), last=_iso(VENUE_LAST_SWAP))
        short_process = _result_json(start=_iso(5000), end=_iso(45500))
        gated = self._status(
            times, log, stopped_by_job=True, capture_start=VENUE_CAPTURE_START, post_spawn=VENUE_POST_SPAWN,
            result_json=short_process,
        )
        self.assertEqual(gated["presentMonStatus"], "degraded", gated)
        self.assertIn("job-stop tail", gated["presentMonStatusReason"])
        self.assertGreaterEqual(gated["tailGapMs"], 2500.0)
        own = self._status(
            times, log, stopped_by_job=False, capture_start=VENUE_CAPTURE_START, post_spawn=VENUE_POST_SPAWN,
            result_json=short_process,
        )
        self.assertEqual(own["presentMonStatus"], "ok", "a capture that ended on its own keeps the old arms")

    # ---- the rest of the arms ------------------------------------------------------------------------

    def test_a_job_caused_stop_that_covers_the_whole_window_stays_ok(self) -> None:
        times = self._rows_through(42000.0, count=801)
        result = self._status(times, _swap_window_log(801), stopped_by_job=True)
        self.assertEqual(result["presentMonStatus"], "ok", result)
        self.assertIsNone(result["presentMonStatusReason"])
        self.assertLessEqual(result["tailGapMs"], 1.0)
        self.assertEqual(result["countDeviation"], 0.0)

    def test_a_capture_that_starts_after_the_first_swap_is_not_ok(self) -> None:
        # Rows begin 2000 ms after the first swap: nothing at or before first_swap + the head bound.
        times = _span(4000.0, 42000.0, 50.0)
        result = self._status(times, _swap_window_log(len(times)), stopped_by_job=True)
        self.assertEqual(result["presentMonStatus"], "degraded", result)
        self.assertIn("job-stop head", result["presentMonStatusReason"])
        self.assertGreaterEqual(result["headGapMs"], 1900.0)

    def test_a_job_caused_stop_whose_in_window_present_count_misses_the_app_swap_count_is_not_ok(self) -> None:
        for swaps in (700, 300):  # fewer presents than swaps (lost rows), and more (a count that disagrees)
            result = self._venue(_venue_times(), swaps)
            self.assertEqual(result["presentMonStatus"], "degraded", (swaps, result))
            self.assertIn("job-stop count", result["presentMonStatusReason"])
            self.assertNotIn("job-stop tail", result["presentMonStatusReason"])
            self.assertNotIn("job-stop head", result["presentMonStatusReason"])
            self.assertGreater(result["countDeviation"], 0.02)

    def test_a_job_caused_stop_without_swap_window_timestamps_cannot_prove_coverage(self) -> None:
        times = self._rows_through(42000.0, count=801)
        gate_only = "playback_smoke.gate session=1 verdict=0 frames_presented=801 decode_requests_issued=801\n"
        result = self._status(times, gate_only, stopped_by_job=True)
        self.assertEqual(result["presentMonStatus"], "degraded", result)
        self.assertIn("cannot be proven", result["presentMonStatusReason"])

    def test_an_inverted_capture_start_bracket_cannot_anchor_the_rows_so_the_stop_is_not_ok(self) -> None:
        # Item 3: when the conversion cannot be trusted the safe answer is degraded, never ok.
        result = self._status(
            _venue_times(), _swap_window_log(VENUE_SWAPS, first=_iso(VENUE_FIRST_SWAP), last=_iso(VENUE_LAST_SWAP)),
            stopped_by_job=True, capture_start=_iso(3000), post_spawn=_iso(0), result_json=VENUE_PROCESS,
        )
        self.assertEqual(result["presentMonStatus"], "degraded", result)
        self.assertIn("job-stop anchor", result["presentMonStatusReason"])

    def test_the_edge_bound_is_small_and_the_count_bound_is_two_percent(self) -> None:
        source = _generator_text()
        self.assertIn("$presentMonJobStopMaxEdgeGapMs = 250.0", source)
        self.assertIn("$presentMonJobStopMaxCountDeviation = 0.02", source)

    def test_sol_repro_a_complete_final_row_kept_by_the_repair_keeps_the_report_whole(self) -> None:
        # judge 3 shape end to end: 720 complete 50 ms rows through 38000 ms and a complete final row at
        # 42000 ms carrying a 4000 ms interval. Kept (not dropped), the capture covers the window.
        times = self._rows_through(38000.0, count=720) + [42000.0]
        result = self._status(times, _swap_window_log(721), stopped_by_job=True, between={42000.0: "4000"})
        self.assertEqual(result["presented"], 721)
        self.assertEqual(result["presentMonStatus"], "ok", result)
        self.assertLessEqual(result["tailGapMs"], 100.0)


if __name__ == "__main__":
    unittest.main()
