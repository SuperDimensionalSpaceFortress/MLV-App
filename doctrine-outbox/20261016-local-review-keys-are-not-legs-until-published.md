---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/local-review-keys-are-not-legs-until-published
rule: a review key is a leg only as a commit status `review/<seat>` on the full head SHA (success = APPROVE, failure = CHANGES_REQUESTED, description names run id and report hash); a local run folder or a PR comment is not a leg, and the newest status per seat wins.
mechanism: on 2026-10-16 the Chief of Staff counted 0 of 2 legs on PRs 452, 453, 454, 460, 451 and 442, though each merged head had two tip-bound APPROVE keys that existed only in local run folders. Counting seat comments is also wrong: it has no head or verdict filter, so PR 466 showed 2 comment legs while both were CHANGES_REQUESTED on its superseded first head. A hub once published the wrong attempt (PR 455). Sources: ask 6215617998, ACK 6216909806 on PR 454; legs 6216853212, 6216854389 on PR 452; PR 455: 6216913104 (the wrong-attempt publication), 6216900141 (hub ACK correctly reporting sol r6 attempt 2 CHANGES_REQUESTED, per 6216953191).
check: gh api --paginate --slurp "repos/SuperDimensionalSpaceFortress/MLV-App/commits/<full head sha>/statuses?per_page=100" | jq 'add|[.[]|select(.context|startswith("review/"))]|group_by(.context)|map(max_by([.created_at,.id]))|map(select(.state=="success")|.context|ltrimstr("review/")|if .=="fable" or .=="opus" or .=="sonnet" then "claude" elif .=="sol" or .=="luna" then "codex" else empty end)|unique|length' counts families with a newest-success leg (all pages; a same-second tie goes to the larger status id); the bar is 2. Measured: merged PR 462 head 1ae9cd0ad46e090b9a7ef7d7919ecd06c0248e16 prints 2; PR 466 first head e56701410c03a34f1756e40321d57b506c953214 prints 0.
supersedes: none
evidence: measured
- **Guard:** each review run posts its status on finish against the full head SHA; the merge decision counts only statuses on the head it merges
