---
target: TRAPS.md
kind: trap
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-04-28 — `os.execv`/`execvp` do not quote argv on Windows, so space-containing paths split in the child

**Symptom (measured):** a Python wrapper resolved an absolute script path that contained a space (a normal Windows directory name), then called `os.execv(sys.executable, [sys.executable, script_path, ...])`. The child process failed immediately with `can't find '__main__' module in '<path fragment up to the first space>'`. The parent had built argv correctly as a Python list; the failure appeared only after handoff to the OS.

**Cause:** on Windows, `os.execv`/`os.execvp` are emulated by re-launching a child process and joining argv into a single command-line string. That emulation joins elements with a plain space and does **not** quote elements that themselves contain spaces, so the CRT command-line parser in the child re-tokenizes the path at the first space. This is a Windows-only defect: the same code is correct on POSIX, where `execv` passes argv directly with no re-serialization step.

**Rule:** on Windows, never use `os.execv`/`os.execvp` (or any wrapper that re-serializes argv into a string) to launch a child whose arguments may contain spaces. Use list-form `subprocess.run(command_list)` followed by `sys.exit(completed.returncode)` instead — `subprocess.list2cmdline()` applies the CRT's actual quoting rules and produces a command line the child parses correctly. The trade-off is that the parent stays alive as the real parent rather than being replaced (no PID takeover), which is usually irrelevant for stdio-transparent use cases like MCP-style servers.

**Falsifier:** call the exec path with a target path containing a space and confirm the child errors on the truncated fragment; then swap to `subprocess.run` with the same list-form argv and confirm it launches cleanly. Any helper whose only job is "launch X" should carry a test that launches X from a path containing a space — a happy-path launch test that avoids spaces will not catch this.
