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


if __name__ == "__main__":
    unittest.main()
