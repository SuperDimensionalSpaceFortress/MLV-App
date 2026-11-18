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
