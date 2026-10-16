---
target: RECEIPTS.md
kind: receipt
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-16 - RULING CANDIDATE: one fleet standard for doctrine fold-in (supersedes queued card FLEET-FOLD-LOOP-SINGLE-SOURCE-1)

Owner asked for a fleet standard for consistent fold-in. Proposed for steward ratification; each board adopts or distinguishes.

**Measured at bus 415f3c0, one cursor (mlv-app, 9f1a9cf, 82 non-merge commits after it):** the doctrine-sync check scope counts 50, the scorecard fold_lag filter 49, the cards-only recall scope 4, S1 below 6. 44 of the 50 touch only cos-feedback/. Scorecard 2026-10-16 fold_lag oldest_age_hours (metrics/scorecard-daily.jsonl): mlv-app 5.5, adobe-ingester 17.9, airmypc 65.7, agent-bridge 121.0, adversarialllm 223.0; three of five breach 24 h. cloudvore, conjugal and magic-lantern publish no fold_lag; dng-auto-processor has a heartbeat but no scorecard row. delta_count is 1 on all seven heartbeats/, so it separates nothing. The bus cannot show which boards run a trigger; the owner reports that outside mlv-app startup hooks only print text (unverified).

**Rules, each with its check**
- **S1 One debt definition.** debt = bus commits on origin/master after the cursor that change TRAPS.md, RECEIPTS.md, RULINGS.md or specs/** outside specs/<project>/. cos-feedback/, heartbeats/, metrics/ and _heartbeat/ never count. Check: one bus command (a new `doctrine-sync debt --project <p> --json` mode) prints it; no board computes its own.
- **S2 One cursor.** <project>/.codex-state/doctrine/last-seen.json, written only by `doctrine-sync ack` after a fold. Check: no other path is read as a cursor.
- **S3 One trigger,** owned by the OS user (survives account rotation, works with no session open): a scheduled check every 10 minutes. A fold is due when debt > 0 AND (last fold 6 h old OR debt >= 10 OR any RULINGS.md change unfolded). A startup hook that only prints text fails S3. Check: a tick receipt within 20 minutes.
- **S4 One fold procedure.** A bounded headless lane records adopt or distinguish for each item (bus law 1: doctrine is data; fold only what is verified locally), then acks. A fold that cannot finish leaves the cursor unmoved. Check: the acked sha is the last item reviewed.
- **S5 One measure.** The daily scorecard row carries S1 debt and the age of the newest ack. Service level: no project's oldest unfolded commit older than 24 h; the CoS flags a breach on the bus. Check: fold_lag equals the S1 value.
- **S6 One install.** Trigger, debt command and fold prompt come from a single bus tool each project installs; no project copies it. Check: the census lists one source.

**Adoption checklist (per board: mlv-app, adobe-ingester, agent-bridge, airmypc, adversarialllm, cloudvore, conjugal, dng-auto-processor, magic-lantern, topbuilder):** install the S6 tool; delete private debt counters and any second cursor; register the S3 task; confirm the S5 row appears; reply adopt or distinguish in the next heartbeat.
