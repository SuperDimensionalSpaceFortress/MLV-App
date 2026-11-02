---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/one-config-driven-doctrine-outbox-nlf-old-git-refuse-1
rule: the fleet carries ONE doctrine_outbox.py, in the bus at tools/factory/, pinned by each project through factory-pins; every project value sits in doctrine-outbox.config.json. A project never edits the tool; it sets `tool_pin` in its config once its pin is reviewed.
mechanism: per-project copies drift under tool-census. The mlv-app copy is the candidate, pin-aware (doctrine-outbox/README.md). This revision makes the bus read refuse, by name, a git that ignores GIT_NO_LAZY_FETCH (older than 2.44, or a version it cannot read) when the bus clone is a partial (promisor) clone, so a ruling-target check can never lazy-fetch from the bus's remote mid-lookup; a full bus clone, or a git that honours the variable, reads as before.
applies: mlv-app, airmypc, agent-bridge, conjugal
check: on the bus run `node tools/tool-census.mjs`; once projects pin and drop their copies, the family reads 1 distinct version.
supersedes: mlv-app/one-config-driven-doctrine-outbox-comment-history-1 (its digest)
evidence: measured
- **Adopt:** bus steward adds tools/coordination/doctrine_outbox.py as tools/factory/doctrine_outbox.py, byte for byte; sha256 of its LF bytes 4e29138114f4f6a04484605d11fcd3502f639eada1f6a84599b4a1f518411fa5.
- **Pin:** each project reviews that bus SHA, then runs `python factory-pins/pin-runner.py pin doctrine-outbox --commit <bus-sha> --path tools/factory/doctrine_outbox.py --reviewer <who> --reason <why>`, runs it as `python factory-pins/pin-runner.py run doctrine-outbox -- --config doctrine-outbox.config.json <command>`, sets `"tool_pin": "doctrine-outbox"`, runs `install-hooks` that way once. OUTBOX-PIN-AWARE-TOOL-AT-REF-1 shipped the pin-aware hook; only after the pin is reviewed does a project delete its copy. Without `tool_pin` it KEEPS it.
