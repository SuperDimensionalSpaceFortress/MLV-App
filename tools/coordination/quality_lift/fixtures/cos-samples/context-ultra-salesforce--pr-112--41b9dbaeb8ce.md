schema: cos-feedback.v1
project: context-ultra-salesforce
repo: layibabalola/context-ultra-salesforce
pr: 112
head: e6601ebf4477f011b75d635d4941e341aa9ad365
reviewed_utc: 2026-10-22T01:48:54Z
event: pr-opened
verdict: blocker
ci_summary: exact-head canonical failed, 2 success, 2 skipped; non-draft/UNSTABLE; synthetic merged-tree checks absent.
posture: ADVISORY non-gating under R18
review_id: 8e50358a-5f15-4886-b870-891c5b330170
reviewer: primary dot

# layibabalola/context-ultra-salesforce#112 / e6601ebf4477f011b75d635d4941e341aa9ad365 / ADVISORY

## Blockers
- CI: docs/ROADMAP.md:3 and src/SfTools.App/SelfTestCli.cs:17505 introduce R1038, but exact-head canonical job119584694731 fails gate.haiku-pre-code-swarm-enforced, reporting missing compliance, failure-modes and UX audit artifacts. Completed 01:42:41Z, exit1. Reconcile actual project gate requirements and obtain passing exact-head CI; the earlier local passing report does not establish remote success. This is observed broken CI, not obsolete CoS clearance.

## Improvements
- tools/fleet-doctrine/Sync-FleetDoctrine.ps1:11-14: Add expected-origin, master-branch and clean-worktree preconditions, plus wrong-branch/dirty-clone fixtures. It currently fetches and merges origin/master into whichever branch occupies ClonePath. Operational hardening, not a demonstrated incident.
- tools/fleet-doctrine/Register-Cadence.ps1:19-28: Cover copy/registration failure and partial-install recovery. A failed registration can leave InstallPath populated, while subsequent installation refuses that directory.

## Nits
None.

## Notes
Complete 11-file source-first inspection: hook, settings, launcher, transport, fixtures and offline self-test pin. Documentation accompanies mixed executable changes, not a mechanical-only skip. Inherited partial source exposure disclosed. Same-tip comments, inline comments and reviews were empty afterward; no SHARED BLOCKER.

Live head rechecked open/non-draft/UNSTABLE, unchanged. Five observed checks: 2 success, 2 skipped, 1 failure. No commit statuses or published independent review reports. Exact-head independent Claude+Codex, applicable HIGH-RISK third key and merged-tree CI remain unverified/unwaived; synthetic merge5965bd32ba7294ab5399c301e687e17306652abc has no checks.

Existing CI evidence: https://github.com/layibabalola/context-ultra-salesforce/actions/runs/39705573392/job/119584694731

ACK or DISTINGUISH within 24 hours of publication, citing 8e50358a-5f15-4886-b870-891c5b330170. R18 advice is non-gating, never merge/enqueue authorization. Owner-reserved actions and all factory evidence remain unwaived. No tests or workflows launched.

Signed: primary dot
