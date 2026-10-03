#include "../common/minitest.h"
#include "../common/test_artifacts.h"

#include "mlv_pipeline_fixture.h"

#include "../../platform/qt/GpuPreviewProcessing.h"
#include "../../src/processing/raw_processing.h"
#include "../../src/debayer/debayer.h"
#include "../../src/mlv/video_mlv.h"
#include "../../src/debug/StageTiming.h"

#include <QtGlobal>
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <thread>
#include <vector>

/* PLAYBACK-SH-OFF-CPU-PATH-1: the quarter-res shadows/highlights frame state
 * must reproduce the legacy full-res frame state (debayerBasicU16 +
 * processingRefreshShadowsHighlightsBlurFromRgb16 in the standard x1 preview
 * lane) BIT-EXACTLY. Tolerance: 0 LSB on every channel of every pixel. */

namespace {

void open_fixture(MlvPipelineFixture & fixture)
{
    QString error_message;
    ASSERT_TRUE(fixture.openTinyDualIso(&error_message));
    ASSERT_TRUE(fixture.loadReceipt(QStringLiteral("tests/fixtures/receipts/tiny_dual_iso_hq.marxml"), &error_message));
    ASSERT_TRUE(fixture.applyReceipt(&error_message));
    processingObject_t * processing = fixture.processing();
    processingAllowCreativeAdjustments(processing);
    /* The owner's Look Assist night preset S/H (32 / -26). */
    processingSetShadows(processing, 0.32);
    processingSetHighlights(processing, -0.26);
    (void)fixture.renderFrame16(0, /*threads=*/1);
}

/* The exact preview state RenderFrameThread sets around the fast S/H refresh. */
struct PreviewState
{
    int mode;
    int aggressive;
    int scale;
    explicit PreviewState(int scaleFactor, int aggressiveMode = -1)
        : mode(processingPlaybackPreviewModeEnabled())
        , aggressive(processingPlaybackAggressivePreviewModeEnabled())
        , scale(processingPlaybackPreviewScaleFactor())
    {
        processingSetPlaybackPreviewMode(1);
        processingSetPlaybackAggressivePreviewMode(
            aggressiveMode >= 0 ? aggressiveMode : (mlvPlaybackAggressivePreviewMode() != 0 ? 1 : 0));
        processingSetPlaybackPreviewScaleFactor(scaleFactor);
    }
    ~PreviewState()
    {
        processingSetPlaybackPreviewScaleFactor(scale);
        processingSetPlaybackAggressivePreviewMode(aggressive);
        processingSetPlaybackPreviewMode(mode);
    }
};

double median(std::vector<double> v)
{
    if (v.empty()) return 0.0;
    std::sort(v.begin(), v.end());
    return v[v.size() / 2];
}

int host_threads()
{
    return qBound(1, static_cast<int>(std::thread::hardware_concurrency()), 16);
}

/* Legacy path: what RenderFrameThread ran per frame before this card. */
std::vector<uint16_t> legacy_full_res_blur(processingObject_t * processing,
                                           std::vector<uint16_t> & bayer,
                                           int w, int h, int threads, int bitShift)
{
    std::vector<uint16_t> rgb(static_cast<size_t>(w) * h * 3u);
    debayerBasicU16(rgb.data(), bayer.data(), w, h, threads, bitShift);
    PreviewState state(1);
    ASSERT_NE(0, processingRefreshShadowsHighlightsBlurFromRgb16(processing, rgb.data(), w, h, threads, 0));
    ASSERT_EQ(1, processingGetLastShadowsHighlightsQuarterresPathTakenForTesting());
    const uint16_t * blur = nullptr;
    int bw = 0, bh = 0;
    ASSERT_NE(0, processingGetShadowsHighlightsBlurData(processing, &blur, &bw, &bh, nullptr));
    ASSERT_EQ(w, bw);
    ASSERT_EQ(h, bh);
    return std::vector<uint16_t>(blur, blur + static_cast<size_t>(w) * h * 3u);
}

/* New path: quarter-only refresh, then the exact expander. */
std::vector<uint16_t> quarter_blur(processingObject_t * processing,
                                   std::vector<uint16_t> & bayer,
                                   int w, int h, int threads, int bitShift,
                                   int * qwOut = nullptr, int * qhOut = nullptr)
{
    PreviewState state(1);
    ASSERT_NE(0, processingShadowsHighlightsQuarterFrameStateEligible(w, h));
    ASSERT_NE(0, processingRefreshShadowsHighlightsQuarterBlurFromBayer16(processing, bayer.data(), w, h, threads, bitShift));
    const uint16_t * quarter = nullptr;
    int qw = 0, qh = 0, fw = 0, fh = 0;
    ASSERT_NE(0, processingGetShadowsHighlightsQuarterBlurData(processing, &quarter, &qw, &qh, &fw, &fh));
    ASSERT_EQ(w / 4, qw);
    ASSERT_EQ(h / 4, qh);
    ASSERT_EQ(w, fw);
    ASSERT_EQ(h, fh);
    if (qwOut) *qwOut = qw;
    if (qhOut) *qhOut = qh;
    std::vector<uint16_t> full(static_cast<size_t>(w) * h * 3u, 0);
    ASSERT_NE(0, processingExpandShadowsHighlightsQuarterBlur(quarter, qw, qh, full.data(), w, h, threads));
    return full;
}

struct ChannelDiff
{
    int maxAbs[3] = { 0, 0, 0 };
    double meanAbs[3] = { 0.0, 0.0, 0.0 };
    size_t differing = 0;
};

ChannelDiff diff_rgb16(const std::vector<uint16_t> & a, const std::vector<uint16_t> & b)
{
    ChannelDiff d;
    ASSERT_EQ(a.size(), b.size());
    double sum[3] = { 0.0, 0.0, 0.0 };
    for (size_t i = 0; i < a.size(); ++i)
    {
        const int delta = std::abs(static_cast<int>(a[i]) - static_cast<int>(b[i]));
        const int c = static_cast<int>(i % 3u);
        d.maxAbs[c] = std::max(d.maxAbs[c], delta);
        sum[c] += delta;
        if (delta) ++d.differing;
    }
    const double n = static_cast<double>(a.size() / 3u);
    for (int c = 0; c < 3; ++c) d.meanAbs[c] = n > 0 ? sum[c] / n : 0.0;
    return d;
}

void record_and_assert_exact(const std::string & label, const ChannelDiff & d)
{
    char buf[256];
    std::snprintf(buf, sizeof(buf), "max_abs=%d/%d/%d mean_abs=%.6f/%.6f/%.6f differing=%zu",
                  d.maxAbs[0], d.maxAbs[1], d.maxAbs[2],
                  d.meanAbs[0], d.meanAbs[1], d.meanAbs[2], d.differing);
    std::printf("[SH-QUARTER-PARITY] %s %s\n", label.c_str(), buf);
    test_artifacts::record("sh_quarter_parity." + label, buf);
    ASSERT_EQ(static_cast<size_t>(0), d.differing);
}

std::vector<uint16_t> synthetic_bayer(int w, int h, unsigned seed, int maxValue)
{
    std::vector<uint16_t> bayer(static_cast<size_t>(w) * h);
    unsigned state = seed;
    for (int y = 0; y < h; ++y)
    {
        for (int x = 0; x < w; ++x)
        {
            state = state * 1664525u + 1013904223u;
            /* Smooth gradient plus noise plus a hard edge, so both the
             * bilateral range term and the clamped borders are exercised. */
            const int base = (x * maxValue) / (w * 2) + (y * maxValue) / (h * 2);
            const int edge = (x > w / 3 && y < (2 * h) / 3) ? maxValue / 3 : 0;
            const int noise = static_cast<int>((state >> 16) % 4096u) - 2048;
            bayer[static_cast<size_t>(y) * w + x] =
                static_cast<uint16_t>(qBound(0, base + edge + noise, maxValue));
        }
    }
    return bayer;
}

} // namespace

TEST(ShFrameStateProxy, QuarterFrameStateMatchesLegacyFullResBlurOnFixtureFrames)
{
    MlvPipelineFixture fixture;
    open_fixture(fixture);
    const int w = fixture.width();
    const int h = fixture.height();
    ASSERT_EQ(0, w % 4);
    ASSERT_EQ(0, h % 4);
    const int bpp = fixture.video()->RAWI.raw_info.bits_per_pixel;
    const int shifts[2] = { 0, 16 - bpp };
    const uint64_t frames = getMlvFrames(fixture.video());
    const uint64_t frameIndices[2] = { 0, frames > 1 ? frames - 1 : 0 };
    /* Each frame runs both bit shifts, and each shift runs single- and
     * multi-threaded across the two frames (4 full-res combinations). */
    for (int fi = 0; fi < 2; ++fi)
    {
        const uint64_t frame = frameIndices[fi];
        std::vector<uint16_t> raw(static_cast<size_t>(w) * h);
        ASSERT_EQ(0, getMlvRawFrameUint16(fixture.video(), frame, raw.data()));
        for (int si = 0; si < 2; ++si)
        {
            const int shift = shifts[si];
            {
                const int threads = ((fi + si) % 2 == 0) ? 1 : host_threads();
                std::vector<uint16_t> legacyBayer = raw;
                std::vector<uint16_t> quarterBayer = raw;
                const std::vector<uint16_t> legacy =
                    legacy_full_res_blur(fixture.processing(), legacyBayer, w, h, threads, shift);
                const std::vector<uint16_t> expanded =
                    quarter_blur(fixture.processing(), quarterBayer, w, h, threads, shift);
                const std::string label = "fixture_f" + std::to_string(frame)
                    + "_shift" + std::to_string(shift) + "_t" + std::to_string(threads);
                record_and_assert_exact(label, diff_rgb16(legacy, expanded));
                /* debayerBasicU16's in-place shift of the Bayer buffer is kept. */
                ASSERT_TRUE(legacyBayer == quarterBayer);
            }
        }
    }
}

TEST(ShFrameStateProxy, QuarterFrameStateMatchesLegacyOnSyntheticSizesAndFullRange)
{
    MlvPipelineFixture fixture;
    open_fixture(fixture);
    const struct { int w; int h; int maxValue; unsigned seed; } cases[] = {
        { 8, 8, 65535, 1u },
        { 64, 48, 16383, 2u },
        { 120, 84, 65535, 3u },
        { 1808 / 4, 2268 / 4 + 1, 65535, 4u },   /* 452 x 568: quarter dims 113 x 142 (odd x) */
    };
    for (const auto & c : cases)
    {
        std::vector<uint16_t> bayer = synthetic_bayer(c.w, c.h, c.seed, c.maxValue);
        std::vector<uint16_t> legacyBayer = bayer;
        std::vector<uint16_t> quarterBayer = bayer;
        const std::vector<uint16_t> legacy =
            legacy_full_res_blur(fixture.processing(), legacyBayer, c.w, c.h, host_threads(), 0);
        const std::vector<uint16_t> expanded =
            quarter_blur(fixture.processing(), quarterBayer, c.w, c.h, host_threads(), 0);
        record_and_assert_exact("synthetic_" + std::to_string(c.w) + "x" + std::to_string(c.h),
                                diff_rgb16(legacy, expanded));
    }
}

TEST(ShFrameStateProxy, QuarterFrameStateRefusesOutsideTheStandardX1Lane)
{
    /* The CUDA texture route runs only at scale 1; at scales 2 and 4 (and in
     * aggressive preview, or for sizes that are not multiples of 4) the
     * quarter refresh refuses, so those frames keep the unchanged legacy path. */
    MlvPipelineFixture fixture;
    open_fixture(fixture);
    processingObject_t * processing = fixture.processing();
    std::vector<uint16_t> bayer = synthetic_bayer(64, 48, 7u, 65535);
    for (int scale : { 2, 4 })
    {
        PreviewState state(scale, 0);
        ASSERT_EQ(0, processingShadowsHighlightsQuarterFrameStateEligible(64, 48));
        ASSERT_EQ(0, processingRefreshShadowsHighlightsQuarterBlurFromBayer16(processing, bayer.data(), 64, 48, 1, 0));
    }
    {
        PreviewState state(1, 1);
        ASSERT_EQ(0, processingRefreshShadowsHighlightsQuarterBlurFromBayer16(processing, bayer.data(), 64, 48, 1, 0));
    }
    {
        PreviewState state(1, 0);
        std::vector<uint16_t> odd = synthetic_bayer(66, 48, 8u, 65535);
        ASSERT_EQ(0, processingRefreshShadowsHighlightsQuarterBlurFromBayer16(processing, odd.data(), 66, 48, 1, 0));
        ASSERT_EQ(0, processingRefreshShadowsHighlightsQuarterBlurFromBayer16(processing, odd.data(), 64, 46, 1, 0));
    }
    ASSERT_EQ(0, processingGetShadowsHighlightsQuarterBlurData(processing, nullptr, nullptr, nullptr, nullptr, nullptr));
}

TEST(ShFrameStateProxy, AnyOtherBlurComputationInvalidatesTheQuarterState)
{
    MlvPipelineFixture fixture;
    open_fixture(fixture);
    processingObject_t * processing = fixture.processing();
    std::vector<uint16_t> bayer = synthetic_bayer(64, 48, 9u, 65535);
    std::vector<uint16_t> copy = bayer;
    (void)quarter_blur(processing, copy, 64, 48, 1, 0);
    ASSERT_NE(0, processingGetShadowsHighlightsQuarterBlurData(processing, nullptr, nullptr, nullptr, nullptr, nullptr));
    copy = bayer;
    (void)legacy_full_res_blur(processing, copy, 64, 48, 1, 0);
    ASSERT_EQ(0, processingGetShadowsHighlightsQuarterBlurData(processing, nullptr, nullptr, nullptr, nullptr, nullptr));
    /* A full render (the CPU route) also recomputes and invalidates it. */
    copy = bayer;
    (void)quarter_blur(processing, copy, 64, 48, 1, 0);
    (void)fixture.renderFrame16(1, /*threads=*/1);
    ASSERT_EQ(0, processingGetShadowsHighlightsQuarterBlurData(processing, nullptr, nullptr, nullptr, nullptr, nullptr));
}

TEST(ShFrameStateProxy, StageCostBeforeAndAfterInformational)
{
    /* INFORMATIONAL: per-frame S/H frame-state cost, legacy vs quarter, on
     * THIS host at the fixture's real 1808x2268. Only measurement invariants
     * are asserted; the numbers are host-specific (the hub VM is CPU-
     * contended) and are not a venue claim. */
    MlvPipelineFixture fixture;
    open_fixture(fixture);
    const int w = fixture.width();
    const int h = fixture.height();
    std::vector<uint16_t> raw(static_cast<size_t>(w) * h);
    ASSERT_EQ(0, getMlvRawFrameUint16(fixture.video(), 0, raw.data()));
    const int threads = host_threads();
    const int iterations = 6;
    std::vector<double> legacyDebayer, legacyRefresh, legacyCopy, legacyTotal;
    std::vector<double> quarterRefresh, quarterCopy, quarterTotal;
    std::vector<uint16_t> rgb(static_cast<size_t>(w) * h * 3u);
    for (int i = 0; i < iterations; ++i)
    {
        std::vector<uint16_t> bayer = raw;
        double t0 = mlv_stage_timing_now();
        debayerBasicU16(rgb.data(), bayer.data(), w, h, threads, 0);
        const double t1 = mlv_stage_timing_now();
        {
            PreviewState state(1);
            ASSERT_NE(0, processingRefreshShadowsHighlightsBlurFromRgb16(fixture.processing(), rgb.data(), w, h, threads, 0));
        }
        const double t2 = mlv_stage_timing_now();
        const uint16_t * blur = nullptr;
        int bw = 0, bh = 0;
        ASSERT_NE(0, processingGetShadowsHighlightsBlurData(fixture.processing(), &blur, &bw, &bh, nullptr));
        /* gpuPreviewProcessingAttachFrameState's QByteArray copy. */
        const QByteArray copy(reinterpret_cast<const char *>(blur), static_cast<int>(static_cast<size_t>(bw) * bh * 6u));
        const double t3 = mlv_stage_timing_now();
        ASSERT_TRUE(!copy.isEmpty());
        legacyDebayer.push_back((t1 - t0) * 1000.0);
        legacyRefresh.push_back((t2 - t1) * 1000.0);
        legacyCopy.push_back((t3 - t2) * 1000.0);
        legacyTotal.push_back((t3 - t0) * 1000.0);

        bayer = raw;
        t0 = mlv_stage_timing_now();
        {
            PreviewState state(1);
            ASSERT_NE(0, processingRefreshShadowsHighlightsQuarterBlurFromBayer16(fixture.processing(), bayer.data(), w, h, threads, 0));
        }
        const double q1 = mlv_stage_timing_now();
        const uint16_t * quarter = nullptr;
        int qw = 0, qh = 0;
        ASSERT_NE(0, processingGetShadowsHighlightsQuarterBlurData(fixture.processing(), &quarter, &qw, &qh, nullptr, nullptr));
        const QByteArray qcopy(reinterpret_cast<const char *>(quarter), static_cast<int>(static_cast<size_t>(qw) * qh * 6u));
        const double q2 = mlv_stage_timing_now();
        ASSERT_TRUE(!qcopy.isEmpty());
        quarterRefresh.push_back((q1 - t0) * 1000.0);
        quarterCopy.push_back((q2 - q1) * 1000.0);
        quarterTotal.push_back((q2 - t0) * 1000.0);
    }
    for (size_t i = 0; i < legacyTotal.size(); ++i)
    {
        ASSERT_TRUE(legacyTotal[i] > 0.0 && quarterTotal[i] > 0.0);
    }
    char buf[512];
    std::snprintf(buf, sizeof(buf),
                  "%dx%d threads=%d legacy(debayer=%.2f refresh=%.2f attach_copy=%.2f total=%.2f) "
                  "quarter(refresh=%.2f attach_copy=%.2f total=%.2f) ms, medians of %d; "
                  "full-res blur bytes %zu -> quarter %zu",
                  w, h, threads,
                  median(legacyDebayer), median(legacyRefresh), median(legacyCopy), median(legacyTotal),
                  median(quarterRefresh), median(quarterCopy), median(quarterTotal), iterations,
                  static_cast<size_t>(w) * h * 6u, static_cast<size_t>(w / 4) * (h / 4) * 6u);
    std::printf("[SH-STAGE-COST] %s\n", buf);
    test_artifacts::record("sh_quarter_stage_cost", buf);
}

namespace {

/* The display-shader route: the GPU preview subset (as the display parity
 * tests in test_gpu_preview_processing.cpp configure it) plus the night preset
 * S/H, with LUTs primed by one render. */
GpuPreviewProcessingConfig build_display_config(MlvPipelineFixture & fixture)
{
    processingObject_t * processing = fixture.processing();
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
    processing->vignette_strength = 0;
    processingSetGamut(processing, GAMUT_Rec709);
    processingAllowCreativeAdjustments(processing);
    processingSetShadows(processing, 0.32);
    processingSetHighlights(processing, -0.26);
    (void)fixture.renderFrame16(1, /*threads=*/1);
    QString reason;
    if (!gpuPreviewProcessingIsSupported(processing, &reason))
    {
        ::minitest::fail(__FILE__, __LINE__, "gpuPreviewProcessingIsSupported", reason.toStdString());
    }
    GpuPreviewProcessingConfig config = gpuPreviewProcessingBuildConfig(processing, &reason);
    ASSERT_TRUE(config.enabled);
    ASSERT_TRUE(config.applyShadowsHighlights);
    return config;
}

struct AttachedPair
{
    GpuPreviewProcessingConfig full;
    GpuPreviewProcessingConfig quarter;
    std::vector<uint16_t> frame;   /* debayered display input */
};

AttachedPair attach_full_and_quarter(MlvPipelineFixture & fixture, uint64_t frameIndex)
{
    AttachedPair pair;
    const GpuPreviewProcessingConfig base = build_display_config(fixture);
    processingObject_t * processing = fixture.processing();
    const int w = fixture.width();
    const int h = fixture.height();
    std::vector<uint16_t> raw(static_cast<size_t>(w) * h);
    ASSERT_EQ(0, getMlvRawFrameUint16(fixture.video(), frameIndex, raw.data()));
    QString reason;
    /* RenderFrameThread's shift for non-HQ-dual-ISO frames: 16 - bits_per_pixel. */
    const int shift = 16 - fixture.video()->RAWI.raw_info.bits_per_pixel;

    std::vector<uint16_t> legacyBayer = raw;
    (void)legacy_full_res_blur(processing, legacyBayer, w, h, host_threads(), shift);
    pair.full = base;
    ASSERT_TRUE(gpuPreviewProcessingAttachFrameState(&pair.full, processing, w, h, &reason));
    ASSERT_TRUE(!pair.full.shadowsHighlightsBlurQuarter);
    ASSERT_EQ(static_cast<int>(static_cast<size_t>(w) * h * 6u), pair.full.shadowsHighlightsBlur.size());

    std::vector<uint16_t> quarterBayer = raw;
    (void)quarter_blur(processing, quarterBayer, w, h, host_threads(), shift);
    pair.quarter = base;
    ASSERT_TRUE(gpuPreviewProcessingAttachFrameState(&pair.quarter, processing, w, h, &reason));
    ASSERT_TRUE(pair.quarter.shadowsHighlightsBlurQuarter);
    ASSERT_EQ(static_cast<int>(static_cast<size_t>(w / 4) * (h / 4) * 6u), pair.quarter.shadowsHighlightsBlur.size());
    ASSERT_TRUE(gpuPreviewProcessingHasShadowsHighlightsFrameState(pair.quarter, w, h));

    pair.frame.assign(static_cast<size_t>(w) * h * 3u, 0);
    debayerBasicU16(pair.frame.data(), legacyBayer.data(), w, h, 1, 0);
    return pair;
}

} // namespace

TEST(ShFrameStateProxy, CpuReferenceWithQuarterFrameStateMatchesFullResFrameState)
{
    MlvPipelineFixture fixture;
    open_fixture(fixture);
    const int w = fixture.width();
    const int h = fixture.height();
    const AttachedPair pair = attach_full_and_quarter(fixture, 1);
    GpuPreviewProcessingConfig expanded;
    const GpuPreviewProcessingConfig & resolved =
        gpuPreviewProcessingResolveFullResShadowsHighlightsBlur(pair.quarter, w, h, &expanded);
    ASSERT_TRUE(!resolved.shadowsHighlightsBlurQuarter);
    ASSERT_TRUE(resolved.shadowsHighlightsBlur == pair.full.shadowsHighlightsBlur);

    std::vector<uint16_t> fromFull(pair.frame.size(), 0);
    std::vector<uint16_t> fromQuarter(pair.frame.size(), 0);
    gpuPreviewProcessingApplyCpuReference(pair.full, pair.frame.data(), fromFull.data(), w, h);
    gpuPreviewProcessingApplyCpuReference(pair.quarter, pair.frame.data(), fromQuarter.data(), w, h);
    record_and_assert_exact("cpu_reference_picture", diff_rgb16(fromFull, fromQuarter));

    /* S/H is visibly active in this picture (so the parity is not vacuous). */
    GpuPreviewProcessingConfig shOff = pair.full;
    shOff.applyShadowsHighlights = false;
    std::vector<uint16_t> noSh(pair.frame.size(), 0);
    gpuPreviewProcessingApplyCpuReference(shOff, pair.frame.data(), noSh.data(), w, h);
    ASSERT_TRUE(noSh != fromFull);
}

TEST(ShFrameStateProxy, DisplayShaderWithQuarterFrameStateMatchesFullResFrameState)
{
    /* The live CUDA texture-present presenters draw through this display
     * shader + binding path (gpuPreviewProcessingApplyDisplayGpuOffscreen uses
     * the same Update/Bind functions). Quarter-res frame state must give the
     * same 16-bit output as the legacy full-res frame state. */
    MlvPipelineFixture fixture;
    open_fixture(fixture);
    const int w = fixture.width();
    const int h = fixture.height();
    const AttachedPair pair = attach_full_and_quarter(fixture, 1);

    qputenv("MLVAPP_GPU_PREVIEW_ALLOW_SOFTWARE", QByteArray("1"));
    std::vector<uint16_t> fromFull(pair.frame.size(), 0);
    std::vector<uint16_t> fromQuarter(pair.frame.size(), 0);
    QString reason;
    QString renderer;
    if (!gpuPreviewProcessingApplyDisplayGpuOffscreen(pair.full, pair.frame.data(), fromFull.data(), w, h, &reason, &renderer))
    {
        const GpuPreviewProcessingBackendAvailability availability = gpuPreviewProcessingProbeGpuBackend();
        if (availability.available)
        {
            ::minitest::fail(__FILE__, __LINE__, "display shader failed on a working GL backend", reason.toStdString());
        }
        /* The hosted "Display parity (software GL required, never skipped)"
         * step sets this: there an unavailable backend is a failure, not a skip. */
        if (qEnvironmentVariableIntValue("MLVAPP_REQUIRE_DISPLAY_PARITY_GL") != 0)
        {
            ::minitest::fail(__FILE__, __LINE__,
                             "MLVAPP_REQUIRE_DISPLAY_PARITY_GL is set but the display shader cannot run",
                             availability.reason.toStdString());
        }
        SKIP_TEST(std::string("no GL backend: ") + availability.reason.toStdString());
    }
    ASSERT_TRUE(gpuPreviewProcessingApplyDisplayGpuOffscreen(pair.quarter, pair.frame.data(), fromQuarter.data(), w, h, &reason, &renderer));
    std::printf("[SH-QUARTER-PARITY] display renderer=%s\n", renderer.toStdString().c_str());
    record_and_assert_exact("display_shader_picture", diff_rgb16(fromFull, fromQuarter));

    GpuPreviewProcessingConfig shOff = pair.full;
    shOff.applyShadowsHighlights = false;
    std::vector<uint16_t> noSh(pair.frame.size(), 0);
    ASSERT_TRUE(gpuPreviewProcessingApplyDisplayGpuOffscreen(shOff, pair.frame.data(), noSh.data(), w, h, &reason, &renderer));
    ASSERT_TRUE(noSh != fromFull);

    /* Kill-switch shape: frame state never attached -> the shader leaves S/H
     * out exactly as before (pre-fix look), quarter or not. */
    GpuPreviewProcessingConfig bypassed = build_display_config(fixture);
    std::vector<uint16_t> bypassedOut(pair.frame.size(), 0);
    ASSERT_TRUE(gpuPreviewProcessingApplyDisplayGpuOffscreen(bypassed, pair.frame.data(), bypassedOut.data(), w, h, &reason, &renderer));
    ASSERT_TRUE(bypassedOut == noSh);
}