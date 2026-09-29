---
target: TRAPS.md
kind: trap
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-06-01 — `TZ=<zone> date` in Windows git-bash silently returns the wrong time under the wrong label

**Symptom (measured):** running `TZ='America/Chicago' date '+...'` inside the Bash tool on a Windows host printed a UTC-like time (about five hours off true Central local time) under the label `GMT` instead of the daylight/standard designation. This produced a concrete downstream error: a peer agent's very recent commit was briefly concluded to be hours old, because the "current time" used for the comparison was wrong.

**Cause:** the git-bash environment on this host does not reliably resolve IANA zoneinfo data via the `TZ` environment-variable override, and falls back to an incorrect value with a misleading label rather than failing loudly. Measured on one Windows host only; whether other git-bash installs share it was not tested, which is the reason for the cross-check below.

**Rule:** do not trust `TZ=<zone> date` output for local-time reasoning on a Windows git-bash host without first cross-checking it once against a known-correct source. Prefer computing local time via a language runtime's own timezone-aware API (e.g. Python's `datetime.now()` plus `time.localtime().tm_isdst` to pick the correct daylight/standard abbreviation) rather than shelling out to `date` with an env-var override on this platform.

**Falsifier:** on a suspect host, run `TZ='<zone>' date` alongside a runtime-native timezone-aware call and compare; any Bash-on-Windows environment where the two disagree should be treated as unable to resolve `TZ` overrides at all, not just imprecise.
