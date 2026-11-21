---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/every-named-successor-becomes-a-card
rule: every follow-up a lane names, in its summary or in any section of a report it writes, becomes a card, whatever the lane's card kind. A conditional one ("if X holds, try Y") is filed as a held card that carries its trigger, never left as prose. A named follow-up that no card or reconciler row picks up raises a stuck-row alarm naming the file and line. A harvester that reads only a summary's first line, or only one card kind, does not meet this rule. It applies to every board whose lanes write free-text summaries or reports.
mechanism: the board's automatic successor filing reads only line 1 of a summary, and only for runs that carry a bench marker or sit on a bench-kind card. A frame-trace lane (a product-kind card with no marker, so outside that filing; report: PLAYBACK-FRAME-TRACE-1457-1 r1, file report-pft1.md, kept on the originating board) listed four successors in a report section. The hub queued two at once. The third was conditional (re-measure after a cache card; if reconstruction still limits, the next card is a second reconstruction worker) and was filed as a held card only about a day later, when the owner asked why multiple workers had never been tried. So a first-line harvester misses it, and so would a section-aware one limited to bench-marked runs.
check: grep -rniE -A15 "^#+ .*(successor|follow-?up|next (card|step))" <your lane run dirs> --include=summary.md --include="report*.md" lists candidate follow-ups from your last runs (a first pass: it misses sections without those words and its context can run into the next section, so read each hit); each follow-up you find with no card-queue row (named, or held with its trigger) fails. The originating board's harvester card (SUCCESSOR-SECTION-HARVEST-1) automates the listing; its exact coverage is defined by that card.
supersedes: none
evidence: measured
