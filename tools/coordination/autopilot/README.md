# tools/coordination/autopilot

Tracked, project-neutral home for the board autopilot scripts (card BOARD-AUTOPILOT-TRACKED-1).
Each script reads a declared board profile instead of MLV constants, so another project can run
the same loop by supplying its own `board-profile.json`.

## Tracked now (slice 1)

| File | What it is |
|---|---|
| `board-profile.json` | The MLV profile (schema below). |
| `BoardProfile.psm1` | `Get-BoardProfile [-Path]`: loads the profile, throws `BoardProfileMissingKeyException` (key absent or empty) or `BoardProfileUnreadableException` (file absent or not JSON). |
| `board-ff.ps1` | Fast-forward of the board checkout to `<remote>/<branch>`. Same behaviour as the live copy, plus a pinned target SHA (result field `Sha`) that the live copy lacks until the cutover. |

Tests: `tests/coordination/test_autopilot_board_ff.py` (throwaway local repos only).

## Fold cadence (DOCTRINE-FOLD-CADENCE-1)

| File | What it is |
|---|---|
| `fold-cadence.ps1` | Read-only verdict step. Reads the acked bus commit from a `last-seen.json` (`-LastSeenPath`), runs `doctrine_recall.py --fold-debt --since <acked sha>` under a hard deadline (`-DeadlineSec`, default 60), and returns/writes a `mlv-app/fold-cadence/v1` verdict (`owedCount`, `oldestOwedAgeHours`, `ackedSha`, `lastFoldUtc`, `dueFold`, `reason`, ...). `dueFold = owedCount > 0 AND UsageLevel == 'ok' AND no fold in the last 6 h` (`Get-FoldCadenceDecision`, callable by dot-sourcing). It queues nothing and writes nothing to the bus; the caller turns `dueFold` into a card-queue row. Every failure (timeout, missing or unreadable last-seen, recall failure) is a typed `reason` with `dueFold` false and exit code 0. |

Tests: `tests/coordination/test_autopilot_fold_cadence.py` (fake recall script; no real bus). Not wired into the live beat by this folder.

## Profile schema (`board-profile.v1`)

All keys are required; a string or array must be non-empty.

| Key | Meaning |
|---|---|
| `schema` | `board-profile.v1`. |
| `project` | Short project id; names the default scratch folder. |
| `boardRoot` | Absolute path of the canonical board checkout. |
| `remote`, `branch` | What the board fast-forwards to (`<remote>/<branch>`). |
| `productPaths` | Repo-relative product source roots (used by later slices). |
| `roadmap` | Repo-relative roadmap file (later slices). |
| `doctrineOutboxTarget` | Bus file the outbox files cards to (later slices). |
| `venues` | Venue names as they appear in tracked files (later slices). |
| `boardOwnedPaths` | Paths a fast-forward must not change; board-ff refuses and leaves them to the hook-receipt refresh. |

## Defaults and overrides

`board-ff.ps1` takes `Board`, `Remote`, `Branch` from the profile unless passed explicitly.
`StateFile` and `ScratchDir` never default to a tracked path: `ScratchDir` is
`$env:BOARD_FF_SCRATCH`, else `<temp>\board-ff-<project>`, and `StateFile` is
`<ScratchDir>\board-ff-state.json`.

## Still board-local (not yet tracked)

`Update-CardQueue`, `queue-drain`, `mechanical-keys` and the dispatch scripts still live under
the untracked `.claude-state\hub-autopilot\`, and the live autopilot still runs the copy there.
Nothing in this folder is wired into it.

## Cutover

Pointing the live autopilot at these tracked scripts is a later slice, with its own re-pin of
the hook receipt. Until then the two copies are kept equal by hand; the parity table is in the
slice-1 PR.
