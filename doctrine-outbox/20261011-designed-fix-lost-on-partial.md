---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/designed-fix-lost-on-partial
rule: a round that ends PARTIAL names its successor card in the same tick that reads it; a card whose design names fix rows codes them in the next round instead of re-measuring; a queued fix idle past 72 h, or one whose blocker has merged, is re-armed or closed by a ruling; a standing sweep lists every designed or partly built card with no merged PR and nothing in flight.
mechanism: a playback stall fix was designed with named fix rows, then spent four measure-only rounds and never coded it. The work kept stopping on infrastructure rather than on the code: a guard refused the design write, a provider quota cut the first round, a producer died around a host restart, and a worktree was removed under a live chain. Nothing re-armed it, and the defect stayed in the product for two days until the owner saw it. A first sweep the same day listed 58 stalled cards out of 341, in several leak shapes: re-measure instead of code, PARTIAL on infrastructure, queued and never re-read, and unblocked by a merge but never re-armed.
check: the sweep prints one STALLED line per designed or partly built card with no merged or open PR, no live lane and no closing ruling whose newest activity is older than the stale threshold (default 24 h); younger cards count as in flight and are not printed; each tick triages the oldest two
supersedes: none
evidence: measured
