---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/deterministic-launcher-refusal-typed-terminal-marker
rule: a launcher refusal that a retry cannot fix (no prompt file, dirty worktree, hook drift) prints a typed marker with a REMEDY, and the retry classifier makes it terminal on attempt 1. Key on the marker, never the exit code: codes collide across refusal kinds. At add time, refuse an immediate row whose prompt is missing; admit a deferred one; only warn on a dirty tree or hook drift, which can change before the row fires.
mechanism: on 2026-11-14/15 three fix rounds refused at launch for fixed reasons (missing prompt, dirty tree, a PR editing its own hook). Each re-fired as a generic failure, up to 3 times at about 15 min of a capped slot each. The launcher now prints REFUSED_FIXROUND_NO_PROMPT, REFUSED_DIRTY_WORKTREE or REFUSED_HOOK_DRIFT; the classifier ends the row and quotes the remedy.
applies: mlv-app
check: board-local, not pushed, PowerShell 7 on Windows. Guards (SHA-256): hub-autopilot\dispatch-fix-round.ps1 d534c4edb78153d0d2e78e06b15a335fb688eb1a94612ad09d4ee9228ebd5fc2; hub-autopilot.ps1 c6357f90baac46247819e44a66441e037f8413da7167a1a86a694c0dbbb469b8; Add-PendingAction.ps1 c5e127ff3402872faac16943991d2a60aab1639e3911b75e0ffa9c170fbf3ee5. 2026-11-15: Test-AddPendingActionBinding.ps1 91/91 green; Test-PendingActionRetry.ps1 7 new rows pass, 3 fail on the old classifier; its other 17 rows lack fixture logs (card TEST-PENDING-ACTION-RETRY-FIXTURES-LOST-1). No tracked test.
supersedes: none
evidence: measured
