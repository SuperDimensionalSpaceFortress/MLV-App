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
All book `ended-incomplete`. The answer judged is the `-o` last-message file, else stdout, never stderr. Provider-refusal detection judges that same answer, so a recovered `429` line on stderr cannot turn a real file-only answer into `refused` (exit 125); an answer made only of ERROR-framed refusal lines is still a refusal ONLY when every answer line names a refusal kind and an ERROR-framed stderr line names one of those same kinds (r3: unrelated recovered stderr never overturns an answer such as `ERROR: file not found`). The final json is the LAST candidate by position across fenced and unfenced forms (`Get-LaneTerminalCandidate`); a fence closes only on the same character at least as long as its opening.
**Evidence:** replayed over 2380 lane-run codex receipts (`complete`, exit 0, answer file kept). The 7 answers under 120 characters are smoke probes (prompts under 200 bytes); every 400+ byte prompt got 231+ characters. The floor rejects 0 real answers. Of 2289 verdict-demanding prompts, one answer lacked a verdict: a key saying `"verdict":null`, "no review verdict was issued" (true positive, booked `complete` before).
**Narrowed promise:** relevance is not decidable by the runner. A LONG off-topic answer to a prompt that demands no verdict is still booked `complete`, as is a 120+ character wrong answer that is verdict-shaped. The floor and verdict shape bound the cheap failures (nothing, a stub, `{}`, a poem, "still working"); relevance stays with the consumer. A probe prompt under 400 characters may answer in one word.

**Falsifier:** `Get-LaneWorkEvidence -Engine codex -ExitCode 0` must return `workCompleted=False` for answers `''`, U+200B only, `'{}'`, `'Still working; please wait.'`, and (with `-RequireVerdict`) 120+ characters of prose without a final json `verdict`; a 120+ character answer returns `True`. With a 170-character last-message file, empty stdout and stderr `ERROR: upstream probe returned 429; recovered`, the Invoke-Lane statements between `K4-ANSWER-BEGIN/END` give `providerRefusal = $null`. Tests: `test_codex_*`, `test_invoke_lane_wiring_*` in `tools/coordination/test_coordination_guardrails.py`.
