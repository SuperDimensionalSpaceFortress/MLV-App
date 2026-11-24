---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/resume-brief-items-re-derived-r2-claim-and-transfer
rule: after a rotation the tick may run before a new hub chat, which treats every brief item as UNKNOWN until it reads each ruling and pending list. Ownership is card-owner-check.ps1 (exit 10 OWNED, 0 FREE), run before arming, after re-reading rulings and pending list, and after claiming via mlv-app/two-hub-actors-must-claim-a-round-before-arming-it (until live, arm only while no tick runs). It reads a ruling only where it declares or dispatches the card, so 'owns' and 'is to arm' rulings read FREE: read the rulings. It matches the id as a substring, so another card's row naming it, a longer id, or a 'not  dispatch' ruling reads OWNED: read each printed signal; OWNED stands unless each is a misread or a later ruling assigns the chat. FREE is the chat's to arm if a ruling assigns it (a first dispatch too), or queued it, the chat or its predecessor ran the prior review, and no later ruling hands it to another actor; a dispatched round needs a retry ruling. The claim tests the exact round only: on a signal beyond its own row, run dir and declare line it re-reads the pending list, cancelling its own row, as -Cancel takes an id and would cancel a replacement, even one written after the re-read. A transfer writes the ruling, then the row; the old row can fire until replaced or cancelled, a beat that already read it can launch it over either. For a granted retry of a fired row, confirm no wrapper process runs, -Cancel it (a pid-less row reads dead; a stale beat can revive it), then arm a new id and run dir with no tick running. Local evidence; siblings adopt or distinguish.
mechanism: two brief items had finished and the tick dispatched three fix rounds before the chat's first arm.
check: Guard: none yet (claim helper, launcher gate, cancel: board cards)
supersedes: mlv-app/resume-brief-items-re-derived-vs-unattended-hub (its digest)
evidence: measured
