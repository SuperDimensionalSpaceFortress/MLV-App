---
target: RECEIPTS.md
kind: receipt
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-11-06 — Kernel K4: a codex lane that exits 0 without a real answer is `ended-incomplete`, not `complete`

**Before:** `Get-LaneWorkEvidence` (`tools/coordination/lane-provider-refusal.ps1`) set `workCompleted = $true` for every codex run with exit 0, so `Invoke-Lane.ps1` booked an empty, zero-width-only or unusable answer as `state=complete`. A key that answered nothing then read as a finished key that gave no verdict (PR #736).
**Now (exit 0 first; non-zero keeps `exit-code-N`):**
1. answer empty after removing whitespace and Unicode Cf characters (U+200B, U+FEFF) -> `codex-empty-answer`;
2. cleaned answer under **120 characters** and prompt 400+ characters -> `codex-answer-too-short`;
3. prompt spells out a `{"verdict": ...}` json object (every review key and the adversary pass) and the answer's FINAL json object has no non-empty `verdict` -> `codex-no-verdict`. The verdict must be a non-blank JSON String (r4): an Array, Object, Boolean or Number books `codex-verdict-wrong-kind-<Kind>`.
All book `ended-incomplete`. The answer judged is the `-o` last-message file, else stdout, never stderr; refusal detection judges the same answer. An answer is a refusal (exit 125) only when every line is an ERROR-framed refusal line, an ERROR-framed stderr line names the same kind (r3), and the prompt does not contain every answer line verbatim (r8: a refusal is never text the prompt contains). Residual: a prompt quoting a refusal line, then a genuine refusal with exactly that line at exit 0, reads `complete`; all 4 recorded codex refusals exited 1. The final json is the LAST candidate by position across fenced and unfenced forms; a fence closes only on the same character, at least as long as its opening. Contract strings compare ordinally (r5: `-ceq` ignores NUL and zero-width).
**Evidence:** replayed over 2380 lane-run codex receipts (`complete`, exit 0, answer file kept). The 7 answers under 120 characters are smoke probes; every 400+ byte prompt got 231+ characters. The floor rejects 0 real answers. Of 2289 verdict-demanding prompts, one answer lacked a verdict: a key saying `"verdict":null`, "no review verdict was issued" (true positive, booked `complete` before).
**Narrowed promise:** relevance is not decidable by the runner. A LONG off-topic answer to a prompt that demands no verdict is still booked `complete`, as is a 120+ character wrong answer that is verdict-shaped. The floor and verdict shape bound the cheap failures (nothing, a stub, `{}`, a poem, "still working"); relevance stays with the consumer.
**Falsifier:** `Get-LaneWorkEvidence -Engine codex -ExitCode 0` returns `workCompleted=False` for `''`, U+200B only, `'{}'`, `'Still working; please wait.'`, and (with `-RequireVerdict`) 120+ characters of prose without a final json `verdict`. A prompt `Reply with exactly: ERROR: HTTP 429` answered exactly, beside stderr `ERROR: upstream probe returned 429; recovered`, is `complete`, exit 0. Tests: `test_codex_*`, `test_invoke_lane_wiring_*`, `test_r8_*` in `tools/coordination/test_coordination_guardrails.py`.
