---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/every-named-successor-becomes-a-card
rule: every follow-up a lane names, in its summary or in any section of a report it writes, becomes a card, whatever the lane's card kind. A conditional one ("if X holds, try Y") is filed as a held card that carries its trigger, never left as prose. A named follow-up that no card or reconciler row picks up raises a stuck-row alarm naming the file and line. A harvester that reads only a summary's first line, or only one card kind, does not meet this rule. It applies to every board whose lanes write free-text summaries or reports.
mechanism: successor filing reads only line 1 of a summary, and only for bench-marked or bench-kind runs. A frame-trace lane (product-kind; report: PLAYBACK-FRAME-TRACE-1457-1 r1, file report-pft1.md) listed four successors in a report section. The hub queued two; the third was conditional (after a cache card, re-measure; if reconstruction still limits, add a second worker) and was filed held only a day later, when the owner asked why multiple workers were never tried. So a first-line harvester misses it, as would a section-aware one limited to bench-marked runs.
check: give your successor harvester one lane report whose "## Successors" section holds two entries: a conditional one that has no card id of its own ("after CARD-A merges, re-measure X; if X still limits, add a worker") and a plain named one ("file CARD-B"). FAIL unless the harvester files exactly two cards from that report: CARD-B, and one held card with a new id of its own (not CARD-A's) whose trigger is CARD-A merging and whose text holds the conditional entry's words after its trigger verbatim: "re-measure X; if X still limits, add a worker".
supersedes: none
evidence: measured
