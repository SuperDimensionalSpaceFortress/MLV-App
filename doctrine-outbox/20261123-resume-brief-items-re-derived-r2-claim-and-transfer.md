---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/resume-brief-items-re-derived-r2-claim-and-transfer
rule: a new hub chat treats every brief item as UNKNOWN until it reads each ruling and pending list. Ownership is card-owner-check.ps1, run before arming, after re-reading both, and after claiming via mlv-app/two-hub-actors-must-claim-a-round-before-arming-it (until live, arm only while no tick runs). It reads a ruling only where it declares or dispatches the card, so 'owns' and 'is to arm' rulings read FREE: read rulings by eye, again after the claim; neither claim nor check reads an assignment, so a later hand-off goes unseen. It matches the id as a substring, so another card's row naming it, a longer id or a 'not  dispatch' ruling reads OWNED: it stands unless each signal is a misread or a later ruling assigns the chat. FREE is the chat's to arm if a ruling assigns it (first dispatch too) or queued it, the chat or its predecessor ran the last review, no later ruling hands it elsewhere; a dispatched round needs a retry ruling. On any other signal, re-read the pending list and cancel only if its run dir is the chat's own; -Cancel takes an id, so a same-id replacement after that read is cancelled too: ledger it. A transfer writes the ruling, then the row; the old row can fire until replaced or cancelled, even from a beat that read it first. Granted retry of a fired row: confirm no wrapper runs, -Cancel <id> -Stop (a beat with an older copy revives a plain -Cancel; a no-pid row is refused, exit 3, while a receipt under 10 min shows running), arm a new id and run dir with no tick running; once live the claim refuses it while the old run dir keeps a receipt. Local evidence; siblings adopt or distinguish.
mechanism: two brief items had finished and the tick dispatched three fix rounds before the chat's first arm.
check: Guard: none yet (board cards)
supersedes: mlv-app/resume-brief-items-re-derived-vs-unattended-hub (its digest)
evidence: measured
