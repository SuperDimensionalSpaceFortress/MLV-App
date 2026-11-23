---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/resume-brief-items-re-derived-r2-claim-and-transfer
rule: after an account rotation the unattended tick may resume before a new hub chat. The chat treats every brief or handoff item as UNKNOWN until it has read every ruling in force, even pre-rotation ones, and the pending list. Another actor owns a card through a pending or fired row of any round, or a ruling that hands it over; the chat does not arm such a card unless a later ruling assigns this round to the chat. Items a ruling only queued (no row, no owner) are the chat's to arm if it ran that card's previous review (here or inherited). After a fired row a retry takes a new round name. Just before arming it re-reads rulings and list, then claims through mlv-app/two-hub-actors-must-claim-a-round-before-arming-it (under the lock); until that step is live it arms only while no tick runs. The claim tests the exact round only, so the chat then re-reads and cancels its row, if still pending, when another actor owns the card and no later ruling assigns this round to the chat. This narrows, not closes, the window: a row can fire between the read and the cancel, and a beat already past its fire gate can launch a cancelled row. A transfer writes the ruling first (durable intent), then the new row superseding only a pending one; the old row may still fire until the launcher reads rulings. Local evidence; siblings adopt or distinguish.
mechanism: two of five brief items had finished four days earlier; the ledger shows the tick lifted the hold, dispatched three fix rounds and armed a P1 card before the chat's first arm. Two queued FINAL rounds had no row; the chat armed them, having run their previous keys. Measured in this rotation only.
check: Guard: none yet (claim helper, launcher gate on transfer rulings and pending-only cancel are board cards)
supersedes: mlv-app/resume-brief-items-re-derived-vs-unattended-hub (its digest)
evidence: measured
