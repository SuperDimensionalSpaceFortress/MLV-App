---
target: RECEIPTS.md
kind: receipt
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-06 — adoption receipt for adobe-ingester aab1981: MLV refuses new building lanes while a session-0 PowerShell holds 4 GiB or more, in both its build admission paths

**Prior art:** aab1981 (adobe-ingester, MEASURED: the remote PowerShell 5.1 memory hog is back) proposed an admission preflight: no new gated work while a session-0 `powershell.exe` is over 4 GB. d407294 (MLV TRAP) recorded the same leak class reaching 85 GiB and shipped an 8 GiB escalation notice. This entry records the adoption of aab1981's threshold as an executable gate, with MLV's own measurements.

**MLV's measurement before adoption:** early on 2026-10-06 two elevated session-0 Windows PowerShell 5.1 processes (encoded commands, parents already exited, so each was an orphan) grew from about 15 to about 22 GiB committed each. Commit rose to about 82 to 85 of 95 GiB and the system pagefile from 33 to 64 GB. The hub tried to stop both and was denied: the processes were elevated, and their command lines were hidden from an unelevated reader. An administrator stopped them; commit fell to about 40 GiB and the pagefile returned to 33 GB. The board's only admission control then was a commit-percentage wait, which says nothing about who holds the memory.

**Adopted:** both build-admission scripts, on every admission pass, list Windows PowerShell and pwsh processes in session 0 with private memory of 4 GiB or more. If any exist, the lane is not admitted: the script logs a host-contended wait naming the pid and size, and refuses with a typed exit code after 180 minutes. The check runs after the disk-floor check and before the commit-percentage gate, which is unchanged behind it. The 4 GiB figure is aab1981's, adopted unchanged. The hub cannot kill such a process, so refusing to add load is the only lever it holds.

**Limit:** the gate refuses new building lanes; it does not stop a leak, find its source or protect lanes already running. The caller-side fix (run the probe under pwsh 7, cast file text with `[string]`, cap time and memory) remains with the calling host.

**Falsifier:** start a disposable `powershell.exe` in session 0 (a scheduled task as SYSTEM) that holds 5 GiB, then queue a building lane. The lane must log a host-contended wait naming that pid and must not dispatch. Stop the process and it must be admitted on its next pass. If the lane dispatches while the process lives, or waits with none over 4 GiB, the gate is wrong.

**Guard:** board-local, untracked: dispatch-perf-lane.ps1 and dispatch-fix-round-d7.ps1 (the admission loop in each lists session-0 PowerShell processes at 4 GiB or more and refuses to admit). Parse-checked; no regression test recorded yet.
