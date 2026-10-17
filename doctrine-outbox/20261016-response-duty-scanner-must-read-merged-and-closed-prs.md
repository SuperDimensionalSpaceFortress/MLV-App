---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/response-duty-scanner-must-read-merged-and-closed-prs
rule: a response-duty scanner covers merged and closed PRs as well as open ones, and an ask is a comment that requests `ACK:`; a verdict or review note that merely mentions ACK is not an ask.
mechanism: the scanner skipped merged PRs, so asks posted after merge went unseen: post-merge asks on PRs 448, 451 and 454 were found by hand, and the ask on PR 409 sat unanswered for two days before an overdue ACK (comment 6218422209). Overdue ACKs on PR 427 (6217656028) are the same shape. The opposite error also cost work: a scanner that counted a verdict as an ask raised false positives, for example PR 446, whose review note says there is no open ask but mentions ACK and DISTINGUISH; the hub answered with a DISTINGUISH (6218423910) only to clear the thread. The false-positive count (11) is in a private hub ledger.
check: gh api repos/SuperDimensionalSpaceFortress/MLV-App/issues/409/comments --jq '.[]|select(.body|startswith("ACK: overdue"))|.id' prints 6218422209 and gh pr view 409 --repo SuperDimensionalSpaceFortress/MLV-App --json state --jq .state prints MERGED. A scanner that listed no open ask on 409 before that comment is the finding.
supersedes: none
evidence: measured
- **Guard:** scan every PR with a comment in the window regardless of state; a fixture holds one merged-PR ask (must be found) and one verdict mentioning ACK (must not be)
