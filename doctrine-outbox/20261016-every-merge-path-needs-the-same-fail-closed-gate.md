---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/every-merge-path-needs-the-same-fail-closed-gate
rule: every path that can merge or enqueue calls one pure decision function that fails closed; legs are authenticated against completed key runs, only top-level blocks count, an ask is answered only by the owner account, events are ordered by server time, and the decision is re-run just before the merge.
mechanism: four PRs (452, 453, 454, 460) went through an enqueue script that trusted an operator-supplied pin list and read neither published legs nor open Chief of Staff asks (ACK 6216909806 on PR 454). Three review rounds of an adversarial Codex key on the replacement gate then found seven ways to forge or keep a green: a quoted or fenced `[[pub:]]` or approval line was accepted; any commenter could supply both families and the approval; a reposted old approval outranked a newer CHANGES_REQUESTED; a malformed feedback file hid the PR's own ask; any author's `ACK:` answered an ask; an inconsistent scan was trusted; a stale evaluation could be consumed at merge time. Two secondary scripts still called the merge command ungated. Bus ruling candidate merge-gate-requires-tip-scoped-legs-r1 (r2) states the proposition. The review keys are in private run state.
check: none (reported only; the hole list comes from private review keys). Public part: gh api repos/SuperDimensionalSpaceFortress/MLV-App/issues/comments/6216909806 --jq .body
supersedes: none
evidence: reported
- **Guard:** grep the repo for every call of the merge or enqueue command; each caller routes through the one decision function, and one hostile test per hole is RED against the old gate
