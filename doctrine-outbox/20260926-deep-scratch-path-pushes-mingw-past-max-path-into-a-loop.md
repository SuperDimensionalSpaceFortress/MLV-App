---
target: TRAPS.md
kind: trap
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-09-26 — staging a build under a deep scratch directory pushed object-file paths past MAX_PATH, and MinGW's `make` looped forever instead of failing

**Symptom (measured):** a build was staged with its output directory under a deeply nested scratch directory. The resulting object-file paths came to 291 characters, past the 260-character Windows limit. Instead of erroring, `mingw32-make` looped: 494 self-repeating make processes accumulated and zero `cc1plus` (the actual compiler) ever ran. A short output directory cured it.

**Cause (not diagnosed):** the measured facts are the 291-character paths, the runaway make loop with no compiler ever started, and that a short output directory fixed it. The mechanism inside `make` was not investigated. The 260-character limit is the working suspect, because the symptom looks nothing like a path-length error. The bus already records a separate case of the same family: `git worktree add` has a hard path budget that `core.longpaths` does not lift.

**Rule:** do not let a build's output directory live under a long, deeply-nested scratch root (auto-generated agent scratchpads, per-run timestamped directories) when the toolchain includes legacy Windows components. Keep build output directories short (directly under a drive root or a short fixed prefix) and measure the longest resulting path rather than assuming. Treat this as a standing constraint on any automation that derives its build output directory from a session-scoped scratch path.

**Falsifier:** compute the full path a generated object file would get under the proposed output directory and check its length before starting a build; if it exceeds ~250 characters, relocate the output directory. If a build hangs with a growing process count and no compiler invocation, measure the longest path first: a short output directory that cures it confirms this trap.
