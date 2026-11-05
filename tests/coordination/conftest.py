"""Shared pytest setup for tests/coordination.

CI-SHARD8-GUARDRAILS-COST-1: every test here shells out to git, many through a pwsh script that shells
out to git again. Skip Git for Windows' two-process ``cmd\\git.exe`` launcher so each of those runs the
real ``mingw64\\bin\\git.exe`` directly. The measurement, the reasoning and the
``MLV_TEST_GIT_LAUNCHER=1`` escape hatch are in tools/coordination/fast_git_path.py. This runs at import
time on purpose: test modules call ``shutil.which("git")`` at import, and they are collected after this file.
"""
import importlib.util
import os
from pathlib import Path

# CI-SHARD8-GUARDRAILS-COST-2: most tests here start a cold pwsh child (some compile with Add-Type). PowerShell's own startup
# telemetry and update check are work no test needs: MEASURED 2026-11-04 on the hub VM, alternating variants, 25 rounds of
# `pwsh -NoProfile -Command exit 0`: median 0.380 s -> 0.362 s; with an Add-Type compile: 1.063 s -> 0.929 s. The children
# inherit this process's environment. setdefault: a caller that sets either variable keeps its own value.
os.environ.setdefault("POWERSHELL_TELEMETRY_OPTOUT", "1")
os.environ.setdefault("POWERSHELL_UPDATECHECK", "Off")

_FAST_GIT = Path(__file__).resolve().parents[2] / "tools" / "coordination" / "fast_git_path.py"
_spec = importlib.util.spec_from_file_location("fast_git_path", _FAST_GIT)
_fast_git_path = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fast_git_path)
_fast_git_path.install()
