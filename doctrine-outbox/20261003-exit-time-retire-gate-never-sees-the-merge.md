---
target: TRAPS.md
kind: trap
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-03 - a retire-if-merged gate that runs at lane exit judges the worktree before its PR merges, so it always answers unmerged and nothing asks again: re-ask the same gate about every other worktree at the next exit

**Symptom (measured):** lane worktrees accumulated without bound. About 0.7 GiB each, 11 to 20 created per day, and free space on the system drive fell from 89 GiB to about 53 GiB in under three weeks. Retiring 32 merged, clean, idle worktrees by hand recovered about 24 GiB; each had already been kept by the exit-time gate.

**Cause:** the gate (retire only when clean, idle and merged into the merge target) is correct. Its only call site is the lane's own exit, and a lane exits before its pull request merges. So it answers `unmerged` every time, and no later event re-asks. The rule was right; the trigger was wrong.

**Fix pattern (no new deletion rule):**

1. At each lane exit, after the lane's own retire step, run a sweep that calls the SAME gate on every OTHER worktree under the scratch root, excluding the lane's own worktree and run directory.
2. Young guard: skip any worktree created or checked out within N hours (default 6). A fresh worktree at the merge target is clean and "merged" before its lane starts.
3. Time budget (default 180 s). When it runs out, stop and report how many candidates were not reached; never assume done.
4. Record the result in the lane's exit receipt (considered, retired, young, kept by reason, not reached, error); a sweep failure is `cannot-determine` and never fails the lane.

Steady-state disk use is then bounded by unmerged worktrees, not by history. Known limit: the young guard reads the index mtime, which the gate's own status call can refresh; that only delays retirement.

**Pinned by:** seven sweep cases in `tests/coordination/test_retire_lane_worktree.py`; deleting the young guard or the root filter turns a named test red.

**Falsifier (a sibling can run it):** for each worktree from `git worktree list --porcelain` other than the main checkout, test `git merge-base --is-ancestor <worktree-HEAD> <merge-target>` and `git -C <worktree> status --porcelain`. If a worktree that is an ancestor of the merge target, clean and older than N hours still exists after several later lane exits, the retire gate has no post-merge trigger. After the fix, a worktree whose PR merged after its lane exited is gone at the next lane exit, while a young, an unmerged and a dirty one all survive.

General rule: a cleanup gate whose precondition only becomes true after its owning actor has exited needs a second, later trigger.
