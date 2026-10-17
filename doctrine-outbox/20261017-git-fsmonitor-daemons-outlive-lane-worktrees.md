---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/git-fsmonitor-daemons-outlive-lane-worktrees
rule: when shared repo config sets core.fsmonitor=true, every linked worktree inherits it and its first `git status` starts a detached `git fsmonitor--daemon` that nothing stops. Per lane worktree, set core.fsmonitor=false in config.worktree (only if extensions.worktreeConfig is already on; otherwise `--worktree` writes the SHARED config and switches the main checkout off too) and run `git fsmonitor--daemon stop` before `worktree remove`. Never kill the daemons by name or pid.
mechanism: on the MLV-App host (git 2.40.0.windows.1) 65 daemons ran for 64 worktrees, the oldest 17 h old, 3.2 GiB private in total, with host commit at 61-64 GiB against a 60 GiB dispatch gate that then refused lanes for hours. A daemon's cwd is the profile dir, so cwd cannot map pid to worktree; `git -C <wt> fsmonitor--daemon status` maps worktree to daemon. Hooks that run `git status` over every worktree start one daemon each.
check: `py -3 -m pytest tools/coordination/test_coordination_guardrails.py -k fsmonitor -q`
supersedes: none
evidence: measured
- **Guard:** tools/coordination/lane-fsmonitor.ps1, called from Invoke-Lane.ps1, Invoke-Workstream.ps1, Invoke-WorkstreamLoop.ps1 and Retire-LaneWorktree.ps1; both helpers refuse the main checkout.
- **Limits:** a live lane keeps its daemon until it ends; the helper skips when the extension is off.
