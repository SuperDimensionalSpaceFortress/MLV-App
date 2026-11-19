---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/command-text-guards-must-tokenize-not-scan-the-line
rule: a pre-tool guard that denies a shell act by regex matches the act's tokens (the executable and its verb, a real redirect operator), not any word pair or character pair anywhere on the line. Authors put prose into files with the file-writing tool, which such guards do not scan as commands; they never reshape command text to get past a guard.
mechanism: one hub session hit two false denies. A credential-verb arm matched a hidden-state folder name that contains the CLI's name plus the word "auth" later in the same line of prose, and a write-root arm read a placeholder such as `<p>/<t>` (a greater-than sign followed by a slash) as a redirect to the filesystem root. Neither command could perform the denied act. Price: a fail-closed guard trades a visible false deny for a silent allow; tokenizing keeps the fail-closed direction while cutting the false-deny class.
applies: any project with command-text guards
check: Guard: none yet (the hook change is its own HIGH-RISK card)
supersedes: none
evidence: measured
