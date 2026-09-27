"""ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 2 (fable/sol major 2): the tracked
tools/profiling/ultra-magnus-agent.ps1 must fall back to its own -JobTimeoutSec default when a
job's inbox\\<id>.meta.json is either absent or unparseable -- the same fall-back rule the deployed,
untracked \\\\bachelor\\mlv-agent copy already implements. The "metadata present and honoured" case
is covered end to end (real agent + real um-run.ps1) by tools/testing/test-ultra-magnus-agent-
timeout.ps1; this file covers the two fallback paths against the same REAL, locally-run agent.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AGENT_SCRIPT = ROOT / "tools" / "profiling" / "ultra-magnus-agent.ps1"
UM_RUN = ROOT / "tools" / "profiling" / "um-run.ps1"
PWSH = shutil.which("pwsh")


@unittest.skipIf(PWSH is None, "pwsh is not on PATH")
@unittest.skipUnless(os.name == "nt", "targets a Windows agent share")
class UltraMagnusAgentMetadataFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="umagent-meta-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(os.path.realpath(self._tmp.name))
        self.share = self.tmp / "agent"
        self.share.mkdir()
        # A small, fixed -JobTimeoutSec default that is easy to distinguish in wall-clock time from
        # both "no timeout at all" and from a job that finishes normally.
        self.agent_proc = subprocess.Popen(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(AGENT_SCRIPT),
             "-Root", str(self.share), "-PollSeconds", "1", "-JobTimeoutSec", "2"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self._stop_agent)
        heartbeat = self.share / "heartbeat.txt"
        deadline = time.time() + 20
        while time.time() < deadline and not heartbeat.exists():
            time.sleep(0.2)
        if not heartbeat.exists():
            self.skipTest("local ultra-magnus-agent.ps1 double did not start in time")

        self.local = self.tmp / "local"
        self.local.mkdir()
        # Sleeps far longer than the agent's 2s default, so a kill at ~2s proves the default was
        # used (as opposed to running to completion, which would prove nothing about timeouts).
        self.job = self.local / "sleepy.job.ps1"
        self.job.write_text("Start-Sleep -Seconds 30\n", encoding="utf-8")

    def _stop_agent(self) -> None:
        try:
            self.agent_proc.terminate()
            self.agent_proc.wait(timeout=10)
        except Exception:
            try:
                self.agent_proc.kill()
            except Exception:
                pass

    def _submit(self, *extra: str, timeout_sec: str = "1") -> subprocess.CompletedProcess:
        return subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(UM_RUN),
             "-ScriptPath", str(self.job), "-AgentShare", str(self.share),
             "-TimeoutSec", timeout_sec, "-PollSeconds", "1", "-MaxQueueWaitSec", "30", *extra],
            capture_output=True, text=True,
        )

    def test_missing_metadata_falls_back_to_the_agents_own_default(self) -> None:
        # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 10: UmRunDrop.psm1 now claims
        # inbox\<id>.meta.json for EVERY submission, even with no budget (JobTimeoutSec 0 just
        # omits `timeoutSec` -- see test_metadata_is_still_claimed_when_no_budget_is_requested_but_
        # omits_timeoutsec in test_um_run_sidefiles.py), so a normal drop can no longer produce
        # "genuinely no metadata file at all". This still exercises the AGENT's OWN missing-
        # metadata fallback (Get-JobMetadata's `missing` branch) directly and race-free, the same
        # way its unparseable-metadata sibling below already does: write ONLY the job file to the
        # inbox, bypassing UmRunDrop.psm1 (and its claim) entirely.
        (self.share / "inbox").mkdir(parents=True, exist_ok=True)
        (self.share / "inbox" / "demo.job.ps1").write_bytes(self.job.read_bytes())
        self.assertFalse((self.share / "inbox" / "demo.meta.json").exists(),
                          "this scenario is only meaningful with no metadata present")

        result_path = self.share / "outbox" / "demo.result.json"
        deadline = time.time() + 20
        while time.time() < deadline and not result_path.exists():
            time.sleep(0.2)
        self.assertTrue(result_path.exists(), "the agent never published a result")
        result = json.loads(result_path.read_text(encoding="ascii"))
        self.assertEqual(result["timeoutSec"], 2, "must fall back to the agent's own -JobTimeoutSec")
        self.assertTrue(result["timedOut"])
        self.assertEqual(result["exitCode"], 124)

    def test_unparseable_metadata_falls_back_to_the_agents_own_default(self) -> None:
        # fable minor 2 territory: a torn/corrupt meta.json must degrade to the agent's default, not
        # abort the job or silently hang. Written directly (bypassing UmRunDrop.psm1's own write-time
        # verification, which this test intentionally defeats) to exercise the AGENT's read-side
        # tolerance in isolation.
        inbox = self.share / "inbox"
        (inbox / "demo.meta.json").write_text("{not valid json", encoding="ascii")
        (inbox / "demo.job.ps1").write_bytes(self.job.read_bytes())

        result_path = self.share / "outbox" / "demo.result.json"
        deadline = time.time() + 20
        while time.time() < deadline and not result_path.exists():
            time.sleep(0.2)
        self.assertTrue(result_path.exists(), "the agent never published a result for a job with unparseable metadata")
        result = json.loads(result_path.read_text(encoding="ascii"))
        self.assertEqual(result["timeoutSec"], 2, "must fall back to the agent's own -JobTimeoutSec")
        self.assertTrue(result["timedOut"])

        # The agent publishes outbox\demo.result.json (the poll above) BEFORE it archives
        # inbox\demo.job.ps1 and inbox\demo.meta.json to processed\ (ultra-magnus-agent.ps1,
        # deliberately: the result publish is what retires the JobId in UmRunDrop.psm1's
        # outbox-result check, so it must land first, before either file moves) -- so the archival
        # is a few more of the agent's own instructions past the instant result.json becomes
        # visible, not simultaneous with it. Give it the same bounded settle time every other
        # file-visibility check in this module gets, rather than asserting the exact instant
        # result.json appears.
        meta_processed = self.share / "processed" / "demo.meta.json"
        deadline = time.time() + 5
        while time.time() < deadline and (inbox / "demo.meta.json").exists() and not meta_processed.exists():
            time.sleep(0.1)
        self.assertFalse((inbox / "demo.meta.json").exists(), "the unparseable metadata must still be moved out of inbox")
        self.assertTrue(meta_processed.exists())

    def test_a_full_budget_job_is_not_misdiagnosed_as_queued_against_the_tracked_agent(self) -> None:
        # sol round 6 major 1: the tracked agent used to emit no claim marker at all, while both
        # production wrapper call sites (attr3-footage-stage.ps1) set -MaxQueueWaitSec EQUAL to
        # -TimeoutSec -- the FULL-BUDGET configuration itself, not a generously larger toy queue
        # ceiling that would hide the race. Without a marker, um-run.ps1's client never leaves its
        # submission-anchored QUEUED phase, so its deadline (submission + MaxQueueWaitSec) equalled
        # the agent's own execution budget measured from submission -- but the agent's real deadline
        # starts at CLAIM, strictly after submission, so the client could throw before the agent's
        # later timeout receipt was ever published. This submits a job that legitimately consumes
        # its entire requested budget (the agent kills it, exactly the receipt that must survive to
        # be read) and proves the client survives long enough to read it, matching sol's repro
        # exactly: TimeoutSec == MaxQueueWaitSec, a job that outlives that budget.
        budget = 6
        job_id = "budget-consuming"
        job = self.local / "budget-consuming.job.ps1"
        job.write_text("Start-Sleep -Seconds 30\n", encoding="utf-8")
        script = self.tmp / "full-budget-submit.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            "try {\n"
            f"  $r = & '{UM_RUN}' -ScriptPath '{job}' -JobId '{job_id}' -AgentShare '{self.share}' "
            f"-TimeoutSec {budget} -MaxQueueWaitSec {budget} -PollSeconds 1\n"
            "  Write-Output ('E2E_RESULT=OK EXIT=' + $r.exitCode + ' TIMEDOUT=' + $r.timedOut)\n"
            "} catch {\n"
            "  Write-Output ('E2E_RESULT=THREW ' + $_.Exception.Message)\n"
            "}\n",
            encoding="utf-8",
        )
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script)],
                               capture_output=True, text=True)
        combined = proc.stdout + proc.stderr
        self.assertNotIn("E2E_RESULT=THREW", combined,
                          "the client misdiagnosed a claimed-but-still-running job as never claimed: " + combined)
        self.assertIn("E2E_RESULT=OK EXIT=124 TIMEDOUT=True", combined, combined)

        # The claim marker's lifetime must match the job's: still present is fine mid-run, but it
        # must never survive past the job it governed (a stale marker would misdate a later retry
        # that reuses this job id as already claimed at THIS run's claim time).
        started_marker = self.share / "running" / f"{job_id}.started.json"
        deadline = time.time() + 5
        while time.time() < deadline and started_marker.exists():
            time.sleep(0.1)
        self.assertFalse(started_marker.exists(), "the claim marker must be removed once the job's result is published")

    def test_a_stale_agent_enumeration_can_still_publish_a_late_launch_failure_receipt_for_a_retracted_job(self) -> None:
        # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 12 (item 1; sol BLOCKER / fable MINOR): the
        # tracked agent lists its inbox once per poll (Get-ChildItem, this file's own agent script
        # at its own top-level while loop) and processes every job in that ONE snapshot
        # sequentially -- so a job queued behind another can still be in the agent's own in-memory
        # list minutes after the client has already renamed it out of the inbox and reported
        # RETRACTED. No execution of the withdrawn job's own script BODY is possible (the agent's
        # launch step opens the file BY PATH, and the path is already gone), but the agent still
        # writes a claim marker for it and can publish an honest launch-failure receipt afterward.
        #
        # This test stops setUp's own default agent and starts a fresh one only once BOTH jobs are
        # already sitting in the inbox together, so the agent's very first Get-ChildItem
        # deterministically captures both in one snapshot -- no wall-clock race with the agent's own
        # poll timing is needed to reproduce the interleaving.
        self._stop_agent()
        inbox = self.share / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        # Sorts before "demo" (Sort-Object Name) so the agent's single snapshot processes it FIRST,
        # giving the client time to retract "demo" while the agent is still busy with this one.
        blocker = inbox / "aaa-blocker.job.ps1"
        blocker.write_text("Start-Sleep -Seconds 5\n", encoding="utf-8")

        demo_content_job = self.local / "demo-content.job.ps1"
        demo_content_job.write_text("Write-Output 'hi'\n", encoding="utf-8")
        submit_proc = subprocess.Popen(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(UM_RUN),
             "-ScriptPath", str(demo_content_job), "-AgentShare", str(self.share),
             "-JobId", "demo", "-TimeoutSec", "5", "-PollSeconds", "1", "-MaxQueueWaitSec", "2"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            demo_job_path = inbox / "demo.job.ps1"
            deadline = time.time() + 15
            while time.time() < deadline and not demo_job_path.exists():
                time.sleep(0.1)
            self.assertTrue(demo_job_path.exists(), "um-run.ps1 never published demo.job.ps1 to the inbox")

            # Both jobs are now sitting in the inbox together -- start a fresh agent so its very
            # first Get-ChildItem enumerates both in the SAME snapshot.
            self.agent_proc = subprocess.Popen(
                [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(AGENT_SCRIPT),
                 "-Root", str(self.share), "-PollSeconds", "1", "-JobTimeoutSec", "2"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

            submit_stdout, submit_stderr = submit_proc.communicate(timeout=30)
        finally:
            if submit_proc.poll() is None:
                submit_proc.kill()
                submit_proc.communicate()
        submit_combined = (submit_stdout or "") + (submit_stderr or "")
        self.assertIn("RETRACTED:", submit_combined, submit_combined)
        self.assertFalse(demo_job_path.exists(), "a genuinely retracted job's own file must be gone")
        self.assertFalse((inbox / "demo.meta.json").exists(), "retraction must clean up this submission's own metadata")

        # The agent is still busy with "aaa-blocker" (up to ~5s) -- once it finishes, it proceeds to
        # "demo" from its OWN earlier snapshot and tries to launch a script that is no longer there.
        result_path = self.share / "outbox" / "demo.result.json"
        deadline = time.time() + 25
        while time.time() < deadline and not result_path.exists():
            time.sleep(0.2)
        self.assertTrue(
            result_path.exists(),
            "the agent's own stale enumeration must still publish a receipt for the withdrawn id",
        )
        result = json.loads(result_path.read_text(encoding="ascii"))
        self.assertNotEqual(result.get("exitCode"), 0, result)
        # The sharpest assertion: the withdrawn job's own script body ("Write-Output 'hi'") never
        # actually ran -- only a launch attempt against its now-missing path did.
        self.assertNotIn("hi", result.get("stdout") or "", result)


if __name__ == "__main__":
    unittest.main()
