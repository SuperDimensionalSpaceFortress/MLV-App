# CUDA playback look parity: the route contract (PARITY-2 round 2)

Companion to [cuda-playback-look-parity.md](cuda-playback-look-parity.md) (which is over the
doc-size cap; new detail goes here instead). Round-1 key: fable blocker "the host asks the engine
for the route outside the playback-preview envelope"; sol hardening H1.

## The contract

> The route the CUDA host hands the display shader equals the route the CPU engine actually takes
> for the same clip, scale, settings AND playback/preview state, at every call site.

There is exactly one call site and one function behind it:

- `platform/qt/GpuPreviewHostRoute.h` `gpuPreviewHostCpuRoute(video, scale, phase3RawConsumed)` is
  what `RenderFrameThread::drawFrame` calls. It returns `{direct8, preCameraClamp}`.
- It asks the engine: `video_mlv.c` `mlvPreviewPlaybackCpuRoute(video, scale, phase3RawEntry,
  &clamp)`. The route and the clamp flag both come from the engine
  (`processingCpuRoutePreCameraClamps`); the host re-derives nothing.
- `HostRouteAndTelemetryComeFromTheSingleEngineCallSite` scans the Qt host sources and fails if any
  other file evaluates the route or the flag, if `gpuPreviewHostCpuRoute` has more than one caller,
  or if the telemetry keys (`gpu_preview_processing_cpu_route_direct8`, `..._pre_camera_clamp`)
  stop reading the helper's result.

## Why the envelope lives in the engine

The CUDA path renders `OutputDebayered16`. `RenderFrameThread`'s `PlaybackPreviewModeGuard` is
enabled only for `OutputProcessed8`, so the thread's preview mode is OFF at the call, and
`mlv_preview_direct8_input_is_cheap` returns 1 unconditionally in that state. Every engine entry
that renders playback frames (`getMlvProcessedFrame8_with_scale`, the processed8 prefetch worker)
evaluates the gate with preview mode ON, playing or paused. `mlvPreviewPlaybackCpuRoute` therefore
saves, sets (preview on, aggressive per `mlvPlaybackAggressivePreviewMode`, scale = effective
scale) and restores the thread's state itself. The caller's state is irrelevant, which is the
property the tests pin (each host-route assertion runs with the thread's preview mode 0 and 1 and
checks the state comes back untouched).

Only the unscaled `getMlvProcessedFrame8` (export, single-frame grab) runs with preview off; it is
not a display path and not asked here.

## The states and their routes (hand-derived, pinned by `HostRoute*`)

| state | engine route | clamp |
|---|---|---|
| eligible receipt, HQ Dual ISO, x1, preview resolution Auto or Half | 16-bit loop (the x1 reduced proxy would engage) | on, except neutral (basic-matrix branch) |
| same, preview resolution Full | direct8 | `AgX \|\| contrast \|\| S/H` |
| same, x2 / x4 | direct8 | as above |
| Dual ISO outside HQ recon (`dual_iso == 2`), any scale, normal dispatch | 16-bit loop | on, except neutral |
| the same frame rendered from Phase 3 raw, x2 / x4 | direct8 (the raw entries check eligibility only) | as direct8 |
| the same frame rendered from Phase 3 raw, x1 | the dispatch (the raw entries return 0 at x1) | as the dispatch |
| ineligible receipt (sharpen, LUT, filter, grain, no camera matrix, ...) | 16-bit loop | on, except neutral |

`phase3RawConsumed` is `decodedRawFrame != nullptr` at the call site: the same condition under
which the CPU branch calls the raw entries. These are the cheapness-refusal fixtures: removing the
cheapness term, or the envelope, fails four tests (measured, see the summary of round 2).

## Pixel cells (sol H1 / fable H2)

`HostRoutedSixteenBitPixelCellsMatchTheEngineWithinOneCode` takes the route and the flag from the
host helper, then compares the display shader (`>> 8`) with the engine route the helper named
(`applyProcessingObject8` or `apply_processing_object`, in 8-bit codes; per-sample tolerance 1, max
3, 0.1%). 16-bit route cells: the fable r1 repro pixel (leveled `[27000,61000,60000]`, vibrance
1.03, WB 6500, contrast 0, S/H 0: 0 codes), sol r2's pixel A, vibrance ramps, vibrance at 2500 K
tint +30, the renderable no-camera-matrix state with vibrance (ramp and pixel), the neutral ramp
(basic-matrix branch, no clamp); and the same states at preview resolution Full against direct8.

**Saturation is an unported stage of the display shader** (disclosed in the main doc). Its cells
therefore compare against the same route with the saturation stage neutralised, on an in-range
pixel (no WB over-range, so the clamp cannot hide anything), and print the saturation delta as
`[UNPORTED-BOUNDARY]` (max 5 to 6 codes, mean 2.7 to 3.3 on the cell pixel) without gating on it.
The route and flag of saturation-only receipts are pinned by `CpuRouteSelectionMatchesEnginePredicates`.
Over-range plus saturation is not pixel-pinned: the stage is not rendered.

## LUT texture cache key (fable H1)

`config.signature` hashes the unclamped diagonal-matrix LUTs only when contrast or S/H is on (this
keeps the pinned golden signatures where they are). The unclamped route feeds them to the camera
matrix directly, so the texture caches also key on `config.rawLutSignature`
(`gpuPreviewProcessingRawLutSignature`, hashed over the three raw LUTs always):
`gpuPreviewProcessingLutTextureSetKeyMatches` compares both, and `GpuDisplayViewport` re-uploads
when either changes. `LutTextureCacheKeyIncludesTheRawLutsWithoutMovingTheSignature` shows two
configs with equal `signature` and different raw LUTs now differ in the key.

## Viewport route contract (fable H3)

`ViewportPresentRoutesNeverBindAStaleShadowsHighlightsBlur` requires the S/H blur upload or
invalidate in each `setPresented*` route to be a full statement at the function's top brace level
and not after an unconditional top-level `return` (`blur_call_is_on_main_path`). Its checker is
mutation-tested in `ViewportRouteContractCheckerAcceptsOnlyTheMainPath` (`if (false) {}`, nested
block, brace-less `if`, lambda, loop, after `return`), and by running the test against mutated
copies of the real source (`MLVAPP_TEST_VIEWPORT_SOURCE`).

## Residual, disclosed

- States the engine reaches only at runtime failure (a raw entry returning 0 after the route was
  chosen falls back to the dispatch) are not modelled.
- The GPU-recon "state debayer" CPU fallback (`allowScale1StateDebayer`) is the CUDA configuration's
  own CPU fallback, not the CPU playback baseline the display shader is compared with.
- Real-GPU / CUDA-hardware confirmation of the look and of S/H cost is unchanged
  (CUDA-LOOK-SH-COST-HARDWARE-MEASURE-1).
