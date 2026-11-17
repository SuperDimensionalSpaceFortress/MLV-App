---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/kill-switch-checked-at-the-launch-sink-not-only-the-scheduler
rule: a host kill switch (pause file) is read by the board launch scripts, not only the scheduler gate, and again after the admission wait. A direct call to Invoke-Lane.ps1 or Invoke-Workstream.ps1 never reads it. A live switch refuses the launch and releases any reservation taken (best effort), except the exempt HUB-TICK dispatcher (proceeds, dispatches nothing). A file seen to exist but unreadable (read error, a directory, no header or until) is a refusal on the same terms; an expired one is not; one access denied on its attributes reads as no pause. MLV_FLEETQUIET_PAUSED_PATH overrides the path (temp dir only); an override naming a missing file reads as no pause.
mechanism: 2026-11-15: a hub tick launched a heavy lane into thermal trial fq-20261115T175518Z; only the scheduler read the file. The fix checks in dispatch-lane.ps1 (and Start-HiddenDetached.ps1, whose launch classifier is a regex heuristic, neither exact nor complete: quoted text can be refused, a quoted -File switch or a heavy -Command string admitted; FLEETQUIET-SINK-BYPASS-SHAPES-1) and after the admission wait, and refuses with FLEETQUIET_REFUSED (exit 3). Before admission a refusal is re-armed, not a failure: 288 pause re-arms, then 144 capacity re-arms; past both it costs an ordinary attempt (3, then failed). But a refusal after the admission wait with PRODUCER_CAP_OK in the dispatch log or a lane receipt is read as admitted, so the row ends done, not re-armed; a refusal receipt that cannot be written (a stale one held open) is never seen, the wrapper ends UNPROVEN (exit 5) and an ordinary attempt is kept. An echoed pause id holding a success, gave-up or terminal marker is read as that marker (row done, gave-up or failed; no re-arm).
applies: mlv-app
check: Guard: Test-DispatchFleetQuiet.ps1 (board hub-tools, not in this repo)
supersedes: none
evidence: measured
