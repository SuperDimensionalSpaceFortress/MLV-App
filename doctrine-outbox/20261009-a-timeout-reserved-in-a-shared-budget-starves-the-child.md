---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/a-timeout-reserved-in-a-shared-budget-starves-the-child
rule: a scheduler that starts a child only while the shared per-beat budget still holds the child's FULL timeout has a start window of budget minus timeout. Keep the start threshold separate from the timeout, check the remaining budget before every launch, and when the launch would be clipped give the attempt back (deferred, never counted as a failure); persist deferred candidates and scan them first on the next beat.
mechanism: the board's mechanical key scheduler ran a 50 s beat and reserved the 45 s key-round timeout, so a round could start only in the first ~5 s, while real keyed beats took 4.6-13 s in total. Whenever the cheap work before it ran past ~5 s the round was deferred, every beat, with no failure recorded: one pull request sat deferred 15+ min and was keyed by hand twice. Deferred-first ordering alone helped but left the margin. Distinct from the checkpoint-overshoot entry (a budget checked at checkpoints bounds how many things START): that guard is too lax, this one is too strict, and the fixes pair (bound each child, and size the start threshold to the measured worst run, not to the timeout).
check: read the scheduler's launch guard: the minimum remaining budget to start must be below the child's timeout and at least its measured worst-case run time (max, not mean). Run one beat with the budget nearly spent and expect a give-back with no failure counted, then a fresh beat and expect the child to start. Measured: before the fix the same PR was deferred on every beat; after it a scheduled beat keyed it in 6.9 s; the producer's RED/GREEN run of the fix failed on the old file and passed on the new.
supersedes: none
evidence: measured
- **Guard:** none yet in tracked code (the scheduler is untracked board tooling; follow-up MECHKEYS-STARVE-LOOP-CAP-1). Residual risk: a round that persistently exceeds the start threshold now starves silently, so give-backs need a consecutive-count cap.
