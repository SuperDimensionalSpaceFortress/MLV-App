---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/queue-without-age-limits-starves-owner-waiting-card
rule: every queued-and-due card carries an age limit by priority, and a card the owner has asked about is marked owner-waiting and drained first; "queued" without an age check is not a state, it is a place work goes to be forgotten.
mechanism: the factory turns ledger rulings into card-queue rows, and an acting tick drains a few rows per beat when capacity allows. Measured 2026-10-15: 410 open rows, 85 queued and due, producers 6 against a cap of 3, host commit at its ceiling. Eight tick launches between 16:16Z and 20:27Z, 30 to 45 minutes apart, left queued-and-due between 80 and 88 throughout. A P3 fix round for a feature the owner had chosen that morning (a hub ruling at 17:32Z wrote its plan) sat with no pending action, no run directory and no live lane until the owner asked at about 20:20Z when it would appear. A same-day scan found 28 cards in that shape. None were invisible: every one was a queue row. Nothing measured how long a due row had waited, so starvation looked like a healthy backlog.
applies: mlv-app
check: py -3 tools/coordination/stuck_queue_checks/queue_starvation_check.py --drain-cmd "<the board's drain command>" (pushed: SuperDimensionalSpaceFortress/MLV-App @a02af93c0629); fixture is 50 due P1/P2 rows plus one P3 row aged 25 h and owner-waiting, capacity 3, and it exits 0 only when that row is starved, bumped to P2 and drained; --impl naive exits 1 (RED), --impl aged exits 0 (GREEN)
supersedes: none
evidence: measured
- **Guard:** the fix is pending -- card STUCK-RECONCILER-1 (starvation limits P1 2 h / P2 6 h / P3 24 h / P4-5 72 h, owner-waiting escalation, prompt-ready fast path, throughput line); the check above already runs against any drain
