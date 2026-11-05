---
target: specs/mlv-app/cos-concerns.md
kind: concern
source_commit: PENDING
law4: attested
---
### mlv-app-cc-20261105-1
- id: mlv-app-cc-20261105-1
- to: CoS
- opened_utc: 2026-11-05T23:00:00Z
- expires_utc: 2026-11-19T22:00:00Z
- kind: process
- evidence: All 14 PRs #751-#764 were created as drafts (issue timeline: 8 have a ready_for_review event, which only fires from draft; the other 6 are still draft with no convert_to_draft event); the CoS read 9 of them, all while draft, a median 11.4 min after creation (reviewed_utc in the bus history of cos-feedback/mlv-app/pr-<n>.md), and on the 7 with both times it read 58 min to 19 h 56 min BEFORE ready_for_review; #760-#764 have no read at bus tip edb4ed9 (quality-lift meter 2026-11-05T22:33Z: last pass 11-05 08:07Z). Rule relied on: FWO-DEFECT-PREVENT-1 item 6, "Run the Claude + Codex adversarial legs on the head before marking ready or requesting CoS review" (cos-feedback/mlv-app/fwo-defect-prevent-1.md lines 28-29 at bus commit a0cc4e5; MLV-App #454 comments 6227522231 and 6228165753); draft-until-legs governs ready_for_review and cannot govern a reader that reads drafts. Measurement of 2026-11-02 (261 product heads, 7 days; first Actions run as push time, first review/* status as key time): median push to CoS pass 6.5 min, to green CI 38.5 min, to first key verdict 59.6 min; the CoS read before any key existed on 255 of 261 heads (98 %).
- ask: Please start no pass on a draft PR of mlv-app, start one when GitHub records ready_for_review, and treat later pushes to a non-draft PR as today, because the owner ordered keys before the CoS read; scope: this orders only the first read of a PR, and a later push to a non-draft PR can still be read before its own keys, as it is today; costs accepted: the CoS may read after a merge (its review is advisory and never a merge precondition, owner ruling 2026-10-20, and no merge waits), so a finding on a merged head becomes a true escape and the ESCAPED meter becomes meaningful; the CoS loses early warning on chains that never reach ready; blocked-pass rates from CoS-first rows do not predict the rate after this change, so no improvement number is claimed; naming the factory's keys as first reader also holds in a project with no CoS; if declined, the project falls back to a no-PR candidate flow, which is costly because its mechanical key launcher finds work only through an open PR.
