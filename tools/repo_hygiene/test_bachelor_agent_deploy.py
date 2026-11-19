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
import os
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

# OLD sleeps so a test can start it as a LIVE agent process (`pwsh -File <venue>\ultra-magnus-agent.ps1`); nothing else runs it.
OLD = b"# old agent\r\nStart-Sleep -Seconds 120\r\nWrite-Output 'old'\r\n"
NEW = b"# new agent\r\nWrite-Output 'new'\r\n"
# The host name every test uses. The script compares a heartbeat's host with $env:COMPUTERNAME; that variable exists on Windows only
# (on Linux it is unset), so the tests SET it for the processes they start, which makes the restart-binding cases platform-neutral.
BENCH_HOST = "BENCHTEST"
BENCH_ENV = dict(os.environ, COMPUTERNAME=BENCH_HOST)


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
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=BENCH_ENV)

    def start_of(self, pid: int) -> str:
        """The UTC start time of a live process, as the agent's heartbeat writes it (processStartUtc)."""
        return subprocess.run(
            [PWSH, "-NoProfile", "-Command", "(Get-Process -Id %d).StartTime.ToUniversalTime().ToString('o')" % pid],
            capture_output=True, text=True, timeout=60).stdout.strip()

    def live_agent(self, *script_args: str):
        """A LIVE process that runs the venue's agent script the way the task does: `pwsh -NoProfile -ExecutionPolicy Bypass -File <agent> [args]`.
        Returns (pid, utc start text). The command line is what Get-ProcessArgv reads (Win32_Process on Windows, /proc elsewhere)."""
        p = subprocess.Popen([PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(self.venue / AGENT_NAME), *script_args])
        self.addCleanup(lambda: (p.kill(), p.wait()))
        return p.pid, self.start_of(p.pid)

    def task_pids(self, *pids: int, name: str = "task-pids") -> Path:
        """The -TaskPidsFile seam: the pids of the task's RUNNING instances, as the Task Scheduler would report them."""
        f = Path(self._tmp.name) / ("%s-%s.txt" % (name, "-".join(str(p) for p in pids) or "none"))
        f.write_text("\n".join(str(p) for p in pids), encoding="ascii")
        return f

    def write_beat(self, pid: int, start: str, agent: Path | None = None) -> None:
        (self.venue / "heartbeat.txt").write_text(
            'alive x pid=%d host=%s generation=1 processStartUtc=%s agentScript="%s"\r\n'
            % (pid, BENCH_HOST, start, agent if agent is not None else self.venue / AGENT_NAME))

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
        self._beat(BENCH_HOST, r"C:\live-agent\ultra-magnus-agent.ps1")
        r = self.run_deploy("-RestartMode", "when-idle")
        self.assertEqual(r.returncode, 16, r.stdout + r.stderr)
        self.assertIn("DEPLOY_REFUSED_RESTART_HOST", r.stdout)
        self.assertIn("agentScript", r.stdout)
        self.assert_untouched()

    def test_a4_restart_refuses_when_the_heartbeat_process_is_not_alive(self) -> None:
        done = subprocess.run([PWSH, "-NoProfile", "-Command", "$PID"], capture_output=True, text=True, timeout=60)
        dead_pid = int(done.stdout.strip())
        self._beat(BENCH_HOST, str(self.venue / AGENT_NAME), pid=dead_pid)
        r = self.run_deploy("-RestartMode", "when-idle")
        self.assertEqual(r.returncode, 16, r.stdout + r.stderr)
        self.assertIn("DEPLOY_REFUSED_RESTART_HOST", r.stdout)
        self.assert_untouched()

    def test_a4_restart_refuses_when_the_task_does_not_run_this_roots_agent(self) -> None:
        pid, start = self.live_agent()
        self.write_beat(pid, start)
        xml = Path(self._tmp.name) / "task.xml"
        xml.write_text('<Task><Actions><Exec><Command>pwsh.exe</Command><Arguments>-File "C:\\live-agent\\ultra-magnus-agent.ps1"</Arguments></Exec></Actions></Task>')
        r = self.run_deploy("-RestartMode", "when-idle", "-TaskXmlFile", str(xml))
        self.assertEqual(r.returncode, 16, r.stdout + r.stderr)
        self.assertIn("DEPLOY_REFUSED_RESTART_HOST", r.stdout)
        self.assertIn("task", r.stdout.lower())
        self.assert_untouched()

    def _dot_source(self, body: str):
        cmd = ". '%s' -VenueRoot '%s'; %s" % (psq(SCRIPT), psq(self.venue), body)
        return subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True, text=True, timeout=120, env=BENCH_ENV)

    def _sleeper(self):
        """A live process that is not the deployer (the deployer is never its own replacement); returns (pid, utc start text)."""
        me = subprocess.Popen([PWSH, "-NoProfile", "-Command", "Start-Sleep -Seconds 120"])
        self.addCleanup(lambda: (me.kill(), me.wait()))
        start = subprocess.run(
            [PWSH, "-NoProfile", "-Command", "(Get-Process -Id %d).StartTime.ToUniversalTime().ToString('o')" % me.pid],
            capture_output=True, text=True, timeout=60).stdout.strip()
        return me.pid, start

    def test_a5_a_new_heartbeat_confirms_a_restart_only_for_a_live_matching_process(self) -> None:
        # A5: a heartbeat written by an agent that already died is not a restart.
        # pass 5 A3: nor is one that names another machine, is the deployer itself, or was started before the task was.
        pid, s = self._sleeper()
        body = (
            "$s='%s'; $a='%s'; $sp=%d;"
            "function B($p,$st,$sc,$h=$env:COMPUTERNAME){ [pscustomobject]@{pid=$p;host=$h;start=$st;agentScript=$sc;ageSec=1} };"
            "'live='+(Test-NewHeartbeatIsLiveAgent -Beat (B $sp $s $a) -AgentScript $a);"
            "'dead='+(Test-NewHeartbeatIsLiveAgent -Beat (B 2147480000 $s $a) -AgentScript $a);"
            "'wrongstart='+(Test-NewHeartbeatIsLiveAgent -Beat (B $sp '2001-01-01T00:00:00.0000000Z' $a) -AgentScript $a);"
            "'nostart='+(Test-NewHeartbeatIsLiveAgent -Beat (B $sp '' $a) -AgentScript $a);"
            "'wrongscript='+(Test-NewHeartbeatIsLiveAgent -Beat (B $sp $s 'C:\\other.ps1') -AgentScript $a);"
            "'null='+(Test-NewHeartbeatIsLiveAgent -Beat $null -AgentScript $a);"
            "'foreignhost='+(Test-NewHeartbeatIsLiveAgent -Beat (B $sp $s $a 'SOME-OTHER-HOST') -AgentScript $a);"
            "$me=Get-Process -Id $PID; $ms=$me.StartTime.ToUniversalTime().ToString('o');"
            "'deployer='+(Test-NewHeartbeatIsLiveAgent -Beat (B $PID $ms $a) -AgentScript $a);"
            "$st=[DateTimeOffset]::Parse($s,[Globalization.CultureInfo]::InvariantCulture,[Globalization.DateTimeStyles]::AssumeUniversal).UtcDateTime;"
            "'ranbefore='+(Test-NewHeartbeatIsLiveAgent -Beat (B $sp $s $a) -AgentScript $a -NotBeforeUtc $st.AddSeconds(-30));"
            "'ranafter='+(Test-NewHeartbeatIsLiveAgent -Beat (B $sp $s $a) -AgentScript $a -NotBeforeUtc $st.AddSeconds(30))"
        ) % (s, psq(self.venue / AGENT_NAME), pid)
        r = self._dot_source(body)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(
            r.stdout.split(),
            ["live=True", "dead=False", "wrongstart=False", "nostart=False", "wrongscript=False", "null=False",
             "foreignhost=False", "deployer=False", "ranbefore=True", "ranafter=False"], r.stdout + r.stderr)

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
        self.assertEqual(self._task_xml_case("quoted", mine, task("-NoProfile -ExecutionPolicy Bypass -File &quot;%s&quot;" % mine)), "True")
        self.assertEqual(self._task_xml_case("bare", mine, task("-NoProfile -ExecutionPolicy Bypass -File %s" % mine)), "True")
        self.assertEqual(self._task_xml_case("case", mine, task("-NoProfile -ExecutionPolicy Bypass -File &quot;C:\\BENCH\\Ultra-Magnus-Agent.ps1&quot;")), "True")
        # XML-escaped path: the correct task for a root with an ampersand must still bind
        amp = r"C:\Bench & Jobs\ultra-magnus-agent.ps1"
        self.assertEqual(self._task_xml_case("amp", amp, task("-NoProfile -ExecutionPolicy Bypass -File &quot;C:\\Bench &amp; Jobs\\ultra-magnus-agent.ps1&quot;")), "True")
        self.assertEqual(self._task_xml_case("nofile", mine, task("-NoProfile")), "False")
        self.assertEqual(self._task_xml_case("malformed", mine, "<Task><Actions>"), "False")

    def test_b1_description_only_task_xml_refuses_end_to_end(self) -> None:
        pid, start = self.live_agent()
        self.write_beat(pid, start)
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
            "param([string]$VenueRoot)\r\nSet-Content -LiteralPath (Join-Path (Join-Path $VenueRoot 'inbox') 'late.job.ps1') -Value 'x'\r\n", encoding="ascii")
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
        self.assertEqual(self._exec_task_case("full-path-host", "C:\\Program Files\\PowerShell\\7\\pwsh.exe", "-NoProfile -ExecutionPolicy Bypass -File %s%s%s" % (q, mine, q)), "True")
        self.assertEqual(self._exec_task_case("ps51", "powershell.exe", "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File %s" % mine), "True")
        self.assertEqual(self._exec_task_case("noexe", "pwsh", "-NoProfile -NoLogo -NonInteractive -ExecutionPolicy Bypass -File %s" % mine), "True")
        # a relative script resolves against a working directory this check cannot see
        self.assertEqual(self._exec_task_case("relative", "pwsh.exe", "-File ultra-magnus-agent.ps1"), "False")

    def test_p3a2_the_task_root_override_is_bound_to_the_checked_root(self) -> None:
        mine = r"C:\bench\ultra-magnus-agent.ps1"
        base = "-NoProfile -ExecutionPolicy Bypass -File %s" % mine
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

    def test_p5a1_a_relative_path_never_names_a_place(self) -> None:
        mine = r"C:\bench\ultra-magnus-agent.ps1"
        base = "-NoProfile -ExecutionPolicy Bypass -File %s" % mine
        # pass 5 A1: -Root . resolves against the TASK's working directory, which this process cannot see
        for name, root in (("dot", "."), ("dotdot", "..\\bench"), ("bare", "bench"), ("drive-relative", "C:bench"), ("tilde", "~")):
            self.assertEqual(self._exec_task_case("root-rel-" + name, "pwsh.exe", base + " -Root " + root), "False", root)
        # the same class at the other sites that compare paths: a relative path is never "the same" as the absolute one in this cwd
        body = (
            "Set-Location -LiteralPath '%s';"
            "'rel='+(Test-SamePath -A 'ultra-magnus-agent.ps1' -B '%s');"
            "'reldot='+(Test-SamePath -A '.' -B '%s');"
            "'abs='+(Test-SamePath -A '%s' -B '%s')"
        ) % (psq(self.venue), psq(self.venue / AGENT_NAME), psq(self.venue), psq(self.venue), psq(str(self.venue) + "\\"))
        r = self._dot_source(body)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.split(), ["rel=False", "reldot=False", "abs=True"], r.stdout + r.stderr)
        self.assertEqual(self._exec_task_case("script-rel", "pwsh.exe", "-NoProfile -ExecutionPolicy Bypass -File .\\ultra-magnus-agent.ps1"), "False")

    def test_p5a2_only_a_space_or_a_tab_separates_task_arguments(self) -> None:
        mine = r"C:\bench\ultra-magnus-agent.ps1"
        # pass 5 A2: U+00A0 is not a separator for CreateProcess; "-NoProfile<NBSP>-File" is ONE argument, so the script never runs
        self.assertEqual(self._exec_task_case("nbsp", "pwsh.exe", "-NoProfile&#160;-File %s" % mine), "False")
        self.assertEqual(self._exec_task_case("nbsp-after-file", "pwsh.exe", "-NoProfile -ExecutionPolicy Bypass -File&#160;%s" % mine), "False")
        self.assertEqual(self._exec_task_case("newline", "pwsh.exe", "-NoProfile&#10;-File %s" % mine), "False")
        self.assertEqual(self._exec_task_case("ideographic", "pwsh.exe", "-NoProfile&#12288;-File %s" % mine), "False")
        self.assertEqual(self._exec_task_case("tab", "pwsh.exe", "-NoProfile -ExecutionPolicy Bypass&#9;-File %s" % mine), "True")
        self.assertEqual(self._exec_task_case("two-spaces", "pwsh.exe", "-NoProfile -ExecutionPolicy Bypass  -File %s" % mine), "True")

    def test_p4a6_the_task_arguments_are_decoded_as_windows_decodes_them(self) -> None:
        mine = r"C:\bench\ultra-magnus-agent.ps1"
        # pass 4 A6: in -Root "C:\bench\" the backslash escapes the closing quote, so Root is C:\bench" (a literal quote), not C:\bench
        self.assertEqual(self._exec_task_case("quote-escaped", "pwsh.exe", "-NoProfile -ExecutionPolicy Bypass -File %s -Root &quot;C:\\bench\\&quot;" % mine), "False")
        # an unterminated or doubled quote is unreadable, so it refuses
        self.assertEqual(self._exec_task_case("unterminated", "pwsh.exe", "-NoProfile -ExecutionPolicy Bypass -File %s -Root &quot;C:\\bench" % mine), "False")
        self.assertEqual(self._exec_task_case("doubled", "pwsh.exe", "-NoProfile -ExecutionPolicy Bypass -File &quot;C:\\bench\\ultra-magnus-agent.ps1&quot;&quot; -Root C:\\bench"), "False")
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
            e = "<Exec><Command>pwsh.exe</Command><Arguments>-NoProfile -ExecutionPolicy Bypass -File %s</Arguments></Exec>"
            return "<Task><Actions>%s%s</Actions></Task>" % (e % first, e % second)

        # `schtasks /End` stops the whole task, so a busy first action is ended with the bench agent that was checked
        self.assertEqual(self._task_xml_case("two-busy-first", mine, two(busy, mine)), "False")
        self.assertEqual(self._task_xml_case("two-mine-first", mine, two(mine, busy)), "False")
        self.assertEqual(self._task_xml_case("two-same", mine, two(mine, mine)), "False")
        com = "<Task><Actions><Exec><Command>pwsh.exe</Command><Arguments>-NoProfile -ExecutionPolicy Bypass -File %s</Arguments></Exec><ComHandler><ClassId>{x}</ClassId></ComHandler></Actions></Task>" % mine
        self.assertEqual(self._task_xml_case("exec-and-com", mine, com), "False")
        one = "<Task><Actions Context=\"Author\"><Exec><Command>pwsh.exe</Command><Arguments>-NoProfile -ExecutionPolicy Bypass -File %s</Arguments></Exec></Actions></Task>" % mine
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
        pid, start = self.live_agent()
        agent = self.venue / AGENT_NAME
        good = 'alive x pid=%d host=BENCHTEST generation=1 processStartUtc=%s agentScript="%s"\r\n' % (pid, start, agent)
        # the same live pid, but the heartbeat now carries NO processStartUtc: it no longer identifies a process
        bad = 'alive x pid=%d host=BENCHTEST generation=1 agentScript="%s"\r\n' % (pid, agent)
        xml = Path(self._tmp.name) / "task-p4a5.xml"
        xml.write_text("<Task><Actions><Exec><Command>pwsh.exe</Command><Arguments>-NoProfile -ExecutionPolicy Bypass -File \"%s\"</Arguments></Exec></Actions></Task>" % agent, encoding="utf-8")
        beat = self.venue / "heartbeat.txt"
        later = Path(self._tmp.name) / "heartbeat-later.txt"
        beat.write_text(good)
        later.write_text(bad)
        cmd = (
            ". '%s' -VenueRoot '%s' -TaskName '\\MlvNoSuchTaskForTest' -TaskXmlFile '%s' -TaskPidsFile '%s'; "
            "$first = Get-RestartBindingRefusal -AgentScript '%s'; 'first=' + $(if ($null -eq $first) { 'bound' } else { $first });"
            "Copy-Item -LiteralPath '%s' -Destination '%s' -Force; "
            "$r = Invoke-AgentRestart -AgentScript '%s'; 'why=' + $r.why"
        ) % (psq(SCRIPT), psq(self.venue), psq(xml), psq(self.task_pids(pid)), psq(agent), psq(later), psq(beat), psq(agent))
        r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True, text=True, timeout=120, env=BENCH_ENV)
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

    # ---- adversary pass 6 findings (A1..A3 of dea7c6ce): each test failed on dea7c6ce ----

    def test_p6a1_a_rooted_path_without_a_drive_never_names_a_place_on_windows(self) -> None:
        # pass 6 A1: "-Root /bench" is resolved against the CURRENT DRIVE of the process that reads it, so the task may serve D:\bench
        # while C:\bench was checked idle. The switch is flipped inside one session so both readings run on any host.
        body = (
            "function T($p){ [string](Test-AbsolutePathText -P $p) };"
            "$script:OnWindows=$true;"
            "'win-slash='+(T '/bench'); 'win-backslash='+(T '\\bench'); 'win-drive='+(T 'C:\\bench'); 'win-drive-fwd='+(T 'C:/bench');"
            "'win-unc='+(T '\\\\srv\\share\\x'); 'win-unc-fwd='+(T '//srv/share/x'); 'win-lone-unc='+(T '\\\\'); 'win-rel='+(T 'bench'); 'win-drive-rel='+(T 'C:bench');"
            "$script:OnWindows=$false;"
            "'posix-slash='+(T '/bench'); 'posix-rel='+(T 'bench')"
        )
        r = self._dot_source(body)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(
            r.stdout.split(),
            ["win-slash=False", "win-backslash=False", "win-drive=True", "win-drive-fwd=True", "win-unc=True", "win-unc-fwd=True",
             "win-lone-unc=False", "win-rel=False", "win-drive-rel=False", "posix-slash=True", "posix-rel=False"], r.stdout + r.stderr)

    def test_p6a1_a_task_whose_root_or_script_has_no_drive_does_not_bind_on_windows(self) -> None:
        mine = r"C:\bench\ultra-magnus-agent.ps1"
        for name, args in (("root-slash", "-NoProfile -ExecutionPolicy Bypass -File %s -Root /bench" % mine), ("root-backslash", "-NoProfile -ExecutionPolicy Bypass -File %s -Root \\bench" % mine),
                           ("script-slash", "-NoProfile -ExecutionPolicy Bypass -File /bench/ultra-magnus-agent.ps1")):
            xml = "<Task><Actions><Exec><Command>pwsh.exe</Command><Arguments>%s</Arguments></Exec></Actions></Task>" % args
            f = Path(self._tmp.name) / ("task-p6a1-%s.xml" % name)
            f.write_text(xml, encoding="utf-8")
            r = self._dot_source("$script:OnWindows=$true; Test-TaskXmlRunsScript -Xml (Get-Content -Raw -LiteralPath '%s') -AgentScript '%s'" % (psq(f), psq(mine)))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(r.stdout.strip(), "False", name)
        # the same drive-less text is legitimate off Windows, where "/" is the root of the one filesystem
        xml = "<Task><Actions><Exec><Command>pwsh</Command><Arguments>-NoProfile -ExecutionPolicy Bypass -File /bench/ultra-magnus-agent.ps1 -Root /bench</Arguments></Exec></Actions></Task>"
        f = Path(self._tmp.name) / "task-p6a1-posix.xml"
        f.write_text(xml, encoding="utf-8")
        r = self._dot_source("$script:OnWindows=$false; Test-TaskXmlRunsScript -Xml (Get-Content -Raw -LiteralPath '%s') -AgentScript '/bench/ultra-magnus-agent.ps1'" % psq(f))
        self.assertEqual(r.stdout.strip(), "True", r.stdout + r.stderr)

    def test_p6a1_no_path_check_is_spelled_out_a_second_time(self) -> None:
        # sibling sweep: the inline drive-or-slash regex that sat in Test-TaskXmlRunsScript is the same decision as Test-AbsolutePathText
        code = "\n".join(l for l in SCRIPT.read_text(encoding="ascii").splitlines() if not l.lstrip().startswith("#"))
        self.assertEqual(code.count("[A-Za-z]:[\\\\/]"), 1, "one place decides what an absolute path is")

    def test_p6a2_the_replacement_must_start_after_run_with_no_tolerance(self) -> None:
        # pass 6 A2: a process that began 1 s before /Run is not the replacement (the old code let it in: start - 2 s tolerance)
        pid, s = self._sleeper()
        body = (
            "$s='%s'; $a='%s'; $sp=%d;"
            "function B($p,$st,$sc){ [pscustomobject]@{pid=$p;host=$env:COMPUTERNAME;start=$st;agentScript=$sc;ageSec=1} };"
            "$st=[DateTimeOffset]::Parse($s,[Globalization.CultureInfo]::InvariantCulture,[Globalization.DateTimeStyles]::AssumeUniversal).UtcDateTime;"
            "'started-1s-before-run='+(Test-NewHeartbeatIsLiveAgent -Beat (B $sp $s $a) -AgentScript $a -NotBeforeUtc $st.AddSeconds(1));"
            "'started-0.5s-before-run='+(Test-NewHeartbeatIsLiveAgent -Beat (B $sp $s $a) -AgentScript $a -NotBeforeUtc $st.AddSeconds(0.5));"
            "'started-after-run='+(Test-NewHeartbeatIsLiveAgent -Beat (B $sp $s $a) -AgentScript $a -NotBeforeUtc $st.AddSeconds(-0.5));"
            # the heartbeat CLAIMS a later start (within the same-process tolerance) than the OS reports: the OS value decides
            "$claim=$st.AddSeconds(1.5).ToString('o');"
            "'claim-later-than-os='+(Test-NewHeartbeatIsLiveAgent -Beat (B $sp $claim $a) -AgentScript $a -NotBeforeUtc $st.AddSeconds(1))"
        ) % (s, psq(self.venue / AGENT_NAME), pid)
        r = self._dot_source(body)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(
            r.stdout.split(),
            ["started-1s-before-run=False", "started-0.5s-before-run=False", "started-after-run=True", "claim-later-than-os=False"], r.stdout + r.stderr)

    def test_p6a2_the_started_after_run_compare_has_no_tolerance_term(self) -> None:
        code = "\n".join(l for l in SCRIPT.read_text(encoding="ascii").splitlines() if not l.lstrip().startswith("#"))
        self.assertIsNone(re.search(r"AddSeconds\(-\$script:StartToleranceSec\)", code), "no tolerance subtracted from a 'newer than' bound")

    def test_p6a3_the_live_process_command_line_is_what_binds(self) -> None:
        # pass 6 A3: the task DEFINITION says -File <agent> (bound), but the RUNNING instance was started with -Root C:\other-root, so
        # ending the task stops an agent serving another root. The live command line is read from the OS and must bind too.
        pid, start = self.live_agent("-Root", r"C:\other-root")
        self.write_beat(pid, start)
        xml = Path(self._tmp.name) / "task-p6a3.xml"
        xml.write_text("<Task><Actions><Exec><Command>pwsh.exe</Command><Arguments>-NoProfile -ExecutionPolicy Bypass -File \"%s\"</Arguments></Exec></Actions></Task>"
                       % (self.venue / AGENT_NAME), encoding="utf-8")
        r = self.run_deploy("-RestartMode", "when-idle", "-TaskXmlFile", str(xml))
        self.assertEqual(r.returncode, 16, r.stdout + r.stderr)
        self.assertIn("DEPLOY_REFUSED_RESTART_HOST", r.stdout)
        self.assertIn("pid=%d" % pid, r.stdout)
        self.assertIn("command line", r.stdout)
        self.assert_untouched()

    def test_p6a3_a_live_process_serving_this_root_binds_and_others_do_not(self) -> None:
        xml = Path(self._tmp.name) / "task-p6a3b.xml"
        xml.write_text("<Task><Actions><Exec><Command>pwsh.exe</Command><Arguments>-NoProfile -ExecutionPolicy Bypass -File \"%s\"</Arguments></Exec></Actions></Task>"
                       % (self.venue / AGENT_NAME), encoding="utf-8")
        agent = self.venue / AGENT_NAME
        verdicts = {}
        for name, args in (("no-root-arg", ()), ("same-root", ("-Root", str(self.venue))), ("other-root", ("-Root", r"C:\other-root")),
                           ("positional-root", (r"C:\other-root",)), ("verbose", ("-Verbose",))):
            pid, start = self.live_agent(*args)
            self.write_beat(pid, start)
            cmd = (". '%s' -VenueRoot '%s' -TaskXmlFile '%s' -TaskPidsFile '%s'; $x = Get-RestartBindingRefusal -AgentScript '%s'; if ($null -eq $x) { 'bound' } else { 'refused:' + $x }"
                   % (psq(SCRIPT), psq(self.venue), psq(xml), psq(self.task_pids(pid)), psq(agent)))
            r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True, text=True, timeout=120, env=BENCH_ENV)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            verdicts[name] = r.stdout.strip().split(":")[0]
        self.assertEqual(verdicts, {"no-root-arg": "bound", "same-root": "bound", "other-root": "refused", "positional-root": "refused", "verbose": "refused"})

    def test_p6a3_a_process_whose_command_line_cannot_be_read_is_refused_not_assumed(self) -> None:
        # the pid is gone (or unreadable): the root it served is unconfirmed, so the answer is a refusal, never "bound"
        done = subprocess.run([PWSH, "-NoProfile", "-Command", "$PID"], capture_output=True, text=True, timeout=60)
        r = self._dot_source("$a = Get-ProcessArgvBounded -ProcessId %d; if ($null -eq $a) { 'null' } else { 'ok=' + $a.ok }" % int(done.stdout.strip()))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(r.stdout.strip(), ("null", "ok=False"))

    def test_p6a3_the_replacement_is_also_checked_by_its_own_command_line(self) -> None:
        # sibling of A3 at the OTHER place the live agent is named: the replacement that /Run started must itself run this script and root
        pid_ok, s_ok = self.live_agent()
        pid_other, s_other = self.live_agent("-Root", r"C:\other-root")
        agent = psq(self.venue / AGENT_NAME)
        body = (
            "$a='%s';"
            "function B($p,$st){ [pscustomobject]@{pid=$p;host=$env:COMPUTERNAME;start=$st;agentScript=$a;ageSec=1} };"
            "'ok='+(Test-NewHeartbeatIsLiveAgent -Beat (B %d '%s') -AgentScript $a -CheckCommandLine);"
            "'other-root='+(Test-NewHeartbeatIsLiveAgent -Beat (B %d '%s') -AgentScript $a -CheckCommandLine);"
            "'other-root-unchecked='+(Test-NewHeartbeatIsLiveAgent -Beat (B %d '%s') -AgentScript $a)"
        ) % (agent, pid_ok, s_ok, pid_other, s_other, pid_other, s_other)
        r = self._dot_source(body)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.split(), ["ok=True", "other-root=False", "other-root-unchecked=True"], r.stdout + r.stderr)

    # ---- adversary pass 7 findings (A1..A2 of 606e89a1): each test failed on 606e89a18 ----

    def _task_xml_for_agent(self) -> Path:
        xml = Path(self._tmp.name) / "task-p7.xml"
        xml.write_text("<Task><Actions><Exec><Command>pwsh.exe</Command><Arguments>-NoProfile -ExecutionPolicy Bypass -File \"%s\"</Arguments></Exec></Actions></Task>"
                       % (self.venue / AGENT_NAME), encoding="utf-8")
        return xml

    def test_p7a2_the_heartbeat_pid_must_be_the_pid_of_the_tasks_running_instance(self) -> None:
        # pass 7 A2: PID 4100 (interactive session) writes a valid matching heartbeat for this root; the task's RUNNING instance is PID
        # 4200 serving a busy root. Every earlier check passes for 4100; ending the task would stop 4200. The scheduler is asked who runs.
        pid, start = self.live_agent()
        self.write_beat(pid, start)
        xml = self._task_xml_for_agent()
        for label, pids, needle in (("other-instance", (4200,), "not heartbeat pid=%d" % pid), ("no-instance", (), "0 running instance"),
                                    ("two-instances", (pid, 4200), "2 running instance")):
            r = self.run_deploy("-RestartMode", "when-idle", "-TaskXmlFile", str(xml), "-TaskPidsFile", str(self.task_pids(*pids, name=label)))
            self.assertEqual(r.returncode, 16, label + r.stdout + r.stderr)
            self.assertIn("DEPLOY_REFUSED_RESTART_HOST", r.stdout)
            self.assertIn(needle, r.stdout, label)
            self.assert_untouched()
        # the scheduler cannot be asked (an unreadable seam stands for a COM failure, and for any non-Windows host): refuse, never assume
        r = self.run_deploy("-RestartMode", "when-idle", "-TaskXmlFile", str(xml), "-TaskPidsFile", str(Path(self._tmp.name) / "no-such-file.txt"))
        self.assertEqual(r.returncode, 16, r.stdout + r.stderr)
        self.assertIn("could not be read from the Task Scheduler", r.stdout)
        self.assert_untouched()

    def test_p7a2_the_task_instance_is_the_same_pid_it_is_bound_to_end_to_end(self) -> None:
        # the matching case: a bound agent whose task instance IS the heartbeat pid passes the binding (it would go on to schtasks, so
        # only the binding function is run here)
        pid, start = self.live_agent()
        self.write_beat(pid, start)
        cmd = (". '%s' -VenueRoot '%s' -TaskXmlFile '%s' -TaskPidsFile '%s'; $x = Get-RestartBindingRefusal -AgentScript '%s'; if ($null -eq $x) { 'bound' } else { 'refused:' + $x }"
               % (psq(SCRIPT), psq(self.venue), psq(self._task_xml_for_agent()), psq(self.task_pids(pid)), psq(self.venue / AGENT_NAME)))
        r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True, text=True, timeout=120, env=BENCH_ENV)
        self.assertEqual(r.stdout.strip(), "bound", r.stdout + r.stderr)

    def test_p7a2_the_replacement_must_be_the_tasks_new_instance(self) -> None:
        pid, s = self._sleeper()
        body = (
            "$a='%s'; $s='%s'; $sp=%d;"
            "function B($p,$st,$sc){ [pscustomobject]@{pid=$p;host=$env:COMPUTERNAME;start=$st;agentScript=$sc;ageSec=1} };"
            "'instance='+(Test-NewHeartbeatIsLiveAgent -Beat (B $sp $s $a) -AgentScript $a -TaskPids @($sp));"
            "'other-instance='+(Test-NewHeartbeatIsLiveAgent -Beat (B $sp $s $a) -AgentScript $a -TaskPids @(4200));"
            "'no-instance='+(Test-NewHeartbeatIsLiveAgent -Beat (B $sp $s $a) -AgentScript $a -TaskPids @());"
            "'not-asked='+(Test-NewHeartbeatIsLiveAgent -Beat (B $sp $s $a) -AgentScript $a)"
        ) % (psq(self.venue / AGENT_NAME), s, pid)
        r = self._dot_source(body)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.split(), ["instance=True", "other-instance=False", "no-instance=False", "not-asked=True"], r.stdout + r.stderr)

    def test_p7a1_idle_is_judged_again_right_before_the_end(self) -> None:
        # pass 7 A1: the binding reads the heartbeat, a command line, the task XML and the scheduler (each up to a deadline), so a job can
        # land AFTER the idle check that preceded it. The venue is idle on entry; the binding (replaced here by a stand-in that drops a
        # job into inbox\ and succeeds, as a slow query would allow) must be followed by a fresh idle check, and nothing may be stopped.
        pid, start = self.live_agent()
        self.write_beat(pid, start)
        body = (
            "$b=[pscustomobject]@{pid=%d;host=$env:COMPUTERNAME;start='%s';agentScript='%s';ageSec=1};"
            "function Get-RestartBindingRefusal { param($AgentScript) $script:BoundBeat=$b; Set-Content -LiteralPath (Join-Path (Join-Path $VenueRoot 'inbox') 'j1.job.ps1') -Value x; return $null };"
            "$r = Invoke-AgentRestart -AgentScript '%s'; 'why=' + $r.why"
        ) % (pid, start, psq(self.venue / AGENT_NAME), psq(self.venue / AGENT_NAME))
        cmd = ". '%s' -VenueRoot '%s' -TaskName '\\MlvNoSuchTaskForTest'; %s" % (psq(SCRIPT), psq(self.venue), body)
        r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True, text=True, timeout=120, env=BENCH_ENV)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("why=the venue is no longer idle, so the agent was not stopped", r.stdout)
        self.assertIn("inbox=1", r.stdout)

    # ---- adversary pass 8 findings (A1..A2 of d6311001): each test failed on d6311001e ----

    def test_p8a1_a_host_execution_policy_that_blocks_the_script_is_refused(self) -> None:
        # pass 8 A1: -ExecutionPolicy Restricted passed the allowlist, so the agent was stopped and its replacement could not run
        mine = r"C:\bench\ultra-magnus-agent.ps1"
        for pol in ("Restricted", "AllSigned", "Default", "Undefined", "restricted"):
            for flag in ("-ExecutionPolicy", "-ep"):
                self.assertEqual(self._exec_task_case("pol-" + pol + flag, "pwsh.exe", "-NoProfile %s %s -File %s" % (flag, pol, mine)), "False", pol + flag)
        for pol in ("Bypass", "RemoteSigned", "Unrestricted", "bypass"):
            self.assertEqual(self._exec_task_case("pol-ok-" + pol, "pwsh.exe", "-NoProfile -ExecutionPolicy %s -File %s" % (pol, mine)), "True", pol)
        code = "\n".join(l for l in SCRIPT.read_text(encoding="ascii").splitlines() if not l.lstrip().startswith("#"))
        self.assertEqual(len(re.findall(r"\$policies\s*=", code)), 1, "one allowlist decides, for the definition and the live process alike")

    def test_p8a1_the_live_process_is_held_to_the_same_host_option_allowlist(self) -> None:
        mine = r"C:\bench\ultra-magnus-agent.ps1"
        body = (
            "function A($t){ [pscustomobject]@{ ok = $true; tokens = $t } };"
            "'restricted='+(Test-ArgvBindsScript -Argv (A @('pwsh.exe','-NoProfile','-ExecutionPolicy','Restricted','-File','%s')) -AgentScript '%s');"
            "'bypass='+(Test-ArgvBindsScript -Argv (A @('pwsh.exe','-NoProfile','-ExecutionPolicy','Bypass','-File','%s')) -AgentScript '%s')"
        ) % (psq(mine), psq(mine), psq(mine), psq(mine))
        r = self._dot_source(body)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.split(), ["restricted=False", "bypass=True"], r.stdout + r.stderr)

    def test_p8a2_the_stop_acts_on_the_proved_instance_never_on_a_task_name(self) -> None:
        # pass 8 A2: `schtasks /End /TN <name>` stops whatever the name resolves to at that moment (another admin re-pointed the task and
        # started another instance after the binding). The stop is now an instance stop by the proved pid; here the scheduler has no such
        # instance, so nothing is stopped, the answer is immediate, and the live agent is untouched.
        pid, start = self.live_agent()
        body = (
            "$b=[pscustomobject]@{pid=%d;host=$env:COMPUTERNAME;start='%s';agentScript='%s';ageSec=1};"
            "function Get-RestartBindingRefusal { param($AgentScript) $script:BoundBeat=$b; return $null };"
            "$t=[Diagnostics.Stopwatch]::StartNew(); $r = Invoke-AgentRestart -AgentScript '%s';"
            "'why=' + $r.why; 'fast=' + ($t.Elapsed.TotalSeconds -lt 25); 'alive=' + [bool](Get-Process -Id %d -ErrorAction SilentlyContinue)"
        ) % (pid, start, psq(self.venue / AGENT_NAME), psq(self.venue / AGENT_NAME), pid)
        cmd = ". '%s' -VenueRoot '%s' -TaskName '\\MlvNoSuchTaskForTest'; %s" % (psq(SCRIPT), psq(self.venue), body)
        r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True, text=True, timeout=120, env=BENCH_ENV)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("nothing was stopped", r.stdout)
        self.assertIn("fast=True", r.stdout)
        self.assertIn("alive=True", r.stdout)
        code = "\n".join(l for l in SCRIPT.read_text(encoding="ascii").splitlines() if not l.lstrip().startswith("#"))
        self.assertNotRegex(code, r"'/End'", "no `schtasks /End /TN <name>`: the stop is an instance stop by pid")

    def test_p8a2_the_task_definition_is_read_again_right_before_it_is_started(self) -> None:
        # pass 8 A2, second half: a definition edited after the binding is launched by /Run before any attestation. It is re-read as the
        # last step before the start; a definition that no longer runs this script is not started (the old agent is down, and said so).
        pid, start = self.live_agent()
        busy = Path(self._tmp.name) / "task-busy.xml"
        busy.write_text("<Task><Actions><Exec><Command>pwsh.exe</Command><Arguments>-NoProfile -ExecutionPolicy Bypass -File C:\\busy\\ultra-magnus-agent.ps1</Arguments></Exec></Actions></Task>", encoding="utf-8")
        body = (
            "$b=[pscustomobject]@{pid=%d;host=$env:COMPUTERNAME;start='%s';agentScript='%s';ageSec=1};"
            "function Get-RestartBindingRefusal { param($AgentScript) $script:BoundBeat=$b; return $null };"
            # stand-in for the scheduler's instance stop (a real one needs the real task): it ends the live agent, as a stop would
            "function Stop-TaskInstanceBounded { param([int]$ProcessId, [string]$StartText) Stop-Process -Id $ProcessId -Force; [pscustomobject]@{ outcome = 'stopped'; why = $null; busy = $null } };"
            "$r = Invoke-AgentRestart -AgentScript '%s'; 'why=' + $r.why"
        ) % (pid, start, psq(self.venue / AGENT_NAME), psq(self.venue / AGENT_NAME))
        # the scheduler has no running instance (the -TaskPidsFile seam: off Windows there is no Task Scheduler to ask, and without it the
        # idle wait would end the restart before the definition is re-read)
        cmd = ". '%s' -VenueRoot '%s' -TaskName '\\MlvNoSuchTaskForTest' -TaskXmlFile '%s' -TaskPidsFile '%s'; %s" % (
            psq(SCRIPT), psq(self.venue), psq(busy), psq(self.task_pids(name="p8a2-idle")), body)
        r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True, text=True, timeout=120, env=BENCH_ENV)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("the old agent is stopped but the task was NOT started", r.stdout)
        self.assertIn("its definition changed", r.stdout)

    # ---- adversary pass 9 findings (A1..A3 of eb16397b): each test failed on eb16397b ----
    # The Task Scheduler is replaced by a stand-in Get-RunningTaskInstances (the one seam): it returns instance objects with the same
    # members the COM IRunningTask has (Path, EnginePID, Stop()). Stop() on the stand-in writes a marker file, so "was it stopped" is observed.

    def _stop_case(self, label: str, *, start_shift_sec: float = 0, drop_job_during_lookup: bool = False, task_path: str = "\\MlvTest"):
        """Runs Stop-TaskInstanceByPid directly against a live process, with a stand-in scheduler. Returns (stdout lines dict, marker Path)."""
        pid, _start = self.live_agent()
        marker = Path(self._tmp.name) / ("stop-marker-%s.txt" % label)
        job = self.venue / "inbox" / "j1.job.ps1"
        body = (
            "$script:Marker='%s'; $script:FakePid=%d; $script:Job='%s'; $script:DropJob=$%s; $script:FakePath='%s';"
            "function Get-RunningTaskInstances {"
            "  if ($script:DropJob) { Set-Content -LiteralPath $script:Job -Value x };"   # work lands while the scheduler is being asked
            "  $o=[pscustomobject]@{ Path=$script:FakePath; EnginePID=$script:FakePid };"
            "  Add-Member -InputObject $o -MemberType ScriptMethod -Name Stop -Value { Set-Content -LiteralPath $script:Marker -Value stopped };"
            "  @($o) };"
            "$real=(Get-Process -Id %d).StartTime.ToUniversalTime();"
            "$claimed=$real.AddSeconds(%s).ToString('o');"
            "$r = Stop-TaskInstanceByPid -ProcessId %d -StartText $claimed;"
            "'outcome=' + $r.outcome; 'why=' + $r.why; 'busy=' + (@($r.busy) -join '|')"
        ) % (psq(marker), pid, psq(job), "true" if drop_job_during_lookup else "false", psq(task_path), pid, start_shift_sec, pid)
        cmd = ". '%s' -VenueRoot '%s' -TaskName '\\MlvTest'; %s" % (psq(SCRIPT), psq(self.venue), body)
        r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True, text=True, timeout=120, env=BENCH_ENV)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = {}
        for line in r.stdout.splitlines():
            k, _, v = line.partition("=")
            out[k] = v
        return out, marker

    def test_p9a1_a_reused_pid_with_another_start_is_not_stopped(self) -> None:
        # pass 9 A1: the binding proved task \MlvAgentBachelor pid P AND its start; that instance exited, the task was re-pointed, and a
        # NEW instance got the same pid P ten seconds later. Pid plus task path matched, so the new instance was stopped.
        out, marker = self._stop_case("reused", start_shift_sec=-10)
        self.assertEqual(out.get("outcome"), "not-stopped", out)
        self.assertFalse(marker.exists(), "an instance whose start differs from the proved one is never stopped")

    def test_p9a1_the_instance_with_the_proved_pid_and_start_is_stopped(self) -> None:
        out, marker = self._stop_case("same")
        self.assertEqual(out.get("outcome"), "stopped", out)
        self.assertTrue(marker.exists(), "the proved instance is stopped")
        # (pass 11 A2 / pass 13 A1: the stop compares two OS reads of ONE process, so a 2 s heartbeat-sized tolerance does not apply, and on
        # Windows the two reads are identical to the tick: see test_r5_a1_a_pid_reused_fifty_ms_later_...)

    def test_p11a2_a_pid_reused_half_a_second_later_is_not_the_proved_instance(self) -> None:
        # pass 11 A2: the binding proved pid 4242 at 12:00:00.0; a re-pointed instance reuses pid 4242 at 12:00:00.5. The heartbeat's 2 s
        # tolerance admitted it. The stop is handed the OS-observed start and compares two reads of one process (100 ms).
        out, marker = self._stop_case("half-second", start_shift_sec=-0.5)
        self.assertEqual(out.get("outcome"), "not-stopped", out)
        self.assertFalse(marker.exists())
        out, marker = self._stop_case("one-second", start_shift_sec=1)
        self.assertEqual(out.get("outcome"), "not-stopped", out)

    def test_p11a2_the_binding_records_the_os_start_and_the_caller_hands_it_to_the_stop(self) -> None:
        pid, start = self.live_agent()
        self.write_beat(pid, start)
        xml = self._task_xml_for_agent()
        cmd = (". '%s' -VenueRoot '%s' -TaskXmlFile '%s' -TaskPidsFile '%s'; $x = Get-RestartBindingRefusal -AgentScript '%s';"
               "'bound=' + ($null -eq $x); 'os=' + $script:BoundBeat.osStart; 'real=' + (Get-Process -Id %d).StartTime.ToUniversalTime().ToString('o')"
               % (psq(SCRIPT), psq(self.venue), psq(xml), psq(self.task_pids(pid)), psq(self.venue / AGENT_NAME), pid))
        r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True, text=True, timeout=120, env=BENCH_ENV)
        vals = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
        self.assertEqual(vals.get("bound"), "True", r.stdout + r.stderr)
        self.assertEqual(vals.get("os"), vals.get("real"), "the bound identity carries the start the OS reports for the pid")
        body = (
            "$b=[pscustomobject]@{pid=%d;host=$env:COMPUTERNAME;start='%s';osStart='2026-11-18T12:00:00.1234567Z';agentScript='%s';ageSec=1};"
            "function Get-RestartBindingRefusal { param($AgentScript) $script:BoundBeat=$b; return $null };"
            "function Stop-TaskInstanceBounded { param([int]$ProcessId, [string]$StartText) Write-Host ('seen start=' + $StartText);"
            "  [pscustomobject]@{ outcome = 'not-stopped'; why = 'stand-in'; busy = $null } };"
            "$r = Invoke-AgentRestart -AgentScript '%s'"
        ) % (pid, start, psq(self.venue / AGENT_NAME), psq(self.venue / AGENT_NAME))
        r = self._dot_source(body)
        self.assertIn("seen start=2026-11-18T12:00:00.1234567Z", r.stdout)

    def test_p11a1_the_definition_is_read_again_after_the_wait_for_the_scheduler(self) -> None:
        # pass 11 A1: the definition was re-read, THEN the restart waited (up to 15 s) for the scheduler to drop the old instance, and
        # /Run started whatever the definition said by then. During that wait another admin re-points the task to another root.
        pid, start = self.live_agent()
        agent = self.venue / AGENT_NAME
        good = Path(self._tmp.name) / "task-p11a1.xml"
        good.write_text("<Task><Actions><Exec><Command>pwsh.exe</Command><Arguments>-NoProfile -ExecutionPolicy Bypass -File \"%s\"</Arguments></Exec></Actions></Task>" % agent, encoding="utf-8")
        busy = "<Task><Actions><Exec><Command>pwsh.exe</Command><Arguments>-NoProfile -ExecutionPolicy Bypass -File C:\\busy\\ultra-magnus-agent.ps1</Arguments></Exec></Actions></Task>"
        body = (
            "$b=[pscustomobject]@{pid=%d;host=$env:COMPUTERNAME;start='%s';agentScript='%s';ageSec=1};"
            "function Get-RestartBindingRefusal { param($AgentScript) $script:BoundBeat=$b; return $null };"
            "function Stop-TaskInstanceBounded { param([int]$ProcessId, [string]$StartText) Stop-Process -Id $ProcessId -Force; [pscustomobject]@{ outcome = 'stopped'; why = $null; busy = $null } };"
            # the scheduler-idle wait is where the definition is edited
            "$orig = ${function:Wait-Until};"
            "function Wait-Until { param([int]$TimeoutSec, [scriptblock]$Condition, [int]$PollMs = 500, [hashtable]$Variables = @{})"
            "  if ($Condition.ToString() -match 'Get-TaskRunningPids') { Set-Content -LiteralPath $TaskXmlFile -Value '%s'; return $true }"
            "  & $orig -TimeoutSec $TimeoutSec -Condition $Condition -PollMs $PollMs -Variables $Variables };"
            "$r = Invoke-AgentRestart -AgentScript '%s'; 'why=' + $r.why"
        ) % (pid, start, psq(agent), psq(busy), psq(agent))
        cmd = ". '%s' -VenueRoot '%s' -TaskName '\\MlvNoSuchTaskForTest' -TaskXmlFile '%s'; %s" % (psq(SCRIPT), psq(self.venue), psq(good), body)
        r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True, text=True, timeout=120, env=BENCH_ENV)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("its definition changed", r.stdout)
        self.assertNotIn("schtasks /Run", r.stdout, "the changed definition is not started")

    def test_p11a4_a_task_without_an_explicit_execution_policy_is_refused(self) -> None:
        # pass 11 A4: with no -ExecutionPolicy the host applies the account's persistent policy, which can be Restricted now though the
        # running agent started under another: the old agent is stopped and the replacement script is blocked.
        mine = r"C:\bench\ultra-magnus-agent.ps1"
        self.assertEqual(self._exec_task_case("p11-nopolicy", "pwsh.exe", "-NoProfile -File %s" % mine), "False")
        self.assertEqual(self._exec_task_case("p11-nopolicy-nologo", "pwsh.exe", "-NoProfile -NoLogo -NonInteractive -File %s" % mine), "False")
        self.assertEqual(self._exec_task_case("p11-policy", "pwsh.exe", "-NoProfile -ExecutionPolicy RemoteSigned -File %s" % mine), "True")
        self.assertEqual(self._exec_task_case("p11-ep", "pwsh.exe", "-nop -ep Bypass -File %s" % mine), "True")
        body = (
            "function A($t){ [pscustomobject]@{ ok = $true; tokens = $t } };"
            "'nopolicy='+(Test-ArgvBindsScript -Argv (A @('pwsh.exe','-NoProfile','-File','%s')) -AgentScript '%s');"
            "'policy='+(Test-ArgvBindsScript -Argv (A @('pwsh.exe','-NoProfile','-ExecutionPolicy','Bypass','-File','%s')) -AgentScript '%s')"
        ) % (psq(mine), psq(mine), psq(mine), psq(mine))
        r = self._dot_source(body)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.split(), ["nopolicy=False", "policy=True"], r.stdout + r.stderr)

    def test_p9a1_the_caller_hands_the_proved_start_to_the_stop(self) -> None:
        pid, start = self.live_agent()
        body = (
            "$b=[pscustomobject]@{pid=%d;host=$env:COMPUTERNAME;start='%s';agentScript='%s';ageSec=1};"
            "function Get-RestartBindingRefusal { param($AgentScript) $script:BoundBeat=$b; return $null };"
            "function Stop-TaskInstanceBounded { param([int]$ProcessId, [string]$StartText) Write-Host ('seen pid=' + $ProcessId + ' start=' + $StartText);"
            "  [pscustomobject]@{ outcome = 'not-stopped'; why = 'stand-in'; busy = $null } };"
            "$r = Invoke-AgentRestart -AgentScript '%s'; 'why=' + $r.why"
        ) % (pid, start, psq(self.venue / AGENT_NAME), psq(self.venue / AGENT_NAME))
        cmd = ". '%s' -VenueRoot '%s' -TaskName '\\MlvNoSuchTaskForTest'; %s" % (psq(SCRIPT), psq(self.venue), body)
        r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True, text=True, timeout=120, env=BENCH_ENV)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("pid=%d start=%s" % (pid, start), r.stdout)

    def test_p9a3_idle_is_judged_after_the_scheduler_lookup_and_right_before_the_stop(self) -> None:
        # pass 9 A3: idle was read, then the scheduler lookup took a while, and a job landed in inbox\ meanwhile. The matching instance
        # is in hand and the venue is NOT idle any more: nothing may be stopped.
        out, marker = self._stop_case("late-job", drop_job_during_lookup=True)
        self.assertEqual(out.get("outcome"), "not-stopped", out)
        self.assertIn("inbox=1", out.get("busy", ""), out)
        self.assertFalse(marker.exists(), "a job that arrived during the lookup stops the stop")

    def test_p9a3_the_caller_says_busy_when_the_stop_found_the_venue_busy(self) -> None:
        pid, start = self.live_agent()
        body = (
            "$b=[pscustomobject]@{pid=%d;host=$env:COMPUTERNAME;start='%s';agentScript='%s';ageSec=1};"
            "function Get-RestartBindingRefusal { param($AgentScript) $script:BoundBeat=$b; return $null };"
            "function Stop-TaskInstanceBounded { param([int]$ProcessId, [string]$StartText)"
            "  [pscustomobject]@{ outcome = 'not-stopped'; why = 'the venue is busy'; busy = @('DEPLOY_REFUSED_BUSY running=0 inbox=1') } };"
            "$r = Invoke-AgentRestart -AgentScript '%s'; 'why=' + $r.why"
        ) % (pid, start, psq(self.venue / AGENT_NAME), psq(self.venue / AGENT_NAME))
        cmd = ". '%s' -VenueRoot '%s' -TaskName '\\MlvNoSuchTaskForTest'; %s" % (psq(SCRIPT), psq(self.venue), body)
        r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True, text=True, timeout=120, env=BENCH_ENV)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("why=the venue is no longer idle, so the agent was not stopped", r.stdout)
        self.assertIn("inbox=1", r.stdout)

    def _unanswered_stop_prelude(self, pid: int, kill: bool) -> str:
        """A stand-in for the bounded runner: the Task Scheduler stop call never answers within the deadline. With kill=True the stop WAS
        applied (late), as a COM call that outlives the worker timeout can be; every other bounded call passes through to the real one."""
        return (
            "$orig = ${function:Invoke-Bounded};"
            "function Invoke-Bounded { param([scriptblock]$Script, [long]$TimeoutMs, [hashtable]$Variables = @{})"
            "  if ($Script.ToString() -match 'Stop-TaskInstanceByPid') {"
            + ("    Stop-Process -Id %d -Force; Start-Sleep -Milliseconds 300;" % pid if kill else "")
            + "    return [pscustomobject]@{ timedOut = $true; error = $null; ok = $false; value = $null } }"
            "  & $orig -Script $Script -TimeoutMs $TimeoutMs -Variables $Variables };"
        )

    def test_p9a2_a_stop_that_did_not_answer_is_unknown_not_nothing_stopped(self) -> None:
        pid, start = self.live_agent()
        body = (
            "$b=[pscustomobject]@{pid=%d;host=$env:COMPUTERNAME;start='%s';agentScript='%s';ageSec=1};"
            "function Get-RestartBindingRefusal { param($AgentScript) $script:BoundBeat=$b; return $null };"
            "%s"
            "$s = Stop-TaskInstanceBounded -ProcessId %d -StartText '%s'; 'outcome=' + $s.outcome; 'why=' + $s.why"
        ) % (pid, start, psq(self.venue / AGENT_NAME), self._unanswered_stop_prelude(pid, kill=False), pid, start)
        r = self._dot_source(body)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("outcome=unknown", r.stdout)
        self.assertNotIn("nothing was stopped", r.stdout)

    def test_p9a2_an_unanswered_stop_that_was_applied_goes_on_to_the_start_gates(self) -> None:
        # pass 9 A2: IRunningTask.Stop was applied after 1 s but its COM answer outlived the 30 s worker timeout. The old agent IS down;
        # saying "nothing was stopped" and skipping the start leaves the venue with no agent. The pid is re-observed, and the restart
        # carries on to the definition re-read and the start (here the definition names another root, so the start is refused: that
        # message is the proof that the restart went past the stop).
        pid, start = self.live_agent()
        busy = Path(self._tmp.name) / "task-busy-p9.xml"
        busy.write_text("<Task><Actions><Exec><Command>pwsh.exe</Command><Arguments>-NoProfile -ExecutionPolicy Bypass -File C:\\busy\\ultra-magnus-agent.ps1</Arguments></Exec></Actions></Task>", encoding="utf-8")
        body = (
            "$b=[pscustomobject]@{pid=%d;host=$env:COMPUTERNAME;start='%s';agentScript='%s';ageSec=1};"
            "function Get-RestartBindingRefusal { param($AgentScript) $script:BoundBeat=$b; return $null };"
            "%s"
            "$r = Invoke-AgentRestart -AgentScript '%s'; 'why=' + $r.why"
        ) % (pid, start, psq(self.venue / AGENT_NAME), self._unanswered_stop_prelude(pid, kill=True), psq(self.venue / AGENT_NAME))
        cmd = ". '%s' -VenueRoot '%s' -TaskName '\\MlvNoSuchTaskForTest' -TaskXmlFile '%s' -TaskPidsFile '%s'; %s" % (
            psq(SCRIPT), psq(self.venue), psq(busy), psq(self.task_pids(name="p9a2-idle")), body)
        r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True, text=True, timeout=120, env=BENCH_ENV)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("nothing was stopped", r.stdout)
        self.assertIn("the old agent is stopped but the task was NOT started", r.stdout)
        self.assertIn("its definition changed", r.stdout, "the restart went past the stop AND the scheduler-idle wait to the definition re-read")

    def test_p9a2_an_unanswered_stop_with_the_agent_still_alive_says_unknown_not_nothing(self) -> None:
        pid, start = self.live_agent()
        body = (
            "$b=[pscustomobject]@{pid=%d;host=$env:COMPUTERNAME;start='%s';agentScript='%s';ageSec=1};"
            "function Get-RestartBindingRefusal { param($AgentScript) $script:BoundBeat=$b; return $null };"
            "%s"
            "$script:StopWaitSec = 2;"   # the wait for the agent to go is shortened for the test
            "$r = Invoke-AgentRestart -AgentScript '%s'; 'why=' + $r.why; 'alive=' + [bool](Get-Process -Id %d -ErrorAction SilentlyContinue)"
        ) % (pid, start, psq(self.venue / AGENT_NAME), self._unanswered_stop_prelude(pid, kill=False), psq(self.venue / AGENT_NAME), pid)
        r = self._dot_source(body)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("nothing was stopped", r.stdout)
        self.assertIn("UNKNOWN", r.stdout)
        self.assertIn("pid=%d" % pid, r.stdout)
        self.assertIn("alive=True", r.stdout)

    def test_p9_a_stop_that_never_reached_the_scheduler_still_says_nothing_was_stopped(self) -> None:
        # the three-valued answer keeps its middle: a stop that found no instance (and so never called Stop()) is 'not-stopped'
        out, marker = self._stop_case("no-such-path", task_path="\\Other")
        self.assertEqual(out.get("outcome"), "not-stopped", out)
        self.assertFalse(marker.exists())

    # ---- adversary pass 10 findings (A1..A3 of ca2a5a75): each test failed on ca2a5a750 ----

    def test_p10a1_a_host_that_loads_a_profile_does_not_bind_the_root(self) -> None:
        # pass 10 A1: with no -NoProfile the host loads a profile, and a profile can set PSDefaultParameterValues['ultra-magnus-agent.ps1:Root'],
        # so a task or live argv that names NO -Root can serve another root than the one checked idle. -NoProfile is required (definition and live argv).
        mine = r"C:\bench\ultra-magnus-agent.ps1"
        self.assertEqual(self._exec_task_case("p10-profile", "pwsh.exe", "-ExecutionPolicy Bypass -File %s" % mine), "False")
        self.assertEqual(self._exec_task_case("p10-bare", "pwsh.exe", "-File %s" % mine), "False")
        self.assertEqual(self._exec_task_case("p10-noprofile", "pwsh.exe", "-NoProfile -ExecutionPolicy Bypass -File %s" % mine), "True")
        self.assertEqual(self._exec_task_case("p10-nop", "pwsh.exe", "-nop -ep Bypass -File %s" % mine), "True")
        body = (
            "function A($t){ [pscustomobject]@{ ok = $true; tokens = $t } };"
            "'profile='+(Test-ArgvBindsScript -Argv (A @('pwsh.exe','-ExecutionPolicy','Bypass','-File','%s')) -AgentScript '%s');"
            "'noprofile='+(Test-ArgvBindsScript -Argv (A @('pwsh.exe','-NoProfile','-ExecutionPolicy','Bypass','-File','%s')) -AgentScript '%s')"
        ) % (psq(mine), psq(mine), psq(mine), psq(mine))
        r = self._dot_source(body)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.split(), ["profile=False", "noprofile=True"], r.stdout + r.stderr)

    def test_p10a3_an_explicit_root_that_is_a_drive_root_binds(self) -> None:
        # pass 10 A3: the script folder of R:\ultra-magnus-agent.ps1 is R:\, and it was computed as "R:" (drive-relative), so the matching
        # -Root R:\ was refused. The same must hold for a UNC share root and for a subfolder.
        body = (
            "'drive='+(Test-HostArgvRunsScript -Exe 'pwsh.exe' -Tokens @('-NoProfile','-ExecutionPolicy','Bypass','-File','R:\\ultra-magnus-agent.ps1','-Root','R:\\') -AgentScript 'R:\\ultra-magnus-agent.ps1');"
            "'drive-no-root='+(Test-HostArgvRunsScript -Exe 'pwsh.exe' -Tokens @('-NoProfile','-ExecutionPolicy','Bypass','-File','R:\\ultra-magnus-agent.ps1') -AgentScript 'R:\\ultra-magnus-agent.ps1');"
            "'drive-other='+(Test-HostArgvRunsScript -Exe 'pwsh.exe' -Tokens @('-NoProfile','-ExecutionPolicy','Bypass','-File','R:\\ultra-magnus-agent.ps1','-Root','R:\\busy') -AgentScript 'R:\\ultra-magnus-agent.ps1');"
            "'sub='+(Test-HostArgvRunsScript -Exe 'pwsh.exe' -Tokens @('-NoProfile','-ExecutionPolicy','Bypass','-File','C:\\bench\\ultra-magnus-agent.ps1','-Root','C:\\bench') -AgentScript 'C:\\bench\\ultra-magnus-agent.ps1');"
            "'sub-other='+(Test-HostArgvRunsScript -Exe 'pwsh.exe' -Tokens @('-NoProfile','-ExecutionPolicy','Bypass','-File','C:\\bench\\ultra-magnus-agent.ps1','-Root','C:\\busy') -AgentScript 'C:\\bench\\ultra-magnus-agent.ps1')"
        )
        r = self._dot_source(body)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.split(), ["drive=True", "drive-no-root=True", "drive-other=False", "sub=True", "sub-other=False"], r.stdout + r.stderr)

    def test_p15a1_an_ignorable_character_in_a_host_argument_binds_nothing(self) -> None:
        # pass 15 A1: PowerShell's -eq / -ceq / -contains / -in on STRINGS are culture-aware, so a soft hyphen (U+00AD) inside "Bypass"
        # or "-NoProfile" compared equal to the plain word. The check passed, the old agent was stopped, and the host (which compares
        # by ordinal) rejected the replacement's arguments. The text this check reads must be compared by ordinal, case-insensitively.
        body = (
            "$s=[string][char]0xAD; $a='C:\\bench\\ultra-magnus-agent.ps1';"
            "function Chk($t){ [string](Test-HostArgvRunsScript -Exe 'pwsh.exe' -Tokens $t -AgentScript $a) };"
            "'plain='+(Chk @('-NoProfile','-ExecutionPolicy','Bypass','-File',$a));"
            "'case='+(Chk @('-noprofile','-EXECUTIONPOLICY','BYPASS','-File',$a));"
            "'shy-policy='+(Chk @('-NoProfile','-ExecutionPolicy',('By'+$s+'pass'),'-File',$a));"
            "'shy-flag='+(Chk @(('-No'+$s+'Profile'),'-ExecutionPolicy','Bypass','-File',$a));"
            "'shy-name='+(Chk @('-NoProfile',('-Execution'+$s+'Policy'),'Bypass','-File',$a));"
            "'shy-style='+(Chk @('-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle',('Hi'+$s+'dden'),'-File',$a));"
            "'shy-only='+(Chk @('-NoProfile','-ExecutionPolicy','Bypass',$s,'-File',$a))"
        )
        r = self._dot_source(body)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(
            r.stdout.split(),
            ["plain=True", "case=True", "shy-policy=False", "shy-flag=False", "shy-name=False", "shy-style=False", "shy-only=False"],
            r.stdout + r.stderr)

    def test_p15a2_a_path_that_compares_equal_to_empty_is_still_made_absolute(self) -> None:
        # pass 15 A2: U+00AD is not empty but `-ne ''` said it was, so the path was never made absolute: Test-Path validated it under the
        # PowerShell location and ReadAllBytes read it under the process directory (two different files). Every "is it empty" test on a
        # path is a length test now.
        cmd = (
            "$s=[string][char]0xAD; . '%s' -VenueRoot '%s' -Source $s -RestartScript $s -TaskXmlFile $s -TaskPidsFile $s;"
            "foreach ($n in 'Source','RestartScript','TaskXmlFile','TaskPidsFile') { $n + '=' + [System.IO.Path]::IsPathRooted([string](Get-Variable -Name $n -ValueOnly)) }"
        ) % (psq(SCRIPT), psq(self.venue))
        r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True, text=True, timeout=120, env=BENCH_ENV)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.split(), ["Source=True", "RestartScript=True", "TaskXmlFile=True", "TaskPidsFile=True"], r.stdout + r.stderr)

    def test_p15_no_string_comparison_operator_is_left_in_the_deployer(self) -> None:
        # class sweep for pass 15: -eq / -ne / -ceq / -cne / -contains / -notcontains / -in / -notin on two STRINGS are culture-aware.
        # A comparison of a string with a literal is allowed only where the other side is a char (a char-to-char -eq is ordinal in effect),
        # an int, $null, or a value the deployer itself generated (a hex digest). Anything else must be [string]::Equals(.., Ordinal*).
        import re
        text = SCRIPT.read_text(encoding="utf-8")
        offenders = []
        for no, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            code = line
            for m in re.finditer(r"\s-(?:c|i)?(?:eq|ne|contains|notcontains|in|notin)\s+(?:'[^']*'|\"[^\"]*\")", code):
                offenders.append("%d: %s" % (no, m.group(0).strip()))
            for m in re.finditer(r"(?:'[^']*'|\"[^\"]*\")\s+-(?:c|i)?(?:eq|ne|contains|notcontains|in|notin)\s", code):
                offenders.append("%d: %s" % (no, m.group(0).strip()))
        self.assertEqual(offenders, [], "string literal compared with a culture-aware operator:\n" + "\n".join(offenders))

    def test_p10a2_the_clock_stepping_back_does_not_let_an_earlier_instance_confirm_the_replacement(self) -> None:
        # pass 10 A2: after the old agent exits a trigger starts PID 4200; the clock steps back; runAt (a wall-clock read) is now BEFORE
        # that start, and /Run is ignored (IgnoreNew): the instance that existed before /Run confirmed the restart. Two closures:
        # a step of the wall clock against a monotonic watch is detected, and the task must have no running instance right before /Run.
        body = (
            "$sw=[Diagnostics.Stopwatch]::StartNew();"
            "'steady='+(Test-ClockStepped -Utc0 ([DateTime]::UtcNow) -Watch $sw);"
            "'back='+(Test-ClockStepped -Utc0 ([DateTime]::UtcNow.AddSeconds(30)) -Watch $sw);"
            "'forward='+(Test-ClockStepped -Utc0 ([DateTime]::UtcNow.AddSeconds(-30)) -Watch $sw)"
        )
        r = self._dot_source(body)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.split(), ["steady=False", "back=True", "forward=True"], r.stdout + r.stderr)

    def test_p10a2_a_task_that_already_runs_an_instance_is_not_started(self) -> None:
        pid, start = self.live_agent()
        agent = self.venue / AGENT_NAME
        xml = Path(self._tmp.name) / "task-p10a2.xml"
        xml.write_text("<Task><Actions><Exec><Command>pwsh.exe</Command><Arguments>-NoProfile -ExecutionPolicy Bypass -File \"%s\"</Arguments></Exec></Actions></Task>" % agent, encoding="utf-8")
        body = (
            "$b=[pscustomobject]@{pid=%d;host=$env:COMPUTERNAME;start='%s';agentScript='%s';ageSec=1};"
            "function Get-RestartBindingRefusal { param($AgentScript) $script:BoundBeat=$b; return $null };"
            "function Stop-TaskInstanceBounded { param([int]$ProcessId, [string]$StartText) Stop-Process -Id $ProcessId -Force; [pscustomobject]@{ outcome = 'stopped'; why = $null; busy = $null } };"
            "$script:TaskIdleWaitSec = 2;"   # the wait for the scheduler to drop the old instance is shortened for the test
            "$r = Invoke-AgentRestart -AgentScript '%s'; 'why=' + $r.why"
        ) % (pid, start, psq(agent), psq(agent))
        # the scheduler (the -TaskPidsFile seam) still lists an instance, pid 4200, that a trigger started after the old agent exited
        cmd = ". '%s' -VenueRoot '%s' -TaskName '\\MlvNoSuchTaskForTest' -TaskXmlFile '%s' -TaskPidsFile '%s'; %s" % (
            psq(SCRIPT), psq(self.venue), psq(xml), psq(self.task_pids(4200, name="p10a2")), body)
        r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True, text=True, timeout=120, env=BENCH_ENV)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("already has a running instance", r.stdout)
        self.assertNotIn("schtasks /Run", r.stdout, "/Run is not issued when an instance already runs")

    # ---- r5: the Ubuntu shard failed four stop tests on a0aa52e0 (a killed, unreaped child still read as alive) ----

    def test_r5_a_killed_child_that_its_parent_has_not_reaped_is_not_alive(self) -> None:
        # The stand-ins above end the agent with Stop-Process while the test process (its parent) never waits on it. On Linux such a child
        # stays listed as a zombie (state Z) and Get-Process still returns it, so the "agent is gone" wait ran out before the gate each
        # test exercises. A process that is killed and not yet collected is not alive on either host: every liveness probe goes through
        # Get-LiveProcess. (Same body on both OSes; no skip.)
        p = subprocess.Popen([PWSH, "-NoProfile", "-Command", "Start-Sleep -Seconds 120"])
        self.addCleanup(lambda: (p.kill(), p.wait()))
        start = self.start_of(p.pid)
        body = (
            "$b=[pscustomobject]@{pid=%d;start='%s'};"
            "'before='+[bool](Get-LiveProcess -ProcessId %d);'before-alive='+(Test-AgentProcessAlive -Beat $b -RequireStart);"
            "Stop-Process -Id %d -Force;"
            "$gone = Wait-Until -TimeoutSec 15 -PollMs 100 -Variables @{ b = $b } -Condition { -not (Test-AgentProcessAlive -Beat $b -RequireStart) };"
            "'gone='+$gone; 'live='+[bool](Get-LiveProcess -ProcessId %d)"
        ) % (p.pid, start, p.pid, p.pid, p.pid)
        r = self._dot_source(body)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.split(), ["before=True", "before-alive=True", "gone=True", "live=False"], r.stdout + r.stderr)
        # the sibling probes: every process-existence read in the deploy script goes through the one helper
        code = "\n".join(l for l in SCRIPT.read_text(encoding="ascii").splitlines() if not l.lstrip().startswith("#"))
        self.assertEqual(len(re.findall(r"Get-Process\s+-Id", code)), 1, "only Get-LiveProcess itself reads a pid with Get-Process")

    # ---- adversary pass 12 findings (A1, A2 of ed9bc359; A3, A4 advisories): each test failed on ed9bc359 ----

    def test_r5_a1_a_control_character_in_the_root_argument_is_not_a_root(self) -> None:
        # pass 12 A1: the task argument "-Root:C:\bench<LF>" matched `^-(..)(:(.*))?$` (`$` matches before a final LF, `.` stops at it), so
        # the definition bound C:\bench while the agent would receive "C:\bench<LF>" and could not serve the checked root.
        mine = r"C:\bench\ultra-magnus-agent.ps1"
        base = "-NoProfile -ExecutionPolicy Bypass -File %s " % mine
        q = "&quot;"
        for label, tail in (("lf", q + r"-Root:C:\bench&#10;" + q), ("crlf", q + r"-Root:C:\bench&#13;&#10;" + q),
                            ("lf-sep", "-Root " + q + r"C:\bench&#10;" + q), ("name-lf", q + "-Root&#10;" + q + r" C:\bench"),
                            ("tab", q + r"-Root:C:\bench&#9;" + q), ("nul-less-bell", q + r"-Root:C:\bench&#7;" + q)):
            self.assertEqual(self._exec_task_case("r5a1-" + label, "pwsh.exe", base + tail), "False", label)
        self.assertEqual(self._exec_task_case("r5a1-ok", "pwsh.exe", base + q + r"-Root:C:\bench" + q), "True")
        # the live process is held to the same reading (its argv comes from the OS, not from the definition)
        body = (
            "function A($t){ [pscustomobject]@{ ok = $true; tokens = $t } };"
            r"'lf='+(Test-ArgvBindsScript -Argv (A @('pwsh.exe','-NoProfile','-ExecutionPolicy','Bypass','-File','%s',('-Root:C:\bench'+[char]10))) -AgentScript '%s');"
            r"'sep='+(Test-ArgvBindsScript -Argv (A @('pwsh.exe','-NoProfile','-ExecutionPolicy','Bypass','-File','%s','-Root',('C:\bench'+[char]10))) -AgentScript '%s');"
            r"'ok='+(Test-ArgvBindsScript -Argv (A @('pwsh.exe','-NoProfile','-ExecutionPolicy','Bypass','-File','%s','-Root:C:\bench')) -AgentScript '%s')"
        ) % (psq(mine), psq(mine), psq(mine), psq(mine), psq(mine), psq(mine))
        r = self._dot_source(body)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.split(), ["lf=False", "sep=False", "ok=True"], r.stdout + r.stderr)
        # the class: no regex over text that came from a task or a process ends in `$` (it matches before a final newline); \z does not
        code = "\n".join(l for l in SCRIPT.read_text(encoding="ascii").splitlines() if not l.lstrip().startswith("#"))
        self.assertEqual(re.findall(r"""-(?:match|notmatch|replace)\s+'[^'\n]*[^\\]\$'""", code), [], "an end-of-text anchor must be \\z")
        self.assertEqual(re.findall(r"""Match\([^\n]*,\s*'[^'\n]*[^\\]\$'\)""", code), [], "an end-of-text anchor must be \\z")

    def test_r5_a1_a_pid_reused_fifty_ms_later_is_not_the_proved_instance(self) -> None:
        # pass 13 A1: the binding proved pid 4242 at 12:00:00.000; that instance exited and another instance of the same task reused pid
        # 4242 at 12:00:00.050. The stop's 100 ms tolerance admitted it (50 > 100 is false), so Stop() hit the new instance. Two reads of
        # ONE process's start are identical on Windows (the creation time, to the tick), so the tolerance there is zero; elsewhere it is a
        # clock-jitter allowance far below 60 ms.
        for label, shift in (("late-60ms", -0.06), ("early-60ms", 0.06), ("late-200ms", -0.2)):
            out, marker = self._stop_case("r5a1-" + label, start_shift_sec=shift)
            self.assertEqual(out.get("outcome"), "not-stopped", (label, out))
            self.assertFalse(marker.exists(), label)
        if sys.platform == "win32":
            for label, shift in (("late-20ms", -0.02), ("early-20ms", 0.02), ("one-ms", 0.001)):
                out, marker = self._stop_case("r5a1w-" + label, start_shift_sec=shift)
                self.assertEqual(out.get("outcome"), "not-stopped", (label, out))
                self.assertFalse(marker.exists(), label)
        out, marker = self._stop_case("r5a1-same")
        self.assertEqual(out.get("outcome"), "stopped", out)
        self.assertTrue(marker.exists())

    def test_r5_a2_a_relative_path_names_the_same_place_to_every_api(self) -> None:
        # pass 13 A2: Test-Path resolves a relative path against the PowerShell location, [System.IO.File]/[Directory] against the process
        # directory, and the two differ (a profile or Set-Location). Source C:\B\candidate.ps1 was validated and C:\A\candidate.ps1 was
        # deployed. Every path parameter is made absolute once, at the top, so all APIs name one place.
        base = Path(self._tmp.name)
        a, b = base / "cwd-a", base / "loc-b"
        a.mkdir(); b.mkdir()
        (a / "candidate.ps1").write_bytes(b"# process-directory copy\r\n")
        (b / "candidate.ps1").write_bytes(b"# selected copy\r\n")
        cmd = "Set-Location -LiteralPath '%s'; & '%s' -VenueRoot '%s' -Source candidate.ps1 -DryRun" % (psq(b), psq(SCRIPT), psq(self.venue))
        r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True, text=True, timeout=120, env=BENCH_ENV, cwd=str(a))
        self.assertIn("sha=%s" % sha(b"# selected copy\r\n"), r.stdout, r.stdout + r.stderr)
        self.assertNotIn(sha(b"# process-directory copy\r\n"), r.stdout)
        # a relative VenueRoot: listed (Directory API) under the same place Test-Path checked
        cmd = "Set-Location -LiteralPath '%s'; & '%s' -VenueRoot 'venue' -Source '%s' -DryRun" % (psq(base), psq(SCRIPT), psq(self.source))
        r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True, text=True, timeout=120, env=BENCH_ENV, cwd=str(a))
        self.assertIn("DEPLOY_DRYRUN", r.stdout, r.stdout + r.stderr)

    def _run_raw(self, **given: str):
        """The script with the named parameters passed as the literal TEXT given (a path text no Path object could carry)."""
        args = {"VenueRoot": str(self.venue), "Source": str(self.source), **given}
        cmd = [PWSH, "-NoProfile", "-NonInteractive", "-File", str(SCRIPT), "-DryRun"]
        for k, v in args.items():
            cmd += ["-" + k, v]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=BENCH_ENV)

    def _absent_drive(self) -> str:
        # a drive letter no PowerShell drive uses: on Windows it is absent (the normalization throws), elsewhere it is an ordinary name
        for letter in "ZYXWVUTSRQ":
            if not os.path.exists(letter + ":\\"):
                return letter
        self.skipTest("no free drive letter")

    def test_r9_a1_a_path_the_normalization_cannot_resolve_is_a_typed_refusal_not_an_exception(self) -> None:
        # pass 16 A1: -VenueRoot 'Z:\venue' (no such drive) made GetUnresolvedProviderPathFromPSPath throw at the top of the script, before
        # the try, so the caller got a raw exception and PowerShell's own exit code instead of the typed refusal. Every path parameter is
        # normalized INSIDE the typed-refusal boundary: the venue root and the secondary paths refuse as unreadable (12), the source as missing (2).
        z = self._absent_drive()
        for label, given, code, typed in (
            ("venue-absent-drive", {"VenueRoot": z + ":\\venue"}, 12, "DEPLOY_REFUSED_UNREADABLE"),
            ("source-absent-drive", {"Source": z + ":\\src\\agent.ps1"}, 2, "DEPLOY_SOURCE_MISSING"),
            ("restart-script-absent-drive", {"RestartScript": z + ":\\r.ps1"}, 12, "DEPLOY_REFUSED_UNREADABLE"),
            ("task-xml-absent-drive", {"TaskXmlFile": z + ":\\t.xml"}, 12, "DEPLOY_REFUSED_UNREADABLE"),
            ("task-pids-absent-drive", {"TaskPidsFile": z + ":\\p.txt"}, 12, "DEPLOY_REFUSED_UNREADABLE"),
        ):
            r = self._run_raw(**given)
            self.assertEqual(r.returncode, code, (label, r.stdout, r.stderr))
            self.assertIn(typed, r.stdout, (label, r.stdout, r.stderr))
            self.assertEqual(r.stderr.strip(), "", (label, "a typed refusal leaves no raw exception on stderr", r.stderr))
        self.assert_untouched()

    def test_r9_a2_a_provider_qualified_path_that_stays_relative_is_a_typed_refusal(self) -> None:
        # pass 16 A2: 'FileSystem::x.ps1' normalizes to 'x.ps1' (still relative, resolved later against a directory that differs per API),
        # and 'Env:\PATH' to 'PATH'. A path that is not absolute after the normalization is refused, whatever provider it names.
        for label, given, code, typed in (
            ("venue-provider-relative", {"VenueRoot": "FileSystem::venue"}, 12, "DEPLOY_REFUSED_UNREADABLE"),
            ("venue-full-provider-relative", {"VenueRoot": "Microsoft.PowerShell.Core\\FileSystem::venue"}, 12, "DEPLOY_REFUSED_UNREADABLE"),
            ("venue-other-provider", {"VenueRoot": "Env:\\PATH"}, 12, "DEPLOY_REFUSED_UNREADABLE"),
            ("source-provider-relative", {"Source": "FileSystem::x.ps1"}, 2, "DEPLOY_SOURCE_MISSING"),
            ("restart-script-provider-relative", {"RestartScript": "FileSystem::r.ps1"}, 12, "DEPLOY_REFUSED_UNREADABLE"),
            ("task-xml-provider-relative", {"TaskXmlFile": "FileSystem::t.xml"}, 12, "DEPLOY_REFUSED_UNREADABLE"),
            ("task-pids-provider-relative", {"TaskPidsFile": "FileSystem::p.txt"}, 12, "DEPLOY_REFUSED_UNREADABLE"),
        ):
            r = self._run_raw(**given)
            self.assertEqual(r.returncode, code, (label, r.stdout, r.stderr))
            self.assertIn(typed, r.stdout, (label, r.stdout, r.stderr))
            self.assertEqual(r.stderr.strip(), "", (label, r.stderr))
        # control: a provider-qualified path that carries an ABSOLUTE path is the same place as the plain one and still deploys (dry run)
        r = self._run_raw(VenueRoot="FileSystem::" + str(self.venue), Source="FileSystem::" + str(self.source))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("DEPLOY_DRYRUN", r.stdout)
        self.assert_untouched()

    def test_r5_a2_the_run_lower_bound_is_never_earlier_than_the_monotonic_expectation(self) -> None:
        # pass 12 A2: the old agent exits, a trigger starts pid 4200 at 12:00:02.5, the wall clock steps BACK 1 s, and runAt is read as
        # 12:00:02: the instance that existed before /Run passed "started at or after /Run" and the final step check (2 s) let 1 s through.
        # runAt is now the LATER of the wall clock and the monotonic expectation, so a backward step cannot move it before an instance.
        body = (
            "$sw=[Diagnostics.Stopwatch]::StartNew(); $now=[DateTime]::UtcNow;"
            "$back = Get-RunLowerBound -Utc0 $now.AddSeconds(5) -Watch $sw;"    # the wall clock is 5 s behind where the watch says it should be
            "$fwd = Get-RunLowerBound -Utc0 $now.AddSeconds(-5) -Watch $sw;"    # the wall clock is 5 s ahead: the wall clock stands
            "'back=' + (($back - $now).TotalSeconds -ge 4.5);"
            "'fwd=' + ((($fwd - $now).TotalSeconds -ge 0) -and (($fwd - $now).TotalSeconds -lt 2))"
        )
        r = self._dot_source(body)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.split(), ["back=True", "fwd=True"], r.stdout + r.stderr)
        code = "\n".join(l for l in SCRIPT.read_text(encoding="ascii").splitlines() if not l.lstrip().startswith("#"))
        self.assertRegex(code, r"\$runAt\s*=\s*Get-RunLowerBound\b")
        self.assertNotRegex(code, r"\$runAt\s*=\s*\[DateTime\]::UtcNow")

    def test_r5_a4_a_bounded_child_with_a_non_positive_timeout_is_killed_not_orphaned(self) -> None:
        # pass 12 A4 (advisory): -TimeoutSec -1 reached WaitForExit(-1000), which throws after the child was started and before the kill.
        body = (
            "$t=[Diagnostics.Stopwatch]::StartNew();"
            "$r = Invoke-BoundedProcess -FilePath '%s' -Arguments @('-NoProfile','-Command','Start-Sleep -Seconds 120') -TimeoutSec -1;"
            "'timedOut=' + $r.timedOut; 'fast=' + ($t.Elapsed.TotalSeconds -lt 60)"
        ) % psq(PWSH)
        r = self._dot_source(body)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.split(), ["timedOut=True", "fast=True"], r.stdout + r.stderr)

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
