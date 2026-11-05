---
target: RECEIPTS.md
kind: receipt
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-11-04 — Correction to the 2026-10-12 R16 receipt: the 72-hour currency check does not read CURRENT for every lane that ran; a lane with missing evidence reads NO-DATA or INCOMPLETE and exits 3

**Before:** the 2026-10-12 R16 receipt (bus `4555cb5`) says "The currency check over the last 72 hours of receipts reads CURRENT for every lane that ran." The shipped rule does not say that. Both review keys on the card's PR (#418, round 4) flagged that a lane whose newest run has a null `resolvedModel`, or whose runs recorded no effort, does not read CURRENT. Nothing ran the check on a schedule, so the sentence was never tested against a busy day.

**What the check returns for a lane with missing evidence** (measured on `check-lane-model-currency.py` at MLV-App commit `5e5e6c9f`, three throwaway receipt sets, real clock, `--since-hours 72 --json`): the verdict vocabulary is STALE, NO-DATA, INCOMPLETE, IN-FLIGHT and CURRENT, and missing evidence is never CURRENT.
- A lane whose newest finished run failed with `resolvedModel` null reads NO-DATA. With a CURRENT fable lane beside it the overall verdict is INCOMPLETE and the exit code is 3.
- A lane with a proven model and a run that recorded no effort reads INCOMPLETE, exit 3.
- A lane whose only receipt is a run still in progress reads NO-DATA (a running receipt holds no model or effort yet), exit 3.
- IN-FLIGHT is new in this change: a lane whose newest receipt is a provably live running run, over a newest finished run that is proven and would read CURRENT. It counts as CURRENT in the overall verdict, so a busy lane no longer pulls the overall result to INCOMPLETE. A running receipt past its own timeout plus 300 seconds is dead and stays unproven.
- Exit codes are unchanged: 0 overall CURRENT, 1 STALE, 2 unreadable runs directory, 3 INCOMPLETE or NO-DATA.

**Measured today:** a 24-hour run over the live receipts read INCOMPLETE, exit 3 (fable and sol CURRENT, opus and sonnet NO-DATA). The cause was the running receipts: opus held three and sonnet one, and those four were exactly the runs with no recorded effort. With the IN-FLIGHT rule the same receipts read CURRENT.

**Falsifier:** write a receipt with state `failed` and `resolvedModel` null as a lane's newest run beside a CURRENT lane, run the check with `--json`: it must print overall INCOMPLETE and exit 3, never CURRENT. The 2026-10-12 sentence stays on the bus unedited; this entry supersedes it.
