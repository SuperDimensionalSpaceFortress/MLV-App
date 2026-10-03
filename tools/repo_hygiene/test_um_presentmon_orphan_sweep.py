"""Tests for UM-PRESENTMON-ORPHAN-SWEEP-1 (round 1): a leftover PresentMon ETW session can never silently starve a capture.

CLASS: before a capture starts, no orphaned PresentMon ETW session can remain on the host; a capture that loses events says so with a typed token; the sweep never touches a
live capture.
Source: lane-UM-PRESENTMON-NOCSV-DIAG-1 r1 (Ultra-Magnus, 2026-10-03). An orphaned default-named `PresentMon` real-time ETW session (left by an earlier killed PresentMon) made
every OTHER-named PresentMon session lose all of its events (15-17k "ETW events were lost" per 12 s), so no CSV was ever written; UM-PRESENTMON-STOP-1 gave each job a unique
--session_name, so the job's own --stop_existing_session no longer cleared the orphan. The same diagnostic inferred that a kill_fallback leaves the job's own MLVAttr3-* session
orphaned too.

  1. Start-PresentMonCapture sweeps, before the spawn, the default `PresentMon` session and every `MLVAttr3-*` session that `logman query -ets` lists -- only when NO PresentMon
     process is alive on the host (a live one may own a capture; the sweep then records that it did not run). Each termination goes through the pinned PresentMon's own
     --terminate_existing_session (the job's existing helper); `logman stop <name> -ets` runs only for a session still listed after that, and what remains is recorded.
     Every action and exit code lands in presentmon-capture.json (orphanSweep) and the trace;
  2. a job-issued Kill() is followed by the terminate of the job's own named session, in both stop paths, so a killed capture cannot orphan it;
  3. PresentMon's stderr saying "ETW events were lost" is a typed PRESENTMON_EVENTS_LOST detail in presentmon-capture.json and in the PresentMon reason of the failure terminals.

The job's text is EXECUTED (real slices of the generator's template run in pwsh against stubs: a .cmd PresentMon, function stubs for Get-Process and logman); the module's own
functions are called directly. Every item has a mutation test.
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from tools.repo_hygiene.test_dual_venue_evidence import GENERATOR, OWNER_CLIP, lf, requires_windows_pwsh, run_pwsh
from tools.repo_hygiene.test_dve_leg_terminals import MODULE, TEMPLATE, SliceHarness, _slice, gpu_summary_fn, presentmon_functions, um_run_log, wait_block
from tools.repo_hygiene.test_dve_presentmon_evidence import PARSE_END, PARSE_START, SIX_FRAMES, UM2_GPU_SUMMARY, UM2_REASON, _StopHarness

STUB_EXE = "PresentMon-stub.cmd"
NEW_MODULE_FUNCTIONS = ("Get-AttrCudaPresentMonOrphanSessionName", "Get-AttrCudaEtsSessionListing", "Stop-AttrCudaEtsSession", "Get-AttrCudaPresentMonEventsLost",
                        "Add-AttrCudaPresentMonEventsLostDetail", "Add-AttrCudaPresentMonEventsLostDetailToReport")
OPEN = "UM-PRESENTMON-ORPHAN-SWEEP-1 >>>"
CLOSE = "UM-PRESENTMON-ORPHAN-SWEEP-1 <<<"

# The shape of the real stderr line, from the diagnostic's per-arm stderr files (count only; nothing else of the evidence is reproduced).
LOST_LINE = "warning: 15318 ETW events were lost.\n"

LOGMAN_HEADER = ("Data Collector Set                      Type                          Status\n"
                 "-------------------------------------------------------------------------------\n")
LOGMAN_FOOTER = "\nThe command completed successfully.\n"


def ps(path: Path) -> str:
    return str(path).replace("'", "''")


def listing(*names: str) -> str:
    # a name longer than the 40-column name field is followed by two spaces here (the real logman's wide-name layout was not observed: the sweep records the matching listing lines so a surprise is visible)
    return LOGMAN_HEADER + "".join(f"{n.ljust(40) if len(n) < 40 else n + '  '}Trace                         Running\n" for n in names) + LOGMAN_FOOTER


def mutate(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, f"the mutation anchor must exist exactly once: {old!r}"
    return text.replace(old, new, 1)


# --------------------------------------------------------------------------------------------------------------------- module functions
@requires_windows_pwsh
class ModuleFunctionTests(unittest.TestCase):
    """Items 1 and 3, the module's own functions, called directly (the job embeds them verbatim)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="um-orphan-mod-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def call(self, body: str, module_text: str | None = None) -> dict:
        module = MODULE
        if module_text is not None:
            module = self.tmp / "AttrCudaArtifacts.psm1"
            module.write_text(module_text, encoding="utf-8")
        proc = run_pwsh(["-Command", f"$ErrorActionPreference = 'Stop'\nImport-Module '{ps(module)}' -Force\n{body}"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        line = next(l for l in proc.stdout.splitlines() if l.startswith("JSON="))
        return json.loads(line[len("JSON="):])

    def names(self, text: str, module_text: str | None = None) -> list[str]:
        src = self.tmp / "listing.txt"
        src.write_text(text, encoding="utf-8")
        got = self.call(f"$r = @(Get-AttrCudaPresentMonOrphanSessionName -ListingText ([IO.File]::ReadAllText('{ps(src)}')))\nWrite-Output ('JSON=' + (ConvertTo-Json -InputObject $r -Compress))", module_text)
        return got

    def test_the_default_session_and_every_mlvattr3_session_are_the_candidates(self) -> None:
        got = self.names(listing("PresentMon", "MLVAttr3-playback-attr-3-cuda-abc-20261002-163000", "mlvattr3-lower-case-job", "Circular Kernel Context Logger", "EventLog-System"))
        self.assertEqual(got, ["PresentMon", "MLVAttr3-playback-attr-3-cuda-abc-20261002-163000", "mlvattr3-lower-case-job"])

    def test_an_unrelated_session_is_never_a_candidate(self) -> None:
        got = self.names(listing("PresentMonitor2", "PresentMon_other", "NT Kernel Logger", "MyMLVAttr3-x", "SomeoneElsesCapture"))
        self.assertEqual(got, [])

    def test_the_listing_boilerplate_is_never_a_candidate(self) -> None:
        self.assertEqual(self.names(LOGMAN_HEADER + LOGMAN_FOOTER), [])
        self.assertEqual(self.names(""), [])

    def test_a_session_is_listed_once(self) -> None:
        self.assertEqual(self.names(listing("PresentMon", "PresentMon")), ["PresentMon"])

    def test_mutation_a_looser_match_would_sweep_a_stranger(self) -> None:
        module = MODULE.read_text(encoding="utf-8")
        anchor = "(PresentMon|MLVAttr3-[A-Za-z0-9_-]+)\\s+\\S"
        self.assertEqual(module.count(anchor), 1, "the mutation anchor must exist exactly once")
        mutated = module.replace(anchor, "(PresentMon\\w*|MLVAttr3-[A-Za-z0-9_-]+)\\s+\\S", 1)
        self.assertEqual(self.names(listing("PresentMonitor2"), module_text=mutated), ["PresentMonitor2"])

    def test_mutation_a_stop_wrapper_without_its_name_guard_would_stop_a_stranger(self) -> None:
        module = MODULE.read_text(encoding="utf-8")
        anchor = "-notmatch '^(?i:PresentMon|MLVAttr3-[A-Za-z0-9_-]+)$'"
        self.assertEqual(module.count(anchor), 1, "the mutation anchor must exist exactly once")
        marker = self.tmp / "stops.log"
        got = self.call(f"function global:logman {{ Add-Content -LiteralPath '{ps(marker)}' -Value ($args[1]); $global:LASTEXITCODE = 0 }}\n"
                        "$r = Stop-AttrCudaEtsSession -SessionName 'NT Kernel Logger'\nWrite-Output ('JSON=' + ($r | ConvertTo-Json -Compress))",
                        module_text=module.replace(anchor, "-notmatch '.'", 1))
        self.assertEqual(got["exitCode"], 0)
        self.assertIn("NT Kernel Logger", marker.read_text(encoding="utf-8"), "the guard is what keeps the wrapper off other people's sessions")

    def lost(self, stderr: bytes | None, module_text: str | None = None) -> dict:
        path = self.tmp / "stderr.txt"
        if stderr is not None:
            path.write_bytes(stderr)
        return self.call(f"$r = Get-AttrCudaPresentMonEventsLost -StderrPath '{ps(path)}'\nWrite-Output ('JSON=' + ($r | ConvertTo-Json -Depth 4 -Compress))", module_text)

    def test_the_um_stderr_line_is_a_typed_events_lost_detail(self) -> None:
        got = self.lost(LOST_LINE.encode("ascii"))
        self.assertTrue(got["detected"])
        self.assertEqual((got["messages"], got["maxReported"]), (1, 15318))
        self.assertTrue(got["detail"].startswith("PRESENTMON_EVENTS_LOST"), got["detail"])
        self.assertIn("15318", got["detail"])

    def test_repeated_lines_report_the_message_count_and_the_largest_count(self) -> None:
        got = self.lost(b"warning: 200 ETW events were lost.\nsomething else\nwarning: 17433 ETW events were lost.\nwarning: 90 ETW events were lost.\n")
        self.assertEqual((got["detected"], got["messages"], got["maxReported"]), (True, 3, 17433))

    def test_a_clean_stderr_detects_nothing(self) -> None:
        got = self.lost(b"")
        self.assertEqual((got["detected"], got["messages"], got["maxReported"], got["detail"]), (False, 0, None, None))
        got = self.lost(b"Started recording.\nStopped recording.\n")
        self.assertFalse(got["detected"])

    def test_a_missing_stderr_detects_nothing_and_never_throws(self) -> None:
        got = self.lost(None)
        self.assertFalse(got["detected"])

    def test_a_loss_older_than_the_bound_is_not_mistaken_for_the_tail(self) -> None:
        # the read is bounded to the stream's tail (like the publish); a loss inside the tail is found, one before it is not claimed
        got = self.lost(b"x" * 400000 + LOST_LINE.encode("ascii"))
        self.assertTrue(got["detected"])

    def test_mutation_a_changed_pattern_loses_the_detail_again(self) -> None:
        module = MODULE.read_text(encoding="utf-8")
        self.assertEqual(module.count("ETW events were lost"), 1, "the mutation anchor must exist exactly once")
        mutated = module.replace("ETW events were lost", "ETW events were found", 1)
        self.assertFalse(self.lost(LOST_LINE.encode("ascii"), module_text=mutated)["detected"], "the UM state of 2026-10-03: a recurrence reads as 'output does not exist'")

    def with_detail(self, reason: str | None, events: dict | None) -> str:
        ev = "$null" if events is None else "[pscustomobject]@{ detected = $%s; detail = 'PRESENTMON_EVENTS_LOST: 12 lost' }" % ("true" if events["detected"] else "false")
        r = "$null" if reason is None else "'" + reason.replace("'", "''") + "'"
        proc = run_pwsh(["-Command", f"$ErrorActionPreference = 'Stop'\nImport-Module '{ps(MODULE)}' -Force\n$r = Add-AttrCudaPresentMonEventsLostDetail -Reason {r} -EventsLost ({ev})\nWrite-Output ('OUT=' + $r)"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return next(l for l in proc.stdout.splitlines() if l.startswith("OUT="))[4:]

    def test_the_reason_gains_the_typed_detail_when_events_were_lost(self) -> None:
        out = self.with_detail("PresentMon output does not exist: X", {"detected": True})
        self.assertTrue(out.startswith("PresentMon output does not exist: X"), out)
        self.assertIn("PRESENTMON_EVENTS_LOST: 12 lost", out)

    def test_the_reason_is_unchanged_without_a_loss_or_without_evidence(self) -> None:
        self.assertEqual(self.with_detail("PresentMon output does not exist: X", {"detected": False}), "PresentMon output does not exist: X")
        self.assertEqual(self.with_detail("PresentMon output does not exist: X", None), "PresentMon output does not exist: X")

    def test_a_report_gains_the_detail_in_a_copy_that_keeps_every_other_property_in_order(self) -> None:
        got = self.call(
            "$report = [pscustomobject]@{ status = 'PRESENTMON_UNAVAILABLE'; reason = 'PresentMon output does not exist: X'; chains = @(1, 2); selectedChain = $null }\n"
            "$e = [pscustomobject]@{ detected = $true; detail = 'PRESENTMON_EVENTS_LOST: 12 lost' }\n"
            "$r = Add-AttrCudaPresentMonEventsLostDetailToReport -Report $report -EventsLost $e\n"
            "Write-Output ('JSON=' + (ConvertTo-Json -InputObject ([ordered]@{ names = @($r.PSObject.Properties.Name); reason = $r.reason; status = $r.status; chains = @($r.chains); same = [object]::ReferenceEquals($r, $report); original = $report.reason }) -Compress))")
        self.assertEqual(got["names"], ["status", "reason", "chains", "selectedChain"])
        self.assertIn("PRESENTMON_EVENTS_LOST", got["reason"])
        self.assertTrue(got["reason"].startswith("PresentMon output does not exist: X"))
        self.assertEqual((got["status"], got["chains"], got["same"]), ("PRESENTMON_UNAVAILABLE", [1, 2], False))
        self.assertEqual(got["original"], "PresentMon output does not exist: X", "the input report is never mutated")

    def test_a_report_with_nothing_to_add_comes_back_as_the_same_object(self) -> None:
        got = self.call(
            "$report = [pscustomobject]@{ status = 'DISPLAY_ASLEEP'; reason = 'r' }\n"
            "$e = [pscustomobject]@{ detected = $false; detail = $null }\n"
            "$r = Add-AttrCudaPresentMonEventsLostDetailToReport -Report $report -EventsLost $e\n"
            "Write-Output ('JSON=' + (ConvertTo-Json -InputObject ([ordered]@{ same = [object]::ReferenceEquals($r, $report) }) -Compress))")
        self.assertTrue(got["same"])

    def global_logman(self, body: str) -> str:
        return "function global:logman { " + body + " }\n"

    def test_the_listing_wrapper_returns_the_text_and_the_exit_code_of_logman_query(self) -> None:
        got = self.call(self.global_logman("$global:LASTEXITCODE = 0; if ($args[0] -eq 'query' -and $args[1] -eq '-ets') { 'PresentMon   Trace   Running' }")
                        + "$r = Get-AttrCudaEtsSessionListing\nWrite-Output ('JSON=' + ($r | ConvertTo-Json -Compress))")
        self.assertEqual((got["exitCode"], got["error"]), (0, None))
        self.assertIn("PresentMon   Trace   Running", got["text"])

    def test_the_listing_wrapper_reports_a_failing_logman_without_throwing(self) -> None:
        got = self.call(self.global_logman("$global:LASTEXITCODE = 5; 'ERROR' ") + "$r = Get-AttrCudaEtsSessionListing\nWrite-Output ('JSON=' + ($r | ConvertTo-Json -Compress))")
        self.assertEqual(got["exitCode"], 5)

    def test_the_stop_wrapper_stops_only_this_harnesss_own_session_names(self) -> None:
        marker = self.tmp / "stops.log"
        stub = self.global_logman(f"if ($args[0] -eq 'stop') {{ Add-Content -LiteralPath '{ps(marker)}' -Value ($args[1] + ' ' + $args[2]); $global:LASTEXITCODE = 0 }} else {{ $global:LASTEXITCODE = 9 }}")
        got = self.call(stub +
                        "$all = foreach ($n in @('PresentMon', 'presentmon', 'MLVAttr3-job-1', 'NT Kernel Logger', 'PresentMon_other', 'MLVAttr3-', 'EventLog-System', 'x -ets; y', 'MyMLVAttr3-x')) { [ordered]@{ name = $n; result = (Stop-AttrCudaEtsSession -SessionName $n) } }\n"
                        "Write-Output ('JSON=' + (ConvertTo-Json -InputObject @($all) -Depth 4 -Compress))")
        by_name = {row["name"]: row["result"] for row in got}
        for ok in ("PresentMon", "presentmon", "MLVAttr3-job-1"):
            self.assertEqual((by_name[ok]["exitCode"], by_name[ok]["error"]), (0, None), ok)
        for refused in ("NT Kernel Logger", "PresentMon_other", "MLVAttr3-", "EventLog-System", "x -ets; y", "MyMLVAttr3-x"):
            self.assertIsNone(by_name[refused]["exitCode"], refused)
            self.assertIn("refused", by_name[refused]["error"], refused)
        self.assertEqual(sorted(marker.read_text(encoding="utf-8").split("\n")[:-1]), ["MLVAttr3-job-1 -ets", "PresentMon -ets", "presentmon -ets"])

    def test_the_detail_is_never_added_twice(self) -> None:
        once = self.with_detail("r", {"detected": True})
        again = run_pwsh(["-Command", f"$ErrorActionPreference = 'Stop'\nImport-Module '{ps(MODULE)}' -Force\n"
                          f"$e = [pscustomobject]@{{ detected = $true; detail = 'PRESENTMON_EVENTS_LOST: 12 lost' }}\n"
                          f"Write-Output ('OUT=' + (Add-AttrCudaPresentMonEventsLostDetail -Reason '{once}' -EventsLost $e))"])
        self.assertEqual(next(l for l in again.stdout.splitlines() if l.startswith("OUT="))[4:], once)


# --------------------------------------------------------------------------------------------------------------------- the sweep in Start-PresentMonCapture
@requires_windows_pwsh
class SweepExecutedTests(SliceHarness, unittest.TestCase):
    """Item 1: the REAL Start-PresentMonCapture and Invoke-PresentMonOrphanSweep, run against a .cmd PresentMon (it logs its argv and, for --terminate_existing_session, records
    the termination), a Get-Process stub (the alive PresentMon pids) and a logman stub (a session list that the terminations edit)."""

    def setUp(self) -> None:
        self.make_slice_dir()

    def run_start(self, name: str, sessions: tuple[str, ...] = ("PresentMon", "MLVAttr3-old-killed-job", "Circular Kernel Context Logger"), live_pids: tuple[int, ...] = (),
                  helper_works: bool = True, logman_stop_works: bool = True, enabled: bool = True, get_process_throws: bool = False, logman_query_fails: bool = False, live_from_second_check: bool = False,
                  text: str | None = None) -> tuple[int, dict, Path, str]:
        d = self.stmp / name
        (d / "cache").mkdir(parents=True)
        (d / "legOut").mkdir()
        (d / "sessions.txt").write_text("\n".join(sessions) + "\n", encoding="utf-8")
        (d / "terminated.txt").write_text("", encoding="utf-8")
        (d / "stopped.txt").write_text("", encoding="utf-8")
        (d / "calls.log").write_text("", encoding="utf-8")
        record = f"echo %2>>\"{d / 'terminated.txt'}\"\r\n" if helper_works else ""
        (d / "cache" / STUB_EXE).write_text(
            "@echo off\r\n"
            f"echo %*>>\"{d / 'calls.log'}\"\r\n"
            "echo %* | findstr /c:\"--terminate_existing_session\" >nul\r\n"
            "if errorlevel 1 exit /b 0\r\n"
            + record + ("exit /b 0\r\n" if helper_works else "exit /b 1\r\n"), encoding="ascii")
        body = text if text is not None else TEMPLATE
        pids = ", ".join(str(p) for p in live_pids)
        script = d / "probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{ps(MODULE)}' -Force\n"
            "function Test-AttrCudaPresentMonTraceReady($Proc) { [pscustomobject]@{ ready = $true; detail = 'stub' } }\n"
            f"$traceFile = '{ps(d / 'trace.log')}'\n"
            "function Write-JobTrace([string]$Message) { Add-Content -LiteralPath $traceFile -Value $Message }\n"
            "function Get-Process {\n"
            "    [CmdletBinding()] param([string[]]$Name)\n"
            "    $global:getProcessCalls = 1 + [int]$global:getProcessCalls\n"
            + ("    throw 'stub Get-Process failure'\n" if get_process_throws else
               ("    if ($global:getProcessCalls -lt 2) { return }\n" if live_from_second_check else "")
               + f"    foreach ($p in @({pids})) {{ [pscustomobject]@{{ Id = [int]$p; Name = 'PresentMon-2.5.1-x64' }} }}\n")
            + "}\n"
            # global: the module's logman wrappers resolve `logman` from the module scope, which sees the global scope but not this script's
            "function global:logman {\n"
            f"    $d = '{ps(d)}'\n"
            "    if ($args[0] -eq 'query') {\n"
            + ("        $global:LASTEXITCODE = 1; return 'ERROR: stub logman query failure'\n" if logman_query_fails else
               "        $gone = @(Get-Content -LiteralPath (Join-Path $d 'terminated.txt')) + @(Get-Content -LiteralPath (Join-Path $d 'stopped.txt'))\n"
               "        $live = @(Get-Content -LiteralPath (Join-Path $d 'sessions.txt') | Where-Object { $_ -and ($gone -notcontains $_.Trim()) })\n"
               f"        $text = \"{LOGMAN_HEADER.replace(chr(10), '`n')}\"\n"
               "        foreach ($n in $live) { $text += ($n.PadRight(40) + \"Trace                         Running`n\") }\n"
               "        $global:LASTEXITCODE = 0; return ($text + 'The command completed successfully.')\n")
            + "    }\n"
            "    if ($args[0] -eq 'stop') {\n"
            + ("        Add-Content -LiteralPath (Join-Path $d 'stopped.txt') -Value $args[1]; $global:LASTEXITCODE = 0\n" if logman_stop_works else
               "        $global:LASTEXITCODE = 1; return 'ERROR: stub logman stop failure'\n")
            + "    }\n"
            "}\n"
            f"$Cache = '{ps(d / 'cache')}'\n$PresentMonName = '{STUB_EXE}'\n$ExeName = 'MLVApp.exe'\n$PresentMonTimedSeconds = 60\n"
            "$PresentMonSessionName = 'MLVAttr3-the-new-job'\n$PresentMonTerminateOnProcExit = $true\n"
            f"$legOut = '{ps(d / 'legOut')}'\n"
            "$presentMonPath = Join-Path $legOut 'presentmon.csv'\n"
            "$presentMonStdoutPath = Join-Path $legOut 'presentmon-stdout.txt'\n$presentMonStderrPath = Join-Path $legOut 'presentmon-stderr.txt'\n"
            "$presentMonTraceReadiness = [ordered]@{}\n$presentMonStreams = [ordered]@{ csvSeenDuringReadiness = $false }\n"
            + ("$presentMonOrphanSweep = [ordered]@{}\n" if enabled else "")
            + presentmon_functions(body) + "\n"
            "$proc = Start-PresentMonCapture $presentMonPath\n"
            "[void]$proc.WaitForExit(20000)\n"
            "if ($null -ne $presentMonOrphanSweep) { Write-Output ('SWEEP=' + ($presentMonOrphanSweep | ConvertTo-Json -Depth 6 -Compress)) }\n", encoding="utf-8")
        proc = run_pwsh(["-File", str(script)])
        sweep: dict = {}
        for line in proc.stdout.splitlines():
            if line.startswith("SWEEP="):
                sweep = json.loads(line[len("SWEEP="):])
        return proc.returncode, sweep, d, proc.stdout + proc.stderr

    @staticmethod
    def calls(d: Path) -> list[str]:
        return [l.strip() for l in (d / "calls.log").read_text(encoding="utf-8").splitlines() if l.strip()]

    @staticmethod
    def terminated_names(calls: list[str]) -> list[str]:
        return [m.group(1) for c in calls if (m := re.search(r"--session_name (\S+) --terminate_existing_session", c))]

    def test_orphans_with_no_presentmon_process_alive_are_swept_before_the_spawn(self) -> None:
        code, sweep, d, out = self.run_start("swept")
        self.assertEqual(code, 0, out)
        calls = self.calls(d)
        self.assertEqual(self.terminated_names(calls), ["PresentMon", "MLVAttr3-old-killed-job"], "the default session and the leftover job session, never the unrelated kernel logger")
        spawn = [i for i, c in enumerate(calls) if "--process_name" in c]
        self.assertEqual(len(spawn), 1)
        self.assertTrue(all(i < spawn[0] for i, c in enumerate(calls) if "--terminate_existing_session" in c), "every sweep action precedes the spawn")
        self.assertTrue(sweep["ran"])
        self.assertIsNone(sweep["skippedReason"])
        self.assertEqual(sweep["listed"], ["PresentMon", "MLVAttr3-old-killed-job"])
        self.assertEqual([(a["session"], a["method"], a["exitCode"]) for a in sweep["actions"]], [("PresentMon", "presentmon_terminate", 0), ("MLVAttr3-old-killed-job", "presentmon_terminate", 0)])
        self.assertEqual(sweep["remaining"], [])
        self.assertIsNone(sweep["error"])
        trace = (d / "trace.log").read_text(encoding="utf-8")
        self.assertIn("step presentmon-orphan-sweep", trace)
        self.assertIn("PresentMon", trace)

    def test_a_live_presentmon_process_is_never_swept(self) -> None:
        code, sweep, d, out = self.run_start("live", live_pids=(4242,))
        self.assertEqual(code, 0, out)
        calls = self.calls(d)
        self.assertEqual(self.terminated_names(calls), [], "another capture may own a session: nothing is terminated")
        self.assertEqual(len([c for c in calls if "--process_name" in c]), 1, "the capture itself still starts")
        self.assertEqual((d / "stopped.txt").read_text(encoding="utf-8").strip(), "", "and logman stop is not used either")
        self.assertFalse(sweep["ran"])
        self.assertEqual(sweep["skippedReason"], "presentmon_process_alive")
        self.assertEqual(sweep["livePresentMonProcessIds"], [4242])
        self.assertEqual(sweep["actions"], [])
        self.assertIn("presentmon_process_alive", (d / "trace.log").read_text(encoding="utf-8"))

    def test_a_presentmon_that_appears_while_the_listing_runs_is_never_swept(self) -> None:
        code, sweep, d, out = self.run_start("late-live", live_pids=(4242,), live_from_second_check=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.terminated_names(self.calls(d)), [])
        self.assertFalse(sweep["ran"])
        self.assertEqual(sweep["skippedReason"], "presentmon_process_started_during_sweep")
        self.assertEqual(sweep["livePresentMonProcessIds"], [4242])

    def test_a_host_with_no_orphan_runs_the_sweep_and_touches_nothing(self) -> None:
        code, sweep, d, out = self.run_start("clean", sessions=("Circular Kernel Context Logger",))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.terminated_names(self.calls(d)), [])
        self.assertTrue(sweep["ran"])
        self.assertEqual((sweep["listed"], sweep["actions"], sweep["remaining"]), ([], [], []))

    def test_logman_stop_runs_only_for_a_session_still_listed_after_the_presentmon_terminate(self) -> None:
        code, sweep, d, out = self.run_start("fallback", helper_works=False, logman_stop_works=True)
        self.assertEqual(code, 0, out)
        stopped = [l.strip() for l in (d / "stopped.txt").read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertEqual(stopped, ["PresentMon", "MLVAttr3-old-killed-job"])
        methods = [(a["session"], a["method"], a["exitCode"]) for a in sweep["actions"]]
        self.assertEqual(methods, [("PresentMon", "presentmon_terminate", 1), ("MLVAttr3-old-killed-job", "presentmon_terminate", 1),
                                   ("PresentMon", "logman_stop", 0), ("MLVAttr3-old-killed-job", "logman_stop", 0)])
        self.assertEqual(sweep["remaining"], [])

    def test_a_successful_terminate_never_needs_logman_stop(self) -> None:
        _code, sweep, d, _out = self.run_start("no-fallback")
        self.assertEqual((d / "stopped.txt").read_text(encoding="utf-8").strip(), "")
        self.assertNotIn("logman_stop", [a["method"] for a in sweep["actions"]])

    def test_a_session_that_survives_both_is_recorded_as_remaining_and_the_capture_still_starts(self) -> None:
        code, sweep, d, out = self.run_start("stubborn", helper_works=False, logman_stop_works=False)
        self.assertEqual(code, 0, out)
        self.assertEqual(sweep["remaining"], ["PresentMon", "MLVAttr3-old-killed-job"], "an orphan that could not be removed is evidence, not silence")
        self.assertEqual(len([c for c in self.calls(d) if "--process_name" in c]), 1)
        self.assertIn("remaining", (d / "trace.log").read_text(encoding="utf-8"))

    def test_a_sweep_that_fails_is_recorded_and_never_blocks_the_capture(self) -> None:
        code, sweep, d, out = self.run_start("sweep-throws", get_process_throws=True)
        self.assertEqual(code, 0, out)
        self.assertIn("stub Get-Process failure", sweep["error"])
        self.assertEqual(len([c for c in self.calls(d) if "--process_name" in c]), 1)

    def test_a_logman_listing_that_fails_is_recorded_and_terminates_nothing(self) -> None:
        code, sweep, d, out = self.run_start("query-fails", logman_query_fails=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.terminated_names(self.calls(d)), [])
        self.assertTrue(sweep["error"] or sweep["listError"], sweep)
        self.assertEqual(len([c for c in self.calls(d) if "--process_name" in c]), 1)

    def test_without_the_callers_record_table_nothing_is_swept(self) -> None:
        # the table is the opt-in: the other suites that execute Start-PresentMonCapture never reach a real logman or session
        code, _sweep, d, out = self.run_start("not-enabled", enabled=False)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.terminated_names(self.calls(d)), [])
        self.assertEqual((d / "stopped.txt").read_text(encoding="utf-8").strip(), "")

    def test_the_job_creates_the_record_table_before_it_starts_the_capture(self) -> None:
        init = TEMPLATE.index("$presentMonOrphanSweep = [ordered]@{")
        self.assertLess(init, TEMPLATE.index("$presentMonProc = Start-PresentMonCapture $presentMonPath"))

    def test_mutation_without_the_sweep_the_orphan_survives_again(self) -> None:
        mutated = mutate(TEMPLATE, "$sweepResult = Invoke-PresentMonOrphanSweep", "$sweepResult = [ordered]@{ ran = $false }")
        code, _sweep, d, out = self.run_start("mut-nosweep", text=mutated)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.terminated_names(self.calls(d)), [], "the UM state of 2026-10-03: the orphan stays and starves the next capture")

    def test_mutation_without_the_live_process_guard_a_live_capture_is_swept(self) -> None:
        # two guards (before the listing, and again right after it): each mutation alone leaves the other standing, so both are removed together and then each singly
        both = mutate(mutate(TEMPLATE, "if ($live.Count -gt 0) {", "if ($false) {"), "if ($liveNow.Count -gt 0) {", "if ($false) {")
        _code, _sweep, d, _out = self.run_start("mut-guard", live_pids=(4242,), text=both)
        self.assertNotEqual(self.terminated_names(self.calls(d)), [], "the mutation sweeps under a live PresentMon -- the guards are what stop it")
        first_only = mutate(TEMPLATE, "if ($live.Count -gt 0) {", "if ($false) {")
        _code, sweep, d2, _out = self.run_start("mut-guard-first", live_pids=(4242,), live_from_second_check=False, text=first_only)
        self.assertEqual(self.terminated_names(self.calls(d2)), [], "the second guard still holds when the first is gone")
        self.assertEqual(sweep["skippedReason"], "presentmon_process_started_during_sweep")
        second_only = mutate(TEMPLATE, "if ($liveNow.Count -gt 0) {", "if ($false) {")
        _code, _sweep, d3, _out = self.run_start("mut-guard-second", live_pids=(4242,), live_from_second_check=True, text=second_only)
        self.assertNotEqual(self.terminated_names(self.calls(d3)), [], "without the second guard a capture that starts during the listing is swept")

    def test_mutation_without_the_record_the_sweep_leaves_no_evidence(self) -> None:
        mutated = mutate(TEMPLATE, "$presentMonOrphanSweep[$key] = $sweepResult[$key]", "$null = $key")
        _code, sweep, _d, _out = self.run_start("mut-record", text=mutated)
        self.assertEqual(sweep, {})

    def test_mutation_without_the_logman_fallback_a_stubborn_session_is_never_stopped(self) -> None:
        mutated = mutate(TEMPLATE, "$stopResult = Stop-AttrCudaEtsSession -SessionName $name", "$stopResult = [pscustomobject]@{ exitCode = 1; error = $null }")
        _code, sweep, d, _out = self.run_start("mut-fallback", helper_works=False, text=mutated)
        self.assertEqual((d / "stopped.txt").read_text(encoding="utf-8").strip(), "")
        self.assertEqual(sweep["remaining"], ["PresentMon", "MLVAttr3-old-killed-job"])


# --------------------------------------------------------------------------------------------------------------------- kill_fallback
@requires_windows_pwsh
class KillFallbackTerminatesOwnSessionTests(SliceHarness, unittest.TestCase):
    """Item 2: after a job-issued Kill() the job's own named session is terminated, in Wait-PresentMonCapture and in Stop-PresentMonCapture."""

    def setUp(self) -> None:
        self.make_slice_dir()

    def run_stop(self, name: str, call: str, terminate_ends_process: bool, text: str | None = None) -> tuple[int, dict, list[str], str]:
        d = self.stmp / name
        d.mkdir(parents=True)
        calls = d / "calls.log"
        calls.write_text("", encoding="utf-8")
        body = text if text is not None else TEMPLATE
        ends = "Stop-Process -Id $global:capturePid -Force -ErrorAction SilentlyContinue; Start-Sleep -Milliseconds 300; " if terminate_ends_process else ""
        script = d / "probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{ps(MODULE)}' -Force\n"
            "$Cache = 'C:\\no-such-cache'\n$PresentMonName = 'PresentMon-2.5.1-x64.exe'\n"
            + presentmon_functions(body) + "\n"
            f"$callsFile = '{ps(calls)}'\n"
            "function Invoke-PresentMonSessionTerminate([string]$SessionName, [int]$TimeoutSeconds = 10) {\n"
            "    Add-Content -LiteralPath $callsFile -Value \"terminate $SessionName\"\n"
            f"    {ends}[pscustomobject]@{{ exitCode = {'0' if terminate_ends_process else '1'}; timedOut = $false; error = $null }}\n"
            "}\n"
            "$p = Start-Process -FilePath 'ping.exe' -ArgumentList @('-n', '120', '127.0.0.1') -PassThru -WindowStyle Hidden\n"
            "$global:capturePid = $p.Id\n"
            "try {\n"
            f"    $r = {call}\n"
            "    Write-Output ('RESULT=' + ($r | ConvertTo-Json -Depth 6 -Compress))\n"
            "} finally { if (-not $p.HasExited) { $p.Kill() } }\n", encoding="utf-8")
        proc = run_pwsh(["-File", str(script)], timeout=180)
        result: dict = {}
        for line in proc.stdout.splitlines():
            if line.startswith("RESULT="):
                result = json.loads(line[len("RESULT="):])
        return proc.returncode, result, [l.strip() for l in calls.read_text(encoding="utf-8").splitlines() if l.strip()], proc.stdout + proc.stderr

    WAIT_CALL = "Wait-PresentMonCapture $p -SessionName 'MLVAttr3-the-job' -TimeoutSeconds 1 -KillWaitTimeoutSeconds 10"
    STOP_CALL = "Stop-PresentMonCapture $p -TimeoutSeconds 10 -SessionName 'MLVAttr3-the-job'"

    def test_wait_terminates_the_own_session_again_after_its_kill_fallback(self) -> None:
        code, result, calls, out = self.run_stop("wait-kill", self.WAIT_CALL, terminate_ends_process=False)
        self.assertEqual(code, 0, out)
        self.assertEqual(result["stopMethod"], "kill_fallback")
        self.assertEqual(calls, ["terminate MLVAttr3-the-job", "terminate MLVAttr3-the-job"], "one terminate before the Kill(), one after it")
        self.assertTrue(result["postKillTerminateIssued"])
        self.assertEqual(result["postKillTerminateExitCode"], 1)
        self.assertFalse(result["postKillTerminateTimedOut"])

    def test_wait_without_a_kill_issues_no_second_terminate(self) -> None:
        code, result, calls, out = self.run_stop("wait-clean", self.WAIT_CALL, terminate_ends_process=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(result["stopMethod"], "session_terminate")
        self.assertEqual(calls, ["terminate MLVAttr3-the-job"])
        self.assertFalse(result["postKillTerminateIssued"])
        self.assertIsNone(result["postKillTerminateExitCode"])

    def test_stop_terminates_the_own_session_again_after_its_kill(self) -> None:
        code, result, calls, out = self.run_stop("stop-kill", self.STOP_CALL, terminate_ends_process=False)
        self.assertEqual(code, 0, out)
        self.assertTrue(result["confirmedExited"])
        self.assertEqual(calls, ["terminate MLVAttr3-the-job", "terminate MLVAttr3-the-job"])
        self.assertEqual(result["postKillSessionTerminate"]["exitCode"], 1)

    def test_stop_without_a_kill_issues_no_second_terminate(self) -> None:
        code, result, calls, out = self.run_stop("stop-clean", self.STOP_CALL, terminate_ends_process=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(calls, ["terminate MLVAttr3-the-job"])
        self.assertIsNone(result["postKillSessionTerminate"])

    def test_mutation_without_the_wait_post_kill_terminate_the_session_orphans_again(self) -> None:
        mutated = mutate(TEMPLATE, "$postKillTerminate = Invoke-PresentMonSessionTerminate -SessionName $SessionName -TimeoutSeconds 5", "$postKillTerminate = $null")
        _code, _result, calls, _out = self.run_stop("mut-wait", self.WAIT_CALL, terminate_ends_process=False, text=mutated)
        self.assertEqual(calls, ["terminate MLVAttr3-the-job"], "the inferred UM state: a killed capture's own session stays")

    def test_mutation_without_the_stop_post_kill_terminate_the_session_orphans_again(self) -> None:
        mutated = mutate(TEMPLATE, "$postKillSessionTerminate = Invoke-PresentMonSessionTerminate -SessionName $SessionName -TimeoutSeconds 5", "$postKillSessionTerminate = $null")
        _code, _result, calls, _out = self.run_stop("mut-stop", self.STOP_CALL, terminate_ends_process=False, text=mutated)
        self.assertEqual(calls, ["terminate MLVAttr3-the-job"])


# --------------------------------------------------------------------------------------------------------------------- typed evidence in the job
@requires_windows_pwsh
class EventsLostEvidenceInTheJobTests(_StopHarness, unittest.TestCase):
    """Item 3, in the job: PresentMon's stderr saying events were lost is a typed detail in presentmon-capture.json and in the failure terminals' PresentMon reason."""

    def setUp(self) -> None:
        self.make_slice_dir()

    def run_parse_with_events(self, name: str, events_lost_stderr: bytes | None = None, text: str | None = None) -> tuple[int, dict, Path, str]:
        """The real display-report failure branch against the UM-2 shape, after the evidence step: $presentMonEventsLost is what the job's own scan produces from the stderr."""
        d = self.stmp / name
        pub = d / "pub"
        pub.mkdir(parents=True)
        cs = d / "contact-sheet"
        cs.mkdir()
        for fname, data in SIX_FRAMES.items():
            (cs / fname).write_bytes(data)
        (d / "run.log").write_text(um_run_log(UM2_GPU_SUMMARY), encoding="utf-8")
        stderr = d / "presentmon-stderr.txt"
        if events_lost_stderr is not None:
            stderr.write_bytes(events_lost_stderr)
        body = text if text is not None else TEMPLATE
        script = d / "probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{ps(MODULE)}' -Force\n"
            + gpu_summary_fn(body) + "\n"
            f"$Pub = '{ps(pub)}'\n$FixtureRehearsal = $false\n$SourceCommit = ('1' * 40)\n$ClipId = '{OWNER_CLIP}'\n"
            "function Save-Json($Object, [string]$Path) { [void](Publish-AttrCudaText -Path $Path -Value ($Object | ConvertTo-Json -Depth 30)) }\n"
            "function Write-JobTrace([string]$Message) { }\nfunction Get-AttrCudaDisplayResultTail([object]$DisplayBlock) { '' }\n"
            f"$rawLog = [IO.File]::ReadAllText('{ps(d / 'run.log')}')\n$measuredSmokeSessionId = '1'\n"
            "$gpuSummary = Get-LastGpuSummary $rawLog $measuredSmokeSessionId\n"
            "$gpuFramesTotal = $gpuSummary.gpuReconReadbackFrames + $gpuSummary.gpuTextureReadbackFrames + $gpuSummary.gpuTextureNoReadbackFrames\n"
            f"$presentMonEventsLost = Get-AttrCudaPresentMonEventsLost -StderrPath '{ps(stderr)}'\n"
            f"$displayReport = [pscustomobject]@{{ status = 'PRESENTMON_UNAVAILABLE'; reason = '{UM2_REASON.replace(chr(39), chr(39) * 2)}'; chains = @(); clockBracket = $null }}\n"
            "$displayAsleepOverridden = $false\n$presentMonCaptureStartUtc = [datetime]::UtcNow\n$displayWake = [ordered]@{}\n$displayBlock = [ordered]@{ venue = 'ultra-magnus' }\n"
            "$diagnostics = [ordered]@{}\n$rows = @(1..3)\n$stats = [ordered]@{}\n"
            f"$ContactSheetEnabled = $true\n$contactSheetDir = '{ps(cs)}'\n"
            + _slice(body, PARSE_START, PARSE_END) + "\nWrite-Output 'RESULT=NO_FAILURE_BRANCH_TAKEN'\n", encoding="utf-8")
        proc = run_pwsh(["-File", str(script)])
        summary_path = pub / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
        return proc.returncode, summary, pub, proc.stdout + proc.stderr

    def test_a_wait_failure_carries_the_typed_detail_in_the_capture_json_and_the_reason(self) -> None:
        code, summary, pub, out = self.run_stop("lost-wait", "throw", csv=None, streams=(b"", LOST_LINE.encode("ascii")))
        self.assertEqual(code, 23, out)
        capture = json.loads((pub / "presentmon-capture.json").read_text(encoding="utf-8"))
        self.assertTrue(capture["eventsLost"]["detected"])
        self.assertEqual(capture["eventsLost"]["maxReported"], 15318)
        self.assertIn("PRESENTMON_EVENTS_LOST", summary["reason"])
        self.assertIn("PRESENTMON_TIMEOUT", summary["reason"], "the original reason is kept, the detail is added")
        self.assertIn("PRESENTMON_EVENTS_LOST", out, "and so is the RESULT line")

    def test_a_clean_wait_failure_carries_no_detail(self) -> None:
        code, summary, pub, out = self.run_stop("clean-wait", "throw", csv=None, streams=(b"", b"Started recording.\n"))
        self.assertEqual(code, 23, out)
        self.assertFalse(json.loads((pub / "presentmon-capture.json").read_text(encoding="utf-8"))["eventsLost"]["detected"])
        self.assertNotIn("PRESENTMON_EVENTS_LOST", summary["reason"])

    def test_the_display_report_failure_reads_as_the_cause_not_as_output_does_not_exist(self) -> None:
        code, summary, _pub, out = self.run_parse_with_events("lost-parse", events_lost_stderr=LOST_LINE.encode("ascii"))
        self.assertEqual(code, 23, out)
        self.assertTrue(summary["reason"].startswith(UM2_REASON), summary["reason"])
        self.assertIn("PRESENTMON_EVENTS_LOST", summary["reason"])
        self.assertIn("PRESENTMON_EVENTS_LOST", out)

    def test_a_display_report_failure_without_a_loss_keeps_its_reason_verbatim(self) -> None:
        code, summary, _pub, out = self.run_parse_with_events("clean-parse")
        self.assertEqual(code, 23, out)
        self.assertEqual(summary["reason"], UM2_REASON)

    def test_both_capture_json_statements_carry_the_orphan_sweep_and_the_events_lost_record(self) -> None:
        self.assertEqual(TEMPLATE.count("orphanSweep=$presentMonOrphanSweep"), 2)
        self.assertEqual(TEMPLATE.count("eventsLost=$presentMonEventsLost"), 2)

    def test_the_events_lost_scan_runs_after_the_stop_and_before_the_terminals(self) -> None:
        block = wait_block()
        self.assertLess(block.index("Publish-AttrCudaPresentMonCaptureEvidence"), block.index("$presentMonEventsLost = Get-AttrCudaPresentMonEventsLost"))
        self.assertLess(block.index("$presentMonEventsLost = Get-AttrCudaPresentMonEventsLost"), block.rindex("exit 23"))

    def test_mutation_without_the_wait_failure_detail_the_reason_is_bare_again(self) -> None:
        mutated = mutate(TEMPLATE, "$presentMonWaitError = Add-AttrCudaPresentMonEventsLostDetail -Reason $presentMonWaitError -EventsLost $presentMonEventsLost", "$null = $presentMonEventsLost")
        _code, summary, _pub, _out = self.run_stop("mut-wait-detail", "throw", csv=None, streams=(b"", LOST_LINE.encode("ascii")), text=mutated)
        self.assertNotIn("PRESENTMON_EVENTS_LOST", summary["reason"])

    def test_mutation_without_the_display_report_detail_the_reason_is_bare_again(self) -> None:
        mutated = mutate(TEMPLATE, "$displayReport = Add-AttrCudaPresentMonEventsLostDetailToReport -Report $displayReport -EventsLost $presentMonEventsLost", "$null = $presentMonEventsLost")
        _code, summary, _pub, _out = self.run_parse_with_events("mut-parse-detail", events_lost_stderr=LOST_LINE.encode("ascii"), text=mutated)
        self.assertNotIn("PRESENTMON_EVENTS_LOST", summary["reason"])

    def test_mutation_without_the_scan_the_capture_json_has_no_record(self) -> None:
        mutated = mutate(TEMPLATE, "$presentMonEventsLost = Get-AttrCudaPresentMonEventsLost -StderrPath $presentMonStderrPath", "$presentMonEventsLost = $null")
        _code, _summary, pub, _out = self.run_stop("mut-scan", "throw", csv=None, streams=(b"", LOST_LINE.encode("ascii")), text=mutated)
        self.assertIsNone(json.loads((pub / "presentmon-capture.json").read_text(encoding="utf-8"))["eventsLost"])


# --------------------------------------------------------------------------------------------------------------------- static pins
class StaticTests(unittest.TestCase):
    def test_every_new_module_function_is_exported_and_spliced_into_the_job_inside_its_bracket(self) -> None:
        generator = lf(GENERATOR.read_text(encoding="utf-8"))
        module = MODULE.read_text(encoding="utf-8")
        for name in NEW_MODULE_FUNCTIONS:
            self.assertIn(f"function {name} {{", module)
            self.assertIn(f"'{name}'", generator, "named in the generator's embed list")
        export = module[module.index("Export-ModuleMember -Function"):]
        for name in NEW_MODULE_FUNCTIONS:
            self.assertIn(name, export)

    def test_the_template_text_added_by_the_card_is_bracketed(self) -> None:
        opens = TEMPLATE.count(OPEN)
        closes = TEMPLATE.count(CLOSE)
        self.assertEqual(opens, closes)
        self.assertGreater(opens, 0)

    def test_the_sweep_function_never_uses_add_type_or_a_static_the_write_scan_does_not_list(self) -> None:
        sweep = _slice(TEMPLATE, "function Invoke-PresentMonOrphanSweep(", "\nfunction Split-PresentMonCsvLine(")
        self.assertNotIn("Add-Type", sweep)
        self.assertNotIn("::", sweep)


if __name__ == "__main__":
    unittest.main()
