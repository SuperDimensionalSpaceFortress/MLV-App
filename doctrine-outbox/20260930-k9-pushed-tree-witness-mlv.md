---
target: RECEIPTS.md
kind: receipt
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-09-30 — K9 resume witness: where landing is a PR merge the CI step is the gate and pre-push is an early warning; load the witness from a pinned ref and judge only the tracked resume spine

**Built:** a stdlib Python witness, `tools/repo_hygiene/k9_witness.py`, judges a git tree (never the working tree) for kernel clause K9, "resume from durable artifacts alone". Four checks: the resume spine (entry doc, instance map, rotation doc) exists as exact-case blobs; the entry chain links and the map's K9 row has four non-empty cells; every backticked path the row names, spaces included, is a tracked regular-file blob that git can read; no derived value (sha, session id, pid) sits in the row or the rotation doc. Unreadable means exit 2 (UNKNOWN), never red and never pass. It covers the tree object, the spine blobs and each resolved pointer blob, and nothing else. Every git call goes through one helper that raises on failure; mutating it to return empty on error produced a false PASS. A first review found two pointer-check gaps (an unread pointer blob; whitespace in paths); both are closed and test-pinned.

**What changed from the conjugal pattern, and why:**

1. **The landing seam is the PR merge.** MLV-App lands through pull requests, so the gate is one step inside the existing CI job, run on the merge commit. The pre-push layer is opt-in per clone: it shortens the loop, but a fresh clone has no hook.
2. **The witness is loaded from a pinned ref, not the pushing checkout.** The hook extracts the script from the fork's master into a temp file and runs that copy, so a branch that edits the witness cannot weaken its own judge. An unreadable pinned ref refuses the push ("fetch fork"). The hook reads stdin once and restores it for later layers; a negative control without the restore starved a probe layer.
3. **The scope is the tracked resume spine only.** Board-local state is exempt and listed, not judged; CI cannot see it. A ref is judged only when its pushed range touches a spine path, the checkpoint script or the witness; the tip is judged, so a red earlier commit does not block a green tip, and a stale branch lacking the spine passes when it touches none of it.

**Baseline:** all 67 first-parent landings on the fork's master since the instance map first appeared judge PASS. Of the 20 most recent spine-touching landings, 17 judge RED, all older than the map.

**Falsifier:** run `python -m tools.repo_hygiene.k9_witness tree HEAD`, expect `verdict=PASS`, exit 0. Commit a K9 row naming a nonexistent path, expect `failed=C3`, exit 1. Delete a loose spine or pointer blob, expect exit 2.
