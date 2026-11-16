---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/kill-switch-checked-at-the-launch-sink-not-only-the-scheduler
rule: a host kill switch (a pause file) is read by the board launch scripts a dispatch passes through, not only by the scheduler gate, and again after any wait longer than a few seconds (an admission wait). A direct call to Invoke-Lane.ps1 or Invoke-Workstream.ps1 never reads it. A live switch refuses the launch and reserves nothing, except for the exempt hub-tick dispatcher (card HUB-TICK), which proceeds and dispatches nothing. An unreadable switch file (read error, a directory, no header, no parsable until) is a refusal on the same terms; an expired one is not.
mechanism: 2026-11-15, a hub tick already in flight dispatched a CPU-heavy lane 11 s into owner thermal trial fq-20261115T175518Z, because only the scheduled gate read the pause file and a running launcher never passes through it. The fix checks in dispatch-lane.ps1 (and Start-HiddenDetached.ps1 for heavy detached launches) and after the admission wait, and refuses with FLEETQUIET_REFUSED card=<c> pause=<id> until=<utc> (exit 3); a refusal is re-armed, not counted as a failure, but only within finite limits: 288 pause re-arms, then the ordinary capacity re-arms (144); past both, a refusal costs an ordinary attempt (3, then failed). A pause id containing a log marker the autopilot matches (ACTION-OK, ACTION-GAVE-UP) is read as that marker: the row ends done or gave-up, nothing re-arms. Related but distinct: RULINGS.md:1401 ("A kill switch that is a FILE") says the stop is a file, and TRAPS.md:2945-2951 says to search for a paused component's decision. Neither says where the file is read.
applies: mlv-app
check: Guard: Test-DispatchFleetQuiet.ps1 in the MLV-App board's hub-tools directory (a board tool, not tracked in this repo; PowerShell 7 on Windows; each fixture pause lives in a temp dir, the real pause file is never created)
supersedes: none
evidence: measured
