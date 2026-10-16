---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/launcher-scoring-per-sentence-exempts-an-unrouted-second
rule: a lint that exempts a launcher because it is routed to a remote venue scores every launcher occurrence on its own, and bounds pattern gaps at clause separators (`;`, `then`, `or`, and the next launcher token); a lazy gap alone still crosses `;`.
mechanism: scoring per sentence lets the first launcher's venue routing exempt a second, unrouted launcher in the same sentence. Chief of Staff follow-up on PR 448 (comment 6216180502) reproduced a pass for a routed launcher, a semicolon, then a plain second launcher. The per-launcher fix on PR 465 closed that shape, and the Chief of Staff shared blocker on its first head (comment 6218718347) found the gap still open when the first launcher did not match on its own: the lazy gap reached the second launcher and the merged span took the first clause's routing. Revision r1 of PR 465 carried 98 test rows, all red-green. It was the seventh round in which an outside probe found an exemption bypass.
check: after PR 465 merges, `python -m pytest tools/repo_hygiene/test_hub_vm_no_local_playback.py -q` passes from the merge commit and its rows include one second-launcher case per separator. Until then PR 465 head 75cb00e1 is the cited head.
supersedes: none
evidence: measured
- **Guard:** one RED row per separator and per launcher token in the lint's must-refuse corpus, plus a row where the venue text belongs to the second launcher
