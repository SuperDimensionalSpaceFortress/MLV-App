# Product-priority dispatch admission

`tools/coordination/Test-ProductRatioGuard.ps1` resolves one source commit and
reports an inclusive seven-day window using Git committer timestamps. Its
product share counts all reachable non-merge commits once. A commit counts as
product work when its diff touches `src/` or `platform/`; a mixed commit still
counts only once. Future-dated and older commits are excluded.

The dispatch-rate denominator is separate: unique recognized GitHub PR landings
on first-parent history whose net diff from the first parent touches product
paths. Merge subjects and squash subjects identify PR numbers. A batch counts
once. Product landings without recognized PR provenance make the rate
unavailable instead of substituting a commit count.

Reservations are the preferred observation source. The legacy dispatch log is
used only when the reservations path is absent. An existing unreadable path
produces unavailable evidence; it cannot trigger fallback. Empty reservations
remain authoritative for observed rows. Malformed rows are counted separately.
Unavailable observations produce a null dispatch rate even when a recognized
PR denominator exists. The caller rejects a numeric rate in that case; a
readable empty observation file still produces a valid zero rate.
The legacy log never proves complete accounting.

## Version-enforced all-venue accounting

Every launch venue writes a ledger row with `schemaVersion` 2:
- `Invoke-Workstream.ps1` writes `reserved`, then `charged` or `refunded`.
- `Invoke-Lane.ps1` writes its row after taking its receipt slot and before any
  provider starts, naming the receipt in `receiptPath`. A direct launch writes
  `reserved`. When Invoke-Workstream passes its reservation id through
  `MLV_DISPATCH_RESERVATION_ID` (which also crosses `Start-EditingLane.ps1`),
  the lane writes `linked` instead.
- If a lane cannot write its row, it refuses to launch.
- Both launchers append under one machine-wide mutex,
  `Global\MLV-App-DispatchLedger`. `FileMode.Append` records end-of-file per
  stream, so unserialized concurrent appends could overwrite each other's rows.

Only `reserved` rows count toward the dispatch rate, so a dispatcher launch counts
once. The rate numerator is unchanged: every reserved launch counts, whatever its
kind or `allowEdits`.

A `linked` row is honoured only when it names an `invoke-workstream`
`reserved` row written no later than itself, and only once per reservation.
Any other `linked` row counts as a launch of its own and adds
`COVERAGE_LINK_UNMATCHED`, so claiming someone else's reservation cannot hide a
launch from the rate.

Without `-RunDir`, `Invoke-Lane.ps1` writes its receipt under the board's
`.claude-state/fleet-runs`.

Rows with venue `invoke-lane` exist only for this coverage accounting.
`Invoke-WorkstreamLoop.ps1` skips them when it computes its daily dispatch
budget, so hub reviews and swarms never spend the unattended loop's budget.

Coverage is `COMPLETE` only when every check below holds. Otherwise it is
`PARTIAL`, and each failed check adds its reason code after
`RED_DISPATCH_COVERAGE_PARTIAL`:

| Check | Reason code when it fails |
|---|---|
| The reservations ledger is the source | `COVERAGE_LEGACY_SOURCE` |
| The ledger writer has landed on the source ref, and a versioned row was written at or after that landing | `COVERAGE_NOT_ENFORCED` |
| The first such row is at or before the window start | `COVERAGE_WINDOW_PREDATES_ENFORCEMENT` |
| No unversioned row falls in the window after enforcement began | `COVERAGE_UNVERSIONED_ROW_AFTER_ENFORCEMENT` |
| Every `linked` row is honoured | `COVERAGE_LINK_UNMATCHED` |
| Every `mlv-app/fleet-lane-receipt/v1` receipt that started in the window is named by a ledger row. The guard reads receipts under the board's `.claude-state/fleet-runs` and under that path in every registered worktree, because an older runner keeps receipts in its own worktree. Each receipt is judged by its `startedUtc`, never its file time. | `COVERAGE_RECEIPT_UNRESERVED` |
| Every in-window versioned row naming a receipt under those trees still has that receipt | `COVERAGE_RESERVATION_RECEIPT_MISSING` |
| In every registered checkout (the main worktree included), both ledger writers, `Invoke-Lane.ps1` and `Invoke-Workstream.ps1`, carry the token `MLV-DISPATCH-LEDGER-WRITER-V3-SERIALIZED`. The function name alone is not enough, because an earlier writer had it without the mutex. `-RunDir` can point anywhere, so an older runner's receipt may never be scanned, but the runner itself can be. The same token marks when enforcement landed. | `COVERAGE_STALE_RUNNER_PRESENT` |
| No registered checkout's writer file was replaced inside the window, since it may have been an older runner earlier in the window | `COVERAGE_RUNNER_UPDATED_IN_WINDOW` |
| Every receipt can be read | `COVERAGE_RECEIPT_UNREADABLE` or `COVERAGE_RECEIPT_IN_FLIGHT` |
| The receipt tree can be read | `COVERAGE_RECEIPTS_UNAVAILABLE` |

An unreadable receipt, an empty receipt slot, or an unreadable tree makes
coverage cannot-determine, and cannot-determine is never `COMPLETE`. A receipt
whose failure is `dispatch-ledger-write-failed` records a refused launch, so it
does not count.

The landing is the committer time of the oldest first-parent commit from which
`tools/coordination/Invoke-Lane.ps1` on the source ref continuously carries the
serialized-writer token. Rows written earlier, for example by an unmerged candidate, never
start the clock.

Coverage therefore cannot be `COMPLETE` until seven days after the writer lands
and every venue runs it. That wait is the ratified seven-day enforcement window,
not a defect.

Two limits remain:
- An older runner outside every registered worktree is outside the guard's view.
  That covers a plain copy of the repository, and a worktree that was removed
  during the window.
- Merging this change leaves the `execution-control-*.json` chain uncertified.
  That chain was already stale before this change, and certifying a re-enable is
  separate work.

A row outside the window is never counted as malformed. Rows with no
`schemaVersion` are legacy: they still count toward the rate, but they never
prove coverage.

Interactive sessions that author commits themselves are not a launch venue.
Their commits are counted by the product share, not by the ledger.

At both actual launch points, `Invoke-Workstream.ps1` checks the guard after the
kill switch and before recording a reservation or launching a provider. Valid
`RED` permits only explicitly typed `product` and `playback` cards. Other or
missing kinds return 6. Unresolved Git history, malformed output, inconsistent
counts or invalid JSON types return 3 and refuse every kind. An empty valid
window is `RED` with an unavailable product share. Dry runs prepare evidence but
do not reserve or launch a provider. The loop records refused cards as skipped
and continues to the next track.

## Booking rule: a kind label must match the card's paths

Under `RED`, the dispatcher admits a card by its `kind` label, while the product
share counts a landing only when its diff touches `src/` or `platform/`. A
`product` or `playback` card whose paths touch neither passes the gate but can
never raise the share. That lets factory work through under a product label.
Five landed cards had this mismatch.

So the label is fixed where it is written, not at dispatch:
- A card may be booked `product` or `playback` only when its `ALLOWED_PATHS`
  (or `scope`) names a path under `src/` or `platform/`.
- Anything else is booked `factory` or `gate`, and names the product PR it
  unblocks in `unblocksProductPr`.
- An `ALLOWED_PATHS` written as prose lists the concrete `src/` and `platform/`
  paths it will touch.

No dispatch-time path check was added. It would narrow the ratified admission
rule above, the composer does not expose parsed fields at the gate, and it would
refuse the only queued product card (PROD-UPSTREAM-SYNC-1, whose prose paths
cover `src/` and `platform/`).

Re-open a dispatch-time check if a new product-kind landing is missing from the
guard's `recognizedProductPrIds`. The adjudication record is
`fleet-runs/swarm-kind-path-20260916T1615Z/SYNTHESIS.md`.

`GREEN` requires all of the following:
- product share at least 0.50
- dispatch rate at most 4.0
- complete coverage
- available observations
- complete landing provenance
- no malformed rows

Tests exercise that decision through the real reader, over a disposable ledger
and receipt tree.

`GREEN` lifts the dispatcher's kind gate, as shipped. It does not thaw cards in
the one-way `frozen-factory-20260906` state. That unfreeze operation still does
not exist. Before it is implemented, seven days of `COMPLETE` version-enforced
accounting and an authoritative `GREEN` reading are required, and Phase 3 then
thaws one card per week. This change adds no factory-unfreeze authority. It
builds only the accounting that precondition needs. The adjudication record is
`fleet-runs/swarm-guard-unfreeze-20260916T1325Z/SYNTHESIS.md`.

Validation uses disposable Git histories, controlled timestamp boundaries,
actual PowerShell guard and dispatcher executions with fake providers, and the
actual loop body extracted by PowerShell's parser. Fixture providers never edit
the live queue or call an external model. Provider exit zero continues to mean
process completion only; reviewed delivery and loop activation require their
separate receipts.
