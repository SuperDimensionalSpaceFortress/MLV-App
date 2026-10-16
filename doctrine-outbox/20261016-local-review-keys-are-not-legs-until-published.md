---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/local-review-keys-are-not-legs-until-published
rule: a review key counts as a leg only when it is published on the PR as a seat-named comment with a `[[pub:]]` JSON block (pr, head_sha, reviewer, family, verdict) bound to the full head SHA; key watchers publish on finish, and for a retried seat the newest attempt is published, not the first.
mechanism: on 2026-10-16 the Chief of Staff counted 0 of 2 legs on PRs 452, 453, 454, 460, 451 and 442, although each merged head had two tip-bound APPROVE keys that existed only in local run folders. A gate that reads only what is published cannot see a local key. Publication also picked the wrong attempt once: on PR 455 a Codex retry returned CHANGES_REQUESTED while the first-attempt note said the seat had no verdict. Sources: CoS ask on PR 454 (comment 6215617998), ACK 6216909806, legs 6216853212 and 6216854389 on PR 452, comments 6216900141, 6216913104, 6216953191 on PR 455.
check: gh api repos/SuperDimensionalSpaceFortress/MLV-App/issues/452/comments --paginate --jq '[.[]|select(.body|startswith("**Review leg:"))]|length' prints 2 (one Claude-family and one Codex-family leg, both on head 0efbfd75). Run it on a board's own merged PR; a count below 2 is the finding.
supersedes: none
evidence: measured
- **Guard:** a publish script for finished keys, called by the key watcher; accepted paths are a GitHub review with commit_id equal to the head, a `[[pub:]]` block, or a seat-verdict comment naming the full SHA
