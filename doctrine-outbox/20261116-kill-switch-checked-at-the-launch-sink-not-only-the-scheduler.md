---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/kill-switch-checked-at-the-launch-sink-not-only-the-scheduler
rule: a host kill switch (pause file) is read by the board launch scripts, not only the scheduler gate, and again after the admission wait. A direct Invoke-Lane.ps1 or Invoke-Workstream.ps1 call skips it. A switch live at the check refuses the launch and releases any reservation taken, best effort; one created after the check, before process creation, is not caught (check-then-launch race). The HUB-TICK dispatcher is exempt. A file seen to exist but unreadable (read error, directory, no header or until) is a refusal on the same terms; an expired one is not; one access denied on its attributes reads as no pause. Where fleetquiet-lib.ps1 will not load, a launcher fails closed instead: Start-HiddenDetached refuses on existence alone (an expired file refuses), queue-drain holds lane fills and HUB-TICK, not its own maintenance launches, dispatch-lane stops. MLV_FLEETQUIET_PAUSED_PATH overrides the path (temp only).
mechanism: a hub tick launched a heavy lane into thermal trial; only the scheduler read it. The fix checks in dispatch-lane.ps1 (and Start-HiddenDetached.ps1, whose launch classifier is a regex heuristic, neither exact nor complete: quoted text can be refused, a quoted -File switch or a heavy -Command string admitted; FLEETQUIET-SINK-BYPASS-SHAPES-1) and after the admission wait, refusing with FLEETQUIET_REFUSED. No re-arm guarantee is made: the board infers a refused row's outcome from run-dir files and counters, best effort, and an echoed card or pause id holding a success, gave-up or terminal marker is read as that marker.
applies: mlv-app
check: Guard: Test-DispatchFleetQuiet.ps1 (board hub-tools, not in this repo)
supersedes: none
evidence: measured
