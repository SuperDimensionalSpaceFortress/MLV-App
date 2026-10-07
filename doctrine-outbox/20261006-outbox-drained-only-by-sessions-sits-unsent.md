---
target: TRAPS.md
kind: trap
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-06 — an outbox drained only by sessions sits unsent for a day or more: merged lesson items waited 23.7 h and 49.5 h because only an interactive session ever ran the drain

**Trap:** the outbox mechanism is a tracked directory of lesson files and a drain command that appends them to the bus. Nothing ran the drain except a person's session. Six items merged in one pull request sat 23.7 hours unsent, and an earlier pull request's items sat 49.5 hours. Meanwhile the board's own heartbeat and acting tick ran every few minutes and never touched the outbox. The debt query reported "unsent" correctly; nothing was scheduled to act on it. A report without an actor is a log.

**Cost:** a lesson is most useful in the first day, while siblings may be diagnosing the same symptom; a day of lag is a day in which a published-late lesson cannot be retrieved.

**Fix:** the OS-owned autopilot beat (a per-user scheduled task, so it survives account rotation) now drains the outbox at most once an hour: it checks for debt on the published master ref, runs the repo's own drain command with push, bounded to 120 seconds (a slow fetch is killed and retried next hour) and idempotent (the bus dedupes on the item marker, and each push is proved before it is recorded). The beat logs the drain's exit code and counts. It was proven on a real beat, not a dry run: the first hourly drain after the change ran with exit 0, and the debt count was 0 afterwards.

**Rule:** any queue of work that must leave the repo needs an owner that runs without a session, a time bound, idempotence and a log line, or it waits for whoever next remembers. Prove it on the real scheduler's beat.

**Falsifier:** merge an outbox item and open no interactive session. Within about one hour plus one beat the autopilot log must show a drain line with exit 0 and the bus must carry the item's marker. Run the debt query immediately after: it must report no debt. If the item is still unsent after two hours with the autopilot beating, or a second drain in the same hour republishes it, the mechanism is not working.

**Guard:** the autopilot's queue-drain step (untracked hub tooling; proven on a live beat, no regression test recorded yet).
