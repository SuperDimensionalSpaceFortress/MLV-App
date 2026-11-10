"""LANE-LOCAL-STAGE-CHAIN-DEADLINE-BYPASS-1: the tracked attr3 assembler never reaches the heavy-slot wait without a deadline.

2026-11-10: lane-local stage-chain.ps1 / stage-one.ps1 copies (hand-copied into run dirs, never committed) called
tools/profiling/bachelor/playback-attr-3-cuda-assemble.ps1 without MLV_HEAVY_SLOT_DEADLINE_UTC. build-guard then waited its
60 min default and every retry re-queued as the youngest heavy-slot ticket. PR #776 fixed only the tracked chain
(Invoke-VenueStageChain.ps1). The assembler is the tracked choke point every chain, tracked or copied, goes through, so it
refuses typed when the deadline is absent: STAGE_NO_HEAVY_SLOT_DEADLINE, exit 27, before it writes anything or takes a ticket.

The real assembler runs on a synthetic one-commit repository. With the deadline present it stops at the missing DLL pair
(exit 3, the step after the source archive), which proves the refusal does not touch a caller that sets the deadline.
No build, no ssh, no heavy slot, no footage.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ASSEMBLER = ROOT / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-assemble.ps1"
CHAIN = ROOT / "tools" / "profiling" / "dual-venue" / "Invoke-VenueStageChain.ps1"
ENV_NAME = "MLV_HEAVY_SLOT_DEADLINE_UTC"
PWSH = shutil.which("pwsh")
GIT = shutil.which("git")


@unittest.skipUnless(PWSH and GIT, "needs pwsh and git")
class AssemblerRequiresHeavySlotDeadlineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.repo = self.tmp / "tiny-repo"
        self.repo.mkdir()
        for args in (("init", "-q"), ("config", "user.email", "t@example.invalid"), ("config", "user.name", "t")):
            self._git(*args)
        (self.repo / "a.txt").write_text("synthetic source", encoding="utf-8")
        self._git("add", "a.txt")
        self._git("commit", "-q", "-m", "x")
        self.sha = self._git("rev-parse", "HEAD")
        self.out_dir = self.tmp / "cache-out"
        self.out_dir.mkdir()

    def _git(self, *args: str) -> str:
        proc = subprocess.run([GIT, "-C", str(self.repo), *args], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc.stdout.strip()

    def _run(self, deadline: "str | None") -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.pop(ENV_NAME, None)  # a staging driver's deadline never leaks into a test from the machine that runs it
        if deadline is not None:
            env[ENV_NAME] = deadline
        return subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(ASSEMBLER),
             "-SourceCommit", self.sha, "-DllPairDir", str(self.tmp / "no-such-dll-pair"),
             "-OutDir", str(self.out_dir), "-RepoRoot", str(self.repo)],
            capture_output=True, text=True, env=env)

    def test_no_deadline_is_a_typed_refusal_before_anything_is_written(self) -> None:
        # RED on fork/master f26387ad: the assembler ignored the missing deadline and went on to the DLL-pair step (exit 3).
        done = self._run(None)
        out = done.stdout + done.stderr
        self.assertEqual(done.returncode, 27, out)
        self.assertIn("STAGE_NO_HEAVY_SLOT_DEADLINE", done.stdout)
        self.assertIn("RESULT=ASSEMBLE_FAILED STEP=heavySlotDeadline EXIT=27", done.stdout)
        self.assertIn("Invoke-VenueStageChain.ps1", out, "the refusal names the route")
        self.assertIn(ENV_NAME, out)
        self.assertEqual(list(self.out_dir.iterdir()), [], "nothing is created in -OutDir before the refusal")

    def test_a_blank_deadline_is_the_same_refusal(self) -> None:
        # build-guard reads a blank value as "none" (60 min default), so a blank one is not a deadline.
        for blank in ("", "   "):
            with self.subTest(blank=repr(blank)):
                done = self._run(blank)
                self.assertEqual(done.returncode, 27, done.stdout + done.stderr)
                self.assertIn("STAGE_NO_HEAVY_SLOT_DEADLINE", done.stdout)
                self.assertEqual(list(self.out_dir.iterdir()), [])

    def test_a_caller_that_sets_the_deadline_is_not_changed(self) -> None:
        # Past the new check the run is the pre-existing one: the synthetic repo has no DLL pair, so it ends at exit 3 / dllPairDir.
        done = self._run("2099-01-01T00:00:00Z")
        self.assertEqual(done.returncode, 3, done.stdout + done.stderr)
        self.assertIn("STEP=dllPairDir", done.stdout)
        self.assertNotIn("STAGE_NO_HEAVY_SLOT_DEADLINE", done.stdout)

    def test_an_already_passed_deadline_is_not_a_refusal_here(self) -> None:
        # The deadline's value is the guard's to judge (a passed one is --wait 0); this check is only "was one given".
        done = self._run("2000-01-01T00:00:00Z")
        self.assertEqual(done.returncode, 3, done.stdout + done.stderr)
        self.assertNotIn("STAGE_NO_HEAVY_SLOT_DEADLINE", done.stdout)

    def test_the_check_runs_before_the_first_step_that_writes_or_queues(self) -> None:
        text = ASSEMBLER.read_text(encoding="utf-8")
        check = text.index("STAGE_NO_HEAVY_SLOT_DEADLINE")
        for later in ("New-AttrCudaOwnedRoot", "git -C $RepoRoot archive", "Invoke-BuildGuard -Label", "New-Item -ItemType Directory -Path $OutDir"):
            self.assertIn(later, text, later)
            self.assertLess(check, text.index(later), f"the refusal must come before: {later}")

    def test_the_tracked_chain_and_nightly_still_hand_the_assembler_a_deadline(self) -> None:
        # Both tracked callers set the env in the assembler child's own environment; the refusal must never trip them.
        nightly = (ROOT / "tools" / "profiling" / "dual-venue" / "Invoke-VenueStageNightly.ps1").read_text(encoding="utf-8")
        self.assertRegex(nightly, r"Invoke-Child 'assemble' \$Assembler @\([^)]*\) -ChildEnv @\{ MLV_HEAVY_SLOT_DEADLINE_UTC = ")
        self.assertRegex(CHAIN.read_text(encoding="utf-8"), re.escape("$psi.Environment['MLV_HEAVY_SLOT_DEADLINE_UTC']"))


if __name__ == "__main__":
    unittest.main()
