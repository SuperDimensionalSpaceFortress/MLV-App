---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/review-ownership-must-carry-the-next-round
rule: when one actor (here the interactive hub chat) runs a card's review, the unattended scheduler rules the card that actor's and stops touching it. So the same actor also arms the card's next fix round, unless a hub ruling explicitly transfers the card. By convention, a note that hands the next round to the scheduler without such a ruling is refused.
mechanism: the hub chat declared a review subject for a patch card and, on CHANGES_REQUESTED, wrote a note leaving round 3 to the scheduler. The scheduler's next run ruled the card owned by the hub chat (one owner per card) and left it alone, so the round had no owner for about 45 min, until the hub chat's next tick noticed the "round ended, no successor" row.
applies: any board with a one-owner-per-card rule and two hub actors
check: the stuck reconciler's round-ended-no-successor row surfaced it
supersedes: none
evidence: measured
