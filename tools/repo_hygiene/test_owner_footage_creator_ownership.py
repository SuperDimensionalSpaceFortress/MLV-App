"""OWNER-FOOTAGE-NO-HARDLINK-1 round 2: CREATOR-RECORDED ownership, delete through a held handle, and a
journal-driven (never recursive-pathname) tree delete -- run against SYNTHETIC NTFS folders only.

Round 1 (4e7bf0c4) made views symlinks or verified copies and put an identity check in front of the
deletes it knew about. sol's r1 review found three paths that still removed a name of owner footage, and
the hub's threat model names the realistic way each one bites with no adversary at all: a LEGACY artifact
-- a hard link an older (#200-era) build left under a neutral owner-clip name -- becomes the LAST name of
old footage the moment the owner deletes, moves or re-records the original (an editor's safe-save, a
OneDrive or backup restore), and then reads NumberOfLinks == 1, exactly like a file this job made.

  B1  the staging CLI removed its share-side slot by pathname (Remove-AttrCudaPartialFile);
  B2  first-SEEN identity was adopted as ownership (the stage job's staged copy, the leftover sweep);
  B3  the scratch-tree delete scanned, then ran `Remove-Item -Recurse`.

The ruling these tests pin: ownership is recorded at CREATION, from the creating handle (journal /
returned Id), and an identity first seen later never confers it; every delete is made through one held
handle opened with share mode none (identity and link count checked on THAT handle); anything not
provably the job's is LEFT and typed (LEFT_UNOWNED / LEFT_LEGACY / LEFT_ID_MISMATCH / LEFT_MULTI_LINK).

RED-FIRST at 4e7bf0c4 (synthetic folders; the repro script and its output are kept in the r2 run
directory): B1, B2a, B2b (the realistic "owner re-records the original" workflow) and B3 each printed
OWNER_NAME_DESTROYED=True -- the synthetic owner name was gone. Every test below that asserts a survivor
fails on that tree.

The hard links these tests create are the TEST FIXTURE for the hostile / legacy state; no tracked
non-test source may create one (test_owner_footage_no_hardlink_class.py).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import unittest
from pathlib import Path

from tools.repo_hygiene.test_playback_attr_3_cuda_behaviour import (
    MODULE,
    OWNER_FOOTAGE_MODULE,
    _guard,
    _PwshCase,
    _run_pwsh_file,
    requires_pwsh,
)

BACHELOR = Path(MODULE).parent
CLI = BACHELOR / "attr3-footage-stage.ps1"
OLD_RECORDING = b"synthetic OLD recording bytes -- never real footage " * 24
BASE_EXTENSION = "." + "MLV"


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
        original = directory / "owner-original.bin"
        original.write_bytes(OLD_RECORDING)
        link = directory / name
        os.link(original, link)
        os.remove(original)
        original.write_bytes(b"the owner's NEW recording at the same name")
        self.assertEqual(os.stat(link).st_nlink, 1, "the fixture must leave a lone last name")
        return link

    def created_id(self, path: Path, payload: bytes = b"a job-made file") -> dict:
        """Create `path` with CreateNew the way the jobs do and return the identity off that handle."""
        proc = self.run_with_module(
            f"$s = [IO.File]::Open('{path}', [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)\n"
            "$id = Get-AttrCudaFileId -Stream $s\n"
            f"$b = [Text.Encoding]::ASCII.GetBytes('{payload.decode('ascii')}'); $s.Write($b, 0, $b.Length); $s.Dispose()\n"
            "[pscustomobject]@{ VolumeSerialNumber = $id.VolumeSerialNumber; FileIndexHigh = $id.FileIndexHigh; "
            "FileIndexLow = $id.FileIndexLow } | ConvertTo-Json -Compress\n",
            name="created-id.ps1",
        )
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        return json.loads(proc.stdout.strip().splitlines()[-1])


@requires_pwsh
class StagingCliResidueCleanupTests(_Synthetic):
    """B1: the submitter CLI's attempt-residue cleanup."""

    def _run_residue(self, share_root: Path, share_dir: Path, slot: Path, fields: dict) -> subprocess.CompletedProcess:
        script = self.tmp / "residue.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"$genText = [IO.File]::ReadAllText('{CLI}')\n"
            "$t = $null; $e = $null\n"
            "$ast = [System.Management.Automation.Language.Parser]::ParseInput($genText, [ref]$t, [ref]$e)\n"
            "$fn = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and "
            "$n.Name -eq 'Remove-Attr3FootageStageAttemptResidue' }, $true) | Select-Object -First 1\n"
            "if (-not $fn) { throw 'FUNCTION_NOT_FOUND' }\n"
            f"Import-Module '{MODULE}' -Force\n"
            f"$shareStageRoot = '{share_root}'\n"
            f"$shareStageDir = '{share_dir}'\n"
            f"$createdShareSlots = @([pscustomobject]@{{ Path = '{slot}'; Id = ({_id_literal(fields)}) }})\n"
            "Invoke-Expression $fn.Extent.Text\n"
            "Remove-Attr3FootageStageAttemptResidue\n",
            encoding="utf-8",
        )
        return _run_pwsh_file(script)

    def test_a_slot_that_became_the_last_name_of_old_footage_is_left_and_recorded(self) -> None:
        # sol r1 blocker 1: part 0 transferred, a later part failed, and the slot is no longer the file
        # this attempt created -- it is a hard link to an old recording whose other name is gone. The
        # pathname delete removed it (the last name: the bytes with it).
        share_root = self.tmp / "share-stage"
        share_dir = share_root / "attempt-1"
        share_dir.mkdir(parents=True)
        slot = share_dir / "part-0"
        fields = self.created_id(slot)
        os.remove(slot)
        legacy = self.make_last_name(self.tmp / "elsewhere", "kept-name")
        os.replace(legacy, slot)

        proc = self._run_residue(share_root, share_dir, slot, fields)

        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.assertEqual(slot.read_bytes(), OLD_RECORDING, "the last name of the old recording was deleted")
        self.assertIn("ATTR3_STAGE_RESIDUE_LEFT_IN_PLACE", proc.stdout)
        self.assertIn("RESULT=LEFT_ID_MISMATCH", proc.stdout)
        self.assertNotIn(str(slot), proc.stdout + proc.stderr)
        self.assertTrue(share_dir.is_dir(), "a directory that still holds a left slot stays")

    def test_a_slot_that_is_still_the_created_object_is_removed_and_so_is_the_empty_directory(self) -> None:
        share_root = self.tmp / "share-stage-ok"
        share_dir = share_root / "attempt-1"
        share_dir.mkdir(parents=True)
        slot = share_dir / "part-0"
        fields = self.created_id(slot)

        proc = self._run_residue(share_root, share_dir, slot, fields)

        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.assertFalse(slot.exists())
        self.assertFalse(share_dir.exists())
        self.assertNotIn("LEFT_IN_PLACE", proc.stdout)

    def test_a_created_slot_that_gained_a_second_name_is_left(self) -> None:
        share_root = self.tmp / "share-stage-linked"
        share_dir = share_root / "attempt-1"
        share_dir.mkdir(parents=True)
        slot = share_dir / "part-0"
        fields = self.created_id(slot, b"a slot with a second name")
        other = self.tmp / "the-other-name"
        os.link(slot, other)

        proc = self._run_residue(share_root, share_dir, slot, fields)

        self.assertIn("RESULT=LEFT_MULTI_LINK", proc.stdout)
        self.assertEqual(slot.read_bytes(), b"a slot with a second name")
        self.assertEqual(other.read_bytes(), b"a slot with a second name")

    def test_send_returns_the_identity_read_off_its_own_creating_handle(self) -> None:
        source = self.tmp / "source.raw"
        source.write_bytes(b"synthetic part bytes " * 40)
        sha = hashlib.sha256(source.read_bytes()).hexdigest()
        stage = self.tmp / "share" / "footage-stage" / "job-1"
        proc = self.run_with_module(
            f"$r = Send-AttrCudaOwnerFootagePartToStaging -SourcePath '{source}' -StagingDirectory '{stage}' -Index 0 "
            f"-ExpectedLength {source.stat().st_size} -ExpectedSha256 '{sha}'\n"
            "$now = Get-AttrCudaFileId -Path $r.Path\n"
            "Write-Output ('CREATED=' + $r.Created)\n"
            "Write-Output ('ID_MATCHES_FINAL_SLOT=' + (($r.Id.VolumeSerialNumber -eq $now.VolumeSerialNumber) -and ($r.Id.FileIndexHigh -eq $now.FileIndexHigh) -and ($r.Id.FileIndexLow -eq $now.FileIndexLow)))\n"
            f"$again = Send-AttrCudaOwnerFootagePartToStaging -SourcePath '{source}' -StagingDirectory '{stage}' -Index 0 "
            f"-ExpectedLength {source.stat().st_size} -ExpectedSha256 '{sha}'\n"
            "Write-Output ('SECOND_CREATED=' + $again.Created)\n"
            "Write-Output ('SECOND_ID_IS_NULL=' + ($null -eq $again.Id))\n")
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("CREATED=True", proc.stdout)
        self.assertIn("ID_MATCHES_FINAL_SLOT=True", proc.stdout)
        self.assertIn("SECOND_CREATED=False", proc.stdout)
        self.assertIn("SECOND_ID_IS_NULL=True", proc.stdout)


@requires_pwsh
class HandleDeleteTests(_Synthetic):
    """Delete through ONE held handle, share mode none."""

    def _remove(self, path: Path, fields: dict, extra: str = "") -> str:
        proc = self.run_with_module(
            f"Write-Output ('TOKEN=' + (Remove-AttrCudaFileById -Path '{path}' -FileId ({_id_literal(fields)}){extra}))\n")
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        return next(line for line in proc.stdout.splitlines() if line.startswith("TOKEN="))[len("TOKEN="):]

    def test_a_name_another_handle_holds_open_even_sharing_everything_is_left(self) -> None:
        # Share mode none: the delete handle conflicts with ANY other open handle, so nothing can slip a
        # second name onto (or swap) the file between the link-count check and the delete. The round 1
        # primitive opened with share-all and deleted a name a permissive reader still held.
        path = self.tmp / "held.partial"
        fields = self.created_id(path)
        proc = self.run_with_module(
            f"$held = [IO.File]::Open('{path}', [IO.FileMode]::Open, [IO.FileAccess]::Read, "
            "([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))\n"
            f"Write-Output ('TOKEN=' + (Remove-AttrCudaFileById -Path '{path}' -FileId ({_id_literal(fields)})))\n"
            "$held.Dispose()\n")
        self.assertIn("TOKEN=LEFT_UNAVAILABLE", proc.stdout, f"{proc.stdout}\n{proc.stderr}")
        self.assertTrue(path.exists())

    def test_a_read_only_job_file_is_deleted_by_clearing_the_flag_on_the_same_handle(self) -> None:
        path = self.tmp / "readonly.partial"
        fields = self.created_id(path)
        os.chmod(path, 0o444)
        self.assertEqual(self._remove(path, fields), "DELETED")
        self.assertFalse(path.exists())

    def test_a_read_only_name_swapped_for_a_hard_link_is_left_and_its_flag_is_not_touched(self) -> None:
        owner = self.tmp / "owner-original.bin"
        owner.write_bytes(OLD_RECORDING)
        path = self.tmp / "swapped.partial"
        fields = self.created_id(path)
        os.remove(path)
        os.link(owner, path)
        os.chmod(owner, 0o444)
        self.assertEqual(self._remove(path, fields), "LEFT_ID_MISMATCH")
        self.assertEqual(owner.read_bytes(), OLD_RECORDING)
        self.assertEqual(os.stat(owner).st_nlink, 2)
        self.assertFalse(os.access(owner, os.W_OK), "the owner's read-only flag must not be cleared")

    def test_the_build_route_partial_cleanup_leaves_a_name_swapped_for_a_hard_link(self) -> None:
        # Remove-AttrCudaPartialFile (build-route .partial slots) is no longer a pathname delete either.
        root = self.tmp / "out"
        root.mkdir()
        owner = self.tmp / "owner-original.bin"
        owner.write_bytes(OLD_RECORDING)
        partial = root / "thing.partial"
        os.link(owner, partial)
        proc = self.run_with_module(
            f"$ok = Remove-AttrCudaPartialFile -TrustedRoot '{root}' -Path '{partial}' -WarningAction SilentlyContinue\n"
            "Write-Output ('REMOVED=' + $ok)\n")
        self.assertIn("REMOVED=False", proc.stdout, f"{proc.stdout}\n{proc.stderr}")
        self.assertEqual(owner.read_bytes(), OLD_RECORDING)
        self.assertEqual(partial.read_bytes(), OLD_RECORDING)

    def test_a_lone_build_route_partial_is_still_removed(self) -> None:
        root = self.tmp / "out2"
        root.mkdir()
        partial = root / "thing.partial"
        partial.write_bytes(b"x")
        proc = self.run_with_module(
            f"$ok = Remove-AttrCudaPartialFile -TrustedRoot '{root}' -Path '{partial}'\nWrite-Output ('REMOVED=' + $ok)\n")
        self.assertIn("REMOVED=True", proc.stdout, f"{proc.stdout}\n{proc.stderr}")
        self.assertFalse(partial.exists())


@requires_pwsh
class JournalTests(_Synthetic):
    def test_a_record_is_appended_durably_with_a_path_relative_to_the_journal_directory(self) -> None:
        work = self.tmp / "work"
        (work / "owner-clip").mkdir(parents=True)
        made = work / "owner-clip" / "a.bin"
        fields = self.created_id(made)
        journal = work / ".attrcuda-owned.jsonl"
        proc = self.run_with_module(
            f"Add-AttrCudaOwnedRecord -Journal '{journal}' -Path '{made}' -FileId ({_id_literal(fields)})\n"
            f"Add-AttrCudaOwnedRecord -Journal '{journal}' -Path '{made}' -FileId ({_id_literal(fields)}) -IsReparsePoint\n")
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        lines = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["p"], "owner-clip\\a.bin")
        self.assertEqual(
            (lines[0]["v"], lines[0]["h"], lines[0]["l"]),
            (fields["VolumeSerialNumber"], fields["FileIndexHigh"], fields["FileIndexLow"]))
        self.assertFalse(lines[0]["r"])
        self.assertTrue(lines[1]["r"])

    def test_a_path_outside_the_journal_directory_is_refused(self) -> None:
        work = self.tmp / "work2"
        work.mkdir()
        outside = self.tmp / "outside.bin"
        fields = self.created_id(outside)
        proc = self.run_with_module(_guard(
            f"Add-AttrCudaOwnedRecord -Journal '{work / 'j.jsonl'}' -Path '{outside}' -FileId ({_id_literal(fields)})"))
        self.assert_throws(proc, "ATTRCUDA_OWNED_RECORD_OUTSIDE_JOURNAL_DIRECTORY")
        self.assertFalse((work / "j.jsonl").exists())

    def test_the_view_builder_journals_the_entry_from_its_creating_handle(self) -> None:
        # Journalled from the CREATING handle: if the copy then fails or the job is killed, the entry is
        # already on the record and the journalled sweep can remove it by identity.
        original = self.tmp / "owner" / ("source" + BASE_EXTENSION)
        original.parent.mkdir()
        original.write_bytes(OLD_RECORDING)
        work = self.tmp / "work3"
        view_dir = work / ("owner-" + "clip")
        view_dir.mkdir(parents=True)
        journal = work / ".attrcuda-owned.jsonl"
        script = self.tmp / "view-journal.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{MODULE}' -Force\n"
            f"Import-Module '{OWNER_FOOTAGE_MODULE}' -Force\n"
            f"$pin = Open-AttrCudaReadOnlyHandle -Path '{original}'\n"
            f"$view = New-AttrCudaOwnerFootageView -Directory '{view_dir}' -Index 0 -SourcePath '{original}' -PinStream $pin -Mode copy -Journal '{journal}'\n"
            "Write-Output ('ENTRY=' + $view.EntryId.FileIndexLow)\n"
            "Close-AttrCudaOwnerFootageWorkspace -Handles @($view.ViewStream, $pin) -Views @($view)\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        entry_low = int(next(line for line in proc.stdout.splitlines() if line.startswith("ENTRY="))[len("ENTRY="):])
        record = json.loads(journal.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(record["l"], entry_low)
        self.assertEqual(record["p"], "owner-" + "clip" + "\\" + "owner-" + "clip" + BASE_EXTENSION)
        self.assertEqual(original.read_bytes(), OLD_RECORDING)


@requires_pwsh
class JournalledTreeTests(_Synthetic):
    """B2b / B3 and the realistic legacy workflow, against the owner job's start-of-run sweep."""

    def _sweep(self, root: Path, work: Path, journal: Path) -> subprocess.CompletedProcess:
        return self.run_with_module(
            f"$r = Remove-AttrCudaTree -TrustedRoot '{root}' -Path '{work}' -OwnedJournal '{journal}'\n"
            "Write-Output ('REMOVED=' + $r.Removed)\n"
            "Write-Output ('LEFT=' + (($r.Left | ForEach-Object { $_.Token }) -join ','))\n"
            "Write-Output ('TREE_REMOVED=' + $r.TreeRemoved)\n")

    def _journal(self, journal: Path, *entries: tuple[Path, dict]) -> None:
        body = ""
        for path, fields in entries:
            body += f"Add-AttrCudaOwnedRecord -Journal '{journal}' -Path '{path}' -FileId ({_id_literal(fields)})\n"
        proc = self.run_with_module(body)
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")

    def test_a_legacy_entry_that_became_the_last_name_after_the_owner_rerecorded_is_never_deleted(self) -> None:
        # The realistic path, no adversary: #200-era builds left a neutral-named hard link to the owner's
        # original under $Work\owner-clip. The owner later re-records the original at its own name, so
        # the legacy entry is the LAST name of the old bytes with NumberOfLinks == 1.
        work = self.tmp / "mlvtmp" / "playback-attr-3-cuda-RUN"
        view_dir = work / ("owner-" + "clip")
        legacy = self.make_last_name(view_dir, "owner-" + "clip" + BASE_EXTENSION)
        journal = work / ".attrcuda-owned.jsonl"
        proc = self.run_with_module(
            f"$left = @(Clear-AttrCudaOwnerFootageLeftovers -Directory '{view_dir}' -Journal '{journal}' -WarningAction SilentlyContinue)\n"
            "Write-Output ('CLEAR_LEFT=' + ($left -join ','))\n"
            f"$r = Remove-AttrCudaTree -TrustedRoot '{self.tmp / 'mlvtmp'}' -Path '{work}' -OwnedJournal '{journal}'\n"
            "Write-Output ('TREE_LEFT=' + (($r.Left | ForEach-Object { $_.Token }) -join ','))\n"
            "Write-Output ('TREE_REMOVED=' + $r.TreeRemoved)\n")
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.assertEqual(legacy.read_bytes(), OLD_RECORDING, "the legacy last name was deleted")
        self.assertIn("CLEAR_LEFT=LEFT_LEGACY", proc.stdout)
        self.assertIn("TREE_LEFT=LEFT_UNOWNED", proc.stdout)
        self.assertIn("TREE_REMOVED=False", proc.stdout)
        self.assertTrue(view_dir.is_dir() and work.is_dir(), "the directories above a left entry stay")

    def test_the_journalled_sweep_removes_only_what_the_journal_names_in_a_mixed_tree(self) -> None:
        root = self.tmp / "mlvtmp2"
        work = root / "run"
        (work / "a" / "b").mkdir(parents=True)
        (work / "keep-dir").mkdir()
        mine1 = work / "a" / "b" / "mine.bin"
        mine2 = work / "mine2.bin"
        stranger = work / "keep-dir" / "stranger.bin"
        f1 = self.created_id(mine1)
        f2 = self.created_id(mine2)
        stranger.write_bytes(b"not journalled -- not mine")
        journal = work / ".attrcuda-owned.jsonl"
        self._journal(journal, (mine1, f1), (mine2, f2))
        proc = self._sweep(root, work, journal)
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("REMOVED=2", proc.stdout)
        self.assertIn("LEFT=LEFT_UNOWNED", proc.stdout)
        self.assertFalse(mine1.exists() or mine2.exists())
        self.assertEqual(stranger.read_bytes(), b"not journalled -- not mine")
        self.assertFalse((work / "a").exists(), "an emptied directory goes")
        self.assertTrue(journal.exists(), "the journal stays while anything it describes is left standing")

    def test_a_fully_journalled_tree_is_removed_with_its_journal(self) -> None:
        root = self.tmp / "mlvtmp3"
        work = root / "run"
        (work / "owner-clip").mkdir(parents=True)
        mine = work / "owner-clip" / "x.bin"
        fields = self.created_id(mine)
        journal = work / ".attrcuda-owned.jsonl"
        self._journal(journal, (mine, fields))
        proc = self._sweep(root, work, journal)
        self.assertIn("TREE_REMOVED=True", proc.stdout, f"{proc.stdout}\n{proc.stderr}")
        self.assertFalse(work.exists())

    def test_a_journalled_name_swapped_for_a_hard_link_to_old_footage_is_left(self) -> None:
        root = self.tmp / "mlvtmp4"
        work = root / "run"
        work.mkdir(parents=True)
        entry = work / "view.bin"
        fields = self.created_id(entry)
        journal = work / ".attrcuda-owned.jsonl"
        self._journal(journal, (entry, fields))
        os.remove(entry)
        owner = self.tmp / "owner-original.bin"
        owner.write_bytes(OLD_RECORDING)
        os.link(owner, entry)
        proc = self._sweep(root, work, journal)
        self.assertIn("LEFT=LEFT_ID_MISMATCH", proc.stdout, f"{proc.stdout}\n{proc.stderr}")
        self.assertEqual(entry.read_bytes(), OLD_RECORDING)
        self.assertEqual(owner.read_bytes(), OLD_RECORDING)
        self.assertEqual(os.stat(owner).st_nlink, 2)

    def test_an_absent_tree_is_a_clean_empty_result(self) -> None:
        root = self.tmp / "mlvtmp5"
        root.mkdir()
        proc = self._sweep(root, root / "nothing-here", root / "nothing-here" / "j.jsonl")
        self.assertIn("REMOVED=0", proc.stdout, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("TREE_REMOVED=True", proc.stdout)

    def test_a_linked_ancestor_still_refuses_before_anything_is_touched(self) -> None:
        root = self.tmp / "mlvtmp6"
        real = self.tmp / "real-target"
        real.mkdir()
        (real / "run").mkdir()
        victim = real / "run" / "victim.bin"
        victim.write_bytes(b"outside the trusted root")
        root.mkdir()
        made = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(root / "linked"), str(real)], capture_output=True, text=True)
        self.assertEqual(made.returncode, 0, made.stdout + made.stderr)
        run = self.run_with_module(_guard(
            f"Remove-AttrCudaTree -TrustedRoot '{root}' -Path '{root / 'linked' / 'run'}' "
            f"-OwnedJournal '{root / 'linked' / 'run' / 'j.jsonl'}'"))
        self.assert_throws(run, "ATTRCUDA_ANCESTOR_IS_LINK")
        self.assertEqual(victim.read_bytes(), b"outside the trusted root")


@requires_pwsh
class BuildScratchTreeTests(_Synthetic):
    """The no-journal mode the build-route jobs use: per-entry handle deletes, never Remove-Item -Recurse."""

    def test_a_tree_with_a_read_only_file_and_nested_directories_is_removed(self) -> None:
        root = self.tmp / "scratch-root"
        work = root / ".work-x"
        (work / "a" / "b").mkdir(parents=True)
        locked = work / "a" / "b" / "ro.bin"
        locked.write_bytes(b"x")
        os.chmod(locked, 0o444)
        (work / "g.bin").write_bytes(b"y")
        proc = self.run_with_module(f"Remove-AttrCudaTree -TrustedRoot '{root}' -Path '{work}'\n")
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.assertFalse(work.exists())

    def test_a_file_that_cannot_be_deleted_fails_loudly_and_the_rest_of_the_tree_is_consistent(self) -> None:
        root = self.tmp / "scratch-root2"
        work = root / ".work-y"
        work.mkdir(parents=True)
        held = work / "held.bin"
        held.write_bytes(b"held")
        (work / "other.bin").write_bytes(b"other")
        proc = self.run_with_module(
            f"$h = [IO.File]::Open('{held}', [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)\n"
            + _guard(f"Remove-AttrCudaTree -TrustedRoot '{root}' -Path '{work}'")
            + "$h.Dispose()\n")
        self.assert_throws(proc, "ATTRCUDA_TREE_NOT_EMPTIED")
        self.assertTrue(held.exists())
        self.assertFalse((work / "other.bin").exists())


@requires_pwsh
class LongPathTests(_Synthetic):
    """fable r1 (ATTRCUDA-FILE-ID-LONG-PATH-1): the identity primitives take the extended-length prefix, so
    a scratch tree holding a 260+ character path is still deleted, not refused."""

    EXTENDED = "\\\\?\\"

    def _long_tree(self) -> tuple[Path, Path]:
        root = self.tmp / "long-root"
        deep = root
        while len(str(deep)) < 300:
            deep = deep / ("segment-" + "x" * 24)
        leaf = deep / "payload.bin"
        try:
            os.makedirs(self.EXTENDED + str(deep))
            Path(self.EXTENDED + str(leaf)).write_bytes(b"long path payload")
        except OSError as error:
            self.skipTest(f"this host cannot create a long path: {error}")
        return root, leaf

    def test_the_identity_reader_and_the_tree_delete_handle_a_path_over_259_characters(self) -> None:
        root, leaf = self._long_tree()
        self.assertGreater(len(str(leaf)), 259)
        proc = self.run_with_module(
            f"$id = Get-AttrCudaFileId -Path '{leaf}'\n"
            "Write-Output ('LINKS=' + $id.NumberOfLinks)\n"
            f"Remove-AttrCudaTree -TrustedRoot '{self.tmp}' -Path '{root}'\n"
            "Write-Output 'SWEPT'\n")
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("LINKS=1", proc.stdout)
        self.assertIn("SWEPT", proc.stdout)
        self.assertFalse(os.path.exists(self.EXTENDED + str(root)))


@requires_pwsh
class FileIdentityOverSmbTests(_Synthetic):
    """Caveat (a): the staged-id hand-off assumes the identity read through the share equals the one the
    agent reads locally. Proven here over loopback SMB (the drive's administrative share) wherever the
    host exposes it; skipped where it does not. A mismatch is safe anyway -- the agent job leaves the
    staged copy (LEFT_ID_MISMATCH) -- but it would strand staged bytes on the share."""

    def test_the_identity_off_a_creating_handle_over_smb_equals_the_local_identity(self) -> None:
        local = self.tmp / "smb-identity.bin"
        drive = local.drive
        unc = Path("\\\\localhost\\" + drive[0] + "$" + str(local)[len(drive):])
        try:
            reachable = unc.parent.exists()
        except OSError:
            reachable = False
        if not reachable:
            self.skipTest("the administrative share is not reachable on this host")
        proc = self.run_with_module(
            f"$s = [IO.File]::Open('{unc}', [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)\n"
            "$viaSmb = Get-AttrCudaFileId -Stream $s\n"
            "$s.Dispose()\n"
            f"$local = Get-AttrCudaFileId -Path '{local}'\n"
            "Write-Output ('EQUAL=' + (($viaSmb.VolumeSerialNumber -eq $local.VolumeSerialNumber) -and "
            "($viaSmb.FileIndexHigh -eq $local.FileIndexHigh) -and ($viaSmb.FileIndexLow -eq $local.FileIndexLow)))\n")
        if proc.returncode != 0:
            self.skipTest("could not create a file over the administrative share: " + proc.stderr.strip()[:120])
        self.assertIn("EQUAL=True", proc.stdout, f"{proc.stdout}\n{proc.stderr}")


if __name__ == "__main__":
    unittest.main()
