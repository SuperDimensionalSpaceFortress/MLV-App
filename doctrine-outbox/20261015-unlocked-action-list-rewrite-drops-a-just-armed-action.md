---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/unlocked-action-list-rewrite-drops-a-just-armed-action
rule: every writer of the pending-action list takes one lock around a fresh read and its write, never held across slow work, and the arming command re-reads the list after writing and fails loudly if its own entry is missing.
mechanism: the scheduled autopilot beat reads the action list, works for some seconds, then writes its in-memory copy back through a temp file and a rename. The arming command does the same read-modify-write with no shared lock. Measured 2026-10-15: an action added at 19:25:52Z printed "ADD" and exited 0; the beat's rewrite at 19:25:59Z, built from a read taken before the add, erased it. The erased entry never fired and nothing reported it: the arming command had already succeeded, and the beat never knew the entry existed. A hub session found it missing, re-added the same id at 19:36:23Z, and it fired at 19:36:55Z, 11 minutes after the first add. A temp-file-and-rename write is atomic against a torn file, not against a lost update.
applies: mlv-app
check: py -3 tools/coordination/stuck_queue_checks/action_list_lost_update_check.py --impl-module <the board's writers> (pushed: SuperDimensionalSpaceFortress/MLV-App @a02af93c0629); each of 50 trials runs the arming command between the beat's read and its write and exits 0 only if no armed entry is lost and a forced loss makes the arming command exit non-zero; --impl naive loses 50/50 (RED), --impl locked loses 0/50 (GREEN)
supersedes: none
evidence: measured
- **Guard:** the fix is pending -- card STUCK-RECONCILER-1 item 1 (named mutex around every read+write; post-write re-read; lost-action re-add from the arming log); the check above already runs against any writer pair
