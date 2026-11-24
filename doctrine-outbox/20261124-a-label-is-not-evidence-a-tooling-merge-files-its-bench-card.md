---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/a-label-is-not-evidence-a-tooling-merge-files-its-bench-card
rule: (1) A meter that picks work by a card's kind label or its name checks the property the label claims (here: that the card runs a venue leg), or it advises wrongly for hours. (2) When a tooling card merges, the hub files, in that tick, the bench card the tooling was built to measure, with its venue leg written down; with none, nothing owns the owed run. (3) A drain at producer cap that Get-DrainDecision would no-op starts a hub tick when Find-BachelorIdleRows returns the owed-first row for a queued bench card; the drain keeps the skips it already makes.
mechanism: the bench venue idled about 11 h. Two code-only rows labeled bench sent the idle meter down its dispatch-oldest-bench path; three earlier idle fixes (10-26, 11-09, 11-19) sat behind the same unverified label test (ledger 2026-11-23T21:07:51Z and :52Z; same class as a-name-prefix-kind-label-defaulting-to-product). When the drift tooling merged, no card owned the sweep it was built for, and the meter kept nominating BENCH-GPU-CLOCKS-FLUSH-1, a code-only card with BENCH in its name; the hub filed the sweep card by a direct queue write at 01:46:38Z. The drain then no-opped at producers 4/4, so no tick started; the hub armed the rider at 01:58:04Z.
applies: mlv-app
check: none yet. Fix pending: BACHELOR-IDLE-NOMINATOR-FALLBACK-1 (a bench row, and a product row named BENCH-..., with no venue leg each read LABEL_NOT_VENUE) and HUB-TICK-START-WHEN-BENCH-OWED-1 (expects a drain start exactly when Find-BachelorIdleRows, stuck-reconciler-lib.ps1, returns the owed-first row for a queued bench card and Get-DrainDecision, hub-queue-lib.ps1, would otherwise no-op at cap; the same skips the drain already makes hold; every other case keeps today's decision).
supersedes: none
evidence: measured
