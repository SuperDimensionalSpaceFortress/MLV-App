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
# Real CoS records copied verbatim from the bus (<project>--pr-<n>--<doctrine commit>.md): the pass each names, and what the
# CoS said of its blockers: blocked, clean, or unknown (counted apart, never as clean). Since r5 (B7) the class is read
# conservatively: blocked only from a line that starts B<n> or SHARED BLOCKER, clean only from a whole section on the exact
# whitelist in blocker-classes.json, unknown otherwise. "r4: x" notes a record r4's meaning-based reading labelled x; each
# moved to unknown because its text is prose or mixed, never between blocked and clean.
SAMPLE_PASSES = {
    "mlv-app--pr-608--b2283ba4d6f6.md": ("SuperDimensionalSpaceFortress/MLV-App", 608, "073fc4f1a84439a8edf75cf6081defd81f08aee3", "blocked"),
    "mlv-app--pr-612--84e86b2e8171.md": ("SuperDimensionalSpaceFortress/MLV-App", 612, "a297f0e5cd0fec6260632162c0e8b203666c1e9d", "blocked"),
    # r4: blocked. Its B1 line also says 'Keep fixed allowlisted labels', and a B<n> line that says fixed is not read as blocked.
    "adobe-ingester--pr-4--c38e3dfd5a56.md": ("layibabalola/adobe-document-cloud-ingester", 4, "23270dcc9113118048e722b64ad617e50b79445d", "unknown"),
    "mlv-app--pr-493--8c755c572106.md": ("SuperDimensionalSpaceFortress/MLV-App", 493, "79db95afcef4959a45daa83aabe54b2c8de9fb6c", "blocked"),
    "mlv-app--pr-480--3a2c8a01e2f9.md": ("SuperDimensionalSpaceFortress/MLV-App", 480, "e5dd4b473b0afbb58acddc22d72c03e1ec26e0f8", "blocked"),
    # r4: clean. 'None demonstrated. No SHARED BLOCKER.' followed by more sentences on the same line: not the whole section.
    "mlv-app--pr-578--4587c6e6dcea.md": ("SuperDimensionalSpaceFortress/MLV-App", 578, "a8858093c127a30b446a26343f7e2c30dad7781c", "unknown"),
    # r4: clean. 'None demonstrated in the independent source-first pass.' plus two more sentences.
    "mlv-app--pr-537--c4893fcf509a.md": ("SuperDimensionalSpaceFortress/MLV-App", 537, "7538f47b58a53ce35022516656a6c13800980587", "unknown"),
    # No Blockers section at all: the CoS stated nothing about blockers, so it is unknown, not clean.
    "mlv-app--pr-533--5c3efd9c33ae.md": ("SuperDimensionalSpaceFortress/MLV-App", 533, "20a58d87b5ab7de25a1543281adc3aa78566dc30", "unknown"),
    # r4: blocked. 'Prior612 B1 persists' plus a failing shard, in prose: no line starts B<n>.
    "mlv-app--pr-613--b26f3e7db3d4.md": ("SuperDimensionalSpaceFortress/MLV-App", 613, "a6fa535270bd665c1445e542e023e7f042a9e51f", "unknown"),
    # r4: blocked (all three). 'Prior829ec450 B1 remains', 'Inherited #267 B1 remains', 'Inherited ownership B1 remains': prose.
    "agent-bridge--pr-267--6049af278f2d.md": ("layibabalola/agent-bridge", 267, "ce87de3e3b9f2e4ebab949dc205917a1609a056e", "unknown"),
    "agent-bridge--pr-268--c6d474d8461f.md": ("layibabalola/agent-bridge", 268, "a9fd2604ee0f8286163034a40e9c02c15d5ca0e1", "unknown"),
    "agent-bridge--pr-270--6a1d388d7b15.md": ("layibabalola/agent-bridge", 270, "5db5a5ea6d950df2d4804e6a617f788217f6ceae", "unknown"),
    # r4: blocked. 'Inherited #4 B1 remains: ...': prose.
    "adobe-ingester--pr-5--3af1813220b0.md": ("layibabalola/adobe-document-cloud-ingester", 5, "ea8cf4558da328b4c37db505e55938d9389e1baf", "unknown"),
    # r4: blocked. The bus SCHEMA.md bullet form '- CI: ... job119584694731 fails gate...': a CI failure in prose.
    "context-ultra-salesforce--pr-112--41b9dbaeb8ce.md": ("layibabalola/context-ultra-salesforce", 112, "e6601ebf4477f011b75d635d4941e341aa9ad365", "unknown"),
    # r4: blocked. A path bullet: '- tools/...:193: actual [Windows shard 2](...) fails at 07:42:20Z'.
    "mlv-app--pr-508--3636d6cb349f.md": ("SuperDimensionalSpaceFortress/MLV-App", 508, "24fdc1986e94655da05adab911904ceb811893a7", "unknown"),
    # Opens 'Prior timing B1 is source-fixed', and its second paragraph starts 'B2 validation mismatch remains.'.
    "mlv-app--pr-657--c4ead2a24093.md": ("SuperDimensionalSpaceFortress/MLV-App", 657, "4ea77b883438c587583fa6c1de45141b7b397f5b", "blocked"),
    # r4: blocked. 'actual [Windows shard 1](...) and [Ubuntu shard 3](...) fail: ...': a CI failure in prose.
    "mlv-app--pr-465--3636d6cb349f.md": ("SuperDimensionalSpaceFortress/MLV-App", 465, "37b6e55bafed02078740512f8c07a3f966d8f369", "unknown"),
    # r4: blocked. 'No new demonstrated defect ... Existing CI is not green: ... failed': mixed.
    "mlv-app--pr-663--6485a13f0388.md": ("SuperDimensionalSpaceFortress/MLV-App", 663, "32d97f32dd4f1bc84bc1d6ed951f74416fd68b2e", "unknown"),
    # r4: clean. 'None newly demonstrated. Prior B1/B2 are source-addressed', then 'B1: <how>' and 'B2: <how>' lines: mixed (a
    # B<n> line under a None opener is unknown, not blocked).
    "mlv-app--pr-689--120cf0f02411.md": ("SuperDimensionalSpaceFortress/MLV-App", 689, "58806c8ebf76683374593d1454a5ffe56f34833b", "unknown"),
    # r4: clean. 'Prior B1 is source-fixed. ... No new demonstrated blocker ...': prose.
    "mlv-app--pr-664--7678ef2deb44.md": ("SuperDimensionalSpaceFortress/MLV-App", 664, "87f34507ef29f7c344cbf3495161a9c85f571251", "unknown"),
    # r4: clean. 'No remaining B2 demonstrated in this delta. Prior SHARED BLOCKER source-resolved: ...': prose.
    "mlv-app--pr-456--b8398105b1d2.md": ("SuperDimensionalSpaceFortress/MLV-App", 456, "580e6c6b55ee6f2d8e8628249277f054037ed6e7", "unknown"),
    # r4: clean. 'No current failing check demonstrated. Prior B1 was an actual failure of ... job...': prose.
    "mlv-app--pr-671--c3c8acfcab09.md": ("SuperDimensionalSpaceFortress/MLV-App", 671, "06ebdd1903e8474d385fe504323f4c0358aeca06", "unknown"),
    # r4: clean. 'None demonstrated in corrective delta. Prior612/613 B1 closed: ...': prose.
    "mlv-app--pr-618--f6c06ffc9be6.md": ("SuperDimensionalSpaceFortress/MLV-App", 618, "7570642b94b1ca01efcfa6c1b39cbbb9252fcffc", "unknown"),
    # r4: clean. 'None newly demonstrated. Prior B3/B4 source remedies verified:' over bullets: more than the whitelist.
    "mlv-app--pr-689--8da382374548.md": ("SuperDimensionalSpaceFortress/MLV-App", 689, "7f6189570adfe4fdc3877e327f3a9c901c768620", "unknown"),
    # 'P2, high confidence: ...', a G1 governance hold, and G2/G3 holds with 'none has failed': not B<n>, not no-blocker.
    "mlv-app--pr-525--c7453f5ec647.md": ("SuperDimensionalSpaceFortress/MLV-App", 525, "c831ed5f348472fc0695dc003b8d5a119510f4e3", "unknown"),
    "mlv-app--pr-507--3c7af4ac0f2f.md": ("SuperDimensionalSpaceFortress/MLV-App", 507, "825bd26ca3093e9fef1ba4be4aa556b56f3c026f", "unknown"),
    "mlv-app--pr-480--cffec45e2b99.md": ("SuperDimensionalSpaceFortress/MLV-App", 480, "bd745d6e13f471cd8f47ec01f813347e6e4e9943", "unknown"),
}

# Loads the meter's functions and $Ql* values from its syntax tree (no top-level code runs) and times each case: the best of
# three runs after a warm-up, so first-call compilation is not counted as the parser's cost.
TIMING_HELPER = r"""
param([string]$Meter, [string]$Cases, [string]$Out)
$ErrorActionPreference = 'Stop'
$ast = [Management.Automation.Language.Parser]::ParseFile($Meter, [ref]$null, [ref]$null)
$defs = @($ast.FindAll({ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] }, $false) | ForEach-Object { $_.Extent.Text })
$vars = @($ast.EndBlock.Statements | Where-Object { $_ -is [Management.Automation.Language.AssignmentStatementAst] -and $_.Left.Extent.Text -like '$Ql*' } | ForEach-Object { $_.Extent.Text })
. ([scriptblock]::Create(($defs + $vars) -join "`n"))
# The meter loads its classes in its main code, which does not run here.
$QlClasses = Read-QlBlockerClasses (Join-Path (Split-Path -Parent $Meter) 'blocker-classes.json')
function Invoke-Case($c) {
    if ($c.kind -eq 'class') { return (Get-QlBlockerClass $c.text).cls }
    if ($c.kind -eq 'identity') { $id = Get-QlIdentity $c.text; if ($id) { return $id.head } else { return '' } }
    [string]$QlRx[$c.kind.Substring(3)].IsMatch($c.text)
}
$res = foreach ($c in @(Get-Content -LiteralPath $Cases -Raw | ConvertFrom-Json)) {
    $null = Invoke-Case ([pscustomobject]@{ kind = $c.kind; text = 'x' })
    $best = [double]::MaxValue; $r = $null
    for ($k = 0; $k -lt 3; $k++) {
        # A match timeout (or any throw) is this case's result, not the end of the run.
        $sw = [Diagnostics.Stopwatch]::StartNew()
        try { $r = Invoke-Case $c } catch { $r = "ERROR $($_.Exception.GetBaseException().GetType().Name)" }
        $sw.Stop()
        $best = [Math]::Min($best, $sw.Elapsed.TotalMilliseconds)
    }
    [pscustomobject]@{ name = $c.name; ms = $best; result = "$r" }
}
ConvertTo-Json -InputObject @($res) | Set-Content -LiteralPath $Out -Encoding utf8
"""


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

    def _run(self, *args: str, env: dict[str, str] | None = None, gh: Path | None = None, timeout: int = 300) -> subprocess.CompletedProcess:
        env = dict(os.environ if env is None else env)
        if gh is not None:
            env["PATH"] = str(gh) + os.pathsep + env.get("PATH", "")
            env["QL_FAKE_GH_DIR"] = str(gh)
        return subprocess.run(
            [PWSH, "-NoProfile", "-NonInteractive", "-File", str(METER), *args],
            capture_output=True, text=True, env=env, timeout=timeout)

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
        self.assertIn("| **fleet** | 12 | 9 (75%) | 0 | | 0 of 5 projects | unknown | unknown | 0 of 9 (9 unknown) |", fleet)

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
        self.assertIn("| **fleet** | 12 | 9 (75%) | 0 | | 5 of 5 projects | 0 of 9 (9 unknown) | 0 | 0 of 9 (9 unknown) |", fleet)

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
        start = datetime(2026, 10, 21, 10, 0, tzinfo=timezone.utc)
        for i, (name, (repo, pr, head, _)) in enumerate(sorted(SAMPLE_PASSES.items())):
            project = name.split("--")[0]
            by_project.setdefault(project, []).append((repo, head))
            commits.append((f"cos-feedback/{project}/pr-{pr}.md", _sample_text(name), _iso(start + timedelta(minutes=i)), name))
        bus = self._small_bus(commits)
        for project, passes in by_project.items():
            cache = {f"{repo}@{head}": {"fetched": "2026-10-25T00:00:00Z", "rows": []} for repo, head in passes}
            result, out = self._measure_bus(bus, project, cache=cache)
            mine = {h: c for n, (_, _, h, c) in SAMPLE_PASSES.items() if n.startswith(project + "--")}
            want = sorted(h for h, c in mine.items() if c == "blocked")
            self.assertEqual(sorted(r["head"] for r in self._corpus(out)), want, project)
            since = result["windows"][0]
            self.assertEqual((since["passes"], since["unparsed"]), (len(passes), 0), project)
            self.assertEqual((since["blocked"], since["blockerUnknown"]),
                             (len(want), sum(1 for c in mine.values() if c == "unknown")), project)
            self.assertEqual(sorted(u["head"] for u in since["blockerUnknownHeads"]),
                             sorted(h for h, c in mine.items() if c == "unknown"), project)

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

    # -- adversary pass r3 rerun on ffe9fd57

    def test_closing_hashes_and_indented_blockers_open_the_section(self) -> None:  # A1
        bodies = ["## Blockers ##\nB1 x.\n", "  ## Blockers\nB1 x.\n", "  Blockers: B1 x.\n", "   ### Blockers ###\n- B1 x.\n"]
        commits, heads = [], []
        for i, body in enumerate(bodies):
            head = f"{i + 1:040x}"
            heads.append(head)
            commits.append((f"cos-feedback/p/pr-{i + 1}.md", f"# acme/widget #{i + 1} / {head}\n\n{body}", f"2026-10-21T10:0{i}:00+00:00", f"v{i}"))
        _, got = self._blocked_heads(self._small_bus(commits), "p", heads)
        self.assertEqual(sorted(got), sorted(heads), [(b, h in got) for b, h in zip(bodies, heads)])

    def test_a_head_longer_than_40_hex_names_no_pass(self) -> None:  # A2
        bus = self._small_bus([
            ("cos-feedback/p/pr-7.md", f"# acme/widget #7 / {'a' * 41}\n\n## Blockers\nB1 x.\n", "2026-10-21T10:00:00+00:00", "41 hex"),
            ("cos-feedback/p/pr-8.md", f"# acme/widget #8 / {H2}\n\n## Blockers\nB1 x.\n\n# acme/widget #8 / {'b' * 41}\n", "2026-10-21T10:01:00+00:00",
             "a valid heading, then a 41-hex one"),
        ])
        result, got = self._blocked_heads(bus, "p", [H2, "a" * 40, "b" * 40])
        self.assertEqual(got, [H2])
        self.assertEqual((result["windows"][0]["passes"], result["windows"][0]["unparsed"]), (1, 1))

    def test_cache_trust_flags_must_be_booleans(self) -> None:  # A3
        bus = self._small_bus([("cos-feedback/p/pr-7.md", _section(7, H1, "B1 x."), "2026-10-21T10:00:00+00:00", "pr7")])
        for flags in ({"native": True, "dismissals": 1}, {"native": True, "dismissals": "true"}, {"native": 1, "dismissals": True}):
            cache = {f"acme/widget@{H1}": dict({"fetched": "2026-10-21T12:00:00Z", "prs": [7], "rows": []}, **flags)}
            result, _ = self._measure_bus(bus, "p", cache=cache, declared=self.NATIVE_KEYBOT, expect=2)
            self.assertEqual(result["windows"][0]["statusUnknown"], 1, flags)
            self.assertTrue(any("malformed" in e for e in result["errors"]), (flags, result["errors"]))

    def _corpus_runs(self, declarations: list[dict]) -> list[dict]:
        bus = self._small_bus([("cos-feedback/p/pr-7.md", _section(7, H1, "B1 x."), "2026-10-21T10:00:00+00:00", "pr7")])
        out = self._dir()
        rows = [{"context": "review/a", "firstUtc": "2026-10-21T09:00:00Z", "state": "success", "successUtc": "2026-10-21T09:00:00Z", "failureUtc": None}]
        cache = {f"acme/widget@{H1}": {"fetched": "2026-10-21T12:00:00Z", "native": True, "dismissals": True, "prs": [7], "rows": rows}}
        (out / "status-cache.json").write_text(json.dumps(cache), encoding="utf-8")
        for declared in declarations:
            proc = self._run("-Bus", str(bus), "-Project", "p", "-Since", "2026-10-01T00:00:00Z", "-OutDir", str(out), "-NoFetch",
                             "-NoStatuses", "-ReviewSources", str(self._sources({"p": declared})))
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return self._corpus(out)

    def test_the_last_corpus_row_decides_whether_a_head_is_current(self) -> None:  # A4
        statuses = {"statusContexts": ["review/*"]}
        rows = self._corpus_runs([statuses, self.NATIVE_KEYBOT, statuses])
        self.assertEqual([r["reviewApprovedBeforeCos"] for r in rows], [["review/a"], [], ["review/a"]])

    def test_declarations_that_print_alike_are_distinct_corpus_bases(self) -> None:  # A5
        rows = self._corpus_runs([{"statusContexts": ["review/a, review/b"]}, {"statusContexts": ["review/a", "review/b"]}])
        self.assertEqual([r["reviewApprovedBeforeCos"] for r in rows], [[], ["review/a"]])

    def test_a_since_without_an_offset_is_a_usage_error(self) -> None:  # A6
        for since in ("10/21/2026", "2026-10-21", "2026-10-21T00:00:00"):
            proc = self._run("-Bus", str(self.bus), "-Project", "demo", "-Since", since, "-OutDir", str(self._dir()), "-NoStatuses", "-NoFetch")
            self.assertEqual(proc.returncode, 4, (since, proc.stdout + proc.stderr))

    # -- adversary pass r3 rerun 2 on 7f618957 (fixed without a further rerun: the README caps reruns at 2)

    def test_nested_markers_and_a_note_inside_bold_are_read(self) -> None:  # A1, A2
        bodies = ["## **Blockers [2026-10-21]**\nB1 x.\n", "## __Blockers (r3):__\nB1 x.\n", "## Blockers\n> - ### **B1** x.\n",
                  "## Blockers\n1. - **B1** x.\n"]
        commits, heads = [], []
        for i, body in enumerate(bodies):
            head = f"{i + 1:040x}"
            heads.append(head)
            commits.append((f"cos-feedback/p/pr-{i + 1}.md", f"# acme/widget #{i + 1} / {head}\n\n{body}", f"2026-10-21T10:0{i}:00+00:00", f"v{i}"))
        _, got = self._blocked_heads(self._small_bus(commits), "p", heads)
        self.assertEqual(sorted(got), sorted(heads), [(b, h in got) for b, h in zip(bodies, heads)])

    def test_an_indented_identity_heading_counts_and_a_huge_pr_number_names_no_pass(self) -> None:  # A3, A6
        bus = self._small_bus([
            ("cos-feedback/p/pr-7.md", f"# acme/widget #7 / {H1}\n\n## Blockers\nB1 x.\n\n  # acme/widget #7 / {H2}\n\n## Blockers\nNone.\n",
             "2026-10-21T10:00:00+00:00", "indented last heading"),
            ("cos-feedback/p/pr-8.md", f"# acme/widget #99999999999 / {H3}\n\n## Blockers\nB1 x.\n", "2026-10-21T10:01:00+00:00", "PR number past Int32"),
            ("cos-feedback/p/pr-9.md", f"# acme/widget #9 / {H4}\n\n## Blockers\nB1 x.\n", "2026-10-21T10:02:00+00:00", "ordinary"),
        ])
        result, got = self._blocked_heads(bus, "p", [H1, H2, H3, H4])
        self.assertEqual(sorted(got), [H4])  # H2 (the last heading) is clean; H1 is not this file's pass
        since = result["windows"][0]
        self.assertEqual((since["measured"], since["passes"], since["unparsed"]), (True, 2, 1))

    def test_a_failed_repository_discovery_leaves_adoption_unknown(self) -> None:  # A5
        bus = self._small_bus([
            ("cos-feedback/p/pr-7.md", _section(7, H1, "None.", "acme/other"), "2026-10-01T00:00:00+00:00", "old, names acme/other"),
            ("cos-feedback/p/pr-8.md", _section(8, H2, "None."), _iso(NOW - timedelta(hours=1)), "recent, names acme/widget"),
        ])
        # git grep exits 1 (the 'no match' code) on a blob it cannot read: the meter must still see the failure.
        blob = _git(bus, "rev-parse", "HEAD:cos-feedback/p/pr-7.md")
        loose = bus / ".git" / "objects" / blob[:2] / blob[2:]
        os.chmod(loose, stat.S_IWRITE | stat.S_IREAD)
        loose.unlink()
        gh = self._fake_gh(prs={"acme/widget": [{"number": 5, "body": "Adversary pass: x", "createdAt": _iso(NOW - timedelta(hours=1))}]})
        result, _ = self._measure_bus(bus, "p", since=_iso(NOW - timedelta(hours=2)), gh=gh, expect=2)
        self.assertEqual(result["shiftLeft"], {"prs": None, "withAdversaryLine": None})

    # -- r4 on 49720f53: blockers read by meaning (B5) and linear parsing of untrusted bus text (B6)

    def _classes(self, bodies: list[tuple[str, str]]) -> tuple[dict, dict[str, str]]:
        """Measures one pass per body (the text after the identity heading); returns the since window and head -> class."""
        commits, heads = [], []
        for i, (body, _) in enumerate(bodies):
            head = f"{i + 1:040x}"
            heads.append(head)
            commits.append((f"cos-feedback/p/pr-{i + 1}.md", f"# acme/widget #{i + 1} / {head}\n\nVerdict.\n\n{body}",
                            _iso(datetime(2026, 10, 21, 10, 0, tzinfo=timezone.utc) + timedelta(minutes=i)), f"b{i}"))
        result, got = self._blocked_heads(self._small_bus(commits), "p", heads)
        since = result["windows"][0]
        unknown = {u["head"] for u in since["blockerUnknownHeads"]}
        return since, {h: ("blocked" if h in got else "unknown" if h in unknown else "clean") for h in heads}

    def test_blockers_are_read_conservatively_blocked_clean_or_unknown(self) -> None:  # B5 inputs, B7 rules
        # B7 (r5): r4 read these by meaning; every prose or mixed section is now unknown by design. A row whose class
        # changed from r4 says so ("r4: x"); none moved between blocked and clean.
        bodies = [
            # Blocked: a line that starts B<n> (or SHARED BLOCKER) and does not say it was fixed, under no None/No opener.
            ("## Blockers\nPrior612 B1 persists: a.py:71 pins 203.\n", "unknown"),  # r4: blocked
            ("## Blockers\nPrior829ec450 B1 remains: a.py:359 replaces runtime.\n", "unknown"),  # r4: blocked
            ("## Blockers\nInherited #267 B1 remains source-unfixed at a.py:361.\n", "unknown"),  # r4: blocked
            ("## Blockers\nInherited ownership B1 remains at a.py:363.\n", "unknown"),  # r4: blocked
            ("## Blockers\nNo new demonstrated blocker. Prior B1 remains: a.ps1:3 still overwrites.\n", "unknown"),  # r4: blocked
            ("## Blockers\nPrior timing B1 is source-fixed.\n\nB2 validation mismatch remains.\n", "blocked"),
            ("## Blockers\nNone newly demonstrated. The current B2 still breaks the guard.\n", "unknown"),  # r4: blocked
            ("## Blockers\nB1 (medium confidence) is retained at a.ps1:9.\n", "blocked"),
            ("## Blockers\n- CI: a.md:3 is new, but exact-head canonical job119584694731 fails gate.x.\n", "unknown"),  # r4: blocked
            ("## Blockers\n- a.py:193: actual [Windows shard 2](https://github.com/o/r/actions/runs/1/job/2) fails at 07:42:20Z.\n", "unknown"),  # r4: blocked
            ("## Blockers\nCurrent-head Ubuntu shard1 independently fails at :954.\n", "unknown"),  # r4: blocked
            ("## Blockers\nB1 partially resolved. a.ps1:9 still races.\n", "unknown"),  # r4: blocked; the line says resolved
            ("## Blockers\n- a.py:181: actual [shard 1](https://github.com/o/r/actions/runs/1/job/2) and [shard 3](x) fail: 227 tests.\n", "unknown"),  # r4: blocked
            ("## Blockers\nNo new demonstrated defect. Existing CI is not green: Bridge401/job120921170318 failed16:17:43.\n", "unknown"),  # r4: blocked
            ("## Blockers\n- a.py:193: actual Windows shard 2 fails: received \"(kill not confirmed)\".\n", "unknown"),  # r4: blocked
            ("## Blockers\nNone newly demonstrated. Prior B1/B2 are source-addressed.\n\nB2: b.ps1:9 still drops the lock.\n", "unknown"),  # r4: blocked; mixed
            ("## Blockers\nSHARED BLOCKER (Dot, CoS-confirmed): the lock is checked before it is taken.\n", "blocked"),
            ("## Blockers\n- **B3 --** a.ps1:9 drops the lock.\n", "blocked"),
            ("## Blockers\nB2 - a.ps1:9 drops the lock.\n", "blocked"),
            ("## Blockers\nG1 OPEN: an ask is unanswered.\nB4: a.ps1:9 drops the lock.\n", "blocked"),
            # Clean: the whole section is one entry of the exact whitelist (blocker-classes.json).
            ("## Blockers\nNone.\n", "clean"),
            ("## Blockers\n(none)\n", "clean"),
            ("## Blockers\nNone demonstrated. No SHARED BLOCKER.\n", "clean"),
            ("## Blockers\n**None demonstrated.**\n", "clean"),
            ("## Blockers\nNo new demonstrated blocker. Prior B1 is source-fixed.\n", "unknown"),  # r4: clean
            ("## Blockers\nPrior metadata-I/O B1 is source-addressed.\n", "unknown"),  # r4: clean
            ("## Blockers\nPrior B1 remains closed.\n", "unknown"),  # r4: clean
            ("## Blockers\nNo remaining B2 demonstrated in this delta.\n", "unknown"),  # r4: clean
            ("## Blockers\nNone newly demonstrated. Prior B3/B4 source remedies remain; B1/B2/I1 dispositions preserved.\n", "unknown"),  # r4: clean
            ("## Blockers\nNo current failing check demonstrated. Prior B1 was an actual failure of job12 at 07:42.\n", "unknown"),  # r4: clean
            ("## Blockers\nNone demonstrated. Thirteen checks succeed, none has failed.\n", "unknown"),  # r4: clean
            ("## Blockers\nNone demonstrated. Prior CI failed on r4; the r5 job passes.\n", "unknown"),  # r4: clean
            ("## Blockers\nNone demonstrated. The gate check fails closed when the job is missing.\n", "unknown"),  # r4: clean
            ("## Blockers\nB1 closed: a.ps1:3 now checks.\n", "blocked"),  # r4: clean; 'closed' is not one of the five fixed words
            ("## Blockers\nB1 fixed: a.ps1:3 now checks.\n", "unknown"),
            ("## Blockers\nNone newly demonstrated. Prior B1/B2 are source-addressed.\nB1: a.ps1:3 now checks the lock.\nB2: b.ps1:9 adds a regression; old failures remain history.\n", "unknown"),  # r4: clean; mixed
            ("Blockers: None established; no SHARED BLOCKER.\n", "clean"),
            ("## Blockers\nNone.\nNone.\n", "unknown"),  # two sentences, though both are on the whitelist
            ("## Blockers\nnone.\n", "unknown"),  # the whitelist is exact, case included
            # Unknown: neither explicit no-blocker text nor a current blocker, a missing or empty section included.
            ("## Blockers\nP2, high confidence: a.ps1:3 races.\n", "unknown"),
            ("## Blockers\nG1 OPEN, response duty: answer the ask.\n", "unknown"),
            ("## Blockers\nReadiness hold: the A/B receipt is missing.\n", "unknown"),
            ("## Blockers\nHistorical finding on reviewed head, addressed in later merged source:\n", "unknown"),
            ("## Blockers\nPrior B1 is not fixed.\n", "unknown"),
            ("## Blockers\nBlocked by nothing; B1 closed.\n", "unknown"),
            ("## Blockers\n\n## Notes\nB1 x.\n", "unknown"),
            ("## Notes\nNothing to say.\n", "unknown"),
        ]
        since, got = self._classes(bodies)
        want = {f"{i + 1:040x}": c for i, (_, c) in enumerate(bodies)}
        self.assertEqual(got, want, [(b, got[h], c) for (b, c), h in zip(bodies, want) if got[h] != c])
        n_blocked = sum(1 for _, c in bodies if c == "blocked")
        n_unknown = sum(1 for _, c in bodies if c == "unknown")
        self.assertEqual((since["passes"], since["blocked"], since["blockerUnknown"]), (len(bodies), n_blocked, n_unknown))
        # The rate is over the passes the meter could classify: an unknown pass is never counted as clean.
        self.assertEqual(since["blockedRate"], round(n_blocked / (len(bodies) - n_unknown), 3))

    def test_adversary_r4_cases_under_the_conservative_rules(self) -> None:  # adversary r4 on e5d54282 and 8694a086
        # The r4 adversary inputs, kept as regressions. Every one of them that is prose or mixed is unknown since r5 (B7);
        # "r4: x" marks a changed class, and none moved between blocked and clean.
        bodies = [
            ("## Blockers\nNone newly demonstrated.\nB1: tools/lease.ps1:9 deletes another owner's lock.\n", "unknown"),  # r4: blocked; mixed
            ("## Blockers\nNone newly demonstrated. Prior B1 is source-addressed.\nB2: a.ps1:9 drops the lock.\n", "unknown"),  # r4: blocked; mixed
            ("## Blockers\nNone newly demonstrated.\nB1: a.ps1:3 is now fixed by the claim check.\n", "unknown"),  # r4: clean
            ("## Blockers\nNone.\nThere is no current B1.\n", "unknown"),  # r4: clean
            ("## Blockers\nNone.\nIt is not true that B1 remains open.\n", "unknown"),  # r4: clean
            ("## Blockers\nNone.\nThe exact-head job1 fails the gate that is intended to fail closed.\n", "unknown"),  # r4: blocked
            ("## Blockers\nNo assessment has been made.\n", "unknown"),
            ("## Blockers\nNothing was reviewed.\n", "unknown"),
            ("## Blockers\nNo new demonstrated defect in this delta.\n", "unknown"),  # r4: clean; not on the whitelist
            ("## Blockers\nNo current failing check demonstrated.\n", "unknown"),  # r4: clean
            ("## Blockers\nNo assessment of the reported blocker has been made.\n", "unknown"),
            ("## Blockers\nNone of the blockers have been reviewed.\n", "unknown"),
            ("## Blockers\nNo new independently established code blocker or SHARED BLOCKER.\n", "unknown"),  # r4: clean
            ("## Blockers\nNo newly demonstrated behavior/security/CI blocker.\n", "unknown"),  # r4: clean
            ("## Blockers\nNone newly demonstrated.\nB1: tools/fixed.ps1:9 deletes another owner's lock.\n", "unknown"),  # r4: blocked; mixed
            ("## Blockers\nPrior B1: tools/fixed.ps1:9 deletes another owner's lock.\n", "unknown"),
            ("## Blockers\nB1: tools/lease.ps1:9 deletes another owner's lock.\n", "blocked"),
            ("## Blockers\nB1: B2 is fixed, but the lock deletion race occurs on every run.\n", "unknown"),  # the line says fixed
            ("## Blockers\nNone newly demonstrated. Prior B1/B2/I1 are source-addressed.\nB2: b.ps1:9 adds a regression.\n", "unknown"),  # r4: clean
            ("## Blockers\nNone newly demonstrated.\nNo new defects were found, but B1 remains open.\n", "unknown"),  # r4: blocked
            ("## Blockers\nNone newly demonstrated.\nCI job1 did not fail, but job2 fails on the exact head.\n", "unknown"),  # r4: blocked
            ("## Blockers\nNone newly demonstrated.\nPrior **B1** persists at a.ps1:9.\n", "unknown"),  # r4: blocked
            ("## Blockers\n**B1** closed: the lock is checked.\n", "blocked"),  # r4: clean; a declared limit ('closed' is not a fixed word)
            ("## Blockers\n__B1__ fixed: the lock is checked.\n", "unknown"),  # r4: clean
            ("## Blockers\n- **B1 / P1:** a.ps1:3 races.\n", "blocked"),
        ]
        _, got = self._classes(bodies)
        want = {f"{i + 1:040x}": c for i, (_, c) in enumerate(bodies)}
        self.assertEqual(got, want, [(b, got[h], c) for (b, c), h in zip(bodies, want) if got[h] != c])

    def test_dot_b7_counterexamples_are_unknown(self) -> None:  # B7 (Dot on a82b77cd; factory packet 6410642859)
        bodies = [
            ("## Blockers\nNo blockers have been reviewed.\n", "unknown"),
            ("## Blockers\nNone.\nCI job1 did not fail, and job2 fails on the exact head\n", "unknown"),
            ("## Blockers\nNone.\nB1: writer is now fixed, but the race still reproduces\n", "unknown"),
            # May be unknown or clean, never blocked: no line starts B<n>.
            ("## Blockers\nThere isn't a current B1\n", "unknown"),
            ("## Blockers\nNone.\nThere isn't a current B1\n", "unknown"),
            # r4 adversary rerun 2 A4: a contraction before a CI failure.
            ("## Blockers\nNone.\nCI job1 hasn't failed.\n", "unknown"),
        ]
        _, got = self._classes(bodies)
        want = {f"{i + 1:040x}": c for i, (_, c) in enumerate(bodies)}
        self.assertEqual(got, want, [(b, got[h], c) for (b, c), h in zip(bodies, want) if got[h] != c])

    def test_every_whitelisted_section_is_clean_and_nothing_else_is(self) -> None:  # B7
        data = json.loads((HERE / "blocker-classes.json").read_text(encoding="utf-8"))
        self.assertEqual(data["schema"], "fleet/quality-lift/blocker-classes/v1")
        self.assertEqual(data["closedWords"], ["source-addressed", "remedied", "resolved", "verified", "fixed"])
        clean = [e["text"] for e in data["clean"]]
        for required in ("None.", "None", "None demonstrated.", "No new demonstrated defect.", "No new demonstrated blocker.",
                         "No demonstrated new defect."):
            self.assertIn(required, clean)
        self.assertEqual(len(clean), len(set(clean)))
        bodies = [(f"## Blockers\n{t}\n", "clean") for t in clean]
        # An extra sentence, a second line, or a near miss is not on the whitelist.
        bodies += [(f"## Blockers\n{t} Merge once CI is green.\n", "unknown") for t in clean[:3]]
        bodies += [(f"## Blockers\n{t}\nG1 OPEN.\n", "unknown") for t in clean[:3]]
        bodies += [(f"## Blockers\n{e['text']}\n", "unknown") for e in data["excluded"]]
        _, got = self._classes(bodies)
        want = {f"{i + 1:040x}": c for i, (_, c) in enumerate(bodies)}
        self.assertEqual(got, want, [(b, got[h], c) for (b, c), h in zip(bodies, want) if got[h] != c])

    def test_a_missing_or_malformed_blocker_classes_file_is_a_usage_error(self) -> None:  # B7
        for content in (None, "{", json.dumps({"schema": "fleet/quality-lift/blocker-classes/v1", "clean": [], "closedWords": ["fixed"]}),
                        json.dumps({"schema": "fleet/quality-lift/blocker-classes/v1", "clean": [{"text": "None."}], "closedWords": [""]}),
                        json.dumps({"schema": "x", "clean": [{"text": "None."}], "closedWords": ["fixed"]})):
            where = self._dir()
            shutil.copy(METER, where / METER.name)
            shutil.copy(HERE / "review-sources.json", where / "review-sources.json")
            if content is not None:
                (where / "blocker-classes.json").write_text(content, encoding="utf-8")
            proc = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(where / METER.name), "-Bus", str(self.bus),
                                   "-Project", "demo", "-Since", "2026-10-01T00:00:00Z", "-OutDir", str(self._dir()), "-NoStatuses", "-NoFetch"],
                                  capture_output=True, text=True, timeout=120)
            self.assertEqual(proc.returncode, 4, (content, proc.stdout + proc.stderr))
            self.assertIn("blocker-classes", proc.stdout)

    def test_the_first_round_fraction_is_over_classified_prs(self) -> None:  # adversary r4 rerun A5
        bus = self._small_bus([
            ("cos-feedback/p/pr-1.md", _section(1, H1, "B1 x."), "2026-10-21T10:00:00+00:00", "blocked"),
            ("cos-feedback/p/pr-2.md", _section(2, H2, "G1 OPEN."), "2026-10-21T10:01:00+00:00", "unknown"),
        ])
        result, out = self._measure_bus(bus, "p", cache={f"acme/widget@{H1}": {"fetched": "2026-10-25T00:00:00Z", "rows": []}})
        since = result["windows"][0]
        self.assertEqual((since["prs"], since["firstRoundBlocked"], since["firstRoundUnknown"], since["firstRoundBlockedRate"]), (2, 1, 1, 1.0))
        self.assertIn("| since | 2 | 1 (100%) | 1 | 1/1 (1 unclassified) |", (out / "quality-lift.md").read_text(encoding="utf-8-sig"))
        fleet_out = self._dir()
        proc = self._run("-Bus", str(bus), "-Since", "2026-10-01T00:00:00Z", "-OutDir", str(fleet_out), "-NoStatuses", "-NoFetch",
                         "-ReviewSources", str(self._sources({"p": REVIEW_STATUS})))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("| p | 2 | 1 (100%) | 1 | 1/1 (1 unclassified) |", (fleet_out / "fleet.md").read_text(encoding="utf-8-sig"))

    def test_an_unknown_pass_folds_apart_and_never_as_clean(self) -> None:  # B5
        p, q = "cos-feedback/p/pr-7.md", "cos-feedback/p/pr-8.md"
        bus = self._small_bus([
            (p, _section(7, H1, "None demonstrated."), "2026-10-21T10:00:00+00:00", "h1 clean"),
            (p, _section(7, H1, "Readiness hold: a receipt is missing."), "2026-10-21T11:00:00+00:00", "h1 unknown later"),
            (q, _section(8, H2, "G1 OPEN: an ask is unanswered."), "2026-10-21T10:00:00+00:00", "h2 unknown"),
            (q, _section(8, H2, "B1 persists: a.ps1:3."), "2026-10-21T12:00:00+00:00", "h2 blocked later"),
            (q, _section(8, H3, "None."), "2026-10-21T13:00:00+00:00", "h3 clean"),
        ])
        cache = {f"acme/widget@{H2}": {"fetched": "2026-10-25T00:00:00Z", "rows": []}}
        result, out = self._measure_bus(bus, "p", cache=cache)
        since = result["windows"][0]
        # H1: clean then unknown -> unknown (the later pass may carry a blocker); H2: unknown then blocked -> blocked.
        self.assertEqual((since["passes"], since["blocked"], since["blockerUnknown"]), (3, 1, 1))
        self.assertEqual([u["head"] for u in since["blockerUnknownHeads"]], [H1])
        self.assertEqual(since["blockedRate"], 0.5)
        # PR 7's first head is unknown, PR 8's first head (H2) is blocked.
        self.assertEqual((since["prs"], since["firstRoundBlocked"], since["firstRoundUnknown"], since["firstRoundBlockedRate"]), (2, 1, 1, 1.0))
        self.assertEqual([r["head"] for r in self._corpus(out)], [H2])
        self.assertTrue(self._corpus(out)[0]["blocker"].startswith("B1 persists"), self._corpus(out))
        md = (out / "quality-lift.md").read_text(encoding="utf-8-sig")
        self.assertIn("| since | 3 | 1 (50%) | 1 |", md)
        fleet_out = self._dir()
        proc = self._run("-Bus", str(bus), "-Since", "2026-10-01T00:00:00Z", "-OutDir", str(fleet_out), "-NoStatuses", "-NoFetch",
                         "-ReviewSources", str(self._sources({"p": REVIEW_STATUS})))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        fleet = (fleet_out / "fleet.md").read_text(encoding="utf-8-sig")
        self.assertIn("| p | 3 | 1 (50%) | 1 |", fleet)
        self.assertIn("| **fleet** | 3 | 1 (50%) | 1 |", fleet)

    def test_long_marker_lines_finish_and_are_classified(self) -> None:  # B6, end to end
        head = lambda i: f"{i:040x}"  # noqa: E731
        texts = [
            f"# acme/widget #1 / {head(1)}\n\n## Blockers\n{'*' * 1000}\n",
            f"# acme/widget #2 / {head(2)}\n\n## Blockers\n{'_' * 1000}\n",
            f"# acme/widget #3 / {head(3)}\n\n## Blockers\n{'*_' * 500}\n",
            f"# acme/widget #4 / {head(4)}\n\n## Blockers\n{'* ' * 500}\n",
            f"# acme/widget #5 / {head(5)}\n\n## Blockers\n{'*' * 1000}B1 x.\n",
            f"# acme/widget #6 / {head(6)}\n\n## Blockers\n{'_' * 1000}None.\n",
            f"# acme/widget #7 / {head(7)}\n\n## Blockers{' ' * 3000}x\nB1 x.\n",
            f"schema: cos-feedback.v1\nrepo: acme/widget\npr: 8\nhead: {head(8)}\n{'x' * 100000}",
            f"# acme/widget #9 / {head(9)}\n\n## Blockers\n- --- {'-' * 1000} x\n\n{'-' * 1000}\nNone.\n",
        ]
        commits = [(f"cos-feedback/p/pr-{i + 1}.md", t, f"2026-10-21T10:0{i}:00+00:00", f"long {i}") for i, t in enumerate(texts)]
        bus = self._small_bus(commits)
        out = self._dir()
        cache = {f"acme/widget@{head(i)}": {"fetched": "2026-10-25T00:00:00Z", "rows": []} for i in range(1, 10)}
        (out / "status-cache.json").write_text(json.dumps(cache), encoding="utf-8")
        proc = self._run("-Bus", str(bus), "-Project", "p", "-Since", "2026-10-01T00:00:00Z", "-OutDir", str(out), "-NoFetch",
                         "-NoStatuses", "-ReviewSources", str(self._sources({"p": REVIEW_STATUS})), timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        since = json.loads((out / "quality-lift.json").read_text(encoding="utf-8-sig"))["windows"][0]
        self.assertEqual([r["head"] for r in self._corpus(out)], [head(5)])
        # 1-4: a thematic break or only marks, so an empty section; 7: not a Blockers heading; 8: no section; 9: a bullet whose
        # text is dashes then x opens the section (not no-blocker text), and the later dash line is a thematic break.
        self.assertEqual(sorted(u["head"] for u in since["blockerUnknownHeads"]), [head(i) for i in (1, 2, 3, 4, 7, 8, 9)])
        self.assertEqual((since["passes"], since["blocked"], since["blockerUnknown"]), (9, 1, 7))

    def test_bus_text_parsers_are_linear_on_long_untrusted_input(self) -> None:  # B6 and its class sweep
        sec = lambda body: f"# acme/widget #1 / {H1}\n\n## Blockers\n{body}\n"  # noqa: E731
        fm = "schema: cos-feedback.v1\nrepo: acme/widget\npr: 8\n"
        cases = [
            ("1000 stars", "class", sec("*" * 1000), "unknown"),
            ("1000 underscores", "class", sec("_" * 1000), "unknown"),
            ("mixed marks", "class", sec("*_" * 500), "unknown"),
            ("spaced thematic break", "class", sec("* " * 500), "unknown"),
            ("mixed marks before B1", "class", sec("*_" * 500 + "B1 x."), "blocked"),
            ("1000 bullets before B1", "class", sec("- " * 500 + "B1 x."), "blocked"),
            ("long prose", "class", sec("None. " + "a check fails closed " * 5000), "unknown"),  # r4: clean
            ("long B note", "class", sec("None. x B1 (" + "a" * 100000), "unknown"),  # r4: clean
            ("Blockers heading then spaces", "class", f"# a/b #1 / {H1}\n\n## Blockers{' ' * 5000}x\n", "unknown"),
            ("Blockers heading spaces and colons", "class", f"# a/b #1 / {H1}\n\n## Blockers{' :' * 5000}x\n", "unknown"),
            ("Blockers label then spaces", "class", f"# a/b #1 / {H1}\n\nBlockers{' ' * 5000}x\n", "unknown"),
            ("front matter long last line", "identity", fm + "x" * 200000, ""),
            ("front matter blank-looking line", "identity", fm + " " * 200000, ""),
            ("identity heading long name", "identity", "# " + "a" * 200000 + "\n", ""),
            ("identity heading long repo", "identity", "# " + "a" * 100000 + "/" + "b" * 100000 + " #1 / x\n", ""),
            ("adversary line long marks", "rx:adversary", "!" * 200000, "False"),
            ("repo line long spaces", "rx:repoLine", "repo:" + " " * 100000 + "a/b" + " " * 100000 + "x", "False"),
            # adversary r4 on e5d54282: A5 (a scan that copied the rest of the heading per note), A6 (\W* ran across lines).
            ("Blockers heading with 64000 notes", "class", f"# a/b #1 / {H1}\n\n## Blockers{'()' * 64000}\nNone.\n", "clean"),
            ("adversary line after 64000 newlines", "rx:adversary", "\n" * 64000 + "x", "False"),
            ("adversary line after a bullet", "rx:adversary", "body\n- **Adversary pass:** x", "True"),
            # A7 (minor): a PR number that fits an Int32 names the pass, one past it names none.
            ("PR number 1000000000", "identity", f"# a/b #1000000000 / {H1}\n", H1),
            ("PR number 2147483647", "identity", f"# a/b #2147483647 / {H1}\n", H1),
            ("PR number 2147483648", "identity", f"# a/b #2147483648 / {H1}\n", ""),
            ("front matter PR 2147483648", "identity", f"schema: cos-feedback.v1\nrepo: a/b\npr: 2147483648\nhead: {H1}\n\n", ""),
            # The r4 rerun fixes' inputs (r4: clean, each now unknown: more than a whitelisted section) stay linear.
            ("underscore run inside a word", "class", sec("None. a" + "_" * 100000 + "b"), "unknown"),
            ("star run inside a line", "class", sec("None. a" + "*" * 100000 + "b"), "unknown"),
            ("spaces before no contrast word", "class", sec("None. x" + " " * 100000 + "y"), "unknown"),
            ("many failures after a hedge", "class", sec("None. CI would " + "fails " * 20000), "unknown"),
            ("many current B1s after a negation", "class", sec("None. It is not true that " + "B1 remains " * 20000), "unknown"),
            # B7 (r5): the B<n> lead, the fixed-word scan and the whitelist on long untrusted lines.
            ("long B line", "class", sec("B1 " + "fixe " * 40000), "blocked"),
            ("long B line ending fixed", "class", sec("B1 " + "x" * 200000 + " fixed"), "unknown"),
            ("SHARED then spaces", "class", sec("SHARED" + " " * 100000 + "x"), "unknown"),
            ("None then spaces", "class", sec("None" + " " * 100000 + "."), "unknown"),
            ("trailing marks after None.", "class", sec("None." + "*_" * 50000), "clean"),
            ("many B lines", "class", sec("\n".join(["B1 fixed x."] * 1000)), "unknown"),
            # adversary r4 rerun A6 (minor): a non-ASCII digit names no pass (it used to throw and unmeasure the project).
            ("PR number in Arabic-Indic digits", "identity", f"# a/b #١ / {H1}\n", ""),
            ("front matter PR in Arabic-Indic digits", "identity", f"schema: cos-feedback.v1\nrepo: a/b\npr: ١\nhead: {H1}\n\n", ""),
        ]
        work = self._dir()
        helper = work / "time-parsers.ps1"
        helper.write_text(TIMING_HELPER, encoding="utf-8")
        (work / "cases.json").write_text(json.dumps([{"name": n, "kind": k, "text": t} for n, k, t, _ in cases]), encoding="utf-8")
        proc = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(helper), "-Meter", str(METER),
                               "-Cases", str(work / "cases.json"), "-Out", str(work / "out.json")],
                              capture_output=True, text=True, timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        got = {r["name"]: r for r in json.loads((work / "out.json").read_text(encoding="utf-8-sig"))}
        for name, _, _, want in cases:
            with self.subTest(name):
                self.assertEqual(got[name]["result"], want)
                self.assertLess(got[name]["ms"], 100, got[name])

    def test_the_meter_is_ascii_and_names_no_board_path(self) -> None:
        raw = METER.read_bytes()
        self.assertTrue(all(b < 128 for b in raw), "Measure-FleetQualityLift.ps1 must be ASCII-only")
        text = raw.decode("ascii").lower()
        for term in ("mlv", "layi wkspc", ".claude-state", "fleet-runs"):
            self.assertNotIn(term, text)


if __name__ == "__main__":
    unittest.main()
