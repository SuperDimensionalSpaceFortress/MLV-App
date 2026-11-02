---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/git-fsmonitor-daemons-outlive-lane-worktrees
rule: when shared repo config sets core.fsmonitor=true, every linked worktree inherits it and its first `git status` starts a detached `git fsmonitor--daemon` that nothing stops. Per lane worktree, set core.fsmonitor=false in config.worktree (only if extensions.worktreeConfig is already on; without it `--worktree` either errors or, on a git that allows it, writes the SHARED config and switches the main checkout off too, so the helper skips) and run `git fsmonitor--daemon stop` before `worktree remove`. Never kill the daemons by name or pid.
mechanism: on the MLV-App host (git 2.40.0.windows.1) 65 daemons ran for 64 worktrees, the oldest 17 h old, 3.2 GiB private in total, with host commit at 61-64 GiB against a 60 GiB dispatch gate that then refused lanes for hours. A daemon's cwd is the profile dir, so cwd cannot map pid to worktree; `git -C <wt> fsmonitor--daemon status` maps worktree to daemon. Hooks that run `git status` over every worktree start one daemon each.
check: `py -3 -m pytest tools/coordination/test_coordination_guardrails.py -k fsmonitor -q`
supersedes: none
evidence: measured
- **Guard:** tools/coordination/lane-fsmonitor.ps1, called from Invoke-Lane.ps1, Invoke-Workstream.ps1, Invoke-WorkstreamLoop.ps1 and Retire-LaneWorktree.ps1; both helpers refuse the main checkout.
- **Order:** the disable (and a stop of any daemon already running there) runs BEFORE the first git command that reads the worktree's index, on every path that reuses an existing worktree, including the continuation branch that refuses a dirty tree. Pinned by an order test.
- **Limits:** a daemon started in a live lane by something other than the lane keeps running until the lane ends; the helper skips when the extension is off. `Retire -WhatIf` writes the worktree-scoped setting (the sweep already stamped config).
