---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/one-config-driven-doctrine-outbox-markup-forms-1
rule: the fleet carries ONE doctrine_outbox.py, in the bus at tools/factory/, pinned by each project through factory-pins; every project value sits in doctrine-outbox.config.json. A project never edits the tool; it sets `tool_pin` in its config once its pin is reviewed.
mechanism: per-project copies drift under tool-census. The mlv-app copy is the candidate: a config-driven superset of the old hook commands, a pin-aware hook and ref check (doctrine-outbox/README.md), and a `validate` refusal of a ruling candidate with no `touches/supersedes:` list of rule ids or bus paths at a given bus clone's tip, read once per item. No `tool_pin` behaves as before.
applies: mlv-app, airmypc, agent-bridge, conjugal
check: on the bus run `node tools/tool-census.mjs`; once projects pin and drop their copies, the family reads 1 distinct version. In MLV-App `py -3 -m pytest -q tests/coordination/test_doctrine_outbox_hooks.py -k pin` pushes through the hook with no copy.
supersedes: mlv-app/one-config-driven-doctrine-outbox-ruling-supersedes-1 (its digest)
evidence: measured
- **Adopt:** bus steward adds tools/coordination/doctrine_outbox.py as tools/factory/doctrine_outbox.py, byte for byte; sha256 of its LF bytes 02c189bb2312c624fd85dd815c0b2e7c0e8e3fd2903753e56a0f6dce4f50edfc.
- **Pin:** each project reviews that bus SHA, then runs `python factory-pins/pin-runner.py pin doctrine-outbox --commit <bus-sha> --path tools/factory/doctrine_outbox.py --reviewer <who> --reason <why>`, runs it as `python factory-pins/pin-runner.py run doctrine-outbox -- --config doctrine-outbox.config.json <command>`, sets `"tool_pin": "doctrine-outbox"`, runs `install-hooks` that way once. OUTBOX-PIN-AWARE-TOOL-AT-REF-1 shipped the pin-aware hook; only after the pin is reviewed does a project delete its copy. Without `tool_pin` it KEEPS it.
