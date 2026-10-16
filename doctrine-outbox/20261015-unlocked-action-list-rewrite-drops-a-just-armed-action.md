---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/unlocked-action-list-rewrite-drops-a-just-armed-action
rule: every writer of the pending-action list takes one lock around a fresh read and its write, never held across slow work, and the arming command re-reads the list after writing and fails loudly if its own entry is missing.
mechanism: the autopilot beat reads the pending-action list, works, then writes its in-memory copy back through a temp file and a move (Write-Actions, no lock). The arming command does its own read-modify-write with no shared lock. Measured 2026-10-15: the lintscope-r11b action added at 19:25:52Z was missing from the file; an autopilot beat had rewritten it at 19:25:59Z from its own earlier read, 7 s after the add. A hub session re-added the same id at 19:36:23Z, verified present after 20 s. A temp-file-and-rename write is atomic against a torn file, not against a lost update. Sources: ledger 2026-10-15T19:37:23Z; project-memory self-heal-stuck-items-strategy-20261015.md S2 (7 s).
applies: mlv-app
check: from the MLV-App repo root, `python tools/coordination/stuck_queue_checks/action_list_lost_update_check.py --impl naive` exits 1 (RED) and `python tools/coordination/stuck_queue_checks/action_list_lost_update_check.py --impl locked` exits 0 (GREEN); any Python 3, stdlib only, pushed at SuperDimensionalSpaceFortress/MLV-App @c5739ef23f09 (PR 454). Each of 50 trials runs the arming command between the beat's read and its write; GREEN needs no armed entry lost and a forced loss to make the arming command exit non-zero. A board checks its own writers with --impl-module and the path of a file defining arm and beat_save; a writer that raises exits 2, never GREEN
supersedes: none
evidence: measured
- **Guard:** the fix is pending -- card STUCK-RECONCILER-1 item 1 (named mutex around every read+write; post-write re-read; lost-action re-add from the arming log); the check above already runs against any writer pair
