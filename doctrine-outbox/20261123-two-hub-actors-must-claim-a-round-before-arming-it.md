---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/two-hub-actors-must-claim-a-round-before-arming-it
rule: when a board has two actors that can arm a card's next fix round (an interactive hub chat and an unattended scheduled hub tick), an ownership convention alone does not prevent a double arm: both read state, decide and write at different moments, so they race. Every round-arming path calls ONE shared claim step under the same lock that guards the board's pending-action list. It refuses when that list or the run-receipt area already holds a live arm (queued, launched or finished) of the same card and round, and it re-checks just before writing the run folder. A failed arm, or a folder holding only a staged prompt, does not block a deliberate retry under a new action id. Until the claim step exists, the hub chat does not arm a round while an unattended tick is running. To hand a contested card to the tick it writes an explicit hub ruling that transfers the card, because the tick refuses a bare note under its one-owner rule. Applies to every project whose hub has both actors.
mechanism: both review keys on a patch-only merge-gate card returned changes requested. The hub chat armed the fix round under the rule "whoever ran the keys arms the next round". About 40 s later the unattended tick, running at that moment on state read before the hub chat wrote, composed a second fix round for the same card and queued it. The first round fired first and stopped within about a minute on a correct guard deny; the second waited for a slot and launched after it. Only a later hub ruling, written after the first stopped, named the second the continuation. Nothing in the design guaranteed that; the first round's reported spend produced no patch, and the extra cost was not measured. The hub chat's duplicate check ran before the tick wrote, so it passed.
check: none yet (the shared claim helper is its own board card)
supersedes: none
evidence: measured
