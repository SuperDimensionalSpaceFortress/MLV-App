---
target: RECEIPTS.md
kind: receipt
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-09-29 — the doctrine outbox's first live cycle: independent review caught two false instructions in a backfill item before anything reached the bus

**Measured event:** MLV-App adopted the doctrine outbox (a tool, a CI trailer check and 14 backfilled trap items staged as files) in one pull request. Before it merged, a read-only Codex reviewer at the PR head returned `CHANGES_REQUESTED`. It reported that three earlier design blockers and two drain races from its pre-review looked fixed, all 14 backfill items passed `validate` and the Law-4 screen, and none repeated an exact heading already at the bus tip. It then found two blockers in one backfill item, both of which validation cannot see:

1. **A falsifier command that did not do what it claimed.** The item told readers to run `xxd <file> | head -c 3` and look for the three BOM bytes. The reviewer piped a known BOM file through it and got `000`, the dump offset, not the bytes. The cited source said to read the first `xxd` line.
2. **A symptom contradicted by its own source.** The item said there was "no visible warning beyond a log line". The cited source recorded that the app showed an error popup and reset the config on dismiss.

The hub fixed both in one follow-up commit (falsifier changed to `xxd <file> | head -1`, checked on a BOM file and a clean file; symptom rewritten to match the source), and the PR merged with the fix as its second parent. Neither false line had been published: the drain had not run.

**Why it matters:** a doctrine entry is an instruction that other projects will run. The screen and the schema check prove shape and privacy, not truth. A wrong falsifier does more damage than a missing one, because a reader who runs it and sees no BOM concludes there is none. The catch came from a reviewer that ran the command and compared each claim with the source it cites, not from tooling.

**Rule:** no doctrine item is drained on validation alone. Every item, including backfills of older facts, gets an independent read that (a) executes each falsifier or test command once on a known-positive and a known-negative input, and (b) checks each symptom and number against the source it cites. Keep the review step before the drain step, so a false line is fixed in a file, not amended on the bus.

**Re-run:** for any pending item, run its falsifier on a positive and a negative sample, then diff each measured claim against its cited source.
