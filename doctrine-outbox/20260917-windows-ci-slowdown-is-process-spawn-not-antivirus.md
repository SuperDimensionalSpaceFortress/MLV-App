---
target: TRAPS.md
kind: trap
source_commit: PENDING
law4: attested
---
### mlv-app, 2026-09-17 — a 5x Windows-vs-Linux CI slowdown was process-spawn volume, not the antivirus everyone blamed

**Symptom (measured):** one CI step (the Python hygiene tests) ran roughly 5x slower on Windows than on an equivalent Linux runner (5.0-6.2x across three master runs). The prior investigation had blamed Windows Defender, tested that theory, falsified it, and closed with "the real cause remains unidentified" — a plausible-sounding culprit had absorbed the investigation without producing an answer.

**Cause, found by measurement:** one test module accounted for 63% of the Windows step's wall time and was itself 11x slower on Windows than Linux. Instrumenting `subprocess.run`/`Popen` across its ~249 tests showed roughly 17,800 real child-process spawns in one run, the overwhelming majority of them `git` subcommands invoked repeatedly by both the code under test and its test harness (one subcommand, `git rev-parse`, accounted for ~7,300 of them at ~30ms each; `git status`, `git remote` and `git merge-base` added a few thousand more). Each spawn cost roughly 30ms on Windows versus 3-5ms on Linux — an intrinsic `CreateProcess` vs `fork` cost difference, not a security-product artifact. 17,800 spawns x a ~25ms Windows/Linux delta alone explains the bulk of the 11x module-level gap, which was itself the majority of the whole-job 5.2x gap.

**Rule:** when a test/CI step is dramatically slower on Windows than Linux, before blaming antivirus, disk, or "Windows is slow," instrument actual subprocess spawn counts (wrap `subprocess.run`/`Popen` to log each call) and look for code — production or test — that repeatedly re-shells to an external tool (`git`, `git status`, ref resolution) instead of caching the result once per test run. Windows process creation is inherently far more expensive per-call than POSIX `fork`, so spawn-count reduction has an outsized effect there specifically.

**Falsifier, and what was measured:** cut the spawn count and see whether the ratio follows. The one fix tried removed only the fixture slice (a per-test repository built by 7-10 git children, replaced by a copied template): interleaved A/B runs went from a 565s to a 489s mean, consistently faster by roughly 9-18%, all 249 tests passing. That is a real but modest win, not parity; the claim here is attribution (spawn volume, not antivirus), and closing the rest of the gap means caching the resolved refs at the remaining call sites.
