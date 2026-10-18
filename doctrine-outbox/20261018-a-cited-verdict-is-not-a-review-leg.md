---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/a-cited-verdict-is-not-a-review-leg
rule: a review verdict cited in a commit message, a PR body or a ledger line is never a leg; only a `review/<seat>` status on the exact head SHA, a `[[pub:]]` JSON comment whose `head_sha` equals the head, or a GitHub review whose `commit_id` equals the head counts, and no summary or ACK says "keyed" or "APPROVE" for a PR without one of the three on its current head.
mechanism: factory ACKs promised fresh legs on the final head while the open PRs showed 0 of 2 published legs on their current heads; on PR 462 the r2 keys were cited only in commit messages and r3 had none published. A push moves the head, so a verdict cited for an earlier head is not a leg for the current one. Sources: CoS ask 6219052910 and hub ACK 6223438673 on PR 454. Extends card local-review-keys-are-not-legs-until-published, which defines the status form; this one names the citation shape and the two other accepted forms.
check: gh api repos/SuperDimensionalSpaceFortress/MLV-App/issues/comments/6223438673 --jq .body prints the rule under item 3. For any open PR, a head SHA with a cited verdict and none of the three published forms is the finding.
supersedes: none
evidence: measured
- **Guard:** the board-local merge-enqueue hub tool refuses a merge or enqueue unless the status gate (rule TIP_LEGS) finds the legs on the head; it is untracked, so the tracked copy of the rule is in agents/orchestration-tiering.md
