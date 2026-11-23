---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/resume-brief-items-re-derived-r2-claim-and-transfer
rule: after an account rotation the unattended tick usually resumes before a new hub chat. The chat treats every brief or handoff item ("the usage hold blocks new lanes") as UNKNOWN until it has read every ruling in force, pre-rotation ones included, and the pending-action list as it stands. It never arms a round the tick dispatched or a ruling hands to another named owner. It may arm a card no one owns, or one a later ruling transfers to it (a first dispatch or a retry). Items a ruling only queued (no row, no owner named) are its to arm if it ran that card's previous review, here or in an inherited chat. After a fired row a retry takes a new round name, even if refused pre-launch. Reading first does not stop a double arm: the chat re-reads the rulings, then arms through the claim step of mlv-app/two-hub-actors-must-claim-a-round-before-arming-it (pending-action list and run-receipt area, under the lock). Until it exists the chat follows the companion (no arm while a tick is running) and, after any wait, re-reads the list and receipts just before arming; that narrows the window, not closes it. A transfer writes the row naming the new owner first (superseding only a still-pending row), then the ruling: no lock covers the ruling line, so a claim sees the row. Any hub with both.
mechanism: the brief carried five resume items; two had finished four days earlier (a merged PR, a retried review). Ten minutes before the chat, the tick lifted the hold, dispatched three fix rounds and armed a P1 tooling card; obeying the brief would have doubled live rounds. Two FINAL rounds queued "behind" it had no pending row; the chat armed them, having run their previous keys.
check: none mechanical; the first resume step reads the rulings and the pending-action list
supersedes: mlv-app/resume-brief-items-re-derived-vs-unattended-hub (its digest)
evidence: measured
