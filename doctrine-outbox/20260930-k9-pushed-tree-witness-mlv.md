---
target: RECEIPTS.md
kind: receipt
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-09-30 — K9 resume witness: where landing is a PR merge the CI step is the gate and pre-push is an early warning; load the witness from a pinned ref and judge only the tracked resume spine

**Built:** a stdlib Python witness, `tools/repo_hygiene/k9_witness.py`, judges a git tree (never the working tree) for kernel clause K9, "resume from durable artifacts alone". Four checks: the resume spine (entry doc, instance map, rotation doc) exists as exact-case blobs; the entry chain links and the map's K9 row has four non-empty cells; every tracked path the row names resolves; no derived value (sha, session id, pid) sits in the row or the rotation doc. A tree it cannot read exits 2 (UNKNOWN), never red and never pass. Every git call goes through one helper that raises on failure. A mutation making that helper return empty on error produced a false PASS for a tree with a missing spine blob.

**What changed from the conjugal pattern, and why:**

1. **The landing seam is the PR merge.** MLV-App lands through pull requests, so the gate is one step inside the existing required CI job, run on the merge commit. The pre-push layer is opt-in per clone: it shortens the loop but a fresh clone has no hook.
2. **The witness is loaded from a pinned ref, not the pushing checkout.** The hook extracts the script from the fork's master into a temp file and runs that copy, so a branch that edits the witness cannot weaken its own judge. An unreadable pinned ref refuses the push ("fetch fork"). The hook reads stdin once and restores it with a here-document for later layers; deleting that restore starved a probe layer in a negative control.
3. **The scope is the tracked resume spine only.** Board-local state is exempt and listed, not judged; CI cannot see it. A ref is judged only when its pushed range touches a spine path, the checkpoint script or the witness itself; the tip is what gets judged, so a red commit earlier in the range does not block a green tip, and a stale branch that lacks the spine passes when it touches none of it.

**Baseline:** all 67 first-parent landings on the fork's master since the instance map first appeared judge PASS. Of the 20 most recent spine-touching landings, 17 judge RED, all older than the map, failing C1, C2 and C3 on the missing file only.

**Falsifier:** on any checkout run `python -m tools.repo_hygiene.k9_witness tree HEAD` and expect `verdict=PASS` and exit 0. Commit a K9 row naming a nonexistent tracked path and expect `verdict=RED failed=C3` and exit 1. Delete a loose spine blob and expect exit 2.
