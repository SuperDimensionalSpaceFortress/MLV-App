---
target: TRAPS.md
kind: trap
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-06 — on a machine shared by several fleet projects, one project's old scratch and worktree directories under a shared temp root were the largest reclaimable item on the system drive while other projects' builds were gated on disk

**Trap:** a census of the system drive's root attributed the largest single directory (about 126 GiB, a shared temp root) to two owners. About 47 GiB was the owner's source footage, which must never be touched. About 79 GiB was scratch, temporary and worktree directories written by one other fleet project (AirMyPC), the newest written within the hour of the census, so it was live. The drive had fallen to about 27.6 GiB free, below MLV's build-admission floor, so work was held while the biggest reclaimable pile belonged to someone else.

**Wrong remedies, rejected:** deleting by age from the shared root would have deleted a live neighbour's data, and could not tell footage from scratch by location. Clearing shared tool caches (package caches of two tools in three folders, about 17 GiB together) was also refused: another project's .NET builds were restoring at the time, and clearing a global package folder mid-restore breaks them.

**Rule:** a project must not delete another project's data. Space on a shared drive is reclaimed by its writer: each project keeps retention for the scratch it writes, age-bounded, keep-newest and liveness-checked (nothing written recently, nothing held by a live process). The fleet adds a census that attributes system-drive space to its owning project, so the ask reaches the owner instead of guesswork.

**Ask:** AirMyPC adopts age-bounded retention for its scratch and worktree directories older than 7 days under the shared temp root, with a liveness check and a manifest before deletion. MLV did this for its own lane workdirs and run folders in a separate change.

**Falsifier:** with one project's scratch older than 7 days present under the shared root and a second project's disk gate refusing, the writer's retention must reclaim its own old directories (receipt shows manifest, bytes freed, no entry younger than 7 days and no live-process path) and the second project's gate must then clear. If the drive stays below the floor while old scratch of one writer remains, retention is not running; if any other project's entry is deleted, the attribution is wrong.

**Guard:** none yet (cross-project; MLV may only report, and AirMyPC's retention and the fleet census do not exist).
