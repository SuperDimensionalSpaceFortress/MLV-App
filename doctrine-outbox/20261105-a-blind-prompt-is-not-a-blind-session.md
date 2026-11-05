---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/a-blind-prompt-is-not-a-blind-session
rule: audit a blind or low-context Claude key from its session files, not its prompt. Run it with CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS=1, and scan hook output for the study's own name as well as the subject's.
mechanism: grepping the prompts found 0 hits while the Claude CLI's git snapshot put recent commit subjects (card, round, an earlier reader blocker) into session context. User-scope SessionStart hooks also printed commit subjects, live command lines and open branches, so all 13 Claude keys saw the study's name. Codex keys get no such hooks: the exposure is on one family and its effect on catch counts is unmeasured. With the variable set, the snapshot was absent from all 7 round-two Claude sessions on CLI 2.1.318; other versions were not checked.
applies: mlv-app
check: count `gitStatus` in each key's session file (0 expected), then search the same files for the study's name and for card names.
supersedes: none
evidence: measured
- **Guard:** none yet
