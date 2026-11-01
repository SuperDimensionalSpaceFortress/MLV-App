## 2026-11-01 KERNEL REFILE code@r11 (cadence): conformance, the 2026-10-13 harvest answered again, K12 refile

KERNEL: DOGFOOD fleet-factory-kernel r5 · profile code@r11 · instance agents/factory-kernel-instance.md (MLV-App, anchored to the merge commit of the PR for card KERNEL-REFILE-AGE-20261031-MLV-1) · since 2026-09-14

This block follows the 2026-10-15 block for this section. The clause-by-clause filing, in kernel §4 grammar, is
`adjudications/factory-kernel/mlv-app.md` on this review branch. The 2026-10-15 filing (`review/mlv-app-kernel-2026-10-15`, bus
`a1ca7a2`) has no harvest yet, so this one answers the 2026-10-13 harvest (`adjudications/factory-kernel/mlv-app.dispositions.md`,
bus `ce2132b`) again. MLV-App paths cite fork/master `df039a92`. Run dirs and ledger stamps are board-local (gitignored on MLV-App).

**Why now.** This is the weekly cadence refile (PROMPT-K §6). Kernel r5 (bus `5d1d0d9`), code@r11 (bus `a0a1325`) and
measured-objective@r3 are unchanged. The board-local meter `hub-autopilot/kernel-refile-schedule.ps1` queued it when the newest
MLV-App bus filing passed 7 days. The window is 2026-10-15T07:32Z .. 2026-11-01T12:45Z, which is 17 days.

**Kernel r5 / code@r11 conformance (DOGFOOD, not ADOPT).** Three subjects were delivered under code@r11 with declarations, keys and
witnesses: #724, #726 and #732. No subject is claimed end-to-end. #732 is delivered-tree-equal (its keys bound tree `f50a8d5b`, the
delivered tree, `key_credit=delivered`), but its pre-work acceptance binding is unproven (the r3 declared digest does not
recompute and the key prompts are unbound), so it gets identity-link credit only.

| Clause | Status | 2026-10-13 harvest | MLV-App evidence |
|---|---|---|---|
| K1 roles | FIT | FIT | sonnet/opus producers; sol, opus, fable read-only keys; #726 key3 is the producer's model in a separate run |
| K2 register | GAP | INSTANCE-FAILURE, still failing | `docs/never-authorized.json` lists prohibitions; owner rulings live in the board ledger only |
| K3 identity | GAP | INSTANCE-FAILURE, still failing (partly cured) | #732 keys and delivery share tree `f50a8d5b` (identity link only); per distinct PR, 236 of 257 witnessed are `head-only` and 21 `delivered`; no gate refuses them |
| K4 positive evidence | GAP (regraded) | FIT | claude lanes conform (`Invoke-Lane.ps1:2251`, `:2483`; guard `test_coordination_guardrails.py:1327`); codex exit 0 with empty, blank or unrelated output is booked complete (`lane-provider-refusal.ps1:88-109`) |
| K5 declared profile | GAP | INSTANCE-FAILURE, still failing (relapsed) | dispatched rounds declare before work; #728, #730 and #731 were produced by a hub chat and declared retroactively |
| K6 independent key | FIT | FIT | sol (OpenAI family) on every subject head; hosted CI 35/35 per head; `merge_group` success per landing |
| K7 delivery | FIT | FIT | enqueue witness per landing; no typed `CLOSURE_INCOMPLETE` |
| K8 capacity | GAP (regraded) | FIT | 97 refusals in 19 events (last 2026-11-01T07:28Z). Most relaunches followed an ok probe, but 5 identical-prompt re-runs ran before a passed probe (10-17, 10-24, 10-29) and 33 launches had no ok probe line since the refusal |
| K9 resume | UNEXERCISED | UNEXERCISED | `k9_witness` in CI (`tests.yml:135`, `:217`); the rotation resume was same-host |
| K10 inventory | GAP | INSTANCE-FAILURE, still failing | inventory dated 2026-09-14; no Codex identity; not re-probed after the 2026-10-27 rotation |
| K11 honest reports | GAP | INSTANCE-FAILURE, still failing (partly cured) | header guard `test_kernel_instance_profile_agreement.py` (#451); K4 citations and the board-local hash table drifted |
| K12 feedback | GAP | INSTANCE-FAILURE, still failing (partly cured) | a board-local scheduler queued this refile; the 2026-10-15 filing waited 8 days for publication; 17-day window |

Profile rows: subject-identity GAP (still failing, partly cured), acceptance-evidence GAP (still failing, partly cured),
resource-terminals FIT, delivery-target FIT. Harvest INSTANCE-FAILURE rows cured: 0 of 8. Five are partly cured, and K5 relapsed. Two clauses the harvest graded FIT are regraded in this refile: K4 and K8 (GAP above).

**Instance changes the steward should see (no text change asked).** (1) By owner ruling on 2026-10-29, a PR touching only `tools/` or
`tests/` and outside the HIGH-RISK path class merges on one authenticated exact-head key, not two. (2) By owner ruling on 2026-10-20,
Dot/CoS review is advisory and never a merge precondition. (3) A declared contract digest recomputes from retained bytes for hand
declarations and for one dispatcher round whose declared and launched digests are equal (#724 r3). It still fails for dispatcher
rounds where the two differ (#726 r5, #732 r3).
