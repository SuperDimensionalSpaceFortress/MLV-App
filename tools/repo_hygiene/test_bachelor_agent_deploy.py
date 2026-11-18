"""BACHELOR-AGENT-DEPLOY-TRACKED-1 r1: tools/profiling/bachelor/Deploy-BachelorAgent.ps1 copies a merged
ultra-magnus-agent.ps1 into the agent root on Bachelor, verifies its sha256 there, and restarts the agent only when idle.

Every case runs the real script against a FAKE venue root in a temp dir (inbox\\, running\\, slot-holders\\ ...). Nothing here
touches \\\\bachelor, a share, or a real task: the restart is proved through the script's -RestartScript seam, and the one
sha-mismatch case through its -TestCorruptAfterWrite seam.

Typed lines and exit codes are the contract (the hub parses them): DEPLOY_NOOP 0, DEPLOY_OK 0, DEPLOY_REFUSED_BUSY 10,
DEPLOY_REFUSED_LEASE 11, DEPLOY_REFUSED_UNREADABLE 12, DEPLOY_SHA_MISMATCH 13, DEPLOY_RESTART_FAILED 14, source missing 2.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "profiling" / "bachelor" / "Deploy-BachelorAgent.ps1"
AGENT_NAME = "ultra-magnus-agent.ps1"
PWSH = shutil.which("pwsh")

OLD = b"# old agent\r\nWrite-Output 'old'\r\n"
NEW = b"# new agent\r\nWrite-Output 'new'\r\n"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def psq(value: object) -> str:
    """Body of a single-quoted PowerShell literal: a path with an apostrophe (C:\\O'Brien\\Temp) must not break out of it."""
    return str(value).replace("'", "''")


@unittest.skipUnless(PWSH, "pwsh (PowerShell 7) is required")
class DeployBachelorAgent(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="deploy-agent-test-")
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.venue = base / "venue"
        for d in ("inbox", "running", "outbox", "processed", "slot-holders", "logs"):
            (self.venue / d).mkdir(parents=True)
        (self.venue / AGENT_NAME).write_bytes(OLD)
        self.source = base / "source" / AGENT_NAME
        self.source.parent.mkdir()
        self.source.write_bytes(NEW)
        self.marker = base / "restart-ran.txt"
        self.restart = base / "restart.ps1"
        self.restart.write_text(
            "param([string]$VenueRoot)\r\nSet-Content -LiteralPath '%s' -Value $VenueRoot\r\n" % psq(self.marker),
            encoding="ascii",
        )

    def run_deploy(self, *extra: str, venue: Path | None = None, source: Path | None = None):
        cmd = [
            PWSH, "-NoProfile", "-NonInteractive", "-File", str(SCRIPT),
            "-VenueRoot", str(venue if venue is not None else self.venue),
            "-Source", str(source if source is not None else self.source),
            *extra,
        ]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    def rollbacks(self) -> list[Path]:
        return sorted(self.venue.glob("rollback-*"))

    def assert_untouched(self) -> None:
        self.assertEqual((self.venue / AGENT_NAME).read_bytes(), OLD, "a refusal leaves the venue copy alone")
        self.assertEqual(self.rollbacks(), [], "a refusal makes no rollback folder")
        self.assertEqual(list(self.venue.glob("*.tmp")), [], "a refusal leaves no temp file")
        self.assertFalse(self.marker.exists(), "a refusal never restarts")

    def test_script_is_ascii_only(self) -> None:
        self.assertTrue(SCRIPT.exists(), "the tracked deploy script exists")
        SCRIPT.read_bytes().decode("ascii")

    def test_noop_when_sha_equal(self) -> None:
        (self.venue / AGENT_NAME).write_bytes(NEW)
        before = (self.venue / AGENT_NAME).stat().st_mtime_ns
        r = self.run_deploy()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("DEPLOY_NOOP sha=%s" % sha(NEW), r.stdout)
        self.assertEqual((self.venue / AGENT_NAME).stat().st_mtime_ns, before, "a no-op does not rewrite the file")
        self.assertEqual(self.rollbacks(), [])

    def test_noop_even_when_busy(self) -> None:
        # Nothing would change, so nothing can be disturbed: equal sha wins over a busy venue.
        (self.venue / AGENT_NAME).write_bytes(NEW)
        (self.venue / "inbox" / "j1.job.ps1").write_text("x")
        r = self.run_deploy()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("DEPLOY_NOOP", r.stdout)

    def test_deploy_verifies_sha_and_keeps_rollback(self) -> None:
        r = self.run_deploy()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.venue / AGENT_NAME).read_bytes(), NEW)
        rb = self.rollbacks()
        self.assertEqual(len(rb), 1, "exactly one rollback folder")
        self.assertRegex(rb[0].name, r"^rollback-\d{8}T\d{6}Z$")
        self.assertEqual((rb[0] / AGENT_NAME).read_bytes(), OLD, "the previous agent is kept byte for byte")
        self.assertEqual(list(self.venue.glob("*.tmp")), [], "temp-and-rename leaves no temp file")
        m = re.search(r"^DEPLOY_OK sha=([0-9A-F]{64}) rollback=(.+) restarted=no$", r.stdout, re.M)
        self.assertIsNotNone(m, r.stdout)
        self.assertEqual(m.group(1), sha(NEW))
        self.assertEqual(Path(m.group(2).strip()), rb[0] / AGENT_NAME)
        self.assertFalse(self.marker.exists(), "-RestartMode none (the default) never restarts")

    def test_second_run_after_deploy_is_a_noop(self) -> None:
        self.assertEqual(self.run_deploy().returncode, 0)
        r = self.run_deploy()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("DEPLOY_NOOP", r.stdout)
        self.assertEqual(len(self.rollbacks()), 1, "idempotent: the second run makes no second rollback")

    def test_first_deploy_with_no_venue_copy_has_no_rollback(self) -> None:
        (self.venue / AGENT_NAME).unlink()
        r = self.run_deploy()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.venue / AGENT_NAME).read_bytes(), NEW)
        self.assertIn("rollback=none restarted=no", r.stdout)

    def test_refuse_busy_running(self) -> None:
        (self.venue / "running" / "j1.started.json").write_text("{}")
        r = self.run_deploy()
        self.assertEqual(r.returncode, 10, r.stdout + r.stderr)
        self.assertIn("DEPLOY_REFUSED_BUSY running=1 inbox=0", r.stdout)
        self.assert_untouched()

    def test_refuse_busy_inbox(self) -> None:
        (self.venue / "inbox" / "j2.job.ps1").write_text("x")
        (self.venue / "inbox" / "j2.meta.json").write_text("{}")
        r = self.run_deploy()
        self.assertEqual(r.returncode, 10, r.stdout + r.stderr)
        self.assertIn("DEPLOY_REFUSED_BUSY running=0 inbox=2", r.stdout)
        self.assert_untouched()

    def test_refuse_lease_held(self) -> None:
        (self.venue / "slot-holders" / "lb-abc.lease.json").write_text("{}")
        r = self.run_deploy()
        self.assertEqual(r.returncode, 11, r.stdout + r.stderr)
        self.assertIn("DEPLOY_REFUSED_LEASE lb-abc.lease.json", r.stdout)
        self.assert_untouched()

    def test_refuse_slot_holder_record(self) -> None:
        (self.venue / "slot-holders" / "j9.holder.json").write_text("{}")
        r = self.run_deploy()
        self.assertEqual(r.returncode, 11, r.stdout + r.stderr)
        self.assertIn("DEPLOY_REFUSED_LEASE j9.holder.json", r.stdout)
        self.assert_untouched()

    def test_refuse_unreadable_root_missing(self) -> None:
        r = self.run_deploy(venue=Path(self._tmp.name) / "no-such-root")
        self.assertEqual(r.returncode, 12, r.stdout + r.stderr)
        self.assertIn("DEPLOY_REFUSED_UNREADABLE", r.stdout)

    def test_refuse_unreadable_root_without_inbox(self) -> None:
        # A root that does not look like an agent root cannot be judged idle: every doubt refuses.
        shutil.rmtree(self.venue / "inbox")
        r = self.run_deploy()
        self.assertEqual(r.returncode, 12, r.stdout + r.stderr)
        self.assertIn("DEPLOY_REFUSED_UNREADABLE", r.stdout)
        self.assertEqual((self.venue / AGENT_NAME).read_bytes(), OLD)
        self.assertEqual(self.rollbacks(), [])

    def test_refuse_unreadable_root_is_a_file(self) -> None:
        f = Path(self._tmp.name) / "a-file"
        f.write_text("x")
        r = self.run_deploy(venue=f)
        self.assertEqual(r.returncode, 12, r.stdout + r.stderr)
        self.assertIn("DEPLOY_REFUSED_UNREADABLE", r.stdout)

    def test_missing_source_is_exit_2(self) -> None:
        r = self.run_deploy(source=Path(self._tmp.name) / "nope.ps1")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("DEPLOY_SOURCE_MISSING", r.stdout)
        self.assert_untouched()

    def test_sha_mismatch_restores_rollback(self) -> None:
        r = self.run_deploy("-TestCorruptAfterWrite")
        self.assertEqual(r.returncode, 13, r.stdout + r.stderr)
        self.assertIn("DEPLOY_SHA_MISMATCH", r.stdout)
        self.assertEqual((self.venue / AGENT_NAME).read_bytes(), OLD, "the rollback copy is put back")
        self.assertEqual(list(self.venue.glob("*.tmp")), [])
        self.assertNotIn("DEPLOY_OK", r.stdout)
        self.assertFalse(self.marker.exists(), "a mismatch never restarts")

    def test_dry_run_changes_nothing(self) -> None:
        r = self.run_deploy("-DryRun", "-RestartMode", "when-idle", "-RestartScript", str(self.restart))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("DEPLOY_DRYRUN action=deploy sha=%s" % sha(NEW), r.stdout)
        self.assert_untouched()

    def test_dry_run_still_reports_a_refusal(self) -> None:
        (self.venue / "running" / "j1.started.json").write_text("{}")
        r = self.run_deploy("-DryRun")
        self.assertEqual(r.returncode, 10, r.stdout + r.stderr)
        self.assertIn("DEPLOY_REFUSED_BUSY", r.stdout)

    def test_restart_when_idle_runs_the_restart_script(self) -> None:
        r = self.run_deploy("-RestartMode", "when-idle", "-RestartScript", str(self.restart))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"DEPLOY_OK sha=[0-9A-F]{64} rollback=.+ restarted=yes")
        self.assertTrue(self.marker.exists(), "the restart script ran")
        self.assertEqual(self.marker.read_text().strip(), str(self.venue))

    def test_restart_not_run_when_busy(self) -> None:
        (self.venue / "running" / "j1.started.json").write_text("{}")
        r = self.run_deploy("-RestartMode", "when-idle", "-RestartScript", str(self.restart))
        self.assertEqual(r.returncode, 10, r.stdout + r.stderr)
        self.assert_untouched()

    def test_restart_script_failure_is_typed(self) -> None:
        bad = Path(self._tmp.name) / "bad-restart.ps1"
        bad.write_text("param([string]$VenueRoot)\r\nexit 7\r\n", encoding="ascii")
        r = self.run_deploy("-RestartMode", "when-idle", "-RestartScript", str(bad))
        self.assertEqual(r.returncode, 14, r.stdout + r.stderr)
        self.assertIn("DEPLOY_RESTART_FAILED", r.stdout)
        self.assertEqual((self.venue / AGENT_NAME).read_bytes(), NEW, "the verified deploy stays; only the restart failed")

    def test_when_idle_without_restart_script_refuses_off_the_agent_host(self) -> None:
        # No -RestartScript means the real schtasks path, which acts on THIS machine's task list. The heartbeat names another host,
        # so the script refuses BEFORE changing anything (and so never reaches schtasks in this test).
        (self.venue / "heartbeat.txt").write_text(
            "alive 2026-11-18T10:00:00.0000000-06:00 pid=4242 host=SOME-OTHER-HOST generation=1 processStartUtc=2026-11-18T09:00:00.0000000Z\r\n"
        )
        r = self.run_deploy("-RestartMode", "when-idle")
        self.assertEqual(r.returncode, 16, r.stdout + r.stderr)
        self.assertIn("DEPLOY_REFUSED_RESTART_HOST", r.stdout)
        self.assert_untouched()

    def test_when_idle_without_restart_script_refuses_with_no_heartbeat(self) -> None:
        r = self.run_deploy("-RestartMode", "when-idle")
        self.assertEqual(r.returncode, 16, r.stdout + r.stderr)
        self.assertIn("DEPLOY_REFUSED_RESTART_HOST", r.stdout)
        self.assert_untouched()

    # ---- adversary r1 findings (A1..A6): each test failed on 0ef6bd17 ----

    def test_a3_noop_still_requires_a_readable_venue(self) -> None:
        # A3: an identical copy in a root with no inbox\ is NOT a clean no-op: the root cannot be judged, every doubt refuses.
        (self.venue / AGENT_NAME).write_bytes(NEW)
        shutil.rmtree(self.venue / "inbox")
        for extra in ((), ("-DryRun",)):
            r = self.run_deploy(*extra)
            self.assertEqual(r.returncode, 12, r.stdout + r.stderr)
            self.assertIn("DEPLOY_REFUSED_UNREADABLE", r.stdout)
            self.assertNotIn("DEPLOY_NOOP", r.stdout)

    def test_a2_a_second_deployer_is_refused_while_the_lock_is_held(self) -> None:
        # A2: two deployers on one root could each judge "no previous copy" and one would delete the other's verified deploy.
        (self.venue / "deploy.lock").write_text("held by another deployer")
        r = self.run_deploy()
        self.assertEqual(r.returncode, 17, r.stdout + r.stderr)
        self.assertIn("DEPLOY_REFUSED_LOCKED", r.stdout)
        self.assertEqual((self.venue / AGENT_NAME).read_bytes(), OLD)
        self.assertEqual(self.rollbacks(), [])
        self.assertTrue((self.venue / "deploy.lock").exists(), "a lock this run did not take is never removed")

    def test_a2_the_lock_is_released_after_a_deploy_and_a_noop(self) -> None:
        self.assertEqual(self.run_deploy().returncode, 0)
        self.assertFalse((self.venue / "deploy.lock").exists())
        self.assertEqual(self.run_deploy().returncode, 0)
        self.assertFalse((self.venue / "deploy.lock").exists())
        (self.venue / AGENT_NAME).write_bytes(OLD)
        (self.venue / "running" / "j.started.json").write_text("{}")
        self.assertEqual(self.run_deploy().returncode, 10)
        self.assertFalse((self.venue / "deploy.lock").exists(), "a refusal releases the lock too")

    def _beat(self, host: str, agent_script: str, pid: int = 4242) -> None:
        (self.venue / "heartbeat.txt").write_text(
            'alive 2026-11-18T10:00:00.0000000-06:00 pid=%d host=%s generation=1 processStartUtc=2026-11-18T09:00:00.0000000Z '
            'imagePath="C:\\pwsh.exe" agentScript="%s"\r\n' % (pid, host, agent_script)
        )

    def test_a4_restart_is_bound_to_the_agent_whose_root_was_checked(self) -> None:
        # A4: a copied heartbeat (host matches, but agentScript is the LIVE agent's path) must not let an idle backup root
        # stand in for a busy live agent: the schtasks End would hit the live task.
        import os
        self._beat(os.environ.get("COMPUTERNAME", "HOST"), r"C:\live-agent\ultra-magnus-agent.ps1")
        r = self.run_deploy("-RestartMode", "when-idle")
        self.assertEqual(r.returncode, 16, r.stdout + r.stderr)
        self.assertIn("DEPLOY_REFUSED_RESTART_HOST", r.stdout)
        self.assertIn("agentScript", r.stdout)
        self.assert_untouched()

    def test_a4_restart_refuses_when_the_heartbeat_process_is_not_alive(self) -> None:
        import os
        done = subprocess.run([PWSH, "-NoProfile", "-Command", "$PID"], capture_output=True, text=True, timeout=60)
        dead_pid = int(done.stdout.strip())
        self._beat(os.environ.get("COMPUTERNAME", "HOST"), str(self.venue / AGENT_NAME), pid=dead_pid)
        r = self.run_deploy("-RestartMode", "when-idle")
        self.assertEqual(r.returncode, 16, r.stdout + r.stderr)
        self.assertIn("DEPLOY_REFUSED_RESTART_HOST", r.stdout)
        self.assert_untouched()

    def test_a4_restart_refuses_when_the_task_does_not_run_this_roots_agent(self) -> None:
        import os
        me = subprocess.Popen([PWSH, "-NoProfile", "-Command", "Start-Sleep -Seconds 60"])
        self.addCleanup(lambda: (me.kill(), me.wait()))
        start = subprocess.run(
            [PWSH, "-NoProfile", "-Command", "(Get-Process -Id %d).StartTime.ToUniversalTime().ToString('o')" % me.pid],
            capture_output=True, text=True, timeout=60).stdout.strip()
        (self.venue / "heartbeat.txt").write_text(
            'alive x pid=%d host=%s generation=1 processStartUtc=%s agentScript="%s"\r\n'
            % (me.pid, os.environ.get("COMPUTERNAME", "HOST"), start, self.venue / AGENT_NAME))
        xml = Path(self._tmp.name) / "task.xml"
        xml.write_text('<Task><Actions><Exec><Command>pwsh.exe</Command><Arguments>-File "C:\\live-agent\\ultra-magnus-agent.ps1"</Arguments></Exec></Actions></Task>')
        r = self.run_deploy("-RestartMode", "when-idle", "-TaskXmlFile", str(xml))
        self.assertEqual(r.returncode, 16, r.stdout + r.stderr)
        self.assertIn("DEPLOY_REFUSED_RESTART_HOST", r.stdout)
        self.assertIn("task", r.stdout.lower())
        self.assert_untouched()

    def _dot_source(self, body: str):
        cmd = ". '%s' -VenueRoot '%s'; %s" % (psq(SCRIPT), psq(self.venue), body)
        return subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True, text=True, timeout=120)

    def test_a5_a_new_heartbeat_confirms_a_restart_only_for_a_live_matching_process(self) -> None:
        # A5: a heartbeat written by an agent that already died is not a restart.
        body = (
            "$me=Get-Process -Id $PID; $s=$me.StartTime.ToUniversalTime().ToString('o'); $a='%s';"
            "function B($p,$st,$sc){ [pscustomobject]@{pid=$p;host='H';start=$st;agentScript=$sc;ageSec=1} };"
            "'live='+(Test-NewHeartbeatIsLiveAgent -Beat (B $PID $s $a) -AgentScript $a);"
            "'dead='+(Test-NewHeartbeatIsLiveAgent -Beat (B 2147480000 $s $a) -AgentScript $a);"
            "'wrongstart='+(Test-NewHeartbeatIsLiveAgent -Beat (B $PID '2001-01-01T00:00:00.0000000Z' $a) -AgentScript $a);"
            "'nostart='+(Test-NewHeartbeatIsLiveAgent -Beat (B $PID '' $a) -AgentScript $a);"
            "'wrongscript='+(Test-NewHeartbeatIsLiveAgent -Beat (B $PID $s 'C:\\other.ps1') -AgentScript $a);"
            "'null='+(Test-NewHeartbeatIsLiveAgent -Beat $null -AgentScript $a)"
        ) % psq(self.venue / AGENT_NAME)
        r = self._dot_source(body)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(
            r.stdout.split(),
            ["live=True", "dead=False", "wrongstart=False", "nostart=False", "wrongscript=False", "null=False"], r.stdout + r.stderr)

    def test_a6_waits_are_bounded_by_a_monotonic_clock(self) -> None:
        # A6: Get-Date moves with the wall clock; a step back of an hour would stretch a 60 s wait to an hour.
        code = "\n".join(l for l in SCRIPT.read_text(encoding="ascii").splitlines() if not l.lstrip().startswith("#"))
        self.assertNotRegex(code, r"\(Get-Date\)\.Add(Seconds|Minutes)", "no wall-clock deadline")
        self.assertIn("[System.Diagnostics.Stopwatch]::StartNew()", code)
        r = self._dot_source("$t=[Diagnostics.Stopwatch]::StartNew(); 'never='+(Wait-Until -TimeoutSec 1 -Condition { $false }); 'sec='+[int]$t.Elapsed.TotalSeconds; 'now='+(Wait-Until -TimeoutSec 5 -Condition { $true })")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.split(), ["never=False", "sec=1", "now=True"], r.stdout + r.stderr)

    # ---- adversary r2 findings (B1..B4): each test failed on e039c604 ----

    def _task_xml_case(self, name: str, agent_script: str, xml: str) -> str:
        f = Path(self._tmp.name) / ("task-%s.xml" % name)
        f.write_text(xml, encoding="utf-8")
        r = self._dot_source("Test-TaskXmlRunsScript -Xml (Get-Content -Raw -LiteralPath '%s') -AgentScript '%s'" % (psq(f), psq(agent_script)))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout.strip()

    def test_b1_task_binding_reads_the_executable_action_not_any_mention(self) -> None:
        def task(args: str, desc: str = "") -> str:
            return ("<Task><RegistrationInfo><Description>%s</Description></RegistrationInfo><Actions><Exec>"
                    "<Command>pwsh.exe</Command><Arguments>%s</Arguments></Exec></Actions></Task>") % (desc, args)

        mine = r"C:\bench\ultra-magnus-agent.ps1"
        # the other root's busy agent, with my path only in the description (the adversary's input)
        self.assertEqual(self._task_xml_case("desc", mine, task("-File &quot;C:\\busy-root\\ultra-magnus-agent.ps1&quot;", "backup of " + mine)), "False")
        # a mention in a later argument is not the script the task runs
        self.assertEqual(self._task_xml_case("later", mine, task("-File &quot;C:\\busy-root\\ultra-magnus-agent.ps1&quot; " + mine)), "False")
        self.assertEqual(self._task_xml_case("quoted", mine, task("-NoProfile -File &quot;%s&quot;" % mine)), "True")
        self.assertEqual(self._task_xml_case("bare", mine, task("-NoProfile -File %s" % mine)), "True")
        self.assertEqual(self._task_xml_case("case", mine, task("-File &quot;C:\\BENCH\\Ultra-Magnus-Agent.ps1&quot;")), "True")
        # XML-escaped path: the correct task for a root with an ampersand must still bind
        amp = r"C:\Bench & Jobs\ultra-magnus-agent.ps1"
        self.assertEqual(self._task_xml_case("amp", amp, task("-File &quot;C:\\Bench &amp; Jobs\\ultra-magnus-agent.ps1&quot;")), "True")
        self.assertEqual(self._task_xml_case("nofile", mine, task("-NoProfile")), "False")
        self.assertEqual(self._task_xml_case("malformed", mine, "<Task><Actions>"), "False")

    def test_b1_description_only_task_xml_refuses_end_to_end(self) -> None:
        import os
        me = subprocess.Popen([PWSH, "-NoProfile", "-Command", "Start-Sleep -Seconds 60"])
        self.addCleanup(lambda: (me.kill(), me.wait()))
        start = subprocess.run(
            [PWSH, "-NoProfile", "-Command", "(Get-Process -Id %d).StartTime.ToUniversalTime().ToString('o')" % me.pid],
            capture_output=True, text=True, timeout=60).stdout.strip()
        (self.venue / "heartbeat.txt").write_text(
            'alive x pid=%d host=%s generation=1 processStartUtc=%s agentScript="%s"\r\n'
            % (me.pid, os.environ.get("COMPUTERNAME", "HOST"), start, self.venue / AGENT_NAME))
        xml = Path(self._tmp.name) / "task-desc.xml"
        xml.write_text(
            "<Task><RegistrationInfo><Description>backup of %s</Description></RegistrationInfo><Actions><Exec><Command>pwsh.exe</Command>"
            "<Arguments>-File \"C:\\busy-root\\ultra-magnus-agent.ps1\"</Arguments></Exec></Actions></Task>" % (self.venue / AGENT_NAME),
            encoding="utf-8")
        r = self.run_deploy("-RestartMode", "when-idle", "-TaskXmlFile", str(xml))
        self.assertEqual(r.returncode, 16, r.stdout + r.stderr)
        self.assertIn("DEPLOY_REFUSED_RESTART_HOST", r.stdout)
        self.assert_untouched()

    def test_b2_a_condition_observed_after_the_deadline_is_not_accepted(self) -> None:
        body = (
            "$s=[Diagnostics.Stopwatch]::StartNew();"
            "'slowcond='+(Wait-Until -TimeoutSec 1 -Condition { Start-Sleep -Milliseconds 1500; $true });"
            "$g=[Diagnostics.Stopwatch]::StartNew();"
            "'late='+(Wait-Until -TimeoutSec 1 -PollMs 900 -Variables @{ g = $g } -Condition { $g.Elapsed.TotalSeconds -gt 1.05 });"
            "'inside='+(Wait-Until -TimeoutSec 5 -PollMs 100 -Variables @{ s = $s } -Condition { $s.Elapsed.TotalSeconds -gt 0 })"
        )
        r = self._dot_source(body)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.split(), ["slowcond=False", "late=False", "inside=True"], r.stdout + r.stderr)

    def test_b3_a_replacement_with_a_reused_pid_is_a_different_process(self) -> None:
        body = (
            "function B($p,$st){ [pscustomobject]@{pid=$p;start=$st} };"
            "'samepid_newstart='+(Test-BeatIsDifferentProcess -Old (B 4242 '2026-11-18T14:00:00.0000000Z') -New (B 4242 '2026-11-18T18:00:01.0000000Z'));"
            "'same='+(Test-BeatIsDifferentProcess -Old (B 4242 '2026-11-18T14:00:00.0000000Z') -New (B 4242 '2026-11-18T14:00:00.5000000Z'));"
            "'newpid='+(Test-BeatIsDifferentProcess -Old (B 4242 '2026-11-18T14:00:00.0000000Z') -New (B 4244 '2026-11-18T14:00:00.0000000Z'));"
            "'nostart='+(Test-BeatIsDifferentProcess -Old (B 4242 '') -New (B 4242 ''))"
        )
        r = self._dot_source(body)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.split(), ["samepid_newstart=True", "same=False", "newpid=True", "nostart=False"], r.stdout + r.stderr)

    def test_b4_get_venue_state_lists_the_root_itself(self) -> None:
        # Platform-free pin (the ACL case below runs on Windows only): the root is enumerated, not only its three children.
        code = "\n".join(l for l in SCRIPT.read_text(encoding="ascii").splitlines() if not l.lstrip().startswith("#"))
        self.assertRegex(code, r"EnumerateFileSystemEntries\(\$VenueRoot\)")

    @unittest.skipUnless(sys.platform == "win32" and shutil.which("icacls"), "needs Windows icacls to deny List-Directory on the root")
    def test_b4_a_root_that_cannot_be_listed_is_unreadable_even_for_a_noop(self) -> None:
        import os
        who = "%s\\%s" % (os.environ.get("USERDOMAIN", ""), os.environ.get("USERNAME", ""))
        (self.venue / AGENT_NAME).write_bytes(NEW)
        # deny List-Directory on the ROOT only (no inheritance), as the adversary's input; the children stay listable
        subprocess.run(["icacls", str(self.venue), "/deny", "%s:(RD)" % who], check=True, capture_output=True)
        self.addCleanup(lambda: subprocess.run(["icacls", str(self.venue), "/remove:d", who], capture_output=True))
        r = self.run_deploy()
        self.assertEqual(r.returncode, 12, r.stdout + r.stderr)
        self.assertIn("DEPLOY_REFUSED_UNREADABLE", r.stdout)
        self.assertNotIn("DEPLOY_NOOP", r.stdout)

    def test_a1_work_that_arrives_during_the_restart_is_reported_not_hidden(self) -> None:
        # A1: no admission barrier exists in the agent, so a job landing during the restart cannot be prevented from here.
        # It must be DETECTED and typed (the agent's own startup recovery then adopts or requeues it).
        arriving = Path(self._tmp.name) / "arriving-restart.ps1"
        arriving.write_text(
            "param([string]$VenueRoot)\r\nSet-Content -LiteralPath (Join-Path $VenueRoot 'inbox\\late.job.ps1') -Value 'x'\r\n", encoding="ascii")
        r = self.run_deploy("-RestartMode", "when-idle", "-RestartScript", str(arriving))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("DEPLOY_WARN_ARRIVED_DURING_RESTART running=0 inbox=1", r.stdout)
        self.assertRegex(r.stdout, r"DEPLOY_OK sha=[0-9A-F]{64} rollback=.+ restarted=yes")

    # ---- adversary r1 pass 3 findings (A1..A5 of fa4db2b5): each test failed on fa4db2b5 ----

    def _exec_task_case(self, name: str, command: str, args: str, agent_script: str = r"C:\bench\ultra-magnus-agent.ps1") -> str:
        xml = "<Task><Actions><Exec><Command>%s</Command><Arguments>%s</Arguments></Exec></Actions></Task>" % (command, args)
        return self._task_xml_case(name, agent_script, xml)

    def test_p3a1_the_task_runs_this_script_as_the_first_file_of_a_powershell_host(self) -> None:
        mine = r"C:\bench\ultra-magnus-agent.ps1"
        q = "&quot;"
        # the adversary's input: a -Command that never runs the agent, then a later -File naming it
        self.assertEqual(self._exec_task_case("cmd-then-file", "pwsh.exe", "-Command %sStart-Sleep 3600; # ignored%s -File %s" % (q, q, mine)), "False")
        self.assertEqual(self._exec_task_case("short-c", "pwsh.exe", "-c x -File %s" % mine), "False")
        self.assertEqual(self._exec_task_case("encoded", "pwsh.exe", "-EncodedCommand AAAA -File %s" % mine), "False")
        # the command must be a PowerShell host: cmd.exe /c echo -File <mine> is not the agent
        self.assertEqual(self._exec_task_case("cmd-exe", "cmd.exe", "/c echo -File %s" % mine), "False")
        self.assertEqual(self._exec_task_case("cmd-exe-file", "cmd.exe", "-File %s" % mine), "False")
        # only the FIRST -File is the script the host runs; a later one is that script's argument
        self.assertEqual(self._exec_task_case("two-files", "pwsh.exe", "-File C:\\busy\\ultra-magnus-agent.ps1 -File %s" % mine), "False")
        # a non-switch token before -File is a script or command the host would run first
        self.assertEqual(self._exec_task_case("positional-first", "pwsh.exe", "C:\\busy\\x.ps1 -File %s" % mine), "False")
        # the real shapes still bind
        self.assertEqual(self._exec_task_case("full-path-host", "C:\\Program Files\\PowerShell\\7\\pwsh.exe", "-NoProfile -File %s%s%s" % (q, mine, q)), "True")
        self.assertEqual(self._exec_task_case("ps51", "powershell.exe", "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File %s" % mine), "True")
        self.assertEqual(self._exec_task_case("noexe", "pwsh", "-NoLogo -NonInteractive -File %s" % mine), "True")
        # a relative script resolves against a working directory this check cannot see
        self.assertEqual(self._exec_task_case("relative", "pwsh.exe", "-File ultra-magnus-agent.ps1"), "False")

    def test_p3a2_the_task_root_override_is_bound_to_the_checked_root(self) -> None:
        mine = r"C:\bench\ultra-magnus-agent.ps1"
        base = "-NoProfile -File %s" % mine
        # the adversary's input: the task serves a different (busy) root than the one checked idle
        self.assertEqual(self._exec_task_case("root-other", "pwsh.exe", base + " -Root C:\\busy-root"), "False")
        self.assertEqual(self._exec_task_case("root-colon", "pwsh.exe", base + " -Root:C:\\busy-root"), "False")
        self.assertEqual(self._exec_task_case("root-abbrev", "pwsh.exe", base + " -Ro C:\\busy-root"), "False")
        self.assertEqual(self._exec_task_case("root-quoted", "pwsh.exe", base + " -Root &quot;C:\\busy root&quot;"), "False")
        # the first positional parameter of the agent IS -Root
        self.assertEqual(self._exec_task_case("root-positional", "pwsh.exe", base + " C:\\busy-root"), "False")
        self.assertEqual(self._exec_task_case("root-no-value", "pwsh.exe", base + " -Root"), "False")
        # pass 4 A2: a common parameter is not a skippable token: PowerShell binds the path that follows -Verbose / -Debug to -Root
        self.assertEqual(self._exec_task_case("verbose-positional", "pwsh.exe", base + " -Verbose C:\\busy"), "False")
        self.assertEqual(self._exec_task_case("debug-positional", "pwsh.exe", base + " -Debug C:\\busy"), "False")
        # every argument beyond -Root is refused, not skipped: the check cannot know what each one binds
        self.assertEqual(self._exec_task_case("verbose", "pwsh.exe", base + " -Verbose"), "False")
        self.assertEqual(self._exec_task_case("runonce", "pwsh.exe", base + " -RunOnce"), "False")
        self.assertEqual(self._exec_task_case("poll", "pwsh.exe", base + " -PollSeconds 5 -Root C:\\bench"), "False")
        self.assertEqual(self._exec_task_case("root-twice", "pwsh.exe", base + " -Root C:\\bench -Root C:\\bench"), "False")
        self.assertEqual(self._exec_task_case("root-ambiguous", "pwsh.exe", base + " -R C:\\bench"), "False")
        # the same root, spelled any way, binds
        self.assertEqual(self._exec_task_case("root-same", "pwsh.exe", base + " -Root C:\\bench"), "True")
        self.assertEqual(self._exec_task_case("root-same-colon", "pwsh.exe", base + " -Root:C:\\bench"), "True")
        self.assertEqual(self._exec_task_case("root-same-abbrev", "pwsh.exe", base + " -Roo C:\\BENCH"), "True")
        self.assertEqual(self._exec_task_case("root-same-slash", "pwsh.exe", base + " -Root C:\\bench\\"), "True")
        self.assertEqual(self._exec_task_case("root-same-quoted-slash", "pwsh.exe", base + " -Root &quot;c:\\BENCH\\\\&quot;"), "True")

    def test_p4a6_the_task_arguments_are_decoded_as_windows_decodes_them(self) -> None:
        mine = r"C:\bench\ultra-magnus-agent.ps1"
        # pass 4 A6: in -Root "C:\bench\" the backslash escapes the closing quote, so Root is C:\bench" (a literal quote), not C:\bench
        self.assertEqual(self._exec_task_case("quote-escaped", "pwsh.exe", "-NoProfile -File %s -Root &quot;C:\\bench\\&quot;" % mine), "False")
        # an unterminated or doubled quote is unreadable, so it refuses
        self.assertEqual(self._exec_task_case("unterminated", "pwsh.exe", "-NoProfile -File %s -Root &quot;C:\\bench" % mine), "False")
        self.assertEqual(self._exec_task_case("doubled", "pwsh.exe", "-NoProfile -File &quot;C:\\bench\\ultra-magnus-agent.ps1&quot;&quot; -Root C:\\bench"), "False")
        # the decoder itself (CommandLineToArgvW): the text goes in through a file, so no shell quoting is involved
        cases = [
            ('-a "b c" d', "True", ["-a", "b c", "d"]),
            ('x\\"y', "True", ['x"y']),                  # one backslash + quote: a literal quote
            ('p\\\\"q r" s', "True", ["p\\q r", "s"]),  # two backslashes + quote: one backslash, the quote toggles
            ('C:\\a\\b "C:\\c d\\"', "False", []),       # the closing quote is escaped: unterminated
            ('a "" b', "True", ["a", "", "b"]),          # an empty quoted argument OUTSIDE a quoted run is fine
        ]
        for n, (text, ok, toks) in enumerate(cases):
            f = Path(self._tmp.name) / ("args-%d.txt" % n)
            f.write_text(text, encoding="utf-8")
            r = self._dot_source("$r = Split-TaskArguments -Text (Get-Content -Raw -LiteralPath '%s'); $r.ok; $r.tokens.Count; $r.tokens" % psq(f))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            lines = r.stdout.splitlines()
            self.assertEqual(lines[0], ok, text)
            self.assertEqual(lines[2:] if ok == "True" else [], toks, text)

    def test_p4a3_a_task_with_a_second_action_is_refused(self) -> None:
        mine = r"C:\bench\ultra-magnus-agent.ps1"
        busy = r"C:\busy\ultra-magnus-agent.ps1"

        def two(first: str, second: str) -> str:
            e = "<Exec><Command>pwsh.exe</Command><Arguments>-NoProfile -File %s</Arguments></Exec>"
            return "<Task><Actions>%s%s</Actions></Task>" % (e % first, e % second)

        # `schtasks /End` stops the whole task, so a busy first action is ended with the bench agent that was checked
        self.assertEqual(self._task_xml_case("two-busy-first", mine, two(busy, mine)), "False")
        self.assertEqual(self._task_xml_case("two-mine-first", mine, two(mine, busy)), "False")
        self.assertEqual(self._task_xml_case("two-same", mine, two(mine, mine)), "False")
        com = "<Task><Actions><Exec><Command>pwsh.exe</Command><Arguments>-NoProfile -File %s</Arguments></Exec><ComHandler><ClassId>{x}</ClassId></ComHandler></Actions></Task>" % mine
        self.assertEqual(self._task_xml_case("exec-and-com", mine, com), "False")
        one = "<Task><Actions Context=\"Author\"><Exec><Command>pwsh.exe</Command><Arguments>-NoProfile -File %s</Arguments></Exec></Actions></Task>" % mine
        self.assertEqual(self._task_xml_case("one", mine, one), "True")

    def test_p4a4_host_options_that_stop_the_script_from_running_are_refused(self) -> None:
        mine = r"C:\bench\ultra-magnus-agent.ps1"
        for name, opts in (("version", "-Version 7"), ("help", "-Help"), ("question", "-?"), ("noexit", "-NoExit"), ("interactive", "-Interactive"),
                           ("cwd", "-WorkingDirectory C:\\x"), ("unknown", "-Bogus"), ("value-eats-file", "-ExecutionPolicy -File")):
            self.assertEqual(self._exec_task_case("host-" + name, "pwsh.exe", "%s -File %s" % (opts, mine)), "False", name)
        self.assertEqual(self._exec_task_case("host-ok", "pwsh.exe", "-NoProfile -NoLogo -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File %s" % mine), "True")
        self.assertEqual(self._exec_task_case("host-short", "pwsh.exe", "-nop -ep Bypass -w Hidden -f %s" % mine), "True")

    def test_p4a5_the_stop_acts_on_a_heartbeat_identified_by_pid_and_start(self) -> None:
        # pass 4 A5: the binding was proved before the deploy; a heartbeat rewritten without a start (PID 4 is a live system process)
        # must not be stopped. Invoke-AgentRestart re-proves the binding first; the task name is one that does not exist, so a regression
        # that reached schtasks could not touch a real task.
        import os
        me = subprocess.Popen([PWSH, "-NoProfile", "-Command", "Start-Sleep -Seconds 120"])
        self.addCleanup(lambda: (me.kill(), me.wait()))
        start = subprocess.run(
            [PWSH, "-NoProfile", "-Command", "(Get-Process -Id %d).StartTime.ToUniversalTime().ToString('o')" % me.pid],
            capture_output=True, text=True, timeout=60).stdout.strip()
        agent = self.venue / AGENT_NAME
        good = 'alive x pid=%d host=BENCHTEST generation=1 processStartUtc=%s agentScript="%s"\r\n' % (me.pid, start, agent)
        # the same live pid, but the heartbeat now carries NO processStartUtc: it no longer identifies a process
        bad = 'alive x pid=%d host=BENCHTEST generation=1 agentScript="%s"\r\n' % (me.pid, agent)
        xml = Path(self._tmp.name) / "task-p4a5.xml"
        xml.write_text("<Task><Actions><Exec><Command>pwsh.exe</Command><Arguments>-NoProfile -File \"%s\"</Arguments></Exec></Actions></Task>" % agent, encoding="utf-8")
        beat = self.venue / "heartbeat.txt"
        later = Path(self._tmp.name) / "heartbeat-later.txt"
        beat.write_text(good)
        later.write_text(bad)
        cmd = (
            ". '%s' -VenueRoot '%s' -TaskName '\\MlvNoSuchTaskForTest' -TaskXmlFile '%s'; "
            "$first = Get-RestartBindingRefusal -AgentScript '%s'; 'first=' + $(if ($null -eq $first) { 'bound' } else { $first });"
            "Copy-Item -LiteralPath '%s' -Destination '%s' -Force; "
            "$r = Invoke-AgentRestart -AgentScript '%s'; 'why=' + $r.why"
        ) % (psq(SCRIPT), psq(self.venue), psq(xml), psq(agent), psq(later), psq(beat), psq(agent))
        env = dict(os.environ, COMPUTERNAME="BENCHTEST")
        r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True, text=True, timeout=120, env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("first=bound", r.stdout)
        self.assertIn("why=the agent binding no longer holds, so nothing was stopped", r.stdout)
        self.assertIn("recorded start", r.stdout)

    def test_p3a3_a_wait_is_bounded_even_when_the_condition_stalls(self) -> None:
        t0 = time.monotonic()
        r = self._dot_source("'stalled='+(Wait-Until -TimeoutSec 1 -Condition { Start-Sleep -Seconds 120; $true })")
        elapsed = time.monotonic() - t0
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.split(), ["stalled=False"], r.stdout + r.stderr)
        self.assertLess(elapsed, 30, "the wait returned at its budget and the host did not wait for the stalled condition")

    def test_p3a3_a_wait_condition_sees_the_scripts_own_functions_and_variables(self) -> None:
        (self.venue / "heartbeat.txt").write_text("alive x pid=%d host=H generation=1\r\n" % 4242)
        body = (
            "'fn='+(Wait-Until -TimeoutSec 20 -PollMs 100 -Condition { $b = Read-Heartbeat; ($null -ne $b) -and ($b.pid -eq 4242) });"
            "'var='+(Wait-Until -TimeoutSec 20 -PollMs 100 -Variables @{ want = 7 } -Condition { $want -eq 7 });"
            "'thrown='+$(try { Wait-Until -TimeoutSec 5 -Condition { throw 'boom' } } catch { 'caught:' + $_.Exception.Message })"
        )
        r = self._dot_source(body)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.split(), ["fn=True", "var=True", "thrown=caught:boom"], r.stdout + r.stderr)

    def test_p3a3_a_child_process_call_is_bounded(self) -> None:
        body = (
            "$t=[Diagnostics.Stopwatch]::StartNew();"
            "$r=Invoke-BoundedProcess -FilePath '%s' -Arguments @('-NoProfile','-Command','Start-Sleep -Seconds 120') -TimeoutSec 2;"
            "'timedout='+$r.timedOut; 'fast='+($t.Elapsed.TotalSeconds -lt 20);"
            "$ok=Invoke-BoundedProcess -FilePath '%s' -Arguments @('-NoProfile','-Command','Write-Output hi; exit 3') -TimeoutSec 60;"
            "'exit='+$ok.exitCode; 'out='+$ok.output.Trim(); 'ok_timedout='+$ok.timedOut"
        ) % (psq(PWSH), psq(PWSH))
        r = self._dot_source(body)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.split(), ["timedout=True", "fast=True", "exit=3", "out=hi", "ok_timedout=False"], r.stdout + r.stderr)

    def test_p3a3_the_restart_calls_and_the_venue_reads_use_the_bounded_helpers(self) -> None:
        code = "\n".join(l for l in SCRIPT.read_text(encoding="ascii").splitlines() if not l.lstrip().startswith("#"))
        self.assertIsNone(re.search(r"&\s*schtasks\.exe", code), "schtasks is run through Invoke-BoundedProcess, never directly")
        self.assertIn("Invoke-BoundedProcess", code)

    def test_p3a5_one_start_time_tolerance_decides_alive_and_different(self) -> None:
        code = "\n".join(l for l in SCRIPT.read_text(encoding="ascii").splitlines() if not l.lstrip().startswith("#"))
        self.assertEqual(len(re.findall(r"\$script:StartToleranceSec\s*=", code)), 1, "the tolerance is defined once")
        self.assertIsNone(re.search(r"TotalSeconds\)\s*-(lt|ge|gt|le)\s+\d", code), "no second tolerance literal beside the shared one")
        # at exactly the tolerance the two checks must still agree: same process in BOTH, or different in BOTH
        body = (
            "$me=Get-Process -Id $PID; $s0=$me.StartTime.ToUniversalTime();"
            "function B($p,$st){ [pscustomobject]@{pid=$p;start=$st;agentScript='x';host='H';ageSec=1} };"
            "foreach($d in 0.5,2.0,2.5){ $st=$s0.AddSeconds($d).ToString('o');"
            " 'd'+$d+' alive='+(Test-AgentProcessAlive -Beat (B $PID $st) -RequireStart)+' different='+(Test-BeatIsDifferentProcess -Old (B $PID $s0.ToString('o')) -New (B $PID $st)) }"
        )
        r = self._dot_source(body)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        lines = [l for l in r.stdout.splitlines() if l.startswith("d")]
        self.assertEqual(len(lines), 3, r.stdout + r.stderr)
        for line in lines:
            m = re.match(r"d(\S+) alive=(\w+) different=(\w+)$", line)
            self.assertIsNotNone(m, line)
            self.assertNotEqual(m.group(2), m.group(3), "alive and different are exact opposites for one pid and one start gap: " + line)
        self.assertEqual([re.match(r"d(\S+) alive=(\w+)", l).group(2) for l in lines], ["True", "True", "False"])

    def test_p3_hosts_without_icacls_skip_the_acl_case_instead_of_erroring(self) -> None:
        # A4: the Ubuntu shard has pwsh but no icacls; the ACL case carries an explicit platform guard.
        src = Path(__file__).read_text(encoding="utf-8")
        i = src.index("def test_b4_a_root_that_cannot_be_listed_is_unreadable_even_for_a_noop")
        self.assertIn('sys.platform == "win32"', src[max(0, i - 260):i])
        self.assertIn('shutil.which("icacls")', src[max(0, i - 260):i])

    def test_default_source_is_the_tracked_agent(self) -> None:
        # With no -Source the script reads the agent beside itself, so the default is the tracked file.
        r = subprocess.run(
            [PWSH, "-NoProfile", "-NonInteractive", "-File", str(SCRIPT), "-VenueRoot", str(self.venue), "-DryRun"],
            capture_output=True, text=True, timeout=120,
        )
        tracked = (SCRIPT.parent / AGENT_NAME).read_bytes()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("sha=%s" % sha(tracked), r.stdout)


if __name__ == "__main__":
    unittest.main()
