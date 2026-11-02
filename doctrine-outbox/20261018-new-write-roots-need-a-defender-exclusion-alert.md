---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/new-write-roots-need-a-defender-exclusion-alert
rule: a change that creates a new heavy-write root (scratch or worktree parent, build tree, temp prefix, per-test LOCALAPPDATA folder) registers it in a machine manifest; a session-start check prints ONE copy-paste elevated command for every unacknowledged root. Agents never add exclusions: a security setting, and unreadable without elevation.
mechanism: a 16-core host sat at 91-100% CPU with Defender at 1.3-1.7 cores despite a long exclusion list. A 60 s Defender performance recording traced the scans to roots created after the list: a test scratch tree moved to a new top-level folder, a `-state` sibling of an excluded tool folder, per-test LOCALAPPDATA folders, and the 8.3 form of an excluded temp path. Image-name process exclusions missed Git's `ls.exe`/`grep.exe`. After the owner added the roots and the unbounded share listing was stopped, Defender fell to about 0.2 core.
check: owner, elevated, `New-MpPerformanceRecording -RecordTo $env:TEMP\mp.etl -Seconds 60; Get-MpPerformanceReport -Path $env:TEMP\mp.etl -TopProcesses 20 -TopFiles 40`; a project write root in TopFiles is the finding.
supersedes: none
evidence: measured
- **Guard:** PROPOSED in softwarefactory-fleet-doctrine PR #89 at `tools/machine/fleet-defender/`, pending merge. Once #89 merges, the code that creates a root runs `Register-DefenderRoot.ps1 -Path <root> -Project <name>` (unelevated, one manifest row); an owner-installed SYSTEM task excludes allowlisted roots (long and 8.3 forms) and a SessionStart hook prints ONE elevated command for the rest. After #89 merges, install once per machine, owner elevated: `pwsh -NoProfile -File <bus>\tools\machine\fleet-defender\Install-FleetDefenderRoots.ps1 -LocalPolicy <machine policy>`.
- **Do not:** list a network share unbounded (`ls -t`): Defender scans each file over SMB, measured at 48 s of scan per 60 s on a 20k-file folder.
