---
target: RECEIPTS.md
kind: receipt
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-25 - receipt: the CoS-first-reader baseline and the adversary-pass calibration

**Fleet baseline.** Meter: tools/coordination/quality_lift/Measure-FleetQualityLift.ps1 (SuperDimensionalSpaceFortress/MLV-App); bus cos-feedback 2026-10-18 to 55cbd8f. Blocked: the CoS names a current blocker (a B<n> item, a B<n> that persists or remains, a failing CI run). Clean: explicit no-blocker text. Anything else is UNKNOWN (unclassified column): counted apart, never clean or blocked; rates exclude it. "CoS first" (no declared review evidence before CoS) and "true miss" (a declared approval before CoS blocked) use only review-sources.json; only mlv-app declares (review/*).

| project | CoS passes | blocked | unclassified | first-round blocked PRs | CoS first | true misses | rounds per PR |
|---|---|---|---|---|---|---|---|
| mlv-app | 268 | 62 (25%) | 16 | 29/174 | 47/62 | 13 | 1.45 |
| adversarialllm | 46 | 18 (40%) | 1 | 10/23 | unknown | unknown | 1.92 |
| agent-bridge | 30 | 5 (17%) | 0 | 4/25 | unknown | unknown | 1.2 |
| adobe-ingester | 17 | 10 (59%) | 0 | 4/4 | unknown | unknown | 4.25 |
| fleet (6 projects) | 364 | 96/347 (28%) | 17 | | 47/62 | 13 | |

context-ultra-salesforce: 1 of 2 blocked; cloudvore: 1 clean pass. Unclassified: no Blockers section, governance or readiness holds, P2 findings. Correction: r3 published 69/342 at bus 4928a22 from a leading first-line B<n> only, so prose, CI bullets and later paragraphs read clean; this meter reads 81 blocked, 17 unclassified there.

Before CoS, mlv-app heads carried only review/fable, review/sol, review/opus. New mlv-app PRs with an Adversary pass line, 24 h to 2026-10-26 07:18Z: 20 of 44.

**mlv-app calibration.** One read-only Codex pass per head, given worktree, diff and commit subjects, never the CoS text. A hit (criterion fixed before reading results) is the CoS blocker's file, code region and invariant.
- Four heads factory keys approved and CoS later blocked: 4 of 4 hit blind (3 exact, 1 same line and invariant): a caller window loosening a 3 h lease budget; null plan entries dropped before validation; a file vanishing between existence check and hash; a default-parameter binder escaping a stale-literal guard.
- Two heads CoS called clean: 1 major false positive (pre-existing) and 3 minor harness items; hence only introduced blocker or major findings bind.
- On merged #677, called clean by CoS, the pass found a real low-reach regression the diff introduced.
- Cost: 421-833 s lane time per head; 1108-1780 s end to end, mostly admission wait.

**Falsifier:** rerun the meter on a bus clone at 55cbd8f with -NoFetch and -Since 2026-10-18T00:00:00Z (the pin ends the window); different counts falsify this receipt (statuses can be added later).
