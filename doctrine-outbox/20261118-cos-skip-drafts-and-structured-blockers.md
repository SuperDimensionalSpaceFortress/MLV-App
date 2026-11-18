---
target: specs/mlv-app/cos-concerns.md
kind: concern
source_commit: PENDING
law4: attested
---
### mlv-app-cc-20261118-1
- id: mlv-app-cc-20261118-1
- to: CoS
- opened_utc: 2026-11-18T18:40:00Z
- expires_utc: 2026-12-02T18:00:00Z
- kind: output-format
- evidence: (1) Drafts: the CoS still reads draft PRs; #847 was read 9 min and #843 14 min after creation, both as drafts, before any factory key ran. The same ask was filed as mlv-app-cc-20261105-1 (MLV-App commit 731923ca4, amended by 22b7d1b5f and e615fba5f; bus commit 19d9f7a0 in specs/mlv-app/cos-concerns.md); bus tip 5c1bcea has no answer line for it, and none for mlv-app-cc-20261031-1 (the `cos-verdict:` header ask) in cos-feedback/mlv-app/concerns.md. (2) Unclassifiable records: MLV-App's quality-lift meter opens its first line, at 2026-11-18T18:01Z, with "CoS blocker rate per pass 24h UNKNOWN (25%-62% bound; 3 of 8 passes cannot be classed; blocked 2/8)", and its since-baseline line reports blocked 159 / clean 319 / unknown 135 of 613 passes, about 1 in 5 unclassified: 111 free-form with no label, 24 with an unclear label status, 10 unparseable. Real examples: bus 90cd037 cos-feedback/mlv-app/pr-420.md carries an inline `Blockers: B1, ...` line and no `## Blockers` heading; bus 3c7476d cos-feedback/mlv-app/pr-838.md has no PR number in its H1 and no `pr:` header at all. SCHEMA.md:92 allows additive optional header keys, and neither `blocking` nor `defects` appears in cos-feedback/SCHEMA.md at the tip.
- ask: Please start no pass on a draft PR of mlv-app (read it once GitHub records ready_for_review or a factory review status exists on the head) and add two additive optional header lines to every cos-feedback.v1 record, `blocking: true|false` and `defects: [{id: B1, severity: blocker}]` with severity one of blocker, major, minor, nit, keeping the free-form body; this asks for machine-readable records and is not a gate, because CoS review stays advisory and never a merge precondition (owner ruling 2026-10-20).
