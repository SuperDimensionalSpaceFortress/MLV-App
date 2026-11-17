# MLV-App product roadmap

Rendered by `tools/coordination/render_roadmap.py` from the board's `card-queue.json`,
the live dispatch surface. That file sits under `.claude-state/hub-autopilot/`,
gitignored, absent from hosted checkouts, so this is the only place a reviewer
without local state sees the open backlog. Where they disagree, `card-queue.json`
wins: re-render, do not edit by hand. The hub renders a live copy to
`.claude-state/heartbeat/roadmap-live.md` every hour. When its
open cards, priorities or notes differ from the live queue and it is 7 or more days old,
a drift detector files a refresh card. `--check` is a manual tool; no CI job runs it.

Only rows with `kind` of `product` or `playback` are shown, plus owner asks
(`origin` = `owner`) of any kind. Hub-tooling, factory, hygiene and ci-flake cards are
tooling churn, not counted here. Unclassified rows are not counted, even if
the card name says PLAYBACK, unless the owner asked.

generated_from: 2026-11-17T21:02:01Z

## Open product and playback cards

**macOS scope (owner ruling 2026-10-25T00:44:41Z).** Deferred: macOS feature, performance and GPU work (MAC-MINI-BUILD-HOST-1, CPU-SIMD-CLANG-X86-ENABLE-1, CPU-NEON-HOT-KERNELS-1, GPU-PORTABLE-BACKEND-RHI-1, GPU-METAL-BACKEND-1) until Windows CUDA and the portable GL/RHI path are solid. Not deferred: macOS compilation stays green (MAC-LINUX-BUILD-RESTORE-1, the compile canary, PR #671).

| P | card | kind | state | queued since | order_chain |
|---|---|---|---|---|---|
| 1 | DISK-STEADY-STATE-1 | hygiene (owner) | running | 2026-10-08 | - |
| 1 | ORPHAN-CLAUDE-PROCESS-REAPER-1 | factory (owner) | declared | 2026-10-14 | - |
| 1 | LOOK-LABEL-FONT-SETTINGS-1 | product | queued | 2026-10-16 | - |
| 1 | LOOK-LABEL-PLAYBACK-INFO-1 | product | pr-open | 2026-11-16 | - |
| 2 | PLAYBACK-GL-PRESENT-BACKLOG-1 | product | declared | 2026-10-09 | ORDER-20261008T151800Z #2 after PLAYBACK-GL-PRESENT-SETUP-STALL-1 |
| 2 | LANE-SINGLE-MEMORY-BUDGET-1 | hub-tooling (owner) | queued | 2026-10-14 | - |
| 2 | ARCHIVE-UM-RETENTION-1 | unclassified (owner) | declared | 2026-10-15 | - |
| 2 | HFR-PLAYBACK-RATE-TOGGLE-1 | unclassified (owner) | queued | 2026-10-31 | - |
| 2 | HFR-CLIP-CENSUS-DISO-FIELD-1 | unclassified (owner) | queued | 2026-10-31 | - |
| 2 | BUILD-GUARD-JOBS-SCALE-TO-HOST-1 | hub-tooling (owner) | queued | 2026-11-16 | - |
| 2 | LANE-NONPS1-DOTSOURCE-SHELLEXECUTE-1 | hub-tooling (owner) | running | 2026-11-17 | - |
| 2 | EXPORT-VENUE-LEG-1 | product | pr-open | 2026-11-17 | ORDER-20261117T184523Z #1 first |
| 3 | PLAYBACK-SUMMARY-SELF-DESCRIBING-1 | hub-tooling (owner) | queued | 2026-10-03 | - |
| 3 | DISK-PAGEFILE-AWARENESS-1 | hygiene (owner) | queued | 2026-10-05 | - |
| 3 | BACHELOR-WPR-ELEVATED-SIDECAR-1 | unclassified (owner) | queued | 2026-10-11 | - |
| 3 | ESCAPED-INCIDENT-LEDGER-1 | unclassified (owner) | queued | 2026-10-11 | - |
| 3 | DEBAYER-ADVISOR-1 | product | queued | 2026-10-12 | ORDER-20261117T184523Z #3 after EXPORT-DEBAYER-QUALITY-1 |
| 3 | BACHELOR-MLV-BACKGROUND-ECOQOS-1 | unclassified (owner) | declared | 2026-10-24 | - |
| 3 | RAM-DERIVED-1 | unclassified (owner) | queued | 2026-10-27 | - |
| 3 | COS-SCAN-EVIDENCE-PACKET-NOT-ASK-1 | unclassified (owner) | queued | 2026-10-29 | - |
| 3 | DOCTRINE-GH-CMD-SHIM-RETIRED-RECEIPT-1 | factory (owner) | queued | 2026-11-08 | - |
| 3 | FLEET-QUALITY-LIFT-DOTREC-PORT-1 | factory (owner) | queued | 2026-11-08 | - |
| 3 | MINI3-QUALITY-LIFT-FIXTURES-MISSING-1 | hub-tooling (owner) | queued | 2026-11-17 | - |
| 3 | EXPORT-DEBAYER-QUALITY-1 | product | queued | 2026-11-17 | ORDER-20261117T184523Z #2 after EXPORT-VENUE-LEG-1 |
| 6 | PREP-WORKER-HL-RECON-REFUSAL-FIXTURE-1 | product | queued | 2026-10-07 | - |
| 6 | C1-PARITY-TESTS-SKIP-WHEN-UNSET-1 | product | queued | 2026-10-09 | - |
| 6 | PREP-TIER1-FROZEN-ORACLE-1 | product | queued | 2026-10-09 | - |
| 6 | NEUTRAL-PROBE-EMPTY-SELECTION-1 | product | queued | 2026-10-09 | - |
| 6 | LJ92-CONTENDED-PAIR-1 | product | queued | 2026-10-10 | - |
| 6 | RECEIPT-TYPED-STOP-REASON-1 | product | queued | 2026-10-10 | - |
| 6 | VENUE-QUIET-PROBE-PATIENCE-1 | product | queued | 2026-10-10 | - |
| 6 | PLAYBACK-HEALTH-SCAN-PRESENTED-FRAMES-1 | unclassified (owner) | queued | 2026-10-11 | - |
| 6 | LOOK-ASSIST-CHROMA-SLACK-QUANTIZATION-1 | product | queued | 2026-10-11 | - |
| 6 | PLAYBACK-HFR-CONFORM-DEFAULT-1 | product | queued | 2026-10-15 | - |
| 6 | STUCK-RECONCILER-1 | unclassified (owner) | queued | 2026-10-15 | - |
| 6 | RAW-UINT16-INFLIGHT-WAIT-MONOTONIC-CLOCK-1 | product | queued | 2026-10-19 | - |
| 6 | GITIGNORE-QMAKE-GENERATED-ONLY-HARDENING-1 | product | queued | 2026-10-21 | - |
| 6 | PROCESSED8-FALLBACK-CLOCK-STEP-BOUND-1 | product | queued | 2026-10-21 | - |
| 6 | PROCESSED8-MEASURED-COUNT-IN-JSON-SUMMARY-1 | product | queued | 2026-10-21 | - |

## Drain

product+playback open: 39 (incl. 20 owner asks of another kind)

Window: the 7 UTC days ending 2026-11-17 (the last day is partial).
Landed = merged rows by `merged_utc` when the row has one, else by the newest stamp (approximate: a lower bound on the merge time, not a measurement); queued = rows of any state by `source_ledger_stamp`.

| day (UTC) | landed | queued |
|---|---|---|
| 2026-11-11 | 0 | 1 |
| 2026-11-12 | 2 | 1 |
| 2026-11-13 | 2 | 2 |
| 2026-11-14 | 1 | 3 |
| 2026-11-15 | 2 | 5 |
| 2026-11-16 | 1 | 4 |
| 2026-11-17 | 1 | 5 |
| total | 9 | 21 |

does not drain (queued >= landed)

## Parked owner asks

- KERNEL-HUB-AUTOPILOT-TRACKED-1 (P6, hub-tooling): parked 42 d (since 2026-10-06): PARKED throughput swarm 3 ruling 2026-10-29T19:13:33Z (non-product, P>=5 or unclassified, >72h, n...
- SESSION-AUTO-PRUNE-1 (P1, unclassified): parked 32 d (since 2026-10-16): HUB-TICK 20261016T0712Z: hub-chat-owned (design para 1: interactive hourly cron only, never a lan...
- PLAY-COUNTERS-GPU (P3, unclassified): parked 25 d (since 2026-10-23): not new work: legacy roadmap row, hosted-leg-landed per HUB NOTE 2026-10-23T15:43:20Z
- PLAY-C2-SUBMIT-2-ACCEPT (P3, unclassified): parked 25 d (since 2026-10-23): not new work: legacy roadmap row, parked-with-reason per HUB NOTE 2026-10-23T15:43:20Z
- MAC-MINI-BUILD-HOST-1 (P4, product): parked 23 d (since 2026-10-25): OWNER RULING 2026-10-25T00:44:41Z - deferred until Windows CUDA + portable GL/RHI solid; ssh mac-...
- CPU-SIMD-CLANG-X86-ENABLE-1 (P5, product): parked 23 d (since 2026-10-25): OWNER RULING 2026-10-25T00:44:41Z - Mac perf work deferred until Windows CUDA + portable GL/RHI s...
- CPU-NEON-HOT-KERNELS-1 (P5, product): parked 23 d (since 2026-10-25): OWNER RULING 2026-10-25T00:44:41Z - deferred; when unparked, port upstream ilia3101 cb5c8eb8b (Ap...
- GPU-PORTABLE-BACKEND-RHI-1 (P5, product): parked 23 d (since 2026-10-25): OWNER - after the CUDA speedup work is complete (CUDA speedup itself owner-deferred 2026-10-21); ...
- GPU-METAL-BACKEND-1 (P6, product): parked 23 d (since 2026-10-25): after GPU-PORTABLE-BACKEND-RHI-1 is measured on the Mac mini
- BENCH-GATE-CALIBRATION-1 (P1, unclassified): parked 23 d (since 2026-10-25): HUB-TICK 20261025T1756Z: r1 record-only measurement complete (RESULT 16:45:32Z); successors LEASE...
- LEASE-MINUTES-SIZE-TO-JOB-1 (P1, unclassified): parked 22 d (since 2026-10-26): conditional 'If requested' card, never requested; HUB-TICK 20261026T1101Z
- PR493-CONTROL-ARM-QUIET-AB-1 (P1, bench): parked 18 d (since 2026-10-30): r2 ended DONE; successor PR493-POSTMERGE-CELL18-REGRESSION-1 r1 live (HUB-TICK 20261030T0351Z rul...
- KERNEL-INDEPENDENT-READER-HOLDOUT-1 (P2, factory): parked 12 d (since 2026-11-05): successor R2 r1 declared 2026-11-04T22:35:10Z and armed; false round-ended-no-successor row
- PAGEFILE-SIZE-METER-1 (P3, unclassified): parked 5 d (since 2026-11-12): HUB-TICK 20261112T0838Z A0b backlog-growing: P3 queued >600 h with no run/PR evidence (p6-prune-c...
- MINI3-CUDA-DLLPAIR-LOCAL-BUILD-1 (P3, factory): parked 2 d (since 2026-11-15): NO_GO by swarm 00:2xZ: build-backend-dll.ps1 ends by running dll_test.exe (needs a GPU; PC-MINI3 ...
- GPU-EXPORT-BITEXACT-1 (P1, product): parked 0 d (since 2026-11-17): held per HUB RULING [tool stamp 2026-11-17T08:15:45Z]: design NO_GO; waits for UPSTREAM-SYNC-2026...

## Parked with reason

**macOS scope (owner ruling 2026-10-25T00:44:41Z).** Deferred: macOS feature, performance and GPU work (MAC-MINI-BUILD-HOST-1, CPU-SIMD-CLANG-X86-ENABLE-1, CPU-NEON-HOT-KERNELS-1, GPU-PORTABLE-BACKEND-RHI-1, GPU-METAL-BACKEND-1) until Windows CUDA and the portable GL/RHI path are solid. Not deferred: macOS compilation stays green (MAC-LINUX-BUILD-RESTORE-1, the compile canary, PR #671).

- PRODUCT-PR-STRANDED-SWEEP-1 (P1): r2 DONE 14:45Z: #447 #459 #477 #516 merged master 0801141b, pushed ff; keys are mechanical-keys work, not a sweep round
- CPU-SCOPE-LIVE-HEAP-CORRUPTION-1 (P1): PARKED and SPLIT by HUB-TICK 20261030T1357Z ruling: successors CPU-SCOPE-LIVE-HEAP-CORRUPTION-SRC-SPLIT-1 + HEAPDUMP-SOAK-ACCOUNTING-ONE-...
- HFR-NATIVE-D4-HEAD-VISUAL-CELLS-1 (P1): HUB-TICK 20261031T1048Z: carried by HFR-NATIVE-60FPS-FULLRES-1-MERGE-MASTER r4 (live) and r5 (armed) on PR #420, same worktree
- OUTPUT-BUDGET-M02-OPTIONAL-1 (P1): HUB-TICK 20261022T0853Z: r1 PARTIAL on NA-4 (gate-spec edit names owner footage path); blocked on NA4-CLIP-OR-NONE-AUTHORING-ROUTE-1 (par...
- UM-FS-CUDA-QUIET-WINDOW-VERIFY-1 (P1): r1 UNJUDGED; UM route closed NO_GO; question moves to BACHELOR-FS-CUDA-QUIET-LEGS-1 (HUB RULING 10:51:06Z)
- VENUE-CUDA-GOAL-REMEASURE-POST-450-1 (P1): owner deferred CUDA speedup 2026-10-21 (hub-tick-prompt D3-DEFER); r4 not dispatched
- CPU-17V9-FPS-GAP-AB-1 (P1): closed by HUB RULING 2026-10-25T09:54:40Z: r7b VERDICT=confound, no regression cell (answered: not a regression)
- PROCESSED8-MS-TELEMETRY-STUCK-1-VENUE-LEG (P1): closed done by HUB-TICK 20261025T1151Z: r4 8bcefec6 not a #553 regression; successors PROCESSED8-STALE-FIELD-CHECKER-PROMOTE-1 + PROCESSE...
- PLAYBACK-CUDA-FULLSCREEN-BLUR-UPLOAD-STALL-POST-502-1 (P1): design NO_GO (UM host contention); successors UM-FS-CUDA-QUIET-WINDOW-VERIFY-1, PLAYBACK-HEALTH-SCAN-CONTENTION-CLASS-1; reopen only on a...
- CPU-DUALISO-REDUCED-RECON-EDGE-FRINGE-1 (P1): r2 PARTIAL FRINGE_NOT_CLOSED; successor chain S4-DECISION-1 (ruled 11:49:12Z) -> CPU-DUALISO-REDUCED-RECON-PLAYBACK-ONLY-1 (design runnin...
- CPU-DUALISO-REDUCED-RECON-EDGE-FRINGE-1-r2 (P1): round ended PARTIAL; successor CPU-DUALISO-REDUCED-RECON-PLAYBACK-ONLY-1
- CPU-DUALISO-REDUCED-RECON-S4-DECISION-1 (P1): decided by HUB RULING 2026-10-31T11:49:12Z; successor CPU-DUALISO-REDUCED-RECON-PLAYBACK-ONLY-1
- CPU-S1-RECON-UNDER-LOOK-PACE-LEVER-1 (P1): HUB RULING 2026-11-07T21:29:03Z: r4 judged pace-neutral on both arms; no master merge or keys for no measured gain on a HIGH-RISK playbac...
- GPU-EVENT153-ROOTCAUSE-3 (P1): closed by HUB RULING 2026-11-10T11:07:16Z item 4 (HUB-TICK 1056Z): r2 DONE verdict gone B 0/12 at 424e8e6b; successor (a) is #743's next ...
- FOOTAGE-ADMIT-HFR-3 (P1, owner ask, superseded): SUPERSEDED by HUB RULING 2026-11-17T05:30:39Z (owner correction): the outdoor 60 fps clip is the already-admitted M16-1457; M16-1534 was ...
- K4-RECEIPT-COMPLETE-ON-MAXTURNS-1 (P2): already fixed on master by 601fb4685 (2026-09-14) + test_coordination_guardrails.py:1257; instance-map K4 cell folded into SPEC-R14-R15-R...
- VENUE-AA-UNDER-LOAD-1 (P2): design done (producer-prompt.md); producer waits on the owner unlocking the Bachelor console (KEEPALIVE_FAILED, HUB RULING 11:14:34Z)
- VENUE-PAIR-COMPARABLE-1 (P2): HUB RULING 2026-10-09T19:10:51Z: AA-UNDER-LOAD-1 closed NOT_COMPARABLE; re-design needs a decode-state covariate, waits on PLAYBACK-BACHE...
- PLAYBACK-CPU-PACE-AWARE-LOOKAHEAD-1 (P2): PR #413 closed unmerged by HUB RULING 2026-10-13T17:16:58Z (no gain; s2 regression belongs to #397 -> PACE-GAP r7)
- PLAYBACK-GPU-SCOPES-NO-VETO-1 (P2): HUB-TICK 20261109T0134Z A0b pr-stranded: PR #447 r2 PARTIAL (S1 n<4 counted), DIRTY ~120 h since r6 (2026-11-03), no keys; parked at owne...
- HFR-CLIP-DIVERSITY-1 (P2, owner ask, done): CLOSED by HUB RULING 2026-11-17T05:30:39Z (owner correction): the owner's outdoor Moody Gardens 59.94 clip is M16-1457, admitted 2026-10-...
- OUTPUT-BUDGET-KNOWN-GOOD-PIN-1 (P2): design NO_GO 2026-10-19T21:24:56Z: pin is owner-reserved (golden authority NOT_INSTALLED); successors OUTPUT-BUDGET-KNOWN-GOOD-PROPOSAL-1...
- OUTPUT-BUDGET-KNOWN-GOOD-PIN-2 (P2): r3 PARTIAL 443/480 quiet; no venue can pass the faster-venue probe; option (b) needs new owner ruling (HUB RULING 20261021T1215Z item 3)
- CORE-PARTITION-UNIDENTIFIED-SEEN-PID-1 (P2): INTAKE-GUARD 2026-10-28: queued>landed 3 days (throughput: 2026-10-28 landed 1 / queued 33; 2026-10-27 landed 5 / queued 38; 2026-10-26 l...
- LEASE-BATCH-RECEIPT-ROOT-1-ADV-A1A2A3 (P2): folded into #729 r8 (HUB-TICK 20261101T0051Z, sol r7 demonstrated A1/A2 as blockers)
- HFR-NATIVE-M16-1457-WINDOWED-PRESENT-PENALTY-1 (P2): NO_GO 2026-11-12T23:5xZ: penalty removed by #743; owner gap = HFR-NATIVE-M16-1457-FULLSCREEN-S1S2-PRESENT-GAP-1
- CPU-S1-PACE-NEXT-LEVER-DESIGN-1 (P2): closed: design r1 delivered; successor CPU-S1-PACE-NEXT-LEVER-1 (#774 r2 live); HUB RULING 2026-11-08T04:51:32Z
- CPU-S1-DUALISO-MIX-CHROMA-LEVER-1 (P2): stale declared row: LEVER-2 shipped as #785; CPU s1 pace now on CPU-S1-PACE-POST-785-LEVER-1 (HUB-TICK 20261113T1931Z)
- CPU-S1-GUI-REGION-RESIDUAL-1 (P2): GOAL_MET by design 20261109T0215Z; successor CPU-S1-DUALISO-MIX-CHROMA-LEVER-1 (HUB-TICK 20261109T0244Z)
- CPU-S1-PREP-POINTWISE-LEVER-1 (P2): closed HUB-TICK 20261116T0412Z: round terminal (DONE at board venue / delivered / lever bar missed); ledger HUB RULING 04:19Z
- FULLSCREEN-INFO-OVERLAY-1 (P2): DUPLICATE of LOOK-LABEL-PLAYBACK-INFO-1 (owner ask 2026-11-06); its extra fields merged there at 2026-11-16T22:24:17Z
- LOOK-ASSIST-DUALISO-FIELD-SEAM-1 (P3): HUB RULING HUB-TICK 20261007T2308Z: after PLAYBACK-CUDA-HONOUR-SCALE-1 r8 VSTRIPES dual-ISO fix merges; first step re-runs U-RECON on the...
- LOOK-ASSIST-WB-DECISION-2 (P3): design GOAL_MET 2026-10-08; colour goal closed (HUB RULING 0331Z tick); improvement is LOOK-ASSIST-GREEN-LEAN-MEASURE-1
- CPU-DUALISO-NOTCH-COUNTER-PRESENTED-1 (P3): order: after PR #369 merges (it adds notch_frames); unpark then. HUB-TICK 20261010T1509Z
- PLAYBACK-CPU-PACE-GAP-1 (P3): hub chat 2026-10-11T19:10Z: stopped twice for local MLVApp playback on the hub VM (owner objection); re-dispatch only with a venue-only D...
- WARM-RUN-CHROMA-SMOOTH-PARITY-1 (P3): HUB-TICK 20261109T0134Z A0b pr-stranded: PR #516 DIRTY ~510 h, stacked on #502, red Repo Hygiene CI, no keys; parked at owner CPU cap 2. ...
- CLIP-OPEN-ABORT-RENDER-THREAD-REBIND-1 (P3): waits for #493 (CPU-SCOPE-PLAYSTOP-LIFETIME-HARDENING-1) to merge: its cited lines exist only on #493 head 421c4aad; re-queue at P3 the t...
- CLIP-OPEN-ABORT-STATE-ROLLBACK-1 (P3): waits for #493 (CPU-SCOPE-PLAYSTOP-LIFETIME-HARDENING-1) to merge: its cited lines exist only on #493 head 421c4aad; re-queue at P3 the t...
- CLIP-OPEN-PREP-ABORT-USER-FEEDBACK-1 (P3): waits for #493 (CPU-SCOPE-PLAYSTOP-LIFETIME-HARDENING-1) to merge: its cited lines exist only on #493 head 421c4aad; re-queue at P3 the t...
- PLAYSTOP-LIFETIME-WIRING-TEST-1 (P3): waits for #493 (CPU-SCOPE-PLAYSTOP-LIFETIME-HARDENING-1) to merge: its cited lines exist only on #493 head 421c4aad; re-queue at P3 the t...
- PRESENTING-SLOT-BORROWER-WATCHDOG-1 (P3): waits for #493 (CPU-SCOPE-PLAYSTOP-LIFETIME-HARDENING-1) to merge: its cited lines exist only on #493 head 421c4aad; re-queue at P3 the t...
- RGB8-SOURCE-PRESENT-NOTHING-LOG-1 (P3): waits for #493 (CPU-SCOPE-PLAYSTOP-LIFETIME-HARDENING-1) to merge: its cited lines exist only on #493 head 421c4aad; re-queue at P3 the t...
- OUTPUT-BUDGET-KNOWN-GOOD-PROPOSAL-1 (P3): design NO_GO 21:58Z; successors VENUE-REFERENCE-FRAME-JOB-1 -> OUTPUT-BUDGET-KNOWN-GOOD-PROPOSAL-2, JUNE9-BRIDGE-INSTRUMENT-1 (HUB-TICK 2...
- BACHELOR-UNGATED-PRIVILEGED-LOAD-2 (P3): HUB-TICK 20261022T1153Z: r1 complete; successor BACHELOR-VENUE-FOREIGN-FLOOR-FAST-FAIL-1 r1 declared 10:54:07Z and live (ruling 11:29:47Z...
- LEASE-BATCH-ZONE-UNRESOLVED-LABEL-1 (P3): folded into #704 r6 (sol r5b B1 = Dot A1), HUB-TICK 20261028T1405Z
- CORE-PARTITION-REGION2-PENDING-ORDER-1 (P3): INTAKE-GUARD 2026-10-28: queued>landed 3 days (throughput: 2026-10-28 landed 1 / queued 33; 2026-10-27 landed 5 / queued 38; 2026-10-26 l...
- HFR-NATIVE-M16-1457-WINDOWED-TIMER-GRANULARITY-1 (P3): folded into HFR-NATIVE-M16-1457-GUI-PRESENT-CADENCE-1 r1 (hub 17:49Z)
- CPU-S1-PACE-POST-785-LEVER-1 (P3): PARKED as open draft PR 813: r5 LEVER_BAR MISSED (best H 18.06 vs bar 22.777); SLIPS direction only (n=2/arm/cell), not a regression; CLO...
- CPU-S1-PACE-813-L1-REMEASURE-MINI3-1 (P3): DONE r5 (comment 6887955681); verdict ruled; HUB-TICK 20261115T0242Z 813 ruling (3-reviewer swarm)
- CPU-S1-PRESENT-INTERVAL-BOUND-1 (P3): closed HUB-TICK 20261116T0412Z: round terminal (DONE at board venue / delivered / lever bar missed); ledger HUB RULING 04:19Z
- M16-CAST-PROBES-SALVAGE-1 (P4): D13(d) HUB-TICK 20261013T0714Z: measure-only salvage, colour goal closed 2026-10-08; P6 backlog when idle
- GPU-FIELD-TELEMETRY-COLLECTION-AND-BATCH-1 (P4): waits for #409 to merge (hardening on its code); unpark the tick #409 MERGED is observed
- PR493-CLIP-OPEN-ABORT-HARDENING-1 (P4): waits for #493 (CPU-SCOPE-PLAYSTOP-LIFETIME-HARDENING-1) to merge: its cited lines exist only on #493 head 421c4aad; re-queue at P3 the t...
- PLAYBACK-SOURCE-FRAME-IDENTITY-FOLLOWUPS-1 (P6): merged into parent PLAYBACK-SOURCE-FRAME-IDENTITY-1 (pr-open); carry its tests item there (hub c998e29e P6 triage 2026-11-01)
- 14 owner asks of another kind are closed with a resolution (done 10, superseded 3, no_go 1; card-queue.json)
