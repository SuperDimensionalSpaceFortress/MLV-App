---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/new-write-roots-need-a-defender-exclusion-alert
rule: a change that creates a new heavy-write root (scratch or worktree parent, build tree, temp prefix, per-test LOCALAPPDATA folder) registers it in a machine manifest; a session-start check prints ONE copy-paste elevated command for every unacknowledged root. Agents never add exclusions: a security setting, and unreadable without elevation.
mechanism: a 16-core host sat at 91-100% CPU with Defender at 1.3-1.7 cores despite a long exclusion list. A 60 s Defender performance recording traced the scans to roots created after the list: a test scratch tree moved to a new top-level folder, a `-state` sibling of an excluded tool folder, per-test LOCALAPPDATA folders, and the 8.3 form of an excluded temp path. Image-name process exclusions missed Git's `ls.exe`/`grep.exe`. After the owner added the roots, Defender fell to about 0.2 core.
check: elevated, `New-MpPerformanceRecording -RecordTo $env:TEMP\mp.etl -Seconds 60; Get-MpPerformanceReport -Path $env:TEMP\mp.etl -TopProcesses 20 -TopFiles 40`; a project write root in TopFiles is the finding.
supersedes: none
evidence: measured
- **Guard:** append-only `%LOCALAPPDATA%\fleet\defender-roots.json` rows (path, project, reason; long and 8.3 forms) written by the code that creates the root; the hook diffs it with `defender-roots.acked.json` and prints `Start-Process pwsh -Verb RunAs -ArgumentList '-NoProfile -Command Add-MpPreference -ExclusionPath ''<root>''; <append ack>'`. Full automation is an owner-installed elevated task only.
- **Do not:** list a network share unbounded (`ls -t`): Defender scans each file over SMB, measured at 48 s of scan per 60 s on a 20k-file folder.
