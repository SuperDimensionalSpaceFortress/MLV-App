---
target: TRAPS.md
kind: trap
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-04-27 — pasting screenshots into a chat client inlines base64 image data into the session transcript and freezes the UI renderer, presenting as a crash

**Symptom (measured):** a desktop chat client's UI stalled/froze when the user tried to ask a follow-up question, well before the model's own context window was anywhere near full (roughly 30-40% used). This looked like a session crash but the underlying agent process had logged a clean stop — the failure was in the *renderer* trying to scroll/re-render an oversized transcript file, not in the agent.

**Cause:** when a screenshot is pasted directly into the chat, the client stores it as a base64-encoded image inline inside the per-session transcript file (a `.jsonl`-style append log). A handful of pasted screenshots is enough to push that transcript file past tens of megabytes. Diagnosis: `awk '{print length, NR}' transcript.jsonl | sort -rn | head` finds line lengths in the hundreds-of-KB range; those lines contain an inline base64 image payload. Archiving the chat in the UI does not shrink or delete the underlying transcript file — it only stops that chat from being the *active*, rendered one — so archiving "fixes" the freeze only by no longer opening the bloated file, not by reclaiming anything.

**Rule:** never paste a screenshot directly into a chat session with a client known to inline images into its transcript log. Save the image to disk (anywhere reachable by the agent) and share the **path** instead — the agent can still read and reason about the image via a file-read tool, and the transcript stays small. Treat any transcript file approaching single-digit megabytes as being in the danger zone for UI sluggishness, and anything past ~25MB as a near-certain freeze on the next render. Starting a fresh session periodically is a reasonable mitigation for any workflow that unavoidably involves many screenshots, since transcript bloat is never trimmed automatically.

**Falsifier:** check a frozen session's transcript file size and grep for `"type":"image","source":{"type":"base64"` — its presence at a large file size, correlated with when the freeze started, confirms this cause over an actual process crash (which would show a non-clean stop in the agent's own logs).
