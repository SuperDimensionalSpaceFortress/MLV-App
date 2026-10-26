"""Fixture proof for Measure-FleetQualityLift.ps1 (py -3 -m unittest, or pytest).

A throwaway git repo shaped like the fleet doctrine bus (cos-feedback/<project>/pr-*.md, one CoS pass per commit)
is measured with -NoFetch. With -NoStatuses no network is read and the status cache in -OutDir is seeded by hand;
without it, a fake `gh` (a gh.ps1 first on PATH) answers the status pages, native PR reviews and review comments,
and the PR listing from files. Unless a test says otherwise, the measured project declares `review/*` status
contexts as its review evidence (-ReviewSources).
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
OTHER = "e" * 40  # a head no pass names

REVIEW_STATUS = {"statusContexts": ["review/*"]}

FAKE_GH = r"""
$dir = $env:QL_FAKE_GH_DIR
if (Test-Path -LiteralPath (Join-Path $dir 'fail')) { exit 1 }
if ($args[0] -eq 'api') {
    $path = @($args | Where-Object { $_ -like 'repos/*' })[0]
    $page = [int](@($args | Where-Object { $_ -like 'page=*' })[0] -replace '^page=', '')
    $parts = $path.Split('/')
    if ($parts[3] -in 'pulls', 'issues') {
        $file = Join-Path $dir ('pull-{0}_{1}_{2}_{3}.json' -f $parts[1], $parts[2], $parts[4], $parts[5])
    } else {
        $file = Join-Path $dir ('status-{0}_{1}_{2}.json' -f $parts[1], $parts[2], $parts[4])
    }
    Add-Content -LiteralPath (Join-Path $dir 'calls.log') -Value "api $path page=$page"
    if (-not (Test-Path -LiteralPath $file)) { '[]'; exit 0 }
    $all = @(Get-Content -LiteralPath $file -Raw | ConvertFrom-Json)
    ConvertTo-Json -InputObject @($all | Select-Object -Skip (($page - 1) * 100) -First 100) -Depth 5
    exit 0
}
if ($args[0] -eq 'pr' -and $args[1] -eq 'list') {
    if (Test-Path -LiteralPath (Join-Path $dir 'fail-pr-list')) { exit 1 }
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


def _review(login: str, state: str, submitted: str, commit: str, rid: int = 0) -> dict:
    row = {"user": {"login": login}, "state": state, "submitted_at": submitted, "commit_id": commit}
    if rid:
        row["id"] = rid
    return row


def _dismissed(rid: int, prior: str, created: str) -> dict:
    """A review_dismissed issue event: GitHub keeps the dismissed review's earlier state only here."""
    return {"event": "review_dismissed", "created_at": created,
            "dismissed_review": {"state": prior, "review_id": rid, "dismissal_message": "stale"}}


def _front(pr: int, head: str, body: str, repo: str = "acme/widget") -> str:
    """A cos-feedback.v1 record that names its pass only in the front matter (no '# owner/repo #n / head' heading)."""
    return (f"schema: cos-feedback.v1\nproject: p\nrepo: {repo}\npr: {pr}\nhead: {head}\nreviewed_utc: 2026-10-21T09:00:00Z\n"
            f"verdict: ADVISORY\n\nADVISORY — {repo} #{pr} / {head}\n\n{body}")


SAMPLES = HERE / "fixtures" / "cos-samples"
# Real CoS records copied verbatim from the bus (<project>--pr-<n>--<doctrine commit>.md): the pass each names, and whether
# the CoS blocked it. The first five were read as clean (or not read at all) by the meter at 58806c8e.
SAMPLE_PASSES = {
    "mlv-app--pr-608--b2283ba4d6f6.md": ("SuperDimensionalSpaceFortress/MLV-App", 608, "073fc4f1a84439a8edf75cf6081defd81f08aee3", True),
    "mlv-app--pr-612--84e86b2e8171.md": ("SuperDimensionalSpaceFortress/MLV-App", 612, "a297f0e5cd0fec6260632162c0e8b203666c1e9d", True),
    "adobe-ingester--pr-4--c38e3dfd5a56.md": ("layibabalola/adobe-document-cloud-ingester", 4, "23270dcc9113118048e722b64ad617e50b79445d", True),
    "mlv-app--pr-493--8c755c572106.md": ("SuperDimensionalSpaceFortress/MLV-App", 493, "79db95afcef4959a45daa83aabe54b2c8de9fb6c", True),
    "mlv-app--pr-480--3a2c8a01e2f9.md": ("SuperDimensionalSpaceFortress/MLV-App", 480, "e5dd4b473b0afbb58acddc22d72c03e1ec26e0f8", True),
    "mlv-app--pr-578--4587c6e6dcea.md": ("SuperDimensionalSpaceFortress/MLV-App", 578, "a8858093c127a30b446a26343f7e2c30dad7781c", False),
    "mlv-app--pr-537--c4893fcf509a.md": ("SuperDimensionalSpaceFortress/MLV-App", 537, "7538f47b58a53ce35022516656a6c13800980587", False),
    "mlv-app--pr-533--5c3efd9c33ae.md": ("SuperDimensionalSpaceFortress/MLV-App", 533, "20a58d87b5ab7de25a1543281adc3aa78566dc30", False),
}


def _sample_text(name: str) -> str:
    # A Windows checkout may add CR; the bus blobs are LF.
    return (SAMPLES / name).read_text(encoding="utf-8").replace("\r\n", "\n")


def _review_comment(login: str, created: str, original_commit: str, commit: str = OTHER) -> dict:
    return {"user": {"login": login}, "created_at": created, "original_commit_id": original_commit, "commit_id": commit}


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

    def _fake_gh(self, statuses: dict[tuple[str, str], list[dict]] | None = None, prs: dict[str, list[dict]] | None = None,
                 pulls: dict[tuple[str, int, str], list[dict]] | None = None) -> Path:
        """pulls: (repo, pr, 'reviews' | 'comments') -> the native rows GitHub would return."""
        fake = self._dir()
        (fake / "gh.ps1").write_text(FAKE_GH, encoding="utf-8")
        for (repo, head), rows in (statuses or {}).items():
            owner, name = repo.split("/")
            (fake / f"status-{owner}_{name}_{head}.json").write_text(json.dumps(rows), encoding="utf-8")
        for (repo, pr, kind), rows in (pulls or {}).items():
            owner, name = repo.split("/")
            (fake / f"pull-{owner}_{name}_{pr}_{kind}.json").write_text(json.dumps(rows), encoding="utf-8")
        for repo, rows in (prs or {}).items():
            (fake / f"prs-{repo.replace('/', '_')}.json").write_text(json.dumps(rows), encoding="utf-8")
        return fake

    def _sources(self, projects: dict[str, dict]) -> Path:
        path = self._dir() / "review-sources.json"
        path.write_text(json.dumps({"schema": "fleet/quality-lift/review-sources/v1", "projects": projects}), encoding="utf-8")
        return path

    def _run(self, *args: str, env: dict[str, str] | None = None, gh: Path | None = None) -> subprocess.CompletedProcess:
        env = dict(os.environ if env is None else env)
        if gh is not None:
            env["PATH"] = str(gh) + os.pathsep + env.get("PATH", "")
            env["QL_FAKE_GH_DIR"] = str(gh)
        return subprocess.run(
            [PWSH, "-NoProfile", "-NonInteractive", "-File", str(METER), *args],
            capture_output=True, text=True, env=env, timeout=300)

    def _measure(self, project: str, out: Path, cache: dict | None = None, gh: Path | None = None,
                 since: str = "2026-10-01T00:00:00Z", expect: int = 0, declared: dict | None = REVIEW_STATUS) -> dict:
        """declared: the project's review evidence in -ReviewSources; None declares nothing for it."""
        if cache is not None:
            (out / "status-cache.json").write_text(json.dumps(cache), encoding="utf-8")
        sources = self._sources({} if declared is None else {project: declared})
        args = ["-Bus", str(self.bus), "-Project", project, "-Since", since, "-OutDir", str(out), "-NoFetch",
                "-ReviewSources", str(sources)]
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
        self.assertEqual(since["contextsBeforeCos"], {"review/key-a": {"review": True, "successBeforeCos": 1, "failureBeforeCos": 0}})
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
        self.assertEqual(since["contextsBeforeCos"]["review/key-a"], {"review": True, "successBeforeCos": 2, "failureBeforeCos": 0})
        self.assertIn(f"api repos/acme/widget/commits/{H9}/statuses page=2", (gh / "calls.log").read_text(encoding="utf-8"))

    # -- review evidence: only what the project declares counts as a factory review (CoS r1 B1 on 01639c98)

    def test_an_unrelated_ci_success_before_cos_is_not_a_factory_key(self) -> None:
        gh = self._fake_gh({
            ("acme/widget", H7): [_status("ci/lint", "success", "2026-10-21T09:00:00Z")],
            ("acme/widget", H8): [_status("ci/lint", "success", "2026-10-21T09:00:00Z"), _status("review/key-a", "success", "2026-10-21T09:30:00Z")],
        })
        out = self._dir()
        since = self._measure("flip", out, gh=gh)["windows"][0]
        self.assertEqual(since["blocked"], 3)
        self.assertEqual(since["trueMiss"], 1)  # H8's review/key-a only; ci/lint is not a factory key
        self.assertEqual(since["noPriorKey"], 2)  # H7 (ci/lint only) and H9 (nothing)
        self.assertEqual(since["statusBeforeCos"], {"noPriorStatus": 1, "successBeforeCos": 2})  # the raw proxy
        self.assertEqual(since["contextsBeforeCos"]["ci/lint"], {"review": False, "successBeforeCos": 2, "failureBeforeCos": 0})
        self.assertEqual(since["contextsBeforeCos"]["review/key-a"]["review"], True)
        rows = {r["head"]: r for r in self._corpus(out)}
        self.assertEqual((rows[H7]["successBeforeCos"], rows[H7]["reviewApprovedBeforeCos"]), (["ci/lint"], []))
        self.assertEqual(rows[H8]["reviewApprovedBeforeCos"], ["review/key-a"])

    def test_a_native_approved_review_before_cos_is_prior_factory_review(self) -> None:
        gh = self._fake_gh(pulls={
            ("acme/widget", 20, "reviews"): [_review("keybot", "APPROVED", "2026-10-21T09:00:00Z", H7)],
            # An approval of another commit, and one by an author the project does not name, prove nothing about H8.
            ("acme/widget", 21, "reviews"): [_review("keybot", "APPROVED", "2026-10-21T09:00:00Z", OTHER),
                                             _review("someone", "APPROVED", "2026-10-21T09:00:00Z", H8)],
            ("acme/widget", 22, "reviews"): [_review("keybot", "APPROVED", "2026-10-21T11:00:00Z", H9)],  # after CoS
        })
        out = self._dir()
        declared = {"nativeReviews": True, "reviewAuthors": ["keybot"]}
        result = self._measure("flip", out, gh=gh, declared=declared)
        since = result["windows"][0]
        self.assertEqual(since["trueMiss"], 1)
        self.assertEqual(since["noPriorKey"], 2)
        # The entry for a head blocked on one PR still lists it, so the next run (here without GitHub) can use it.
        self.assertEqual(json.loads((out / "status-cache.json").read_text(encoding="utf-8-sig"))[f"acme/widget@{H7}"]["prs"], [20])
        again = self._measure("flip", out, declared=declared)
        self.assertEqual((again["windows"][0]["trueMiss"], again["windows"][0]["statusUnknown"], again["errors"]), (1, 0, []))
        self.assertEqual(since["statusBeforeCos"]["noPriorStatus"], 3)
        self.assertIn("native", since["reviewSource"])
        self.assertTrue(since["nativeBeforeCos"]["pull-request-review/keybot"]["review"])
        self.assertFalse(since["nativeBeforeCos"]["pull-request-review/someone"]["review"])
        calls = (gh / "calls.log").read_text(encoding="utf-8")
        self.assertIn("api repos/acme/widget/pulls/20/reviews page=1", calls)
        self.assertIn("api repos/acme/widget/pulls/20/comments page=1", calls)

    def test_a_review_comment_before_cos_is_review_evidence_not_an_approval(self) -> None:
        gh = self._fake_gh(pulls={
            ("acme/widget", 20, "comments"): [_review_comment("keybot", "2026-10-21T09:00:00Z", H7)],
            ("acme/widget", 21, "reviews"): [_review("keybot", "COMMENTED", "2026-10-21T09:00:00Z", H8)],
            ("acme/widget", 22, "reviews"): [_review("keybot", "CHANGES_REQUESTED", "2026-10-21T09:00:00Z", H9)],
        })
        since = self._measure("flip", self._dir(), gh=gh, declared={"nativeReviews": True})["windows"][0]
        self.assertEqual((since["noPriorKey"], since["trueMiss"]), (0, 0))
        self.assertEqual(since["nativeBeforeCos"]["pull-request-review-comment/keybot"],
                         {"review": True, "successBeforeCos": 0, "failureBeforeCos": 0})
        self.assertEqual(since["nativeBeforeCos"]["pull-request-review/keybot"]["failureBeforeCos"], 1)

    def test_a_project_with_no_declared_review_source_is_unknown_not_zero(self) -> None:
        out = self._dir()
        result = self._measure("flip", out, gh=self._fake_gh(), declared=None)
        for window in result["windows"]:
            self.assertEqual((window["noPriorKey"], window["trueMiss"]), ("unknown", "unknown"), window["window"])
            self.assertEqual(window["reviewSource"], "undeclared")
        self.assertEqual(result["windows"][0]["statusBeforeCos"], {"noPriorStatus": 3, "successBeforeCos": 0})
        md = (out / "quality-lift.md").read_text(encoding="utf-8-sig")
        self.assertIn("CoS read first 24h unknown", md)
        self.assertNotIn("0 of 3", md.split("|")[0])
        self.assertTrue(all(r["reviewApprovedBeforeCos"] == "unknown" for r in self._corpus(out)))
        history = [json.loads(x) for x in (out / "history.jsonl").read_text(encoding="utf-8-sig").splitlines() if x.strip()]
        self.assertEqual((history[-1]["noPriorKey24h"], history[-1]["trueMiss24h"]), ("unknown", "unknown"))
        # Fleet mode: an undeclared project never adds to the fleet's CoS-first count.
        fleet_out = self._dir()
        proc = self._run("-Bus", str(self.bus), "-Since", "2026-10-01T00:00:00Z", "-OutDir", str(fleet_out), "-NoStatuses",
                         "-NoFetch", "-ReviewSources", str(self._sources({})))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        fleet = (fleet_out / "fleet.md").read_text(encoding="utf-8-sig")
        self.assertIn("| **fleet** | 12 | 9 (75%) | | 0 of 5 projects | unknown | unknown | 0 of 9 (9 unknown) |", fleet)

    def test_a_missing_or_malformed_review_sources_file_is_a_usage_error(self) -> None:
        out = self._dir()
        proc = self._run("-Bus", str(self.bus), "-Project", "demo", "-OutDir", str(out), "-NoStatuses", "-NoFetch",
                         "-ReviewSources", str(self.root / "no-such-sources.json"))
        self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)
        for projects in ({"demo": {"statusContexts": "review/*"}}, {"demo": {}}, {"demo": {"nativeReviews": "yes"}},
                         {"demo": {"statusContexts": [""]}}, {"demo": {"nativeReviews": True, "reviewAuthors": [7]}}):
            proc = self._run("-Bus", str(self.bus), "-Project", "demo", "-OutDir", str(out), "-NoStatuses", "-NoFetch",
                             "-ReviewSources", str(self._sources(projects)))
            self.assertEqual(proc.returncode, 4, (projects, proc.stdout + proc.stderr))
        self.assertFalse((out / "quality-lift.json").exists())

    # -- adversary pass r2 on f6b187b0

    def test_cached_native_evidence_must_cover_every_pr_the_head_was_blocked_on(self) -> None:  # A1
        bus = self._small_bus([
            ("cos-feedback/p/pr-7.md", _section(7, H1, "B1 x."), "2026-10-21T10:00:00+00:00", "pr7"),
            ("cos-feedback/p/pr-8.md", _section(8, H1, "B1 x."), "2026-10-21T10:01:00+00:00", "pr8 same head"),
        ])
        # Fetched after both passes, but by a run that had only seen PR 7.
        cache = {f"acme/widget@{H1}": {"fetched": "2026-10-21T10:02:00Z", "native": True, "dismissals": True, "prs": [7], "rows": []}}
        gh = self._fake_gh(pulls={("acme/widget", 8, "reviews"): [_review("keybot", "APPROVED", "2026-10-21T09:00:00Z", H1)]})
        result, _ = self._measure_bus(bus, "p", cache=cache, gh=gh, declared={"nativeReviews": True})
        # The approval is of the exact commit, so it precedes both blocking passes on H1.
        self.assertEqual((result["windows"][0]["noPriorKey"], result["windows"][0]["trueMiss"]), (0, 2))
        # Under -NoStatuses the same partial entry proves nothing.
        result, _ = self._measure_bus(bus, "p", cache=cache, declared={"nativeReviews": True})
        self.assertEqual(result["windows"][0]["statusUnknown"], 1)

    def test_a_project_name_is_literal_and_skipped_folders_are_named(self) -> None:  # A2
        bus = self._small_bus([
            ("cos-feedback/README.md", "readme\n", "2026-10-20T00:00:00+00:00", "seed"),
            ("cos-feedback/demo[1]/pr-7.md", _section(7, H1, "B1 x."), "2026-10-21T10:00:00+00:00", "bracketed"),
            ("cos-feedback/demo1/pr-8.md", _section(8, H2, "B1 x."), "2026-10-21T11:00:00+00:00", "a name the pattern would match"),
            ("cos-feedback/_demo/pr-9.md", _section(9, H3, "B1 x."), "2026-10-21T12:00:00+00:00", "underscore folder"),
        ])
        since = self._measure_bus(bus, "demo[1]")[0]["windows"][0]
        self.assertEqual((since["passes"], since["blocked"]), (1, 1))
        self.assertEqual(self._measure_bus(bus, "demo1")[0]["windows"][0]["passes"], 1)
        out = self._dir()
        proc = self._run("-Bus", str(bus), "-Since", "2026-10-01T00:00:00Z", "-OutDir", str(out), "-NoStatuses", "-NoFetch")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        fleet = (out / "fleet.md").read_text(encoding="utf-8-sig")
        self.assertIn("| demo[1] | 1 |", fleet)
        self.assertIn("Not measured (bus folders starting with _ or .): _demo", fleet)

    def test_equal_timestamps_keep_commit_and_listing_order(self) -> None:  # A3
        p = "cos-feedback/p/pr-7.md"
        bus = self._small_bus([
            (p, _section(7, H2, "B1 x."), "2026-10-21T10:00:00+00:00", "older: blocks H2"),
            (p, _section(7, H1, "None."), "2026-10-21T10:00:00+00:00", "child, same second: H1 clean"),
        ])
        gh = self._fake_gh({("acme/widget", H2): [_status("review/key-a", "success", "2026-10-21T09:00:00Z"),
                                                  _status("review/key-a", "pending", "2026-10-21T09:00:00Z")]})  # newest first
        since, out = self._measure_bus(bus, "p", gh=gh)
        self.assertEqual(since["windows"][0]["firstRoundBlocked"], 1)
        cache = json.loads((out / "status-cache.json").read_text(encoding="utf-8-sig"))
        rows = cache[f"acme/widget@{H2}"]["rows"]
        rows = rows if isinstance(rows, list) else [rows]
        self.assertEqual(rows[0]["state"], "success")

    def test_an_invalid_wildcard_in_review_sources_is_a_usage_error(self) -> None:  # A4
        bus = self._small_bus([("cos-feedback/p/pr-7.md", _section(7, H1, "B1 x."), "2026-10-21T10:00:00+00:00", "pr7")])
        cache = {f"acme/widget@{H1}": {"fetched": "2026-10-21T12:00:00Z", "rows": [
            {"context": "review/sol", "firstUtc": "2026-10-21T09:00:00Z", "state": "success", "successUtc": "2026-10-21T09:00:00Z", "failureUtc": None}]}}
        out = self._dir()
        (out / "status-cache.json").write_text(json.dumps(cache), encoding="utf-8")
        proc = self._run("-Bus", str(bus), "-Project", "p", "-Since", "2026-10-01T00:00:00Z", "-OutDir", str(out), "-NoStatuses", "-NoFetch",
                         "-ReviewSources", str(self._sources({"p": {"statusContexts": ["review/[z-a]"]}})))
        self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)

    def test_a_status_named_like_a_native_review_is_kept_apart(self) -> None:  # A5
        gh = self._fake_gh({("acme/widget", H7): [_status("pull-request-review/keybot", "pending", "2026-10-21T08:00:00Z")]},
                           pulls={("acme/widget", 20, "reviews"): [_review("keybot", "APPROVED", "2026-10-21T09:00:00Z", H7)]})
        since = self._measure("flip", self._dir(), gh=gh, declared={"nativeReviews": True, "reviewAuthors": ["keybot"]})["windows"][0]
        self.assertEqual(since["contextsBeforeCos"]["pull-request-review/keybot"], {"review": False, "successBeforeCos": 0, "failureBeforeCos": 0})
        self.assertEqual(since["nativeBeforeCos"]["pull-request-review/keybot"], {"review": True, "successBeforeCos": 1, "failureBeforeCos": 0})
        self.assertEqual(since["trueMiss"], 1)

    def test_a_project_named_like_a_fleet_report_is_a_named_error(self) -> None:  # A6
        bus = self._small_bus([
            ("cos-feedback/fleet.json/pr-7.md", _section(7, H1, "B1 x."), "2026-10-21T10:00:00+00:00", "collides with fleet.json"),
            ("cos-feedback/ok/pr-8.md", _section(8, H2, "B1 x."), "2026-10-21T11:00:00+00:00", "ordinary"),
        ])
        out = self._dir()
        proc = self._run("-Bus", str(bus), "-Since", "2026-10-01T00:00:00Z", "-OutDir", str(out), "-NoStatuses", "-NoFetch")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertEqual([r["project"] for r in json.loads((out / "fleet.json").read_text(encoding="utf-8-sig"))], ["ok"])
        self.assertIn("fleet.json", (out / "fleet.md").read_text(encoding="utf-8-sig").split("Errors:")[1])

    def test_a_torn_jsonl_tail_is_not_appended_to(self) -> None:  # A7
        bus = self._small_bus([("cos-feedback/p/pr-7.md", _section(7, H1, "B1 x."), "2026-10-21T10:00:00+00:00", "pr7")])
        out = self._dir()
        (out / "corpus.jsonl").write_text("{", encoding="utf-8")
        (out / "history.jsonl").write_text("{", encoding="utf-8")
        (out / "status-cache.json").write_text(json.dumps({f"acme/widget@{H1}": {"fetched": "2026-10-21T12:00:00Z", "rows": []}}), encoding="utf-8")
        proc = self._run("-Bus", str(bus), "-Project", "p", "-Since", "2026-10-01T00:00:00Z", "-OutDir", str(out), "-NoStatuses", "-NoFetch",
                         "-ReviewSources", str(self._sources({"p": REVIEW_STATUS})))
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        for name in ("corpus.jsonl", "history.jsonl"):
            lines = (out / name).read_text(encoding="utf-8-sig").splitlines()
            self.assertEqual(lines[0], "{", name)
            self.assertEqual(len(lines), 2, name)
            json.loads(lines[1])
        result = json.loads((out / "quality-lift.json").read_text(encoding="utf-8-sig"))
        self.assertTrue(any("corpus.jsonl" in e for e in result["errors"]), result["errors"])

    def test_the_lock_wait_is_timed_by_a_monotonic_clock(self) -> None:  # A8: a wall-clock step cannot be faked here
        text = METER.read_text(encoding="ascii")
        self.assertNotIn("UtcNow.AddSeconds($LockWaitSec)", text)
        self.assertIn("Diagnostics.Stopwatch", text)

    def test_a_review_comment_row_that_claims_a_success_is_malformed(self) -> None:  # A9
        bus = self._small_bus([("cos-feedback/p/pr-7.md", _section(7, H1, "B1 x."), "2026-10-21T10:00:00+00:00", "pr7")])
        row = {"context": "pull-request-review-comment/keybot", "source": "review-comment", "login": "keybot",
               "firstUtc": "2026-10-21T09:00:00Z", "state": "COMMENTED", "successUtc": "2026-10-21T09:00:00Z", "failureUtc": None}
        cache = {f"acme/widget@{H1}": {"fetched": "2026-10-21T12:00:00Z", "native": True, "prs": [7], "rows": [row]}}
        result, _ = self._measure_bus(bus, "p", cache=cache, expect=2, declared={"nativeReviews": True})
        self.assertEqual((result["windows"][0]["statusUnknown"], result["windows"][0]["trueMiss"]), (1, 0))

    def test_the_reported_since_keeps_its_fraction(self) -> None:  # A10
        result = self._measure("demo", self._dir(), since="2026-10-21T10:00:00.500Z")
        self.assertEqual(result["since"], "2026-10-21T10:00:00.5000000Z")

    def test_the_tracked_review_sources_file_parses_and_declares_review_evidence(self) -> None:
        tracked = json.loads((HERE / "review-sources.json").read_text(encoding="utf-8"))
        self.assertEqual(tracked["schema"], "fleet/quality-lift/review-sources/v1")
        for name, decl in tracked["projects"].items():
            self.assertTrue(decl.get("statusContexts") or decl.get("nativeReviews") is True, name)

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

    def test_adoption_reads_repositories_named_at_the_bus_head_even_with_no_pass_in_the_window(self) -> None:
        gh = self._fake_gh(prs={"acme/widget": [
            {"number": 5, "body": "Adversary pass: run verdict=CLEAN found=0 fixed=0", "createdAt": _iso(NOW - timedelta(hours=1))}]})
        result = self._measure("demo", self._dir(), gh=gh, since=_iso(NOW - timedelta(hours=2)))
        self.assertEqual(result["windows"][0]["passes"], 0)
        self.assertEqual(result["shiftLeft"], {"prs": 1, "withAdversaryLine": 1})

    def test_exactly_full_listings_are_complete_not_truncated(self) -> None:
        recent = _iso(NOW - timedelta(hours=1))
        full = [_status(f"ctx/{i}", "pending", "2026-10-21T11:00:00Z") for i in range(2000)]
        gh = self._fake_gh(statuses={("acme/widget", H3): full},
                           prs={"acme/widget": [{"number": n, "body": "x", "createdAt": recent} for n in range(200, 0, -1)]})
        result = self._measure("demo", self._dir(), gh=gh)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["shiftLeft"], {"prs": 200, "withAdversaryLine": 0})

    # -- passes, heads and the cache

    def _small_bus(self, commits: list[tuple[str, str, str, str]]) -> Path:
        bus = self._dir()
        _git(bus, "init", "-q")
        for rel, text, when, subject in commits:
            _commit(bus, rel, text, when, subject)
        return bus

    def _measure_bus(self, bus: Path, project: str, since: str = "2026-10-01T00:00:00Z", cache: dict | None = None,
                     expect: int = 0, gh: Path | None = None, declared: dict | None = REVIEW_STATUS) -> tuple[dict, Path]:
        out = self._dir()
        if cache is not None:
            (out / "status-cache.json").write_text(json.dumps(cache), encoding="utf-8")
        sources = self._sources({} if declared is None else {project: declared})
        args = ["-Bus", str(bus), "-Project", project, "-Since", since, "-OutDir", str(out), "-NoFetch",
                "-ReviewSources", str(sources)]
        if gh is None:
            args.append("-NoStatuses")
        proc = self._run(*args, gh=gh)
        self.assertEqual(proc.returncode, expect, proc.stdout + proc.stderr)
        return json.loads((out / "quality-lift.json").read_text(encoding="utf-8-sig")), out

    def test_the_first_head_is_ordered_by_its_first_pass_and_a_later_block_keeps_it_first(self) -> None:
        p = "cos-feedback/p/pr-7.md"
        bus = self._small_bus([
            (p, _section(7, H1, "None."), _iso(NOW - timedelta(days=3)), "h1 clean"),
            (p, _section(7, H2, "None."), _iso(NOW - timedelta(days=2)), "h2 clean"),
            (p, _section(7, H1, "B1 x."), _iso(NOW - timedelta(days=1)), "h1 blocked later"),
        ])
        since = self._measure_bus(bus, "p", since=_iso(NOW - timedelta(days=5)))[0]["windows"][0]
        self.assertEqual((since["passes"], since["blocked"], since["firstRoundBlocked"]), (2, 1, 1))

    def test_a_future_block_does_not_hide_a_clean_pass_inside_the_window(self) -> None:
        p = "cos-feedback/p/pr-7.md"
        bus = self._small_bus([
            (p, _section(7, H1, "None."), _iso(NOW - timedelta(days=1)), "h1 clean"),
            (p, _section(7, H1, "B1 x."), _iso(NOW + timedelta(days=2)), "h1 blocked in the future"),
        ])
        since = self._measure_bus(bus, "p", since=_iso(NOW - timedelta(days=5)))[0]["windows"][0]
        self.assertEqual((since["passes"], since["blocked"]), (1, 0))

    def test_a_commit_behind_an_older_dated_child_is_still_read(self) -> None:
        p = "cos-feedback/p/pr-7.md"
        bus = self._small_bus([
            (p, _section(7, H1, "B1 x."), _iso(NOW - timedelta(hours=3)), "blocked, in the window"),
            (p, _section(7, H2, "None."), "2026-09-01T10:00:00+00:00", "child with a skewed old date"),
        ])
        since = self._measure_bus(bus, "p", since=_iso(NOW - timedelta(days=1)))[0]["windows"][0]
        self.assertEqual((since["passes"], since["blocked"]), (1, 1))

    def test_only_the_blockers_heading_line_opens_the_section(self) -> None:
        text = f"# acme/widget #7 / {H1}\n\nVerdict: see the ## Blockers section below.\n\n## Blockers\n\nB1 a demonstrated defect.\n"
        bus = self._small_bus([("cos-feedback/p/pr-7.md", text, "2026-10-20T10:00:00+00:00", "inline phrase")])
        since = self._measure_bus(bus, "p")[0]["windows"][0]
        self.assertEqual(since["blocked"], 1)

    def test_one_corpus_row_per_repo_and_head_within_a_run(self) -> None:
        bus = self._small_bus([
            ("cos-feedback/p/pr-7.md", _section(7, H1, "B1 x."), "2026-10-21T10:00:00+00:00", "pr7"),
            ("cos-feedback/p/pr-8.md", _section(8, H1, "B1 x."), "2026-10-21T11:00:00+00:00", "pr8 same head"),
        ])
        cache = {f"acme/widget@{H1}": {"fetched": "2026-10-21T12:00:00Z", "rows": []}}
        result, out = self._measure_bus(bus, "p", cache=cache)
        self.assertEqual(result["corpusNew"], 1)
        self.assertEqual(len(self._corpus(out)), 1)

    def test_a_malformed_cache_entry_is_unknown_and_a_named_error(self) -> None:
        bus = self._small_bus([("cos-feedback/p/pr-7.md", _section(7, H1, "B1 x."), "2026-10-21T10:00:00+00:00", "pr7")])
        key = f"acme/widget@{H1}"
        for entry in (None, {}, {"fetched": "2026-10-21T12:00:00Z", "rows": None}, {"fetched": "2026-10-21T12:00:00Z", "rows": [None]},
                      {"fetched": "2026-10-21T12:00:00Z", "rows": "true"}, {"fetched": "2026-10-21T12:00:00Z", "rows": [{"context": "k"}]},
                      {"fetched": None, "rows": []}):
            result, out = self._measure_bus(bus, "p", cache={key: entry}, expect=2)
            since = result["windows"][0]
            self.assertEqual((since["statusUnknown"], since["noPriorKey"]), (1, 0), entry)
            self.assertTrue(any("malformed" in e for e in result["errors"]), (entry, result["errors"]))
            self.assertEqual(self._corpus(out), [], entry)
        # With statuses on, the dropped entry is refetched instead of crashing the run.
        result, out = self._measure_bus(bus, "p", cache={key: {"fetched": None, "rows": []}}, expect=2, gh=self._fake_gh())
        self.assertEqual((result["windows"][0]["statusUnknown"], result["windows"][0]["noPriorKey"]), (0, 1))

    def test_a_cache_fetched_before_the_pass_is_not_evidence_when_the_refetch_fails(self) -> None:
        bus = self._small_bus([("cos-feedback/p/pr-7.md", _section(7, H1, "B1 x."), _iso(NOW - timedelta(hours=1)), "pr7")])
        gh = self._fake_gh()
        (gh / "fail").write_text("", encoding="utf-8")
        cache = {f"acme/widget@{H1}": {"fetched": _iso(NOW - timedelta(hours=2)), "rows": []}}
        result, out = self._measure_bus(bus, "p", cache=cache, expect=2, gh=gh)
        for window in result["windows"]:
            self.assertEqual((window["statusUnknown"], window["noPriorKey"]), (1, 0), window["window"])
        self.assertEqual(self._corpus(out), [])
        result, _ = self._measure_bus(bus, "p", cache=cache)  # -NoStatuses: the same stale entry proves nothing either
        self.assertEqual((result["windows"][0]["statusUnknown"], result["windows"][0]["noPriorKey"]), (1, 0))

    def test_a_later_blocking_pass_refetches_and_is_judged_on_its_own_time(self) -> None:
        p = "cos-feedback/p/pr-7.md"
        bus = self._small_bus([
            (p, _section(7, H1, "B1 x."), _iso(NOW - timedelta(days=2)), "first block"),
            (p, _section(7, H1, "B1 still."), _iso(NOW - timedelta(hours=1)), "second block, same head"),
        ])
        gh = self._fake_gh({("acme/widget", H1): [_status("review/key-a", "success", _iso(NOW - timedelta(days=1)))]})
        cache = {f"acme/widget@{H1}": {"fetched": _iso(NOW - timedelta(days=2) + timedelta(minutes=1)), "rows": []}}
        result, _ = self._measure_bus(bus, "p", since=_iso(NOW - timedelta(days=5)), cache=cache, gh=gh)
        since, d1 = result["windows"]
        self.assertEqual((since["noPriorKey"], since["trueMiss"]), (1, 0))  # judged at the first block: success came later
        self.assertEqual((d1["noPriorKey"], d1["trueMiss"]), (0, 1))  # judged at the second block: success came before

    def test_a_cache_row_whose_context_is_not_text_is_malformed(self) -> None:
        bus = self._small_bus([("cos-feedback/p/pr-7.md", _section(7, H1, "B1 x."), "2026-10-21T10:00:00+00:00", "pr7")])
        for context in (["a", "b"], 7, True, {"k": 1}, ""):
            row = {"context": context, "firstUtc": "2026-10-21T09:00:00Z", "state": "success", "successUtc": "2026-10-21T09:00:00Z", "failureUtc": None}
            result, _ = self._measure_bus(bus, "p", cache={f"acme/widget@{H1}": {"fetched": "2026-10-21T12:00:00Z", "rows": [row]}}, expect=2)
            self.assertEqual(result["windows"][0]["statusUnknown"], 1, context)

    def test_fleet_mode_finds_a_project_whose_folder_was_deleted_in_the_window(self) -> None:
        bus = self._small_bus([
            ("cos-feedback/README.md", "readme\n", "2026-10-20T00:00:00+00:00", "seed"),
            ("cos-feedback/p/pr-7.md", _section(7, H1, "B1 x."), _iso(NOW - timedelta(hours=3)), "pr7"),
            ("cos-feedback/p/pr-7.md", None, _iso(NOW - timedelta(hours=2)), "pr7 deleted"),
        ])
        out = self._dir()
        proc = self._run("-Bus", str(bus), "-Since", _iso(NOW - timedelta(days=1)), "-OutDir", str(out), "-NoStatuses", "-NoFetch")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("| **fleet** | 1 | 1 (100%) |", (out / "fleet.md").read_text(encoding="utf-8-sig"))

    @unittest.skipUnless(os.name == "nt", "the lock is a Windows share-mode lock")
    def test_a_second_run_on_the_same_folder_waits_then_reports_the_held_lock(self) -> None:
        out = self._dir()
        with open(out / ".quality-lift.lock", "a", encoding="utf-8"):
            proc = self._run("-Bus", str(self.bus), "-Project", "demo", "-Since", "2026-10-01T00:00:00Z", "-OutDir", str(out),
                             "-NoStatuses", "-NoFetch", "-LockWaitSec", "1")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("holds the lock", proc.stdout)
        self.assertFalse((out / "quality-lift.json").exists())

    def test_a_heading_that_only_starts_with_blockers_is_not_the_section(self) -> None:
        text = f"# acme/widget #7 / {H1}\n\n## Blockers explained\nContext only.\n\n## Blockers\nB1 real defect.\n"
        bus = self._small_bus([("cos-feedback/p/pr-7.md", text, "2026-10-20T10:00:00+00:00", "heading prefix")])
        self.assertEqual(self._measure_bus(bus, "p")[0]["windows"][0]["blocked"], 1)

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
        projects = ("demo", "other", "twin", "flip", "recent")
        sources = self._sources({p: REVIEW_STATUS for p in projects})
        proc = self._run("-Bus", str(self.bus), "-Since", "2026-10-01T00:00:00Z", "-OutDir", str(out), "-NoStatuses", "-NoFetch",
                         "-ReviewSources", str(sources))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for project in projects:
            self.assertTrue((out / project / "quality-lift.json").is_file(), project)
        other = json.loads((out / "other" / "quality-lift.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(other["windows"][0]["passes"], 1)  # the later commit that deletes pr-1.md is not an error
        fleet = (out / "fleet.md").read_text(encoding="utf-8-sig")
        self.assertIn("| **fleet** | 12 | 9 (75%) | | 5 of 5 projects | 0 of 9 (9 unknown) | 0 | 0 of 9 (9 unknown) |", fleet)

    def test_fleet_markdown_names_a_project_that_errored_with_no_passes(self) -> None:
        bus = self._dir()
        _git(bus, "init", "-q")
        _commit(bus, "cos-feedback/x/pr-1.md", _section(1, H1, "B1 x."), "2026-10-20T00:00:00+00:00", "x")
        blob = _git(bus, "rev-parse", "HEAD:cos-feedback/x/pr-1.md")
        loose = bus / ".git" / "objects" / blob[:2] / blob[2:]
        os.chmod(loose, stat.S_IWRITE | stat.S_IREAD)
        loose.unlink()
        out = self._dir()
        proc = self._run("-Bus", str(bus), "-Since", "2026-10-01T00:00:00Z", "-OutDir", str(out), "-NoStatuses", "-NoFetch")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        # Its bus read failed, so it is named as not measured, never as 0 passes (adversary r3 A4).
        self.assertRegex((out / "fleet.md").read_text(encoding="utf-8-sig"), r"(?m)^\| x \| not measured \|.*\| 1 \|$")

    # -- paths git would quote (CI DG-GIT-PATHLIST on 01639c98)

    def test_a_project_folder_git_would_quote_is_still_measured(self) -> None:
        project = "café"
        p = f"cos-feedback/{project}/pr-7.md"
        bus = self._small_bus([
            ("cos-feedback/README.md", "readme\n", "2026-10-20T00:00:00+00:00", "seed (keeps cos-feedback/)"),
            (p, _section(7, H1, "B1 x."), _iso(NOW - timedelta(hours=3)), "pr7"),
            (f"cos-feedback/{project}/pr-8.md", _section(8, H2, "None."), _iso(NOW - timedelta(hours=2)), "pr8"),
        ])
        since = self._measure_bus(bus, project, since=_iso(NOW - timedelta(days=1)))[0]["windows"][0]
        self.assertEqual((since["passes"], since["blocked"]), (2, 1))
        # Fleet mode finds the folder from the log alone once it is deleted from the checkout.
        _commit(bus, p, None, _iso(NOW - timedelta(hours=1)), "pr7 deleted")
        _commit(bus, f"cos-feedback/{project}/pr-8.md", None, _iso(NOW - timedelta(minutes=30)), "pr8 deleted")
        out = self._dir()
        proc = self._run("-Bus", str(bus), "-Since", _iso(NOW - timedelta(days=1)), "-OutDir", str(out), "-NoStatuses", "-NoFetch")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"| {project} | 2 | 1 (50%) |", (out / "fleet.md").read_text(encoding="utf-8-sig"))

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

    # -- r3 on 58806c8e: CoS markdown variants (key3 B3, catalogue D-03) and dismissed native reviews (sol r2)

    def _blocked_heads(self, bus: Path, project: str, heads: list[str], repo: str = "acme/widget") -> tuple[dict, list[str]]:
        """Measures with known (empty) evidence for every head, so the corpus lists exactly the heads read as blocked."""
        cache = {f"{repo}@{h}": {"fetched": "2026-10-25T00:00:00Z", "rows": []} for h in heads}
        result, out = self._measure_bus(bus, project, cache=cache)
        return result, [r["head"] for r in self._corpus(out)]

    def test_markdown_blocker_forms_in_the_blockers_section_are_blocked(self) -> None:
        blocked = ["- B1 x.", "* B1 x.", "+ B2 x.", "**B1** x.", "__B1__ x.", "*B1* x.", "1. B1 x.", "2) B1 x.",
                   "- **B1 / P1:** `a.ps1:3` x.", "> B1 x.", "### B1 x.", "  - B1 indented."]
        clean = ["None.", "- None demonstrated.", "**None demonstrated.**", "1. None.",
                 "No new demonstrated blocker. Prior B1 is source-fixed.", "- Prior B1 is source-fixed.", "Blocked by nothing; B1 closed."]
        commits, heads = [], []
        for i, text in enumerate(blocked + clean):
            head = f"{i + 1:040x}"
            heads.append(head)
            commits.append((f"cos-feedback/p/pr-{i + 1}.md", _section(i + 1, head, text), f"2026-10-21T10:{i:02d}:00+00:00", f"form {i}"))
        result, got = self._blocked_heads(self._small_bus(commits), "p", heads)
        self.assertEqual(sorted(got), sorted(heads[:len(blocked)]),
                         [(t, h in got) for t, h in zip(blocked + clean, heads)])
        since = result["windows"][0]
        self.assertEqual((since["passes"], since["blocked"], since["firstRoundBlocked"]), (len(heads), len(blocked), len(blocked)))

    def test_a_b_line_after_the_blockers_section_ends_is_not_a_blocker(self) -> None:
        texts = [
            f"# acme/widget #1 / {H1}\n\n## Blockers\n\n## Improvements\n- B1 an improvement numbered like a blocker.\n",
            f"# acme/widget #2 / {H2}\n\n## Blockers\n\n# Appendix\nB1 x.\n",
            f"# acme/widget #3 / {H3}\n\n## Blockers\nNone.\n\n## Improvements\n- B1 x.\n",
            f"# acme/widget #4 / {H4}\n\nBlockers: None demonstrated.\nImprovements:\n- B1 x.\n",
        ]
        commits = [(f"cos-feedback/p/pr-{i + 1}.md", t, f"2026-10-21T10:0{i}:00+00:00", f"t{i}") for i, t in enumerate(texts)]
        result, got = self._blocked_heads(self._small_bus(commits), "p", [H1, H2, H3, H4])
        self.assertEqual(got, [])
        self.assertEqual((result["windows"][0]["passes"], result["windows"][0]["blocked"]), (4, 0))

    def test_blockers_heading_and_label_variants_open_the_section(self) -> None:
        bodies = [
            ("## Blockers (2026-10-19T15:36:51Z)\n- B1 x.\n", True),
            ("## Blockers:\nB1 x.\n", True),
            ("### Blockers\n\n- B1 x.\n", True),
            ("## **Blockers**\n- B1 x.\n", True),
            ("Blockers: B1 (medium confidence) x.\n\nImprovements: none.\n", True),
            ("**Blockers:** B1 x.\n", True),
            ("Blockers:\n- B1 x.\n", True),
            ("## Blockers (none)\nNone demonstrated.\n", False),
            ("Blockers: None demonstrated in this pass.\n", False),
        ]
        commits, heads, want = [], [], []
        for i, (body, is_blocked) in enumerate(bodies):
            head = f"{i + 1:040x}"
            heads.append(head)
            if is_blocked:
                want.append(head)
            commits.append((f"cos-feedback/p/pr-{i + 1}.md", f"# acme/widget #{i + 1} / {head}\n\nVerdict.\n\n{body}",
                            f"2026-10-21T10:{i:02d}:00+00:00", f"v{i}"))
        _, got = self._blocked_heads(self._small_bus(commits), "p", heads)
        self.assertEqual(sorted(got), sorted(want), [(b, h in got) for (b, _), h in zip(bodies, heads)])

    def test_front_matter_names_the_pass_when_there_is_no_heading(self) -> None:
        bus = self._small_bus([
            ("cos-feedback/p/pr-7.md", _front(7, H1, "## Blockers\n- B1 x.\n"), "2026-10-21T10:00:00+00:00", "front matter, blocked"),
            ("cos-feedback/p/pr-8.md", _front(8, H2, "## Blockers\nNone demonstrated.\n"), "2026-10-21T10:01:00+00:00", "front matter, clean"),
            # A heading wins over the front matter: it is what a multi-section file appends per head.
            ("cos-feedback/p/pr-9.md", _front(9, H3, f"# acme/widget #9 / {H4}\n\n## Blockers\nB1 x.\n"), "2026-10-21T10:02:00+00:00", "both"),
            # Front matter that does not name a full head names no pass.
            ("cos-feedback/p/pr-10.md", "schema: cos-feedback.v1\nrepo: acme/widget\npr: 10\nhead: abc123 (merged)\n\n## Blockers\n- B1 x.\n",
             "2026-10-21T10:03:00+00:00", "bad head"),
            ("cos-feedback/p/pr-11.md", "# Agent-share listing: ADVISORY\n\n## Blockers\n- B1 x.\n", "2026-10-21T10:04:00+00:00", "no identity"),
        ])
        result, got = self._blocked_heads(bus, "p", [H1, H2, H3, H4])
        self.assertEqual(sorted(got), sorted([H1, H4]))
        since = result["windows"][0]
        self.assertEqual((since["passes"], since["blocked"], since["unparsed"]), (3, 2, 2))
        self.assertEqual(result["windows"][1]["unparsed"], 0)  # the 24 h window holds none of them

    def test_an_unparsed_cos_file_is_counted_and_named_not_silently_skipped(self) -> None:
        bus = self._small_bus([("cos-feedback/p/pr-11.md", "# Agent-share listing: ADVISORY\n\n## Blockers\n- B1 x.\n",
                                _iso(NOW - timedelta(hours=1)), "no identity")])
        result, out = self._measure_bus(bus, "p", since=_iso(NOW - timedelta(days=1)))
        self.assertEqual([w["unparsed"] for w in result["windows"]], [1, 1])
        md = (out / "quality-lift.md").read_text(encoding="utf-8-sig")
        self.assertIn("not measured: 1", md)
        fleet_out = self._dir()
        proc = self._run("-Bus", str(bus), "-Since", _iso(NOW - timedelta(days=1)), "-OutDir", str(fleet_out), "-NoStatuses", "-NoFetch")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("CoS files that name no pass (not measured): p 1", (fleet_out / "fleet.md").read_text(encoding="utf-8-sig"))

    def test_real_cos_records_are_read_as_the_cos_wrote_them(self) -> None:
        commits, by_project = [], {}
        for i, (name, (repo, pr, head, _)) in enumerate(sorted(SAMPLE_PASSES.items())):
            project = name.split("--")[0]
            by_project.setdefault(project, []).append((repo, head))
            commits.append((f"cos-feedback/{project}/pr-{pr}.md", _sample_text(name), f"2026-10-21T1{i}:00:00+00:00", name))
        bus = self._small_bus(commits)
        for project, passes in by_project.items():
            cache = {f"{repo}@{head}": {"fetched": "2026-10-25T00:00:00Z", "rows": []} for repo, head in passes}
            result, out = self._measure_bus(bus, project, cache=cache)
            want = sorted(h for n, (_, _, h, b) in SAMPLE_PASSES.items() if b and n.startswith(project + "--"))
            self.assertEqual(sorted(r["head"] for r in self._corpus(out)), want, project)
            self.assertEqual((result["windows"][0]["passes"], result["windows"][0]["unparsed"]), (len(passes), 0), project)

    @unittest.skipUnless(os.environ.get("FACTORY_BUS_CLONE"), "binds the samples to the bus; set FACTORY_BUS_CLONE to a bus clone")
    def test_the_real_records_match_the_bus_at_their_doctrine_commit(self) -> None:
        bus = Path(os.environ["FACTORY_BUS_CLONE"])
        self.assertEqual(sorted(p.name for p in SAMPLES.glob("*.md")), sorted(SAMPLE_PASSES))
        for name, (_, pr, _, _) in SAMPLE_PASSES.items():
            project, _, commit = name[:-3].split("--")
            blob = subprocess.run(["git", "-C", str(bus), "show", f"{commit}:cos-feedback/{project}/pr-{pr}.md"],
                                  capture_output=True, check=True).stdout.decode("utf-8")
            self.assertEqual(_sample_text(name), blob, name)

    def test_adoption_reads_a_repository_named_only_in_front_matter(self) -> None:
        bus = self._small_bus([("cos-feedback/p/pr-5.md", _front(5, H1, "## Blockers\nNone.\n"), "2026-10-01T00:00:00+00:00", "old")])
        gh = self._fake_gh(prs={"acme/widget": [
            {"number": 6, "body": "Adversary pass: run verdict=CLEAN found=0 fixed=0", "createdAt": _iso(NOW - timedelta(hours=1))}]})
        result, _ = self._measure_bus(bus, "p", since=_iso(NOW - timedelta(hours=2)), gh=gh)
        self.assertEqual(result["shiftLeft"], {"prs": 1, "withAdversaryLine": 1})

    NATIVE_KEYBOT = {"nativeReviews": True, "reviewAuthors": ["keybot"]}

    def test_a_dismissed_approval_before_cos_is_still_a_prior_approval(self) -> None:
        # GitHub returns a dismissed review as DISMISSED with its original submitted_at; its earlier state is on the event.
        gh = self._fake_gh(pulls={
            ("acme/widget", 20, "reviews"): [_review("keybot", "DISMISSED", "2026-10-21T09:00:00Z", H7, rid=101)],
            ("acme/widget", 20, "events"): [_dismissed(101, "approved", "2026-10-21T11:00:00Z")],  # dismissed after CoS
            ("acme/widget", 21, "reviews"): [_review("keybot", "DISMISSED", "2026-10-21T09:00:00Z", H8, rid=102)],
            ("acme/widget", 21, "events"): [_dismissed(102, "changes_requested", "2026-10-21T11:00:00Z")],
            ("acme/widget", 22, "reviews"): [_review("keybot", "APPROVED", "2026-10-21T09:00:00Z", H9, rid=103)],
        })
        out = self._dir()
        since = self._measure("flip", out, gh=gh, declared=self.NATIVE_KEYBOT)["windows"][0]
        self.assertEqual((since["trueMiss"], since["noPriorKey"], since["statusUnknown"]), (2, 0, 0))
        self.assertEqual(since["nativeBeforeCos"]["pull-request-review/keybot"], {"review": True, "successBeforeCos": 2, "failureBeforeCos": 1})
        rows = {r["head"]: r for r in self._corpus(out)}
        self.assertEqual((rows[H7]["nativeApprovedBeforeCos"], rows[H8]["nativeApprovedBeforeCos"]), (["pull-request-review/keybot"], []))
        calls = (gh / "calls.log").read_text(encoding="utf-8")
        self.assertIn("api repos/acme/widget/issues/20/events page=1", calls)
        self.assertNotIn("issues/22/events", calls)  # no dismissed review there, so no history is read
        # The next run reads the same evidence from the cache.
        again = self._measure("flip", out, declared=self.NATIVE_KEYBOT)["windows"][0]
        self.assertEqual((again["trueMiss"], again["statusUnknown"]), (2, 0))

    def test_a_dismissed_review_whose_history_cannot_be_read_is_unknown_not_zero(self) -> None:
        gh = self._fake_gh(pulls={
            ("acme/widget", 20, "reviews"): [_review("keybot", "DISMISSED", "2026-10-21T09:00:00Z", H7, rid=101)],
            ("acme/widget", 21, "reviews"): [_review("keybot", "DISMISSED", "2026-10-21T09:00:00Z", H8, rid=102)],
            ("acme/widget", 21, "events"): [_dismissed(999, "approved", "2026-10-21T11:00:00Z")],  # another review's event
        })
        out = self._dir()
        since = self._measure("flip", out, gh=gh, declared=self.NATIVE_KEYBOT)["windows"][0]
        self.assertEqual((since["statusUnknown"], since["trueMiss"], since["noPriorKey"]), (2, 0, 1))  # H9 has no review
        self.assertEqual([r["head"] for r in self._corpus(out)], [H9], "an unknown head must not be frozen into the corpus")
        # A known approval on the same head decides it, whatever the dismissed review was.
        gh = self._fake_gh(pulls={("acme/widget", 20, "reviews"): [
            _review("keybot", "DISMISSED", "2026-10-21T08:00:00Z", H7, rid=101), _review("keybot", "APPROVED", "2026-10-21T09:00:00Z", H7, rid=104)]})
        since = self._measure("flip", self._dir(), gh=gh, declared=self.NATIVE_KEYBOT)["windows"][0]
        self.assertEqual((since["statusUnknown"], since["trueMiss"]), (0, 1))

    def test_a_native_cache_entry_without_dismissal_history_is_not_evidence(self) -> None:
        # Written by a meter that read a dismissed review as no approval: refetched with GitHub, unknown without it.
        bus = self._small_bus([("cos-feedback/p/pr-7.md", _section(7, H1, "B1 x."), "2026-10-21T10:00:00+00:00", "pr7")])
        cache = {f"acme/widget@{H1}": {"fetched": "2026-10-21T12:00:00Z", "native": True, "prs": [7], "rows": []}}
        result, _ = self._measure_bus(bus, "p", cache=cache, declared=self.NATIVE_KEYBOT)
        self.assertEqual(result["windows"][0]["statusUnknown"], 1)
        gh = self._fake_gh(pulls={("acme/widget", 7, "reviews"): [_review("keybot", "DISMISSED", "2026-10-21T09:00:00Z", H1, rid=5)],
                                  ("acme/widget", 7, "events"): [_dismissed(5, "approved", "2026-10-21T11:00:00Z")]})
        result, _ = self._measure_bus(bus, "p", cache=cache, gh=gh, declared=self.NATIVE_KEYBOT)
        self.assertEqual((result["windows"][0]["statusUnknown"], result["windows"][0]["trueMiss"]), (0, 1))

    # -- adversary pass r3 on b522ea11

    def test_more_heading_and_emphasis_forms_are_read(self) -> None:  # A1, A2
        bodies = ["## Blockers [2026-10-21]\nB1 x.\n", "## **Blockers:**\nB1 x.\n", "## __Blockers:__\nB1 x.\n", "## **Blockers**:\nB1 x.\n",
                  "## Blockers\n***B1*** x.\n", "## Blockers\n___B1___ x.\n", "## Blockers\n- ***B1 / P1:*** x.\n"]
        commits, heads = [], []
        for i, body in enumerate(bodies):
            head = f"{i + 1:040x}"
            heads.append(head)
            commits.append((f"cos-feedback/p/pr-{i + 1}.md", f"# acme/widget #{i + 1} / {head}\n\n{body}", f"2026-10-21T10:0{i}:00+00:00", f"v{i}"))
        _, got = self._blocked_heads(self._small_bus(commits), "p", heads)
        self.assertEqual(sorted(got), sorted(heads), [(b, h in got) for b, h in zip(bodies, heads)])

    def test_an_uppercase_head_names_the_pass_in_lowercase(self) -> None:  # A3
        upper = H1.upper().replace("1", "A")
        bus = self._small_bus([
            ("cos-feedback/p/pr-7.md", f"# acme/widget #7 / {H2}\n\n## Blockers\nNone.\n\n# acme/widget #7 / {upper}\n\n## Blockers\nB1 x.\n",
             "2026-10-21T10:00:00+00:00", "uppercase heading after a lowercase one"),
            ("cos-feedback/p/pr-8.md", _front(8, H3.upper().replace("3", "C"), "## Blockers\n- B1 x.\n"), "2026-10-21T10:01:00+00:00", "uppercase front matter"),
        ])
        result, got = self._blocked_heads(bus, "p", [upper.lower(), H3.upper().replace("3", "C").lower()])
        self.assertEqual(sorted(got), sorted([upper.lower(), "c" * 40]))
        self.assertEqual((result["windows"][0]["passes"], result["windows"][0]["unparsed"]), (2, 0))

    def test_an_unreadable_bus_is_not_measured_not_a_known_zero(self) -> None:  # A4, A5
        bus = self._dir()
        _git(bus, "init", "-q")
        _commit(bus, "cos-feedback/x/pr-1.md", _section(1, H1, "B1 x."), "2026-10-20T00:00:00+00:00", "x")
        _commit(bus, "cos-feedback/y/pr-2.md", _section(2, H2, "B1 x."), "2026-10-20T01:00:00+00:00", "y")
        blob = _git(bus, "rev-parse", "HEAD:cos-feedback/x/pr-1.md")
        loose = bus / ".git" / "objects" / blob[:2] / blob[2:]
        os.chmod(loose, stat.S_IWRITE | stat.S_IREAD)
        loose.unlink()
        out = self._dir()
        proc = self._run("-Bus", str(bus), "-Since", "2026-10-01T00:00:00Z", "-OutDir", str(out), "-NoStatuses", "-NoFetch",
                         "-ReviewSources", str(self._sources({"x": REVIEW_STATUS, "y": REVIEW_STATUS})))
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        x = json.loads((out / "x" / "quality-lift.json").read_text(encoding="utf-8-sig"))
        self.assertEqual([w["measured"] for w in x["windows"]], [False, False])
        self.assertTrue((out / "x" / "quality-lift.md").read_text(encoding="utf-8-sig").startswith("QUALITY-LIFT x NOT MEASURED"))
        self.assertFalse((out / "x" / "history.jsonl").exists(), "a failed read must not add a zero to the trend")
        fleet = (out / "fleet.md").read_text(encoding="utf-8-sig")
        self.assertRegex(fleet, r"(?m)^\| x \| not measured \|")
        self.assertIn("| **fleet** | 1 | 1 (100%) |", fleet)  # y only
        self.assertIn("Not measured, so not in the fleet totals: x", fleet)
        self.assertTrue(json.loads((out / "y" / "quality-lift.json").read_text(encoding="utf-8-sig"))["windows"][0]["measured"])

    def test_a_failed_pr_listing_leaves_adoption_unknown_not_zero(self) -> None:  # A4
        gh = self._fake_gh(prs={"acme/widget": [{"number": 5, "body": "Adversary pass: x", "createdAt": _iso(NOW - timedelta(hours=1))}]})
        (gh / "fail-pr-list").write_text("", encoding="utf-8")
        result = self._measure("twin", self._dir(), gh=gh, expect=2)  # twin names acme/widget and fork/widget
        self.assertEqual(result["shiftLeft"], {"prs": None, "withAdversaryLine": None})

    def test_a_held_lock_in_fleet_mode_is_named_not_measured(self) -> None:  # A5
        bus = self._small_bus([("cos-feedback/p/pr-7.md", _section(7, H1, "B1 x."), "2026-10-21T10:00:00+00:00", "pr7"),
                               ("cos-feedback/q/pr-8.md", _section(8, H2, "B1 x."), "2026-10-21T11:00:00+00:00", "pr8")])
        out = self._dir()
        (out / "p").mkdir()
        with open(out / "p" / ".quality-lift.lock", "a", encoding="utf-8"):
            proc = self._run("-Bus", str(bus), "-Since", "2026-10-01T00:00:00Z", "-OutDir", str(out), "-NoStatuses", "-NoFetch", "-LockWaitSec", "0")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        fleet = (out / "fleet.md").read_text(encoding="utf-8-sig")
        self.assertRegex(fleet, r"(?m)^\| p \| not measured \|")
        self.assertIn("| **fleet** | 1 | 1 (100%) |", fleet)
        self.assertFalse((out / "p" / "quality-lift.json").exists())

    def test_a_corpus_row_from_weaker_evidence_is_superseded(self) -> None:  # A6
        bus = self._small_bus([("cos-feedback/p/pr-7.md", _section(7, H1, "B1 x."), "2026-10-21T10:00:00+00:00", "pr7")])
        out = self._dir()
        # A row written by the 58806c8e meter, which read the dismissed approval as no approval.
        (out / "corpus.jsonl").write_text(json.dumps({
            "addedUtc": "2026-10-21T12:00:00Z", "cosUtc": "2026-10-21T10:00:00Z", "repo": "acme/widget", "pr": 7, "head": H1,
            "blocker": "B1 x.", "successBeforeCos": [], "reviewApprovedBeforeCos": [], "nativeApprovedBeforeCos": [],
            "reviewSource": "native reviews by keybot"}) + "\n", encoding="utf-8")
        gh = self._fake_gh(pulls={("acme/widget", 7, "reviews"): [_review("keybot", "DISMISSED", "2026-10-21T09:00:00Z", H1, rid=5)],
                                  ("acme/widget", 7, "events"): [_dismissed(5, "approved", "2026-10-21T11:00:00Z")]})
        sources = self._sources({"p": self.NATIVE_KEYBOT})
        args = ["-Bus", str(bus), "-Project", "p", "-Since", "2026-10-01T00:00:00Z", "-OutDir", str(out), "-NoFetch", "-ReviewSources", str(sources)]
        self.assertEqual(self._run(*args, gh=gh).returncode, 0)
        rows = self._corpus(out)
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[-1]["nativeApprovedBeforeCos"], rows[-1]["supersedes"]), (["pull-request-review/keybot"], True))
        self.assertEqual(self._run(*args, gh=gh).returncode, 0)
        self.assertEqual(len(self._corpus(out)), 2, "the same evidence basis is written once")
        # A head first written while its project declared nothing gets a declared row once it declares.
        out2 = self._dir()
        cache = {f"acme/widget@{H1}": {"fetched": "2026-10-21T12:00:00Z", "rows": []}}
        (out2 / "status-cache.json").write_text(json.dumps(cache), encoding="utf-8")
        for declared in ({}, {"p": REVIEW_STATUS}):
            proc = self._run("-Bus", str(bus), "-Project", "p", "-Since", "2026-10-01T00:00:00Z", "-OutDir", str(out2), "-NoFetch",
                             "-NoStatuses", "-ReviewSources", str(self._sources(declared)))
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual([r["reviewApprovedBeforeCos"] for r in self._corpus(out2)], ["unknown", []])

    def test_a_cache_time_that_is_not_text_is_malformed(self) -> None:  # A7
        bus = self._small_bus([("cos-feedback/p/pr-7.md", _section(7, H1, "B1 x."), "2026-10-21T10:00:00+00:00", "pr7")])
        base = {"context": "pull-request-review/keybot", "source": "review", "login": "keybot", "firstUtc": "2026-10-21T09:00:00Z",
                "state": "DISMISSED", "successUtc": None, "failureUtc": None}
        for field in ("unknownUtc", "successUtc", "failureUtc", "firstUtc"):
            row = dict(base, **{field: {}})
            cache = {f"acme/widget@{H1}": {"fetched": "2026-10-22T00:00:00Z", "native": True, "dismissals": True, "prs": [7], "rows": [row]}}
            result, _ = self._measure_bus(bus, "p", cache=cache, expect=2, declared=self.NATIVE_KEYBOT)
            self.assertEqual(result["windows"][0]["statusUnknown"], 1, field)

    def test_the_meter_is_ascii_and_names_no_board_path(self) -> None:
        raw = METER.read_bytes()
        self.assertTrue(all(b < 128 for b in raw), "Measure-FleetQualityLift.ps1 must be ASCII-only")
        text = raw.decode("ascii").lower()
        for term in ("mlv", "layi wkspc", ".claude-state", "fleet-runs"):
            self.assertNotIn(term, text)


if __name__ == "__main__":
    unittest.main()
