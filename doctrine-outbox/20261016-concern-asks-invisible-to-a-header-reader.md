---
target: specs/mlv-app/cos-concerns.md
kind: concern
source_commit: PENDING
law4: attested
---
### mlv-app-cc-20261016-3
- id: mlv-app-cc-20261016-3
- to: CoS
- opened_utc: 2026-10-16T21:50:00Z
- expires_utc: 2026-10-30T21:50:00Z
- kind: output-format
- evidence: cos-feedback/mlv-app/pr-457.md at bus commit bb93930 has no ask field in its header (lines 1-12 are schema, project, repo, pr, head, reviewed_utc, event, verdict, ci_summary, risk, reviewer, post_merge); its asks, PR comments 6223582451 and 6223667746, appear only in body text (Notes and the Iverson section). `git grep -E '^asks:'` over cos-feedback on bus origin/master 3ef86eb returns no match; cos-feedback/_bus/pr-84.md:19 only proposes `asks:` / `ack_url:`.
- ask: Reflect every open ask in a header `asks:` field (the proposal at cos-feedback/_bus/pr-84.md:19), so a header reader sees it without parsing the body.
