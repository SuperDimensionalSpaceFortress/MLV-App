---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/resume-brief-items-re-derived-r2-claim-and-transfer
rule: after a rotation the tick may run before a new hub chat, which treats every brief item as UNKNOWN until it reads every ruling and pending list. Ownership is card-owner-check.ps1's verdict (exit 10 OWNED, 0 FREE), run before arming and again after; the chat arms no OWNED card unless a later ruling assigns it the round. The check reads a ruling only where it declares or dispatches the card, so a ruling that names another actor to arm blocks too. The check can misread a two-space 'not  dispatch' or a longer card id in a pending row. A ruling-queued item, check FREE, is the chat's to arm if it or its inherited-from chat ran the prior review. A round already dispatched is not armed again without a retry ruling; a fired row retries under the SAME round on the beat, so reconcile it (done, failed or cancelled) before a new round name; an assigned retry of one has no safe route yet: hold it. Before arming it re-reads both and claims via mlv-app/two-hub-actors-must-claim-a-round-before-arming-it; until live it arms only while no tick runs. The claim tests the exact round only, so it re-runs the check and cancels its row, if pending, on an owner signal beyond its own row, run dir and declare line, or such a ruling, unless a later ruling assigns this round to the chat. A transfer writes the ruling, then the row; the old pending row can still fire until replaced or cancelled, same round or new, and a beat that already read it can launch it over a replacement or cancellation, as the launcher does not read rulings. Local evidence; siblings adopt or distinguish.
mechanism: two brief items had finished and the tick dispatched three fix rounds before the chat's first arm. One rotation.
check: Guard: none yet (claim helper, launcher gate, cancel: board cards)
supersedes: mlv-app/resume-brief-items-re-derived-vs-unattended-hub (its digest)
evidence: measured
