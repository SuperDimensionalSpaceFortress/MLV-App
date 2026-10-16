---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/queue-without-age-limits-starves-owner-waiting-card
rule: every queued-and-due card carries an age limit by priority, and a card the owner has asked about is marked owner-waiting and drained first; "queued" without an age check is not a state, it is a place work goes to be forgotten.
mechanism: card-queue rows are built from the ledger and a queue-drain launch has a hub tick pick a few rows per beat when capacity allows. Measured 2026-10-15: 410 open rows, 85 queued and due plus 236 P6 backlog, producers 6 against a cap of 3, host commit at the 60 GiB ceiling. A P3 product round (Vision 50D r2, hub ruling 17:32Z) sat behind about 85 due rows with no age limit and no owner-waiting signal, queued in the ledger but never armed. A same-day scan found 28 cards queued with no action, run directory or live process; all 28 were queue rows, none invisible. Sources: ledger 2026-10-15T20:32:36Z; project-memory self-heal-stuck-items-strategy-20261015.md.
applies: mlv-app
check: from the MLV-App repo root, `python tools/coordination/stuck_queue_checks/queue_starvation_check.py --impl naive` exits 1 (RED) and `python tools/coordination/stuck_queue_checks/queue_starvation_check.py --impl aged` exits 0 (GREEN); any Python 3, stdlib only, pushed at SuperDimensionalSpaceFortress/MLV-App @c5739ef23f09 (PR 454). Fixture: 50 due P1/P2 rows plus one P3 row aged 25 h and owner-waiting, capacity 3; GREEN needs that row reported starved, bumped to P2 and drained. A board checks its own drain by passing its command line to --drain-cmd (stdin {capacity, rows}, stdout {drained, starved, bumped}); a command that fails or prints no JSON exits 2, never GREEN
supersedes: none
evidence: measured
- **Guard:** the fix is pending -- card STUCK-RECONCILER-1 (starvation limits P1 2 h / P2 6 h / P3 24 h / P4-5 72 h, owner-waiting escalation, prompt-ready fast path, throughput line); the check above already runs against any drain
