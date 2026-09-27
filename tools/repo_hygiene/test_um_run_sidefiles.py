"""Behavioural tests for um-run.ps1 side-file placement (tools/profiling/UmRunDrop.psm1).

The placement code is exercised two ways:
  - through the module with an OBSERVING or FAULTY copier, which is what proves the properties a
    final-state check cannot (sol, PR #135 r1): the share-side hash comparison actually rejects
    altered bytes, every side-file is renamed into place before the job's temporary copy is even
    written, and renames never overwrite a destination that appears concurrently;
  - end to end through um-run.ps1 against a fake agent share with a fresh heartbeat and no agent,
    so each submission times out right after its drop.
"""

from __future__ import annotations

import re
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

# hub: pwsh colours its error view with ANSI escapes whenever the parent environment advertises a colour
# terminal (e.g. TERM=xterm-256color), and a wrapped error line then carries an escape mid-phrase. Strip them
# so the phrase assertions below do not depend on the caller's terminal.
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


ROOT = Path(__file__).resolve().parents[2]
UM_RUN = ROOT / "tools" / "profiling" / "um-run.ps1"
MODULE = ROOT / "tools" / "profiling" / "UmRunDrop.psm1"
PWSH = shutil.which("pwsh")
GIT = shutil.which("git")

# Tracked-fixture admission asks git whether the file is tracked, so those cases need git.
requires_git = unittest.skipIf(GIT is None, "git is not on PATH")


def _q(path: Path | str) -> str:
    return "'" + str(path).replace("'", "''") + "'"


@unittest.skipIf(PWSH is None, "pwsh is not on PATH")
@unittest.skipUnless(os.name == "nt", "um-run.ps1 targets Windows agent shares")
class _Share(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="umrun-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(os.path.realpath(self._tmp.name))
        self.share = self.tmp / "agent"
        self.inbox = self.share / "inbox"
        self.outbox = self.share / "outbox"
        self.inbox.mkdir(parents=True)
        self.outbox.mkdir()
        (self.share / "heartbeat.txt").write_text("alive", encoding="utf-8")
        self.local = self.tmp / "local"
        self.local.mkdir()
        self.job = self.local / "demo.job.ps1"
        self.job.write_text("Write-Output 'hi'\n", encoding="utf-8")
        self.side = self.local / "demo-source.zip"
        self.side.write_bytes(os.urandom(4096))
        self.log = self.tmp / "copies.log"

    def names(self) -> list[str]:
        return sorted(p.name for p in self.inbox.iterdir())

    def touch_heartbeat(self, *, job_id: str | None = None) -> None:
        # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 10: rewrites heartbeat.txt with a fresh
        # LastWriteTimeUtc, mirroring the shape the deployed/tracked agent actually writes
        # (ultra-magnus-agent.ps1's Write-AgentHeartbeat) closely enough for um-run.ps1's own
        # ` job=<id>` regex to match -- so a test can simulate "the agent is genuinely still
        # working on this job" without a real agent process.
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        line = f'alive {now} pid=1 host=TESTHOST generation=1 processStartUtc={now} imagePath="x" agentScript="y"'
        if job_id:
            line += f" job={job_id}"
        (self.share / "heartbeat.txt").write_text(line, encoding="utf-8")

    def drop(self, copier: str, *, side: list[Path] | None = None, job_id: str = "demo",
             job_timeout_sec: int | None = None,
             before_job_visible: str | None = None,
             after_meta_tmp_written: str | None = None) -> subprocess.CompletedProcess:
        sides = ",".join(_q(p) for p in (side if side is not None else [self.side]))
        timeout_arg = "" if job_timeout_sec is None else f" -JobTimeoutSec {job_timeout_sec}"
        script = self.tmp / "drop.ps1"
        preamble = (
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module {_q(MODULE)} -Force\n"
            f"$log = {_q(self.log)}\n"
            f"$copier = {copier}\n"
        )
        hook_args = ""
        if before_job_visible is not None:
            preamble += f"$beforeJobVisible = {before_job_visible}\n"
            hook_args += " -TestHookBeforeJobVisible $beforeJobVisible"
        if after_meta_tmp_written is not None:
            preamble += f"$afterMetaTmpWritten = {after_meta_tmp_written}\n"
            hook_args += " -TestHookAfterMetaTmpWritten $afterMetaTmpWritten"
        script.write_text(
            preamble +
            "try {\n"
            f"  Invoke-UmRunDrop -Inbox {_q(self.inbox)} -Outbox {_q(self.outbox)} -ScriptPath {_q(self.job)} "
            f"-JobId '{job_id}' -SideFile @({sides}){timeout_arg}{hook_args} -Copier $copier\n"
            "} catch { Write-Output ('THREW ' + $_.Exception.Message) }\n",
            encoding="utf-8",
        )
        return subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script)],
                              capture_output=True, text=True)


OBSERVING = "{ param($s, $d) Add-Content -LiteralPath $log -Value $d; Copy-Item -LiteralPath $s -Destination $d }"
CORRUPTING = ("{ param($s, $d) Copy-Item -LiteralPath $s -Destination $d; "
              "if ($d -like '*.sidepart') { [IO.File]::AppendAllText($d, 'x') } }")
RACING_SIDE = ("{ param($s, $d) Copy-Item -LiteralPath $s -Destination $d; "
               "if ($d -like '*.sidepart') { $final = $d -replace '\\.[0-9a-f]{32}\\.sidepart$', ''; "
               "[IO.File]::WriteAllText($final, 'another submitter') } }")
RACING_JOB = ("{ param($s, $d) Copy-Item -LiteralPath $s -Destination $d; "
              "if ($d -like '*.job.tmp') { $final = $d -replace '\\.[0-9a-f]{32}\\.job\\.tmp$', '.job.ps1'; "
              "[IO.File]::WriteAllText($final, 'Write-Output concurrent') } }")
# ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 2 (fable/sol major 1), round 4 (sol major: reordered so
# the job's own bytes are copied BEFORE metadata is written at all -- see UmRunDrop.psm1's own
# header). The job's own copy is made to fail, simulating the share hiccup / concurrent-rename
# class of failure fable's review used as its repro; because it fails before metadata is ever
# written, this now proves the weaker "nothing partial survives the very first copy" property --
# see test_a_racing_job_rename_with_a_budget_rolls_back_its_own_metadata below for the round-4
# rollback proof that actually exercises metadata-then-job-rename-fails.
JOB_COPY_FAILS = ("{ param($s, $d) if ($d -like '*.job.tmp') { throw 'INJECTED_JOB_COPY_FAILURE' }; "
                   "Copy-Item -LiteralPath $s -Destination $d }")


class UmRunDropModuleTests(_Share):
    def test_every_side_file_is_in_place_before_the_job_temp_is_written(self) -> None:
        second = self.local / "demo-build.json"
        second.write_text("{}", encoding="utf-8")
        proc = self.drop(OBSERVING, side=[self.side, second])
        self.assertIn("UMRUN_JOBID=demo", proc.stdout, proc.stdout + proc.stderr)
        copies = [Path(line).name for line in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(copies), 3, copies)
        self.assertTrue(copies[0].endswith(".sidepart") and copies[1].endswith(".sidepart"), copies)
        self.assertTrue(copies[2].endswith(".job.tmp"), copies)
        # unique per-submission temporary names, never the bare shared forms
        self.assertNotIn("demo-source.zip.sidepart", copies)
        self.assertNotIn("demo.job.tmp", copies)
        # round 10: every submission claims inbox\<id>.meta.json first, with or without a budget --
        # this call passed none, so the claim's own metadata omits `timeoutSec` (see
        # test_metadata_is_still_claimed_when_no_budget_is_requested_but_omits_timeoutsec below).
        self.assertEqual(self.names(), ["demo-build.json", "demo-source.zip", "demo.job.ps1", "demo.meta.json"])

    # ---- agent-side job budget (ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1) --------------------------
    # The agent reads inbox\<id>.meta.json when it claims a job and honours timeoutSec in 1..86400,
    # falling back to its own 1800 s default when the file is missing or unparseable. Before this,
    # nothing wrote that file: um-run's -TimeoutSec reached only the client poll, so a multi-GB
    # placement asked to take an hour was killed at 30 minutes, twice, discarding a ~50 min transfer.

    def test_the_job_budget_is_in_place_before_the_job_becomes_visible(self) -> None:
        # sol round 4 minor: this used to prove metadata-before-the-job's-OWN-temporary-copy (a
        # copier hook fired when demo.job.tmp was written) -- the wrong instant, since round 4
        # reordered the module to copy the job's bytes to that same temporary FIRST, before
        # metadata is written at all (see UmRunDrop.psm1's own header). The real contract the agent
        # depends on is metadata-before-the-job-becoming-VISIBLE (the rename to demo.job.ps1) --
        # -TestHookBeforeJobVisible fires at exactly that instant, whatever the module's internal
        # copy order is.
        hook = ("{ Add-Content -LiteralPath " + _q(self.log) + " -Value ('meta-present-before-visible=' + "
                "(Test-Path -LiteralPath (Join-Path " + _q(self.inbox) + " 'demo.meta.json'))) }")
        proc = self.drop(OBSERVING, side=[], job_timeout_sec=3600, before_job_visible=hook)
        self.assertIn("UMRUN_JOBID=demo", proc.stdout, proc.stdout + proc.stderr)
        lines = self.log.read_text(encoding="utf-8").splitlines()
        self.assertIn("meta-present-before-visible=True", lines, lines)
        self.assertEqual(self.names(), ["demo.job.ps1", "demo.meta.json"])
        meta = json.loads((self.inbox / "demo.meta.json").read_text(encoding="ascii"))
        self.assertEqual(meta["timeoutSec"], 3600)
        self.assertEqual(meta["jobId"], "demo")

    def test_metadata_is_claimed_before_any_side_file_or_job_byte_is_copied(self) -> None:
        # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 10: this INVERTS the pre-round-10 ordering test
        # (metadata was published only after the job's bytes already sat on the share, narrowing
        # the "visible with no job" window to a single rename). Round 10 needs the opposite: the
        # claim is the FIRST thing written, before a single side-file or job byte -- see this
        # module's own header. The copier below observes the live inbox for demo.meta.json at the
        # instant it copies EITHER the side-file's own temporary OR the job's -- proving the claim
        # is already on disk before both, not just before the job's.
        copier = ("{ param($s, $d) if ($d -like '*.sidepart' -or $d -like '*.job.tmp') { "
                  "Add-Content -LiteralPath " + _q(self.log) +
                  " -Value (($(if ($d -like '*.sidepart') { 'sidefile' } else { 'job' })) + "
                  "'-copy meta-present=' + (Test-Path -LiteralPath (Join-Path " +
                  _q(self.inbox) + " 'demo.meta.json'))) }; Copy-Item -LiteralPath $s -Destination $d }")
        proc = self.drop(copier, job_timeout_sec=3600)   # self.side is a real side-file by default
        self.assertIn("UMRUN_JOBID=demo", proc.stdout, proc.stdout + proc.stderr)
        lines = self.log.read_text(encoding="utf-8").splitlines()
        self.assertIn("sidefile-copy meta-present=True", lines, lines)
        self.assertIn("job-copy meta-present=True", lines, lines)

    def test_metadata_is_still_claimed_when_no_budget_is_requested_but_omits_timeoutsec(self) -> None:
        # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 10: before this round, no budget meant no
        # metadata at all -- round 10's claim-first ownership needs EVERY submission to claim the
        # JobId, with or without a budget, so the claim's own metadata is still written, just with
        # `timeoutSec` omitted -- which both the tracked and deployed agents already treat as "fall
        # back to my own default" (confirmed against the deployed agent; see summary.md), so this
        # is not a new parser rule.
        proc = self.drop(OBSERVING, side=[])
        self.assertIn("UMRUN_JOBID=demo", proc.stdout, proc.stdout + proc.stderr)
        self.assertEqual(self.names(), ["demo.job.ps1", "demo.meta.json"],
                         "a caller that names no budget must still claim the JobId")
        meta = json.loads((self.inbox / "demo.meta.json").read_text(encoding="ascii"))
        self.assertEqual(meta["jobId"], "demo")
        self.assertNotIn("timeoutSec", meta,
                         "a caller that names no budget must leave the agent on its own default")

    def test_a_budget_outside_the_agents_accepted_range_is_refused_and_nothing_is_placed(self) -> None:
        for bad in (86401, -1):
            with self.subTest(timeout=bad):
                for leftover in self.inbox.iterdir():
                    leftover.unlink()
                proc = self.drop(OBSERVING, side=[], job_timeout_sec=bad)
                self.assertIn("THREW UMRUN_JOB_TIMEOUT_INVALID", proc.stdout, proc.stdout + proc.stderr)
                self.assertEqual(self.names(), [],
                                 "a refused budget must leave neither metadata nor a job")

    def test_an_invalid_budget_is_refused_before_any_sidefile_is_copied(self) -> None:
        # fable/sol minor 1: the range check used to run AFTER the side-file loop, so an invalid
        # -JobTimeoutSec was only discovered after a possibly multi-GB transfer had already happened.
        proc = self.drop(OBSERVING, job_timeout_sec=86401)   # self.side is a real side-file by default
        self.assertIn("THREW UMRUN_JOB_TIMEOUT_INVALID", proc.stdout, proc.stdout + proc.stderr)
        self.assertFalse(self.log.exists(), "no copy may happen before the budget is validated")
        self.assertEqual(self.names(), [])

    def test_a_failed_job_copy_removes_the_metadata_so_a_retry_with_the_same_job_id_succeeds(self) -> None:
        # fable/sol major 1: metadata was placed and never rolled back when the job's OWN copy then
        # failed. Deterministic job ids (playback-attr-3-cuda-dll-job.ps1, ...-stage-job.ps1) have no
        # attempt nonce, so every retry of the same id was then refused (UMRUN_JOBID_IN_USE) against
        # a job/result that never actually existed -- this is the SUBMIT-RETRY-1 bug itself.
        first = self.drop(JOB_COPY_FAILS, side=[], job_timeout_sec=3600)
        self.assertIn("THREW", first.stdout, first.stdout + first.stderr)
        self.assertNotIn("UMRUN_JOBID=demo", first.stdout)
        self.assertEqual(self.names(), [],
                         "a failed job placement must leave neither its own metadata nor a job behind")

        retry = self.drop(OBSERVING, side=[], job_timeout_sec=3600)
        self.assertIn("UMRUN_JOBID=demo", retry.stdout, retry.stdout + retry.stderr)
        self.assertEqual(self.names(), ["demo.job.ps1", "demo.meta.json"],
                         "the retry must succeed exactly as if the first attempt had never happened")

    def test_a_racing_job_rename_with_a_budget_rolls_back_its_own_metadata(self) -> None:
        # sol round 4 major (item 4/5): the "budgeted final-job rename refusal" coverage sol named
        # as absent. With the round-4 reorder, metadata is published only just before the job's
        # final rename -- this proves that when that LAST rename loses a race (another submitter's
        # job.ps1 appears first), this call's own metadata is rolled back exactly like the
        # job-copy-failure case above, never left to brick a later retry.
        proc = self.drop(RACING_JOB, side=[], job_timeout_sec=3600)
        self.assertIn("THREW UMRUN_JOBID_IN_USE", proc.stdout, proc.stdout + proc.stderr)
        self.assertEqual((self.inbox / "demo.job.ps1").read_text(encoding="utf-8"), "Write-Output concurrent")
        self.assertEqual(self.names(), ["demo.job.ps1"],
                         "a lost job-rename race must roll back this call's own metadata, not leave it behind")

    def test_two_racing_claims_for_the_same_jobid_exactly_one_proceeds_metadata_belongs_to_the_winner(self) -> None:
        # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 10 (item 3's required concurrency test):
        # claim-first moves the sole tiebreaker for two submitters racing the same JobId to the
        # metadata rename itself, at the very start of the call -- before either has touched a
        # side-file or job byte (round-4/8 era code raced at the JOB's own final rename instead,
        # after side-files and the job's own copy had already happened for BOTH submitters; that
        # race no longer exists to test, since the loser here never reaches it). Simulates a second
        # submitter's own claim landing in between this call's meta.tmp write and its own rename to
        # demo.meta.json -- -TestHookAfterMetaTmpWritten fires at exactly that instant.
        hook = ("{ param($p) [IO.File]::WriteAllText(" + _q(self.inbox / 'demo.meta.json') +
                ", '{\"jobId\":\"demo\",\"nonce\":\"winner-nonce\"}') }")
        proc = self.drop(OBSERVING, side=[], job_timeout_sec=3600, after_meta_tmp_written=hook)
        self.assertIn("THREW UMRUN_JOBID_IN_USE", proc.stdout, proc.stdout + proc.stderr)
        meta = json.loads((self.inbox / "demo.meta.json").read_text(encoding="ascii"))
        self.assertEqual(meta["nonce"], "winner-nonce",
                         "the loser's own claim must never overwrite the winner's")
        self.assertEqual(self.names(), ["demo.meta.json"],
                         "the loser must touch no side-file or job byte once its own claim is refused")

    def test_a_rollback_deletion_failure_is_surfaced_not_silently_swallowed(self) -> None:
        # sol round 6 minor, re-raised independently by both keys at round 7: reverting the rollback
        # deletion's -ErrorAction Stop + folded message back to SilentlyContinue fails no existing
        # test -- test_a_stale_orphan_that_cannot_be_removed_... below covers a DIFFERENT removal
        # site (the pre-existing-orphan self-heal), never the rollback path exercised by
        # test_a_racing_job_rename_with_a_budget_rolls_back_its_own_metadata above. This forces THAT
        # removal itself to fail: -TestHookBeforeJobVisible opens this submission's own just-placed
        # demo.meta.json with FileShare.None (an exclusive lock held for the rest of this process)
        # and then plants a concurrent demo.job.ps1, so the module's own final rename loses its race
        # exactly as in the passing case above, but this time its rollback's Remove-Item hits a real
        # sharing violation and must surface it, not silently continue as if cleanup had succeeded.
        hook = (
            "{ "
            f"$script:umrunTestLock = [IO.File]::Open({_q(self.inbox / 'demo.meta.json')}, 'Open', 'Read', 'None'); "
            f"[IO.File]::WriteAllText({_q(self.inbox / 'demo.job.ps1')}, 'Write-Output concurrent') "
            "}"
        )
        proc = self.drop(OBSERVING, side=[], job_timeout_sec=3600, before_job_visible=hook)
        combined = proc.stdout + proc.stderr
        self.assertIn("THREW UMRUN_JOBID_IN_USE", combined, combined)
        self.assertIn("could not be removed during rollback", combined, combined)
        self.assertIn("may outlive this refused submission", combined, combined)
        # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 9 (sol/astra test-strength minor): every
        # assertion above also passes if $($originalError.Exception.Message) in the fold is
        # replaced by the literal "UMRUN_JOBID_IN_USE" -- both the "THREW" prefix and the two
        # static fold-suffix strings are unaffected by that substitution, since the real original
        # message here ALSO starts with the literal "UMRUN_JOBID_IN_USE". What only the real
        # $originalError.Exception.Message carries is the rest of the job-rename-race throw's own
        # DISTINCT text (UmRunDrop.psm1's "inbox\<id>.job.ps1 appeared concurrently; refusing to
        # replace it") -- asserting on that proves the actual original exception's message, not a
        # constant standing in for it, survived the fold.
        self.assertIn("inbox\\demo.job.ps1 appeared concurrently; refusing to replace it", combined, combined)
        self.assertEqual((self.inbox / "demo.job.ps1").read_text(encoding="utf-8"), "Write-Output concurrent")
        # The lock made removal genuinely fail -- the metadata must still be sitting there, proving
        # the folded message describes a real failure and not a lucky-looking string.
        self.assertTrue((self.inbox / "demo.meta.json").exists(),
                         "a surfaced rollback failure must mean the metadata really was left behind")

    def test_a_torn_metadata_write_is_rejected_by_readback_verification(self) -> None:
        # sol round 4 minor: "removing metadata readback verification would not fail any test" --
        # -TestHookAfterMetaTmpWritten corrupts the metadata temp file after it is written but
        # before the write-back comparison, proving that comparison actually rejects a torn write
        # rather than merely existing, untested, in the source.
        hook = "{ param($p) [IO.File]::AppendAllText($p, 'TORN') }"
        proc = self.drop(OBSERVING, side=[], job_timeout_sec=3600, after_meta_tmp_written=hook)
        self.assertIn("THREW UMRUN_JOB_METADATA_VERIFY_FAILED", proc.stdout, proc.stdout + proc.stderr)
        self.assertEqual(self.names(), [], "a torn metadata write must leave neither metadata nor a job")

    def test_metadata_that_appears_concurrently_is_not_overwritten(self) -> None:
        (self.inbox / "demo.meta.json").write_text('{"jobId":"demo","timeoutSec":42}', encoding="ascii")
        proc = self.drop(OBSERVING, side=[], job_timeout_sec=3600)
        self.assertIn("THREW UMRUN_JOBID_IN_USE", proc.stdout, proc.stdout + proc.stderr)
        self.assertEqual(json.loads((self.inbox / "demo.meta.json").read_text(encoding="ascii"))["timeoutSec"], 42)
        self.assertEqual(self.names(), ["demo.meta.json"], "no job may be dropped after a metadata conflict")

    def test_an_existing_claim_of_any_age_is_never_reclaimed(self) -> None:
        # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 11 (sol BLOCKER + fable MAJOR): age-based
        # reclamation is gone entirely -- see UmRunDrop.psm1's own header. A claim that is hours
        # old (a "paused owner", in the brief's own words) must be refused exactly like a
        # brand-new one; nothing in this module ever measures or compares its age any more. The
        # concurrency property the round-11 brief asks for: a paused owner is never displaced.
        (self.inbox / "demo.meta.json").write_text('{"jobId":"demo","nonce":"old-owner"}', encoding="ascii")
        old = time.time() - 10_000   # ~2.8 hours in the past -- far past every former grace default
        os.utime(self.inbox / "demo.meta.json", (old, old))
        proc = self.drop(OBSERVING, side=[], job_timeout_sec=3600)
        combined = proc.stdout + proc.stderr
        self.assertIn("THREW UMRUN_JOBID_IN_USE", combined, combined)
        self.assertIn("choose a new -JobId", combined, combined)
        self.assertEqual(self.names(), ["demo.meta.json"])
        self.assertEqual(json.loads((self.inbox / "demo.meta.json").read_text(encoding="ascii"))["nonce"], "old-owner",
                         "an existing claim, of any age, must never be reclaimed or altered")

    def test_rollback_never_deletes_a_claim_it_does_not_own_by_nonce(self) -> None:
        # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 11 (sol BLOCKER + fable MAJOR, round 10's own
        # disclosed residual): simulates an operator manually clearing this submission's own stuck
        # claim and resubmitting the same JobId -- a DIFFERENT nonce's metadata is sitting at
        # demo.meta.json by the time this call's own post-claim work fails (a racing job.ps1 makes
        # the final rename lose, exactly as in the plain rollback test above). The rollback must
        # read the nonce back and refuse to delete what it does not own, leaving the new owner's
        # claim completely untouched.
        hook = (
            "{ [IO.File]::WriteAllText(" + _q(self.inbox / 'demo.meta.json') +
            ", '{\"jobId\":\"demo\",\"nonce\":\"someone-elses-nonce\"}'); "
            "[IO.File]::WriteAllText(" + _q(self.inbox / 'demo.job.ps1') + ", 'Write-Output concurrent') }"
        )
        proc = self.drop(OBSERVING, side=[], job_timeout_sec=3600, before_job_visible=hook)
        combined = proc.stdout + proc.stderr
        self.assertIn("THREW UMRUN_JOBID_IN_USE", combined, combined)
        self.assertIn("no longer this submission's own claim", combined, combined)
        meta = json.loads((self.inbox / "demo.meta.json").read_text(encoding="ascii"))
        self.assertEqual(meta["nonce"], "someone-elses-nonce",
                         "rollback must never delete a claim it does not own")
        self.assertEqual((self.inbox / "demo.job.ps1").read_text(encoding="utf-8"), "Write-Output concurrent")

    def test_bytes_altered_on_the_share_are_refused_and_nothing_is_placed(self) -> None:
        proc = self.drop(CORRUPTING)
        self.assertIn("THREW UMRUN_SIDEFILE_VERIFY_FAILED", proc.stdout, proc.stdout + proc.stderr)
        self.assertEqual(self.names(), [], "neither the side-file, its temporary, nor the job may remain")

    def test_a_side_file_that_appears_concurrently_with_other_bytes_is_not_overwritten(self) -> None:
        proc = self.drop(RACING_SIDE)
        self.assertIn("THREW UMRUN_SIDEFILE_CONFLICT", proc.stdout, proc.stdout + proc.stderr)
        self.assertEqual((self.inbox / "demo-source.zip").read_text(encoding="utf-8"), "another submitter")
        self.assertEqual(self.names(), ["demo-source.zip"], "no job may be dropped after a conflict")

    def test_a_job_that_appears_concurrently_is_not_replaced(self) -> None:
        proc = self.drop(RACING_JOB, side=[])
        self.assertIn("THREW UMRUN_JOBID_IN_USE", proc.stdout, proc.stdout + proc.stderr)
        self.assertEqual((self.inbox / "demo.job.ps1").read_text(encoding="utf-8"), "Write-Output concurrent")
        self.assertEqual(self.names(), ["demo.job.ps1"])

    def test_a_trailing_dot_alias_of_a_job_file_is_refused(self) -> None:
        evil = self.local / "evil.job.ps1"
        evil.write_text("throw 'unintended'\n", encoding="utf-8")
        proc = self.drop(OBSERVING, side=[Path(str(evil) + ".")])
        self.assertIn("THREW UMRUN_SIDEFILE_NAME_INVALID", proc.stdout, proc.stdout + proc.stderr)
        self.assertEqual(self.names(), [])

    # ---- tracked fixture admission (ATTR3-FIXTURE-REHEARSAL-1) -------------------------------
    # These use the REPOSITORY's own fixture clips, discovered by listing the directory rather than
    # by naming a file, and never assert on a media extension. Admission is anchored to this repo:
    # the source's real directory must BE <repo>/tests/fixtures/clips and the file must be tracked.

    # The admissible stems, mirrored from UmRunDrop.psm1. sol PR #137 r2 BLOCKER: the first version
    # of this helper took the SMALLEST tracked file in the directory, which is its 132-byte
    # README.md -- so the "fixture is admitted" test proved the bypass instead of the feature.
    FIXTURE_STEMS = ("tiny_dual_iso", "large_dual_iso")

    def repo_fixture(self) -> Path:
        fixtures = ROOT / "tests" / "fixtures" / "clips"
        if not fixtures.is_dir():
            self.skipTest("no repository fixture clips directory")
        clips = [f for f in fixtures.iterdir() if f.is_file() and f.stem in self.FIXTURE_STEMS]
        if not clips:
            self.skipTest("no repository fixture clips")
        return sorted(clips, key=lambda f: f.stat().st_size)[0]

    def repo_non_clip(self) -> Path:
        fixtures = ROOT / "tests" / "fixtures" / "clips"
        others = [f for f in fixtures.iterdir() if f.is_file() and f.stem not in self.FIXTURE_STEMS]
        if not others:
            self.skipTest("no tracked non-clip file in the fixtures directory")
        return others[0]

    def probe_source(self, path: Path) -> str:
        script = self.tmp / f"fixture-probe-{abs(hash(str(path))) % 10**8}.ps1"
        script.write_text(
            "Import-Module " + _q(MODULE) + " -Force\n"
            "Write-Output ('RESULT=' + (Test-UmRunTrackedFixtureSource -SourcePath " + _q(path) + "))\n",
            encoding="utf-8",
        )
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script)],
                              capture_output=True, text=True)
        return proc.stdout + proc.stderr

    @requires_git
    def test_a_tracked_repository_fixture_is_admitted_and_placed(self) -> None:
        clip = self.repo_fixture()
        proc = self.drop(OBSERVING, side=[clip])
        self.assertIn("UMRUN_JOBID=demo", proc.stdout, proc.stdout + proc.stderr)
        self.assertEqual(self.names(), sorted(["demo.job.ps1", "demo.meta.json", clip.name]))
        self.assertEqual(
            hashlib.sha256((self.inbox / clip.name).read_bytes()).hexdigest(),
            hashlib.sha256(clip.read_bytes()).hexdigest(),
        )

    @requires_git
    def test_the_same_bytes_and_name_from_another_directory_are_refused(self) -> None:
        clip = self.repo_fixture()
        elsewhere = self.local / "downloads"
        elsewhere.mkdir()
        impostor = elsewhere / clip.name
        shutil.copy2(clip, impostor)
        proc = self.drop(OBSERVING, side=[impostor])
        self.assertIn("THREW UMRUN_SIDEFILE_NAME_INVALID", proc.stdout, proc.stdout + proc.stderr)
        self.assertEqual(self.names(), [])

    @requires_git
    def test_a_lookalike_fixtures_tree_outside_the_repository_is_refused(self) -> None:
        # sol PR #137 r1 BLOCKER: a lexical segment match admitted any tests\fixtures\clips tree.
        clip = self.repo_fixture()
        lookalike = self.local / "tests" / "fixtures" / "clips"
        lookalike.mkdir(parents=True)
        impostor = lookalike / clip.name
        shutil.copy2(clip, impostor)
        self.assertIn("RESULT=False", self.probe_source(impostor))
        proc = self.drop(OBSERVING, side=[impostor])
        self.assertIn("THREW UMRUN_SIDEFILE_NAME_INVALID", proc.stdout, proc.stdout + proc.stderr)
        self.assertEqual(self.names(), [])

    @requires_git
    def test_a_path_through_a_junction_to_a_lookalike_tree_is_refused(self) -> None:
        clip = self.repo_fixture()
        lookalike = self.local / "outside" / "tests" / "fixtures" / "clips"
        lookalike.mkdir(parents=True)
        shutil.copy2(clip, lookalike / clip.name)
        link = self.local / "link"
        made = subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
             f"New-Item -ItemType Junction -Path {_q(link)} -Target {_q(self.local / 'outside')} | Out-Null"],
            capture_output=True, text=True)
        if made.returncode != 0 or not link.exists():
            self.skipTest(f"cannot create a junction here: {made.stderr}")
        through = link / "tests" / "fixtures" / "clips" / clip.name
        self.assertIn("RESULT=False", self.probe_source(through))

    @requires_git
    def test_a_tracked_non_clip_file_in_the_fixtures_directory_is_refused(self) -> None:
        # sol PR #137 r2 BLOCKER: "tracked under tests/fixtures/clips" admitted that directory's
        # README, whose extension the allowlist would otherwise refuse.
        other = self.repo_non_clip()
        self.assertIn("RESULT=False", self.probe_source(other))
        proc = self.drop(OBSERVING, side=[other])
        self.assertIn("THREW UMRUN_SIDEFILE_NAME_INVALID", proc.stdout, proc.stdout + proc.stderr)
        self.assertEqual(self.names(), [])

    @requires_git
    def test_an_untracked_file_inside_the_real_fixtures_directory_is_refused(self) -> None:
        # A file merely dropped into the repository's fixtures directory is not a fixture.
        fixtures = ROOT / "tests" / "fixtures" / "clips"
        intruder = fixtures / "tiny_dual_iso.umrunprobe"  # a fixture STEM, deliberately: this must fail the TRACKED test
        intruder.write_bytes(os.urandom(64))
        self.addCleanup(lambda: intruder.exists() and intruder.unlink())
        self.assertIn("RESULT=False", self.probe_source(intruder))

    def test_names_that_are_not_plain_allowlisted_basenames_are_refused(self) -> None:
        cases = ["x.job.ps1", "x.job.tmp", "x.ps1", "x.sidepart", "x.zip.", "x.zip ", "CON.zip", "noext", "x..zip", "x.exe.cmd"]
        body = "Import-Module " + _q(MODULE) + " -Force\n"
        for name in cases:
            body += ("try { [void](Assert-UmRunSideFileName -Name " + _q(name) + " -Inbox " + _q(self.inbox) + "); "
                     "Write-Output ('ACCEPTED ' + " + _q(name) + ") } catch { Write-Output ('REFUSED ' + " + _q(name) + ") }\n")
        script = self.tmp / "names.ps1"
        script.write_text(body, encoding="utf-8")
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script)], capture_output=True, text=True)
        for name in cases:
            with self.subTest(name=name):
                self.assertIn(f"REFUSED {name}", proc.stdout, proc.stdout + proc.stderr)
        for good in ["playback-attr-3-cuda-dllpair-4b20b66f7401-source.zip", "playback-attr-3-cuda-4b20b66f7401-build.json"]:
            proc2 = subprocess.run(
                [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
                 f"Import-Module {_q(MODULE)} -Force; [void](Assert-UmRunSideFileName -Name {_q(good)} -Inbox {_q(self.inbox)}); 'OK'"],
                capture_output=True, text=True)
            self.assertIn("OK", proc2.stdout, proc2.stdout + proc2.stderr)

    def test_an_existing_result_for_the_job_id_is_refused(self) -> None:
        (self.outbox / "demo.result.json").write_text("{}", encoding="utf-8")
        proc = self.drop(OBSERVING)
        self.assertIn("THREW UMRUN_JOBID_IN_USE", proc.stdout, proc.stdout + proc.stderr)
        self.assertEqual(self.names(), [])


class UmRunAgentLivenessTests(_Share):
    """Get-UmRunAgentLiveness, driven directly (round 11 moved it into UmRunDrop.psm1 from
    um-run.ps1's own local definition precisely so it could be tested this way -- fast, isolated,
    and able to reuse the module's existing share-clock test hooks instead of a slow, timing-
    dependent E2E subprocess race)."""

    def call_liveness(self, *, job_id: str = "demo", max_heartbeat_age_sec: int = 30,
                       after_first_read_hook: str | None = None) -> dict:
        script = self.tmp / "liveness.ps1"
        preamble = (
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module {_q(MODULE)} -Force\n"
        )
        hook_arg = ""
        if after_first_read_hook is not None:
            preamble += f"$afterFirstRead = {after_first_read_hook}\n"
            hook_arg = " -TestHookAfterFirstHeartbeatRead $afterFirstRead"
        script.write_text(
            preamble +
            f"$r = Get-UmRunAgentLiveness -HeartbeatPath {_q(self.share / 'heartbeat.txt')} "
            f"-Inbox {_q(self.inbox)} -JobId '{job_id}' -MaxHeartbeatAgeSec {max_heartbeat_age_sec}{hook_arg}\n"
            "$r | ConvertTo-Json -Compress\n",
            encoding="utf-8",
        )
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script)],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def test_a_stable_different_job_tag_is_a_real_mismatch(self) -> None:
        self.touch_heartbeat(job_id="someone-else")
        result = self.call_liveness(job_id="demo")
        self.assertTrue(result["JobMismatch"], result)
        self.assertEqual(result["OtherJobId"], "someone-else")

    def test_a_torn_read_that_self_corrects_between_the_two_reads_is_not_a_false_mismatch(self) -> None:
        # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 11 (sol MAJOR): a torn read of a real, complete
        # "job=<id>" tag can look like a complete tag for a SHORTER, different id on a single read
        # (e.g. "job=dem" read mid-write of "job=demo"). The second read, a short delay later, is
        # what actually decides a mismatch -- -TestHookAfterFirstHeartbeatRead fires right after
        # the first read, before that delay, letting this test rewrite heartbeat.txt to a
        # genuinely different, COMPLETE, real tag in between: the two reads disagree, so no
        # mismatch is reported, proving the first read alone is never trusted.
        self.touch_heartbeat(job_id="dem")   # looks like a complete, different job id on its own
        hook = (
            "{ param($t) Start-Sleep -Milliseconds 5; "
            f"$now = (Get-Date).ToString('o'); "
            f"Set-Content -LiteralPath {_q(self.share / 'heartbeat.txt')} "
            "-Value \"alive $now pid=1 host=TESTHOST job=demo\" -Encoding ascii }"
        )
        result = self.call_liveness(job_id="demo", after_first_read_hook=hook)
        self.assertFalse(result["JobMismatch"], result)

    def test_the_liveness_age_check_reads_the_probe_files_own_share_stamped_clock_not_the_clients(self) -> None:
        # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 11 (sol minor, item 5a): "no test fails if
        # Get-UmRunAgentLiveness's own share-clock probe reverts to the client's own Get-Date" --
        # true before this test, since submitter and share sit on ONE clock in this suite. Stamping
        # the probe file with a timestamp decades in the future makes the two behaviours diverge on
        # the actual Fresh/stale OUTCOME, not just on a probed value nobody asserts against: a
        # genuine probe read computes an enormous age (stale); a Get-Date substitution computes an
        # age near zero (fresh), for the exact same real heartbeat file.
        self.touch_heartbeat(job_id="demo")
        sentinel = "2099-01-01T00:00:00Z"
        stamp_hook = (
            "{ param($p) "
            f"[IO.File]::SetLastWriteTimeUtc($p, [datetime]::Parse('{sentinel}').ToUniversalTime()) "
            "}"
        )
        script = self.tmp / "liveness-clock.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module {_q(MODULE)} -Force\n"
            f"$stampHook = {stamp_hook}\n"
            f"$r = Get-UmRunAgentLiveness -HeartbeatPath {_q(self.share / 'heartbeat.txt')} "
            f"-Inbox {_q(self.inbox)} -JobId 'demo' -MaxHeartbeatAgeSec 30 "
            "-TestHookAfterProbeWritten $stampHook\n"
            "$r | ConvertTo-Json -Compress\n",
            encoding="utf-8",
        )
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script)],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads(proc.stdout)
        self.assertFalse(result["Fresh"], result)
        self.assertGreater(result["AgeSec"], 1_000_000_000,
                           "the age must come from the share-stamped probe clock, not the client's own")


class UmRunEndToEndTests(_Share):
    def submit(self, *extra: str, timeout_sec: str = "1", max_queue_wait_sec: str = "5") -> subprocess.CompletedProcess:
        # max_queue_wait_sec is small here on purpose: these tests use a FAKE share with no agent,
        # so the job is NEVER claimed, and the production default (86400s -- see um-run.ps1's own
        # comment on -MaxQueueWaitSec) would make every one of them hang. A real caller that expects
        # queue contention passes a larger value explicitly; a caller that does not gets a fast,
        # honestly-worded "never claimed" failure instead of a false "agent is down" diagnosis.
        return subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(UM_RUN),
             "-ScriptPath", str(self.job), "-AgentShare", str(self.share),
             "-TimeoutSec", timeout_sec, "-PollSeconds", "1", "-MaxQueueWaitSec", max_queue_wait_sec, *extra],
            capture_output=True, text=True,
        )

    def test_side_file_and_job_land_with_identical_bytes(self) -> None:
        proc = self.submit("-SideFile", str(self.side), "-JobId", "demo")
        combined = proc.stdout + proc.stderr
        self.assertIn("side-file placed: demo-source.zip", proc.stdout, combined)
        # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 11: against this fake, agent-less share the
        # queue-wait ceiling is reached with no marker ever appearing, so this client RETRACTS the
        # job -- the side-file placed earlier is untouched, but the job and its claim metadata are
        # withdrawn, never left to outlive this client (the original SUBMIT-RETRY-1 failure shape).
        self.assertIn("RETRACTED:", combined, combined)
        self.assertEqual(self.names(), ["demo-source.zip"])
        self.assertEqual(hashlib.sha256((self.inbox / "demo-source.zip").read_bytes()).hexdigest(),
                         hashlib.sha256(self.side.read_bytes()).hexdigest())

    def test_a_timeout_of_zero_is_rejected_by_the_public_client(self) -> None:
        # sol major 3: 0 is a valid MODULE-level sentinel ("write no metadata"), but the public
        # client forwarded it unchanged into a grace formula that gave the CALLER only 5s of
        # patience while a compatible agent quietly fell back to its own (possibly 1800s+) default.
        # The public contract closes that collision by refusing 0 outright, before anything is
        # submitted.
        proc = self.submit(timeout_sec="0")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("UMRUN_TIMEOUT_SEC_INVALID", proc.stdout + proc.stderr)
        self.assertEqual(self.names(), [], "a rejected -TimeoutSec must submit nothing at all")

    def test_the_callers_timeout_reaches_the_agent_as_the_jobs_own_budget(self) -> None:
        # The bug this closes: -TimeoutSec bounded only the client poll, so the agent ran every job
        # on its 1800 s default and killed a placement the caller had given an hour.
        # A real hour-long budget now makes the CLIENT wait an hour too (that is the point of the
        # change), so this asserts on the artifact and kills the poll rather than sitting out the
        # deadline: the metadata is written during the drop, long before any result could appear.
        proc = subprocess.Popen(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(UM_RUN),
             "-ScriptPath", str(self.job), "-AgentShare", str(self.share),
             "-TimeoutSec", "3600", "-PollSeconds", "1", "-JobId", "demo"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            meta_path = self.inbox / "demo.meta.json"
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and not meta_path.exists():
                time.sleep(0.2)
            self.assertTrue(meta_path.exists(), "the drop must write the agent's job budget")
            meta = json.loads(meta_path.read_text(encoding="ascii"))
            self.assertEqual(meta["timeoutSec"], 3600)
            self.assertEqual(meta["jobId"], "demo")
        finally:
            proc.kill()
            proc.communicate()

    def test_retracted_message_discloses_the_agents_own_stale_enumeration_window(self) -> None:
        # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 12 (sol BLOCKER / fable MINOR, item 1): RETRACTED
        # used to assert "nothing was submitted from the agent's point of view" -- true for
        # EXECUTION (both the tracked and deployed agents write their own claim marker strictly
        # before a child ever opens the job file by path, so a successful rename means the job's
        # own content can never run), but not for what the agent can still RECORD: it lists its
        # inbox once per poll and processes that whole snapshot sequentially (verified directly
        # against both agents), so a claim marker -- and an honest launch-failure receipt -- for
        # this id can still land minutes later, from a snapshot taken before this rename. The
        # message must disclose that rather than assert a stronger guarantee than this client can
        # actually prove.
        proc = self.submit("-JobId", "demo", max_queue_wait_sec="0")
        combined = " ".join(_ANSI_ESCAPE.sub("", proc.stdout + proc.stderr).replace("|", " ").split())
        self.assertIn("RETRACTED:", combined, combined)
        self.assertIn("per-poll inbox listing can be stale", combined, combined)
        self.assertIn("launch-failure receipt", combined, combined)
        self.assertIn("can never execute now", combined, combined)

    def test_an_unclaimed_job_is_retracted_never_a_down_diagnosis(self) -> None:
        # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 2 (fable/sol major 3): the OLD message
        # ("the job never ran or the agent is down") is an affirmative diagnosis the client has not
        # earned -- it cannot distinguish "dead agent" from "queued behind other work". Round 11
        # (sol blocker, item 2): against this fake, agent-less share the job is genuinely never
        # claimed, and the queue-wait ceiling now withdraws it from the inbox -- RETRACTED, not a
        # diagnosis about the agent's health at all.
        proc = self.submit("-JobId", "demo", max_queue_wait_sec="2")
        combined = proc.stdout + proc.stderr
        self.assertIn("RETRACTED:", combined, combined)
        self.assertNotIn("the job never ran or the agent is down", combined, combined)
        self.assertEqual(self.names(), [], "a genuinely retracted job must leave nothing behind")

    def test_a_late_claim_extends_the_wait_past_the_original_queue_ceiling_and_past_budget(self) -> None:
        # fable/sol major 3: the agent's deadline starts at CLAIM, not submission, and jobs are
        # processed sequentially, so a job stuck behind another can be claimed well after this
        # client's naive submit-time deadline would have expired. Simulates a compatible agent's
        # claim marker (running\<id>.started.json) appearing late, past the old queue ceiling.
        #
        # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 10 (liveness test 1/3 -- "fresh -> keeps
        # waiting past budget and returns the late receipt"): -TimeoutSec is 1s, so this run is
        # past its own budget within a second or two of being claimed -- proving the client is
        # STILL alive well after that, for as long as this test keeps heartbeat.txt looking like a
        # real agent's (refreshed, tagged with this job's id), is round 10's whole point: patience
        # no longer comes from a budget+grace constant, it comes from proof of liveness.
        running_dir = self.share / "running"
        proc = subprocess.Popen(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(UM_RUN),
             "-ScriptPath", str(self.job), "-AgentShare", str(self.share),
             "-TimeoutSec", "1", "-PollSeconds", "1", "-MaxQueueWaitSec", "3", "-JobId", "demo"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            time.sleep(1.5)   # inside the 3s queue-wait ceiling
            running_dir.mkdir(parents=True, exist_ok=True)
            marker = {"jobId": "demo", "startedUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            (running_dir / "demo.started.json").write_text(json.dumps(marker), encoding="ascii")
            # Keep the heartbeat fresh and tagged with this job's id for ~4s -- comfortably past
            # both the old 3s queue ceiling AND the 1s job budget -- the whole time this client
            # must still be alive, waiting on proof of liveness rather than a fixed cutoff.
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline:
                self.touch_heartbeat(job_id="demo")
                time.sleep(0.4)
            self.assertIsNone(proc.poll(),
                              "a fresh, job-matching heartbeat must keep the client waiting past its own budget")
            result = {"jobId": "demo", "exitCode": 0, "stdout": "late but real", "stderr": "",
                      "timeoutSec": 1, "timedOut": False}
            tmp = self.outbox / "demo.result.tmp"
            tmp.write_text(json.dumps(result), encoding="ascii")
            tmp.replace(self.outbox / "demo.result.json")
            stdout, stderr = proc.communicate(timeout=15)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        # Whitespace-flattened, with "|" removed: PowerShell's own default uncaught-exception
        # formatting word-wraps a long message at a fixed console width, prefixing each
        # continuation line with a literal "| " margin -- both would otherwise split a long
        # asserted phrase across a line break, or embed a stray "|" mid-phrase, by coincidence of
        # length rather than content. None of this file's own asserted text ever contains "|".
        combined = " ".join(_ANSI_ESCAPE.sub("", (stdout or "") + (stderr or "")).replace("|", " ").split())
        self.assertEqual(proc.returncode, 0, combined)
        self.assertNotIn("Timed out", combined, combined)

    def test_a_clock_skewed_claim_marker_does_not_shrink_the_clients_patience(self) -> None:
        # sol/fable blocker (round 7): claimedAt used to come from the marker's own startedUtc field
        # -- a timestamp stamped by the AGENT HOST's clock -- while the deadline built from it is
        # compared against THIS CLIENT's Get-Date. Simulates a badly-skewed (or merely very slow to
        # publish) agent host: the marker's declared startedUtc is an hour in the past relative to
        # real time, even though the client is only NOW observing it. Under a marker-anchored
        # deadline, that deadline would already be far in the past the instant this marker is
        # observed, so the client would throw (or, under round 10, misjudge liveness) within the
        # very next poll. The fix anchors purely to the client's OWN observation, so it must still
        # be alive well past that point.
        #
        # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 10 (liveness test 2/3 -- "stale -> stops with
        # the liveness message"): -MaxHeartbeatAgeSec is set to 2s (explicit and small, so the
        # natural stale-liveness throw is fast and intentional, not an accidental ~30s coincidence
        # with the default) and heartbeat.txt is never refreshed after setUp, so it goes stale
        # almost immediately once the client starts actually checking it (past budget).
        running_dir = self.share / "running"
        marker_path = running_dir / "demo.started.json"
        proc = subprocess.Popen(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(UM_RUN),
             "-ScriptPath", str(self.job), "-AgentShare", str(self.share),
             "-TimeoutSec", "2", "-PollSeconds", "1", "-MaxQueueWaitSec", "10",
             "-MaxHeartbeatAgeSec", "2", "-JobId", "demo"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            time.sleep(1.0)
            running_dir.mkdir(parents=True, exist_ok=True)
            skewed = time.gmtime(time.time() - 3600)
            marker = {"jobId": "demo", "startedUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", skewed)}
            marker_path.write_text(json.dumps(marker), encoding="ascii")
            # One more poll cycle: under a marker-anchored deadline the client would already have
            # thrown (or misjudged liveness) on the very first check after observing this marker.
            time.sleep(1.0)
            self.assertIsNone(proc.poll(), "a stale agent-clock stamp must not shrink the client's own patience")
            # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 11 (item 3): the started marker's own
            # presence is now an independent proof the agent may still own this job (the deployed
            # agent's silent kill/drain/publish window) -- removing it here simulates that window
            # having already ended (Complete-StartedMarker already ran, strictly after some receipt
            # was published) so the stale-heartbeat diagnosis below can actually fire, the same way
            # a real agent's own marker removal would let it fire in production.
            marker_path.unlink()
            stdout, stderr = proc.communicate(timeout=15)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        # Whitespace-flattened, with "|" removed: PowerShell's own default uncaught-exception
        # formatting word-wraps a long message at a fixed console width, prefixing each
        # continuation line with a literal "| " margin -- both would otherwise split a long
        # asserted phrase across a line break, or embed a stray "|" mid-phrase, by coincidence of
        # length rather than content. None of this file's own asserted text ever contains "|".
        combined = " ".join(_ANSI_ESCAPE.sub("", (stdout or "") + (stderr or "")).replace("|", " ").split())
        self.assertIn("UNRESOLVED:", combined, combined)
        self.assertIn("claimed by the agent", combined, combined)
        self.assertNotIn("RETRACTED", combined, combined)
        self.assertIn("stopped proving liveness", combined, combined)
        self.assertIn("MaxHeartbeatAgeSec 2", combined, combined)
        self.assertIn("its own started marker is gone too", combined, combined)
        # round 9 wording, unchanged in spirit under round 11's UNRESOLVED message: this is THIS
        # CLIENT's own patience running out, never a diagnosis the client cannot make.
        self.assertIn("the agent may still own demo", combined, combined)
        self.assertIn("its receipt may still land at", combined, combined)

    def test_the_outer_ceiling_stops_the_client_even_while_heartbeat_stays_fresh(self) -> None:
        # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 10 (liveness test 3/3 -- "outer ceiling ->
        # stops"): a stuck-but-heartbeating agent must not be trusted forever. -MaxClaimedWaitSec
        # is set small and explicit (2s) so this client gives up at claimedAt + budget +
        # -MaxClaimedWaitSec despite a heartbeat this test keeps continuously fresh and correctly
        # job-tagged throughout -- proving the ceiling fires on its OWN terms, never because
        # liveness was ever lost.
        running_dir = self.share / "running"
        proc = subprocess.Popen(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(UM_RUN),
             "-ScriptPath", str(self.job), "-AgentShare", str(self.share),
             "-TimeoutSec", "1", "-PollSeconds", "1", "-MaxQueueWaitSec", "5",
             "-MaxClaimedWaitSec", "2", "-JobId", "demo"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            time.sleep(1.0)
            running_dir.mkdir(parents=True, exist_ok=True)
            marker = {"jobId": "demo", "startedUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            (running_dir / "demo.started.json").write_text(json.dumps(marker), encoding="ascii")
            # Keep refreshing heartbeat.txt for the whole run -- well past claimedAt + 1s budget +
            # 2s outer ceiling (~3s from claim) -- so any throw here can only be the outer ceiling,
            # never a liveness-lost diagnosis.
            deadline = time.monotonic() + 6.0
            while time.monotonic() < deadline and proc.poll() is None:
                self.touch_heartbeat(job_id="demo")
                time.sleep(0.4)
            stdout, stderr = proc.communicate(timeout=15)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        # Whitespace-flattened, with "|" removed: PowerShell's own default uncaught-exception
        # formatting word-wraps a long message at a fixed console width, prefixing each
        # continuation line with a literal "| " margin -- both would otherwise split a long
        # asserted phrase across a line break, or embed a stray "|" mid-phrase, by coincidence of
        # length rather than content. None of this file's own asserted text ever contains "|".
        combined = " ".join(_ANSI_ESCAPE.sub("", (stdout or "") + (stderr or "")).replace("|", " ").split())
        self.assertNotEqual(proc.returncode, 0, combined)
        self.assertIn("UNRESOLVED:", combined, combined)
        self.assertIn("claimed by the agent", combined, combined)
        self.assertIn("absolute outer ceiling", combined, combined)
        self.assertIn("MaxClaimedWaitSec (2s)", combined, combined)
        self.assertNotIn("stopped proving liveness", combined,
                         "a continuously fresh heartbeat must never be diagnosed as liveness-lost")
        self.assertIn("the agent may still own demo", combined, combined)

    def test_a_fresh_heartbeat_naming_a_different_job_is_treated_as_liveness_lost_for_this_one(self) -> None:
        # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 10: a heartbeat can be FRESH (age-wise) while
        # naming a job that is not ours -- proof the agent process itself is alive, but not proof
        # it is still working on OUR job. Age-freshness alone must not be enough once the heartbeat
        # explicitly names a different job: that is liveness-lost for THIS job specifically, and
        # must be diagnosed as such (not silently trusted, and not the generic "no heartbeat" or
        # plain staleness wording).
        running_dir = self.share / "running"
        marker_path = running_dir / "demo.started.json"
        proc = subprocess.Popen(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(UM_RUN),
             "-ScriptPath", str(self.job), "-AgentShare", str(self.share),
             "-TimeoutSec", "1", "-PollSeconds", "1", "-MaxQueueWaitSec", "5", "-JobId", "demo"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            time.sleep(1.0)
            running_dir.mkdir(parents=True, exist_ok=True)
            marker = {"jobId": "demo", "startedUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            marker_path.write_text(json.dumps(marker), encoding="ascii")
            # Fresh, but for a DIFFERENT job -- keep it refreshed so the mismatch is definitely
            # observed at least once.
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                self.touch_heartbeat(job_id="someone-elses-job")
                time.sleep(0.4)
            # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 11 (item 3): a job-mismatch alone is not
            # enough to end the wait any more -- the started marker must ALSO be gone (simulating
            # Complete-StartedMarker having already run for whatever job the agent is now on).
            marker_path.unlink()
            stdout, stderr = proc.communicate(timeout=15)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        # Whitespace-flattened, with "|" removed: PowerShell's own default uncaught-exception
        # formatting word-wraps a long message at a fixed console width, prefixing each
        # continuation line with a literal "| " margin -- both would otherwise split a long
        # asserted phrase across a line break, or embed a stray "|" mid-phrase, by coincidence of
        # length rather than content. None of this file's own asserted text ever contains "|".
        combined = " ".join(_ANSI_ESCAPE.sub("", (stdout or "") + (stderr or "")).replace("|", " ").split())
        self.assertNotEqual(proc.returncode, 0, combined)
        self.assertIn("UNRESOLVED:", combined, combined)
        self.assertIn("claimed by the agent", combined, combined)
        self.assertIn("DIFFERENT job (someone-elses-job)", combined, combined)
        self.assertIn("its own started marker is gone too", combined, combined)
        self.assertNotIn("stopped proving liveness", combined,
                         "a job-mismatch is its own diagnosis, distinct from plain staleness")
        self.assertIn("moved on without a receipt", combined, combined)

    def test_a_job_mismatched_heartbeat_with_the_started_marker_still_present_keeps_waiting(self) -> None:
        # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 11 (item 3, the core of this round's own
        # principle): the deployed agent writes NO heartbeat between its own deadline and
        # publishing a receipt (Stop-ProcessTree, stream drain, Publish-JobResult are all silent),
        # so a stale or mismatched heartbeat alone must not end the wait while running\<id>.
        # started.json -- proof the agent has not yet finished with this job either way, since
        # Complete-StartedMarker only ever runs strictly AFTER a receipt is durably published --
        # is still sitting there. This keeps the marker present THE WHOLE TIME and proves the
        # client survives well past what would otherwise be an immediate liveness-lost throw,
        # then returns the late receipt once it appears.
        running_dir = self.share / "running"
        proc = subprocess.Popen(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(UM_RUN),
             "-ScriptPath", str(self.job), "-AgentShare", str(self.share),
             "-TimeoutSec", "1", "-PollSeconds", "1", "-MaxQueueWaitSec", "5",
             "-MaxHeartbeatAgeSec", "1", "-JobId", "demo"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            time.sleep(1.0)
            running_dir.mkdir(parents=True, exist_ok=True)
            marker = {"jobId": "demo", "startedUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            (running_dir / "demo.started.json").write_text(json.dumps(marker), encoding="ascii")
            # Heartbeat is never refreshed again after this -- it goes stale almost immediately
            # (past -MaxHeartbeatAgeSec 1) -- and the marker is left in place throughout.
            time.sleep(3.0)
            self.assertIsNone(proc.poll(),
                              "a stale heartbeat must not end the wait while the started marker is "
                              "still present")
            result = {"jobId": "demo", "exitCode": 0, "stdout": "late but real", "stderr": "",
                      "timeoutSec": 1, "timedOut": False}
            tmp = self.outbox / "demo.result.tmp"
            tmp.write_text(json.dumps(result), encoding="ascii")
            tmp.replace(self.outbox / "demo.result.json")
            stdout, stderr = proc.communicate(timeout=15)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        # Whitespace-flattened, with "|" removed: PowerShell's own default uncaught-exception
        # formatting word-wraps a long message at a fixed console width, prefixing each
        # continuation line with a literal "| " margin -- both would otherwise split a long
        # asserted phrase across a line break, or embed a stray "|" mid-phrase, by coincidence of
        # length rather than content. None of this file's own asserted text ever contains "|".
        combined = " ".join(_ANSI_ESCAPE.sub("", (stdout or "") + (stderr or "")).replace("|", " ").split())
        self.assertEqual(proc.returncode, 0, combined)
        self.assertNotIn("UNRESOLVED", combined, combined)

    def test_a_claim_landing_exactly_at_the_queue_deadline_is_not_misreported_as_never_claimed(self) -> None:
        # sol blocker (round 7): the final recheck before throwing only ever re-read the RESULT file,
        # despite its own comment promising a receipt-OR-claim recheck -- a claim landing in the
        # instant between the loop's last (negative) marker check and the throw was still reported
        # as "never claimed". That window is normally sub-millisecond and cannot be hit reliably from
        # outside the process; -TestHookAtQueueDeadline (test-only, fires exactly there) closes it
        # deterministically. um-run.ps1 is invoked here via '&' from a wrapper script -- not '-File'
        # -- specifically so an actual scriptblock, not a CLI string, can be passed through.
        #
        # sol round 8 narrower-revert review: the round-7 version of this test asserted only on the
        # final diagnosis WORDING ("claimed by the agent", not "was never claimed"). A revert that
        # drops just the loop's `continue` after finding the late marker -- leaving the hook, the
        # marker recheck, and claimedAt/execDeadline all intact -- still sets claimedAt before an
        # unconditional `break`, so the post-loop code still lands in the "claimed by the agent"
        # branch and every round-7 assertion still passed, even though the loop never actually got
        # the one extra poll `continue` exists to grant. This version writes a REAL receipt shortly
        # after the hook observes the claim -- late enough that only a loop which keeps polling after
        # `continue` can ever read it. Without `continue`, the loop breaks immediately, the one
        # post-loop recheck runs before the receipt exists, and the run throws instead of returning.
        running_dir = self.share / "running"
        wrapper = self.tmp / "run-with-hook.ps1"
        hook = (
            "{ "
            f"New-Item -ItemType Directory -Force -Path {_q(running_dir)} | Out-Null; "
            "$marker = @{ jobId = 'demo'; startedUtc = (Get-Date).ToUniversalTime().ToString('o') } "
            "| ConvertTo-Json -Compress; "
            f"Set-Content -LiteralPath {_q(running_dir / 'demo.started.json')} -Value $marker "
            "-Encoding ascii -NoNewline "
            "}"
        )
        wrapper.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"$hook = {hook}\n"
            "try {\n"
            f"  $r = & {_q(UM_RUN)} -ScriptPath {_q(self.job)} -AgentShare {_q(self.share)} "
            "-TimeoutSec 2 -PollSeconds 1 -MaxQueueWaitSec 0 -JobId 'demo' "
            "-TestHookAtQueueDeadline $hook\n"
            "  Write-Output ('E2E_RESULT=OK EXIT=' + $r.exitCode)\n"
            "} catch { Write-Output ('THREW ' + $_.Exception.Message) }\n",
            encoding="utf-8",
        )
        proc = subprocess.Popen([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(wrapper)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            marker_path = running_dir / "demo.started.json"
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not marker_path.exists():
                time.sleep(0.1)
            self.assertTrue(marker_path.exists(), "the hook never wrote the claim marker")
            # Written only after the claim is already on disk -- readable only by a loop iteration
            # that runs AFTER `continue` sends it back to the top, never by the single recheck that
            # follows an immediate `break`.
            time.sleep(2.0)
            result = {"jobId": "demo", "exitCode": 0, "stdout": "late but real", "stderr": "",
                      "timeoutSec": 2, "timedOut": False}
            tmp = self.outbox / "demo.result.tmp"
            tmp.write_text(json.dumps(result), encoding="ascii")
            tmp.replace(self.outbox / "demo.result.json")
            stdout, stderr = proc.communicate(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        # Whitespace-flattened, with "|" removed: PowerShell's own default uncaught-exception
        # formatting word-wraps a long message at a fixed console width, prefixing each
        # continuation line with a literal "| " margin -- both would otherwise split a long
        # asserted phrase across a line break, or embed a stray "|" mid-phrase, by coincidence of
        # length rather than content. None of this file's own asserted text ever contains "|".
        combined = " ".join(_ANSI_ESCAPE.sub("", (stdout or "") + (stderr or "")).replace("|", " ").split())
        self.assertNotIn("was never claimed", combined, combined)
        self.assertNotIn("THREW", combined, combined)
        self.assertIn("E2E_RESULT=OK EXIT=0", combined, combined)

    def test_a_clock_skewed_claim_marker_observed_at_the_queue_deadline_does_not_shrink_the_clients_patience(self) -> None:
        # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 9 (astra test-strength minor): the round-7 clock-
        # skew test above (test_a_clock_skewed_claim_marker_does_not_shrink_the_clients_patience)
        # only ever exercises the FIRST claimedAt assignment (the one at the top of the poll loop,
        # while claimedAt is still null and the marker is discovered on an ordinary iteration). It
        # never reaches the SECOND, separate `$claimedAt = Get-Date` at the queue-deadline recheck
        # (um-run.ps1, inside `if ($null -eq $claimedAt) { ... if (Test-Path $startedMarker) {
        # $claimedAt = Get-Date; ...; continue } }`), which only runs when the marker appears
        # exactly at/after the queue deadline -- reached here the same way
        # test_a_claim_landing_exactly_at_the_queue_deadline_is_not_misreported_as_never_claimed
        # above reaches it, via -TestHookAtQueueDeadline with -MaxQueueWaitSec 0, except this hook's
        # marker declares a startedUtc an hour in the past (same skew as round 7's test). If that
        # second assignment were ever changed to read the marker's own stale startedUtc instead of
        # the client's own Get-Date, execDeadline would already be ~3600s in the past the instant it
        # is computed, and the very next loop iteration would throw almost immediately -- long
        # before this client's real budget (2s + >= 20s grace) could possibly be exhausted.
        running_dir = self.share / "running"
        wrapper = self.tmp / "run-with-hook-skewed.ps1"
        hook = (
            "{ "
            f"New-Item -ItemType Directory -Force -Path {_q(running_dir)} | Out-Null; "
            "$marker = @{ jobId = 'demo'; startedUtc = (Get-Date).AddHours(-1).ToUniversalTime().ToString('o') } "
            "| ConvertTo-Json -Compress; "
            f"Set-Content -LiteralPath {_q(running_dir / 'demo.started.json')} -Value $marker "
            "-Encoding ascii -NoNewline "
            "}"
        )
        wrapper.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"$hook = {hook}\n"
            "try {\n"
            f"  $r = & {_q(UM_RUN)} -ScriptPath {_q(self.job)} -AgentShare {_q(self.share)} "
            "-TimeoutSec 3 -PollSeconds 1 -MaxQueueWaitSec 0 -JobId 'demo' "
            "-TestHookAtQueueDeadline $hook\n"
            "  Write-Output ('E2E_RESULT=OK EXIT=' + $r.exitCode)\n"
            "} catch { Write-Output ('THREW ' + $_.Exception.Message) }\n",
            encoding="utf-8",
        )
        proc = subprocess.Popen([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(wrapper)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            marker_path = running_dir / "demo.started.json"
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not marker_path.exists():
                time.sleep(0.1)
            self.assertTrue(marker_path.exists(), "the hook never wrote the skewed claim marker")
            # A client that (wrongly) anchored claimedAt to the marker's own hour-old startedUtc
            # would already be past its execDeadline on the very next poll -- well under 2s away.
            # Still alive at 2s proves this run's patience came from the client's OWN observation.
            time.sleep(2.0)
            self.assertIsNone(
                proc.poll(),
                "a stale agent-clock stamp observed at the queue deadline must not shrink the "
                "client's own patience",
            )
            result = {"jobId": "demo", "exitCode": 0, "stdout": "late but real", "stderr": "",
                      "timeoutSec": 3, "timedOut": False}
            tmp = self.outbox / "demo.result.tmp"
            tmp.write_text(json.dumps(result), encoding="ascii")
            tmp.replace(self.outbox / "demo.result.json")
            stdout, stderr = proc.communicate(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        # Whitespace-flattened, with "|" removed: PowerShell's own default uncaught-exception
        # formatting word-wraps a long message at a fixed console width, prefixing each
        # continuation line with a literal "| " margin -- both would otherwise split a long
        # asserted phrase across a line break, or embed a stray "|" mid-phrase, by coincidence of
        # length rather than content. None of this file's own asserted text ever contains "|".
        combined = " ".join(_ANSI_ESCAPE.sub("", (stdout or "") + (stderr or "")).replace("|", " ").split())
        self.assertNotIn("was never claimed", combined, combined)
        self.assertNotIn("THREW", combined, combined)
        self.assertIn("E2E_RESULT=OK EXIT=0", combined, combined)

    # ---- round 11 (sol BLOCKER, item 2): RETRACT at the queue deadline instead of leaving the job
    # ---- in the inbox for the agent to claim after this client has already given up ------------

    def test_a_retraction_race_where_the_agent_claims_first_is_honoured_not_retracted(self) -> None:
        # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 11 (item 2's own required race test): the agent
        # can claim the job in the exact instant between this client's rename attempt and its own
        # marker recheck -- -TestHookAfterRetractionRename (test-only, fires exactly there) closes
        # that window deterministically, the same way -TestHookAtQueueDeadline already does for the
        # marker-appears-BEFORE-the-rename race. um-run.ps1 is invoked via '&' from a wrapper
        # script so an actual scriptblock can be passed through.
        running_dir = self.share / "running"
        wrapper = self.tmp / "run-with-retraction-race-hook.ps1"
        hook = (
            "{ "
            f"New-Item -ItemType Directory -Force -Path {_q(running_dir)} | Out-Null; "
            "$marker = @{ jobId = 'demo'; startedUtc = (Get-Date).ToUniversalTime().ToString('o') } "
            "| ConvertTo-Json -Compress; "
            f"Set-Content -LiteralPath {_q(running_dir / 'demo.started.json')} -Value $marker "
            "-Encoding ascii -NoNewline "
            "}"
        )
        wrapper.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"$hook = {hook}\n"
            "try {\n"
            f"  $r = & {_q(UM_RUN)} -ScriptPath {_q(self.job)} -AgentShare {_q(self.share)} "
            "-TimeoutSec 2 -PollSeconds 1 -MaxQueueWaitSec 0 -JobId 'demo' "
            "-TestHookAfterRetractionRename $hook\n"
            "  Write-Output ('E2E_RESULT=OK EXIT=' + $r.exitCode)\n"
            "} catch { Write-Output ('THREW ' + $_.Exception.Message) }\n",
            encoding="utf-8",
        )
        proc = subprocess.Popen([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(wrapper)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            marker_path = running_dir / "demo.started.json"
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not marker_path.exists():
                time.sleep(0.1)
            self.assertTrue(marker_path.exists(), "the hook never wrote the claim marker")
            time.sleep(1.0)
            result = {"jobId": "demo", "exitCode": 0, "stdout": "late but real", "stderr": "",
                      "timeoutSec": 2, "timedOut": False}
            tmp = self.outbox / "demo.result.tmp"
            tmp.write_text(json.dumps(result), encoding="ascii")
            tmp.replace(self.outbox / "demo.result.json")
            stdout, stderr = proc.communicate(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        # Whitespace-flattened, with "|" removed: PowerShell's own default uncaught-exception
        # formatting word-wraps a long message at a fixed console width, prefixing each
        # continuation line with a literal "| " margin -- both would otherwise split a long
        # asserted phrase across a line break, or embed a stray "|" mid-phrase, by coincidence of
        # length rather than content. None of this file's own asserted text ever contains "|".
        combined = " ".join(_ANSI_ESCAPE.sub("", (stdout or "") + (stderr or "")).replace("|", " ").split())
        self.assertNotIn("RETRACTED", combined, combined)
        self.assertNotIn("THREW", combined, combined)
        self.assertIn("E2E_RESULT=OK EXIT=0", combined, combined)

    def test_a_failed_retraction_rename_falls_through_to_the_claimed_wait_not_retracted(self) -> None:
        # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 11 (item 2): if the retraction rename itself
        # fails for any reason, this client must never assert a retraction it cannot prove --
        # simulated here by removing the job file out from under the client's own rename attempt,
        # via -TestHookAtQueueDeadline (fires right before the rename is even attempted), so
        # Move-Item fails with the source already gone. No marker ever appears either, so this
        # must resolve as UNRESOLVED once its own (small) claimed-phase patience elapses, never
        # RETRACTED.
        wrapper = self.tmp / "run-with-vanished-job-hook.ps1"
        hook = "{ Remove-Item -LiteralPath " + _q(self.inbox / "demo.job.ps1") + " -Force -ErrorAction SilentlyContinue }"
        wrapper.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"$hook = {hook}\n"
            "try {\n"
            f"  $r = & {_q(UM_RUN)} -ScriptPath {_q(self.job)} -AgentShare {_q(self.share)} "
            "-TimeoutSec 1 -PollSeconds 1 -MaxQueueWaitSec 0 -MaxClaimedWaitSec 1 "
            "-MaxHeartbeatAgeSec 1 -JobId 'demo' -TestHookAtQueueDeadline $hook\n"
            "  Write-Output ('E2E_RESULT=OK EXIT=' + $r.exitCode)\n"
            "} catch { Write-Output ('THREW ' + $_.Exception.Message) }\n",
            encoding="utf-8",
        )
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(wrapper)],
                               capture_output=True, text=True, timeout=30)
        combined = proc.stdout + proc.stderr
        self.assertNotIn("RETRACTED", combined, combined)
        self.assertIn("UNRESOLVED:", combined, combined)

    def test_retraction_cleans_up_metadata_by_nonce_never_a_blind_delete(self) -> None:
        # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 11 (item 1's nonce-checked-delete invariant,
        # applied to the NEW retraction path too): plants a DIFFERENT submission's claim at
        # demo.meta.json (a different nonce) exactly when the retraction rename fires, simulating
        # an operator manually clearing this client's own claim and resubmitting the same JobId
        # while this client was still (usually harmlessly) waiting out its queue ceiling. The
        # retraction must still succeed (the job file itself is this client's own to withdraw
        # regardless), but must never delete metadata it does not own.
        hook = (
            "{ [IO.File]::WriteAllText(" + _q(self.inbox / 'demo.meta.json') +
            ", '{\"jobId\":\"demo\",\"nonce\":\"someone-elses-nonce\"}') }"
        )
        wrapper = self.tmp / "run-with-retraction-nonce-hook.ps1"
        wrapper.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"$hook = {hook}\n"
            "try {\n"
            f"  $r = & {_q(UM_RUN)} -ScriptPath {_q(self.job)} -AgentShare {_q(self.share)} "
            "-TimeoutSec 1 -PollSeconds 1 -MaxQueueWaitSec 0 -JobId 'demo' "
            "-TestHookAfterRetractionRename $hook\n"
            "  Write-Output ('E2E_RESULT=OK EXIT=' + $r.exitCode)\n"
            "} catch { Write-Output ('THREW ' + $_.Exception.Message) }\n",
            encoding="utf-8",
        )
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(wrapper)],
                               capture_output=True, text=True, timeout=30)
        combined = proc.stdout + proc.stderr
        self.assertIn("THREW RETRACTED:", combined, combined)
        meta = json.loads((self.inbox / "demo.meta.json").read_text(encoding="ascii"))
        self.assertEqual(meta["nonce"], "someone-elses-nonce",
                         "retraction must never delete metadata it does not own")

    def test_a_receipt_planted_before_the_outer_ceiling_throw_is_still_read(self) -> None:
        # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 11 (fable minor, item 5): the receipt recheck
        # immediately before the absolute-outer-ceiling throw is pinned here -- deleting those two
        # lines fails no OTHER test, since the outer-ceiling test above never plants a receipt.
        running_dir = self.share / "running"
        hook = (
            "{ "
            "$result = @{ jobId = 'demo'; exitCode = 0; stdout = 'late but real'; stderr = ''; "
            "timeoutSec = 1; timedOut = $false } | ConvertTo-Json -Compress; "
            f"$tmp = {_q(self.outbox / 'demo.result.tmp')}; "
            f"$fin = {_q(self.outbox / 'demo.result.json')}; "
            "Set-Content -LiteralPath $tmp -Value $result -Encoding ascii -NoNewline; "
            "Move-Item -Force -LiteralPath $tmp -Destination $fin "
            "}"
        )
        wrapper = self.tmp / "run-with-outer-ceiling-receipt-hook.ps1"
        wrapper.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"$hook = {hook}\n"
            "try {\n"
            f"  $r = & {_q(UM_RUN)} -ScriptPath {_q(self.job)} -AgentShare {_q(self.share)} "
            "-TimeoutSec 1 -PollSeconds 1 -MaxQueueWaitSec 5 -MaxClaimedWaitSec 1 -JobId 'demo' "
            "-TestHookBeforeOuterCeilingReceiptRecheck $hook\n"
            "  Write-Output ('E2E_RESULT=OK EXIT=' + $r.exitCode)\n"
            "} catch { Write-Output ('THREW ' + $_.Exception.Message) }\n",
            encoding="utf-8",
        )
        proc = subprocess.Popen([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(wrapper)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            running_dir.mkdir(parents=True, exist_ok=True)
            marker = {"jobId": "demo", "startedUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            (running_dir / "demo.started.json").write_text(json.dumps(marker), encoding="ascii")
            # Keep the heartbeat fresh and job-tagged throughout, so the ONLY way this run can end
            # is the outer ceiling (never a liveness-lost diagnosis) -- the hook above plants the
            # receipt in the exact instant right before that throw would otherwise fire.
            deadline = time.monotonic() + 6.0
            while time.monotonic() < deadline and proc.poll() is None:
                self.touch_heartbeat(job_id="demo")
                time.sleep(0.3)
            stdout, stderr = proc.communicate(timeout=15)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        # Whitespace-flattened, with "|" removed: PowerShell's own default uncaught-exception
        # formatting word-wraps a long message at a fixed console width, prefixing each
        # continuation line with a literal "| " margin -- both would otherwise split a long
        # asserted phrase across a line break, or embed a stray "|" mid-phrase, by coincidence of
        # length rather than content. None of this file's own asserted text ever contains "|".
        combined = " ".join(_ANSI_ESCAPE.sub("", (stdout or "") + (stderr or "")).replace("|", " ").split())
        self.assertNotIn("THREW", combined, combined)
        self.assertIn("E2E_RESULT=OK EXIT=0", combined, combined)

    def test_a_receipt_planted_before_the_liveness_lost_throw_is_still_read(self) -> None:
        # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 11 (fable minor, item 5): the receipt recheck
        # immediately before the liveness-lost throw is pinned here. THIS TEST removes the started
        # marker itself (simulating Complete-StartedMarker's own ordering: the marker is only ever
        # removed strictly AFTER a receipt already exists) well before the hook fires, so the
        # client is already past budget with a stale heartbeat AND a gone marker by the time it
        # next polls -- landing in the liveness-lost branch, where the hook plants the receipt
        # right before the recheck that must still find it.
        running_dir = self.share / "running"
        marker_path = running_dir / "demo.started.json"
        hook = (
            "{ "
            "$result = @{ jobId = 'demo'; exitCode = 0; stdout = 'late but real'; stderr = ''; "
            "timeoutSec = 1; timedOut = $false } | ConvertTo-Json -Compress; "
            f"$tmp = {_q(self.outbox / 'demo.result.tmp')}; "
            f"$fin = {_q(self.outbox / 'demo.result.json')}; "
            "Set-Content -LiteralPath $tmp -Value $result -Encoding ascii -NoNewline; "
            "Move-Item -Force -LiteralPath $tmp -Destination $fin "
            "}"
        )
        wrapper = self.tmp / "run-with-liveness-lost-receipt-hook.ps1"
        wrapper.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"$hook = {hook}\n"
            "try {\n"
            f"  $r = & {_q(UM_RUN)} -ScriptPath {_q(self.job)} -AgentShare {_q(self.share)} "
            "-TimeoutSec 1 -PollSeconds 1 -MaxQueueWaitSec 5 -MaxHeartbeatAgeSec 1 -JobId 'demo' "
            "-TestHookBeforeLivenessLostReceiptRecheck $hook\n"
            "  Write-Output ('E2E_RESULT=OK EXIT=' + $r.exitCode)\n"
            "} catch { Write-Output ('THREW ' + $_.Exception.Message) }\n",
            encoding="utf-8",
        )
        proc = subprocess.Popen([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(wrapper)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            running_dir.mkdir(parents=True, exist_ok=True)
            marker = {"jobId": "demo", "startedUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            marker_path.write_text(json.dumps(marker), encoding="ascii")
            # heartbeat.txt is never refreshed after setUp -- it goes stale past -MaxHeartbeatAgeSec
            # 1 almost immediately once the client starts checking it (past its 1s budget). Give it
            # comfortably long enough to be both past budget and past staleness while the marker is
            # still present (the client just keeps waiting silently through that), then remove the
            # marker so the NEXT poll lands in the liveness-lost branch and fires the hook.
            time.sleep(2.5)
            marker_path.unlink()
            stdout, stderr = proc.communicate(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        # Whitespace-flattened, with "|" removed: PowerShell's own default uncaught-exception
        # formatting word-wraps a long message at a fixed console width, prefixing each
        # continuation line with a literal "| " margin -- both would otherwise split a long
        # asserted phrase across a line break, or embed a stray "|" mid-phrase, by coincidence of
        # length rather than content. None of this file's own asserted text ever contains "|".
        combined = " ".join(_ANSI_ESCAPE.sub("", (stdout or "") + (stderr or "")).replace("|", " ").split())
        self.assertNotIn("THREW", combined, combined)
        self.assertIn("E2E_RESULT=OK EXIT=0", combined, combined)

    # ---- round 12 (sol BLOCKER, item 3): a structural catch converts ANY unexpected error after ---
    # ---- the job was made visible into UNRESOLVED, never a raw escape or CLASS=UNKNOWN ------------

    def test_an_unexpected_error_during_the_wait_loop_resolves_to_unresolved_never_raw(self) -> None:
        # An unanticipated share I/O failure after the job was made visible (a heartbeat vanishing
        # between Test-Path and Get-Item, a torn share-clock probe, ...) used to propagate raw past
        # every outcome this file's own header promises -- a caller then misclassified it
        # CLASS=UNKNOWN and treated it as an ordinary, safely-retryable failure while the agent
        # might still own the job. -TestHookAtLoopTop injects a synthetic, unrelated exception on
        # the very first iteration, standing in for any such failure -- the structural catch around
        # the whole loop must convert it to UNRESOLVED and must never forward the wrapped message.
        hook = "{ throw [System.IO.IOException]::new('synthetic share hiccup') }"
        wrapper = self.tmp / "run-with-loop-top-throw-hook.ps1"
        wrapper.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"$hook = {hook}\n"
            "try {\n"
            f"  $r = & {_q(UM_RUN)} -ScriptPath {_q(self.job)} -AgentShare {_q(self.share)} "
            "-TimeoutSec 30 -PollSeconds 1 -MaxQueueWaitSec 30 -JobId 'demo' "
            "-TestHookAtLoopTop $hook\n"
            "  Write-Output ('E2E_RESULT=OK EXIT=' + $r.exitCode)\n"
            "} catch { Write-Output ('THREW ' + $_.Exception.Message) }\n",
            encoding="utf-8",
        )
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(wrapper)],
                               capture_output=True, text=True, timeout=30)
        combined = proc.stdout + proc.stderr
        self.assertIn("THREW UNRESOLVED:", combined, combined)
        self.assertIn("IOException", combined, combined)
        self.assertNotIn("synthetic share hiccup", combined, combined)
        self.assertNotIn("RETRACTED", combined, combined)

    def test_a_receipt_already_present_survives_an_unexpected_error_via_the_structural_catch(self) -> None:
        # The other half of the same mechanism: a receipt that already landed on disk at the exact
        # instant the unexpected error fires must still be found by the structural catch's own
        # recheck -- an unrelated I/O hiccup elsewhere in the loop must never cost a receipt that
        # already exists.
        hook = (
            "{ "
            "$result = @{ jobId = 'demo'; exitCode = 0; stdout = 'late but real'; stderr = ''; "
            "timeoutSec = 30; timedOut = $false } | ConvertTo-Json -Compress; "
            f"$tmp = {_q(self.outbox / 'demo.result.tmp')}; "
            f"$fin = {_q(self.outbox / 'demo.result.json')}; "
            "Set-Content -LiteralPath $tmp -Value $result -Encoding ascii -NoNewline; "
            "Move-Item -Force -LiteralPath $tmp -Destination $fin; "
            "throw [System.IO.IOException]::new('synthetic share hiccup after the receipt landed') "
            "}"
        )
        wrapper = self.tmp / "run-with-loop-top-receipt-then-throw-hook.ps1"
        wrapper.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"$hook = {hook}\n"
            "try {\n"
            f"  $r = & {_q(UM_RUN)} -ScriptPath {_q(self.job)} -AgentShare {_q(self.share)} "
            "-TimeoutSec 30 -PollSeconds 1 -MaxQueueWaitSec 30 -JobId 'demo' "
            "-TestHookAtLoopTop $hook\n"
            "  Write-Output ('E2E_RESULT=OK EXIT=' + $r.exitCode)\n"
            "} catch { Write-Output ('THREW ' + $_.Exception.Message) }\n",
            encoding="utf-8",
        )
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(wrapper)],
                               capture_output=True, text=True, timeout=30)
        combined = proc.stdout + proc.stderr
        self.assertNotIn("THREW", combined, combined)
        self.assertIn("E2E_RESULT=OK EXIT=0", combined, combined)

    # ---- round 12 (sol MAJOR + fable MINOR, item 5): RETRACTED cleanup failures are surfaced -------

    def test_a_retraction_metadata_cleanup_failure_is_surfaced_not_silently_swallowed(self) -> None:
        # A failed nonce-checked delete of this submission's own claim metadata during RETRACTED
        # used to be silently swallowed (SilentlyContinue) -- surfaced now, matching the module
        # rollback's own "may outlive this refused submission" standard. Locks demo.meta.json
        # exclusively right after the retraction rename (and its own temp-copy cleanup) complete,
        # forcing the nonce-checked delete that follows to fail.
        hook = (
            "{ $script:umrunTestLock = [IO.File]::Open(" + _q(self.inbox / 'demo.meta.json') +
            ", 'Open', 'Read', 'None') }"
        )
        wrapper = self.tmp / "run-with-retraction-metadata-lock-hook.ps1"
        wrapper.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"$hook = {hook}\n"
            "try {\n"
            f"  $r = & {_q(UM_RUN)} -ScriptPath {_q(self.job)} -AgentShare {_q(self.share)} "
            "-TimeoutSec 1 -PollSeconds 1 -MaxQueueWaitSec 0 -JobId 'demo' "
            "-TestHookAfterRetractionRename $hook\n"
            "  Write-Output ('E2E_RESULT=OK EXIT=' + $r.exitCode)\n"
            "} catch { Write-Output ('THREW ' + $_.Exception.Message) }\n",
            encoding="utf-8",
        )
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(wrapper)],
                               capture_output=True, text=True, timeout=30)
        combined = proc.stdout + proc.stderr
        self.assertIn("THREW RETRACTED:", combined, combined)
        self.assertIn("meta.json could not be removed during retraction", combined, combined)
        self.assertIn("may outlive this refused submission", combined, combined)
        self.assertTrue((self.inbox / "demo.meta.json").exists(),
                         "a surfaced retraction cleanup failure must mean the metadata really was left behind")

    def test_a_retraction_job_temp_cleanup_failure_is_surfaced_not_silently_swallowed(self) -> None:
        # The other half: a failed removal of this client's OWN renamed-out job temporary (the
        # withdrawn job's bytes, moved aside during retraction) used to be silently swallowed too.
        # -TestHookAfterRetractionJobRenamed hands the test the exact, GUID-named path -- unknowable
        # to an external caller in advance -- so it can be locked deterministically right after the
        # rename succeeds, before this client's own removal attempt.
        hook = "{ param($p) $script:umrunTestLock2 = [IO.File]::Open($p, 'Open', 'Read', 'None') }"
        wrapper = self.tmp / "run-with-retraction-job-temp-lock-hook.ps1"
        wrapper.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"$hook = {hook}\n"
            "try {\n"
            f"  $r = & {_q(UM_RUN)} -ScriptPath {_q(self.job)} -AgentShare {_q(self.share)} "
            "-TimeoutSec 1 -PollSeconds 1 -MaxQueueWaitSec 0 -JobId 'demo' "
            "-TestHookAfterRetractionJobRenamed $hook\n"
            "  Write-Output ('E2E_RESULT=OK EXIT=' + $r.exitCode)\n"
            "} catch { Write-Output ('THREW ' + $_.Exception.Message) }\n",
            encoding="utf-8",
        )
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(wrapper)],
                               capture_output=True, text=True, timeout=30)
        combined = proc.stdout + proc.stderr
        self.assertIn("THREW RETRACTED:", combined, combined)
        self.assertIn("renamed-out temporary copy could not be removed", combined, combined)
        self.assertIn("may outlive this refused submission", combined, combined)
        # The locked temp file must still be sitting in the inbox -- a real, surfaced failure, not
        # a lucky-looking string.
        leftover = [p for p in self.inbox.iterdir() if p.name.endswith(".retracted.tmp")]
        self.assertEqual(len(leftover), 1, self.names())

    def test_a_receipt_written_during_the_final_sleep_is_still_read(self) -> None:
        # fable/sol major 3 (second half): the old loop tested its deadline BEFORE sleeping, so a
        # receipt published during the final poll sleep was skipped -- the deadline had already
        # passed by the time the loop would have looked again. PollSeconds is deliberately larger
        # than MaxQueueWaitSec so the run's only sleep straddles the deadline.
        proc = subprocess.Popen(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(UM_RUN),
             "-ScriptPath", str(self.job), "-AgentShare", str(self.share),
             "-TimeoutSec", "1", "-PollSeconds", "3", "-MaxQueueWaitSec", "1", "-JobId", "demo"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            time.sleep(1.5)   # during the loop's single sleep, after the 1s queue-wait ceiling
            result = {"jobId": "demo", "exitCode": 0, "stdout": "late but real", "stderr": "",
                      "timeoutSec": 1, "timedOut": False}
            tmp = self.outbox / "demo.result.tmp"
            tmp.write_text(json.dumps(result), encoding="ascii")
            tmp.replace(self.outbox / "demo.result.json")
            stdout, stderr = proc.communicate(timeout=15)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        # Checked by exit code and absence of the timeout throw, not by scraping stdout for the
        # returned object's fields: PowerShell's default console formatting of a returned
        # PSCustomObject is not a stable text contract to assert against.
        # Whitespace-flattened, with "|" removed: PowerShell's own default uncaught-exception
        # formatting word-wraps a long message at a fixed console width, prefixing each
        # continuation line with a literal "| " margin -- both would otherwise split a long
        # asserted phrase across a line break, or embed a stray "|" mid-phrase, by coincidence of
        # length rather than content. None of this file's own asserted text ever contains "|".
        combined = " ".join(_ANSI_ESCAPE.sub("", (stdout or "") + (stderr or "")).replace("|", " ").split())
        self.assertEqual(proc.returncode, 0, combined)
        self.assertNotIn("Timed out", combined, combined)

    def test_a_different_same_named_file_is_refused_and_no_job_is_dropped(self) -> None:
        (self.inbox / "demo-source.zip").write_bytes(b"someone else's bytes")
        proc = self.submit("-SideFile", str(self.side), "-JobId", "demo")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("UMRUN_SIDEFILE_CONFLICT", proc.stdout + proc.stderr)
        self.assertEqual(self.names(), ["demo-source.zip"])

    def test_an_identical_file_already_present_is_accepted(self) -> None:
        shutil.copy2(self.side, self.inbox / "demo-source.zip")
        proc = self.submit("-SideFile", str(self.side), "-JobId", "demo")
        combined = proc.stdout + proc.stderr
        self.assertIn("already present with matching sha256", proc.stdout, combined)
        # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 11: RETRACTED at the queue-wait ceiling removes
        # the job and its claim metadata; the side-file (placed before submission even began, and
        # never this client's to retract) is untouched.
        self.assertIn("RETRACTED:", combined, combined)
        self.assertEqual(self.names(), ["demo-source.zip"])

    def test_semicolon_list_places_every_file(self) -> None:
        second = self.local / "demo-build.json"
        second.write_text("{}", encoding="utf-8")
        proc = self.submit("-SideFile", f"{self.side};{second}", "-JobId", "demo")
        combined = proc.stdout + proc.stderr
        self.assertIn("RETRACTED:", combined, combined)
        self.assertEqual(self.names(), ["demo-build.json", "demo-source.zip"], combined)


class UmRunDeadlineTypeTests(unittest.TestCase):
    """ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 12 (sol MINOR, item 6): every wait-loop deadline is
    built from [DateTimeOffset]::Now specifically so a comparison against another DateTimeOffset is
    defined on the absolute instant represented, regardless of Kind/offset -- reverting any ONE of
    these root assignments back to [DateTime]/Get-Date reintroduces the exact Kind-ambiguity a prior
    round had to remove (round 10/11's own header comment), but no test failed if that reversion
    happened on a host whose local UTC offset is zero. This asserts on the TYPE the real source
    actually constructs at each root assignment -- extracted via AST from um-run.ps1 itself, never a
    hand-copied duplicate -- so it fails on EVERY host, independent of timezone, the instant any one
    of these assignments stops constructing a DateTimeOffset. $queueDeadline/$budgetDeadline/
    $outerCeiling are not checked directly: each is built via .AddSeconds() on an already-verified
    DateTimeOffset root, and .AddSeconds() on a DateTimeOffset cannot itself return anything else --
    the four ROOT `[DateTimeOffset]::Now` assignments are the only sites this class of regression
    can actually enter at."""

    @unittest.skipIf(PWSH is None, "pwsh is not on PATH")
    def test_every_now_anchored_deadline_variable_constructs_a_datetimeoffset(self) -> None:
        with tempfile.TemporaryDirectory(prefix="umrundeadlinetype-") as tmp_dir:
            script = Path(tmp_dir) / "deadline-type-probe.ps1"
            script.write_text(
                "$ErrorActionPreference = 'Stop'\n"
                f"$text = [IO.File]::ReadAllText({_q(UM_RUN)})\n"
                "$t = $null; $e = $null\n"
                "$ast = [System.Management.Automation.Language.Parser]::ParseInput($text, [ref]$t, [ref]$e)\n"
                "$assignments = @($ast.FindAll({ param($n) $n -is "
                "[System.Management.Automation.Language.AssignmentStatementAst] -and "
                "$n.Left.Extent.Text -in @('$submittedAt', '$claimedAt') -and "
                "$n.Right.Extent.Text -eq '[DateTimeOffset]::Now' }, $true))\n"
                "if ($assignments.Count -eq 0) { throw 'NO_ROOT_ASSIGNMENTS_FOUND' }\n"
                "$types = @($assignments | ForEach-Object { (Invoke-Expression $_.Right.Extent.Text).GetType().Name })\n"
                "@{ count = $assignments.Count; types = $types } | ConvertTo-Json -Compress\n",
                encoding="utf-8",
            )
            proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script)],
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            result = json.loads(proc.stdout)
        # $submittedAt (1 site) plus $claimedAt (3 separate assignment sites -- the QUEUED-to-
        # CLAIMED transition, the marker-appears-at-the-queue-deadline transition, and the
        # retraction-race fallback). A future edit that adds or removes a root assignment must
        # update this count deliberately, not silently pass with fewer sites checked.
        self.assertEqual(result["count"], 4, result)
        types = result["types"] if isinstance(result["types"], list) else [result["types"]]
        for type_name in types:
            self.assertEqual(
                type_name, "DateTimeOffset",
                f"a wait-loop deadline root assignment constructs {type_name}, not DateTimeOffset "
                f"-- reintroducing the Kind-ambiguity a prior round had to remove: {result}",
            )


if __name__ == "__main__":
    unittest.main()
