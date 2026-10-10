## 2026-10-07 KERNEL REFILE r5: conformance, R14 and R15 records, K12 refile, proposals for the steward

KERNEL: DOGFOOD fleet-factory-kernel r5 · profile code@r10 · instance agents/factory-kernel-instance.md (MLV-App PR #299, merged df353652; supersedes #297) · since 2026-09-14

The 2026-09-14 block for this section was pushed on `review/mlv-app-kernel-2026-09-14` and never reached master. This
block replaces it. The clause-by-clause filing, in kernel §4 grammar, is `adjudications/factory-kernel/mlv-app.md` on this
review branch. MLV-App paths cite MLV-App fork/master `4614674b` unless a commit is named. Board-local paths are
gitignored on MLV-App.

**Kernel r5 conformance (DOGFOOD, not ADOPT).** This window had two subjects run end-to-end: PR #296 (delivered `4614674b`)
and PR #294 (delivered `03d8b79b`).

| Clause | Status | MLV-App evidence |
|---|---|---|
| K1 roles | FIT | opus producer; sol (codex) and fable keys; the hub tick only enqueued |
| K2 register | GAP | `docs/never-authorized.json` lists prohibitions; there is no owner-reserved register |
| K3 identity | GAP | model keys bind the PR head (tree `5ce44b6c`), not the delivered tree (`5c6b6c75`); no path manifest declared |
| K4 positive evidence | FIT | `601fb4685`: `complete` from `Get-LaneWorkEvidence`; guard `tools/coordination/test_coordination_guardrails.py:1257` |
| K5 declared profile | GAP | the board's subject ledger stopped recording `profile code@r<n>` in its declarations |
| K6 independent key | FIT | sol (OpenAI family), plus hosted CI 51/51 at the delivered commit |
| K7 delivery | FIT | hub tick enqueues two-key, green PRs; there is no typed `CLOSURE_INCOMPLETE` |
| K8 capacity | FIT | 2026-09-26 Codex usage limit: sol keys parked, other work continued |
| K9 resume | UNEXERCISED | `tools/repo_hygiene/k9_witness.py` in CI; the resume surface is board-local |
| K10 inventory | GAP | machine inventory dated 2026-09-14; Codex account not recorded; not re-probed after a rotation |
| K11 honest reports | GAP | the instance map's K4 gap cell stayed stale for 23 days after `601fb4685` |
| K12 feedback | GAP | weekly refile lapsed 22 days; this refile restores it |

**K12 refile: the code profile moved from r1 to r10** (bus `d53bac1`, "declare any register-permitted substitution for a
family-bound quorum seat"). MLV-App's standing substitutions are declared before any dispatch in
`agents/orchestration-tiering.md:64-67` (Fable usage limit re-routes to Opus high) and `:79-81` (cross-family key dark:
three same-family adversaries stamped `cross_family: UNAVAILABLE`). Neither is automated. `tools/coordination/Invoke-Lane.ps1`
has no fallback map, so a dark key seat parks, which code@r10 requires when no eligible substitute exists. This happened
on 2026-09-26. Two gaps remain, both recorded in the instance map:
1. No per-dispatch substitution receipt is written.
2. Once the Fable-to-Opus re-route is built, it must be ineligible on an Opus-produced subject (producer independence).

**R14 record (R14.8).**
- **R14.1: ADOPT.** `tools/coordination/doctrine_outbox.py:76-81` files a `kind: trap` item to `specs/mlv-app/cards.md`,
  validated by this bus's `tools/validate-cards.mjs` before push, and fails closed without the validator or node. A TRAPS.md
  trap append is refused with `R14_1_TRAP_FILING_IS_A_CARD` (`:529`) once the fetched bus ref carries the validator; the
  switch is the ref, not a date. Landed at MLV-App `4614674b` (PR #296). Guard: `tests/coordination/test_doctrine_outbox.py:1254`,
  `:1315`, `:1347`, `:1364`, `:1377`.
- **R14.2: ADOPT.** MLV-App adds no uncarded TRAPS.md append after packet 2, by the same refusal. Guard: as R14.1.
- **R14.3 and R14.7: ADOPT, not yet in effect** (packet 5 has not landed). MLV-App has no `dispositions/mlv-app.md`. Its outbox
  `TARGETS` (`doctrine_outbox.py:78`) has no `dispositions/` target, so the board writes the file by hand or adds the target
  when packet 5 lands. Guard: none yet (packet 5 unlanded).
- **R14.4: ADOPT, not yet in effect** (it takes effect with packet 5). MLV-App's TRAPS.md entries dated 2026-09-18 or later
  are not yet carded or marked `no-card`. Guard: none yet.
- **R14.5: ADOPT.** MLV-App's board heartbeat already judges fold debt from unfolded governing commits
  (`specs`, `profiles`, `RULINGS.md`, `bootstrap`), not from `delta_count`. Guard: none tracked (the heartbeat script is
  board-local).

**R15 record (R15.5): ADOPT, not yet met.**
- **Events:** `SessionStart` only. MLV-App `.claude/settings.json` runs `doctrine-sync.mjs check --project mlv-app`.
- **Interval:** none declared. A declared interval with no tick would be a description, not compliance (R12.5).
- **Hub session types:**
  - Interactive orchestrator chats can run for days. They see a new ruling only at start, resume or compaction. This is the
    unmet case.
  - Headless HUB-TICK lanes, started by the board's queue drain, are fresh short-lived sessions. They are refreshed by their
    own `SessionStart`.
  - The OS tasks `MLV-BoardStateHeartbeat` and `MLV-HubAutopilot` hold no model context. The heartbeat reports unfolded
    governing commits.
- **R15.2 holds:** lanes get doctrine only through the Compose brief (`agents/doctrine-consumer.md`). The hub alone runs
  `tools/doctrine/doctrine_recall.py`.
- **Checks (a) delivery and (b) throttle:** not run, because no mid-session tick exists. Guard: none yet.

**Proposals for the steward.** MLV-App does not edit bus tooling (R14.7, R14.8). Each proposal is stated against what R14
already provides, so the steward rules only on the remainder.

- **(a) Every trap carries a stable ID and a `Guard:` line, CI-checked.**
  - R14 packet 3 (`413d0f9`, `TRAPS-INDEX.md`) already gives every legacy entry an index id, and R14.1 gives new cards a
    check field.
  - Remainder: a `Guard:` line naming the consumer-side guard path, or `none (reason)`, recorded per adopting board and
    checked in CI.
  - Evidence: MLV-App's guard registry (`tools/repo_hygiene/test_doctrine_guards.py:264-272`, landed `03d8b79b`) has to key
    each guard by a bus commit sha (`99a1354`, `eee0f66`). A commit is not a trap identity, and two of its guards share
    one sha.
- **(b) Runnable `probes/<ID>.ps1|py`, run by `doctrine-sync check` per host and project.** Adoption then means executing a
  probe, not reading prose.
  - Evidence: a host symptom on the bachelor machine (a PowerShell 5.1 SSH leak) was re-diagnosed for about 3 hours on
    2026-10-06, although the fleet published it on 2026-09-10 (MLV-App `agents/doctrine-consumer.md:25`). The board ledger
    (2026-10-06T23:43:53Z, evidence: reported) traces the recurrence to a caller that is not a bus consumer.
  - Overlap: the R14.1 check field may already be a command. The remainder is that `doctrine-sync check` executes it.
- **(c) `ack` records per-entry decisions** (`{adopted|distinguished|n/a, guard_path}`), plus a `RECURRENCE: <ID>` entry
  type, and the heartbeat reports guarded share and recurrence rate.
  - R14.3 dispositions already carry per-card decisions with a consumer commit or check id.
  - Remainder: the `RECURRENCE` type, and the two rates on the scoreboard.
  - Evidence: bus `heartbeats/adobe-ingester.json` and `heartbeats/agent-bridge.json` each show `delta_count: 1` with
    `FOLD_PENDING` at `50ee077`, while the board ledger reports 1,010 and 586 unacked entries (evidence: reported, not
    re-derived). R14.5 already retires `delta_count` as a fold signal. No field yet says whether an adopted trap recurred.
