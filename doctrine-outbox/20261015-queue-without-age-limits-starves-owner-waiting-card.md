---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/queue-without-age-limits-starves-owner-waiting-card
rule: every queued-and-due card carries an age limit by priority, and a card the owner has asked about is marked owner-waiting and drained first; "queued" without an age check is not a state, it is a place work goes to be forgotten.
mechanism: the factory turns ledger rulings into card-queue rows, and an acting tick drains a few rows per beat when capacity allows. Measured 2026-10-15: 410 open rows, 85 queued and due now (P1-5), producers 6 against a cap of 3 and host commit at its ceiling, so the drain moved a handful per hour. A P3 fix round for a feature the owner had chosen that morning (a hub ruling at 17:32Z wrote its plan) sat with no pending action, no run directory and no live lane until the owner asked at about 20:20Z when the feature would appear. A same-day scan found 28 cards in the same shape. None were invisible: every one was a queue row. Nothing measured how long a due row had waited, and nothing carried "the owner is waiting" into the drain order, so starvation looked exactly like a healthy backlog.
applies: mlv-app
check: seed a queue fixture with one due P3 row aged 25 h and owner-waiting set, among 50 due P1-P2 rows; run one reconciler beat. The P3 row must be reported starved (age over its 24 h limit), bumped one priority, and drained ahead of the P1-P2 rows because it is owner-waiting. A reconciler that leaves it in place is the trap.
supersedes: none
evidence: measured
- **Guard:** pending -- card STUCK-RECONCILER-1 (starvation limits P1 2 h / P2 6 h / P3 24 h / P4-5 72 h, owner-waiting escalation, prompt-ready fast path, throughput line)
