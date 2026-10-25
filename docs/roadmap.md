# MLV-App product roadmap

Rendered by `tools/coordination/render_roadmap.py` from the board's `card-queue.json`
(schema `mlv-app/card-queue/v1`), the live dispatch surface. It replaces the earlier
mirror of the frozen dual-lane `queue.json`, whose 16 rows had mostly landed. The
card-queue file lives under `.claude-state/hub-autopilot/`, which is gitignored and absent
from every hosted checkout, so this file is the only place a reviewer without local state
can see the open product backlog. Where the two disagree, `card-queue.json` wins:
re-render, do not edit by hand. The hub checks freshness with `--check`.

Only rows with `kind` of `product` or `playback` are shown. Hub-tooling, factory,
hygiene and ci-flake cards are tooling churn and are not counted here. Rows the board has
not classified are not counted either, even when the card name says PLAYBACK.

generated_from: 2026-10-25T05:46:25Z

## Open product and playback cards

**macOS scope (owner ruling 2026-10-25T00:44:41Z).** Deferred: macOS feature, performance and GPU work (MAC-MINI-BUILD-HOST-1, CPU-SIMD-CLANG-X86-ENABLE-1, CPU-NEON-HOT-KERNELS-1, GPU-PORTABLE-BACKEND-RHI-1, GPU-METAL-BACKEND-1) until Windows CUDA and the portable GL/RHI path are solid. Not deferred: macOS compilation stays green (MAC-LINUX-BUILD-RESTORE-1, the compile canary, PR #671).

| P | card | kind | state | queued since | order_chain |
|---|---|---|---|---|---|
| 1 | PRODUCT-PR-STRANDED-SWEEP-1 | product | declared | 2026-10-09 | ORDER-20261010T232453Z #3 after CPU-DUALISO-REDUCED-ISO-NOTCH-1 |
| 1 | CPU-SCOPE-LIVE-HEAP-CORRUPTION-1 | product | pr-open | 2026-10-15 | - |
| 1 | CPU-SCOPE-PLAYSTOP-LIFETIME-HARDENING-1 | product | pr-open | 2026-10-15 | - |
| 1 | PLAYBACK-PREP-CONFLATED-MOVE-FR1B-AB-1 | product | queued | 2026-10-15 | - |
| 1 | HFR-NATIVE-D4-HEAD-VISUAL-CELLS-1 | product | queued | 2026-10-16 | - |
| 1 | PR493-CONTROL-ARM-QUIET-AB-1 | product | queued | 2026-10-21 | ORDER-20261021T111948Z #3 after BACHELOR-FS-CUDA-QUIET-LEGS-1 |
| 1 | CPU-LOOKASSIST-PLAYBACK-COST-1 | product | queued | 2026-10-21 | - |
| 1 | CPU-DUALISO-PREVIEW-SCALE-VERIFY-1 | product | queued | 2026-10-21 | - |
| 1 | BACHELOR-CORE-PARTITION-AB-1 | product | queued | 2026-10-23 | - |
| 1 | PROCESSED8-MS-TELEMETRY-STUCK-1-VENUE-LEG | product | declared | 2026-10-24 | - |
| 1 | CPU-17V9-FPS-GAP-AB-1 | product | running | 2026-10-24 | ORDER-20261025T013808Z #2 after LEASE-BATCH-LEASE-END-TZ-1 |
| 2 | PLAYBACK-GL-PRESENT-BACKLOG-1 | product | declared | 2026-10-09 | ORDER-20261008T151800Z #2 after PLAYBACK-GL-PRESENT-SETUP-STALL-1 |
| 2 | PLAYBACK-GPU-SCOPES-NO-VETO-1 | product | pr-open | 2026-10-15 | - |
| 2 | LOOK-FULLSCREEN-PARITY-1 | product | pr-open | 2026-10-16 | - |
| 2 | PLAYBACK-SOURCE-FRAME-IDENTITY-1 | product | pr-open | 2026-10-17 | - |
| 3 | PLAYBACK-CLIP-LENGTH-ENFORCE-2 | product | declared | 2026-10-01 | - |
| 3 | HFR-NATIVE-60FPS-FULLRES-1 | product | pr-open | 2026-10-12 | ORDER-20261014T015628Z #3 after HFR-NATIVE-M16-1457-TIMEBUDGET-1 |
| 3 | PLAYBACK-HFR-CONFORM-DEFAULT-1 | product | declared | 2026-10-15 | - |
| 3 | WARM-RUN-CHROMA-SMOOTH-PARITY-1 | product | pr-open | 2026-10-18 | - |
| 3 | MAC-LINUX-BUILD-RESTORE-1 | product | pr-open | 2026-10-25 | - |
| 6 | PREP-WORKER-HL-RECON-REFUSAL-FIXTURE-1 | product | queued | 2026-10-07 | - |
| 6 | C1-PARITY-TESTS-SKIP-WHEN-UNSET-1 | product | queued | 2026-10-09 | - |
| 6 | PREP-TIER1-FROZEN-ORACLE-1 | product | queued | 2026-10-09 | - |
| 6 | NEUTRAL-PROBE-EMPTY-SELECTION-1 | product | queued | 2026-10-09 | - |
| 6 | LJ92-CONTENDED-PAIR-1 | product | queued | 2026-10-10 | - |
| 6 | RECEIPT-TYPED-STOP-REASON-1 | product | queued | 2026-10-10 | - |
| 6 | VENUE-QUIET-PROBE-PATIENCE-1 | product | queued | 2026-10-10 | - |
| 6 | LOOK-ASSIST-CHROMA-SLACK-QUANTIZATION-1 | product | queued | 2026-10-11 | - |
| 6 | PLAYBACK-SOURCE-FRAME-IDENTITY-FOLLOWUPS-1 | product | queued | 2026-10-17 | - |
| 6 | RAW-UINT16-INFLIGHT-WAIT-MONOTONIC-CLOCK-1 | product | queued | 2026-10-19 | - |
| 6 | GITIGNORE-QMAKE-GENERATED-ONLY-HARDENING-1 | product | queued | 2026-10-21 | - |
| 6 | PROCESSED8-FALLBACK-CLOCK-STEP-BOUND-1 | product | queued | 2026-10-21 | - |
| 6 | PROCESSED8-MEASURED-COUNT-IN-JSON-SUMMARY-1 | product | queued | 2026-10-21 | - |

## Drain

product+playback open: 33

Window: the 7 UTC days ending 2026-10-25 (the last day is partial).
Landed = merged rows by `merged_utc` when the row has one, else by the newest stamp (approximate: a lower bound on the merge time, not a measurement); queued = rows of any state by `source_ledger_stamp`.

| day (UTC) | landed | queued |
|---|---|---|
| 2026-10-19 | 1 | 12 |
| 2026-10-20 | 2 | 3 |
| 2026-10-21 | 3 | 14 |
| 2026-10-22 | 4 | 3 |
| 2026-10-23 | 4 | 3 |
| 2026-10-24 | 0 | 2 |
| 2026-10-25 | 0 | 6 |
| total | 14 | 43 |

does not drain (queued >= landed)

## Parked with reason

**macOS scope (owner ruling 2026-10-25T00:44:41Z).** Deferred: macOS feature, performance and GPU work (MAC-MINI-BUILD-HOST-1, CPU-SIMD-CLANG-X86-ENABLE-1, CPU-NEON-HOT-KERNELS-1, GPU-PORTABLE-BACKEND-RHI-1, GPU-METAL-BACKEND-1) until Windows CUDA and the portable GL/RHI path are solid. Not deferred: macOS compilation stays green (MAC-LINUX-BUILD-RESTORE-1, the compile canary, PR #671).

- OUTPUT-BUDGET-M02-OPTIONAL-1 (P1): HUB-TICK 20261022T0853Z: r1 PARTIAL on NA-4 (gate-spec edit names owner footage path); blocked on NA4-CLIP-OR-NONE-AUTHORING-ROUTE-1 (par...
- CI-WINDOWS-UNITTEST-SHARD-8-1 (P1): HUB-TICK 20261021T0740Z A4: not triggered (merge_group last 15 runs all success, none on ShardDurationBudgetTests; max windows shard ~114...
- PLAYBACK-CUDA-FULLSCREEN-BLUR-UPLOAD-STALL-POST-502-1 (P1): design NO_GO (UM host contention); successors UM-FS-CUDA-QUIET-WINDOW-VERIFY-1, PLAYBACK-HEALTH-SCAN-CONTENTION-CLASS-1; reopen only on a...
- UM-FS-CUDA-QUIET-WINDOW-VERIFY-1 (P1): r1 UNJUDGED; UM route closed NO_GO; question moves to BACHELOR-FS-CUDA-QUIET-LEGS-1 (HUB RULING 10:51:06Z)
- VENUE-CUDA-GOAL-REMEASURE-POST-450-1 (P1): owner deferred CUDA speedup 2026-10-21 (hub-tick-prompt D3-DEFER); r4 not dispatched
- K4-RECEIPT-COMPLETE-ON-MAXTURNS-1 (P2): already fixed on master by 601fb4685 (2026-09-14) + test_coordination_guardrails.py:1257; instance-map K4 cell folded into SPEC-R14-R15-R...
- VENUE-AA-UNDER-LOAD-1 (P2): design done (producer-prompt.md); producer waits on the owner unlocking the Bachelor console (KEEPALIVE_FAILED, HUB RULING 11:14:34Z)
- VENUE-PAIR-COMPARABLE-1 (P2): HUB RULING 2026-10-09T19:10:51Z: AA-UNDER-LOAD-1 closed NOT_COMPARABLE; re-design needs a decode-state covariate, waits on PLAYBACK-BACHE...
- PLAYBACK-CPU-PACE-AWARE-LOOKAHEAD-1 (P2): PR #413 closed unmerged by HUB RULING 2026-10-13T17:16:58Z (no gain; s2 regression belongs to #397 -> PACE-GAP r7)
- GPU-EVENT153-ROOTCAUSE-3 (P2): waits for #437 merge (HUB-TICK 20261015T0610Z D13(d) re-scope: base 228d9a62 superseded by #434 tiling d9d6cd98; D13(e) re-queues on the ...
- OUTPUT-BUDGET-KNOWN-GOOD-PIN-1 (P2): design NO_GO 2026-10-19T21:24:56Z: pin is owner-reserved (golden authority NOT_INSTALLED); successors OUTPUT-BUDGET-KNOWN-GOOD-PROPOSAL-1...
- OUTPUT-BUDGET-KNOWN-GOOD-PIN-2 (P2): r3 PARTIAL 443/480 quiet; no venue can pass the faster-venue probe; option (b) needs new owner ruling (HUB RULING 20261021T1215Z item 3)
- LOOK-ASSIST-FLAVORS-1 (P3): CLOSED (c) HUB-TICK 20261011T1318Z: shipped as LOOK-ASSIST-FLAVORS-2 #254 merge 968c5ce5
- LOOK-ASSIST-M16-CAST-2 (P3): CLOSED-RULED: hub ruling ledger L2213 (#278 CLOSED unmerged, branch kept) and L3107 (CAST stack superseded, colour goal met by #295 #300 ...
- LOOK-ASSIST-M16-CAST-3 (P3): CLOSED-RULED: hub ruling ledger L3107 (#280 CLOSE, lower layer of the CAST stack, superseded, colour goal met by #295 #300 #303)
- LOOK-ASSIST-M16-CAST-4 (P3): CLOSED-RULED: hub ruling ledger L3107 (#282 CLOSE, lower layer of the CAST stack, superseded, colour goal met by #295 #300 #303)
- LOOK-ASSIST-M16-CAST-5 (P3): CLOSED-RULED: hub ruling ledger L3478 (HUB-TICK 20261011T1150Z D13 triage: CAST-5 CLOSED, not a fix) and L3107 (#283 CLOSE)
- LOOK-ASSIST-M16-CAST-6 (P3): PR #286 CLOSED; colour goal closed 2026-10-08 (goal-watch colour ok); parked HUB-TICK 20261020T2231Z
- LOOK-ASSIST-DUALISO-FIELD-SEAM-1 (P3): HUB RULING HUB-TICK 20261007T2308Z: after PLAYBACK-CUDA-HONOUR-SCALE-1 r8 VSTRIPES dual-ISO fix merges; first step re-runs U-RECON on the...
- LOOK-ASSIST-WB-DECISION-2 (P3): design GOAL_MET 2026-10-08; colour goal closed (HUB RULING 0331Z tick); improvement is LOOK-ASSIST-GREEN-LEAN-MEASURE-1
- CPU-DUALISO-NOTCH-COUNTER-PRESENTED-1 (P3): order: after PR #369 merges (it adds notch_frames); unpark then. HUB-TICK 20261010T1509Z
- PLAYBACK-CPU-PACE-GAP-1 (P3): hub chat 2026-10-11T19:10Z: stopped twice for local MLVApp playback on the hub VM (owner objection); re-dispatch only with a venue-only D...
- CLIP-OPEN-ABORT-RENDER-THREAD-REBIND-1 (P3): waits for #493 (CPU-SCOPE-PLAYSTOP-LIFETIME-HARDENING-1) to merge: its cited lines exist only on #493 head 421c4aad; re-queue at P3 the t...
- CLIP-OPEN-ABORT-STATE-ROLLBACK-1 (P3): waits for #493 (CPU-SCOPE-PLAYSTOP-LIFETIME-HARDENING-1) to merge: its cited lines exist only on #493 head 421c4aad; re-queue at P3 the t...
- CLIP-OPEN-PREP-ABORT-USER-FEEDBACK-1 (P3): waits for #493 (CPU-SCOPE-PLAYSTOP-LIFETIME-HARDENING-1) to merge: its cited lines exist only on #493 head 421c4aad; re-queue at P3 the t...
- PLAYSTOP-LIFETIME-WIRING-TEST-1 (P3): waits for #493 (CPU-SCOPE-PLAYSTOP-LIFETIME-HARDENING-1) to merge: its cited lines exist only on #493 head 421c4aad; re-queue at P3 the t...
- PRESENTING-SLOT-BORROWER-WATCHDOG-1 (P3): waits for #493 (CPU-SCOPE-PLAYSTOP-LIFETIME-HARDENING-1) to merge: its cited lines exist only on #493 head 421c4aad; re-queue at P3 the t...
- RGB8-SOURCE-PRESENT-NOTHING-LOG-1 (P3): waits for #493 (CPU-SCOPE-PLAYSTOP-LIFETIME-HARDENING-1) to merge: its cited lines exist only on #493 head 421c4aad; re-queue at P3 the t...
- OUTPUT-BUDGET-KNOWN-GOOD-PROPOSAL-1 (P3): design NO_GO 21:58Z; successors VENUE-REFERENCE-FRAME-JOB-1 -> OUTPUT-BUDGET-KNOWN-GOOD-PROPOSAL-2, JUNE9-BRIDGE-INSTRUMENT-1 (HUB-TICK 2...
- BACHELOR-UNGATED-PRIVILEGED-LOAD-2 (P3): HUB-TICK 20261022T1153Z: r1 complete; successor BACHELOR-VENUE-FOREIGN-FLOOR-FAST-FAIL-1 r1 declared 10:54:07Z and live (ruling 11:29:47Z...
- M16-CAST-PROBES-SALVAGE-1 (P4): D13(d) HUB-TICK 20261013T0714Z: measure-only salvage, colour goal closed 2026-10-08; P6 backlog when idle
- GPU-FIELD-TELEMETRY-COLLECTION-AND-BATCH-1 (P4): waits for #409 to merge (hardening on its code); unpark the tick #409 MERGED is observed
- PR493-CLIP-OPEN-ABORT-HARDENING-1 (P4): waits for #493 (CPU-SCOPE-PLAYSTOP-LIFETIME-HARDENING-1) to merge: its cited lines exist only on #493 head 421c4aad; re-queue at P3 the t...
- MAC-MINI-BUILD-HOST-1 (P4): OWNER RULING 2026-10-25T00:44:41Z - deferred until Windows CUDA + portable GL/RHI solid; ssh mac-mini already works (Qt not installed)
- CPU-NEON-HOT-KERNELS-1 (P5): OWNER RULING 2026-10-25T00:44:41Z - deferred; when unparked, port upstream ilia3101 cb5c8eb8b (Apple Silicon NEON, 2026-10-13) instead of...
- CPU-SIMD-CLANG-X86-ENABLE-1 (P5): OWNER RULING 2026-10-25T00:44:41Z - Mac perf work deferred until Windows CUDA + portable GL/RHI solid
- GPU-PORTABLE-BACKEND-RHI-1 (P5): OWNER - after the CUDA speedup work is complete (CUDA speedup itself owner-deferred 2026-10-21); validating it needs macOS/Metal too
- GPU-METAL-BACKEND-1 (P6): after GPU-PORTABLE-BACKEND-RHI-1 is measured on the Mac mini
