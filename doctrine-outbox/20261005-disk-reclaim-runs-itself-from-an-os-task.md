---
target: RECEIPTS.md
kind: receipt
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-05 — disk reclaim runs itself: an OS-owned scheduled task sweeps on a timer and under a free-space threshold, deletes only from a manifest, and proves reclaimed bytes from its own receipt

**Before:** nothing freed disk on its own. A disk guard only raised alerts. The reclaim sweep ran only when a hub session started it, and the lane-exit retire removed only merged worktrees. The free margin on the system volume sat a few GiB above the stop floor, and every hub session spent turns on manual cleanup.

**Built:** a per-OS-user scheduled task (windowless launcher, with the launcher, PowerShell, tick script and sweep pinned by hash, and `-Install` / `-Status` / `-Uninstall`) ticks every 15 minutes. It sweeps on a two-hour timer, or when free space falls under the pressure threshold and the last sweep is at least 45 minutes old. It never sweeps while the sweep lock is held, or while a hub reclaim is live (receipt open AND a live process naming its folder). Because the task belongs to the OS user, not to any agent session or account, it survives account rotation.

The sweep now also prunes run folders whose pull request is merged or closed. It deletes archives, frame-pacing captures and build folders, and keeps summaries, receipts, prompts and review verdicts, even inside a build folder. It skips any open PR, any run whose PR it cannot resolve, any run with a live process, anything written in the last two hours, and anything under a hold-until date. Every path goes into a manifest before deletion. The receipt reports manifest bytes, bytes actually gone, and the change in free space as three separate numbers, because they disagree when files are hard-linked or locked.

**Proof:** the first task-started sweep (trigger task-timer, not a shell) reported manifest 9.93 GiB, reclaimed 8.47 GiB and free-space change 8.14 GiB. That took free space from 63.2 to 71.4 GiB. The task's own status reports `lastResult=0` and the sweep pin MATCH. Earlier the same day, a manual archive-then-retire of eight clean, pushed lane worktrees plus in-place LZX compression of agent session logs had taken it from 56.0 to 62.9 GiB.

**Falsifier:** run the task's `-Status`. Expect a tick within 15 minutes, `lastResult=0` and a matching sweep pin. Then open the newest task-triggered sweep receipt and confirm reclaimed bytes are greater than zero and every deleted path is listed in its manifest.
