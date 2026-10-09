---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/worktree-live-check-misses-holders-that-do-not-name-the-path
rule: before a cleanup removes a worktree, "no live process names the path on its command line" is not enough; also refuse when a live process has the worktree (or a subdirectory) as its current directory, or when a script it runs, or a sibling script that one dot-sources or calls by name, contains the path, and re-run that check on a fresh process scan immediately before the first delete.
mechanism: the lane-exit merged-worktree sweep (tools/coordination/Retire-LaneWorktree.ps1, Invoke-SweepMergedLaneWorktrees, called from Invoke-Lane.ps1) retired a clean, pushed, merged worktree while a detached measurement chain was using it. The chain's command lines named its own run directory; the worktree path lived only in a dot-sourced arms.ps1 variable and on the command line of a probe child that exists for part of each wait loop. During a 20-minute `Start-Sleep -Seconds 60` wait loop no process named the worktree, so the SAFE gate passed, `git worktree remove` ran, and the next probe failed with "The argument ... is not recognized as the name of a script file" (pwsh exit 64) for every remaining leg. Related: on Windows a worktree that is a process's current directory is half-removed by `git worktree remove` (tracked files gone, directory kept, `remove-failed`), so the cwd case is also a corruption, not only a missed use.
applies: every board whose cleanup or retention driver removes worktrees under a path other lanes script against
check: `py -3 -m pytest tests/coordination/test_retire_lane_worktree.py -q` (cwd holder, cwd-in-subdirectory holder, chain.ps1 -> quiet.ps1 -> arms.ps1 holder, forward-slash spelling, negative control, end-to-end release once the holder exits, sweep keeps a cwd-held worktree)
supersedes: none
evidence: measured
- **Guard:** tests/coordination/test_retire_lane_worktree.py (8 new cases); the gate writes one `REFUSED worktree-removal path=... pid=N ... via=cmdline|cwd|script:<file>` line per holder to stderr and returns the holders in the disposition. Limits: a 32-bit or other-user process cwd is unreadable and not counted; a script path with spaces and no quotes on the command line is not followed; a holder that only holds the path in memory (an env var or argument passed down from a parent) is invisible.
