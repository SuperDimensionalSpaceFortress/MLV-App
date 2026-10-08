# CUDA playback present-cadence forensics (CUDA-PLAYBACK-PRESENT-CADENCE-1, round 1)

Why CUDA-backed GPU-window playback swaps at ~6/s while the pipeline decodes/processes at
~25fps (`STALL_PERSISTS_FOREGROUND`,
`.claude-state/fleet-runs/lane-CUDA-PERF-FOREGROUND-JOINED-RUN-1-provisional-20260925T1534Z/summary.md`).
This is a forensics pass over that run's already-captured raw logs -- no new bachelor run.

## Method

Raw inputs (`C:\mlvtmp\lane-FOREGROUND-RUN-20260925\.run-artifacts\raw\{gpu1a,gpu1b,cpu1}\`):
`presentmon.csv`, `presentmon-capture.json`, `result.json`, and `swap-lines.txt` (every
`gpu_window.swap` line extracted from that run's `smoke-run.log`). The full per-frame
`smoke-run.log` itself (the source of `playback_smoke.frame` lines carrying
`present_ui_signal_latency_ms` / `present_overlays_scopes_ms` / `frame_ready_emit` /
`draw_begin` / `present_begin`/`present_end` timestamps) was **not** part of this pull -- only
the swap-line extraction was kept. That log is 11.5MB per leg
(`result.json`'s own `evidence.runLogSnapshot` node names it and its sha256, but the file
itself sits on bachelor, not in this pulled artifact set) -- see Open question below for what
this limits.

Three techniques, each usable with what *was* pulled:

1. **Swap-serial gap analysis.** Every `gpu_window.swap` line carries `presented_serial` --
   the `presentationSerial` (== `PlaybackPrepTask::requestSerial`, a per-frame monotonic
   counter assigned upstream) of the frame that swap actually displayed. Parsing
   `swap-lines.txt` and diffing consecutive `presented_serial` values turns every swap's own
   log line into a precise count of how many *other* submitted frames were overwritten
   between it and the previous swap -- no additional log needed, because a frame that is
   overwritten before paint leaves no trace of its own in this log (see Mechanism below), so
   its absence *is* the count.
2. **Swap-chain cross-check.** `presentmon.csv`'s `SwapChainAddress` column tests the run's
   own "mid-session swap-chain recreation" caveat directly.
3. **Source-code check of the GUI-thread-blocked lead.** The run's hub lead named specific
   candidates (overlays, scopes, slider/UI refresh, histogram, look-assist, logging) for what
   might be occupying the GUI thread between frames. Grepped `MainWindow.cpp` for where those
   are actually computed and whether that code runs on the playback per-frame present path.

## Mechanism: the GPU window is a one-slot mailbox, not a queue

`GpuDisplayWindow` (`platform/qt/GpuDisplayWindow.h`/`.cpp`) holds exactly one pending frame:
`m_pendingImage` / the GL texture, plus `m_pendingPresentationSerial`. Both
`setPresentedImage()` (CPU/QImage route) and
`setPresentedGpuPlaybackReconAmazePostWbTexture()` (the CUDA no-readback texture route this
brief is scoped to) **unconditionally overwrite** that single slot and call `update()`; there
is no per-frame queue anywhere in this window. `paintGL()` -- invoked whenever Qt's event loop
gets around to servicing that `update()` -- draws whatever is *currently* in the slot and, via
Qt's own automatic swap (`QOpenGLWindow`, default `AutoSwapBuffers` behavior; `swapInterval=0`
per the constructor), swaps it; `frameSwapped()` then fires `noteRealSwap()`, which is the
`gpu_window.swap` line these logs contain.

By this construction, **any submission that is not the most recent one at the moment Qt
actually invokes `paintGL()` is silently and completely lost** -- overwritten before it was
ever drawn, with (previously) no counter and no log line of its own. This one mechanism, alone,
predicts every property the data below actually shows:

- `presented_serial` across consecutive swaps is **strictly increasing, with zero repeats and
  zero out-of-order values** (verified below) -- exactly what a single-slot mailbox produces:
  each swap shows whatever was most recently submitted, and a submission can never "come back"
  once superseded.
- The gap between consecutive `presented_serial` values is **exactly** the count of frames
  superseded in between -- because a superseded frame, under this design, is not merely
  dropped by some *other* stage before reaching the window; it reached the window and was
  overwritten there, so counting requester-serial gaps at the window's own swap log is exact,
  not an estimate.

This also means the fate classification the brief's method asked for
(superseded-before-paint / painted-without-swap / GUI-thread-blocked-by-X /
swap-chain-recreation / unexplained) collapses for this window: there is no
"painted-without-swap" state to find (Qt's automatic swap always follows a `paintGL()` call
here), and every non-swapped frame is **superseded-before-paint** by construction. What varies
leg to leg is only *how many* frames land in that bucket, which is exactly what the serial-gap
count below measures.

## Per-leg counts

| leg | swaps | decoded (`frames_presented`) | superseded-before-paint (serial-gap count) | max single coalescing event | swap-to-swap gap range | mean swap gap |
|---|---|---|---|---|---|---|
| gpu1a (GPU, scale=1, foreground-verified) | 156 | 668 | **511** (between swaps) + 4 (serials 1-4, before the first swap) = matches the run's own `swaps_minus_frames_presented=-512` to within edge-counting at session start/end | 10 frames in one gap | 41ms - 355ms | 160.4ms (~6.24/s) |
| gpu1b (GPU, scale=1, foreground NOT verified) | 229 | 1011 | **777** (between swaps) + 4 = matches `swaps_minus_frames_presented=-782` to within the same edge margin | 25 frames in one gap | 39ms - 984ms | 169.2ms (~5.91/s) |
| cpu1 (CPU control, scale=1) | 73 | 73 | **0** -- zero gap in every consecutive pair | 1 (i.e. no coalescing at all) | 359ms - 672ms | 536.1ms (~1.87/s, matches decode 1:1) |

Method: parse `swap-lines.txt`'s `presented_serial` sequence per leg; sum `(gap - 1)` over
consecutive pairs where `gap > 0`; separately confirm zero non-monotonic or repeated values
(`non_monotonic_or_repeat_count = 0` in all three legs) -- the mailbox's own signature. The
small residual between the serial-gap sum and the run's own `swaps_minus_frames_presented`
(511 vs 512, 777 vs 782) is the handful of in-flight frames before the first swap and after the
last swap in the session window, which the serial-gap method (which only counts gaps *between*
swaps) does not attribute to either bucket; it is not evidence of a second loss mechanism.

**The CPU control leg is the clean negative case**: identical code path, same clip, same
scale, zero coalescing, `frames_presented == swaps` exactly. It presents at ~1.9/s not because
frames are lost, but because CPU-path per-frame processing itself takes ~536ms; every frame it
finishes gets shown. The GPU legs finish frames far faster (~25/s) and lose the overwhelming
majority of them (76-77%) to the mailbox before they are ever seen.

## Ruled out: swap-chain recreation

The run's own caveat named "presents split across 2 swap chains for a single MLVApp process
id" as a live, unproven mechanism. Checked directly against `presentmon.csv`:

| leg | chain A | chain B |
|---|---|---|
| gpu1a | 1 present @ t=95832.7ms (single row) | 162 presents, t=107540.4 - 171153.8ms (63.6s span) |
| gpu1b | 1 present @ t=293040.9ms (single row) | 235 presents, t=301435.6 - 365732.3ms (64.3s span) |
| cpu1  | 1 present @ t=46840.6ms (single row)  | 78 presents, t=59071.5 - 127126.8ms (68.1s span) |

All three legs -- **including the CPU control leg, which has zero coalescing and no stall** --
show the identical pattern: one single, isolated present on an early swap chain (consistent
with `MainWindow`'s own top-level window presenting once at startup, before `GpuDisplayWindow`
is installed and takes over the whole session), followed by every remaining present on one
second, session-long chain with no further gaps or interleaving. There is no mid-session
recreation in any leg, GPU or CPU. **Ruled out** as a contributor to the stall.

## Refuted (for the steady-state playback loop specifically): GUI-thread work on a named component

The hub lead's specific candidates -- overlays, scopes, slider/UI refresh, histogram,
look-assist, logging -- are computed by `MainWindow::drawFrame()` (`platform/qt/MainWindow.cpp`,
line 6190; see `draw_frame_ready_scopes_ms` / `draw_frame_ready_overlay_ms` stage-timing keys it
populates). `drawFrame()`'s only call sites are lines 3077, 3203, 7056 and 7787 -- none of them
inside `MainWindow::presentPlaybackPreparedFrame()` (lines 4668-6126), which is the function the
per-frame CUDA-texture playback present path (`presentGpuPlaybackReconAmazePostWbTextureIfActive`,
called at line 5050) actually runs through every frame. **`drawFrame()` does not run per playback
frame on the GPU-window path**, so the elevated `present_ui_signal_latency_ms`/
`present_overlays_scopes_ms` values the hub lead observed on the *first* frames are consistent
with a one-time startup cost (the one `drawFrame()` call that primes the display before playback
begins), not a recurring per-frame cost during steady-state playback. This is a refutation of
that specific named mechanism for the steady state, not a claim that nothing else could be
occupying the GUI thread during playback -- see Open question.

## Open question (DISCLOSED-OPEN): what paces `paintGL()` itself

The mailbox mechanism above fully explains **that** superseded frames are lost and **how many**
are lost per leg. It does not, by itself, explain **why** Qt only gets around to invoking
`paintGL()` at ~6/s when `update()` is being requested at ~25/s with `swapInterval=0`
(uncapped). Two candidate causes both remain consistent with every number in this pass, and
distinguishing them needs data this pull does not have:

- **GUI-thread contention**: something else the GUI thread does per frame (or per some other
  cadence) delays it from returning to the event loop often enough for Qt's coalesced
  `update()` events to be serviced faster. The swap-gap **irregularity** favors this over a
  smooth cap: gpu1a's gaps range 41-355ms and gpu1b's range 39-984ms (a >20x spread within a
  single leg) -- a uniform hardware/driver pacing limit would be expected to produce a far
  tighter, more constant inter-swap interval than that.
- **OS/driver/compositor present-callback pacing** independent of GUI-thread load -- e.g. the
  Optimus-hybrid-specific present-path quirks this exact file's own header comment already
  documents for this hardware (QTBUG-68329 solid-black workaround), or idle-power throttling
  on an automated, input-free launch (the run's own named alternative).

**What would resolve it**: rejoin `playback_smoke.frame` lines (from the full `smoke-run.log`,
not pulled this round -- see Method) to `gpu_window.swap`/`gpu_window.present_fate` lines by
`requestSerial`, for the *entire* session (not just the first frames the hub lead already
looked at), and check whether `presentPlaybackPreparedFrame()`'s own wall-clock cost per call
(`stage_present_ui_signal_latency_ms` etc.) correlates with the swap-gap lengths measured here.
If it does, GUI-thread contention is confirmed; if per-frame cost stays flat while swap gaps
still vary 20x, the OS/driver-pacing hypothesis gains support instead. The brief's own
sanctioned discriminating tool for the power/idle-throttling half of this (forcing
`ES_DISPLAY_REQUIRED`/`ES_SYSTEM_REQUIRED` for the run's duration, no persistent power-setting
change) still applies and was not exercised this round (no bachelor job this round).

## What this round shipped instead of guessing

Per this round's own decision rule, a present-path behavior change (e.g. forcing a synchronous
paint+swap per submission, removing Qt's `update()` coalescing from the playback path entirely)
was considered and **deliberately not made**: it is exactly the kind of change whose safety
depends on which half of the Open question above is true (a synchronous paint could just as
easily make GUI-thread-side pacing worse, blocking on a slow OS-level swap per frame instead of
letting the async path ride out an occasional slow swap), and this round has no bachelor job to
validate it against real hybrid-GPU hardware. Instead, this round ships the fate telemetry the
forensics above needed and did not have (`gpu_window.present_fate`, see the code comments on
`GpuDisplayWindow::noteSupersededBeforePaint()` and the extended
`playback_smoke.gpu_window_swaps` summary line) so the *next* round's forensics -- and any
future bachelor run -- has this counted and logged **live**, rather than needing to be
reconstructed after the fact from swap-serial gaps the way this round had to.

## Slip accounting and capture-free pace legs (PLAYBACK-BACHELOR-PRESENT-JITTER-1)

Every playback smoke session now also logs one `playback_smoke.slip_summary` line (after
`overlap_summary`) plus at most 64 `playback_smoke.slip` lines, from `PlaybackSlipHistogram.h`.
A slip is a frame the timeline passed but no present showed: `displayFrame(k) - displayFrame(k-1) - 1`
for a forward step (a loop wrap or a repeat is never a slip; the first present and the catch-up it
repays are `startup_catchup_frames`). Slips and class counts are in frames, so `slips_per_1000`
(per timeline frame after the first present) reads directly against the 23.9 bar (3.2 per 1000).
Each slip gets one class, first match wins: `capture` (a contact-sheet grab ran in the interval),
`gap` (interval >= 250 ms), `gui_late` (ready before the skipped frame's deadline, presented more than
one period later), `upstream_late` (ready after that deadline, sub-tagged decode/recon/render/queue/none
by the stage over 2x its session median), `clock` (two or more timeline frames in <= 1.25 periods), else
`other`. `hist_slip` buckets slip sizes 0/1/2/3/4-7/8+ and `hist_interval` present intervals
<=45/45-62.5/62.5-83.4/83.4-125/125-250/>250 ms. `native_equiv_presented_fps` is pace x presents /
timeline frames after the first present. The wait for the first frame is mostly repaid a few presents
after it (the guard banks credit while no grant runs, and one later tick spends it), so skips made while
the timeline is still behind the wall time since Play, up to that wait's credit, count as
`startup_catchup_frames`, not slips, and leave the paced `timeline_after_first` (and pace_summary's
`timeline_fps_after_first_present`); `timeline_after_first_raw` keeps the old reading. `timeline_advance_by_path` splits the session's timeline
delta by engine path (`drop_tick`, `whole_frame`, `loop_wrap`; `other` is any slider move while
playing that no engine path made, plus whatever the paths leave unexplained) next to
`pace_guard_granted_frames`, so a timeline faster than the pace can be traced to its path. The
`m16-1243-pace-cinematic-{fullscreen-s4,fullscreen-s2,windowed-s4}` legs are their display-matrix
cells run as SPEED legs with Look Assist forced (cinematic) through `generatorArgs.forceLookAssist`
and no contact sheet (generator `-LookPaceLeg`), so no 30-71 ms GUI-thread framebuffer grab lands
inside the timed Play; the look itself stays on the look and display-matrix legs.
`m16-1243-pace-cinematic-fullscreen-s4-heavy` is the owner-shape pace leg at `telemetryArm` HEAVY, which
keeps the per-frame `playback_smoke.frame` log that LIGHT turns off, so the present that ends a >= 250 ms
interval shows its own queue wait, render, draw and UI latency. It is a stall diagnostic, never a pace number.
The leg inherits the pace legs' 95 % job quiescence threshold (`cpuQuiescenceThresholdPercent` 95, the same as its LIGHT twin
and every pace and display-matrix leg), so at 95 the job's own gate is effectively open. The receipt's `cpuQuiescence` block
records the load but does not label a contended run. COUNTED is decided lane-side (Wait-VenueQuiet QUIET, PASS, exe match and
preLoad <= 30 %), so this leg is evidence-only.