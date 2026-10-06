---
target: RECEIPTS.md
kind: receipt
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-05 — an acting tick, not a status heartbeat: the OS-owned autopilot now drains a card queue by starting one bounded headless hub lane, after the board sat idle 3.7 hours with six cards queued in prose

**Before:** the hub's turn ended with six cards queued in ledger prose, no lane running and every promise done. The OS-owned autopilot executed promises, not a queue, so across about 3.7 hours it logged "beat ok" 44 times with nothing to act on, and status heartbeats recorded "idle" about 20 times. A heartbeat that only reports cannot repair what it reports.

**Built:** every autopilot beat now runs a drain step. When the board is idle (20 minutes or more since the last tick) or changed (30 minutes or more), usage is not on hold and the system drive has at least 50 GiB free, it starts exactly one headless hub lane: bounded to 40 minutes, single-instance (a stale beat lock is checked against a live process id), standing prompt pinned by hash. The lane merges what has two approvals on the current head, dispatches review keys once per sha, runs one fix round for a blocking verdict, and picks queued cards in priority order, then exits without waiting on CI. Each tick writes a DRAIN line to the autopilot log and the inflight board. Cards later moved from prose into a machine-readable queue file, with a running-lane detector that requires a live process and a matching command line. Owned by the OS user, it survives account rotation.

**Proof (same day, no interactive session):** the tick re-dispatched two producer lanes that a free-space gate had refused (new run folders, both proven by process id), started a third non-building card, keyed a new PR with both reviewers, merged a guard PR (queue enqueue pinned to the approved sha), and in the same act armed an action that re-pins the hook's enforcement receipt on merge, failing closed on a blob mismatch, a non-success hosted run or a shrinking rewrite. Eight PRs merged that day.

**Falsifier:** with no hub session open, the owner's goal unmet and one queued card with no dispatch line, an idle board must produce, within two beats after the 20-minute mark, a lane receipt for a headless hub lane and a dispatch or declaration line for that card. If the autopilot log shows consecutive beats ok or idle across 60 minutes while a queued card has no such line, usage was not on hold and free space was at least 50 GiB, the acting tick is not acting.
