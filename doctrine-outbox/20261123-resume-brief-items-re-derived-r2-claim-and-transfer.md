---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/resume-brief-items-re-derived-r2-claim-and-transfer
rule: after a rotation the tick may resume before a new hub chat, which treats every brief or handoff item as UNKNOWN until it has read every ruling in force and the pending list. Ownership is what the board's card-owner-check.ps1 -Card CARD reports (exit 10 OWNED, 0 FREE), run before arming and again after the claim; the chat does not arm an OWNED card unless a later ruling assigns the round to it. A ruling that names another actor to arm it blocks too; the check ignores rulings. A ruling-queued item with the check FREE is the chat's to arm if it ran its prior review. A fired row retries under the SAME round on the beat, so the old row is reconciled (done, failed or cancelled) before a new round name is armed. Just before arming it re-reads rulings and list, then claims through mlv-app/two-hub-actors-must-claim-a-round-before-arming-it; until that step is live it arms only while no tick runs. The claim tests the exact round only, so it re-runs the check and cancels its row, if still pending, when it reports an owner signal other than its own row and no later ruling assigns this round to the chat. A row can still fire between the read and the cancel, and a beat past its fire gate can launch a cancelled row. A transfer writes the ruling first, then the new row, superseding a pending same-round row only; under a new round name the old pending row can still fire until cancelled or the launcher reads rulings. Local evidence; siblings adopt or distinguish.
mechanism: two of five brief items had finished four days earlier and the tick dispatched three fix rounds before the chat's first arm; two queued FINAL rounds had no row and the chat armed them. This rotation only.
check: Guard: none yet (claim helper, launcher gate and cancel are board cards)
supersedes: mlv-app/resume-brief-items-re-derived-vs-unattended-hub (its digest)
evidence: measured
