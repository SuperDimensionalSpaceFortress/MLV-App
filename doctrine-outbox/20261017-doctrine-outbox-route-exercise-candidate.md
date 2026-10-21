---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/one-config-driven-doctrine-outbox-exercise-3
rule: the fleet carries ONE doctrine_outbox.py, in the bus at tools/factory/, pinned by each project through factory-pins; every project value sits in doctrine-outbox.config.json at the project root. A project never edits the tool and keeps its local copy (see Pin).
mechanism: tool-census hashes whole files, so per-project copies drift. The mlv-app copy is the candidate for tools/factory/doctrine_outbox.py: the config-driven superset of the hook commands, with a fail-closed `validate --exercise` (routes are checked in a scratch clone, never the live bus; the clone and read-only git routes run with no host git config, filter or hook; the rest are existence-checked).
applies: mlv-app, airmypc, agent-bridge, conjugal
check: on the bus run `node tools/tool-census.mjs`; once projects pin tools/factory/ and drop their copies, the family reads 1 distinct version.
supersedes: mlv-app/one-config-driven-doctrine-outbox-pr-470 (its digest; same file plus route check)
evidence: measured
- **Adopt:** bus steward adds tools/coordination/doctrine_outbox.py from MLV-App card CARD-ROUTE-EXERCISE-3 as tools/factory/doctrine_outbox.py, byte for byte; sha256 of its LF bytes 0c73c10198bb940b97ba2f624deda4df79aa24ddfa55c12200c30ad447867e57.
- **Pin:** each project reviews that bus SHA and runs `python factory-pins/pin-runner.py pin doctrine-outbox --commit <bus-sha> --path tools/factory/doctrine_outbox.py --reviewer <who> --reason <why>`. A pinned run is `python factory-pins/pin-runner.py run doctrine-outbox -- --config doctrine-outbox.config.json <command>`. KEEP the local copy: deleting it bricks the push hook and reads debt as UNKNOWN TOOL_ABSENT_AT_REF.
- **Pin-aware hook:** OUTBOX-PIN-AWARE-TOOL-AT-REF-1 shipped: with `tool_pin` set, the hook and `require_tool_at_ref` accept the pinned tool; a project deletes its copy only once its pin is reviewed.
