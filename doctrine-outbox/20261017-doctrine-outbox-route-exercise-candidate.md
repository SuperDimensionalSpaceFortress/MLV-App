---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/one-config-driven-doctrine-outbox-exercise-1
rule: the fleet carries ONE doctrine_outbox.py, in the bus at tools/factory/, pinned by each project through factory-pins; every project value sits in doctrine-outbox.config.json at the project root. A project never edits the tool and keeps its local copy (see Pin).
mechanism: tool-census hashes whole files, so a per-project copy drifts. The mlv-app copy is the candidate for tools/factory/doctrine_outbox.py: the config-driven superset of the old hook commands, now with `validate --exercise` (runs every command a card names in a scratch clone, never in the live bus) and a refusal of a ruling candidate with no `touches/supersedes:` list.
applies: mlv-app, airmypc, agent-bridge, conjugal
check: on the bus run `node tools/tool-census.mjs`; once projects run the file by pin from tools/factory/ and drop their copies, the doctrine-outbox family reads 1 distinct version.
supersedes: mlv-app/one-config-driven-doctrine-outbox-pr-470 (its digest; the same file plus the two checks above)
evidence: measured
- **Adopt:** bus steward adds tools/coordination/doctrine_outbox.py from MLV-App card CARD-ROUTE-EXERCISE-1 as tools/factory/doctrine_outbox.py, byte for byte; sha256 of its LF bytes b13b370ea045a46094b8b4ad67097d11b4783a224296409b92223617278f671e.
- **Pin:** each project reviews that bus SHA, then runs `python factory-pins/pin-runner.py pin doctrine-outbox --commit <bus-sha> --path tools/factory/doctrine_outbox.py --reviewer <who> --reason <why>`. A pinned run is `python factory-pins/pin-runner.py run doctrine-outbox -- --config doctrine-outbox.config.json <command>`. The project KEEPS its copy: deleting it bricks the push hook and reads debt as UNKNOWN TOOL_ABSENT_AT_REF.
- **Follow-up:** OUTBOX-PIN-AWARE-TOOL-AT-REF-1 makes the hook invocation config-driven and `require_tool_at_ref` accept a pinned tool. Only after it ships does a project delete its copy.
