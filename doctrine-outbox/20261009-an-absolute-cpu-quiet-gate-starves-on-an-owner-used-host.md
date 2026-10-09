---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/an-absolute-cpu-quiet-gate-starves-on-an-owner-used-host
rule: on a host the owner also uses, an absolute CPU quiet gate (here 20 percent) passes rarely and unevenly. Do not relax it and do not exclude processes from it. First log its pass rate per hour and attribute the load from system counters; for RELATIVE claims judge paired comparability (ABBA order, a ceiling on the load gap between arms) and label such pairs loaded-comparable, never quiet.
mechanism: over the probe history 25 verdicts were QUIET against 45 COOLDOWN_UNMET, and 25 of 293 probes read at or below 20 percent; ~41 percent of probes passed in one 3-hour window and ~2 percent elsewhere (one day of data: a hypothesis). A non-elevated per-process probe cannot attribute kernel or SYSTEM time: the top 5 processes explained ~6 of a ~28-point mean, so neither a higher threshold nor an exclusion list is a fix. Load does damage legs: one decode timing read 47.06 at 49.6 percent pre-load against 7.02 for its twin at 18.4. Distinct from the card where a gate counted a foreign runner's test host as load (fixed by ancestry): here the load is real and no per-process view can name it.
check: `Get-Counter '\Processor(_Total)\% Processor Time','\Processor(_Total)\% Privileged Time','\Processor(_Total)\% DPC Time','\Process(*)\% Processor Time' -ErrorAction SilentlyContinue` (without the flag, exited processes raise an invalid-sample error); sum the Status-0 Process samples other than _total and idle, divide by the logical CPU count, and compare with _Total (a gap over ~5 points is unattributed load). Count QUIET probes per hour from the gate log before tuning anything.
supersedes: none
evidence: measured
- **Guard:** none yet (VENUE-QUIET-ATTRIBUTION-1 will extend tools/repo_hygiene/test_dual_venue_evidence.py). The gate was deliberately not relaxed.
