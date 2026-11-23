---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/resume-brief-items-re-derived-vs-unattended-hub
rule: after an account rotation, the unattended scheduled hub tick (it belongs to the OS user, not to the account) usually resumes before any new hub chat does. A new hub chat therefore treats every item in its starting brief or handoff ("on resume, dispatch X", "re-dispatch Y if Z is missing", "the usage hold blocks new lanes") as UNKNOWN until it has read the newest ledger rulings and the pending-action list written since the rotation. It acts only on what is still unowned, and it never re-fires a round the unattended tick already dispatched or explicitly sequenced. Items a ruling queued behind other work but did not arm are still the hub's to arm, if the hub ran that card's previous review, in this chat or in the earlier chat whose handoff this one inherited. It applies to every project whose hub has both an unattended scheduled tick and interactive hub chats.
mechanism: in this rotation the brief carried five resume items. Two had finished four days earlier: an outbox PR had merged and its items were on the bus master, and a review that had been "cut off" had been retried, after which one later round asked for changes and the round after it approved. The unattended tick had already lifted the pre-rotation usage hold, filled the producer cap with three fix rounds and armed a P1 tooling card, about ten minutes before the hub chat started. Acting on the brief as written would have doubled live rounds or re-run finished work. Two FINAL rounds the tick had queued "behind" the P1 card had no pending row at all; the hub chat armed them, because the hub, through the earlier chat whose handoff it inherited, had run their previous keys.
check: none mechanical; the hub chat's first resume step reads the ledger tail and the pending-action list before any dispatch
supersedes: none
evidence: measured
