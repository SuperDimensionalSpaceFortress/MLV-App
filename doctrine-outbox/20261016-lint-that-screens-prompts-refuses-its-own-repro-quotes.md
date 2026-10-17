---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/lint-that-screens-prompts-refuses-its-own-repro-quotes
rule: when a safety lint also screens agent prompts, a dispatch prompt points at the source comment that holds the bypass repros (by comment id) and never quotes the repro lines.
mechanism: the lint refuses any prompt whose text matches the shapes it exists to block, and a bypass repro is by construction such a shape. On PR 465 the dispatch that quoted the repro lines was refused; it succeeded once the prompt referenced the Chief of Staff comment (6216180502, which holds the repros) and described the fix in words. The refusal is correct, not a lint bug, so the fix belongs in the prompt writer, not in an exemption. The refused dispatch is recorded only in private run state.
check: none (reported only; the refusal is in private run state). Public part: gh api repos/SuperDimensionalSpaceFortress/MLV-App/issues/comments/6216180502 --jq .body shows the repro lines a prompt should cite instead of copy.
supersedes: none
evidence: reported
- **Guard:** the prompt template for lint-fix cards carries a line "see comment <id>", and a test shows a prompt with an inline repro is refused
