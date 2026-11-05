---
target: RECEIPTS.md
kind: receipt
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-11-04 — Correction to the 2026-10-12 R16 receipt: the 72-hour currency check does not read CURRENT for every lane that ran; a lane with missing evidence reads NO-DATA or INCOMPLETE and exits 3

**Before:** the 2026-10-12 R16 receipt (bus `4555cb5`) says "The currency check over the last 72 hours of receipts reads CURRENT for every lane that ran." Both review keys on PR #418 flagged that a lane whose newest run has a null `resolvedModel`, or whose runs recorded no effort, does not read CURRENT.
**What the check returns** (as pinned by the tests at MLV-App commit `75cd4479`, through `main --json`, clock fixed): the verdicts are STALE, NO-DATA, INCOMPLETE, IN-FLIGHT and CURRENT, and missing evidence is never CURRENT, except one hub-ruled exception. A `startedUtc` is read only as `YYYY-MM-DDTHH:MM:SS`, optional 1 to 9 fraction digits, then `Z` or an HH:MM offset; any other stamp is unreadable, so a newest failed run with one is dropped and counted in `unreadableReceipts`. CURRENT describes historical evidence in the receipts, not a running process.
- A lane whose newest finished run failed with `resolvedModel` null reads NO-DATA. Beside a CURRENT fable lane the overall verdict is INCOMPLETE, exit 3.
- A lane with a proven model and a finished run that recorded no effort reads INCOMPLETE, exit 3. The exemption: a live-candidate running receipt, which has no effort yet.
- A running receipt is never a proven run, live or expired. A lane with only running receipts reads NO-DATA, exit 3, as does a lane whose newest non-live receipt is an expired running one, unless a STALE-MODEL or BAD-EFFORT reason outranks it: that reads STALE, exit 1.
- IN-FLIGHT: the lane's newest receipt is a live-candidate running receipt over a newest finished run that is proven and would read CURRENT; it counts as CURRENT overall. A live candidate is an age window, not a process probe: `state=running`, no end time, a `timeoutSec` of at most 48 hours and 15 significant digits, a readable `startedUtc` at most 300 seconds ahead of the clock, and no older than its timeout plus 300 seconds. A lane that died inside the window still reads IN-FLIGHT.
- Exit codes: 0 overall CURRENT, 1 STALE, 2 unreadable runs directory, 3 INCOMPLETE or NO-DATA.

**Measured today:** a 24-hour run over live receipts read INCOMPLETE, exit 3 (opus and sonnet NO-DATA, holding running receipts with no effort); with the IN-FLIGHT rule they read IN-FLIGHT, overall CURRENT.

**Falsifier:** a receipt with state `failed`, `resolvedModel` null and a readable `startedUtc` as a lane's newest run beside a CURRENT lane must give overall INCOMPLETE, exit 3, never CURRENT. The 2026-10-12 sentence stays on the bus unedited; this entry supersedes it.
