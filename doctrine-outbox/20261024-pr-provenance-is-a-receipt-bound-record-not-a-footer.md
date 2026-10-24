---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/pr-provenance-is-a-receipt-bound-record-not-a-footer
rule: a PR states which provider and exact model played each role (implementer, fixer, reviewer keys, third key, verifier, execution agent) from the lane receipts, not from an authorship footer: provider from the receipt engine, the resolved model id (or "claimed: <requested>" when none was resolved), and the full 40-hex SHA each row is bound to (a key's subject_sha, a producer's DONE sha), with "unbound" when it cannot be derived and "stale (other head)" when it names an older head. A footer is "claimed by footer, unverified". CI is listed apart as not a model. The section is record-only: a missing or stale one prints one advisory WARN line and never blocks a merge.
mechanism: 2026-10-24 the owner asked (dot notice on MLV-App #660) for provider and model per role on every PR, because a Co-Authored-By footer names whatever the committer typed and says nothing about who reviewed which head. The receipts already carry engine, resolved model and effort; a key's verdict JSON carries subject_sha. tools/coordination/pr_topology.py renders, splices and checks the block (check always exits 0).
applies: mlv-app
check: in mlv-app, py -3 -m unittest tests.coordination.test_pr_topology (26 cases: render roles and stale rows, claimed/unreadable receipts, idempotent splice, check exit 0 on every input, malformed markers plus a stale head never read as ok).
supersedes: none
evidence: measured
