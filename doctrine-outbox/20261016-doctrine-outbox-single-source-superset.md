---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/one-config-driven-doctrine-outbox-for-the-fleet
rule: the fleet carries ONE doctrine_outbox.py, in the bus at tools/factory/, run by each project through its factory-pins pin and never copied; every project value sits in a doctrine-outbox.config.json at the project root. A project never edits the tool.
mechanism: tool-census hashes whole files. The outbox was copied, then edited per project: 3 versions (bus reference 449 lines, airmypc 1099, mlv-app 1635), so each fix drifted from the rest. The bus deleted its reference copy and moved shared tools to the pin model, so the mlv-app copy is the candidate for tools/factory/doctrine_outbox.py: the config-driven superset, which also answers the old reference's hook commands (check, check-range, pre-push, stop, session-start, install-hooks).
applies: mlv-app, airmypc, agent-bridge, conjugal
check: on the bus run `node tools/tool-census.mjs`; once the file is in tools/factory/ and projects run it by pin with their copies deleted, the doctrine-outbox family reads 1 distinct version.
supersedes: none
evidence: measured
- **Adopt:** bus steward adds tools/coordination/doctrine_outbox.py from MLV-App master (PR OUTBOX-SINGLE-SOURCE-1) as tools/factory/doctrine_outbox.py, byte for byte; sha256 of its LF bytes ad9bf2a66dc2918ab2abd359a45693165f75482af82717ee4cddbef1b42da0b5.
- **Pin:** each project reviews that bus SHA, records it with `pin-runner.py pin doctrine-outbox --commit <bus-sha> --path tools/factory/doctrine_outbox.py`, calls it via `pin-runner.py run`, deletes its copy.
- **Open:** the reference's drain(cfg, hr, project) entry is not in the shared file (use the CLI `drain`); a project's pre-push snippet and tool_rel still name a local file until its own pin lands.
