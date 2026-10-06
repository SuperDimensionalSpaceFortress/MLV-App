---
target: TRAPS.md
kind: trap
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-05 — a pagefile swing mistaken for disk exhaustion: free space fell from about 55 to 10.9 GiB and recovered by itself, because the system-managed pagefile grew about 33 to 75 GB under several parallel C++ builds

**Trap:** the owner reported the drive "super low". Free space on the system drive had fallen from about 55 GiB to 10.9 GiB and minutes later was back near 55. Reclaim receipts showed free-space swings of +5.5 and -4.2 GiB while reclaiming about nothing. The cause was not data: the system-managed pagefile was 75.4 GB at the low point and shrank to 33.0 GB, during a memory-commit peak (29 GiB committed on a 32 GB host) with several lanes compiling C++ at once.

**Evidence method, including a mistake:** a growth probe found no folder over 500 MB new in 4 hours under any lane, worktree, run-folder, temp, venue or agent-log root; temp gained 1.7 GiB that day. The first scan was wrong: it filtered by each directory's own modification time, which does not change when files deep inside it change. The working probe compares recursive file times. The desktop app's large "bytes written" counter was churn, not retained data.

**Cost beyond the scare:** two armed goal-chain launches were refused by the dispatcher's 55 GiB free-space gate while free space hovered at 54.5 to 54.9, so cards the owner was waiting on did not start for hours, and the ledger named lanes that never had a receipt.

**Remedy:** (1) cap concurrent building lanes at 2, enforced in the dispatcher (it waits, then refuses with a retryable exit code, counting live producers by run folder and ignoring read-only review and design lanes), not in prose; (2) dispatch no new building lane below 40 GiB free; (3) read the pagefile's size before calling disk low: a swing there is memory pressure, and the remedy is fewer concurrent builds, never deleting evidence; (4) log the pagefile size beside free space in the reclaim tick, board heartbeat and disk guard. A fixed pagefile size is a system setting left to the owner.

**Falsifier:** during a memory peak with three C++ build lanes running, record free space and the pagefile's size each minute: free space should dip and recover in step with the pagefile while a recursive-file-time probe reports no new data. Then try to dispatch a third building lane: the dispatcher must wait or refuse, and live building lanes must never exceed 2. If free space stays down after the pagefile shrinks, or the probe finds new bytes, the cause was data and this entry does not apply.
