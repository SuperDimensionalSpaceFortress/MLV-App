---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/resume-pointer-must-list-every-session-cron
rule: a resume pointer that tells a fresh session what to re-arm after a restart or an account rotation lists EVERY session-scoped scheduled prompt verbatim, each with its schedule, in one block at the top. The newest handoff checkpoint block itself (never an older block below it) carries a RE-DERIVE list of real, runnable commands and a RE-ARM list of real scheduled prompts with their schedules; a block that only names the lists, or says they are absent, does not meet the rule. The hub reads a completeness screen's verdict on every tick, not only when a rotation is announced, and treats that verdict as a screen, not as proof the rule is met. It applies to every project with session-scoped crons.
mechanism: the pointer's header said "re-arm TWO crons" while four were running; two had been added later and the header was never updated, so a resumed session would have silently dropped the bench-host stuck-check and the six-hourly roadmap report. Eight consecutive checkpoints in that session carried status and owed items but no RE-ARM list. The board's completeness check reported STALE every ten minutes, and nobody read it until the owner asked, before an early rotation.
check: a heading-and-marker screen, not an enforcement: it splits checkpoint blocks on one heading shape and looks for the two list marker words in the newest block, and reports READY when both words are found. It has two known gaps, so a READY is not proof: (1) a newest block whose heading uses a different shape (for example a timestamp without brackets) is not split off, so an older block's lists can satisfy it; (2) the marker search is unanchored, so a sentence that mentions a marker word, including one saying the list is absent, passes. A reader confirms both lists by eye in the newest block; closing the two gaps is a separate checker card
supersedes: none
evidence: measured
