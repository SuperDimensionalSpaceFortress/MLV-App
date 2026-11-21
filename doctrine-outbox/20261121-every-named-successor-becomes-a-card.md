---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/every-named-successor-becomes-a-card
rule: every follow-up a lane names, in its summary or in any section of a report it writes, becomes a card, whatever the lane's card kind. A conditional one ("if X holds, try Y") is filed as a held card that carries its trigger, never left as prose. A named follow-up that no card or reconciler row picks up raises a stuck-row alarm naming the file and line. A harvester that reads only a summary's first line, or only one card kind, does not meet this rule. It applies to every board whose lanes write free-text summaries or reports.
mechanism: the board's automatic successor filing reads only line 1 of a summary, and only for bench-kind runs. A frame-trace lane (a product-kind card, so outside that filing) listed four successors in a report section. The hub queued two at once. The third was conditional (re-measure after a cache card; if reconstruction still limits, the next card is a second reconstruction worker) and was filed as a held card only about a day later, when the owner asked why multiple workers had never been tried. So a first-line harvester misses it, and so would a section-aware one limited to bench runs.
check: none yet. The board's card SUCCESSOR-SECTION-HARVEST-1 is queued; its RED test is that report, whose conditional successor names a prerequisite card but not its own card id.
supersedes: none
evidence: measured
