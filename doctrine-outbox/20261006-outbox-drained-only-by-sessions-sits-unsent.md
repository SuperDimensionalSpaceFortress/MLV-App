---
target: RECEIPTS.md
kind: receipt
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-06 — an MLV measurement for ruling candidate mechanical-outbox-every-member-r1 (bus 2877eff): merged outbox items waited 23.7 h and 49.5 h because only a session ran the drain; the hourly unattended drain is now adopted

**Prior art:** ruling-candidates/mechanical-outbox-every-member-r1.md (bus 2877eff, proposed by MLV on 2026-09-28) already states the rule. Its part 2 says an OS-scheduled task, not a session, drains committed unsent items, idempotently and with each push proved; its part 3 puts debt older than 24 h on the heartbeat. MLV declared card DOCTRINE-OUTBOX-ADOPT-MLV-1 that day, and its own outbox README then assigned the unattended drain to card DOCTRINE-OUTBOX-ADOPT-MLV-1b and said nothing in the directory runs itself. MLV did not ship the drain before the measurements below. This entry restates none of the rule; it records the cost of that gap and the adoption of part 2 only.

**Measured (MLV):** six items merged in one pull request sat 23.7 hours unsent, and an earlier pull request's items sat 49.5 hours, because only an interactive session ever ran the drain. The debt query reported "unsent" correctly. Meanwhile the board's heartbeat and acting tick ran every few minutes and never touched the outbox. 49.5 hours is past the candidate's 24 hour debt line; 23.7 hours is just under it. A report without an actor is a log.

**Adopted (part 2 only):** the OS-owned autopilot beat, a per-user scheduled task, now runs the repo's own drain command with push at most once an hour. The tool publishes only unsent items, so a second run in the same hour republishes nothing. The call is bounded to 120 seconds (a slow fetch is killed and retried next hour), and the beat logs the exit code and counts. It was proven on a real beat, not a dry run: the first hourly drain after the change ran with exit 0, and the debt count was 0 afterwards. This entry makes no claim about the candidate's capture gate (part 1) or its debt heartbeat (part 3).

**Falsifier:** merge an outbox item and open no interactive session. Within about one hour plus one beat the autopilot log must show a drain line with exit 0 and the bus must carry the item's marker. Run the debt query immediately after: it must report no debt. If the item is still unsent after two hours with the autopilot beating, or a second drain in the same hour republishes it, the adoption is not working.

**Guard:** board-local, untracked: queue-drain.ps1, run by hub-autopilot.ps1 on every beat, drains at most hourly and bounded at 120 seconds. Proven on a live beat; no regression test recorded yet.
