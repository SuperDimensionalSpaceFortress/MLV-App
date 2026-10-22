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
     and its receipt check (source_advanced, run nonce, exit 43) still refuses under them;
  5. r4 (CoS 81b6c553): a gate profile that needs a committed receipt is refused (generator and emitted job), the job id is the effective
     configuration, a failure between the neutral view and the capture still closes the workspace, and exit 225 stamps the candidate invalid.
"""

from __future__ import annotations

import base64
import concurrent.futures
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
from tools.repo_hygiene.synthetic_mlv import MLV_EXTENSION, write_synthetic_mlv
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


_fixture_repo: dict[str, object] = {}


def _shared_fixture_repo(head: str) -> Path:
    """One read-only clone of this repository at HEAD for the whole module: the generator only reads it (the blanket case asserts it stays clean),
    and every case that needs a repository it may change makes a clone of its own. Removed by tearDownModule."""
    if "repo" not in _fixture_repo:
        holder = tempfile.TemporaryDirectory(prefix="refframe-repo-")
        _fixture_repo["holder"] = holder
        repo = Path(os.path.realpath(holder.name)) / "repo"
        _GeneratorCase.clone_fixture_repo(repo, head)
        _fixture_repo["repo"] = repo
    return _fixture_repo["repo"]  # type: ignore[return-value]


class _GeneratorCase(unittest.TestCase):
    """A shared clone of this repository at HEAD (the committed consent, venue table and gate spec) and a way to run the generator on it."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="refframe-gen-")
        cls.tmp = Path(os.path.realpath(cls._tmp.name))
        cls.head = _git("rev-parse", "HEAD").decode().strip()
        cls.repo = _shared_fixture_repo(cls.head)
        cls._default_emission: str | None = None
        cls._emission_by_inputs: dict[tuple[str, str], str] = {}

    @staticmethod
    def clone_fixture_repo(repo: Path, head: str) -> None:
        subprocess.run(["git", "clone", "-q", "--shared", "--no-checkout", str(ROOT), str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "sparse-checkout", "set", "--cone", "tools"], check=True)
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", head], check=True)
        # the clip resolver reads the spec from the full ref refs/remotes/fork/master, never the working tree
        subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/fork/master", head], check=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def generate(self, name: str, *extra: str, clip_id: str = CLIP_ID) -> tuple[subprocess.CompletedProcess, Path]:
        out = self.tmp / f"{name}.job.ps1"
        proc = _run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(GENERATOR),
                     "-SourceCommit", self.head, "-BuildManifestSha256", MANIFEST_SHA, "-ClipId", clip_id,
                     "-RepoRoot", str(self.repo), "-OutFile", str(out), *extra])
        return proc, out

    def emitted(self, name: str = "fix", *, fresh: bool = False) -> str:
        """The job text for the default inputs. The generator is a pure function of its pinned inputs (the identity test asserts a second run
        is byte-identical in id), so the tests that only read the text share one pwsh launch per class; `fresh` forces a launch of its own."""
        if not fresh and self._default_emission is not None:
            return self._default_emission
        proc, out = self.generate(name)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("RESULT=REFERENCE_FRAME_JOB_EMITTED", proc.stdout)
        text = out.read_text(encoding="utf-8")
        if not fresh:
            type(self)._default_emission = text
        return text

    def emitted_file(self, name: str) -> Path:
        """The default emission written to a file of this test's own, for a test that runs or mutates the job."""
        out = self.tmp / f"{name}.job.ps1"
        out.write_text(self.emitted(), encoding="utf-8")
        return out

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
        self.clone_fixture_repo(clone, self.head)
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
        out = self.emitted_file("verify")
        before = set(self.tmp.iterdir())
        ran = _run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(out), "-VerifyOnly"])
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertIn("RESULT=VERIFY_ONLY_OK", ran.stdout)
        self.assertEqual(set(self.tmp.iterdir()), before)

    def test_a_tampered_bundle_is_refused_before_a_byte_is_written(self) -> None:
        out = self.emitted_file("tamper")
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
        job = self.emitted("blanket", fresh=True)
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

    # --- r4 (CoS 81b6c553 P2): a profile the job cannot apply is refused, never recorded and silently dropped ------------------------------------
    def test_a_gate_profile_that_needs_a_committed_receipt_is_refused_before_any_job_is_written(self) -> None:
        # compare-output-budget.ps1 hands a receipt profile spec.receipt.path; this job carries no receipt to the capture (the capture script has
        # no -Receipt), so such a job would record a profile it cannot run under. Refused, typed, until receipt transport exists.
        spec = json.loads(_git("show", f"{self.head}:tools/gates/output-budget.json"))
        receipt_profiles = [p["id"] for p in spec["profiles"] if p.get("playbackProcessing") == "receipt" or "receipt" in p]
        self.assertIn("committed_receipt", receipt_profiles, "the spec no longer names a receipt profile: this assertion is stale")
        for profile_id in receipt_profiles:
            proc, out = self.generate(f"receipt-{profile_id}", "-GateProfile", profile_id)
            self.refused(proc, "REFERENCE_FRAME_RECEIPT_PROFILE_UNSUPPORTED")
            self.assertFalse(out.exists(), f"{profile_id}: a refused receipt profile left a job behind")

    def test_every_step_code_names_one_step_and_the_receipt_refusal_has_its_own(self) -> None:
        job = self.emitted("stepcodes")
        pairs = set(re.findall(r"Complete-Failed (\d+) '(\w+)'", job))
        by_code: dict[int, set[str]] = {}
        by_step: dict[str, set[int]] = {}
        for code, step in pairs:
            by_code.setdefault(int(code), set()).add(step)
            by_step.setdefault(step, set()).add(int(code))
        self.assertEqual({c: s for c, s in by_code.items() if len(s) > 1}, {}, "two steps share an exit code")
        self.assertEqual({s: c for s, c in by_step.items() if len(c) > 1}, {}, "one step exits with two codes")
        self.assertEqual(by_step.get("receiptProfile"), {226}, sorted(pairs))
        self.assertNotIn(226, re.findall(r"\bexit (\d+)", _git("show", f"{self.head}:{CLOSURE[0]}").decode("utf-8")))

    # --- r4 i1: the job's identity is its effective configuration -----------------------------------------------------------------------------------
    @staticmethod
    def job_id(job: str) -> str:
        found = re.findall(r"(?m)^\$JobId = '([^']+)'$", job)
        assert len(found) == 1, found
        return found[0]

    def test_two_jobs_that_differ_only_in_gate_profile_spec_or_a_capture_parameter_get_distinct_identities(self) -> None:
        base = self.emitted("ident-base")
        self.assertIn('$Work = Join-Path $AgentRoot "work\\$JobId"', base)
        self.assertIn('$Pub = Join-Path $AgentRoot "outbox\\$JobId.artifacts"', base)
        # Six independent generator launches (each a pwsh start plus git reads). They share no output file, so they run concurrently: the wall
        # time of this case is the slowest launch, not their sum. Every launch is still asserted below, on its own result.
        def spec_clone_launch(label: str, profile: str) -> tuple[subprocess.CompletedProcess, Path]:
            clone = self.tmp / "ident-repo"   # a tooling commit whose spec differs (same closure bytes, so the bundle digest is unchanged)
            out = self.tmp / f"ident-{label}.job.ps1"
            proc = _run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(GENERATOR),
                         "-SourceCommit", self.head, "-BuildManifestSha256", MANIFEST_SHA, "-ClipId", CLIP_ID, "-GateProfile", profile,
                         "-RepoRoot", str(clone), "-OutFile", str(out)])
            return proc, out

        def make_spec_clone() -> None:
            clone = self.tmp / "ident-repo"
            self.clone_fixture_repo(clone, self.head)
            spec_path = clone / "tools" / "gates" / "output-budget.json"
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            alt = dict(next(p for p in spec["profiles"] if p["id"] == "wb_locked"), id="wb_locked_alt")   # a second profile id identical in content
            spec["profiles"].append(alt)
            spec_path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(clone), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-a", "-m", "spec"], check=True)

        def clone_then_launch(label: str, profile: str, ready: concurrent.futures.Future) -> tuple[subprocess.CompletedProcess, Path]:
            ready.result()
            return spec_clone_launch(label, profile)

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            clone_ready = pool.submit(make_spec_clone)
            again = pool.submit(self.emitted, "ident-again", fresh=True)
            plain = {label: pool.submit(self.generate, f"ident-{label}", *extra)
                     for label, extra in (("seconds", ("-Seconds", "31")), ("frames", ("-PresentedFrames", "10")))}
            cloned = {label: pool.submit(clone_then_launch, label, profile, clone_ready)
                      for label, profile in (("spec", "wb_locked"), ("profile", "wb_locked_alt"))}
            clone_ready.result()
            # the same configuration is the same job (a resubmission retries the same work and outbox names)
            self.assertEqual(self.job_id(again.result()), self.job_id(base))
            ids = {"base": self.job_id(base)}
            for label, future in plain.items():
                proc, out = future.result()
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                ids[label] = self.job_id(out.read_text(encoding="utf-8"))
            for label, future in cloned.items():
                proc, out = future.result()
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                job = out.read_text(encoding="utf-8")
                self.assertEqual(self.bundle(job), self.bundle(base), "the closure changed: this case no longer isolates the spec")
                ids[label] = self.job_id(job)
        self.assertEqual(len(set(ids.values())), len(ids), ids)

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
        invocation = next(line.strip() for line in job.splitlines() if line.strip().startswith("$captureCommand =") and "-Exe" in line)
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

    def test_a_failed_capture_exits_outside_every_step_code(self) -> None:
        # REFERENCE-FRAME-JOB-EXIT-CODE-SPACE-1: the capture's code stays the job's exit code (test_playback_receipt_run_binding.py pins that),
        # so the job's OWN step codes moved to 2xx: the capture script's every exit is below 100 (70 from the command wrapper included), and a
        # code outside 1-199 is folded to 199 -- no capture code can read as a step code.
        job = self.emitted()
        steps = {int(code) for code in re.findall(r"Complete-Failed (\d+) '", job)}
        self.assertTrue(steps, "no step codes found: this assertion is stale")
        self.assertTrue(all(200 <= code <= 255 for code in steps), sorted(steps))
        self.assertIn("if ($captureRc -ne 0 -and ($captureRc -lt 1 -or $captureRc -gt 199)) { $captureRc = 199 }", job)
        self.assertEqual(sorted(int(code) for code in re.findall(r"(?m)^\s*exit (\d+)\s*$", job)), [0, 0, 225])
        capture_codes = {int(code) for code in re.findall(r"\bexit (\d+)", _git("show", f"{self.head}:{CLOSURE[0]}").decode("utf-8"))}
        self.assertTrue(capture_codes and max(capture_codes) < 100, sorted(capture_codes))

    def test_the_capture_is_handed_only_the_neutral_view_and_the_originals_are_pinned_first(self) -> None:
        # F-01, statically (the fixture run above proves it in behaviour): the pin precedes the view, the view precedes the one content read,
        # and the -Clip argument is the view of part 0 -- never a decoded part path.
        job = self.emitted()
        order = [job.index(needle) for needle in (
            "Open-AttrCudaReadOnlyHandle -Path $part.path", "New-AttrCudaOwnerFootageView -Directory $OwnerClipDir",
            "Test-AttrCudaFootagePart -Path $view.Path", "$captureCommand = ")]
        self.assertEqual(order, sorted(order))
        self.assertIn("if ($part.index -eq 0) { $clipPath = $view.Path }", job)
        self.assertIn("-Clip '$clipPath'", job)
        self.assertNotIn("$clipPath = @($verifiedParts", job, "the capture is handed a decoded part path again")
        self.assertIn("$OwnerClipDir = New-AttrCudaDirectory -Path (Join-Path $Work 'owner-clip')", job)

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
            # a fixed timestamp, so the same build stages to the same sha256 in every test (the emitted job pins it)
            archive.writestr(zipfile.ZipInfo("MLVApp-release/MLVApp.exe", date_time=(2026, 10, 20, 0, 0, 0)), b"stand-in")
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

    def job_for_this_agent(self, manifest_sha: str, name: str, clip_id: str = CLIP_ID) -> Path:
        """The emitted job for this test's own agent root, written to `<name>.job.ps1`.

        The generator is a pure function of (commit, manifest sha, clip) plus the agent root it bakes into ONE line, `$AgentRoot = '...'`. Every
        test here wants the same job on a different throwaway root, so the generator is launched once per distinct (manifest sha, clip) per class
        -- with an -AgentRoot of its own, so its substitution and allowlist are exercised for real -- and each test re-points that single line at
        its own root. The swap asserts the anchor is unique, so a generator that moved or doubled the line fails loudly here."""
        cache = type(self)._emission_by_inputs
        key = (manifest_sha, clip_id)
        if key not in cache:
            template_root = self.tmp / f"agent-template-{len(cache)}"
            template_root.mkdir()
            out = self.tmp / f"template-{len(cache)}.job.ps1"
            gen = _run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(GENERATOR),
                        "-SourceCommit", self.head, "-BuildManifestSha256", manifest_sha, "-ClipId", clip_id, "-RepoRoot", str(self.repo),
                        "-OutFile", str(out), "-AgentRoot", str(template_root)])
            self.assertEqual(gen.returncode, 0, gen.stdout + gen.stderr)
            cache[key] = out.read_text(encoding="utf-8")
        job, count = re.subn(r"(?m)^\$AgentRoot = '[^']*'$", lambda _m: f"$AgentRoot = '{self.agent}'", cache[key])
        self.assertEqual(count, 1, "the emitted job's $AgentRoot line moved or doubled: this fixture is stale")
        path = self.tmp / f"{name}.job.ps1"
        path.write_text(job, encoding="utf-8")
        return path

    def emit_and_run(self, manifest_sha: str, name: str, clip_id: str = CLIP_ID) -> subprocess.CompletedProcess:
        out = self.job_for_this_agent(manifest_sha, name, clip_id)
        return _run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(out)])

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
        ran = self.emit_and_run(MANIFEST_SHA, "nobuild")
        self.assertNotEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertIn("RESULT=REFERENCE_FRAME_FAILED STEP=buildVerify", ran.stdout)
        self.assertEqual(self.bundle_on_disk(), self.pinned(), "the bundle on disk is not the pinned bundle")
        result = json.loads((self.artifacts() / "result.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(result["failedStep"], "buildVerify")
        self.assertFalse((self.artifacts() / "capture").exists(), "the capture directory exists: the capture script was launched")

    def test_a_build_json_that_is_not_the_pinned_one_is_refused_before_a_field_of_it_is_read(self) -> None:
        self.stage_build()
        ran = self.emit_and_run(MANIFEST_SHA, "wrongmanifest")
        self.assertIn("RESULT=REFERENCE_FRAME_FAILED STEP=buildVerify", ran.stdout)
        self.assertIn("ATTRCUDA_BUILD_MANIFEST_SHA_MISMATCH", ran.stdout)

    def test_a_staged_build_of_another_commit_is_refused(self) -> None:
        sha = self.stage_build(manifest_commit="0" * 40)
        ran = self.emit_and_run(sha, "othercommit")
        self.assertIn("ATTRCUDA_BUILD_MANIFEST_COMMIT_MISMATCH", ran.stdout)

    # --- the clip part check, against SYNTHETIC footage (the job's baked parts are swapped for a throwaway file; owner footage is never read) ---
    def run_with_synthetic_part(self, name: str, *, data: bytes | None, declared_length: int, declared_sha: str,
                                edit: tuple[str, str] | None = None) -> tuple[subprocess.CompletedProcess, Path]:
        sha = self.stage_build()
        gen_out = self.job_for_this_agent(sha, name)
        # the base extension a real part 0 carries (the job views part i under the neutral name for index i, which checks it)
        part = self.tmp / (f"{name}-synthetic-part" + MLV_EXTENSION)
        if data is not None:
            part.write_bytes(data)
        encoded = base64.b64encode(str(part).replace("\\", "/").encode("utf-8")).decode("ascii")
        parts = json.dumps([{"index": 0, "pathBase64": encoded, "length": declared_length, "sha256": declared_sha}], separators=(",", ":"))
        job = gen_out.read_text(encoding="utf-8")
        swapped, count = re.subn(r"(?m)^\$OwnerPartsJson = '.*'$", lambda _m: f"$OwnerPartsJson = '{parts}'", job)
        self.assertEqual(count, 1)
        if edit is not None:   # a fault injected into the job's own text, at an anchor that must stay unique
            self.assertEqual(swapped.count(edit[0]), 1, f"injection anchor {edit[0]!r} is not unique: this fixture is stale")
            swapped = swapped.replace(edit[0], edit[1])
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
        # its exit code: it is the job's exit code (and captureExitCode, CAPTURE_EXIT=), no step code shares it (the job's own are 2xx,
        # REFERENCE-FRAME-JOB-EXIT-CODE-SPACE-1), and the result's status is capture-failed, never a candidate.
        data = b"synthetic-footage-0123456789"
        ran, _ = self.run_with_synthetic_part("goodpart", data=data, declared_length=len(data), declared_sha=hashlib.sha256(data).hexdigest())
        captured = self.artifacts() / "capture-stdout.txt"
        self.assertEqual(ran.returncode, 42, ran.stdout + ran.stderr + (captured.read_text(encoding="utf-8-sig") if captured.exists() else "<no capture-stdout.txt>"))
        self.assertIn("RESULT=REFERENCE_FRAME_CAPTURE_FAILED EXIT=42 CAPTURE_EXIT=42", ran.stdout)
        self.assertNotIn("CAPTURE_OK", ran.stdout)
        result = json.loads((self.artifacts() / "result.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(result["status"], "capture-failed")
        self.assertEqual(result["exitCode"], 42)
        self.assertEqual(result["captureExitCode"], 42)
        self.assertEqual(result["gateProfile"]["startFrame"], 120)
        self.assertIsNone(result["candidateSha256"])
        self.assertIn("CAPTURE: REFUSED", (self.artifacts() / "capture-stdout.txt").read_text(encoding="utf-8-sig"))
        self.assertFalse((self.artifacts() / "capture" / "reference-frame-candidate.json").exists())

    # --- F-01 footage-path-disclosure: a SUCCESSFUL capture publishes nothing that names the owner's part path ---------------------------------
    def run_stand_in_capture(self, name: str, edit: tuple[str, str] | None = None,
                             more_edits: tuple[tuple[str, str], ...] = ()) -> tuple[subprocess.CompletedProcess, Path, Path, str, Path, str]:
        """The emitted job run end to end against a stand-in app; returns (ran, outbox, part, part sha256, the app's argv file, the path marker)."""
        marker = "OWNERREALPATHMARKERq7z3"
        owner_dir = self.tmp / f"{marker}-dir"
        owner_dir.mkdir(exist_ok=True)
        part = write_synthetic_mlv(owner_dir / (f"{marker}-part" + MLV_EXTENSION), oracle_tests.FORTY_SECONDS_AT_23976)
        part_bytes = part.read_bytes()
        part_sha = hashlib.sha256(part_bytes).hexdigest()
        sha = self.stage_build()
        gen_out = self.job_for_this_agent(sha, name)
        app_dir = self.tmp / "stand-in-app"
        app_dir.mkdir(exist_ok=True)
        log_template = app_dir / "app.log.template"
        log_template.write_text("t0 playback_smoke.start session=1\nt1 " + oracle_tests._good_line() + "\n", encoding="utf-8")
        grab = app_dir / "grab.template"
        grab.write_bytes(b"\0" * 65536)
        args_file = app_dir / "app-args.json"
        fake = oracle_tests._make_fake_app(app_dir, exit_code=0, args_file=args_file, copies=(
            (log_template, oracle_tests.LOG_DESTINATION), (grab, "%MLVAPP_CRASH_FORENSICS_LOG_DIR%\\reference-frame.png")))
        encoded = base64.b64encode(str(part).replace("\\", "/").encode("utf-8")).decode("ascii")
        parts = json.dumps([{"index": 0, "pathBase64": encoded, "length": len(part_bytes), "sha256": part_sha}], separators=(",", ":"))
        job = gen_out.read_text(encoding="utf-8")
        job, count = re.subn(r"(?m)^\$OwnerPartsJson = '.*'$", lambda _m: f"$OwnerPartsJson = '{parts}'", job)
        self.assertEqual(count, 1)
        self.assertEqual(job.count("-Exe '$exePath'"), 1, "the capture's -Exe anchor moved: this fixture is stale")
        job = job.replace("-Exe '$exePath'", f"-Exe '{fake}'")
        for anchor, injected in (() if edit is None else (edit,)) + tuple(more_edits):
            self.assertEqual(job.count(anchor), 1, f"injection anchor {anchor!r} is not unique: this fixture is stale")
            job = job.replace(anchor, injected)
        gen_out.write_text(job, encoding="utf-8")
        ran = _run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(gen_out)])
        return ran, self.artifacts(), part, part_sha, args_file, marker

    def test_a_successful_capture_publishes_no_artifact_naming_the_part_path_and_the_capture_sees_only_the_neutral_view(self) -> None:
        # The real capture script runs out of the verified bundle against a stand-in app (oracle_tests' fake: records its argv, writes a
        # receipt carrying this run's nonce and a stand-in grab). The only edit to the emitted job is the two things a fixture cannot provide:
        # the part (a synthetic 41 s clip in a directory, and under a name, that carry a distinctive marker) and the -Exe the capture launches
        # (the stand-in, since the staged exe in a fixture cache is not a program). Everything between -- the part check, the view, the pin,
        # the capture command, the publish -- is the job's own code.
        ran, pub, part, part_sha, args_file, marker = self.run_stand_in_capture("success")
        captured = pub / "capture-stdout.txt"
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr + (captured.read_text(encoding="utf-8-sig") if captured.exists() else "<no capture-stdout.txt>"))
        self.assertIn("RESULT=REFERENCE_FRAME_CAPTURE_OK", ran.stdout)
        self.assertTrue((pub / "capture" / "reference-frame-candidate.json").is_file())

        # 1. no published file -- the capture's stdout, result.json, the candidate manifest, the app's log, anything -- names the part
        needles = {marker, part.name, str(part), str(part).replace("\\", "/"), str(part).replace("\\", "\\\\")}
        leaks = []
        for published in sorted(p for p in pub.rglob("*") if p.is_file()):
            data = published.read_bytes()
            for needle in needles:
                if needle.encode("utf-8") in data or needle.encode("utf-16-le") in data:
                    leaks.append(f"{published.relative_to(pub)} names {needle!r}")
        self.assertEqual(leaks, [], "a published artifact names the owner's footage path (F-01)")
        for stream in (ran.stdout, ran.stderr):
            self.assertNotIn(marker, stream, "the job's own output names the owner's footage path")

        # 2. the capture -- and so the app -- received only the neutral view under the job's work directory
        argv = json.loads(args_file.read_text(encoding="utf-8"))
        given = Path(argv[argv.index("--input") + 1])
        work = next((self.agent / "work").glob("reference-frame-*"))
        self.assertEqual(given.name.lower(), ("owner-clip" + MLV_EXTENSION).lower(), argv)
        self.assertEqual(given.parent, work / "owner-clip", argv)
        self.assertNotIn(marker, json.dumps(argv))
        manifest = json.loads((pub / "capture" / "reference-frame-candidate.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(manifest["clip"]["sha256"].lower(), part_sha, "the view the capture hashed is not the verified part")
        result = json.loads((pub / "result.json").read_text(encoding="utf-8-sig"))
        self.assertEqual((result["status"], result["exitCode"], result["captureExitCode"]), ("candidate-unreviewed", 0, 0))
        self.assertIn(result["footageView"]["mode"], ("symlink", "copy"))
        self.assertIs(result["footageView"]["intactAfterCapture"], True)

        # 3. the original is untouched, and the job removed its view entry when it closed the workspace
        self.assertEqual(hashlib.sha256(part.read_bytes()).hexdigest(), part_sha)
        self.assertEqual([p.name for p in (work / "owner-clip").iterdir()], [], "a view entry outlived the job")

    # --- r4 (CoS 81b6c553 P2): the emitted job itself refuses a receipt profile, before anything is written or launched ---------------------------
    def test_an_emitted_job_carrying_a_receipt_profile_refuses_before_anything_is_written_or_launched(self) -> None:
        out = self.job_for_this_agent(self.stage_build(), "receiptjob")
        job, count = re.subn(r"(?m)^\$PlaybackProcessing = '[a-z]*'$", "$PlaybackProcessing = 'receipt'", out.read_text(encoding="utf-8"))
        self.assertEqual(count, 1)
        out.write_text(job, encoding="utf-8")
        for args in ((), ("-VerifyOnly",)):
            ran = _run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(out), *args])
            self.assertEqual(ran.returncode, 226, ran.stdout + ran.stderr)
            self.assertIn("RESULT=REFERENCE_FRAME_FAILED STEP=receiptProfile EXIT=226", ran.stdout)
            self.assertIn("RECEIPT_PROFILE_UNSUPPORTED", ran.stdout)
            self.assertNotIn("step capture start", ran.stdout)
            self.assertFalse((self.agent / "work").exists(), "the job created its work tree before refusing")
            self.assertFalse((self.agent / "outbox").exists(), "the job created its outbox before refusing")

    # --- r4 i2: a failure between the neutral view and the capture still closes the owner-footage workspace -----------------------------------------
    def assert_window_failure_closed_the_workspace(self, ran: subprocess.CompletedProcess, part: Path, data: bytes, step: str, code: int) -> None:
        self.assertEqual(ran.returncode, code, ran.stdout + ran.stderr)
        self.assertIn(f"RESULT=REFERENCE_FRAME_FAILED STEP={step} EXIT={code}", ran.stdout)
        self.assertNotIn(part.name, ran.stdout + ran.stderr, "a footage path reached the output")
        work = next((self.agent / "work").glob("reference-frame-*"))
        self.assertEqual([p.name for p in (work / "owner-clip").iterdir()], [], "a neutral view outlived a failure in the cleanup window")
        self.assertEqual(part.read_bytes(), data, "the original part changed")
        self.assertFalse((self.artifacts() / "capture-stdout.txt").exists(), "the capture was launched")
        result = json.loads((self.artifacts() / "result.json").read_text(encoding="utf-8-sig"))
        self.assertEqual((result["failedStep"], result["exitCode"]), (step, code))

    def test_the_workspace_is_closed_after_the_view_is_built_on_a_bundle_hash_read_failure(self) -> None:
        data = b"synthetic-footage-0123456789"
        ran, part = self.run_with_synthetic_part("winhash", data=data, declared_length=len(data), declared_sha=hashlib.sha256(data).hexdigest(),
                                                 edit=("Get-ShaLower (Join-Path $BundleDir $entry.name)", "Get-ShaLower (Join-Path $BundleDir 'injected-absent-bundle-file')"))
        self.assert_window_failure_closed_the_workspace(ran, part, data, "bundleReverify", 219)

    def test_the_workspace_is_closed_after_the_view_is_built_on_a_capture_dir_create_failure(self) -> None:
        data = b"synthetic-footage-0123456789"
        ran, part = self.run_with_synthetic_part("windir", data=data, declared_length=len(data), declared_sha=hashlib.sha256(data).hexdigest(),
                                                 edit=("[void](New-AttrCudaDirectory -Path $captureOut)", "throw 'INJECTED_CAPTURE_DIR_FAILURE'"))
        self.assert_window_failure_closed_the_workspace(ran, part, data, "captureSetup", 227)

    # --- r4 i3: exit 225 invalidates the candidate manifest as well as result.json -----------------------------------------------------------------
    def test_a_view_that_changed_during_the_capture_marks_the_candidate_manifest_invalid(self) -> None:
        ran, pub, _, _, _, _ = self.run_stand_in_capture("viewchanged", edit=(
            "Assert-AttrCudaOwnerFootageViewsIntact -Views @($ownerViews); $viewsIntactAfterCapture = $true",
            "throw 'INJECTED_VIEW_CHANGED'; $viewsIntactAfterCapture = $true"))
        self.assertEqual(ran.returncode, 225, ran.stdout + ran.stderr)
        self.assertIn("RESULT=REFERENCE_FRAME_FAILED STEP=footageViewIntact EXIT=225", ran.stdout)
        result = json.loads((pub / "result.json").read_text(encoding="utf-8-sig"))
        self.assertEqual((result["status"], result["exitCode"], result["captureExitCode"]), ("capture-failed", 225, 0))
        self.assertIsNone(result["candidateSha256"])
        candidate = json.loads((pub / "capture" / "reference-frame-candidate.json").read_text(encoding="utf-8-sig"))
        self.assertNotEqual(candidate["status"], "candidate-unreviewed", "the candidate still describes itself as a candidate")
        self.assertEqual(candidate["status"], "invalid")
        self.assertIs(candidate["invalid"], True)
        self.assertIn("OWNER_FOOTAGE_VIEW_CHANGED_DURING_CAPTURE", candidate["invalid_reasons"])
        self.assertIs(result["candidateInvalidated"], True)
        self.assertIs(result["candidateInvalidMarker"], True, "the marker is written whenever a manifest is invalidated, not only when the stamp fails")

    # --- VENUE-REFERENCE-FRAME-CANDIDATE-INVALID-MARKER-1: the in-place stamp is best effort, so a durable marker backs it -------------------------
    VIEW_CHANGED = ("Assert-AttrCudaOwnerFootageViewsIntact -Views @($ownerViews); $viewsIntactAfterCapture = $true",
                    "throw 'INJECTED_VIEW_CHANGED'; $viewsIntactAfterCapture = $true")
    # (a) the manifest on disk does not parse when the job comes to stamp it
    UNPARSEABLE_MANIFEST = ("$candidatePath = Join-Path $captureOut 'reference-frame-candidate.json'",
                            "$candidatePath = Join-Path $captureOut 'reference-frame-candidate.json'; "
                            "[IO.File]::WriteAllText($candidatePath, '{\"status\": \"candidate-unreviewed\", \"truncated')")
    # (b) the manifest parses, but publishing the stamped copy fails
    STAMP_PUBLISH_FAILS = ("[void](Publish-AttrCudaText -Path $candidatePath -Value ($candidateDoc | ConvertTo-Json -Depth 64))",
                           "throw 'INJECTED_CANDIDATE_PUBLISH_FAILURE'")
    # (c) the marker cannot be written either
    MARKER_PUBLISH_FAILS = ("[void](Publish-AttrCudaText -Path $candidateMarkerPath ",
                            "throw 'INJECTED_MARKER_PUBLISH_FAILURE'; [void](Publish-AttrCudaText -Path $candidateMarkerPath ")

    @staticmethod
    def _frame_provenance():
        import importlib.util
        spec = importlib.util.spec_from_file_location("frame_provenance_under_test", ROOT / "tools" / "profiling" / "frame_provenance.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def assert_failed_stamp_left_a_marker_and_no_candidate(self, ran: subprocess.CompletedProcess, pub: Path) -> dict:
        self.assertEqual(ran.returncode, 225, ran.stdout + ran.stderr)
        self.assertIn("RESULT=REFERENCE_FRAME_FAILED STEP=footageViewIntact EXIT=225", ran.stdout)
        result = json.loads((pub / "result.json").read_text(encoding="utf-8-sig"))
        self.assertEqual((result["status"], result["exitCode"], result["captureExitCode"]), ("capture-failed", 225, 0))
        self.assertIsNone(result["candidateSha256"])
        self.assertIs(result["candidateInvalidated"], False, "the stamp did not land: result.json must not say it did")
        marker = pub / "capture" / "reference-frame-candidate.invalid"
        self.assertTrue(marker.is_file(), "no durable invalid marker: the manifest still reads as a candidate")
        document = json.loads(marker.read_text(encoding="utf-8-sig"))
        self.assertIs(document["invalid"], True)
        self.assertIn("OWNER_FOOTAGE_VIEW_CHANGED_DURING_CAPTURE", document["invalid_reasons"])
        self.assertIs(result["candidateInvalidMarker"], True)
        # the reader in tree honours it: marker present = not a candidate, whatever the manifest says
        self.assertEqual(self._frame_provenance().index_capture_records(str(pub)), {}, "frame_provenance still indexes the capture as a candidate")
        return result

    def test_a_manifest_that_does_not_parse_leaves_a_marker_even_so(self) -> None:
        ran, pub, _, _, _, _ = self.run_stand_in_capture("unparseable", edit=self.VIEW_CHANGED, more_edits=(self.UNPARSEABLE_MANIFEST,))
        self.assertIn("CANDIDATE_NOT_INVALIDATED", ran.stdout)
        self.assert_failed_stamp_left_a_marker_and_no_candidate(ran, pub)
        with self.assertRaises(ValueError):
            json.loads((pub / "capture" / "reference-frame-candidate.json").read_text(encoding="utf-8-sig"))

    def test_a_stamp_that_cannot_be_published_leaves_a_marker_anyway(self) -> None:
        ran, pub, _, _, _, _ = self.run_stand_in_capture("stampfails", edit=self.VIEW_CHANGED, more_edits=(self.STAMP_PUBLISH_FAILS,))
        self.assertIn("CANDIDATE_NOT_INVALIDATED", ran.stdout)
        self.assert_failed_stamp_left_a_marker_and_no_candidate(ran, pub)
        # the manifest kept its old, unstamped text: this is exactly the case the marker exists for
        manifest = json.loads((pub / "capture" / "reference-frame-candidate.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(manifest["status"], "candidate-unreviewed")

    def test_when_neither_the_stamp_nor_the_marker_can_be_written_the_manifest_is_moved_out_of_the_candidate_name(self) -> None:
        ran, pub, _, _, _, _ = self.run_stand_in_capture("nomarker", edit=self.VIEW_CHANGED,
                                                         more_edits=(self.STAMP_PUBLISH_FAILS, self.MARKER_PUBLISH_FAILS))
        self.assertEqual(ran.returncode, 225, ran.stdout + ran.stderr)
        self.assertIn("CANDIDATE_MARKER_NOT_WRITTEN", ran.stdout)
        result = json.loads((pub / "result.json").read_text(encoding="utf-8-sig"))
        self.assertEqual((result["status"], result["exitCode"], result["captureExitCode"]), ("capture-failed", 225, 0))
        self.assertIsNone(result["candidateSha256"])
        self.assertEqual((result["candidateInvalidated"], result["candidateInvalidMarker"], result["candidateQuarantined"]), (False, False, True))
        self.assertFalse((pub / "capture" / "reference-frame-candidate.json").exists(), "the manifest still carries the candidate name")
        self.assertFalse((pub / "capture" / "reference-frame-candidate.invalid").exists())
        self.assertEqual(self._frame_provenance().index_capture_records(str(pub)), {})

    def test_a_marker_that_cannot_be_written_but_a_stamp_that_lands_is_still_not_a_candidate_to_the_reader(self) -> None:
        """REFERENCE-FRAME-CONSUMER-HONORS-INVALID-STAMP-1: marker publish fails, stamp succeeds -> no marker and no quarantine (the job only does
        either when the stamp failed too), so the stamp alone must keep frame_provenance from indexing the voided capture."""
        ran, pub, _, _, _, _ = self.run_stand_in_capture("markerfails", edit=self.VIEW_CHANGED, more_edits=(self.MARKER_PUBLISH_FAILS,))
        self.assertEqual(ran.returncode, 225, ran.stdout + ran.stderr)
        self.assertIn("CANDIDATE_MARKER_NOT_WRITTEN", ran.stdout)
        result = json.loads((pub / "result.json").read_text(encoding="utf-8-sig"))
        self.assertEqual((result["candidateInvalidated"], result["candidateInvalidMarker"], result["candidateQuarantined"]), (True, False, None))
        self.assertFalse((pub / "capture" / "reference-frame-candidate.invalid").exists())
        manifest = json.loads((pub / "capture" / "reference-frame-candidate.json").read_text(encoding="utf-8-sig"))
        self.assertEqual((manifest["status"], manifest["invalid"]), ("invalid", True))
        # No `skipped` argument first, so a reader that predates the stamp check fails here on the assertion, not on a TypeError for the new parameter.
        self.assertEqual(self._frame_provenance().index_capture_records(str(pub)), {}, "frame_provenance still indexes the stamped-invalid capture")
        skipped: list[dict] = []
        self.assertEqual(self._frame_provenance().index_capture_records(str(pub), skipped), {}, "frame_provenance still indexes the stamped-invalid capture")
        self.assertEqual([s["reason"] for s in skipped], ["manifest-stamped-invalid"])

    def test_a_capture_that_passed_writes_no_invalid_marker_and_reports_none(self) -> None:
        ran, pub, _, _, _, _ = self.run_stand_in_capture("nomarkerok")
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertFalse((pub / "capture" / "reference-frame-candidate.invalid").exists())
        result = json.loads((pub / "result.json").read_text(encoding="utf-8-sig"))
        self.assertEqual((result["candidateInvalidated"], result["candidateInvalidMarker"], result["candidateQuarantined"]), (None, None, None))
        self.assertEqual(len(self._frame_provenance().index_capture_records(str(pub))), 1)

    def test_the_comment_states_the_marker_rule_and_no_longer_claims_the_manifest_alone_decides(self) -> None:
        text = GENERATOR.read_text(encoding="utf-8")
        self.assertNotIn("so the manifest alone never\n# reads as a candidate", text)
        self.assertIn("reference-frame-candidate.invalid", text)
        self.assertIn("marker present = not a candidate", text)


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


M16_1243_FRAMES = 612          # 25.526 s at 23.976 fps; from -StartFrame 120 the footage left is 492 frames = 20.521 s
M16_1243_START_FRAME = "120"


@requires_pwsh
class CaptureScriptPinnedWindowTests(oracle_tests._LauncherCase):
    """REFERENCE-FRAME-PINNED-WINDOW-GATE-1: a pinned-frame capture needs the PINNED frame, not real-time pace.

    OUTPUT-BUDGET-KNOWN-GOOD-PIN-2 r1 could not capture the known-good frame of M16-1243 (wb_locked, startFrame 120, 20.521 s of footage left):
    `-Seconds 30` returned 41 CLIP_TOO_SHORT although the app only ever plays min(--seconds, N / fps) = 20.02 s of it (smokePlayRequestSeconds,
    platform/qt/PlaybackFrameRange.h), and `-Seconds 20` came back as exit 4 with the app's exit 11 reading as an untyped `exited 11 (NONE)`.
    A fake app records its argv and writes a chosen receipt; no venue, no footage, no app launch."""

    def setUp(self) -> None:
        super().setUp()
        self.clip = write_synthetic_mlv(self.tmp / ("m16" + MLV_EXTENSION), M16_1243_FRAMES)

    def run_capture(self, line: str | None, *extra: str, exit_code: int = 0, stderr_text: str = "") -> tuple[subprocess.CompletedProcess, list[str]]:
        out = self.tmp / "capture-out"
        out.mkdir(exist_ok=True)
        grab = self.tmp / "grab.template"
        grab.write_bytes(b"\0" * 65536)
        args_file = self.tmp / "app-args.json"
        if args_file.exists():
            args_file.unlink()
        fake = oracle_tests._make_fake_app(self.tmp, exit_code=exit_code, args_file=args_file, stderr_text=stderr_text,
                                           copies=self.log_template(line) + ((grab, str(out / "reference-frame.png")),))
        proc = _run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(CAPTURE),
                     "-Exe", str(fake), "-Clip", str(self.clip), "-OutDir", str(out), "-SettleMs", "100", "-StartFrame", M16_1243_START_FRAME,
                     "-Commit", "0123456789abcdef0123456789abcdef01234567", *extra], timeout=240)
        argv = json.loads(args_file.read_text(encoding="utf-8")) if args_file.exists() else []
        return proc, argv

    @staticmethod
    def pinned_line(**overrides: object) -> str:
        return oracle_tests._good_line(**{"total_frames": M16_1243_FRAMES, "clip_seconds": 25.526, "presented_frames": 480, **overrides})

    def test_a_timeout_longer_than_the_footage_left_does_not_refuse_a_pinned_frame_capture(self) -> None:
        """Failure shape 1: -Seconds 30 is a TIMEOUT for a pinned run, so it may exceed the 20.521 s left after startFrame 120."""
        proc, argv = self.run_capture(self.pinned_line(), "-Seconds", "30")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("CLIP_TOO_SHORT", proc.stdout)
        self.assertEqual(argv[argv.index("--seconds") + 1], "30")
        self.assertEqual(argv[argv.index("--presented-frames") + 1], "480")
        self.assertEqual(argv[argv.index("--start-frame") + 1], M16_1243_START_FRAME)

    def test_the_timeout_equal_to_the_footage_left_still_passes(self) -> None:
        proc, _ = self.run_capture(self.pinned_line(), "-Seconds", "20")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_a_time_based_capture_still_refuses_a_window_longer_than_the_footage_left(self) -> None:
        """-PresentedFrames 0 plays for -Seconds of wall clock, so there the window IS -Seconds and 41 stays."""
        proc, argv = self.run_capture(self.pinned_line(), "-Seconds", "30", "-PresentedFrames", "0")
        self.assertEqual(proc.returncode, 41, proc.stdout + proc.stderr)
        self.assertIn("CLIP_TOO_SHORT", proc.stdout)
        self.assertEqual(argv, [], "the app was launched past a refused window")

    def test_a_pinned_frame_target_that_outlasts_the_footage_left_is_still_refused(self) -> None:
        """The window is min(-Seconds, N / fps): N = 600 frames is 25.02 s, more than the 20.521 s left, whatever the timeout."""
        proc, argv = self.run_capture(self.pinned_line(), "-Seconds", "30", "-PresentedFrames", "600")
        self.assertEqual(proc.returncode, 41, proc.stdout + proc.stderr)
        self.assertIn("CLIP_TOO_SHORT", proc.stdout)
        self.assertEqual(argv, [])

    def test_a_timeout_under_the_floor_is_still_refused_whatever_the_target(self) -> None:
        proc, argv = self.run_capture(self.pinned_line(), "-Seconds", "10")
        self.assertEqual(proc.returncode, 41, proc.stdout + proc.stderr)
        self.assertIn("PLAY_WINDOW_TOO_SHORT", proc.stdout)
        self.assertEqual(argv, [])

    def test_a_target_under_the_floor_is_still_refused(self) -> None:
        proc, argv = self.run_capture(self.pinned_line(), "-Seconds", "30", "-PresentedFrames", "24")
        self.assertEqual(proc.returncode, 41, proc.stdout + proc.stderr)
        self.assertIn("PLAY_WINDOW_TOO_SHORT", proc.stdout)
        self.assertEqual(argv, [])

    def test_the_receipt_oracle_still_judges_a_pinned_run_over_the_window_it_played(self) -> None:
        """Widening the gate must not widen the verdict: a receipt that wrapped, or whose engine consumed fewer than the window, stays INVALID (43)."""
        for label, line in (("wrapped", self.pinned_line(wrapped=1, wrap_count=1)),
                            ("source frames short", self.pinned_line(source_advanced=479)),
                            ("clip under the window it played", self.pinned_line(clip_seconds=19.0))):
            with self.subTest(label):
                proc, _ = self.run_capture(line, "-Seconds", "30")
                self.assertEqual(proc.returncode, EXIT_INVALID, proc.stdout + proc.stderr)
                self.assertIn("CAPTURE: INVALID", proc.stdout)
                self.assertFalse((self.tmp / "capture-out" / "reference-frame-candidate.json").exists())

    def test_a_pinned_frame_never_presented_is_a_typed_failure_not_a_candidate(self) -> None:
        """Failure shape 2, as far as the tool can see it: the app exits 11 (`exact presented-frame target was not met`) and writes no frame."""
        stderr = "[GUI-SMOKE] ERROR: exact presented-frame target was not met; target=480 observed=418."
        proc, _ = self.run_capture(None, "-Seconds", "30", exit_code=11, stderr_text=stderr)
        self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)
        self.assertIn("PINNED_FRAME_NOT_PRESENTED", proc.stdout)
        self.assertIn("target=480", proc.stdout)
        self.assertIn("observed=418", proc.stdout)
        self.assertNotIn("(NONE)", proc.stdout)
        self.assertFalse((self.tmp / "capture-out" / "reference-frame-candidate.json").exists())

    def test_any_other_app_failure_keeps_its_own_reason(self) -> None:
        proc, _ = self.run_capture(None, "-Seconds", "30", exit_code=7, stderr_text="[GUI-SMOKE] ERROR: Play action did not enter checked state.")
        self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)
        self.assertNotIn("PINNED_FRAME_NOT_PRESENTED", proc.stdout)
        self.assertIn("exe exited 7", proc.stdout)


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
    holder = _fixture_repo.pop("holder", None)
    _fixture_repo.clear()
    if holder is not None:
        holder.cleanup()   # type: ignore[attr-defined]
    if _hub_fixture_env_before is None:
        os.environ.pop(_HUB_FIXTURE_ENV, None)
    else:
        os.environ[_HUB_FIXTURE_ENV] = _hub_fixture_env_before


if __name__ == "__main__":
    unittest.main()
