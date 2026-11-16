# CPU-S1-FS-SPIKE-TEAM-PARTITION-1 -- r6 receipts on the exact subject build

Evidence pointer only (no source change). Subject build: `1f244117718d28dc16bdef2a287e37c5b61f16a6`
(build manifest `910f8763c162486256b02b9e7f7c3d9738e754353ba4a7178cc782e2da79075a`, staged through the
tracked venue stage chain; DLL pair `playback-attr-3-cuda-dllpair-1f244117718d-r2`, `RESULT=DLL_PAIR_OK`).

Why this file exists: sol r5 raised a BLOCKER (E-01 attribution): the r5 receipts were measured on build
`087639ab`, not on the PR head. Every receipt below carries `metrics.sourceCommit == 1f244117...`, and the
commit that adds this file leaves `git diff --stat 1f244117718d28dc16bdef2a287e37c5b61f16a6 HEAD -- src platform tests`
empty, so the measured build and the PR head differ only by this documentation file.

Venue: Bachelor, clip M16-1243, arm H (candidate), D4b fail closed (`look_assist_enabled=1` read from each
leg's `smoke-run.log`; team partition verdict HONOURED, 12+6). 14 of 14 counted cells (3 x fs-s1 cpu A/B arm plus
the 11 other matrix cells); one extra leg (windowed-s1 cuda, b105062c) ended `INVALID/LEASE_DEMOTED`, was not
counted, and was re-run (0c19873d).

| cell | r6 receipt | outcome | fps (presented after first) | slips/1000 | avg present interval ms |
|---|---|---|---|---|---|
| fullscreen s1 cpu | 0e35d7c1 | PASS QUIET_PRELOAD_OK | 15.319 | 201.005 | 65.201 |
| fullscreen s1 cpu | c5da324b | PASS QUIET_PRELOAD_OK | 15.616 | 184.255 | 64.022 |
| fullscreen s1 cpu | 4706e427 | PASS QUIET_PRELOAD_OK | 15.565 | 175.879 | 64.240 |
| fullscreen s2 cpu | 90f00523 | PASS QUIET_PRELOAD_OK | 23.877 | 5.017 | 41.862 |
| fullscreen s4 cpu | da09523f | PASS QUIET_PRELOAD_OK | 24.013 | 0.000 | 41.641 |
| windowed s1 cpu | d9020657 | PASS QUIET_PRELOAD_OK | 15.906 | 108.878 | 62.813 |
| windowed s2 cpu | 1a522152 | PASS QUIET_PRELOAD_OK | 23.495 | 21.776 | 42.551 |
| windowed s4 cpu | 5c539c19 | PASS QUIET_PRELOAD_OK | 23.901 | 5.017 | 41.835 |
| fullscreen s1 cuda | 1eb1169d | PASS QUIET_PRELOAD_OK | 24.044 | 0.000 | 41.550 |
| fullscreen s2 cuda | 2a7d1067 | PASS QUIET_PRELOAD_OK | 24.000 | 0.000 | 41.644 |
| fullscreen s4 cuda | c2ff6c31 | PASS QUIET_PRELOAD_OK | 23.960 | 3.344 | 41.695 |
| windowed s1 cuda | 0c19873d | PASS QUIET_PRELOAD_OK | 24.044 | 0.000 | 41.561 |
| windowed s2 cuda | f87f1857 | PASS QUIET_PRELOAD_OK | 23.955 | 1.672 | 41.713 |
| windowed s4 cuda | bc0856be | PASS QUIET_PRELOAD_OK | 23.957 | 3.344 | 41.688 |

Receipts live at `.claude-state/dual-venue/receipts/DUAL-VENUE-DISPLAY-MATRIX-1/<leg>/bachelor/<receiptId>.json`
and evidence (logs, contact sheets) at `.claude-state/dual-venue/evidence/<receiptId>/` (gitignored, local to the
hub). All 14 contact sheets were opened: build `1f244117718d`, Look Assist on, frames advance through the clip,
no black or frozen frames. Reading: S1 on cpu stays far below the 23.976 pace (the spike this card targets);
S2 and S4 and every cuda cell hold pace.

r5 comparison receipts (build 087639ab): fs-s1 cpu 9bd4559c, 92904ad2, 816ac37b; fs-s2 cpu ab2ebeaf; fs-s4 cpu
5122507c; w-s1 cpu 91c4b06e; w-s2 cpu 0314bbaa; w-s4 cpu f10552af; fs-s1 cuda cf5a9154; fs-s2 cuda e87a2b9d;
fs-s4 cuda 9e5dfb64; w-s1 cuda 66f07ecf; w-s2 cuda 9b94d404; w-s4 cuda efa075e6.

Still OPEN (not closed by this file): `tests/pipeline/test_dual_iso_pipeline.cpp:7838,7881` (Full20 parity never
observes the realized OpenMP team; closing it needs a C++ edit, which would make these receipts non-attributable).
