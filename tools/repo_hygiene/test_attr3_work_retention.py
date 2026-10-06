"""LANE-BUILD-WORKDIR-RETENTION-1: the assembler's per-head scratch trees are bounded.

Every head iteration of a build leg left <build dir>\\.work-<sha12> (about 1.2 GiB) behind, including
.job-tmp\\<sha12>-source.zip, a 0.29 GiB duplicate of the tree expanded from it. This pins both halves of the fix
against synthetic fixtures (never real footage, never a compile):

  1. the source archive is deleted once the build has consumed it, on success and on failure, by the ownership
     journal's proof (the real assembler script runs on a tiny repository and stops at the missing DLL pair);
  2. keep-newest retention: the newest .work-<sha12> stays, every superseded one has its evidence copied and
     VERIFIED before it is dropped, a copy that does not verify keeps the tree, a tree the journal cannot prove is
     left standing, and -DryRun changes nothing.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import unittest
from pathlib import Path

from tools.repo_hygiene.test_playback_attr_3_cuda_behaviour import (
    MODULE,
    _PwshCase,
    requires_git,
    requires_pwsh,
)

BACHELOR = Path(MODULE).parent
ASSEMBLER = BACHELOR / "playback-attr-3-cuda-assemble.ps1"
RETENTION = BACHELOR / "AttrCudaWorkRetention.psm1"

SRC_BYTES = b"o" * (3 * 1024 * 1024)
LOG_BYTES = b"make output\n" * 40
EXE_BYTES = b"MZ synthetic exe " * 64
PROBE_BYTES = b"launch probe ok\n"


def _sha(index: int) -> str:
    return f"{index:012x}"


class _RetentionCase(_PwshCase):
    def pwsh(self, body: str) -> subprocess.CompletedProcess:
        proc = self.run_with_module(f"Import-Module '{RETENTION}' -Force\n" + body)
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        return proc

    def make_build(self, shas: list[str]) -> tuple[Path, Path]:
        """A build dir holding one fresh, journalled .work-<sha> per sha, OLDEST first."""
        base = self.tmp / "build-head"
        base.mkdir()
        journal = base / ".attrcuda-owned.jsonl"
        roots = "".join(
            f"[void](New-AttrCudaOwnedRoot -TrustedRoot '{base}' -Path '{base / ('.work-' + sha)}' "
            f"-OwnedJournal '{journal}' -WarningAction SilentlyContinue)\n" for sha in shas)
        self.pwsh(roots)
        now = time.time()
        for position, sha in enumerate(shas):
            tree = base / f".work-{sha}"
            self.populate(tree, sha)
            stamp = now - 3 * 3600 * (len(shas) - position)
            os.utime(tree, (stamp, stamp))
        return base, journal

    def populate(self, tree: Path, sha: str) -> None:
        (tree / "src" / "platform" / "qt").mkdir(parents=True)
        (tree / "src" / "platform" / "qt" / "tool-made.obj").write_bytes(SRC_BYTES)
        (tree / "src" / "platform" / "qt" / "vendored.dll").write_bytes(b"source-tree dll: not evidence")
        (tree / "logs").mkdir()
        (tree / "logs" / "make.log").write_bytes(LOG_BYTES)
        (tree / "logs" / "qmake.log").write_bytes(LOG_BYTES + sha.encode())
        (tree / ".job-tmp" / "launch-probe").mkdir(parents=True)
        (tree / ".job-tmp" / "launch-probe" / "probe.txt").write_bytes(PROBE_BYTES)
        (tree / ".job-tmp" / f"MLVApp-playback-attr-3-cuda-{sha}.exe").write_bytes(EXE_BYTES)
        (tree / ".job-tmp" / f"{sha}-source.zip").write_bytes(SRC_BYTES)

    def retain(self, base: Path, journal: Path, extra: str = "") -> list[dict]:
        proc = self.pwsh(
            f"$r = Invoke-AttrCudaWorkRetention -BuildDir '{base}' -OwnedJournal '{journal}' {extra}\n"
            "Write-Output ('JSON=' + (ConvertTo-Json -InputObject @($r) -Depth 4 -Compress))\n")
        line = next(l for l in proc.stdout.splitlines() if l.startswith("JSON="))
        parsed = json.loads(line[len("JSON="):])
        return parsed if isinstance(parsed, list) else [parsed]


@requires_pwsh
class KeepNewestRetentionTests(_RetentionCase):
    def test_the_newest_tree_stays_and_each_superseded_one_leaves_verified_evidence(self) -> None:
        shas = [_sha(1), _sha(2), _sha(3)]
        base, journal = self.make_build(shas)
        results = self.retain(base, journal)
        self.assertEqual(sorted(r["Sha"] for r in results), [_sha(1), _sha(2)])
        self.assertEqual({r["Action"] for r in results}, {"dropped"}, results)
        self.assertTrue((base / f".work-{_sha(3)}" / "src" / "platform" / "qt" / "tool-made.obj").is_file(),
                        "the newest tree must stay whole")
        for old in (_sha(1), _sha(2)):
            self.assertFalse((base / f".work-{old}").exists(), "a superseded tree was not dropped")
            evidence = base / f"evidence-{old}"
            self.assertEqual((evidence / "logs" / "make.log").read_bytes(), LOG_BYTES)
            self.assertEqual((evidence / "logs" / "qmake.log").read_bytes(), LOG_BYTES + old.encode())
            self.assertEqual((evidence / ".job-tmp" / "launch-probe" / "probe.txt").read_bytes(), PROBE_BYTES)
            self.assertEqual(
                (evidence / ".job-tmp" / f"MLVApp-playback-attr-3-cuda-{old}.exe").read_bytes(), EXE_BYTES)
            self.assertFalse((evidence / "src").exists(), "the reproducible source tree is not evidence")
            self.assertFalse((evidence / ".job-tmp" / f"{old}-source.zip").exists(), "a zip is not evidence")
        for result in results:
            self.assertEqual(result["EvidenceFiles"], 4)
            self.assertEqual(result["EvidenceBytes"], len(LOG_BYTES) * 2 + 12 + len(PROBE_BYTES) + len(EXE_BYTES))
            self.assertGreaterEqual(result["FreedBytes"], len(SRC_BYTES) * 2, "the drop must free the tree")
        self.assertTrue(journal.is_file(), "the ownership journal is not the retention's to delete")

    def test_a_tree_written_inside_the_min_age_window_is_never_dropped(self) -> None:
        base, journal = self.make_build([_sha(1), _sha(2), _sha(3)])
        # two legs are building at once: the newest is built now, the second newest 30 minutes ago
        os.utime(base / f".work-{_sha(3)}", None)
        recent = time.time() - 30 * 60
        os.utime(base / f".work-{_sha(2)}", (recent, recent))
        self.assertEqual([r["Sha"] for r in self.retain(base, journal)], [_sha(1)])
        self.assertTrue((base / f".work-{_sha(2)}" / "logs" / "make.log").is_file(), "a tree a sibling leg may be using was dropped")
        self.assertEqual([r["Sha"] for r in self.retain(base, journal, "-MinAgeMinutes 0")], [_sha(2)])

    def test_each_head_has_its_own_build_dir_so_newest_is_kept_across_the_run(self) -> None:
        run = self.tmp / "lane-CARD-r1-20261006T0000Z"
        run.mkdir()
        now = time.time()
        builds = []
        for position, (name, sha) in enumerate((("build-ctl", _sha(1)), ("build-head", _sha(2)), ("build-head2", _sha(3)))):
            build = run / name
            build.mkdir()
            journal = build / ".attrcuda-owned.jsonl"
            self.pwsh(f"[void](New-AttrCudaOwnedRoot -TrustedRoot '{build}' -Path '{build / ('.work-' + sha)}' "
                      f"-OwnedJournal '{journal}' -WarningAction SilentlyContinue)\n")
            self.populate(build / f".work-{sha}", sha)
            stamp = now - 3 * 3600 * (3 - position)
            os.utime(build / f".work-{sha}", (stamp, stamp))
            builds.append((build, journal))
        self.assertEqual(self.retain(builds[2][0], builds[2][1]), [], "a build dir on its own holds one tree: nothing to prune")
        results = self.retain(builds[2][0], builds[2][1], "-IncludeSiblingBuildDirs")
        self.assertEqual({(r["Build"], r["Sha"], r["Action"]) for r in results},
                         {("build-ctl", _sha(1), "dropped"), ("build-head", _sha(2), "dropped")})
        self.assertTrue((builds[2][0] / f".work-{_sha(3)}" / "src").is_dir(), "the newest tree of the run stays")
        for build, sha in ((builds[0][0], _sha(1)), (builds[1][0], _sha(2))):
            self.assertFalse((build / f".work-{sha}").exists())
            self.assertEqual((build / f"evidence-{sha}" / "logs" / "make.log").read_bytes(), LOG_BYTES)
            self.assertTrue((build / ".attrcuda-owned.jsonl").is_file())

    def test_the_head_named_by_keep_sha_is_protected_even_when_it_is_not_the_newest(self) -> None:
        base, journal = self.make_build([_sha(1), _sha(2), _sha(3)])
        results = self.retain(base, journal, f"-KeepSha '{_sha(1)}'")
        self.assertEqual([r["Sha"] for r in results], [_sha(2)])
        self.assertTrue((base / f".work-{_sha(1)}").is_dir(), "the head just built was pruned")
        self.assertTrue((base / f".work-{_sha(3)}").is_dir(), "the newest tree was pruned")
        self.assertFalse((base / f".work-{_sha(2)}").exists())

    def test_a_copy_that_cannot_complete_keeps_the_tree(self) -> None:
        base, journal = self.make_build([_sha(1), _sha(2), _sha(3)])
        (base / f"evidence-{_sha(1)}").write_bytes(b"a FILE squats on the evidence directory name")
        results = {r["Sha"]: r for r in self.retain(base, journal)}
        self.assertEqual(results[_sha(1)]["Action"], "kept-copy-failed", results)
        self.assertTrue((base / f".work-{_sha(1)}" / "src" / "platform" / "qt" / "tool-made.obj").is_file(),
                        "the tree was dropped although its evidence did not verify")
        self.assertTrue((base / f".work-{_sha(1)}" / "logs" / "make.log").is_file())
        self.assertEqual(results[_sha(2)]["Action"], "dropped", "one failed copy must not stop the others")
        self.assertFalse((base / f".work-{_sha(2)}").exists())

    def test_a_tree_the_journal_cannot_prove_is_left_standing_with_its_evidence_copied(self) -> None:
        base, journal = self.make_build([_sha(2), _sha(3)])
        legacy = base / f".work-{_sha(1)}"
        legacy.mkdir()
        self.populate(legacy, _sha(1))
        stamp = time.time() - 3 * 3600 * 10
        os.utime(legacy, (stamp, stamp))
        results = {r["Sha"]: r for r in self.retain(base, journal)}
        self.assertEqual(results[_sha(1)]["Action"], "kept-partial", results)
        self.assertEqual((legacy / "logs" / "make.log").read_bytes(), LOG_BYTES, "an unproven name was deleted")
        self.assertTrue((legacy / "src" / "platform" / "qt" / "tool-made.obj").is_file())
        self.assertEqual(results[_sha(2)]["Action"], "dropped")

    def test_a_dry_run_changes_nothing(self) -> None:
        base, journal = self.make_build([_sha(1), _sha(2)])
        before = sorted(p.relative_to(base).as_posix() for p in base.rglob("*"))
        results = self.retain(base, journal, "-DryRun")
        self.assertEqual([r["Action"] for r in results], ["would-drop"])
        self.assertEqual(results[0]["EvidenceFiles"], 4)
        self.assertEqual(sorted(p.relative_to(base).as_posix() for p in base.rglob("*")), before)

    def test_one_tree_and_quarantined_trees_are_never_candidates(self) -> None:
        base, journal = self.make_build([_sha(1)])
        quarantined = base / f".work-{_sha(2)}.unproven-20261006T000000Z-abcd1234"
        quarantined.mkdir()
        (quarantined / "keep.txt").write_bytes(b"left by New-AttrCudaOwnedRoot, never deleted")
        self.assertEqual(self.retain(base, journal), [])
        self.assertTrue((quarantined / "keep.txt").is_file())
        self.assertTrue((base / f".work-{_sha(1)}").is_dir())

    def test_the_module_has_no_pathname_or_recursive_delete(self) -> None:
        text = RETENTION.read_text(encoding="ascii")
        code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
        self.assertNotRegex(code, r"\bRemove-Item\b|\[IO\.(?:File|Directory)\]::Delete|\brmtree\b|-Recurse\b")
        self.assertRegex(code, r"Remove-AttrCudaTree -TrustedRoot \$treeBuildDir -Path \$tree\.FullName -OwnedJournal \$treeJournal")


@requires_pwsh
@requires_git
class AssemblerSourceArchiveTests(_PwshCase):
    """The REAL assembler on a synthetic tiny repository: it stops at the missing DLL pair (exit 3), which is a
    failure exit taken AFTER the source archive was made and consumed."""

    def _git(self, repo: Path, *args: str) -> str:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc.stdout.strip()

    def _run(self, sha: str, repo: Path, out_dir: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [shutil.which("pwsh"), "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(ASSEMBLER),
             "-SourceCommit", sha, "-DllPairDir", str(self.tmp / "no-such-dll-pair"), "-OutDir", str(out_dir),
             "-RepoRoot", str(repo)],
            capture_output=True, text=True)

    def test_the_source_archive_is_gone_after_a_failed_run_and_its_tree_was_built_from_it(self) -> None:
        repo = self.tmp / "tiny-repo"
        repo.mkdir()
        self._git(repo, "init", "-q")
        self._git(repo, "config", "user.email", "t@example.invalid")
        self._git(repo, "config", "user.name", "t")
        (repo / "a.txt").write_text("synthetic source", encoding="utf-8")
        self._git(repo, "add", "a.txt")
        self._git(repo, "commit", "-q", "-m", "x")
        sha = self._git(repo, "rev-parse", "HEAD")
        out_dir = self.tmp / "cache-out"
        out_dir.mkdir()

        proc = self._run(sha, repo, out_dir)

        self.assertEqual(proc.returncode, 3, f"{proc.stdout}\n{proc.stderr}")
        work = out_dir / f".work-{sha[:12]}"
        self.assertEqual((work / "src" / "a.txt").read_text(encoding="utf-8"), "synthetic source",
                         "the archive was not consumed into the source tree")
        self.assertEqual(list((work / ".job-tmp").glob("*-source.zip")), [], "the source archive outlived its build")
        self.assertRegex(proc.stdout, r"SOURCE ARCHIVE removed after consume, freed \d+ bytes")
        self.assertTrue((out_dir / ".attrcuda-owned.jsonl").is_file())


class AssemblerWiringTests(unittest.TestCase):
    """Static pins: the archive delete sits in a finally after Expand-Archive, and a head prunes its predecessors."""

    def setUp(self) -> None:
        self.text = "\n".join(line.split("#", 1)[0] for line in ASSEMBLER.read_text(encoding="utf-8").splitlines())

    def test_the_archive_is_removed_in_a_finally_by_journal_proof(self) -> None:
        match = re.search(r"Expand-Archive[^\n]*\n[^\n]*\n\}\s*finally\s*\{(.*?)\n\}", self.text, re.S)
        self.assertIsNotNone(match, "the finally after Expand-Archive is missing")
        self.assertIn("Remove-AttrCudaPartialFile -TrustedRoot $OutDir -Path $archivePath -OwnedJournal $OwnedJournal", match.group(1))
        self.assertIn("freed $archiveBytes bytes", match.group(1))

    def test_a_new_head_prunes_the_previous_ones_without_ever_failing_the_build(self) -> None:
        self.assertIn("Invoke-AttrCudaWorkRetention -BuildDir $OutDir -OwnedJournal $OwnedJournal -KeepSha $names.shortSha -IncludeSiblingBuildDirs", self.text)
        position = self.text.index("Invoke-AttrCudaWorkRetention")
        self.assertGreater(position, self.text.index("$publishedManifestSha256 = "), "retention must run after the publish")
        self.assertRegex(self.text[position:position + 400], r"\}\s*catch\s*\{\s*Say \"RETENTION skipped")
        self.assertLess(position, self.text.index("RESULT=ASSEMBLE_OK"))


if __name__ == "__main__":
    unittest.main()
