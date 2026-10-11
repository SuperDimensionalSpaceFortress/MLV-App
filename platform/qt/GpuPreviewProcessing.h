#ifndef GPUPREVIEWPROCESSING_H
#define GPUPREVIEWPROCESSING_H

#include "../../src/processing/processing_object.h"
#include <QByteArray>
#include <QOpenGLShaderProgram>
#include <QOpenGLTexture>
#include <QString>
#include <QStringList>
#include <QVector2D>

#include <cstdint>

/* PLAYBACK-GL-PRESENT-SETUP-STALL-1: the config fields `signature` hashes, split into
 * groups so a LUT texture-set rebuild can name what moved (the first group whose digest
 * differs from the one the set was built from). The first nine are texture content; the
 * last four reach the display shader only as uniforms. The digests are computed next to
 * `signature` in gpuPreviewProcessingBuildConfig and never change it. */
enum GpuPreviewProcessingLutGroup
{
    GpuPreviewLutGroupLevels = 0,
    GpuPreviewLutGroupMatrix,
    GpuPreviewLutGroupMatrixRaw,
    GpuPreviewLutGroupGamma,
    GpuPreviewLutGroupContrastCurve,
    GpuPreviewLutGroupShCurve,
    GpuPreviewLutGroupCreative,
    GpuPreviewLutGroupHueVs,
    GpuPreviewLutGroupLut3d,
    GpuPreviewLutGroupHighestGreen,
    GpuPreviewLutGroupHighestGreenDiso,
    GpuPreviewLutGroupWbMatrix,
    GpuPreviewLutGroupOtherUniforms,
    GpuPreviewLutGroupCount
};
/* Static lower-case group name ("levels", ..., "other_uniforms"); "unknown" out of range. */
const char * gpuPreviewProcessingLutGroupName(int group);

/* PLAYBACK-GL-PRESENT-SETUP-STALL-1: where one GUI-thread texture-present setup block
 * (GpuDisplayWindow / GpuDisplayViewport setupMs) spent its wall time. CPU wall time only
 * (QElapsedTimer reads, no GL query or fence). lut_miss is a static string: "none" when
 * the LUT set was not rebuilt, else the first differing group, "unbuilt" when the set
 * had no valid key, or "unknown" when every group digest matched. */
struct GpuPresentSetupTiming
{
    double program_ms = 0.0;
    double lut_ms = 0.0;
    bool lut_rebuilt = false;
    const char * lut_miss = "none";
    double blur_drain_ms = 0.0;
    double blur_upload_ms = 0.0;
    double blur_check_ms = 0.0;
    double realloc_ms = 0.0;
    double sampling_ms = 0.0;
};

struct GpuPreviewProcessingConfig
{
    bool enabled = false;
    bool useCameraMatrix = false;
    bool applyGamutCompression = false;
    /* The LIVE DISPLAY shader's pre-camera clamp (see
     * gpuPreviewProcessingApplyCpuRoute): true clamps + truncates the WB-matrix
     * output to uint16 before the camera matrix like the generic 16-bit loop,
     * false feeds it unclamped like the direct-8-bit kernel without AgX/local
     * tone. Default true = the historical shader behaviour. Not part of the
     * signature: it is a uniform, not a texture. */
    bool preCameraClamp = true;
    float sourceExposureStops = 0.0f;
    float properWbMatrix[9] = { 0.0f };
    float rgbToY[3] = { 0.0f };
    QByteArray levelsLut;
    QByteArray matrixLutR;
    QByteArray matrixLutG;
    QByteArray matrixLutB;
    /* UNCLAMPED diagonal-matrix (WB) LUTs, int32[65536] each, in 16-bit code
     * units. matrixLut{R,G,B} above is the same table clamped to 16 bits; the
     * engine keeps the full value because contrast and shadows/highlights read
     * their luma from it and multiply it BEFORE clamping (pix = (uint16_t)LIMIT16
     * (pm[...] * expo_correction), raw_processing.c ~3420). A WB-boosted channel
     * reaches several times 65535 (blue x2.86 at 3000 K), so the clamped copy
     * cannot reproduce the engine once the factor is not 1. Values are limited
     * to [-2^20, 2^20-1] (more than 16x over-range; a degenerate tint gives a
     * negative green gain, which the engine also keeps) so the 4R+11G+B luma sum
     * stays exactly representable in float32 on the GPU. Empty = fall back to
     * the clamped LUT (hand-built configs). */
    QByteArray matrixLutRawR;
    QByteArray matrixLutRawG;
    QByteArray matrixLutRawB;
    QByteArray gammaLut;
    bool applyCreativeCurves = false;
    bool applyToning = false;
    float toningGain[3] = { 1.0f, 1.0f, 1.0f };
    bool applyVibrance = false;
    float vibrance = 1.0f;
    bool applySaturation = false;
    float saturation = 1.0f;
    bool applyHueVs = false;
    bool applyInLoopContrast = false;
    bool applyAgx = false;
    float agxForward[9] = { 0.0f };
    float agxInverse[9] = { 0.0f };
    bool applyVignette = false;
    int vignetteStrength = 0;
    QByteArray vignetteMask;
    bool applyShadowsHighlights = false;
    bool shadowsHighlightsCurveIndexMask = false;
    bool shadowsHighlightsFrameStateReady = false;
    int shadowsHighlightsFrameWidth = 0;
    int shadowsHighlightsFrameHeight = 0;
    /* PLAYBACK-SH-OFF-CPU-PATH-1: true when shadowsHighlightsBlur holds the
     * quarter-res RBF output ((frame width / 4) x (frame height / 4) RGB16)
     * instead of the full-res blur. Consumers reproduce the engine's two 2x
     * bilinear upsample stages exactly (the display shader in GLSL, the CPU
     * reference via gpuPreviewProcessingResolveFullResShadowsHighlightsBlur). */
    bool shadowsHighlightsBlurQuarter = false;
    QByteArray shadowsHighlightsBlur;
    QByteArray shadowsHighlightsCurve;
    bool applyLut = false;
    bool lut3d = false;
    int lutDimension = 0;
    float lutIntensity = 1.0f;
    float lutDomainMin[3] = { 0.0f, 0.0f, 0.0f };
    float lutDomainMax[3] = { 1.0f, 1.0f, 1.0f };
    QByteArray lutCube;
    bool applyHighlightReconstruction = false;
    bool highlightReconDualIso = false;
    int highestGreen = 0;
    int highestGreenDiso = 0;
    bool applyGradient = false;
    bool applyGradientContrast = false;
    int gradientHighestGreen = 0;
    int gradientHighestGreenDiso = 0;
    QByteArray gradientMatrixLutR;
    QByteArray gradientMatrixLutG;
    QByteArray gradientMatrixLutB;
    QByteArray gradientGammaLut;
    QByteArray gradientContrastCurve;
    bool applyChroma = false;
    int chromaBlurRadius = 0;
    bool applySharpen = false;
    double sharpenA = 1.0;
    double sharpenX = 0.0;
    double sharpenY = 0.0;
    bool applyMedian = false;
    int medianWindow = 0;
    int medianStrength = 0;
    /* Raw pointer into processing->gradient_mask (uint16, frame-sized). The
     * gradient mask has no companion length on the processing object, so it is
     * carried as a pointer and read by the callers, which know the frame
     * dimensions (CPU reference by pixel index, GPU offscreen by width*height).
     * Production must keep this current per frame (like highest_green_diso). */
    const uint16_t * gradientMaskData = nullptr;
    float sourceContrast = 0.0f;
    QByteArray inLoopContrastCurve;
    QByteArray contrastCurveLut;
    QByteArray gradationLutY;
    QByteArray gradationLutR;
    QByteArray gradationLutG;
    QByteArray gradationLutB;
    QByteArray hueVsHueCurve;
    QByteArray hueVsSaturationCurve;
    QByteArray hueVsLumaCurve;
    QByteArray lumaVsSaturationCurve;
    uint64_t signature = 0;
    /* The unclamped diagonal-matrix LUTs (matrixLutRaw*) feed the camera matrix
     * directly on the unclamped CPU route (preCameraClamp == false), whatever the
     * contrast / shadows-highlights state, and are uploaded in the matrix LUT
     * textures. `signature` hashes them only when contrast or S/H is on (so the
     * pinned golden signatures never move), so the TEXTURE caches key on this
     * second hash as well; see gpuPreviewProcessingRawLutSignature. */
    uint64_t rawLutSignature = 0;
    /* PLAYBACK-GL-PRESENT-SETUP-STALL-1: per-group digests (GpuPreviewProcessingLutGroup),
     * for naming a LUT-set rebuild only. Zero on hand-built configs. */
    uint64_t lutGroupDigests[GpuPreviewLutGroupCount] = { 0 };
};

/* Hash of the three unclamped matrix LUTs, always (not gated like `signature`). */
uint64_t gpuPreviewProcessingRawLutSignature(const GpuPreviewProcessingConfig & config);

const char * gpuPreviewProcessingEnvironmentVariableName(void);
bool gpuPreviewProcessingRequestedByEnvironment(void);
QByteArray gpuPreviewProcessingVertexShaderSource(void);
QByteArray gpuPreviewProcessingDisplayFragmentShaderSource(void);
QByteArray gpuPreviewProcessingSubsetFragmentShaderSource(void);
QByteArray gpuPreviewProcessingPackLookupTextureRgba16(const QByteArray & sourceLut);
/* Matrix-LUT variant: R = the 16-bit clamped value (what every shader that
 * does not need the over-range reads), G = low 16 bits and B = bits 16..19 of
 * the unclamped value, so the display shader can rebuild it exactly
 * (raw + 2^20 = G + B*65536, in code units; the bias keeps negative gains
 * representable). rawLut (int32[65536]) may be empty, in which case raw = the
 * clamped value. */
QByteArray gpuPreviewProcessingPackMatrixLookupTextureRgba16(const QByteArray & clampedLut,
                                                             const QByteArray & rawLut);
/* PLAYBACK-SEEK-RENDER-PARITY-1: the creative curves (pre_calc_curve_r, then
 * gcurve_y, then gcurve_r/g/b) composed per channel into ONE RGBA16 256x256
 * lookup texture for the display shader: R[x] = gcurve_r[gcurve_y[curve[x]]],
 * G and B likewise. Integer table composition, so it is bit-exact with the
 * engine's three sequential lookups (raw_processing.c, the creative-curve and
 * gradation loops; raw_processing_8bit_kernel.inc ~339-352). Empty LUTs in the
 * config compose as identity. */
QByteArray gpuPreviewProcessingComposeCreativeCurvesRgba16(const GpuPreviewProcessingConfig & config);
/* The four hue-vs / luma-vs curves (float[36000] each) packed into one RGBA32F
 * 256x141 texture: R = hue-vs-hue, G = hue-vs-saturation, B = hue-vs-luma,
 * A = luma-vs-saturation. */
QByteArray gpuPreviewProcessingPackHueVsCurvesRgba32F(const GpuPreviewProcessingConfig & config);
/* PLAYBACK-SEEK-RENDER-PARITY-1: the stages the LIVE DISPLAY shader does NOT
 * apply although the subset (and therefore the texture route's admission gate,
 * gpuPreviewProcessingIsSupported) accepts them. Any of these active means the
 * display shader would show a different look than the engine, so the CUDA
 * texture route must refuse and fall back to a route that applies them. Stable
 * lower-case tokens, in pipeline order: "vignette", "highlight_reconstruction",
 * "gradient", "lut", "chroma_separation", "sharpen", "median_denoise". Empty =
 * the display shader applies every active stage. The config form reads the
 * config's own stage flags; the processing-object form is the cheap per-frame
 * policy predicate and is pinned against the config form by a test. */
QStringList gpuPreviewProcessingDisplayShaderRefusedStages(const GpuPreviewProcessingConfig & config);
QStringList gpuPreviewProcessingDisplayShaderRefusedStages(const processingObject_t * processing);
/* "display_shader_refused_stages=<comma list>" or empty; the typed telemetry value. */
QString gpuPreviewProcessingDisplayShaderRefusalReason(const QStringList & refusedStages);
bool gpuPreviewProcessingRendererIsSoftware(const QString & rendererDescription);
bool gpuPreviewProcessingIsSupported(const processingObject_t * processing,
                                     QString * reason = nullptr);
GpuPreviewProcessingConfig gpuPreviewProcessingBuildConfig(
    const processingObject_t * processing,
    QString * reason = nullptr);
bool gpuPreviewProcessingNeedsShadowsHighlightsFrameState(
    const GpuPreviewProcessingConfig & config);
bool gpuPreviewProcessingDisplayShaderUsesShadowsHighlightsFrameState(
    const GpuPreviewProcessingConfig & config);
/* Whether the fast shadows/highlights frame-state path is bypassed for the live
 * display shader (RenderFrameThread's gpuTexNrDisplayLutOnlyShStateBypass, minus
 * its per-frame eligibility terms). skipShStateEnvironmentValue is
 * qEnvironmentVariable("MLVAPP_GPU_TEX_NR_DISPLAY_LUT_ONLY_SKIP_SH_STATE"): null
 * (unset) = bypass only if the display shader cannot apply S/H, which is never
 * now; "0" = never bypass; any other value = KILL SWITCH, force the bypass so the
 * S/H display path can be A/B'd on hardware. False when S/H is not requested. */
bool gpuPreviewProcessingDisplayShadowsHighlightsFrameStateBypassed(
    const GpuPreviewProcessingConfig & config,
    const QString & skipShStateEnvironmentValue);
bool gpuPreviewProcessingHasShadowsHighlightsFrameState(
    const GpuPreviewProcessingConfig & config,
    int width,
    int height);
/* Declares the DISPLAY PARITY REFERENCE: the display shader reproduces the CPU
 * preview route for the current state. direct8Route is the engine's own
 * route decision (mlvPreviewPlaybackCpuRoute, reached through
 * gpuPreviewHostCpuRoute: direct8-eligible AND input cheap at the playback
 * scale); the clamp flag is then asked of the engine
 * (processingCpuRoutePreCameraClamps), never re-derived here. */
void gpuPreviewProcessingApplyCpuRoute(GpuPreviewProcessingConfig * config,
                                       const processingObject_t * processing,
                                       bool direct8Route);
/* Attaches the current S/H frame state: the quarter-res blur when the engine's
 * last refresh was the quarter-only one for this width x height
 * (processingGetShadowsHighlightsQuarterBlurData), else the full-res blur. */
bool gpuPreviewProcessingAttachFrameState(GpuPreviewProcessingConfig * config,
                                          const processingObject_t * processing,
                                          int width,
                                          int height,
                                          QString * reason = nullptr);
/* Returns config itself when it carries no quarter-res blur; otherwise fills
 * *expanded with a copy whose blur is expanded to full res by the engine's own
 * upsample stages (bit-identical to the legacy full-res blur) and returns it. */
const GpuPreviewProcessingConfig & gpuPreviewProcessingResolveFullResShadowsHighlightsBlur(
    const GpuPreviewProcessingConfig & config,
    int width,
    int height,
    GpuPreviewProcessingConfig * expanded);
struct GpuPreviewProcessingBackendAvailability
{
    bool available = false;
    QString reason;
    QString rendererDescription;
};
GpuPreviewProcessingBackendAvailability gpuPreviewProcessingProbeGpuBackend(void);
void gpuPreviewProcessingApplyCpuReference(const GpuPreviewProcessingConfig & config,
                                           const uint16_t * inputRgb16,
                                           uint16_t * outputRgb16,
                                           int width,
                                           int height);
/* CPU-PLAYBACK-PREP-WORKER-BUILD-1: per-stage attribution of one CPU reference
 * run (QElapsedTimer ns spans, reported in ms) and the active-stage bitmask. */
enum GpuPreviewProcessingCpuStageBit : uint32_t
{
    GpuPreviewCpuStageVignette                = 1u << 0,
    GpuPreviewCpuStageShadowsHighlights       = 1u << 1,
    GpuPreviewCpuStageShadowsHighlightsQuarter = 1u << 2,
    GpuPreviewCpuStageGradient                = 1u << 3,
    GpuPreviewCpuStageLut                     = 1u << 4,
    GpuPreviewCpuStageHighlightRecon          = 1u << 5,
    GpuPreviewCpuStageChroma                  = 1u << 6,
    GpuPreviewCpuStageSharpen                 = 1u << 7,
    GpuPreviewCpuStageMedian                  = 1u << 8
};
struct GpuPreviewProcessingCpuSpans
{
    double shExpandMs = 0.0;
    double pointwiseMs = 0.0;
    double chromaMs = 0.0;
    double sharpenMs = 0.0;
    double medianMs = 0.0;
    double rgb16to8Ms = 0.0;
    uint32_t stageMask = 0;
    /* true when >>8 ran inside the pointwise loop (no spatial post-pass). */
    bool fused8 = false;
};
uint32_t gpuPreviewProcessingCpuStageMask(const GpuPreviewProcessingConfig & config);
/* Chroma, sharpen or median: the stages that read neighbouring pixels after
 * the pointwise pass. */
bool gpuPreviewProcessingCpuHasSpatialPostPass(const GpuPreviewProcessingConfig & config);
/* CPU-PLAYBACK-PREP-WORKER-BUILD-1 D1: the byte-identical tier-1 items (row-
 * parallel box blur/chroma/sharpen/median, the parallel S/H quarter-blur
 * expansion, the fused 8-bit pass, the prep thread's persistent scratch and
 * shared avir pool) are OFF unless MLVAPP_PLAYBACK_PREP_TIER1 is exactly "1"
 * (strict opt-in: "0", "off", "", whitespace, " 1" and "1garbage" are all off).
 * Read on every call. */
const char * gpuPreviewProcessingTier1SwitchName(void);
bool gpuPreviewProcessingTier1Enabled(void);
/* gpuPreviewProcessingApplyCpuReference with per-stage spans (spans may be null). */
void gpuPreviewProcessingApplyCpuReferenceTimed(const GpuPreviewProcessingConfig & config,
                                                const uint16_t * inputRgb16,
                                                uint16_t * outputRgb16,
                                                int width,
                                                int height,
                                                GpuPreviewProcessingCpuSpans * spans);
/* The CPU reference straight to 8-bit, byte-identical to
 * gpuPreviewProcessingApplyCpuReference followed by a per-sample >> 8. With no
 * spatial post-pass the >> 8 is fused into the pointwise loop and scratch16 is
 * not touched; otherwise the 16-bit passes run in scratch16 (width*height*3
 * words, caller-owned, contents ignored) and are then converted. With tier 1
 * off and a scratch16 given, the 16-bit route is taken even without a post-pass. */
void gpuPreviewProcessingApplyCpuReferenceTo8(const GpuPreviewProcessingConfig & config,
                                              const uint16_t * inputRgb16,
                                              uint16_t * scratch16,
                                              uint8_t * outputRgb8,
                                              int width,
                                              int height,
                                              GpuPreviewProcessingCpuSpans * spans);
bool gpuPreviewProcessingApplyGpuOffscreen(const GpuPreviewProcessingConfig & config,
                                           const uint16_t * inputRgb16,
                                           uint16_t * outputRgb16,
                                           int width,
                                           int height,
                                           QString * reason = nullptr,
                                           QString * rendererDescription = nullptr);
/* CUDA-PLAYBACK-LOOK-PARITY-1: same contract as
 * gpuPreviewProcessingApplyGpuOffscreen, but renders through the DISPLAY
 * shader (gpuPreviewProcessingDisplayFragmentShaderSource) via the exact
 * production binding functions GpuDisplayWindow/GpuDisplayViewport use, for
 * parity tests against the live CUDA texture-present fast path. */
bool gpuPreviewProcessingApplyDisplayGpuOffscreen(const GpuPreviewProcessingConfig & config,
                                                  const uint16_t * inputRgb16,
                                                  uint16_t * outputRgb16,
                                                  int width,
                                                  int height,
                                                  QString * reason = nullptr,
                                                  QString * rendererDescription = nullptr);

/* Shared shader-program setup + LUT texture set (levels + R/G/B matrix + gamma) for
 * the preview-processing DISPLAY fragment shader
 * (gpuPreviewProcessingDisplayFragmentShaderSource): levels LUT, WB/matrix LUTs,
 * proper-WB rows, gamut compression, gamma. One GL-context-bound presenter owns one
 * of each; used by BOTH GpuDisplayViewport and GpuDisplayWindow so recon/preview
 * textures draw through the identical shader and LUT upload/bind path on both routes
 * instead of each keeping a private copy (GPU-TEXNR-S1-DARK-GREEN-1).
 */
struct GpuPreviewProcessingLutTextureSet
{
    QOpenGLTexture * levels = nullptr;
    QOpenGLTexture * matrixR = nullptr;
    QOpenGLTexture * matrixG = nullptr;
    QOpenGLTexture * matrixB = nullptr;
    QOpenGLTexture * gamma = nullptr;
    /* CUDA-PLAYBACK-LOOK-PARITY-1: contrast+pivot and shadows/highlights curve
     * LUTs for the live DISPLAY shader. Signature-cached like the five above
     * (rebuilt only when config.signature changes). */
    QOpenGLTexture * contrastCurve = nullptr;
    QOpenGLTexture * shadowsHighlightsCurve = nullptr;
    /* PLAYBACK-SEEK-RENDER-PARITY-1: the composed creative-curve LUT
     * (gpuPreviewProcessingComposeCreativeCurvesRgba16) and the packed hue-vs
     * curves (gpuPreviewProcessingPackHueVsCurvesRgba32F). Built only when the
     * config applies the stage; while it does, the set is not ready without
     * them (fail closed: never present with the stage silently dropped). */
    QOpenGLTexture * creativeCurves = nullptr;
    QOpenGLTexture * hueVsCurves = nullptr;
    uint64_t signature = 0;
    uint64_t rawLutSignature = 0;   /* config.rawLutSignature the matrix textures were built from */
    bool signatureValid = false;
    /* config.lutGroupDigests the set was built from (names the group on the next miss). */
    uint64_t lutGroupDigests[GpuPreviewLutGroupCount] = { 0 };
    /* The shadows/highlights BLUR texture is per-FRAME content (the spatial
     * low-pass of the current frame), not per-settings-signature, so it is
     * tracked and re-uploaded separately every frame by
     * gpuPreviewProcessingUpdateShadowsHighlightsBlurTexture -- never gated by
     * `signature`/`signatureValid` above. */
    QOpenGLTexture * shadowsHighlightsBlur = nullptr;
    int shadowsHighlightsBlurWidth = 0;
    int shadowsHighlightsBlurHeight = 0;
    bool shadowsHighlightsBlurReady = false;
    bool shadowsHighlightsBlurQuarter = false;   /* texture holds the quarter-res blur */
    /* PLAYBACK-GPU-EPISODIC-STALL-1 r2: the GL_PIXEL_UNPACK_BUFFER the steady blur
     * upload goes through in GpuShBlurUploadPbo mode (0 = not created yet). */
    unsigned int shadowsHighlightsBlurPbo = 0;
};

/* PLAYBACK-GPU-EPISODIC-STALL-1 r2: how the STEADY (same-size, already-allocated)
 * shadows/highlights blur upload reaches its texture. The allocation upload is never
 * affected. Client = glTexSubImage2D from client memory (the pre-r2 path); Skip = no
 * steady upload at all, the texture keeps what it last held, in a skip-only session the
 * allocation-time blur (MEASUREMENT ONLY, the picture is wrong); Pbo = the app maps a GL_PIXEL_UNPACK_BUFFER, packs the blur
 * into it, and glTexSubImage2D reads from offset 0 (pixel-identical to Client, and no
 * driver call reads client memory). A context that cannot map a buffer range falls
 * back to Client for that call. */
enum GpuShBlurUploadMode
{
    GpuShBlurUploadClient = 0,
    GpuShBlurUploadSkip = 1,
    GpuShBlurUploadPbo = 2
};
/* "MLVAPP_GPU_SH_BLUR_UPLOAD_MODE". */
const char * gpuPreviewProcessingShBlurUploadModeSwitchName(void);
/* value = qEnvironmentVariable(switch name). Exactly "client", "skip" or "pbo"
 * (case-insensitive, surrounding whitespace ignored) select that mode; null, empty
 * and every other value select the default, Client. */
GpuShBlurUploadMode gpuPreviewProcessingShBlurUploadModeFromValue(const QString & value);
const char * gpuPreviewProcessingShBlurUploadModeName(GpuShBlurUploadMode mode);
/* The mode from the environment, read on every call. */
GpuShBlurUploadMode gpuPreviewProcessingShBlurUploadMode(void);

/* A presenter route that does NOT upload a fresh per-frame shadows/highlights
 * blur must call this, so the display shader binds S/H off for that frame
 * instead of sampling a blur left over from an earlier route (the LUT set is
 * shared between the routes of one presenter). Routes that DO upload one go
 * through gpuPreviewProcessingUpdateShadowsHighlightsBlurTexture, which sets
 * the flag itself. */
void gpuPreviewProcessingMarkShadowsHighlightsBlurStale(GpuPreviewProcessingLutTextureSet * set);

/* Per-presenter display knobs for gpuPreviewProcessingBindDisplayUniformsAndTextures,
 * beyond the GpuPreviewProcessingConfig itself. */
struct GpuPreviewProcessingDisplayUniforms
{
    QVector2D textureSize;
    int frameTextureMode = 0;   // 0 = already-debayered RGBA, 1 = raw Bayer16
    int samplingMode = 0;       // GpuDisplayViewport::SamplingMode
    bool zebraEnabled = false;
    float zebraUnderThreshold = 0.0f;
    float zebraOverThreshold = 0.0f;
};

/* Compiles+links the shared display vertex/fragment shader pair into *program if it
 * is not already set. Returns false (and leaves *program null) on a compile/link
 * failure, logged via qWarning. shaderParent becomes the QOpenGLShaderProgram's
 * QObject parent (typically the presenter widget/window itself). */
bool gpuPreviewProcessingEnsureDisplayProgram(QOpenGLShaderProgram *& program,
                                              QObject * shaderParent);
QOpenGLTexture * gpuPreviewProcessingCreateOrResizeLookupTexture(QOpenGLTexture * texture,
                                                                 int width,
                                                                 int height);
void gpuPreviewProcessingDestroyLutTextureSet(GpuPreviewProcessingLutTextureSet & set);
/* Rebuilds/uploads the 5 LUT textures from config when its signature changed (dirty-
 * tracked via config.signature); destroys them and no-ops when config.enabled is
 * false. Safe to call every frame. setupTiming (optional) gets lut_rebuilt and lut_miss
 * (PLAYBACK-GL-PRESENT-SETUP-STALL-1); the group comparison runs on a miss only. */
void gpuPreviewProcessingUpdateLutTextureSet(GpuPreviewProcessingLutTextureSet & set,
                                             const GpuPreviewProcessingConfig & config,
                                             GpuPresentSetupTiming * setupTiming = nullptr);
/* True when the set was built from exactly this config's LUT content: both
 * `signature` and `rawLutSignature` match (the texture cache key). */
bool gpuPreviewProcessingLutTextureSetKeyMatches(const GpuPreviewProcessingLutTextureSet & set,
                                                 const GpuPreviewProcessingConfig & config);
/* PLAYBACK-GL-PRESENT-SETUP-STALL-1 (GL-free): the lut_miss name for a rebuild of `set`
 * from `config` -- "unbuilt" when the set has no valid key, else the first group whose
 * digest differs, else "unknown". */
const char * gpuPreviewProcessingLutTextureSetMissGroup(const GpuPreviewProcessingLutTextureSet & set,
                                                        const GpuPreviewProcessingConfig & config);
bool gpuPreviewProcessingLutTextureSetReady(const GpuPreviewProcessingLutTextureSet & set,
                                            const GpuPreviewProcessingConfig & config);
/* CUDA-PLAYBACK-LOOK-PARITY-1: uploads/refreshes the per-frame shadows/
 * highlights blur texture (set.shadowsHighlightsBlur) from
 * config.shadowsHighlightsBlur, sized width x height. Unlike the signature-
 * cached LUTs above, this runs unconditionally every call when
 * config.applyShadowsHighlights is true, because the blur content changes
 * every frame during playback. No-ops (leaves shadowsHighlightsBlurReady
 * false) when S/H is not requested or the frame-state bytes do not match
 * width*height*3 uint16 -- the caller then binds previewApplyShadowsHighlights
 * = 0 for this frame and must record the drop via telemetry (round-1 v2.1
 * disclosed-open contract: no silent drop). Safe to call every paint. setupTiming
 * (optional, PLAYBACK-GL-PRESENT-SETUP-STALL-1) gets the glGetError drain, the setData
 * upload and the glGetError check as separate wall spans, and any blur realloc. */
bool gpuPreviewProcessingUpdateShadowsHighlightsBlurTexture(
    GpuPreviewProcessingLutTextureSet & set,
    const GpuPreviewProcessingConfig & config,
    int width,
    int height,
    GpuPresentSetupTiming * setupTiming = nullptr);
/* PLAYBACK-CUDA-HONOUR-SCALE-1 r2: present-gap instrumentation (LIGHT-safe,
 * one qInfo line per event). Presenters note each texture reallocation; the
 * play-stop summary reads the count. The context (smoke session id and last
 * presented frame) is set by MainWindow; gpuPresentEventLogReconLine is the
 * llrpSetGpuReconEventLogger target for the C seam's set_clip/set_luts lines. */
void gpuPresentEventSetContext(quint64 sessionId, qint64 lastPresentedFrame);
void gpuPresentEventNoteTextureRealloc(const char * site, int width, int height);
quint64 gpuPresentEventTextureReallocCount();
void gpuPresentEventLogReconLine(const char * line);
/* Single production decision for whether a presenter must refuse to draw a GPU-recon/
 * AMaZE texture (post-WB-undo linear camera RGB) this paint, rather than ever letting it
 * fall through to the shared shader's previewProcessingEnabled=0 passthrough-equivalent
 * branch. Both GpuDisplayWindow::paintGL and GpuDisplayViewport::paintGL call this exact
 * function for their re-check (GPU-TEXNR-S1-DARK-GREEN-1 round 4) so the two routes
 * cannot diverge. False whenever presentingReconTexture is false -- only-already-
 * display-referred content (the QImage route) is never refused for LUT unreadiness. */
bool gpuPreviewProcessingReconTexturePresentationRefused(
    bool presentingReconTexture,
    const GpuPreviewProcessingLutTextureSet & set,
    const GpuPreviewProcessingConfig & config);
/* Binds every display-shader uniform (including the LUT sampler units 1-5) on
 * `program`, which must already be ->bind()'d by the caller; the caller retains
 * ownership of texture unit 0 (frameTexture). */
void gpuPreviewProcessingBindDisplayUniformsAndTextures(
    QOpenGLShaderProgram * program,
    const GpuPreviewProcessingConfig & config,
    const GpuPreviewProcessingLutTextureSet & lutSet,
    const GpuPreviewProcessingDisplayUniforms & uniforms,
    bool lutsReady);
void gpuPreviewProcessingReleaseDisplayTextures(const GpuPreviewProcessingLutTextureSet & lutSet,
                                                bool lutsReady);

/* Separable integer box blur on the GPU, the bit-exact reproduction of the
 * engine's blur_image (processing.c:589): two render-to-texture passes
 * (horizontal then vertical), each summing the (2*radius+1)-tap window with a
 * truncating integer divide and GL_CLAMP_TO_EDGE addressing. This is the
 * keystone pre-pass for the spatial stages (chroma blur / sharpen / median).
 * do_r/do_g/do_b select which interleaved channels are blurred (disabled
 * channels pass through), matching blur_image's channel flags. Radius must be
 * <= 127 so the integer window sum stays exact in float32. */
QByteArray gpuPreviewProcessingBoxBlurFragmentShaderSource(void);
bool gpuPreviewProcessingApplyBoxBlurOffscreen(const uint16_t * inputRgb16,
                                               uint16_t * outputRgb16,
                                               int width,
                                               int height,
                                               int radius,
                                               bool doR,
                                               bool doG,
                                               bool doB,
                                               QString * reason = nullptr,
                                               QString * rendererDescription = nullptr);

#endif // GPUPREVIEWPROCESSING_H
