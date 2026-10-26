schema: cos-feedback.v1
project: mlv-app
repo: SuperDimensionalSpaceFortress/MLV-App
pr: 480
head: e5dd4b473b0afbb58acddc22d72c03e1ec26e0f8
reviewed_utc: 2026-10-18T16:18:30Z
event: review-requested
review_id: 09a05586-cec8-4ad7-9a17-144f5739de71
seat_resolution_id: primary-seat-20261018T161437Z-intake-1615
verdict: not-merge-ready
ci_summary: 28 exact-head checks and both published factory review statuses are green; B1 remains a source-traced correctness issue.
risk: ordinary (standalone local runtime-suite utility; no workflow or security-setting implementation); LOW-CONFIDENCE
seat: dot
provisional: false
seat_claim_id: c9bdbb0c-e504-4f5a-a89e-53b54280951c
post_merge: false
asks: Fix or disprove B1 with a relative-worktree case, then publish independent reviews and required CI bound to the live head.

# Runtime-floor local matrix

SuperDimensionalSpaceFortress/MLV-App / PR480 / e5dd4b473b0afbb58acddc22d72c03e1ec26e0f8 / ADVISORY

Blockers: B1 (medium confidence, static trace): tools/repo_hygiene/runtime_floor_suites.py:359-370,411 preserves a relative --worktree then prefixes it into PowerShell file arguments while also setting child cwd to that worktree. From /parent with --worktree lane, a valid lane/tools/a.ps1 is passed as lane/tools/a.ps1 from /parent/lane, targeting /parent/lane/lane/tools/a.ps1. Both ps-script and ps-parse paths are affected; pytest uses the correct relative suite path. Resolve the worktree to an absolute path once before planning/spawning, or pass child-relative paths consistently, and pin a relative-worktree case. No runtime reproduction was executed under the read-only testing restriction.

Improvements: Existing factory hardening remains: bound taskkill.exe itself at line 378 and terminate POSIX child process groups at lines 379-384. The current timeout only kills the direct POSIX child.

Nits: none additional.

Independent source pass preceded reading factory review threads. Both published factory keys APPROVE this head; neither identifies B1, so it is not a SHARED BLOCKER. Earlier UTF-8/NUL/backslash path regressions are fixed and their tests remain. This advisory is not a factory leg.

## Evidence and scope
- Source is the three-file live diff: runtime-floors.json, runtime_floor_suites.py and test_runtime_floor_suites.py. No workflow/hook/settings implementation is included. CI-matrix and hook proposals mentioned by factory receipts are outside this change and not authorized by this verdict.
- Complete factory packet: https://github.com/SuperDimensionalSpaceFortress/MLV-App/pull/480#issuecomment-6250318793 .
- Producer: separate completed Sonnet implementing runs r1 (2026-10-17 10:14:35–10:52:57Z), r2 (12:10:28–12:18:53Z) and r3 (16:39:45–16:49:20Z), claude-sonnet-5-5, edits yes. Later head is a master merge; published Sol verified the three card files unchanged from add0d521. Published receipts are evidence, not raw-process access by dot.
- Codex leg review-pr480-sol-r3-20261018T1302Z completed 2026-10-18T13:10:00.7757410Z, APPROVE; https://github.com/SuperDimensionalSpaceFortress/MLV-App/pull/480#issuecomment-6250308641 . review/sol success description matches digest fb373bb4991aef2f.
- Claude leg review-pr480-opus-r3-20261018T1302Z completed 2026-10-18T13:11:02.9320253Z, APPROVE; https://github.com/SuperDimensionalSpaceFortress/MLV-App/pull/480#issuecomment-6250318277 . review/opus success matches digest 1f0649f2bec0b05b. This key used read-only file inspection and attributed execution receipts. Distinct nonediting review runs are separate from the implementing Sonnet runs; common publication author alone is not independence proof.
- Existing CI inspected 2026-10-18T16:18Z: all 28 check runs success, Tests https://github.com/SuperDimensionalSpaceFortress/MLV-App/actions/runs/39093445585 and Factory Bridge https://github.com/SuperDimensionalSpaceFortress/MLV-App/actions/runs/39093445769 . No tests or workflows were launched by dot.
- Existing RUNTIME-FLOOR-TASKKILL-TIMEOUT-1 and weak module-floor citation follow-ups remain; no claim that the timeout or runtime evidence is stronger than documented.
- No current owner clearance or ACK can remove a demonstrated code blocker. Required CI and real independent legs must remain current after any head change. No merge, enqueue or GitHub approval is performed.
- Amendment 5 permanent primary effective 2026-10-18T14:35Z. HIGH-RISK sunset count remains 1/3; this ordinary-risk determination does not change that count. Grok return alone does not transfer the seat.

Signed: dot, primary CoS.
