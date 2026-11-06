---
target: RECEIPTS.md
kind: receipt
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-11-06 — Kernel K4: a codex lane that exits 0 with no answer is `ended-incomplete`, not `complete`

**Before:** `Get-LaneWorkEvidence` in `tools/coordination/lane-provider-refusal.ps1` set `workCompleted = $true` for every `-Engine codex` run with exit code 0 ("nothing more is observable"), so `Invoke-Lane.ps1` booked an empty, whitespace-only or otherwise silent codex answer as `state=complete`, `complete=true`. A review key that answered nothing then read as a finished key that gave no verdict (found by the sol r2 key on mlv-app PR #736, 000050b5 B2).
**Now:** for codex, exit 0 plus a non-blank answer (`IsNullOrWhiteSpace` false) is the positive completion evidence. An empty or whitespace-only answer returns `workCompleted=$false` with reason `codex-empty-answer`, which Invoke-Lane books as `state=ended-incomplete`, the state a claude `no-result-envelope` already gets. A non-zero exit keeps its `exit-code-N` reason first.
**Evidence for the rule (not taste):** 1121 real codex receipts with `state=complete` and `exitCode=0` in the lane run receipts all had a non-blank `.last.txt` and a non-blank `.out.txt`; the shortest trimmed answer was 518 characters. The rule would have rejected 0 of 1121. No stronger shape is used because real answers (reviews, recon notes, producer summaries) share no terminal marker.
**Non-goal, pinned by a test:** a non-blank answer that is unrelated to the prompt still counts as complete; telling that apart is the consumer's verdict parse (a key watcher), not the lane runner's job.

**Falsifier:** `Get-LaneWorkEvidence -Engine codex -Answer '' -ExitCode 0` (or an answer of only spaces, tabs or newlines) must return `workCompleted=False`, `reason=codex-empty-answer`; with a one-word answer it must return `workCompleted=True`. Tests: `test_codex_exit_0_*` in `tools/coordination/test_coordination_guardrails.py`.
