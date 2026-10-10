---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/worktree-live-check-misses-holders-that-do-not-name-the-path
rule: before a cleanup removes a worktree, "no live process names the path on its command line" is not enough; also refuse when a live process has the worktree (or a subdirectory) as its current directory, or runs a script (or a sibling it dot-sources or calls by name) that contains the path, and re-scan immediately before the first delete. A probe that cannot see is a refusal, never a pass: a null command line is still a live process, and an unreadable cwd is exempt only if the process is older than the directory.
mechanism: the lane-exit merged-worktree sweep (tools/coordination/Retire-LaneWorktree.ps1, called from Invoke-Lane.ps1) retired a clean, pushed, merged worktree while a detached measurement chain used it. The chain's command lines named only its run directory; the worktree path lived in a dot-sourced arms.ps1 variable and on a probe child alive for part of each `Start-Sleep -Seconds 60` wait loop, so no process named the worktree, the gate passed, `git worktree remove` ran and the next probe failed with pwsh exit 64. On Windows a worktree that is a process's current directory is half-removed by `git worktree remove`, so the cwd case is a corruption.
check: `py -3 -m pytest tests/coordination/test_retire_lane_worktree.py -q`
supersedes: none
evidence: measured
- **Guard:** tests/coordination/test_retire_lane_worktree.py; the gate writes one `REFUSED worktree-removal path=... pid=N ... via=cmdline|cwd|script:<file>|cwd-unknown|cwd-probe-unavailable` line per holder to stderr.
- **Limits:** a process older than the worktree that later changes into it with an unreadable cwd is not caught; an unquoted script path containing spaces is not followed; an unreadable process created after the worktree makes every removal refuse until it exits.
