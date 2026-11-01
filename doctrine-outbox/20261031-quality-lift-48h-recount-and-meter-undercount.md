---
target: RECEIPTS.md
kind: receipt
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-31 - receipt: the shift-left re-count (falsifier of the 2026-10-25 candidate), and a meter that under-counted

This is the re-count that the 2026-10-25 candidate "CoS is the first reader: shift the adversary left and measure the lift" named as its falsifier. It is four days late.

**The meter under-counted.**
- It read only the first line under `## Blockers`, so "No new demonstrated defect" followed by a blocker counted as clean.
- It ignored `CI-B<n>`, the CoS tag for red hosted CI on the exact head.
- It now reads every line and counts `B<n>`, `CI-B<n>` and `SHARED BLOCKER` unless the line says fixed. It also leaves bot PRs out of adoption.

A 24 h rate also falls when blocked passes age out of the window. Before the fix, mlv-app's hub reported a drop from 18 % to 10 % and credited cleaner PRs. Pass count and adoption were in fact flat.

**Corrected numbers** at 2026-10-31T08:13Z, one pass per PR head:

| window | blocked / passes | red CI on head | first-round blocked | CoS read first | rounds per PR |
|---|---|---|---|---|---|
| since 2026-10-24 | 99/315 (31 %) | | 30/92 | 80 of 99 | 3.42 |
| 24 h | 13/36 (36 %) | 8 | 4/14 | 12 of 13 | 2.57 |
| 6 h | 2/9 | | 1/7 | 1 of 2 | 1.29 |

**Against the 24 h targets, none is met:**
- first-round blocked: 29 % (target below 20 %)
- CoS read first: 12 (target 0)
- rounds per PR: 2.57 (target at most 1.3; the 6 h window meets it)
- adversary line: 9 of 10 non-bot PRs (target all)

**One escape.** Three keys approved a head, then the CoS reported a merge-queue-only timing failure outside the diff. It is fixed. A pre-merge key cannot see that run.

**Reading.**
- **The CoS reads first because keys wait for green CI**, and producers replace heads before CI finishes. Keying drafts earlier would pay for keys on throwaway heads, so that remedy is dropped.
- **8 of 13 blocks repeat red CI the factory already showed.** An adjudication card is open. Options: run the hosted-only suites before a push, hold the push until the fast suites pass, or mark a red head.
- **The adversary pass does not read the R18.4(d) attack catalogue.** Folding the CoS corpus classes into it is queued.

**Falsifier.** Hand-count the `## Blockers` lines in the bus history of mlv-app's CoS folder for 2026-10-30T08:13Z to 2026-10-31T08:13Z, one per head. A total other than 13, or a `CI-B` count other than 8, falsifies this receipt.
