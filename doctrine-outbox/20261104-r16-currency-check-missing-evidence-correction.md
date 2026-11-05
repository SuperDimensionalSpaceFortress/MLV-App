---
target: RECEIPTS.md
kind: receipt
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-11-04 — Correction to the 2026-10-12 R16 receipt: the 72-hour currency check does not read CURRENT for every lane that ran; a lane with missing evidence reads NO-DATA or INCOMPLETE and exits 3

**Before:** the 2026-10-12 R16 receipt (bus `4555cb5`) says "The currency check over the last 72 hours of receipts reads CURRENT for every lane that ran." The shipped rule does not say that. Both review keys on the card's PR (#418, round 4) flagged that a lane whose newest run has a null `resolvedModel`, or whose runs recorded no effort, does not read CURRENT.
**What the check returns** (measured on `check-lane-model-currency.py` at MLV-App commit `c3cb2f3e`, three throwaway receipt sets, real clock, `--since-hours 72 --json`): the verdicts are STALE, NO-DATA, INCOMPLETE, IN-FLIGHT and CURRENT, and missing evidence is never CURRENT. CURRENT describes the historical model and effort evidence in the receipts, not a running process.
- A lane whose newest finished run failed with `resolvedModel` null reads NO-DATA. Beside a CURRENT fable lane the overall verdict is INCOMPLETE, exit 3.
- A lane with a proven model and a finished run that recorded no effort reads INCOMPLETE, exit 3. The one intentional exemption is a live-candidate running receipt, which has no effort yet by design.
- A lane whose only receipt is a running one reads NO-DATA, exit 3, unless that receipt is a live candidate.
- IN-FLIGHT is new: the lane's newest receipt is a live-candidate running receipt over a newest finished run that is proven and would read CURRENT. It counts as CURRENT overall. A live candidate is an age window, not a process probe: `state=running`, no end time, a finite `timeoutSec` of at most 48 hours, dated at most 300 seconds ahead of the clock, and no older than its timeout plus 300 seconds. The check reads receipts only, so a lane that died inside that window still reads IN-FLIGHT, and only over a finished run that is CURRENT anyway. A running receipt outside the window (expired-unproven or dated too far ahead) stays unproven.
- Exit codes: 0 overall CURRENT, 1 STALE, 2 unreadable runs directory, 3 INCOMPLETE or NO-DATA.

**Measured today:** a 24-hour run over the live receipts read INCOMPLETE, exit 3 (opus and sonnet NO-DATA). Opus held three running receipts and sonnet one, exactly the runs with no recorded effort. With the IN-FLIGHT rule they read IN-FLIGHT and the overall verdict CURRENT, which is about recorded evidence, not a running process.

**Falsifier:** a receipt with state `failed` and `resolvedModel` null as a lane's newest run beside a CURRENT lane must give overall INCOMPLETE, exit 3, never CURRENT. The 2026-10-12 sentence stays on the bus unedited; this entry supersedes it.
