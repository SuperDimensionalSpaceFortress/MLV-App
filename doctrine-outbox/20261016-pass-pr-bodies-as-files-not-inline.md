---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/pass-pr-bodies-as-files-not-inline
rule: write a PR body or comment to a file with the file-write tool and pass it with `--body-file`; never inline `--body` text, and search with a structured search tool rather than a shell grep when the pattern holds redirect characters.
mechanism: command-line guards scan the whole command string, so inline body text containing backticks, `>>>` or `<<<` is read as a redirect or write path and the command is denied. Lane reports on PR 464 (PR create denied) and PR 457 (a shell grep with such a pattern denied) show both shapes; both completed once the body came from a file and the search ran through the structured tool. Guards differ by project, so the general rule is to keep free text out of command lines.
check: none (reported only; the denials are in private lane reports). Public part: gh pr view 464 --repo SuperDimensionalSpaceFortress/MLV-App --json body --jq .body shows a body that was created from a file.
supersedes: none
evidence: reported
- **Guard:** the lane prompt template states "gh pr create --body-file <path>" and forbids inline --body
