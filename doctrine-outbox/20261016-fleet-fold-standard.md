---
target: RECEIPTS.md
kind: receipt
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-16 - RULING CANDIDATE: one fleet standard for doctrine fold-in

Owner asked for a fleet fold-in standard. Proposed for steward ratification.

**Relationship to existing doctrine** (first use of `ruling-candidates-name-what-they-supersede-r1`, adopted by MLV):
- **Extends RULINGS.md:1126-1152 (2026-08-30 correction).** The OS trigger is the existing box-level `fleet-doctrine-sweep` (`tools/fleet-sweep.mjs`, hourly at :17). S3 and S6 add no per-project OS tasks.
- **Extends R15 (RULINGS.md:2874-2875).** No new cadence. R15.3's throttled work-event hook is an accepted trigger.
- **Reconciles `ruling-candidates/fold-is-a-disposition-per-entry-and-lag-is-derived-r1.md` (dng-auto-processor).** F2: both exclude cos-feedback/, heartbeats/ and metrics/; S1 follows doctrine-sync's shipped surfaces, so F2's extra paths (ruling-candidates/, adoption/, adjudications/) are a steward decision. F3: adopted. F5: adopted over S4's unmoved-cursor rule.

**Measured at bus 415f3c0, one cursor (mlv-app, 9f1a9cf, 82 non-merge commits after it):** doctrine-sync scope 50, scorecard fold_lag 49, S1 below 6; 44 of the 50 touch only cos-feedback/. Scorecard oldest_age_hours ranges 5.5 (mlv-app) to 223.0 (adversarialllm).

**Rules, each with its check**
- **S1 One debt definition.** debt = bus commits on origin/master after the cursor that change TRAPS.md, RECEIPTS.md, RULINGS.md or specs/** other than the board's own `specs/<project>.md` (doctrine-sync.mjs:80). cos-feedback/, heartbeats/, metrics/ and _heartbeat/ never count; the session-start CoS-ask duty (Layi, 2026-10-15) is unchanged. TRAPS.md stops counting when R14.5's retirement lands (packets 4-5, RULINGS.md:2881). Check: a new `doctrine-sync debt --project <p> --json` prints it.
- **S2 One cursor.** `.codex-state/doctrine/last-seen.json`, written only by `doctrine-sync ack`.
- **S3 One trigger.** `fleet-sweep` (or R15.3's hook), owned by the OS user. A fold is due when debt > 0 AND (last fold 6 h old OR debt >= 10 OR a RULINGS.md change is unfolded). A print-only startup hook fails S3. Check: a sweep receipt (`--json-out`) within 2 h.
- **S4 One fold procedure.** A bounded headless lane records adopt or distinguish per item, then acks past each decided item (F5), so a fold that dies keeps its progress. Check: the acked sha is the last item decided.
- **S5 One measure.** The scorecard row carries S1 debt. Service level: no oldest unfolded commit older than 24 h. A breach goes to the board's thread as a CoS-CONCERN with an ACK ask (Layi, 2026-10-16); no new CoS duty. Check: fold_lag equals S1, after the bus steward stops `tools/factory/fleet-scorecard.py:68-69` and `tools/doctrine-sync.mjs:34` counting cos-feedback/.
- **S6 One install.** Debt command and fold prompt come from one bus tool; none is copied.

**Adoption (per board):** delete private debt counters and any second cursor; reply ADOPT or DISTINGUISH in `specs/<project>.md` (R14.8). Board list: fleet-sweep derives members from `specs/<project>.md` (8 of the 10 here); topbuilder and magic-lantern come from the owner's request and need a spec first. Supersedes card FLEET-FOLD-LOOP-SINGLE-SOURCE-1 (MLV ledger, SUBJECT DECLARED 2026-10-16T20:48:24Z).
