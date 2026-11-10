"""LANE-LOCAL-STAGE-CHAIN-DEADLINE-BYPASS-1: the tracked attr3 assembler never reaches the heavy-slot wait without a deadline.

2026-11-10: lane-local stage-chain.ps1 / stage-one.ps1 copies (hand-copied into run dirs, never committed) called
tools/profiling/bachelor/playback-attr-3-cuda-assemble.ps1 without MLV_HEAVY_SLOT_DEADLINE_UTC. build-guard then waited its
60 min default and every retry re-queued as the youngest heavy-slot ticket. PR #776 fixed only the tracked chain
(Invoke-VenueStageChain.ps1). The assembler is the tracked choke point every chain, tracked or copied, goes through, so it
refuses typed when the deadline is absent: STAGE_NO_HEAVY_SLOT_DEADLINE, exit 27, before it writes anything or takes a ticket.

The real assembler runs on a synthetic one-commit repository. With the deadline present it stops at the missing DLL pair
(exit 3, the step after the source archive), which proves the refusal does not touch a caller that sets the deadline.
No build, no ssh, no heavy slot, no footage.

WINDOWS-ONLY PAST THE GATE (LANE-LOCAL-STAGE-CHAIN-DEADLINE-BYPASS-1 r2, Dot 791-C1). The two tests that run the assembler PAST the
deadline gate (a deadline given) reach the source-archive step's -OutDir containment check, and that check is Windows-only by contract:
AttrCudaArtifacts.psm1 Assert-AttrCudaNoLinkBelowRoot compares with a hard-coded backslash (`$full.StartsWith($rootFull + '\')`), so a POSIX
`.work-<sha>` child of `-OutDir` is ATTRCUDA_PATH_NOT_UNDER_ROOT (exit 1). Hosted CI run 42772002818 (ubuntu shard job 129681744797) failed exactly
those two tests that way. They are scoped to Windows with that reason named, not skipped silently; WindowsOnlyContractTests pins the cause on every
host, so a POSIX-correct check turns it red and the two scopes must then be dropped. The refusal tests (no deadline: exit 27 BEFORE any path
is touched) run on every host.
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

WINDOWS_ONLY_REASON = (
    "the attr3 assembler is Windows-only by contract: Assert-AttrCudaNoLinkBelowRoot (AttrCudaArtifacts.psm1) compares paths with a hard-coded "
    "backslash, so a POSIX .work-<sha> child of -OutDir is ATTRCUDA_PATH_NOT_UNDER_ROOT (original failure: Tests run 42772002818, ubuntu shard "
    "job 129681744797); the ATTR-3 host jobs run only on Windows"
)


def windows_only_assembler(test):
    """Scope a test that runs the assembler PAST the deadline gate to Windows, naming the contract; tagged so a test can list them."""
    test.windows_only_assembler = True
    return unittest.skipUnless(os.name == "nt", WINDOWS_ONLY_REASON)(test)


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

    @windows_only_assembler
    def test_a_caller_that_sets_the_deadline_is_not_changed(self) -> None:
        # Past the new check the run is the pre-existing one: the synthetic repo has no DLL pair, so it ends at exit 3 / dllPairDir.
        done = self._run("2099-01-01T00:00:00Z")
        self.assertEqual(done.returncode, 3, done.stdout + done.stderr)
        self.assertIn("STEP=dllPairDir", done.stdout)
        self.assertNotIn("STAGE_NO_HEAVY_SLOT_DEADLINE", done.stdout)

    @windows_only_assembler
    def test_an_already_passed_deadline_is_not_a_refusal_here(self) -> None:
        # The deadline's value is the guard's to judge (a passed one is a single --wait 0 attempt, bounded, not the 60 min default); this check is only "was one given".
        done = self._run("2000-01-01T00:00:00Z")
        self.assertEqual(done.returncode, 3, done.stdout + done.stderr)
        self.assertNotIn("STAGE_NO_HEAVY_SLOT_DEADLINE", done.stdout)

    def test_the_check_runs_before_the_first_step_that_writes_or_queues(self) -> None:
        text = ASSEMBLER.read_text(encoding="utf-8")
        # Anchor on the EXECUTABLE statement, not the first mention of the token (the exit-code header comment also names it; adversary e7265eea-A1).
        gate = "if ([string]::IsNullOrWhiteSpace($env:MLV_HEAVY_SLOT_DEADLINE_UTC)) {"
        self.assertEqual(text.count(gate), 1, "exactly one executable deadline gate")
        check = text.index(gate)
        block = text[check:text.index("\n}\n", check)]
        self.assertIn("STAGE_NO_HEAVY_SLOT_DEADLINE", block)
        self.assertIn("Complete-Failed 27 'heavySlotDeadline'", block, "the gate ends the run")
        for later in ("New-AttrCudaOwnedRoot", "git -C $RepoRoot archive", "Invoke-BuildGuard -Label", "New-Item -ItemType Directory -Path $OutDir"):
            self.assertIn(later, text, later)
            self.assertLess(check, text.index(later), f"the refusal must come before: {later}")

    def test_the_tracked_chain_and_nightly_still_hand_the_assembler_a_deadline(self) -> None:
        # Both tracked callers set the env in the assembler child's own environment; the refusal must never trip them.
        nightly = (ROOT / "tools" / "profiling" / "dual-venue" / "Invoke-VenueStageNightly.ps1").read_text(encoding="utf-8")
        self.assertRegex(nightly, r"Invoke-Child 'assemble' \$Assembler @\([^)]*\) -ChildEnv @\{ MLV_HEAVY_SLOT_DEADLINE_UTC = ")
        self.assertRegex(CHAIN.read_text(encoding="utf-8"), re.escape("$psi.Environment['MLV_HEAVY_SLOT_DEADLINE_UTC']"))


PSM1 = ROOT / "tools" / "profiling" / "bachelor" / "AttrCudaArtifacts.psm1"

# Runs the containment condition EXACTLY as the module's source states it (the first `if` of Assert-AttrCudaNoLinkBelowRoot that throws
# ATTRCUDA_PATH_NOT_UNDER_ROOT), on the two strings the caller gives. GetFullPath is not applied: these ARE the normalised strings the check compares.
_CONDITION_PROBE = r"""
param([string]$Psm1, [string]$Root, [string]$Candidate)
$errs = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Psm1, [ref]$null, [ref]$errs)
$fn = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Assert-AttrCudaNoLinkBelowRoot' }, $true)
$if = $fn.Body.Find({ param($n) $n -is [System.Management.Automation.Language.IfStatementAst] -and $n.Extent.Text.Contains('ATTRCUDA_PATH_NOT_UNDER_ROOT') }, $true)
$condition = $if.Clauses[0].Item1.Extent.Text
$rootFull = $Root.TrimEnd('\')
$full = $Candidate.TrimEnd('\')
Write-Output ('CONDITION=' + $condition)
Write-Output ('REFUSED=' + [bool](Invoke-Expression $condition))
"""


@unittest.skipUnless(PWSH, "needs pwsh")
class WindowsOnlyContractTests(unittest.TestCase):
    """Dot 791-C1: the cause of the ubuntu failure, pinned on EVERY host, and the scope of the two tests it forced."""

    # The exact strings of the failing CI run (job 129681744797): -OutDir and its .work-<sha12> child.
    CI_ROOT = "/tmp/tmpuom648ob/cache-out"
    CI_CANDIDATE = "/tmp/tmpuom648ob/cache-out/.work-39aeec2b3918"

    def _probe(self, root: str, candidate: str) -> "tuple[str, bool]":
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "probe.ps1"
            script.write_text(_CONDITION_PROBE, encoding="ascii")
            done = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script),
                                   "-Psm1", str(PSM1), "-Root", root, "-Candidate", candidate], capture_output=True, text=True)
        out = done.stdout + done.stderr
        self.assertEqual(done.returncode, 0, out)
        condition = re.search(r"^CONDITION=(.*)$", done.stdout, re.M)
        refused = re.search(r"^REFUSED=(True|False)$", done.stdout, re.M)
        self.assertTrue(condition and refused, out)
        return condition.group(1), refused.group(1) == "True"

    def test_the_module_refuses_the_posix_work_child_the_ci_run_saw_and_accepts_the_windows_form(self) -> None:
        # The cause as FOUND: the condition appends a backslash to the root, so a '/'-separated child never starts with it.
        # On 896acdc9 the two scoped tests ran on ubuntu and failed on exactly this refusal. A POSIX-correct check makes the
        # first assertion fail -- the signal that the Windows-only scopes can be dropped.
        condition, refused_posix = self._probe(self.CI_ROOT, self.CI_CANDIDATE)
        self.assertIn("'\\'", condition, "the compared prefix is rootFull + a backslash literal: " + condition)
        self.assertTrue(refused_posix, "a POSIX .work-<sha> child is refused by the module's own condition: " + condition)
        _, refused_windows = self._probe("C:\\work\\cache-out", "C:\\work\\cache-out\\.work-39aeec2b3918")
        self.assertFalse(refused_windows, "the same shape in the contract's own (Windows) form is accepted")

    def test_exactly_the_tests_that_run_past_the_gate_are_scoped_to_windows(self) -> None:
        cls = AssemblerRequiresHeavySlotDeadlineTests
        scoped = sorted(n for n in dir(cls) if getattr(getattr(cls, n), "windows_only_assembler", False))
        self.assertEqual(scoped, ["test_a_caller_that_sets_the_deadline_is_not_changed",
                                  "test_an_already_passed_deadline_is_not_a_refusal_here"])
        for name in scoped:
            method = getattr(cls, name)
            if os.name != "nt":
                self.assertTrue(method.__unittest_skip__, name)
                self.assertIn("job 129681744797", method.__unittest_skip_why__)
                self.assertIn("Windows-only by contract", method.__unittest_skip_why__)


if __name__ == "__main__":
    unittest.main()
