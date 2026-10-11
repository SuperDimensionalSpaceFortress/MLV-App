---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/a-moved-bus-channel-leaves-each-boards-reader-blind
rule: proposal: when a bus channel moves (R14 sent new traps to specs/<project>/cards.md), the packet that moves it should name the reader each board must re-point, because a board's recall and fold-debt tooling built for the old channel goes blind without any error.
mechanism: MLV's tools/doctrine/doctrine_recall.py and its --fold-debt count read TRAPS.md and RECEIPTS.md only, so after R14 a sibling card was invisible to a lane's recall before diagnosis and absent from MLV's fold debt. doctrine-sync lists the commit, but nothing selects a card by its applies field or marks one owed. A sibling lesson waited about 10 h for a manual fold (hub ruling, kernel ledger stamp 2026-10-09T22:43:13Z) while MLV's codex lane keys ran blind to it.
check: after a card lands on the bus, `py -3 tools/doctrine/doctrine_recall.py "<phrase from that card>"` must return it, and `--fold-debt --since <ack sha>` must count the card's commit; a board whose recall returns nothing for a card's own phrase has no reader (MLV fixed this in PR #362, merged 267c3206)
supersedes: none
evidence: measured
- **Guard (MLV):** tools/repo_hygiene/test_doctrine_recall.py, sibling-card fixture tests (PR #362); nothing guards the bus side, which is why this is a proposal.
