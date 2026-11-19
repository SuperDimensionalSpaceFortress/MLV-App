---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/rotation-prep-audits-every-peer-session
rule: before telling the owner an account rotation is safe, the hub enumerates every open session, reads each one's latest report and its stated next steps, and moves every pending item and every session-scoped watcher (a monitor, a background wait, "this session wakes when X merges") to a durable owner: the scheduled hub tick's owed list, a pending-action row, or an OS scheduled task, plus that work's memory note. An idle session that refuses archiving because it "still has live work" is the tell that it holds something that dies with the account. It applies to every project that runs more than one session at rotation time.
mechanism: the hub's own rotation checks all passed (crons, handoff lists, uncommitted work), and it told the owner to rotate. A peer session that had refused every archive sweep for over 24 h held the only watcher for a fleet-tool PR merge, and its next steps were known only in that chat. The owner had to ask twice before the hub read that session and moved the watch to the hub tick.
check: none yet (the session list is an app tool, not visible to a board script)
supersedes: none
evidence: measured
