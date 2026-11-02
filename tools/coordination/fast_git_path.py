"""Put Git for Windows' real git.exe ahead of its ``cmd\\git.exe`` launcher, for a pytest run only.

COST (CI-SHARD8-GUARDRAILS-COST-1). On Windows, ``git`` on PATH is ``<Git>\\cmd\\git.exe``, a small
launcher whose only job is to start ``<Git>\\mingw64\\bin\\git.exe``. Every ``git`` the tests spawn
therefore pays for two process creations. MEASURED 2026-10-17 on the hub VM, 25 runs of
``git rev-parse --git-dir`` each: launcher 63 ms median, real binary 28 ms median. The guardrail
suites under tools/coordination and tests/coordination spawn well over ten thousand git processes
(directly, and through the pwsh scripts they run), so the launcher is a double-digit share of the
"Run coordination and self-healing guardrails" step.

It is the same Git, the same version and the same configuration: only the launcher hop is skipped.
Checked on the same machine with a PATH holding no Git ``usr\\bin`` entry: a ``#!/bin/sh`` hook runs,
a clone and a push over a local path succeed, and a shell ``clean`` filter works, identically through
both binaries. Production code is untouched; nothing outside a pytest run calls this.

Escape hatch: ``MLV_TEST_GIT_LAUNCHER=1`` leaves PATH alone, to reproduce a failure through the
launcher exactly as before. Off Windows, or when Git is not laid out the usual way, it does nothing.

Called from the conftest.py files beside the suites, at import time, so that module-level
``shutil.which("git")`` calls in test files (collected afterwards) already see the real binary.
"""
import os
import shutil
from pathlib import Path


def install():
    """Put Git's mingw64\\bin first on PATH. Returns the directory put first, or None when nothing was changed."""
    if os.name != "nt" or os.environ.get("MLV_TEST_GIT_LAUNCHER") == "1":
        return None
    found = shutil.which("git")
    if not found:
        return None
    launcher = Path(found)
    # Git\cmd\git.exe and Git\bin\git.exe are the two launchers; Git\mingw64\bin\git.exe is the real binary.
    if launcher.parent.name.lower() not in ("cmd", "bin") or launcher.parent.parent.name.lower() == "mingw64":
        return None
    real = launcher.parent.parent / "mingw64" / "bin"
    if not (real / "git.exe").is_file():
        return None
    path = os.environ.get("PATH", "")
    # A hosted runner already lists mingw64\bin, but after the launcher: move it to the front.
    if path.split(os.pathsep)[0].rstrip("\\/").lower() == str(real).lower():
        return None
    os.environ["PATH"] = str(real) + os.pathsep + path
    return str(real)
