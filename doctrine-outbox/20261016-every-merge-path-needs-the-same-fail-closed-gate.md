---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/every-merge-path-needs-the-same-fail-closed-gate
rule: every merge or enqueue path calls one fail-closed decision function and all other paths are retired; it reads only structured sources (legs are head-bound commit statuses `review/<seat>` with run id and report hash, CoS asks come only from the CoS bus record, `.github/` approvals only from a hub-owned record bound to repo, PR, head and blob hash) and re-checks right before the merge: CoS scan always re-run (never cached), approvals record and file and blob reads first, leg statuses the LAST external read before the merge call.
mechanism: PRs 452, 453, 454 and 460 went through an enqueue script that read neither legs nor open CoS asks (ACK 6216909806). The first replacement parsed PR comment text. Three Codex review rounds of that parser found 7, 6 and 6 blockers, each a new false-ALLOW class in free text under one shared account: quoted or fenced blocks accepted, one commenter supplying both families and the approval, a reposted old approval outranking a newer CHANGES_REQUESTED, any author's ACK answering an ask, a stale evaluation consumed at merge. One account writes every comment, so hardening only moves the hole; the parser was stopped on PR 454 (comment 6220283608), left in shadow, and replaced by the sources above. Two ungated scripts were made to refuse. The gate's own review found the ordering fault twice (statuses read before the slow reads; a cached scan reused), both fixed before approval.
check: gh api repos/SuperDimensionalSpaceFortress/MLV-App/issues/comments/6220283608 --jq .body prints the stop decision and the design; legs: see card local-review-keys-are-not-legs-until-published.
supersedes: none
evidence: reported
- **Guard:** grep the repo for every call of the merge or enqueue command; each routes through the one function, and one hostile test per hole (incl. a failure posted during the scans) is RED against the old gate
