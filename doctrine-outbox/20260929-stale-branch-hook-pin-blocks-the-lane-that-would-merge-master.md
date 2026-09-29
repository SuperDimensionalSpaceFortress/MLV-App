---
target: TRAPS.md
kind: trap
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-09-29 — a stale branch cannot start the editing lane that would merge master: the runner's hook pin refuses it first, so sync only the guard files, then let the lane merge

**Symptom (measured):** an editing lane dispatched onto a branch that was behind master threw `hook-not-enforced: worktree hook sha256=<A> != receipt hookSha256=<B>` at the runner's pre-launch gate and produced no lane output at all (empty stdout, a 452-byte error file). The lane's first job was to merge master into the branch (three tooling files conflict, and resolving them is the lane's job, not the orchestrator's), so the branch was stuck behind the very gate the merge would satisfy.

**Cause:** the runner refuses `-AllowEdits` unless the worktree's command-guard hook hash equals the hash pinned in the enforcement receipt, which tracks the hook on master. A branch cut before a hook change fails the pin by construction, and the gate runs before the lane can act. This mirrors the 2026-09-27 entry (receipt stale after a merge to master); here the receipt was current and the branch old.

**What worked (sequence, each step with a receipt):**

1. The dispatch above failed: empty stdout, `DISPATCH_UNPROVEN`, `hook-not-enforced` in the error file.
2. One named commit (`c8f8b72a`) brought over only the guard: the hook script and the guard's data file, checked out from the fork's master. A saved `git show --stat` receipt gives its size: 2 files changed, 86 insertions, 2 deletions. It ran as a pre-step script (the first version had a parse error and never ran; the second did).
3. The next dispatch wrote a dispatch-proof reading `PROVEN` at 13:47:04Z.
4. That lane then did the full master merge itself: merge commit `d899b358` with the three conflicted files resolved by union (none of them a guard file) and exited 0.

**Rule:** when `hook-not-enforced` refuses a stale branch, do not loosen or re-pin the receipt and do not hand-merge master from the orchestrator. Sync the guard files alone in one named commit (never a broad checkout, never `add -A`), confirm the worktree hook hash now equals the receipt's, then dispatch the lane to merge and resolve. Before dispatching any editing lane onto a branch older than the last hook change, do the sync first.

**Falsifier:** compute the hook's SHA-256 in the worktree and read the receipt's `hookSha256`; unequal means the next editing dispatch will refuse. After the sync commit the two match and a probe dispatch with edits starts.

Relates to `TRAPS.md` > "Appended by MLV-App (hub session), 2026-09-27 - three measured traps from one day of lanes on a live venue", item 1 ("Merging a guard-hook change leaves the hook-enforcement receipt pinned to the old hash").
