---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/heavy-slot-waiter-poll-and-unreaped-nul-records
rule: a heavy-slot queue waiter should check liveness only for queued pids (GetProcessById), not by a whole-host process snapshot per poll. Sweep a malformed or all-NUL queue record only when ALL hold: (a) its LastWriteTime is older than MalformedSweepMinAge, a parameter at or above QueueStale (30 s, :24); (b) a read error never triggers a sweep; (c) a parsable record keeps the PID + start-time identity check; (d) removal is identity-safe and serialized: one sweeper at a time (named lock around move-check-restore); File.Move the record to a unique .sweep name, re-read it, delete it only if still unparsable; if it parses, a writer replaced it: move it back without overwrite; on a restore collision never infer republication from path existence (concurrent quarantines of V0/V1, V0 restored first, delete V1): keep the record with the newer heartbeatAt; on any other restore failure keep the moved file. No delete-by-pathname. For the heavy-slot owner to adopt or distinguish.
mechanism: an ineligible waiter sleeps, then Test-MyTurn calls Get-ProcessTable ([Diagnostics.Process]::GetProcesses()) and rereads every queue/*.json. Read-Json returns $null for a read error and bad content alike, and Get-LiveQueue's `if (-not $e) { continue }` precedes its -Sweep branch, so it is reread every poll, never removed. Write-JsonAtomic (:100-107) renames a tmp file over the path, so an old malformed record has no live writer. The heartbeat (:653-655) only recreates a wrongly removed record.
check: sample each `heavy-slot.ps1 run` pid's TotalProcessorTime twice, 10 s apart (delta/10); run Format-Hex on the oldest queue\*.json. MLV-App cannot patch the installed copy (tools/coordination/build-guard.ps1:152 pins its SHA-256).
supersedes: none
evidence: reported
- **Guard:** none yet (the installed heavy-slot is owner-installed; a fix ships as a new version; consumers re-pin)
