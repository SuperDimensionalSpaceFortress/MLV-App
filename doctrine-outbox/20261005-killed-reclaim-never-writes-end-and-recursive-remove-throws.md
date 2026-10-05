---
target: TRAPS.md
kind: trap
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-05 — a reclaim killed at its time bound never writes its end line, and `Remove-Item -Recurse -ErrorAction SilentlyContinue` still throws a terminating error: key the in-progress guard on a live process, and wrap every removal in try/catch

**Trap 1: start/end receipt lines are not a liveness signal.** A manual reclaim (archive each lane worktree to a network share, verify count and bytes, then `git worktree remove`) was stopped at its one-hour background bound after three of eight retirements. Its receipt has a start line and no end line, and always will. The new unattended sweep checks for a hub reclaim in progress before running. A guard that only compared start and end lines would have treated the killed run as live forever and blocked every later sweep. The guard now also requires a live process whose command line names that reclaim's folder. A dead writer means the run is over, whatever its receipt says. The killed run was safe to restart because removal only follows a verified copy. The re-run skipped the three absent worktrees, completed the half-written copy and retired the rest.

**Trap 2: `-ErrorAction SilentlyContinue` does not silence every failure of `Remove-Item -Recurse -Force`.** On an OS temp tree that another process was pruning concurrently, a vanished child raised a terminating "path not found" IOException. That ended the sweep's first scheduled run right after its temp section, so every later section (merged-PR artifact pruning) silently never ran, and the run still exited 0 because the scheduled-task wrapper reports how the launcher exited. The fix is to wrap each removal in try/catch, give each section and each run folder its own try/catch, and write a manifest before deleting. Then a removal that throws costs one entry, not the rest of the run, and the receipt's reclaimed bytes are measured, not assumed.

**Also learned:** the size of a manual archive-then-retire is set by the network copy (about 20 minutes per 0.7 GiB worktree here), not by the delete. Budget the bound from the copy, or do in-place reversible work (LZX compression) first, so a killed run still leaves the cheap reclaim done.

**Falsifier:** start a reclaim, kill it before its end line, and confirm the unattended sweep's next tick reports the hub reclaim finished and sweeps, rather than skipping. Inject a missing child path into a temp tree under removal, and confirm the receipt still contains the later sections.
