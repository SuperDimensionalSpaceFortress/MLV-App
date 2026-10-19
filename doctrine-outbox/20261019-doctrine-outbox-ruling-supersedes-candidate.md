---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/one-config-driven-doctrine-outbox-ruling-supersedes-1
rule: the fleet carries ONE doctrine_outbox.py, in the bus at tools/factory/, pinned by each project through factory-pins; every project value sits in doctrine-outbox.config.json. A project never edits the tool; it sets `tool_pin` in its config once its pin is reviewed, then drops its local copy.
mechanism: tool-census hashes whole files, so per-project copies drift. The mlv-app copy is the candidate: a config-driven superset of the old hook commands, a pin-aware hook and ref check (see doctrine-outbox/README.md), and a `validate` refusal of a ruling candidate with no `touches/supersedes:` list of rule ids or bus paths at the bus tip. No `tool_pin` behaves as before.
applies: mlv-app, airmypc, agent-bridge, conjugal
check: on the bus run `node tools/tool-census.mjs`; once projects pin and drop their copies, the family reads 1 distinct version. In MLV-App `py -3 -m pytest -q tests/coordination/test_doctrine_outbox_hooks.py -k pin` pushes through the hook with no local copy.
supersedes: mlv-app/one-config-driven-doctrine-outbox-pr-470 (its digest; same file plus the check), mlv-app/pin-aware-doctrine-outbox-hook-and-ref-check (its digest)
evidence: measured
- **Adopt:** bus steward adds tools/coordination/doctrine_outbox.py as tools/factory/doctrine_outbox.py, byte for byte; sha256 of its LF bytes 2b50558037d2d0d472162676d04ed077e0c09bd191d7cce85391e75e7a9fa84e.
- **Pin:** each project reviews that bus SHA, then runs `python factory-pins/pin-runner.py pin doctrine-outbox --commit <bus-sha> --path tools/factory/doctrine_outbox.py --reviewer <who> --reason <why>`, runs the tool as `python factory-pins/pin-runner.py run doctrine-outbox -- --config doctrine-outbox.config.json <command>`, sets `"tool_pin": "doctrine-outbox"`, runs `install-hooks` that way once, then deletes its copy. Without `tool_pin` the project KEEPS it.
