---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/a-label-is-not-evidence-owed-bench-work-starts-a-tick-at-cap
rule: (1) A meter that picks work by a card's kind label or its name checks the property the label claims, or it advises wrongly. (2) At producer cap, a drain Get-DrainDecision would no-op starts a hub tick when Find-BachelorIdleRows returns the owed-first row for a bench card the rider can dispatch now; its usual skips hold.
mechanism: the bench venue idled about 11 h. Two code-only rows labeled bench sent the idle meter down its dispatch-oldest-bench path; three earlier idle fixes (10-26, 11-09, 11-19) sat behind the same unverified label test (ledger 2026-11-23T21:07:51Z and :52Z; same class as a-name-prefix-kind-label-defaulting-to-product). After the drift tooling merged, the meter kept nominating BENCH-GPU-CLOCKS-FLUSH-1, a code-only card with BENCH in its name. The drain then no-opped at producers 4/4; the hub armed the rider at 01:58:04Z.
applies: mlv-app
check: `pwsh -NoProfile -File Test-BachelorNominatorUnpark.ps1`, run in the board's hub-autopilot directory beside stuck-reconciler-lib.ps1, runs Find-BachelorIdleRows on fixtures, and `pwsh -NoProfile -File Test-HubTickAtCapThrottle.ps1`, run in that directory, runs Get-DrainDecision (hub-queue-lib.ps1) on fixtures at and over the cap; both pass today and pin the current behaviour. Fix pending: BACHELOR-IDLE-NOMINATOR-FALLBACK-1 (a bench row, and a product row named BENCH-..., with no venue leg each read LABEL_NOT_VENUE) and HUB-TICK-START-WHEN-BENCH-OWED-1 (a drain start exactly when the owed-first row is for a bench card the rider can dispatch now (slot free in producer-cap-gate.ps1, no FleetQuiet pause, no order-chain predecessor still queued or running) and Get-DrainDecision would otherwise no-op at cap; the drain's usual skips hold; every other case keeps today's decision).
supersedes: none
evidence: measured
