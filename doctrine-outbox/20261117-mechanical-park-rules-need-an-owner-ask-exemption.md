---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/mechanical-park-rules-need-an-owner-ask-exemption
rule: a card filed from an owner ask carries origin=owner on its queue row, and every mechanical park or prune rule (intake ratio guard, age-and-kind backlog park, priority-age prune list) skips that row. Only a named hub ruling that names the card may park it; the queue tool refuses any other park with a typed exit. A park note naming a blocker PR ("waits for #n", "after #n") records that PR on the row, and the queue tool re-queues the row at its old priority when the PR merges. The roadmap counts owner asks of any kind and lists the ones still owed in their own parked section (reason and age, capped at 25 rows). An owner park closes only by a structured resolution on the row (done, duplicate, superseded, no_go or refused) that the queue tool's explicit park command writes; the note is never read for completion words (deny by default).
mechanism: an owner ask sat parked 10 days: three mechanical rules had no owner exemption, it was filed as kind unclassified so product protections skipped it, nothing re-read parked rows, and its blocker PR merged a day after the park. Parked rows are invisible to a "what is queued" search, so nobody saw it. Classifying notes as terminal from words like DONE or shipped drew three adversary rounds, so a closed field replaced the words. Price: a wider owner match keeps more rows out of the prune list; a delivered ask stays owed until the hub states its resolution once.
applies: mlv-app
check: py -3 -m unittest tools.coordination.test_render_roadmap -v (OwnerAskTests, R2AdversaryTests: owner asks counted and listed, a resolved one is not, terminal words without a resolution stay owed, 112 parked owner rows fit the budget). Queue side is board-local: Test-OwnerAskParkGuard.ps1 beside Update-CardQueue.ps1.
supersedes: none
evidence: measured
