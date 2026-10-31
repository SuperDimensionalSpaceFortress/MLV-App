---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/heavy-slot-waiter-poll-and-unreaped-nul-records
rule: a heavy-slot queue waiter should resolve liveness only for the pids in the queue (GetProcessById), not by a whole-host process snapshot on every poll. A malformed or all-NUL queue record should be swept only when ALL hold: (a) the file is older than MalformedSweepMinAge, a named parameter set to 3 x the heartbeat interval; (b) it is re-read once more immediately before removal and is still unparsable; (c) a queue file that cannot be read (a read error, not bad content) is never swept; (d) a parsable record keeps the existing PID + process start-time identity check. The poll cost is small: do not chase it as the cause of a CPU peg. For the heavy-slot owner to adopt or distinguish; MLV-App does not edit the installed copy.
mechanism: an ineligible waiter sleeps a short jittered interval, then Test-MyTurn calls Get-ProcessTable ([Diagnostics.Process]::GetProcesses(), whole host) and rereads every queue/*.json. Read-Json returns $null for a read error and for bad content alike, and Get-LiveQueue does `if (-not $e) { continue }` before its -Sweep branch, so such a record is skipped, never removed, and reread on every poll. A live writer rewrites its record every $QueueHeartbeat (5 s), so an age bound of several heartbeats cannot reap a live record. Observed on a 16-core host at high CPU under concurrent lanes: the waiters were a small share of the host.
check: sample TotalProcessorTime of each `heavy-slot.ps1 run` pid twice, 10 s apart, and divide the delta by 10; run Format-Hex on the oldest queue\*.json. MLV-App cannot patch the installed copy: tools/coordination/build-guard.ps1:58 refuses a heavy-slot.ps1 whose SHA-256 is not the pin.
supersedes: none
evidence: reported
- **Guard:** none yet (the installed heavy-slot is owner-installed; a fix ships as a new version and pinning consumers re-pin in the same change)
