---
target: RECEIPTS.md
kind: receipt
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-11-05 - receipt: blind keys overlap with an outside reader but do not replace it (K6, K12)

**Method.** 10 old heads the outside reader had blocked (no factory key, spaced evenly by rank in time order) plus 3 it had passed were keyed blind by both families: 26 keys, given the sha, diff and file list only. This project scored its own run; no second party reviewed the catch calls.

**Result: 13 reader blockers on the 10 blocked heads.** Caught: Claude 6, Codex 5, either family 8, both 3. Missed by both 5. Disagreed 0. 25 of 26 keys blocked.
- **Taint:** one catch may be tainted: its diff cites the reader's earlier blocker. Without it, either family caught 7 of 12; also without two catches with adjacent reader text, 5 of 10.
- **CI-visible split:** 5 of 13 were test or census failures; either family caught 3. On the other 8, either caught 5. Both families missed 2 census failures and 3 reader-only blockers, so 3 of 13 were named by neither a key nor the CI-visible class, assuming one census failure fails hosted CI (not run).
- 5 of the 8 catches came from one family only. 8 of 13 has a rough 95% interval of 32 to 86%; blockers cluster by head.

**Controls: no key false-block rate is stated, all three controls are unclean.** One retains defects in the reader's later prose. On one, a Claude key approved a defect the PR later fixed after the factory's adversary found it. On one, a blocking finding repeats a limit the reader noted without blocking.

**K6, default for a project with no outside reader.** The result does not support two keys equalling two keys plus an outside reader. The default is conservative, not measured: two keys from different families; hosted CI green on the exact head; merged-tree census step kept; a third key on high-risk diffs; keys judged by sampled post-merge escapes. Specificity is unmeasured.

**K12.**
- **A blind prompt is not a blind session.** The CLI git snapshot put commit subjects in key context until `CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS=1` was set (absent in round-two session files, CLI 2.1.318). Audit sessions.
- **User-scope hooks reach headless keys.** All 13 Claude keys saw the study's name. Codex keys get no such hook, so exposure is on one family; its effect is unmeasured.
- **Meter:** its exclusion dropped any blocker line containing "fixed" or "closed", even "fail-closed", which explains never-keyed 73 of 119 against 66 of 111. Fixed the same day: old-format records moved 14 in and 1 out; the new parser, reading all formats, gives 142 of 461 (old: 111 of 411).

Evidence (local to mlv-app): `results.md` in `lane-KERNEL-INDEPENDENT-READER-HOLDOUT-1-R2-r1-20261104T2300Z` and `lane-KERNEL-INDEPENDENT-READER-HOLDOUT-1-r1-20261104T1835Z`.
