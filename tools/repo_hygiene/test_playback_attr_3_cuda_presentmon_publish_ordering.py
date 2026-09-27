"""CUDA-PERF-DISPLAY-IDENTITY-HARNESS-1/2: static assertions on the attribution generator's own
$template text -- never executed, since no real MLVApp/GPU/display is available here -- proving
the ordering and typed-terminal shape this round requires: smoke artifacts (result.json,
stdout/stderr, probe-timeline.csv, the log snapshot) are published BEFORE PresentMon is even
waited on (HARNESS-2: not just before its report is built -- see sol BLOCKER 1 below),
presentmon.csv is never read without a Test-Path guard ahead of it, and the old unguarded "no
positive MsBetweenDisplayChange samples" throw -- which used to fire AFTER a passed smoke run and
destroy every artifact already produced -- is gone.

HARNESS-2 closed a gap this file's own predecessor left open (sol BLOCKER 1): the previous
ordering test only pinned publish-before-REPORT, not publish-before-WAIT, so a PresentMon that
hung past its wait timeout or exited nonzero after a passed smoke run still threw before anything
was published, and this suite stayed green. Wait-PresentMonCapture's call site is now wrapped in
try/catch and routes to the same typed PRESENTMON_UNAVAILABLE terminal a parse failure already
used; see test_smoke_artifacts_are_published_before_presentmon_is_waited_on and
test_presentmon_wait_failure_is_a_typed_terminal_not_an_uncaught_throw below. HARNESS-2 also
closed sol BLOCKER 2 (the clock anchor is now a bracketed pre-spawn/post-spawn/OS-reported-start
triple with a stated uncertainty, not one guessed instant) and sol BLOCKER 3 (the required
PresentMon columns match the real pinned 2.5.1 header -- see the sibling display-report suite).

These are regression tripwires against reintroducing that ordering bug, not a soundness proof:
tools/repo_hygiene/test_playback_attr_3_cuda_presentmon_display_report.py is what actually EXECUTES
Get-AttrCudaPresentMonDisplayReport against real csv fixtures.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "tools" / "profiling" / "bachelor" / "AttrCudaArtifacts.psm1"
ATTRIBUTION_GENERATOR = ROOT / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-job.ps1"
BACHELOR_DIR = ATTRIBUTION_GENERATOR.parent

PWSH = shutil.which("pwsh")
requires_pwsh = unittest.skipIf(PWSH is None, "pwsh is not on PATH")


def _run_pwsh_file(script: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(script)],
        capture_output=True,
        text=True,
    )


class TemplateOrderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        text = ATTRIBUTION_GENERATOR.read_text(encoding="utf-8")
        start = text.index("$template = @'")
        end = text.index("\n'@", start)
        cls.template = text[start:end]

    def test_smoke_artifacts_are_published_before_the_display_report_is_built(self) -> None:
        # The bare function NAME also appears earlier, in a comment explaining why
        # $presentMonCaptureStartUtc is recorded before PresentMon starts -- the actual CALL is
        # anchored by its assignment, so an early comment mention can never make this test pass
        # for the wrong reason.
        report_call = self.template.index("$displayReport = Get-AttrCudaPresentMonDisplayReport")
        publish_result_json = self.template.index("Publish-AttrCudaFileCopy -Source $resultPath")
        publish_stdout = self.template.index("'smoke-stdout.txt') -Destination")
        publish_log = self.template.index("logs\\smoke-run.log'")
        self.assertLess(publish_result_json, report_call)
        self.assertLess(publish_stdout, report_call)
        self.assertLess(publish_log, report_call)

    def test_presentmon_csv_is_never_read_without_a_test_path_guard(self) -> None:
        # The only Import-Csv over $presentMonPath left in the template is the one inside the
        # embedded Get-AttrCudaPresentMonDisplayReport function body -- which is not present as
        # literal text in the generator's own $template (only the __EMBEDDED_FUNCTIONS__
        # placeholder token is, until the generator splices it in at generation time) -- so there
        # is no bare `Import-Csv -LiteralPath $presentMonPath` left at the template's own top
        # level, guarded or not.
        self.assertNotIn("Import-Csv -LiteralPath $presentMonPath", self.template)
        report_call = self.template.index("$displayReport = Get-AttrCudaPresentMonDisplayReport")
        test_path_guard = self.template.index("Test-Path -LiteralPath $presentMonPath -PathType Leaf")
        self.assertLess(test_path_guard, report_call)

    def test_the_old_unguarded_no_positive_samples_throw_is_gone(self) -> None:
        self.assertNotIn("PresentMon sidecar had no positive MsBetweenDisplayChange samples", self.template)

    def test_the_exit_codes_route_on_the_typed_status(self) -> None:
        # The literal status strings ('PRESENTMON_UNAVAILABLE' / 'DISPLAY_ASLEEP') are defined
        # once, in Get-AttrCudaPresentMonDisplayReport's own return objects (asserted against
        # directly by test_playback_attr_3_cuda_presentmon_display_report.py) -- the template
        # only branches on $displayReport.status dynamically, so it is the branch and the
        # exit-code mapping that are checked here, not a re-spelling of the module's own string
        # literals.
        self.assertIn("if ($displayReport.status -ne 'OK')", self.template)
        self.assertIn("{ 24 } else { 23 }", self.template)
        self.assertIn("if ($displayReport.status -eq 'DISPLAY_ASLEEP')", self.template)

    def test_the_module_defines_both_typed_statuses(self) -> None:
        module_text = MODULE.read_text(encoding="utf-8")
        start = module_text.index("function Get-AttrCudaPresentMonDisplayReport {")
        end = module_text.index("\n}\n\nExport-ModuleMember", start)
        function_text = module_text[start:end]
        self.assertIn("'PRESENTMON_UNAVAILABLE'", function_text)
        self.assertIn("'DISPLAY_ASLEEP'", function_text)

    def test_the_display_report_function_is_embedded(self) -> None:
        embed_list_start = self.template.find("__EMBEDDED_FUNCTIONS__")
        self.assertNotEqual(embed_list_start, -1)
        text = ATTRIBUTION_GENERATOR.read_text(encoding="utf-8")
        embed_call_start = text.index("Get-AttrCudaEmbeddedFunctionSource -Name @(")
        embed_call_end = text.index("\n)", embed_call_start)
        self.assertIn("Get-AttrCudaPresentMonDisplayReport", text[embed_call_start:embed_call_end])

    def test_presentmon_capture_anchor_is_persisted_before_the_report_is_built(self) -> None:
        # hub (sol step-0 key): the join of app swap UTC timestamps against PresentMon TimeInMs needs the capture
        # origin on disk; step 0 mis-derived it. It must be saved before parsing and carried by both outcomes.
        save_anchor = self.template.index("'presentmon-capture.json')")
        report_call = self.template.index("$displayReport = Get-AttrCudaPresentMonDisplayReport")
        self.assertLess(save_anchor, report_call)
        self.assertIn("presentMonCaptureStartUtc=$presentMonCaptureStartUtc.ToString('o')", self.template)
        self.assertIn("presentMonCaptureStartUtc = $presentMonCaptureStartUtc.ToString('o')", self.template)

    def test_smoke_artifacts_are_published_before_presentmon_is_waited_on(self) -> None:
        # CUDA-PERF-DISPLAY-IDENTITY-HARNESS-2 (sol BLOCKER 1 / fable HARDENING): a PresentMon
        # wait failure (a hang past TimeoutSeconds, a nonzero exit code) used to throw BEFORE any
        # smoke evidence was published, destroying a passed smoke run's evidence -- the
        # predecessor test above only pinned publish-before-REPORT, which a Wait failure never
        # reaches. The wait call must now come strictly after every smoke-evidence publish.
        wait_call = self.template.index("$presentMonDoneResult = Wait-PresentMonCapture $presentMonProc")
        publish_result_json = self.template.index("Publish-AttrCudaFileCopy -Source $resultPath")
        publish_stdout = self.template.index("'smoke-stdout.txt') -Destination")
        publish_probe_timeline = self.template.index("'probe-timeline.csv') -Destination")
        publish_log = self.template.index("logs\\smoke-run.log'")
        self.assertLess(publish_result_json, wait_call)
        self.assertLess(publish_stdout, wait_call)
        self.assertLess(publish_probe_timeline, wait_call)
        self.assertLess(publish_log, wait_call)

    def test_presentmon_wait_failure_is_a_typed_terminal_not_an_uncaught_throw(self) -> None:
        # The wait call is wrapped in try/catch; a caught failure routes to a typed
        # PRESENTMON_UNAVAILABLE summary and exit 23 -- the same terminal a parsing failure
        # already used -- never an uncaught throw that would propagate past the publish above.
        wait_call = self.template.index("$presentMonDoneResult = Wait-PresentMonCapture $presentMonProc")
        try_start = self.template.rindex("try {", 0, wait_call)
        catch_start = self.template.index("} catch {", wait_call)
        error_capture = self.template.index("$presentMonWaitError = $_.Exception.Message", catch_start)
        typed_check = self.template.index("if ($null -ne $presentMonWaitError) {", error_capture)
        typed_result = self.template.index("result='PRESENTMON_UNAVAILABLE'", typed_check)
        typed_exit = self.template.index("exit 23", typed_result)
        self.assertLess(try_start, wait_call)
        self.assertLess(wait_call, catch_start)
        self.assertLess(catch_start, error_capture)
        self.assertLess(error_capture, typed_check)
        self.assertLess(typed_check, typed_result)
        self.assertLess(typed_result, typed_exit)
        # PRESENTMON_TIMEOUT's own throw message (inside Wait-PresentMonCapture's body) is
        # unchanged -- only this call site's handling of it changed.
        self.assertIn("PRESENTMON_TIMEOUT: did not exit within", self.template)

    def test_presentmon_wait_failure_also_publishes_the_csv_and_capture_bracket(self) -> None:
        # CUDA-PERF-DISPLAY-IDENTITY-HARNESS-3 (fable HARDENING): a wait failure used to publish
        # neither the partial presentmon.csv nor the capture-start bracket sidecar, leaving an
        # operator diagnosing a PresentMon hang with strictly less evidence than a parse failure
        # would have left. Both must now be published inside the wait-failure catch, before its
        # typed PRESENTMON_UNAVAILABLE summary.json and exit 23 -- scoped to strictly between the
        # typed-terminal check and its exit, so this passes only if the publish calls are actually
        # inside THIS branch, not merely present somewhere earlier in the template.
        wait_call = self.template.index("$presentMonDoneResult = Wait-PresentMonCapture $presentMonProc")
        typed_check = self.template.index("if ($null -ne $presentMonWaitError) {", wait_call)
        typed_exit = self.template.index("exit 23", typed_check)
        csv_publish = self.template.index(
            "Publish-AttrCudaFileCopy -Source $presentMonPath -Destination (Join-Path $Pub 'presentmon.csv')",
            typed_check,
        )
        bracket_save = self.template.index("(Join-Path $Pub 'presentmon-capture.json')", typed_check)
        self.assertLess(typed_check, csv_publish)
        self.assertLess(csv_publish, typed_exit)
        self.assertLess(typed_check, bracket_save)
        self.assertLess(bracket_save, typed_exit)

    def test_the_display_report_call_windows_under_both_bracket_endpoints(self) -> None:
        # CUDA-PERF-DISPLAY-IDENTITY-HARNESS-3 (sol+fable HARDENING): the call must pass BOTH
        # endpoints of the capture-start bracket, never just the single earlier-only anchor the
        # prior round used (whose comment argument was inverted -- see the corrected comment
        # test below).
        self.assertIn(
            "Get-AttrCudaPresentMonDisplayReport -CsvPath $presentMonPath -ResultJson $resultJson "
            "-EarliestCaptureStartUtc $presentMonCaptureStartUtc -LatestCaptureStartUtc $presentMonPostSpawnUtc",
            self.template,
        )
        self.assertNotIn("-ResultJson $resultJson -CaptureStartUtc $presentMonCaptureStartUtc", self.template)

    def test_the_anchor_comment_no_longer_claims_a_single_safe_direction(self) -> None:
        # sol+fable (HARNESS-2 review): "windowing against it never excludes a row that truly
        # falls inside the playback window" was the inverted claim -- an anchor at or before the
        # true origin shifts the window LATER and CAN exclude a genuine front-edge row. The old
        # claim text must be gone; the corrected reasoning must be present.
        self.assertNotIn("so windowing against it never", self.template)
        self.assertIn("That direction argument was inverted", self.template)

    def test_interval_stats_are_filtered_before_get_stats(self) -> None:
        # CUDA-PERF-DISPLAY-IDENTITY-HARNESS-3 (sol BLOCKER): a row displayed only via
        # MsUntilDisplayed carries msBetweenDisplayChange=$null; [double]$null coerces to 0.0,
        # so feeding $pmRows straight into Get-Stats turned a no-interval row into a spuriously
        # fast one. The filtered $pmIntervalRows array -- never $pmRows directly -- must feed
        # Get-Stats. Executed end to end against the module's real fixture in the sibling
        # tools/repo_hygiene/test_playback_attr_3_cuda_presentmon_display_report.py
        # (IntervalStatsFilterFixtureTests); this is the static ordering/text tripwire.
        filter_line = self.template.index(
            "$pmIntervalRows = @($pmRows | Where-Object "
            "{ $null -ne $_.msBetweenDisplayChange -and $_.msBetweenDisplayChange -gt 0 })"
        )
        pm_rows_built = self.template.index("$pmRows = @($displayReport.selectedChainRows)")
        get_stats_call = self.template.index(
            "$pmStats = Get-Stats @($pmIntervalRows | ForEach-Object { [double]$_.msBetweenDisplayChange })"
        )
        self.assertLess(pm_rows_built, filter_line)
        self.assertLess(filter_line, get_stats_call)
        # The raw (unfiltered) array must never be the one handed to Get-Stats.
        self.assertNotIn(
            "Get-Stats @($pmRows | ForEach-Object { [double]$_.msBetweenDisplayChange })", self.template
        )

    def test_positive_samples_reflects_the_interval_filtered_count(self) -> None:
        # CUDA-PERF-DISPLAY-IDENTITY-HARNESS-3 (sol BLOCKER): manifest.presentMon.positiveSamples
        # must count the same population presentMonStats was computed on ($pmIntervalRows), not
        # every displayed row ($pmRows), which previously over-counted by including the
        # interval-less NA-first-present row.
        self.assertIn("positiveSamples=$pmIntervalRows.Count", self.template)
        self.assertNotIn("positiveSamples=$pmRows.Count", self.template)

    def test_presentmon_capture_anchor_bracket_and_uncertainty_are_persisted(self) -> None:
        # CUDA-PERF-DISPLAY-IDENTITY-HARNESS-2 (sol BLOCKER 2 / fable HARDENING): a single
        # guessed captureStartUtc had no stated error bar. The pre-spawn/post-spawn wall-clock
        # bracket, the OS-reported process start, and the uncertainty in ms must all be
        # persisted alongside it, before parsing.
        save_anchor = self.template.index("'presentmon-capture.json')")
        report_call = self.template.index("$displayReport = Get-AttrCudaPresentMonDisplayReport")
        self.assertLess(save_anchor, report_call)
        self.assertIn("preSpawnUtc=$presentMonPreSpawnUtc.ToString('o')", self.template)
        self.assertIn("postSpawnUtc=$presentMonPostSpawnUtc.ToString('o')", self.template)
        self.assertIn("captureStartUncertaintyMs=$presentMonCaptureStartUncertaintyMs", self.template)
        self.assertIn("$presentMonProc.StartTime.ToUniversalTime()", self.template)

    def test_the_required_columns_match_the_real_presentmon_2_5_1_legacy_header(self) -> None:
        # sol BLOCKER 3: DisplayedTime does not exist in the pinned tool's real legacy CSV schema
        # -- requiring it made every real capture PRESENTMON_UNAVAILABLE. Confirmed against real
        # Bachelor captures (presentmon.csv header), never a synthetic fixture's own columns.
        module_text = MODULE.read_text(encoding="utf-8")
        # Quoted, not a bare substring check: the module's own comments legitimately mention
        # DisplayedTime by name to explain why it was removed from the requirement.
        self.assertNotIn("'DisplayedTime'", module_text)
        self.assertIn(
            "$requiredColumns = @('Application', 'ProcessID', 'SwapChainAddress', 'PresentMode', "
            "'MsBetweenPresents', 'MsBetweenDisplayChange', 'MsUntilDisplayed', 'TimeInMs')",
            module_text,
        )

    def test_start_presentmon_capture_is_wrapped_in_try_catch_not_left_uncaught(self) -> None:
        # PRESENTMON-HARNESS-ROBUSTNESS-1: the call site used to sit bare inside the outer
        # try/finally (which has no catch of its own) -- either of Start-PresentMonCapture's own
        # throws would crash the whole job with a raw PowerShell error. Now caught, typed, and
        # exits 23 like every other PresentMon failure.
        spawn_call = self.template.index("$presentMonProc = Start-PresentMonCapture $presentMonPath")
        try_start = self.template.rindex("try {", 0, spawn_call)
        catch_start = self.template.index("} catch {", spawn_call)
        error_capture = self.template.index("$presentMonSpawnError = $_.Exception.Message", catch_start)
        typed_check = self.template.index("if ($null -ne $presentMonSpawnError) {", error_capture)
        typed_result = self.template.index("result='PRESENTMON_UNAVAILABLE'", typed_check)
        typed_exit = self.template.index("exit 23", typed_result)
        self.assertLess(try_start, spawn_call)
        self.assertLess(spawn_call, catch_start)
        self.assertLess(catch_start, error_capture)
        self.assertLess(error_capture, typed_check)
        self.assertLess(typed_check, typed_result)
        self.assertLess(typed_result, typed_exit)
        # This typed check must run strictly before the smoke run is ever launched -- a spawn
        # failure means no app-side measurement exists yet to preserve.
        smoke_launch = self.template.index("$smokeLaunchException = $null")
        self.assertLess(typed_exit, smoke_launch)

    def test_the_display_and_wait_failure_branches_carry_a_present_mon_status_field(self) -> None:
        # PRESENTMON-HARNESS-ROBUSTNESS-1: every typed PresentMon refusal (spawn, wait) tags
        # presentMonStatus='unavailable' literally -- a coarse field simple downstream consumers
        # can key on without re-deriving it from .status/exit code.
        self.assertEqual(self.template.count("presentMonStatus='unavailable'"), 2)

    def test_every_result_line_reason_is_sanitized_before_embedding(self) -> None:
        # PRESENTMON-HARNESS-ROBUSTNESS-2 (fable note, applied to all three REASON= sites: spawn,
        # wait, and display-failure): free-form text (an exception message, a typed reason) must
        # pass through ConvertTo-AttrCudaResultLineSafeText before it is embedded in a RESULT=
        # stdout line's quoted REASON="..." field, so a literal '"' inside it cannot garble a
        # naive downstream parser. Executed end to end (for the spawn branch) in
        # tools/repo_hygiene/test_playback_attr_3_cuda_behaviour.py::PresentMonSpawnFailureTests;
        # this is the static tripwire pinning all three call sites, including the ones not
        # separately executed.
        self.assertIn(
            'REASON=`"PresentMon failed to start: $(ConvertTo-AttrCudaResultLineSafeText $presentMonSpawnError)`"',
            self.template,
        )
        self.assertIn(
            'REASON=`"$(ConvertTo-AttrCudaResultLineSafeText $presentMonWaitError)`"',
            self.template,
        )
        self.assertIn(
            'REASON=`"$(ConvertTo-AttrCudaResultLineSafeText $displayReport.reason)`"',
            self.template,
        )
        self.assertIn("'ConvertTo-AttrCudaResultLineSafeText'", ATTRIBUTION_GENERATOR.read_text(encoding="utf-8"))

    def test_the_display_failure_branch_distinguishes_verified_zero_displayed(self) -> None:
        # PRESENTMON-HARNESS-ROBUSTNESS-2 (fable note): DISPLAY_ASLEEP is PresentMon
        # AFFIRMATIVELY measuring zero displayed frames, not PresentMon being unable to measure
        # at all -- the display-failure branch's presentMonStatus is now derived dynamically from
        # $displayReport.status rather than the blanket literal 'unavailable' the other two
        # refusal branches still use.
        self.assertIn(
            "$displayFailurePresentMonStatus = if ($displayReport.status -eq 'DISPLAY_ASLEEP') "
            "{ 'verified_zero_displayed' } else { 'unavailable' }",
            self.template,
        )
        self.assertIn("presentMonStatus=$displayFailurePresentMonStatus", self.template)

    def test_the_display_failure_branch_carries_the_already_computed_app_side_measurement(self) -> None:
        # PRESENTMON-HARNESS-ROBUSTNESS-1: the backend-eligibility/GPU-frames gates and the
        # region timing stats have ALL already run and succeeded by the time displayReport.status
        # is checked -- discarding them here (the pre-fix behaviour) threw away a leg whose own
        # app-side measurement was fine, just because PresentMon itself could not verify display.
        report_check = self.template.index("if ($displayReport.status -ne 'OK') {")
        typed_exit = self.template.index("exit $displayExitCode", report_check)
        for field in ("diagnostics=$diagnostics", "gpuSummary=$gpuSummary", "gpuFramesTotal=$gpuFramesTotal",
                      "frameRows=$rows.Count", "regions=$stats"):
            with self.subTest(field=field):
                pos = self.template.index(field, report_check)
                self.assertLess(pos, typed_exit)

    def test_the_wait_failure_branch_carries_the_frame_row_count(self) -> None:
        # PRESENTMON-HARNESS-ROBUSTNESS-1: $rows is already parsed and published by the time a
        # PresentMon WAIT failure can happen -- carried into that typed refusal too.
        typed_check = self.template.index("if ($null -ne $presentMonWaitError) {")
        typed_exit = self.template.index("exit 23", typed_check)
        frame_rows = self.template.index("frameRows=$rows.Count", typed_check)
        self.assertLess(typed_check, frame_rows)
        self.assertLess(frame_rows, typed_exit)

    def test_the_success_path_derives_ok_or_degraded_from_a_sufficiency_gate(self) -> None:
        # PRESENTMON-HARNESS-ROBUSTNESS-2 (sol BLOCKER, PR #174 r1): the round-1 rule
        # ($pmIntervalRows.Count -gt 0) was itself too weak -- a single positive interval still
        # read fully 'ok'.
        #
        # PRESENTMON-HARNESS-ROBUSTNESS-2 r1b (sol BLOCKER, pre-review): r1's own coverage arm
        # divided by $displayReport.selectedChain.presentedCount -- PresentMon's own count, from
        # the same csv as the numerator, so a truncated capture could read 100% coverage of its own
        # truncated rows. 'ok' now requires THREE independent arms together: count, app-swap
        # coverage (against Get-AttrCudaAppSwapTelemetry's independent app-side count, never
        # presentedCount), and temporal (no gap between positive-interval rows, including the
        # window's own head/tail bounds, exceeds a stated ceiling) -- never a bare count-only or
        # coverage-only check, and never re-spelled as $pmRows.Count (which over-counts by
        # including the interval-less NA-first-present row).
        self.assertIn("$presentMonSufficiencyMinIntervalCount = 30", self.template)
        self.assertIn("$presentMonSufficiencyMinCoverageFraction = 0.5", self.template)
        self.assertIn("$presentMonSufficiencyMaxGapMs = 5000.0", self.template)
        self.assertIn(
            "$presentMonPresentedCount = [int]$displayReport.selectedChain.presentedCount",
            self.template,
        )
        self.assertIn("$appSwapTelemetry = Get-AttrCudaAppSwapTelemetry -LogText $rawLog", self.template)
        self.assertIn(
            "$presentMonTemporal = Get-AttrCudaTemporalCoverage -TimeInMsValues "
            "@($pmIntervalRows | ForEach-Object { [double]$_.timeInMs }) -WindowStartMs "
            "$displayReport.windowStartMs -WindowEndMs $displayReport.windowEndMs -MaxGapMs "
            "$presentMonSufficiencyMaxGapMs",
            self.template,
        )
        self.assertIn(
            "$presentMonCountSufficient = ($pmIntervalRows.Count -ge $presentMonSufficiencyMinIntervalCount)",
            self.template,
        )
        self.assertIn(
            "$presentMonCoverageSufficient = ($presentMonCoverageAvailable -and "
            "($presentMonCoverageFraction -ge $presentMonSufficiencyMinCoverageFraction))",
            self.template,
        )
        self.assertIn("$presentMonTemporalSufficient = [bool]$presentMonTemporal.sufficient", self.template)
        self.assertIn(
            "$presentMonSufficient = $presentMonCountSufficient -and $presentMonCoverageSufficient "
            "-and $presentMonTemporalSufficient",
            self.template,
        )
        self.assertIn(
            "$presentMonStatus = if ($presentMonSufficient) { 'ok' } else { 'degraded' }",
            self.template,
        )
        # The old blocker rules (bare "any positive sample" gate, and r1's circular
        # presentedCount-based coverage-only gate) must be gone, not merely superseded elsewhere.
        self.assertNotIn(
            "$presentMonStatus = if ($pmIntervalRows.Count -gt 0) { 'ok' } else { 'degraded' }",
            self.template,
        )
        self.assertNotIn(
            "$presentMonStatus = if ($pmRows.Count -gt 0) { 'ok' } else { 'degraded' }",
            self.template,
        )
        self.assertNotIn(
            "$presentMonCoverageFraction = if ($presentMonPresentedCount -gt 0) "
            "{ $pmIntervalRows.Count / [double]$presentMonPresentedCount } else { 0.0 }",
            self.template,
        )
        pm_rows_built = self.template.index("$pmRows = @($displayReport.selectedChainRows)")
        coverage_computed = self.template.index("$presentMonCoverageFraction = if (")
        temporal_computed = self.template.index("$presentMonTemporal = Get-AttrCudaTemporalCoverage")
        status_assigned = self.template.index("$presentMonStatus = if ($presentMonSufficient)")
        self.assertLess(pm_rows_built, coverage_computed)
        self.assertLess(coverage_computed, temporal_computed)
        self.assertLess(temporal_computed, status_assigned)

    def test_the_sufficiency_rule_and_computed_coverage_are_published(self) -> None:
        # PRESENTMON-HARNESS-ROBUSTNESS-2 (round 1 requirement, extended r1b): the rule, its
        # thresholds, and the computed coverage/temporal figures are all persisted -- in
        # evidence-manifest.json's presentMon block, at summary.json's top level, and in the
        # RESULT= stdout line -- so a reader never has to reverse-engineer the threshold from
        # positiveSamples alone.
        self.assertIn("presentedCount=$presentMonPresentedCount", self.template)
        self.assertIn("appSwapCount=$presentMonAppSwapCount", self.template)
        self.assertIn("appSwapSource=$presentMonAppSwapSource", self.template)
        self.assertIn("coverageFraction=$presentMonCoverageFraction", self.template)
        self.assertIn("temporalMaxGapMs=$presentMonTemporal.maxGapMs", self.template)
        self.assertIn(
            "sufficiency=[ordered]@{ minIntervalCount=$presentMonSufficiencyMinIntervalCount; "
            "minCoverageFraction=$presentMonSufficiencyMinCoverageFraction; "
            "maxGapMs=$presentMonSufficiencyMaxGapMs; countSufficient=$presentMonCountSufficient; "
            "coverageSufficient=$presentMonCoverageSufficient; "
            "temporalSufficient=$presentMonTemporalSufficient; sufficient=$presentMonSufficient }",
            self.template,
        )
        self.assertIn("presentMonPositiveSamples = $pmIntervalRows.Count", self.template)
        self.assertIn("presentMonPresentedCount = $presentMonPresentedCount", self.template)
        self.assertIn("presentMonAppSwapCount = $presentMonAppSwapCount", self.template)
        self.assertIn("presentMonCoverageFraction = $presentMonCoverageFraction", self.template)
        self.assertIn(
            "PRESENTMON_COVERAGE=$([math]::Round($presentMonCoverageFraction, 3))",
            self.template,
        )

    def test_present_modes_are_a_field_of_get_attr_cuda_present_mon_display_report(self) -> None:
        # PRESENTMON-HARNESS-ROBUSTNESS-1: the module's own chain/selectedChain objects, not a
        # job-level re-derivation -- a full-screen leg's missing-samples diagnosis needs this on
        # every chain the function already builds, never a second copy that could drift from it.
        module_text = MODULE.read_text(encoding="utf-8")
        start = module_text.index("function Get-AttrCudaPresentMonDisplayReport {")
        end = module_text.index("\n}\n\nExport-ModuleMember", start)
        function_text = module_text[start:end]
        self.assertEqual(function_text.count("presentModes = @("), 2)


@requires_pwsh
class EmbeddedFunctionCoverageTests(unittest.TestCase):
    """PRESENTMON-HARNESS-ROBUSTNESS-2 r1c (sol PRE-REVIEW #2 BLOCKER): the emitted job has no
    checkout and cannot Import-Module on its host -- every function the template CALLS must be
    present in the REAL text Get-AttrCudaEmbeddedFunctionSource splices in, not merely listed in
    one of the generator's own -Name arrays (a name could be typo'd, or a new call added without
    ever touching either -Name list, and a check against the quoted list alone would still pass).
    This runs the generator's own embedding statements verbatim -- the exact source text between
    Import-Module and the second Get-AttrCudaEmbeddedFunctionSource call's closing parens -- against
    the real modules, so the assembled text checked here is byte-for-byte what Bachelor receives.
    A missing embed (this round's blocker: Get-AttrCudaAppSwapTelemetry / Get-AttrCudaTemporalCoverage
    called at template:1540/1545 but absent from the -Name list at generator:436-464) reds this
    test instead of surfacing only as a CommandNotFoundException after PresentMon has already run."""

    @classmethod
    def setUpClass(cls) -> None:
        text = ATTRIBUTION_GENERATOR.read_text(encoding="utf-8").replace("\r\n", "\n")
        start = text.index("Import-Module (Join-Path $PSScriptRoot 'AttrCudaArtifacts.psm1') -Force")
        second_call = text.index("$embeddedFunctions = $embeddedFunctions + ", start)
        end = text.index("\n))\n", second_call) + len("\n))")
        cls.embedding_snippet = text[start:end].replace("$PSScriptRoot", "$bachelorRoot")

        # The body ONLY -- unlike this file's other classes, which keep the "$template = @'"
        # prefix (harmless there, since they only ever substring-search it) -- because this class
        # feeds the text to PowerShell's own parser, and a bare unterminated "@'" opener with no
        # matching "'@" is a real parse error ("missing the terminator: '@"), not junk to ignore.
        template_marker = "$template = @'"
        template_start = text.index(template_marker) + len(template_marker)
        template_end = text.index("\n'@", template_start)
        cls.template = text[template_start:template_end]

    def test_every_called_attrcuda_command_is_defined_in_the_real_embedded_text(self) -> None:
        # Both halves use PowerShell's own AST parser, exactly like
        # test_every_module_helper_call_names_all_mandatory_parameters in
        # test_playback_attr_3_cuda_behaviour.py: CommandAst.GetCommandName() for call sites (so a
        # comment mentioning a function's name, e.g. line ~853's "(Get-AttrCudaEmbeddedFunctionSource),
        # so there is only ever one definition ...", can never be mistaken for a call), and
        # FunctionDefinitionAst.Name for what the real embedding actually defines. __TOKEN__
        # placeholders are substituted with a dummy variable reference first, the same way that
        # sibling test parses the generator's own placeholder-bearing source.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            template_path = tmp_path / "template.txt"
            template_path.write_text(self.template, encoding="utf-8")
            out_path = tmp_path / "missing.txt"
            script = tmp_path / "probe.ps1"
            script.write_text(
                "$ErrorActionPreference = 'Stop'\n"
                f"$bachelorRoot = '{BACHELOR_DIR}'\n"
                f"{self.embedding_snippet}\n"
                "$definedNames = [System.Collections.Generic.HashSet[string]]::new()\n"
                "$tokens = $null; $errors = $null\n"
                "$embeddedAst = [System.Management.Automation.Language.Parser]::ParseInput("
                "$embeddedFunctions, [ref]$tokens, [ref]$errors)\n"
                "foreach ($fn in $embeddedAst.FindAll("
                "{ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) {\n"
                "    [void]$definedNames.Add($fn.Name)\n"
                "}\n"
                f"$templateText = Get-Content -LiteralPath '{template_path}' -Raw\n"
                "$templateText = [regex]::Replace($templateText, '__[A-Z0-9_]+__', '$attrCudaPlaceholder')\n"
                "$tokens = $null; $errors = $null\n"
                "$templateAst = [System.Management.Automation.Language.Parser]::ParseInput("
                "$templateText, [ref]$tokens, [ref]$errors)\n"
                "$called = [System.Collections.Generic.HashSet[string]]::new()\n"
                "foreach ($call in $templateAst.FindAll("
                "{ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true)) {\n"
                "    $name = $call.GetCommandName()\n"
                "    if ($name -and $name -match '-AttrCuda') { [void]$called.Add($name) }\n"
                "}\n"
                "$missing = @($called | Where-Object { -not $definedNames.Contains($_) } | Sort-Object)\n"
                f"Set-Content -LiteralPath '{out_path}' -Value $missing -Encoding UTF8\n"
                "Write-Output ('CALLED_COUNT=' + $called.Count)\n"
                "Write-Output 'PROBE_DONE'\n",
                encoding="utf-8",
            )
            proc = _run_pwsh_file(script)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("PROBE_DONE", proc.stdout, proc.stdout + proc.stderr)
            self.assertNotIn("CALLED_COUNT=0", proc.stdout, "no *-AttrCuda* calls found in the template at all")
            missing = [line for line in out_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(
                missing, [],
                f"called in the template but not defined in the real embedded text: {missing} "
                f"-- add each to the -Name list(s) in {ATTRIBUTION_GENERATOR.name}",
            )


if __name__ == "__main__":
    unittest.main()
