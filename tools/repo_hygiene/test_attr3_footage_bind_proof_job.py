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


@unittest.skipIf(PWSH is None, "pwsh is not on PATH")
@unittest.skipUnless(os.name == "nt", "the emitted job targets a Windows measurement host")
class FootageBindProofJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="attr3bindproof-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.out = self.tmp / "out"
        self.out.mkdir()
        self.work_root = self.tmp / "work-root"
        self.work_root.mkdir()

    def _emit(self, parts) -> Path:
        script = self.tmp / "emit.ps1"
        parts_json = json.dumps(parts).replace("'", "''")
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{BACHELOR / 'AttrCudaArtifacts.psm1'}' -Force\n"
            f"Import-Module '{BACHELOR / 'Attr3FootageBindProofJob.psm1'}' -Force\n"
            f"$parts = @('{parts_json}' | ConvertFrom-Json)\n"
            f"$r = New-Attr3FootageBindProofJob -ClipId 'SYN-BINDPROOF-0001' -Parts $parts -OutDir '{self.out}' -WorkRoot '{self.work_root}'\n"
            "Write-Output ('JOBFILE=' + $r.jobFile)\n",
            encoding="utf-8",
        )
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script)],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"{proc.stdout}\n{proc.stderr}")
        line = next(l for l in proc.stdout.splitlines() if l.startswith("JOBFILE="))
        return Path(line[len("JOBFILE="):])

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


if __name__ == "__main__":
    unittest.main()
