---
target: TRAPS.md
kind: trap
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-09-07 — a prompt file with non-ASCII characters silently kills a stdin-piped agent-CLI dispatch, and the file itself is not the cause

**Symptom (measured):** dispatching the Codex CLI (`codex exec`, invoked non-interactively, prompt piped over stdin by a PowerShell wrapper) with a prompt file that contained Unicode characters — checkmark/cross glyphs pasted from a terminal transcript, plus em-dashes — failed instantly (under a second, exit 1) with the CLI's own error: `Failed to read prompt from stdin: input is not valid UTF-8 (invalid byte at offset N)`. The prompt `.md` file itself was verified to be valid UTF-8 on disk; the corruption happened somewhere in the wrapper script's stdin-piping step, not in the file the agent tooling authored.

**Cause:** an intermediate PowerShell-based dispatch wrapper's stdin-piping to the CLI process does not reliably preserve arbitrary Unicode byte sequences end-to-end (root cause of the corruption itself was out of scope to chase further at the time). The project already had a known "ASCII-only" convention for its own `.ps1` source files (non-ASCII had broken PowerShell parsing before), but that prior rule had only ever been observed to apply to tracked script files — not to prompt *text* being dispatched at runtime to a different agent CLI through the same wrapper.

**Rule:** before dispatching prompt text to any agent CLI through a PowerShell (or similar shell-mediated) stdin pipe, scan it for non-ASCII bytes and normalize: replace terminal glyphs with plain words (PASS/FAIL/SKIP), em-dashes with `--`, curly quotes with straight ones. A one-line verification before dispatch: attempt to `.encode("ascii")` the prompt text and treat any exception as a hard stop. Measured only for Codex dispatches through this wrapper; Claude-lane prompts through the same wrapper showed no such failure, so the fix scope is the Codex lane, and any other lane's prompt deserves the same caution until it is shown otherwise.

**Falsifier:** dispatch a prompt containing a single non-ASCII character through the same wrapper and confirm the instant stdin-decode failure; then confirm the ASCII-normalized version of the same prompt dispatches successfully.
