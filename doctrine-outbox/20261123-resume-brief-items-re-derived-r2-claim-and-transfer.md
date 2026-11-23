---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/resume-brief-items-re-derived-r2-claim-and-transfer
rule: after an account rotation the unattended tick usually resumes before a new hub chat. The chat treats every brief or handoff item ("the usage hold blocks new lanes") as UNKNOWN until it has read every ruling in force, pre-rotation ones included, and the pending-action list as it stands, whatever its write date. It never arms a round the tick dispatched or a ruling hands to another named owner. It may arm a card no one owns, or one a later ruling transfers to it (a first dispatch or a retry). Items a ruling only queued (no row, no owner named) are its to arm if it ran that card's previous review, here or in an inherited chat. After a fired row a retry takes a new round name, even if refused before any lane launched. Reading first does not stop a double arm: the chat arms through the claim step of mlv-app/two-hub-actors-must-claim-a-round-before-arming-it, which re-reads the rulings and the pending-action list under the lock. Until it exists the chat records a ruling taking the card, then follows the companion (no arm while a tick is running); that narrows the window, not closes it. A transfer is one locked write: the ruling and a row naming the new owner, superseding only a still-pending row. Any hub with both.
mechanism: the brief carried five resume items; two had finished four days earlier (an outbox PR merged, a "cut off" review retried and passed). Ten minutes before the chat, the tick lifted the hold, dispatched three fix rounds and armed a P1 tooling card; obeying the brief would have doubled live rounds. Two FINAL rounds queued "behind" it had no pending row; the chat armed them, having run their previous keys.
check: none mechanical; the first resume step reads every ruling in force and the pending-action list before any dispatch
supersedes: mlv-app/resume-brief-items-re-derived-vs-unattended-hub (its digest)
evidence: measured
