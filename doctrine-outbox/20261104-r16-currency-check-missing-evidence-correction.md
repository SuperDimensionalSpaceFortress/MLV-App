---
target: RECEIPTS.md
kind: receipt
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-11-04 — Correction to the 2026-10-12 R16 receipt: a lane with missing evidence reads NO-DATA or INCOMPLETE, exit 3, not CURRENT

**Before:** the 2026-10-12 R16 receipt (bus `4555cb5`) says "The currency check over the last 72 hours of receipts reads CURRENT for every lane that ran." Both review keys on PR #418 flagged that a lane whose newest run has a null `resolvedModel`, or whose runs recorded no effort, does not read CURRENT.
**What the check returns** (pinned by the tests at MLV-App commit `21397fae`, through `main --json`): the verdicts are STALE, NO-DATA, INCOMPLETE, IN-FLIGHT and CURRENT. A STALE-MODEL or BAD-EFFORT reason outranks every other verdict and reads STALE, exit 1. Otherwise missing evidence is never CURRENT, except one hub-ruled exception: a `startedUtc` is read only as `YYYY-MM-DDTHH:MM:SS`, optional 1 to 9 fraction digits, then `Z` or `±HH:MM` (hour 00-23, minute 00-59). A receipt of any state, a newest failed run included, with any other stamp is dropped and counted in `unreadableReceipts`. That can hide a violation, not only a gap: a complete effort-low run with a foreign stamp beside a current run reads CURRENT, exit 0. Each row below holds unless a dropped stamp removed the receipt it names.
- Newest finished run failed with `resolvedModel` null: NO-DATA. Beside a CURRENT fable lane the overall verdict is INCOMPLETE, exit 3.
- Proven model and a finished run that recorded no effort: INCOMPLETE, exit 3, unless the newest run is a failed null-model run (NO-DATA). A live-candidate running receipt is exempt.
- A running receipt is never a proven run. Only running receipts, or an expired running one as the newest non-live receipt: NO-DATA, exit 3.
- IN-FLIGHT: the newest receipt is a live-candidate running receipt over a newest finished run that is proven and would read CURRENT; it counts as CURRENT overall. A live candidate is an age window, not a process probe: `state=running`, no end time, a positive `timeoutSec` of at most 48 hours and 15 significant digits, a `startedUtc` at most 300 seconds ahead of the clock, and no older than its timeout plus 300 seconds.
- Exit codes: 0 overall CURRENT, 1 STALE, 2 unreadable runs directory, 3 INCOMPLETE or NO-DATA.

**Measured today:** a 24-hour run over live receipts read INCOMPLETE, exit 3; with IN-FLIGHT, overall CURRENT.

**Falsifier:** a `failed` receipt with `resolvedModel` null and a readable `startedUtc` as a lane's newest run beside a CURRENT lane, and no STALE reason, must give overall INCOMPLETE, exit 3, never CURRENT. With a STALE reason (effort other than high, or two stale model runs) it gives STALE, exit 1. The 2026-10-12 sentence stays on the bus unedited; this entry supersedes it.
