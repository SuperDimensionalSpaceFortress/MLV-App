---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/resume-brief-items-re-derived-r2-claim-and-transfer
rule: after a rotation the tick may resume before a new hub chat, which treats every brief item as UNKNOWN until it has read every ruling in force and the pending list. Ownership is what the board's card-owner-check.ps1 reports (exit 10 OWNED, 0 FREE), run before arming and again after the claim; the chat does not arm an OWNED card unless a later ruling assigns the round to it. A ruling that names another actor to arm it blocks too; the check ignores rulings. A ruling-queued item with the check FREE is the chat's to arm if it, or the chat it inherited from, ran the prior review. A round already dispatched is not armed again without a retry ruling; a fired row retries under the SAME round on the beat, so the old row is reconciled (done, failed or cancelled) before a new round name. Just before arming it re-reads rulings and list, then claims through mlv-app/two-hub-actors-must-claim-a-round-before-arming-it; until that step is live it arms only while no tick runs. The claim tests the exact round only, so it re-runs the check and cancels its row, if still pending, on an owner signal beyond its own row and run dir, or such a ruling, unless a later ruling assigns this round to the chat. A row can fire between the read and the cancel, and a beat past its fire gate can launch a cancelled row. A transfer writes the ruling first, then the new row, superseding a pending same-round row only; under a new round name the old pending row can still fire until cancelled or the launcher reads rulings. Local evidence; siblings adopt or distinguish.
mechanism: two brief items had finished and the tick dispatched three fix rounds before the chat's first arm. This rotation only.
check: Guard: none yet (claim helper, launcher gate and cancel are board cards)
supersedes: mlv-app/resume-brief-items-re-derived-vs-unattended-hub (its digest)
evidence: measured
