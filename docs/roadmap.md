# MLV-App product roadmap

Rendered by `tools/coordination/render_roadmap.py` from the board's `card-queue.json`.
That file sits under `.claude-state/hub-autopilot/`,
gitignored, absent from hosted checkouts, so this is the only place a reviewer
without local state sees the open backlog. If they differ, `card-queue.json`
wins: re-render, do not edit by hand. The hub renders a copy to
`.claude-state/heartbeat/roadmap-live.md` every hour. When this file is
7 or more days old and its open cards, priorities or pre-table note words differ from the queue,
a drift detector may file a refresh card, or none if a row will not parse. `--check` is manual; no CI runs it.

Only rows with `kind` of `product` or `playback` are shown, plus owner asks
(`origin` = `owner`) of any kind. Hub-tooling, factory, hygiene and ci-flake cards are
tooling churn, not counted. Unclassified rows are not counted, even if
the card name says PLAYBACK, unless the owner asked.

generated_from: 2026-11-25T04:42:20Z

## Open product and playback cards

**macOS scope (owner ruling 2026-10-25T00:44:41Z).** Deferred: macOS feature, performance and GPU work (MAC-MINI-BUILD-HOST-1, CPU-SIMD-CLANG-X86-ENABLE-1, CPU-NEON-HOT-KERNELS-1, GPU-PORTABLE-BACKEND-RHI-1, GPU-METAL-BACKEND-1) until Windows CUDA and the portable GL/RHI path are solid. Not deferred: macOS compilation stays green (MAC-LINUX-BUILD-RESTORE-1, the compile canary, PR #671).

| P | card | kind | state | queued since | order_chain |
|---|---|---|---|---|---|
| 1 | DEBAYER-ADVISOR-1 | product | queued | 2026-10-12 | ORDER-20261117T184523Z #3 after EXPORT-DEBAYER-QUALITY-1 |
| 1 | LOOK-LABEL-FONT-SETTINGS-1 | product | queued | 2026-10-16 | - |
| 1 | EXPORT-DEBAYER-QUALITY-1 | product | pr-open | 2026-11-17 | ORDER-20261117T184523Z #2 after EXPORT-VENUE-LEG-1 |
| 1 | EXPORT-PRORES-FFMPEG-EXE-PACKAGE-1 | product | pr-open | 2026-11-18 | - |
| 1 | DUALISO-CONFIGURE-CACHE-1 | product | pr-open | 2026-11-19 | - |
| 1 | PLAYBACK-TRACE-LOWOVERHEAD-1 | playback | pr-open | 2026-11-20 | - |
| 1 | BENCH-GPU-CLOCKS-FLUSH-1 | product | queued | 2026-11-20 | - |
| 1 | DUALISO-CACHE-FAILURE-FIXTURES-1 | product | queued | 2026-11-21 | - |
| 1 | DUALISO-LUT-STAMP-END-TO-END-TEST-1 | product | queued | 2026-11-21 | - |
| 1 | DLL-TEST-ISO-PATTERN-SENSITIVE-VECTOR-1 | product | queued | 2026-11-21 | - |
| 1 | EXPORT-PRORES-LOOK-ASSIST-1 | product | queued | 2026-11-21 | - |
| 1 | DEBAYER-CAPTURE-LABEL-1 | product | queued | 2026-11-21 | - |
| 1 | LJ92-PRED1-DECODER-TEST-HARDENING-1 | product | queued | 2026-11-21 | - |
| 1 | EXPORT-PROFILE-GPU-RECON-CACHE-COUNTERS-1 | product | queued | 2026-11-21 | - |
| 1 | CUDA-RECON-AMAZE-KERNEL-OVERLAP-1 | product | queued | 2026-11-21 | - |
| 1 | ATTR3-PRESENTMON-BLIND-OVERRIDE-2 | product | queued | 2026-11-21 | - |
| 1 | ATTR3-OVERRIDE-NULL-CADENCE-FIELDS-1 | product | queued | 2026-11-21 | - |
| 1 | GPU-WINDOW-FATE-TELEMETRY-FAIL-PATH-1 | product | queued | 2026-11-21 | - |
| 1 | CURSOR-AUTOHIDE-REHIDE-NEW-SURFACE-1 | product | queued | 2026-11-21 | - |
| 1 | PLAYBACK-FPS-METER-MONOTONIC-CLOCK-1 | product | pr-open | 2026-11-21 | - |
| 1 | PLAYER-CURSOR-AUTOHIDE-HISTORY-CONTINUITY-1 | product | queued | 2026-11-21 | - |
| 1 | PLAYER-CURSOR-AUTOHIDE-PREDECESSOR-RED-PROOF-1 | product | queued | 2026-11-21 | - |
| 1 | APP-PLAY-SCAN-PINNED-CAPTURE-GUARD-1 | product | queued | 2026-11-21 | - |
| 1 | GUI-SMOKE-ARG-RANGE-OVERFLOW-1 | product | queued | 2026-11-21 | - |
| 1 | PINNED-EXTRACTOR-INVOCATION-CONTRACT-1 | product | queued | 2026-11-21 | - |
| 1 | VENUE-FIFO-ADVERSARY-TAIL-2 | product | queued | 2026-11-21 | - |
| 1 | LJ92-ALIAS-DISTINCT-MAPPING-1 | product | queued | 2026-11-22 | - |
| 1 | EVERY-FRAME-PLAY-OLDEST-FIRST-UNPINNED-VENUE-LEG-1 | product | queued | 2026-11-22 | - |
| 1 | PINNED-ADMISSION-JUDGES-SLIDER-START-1 | product | queued | 2026-11-22 | - |
| 1 | PINNED-TARGET-BELOW-REQUIRED-UNPACED-EXIT-TYPE-1 | product | queued | 2026-11-22 | - |
| 1 | VENUE-CHAIN-PLAN-PREFLIGHT-PARITY-1 | product | queued | 2026-11-22 | - |
| 2 | PLAYBACK-GL-PRESENT-BACKLOG-1 | product | declared | 2026-10-09 | ORDER-20261008T151800Z #2 after PLAYBACK-GL-PRESENT-SETUP-STALL-1 |
| 2 | LJ92-PRED1-FAST-PATH-1 | product | pr-open | 2026-11-19 | - |
| 2 | EXPORT-RENDER-DUALISO-APPLIED-SIGNAL-1 | product | pr-open | 2026-11-23 | - |
| 2 | LOOK-LABEL-SCALE-FIELD-PRESENTED-1 | product | declared | 2026-11-24 | - |
| 6 | PREP-WORKER-HL-RECON-REFUSAL-FIXTURE-1 | product | queued | 2026-10-07 | - |
| 6 | C1-PARITY-TESTS-SKIP-WHEN-UNSET-1 | product | queued | 2026-10-09 | - |
| 6 | PREP-TIER1-FROZEN-ORACLE-1 | product | queued | 2026-10-09 | - |
| 6 | NEUTRAL-PROBE-EMPTY-SELECTION-1 | product | queued | 2026-10-09 | - |
| 6 | LJ92-CONTENDED-PAIR-1 | product | queued | 2026-10-10 | - |
| 6 | RECEIPT-TYPED-STOP-REASON-1 | product | queued | 2026-10-10 | - |
| 6 | VENUE-QUIET-PROBE-PATIENCE-1 | product | queued | 2026-10-10 | - |
| 6 | LOOK-ASSIST-CHROMA-SLACK-QUANTIZATION-1 | product | queued | 2026-10-11 | - |
| 6 | PLAYBACK-HFR-CONFORM-DEFAULT-1 | product | queued | 2026-10-15 | - |
| 6 | RAW-UINT16-INFLIGHT-WAIT-MONOTONIC-CLOCK-1 | product | queued | 2026-10-19 | - |
| 6 | GITIGNORE-QMAKE-GENERATED-ONLY-HARDENING-1 | product | queued | 2026-10-21 | - |
| 6 | PROCESSED8-FALLBACK-CLOCK-STEP-BOUND-1 | product | queued | 2026-10-21 | - |
| 6 | PROCESSED8-MEASURED-COUNT-IN-JSON-SUMMARY-1 | product | queued | 2026-10-21 | - |
| - | ... and 44 more open owner asks of another kind (card-queue.json) | - | - | - | - |

## Drain

product+playback open: 92 (incl. 44 owner asks of another kind)

Window: the 7 UTC days ending 2026-11-25 (the last day is partial).
Landed = merged rows by `merged_utc` when the row has one, else by the newest stamp (approximate: a lower bound on the merge time, not a measurement); queued = rows of any state by `source_ledger_stamp`.

| day (UTC) | landed | queued |
|---|---|---|
| 2026-11-19 | 3 | 8 |
| 2026-11-20 | 1 | 8 |
| 2026-11-21 | 2 | 56 |
| 2026-11-22 | 3 | 11 |
| 2026-11-23 | 1 | 2 |
| 2026-11-24 | 1 | 5 |
| 2026-11-25 | 0 | 0 |
| total | 11 | 90 |

does not drain (queued >= landed)

## Parked owner asks

- ... and 30 more owner asks parked (card-queue.json)

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
- CUDA-1457-FS-S2-SLIP-CHECK-1 (P1): folded into PLAYER-FULLSCREEN-KEEPS-PLAYING-1 r2 (#848): same 1457 s2 control-vs-head 2+2 quiet legs; SLIP_CHECK line in its summary.md (...
- VENUE-FIFO-ADVERSARY-TAIL-1 (P1): HUB RULING 2026-11-24T15:30:24Z item 1: r11 FINAL sol CR B1+B2 r11-introduced; successor VENUE-FIXTURE-DRAINEDPOPEN-STDLIB-PARITY-1
- REFERENCE-FRAME-PRESENTED-COUNT-LOOP-EXIT-1-FOLLOWUP-1 (P1): HELD by hub chat c8429ad2 2026-11-21T23:27Z: it edits the MainWindow.cpp loop-exit code that #881 (OPEN, head 970ef3f9, held on the Bache...
- PINNED-OUTPUT-PIN-EXTENDED-PREFIX-1 (P1): HELD by hub chat c8429ad2 2026-11-21T23:27Z: the fix is at tools/release-runtime-admission.psm1:751 as changed by #862 (OPEN, draft, head...
- CUDA-SHADOW-M16-1457-FRAME-86-MISMATCH-1 (P1): superseded by its D13 successor CUDA-SHADOW-M16-1457-FRAME-86-STAGE-DUMP-1 (r1 armed as slot row f86sd-r1 by HUB-TICK 20261122T1852Z)
- BACHELOR-1457-NATIVE-GUI-LATE-SITTING-20261122-1 (P1): CLOSED ANSWERED (bench, no PR): r3 PERSISTS_CONTROL 38.814 fps on 6/6 CONTROL legs (ledger 12:45:16Z); successor BACHELOR-1457-NATIVE-GUI...
- K4-RECEIPT-COMPLETE-ON-MAXTURNS-1 (P2): already fixed on master by 601fb4685 (2026-09-14) + test_coordination_guardrails.py:1257; instance-map K4 cell folded into SPEC-R14-R15-R...
- VENUE-AA-UNDER-LOAD-1 (P2): design done (producer-prompt.md); producer waits on the owner unlocking the Bachelor console (KEEPALIVE_FAILED, HUB RULING 11:14:34Z)
- VENUE-PAIR-COMPARABLE-1 (P2): HUB RULING 2026-10-09T19:10:51Z: AA-UNDER-LOAD-1 closed NOT_COMPARABLE; re-design needs a decode-state covariate, waits on PLAYBACK-BACHE...
- PLAYBACK-CPU-PACE-AWARE-LOOKAHEAD-1 (P2): PR #413 closed unmerged by HUB RULING 2026-10-13T17:16:58Z (no gain; s2 regression belongs to #397 -> PACE-GAP r7)
- PLAYBACK-GPU-SCOPES-NO-VETO-1 (P2): HUB-TICK 20261109T0134Z A0b pr-stranded: PR #447 r2 PARTIAL (S1 n<4 counted), DIRTY ~120 h since r6 (2026-11-03), no keys; parked at owne...
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
- PLAYBACK-RECON-SECOND-WORKER-1 (P2): HELD by HUB RULING ledger 2026-11-20T02:33:42Z (owner directive item 7) for IMPLEMENTATION; design lane PLAYBACK-FRAME-PARALLEL-DESIGN-1 ...
- PLAYBACK-FRAME-PARALLEL-DESIGN-1 (P2): r1 DONE (design: K=2 recommended); successor PLAYBACK-CPU-UTIL-OMP-SWEEP-1 QUEUED 10:40:23Z; hub chat c8429ad2 11:24Z
- PLAYBACK-CPU-UTIL-OMP-SWEEP-1 (P2): PARKED 13:26Z by hub chat c8429ad2: r2 ended PARTIAL SPEC_GATE_LEG_SPEC_NOT_COMMITTED; r3 needs PLAYBACK-CPU-UTIL-OMP-SWEEP-SPECS-1 lande...
- PRESENT-TRACE-INLINE-STAGE-STAMPS-1 (P2): HELD on trigger: waits for #873 (PLAYBACK-TRACE-LOWOVERHEAD-1) to merge. On the inline and runSerial routes decode and recon run on the r...
- LOOK-LABEL-ENGINE-REPLAY-REGRESSION-1 (P2): HELD on trigger: waits for #834 (LOOK-LABEL-PLAYBACK-INFO-1) to merge. B1-class regression guard: same-frame scale-1 cache replay check i...
- LOOK-LABEL-PLAYBACK-INFO-2 (P2): HUB RULING HUB-TICK 20261124T1627Z item 1: r9 FINAL, sol r9 INV-3 open; successor LOOK-LABEL-SCALE-FIELD-PRESENTED-1
- CI-FLAKE-PLAYBACK-DUE-WAKE-R17A1-TIMING-1 (P2): PR 889 parked by HUB RULING 2026-11-24T17:11:36Z item 3; successor CI-FLAKE-R17A1-SPAN-LOSS-TYPED-VERDICT-1 (HUB-TICK 20261124T2122Z)
- DUALISO-REUSE-DECISION-CONFIGURE-EXCLUSION-1 (P2): A1 of #868 r22 adversary pass 2 (GpuDisplayWindow.cpp:1462): a configure can store between the final generation load and the reuse decisi...
- PLAY-PACE-BUDGET-KNIFE-EDGE-1457-UNPACED-1 (P2): hub chat 351c6a3c 14:25:58Z: r4 (cap) ended ADVERSARY_OPEN A1,A2, nothing pushed; PARKED; work preserved at 6e651a00; successors UNPACED-...
- PLAY-PACE-BUDGET-KNIFE-EDGE-1457-UNPACED-2 (P2): hub chat 351c6a3c 15:1xZ: r1 PARTIAL ADVERSARY_OPEN A1-A3 (introduced majors, incl. A2 video_mlv.c:10551 MAPP fps regression); parked per...
- LOOK-ASSIST-DUALISO-FIELD-SEAM-1 (P3): HUB RULING HUB-TICK 20261007T2308Z: after PLAYBACK-CUDA-HONOUR-SCALE-1 r8 VSTRIPES dual-ISO fix merges; first step re-runs U-RECON on the...
- LOOK-ASSIST-WB-DECISION-2 (P3): design GOAL_MET 2026-10-08; colour goal closed (HUB RULING 0331Z tick); improvement is LOOK-ASSIST-GREEN-LEAN-MEASURE-1
- CPU-DUALISO-NOTCH-COUNTER-PRESENTED-1 (P3): order: after PR #369 merges (it adds notch_frames); unpark then. HUB-TICK 20261010T1509Z
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
- PLAYER-TOOLBAR-STOP-MAINWINDOW-DISPATCH-2 (P3): HELD hub chat c8429ad2 09:25Z: capacity hold (producers 4/4, 7 hub slot rows pending). Seam per r1 feasibility file: separate tests/mainw...
- PRESENT-TRACE-HARDENING-BATCH-1 (P3): HELD on trigger: waits for #873 (PLAYBACK-TRACE-LOWOVERHEAD-1) to merge. Key-hardening backlog carried open on #873, one card (split if t...
- PLAYER-FULLSCREEN-CONTROL-BAR-LABEL-LIFT-1 (P3): HELD on trigger: waits for #834 (LOOK-LABEL-PLAYBACK-INFO-1) to merge. Lift the fullscreen look label above the shown control bar (#875 m...
- DUALISO-1457-SHADOW-FRAME-86-GPU-CPU-MISMATCH-1 (P3): Superseded 2026-11-22T10:55Z by CUDA-SHADOW-M16-1457-FRAME-86-MISMATCH-1 (P2, same defect: M16-1457 frame 86 GPU vs CPU, master and head ...
- PLAYBACK-DUE-WAKE-EXPIRY-STAMP-SEAM-1 (P3): SPLIT by design-PLAYBACK-DUE-WAKE-EXPIRY-STAMP-SEAM-1-roundtail-20261123T1435Z; in-scope remainder folded into #889 r10 (ruling 15:40:10Z...
- LAUNCHER-APP-PLAY-BUDGET-HANDSHAKE-1 (P3): hub chat 351c6a3c 15:1xZ: parked with the #902 line; the defect exists only in unmerged #902/UNPACED-2 code
- M16-CAST-PROBES-SALVAGE-1 (P4): D13(d) HUB-TICK 20261013T0714Z: measure-only salvage, colour goal closed 2026-10-08; P6 backlog when idle
- GPU-FIELD-TELEMETRY-COLLECTION-AND-BATCH-1 (P4): waits for #409 to merge (hardening on its code); unpark the tick #409 MERGED is observed
- PR493-CLIP-OPEN-ABORT-HARDENING-1 (P4): waits for #493 (CPU-SCOPE-PLAYSTOP-LIFETIME-HARDENING-1) to merge: its cited lines exist only on #493 head 421c4aad; re-queue at P3 the t...
- LOOK-LABEL-ADVERSARY-00C600A9-1 (P4): HELD on trigger: waits for #834 (LOOK-LABEL-PLAYBACK-INFO-1) to merge. Adversary pass 00C600A9 findings, not touched, OPEN. source lane-L...
- DUALISO-CONFIGURE-CACHE-INV7-SNAPSHOT-HARDEN-1 (P4): HELD on trigger: waits for #868 (DUALISO-CONFIGURE-CACHE-1) to merge. INV-7 snapshot hardening at llrawproc.c:2108 not done; advisory on ...
- PLAYBACK-SOURCE-FRAME-IDENTITY-FOLLOWUPS-1 (P6): merged into parent PLAYBACK-SOURCE-FRAME-IDENTITY-1 (pr-open); carry its tests item there (hub c998e29e P6 triage 2026-11-01)
- ... and 2 more owner asks closed with a resolution (card-queue.json)
- 21 owner asks of another kind are closed with a resolution (done 16, superseded 3, no_go 2; card-queue.json)
