# Playback "freeze on open" — investigation & fix decision log

**Audience:** anyone (incl. an external auditor) who needs to see *what was changed, what was
reverted, and why* for the playback-freeze work. This is the human-readable trail; the per-file
diff is in git once committed, and durable one-line lessons are in the agent memory
(`memory/playback_cold_open_freeze_look_assist.md` and siblings).

**Status:** as of 2026-06-10, the clip-open freeze is FIXED on branch
`fix/playback-look-assist-cold-open-freeze` (de-dupe halves the analysis; decouple shows the first
frame immediately). One accepted caveat remains: a brief one-frame pink flash on open — see
"Cold frame-0 pink (separate bug)" below.

**Reading `x1` / `x2` / `x4` / `x8` in this log:** these are the playback scale *factors* the
investigation recorded at the time, kept verbatim in the iteration notes and tables. They mean
resolution divided by that factor: x1 = Full res (1x), x2 = Half res (0.5x), x4 = Quarter res
(0.25x), x8 = Custom res (1/8). The app now shows those words instead of "x2".

**Commits on this branch (newest first):**
- checkpoint 2 — Decouple the (single) analysis from the first-frame paint: the clip paints its first
  frame immediately on open instead of holding black. Trace: analysis delayed ~50ms after the frame,
  frozen window ~6s -> ~1.8s. Accepted brief frame-0 pink-flash caveat.
- `d2301d5c` — checkpoint 1: Auto Look Assist de-dupe + dual-ISO seed fix + automation hooks +
  `MLVAPP_NO_LOOK_ASSIST` gate + this log. Dead drop-on-miss code removed (never committed).

## Cold frame-0 pink (separate bug, NOT fixed here)

Surfaced by the decouple (the black freeze used to hide it). On clip OPEN, the very first painted
frame is the COLD dual-ISO render — recon + focus/bad-pixel maps not yet settled — which the Auto
Look Assist night preset's **+187 exposure boost** amplifies into a brief magenta/pink band. It
clears the instant the clip advances (frame 1+ are clean); it is a **one-frame flash**.

Key facts for whoever fixes it: it appears at **all playback resolutions including Full res (1x)**, so it is NOT the committed
scaled-seed fix's territory (that only covers the Quarter res / Custom res (1/8) downsample paths, video_mlv.c:3464/4023/4133).
look-assist OFF = clean (the artifact is dark/hidden without the boost). Ruled out: WB (auto-WB was
rejected as extreme-color-cast), and slider settings (skipping the baseline restore did not help and
made it worse). The fix is in the full-res + scaled cold dual-ISO recon / map-init path (fragile;
risk of regressing the recon) — deserves its own focused session. Repro: cold-capture with look-assist
on, view f000 (pink) vs f015 (clean).

## Playback-loop bugs (separate from the clip-open fix — 2026-06-10)

After the clip-open freeze was fixed, the user pressed Play and two playback-LOOP problems remained,
distinct from the (now fixed) clip-open freeze:

**Stuck-frame — the user's core "frozen frame". FIXED 2026-06-10** (branch
`fix/playback-stuck-frame-display`, work block wb-97bb261fce6a45d0; was chipped as task_d2c52e6c).
During playback the displayed image froze on ~frame 0 while the engine advanced: GUI showed
"Playback: 25-27 fps" and "Frame N/1081" climbing, timecode advancing, but the on-screen pixels never
changed (viewport span=0; user-confirmed via screenshot).

**Root cause — a poisoned processed8 prefetch cache, NOT the present pipeline.** The MainWindow-side
suspects (acquireLatestReadyFrame, the 2026-04-23 overlap handoff, the 2026-06-08 forward-only guard)
were all exonerated by pipeline-stage captures (`MLVAPP_PIPELINE_CAPTURE_DIR`): S0 raw decode advanced
per frame, S6 displayImage was byte-identical for every frame, and the new SDBG probes showed the
prefetch worker's direct8 render input (scaled RGB16) ADVANCING while its output stayed frozen with
`direct8_gate_fail`. Chain: `applyProcessingObject8` (raw_processing.c) silently returns WITHOUT
writing the output buffer when the processing state is direct8-incompatible (the Auto Look Assist
preset makes it incompatible on this clip); `mlv_render_processed_frame8_direct_with_processing`
reported success anyway; the processed8 prefetch worker (video_mlv.c) then stored its unchanged
thread-local buffer under every advancing frame index with a valid signature; the foreground hit those
poisoned entries every frame and faithfully presented the same stale pixels. Engine/fps/timecode all
ran off frame NUMBERS, which advanced fine.

**Fix (smallest safe):** fail closed — the three direct8 render functions in `src/mlv/video_mlv.c`
now return 0 without claiming success when `processingCanUseDirect8BitOutput()` fails, and the
prefetch task skips rendering entirely in that state (the foreground falls back to the proven indirect
processed16→8 path). Validated by pixels (validate-visible-playback span): x8 0→44.75, x4 43.87,
x2 47.18, x8-no-look-assist (gate-pass side) 34.82 — all SMOOTH, filmstrips visually clean, S6
display bytes now distinct per frame. Pipeline suite: 16 failures with fix vs 17 at baseline (strict
subset — zero new failures, Phase4A cache-scale-isolation now passes).

**Measurement caveat this creates (2026-06-10):** every earlier FPS number measured with the
processed8 prefetch serving hits on a look-assist'd dual-ISO clip (e.g. iteration 12/13's "Sharp
presents prefetch-cached frames at ~7 ms / 24-26 fps") was presenting FROZEN pixels — the hits were
real but the content was poisoned, and single-frame screenshots could not see it. The x2 "real-time
when cool" conclusion and the quarter-res-vs-prefetch comparison need re-measurement on the fixed
build (the prefetch is now inert whenever look assist makes the state direct8-incompatible).
The full audit of which prior conclusions survive is in the next section.

## Audit of prior performance conclusions (2026-06-10, post stuck-frame fix)

**Why this section exists.** A documented dead end is a *conditional* claim: "X didn't help, as
measured by instrument I, against baseline B, on build S." It is never invalidated in the abstract —
it falls when I or B turns out to have been broken. The stuck-frame fix broke one of each: the fps/
cadence **instrument** never validated displayed content, and the prefetch-cached default **baseline**
achieved its speed by not doing the work (serving one frozen frame). This audit sweeps every prior
conclusion that cited either.

**How to run this audit after any future instrument/baseline fix:** (1) name the invalidated layer;
(2) classify each documented conclusion by what killed it — code-reading/theory, direct measurement,
baseline comparison, or attribution — the first is immune to measurement bugs, the last three are
not; (3) grep the decision log / handoff / memories for conclusions citing the fallen layer and
annotate tainted-vs-survives in place (never delete history); (4) re-run the cheapest discriminating
experiment for each tainted item (dead ends kept re-runnable behind env gates or in git history make
this cheap); (5) when documenting any new conclusion, record metric + baseline + build SHA + one
"holds only if..." sentence. Re-audit triggers: an instrument fix, a baseline-participant fix, two
instruments disagreeing (headless fps vs GUI fps vs the user's eyes), or a too-convenient result
("heavy dual-ISO presents in 7 ms" deserved a content check).

| Prior conclusion (where) | Verdict | Why |
|---|---|---|
| "Default Sharp x2 presents prefetch-cached frames at ~7 ms, 24-26 fps real-time when cool" (iter 12/13) | **TAINTED** | The ~7 ms "presents" were poisoned-cache hits displaying one frozen frame; cost and fps reflect skipped work, not rendering. |
| Quarter-res x2 revert: "cannot beat the prefetch-cached default" (iter 13, revert of af40fe88/0123fef9) | **TAINTED basis** | Unfair A/B: quarter-res arm got 0 hits (honest renders) vs default arm's poisoned hits. The revert may still be the right call — re-decide against an honest baseline (chip task_ea437ff2). |
| "x2/x4/x8 already real-time, not bottlenecked" (iter 2 reframe; `playback_optimization_ceiling` memory) | **PARTIALLY TAINTED** | Cadence was inflated by cheap poisoned hits. Post-fix spot checks (span metric) still show x2/x4/x8 SMOOTH, but the honest fps baseline is pending re-measurement. |
| Thermal/load drift: "cool 12-20 ms vs hot 40-47 ms render for identical config" (iter 9) | **PARTIALLY TAINTED** | `render_total` on hit-frames measures a cache copy, not a render; hit rate itself varies with load, conflating with thermals. Mechanism plausible; the numbers need re-anchoring. |
| x1 prefetch-drop win, +55-66% x1 FPS (landed 2026-06-09) | **SURVIVES** | x1 measured `processed8_prefetch_hits=0` in both arms — both arms were honest renders; the win came from freeing cores/disk from a useless background pipeline. |
| "x1 has no safe (non-pixel) FPS lever; ~8-9 fps compute-bound" (iter 3) | **SURVIVES** | Killed by code-reading (half-res recon excludes scale<=1 by design; threads maxed; AVX2 debayer already fast) plus honest x1 renders (no prefetch at x1). |
| Forward-only present guard + flicker findings (iter 5/6) | **SURVIVES (scope-limited)** | Frame-ORDER claims measured real frame numbers. They say nothing about content — that blindness is now closed by the frozen-content gate below. |
| Timer-quantization model: "render just over one 42 ms tick → wait for second tick → ~12 fps" (iter 4/8) | **PARTIALLY TAINTED** | The mechanism is real and re-derivable; the specific render/fps numbers came from hit-era runs. |
| Screenshot grab freezes UI ~2.5 s and distorts fps; thermal session drift (`playback_fps_measurement_reliability`) | **SURVIVES** | Independently demonstrated from timer-event gaps; content-independent. |

## Tooling added so this bug class is caught headlessly (2026-06-10)

1. **Presented-content hash telemetry**: `presentPlaybackPreparedFrame` (MainWindow.cpp) logs
   `draw_frame_ready.present_content display_frame=N play_checked=B position=P hash=H` per present
   when `MLVAPP_INTERACTIVE_TRACE=1` — a sampled FNV-1a over the bytes actually handed to the
   display, computed while the source buffer is still held. Zero cost when the trace is off; the
   experimental gpu16 viewport path is not hashed.
2. **Frozen-content detector check**: `tools/profiling/detect-playback-artifacts.ps1` now FAILs when
   the content hash stays identical across >=10 consecutive presents while the position advances,
   and reports `content_events / distinct_hashes / frozen_content_runs / longest_frozen_run` on the
   machine-readable ARTIFACT-CHECK line (consumed into the gui-smoke `playbackArtifacts` JSON).
   Logs from older builds get an explicit "telemetry missing" NOTE instead of a silent pass.
   Validated three ways: live x8 run = 389 presents / 381 distinct hashes / PASS; synthetic
   frozen log = FAIL with `frozen_content_runs=1`; the original stuck-frame trace (which PASSes
   every cadence metric at "28.6 fps") is exactly the blind spot the check closes.

**x2 ~9 fps compute ceiling.** Re-measured on a COOLED machine: mean 111ms/frame, 66% of frames
80-150ms - cooling did NOT help, so it is NOT thermal (I was wrong about that). The heavy dual-ISO
recon at half-res is inherently ~9 fps; CPU threads maxed, no usable GPU. No free fix. Levers: lower
preview scale (export + paused/scrub stay full quality) or a deliberate quality-tradeoff faster preview.
x4 renders ~22 fps (mean 46ms) but is hit by the stuck-frame bug above.

---

## 1. The bug

Opening a heavy Dual-ISO clip (e.g. `M16-1327.MLV`) and pressing play froze the picture for several
seconds with a black viewport and "no new frames", at every playback resolution (Full / Half / Quarter / 1/8 res). The user
remembered older behaviour as "~7 fps, dropped frames, never frozen" and asked, exhaustively, *when*
this regressed.

## 2. Root cause (confirmed)

**Auto Look Assist** runs a synchronous auto-WB / colour analysis **twice** on the UI thread at
**clip-open**, blocking the first frame ~6–7 s. It is NOT a playback-loop problem; playback after the
cold open is smooth. Introduced **2026-05-25 → 05-27**:

- `026cf20b 2026-05-25` auto-look design, `ae701b09 2026-05-25` "harden auto look"
- `f8b22e56 2026-05-26` `look-assist-baseline-frame-ready` trigger
- `3a8f9d9a 2026-05-27` the `auto_wb` analysis
- `4322b9e6 2026-05-29` "speed up Direct8 Look Assist path" (they already knew it was slow)

**Proof (3 independent):** (a) `MLVAPP_INTERACTIVE_TRACE` shows the ~6.9 s clip-open gap is wall-to-wall
`look_assist.apply.*` events; (b) git as above; (c) same-build A/B — look-assist ON = ~6 s cold open
with `auto_wb_ran=2`, OFF = ~1.6 s with `auto_wb_ran=0`.

## 3. Chronological journey (incl. dead ends)

| # | Action | Outcome | Status |
|---|--------|---------|--------|
| 1 | **Catch-up cap** in `timerFrameEvent` relaxed 1-frame → 250 ms (env `MLVAPP_PLAYBACK_CATCHUP_CAP_MS`) | A/B `frames_advanced` identical on/off — the cap is benign, NOT the cause. I over-claimed it. | **REVERTED** (code restored to original 1-frame cap) |
| 2 | **Drop-on-miss**: reuse last displayed frame on a processed8 cache miss, behind `MLVAPP_PLAYBACK_DROP_ON_MISS` (default off) + a "Drop Slow Frames" GUI toggle | User visual A/B: did not help ("same issue") — because the freeze is at clip-open, not in the playback loop. | **DEAD — to be removed** (still in tree, default-off so inert) |
| 3 | User redirect: "are we sure WHEN the regression began — investigate exhaustively" | Trace + git + A/B → Auto Look Assist (section 2). | done |
| 4 | **Diagnostic gate** `MLVAPP_NO_LOOK_ASSIST` at the apply gate (value-based) to measure/disable the analysis | Confirmed look-assist = the freeze. | **KEEP** (useful dev affordance) |
| 5 | **Fix part A — de-dupe**: member guard `m_lookAssistAppliedReceipt` so the analysis runs once per clip-open | `auto_wb_ran` 2 → 1, cold open ~6 s → 3.0 s. | **KEEP (validated)** |
| 6 | **Fix part B — decouple**: run the single analysis after the first frame paints (so the clip shows immediately) | pending | TODO |
| 7 | Residual: the one ~3 s analysis still blocks the UI after the frame shows; full elimination needs moving it off-thread | not started; needs user decision | TODO/optional |

## 4. Current working-tree state (per file)

- `platform/qt/MainWindow.cpp` / `.h` — **KEEP**: look-assist de-dupe guard (`m_lookAssistAppliedReceipt`),
  `MLVAPP_NO_LOOK_ASSIST` diagnostic gate, autoplay/loop automation hooks (`MLVAPP_AUTOPLAY_*`),
  "Drop Slow Frames" menu toggle (**DEAD — remove with drop-on-miss**), reverted catch-up cap (no net change).
- `src/mlv/video_mlv.c` / `.h` — **KEEP**: x8/x4 dual-ISO seed fix (`llrpEnsureDualIsoPatternSeeded` calls),
  `mlvSetPlaybackDropOnMiss`/`mlvPlaybackDropOnMissEnabled` + the thread-local drop-on-miss buffer
  (**DEAD — remove**), inert `MLVAPP_PROCESSED8_LOOKAHEAD` helper.
- `src/mlv/llrawproc/llrawproc.c` / `.h` — **KEEP**: dual-ISO seed fix (fixes the x8/x4 cold pink
  corruption; landed earlier this session, separate from the freeze).
- `src/mlv/mlv_object.h` — processed8 cache slots (unchanged value, comment only).

## 5. Tooling / scripts added (`.claude-state/scripts/`, untracked scratch)

`validate-visible-playback.ps1` (PrintWindow visible-playback validator), `cold-capture.ps1`
(cold-start window capture), `watch-dropmiss.ps1` (manual A/B launcher). In-app automation:
`MLVAPP_AUTOPLAY_SECONDS/SETTLE_MS/EXIT/LOOP`.

## 6. Lessons (also in agent memory)

- `frames_advanced`/FPS measure the playback **position**, not the displayed pixels — they hid the
  display freeze. Use the interaction trace (`auto_wb`/`draw_frame_ready` timestamps).
- Window-capture `max_static_run` is **thermally noisy** for cold-open timing; the trace is robust.
- `MLVAPP_AUTOPLAY_EXIT` (graceful `qApp->quit`) pops a "save session?" dialog and hangs the instance
  — automation must **force-kill** (`Stop-Process -Force`), never graceful-exit.
- A set-but-EMPTY env var read by `qEnvironmentVariableIsSet` counts as "set"; use a value check
  (`qEnvironmentVariableIntValue(...) != 0`). Empty env vars also leak across PowerShell tool calls.

## 7. Closeout plan

1. Finish decouple (fix part B), validate.
2. Remove dead drop-on-miss code (buffer, runtime flag/getter, "Drop Slow Frames" toggle, store calls).
3. Decide on residual ~3 s (off-thread) with the user.
4. Commit the kept changes in clear, separated commits (seed fix; look-assist de-dupe+decouple;
   automation hooks) so git is the primary audit trail. Keep this log updated through all of it.
