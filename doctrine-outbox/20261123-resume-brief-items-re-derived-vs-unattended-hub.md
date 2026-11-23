---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/resume-brief-items-re-derived-vs-unattended-hub
rule: after an account rotation, the unattended scheduled hub tick (it belongs to the OS user, not to the account) usually resumes before a new hub chat. A new hub chat therefore treats every item in its starting brief or handoff ("on resume, dispatch X", "re-dispatch Y if Z is missing", "the usage hold blocks new lanes") as UNKNOWN until it has read the ledger rulings written since the rotation and the pending-action list as it stands now, whatever its write date. It acts only on what is still unowned, and it never re-fires a round the unattended tick already dispatched, or that a ruling hands to another named owner. Items a ruling only queued behind other work (not armed, no other owner named) are still the hub's to arm, if the hub ran that card's previous review, in this chat or the earlier one whose handoff it inherited. It applies to every project whose hub has both an unattended scheduled tick and interactive hub chats.
mechanism: in this rotation the brief carried five resume items. Two had finished four days earlier: an outbox PR had merged and its items were on the bus, and a review that had been "cut off" had been retried, after which one later round asked for changes and the round after it approved. The unattended tick had already lifted the pre-rotation usage hold, dispatched three fix rounds into the producer slots and armed a P1 tooling card, about ten minutes before the hub chat started. Acting on the brief as written would have doubled live rounds or re-run finished work. Two FINAL rounds the tick had queued "behind" the P1 card had no pending row; the hub chat armed them, because the hub, through the earlier chat it inherited, had run their previous keys.
check: none mechanical; the first resume step reads the ledger tail and the pending-action list before any dispatch
supersedes: none
evidence: measured
