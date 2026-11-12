---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/worktree-guard-parent-pid-chain-needs-creation-time-order
rule: a guard that excludes "the caller's own ancestors" from its holder census must not walk ParentProcessId by pid alone: the column is written at creation and never updated, so once a parent exits Windows hands its pid to a new process and the walk drops that unrelated live process from every holder check. Order each parent/child pair by the kernel's monotonic creation sequence (ProcessSequenceNumber, NtQueryInformationProcess class 64), a parent being created before its child. The wall clock alone fails both ways: after a backward clock step a GENUINE parent carries the later stamp and is refused as a holder (review r1, ANC-01). Use the wall-clock order only for a pair whose sequence cannot be read, and end the chain when either creation time is unreadable. An absent parent row exited and excludes nothing.
mechanism: 2026-11-12, hosted Tests run 43201503884 (job 131125818658, #802): the retirement test saw kept/in-use from the rename probe with holders=[] while its holder (pid 732, a recycled low pid) was alive with the worktree as cwd. "corpus files: 0" is true of any -Command holder and is not the cause. Reproduced with a Win32_Process table whose chain-top parent was created after its child: the gate returned would-retire for a live holder.
applies: mlv-app
check: in mlv-app, pytest tests/coordination/test_retire_lane_worktree.py -k "recycled_parent or ancestor_chain or genuine_parent or genuine_ancestor_whose or real_process_creation"
supersedes: none
evidence: measured
