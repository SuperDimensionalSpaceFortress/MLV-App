---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/the-fold-publish-share-loop-is-rebuilt-per-board
rule: proposal to the bus steward, not an instruction to any board: promote the loop that folds sibling doctrine, publishes a board's own findings and reads its product share into one project-neutral bus tool, driven by a per-project profile (paths, kinds, ack cursor, outbox target), so a board adopts it by writing a profile and no code.
mechanism: MLV carries its own copy of each stage: tools/coordination/doctrine_outbox.py (publish; a port of agent-bridge's port of the bus reference outbox), tools/doctrine/doctrine_recall.py with --fold-debt (read and fold debt), tools/coordination/Test-ProductRatioGuard.ps1 (share). The bus holds doctrine-sync.mjs, Publish-BoardHeartbeat.ps1 and adobe-ingester's product-control-ratio.mjs for the same stages. Each fault found this week (a cards reader, an owed count, a label by prefix) had to be fixed once per board.
check: falsifier: a second board gets fold debt, outbox drain and product share by committing one profile file; if adoption needs a code edit, the profile boundary is drawn in the wrong place
supersedes: none
evidence: measured
- **Guard:** none yet (proposal; the stage copies listed above are the measured part, the shared tool is untested).
