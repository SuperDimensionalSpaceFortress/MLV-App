---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/resume-brief-items-re-derived-r2-claim-and-transfer
rule: after a rotation the tick may resume before a new hub chat, which treats every brief item as UNKNOWN until it reads every ruling and the pending list. Ownership is the board's card-owner-check.ps1 verdict (exit 10 OWNED, 0 FREE), run before arming and again after the claim; the chat arms no OWNED card unless a later ruling assigns it the round. The check reads a ruling only where it declares or dispatches the card, so a ruling that names another actor to arm blocks too. A ruling-queued item, check FREE, is the chat's to arm if it or the chat it inherited from ran the prior review. A round already dispatched is not armed again without a retry ruling; a fired row retries under the SAME round on the beat, so the old row is reconciled (done, failed or cancelled) before a new round name. The claim refuses a fired same-round row, so an assigned retry waits for the beat's. Just before arming it re-reads both, then claims via mlv-app/two-hub-actors-must-claim-a-round-before-arming-it; until live it arms only while no tick runs. The claim tests the exact round only, so it re-runs the check and cancels its row, if pending, on an owner signal beyond its own row, run dir and declare line, or such a ruling, unless a later ruling assigns this round to the chat. A row can fire between the read and the cancel; a beat past its fire gate can launch it. A transfer writes the ruling, then the new row; the old pending row can still fire until replaced or cancelled, same round or new, as the launcher does not read rulings. Local evidence; siblings adopt or distinguish.
mechanism: two brief items had finished and the tick dispatched three fix rounds before the chat's first arm. One rotation.
check: Guard: none yet (claim helper, launcher gate, cancel: board cards)
supersedes: mlv-app/resume-brief-items-re-derived-vs-unattended-hub (its digest)
evidence: measured
