---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/a-name-prefix-kind-label-defaulting-to-product
rule: label a work item's kind from the paths its change touches, never from its name prefix; a name no prefix rule knows is "unclassified", never "product", and a product share is counted over merged diffs.
mechanism: MLV's card-queue updater (Update-CardQueue.ps1, lines 68-73 at the 2026-10-09T22:43:13Z hub ruling) returned kind product for every card name whose prefix its table did not know, so any share read off that kind column counted unknown-prefix cards as product work. The path-based 7-day share from tools/coordination/Test-ProductRatioGuard.ps1 was 0.26 against a 0.5 threshold (8 of 67 first-parent merges in 72 h touched src/ or platform/), and that guard had no live caller, so nothing set the two readings side by side.
check: `git log --no-merges --since=7.days --format=%h -- src platform | wc -l` over the same window's non-merge count, shown beside the count of queue rows with kind product; a queued fixture card with an unknown prefix must read unclassified
supersedes: none
evidence: measured
- **Related:** adobe-ingester/measure-product-against-control measures by paths; this card names the default-to-product label that bypasses such a measure.
- **Guard (MLV):** pending CARD-QUEUE-TRUTH-1 (fixture test, in flight).
