"""Guard for MLV-LANE-CONSOLE-HIDDEN-1: no console program may be spawned without a hidden-window flag.

A console program started without CREATE_NO_WINDOW (Python) or ``-WindowStyle Hidden`` (Start-Process)
by a parent that owns no console opens a VISIBLE window on the owner's desktop. Roughly 15 such
``...pythoncore-3.14-64\\python.exe`` windows were recorded on 2026-11-09 (ledger QUEUED 2026-11-09T23:14:17Z).

Three pins:
  1. ``hidden_spawn.no_window_flags`` sets CREATE_NO_WINDOW on Windows, ORs an existing flag, and is a no-op elsewhere.
  2. Every ``subprocess.run/Popen/check_output/check_call/call`` in a non-test tool module under tools/ carries a
     ``creationflags=`` keyword. The set is derived by glob, so a NEW unflagged spawn fails here.
     HIGH-RISK path classes (key3) are listed in EXCLUDED_PREFIXES and are left to their own cards.
  3. The named test fixtures that the 2026-11-09 evidence points at (the spliced-body-scan runner and the
     host-load-gate fixtures) spawn hidden, and every ``Start-Process powershell.exe`` in the gate's generated
     run.ps1 bodies says ``-WindowStyle Hidden``.
"""
from __future__ import annotations

import ast
import re
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from tools.repo_hygiene import hidden_spawn

ROOT = Path(__file__).resolve().parents[2]
SPAWN_ATTRS = {"run", "Popen", "check_output", "check_call", "call"}

# Key3 HIGH-RISK classes (and the frozen in-tree agent-bridge) stay out of this card's diff; each is an OPEN line.
EXCLUDED_PREFIXES = (
    "tools/agent-bridge/",
    "tools/hooks/",
    "tools/gates/",
    "tools/profiling/bachelor/",
    "tools/profiling/dual-venue/",
    "tools/gpu/backend/",
    "tools/repo_hygiene/brokered_closeout",
)
EXCLUDED_NAME_RE = re.compile(r"^tools/coordination/merge[^/]*$")

# Test modules whose spawns must be hidden (the evidence chain and the fixtures named by the card).
NAMED_TEST_MODULES = (
    "tools/repo_hygiene/test_attr3_spliced_body_scan.py",
    "tools/repo_hygiene/test_playback_host_load_gate.py",
)


def _tracked_py_files() -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "--", "tools/*.py"],
        capture_output=True, text=True, check=True, timeout=60,
        creationflags=hidden_spawn.no_window_flags(),
    ).stdout.splitlines()
    return sorted(out)


def _tool_modules() -> list[str]:
    files = []
    for rel in _tracked_py_files():
        name = rel.rsplit("/", 1)[-1]
        if name.startswith("test_") or name == "conftest.py":
            continue
        if rel.startswith(EXCLUDED_PREFIXES) or EXCLUDED_NAME_RE.match(rel):
            continue
        files.append(rel)
    return files


def _unflagged_spawns(rel: str) -> list[str]:
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"), filename=rel)
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr in SPAWN_ATTRS
                and isinstance(fn.value, ast.Name) and fn.value.id == "subprocess"):
            continue
        if any(kw.arg == "creationflags" for kw in node.keywords):
            continue
        bad.append(f"{rel}:{node.lineno} subprocess.{fn.attr}(...) has no creationflags=")
    return bad


class NoWindowFlagsTests(unittest.TestCase):
    def test_sets_create_no_window_on_windows(self):
        with mock.patch.object(hidden_spawn.os, "name", "nt"):
            flags = hidden_spawn.no_window_flags()
        self.assertEqual(flags & 0x08000000, 0x08000000)

    def test_ors_an_existing_flag_instead_of_replacing_it(self):
        below_normal = 0x00004000
        with mock.patch.object(hidden_spawn.os, "name", "nt"):
            flags = hidden_spawn.no_window_flags(below_normal)
        self.assertEqual(flags, below_normal | 0x08000000)

    def test_is_a_no_op_off_windows(self):
        with mock.patch.object(hidden_spawn.os, "name", "posix"):
            self.assertEqual(hidden_spawn.no_window_flags(), 0)
            self.assertEqual(hidden_spawn.no_window_flags(0x20), 0x20)


class ToolSpawnSitesTests(unittest.TestCase):
    def test_scan_set_is_not_empty_and_skips_excluded_classes(self):
        files = _tool_modules()
        self.assertGreater(len(files), 50, "glob matched suspiciously few tool modules")
        self.assertIn("tools/repo_hygiene/core.py", files)
        self.assertNotIn("tools/gates/output_budget.py", files)

    def test_every_tool_spawn_carries_creationflags(self):
        bad = []
        for rel in _tool_modules():
            bad.extend(_unflagged_spawns(rel))
        self.assertEqual(bad, [], "console spawn without CREATE_NO_WINDOW:\n" + "\n".join(bad))


class NamedTestFixtureTests(unittest.TestCase):
    def test_named_test_modules_spawn_hidden(self):
        bad = []
        for rel in NAMED_TEST_MODULES:
            bad.extend(_unflagged_spawns(rel))
        self.assertEqual(bad, [], "test fixture spawn without CREATE_NO_WINDOW:\n" + "\n".join(bad))

    def test_host_load_gate_start_process_fixtures_are_hidden(self):
        src = (ROOT / "tools/repo_hygiene/test_playback_host_load_gate.py").read_text(encoding="utf-8")
        starts = [m.start() for m in re.finditer(r"Start-Process -FilePath 'powershell\.exe'", src)]
        self.assertGreaterEqual(len(starts), 3, "expected the three run.ps1 Start-Process fixtures")
        bad = []
        for pos in starts:
            end = src.index("-PassThru", pos)
            if "-WindowStyle Hidden" not in src[pos:end]:
                bad.append(f"test_playback_host_load_gate.py:{src.count(chr(10), 0, pos) + 1}")
        self.assertEqual(bad, [], "Start-Process powershell.exe without -WindowStyle Hidden: " + ", ".join(bad))


if __name__ == "__main__":
    unittest.main()
