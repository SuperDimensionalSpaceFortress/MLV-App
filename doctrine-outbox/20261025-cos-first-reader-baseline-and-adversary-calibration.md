---
target: RECEIPTS.md
kind: receipt
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-25 - receipt: the CoS-first-reader baseline and the adversary-pass calibration

**Fleet baseline.** Every number here was measured by the mlv-app hub staging meter (not shipped), conservative classification, r5 run on a bus clone at d4d92ca (cos-feedback 2026-10-18 to 2026-10-26 08:54Z). UNKNOWN share: 51 % fleet (187 of 366 passes), 52 % mlv-app (140 of 270). Blocked: a Blockers line starts B<n> or SHARED BLOCKER and does not say fixed. Clean: the whole section is one whitelisted no-blocker text. All else (prose, mixed sections) is UNKNOWN, outside every rate. "CoS first" (no review evidence before CoS) and "true miss" (review approval, then CoS blocked) need declared review evidence; only mlv-app declares (review/* statuses).

| project | CoS passes | blocked (of classified) | UNKNOWN | first-round blocked (of classified PRs) | CoS first | true misses | rounds per PR |
|---|---|---|---|---|---|---|---|
| mlv-app | 270 | 55/130 (42 %) | 140 | 27/96 | 40/55 | 13 | 1.46 |
| adversarialllm | 46 | 16/24 (67 %) | 22 | 9/15 | unknown | unknown | 1.92 |
| agent-bridge | 30 | 2/19 (11 %) | 11 | 2/16 | unknown | unknown | 1.2 |
| adobe-ingester | 17 | 6/6 (100 %) | 11 | 2/2 | unknown | unknown | 4.25 |
| fleet (6 projects) | 366 | 79/179 (44 %) | 187 | | 40/55 | 13 | |

Two small projects: 3 passes, all UNKNOWN. Reading prose by meaning did not converge over five review rounds, so the meter stays unshipped until the CoS writes a machine-readable verdict line. New mlv-app PRs with an Adversary pass line, 24 h to 08:54Z: 24 of 44.

**mlv-app calibration.** One read-only Codex pass per head, given worktree, diff and subjects, never the CoS text. A hit (criterion fixed in advance) is the CoS blocker's file, code region and invariant.
- Four heads factory keys approved and CoS later blocked: 4 of 4 hit blind (3 exact, 1 same line and invariant): a caller window loosening a 3 h lease budget; null plan entries dropped before validation; a file vanishing between existence check and hash; a default-parameter binder escaping a stale-literal guard.
- Two heads CoS called clean: 1 major false positive (pre-existing), 3 minor harness items; hence only introduced blocker or major findings bind.
- Merged #677, called clean by CoS: the pass found a real low-reach regression the diff introduced.
- Cost: 421-833 s lane time per head; 1108-1780 s end to end.

**Falsifier:** hand-count one project's cos-feedback at d4d92ca under the rule above; a blocked head with no B<n> or SHARED BLOCKER line falsifies it. Statuses read as of 08:54Z.
