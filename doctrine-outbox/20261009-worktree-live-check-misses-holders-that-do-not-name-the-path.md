---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/worktree-live-check-misses-holders-that-do-not-name-the-path
rule: before a cleanup removes a worktree, "no live process names the path on its command line" is not enough; also refuse when a live process has the worktree (or a subdirectory) as its current directory, or when a script it runs, or a sibling script that one dot-sources or calls by name, contains the path, and re-run that check on a fresh process scan immediately before the first delete. A probe that cannot see (cwd probe unavailable, or a live process's cwd unreadable) is a refusal, never a pass.
mechanism: the lane-exit merged-worktree sweep (tools/coordination/Retire-LaneWorktree.ps1, Invoke-SweepMergedLaneWorktrees, called from Invoke-Lane.ps1) retired a clean, pushed, merged worktree while a detached measurement chain was using it. The chain's command lines named its own run directory; the worktree path lived only in a dot-sourced arms.ps1 variable and on a probe child's command line that exists for part of each wait loop. During a `Start-Sleep -Seconds 60` wait loop no process named the worktree, so the SAFE gate passed, `git worktree remove` ran, and the next probe failed with pwsh exit 64 for every remaining leg. On Windows a worktree that is a process's current directory is also half-removed by `git worktree remove`, so the cwd case is a corruption, not only a missed use.
applies: every board whose cleanup or retention driver removes worktrees under a path other lanes script against
check: `py -3 -m pytest tests/coordination/test_retire_lane_worktree.py -q` (cwd-probe-unavailable and cwd-unknown refusals, relative and quoted-relative script path, cwd holder, chain.ps1 -> quiet.ps1 -> arms.ps1 holder, negative controls, release once the holder exits)
supersedes: none
evidence: measured
- **Guard:** tests/coordination/test_retire_lane_worktree.py (8 cases in r1, 15 more in r2). The gate writes one `REFUSED worktree-removal path=... pid=N ... via=cmdline|cwd|script:<file>|cwd-unknown|cwd-probe-unavailable` line per holder to stderr and returns `holders`, `cwdProbe` and `cwdUnknown` in the disposition.
- **r2:** probe cannot run gives `kept` / `cwd-probe-unavailable`; probe cannot read a live non-service process gives `kept` / `cwd-unknown` naming the pids; never a would-retire with `holders=[]`. The probe also reads 32-bit (WOW64) processes. A relative script path on a holder's command line, quoted or not, is resolved against THAT holder's cwd and followed; with the cwd unknown the holder is treated as possibly naming the worktree.
- **Limits:** a session-0 service process with an unreadable cwd is not counted; an unquoted script path containing spaces is not followed; a holder that only holds the path in memory (an env var or argument from a parent) is invisible; an elevated or other-user process in the caller's session makes every removal refuse until it exits.
