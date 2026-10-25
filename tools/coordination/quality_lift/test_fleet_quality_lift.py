"""Fixture proof for Measure-FleetQualityLift.ps1 (py -3 -m unittest, or pytest).

A throwaway git repo shaped like the fleet doctrine bus (cos-feedback/<project>/pr-*.md, one CoS pass per commit)
is measured with -NoStatuses -NoFetch, so no network is read. The status cache the meter keeps in -OutDir is
seeded by hand, which is how the before/after comparisons are proven without GitHub.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
METER = HERE / "Measure-FleetQualityLift.ps1"
PWSH = shutil.which("pwsh")

H1 = "1" * 40  # PR 7: reviewed clean, then blocked on the same head (-05:00 commit times)
H2 = "2" * 40  # PR 7: second head, clean
H3 = "3" * 40  # PR 8: blocked on its first pass
H4 = "4" * 40  # PR 9: clean
H5 = "5" * 40  # second project, clean


def _section(pr: int, head: str, blockers: str) -> str:
    return f"# acme/widget #{pr} / {head}\n\nVerdict text.\n\n## Blockers\n\n{blockers}\n\n## Notes\nnone\n"


class MeterFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if PWSH is None:
            raise unittest.SkipTest("pwsh is not on PATH")
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        cls.bus = cls.root / "bus"
        cls.bus.mkdir()
        cls._git("init", "-q")
        cls._commit("README.md", "bus\n", "2026-10-19T08:00:00+00:00", "seed")
        # The local-time trap: 12:00 at -05:00 is 17:00Z. A bare [DateTime] cast reads it as local time.
        cls._commit("cos-feedback/demo/pr-7.md", _section(7, H1, "None demonstrated."), "2026-10-20T10:00:00-05:00", "pr7 clean")
        cls._commit("cos-feedback/demo/pr-7.md", _section(7, H1, "B1 a demonstrated defect at x.ps1:10."), "2026-10-20T12:00:00-05:00", "pr7 blocked")
        cls._commit("cos-feedback/demo/pr-8.md", _section(8, H3, "B1 another defect."), "2026-10-21T10:00:00+00:00", "pr8 blocked")
        cls._commit("cos-feedback/demo/pr-7.md", _section(7, H2, "None demonstrated."), "2026-10-21T12:00:00+00:00", "pr7 head 2 clean")
        cls._commit("cos-feedback/demo/pr-9.md", _section(9, H4, "None."), "2026-10-21T13:00:00+00:00", "pr9 clean")
        cls._commit("cos-feedback/other/pr-1.md", _section(1, H5, "None demonstrated."), "2026-10-21T14:00:00+00:00", "other clean")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    @classmethod
    def _git(cls, *args: str, env: dict[str, str] | None = None) -> str:
        proc = subprocess.run(
            ["git", "-C", str(cls.bus), "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
             "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", *args],
            capture_output=True, text=True, env=env, check=True)
        return proc.stdout

    @classmethod
    def _commit(cls, rel: str, text: str, when: str, subject: str) -> None:
        target = cls.bus / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
        cls._git("add", "--", rel)
        env = dict(os.environ, GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
        cls._git("commit", "-q", "--no-verify", "-m", subject, env=env)

    def _run(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [PWSH, "-NoProfile", "-NonInteractive", "-File", str(METER), *args],
            capture_output=True, text=True, env=env, timeout=300)

    def _out(self) -> Path:
        tmp = tempfile.TemporaryDirectory(dir=self.root)
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

    def _measure_demo(self, out: Path, cache: dict | None = None) -> dict:
        if cache is not None:
            (out / "status-cache.json").write_text(json.dumps(cache), encoding="utf-8")
        proc = self._run("-Bus", str(self.bus), "-Project", "demo", "-Since", "2026-10-01T00:00:00Z",
                         "-OutDir", str(out), "-NoStatuses", "-NoFetch")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads((out / "quality-lift.json").read_text(encoding="utf-8-sig"))

    def test_counts_passes_blocked_and_first_round(self) -> None:
        since = self._measure_demo(self._out())["windows"][0]
        self.assertEqual(since["window"], "since")
        self.assertEqual(since["passes"], 4)  # H1 once (clean-then-blocked), H2, H3, H4
        self.assertEqual(since["blocked"], 2)
        self.assertEqual(since["prs"], 3)
        self.assertEqual(since["firstRoundBlocked"], 2)  # PR 7 (H1) and PR 8; PR 9 was clean
        self.assertEqual(since["roundsPerPr"], 1.33)
        self.assertEqual(since["roundsPerPrMax"], 2)
        self.assertEqual(since["noPriorKey"], 2)  # no status cache at all: CoS read both blocked heads first
        self.assertEqual(since["trueMiss"], 0)

    def test_a_head_reviewed_clean_then_blocked_counts_as_blocked(self) -> None:
        out = self._out()
        self._measure_demo(out)
        rows = [json.loads(line) for line in (out / "corpus.jsonl").read_text(encoding="utf-8-sig").splitlines() if line.strip()]
        self.assertEqual([r["head"] for r in rows], [H1, H3])
        self.assertTrue(rows[0]["blocker"].startswith("B1 a demonstrated defect"), rows[0])

    def test_commit_times_with_an_offset_are_converted_to_utc(self) -> None:
        out = self._out()
        cache = {
            # H1's blocking pass is 17:00Z. A success at 16:30Z is BEFORE it (a true miss); read as local time it is not.
            H1: {"fetched": "2026-10-20T18:00:00Z", "rows": [{"context": "review/key-a", "firstUtc": "2026-10-20T16:30:00Z", "state": "success"}]},
            # H3's pass is 10:00Z; this status came after it, so CoS still read H3 first.
            H3: {"fetched": "2026-10-21T12:00:00Z", "rows": [{"context": "review/key-a", "firstUtc": "2026-10-21T11:00:00+00:00", "state": "success"}]},
        }
        since = self._measure_demo(out, cache)["windows"][0]
        self.assertEqual(since["trueMiss"], 1)
        self.assertEqual(since["noPriorKey"], 1)
        self.assertEqual(since["keysBeforeCos"], {"review/key-a": {"successBeforeCos": 1, "failureBeforeCos": 0}})
        rows = [json.loads(line) for line in (out / "corpus.jsonl").read_text(encoding="utf-8-sig").splitlines() if line.strip()]
        self.assertEqual(rows[0]["cosUtc"], "2026-10-20T17:00:00.0000000Z")
        self.assertEqual(rows[0]["successBeforeCos"], ["review/key-a"])

    def test_fleet_mode_writes_every_project_and_the_fleet_row(self) -> None:
        out = self._out()
        proc = self._run("-Bus", str(self.bus), "-Since", "2026-10-01T00:00:00Z", "-OutDir", str(out), "-NoStatuses", "-NoFetch")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue((out / "demo" / "quality-lift.json").is_file())
        self.assertTrue((out / "other" / "quality-lift.json").is_file())
        fleet = (out / "fleet.md").read_text(encoding="utf-8-sig")
        self.assertIn("| **fleet** | 5 | 2 (40%) | | 2 of 2 | 0 |", fleet)

    def test_the_bus_comes_from_the_environment_when_no_bus_is_passed(self) -> None:
        out = self._out()
        env = dict(os.environ, FACTORY_BUS_CLONE=str(self.bus))
        proc = self._run("-Project", "demo", "-Since", "2026-10-01T00:00:00Z", "-OutDir", str(out), "-NoStatuses", "-NoFetch", env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue((out / "quality-lift.json").is_file())

    def test_a_missing_or_unset_bus_exits_3(self) -> None:
        out = self._out()
        proc = self._run("-Bus", str(self.root / "no-such-bus"), "-OutDir", str(out), "-NoStatuses", "-NoFetch")
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("BUS_UNREADABLE", proc.stdout)
        env = {k: v for k, v in os.environ.items() if k != "FACTORY_BUS_CLONE"}
        proc = self._run("-OutDir", str(out), "-NoStatuses", "-NoFetch", env=env)
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertEqual(list(out.iterdir()), [])

    def test_a_folder_with_cos_feedback_that_is_not_a_git_repo_exits_3(self) -> None:
        fake = self._out()
        (fake / "cos-feedback" / "demo").mkdir(parents=True)
        proc = self._run("-Bus", str(fake), "-OutDir", str(self._out()), "-NoStatuses", "-NoFetch")
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)

    def test_usage_errors_exit_4(self) -> None:
        proc = self._run("-Bus", str(self.bus), "-NoStatuses", "-NoFetch")
        self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)
        proc = self._run("-Bus", str(self.bus), "-OutDir", str(self._out()), "-Since", "not-a-time", "-NoStatuses", "-NoFetch")
        self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)

    def test_the_meter_is_ascii_and_names_no_board_path(self) -> None:
        raw = METER.read_bytes()
        self.assertTrue(all(b < 128 for b in raw), "Measure-FleetQualityLift.ps1 must be ASCII-only")
        text = raw.decode("ascii").lower()
        for term in ("mlv", "layi wkspc", ".claude-state", "fleet-runs"):
            self.assertNotIn(term, text)


if __name__ == "__main__":
    unittest.main()
