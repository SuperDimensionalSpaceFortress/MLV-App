---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/unlocked-action-list-rewrite-drops-a-just-armed-action
rule: every writer of the pending-action list takes one lock around its read and its write, and the arming command re-reads the list after writing and fails loudly if its own entry is missing.
mechanism: the scheduled autopilot beat reads the action list, works for some seconds, then writes its in-memory copy back through a temp file and a rename. The arming command does the same read-modify-write with no shared lock. Measured 2026-10-15: an action added at 19:25:52Z printed "ADD" and exited 0; the beat's rewrite at 19:25:59Z, built from a read taken before the add, erased it. The fix round it armed never fired, and nothing reported it: the arming command had already succeeded, and the beat never knew the entry existed. A temp-file-and-rename write is atomic against a torn file, not against a lost update.
applies: mlv-app
check: run two writers in a loop against a fixture list, one appending a uniquely named entry and one rewriting the list from an earlier read, 50 iterations each. Any appended entry missing at the end is the trap; with the lock and the post-write re-read, zero are lost and a forced loss makes the arming command exit non-zero.
supersedes: none
evidence: measured
- **Guard:** pending -- card STUCK-RECONCILER-1 item 1 (named mutex around every read+write; post-write re-read; lost-action re-add from the arming log)
