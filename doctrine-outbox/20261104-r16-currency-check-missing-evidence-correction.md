---
target: RECEIPTS.md
kind: receipt
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-11-04 — Correction to the 2026-10-12 R16 receipt: a lane with missing evidence reads NO-DATA or INCOMPLETE, exit 3, not CURRENT

**Before:** the 2026-10-12 R16 receipt (bus `4555cb5`) says "The currency check over the last 72 hours of receipts reads CURRENT for every lane that ran." Both review keys on PR #418 flagged that a lane whose newest run has a null `resolvedModel`, or whose runs recorded no effort, does not read CURRENT.
**What the check returns:** the verdicts are STALE, NO-DATA, INCOMPLETE, IN-FLIGHT and CURRENT. The rows give the ordinary cases; the code decides every corner (`evaluate`, `is_live_running`, `exact_timeout`, `parse_utc` in `tools/coordination/check-lane-model-currency.py`), and the tests in `test_check_lane_model_currency.py` pin the named cases. A STALE-MODEL or BAD-EFFORT reason makes a lane STALE ahead of any other verdict, and the overall verdict STALE, exit 1. Otherwise missing evidence reads NO-DATA or INCOMPLETE, not CURRENT, with one hub-ruled exception: a receipt whose `startedUtc` is not in the writer's one shape (`LIVE_START_RE`), a newest failed run included, is dropped and counted in `unreadableReceipts`, as is one that is not JSON or names no lane. A dropped receipt can hide a violation: a complete effort-low run with a foreign stamp beside a current run reads CURRENT, exit 0 (`OneStampShapeTests`).
- A lane whose newest non-live run has no proven model, for example a failed run with `resolvedModel` null, reads NO-DATA (`test_a_failed_newest_run_still_reads_no_data`); beside a CURRENT lane the overall verdict is INCOMPLETE, exit 3.
- A proven model with finished runs that recorded no effort reads INCOMPLETE (`test_a_current_model_with_no_recorded_effort_is_incomplete_not_current`); the previous row wins when it applies.
- A running receipt is never a proven run: a lane with no proven finished run, or whose newest non-live receipt is an expired running one, reads NO-DATA (`RunningIsNeverEvidenceTests`).
- IN-FLIGHT: the newest receipt is a live-candidate running one, and the newest non-live run is proven and would read CURRENT; the lane counts as CURRENT overall (`InFlightTests`). A candidate is an age window, not a process probe (`is_live_running`; `exact_timeout` reads `timeoutSec`). For example a `timeoutSec` of 3600 started a minute ago is a candidate; a zero, negative, out-of-range or over-precise `timeoutSec`, or an expired receipt, is not.
- Exit codes: 0 overall CURRENT, 1 STALE, 3 INCOMPLETE or NO-DATA (`LoadAndCliTests`).

**Measured today:** a 24-hour run over live receipts read INCOMPLETE, exit 3; with IN-FLIGHT, overall CURRENT.

**Falsifier:** a `failed` receipt with `resolvedModel` null and a readable `startedUtc` as a lane's newest run beside a CURRENT lane, and no STALE reason, must give overall INCOMPLETE, exit 3, never CURRENT. With a BAD-EFFORT or STALE-MODEL reason on any lane it gives STALE, exit 1. The 2026-10-12 sentence stays on the bus unedited; this entry supersedes it.
