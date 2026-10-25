---
target: RECEIPTS.md
kind: receipt
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-10-25 — MODEL-POSTURE-2026-10-22: ADOPTED (RULINGS.md R19.1 clause 3, adoption record under R19.2)

MODEL-POSTURE-2026-10-22: ADOPTED (agents/orchestration-tiering.md:27 and tools/coordination/Invoke-Lane.ps1:211-213; guard in tools/coordination/check-lane-model-currency.py `--posture`)

**Before:** the board's tiering table named Codex Astra as the model of the "judgement, cross-family" row (one pre-implementation design pass per substantive packet, one arbiter seat when verdicts conflict), and the lane table comment called astra the tie-breaker and design arbiter. That reserved two roles for Astra, which R19.1 clause 3 forbids ("A procedure that reserves a role for Astra re-points that role to the Sol tier").

**Built:** the row at agents/orchestration-tiering.md:27 now names the Sol tier for the design pass and the arbiter seat, and carries Astra as an optional diversity seat: never a default, never a gate, never the only Codex seat a procedure waits for, recorded as `astra: diversity` when a round uses it. The comment at tools/coordination/Invoke-Lane.ps1:211-213 says the same. The `role` string value of the astra row is unchanged, because tools/coordination/test_coordination_guardrails.py pins the whole row; only the comment carries the posture. R16.2 launch-time tier resolution, the `cross_family: UNAVAILABLE` degrade path and Sol as review key A are untouched.

**Proof:** `python tools/coordination/check-lane-model-currency.py --posture` exits 0 on the edited files and 1 on the pre-change copies of both (four problems named). The new AstraPostureTests in tools/coordination/test_check_lane_model_currency.py were 10 red before the guard existed and 10 green after; one of them runs the guard over the committed files, so a later edit that makes Astra a default or a gate fails that suite.

**Falsifier:** run the command above on the board's master. A non-zero exit, or a row in agents/orchestration-tiering.md that names Astra as the model of a required role, means the adoption has drifted. Boundary: this guard reads those two files; a prompt or a script outside them that waits on an Astra seat is not seen by it.
