---
target: RECEIPTS.md
kind: receipt
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-16 — throughput tooling outran the merge gate: merges tripled to 205 in a half-month, PRs shrank to a median 287 lines, and six PRs merged without published review legs

**Before:** on the user-account repo, 67 PRs merged between Sep 16 and Sep 30 (median 863 lines added), and the Sep 7-15 window shows at least 22 (the 300-row query starts mid Sep 7).
**Built:** the repo moved to an organization with the native merge queue on 2026-10-04 (after PR 256), the OS-scheduled hub autopilot was installed the same day with its first tick on 2026-10-05, and review keys became mechanical. Merges in Oct 1-15 reached 205, a median of 287 lines added. By title theme, about 96 were factory work (47 factory, hub or Chief of Staff, 29 other tooling, 20 CI), 43 playback, 24 venue, 21 Look Assist, 11 GPU and 4 HFR; 6 fit no theme.

**Proof:** the same speed let PRs 442, 451, 452, 453, 454 and 460 merge without a review-leg status on the merged head, because the merge path did not read legs; see the traps local-review-keys-are-not-legs-until-published and every-merge-path-needs-the-same-fail-closed-gate. The fix was one gate on every merge path, reading structured sources, not slower merging.

**Falsifier:** gh pr list --repo SuperDimensionalSpaceFortress/MLV-App --state merged --limit 300 --json number,mergedAt,additions returns about 205 rows with mergedAt in Oct 1-15, about 67 in Sep 16-30, and median additions of 287 and 863. A board that adds a queue, an autopilot or mechanical keys without a gate in the same change should expect the same: count merges whose head has no newest-success review/<seat> status for both families.
