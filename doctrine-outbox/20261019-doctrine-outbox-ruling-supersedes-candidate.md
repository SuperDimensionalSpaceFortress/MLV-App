---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/one-config-driven-doctrine-outbox-ruling-supersedes-1
rule: the fleet carries ONE doctrine_outbox.py, in the bus at tools/factory/, pinned by each project through factory-pins; every project value sits in doctrine-outbox.config.json at the project root. A project never edits the tool; it sets `tool_pin` in its config once its pin is reviewed, and only then drops its local copy.
mechanism: tool-census hashes whole files, so a per-project copy drifts. The mlv-app copy is the candidate for tools/factory/doctrine_outbox.py: the config-driven superset of the old hook commands, the pin-aware hook and ref check (a `tool_pin` config has the installed hook run the pin and `require_tool_at_ref` accept the pin runner plus a pins file naming it, so a project that pinned the outbox and deleted its copy no longer bricks its own pushes), and a `validate` refusal of a ruling candidate (marker read after HTML and Unicode rendering, where a block opens) with no `touches/supersedes:` list (alias `Relationship to existing doctrine:`) of rule ids or bus paths at the bus tip. A config without `tool_pin` behaves as before.
applies: mlv-app, airmypc, agent-bridge, conjugal
check: on the bus run `node tools/tool-census.mjs`; once projects run the file by pin from tools/factory/ and drop their copies, the doctrine-outbox family reads 1 distinct version. In MLV-App `py -3 -m pytest -q tests/coordination/test_doctrine_outbox_hooks.py -k pin` runs a fixture that pins the tool, holds no copy, installs the hook and pushes through it.
supersedes: mlv-app/one-config-driven-doctrine-outbox-pr-470 (its digest; same file plus the check), mlv-app/pin-aware-doctrine-outbox-hook-and-ref-check (its digest; same file plus the ruling-candidate refusal)
evidence: measured
- **Adopt:** bus steward adds tools/coordination/doctrine_outbox.py as tools/factory/doctrine_outbox.py, byte for byte; sha256 of its LF bytes 2b50558037d2d0d472162676d04ed077e0c09bd191d7cce85391e75e7a9fa84e.
- **Pin:** each project reviews that bus SHA, then runs `python factory-pins/pin-runner.py pin doctrine-outbox --commit <bus-sha> --path tools/factory/doctrine_outbox.py --reviewer <who> --reason <why>`. A pinned run is `python factory-pins/pin-runner.py run doctrine-outbox -- --config doctrine-outbox.config.json <command>`. Then set `"tool_pin": "doctrine-outbox"` in the config and run `install-hooks` through that pinned command once. Without `tool_pin` the hook and the debt checks read the local copy, so the project KEEPS it.
- **Follow-up:** OUTBOX-PIN-AWARE-TOOL-AT-REF-1 shipped the hook and ref check. Only after the bus holds the file and the project's pin is reviewed does that project delete its copy.
