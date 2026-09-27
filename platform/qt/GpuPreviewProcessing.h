#ifndef GPUPREVIEWPROCESSING_H
#define GPUPREVIEWPROCESSING_H

#include "../../src/processing/processing_object.h"
#include <QByteArray>
#include <QOpenGLShaderProgram>
#include <QOpenGLTexture>
#include <QString>
#include <QVector2D>

#include <cstdint>

struct GpuPreviewProcessingConfig
{
    bool enabled = false;
    bool useCameraMatrix = false;
    bool applyGamutCompression = false;
    float sourceExposureStops = 0.0f;
    float properWbMatrix[9] = { 0.0f };
    float rgbToY[3] = { 0.0f };
    QByteArray levelsLut;
    QByteArray matrixLutR;
    QByteArray matrixLutG;
    QByteArray matrixLutB;
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
};

const char * gpuPreviewProcessingEnvironmentVariableName(void);
bool gpuPreviewProcessingRequestedByEnvironment(void);
QByteArray gpuPreviewProcessingVertexShaderSource(void);
QByteArray gpuPreviewProcessingDisplayFragmentShaderSource(void);
QByteArray gpuPreviewProcessingSubsetFragmentShaderSource(void);
QByteArray gpuPreviewProcessingPackLookupTextureRgba16(const QByteArray & sourceLut);
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
bool gpuPreviewProcessingHasShadowsHighlightsFrameState(
    const GpuPreviewProcessingConfig & config,
    int width,
    int height);
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
    uint64_t signature = 0;
    bool signatureValid = false;
};

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
bool gpuPreviewProcessingLutTextureSetReady(const GpuPreviewProcessingLutTextureSet & set,
                                            const GpuPreviewProcessingConfig & config);
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
