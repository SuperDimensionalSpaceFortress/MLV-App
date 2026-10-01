"""PLAYBACK-CLIP-LENGTH-ENFORCE-4 round 2 -- "a receipt counts only if it was written by THIS invocation of the app, for THIS run".

Round 1 closed the class "every receipt field must be PRESENT and every verdict CONSUMED". sol's round-1 key found the next
member of the same family: the profile launcher read ``-Output`` and judged whatever it found, so a receipt a PREVIOUS
run left behind (valid, every field present, source_advanced >= required) was accepted when the app exited 0 without
playing -- ``--exercise-play-action --help`` returns 0 before it writes anything. A receipt is evidence only for the run
that wrote it. This file pins the whole class, in every launcher that judges a receipt:

  A. the oracle binds a receipt to its run: the launcher generates a nonce, hands it to the app (MLVAPP_RUN_NONCE), the app
     echoes it on the receipt, and a missing / mismatched / unbound nonce, or a receipt file older than the launch, is
     INVALID (RECEIPT_NOT_THIS_RUN, exit 43) -- EXECUTED in both oracle copies and mutation-tested;
  B. every launcher (the profile wrapper, capture-reference-frame, validate-visible-playback; the runner and the attribution
     job by their wiring) sets any pre-existing receipt aside BEFORE launching and judges only the nonce-bound one --
     EXECUTED against a fake app that writes a chosen receipt, with each layer mutation-tested on its own;
  C. on any INVALID the rejected receipt is renamed (``<name>.INVALID<ext>``) and marked, so an offline reader cannot mistake
     it for evidence (fable H2);
  D. a settle / toggle profile that cannot admit a Play is refused up front with a typed reason (fable H1);
  E. a present ``pace_fps`` <= 0 is INVALID in both oracle copies (fable H5);
  F. every consumer pin matches the BODY of its exit check -- the failing action -- not only the header (fable H4);
  G. the autoplay hook exits observably offscreen: the probe asserts exit 14, never kills at a bound (fable H3).
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

from tools.repo_hygiene.test_playback_clip_length_gate import (
    GATE,
    MAIN_WINDOW,
    PROFILING,
    PWSH,
    ROOT,
    RUN_NONCE,
    RUNNER,
    _pwsh,
    _q,
    _strip_comments,
    _summary_line,
    requires_pwsh,
)
import tools.repo_hygiene.test_playback_launcher_receipt_oracle as oracle_tests
from tools.repo_hygiene.test_playback_launcher_receipt_oracle import (
    CAPTURE,
    EXIT_INVALID,
    GOOD_FIELDS,
    PROFILE_WRAPPER,
    VISIBLE,
    _good_line,
    _make_fake_app,
    _oracle,
)
from tools.repo_hygiene.test_playback_evidence_completeness import _consumer_sources, requires_windows

MODULE = PROFILING / "bachelor" / "AttrCudaArtifacts.psm1"
JOB = PROFILING / "bachelor" / "playback-attr-3-cuda-job.ps1"
OFFSCREEN = PROFILING / "test-app-play-gate-offscreen.ps1"
STALE_NONCE = "nstalerun0123456789abcdef0123456789"
GOOD_METADATA = oracle_tests.ProfileWrapperLauncherTests.GOOD_METADATA


# ---- A. the oracle binds a receipt to its run ---------------------------------------------------------------------------

@requires_pwsh
class RunNonceOracleTests(unittest.TestCase):
    """A1. Get-GuiSmokeEvidencePlayVerdict / Get-GuiSmokeLoopVerdict, executed."""

    def test_a_receipt_carrying_this_runs_nonce_is_valid(self) -> None:
        self.assertFalse(_oracle(_good_line())["invalid"])

    def test_a_receipt_another_run_wrote_is_not_this_runs_receipt(self) -> None:
        verdict = _oracle(_good_line(run_nonce=STALE_NONCE))
        self.assertTrue(verdict["invalid"], verdict)
        self.assertEqual(verdict["exitCode"], EXIT_INVALID)
        self.assertIn("RECEIPT_NOT_THIS_RUN", json.dumps(verdict["failures"]))

    def test_a_receipt_with_no_nonce_is_not_this_runs_receipt(self) -> None:
        line = re.sub(r"\s*run_nonce=\S+", "", _good_line())
        verdict = _oracle(line)
        self.assertTrue(verdict["invalid"], verdict)
        self.assertIn("RECEIPT_NOT_THIS_RUN", json.dumps(verdict["failures"]))

    def test_the_apps_no_nonce_token_never_matches(self) -> None:
        # the app writes `none` when it was not handed (or was handed a malformed) nonce
        verdict = _oracle(_good_line(run_nonce="none"))
        self.assertTrue(verdict["invalid"], verdict)
        self.assertIn("RECEIPT_NOT_THIS_RUN", json.dumps(verdict["failures"]))

    def test_a_launcher_that_bound_no_nonce_cannot_accept_any_receipt(self) -> None:
        # fail closed on the LAUNCHER's omission too: no expected nonce -> nothing can be shown to be this run's
        for expected in (None, ""):
            with self.subTest(expected=expected):
                verdict = _oracle(_good_line(), nonce=expected)
                self.assertTrue(verdict["invalid"], verdict)
                self.assertIn("RECEIPT_NOT_THIS_RUN", json.dumps(verdict["failures"]))

    def test_an_app_exit_zero_that_wrote_no_receipt_is_invalid(self) -> None:
        verdict = _oracle(None)
        self.assertTrue(verdict["invalid"], verdict)
        self.assertEqual(verdict["exitCode"], EXIT_INVALID)

    def test_a_receipt_file_older_than_the_launch_is_invalid_even_with_the_right_nonce(self) -> None:
        with tempfile.TemporaryDirectory(prefix="stale-mtime-") as tmp:
            receipt = Path(tmp) / "profile.json"
            receipt.write_text(json.dumps({"metadata": GOOD_METADATA}), encoding="utf-8")
            os.utime(receipt, (1_250_000_000, 1_250_000_000))   # 2009
            proc = _pwsh(
                f". {_q(GATE)}; $s = Get-GuiSmokeProfileReceiptSummary -Path {_q(receipt)}; "
                f"$v = Get-GuiSmokeEvidencePlayVerdict -Summary $s -ExitCode 0 -ExpectedRunNonce {_q(RUN_NONCE)} -RequireAdmission $true "
                f"-ReceiptPath {_q(receipt)} -LaunchedUtc ([datetime]::UtcNow); "
                "[pscustomobject]@{ invalid = $v.invalid; failures = @($v.failures) } | ConvertTo-Json -Compress")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            verdict = json.loads(proc.stdout)
            self.assertTrue(verdict["invalid"], verdict)
            self.assertIn("RECEIPT_NOT_THIS_RUN", json.dumps(verdict["failures"]))
            # ... and the same receipt, fresh, is valid
            receipt.write_text(json.dumps({"metadata": GOOD_METADATA}), encoding="utf-8")
            proc = _pwsh(
                f". {_q(GATE)}; $s = Get-GuiSmokeProfileReceiptSummary -Path {_q(receipt)}; "
                f"$v = Get-GuiSmokeEvidencePlayVerdict -Summary $s -ExitCode 0 -ExpectedRunNonce {_q(RUN_NONCE)} -RequireAdmission $true "
                f"-ReceiptPath {_q(receipt)} -LaunchedUtc ([datetime]::UtcNow.AddSeconds(-30)); $v.invalid")
            self.assertEqual(proc.stdout.strip(), "False", proc.stdout + proc.stderr)

    def test_the_profile_receipt_reader_carries_the_nonce_and_never_invents_one(self) -> None:
        with tempfile.TemporaryDirectory(prefix="profile-nonce-") as tmp:
            receipt = Path(tmp) / "profile.json"
            for metadata, expected in (({"run_nonce": RUN_NONCE, "programmatic_play_admitted": 1}, RUN_NONCE),
                                       ({"programmatic_play_admitted": 1}, None)):
                receipt.write_text(json.dumps({"metadata": metadata}), encoding="utf-8")
                proc = _pwsh(f". {_q(GATE)}; $s = Get-GuiSmokeProfileReceiptSummary -Path {_q(receipt)}; $s | ConvertTo-Json -Compress")
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(json.loads(proc.stdout).get("run_nonce"), expected)

    def test_the_nonce_generator_makes_a_distinct_string_token_the_app_accepts(self) -> None:
        proc = _pwsh(f". {_q(GATE)}; (New-GuiSmokeRunNonce), (New-GuiSmokeRunNonce)")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        first, second = proc.stdout.split()
        self.assertNotEqual(first, second)
        for nonce in (first, second):
            self.assertRegex(nonce, r"^n[0-9a-f]{32}$")     # a leading letter: an all-digit token would parse as a number

    def test_the_nonce_decision_is_mutation_tested(self) -> None:
        source = GATE.read_text(encoding="utf-8").replace("\r\n", "\n")
        mutations = {
            "a mismatched nonce is accepted": ("-cne $ExpectedRunNonce", "-cne $ExpectedRunNonce -and $false"),
            "the verdict never asks for the nonce": ("$nonceFailure = Get-GuiSmokeRunNonceFailure -Summary $Summary -ExpectedRunNonce $ExpectedRunNonce",
                                                     "$nonceFailure = $null"),
        }
        probes = [(_good_line(run_nonce=STALE_NONCE), RUN_NONCE), (re.sub(r"\s*run_nonce=\S+", "", _good_line()), RUN_NONCE),
                  (_good_line(), None), (_good_line(run_nonce="none"), RUN_NONCE)]
        with tempfile.TemporaryDirectory(prefix="nonce-mut-") as tmp:
            for name, (old, new) in mutations.items():
                self.assertEqual(source.count(old), 1, f"mutation anchor missing or ambiguous: {name}")
                mutated = Path(tmp) / (re.sub(r"\W", "_", name) + ".ps1")
                mutated.write_text(source.replace(old, new), encoding="utf-8")
                with self.subTest(name):
                    differing = [p for p in probes if _oracle(p[0], nonce=p[1], gate=mutated)["invalid"] != _oracle(p[0], nonce=p[1])["invalid"]]
                    self.assertTrue(differing, f"mutation `{name}` survived: no probe changed its verdict")


@requires_pwsh
class RunNonceInTheAttributionJobCopyTests(unittest.TestCase):
    """A2. The attribution job cannot dot-source the gate, so its embedded oracle carries the same nonce rule."""

    def _verdict(self, line: str, nonce: str | None) -> dict:
        argument = "" if nonce is None else f" -ExpectedRunNonce {_q(nonce)}"
        proc = _pwsh(f"Import-Module {_q(MODULE)} -Force -DisableNameChecking; "
                     f"$v = Get-AttrCudaSourceFramesVerdict -SummaryLine {_q(line)}{argument}; "
                     "[pscustomobject]@{ invalid = [bool]$v.invalid; failures = @($v.failures) } | ConvertTo-Json -Compress")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def test_the_embedded_copy_binds_the_receipt_to_the_run(self) -> None:
        self.assertFalse(self._verdict(_good_line(), RUN_NONCE)["invalid"])
        for name, line, nonce in (("another run's nonce", _good_line(run_nonce=STALE_NONCE), RUN_NONCE),
                                  ("no nonce on the line", re.sub(r"\s*run_nonce=\S+", "", _good_line()), RUN_NONCE),
                                  ("the app's no-nonce token", _good_line(run_nonce="none"), RUN_NONCE),
                                  ("a launcher that bound none", _good_line(), None),
                                  ("a launcher that bound an empty one", _good_line(), "")):
            with self.subTest(name):
                verdict = self._verdict(line, nonce)
                self.assertTrue(verdict["invalid"], verdict)
                self.assertIn("RECEIPT_NOT_THIS_RUN", json.dumps(verdict["failures"]))

    def test_the_job_hands_the_oracle_the_nonce_of_the_run_log_it_read(self) -> None:
        text = _strip_comments(JOB.read_text(encoding="utf-8"))
        self.assertRegex(text, r"Get-AttrCudaSourceFramesVerdict\s+-SummaryLine\s+\$sourceFramesSummaryLine\s+-ExpectedRunNonce\s+\$runLog\.runNonce")


# ---- B. + C. every launcher sets stale receipts aside and judges only the nonce-bound one --------------------------------

class _BindingCase(oracle_tests._LauncherCase):
    def mutated_tools(self, *, gate: str | None = None, launcher: Path, launcher_text: str | None = None) -> Path:
        directory = self.tmp / "mutant-tools"
        directory.mkdir(exist_ok=True)
        for name in ("filmstrip-balance-trace.ps1", "capture-window-screenshot.ps1"):
            if (PROFILING / name).is_file():
                shutil.copy(PROFILING / name, directory / name)
        (directory / "gui-smoke-length-gate.ps1").write_text(GATE.read_text(encoding="utf-8") if gate is None else gate, encoding="utf-8")
        target = directory / launcher.name
        target.write_text(launcher.read_text(encoding="utf-8") if launcher_text is None else launcher_text, encoding="utf-8")
        return target


@requires_pwsh
class ProfileWrapperRunBindingTests(_BindingCase):
    """B1. run-release-playback-profile.ps1: sol's repro, and every way a receipt can be somebody else's."""

    STALE = {**GOOD_METADATA, "run_nonce": STALE_NONCE}

    def run_profile(self, metadata: dict | None, *, preexisting: dict | None = None, exit_code: int = 0,
                    options: tuple[str, ...] = ("--exercise-play-action",), nonce_mode: str = "echo", silent_on_help: bool = False,
                    quality_mode: str | None = None, script: Path = PROFILE_WRAPPER) -> subprocess.CompletedProcess:
        bin_dir = self.tmp / "bin"
        (bin_dir / "platforms").mkdir(parents=True, exist_ok=True)
        (bin_dir / "platforms" / "qwindows.dll").write_bytes(b"stand-in")
        self.output = self.tmp / "profile.json"
        for leftover in self.tmp.glob("*profile*.json"):
            if leftover.name != "profile.template.json":
                leftover.unlink()
        if preexisting is not None:
            self.output.write_text(json.dumps({"metadata": preexisting}), encoding="utf-8")
        copies: tuple[tuple[Path, str], ...] = ()
        if metadata is not None:
            template = self.tmp / "profile.template.json"
            template.write_text(json.dumps({"metadata": metadata}), encoding="utf-8")
            copies = ((template, str(self.output)),)
        fake = _make_fake_app(self.tmp, exit_code=exit_code, copies=copies, nonce_mode=nonce_mode, silent_on_help=silent_on_help)
        target = bin_dir / fake.name
        shutil.copy(fake, target)
        quality = f"-QualityMode {_q(quality_mode)} " if quality_mode else ""
        command = (f"& {_q(script)} -RepoRoot {_q(ROOT)} -ExePath {_q(target)} -Input {_q(self.clip)} -Output {_q(self.output)} "
                   f"-Frames 3 {quality}-AdditionalArgs @({', '.join(_q(o) for o in options)}); exit $LASTEXITCODE")
        return subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
                              capture_output=True, text=True, timeout=240)

    def test_a_run_whose_app_echoes_this_runs_nonce_passes(self) -> None:
        proc = self.run_profile(dict(GOOD_METADATA))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(self.output.is_file(), "a valid receipt stays where the caller asked for it")
        self.assertEqual(list(self.tmp.glob("*INVALID*")), [])

    def test_sols_repro_a_stale_valid_receipt_and_an_app_that_exits_zero_on_help_is_invalid(self) -> None:
        # sol r1 BLOCKER: a previous run's receipt (valid, every field present) + --exercise-play-action --help. The app returns 0
        # before playback or receipt writing; the launcher used to read the old file and exit 0.
        proc = self.run_profile(None, preexisting=self.STALE, options=("--exercise-play-action", "--help"), silent_on_help=True)
        self.assertEqual(proc.returncode, EXIT_INVALID, proc.stdout + proc.stderr)
        self.assertIn("INVALID", proc.stderr)
        self.assertFalse(self.output.exists(), "the stale receipt must not be left at the path a reader will read")
        aside = list(self.tmp.glob("STALE-*profile.json"))
        self.assertEqual(len(aside), 1, "the stale receipt is set aside, not destroyed")
        self.assertEqual(json.loads(aside[0].read_text(encoding="utf-8"))["metadata"]["run_nonce"], STALE_NONCE)

    def test_a_stale_receipt_never_turns_an_app_that_wrote_nothing_into_a_result(self) -> None:
        # exit 0 and no receipt of its own: INVALID (exit 43), whatever an earlier run left at the path
        proc = self.run_profile(None, preexisting=self.STALE)
        self.assertEqual(proc.returncode, EXIT_INVALID, proc.stdout + proc.stderr)
        # the app's own typed failure code still passes straight through (the stale file is not consulted at all)
        proc = self.run_profile(None, preexisting=self.STALE, exit_code=14)
        self.assertEqual(proc.returncode, 14, proc.stdout + proc.stderr)

    def test_an_app_that_writes_a_receipt_carrying_an_earlier_runs_nonce_is_invalid(self) -> None:
        proc = self.run_profile(dict(self.STALE), nonce_mode="stale")
        self.assertEqual(proc.returncode, EXIT_INVALID, proc.stdout + proc.stderr)
        self.assertIn("RECEIPT_NOT_THIS_RUN", proc.stderr)

    def test_an_app_that_predates_the_nonce_is_invalid(self) -> None:
        proc = self.run_profile(dict(GOOD_METADATA), nonce_mode="absent")
        self.assertEqual(proc.returncode, EXIT_INVALID, proc.stdout + proc.stderr)
        self.assertIn("RECEIPT_NOT_THIS_RUN", proc.stderr)

    def test_the_wrapper_hands_the_app_the_nonce_it_will_demand_back(self) -> None:
        # the fake echoes MLVAPP_RUN_NONCE: a wrapper that did not export one would see the 'echo' write an EMPTY nonce
        proc = self.run_profile(dict(GOOD_METADATA))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        written = json.loads(self.output.read_text(encoding="utf-8"))["metadata"]["run_nonce"]
        self.assertRegex(written, r"^n[0-9a-f]{32}$")
        self.assertNotEqual(written, RUN_NONCE)

    def test_a_caller_cannot_override_the_nonce_through_extra_environment(self) -> None:
        text = _strip_comments(PROFILE_WRAPPER.read_text(encoding="utf-8"))
        add_at = text.index("Add-EnvironmentPairs -Target $envBlock -Pairs $ExtraEnvironment")
        nonce_at = text.index('$envBlock["MLVAPP_RUN_NONCE"] = $runNonce')
        self.assertLess(add_at, nonce_at, "the wrapper's nonce is set AFTER -ExtraEnvironment so a caller cannot choose it")

    # -- C. quarantine of a rejected receipt --

    def test_a_rejected_receipt_is_renamed_and_marked_invalid(self) -> None:
        bad = {**GOOD_METADATA, "source_advanced": 240}
        proc = self.run_profile(bad)
        self.assertEqual(proc.returncode, EXIT_INVALID, proc.stdout + proc.stderr)
        self.assertFalse(self.output.exists(), "an INVALID receipt must not stay at the path a reader trusts")
        renamed = self.tmp / "profile.INVALID.json"
        self.assertTrue(renamed.is_file(), list(p.name for p in self.tmp.iterdir()))
        document = json.loads(renamed.read_text(encoding="utf-8"))
        self.assertIs(document.get("invalid"), True)
        self.assertIn("INVALID_SOURCE_FRAMES", json.dumps(document.get("invalid_reasons")))
        self.assertEqual(document["metadata"]["source_advanced"], 240, "the evidence itself is preserved")

    def test_every_invalid_shape_is_quarantined_and_a_valid_run_is_not(self) -> None:
        shapes = {"stale nonce": (dict(self.STALE), "stale"), "no nonce": (dict(GOOD_METADATA), "absent"),
                  "wrap": ({**GOOD_METADATA, "play_wrap_count": 2}, "echo")}
        for name, (metadata, mode) in shapes.items():
            with self.subTest(name):
                proc = self.run_profile(metadata, nonce_mode=mode)
                self.assertEqual(proc.returncode, EXIT_INVALID, proc.stdout + proc.stderr)
                self.assertFalse(self.output.exists())
                self.assertTrue((self.tmp / "profile.INVALID.json").is_file())

    # -- the layers, each mutation-tested on its own --

    def test_the_stale_receipt_scenario_is_caught_by_each_layer_alone_and_missed_only_with_both_removed(self) -> None:
        gate = GATE.read_text(encoding="utf-8")
        wrapper = PROFILE_WRAPPER.read_text(encoding="utf-8")
        nonce_off = gate.replace("-cne $ExpectedRunNonce", "-cne $ExpectedRunNonce -and $false")
        self.assertNotEqual(nonce_off, gate, "mutation anchor missing: the nonce comparison")
        aside_call = "Move-GuiSmokeStaleReceiptAside -Path $outputPath"
        self.assertEqual(wrapper.count(aside_call), 1, "mutation anchor missing: the pre-launch set-aside")
        aside_off = wrapper.replace(aside_call, "[pscustomobject]@{ ok = $true }")
        # the repro, with a receipt whose nonce the app echoes wrongly AND stale content in place
        scenario = dict(preexisting=self.STALE, options=("--exercise-play-action", "--help"), silent_on_help=True)
        both_present = self.run_profile(None, **scenario)
        self.assertEqual(both_present.returncode, EXIT_INVALID)
        only_set_aside = self.run_profile(None, script=self.mutated_tools(gate=nonce_off, launcher=PROFILE_WRAPPER), **scenario)
        self.assertEqual(only_set_aside.returncode, EXIT_INVALID, "the pre-launch set-aside alone must reject the stale receipt")
        only_nonce = self.run_profile(None, script=self.mutated_tools(launcher=PROFILE_WRAPPER, launcher_text=aside_off), **scenario)
        self.assertEqual(only_nonce.returncode, EXIT_INVALID, "the nonce binding alone must reject the stale receipt")
        neither = self.run_profile(None, script=self.mutated_tools(gate=nonce_off, launcher=PROFILE_WRAPPER, launcher_text=aside_off), **scenario)
        self.assertEqual(neither.returncode, 0, "with both layers removed the stale receipt is accepted: that is sol's bug, and this test can see it")

    def test_the_quarantine_is_mutation_tested(self) -> None:
        wrapper = PROFILE_WRAPPER.read_text(encoding="utf-8")
        self.assertEqual(wrapper.count("Set-GuiSmokeReceiptInvalid -Path $outputPath"), 1, "the wrapper must call the quarantine once")
        gate = GATE.read_text(encoding="utf-8").replace("\r\n", "\n")
        needle = "function Set-GuiSmokeReceiptInvalid {\n"
        self.assertEqual(gate.count(needle), 1, "mutation anchor missing: the quarantine function")
        mutated = self.mutated_tools(gate=gate.replace(needle, needle + "    return $null\n"), launcher=PROFILE_WRAPPER)
        proc = self.run_profile({**GOOD_METADATA, "source_advanced": 240}, script=mutated)
        self.assertEqual(proc.returncode, EXIT_INVALID)
        self.assertTrue(self.output.exists(), "with the quarantine removed the rejected receipt stays where a reader trusts it: the bug")

    # -- D. fable H1: a profile that cannot admit a Play is refused up front --

    def test_a_settle_or_toggle_profile_without_a_play_admitting_quality_mode_is_refused_before_launch(self) -> None:
        for option in ("--exercise-look-assist-settle", "--exercise-look-assist-toggle"):
            for quality in (None, "HighQuality", "hq", "Off"):
                with self.subTest(option=option, quality=quality):
                    proc = self.run_profile(dict(GOOD_METADATA), options=(option,), quality_mode=quality)
                    self.assertEqual(proc.returncode, 44, proc.stdout + proc.stderr)
                    self.assertIn("PASS_THROUGH_REFUSED", proc.stderr)
                    self.assertIn("auto_quality_mode", proc.stderr)
                    self.assertFalse(self.output.exists(), "the app must not even be launched")

    def test_a_settle_or_toggle_profile_in_auto_quality_mode_is_allowed_in_any_case(self) -> None:
        for option in ("--exercise-look-assist-settle", "--exercise-look-assist-toggle"):
            for quality in ("auto", "Auto", "AUTO"):
                with self.subTest(option=option, quality=quality):
                    proc = self.run_profile(dict(GOOD_METADATA), options=(option,), quality_mode=quality)
                    self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_the_play_action_does_not_need_the_auto_mode(self) -> None:
        for quality in (None, "HighQuality"):
            with self.subTest(quality=quality):
                self.assertEqual(self.run_profile(dict(GOOD_METADATA), quality_mode=quality).returncode, 0)

    def test_the_up_front_refusal_is_mutation_tested(self) -> None:
        wrapper = PROFILE_WRAPPER.read_text(encoding="utf-8")
        needle = "$modeGate.verdict -ne 'OK'"
        self.assertEqual(wrapper.count(needle), 1, "mutation anchor missing: the mode gate")
        mutated = self.mutated_tools(launcher=PROFILE_WRAPPER, launcher_text=wrapper.replace(needle, "$false"))
        proc = self.run_profile(dict(GOOD_METADATA), options=("--exercise-look-assist-settle",), script=mutated)
        self.assertEqual(proc.returncode, 0, "without the up-front refusal the settle profile launches: that is the bug this test can see")


@requires_pwsh
class CaptureReferenceFrameRunBindingTests(_BindingCase):
    """B2. capture-reference-frame.ps1: the log directory and the grab it judges are THIS run's."""

    def run_capture(self, line: str | None, *, nonce_mode: str = "echo", exit_code: int = 0, seed_stale: bool = False,
                    write_grab: bool = True, script: Path = CAPTURE) -> tuple[subprocess.CompletedProcess, Path]:
        out = self.tmp / "capture-out"
        out.mkdir(exist_ok=True)
        if seed_stale:
            (out / "reference-frame.png").write_bytes(b"\1" * 65536)
            (out / "reference-frame-candidate.json").write_text(json.dumps({"status": "candidate-unreviewed", "sourceFrames": {"verdict": "VALID"}}),
                                                                 encoding="utf-8")
        grab = self.tmp / "grab.template"
        grab.write_bytes(b"\0" * 65536)
        copies = self.log_template(line) + (((grab, str(out / "reference-frame.png")),) if write_grab else ())
        fake = _make_fake_app(self.tmp, exit_code=exit_code, copies=copies, nonce_mode=nonce_mode)
        proc = subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
             "-Exe", str(fake), "-Clip", str(self.clip), "-OutDir", str(out), "-Seconds", "25", "-SettleMs", "100",
             "-Commit", "0123456789abcdef0123456789abcdef01234567"],
            capture_output=True, text=True, timeout=240)
        return proc, out

    def test_this_runs_receipt_makes_a_candidate(self) -> None:
        proc, out = self.run_capture(_good_line())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue((out / "reference-frame-candidate.json").is_file())
        self.assertFalse((out / "INVALID.json").exists())

    def test_a_receipt_with_another_runs_nonce_is_invalid_and_nothing_is_left_to_mistake(self) -> None:
        proc, out = self.run_capture(_good_line(run_nonce=STALE_NONCE), nonce_mode="stale")
        self.assertEqual(proc.returncode, EXIT_INVALID, proc.stdout + proc.stderr)
        self.assertIn("RECEIPT_NOT_THIS_RUN", proc.stdout)
        self.assertFalse((out / "reference-frame-candidate.json").exists())
        self.assertFalse((out / "reference-frame.png").exists(), "the grab of an INVALID run is renamed")
        self.assertTrue((out / "reference-frame.INVALID.png").is_file())
        marker = json.loads((out / "INVALID.json").read_text(encoding="utf-8"))
        self.assertIs(marker["invalid"], True)
        self.assertIn("RECEIPT_NOT_THIS_RUN", json.dumps(marker["reasons"]))

    def test_last_runs_grab_and_candidate_are_set_aside_and_an_app_that_wrote_nothing_is_invalid(self) -> None:
        # the old behaviour: the app exited 0 without a grab or a receipt; the stale PNG satisfied `Test-Path $shot`
        proc, out = self.run_capture(None, seed_stale=True, write_grab=False)
        self.assertEqual(proc.returncode, EXIT_INVALID, proc.stdout + proc.stderr)
        self.assertFalse((out / "reference-frame-candidate.json").exists(), "last run's candidate manifest must not survive")
        self.assertEqual(len(list(out.glob("STALE-*reference-frame.png"))), 1)
        self.assertEqual(len(list(out.glob("STALE-*reference-frame-candidate.json"))), 1)

    def test_the_set_aside_and_the_nonce_are_each_mutation_tested(self) -> None:
        text = CAPTURE.read_text(encoding="utf-8")
        aside = "Move-GuiSmokeStaleReceiptAside -Path"
        self.assertGreaterEqual(text.count(aside), 1)
        no_aside = re.sub(r"Move-GuiSmokeStaleReceiptAside -Path \S+", "[pscustomobject]@{ ok = $true }", text)
        proc, out = self.run_capture(_good_line(), seed_stale=True, write_grab=False,
                                     script=self.mutated_tools(launcher=CAPTURE, launcher_text=no_aside))
        self.assertEqual(proc.returncode, 0, "with the set-aside gone, last run's grab is accepted next to this run's receipt: the bug")
        gate = GATE.read_text(encoding="utf-8")
        nonce_off = gate.replace("-cne $ExpectedRunNonce", "-cne $ExpectedRunNonce -and $false")
        proc, out = self.run_capture(_good_line(run_nonce=STALE_NONCE), nonce_mode="stale",
                                     script=self.mutated_tools(gate=nonce_off, launcher=CAPTURE))
        self.assertEqual(proc.returncode, 0, "with the nonce check gone another run's log is accepted: the bug")


@requires_pwsh
@requires_windows
class VisiblePlaybackRunBindingTests(_BindingCase):
    """B3. validate-visible-playback.ps1: the filmstrip's receipt is THIS run's."""

    def run_visible(self, line: str | None, *, nonce_mode: str = "echo", seed_stale_caps: bool = False,
                    script: Path = VISIBLE) -> tuple[subprocess.CompletedProcess, Path]:
        out = self.tmp / "visible-out"
        out.mkdir(exist_ok=True)
        if seed_stale_caps:
            (out / "cap-000.png").write_bytes(b"\2" * 2048)
        fake = _make_fake_app(self.tmp, exit_code=0, copies=self.log_template(line), nonce_mode=nonce_mode)
        proc = subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
             "-ExePath", str(fake), "-ClipPath", str(self.clip), "-OutDir", str(out), "-Captures", "0", "-SettleMs", "100", "-Seconds", "25"],
            capture_output=True, text=True, timeout=300)
        return proc, out

    def test_this_runs_receipt_is_valid(self) -> None:
        proc, out = self.run_visible(_good_line())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse((out / "INVALID.json").exists())

    def test_another_runs_receipt_is_invalid_and_the_run_is_marked(self) -> None:
        proc, out = self.run_visible(_good_line(run_nonce=STALE_NONCE), nonce_mode="stale")
        self.assertEqual(proc.returncode, EXIT_INVALID, proc.stdout + proc.stderr)
        self.assertIn("RECEIPT_NOT_THIS_RUN", proc.stderr)
        marker = json.loads((out / "INVALID.json").read_text(encoding="utf-8"))
        self.assertIs(marker["invalid"], True)

    def test_last_runs_filmstrip_frames_are_set_aside_before_launch_and_not_counted(self) -> None:
        proc, out = self.run_visible(_good_line(), seed_stale_caps=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(len(list(out.glob("STALE-*cap-000.png"))), 1)
        self.assertFalse((out / "cap-000.png").exists())
        # Format-List colours its property names on a CI runner (ANSI escapes between the name and the value): strip them first
        self.assertRegex(re.sub(r"\x1b\[[0-9;]*m", "", proc.stdout), r"captures\s*:\s*0\b")


# The CLASS pin: every launcher the scan sees starting the app for an evidence Play (the same derived set the receipt-oracle
# scan uses, minus its reasoned exemptions) carries all three layers of the run binding. A NEW launcher that has the oracle
# but not the binding fails here until it does.
RUN_BINDING_TOKENS = ("-ExpectedRunNonce", "MLVAPP_RUN_NONCE", "Move-GuiSmokeStaleReceiptAside")


def find_launchers_without_run_binding(files: dict[str, str]) -> list[str]:
    from tools.repo_hygiene.test_playback_launcher_receipt_oracle import RECEIPT_ORACLE_EXEMPT, _launches_play
    offenders = []
    for rel, text in sorted(files.items()):
        code = _strip_comments(text)
        if rel in RECEIPT_ORACLE_EXEMPT or not _launches_play(code):
            continue
        missing = [token for token in RUN_BINDING_TOKENS if token not in code]
        if missing:
            offenders.append(f"{rel}: starts the app for a Play but is not bound to its run (missing {', '.join(missing)})")
    return offenders


class RunBindingScanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.files = oracle_tests._tools_by_path()

    def test_every_evidence_launcher_is_bound_to_its_run(self) -> None:
        self.assertEqual(find_launchers_without_run_binding(self.files), [])

    def test_the_four_direct_launchers_are_in_the_scanned_set(self) -> None:
        from tools.repo_hygiene.test_playback_launcher_receipt_oracle import RECEIPT_ORACLE_REQUIRED, _launches_play
        derived = {rel for rel, text in self.files.items() if _launches_play(_strip_comments(text))}
        self.assertTrue(RECEIPT_ORACLE_REQUIRED <= derived)

    def test_removing_any_layer_from_any_launcher_is_caught(self) -> None:
        from tools.repo_hygiene.test_playback_launcher_receipt_oracle import RECEIPT_ORACLE_REQUIRED
        for rel in sorted(RECEIPT_ORACLE_REQUIRED):
            for token in RUN_BINDING_TOKENS:
                with self.subTest(rel=rel, token=token):
                    text = self.files[rel]
                    self.assertIn(token, _strip_comments(text), f"{rel} does not carry {token}")
                    mutated = text.replace(token, "Removed-By-Mutation")
                    self.assertTrue(find_launchers_without_run_binding({rel: mutated}), f"{rel} without {token} passed the scan")

    def test_a_new_launcher_that_has_the_oracle_but_no_binding_is_caught(self) -> None:
        fresh = {"tools/profiling/brand-new-launcher.ps1":
                 "& $exe --gui-smoke-playback --input $clip --seconds 25\n$v = Get-GuiSmokeEvidencePlayVerdict -Summary $s -ExitCode $c\nif ($v.invalid) { exit 43 }"}
        self.assertTrue(find_launchers_without_run_binding(fresh))


class RunnerAndJobBindingWiringTests(unittest.TestCase):
    """B4. run-release-gui-smoke.ps1 already used a per-run log directory; the loop verdict now also demands the nonce."""

    TEXT = _strip_comments(RUNNER.read_text(encoding="utf-8"))

    def test_the_runner_exports_the_nonce_it_will_demand_and_passes_it_to_the_verdict(self) -> None:
        self.assertIn('$envBlock["MLVAPP_RUN_NONCE"] = $runNonce', self.TEXT)
        self.assertRegex(self.TEXT, r"Get-GuiSmokeLoopVerdict\s+-Summary\s+\$playbackSummary\s+-WindowSeconds\s+\$Seconds\s+-ExpectedRunNonce\s+\$runNonce\b")
        self.assertIn("Move-GuiSmokeStaleReceiptAside -Path $outputPath", self.TEXT)

    def test_a_runner_result_marked_invalid_by_the_oracle_says_so_in_the_file(self) -> None:
        self.assertIn("-NotePropertyName invalid -NotePropertyValue $true", self.TEXT)
        self.assertIn("-NotePropertyName invalid_reasons -NotePropertyValue @($loopVerdict.failures)", self.TEXT)

    def test_the_apps_nonce_comes_from_the_environment_and_is_echoed_on_both_receipts(self) -> None:
        window = MAIN_WINDOW.read_text(encoding="utf-8")
        self.assertIn('qgetenv( "MLVAPP_RUN_NONCE" )', window)
        self.assertIn('QStringLiteral("run_nonce"), automationRunNonce()', window)
        self.assertIn('"run_nonce=%80"', window)
        self.assertIn(".arg( automationRunNonce() );", window)


@requires_pwsh
class RunnerNonceApplicationTests(unittest.TestCase):
    """B5. The runner's APPLICATION of the verdict, executed: another run's summary line is INVALID_LOOPED / exit 43."""

    def _apply(self, line: str, nonce: str) -> dict:
        text = RUNNER.read_text(encoding="utf-8")
        start = text.index('$loopWrappedRaw = Get-ObjectPropertyValue $playbackSummary "wrapped"')
        end = text.index("if ($LaunchOnlyProbe -and ($playbackStartLine -or $summaryLine)) {", start)
        proc = _pwsh(
            f". {_q(GATE)}\n"
            "function Get-ObjectPropertyValue { param($Object, $Name) "
            "if ($null -ne $Object -and $Object.PSObject.Properties[$Name]) { $Object.$Name } else { $null } }\n"
            f"$playbackSummary = Convert-PlaybackLogLineToObject {_q(line)}\n"
            f"$runNonce = {_q(nonce)}; $Seconds = 24; $LaunchOnlyProbe = $false; $validationFailures = @(); $validationWarnings = @(); "
            "$clipLengthGate = [pscustomobject]@{ frames = 984 }\n"
            f"{text[start:end]}\n"
            "[pscustomobject]@{ invalidLooped = [bool]$invalidLooped; failures = @($validationFailures) } | ConvertTo-Json -Compress -Depth 4")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def test_the_runner_rejects_a_summary_another_run_wrote(self) -> None:
        self.assertFalse(self._apply(_good_line(), RUN_NONCE)["invalidLooped"])
        got = self._apply(_good_line(run_nonce=STALE_NONCE), RUN_NONCE)
        self.assertTrue(got["invalidLooped"], got)
        self.assertIn("RECEIPT_NOT_THIS_RUN", json.dumps(got["failures"]))


# ---- B6. the app's side of the binding, pinned by app_play_scan.py and mutation-tested ---------------------------------------

class AppRunBindingPinTests(unittest.TestCase):
    """The nonce writes, the no-prompt close and the refusal's exit code are pinned statements; each mutation fails the scan."""

    @classmethod
    def setUpClass(cls) -> None:
        from tools.repo_hygiene.app_play_scan import HEADER_SOURCE, PLAY_SOURCES
        cls.sources = {path: (ROOT / path).read_text(encoding="utf-8") for path in (*PLAY_SOURCES, HEADER_SOURCE)}

    def caught(self, old: str, new: str) -> list[str]:
        from tools.repo_hygiene.app_play_scan import scan_app_play_sources
        text = self.sources["platform/qt/MainWindow.cpp"]
        self.assertEqual(text.count(old), 1, f"mutation anchor missing or ambiguous: {old[:60]}")
        return scan_app_play_sources({**self.sources, "platform/qt/MainWindow.cpp": text.replace(old, new)})

    def test_the_real_sources_are_clean(self) -> None:
        from tools.repo_hygiene.app_play_scan import scan_app_play_sources
        self.assertEqual(scan_app_play_sources(self.sources), [])

    def test_each_mutation_is_caught(self) -> None:
        cases = {
            "the close event prompts in an automation run again": ("!m_automationVerdict.armed() && ", ""),
            "the close event never consults the latch": ("if( !m_automationVerdict.armed() && ui->actionAskForSavingOnQuit->isChecked()",
                                                         "if( ui->actionAskForSavingOnQuit->isChecked()"),
            "the refusal quits (and waits on the prompt) instead of exiting 14":
                ("qApp->exit( playback_frame_range::AutomationVerdictLatch::kFailExitCode ); } );", "qApp->quit(); } );"),
            "the refusal exits 0": ("qApp->exit( playback_frame_range::AutomationVerdictLatch::kFailExitCode ); } );",
                                    "qApp->exit( 0 ); } );"),
            "the summary writes a constant instead of the run nonce": (".arg( automationRunNonce() );", ".arg( QStringLiteral(\"none\") );"),
            "the summary drops the run_nonce field": ('"run_nonce=%80" )', '"x=%80" )'),
            "the profile receipt drops the run nonce": ('metadata.insert( QStringLiteral("run_nonce"), automationRunNonce() );', ""),
        }
        for name, (old, new) in cases.items():
            with self.subTest(name):
                self.assertTrue(self.caught(old, new), f"`{name}` survived the app scan")

    def test_the_nonce_comes_only_from_the_sanitising_helper(self) -> None:
        window = self.sources["platform/qt/MainWindow.cpp"]
        self.assertEqual(window.count('qgetenv( "MLVAPP_RUN_NONCE" )'), 1, "the environment is read in exactly one place")
        self.assertIn("playback_frame_range::sanitizeRunNonce(", window)


# ---- E. fable H5: a present pace_fps <= 0 is INVALID in both oracle copies -----------------------------------------------

@requires_pwsh
class PaceZeroTests(unittest.TestCase):
    def test_a_present_non_positive_pace_is_invalid_in_both_copies(self) -> None:
        for pace in (0, "0.000", "-1.000"):
            with self.subTest(pace=pace):
                line = _good_line(pace_fps=pace)
                gate = _oracle(line)
                self.assertTrue(gate["invalid"], gate)
                self.assertIn("pace_fps", json.dumps(gate["failures"]))
                proc = _pwsh(f"Import-Module {_q(MODULE)} -Force -DisableNameChecking; "
                             f"$v = Get-AttrCudaSourceFramesVerdict -SummaryLine {_q(line)} -ExpectedRunNonce {_q(RUN_NONCE)}; "
                             "[pscustomobject]@{ invalid = [bool]$v.invalid; failures = @($v.failures) } | ConvertTo-Json -Compress")
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                module = json.loads(proc.stdout)
                self.assertTrue(module["invalid"], module)
                self.assertIn("pace_fps", json.dumps(module["failures"]))

    def test_the_native_pace_is_still_valid(self) -> None:
        self.assertFalse(_oracle(_good_line())["invalid"])

    def test_the_pace_zero_rule_is_mutation_tested_in_both_copies(self) -> None:
        gate_text = GATE.read_text(encoding="utf-8").replace("\r\n", "\n")
        old = "[double]$paceFps -le 0"
        self.assertEqual(gate_text.count(old), 1, "mutation anchor missing in the gate")
        with tempfile.TemporaryDirectory(prefix="pace-mut-") as tmp:
            mutated = Path(tmp) / "gate.ps1"
            mutated.write_text(gate_text.replace(old, "$false"), encoding="utf-8")
            # a zero pace also differs from the native pace, so the verdict stays INVALID either way; what the dedicated rule adds is
            # the TYPED reason (the pace is unknown), so the mutation is observed in the failure text
            self.assertNotEqual(_oracle(_good_line(pace_fps=0), gate=mutated)["failures"], _oracle(_good_line(pace_fps=0))["failures"])
            module_text = MODULE.read_text(encoding="utf-8").replace("\r\n", "\n")
            old_module = "$pace -le 0"
            self.assertEqual(module_text.count(old_module), 1, "mutation anchor missing in the module")
            mutated_module = Path(tmp) / "AttrCudaArtifacts.psm1"
            mutated_module.write_text(module_text.replace(old_module, "$false"), encoding="utf-8")
            line = _good_line(pace_fps=0)
            run = lambda module: json.loads(_pwsh(  # noqa: E731
                f"Import-Module {_q(module)} -Force -DisableNameChecking; "
                f"$v = Get-AttrCudaSourceFramesVerdict -SummaryLine {_q(line)} -ExpectedRunNonce {_q(RUN_NONCE)}; "
                "[pscustomobject]@{ failures = @($v.failures) } | ConvertTo-Json -Compress").stdout)["failures"]
            self.assertNotEqual(run(mutated_module), run(MODULE))


# ---- F. fable H4: consumer pins match the BODY of the exit check --------------------------------------------------------

# rel path -> ((regex, minimum matches), ...). Each regex matches the exit-code check AND the statement in its BODY that fails
# the run (the named group `act`): `exit`, a recorded failure, an INVALID verdict. Emptying a body removes `act`, the regex no
# longer matches, and the scan fails -- the header-only pins of round 1 survived exactly that mutation.
CONSUMER_FAILING_ACTION: dict[str, tuple[tuple[str, int], ...]] = {
    "tools/gates/compare-output-budget.ps1": (
        (r"&\s*\$runner\s+@runnerArgs\s*\|\s*Out-Null\s*if\s*\(\$LASTEXITCODE\s+-ne\s+0\s+-or[^\n]*\)\s*\{\s*"
         r"(?P<act>\$failureReport\s*=\s*\[ordered\]@\{[\s\S]*?blockingVerdict\s*=\s*'INDETERMINATE')", 1),),
    "tools/profiling/lookassist-wb-determinism.ps1": (
        (r"\$smokeExit\s*=\s*\$LASTEXITCODE\s*if\s*\(\$smokeExit\s+-ne\s+0\)\s*\{\s*Write-Host\s*\([^\n]*\n\s*"
         r"(?P<act>\$rows\s*\+=[^\n]*reason\s*=\s*\"runner exit[^\n]*\n\s*continue\b)", 1),),
    "tools/profiling/lookassist-wb-multiclip-probe.ps1": (
        (r"\$smokeExit\s*=\s*\$LASTEXITCODE[\s\S]*?RunnerExit\s*=\s*\$smokeExit[\s\S]*?\$invalidRows\s*=\s*@\([\s\S]*?RunnerExit\s+-ne\s+0"
         r"[\s\S]*?(?m:^)[ \t]*(?P<act>exit\s+43)[ \t]*(?m:$)", 1),),
    "tools/profiling/measure-wb-solve-distribution.ps1": (
        (r"\$captureExit\s*=\s*\$LASTEXITCODE[\s\S]*?if\s*\(\$captureExit\s+-ne\s+0\b[^\n]*\{\s*(?P<act>\$invalid\+\+[\s\S]*?\bcontinue\b)", 1),
        (r"if\s*\(\$invalidTotal\s+-gt\s+0\)\s*\{[^}]*?(?P<act>;\s*exit\s+43\b)", 1)),
    "tools/profiling/review-dualiso-fullres-recon.ps1": (
        (r"Assert-GuiSmokeChildEvidenceReady\s+`?\s*-ExitCode\s+\$smokeExitCode[^{}]*\}\s*catch\s*\{\s*Write-Host[^\n]*\n\s*(?P<act>exit\s+2\b)", 1),),
    "tools/profiling/run-non-dual-iso-guard-smoke.ps1": (
        (r"\$smokeExit\s*=\s*\$LASTEXITCODE\s*if\s*\(\$smokeExit\s+-ne\s+0\)\s*\{\s*(?P<act>\$failures\.Add\()", 1),),
    "tools/profiling/run-release-cuda-playback-ab.ps1": (
        (r"if\s*\(\$baselineResult\.exitCode\s+-ne\s+0\)\s*\{\s*(?P<act>\$proofFailures\s*\+=)", 1),
        (r"if\s*\(\$candidateResult\.exitCode\s+-ne\s+0\)\s*\{\s*(?P<act>\$proofFailures\s*\+=)", 1)),
    "tools/profiling/run-ultramagnus-p3-validation.ps1": (
        (r"\$smokeExit\s*=\s*\$LASTEXITCODE[\s\S]*?if\s*\(\$smokeExit\s+-ne\s+0\)\s*\{\s*(?P<act>Add-Failure\s+\$clipFailures)", 1),),
    "tools/profiling/bachelor/playback-attr-3-cuda-job.ps1": (
        (r"\$smokeRc\s*=\s*\$LASTEXITCODE[\s\S]*?if\s*\(\$null\s+-ne\s+\$smokeLaunchException\s+-or\s+\$smokeRc\s+-ne\s+0[^\n]*\)\s*\{"
         r"[\s\S]*?result='SMOKE_RUN_FAILED'[\s\S]*?(?P<act>\bexit\s+18\b)", 1),),
    "tools/profiling/run-shipping-guard-smoke.ps1": (
        (r"\$code\s*=\s*\$LASTEXITCODE[\s\S]*?status\s*=\s*if\s*\(\$code\s+-eq\s+0\)\s*\{\s*\"success\"\s*\}\s*else\s*\{\s*(?P<act>\"failed\")", 1),),
    "tools/profiling/run-local-cuda-playback-dng-smoke.ps1": (
        (r"if\s*\(\$playbackChild\.exitCode\s+-ne\s+0\)\s*\{\s*(?P<act>Add-Failure\s+\$failures)", 1),
        (r"if\s*\(\$playbackAbChild\.exitCode\s+-ne\s+0\)\s*\{\s*(?P<act>Add-Failure\s+\$failures)", 1)),
    "tools/profiling/invoke-ultramagnus-p3-evidence.ps1": (
        (r"if\s*\(\$importResult\.exitCode\s+-ne\s+0\)\s*\{\s*(?P<act>Add-Failure\s+\$failures)", 2),),
    "tools/profiling/export-release-cuda-dogfood-kit.ps1": (
        (r"\$proofExit\s*=\s*\$LASTEXITCODE[\s\S]*?(?P<act>\bexit\s+\$proofExit\b)", 1),),
    # DUAL-VENUE-EVIDENCE-1 r2: a printed capture whose job exited non-zero is downgraded to INVALID.
    "tools/profiling/dual-venue/Invoke-VenueLeg.ps1": (
        (r"\$exitCode\s*=\s*\[int\]\$run\.result\.exitCode[\s\S]*?if\s*\(\$exitCode\s+-ne\s+0\s+-and\s+\$resolved\.outcome\s+-eq\s+'CAPTURED'\)\s*\{\s*"
         r"(?P<act>\$resolved\s*=\s*\[pscustomobject\]@\{\s*outcome\s*=\s*'INVALID')", 1),),
}


def failure_body_problems(code_by_rel: dict[str, str], table: dict | None = None) -> list[str]:
    """`code_by_rel`: rel -> comment-stripped, newline-normalised source. Reports every consumer whose exit check no longer
    has the failing action in its body."""
    table = CONSUMER_FAILING_ACTION if table is None else table
    problems: list[str] = []
    for rel, entries in sorted(table.items()):
        code = code_by_rel.get(rel)
        if code is None:
            problems.append(f"{rel}: pinned consumer not found")
            continue
        for pattern, minimum in entries:
            found = len(list(re.finditer(pattern, code)))
            if found < minimum:
                problems.append(f"{rel}: the exit check's BODY no longer fails the run ({found} of {minimum} pinned failing action(s) "
                                f"present): {pattern[:90]}...")
    return problems


class ConsumerFailureBodyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.code = {rel: re.sub(r"\r?\n", "\n", _strip_comments(text)) for rel, text in _consumer_sources().items()
                    if rel in CONSUMER_FAILING_ACTION}

    def test_every_pinned_consumer_has_a_failing_action_in_the_body_of_its_exit_check(self) -> None:
        self.assertEqual(failure_body_problems(self.code), [])

    def test_the_body_table_covers_every_header_pinned_consumer(self) -> None:
        from tools.repo_hygiene.test_playback_evidence_completeness import CONSUMER_ACTS_ON_EXIT
        self.assertEqual(sorted(CONSUMER_FAILING_ACTION), sorted(CONSUMER_ACTS_ON_EXIT))

    def test_emptying_the_body_of_any_exit_check_fails_the_scan(self) -> None:
        for rel, entries in sorted(CONSUMER_FAILING_ACTION.items()):
            for index, (pattern, minimum) in enumerate(entries):
                with self.subTest(rel=rel, check=index):
                    matches = list(re.finditer(pattern, self.code[rel]))
                    self.assertGreaterEqual(len(matches), minimum, f"the pin does not match the real source of {rel}")
                    start, end = matches[0].span("act")
                    mutated = self.code[rel][:start] + self.code[rel][end:]
                    problems = failure_body_problems({**self.code, rel: mutated})
                    self.assertTrue(any(rel in p for p in problems), f"emptying the body of {rel} check {index} survived the scan")

    def test_a_header_with_an_empty_body_is_the_exact_round_one_survivor(self) -> None:
        rel = "tools/profiling/run-ultramagnus-p3-validation.ps1"
        emptied = re.sub(r"(if\s*\(\$smokeExit\s+-ne\s+0\)\s*\{)\s*Add-Failure\s+\$clipFailures[^\n]*", r"\1", self.code[rel], count=1)
        self.assertNotEqual(emptied, self.code[rel])
        from tools.repo_hygiene.test_playback_evidence_completeness import CONSUMER_ACTS_ON_EXIT
        self.assertRegex(emptied, CONSUMER_ACTS_ON_EXIT[rel], "the header-only pin still matches: that was the round-1 gap")
        self.assertTrue(failure_body_problems({**self.code, rel: emptied}), "the body pin must not")


# ---- G. fable H3: the autoplay exit is observable offscreen --------------------------------------------------------------

class OffscreenProbeExitCodeTests(unittest.TestCase):
    TEXT = OFFSCREEN.read_text(encoding="utf-8")

    def test_no_entry_is_allowed_to_outlive_its_bound_or_skip_the_exit_assertion(self) -> None:
        code = _strip_comments(self.TEXT)
        self.assertNotIn("MayNotExit", code, "an entry that may not exit is an entry whose exit code is never asserted")
        self.assertIn("$exitOk = ($exitCode -eq [int]$ExpectExit)", code)

    def test_both_autoplay_entries_assert_the_latched_exit_14(self) -> None:
        for name in ("autoplay-env-hook", "autoplay-env-hook-24s-on-a-short-clip"):
            match = re.search(rf"Invoke-GateEntry -Name '{name}'([^\n]*)", self.TEXT)
            self.assertIsNotNone(match, name)
            self.assertIn("-ExpectExit 14", match.group(1), name)

    def test_the_probe_names_the_only_exit_zero_case_it_cannot_reach(self) -> None:
        self.assertIn("Reached", self.TEXT)

    def test_the_app_never_prompts_in_an_automation_run_and_exits_with_the_latched_code(self) -> None:
        window = MAIN_WINDOW.read_text(encoding="utf-8")
        self.assertIn("if( !m_automationVerdict.armed() && ui->actionAskForSavingOnQuit->isChecked() && SESSION_CLIP_COUNT != 0 )", window)
        self.assertIn("qApp->exit( playback_frame_range::AutomationVerdictLatch::kFailExitCode )", window)
        self.assertNotRegex(window, r"m_automationVerdict\.fail\(\);\s*if\( autoplayExit \) QTimer::singleShot\( 400, this, \[\]\(\)\{ qApp->quit\(\); \} \);")


if __name__ == "__main__":
    unittest.main()
