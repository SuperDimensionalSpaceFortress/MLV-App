"""Fixture proof for Measure-FleetQualityLift.ps1 (py -3 -m unittest, or pytest).

A throwaway git repo shaped like the fleet doctrine bus (cos-feedback/<project>/pr-*.md, one CoS pass per commit)
is measured with -NoFetch. With -NoStatuses no network is read and the status cache in -OutDir is seeded by hand;
without it, a fake `gh` (a gh.ps1 first on PATH) answers the status pages and the PR listing from files.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
METER = HERE / "Measure-FleetQualityLift.ps1"
PWSH = shutil.which("pwsh")
NOW = datetime.now(timezone.utc).replace(microsecond=0)

H1 = "1" * 40  # demo PR 7: reviewed clean, then blocked on the same head (-05:00 commit times)
H2 = "2" * 40  # demo PR 7: second head, clean
H3 = "3" * 40  # demo PR 8: blocked on its first pass
H4 = "4" * 40  # demo PR 9: clean
H5 = "5" * 40  # other PR 1: clean; its file is deleted later
H6 = "6" * 40  # twin: the same head blocked in two repositories
H7 = "7" * 40  # flip: pending before CoS, success after
H8 = "8" * 40  # flip: success before CoS, failure after
H9 = "9" * 40  # flip: the success before CoS is on the second status page
H10 = "a" * 40  # recent: blocked 3 h ago
H11 = "b" * 40  # recent: blocked 1 h ago
H12 = "c" * 40  # recent: dated two days in the future

FAKE_GH = r"""
$dir = $env:QL_FAKE_GH_DIR
if ($args[0] -eq 'api') {
    $path = @($args | Where-Object { $_ -like 'repos/*' })[0]
    $page = [int](@($args | Where-Object { $_ -like 'page=*' })[0] -replace '^page=', '')
    $parts = $path.Split('/')
    $file = Join-Path $dir ('status-{0}_{1}_{2}.json' -f $parts[1], $parts[2], $parts[4])
    Add-Content -LiteralPath (Join-Path $dir 'calls.log') -Value "api $path page=$page"
    if (-not (Test-Path -LiteralPath $file)) { '[]'; exit 0 }
    $all = @(Get-Content -LiteralPath $file -Raw | ConvertFrom-Json)
    ConvertTo-Json -InputObject @($all | Select-Object -Skip (($page - 1) * 100) -First 100) -Depth 5
    exit 0
}
if ($args[0] -eq 'pr' -and $args[1] -eq 'list') {
    $repo = $args[[array]::IndexOf($args, '--repo') + 1]
    $limit = [int]$args[[array]::IndexOf($args, '--limit') + 1]
    $file = Join-Path $dir ('prs-{0}.json' -f $repo.Replace('/', '_'))
    if (-not (Test-Path -LiteralPath $file)) { '[]'; exit 0 }
    $all = @(Get-Content -LiteralPath $file -Raw | ConvertFrom-Json)
    ConvertTo-Json -InputObject @($all | Select-Object -First $limit) -Depth 5
    exit 0
}
exit 9
"""


def _iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _section(pr: int, head: str, blockers: str, repo: str = "acme/widget") -> str:
    return f"# {repo} #{pr} / {head}\n\nVerdict text.\n\n## Blockers\n\n{blockers}\n\n## Notes\nnone\n"


def _status(context: str, state: str, created: str) -> dict:
    return {"context": context, "state": state, "created_at": created}


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
         "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", *args],
        capture_output=True, text=True, env=env, check=True)
    return proc.stdout.strip()


def _commit(repo: Path, rel: str, text: str | None, when: str, subject: str) -> None:
    target = repo / rel
    if text is None:
        _git(repo, "rm", "-q", "--", rel)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
        _git(repo, "add", "--", rel)
    env = dict(os.environ, GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
    _git(repo, "commit", "-q", "--no-verify", "-m", subject, env=env)


class MeterFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if PWSH is None:
            raise unittest.SkipTest("pwsh is not on PATH")
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        cls.bus = cls.root / "bus"
        cls.bus.mkdir()
        _git(cls.bus, "init", "-q")
        c = lambda rel, text, when, subject: _commit(cls.bus, rel, text, when, subject)  # noqa: E731
        c("README.md", "bus\n", "2026-10-19T08:00:00+00:00", "seed")
        # The local-time trap: 12:00 at -05:00 is 17:00Z. A bare [DateTime] cast reads it as local time.
        c("cos-feedback/demo/pr-7.md", _section(7, H1, "None demonstrated."), "2026-10-20T10:00:00-05:00", "pr7 clean")
        c("cos-feedback/demo/pr-7.md", _section(7, H1, "B1 a demonstrated defect at x.ps1:10."), "2026-10-20T12:00:00-05:00", "pr7 blocked")
        c("cos-feedback/demo/pr-8.md", _section(8, H3, "B1 another defect."), "2026-10-21T10:00:00+00:00", "pr8 blocked")
        c("cos-feedback/demo/pr-7.md", _section(7, H2, "None demonstrated."), "2026-10-21T12:00:00+00:00", "pr7 head 2 clean")
        c("cos-feedback/demo/pr-9.md", _section(9, H4, "None."), "2026-10-21T13:00:00+00:00", "pr9 clean")
        c("cos-feedback/other/concerns.md", "none\n", "2026-10-21T13:30:00+00:00", "other concerns file (keeps the folder)")
        c("cos-feedback/other/pr-1.md", _section(1, H5, "None demonstrated."), "2026-10-21T14:00:00+00:00", "other clean")
        c("cos-feedback/other/pr-1.md", None, "2026-10-22T09:00:00+00:00", "other pr file deleted")
        c("cos-feedback/twin/pr-70.md", _section(70, H6, "B1 x."), "2026-10-21T15:00:00+00:00", "twin acme")
        c("cos-feedback/twin/pr-71.md", _section(71, H6, "B1 x.", "fork/widget"), "2026-10-21T16:00:00+00:00", "twin fork")
        c("cos-feedback/flip/pr-20.md", _section(20, H7, "B1 x."), "2026-10-21T10:00:00+00:00", "flip h7")
        c("cos-feedback/flip/pr-21.md", _section(21, H8, "B1 x."), "2026-10-21T10:00:00+00:00", "flip h8")
        c("cos-feedback/flip/pr-22.md", _section(22, H9, "B1 x."), "2026-10-21T10:00:00+00:00", "flip h9")
        c("cos-feedback/recent/pr-80.md", _section(80, H10, "B1 x."), _iso(NOW - timedelta(hours=3)), "recent 3h")
        c("cos-feedback/recent/pr-81.md", _section(81, H11, "B1 x."), _iso(NOW - timedelta(hours=1)), "recent 1h")
        c("cos-feedback/recent/pr-82.md", _section(82, H12, "B1 x."), _iso(NOW + timedelta(days=2)), "recent future")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def _dir(self, name: str = "") -> Path:
        tmp = tempfile.TemporaryDirectory(dir=self.root)
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / name if name else Path(tmp.name)
        path.mkdir(exist_ok=True)
        return path

    def _fake_gh(self, statuses: dict[tuple[str, str], list[dict]] | None = None, prs: dict[str, list[dict]] | None = None) -> Path:
        fake = self._dir()
        (fake / "gh.ps1").write_text(FAKE_GH, encoding="utf-8")
        for (repo, head), rows in (statuses or {}).items():
            owner, name = repo.split("/")
            (fake / f"status-{owner}_{name}_{head}.json").write_text(json.dumps(rows), encoding="utf-8")
        for repo, rows in (prs or {}).items():
            (fake / f"prs-{repo.replace('/', '_')}.json").write_text(json.dumps(rows), encoding="utf-8")
        return fake

    def _run(self, *args: str, env: dict[str, str] | None = None, gh: Path | None = None) -> subprocess.CompletedProcess:
        env = dict(os.environ if env is None else env)
        if gh is not None:
            env["PATH"] = str(gh) + os.pathsep + env.get("PATH", "")
            env["QL_FAKE_GH_DIR"] = str(gh)
        return subprocess.run(
            [PWSH, "-NoProfile", "-NonInteractive", "-File", str(METER), *args],
            capture_output=True, text=True, env=env, timeout=300)

    def _measure(self, project: str, out: Path, cache: dict | None = None, gh: Path | None = None,
                 since: str = "2026-10-01T00:00:00Z", expect: int = 0) -> dict:
        if cache is not None:
            (out / "status-cache.json").write_text(json.dumps(cache), encoding="utf-8")
        args = ["-Bus", str(self.bus), "-Project", project, "-Since", since, "-OutDir", str(out), "-NoFetch"]
        if gh is None:
            args.append("-NoStatuses")
        proc = self._run(*args, gh=gh)
        self.assertEqual(proc.returncode, expect, proc.stdout + proc.stderr)
        return json.loads((out / "quality-lift.json").read_text(encoding="utf-8-sig"))

    @staticmethod
    def _corpus(out: Path) -> list[dict]:
        path = out / "corpus.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]

    # -- counts and definitions

    def test_counts_passes_blocked_and_first_round(self) -> None:
        since = self._measure("demo", self._dir())["windows"][0]
        self.assertEqual(since["window"], "since")
        self.assertEqual(since["passes"], 4)  # H1 once (clean-then-blocked), H2, H3, H4
        self.assertEqual(since["blocked"], 2)
        self.assertEqual(since["prs"], 3)
        self.assertEqual(since["firstRoundBlocked"], 2)  # PR 7 (first head H1 ended blocked) and PR 8; PR 9 was clean
        self.assertEqual(since["roundsPerPr"], 1.33)
        self.assertEqual(since["roundsPerPrMax"], 2)

    def test_a_head_reviewed_clean_then_blocked_counts_as_blocked(self) -> None:
        out = self._dir()
        cache = {f"acme/widget@{H1}": {"fetched": "2026-10-20T18:00:00Z", "rows": []},
                 f"acme/widget@{H3}": {"fetched": "2026-10-21T12:00:00Z", "rows": []}}
        self._measure("demo", out, cache)
        rows = self._corpus(out)
        self.assertEqual([r["head"] for r in rows], [H1, H3])
        self.assertTrue(rows[0]["blocker"].startswith("B1 a demonstrated defect"), rows[0])

    def test_without_status_evidence_cos_first_is_unknown_not_measured(self) -> None:
        out = self._dir()
        since = self._measure("demo", out)["windows"][0]
        self.assertEqual(since["statusUnknown"], 2)
        self.assertEqual(since["noPriorKey"], 0)
        self.assertEqual(since["trueMiss"], 0)
        self.assertEqual(self._corpus(out), [], "an unknown head must not be frozen into the corpus")
        self.assertIn("0 of 2 (2 unknown)", (out / "quality-lift.md").read_text(encoding="utf-8-sig"))

    def test_commit_times_with_an_offset_are_converted_to_utc(self) -> None:
        out = self._dir("q[d]")  # a literal [d] in the folder name is not a wildcard
        cache = {
            # H1's blocking pass is 17:00Z. A success at 16:30Z is BEFORE it (a true miss); read as local time it is not.
            f"acme/widget@{H1}": {"fetched": "2026-10-20T18:00:00Z", "rows": [
                {"context": "review/key-a", "firstUtc": "2026-10-20T16:30:00Z", "state": "success", "successUtc": "2026-10-20T16:30:00Z", "failureUtc": None}]},
            # H3's pass is 10:00Z; this status came after it, so CoS still read H3 first.
            f"acme/widget@{H3}": {"fetched": "2026-10-21T12:00:00Z", "rows": [
                {"context": "review/key-a", "firstUtc": "2026-10-21T11:00:00+00:00", "state": "success", "successUtc": "2026-10-21T11:00:00+00:00", "failureUtc": None}]},
        }
        since = self._measure("demo", out, cache)["windows"][0]
        self.assertEqual(since["trueMiss"], 1)
        self.assertEqual(since["noPriorKey"], 1)
        self.assertEqual(since["statusUnknown"], 0)
        self.assertEqual(since["keysBeforeCos"], {"review/key-a": {"successBeforeCos": 1, "failureBeforeCos": 0}})
        rows = self._corpus(out)
        self.assertEqual(rows[0]["cosUtc"], "2026-10-20T17:00:00.0000000Z")
        self.assertEqual(rows[0]["successBeforeCos"], ["review/key-a"])

    # -- status evidence read from (fake) GitHub

    def test_before_cos_is_decided_per_state_time_not_first_time_and_last_state(self) -> None:
        later = [_status(f"later/{i}", "pending", "2026-10-21T11:00:00Z") for i in range(1, 101)]
        gh = self._fake_gh({
            ("acme/widget", H7): [_status("review/key-a", "success", "2026-10-21T11:00:00Z"), _status("review/key-a", "pending", "2026-10-21T09:00:00Z")],
            ("acme/widget", H8): [_status("review/key-a", "failure", "2026-10-21T11:00:00Z"), _status("review/key-a", "success", "2026-10-21T09:00:00Z")],
            ("acme/widget", H9): later + [_status("review/key-a", "success", "2026-10-21T09:00:00Z")],
        })
        since = self._measure("flip", self._dir(), gh=gh)["windows"][0]
        self.assertEqual(since["blocked"], 3)
        self.assertEqual(since["trueMiss"], 2)  # H8 and H9 (page 2); H7's success came after CoS
        self.assertEqual(since["noPriorKey"], 0)
        self.assertEqual(since["statusUnknown"], 0)
        self.assertEqual(since["keysBeforeCos"]["review/key-a"], {"successBeforeCos": 2, "failureBeforeCos": 0})
        self.assertIn(f"api repos/acme/widget/commits/{H9}/statuses page=2", (gh / "calls.log").read_text(encoding="utf-8"))

    def test_status_evidence_is_kept_per_repository_not_per_sha(self) -> None:
        gh = self._fake_gh({("acme/widget", H6): [_status("review/key-a", "success", "2026-10-21T14:00:00Z")]})
        out = self._dir()
        since = self._measure("twin", out, gh=gh)["windows"][0]
        self.assertEqual(since["blocked"], 2)
        self.assertEqual(since["trueMiss"], 1)
        self.assertEqual(since["noPriorKey"], 1)
        self.assertEqual(sorted((r["repo"], r["successBeforeCos"]) for r in self._corpus(out)),
                         [("acme/widget", ["review/key-a"]), ("fork/widget", [])])

    def test_adoption_counts_recent_pr_bodies_and_refuses_a_truncated_listing(self) -> None:
        recent, old = _iso(NOW - timedelta(hours=1)), _iso(NOW - timedelta(days=2))
        gh = self._fake_gh(prs={"acme/widget": [
            {"number": 3, "body": "What.\n\n**Adversary pass:** run verdict=CLEAN found=0 fixed=0\n", "createdAt": recent},
            {"number": 2, "body": "No line here.", "createdAt": recent},
            {"number": 1, "body": "Adversary pass: old", "createdAt": old},
        ]})
        result = self._measure("demo", self._dir(), gh=gh)
        self.assertEqual(result["shiftLeft"], {"prs": 2, "withAdversaryLine": 1})
        flood = [{"number": n, "body": "x", "createdAt": recent} for n in range(201, 0, -1)]
        gh = self._fake_gh(prs={"acme/widget": flood})
        result = self._measure("demo", self._dir(), gh=gh, expect=2)
        self.assertTrue(any("truncated" in e for e in result["errors"]), result["errors"])

    # -- windows

    def test_last_24h_ignores_a_narrower_since_and_future_commits(self) -> None:
        result = self._measure("recent", self._dir(), since=_iso(NOW - timedelta(hours=2)))
        since, d1 = result["windows"]
        self.assertEqual(since["passes"], 1)  # H11 only; H12 is in the future
        self.assertEqual(d1["window"], "last-24h")
        self.assertEqual(d1["passes"], 2)  # H10 and H11, though H10 is before -Since

    # -- fleet mode and failures

    def test_fleet_mode_writes_every_project_and_the_fleet_row(self) -> None:
        out = self._dir()
        proc = self._run("-Bus", str(self.bus), "-Since", "2026-10-01T00:00:00Z", "-OutDir", str(out), "-NoStatuses", "-NoFetch")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for project in ("demo", "other", "twin", "flip", "recent"):
            self.assertTrue((out / project / "quality-lift.json").is_file(), project)
        other = json.loads((out / "other" / "quality-lift.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(other["windows"][0]["passes"], 1)  # the later commit that deletes pr-1.md is not an error
        fleet = (out / "fleet.md").read_text(encoding="utf-8-sig")
        self.assertIn("| **fleet** | 12 | 9 (75%) | | 0 of 9 (9 unknown) | 0 |", fleet)

    def test_an_empty_bus_still_writes_the_fleet_report(self) -> None:
        bus = self._dir()
        _git(bus, "init", "-q")
        _commit(bus, "cos-feedback/README.md", "readme\n", "2026-10-20T00:00:00+00:00", "seed")
        out = self._dir() / "new" / "report"
        proc = self._run("-Bus", str(bus), "-OutDir", str(out), "-NoStatuses", "-NoFetch")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue((out / "fleet.md").is_file())
        self.assertEqual(json.loads((out / "fleet.json").read_text(encoding="utf-8-sig")), [])

    def test_an_unreadable_bus_object_is_a_source_error(self) -> None:
        bus = self._dir()
        _git(bus, "init", "-q")
        _commit(bus, "cos-feedback/x/pr-1.md", _section(1, H1, "B1 x."), "2026-10-20T00:00:00+00:00", "x")
        blob = _git(bus, "rev-parse", "HEAD:cos-feedback/x/pr-1.md")
        loose = bus / ".git" / "objects" / blob[:2] / blob[2:]
        os.chmod(loose, stat.S_IWRITE | stat.S_IREAD)
        loose.unlink()
        out = self._dir()
        proc = self._run("-Bus", str(bus), "-Project", "x", "-Since", "2026-10-01T00:00:00Z", "-OutDir", str(out), "-NoStatuses", "-NoFetch")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        errors = json.loads((out / "quality-lift.json").read_text(encoding="utf-8-sig"))["errors"]
        self.assertTrue(any("git show" in e for e in errors), errors)

    def test_a_failed_bus_pull_is_a_source_error(self) -> None:
        out = self._dir()
        proc = self._run("-Bus", str(self.bus), "-Project", "demo", "-Since", "2026-10-01T00:00:00Z", "-OutDir", str(out), "-NoStatuses")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)  # the fixture bus has no upstream to pull from
        self.assertIn("bus pull", (out / "quality-lift.md").read_text(encoding="utf-8-sig"))

    def test_the_bus_comes_from_the_environment_when_no_bus_is_passed(self) -> None:
        out = self._dir()
        env = dict(os.environ, FACTORY_BUS_CLONE=str(self.bus))
        proc = self._run("-Project", "demo", "-Since", "2026-10-01T00:00:00Z", "-OutDir", str(out), "-NoStatuses", "-NoFetch", env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue((out / "quality-lift.json").is_file())

    def test_a_missing_or_unset_bus_exits_3(self) -> None:
        out = self._dir()
        proc = self._run("-Bus", str(self.root / "no-such-bus"), "-OutDir", str(out), "-NoStatuses", "-NoFetch")
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("BUS_UNREADABLE", proc.stdout)
        env = {k: v for k, v in os.environ.items() if k != "FACTORY_BUS_CLONE"}
        proc = self._run("-OutDir", str(out), "-NoStatuses", "-NoFetch", env=env)
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertEqual(list(out.iterdir()), [])

    def test_a_folder_with_cos_feedback_that_is_not_a_git_repo_exits_3(self) -> None:
        fake = self._dir()
        (fake / "cos-feedback" / "demo").mkdir(parents=True)
        proc = self._run("-Bus", str(fake), "-OutDir", str(self._dir()), "-NoStatuses", "-NoFetch")
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)

    def test_usage_errors_exit_4(self) -> None:
        proc = self._run("-Bus", str(self.bus), "-NoStatuses", "-NoFetch")
        self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)
        proc = self._run("-Bus", str(self.bus), "-OutDir", str(self._dir()), "-Since", "not-a-time", "-NoStatuses", "-NoFetch")
        self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)

    def test_the_meter_is_ascii_and_names_no_board_path(self) -> None:
        raw = METER.read_bytes()
        self.assertTrue(all(b < 128 for b in raw), "Measure-FleetQualityLift.ps1 must be ASCII-only")
        text = raw.decode("ascii").lower()
        for term in ("mlv", "layi wkspc", ".claude-state", "fleet-runs"):
            self.assertNotIn(term, text)


if __name__ == "__main__":
    unittest.main()
