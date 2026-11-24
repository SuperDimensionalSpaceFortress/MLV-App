---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/a-label-is-not-evidence-a-tooling-merge-files-its-bench-card
rule: (1) A meter that picks work by a card's kind label checks the property the label claims (here: that the card runs a venue leg), or it gives confident wrong advice for hours. (2) When a tooling card merges, the hub files, in that tick, the bench card the tooling was built to measure, with its venue leg written down; with none, the idle meter has nothing to pick and falls back to a label match. (3) A drain that starts no hub tick at producer cap still starts one, or arms the bench rider, when bench work is owed and no rider is live: riders are exempt from the cap.
mechanism: the bench venue idled about 11 h. Three earlier idle fixes (2026-10-26, 11-09, 11-19) each sat behind the same unverified label test (ledger 2026-11-23T21:07:52Z; same class as a-name-prefix-kind-label-defaulting-to-product). When the drift tooling merged, no bench card existed; the hub chat filed it at 2026-11-24T01:46:39Z. The drain then no-opped at producers 4/4, so no tick could dispatch it; the hub chat armed the rider by hand at 01:58:04Z and it fired at 02:01Z. Fix pending: HUB-TICK-START-WHEN-BENCH-OWED-1.
applies: any board with a bench venue, an idle meter and a producer cap.
check: none yet. Fix pending: HUB-TICK-START-WHEN-BENCH-OWED-1; its test: producers 4/4 plus a queued bench card plus no rider expects a drain start, and no bench owed keeps the no-op.
supersedes: none
evidence: measured
