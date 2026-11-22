---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/every-named-successor-becomes-a-card
rule: every follow-up a lane names, in its summary or in any section of a report it writes, becomes a card, whatever the lane's card kind. A conditional one ("if X holds, try Y") is filed as a held card that carries its trigger, never left as prose. A named follow-up that no card or reconciler row picks up raises a stuck-row alarm naming the file and line. A harvester that reads only a summary's first line, or only one card kind, does not meet this rule. It applies to every board whose lanes write free-text summaries or reports.
mechanism: successor filing reads only line 1 of a summary, and only for bench-marked or bench-kind runs. A frame-trace lane (product-kind; report: PLAYBACK-FRAME-TRACE-1457-1 r1, file report-pft1.md) listed four successors in a report section. The hub queued two; the third was conditional (after a cache card, re-measure; if reconstruction still limits, add a second worker) and was filed held only a day later, when the owner asked why multiple workers were never tried. So a first-line harvester misses it, as would a section-aware one limited to bench-marked runs.
check: read every .md file a lane wrote; take each bullet under a heading named Successors, Follow-ups, Next steps or Next, to the end of that section, and any "only if" line elsewhere. A follow-up passes only through a live queue row (not closed as done, duplicate, superseded or cancelled) that names it and, when it is conditional, carries the report's own trigger condition; a closed row counts only through the live row it names as its replacement, which must pass too. Any other follow-up fails.
supersedes: none
evidence: measured
