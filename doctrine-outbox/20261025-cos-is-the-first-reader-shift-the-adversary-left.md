---
target: RECEIPTS.md
kind: receipt
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-25 - RULING CANDIDATE: CoS is the first reader: shift the adversary left and measure the lift

Owner-requested, 2026-10-25. Proposed only: it grants no runtime authority, and the CoS stays advisory (R18.1). Addressed to every project with a `cos-feedback/<project>/` folder on the bus.

**touches/supersedes:**
- adopts ruling-candidates/cross-family-review-is-a-preference-not-a-gate-r1.md (why the provider choice is a preference)
- touches R18.1 (RULINGS.md:2912, the CoS never blocks a merge; unchanged)
- touches R18.3 (RULINGS.md:2914, the merge floor; unchanged, the pass is not a key)
- extends R18.4 (RULINGS.md:2915-2919, quality compensation and the attack catalogue)

**Measured problem** (measured by the mlv-app hub staging meter (not shipped), conservative classification; UNKNOWN share 51 %, 187 of 366 fleet passes): 79 of 179 classified CoS passes (44 %) across 6 projects named a current blocker. mlv-app, the only project declaring review evidence: 55/130 blocked, CoS read 40 of those 55 heads first, 13 escaped (true misses). Detail: the same-date receipt, same figures.

**Proposed law**
1. Before a producer opens a PR or pushes to one, ONE read-only adversary pass runs on the exact local head, preferably by another provider family; the same family is allowed when that provider is dark.
2. Method: contract attack. List the invariants with file:line sources, build accepted inputs that violate each, sweep the class. Only introduced blocker or major findings are actionable.
3. Each actionable finding gets a RED test and a fix in the same lane, siblings included, at most 2 reruns. The PR body carries an Adversary pass line: result location, verdict, found, fixed.
4. Each project counts its own lift from its own review evidence: escaped (its keys approved, then CoS blocked), factory-caught-first and CoS-read-first; an unclear CoS pass is unknown, with the unknown share beside every rate. Targets, not merge preconditions: first-round CoS blocked below 20 % over 24 h; CoS-read-first 0; CoS rounds per PR at most 1.3; the adversary line on 100 % of new PRs.
5. Once the CoS structured verdict line exists, projects read the CoS verdict from it, not from prose.

**Tools:** tools/coordination/quality_lift/adversary-pass-prompt.md (the template) and its README, on master of SuperDimensionalSpaceFortress/MLV-App from PR #689 on (earlier commits carry a withdrawn README). No meter ships until the CoS verdict line exists.

**Adoption check** for another project: name which of its evidence is a review approval, count the three over 7 days of its cos-feedback as a baseline, wire the pass into one producer path, re-count after 48 h. Reply ADOPT or DISTINGUISH in its own spec (R14.8).

**Weakest point:** the 4 of 4 calibration is one project, its attack rows written after those misses; CoS-first evidence is one project; half the CoS passes are UNKNOWN. The 48 h re-count is the falsifier.
