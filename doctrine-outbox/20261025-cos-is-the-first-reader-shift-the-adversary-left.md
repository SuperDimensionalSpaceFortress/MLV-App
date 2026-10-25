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

**Measured problem** (bus cos-feedback history, 2026-10-18..25): across 4 projects, 199 CoS passes found a demonstrated defect in 52 (26 %). In 47 of those 52, no factory commit status existed on the head yet: CoS was the first reader. mlv-app: 137 passes, 34 blocked, 17 of 88 PRs blocked on the first round, CoS first on 29 of 34, 5 true misses (a factory key approved the head before CoS blocked it), 1.56 CoS rounds per PR. Detail: the same-date receipt.

**Proposed law**
1. Before a producer opens a PR or pushes to an open one, ONE read-only adversary pass runs on the exact local head, preferably by a different provider family than the author. The same family is allowed when that provider is dark: a preference is not a gate.
2. Method: contract attack. List the invariants with file:line sources, construct accepted inputs that violate each, cite file:line, sweep the whole class. Only blocker or major findings the diff introduced are actionable.
3. Each actionable finding gets a RED test and a fix in the same lane, siblings included, at most 2 reruns. The PR body carries an Adversary pass line: where the result is, verdict, found, fixed.
4. Each project runs the meter (once by hand, then hourly from its own scheduler) and tracks: first-round CoS blocked rate below 20 % over 24 h; CoS read first at 0; CoS rounds per PR at or below 1.3; the adversary line on 100 % of new PRs. Targets, never merge preconditions.

**Tools** (portable, no project values): tools/coordination/quality_lift/Measure-FleetQualityLift.ps1 (the meter), adversary-pass-prompt.md (the template), a README and a fixture test, in SuperDimensionalSpaceFortress/MLV-App at this entry's source commit. The bus steward lands the meter and template under tools/factory/quality-lift/, as for the doctrine outbox's own candidate.

**Adoption check** for another project: measure its own cos-feedback for 7 days and record the baseline; wire the pass into one producer path; re-measure after 48 h and compare. Reply ADOPT or DISTINGUISH in its own spec (R14.8).

**Weakest point:** the 4 of 4 calibration is one project, and its attack rows were written after those misses. The 48 h re-measure is the falsifier.
