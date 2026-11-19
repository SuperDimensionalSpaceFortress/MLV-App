# heavy-slot-ab: a bounded A/B window for a second heavy-slot, pytest side only

Card HEAVY-SLOT-SECOND-SLOT-MEASURED-1. PC-MINI3 runs one heavy build slot while CPU p95 is 27 %, nine physical cores are free at
p90 and 64 GB RAM is available, but thermals are tight (>= 90 C for 9.9 % of the time). This folder measures what a second slot would
do before anyone adopts it.

## What the A/B changes, and what it never changes

* **Builds stay at count 1.** `tools/coordination/build-guard.ps1` strips `HEAVY_SLOT_COUNT` from every build's heavy-slot call and
  accepts only a slot-0-of-1 inherited holder, and MLV builds also hold a one-slot MLV build lock. Two builds at once is out of scope
  (a later card that widens that lock).
* **In B blocks the fast-suites pytest ticket runs with `HEAVY_SLOT_COUNT=2`.** `hub-tools\fast-suites-for-diff.ps1` does not strip the
  variable, so it inherits it. Possible overlaps: build + pytest, pytest + pytest. Never build + build.
* heavy-slot itself (`C:\ProgramData\HeavySlot\bin`, machine-owned, owner-only install) is never edited, installed or probed. This
  folder reads `C:\ProgramData\HeavySlot\{ledger.jsonl, receipts\, held\}` read-only. No file here calls heavy-slot's reporting verb
  (it sweeps live waiters), opens a slot mutex, or waits on one. A test pins that.

## Files

| File | Role |
|---|---|
| `HeavySlotAb.psm1` | `Get-HeavySlotAbCount -SwitchPath <p> [-Now]`: 2 only for a live, in-window, schema-valid B switch; anything else 1. The block between the `HEAVY_SLOT_AB_READER v1` markers is inlined verbatim into the staged fast-suites patch. |
| `Invoke-HeavySlotAbDriver.ps1` | The ONLY writer of the switch file (default `C:\mlvtmp\locks\heavy-slot-ab.json`). Preflight, alternating 3 h blocks A,B,A,B... (max 25 h), 15 s samples, hard aborts, guaranteed revert. |
| `Measure-HeavySlotAb.ps1` | Read-only evaluator: writes `ab-verdict.json` (+ `ab-headroom-summary.json`). |

## The switch file

`{"schema":"heavy-slot-ab.v1","count":2,"state":"B","windowStartUtc":...,"endUtc":...,"driverPid":...,"driverStartTicks":...}`.
The reader returns 2 only when the schema matches, `count == 2`, `state == 'B'`, `now < endUtc`, `endUtc - windowStartUtc <= 25 h`, and
`driverPid` is alive with start ticks equal to `driverStartTicks` (`Get-Process -Id`, never a process census). A killed driver therefore
switches the feature off at once, whatever the file still says. A missing, unreadable or malformed file is count 1. Revert is a
**rewrite** to `REVERTED`/count 1; nothing here removes the file.

## Driver

Preflight (any failure ends `AB_REFUSED`, exit 2, and no switch is left at count 2; a refusal writes nothing else): a named mutex shows
no other driver is running; an existing switch file is reverted; the heavy-slot.ps1 SHA-256 equals the build-guard pin; fast-suites
carries the marker line `HEAVY_SLOT_AB_READER v1`; the CoreTemp feed is under 120 s old; free commit >= 16 GB; C: free >= 40 GiB.

Outputs in `-WindowDir`: `ab-blocks.jsonl` (one row per block edge), `ab-samples.jsonl` (one row per poll: block, state, live holders with
labels and slots, newest CoreTemp temp / cpu / package W, free commit and physical, processor performance and frequency, C: free),
`ab-receipt.json` (`"state":"DONE"` or `"state":"ABORTED"` plus the reason), and the stop file `ab-stop`.

Hard aborts (each rewrites the switch to REVERTED, writes the ABORTED receipt, exit 3; thresholds are parameters, defaults shown):

| Reason | Default |
|---|---|
| `thermal-cap` | temp >= 95 C for >= 60 s cumulative in any 5 min |
| `warm-while-2` | time with temp >= 90 C and 2+ live holders > 50 % of a rolling 15 min |
| `stale-feed` | newest CoreTemp row older than 180 s |
| `low-memory` | free commit < 10 GB or free physical < 6 GB on 2 consecutive samples |
| `event-2004` | System Event 2004 since window start (checked every 5 min) |
| `low-disk` | C: free < 40 GiB |
| `unguarded` | a ledger `unguarded` event since window start |
| `exclusive-admit` | an admit, held record or receipt with class != shared or wholeMachine = true |
| `stop-file` | `<WindowDir>\ab-stop` exists |

The thermal rules weigh each CoreTemp row by the time to the previous row (capped at 6 s), so they use every 2 s feed row, not only the
15 s poll. "warm-while-2" is read as the fraction of the rolling 15 min that is BOTH hot and at 2+ holders (a fixed 15 min denominator),
so a short overlap cannot trip it on one hot row.

## Evaluator

`Measure-HeavySlotAb.ps1 -WindowDir <dir>`. Hub ruling 2026-11-18T00:10:38Z, both conditions: (a) **dead holders are excluded** from the
wait comparison and counted (an admit with no exit / tool-exit row by window end + 90 min, or a `reaped kind=held` row; before the
grace they are `pending`); (b) wait, frequency and temperature are reported **split by holders = 1 and holders = 2**.
Each ticket belongs to the block it enqueued in; a wait that crosses a block edge is `spill` and left out. Reported for A and B, overall
and per class (label suffix `build` / `pytest`): n, wait_s p50/p90/p99 (nearest-rank), hold_s p50.

Verdict (every threshold is printed in the json):

* `INCONCLUSIVE`: no finished receipt; < 20 live closed tickets or < 10 builds in either arm; < 30 min of holders >= 2; A never queued;
  no under-load frequency sample.
* `ADOPT` only if all hold: B wait_p90 <= 0.75 x A; B build wait_p90 <= 1.10 x A; B build hold_p50 <= 1.15 x A; at holders >= 2
  temp_ge90_frac <= 0.20 and temp_c_p95 <= 94; at holders >= 2 under load (cpu >= 50 %) freq_p50 >= 0.90 x the holders = 1 figure; no
  Event 2004; min free commit in B samples >= 10 GB. Otherwise `REJECT`.
* `ABORTED` is carried through from the driver receipt.

## Rules the adversary pass added (r1)

* **One window per directory.** The driver refuses a `-WindowDir` that already holds `ab-blocks.jsonl`, `ab-samples.jsonl` or `ab-receipt.json`
  (`window-dir-used`); the evaluator calls a directory with two windows, or a receipt that names another window, INCONCLUSIVE and ignores
  samples outside the window.
* **Exit codes.** Driver 0 DONE, 2 REFUSED, 3 ABORTED, 4 the final revert could not be proven (receipt `switchFinal: REVERT_FAILED`; the reader
  still answers 1 once the driver process is gone). The revert is tried 5 times and read back.
* **Holders are exact.** A held record counts only when its pid is alive with exactly the recorded start ticks (as heavy-slot itself compares). A feed row
  between two polls counts as 2+ holders only when both bracketing polls saw 2+, so a second holder seen at alternate polls does not trip `warm-while-2`.
* **Receipts.** New receipt files are read whatever time their name carries, and a half-written one is retried, not marked seen.
* **Event 2004.** A count that cannot be read is never zero: two unreadable checks abort the window (`event-2004-unreadable`); the evaluator is INCONCLUSIVE.
* **Per hold.** The evaluator writes `ab-holds.jsonl` (one row per live closed hold: wait, hold, peak live holders, hottest and mean temp, median frequency)
  and `holds_by_holders` in the verdict (holders_1 / holders_2_plus / holders_unknown, per arm and overall). An exit that landed after window end + grace is a dead holder.

## Rules the adversary pass added (r2)

* **One driver per switch.** Besides the named mutex, the driver holds an exclusive lock file `<SwitchPath>.lock` for its whole life (never removed), so a second
  driver with another `-MutexName` but the same switch is refused. A refusal leaves at most that empty lock file.
* **Read-only means read-only.** The switch, its lock and the window dir may not be, or lie under, the HeavySlot dir or any input the driver only reads (`path-overlap`);
  the evaluator refuses a window dir under the HeavySlot dir (`AB_EVAL_REFUSED`).
* **Preflight failures are refusals.** A locked or unreadable input is `preflight-failed` (exit 2), never an abort; a refusal whose revert cannot be proven exits 4.
* **No gap at the edges.** The ledger and receipt baselines are taken before the window clock starts, and one last sweep runs after the final poll.
* **The blocks log is what was applied.** Each tick applies the block due NOW by the clock (a slow poll skips blocks); a row's `startUtc` is the moment the state was applied
  and the evaluator attributes by those moments (a block lasts until the next one was applied).
* **Evaluator hygiene.** Block rows need an integer `block`, state A or B and parseable times, else `AB_EVAL_NO_INPUT` (exit 2); unparseable sample fields read as unknown;
  any other error is `AB_EVAL_ERROR` (exit 3); a receipt that closed after window end + grace is a dead holder even without a ledger admit row;
  each sample stands for at most the time left to window end.
* **Reader.** A switch whose window has not started is count 1 (a clock stepped back); the file is read with delete sharing.

## Rules added in r4 (CI run 44251460414, adversary pass 3, Dot advisory)

* **The rewrite waits out a reader.** A switch rewrite retries against a time budget (`-SwitchRetrySeconds`, default 10 s, jittered 10-40 ms pauses), not 10 fixed
  tries: a reader that keeps the file open for seconds on a loaded host no longer ends the window with `driver-exception ... Access to the path is denied`.
* **One window dir, claimed atomically.** The driver takes an exclusive `ab-window.lock` in the window dir (held, never removed, so a used dir stays used).
  Two drivers that both passed the unused-dir check cannot both run: the loser is refused `window-dir-used`.
* **Evidence fails closed.** A ledger that cannot be read on 2 polls in a row or in the final sweep (4 tries) aborts `ledger-unreadable`; a held record that cannot
  be parsed on 2 polls aborts `held-unreadable`; a receipt that never parses aborts `receipt-unreadable` (never skipped). The evaluator is INCONCLUSIVE for a B
  sample with no free-commit reading and for a receipt written during the window that cannot be read; every unparseable input line is counted in `input_problems`.
* **No receipt, no DONE.** A final receipt that cannot be written (3 tries) prints `AB_RECEIPT_FAILED` and exits 5; `AB_DONE` is never printed.
* **Compare before rounding.** Free disk and memory in the driver, and holders>=2 minutes, the >=90 C fraction and the percentiles in the evaluator are compared
  exactly; only printed text is rounded.
* **Aliases.** The containment checks of both tools resolve junctions, symlinks and 8.3 names to the real path first (`Resolve-HeavySlotAbRealPath`).
* **Block edges.** The blocks log `startUtc` is taken before the switch write (`switchWrittenUtc` is when the write returned); a `reaped kind=held` row excludes its
  receipt even with no admit row; a never-admitted wait cut off in another block is spill; a holders value that is not a finite whole number 0..1000 is a skip.
* **Telemetry.** A feed row dated later than now + 30 s is invalid: a feed with only such rows is refused `feed-future`, and a frozen future row ages into `stale-feed`.
* **Adoption** also needs at least 10 B pytest tickets whose receipt shows slotCount 2 (`thresholds.min_b_pytest_at_slotcount_2`).
* **Open, outside this card:** `tools/coordination/build-guard.ps1:446-447` still compares the rounded memory figure (BUILD-GUARD-MEMORY-FLOOR-COMPARE-UNROUNDED-1).

## Rules added in r9 (adversary review of r8, five blockers)

* **No observed interval, no overlap credit.** A feed row is credited with 2+ holders only inside the interval two bracketing polls observed
  (`Get-HeavySlotAbRowHolders`): the FIRST read has no previous poll and credits nothing, so the feed's pre-window tail (for example 15 minutes of old 91 C
  rows) can never abort a legitimate window as `warm-while-2`.
* **A replaced feed fails closed.** A read that carries a row at or below the accepted watermark that this stream never delivered (a replaced or truncated
  feed hiding a hot row under the watermark) is refused `feed-time-regressed`; the identities of every accepted row are kept (`Get-HeavySlotAbFeedRowKey`).
* **No retry starts past its deadline.** After the jittered sleep the clock is read again; an attempt that would START after the shared `-SwitchRetrySeconds`
  budget is not made, whatever delayed the thread.
* **No receipt, no DONE, even when the window directory vanished.** The receipt is owed by the outcome plus the switch touch (`Test-HeavySlotAbReceiptOwed`),
  not by the directory still existing: the write fails and the driver exits 5 `AB_RECEIPT_FAILED`.
* **A diagnostic never loses evidence.** The unknown-evidence text is length-bounded on the normalized value (64 spaces normalize to one character), and a
  ledger read that throws while judging puts the offset back, so the row is read again and two failures in a row abort `ledger-unreadable`.

## Known limits

* The slot-1 object is not keeper-owned during the window (`HeavySlot-KeepObjects` keeps slot 0 only), so every heavy-slot run in a
  count-2 admission prints a `slot object DACL/owner unexpected` warning for `Global\...-1` and logs `slot-object-unexpected`. It still runs.
* `held\*.json` can read "none" while a run holds the slot (the known limit recorded in TRAPS.md), so `holders` can undercount.
* One 15 s poll can lag a hung process: the driver is a plain loop with per-query timeouts, and a hung driver stays alive until the window
  end. Killing it ends the feature at once (the reader checks the pid).
* Adoption of two slots permanently also needs an owner-only elevated re-registration of `HeavySlot-KeepObjects` (INSTALL.md). Nothing here does that.
