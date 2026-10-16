---
target: RECEIPTS.md
kind: receipt
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-16 - RULING CANDIDATE: one fleet standard for doctrine fold-in

Owner-requested; for steward ratification.

**Relationship to existing doctrine** (`ruling-candidates-name-what-they-supersede-r1`):
- **Extends RULINGS.md:1126-1152 and R15 (:2874-2875).** Trigger: the existing box-level `fleet-doctrine-sweep` (`tools/fleet-sweep.mjs`, hourly at :17) or R15.3's hook. No per-project tasks or new cadence.
- **Extends R14.8 (:2820) and R15.5 (:2869):** S6's one bus tool is pinned per board (README.md:379-383), not vendored.
- **Reconciles `ruling-candidates/fold-is-a-disposition-per-entry-and-lag-is-derived-r1.md`.** F2: S1 also counts other boards' `specs/` (F2 excludes them); F2's extra paths (ruling-candidates/, adoption/, adjudications/) are a steward decision. F3: S5's 24 h is the fleet ceiling, a board's wake period its tighter bound, S3's 6 h the default. F4: adopted as S5's measure (S1 debt as `delta_count`, S2's cursor as `bus_cursor`; fleet-sweep only, heartbeats/README.md:11-13). F5: adopted (S4).

**Measured at bus 415f3c0 (mlv-app cursor 9f1a9cf):** doctrine-sync scope 50, scorecard fold_lag 49, S1 below 6; 44 of 50 touch only cos-feedback/.

**Rules, each with its check**
- **S1 One debt definition.** debt = bus commits after the cursor that change TRAPS.md, RECEIPTS.md, RULINGS.md or specs/** other than the board's own (doctrine-sync.mjs:80). cos-feedback/ never counts; CoS asks (Layi, 2026-10-15) are unchanged, and `doctrine-sync check` still lists it (:78). TRAPS.md leaves when R14.5 retires it (RULINGS.md:2881). Check: `doctrine-sync debt --project <p> --json`.
- **S2 One cursor.** `.codex-state/doctrine/last-seen.json`, written only by `doctrine-sync ack`.
- **S3 One trigger.** `fleet-sweep` or R15.3's hook. Due when debt > 0 AND (last fold 6 h old OR debt >= 10 OR a RULINGS.md change unfolded). The sweep only alarms (fleet-sweep.mjs:43-45; exit 1 = some member stale), so the board's existing wake tick reads its row of the receipt and launches S4's lane. The task must add `--json-out` (RULINGS.md:1149-1152); the counted receipt is from the host that clones the board (elsewhere `no-local-clone`, fleet-sweep.mjs:181). Check: that receipt within 2 h.
- **S4 One fold procedure.** A bounded headless lane records adopt or distinguish per item and acks past each (F5). Check: the acked sha is the last item decided.
- **S5 One measure.** The scorecard row carries S1 debt; oldest unfolded commit under 24 h. A breach goes to the board's thread as a CoS-CONCERN with an ACK ask (Layi, 2026-10-16); no new CoS duty. Check: fold_lag equals S1 once the steward adds a debt-only cos-feedback/ exclusion to `doctrine-sync.mjs` (BUS_SURFACES :34 stays for `check`) and `fleet-scorecard.py:68-69`; the parity test (`fleet-scorecard.tests.py:411-416`) moves with it.
- **S6 One install.** Debt command and fold prompt come from one pinned bus tool; none is copied.

**Adoption (per board):** delete private debt counters and second cursors; reply ADOPT or DISTINGUISH in `specs/<project>.md` (R14.8). Topbuilder and magic-lantern come from the owner's request and need a spec first. Supersedes card FLEET-FOLD-LOOP-SINGLE-SOURCE-1 (MLV ledger, SUBJECT DECLARED 2026-10-16T20:48:24Z).
