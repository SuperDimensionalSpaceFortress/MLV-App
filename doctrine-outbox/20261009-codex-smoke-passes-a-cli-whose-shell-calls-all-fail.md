---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/codex-smoke-passes-a-cli-whose-shell-calls-all-fail
rule: an upgrade smoke for a Codex CLI must run one real shell command under `--sandbox read-only` and fail the version when the output carries "setup refresh had errors"; a model reply that says READY proves the API and the shim, not that a lane can read a file.
mechanism: tools/cli-currency.py smoke() runs `codex exec --sandbox read-only` with a prompt that asks only for the word READY (cmd built at line 287, verdict `rc == 0 and reply == READY` at line 295, shim check at line 265). A model answers READY without calling a tool, so the smoke passed codex 0.161.0 and 0.162.0 while every shell call in a read-only exec failed with "windows sandbox failed: helper_unknown_error: setup refresh had errors". The fleet-wide hold file (policy/cli-currency-hold.json, read by held() at line 151) therefore never fired, and the updater kept installing the blind releases.
check: `codex exec --ephemeral --skip-git-repo-check --sandbox read-only -` with stdin "Run `git --version` and print its output"; expect a version line, and treat the string "setup refresh had errors" in the output as a failed smoke (zero-token form: `codex sandbox -P :read-only -C <repo> -- git --version`, exit 0)
supersedes: none
evidence: measured
- **Guard:** none yet (proposal to the bus cli-currency owner; MLV does not own tools/cli-currency.py)
