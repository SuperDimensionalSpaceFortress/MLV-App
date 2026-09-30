"""UM-OWNER-FOOTAGE-CROSS-VOLUME-1: the bounded owner-footage bind proof job
(tools/profiling/bachelor/Attr3FootageBindProofJob.psm1, emitted by the presence CLI's -BindProof).

Every row uses SYNTHETIC parts written under a temp directory (a ``.raw`` stem plus the neutral
naming's own extension, composed, never the real clip), built through the module's function
directly -- the real CLI's only path to parts is tools/gates/resolve_consented_clip.py. The emitted
job is then RUN locally: it must link, hold, hash once, build the binding, prove the write block,
release everything and leave the source with a single name, saying nothing that names a path.
A second row proves the job refuses a part whose bytes differ from the baked length/sha256.
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
        self.assertEqual(proof["sourceLinkCountAfterCleanup"], 1)
        self.assertTrue(proof["workTreeRemoved"])
        self.assertEqual(list(self.work_root.iterdir()), [])
        for path, payload in zip(paths, payloads):
            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual(os.stat(path).st_nlink, 1)

    def test_a_single_part_clip_is_proven_too(self) -> None:
        # The real clip is one part: a one-element result must not be indexed as a dictionary.
        payload = b"bind proof single part " * 300
        parts, paths = self._parts([payload])
        proc = self._run(self._emit(parts))

        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("RESULT=OWNER_FOOTAGE_BIND_PROVEN", proc.stdout)
        self.assertEqual(os.stat(paths[0]).st_nlink, 1)
        self.assertEqual(list(self.work_root.iterdir()), [])

    def test_a_part_whose_bytes_differ_is_refused_and_nothing_is_left_behind(self) -> None:
        payload = b"bind proof mismatch " * 300
        parts, paths = self._parts([payload])
        # Same length, different bytes: passes the cheap screen, fails the one identity hash.
        paths[0].write_bytes(b"X" * len(payload))
        proc = self._run(self._emit(parts))

        self.assertEqual(proc.returncode, 19, f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("RESULT=OWNER_FOOTAGE_NOT_VERIFIED", proc.stdout)
        self.assertNotIn(TOKEN, proc.stdout + proc.stderr)
        self.assertEqual(list(self.work_root.iterdir()), [])
        self.assertEqual(os.stat(paths[0]).st_nlink, 1)

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
        cleanup_gate = "if ($exitCode -eq 0 -and ($linksAfter -ne 1 -or -not $workGone -or -not $linkDirGone))"
        self.assertIn(cleanup_gate, text)
        self.assertLess(text.index(cleanup_gate), text.index("$result = if ($exitCode -eq 0)"))


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
        self.assertEqual(list(self.work_root.iterdir()), [])

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
