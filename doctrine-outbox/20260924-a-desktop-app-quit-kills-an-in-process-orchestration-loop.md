---
target: TRAPS.md
kind: trap
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-09-24 — quitting a desktop chat app silently kills an orchestration loop running inside its process, while the work it already dispatched keeps running unread

**Symptom (measured):** an orchestration loop running inside a desktop chat application's session (dispatching background lane/subprocess work on a recurring basis) stalled for 21.4 hours. The application had not crashed — it had been quit normally (an `app_quit` event, not an exception, confirmed in the app's own main log). Meanwhile, background work items that loop had already dispatched before the quit kept running to completion in their own child processes, finishing with results nobody was reading, because the loop that would have read and acted on them was gone.

**Cause:** the orchestration loop (and any recurring re-check/wake logic tied to it) lived entirely inside the lifetime of one desktop-app process. That process has no special protection against a normal quit: the app closing takes every session, loop and timer inside it down at the same instant (the app's log recorded it stopping 22 active sessions), with no notification to anything outside the process. Here it was a quit, not a crash, a sleep or a reboot; the OS logs showed no power or crash event in the window. Work already handed off to separate child processes is unaffected and keeps running, which produces the worst version of this failure: results silently pile up unattended rather than the whole system visibly stopping.

**Rule:** never rely on a long-lived interactive chat/desktop-app session as the sole host for a recurring orchestration loop meant to survive hours or days. Treat it as ephemeral. On any resume after a gap, first check the host application's own log for a clean-quit or restart marker around the stall window, then explicitly enumerate and read every background/child-process result produced since the last confirmed loop action — do not assume "the loop would have surfaced it" for anything dispatched before the gap. The durable fix is an out-of-session driver (an OS-scheduled task that notices "a lane finished and nothing has acted since" and resumes or alerts); at the time of writing it was proposed, not built, and the scheduled snapshot task that existed only recorded state and never acted.

**Falsifier:** grep the host app's log for a quit/teardown event timestamped inside a stall window; if found, confirm whether any child processes dispatched before that timestamp produced results that were never subsequently read.
