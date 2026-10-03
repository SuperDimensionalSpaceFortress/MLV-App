---
target: TRAPS.md
kind: trap
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-03 - a retire-if-merged gate at lane exit runs before the PR merges, so it always answers unmerged: re-ask it about every other worktree at each exit, least-recently-examined first, or the budgeted sweep starves its tail

**Symptom (measured):** lane worktrees piled up, about 0.7 GiB each; free space fell from 89 to about 53 GiB. Retiring 32 merged, clean worktrees by hand recovered about 24 GiB.

**Cause:** the gate (clean, idle, merged) is right, but its only call site is the lane's own exit, which precedes the merge. The trigger was wrong, not the rule.

**Fix pattern (no new deletion rule):**

1. At each lane exit, run the SAME gate on every OTHER worktree under the scratch root.
2. Young guard: skip worktrees created or checked out within N hours (default 6); a fresh one is clean and "merged".
3. Time budget (default 180 s); on expiry report `notReached`.
4. Receipt: considered, examined, retired, young, kept by reason, notReached, elapsedMs, error. Never fail the lane.

**Second trap (first real sweep: 21 candidates, 10 examined, 11 never reached):** a budgeted sweep in a STABLE order starves its tail whenever the head is permanently kept. The gate cost about 36 s per worktree, so 180 s reached about 5 to 10; the same long-lived unmerged or dirty worktrees led `git worktree list` every time, so a merged one behind them was never examined. Steps 1 to 4 alone do NOT guarantee "gone at the next exit".

**Fair-order rule:** visit least-recently-examined first. After the gate returns for a worktree (anything but retired), write a UTC timestamp file into its git admin dir (`<common>/worktrees/<name>/`; git ignores it, `worktree remove` deletes it, nothing enters the worktree). Sort ascending, missing = oldest, ties in list order, no randomness. A stamp write failure is counted, never fatal. The young guard reads only creation time, HEAD and index mtimes, so the stamp cannot make a worktree look young.

**Measure first:** about 93% of the gate was two process scans (a per-ancestor `Get-CimInstance -Filter` loop, 31 s; the full list, 2.4 s); ten git calls totalled 1.7 s. One shared process snapshot per sweep fixed it.

**Pinned by:** `tests/coordination/test_retire_lane_worktree.py`: capped at one gate call per sweep, three sweeps examine three different worktrees and retire a merged one listed last; removing the sort turns two tests red.

**Falsifier:** a merged, clean, old worktree surviving several sweeps means no post-merge trigger or a starved tail; compare the receipt's `examined` with `considered`. After the fix, repeated budget-limited sweeps examine different worktrees.

General rule: a cleanup gate whose precondition becomes true only after its owner exits needs a later trigger, and a budgeted sweep needs a fair order.
