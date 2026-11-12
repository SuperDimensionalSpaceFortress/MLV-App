---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/worktree-guard-parent-pid-chain-needs-creation-time-order
rule: a guard that excludes "the caller's own ancestors" from its holder census must not walk ParentProcessId by pid alone. That column is written at process creation and never updated, so once a parent exits Windows hands its pid to the next new process, and the walk steps onto an unrelated live process and drops it from every holder check. A real parent is created before its child: end the chain at a live parent row whose creation time is later than the child's, and at one whose creation time (or the child's) cannot be read, and leave that process in the census to be judged like any other. An absent parent row is a parent that already exited and excludes nothing.
mechanism: 2026-11-12, hosted Tests run 43201503884 (job 131125818658, #802): the end-to-end retirement test saw the gate return kept/in-use from the rename probe with holders=[] and cwdUnknown=[] while its holder (pid 732, a low recycled pid) was alive with the worktree as its cwd. The test's own diagnostics showed "corpus files: 0", which is true of any `-Command` holder and is not the cause. Reproduced without the host by feeding the snapshot a Win32_Process table where the chain's top parent is a live process created after its child: the gate returned would-retire for a live holder whose cwd was the worktree. Only the rename probe stood between that and a worktree deleted under a live process.
applies: mlv-app
check: in mlv-app, pytest tests/coordination/test_retire_lane_worktree.py -k "recycled_parent or ancestor_chain" (3 cases RED before the fix; the real-ancestors case passes on both).
supersedes: none
evidence: measured
