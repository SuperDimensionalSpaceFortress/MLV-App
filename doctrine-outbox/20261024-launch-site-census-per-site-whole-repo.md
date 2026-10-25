---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/launch-site-census-per-site-whole-repo-1
rule: a census of the launch sites a live-reference guard must see classifies SITES, not files, over every tracked *.ps1 / *.psm1, read from the language's own parser (Parser::ParseInput), never a hand-written tokenizer. Each base64 (-EncodedCommand) or detached (hidden Start-Process, WMI process create) launch is pinned per site with the reason the guard still sees it; a second site in a classified file, or a pinned site that moved or vanished, fails like a new file. A string that is executed but built at run time is an `unresolved` site that fails until classified, and a variable resolves to a literal only through assignments in the scope that reads it, made before the read; a qualified write counts in the scope it names, and any other binder (Set-Variable, the variable: drive, PSVariable, code run in that scope) blocks resolution. Bounds: PowerShell scripts only; tests and the guard are excluded; variable flow stays within one file; ProcessStartInfo launches are not a detached shape.
mechanism: 2026-10-24/25 reviews of PR #665 and #669 r1-r3. A file-level found-set let a second launch hide in a classified file; three Python tokenizer rounds each closed the named holes and each review found more; the AST read closed them, then a file-wide assignment map let a parameter shadow an unrelated outer literal (Dot B1), closed by scope-aware resolution in r4; r5 closed qualified writes, ordering and the other binders (Dot, Grok).
applies: mlv-app
check: in mlv-app, pytest tests/coordination/test_retire_lane_worktree.py -k launcher_census (136 cases; 31 were RED before r5).
supersedes: 20261024-worktree-guard-unknown-holder-and-encoded-driver (its census sentence said "every tracked launch site" over two roots)
evidence: measured
