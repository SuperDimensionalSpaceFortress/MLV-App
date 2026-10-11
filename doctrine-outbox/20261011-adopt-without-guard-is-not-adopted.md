---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/adopt-without-guard-is-not-adopted
rule: an ADOPT of a bus card closes only when a tracked guard with a RED fixture (an input the check must fail) has merged; a deploy that lives only in untracked board tooling, or a ledger line, stays recorded as unenforced and keeps the card owed.
mechanism: a fold marks a card adopted when the lesson is written down or a local script is deployed, so the count of adopted cards says nothing about whether the failure can still recur unnoticed. On this board 24 queued ADOPTs resolved as 8 merged PRs, 10 untracked board tooling only and 6 still ledger lines, while the tracked guard registry held 4 entries.
check: for each ADOPT row compute the state merged-with-RED-fixture | untracked-tooling | ledger-only, and the ratio of tracked guards to ADOPTs; a row in either unenforced state carries a guard card and is not counted as closed. The registry size is the REGISTRY tuple in tools/repo_hygiene/test_doctrine_guards.py.
supersedes: none
evidence: measured
