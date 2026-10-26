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

**Measured problem** (bus cos-feedback, 2026-10-18..25): across 6 projects, 69 of 342 CoS passes (20 %) found a demonstrated defect. CoS-first is claimed only from declared review evidence. mlv-app declares its review/* factory keys: 43 of 248 blocked, 25 of 179 PRs blocked first round, CoS first on 33 of 43 (no prior review/* status), 10 true misses (a review/* key said success before CoS blocked), 1.39 rounds per PR. The other 5 declare nothing yet: unknown. Detail: same-date receipt.

**Proposed law**
1. Before a producer opens a PR or pushes to an open one, ONE read-only adversary pass runs on the exact local head, preferably by a different provider family than the author. The same family is allowed when that provider is dark: a preference is not a gate.
2. Method: contract attack. List the invariants with file:line sources, construct accepted inputs that violate each, cite file:line, sweep the whole class. Only blocker or major findings the diff introduced are actionable.
3. Each actionable finding gets a RED test and a fix in the same lane, siblings included, at most 2 reruns. The PR body carries an Adversary pass line: where the result is, verdict, found, fixed.
4. Each project declares its review evidence (its review status contexts and/or native PR reviews), runs the meter (by hand, then hourly) and tracks: first-round CoS blocked rate below 20 % over 24 h; CoS read first at 0; CoS rounds per PR at or below 1.3; the adversary line on 100 % of new PRs. Targets, not merge preconditions.

**Tools** (portable; project values only in review-sources.json): tools/coordination/quality_lift/ Measure-FleetQualityLift.ps1 (the meter), review-sources.json, adversary-pass-prompt.md (the template), README, fixture test, in SuperDimensionalSpaceFortress/MLV-App at this entry's source commit. The bus steward lands them under tools/factory/quality-lift/.

**Adoption check** for another project: declare its review evidence, measure 7 days of its cos-feedback as the baseline, wire the pass into one producer path, re-measure after 48 h. Reply ADOPT or DISTINGUISH in its own spec (R14.8).

**Weakest point:** the 4 of 4 calibration is one project, its attack rows written after those misses; CoS-first evidence is one project until others declare. The 48 h re-measure is the falsifier.
