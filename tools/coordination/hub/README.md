# tools/coordination/hub

Tracked home for the hub's board tools (HUB-TOOLS-TRACKED-1). They used to live only under the
gitignored `.claude-state\fleet-runs\hub-tools\`, so a worktree never saw them and a new project
could not install them. Every project-specific value is read from `hub-profile.json`; nothing
else in a tool names a project.

## Tracked so far (round 1)

| File | Role |
|---|---|
| `cos-open-ask-scan.ps1` | Reads the Chief-of-Staff feedback files on the bus ref and reports every open ACK ask (exit 0 none, 10 unanswered, 20 blind). |
| `Test-CosOpenAskScan.ps1` + `cos-scan-fixtures-bus/` | Its fixture-driven suite (`TESTS passed=<n> failed=0`). |
| `hub-profile.json`, `HubProfile.psm1` | The profile and its loader (`Get-HubProfile`). |

CI runs `tests/coordination/test_hub_cos_open_ask_scan.py`, which runs the suite and the profile rows.

**Not wired into the live board until the hub applies the cutover.** The live scanner path stays
a full copy until the hub swaps it for a forwarder to this tracked copy; nothing here is called by
the hub tick or `merge-enqueue.ps1` yet.

## hub-profile.v1

Every key is required and non-empty. A bad profile is `hub-profile-unreadable` (file absent, not valid
JSON, or a root that is not exactly one JSON object: a scalar or an array of any length, even `[ {profile} ]`) or `hub-profile-missing-key: <key>`; the scanner turns either into
`UNKNOWN pr=- reason=...` and exit 20.

| Key | Meaning |
|---|---|
| `schema` | `hub-profile.v1` |
| `project` | Short project name. |
| `boardRoot` | The board checkout (holds `.claude-state\`). |
| `repoSlug` | `owner/name` of the GitHub repo. |
| `repoAliases` | Former names of that repo; bus headers naming one are scanned normally. |
| `ownerLogin` | The account whose PR comments answer an ask (the hub posts under it). |
| `busRoot` | The doctrine bus clone. |
| `busRef` | The bus ref the feedback files are read from. |
| `cosFeedbackPath` | The bus folder that holds `pr-<N>.md`. |
| `cosOpenAsksOutFile` | The scan's markdown view, relative to `boardRoot`. |

A parameter passed on the command line wins over the profile. Bound vs unbound is decided by
`$PSBoundParameters`, so `-OutFile ''` (no file) is honoured.

## Still to track, in order

1. the publisher;
2. the merge gate, `merge-enqueue`, the stacked-parent helper and `draft-ready-scan`;
3. the reconciler, `hub-queue-lib` and `actions-lock-lib`;
4. `dispatch-lane` and the files it loads.

## Installing in another project

Copy this `hub/` folder into the project's `tools/coordination/`, then write that project's own
`hub-profile.json` (all ten keys). No tool needs editing.
