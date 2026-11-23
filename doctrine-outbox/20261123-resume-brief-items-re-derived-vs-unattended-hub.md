---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/resume-brief-items-re-derived-vs-unattended-hub
rule: after an account rotation, the unattended scheduled hub tick usually resumes before a new hub chat. A new hub chat treats every item in its brief or handoff ("the usage hold blocks new lanes") as UNKNOWN until it has read every ledger ruling still in force, including pre-rotation rulings, and the pending-action list as it stands now, whatever its write date. It acts only on what is still unowned and never re-fires a round the tick already dispatched, or that a ruling hands to another named owner. Items a ruling only queued behind other work (not armed, no other owner named) are still the hub's to arm if the hub ran that card's previous review, here or in the chat whose handoff it inherited. Reading first does not stop a double arm while the tick runs: re-read the pending list just before arming, and follow mlv-app/two-hub-actors-must-claim-a-round-before-arming-it (companion, may publish later). It applies to any project whose hub has an unattended tick and hub chats.
mechanism: in this rotation the brief carried five resume items. Two had finished four days earlier: an outbox PR had merged and its items were on the bus, and a review that had been "cut off" had been retried, after which one round asked for changes and the next approved. The unattended tick had already lifted the pre-rotation usage hold, dispatched three fix rounds into the producer slots and armed a P1 tooling card, about ten minutes before the hub chat started. Acting on it as written would have doubled live rounds or re-run finished work. Two FINAL rounds queued "behind" the P1 card had no pending row; the hub chat armed them: the hub, via the chat it inherited, had run their previous keys.
check: none mechanical; the first resume step reads every ruling still in force and the pending-action list before any dispatch
supersedes: none
evidence: measured
