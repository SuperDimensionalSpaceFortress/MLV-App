---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/resume-brief-items-re-derived-r2-claim-and-transfer
rule: after an account rotation the unattended tick usually resumes before a new hub chat. The chat treats every brief or handoff item ("the usage hold blocks new lanes") as UNKNOWN until it has read every ruling in force, pre-rotation ones included, and the pending-action list as it stands, whatever its write date. It arms only a card no one owns, never a round the tick dispatched or a ruling hands to another named owner. A later ruling that transfers a card to the chat (a first dispatch or a retry) makes it the owner; a retry of a fired round is a new round name, never the same round. Items a ruling only queued (no row, no owner named) are the chat's to arm if it ran that card's previous review, here or in the chat whose handoff it inherited. Reading first does not stop a double arm: the chat arms only through the claim step of mlv-app/two-hub-actors-must-claim-a-round-before-arming-it, which re-reads the rulings and the pending-action list under the lock; until it exists the chat does not arm a round while a tick is running. A transfer is one locked write: the ruling and a row naming the new owner, superseding only a still-pending row. Any hub with both actors.
mechanism: the brief carried five resume items; two had finished four days earlier (an outbox PR merged, a "cut off" review retried and passed). About ten minutes before the chat started, the tick lifted the usage hold, dispatched three fix rounds and armed a P1 tooling card; acting on the brief would have doubled live rounds. Two FINAL rounds queued "behind" it had no pending row; the chat armed them, having run their previous keys.
check: none mechanical; the first resume step reads every ruling in force and the pending-action list before any dispatch
supersedes: mlv-app/resume-brief-items-re-derived-vs-unattended-hub (its digest)
evidence: measured
