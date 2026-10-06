"""LANE-BUILD-WORKDIR-RETENTION-1: the assembler's per-head scratch trees are bounded.

Every head iteration of a build leg left <build dir>\\.work-<sha12> (about 1.2 GiB) behind, including
.job-tmp\\<sha12>-source.zip, a 0.29 GiB duplicate of the tree expanded from it. This pins both halves of the fix
against synthetic fixtures (never real footage, never a compile):

  1. the source archive is deleted once the build has consumed it, on success and on failure, by the ownership
     journal's proof (the real assembler script runs on a tiny repository and stops at the missing DLL pair);
  2. keep-newest retention: the newest .work-<sha12> stays, every superseded one has its evidence copied and
     VERIFIED before it is dropped, a copy that does not verify keeps the tree, a tree the journal cannot prove is
     left standing, and -DryRun changes nothing;
  3. (pr285-r2) only a PUBLISHED head is a candidate -- a tree whose build.json was never written failed before
     staging or is still building, and is kept whole -- and a candidate holding a recording (found by its first
     bytes, never by name) or an over-limit file is kept whole too, unless the file is a tracked fixture matched by
     git object identity.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.repo_hygiene.test_playback_attr_3_cuda_behaviour import (
    GIT,
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
# The first four bytes of the raw-video container plus a body. Built from hex so that no test names the
# container's file extension; every file carrying it is named .dat or .bin.
RECORDING_BYTES = bytes.fromhex("4d4c5649") + b"synthetic recording body " * 64


def _sha(index: int) -> str:
    return f"{index:012x}"


def _full(sha: str) -> str:
    return sha + "0" * 28


class _RetentionCase(_PwshCase):
    def pwsh(self, body: str) -> subprocess.CompletedProcess:
        proc = self.run_with_module(f"Import-Module '{RETENTION}' -Force\n" + body)
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        return proc

    def publish(self, build: Path, sha: str, commit: str | None = None, assembled: datetime | None = None) -> None:
        """The manifest the assembler writes LAST, which is what makes a head published."""
        manifest = {
            "sourceCommit": commit or _full(sha),
            "assembledAtUtc": (assembled or datetime.now(timezone.utc)).isoformat(),
        }
        (build / f"playback-attr-3-cuda-{sha}-build.json").write_text(json.dumps(manifest), encoding="ascii")

    def make_build(self, shas: list[str], unpublished: tuple[str, ...] = (), commits: dict[str, str] | None = None,
                   ) -> tuple[Path, Path]:
        """A build dir holding one fresh, journalled .work-<sha> per sha, OLDEST first; every head is published
        unless named in `unpublished`."""
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
            if sha not in unpublished:
                self.publish(base, sha, (commits or {}).get(sha))
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

    def retain(self, base: Path, journal: Path, extra: str = "", prelude: str = "") -> list[dict]:
        proc = self.pwsh(
            prelude +
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
            self.publish(build, sha)
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
        self.publish(base, _sha(1))
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
class PublishedOnlyAndContentGuardTests(_RetentionCase):
    """pr285-r2: the two blockers sol raised on 3040d561, plus the hardening folded in with them."""

    def tree(self, base: Path, sha: str) -> Path:
        return base / f".work-{sha}"

    def test_a_failed_pre_staging_sibling_with_a_unique_exe_is_kept_whole(self) -> None:
        # sol blocker 1: the build got through make, then exited before staging. Its only Release exe sits under
        # src, which the evidence copy skips, and the head never wrote its build.json.
        base, journal = self.make_build([_sha(1), _sha(2), _sha(3)], unpublished=(_sha(1),))
        release = self.tree(base, _sha(1)) / "src" / "platform" / "qt" / "build-release" / "release"
        release.mkdir(parents=True)
        (release / "MLVApp.exe").write_bytes(EXE_BYTES + b" the only copy")
        results = {r["Sha"]: r for r in self.retain(base, journal)}
        self.assertEqual(results[_sha(1)]["Action"], "kept-unpublished", results)
        self.assertEqual((release / "MLVApp.exe").read_bytes(), EXE_BYTES + b" the only copy")
        self.assertTrue((self.tree(base, _sha(1)) / "logs" / "make.log").is_file(), "the whole tree must stay")
        self.assertFalse((base / f"evidence-{_sha(1)}").exists(), "an unpublished tree is not processed at all")
        self.assertEqual(results[_sha(2)]["Action"], "dropped", "a published sibling is still pruned")

    def test_a_manifest_that_does_not_vouch_for_the_tree_does_not_publish_it(self) -> None:
        base, journal = self.make_build([_sha(i) for i in range(1, 6)], unpublished=(_sha(1), _sha(2), _sha(3)))
        self.publish(base, _sha(1), commit=_full(_sha(7)))                                    # names another commit
        self.publish(base, _sha(2), assembled=datetime.now(timezone.utc) - timedelta(days=1))  # an earlier run's manifest
        (base / f"playback-attr-3-cuda-{_sha(3)}-build.json").write_text("{ not json", encoding="ascii")
        results = {r["Sha"]: r for r in self.retain(base, journal)}
        for sha in (_sha(1), _sha(2), _sha(3)):
            self.assertEqual(results[sha]["Action"], "kept-unpublished", results)
            self.assertTrue((self.tree(base, sha) / "logs" / "make.log").is_file())
        self.assertEqual(results[_sha(4)]["Action"], "dropped")

    def test_a_recording_copied_into_a_published_trees_scratch_is_kept_whole(self) -> None:
        # sol blocker 2: created after the recorded root, so the journal's root proof would authorise its deletion.
        base, journal = self.make_build([_sha(1), _sha(2), _sha(3), _sha(4)])
        scratch = self.tree(base, _sha(1)) / ".job-tmp" / "launch-probe"
        (scratch / "diagnosis-copy.dat").write_bytes(RECORDING_BYTES)
        # the name decides nothing: a recording renamed to an evidence-looking name is found by its first bytes
        (self.tree(base, _sha(2)) / "logs" / "renamed.txt").write_bytes(RECORDING_BYTES)
        results = {r["Sha"]: r for r in self.retain(base, journal)}
        for sha in (_sha(1), _sha(2)):
            self.assertEqual(results[sha]["Action"], "kept-guard", results)
            self.assertTrue((self.tree(base, sha) / "logs" / "make.log").is_file(), "the whole tree must stay")
            self.assertFalse((base / f"evidence-{sha}").exists())
        self.assertEqual((scratch / "diagnosis-copy.dat").read_bytes(), RECORDING_BYTES)
        self.assertEqual((self.tree(base, _sha(2)) / "logs" / "renamed.txt").read_bytes(), RECORDING_BYTES)
        self.assertEqual(results[_sha(3)]["Action"], "dropped")

    def test_a_file_over_the_size_limit_keeps_the_tree(self) -> None:
        base, journal = self.make_build([_sha(1), _sha(2), _sha(3)])
        (self.tree(base, _sha(1)) / "src" / "huge.bin").write_bytes(b"\0" * (5 * 1024 * 1024))
        results = {r["Sha"]: r for r in self.retain(base, journal, "-MaxFileBytes 4194304")}
        self.assertEqual(results[_sha(1)]["Action"], "kept-guard", results)
        self.assertTrue((self.tree(base, _sha(1)) / "src" / "huge.bin").is_file())
        self.assertEqual(results[_sha(2)]["Action"], "dropped", "a tree under the limit is unaffected")

    # --- tracked-fixture exemption, by git object identity -------------------------------------------------

    def _fixture_build(self) -> tuple[Path, Path, Path, str, Path]:
        """A real repository tracking one recording-shaped fixture, and a build dir whose OLDEST head is that
        repository's commit, with the tracked bytes expanded under its tree's own src\\tests\\fixtures."""
        repo = self.tmp / "repo"
        fixture_dir = repo / "tests" / "fixtures" / "clips"
        fixture_dir.mkdir(parents=True)
        (fixture_dir / "clip.dat").write_bytes(RECORDING_BYTES)
        for args in (["init", "-q"], ["config", "user.email", "t@example.invalid"], ["config", "user.name", "t"],
                     ["config", "core.autocrlf", "false"], ["add", "tests/fixtures/clips/clip.dat"],
                     ["commit", "-q", "-m", "fixture"]):
            subprocess.run([GIT, "-C", str(repo), *args], check=True, capture_output=True)
        full = subprocess.run([GIT, "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True,
                              text=True).stdout.strip()
        sha = full[:12]
        base, journal = self.make_build([sha, _sha(9)], commits={sha: full})
        fixture = self.tree(base, sha) / "src" / "tests" / "fixtures" / "clips" / "clip.dat"
        fixture.parent.mkdir(parents=True)
        fixture.write_bytes(RECORDING_BYTES)
        return repo, base, journal, sha, fixture

    @requires_git
    def test_a_tracked_fixture_whose_bytes_match_its_commit_is_exempt_and_the_tree_drops(self) -> None:
        repo, base, journal, sha, fixture = self._fixture_build()
        results = {r["Sha"]: r for r in self.retain(base, journal, f"-RepoRoot '{repo}'")}
        self.assertEqual(results[sha]["Action"], "dropped", results)
        self.assertFalse(self.tree(base, sha).exists())
        self.assertEqual((base / f"evidence-{sha}" / "logs" / "make.log").read_bytes(), LOG_BYTES)

    @requires_git
    def test_the_same_fixture_path_with_one_byte_flipped_keeps_the_tree(self) -> None:
        repo, base, journal, sha, fixture = self._fixture_build()
        flipped = bytearray(RECORDING_BYTES)
        flipped[10] ^= 0xFF
        fixture.write_bytes(bytes(flipped))
        results = {r["Sha"]: r for r in self.retain(base, journal, f"-RepoRoot '{repo}'")}
        self.assertEqual(results[sha]["Action"], "kept-guard", results)
        self.assertEqual(fixture.read_bytes(), bytes(flipped))

    @requires_git
    def test_identical_bytes_outside_the_fixture_directory_or_untracked_keep_the_tree(self) -> None:
        repo, base, journal, sha, fixture = self._fixture_build()
        (self.tree(base, sha) / "src" / "tests" / "fixtures" / "clips" / "untracked-name.bin").write_bytes(RECORDING_BYTES)
        results = {r["Sha"]: r for r in self.retain(base, journal, f"-RepoRoot '{repo}'")}
        self.assertEqual(results[sha]["Action"], "kept-guard", "an untracked name under fixtures is not a tracked fixture")
        self.assertTrue(self.tree(base, sha).is_dir())

    @requires_git
    def test_without_a_repo_root_no_fixture_is_exempt(self) -> None:
        repo, base, journal, sha, fixture = self._fixture_build()
        results = {r["Sha"]: r for r in self.retain(base, journal)}
        self.assertEqual(results[sha]["Action"], "kept-guard", results)
        self.assertTrue(fixture.is_file())

    # --- fable HARDENING-2 and sol hardening 4 ------------------------------------------------------------

    def test_a_junction_inside_a_superseded_tree_is_left_and_its_target_is_untouched(self) -> None:
        base, journal = self.make_build([_sha(1), _sha(2), _sha(3)])
        outside = self.tmp / "outside-target"
        outside.mkdir()
        (outside / "precious.txt").write_bytes(b"belongs to nobody the retention knows")
        link = self.tree(base, _sha(1)) / ".job-tmp" / "linked"
        self.pwsh(f"[void](New-Item -ItemType Junction -Path '{link}' -Target '{outside}')\n")
        results = {r["Sha"]: r for r in self.retain(base, journal)}
        self.assertEqual(results[_sha(1)]["Action"], "kept-partial", results)
        self.assertIn("LEFT_NOT_A_FILE", results[_sha(1)]["Detail"])
        self.assertEqual((outside / "precious.txt").read_bytes(), b"belongs to nobody the retention knows")
        self.assertFalse((base / f"evidence-{_sha(1)}" / ".job-tmp" / "linked").exists(), "the evidence copy followed the link")
        self.assertEqual(results[_sha(2)]["Action"], "dropped")

    def test_a_work_tree_outside_a_build_directory_is_never_a_candidate(self) -> None:
        run = self.tmp / "lane-CARD-r1-20261006T0000Z"
        run.mkdir()
        base = run / "build-head"
        base.mkdir()
        journal = base / ".attrcuda-owned.jsonl"
        stage = run / "stage-foo"
        stage.mkdir()
        old = time.time() - 3 * 3600 * 10
        for container, sha in ((stage, _sha(1)), (base, _sha(2)), (base, _sha(3))):
            self.pwsh(f"[void](New-AttrCudaOwnedRoot -TrustedRoot '{container}' -Path '{container / ('.work-' + sha)}' "
                      f"-OwnedJournal '{container / '.attrcuda-owned.jsonl'}' -WarningAction SilentlyContinue)\n")
            self.populate(container / f".work-{sha}", sha)
            self.publish(container, sha)
            stamp = old if sha != _sha(3) else time.time() - 3 * 3600
            os.utime(container / f".work-{sha}", (stamp, stamp))
        results = self.retain(base, journal, "-IncludeSiblingBuildDirs")
        self.assertEqual([r["Sha"] for r in results], [_sha(2)], "the stage-* tree is out of scope")
        self.assertTrue((stage / f".work-{_sha(1)}" / "logs" / "make.log").is_file())
        self.assertFalse((stage / f"evidence-{_sha(1)}").exists())

    def test_a_copy_that_succeeds_but_comes_up_short_keeps_the_tree(self) -> None:
        # The verification must read the DESTINATION: a Copy-Item that reports success and writes one byte too few
        # is the shape a full disk or a filter driver produces.
        shim = (
            "function global:Copy-Item {\n"
            "    [CmdletBinding()] param([string]$LiteralPath, [string]$Destination, [switch]$Force)\n"
            "    $bytes = [IO.File]::ReadAllBytes($LiteralPath)\n"
            "    [IO.File]::WriteAllBytes($Destination, [byte[]]($bytes[0..($bytes.Length - 2)]))\n"
            "}\n")
        base, journal = self.make_build([_sha(1), _sha(2)])
        results = self.retain(base, journal, prelude=shim)
        self.assertEqual([r["Action"] for r in results], ["kept-copy-failed"], results)
        self.assertIn("length", results[0]["Detail"])
        self.assertTrue((self.tree(base, _sha(1)) / "logs" / "make.log").is_file(), "dropped on a short copy")
        self.assertTrue((self.tree(base, _sha(1)) / "src" / "platform" / "qt" / "tool-made.obj").is_file())


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

    def test_the_finally_says_absent_when_git_archive_made_no_file(self) -> None:
        match = re.search(r"\}\s*finally\s*\{(.*?)\n\}", self.text, re.S)
        self.assertIsNotNone(match)
        self.assertRegex(match.group(1), r"\$archiveExisted = Test-Path -LiteralPath \$archivePath")
        self.assertRegex(match.group(1), r"if \(\$archiveGone -and -not \$archiveExisted\) \{ Say \"SOURCE ARCHIVE absent")
        self.assertLess(match.group(1).index("SOURCE ARCHIVE absent"), match.group(1).index("removed after consume"))

    def test_a_new_head_prunes_the_previous_ones_without_ever_failing_the_build(self) -> None:
        self.assertIn("Invoke-AttrCudaWorkRetention -BuildDir $OutDir -OwnedJournal $OwnedJournal -KeepSha $names.shortSha -IncludeSiblingBuildDirs -RepoRoot $RepoRoot", self.text)
        position = self.text.index("Invoke-AttrCudaWorkRetention")
        self.assertGreater(position, self.text.index("$publishedManifestSha256 = "), "retention must run after the publish")
        self.assertRegex(self.text[position:position + 400], r"\}\s*catch\s*\{\s*Say \"RETENTION skipped")
        self.assertLess(position, self.text.index("RESULT=ASSEMBLE_OK"))


if __name__ == "__main__":
    unittest.main()
