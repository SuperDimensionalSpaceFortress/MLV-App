---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/a-label-is-not-evidence-a-tooling-merge-files-its-bench-card
rule: (1) A meter that picks work by a card's kind label checks the property the label claims (here: that the card runs a venue leg), or it gives confident wrong advice for hours. (2) When a tooling card merges, the hub files, in that tick, the bench card the tooling was built to measure, with its venue leg written down; with none, nothing owns the owed run and the venue idles. (3) A drain that starts no hub tick at producer cap still starts one, or arms the bench rider, when bench work is owed and no rider is live: riders are exempt from the cap.
mechanism: the bench venue idled about 11 h. Two code-only rows labeled bench sent the idle meter down its dispatch-oldest-bench path, and three earlier idle fixes (2026-10-26, 11-09, 11-19) sat behind the same unverified label test (ledger 2026-11-23T21:07:51Z and :52Z; same class as a-name-prefix-kind-label-defaulting-to-product). When the drift tooling merged, no card owned the sweep it was built for, and the meter kept nominating BENCH-GPU-CLOCKS-FLUSH-1, a code-only card with BENCH in its name; the hub chat filed the sweep card by a direct queue write at 2026-11-24T01:46:38Z (lesson stamp 01:46:39Z). The drain then no-opped at producers 4/4, so no tick could dispatch it; the hub chat armed the rider at 01:58:04Z. Fix pending: BACHELOR-IDLE-NOMINATOR-FALLBACK-1 for (1), HUB-TICK-START-WHEN-BENCH-OWED-1 for (3).
applies: mlv-app
check: none yet. Fix pending: BACHELOR-IDLE-NOMINATOR-FALLBACK-1 (a bench row, and a product row named BENCH-..., with no venue leg each read LABEL_NOT_VENUE) and HUB-TICK-START-WHEN-BENCH-OWED-1 (producers 4/4 plus a queued bench card plus no rider, usage ok, FreeGiB at or above the emergency floor, expects a drain start; no bench owed, before the 90-minute backstop, keeps the no-op).
supersedes: none
evidence: measured
