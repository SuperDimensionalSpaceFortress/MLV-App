---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/two-hub-actors-must-claim-a-round-before-arming-it
rule: when a board has two actors that can arm a card's next fix round (an interactive hub chat and an unattended scheduled hub tick), an ownership convention alone does not prevent a double arm, because both actors read state, decide, and write a round at different moments, so they race. Every round-arming path calls ONE shared claim step under the same lock that guards the board's pending-action list. The claim step refuses when that list or the run-receipt area already holds the same card and round, and it re-checks immediately before writing the run folder. Until that claim step exists, the hub chat does not arm a round while an unattended tick is running, and it hands a contested card to the tick in writing rather than arming again. It applies to every project whose hub has both an interactive hub chat and an unattended scheduled tick that can arm rounds.
mechanism: both review keys on a patch-only merge-gate card returned changes requested. The hub chat armed the fix round under the existing rule "whoever ran the keys arms the next round". About 40 s later the unattended tick, which was running at that moment and had read state before the hub chat wrote, composed a second fix round for the same card and queued it. The first round was dispatched at once; the second waited for a slot. The first then stopped within about a minute on a correct guard deny, and the second launched after it. Only a later hub ruling, written after the first had stopped, named the second round the card's continuation, so the double arm cost nothing this time; nothing in the design guaranteed that. The hub chat's own duplicate check ran before the tick wrote, so it passed.
check: none yet (the shared claim helper is its own board card)
supersedes: none
evidence: measured
