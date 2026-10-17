---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/one-config-driven-doctrine-outbox-pr-470
rule: the fleet carries ONE doctrine_outbox.py, in the bus at tools/factory/, pinned by each project through factory-pins; every project value sits in doctrine-outbox.config.json at the project root. A project never edits the tool and keeps its local copy (see Pin).
mechanism: tool-census hashes whole files: the outbox was copied and edited per project (3 versions: bus reference 449 lines, airmypc 1099, mlv-app 1635), so each fix drifted. The mlv-app copy is the candidate for tools/factory/doctrine_outbox.py: the config-driven superset that also answers the old hook commands (check, check-range, pre-push, stop, session-start, install-hooks).
applies: mlv-app, airmypc, agent-bridge, conjugal
check: on the bus run `node tools/tool-census.mjs`; once projects run the file by pin from tools/factory/ and drop their copies, the doctrine-outbox family reads 1 distinct version.
supersedes: mlv-app/one-config-driven-doctrine-outbox-for-the-fleet (its PR 468 digest)
evidence: measured
- **Adopt:** bus steward adds tools/coordination/doctrine_outbox.py from MLV-App PR 470 as tools/factory/doctrine_outbox.py, byte for byte; sha256 of its LF bytes 9e6f2b55c2a646db7c218cf460851b55bb0eeff2390c4473ed358ad1b98eaabf.
- **Pin:** each project reviews that bus SHA, then runs `python factory-pins/pin-runner.py pin doctrine-outbox --commit <bus-sha> --path tools/factory/doctrine_outbox.py --reviewer <who> --reason <why>`. A pinned run is `python factory-pins/pin-runner.py run doctrine-outbox -- --config doctrine-outbox.config.json <command>`. The project KEEPS its copy: deleting it bricks the push hook and reads debt as UNKNOWN TOOL_ABSENT_AT_REF.
- **Follow-up:** OUTBOX-PIN-AWARE-TOOL-AT-REF-1 makes the hook invocation config-driven and `require_tool_at_ref` accept a pinned tool. Only after it ships does a project delete its copy.
