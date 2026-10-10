# tools/coordination/autopilot

Tracked, project-neutral home for the board autopilot scripts (card BOARD-AUTOPILOT-TRACKED-1).
Each script reads a declared board profile instead of MLV constants, so another project can run
the same loop by supplying its own `board-profile.json`.

## Tracked now (slice 1)

| File | What it is |
|---|---|
| `board-profile.json` | The MLV profile (schema below). |
| `BoardProfile.psm1` | `Get-BoardProfile [-Path]`: loads the profile, throws `BoardProfileMissingKeyException` (key absent or empty) or `BoardProfileUnreadableException` (file absent or not JSON). |
| `board-ff.ps1` | Fast-forward of the board checkout to `<remote>/<branch>`. Same behaviour and result object as the live copy. |

Tests: `tests/coordination/test_autopilot_board_ff.py` (throwaway local repos only).

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
