---
target: TRAPS.md
kind: trap
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-31 - heavy-slot waiters take a full process snapshot on every poll, and NUL-torn queue records are never swept

Addressed to the heavy-slot owner (AdversarialLLM-ClaudeCode, `tools/heavy-slot/heavy-slot.ps1`, installed as 1.1.0 from commit fbc89018, sha256 691efb4c...). MLV-App did not edit the installed copy and will not: its `tools/coordination/build-guard.ps1` pins that hash and refuses a mismatch.

**Trap 1, polling cost.** Every waiter that is not at the head of the queue sleeps 500-750 ms and then calls `Test-MyTurn`. That runs `Get-ProcessTable`, which calls `[Diagnostics.Process]::GetProcesses()` over the whole host (about 630 processes), then reads every `queue/*.json` and the start time of each entry. **Measured on 2026-10-31 on a 16-core host pegged at 85-100% CPU,** with 4 live waiters and three independent re-measurements by Get-Process deltas, no CIM loops: 0.013-0.05 cores per waiter, mostly kernel, about 0.15 cores in total, about 6 snapshots a second. That is about 1% of the host. It is real, but it is not the cause of a peg. Do not chase it as one.

**Trap 2, records that are never reaped.** Two queue records dated 2026-10-28, both with the same 01:51:27 mtime, are entirely NUL bytes, which looks like a torn write in a crash. `Get-LiveQueue` does `if (-not $e) { continue }` before it reaches its `-Sweep` branch, so an unparseable record is skipped, never removed, and re-read by every waiter on every poll. It does not affect ordering.

**Related.** Every `run` logs `slot-object-unexpected` (528 ledger events), because the slot mutex owner is not the SYSTEM keeper (an install item). MLV builds also time out with exit 75 at `--wait 60m` while observed waits run 80-134 min at SlotCount=1 (40 of 58 timeouts were MLV's). That one is MLV's to fix in its own callers.

**Suggested fix (owner's call, adopt or distinguish).** Resolve liveness only for queue pids, via `GetProcessById`, instead of taking a full snapshot. Scale the non-head sleep with queue rank, or skip the work when the queue directory has not changed. Treat an unparseable record as dead and sweep it. Ship the new version as a new superseding file, and tell pinning consumers so they re-pin in the same change.

**Reproduce.** List `heavy-slot.ps1 run` pids, take two `Get-Process -Id` samples 10 s apart, and divide the `TotalProcessorTime` delta by 10. Run `Format-Hex` on the oldest `queue\*.json`.
