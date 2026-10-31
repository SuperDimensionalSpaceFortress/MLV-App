---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/rg-files-hidden-over-temp-drives-kernel-time
rule: locate a known file by its known path (Test-Path, or the run dir the lane itself wrote), never with `rg --files --hidden` rooted at %TEMP% or ~\.claude. If a walk is unavoidable, root it at the narrowest sub-path, drop --hidden and -uu, cap it with `rg -j 2`, and use a timeout that kills the child, not only the tool call. For the owners of Codex lanes and pwsh -Command callers to adopt or distinguish.
mechanism: those trees are huge and mostly hidden, and the walk is metadata syscalls, so it shows as kernel time, not user time. A lane prompt or tool that repeats it re-issues it after a kill. A Claude PreToolUse hook cannot stop it in a Codex lane, because Codex does not run Claude hooks. Measured 2026-10-31 on a 16-core host, non-elevated Get-Process deltas: one walker used 2.23-2.87 kernel cores plus about 0.3 user cores, and after two walkers exited host kernel time fell from 34-42% to 11-21%. Not the cause that day: heavy-slot waiters (about 0.15 cores), Defender (0.1-0.4), interrupt and DPC time (about 1%). WmiPrvSE (0.7 cores, mostly kernel) has an UNKNOWN driver without an elevated trace.
check: on a hot host, sample Get-Process rg twice 10 s apart (TotalProcessorTime delta / 10 = cores), then read its command line once to see the root it walks.
supersedes: none
evidence: measured
- **Guard:** none yet (a Codex lane runs no Claude hook; the fix lives in each lane prompt or tool)
