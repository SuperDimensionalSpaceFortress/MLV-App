"""Shared pytest setup for tests/coordination.

CI-SHARD8-GUARDRAILS-COST-1: every test here shells out to git, many through a pwsh script that shells
out to git again. Skip Git for Windows' two-process ``cmd\\git.exe`` launcher so each of those runs the
real ``mingw64\\bin\\git.exe`` directly. The measurement, the reasoning and the
``MLV_TEST_GIT_LAUNCHER=1`` escape hatch are in tools/coordination/fast_git_path.py. This runs at import
time on purpose: test modules call ``shutil.which("git")`` at import, and they are collected after this file.
"""
import importlib.util
from pathlib import Path

_FAST_GIT = Path(__file__).resolve().parents[2] / "tools" / "coordination" / "fast_git_path.py"
_spec = importlib.util.spec_from_file_location("fast_git_path", _FAST_GIT)
_fast_git_path = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fast_git_path)
_fast_git_path.install()
