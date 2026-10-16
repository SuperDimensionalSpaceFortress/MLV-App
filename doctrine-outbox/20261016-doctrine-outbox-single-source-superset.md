---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/one-config-driven-doctrine-outbox-for-the-fleet
rule: the fleet carries ONE doctrine_outbox.py, byte-identical in every project and in the bus, with every project value in a doctrine-outbox.config.json beside it. A project never edits the tool, so the census sees one version.
mechanism: tool-census hashes whole files. The outbox was copied, then edited per project: 3 versions (bus reference 449 lines, airmypc 1099, mlv-app 1635), so each fix landed in one copy and drifted from the rest. The mlv-app copy is now the config-driven superset and also answers the bus reference's hook commands (check, check-range, pre-push, stop, session-start, install-hooks): a config with outbox_dir coordination/doctrine-outbox, sent_dir sent, trailer_mode bare, default_ref master and the three finding_path_regexes reproduces the reference's behaviour.
applies: mlv-app, airmypc, agent-bridge, conjugal
check: on the bus run `node tools/tool-census.mjs`; once the file below is adopted in the bus and vendored in airmypc and agent-bridge, the doctrine-outbox family reads 1 distinct version.
supersedes: none
evidence: measured
- **Adopt:** bus steward replaces tools/conjugal-reference/doctrine_outbox.py with tools/coordination/doctrine_outbox.py from MLV-App master (PR OUTBOX-SINGLE-SOURCE-1), byte for byte; sha256 of its LF bytes a6beb9cf8fd425c6e19507202c577ce2063ed38126369766d65ad23ec26fd084. Conjugal keeps its paths in its own config.
- **Vendor:** airmypc and agent-bridge copy the same bytes and keep a config of their own; verify with the sha256 above.
- **Open:** the reference's Python entry drain(cfg, hr, project) for the harvest steward is not in the shared file; the steward calls the CLI `drain` or keeps a thin shim.
