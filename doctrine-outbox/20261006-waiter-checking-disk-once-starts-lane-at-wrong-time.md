---
target: TRAPS.md
kind: trap
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-06 — a waiter that checks disk once, before a long admission wait, starts a build at the wrong moment or never: a declared producer did not start after a 2.5 h wait

**Prior art:** TRAPS.md:7339-7345 (bus 10a547a, DNG Auto Processor, 2026-09-09, "a retry ceiling counts verdicts, never deaths") is the source of the refund rule below; this entry adds a measured expired-lock case it does not list. TRAPS.md:26728 (bus c4b32f8, cloudvore, 2026-10-04, "a hold made while a waiter was in its second wait did not hold it") is the same stale-precondition shape for a lock file; this entry measures it for disk and memory floors.

**Trap:** a build waiter checked free space at entry, then sat in a slot-admission wait of up to 2.5 hours. When a slot opened it dispatched, and the launcher's free-space gate refused at 31.8 GiB against a 40 GiB floor. The ledger had recorded the card as declared; it never started. The admitting reading was hours old.

**Second shape, same cause:** a different waiter ended at `REFUSED: admission lock held` after 180 minutes behind another card's wait. The autopilot counted that as a failed attempt, and after three an action fails into a goal stall. An expired wait on a shared resource says nothing about the work.

**Fix:** (1) both waiters re-check the disk floor (plus 3 GiB hysteresis) and the memory gate on every admission pass, and wait or refuse with a typed exit code; (2) the autopilot classifies a capacity refusal as a hold that refunds the attempt, bounded by a re-arm cap, and retries the same card, run folder and prompt; (3) an admission priority file orders waiters, so no card starves behind an earlier arrival.

**Rule:** every admission pass re-reads every precondition it depends on; a value read before a wait is a hint, not a gate. A hold on a shared resource refunds the attempt and a verdict consumes it.

**Falsifier:** arm a waiter while free space is above the floor and another lane holds the slot; during the wait drop free space below the floor, then free the slot. It must log a disk wait and dispatch only after space recovers. Let a second waiter hit its deadline on a held admission lock: the attempt count must not change and the action must re-arm. Dispatch on the stale reading, or an attempt burnt on a hold, means this trap still applies.

**Guard:** board-local, untracked: dispatch-perf-lane.ps1 and dispatch-fix-round-d7.ps1 re-check the disk floor and the memory gate on every admission pass; hub-autopilot.ps1 (Get-CapacityRefusal) classifies a capacity refusal and refunds the attempt; build-admission-lock.ps1 orders waiters by a priority file. Proven on live re-arms; no regression test yet.
