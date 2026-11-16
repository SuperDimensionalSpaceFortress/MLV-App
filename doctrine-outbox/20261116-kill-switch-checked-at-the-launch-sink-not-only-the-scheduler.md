---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/kill-switch-checked-at-the-launch-sink-not-only-the-scheduler
rule: a host kill switch (a pause file) is read by the board launch scripts a dispatch passes through, not only by the scheduler gate, and again after any long wait (the admission wait). A direct call to Invoke-Lane.ps1 or Invoke-Workstream.ps1 never reads it. A live switch refuses the launch and releases any reservation taken (best effort), except for the exempt HUB-TICK dispatcher, which proceeds and dispatches nothing. An unreadable switch file (read error, a directory, no header or until) is a refusal on the same terms; an expired one is not. The switch path can be overridden (MLV_FLEETQUIET_PAUSED_PATH, temp dir only); an override naming a missing file reads as no pause.
mechanism: 2026-11-15: a hub tick in flight dispatched a CPU-heavy lane 11 s into owner thermal trial fq-20261115T175518Z; only the scheduled gate read the file. The fix checks in dispatch-lane.ps1 (and Start-HiddenDetached.ps1, whose launch classifier is a regex heuristic over program-like tokens, neither exact nor complete: quoted text can be refused, a quoted -File switch or a heavy command inside a -Command string can be admitted; FLEETQUIET-SINK-BYPASS-SHAPES-1) and after the admission wait, and refuses with FLEETQUIET_REFUSED card=<c> pause=<id> until=<utc> (exit 3); a refusal is re-armed, not counted as a failure, within finite limits: 288 pause re-arms, then 144 capacity re-arms; past both, a refusal costs an ordinary attempt (3, then failed). The refusal echoes the pause id into the action log; an id holding a success, gave-up or terminal marker is read as that marker: the row ends done, gave-up or failed, and nothing re-arms.
applies: mlv-app
check: Guard: Test-DispatchFleetQuiet.ps1 (MLV-App board hub-tools, not in this repo; fixture pauses are temp files, never the real one)
supersedes: none
evidence: measured
