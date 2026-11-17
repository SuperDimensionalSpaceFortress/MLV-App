---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/mechanical-park-rules-need-an-owner-ask-exemption
rule: a card filed from an owner ask carries origin=owner on its queue row, and every mechanical park or prune rule (an intake ratio guard, an age-and-kind backlog park, a priority-age prune list) skips that row. Only a named hub ruling that names the card may park it, and the queue tool refuses any other park of it with a typed exit. A park whose note names a blocker PR ("waits for #n", "stacked on #n", "after #n") records that PR on the row, and the queue tool returns the row to queued, at its old priority, when the PR merges. The roadmap counts owner asks whatever their kind and lists the ones still owed in their own parked section, with reason and age.
mechanism: an owner product ask sat parked 10 days. Three mechanical rules had no owner exemption, the ask was filed with kind unclassified so the product protections skipped it, nothing re-read parked rows, and its blocker PR merged one day after the park. Parked rows are invisible to a "what is queued" search, so nobody saw it until the owner asked again. Price: a wider owner match keeps more rows out of the prune list; a terminal park note (done, superseded, duplicate) never re-queues, and a hub-written park reason is never overridden from the source line.
applies: mlv-app
check: py -3 -m unittest tools.coordination.test_render_roadmap -v covers the roadmap side (OwnerAskTests: an owner ask of any kind is counted, a parked one is listed with reason and age, a terminal one is not, a non-owner tooling row is still not counted). The queue side is board-local: Test-OwnerAskParkGuard.ps1 beside Update-CardQueue.ps1 (an owner row survives each park rule, a parked row re-queues on its blocker merge, a non-owner row is parked exactly as before).
supersedes: none
evidence: measured
