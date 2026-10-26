schema: cos-feedback.v1
project: agent-bridge
repo: layibabalola/agent-bridge
pr: 268
head: a9fd2604ee0f8286163034a40e9c02c15d5ca0e1
reviewed_utc: 2026-10-24T23:15:30Z
event: opened
verdict: ADVISORY
ci_summary: Windows40141853848 queued; current independent keys and merged-tree CI unverified
reviewer: primary dot
review_uuid: 466d2b3b-c8b4-47ad-b978-f98cb3249b55
posture: non-gating ADVISORY under R18

# layibabalola/agent-bridge #268 / a9fd2604ee0f8286163034a40e9c02c15d5ca0e1 / ADVISORY
## Blockers
Inherited #267 B1 remains source-unfixed at dashboard_launcher.py:361-372: A reads its matching PID/URL, B publishes a new live runtime, then A unconditionally replaces B with stopped-A. The new commit changes only diagnostic output, not ownership-check/commit coordination. T3/T5 install the foreign record before the read, not between read and replace. Carry original ask6389390723 and single reminder6389547913; no duplicate outreach or claim this stderr delta caused the race.
## Improvements
The new :375-377 guarded os.write(2, UTF-8/backslashreplace bytes) removes this diagnostic's Python stderr buffering, addressing the declared closed-pipe exit-time flush failure. T7 and T8 exercise loop and signal paths with closed stderr; T10 covers server-thread termination; T4 now captures fd2. Source repair is distinct from queued hosted CI.
Best-effort diagnostic caveat: os.write's returned byte count is unchecked, so short writes may truncate a line; no demonstrated exit-status regression from that.
## Nits
Commit subject says “leaves no temp file,” but its body explicitly defers cleanup; no temp-file cleanup is implemented by this delta.
## Dispositions and coverage
Exact compare against already-reviewed ce87de3e3b9f2e4ebab949dc205917a1609a056e is one commit/two files: +7/-4 launcher and +204/-15 tests. Reviewed only that delta; unchanged inherited code not re-reviewed. Separate source-only inspection found no new delta blocker. #267 remains open at its old head; this new PR is not resolution of its ownership finding or the separate #266 background shutdown residual.
Prior own findings known; source-first before same-tip comments/reviews, both empty. No tests/code/workflows executed. Exact-head independent Claude+Codex, applicable third independent adversarial key, required CI and actual merged-tree CI remain unverified/pending. R18 advice is never a CoS merge prerequisite; defect merits, all cancellations and owner-reserved actions remain. No product writes, merge or approval.
Signed: primary dot.
