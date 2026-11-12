"""Shared hidden-spawn helper (MLV-LANE-CONSOLE-HIDDEN-1).

Why: a console program started WITHOUT CREATE_NO_WINDOW by a parent that has no console of its own
(a hidden/detached/GUI parent, a scheduled task, a hook) opens a VISIBLE console window on the owner's
desktop. A child of a console parent inherits that console and opens nothing, so the defect only shows
when the parent is console-less, which is exactly when nobody is watching to notice.

What: ``no_window_flags()`` returns ``subprocess.CREATE_NO_WINDOW`` on Windows and 0 elsewhere, so a
call site can pass ``creationflags=no_window_flags()`` unconditionally. ``extra`` ORs in flags the site
already uses (for example a priority class) so adding this never replaces an existing flag.

What it does NOT do: change stdout/stderr capture, cwd, env, timeouts or anything a caller measures.
Standalone scripts that cannot import this package carry the same one-liner inline; the guard test
``test_hidden_spawn_flags.py`` pins both shapes.
"""
from __future__ import annotations

import os
import subprocess

_WINDOWS_NO_WINDOW = 0x08000000  # winbase.h CREATE_NO_WINDOW; used when the subprocess module lacks the name
_WINDOWS_NEW_CONSOLE = 0x00000010  # winbase.h CREATE_NEW_CONSOLE: Windows IGNORES CREATE_NO_WINDOW when this bit is also set


def no_window_flags(extra: int = 0) -> int:
    """CREATE_NO_WINDOW on Windows (OR'd with ``extra``), ``extra`` unchanged elsewhere.

    On Windows CREATE_NEW_CONSOLE is dropped from ``extra``: with both bits set the system ignores CREATE_NO_WINDOW
    and opens a visible console, which is the defect this module exists to prevent.
    """
    if os.name != "nt":
        return int(extra)
    return (int(extra) & ~_WINDOWS_NEW_CONSOLE) | int(getattr(subprocess, "CREATE_NO_WINDOW", _WINDOWS_NO_WINDOW))
