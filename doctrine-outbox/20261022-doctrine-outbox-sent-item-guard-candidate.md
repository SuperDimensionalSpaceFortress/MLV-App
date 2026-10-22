---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/one-config-driven-doctrine-outbox-sent-item-guard-1
rule: the fleet carries ONE doctrine_outbox.py, in the bus at tools/factory/, pinned by each project through factory-pins; every project value sits in doctrine-outbox.config.json. A project never edits the tool; it sets `tool_pin` in its config once its pin is reviewed.
mechanism: per-project copies drift under tool-census. The mlv-app copy is the candidate, with a pin-aware hook (doctrine-outbox/README.md). New: `check-commits` and the pre-push guard refuse a commit that edits an outbox item whose key the sent ledger records (SENT_ITEM_EDITED), since the new key republishes it. A new item naming it on a `supersedes:` line is the one allowed edit; no ledger is the typed skip sent-ledger-absent.
applies: mlv-app, airmypc, agent-bridge, conjugal
check: on the bus run `node tools/tool-census.mjs`; once projects pin and drop their copies, the family reads 1 distinct version.
supersedes: mlv-app/one-config-driven-doctrine-outbox-markup-forms-1 (its digest)
evidence: measured
- **Adopt:** bus steward adds tools/coordination/doctrine_outbox.py as tools/factory/doctrine_outbox.py, byte for byte; sha256 of its LF bytes 1a717b0f2aea99a960986a872b8bfa347b2a0db9afeec77b974da8e6b2c3be01.
- **Pin:** each project reviews that bus SHA, then runs `python factory-pins/pin-runner.py pin doctrine-outbox --commit <bus-sha> --path tools/factory/doctrine_outbox.py --reviewer <who> --reason <why>`, runs it as `python factory-pins/pin-runner.py run doctrine-outbox -- --config doctrine-outbox.config.json <command>`, sets `"tool_pin": "doctrine-outbox"`, runs `install-hooks` that way once. OUTBOX-PIN-AWARE-TOOL-AT-REF-1 shipped the pin-aware hook; only after the pin is reviewed does a project delete its copy. Without `tool_pin` it KEEPS it.
