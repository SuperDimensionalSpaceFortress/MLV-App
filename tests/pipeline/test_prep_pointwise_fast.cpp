// CPU-LOOKASSIST-PLAYBACK-COST-1: the prep route's pointwise fast path.
//  F1  it is ADMITTED for the M16-1243 Look Assist ON stage set (levels, WB
//      matrix, full-res shadows/highlights, in-loop contrast, camera matrix +
//      gamut compression, gamma, vibrance, creative curves) and for Look Assist OFF.
//  F2  it is BYTE-IDENTICAL (max abs diff 0, all three channels) to
//      gpuPreviewProcessingApplyCpuReference -- the reference loop, which is the
//      CUDA/GL parity oracle and never takes the fast path -- on a 1808x2268
//      synthetic frame: 16-bit (ApplyCpuReferenceTimed, and To8's 16-bit route,
//      the tier-1-off default) and fused 8-bit, at OpenMP 1 and 4. The team size
//      the pass reports is read inside its region.
//  F3  every parity-fixture stage config: admitted ones match; refused ones name
//      the stage, fall back to the reference loop and still match; the kill
//      switch does the same.
//  F4  opt-in (MLVAPP_PREP_POINTWISE_FAST_BENCH=1, never in CI): reference vs
//      fast on a std::thread, as the prep thread runs it. Printed, never asserted.
// No assertion here reads a duration.
#include "../common/minitest.h"

#include "mlv_pipeline_fixture.h"

#include "../../platform/qt/GpuPreviewProcessing.h"
#include "../../src/processing/raw_processing.h"

#include <QByteArray>
#include <QString>

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <functional>
#include <string>
#include <thread>
#include <vector>
#include <omp.h>

namespace {

/* One environment variable for one scope (unset when value is null). */
struct EnvScope
{
    EnvScope(const char * name, const char * value)
        : name(name), had(qEnvironmentVariableIsSet(name)), previous(qgetenv(name))
    {
        if (value) qputenv(name, QByteArray(value));
        else qunsetenv(name);
    }
    ~EnvScope()
    {
        if (had) qputenv(name, previous);
        else qunsetenv(name);
    }
    const char * name;
    bool had;
    QByteArray previous;
};

struct OmpThreads
{
    explicit OmpThreads(int n) : previous(omp_get_max_threads()) { omp_set_num_threads(n); }
    ~OmpThreads() { omp_set_num_threads(previous); }
    int previous;
};

void openFixture(MlvPipelineFixture & fixture)
{
    QString error_message;
    ASSERT_TRUE(fixture.openTinyDualIso(&error_message));
    ASSERT_TRUE(fixture.loadReceipt(QStringLiteral("tests/fixtures/receipts/tiny_dual_iso_hq.marxml"), &error_message));
    ASSERT_TRUE(fixture.applyReceipt(&error_message));
}

/* Every unported or spatial stage off (test_playback_prep_worker.cpp configureBaseSubset). */
void configureBaseSubset(processingObject_t * processing)
{
    processing->AgX = 0;
    processingDontAllowCreativeAdjustments(processing);
    processing->highlight_reconstruction = 0;
    processing->gradient_enable = 0;
    processing->lut_on = 0;
    processing->filter_on = 0;
    processing->exr_mode = 0;
    processing->denoiserStrength = 0;
    processing->rbfDenoiserLuma = 0;
    processing->rbfDenoiserChroma = 0;
    processing->grainStrength = 0;
    processing->ca_desaturate = 0;
    processing->sharpen = 0.0;
    processing->cs_zone.use_cs = 0;
    processing->cs_zone.chroma_blur_radius = 0;
    processing->clarity = 0.0;
    processing->shadows_highlights.shadows = 0.0;
    processing->shadows_highlights.highlights = 0.0;
    processing->vignette_strength = 0;
    processingSetGamut(processing, GAMUT_Rec709);
}

/* The M16-1243 Look Assist preset (receipt 4402ae33, gui_smoke.visual_state:
 * exposure=380 contrast=55 pivot=50 shadows=-8 highlights=-43 vibrance=11
 * temperature=6485 tint=-12) through MainWindow's slider maps
 * (MainWindow.cpp on_horizontalSlider*_valueChanged). */
void applyM16LookAssistPreset(processingObject_t * p)
{
    processingAllowCreativeAdjustments(p);
    processingSetExposureStops(p, 380 / 100.0 + 1.2);
    processingSetWhiteBalance(p, 6485.0, -12 / 10.0);
    processingSetSimpleContrast(p, 55 / 100.0);
    processingSetPivot(p, 50 / 100.0);
    processingSetShadows(p, -8 * 1.5 / 100.0);
    processingSetHighlights(p, -43 * 1.5 / 100.0);
    processingSetVibrance(p, std::pow((11 + 100) / 200.0 * 2.0, std::log(3.6) / std::log(2.0)));
}

/* Deterministic synthetic RGB16 frame: full-range ramps (every LUT index), a band
 * of clipped / black / single-channel-clipped blocks, and a dark noisy gradient
 * like the real clip (median 32/255 before the preset). */
std::vector<uint16_t> makeSyntheticFrame(int width, int height, uint32_t seed)
{
    std::vector<uint16_t> frame(static_cast<size_t>(width) * height * 3u);
    uint32_t state = seed;
    auto next = [&state]() -> uint32_t { state = state * 1664525u + 1013904223u; return state >> 8; };
    for (int y = 0; y < height; ++y)
        for (int x = 0; x < width; ++x)
        {
            uint16_t * px = frame.data() + (static_cast<size_t>(y) * width + x) * 3u;
            if (y < height / 8)
            {
                const uint32_t v = static_cast<uint32_t>((static_cast<uint64_t>(x) * 65535u) / (width - 1));
                px[0] = static_cast<uint16_t>(v);
                px[1] = static_cast<uint16_t>(65535u - v);
                px[2] = static_cast<uint16_t>((v + 21845u) & 0xFFFFu);
            }
            else if (y < height / 4)
            {
                switch ((x / 64 + y / 64) % 5)
                {
                case 0: px[0] = px[1] = px[2] = 65535; break;
                case 1: px[0] = px[1] = px[2] = 0; break;
                case 2: px[0] = static_cast<uint16_t>(next()); px[1] = 65535; px[2] = static_cast<uint16_t>(next()); break;
                case 3: px[0] = 65535; px[1] = static_cast<uint16_t>(next()); px[2] = 65535; break;
                default:
                    for (int c = 0; c < 3; ++c) px[c] = static_cast<uint16_t>(next());
                    break;
                }
            }
            else
            {
                const double base = 1500.0 + 22000.0 * (static_cast<double>(x) / width)
                                                     * (static_cast<double>(y) / height);
                const double factors[3] = { 1.05, 1.0, 0.82 };
                for (int c = 0; c < 3; ++c)
                {
                    const int noise = static_cast<int>(next() % 4097u) - 2048;
                    const int v = static_cast<int>(base * factors[c]) + noise;
                    px[c] = static_cast<uint16_t>(v < 0 ? 0 : (v > 65535 ? 65535 : v));
                }
            }
        }
    return frame;
}

/* A smooth frame-sized S/H "blur": the 8x8 block mean of the frame. Byte identity
 * does not depend on it being the engine's RBF output, only on it being the
 * same bytes for both loops. */
std::vector<uint16_t> makeBlockBlur(const std::vector<uint16_t> & frame, int width, int height)
{
    std::vector<uint16_t> blur(frame.size());
    for (int by = 0; by < height; by += 8)
        for (int bx = 0; bx < width; bx += 8)
        {
            const int ey = std::min(height, by + 8);
            const int ex = std::min(width, bx + 8);
            for (int c = 0; c < 3; ++c)
            {
                uint64_t sum = 0;
                for (int y = by; y < ey; ++y)
                    for (int x = bx; x < ex; ++x)
                        sum += frame[(static_cast<size_t>(y) * width + x) * 3u + c];
                const uint16_t mean = static_cast<uint16_t>(sum / static_cast<uint64_t>((ey - by) * (ex - bx)));
                for (int y = by; y < ey; ++y)
                    for (int x = bx; x < ex; ++x)
                        blur[(static_cast<size_t>(y) * width + x) * 3u + c] = mean;
            }
        }
    return blur;
}

void attachBlur(GpuPreviewProcessingConfig * config, const std::vector<uint16_t> & blur,
                int width, int height, bool quarter)
{
    config->shadowsHighlightsFrameStateReady = true;
    config->shadowsHighlightsFrameWidth = width;
    config->shadowsHighlightsFrameHeight = height;
    config->shadowsHighlightsBlurQuarter = quarter;
    config->shadowsHighlightsBlur = QByteArray(reinterpret_cast<const char *>(blur.data()),
                                               static_cast<int>(blur.size() * sizeof(uint16_t)));
}

GpuPreviewProcessingConfig buildConfig(MlvPipelineFixture & fixture,
                                       const std::function<void(processingObject_t *)> & setup)
{
    processingObject_t * p = fixture.processing();
    ASSERT_TRUE(p != nullptr);
    configureBaseSubset(p);
    setup(p);
    (void)fixture.renderDebayeredFrame16(0); /* settle the LUTs like the engine sweep tests */
    QString reason;
    if (!gpuPreviewProcessingIsSupported(p, &reason))
        ::minitest::fail(__FILE__, __LINE__, "config supported", reason.toStdString());
    const GpuPreviewProcessingConfig config = gpuPreviewProcessingBuildConfig(p, &reason);
    ASSERT_TRUE(config.enabled);
    return config;
}

int maxAbsDiff16(const std::vector<uint16_t> & a, const std::vector<uint16_t> & b, int * firstIndex)
{
    int worst = 0;
    *firstIndex = -1;
    for (size_t i = 0; i < a.size(); ++i)
    {
        const int d = std::abs(static_cast<int>(a[i]) - static_cast<int>(b[i]));
        if (d > 0 && *firstIndex < 0) *firstIndex = static_cast<int>(i);
        worst = std::max(worst, d);
    }
    return worst;
}

int maxAbsDiff8(const std::vector<uint8_t> & out8, const std::vector<uint16_t> & ref16, int * firstIndex)
{
    int worst = 0;
    *firstIndex = -1;
    for (size_t i = 0; i < out8.size(); ++i)
    {
        const int d = std::abs(static_cast<int>(out8[i]) - static_cast<int>(ref16[i] >> 8));
        if (d > 0 && *firstIndex < 0) *firstIndex = static_cast<int>(i);
        worst = std::max(worst, d);
    }
    return worst;
}

void failDiff(const char * file, int line, const std::string & what, const std::string & label,
              int worst, int firstIndex)
{
    ::minitest::fail(file, line, what,
                     label + " max_abs_diff=" + std::to_string(worst)
                     + " first_sample=" + std::to_string(firstIndex));
}

/* The whole contract for one config on one frame: the reference loop's output,
 * then the 16-bit Timed entry, To8's 16-bit route (tier 1 off, the default) and
 * the fused 8-bit entry, each at OpenMP 1 and 4. expectedRefusal "" = admitted. */
void checkConfig(const std::string & label, const GpuPreviewProcessingConfig & config,
                 const std::vector<uint16_t> & frame, int width, int height,
                 const char * expectedRefusal)
{
    const bool admitted = expectedRefusal[0] == '\0';
    std::vector<uint16_t> ref(frame.size(), 0);
    gpuPreviewProcessingApplyCpuReference(config, frame.data(), ref.data(), width, height);
    const bool spatial = gpuPreviewProcessingCpuHasSpatialPostPass(config);
    for (int threads : { 1, 4 })
    {
        const std::string tag = label + ".omp" + std::to_string(threads);
        const std::string threadText = std::to_string(threads);
        /* test_runtime::force_single_threaded_pipeline sets MLVAPP_FORCE_SINGLETHREAD
         * for the whole binary; the fast path's team comes from
         * mlvappEffectivePlaybackWorkerThreadCount, so MLVAPP_FORCE_THREADS picks it. */
        EnvScope single("MLVAPP_FORCE_SINGLETHREAD", nullptr);
        EnvScope forced("MLVAPP_FORCE_THREADS", threadText.c_str());
        EnvScope tier1(gpuPreviewProcessingTier1SwitchName(), nullptr);
        OmpThreads scope(threads);

        GpuPreviewProcessingCpuSpans spans;
        std::vector<uint16_t> out16(frame.size(), 0);
        gpuPreviewProcessingApplyCpuReferenceTimed(config, frame.data(), out16.data(), width, height, &spans);
        if (std::string(expectedRefusal) != spans.pointwiseFastRefusal)
            ::minitest::fail(__FILE__, __LINE__, "fast-path refusal", tag + " expected=\"" + expectedRefusal
                             + "\" got=\"" + spans.pointwiseFastRefusal + "\"");
        ASSERT_EQ(admitted, spans.pointwiseFast);
        ASSERT_EQ(threads, spans.pointwiseOmpThreads);
        int first = -1;
        int worst = maxAbsDiff16(out16, ref, &first);
        if (worst != 0) failDiff(__FILE__, __LINE__, "Timed 16-bit == reference", tag, worst, first);

        GpuPreviewProcessingCpuSpans spans8;
        std::vector<uint16_t> scratch(frame.size(), 0);
        std::vector<uint8_t> out8(frame.size(), 0);
        gpuPreviewProcessingApplyCpuReferenceTo8(config, frame.data(), scratch.data(), out8.data(),
                                                 width, height, &spans8);
        ASSERT_TRUE(!spans8.fused8);
        ASSERT_EQ(admitted, spans8.pointwiseFast);
        ASSERT_EQ(threads, spans8.pointwiseOmpThreads);
        worst = maxAbsDiff8(out8, ref, &first);
        if (worst != 0) failDiff(__FILE__, __LINE__, "To8 16-bit route == reference >> 8", tag, worst, first);

        if (!spatial)
        {
            GpuPreviewProcessingCpuSpans spansF;
            std::vector<uint8_t> fused(frame.size(), 0);
            gpuPreviewProcessingApplyCpuReferenceTo8(config, frame.data(), nullptr, fused.data(),
                                                     width, height, &spansF);
            ASSERT_TRUE(spansF.fused8);
            ASSERT_EQ(admitted, spansF.pointwiseFast);
            ASSERT_EQ(threads, spansF.pointwiseOmpThreads);
            worst = maxAbsDiff8(fused, ref, &first);
            if (worst != 0) failDiff(__FILE__, __LINE__, "fused 8-bit == reference >> 8", tag, worst, first);
        }
    }
    std::printf("[PREP-FAST] %s %dx%d refusal=\"%s\" byte-identical at omp 1,4\n",
                label.c_str(), width, height, expectedRefusal);
}

constexpr int kFullWidth = 1808;   /* M16-1243 at s1 (playback_prep processed dims) */
constexpr int kFullHeight = 2268;
constexpr int kMatrixWidth = 452;  /* the stage matrix: a quarter of each side */
constexpr int kMatrixHeight = 568;

GpuPreviewProcessingConfig buildLookAssistOnConfig(MlvPipelineFixture & fixture,
                                                   const std::vector<uint16_t> & blur,
                                                   int width, int height)
{
    GpuPreviewProcessingConfig config = buildConfig(fixture, applyM16LookAssistPreset);
    /* The stage set the venue log reports for M16-1243 LA on. */
    ASSERT_TRUE(config.applyShadowsHighlights);
    ASSERT_TRUE(config.applyInLoopContrast);
    ASSERT_TRUE(config.applyVibrance);
    ASSERT_TRUE(config.applyCreativeCurves);
    ASSERT_TRUE(config.useCameraMatrix);
    ASSERT_TRUE(config.applyGamutCompression);
    ASSERT_TRUE(!config.applyVignette && !config.applyGradient && !config.applyLut
                && !config.applyHighlightReconstruction && !config.applyAgx && !config.applyHueVs);
    ASSERT_TRUE(!gpuPreviewProcessingCpuHasSpatialPostPass(config));
    attachBlur(&config, blur, width, height, false);
    ASSERT_EQ(static_cast<uint32_t>(GpuPreviewCpuStageShadowsHighlights),
              gpuPreviewProcessingCpuStageMask(config));
    return config;
}

/* The optional stages one stage-matrix case may turn on: the config's stage flags,
 * plus the settings the base subset does not reset (camera matrix, gamut, WB,
 * exposure, the dual-ISO switch the HL-recon cases flip). A case's mask must equal
 * the stages it is named for, so no setting from another case rides along. */
enum CaseStage : uint32_t
{
    kStageCreative       = 1u << 0,
    kStageToning         = 1u << 1,
    kStageSaturation     = 1u << 2,
    kStageVibrance       = 1u << 3,
    kStageInLoopContrast = 1u << 4,
    kStageHueVs          = 1u << 5,
    kStageAgx            = 1u << 6,
    kStageVignette       = 1u << 7,
    kStageShadowsHighlights = 1u << 8,
    kStageLut            = 1u << 9,
    kStageHighlightRecon = 1u << 10,
    kStageGradient       = 1u << 11,
    kStageChroma         = 1u << 12,
    kStageSharpen        = 1u << 13,
    kStageMedian         = 1u << 14,
    kStageNoCameraMatrix = 1u << 15,
    kStageNoGamutCompression = 1u << 16,   /* camera matrix on, compression off (exr_mode) */
    kStageGamut          = 1u << 17,       /* not Rec709 */
    kStageWb             = 1u << 18,       /* kelvin or tint off the receipt's */
    kStageExposure       = 1u << 19,       /* exposure off the receipt's */
    kStageDualIsoOff     = 1u << 20        /* the receipt's dual ISO switched off */
};

const char * const kCaseStageNames[] = {
    "creative", "toning", "saturation", "vibrance", "in_loop_contrast", "hue_vs", "agx",
    "vignette", "shadows_highlights", "lut", "highlight_recon", "gradient", "chroma",
    "sharpen", "median", "no_camera_matrix", "no_gamut_compression", "gamut", "wb",
    "exposure", "dual_iso_off"
};

std::string caseStageText(uint32_t mask)
{
    std::string text;
    for (size_t bit = 0; bit < sizeof(kCaseStageNames) / sizeof(kCaseStageNames[0]); ++bit)
        if (mask & (1u << bit))
            text += (text.empty() ? "" : "|") + std::string(kCaseStageNames[bit]);
    return text.empty() ? "none" : text;
}

/* The receipt's settings on a freshly opened fixture, before any case touches it. */
struct ReceiptBaseline
{
    double kelvin = 0.0;
    double tint = 0.0;
    double exposure = 0.0;
    bool dualIso = false;
};

ReceiptBaseline captureBaseline(const processingObject_t * p)
{
    ReceiptBaseline baseline;
    baseline.kelvin = p->kelvin;
    baseline.tint = p->wb_tint;
    baseline.exposure = p->exposure_stops;
    baseline.dualIso = p->dual_iso != nullptr && *p->dual_iso != 0;
    return baseline;
}

uint32_t caseStageMask(const GpuPreviewProcessingConfig & c, const processingObject_t * p,
                       const ReceiptBaseline & baseline)
{
    uint32_t mask = 0;
    if (c.applyCreativeCurves) mask |= kStageCreative;
    if (c.applyToning) mask |= kStageToning;
    if (c.applySaturation) mask |= kStageSaturation;
    if (c.applyVibrance) mask |= kStageVibrance;
    if (c.applyInLoopContrast) mask |= kStageInLoopContrast;
    if (c.applyHueVs) mask |= kStageHueVs;
    if (c.applyAgx) mask |= kStageAgx;
    if (c.applyVignette) mask |= kStageVignette;
    if (c.applyShadowsHighlights) mask |= kStageShadowsHighlights;
    if (c.applyLut) mask |= kStageLut;
    if (c.applyHighlightReconstruction) mask |= kStageHighlightRecon;
    if (c.applyGradient) mask |= kStageGradient;
    if (c.applyChroma) mask |= kStageChroma;
    if (c.applySharpen) mask |= kStageSharpen;
    if (c.applyMedian) mask |= kStageMedian;
    if (!c.useCameraMatrix) mask |= kStageNoCameraMatrix;
    if (c.useCameraMatrix && !c.applyGamutCompression) mask |= kStageNoGamutCompression;
    if (p->colour_gamut != GAMUT_Rec709) mask |= kStageGamut;
    if (p->kelvin != baseline.kelvin || p->wb_tint != baseline.tint) mask |= kStageWb;
    if (p->exposure_stops != baseline.exposure) mask |= kStageExposure;
    if (baseline.dualIso && !c.highlightReconDualIso) mask |= kStageDualIsoOff;
    return mask;
}

} // namespace

TEST(PrepPointwiseFast, LookAssistOnAndOffAreAdmittedAndByteIdenticalAtFullSize)
{
    MlvPipelineFixture fixture;
    openFixture(fixture);
    const std::vector<uint16_t> frame = makeSyntheticFrame(kFullWidth, kFullHeight, 0x1243u);
    const std::vector<uint16_t> blur = makeBlockBlur(frame, kFullWidth, kFullHeight);

    const GpuPreviewProcessingConfig laOn = buildLookAssistOnConfig(fixture, blur, kFullWidth, kFullHeight);
    ASSERT_EQ(std::string(""), std::string(gpuPreviewProcessingPointwiseFastRefusal(laOn)));
    checkConfig("m16_la_on", laOn, frame, kFullWidth, kFullHeight, "");

    /* The engine's other S/H mode: the curve indexed by the blur's first channel. */
    GpuPreviewProcessingConfig laOnIndexMask = laOn;
    laOnIndexMask.shadowsHighlightsCurveIndexMask = !laOn.shadowsHighlightsCurveIndexMask;
    checkConfig("m16_la_on.sh_index_mask_flipped", laOnIndexMask, frame, kFullWidth, kFullHeight, "");

    /* Look Assist off, on a fresh fixture (the preset's contrast and vibrance stay
     * on a processing object): the Standard profile's creative curves, every
     * slider at its neutral position, the same WB. */
    MlvPipelineFixture offFixture;
    openFixture(offFixture);
    const GpuPreviewProcessingConfig laOff = buildConfig(offFixture, [](processingObject_t * p) {
        processingAllowCreativeAdjustments(p);
        processingSetExposureStops(p, 0 / 100.0 + 1.2);
        processingSetSimpleContrast(p, 0.0);
        processingSetShadows(p, 0.0);
        processingSetHighlights(p, 0.0);
        processingSetVibrance(p, 1.0);
        processingSetWhiteBalance(p, 6485.0, -12 / 10.0);
    });
    ASSERT_TRUE(!laOff.applyShadowsHighlights && !laOff.applyInLoopContrast && !laOff.applyVibrance);
    ASSERT_EQ(std::string(""), std::string(gpuPreviewProcessingPointwiseFastRefusal(laOff)));
    checkConfig("m16_la_off", laOff, frame, kFullWidth, kFullHeight, "");
}

TEST(PrepPointwiseFast, EveryParityFixtureStageMatchesAndRefusedStagesFallBack)
{
    MlvPipelineFixture fixture;
    openFixture(fixture);
    const int w = kMatrixWidth;
    const int h = kMatrixHeight;
    const std::vector<uint16_t> frame = makeSyntheticFrame(w, h, 0xC0FFEEu);
    const std::vector<uint16_t> blur = makeBlockBlur(frame, w, h);
    const uint16_t uw = static_cast<uint16_t>(w);
    const uint16_t uh = static_cast<uint16_t>(h);

    struct Case
    {
        const char * label;
        const char * refusal;
        uint32_t stages;   /* caseStageMask the case must produce, and nothing else */
        std::function<void(processingObject_t *)> setup;
        int blurMode;   /* 0 none, 1 full-res, 2 quarter-res, 3 frame state not ready, 4 short blur */
    };
    constexpr uint32_t kSh = kStageCreative | kStageShadowsHighlights;
    constexpr uint32_t kM16 = kStageCreative | kStageInLoopContrast | kStageShadowsHighlights | kStageVibrance
                            | kStageWb | kStageExposure;
    const Case cases[] = {
        { "plain", "", 0, [](processingObject_t *) {}, 0 },
        { "creative_standard", "", kStageCreative, [](processingObject_t * p) { processingAllowCreativeAdjustments(p); }, 0 },
        { "toning", "", kStageCreative | kStageToning, [](processingObject_t * p) { processingAllowCreativeAdjustments(p); processingSetToning(p, 255, 192, 0, 40); }, 0 },
        { "saturation", "", kStageCreative | kStageSaturation, [](processingObject_t * p) { processingAllowCreativeAdjustments(p); processingSetSaturation(p, 1.5); }, 0 },
        { "vibrance_up", "", kStageCreative | kStageVibrance, [](processingObject_t * p) { processingAllowCreativeAdjustments(p); processingSetVibrance(p, 1.5); }, 0 },
        { "vibrance_down", "", kStageCreative | kStageVibrance, [](processingObject_t * p) { processingAllowCreativeAdjustments(p); processingSetVibrance(p, 0.6); }, 0 },
        { "in_loop_contrast", "", kStageCreative | kStageInLoopContrast, [](processingObject_t * p) { processingAllowCreativeAdjustments(p); processingSetSimpleContrast(p, 1.0); }, 0 },
        { "gamut_rec2020", "", kStageGamut, [](processingObject_t * p) { processingSetGamut(p, GAMUT_Rec2020); }, 0 },
        { "exr_mode", "", kStageNoGamutCompression, [](processingObject_t * p) { p->exr_mode = 1; }, 0 },
        { "no_camera_matrix", "", kStageNoCameraMatrix, [](processingObject_t * p) { p->use_cam_matrix = 0; }, 0 },
        { "wb_3000k", "", kStageWb, [](processingObject_t * p) { processingSetWhiteBalance(p, 3000.0, 0.0); }, 0 },
        { "hl_recon", "", kStageHighlightRecon | kStageDualIsoOff, [](processingObject_t * p) { p->highlight_reconstruction = 1; if (p->dual_iso) *p->dual_iso = 0; }, 0 },
        { "hl_recon_dual_iso", "", kStageHighlightRecon, [](processingObject_t * p) { p->highlight_reconstruction = 1; if (p->dual_iso) *p->dual_iso = 1; p->highest_green_diso = 30000; }, 0 },
        { "sh_full", "", kSh, [](processingObject_t * p) { processingAllowCreativeAdjustments(p); processingSetShadows(p, 0.32); processingSetHighlights(p, -0.26); }, 1 },
        { "sh_quarter", "", kSh, [](processingObject_t * p) { processingAllowCreativeAdjustments(p); processingSetShadows(p, 0.32); processingSetHighlights(p, -0.26); }, 2 },
        { "sh_no_frame_state", "", kSh, [](processingObject_t * p) { processingAllowCreativeAdjustments(p); processingSetShadows(p, 0.32); processingSetHighlights(p, -0.26); }, 3 },
        { "sh_short_blur", "", kSh, [](processingObject_t * p) { processingAllowCreativeAdjustments(p); processingSetShadows(p, 0.32); processingSetHighlights(p, -0.26); }, 4 },
        { "night_preset", "", kSh | kStageInLoopContrast | kStageVibrance, [](processingObject_t * p) {
              processingAllowCreativeAdjustments(p); processingSetSimpleContrast(p, 0.14); processingSetPivot(p, 0.46);
              processingSetShadows(p, 0.32); processingSetHighlights(p, -0.26); processingSetVibrance(p, 1.03); }, 1 },
        { "m16_la_on", "", kM16, applyM16LookAssistPreset, 1 },
        { "m16_la_on_hl_recon_dual_iso", "", kM16 | kStageHighlightRecon, [](processingObject_t * p) {
              applyM16LookAssistPreset(p); p->highlight_reconstruction = 1; if (p->dual_iso) *p->dual_iso = 1; p->highest_green_diso = 30000; }, 1 },
        { "chroma_r2", "", kStageChroma, [](processingObject_t * p) { processingEnableChromaSeparation(p); processingSetChromaBlurRadius(p, 2); }, 0 },
        { "sharpen", "", kStageSharpen, [](processingObject_t * p) { processingSetSharpening(p, 0.5); }, 0 },
        { "median_w3", "", kStageMedian, [](processingObject_t * p) { p->denoiserWindow = 3; p->denoiserStrength = 80; }, 0 },
        /* Refused: the stages the fast path does not carry. */
        { "vignette", "vignette", kStageVignette, [uw, uh](processingObject_t * p) { processingSetVignetteMask(p, uw, uh, 0.5f, 0.2f, 1.0f, 1.4f); processingSetVignetteStrength(p, 60); }, 0 },
        { "gradient", "gradient", kStageGradient, [uw, uh](processingObject_t * p) {
              processingSetGradientEnable(p, 1);
              processingSetGradientMask(p, uw, uh, 0.0f, 0.0f, static_cast<float>(uw), static_cast<float>(uh));
              processingSetGradientExposure(p, 1.0); }, 0 },
        { "agx", "agx", kStageAgx, [](processingObject_t * p) { processingEnableAgX(p); }, 0 },
        { "hue_vs", "hue_vs", kStageCreative | kStageHueVs, [](processingObject_t * p) {
              processingAllowCreativeAdjustments(p);
              for (int i = 0; i < 36000; ++i) p->hue_vs_hue[i] = 0.5f;
              p->hue_vs_hue_used = 1; }, 0 },
        { "lut3d", "lut", kStageLut, [](processingObject_t * p) {
              lut_t * lut = p->lut;
              ASSERT_TRUE(lut != nullptr);
              if (lut->cube) { free(lut->cube); lut->cube = nullptr; }
              const int dim = 17;
              lut->dimension = static_cast<uint16_t>(dim);
              lut->is3d = 1;
              lut->intensity = 100;
              for (int i = 0; i < 3; ++i) { lut->domain_min[i] = 0.0f; lut->domain_max[i] = 1.0f; }
              lut->cube = static_cast<float *>(malloc(static_cast<size_t>(dim) * dim * dim * 3 * sizeof(float)));
              ASSERT_TRUE(lut->cube != nullptr);
              for (int e = 0; e < dim * dim * dim; ++e)
                  for (int c = 0; c < 3; ++c)
                      lut->cube[e * 3 + c] = static_cast<float>((e * 3 + c) % dim) / (dim - 1);
              p->lut_on = 1; }, 0 },
    };

    int checked = 0;
    const ReceiptBaseline baseline = captureBaseline(fixture.processing());
    std::string stageMismatches;
    for (const Case & c : cases)
    {
        GpuPreviewProcessingConfig config = buildConfig(fixture, c.setup);
        const uint32_t stages = caseStageMask(config, fixture.processing(), baseline);
        if (stages != c.stages)
            stageMismatches += std::string(" ") + c.label + ": expected=" + caseStageText(c.stages)
                             + " got=" + caseStageText(stages) + ";";
        if (c.blurMode != 0) ASSERT_TRUE(config.applyShadowsHighlights);
        if (c.blurMode == 1)
        {
            attachBlur(&config, blur, w, h, false);
        }
        else if (c.blurMode == 2)
        {
            std::vector<uint16_t> quarter(static_cast<size_t>(w / 4) * (h / 4) * 3u);
            ASSERT_TRUE(processingRgbU16BoxDownsample(blur.data(), quarter.data(), w, h, 4, 1) != 0);
            attachBlur(&config, quarter, w, h, true);
        }
        else if (c.blurMode == 3)
        {
            config.shadowsHighlightsFrameStateReady = false;
            config.shadowsHighlightsBlur.clear();
        }
        else if (c.blurMode == 4)
        {
            /* A blur that covers only the first half of the frame: the reference
             * reads factor 1 past its end. */
            attachBlur(&config, blur, w, h, false);
            config.shadowsHighlightsBlur.truncate(config.shadowsHighlightsBlur.size() / 2);
        }
        checkConfig(c.label, config, frame, w, h, c.refusal);
        ++checked;
    }
    ASSERT_EQ(static_cast<int>(sizeof(cases) / sizeof(cases[0])), checked);
    if (!stageMismatches.empty())
        ::minitest::fail(__FILE__, __LINE__, "each case enables only its own stages", stageMismatches);
}

TEST(PrepPointwiseFast, KillSwitchRestoresTheReferenceLoop)
{
    MlvPipelineFixture fixture;
    openFixture(fixture);
    const std::vector<uint16_t> frame = makeSyntheticFrame(kMatrixWidth, kMatrixHeight, 0x5EEDu);
    const std::vector<uint16_t> blur = makeBlockBlur(frame, kMatrixWidth, kMatrixHeight);
    const GpuPreviewProcessingConfig laOn = buildLookAssistOnConfig(fixture, blur, kMatrixWidth, kMatrixHeight);
    const char * name = gpuPreviewProcessingPointwiseFastKillSwitchName();
    ASSERT_EQ(std::string("MLVAPP_DISABLE_PREP_POINTWISE_FAST"), std::string(name));
    for (const char * on : { "1", "true", "yes", "on", " ON " })
    {
        EnvScope kill(name, on);
        checkConfig(std::string("kill_switch.") + on, laOn, frame, kMatrixWidth, kMatrixHeight, "kill_switch");
    }
    for (const char * off : { "0", "", "off", "2" })
    {
        EnvScope kill(name, off);
        ASSERT_EQ(std::string(""), std::string(gpuPreviewProcessingPointwiseFastRefusal(laOn)));
    }
    EnvScope unset(name, nullptr);
    ASSERT_EQ(std::string(""), std::string(gpuPreviewProcessingPointwiseFastRefusal(laOn)));
}

TEST(PrepPointwiseFast, OptInBenchmarkOnAPrepStyleThread)
{
    if (qgetenv("MLVAPP_PREP_POINTWISE_FAST_BENCH") != QByteArrayLiteral("1"))
    {
        SKIP_TEST("opt-in: set MLVAPP_PREP_POINTWISE_FAST_BENCH=1");
    }
    MlvPipelineFixture fixture;
    openFixture(fixture);
    const std::vector<uint16_t> frame = makeSyntheticFrame(kFullWidth, kFullHeight, 0x1243u);
    const std::vector<uint16_t> blur = makeBlockBlur(frame, kFullWidth, kFullHeight);
    const GpuPreviewProcessingConfig laOn = buildLookAssistOnConfig(fixture, blur, kFullWidth, kFullHeight);
    const char * kill = gpuPreviewProcessingPointwiseFastKillSwitchName();
    EnvScope killUnset(kill, nullptr);
    /* Production thread counts: no test-harness override, so the fast team is
     * the playback worker count (min(idealThreadCount, 16)). */
    EnvScope single("MLVAPP_FORCE_SINGLETHREAD", nullptr);
    EnvScope forced("MLVAPP_FORCE_THREADS", nullptr);

    constexpr int kReps = 7;
    std::vector<double> refMs, fastMs, refMs1, fastMs1;
    int workerMaxThreads = 0, refTeam = 0, fastTeam = 0;
    bool identical = true;
    /* A plain std::thread, as MainWindow::playbackPrepThreadLoop is: its OpenMP
     * ICVs are the process defaults, not the render thread's. */
    std::thread worker([&]() {
        workerMaxThreads = omp_get_max_threads();
        std::vector<uint16_t> refOut(frame.size()), fastOut(frame.size());
        for (int rep = 0; rep < kReps; ++rep)
        {
            for (int leg = 0; leg < 2; ++leg)
            {
                const bool reference = ((rep + leg) % 2) == 0;   /* interleaved, alternating first */
                GpuPreviewProcessingCpuSpans spans;
                if (reference)
                {
                    qputenv(kill, QByteArray("1"));
                    gpuPreviewProcessingApplyCpuReferenceTimed(laOn, frame.data(), refOut.data(),
                                                               kFullWidth, kFullHeight, &spans);
                    qunsetenv(kill);
                    refMs.push_back(spans.pointwiseMs);
                    refTeam = spans.pointwiseOmpThreads;
                }
                else
                {
                    gpuPreviewProcessingApplyCpuReferenceTimed(laOn, frame.data(), fastOut.data(),
                                                               kFullWidth, kFullHeight, &spans);
                    fastMs.push_back(spans.pointwiseMs);
                    fastTeam = spans.pointwiseOmpThreads;
                    if (!spans.pointwiseFast) identical = false;
                }
            }
            if (refOut != fastOut) identical = false;
        }
        /* Scaling: both loops on one thread (3 reps each, interleaved). */
        omp_set_num_threads(1);
        qputenv("MLVAPP_FORCE_THREADS", QByteArray("1"));
        for (int rep = 0; rep < 3; ++rep)
        {
            GpuPreviewProcessingCpuSpans spans;
            qputenv(kill, QByteArray("1"));
            gpuPreviewProcessingApplyCpuReferenceTimed(laOn, frame.data(), refOut.data(),
                                                       kFullWidth, kFullHeight, &spans);
            qunsetenv(kill);
            refMs1.push_back(spans.pointwiseMs);
            gpuPreviewProcessingApplyCpuReferenceTimed(laOn, frame.data(), fastOut.data(),
                                                       kFullWidth, kFullHeight, &spans);
            fastMs1.push_back(spans.pointwiseMs);
            if (refOut != fastOut || spans.pointwiseOmpThreads != 1) identical = false;
        }
        qunsetenv("MLVAPP_FORCE_THREADS");
    });
    worker.join();
    auto median = [](std::vector<double> v) { std::sort(v.begin(), v.end()); return v[v.size() / 2]; };
    const double refMed = median(refMs);
    const double fastMed = median(fastMs);
    std::printf("[PREP-FAST-BENCH] frame=%dx%d reps=%d worker_omp_get_max_threads=%d "
                "reference_team=%d fast_team=%d reference_median_ms=%.3f fast_median_ms=%.3f "
                "speedup=%.3f byte_identical=%d\n",
                kFullWidth, kFullHeight, kReps, workerMaxThreads, refTeam, fastTeam,
                refMed, fastMed, fastMed > 0.0 ? refMed / fastMed : 0.0, identical ? 1 : 0);
    for (int rep = 0; rep < kReps; ++rep)
        std::printf("[PREP-FAST-BENCH] rep=%d reference_ms=%.3f fast_ms=%.3f\n", rep, refMs[rep], fastMs[rep]);
    std::printf("[PREP-FAST-BENCH] one_thread reference_median_ms=%.3f fast_median_ms=%.3f "
                "reference_ns_per_px=%.2f fast_ns_per_px=%.2f\n",
                median(refMs1), median(fastMs1),
                median(refMs1) * 1.0e6 / (static_cast<double>(kFullWidth) * kFullHeight),
                median(fastMs1) * 1.0e6 / (static_cast<double>(kFullWidth) * kFullHeight));
    ASSERT_TRUE(identical);
}
