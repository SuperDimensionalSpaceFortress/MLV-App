---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/resume-brief-items-re-derived-r2-claim-and-transfer
rule: after a rotation the tick may run before a new hub chat, which treats every brief item as UNKNOWN until it reads each ruling and pending list. Ownership is card-owner-check.ps1 (exit 10 OWNED, 0 FREE), run before arming and after claiming via mlv-app/two-hub-actors-must-claim-a-round-before-arming-it (until live, arm only while no tick runs). It reads a ruling only where it declares or dispatches the card, so an 'owns' or 'is to arm' ruling reads FREE: read the rulings. It matches the id as a substring, so another card's row naming it, or a longer id, reads OWNED: read each printed signal; OWNED stands unless each is a misread or a later ruling assigns the chat. FREE is the chat's to arm if a ruling assigns it (a first dispatch too), or queued it and the chat or its predecessor ran the prior review; a dispatched round needs a retry ruling. The claim tests the exact round only: on a signal beyond its own row, run dir and declare line it re-reads the pending list, cancelling its own row if still there, as -Cancel takes an id and would cancel a replacement. A transfer writes the ruling, then the row; the old pending row can still fire until replaced or cancelled, a beat that already read it can launch it over either, as the launcher reads no rulings. A dead fired row whose log shows PROVEN or exit 0 is failed, never retried; one with no launch retries on the beat, same id and run dir, up to three times. For a granted retry, -Cancel it (dead rows only), then arm a new id and run dir with no tick running. Local evidence; siblings adopt or distinguish.
mechanism: two brief items had finished and the tick dispatched three fix rounds before the chat's first arm. One rotation.
check: Guard: none yet (claim helper, launcher gate, cancel: board cards)
supersedes: mlv-app/resume-brief-items-re-derived-vs-unattended-hub (its digest)
evidence: measured
