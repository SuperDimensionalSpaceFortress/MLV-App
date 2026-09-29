---
target: TRAPS.md
kind: trap
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-09-26 — staging a build under a deep scratch directory pushed object-file paths past MAX_PATH, and MinGW's `make` looped forever instead of failing

**Symptom (measured):** a build staged its output under a deeply nested scratch/temp directory. The resulting object-file paths came to 291 characters, past the 260-character limit. Instead of erroring, `mingw32-make` entered a runaway loop: 494 self-repeating make processes accumulated with zero `cc1plus` (the actual compiler) ever running — CPU and process-table exhaustion with no compiler error anywhere in the log, because the compiler was never actually invoked.

**Cause:** Windows' legacy `MAX_PATH` (260-character) limit still governs many toolchain components (this MinGW `make`/toolchain in particular) even when the underlying filesystem and APIs support longer paths. A path that crosses the limit is mishandled by some tools rather than rejected outright, and the symptom looks nothing like a path-length error. The mechanism inside `make` was not diagnosed; the measured facts are the 291-character paths, the runaway loop with no compiler ever started, and that a short output directory cured it. The same limit bites other tools with different symptoms: on 2026-09-29 a `git commit` under a session scratch root failed with `Filename too long` at a 266-character object path, and the bus already records a separate `git worktree add` path budget that `core.longpaths` does not lift.

**Rule:** never let a build's output directory live under a long, deeply-nested scratch/temp root (auto-generated agent scratchpads, per-run timestamped directories, etc.) when the toolchain includes any legacy Windows component. Keep build output directories short (e.g. directly under a drive root or a short fixed prefix) and measure the longest resulting path rather than assuming. Add this as a standing constraint on any automation that computes its own build/output directory from a session-scoped scratch path.

**Falsifier:** compute the full path a generated object file would get under the proposed output directory and check its length before starting a build; if any exceeds ~250 characters, relocate the output directory rather than proceeding. A build that hangs with 0 compiler invocations and a growing process count, with no explicit path-length error, is this trap, not a toolchain crash.
