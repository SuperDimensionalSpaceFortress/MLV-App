---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/worktree-guard-unreadable-script-is-unknown
rule: a worktree guard that finds holders through the script text a live process can load treats a named script that exists and cannot be read as an unknown holder and refuses, naming the pid and the file. It never reads as a no-hit. A confirmed-missing candidate stays a no-hit, because the path regex matches many words that are not files. Tell the two apart with a call that throws (File.GetAttributes), never with FileInfo.Exists, which reads false for a denied file as well as a missing one. A rename probe does not settle this row: it sees only handles under the tree, and such a holder may have none. A holder row built by a caller may omit its optional fields (file, file error, creation time); read each one safely, so a missing field still yields the refusal that names the pid instead of a thrown error that every caller reports as an anonymous failure. Measure a new refusal against the live process table through the non-deferred path before shipping it, since a holder row refuses every worktree on the host.
mechanism: 2026-11-05, reproduced through the full gate: a live holder with its cwd outside the tree, its only link a dot-sourced sibling held open FileShare.None, was retired with the rename probe passing and the worktree deleted under it. The corpus dropped the file in a catch, and the index skipped the holder because its cwd was known. On this host the new row was 0 of 6 snapshots of about 600 processes. The size, file-count and depth bounds have the same silent gap and stay open: 5-7, 19-25 and 32-47 live processes per snapshot, so refusing on them would stop every retirement.
applies: mlv-app
check: in mlv-app, pytest tests/coordination/test_retire_lane_worktree.py -k "full_gate or live_refusal_guard_refuses_an_unreadable" (10 cases RED before the fix; 3 absence and dead-process controls pass on both).
supersedes: none
evidence: measured
