---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/two-hub-actors-must-claim-a-round-before-arming-it
rule: when a board has two actors that can arm a card's next fix round (an interactive hub chat and an unattended tick), an ownership convention alone cannot prevent a double arm: both read state, decide and write at different moments. Every round-arming path calls ONE shared claim step under the lock that guards the board's pending-action list. It refuses a second arm of the same card and the exact same round (r2 never matches r20) in that list or the run-receipt area, unless the caller names the prior action as superseded in the same locked write that adds the new row, so a retry is an explicit, recorded act. Only a still-pending row can be superseded: the launcher flips pending to fired by compare-and-set under that lock and skips a superseded row, so a fired row is never superseded. The claim step re-checks before writing the run folder. Until then the hub chat does not arm a round while a tick is running. To hand a card to the tick it writes an explicit hub ruling that transfers it; the tick refuses a bare note (one-owner rule). Applies to any hub with both actors.
mechanism: both review keys on a patch-only merge-gate card requested changes. The hub chat armed the fix round ("whoever ran the keys arms the next round"). About 40 s later the unattended tick, running on state read before the hub chat wrote, composed and queued a second fix round for the same card. The first round fired first, hit a correct guard deny about a minute in and ended four minutes after launch; the second waited for a slot, then launched. Only a later hub ruling, after the first ended, named the second the continuation. Nothing guaranteed that; round one's reported spend produced no patch; extra cost not measured. The chat's duplicate check ran before the tick wrote.
check: none yet (the shared claim helper is its own board card)
supersedes: none
evidence: measured
