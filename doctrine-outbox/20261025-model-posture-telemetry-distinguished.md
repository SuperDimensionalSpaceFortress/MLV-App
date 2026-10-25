---
target: RECEIPTS.md
kind: receipt
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-25 — MODEL-POSTURE-TELEMETRY: DISTINGUISHED (RULINGS.md R20.5 adoption record; MLV publishes no posture shard yet)

MODEL-POSTURE-TELEMETRY: DISTINGUISHED (this board runs no posture-shard writer: no pin of tools/factory/model-posture.py, and no provider-usage-event-v1 rows to reduce; R20.4's merge wait is already over, PR #91 merged 2026-10-23T14:44:36Z)

**Before:** the card that queued this record gave the reason as "R20.4: no board publishes posture shards before bus PR #91 merges". That reason is stale. `gh pr view 91 --repo layibabalola/softwarefactory-fleet-doctrine --json state,mergedAt` returns MERGED at 2026-10-23T14:44:36Z, so R20.4 no longer holds publication back.

**Built:** nothing publishes. A search of this repository for `provider-usage-event`, `model-posture.py` and `posture-YYYY` finds no match outside this record, factory-pins/factory-pins.json carries no pin for the writer, and the bus holds no `metrics/mlv-app/` shard. What the board does record per lane run is `requestedModel`, `resolvedModel` and `effort` in each lane receipt, read by tools/coordination/check-lane-model-currency.py for STALE-MODEL and BAD-EFFORT. That is a local check, not a published aggregate.

**Proof:** the R19.1 clause 3 reassessment counts from each board's own key receipts (R19.4), and this board's receipts already carry the fields it needs. Telemetry is diagnostic only (R20.1), so declining it blocks nothing.

**Falsifier:** if this board later pins tools/factory/model-posture.py and runs `publish`, this record is superseded by a `MODEL-POSTURE-TELEMETRY: ADOPTED (<where>)` entry citing the pin and the first shard. Until then, a shard under `metrics/mlv-app/posture-*.jsonl` on the bus would contradict this record.
