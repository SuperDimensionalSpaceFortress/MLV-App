---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/resume-brief-items-re-derived-r2-claim-and-transfer
rule: a new hub chat treats every brief item as UNKNOWN until it reads each ruling and pending list. Ownership is card-owner-check.ps1, run before arming, after re-reading both, and after claiming via mlv-app/two-hub-actors-must-claim-a-round-before-arming-it (until live, arm only while no tick runs). It reads a ruling only where it declares or dispatches the card, so 'owns' and 'is to arm' rulings read FREE: read rulings by eye and after the claim; neither claim nor check reads an assignment, so a later hand-off is unseen. It matches the id as a substring, so another card's row naming it, a longer id or a 'not  dispatch' ruling reads OWNED: it stands unless each is a misread or a later ruling assigns the chat. FREE is the chat's to arm if a ruling assigns it (first dispatch too) or queued it, the chat or its predecessor ran the last review, no later ruling hands it on; a dispatched round needs a retry ruling. After the claim its own row, run dir and declare line are not signals; on any other, re-read the row and cancel only if its run dir is the chat's own and no other actor can write that id (-Cancel takes an id only: a same-id replacement is cancelled, its wrapper too with -Stop; read the exit code); else ledger it. A retry is such a cancel: confirm no wrapper or running receipt in its run dir, -Cancel <id> -Stop (a plain -Cancel, or an earlier cancel, has no stop marker: an older beat revives it), arm a new id and run dir; the staged claim has no retry route. A transfer writes the ruling, then the row; the old row can fire first, and a beat past the fire gate launches it after a replace or cancel.
mechanism: two brief items had finished and the tick dispatched three fix rounds before the chat's first arm.
applies: mlv-app
check: Guard: none yet
supersedes: mlv-app/resume-brief-items-re-derived-vs-unattended-hub (its digest)
evidence: measured
