---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/kill-switch-checked-at-the-launch-sink-not-only-the-scheduler
rule: a host kill switch (a pause file) is read by the board launch scripts a dispatch passes through, not only by the scheduler gate, and again after any wait longer than a few seconds (an admission wait). A direct call to Invoke-Lane.ps1 or Invoke-Workstream.ps1 never reads it. A live switch refuses the launch and reserves nothing, except for the exempt hub-tick dispatcher (card HUB-TICK), which proceeds and dispatches nothing. An unreadable switch file (read error, a directory, no header, no parsable until) is a refusal on the same terms; an expired one is not. The switch path can be overridden (MLV_FLEETQUIET_PAUSED_PATH, temp dir only); an absent override reads as no pause.
mechanism: 2026-11-15, a hub tick in flight dispatched a CPU-heavy lane 11 s into owner thermal trial fq-20261115T175518Z, because only the scheduled gate read the file. The fix checks in dispatch-lane.ps1 (and Start-HiddenDetached.ps1, which classifies by program tokens: the program, any argument ending in a script or exe name with no ' -' flag in it, a -File script; so a heavy command inside a -Command string is admitted unless the string ends in one) and after the admission wait, and refuses with FLEETQUIET_REFUSED card=<c> pause=<id> until=<utc> (exit 3); a refusal is re-armed, not counted as a failure, within finite limits: 288 pause re-arms, then the ordinary capacity re-arms (144); past both, a refusal costs an ordinary attempt (3, then failed). The refusal echoes the pause id into the action log; an id holding any text the autopilot matches there is read as that marker: the row ends done, gave-up or failed, and nothing re-arms.
applies: mlv-app
check: Guard: Test-DispatchFleetQuiet.ps1 in the MLV-App board's hub-tools directory (a board tool, not in this repo; fixture pauses live in a temp dir, never the real file)
supersedes: none
evidence: measured
