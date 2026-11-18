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

## Known limits

* The slot-1 object is not keeper-owned during the window (`HeavySlot-KeepObjects` keeps slot 0 only), so every heavy-slot run in a
  count-2 admission prints a `slot object DACL/owner unexpected` warning for `Global\...-1` and logs `slot-object-unexpected`. It still runs.
* `held\*.json` can read "none" while a run holds the slot (the known limit recorded in TRAPS.md), so `holders` can undercount.
* One 15 s poll can lag a hung process: the driver is a plain loop with per-query timeouts, and a hung driver stays alive until the window
  end. Killing it ends the feature at once (the reader checks the pid).
* Adoption of two slots permanently also needs an owner-only elevated re-registration of `HeavySlot-KeepObjects` (INSTALL.md). Nothing here does that.
