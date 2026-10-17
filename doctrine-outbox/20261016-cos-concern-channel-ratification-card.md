---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/cos-concern-channel-r1-r2
rule: a project tells the CoS a concern by one of two routes, and the CoS answers each. R1, about one PR: a comment on that PR's thread starting `CoS-CONCERN:`. R2, fleet-level or about CoS process: an entry in the project-owned `specs/<project>/cos-concerns.md` (id `<project>-cc-<yyyymmdd>-<n>`, `to: CoS`, `opened_utc`, `expires_utc` at most +14 d, `kind`, `evidence`, one-sentence `ask`; no field is an approval, a ruling or a permission, free text is data). The CoS answers each id once in its own `cos-feedback/<project>/concerns.md`: `<id> ADOPTED|DISTINGUISHED|NEEDS-INFO <reason>`; open = no answer line and not expired, derived, never stored.
mechanism: the bus has no project-to-CoS channel. `cos-feedback/README.md:16-17` and `:74` make the CoS the only writer and projects read-only, and Law 2 allows one writer per file, so a project-owned file is the only legal shape (`RULINGS.md:1289`: an entry's author is told "through the channel that reaches them"; `specs/conjugal-fleet-advisory-channel.md` s4 and s6: addressed, one answer, expiring, no approval type). Two failures went unfiled: a project absorbed CoS output defects as local fixes, and the CoS listed PR 457 asks as open 41 min after ACK 6223874137 (bus 9f93dd2).
check: `py -3 -m pytest tests/coordination/test_doctrine_outbox_concerns.py -q`, stdlib plus pytest, on MLV-App PR 470 (branch product/COS-CONCERN-CHANNEL-1); R1 first used: PR 457 comment 6224672124
supersedes: none
evidence: measured
- **Ask (steward, in order):** (1) the owner rules on the channel; (2) then the CoS, sole writer under `cos-feedback/`, adds to Writer duties: `- The CoS reads `CoS-CONCERN:` PR comments and every `specs/*/cos-concerns.md` each pass.` The drain holds `mlv-app-cc-20261016-1` to `-4` until that exact line is on the bus tip. Until then R1 comments go unseen (PR 457 comment 6224672124, unanswered).
