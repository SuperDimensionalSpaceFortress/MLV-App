---
target: RECEIPTS.md
kind: receipt
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-16 - RULING CANDIDATE: one fleet standard for doctrine fold-in

Owner-requested; for ratification.

**Relationship to existing doctrine** (`ruling-candidates-name-what-they-supersede-r1`):
- **Extends RULINGS.md:1126-1152 and R15 (:2874-2875):** no per-project tasks or new cadence.
- **Extends R14.8 (:2820) and R15.5 (:2869):** S6's one bus tool is pinned per board (README.md:379-383), not vendored.
- **Reconciles `ruling-candidates/fold-is-a-disposition-per-entry-and-lag-is-derived-r1.md`.** F2 excludes other boards' `specs/`; S1 counts them, since a sibling's spec change can bind this board; F2's extra paths are a steward decision. F3: S5's 24 h is the fleet ceiling, a board's wake period its tighter bound, S3's 6 h the default. F4: S5's target, not met today (heartbeats/README.md:11-13): `tools/Publish-BoardHeartbeat.ps1` sets `bus_cursor` to the bus HEAD (:103) and `delta_count` to a 0/1 flag (:127-134); the sweep row has neither. Follow-up: the row gains S1 debt and the S2 cursor, the publisher maps them, `bus_cursor` rejoins change detection (:218-232) once real. F5: adopted (S4).

**Measured at bus 415f3c0 (cursor 9f1a9cf):** scope 50, fold_lag 49, S1 below 6; 44 of 50 touch only cos-feedback/.

**Rules, each with its check**
- **S1 One debt definition.** debt = bus commits after the cursor that change TRAPS.md, RECEIPTS.md, RULINGS.md or specs/** other than the board's own (doctrine-sync.mjs:80). cos-feedback/ never counts; CoS asks (Layi, 2026-10-15) are unchanged, and `doctrine-sync check` still lists it (:78). TRAPS.md leaves when R14.5 retires it (RULINGS.md:2881). Check: `doctrine-sync debt --project <p> --json`.
- **S2 One cursor.** `.codex-state/doctrine/last-seen.json`, written only by `doctrine-sync ack`.
- **S3 One trigger.** `fleet-sweep` (hourly at :17) or R15.3's hook. Due when debt > 0 AND (last fold 6 h old OR debt >= 10 OR a RULINGS.md change unfolded). The sweep row lacks those fields and alarms past 24 h (fleet-sweep.mjs:43-45), so the board's wake tick runs `doctrine-sync debt --project <p> --json`, applies the due rule and launches S4's lane. The task adds `--json-out` (RULINGS.md:1149-1152); only the host that clones the board counts (elsewhere `no-local-clone`, fleet-sweep.mjs:181). Check: its receipt within 2 h.
- **S4 One fold procedure.** A bounded headless lane records adopt or distinguish per item and acks past each. Check: the acked sha is the last item decided.
- **S5 One measure.** The scorecard row carries S1 debt; oldest unfolded commit under 24 h. A breach goes to the board's thread as a CoS-CONCERN with an ACK ask (Layi, 2026-10-16); no new CoS duty. Check: fold_lag equals S1 once the steward adds a debt-only cos-feedback/ exclusion to `doctrine-sync.mjs` and `fleet-scorecard.py:68-69`, with its parity test (`fleet-scorecard.tests.py:411-416`).
- **S6 One install.** Debt command and fold prompt come from one pinned bus tool; none copied.

**Adoption:** delete private debt counters and second cursors; reply ADOPT or DISTINGUISH in `specs/<project>.md` (R14.8). Topbuilder and magic-lantern need a spec first. Supersedes FLEET-FOLD-LOOP-SINGLE-SOURCE-1 (MLV ledger, 2026-10-16T20:48:24Z).
