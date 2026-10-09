---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/worktree-live-check-misses-holders-that-do-not-name-the-path
rule: before a cleanup removes a worktree, "no live process names the path on its command line" is not enough; also refuse when a live process has the worktree (or a subdirectory) as its current directory, or when a script it runs, or a sibling script that one dot-sources or calls by name, contains the path, and re-run that check on a fresh process scan immediately before the first delete. A probe that cannot see is a refusal, never a pass: a process with a null command line is still a live process, and an unreadable cwd is exempt only if the process is older than the directory.
mechanism: the lane-exit merged-worktree sweep (tools/coordination/Retire-LaneWorktree.ps1, called from Invoke-Lane.ps1) retired a clean, pushed, merged worktree while a detached measurement chain was using it. The chain's command lines named its own run directory; the worktree path lived only in a dot-sourced arms.ps1 variable and on a probe child's command line that exists for part of each wait loop. During a `Start-Sleep -Seconds 60` wait loop no process named the worktree, so the SAFE gate passed, `git worktree remove` ran, and the next probe failed with pwsh exit 64. On Windows a worktree that is a process's current directory is also half-removed by `git worktree remove`, so the cwd case is a corruption.
applies: every board whose cleanup or retention driver removes worktrees under a path other lanes script against
check: `py -3 -m pytest tests/coordination/test_retire_lane_worktree.py -q`
supersedes: none
evidence: measured
- **Guard:** tests/coordination/test_retire_lane_worktree.py. The gate writes one `REFUSED worktree-removal path=... pid=N ... via=cmdline|cwd|script:<file>|cwd-unknown|cwd-probe-unavailable` line per holder to stderr and returns `holders`, `cwdProbe`, `cwdUnknown`, `cwdExempt`, `worktreeCreatedUtc`.
- **r2:** probe cannot run gives `kept` / `cwd-probe-unavailable`; probe cannot read a live process gives `kept` / `cwd-unknown` naming the pids. The probe reads 32-bit (WOW64) processes. A relative script path is resolved against THAT holder's cwd; with the cwd unknown the holder is treated as possibly naming the worktree.
- **r3:** a null CommandLine no longer drops the process from the snapshot (TRAPS.md:15808). The session-0 exemption is gone, since a session id is not evidence. One exemption remains: an unreadable cwd is ignored only when the process CreationDate is strictly earlier than the worktree root's CreationTimeUtc; an unreadable date is refused.
- **Limits:** a process older than the worktree that later changes into it, with a cwd unreadable to us, is not caught; an unquoted script path containing spaces is not followed; a holder that only holds the path in memory is invisible; an unreadable process created after the worktree makes every removal refuse until it exits.
