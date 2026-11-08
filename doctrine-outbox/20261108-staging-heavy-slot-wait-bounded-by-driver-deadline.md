---
target: RECEIPTS.md
kind: receipt
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-11-08 — Staging build lost its machine HeavySlot turn: the wait is now the driver's deadline, not a fixed 60 min

**Before:** `tools/coordination/build-guard.ps1` always passed `--wait 60m` to heavy-slot. A venue staging build was refused at 60 min (heavy-slot code 75), and the stage chain's retry 300 s later took a NEW ticket behind every newer waiter. The slot admits oldest first, so the turn was lost on the caller's side. Observed 2026-11-08: timed out 11:12:39Z-12:12:39Z, re-queued as #2 of 6, staged only at 13:10:18Z.
**Now:** the caller's deadline (`Invoke-BuildGuard -Options @{ HeavySlotDeadlineUtc = ... }`, the assembler's `-HeavySlotDeadlineUtc`, or env `MLV_HEAVY_SLOT_DEADLINE_UTC`; the option wins) sets `--wait` to what is left of it: 0 once passed, one week at most, sampled just before launch. No deadline: 60 min, unchanged. The guard prints `BUILD_HEAVY_SLOT_WAIT_BOUND ... source=deadline ... default=60m` when a deadline set the bound; an unparsable one is `BUILD_HEAVY_SLOT_REFUSED reason=invalid-option`, exit 2. `Invoke-VenueStageNightly.ps1` exports its deadline. The machine HeavySlot is not edited.
**The chain (r3):** the incident chain was driver -> run-dir `stage-chain.ps1` copy -> run-dir `stage-one.ps1` copy -> assembler -> guard, and both copies dropped the deadline. No generator writes them; each lane hand-copies the last run's. So the stage chain is now tracked: `tools/profiling/dual-venue/Invoke-VenueStageChain.ps1 -Sha <40> -DeadlineUtc <ISO-8601> -StageOne <run>\stage-one.ps1` exports the deadline to every child (copies that omit the parameter still hand it on by inheritance), passes `-DeadlineUtc` to a stage-one that declares it, restores the caller's value, and refuses a non-ISO deadline. Drivers call it instead of a stage-chain copy.
**Evidence:** RED on `0a553b4e`: a scripted queue (stub `queued` mode, 130 min ahead) with a deadline 240 min out got `--wait 60m` and a fresh ticket on retry; GREEN: `--wait` about 240m, admitted on the first ticket. r3: the full chain at 61/130/180 min gets one ticket each; a chain that does not export the variable fails with two fresh tickets. Live proof is the first staged build after merge (its `BUILD_HEAVY_SLOT_WAIT_BOUND` line).
**Narrowed promise:** a driver still calling a hand-written stage-chain copy that neither exports the variable nor forwards the deadline still waits 60 min. A long wait holds the chain open until its deadline.
**Falsifier:** `pytest tests/coordination/test_build_guard.py -k "staging_deadline or full_stage_chain or no_deadline_control"` passes; on `0a553b4e` the first fails with `wait=60m`.
