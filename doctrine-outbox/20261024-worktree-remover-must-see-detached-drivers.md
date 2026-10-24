---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/worktree-remover-must-see-detached-drivers
rule: a remover that frees a lane worktree asks one shared live-reference guard right before it removes, and refuses on a live process naming the path (path-prefix match: lane-X-1 is never named by lane-X-10), on a live pid recorded in a run dir (pid file, child record, receipt pid) whose command line names the worktree or that run dir while the run dir names the worktree, and on any census failure (unknown is never free). One typed REFUSED line per refusal.
mechanism: 2026-10-24 06:02Z a retention pass removed a worktree under a detached venue driver whose command line named only its run dir; the lane that launched it had exited (receipt complete). A command-line regex and a running-receipt check both read it as idle; the driver then logged "not measured" every 5 min for 40 min. A missing driver script is now typed (VENUE_DRIVER_SCRIPT_MISSING path=) and stops the chain.
applies: mlv-app
check: in mlv-app, pytest tests/coordination/test_retire_lane_worktree.py -k "live_refusal or live_driver or prefix_sibling or census" (10 cases, RED before the fix).
supersedes: none
evidence: measured
