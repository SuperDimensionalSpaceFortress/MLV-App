---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/every-named-successor-becomes-a-card
rule: every follow-up a lane names, in its summary or in any section of a report it writes, becomes a card, whatever the lane's card kind. A conditional one ("if X holds, try Y") is filed as a held card that carries its trigger, never left as prose. A named follow-up that no card or reconciler row picks up raises a stuck-row alarm naming the file and line. A harvester that reads only a summary's first line, or only one card kind, does not meet this rule. It applies to every board whose lanes write free-text summaries or reports.
mechanism: successor filing reads only line 1 of a summary, and only for bench-marked or bench-kind runs. A frame-trace lane (product-kind; report: PLAYBACK-FRAME-TRACE-1457-1 r1, file report-pft1.md) listed four successors in a report section. The hub queued two; the third was conditional (after a cache card, re-measure; if reconstruction still limits, add a second worker) and was filed held only a day later, when the owner asked why multiple workers were never tried. So a first-line harvester misses it, as would a section-aware one limited to bench-marked runs.
check: read every text file a lane wrote (an unreadable one fails). Take each entry (bullet or line) under any heading that names Successors, Follow-ups, Next steps or Next (numbering or a suffix is fine) to the end of that section, plus any line elsewhere that names a card to file or try, a successor= field included. A follow-up passes only through a live row (any state not closed) that names it; if it carries an "if" clause, the row must carry that trigger and be held until it fires. A row closed as done passes; a row closed any other way (duplicate, superseded, cancelled, refused) counts only by following its named replacements to a live row that passes. Any other follow-up fails.
supersedes: none
evidence: measured
