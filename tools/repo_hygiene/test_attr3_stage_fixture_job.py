"""Behavioural tests for tools/profiling/bachelor/attr3-stage-fixture-job.ps1.

The generator emits a job that publishes ONE tracked repository fixture clip into the measurement
host's cache (ATTR3-FIXTURE-REHEARSAL-1). These tests run the generator for real, then run the
EMITTED job against a temporary directory shaped like the agent root, so the assertions are about
what ends up on disk and what the job refuses -- not about what the script says.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "tools" / "profiling" / "bachelor" / "attr3-stage-fixture-job.ps1"
UMRUNDROP_MODULE = ROOT / "tools" / "profiling" / "UmRunDrop.psm1"
ATTR_CUDA_MODULE = ROOT / "tools" / "profiling" / "bachelor" / "AttrCudaArtifacts.psm1"
FIXTURES = ROOT / "tests" / "fixtures" / "clips"
FIXTURE_STEMS = ("tiny_dual_iso", "large_dual_iso")
PWSH = shutil.which("pwsh")
GIT = shutil.which("git")


def _q(path: Path | str) -> str:
    return "'" + str(path).replace("'", "''") + "'"


@unittest.skipIf(PWSH is None, "pwsh is not on PATH")
@unittest.skipIf(GIT is None, "git is not on PATH")
@unittest.skipUnless(os.name == "nt", "the emitted job targets a Windows measurement host")
class StageFixtureJobTests(unittest.TestCase):
    def setUp(self) -> None:
        if not FIXTURES.is_dir():
            self.skipTest("no repository fixture clips directory")
        clips = [f for f in FIXTURES.iterdir() if f.is_file() and f.stem in FIXTURE_STEMS]
        if not clips:
            self.skipTest("no repository fixture clips")
        self.clip = sorted(clips, key=lambda f: f.stat().st_size)[0]
        self._tmp = tempfile.TemporaryDirectory(prefix="attr3stage-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(os.path.realpath(self._tmp.name))
        self.agent = self.tmp / "agent"
        self.inbox = self.agent / "inbox"
        self.cache = self.agent / "cache"
        self.inbox.mkdir(parents=True)
        self.cache.mkdir()
        self.out = self.tmp / "out"
        self.out.mkdir()

    def generate(self, stem: str | None = None, clip: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(GENERATOR),
             "-ClipStem", stem or self.clip.stem, "-FixturePath", str(clip or self.clip),
             "-OutDir", str(self.out), "-AgentRoot", str(self.agent)],
            capture_output=True, text=True,
        )

    def job_path(self, proc: subprocess.CompletedProcess) -> Path:
        jobs = sorted(self.out.glob("*.job.ps1"))
        self.assertEqual(len(jobs), 1, proc.stdout + proc.stderr)
        return jobs[0]

    def run_job(self, job: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(job), *args],
            capture_output=True, text=True,
        )

    def drop_side_file(self) -> None:
        shutil.copy2(self.clip, self.inbox / self.clip.name)

    # ---- generator refusals ------------------------------------------------------------------

    def test_a_clip_outside_the_repository_fixtures_is_refused(self) -> None:
        impostor = self.tmp / self.clip.name
        shutil.copy2(self.clip, impostor)
        proc = self.generate(clip=impostor)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("ATTR3_FIXTURE_NOT_TRACKED", proc.stdout + proc.stderr)
        self.assertEqual(sorted(self.out.iterdir()), [])

    def test_a_stem_that_does_not_match_the_file_is_refused(self) -> None:
        other = "large_dual_iso" if self.clip.stem == "tiny_dual_iso" else "tiny_dual_iso"
        proc = self.generate(stem=other)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("ATTR3_FIXTURE_STEM_MISMATCH", proc.stdout + proc.stderr)

    # Generator-time admission/bake race coverage (sol, PR #140 r2c) moved to
    # StageFixtureJobRaceTests below (sol, PR #140 r2e MAJOR): it used to corrupt THIS class's
    # self.clip -- the REAL tracked repository fixture -- relying on unittest's addCleanup to
    # restore it, so a killed test process left the real checkout dirty. See that class.

    # ---- emitted job -------------------------------------------------------------------------

    def test_verify_only_touches_nothing_and_reports_the_baked_hash(self) -> None:
        proc = self.generate()
        job = self.job_path(proc)
        self.drop_side_file()
        run = self.run_job(job, "-VerifyOnly")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("RESULT=VERIFY_ONLY_OK", run.stdout)
        self.assertIn(hashlib.sha256(self.clip.read_bytes()).hexdigest(), run.stdout)
        self.assertEqual(sorted(p.name for p in self.cache.iterdir()), [])
        self.assertEqual(sorted(p.name for p in self.inbox.iterdir()), [self.clip.name])

    def test_a_complete_run_publishes_the_clip_and_empties_the_inbox(self) -> None:
        proc = self.generate()
        job = self.job_path(proc)
        self.drop_side_file()
        run = self.run_job(job)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("RESULT=FIXTURE_STAGE_OK", run.stdout)
        staged = self.cache / self.clip.name
        self.assertTrue(staged.is_file())
        self.assertEqual(
            hashlib.sha256(staged.read_bytes()).hexdigest(),
            hashlib.sha256(self.clip.read_bytes()).hexdigest(),
        )
        self.assertEqual(sorted(p.name for p in self.inbox.iterdir()), [])
        self.assertFalse(any(p.name.endswith(".partial") for p in self.cache.iterdir()))
        result = json.loads((self.agent / "outbox" / f"{job.stem.replace('.job', '')}.artifacts" / "result.json").read_text(encoding="utf-8"))
        self.assertTrue(result["fixtureRehearsal"])
        self.assertFalse(result["alreadyStaged"])

    def test_a_tampered_side_file_publishes_nothing(self) -> None:
        proc = self.generate()
        job = self.job_path(proc)
        (self.inbox / self.clip.name).write_bytes(b"swapped after the hash was baked")
        run = self.run_job(job)
        self.assertEqual(run.returncode, 4, run.stdout + run.stderr)
        self.assertIn("STEP=fixtureHash", run.stdout)
        self.assertEqual(sorted(p.name for p in self.cache.iterdir()), [])

    def test_a_missing_side_file_fails_closed(self) -> None:
        proc = self.generate()
        job = self.job_path(proc)
        run = self.run_job(job)
        self.assertEqual(run.returncode, 3, run.stdout + run.stderr)
        self.assertIn("STEP=inboxSideFile", run.stdout)

    def test_restaging_identical_bytes_is_a_no_op(self) -> None:
        proc = self.generate()
        job = self.job_path(proc)
        self.drop_side_file()
        self.assertEqual(self.run_job(job).returncode, 0)
        self.drop_side_file()
        again = self.run_job(job)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("ALREADY=1", again.stdout)

    def test_a_cache_entry_with_different_bytes_is_not_overwritten(self) -> None:
        proc = self.generate()
        job = self.job_path(proc)
        (self.cache / self.clip.name).write_bytes(b"someone else staged this")
        self.drop_side_file()
        run = self.run_job(job)
        self.assertEqual(run.returncode, 21, run.stdout + run.stderr)
        self.assertEqual((self.cache / self.clip.name).read_bytes(), b"someone else staged this")

    # ---- ATTR3-FIXTURE-STAGE-1 (sol, PR #139 r1 MINOR): inbox cleanup -----------------------

    def test_already_staged_branch_also_removes_the_inbox_copy(self) -> None:
        proc = self.generate()
        job = self.job_path(proc)
        self.drop_side_file()
        first = self.run_job(job)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.drop_side_file()
        self.assertEqual(sorted(p.name for p in self.inbox.iterdir()), [self.clip.name])
        second = self.run_job(job)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn("ALREADY=1", second.stdout)
        self.assertEqual(
            sorted(p.name for p in self.inbox.iterdir()),
            [],
            "the already-staged branch left its inbox copy behind",
        )

    def _make_junction(self, link: Path, target: Path) -> bool:
        proc = subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
             f"New-Item -ItemType Junction -Path '{link}' -Target '{target}' | Out-Null"],
            capture_output=True, text=True,
        )
        return proc.returncode == 0 and link.exists()

    def test_a_refused_inbox_cleanup_fails_closed_after_a_successful_publish(self) -> None:
        # sol, PR #139 r1 MINOR: the normal path used to discard Remove-AttrCudaPartialFile's
        # boolean result and record inboxCleanup=0 (success) even when it was refused. Route
        # the inbox through a junction: the guarded remover's own ancestor-chain check refuses
        # to touch anything reached through a reparse point and returns $false -- a real refusal
        # of the exact kind Remove-AttrCudaPartialFile is documented to report, not a crash.
        self.inbox.rmdir()
        outside = self.tmp / "outside-inbox"
        outside.mkdir()
        if not self._make_junction(self.inbox, outside):
            self.skipTest("cannot create a junction here")
        proc = self.generate()
        job = self.job_path(proc)
        shutil.copy2(self.clip, self.inbox / self.clip.name)

        run = self.run_job(job)

        self.assertEqual(run.returncode, 22, run.stdout + run.stderr)
        self.assertIn("STEP=inboxCleanup", run.stdout)
        # The cache publish itself must have already succeeded -- only cleanup failed closed.
        staged = self.cache / self.clip.name
        self.assertTrue(staged.is_file())
        self.assertEqual(
            hashlib.sha256(staged.read_bytes()).hexdigest(),
            hashlib.sha256(self.clip.read_bytes()).hexdigest(),
        )
        self.assertTrue(
            (outside / self.clip.name).is_file(),
            "a refused cleanup must leave the file exactly where it was",
        )


@unittest.skipIf(PWSH is None, "pwsh is not on PATH")
@unittest.skipIf(GIT is None, "git is not on PATH")
@unittest.skipUnless(os.name == "nt", "the emitted job targets a Windows measurement host")
class StageFixtureJobRaceTests(unittest.TestCase):
    """sol, PR #140 r2e MAJOR: the generator-time admission/bake race coverage (sol, PR #140 r2c)
    used to live in StageFixtureJobTests and corrupted THE REAL TRACKED FIXTURE in
    tests/fixtures/clips directly via -PostAdmissionHook, relying on unittest's addCleanup to
    restore the original bytes afterward -- so a hard-killed test process (timeout, Ctrl-C, a
    crash in an earlier assertion) left the real checkout holding corrupted fixture bytes with
    nothing left to restore them.

    This exercises the IDENTICAL race against a throwaway git repository instead. The generator
    resolves its own -RepoRoot as three levels up from wherever ITS OWN SCRIPT FILE lives
    ($PSScriptRoot), and imports UmRunDrop.psm1 and AttrCudaArtifacts.psm1 from paths relative to
    that same location -- so a COPY of all three, placed at the identical relative layout
    (<disposable root>\\tools\\profiling\\bachelor\\attr3-stage-fixture-job.ps1 and its two
    dependency modules) under a disposable git repository, resolves its trusted root to that
    disposable repository exclusively. A fixture is committed under ITS OWN
    tests\\fixtures\\clips, never the real repository's. Nothing under the real working tree is
    ever opened for writing.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="attr3stage-race-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(os.path.realpath(self._tmp.name))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        for args in (
            ["init", "-q"],
            ["config", "commit.gpgsign", "false"],
            ["config", "user.email", "attr3stage-race@example.invalid"],
            ["config", "user.name", "Attr3 Stage Race Test"],
            # fable, PR #140 r2d MINOR: see test_playback_attr_3_cuda_behaviour.py's
            # _make_fixture_repo -- this repo's content-pin check is the same reused code path.
            ["config", "core.autocrlf", "true"],
        ):
            subprocess.run([GIT, *args], cwd=str(self.repo), capture_output=True, text=True, check=True)

        clips = self.repo / "tests" / "fixtures" / "clips"
        clips.mkdir(parents=True)
        self.clip = clips / "tiny_dual_iso.umrunprobe"
        self.clip.write_bytes(b"committed fixture bytes for the disposable generator race repo")
        subprocess.run([GIT, "add", "tests/fixtures/clips/tiny_dual_iso.umrunprobe"],
                        cwd=str(self.repo), capture_output=True, text=True, check=True)
        subprocess.run([GIT, "commit", "-q", "-m", "fixture"],
                        cwd=str(self.repo), capture_output=True, text=True, check=True)

        # The real generator and its two dependency modules, copied to the SAME relative layout
        # under the disposable root, so every $PSScriptRoot-derived path (the generator's own
        # -RepoRoot, and its two Import-Module calls) resolves inside the disposable tree only.
        bachelor_dir = self.repo / "tools" / "profiling" / "bachelor"
        bachelor_dir.mkdir(parents=True)
        self.generator = bachelor_dir / GENERATOR.name
        shutil.copy2(GENERATOR, self.generator)
        shutil.copy2(ATTR_CUDA_MODULE, bachelor_dir / ATTR_CUDA_MODULE.name)
        shutil.copy2(UMRUNDROP_MODULE, self.repo / "tools" / "profiling" / UMRUNDROP_MODULE.name)

        self.out = self.tmp / "out"
        self.out.mkdir()
        self.agent = self.tmp / "agent"
        (self.agent / "inbox").mkdir(parents=True)
        (self.agent / "cache").mkdir()

    def generate_with_post_admission_hook(self, hook: str) -> subprocess.CompletedProcess:
        # sol, PR #140 r2c: -PostAdmissionHook is a test-only seam (no-op in production, mirroring
        # Invoke-UmRunDrop's identical seam in tools/profiling/UmRunDrop.psm1) that fires in the
        # exact gap between admission verifying the fixture's committed bytes and this generator's
        # own re-read of those bytes for the sha256 it bakes -- the gap a swap would need to win.
        script = self.tmp / "generate-with-hook.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"& {_q(self.generator)} -ClipStem {_q(self.clip.stem)} -FixturePath {_q(self.clip)} "
            f"-OutDir {_q(self.out)} -AgentRoot {_q(self.agent)} -PostAdmissionHook {hook}\n",
            encoding="utf-8",
        )
        return subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script)],
            capture_output=True, text=True,
        )

    def test_bytes_swapped_between_admission_and_the_baked_hash_are_refused(self) -> None:
        # THE DEFECT this closes: the generator used to call Assert-AttrCudaFixtureCommittedBytes
        # to verify the committed bytes, then take a SECOND, independent Get-FileHash read to bake
        # into the job -- bytes swapped in that gap would silently bake the SWAPPED bytes' hash
        # with no error at all. -PostAdmissionHook fires in exactly that gap and is never set in
        # production; it stands in for a real attacker/race window. self.clip is this test's own
        # disposable fixture, so corrupting it is exactly the point and needs no restoration.
        hook = (
            "{ param($p) [IO.File]::WriteAllBytes($p, "
            "[Text.Encoding]::UTF8.GetBytes('raced bytes (attr3-stage-fixture-job race test)')) }"
        )

        proc = self.generate_with_post_admission_hook(hook)

        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("ATTR3_FIXTURE_CONTENT_PIN_RACE", proc.stdout + proc.stderr)
        self.assertEqual(
            sorted(self.out.iterdir()), [], "a raced fixture must never reach an emitted job"
        )

    def test_a_no_op_hook_still_bakes_the_correct_hash(self) -> None:
        # Positive control: the new check must not false-positive when nothing raced.
        proc = self.generate_with_post_admission_hook("{ param($p) }")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(hashlib.sha256(self.clip.read_bytes()).hexdigest(), proc.stdout)
        self.assertEqual(len(sorted(self.out.glob("*.job.ps1"))), 1, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
