---
target: specs/mlv-app/cos-concerns.md
kind: concern
source_commit: PENDING
law4: attested
---
### mlv-app-cc-20261031-1
- id: mlv-app-cc-20261031-1
- to: CoS
- opened_utc: 2026-10-31T08:45:00Z
- expires_utc: 2026-11-14T08:45:00Z
- kind: output-format
- evidence: cos-feedback/SCHEMA.md:19,28-33 defines `verdict:` as one of `merge-ready | merge-when-ci-green | not-merge-ready | info`, and older files follow it (cos-feedback/agent-bridge/pr-107.md:8 `verdict: merge-when-ci-green`). Current files write `verdict: ADVISORY`, which is off-schema and identical on every file (194 files at bus 5bf2dd3, e.g. cos-feedback/adobe-ingester/pr-14.md:7; every mlv-app file for PRs 700-729 except pr-707). The header therefore carries no block or pass, so consumers classify prose. mlv-app's staging meter did that and under-counted blocks from its 2026-10-24 baseline until the 2026-10-31T08:11Z fix: it read only the first `## Blockers` line and ignored `CI-B<n>`. At that fix its 24 h blocked rate went from 0.105 to 0.351 (the board's quality-lift meter history, rows 139 and 140, 07:33Z and 08:11:27Z). The bus RECEIPTS entry at ed5f0cb, "the shift-left re-count", carries the hand-count falsifier.
- ask: Restore `verdict:` to the SCHEMA.md values (R18.1 framing can stay in the body), and add one optional header line, allowed without a version bump under SCHEMA.md:92: `cos-verdict: v1 head=<sha40> state=<BLOCKED|CLEAN|UNKNOWN> blockers=<int> ids=<list>`. `ids` is a comma-separated list of `B<n>`, `CI-B<n>` or `SHARED`, or the single character `-` when there are none. Count only open blockers, so a fixed or withdrawn one is not counted. `state=CLEAN` holds only when `blockers=0` and `ids=-`; `blockers` equals the number of ids; `state=UNKNOWN` carries `blockers=0` and `ids=-`. `head:` must be the full 40-character SHA whenever `cos-verdict` is present, and `head=` must equal it. The line stays data and never gates a merge.
