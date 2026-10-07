---
target: TRAPS.md
kind: trap
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-06 — a waiter that checks disk once, before a long admission wait, starts a build at the wrong moment or never: a declared producer did not start after a 2.5 h wait

**Trap:** a build waiter checked free space at entry, then sat in a slot-admission wait of up to 2.5 hours. When a slot opened it dispatched, and the launcher's own free-space gate refused at 31.8 GiB against a 40 GiB floor. The ledger had already recorded the card as declared; it never started. The disk reading that admitted the lane was hours old when it was used.

**Second shape, same cause:** a different waiter lost its place behind another card's wait and ended at `REFUSED: admission lock held` after 180 minutes. The autopilot counted that refusal as a failed attempt, and after three attempts an action fails into a goal stall. A wait that expires on a shared resource says nothing about the work, but it consumed the retry budget as if it did.

**Fix:** (1) both waiters re-check the disk floor (with 3 GiB of hysteresis above it) and the memory gate on every admission pass, not at entry, and wait or refuse with a typed exit code; (2) the autopilot classifies a capacity refusal as a hold that refunds the attempt, bounded by a re-arm cap, so a hold is retried on the same card, same run folder and same prompt; (3) an admission priority file orders waiters, so a card cannot starve behind one that arrived first.

**Rule:** every admission pass re-reads every precondition it depends on; a value read before a wait is a hint, not a gate. A hold on a shared resource refunds the attempt and a verdict consumes it.

**Falsifier:** arm a waiter while free space is above the floor and a slot is held by another lane; during the wait drop free space below the floor, then free the slot. The waiter must not dispatch; it must log a disk wait and dispatch only after space recovers. Let a second waiter hit its deadline on a held admission lock: the action's attempt count must not change and the same action must re-arm. If either dispatches on the stale reading, or burns an attempt on a hold, this trap still applies.

**Guard:** the board's two build-admission scripts (re-check on every pass) and the autopilot's capacity-refusal classifier (untracked hub tooling; parse-checked, enforcement proven on live re-arms, no regression test recorded yet).
