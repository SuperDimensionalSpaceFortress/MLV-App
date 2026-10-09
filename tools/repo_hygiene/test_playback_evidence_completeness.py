"""PLAYBACK-CLIP-LENGTH-ENFORCE-4 -- "evidence is valid ONLY when every required receipt field is PRESENT and passing,
every consumer of an evidence launcher ACTS on its exit code, and an app that exits before its verdict is consumed
exits non-zero".

PR #217 (ENFORCE-3) was parked after its second key round for one root cause: its scans tested the PRESENCE of a gate,
not the CONSUMPTION of a verdict nor the ABSENCE of a field. Three repros, each a way to report success on < 20 s of
source footage:

  1. an absent ``programmatic_play_admitted`` read as "no Play happened", so a master-era binary's profile receipt (which
     carries no such field) exited 0 -- and the test's "predates" row used admitted=1, a shape no master binary writes;
  2. ``measure-wb-solve-distribution.ps1`` discarded ``capture-reference-frame.ps1``'s exit code and published
     statistics from captures the oracle had rejected;
  3. an autoplay closed mid-Play exited 0: the verdict started at 0 and was set only by a poll timer.

This file pins the whole class, with the TRUE legacy receipt shape (the fields a master-era binary writes are simply
absent -- never zero):

  A. each oracle field, ABSENT, makes the receipt INVALID, in every reader (the runner's log summary, the profile
     receipt through the wrapper, the attribution job's embedded copy) -- one test per field;
  B. every consumer of an evidence launcher is classified; each one that publishes a result acts on the launcher's exit
     code (a mutation that deletes the check makes the scan fail), and the three consumers that did not now fail the
     script, EXECUTED against a fake launcher;
  C. the app: the autoplay verdict is a fail-closed latch (header tests in tests/console/test_playback_frame_range.cpp)
     and the scan pins every write of it, of the safety budget and of the pace.
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

from tools.repo_hygiene.app_play_scan import (
    HEADER_SOURCE,
    PINNED_BUDGET_WRITES,
    PINNED_LATCH_CALLS,
    PLAY_SOURCES,
    scan_app_play_sources,
)
from tools.repo_hygiene.test_playback_clip_length_gate import (
    GATE,
    JOB_GENERATOR,
    MAIN_WINDOW,
    PROFILING,
    PWSH,
    ROOT,
    RUN_NONCE,
    RUNNER,
    _pwsh,
    _q,
    _strip_comments,
    _tool_files,
    requires_pwsh,
)
import tools.repo_hygiene.test_playback_launcher_receipt_oracle as oracle_tests
from tools.repo_hygiene.test_playback_launcher_receipt_oracle import (
    EXIT_INVALID,
    GOOD_FIELDS,
    _good_line,
    _oracle,
)

requires_windows = unittest.skipUnless(os.name == "nt", "the probe loads System.Drawing and the runner's Windows-only paths")
MODULE = PROFILING / "bachelor" / "AttrCudaArtifacts.psm1"
DOC = ROOT / "docs" / "playback-clip-length-rule.md"
HEADER = ROOT / "platform" / "qt" / "PlaybackFrameRange.h"
MAIN_WINDOW_H = ROOT / "platform" / "qt" / "MainWindow.h"
MAIN_CPP = ROOT / "platform" / "qt" / "main.cpp"

# The fields an evidence Play's receipt must CARRY. Absent = INVALID, never "not played" / "no override" / "pace
# unchecked" / "no wrap".
LOG_SUMMARY_FIELDS = ("source_advanced", "required_source_frames", "native_fps", "pace_fps", "fps_override", "wrapped", "wrap_count")


def _without(line: str, *keys: str) -> str:
    """A summary line as a build that does not write those fields would write it: the keys are OMITTED, not zeroed."""
    for key in keys:
        line = re.sub(rf"(?<![\w]){key}=\S+\s*", "", line)
    return line


# ---- A. each oracle field, absent ------------------------------------------------------------------------------------

@requires_pwsh
class AbsentFieldIsInvalidOnTheRunnerLogSummaryTests(unittest.TestCase):
    """A1. Get-GuiSmokeEvidencePlayVerdict on the app's playback_smoke.summary line."""

    def test_the_oracle_rederives_the_20_second_floor_and_does_not_trust_the_apps_requirement(self) -> None:
        # a build (or a bug) that admits a Play for 5 s of footage and then reports it consumed all of it
        for name, fields in {"5 s requirement": dict(source_advanced=120, required_source_frames=120),
                             "one frame under 20 s": dict(source_advanced=479, required_source_frames=479),
                             "native fps 0": dict(native_fps=0)}.items():
            with self.subTest(name):
                verdict = _oracle(_good_line(**fields))
                self.assertTrue(verdict["invalid"], verdict)
                self.assertIn("INVALID_SOURCE_FRAMES", json.dumps(verdict["failures"]))
        # exactly ceil(20 x 23.976) = 480 is enough, and so is more
        self.assertFalse(_oracle(_good_line(source_advanced=480, required_source_frames=480))["invalid"])
        self.assertFalse(_oracle(_good_line(source_advanced=600, required_source_frames=576))["invalid"])

    def test_the_floor_rederivation_is_mutation_tested(self) -> None:
        source = GATE.read_text(encoding="utf-8")
        mutations = {
            "the requirement check is inverted": ("[int64]$required -lt [int64][Math]::Ceiling($script:GuiSmokeMinClipSeconds",
                                                  "[int64]$required -gt [int64][Math]::Ceiling($script:GuiSmokeMinClipSeconds"),
            "an unknown native fps is forgiven": ("[double]$nativeFps -le 0) {\n        $failures += \"INVALID_SOURCE_FRAMES: native_fps=",
                                                  "[double]$nativeFps -lt -1) {\n        $failures += \"INVALID_SOURCE_FRAMES: native_fps="),
        }
        probes = [dict(source_advanced=120, required_source_frames=120), dict(native_fps=0), dict(source_advanced=480, required_source_frames=480)]
        with tempfile.TemporaryDirectory(prefix="floor-mut-") as tmp:
            for name, (old, new) in mutations.items():
                normalised = source.replace("\r\n", "\n")
                self.assertEqual(normalised.count(old), 1, f"mutation anchor missing: {name}")
                mutated = Path(tmp) / (re.sub(r"\W", "_", name) + ".ps1")
                mutated.write_text(normalised.replace(old, new), encoding="utf-8")
                with self.subTest(name):
                    differing = [p for p in probes if _oracle(_good_line(**p), gate=mutated)["invalid"] != _oracle(_good_line(**p))["invalid"]]
                    self.assertTrue(differing, f"mutation `{name}` survived")

    def test_the_helper_really_omits_the_key(self) -> None:
        line = _good_line()
        for key in LOG_SUMMARY_FIELDS:
            self.assertRegex(line, rf"(?<![\w]){key}=", key)
            self.assertNotRegex(_without(line, key), rf"(?<![\w]){key}=", key)

    def test_every_oracle_field_absent_is_invalid_and_named(self) -> None:
        self.assertFalse(_oracle(_good_line())["invalid"], "the control row must be valid")
        for key in LOG_SUMMARY_FIELDS:
            with self.subTest(absent=key):
                verdict = _oracle(_without(_good_line(), key))
                self.assertTrue(verdict["invalid"], f"{key} absent still passed: {verdict}")
                self.assertEqual(verdict["exitCode"], EXIT_INVALID)
                self.assertIn(key, json.dumps(verdict["failures"]), f"the failure must name the absent field {key}")
                self.assertIn("RECEIPT_FIELD_ABSENT", json.dumps(verdict["failures"]))

    def test_the_true_master_era_summary_is_invalid(self) -> None:
        # a master-era binary writes none of the ENFORCE-1..3 fields at all
        old = _without(_good_line(), *LOG_SUMMARY_FIELDS, "source_start_frame")
        verdict = _oracle(old)
        self.assertTrue(verdict["invalid"], verdict)
        for key in LOG_SUMMARY_FIELDS:
            self.assertIn(key, json.dumps(verdict["failures"]))

    def test_the_wrap_fields_are_required_even_when_the_source_frames_are_all_there(self) -> None:
        for key in ("wrapped", "wrap_count"):
            with self.subTest(absent=key):
                verdict = _oracle(_without(_good_line(), key))
                self.assertTrue(verdict["invalid"], verdict)
                self.assertIn("INVALID_LOOPED", json.dumps(verdict["failures"]))

    def test_a_launch_only_probe_never_plays_so_it_needs_none_of_them(self) -> None:
        # the launch-only branch is the one place the fields are not required: it must present nothing at all
        proc = _pwsh(
            f". {_q(GATE)}; $s = Convert-PlaybackLogLineToObject 'playback_smoke.summary session=1 presented_frames=0'; "
            "(Get-GuiSmokeLoopVerdict -Summary $s -WindowSeconds 24 -LaunchOnlyProbe $true).invalid")
        self.assertEqual(proc.stdout.strip(), "False", proc.stdout + proc.stderr)

    def test_the_field_absence_rows_are_mutation_tested(self) -> None:
        source = GATE.read_text(encoding="utf-8")
        needles = [text for text in ("RECEIPT_FIELD_ABSENT",) if text in source]
        self.assertTrue(needles, "the gate must carry the typed RECEIPT_FIELD_ABSENT failure")
        with tempfile.TemporaryDirectory(prefix="absent-mut-") as tmp:
            mutated = Path(tmp) / "gate.ps1"
            # forgive every absent field: the failures it would add are never recorded
            mutated.write_text(source.replace("$absent.Count -gt 0", "$false"), encoding="utf-8")
            if source.count("$absent.Count -gt 0") == 0:
                self.fail("mutation anchor vanished: the absent-field checks must test `$absent.Count -gt 0`")
            differing = [key for key in LOG_SUMMARY_FIELDS
                         if _oracle(_without(_good_line(), key), gate=mutated)["invalid"]
                         != _oracle(_without(_good_line(), key))["invalid"]]
            self.assertTrue(differing, "forgiving absent fields changed no verdict: the absence checks are not under test")


@requires_pwsh
class AbsentFieldIsInvalidInTheAttributionJobCopyTests(unittest.TestCase):
    """A2. The attribution job cannot dot-source the gate, so it embeds Get-AttrCudaSourceFramesVerdict: same rule."""

    def _module_verdict(self, line: str) -> dict:
        proc = _pwsh(
            f"Import-Module {_q(MODULE)} -Force -DisableNameChecking; "
            f"$v = Get-AttrCudaSourceFramesVerdict -SummaryLine {_q(line)} -ExpectedRunNonce {_q(RUN_NONCE)}; "
            "[pscustomobject]@{ invalid = [bool]$v.invalid; failures = @($v.failures) } | ConvertTo-Json -Compress")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def test_the_embedded_copy_rederives_the_20_second_floor_too(self) -> None:
        for name, fields in {"5 s requirement": dict(source_advanced=120, required_source_frames=120),
                             "one frame under 20 s": dict(source_advanced=479, required_source_frames=479),
                             "native fps 0": dict(native_fps=0)}.items():
            with self.subTest(name):
                self.assertTrue(self._module_verdict(_good_line(**fields))["invalid"])
        self.assertFalse(self._module_verdict(_good_line(source_advanced=480, required_source_frames=480))["invalid"])

    def test_every_oracle_field_absent_is_invalid_in_the_embedded_copy(self) -> None:
        self.assertFalse(self._module_verdict(_good_line())["invalid"])
        for key in LOG_SUMMARY_FIELDS:
            with self.subTest(absent=key):
                verdict = self._module_verdict(_without(_good_line(), key))
                self.assertTrue(verdict["invalid"], f"{key} absent still passed the job's oracle: {verdict}")
                self.assertIn(key, json.dumps(verdict["failures"]))


@requires_pwsh
class AbsentFieldIsInvalidOnTheProfileReceiptTests(oracle_tests._LauncherCase):
    """A3. run-release-playback-profile.ps1 with a play-capable option, against a fake app that writes a chosen
    receipt. Every receipt here is the TRUE shape of the build it models: fields it does not write are omitted."""

    GOOD_METADATA = oracle_tests.ProfileWrapperLauncherTests.GOOD_METADATA
    run_wrapper = oracle_tests.ProfileWrapperLauncherTests.run_wrapper

    def test_the_control_receipt_passes(self) -> None:
        proc = self.run_wrapper(dict(self.GOOD_METADATA))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_every_receipt_field_absent_is_invalid_exit_43(self) -> None:
        for key in self.GOOD_METADATA:
            with self.subTest(absent=key):
                metadata = {k: v for k, v in self.GOOD_METADATA.items() if k != key}
                proc = self.run_wrapper(metadata)
                self.assertEqual(proc.returncode, EXIT_INVALID, f"{key} absent still exited {proc.returncode}: {proc.stdout}{proc.stderr}")
                self.assertIn("INVALID", proc.stderr)

    def test_the_master_era_receipt_exits_43_never_zero(self) -> None:
        # fable/sol blocker 1: what a build from 1555952c or earlier writes for --exercise-play-action -- the play-action
        # metadata and NONE of the admission / source-frame fields. (The old test row, admitted=1, was a shape only an
        # unmerged ENFORCE-2 build writes.)
        master_era = {"play_action_smoke_requested": True, "play_action_smoke_started": True,
                      "play_action_smoke_frame_advanced": True, "play_action_smoke_timed_out": False,
                      "play_action_smoke_elapsed_ms": 5000.0, "measured_frames": 3, "total_frames": 984}
        for option in ("--exercise-play-action", "--exercise-look-assist-settle", "--exercise-look-assist-toggle"):
            with self.subTest(option=option):
                # settle / toggle only Play in Auto quality mode (r2 fable H1: any other mode is refused before launch)
                proc = self.run_wrapper(dict(master_era), option=option,
                                        quality_mode=None if option == "--exercise-play-action" else "auto")
                self.assertEqual(proc.returncode, EXIT_INVALID, proc.stdout + proc.stderr)

    def test_no_admitted_play_on_a_play_capable_profile_is_invalid_not_not_played(self) -> None:
        for admitted in (0, -1):
            with self.subTest(admitted=admitted):
                proc = self.run_wrapper({**self.GOOD_METADATA, "programmatic_play_admitted": admitted})
                self.assertEqual(proc.returncode, EXIT_INVALID, proc.stdout + proc.stderr)

    def test_a_receipt_the_app_wrote_without_metadata_or_not_at_all_is_invalid(self) -> None:
        self.assertEqual(self.run_wrapper(None).returncode, EXIT_INVALID)

    def test_the_wrapper_requires_admission_and_the_reader_no_longer_infers_not_played(self) -> None:
        wrapper = _strip_comments(oracle_tests.PROFILE_WRAPPER.read_text(encoding="utf-8"))
        self.assertIn("-RequireAdmission", wrapper)
        gate = _strip_comments(GATE.read_text(encoding="utf-8"))
        self.assertNotIn("play_performed", gate, "an absent admission must never be read as `not played`")


# ---- the runner: absent wrap fields are a failure, not a warning ------------------------------------------------------

@requires_pwsh
class RunnerAbsentWrapFieldsAreAFailureTests(unittest.TestCase):
    """A4. run-release-gui-smoke.ps1's application of the verdict used to WARN on absent wrapped / wrap_count."""

    def _block(self) -> str:
        text = RUNNER.read_text(encoding="utf-8")
        start = text.index('$loopWrappedRaw = Get-ObjectPropertyValue $playbackSummary "wrapped"')
        end = text.index("if ($LaunchOnlyProbe -and ($playbackStartLine -or $summaryLine)) {", start)
        return text[start:end]

    def _apply(self, block: str, line: str) -> dict:
        proc = _pwsh(
            f". {_q(GATE)}\n"
            "function Get-ObjectPropertyValue { param($Object, $Name) "
            "if ($null -ne $Object -and $Object.PSObject.Properties[$Name]) { $Object.$Name } else { $null } }\n"
            f"$playbackSummary = Convert-PlaybackLogLineToObject {_q(line)}\n"
            f"$runNonce = {_q(RUN_NONCE)}; "
            "$Seconds = 24; $LaunchOnlyProbe = $false; $validationFailures = @(); $validationWarnings = @(); "
            "$clipLengthGate = [pscustomobject]@{ frames = 984 }\n"
            f"{block}\n"
            "[pscustomobject]@{ invalidLooped = [bool]$invalidLooped; failures = @($validationFailures); "
            "warnings = @($validationWarnings) } | ConvertTo-Json -Compress -Depth 4")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def test_missing_wrap_fields_fail_the_run_and_are_not_a_warning(self) -> None:
        block = self._block()
        for keys in (("wrapped",), ("wrap_count",), ("wrapped", "wrap_count")):
            with self.subTest(absent=keys):
                got = self._apply(block, _without(_good_line(), *keys))
                self.assertTrue(got["invalidLooped"], got)
                self.assertTrue(any("RECEIPT_FIELD_ABSENT" in f for f in got["failures"]), got)
        control = self._apply(block, _good_line())
        self.assertFalse(control["invalidLooped"], control)
        self.assertEqual(control["failures"], [])

    def test_the_backstop_warning_is_gone(self) -> None:
        self.assertNotIn("BACKSTOP_FIELDS_MISSING", _strip_comments(RUNNER.read_text(encoding="utf-8")),
                         "an absent wrap field is a failure of the oracle, never a validation warning")


# ---- B. every consumer of an evidence launcher acts on its exit code -------------------------------------------------

EVIDENCE_LAUNCHERS = ("run-release-gui-smoke.ps1", "capture-reference-frame.ps1",
                      "validate-visible-playback.ps1", "run-release-playback-profile.ps1")

# Scripts that call an evidence launcher (or a script that calls one) and PUBLISH a result from it, each with the
# statement that ACTS on the callee's exit code: (regex over the comment-stripped source). Deleting that statement must
# make the scan fail (mutation-tested below). A consumer that is not listed here, or in CONSUMER_EXEMPT with a reason,
# fails the scan -- a NEW consumer cannot appear unreviewed.
CONSUMER_ACTS_ON_EXIT: dict[str, str] = {
    "tools/gates/compare-output-budget.ps1":
        r"&\s*\$runner\s+@runnerArgs\s*\|\s*Out-Null\s*if\s*\(\$LASTEXITCODE\s+-ne\s+0\s+-or",
    "tools/profiling/lookassist-wb-determinism.ps1":
        r"\$smokeExit\s*=\s*\$LASTEXITCODE\s*if\s*\(\$smokeExit\s+-ne\s+0\)\s*\{",
    "tools/profiling/lookassist-wb-multiclip-probe.ps1":
        r"\$smokeExit\s*=\s*\$LASTEXITCODE[\s\S]*?RunnerExit\s*=\s*\$smokeExit[\s\S]*?\$invalidRows\s*=\s*@\([\s\S]*?RunnerExit\s+-ne\s+0[\s\S]*?\bexit\s+43\b",
    "tools/profiling/measure-wb-solve-distribution.ps1":
        r"\$captureExit\s*=\s*\$LASTEXITCODE[\s\S]*?if\s*\(\$captureExit\s+-ne\s+0\b[\s\S]*?\bexit\s+43\b",
    "tools/profiling/review-dualiso-fullres-recon.ps1":
        r"Assert-GuiSmokeChildEvidenceReady\s+`?\s*-ExitCode\s+\$smokeExitCode",
    "tools/profiling/run-non-dual-iso-guard-smoke.ps1":
        r"\$smokeExit\s*=\s*\$LASTEXITCODE\s*if\s*\(\$smokeExit\s+-ne\s+0\)\s*\{",
    "tools/profiling/run-release-cuda-playback-ab.ps1":
        r"if\s*\(\$baselineResult\.exitCode\s+-ne\s+0\)[\s\S]*?if\s*\(\$candidateResult\.exitCode\s+-ne\s+0\)",
    "tools/profiling/run-ultramagnus-p3-validation.ps1":
        r"\$smokeExit\s*=\s*\$LASTEXITCODE[\s\S]*?if\s*\(\$smokeExit\s+-ne\s+0\)\s*\{",
    "tools/profiling/bachelor/playback-attr-3-cuda-job.ps1":
        r"\$smokeRc\s*=\s*\$LASTEXITCODE[\s\S]*?\$smokeRc\s+-ne\s+0",
    "tools/profiling/run-shipping-guard-smoke.ps1":
        r"\$code\s*=\s*\$LASTEXITCODE[\s\S]*?status\s*=\s*if\s*\(\$code\s+-eq\s+0\)",
    "tools/profiling/run-local-cuda-playback-dng-smoke.ps1":
        r"if\s*\(\$playbackChild\.exitCode\s+-ne\s+0\)[\s\S]*?if\s*\(\$playbackAbChild\.exitCode\s+-ne\s+0\)",
    "tools/profiling/invoke-ultramagnus-p3-evidence.ps1":
        r"if\s*\(\$importResult\.exitCode\s+-ne\s+0\)[\s\S]*?if\s*\(\$importResult\.exitCode\s+-ne\s+0\)",
    "tools/profiling/export-release-cuda-dogfood-kit.ps1":
        r"\$proofExit\s*=\s*\$LASTEXITCODE[\s\S]*?\bexit\s+\$proofExit\b",
    # DUAL-VENUE-EVIDENCE-1 r2: submits the attribution job through um-run and reads the job's exit code; a capture that
    # contradicts its own exit code is INVALID (Resolve-DvJobOutcome), and a PASS/FAIL also needs the oracle's verdict.
    "tools/profiling/dual-venue/Invoke-VenueLeg.ps1":
        r"\$exitCode\s*=\s*\[int\]\$run\.result\.exitCode[\s\S]*?if\s*\(\$exitCode\s+-ne\s+0\s+-and\s+\$resolved\.outcome\s+-eq\s+'CAPTURED'\)\s*\{",
    # VENUE-CHAIN-RUNNER-1 r2: runs each leg through Invoke-VenueLeg.ps1 and publishes CHAIN_RESULT; a leg process that exits non-zero is
    # LEG_FAILED / INVALID and the chain exits 7, whatever DVE_OUTCOME the dead leg printed (never recorded as a RAN leg).
    "tools/profiling/dual-venue/Invoke-VenueChain.ps1":
        r"\$legCode\s*=\s*\$LASTEXITCODE[\s\S]*?if\s*\(\$legCode\s+-ne\s+0\)\s*\{[\s\S]*?'LEG_FAILED'[\s\S]*?\$exitCode\s*=\s*7\b",
}

# Scripts that NAME an evidence launcher (or a consumer) and are not consumers that publish a result. Each reason is read
# by a reviewer; a new entry means a new decision.
CONSUMER_EXEMPT: dict[str, str] = {
    "tools/profiling/gui-smoke-length-gate.ps1":
        "the gate and the oracle themselves: they name the launchers in order to describe them and never launch anything.",
    "tools/profiling/bachelor/AttrCudaArtifacts.psm1":
        "the closure manifest / staging lists name the runner as a file to copy; it launches nothing.",
    "tools/profiling/bachelor/playback-attr-3-cuda-compile-job.ps1":
        "prints the attribution job as a later step of a checklist; it launches nothing.",
    "tools/profiling/test-gui-smoke-color-artifact-scan.ps1":
        "static self-test of the runner's SOURCE (integration symbols); it never plays and fails on any mismatch.",
    "tools/profiling/test-gui-smoke-gpu-texture-route-validation.ps1":
        "self-test of a pure validation function; the runner is named in strings only.",
    "tools/profiling/test-gui-smoke-screenshot-provenance.ps1":
        "parameter-validation self-test: it runs the runner only to see it REFUSE at param validation (never plays).",
    "tools/profiling/test-playback-quality-contract.ps1":
        "contract self-test: every case runs the runner expecting a non-zero exit and fails when it exits 0.",
    "tools/profiling/summarize-local-cuda-proof.ps1": "prints the proof commands as text; it launches nothing.",
    "tools/profiling/package-local-cuda-proof-result.ps1": "packages an existing proof folder; it launches nothing.",
    "tools/profiling/run-release-cuda-dng-export.ps1":
        "export launcher: -Context 'launcher' refuses every play mode; it names a proof command as text.",
    "tools/profiling/start-release-cuda-playback.ps1":
        "interactive launcher: -Context 'launcher' refuses every play mode; it names a proof command as text.",
    "tools/repo_hygiene/sealed_real_clip_receipt.py": "names the runner path as a constant to hash; it launches nothing.",
    "tools/repo_hygiene/gpu_job_result_provenance.py": "documentation of which jobs write a provenance record; it launches nothing.",
    "tools/profiling/refresh_period_histogram.py": "documentation of where a sample came from; it launches nothing.",
    "tools/gates/test_output_budget.py": "unit test of compare-output-budget.ps1 (runs it against fakes).",
}


_SCRIPT_SUFFIXES = (".ps1", ".psm1", ".py", ".cmd", ".bat", ".sh", ".mjs", ".js", ".yml", ".yaml")


def _consumer_sources() -> dict[str, str]:
    """Every script a consumer could live in: the tools/ and .github/ scan set, every other git-tracked script outside
    docs/ (a launcher called from tests/, .claude/ or the repo root is a consumer too), and tools/gates."""
    files = {p.relative_to(ROOT).as_posix(): p.read_text(encoding="utf-8", errors="replace") for p in _tool_files()}
    tracked: list[str] = []
    try:
        listing = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True, timeout=60)
        if listing.returncode == 0:
            tracked = [line for line in listing.stdout.splitlines() if line.endswith(_SCRIPT_SUFFIXES)]
    except (OSError, subprocess.SubprocessError):
        tracked = []
    if not tracked:   # no git: walk the tree
        tracked = [p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*")
                   if p.suffix in _SCRIPT_SUFFIXES and p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts]
    for rel in tracked:
        if rel.startswith(("docs/", ".claude-state/")) or rel in files:
            continue
        if rel.startswith("tools/repo_hygiene/test_") or "/test_" in rel and rel.endswith(".py") and not rel.startswith("tools/gates/"):
            continue   # python unit tests name launchers as strings
        path = ROOT / rel
        if path.is_file():
            files[rel] = path.read_text(encoding="utf-8", errors="replace")
    for extra in ("tools/gates/compare-output-budget.ps1", "tools/gates/test_output_budget.py",
                  "tools/repo_hygiene/sealed_real_clip_receipt.py", "tools/repo_hygiene/gpu_job_result_provenance.py"):
        path = ROOT / extra
        if path.is_file():
            files.setdefault(extra, path.read_text(encoding="utf-8", errors="replace"))
    return files


def derive_consumers(files: dict[str, str], acting: dict[str, str] | None = None) -> dict[str, str]:
    """rel path -> the node it names: every script whose comment-stripped code names an evidence launcher, or a
    consumer already known to act on one (so a caller of a caller is found too). The launchers themselves are not
    consumers of each other's output through this scan (their own receipt oracle is pinned in
    test_playback_launcher_receipt_oracle.py)."""
    acting = CONSUMER_ACTS_ON_EXIT if acting is None else acting
    nodes = set(EVIDENCE_LAUNCHERS) | {rel.rsplit("/", 1)[-1] for rel in acting}
    own = {f"tools/profiling/{name}" for name in EVIDENCE_LAUNCHERS}
    found: dict[str, str] = {}
    for rel, text in sorted(files.items()):
        if rel in own:
            continue
        base = rel.rsplit("/", 1)[-1]
        code = _strip_comments(text)
        for node in sorted(nodes):
            if node != base and node in code:
                found.setdefault(rel, node)
    return found


def find_consumers_not_acting_on_the_exit_code(files: dict[str, str], acting: dict[str, str] | None = None,
                                               exempt: dict[str, str] | None = None) -> list[str]:
    acting = CONSUMER_ACTS_ON_EXIT if acting is None else acting
    exempt = CONSUMER_EXEMPT if exempt is None else exempt
    problems: list[str] = []
    derived = derive_consumers(files, acting)
    for rel, node in sorted(derived.items()):
        if rel not in acting and rel not in exempt:
            problems.append(f"{rel}: names {node} but is neither a pinned consumer nor an exempt script")
    for rel, pattern in sorted(acting.items()):
        text = files.get(rel)
        if text is None:
            problems.append(f"{rel}: pinned consumer not found")
            continue
        code = _strip_comments(text)
        if not re.search(pattern, re.sub(r"\r?\n", "\n", code)):
            problems.append(f"{rel}: does not act on the evidence launcher's exit code (pinned statement missing: {pattern[:80]}...)")
    return problems


class ConsumerExitCodeScanTests(unittest.TestCase):
    """B1. Every caller of an evidence launcher reads and acts on its exit code (the check that was never pinned)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.files = _consumer_sources()

    def test_every_consumer_is_classified_and_acts_on_the_exit_code(self) -> None:
        self.assertEqual(find_consumers_not_acting_on_the_exit_code(self.files), [])

    def test_the_enumeration_finds_the_consumers_it_must(self) -> None:
        derived = derive_consumers(self.files)
        for rel in ("tools/profiling/measure-wb-solve-distribution.ps1", "tools/profiling/lookassist-wb-determinism.ps1",
                    "tools/profiling/lookassist-wb-multiclip-probe.ps1", "tools/profiling/review-dualiso-fullres-recon.ps1",
                    "tools/gates/compare-output-budget.ps1", "tools/profiling/run-ultramagnus-p3-validation.ps1",
                    "tools/profiling/run-release-cuda-playback-ab.ps1", "tools/profiling/run-non-dual-iso-guard-smoke.ps1"):
            self.assertIn(rel, derived, rel)
        # callers of callers: the shipping guard, the local proof and the evidence packager reach a launcher through one
        for rel in ("tools/profiling/run-shipping-guard-smoke.ps1", "tools/profiling/run-local-cuda-playback-dng-smoke.ps1",
                    "tools/profiling/invoke-ultramagnus-p3-evidence.ps1"):
            self.assertIn(rel, derived, rel)

    def test_pins_and_exemptions_are_real_scripts_and_disjoint(self) -> None:
        self.assertFalse(set(CONSUMER_ACTS_ON_EXIT) & set(CONSUMER_EXEMPT))
        for rel in list(CONSUMER_ACTS_ON_EXIT) + list(CONSUMER_EXEMPT):
            self.assertTrue((ROOT / rel).is_file(), f"stale entry {rel}")
        for rel, reason in CONSUMER_EXEMPT.items():
            self.assertGreater(len(reason), 30, rel)

    def test_mutation_deleting_the_exit_code_check_of_each_consumer_is_caught(self) -> None:
        for rel, pattern in sorted(CONSUMER_ACTS_ON_EXIT.items()):
            with self.subTest(rel):
                text = self.files[rel]
                matches = list(re.finditer(pattern, text))
                self.assertTrue(matches, f"the pin does not match the real source of {rel}")
                # the pin must match the CODE (not a comment): delete the first matched statement
                mutated = text[:matches[0].start()] + text[matches[0].end():]
                self.assertNotEqual(mutated, text)
                problems = find_consumers_not_acting_on_the_exit_code({**self.files, rel: mutated})
                self.assertTrue(any(rel in p and "does not act" in p for p in problems), problems)

    def test_mutation_a_comment_that_names_the_check_does_not_count(self) -> None:
        rel = "tools/profiling/lookassist-wb-determinism.ps1"
        commented = re.sub(r"(?m)^(\s*)(\$smokeExit = \$LASTEXITCODE)", r"\1# \2", self.files[rel])
        commented = re.sub(r"(?m)^(\s*)(if \(\$smokeExit -ne 0\) \{)", r"\1# \2", commented)
        self.assertNotEqual(commented, self.files[rel])
        self.assertTrue(find_consumers_not_acting_on_the_exit_code({**self.files, rel: commented}))

    def test_mutation_a_brand_new_consumer_is_caught_until_it_is_classified(self) -> None:
        fresh = {**self.files, "tools/profiling/brand-new-wb-stats.ps1":
                 "& pwsh -NoProfile -File (Join-Path $PSScriptRoot 'capture-reference-frame.ps1') -Exe $exe | Out-Null"}
        problems = find_consumers_not_acting_on_the_exit_code(fresh)
        self.assertTrue(any("brand-new-wb-stats.ps1" in p for p in problems), problems)
        # ... and a caller of a caller (a script that runs a pinned consumer) is caught too
        deeper = {**self.files, "tools/profiling/brand-new-wrapper.ps1": "& pwsh -File .\\run-ultramagnus-p3-validation.ps1"}
        self.assertTrue(any("brand-new-wrapper.ps1" in p for p in find_consumers_not_acting_on_the_exit_code(deeper)))

    def test_the_launcher_table_in_the_docs_names_every_pinned_consumer(self) -> None:
        doc = DOC.read_text(encoding="utf-8")
        for rel in CONSUMER_ACTS_ON_EXIT:
            self.assertIn(rel.rsplit("/", 1)[-1], doc, f"docs/playback-clip-length-rule.md must list {rel}")


# ---- B2. the three consumers that did not, EXECUTED against a fake launcher -----------------------------------------------

FAKE_CAPTURE = r"""
param([string]$Exe, [string]$Clip, [string]$OutDir, [string]$Commit, [int]$Seconds, [int]$PresentedFrames)
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$mode = $env:FAKE_CAPTURE_MODE
$valid = 'WB_TRACE_WORKER preview_mode=1 play_scale_active=1 patch_valid=1 patch_rawXY=10/10 raw_wb_temp=5500 raw_wb_tint=-10'
$bad   = 'WB_TRACE_WORKER preview_mode=1 play_scale_active=1 patch_valid=1 patch_rawXY=10/10 raw_wb_temp=9999 raw_wb_tint=77'
$code = 0; $line = $valid; $write = $true
switch ($mode) {
  'all_ok'         { }
  'all_invalid'    { $code = 43; $line = $bad }
  'second_invalid' { if ($OutDir -match '-r2$') { $code = 43; $line = $bad } }
  'app_failed'     { $code = 4;  $line = $bad }
  'no_err_file'    { $write = $false }
}
if ($write) { Set-Content -LiteralPath (Join-Path $OutDir 'capture.err.txt') -Value $line }
exit $code
"""


@requires_pwsh
class MeasureWbSolveDistributionTests(unittest.TestCase):
    """B2a. measure-wb-solve-distribution.ps1 published statistics from captures the oracle had rejected (sol/fable blocker)."""

    SCRIPT = PROFILING / "measure-wb-solve-distribution.ps1"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wb-dist-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(os.path.realpath(self._tmp.name))
        self.root = self.tmp / "agent"
        for sub in ("june-765ed4a3d066-dirty\\MLVApp-765ed4a3d066-dirty", "june-d14139e8225e-dirty\\MLVApp-d14139e8225e-dirty",
                    "june-ba9dec3f3427\\MLVApp-ba9dec3f3427", "mlvapp-5a30efddd186\\MLVApp-5a30efddd186"):
            exe = self.root / "staging" / (sub.replace("\\", "/") + ".exe")
            exe.parent.mkdir(parents=True, exist_ok=True)
            exe.write_bytes(b"stand-in")
        self.fake = self.tmp / "fake-capture.ps1"
        self.fake.write_text(FAKE_CAPTURE, encoding="utf-8")

    def run_script(self, mode: str, script: Path | None = None) -> tuple[subprocess.CompletedProcess, dict | None]:
        env = {**os.environ, "FAKE_CAPTURE_MODE": mode}
        proc = subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script or self.SCRIPT),
             "-ClipPath", str(self.tmp / "footage.bin"), "-Root", str(self.root), "-CaptureScript", str(self.fake), "-Runs", "3"],
            capture_output=True, text=True, timeout=300, env=env)
        published = None
        for line in reversed(proc.stdout.strip().splitlines()):
            if line.startswith("{"):
                published = json.loads(line)
                break
        return proc, published

    def test_a_clean_set_of_captures_publishes_the_distribution_and_exits_zero(self) -> None:
        proc, doc = self.run_script("all_ok")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for point in doc["points"]:
            self.assertEqual(point["validRuns"], 3)
            self.assertEqual(point["invalidRuns"], 0)
            self.assertEqual(point["tint"]["n"], 3)
        self.assertEqual(doc["verdict"], "OK")

    def test_an_invalid_capture_is_excluded_and_fails_the_script(self) -> None:
        proc, doc = self.run_script("second_invalid")
        self.assertEqual(proc.returncode, EXIT_INVALID, proc.stdout + proc.stderr)
        self.assertEqual(doc["verdict"], "INVALID")
        for point in doc["points"]:
            self.assertEqual(point["validRuns"], 2)
            self.assertEqual(point["invalidRuns"], 1)
            self.assertEqual(point["tint"]["n"], 2, "the invalid capture's traces entered the distribution")
            self.assertNotIn("77", point["tint"]["values"], "a value from an INVALID capture was published")

    def test_every_capture_rejected_means_no_statistics_and_a_failing_exit(self) -> None:
        # the four historical binaries all predate ENFORCE-3, so this is also what the script does today
        proc, doc = self.run_script("all_invalid")
        self.assertEqual(proc.returncode, EXIT_INVALID, proc.stdout + proc.stderr)
        for point in doc["points"]:
            self.assertEqual(point["validRuns"], 0)
            self.assertEqual(point["invalidRuns"], 3)
            self.assertIsNone(point["tint"])
            self.assertIsNone(point["temp"])

    def test_an_app_failure_is_not_a_measurement_either(self) -> None:
        proc, doc = self.run_script("app_failed")
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(all(point["validRuns"] == 0 for point in doc["points"]))

    def test_a_capture_that_exits_zero_but_wrote_no_trace_file_is_not_a_valid_run(self) -> None:
        proc, doc = self.run_script("no_err_file")
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(all(point["validRuns"] == 0 for point in doc["points"]))

    def test_a_missing_build_fails_the_script_too(self) -> None:
        shutil.rmtree(self.root / "staging" / "june-ba9dec3f3427")
        proc, doc = self.run_script("all_ok")
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(doc["verdict"], "INVALID")

    def test_the_script_uses_the_tracked_launcher_not_a_staged_copy(self) -> None:
        code = _strip_comments(self.SCRIPT.read_text(encoding="utf-8"))
        self.assertIn("Join-Path $PSScriptRoot 'capture-reference-frame.ps1'", code)
        self.assertNotIn("staging\\capture-reference-frame.ps1", code)

    def test_mutation_ignoring_the_launcher_exit_is_caught_by_the_scenarios(self) -> None:
        text = self.SCRIPT.read_text(encoding="utf-8")
        needle = "if($captureExit -ne 0 -or"
        self.assertEqual(text.count(needle), 1, "mutation anchor vanished")
        mutated = self.tmp / "measure-mutant.ps1"
        mutated.write_text(text.replace(needle, "if($false -and $captureExit -ne 0 -or"), encoding="utf-8")
        proc, doc = self.run_script("second_invalid", script=mutated)
        self.assertEqual(proc.returncode, 0, "with the exit unread the script reports success: that is the bug")
        self.assertEqual(doc["points"][0]["tint"]["n"], 3)


FAKE_RUNNER_FOR_PROBES = r"""
param([Parameter(ValueFromRemainingArguments = $true)]$Rest)
$out = $null
for ($i = 0; $i -lt $Rest.Count; $i++) { if ("$($Rest[$i])" -ieq '-Output') { $out = "$($Rest[$i + 1])" } }
$mode = $env:FAKE_RUNNER_MODE
$counter = Join-Path (Split-Path -Parent $PSCommandPath) 'calls.txt'
Add-Content -LiteralPath $counter -Value 'x'
$n = @(Get-Content -LiteralPath $counter).Count
$trace = 'WB_TRACE_WORKER preview_mode=1 play_scale_active=1 patch_valid=1 patch_rawXY=10/10 raw_wb_temp=5500 raw_wb_tint=-10'
$code = 0
if ($mode -eq 'all_invalid') { $code = 43 }
if ($mode -eq 'second_invalid' -and $n -eq 2) { $code = 43 }
if ($out) {
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $out) | Out-Null
  $json = @{ log = $trace; visualQuality = @{ visualState = @{ temperature = 5500; tint = -10 }; lookAssist = @{ safetyWarning = $null } } } | ConvertTo-Json -Depth 6
  Set-Content -LiteralPath $out -Value $json
}
exit $code
"""


@requires_pwsh
@requires_windows
class WbProbeConsumersFailTheScriptTests(unittest.TestCase):
    """B2b. lookassist-wb-multiclip-probe.ps1 only LABELLED a row; lookassist-wb-determinism.ps1 published a verdict from
    the reps that happened to pass. Both now fail the script, EXECUTED against a fake runner."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wb-probes-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(os.path.realpath(self._tmp.name))
        self.tools = self.tmp / "tools" / "profiling"
        self.tools.mkdir(parents=True)
        (self.tools / "run-release-gui-smoke.ps1").write_text(FAKE_RUNNER_FOR_PROBES, encoding="utf-8")
        self.exe = self.tmp / "MLVApp.exe"
        self.exe.write_bytes(b"stand-in")
        self.clip_a = self.tmp / "a.bin"
        self.clip_b = self.tmp / "b.bin"
        for path in (self.clip_a, self.clip_b):
            path.write_bytes(b"stand-in")

    def _copy(self, name: str, text: str | None = None) -> Path:
        target = self.tools / name
        target.write_text((PROFILING / name).read_text(encoding="utf-8") if text is None else text, encoding="utf-8")
        return target

    def _reset_calls(self) -> None:
        calls = self.tools / "calls.txt"
        if calls.exists():
            calls.unlink()

    def run_multiclip(self, mode: str, script: Path | None = None) -> tuple[subprocess.CompletedProcess, dict]:
        self._reset_calls()
        script = script or self._copy("lookassist-wb-multiclip-probe.ps1")
        out = self.tmp / "multi-out"
        command = (f"& {_q(script)} -ExePath {_q(self.exe)} -Clips @({_q(self.clip_a)}, {_q(self.clip_b)}) -OutDir {_q(out)} "
                   "-QtBinPrepend 'C:\\Windows'; exit $LASTEXITCODE")
        proc = subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
            capture_output=True, text=True, timeout=300, env={**os.environ, "FAKE_RUNNER_MODE": mode})
        matrix = json.loads((out / "wb-matrix.json").read_text(encoding="utf-8-sig")) if (out / "wb-matrix.json").exists() else {}
        return proc, matrix

    def run_determinism(self, mode: str) -> tuple[subprocess.CompletedProcess, dict]:
        self._reset_calls()
        script = self._copy("lookassist-wb-determinism.ps1")
        out = self.tmp / "det-out"
        proc = subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
             "-RepoRoot", str(self.tmp), "-ExePath", str(self.exe), "-Input", str(self.clip_a), "-Reps", "3",
             "-OutputRoot", str(out)],
            capture_output=True, text=True, timeout=300, env={**os.environ, "FAKE_RUNNER_MODE": mode})
        verdict = out / "determinism-verdict.json"
        return proc, (json.loads(verdict.read_text(encoding="utf-8-sig")) if verdict.exists() else {})

    def test_multiclip_all_valid_runs_exit_zero(self) -> None:
        proc, matrix = self.run_multiclip("all_ok")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual([row["RunnerExit"] for row in matrix["rows"]], [0, 0])

    def test_multiclip_an_invalid_run_fails_the_script_not_just_a_row(self) -> None:
        proc, matrix = self.run_multiclip("second_invalid")
        self.assertEqual(proc.returncode, EXIT_INVALID, proc.stdout + proc.stderr)
        self.assertEqual([row["RunnerExit"] for row in matrix["rows"]], [0, 43])
        self.assertIn("RUN_INVALID", matrix["rows"][1]["TintFlag"])     # the row is still labelled, for the reader

    def test_multiclip_every_run_invalid_fails_the_script(self) -> None:
        proc, _ = self.run_multiclip("all_invalid")
        self.assertEqual(proc.returncode, EXIT_INVALID, proc.stdout + proc.stderr)

    def test_multiclip_mutation_the_exit_is_labelled_but_not_acted_on(self) -> None:
        text = (PROFILING / "lookassist-wb-multiclip-probe.ps1").read_text(encoding="utf-8")
        needle = "exit 43"
        self.assertIn(needle, text, "mutation anchor vanished")
        mutated = self._copy("lookassist-wb-multiclip-probe.ps1", text.replace(needle, "exit 0"))
        proc, _ = self.run_multiclip("second_invalid", script=mutated)
        self.assertEqual(proc.returncode, 0, "the pre-fix behaviour: label the row, exit 0")

    def test_determinism_all_valid_reps_still_reach_a_verdict(self) -> None:
        proc, verdict = self.run_determinism("all_ok")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(verdict["verdict"], "STABLE")

    def test_determinism_one_invalid_rep_makes_the_whole_verdict_invalid(self) -> None:
        proc, verdict = self.run_determinism("second_invalid")
        self.assertEqual(proc.returncode, EXIT_INVALID, proc.stdout + proc.stderr)
        self.assertEqual(verdict["verdict"], "INVALID")
        self.assertEqual(verdict["invalidReps"], 1)

    def test_determinism_every_rep_invalid_is_invalid_too(self) -> None:
        proc, verdict = self.run_determinism("all_invalid")
        self.assertEqual(proc.returncode, EXIT_INVALID, proc.stdout + proc.stderr)
        self.assertEqual(verdict["verdict"], "INVALID")


# ---- the attribution job no longer seeds the venue's registry --------------------------------------------------------

class AttributionJobRegistrySeedTests(unittest.TestCase):
    """ATTR3-DEAD-REGISTRY-SEED-1: an automation run reads a run-scoped settings store, never HKCU, so the job's seven
    `reg add HKCU\\Software\\magiclantern.MLVApp` writes did nothing -- except dirty the venue's real store."""

    def test_the_job_writes_nothing_to_the_apps_settings_registry_key(self) -> None:
        code = _strip_comments(JOB_GENERATOR.read_text(encoding="utf-8"))
        self.assertNotIn("magiclantern.MLVApp", code)
        self.assertNotRegex(code, r"(?i)reg\s+add\s+\"?HKCU\\Software\\magiclantern")

    def test_every_value_it_used_to_seed_is_the_apps_own_default(self) -> None:
        # the seven seeds were: processing subset (the option default is Subset), zebras / caching (off), QualityMode 1
        # (HighQuality), PreviewMode 0 (SharpSmooth), ScaleFactorOverride 0 (auto), PreviewResolution 0 (Auto)
        policy = (ROOT / "platform" / "qt" / "PlaybackQualityPolicy.h").read_text(encoding="utf-8")
        self.assertRegex(policy, r"kDefaultQualityMode\(\)\s*\{\s*return static_cast<int>\(\s*PlaybackQualityMode::HighQuality\s*\);")
        self.assertRegex(policy, r"kDefaultPreviewMode\(\)\s*\{\s*return static_cast<int>\(\s*PlaybackPreviewMode::SharpSmooth\s*\);")
        self.assertRegex(policy, r"kDefaultScaleFactorOverride\(\)\s*\{\s*return 0;")
        self.assertRegex(policy, r"kDefaultPreviewResolution\(\)\s*\{\s*return static_cast<int>\(\s*PlaybackPreviewResolution::Auto\s*\);")
        self.assertRegex(policy, r"HighQuality\s*=\s*1\b")
        header = MAIN_WINDOW_H.read_text(encoding="utf-8")
        self.assertRegex(header, r"PlaybackProfileProcessingRequest\s+playbackProcessing\s*=\s*\s*PlaybackProfileProcessingRequest::Subset")
        window = MAIN_WINDOW.read_text(encoding="utf-8")
        self.assertIn('set.value( "zebras", false )', window)
        self.assertIn('set.value( "caching", false )', window)


# ---- C. the app: the latch, the budget and the pace are written only where reviewed ------------------------------------

class AppVerdictLatchPinTests(unittest.TestCase):
    """C1. app_play_scan.py: the autoplay verdict LATCH, the safety budget and the pace -- mutation-tested."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.sources = {path: (ROOT / path).read_text(encoding="utf-8") for path in (*PLAY_SOURCES, HEADER_SOURCE)}

    def mutated(self, old: str, new: str, count: int = 1, path: str = "platform/qt/MainWindow.cpp") -> dict[str, str]:
        text = self.sources[path]
        self.assertIn(old, text, "mutation anchor missing: update the test with the source")
        sources = dict(self.sources)
        sources[path] = text.replace(old, new, count)
        return sources

    def assert_caught(self, sources: dict[str, str], needle: str = "") -> None:
        problems = scan_app_play_sources(sources)
        self.assertTrue(problems and any(needle in p for p in problems), problems)

    def test_the_real_sources_are_clean(self) -> None:
        self.assertEqual(scan_app_play_sources(self.sources), [])

    def test_the_pins_name_what_was_reviewed(self) -> None:
        self.assertEqual(PINNED_LATCH_CALLS, {"armPending": 1, "fail": 1, "resolve": 1})
        self.assertEqual(sorted(PINNED_BUDGET_WRITES), ["m_playPaceFps", "m_playRequestedSeconds"])

    def test_removing_the_arm_is_caught(self) -> None:
        self.assert_caught(self.mutated("m_automationVerdict.armPending();", ""), "m_automationVerdict calls")

    def test_arming_only_after_the_clip_opens_is_caught(self) -> None:
        arm = ('if( qEnvironmentVariableIntValue( "MLVAPP_AUTOPLAY_SECONDS" ) > 0 ) m_automationVerdict.armPending();')
        text = self.sources["platform/qt/MainWindow.cpp"]
        self.assertIn(arm, text)
        moved = text.replace(arm, "", 1).replace("openMlvSet( startupFiles );", "openMlvSet( startupFiles ); " + arm, 1)
        self.assert_caught({**self.sources, "platform/qt/MainWindow.cpp": moved}, "pinned stop statement")

    def test_a_second_resolve_or_a_resolve_outside_the_hook_is_caught(self) -> None:
        resolve = "m_automationVerdict.resolve( autoplayState );"
        self.assert_caught(self.mutated(resolve, resolve + " " + resolve), "m_automationVerdict calls")
        self.assert_caught(self.mutated("void MainWindow::closeEvent(QCloseEvent *event)\n{",
                                        "void MainWindow::closeEvent(QCloseEvent *event)\n{ m_automationVerdict.resolve( "
                                        "playback_frame_range::PlayStopState::Reached );"),
                           "driven only by the autoplay hook")

    def test_resolving_failures_as_success_is_caught(self) -> None:
        self.assert_caught(self.mutated("m_automationVerdict.resolve( autoplayState );",
                                        "m_automationVerdict.resolve( playback_frame_range::PlayStopState::Reached );"),
                           "pinned stop statement")

    def test_assigning_or_escaping_the_latch_is_caught(self) -> None:
        self.assert_caught(self.mutated("m_automationVerdict.fail();",
                                        "m_automationVerdict = playback_frame_range::AutomationVerdictLatch();"), "unreviewed use of m_automationVerdict")
        self.assert_caught(self.mutated("m_automationVerdict.fail();", "auto *p = &m_automationVerdict; (void)p;"), "escapes")

    def test_the_retired_int_verdict_cannot_come_back(self) -> None:
        self.assert_caught(self.mutated("m_automationVerdict.fail();", "m_automationVerdictExitCode = 0;"), "retired int verdict")

    def test_a_stray_safety_budget_or_pace_write_is_caught(self) -> None:
        anchor = 'forceLoopOffForAutomation( "gui-smoke-measured" );'
        self.assert_caught(self.mutated(anchor, anchor + " m_playRequestedSeconds = 3600;"), "m_playRequestedSeconds written")
        self.assert_caught(self.mutated(anchor, anchor + " m_playPaceFps = 24.0;"), "m_playPaceFps written")
        self.assert_caught(self.mutated(anchor, anchor + " m_playRequestedSeconds += 1;"), "m_playRequestedSeconds written")
        self.assert_caught(self.mutated(anchor, anchor + " double *b = &m_playRequestedSeconds; (void)b;"), "escapes")

    def test_dropping_or_duplicating_a_reviewed_budget_write_is_caught(self) -> None:
        text = self.sources["platform/qt/MainWindow.cpp"]
        self.assertEqual(text.count("m_playRequestedSeconds = requestedSeconds;"), 2)
        self.assert_caught(self.mutated("m_playRequestedSeconds = requestedSeconds;", "", count=1), "m_playRequestedSeconds writes")

    def test_the_budget_is_not_written_from_anything_but_the_gates_own_value(self) -> None:
        self.assert_caught(self.mutated("m_playRequestedSeconds = requestedSeconds;", "m_playRequestedSeconds = requestedSeconds * 10.0;"),
                           "other than the reviewed assignment")

    def test_the_header_is_scanned_too_an_inline_write_cannot_escape_the_pins(self) -> None:
        # COUNTER-PIN-SCOPE-1: the .cpp pins alone would pass an inline header method that rewrites the budget or the latch
        declaration = "playback_frame_range::AutomationVerdictLatch m_automationVerdict;"
        for name, inline, needle in (
                ("budget", "void stretchBudget() { m_playRequestedSeconds = 3600.0; }", "m_playRequestedSeconds used in the header"),
                ("pace", "void fakePace() { m_playPaceFps = 24.0; }", "m_playPaceFps used in the header"),
                ("latch", "void forgive() { m_automationVerdict.resolve( playback_frame_range::PlayStopState::Reached ); }",
                 "m_automationVerdict used in the header"),
                ("counter", "void reset() { m_sourceAdvance = playback_frame_range::SourceFrameAdvanceCounter(); }",
                 "m_sourceAdvance used in the header"),
                ("requirement", "void cheat() { m_playRequiredSourceFrames = 1; }", "m_playRequiredSourceFrames used in the header")):
            with self.subTest(name):
                self.assert_caught(self.mutated(declaration, declaration + " " + inline, path=HEADER_SOURCE), needle)
        # and the real header's accessor is read-only
        accessor = "int automationVerdictExitCode() const { return m_automationVerdict.exitCode(); }"
        self.assert_caught(self.mutated(accessor, "int automationVerdictExitCode() const { m_automationVerdict.fail(); return 0; }", path=HEADER_SOURCE),
                           "m_automationVerdict used in the header")

    def test_the_header_and_main_wire_the_latch(self) -> None:
        header = MAIN_WINDOW_H.read_text(encoding="utf-8")
        self.assertRegex(header, r"playback_frame_range::AutomationVerdictLatch\s+m_automationVerdict\s*;")
        self.assertRegex(header, r"int\s+automationVerdictExitCode\(\)\s+const\s*\{\s*return\s+m_automationVerdict\.exitCode\(\)\s*;\s*\}")
        self.assertNotIn("m_automationVerdictExitCode", header)
        main = MAIN_CPP.read_text(encoding="utf-8")
        self.assertRegex(main, r"return\s+guiExitCode\s*!=\s*0\s*\?\s*guiExitCode\s*:\s*w\.automationVerdictExitCode\(\)\s*;")
        latch = HEADER.read_text(encoding="utf-8")
        self.assertIn("class AutomationVerdictLatch", latch)
        self.assertRegex(latch, r"void\s+armPending\(\)\s*\{\s*m_armed\s*=\s*true;\s*m_exitCode\s*=\s*kFailExitCode;\s*\}")
        self.assertRegex(latch, r"void\s+resolve\(\s*PlayStopState state\s*\)\s*\{\s*m_exitCode\s*=\s*state\s*==\s*PlayStopState::Reached\s*\?\s*0\s*:\s*kFailExitCode;\s*\}")

    def test_the_header_tests_cover_the_latch(self) -> None:
        tests = (ROOT / "tests" / "console" / "test_playback_frame_range.cpp").read_text(encoding="utf-8")
        for name in ("AnUnarmedLatchIsExitZeroBecauseNoAutomationPlayWasRequested", "ARequestedPlayThatIsNeverResolvedExitsFailingNotZero",
                     "OnlyReachedClearsTheLatch", "ARefusalAndALateRearmBothFailClosed", "ARefusalAloneFailsAnUnarmedLatch",
                     "ArmedIsStickyAndSurvivesAReachedVerdict", "ARefusalAloneArmsTheLatch"):
            self.assertIn(f"AutomationVerdictLatch, {name}", tests)
        for name in ("AValidNonceIsEchoedVerbatim", "UnsetOrMalformedIsTheNoNonceToken"):
            self.assertIn(f"RunNonce, {name}", tests)


if __name__ == "__main__":
    unittest.main()
