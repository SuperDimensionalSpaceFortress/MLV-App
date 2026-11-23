---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/resume-brief-items-re-derived-r2-claim-and-transfer
rule: after an account rotation the unattended tick usually resumes before a new hub chat. The chat treats every brief or handoff item as UNKNOWN until it has read every ruling in force, even pre-rotation ones, and the pending list as it stands. It never arms a round the tick dispatched or a ruling hands to another owner. Ownership is judged card-wide first: any pending or fired row of the card owned by another actor, whatever its round name, blocks the arm, unless a later ruling assigns this round to the chat (first dispatch or retry). It may arm a card no one owns. Items a ruling only queued (no row, no owner) are its to arm if it ran that card's previous review (here or inherited). After a fired row a retry takes a new round name, even if refused pre-launch. Reading first does not stop a double arm: just before arming, waited or not, the chat re-reads rulings and list, then claims through mlv-app/two-hub-actors-must-claim-a-round-before-arming-it (list and receipts, under the lock; no arm while a tick runs). That claim tests the exact round only, so after it the chat re-reads once more and cancels its row, if still pending, when another actor now owns the card. This narrows, not closes, the window. A transfer first cancels the former owner's pending row, then writes the ruling, then the new row (a held row is released after the ruling). Any hub with both.
mechanism: of five brief resume items two had finished four days earlier; ten minutes before the chat the tick lifted the hold, dispatched three fix rounds and armed a P1 card, so the brief would double live rounds. Two queued FINAL rounds had no row; the chat armed them, having run their previous keys.
check: none mechanical; the first resume step reads the rulings and the pending-action list
supersedes: mlv-app/resume-brief-items-re-derived-vs-unattended-hub (its digest)
evidence: measured
