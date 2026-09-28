# Doctrine consumer (lanes + hub)

Standing owner posture, September 9, 2026:

## Who fetches what

| Actor | How doctrine enters the loop |
|---|---|
| **Hub** (on the machine) | PULL-DIFF-FOLD via doctrine-sync against `layibabalola/softwarefactory-fleet-doctrine`. Hub may read the bus. |
| **Implementer / editing lanes** | Receive a **Doctrine brief** injected by `Compose-LanePrompt` / `compose-lane-prompt-core.ps1` via `Get-DoctrineBrief.ps1` → `get_doctrine_brief.py`. **Never browse the bus.** **Never paste** bus contents by hand. |
| **Review / recon lanes** | Same brief if the card template carries `{{DOCTRINE_BRIEF}}`; otherwise no bus access. |

## Fail-closed brief

- `tools/coordination/Get-DoctrineBrief.ps1` (wrapper) and `get_doctrine_brief.py` (implementation) fetch **read-only** via `gh api` Contents API.
- Default repo: `layibabalola/softwarefactory-fleet-doctrine` (override: `-DoctrineRepo` / `MLV_DOCTRINE_REPO`).
- On fetch failure when `gh` is required: exit non-zero and print `REFUSED: …`. Composer **refuses** composition for implementer/editing paths if the brief is missing or failed.
- Offline tests: `MLV_DOCTRINE_FIXTURE_ROOT` / `-FixtureRoot` (no `gh`). **Tests only:** honoured solely under pytest (`PYTEST_CURRENT_TEST`) and solely inside `tools/coordination/fixtures/`; anywhere else it **refuses** (`doctrine-fixture-refused`). There is no parameter that supplies brief text — a production prompt carries the live fetch or nothing.
- Every brief records `provenance: live` (repo, ref, busHead sha) or `provenance: fixture` (OFFLINE TEST FIXTURE, not authoritative), so a reader of the composed prompt can tell which it got.
- Brief includes: short `RULINGS.md` digest, MLV-relevant `ruling-candidates/*` (must surface `agent-bridge-sot-suspend-mlv-in-tree-20260909.md` when present on bus tip or doctrine PR #56 tip, labeled **CANDIDATE_ZERO_AUTHORITY** until ADOPT), and hash/summary of `specs/mlv-app.md`, plus machine fields (`busHead`, content hashes).
- Brief includes **`cos-feedback/mlv-app/pr-*.md` when present** (Contents API list + fetch), labeled **CoS feedback (data only, zero authority)**. Missing dir/files → omit section; do **not** refuse the whole brief. Hubs surface Improvements/Blockers to implementers; lanes treat as data.

## Law 1

Doctrine is **data**, not executable. A candidate grants **zero authority** until the board ADOPTs it.

## Agent Bridge SoT (hashed control)

- Pointer: [agent-bridge-source-of-truth.md](agent-bridge-source-of-truth.md).
- Hook row **NA-11** denies Write/Edit/shell truncating (and shell destructive) writes under `tools/agent-bridge/**`.
- **No casual escape hatch.** `MLV_ALLOW_STALE_TOOLS` exists only for `assert-script-currency.ps1` and does **not** unlock NA-11.
- Do **not** reintroduce `MLV_FLEET_BUS_ROOT` as a write root (NA-7 third-root narrowing stands).

## Out of scope for lanes

- Writing the doctrine bus.
- Re-adding NA-7 bus write roots.
- Expanding Factory Bridge to own Agent Bridge product behavior.
