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
bool gpuPreviewProcessingAttachFrameState(GpuPreviewProcessingConfig * config,
                                          const processingObject_t * processing,
                                          int width,
                                          int height,
                                          QString * reason = nullptr);
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
    /* The shadows/highlights BLUR texture is per-FRAME content (the spatial
     * low-pass of the current frame), not per-settings-signature, so it is
     * tracked and re-uploaded separately every frame by
     * gpuPreviewProcessingUpdateShadowsHighlightsBlurTexture -- never gated by
     * `signature`/`signatureValid` above. */
    QOpenGLTexture * shadowsHighlightsBlur = nullptr;
    int shadowsHighlightsBlurWidth = 0;
    int shadowsHighlightsBlurHeight = 0;
    bool shadowsHighlightsBlurReady = false;
};

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
 * false. Safe to call every frame. */
void gpuPreviewProcessingUpdateLutTextureSet(GpuPreviewProcessingLutTextureSet & set,
                                             const GpuPreviewProcessingConfig & config);
/* True when the set was built from exactly this config's LUT content: both
 * `signature` and `rawLutSignature` match (the texture cache key). */
bool gpuPreviewProcessingLutTextureSetKeyMatches(const GpuPreviewProcessingLutTextureSet & set,
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
 * disclosed-open contract: no silent drop). Safe to call every paint. */
bool gpuPreviewProcessingUpdateShadowsHighlightsBlurTexture(
    GpuPreviewProcessingLutTextureSet & set,
    const GpuPreviewProcessingConfig & config,
    int width,
    int height);
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
