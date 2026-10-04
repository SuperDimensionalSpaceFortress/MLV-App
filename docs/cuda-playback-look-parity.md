# CUDA playback Look Assist parity — round 1 inventory

CUDA-PLAYBACK-LOOK-PARITY-1, round 1. Traces every Look Assist parameter from MainWindow's
apply path through `processingObject_t` / `GpuPreviewProcessingConfig` to whichever shader the
live CUDA texture-present fast path (`GpuDisplayWindow` / `GpuDisplayViewport`
`presentGpuPlaybackReconAmazePostWbTexture*`) actually draws with, per file:line evidence.

## CURRENT STATE (PLAYBACK-SEEK-RENDER-PARITY-1, 2026-10-03) -- read this first

The sections below are the history of CUDA-PLAYBACK-LOOK-PARITY-1/-2. Since
PLAYBACK-SEEK-RENDER-PARITY-1 the live DISPLAY shader applies the **whole per-pixel chain** in
the engine's order: levels, WB, contrast+pivot, shadows/highlights, camera matrix + gamut
compression, **AgX forward**, gamma, **hue-vs / luma-vs curves**, vibrance, **saturation**,
**toning**, the **creative curves** (the receipt's dark/light S-curve + lightening and the
gradation curves Y/R/G/B, composed bit-exactly into one per-channel RGBA16 table) and the **AgX
inverse**. The default receipt's S-curve was the owner-visible defect: CUDA playback dropped it
and looked lifted ("milky") against the paused frame and the CPU route.

Stages the display shader does **not** apply -- **vignette, highlight reconstruction, gradient,
.cube LUT, chroma separation/blur, sharpen, median denoise** -- are **refused**, never dropped:
`gpuPreviewProcessingDisplayShaderRefusedStages()` turns off
`MainWindowGpuPreviewPolicyState::gpuPreviewProcessingDisplayShaderCompatible`, which refuses the
GPU recon and AMaZE texture-present routes (and the scale-1 clamp), and the viewport's RGB16
display-shader present is skipped, so playback stays on a route whose processing applies them.
Telemetry: `gpu_display_shader_refused_stages=<list>` per frame, and the texture-present
fallback reason starts with `display_shader_refused_stages=<list>`.

The test-side switch `neutralize_unported_creative_stages` is gone: every engine-anchored and
direct8-anchored test compares against the FULL engine, S-curve included
(`GpuPreviewProcessing.EngineAnchoredReceiptSCurveRealFrameMatchesEngine`,
`...EngineAnchoredCreativeChainMatchesEngineOnBothCpuRoutes`,
`...DisplayShaderCreativeCurveCompositionIsBitExact`,
`...DisplayShaderRefusalPredicateMatchesConfigFlags`). The "PARTIAL PARITY" list that follows
is kept as history and no longer describes the code.

## PARTIAL PARITY, NOT FULL PARITY (history: CUDA-PLAYBACK-LOOK-PARITY-1/-2)

This work makes the live CUDA display shader apply **contrast+pivot, shadows/highlights and
vibrance**. It does **not** make CUDA playback match CPU Look Assist in general. The live
DISPLAY shader still **skips** each of the following (the SUBSET/offscreen shader and the CPU
path apply them; the live CUDA fast path does not):

- creative curves (`applyCreativeCurves`: contrast-curve LUT, gradation Y/R/G/B)
- toning (`applyToning`)
- saturation (`applySaturation`)
- hue-vs / luma-vs curves (`applyHueVs`)
- chroma smooth (`applyChroma`, see "DISCLOSED-OPEN" below)

Found while landing (LAND-2, grep of `previewApply*` uniforms in
`gpuPreviewProcessingDisplayFragmentShaderSource` vs `gpuPreviewProcessingSubsetFragmentShaderSource`
in `platform/qt/GpuPreviewProcessing.cpp`): the display shader declares only
`previewApplyInLoopContrast`, `previewApplyShadowsHighlights`, `previewApplyVibrance` and
`previewApplyGamutCompression`. The subset shader additionally has `previewApplyAgx`,
`previewApplyGradient`, `previewApplyGradientContrast`, `previewApplyHighlightRecon`,
`previewApplyLut` and `previewApplyVignette`, which are therefore also absent from the display
shader source. Whether any of those is covered by another stage on the live CUDA path was not
traced in this work; treat them as not applied until CUDA-LOOK-DISPLAY-STAGES-1 proves otherwise.

**What "parity" means here (round 2).** Round 1 compared the display shader with the in-file C++
mirror `gpuPreviewProcessingApplyCpuReference` and called that CPU parity. A mirror written next to
the shader shares its author's assumptions: both sides clamped the WB-boosted matrix value to 16
bits *before* the contrast / shadows-highlights multiply, which the shipped engine does not do, so
the tests passed while the shipped colour was wrong (sol r1 BLOCKER; fable r1 hardening). Round 2
removed those mirror tests and replaced them with **engine-anchored** tests that call the
production engine (`apply_processing_object`, or the whole `getMlvProcessedFrame16` render where the
shadows/highlights blur must come from the engine's own refresh) on the same debayered frame the
display shader receives. **Only the engine-anchored tests (`GpuPreviewProcessing.EngineAnchored*`
and `GpuPreviewProcessing.DisplayShader*MatchesProductionEngine`) prove "parity with the production
engine", and only for contrast+pivot, shadows/highlights, vibrance and the WB / camera-matrix /
gamut / gamma chain they depend on**, with the creative-curve stages switched off in the engine
(`neutralize_unported_creative_stages`) because the display shader does not implement them. They
say nothing about the stages listed above. A preset that uses any of those will still look
different on live CUDA than on CPU. See "Engine-anchored parity (round 2)" below for the engine
path they anchor to, the measured residuals and what is still not covered. (PARITY-2: those tests
anchor to the generic 16-bit route; the declared reference is the direct8 preview route, see the
next section and `GpuPreviewProcessing.Direct8Anchored*`.)

These gaps belong to the follow-up card **CUDA-LOOK-DISPLAY-STAGES-1**. No document, commit
message or PR for this work may claim full CUDA/CPU Look Assist parity.

## DECLARED PARITY REFERENCE (CUDA-PLAYBACK-LOOK-PARITY-2)

PR #212 was parked after its second review key (sol r2) because "parity with the engine" was
ambiguous: the CPU has more than one production route and they disagree. **The reference is now
declared: the display shader reproduces the route CPU playback preview takes for the current receipt
at the current scale** — the direct-8-bit kernel when the receipt is eligible and the input is cheap
(the route CUDA playback replaces), otherwise the generic 16-bit loop.

### The CPU routes (file:line at the time of writing)

| route | picked when | clamps + truncates the WB-matrix output to uint16 before the camera matrix? |
|---|---|---|
| direct-8-bit kernel (`raw_processing_8bit_kernel.inc` ~189-197; shared C kernel, or the intrinsics kernel `raw_processing.c` ~2685 for receipts with none of AgX / contrast / S-H / vibrance / saturation) | `processing_can_use_direct_8bit_output` (camera matrix on; no hue/luma curves, toning, highlight recon, gradient, vignette, LUT, filter, denoiser, CA-desaturate, chroma separation, sharpening > 0.005, grain; `raw_processing.c` ~2523) **and** `mlv_preview_direct8_input_is_cheap` (`video_mlv.c` ~6325: the x1 reduced-proxy guard and dual-ISO outside HQ recon can refuse it); the dispatch is `getMlvProcessedFrame8Scaled` (`video_mlv.c` ~7288) | **only if `AgX || contrast || shadows/highlights`** (`if( apply_agx || apply_local_tone )`); otherwise the unclamped float value goes straight into the camera matrix, and a post-matrix result at or below -1 wraps (`(uint32_t)` cast) to gamma index 65535 |
| generic 16-bit loop (`apply_processing_object`, `raw_processing.c` ~3243-3422) | everything else (the 8-bit path falls back to it through the 16-bit render) | always (`pix[i] = LIMIT16(..)` stored as uint16) |
| basic-matrix fast branch of the 16-bit loop (`raw_processing.c` ~3139, `processing_can_use_basic_matrix_fast_path && !AgX`) | creative and local tone neutral, no highlight recon / gradient / vignette | never (same as the unclamped direct8 case) |

### What the shader does now

`GpuPreviewProcessingConfig::preCameraClamp` (uniform `previewPreCameraClamp`) selects the
behaviour per frame. The host does **not** re-derive the predicates: `RenderFrameThread` calls
`gpuPreviewHostCpuRoute`, which asks the engine for the route and the flag, per frame (the route
depends on scale and input); telemetry `gpu_preview_processing_cpu_route_direct8` /
`..._pre_camera_clamp`. Contract, states, Phase 3, tests:
[cuda-playback-look-parity-route.md](cuda-playback-look-parity-route.md).

- clamped route: `pix = floor(clamp(diagonal * expo, 0, 65535))`, gamma index `floor(clamp(pix))`
  (as before);
- unclamped route: `pix = diagonal * expo` into the camera matrix, gamma index = truncate toward
  zero, a negative result wraps to 65535 exactly like `LIMIT16((uint32_t)result)` in the shared and
  the intrinsics kernel (reproduced on purpose: it is what CPU preview does; see the finding below).

### Tests (`GpuPreviewProcessing.Direct8Anchored*`, `CpuRouteSelection*`)

They call the real `applyProcessingObject8` (the shared kernel for contrast / S-H / vibrance, the
intrinsics kernel for neutral receipts on an AVX2 host — the dispatch latched by the process, logged
as `intrin=`), compare its 8-bit frame with the display shader's output `>> 8` (per-sample tolerance
1 code, max 3, 0.1% mismatch) and, for every cell, also run the shader as the 16-bit route against
`apply_processing_object` (the 4/32/0.1% 16-bit budget of the earlier tests). Cells: neutral (creative
off and on), a bare pivot, vibrance 1.03 / 1.60 / 0.70 at 6500 K, 2500 K tint +30 and 10000 K tint -30,
sol r2's two repro pixels, sol r1 / fable r1's pixels with vibrance and with contrast, contrast
alone (three settings, three WBs), shadows alone, highlights alone, both, contrast + vibrance, the
night preset at 3000 K and 6500 K (the S/H blur is the kernel's own, attached the way production
attaches it), and two real-clip frames through the whole direct8 render. 38 `[DIRECT8-PARITY]`
comparisons across the step's tests, all within 1 code of the route they name (the 16-bit
comparisons of the older cells within the old budget).
`CpuRouteSelectionMatchesEnginePredicates` pins, over states the shader cannot render
(saturation, AgX, sharpen, LUT, filter, grain, no camera matrix), that the host's route choice and
the clamp flag equal what the engine source says; the `HostRoute*` tests pin the cheapness
refusals and the call site.

**Red-first.** `Direct8AnchoredLegacyUnconditionalClampIsDetected` forces `preCameraClamp = true`
(exactly the shader of #212 at 8e928529) on sol r2's second repro and requires the direct8
comparison to FAIL (max 6 codes, every sample off by more than 1) while the host-chosen flag passes
(max 1). Measured, not source-derived: sol r2's pixel A (leveled `[40000,61000,43000]`) differs by 3
codes between the routes and pixel B (`[27000,61000,60000]`) by 6, smaller than the ~10 sol's
arithmetic suggested.

### The CPU routes still disagree (INFORMATION, not gated; out of scope here)

Making them identical is **CPU-DIRECT8-16BIT-CLAMP-UNIFY** (changes shipped preview/export pixels,
needs its own review and a golden refresh). Direct8 against the 16-bit loop on the same frames, in
8-bit codes (max / mean; `[CPU-ROUTE-DELTA]` in the test output):

| cell | max | mean |
|---|---|---|
| neutral, pivot-only, every contrast / S-H cell, contrast + vibrance, night preset | 0 | 0 |
| vibrance 1.03 / 1.60 / 0.70 at 6500 K (ramp to 2x the 16-bit range) | 10 / 12 / 9 | 0.68 / 1.09 / 0.47 |
| vibrance 1.30 at 2500 K tint +30 | 15 | 0.69 |
| sol r2 pixels A / B (vibrance 1.03) | 3 / 6 | 2.0 / 3.3 |
| **vibrance 1.30 at 10000 K tint -30 (degenerate WB, no over-range)** | **207** | **94** |

The last row is the `(uint32_t)` wrap: at that WB the camera-matrix output goes negative where luma
is not positive, the direct8 kernel indexes gamma at 65535 and the frame goes bright, where the
16-bit loop clamps to 0. The display shader follows direct8 (max 1 code against it, 10 of 65535
against the 16-bit loop). It is flagged for the unify card; it is a degenerate input (tint beyond
what the UI offers), not a covered case.

### Hosted CI: parity tests that SKIP are not parity (fable r2)

The display-parity tests used to skip when the offscreen GL backend probe failed, and no hosted log
showed whether they ran. The probe builds the offscreen **subset** shader, which binds more than 16
samplers; Qt's bundled software GL (`opengl32sw.dll`, Mesa 11.2 llvmpipe) refuses it with "Too many
fragment shader texture samplers" although the **display** shader (9 samplers) runs on it, so a
runner that has only that GL skipped everything. Now: every display-parity test renders through
`render_display_for_parity`, which proceeds whenever the display shader itself renders (whatever the
subset probe says), FAILS when it does not render on a backend the probe calls working, and, when
nothing renders, SKIPS on a developer box but FAILS if `MLVAPP_REQUIRE_DISPLAY_PARITY_GL=1`.
The Product Oracles job has a dedicated step, "Display parity (software GL required, never
skipped)", that forces `QT_OPENGL=software` with that variable set, runs the engine-anchored,
real-frame, direct8-anchored and `HostRoute*` tests plus the kill-switch render test (22), and fails
on any skip, failure, fewer than 22 tests or fewer than 36 direct8 comparisons. The same
configuration (Qt's `opengl32sw.dll`, `QT_OPENGL=software`) was run locally: 22 tests / 1040
assertions / 0 skipped / 0 failed. The shards still skip where there is no GL; the new step proves the tests ran.

## The two shaders

`platform/qt/GpuPreviewProcessing.cpp` builds two GLSL fragment shaders from one
`GpuPreviewProcessingConfig`:

- **DISPLAY shader** (`gpuPreviewProcessingDisplayFragmentShaderSource`, ~line 1331 pre-round):
  bound by `GpuDisplayWindow::paintGL` / `GpuDisplayViewport::paintGL` for the live CUDA
  no-readback texture-present fast path. Round-0 state: `levelsLut` / `matrixLutR,G,B` /
  `gammaLut` only (raw levels, WB/camera matrix, gamma).
- **SUBSET shader** (`gpuPreviewProcessingSubsetFragmentShaderSource`, ~line 1527 pre-round):
  used by the offscreen GPU path (`gpuPreviewProcessingApplyGpuOffscreen`, exports/tests), not
  by live playback presentation. Round-0 state: every ported Look Assist stage (contrast,
  vibrance, saturation, hue-vs, shadows/highlights, vignette, highlight recon, gradient, LUT,
  AgX, toning, creative curves).

The live playback fast path (`RenderFrameThread.cpp` `gpuTexNrSkipCandidate` /
`skipCpuDebayerForGpuTextureNoReadback`, ~line 4056) hands the AMaZE-reconned post-WB-undo
texture straight to the DISPLAY shader. Whatever the DISPLAY shader does not implement, the
owner never sees during playback — regardless of what the SUBSET shader or the CPU path do.

## Parameter-by-parameter trace (round-0 / before this round's fix)

| Parameter | `processingObject_t` field | `GpuPreviewProcessingConfig` | DISPLAY shader (round 0) | Verdict |
|---|---|---|---|---|
| Exposure | `exposure_stops` | folded into `pre_calc_matrix` (neg.) / `pre_calc_gamma` (pos.) — `GpuPreviewProcessing.cpp:2388-2392` | `matrixLutR/G/B`, `gammaLut` bound (`gpuPreviewProcessingBindDisplayUniformsAndTextures`) | **APPLIED** |
| White balance / tint | `proper_wb_matrix`, baked into `pre_calc_matrix` | `matrixLutR/G/B` | bound | **APPLIED** |
| Raw levels | `pre_calc_levels` | `levelsLut` | bound | **APPLIED** |
| Contrast + pivot | `contrast`, `pivot` → `contrast_curve[65536]` | `applyInLoopContrast`, `inLoopContrastCurve` (`GpuPreviewProcessing.cpp:2475-2489`) | **no `inLoopContrastCurve` uniform, no `previewApplyInLoopContrast` branch** in `gpuPreviewProcessingDisplayFragmentShaderSource` | **DROPPED** |
| Shadows / highlights | `shadows_highlights.{shadow_highlight_curve, blur}` | `applyShadowsHighlights`, `shadowsHighlightsCurve`, `shadowsHighlightsBlur` (`GpuPreviewProcessing.cpp:2529-2544`) | **no S/H uniforms at all**; frame-state attach itself was skipped for this path (see below) | **DROPPED** |
| Vibrance | `vibrance` | `applyVibrance`, `vibrance` | **no `previewApplyVibrance`/`previewVibrance` uniform** | **DROPPED** |
| Chroma smooth | `cs_zone.{use_cs, chroma_blur_radius}` | `applyChroma`, `chromaBlurRadius` | not present in either shader (SUBSET applies it as a separate GPU post-pass, `applyChromaPostPassGpu`, not in-shader) — the live fast path never runs that post-pass | **DROPPED** (open this round, see below) |

## The S/H drop was two bugs stacked, not one

1. **The gate**: `gpuPreviewProcessingDisplayShaderUsesShadowsHighlightsFrameState()`
   (`GpuPreviewProcessing.cpp`, pre-round ~line 2749) was hardcoded:
   ```cpp
   Q_UNUSED(config);
   return false;
   ```
   with a comment asserting the display shader "applies only levels, matrix/WB, gamma" — an
   accurate description of what it did, not a requirement.

2. **The consequence**: `RenderFrameThread.cpp:4078-4086` computes
   `gpuTexNrDisplayLutOnlyShStateBypass` from that gate. When true, the block at
   `RenderFrameThread.cpp:4622-4658` takes the `if (... && gpuTexNrDisplayLutOnlyShStateBypass)`
   branch and **never calls** `gpuPreviewProcessingAttachFrameState()` — the fast CPU-side
   approximate-debayer + blur refresh already implemented at `RenderFrameThread.cpp:4121-4191`
   (`gpuTexNrFastShFrameStateEligible` / `debayerBasicU16` +
   `processingRefreshShadowsHighlightsBlurFromRgb16`) runs for nothing; its result is discarded,
   and `slot.presentationContext.gpuPreviewProcessingConfig.shadowsHighlightsBlur` is left
   empty. This IS what `gpu_tex_nr_display_lut_only_sh_frame_state_bypass` in the hub's raw
   finding refers to.

   `slot.presentationContext` flows verbatim through `ReadyFrame::presentationContext` →
   `MainWindow.cpp:28021 requestContext = readyFrame.presentationContext;` →
   `MainWindow.cpp:28518 gpuPresentationOptions.previewProcessing = gpuPreviewProcessingConfig;`
   → `GpuDisplayWindow::setPresentedGpuPlaybackReconAmazePostWbTexture` /
   `GpuDisplayViewport::setPresentedGpuPlaybackReconAmazePostWbTexture` (`options.previewProcessing`)
   — end to end, so even if the DISPLAY shader had S/H uniforms, they would have had no blur data
   to sample from `gpuTexNrDisplayLutOnlyShStateBypass` was true.

Fixing only the gate (item 1) without the shader (item 2 needing new GLSL) would have added the
fast-blur CPU cost with zero visible effect — a pure regression. Fixing only the shader without
the gate would have compiled correct GLSL that always read a stale/empty blur texture. Both were
required together; see "Fix" below.

## `env` gate referenced by the hub finding

`MLVAPP_GPU_TEX_NR_DISPLAY_LUT_ONLY_SKIP_SH_STATE`.

**Round 1 claimed this variable was still a valid A/B switch. It was not** (fable r1 hardening):
`gpuTexNrDisplayLutOnlyShStateBypass` was `envEnabled && needsFrameState &&
!displayShaderUsesShadowsHighlightsFrameState && ...`, and the fix above made
`displayShaderUsesShadowsHighlightsFrameState` true whenever S/H is requested, so no value of the
variable could force the bypass any more. **Round 2 restored a working kill switch**, as one pure
function, `gpuPreviewProcessingDisplayShadowsHighlightsFrameStateBypassed(config, envValue)`, that
`RenderFrameThread` (`gpuPlaybackReconDisplayShadowsHighlightsFrameStateBypassed`) and the pipeline
tests both call:

| `MLVAPP_GPU_TEX_NR_DISPLAY_LUT_ONLY_SKIP_SH_STATE` | S/H requested | result |
|---|---|---|
| unset (default) | yes | **not bypassed**: the display shader applies S/H (the fix) |
| `0` | yes | never bypassed |
| any other value, including set-but-empty | yes | **KILL SWITCH: bypass forced**, S/H is left out of the display shader and its CPU cost is not paid |
| any | no / config disabled | nothing to bypass |

`GpuPreviewProcessing.ShadowsHighlightsKillSwitchForcesDisplayBypass` tests the table;
`ShadowsHighlightsKillSwitchBypassRestoresPreFixLook` renders through the display shader and proves a
bypassed frame is byte-identical to the S/H-off frame (the pre-fix look, i.e. the A/B baseline) and
differs from the S/H-applied frame. The per-frame eligibility terms (texture-present requested, scale
1, candidate, output mode) are unchanged. The hardware A/B itself is not done: it needs a venue
(follow-up card CUDA-LOOK-SH-COST-HARDWARE-MEASURE-1, below).

## Fix (this round)

1. `gpuPreviewProcessingDisplayShaderUsesShadowsHighlightsFrameState()` now returns
   `config.enabled && config.applyShadowsHighlights` — flips `gpuTexNrDisplayLutOnlyShStateBypass`
   off whenever S/H is actually requested, so the existing fast-blur computation's result reaches
   `slot.presentationContext.gpuPreviewProcessingConfig.shadowsHighlightsBlur`.
2. `gpuPreviewProcessingDisplayFragmentShaderSource` gained a GLSL port of:
   - the SUBSET shader's `previewApplyInLoopContrast` branch (contrast+pivot, luma-weighted
     `(R*4+G*11+B)/16` multiply against `inLoopContrastCurve`), applied to `matrixApplied`
     pre-gamma, in the same relative position as the SUBSET shader (before the WB/gamut step);
   - the SUBSET shader's `previewApplyShadowsHighlights` branch (spatial blur lookup →
     `shadowsHighlightsCurve` → multiply into `matrixApplied`), same position;
   - the SUBSET shader's `previewApplyVibrance` branch (saturation-weighted blend), applied
     post-gamma to `result`, same position.
   Chroma smooth is **not** added this round (see Disclosed-open).
3. `GpuPreviewProcessingLutTextureSet` gained `contrastCurve` / `shadowsHighlightsCurve`
   (signature-cached, same lifecycle as the existing 5 LUTs — rebuilt only when
   `config.signature` changes) and `shadowsHighlightsBlur` (tracked separately: re-uploaded on
   **every** present call via the new `gpuPreviewProcessingUpdateShadowsHighlightsBlurTexture`,
   since its content is per-frame, not per-signature).
4. `GpuDisplayWindow::setPresentedGpuPlaybackReconAmazePostWbTexture` and
   `GpuDisplayViewport::setPresentedGpuPlaybackReconAmazePostWbTexture` both call the new
   per-frame blur-texture update right after the existing signature-cached LUT update.
5. Orientation note (worth a reviewer's attention): the DISPLAY shader's `frameTextureMode==0`
   branch samples `frameTexture` at `vTexCoord` **unflipped** (matches
   `GpuDisplayWindow::paintGL`'s own quad, "screen-top maps to texture v=0" — a *different*
   v-mapping than the generic `kQuadVertices` the SUBSET offscreen helper uses, which is why that
   shader flips with `1.0 - vTexCoord.y`). The new S/H blur-texture lookup follows the DISPLAY
   shader's own (unflipped) convention, not the SUBSET shader's, so it samples the same pixel the
   frame texture itself samples for that fragment. See the in-source comment on
   `gpuPreviewProcessingApplyDisplayGpuOffscreen` for the derivation and the test-only quad this
   required.
6. Failure contract (corrected in round 2; round 1 called it a soft degrade for all three
   textures, which is wrong for two of them): only the **per-frame S/H blur texture** degrades
   softly. If its upload fails, `gpuPreviewProcessingBindDisplayUniformsAndTextures` binds
   `previewApplyShadowsHighlights` to `0.0` for that frame and the rest keeps drawing. A failure
   creating or uploading the **contrast or S/H curve texture** destroys the whole LUT set
   (`gpuPreviewProcessingUpdateLutTextureSet`) and the recon presentation is **refused**
   (fail-closed, like the 5 core LUTs, per GPU-TEXNR-S1-DARK-GREEN-1). The header comment on
   `gpuPreviewProcessingUpdateShadowsHighlightsBlurTexture` also says callers must record the drop;
   both presenters currently discard its return value, which stays DISCLOSED-OPEN below.
7. (round 2) The raw-Bayer16 route of `GpuDisplayViewport` shares the LUT set and program with the
   AMaZE route but never refreshes the S/H blur and samples the frame y-flipped, so after an AMaZE
   present with S/H on, a Bayer16 frame would have bound a stale blur (fable r1). That route now
   marks `shadowsHighlightsBlurReady = false` and draws without S/H (a disclosed gap, not a stale
   look). Not traced: whether `MainWindow` can alternate the two routes in one session.
   (PARITY-2, fable r2) Every other `GpuDisplayViewport` route that draws through that program and
   LUT set but uploads no blur — `setPresentedImage`, `setPresentedRgb16`, `setPresentedBayer16`,
   `setPresentedAmazePostWbTexture` — now does the same through
   `gpuPreviewProcessingMarkShadowsHighlightsBlurStale`; only
   `setPresentedGpuPlaybackReconAmazePostWbTexture` uploads a fresh blur.
   `ViewportPresentRoutesNeverBindAStaleShadowsHighlightsBlur` enumerates the viewport's
   `setPresented*` definitions in the source and fails if one does neither (red on 8e928529: it
   names the four routes; a new route must make the same choice). `GpuDisplayWindow` has no other
   route that draws with preview processing.

## Cost measured / not measured (round 2 update)

**Correction to round 1's framing**: `gpuTexNrDisplayLutOnlyShStateBypass`'s own enabling flag,
`gpuPlaybackReconDisplayLutOnlySkipShadowsHighlightsFrameStateEnabled()` (`RenderFrameThread.cpp:578`),
defaults **on** (enabled unless `MLVAPP_GPU_TEX_NR_DISPLAY_LUT_ONLY_SKIP_SH_STATE=0` is set). Combined
with round 1's hardcoded-false gate, `gpuTexNrFastShFrameStateEligible` was **false by default before
this round's fix** — the CPU-side blur refresh below was NOT being paid on this specific CUDA
texture-present fast path pre-round in the default configuration (only when that env var was
explicitly overridden). Round 1's "already being paid… in some configurations" undersold how new
this cost is for the common case; corrected here.

Round 2 now has a working local GL backend (round 1 reported none — `platform/qt/GpuDisplayWindow.cpp`
had been built against a stray Qt 5.15.2 kit rather than the project's pinned Qt 6.10.2 + MinGW 13.1,
which is why offscreen GL context creation failed; see `docs/10-build-windows.md`), so both halves of
round 1's "not measured" gap now have local numbers, added by
`GpuPreviewProcessing.DisplayShaderFastPathFrameCostBudget` (renamed
`...CostInformational` in LAND r2; its p90 figures below are sums of independent p90s, see the
caveat under "Re-measured at landing")
(`tests/pipeline/test_gpu_preview_processing.cpp`):

- **CPU fast S/H refresh (trustworthy, host-independent)** — `debayerBasicU16` +
  `processingRefreshShadowsHighlightsBlurFromRgb16` at this fixture's real 1808x2268 size, run through
  the *exact* preview-mode state `RenderFrameThread.cpp:4152-4178` sets around this call
  (`processingSetPlaybackPreviewMode(1)`, aggressive-preview off by default, scale factor 1). That
  state matters: at scale 1 with preview mode on and aggressive preview mode off,
  `processing_standard_x1_shadows_highlights_quarterres_enabled()` (`raw_processing.c:264-286`)
  engages the existing quarter-resolution RBF blur path instead of full-resolution — measuring without
  it first (a mistake caught and corrected this round) gave misleadingly high numbers (p90 up to
  ~87 ms combined) that did not reflect what production actually runs.
  - 16 threads (`hardware_concurrency()`, this host): combined debayer+refresh **p50 22.4 ms / p90
    25.0 ms** — 56% / 63% of a 40 ms budget.
  - 1 thread (this test binary's own forced-single-threaded determinism mode — a worst-case bound,
    not what real playback uses): combined **p50 29.1 ms / p90 41.9 ms** — 73% / 105% of budget.
  - Verdict: material (56-105% of the whole frame budget for one CPU stage, before any GPU work) but
    **not an obvious throughput halving** at realistic thread counts, because the existing
    quarter-resolution downsample path already keeps it there once invoked correctly. No code change
    made this round: the mitigation this item asked for ("keep the fast path fast") is already in
    place and already engaged by the call site; what was missing was measurement proving that, not a
    missing optimization. DISCLOSED-OPEN for a follow-up round: the single-threaded worst case exceeds
    budget at p90 — worth an explicit low-thread-count fallback (e.g. auto-engaging aggressive preview
    mode under contention) if the bachelor run or a low-core-count host shows it materializing.
- **Blur-texture-upload / display-shader-draw (NOT trustworthy as absolute numbers)** — measured via
  the public `gpuPreviewProcessingApplyDisplayGpuOffscreen()` entry point in a loop: **p50 363 ms / p90
  415 ms per call**, renderer `llvmpipe (LLVM 5.0.1, 256 bits)` (software). Two reasons these are not
  usable as real per-frame GPU cost: (a) this box has no hardware GL, only Mesa's software rasterizer,
  which is not representative of the owner's real GPU by orders of magnitude; (b) unlike the live
  `GpuDisplayWindow`/`GpuDisplayViewport` presenters, this public entry point creates a fresh GL
  context, shader program and full 7-texture LUT set on *every* call instead of reusing the
  signature-cached ones the live path keeps across frames, so even on real hardware this number would
  overstate steady-state per-frame cost. No lower-overhead public surface exists to benchmark against
  without exposing more of `GpuPreviewProcessing.cpp`'s internals, which this round did not do.
  Real per-frame GPU numbers need the bachelor bench (`bench-plan.md`) on actual hardware, which is
  exactly what item 5 exists for — this box cannot produce a trustworthy substitute for that.

### Re-measured at landing (LAND-2, merged tree on master d489e09a)

**Caveat added in round 2 (sol r1 hardening):** every "combined p90" in this document and in the
earlier test output is the p90 of the debayer samples **plus** the p90 of the refresh samples, which
is not the p90 of the per-frame total (two sample sets `[1 x8, 30 x2]` and `[30 x2, 1 x8]` pair to a
p90 of 31 ms while their independent p90s add to 60 ms). The numbers below are therefore an upper
bound on the real per-frame figure, not a measurement of it. The paired figures are in "Paired
re-measure" after this table.

`GpuPreviewProcessing.DisplayShaderFastPathFrameCostBudget` (since renamed, see below), run three
times back to back on the merged build (i9-13900KS, 16 logical CPUs, a shared VM with other lanes
active; renderer `llvmpipe (LLVM 5.0.1, 256 bits)`, no hardware GL). Sum of independent p90s,
debayer + S/H blur refresh, ms:

| run | 16 threads p50 / p90 | 1 thread p50 / p90 |
|---|---|---|
| 1 | 43.1 / 60.2 | 49.7 / 62.0 |
| 2 | 33.1 / 40.8 | 46.1 / 55.5 |
| 3 | 31.2 / 35.2 | 35.5 / 48.9 |

Against the 40 ms budget this is 88-150% (16 threads, p90) and 122-155% (1 thread, p90): worse
than the 25.0 ms / 41.9 ms measured on the branch, and noisy run to run. The cause was not
isolated: the host was shared and loaded, so these numbers cannot be called a regression in the
code nor cleared as noise. The test records the figures and asserts no budget, so it passes
either way. The single-threaded and low-core-count S/H cost therefore stays DISCLOSED-OPEN, and
a quiet-host or bachelor-hardware measurement is still needed before claiming it fits the frame
budget. The software-GL offscreen call measured p50 520-624 ms / p90 658-792 ms, still not a
usable GPU figure for the reasons given above.

### Paired re-measure (LAND r2) and what the cost test now is

The test is renamed `GpuPreviewProcessing.DisplayShaderFastPathFrameCostInformational` because it
never asserted the budget its old name promised. It now times the debayer and the refresh on the
**same iteration**, stores their sum per iteration, and reports percentiles of that paired cost
(`fast_sh_paired_frame_ms_p50/p90/p99`). Its only assertions are the measurement's own invariants:
every iteration was timed, each paired sample is at least either component, the paired p90 is at
least either component's p90, and p50 <= p90 <= p99. Iterations were cut from 20 to 12 (and the
software-GL calls from 21 to 9) to bound its time inside the 240 s shard cap.

One run on this loaded shared VM (llvmpipe, other lanes active, so it is neither a regression nor a
clearance): 16 threads **paired** p50 31.0 / p90 44.0 / p99 47.2 ms; 1 thread paired p50 38.6 /
p90 44.0 / p99 53.4 ms, against a 40 ms frame budget. Still over budget at p90 on this host. **Not
established by any of this**: steady-state CUDA play-through cadence with S/H on, the real GPU cost
of the new full-frame 8-bytes-per-pixel CPU pack and synchronous GL upload the blur needs per
frame (`gpuPreviewProcessingUpdateShadowsHighlightsBlurTexture`, 32.8 MB at 1808x2268), and the
effect of the kill switch on frame time. That evidence needs a quiet host with a real GPU and is the
follow-up card **CUDA-LOOK-SH-COST-HARDWARE-MEASURE-1** (needs a venue; not started here). Remedies
to try there if the hardware A/B shows a cost: upload RGB16 without the RGBA repack, or use a PBO.

## Engine-anchored parity (LAND r2)

### What the engine really does (the two review readings are one statement)

Sol r1 read the engine as "pre-camera clamp and uint16 truncation"; fable r1 read it as "multiplies
the UNclamped WB-boosted value and clamps afterwards". Both describe the same lines. In the generic
16-bit loop (`raw_processing.c` ~3243-3422) `pm0/pm4/pm8[level]` are the **unclamped** int32
diagonal-matrix values (blue is x2.86 at 3000 K, so `pre_calc_matrix[8][30000]` = 85,899);
`expo_correction` (vignette x shadows/highlights x contrast, accumulated in double) multiplies them
in float; and only then does `pix[i] = LIMIT16(...)` clamp and store to `uint16_t`, which truncates.
The contrast index `cval = (4R+11G+B)>>4` ("Contrast on untouched pixel", ~3283) and the
shadows/highlights index (from the blur through the same unclamped `pm` tables, ~3263) are taken from
the **unclamped** values and clamped only as a curve index (`LIMIT16`). The camera matrix, gamut
compression, `pix = LIMIT16(result)` (truncating) and `pre_calc_gamma[pix]` follow.

Round 1 clamped the LUT to 16 bits in `gpuPreviewProcessingBuildConfig` and multiplied after, in the
shader AND in the C++ mirror, so every pixel whose WB-boosted channel or whose factor-scaled channel
passed 65535 took a different path through the camera matrix: sol's repro (WB 6500, contrast 0.14
pivot 0.46, leveled `[27000,61000,43000]`) gave GLSL `[64090,64346,64293]` against the engine's
`[60454,64592,61692]`.

### What changed

- `GpuPreviewProcessingConfig::matrixLutRaw{R,G,B}`: the unclamped `pre_calc_matrix` diagonal as
  signed int32 limited to [-2^20, 2^20-1] (more than 16x over-range, so the 4R+11G+B luma sum is
  exact in float32; a degenerate tint gives a negative green gain, which the engine keeps in its luma
  sums). They enter `config.signature` **only** when contrast or S/H is on, so every other config's
  signature, and the pinned golden signatures, are unchanged.
- The matrix LUT textures carry the raw value in G/B (`raw + 2^20 = G + 65536*B`); R stays the
  16-bit clamped value, so nothing that reads `.r` changed. `sampleMatrixRaw` rebuilds it.
- Display shader (not the subset shader): contrast and S/H luma from
  the raw diagonal; one `expoCorrection`; `pix = floor(clamp(diagonal * expoCorrection, 0, 65535))`;
  camera matrix and gamut compression in code units; `gammaIndex = floor(clamp(pix, 0, 65535))` and a
  gamma read by integer index (the engine truncates, the round-1 shader rounded the index);
  vibrance reads its input rounded. The C++ mirror follows the same pre-camera order
  (`matrixRawValue`, double `expoCorrection`, `floor(clamp16(...))`).
- Found by the sweep and fixed **in the display shader only**: the gamut compression used the
  **blue-channel** Reinhard curve for green; the engine uses the plain `ReinhardTonemap_f` for
  green, the red curve for red and the blue curve for blue (`raw_processing.c` ~3523). The two
  curves differ for every green below luma, so this moved warm and saturated pixels (the fable
  repro was 923 codes off with the clamp order fixed and this not). The same fix in the C++
  mirror changed 4 of the 15 pinned golden hashes
  (`tiny_dual_iso.gpu_preview_subset.{frame0,frame1,exposure_0_75.frame0}` and
  `preview_processing.cpu.frame0`; the signatures did not move), and the golden artifact must not
  move, so it was **reverted** in the mirror and the subset shader.
- The unchanged parts of the mirror and the subset shader (they are tested against each other, and
  the mirror is MainWindow's CPU fallback): they still **round** the post-camera gamma index where
  the engine and the display shader truncate, and they keep the blue curve for green in the gamut
  step, both because the golden hashes depend on them. So they agree with the engine on the
  pre-camera order (the BLOCKER) but are **not** an engine oracle at the 1-LSB level (up to 18
  codes, mean 1.4, on the ramp frames, and a visible difference on greens below luma), and no
  test claims they are. Closing that gap means re-ratifying the golden: CUDA-LOOK-MIRROR-ENGINE-EXACT-1.
- Golden: `TinyDualIsoReceiptSubsetGoldenOutputIsStable` and the other six producers still produce
  the tracked 15 keys byte-for-byte (proof in the run summary).

### Evidence

Red on the round-1 source (`1446c8ac` production code, engine-anchored tests only added), llvmpipe:
`sol_r1_flat_pixel_6500K` max diff **1016** codes (gpu == mirror `[52213,52280,52265]`, engine
`[51197,52344,51552]`), `fable_r1_flat_pixel_3000K` max **923**, the 6500 K contrast ramp max
**1620** (the run logs are in the lane run folder, `red6.out.txt`). After: `sol_r1_flat_pixel_6500K`
**0**, `fable_r1_flat_pixel_3000K` **0**, and the sweep below.

| case (`EngineAnchored*`, 128x64 ramp to 130,000 diagonal codes unless noted) | max diff (codes) | mean |
|---|---|---|
| contrast +0.14/0.46, -0.30/0.50, +0.60/0.30 at 6500 K | 2 / 3 / 3 | 0.14 |
| contrast at 2500 K (+0.14, +0.60 tint +30) | 1 / 7 | 0.13-0.14 |
| contrast at 3000 K (-0.30), 10000 K (+0.14; +0.60 tint -30) | 2 / 4 / 9 | 0.14-0.15 |
| vibrance 1.03, 1.60, 0.70 at 6500 K; 1.30 at 2500 K | 2 / 2 / 2 / 3 | 0.15-0.66 |
| contrast + vibrance at 3000 K and 10000 K | 2 / 2 | 0.61-0.63 |
| rounding floor (contrast 0.02, ramp capped at 60,000) | 5 | 0.16 |
| S/H +32/-26 at 3000 K and 6500 K; +60/-60 at 3000 K (engine blur) | 2 / 3 / 7 | 0.14-0.15 |
| night preset (contrast 14/46, S/H 32/-26, vibrance 3) at 3000 K and 6500 K | 5 / 3 | 0.64-0.66 |
| real clip frame (1808x2268, whole `getMlvProcessedFrame16` render): contrast / vibrance / S/H / combined | 13 / 17 / 19 / 18 | 0.19 / 0.68 / 0.30 / 0.77 |

Tolerances (`kEngineParity*`): 4 codes per sample, 32 max, 0.1% of samples above 4. They were set
from these measurements (worst case 19 on the real frame, 9 on the sweeps; worst fraction of samples
above 4 is 8.7e-5 on the real frame and 1.6e-4 on a ramp), with 1.7x on the worst case for a
different GL driver, not from the old mirror tolerances. They sit more than an order of magnitude
under the defects they catch (900-13,000 codes). The residual is float32/double accumulation and
LUT-boundary rounding; no other cause is claimed.

### Not covered / engine-internal findings (open, nothing here resolves them)

- **The engine is not self-consistent about the pre-camera clamp** (LAND r2 text, superseded by
  "DECLARED PARITY REFERENCE" above): the generic 16-bit loop clamps and truncates, the direct-8-bit
  kernel and the basic-matrix branch do not unless AgX / local tone is active. PARITY-2 measured the
  disagreement (table above) and made the display shader follow the route CPU playback takes; the
  CPU routes themselves are unchanged (CPU-DIRECT8-16BIT-CLAMP-UNIFY).
- The engine casts a negative float to `uint32_t` in the fast path and kernel (undefined; observed
  `gamma[65535]` for an out-of-gamut blue), while the generic loop clamps first. The display shader
  now reproduces the cast on the unclamped (direct8 / basic-matrix) route and clamps on the
  clamped route.
- At degenerate white balance (tint beyond the UI range) the gamut compression is ill-conditioned
  near luma 0: the mirror differs from the engine by up to 1,938 codes there, the display shader by
  9. That is a property of the input, not a coverage claim.
- The tiny clip is dark (maximum leveled value ~7,400), so WB over-range is exercised on synthetic
  frames built in diagonal-matrix space, not on real footage. Real-frame tests anchor the stage
  order on real texture only.
- llvmpipe only: no hardware GL, no CUDA. The same tests run on the 4090 are a venue item.
- Still not implemented by the display shader (unchanged): see the list at the top.
- Hosted CI skip behaviour: see "Hosted CI: parity tests that SKIP are not parity" above. Whether
  the hosted runner now runs them is read from the new step's log, not assumed.

## DISCLOSED-OPEN (round-1 v2.1 contract: no silent drop)

- **Chroma smooth** remains dropped on the live DISPLAY-shader fast path this round. It is
  **not silently dropped**: `config.applyChroma` is still visible in
  `slot.presentationContext.gpuPreviewProcessingConfig` and was already NOT part of the
  `gpuPreviewProcessingIsSupported()` gate that governs the SUBSET/offscreen path either — the
  SUBSET shader itself does not apply chroma smooth in-shader; it is a separate GPU post-pass
  (`applyChromaPostPassGpu`) that only the offscreen/export path runs. Remedy for a follow-up
  round: either (a) a second offscreen box-blur pass inserted into the live present path before
  the DISPLAY shader draw (reusing the existing `gpuPreviewProcessingApplyBoxBlurOffscreen`
  infrastructure, which already implements the exact `blur_image` port needed), at the
  measured cost of one extra render-to-texture round trip per frame, or (b) fold a bounded-radius
  chroma blur into the DISPLAY shader itself as a second sampling pass over `shadowsHighlightsBlurTexture`-style
  per-frame texture, avoiding the extra pass at the cost of shader complexity. Telemetry: this
  round did not add a dedicated per-frame "chroma dropped" telemetry key (existing
  `slot.presentationContext.gpuPreviewProcessingConfig.applyChroma` is visible to any caller that
  wants to check it, but nothing inserts it into `stageTimingTelemetry` today) — DISCLOSED-OPEN,
  remedy: add `gpu_playback_recon_display_shader_chroma_smooth_dropped` next to the existing
  `gpu_preview_processing_shadows_highlights_*` keys in `RenderFrameThread.cpp:4622-4658`.
- **GL-upload-specific telemetry for contrast/S-H** (distinct from "was frame-state data ready",
  which IS telemetried per-frame via the now-actually-firing
  `gpu_preview_processing_shadows_highlights_frame_state_ready` /
  `..._frame_state_reason` keys at `RenderFrameThread.cpp:4622-4658`): if
  `gpuPreviewProcessingUpdateLutTextureSet` or
  `gpuPreviewProcessingUpdateShadowsHighlightsBlurTexture` fails at the GL level (lost context,
  driver rejects upload) *after* frame-state data was ready, that specific failure mode is not
  separately telemetried — it follows the same precedent as the pre-existing 5-LUT upload failure
  path, which is also not individually telemetried beyond the hard-refusal `reason` string.
  DISCLOSED-OPEN, remedy: a small public accessor on `GpuDisplayWindow` /
  `GpuDisplayViewport` (`lastPresentedShadowsHighlightsApplied()`) that `MainWindow.cpp`'s
  texture-present block reads and inserts into `readyFrame.stageTimingTelemetry`, mirroring the
  existing `texturePresentReason` pattern.

## Follow-up cards (named here, none started)

- **CUDA-LOOK-SH-COST-HARDWARE-MEASURE-1** (needs a venue): quiet host, real GPU, S/H on versus the
  kill switch, play-through cadence and per-frame cost of the blur pack/upload.
- **CUDA-LOOK-DISPLAY-STAGES-1**: creative curves, toning, saturation, hue-vs, chroma smooth, and
  the subset-only stages (AgX, gradient, highlight recon, LUT, vignette).
- **CUDA-LOOK-MIRROR-ENGINE-EXACT-1**: make the C++ mirror and the subset shader truncate the
  post-camera gamma index and use the plain Reinhard curve for green like the engine and the
  display shader. Moves 4 pinned golden hashes, so it needs a golden re-ratification.
- **CPU-DIRECT8-16BIT-CLAMP-UNIFY** (queued, replaces CUDA-LOOK-ENGINE-KERNEL-CLAMP-DIVERGENCE-1):
  make the CPU routes byte-identical (the direct8 kernel and the basic-matrix branch omit the
  pre-camera clamp/truncation the generic loop applies; the direct8 `(uint32_t)` wrap sends
  out-of-gamut pixels to gamma[65535]). Changes shipped preview/export pixels: its own review and a
  golden refresh. When it lands, `processingCpuRoutePreCameraClamps` collapses to a constant.
- Surface the S/H blur-texture drop to telemetry (both presenters discard the return value).

## Files changed (PARITY-2 on top of #212)

- `src/processing/raw_processing.c` / `.h` — `processingCpuRoutePreCameraClamps` (reads the direct8
  kernel's and the basic-matrix branch's own predicates).
- `src/mlv/video_mlv.c` / `.h` — `mlvPreviewPlaybackCpuRoute`; `platform/qt/GpuPreviewHostRoute.h`.
- `platform/qt/GpuPreviewProcessing.h` / `.cpp` — `preCameraClamp`, `previewPreCameraClamp` uniform and
  the display shader's two route-dependent lines, `gpuPreviewProcessingApplyCpuRoute`,
  `gpuPreviewProcessingMarkShadowsHighlightsBlurStale`.
- `platform/qt/RenderFrameThread.cpp` — applies the route per frame (config and presentation
  options) and records it in telemetry.
- `platform/qt/GpuDisplayViewport.cpp` — the four routes that upload no S/H blur mark it stale.
- `tests/pipeline/test_gpu_preview_processing.cpp` — direct8-anchored cells, route-selection test,
  viewport-route contract test, `render_display_for_parity` backend policy.
- `.github/workflows/tests.yml` — "Display parity (software GL required, never skipped)" step.
- `tests/fixtures/golden/pipeline_hashes.provenance.json` — source pin rebind only; the golden
  artifact is unchanged.

## Files changed (LAND r2 on top of round 1)

- `platform/qt/GpuPreviewProcessing.h` / `.cpp` — raw diagonal LUTs and their texture packing,
  display/subset shader and mirror pre-camera order, exact gamma truncation and the plain green
  gamut tonemap in the display shader only, `gpuPreviewProcessingDisplayShadowsHighlightsFrameStateBypassed`.
- `platform/qt/RenderFrameThread.cpp` — the bypass gate calls that function (kill switch restored).
- `platform/qt/GpuDisplayViewport.cpp` — Bayer16 route no longer binds a stale blur.
- `tests/pipeline/test_gpu_preview_processing.cpp` — mirror display tests replaced by
  engine-anchored ones, kill-switch tests, paired cost percentiles.
- `tests/fixtures/golden/pipeline_hashes.provenance.json` — source pin rebind only (see the PR).

## Files changed in round 1

- `platform/qt/GpuPreviewProcessing.h` / `.cpp` — shader source, LUT-texture-set fields, new
  per-frame blur-texture update function, gate flip, new
  `gpuPreviewProcessingApplyDisplayGpuOffscreen` test-support entry point.
- `platform/qt/GpuDisplayWindow.cpp`, `platform/qt/GpuDisplayViewport.cpp` — wire the new
  per-frame blur update into the existing per-present LUT update call site.
- `tests/pipeline/test_gpu_preview_processing.cpp` — updated the test that had locked in the old
  (buggy) gate behavior; added parity + no-silent-drop tests (see round summary for names).
