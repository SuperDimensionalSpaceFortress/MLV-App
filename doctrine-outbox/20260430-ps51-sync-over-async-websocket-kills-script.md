---
target: TRAPS.md
kind: trap
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-04-30 — PowerShell 5.1 sync-over-async on a multi-second wait silently kills the process, `finally` never runs

**Symptom (measured):** a PowerShell 5.1 (Desktop edition) script used `$ws.ReceiveAsync($segment, $token).GetAwaiter().GetResult()` to block on a WebSocket message. Waits under roughly a second worked fine. Waits of about 2-3 seconds or more reliably killed the entire script process with exit code 2 — no exception printed, no stack trace, and critically **no `finally` block executed**, so cleanup code, summary files, and orphan-prevention logic never ran and child processes were left running. Reproduced three times across separate runs, each stopping right after the first inbound event.

**Cause:** this is the classic .NET sync-over-async deadlock shape — blocking on `.GetAwaiter().GetResult()` from a single-threaded-apartment pipeline thread whose continuation wants to resume on the same `SynchronizationContext` it is currently blocking. PowerShell 5.1's host appears to fail-fast (process exit 2) on this condition after a few seconds rather than hanging indefinitely, which makes it look like an unrelated crash rather than what it is.

**Rule:** never call `.GetAwaiter().GetResult()` on a Task that can take more than roughly a second to complete, from PowerShell 5.1. (`.Wait()` and `.Result` have the same shape but were not measured here.) Instead poll to completion (`while (-not $task.IsCompleted) { Start-Sleep -Milliseconds 25 }`, then call `GetResult()` only once the task is already done, which is always safe), or move the async logic to a runtime with proper async support (PowerShell 7+; in this case a Python asyncio client proved to work end to end on the same protocol).

**Falsifier:** run the same blocking pattern with an artificially delayed response (>3s) under PS 5.1 and confirm the process exits before any `finally`/`trap` block runs; then swap to the poll-to-completion pattern and confirm both the wait and the cleanup complete.
