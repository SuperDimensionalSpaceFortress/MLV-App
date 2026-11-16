---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/kill-switch-checked-at-the-launch-sink-not-only-the-scheduler
rule: a host kill switch (a pause file) is read by the one shared launch sink every dispatcher goes through, not only by the scheduler gate, and is read again after any wait longer than a few seconds (an admission or slot wait). A live switch refuses the launch with a typed line and reserves nothing. An unreadable switch file (read error, a directory, no header, no parsable until) is a refusal; an expired one is not.
mechanism: 2026-11-15, a hub tick already in flight dispatched a CPU-heavy lane 11 s into owner thermal trial fq-20261115T175518Z, because only the scheduled gate read the pause file and a launcher that is already running never passes through that gate. The fix checks at the sink and after the admission wait, and refuses with FLEETQUIET_REFUSED card=<c> pause=<id> until=<utc> (exit 3, no claim, no slot); a refusal is re-armed, not counted as a failure. Related but distinct: RULINGS.md:1401 ("A kill switch that is a FILE") says the stop is a file, and TRAPS.md:2945-2951 says to search for the decision about a paused component. Neither says where the file is read.
applies: mlv-app
check: Guard: Test-DispatchFleetQuiet.ps1 in the MLV-App board's hub-tools directory (a board tool, not tracked in this repo; PowerShell 7 on Windows; each fixture pause lives in a temp dir, the real pause file is never created)
supersedes: none
evidence: measured
