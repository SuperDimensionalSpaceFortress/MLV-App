---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/pin-aware-doctrine-outbox-hook-and-ref-check
rule: a project that runs doctrine_outbox.py by pin sets `tool_pin` in doctrine-outbox.config.json; the installed pre-push hook then runs the pin through factory-pins/pin-runner.py and the debt and drain ref check accepts the pin, so the project can drop its local copy.
mechanism: the hook ran `$REPO_ROOT/<tool_rel>` and require_tool_at_ref demanded `tool_rel` at the ref, so a project that pinned the shared outbox and deleted its copy bricked its own pushes (CoS repro on a4480947). A config without `tool_pin` behaves exactly as before. The file changed, so the digest below replaces the earlier card's.
applies: mlv-app, airmypc, agent-bridge, conjugal
check: `py -3 -m pytest -q tests/coordination/test_doctrine_outbox_hooks.py -k pin` in MLV-App: the fixture pins the tool, holds no copy, installs the hook and pushes through it.
supersedes: mlv-app/one-config-driven-doctrine-outbox-pr-470 (its digest; the tool gained `tool_pin`)
evidence: measured
- **Adopt:** bus steward adds tools/coordination/doctrine_outbox.py from the MLV-App PR OUTBOX-PIN-AWARE-TOOL-AT-REF-1 as tools/factory/doctrine_outbox.py, byte for byte; sha256 of its LF bytes 980884caa698efd4352e776300876d90edfd65c39dfbd8ad38627ecd9b5bb10a.
- **Pin:** each project reviews that bus SHA, then runs `python factory-pins/pin-runner.py pin doctrine-outbox --commit <bus-sha> --path tools/factory/doctrine_outbox.py --reviewer <who> --reason <why>`. A pinned run is `python factory-pins/pin-runner.py run doctrine-outbox -- --config doctrine-outbox.config.json <command>`. Then set `"tool_pin": "doctrine-outbox"` in the config and run `install-hooks` through that pinned command once.
- **Follow-up:** OUTBOX-PIN-AWARE-TOOL-AT-REF-1 shipped the hook and ref check. Only after the bus holds the file and the project's pin is reviewed does that project delete its copy.
