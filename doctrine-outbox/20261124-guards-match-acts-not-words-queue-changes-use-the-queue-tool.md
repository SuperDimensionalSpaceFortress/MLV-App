---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/guards-match-acts-not-words-queue-changes-use-the-queue-tool
rule: (1) A command guard counts a dangerous verb (delete, move, truncate) only in the command segment that names the protected path, and never inside a quoted search pattern. (2) Until it does, a lane prompt that reads protected files says: use the read and search tools, never a shell command. (3) A card that loses three rounds in a row to hook shapes is parked with its staged design and its fix goes to the hook's own card; it is not dispatched a fourth time. (4) A hub actor changes queue state only through the queue tool's set command, or a queue line in a shape its parser reads.
mechanism: a guard added the delete act when a delete verb appeared anywhere in a command, then denied every protected path the command named, so a read-only search whose pattern listed a delete verb beside a protected path killed a lane round (ledger 2026-11-23T23:17:15Z); a second round died on the same hook within the hour (2026-11-24T00:24:31Z). Two hub-filed fix cards used a bold QUEUED line the queue parser does not read, got the default priority, and the intake guard parked them at P5 until a ruling re-queued them through the tool (2026-11-24T02:22:15Z). Fix pending: HOOK-NA2-PROSE-VERB-FALSE-POSITIVE-1.
applies: any board with command-text guards and a hub that edits its own queue.
check: none yet. Fix pending: HOOK-NA2-PROSE-VERB-FALSE-POSITIVE-1; its tests: a read-only search naming a delete verb and a protected path is allowed, and a real delete of that path stays denied.
supersedes: none
evidence: measured
