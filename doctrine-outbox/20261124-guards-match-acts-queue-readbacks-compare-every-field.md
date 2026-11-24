---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/guards-match-acts-queue-readbacks-compare-every-field
rule: (1) A command guard counts a delete, move or truncate verb only when its operand can reach the protected path (the same segment, a pipeline from one naming it, or a variable or location set from it); an operand it cannot resolve counts as the path; a verb inside a quoted search pattern is not counted. (2) A hub session changes queue state only through the queue tool's set command, or a queue line in a shape its parser reads, then reads the queue back: the row matches every field the request set, kind included, and no unrequested row appeared.
mechanism: a guard counted a delete verb anywhere in a command and denied every protected path it named, so a read-only search whose pattern listed a delete verb beside a protected path killed a lane round (ledger 2026-11-23T23:17:15Z). Two hub-filed fix cards used a QUEUED line the queue parser did not read as a priority, got the default, and the intake guard parked them at P5; one was re-queued P2 through the tool, the other parked as a duplicate (2026-11-24T02:22:15Z). A bold QUEUED line asking for kind factory (02:13:29Z) parsed as unclassified at P3, so a priority and state readback passed.
applies: mlv-app
check: `py -3 -m pytest tools/repo_hygiene/test_mlv_never_authorized.py -k "remove_item_recurse_fleet_runs or rm_rf_closeout_evidence" -q` runs the delete-verb arm of tools/hooks/mlv-never-authorized.py, and `pwsh -NoProfile -File Test-CardQueueKindPrefix.ps1`, run in the board's hub-autopilot directory beside Update-CardQueue.ps1, runs its kind parse on fixture ledgers; both pass today. Fix pending: HOOK-NA2-PROSE-VERB-FALSE-POSITIVE-1; its tests: a read-only search naming a delete verb and a protected path is allowed; a real delete of that path, one fed by a pipeline from a command naming it, and one through a variable holding the path stay denied.
supersedes: none
evidence: measured
