## 2026-10-15 KERNEL REFILE code@r11: conformance, answers to the 2026-10-13 harvest, K12 refile

KERNEL: DOGFOOD fleet-factory-kernel r5 · profile code@r11 · instance agents/factory-kernel-instance.md (MLV-App, anchored to the PR #444 merge commit) · since 2026-09-14

This block follows the 2026-10-07 block for this section. The clause-by-clause filing, in kernel §4 grammar, is
`adjudications/factory-kernel/mlv-app.md` on this review branch. It answers each row of the 2026-10-13 harvest
(`adjudications/factory-kernel/mlv-app.dispositions.md`, bus `ce2132b`). MLV-App paths cite fork/master `5d14b70c`.
Run dirs and ledger stamps are board-local (gitignored on MLV-App).

**Why now.** The code profile moved r10 to r11 (bus `a0a1325`; only the Subject identity (K3) opening changed). MLV-App
cut its subject declarations over to `profile: code@r11 kernel r5` at 2026-10-13T19:10:00Z. PROMPT-K §6 asks for a
refile on a revision change and weekly while the kernel is a CANDIDATE. This refile comes 8 days after the last one.

**Kernel r5 / code@r11 conformance (DOGFOOD, not ADOPT).** The window had 0 subjects closed end-to-end under code@r11. S1, PR #440
(delivered `5d14b70c`), is claimed for its pre-work declaration only, because its model keys bind the head tree, not the delivered tree.

| Clause | Status | 2026-10-13 harvest | MLV-App evidence |
|---|---|---|---|
| K1 roles | FIT | FIT | opus producer; sol (codex) and fable keys; the hub only keyed and enqueued |
| K2 register | GAP | INSTANCE-FAILURE, still failing | `docs/never-authorized.json` lists prohibitions; no owner-reserved register |
| K3 identity | GAP | INSTANCE-FAILURE, still failing | keys on tree `7aa104d2`, delivered `7809d1cd`; the merge witness now records `tree_equal=N key_credit=head-only` |
| K4 positive evidence | FIT | FIT | `Invoke-Lane.ps1:2142`, `:2351`; guard `test_coordination_guardrails.py:1289` |
| K5 declared profile | FIT | INSTANCE-FAILURE, **cured** | S1 declared `code@r11 kernel r5` + `contract_sha256` at 17:14:47Z; producer started 18:54:35Z |
| K6 independent key | FIT | FIT | sol (OpenAI family); hosted `merge_group` Tests success at the delivered commit, 47/47 checks |
| K7 delivery | FIT | FIT | pinned merge-queue enqueue plus a landing witness; no typed `CLOSURE_INCOMPLETE` |
| K8 capacity | FIT | FIT | Claude weekly 429 on 2026-10-15 01:19Z: refused work re-dispatched on original prompts after a usage probe |
| K9 resume | UNEXERCISED | UNEXERCISED | `tools/repo_hygiene/k9_witness.py` in CI; the resume surface is board-local |
| K10 inventory | GAP | INSTANCE-FAILURE, still failing | inventory dated 2026-09-14; no Codex account identity |
| K11 honest reports | GAP | INSTANCE-FAILURE, still failing | the instance map header said `code@r10` after the cut-over (fixed by PR #443, merge 3f5896d7, after the r1 evidence cut-off); its K4 line citations drifted |
| K12 feedback | GAP | INSTANCE-FAILURE, still failing | refile 8 days after the last; nothing schedules it; carded as KERNEL-REFILE-SCHEDULE-1, not yet built |

Profile rows: subject-identity GAP (still failing), acceptance-evidence GAP (still failing, partly cured), resource-terminals FIT,
delivery-target FIT. Harvest INSTANCE-FAILURE rows cured: 1 of 8 (K5).

**Measured gap the steward may want as data (no text change asked).** MLV-App binds a contract digest before work, but
two digests exist per subject. `declared` is computed over the prompt as authored and goes into the ledger line. `launched`
is computed over the text actually handed to the producer, after injected launch blocks. The authored bytes are not retained, so only
`launched` recomputes (S1: `declared=673066...`, `launched=bfb7b871...`). Key prompts, which carry the review scope and the
round-closing rule, are written at key time and bound by no digest. MLV-App's own follow-ups are KERNEL-K5-CONTRACT-SPAN-MARKERS-1
and KERNEL-K3-MECHKEYS-TREE-OID-1.
