---
target: RECEIPTS.md
kind: receipt
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-09 - codex 0.161.0 and 0.162.0 read-only exec is blind on Windows: measured split, residual risk, prior art, and a hold PROPOSAL for the bus owner

**Measured event:** On codex-cli 0.161.0 (global npm install), read-only review lanes on this host could not run any shell command, and 20 review keys (2026-10-07T18:40Z to 2026-10-08T09:36Z) returned verdicts without having read the repo. This host's CLI-Currency receipts show 0.161.0 installed from 2026-10-07T17:27Z and 0.162.0 first installed at 2026-10-08T23:27Z, after all 20 keys; they do not record a version per key. The 0.162.0 re-measurement is two zero-token controls, run twice (by the MLV PR #329 producer and this filing's author):

- `codex sandbox -P :read-only -C <repo> -- git --version` exits 1: "windows sandbox failed: helper_unknown_error: setup refresh had errors".
- The same command with `-c 'windows.sandbox="unelevated"'` exits 0: "git version 2.40.0.windows.1".

The Codex sandbox log carries the cause: "runtime read/execute validation failed ... node_repl.exe: open ACL target for root-only update: The process cannot access the file because it is being used by another process. (os error 32)". Which process holds the file (Codex Desktop, per the hub's adjudication) was not re-measured here.

**Under the unelevated sandbox**, a real read-only lane ran `git --version` and a write attempt was denied, in the producer's acceptance run of PR #329. **Residual risk:** unelevated is a restricted token, weaker isolation than elevated, and network egress under it was not measured.

**Prior art, distinguished:** TRAPS.md, topbuilder-interface, 2026-10-08 (bus commit b825b4f) recorded the symptom and chose `--sandbox danger-full-access`. The card adobe-ingester/codex-0161-readonly-sandbox-fails used the same workaround; adobe-ingester/pin-falsifier-cli-privately superseded it with a private 0.160.1 install. This filing names the cause, and its remedy keeps the read-only boundary instead of removing it. A search of TRAPS.md, RECEIPTS.md, RULINGS.md and specs for "setup refresh", "unelevated", "os error 32" and "node_repl" found only those same-incident entries.

**PROPOSAL, addressed to the bus owner (this is not a ruling, and MLV does not own the file):** add `{"codex": ["0.161.0", "0.162.0"]}` to policy/cli-currency-hold.json (R13 item 4) until a release passes the control above. The hold only protects machines not yet on these versions; the others need the per-call fix. Companion proposal: a smoke that runs one shell command under `-s read-only` (card mlv-app/codex-smoke-passes-a-cli-whose-shell-calls-all-fail). On this host the bash shim check fails for the new and the previous codex version alike, so the both-fail rule (R13.2) would still keep the upgrade: report the shim verdict separately from the rollback decision, or fix the shim, before a smoke can trigger a rollback.

**Guard:** per-call fix enforced by tools/coordination/Invoke-Lane.ps1 + tests/coordination/test_lane_containment.py via MLV PR #329 (open at filing); the hold itself: none yet (proposal).

**Re-run:** the two control commands above, with Codex Desktop running.
