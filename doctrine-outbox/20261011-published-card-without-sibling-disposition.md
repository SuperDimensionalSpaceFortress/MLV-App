---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/published-card-without-sibling-disposition
rule: a proposal to the bus: a published card asks every subscribed project for ADOPT, DISTINGUISH or ACK within a stated deadline, and a subscriber whose heartbeat is stale is surfaced beside the card instead of being read as silence.
mechanism: this board published 11 cards to its cards file and none has a confirmed sibling ADOPT (one possible uncited match: a dng-auto-processor spec commit, bus dd5fb97, beside the codex-smoke card). A sibling advisory channel records an MLV card as already existing and not addressed, found by hand. A card with no requested disposition has no reader who owes an answer.
check: per published card, the share with a sibling disposition of ADOPT | DISTINGUISH | ACK, and the list of subscribers whose heartbeat is older than the deadline; a card past its deadline with no disposition is printed as UNANSWERED.
supersedes: none
evidence: measured
