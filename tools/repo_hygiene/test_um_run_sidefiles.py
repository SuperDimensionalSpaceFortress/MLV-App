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

import ast
import re
import hashlib
import json
import os
import shutil
import subprocess
import sys
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
ATTR_CUDA_MODULE = ROOT / "tools" / "profiling" / "bachelor" / "AttrCudaArtifacts.psm1"
PWSH = shutil.which("pwsh")
GIT = shutil.which("git")

# Tracked-fixture admission asks git whether the file is tracked, so those cases need git.
requires_git = unittest.skipIf(GIT is None, "git is not on PATH")


def _q(path: Path | str) -> str:
    return "'" + str(path).replace("'", "''") + "'"


CLAIM_WAIT_PASS = "PASS"
CLAIM_WAIT_FAIL = "FAIL"
CLAIM_WAIT_INCONCLUSIVE = "INCONCLUSIVE"


def judge_claim_wait(claim_lo: float, claim_hi: float, verdict_time: float, timeout_sec: float,
                     slack: float = 0.5) -> str:
    """Judge the claimed wait from three epoch-second stamps, with no process involved.

    UMRUN-SIBLING-1122-CLAIM-WINDOW-ORACLE-1: pure so the verdict table is covered deterministically
    (UmRunClaimWaitOracleTests) instead of by whatever the scheduler does to a live client.

    FAIL: the verdict came before even the earliest possible claim (claim_lo) plus the budget: a
    correct client cannot do that. PASS: the verdict is also a full budget after the LATEST possible
    claim (claim_hi), so the wait was served whatever happened inside the claim window. Between the
    two the stamps cannot say where the time went: a client that stalled ~8 s inside the window
    (before or after assigning claimedAt) and a healthy one look alike, so the outcome is
    INCONCLUSIVE, never a FAIL and never a PASS. The window is microseconds on a healthy host, so
    this almost never triggers; it only stops a stalled run from vouching for a wrong anchor."""
    if verdict_time - claim_lo < timeout_sec - slack:
        return CLAIM_WAIT_FAIL
    if verdict_time - claim_hi < timeout_sec - slack:
        return CLAIM_WAIT_INCONCLUSIVE
    return CLAIM_WAIT_PASS


def skip_if_claim_wait_inconclusive(test: unittest.TestCase, claim_lo: float, claim_hi: float,
                                    verdict_time: float, timeout_sec: float, timing_facts: str) -> None:
    """The live wait test's last step, extracted so a committed test can drive it without a UM client
    (UMRUN-CLAIM-WINDOW-ORACLE-BOUNDARY-PINS-1): an INCONCLUSIVE verdict skips, anything else returns."""
    if judge_claim_wait(claim_lo, claim_hi, verdict_time, timeout_sec) == CLAIM_WAIT_INCONCLUSIVE:
        test.skipTest(
            "INCONCLUSIVE: the claim window (claim_lo -> claim_hi) was wide enough that a stall "
            "inside it cannot be told from an hour-back client; every other check passed "
            f"[{timing_facts}]")


_SEAM_CALL_ARGS = ["self", "claim_lo", "claim_hi", "verdict_time", "timeout_sec", "timing_facts"]
_SEAM_CALL_METHOD = "test_a_clock_skewed_claim_marker_observed_at_the_queue_deadline_does_not_shrink_the_clients_patience"


def seam_call_position_problem(source: str, class_name: str, method_name: str | None = None) -> str | None:
    """UMRUN-SEAM-CALL-POSITION-PIN-1: None when `class_name` in `source` calls
    skip_if_claim_wait_inconclusive exactly once, as the FINAL top-level statement of a method body
    and with the live test's own arguments; otherwise a sentence saying what is wrong. The seam only
    skips after every other check has run, so a call placed earlier would skip past the UNRESOLVED /
    started-marker asserts below it. Takes source text so a test can feed it a mutated copy. When
    `method_name` is given, the call must also sit in the method of that name.

    Final-statement placement is a positional contract only: it does not prove the call is reached at
    run time (an early return, a raised skip or an exception above it still bypasses it). The live
    test's own asserts are what make it reachable."""
    classes = [node for node in ast.walk(ast.parse(source))
               if isinstance(node, ast.ClassDef) and node.name == class_name]
    if len(classes) != 1:
        return f"expected exactly one class {class_name}, found {len(classes)}"
    calls = [(member, node) for member in classes[0].body for node in ast.walk(member)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
             and node.func.id == "skip_if_claim_wait_inconclusive"]
    if len(calls) != 1:
        return f"expected exactly one seam call in {class_name}, found {len(calls)}"
    method, call = calls[0]
    if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return "the seam call is not inside a method body"
    if method_name is not None and method.name != method_name:
        return f"the seam call is in {method.name}, not in the expected method {method_name}"
    if [ast.unparse(arg) for arg in call.args] != _SEAM_CALL_ARGS or call.keywords:
        return f"{method.name} calls the seam with unexpected arguments: {ast.unparse(call)}"
    final = method.body[-1]
    if not (isinstance(final, ast.Expr) and final.value is call):
        return (f"{method.name}: the seam call (line {call.lineno}) is not the final top-level "
                f"statement of the method body (that is line {final.lineno})")
    return None


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

    # ---- following the client's own poll loop instead of the wall clock -----------------------
    # UMRUN-E2E-TIMING-FLAKES-1: a test that choreographs a child pwsh with time.sleep() races the
    # child's start-up (module import + the drop can take several seconds on a hosted runner, so a
    # marker written at "t=1s" and removed at "t=2s" can come and go before the client ever polls
    # -- the client then never saw a claim and RETRACTs) and, when the test also rewrites a file the
    # client reads, races that read (a truncate-then-write torn heartbeat reads as state=?). The
    # client already exposes -TestHookAtLoopTop, run at the top of every wait-loop iteration; the
    # hook below logs "<poll number> <epoch ms>" there, so a test advances on what the client has
    # DONE (it has polled N times) and never on how long it has been. This tick hook changes nothing
    # in um-run.ps1 or UmRunDrop.psm1 (production never passes it); the separate claim hook below
    # is a gated seam of its own (UMRUN-SIBLING-1122-WAIT-LOCUS-1).
    def start_ticked(self, *um_run_args: str, hold_first_poll_until: Path | None = None,
                     queue_deadline_hook: str | None = None,
                     tick_action: tuple[int, str] | None = None,
                     claim_hook: str | None = None) -> subprocess.Popen:
        """Start um-run.ps1 for JobId demo with the tick hook; failures print `THREW <message>` and exit 1.

        hold_first_poll_until: the client blocks (at most 120 s) at the top of its FIRST poll until this
        path exists, so a test can guarantee a claim marker is in place before the client makes any
        queue-deadline decision, whatever the scheduler does to the test process in between.

        queue_deadline_hook: a PowerShell scriptblock literal (`{ ... }`) passed as -TestHookAtQueueDeadline,
        run inside the client at exactly the queue-deadline recheck.

        tick_action: (poll number, PowerShell statements). The statements run inside the client, right
        after it logs that poll, so a side effect lands at a point in the client's own poll sequence
        and not at whatever moment the test process happens to get scheduled.

        claim_hook: a PowerShell scriptblock literal (`{ ... }`) passed as
        -TestHookAroundQueueDeadlineClaim; the client calls it with no arguments twice, immediately
        before and immediately after the queue-deadline recheck sets `$claimedAt`, so a test can
        stamp, from its own clock, a lower and an upper bound on the instant the claimed wait begins
        (never the client's `$claimedAt` value, which a broken client would get wrong).

        On any throw the client's wrapper also stamps the instant of its own verdict (verdict_at()),
        so a test can judge WHEN the client decided without depending on when the parent noticed."""
        self.ticks_path = self.tmp / "client-ticks.log"
        self.verdict_path = self.tmp / "client-verdict-at.log"
        hold = _q(hold_first_poll_until) if hold_first_poll_until is not None else "''"
        queue_hook_arg = " -TestHookAtQueueDeadline $queueHook" if queue_deadline_hook is not None else ""
        claim_hook_arg = " -TestHookAroundQueueDeadlineClaim $claimHook" if claim_hook is not None else ""
        action_poll, action_body = tick_action if tick_action is not None else (0, "")
        wrapper = self.tmp / "run-ticked.ps1"
        wrapper.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"$queueHook = {queue_deadline_hook if queue_deadline_hook is not None else '$null'}\n"
            f"$claimHook = {claim_hook if claim_hook is not None else '$null'}\n"
            f"$tickLog = {_q(self.ticks_path)}\n"
            f"$tickHold = {hold}\n"
            "$tickHook = {\n"
            "  $tickN = 1 + @(Get-Content -LiteralPath $tickLog -ErrorAction SilentlyContinue).Count\n"
            "  if ($tickN -eq 1 -and $tickHold) {\n"
            "    $tickUntil = [DateTime]::UtcNow.AddSeconds(120)\n"
            "    while (-not (Test-Path -LiteralPath $tickHold) -and [DateTime]::UtcNow -lt $tickUntil) { Start-Sleep -Milliseconds 20 }\n"
            "  }\n"
            "  $tickLine = '{0} {1}' -f $tickN, [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()\n"
            "  for ($tickTry = 0; $tickTry -lt 40; $tickTry++) {\n"
            "    try { Add-Content -LiteralPath $tickLog -Value $tickLine -Encoding ascii -ErrorAction Stop; break }\n"
            "    catch { Start-Sleep -Milliseconds 25 }\n"
            "  }\n"
            f"  if ($tickN -eq {action_poll}) {{ {action_body} }}\n"
            "}\n"
            "try {\n"
            f"  $r = & {_q(UM_RUN)} -ScriptPath {_q(self.job)} -AgentShare {_q(self.share)} "
            f"-PollSeconds 1 -JobId 'demo' {' '.join(um_run_args)} -TestHookAtLoopTop $tickHook{queue_hook_arg}{claim_hook_arg}\n"
            "  Write-Output ('E2E_RESULT=OK EXIT=' + $r.exitCode)\n"
            "} catch {\n"
            f"  Set-Content -LiteralPath {_q(self.verdict_path)} -Value ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()) -Encoding ascii\n"
            "  Write-Output ('THREW ' + $_.Exception.Message); exit 1\n"
            "}\n",
            encoding="utf-8",
        )
        return subprocess.Popen([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(wrapper)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def ticks(self) -> list[tuple[int, float]]:
        """(poll number, epoch seconds) for every poll the client has started so far."""
        try:
            text = self.ticks_path.read_text(encoding="ascii")
        except OSError:
            return []
        polls = []
        for line in text.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():   # a torn last line is skipped
                polls.append((int(parts[0]), int(parts[1]) / 1000.0))
        return polls

    def verdict_at(self) -> float:
        """Epoch seconds at which the client's wrapper caught the client's throw (its verdict)."""
        return int(self.verdict_path.read_text(encoding="ascii").strip()) / 1000.0

    def wait_for_polls(self, proc: subprocess.Popen, count: int, *, within: float = 120.0) -> None:
        """Block until the client has started `count` polls. `within` is a hang guard, not a budget."""
        deadline = time.monotonic() + within
        while time.monotonic() < deadline:
            if len(self.ticks()) >= count:
                return
            self.assertIsNone(proc.poll(), f"um-run exited before it started poll {count}")
            time.sleep(0.05)
        self.fail(f"um-run never started poll {count} within {within}s (saw {len(self.ticks())})")

    def wait_until_the_client_has_read(self, proc: subprocess.Popen) -> None:
        """Return once the client has fully run one poll that began after this call.

        Poll N+1 starts after the caller's last write, so it reads that write; poll N+2 starting
        proves poll N+1 ran to its end. (Poll N, already running when the caller wrote, may or may
        not have seen it -- it is never relied on.)"""
        self.wait_for_polls(proc, len(self.ticks()) + 2)

    def drop(self, copier: str, *, side: list[Path] | None = None, job_id: str = "demo",
             module: Path | None = None, repo_root: Path | None = None,
             job_timeout_sec: int | None = None,
             before_job_visible: str | None = None,
             after_meta_tmp_written: str | None = None) -> subprocess.CompletedProcess:
        sides = ",".join(_q(p) for p in (side if side is not None else [self.side]))
        timeout_arg = "" if job_timeout_sec is None else f" -JobTimeoutSec {job_timeout_sec}"
        script = self.tmp / "drop.ps1"
        repo_root_arg = f" -RepoRoot {_q(repo_root)}" if repo_root is not None else ""
        preamble = (
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module {_q(module or MODULE)} -Force\n"
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
            f"-JobId '{job_id}' -SideFile @({sides}){timeout_arg}{hook_args} -Copier $copier{repo_root_arg}\n"
            "} catch { Write-Output ('THREW ' + $_.Exception.Message) }\n",
            encoding="utf-8",
        )
        return subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script)],
                              capture_output=True, text=True)


OBSERVING = "{ param($s, $d) Add-Content -LiteralPath $log -Value $d; Copy-Item -LiteralPath $s -Destination $d }"
CORRUPTING = ("{ param($s, $d) Copy-Item -LiteralPath $s -Destination $d; "
              "if ($d -like '*.sidepart') { [IO.File]::AppendAllText($d, 'x') } }")
CORRUPTING_JOB = ("{ param($s, $d) Copy-Item -LiteralPath $s -Destination $d; "
                   "if ($d -like '*.job.tmp') { [IO.File]::AppendAllText($d, 'x') } }")
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

    def test_job_bytes_altered_on_the_share_are_refused_and_the_job_is_not_placed(self) -> None:
        # This is the falsifier for round 2g's UMRUN_JOB_VERIFY_FAILED fix (item 5, PR #140
        # round 2h): before round 2g, the job's own .job.tmp was renamed with NO round-trip
        # verification at all, so this exact corruption would have been published as
        # inbox/demo.job.ps1 unnoticed. Removing the fix makes this test fail (NO_THROW, and the
        # corrupted job present at demo.job.ps1) instead of throwing UMRUN_JOB_VERIFY_FAILED.
        proc = self.drop(CORRUPTING_JOB)
        self.assertIn("THREW UMRUN_JOB_VERIFY_FAILED", proc.stdout, proc.stdout + proc.stderr)
        # Side-file placement precedes the job attempt (proven above) and is not rolled back on a
        # later job-stage failure, so the side-file remains; the job itself must not.
        self.assertEqual(self.names(), ["demo-source.zip"], "the corrupted job must not be placed")

    def test_a_side_file_that_appears_concurrently_with_other_bytes_is_not_overwritten(self) -> None:
        proc = self.drop(RACING_SIDE)
        self.assertIn("THREW UMRUN_SIDEFILE_CONFLICT", proc.stdout, proc.stdout + proc.stderr)
        self.assertEqual((self.inbox / "demo-source.zip").read_text(encoding="utf-8"), "another submitter")
        self.assertEqual(self.names(), ["demo-source.zip"], "no job may be dropped after a conflict")

    def test_a_write_attempt_between_the_share_hash_and_the_rename_is_denied(self) -> None:
        # ATTR3-ADMIT-CONTENT-PIN-1 round 2e (sol BLOCKER): the share round-trip hash and the
        # rename used to be two independent operations on the mutable .sidepart path, leaving a
        # gap for a concurrent writer to swap the verified bytes before the rename picked them up.
        # -PreRenameRaceHook fires in exactly that gap, with the module's own deny-write handle on
        # the .sidepart STILL OPEN -- so the hook's own write attempt is racing the CLOSED window,
        # not an open one. The hook catches its own exception (mirroring what an external writer's
        # attempt would experience -- a failure on ITS side, invisible to Invoke-UmRunDrop) and
        # logs the outcome, so this test can assert on what happened without relying on
        # Invoke-UmRunDrop reacting to it. On the pre-2e shape (Get-FileHash opens and closes its
        # own independent handle, then Move-Item opens a third one), this same hook's write would
        # have SUCCEEDED and the corrupted bytes would have been renamed into place unverified.
        hook = (
            "{ param($p) try { "
            "[IO.File]::WriteAllBytes($p, [Text.Encoding]::UTF8.GetBytes('raced sidepart bytes')); "
            "Add-Content -LiteralPath $log -Value 'RACE_WRITE_SUCCEEDED' "
            "} catch { Add-Content -LiteralPath $log -Value ('RACE_WRITE_DENIED: ' + $_.Exception.GetType().Name) } }"
        )
        script = self.tmp / "prerename-race-drop.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module {_q(MODULE)} -Force\n"
            f"$log = {_q(self.log)}\n"
            f"$hook = {hook}\n"
            "try {\n"
            f"  Invoke-UmRunDrop -Inbox {_q(self.inbox)} -Outbox {_q(self.outbox)} -ScriptPath {_q(self.job)} "
            f"-JobId 'demo' -SideFile @({_q(self.side)}) -PreRenameRaceHook $hook\n"
            "} catch { Write-Output ('THREW ' + $_.Exception.Message) }\n",
            encoding="utf-8",
        )
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script)],
                              capture_output=True, text=True)

        self.assertIn("UMRUN_JOBID=demo", proc.stdout, proc.stdout + proc.stderr)
        log_text = self.log.read_text(encoding="utf-8")
        self.assertIn("RACE_WRITE_DENIED", log_text, log_text)
        self.assertNotIn("RACE_WRITE_SUCCEEDED", log_text, log_text)
        self.assertEqual(self.names(), ["demo-source.zip", "demo.job.ps1", "demo.meta.json"])
        self.assertEqual(
            hashlib.sha256((self.inbox / "demo-source.zip").read_bytes()).hexdigest(),
            hashlib.sha256(self.side.read_bytes()).hexdigest(),
            "the placed bytes must be the untampered originals, not the raced write attempt",
        )

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

    @requires_git
    def test_a_bachelor_less_host_refuses_a_tracked_fixture_rather_than_admitting_it_unpinned(self) -> None:
        # fable MINOR: nothing previously proved the module-absent path fails CLOSED end to end.
        # A "if present assert, else return" rewrite of Test-UmRunFixtureContentPin would make
        # Get-UmRunFixtureAdmission treat the tracked fixture below as admitted-but-unverified,
        # which -- because admission is what EXEMPTS a fixture from the extension allowlist --
        # would place these bytes anyway despite the media extension the allowlist refuses.
        # Round 2d: this used to RENAME the real, tracked bachelor module in the live working
        # tree and restore it in a finally -- a hard-killed run left the checkout without it and
        # dirty. A COPY of UmRunDrop.psm1 with no bachelor/ next to it produces the identical
        # module-absent condition instead ($script:AttrCudaArtifactsModulePath is derived from the
        # importing copy's own $PSScriptRoot), so the real tree is never touched; -RepoRoot pins
        # the copy back to the real repository so the real fixture is still what gets admitted.
        clip = self.repo_fixture()
        module_copy = self.tmp / "no-bachelor" / "UmRunDrop.psm1"
        module_copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(MODULE, module_copy)
        proc = self.drop(OBSERVING, side=[clip], module=module_copy, repo_root=ROOT)
        # round 2e (sol/fable MAJOR): a module-missing host is a COULD-NOT-DETERMINE outcome, not
        # a definite "this fixture is bad" -- distinct token from a real content refusal so a
        # caller filtering on it does not conflate the two.
        self.assertIn("THREW UMRUN_SIDEFILE_ADMISSION_INDETERMINATE", proc.stdout, proc.stdout + proc.stderr)
        self.assertEqual(self.names(), [], "a bachelor-less host must never place an unverified fixture")

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
        #
        # UMRUN-E2E-TIMING-FLAKES-1: this used to run -TimeoutSec 2 on wall-clock sleeps (marker at
        # t=1s, removed at t=2s). On a hosted runner the child pwsh could reach its first poll only
        # AFTER the marker had come and gone (merge_group run 38675787436: "agent heartbeat is 2s
        # old" at start-up, then RETRACTED after the 10 s queue ceiling), and even when it saw the
        # marker the old assertions could not tell a marker-anchored deadline from the correct one --
        # with the marker present a client waits either way, and after it is removed both throw.
        # Now (1) the test advances on the client's own polls (start_ticked), so the marker is
        # always observed before it is removed; (2) the heartbeat's mtime is set an hour old, so it
        # is stale by construction rather than by elapsed time; and (3) -TimeoutSec 8 makes the claim
        # measurable: a client whose deadline starts at its own observation cannot reach the liveness
        # verdict before observation + 8 s, while one anchored to the marker's hour-old startedUtc
        # (the bug) throws at its first poll after the marker is gone, 1-2 s after the marker was
        # written. The assertion is on the time of the client's LAST poll against the time the marker
        # was written -- a delay (a stall, a slow runner) can only move that later, so a correct
        # client cannot fail it, and the bug is caught unless something stalls it for 4+ s.
        timeout_sec = 8
        running_dir = self.share / "running"
        marker_path = running_dir / "demo.started.json"
        stale_at = time.time() - 3600
        os.utime(self.share / "heartbeat.txt", (stale_at, stale_at))
        proc = self.start_ticked("-TimeoutSec", str(timeout_sec), "-MaxQueueWaitSec", "300",
                                 "-MaxHeartbeatAgeSec", "2")
        try:
            self.wait_for_polls(proc, 1)   # the client has dropped the job and is polling
            running_dir.mkdir(parents=True, exist_ok=True)
            skewed = time.gmtime(time.time() - 3600)
            marker = {"jobId": "demo", "startedUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", skewed)}
            marker_written_at = time.time()
            marker_path.write_text(json.dumps(marker), encoding="ascii")
            self.wait_until_the_client_has_read(proc)   # it has now observed the marker
            # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 11 (item 3): the started marker's own
            # presence is now an independent proof the agent may still own this job (the deployed
            # agent's silent kill/drain/publish window) -- removing it here simulates that window
            # having already ended (Complete-StartedMarker already ran, strictly after some receipt
            # was published) so the stale-heartbeat diagnosis below can actually fire, the same way
            # a real agent's own marker removal would let it fire in production.
            marker_path.unlink()
            stdout, stderr = proc.communicate(timeout=timeout_sec + 90)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        last_poll_at = self.ticks()[-1][1]
        self.assertGreaterEqual(
            last_poll_at - marker_written_at, timeout_sec - 2,
            "a stale agent-clock stamp must not shrink the client's own patience: it reached the "
            f"liveness verdict {last_poll_at - marker_written_at:.1f}s after the marker was written, "
            f"before its own {timeout_sec}s budget (counted from observing the marker) could elapse")
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
        #
        # UMRUN-E2E-TIMING-FLAKES-1: this used to hold the marker for a wall-clock 1 s..3 s window
        # with -MaxQueueWaitSec 5; a child pwsh that reached its first poll after the marker was
        # removed never saw a claim and RETRACTED at the 5 s queue ceiling (PR #427 r4, run
        # 38568661635). Now the test advances on the client's own polls (start_ticked): the marker is
        # removed only once the client has observed it. The heartbeat is written once, naming another
        # job, with -MaxHeartbeatAgeSec 3600 so it is fresh by age for the whole test whatever the
        # runner does -- the ONLY thing that can make it non-fresh is the job mismatch this test
        # pins (a broken mismatch check would treat it as fresh and wait, and the test would time out).
        running_dir = self.share / "running"
        marker_path = running_dir / "demo.started.json"
        proc = self.start_ticked("-TimeoutSec", "1", "-MaxQueueWaitSec", "300", "-MaxHeartbeatAgeSec", "3600")
        try:
            self.wait_for_polls(proc, 1)   # the client has dropped the job and is polling
            running_dir.mkdir(parents=True, exist_ok=True)
            marker = {"jobId": "demo", "startedUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            marker_path.write_text(json.dumps(marker), encoding="ascii")
            # Fresh, but for a DIFFERENT job.
            self.touch_heartbeat(job_id="someone-elses-job")
            self.wait_until_the_client_has_read(proc)   # it has now observed the claim
            # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 11 (item 3): a job-mismatch alone is not
            # enough to end the wait any more -- the started marker must ALSO be gone (simulating
            # Complete-StartedMarker having already run for whatever job the agent is now on).
            marker_path.unlink()
            try:
                stdout, stderr = proc.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                self.fail("the client never stopped: a heartbeat naming a DIFFERENT job, with the started "
                          "marker gone, was treated as proof of liveness for this job")
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
        # before this client's real budget (-TimeoutSec 8 here) could possibly be exhausted.
        #
        # UMRUN-SIBLING-1122-VACUOUS-1: that test was VACUOUS. It checked only that the process was
        # still alive 2 s after the marker appeared and then delivered a receipt -- but with the
        # default 30 s heartbeat freshness and a heartbeat written at setUp, a client whose deadline
        # was (wrongly) anchored an hour in the past ALSO stays alive: the marker is present, so the
        # "may still own the job" rule keeps it waiting, and the receipt lands first. Breaking the
        # queue-deadline `$claimedAt = ...` site left it green. It now uses the same shape as the
        # skew test above (UMRUN-E2E-TIMING-FLAKES-1): (1) the heartbeat's mtime is an hour old, so
        # liveness is lost by construction; (2) the marker is removed INSIDE the client, at the top
        # of its second poll (tick_action; the claim is set before that poll begins), so the first
        # budget check that finds the budget spent also finds no marker and throws -- and no
        # parent-side delay can move that removal; and (3) -TimeoutSec 8 makes the budget
        # measurable. A client whose claim starts at its own observation cannot reach that verdict
        # before observation + 8 s; one anchored to the hour-old startedUtc throws on poll 2 itself.
        #
        # What is judged is the client's DECISION, not a poll: the instant the client's own wrapper
        # caught the throw (verdict_at), measured from the instant the hook placed the marker (which
        # is before the client's claimedAt, so the bound only errs toward the client). A scheduler
        # pause between a poll's start and its verdict can therefore only move the verdict later --
        # a correct client cannot fail on it (UMRUN-SIBLING-1122-VACUOUS-1 r2, sol BLOCKER). The
        # poll count is deliberately NOT a discriminator (r3, sol r2 BLOCKER): a correct client
        # whose poll 2 is delayed past the budget also decides after two polls.
        timeout_sec = 8
        running_dir = self.share / "running"
        marker_path = running_dir / "demo.started.json"
        claim_stamp_path = self.tmp / "marker-placed-at.log"
        stale_at = time.time() - 3600
        os.utime(self.share / "heartbeat.txt", (stale_at, stale_at))
        hook = (
            "{ "
            f"New-Item -ItemType Directory -Force -Path {_q(running_dir)} | Out-Null; "
            "$marker = @{ jobId = 'demo'; startedUtc = (Get-Date).AddHours(-1).ToUniversalTime().ToString('o') } "
            "| ConvertTo-Json -Compress; "
            f"Set-Content -LiteralPath {_q(marker_path)} -Value $marker "
            "-Encoding ascii -NoNewline; "
            f"Set-Content -LiteralPath {_q(claim_stamp_path)} "
            "-Value ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()) -Encoding ascii "
            "}"
        )
        # UMRUN-SIBLING-1122-WAIT-LOCUS-1 r3: the claim instant is BRACKETED by the client calling
        # -TestHookAroundQueueDeadlineClaim immediately before it sets $claimedAt (first line:
        # claim_lo) and again immediately after (last line: claim_hi). The hook reads the real
        # clock (same PowerShell UtcNow as the other stamps) and is NOT handed $claimedAt: a client
        # whose claim is anchored wrongly (an hour back) would otherwise vouch for its own claim.
        # The in-wait bound below is anchored to claim_lo, which is never later than the true claim,
        # so a scheduler pause between the claim and the hook's later sample (claim_hi) cannot make
        # a correct client look early; and not to poll 2, which comes later still.
        claim_at_path = self.tmp / "claim-at.log"
        claim_hook = (
            "{ "
            f"Add-Content -LiteralPath {_q(claim_at_path)} "
            "-Value ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()) -Encoding ascii "
            "}"
        )
        # The started marker's presence keeps a client waiting past its budget; it is removed (in the
        # client, on poll 2) so the stale-heartbeat verdict can fire (see the skew test above).
        remove_marker = f"Remove-Item -LiteralPath {_q(marker_path)} -Force"
        proc = self.start_ticked("-TimeoutSec", str(timeout_sec), "-MaxQueueWaitSec", "0",
                                 "-MaxHeartbeatAgeSec", "2", queue_deadline_hook=hook,
                                 tick_action=(2, remove_marker), claim_hook=claim_hook)
        try:
            stdout, stderr = proc.communicate(timeout=timeout_sec + 90)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        self.assertTrue(self.verdict_path.exists(),
                        "the client returned instead of throwing UNRESOLVED, so it left no verdict "
                        "stamp to judge")
        marker_placed_at = int(claim_stamp_path.read_text(encoding="ascii").strip()) / 1000.0
        decided_after = self.verdict_at() - marker_placed_at
        # The anchor is judged by TIME ALONE: a correct client sets claimedAt after the hook stamped
        # marker_placed_at and throws only once Now >= claimedAt + timeout_sec, so its verdict is
        # >= timeout_sec after the stamp whatever the scheduler does and however many polls it got
        # (the 0.5 s of slack covers the stamps' millisecond truncation and a system-clock
        # adjustment between the two samples; the interval between the hook's stamp and the
        # client's own claim can only make decided_after LONGER and no process start happens in it,
        # so it needs no slack; both the stamp and the verdict come from PowerShell
        # DateTimeOffset.UtcNow, so it is not a PowerShell/Python clock difference). The hour-back client
        # decides on poll 2, about a second after the stamp. No poll-count bound is asserted: one
        # would reject a correct client whose polls were delayed (UMRUN-SIBLING-1122-VACUOUS-1 r3,
        # sol r2 BLOCKER); only the floor of two polls is, since the marker is removed on the second.
        # The raw timing facts go to the log on the pass path too, so a scheduler-stalled false PASS
        # (verdict >= 7.5 s after the stamp on a client that should decide in ~1 s) or a near-miss
        # is visible in a hosted CI log (sol r3, Dot f936802c).
        #
        # UMRUN-SIBLING-1122-WAIT-LOCUS-1: elapsed wall time alone does not say WHERE the wait
        # happened, so the facts also record the wait locus. The client enters its claimed wait at
        # the instant it sets $claimedAt, which lies between the two stamps the claim hook takes
        # (claim_lo before it, claim_hi after it; the hook samples the clock itself and is given
        # nothing by the client) and leaves it at the throw its wrapper stamps as verdict_at; all
        # stamps are the same PowerShell clock. pre_wait is the time before that entry (the hook
        # stamp to claim_lo: no waiting by the client, only scheduling), in_wait the time inside it
        # measured from claim_lo (so it can only over-state the wait, toward the client),
        # claim_gap the claim_lo -> claim_hi spread (scheduling between the two samples, recorded,
        # not judged), lo_to_first_poll the claim_lo -> poll-2 gap (the first poll after the claim; it
        # includes the claim window itself and so the time before the claim, a scheduling delay
        # a correct client may have, recorded and not judged on its own),
        # tail the time from the last poll to the verdict, max_tick_gap the longest pause between
        # two polls (a stall inside the wait shows as a gap far above the 1 s poll interval). A
        # stall in pre_wait pushes decided_after over the bound for a client that decided at once;
        # in_wait is judged below.
        all_ticks = self.ticks()
        verdict_time = self.verdict_at()
        tick2_at = next((at for number, at in all_ticks if number == 2), None)
        claim_stamps = ([int(line.strip()) / 1000.0
                         for line in claim_at_path.read_text(encoding="ascii").splitlines()
                         if line.strip()] if claim_at_path.exists() else [])
        claim_lo = claim_stamps[0] if len(claim_stamps) >= 2 else None
        claim_hi = claim_stamps[-1] if len(claim_stamps) >= 2 else None
        locus_facts = f" wait_locus=unknown(claim stamps={len(claim_stamps)}, need 2)"
        if claim_lo is not None:
            wait_ticks = [at for number, at in all_ticks if number >= 2]
            gaps = [later - earlier for earlier, later in zip(wait_ticks, wait_ticks[1:])]
            lo_to_first_poll = f"{tick2_at - claim_lo:.3f}s" if tick2_at is not None else "none"
            locus_facts = (
                f" claim_lo={claim_lo:.3f} claim_hi={claim_hi:.3f} claim_gap={claim_hi - claim_lo:.3f}s"
                f" pre_wait={claim_lo - marker_placed_at:.3f}s"
                f" in_wait={verdict_time - claim_lo:.3f}s lo_to_first_poll={lo_to_first_poll}"
                f" tail={verdict_time - (wait_ticks[-1] if wait_ticks else claim_lo):.3f}s"
                f" polls_in_wait={len(wait_ticks)} max_tick_gap={max(gaps, default=0.0):.3f}s")
        timing_facts = (
            f"decided_after={decided_after:.3f}s marker_placed_at={marker_placed_at:.3f} "
            f"verdict_at={verdict_time:.3f} tick2_at="
            + (f"{tick2_at:.3f}" if tick2_at is not None else "none")
            + f" timeout_sec={timeout_sec}" + locus_facts)
        print(f"UMRUN-1122-TIMING {timing_facts}", file=sys.stderr)
        self.assertGreaterEqual(
            decided_after, timeout_sec - 0.5,
            "a stale agent-clock stamp observed at the queue deadline must not shrink the client's "
            f"own patience: it reached the liveness verdict {decided_after:.1f}s "
            f"after the marker was placed, before its own {timeout_sec}s budget (counted from "
            f"observing the marker) could elapse [{timing_facts}]")
        self.assertGreaterEqual(
            len(self.ticks()), 2,
            "the client never reached its second poll, the one on which the marker is removed")
        # The waited interval must lie INSIDE the wait region: the time the client spent between
        # entering its claimed wait and its verdict has to cover the budget by itself, so a stall
        # before the wait (pre_wait) cannot carry decided_after over the bound above for a client
        # that decided at once. The wait is measured from claim_lo, the stamp taken immediately
        # BEFORE the client sets $claimedAt, never from poll 2 and never from the later claim_hi
        # sample: a correct client throws once Now >= claimedAt + timeout_sec, and claimedAt >=
        # claim_lo, so verdict - claim_lo can only be LONGER than the wait the client really did;
        # a scheduling delay between the claim and either poll 2 (lo_to_first_poll) or the hook's later
        # sample (claim_gap) cannot fail a correct client (r2 sol r1 BLOCKER; r3 sol r2 BLOCKER).
        # A stall that precedes claim_lo is still caught. Same 0.5 s slack as above (stamp
        # truncation and system-clock adjustment only); the bound is not looser than the one above.
        #
        # UMRUN-SIBLING-1122-CLAIM-WINDOW-ORACLE-1: that bound cannot see a stall INSIDE the claim
        # window (claim_lo to the assignment), where an hour-back client pausing ~8 s still decides
        # >= timeout_sec - 0.5 after claim_lo. So a pass that rests on time inside the window is not
        # reported as a pass: judge_claim_wait() returns INCONCLUSIVE when the verdict is not also a
        # full budget after claim_hi, and the test skips at the very end (after every other check
        # below has run) rather than vouching for a client it could not tell from a broken one.
        # The FAIL branch above is unchanged and no wall-clock ceiling is added; claim_gap stays a
        # diagnostic. The verdict table is covered by UmRunClaimWaitOracleTests.
        self.assertIsNotNone(claim_lo, f"no claim bracket, so the wait locus is unknown [{timing_facts}]")
        self.assertGreaterEqual(
            verdict_time - claim_lo, timeout_sec - 0.5,
            f"the client's verdict came {verdict_time - claim_lo:.1f}s after the instant just before "
            f"it entered its claimed wait (claim_lo), inside its own {timeout_sec}s budget: the {decided_after:.1f}s "
            "measured from the marker stamp was spent before the wait began (scheduler or hook time), "
            f"not waiting [{timing_facts}]")
        # Whitespace-flattened, with "|" removed: PowerShell's own default uncaught-exception
        # formatting word-wraps a long message at a fixed console width, prefixing each
        # continuation line with a literal "| " margin -- both would otherwise split a long
        # asserted phrase across a line break, or embed a stray "|" mid-phrase, by coincidence of
        # length rather than content. None of this file's own asserted text ever contains "|".
        combined = " ".join(_ANSI_ESCAPE.sub("", (stdout or "") + (stderr or "")).replace("|", " ").split())
        self.assertNotIn("was never claimed", combined, combined)
        self.assertNotIn("RETRACTED", combined, combined)
        self.assertIn("UNRESOLVED:", combined, combined)
        self.assertIn("stopped proving liveness", combined, combined)
        self.assertIn("its own started marker is gone too", combined, combined)
        skip_if_claim_wait_inconclusive(self, claim_lo, claim_hi, verdict_time, timeout_sec, timing_facts)

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


class UmRunAdmissionWaitTests(_Share):
    """MLV-AGENT-HEAVY-SLOT-GATE-2: an agent with a heavy-slot admission gate holds a job before it
    launches it, and writes running\\<id>.started.json with phase=admission-wait (childPid null) before
    that wait. um-run.ps1 must treat that as neither unclaimed (never RETRACTED) nor claimed (the
    claimed clock starts only at phase=launched), wait under -MaxAdmissionWaitSec, and log the
    heartbeat's gate-wait reason. A marker with no phase field (any older agent) means launched."""

    def start(self, *extra: str) -> subprocess.Popen:
        return subprocess.Popen(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(UM_RUN),
             "-ScriptPath", str(self.job), "-AgentShare", str(self.share), "-PollSeconds", "1",
             "-JobId", "demo", *extra],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def wait_for_drop(self, proc: subprocess.Popen) -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not (self.inbox / "demo.job.ps1").exists():
            self.assertIsNone(proc.poll(), "um-run exited before its job was dropped")
            time.sleep(0.1)
        self.assertTrue((self.inbox / "demo.job.ps1").exists(), "the job was never dropped")

    def write_marker(self, body: str) -> None:
        running_dir = self.share / "running"
        running_dir.mkdir(parents=True, exist_ok=True)
        tmp = running_dir / "demo.started.json.tmp"
        tmp.write_text(body, encoding="ascii")
        tmp.replace(running_dir / "demo.started.json")   # atomic, as the agent's Write-JsonFileAtomic

    def admission_marker(self) -> str:
        return json.dumps({"schema": "mlvapp.agent.started.v1", "jobId": "demo", "phase": "admission-wait",
                           "status": "admission-wait", "childPid": None,
                           "gateReason": "waiting for heavy-slot admission"})

    def launched_marker(self) -> str:
        return json.dumps({"schema": "mlvapp.agent.started.v1", "jobId": "demo", "phase": "launched",
                           "status": "started", "childPid": 4242,
                           "startedUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})

    def gate_heartbeat(self, state: str, waited: int) -> None:
        # The shape agent r1i/r2 writes while it holds a job: "... gate-wait job=<id> state=<s> waited=<n>s".
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        (self.share / "heartbeat.txt").write_text(
            f'alive {now} pid=1 host=TESTHOST generation=1 processStartUtc={now} imagePath="x" agentScript="y" '
            f"gate-wait job=demo state={state} waited={waited}s", encoding="utf-8")

    def hold(self, proc: subprocess.Popen, seconds: float, state: str = "hot") -> None:
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds and proc.poll() is None:
            self.gate_heartbeat(state, int(time.monotonic() - t0))
            time.sleep(0.4)

    def publish_result(self) -> None:
        result = {"jobId": "demo", "exitCode": 0, "stdout": "ran after admission", "stderr": "",
                  "timeoutSec": 3, "timedOut": False}
        tmp = self.outbox / "demo.result.tmp"
        tmp.write_text(json.dumps(result), encoding="ascii")
        tmp.replace(self.outbox / "demo.result.json")

    def finish(self, proc: subprocess.Popen, timeout: float = 30) -> str:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        return " ".join(_ANSI_ESCAPE.sub("", (stdout or "") + (stderr or "")).replace("|", " ").split())

    def test_an_admission_wait_hold_past_every_ceiling_is_not_retracted_and_resolves_on_launch(self) -> None:
        # RED/GREEN guard. The hold (9 s) outlasts -MaxQueueWaitSec (2 s) AND -TimeoutSec +
        # -MaxClaimedWaitSec (3 + 3 s): a client that read the hold as unclaimed retracts it, and one
        # that read it as claimed stops UNRESOLVED at its outer ceiling. Only a client that starts the
        # claimed clock at phase=launched is still waiting, and then returns the receipt.
        proc = self.start("-TimeoutSec", "3", "-MaxClaimedWaitSec", "3", "-MaxQueueWaitSec", "2",
                          "-MaxAdmissionWaitSec", "120")
        try:
            self.wait_for_drop(proc)
            self.write_marker(self.admission_marker())
            self.hold(proc, 9.0)
            self.assertIsNone(proc.poll(), "a job held in the admission gate must outlive the queue and claimed ceilings")
            self.assertTrue((self.inbox / "demo.job.ps1").exists(), "a held job must never be withdrawn")
            self.write_marker(self.launched_marker())
            self.touch_heartbeat(job_id="demo")
            time.sleep(1.5)
            self.publish_result()
        except BaseException:
            proc.kill()
            raise
        combined = self.finish(proc)
        self.assertEqual(proc.returncode, 0, combined)
        self.assertNotIn("RETRACTED", combined, combined)
        self.assertNotIn("UNRESOLVED", combined, combined)
        self.assertIn("UMRUN_ADMISSION_WAIT job=demo state=hot", combined, combined)
        self.assertIn("marker=admission-wait", combined, combined)

    def test_the_admission_ceiling_stops_unresolved_and_never_withdraws_the_job(self) -> None:
        # Slots-full and unknown-feed holds are uncapped in the agent; -MaxAdmissionWaitSec bounds them
        # here. The outcome is UNRESOLVED (the agent still holds the job), never RETRACTED.
        #
        # UMRUN-E2E-TIMING-FLAKES-1: this used to rewrite heartbeat.txt every 0.4 s from the test
        # while the client read it. write_text truncates before it writes, so a read in between is
        # empty and the ceiling message says "last gate state=?" (PR #425 r5, run 38528323174). The
        # ceiling does not need a live heartbeat at all -- only the gate state it carries -- so it is
        # written ONCE, before the marker, and nothing writes it while the client runs. The first
        # poll is also held until the marker exists: with -MaxQueueWaitSec 1 the old test relied on
        # the test process writing the marker within a second of the drop, whatever the scheduler did.
        proc = self.start_ticked("-TimeoutSec", "60", "-MaxQueueWaitSec", "1", "-MaxAdmissionWaitSec", "3",
                                 hold_first_poll_until=self.share / "running" / "demo.started.json")
        try:
            self.wait_for_drop(proc)
            self.gate_heartbeat("slots-full", 0)
            self.write_marker(self.admission_marker())
        except BaseException:
            proc.kill()
            raise
        combined = self.finish(proc, timeout=120)
        self.assertNotEqual(proc.returncode, 0, combined)
        self.assertIn("UNRESOLVED:", combined, combined)
        self.assertIn("held unlaunched in the agent's heavy-slot admission gate", combined, combined)
        self.assertIn("MaxAdmissionWaitSec (3s)", combined, combined)
        self.assertIn("last gate state=slots-full", combined, combined)
        self.assertNotIn("RETRACTED", combined, combined)
        self.assertTrue((self.inbox / "demo.job.ps1").exists(), "the admission ceiling must not withdraw the job")

    def test_a_launching_marker_is_claimed_not_launched_and_resolves_on_launch(self) -> None:
        # Agent r2b writes phase=launching (childPid null) just before it creates the child. The client
        # reads it as its own state (logged marker=launching, never cannot-tell) and handles it exactly
        # like admission-wait: never retracted at the queue ceiling, and it never starts the claimed
        # clock, so a launching marker held past -TimeoutSec + -MaxClaimedWaitSec does not stop the
        # client at its outer ceiling. Once phase=launched (with a pid) lands, the receipt is returned.
        proc = self.start("-TimeoutSec", "3", "-MaxClaimedWaitSec", "3", "-MaxQueueWaitSec", "2",
                          "-MaxAdmissionWaitSec", "120")
        try:
            self.wait_for_drop(proc)
            self.write_marker(json.dumps({"schema": "mlvapp.agent.started.v1", "jobId": "demo",
                                          "phase": "launching", "status": "launching", "childPid": None}))
            self.hold(proc, 9.0, state="clear")
            self.assertIsNone(proc.poll(), "a launching marker must outlive the queue and claimed ceilings")
            self.assertTrue((self.inbox / "demo.job.ps1").exists(), "a launching job must never be withdrawn")
            self.write_marker(self.launched_marker())
            self.touch_heartbeat(job_id="demo")
            time.sleep(1.5)
            self.publish_result()
        except BaseException:
            proc.kill()
            raise
        combined = self.finish(proc)
        self.assertEqual(proc.returncode, 0, combined)
        self.assertNotIn("RETRACTED", combined, combined)
        self.assertNotIn("UNRESOLVED", combined, combined)
        self.assertIn("marker=launching", combined, combined)
        self.assertNotIn("marker=unknown", combined, combined)

    def test_a_marker_that_cannot_be_read_is_cannot_tell_never_retracted(self) -> None:
        # Cannot-tell is its own outcome: a present but unparseable marker past the queue ceiling is
        # never folded into "unclaimed". Once it reads as launched, the receipt is returned.
        proc = self.start("-TimeoutSec", "5", "-MaxQueueWaitSec", "2", "-MaxAdmissionWaitSec", "120")
        try:
            self.wait_for_drop(proc)
            self.write_marker('{"jobId": "demo", "phase": "admiss')   # torn
            self.hold(proc, 5.0)
            self.assertIsNone(proc.poll(), "an unreadable marker must not be retracted at the queue ceiling")
            self.assertTrue((self.inbox / "demo.job.ps1").exists(), "an unreadable marker must not be withdrawn")
            self.write_marker(self.launched_marker())
            self.touch_heartbeat(job_id="demo")
            self.publish_result()
        except BaseException:
            proc.kill()
            raise
        combined = self.finish(proc)
        self.assertEqual(proc.returncode, 0, combined)
        self.assertNotIn("RETRACTED", combined, combined)
        self.assertIn("marker=unknown", combined, combined)

    def test_falsifier_the_same_job_with_no_marker_is_retracted_at_the_queue_ceiling(self) -> None:
        # The same parameters and the same gate-wait heartbeat, with no marker (what agent r1i
        # presents): the heartbeat alone is not a claim, so the queue ceiling still retracts the job.
        proc = self.start("-TimeoutSec", "3", "-MaxClaimedWaitSec", "3", "-MaxQueueWaitSec", "2",
                          "-MaxAdmissionWaitSec", "120")
        try:
            self.wait_for_drop(proc)
            self.hold(proc, 20.0)
        except BaseException:
            proc.kill()
            raise
        combined = self.finish(proc)
        self.assertNotEqual(proc.returncode, 0, combined)
        self.assertIn("RETRACTED:", combined, combined)
        self.assertNotIn("UMRUN_ADMISSION_WAIT", combined, combined)
        self.assertEqual(self.names(), [], "a retracted job leaves nothing in the inbox")

    def test_a_marker_without_phase_is_launched_as_before(self) -> None:
        # Backward compatibility: a marker with no phase field (every agent before GATE-2) starts the
        # claimed clock at once, so a continuously fresh heartbeat still ends at the absolute outer
        # ceiling (-TimeoutSec + -MaxClaimedWaitSec), never in the admission wait.
        proc = self.start("-TimeoutSec", "1", "-MaxClaimedWaitSec", "2", "-MaxQueueWaitSec", "30",
                          "-MaxAdmissionWaitSec", "120")
        try:
            self.wait_for_drop(proc)
            self.write_marker(json.dumps({"jobId": "demo",
                                          "startedUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}))
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and proc.poll() is None:
                self.touch_heartbeat(job_id="demo")
                time.sleep(0.4)
        except BaseException:
            proc.kill()
            raise
        combined = self.finish(proc)
        self.assertNotEqual(proc.returncode, 0, combined)
        self.assertIn("UNRESOLVED:", combined, combined)
        self.assertIn("absolute outer ceiling", combined, combined)
        self.assertIn("MaxClaimedWaitSec (2s)", combined, combined)
        self.assertNotIn("UMRUN_ADMISSION_WAIT", combined, combined)
        self.assertNotIn("RETRACTED", combined, combined)


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
                "$n.Left.Extent.Text -in @('$submittedAt', '$claimedAt', '$admissionAt') -and "
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
        # MLV-AGENT-HEAVY-SLOT-GATE-2: +1 $claimedAt (launched seen at the admission ceiling) and
        # 3 $admissionAt roots (first admission-wait read, marker present at the queue deadline, and
        # the retraction-race fallback when the late marker is an admission-wait claim).
        self.assertEqual(result["count"], 8, result)
        types = result["types"] if isinstance(result["types"], list) else [result["types"]]
        for type_name in types:
            self.assertEqual(
                type_name, "DateTimeOffset",
                f"a wait-loop deadline root assignment constructs {type_name}, not DateTimeOffset "
                f"-- reintroducing the Kind-ambiguity a prior round had to remove: {result}",
            )


class UmRunClaimWaitOracleTests(unittest.TestCase):
    """UMRUN-SIBLING-1122-CLAIM-WINDOW-ORACLE-1 (Dot e3cb59d2 on #531 50a17a9c, hub ACK 6294165342).

    The client sets `$claimedAt` at an instant the test cannot see, only bracket: claim_lo is stamped
    just before it, claim_hi just after. An hour-back client that stalls ~8 s inside that window
    reaches its poll-2 verdict ~8 s after claim_lo, so `verdict - claim_lo` alone cannot tell it from a
    healthy client. These cases drive the verdict table with synthetic stamps (T = 8 s budget), so
    the claim does not depend on what a live scheduler happens to do, and none of them adds a
    wall-clock ceiling: a stamp pattern that cannot separate the two is INCONCLUSIVE, never FAIL."""

    T = 8

    def judge(self, claim_lo: float, claim_hi: float, verdict_time: float) -> str:
        return judge_claim_wait(claim_lo, claim_hi, verdict_time, self.T)

    def test_a_healthy_client_with_a_tight_claim_bracket_passes(self) -> None:
        self.assertEqual(self.judge(0.0, 0.005, 8.2), CLAIM_WAIT_PASS)

    def test_an_hour_back_client_that_decides_at_once_fails(self) -> None:
        self.assertEqual(self.judge(0.0, 0.005, 1.1), CLAIM_WAIT_FAIL)

    def test_an_hour_back_client_stalled_inside_the_claim_window_is_not_reported_as_a_pass(self) -> None:
        # Dot's masking case: claim_lo stamped, the client stalls 8 s, assigns an hour-back claimedAt,
        # claim_hi is stamped ~8 s after claim_lo, poll 2 decides ~1 s later. verdict - claim_lo
        # = 9.0 s clears the bound, yet no wait happened inside the claimed wait.
        self.assertEqual(self.judge(0.0, 8.0, 9.0), CLAIM_WAIT_INCONCLUSIVE)

    def test_a_healthy_client_stalled_after_assigning_claimed_at_is_inconclusive_not_failed(self) -> None:
        # The same stamps an hour-back client produces: claimedAt assigned at once, then an 8 s stall
        # before claim_hi. It decides at claimedAt + T, ~8.1 s after claim_lo. Stamps alone cannot
        # separate it from the case above, so neither may be judged either way.
        self.assertEqual(self.judge(0.0, 8.0, 8.1), CLAIM_WAIT_INCONCLUSIVE)

    def test_a_healthy_client_stalled_before_assigning_claimed_at_passes(self) -> None:
        # Stall inside the window, THEN a correct assignment: the budget is spent after claim_hi.
        self.assertEqual(self.judge(0.0, 8.0, 16.1), CLAIM_WAIT_PASS)

    def test_the_conclusive_bound_uses_the_same_half_second_slack_as_the_other_bounds(self) -> None:
        self.assertEqual(self.judge(0.0, 0.4, 8.0), CLAIM_WAIT_PASS)
        self.assertEqual(self.judge(0.0, 0.6, 8.0), CLAIM_WAIT_INCONCLUSIVE)

    def test_a_verdict_inside_the_budget_fails_whatever_the_bracket_width(self) -> None:
        self.assertEqual(self.judge(0.0, 8.0, 7.0), CLAIM_WAIT_FAIL)
        self.assertEqual(self.judge(0.0, 0.005, 7.0), CLAIM_WAIT_FAIL)

    # UMRUN-CLAIM-WINDOW-ORACLE-BOUNDARY-PINS-1 (Dot 89837a67 on #555 6a8c3b35, items 1+2): the cases
    # above sit NEAR the two T-minus-slack boundaries; these sit EXACTLY on them and one step either
    # side, to pin the two strict `<` comparisons in judge_claim_wait(). T = 8 and slack = 0.5, so
    # both boundaries are 7.5. Every stamp below (0.0, 0.5, 7.5, 8.0, 7.5 -/+ 1/64) is a dyadic
    # rational, exact in binary floating point, so each "exactly on the boundary" subtraction really
    # is equal to 7.5 rather than off by a rounding error.
    _STEP = 1 / 64

    def test_a_verdict_exactly_t_minus_slack_after_claim_lo_is_not_failed(self) -> None:
        # verdict - claim_lo == 7.5 == T - slack: strict `<` is False, so not FAIL. claim_hi == claim_lo
        # keeps the second comparison at 7.5 < 7.5, also False, so the outcome is PASS.
        self.assertEqual(self.judge(0.0, 0.0, 7.5), CLAIM_WAIT_PASS)

    def test_a_verdict_one_step_short_of_t_minus_slack_after_claim_lo_fails(self) -> None:
        self.assertEqual(self.judge(0.0, 0.0, 7.5 - self._STEP), CLAIM_WAIT_FAIL)

    def test_a_verdict_exactly_t_minus_slack_after_claim_hi_is_conclusive_not_inconclusive(self) -> None:
        # verdict - claim_hi == 7.5 == T - slack: strict `<` is False, so not INCONCLUSIVE. The first
        # comparison is 8.0 - 0.0 = 8.0 >= 7.5, so it does not fail either: PASS.
        self.assertEqual(self.judge(0.0, 0.5, 8.0), CLAIM_WAIT_PASS)

    def test_a_verdict_one_step_short_of_t_minus_slack_after_claim_hi_is_inconclusive(self) -> None:
        self.assertEqual(self.judge(0.0, 0.5 + self._STEP, 8.0), CLAIM_WAIT_INCONCLUSIVE)

    def test_both_boundaries_hold_when_they_coincide_at_a_shared_stamp_offset(self) -> None:
        # claim_lo == claim_hi == 1.0 and verdict 8.5: both differences are exactly 7.5.
        self.assertEqual(self.judge(1.0, 1.0, 8.5), CLAIM_WAIT_PASS)

    def test_the_slack_is_subtracted_from_the_budget_not_ignored(self) -> None:
        # With no slack the first bound would be 8.0 and a verdict at 7.5 would FAIL; the half
        # second is what makes it a PASS. Passing slack=0.0 explicitly shows the contrast.
        self.assertEqual(judge_claim_wait(0.0, 0.0, 7.5, self.T), CLAIM_WAIT_PASS)
        self.assertEqual(judge_claim_wait(0.0, 0.0, 7.5, self.T, slack=0.0), CLAIM_WAIT_FAIL)


class UmRunClaimWaitSkipWiringTests(unittest.TestCase):
    """UMRUN-CLAIM-WINDOW-ORACLE-BOUNDARY-PINS-1 (Dot 89837a67 on #555, item 2): an INCONCLUSIVE
    claim-window verdict must reach skipTest in the live wait test, which had only been exercised in
    review. skip_if_claim_wait_inconclusive() is the seam the live test now calls as its last step."""

    T = 8
    FACTS = "decided_after=9.000s timeout_sec=8"

    @staticmethod
    def _run(claim_lo: float, claim_hi: float, verdict_time: float) -> unittest.TestResult:
        class _Probe(unittest.TestCase):
            def runTest(self) -> None:
                skip_if_claim_wait_inconclusive(
                    self, claim_lo, claim_hi, verdict_time, UmRunClaimWaitSkipWiringTests.T,
                    UmRunClaimWaitSkipWiringTests.FACTS)

        result = unittest.TestResult()
        _Probe().run(result)
        return result

    def test_an_inconclusive_stamp_pattern_is_recorded_as_a_skip_with_the_inconclusive_reason(self) -> None:
        # Dot's masking case: judge_claim_wait() rules (0.0, 8.0, 9.0) INCONCLUSIVE.
        self.assertEqual(judge_claim_wait(0.0, 8.0, 9.0, self.T), CLAIM_WAIT_INCONCLUSIVE)
        result = self._run(0.0, 8.0, 9.0)
        self.assertEqual(len(result.skipped), 1, result.skipped)
        self.assertEqual((result.errors, result.failures), ([], []))
        reason = result.skipped[0][1]
        self.assertTrue(reason.startswith("INCONCLUSIVE:"), reason)
        self.assertTrue(reason.endswith(f"[{self.FACTS}]"), reason)

    def test_a_pass_stamp_pattern_does_not_skip(self) -> None:
        self.assertEqual(judge_claim_wait(0.0, 0.005, 8.2, self.T), CLAIM_WAIT_PASS)
        result = self._run(0.0, 0.005, 8.2)
        self.assertEqual((result.skipped, result.errors, result.failures), ([], [], []))
        self.assertEqual(result.testsRun, 1)

    def test_a_fail_stamp_pattern_does_not_skip(self) -> None:
        # The FAIL branch is asserted by the live test's own assertGreaterEqual before this step; the
        # seam must not turn it into a skip either.
        self.assertEqual(judge_claim_wait(0.0, 0.005, 1.1, self.T), CLAIM_WAIT_FAIL)
        result = self._run(0.0, 0.005, 1.1)
        self.assertEqual((result.skipped, result.errors, result.failures), ([], [], []))

    def test_the_live_wait_test_ends_by_calling_the_seam(self) -> None:
        import inspect
        source = inspect.getsource(UmRunEndToEndTests)
        calls = source.count(
            "skip_if_claim_wait_inconclusive(self, claim_lo, claim_hi, verdict_time, timeout_sec, timing_facts)")
        self.assertEqual(calls, 1, "the live wait test no longer routes its INCONCLUSIVE verdict through the seam")

    # UMRUN-SEAM-CALL-POSITION-PIN-1 (opus r1 on #592): the count above is blind to WHERE the call
    # sits; these pin it as the live wait test's final top-level statement.
    _SEAM_CALL = "skip_if_claim_wait_inconclusive(self, claim_lo, claim_hi, verdict_time, timeout_sec, timing_facts)"
    _SYNTH = (
        "class Live:\n"
        "    def test_it(self):\n"
        "{body}"
        "\n"
    )

    @classmethod
    def _synth(cls, *body: str) -> str:
        return cls._SYNTH.format(body="".join(f"        {line}\n" for line in body))

    @classmethod
    def _real_source(cls) -> str:
        return Path(__file__).read_text(encoding="utf-8")

    @classmethod
    def _real_source_with_call_moved_above(cls, marker: str) -> str:
        """The file's own source with the live wait test's seam call moved to just above the first
        line in that test containing `marker`."""
        source = cls._real_source()
        lines = source.splitlines()
        live = next(node for node in ast.walk(ast.parse(source))
                    if isinstance(node, ast.ClassDef) and node.name == "UmRunEndToEndTests")
        method = next(m for m in live.body if isinstance(m, ast.FunctionDef) and m.end_lineno is not None
                      and lines[m.end_lineno - 1].strip() == cls._SEAM_CALL)
        call_idx = method.end_lineno - 1
        target_idx = next(i for i in range(method.lineno - 1, call_idx) if marker in lines[i])
        lines.insert(target_idx, lines.pop(call_idx))
        return "\n".join(lines) + "\n"

    def test_the_live_wait_tests_final_statement_is_the_seam_call(self) -> None:
        self.assertIsNone(seam_call_position_problem(
            self._real_source(), "UmRunEndToEndTests", method_name=_SEAM_CALL_METHOD))

    # UMRUN-SIBLING-1122-SLACK-COMMENT-SAME-CLOCK-1 (Dot c91c2dda on #597, H1): bind the method name.
    def test_the_seam_call_moved_into_a_differently_named_method_is_rejected_when_the_name_is_bound(self) -> None:
        source = self._synth(self._SEAM_CALL).replace("def test_it(", "def test_other(")
        problem = seam_call_position_problem(source, "Live", method_name="test_it")
        self.assertIsNotNone(problem)
        self.assertIn("test_other", problem)
        self.assertIn("test_it", problem)

    def test_the_seam_call_in_the_renamed_real_live_wait_test_is_rejected_when_the_name_is_bound(self) -> None:
        mutated = self._real_source().replace(f"def {_SEAM_CALL_METHOD}(", "def test_renamed_live_wait(", 1)
        self.assertNotEqual(mutated, self._real_source())
        problem = seam_call_position_problem(mutated, "UmRunEndToEndTests", method_name=_SEAM_CALL_METHOD)
        self.assertIsNotNone(problem)
        self.assertIn("test_renamed_live_wait", problem)
        self.assertIn(_SEAM_CALL_METHOD, problem)

    def test_a_seam_call_in_the_bound_method_name_is_accepted(self) -> None:
        self.assertIsNone(seam_call_position_problem(self._synth(self._SEAM_CALL), "Live", method_name="test_it"))

    def test_the_seam_call_moved_above_the_unresolved_assert_is_rejected_though_the_count_still_passes(self) -> None:
        for marker in ('self.assertIn("UNRESOLVED:"', 'self.assertIn("its own started marker is gone too"'):
            with self.subTest(marker=marker):
                mutated = self._real_source_with_call_moved_above(marker)
                class_node = next(node for node in ast.parse(mutated).body
                                  if isinstance(node, ast.ClassDef) and node.name == "UmRunEndToEndTests")
                # The mutation is one the old count-only pin cannot see ...
                self.assertEqual(ast.get_source_segment(mutated, class_node).count(self._SEAM_CALL), 1)
                # ... and the position pin rejects it.
                problem = seam_call_position_problem(mutated, "UmRunEndToEndTests")
                self.assertIsNotNone(problem)
                self.assertIn("not the final top-level statement", problem)

    def test_a_seam_call_followed_by_any_further_statement_is_rejected(self) -> None:
        source = self._synth(self._SEAM_CALL, "self.assertTrue(True)")
        self.assertIn("not the final", seam_call_position_problem(source, "Live") or "")

    def test_a_seam_call_nested_in_a_branch_or_assigned_is_not_the_final_statement(self) -> None:
        for body in (("if claim_lo:", "    " + self._SEAM_CALL),
                     (f"outcome = {self._SEAM_CALL}",),
                     (f"return {self._SEAM_CALL}",)):
            with self.subTest(body=body):
                self.assertIsNotNone(seam_call_position_problem(self._synth(*body), "Live"))

    def test_a_missing_or_duplicated_seam_call_is_rejected(self) -> None:
        self.assertIn("found 0", seam_call_position_problem(self._synth("pass"), "Live") or "")
        self.assertIn("found 2", seam_call_position_problem(
            self._synth(self._SEAM_CALL, self._SEAM_CALL), "Live") or "")

    def test_a_seam_call_with_different_arguments_is_rejected(self) -> None:
        source = self._synth("skip_if_claim_wait_inconclusive(self, claim_hi, claim_lo, verdict_time, timeout_sec, timing_facts)")
        self.assertIn("unexpected arguments", seam_call_position_problem(source, "Live") or "")

    def test_a_final_seam_call_is_accepted(self) -> None:
        self.assertIsNone(seam_call_position_problem(self._synth("self.assertTrue(True)", self._SEAM_CALL), "Live"))


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run([GIT, *args], cwd=str(cwd), capture_output=True, text=True, check=True)


def _env_without_git() -> dict[str, str] | None:
    """A copy of os.environ with every PATH entry that carries git.exe removed, or None if a probe
    subprocess still finds git afterwards (some hosts resolve git through a mechanism PATH-editing
    alone cannot defeat, e.g. an app-execution alias) -- callers skip rather than false-fail then."""
    env = dict(os.environ)
    kept = [part for part in env.get("PATH", "").split(os.pathsep)
            if part and not (Path(part) / "git.exe").exists()]
    env["PATH"] = os.pathsep.join(kept)
    probe = subprocess.run(
        [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
         "if (Get-Command git -ErrorAction SilentlyContinue) { 'FOUND' } else { 'GONE' }"],
        capture_output=True, text=True, env=env,
    )
    if "GONE" not in probe.stdout:
        return None
    return env


@unittest.skipIf(PWSH is None, "pwsh is not on PATH")
@unittest.skipIf(GIT is None, "git is not on PATH")
@unittest.skipUnless(os.name == "nt", "um-run.ps1 targets Windows agent shares")
class UmRunFixtureContentPinTests(unittest.TestCase):
    """ATTR3-ADMIT-CONTENT-PIN-1 (fable key on PR #137): admission must pin the WORKING-TREE bytes
    to the committed blob, not merely a tracked name. Every repo here is a disposable, TEMPORARY git
    repository built under a scratch tempdir -- never the real tests/fixtures tree -- so a
    bytes-corrupting test can never touch a real fixture. The synthetic fixture uses the same
    ".umrunprobe" suffix the existing untracked-fixture test already uses (never a new
    media-extension literal), with a stem ("tiny_dual_iso") from the module's own admissible set.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="umrun-pin-")
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(os.path.realpath(self._tmp.name)) / "repo"
        self.repo.mkdir()
        _git(["init", "-q"], self.repo)
        _git(["config", "user.email", "umrun-pin-test@example.invalid"], self.repo)
        _git(["config", "user.name", "UmRun Pin Test"], self.repo)
        # fable, PR #140 r2d MINOR (same fix as _make_fixture_repo in
        # test_playback_attr_3_cuda_behaviour.py): this repo's own content-pin checks reuse the
        # identical git-blob comparison, so its correctness should not depend on the host's
        # unpinned global core.autocrlf either, even though this particular fixture's raw bytes
        # (below) carry no CRLF for it to normalise today.
        _git(["config", "core.autocrlf", "true"], self.repo)
        self.clips = self.repo / "tests" / "fixtures" / "clips"
        self.clips.mkdir(parents=True)
        self.fixture = self.clips / "tiny_dual_iso.umrunprobe"
        self.fixture.write_bytes(b"committed fixture bytes")
        _git(["add", "tests/fixtures/clips/tiny_dual_iso.umrunprobe"], self.repo)
        _git(["commit", "-q", "-m", "fixture"], self.repo)

    def probe(self, path: Path, *, repo_root: Path | None = None, env: dict[str, str] | None = None,
              module: Path | None = None) -> str:
        # $VerbosePreference (not just -Verbose on the outer call) so Write-Verbose inside the
        # nested Get-UmRunFixtureAdmission catch block surfaces regardless of exactly how deep
        # the call chain runs -- the underlying ATTR3_FIXTURE_* / UMRUN_FIXTURE_CONTENT_PIN_*
        # token, which Test-UmRunTrackedFixtureSource's boolean return would otherwise discard.
        script = self.repo.parent / f"probe-{abs(hash(str(path))) % 10**8}.ps1"
        script.write_text(
            "$VerbosePreference = 'Continue'\n"
            "Import-Module " + _q(module or MODULE) + " -Force\n"
            "Write-Output ('RESULT=' + (Test-UmRunTrackedFixtureSource -SourcePath " + _q(path) +
            " -RepoRoot " + _q(repo_root if repo_root is not None else self.repo) + " -Verbose))\n",
            encoding="utf-8",
        )
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script)],
                              capture_output=True, text=True, env=env)
        return proc.stdout + proc.stderr

    def test_identical_working_tree_bytes_are_admitted(self) -> None:
        self.assertIn("RESULT=True", self.probe(self.fixture))

    def test_altered_working_tree_bytes_are_refused(self) -> None:
        # THE DEFECT: the old name-plus-tracked check admitted this unconditionally.
        self.fixture.write_bytes(b"foreign bytes staged over the fixture")
        output = self.probe(self.fixture)
        self.assertIn("RESULT=False", output)
        self.assertIn("ATTR3_FIXTURE_WORKING_TREE_DIRTY", output, output)

    def test_an_untracked_fixture_shaped_file_is_refused(self) -> None:
        untracked = self.clips / "large_dual_iso.umrunprobe"
        untracked.write_bytes(b"never committed")
        output = self.probe(untracked)
        self.assertIn("RESULT=False", output)
        self.assertIn("ATTR3_FIXTURE_NOT_COMMITTED", output, output)

    def test_a_fixture_shaped_file_outside_any_repo_is_refused(self) -> None:
        # No `git init` anywhere under orphan_root: the directory shape and stem are admissible,
        # but there is no repository at all to hold a committed blob.
        orphan_root = Path(os.path.realpath(self._tmp.name)) / "orphan"
        orphan_clips = orphan_root / "tests" / "fixtures" / "clips"
        orphan_clips.mkdir(parents=True)
        orphan = orphan_clips / "tiny_dual_iso.umrunprobe"
        shutil.copy2(self.fixture, orphan)
        output = self.probe(orphan, repo_root=orphan_root)
        self.assertIn("RESULT=False", output)
        self.assertIn("ATTR3_FIXTURE_NOT_IN_A_REPO", output, output)

    def test_no_git_on_path_is_refused(self) -> None:
        env = _env_without_git()
        if env is None:
            self.skipTest("could not remove git from PATH in this environment")
        output = self.probe(self.fixture, env=env)
        self.assertIn("RESULT=False", output)
        self.assertIn("ATTR3_FIXTURE_GIT_UNAVAILABLE", output, output)

    def test_a_nested_repository_beneath_the_trusted_root_cannot_authorize_the_fixture(self) -> None:
        # sol, PR #140 r2 BLOCKER: without -RepoRoot pinned to the caller's trusted root, the old
        # code discovered the repository from the FIXTURE'S OWN DIRECTORY -- so a nested
        # repository committed under tests/fixtures/clips could authorize bytes the outer
        # repository's HEAD never held. This repository is disposable and separate from the one
        # setUp already built at self.repo; the outer repo's committed fixture bytes are
        # untouched, and a nested repo underneath is the only thing that changes.
        _git(["init", "-q"], self.clips)
        _git(["config", "user.email", "umrun-pin-test@example.invalid"], self.clips)
        _git(["config", "user.name", "UmRun Pin Test"], self.clips)
        self.fixture.write_bytes(b"foreign bytes authorized only by the nested repo")
        _git(["add", "tiny_dual_iso.umrunprobe"], self.clips)
        _git(["commit", "-q", "-m", "foreign"], self.clips)
        output = self.probe(self.fixture)
        self.assertIn("RESULT=False", output)
        self.assertIn("ATTR3_FIXTURE_FOREIGN_REPO", output, output)

    def test_a_missing_bachelor_module_is_refused_as_content_pin_unavailable(self) -> None:
        # Test-UmRunFixtureContentPin's own fail-closed branch: the bachelor module (which carries
        # Assert-AttrCudaFixtureCommittedBytes) is the one thing this whole content-pin admission
        # surface depends on being importable ON DEMAND. Earlier coverage
        # (test_a_bachelor_less_host_refuses_a_tracked_fixture_rather_than_admitting_it_unpinned,
        # UmRunDropModuleTests above) proves the END-TO-END placement outcome when the module is
        # absent, but folds the reason into Test-UmRunTrackedFixtureSource's boolean return and
        # never asserts the UMRUN_FIXTURE_CONTENT_PIN_UNAVAILABLE token itself -- this is the
        # fail-open regression guard for the whole content pin, so a future change that quietly
        # turned "module missing" into "admit unpinned" would need to change this exact string, not
        # merely leave the end-to-end refusal (which a different bug could equally produce)
        # looking unchanged.
        # Round 2d: this used to RENAME the real, tracked bachelor module in the live working
        # tree and restore it in a finally -- a hard-killed run left the checkout without it and
        # dirty. A COPY of UmRunDrop.psm1 with no bachelor/ next to it produces the identical
        # module-absent condition without ever touching the real tree; -RepoRoot (via probe's
        # default of self.repo) still pins the disposable git repo this test built in setUp.
        module_copy = self.repo.parent / "no-bachelor" / "UmRunDrop.psm1"
        module_copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(MODULE, module_copy)
        output = self.probe(self.fixture, module=module_copy)
        self.assertIn("RESULT=False", output)
        self.assertIn("UMRUN_FIXTURE_CONTENT_PIN_UNAVAILABLE", output, output)

    def test_a_present_but_unloadable_bachelor_module_is_indeterminate_not_untracked(self) -> None:
        # sol/fable, PR #140 r2e MAJOR: Import-Module used to run with no try/catch of its own, so
        # a PRESENT-but-broken module (corrupted file, a syntax error, a permission denial) let
        # PowerShell's own raw exception message propagate unwrapped -- its first whitespace token
        # is never one of $UmRunIndeterminateAdmissionTokens, so Get-UmRunFixtureAdmission's catch
        # classified this exact could-not-determine scenario as a DEFINITE refusal purely because
        # the message shape did not match, not because anything about the fixture's bytes failed.
        # A syntax error (not a missing file) forces Import-Module itself to fail, distinct from
        # test_a_missing_bachelor_module_is_refused_as_content_pin_unavailable above.
        module_copy = self.repo.parent / "broken-bachelor" / "UmRunDrop.psm1"
        module_copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(MODULE, module_copy)
        broken_bachelor_dir = module_copy.parent / "bachelor"
        broken_bachelor_dir.mkdir()
        (broken_bachelor_dir / "AttrCudaArtifacts.psm1").write_text(
            "function Assert-AttrCudaFixtureCommittedBytes { ( { }\n", encoding="utf-8"
        )
        output = self.probe(self.fixture, module=module_copy)
        self.assertIn("RESULT=False", output)
        self.assertIn("UMRUN_FIXTURE_CONTENT_PIN_UNAVAILABLE", output, output)
        self.assertIn("is present but failed to load", output, output)


@unittest.skipIf(PWSH is None, "pwsh is not on PATH")
class UmRunFixtureContentPinInternalReadMechanismTests(unittest.TestCase):
    """ATTR3-ADMIT-CONTENT-PIN-1 round 2d closed a DIFFERENT gap than
    UmRunFixtureContentPinRaceTests below: not the OUTER race between admission returning and a
    caller re-reading the source later (r2/r2c, closed by -PostAdmissionHook and tested there),
    but an INNER one -- inside Assert-AttrCudaFixtureCommittedBytes itself, between its own
    git-blob comparison and a caller's separate re-hash of the same mutable path for the pin it
    handed back. Round 2d closed it structurally: the fixture is read ONCE into an in-memory
    buffer, and BOTH the committed-blob comparison and the returned SHA256 pin are derived from
    that single buffer -- there is no second file read left for a swap to land in between.

    NO CONTENT-SWAP RED/GREEN TEST EXISTS FOR THIS SPECIFIC WINDOW, and none is added here --
    stated plainly rather than left to be inferred from an absence. -PostAdmissionHook (the seam
    UmRunFixtureContentPinRaceTests and attr3-stage-fixture-job.ps1's own race test use) fires
    AFTER Get-UmRunFixtureAdmission has ALREADY RETURNED, i.e. after this internal window has
    already opened, run and closed; it cannot inject anywhere near it. A hook placed deeper still
    would have to sit inside Assert-AttrCudaFixtureCommittedBytes's own file read (lines ~462-482
    of AttrCudaArtifacts.psm1 at the time of writing), and that read is itself held under a
    deny-write file handle for its whole duration -- a same-host writer cannot open the path for
    writing while the read is in progress (proven below), so there is no filesystem-level way to
    win a race against it. Injecting a swap would require overwriting the read buffer in another
    process's memory, which is not a thing a PowerShell test (or an attacker with ordinary
    filesystem access) can do. What closure this window actually rests on, and what IS tested
    here instead of a swap:
      (1) STRUCTURE -- the comparison and the pin are read directly off the source as coming from
          the identical buffer, since that is a static property of the code, not a runtime race
          outcome a test could flip by timing;
      (2) MECHANISM -- the deny-write guarantee the fix depends on: FileShare.Read really does
          block a concurrent writer for as long as the handle stays open, exercised with the
          IDENTICAL FileMode/FileAccess/FileShare triple the function opens its handle with.
    """

    def test_the_source_derives_both_hashes_from_one_read_buffer(self) -> None:
        text = ATTR_CUDA_MODULE.read_text(encoding="utf-8")
        # The single open call, deny-write, whose result is read into $bytes ONCE.
        self.assertIn(
            "[IO.File]::Open($full, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)",
            text,
        )
        # The committed-blob comparison consumes that buffer, not a fresh read of $full.
        self.assertIn("Get-AttrCudaGitBlobHashFromBytes -Bytes $bytes", text)
        # -Sha256Pin is computed from the SAME $bytes, not a second Get-FileHash of $full.
        self.assertIn("$sha256.ComputeHash($bytes)", text)
        self.assertNotRegex(text, r"Get-FileHash\s+-LiteralPath\s+\$full\b")

    def test_a_deny_write_handle_blocks_a_concurrent_writer_for_its_whole_lifetime(self) -> None:
        # The mechanism the fix depends on, proven with the EXACT FileMode/FileAccess/FileShare
        # triple Assert-AttrCudaFixtureCommittedBytes opens its read handle with.
        tmp = tempfile.TemporaryDirectory(prefix="umrun-denywrite-")
        self.addCleanup(tmp.cleanup)
        target = Path(os.path.realpath(tmp.name)) / "target.bin"
        target.write_bytes(b"original bytes")
        script = target.parent / "deny-write-probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"$stream = [IO.File]::Open({_q(target)}, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)\n"
            "try {\n"
            "  try {\n"
            f"    $w = [IO.File]::Open({_q(target)}, [IO.FileMode]::Open, [IO.FileAccess]::Write, [IO.FileShare]::None)\n"
            "    $w.Dispose()\n"
            "    Write-Output 'WRITE_UNEXPECTEDLY_SUCCEEDED'\n"
            "  } catch { Write-Output ('WRITE_DENIED: ' + $_.Exception.GetType().Name) }\n"
            "} finally { $stream.Dispose() }\n",
            encoding="utf-8",
        )
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script)],
                              capture_output=True, text=True)
        self.assertIn("WRITE_DENIED", proc.stdout, proc.stdout + proc.stderr)
        self.assertNotIn("WRITE_UNEXPECTEDLY_SUCCEEDED", proc.stdout)


@unittest.skipIf(PWSH is None, "pwsh is not on PATH")
@unittest.skipIf(GIT is None, "git is not on PATH")
@unittest.skipUnless(os.name == "nt", "um-run.ps1 targets Windows agent shares")
class UmRunFixtureContentPinRaceTests(unittest.TestCase):
    """sol, PR #140 r2 MAJOR: admission and placement used to be independent reads of the same
    source path -- Assert-UmRunSideFileName's content-pin check returned, then the source was
    re-read for its sha256 and for the copy, so bytes that changed in that gap were never bound
    to the blob admission actually verified. A disposable git repository (never the real
    tests/fixtures/clips tree) plus Invoke-UmRunDrop's -PostAdmissionHook test seam -- which
    fires in exactly that gap and is never set in production -- exercises the race directly.

    THIS IS THE OUTER RACE, NOT ROUND 2D'S. -PostAdmissionHook fires after
    Get-UmRunFixtureAdmission has ALREADY RETURNED -- i.e. after admission's own internal read is
    long finished -- so nothing here exercises (or could ever exercise) the gap round 2d closed
    inside Assert-AttrCudaFixtureCommittedBytes itself. See
    UmRunFixtureContentPinInternalReadMechanismTests below for what round 2d actually closed and
    why a content-swap RED/GREEN test cannot be built for that window specifically."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="umrun-race-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(os.path.realpath(self._tmp.name))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        _git(["init", "-q"], self.repo)
        _git(["config", "user.email", "umrun-race-test@example.invalid"], self.repo)
        _git(["config", "user.name", "UmRun Race Test"], self.repo)
        # fable, PR #140 r2d MINOR: see the identical pin in UmRunFixtureContentPinTests.setUp.
        _git(["config", "core.autocrlf", "true"], self.repo)
        self.clips = self.repo / "tests" / "fixtures" / "clips"
        self.clips.mkdir(parents=True)
        self.fixture = self.clips / "tiny_dual_iso.umrunprobe"
        self.fixture.write_bytes(b"committed fixture bytes")
        _git(["add", "tests/fixtures/clips/tiny_dual_iso.umrunprobe"], self.repo)
        _git(["commit", "-q", "-m", "fixture"], self.repo)

        self.share = self.tmp / "agent"
        self.inbox = self.share / "inbox"
        self.outbox = self.share / "outbox"
        self.inbox.mkdir(parents=True)
        self.outbox.mkdir()
        self.job = self.tmp / "demo.job.ps1"
        self.job.write_text("Write-Output 'hi'\n", encoding="utf-8")

    def names(self) -> list[str]:
        return sorted(p.name for p in self.inbox.iterdir())

    def drop(self, hook: str) -> subprocess.CompletedProcess:
        script = self.tmp / "race-drop.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module {_q(MODULE)} -Force\n"
            "try {\n"
            f"  Invoke-UmRunDrop -Inbox {_q(self.inbox)} -Outbox {_q(self.outbox)} -ScriptPath {_q(self.job)} "
            f"-JobId 'demo' -SideFile @({_q(self.fixture)}) -RepoRoot {_q(self.repo)} "
            f"-PostAdmissionHook {hook}\n"
            "} catch { Write-Output ('THREW ' + $_.Exception.Message) }\n",
            encoding="utf-8",
        )
        return subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script)],
                              capture_output=True, text=True)

    def test_a_fixture_swapped_after_admission_but_before_placement_is_refused(self) -> None:
        hook = "{ param($p) [IO.File]::WriteAllBytes($p, [Text.Encoding]::UTF8.GetBytes('raced bytes')) }"
        proc = self.drop(hook)
        self.assertIn("THREW UMRUN_FIXTURE_CONTENT_PIN_RACE", proc.stdout, proc.stdout + proc.stderr)
        self.assertEqual(self.names(), [], "bytes swapped after admission must never reach the inbox")

    def test_a_no_op_hook_still_admits_the_clean_fixture(self) -> None:
        # Positive control: the new check must not false-positive when nothing raced.
        proc = self.drop("{ param($p) }")
        self.assertIn("UMRUN_JOBID=demo", proc.stdout, proc.stdout + proc.stderr)
        self.assertEqual(self.names(), sorted(["demo.job.ps1", "demo.meta.json", "tiny_dual_iso.umrunprobe"]))
        self.assertEqual(
            hashlib.sha256((self.inbox / "tiny_dual_iso.umrunprobe").read_bytes()).hexdigest(),
            hashlib.sha256(self.fixture.read_bytes()).hexdigest(),
        )


if __name__ == "__main__":
    unittest.main()
