# Orchestration tiering (owner ruling 2026-09-08, adjudicated, veto open)

Standing owner instruction, September 8, 2026: Fable spends tokens only on important complex
reviews; Opus is the hub-lane traffic cop; Haiku reports status in chat sessions; adjudication
swarms are Opus; when Fable is exhausted, fall back to Opus at high effort. This document is the
tracked pointer; the decision record with measurements and the packet order lives in
`.claude-state/continuity/ORCHESTRATION-TIERING-20260908.md` (gitignored, canonical checkout only).
It composes with [error-remediation.md](error-remediation.md) and the fleet ruling that the
inference tier follows the highest-stakes act a surface performs.

## Tiers

LANE-MODEL-CURRENCY-1 (owner ruling 2026-09-26): no lane ever runs a superseded model
because a version was written down somewhere, and every lane runs at HIGH effort, never
max/xhigh. A claude-engine row in `tools/coordination/Invoke-Lane.ps1`'s `$LANES` table
names a bare CLI alias (`opus`/`sonnet`/`fable`), which floats with the claude CLI itself
-- it is never a pinned id like `claude-fable-5`. A codex-engine row names a TIER
(`sol`/`luna`/`astra`), resolved to the current highest-version model slug AT LAUNCH by
`tools/coordination/resolve-codex-tier.py` reading `~/.codex/models_cache.json` --
Codex has no floating alias, so this resolution step is the equivalent. Every receipt
records `requestedModel` (the alias/tier asked for) and `resolvedModel` (what the run's
own output, or the resolver, proved actually ran), never a copy of one for the other.

| tier | model | effort | does | never does |
|---|---|---|---|---|
| judgement | Fable (`fable` alias row in `tools/coordination/Invoke-Lane.ps1`, or a Fable chat session) | high, and only high (LANE-MODEL-CURRENCY-1 round 2: `Invoke-Lane.ps1` now refuses with a typed `lane-effort-must-be-high` failure receipt before ever starting a real lane at any other effort) | consequential reviews, design, doctrine-fold decisions, one hard adjudication per packet | implement, ratification loops, routine cards, the hub loop |
| judgement, cross-family | Codex Astra (`astra` tier row in `tools/coordination/Invoke-Lane.ps1`, resolved to the current highest-version `gpt-*-astra` slug at launch) | high (LANE-MODEL-CURRENCY-1 superseded the earlier xhigh choice: every lane, astra included, runs at high, never max/xhigh) | one pre-implementation design pass per substantive packet; one arbiter seat when consequential verdicts conflict; one doctrine seam when a packet exposes a reusable policy defect | anything routine; a loop, heartbeat, cadence wake, recon, or edit; the `solVerdictPath` key; astra is deliberately absent from `Invoke-Workstream.ps1` and `Invoke-WorkstreamLoop.ps1`'s own `-Lane` lists, so nothing on a timer can reach this tier. Reachable and measured: codex-cli 0.154.0, `xhigh` accepted via `%APPDATA%\npm\codex.cmd`, probed 2026-09-15T23:16Z (superseding the earlier 0.147.0 unreachable finding); effort itself is now high, per LANE-MODEL-CURRENCY-1. |
| hub | Opus | high | derive board, pick ONE packet, dispatch ONE editing lane in an isolated worktree, verify the receipt, refresh the checkpoint, reconcile queue rows through the verified writer | edit product source, build, run probes itself |
| adjudication swarm | Opus, three briefs (against the default; what outranks it; post-mortem and evidence binding) | high (LANE-MODEL-CURRENCY-1 round 2: no real lane, this swarm included, can run below high) | any blocker or "what next"; a new class of owner grant or a trust-boundary design | ruling on procedure alone (Haiku with script-first pre-filtering is admitted for that) |
| reviewer | Sol (`sol` tier row in `tools/coordination/Invoke-Lane.ps1`, resolved to the current highest-version `gpt-*-sol` slug at launch) | high, and only high (LANE-MODEL-CURRENCY-1 round 2 retired the earlier low-for-PR-review carve-out) | final PR review bound to the exact head; adversarial verification | editing; sole authority on a contract claim |
| implementer | Sonnet | high (LANE-MODEL-CURRENCY-1: every lane, sonnet included, now high by default in the table, never max/xhigh) | one packet, one worktree, one review subject | the canonical checkout; widening scope |
| recon | Luna (`luna` tier row in `tools/coordination/Invoke-Lane.ps1`, resolved to the current highest-version `gpt-*-luna` slug at launch) | high, and only high (LANE-MODEL-CURRENCY-1 round 2 retired the earlier low-recon carve-out) | read-only shards, doctrine folds, evidence audits | editing |
| status | Haiku (Desktop chat session from `.claude-state/continuity/HAIKU-STATUS-PASTE.md`) | n/a | read heartbeat, receipts, PR list; report | adjudicate, mutate, dispatch, answer "what next" |

## Precedence against error-remediation.md

[error-remediation.md](error-remediation.md) governs a recoverable FAILURE inside an already
authorized packet: the hub records the failure, dispatches two or three read-only Luna shards for
root cause and regression evidence, takes Fable or Opus wisdom on conflicting evidence, and applies
the smallest supported fix itself, in the packet's own worktree, within the packet's allowed paths.
That stays as written. This document governs SELECTION and ADJUDICATION: which packet runs next,
who runs it, and any question of policy, ordering or trust boundary. Those use the Opus swarm.
"The hub dispatches; it does not do" means the hub never takes a packet's implementation onto
itself; it does not forbid the in-packet remediation fix that error-remediation.md assigns to the
hub. Luna shards and the Opus swarm are complementary: Luna gathers evidence at zero marginal
cost, Opus rules on it.

The lane table in `tools/coordination/Invoke-Lane.ps1` hard-codes every row at high effort
(LANE-MODEL-CURRENCY-1, 2026-09-26: sonnet and astra joined Luna and Sol at high, and the
table admits no other effort value). LANE-MODEL-CURRENCY-1 round 2 (owner ruling, 2026-09-26:
"every lane runs at high effort") closed the one remaining way to launch below that: the
`-ReasoningEffort` parameter itself still parses `low`/`medium` (unchanged, so existing
guardrail tests keep pinning its `ValidateSet`), but `Invoke-Lane.ps1` now refuses to start any
REAL lane at anything other than `high` -- a typed `lane-effort-must-be-high` failure, a normal
`failed` receipt, non-zero exit, before any provider process starts. The historical low-effort
Luna/Sol receipts cited in earlier revisions of this paragraph predate that enforcement and are
no longer a live dispatch pattern; the only surviving non-high `-ReasoningEffort` invocations are
inside the containment test fixture, which replaces the launcher's own `$CLAUDE_EXE`/`$CODEX_EXE`
with a disposable shim and so can never reach a real model regardless of the value passed.

## Rules that carry

- The hub dispatches; it does not do. A coordinator's own mutations are receipted like a lane's.
- Fallback: a Fable usage-limit refusal routes the same prompt to Opus high. The runner's refusal
  classifier detects the limit; the automatic re-route is packet LANE-FALLBACK-1 (a fallback map
  beside the lane table in `tools/coordination/Invoke-Lane.ps1`, every row staying at `high` effort
  per LANE-MODEL-CURRENCY-1 -- never `max`/`xhigh`).
- Cross-family review is preferred, and required only for guard, merge, commit and
  ratification-path changes. The normative list of those paths is the hook's own fixed set,
  `CONTROL_HASHES_BASE` plus `EXECUTION_CONTROL_HASH_TABLE` in
  `tools/hooks/mlv-never-authorized.py`, which at 3e2b2220 enumerates eleven files:
  `tools/hooks/mlv-never-authorized.py`, `tools/hooks/test_registration_path_local.py`,
  `tools/repo_hygiene/test_mlv_never_authorized.py`, `tools/coordination/Invoke-Lane.ps1`,
  `Invoke-Workstream.ps1`, `Invoke-WorkstreamLoop.ps1`, `Compose-LanePrompt.ps1`,
  `demote-factory-bridge.ps1`, `set-required-checks.ps1`, `Test-ProductRatioGuard.ps1`,
  `freeze-factory-cards.py`; plus `closeout.config.json`, `.github/workflows/`, `CLAUDE.md`
  and `AGENTS.md` as operating-contract surfaces. Independence is four properties: author is not reviewer, adversarial
  instruction, independent access to ground truth, decorrelation by assignment. Same-family
  adversarial swarms are eligible reviews. Degraded mode when the cross-family key is dark: three
  same-family adversaries with distinct attack surfaces, a stamped `cross_family: UNAVAILABLE`
  token, and automatic re-review when the key returns. The `solVerdictPath` key in the control
  receipts stays frozen.
- Fan-out cap: total agent processes at most two per physical core; a three-agent frontier swarm
  does not run concurrently with a high-effort Sol lane.
- Before scaling Sonnet dispatch, land HOOK-FALSE-POSITIVE-1: the project hook's shell-text rule
  denied ordinary diagnostic commands and cost 14 of 27 editing runs on 2026-09-07.
