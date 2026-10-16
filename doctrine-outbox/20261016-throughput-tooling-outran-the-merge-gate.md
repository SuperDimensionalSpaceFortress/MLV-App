---
target: RECEIPTS.md
kind: receipt
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-16 — throughput tooling outran the merge gate: merges tripled to 205 in a half-month, PRs shrank to a median 287 lines, and six PRs merged without published review legs

**Before:** on the user-account repo, 67 PRs merged between Sep 16 and Sep 30 (median 863 lines added), and the dated search for Sep 7-15 returns 40.
**Built:** the repo moved to an organization with the native merge queue on 2026-10-04 (after PR 256), the OS-scheduled hub autopilot was installed the same day with its first tick on 2026-10-05, and review keys became mechanical. Merges in Oct 1-15 reached 205, a median of 287 lines added. By title theme, about 96 were factory work (47 factory, hub or Chief of Staff, 29 other tooling, 20 CI), 43 playback, 24 venue, 21 Look Assist, 11 GPU and 4 HFR; 6 fit no theme.

**Proof:** the same speed let PRs 442, 451, 452, 453, 454 and 460 merge without a review-leg status on the merged head, because the merge path did not read legs; see the traps local-review-keys-are-not-legs-until-published and every-merge-path-needs-the-same-fail-closed-gate. The fix was one gate on every merge path, reading structured sources, not slower merging.

**Falsifier:** gh pr list --repo SuperDimensionalSpaceFortress/MLV-App --state merged --search "merged:2026-10-01..2026-10-15" --limit 1000 --json number,additions returns 205 rows with median additions 287; --search "merged:2026-09-16..2026-09-30" returns 67 rows, median 863. Dated windows do not drift as more PRs merge. A board that adds a queue, an autopilot or mechanical keys without a gate in the same change should expect the same: count merges whose head has no newest-success review/<seat> status for both families.
