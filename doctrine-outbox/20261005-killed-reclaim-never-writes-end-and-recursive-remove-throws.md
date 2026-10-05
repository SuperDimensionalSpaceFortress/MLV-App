---
target: TRAPS.md
kind: trap
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-05 — three silent failure paths in an unattended disk sweep: a killed reclaim never writes its end line, a terminating error inside try/finally still exits 0, and an older prune ran ahead of the safety gate

**Trap 1: start/end receipt lines are not liveness.** A manual archive-then-retire reclaim was stopped at its one-hour bound after three of eight retirements. Its receipt has a start line and no end line. A guard comparing only those lines would block every sweep for as long as it reads that receipt (12 hours here). The guard also requires a live process whose command line names the reclaim's folder. Restart was safe because removal only follows a verified copy.

**Trap 2: the sweep itself exited 0 after losing most of its run.** `Remove-Item -Recurse -Force -ErrorAction SilentlyContinue` on an OS temp tree raised a terminating "path not found" error. The body sat in try/finally with no catch, under `ErrorActionPreference=Continue`: the error skipped the rest of the try body (every run-folder section), the finally wrote the end line, execution resumed after the try, and the script exited 0. The scheduler passed that 0 on faithfully; only missing receipt section lines showed the truncation. Fix: try/catch around each removal, and a body catch that writes `ABORTED <message>` to the receipt and exits 2. An injected throw after the temp section exits 0 on the old copy, 2 with ABORTED on the fixed one.

**Trap 3: a guarantee stated by one section is false if another section deletes first.** An older build-copy prune ran before the new merged-PR section, deleting build folders of any finished run without resolving its PR, without the two-hour write gate and without the keep list. The first complete task sweep deleted a 1.21 GiB build folder whose PR was still open; the same receipt then listed that run as kept, PR open. Fix: one eligibility gate for every deletion under the run-folder root. Audit every deletion path in a script before publishing what it never deletes.

**Also learned:** archive-then-retire time is the network copy: about 20 minutes per worktree in the killed run, 5 in the re-run.

**Falsifier:** (1) With the sweep due and no lock, kill a reclaim after its start line; the next tick must not report "hub reclaim in progress" (sweep, or idle if not due). (2) In a test copy, make a statement after the temp section throw (a .NET delete of a missing directory): exit 2, ABORTED in the receipt. (3) Seed disposable run folders for an open PR, an unresolvable PR and a merged PR with a file written within 2 hours; a `-WhatIf` sweep's manifest lists none of them.
