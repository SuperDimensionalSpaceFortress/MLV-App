"""Behavioural tests for UM-DISPLAY-SELECT-AND-LOG-1 round 2b's venue-quiescence gate.

WHY. A hub probe on Ultra-Magnus (2026-09-26T15:51Z, same ~26s window) read
Win32_Processor.LoadPercentage at 73/82/83 while \\Processor(_Total)\\% Processor Time read
21.4/43.2/37 on the same i9-13900KS -- LoadPercentage is frequency-scaled processor UTILITY
(turbo inflates it), not busy TIME, so gating playback-attr-3-cuda-job.ps1's venue check on it
refused three legs at 58-83% while actual busy time was ~15-40%. Get-AttrCudaQuiescenceSample,
Get-AttrCudaProcessCpuSnapshot and Get-AttrCudaTopCpuProcesses (tools/profiling/bachelor/
AttrCudaArtifacts.psm1) are the fix: gate on the TIME counter, record both metrics plus the top
CPU-seconds consumers as evidence, and treat an unreadable counter as a third, refusing state.

These tests EXECUTE the real module functions (Import-Module, not a reimplementation), the same
functions playback-attr-3-cuda-job.ps1 splices verbatim into every generated job via
Get-AttrCudaEmbeddedFunctionSource -- see test_playback_attr_3_cuda_behaviour.py's own header for
that precedent. Get-CimInstance/Get-Counter/Get-Process are overridden per test so a real host's
current CPU load never makes a test flaky.
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

PWSH = shutil.which("pwsh")
requires_pwsh = unittest.skipIf(PWSH is None, "pwsh is not on PATH")


class _ProbeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="attr3-quiescence-")
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
class QuiescenceSampleTests(_ProbeCase):
    """Get-AttrCudaQuiescenceSample: both metrics, and a third (unknown) state for the counter."""

    def test_both_metrics_are_read_when_both_cmdlets_succeed(self) -> None:
        proc = self.run_snippet(
            "function Get-CimInstance { param($ClassName, $ErrorAction) "
            "[pscustomobject]@{ LoadPercentage = 82 } }\n"
            "function Get-Counter { param($Counter, $ErrorAction) "
            "[pscustomobject]@{ CounterSamples = @([pscustomobject]@{ CookedValue = 37.2; Status = 0 }) } }\n"
            "$s = Get-AttrCudaQuiescenceSample\n"
            "Write-Host \"UTILITY=$($s.utilityPercent) TIME=$($s.timePercent) ERROR=$($s.timePercentError)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("UTILITY=82", proc.stdout)
        self.assertIn("TIME=37.2", proc.stdout)
        self.assertIn("ERROR=", proc.stdout)
        error_line = next(l for l in proc.stdout.splitlines() if l.startswith("UTILITY="))
        self.assertTrue(error_line.endswith("ERROR="), "no error expected when the counter reads cleanly")

    def test_an_unreadable_time_counter_is_the_third_unknown_state_not_zero(self) -> None:
        # The exact defect this round exists to close: a counter read failure must never be
        # folded into "0% busy" (which would silently pass the gate) nor into the utility number.
        proc = self.run_snippet(
            "function Get-CimInstance { param($ClassName, $ErrorAction) "
            "[pscustomobject]@{ LoadPercentage = 10 } }\n"
            "function Get-Counter { param($Counter, $ErrorAction) "
            "throw [System.Management.Automation.RuntimeException]::new('counter set not found') }\n"
            "$s = Get-AttrCudaQuiescenceSample\n"
            "Write-Host \"UTILITY=$($s.utilityPercent) TIME=$(if ($null -eq $s.timePercent) { 'NULL' } else { $s.timePercent }) ERROR=$($s.timePercentError)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("UTILITY=10", proc.stdout)
        self.assertIn("TIME=NULL", proc.stdout)
        self.assertIn("ERROR=RuntimeException", proc.stdout)

    def test_an_unreadable_utility_cim_call_does_not_block_the_time_reading(self) -> None:
        proc = self.run_snippet(
            "function Get-CimInstance { param($ClassName, $ErrorAction) "
            "throw [System.InvalidOperationException]::new('CIM unavailable') }\n"
            "function Get-Counter { param($Counter, $ErrorAction) "
            "[pscustomobject]@{ CounterSamples = @([pscustomobject]@{ CookedValue = 15.0; Status = 0 }) } }\n"
            "$s = Get-AttrCudaQuiescenceSample\n"
            "Write-Host \"UTILITY=$(if ($null -eq $s.utilityPercent) { 'NULL' } else { $s.utilityPercent }) TIME=$($s.timePercent)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("UTILITY=NULL", proc.stdout)
        self.assertIn("TIME=15", proc.stdout)

    # UM-DISPLAY-SELECT-AND-LOG-1 round 1c (sol BLOCKER 4 / opus design-review hardening item 4):
    # a null/NaN/out-of-range CookedValue, or a nonzero-invalid Status, must be the same third
    # (unknown) state as a throwing Get-Counter -- never cast through to "0% busy" or left to
    # compare falsely against both -gt and -le.

    def test_a_null_cooked_value_is_unknown_not_zero_percent_busy(self) -> None:
        proc = self.run_snippet(
            "function Get-CimInstance { param($ClassName, $ErrorAction) [pscustomobject]@{ LoadPercentage = 10 } }\n"
            "function Get-Counter { param($Counter, $ErrorAction) "
            "[pscustomobject]@{ CounterSamples = @([pscustomobject]@{ CookedValue = $null; Status = 0 }) } }\n"
            "$s = Get-AttrCudaQuiescenceSample\n"
            "Write-Host \"TIME=$(if ($null -eq $s.timePercent) { 'NULL' } else { $s.timePercent }) ERROR=$($s.timePercentError)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("TIME=NULL", proc.stdout)
        self.assertIn("ERROR=InvalidOperationException", proc.stdout)

    def test_a_nan_cooked_value_is_unknown(self) -> None:
        proc = self.run_snippet(
            "function Get-CimInstance { param($ClassName, $ErrorAction) [pscustomobject]@{ LoadPercentage = 10 } }\n"
            "function Get-Counter { param($Counter, $ErrorAction) "
            "[pscustomobject]@{ CounterSamples = @([pscustomobject]@{ CookedValue = [double]::NaN; Status = 0 }) } }\n"
            "$s = Get-AttrCudaQuiescenceSample\n"
            "Write-Host \"TIME=$(if ($null -eq $s.timePercent) { 'NULL' } else { $s.timePercent }) ERROR=$($s.timePercentError)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("TIME=NULL", proc.stdout)
        self.assertIn("ERROR=InvalidOperationException", proc.stdout)

    def test_an_infinite_cooked_value_is_unknown(self) -> None:
        proc = self.run_snippet(
            "function Get-CimInstance { param($ClassName, $ErrorAction) [pscustomobject]@{ LoadPercentage = 10 } }\n"
            "function Get-Counter { param($Counter, $ErrorAction) "
            "[pscustomobject]@{ CounterSamples = @([pscustomobject]@{ CookedValue = [double]::PositiveInfinity; Status = 0 }) } }\n"
            "$s = Get-AttrCudaQuiescenceSample\n"
            "Write-Host \"TIME=$(if ($null -eq $s.timePercent) { 'NULL' } else { $s.timePercent })\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("TIME=NULL", proc.stdout)

    def test_an_out_of_range_cooked_value_is_unknown(self) -> None:
        proc = self.run_snippet(
            "function Get-CimInstance { param($ClassName, $ErrorAction) [pscustomobject]@{ LoadPercentage = 10 } }\n"
            "function Get-Counter { param($Counter, $ErrorAction) "
            "[pscustomobject]@{ CounterSamples = @([pscustomobject]@{ CookedValue = 101.0; Status = 0 }) } }\n"
            "$s = Get-AttrCudaQuiescenceSample\n"
            "Write-Host \"TIME=$(if ($null -eq $s.timePercent) { 'NULL' } else { $s.timePercent })\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("TIME=NULL", proc.stdout)

    def test_a_negative_cooked_value_is_unknown(self) -> None:
        proc = self.run_snippet(
            "function Get-CimInstance { param($ClassName, $ErrorAction) [pscustomobject]@{ LoadPercentage = 10 } }\n"
            "function Get-Counter { param($Counter, $ErrorAction) "
            "[pscustomobject]@{ CounterSamples = @([pscustomobject]@{ CookedValue = -1.0; Status = 0 }) } }\n"
            "$s = Get-AttrCudaQuiescenceSample\n"
            "Write-Host \"TIME=$(if ($null -eq $s.timePercent) { 'NULL' } else { $s.timePercent })\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("TIME=NULL", proc.stdout)

    def test_a_nonzero_invalid_status_is_unknown_even_with_a_plausible_cooked_value(self) -> None:
        proc = self.run_snippet(
            "function Get-CimInstance { param($ClassName, $ErrorAction) [pscustomobject]@{ LoadPercentage = 10 } }\n"
            "function Get-Counter { param($Counter, $ErrorAction) "
            "[pscustomobject]@{ CounterSamples = @([pscustomobject]@{ CookedValue = 15.0; Status = 0x800000BC }) } }\n"
            "$s = Get-AttrCudaQuiescenceSample\n"
            "Write-Host \"TIME=$(if ($null -eq $s.timePercent) { 'NULL' } else { $s.timePercent }) ERROR=$($s.timePercentError)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("TIME=NULL", proc.stdout)
        self.assertIn("ERROR=InvalidOperationException", proc.stdout)

    def test_an_absent_status_is_unknown_not_valid(self) -> None:
        # UM-DISPLAY-SELECT-AND-LOG-1 round 3 (sol hardening): a real PDH CounterSample always
        # carries Status; a sample with NO Status member cannot be vouched for, so it must be the
        # same third (unknown) state -- never accepted as valid. MUTATION CAUGHT: treating an
        # absent Status as valid again.
        proc = self.run_snippet(
            "function Get-CimInstance { param($ClassName, $ErrorAction) [pscustomobject]@{ LoadPercentage = 10 } }\n"
            "function Get-Counter { param($Counter, $ErrorAction) "
            "[pscustomobject]@{ CounterSamples = @([pscustomobject]@{ CookedValue = 15.0 }) } }\n"
            "$s = Get-AttrCudaQuiescenceSample\n"
            "Write-Host \"TIME=$(if ($null -eq $s.timePercent) { 'NULL' } else { $s.timePercent }) ERROR=$($s.timePercentError)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("TIME=NULL", proc.stdout)
        self.assertIn("ERROR=InvalidOperationException", proc.stdout)

    def test_status_0_and_status_1_are_both_accepted_as_valid(self) -> None:
        for status in (0, 1):
            with self.subTest(status=status):
                proc = self.run_snippet(
                    "function Get-CimInstance { param($ClassName, $ErrorAction) [pscustomobject]@{ LoadPercentage = 10 } }\n"
                    "function Get-Counter { param($Counter, $ErrorAction) "
                    f"[pscustomobject]@{{ CounterSamples = @([pscustomobject]@{{ CookedValue = 15.0; Status = {status} }}) }} }}\n"
                    "$s = Get-AttrCudaQuiescenceSample\n"
                    "Write-Host \"TIME=$($s.timePercent)\"\n"
                )
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn("TIME=15", proc.stdout)


@requires_pwsh
class TopCpuProcessesTests(_ProbeCase):
    """Get-AttrCudaTopCpuProcesses: ranking, missing-in-one-snapshot handling, VM annotation."""

    def test_ranks_by_cpu_seconds_delta_descending_and_caps_at_count(self) -> None:
        before = (
            "@("
            "[pscustomobject]@{ id=1; name='a'; cpuSeconds=0.0 }, "
            "[pscustomobject]@{ id=2; name='b'; cpuSeconds=0.0 }, "
            "[pscustomobject]@{ id=3; name='c'; cpuSeconds=0.0 }"
            ")"
        )
        after = (
            "@("
            "[pscustomobject]@{ id=1; name='a'; cpuSeconds=1.5 }, "
            "[pscustomobject]@{ id=2; name='b'; cpuSeconds=9.0 }, "
            "[pscustomobject]@{ id=3; name='c'; cpuSeconds=4.25 }"
            ")"
        )
        proc = self.run_snippet(
            f"$before = {before}\n$after = {after}\n"
            "$top = Get-AttrCudaTopCpuProcesses -Before $before -After $after -Count 2\n"
            "Write-Host \"COUNT=$($top.Count)\"\n"
            "$top | ForEach-Object { Write-Host \"ROW=$($_.name),$($_.cpuSeconds)\" }\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("COUNT=2", proc.stdout)
        rows = [l for l in proc.stdout.splitlines() if l.startswith("ROW=")]
        self.assertEqual(rows, ["ROW=b,9", "ROW=c,4.25"], "must be sorted descending and capped at -Count")

    def test_a_process_present_in_only_one_snapshot_contributes_nothing(self) -> None:
        before = "@([pscustomobject]@{ id=1; name='a'; cpuSeconds=0.0 })"
        after = (
            "@("
            "[pscustomobject]@{ id=1; name='a'; cpuSeconds=2.0 }, "
            "[pscustomobject]@{ id=99; name='started-mid-window'; cpuSeconds=50.0 }"
            ")"
        )
        proc = self.run_snippet(
            f"$before = {before}\n$after = {after}\n"
            "$top = Get-AttrCudaTopCpuProcesses -Before $before -After $after -Count 10\n"
            "Write-Host \"NAMES=$($top.name -join '|')\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("NAMES=a", proc.stdout)
        self.assertNotIn("started-mid-window", proc.stdout)

    def test_pid_reuse_across_the_window_does_not_merge_two_different_processes(self) -> None:
        # A negative delta (the "after" process at this Id is a DIFFERENT, shorter-lived process
        # than the "before" one) must be dropped, never reported as negative CPU usage.
        before = "@([pscustomobject]@{ id=7; name='old-owner'; cpuSeconds=100.0 })"
        after = "@([pscustomobject]@{ id=7; name='new-owner'; cpuSeconds=0.5 })"
        proc = self.run_snippet(
            f"$before = {before}\n$after = {after}\n"
            "$top = Get-AttrCudaTopCpuProcesses -Before $before -After $after -Count 10\n"
            "Write-Host \"COUNT=$($top.Count)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("COUNT=0", proc.stdout)

    def test_the_board_vm_process_is_annotated_case_insensitively(self) -> None:
        before = "@([pscustomobject]@{ id=1; name='VMware-VMX'; cpuSeconds=0.0 })"
        after = "@([pscustomobject]@{ id=1; name='VMware-VMX'; cpuSeconds=9.0 })"
        proc = self.run_snippet(
            f"$before = {before}\n$after = {after}\n"
            "$top = Get-AttrCudaTopCpuProcesses -Before $before -After $after -Count 10\n"
            "Write-Host \"NOTE=$($top[0].note)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("board VM", proc.stdout)

    def test_empty_snapshots_are_accepted_and_produce_no_rows(self) -> None:
        proc = self.run_snippet(
            "$top = Get-AttrCudaTopCpuProcesses -Before @() -After @() -Count 10\n"
            "Write-Host \"COUNT=$($top.Count)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("COUNT=0", proc.stdout)


@requires_pwsh
class ProcessCpuSnapshotTests(_ProbeCase):
    """Get-AttrCudaProcessCpuSnapshot: evidentiary collection, resilient to a throwing Get-Process."""

    def test_a_throwing_get_process_returns_an_empty_snapshot_not_an_error(self) -> None:
        proc = self.run_snippet(
            "function Get-Process { param($ErrorAction) throw [System.InvalidOperationException]::new('synthetic') }\n"
            "$s = Get-AttrCudaProcessCpuSnapshot\n"
            "Write-Host \"COUNT=$($s.Count)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("COUNT=0", proc.stdout)

    def test_real_snapshot_collects_at_least_this_process(self) -> None:
        proc = self.run_snippet(
            "$s = Get-AttrCudaProcessCpuSnapshot\n"
            "Write-Host \"COUNT=$($s.Count)\"\n"
            "Write-Host \"HAS_FIELDS=$($null -ne $s[0].id -and $null -ne $s[0].name -and $null -ne $s[0].cpuSeconds)\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        count_line = next(l for l in proc.stdout.splitlines() if l.startswith("COUNT="))
        self.assertGreater(int(count_line[len("COUNT="):]), 0)
        self.assertIn("HAS_FIELDS=True", proc.stdout)


@requires_pwsh
class JobTemplateGateWiringTests(unittest.TestCase):
    """Text-level pin on playback-attr-3-cuda-job.ps1's embedded template: the gate reads the TIME
    counter (mutating it back to a bare LoadPercentage comparison must red this), and both metrics
    plus the top-process list are written to summary.json on the refusal path."""

    def setUp(self) -> None:
        self.source = JOB_SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")
        start = self.source.index("$template = @'")
        end = self.source.index("\n'@", start)
        self.template = self.source[start:end]

    def test_gate_condition_compares_the_time_mean_not_a_bare_load_percentage(self) -> None:
        # UM-DISPLAY-SELECT-AND-LOG-1 round 1c (opus hardening item 4): written fail-closed as
        # "-not (<= threshold)" rather than "-gt threshold" -- a stray NaN compares false against
        # both operators, so only the negated -le form refuses it.
        self.assertIn(
            "if ($cpuTimeUnknown -or -not ($avgTime -le $cpuThresholdPercent)) {", self.template)
        self.assertNotIn("if ($cpuTimeUnknown -or $avgTime -gt $cpuThresholdPercent) {", self.template)
        # The old bare-utility gate must be gone, not merely supplemented.
        self.assertNotIn("if ($avgLoad -gt 20.0) {", self.template)

    def test_an_unreadable_time_counter_forces_refusal_not_a_pass(self) -> None:
        self.assertIn("$cpuTimeUnknown = $cpuTimeSamples.Count -lt 3", self.template)
        self.assertIn("$avgTime = if ($cpuTimeUnknown) { $null } else { Get-Mean $cpuTimeSamples }", self.template)

    def test_refusal_path_publishes_both_metrics_and_the_top_process_list(self) -> None:
        refusal_at = self.template.index("result='VENUE_NOT_QUIESCENT'")
        refusal_tail = self.template[refusal_at:refusal_at + 900]
        self.assertIn("cpuUtilitySamples=$cpuUtilitySamples", refusal_tail)
        self.assertIn("cpuUtilityMean=$avgUtility", refusal_tail)
        self.assertIn("cpuTimeSamples=$cpuTimeSamples", refusal_tail)
        self.assertIn("cpuTimeMean=$avgTime", refusal_tail)
        self.assertIn("cpuTimeUnknown=$cpuTimeUnknown", refusal_tail)
        self.assertIn("topCpuProcesses=$topCpuProcesses", refusal_tail)

    def test_pass_path_evidence_manifest_publishes_both_metrics_and_the_top_process_list(self) -> None:
        pass_at = self.template.index("cpuQuiescence = [ordered]@{")
        pass_tail = self.template[pass_at:pass_at + 500]
        self.assertIn("timeMeanPercent=$avgTime", pass_tail)
        self.assertIn("utilityMeanPercent=$avgUtility", pass_tail)
        self.assertIn("timeUnknown=$cpuTimeUnknown", pass_tail)
        self.assertIn("topCpuProcesses=$topCpuProcesses", pass_tail)

    def test_result_lines_on_both_paths_carry_cpu_time_and_cpu_utility_fields(self) -> None:
        self.assertIn(
            'Write-Output "RESULT=VENUE_NOT_QUIESCENT CPU_TIME=$cpuTimeField CPU_UTILITY=$avgUtility ARTIFACTS=$Pub"',
            self.template,
        )
        self.assertIn("CPU_TIME=$cpuTimeResultField CPU_UTILITY=$avgUtility", self.template)

    def test_the_three_new_functions_are_embedded_and_called_after_the_placeholder(self) -> None:
        placeholder_at = self.source.index("__EMBEDDED_FUNCTIONS__")
        for name in (
            "Get-AttrCudaQuiescenceSample",
            "Get-AttrCudaProcessCpuSnapshot",
            "Get-AttrCudaTopCpuProcesses",
        ):
            with self.subTest(function=name):
                self.assertIn(f"'{name}'", self.source[:placeholder_at])
                first_call = self.template.find(name)
                self.assertGreater(first_call, -1)


if __name__ == "__main__":
    unittest.main()
