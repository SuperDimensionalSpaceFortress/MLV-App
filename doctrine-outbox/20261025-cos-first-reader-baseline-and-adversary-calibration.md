---
target: RECEIPTS.md
kind: receipt
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-25 - receipt: the CoS-first-reader baseline and the adversary-pass calibration

Data for the same-date candidate on CoS as the first reader.

**Fleet baseline.** The meter (tools/coordination/quality_lift/Measure-FleetQualityLift.ps1 in SuperDimensionalSpaceFortress/MLV-App) over bus cos-feedback commits from 2026-10-18 to 2026-10-25 21:19Z. "CoS first": no factory commit status on the head before the CoS pass. "True miss": a status said success before CoS blocked.

| project | CoS passes | blocked | first-round blocked PRs | CoS first | true misses | CoS rounds per PR |
|---|---|---|---|---|---|---|
| mlv-app | 137 | 34 (25%) | 17 of 88 | 29 of 34 | 5 | 1.56 |
| agent-bridge | 25 | 2 (8%) | 2 of 22 | 2 of 2 | 0 | 1.14 |
| adversarialllm | 20 | 8 (40%) | 5 of 10 | 8 of 8 | 0 | 2 |
| adobe-ingester | 17 | 8 (47%) | 2 of 4 | 8 of 8 | 0 | 4.25 |
| fleet | 199 | 52 (26%) | | 47 of 52 | 5 | |

New PRs with an Adversary pass line in the last 24 h: 0 of 32.

**mlv-app calibration.** One read-only Codex pass per head, given only the worktree, the diff and the commit subjects, never the CoS text. A hit is the same file, code region and invariant as the CoS blocker, a criterion fixed before the results were read.
- Four heads that factory keys approved and CoS later blocked: 4 of 4 hit blind (3 exact, 1 at the same line and invariant with another input): a caller window that loosened a 3 h lease budget; null plan entries dropped before validation; a file that vanished between an existence check and its hash, so an unsent request was treated as sent; a default-parameter binder that escaped a stale-literal guard.
- Two heads CoS called clean: 1 major false positive (a gap the pass itself called pre-existing) and 3 minor test-harness items. Hence only blocker or major findings the diff introduced bind the producer.
- On one of those clean heads, merged PR #677, the pass found a real regression CoS had called clean: a date subtraction moved outside the base's try/catch, so a marker carrying the minimum date now throws out of startup recovery where the base answered "unknown". Low reach, but introduced by the diff.
- Cost: 421 to 833 s of lane time per head; 1108 to 1780 s end to end, mostly admission wait.

**Falsifier:** rerun the meter on the bus for the same window; different counts mean this receipt is wrong (true misses also read GitHub statuses, which can be added later).
