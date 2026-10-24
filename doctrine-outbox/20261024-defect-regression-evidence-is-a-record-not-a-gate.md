---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/defect-regression-evidence-is-a-record-not-a-gate
rule: for each demonstrated defect (a key BLOCKER, a confirmed Dot/Grok finding, a merge_group failure that is not a flake) the landing PR body records a row: the regression test that is RED on the old sha and GREEN on the new (two different shas), the adversarial combination tests or "none" with a reason, and a disposition of fixed, distinguished: <reason> or deferred: <CARD>. A missing field reads MISSING and is never invented. The record adds no merge precondition: an incomplete or absent block prints one advisory WARN line and never blocks, and escape counts (merged defects, reverts, non-flake merge_group failures, repeat failures) come from the same records.
mechanism: 2026-10-24 the owner asked (dot notice on MLV-App #660) that escapes and repeat failures show up daily, and the hub re-scoped the card to evidence-only. Without a record, "the fix has a regression test" is a claim in a commit message; with one, the old-code RED sha and the combination test are named and a repeat of the same test id or claim across two PRs is countable. tools/coordination/defect_ledger.py renders, splices and checks the block (check and escapes always exit 0).
applies: mlv-app
check: in mlv-app, py -3 -m unittest tests.coordination.test_defect_ledger (51 cases: render roles and MISSING, idempotent splice, check exit 0 on every input, a 7-day escapes window with flake exclusion and repeat clusters, duplicate or unpaired markers and equal old/new shas never read as ok).
supersedes: none
evidence: measured
