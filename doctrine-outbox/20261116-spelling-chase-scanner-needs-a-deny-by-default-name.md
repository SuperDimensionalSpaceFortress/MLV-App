---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/spelling-chase-scanner-needs-a-deny-by-default-name
rule: when a refusal must hold against a construct a language lets you spell many ways (a WMI process create), do not write a pattern scanner that lists the spellings. Guard the NAME every spelling contains, deny by default: refuse any tracked script whose text names the class (code, strings and comments, after undoing backtick escapes, quote splicing and UTF-16 NULs), accept a file only through a reviewed allowlist row `<path> | <reason>` with a closed reason vocabulary, and fail a stale row. Keep an AST rule only as a second look at allowlisted files. Say in the test header what is NOT covered (a name built from pieces, run-time evaluation).
mechanism: the WMI create scanner on PR 815 read PowerShell's AST and drew 90 major findings over 30 adversary passes and 12 rounds, with no falling trend: each rule leaked to a new spelling (a verb from a branch expression, diagnostic-stream text read as an operand, provider rebinding). The class name is the one token they share, so denying it needs no parser. Price: over-refusal (a Win32_Processor read needs a row), reviewed once per row.
applies: mlv-app
check: py -3 -m pytest tests/coordination/test_wmi_process_allowlist.py -q covers the real tree against tools/coordination/wmi-process-allowlist.txt and the RED fixtures (an unlisted file, a stale row, a bad reason word); tests/coordination/test_doctrine_outbox.py -k test_every_committed_outbox_item_passes_the_card_validator covers this card's shape. No must-refuse sweep ships: no scanner was narrowed.
supersedes: none
evidence: measured
