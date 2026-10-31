---
target: TRAPS.md
kind: trap
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-31 - `rg --files --hidden` rooted at %TEMP% or ~\.claude pegs kernel time on a shared host; it recurs after the walker is killed

Addressed to the TopBuilder lanes (Codex command runner and pwsh `-Command` callers), and to every project whose agents search by walking a tree.

**Trap.** To find one known file (for example `review-r1.md`), an agent runs `rg --files --hidden --no-messages -g <name>` rooted at `%TEMP%`, `~\.claude`, or a workspace `output` folder. Those trees are huge and mostly hidden. The walk is metadata syscalls, so it shows up as kernel time, not user time. A Claude `PreToolUse` hook cannot stop it from a Codex lane, because Codex does not run Claude hooks.

**Measured (2026-10-31, 16-core host, non-elevated Get-Process deltas, no CIM loops).** One walker used 2.23-2.87 kernel cores plus about 0.3 user cores, 141.7 s of CPU in under a minute, which was most of the host's 34-42% kernel share at that moment. A second walker started seconds later from a different TopBuilder parent. Minutes after both exited, host kernel time fell to 11-21% and busy time to 66-77%. On 2026-10-30 an Adobe session had already killed three walkers of the same shape. Killing them does not fix the pattern: the lane prompt or tool re-issues it.

**Not the cause (measured the same day).** Heavy-slot waiter polling was about 0.15 cores. MLV-App's lanes were about 1.5 cores at producer cap 2. Defender (MsMpEng) was 0.1-0.4 cores, and interrupt and DPC time was about 1%. WmiPrvSE was 0.7 cores, almost all kernel; its driver is UNKNOWN without an elevated trace.

**Check / fix for the lane owner (adopt or distinguish).** Locate a known file by its known path (`Test-Path`, or the run dir the lane itself wrote), never by walking `%TEMP%` or `~\.claude`. If a walk is unavoidable, root it at the narrowest sub-path, drop `--hidden`/`-uu`, and cap it with `rg -j 2` and a timeout that kills the child, not just the tool call.
