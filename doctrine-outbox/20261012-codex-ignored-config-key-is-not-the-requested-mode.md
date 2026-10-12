---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/codex-ignored-config-key-is-not-the-requested-mode
rule: a launcher that passes a containment setting to codex as `-c <key>=<value>` must not record that value as the mode the lane ran; when codex's stderr preamble says the key was ignored, the receipt records `requested-<value>-ignored-by-codex` with the matched line, never the requested value.
mechanism: codex 0.162.0 accepts an unknown `-c` key with exit 0 (sol, PR #329 r1), prints "warning: Codex is ignoring N unrecognized configuration setting(s)" plus one indented "<source>: `<key>` is ignored." line, and runs on its default. Invoke-Lane.ps1 recorded `authority.windowsSandbox` from its own argv, so a build without the key would yield a receipt claiming `unelevated`. The fix scans only codex's preamble: it stops at the first bare `codex` turn line (a transcript can quote a nested run) and skips the echoed prompt BY POSITION, the span after codex's `user` marker line, cut once. Removing the prompt as a substring is itself a trap: `windows.sandbox`, `windows` or the warning block as the prompt also occurs inside the genuine warning, erasing the evidence (sol, PR #407 r1). No zero-token probe prints the warning (0.162.1).
check: run the lane with stderr carrying the warning block naming `windows.sandbox` and read the receipt: `authority.windowsSandbox` must be `requested-unelevated-ignored-by-codex` with `windowsSandboxIgnoredDetail` quoting the line; with no such line (or only `cli_auth` ignored) it must stay the requested mode; a prompt overlapping the warning text must not mask a genuine one.
supersedes: none
evidence: measured
- **Guard:** tests/coordination/test_lane_containment.py `test_codex_receipt_never_claims_a_windows_sandbox_mode_codex_ignored` (RED on fork/master 29f52739) and the `test_codex_receipt_keeps_the_requested_windows_sandbox_*` rows; `test_codex_receipt_keeps_genuine_ignored_evidence_*` (RED on 53b786f5)
