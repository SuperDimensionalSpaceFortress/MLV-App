"""VENUE-REFERENCE-FRAME-JOB-1 -- a tracked venue job runs capture-reference-frame.ps1 under the gate profile.

OUTPUT-BUDGET-KNOWN-GOOD-PROPOSAL-1 (design NO_GO, 2026-10-19) found that no tracked venue job runs
tools/profiling/capture-reference-frame.ps1 and that its argv does not match the wb_locked gate profile (start frame 0 where
the gate compares frame 120, no Look Assist or processing flags). This file pins the fix, with fixtures only (no venue, no
staging, no app launch, no capture):

  1. the generator (tools/profiling/bachelor/reference-frame-runner-job.ps1) bakes EVERY dependency of the capture script as a
     hash-pinned bundle, the way attr3-stage-smoke-runner-job.ps1 does, and the emitted job re-verifies it;
  2. it refuses, with a typed code and BEFORE it writes a job, a clip that Get-DvClipAdmission does not admit;
  3. the gate profile (Look Assist off, processing mode, quality mode, start frame) is read from the committed
     tools/gates/output-budget.json and reaches the capture command line in the job;
  4. capture-reference-frame.ps1's new arguments default to today's argv byte for byte, they reach the app's argv when given,
     and its receipt check (source_advanced, run nonce, exit 43) still refuses under them.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from tools.repo_hygiene import test_playback_launcher_receipt_oracle as oracle_tests
from tools.repo_hygiene.test_playback_clip_length_gate import PWSH, ROOT, _q, requires_pwsh

GENERATOR = ROOT / "tools" / "profiling" / "bachelor" / "reference-frame-runner-job.ps1"
CAPTURE = oracle_tests.CAPTURE
EXIT_INVALID = oracle_tests.EXIT_INVALID
REFUSED_EXIT = 21
CLOSURE = ("tools/profiling/capture-reference-frame.ps1", "tools/profiling/gui-smoke-length-gate.ps1")
CLIP_ID = "M16-1243"
MANIFEST_SHA = "ab" * 32
CONSENT_SCHEMA = "mlv-app/dual-venue-clip-consent/v1"
requires_windows = unittest.skipUnless(os.name == "nt", "a Windows venue tool: drive-letter agent roots, Win32 file identity in the job's owned-root helpers")


def _git(*args: str, cwd: Path = ROOT) -> bytes:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, check=True).stdout


def _run(command: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout)


class _GeneratorCase(unittest.TestCase):
    """A shared clone of this repository at HEAD (the committed consent, venue table and gate spec) and a way to run the generator on it."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="refframe-gen-")
        cls.tmp = Path(os.path.realpath(cls._tmp.name))
        cls.head = _git("rev-parse", "HEAD").decode().strip()
        cls.repo = cls.tmp / "repo"
        subprocess.run(["git", "clone", "-q", "--shared", "--no-checkout", str(ROOT), str(cls.repo)], check=True)
        subprocess.run(["git", "-C", str(cls.repo), "sparse-checkout", "set", "--cone", "tools"], check=True)
        subprocess.run(["git", "-C", str(cls.repo), "checkout", "-q", cls.head], check=True)
        # the clip resolver reads the spec from the full ref refs/remotes/fork/master, never the working tree
        subprocess.run(["git", "-C", str(cls.repo), "update-ref", "refs/remotes/fork/master", cls.head], check=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def generate(self, name: str, *extra: str, clip_id: str = CLIP_ID) -> tuple[subprocess.CompletedProcess, Path]:
        out = self.tmp / f"{name}.job.ps1"
        proc = _run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(GENERATOR),
                     "-SourceCommit", self.head, "-BuildManifestSha256", MANIFEST_SHA, "-ClipId", clip_id,
                     "-RepoRoot", str(self.repo), "-OutFile", str(out), *extra])
        return proc, out

    def emitted(self, name: str = "fix") -> str:
        proc, out = self.generate(name)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("RESULT=REFERENCE_FRAME_JOB_EMITTED", proc.stdout)
        return out.read_text(encoding="utf-8")

    @staticmethod
    def bundle(job: str) -> dict[str, tuple[str, str]]:
        return {m.group(1): (m.group(2), m.group(3))
                for m in re.finditer(r"name = '([^']+)'; sha256 = '([0-9a-f]{64})'; base64 = '([A-Za-z0-9+/=]+)'", job)}

@requires_pwsh
@requires_windows
class GeneratorTests(_GeneratorCase):
    """The generator, executed. Windows only: it is a Windows venue tool (Join-Path '..\\dual-venue', drive-letter agent roots)."""

    # --- 1. the bundle is hash-pinned ------------------------------------------------------------------------------------------------------------
    def test_the_generated_job_pins_every_dependency_of_the_capture_script_by_committed_hash(self) -> None:
        job = self.emitted()
        pinned = self.bundle(job)
        self.assertEqual(sorted(pinned), sorted(Path(rel).name for rel in CLOSURE))
        for rel in CLOSURE:
            committed = _git("show", f"{self.head}:{rel}")
            sha, payload = pinned[Path(rel).name]
            self.assertEqual(sha, hashlib.sha256(committed).hexdigest(), f"{rel}: the pin is not the committed bytes' sha256")
            self.assertEqual(base64.b64decode(payload), committed, f"{rel}: the embedded bytes are not the committed bytes")

    def test_the_capture_scripts_own_load_sites_are_all_inside_the_pinned_bundle(self) -> None:
        # every sibling capture-reference-frame.ps1 dot-sources must be a bundle member, or the job dies at its own first load line
        text = _git("show", f"{self.head}:{CLOSURE[0]}").decode("utf-8")
        loaded = set(re.findall(r"Join-Path \$PSScriptRoot '([^']+)'", text))
        self.assertTrue(loaded, "the capture script no longer loads a sibling: this assertion is stale")
        self.assertLessEqual(loaded, {Path(rel).name for rel in CLOSURE})

    def test_the_job_pins_the_build_the_commit_the_clip_and_the_gate_profile(self) -> None:
        job = self.emitted()
        self.assertIn(f"$SourceCommit = '{self.head}'", job)
        self.assertIn(f"$ToolsCommit = '{self.head}'", job)
        self.assertIn(f"$BuildManifestSha256 = '{MANIFEST_SHA}'", job)
        self.assertIn(f"$ClipId = '{CLIP_ID}'", job)
        self.assertIn("$GateProfileId = 'wb_locked'", job)

    def test_the_build_commit_and_the_tooling_commit_are_two_different_pins(self) -> None:
        # OUTPUT-BUDGET-KNOWN-GOOD-PROPOSAL-2 stages an OLD build (caf2c612) but needs today's capture script: the bundle comes from the tooling commit.
        old_build = "caf2c612f38bfd21d396ddf04ff65f3b2fdcd5c3"
        out = self.tmp / "twocommits.job.ps1"
        proc = _run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(GENERATOR),
                     "-SourceCommit", old_build, "-BuildManifestSha256", MANIFEST_SHA, "-ClipId", CLIP_ID, "-RepoRoot", str(self.repo), "-OutFile", str(out)])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        job = out.read_text(encoding="utf-8")
        self.assertIn(f"$SourceCommit = '{old_build}'", job)
        self.assertIn(f"$ToolsCommit = '{self.head}'", job)
        self.assertIn(f"MLVApp-playback-attr-3-cuda-{old_build[:12]}.exe", job, "the staged build's cache names come from the build commit")
        for rel in CLOSURE:
            self.assertEqual(self.bundle(job)[Path(rel).name][0], hashlib.sha256(_git("show", f"{self.head}:{rel}")).hexdigest())

    def test_a_tooling_commit_whose_capture_script_predates_the_gate_profile_is_refused(self) -> None:
        # a throwaway clone with one extra commit that removes -StartFrame from the capture script: the job would die at parameter binding on a venue
        clone = self.tmp / "predates-repo"
        subprocess.run(["git", "clone", "-q", "--shared", "--no-checkout", str(ROOT), str(clone)], check=True)
        subprocess.run(["git", "-C", str(clone), "sparse-checkout", "set", "--cone", "tools"], check=True)
        subprocess.run(["git", "-C", str(clone), "checkout", "-q", self.head], check=True)
        subprocess.run(["git", "-C", str(clone), "update-ref", "refs/remotes/fork/master", self.head], check=True)
        script = clone / "tools" / "profiling" / "capture-reference-frame.ps1"
        text = script.read_bytes().decode("utf-8")
        mutated = text.replace("[ValidateRange(0, 2147483647)][int]$StartFrame = 0,", "")
        self.assertNotEqual(mutated, text, "mutation anchor vanished")
        script.write_bytes(mutated.encode("utf-8"))
        subprocess.run(["git", "-C", str(clone), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-a", "-m", "predate"], check=True)
        old_tools = _git("rev-parse", "HEAD", cwd=clone).decode().strip()
        out = self.tmp / "predates.job.ps1"
        proc = _run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(GENERATOR),
                     "-SourceCommit", self.head, "-ToolsCommit", old_tools, "-BuildManifestSha256", MANIFEST_SHA, "-ClipId", CLIP_ID,
                     "-RepoRoot", str(clone), "-OutFile", str(out)])
        self.refused(proc, "REFERENCE_FRAME_CAPTURE_SCRIPT_PREDATES_GATE_PROFILE")
        self.assertFalse(out.exists())

    def test_verify_only_accepts_the_pinned_bundle_and_writes_nothing(self) -> None:
        proc, out = self.generate("verify")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        before = set(self.tmp.iterdir())
        ran = _run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(out), "-VerifyOnly"])
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertIn("RESULT=VERIFY_ONLY_OK", ran.stdout)
        self.assertEqual(set(self.tmp.iterdir()), before)

    def test_a_tampered_bundle_is_refused_before_a_byte_is_written(self) -> None:
        proc, out = self.generate("tamper")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        job = out.read_text(encoding="utf-8")
        name, (sha, payload) = next(iter(self.bundle(job).items()))
        raw = bytearray(base64.b64decode(payload))
        raw[0] ^= 0x01
        forged = base64.b64encode(bytes(raw)).decode("ascii")
        out.write_text(job.replace(payload, forged, 1), encoding="utf-8")
        ran = _run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(out), "-VerifyOnly"])
        self.assertNotEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertIn("RESULT=REFERENCE_FRAME_FAILED STEP=bundleDecode", ran.stdout)
        self.assertIn(name, ran.stdout)

    # --- 2. consent -----------------------------------------------------------------------------------------------------------------------------
    def refused(self, proc: subprocess.CompletedProcess, code: str) -> None:
        self.assertEqual(proc.returncode, REFUSED_EXIT, proc.stdout + proc.stderr)
        self.assertIn(f"RESULT=REFERENCE_FRAME_JOB_REFUSED CODE={code}", proc.stdout)

    def offline_inputs(self, records: list | None, blankets: list | None = None) -> tuple[Path, Path]:
        consent = self.tmp / "consent.json"
        doc: dict = {"schema": CONSENT_SCHEMA, "records": records or []}
        if blankets is not None:
            doc["blanketRecords"] = blankets
        consent.write_text(json.dumps(doc), encoding="utf-8")
        table = self.tmp / "venues.json"
        shutil.copy(self.repo / "tools" / "profiling" / "dual-venue" / "venues.json", table)
        return consent, table

    def test_a_clip_with_no_consent_record_is_refused_with_a_typed_code_and_no_job_is_written(self) -> None:
        consent, table = self.offline_inputs(records=[])
        proc, out = self.generate("noconsent", "-OfflineTestMode", "-ConsentPath", str(consent), "-VenueTablePath", str(table))
        self.refused(proc, "VENUE_CLIP_CONSENT_ABSENT")
        self.assertFalse(out.exists(), "a refused clip must not leave a job behind")

    def test_a_broken_consent_file_fails_closed_with_its_own_code(self) -> None:
        consent = self.tmp / "broken-consent.json"
        consent.write_text("{ not json", encoding="utf-8")
        _, table = self.offline_inputs(records=[])
        proc, out = self.generate("brokenconsent", "-OfflineTestMode", "-ConsentPath", str(consent), "-VenueTablePath", str(table))
        self.refused(proc, "VENUE_CLIP_CONSENT_INVALID")
        self.assertFalse(out.exists())

    def test_a_tracked_fixture_is_refused_by_the_length_gate_never_played(self) -> None:
        proc, out = self.generate("fixture", clip_id="tiny_dual_iso")
        self.assertEqual(proc.returncode, REFUSED_EXIT, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"RESULT=REFERENCE_FRAME_JOB_REFUSED CODE=FIXTURE_REFUSED_[A-Z_]+")
        self.assertFalse(out.exists())

    def test_the_committed_blanket_record_admits_the_clip_and_the_consent_file_is_never_written(self) -> None:
        consent = self.repo / "tools" / "profiling" / "dual-venue" / "venue-clip-consent.json"
        before = hashlib.sha256(consent.read_bytes()).hexdigest()
        job = self.emitted("blanket")
        self.assertIn("$AdmissionMode = 'production'", job)
        self.assertEqual(hashlib.sha256(consent.read_bytes()).hexdigest(), before)
        self.assertEqual(_git("status", "--porcelain", cwd=self.repo).decode().strip(), "", "the generator dirtied the repository")

    def test_an_offline_test_job_can_never_run(self) -> None:
        owner = {"venue": "bachelor", "clipId": CLIP_ID, "ownerLine": f"CLIP bachelor: {CLIP_ID}",
                 "ownerLineSha256": hashlib.sha256(f"CLIP bachelor: {CLIP_ID}".encode()).hexdigest(),
                 "recordedUtc": "2026-10-01T22:20:00Z", "recordedBy": "owner"}
        consent, table = self.offline_inputs(records=[owner])
        proc, out = self.generate("offline", "-OfflineTestMode", "-ConsentPath", str(consent), "-VenueTablePath", str(table))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        job = out.read_text(encoding="utf-8")
        self.assertIn("$AdmissionMode = 'offline-test'", job)
        ran = _run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(out)])
        self.assertNotEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertIn("OFFLINE_TEST_JOB_REFUSED", ran.stdout)

    def test_the_test_seams_are_closed_in_production(self) -> None:
        consent, table = self.offline_inputs(records=[])
        proc, out = self.generate("seam", "-ConsentPath", str(consent))
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("TEST_SEAM_IN_PRODUCTION", proc.stdout + proc.stderr)
        self.assertFalse(out.exists())

    def test_a_clip_the_gate_spec_does_not_name_has_no_gate_frame_and_is_refused(self) -> None:
        proc, out = self.generate("notingate", clip_id="M99-9999")
        self.refused(proc, "REFERENCE_FRAME_CLIP_NOT_IN_GATE_SPEC")
        self.assertFalse(out.exists())

    # --- 3. the gate profile reaches the capture command line --------------------------------------------------------------------------------------
    def test_the_gate_profile_reaches_the_capture_command_line_in_the_job(self) -> None:
        spec = json.loads(_git("show", f"{self.head}:tools/gates/output-budget.json"))
        profile = next(p for p in spec["profiles"] if p["id"] == "wb_locked")
        clip = next(c for c in spec["clips"] if c["id"] == CLIP_ID)
        self.assertTrue(profile["disableLookAssist"])
        job = self.emitted()
        # the profile's values are baked from the committed spec ...
        self.assertIn(f"$StartFrame = {int(clip['startFrame'])}", job)
        self.assertIn(f"$PlaybackProcessing = '{profile['playbackProcessing']}'", job)
        self.assertIn(f"$QualityMode = '{profile['qualityMode']}'", job)
        self.assertIn("$DisableLookAssist = $true", job)
        # ... and are the arguments of the one capture invocation
        invocation = next(line for line in job.splitlines() if line.startswith("$captureCommand =") and "-Exe" in line)
        self.assertIn("-StartFrame $StartFrame", invocation)
        self.assertIn("-ProfileId $GateProfileId", invocation)
        block = job[job.index(invocation):job.index("$captureOutput =")]
        for needle in ("-DisableLookAssist", "-PlaybackProcessing $PlaybackProcessing", "MLVAPP_PLAYBACK_QUALITY_MODE"):
            self.assertIn(needle, block)
        # the one capture invocation runs the script out of the verified bundle, never a copy found on the venue
        self.assertIn("$bundleCapture = Join-Path $BundleDir 'capture-reference-frame.ps1'", job)

    def test_the_job_acts_on_the_capture_exit_code_and_never_calls_the_candidate_reviewed(self) -> None:
        job = self.emitted()
        self.assertRegex(job, r"\$captureRc\s*=\s*\$LASTEXITCODE[\s\S]*?\$captureRc\s+-ne\s+0")
        # fail closed: a capture command that produced no exit code (a parameter that did not bind) is exit 70, never `exit $null` (= 0)
        self.assertIn("if ($null -eq $LASTEXITCODE) { exit 70 }", job)
        self.assertNotIn("reviewed_instrumented_known_good", job)

    def test_the_generator_never_launches_the_app_or_writes_the_consent_file(self) -> None:
        code = re.sub(r"(?m)^\s*#.*$", "", GENERATOR.read_text(encoding="utf-8"))
        for forbidden in ("Start-Process", "MLVApp.exe", "Set-Content", "Out-File", "venue-clip-consent.json'"):
            self.assertNotIn(forbidden, code, f"the generator must not contain {forbidden!r}")


@requires_pwsh
@requires_windows
class EmittedJobOnAFixtureAgentRootTests(_GeneratorCase):
    """The emitted job RUN on a throwaway agent root (no venue, no app, no capture): the venue-side steps up to the first thing a fixture cannot
    provide. Every failure is typed, names no path, and leaves the capture script unlaunched."""

    def setUp(self) -> None:
        self.agent = self.tmp / ("agent-" + self._testMethodName[-24:])
        (self.agent / "cache").mkdir(parents=True)
        self.short = self.head[:12]

    def stage_build(self, *, manifest_commit: str | None = None) -> str:
        """A cache holding the four files of a staged build, as the assembler and the stage job leave them; returns build.json's sha256."""
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w") as archive:   # the release package: the unrenamed build name inside, a stand-in (never executed)
            archive.writestr("MLVApp-release/MLVApp.exe", b"stand-in")
        files = {f"MLVApp-playback-attr-3-cuda-{self.short}-pkg.zip": package.getvalue(), f"MLVApp-playback-attr-3-cuda-{self.short}.exe": b"exe",
                 f"igpu_recon_cuda-playback-attr-3-cuda-{self.short}.dll": b"dll"}
        for name, data in files.items():
            (self.agent / "cache" / name).write_bytes(data)
        sha = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
        manifest = {"sourceCommit": manifest_commit or self.head, "dllPairManifestSha256": "cd" * 32, "pendingSymbolPresence": False,
                    "packageZip": {"sha256": sha[f"MLVApp-playback-attr-3-cuda-{self.short}-pkg.zip"]},
                    "exe": {"sha256": sha[f"MLVApp-playback-attr-3-cuda-{self.short}.exe"]},
                    "dll": {"sha256": sha[f"igpu_recon_cuda-playback-attr-3-cuda-{self.short}.dll"]}}
        path = self.agent / "cache" / f"playback-attr-3-cuda-{self.short}-build.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def emit_and_run(self, manifest_sha: str, name: str, clip_id: str = CLIP_ID) -> tuple[subprocess.CompletedProcess, subprocess.CompletedProcess]:
        out = self.tmp / f"{name}.job.ps1"
        gen = _run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(GENERATOR),
                    "-SourceCommit", self.head, "-BuildManifestSha256", manifest_sha, "-ClipId", clip_id, "-RepoRoot", str(self.repo),
                    "-OutFile", str(out), "-AgentRoot", str(self.agent)])
        self.assertEqual(gen.returncode, 0, gen.stdout + gen.stderr)
        ran = _run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(out)])
        return gen, ran

    def artifacts(self) -> Path:
        found = list((self.agent / "outbox").glob("reference-frame-*.artifacts"))
        self.assertEqual(len(found), 1, found)
        return found[0]

    def bundle_on_disk(self) -> dict[str, str]:
        work = next((self.agent / "work").glob("reference-frame-*")) / "bundle"
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in work.iterdir()}

    def pinned(self) -> dict[str, str]:
        return {Path(rel).name: hashlib.sha256(_git("show", f"{self.head}:{rel}")).hexdigest() for rel in CLOSURE}

    def test_without_a_staged_build_the_job_writes_the_verified_bundle_then_fails_typed_at_the_build_and_never_runs_the_capture(self) -> None:
        _, ran = self.emit_and_run(MANIFEST_SHA, "nobuild")
        self.assertNotEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertIn("RESULT=REFERENCE_FRAME_FAILED STEP=buildVerify", ran.stdout)
        self.assertEqual(self.bundle_on_disk(), self.pinned(), "the bundle on disk is not the pinned bundle")
        result = json.loads((self.artifacts() / "result.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(result["failedStep"], "buildVerify")
        self.assertFalse((self.artifacts() / "capture").exists(), "the capture directory exists: the capture script was launched")

    def test_a_build_json_that_is_not_the_pinned_one_is_refused_before_a_field_of_it_is_read(self) -> None:
        self.stage_build()
        _, ran = self.emit_and_run(MANIFEST_SHA, "wrongmanifest")
        self.assertIn("RESULT=REFERENCE_FRAME_FAILED STEP=buildVerify", ran.stdout)
        self.assertIn("ATTRCUDA_BUILD_MANIFEST_SHA_MISMATCH", ran.stdout)

    def test_a_staged_build_of_another_commit_is_refused(self) -> None:
        sha = self.stage_build(manifest_commit="0" * 40)
        _, ran = self.emit_and_run(sha, "othercommit")
        self.assertIn("ATTRCUDA_BUILD_MANIFEST_COMMIT_MISMATCH", ran.stdout)

    # --- the clip part check, against SYNTHETIC footage (the job's baked parts are swapped for a throwaway file; owner footage is never read) ---
    def run_with_synthetic_part(self, name: str, *, data: bytes | None, declared_length: int, declared_sha: str) -> tuple[subprocess.CompletedProcess, Path]:
        sha = self.stage_build()
        gen_out = self.tmp / f"{name}.job.ps1"
        gen = _run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(GENERATOR),
                    "-SourceCommit", self.head, "-BuildManifestSha256", sha, "-ClipId", CLIP_ID, "-RepoRoot", str(self.repo),
                    "-OutFile", str(gen_out), "-AgentRoot", str(self.agent)])
        self.assertEqual(gen.returncode, 0, gen.stdout + gen.stderr)
        part = self.tmp / f"{name}-synthetic-part.bin"
        if data is not None:
            part.write_bytes(data)
        encoded = base64.b64encode(str(part).replace("\\", "/").encode("utf-8")).decode("ascii")
        parts = json.dumps([{"index": 0, "pathBase64": encoded, "length": declared_length, "sha256": declared_sha}], separators=(",", ":"))
        job = gen_out.read_text(encoding="utf-8")
        swapped, count = re.subn(r"(?m)^\$OwnerPartsJson = '.*'$", lambda _m: f"$OwnerPartsJson = '{parts}'", job)
        self.assertEqual(count, 1)
        gen_out.write_text(swapped, encoding="utf-8")
        return _run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(gen_out)]), part

    def test_a_clip_part_that_is_absent_fails_typed_without_naming_a_path(self) -> None:
        ran, part = self.run_with_synthetic_part("absent", data=None, declared_length=10, declared_sha="00" * 32)
        self.assertNotEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertIn("RESULT=REFERENCE_FRAME_FAILED STEP=footageVerify", ran.stdout)
        self.assertIn("FOOTAGE_PART_NOT_FOUND part=0", ran.stdout)
        self.assertNotIn(part.name, ran.stdout + ran.stderr, "a footage path reached the output")
        self.assertFalse((self.artifacts() / "capture").exists())

    def test_a_clip_part_with_the_wrong_content_is_refused_by_hash_and_by_length(self) -> None:
        data = b"synthetic-footage-0123456789"
        ran, _ = self.run_with_synthetic_part("badsha", data=data, declared_length=len(data), declared_sha="00" * 32)
        self.assertIn("FOOTAGE_PART_SHA256_MISMATCH part=0", ran.stdout)
        ran, _ = self.run_with_synthetic_part("badlen", data=data, declared_length=len(data) + 1, declared_sha=hashlib.sha256(data).hexdigest())
        self.assertIn("FOOTAGE_PART_LENGTH_MISMATCH part=0", ran.stdout)

    def test_a_capture_the_script_refuses_is_a_failed_capture_carrying_the_scripts_own_exit_code(self) -> None:
        # The synthetic part is not footage, so capture-reference-frame.ps1 itself refuses before it would launch anything (its clip-length gate,
        # exit 42 CLIP_LENGTH_UNKNOWN). The job ran the capture script out of the verified bundle, under the gate profile, and never reinterprets
        # its exit code: it is the job's exit code and the result's status is capture-failed, never a candidate.
        data = b"synthetic-footage-0123456789"
        ran, _ = self.run_with_synthetic_part("goodpart", data=data, declared_length=len(data), declared_sha=hashlib.sha256(data).hexdigest())
        captured = self.artifacts() / "capture-stdout.txt"
        self.assertEqual(ran.returncode, 42, ran.stdout + ran.stderr + (captured.read_text(encoding="utf-8-sig") if captured.exists() else "<no capture-stdout.txt>"))
        self.assertIn("RESULT=REFERENCE_FRAME_CAPTURE_FAILED EXIT=42", ran.stdout)
        self.assertNotIn("CAPTURE_OK", ran.stdout)
        result = json.loads((self.artifacts() / "result.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(result["status"], "capture-failed")
        self.assertEqual(result["exitCode"], 42)
        self.assertEqual(result["gateProfile"]["startFrame"], 120)
        self.assertIsNone(result["candidateSha256"])
        self.assertIn("CAPTURE: REFUSED", (self.artifacts() / "capture-stdout.txt").read_text(encoding="utf-8-sig"))
        self.assertFalse((self.artifacts() / "capture" / "reference-frame-candidate.json").exists())


@requires_pwsh
class CaptureScriptGateProfileArgumentTests(oracle_tests._LauncherCase):
    """capture-reference-frame.ps1 against a fake app that records its argv and writes a chosen receipt."""

    def run_capture(self, line: str | None, *extra: str, nonce_mode: str = "echo", exit_code: int = 0) -> tuple[subprocess.CompletedProcess, list[str]]:
        out = self.tmp / "capture-out"
        out.mkdir(exist_ok=True)
        grab = self.tmp / "grab.template"
        grab.write_bytes(b"\0" * 65536)
        args_file = self.tmp / "app-args.json"
        if args_file.exists():
            args_file.unlink()
        fake = oracle_tests._make_fake_app(self.tmp, exit_code=exit_code, nonce_mode=nonce_mode, args_file=args_file,
                                           copies=self.log_template(line) + ((grab, str(out / "reference-frame.png")),))
        proc = _run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(CAPTURE),
                     "-Exe", str(fake), "-Clip", str(self.clip), "-OutDir", str(out), "-Seconds", "25", "-SettleMs", "100",
                     "-Commit", "0123456789abcdef0123456789abcdef01234567", *extra], timeout=240)
        argv = json.loads(args_file.read_text(encoding="utf-8")) if args_file.exists() else []
        return proc, argv

    def test_the_default_argv_is_today_s_argv_byte_for_byte(self) -> None:
        proc, argv = self.run_capture(oracle_tests._good_line())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        shot = str(self.tmp / "capture-out" / "reference-frame.png")
        self.assertEqual(argv, ["--gui-smoke-playback", "--input", str(self.clip), "--scope", "none", "--no-zebras", "--seconds", "25",
                                "--settle-ms", "100", "--start-frame", "0", "--drop-frame-mode", "off",
                                "--presented-frames", "480", "--screenshot-output", shot])

    def test_the_gate_profile_arguments_reach_the_app(self) -> None:
        proc, argv = self.run_capture(oracle_tests._good_line(), "-StartFrame", "120", "-DisableLookAssist", "-PlaybackProcessing", "subset")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(argv[argv.index("--start-frame") + 1], "120")
        self.assertIn("--no-look-assist", argv)
        self.assertIn("--playback-processing=subset", argv)
        # nothing else moved: the same flags in the same order, the screenshot path still last
        self.assertEqual(argv[-2], "--screenshot-output")
        self.assertEqual(argv.count("--start-frame"), 1)

    def test_each_argument_alone_changes_only_its_own_flag(self) -> None:
        _, base = self.run_capture(oracle_tests._good_line())
        for extra, expected in ((("-DisableLookAssist",), ["--no-look-assist"]), (("-PlaybackProcessing", "receipt"), ["--playback-processing=receipt"])):
            with self.subTest(extra=extra):
                proc, argv = self.run_capture(oracle_tests._good_line(), *extra)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual([a for a in argv if a not in base], expected)

    def test_the_manifest_records_the_gate_profile_it_ran_under(self) -> None:
        proc, _ = self.run_capture(oracle_tests._good_line(), "-StartFrame", "120", "-DisableLookAssist", "-PlaybackProcessing", "subset",
                                   "-ProfileId", "wb_locked")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        manifest = json.loads((self.tmp / "capture-out" / "reference-frame-candidate.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(manifest["status"], "candidate-unreviewed")
        self.assertEqual(manifest["profileId"], "wb_locked")
        self.assertEqual(manifest["settings"]["startFrame"], 120)
        self.assertIs(manifest["settings"]["lookAssistDisabled"], True)
        self.assertEqual(manifest["settings"]["playbackProcessing"], "subset")

    def test_an_unknown_processing_mode_is_refused_before_any_launch(self) -> None:
        proc, argv = self.run_capture(oracle_tests._good_line(), "-PlaybackProcessing", "bogus")
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(argv, [], "the app was launched with an unknown processing mode")

    # --- the receipt check is not weakened by the new arguments ----------------------------------------------------------------------------------
    PROFILE = ("-StartFrame", "120", "-DisableLookAssist", "-PlaybackProcessing", "subset")

    def test_the_receipt_check_still_refuses_a_receipt_without_source_advanced_under_the_gate_profile(self) -> None:
        proc, _ = self.run_capture(oracle_tests._pre_enforce_3_line(), *self.PROFILE)
        self.assertEqual(proc.returncode, EXIT_INVALID, proc.stdout + proc.stderr)
        self.assertIn("CAPTURE: INVALID", proc.stdout)
        self.assertIn("INVALID_SOURCE_FRAMES", proc.stdout)
        self.assertFalse((self.tmp / "capture-out" / "reference-frame-candidate.json").exists())

    def test_the_receipt_check_still_refuses_a_receipt_with_another_runs_nonce_under_the_gate_profile(self) -> None:
        proc, _ = self.run_capture(oracle_tests._good_line(), *self.PROFILE, nonce_mode="stale")
        self.assertEqual(proc.returncode, EXIT_INVALID, proc.stdout + proc.stderr)
        self.assertIn("CAPTURE: INVALID", proc.stdout)
        self.assertFalse((self.tmp / "capture-out" / "reference-frame-candidate.json").exists())

    def test_the_receipt_check_still_refuses_a_receipt_with_no_nonce_under_the_gate_profile(self) -> None:
        proc, _ = self.run_capture(oracle_tests._good_line(), *self.PROFILE, nonce_mode="absent")
        self.assertEqual(proc.returncode, EXIT_INVALID, proc.stdout + proc.stderr)

    def test_a_run_with_no_summary_at_all_is_still_invalid_under_the_gate_profile(self) -> None:
        proc, _ = self.run_capture(None, *self.PROFILE)
        self.assertEqual(proc.returncode, EXIT_INVALID, proc.stdout + proc.stderr)

    def test_the_receipt_oracle_lines_of_the_script_are_untouched(self) -> None:
        text = CAPTURE.read_text(encoding="utf-8")
        for needle in ("-ExpectedRunNonce $runNonce", "if ($captureVerdict.invalid) {", "exit $captureVerdict.exitCode",
                       "Get-GuiSmokeEvidencePlayVerdict -Summary $captureSummary", "-KilledByLauncher $captureKilled"):
            self.assertEqual(text.count(needle), 1, needle)


# HUB-VM-NO-LOCAL-PLAYBACK-1: CaptureScriptGateProfileArgumentTests runs the real capture script against a stand-in exe that is never launched.
# The script refuses on the hub VM (tools/profiling/dual-venue/hub-hosts.json) unless the CI-fixture override is set, so set it for this
# module's own subprocesses only, in the test process, exactly as the sibling playback test modules do.
_HUB_FIXTURE_ENV = "MLVAPP_HUB_PLAYBACK_CI_FIXTURE"
_hub_fixture_env_before = None


def setUpModule() -> None:
    global _hub_fixture_env_before
    _hub_fixture_env_before = os.environ.get(_HUB_FIXTURE_ENV)
    os.environ[_HUB_FIXTURE_ENV] = "CI_FIXTURE_ONLY"


def tearDownModule() -> None:
    if _hub_fixture_env_before is None:
        os.environ.pop(_HUB_FIXTURE_ENV, None)
    else:
        os.environ[_HUB_FIXTURE_ENV] = _hub_fixture_env_before


if __name__ == "__main__":
    unittest.main()
