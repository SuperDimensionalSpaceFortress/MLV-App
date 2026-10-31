---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/rg-files-hidden-over-temp-drives-kernel-time
rule: locate a known file by its known path (Test-Path, or the run dir the lane itself wrote), never with `rg --files --hidden` rooted at %TEMP% or ~\.claude. If a walk is unavoidable, root it at the narrowest sub-path, drop --hidden and -uu, cap it with `rg -j 2`, and use a timeout that kills the child, not only the tool call. For the owners of Codex lanes and pwsh -Command callers to adopt or distinguish.
mechanism: those trees are huge and mostly hidden, and the walk is metadata syscalls, so it shows as kernel time, not user time. A lane prompt or tool that repeats it re-issues it after a kill. A Claude PreToolUse hook cannot stop it in a Codex lane, because Codex does not run Claude hooks.check: on a hot host, sample Get-Process rg twice 10 s apart (TotalProcessorTime delta / 10 = cores), then read its command line once to see the root it walks.
check: on a hot host, sample Get-Process rg twice 10 s apart (TotalProcessorTime delta / 10 = cores), then read its command line once to see the root it walks.
supersedes: none
evidence: reported
- **Guard:** none yet (a Codex lane runs no Claude hook; the fix lives in each lane prompt or tool)
