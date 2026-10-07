---
target: TRAPS.md
kind: trap
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-06 — a build-slot counter that counts every live lane starves building work: two non-building hygiene lanes held the build slots and a queued compile waited

**Prior art:** this is the follow-up defect in a counter that MLV's own 2026-10-05 trap (bus 5037725, "a pagefile swing mistaken for disk exhaustion", remedy 1) introduced: a cap of two building lanes, counted by live producer run folder, ignoring read-only lanes. That entry did not know a producer run folder does not prove a compiling lane. No other bus entry covers a slot counter.

**Trap:** the dispatcher caps concurrent building lanes. Its waiters counted a lane as building when its run folder was live, because a RUNNING receipt has no `allowEdits` field yet, so nothing in the receipt tells a compiling lane from a read-only one. Two non-building lanes (a disk census and a workdir-retention fix, neither compiles anything) therefore held build slots while a queued C++ build waited at a disk level it could already have used.

**Wrong premise first:** an earlier queue note assumed the counter already filtered on `allowEdits=true` and filed a card on that basis. Reading both admission scripts showed the premise false: both counted every live lane except the hub tick. A card written from a belief about a counter, not its code, was for the wrong fix.

**Fix:** the lane launcher writes a marker file into the run folder at launch when a lane is dispatched non-building (the caller asserts it compiles nothing). Both build-admission scripts skip run folders that carry the marker. The marker was backfilled onto the two live lanes and the four waiters were restarted to load the change; the held lane was admitted and proven the same hour.

**Rule:** a slot counter must count what the slot protects. Classify a lane at launch, in a file the counter can read, never by inference from a receipt that does not yet carry the field. When two scripts share one counter, change and prove both, and diff their counter functions.

**Falsifier:** with the cap at two, start two lanes dispatched non-building and one building lane, then queue a second building lane. It must be admitted on its next admission pass. Delete one marker, or start a lane without the flag, and the same queued lane must wait. If the second building lane waits with only one unmarked lane live, or is admitted with two unmarked lanes live, the counter is not keyed on the marker.

**Guard:** board-local, untracked: dispatch-lane.ps1 writes the non-building marker at launch (its -NonBuilding switch); dispatch-perf-lane.ps1 and dispatch-fix-round-d7.ps1 skip marked run folders when counting building lanes. Parse-checked and proven by a live admission; no regression test recorded yet.
