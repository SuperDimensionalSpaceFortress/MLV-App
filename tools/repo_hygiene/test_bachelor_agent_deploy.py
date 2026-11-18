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
import tempfile
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
            "param([string]$VenueRoot)\r\nSet-Content -LiteralPath '%s' -Value $VenueRoot\r\n" % self.marker,
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
        cmd = ". '%s' -VenueRoot '%s'; %s" % (SCRIPT, self.venue, body)
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
        ) % (self.venue / AGENT_NAME)
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
        r = self._dot_source("Test-TaskXmlRunsScript -Xml (Get-Content -Raw -LiteralPath '%s') -AgentScript '%s'" % (f, agent_script))
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
        self.assertEqual(self._task_xml_case("bare", mine, task("-NoProfile -File %s -Foo" % mine)), "True")
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
            "'late='+(Wait-Until -TimeoutSec 1 -PollMs 900 -Condition { $g.Elapsed.TotalSeconds -gt 1.05 });"
            "'inside='+(Wait-Until -TimeoutSec 5 -PollMs 100 -Condition { $s.Elapsed.TotalSeconds -gt 0 })"
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
