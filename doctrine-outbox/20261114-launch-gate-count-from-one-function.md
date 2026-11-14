---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/launch-gate-count-from-one-function
rule: a count that gates a launch comes from the one gate function that owns the exemptions, never from a second hand-written filter over the process census. A consumer calls that function (in a bounded child if its script-scope variables would leak), falls back to the stricter reading when the call fails, and logs a token naming each exempt lane.
mechanism: two consumers of the producer cap each re-derived "live producers" from the census and counted the exempt non-building rider as a holder: a slot-free counter on 2026-11-13, and the queue drain's fill decision on 2026-11-14. Both reported at cap while the cap gate itself would have admitted the launch, so a free slot sat unused. The exemption lived only in the gate, so every naive copy of the count was wrong the same way.
applies: mlv-app
check: board-local, not pushed, PowerShell 7 on Windows only: the offline guard hub-autopilot/Test-DrainFillCountRiderExempt.ps1 (sha256 a475d957385545417bb318c5e4a63c209f9e727bb309203b6422ca64f2d982ac at the green run) lifts the drain's real functions by AST and ran 15 of 15 green on 2026-11-14 (red on the pre-change drain); live, the fill-decision log line carries rider-exempt with the lane name. The tracked tree has no test for this clause.
supersedes: none
evidence: measured
