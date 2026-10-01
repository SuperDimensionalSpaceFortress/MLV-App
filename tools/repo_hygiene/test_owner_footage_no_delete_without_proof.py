"""OWNER-FOOTAGE-NO-HARDLINK-2: NO delete-capable function can EVER adopt a first-seen identity -- run
against SYNTHETIC NTFS folders only (never real footage).

PR #211 (OWNER-FOOTAGE-NO-HARDLINK-1) was parked on sol's r2 blocker: Remove-AttrCudaTree had an optional
-OwnedJournal and, without one, read each file's CURRENT identity and handed it to the delete as authority.
The assembler called it without a journal on OutDir\\.work-<sha12>, the very tree an older bind-proof job
could have left a neutral owner-clip hard link in; after the owner re-recorded the original, that link was
the LAST name of the old recording with NumberOfLinks == 1 -- indistinguishable from a file the assembler
made -- and the rerun deleted it. No adversary is needed for that; it is the owner's normal workflow.

The class these tests pin is "ownership proof is MANDATORY":
  * every delete-capable function takes its proof as a mandatory argument with no default;
  * a name the journal does not prove is LEFT and typed, never adopted;
  * a build job's scratch/publish tree is created FRESH and recorded (New-AttrCudaOwnedRoot); a tree
    already standing at that name that is not proven is MOVED ASIDE (a rename deletes no name), and what
    is deleted inside a fresh tree must also have been created after the tree was;
  * a submitted input file (no creating handle) is removed only against its CONTENT hash.

RED-FIRST at d8331894 (PR #211's head): each repro below printed OWNER_NAME_DESTROYED=True there (the
standalone script and its output are kept in the round's run directory); every assertion that a synthetic
owner name SURVIVES fails on that tree.

The hard links these tests create are the TEST FIXTURE for the legacy / hostile state; no tracked non-test
source may create one (test_owner_footage_no_hardlink_class.py).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

from tools.repo_hygiene.test_playback_attr_3_cuda_behaviour import (
    MODULE,
    _guard,
    _PwshCase,
    _run_pwsh_file,
    requires_git,
    requires_pwsh,
)

BACHELOR = Path(MODULE).parent
ASSEMBLER = BACHELOR / "playback-attr-3-cuda-assemble.ps1"
OLD_RECORDING = b"synthetic OLD recording bytes -- never real footage " * 24
NEW_RECORDING = b"the owner's NEW recording at the same name"


def _id_literal(fields: dict) -> str:
    return (
        "[pscustomobject]@{ "
        f"VolumeSerialNumber = [uint32]{fields['VolumeSerialNumber']}; "
        f"FileIndexHigh = [uint32]{fields['FileIndexHigh']}; "
        f"FileIndexLow = [uint32]{fields['FileIndexLow']} }}"
    )


class _Synthetic(_PwshCase):
    def make_last_name(self, directory: Path, name: str) -> Path:
        """A synthetic 'owner original' whose second name `name` becomes the LAST name of the old bytes:
        the owner then deletes / re-records the original at its own path (a safe-save, a restore)."""
        directory.mkdir(parents=True, exist_ok=True)
        original = self.tmp / "owner-home" / "owner-original.bin"
        original.parent.mkdir(exist_ok=True)
        original.write_bytes(OLD_RECORDING)
        link = directory / name
        os.link(original, link)
        os.remove(original)
        original.write_bytes(NEW_RECORDING)
        self.assertEqual(os.stat(link).st_nlink, 1, "the fixture must leave a lone last name")
        return link

    def assert_old_recording_survives(self, link: Path) -> None:
        self.assertTrue(link.exists(), "the last name of the old recording was deleted")
        self.assertEqual(link.read_bytes(), OLD_RECORDING)

    def pwsh(self, body: str) -> subprocess.CompletedProcess:
        proc = self.run_with_module(body)
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        return proc


@requires_pwsh
class ProofIsMandatoryTests(_Synthetic):
    """No delete-capable function can be called without its proof -- the parameter is mandatory, so the
    call fails before anything is touched."""

    def _victim(self) -> Path:
        victim = self.tmp / "scratch" / "victim.partial"
        victim.parent.mkdir()
        victim.write_bytes(b"a lone plain file that proves nothing")
        return victim

    def _refused(self, body: str, victim: Path) -> None:
        proc = self.run_with_module(body)
        self.assertNotEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.assertTrue(victim.exists(), "a call without a proof deleted a name")
        self.assertEqual(victim.read_bytes(), b"a lone plain file that proves nothing")

    def test_the_handle_delete_has_no_mode_without_a_proof(self) -> None:
        victim = self._victim()
        self._refused(f"Remove-AttrCudaFileByProof -Path '{victim}'\n", victim)

    def test_by_id_needs_the_recorded_identity(self) -> None:
        victim = self._victim()
        self._refused(f"Remove-AttrCudaFileById -Path '{victim}'\n", victim)

    def test_the_partial_remover_needs_a_journal(self) -> None:
        victim = self._victim()
        self._refused(f"Remove-AttrCudaPartialFile -TrustedRoot '{victim.parent}' -Path '{victim}'\n", victim)

    def test_the_tree_remover_needs_a_journal(self) -> None:
        victim = self._victim()
        self._refused(f"Remove-AttrCudaTree -TrustedRoot '{victim.parent.parent}' -Path '{victim.parent}'\n", victim)

    def test_the_input_remover_needs_the_content_hash(self) -> None:
        victim = self._victim()
        self._refused(f"Remove-AttrCudaInputFileByContent -TrustedRoot '{victim.parent}' -Path '{victim}'\n", victim)

    def test_the_root_creator_needs_a_journal(self) -> None:
        proc = self.run_with_module(
            f"New-AttrCudaOwnedRoot -TrustedRoot '{self.tmp}' -Path '{self.tmp / 'fresh'}'\n")
        self.assertNotEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.assertFalse((self.tmp / "fresh").exists())

    def test_two_proofs_at_once_are_refused(self) -> None:
        victim = self._victim()
        self._refused(
            f"Remove-AttrCudaFileByProof -Path '{victim}' -NotBeforeFileTime 1 -ExpectedSha256 ('0' * 64)\n", victim)


@requires_pwsh
class OwnedRootTests(_Synthetic):
    """New-AttrCudaOwnedRoot + Remove-AttrCudaTree: what a build job's scratch tree may and may not lose."""

    def _root_and_journal(self, name: str = "run") -> tuple[Path, Path, Path]:
        base = self.tmp / "agent"
        base.mkdir(exist_ok=True)
        return base, base / name, base / ".attrcuda-owned.jsonl"

    def _create_root(self, base: Path, root: Path, journal: Path) -> subprocess.CompletedProcess:
        return self.pwsh(
            f"$r = New-AttrCudaOwnedRoot -TrustedRoot '{base}' -Path '{root}' -OwnedJournal '{journal}' -WarningAction SilentlyContinue\n"
            "Write-Output ('SWEPT=' + $r.Swept)\n"
            "Write-Output ('QUARANTINED=' + $r.Quarantined)\n")

    def _sweep(self, base: Path, root: Path, journal: Path) -> subprocess.CompletedProcess:
        return self.pwsh(
            f"$r = Remove-AttrCudaTree -TrustedRoot '{base}' -Path '{root}' -OwnedJournal '{journal}'\n"
            "Write-Output ('REMOVED=' + $r.Removed)\n"
            "Write-Output ('LEFT=' + (($r.Left | ForEach-Object { $_.Token }) -join ','))\n"
            "Write-Output ('TREE_REMOVED=' + $r.TreeRemoved)\n")

    def test_sol_r2_a_legacy_last_name_under_a_standing_work_tree_is_moved_aside_never_deleted(self) -> None:
        # sol r2's blocker, at the function level: the standing tree is the assembler's .work-<sha12>; an
        # older bind-proof job left a neutral owner-clip entry in it; the owner re-recorded the original.
        base, root, journal = self._root_and_journal(".work-0123456789ab")
        legacy = self.make_last_name(root / "proof-1" / "owner-clip", "owner-clip.bin")
        proc = self._create_root(base, root, journal)
        quarantined = Path(next(l for l in proc.stdout.splitlines() if l.startswith("QUARANTINED="))[len("QUARANTINED="):])
        self.assertNotEqual(str(quarantined), "")
        self.assertIn(".unproven-", quarantined.name)
        self.assert_old_recording_survives(quarantined / "proof-1" / "owner-clip" / "owner-clip.bin")
        self.assertFalse(legacy.exists(), "the old tree moved aside as a whole")
        self.assertTrue(root.is_dir(), "a fresh root stands at the requested name")
        self.assertEqual(sorted(p.name for p in root.iterdir()), [], "the fresh root is empty")

    def test_a_tree_this_code_recorded_earlier_is_swept_by_proof_and_the_name_is_reused(self) -> None:
        base, root, journal = self._root_and_journal()
        self._create_root(base, root, journal)
        (root / "src" / "deep").mkdir(parents=True)
        (root / "src" / "deep" / "tool-made.obj").write_bytes(b"made by a child tool")
        (root / "log.txt").write_bytes(b"x")
        proc = self._create_root(base, root, journal)  # the next run
        self.assertIn("SWEPT=2", proc.stdout)
        self.assertIn("QUARANTINED=", proc.stdout.splitlines())
        self.assertEqual(sorted(p.name for p in root.iterdir()), [], "the swept name was created fresh again")
        self.assertEqual([p.name for p in base.iterdir() if ".unproven-" in p.name], [])

    def test_a_standing_tree_with_a_name_the_sweep_cannot_prove_is_moved_aside_whole(self) -> None:
        base, root, journal = self._root_and_journal()
        self._create_root(base, root, journal)
        (root / "mine.bin").write_bytes(b"made after the root")
        older = self.make_last_name(self.tmp / "elsewhere", "older.bin")
        os.link(older, root / "planted-old-bytes.bin")  # keeps the OLD file's creation time
        proc = self._create_root(base, root, journal)
        quarantined = Path(next(l for l in proc.stdout.splitlines() if l.startswith("QUARANTINED="))[len("QUARANTINED="):])
        self.assertIn(".unproven-", quarantined.name)
        self.assertEqual((quarantined / "planted-old-bytes.bin").read_bytes(), OLD_RECORDING)
        self.assertEqual(older.read_bytes(), OLD_RECORDING)
        self.assertFalse((quarantined / "mine.bin").exists(), "a provably made file in the same tree was still removed")

    def test_a_hard_link_to_older_bytes_inside_a_fresh_root_is_left_even_with_one_name(self) -> None:
        # The last-name case INSIDE a recorded tree: the other name is gone, so the link reads
        # NumberOfLinks == 1. Its creation time is the OLD file's, which predates the root.
        base, root, journal = self._root_and_journal()
        older = self.make_last_name(self.tmp / "elsewhere2", "older.bin")
        old_stamp = os.stat(older).st_ctime
        self.assertLess(old_stamp, 1e12)
        # Make sure the root's creation time is later than the old file's.
        self._create_root(base, root, journal)
        os.replace(older, root / "carried.bin")  # a rename keeps the creation time of the old object
        (root / "mine.bin").write_bytes(b"mine")
        proc = self._sweep(base, root, journal)
        self.assertIn("REMOVED=1", proc.stdout)
        self.assertIn("LEFT=LEFT_PREDATES_ROOT", proc.stdout)
        self.assertIn("TREE_REMOVED=False", proc.stdout)
        self.assertEqual((root / "carried.bin").read_bytes(), OLD_RECORDING)

    def test_a_directory_swapped_in_under_a_recorded_name_attests_nothing(self) -> None:
        base, root, journal = self._root_and_journal()
        self._create_root(base, root, journal)
        shutil.rmtree(root)
        root.mkdir()  # NOT created by the job: a different directory object under the recorded name
        stranger = root / "stranger.bin"
        stranger.write_bytes(b"not provably the job's")
        proc = self._sweep(base, root, journal)
        self.assertIn("REMOVED=0", proc.stdout)
        self.assertIn("LEFT=LEFT_UNOWNED", proc.stdout)
        self.assertEqual(stranger.read_bytes(), b"not provably the job's")

    def test_an_unrecorded_tree_loses_nothing_not_even_lone_plain_files(self) -> None:
        base, root, journal = self._root_and_journal()
        (root / "a" / "b").mkdir(parents=True)
        (root / "a" / "b" / "f.bin").write_bytes(b"x")
        (root / "g.bin").write_bytes(b"y")
        proc = self._sweep(base, root, journal)
        self.assertIn("REMOVED=0", proc.stdout)
        self.assertIn("LEFT=LEFT_UNOWNED,LEFT_UNOWNED", proc.stdout)
        self.assertEqual((root / "a" / "b" / "f.bin").read_bytes(), b"x")
        self.assertEqual((root / "g.bin").read_bytes(), b"y")

    def test_a_read_only_tool_file_and_nested_directories_in_a_fresh_root_are_removed(self) -> None:
        base, root, journal = self._root_and_journal()
        self._create_root(base, root, journal)
        (root / "a" / "b").mkdir(parents=True)
        locked = root / "a" / "b" / "ro.bin"
        locked.write_bytes(b"x")
        os.chmod(locked, 0o444)
        (root / "g.bin").write_bytes(b"y")
        proc = self._sweep(base, root, journal)
        self.assertIn("REMOVED=2", proc.stdout, f"{proc.stdout}")
        self.assertIn("TREE_REMOVED=True", proc.stdout)
        self.assertFalse(root.exists())

    def test_a_file_another_handle_holds_is_left_and_the_rest_of_the_tree_still_goes(self) -> None:
        base, root, journal = self._root_and_journal()
        self._create_root(base, root, journal)
        held = root / "held.bin"
        held.write_bytes(b"held")
        (root / "other.bin").write_bytes(b"other")
        proc = self.pwsh(
            f"$h = [IO.File]::Open('{held}', [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)\n"
            f"$r = Remove-AttrCudaTree -TrustedRoot '{base}' -Path '{root}' -OwnedJournal '{journal}'\n"
            "$h.Dispose()\n"
            "Write-Output ('LEFT=' + (($r.Left | ForEach-Object { $_.Token }) -join ','))\n"
            "Write-Output ('TREE_REMOVED=' + $r.TreeRemoved)\n")
        self.assertIn("LEFT=LEFT_UNAVAILABLE", proc.stdout)
        self.assertIn("TREE_REMOVED=False", proc.stdout)
        self.assertTrue(held.exists())
        self.assertFalse((root / "other.bin").exists())

    def test_a_root_name_occupied_by_a_file_is_refused(self) -> None:
        base, root, journal = self._root_and_journal()
        root.write_bytes(b"a file where the tree should be")
        proc = self.run_with_module(_guard(
            f"New-AttrCudaOwnedRoot -TrustedRoot '{base}' -Path '{root}' -OwnedJournal '{journal}'"))
        self.assert_throws(proc, "ATTRCUDA_ROOT_OCCUPIED")
        self.assertEqual(root.read_bytes(), b"a file where the tree should be")

    def test_a_linked_ancestor_is_refused_before_anything_moves(self) -> None:
        base = self.tmp / "agent2"
        real = self.tmp / "real-target"
        (real / "run").mkdir(parents=True)
        victim = real / "run" / "victim.bin"
        victim.write_bytes(b"outside the trusted root")
        base.mkdir()
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(base / "linked"), str(real)], capture_output=True, text=True)
        self.assertEqual(made.returncode, 0, made.stdout + made.stderr)
        proc = self.run_with_module(_guard(
            f"New-AttrCudaOwnedRoot -TrustedRoot '{base}' -Path '{base / 'linked' / 'run'}' -OwnedJournal '{base / 'j.jsonl'}'"))
        self.assert_throws(proc, "ATTRCUDA_ANCESTOR_IS_LINK")
        self.assertEqual(victim.read_bytes(), b"outside the trusted root")


@requires_pwsh
class ContentProofTests(_Synthetic):
    """A submitted input (no creating handle) is removed only against the bytes the job verified."""

    def _remove(self, path: Path, sha: str) -> str:
        proc = self.pwsh(
            f"$ok = Remove-AttrCudaInputFileByContent -TrustedRoot '{path.parent}' -Path '{path}' -ExpectedSha256 '{sha}' -WarningAction SilentlyContinue\n"
            "Write-Output ('REMOVED=' + $ok)\n")
        return next(l for l in proc.stdout.splitlines() if l.startswith("REMOVED="))[len("REMOVED="):]

    def test_the_verified_package_bytes_are_removed(self) -> None:
        import hashlib
        inbox = self.tmp / "inbox"
        inbox.mkdir()
        pkg = inbox / "pkg.zip"
        pkg.write_bytes(b"PACKAGE BYTES " * 100)
        self.assertEqual(self._remove(pkg, hashlib.sha256(pkg.read_bytes()).hexdigest()), "True")
        self.assertFalse(pkg.exists())

    def test_a_name_that_is_the_last_name_of_an_old_recording_is_left_whatever_it_is_called(self) -> None:
        # The realistic case: the inbox slot holds a legacy hard link that is now the last name of old
        # footage. Its bytes are not the verified package bytes.
        import hashlib
        inbox = self.tmp / "inbox2"
        legacy = self.make_last_name(inbox, "pkg.zip")
        expected = hashlib.sha256(b"PACKAGE BYTES").hexdigest()
        self.assertEqual(self._remove(legacy, expected), "False")
        self.assert_old_recording_survives(legacy)

    def test_a_name_with_a_second_name_is_left_even_when_the_bytes_match(self) -> None:
        import hashlib
        inbox = self.tmp / "inbox3"
        inbox.mkdir()
        pkg = inbox / "pkg.zip"
        pkg.write_bytes(b"PACKAGE BYTES")
        other = self.tmp / "other-name.zip"
        os.link(pkg, other)
        self.assertEqual(self._remove(pkg, hashlib.sha256(b"PACKAGE BYTES").hexdigest()), "False")
        self.assertEqual(pkg.read_bytes(), b"PACKAGE BYTES")
        self.assertEqual(other.read_bytes(), b"PACKAGE BYTES")

    def test_an_absent_name_is_a_clean_true_and_a_directory_is_left(self) -> None:
        inbox = self.tmp / "inbox4"
        inbox.mkdir()
        self.assertEqual(self._remove(inbox / "nothing.zip", "0" * 64), "True")
        (inbox / "dir").mkdir()
        self.assertEqual(self._remove(inbox / "dir", "0" * 64), "False")
        self.assertTrue((inbox / "dir").is_dir())


@requires_pwsh
class JournalProofTests(_Synthetic):
    def test_the_journal_records_its_own_creation_and_is_deleted_only_by_that_identity(self) -> None:
        base = self.tmp / "agent"
        root = base / "run"
        journal = root / ".attrcuda-owned.jsonl"
        root.mkdir(parents=True)
        mine = root / "mine.bin"
        made = self.pwsh(f"[void](Publish-AttrCudaText -Path '{mine}' -Value 'x' -OwnedJournal '{journal}')\n")
        first = json.loads(journal.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(first["k"], "self")
        proc = self.pwsh(
            f"$r = Remove-AttrCudaTree -TrustedRoot '{base}' -Path '{root}' -OwnedJournal '{journal}'\n"
            "Write-Output ('TREE_REMOVED=' + $r.TreeRemoved)\n")
        self.assertIn("TREE_REMOVED=True", proc.stdout)
        self.assertFalse(root.exists())
        self.assertEqual(made.returncode, 0)

    def test_a_journal_name_swapped_for_a_hard_link_to_old_footage_is_left_and_unwritten(self) -> None:
        base = self.tmp / "agent3"
        root = base / "run"
        journal = root / ".attrcuda-owned.jsonl"
        root.mkdir(parents=True)
        mine = root / "mine.bin"
        self.pwsh(f"[void](Publish-AttrCudaText -Path '{mine}' -Value 'x' -OwnedJournal '{journal}')\n")
        os.remove(journal)
        legacy = self.make_last_name(self.tmp / "elsewhere3", "keep.bin")
        os.link(legacy, journal)
        before = legacy.read_bytes()
        proc = self.run_with_module(_guard(
            f"Add-AttrCudaOwnedRecord -Journal '{journal}' -Path '{mine}' -FileId ([pscustomobject]@{{ VolumeSerialNumber=1; FileIndexHigh=2; FileIndexLow=3 }})"))
        self.assert_throws(proc, "ATTRCUDA_OWNED_JOURNAL_HAS_SECOND_NAME")
        self.assertEqual(legacy.read_bytes(), before, "a byte was appended to a name of old footage")
        self.assertEqual(journal.read_bytes(), before)

    def test_a_journal_that_is_a_symlink_is_refused_before_a_byte_is_written(self) -> None:
        base = self.tmp / "agent4"
        root = base / "run"
        root.mkdir(parents=True)
        target = self.tmp / "victim.txt"
        target.write_bytes(b"victim")
        journal = root / ".attrcuda-owned.jsonl"
        try:
            os.symlink(target, journal)
        except OSError:
            self.skipTest("this host cannot create a symlink")
        mine = root / "mine.bin"
        proc = self.run_with_module(_guard(
            f"Add-AttrCudaOwnedRecord -Journal '{journal}' -Path '{mine}' -FileId ([pscustomobject]@{{ VolumeSerialNumber=1; FileIndexHigh=2; FileIndexLow=3 }})"))
        self.assert_throws(proc, "ATTRCUDA_OWNED_JOURNAL_IS_LINK")
        self.assertEqual(target.read_bytes(), b"victim")

    def test_a_create_that_cannot_be_recorded_removes_the_file_it_just_made(self) -> None:
        # The record path is outside the journal's directory, so Add-AttrCudaOwnedRecord throws; the
        # stream is closed and the file is deleted by the identity read off its creating handle.
        base = self.tmp / "agent5"
        (base / "a").mkdir(parents=True)
        (base / "b").mkdir()
        made = base / "b" / "x.bin"
        proc = self.run_with_module(_guard(
            f"Publish-AttrCudaText -Path '{made}' -Value 'x' -OwnedJournal '{base / 'a' / 'j.jsonl'}'"))
        self.assert_throws(proc, "ATTRCUDA_OWNED_RECORD_OUTSIDE_JOURNAL_DIRECTORY")
        self.assertFalse(made.exists(), "a file this call created and could not record was left behind")

    def test_two_processes_appending_to_one_journal_lose_no_record(self) -> None:
        base = self.tmp / "agent6"
        base.mkdir()
        journal = base / ".attrcuda-owned.jsonl"
        scripts = []
        for tag in ("p", "q"):
            script = self.tmp / f"append-{tag}.ps1"
            script.write_text(
                "$ErrorActionPreference = 'Stop'\n"
                f"Import-Module '{MODULE}' -Force\n"
                "1..12 | ForEach-Object {\n"
                f"    $path = '{base}\\{tag}-' + $_ + '.bin'\n"
                f"    [void](Publish-AttrCudaText -Path $path -Value 'x' -OwnedJournal '{journal}')\n"
                "}\n",
                encoding="utf-8")
            scripts.append(script)
        procs = [subprocess.Popen(
            [shutil.which("pwsh"), "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(s)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for s in scripts]
        outputs = [p.communicate(timeout=300) for p in procs]
        for proc, (out, err) in zip(procs, outputs):
            self.assertEqual(proc.returncode, 0, f"{out}\n{err}")
        records = [json.loads(l) for l in journal.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(sum(1 for r in records if r.get("k") == "self"), 1, "exactly one creator wrote the self line")
        self.assertEqual(sum(1 for r in records if r.get("k") != "self"), 24)


@requires_pwsh
@requires_git
class AssemblerLegacyScratchTests(_Synthetic):
    """sol r2's repro, end to end through the REAL assembler script on a synthetic tiny repository: the
    run stops at the missing DLL pair (exit 3), AFTER the scratch-tree step that used to delete."""

    def _git(self, repo: Path, *args: str) -> str:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc.stdout.strip()

    def test_the_assembler_leaves_a_legacy_last_name_under_its_work_tree(self) -> None:
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
        legacy = self.make_last_name(out_dir / f".work-{sha[:12]}" / "proof-1" / ("owner-" + "clip"), "owner-clip.bin")

        proc = subprocess.run(
            [shutil.which("pwsh"), "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(ASSEMBLER),
             "-SourceCommit", sha, "-DllPairDir", str(self.tmp / "no-such-dll-pair"), "-OutDir", str(out_dir),
             "-RepoRoot", str(repo)],
            capture_output=True, text=True)

        self.assertEqual(proc.returncode, 3, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("STEP=dllPairDir", proc.stdout)
        survivors = [p for p in out_dir.glob(f".work-{sha[:12]}.unproven-*")]
        self.assertEqual(len(survivors), 1, f"{[p.name for p in out_dir.iterdir()]}")
        self.assert_old_recording_survives(survivors[0] / "proof-1" / ("owner-" + "clip") / "owner-clip.bin")
        self.assertTrue((out_dir / f".work-{sha[:12]}" / "src").is_dir(), "the run built in a fresh tree")
        self.assertEqual((self.tmp / "owner-home" / "owner-original.bin").read_bytes(), NEW_RECORDING)

    def test_the_assembler_sweeps_its_own_previous_tree_by_proof(self) -> None:
        repo = self.tmp / "tiny-repo2"
        repo.mkdir()
        self._git(repo, "init", "-q")
        self._git(repo, "config", "user.email", "t@example.invalid")
        self._git(repo, "config", "user.name", "t")
        (repo / "a.txt").write_text("synthetic source", encoding="utf-8")
        self._git(repo, "add", "a.txt")
        self._git(repo, "commit", "-q", "-m", "x")
        sha = self._git(repo, "rev-parse", "HEAD")
        out_dir = self.tmp / "cache-out2"
        out_dir.mkdir()
        argv = [shutil.which("pwsh"), "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(ASSEMBLER),
                "-SourceCommit", sha, "-DllPairDir", str(self.tmp / "no-such-dll-pair"), "-OutDir", str(out_dir),
                "-RepoRoot", str(repo)]
        first = subprocess.run(argv, capture_output=True, text=True)
        self.assertEqual(first.returncode, 3, f"{first.stdout}\n{first.stderr}")
        marker = out_dir / f".work-{sha[:12]}" / "from-the-first-run.txt"
        marker.write_text("x", encoding="utf-8")
        second = subprocess.run(argv, capture_output=True, text=True)
        self.assertEqual(second.returncode, 3, f"{second.stdout}\n{second.stderr}")
        self.assertFalse(marker.exists(), "the first run's own tree was not swept")
        self.assertEqual([p.name for p in out_dir.iterdir() if ".unproven-" in p.name], [], "a tree this code recorded was quarantined instead of swept")


if __name__ == "__main__":
    unittest.main()
