"""UM-OWNER-FOOTAGE-CROSS-VOLUME-1: the bounded owner-footage bind proof job
(tools/profiling/bachelor/Attr3FootageBindProofJob.psm1, emitted by the presence CLI's -BindProof).

Every row uses SYNTHETIC parts written under a temp directory (a ``.raw`` stem plus the neutral
naming's own extension, composed, never the real clip), built through the module's function
directly -- the real CLI's only path to parts is tools/gates/resolve_consented_clip.py. The emitted
job is then RUN locally: it must link, hold, hash once, build the binding, prove the write block,
release the handles and leave every owner part untouched, saying nothing that names an owner path.
UM-OWNER-FOOTAGE-CROSS-VOLUME-2: the job deletes NO link name (a "read the link count, then delete"
cleanup can remove the last name of the clip when the owner's source name is replaced between the
two); the directory it leaves is reported in integrity.leftover. A row proves the job refuses a
part whose bytes differ from the baked length/sha256, and one replaces the source name mid-job.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACHELOR = ROOT / "tools" / "profiling" / "bachelor"
PWSH = shutil.which("pwsh")
TOKEN = "ZZBINDPROOFTESTTOKENZZ"


class _BindProofHarness(unittest.TestCase):
    """Shared scaffolding: a temp tree, the emit step and the synthetic parts. Holds no rows."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="attr3bindproof-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.out = self.tmp / "out"
        self.out.mkdir()
        self.work_root = self.tmp / "work-root"
        self.work_root.mkdir()

    def _emit_raw(self, parts, work_root) -> subprocess.CompletedProcess:
        script = self.tmp / "emit.ps1"
        parts_json = json.dumps(parts).replace("'", "''")
        work_root = str(work_root).replace("'", "''")
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{BACHELOR / 'AttrCudaArtifacts.psm1'}' -Force\n"
            f"Import-Module '{BACHELOR / 'Attr3FootageBindProofJob.psm1'}' -Force\n"
            f"$parts = @('{parts_json}' | ConvertFrom-Json)\n"
            f"$r = New-Attr3FootageBindProofJob -ClipId 'SYN-BINDPROOF-0001' -Parts $parts -OutDir '{self.out}' -WorkRoot '{work_root}'\n"
            "Write-Output ('JOBFILE=' + $r.jobFile)\n",
            encoding="utf-8",
        )
        return subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script)],
                              capture_output=True, text=True)

    def _emit(self, parts, work_root=None) -> Path:
        proc = self._emit_raw(parts, self.work_root if work_root is None else work_root)
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        line = next(l for l in proc.stdout.splitlines() if l.startswith("JOBFILE="))
        return Path(line[len("JOBFILE="):])

    def _emit_many(self, parts, work_roots) -> list:
        """Try each work root in ONE pwsh process (a spawn is ~1.5 s on windows-latest); returns
        one ``("EMITTED", "")`` or ``("REFUSED", <message>)`` per input, in order."""
        script = self.tmp / "emit-many.ps1"
        parts_json = json.dumps(parts).replace("'", "''")
        roots_b64 = base64.b64encode(json.dumps(list(map(str, work_roots))).encode("utf-8")).decode("ascii")
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{BACHELOR / 'AttrCudaArtifacts.psm1'}' -Force\n"
            f"Import-Module '{BACHELOR / 'Attr3FootageBindProofJob.psm1'}' -Force\n"
            f"$parts = @('{parts_json}' | ConvertFrom-Json)\n"
            f"$roots = @([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{roots_b64}')) | ConvertFrom-Json)\n"
            "for ($i = 0; $i -lt $roots.Count; $i++) {\n"
            "    try {\n"
            f"        [void](New-Attr3FootageBindProofJob -ClipId 'SYN-BINDPROOF-0001' -Parts $parts -OutDir '{self.out}' -WorkRoot ([string]$roots[$i]))\n"
            "        Write-Output \"EMITTED$i=\"\n"
            "    } catch { Write-Output (\"REFUSED$i=\" + $_.Exception.Message) }\n"
            "}\n",
            encoding="utf-8",
        )
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script)],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        results = {}
        for line in proc.stdout.splitlines():
            for kind in ("EMITTED", "REFUSED"):
                if line.startswith(kind):
                    index, _, message = line[len(kind):].partition("=")
                    results[int(index)] = (kind, message)
        self.assertEqual(sorted(results), list(range(len(work_roots))), proc.stdout)
        return [results[i] for i in range(len(work_roots))]

    def _run(self, job: Path) -> subprocess.CompletedProcess:
        return subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(job)],
                              capture_output=True, text=True)

    def _parts(self, payloads):
        src_dir = self.tmp / f"{TOKEN}-source"
        src_dir.mkdir()
        parts, paths = [], []
        for index, payload in enumerate(payloads):
            extension = ("." + "MLV") if index == 0 else ("." + "M{0:02d}".format(index - 1))
            path = src_dir / ("source" + extension)
            path.write_bytes(payload)
            paths.append(path)
            parts.append({"index": index, "path": str(path).replace("\\", "/"), "length": len(payload),
                          "sha256": hashlib.sha256(payload).hexdigest()})
        return parts, paths


@unittest.skipIf(PWSH is None, "pwsh is not on PATH")
@unittest.skipUnless(os.name == "nt", "the emitted job targets a Windows measurement host")
class FootageBindProofJobTests(_BindProofHarness):
    def test_the_job_proves_link_hold_one_read_and_release_without_naming_a_path(self) -> None:
        payloads = [b"bind proof part zero " * 300, b"bind proof part one " * 300]
        parts, paths = self._parts(payloads)
        proc = self._run(self._emit(parts))

        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("RESULT=OWNER_FOOTAGE_BIND_PROVEN", proc.stdout)
        self.assertNotIn(TOKEN, proc.stdout + proc.stderr)
        payload_line = next(l for l in proc.stdout.splitlines() if '"mlvapp.attr3-footage-bind-proof.v1"' in l or "mlvapp.attr3-footage-bind-proof.v1" in l)
        proof = json.loads(payload_line)["proof"]
        self.assertEqual(proof["partCount"], 2)
        self.assertEqual(proof["hashReads"], 2)  # exactly one full read per part
        self.assertTrue(proof["linkOnSourceVolume"])
        self.assertTrue(proof["bindingMatches"])
        self.assertTrue(proof["writeBlockedWhileHeld"])
        self.assertTrue(proof["sourcesIntact"])
        integrity = json.loads(payload_line)["integrity"]
        self.assertTrue(integrity["sourcesIntact"])
        # No name of the owner's footage was deleted: each part keeps its own name AND the job's
        # private link, and the directory holding the links is recorded for a later sweep.
        for path, payload in zip(paths, payloads):
            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual(os.stat(path).st_nlink, 2)
        leftover = integrity["leftover"]
        self.assertEqual(leftover["schema"], "mlvapp.owner-link-leftover.v1")
        self.assertTrue(leftover["leftover"])
        link_dir = Path(leftover["linkDirectory"])
        self.assertTrue(link_dir.is_dir())
        self.assertEqual(len(leftover["linkNames"]), 2)
        for name, path in zip(leftover["linkNames"], paths):
            self.assertTrue(os.path.samefile(link_dir / name, path))
        self.assertTrue(integrity["workTreeLeftover"])

    def test_a_single_part_clip_is_proven_too(self) -> None:
        # The real clip is one part: a one-element result must not be indexed as a dictionary.
        payload = b"bind proof single part " * 300
        parts, paths = self._parts([payload])
        proc = self._run(self._emit(parts))

        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("RESULT=OWNER_FOOTAGE_BIND_PROVEN", proc.stdout)
        self.assertEqual(os.stat(paths[0]).st_nlink, 2)
        self.assertEqual(paths[0].read_bytes(), payload)

    def test_a_part_whose_bytes_differ_is_refused_and_nothing_is_left_behind(self) -> None:
        payload = b"bind proof mismatch " * 300
        parts, paths = self._parts([payload])
        # Same length, different bytes: passes the cheap screen, fails the one identity hash.
        paths[0].write_bytes(b"X" * len(payload))
        proc = self._run(self._emit(parts))

        self.assertEqual(proc.returncode, 19, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("RESULT=OWNER_FOOTAGE_NOT_VERIFIED", proc.stdout)
        self.assertNotIn(TOKEN, proc.stdout + proc.stderr)
        # The owner's part is untouched; the link the job made before the hash refused it stays.
        self.assertEqual(paths[0].read_bytes(), b"X" * len(payload))
        self.assertEqual(os.stat(paths[0]).st_nlink, 2)

    def test_success_is_gated_on_every_reported_invariant(self) -> None:
        # sol r1 hardening: bindingMatches, hashReads and the cleanup fields are reported AND
        # enforced -- a proof that reports one of them false must not exit 0 / say PROVEN.
        text = (BACHELOR / "Attr3FootageBindProofJob.psm1").read_text(encoding="utf-8")
        success_at = text.index("    $exitCode = 0\n} catch {")
        for gate in (
            "if (-not $bindingMatches) { $token = 'OWNER_FOOTAGE_BIND_MISMATCH'; $exitCode = 24; throw $token }",
            "if ($hashReads -ne $verified.Count) { $token = 'OWNER_FOOTAGE_BIND_HASH_READS'; $exitCode = 25; throw $token }",
        ):
            self.assertIn(gate, text)
            self.assertLess(text.index(gate), success_at, gate)
        # UM-OWNER-FOOTAGE-CROSS-VOLUME-2: the gate is the real safety property -- every owner part
        # STILL EXISTS under its own name with the identity it had before the job -- not whether a
        # cleanup managed to delete the job's own links.
        source_gate = "if ($exitCode -eq 0 -and -not $sourcesIntact) { $token = 'OWNER_FOOTAGE_BIND_SOURCE_NOT_INTACT'; $exitCode = 26 }"
        self.assertIn(source_gate, text)
        self.assertLess(text.index(source_gate), text.index("$result = if ($exitCode -eq 0)"))
        self.assertNotIn("linkDirectoryRemoved", text)
        self.assertNotIn("OWNER_FOOTAGE_BIND_CLEANUP_INCOMPLETE", text)

    def test_replacing_the_owners_source_name_mid_job_loses_no_name_and_fails_the_proof(self) -> None:
        # Item 4a at job level. A decoy is renamed over the owner's source name AFTER the links
        # exist and the hash is done, before the job's finally runs (the NTFS premise of PR #200's
        # own swap test). The job must delete nothing -- the original bytes keep the private link --
        # and, because the owner's source name is no longer the file it was, must NOT say PROVEN.
        payload = b"bind proof swap mid job " * 300
        parts, paths = self._parts([payload])
        decoy = self.tmp / "decoy.bin"
        decoy_bytes = b"a different file that replaces the source name"
        decoy.write_bytes(decoy_bytes)
        job = self._emit(parts)
        text = job.read_text(encoding="utf-8")
        marker = "    $binding = New-AttrCudaVerifiedClipBinding"
        self.assertEqual(text.count(marker), 1)
        swap = f"    [IO.File]::Move('{decoy}', '{paths[0]}', $true)\r\n"
        job.write_text(text.replace(marker, swap + marker), encoding="utf-8")

        proc = self._run(job)

        self.assertEqual(proc.returncode, 26, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("RESULT=OWNER_FOOTAGE_BIND_SOURCE_NOT_INTACT", proc.stdout)
        self.assertEqual(paths[0].read_bytes(), decoy_bytes)
        result = json.loads(next(l for l in proc.stdout.splitlines() if "mlvapp.attr3-footage-bind-proof.v1" in l))
        self.assertFalse(result["integrity"]["sourcesIntact"])
        link_dir = Path(result["integrity"]["leftover"]["linkDirectory"])
        survivors = [p for p in link_dir.iterdir()]
        self.assertEqual(len(survivors), 1)
        self.assertEqual(survivors[0].read_bytes(), payload)  # the last name of the original bytes

    def test_the_leftover_record_is_on_disk_before_the_first_link_exists(self) -> None:
        # sol r1 + fable r1 hardening: the proof used to keep its leftover record in memory and emit it
        # only in its final JSON, so a proof killed after the first link left a link directory that no
        # record named. The record is persisted BEFORE the first link, like the attribution job's
        # owner-link-directory.json. The job is ended hard (no finally, no final output) right after
        # the first link is created.
        payload = b"bind proof killed after the first link " * 300
        parts, paths = self._parts([payload])
        job = self._emit(parts)
        text = job.read_text(encoding="utf-8")
        marker = "        $handle = Open-AttrCudaReadOnlyHandle -Path $linkPath"
        self.assertEqual(text.count(marker), 1)
        job.write_text(text.replace(marker, "        [Environment]::Exit(97)\r\n" + marker), encoding="utf-8")

        proc = self._run(job)

        self.assertEqual(proc.returncode, 97, f"{proc.stdout}\n{proc.stderr}")
        self.assertNotIn("RESULT=", proc.stdout)
        work_dirs = [p for p in self.work_root.iterdir() if p.is_dir()]
        self.assertEqual(len(work_dirs), 1)
        record = json.loads((work_dirs[0] / "owner-link-directory.json").read_text(encoding="utf-8"))
        self.assertEqual(record["schema"], "mlvapp.owner-link-leftover.v1")
        self.assertTrue(record["leftover"])
        link_dir = Path(record["linkDirectory"])
        self.assertEqual(len(record["linkNames"]), 1)
        self.assertTrue(os.path.samefile(link_dir / record["linkNames"][0], paths[0]))
        self.assertEqual(os.stat(paths[0]).st_nlink, 2)

    def test_a_record_that_cannot_be_written_stops_the_proof_before_any_link_exists(self) -> None:
        payload = b"bind proof unwritable record " * 300
        parts, paths = self._parts([payload])
        job = self._emit(parts)
        text = job.read_text(encoding="utf-8")
        target = "(Join-Path $work 'owner-link-directory.json')"
        self.assertEqual(text.count(target), 1)
        job.write_text(text.replace(target, "'Z:\\no-such-directory\\record.json'"), encoding="utf-8")

        proc = self._run(job)

        self.assertEqual(proc.returncode, 22, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("RESULT=OWNER_FOOTAGE_LINK_FAILED", proc.stdout)
        self.assertEqual(os.stat(paths[0]).st_nlink, 1, "a link was made although its record could not be written")
        self.assertEqual(paths[0].read_bytes(), payload)

    def test_a_work_directory_that_already_exists_is_refused_and_left_untouched(self) -> None:
        # r2: the proof's work directory is created create-new. The pre-r2 job took an existing one
        # (New-Item -Force) and carried on into it; it is now refused and nothing in it is touched.
        payload = b"bind proof existing work directory " * 300
        parts, paths = self._parts([payload])
        job = self._emit(parts)
        job_id = job.name[: -len(".job.ps1")]
        work = self.work_root / job_id
        work.mkdir()
        retained = work / ("owner-" + "clip" + ".saved")
        os.link(paths[0], retained)

        proc = self._run(job)

        self.assertEqual(proc.returncode, 28, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("RESULT=WORK_DIRECTORY_EXISTS", proc.stdout)
        self.assertTrue(retained.exists(), "a name of owner footage was deleted")
        self.assertEqual(os.stat(paths[0]).st_nlink, 2)
        self.assertEqual(sorted(p.name for p in work.iterdir()), [retained.name])


def _short_path(path: Path) -> str:
    """The 8.3 spelling of ``path`` (every segment that has an alias), or the path unchanged."""
    import ctypes

    buf = ctypes.create_unicode_buffer(1024)
    n = ctypes.windll.kernel32.GetShortPathNameW(str(path), buf, 1024)
    return buf.value if 0 < n < 1024 else str(path)


@unittest.skipIf(PWSH is None, "pwsh is not on PATH")
@unittest.skipUnless(os.name == "nt", "the emitted job targets a Windows measurement host")
class FootageBindProofWorkRootTests(_BindProofHarness):
    """-WorkRoot is resolved to its long path BEFORE it is validated (round 2b).

    A hosted runner's temp directory is spelled C:\\Users\\RUNNER~1\\..., and a validator on the raw
    argument refused it, reddening three bind-proof rows on windows-latest.
    """

    def _work_root_literal(self, job: Path) -> str:
        line = next(l for l in job.read_text(encoding="utf-8").splitlines() if l.startswith("$WorkRoot = '"))
        return line[len("$WorkRoot = '"):-1]

    def test_an_8dot3_short_work_root_is_expanded_and_accepted(self) -> None:
        short = _short_path(self.work_root)
        if "~" not in short:
            self.skipTest("8.3 short names are not generated on this volume")
        parts, _ = self._parts([b"short work root " * 300])
        job = self._emit(parts, work_root=short)

        literal = self._work_root_literal(job)
        self.assertNotIn("~", literal)
        self.assertEqual(os.path.normcase(literal), os.path.normcase(os.path.realpath(self.work_root)))
        self.assertEqual(self._run(job).returncode, 0)
        # The job ran under the resolved root; its work tree stays (it holds the job's trace and
        # the private link directory -- no job deletes a link name).
        self.assertEqual(len(list(self.work_root.iterdir())), 1)

    def test_a_not_yet_existing_directory_under_a_short_ancestor_is_accepted(self) -> None:
        short = _short_path(self.work_root)
        if "~" not in short:
            self.skipTest("8.3 short names are not generated on this volume")
        parts, _ = self._parts([b"short ancestor " * 300])
        job = self._emit(parts, work_root=short + "\\not-yet-created")

        literal = self._work_root_literal(job)
        self.assertNotIn("~", literal)
        self.assertTrue(literal.lower().endswith("\\work-root\\not-yet-created"), literal)

    def test_a_dotdot_segment_is_refused_even_when_it_resolves_inside_the_root(self) -> None:
        parts, _ = self._parts([b"traversal " * 300])
        raws = [
            f"{self.work_root}\\..\\work-root",
            f"{self.work_root}/../work-root",
            f"{_short_path(self.work_root)}\\..\\work-root",
            f"{self.tmp}\\..\\{self.tmp.name}\\work-root",
        ]
        for raw, (kind, message) in zip(raws, self._emit_many(parts, raws)):
            self.assertEqual(kind, "REFUSED", raw)
            self.assertIn("ATTR3_BINDPROOF_WORKROOT_TRAVERSAL", message, raw)
        self.assertEqual(list(self.out.iterdir()), [])

    def test_a_work_root_that_is_not_a_local_drive_path_is_refused(self) -> None:
        parts, _ = self._parts([b"not a drive path " * 300])
        raws = ["work-root", "\\\\server\\share\\work-root", "\\\\?\\C:\\work-root", "C:work-root"]
        for raw, (kind, message) in zip(raws, self._emit_many(parts, raws)):
            self.assertEqual(kind, "REFUSED", raw)
            self.assertIn("ATTR3_BINDPROOF_WORKROOT_INVALID", message, raw)

    def test_a_character_outside_the_allowed_set_is_refused_after_resolution(self) -> None:
        # The emitted job embeds the value in a single-quoted literal: a quote or a metacharacter
        # must never survive the resolution, whatever spelling it arrived in.
        parts, _ = self._parts([b"bad character " * 300])
        raws = [f"{self.work_root}\\{leaf}" for leaf in ("evil'root", "evil;root", "evil$root", "evil~root")]
        for raw, (kind, message) in zip(raws, self._emit_many(parts, raws)):
            self.assertEqual(kind, "REFUSED", raw)
            self.assertIn("ATTR3_BINDPROOF_WORKROOT_INVALID", message, raw)
        self.assertEqual(list(self.out.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
