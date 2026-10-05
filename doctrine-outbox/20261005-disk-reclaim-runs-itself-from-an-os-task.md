---
target: RECEIPTS.md
kind: receipt
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-05 — disk reclaim runs itself: an OS-owned scheduled task sweeps on a timer and under a free-space threshold, deletes only through one eligibility gate and a manifest, and proves reclaimed bytes from its own receipt

**Before:** nothing freed disk on its own. A disk guard only alerted, the sweep ran only when a hub session started it, and hub sessions spent turns on manual cleanup.

**Built:** a per-OS-user scheduled task (windowless launcher; launcher, PowerShell, tick script and sweep pinned by hash; `-Install` / `-Status` / `-Uninstall`) ticks every 15 minutes. It sweeps on a two-hour timer, or under the free-space threshold when the last sweep started at least 45 minutes ago. It never sweeps while the sweep lock is held or a hub reclaim is live: a reclaim receipt written in the last 12 hours, open, AND named by a live process's command line (if process listing fails: open and written within 3 hours). Owned by the OS user, not an agent session, it survives account rotation.

The sweep prunes run folders whose PR is merged or closed: archives, frame-pacing captures and build folders, keeping summaries, receipts, prompts and verdicts even inside a build folder. Every deletion in the run-folder root passes one gate: PR resolved and not open, no live process, nothing in the folder written in the last two hours, no hold-until date. If the PR list cannot be fetched, nothing there is deleted. Paths go into a manifest before deletion; the receipt reports manifest bytes, bytes gone and free-space change separately, because hard links and locks make them disagree.

**Proof:** the first task sweep (08:47Z, timer) was truncated after its temp section: manifest 1.83, reclaimed 0.38, free change 0.39 GiB (see the companion TRAP). After the fix an operator re-pinned the sweep with the cooldown back-dated; the second task sweep (08:51Z) reported 9.93 / 8.47 / 8.14 GiB, free 63.2 to 71.4. At 09:37Z the task swept unattended on pressure: 4.74 / 3.29 / 3.31 GiB, free 69.8 to 73.1. Earlier, a manual archive-then-retire of eight worktrees plus LZX compression of agent logs took free space from 56.0 to 62.9 GiB.

**Falsifier:** with no sweep lock younger than 3 hours and the last task sweep at least 2 hours old, seed three disposable run folders written over 2 hours ago: a merged PR whose build folder holds a summary and a binary, an open PR, and no resolvable PR. After the next tick expect `-Status` to show a sweep with exit 0 and a matching pin; the receipt to carry its temp, merged-PR tally and end lines; the merged seed's binary manifested and gone, its summary kept; the other seeds untouched.
