---
target: RECEIPTS.md
kind: receipt
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-25 - receipt: the CoS-first-reader baseline and the adversary-pass calibration

Data for the same-date CoS-first-reader candidate.

**Fleet baseline.** The meter (tools/coordination/quality_lift/Measure-FleetQualityLift.ps1 in SuperDimensionalSpaceFortress/MLV-App), bus cos-feedback 2026-10-18 to 2026-10-25 23:09Z (bus 4928a22). "CoS first" and "true miss" count only declared review evidence (review-sources.json): none before the CoS pass; a declared approval before CoS blocked. mlv-app declares its review/* keys; the others declare nothing, so read unknown. The proxy (no status of any context before CoS) proves neither a review nor its absence.

| project | CoS passes | blocked | first-round blocked PRs | CoS first | true misses | proxy | rounds per PR |
|---|---|---|---|---|---|---|---|
| mlv-app | 140 | 35 (25%) | 18 of 91 | 30 of 35 | 5 | 30 of 35 | 1.54 |
| agent-bridge | 25 | 2 (8%) | 2 of 22 | unknown | unknown | 2 of 2 | 1.14 |
| adversarialllm | 20 | 8 (40%) | 5 of 10 | unknown | unknown | 8 of 8 | 2 |
| adobe-ingester | 17 | 8 (47%) | 2 of 4 | unknown | unknown | 8 of 8 | 4.25 |
| fleet | 202 | 53 (26%) | | 30 of 35 (1 of 4 declared) | 5 | 48 of 53 | |

Before CoS, mlv-app heads carried only review/fable, review/sol, review/opus. New PRs with an Adversary pass line, last 24 h: 4 of 36.

**mlv-app calibration.** One read-only Codex pass per head, given worktree, diff and commit subjects, never the CoS text. A hit (criterion fixed before reading results) is the CoS blocker's file, code region and invariant.
- Four heads factory keys approved and CoS later blocked: 4 of 4 hit blind (3 exact, 1 same line and invariant): a caller window loosening a 3 h lease budget; null plan entries dropped before validation; a file vanishing between existence check and hash, so an unsent request read as sent; a default-parameter binder escaping a stale-literal guard.
- Two heads CoS called clean: 1 major false positive (a pre-existing gap) and 3 minor harness items; hence only introduced blocker or major findings bind.
- On merged #677, called clean by CoS, the pass found a real regression: a date subtraction moved outside the base's try/catch, so a minimum-date marker throws out of startup recovery where the base answered "unknown". Low reach, introduced by the diff.
- Cost: 421-833 s lane time per head; 1108-1780 s end to end, mostly admission wait.

**Falsifier:** rerun the meter for the same window; different counts falsify this receipt (statuses can be added later). Declaring review evidence turns an unknown into a count.
