---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/two-hubs-on-one-board-duplicate-outward-actions
rule: before any outward action (a PR comment, a dispatch), a hub reads the other hub's record of that action on the same target, and dispatch claims are keyed per card so a second claim is refused.
mechanism: an unattended hub tick and an interactive hub both worked the same Chief of Staff asks; about a minute apart they answered the same 12 asks, two answers conflicted, and a duplicate gate card was started and then cancelled. The public trace is PR 455: the first hub's ACK 6216900141 at 13:47:29Z, a second ACK 6216913104 at 13:48:12Z saying the Codex seat had no verdict, and a correction 6216953191 at 13:50:22Z after the first note showed the seat had returned CHANGES_REQUESTED. A second duplicate pair is on PR 458. The count of 12 and the cancelled card are in private hub state.
check: gh api repos/SuperDimensionalSpaceFortress/MLV-App/issues/455/comments --jq '[.[]|select(.id==6216900141 or .id==6216913104 or .id==6216953191)|.created_at]' lists three timestamps within about three minutes. Two ACKs from different hubs inside a minute on one PR is the finding.
supersedes: none
evidence: measured
- **Guard:** an outward-action ledger keyed by (target, kind) read before posting, and a claim file per card that a second dispatcher refuses
