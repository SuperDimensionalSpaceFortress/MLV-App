"""DURATION-SCAN-UTF8-PATHS-1: duration_as_proof_scan must see a tracked non-ASCII path.

`_tracked_files` reads `git ls-files -z` output. git prints paths as UTF-8 bytes; under
`subprocess.run(text=True)` with no encoding Python decodes them with the locale codec, which is
cp1252 on this host (utf8_mode=0 is the default). A tracked `tests/caf<e-acute>.cpp` then
arrives as the mojibake `tests/caf<A-tilde><copyright>.cpp`, a path that names nothing on disk,
so the scan opens the wrong file or none. Bus TRAPS.md "A locale decode turned a non-ASCII
worktree path into a path that named nothing" (K70) is the same defect in another tool.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.repo_hygiene import duration_as_proof_scan

REPO_ROOT = Path(__file__).resolve().parents[2]
NON_ASCII_REL = "tests/café.cpp"
SEED_SOURCE = "void f() { if (elapsedMs > 0) { run(); } }\n"

_CHILD = r"""
import json, locale, sys
from pathlib import Path
from tools.repo_hygiene import duration_as_proof_scan as scan
rows = scan.scan_repo(Path(sys.argv[1]))
json.dump({"encoding": locale.getpreferredencoding(False), "utf8_mode": sys.flags.utf8_mode,
           "paths": sorted({c.path for c in rows}), "tracked": scan._tracked_files(Path(sys.argv[1]))},
          sys.stdout)
"""


def _make_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True, timeout=60)
    target = root / NON_ASCII_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(SEED_SOURCE, encoding="utf-8", newline="\n")
    subprocess.run(["git", "-C", str(root), "add", "--", NON_ASCII_REL], check=True, timeout=60)


class NonAsciiTrackedPathTests(unittest.TestCase):
    def test_default_codec_child_sees_the_non_ascii_path(self) -> None:
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
        env["PYTHONUTF8"] = "0"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_repo(root)
            proc = subprocess.run([sys.executable, "-X", "utf8=0", "-c", _CHILD, str(root)],
                                  cwd=str(REPO_ROOT), env=env, capture_output=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        result = json.loads(proc.stdout.decode("ascii"))
        self.assertEqual(result["utf8_mode"], 0)
        self.assertEqual(result["tracked"], [NON_ASCII_REL],
                         f"child codec {result['encoding']} mangled the tracked path")
        self.assertEqual(result["paths"], [NON_ASCII_REL])

    def test_cp1252_decode_of_git_output_cannot_mangle_the_path(self) -> None:
        # Deterministic on any host: emulate what text=True does under a cp1252 locale.
        real_run = subprocess.run

        def locale_decoding_run(cmd, *args, **kwargs):
            text = kwargs.pop("text", False)
            encoding = kwargs.pop("encoding", None)
            errors = kwargs.pop("errors", None)
            proc = real_run(cmd, *args, **kwargs)
            if encoding is None and text:
                encoding = "cp1252"
            if encoding is not None and isinstance(proc.stdout, bytes):
                proc.stdout = proc.stdout.decode(encoding, errors or "replace")
            return proc

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_repo(root)
            with mock.patch.object(duration_as_proof_scan.subprocess, "run", locale_decoding_run):
                tracked = duration_as_proof_scan._tracked_files(root)
                found = {c.path for c in duration_as_proof_scan.scan_repo(root)}
        self.assertEqual(tracked, [NON_ASCII_REL])
        self.assertEqual(found, {NON_ASCII_REL})


if __name__ == "__main__":
    unittest.main()
