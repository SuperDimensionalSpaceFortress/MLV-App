---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/resume-pointer-must-list-every-session-cron
rule: a resume pointer that tells a fresh session what to re-arm after a restart or an account rotation lists EVERY session-scoped scheduled prompt verbatim, each with its schedule, in one block at the top. Every handoff checkpoint carries a RE-DERIVE list (commands) and a RE-ARM list. A mechanical completeness check reads the newest checkpoint for both lists, and the hub reads that check's verdict on every tick, not only when a rotation is announced.
mechanism: the pointer's header said "re-arm TWO crons" while four were running; two had been added later and the header was never updated, so a resumed session would have silently dropped the bench-host stuck-check and the six-hourly roadmap report. Eight consecutive checkpoints in that session carried status and owed items but no RE-ARM list. The board's completeness check reported STALE every ten minutes, and nobody read it until the owner asked, before an early rotation.
applies: every project with session-scoped crons
check: the completeness check returns READY only when the newest checkpoint block carries both lists
supersedes: none
evidence: measured
