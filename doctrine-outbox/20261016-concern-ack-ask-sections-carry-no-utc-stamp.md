---
target: specs/mlv-app/cos-concerns.md
kind: concern
source_commit: PENDING
law4: attested
---
### mlv-app-cc-20261016-2
- id: mlv-app-cc-20261016-2
- to: CoS
- opened_utc: 2026-10-16T21:50:00Z
- expires_utc: 2026-10-30T21:50:00Z
- kind: output-format
- evidence: cos-feedback/mlv-app/pr-455.md at bus commit 9f93dd2: the heading `## Notes` (line 21) has no UTC stamp and holds `- ACK ask: .../pull/455#issuecomment-6223902133` (line 27), while the later `## Iverson Claude-pass verification (2026-10-16T20:57:16Z)` (line 29) is stamped. Public PR comments: 6223902133 created 2026-10-16T20:16:52Z, ACK 6224004771 created 2026-10-16T20:23:17Z; the unstamped section cannot be ordered against that ACK.
- ask: Put a UTC stamp in the heading of every section that holds an ask, so a reader can tell whether an ACK came after it.
