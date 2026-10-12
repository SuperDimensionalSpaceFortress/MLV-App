---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/codex-ignored-config-key-is-not-the-requested-mode
rule: a launcher that passes a containment setting to codex as `-c <key>=<value>` must not record that value as the mode the lane ran; when codex's stderr preamble says the key was ignored, the receipt records `requested-<value>-ignored-by-codex` with the matched line, never the requested value.
mechanism: codex 0.162.0 accepts an unknown `-c` key with exit 0 (sol, PR #329 r1: `windows.sandbox_unknown_probe="unelevated"` exit 0, while an invalid VALUE for a known key exits 1), prints "warning: Codex is ignoring N unrecognized configuration setting(s)" plus one indented "<source>: `<key>` is ignored." line, and runs on its default. tools/coordination/Invoke-Lane.ps1 recorded `authority.windowsSandbox` from the argv it built, so a build without the `windows.sandbox` key would have produced a receipt claiming `unelevated` for a lane running elevated or otherwise contained. The fix (Get-CodexIgnoredWindowsSandboxLine) scans only codex's own preamble: it stops at the first bare `codex` turn line, because a transcript can quote a nested run's warning, and it removes the echoed prompt first, because a prompt can quote it too. No zero-token probe shows the warning: `features list`, `mcp list` and an empty-prompt `exec` all exit before it (0.162.1).
check: run the lane with stderr carrying the warning block naming `windows.sandbox` and read the receipt: `authority.windowsSandbox` must be `requested-unelevated-ignored-by-codex` with `windowsSandboxIgnoredDetail` quoting the line; with no such line (or only `cli_auth` ignored) it must stay the requested mode.
supersedes: none
evidence: measured
- **Guard:** tests/coordination/test_lane_containment.py `test_codex_receipt_never_claims_a_windows_sandbox_mode_codex_ignored` (RED on fork/master 29f52739) and the two `test_codex_receipt_keeps_the_requested_windows_sandbox_*` rows
