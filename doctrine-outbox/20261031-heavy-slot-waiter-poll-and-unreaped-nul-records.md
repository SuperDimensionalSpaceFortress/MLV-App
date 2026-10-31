---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/heavy-slot-waiter-poll-and-unreaped-nul-records
rule: a heavy-slot queue waiter should resolve liveness only for the pids in the queue (GetProcessById), not by a whole-host process snapshot on every poll, and an unparseable queue record should be swept as dead. The poll cost is small: do not chase it as the cause of a CPU peg. For the heavy-slot owner to adopt or distinguish; MLV-App does not edit the installed copy.
mechanism: a non-head waiter sleeps 500-749 ms, then Test-MyTurn calls Get-ProcessTable ([Diagnostics.Process]::GetProcesses(), whole host) and rereads every queue/*.json. Get-LiveQueue does `if (-not $e) { continue }` before its -Sweep branch, so an all-NUL record (a torn write; two were found, both dated 2026-10-28) is skipped, never removed, and reread on every poll. It does not change ordering. Measured 2026-10-31 on a 16-core host at 85-100% CPU, 4 live waiters, three repeats of Get-Process TotalProcessorTime deltas: about 0.15 cores in all, roughly 1% of the host.
check: sample TotalProcessorTime of each `heavy-slot.ps1 run` pid twice, 10 s apart, and divide the delta by 10; run Format-Hex on the oldest queue\*.json. MLV-App cannot patch the installed copy: tools/coordination/build-guard.ps1:58 refuses a heavy-slot.ps1 whose SHA-256 is not the pin.
supersedes: none
evidence: measured
- **Guard:** none yet (the installed heavy-slot is owner-installed; a fix ships as a new version and pinning consumers re-pin in the same change)
