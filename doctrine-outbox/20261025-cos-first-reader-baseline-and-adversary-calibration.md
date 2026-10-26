---
target: RECEIPTS.md
kind: receipt
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-25 - receipt: the CoS-first-reader baseline and the adversary-pass calibration

**Fleet baseline.** Meter: tools/coordination/quality_lift/Measure-FleetQualityLift.ps1 (SuperDimensionalSpaceFortress/MLV-App); bus cos-feedback 2026-10-18 to d4d92ca. Blocked: a Blockers line starts B<n> or SHARED BLOCKER and does not say fixed. Clean: the whole section is one whitelisted no-blocker text (blocker-classes.json). Everything else, prose and mixed sections included, is UNKNOWN (unclassified): counted apart, excluded from rates. "CoS first" (no declared review evidence before CoS) and "true miss" (declared approval, then CoS blocked) use only review-sources.json; only mlv-app declares.

| project | CoS passes | blocked | unclassified | first-round blocked PRs | CoS first | true misses | rounds per PR |
|---|---|---|---|---|---|---|---|
| mlv-app | 270 | 55 (42%) | 140 | 27/96 | 40/55 | 13 | 1.46 |
| adversarialllm | 46 | 16 (67%) | 22 | 9/15 | unknown | unknown | 1.92 |
| agent-bridge | 30 | 2 (10%) | 11 | 2/16 | unknown | unknown | 1.2 |
| adobe-ingester | 17 | 6 (100%) | 11 | 2/2 | unknown | unknown | 4.25 |
| fleet (6 projects) | 366 | 79/179 (44%) | 187 | | 40/55 | 13 | |

**UNKNOWN is 187 of 366 fleet passes (51%), 140 of 270 for mlv-app (52%):** rates cover only the classified minority. Two small projects: 3 passes, all unclassified. Correction: the previous meter read prose by meaning, misreading crafted prose (98 blocked, 17 unclassified); this moves 170 heads (19 blocked, 151 clean) to UNKNOWN, none between blocked and clean.

Before CoS, mlv-app blocked heads carried only review/fable, sol, opus and ci-integrity. New mlv-app PRs with an Adversary pass line, 24 h to 2026-10-26 08:30Z: 22 of 42.

**mlv-app calibration.** One read-only Codex pass per head, given worktree, diff and subjects, never the CoS text. A hit (criterion fixed before reading results) is the CoS blocker's file, code region and invariant.
- Four heads factory keys approved and CoS later blocked: 4 of 4 hit blind (3 exact, 1 same line and invariant): a caller window loosening a 3 h lease budget; null plan entries dropped before validation; a file vanishing between existence check and hash; a default-parameter binder escaping a stale-literal guard.
- Two heads CoS called clean: 1 major false positive (pre-existing) and 3 minor harness items; hence only introduced blocker or major findings bind.
- On merged #677, called clean by CoS, the pass found a real low-reach regression the diff introduced.
- Cost: 421-833 s lane time per head; 1108-1780 s end to end.

**Falsifier:** rerun the meter on a bus clone at d4d92ca with -NoFetch and -Since 2026-10-18T00:00:00Z; different counts falsify this receipt (statuses can be added later).
