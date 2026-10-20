import hashlib
import json
import re
import struct
import tempfile
import unittest
import zlib
import shutil
import subprocess
from unittest import mock
from pathlib import Path

from tools.gates import output_budget

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_SPEC = REPO_ROOT / "tools/gates/output-budget.json"
WRAPPER = REPO_ROOT / "tools/gates/compare-output-budget.ps1"
RUNNER = REPO_ROOT / "tools/profiling/run-release-gui-smoke.ps1"
BINDINGS_DUMP = REPO_ROOT / "tools/gates/dump-runner-bindings.ps1"


def png_bytes(width, height, pixels):
    raw = b"".join(b"\x00" + bytes(pixels[y * width * 3:(y + 1) * width * 3]) for y in range(height))
    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


class OutputBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "tools/gates").mkdir(parents=True)
        self.shipping = self.root / "tools/gates/shipping-defaults.json"
        self.shipping.write_text(json.dumps({"playback": {"qualityMode": {"value": 1}, "scaleFactorOverride": {"value": 0}, "derived": {"initialScaleRequest": {"nonDualIso": 4, "dualIso": 4}}}}), encoding="utf-8")
        self.receipt = self.root / "receipt.bin"
        self.receipt.write_bytes(b"receipt")
        self.clip = self.root / "clip.mlv"
        self.clip.write_bytes(b"clip")
        self.base_png = self.root / "base.png"
        self.cand_png = self.root / "candidate.png"
        pixels = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120]
        self.base_png.write_bytes(png_bytes(2, 2, pixels))
        self.cand_png.write_bytes(png_bytes(2, 2, pixels))
        zero_values = {
            "maxAbsLumaP05Delta": 0.0, "maxAbsLumaP50Delta": 0.0, "maxAbsLumaP95Delta": 0.0,
            "maxAbsLumaMeanDelta": 0.0, "maxAbsVisibleGreenAxisDelta": 0.0, "maxAbsGreenArtifactRatioDelta": 0.0,
        }
        zero_pixels = {
            "maxMeanAbsRgb": 0.0, "maxP95AbsRgb": 0.0, "maxAbsRgb": 0, "maxChangedPixelRatioGt0": 0.0,
            "maxChangedPixelRatioGt2": 0.0, "maxChangedPixelRatioGt8": 0.0, "maxRmseRgb": 0.0,
        }
        self.spec = {
            "schema": output_budget.SCHEMA,
            "shippingDefaults": {"path": "tools/gates/shipping-defaults.json", "sha256": output_budget.sha256_file(self.shipping)},
            "baseline": {"status": "reviewed_instrumented_known_good", "commit": "a" * 40, "executableSha256": "A" * 64, "artifact": "fixture://reviewed-baseline"},
            "receipt": {"path": str(self.receipt), "length": 7, "sha256": output_budget.sha256_file(self.receipt)},
            "profiles": [{"id": "wb_locked", "disableLookAssist": True, "playbackProcessing": "subset", "qualityMode": "1", "expectedQualityMode": 1, "scaleFactor": "", "expectedScaleRequest": 4, "selectionAuthority": "shipping-default-controlled"}],
            "clips": [{"id": "clip", "required": True, "path": str(self.clip), "length": 4, "sha256": output_budget.sha256_file(self.clip), "startFrame": 4,
                       "parts": [{"path": str(self.clip), "length": 4, "sha256": output_budget.sha256_file(self.clip)}],
                       "aspect": {"mode": "presented-playback-stretch", "stretchX": 3.0, "stretchY": 1.0, "hStretchIndex": 0, "vStretchIndex": 3}}],
            "budgets": {"values": zero_values, "pixels": zero_pixels, "cadence": {"repeats": 3, "seconds": 30, "maxP99DeltaMs": 15.0, "maxHitchFractionDelta": 0.02, "authority": "advisory_only"}},
            "legacyPolicyAllowlist": [],
        }
        self.spec_path = self.root / "tools/gates/spec.json"
        self.spec_path.write_text(json.dumps(self.spec), encoding="utf-8")
        self.baseline = {"path": str(self.root / "base.exe"), "commit": "a" * 40, "sha256": "A" * 64}
        self.candidate = {"path": str(self.root / "candidate.exe"), "commit": "b" * 40, "sha256": "B" * 64}
        (self.root / "base.exe").write_bytes(b"MLVAPP_BUILDSTAMP_v1|sha=" + b"a" * 40 + b"|dirty=0")
        (self.root / "candidate.exe").write_bytes(b"MLVAPP_BUILDSTAMP_v1|sha=" + b"b" * 40 + b"|dirty=0")
        self.baseline["sha256"] = output_budget.sha256_file(self.root / "base.exe")
        self.candidate["sha256"] = output_budget.sha256_file(self.root / "candidate.exe")
        self.spec["baseline"]["executableSha256"] = self.baseline["sha256"]
        self.spec_path.write_text(json.dumps(self.spec), encoding="utf-8")
        self.base_result = self._write_result("base-result.json", self.baseline["path"], self.base_png)
        self.cand_result = self._write_result("candidate-result.json", self.candidate["path"], self.cand_png)

    def tearDown(self):
        self.temp.cleanup()

    def _stub_exe_validation(self):
        original = output_budget.validate_exe
        output_budget.validate_exe = lambda path, commit, digest, root: {"path": str(path), "commit": commit, "sha256": digest, "dirty": 0}
        self.addCleanup(setattr, output_budget, "validate_exe", original)

    def _write_result(self, name, exe, screenshot):
        provenance = {
            "schema": "mlvapp-gui-smoke-screenshot-provenance.v2", "validAssociation": True, "validFresh": True,
            "pathSource": "render_thread", "processed8CacheHit": 0, "displayFrame": 4, "requestedStartFrame": 4,
            "effectiveStartFrame": 4, "requestedFrame": 4, "presentationIndex": 1, "requestSerial": 2,
            "requestSerialOffset": 1, "screenshotMethod": "presented", "screenshotWidth": 2, "screenshotHeight": 2,
            "screenshotPath": str(screenshot),
            "presentedHistory": [{"index": 1, "displayFrame": 4, "serial": 2, "serialOffset": 1, "requestedFrame": 4, "generation": 8}],
            "effectiveState": {"visualState": {"scale": 4}, "playbackPolicy": {"processing": "none"}, "frame": {"scale": 4}, "renderManifest": {"path": "render_thread"}},
        }
        result = {
            "schema": "mlvapp-gui-smoke-result.v1", "exePath": exe, "clipPath": str(self.clip),
            "inputBindings": {
                "executable": {"path": exe, "length": Path(exe).stat().st_size, "sha256": output_budget.sha256_file(Path(exe)), "embeddedCommit": "a" * 40 if "base.exe" in exe else "b" * 40, "dirty": 0, "stampFound": True},
                "clipParts": [{"path": str(self.clip), "length": 4, "sha256": output_budget.sha256_file(self.clip)}],
                "receipt": None,
            },
            "validation": {"ok": True, "screenshotProvenance": provenance},
            "screenshot": {"path": str(screenshot), "capture": {"image": {"sha256": output_budget.sha256_file(screenshot)}}},
            "visualQuality": {"playbackPolicy": {"playback_processing_selected": "subset"}, "visualState": {"look_assist_enabled": 0, "quality_mode": 1, "scale_request": 4}, "aspectEvidence": {"mode": "presented-playback-stretch", "stretchX": 3.0, "stretchY": 1.0, "hStretchIndex": 0, "vStretchIndex": 3}},
        }
        path = self.root / name
        path.write_text(json.dumps(result), encoding="utf-8")
        return path

    def evidence(self):
        return {
            "schema": "mlvapp.output-budget-evidence.v1", "specSha256": output_budget.sha256_file(self.spec_path),
            "shippingDefaultsSha256": output_budget.sha256_file(self.shipping), "baseline": self.baseline, "candidate": self.candidate,
            "pairs": [{"clipId": "clip", "profileId": "wb_locked", "baselineResult": str(self.base_result), "candidateResult": str(self.cand_result)}],
            "cadence": None,
        }

    def test_identical_full_frame_passes_without_cadence_credit(self):
        original_validate_exe = output_budget.validate_exe
        output_budget.validate_exe = lambda path, commit, digest, root: {"path": str(path), "commit": commit, "sha256": digest, "dirty": 0}
        self.addCleanup(setattr, output_budget, "validate_exe", original_validate_exe)
        report, code = output_budget.evaluate(output_budget.validate_spec(self.spec), self.evidence(), self.spec_path)
        self.assertEqual(0, code)
        self.assertEqual("PASS", report["blockingVerdict"])
        self.assertEqual("INDETERMINATE", report["cadenceVerdict"])
        self.assertTrue(report["authorizing"])

    def test_last_pixel_change_is_not_hidden_by_sampling(self):
        original_validate_exe = output_budget.validate_exe
        output_budget.validate_exe = lambda path, commit, digest, root: {"path": str(path), "commit": commit, "sha256": digest, "dirty": 0}
        self.addCleanup(setattr, output_budget, "validate_exe", original_validate_exe)
        changed = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 121]
        self.cand_png.write_bytes(png_bytes(2, 2, changed))
        self.cand_result = self._write_result("candidate-result.json", self.candidate["path"], self.cand_png)
        report, code = output_budget.evaluate(output_budget.validate_spec(self.spec), self.evidence(), self.spec_path)
        self.assertEqual(3, code)
        self.assertEqual("FAIL", report["blockingVerdict"])
        self.assertFalse(report["authorizing"])

    def test_transaction_history_mismatch_blocks(self):
        original_validate_exe = output_budget.validate_exe
        output_budget.validate_exe = lambda path, commit, digest, root: {"path": str(path), "commit": commit, "sha256": digest, "dirty": 0}
        self.addCleanup(setattr, output_budget, "validate_exe", original_validate_exe)
        result = json.loads(self.cand_result.read_text())
        result["validation"]["screenshotProvenance"]["requestSerial"] = 3
        self.cand_result.write_text(json.dumps(result), encoding="utf-8")
        report, code = output_budget.evaluate(output_budget.validate_spec(self.spec), self.evidence(), self.spec_path)
        self.assertEqual(3, code)
        self.assertTrue(any("requestSerial mismatch" in item for item in report["failures"]))

    def test_replacement_baseline_is_rejected_even_when_clean_and_distinct(self):
        evidence = self.evidence()
        evidence["baseline"]["commit"] = "c" * 40
        evidence["baseline"]["sha256"] = "C" * 64
        with self.assertRaises(output_budget.ContractError):
            output_budget.evaluate(output_budget.validate_spec(self.spec), evidence, self.spec_path)

    def test_supplied_evidence_cannot_bypass_pinned_start_frame(self):
        self._stub_exe_validation()
        result = json.loads(self.cand_result.read_text())
        result["validation"]["screenshotProvenance"]["requestedStartFrame"] = 999
        result["validation"]["screenshotProvenance"]["effectiveStartFrame"] = 999
        self.cand_result.write_text(json.dumps(result), encoding="utf-8")
        with self.assertRaises(output_budget.ContractError):
            output_budget.evaluate(output_budget.validate_spec(self.spec), self.evidence(), self.spec_path)

    def test_stale_result_cannot_be_rebound_to_replaced_executable_bytes(self):
        self._stub_exe_validation()
        result = json.loads(self.cand_result.read_text())
        result["inputBindings"]["executable"]["sha256"] = "C" * 64
        self.cand_result.write_text(json.dumps(result), encoding="utf-8")
        with self.assertRaises(output_budget.ContractError):
            output_budget.evaluate(output_budget.validate_spec(self.spec), self.evidence(), self.spec_path)

    def test_committed_receipt_profile_requires_exact_launch_time_receipt_binding(self):
        self._stub_exe_validation()
        self.spec["profiles"][0]["playbackProcessing"] = "receipt"
        self.spec_path.write_text(json.dumps(self.spec), encoding="utf-8")
        for path in (self.base_result, self.cand_result):
            result = json.loads(path.read_text())
            result["visualQuality"]["playbackPolicy"]["playback_processing_selected"] = "receipt"
            path.write_text(json.dumps(result), encoding="utf-8")
        with self.assertRaises(output_budget.ContractError):
            output_budget.evaluate(output_budget.validate_spec(self.spec), self.evidence(), self.spec_path)

    def test_runtime_failure_atomically_replaces_stale_authorizing_report(self):
        evidence_path = self.root / "evidence.json"
        evidence_path.write_text(json.dumps(self.evidence()), encoding="utf-8")
        report_path = self.root / "report.json"
        report_path.write_text('{"authorizing":true}', encoding="utf-8")
        argv = ["output_budget.py", "evaluate", "--spec", str(self.spec_path), "--evidence", str(evidence_path), "--output", str(report_path)]
        with mock.patch("sys.argv", argv), mock.patch.object(output_budget, "evaluate", side_effect=OSError("input disappeared")):
            self.assertEqual(2, output_budget.main())
        replacement = json.loads(report_path.read_text())
        self.assertFalse(replacement["authorizing"])
        self.assertIn("input disappeared", replacement["failures"][0])

    def test_evidence_only_wrapper_is_non_authorizing_and_nonzero(self):
        wrapper = (Path(__file__).parent / "compare-output-budget.ps1").read_text(encoding="utf-8")
        block = wrapper.split("if ($EvidenceOnly", 1)[1]
        self.assertIn("$report.authorizing = $false", block)
        self.assertIn("exit 2", block)

    @unittest.skipUnless(shutil.which("pwsh"), "PowerShell 7 is required for the wrapper integration test")
    def test_failed_wrapper_preflight_replaces_stale_authorizing_report(self):
        repo_root = Path(__file__).resolve().parents[2]
        state_root = repo_root / ".claude-state"
        state_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=state_root) as output:
            output_path = Path(output)
            report_path = output_path / "report.json"
            report_path.write_text('{"authorizing":true,"marker":"stale"}', encoding="utf-8")
            command = [
                shutil.which("pwsh"), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(repo_root / "tools/gates/compare-output-budget.ps1"),
                "-RepoRoot", str(repo_root), "-SpecPath", "tools/gates/output-budget.json",
                "-BaselineExe", str(repo_root / "missing-baseline.exe"), "-BaselineCommit", "a" * 40, "-BaselineSha256", "A" * 64,
                "-CandidateExe", str(repo_root / "missing-candidate.exe"), "-CandidateCommit", "b" * 40, "-CandidateSha256", "B" * 64,
                "-OutputRoot", str(output_path.relative_to(repo_root)).replace("\\", "/"),
            ]
            completed = subprocess.run(command, cwd=repo_root, capture_output=True, text=True)
            self.assertEqual(4, completed.returncode, completed.stdout + completed.stderr)
            report = json.loads(report_path.read_text(encoding="utf-8-sig"))
            self.assertFalse(report["authorizing"])
            self.assertNotIn("marker", report)

    def test_unbound_cadence_is_rejected_and_cannot_mute_pixel_failure(self):
        original_validate_exe = output_budget.validate_exe
        output_budget.validate_exe = lambda path, commit, digest, root: {"path": str(path), "commit": commit, "sha256": digest, "dirty": 0}
        self.addCleanup(setattr, output_budget, "validate_exe", original_validate_exe)
        self.cand_png.write_bytes(png_bytes(2, 2, [10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 121]))
        self.cand_result = self._write_result("candidate-result.json", self.candidate["path"], self.cand_png)
        evidence = self.evidence()
        evidence["cadence"] = {"baselineP99Ms": 10.0, "candidateP99Ms": 30.0, "baselineHitchFraction": 0.0, "candidateHitchFraction": 0.03, "repeats": 3}
        with self.assertRaises(output_budget.ContractError):
            output_budget.evaluate(output_budget.validate_spec(self.spec), evidence, self.spec_path)

    def test_duplicate_key_and_bool_integer_are_rejected(self):
        duplicate = self.root / "duplicate.json"
        duplicate.write_text('{"schema":"x","schema":"y"}', encoding="utf-8")
        with self.assertRaises(output_budget.ContractError):
            output_budget.load_json(duplicate)
        broken = json.loads(json.dumps(self.spec))
        broken["clips"][0]["startFrame"] = True
        with self.assertRaises(output_budget.ContractError):
            output_budget.validate_spec(broken)

    def test_required_unacquired_clip_is_a_hard_preflight_block(self):
        broken = json.loads(json.dumps(self.spec))
        broken["clips"][0]["length"] = None
        broken["clips"][0]["sha256"] = None
        broken["clips"][0]["parts"] = []
        broken["clips"][0]["evidenceStatus"] = "required_corpus_not_acquired"
        self.spec_path.write_text(json.dumps(broken), encoding="utf-8")
        report, code = output_budget.preflight(self.spec_path, self.root, None, None)
        self.assertEqual(4, code)
        self.assertFalse(report["authorizing"])
        self.assertTrue(any("no pinned length/hash" in item for item in report["failures"]))

    def _preflight_codes(self, spec):
        self.spec_path.write_text(json.dumps(spec), encoding="utf-8")
        report, code = output_budget.preflight(self.spec_path, self.root, None, None)
        return report, code, sorted((item["code"], item.get("clipId")) for item in report["typedFailures"])

    def _unpinned(self):
        spec = json.loads(json.dumps(self.spec))
        spec["baseline"] = {"status": "pending_instrumented_known_good_bridge", "commit": None, "executableSha256": None, "artifact": None}
        return spec

    def test_missing_receipt_is_its_own_code_even_while_the_baseline_is_unpinned(self):
        spec = self._unpinned()
        spec["receipt"]["path"] = str(self.root / "no-such-receipt.marxml")
        report, code, codes = self._preflight_codes(spec)
        self.assertEqual(4, code)
        self.assertEqual([("BASELINE_NOT_PINNED", None), ("RECEIPT_MISSING", None)], codes)
        self.assertEqual(len(report["failures"]), len(report["typedFailures"]))

    def test_receipt_length_and_hash_mismatch_are_distinct_codes(self):
        spec = self._unpinned()
        spec["receipt"]["length"] = 8
        self.assertIn(("RECEIPT_LENGTH_MISMATCH", None), self._preflight_codes(spec)[2])
        spec["receipt"]["length"] = 7
        spec["receipt"]["sha256"] = "0" * 64
        self.assertIn(("RECEIPT_HASH_MISMATCH", None), self._preflight_codes(spec)[2])

    def test_unacquired_clip_and_unpinned_baseline_are_separate_entries(self):
        spec = self._unpinned()
        spec["clips"][0].update({"length": None, "sha256": None, "parts": [], "evidenceStatus": "required_corpus_not_acquired"})
        _, code, codes = self._preflight_codes(spec)
        self.assertEqual(4, code)
        self.assertEqual([("BASELINE_NOT_PINNED", None), ("REQUIRED_CLIP_NOT_ACQUIRED", "clip")], codes)

    def test_unacquired_clip_alone_is_reported_without_a_baseline_code(self):
        spec = json.loads(json.dumps(self.spec))
        spec["clips"][0].update({"length": None, "sha256": None, "parts": [], "evidenceStatus": "required_corpus_not_acquired"})
        _, code, codes = self._preflight_codes(spec)
        self.assertEqual(4, code)
        self.assertEqual([("REQUIRED_CLIP_NOT_ACQUIRED", "clip")], codes)

    def test_missing_clip_part_is_typed_when_the_contract_is_otherwise_clear(self):
        spec = json.loads(json.dumps(self.spec))
        missing = str(self.root / "missing-part.bin")
        spec["clips"][0]["path"] = missing
        spec["clips"][0]["parts"][0]["path"] = missing
        _, code, codes = self._preflight_codes(spec)
        self.assertEqual(4, code)
        self.assertEqual([("CLIP_PART_MISSING", "clip")], codes)

    def test_relative_receipt_path_resolves_against_the_repo_root_and_cannot_escape_it(self):
        (self.root / "tools/gates/receipts").mkdir()
        (self.root / "tools/gates/receipts/r.marxml").write_bytes(b"receipt")
        spec = json.loads(json.dumps(self.spec))
        spec["receipt"]["path"] = "tools/gates/receipts/r.marxml"
        report, code, _ = self._preflight_codes(spec)
        self.assertEqual(0, code, report["failures"])
        for bad in ("../outside.marxml", "tools/../../outside.marxml"):
            spec["receipt"]["path"] = bad
            with self.assertRaises(output_budget.ContractError):
                output_budget.validate_spec(spec)


def _runner_bindings():
    """Parse-only dump of the wrapper's runner call and the runner's param block (nothing is launched)."""
    completed = subprocess.run(
        [shutil.which("pwsh"), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(BINDINGS_DUMP),
         "-RunnerPath", str(RUNNER), "-WrapperPath", str(WRAPPER)],
        capture_output=True, text=True)
    if completed.returncode != 0:
        raise AssertionError(f"runner binding dump failed: {completed.stdout}{completed.stderr}")
    return json.loads(completed.stdout)


class RealSpecBindingTests(unittest.TestCase):
    """OUTPUT-BUDGET-GATE-BINDABLE-1: the committed spec must be runnable, not just well-formed."""

    @classmethod
    def setUpClass(cls):
        cls.spec = output_budget.validate_spec(output_budget.load_json(REAL_SPEC))

    @unittest.skipUnless(shutil.which("pwsh"), "PowerShell 7 is required for the static runner-binding check")
    def test_every_profile_binds_to_the_smoke_runner_parameters(self):
        bindings = _runner_bindings()
        declared = bindings["runnerParams"]
        profile_field = {}
        for key, expression in bindings["wrapperArgs"].items():
            match = re.fullmatch(r"\[\w+\]\s*\$profile\.(\w+)", expression.strip())
            if match:
                profile_field[key] = match.group(1)
        self.assertIn("PlaybackProcessing", profile_field, "the wrapper must still derive -PlaybackProcessing from the profile")
        self.assertTrue(self.spec["profiles"])
        for profile in self.spec["profiles"]:
            active = set(bindings["wrapperArgs"])
            for guarded in bindings["guardedArgs"]:
                if f"'{profile['playbackProcessing']}'" in guarded["guard"]:
                    active.add(guarded["key"])
            for key in sorted(active):
                self.assertIn(key, declared, f"profile {profile['id']}: the wrapper splats -{key}, which the runner does not declare")
            for key, field in profile_field.items():
                allowed = declared[key]["validateSet"]
                if allowed is not None:
                    self.assertIn(str(profile[field]).lower(), [item.lower() for item in allowed],
                                  f"profile {profile['id']}: -{key} {profile[field]!r} is outside the runner's ValidateSet {allowed}")

    def test_profile_processing_is_a_value_the_app_can_report(self):
        # the evaluator compares the profile value with the app's playback_processing_selected, which MainWindow
        # selectedPlaybackProcessingLabel() renders as exactly "subset" or "receipt".
        for profile in self.spec["profiles"]:
            self.assertIn(profile["playbackProcessing"], ("subset", "receipt"), profile["id"])

    def test_none_is_not_an_accepted_playback_processing(self):
        broken = json.loads(REAL_SPEC.read_text(encoding="utf-8"))
        broken["profiles"][0]["playbackProcessing"] = "none"
        with self.assertRaises(output_budget.ContractError):
            output_budget.validate_spec(broken)

    def test_receipt_is_a_reachable_tracked_file_matching_its_pin(self):
        path = output_budget.resolve_receipt_path(self.spec, REPO_ROOT)
        self.assertTrue(path.is_file(), f"receipt is not reachable: {path}")
        output_budget.validate_file(path, self.spec["receipt"]["length"], self.spec["receipt"]["sha256"], "receipt")
        tracked = subprocess.run(["git", "-C", str(REPO_ROOT), "ls-files", "--error-unmatch", "--", path.relative_to(REPO_ROOT).as_posix()], capture_output=True, text=True)
        self.assertEqual(0, tracked.returncode, "the pinned receipt must be tracked, not an ignored local copy")

    @unittest.skipUnless(shutil.which("git"), "git is required for the attribute check")
    def test_receipt_bytes_are_immune_to_autocrlf(self):
        relative = output_budget.resolve_receipt_path(self.spec, REPO_ROOT).relative_to(REPO_ROOT).as_posix()
        attrs = subprocess.run(["git", "-C", str(REPO_ROOT), "check-attr", "text", "eol", "--", relative], capture_output=True, text=True).stdout
        self.assertIn("eol: lf", attrs)
        self.assertIn("text: set", attrs)

    def test_real_preflight_names_each_block_separately(self):
        report, code = output_budget.preflight(REAL_SPEC, REPO_ROOT, None, None)
        self.assertEqual(4, code)
        self.assertFalse(report["authorizing"])
        codes = [(item["code"], item.get("clipId")) for item in report["typedFailures"]]
        self.assertIn(("BASELINE_NOT_PINNED", None), codes)
        self.assertIn(("REQUIRED_CLIP_NOT_ACQUIRED", "M02-1344"), codes)
        self.assertFalse([item for item in codes if item[0].startswith("RECEIPT_")], "the receipt is reachable, so it must not be reported")
        self.assertEqual(len(report["failures"]), len(report["typedFailures"]))


if __name__ == "__main__":
    unittest.main()
