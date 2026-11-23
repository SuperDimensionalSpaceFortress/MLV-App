---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/resume-brief-items-re-derived-r2-claim-and-transfer
rule: after an account rotation the unattended tick usually resumes before a new hub chat. The chat treats every brief or handoff item as UNKNOWN until it has read every ruling in force, pre-rotation ones included, and the pending-action list as it stands. It never arms a round the tick dispatched or a ruling hands to another named owner. Ownership is judged card-wide first: any pending or fired row of the card owned by another actor, whatever its round name, blocks the arm before any round-name rule. It may arm a card no one owns, or one a later ruling transfers to it (first dispatch or retry). Items a ruling only queued (no row, no owner) are its to arm if it ran that card's previous review (here or inherited). After a fired row a retry takes a new round name, even if refused pre-launch. Reading first does not stop a double arm: just before arming, waited or not, the chat re-reads the rulings and the pending list, then arms through the claim step of mlv-app/two-hub-actors-must-claim-a-round-before-arming-it (list and receipts, under the lock), obeying its no-arm-while-a-tick-runs rule until it exists; this narrows the window, not closes it. A transfer never publishes a runnable row ahead of its ruling: ruling first, then the row, or a held row released once the ruling exists. The claim reads both. Any hub with both.
mechanism: the brief carried five resume items; two had finished four days earlier. Ten minutes before the chat, the tick lifted the hold, dispatched three fix rounds and armed a P1 card; the brief would have doubled live rounds. Two FINAL rounds queued "behind" it had no pending row; the chat armed them, having run their previous keys.
check: none mechanical; the first resume step reads the rulings and the pending-action list
supersedes: mlv-app/resume-brief-items-re-derived-vs-unattended-hub (its digest)
evidence: measured
