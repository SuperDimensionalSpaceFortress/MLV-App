---
target: RECEIPTS.md
kind: receipt
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-25 - receipt: the CoS-first-reader baseline and the adversary-pass calibration

**Fleet baseline.** The meter (tools/coordination/quality_lift/Measure-FleetQualityLift.ps1 in SuperDimensionalSpaceFortress/MLV-App), bus cos-feedback 2026-10-18 to bus 4928a22 (2026-10-25 23:09Z). "CoS first" (no declared review evidence before the CoS pass) and "true miss" (a declared approval before CoS blocked) count only review-sources.json evidence; only mlv-app declares (review/*). The proxy (no status before CoS) proves nothing about reviews.

| project | CoS passes | blocked | first-round blocked PRs | CoS first | true misses | proxy | rounds per PR |
|---|---|---|---|---|---|---|---|
| mlv-app | 248 | 43 (17%) | 25 of 179 | 33 of 43 | 10 | 33 of 43 | 1.39 |
| adversarialllm | 44 | 15 (34%) | 9 of 23 | unknown | unknown | 15 of 15 | 1.91 |
| agent-bridge | 30 | 2 (7%) | 2 of 25 | unknown | unknown | 2 of 2 | 1.2 |
| adobe-ingester | 17 | 9 (53%) | 3 of 4 | unknown | unknown | 9 of 9 | 4.25 |
| fleet (6 projects) | 342 | 69 (20%) | | 33 of 43 | 10 | 59 of 69 | |

Two more projects had 3 clean passes. Correction: an earlier draft read 202 passes, 53 blocked. That meter skipped files naming their pass only in cos-feedback.v1 front matter (the other 140 passes) and read listed or bold blockers (`- B1`, `- **B1 / P1:**`, `Blockers: B1`) as clean.

Before CoS, mlv-app heads carried only review/fable, review/sol, review/opus. New mlv-app PRs with an Adversary pass line, 24 h to 2026-10-26 02:27Z: 12 of 40.

**mlv-app calibration.** One read-only Codex pass per head, given worktree, diff and commit subjects, never the CoS text. A hit (criterion fixed before reading results) is the CoS blocker's file, code region and invariant.
- Four heads factory keys approved and CoS later blocked: 4 of 4 hit blind (3 exact, 1 same line and invariant): a caller window loosening a 3 h lease budget; null plan entries dropped before validation; a file vanishing between existence check and hash; a default-parameter binder escaping a stale-literal guard.
- Two heads CoS called clean: 1 major false positive (pre-existing) and 3 minor harness items; hence only introduced blocker or major findings bind.
- On merged #677, called clean by CoS, the pass found a real low-reach regression the diff introduced.
- Cost: 421-833 s lane time per head; 1108-1780 s end to end, mostly admission wait.

**Falsifier:** rerun the meter on a bus clone at 4928a22 with -NoFetch and -Since 2026-10-18T00:00:00Z (the pin ends the window); different counts falsify this receipt (statuses can be added later).
