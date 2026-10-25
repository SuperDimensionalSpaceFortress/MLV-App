---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/launch-site-census-per-site-whole-repo-1
rule: a census of the launch sites a live-reference guard must see classifies SITES, not files, over every tracked *.ps1 / *.psm1, read from the language's own parser (Parser::ParseInput), never a hand-written tokenizer. Each base64 (-EncodedCommand) or detached (hidden Start-Process, WMI process create) launch is pinned per site with the reason the guard still sees it; a second site in a classified file, or a pinned site that moved or vanished, fails like a new file. A string executed but built at run time is an `unresolved` site that fails until classified. A variable resolves to a literal by allowlist only (a same-scope literal before the read): any other binder blocks it, a dynamic name (non-constant member, variable name, splat) blocks every name, and a parameter-metadata walk proves no *Variable* parameter escapes. Bounds: PowerShell scripts only; tests and the guard are excluded; variable flow stays within one file; ProcessStartInfo launches are not a detached shape.
mechanism: 2026-10-24/25 reviews of PR #665 and #669 r1-r3. A file-level found-set let a second launch hide in a classified file; three Python tokenizer rounds each closed the named holes and each review found more; the AST read closed them; r4-r6 reviews (Dot, Grok) kept finding binders (a shadowing param, qualified writes, Tee-Object -Variable, dynamic members), so r7 replaced the per-writer chase with the allowlist and the metadata walk.
applies: mlv-app
check: in mlv-app, pytest tests/coordination/test_retire_lane_worktree.py -k launcher_census (147 cases; 4 RED before r7) and python -m unittest tools.repo_hygiene.test_launch_site_census_binders (131 binder spellings; r5 missed 112).
supersedes: 20261024-worktree-guard-unknown-holder-and-encoded-driver (its census sentence said "every tracked launch site" over two roots)
evidence: measured
