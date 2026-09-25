# CUDA playback Look Assist parity — round 1 inventory

CUDA-PLAYBACK-LOOK-PARITY-1, round 1. Traces every Look Assist parameter from MainWindow's
apply path through `processingObject_t` / `GpuPreviewProcessingConfig` to whichever shader the
live CUDA texture-present fast path (`GpuDisplayWindow` / `GpuDisplayViewport`
`presentGpuPlaybackReconAmazePostWbTexture*`) actually draws with, per file:line evidence.

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

`MLVAPP_GPU_TEX_NR_DISPLAY_LUT_ONLY_SKIP_SH_STATE`
(`RenderFrameThread.cpp:582-584`, backing `gpuPlaybackReconDisplayLutOnlySkipShadowsHighlightsFrameStateEnabled()`)
is a *separate*, independently-gated bypass, defaulting to the code-gate above
(`gpuPreviewProcessingDisplayShaderUsesShadowsHighlightsFrameState`) but overridable via env for
diagnostics. It is untouched by this round: with the shader now consuming S/H, setting this env
var to force the bypass is still a valid way to A/B the display-LUT-only fast path against the
full one, and both `gpuTexNrDisplayLutOnlyShStateBypass`'s inputs are still ANDed together
(`RenderFrameThread.cpp:4078-4086`).

## Fix (this round)

1. `gpuPreviewProcessingDisplayShaderUsesShadowsHighlightsFrameState()` now returns
   `config.enabled && config.applyShadowsHighlights` — flips `gpuTexNrDisplayLutOnlyShStateBypass`
   off whenever S/H is actually requested, so the existing fast-blur computation's result reaches
   `slot.presentationContext.gpuPreviewProcessingConfig.shadowsHighlightsBlur`.
2. `gpuPreviewProcessingDisplayFragmentShaderSource` gained the exact GLSL port (byte-identical
   formulas, ported verbatim) of:
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
6. A **soft-degrade** contract, not a hard refusal: if the contrast/S-H curve texture upload or
   the per-frame blur upload fails, `gpuPreviewProcessingBindDisplayUniformsAndTextures` binds
   that one stage's `previewApply*` uniform to `0.0` for this frame — never destroys the whole
   recon presentation over a look-parity miss (levels/matrix/gamma keep drawing). This differs
   from how the 5 core LUTs fail (hard refusal, matching the pre-existing
   GPU-TEXNR-S1-DARK-GREEN-1 FAIL CLOSED policy for those) because a levels/matrix/gamma failure
   produces a wrong-color image (the incident that policy exists to prevent), while a
   contrast/S-H/vibrance miss produces a still-correct, merely flatter image.

## Cost measured / not measured this round

- **Measured (local, this box)**: the fast CPU-side approximate blur refresh
  (`RenderFrameThread.cpp:4121-4191`, `gpuTexNrFastShDebayerMs` + `gpuTexNrFastShRefreshMs`
  telemetry, already existed pre-round) was **already being paid** even before this fix in some
  configurations (whenever `gpuTexNrFastShFrameStateEligible` was true and the display-only
  bypass was NOT the active gate) — this round does not change that cost, only whether its
  *result* reaches the screen.
- **Not measured this round**: (a) the new per-frame `shadowsHighlightsBlur` texture upload cost
  (`gpuPreviewProcessingUpdateShadowsHighlightsBlurTexture`, a full `width*height*4*uint16`
  `glTexImage`-equivalent upload every present call) — this box has no GL backend at all
  (`QOpenGLContext creation failed` even with `MLVAPP_GPU_PREVIEW_ALLOW_SOFTWARE=1`; see
  `tests/pipeline/test_gpu_preview_processing.cpp`'s `DisplayShader*MatchesCpuReference` tests,
  which SKIP for that reason, consistent with every other pre-existing GPU-offscreen test in the
  suite); (b) the two new signature-cached curve-texture rebuilds (cheap relative to the 5
  existing ones — same 256x256 R32F class — but not measured on hardware this round). The
  benchmark handoff (`bench-plan.md`) asks the bachelor run to capture both.

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

## Files changed this round

- `platform/qt/GpuPreviewProcessing.h` / `.cpp` — shader source, LUT-texture-set fields, new
  per-frame blur-texture update function, gate flip, new
  `gpuPreviewProcessingApplyDisplayGpuOffscreen` test-support entry point.
- `platform/qt/GpuDisplayWindow.cpp`, `platform/qt/GpuDisplayViewport.cpp` — wire the new
  per-frame blur update into the existing per-present LUT update call site.
- `tests/pipeline/test_gpu_preview_processing.cpp` — updated the test that had locked in the old
  (buggy) gate behavior; added parity + no-silent-drop tests (see round summary for names).
