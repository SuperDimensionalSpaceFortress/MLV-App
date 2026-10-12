---
target: RECEIPTS.md
kind: receipt
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-11 — a meter that cannot answer must read UNKNOWN and escalate, never "nothing owed": the fold-debt meter, an owner-requested waiter and a blind health scan each failed open on the same day

**Before:** three standing instruments failed open in one day. (1) The doctrine fold-debt meter flipped to "owed: unknown" (the recall tool exited 4, git timed out resolving the cursor) and its verdict read as nothing due while sibling cards were owed; the wrapper deleted the tool's stderr in a finally block, so the cause was lost, and nothing retried. (2) An owner-requested waiter ("run the quiet measurement when the VM is idle") ended after 12 hours with the marker the autopilot counts as success, so a miss closed the promise as done and nobody was told. (3) The playback health scan read every receipt as smooth for a day while the display was frozen, because it judged only the gaps between presented frames, and it had no canary proving it still detected anything.

**Built:** one health contract for every meter, waiter and scan: a state of OK, DUE, UNKNOWN or GAVE-UP with the time it was last healthy, shown in a "Meter health" section of the in-flight board. A meter that is not re-evaluated within three of its own cadences reads UNKNOWN (a dead meter is not a quiet one); UNKNOWN for more than two beats, or any GAVE-UP, files one priority-1 ledger line for that meter (deduplicated by meter id). The fold-debt step keeps the recall's stderr, retries a timeout or git failure once with a larger budget, separates timeout from git failure, and treats an unknown debt as due from the second consecutive unknown beat (same usage and six-hour gates as a real debt). A waiter that gives up now says so with a distinct marker, and the legacy "no idle window" success marker is read as GAVE-UP. The health scan runs a synthetic bad receipt (3 presented frames in 25 s, a 400 ms setup stall) through its own code every run and reads UNKNOWN when the canary is not flagged.

**Falsifier:** inject a recall that exits 4 on two consecutive beats: the second verdict must say dueFold true with reason unknown-fail-closed, and the board must show the fold meter UNKNOWN for as long as it fails. Make the scan's presented-frame check return nothing: the scan must print its canary as failed and the board must show it UNKNOWN. If either reads healthy, the contract is not holding.
