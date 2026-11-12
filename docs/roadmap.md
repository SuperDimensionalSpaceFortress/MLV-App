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

generated_from: 2026-11-12T23:32:29Z

## Open product and playback cards

**macOS scope (owner ruling 2026-10-25T00:44:41Z).** Deferred: macOS feature, performance and GPU work (MAC-MINI-BUILD-HOST-1, CPU-SIMD-CLANG-X86-ENABLE-1, CPU-NEON-HOT-KERNELS-1, GPU-PORTABLE-BACKEND-RHI-1, GPU-METAL-BACKEND-1) until Windows CUDA and the portable GL/RHI path are solid. Not deferred: macOS compilation stays green (MAC-LINUX-BUILD-RESTORE-1, the compile canary, PR #671).

| P | card | kind | state | queued since | order_chain |
|---|---|---|---|---|---|
| 1 | LEASE-BATCH-IDLE-END-UNSEEN-1 | product | declared | 2026-11-05 | - |
| 2 | PLAYBACK-GL-PRESENT-BACKLOG-1 | product | declared | 2026-10-09 | ORDER-20261008T151800Z #2 after PLAYBACK-GL-PRESENT-SETUP-STALL-1 |
| 2 | CPU-S1-DUALISO-MIX-CHROMA-LEVER-1 | product | declared | 2026-11-08 | - |
| 3 | PLAYBACK-HFR-CONFORM-DEFAULT-1 | product | declared | 2026-10-15 | - |
| 6 | PREP-WORKER-HL-RECON-REFUSAL-FIXTURE-1 | product | queued | 2026-10-07 | - |
| 6 | C1-PARITY-TESTS-SKIP-WHEN-UNSET-1 | product | queued | 2026-10-09 | - |
| 6 | PREP-TIER1-FROZEN-ORACLE-1 | product | queued | 2026-10-09 | - |
| 6 | NEUTRAL-PROBE-EMPTY-SELECTION-1 | product | queued | 2026-10-09 | - |
| 6 | LJ92-CONTENDED-PAIR-1 | product | queued | 2026-10-10 | - |
| 6 | RECEIPT-TYPED-STOP-REASON-1 | product | queued | 2026-10-10 | - |
| 6 | VENUE-QUIET-PROBE-PATIENCE-1 | product | queued | 2026-10-10 | - |
| 6 | LOOK-ASSIST-CHROMA-SLACK-QUANTIZATION-1 | product | queued | 2026-10-11 | - |
| 6 | RAW-UINT16-INFLIGHT-WAIT-MONOTONIC-CLOCK-1 | product | queued | 2026-10-19 | - |
| 6 | GITIGNORE-QMAKE-GENERATED-ONLY-HARDENING-1 | product | queued | 2026-10-21 | - |
| 6 | PROCESSED8-FALLBACK-CLOCK-STEP-BOUND-1 | product | queued | 2026-10-21 | - |
| 6 | PROCESSED8-MEASURED-COUNT-IN-JSON-SUMMARY-1 | product | queued | 2026-10-21 | - |

## Drain

product+playback open: 16

Window: the 7 UTC days ending 2026-11-12 (the last day is partial).
Landed = merged rows by `merged_utc` when the row has one, else by the newest stamp (approximate: a lower bound on the merge time, not a measurement); queued = rows of any state by `source_ledger_stamp`.

| day (UTC) | landed | queued |
|---|---|---|
| 2026-11-06 | 1 | 2 |
| 2026-11-07 | 1 | 1 |
| 2026-11-08 | 3 | 2 |
| 2026-11-09 | 4 | 1 |
| 2026-11-10 | 3 | 5 |
| 2026-11-11 | 0 | 1 |
| 2026-11-12 | 1 | 4 |
| total | 13 | 16 |

does not drain (queued >= landed)

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
- HEAPDUMP-SOAK-ACCOUNTING-ONE-POLICY-1 (P1): Design NO_GO 2026-11-04 (design-HEAPDUMP-SOAK-ACCOUNTING-ONE-POLICY-1-20261104T1615Z): no capture reader on fork/master; reopen triggers ...
- CPU-DUALISO-REDUCED-RECON-EDGE-FRINGE-1 (P1): r2 PARTIAL FRINGE_NOT_CLOSED; successor chain S4-DECISION-1 (ruled 11:49:12Z) -> CPU-DUALISO-REDUCED-RECON-PLAYBACK-ONLY-1 (design runnin...
- CPU-DUALISO-REDUCED-RECON-EDGE-FRINGE-1-r2 (P1): round ended PARTIAL; successor CPU-DUALISO-REDUCED-RECON-PLAYBACK-ONLY-1
- CPU-DUALISO-REDUCED-RECON-S4-DECISION-1 (P1): decided by HUB RULING 2026-10-31T11:49:12Z; successor CPU-DUALISO-REDUCED-RECON-PLAYBACK-ONLY-1
- CPU-S1-RECON-UNDER-LOOK-PACE-LEVER-1 (P1): HUB RULING 2026-11-07T21:29:03Z: r4 judged pace-neutral on both arms; no master merge or keys for no measured gain on a HIGH-RISK playbac...
- GPU-EVENT153-ROOTCAUSE-3 (P1): closed by HUB RULING 2026-11-10T11:07:16Z item 4 (HUB-TICK 1056Z): r2 DONE verdict gone B 0/12 at 424e8e6b; successor (a) is #743's next ...
- K4-RECEIPT-COMPLETE-ON-MAXTURNS-1 (P2): already fixed on master by 601fb4685 (2026-09-14) + test_coordination_guardrails.py:1257; instance-map K4 cell folded into SPEC-R14-R15-R...
- VENUE-AA-UNDER-LOAD-1 (P2): design done (producer-prompt.md); producer waits on the owner unlocking the Bachelor console (KEEPALIVE_FAILED, HUB RULING 11:14:34Z)
- VENUE-PAIR-COMPARABLE-1 (P2): HUB RULING 2026-10-09T19:10:51Z: AA-UNDER-LOAD-1 closed NOT_COMPARABLE; re-design needs a decode-state covariate, waits on PLAYBACK-BACHE...
- PLAYBACK-CPU-PACE-AWARE-LOOKAHEAD-1 (P2): PR #413 closed unmerged by HUB RULING 2026-10-13T17:16:58Z (no gain; s2 regression belongs to #397 -> PACE-GAP r7)
- PLAYBACK-GPU-SCOPES-NO-VETO-1 (P2): HUB-TICK 20261109T0134Z A0b pr-stranded: PR #447 r2 PARTIAL (S1 n<4 counted), DIRTY ~120 h since r6 (2026-11-03), no keys; parked at owne...
- OUTPUT-BUDGET-KNOWN-GOOD-PIN-1 (P2): design NO_GO 2026-10-19T21:24:56Z: pin is owner-reserved (golden authority NOT_INSTALLED); successors OUTPUT-BUDGET-KNOWN-GOOD-PROPOSAL-1...
- OUTPUT-BUDGET-KNOWN-GOOD-PIN-2 (P2): r3 PARTIAL 443/480 quiet; no venue can pass the faster-venue probe; option (b) needs new owner ruling (HUB RULING 20261021T1215Z item 3)
- CORE-PARTITION-UNIDENTIFIED-SEEN-PID-1 (P2): INTAKE-GUARD 2026-10-28: queued>landed 3 days (throughput: 2026-10-28 landed 1 / queued 33; 2026-10-27 landed 5 / queued 38; 2026-10-26 l...
- LEASE-BATCH-RECEIPT-ROOT-1-ADV-A1A2A3 (P2): folded into #729 r8 (HUB-TICK 20261101T0051Z, sol r7 demonstrated A1/A2 as blockers)
- HFR-NATIVE-M16-1457-WINDOWED-PRESENT-PENALTY-1 (P2): waits on HFR-NATIVE-M16-1457-PRESENT-TRACE-LEGS-1 r1 window split (Bachelor legs); hub writes the prompt from its summary
- CPU-S1-PACE-NEXT-LEVER-DESIGN-1 (P2): closed: design r1 delivered; successor CPU-S1-PACE-NEXT-LEVER-1 (#774 r2 live); HUB RULING 2026-11-08T04:51:32Z
- CPU-S1-GUI-REGION-RESIDUAL-1 (P2): GOAL_MET by design 20261109T0215Z; successor CPU-S1-DUALISO-MIX-CHROMA-LEVER-1 (HUB-TICK 20261109T0244Z)
- CPU-S1-DUALISO-MIX-CHROMA-LEVER-2 (P2): HUB RULING 2026-11-12T04:21:53Z: LEVER_BAR_MISSED 8.011<10.0 ms under the pre-registered r11 plan; PR #785 stays draft; freeze is on both...
- CPU-S1-DUALISO-PHASE-BALANCE-AVX2-1 (P2): design NO_GO 2026-11-12; ruling 0748
- CPU-S1-DUALISO-MIX-CHROMA-LEVER-2-CONFIRM-1 (P2): HUB-TICK 20261112T0944Z: r1 DONE P1 MET 12.660 ms; its result feeds #785 r11 keys (closed by ruling 09:21:13Z); terminal, no successor round
- CPU-S1-DUALISO-MIX-CHROMA-LEVER-2-REVERIFY-E101-1 (P2): HUB-TICK 20261112T2026Z: r2 DONE REVERIFIED (pooled 5(b) shared=3 differ=0); successor is CPU-S1-DUALISO-MIX-CHROMA-LEVER-2-GUARDS-E101-1...
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
- CPU-S1-DUALISO-MIX-CHROMA-LEVER-2-EVIDENCE-1 (P3): HUB-TICK 20261112T0944Z: r1 complete 08:46Z (5(b) MET, ws-s1 counted B=2 H=2); feeds #785 r11 keys; terminal
- M16-CAST-PROBES-SALVAGE-1 (P4): D13(d) HUB-TICK 20261013T0714Z: measure-only salvage, colour goal closed 2026-10-08; P6 backlog when idle
- GPU-FIELD-TELEMETRY-COLLECTION-AND-BATCH-1 (P4): waits for #409 to merge (hardening on its code); unpark the tick #409 MERGED is observed
- PR493-CLIP-OPEN-ABORT-HARDENING-1 (P4): waits for #493 (CPU-SCOPE-PLAYSTOP-LIFETIME-HARDENING-1) to merge: its cited lines exist only on #493 head 421c4aad; re-queue at P3 the t...
- MAC-MINI-BUILD-HOST-1 (P4): OWNER RULING 2026-10-25T00:44:41Z - deferred until Windows CUDA + portable GL/RHI solid; ssh mac-mini already works (Qt not installed)
- CPU-NEON-HOT-KERNELS-1 (P5): OWNER RULING 2026-10-25T00:44:41Z - deferred; when unparked, port upstream ilia3101 cb5c8eb8b (Apple Silicon NEON, 2026-10-13) instead of...
- CPU-SIMD-CLANG-X86-ENABLE-1 (P5): OWNER RULING 2026-10-25T00:44:41Z - Mac perf work deferred until Windows CUDA + portable GL/RHI solid
- GPU-PORTABLE-BACKEND-RHI-1 (P5): OWNER - after the CUDA speedup work is complete (CUDA speedup itself owner-deferred 2026-10-21); validating it needs macOS/Metal too
- PLAYBACK-SOURCE-FRAME-IDENTITY-FOLLOWUPS-1 (P6): merged into parent PLAYBACK-SOURCE-FRAME-IDENTITY-1 (pr-open); carry its tests item there (hub c998e29e P6 triage 2026-11-01)
- GPU-METAL-BACKEND-1 (P6): after GPU-PORTABLE-BACKEND-RHI-1 is measured on the Mac mini
