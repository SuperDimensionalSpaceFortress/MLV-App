---
target: TRAPS.md
kind: trap
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-09-29 — a stale branch cannot start the editing lane that would merge master: the runner's hook pin refuses it first, so sync only the guard files, then let the lane merge

**Symptom (measured):** an editing lane dispatched onto a branch that was behind master threw `hook-not-enforced: worktree hook sha256=<A> != receipt hookSha256=<B>` at the runner's pre-launch gate and produced no lane output at all (empty stdout, a 452-byte error file, a dispatch-proof file). The lane's first job was exactly to merge master into the branch, and a full master merge conflicted in three tooling files, and resolving them was the lane's job, not the orchestrator's. The branch was stuck behind the very gate the merge would have satisfied.

**Cause:** the runner refuses `-AllowEdits` unless the worktree's command-guard hook hash equals the hash pinned in the enforcement receipt, which tracks the hook on master. A branch cut before a hook change carries the old hook, so it fails the pin by construction, and the gate runs before the lane can do anything about it. This is the branch-side mirror of the 2026-09-27 entry, where the receipt went stale after a merge to master; here the receipt was current and the branch was old.

**What worked:** one named commit on the branch that brought over only the guard: the hook directory and the guard's data file, checked out from the fork's master (2 files, +86/-2 lines). The orchestrator ran it as a small pre-step script (the first version had a PowerShell parse error and never ran; the second did). The next dispatch passed the gate, and the lane did the full master merge itself: a merge commit with the three conflicted files resolved by union, then its own fixes, ending `exit=0`. The guard files then merged cleanly because their content was already identical.

**Rule:** when `hook-not-enforced` refuses a stale branch, do not loosen or re-pin the receipt and do not hand-merge master from the orchestrator. Sync the guard files alone in one named commit (never a broad checkout, never `add -A`), confirm the worktree hook hash now equals the receipt's, then dispatch the lane to merge and resolve. Before dispatching any editing lane onto a branch older than the last hook change, do the sync first.

**Falsifier:** compute the hook's SHA-256 in the worktree and read the receipt's `hookSha256`; unequal means the next editing dispatch will refuse. After the sync commit the two match and a probe dispatch with edits starts.

Relates to `TRAPS.md` > "Appended by MLV-App (hub session), 2026-09-27 - three measured traps from one day of lanes on a live venue", item 1 ("Merging a guard-hook change leaves the hook-enforcement receipt pinned to the old hash").
