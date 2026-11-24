---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/guards-match-acts-queue-readbacks-compare-every-field
rule: (1) A command guard counts a delete, move or truncate verb only when its operand can reach the protected path (the same segment, a pipeline from one naming it, or a variable or location set from it); an operand it cannot resolve counts as the path; a verb in a quoted search pattern is not counted. (2) A hub session changes queue state only through the queue tool's set command, or a queue line in a shape its parser reads, one writer at a time (it republishes a whole snapshot, undoing an overlapping write unseen), then reads the queue back: the row matches every field the request set, kind included, and the set of row ids and every other row match the copy taken before (none appeared, vanished or changed).
mechanism: a guard counted a delete verb anywhere in a command, so a read-only search listing a delete verb beside a protected path was denied and killed a lane round (2026-11-23T23:17:15Z). Two fix cards used a QUEUED line the parser did not read as a priority; the intake guard parked them at P5, one re-queued P2 through the tool, the other as a duplicate (2026-11-24T02:22:15Z). A bold QUEUED line asking for kind factory (02:13:29Z) parsed as unclassified at P3, so a priority and state readback passed.
applies: mlv-app
check: `py -3 -m pytest tools/repo_hygiene/test_mlv_never_authorized.py -k "remove_item_recurse_fleet_runs or rm_rf_closeout_evidence" -q` runs the guard's delete-verb arm, and `pwsh -NoProfile -File Test-CardQueueKindPrefix.ps1`, run in the board's hub-autopilot directory, runs Update-CardQueue.ps1's kind parse on fixtures; both pass today. Fix pending: HOOK-NA2-PROSE-VERB-FALSE-POSITIVE-1, tests: a read-only search naming a delete verb and the path is allowed; a real delete of that path, one fed by a pipeline from a command naming it, and one through a variable holding it stay denied.
supersedes: none
evidence: measured
