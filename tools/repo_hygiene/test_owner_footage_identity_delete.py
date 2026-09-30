"""OWNER-FOOTAGE-NO-HARDLINK-1: the identity-checked delete, the file-identity reader and the
scratch-tree refusal, run against SYNTHETIC NTFS folders (never real footage).

The class these pin. A hard link is a second, equal NAME of a file's bytes, so any tool that deletes,
sweeps or truncates a path it believes is job scratch can destroy the owner's footage through it --
four review rounds on PR #200 / #203 found that same class in a new place each time (the assembler's
recursive scratch delete, the staging job's partial and staged-copy deletes, lane scratch sweeps, a
write through a leftover link). The hub ruling removed the class by changing the mechanism: no code
creates a hard link to owner footage (tools/repo_hygiene/test_owner_footage_no_hardlink_class.py is
that guard), views are symbolic links or verified copies, and every delete of a name this repository
made goes through Remove-AttrCudaFileById, which checks the file object's identity and link count on
the very handle that deletes.

The hard links these tests create are the TEST FIXTURE for the hostile state (another process swapped
a pathname for a hard link to a file that matters); no tracked non-test source may create one.

Red-first on fork/master (11c346e5): Remove-AttrCudaTree deleted a tree that held a second name of a
file (the owner's synthetic original survived only because its own name did), and there was no
identity-checked delete at all -- Remove-Item on a swapped partial path removed whatever name the path
then meant.
"""

from __future__ import annotations

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

PAYLOAD = b"synthetic owner bytes -- never real footage " * 32


def _id_literal(fields: dict) -> str:
    return (
        "[pscustomobject]@{ "
        f"VolumeSerialNumber = [uint32]{fields['VolumeSerialNumber']}; "
        f"FileIndexHigh = [uint32]{fields['FileIndexHigh']}; "
        f"FileIndexLow = [uint32]{fields['FileIndexLow']} }}"
    )


@requires_pwsh
class IdentityCheckedDeleteTests(_PwshCase):
    def _create_recorded(self, path: Path, payload: bytes = b"a job-made partial") -> dict:
        """Create `path` with CreateNew like the jobs do and return the identity read off that handle."""
        proc = self.run_with_module(
            f"$s = [IO.File]::Open('{path}', [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)\n"
            f"$id = Get-AttrCudaFileId -Stream $s\n"
            f"$bytes = [Text.Encoding]::ASCII.GetBytes('{payload.decode('ascii')}')\n"
            "$s.Write($bytes, 0, $bytes.Length); $s.Dispose()\n"
            "[pscustomobject]@{ VolumeSerialNumber = $id.VolumeSerialNumber; FileIndexHigh = $id.FileIndexHigh; "
            "FileIndexLow = $id.FileIndexLow; NumberOfLinks = $id.NumberOfLinks } | ConvertTo-Json -Compress\n",
            name="create-recorded.ps1",
        )
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        fields = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(fields["NumberOfLinks"], 1)
        return fields

    def _remove(self, path: Path, fields: dict, extra: str = "") -> str:
        proc = self.run_with_module(
            f"Write-Output ('TOKEN=' + (Remove-AttrCudaFileById -Path '{path}' -FileId ({_id_literal(fields)}){extra}))\n",
            name="remove-by-id.ps1",
        )
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        return next(line for line in proc.stdout.splitlines() if line.startswith("TOKEN="))[len("TOKEN="):]

    def test_a_lone_file_that_is_still_the_recorded_object_is_deleted(self) -> None:
        partial = self.tmp / "job.partial"
        fields = self._create_recorded(partial)
        self.assertEqual(self._remove(partial, fields), "DELETED")
        self.assertFalse(partial.exists())

    def test_an_absent_name_is_reported_absent(self) -> None:
        fields = {"VolumeSerialNumber": 1, "FileIndexHigh": 2, "FileIndexLow": 3}
        self.assertEqual(self._remove(self.tmp / "never-created.partial", fields), "ABSENT")

    def test_a_name_swapped_for_a_hard_link_to_the_owners_file_is_left_and_both_names_survive(self) -> None:
        # sol r2 repro 2: a job-made partial's pathname is replaced -- after the job verified and
        # recorded it -- by a hard link to the owner's original. A path-based delete removes one name
        # of the OWNER's bytes; the identity check sees a different file object and leaves it.
        owner_original = self.tmp / "owner-original.bin"
        owner_original.write_bytes(PAYLOAD)
        partial = self.tmp / "job.partial"
        fields = self._create_recorded(partial)
        os.remove(partial)
        os.link(owner_original, partial)

        self.assertEqual(self._remove(partial, fields), "LEFT_ID_MISMATCH")

        self.assertEqual(owner_original.read_bytes(), PAYLOAD)
        self.assertEqual(partial.read_bytes(), PAYLOAD)
        self.assertEqual(os.stat(owner_original).st_nlink, 2)

    def test_a_recorded_object_that_gained_a_second_name_is_left(self) -> None:
        # Same file object, but it is no longer the only name of its bytes: deleting this name could
        # be deleting one of the last, so it is left.
        partial = self.tmp / "job.partial"
        fields = self._create_recorded(partial, b"a partial with a second name")
        other_name = self.tmp / "the-second-name"
        os.link(partial, other_name)

        self.assertEqual(self._remove(partial, fields), "LEFT_LINKED")

        self.assertEqual(partial.read_bytes(), b"a partial with a second name")
        self.assertEqual(other_name.read_bytes(), b"a partial with a second name")

    def test_a_directory_is_never_deleted(self) -> None:
        directory = self.tmp / "a-directory"
        directory.mkdir()
        (directory / "keep.txt").write_bytes(b"keep")
        fields = {"VolumeSerialNumber": 1, "FileIndexHigh": 2, "FileIndexLow": 3}
        self.assertEqual(self._remove(directory, fields), "LEFT_NOT_A_FILE")
        self.assertEqual((directory / "keep.txt").read_bytes(), b"keep")

    def test_a_name_someone_else_holds_open_is_left_and_the_call_does_not_throw(self) -> None:
        partial = self.tmp / "held.partial"
        fields = self._create_recorded(partial)
        proc = self.run_with_module(
            f"$held = [IO.File]::Open('{partial}', [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)\n"
            f"Write-Output ('TOKEN=' + (Remove-AttrCudaFileById -Path '{partial}' -FileId ({_id_literal(fields)})))\n"
            "$held.Dispose()\n",
            name="held-open.ps1",
        )
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("TOKEN=LEFT_UNAVAILABLE", proc.stdout)
        self.assertTrue(partial.exists())

    def test_a_path_that_cannot_be_opened_never_throws(self) -> None:
        fields = {"VolumeSerialNumber": 1, "FileIndexHigh": 2, "FileIndexLow": 3}
        token = self._remove(Path("Q:/no/such/volume/at/all.partial"), fields)
        self.assertIn(token, {"ABSENT", "LEFT_UNAVAILABLE"})


@requires_pwsh
class FileIdentityReaderTests(_PwshCase):
    def test_the_link_count_is_live_and_a_stream_reads_the_same_object_as_its_path(self) -> None:
        original = self.tmp / "original.bin"
        original.write_bytes(PAYLOAD)
        proc = self.run_with_module(
            f"$before = Get-AttrCudaFileId -Path '{original}'\n"
            f"$s = [IO.File]::OpenRead('{original}')\n"
            "$fromStream = Get-AttrCudaFileId -Stream $s\n"
            "$s.Dispose()\n"
            "Write-Output ('LINKS_BEFORE=' + $before.NumberOfLinks)\n"
            "Write-Output ('STREAM_SAME_AS_PATH=' + (($before.VolumeSerialNumber -eq $fromStream.VolumeSerialNumber) -and ($before.FileIndexHigh -eq $fromStream.FileIndexHigh) -and ($before.FileIndexLow -eq $fromStream.FileIndexLow)))\n"
            "Write-Output ('IS_DIRECTORY=' + $before.IsDirectory)\n"
            "Write-Output ('IS_REPARSE=' + $before.IsReparsePoint)\n",
        )
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("LINKS_BEFORE=1", proc.stdout)
        self.assertIn("STREAM_SAME_AS_PATH=True", proc.stdout)
        self.assertIn("IS_DIRECTORY=False", proc.stdout)
        self.assertIn("IS_REPARSE=False", proc.stdout)
        os.link(original, self.tmp / "second-name.bin")
        again = self.run_with_module(f"Write-Output ('LINKS_AFTER=' + (Get-AttrCudaFileId -Path '{original}').NumberOfLinks)\n")
        self.assertIn("LINKS_AFTER=2", again.stdout)

    def test_exactly_one_of_path_or_stream_is_required(self) -> None:
        proc = self.run_with_module(_guard("Get-AttrCudaFileId"))
        self.assert_throws(proc, "ATTRCUDA_FILE_ID_UNAVAILABLE")

    def test_an_unreadable_path_throws_a_message_that_does_not_echo_the_path(self) -> None:
        missing = self.tmp / "sentinel-missing-path-0001"
        proc = self.run_with_module(_guard(f"Get-AttrCudaFileId -Path '{missing}'"))
        self.assert_throws(proc, "ATTRCUDA_FILE_ID_UNAVAILABLE")
        self.assertNotIn("sentinel-missing-path-0001", proc.stdout)


@requires_pwsh
class ScratchTreeRefusesASecondNameTests(_PwshCase):
    def test_a_tree_holding_a_second_name_of_a_file_is_refused_whole_and_both_names_survive(self) -> None:
        # sol r2 repro 1 (the assembler's recursive scratch delete over a tree that holds a name of
        # the owner's bytes): the tree delete removed that name on fork/master. Now it refuses.
        owner_original = self.tmp / "owner" / "original.bin"
        owner_original.parent.mkdir()
        owner_original.write_bytes(PAYLOAD)
        scratch = self.tmp / "scratch" / ".work-abc"
        (scratch / "owner-clip").mkdir(parents=True)
        inside = scratch / "owner-clip" / "a-name-of-the-owners-bytes.bin"
        os.link(owner_original, inside)

        proc = self.run_with_module(_guard(f"Remove-AttrCudaTree -TrustedRoot '{self.tmp / 'scratch'}' -Path '{scratch}'"))

        self.assert_throws(proc, "ATTRCUDA_TREE_HAS_HARD_LINK")
        self.assertEqual(owner_original.read_bytes(), PAYLOAD)
        self.assertEqual(inside.read_bytes(), PAYLOAD)

    def test_a_tree_of_lone_files_is_still_removed(self) -> None:
        scratch = self.tmp / "scratch" / ".work-lone"
        (scratch / "a" / "b").mkdir(parents=True)
        (scratch / "a" / "b" / "f.bin").write_bytes(b"x")
        (scratch / "g.bin").write_bytes(b"y")
        proc = self.run_with_module(f"Remove-AttrCudaTree -TrustedRoot '{self.tmp / 'scratch'}' -Path '{scratch}'\n")
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.assertFalse(scratch.exists())

    def test_a_copy_view_directory_survives_a_recursive_sweep_with_the_original_intact(self) -> None:
        # The view directory the job builds (copy mode) holds a separate file, so the sweep that
        # used to be able to reach the owner's bytes through a hard link now reaches only a copy.
        base_extension = "." + "MLV"
        original = self.tmp / "owner" / ("source" + base_extension)
        original.parent.mkdir()
        original.write_bytes(PAYLOAD)
        work = self.tmp / "scratch" / ".work-view"
        work.mkdir(parents=True)
        view_dir = work / "owner-clip"
        view_dir.mkdir()
        script = self.tmp / "copy-view-then-sweep.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{MODULE}' -Force\n"
            f"Import-Module '{OWNER_FOOTAGE_MODULE}' -Force\n"
            f"$pin = Open-AttrCudaReadOnlyHandle -Path '{original}'\n"
            f"$view = New-AttrCudaOwnerFootageView -Directory '{view_dir}' -Index 0 -SourcePath '{original}' -PinStream $pin -Mode copy\n"
            "Close-AttrCudaOwnerFootageWorkspace -Handles @($view.ViewStream, $pin) -Views @()\n"
            # The view entry is left in place on purpose (-Views @()): the sweep must cope with it.
            f"Remove-AttrCudaTree -TrustedRoot '{self.tmp / 'scratch'}' -Path '{work}'\n"
            "Write-Output 'SWEPT'\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("SWEPT", proc.stdout)
        self.assertFalse(work.exists())
        self.assertEqual(original.read_bytes(), PAYLOAD)
        self.assertEqual(os.stat(original).st_nlink, 1)


@requires_pwsh
class SymlinkCapabilityProbeTests(_PwshCase):
    def _probe(self, directory: Path, mock: str = "") -> subprocess.CompletedProcess:
        script = self.tmp / "capability-probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{MODULE}' -Force\n"
            f"Import-Module '{OWNER_FOOTAGE_MODULE}' -Force\n"
            "$mod = Get-Module AttrCudaOwnerFootage\n"
            + mock
            + f"$r = Test-AttrCudaSymlinkCapability -Directory '{directory}'\n"
            "Write-Output ('RESULT=' + $r.Result)\n"
            "Write-Output ('CAPABLE=' + $r.Capable)\n",
            encoding="utf-8",
        )
        return _run_pwsh_file(script)

    def test_a_venue_that_cannot_create_a_symlink_gets_the_typed_unavailable_result_and_no_litter(self) -> None:
        directory = self.tmp / "probe-dir"
        directory.mkdir()
        proc = self._probe(directory, mock=(
            "& $mod { Set-Item -Path function:New-AttrCudaFileSymlink -Value {\n"
            "    param([string]$LinkPath, [string]$TargetPath)\n"
            "    throw 'Administrator privilege required for this operation.'\n"
            "} }\n"))
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("RESULT=SYMLINK_UNAVAILABLE", proc.stdout)
        self.assertIn("CAPABLE=False", proc.stdout)
        self.assertEqual(list(directory.iterdir()), [])

    def test_a_link_that_is_not_really_a_link_is_not_reported_capable_and_leaves_nothing(self) -> None:
        # A filesystem that "succeeds" but hands back a plain file must not read as capable.
        directory = self.tmp / "probe-dir-fake"
        directory.mkdir()
        proc = self._probe(directory, mock=(
            "& $mod { Set-Item -Path function:New-AttrCudaFileSymlink -Value {\n"
            "    param([string]$LinkPath, [string]$TargetPath)\n"
            "    [IO.File]::WriteAllBytes($LinkPath, [byte[]](1))\n"
            "} }\n"))
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("RESULT=SYMLINK_UNAVAILABLE", proc.stdout)
        self.assertEqual(list(directory.iterdir()), [])

    def test_the_real_probe_returns_one_of_the_two_typed_results_and_leaves_nothing(self) -> None:
        directory = self.tmp / "probe-dir-real"
        directory.mkdir()
        proc = self._probe(directory)
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.assertRegex(proc.stdout, r"RESULT=(SYMLINK_CAPABLE|SYMLINK_UNAVAILABLE)")
        self.assertEqual(list(directory.iterdir()), [])
        capable = "RESULT=SYMLINK_CAPABLE" in proc.stdout
        self.assertEqual("CAPABLE=True" in proc.stdout, capable)


if __name__ == "__main__":
    unittest.main()
