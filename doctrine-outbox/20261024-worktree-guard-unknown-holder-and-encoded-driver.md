---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/worktree-guard-unknown-holder-and-encoded-driver
rule: the shared live-reference guard a worktree remover calls is fail-closed on the holders it cannot read, not only on the ones it can: a live candidate whose cwd cannot be read (probe unavailable, or unreadable and not exempt by creation time or service owner) or whose -EncodedCommand does not decode refuses with a typed reason (cwd-unknown / cwd-probe-unavailable), and a run-dir driver's tie to its run dir is read over its command line with every -EncodedCommand argument decoded, so a run dir named only inside the base64 is a live driver. Only a remover that renames the root first (the rename settles an unreadable cwd) may defer the unreadable-cwd case; one that removes directly may not. Every tracked launch site that starts PowerShell from base64 or detaches a process is listed in a census test with the reason the guard still sees it.
mechanism: 2026-10-24 Dot reconciliation of PR #654: the guard built a list of unknown holders and never read it (FREE), and matched run dirs against the raw command line only, while agent tools and the lane job host launch `pwsh -EncodedCommand`. The census found one encoded lane launch on master (Invoke-Lane's Claude job host), safe because its child names the run dir through --settings and is recorded in <run dir>\<lane>-NNN.child.json.
applies: mlv-app
check: in mlv-app, pytest tests/coordination/test_retire_lane_worktree.py -k "b1_ or b2_ or launcher_census" (11 cases; the 6 refusal cases were RED before their fix, the older-than-worktree and other-worktree cases pin no over-refusal).
supersedes: none
evidence: measured
