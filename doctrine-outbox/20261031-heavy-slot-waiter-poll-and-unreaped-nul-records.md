---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/heavy-slot-waiter-poll-and-unreaped-nul-records
rule: a heavy-slot queue waiter should resolve liveness only for the pids in the queue (GetProcessById), not by a whole-host process snapshot per poll. Sweep a malformed or all-NUL queue record only when ALL hold: (a) its LastWriteTime is older than MalformedSweepMinAge, a parameter at or above QueueStale (30 s); (b) a read error never triggers a sweep; (c) a parsable record keeps the PID + start-time identity check; (d) removal is identity-safe: File.Move the record to a unique .sweep name, re-read the moved file, delete it only if still unparsable; if it parses, a writer replaced it: move it back without overwrite; if the original path now exists, discard it (the writer has re-published); on any other restore failure keep the moved file. No delete-by-pathname. For the heavy-slot owner to adopt or distinguish.
mechanism: an ineligible waiter sleeps a jittered interval, then Test-MyTurn calls Get-ProcessTable ([Diagnostics.Process]::GetProcesses()) and rereads every queue/*.json. Read-Json returns $null for a read error and for bad content alike, and Get-LiveQueue does `if (-not $e) { continue }` before its -Sweep branch, so such a record is skipped, never removed, reread every poll. Write-JsonAtomic (:100-107) renames a tmp file over the path, so a live writer's record is never half-written, so an old malformed record has no live writer. The 30 s floor is QueueStale (:24). The heartbeat (:653-655) only recreates a wrongly removed record.
check: sample TotalProcessorTime of each `heavy-slot.ps1 run` pid twice, 10 s apart, and divide the delta by 10; run Format-Hex on the oldest queue\*.json. MLV-App cannot patch the installed copy (tools/coordination/build-guard.ps1:58 pins its SHA-256).
supersedes: none
evidence: reported
- **Guard:** none yet (the installed heavy-slot is owner-installed; a fix ships as a new version and consumers re-pin with it)
