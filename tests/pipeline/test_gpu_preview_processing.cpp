#include "../common/minitest.h"
#include "../common/hash_helpers.h"
#include "../common/test_artifacts.h"
#include "../common/frame_compare.h"

#include "mlv_pipeline_fixture.h"

#include "../../platform/qt/GpuPreviewProcessing.h"
#include "../../platform/qt/GpuPreviewHostRoute.h"
#include "../../src/processing/raw_processing.h"
#include "../../src/debug/StageTiming.h"
#include "../../src/batch/WorkerThreadCount.h"

#include <QFile>
#include <QtGlobal>
#include <algorithm>
#include <array>
#include <cctype>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <functional>
#include <string>
#include <thread>
#include <vector>

static void assert_gpu_preview_fixture_ready(MlvPipelineFixture & fixture)
{
    QString error_message;
    ASSERT_TRUE(fixture.openTinyDualIso(&error_message));
    ASSERT_TRUE(fixture.loadReceipt(QStringLiteral("tests/fixtures/receipts/tiny_dual_iso_hq.marxml"), &error_message));
    ASSERT_TRUE(fixture.applyReceipt(&error_message));
}

static void configure_gpu_preview_supported_subset(MlvPipelineFixture & fixture)
{
    processingObject_t * processing = fixture.processing();
    ASSERT_TRUE(processing != nullptr);

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

static GpuPreviewProcessingConfig assert_gpu_preview_subset_supported(MlvPipelineFixture & fixture)
{
    configure_gpu_preview_supported_subset(fixture);

    QString reason;
    if( !gpuPreviewProcessingIsSupported(fixture.processing(), &reason) )
    {
        ::minitest::fail(__FILE__, __LINE__,
                         "gpuPreviewProcessingIsSupported(fixture.processing(), &reason)",
                         reason.toStdString());
    }

    const GpuPreviewProcessingConfig config = gpuPreviewProcessingBuildConfig(fixture.processing(), &reason);
    ASSERT_TRUE(config.enabled);
    ASSERT_TRUE(config.signature != 0);
    ASSERT_EQ(static_cast<int>(65536u * sizeof(uint16_t)), config.levelsLut.size());
    ASSERT_EQ(static_cast<int>(65536u * sizeof(uint16_t)), config.gammaLut.size());
    return config;
}

static void assert_gpu_preview_rejects_processing_feature(
    const char * label,
    const QString & expected_reason,
    const std::function<void(processingObject_t *)> & enable_unsupported_feature)
{
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    (void)assert_gpu_preview_subset_supported(fixture);

    processingObject_t * processing = fixture.processing();
    ASSERT_TRUE(processing != nullptr);
    enable_unsupported_feature(processing);

    QString reason;
    if (gpuPreviewProcessingIsSupported(processing, &reason))
    {
        ::minitest::fail(__FILE__, __LINE__,
                         std::string("gpuPreviewProcessingIsSupported rejects ") + label);
    }
    ASSERT_EQ(expected_reason.toStdString(), reason.toStdString());

    const GpuPreviewProcessingConfig rejected_config =
        gpuPreviewProcessingBuildConfig(processing, &reason);
    if (rejected_config.enabled)
    {
        ::minitest::fail(__FILE__, __LINE__,
                         std::string("gpuPreviewProcessingBuildConfig disables ") + label);
    }
    ASSERT_EQ(expected_reason.toStdString(), reason.toStdString());

    test_artifacts::record(std::string("gpu_preview_subset.unsupported.") + label,
                           reason.toStdString());
}

static std::vector<uint16_t> render_gpu_preview_subset_cpu_reference(MlvPipelineFixture & fixture,
                                                                     const GpuPreviewProcessingConfig & config,
                                                                     uint64_t frame_index)
{
    const std::vector<uint16_t> debayered = fixture.renderDebayeredFrame16(frame_index);
    ASSERT_TRUE(!debayered.empty());
    std::vector<uint16_t> output(debayered.size(), 0);
    gpuPreviewProcessingApplyCpuReference(config,
                                          debayered.data(),
                                          output.data(),
                                          fixture.width(),
                                          fixture.height());
    return output;
}

static std::string render_subset_hash(MlvPipelineFixture & fixture,
                                      const GpuPreviewProcessingConfig & config,
                                      uint64_t frame_index)
{
    const std::vector<uint16_t> subset_output =
        render_gpu_preview_subset_cpu_reference(fixture, config, frame_index);
    return sha256_bytes(subset_output.data(), subset_output.size() * sizeof(uint16_t));
}

static GpuPreviewProcessingConfig build_shadows_highlights_config_with_frame_state(
    MlvPipelineFixture & fixture)
{
    configure_gpu_preview_supported_subset(fixture);

    processingObject_t * processing = fixture.processing();
    ASSERT_TRUE(processing != nullptr);
    processingAllowCreativeAdjustments(processing);
    processingSetShadows(processing, 0.32);
    processingSetHighlights(processing, -0.26);

    QString reason;
    ASSERT_TRUE(gpuPreviewProcessingIsSupported(processing, &reason));
    GpuPreviewProcessingConfig config =
        gpuPreviewProcessingBuildConfig(processing, &reason);
    ASSERT_TRUE(config.enabled);
    ASSERT_TRUE(config.applyShadowsHighlights);
    ASSERT_TRUE(gpuPreviewProcessingNeedsShadowsHighlightsFrameState(config));
    ASSERT_TRUE(!gpuPreviewProcessingHasShadowsHighlightsFrameState(
        config, fixture.width(), fixture.height()));

    const std::vector<uint16_t> refreshed =
        fixture.renderFrame16(0, /*threads=*/1);
    ASSERT_TRUE(!refreshed.empty());

    ASSERT_TRUE(gpuPreviewProcessingAttachFrameState(
        &config, processing, fixture.width(), fixture.height(), &reason));
    ASSERT_TRUE(gpuPreviewProcessingHasShadowsHighlightsFrameState(
        config, fixture.width(), fixture.height()));
    ASSERT_TRUE(config.shadowsHighlightsFrameStateReady);
    ASSERT_EQ(static_cast<int>(static_cast<size_t>(fixture.width())
                              * fixture.height() * 3u * sizeof(uint16_t)),
              config.shadowsHighlightsBlur.size());
    ASSERT_EQ(static_cast<int>(65536u * sizeof(float)),
              config.shadowsHighlightsCurve.size());
    return config;
}

/* A skip reason is "expected" when it reflects an absent / unusable GL backend
 * (headless Session-0 CI, no context, software rasterizer) rather than a real
 * shader/parity bug. Mirrors assert_known_gpu_*_skip_reason in the debayer shell
 * test so an UNEXPECTED failure surfaces instead of silently skipping. */
static bool gpu_preview_skip_reason_is_known(const QString & reason)
{
    return reason.contains(QStringLiteral("QOpenGLContext"))
        || reason.contains(QStringLiteral("OpenGL"), Qt::CaseInsensitive)
        || reason.contains(QStringLiteral("context"), Qt::CaseInsensitive)
        || reason.contains(QStringLiteral("offscreen"), Qt::CaseInsensitive)
        || reason.contains(QStringLiteral("framebuffer"), Qt::CaseInsensitive)
        || reason.contains(QStringLiteral("shader"), Qt::CaseInsensitive)
        || reason.contains(QStringLiteral("software"), Qt::CaseInsensitive)
        || reason.contains(QStringLiteral("GPU"))
        || reason.contains(QStringLiteral("backend"), Qt::CaseInsensitive)
        || reason.contains(QStringLiteral("renderer"), Qt::CaseInsensitive);
}

/* CPU-vs-GPU parity harness. Runs the SAME debayered frame through the CPU
 * reference (the local bit-exact oracle) and the real GPU offscreen GLSL path,
 * then asserts the two agree within tolerance. It runs on ANY conformant GL
 * backend -- including Mesa llvmpipe (software GL) on headless Session-0 CI,
 * which still compiles and EXECUTES the exact GLSL, making it a valid oracle for
 * shader LOGIC (unlike the CUDA debayer path, which genuinely needs hardware).
 * It SKIPS (never fails) only when no GL context can be created at all. The same
 * test on the RTX 4090 additionally validates the hardware deployment target.
 * This is the shader-level validation the unit tests above cannot give (they
 * exercise only the CPU reference). Tolerance is provisional: the LUT stages are
 * exact integer lookups, but the float math (HSV, curve multiplies) can deviate
 * a few LSB between CPU and GPU float32; the renderer string + compare summary
 * are recorded so a real-hardware run can tighten these thresholds. */
static void assert_gpu_offscreen_matches_cpu_reference(MlvPipelineFixture & fixture,
                                                       const GpuPreviewProcessingConfig & config,
                                                       const char * label)
{
    ASSERT_TRUE(config.enabled);

    /* Opt into software GL so shader logic is validated even on headless CI
     * (llvmpipe). Harmless on real hardware: the gate it relaxes only fires for
     * software renderers, so the 4090 still runs natively. */
    qputenv("MLVAPP_GPU_PREVIEW_ALLOW_SOFTWARE", QByteArray("1"));

    const GpuPreviewProcessingBackendAvailability availability =
        gpuPreviewProcessingProbeGpuBackend();
    if (!availability.available)
    {
        ASSERT_TRUE(gpu_preview_skip_reason_is_known(availability.reason));
        SKIP_TEST(availability.reason.toStdString());
    }

    const std::vector<uint16_t> debayered = fixture.renderDebayeredFrame16(0);
    ASSERT_TRUE(!debayered.empty());
    const int pixel_count = fixture.width() * fixture.height();
    ASSERT_EQ(static_cast<size_t>(pixel_count) * 3u, debayered.size());

    std::vector<uint16_t> cpu_output(debayered.size(), 0);
    gpuPreviewProcessingApplyCpuReference(config, debayered.data(),
                                          cpu_output.data(), fixture.width(), fixture.height());

    std::vector<uint16_t> gpu_output(debayered.size(), 0);
    QString reason;
    QString renderer;
    const bool ok = gpuPreviewProcessingApplyGpuOffscreen(
        config, debayered.data(), gpu_output.data(),
        fixture.width(), fixture.height(), &reason, &renderer);
    if (!ok)
    {
        ASSERT_TRUE(gpu_preview_skip_reason_is_known(reason));
        SKIP_TEST(reason.toStdString());
    }

    /* Thresholds calibrated against the first real GL run (Mesa llvmpipe, software
     * GL): the supported subset diverges from the CPU reference by max 9 LSB on
     * 0.0013% of samples, and the full creative grade by max 11 LSB on 1.64% (>2
     * LSB). These are float-platform ULP differences amplified at LUT boundaries
     * through the chain of re-quantizing creative stages -- imperceptible (11 /
     * 65535 = 0.017% of range), NOT a logic bug: a real stage error would produce
     * hundreds-to-thousands of LSB or a large mismatch fraction, which these
     * thresholds still catch. Provisional/software-calibrated; the renderer string
     * + compare summary are recorded so an RTX 4090 hardware run can confirm or
     * tighten them (hardware FMA may match the CPU more or less closely). */
    const frame_compare_result_t result = compare_frames_u16(
        cpu_output.data(), gpu_output.data(),
        fixture.width(), fixture.height(), 3, /*per_pixel_tolerance=*/2);
    const frame_tolerance_verdict_t verdict = evaluate_frame_tolerance(
        result, debayered.size(),
        /*max_abs_diff_threshold=*/16, /*max_mismatch_fraction=*/0.03);

    test_artifacts::record(std::string("gpu_preview_subset.gpu_parity.") + label + ".renderer",
                           renderer.toStdString());
    test_artifacts::record(std::string("gpu_preview_subset.gpu_parity.") + label + ".compare",
                           frame_compare_summary(result));

    if (!verdict.passed)
    {
        ::minitest::fail(__FILE__, __LINE__,
                         std::string("GPU offscreen vs CPU reference parity (") + label + ")",
                         verdict.detail);
    }
}

TEST(GpuPreviewProcessing, TinyDualIsoReceiptSubsetGoldenOutputIsStable)
{
    MlvPipelineFixture frame0_fixture;
    assert_gpu_preview_fixture_ready(frame0_fixture);
    const GpuPreviewProcessingConfig frame0_config = assert_gpu_preview_subset_supported(frame0_fixture);
    ASSERT_NEAR(0.0, frame0_config.sourceExposureStops, 0.0001);
    test_artifacts::record("tiny_dual_iso.gpu_preview_subset.frame0",
                           render_subset_hash(frame0_fixture, frame0_config, 0));
    test_artifacts::record("tiny_dual_iso.gpu_preview_subset.signature.frame0",
                           std::to_string(frame0_config.signature));

    MlvPipelineFixture frame1_fixture;
    assert_gpu_preview_fixture_ready(frame1_fixture);
    const GpuPreviewProcessingConfig frame1_config = assert_gpu_preview_subset_supported(frame1_fixture);
    ASSERT_NEAR(0.0, frame1_config.sourceExposureStops, 0.0001);
    test_artifacts::record("tiny_dual_iso.gpu_preview_subset.frame1",
                           render_subset_hash(frame1_fixture, frame1_config, 1));
    test_artifacts::record("tiny_dual_iso.gpu_preview_subset.signature.frame1",
                           std::to_string(frame1_config.signature));
}

TEST(GpuPreviewProcessing, OffscreenResourcesSurviveRepeatedSoftwareGlRuns)
{
    /* This is deliberately a resource-lifecycle contract, not a golden. The
     * hosted CI failure was a null GL control transfer after repeated offscreen
     * resource work. Software GL is allowed here so the contract runs in
     * headless Session-0; production's default software-renderer rejection is
     * unchanged because this test opts in explicitly. */
    qputenv("MLVAPP_GPU_PREVIEW_ALLOW_SOFTWARE", QByteArrayLiteral("1"));
    const GpuPreviewProcessingBackendAvailability availability =
        gpuPreviewProcessingProbeGpuBackend();
    if ( !availability.available )
    {
        ASSERT_TRUE(gpu_preview_skip_reason_is_known(availability.reason));
        SKIP_TEST(availability.reason.toStdString());
    }

    constexpr int width = 8;
    constexpr int height = 6;
    std::vector<uint16_t> input(width * height * 3u);
    std::vector<uint16_t> output(input.size());
    for (size_t i = 0; i < input.size(); ++i)
    {
        input[i] = static_cast<uint16_t>((i * 977u + 123u) & 0xffffu);
    }

    for (int iteration = 0; iteration < 5; ++iteration)
    {
        QString reason;
        QString renderer;
        ASSERT_TRUE(gpuPreviewProcessingApplyBoxBlurOffscreen(
            input.data(), output.data(), width, height, 2, true, true, true,
            &reason, &renderer));
        ASSERT_TRUE(reason.isEmpty());
        ASSERT_TRUE(!renderer.isEmpty());
        ASSERT_TRUE(output != input);
    }
}

TEST(GpuPreviewProcessing, DisplayShaderUsesShadowsHighlightsFrameStateWhenApplied)
{
    /* CUDA-PLAYBACK-LOOK-PARITY-1: the live display shader now applies
     * shadows/highlights (shadowsHighlightsBlurTexture/shadowsHighlightsCurve
     * branch in gpuPreviewProcessingDisplayFragmentShaderSource), so it must
     * request the same per-frame frame-state as the subset shader -- this
     * governs RenderFrameThread's gpuTexNrDisplayLutOnlyShStateBypass gate. */
    GpuPreviewProcessingConfig config;
    config.enabled = true;
    config.applyShadowsHighlights = true;

    ASSERT_TRUE(gpuPreviewProcessingNeedsShadowsHighlightsFrameState(config));
    ASSERT_TRUE(gpuPreviewProcessingDisplayShaderUsesShadowsHighlightsFrameState(config));

    GpuPreviewProcessingConfig disabled;
    disabled.enabled = true;
    disabled.applyShadowsHighlights = false;
    ASSERT_TRUE(!gpuPreviewProcessingDisplayShaderUsesShadowsHighlightsFrameState(disabled));

    GpuPreviewProcessingConfig notEnabled;
    notEnabled.enabled = false;
    notEnabled.applyShadowsHighlights = true;
    ASSERT_TRUE(!gpuPreviewProcessingDisplayShaderUsesShadowsHighlightsFrameState(notEnabled));
}

TEST(GpuPreviewProcessing, ShadowsHighlightsBlurTextureUpdateReportsDropWhenFrameStateMissing)
{
    /* CUDA-PLAYBACK-LOOK-PARITY-1 round-1 v2.1 contract: "no silent drop" --
     * this is the function RenderFrameThread's caller (GpuDisplayWindow /
     * GpuDisplayViewport) relies on to know whether S/H actually reached the
     * screen for this frame. It must report the miss via its return value AND
     * leave shadowsHighlightsBlurReady false, without touching GL at all, so
     * this runs unconditionally (no GPU/context requirement, no SKIP_TEST). */
    GpuPreviewProcessingConfig config;
    config.enabled = true;
    config.applyShadowsHighlights = true;
    /* shadowsHighlightsBlur left empty / shadowsHighlightsFrameStateReady left
     * false -> gpuPreviewProcessingHasShadowsHighlightsFrameState(config, ...)
     * is false, exactly the state RenderFrameThread leaves the config in when
     * gpuTexNrDisplayLutOnlyShStateBypass was still true (fast frame-state
     * refresh never ran) or the refresh itself failed. */

    GpuPreviewProcessingLutTextureSet lutSet;
    const bool updated = gpuPreviewProcessingUpdateShadowsHighlightsBlurTexture(
        lutSet, config, 8, 6);
    ASSERT_TRUE(!updated);
    ASSERT_TRUE(!lutSet.shadowsHighlightsBlurReady);
    ASSERT_TRUE(lutSet.shadowsHighlightsBlur == nullptr);

    gpuPreviewProcessingDestroyLutTextureSet(lutSet);
}

/* ---- ENGINE-ANCHORED display-shader parity (CUDA-PLAYBACK-LOOK-PARITY-1-LAND r2) ----
 *
 * Round 1 compared the display shader with gpuPreviewProcessingApplyCpuReference,
 * an in-file C++ mirror written alongside the shader (those four mirror tests
 * are gone). A mirror shares its author's assumptions, so round 1 passed while
 * both sides clamped the WB-boosted matrix value BEFORE the contrast /
 * shadows-highlights multiply, which the shipped engine does not do (and used
 * the blue-channel Reinhard curve for green in the gamut stage, where the
 * engine uses the plain one). The tests below instead call the PRODUCTION
 * engine (apply_processing_object, or getMlvProcessedFrame16 where the shadows /
 * highlights blur has to come from the engine's own refresh) on the same
 * debayered frame the GPU display shader receives. Only these tests may be
 * cited as "parity with the production engine".
 *
 * The mirror is kept (it is MainWindow's CPU fallback and the subset shader's
 * oracle) and now follows the engine's pre-camera order too, but it still
 * ROUNDS the post-camera gamma index where the engine and the display shader
 * truncate, because the pinned golden hashes depend on that rounding. It is
 * therefore not an engine oracle at the 1-LSB level and no test claims it is.
 *
 * Engine path THIS block anchors to: the generic 16-bit loop in raw_processing.c
 * (contrast / shadows-highlights / vibrance make the creative adjustments
 * non-neutral, which rules out the basic-matrix fast path). That loop applies
 * pix = (uint16_t)LIMIT16(unclamped diagonal-matrix value * expo_correction)
 * BEFORE the camera matrix, with the contrast and shadows-highlights luma taken
 * from the UNclamped diagonal-matrix values. These tests run the shader as the
 * 16-bit route (config.preCameraClamp stays at its default, true).
 *
 * The direct-8-bit kernel (raw_processing_8bit_kernel.inc) omits that clamp and
 * truncation unless AgX or local tone is active. It is the DECLARED PARITY
 * REFERENCE (CUDA-PLAYBACK-LOOK-PARITY-2): the "Direct8Anchored*" tests below
 * call applyProcessingObject8 and drive the shader's route flag the way the app
 * does. The two CPU routes still disagree with each other; making them
 * byte-identical is the separate card CPU-DIRECT8-16BIT-CLAMP-UNIFY. */

/* Rounding budget between the engine (integer LUT indexing with truncation,
 * float32 / double accumulation on the CPU) and the display shader (float32 on
 * the GPU). Measured on this branch (llvmpipe; the cases of
 * docs/cuda-playback-look-parity.md "Engine-anchored parity"): the synthetic
 * sweeps differ from the engine by at most 9 codes (typically 1-3, mean 0.14),
 * the real clip frame by at most 19 codes with at most 8.7e-5 of its samples
 * above 4. The bounds below leave about 1.7x on the worst case (a different GL
 * driver rounds differently) and 11x on the fraction, and sit more than an
 * order of magnitude under the defects they exist to catch (round 1 measured
 * 900-13,000 codes on over-ranged pixels). */
static constexpr uint16_t kEngineParityPerSampleTolerance = 4;
static constexpr uint16_t kEngineParityMaxAbsDiff = 32;
static constexpr double kEngineParityMismatchFraction = 0.001;

static std::vector<uint16_t> run_production_engine_on_frame(processingObject_t * processing,
                                                            const std::vector<uint16_t> & debayered,
                                                            int width,
                                                            int height)
{
    /* apply_processing_object applies the levels LUT to its input in place, so
     * it gets a private copy and the GPU keeps the pristine frame. */
    std::vector<uint16_t> engine_input = debayered;
    std::vector<uint16_t> engine_output(debayered.size(), 0);
    std::vector<uint16_t> engine_blur(debayered.size(), 0);
    apply_processing_object(processing, width, height,
                            engine_input.data(), engine_output.data(),
                            engine_blur.data(), processing->gradient_mask,
                            processing->vignette_mask, nullptr);
    return engine_output;
}

/* Smallest input sample whose leveled value reaches `target` (the levels LUT is
 * monotone), so synthetic frames can be written in leveled units. */
static uint16_t input_sample_for_leveled_value(const processingObject_t * processing, int target)
{
    const uint16_t * levels = processing->pre_calc_levels;
    int low = 0;
    int high = 65535;
    while (low < high)
    {
        const int mid = (low + high) / 2;
        if (levels[mid] >= target) high = mid; else low = mid + 1;
    }
    return static_cast<uint16_t>(low);
}

/* Counts samples whose diagonal-matrix (WB) value exceeds 16 bits, i.e. the
 * pixels where clamping before versus after the exposure multiply differs. */
static size_t count_wb_overrange_samples(const processingObject_t * processing,
                                         const std::vector<uint16_t> & debayered)
{
    size_t count = 0;
    for (size_t index = 0; index < debayered.size(); ++index)
    {
        const int channel = static_cast<int>(index % 3u);
        const uint16_t level = processing->pre_calc_levels[debayered[index]];
        if (processing->pre_calc_matrix[channel * 4][level] > 65535) ++count;
    }
    return count;
}

/* Input sample that lands on diagonal-matrix (WB-applied, pre-exposure) value
 * `target` for `channel`: the smallest leveled value whose pre_calc_matrix entry
 * reaches it (the LUT is monotone), written back through the levels inverse.
 * Targets are expressed AFTER the WB multiply so a frame stays near-neutral
 * (in-gamut) at every white balance, which is what real scenes look like. */
static uint16_t input_sample_for_diagonal_value(const processingObject_t * processing,
                                                int channel,
                                                double target)
{
    const int32_t * matrix = processing->pre_calc_matrix[channel * 4];
    int low = 0;
    int high = 65535;
    while (low < high)
    {
        const int mid = (low + high) / 2;
        if (matrix[mid] >= target) high = mid; else low = mid + 1;
    }
    return input_sample_for_leveled_value(processing, low);
}

/* Smooth synthetic frame: x is a geometric luminance ramp in diagonal-matrix
 * space (500 .. maxDiagonal codes, so it crosses 16 bits), y adds a +-25%
 * red/blue chroma swing around neutral. Smooth, so the engine's blur and the
 * pixel agree and shadows/highlights is exercised across its whole curve. */
static std::vector<uint16_t> make_synthetic_ramp_frame(const processingObject_t * processing,
                                                       double maxDiagonal,
                                                       int * width,
                                                       int * height)
{
    constexpr int kWidth = 128;
    constexpr int kHeight = 64;
    *width = kWidth;
    *height = kHeight;
    std::vector<uint16_t> frame(static_cast<size_t>(kWidth) * kHeight * 3u);
    for (int y = 0; y < kHeight; ++y)
    {
        const double swing = 0.25 * std::sin(6.283185307179586 * y / kHeight);
        const double factors[3] = { 1.0 + swing, 1.0, 1.0 - 0.8 * swing };
        for (int x = 0; x < kWidth; ++x)
        {
            const double luminance = 500.0 * std::pow(maxDiagonal / 500.0, x / double(kWidth - 1));
            for (int channel = 0; channel < 3; ++channel)
            {
                frame[(static_cast<size_t>(y) * kWidth + x) * 3u + channel] =
                    input_sample_for_diagonal_value(processing, channel, luminance * factors[channel]);
            }
        }
    }
    return frame;
}

/* Flat frame holding one pixel given in LEVELED units (the units review used). */
static std::vector<uint16_t> make_flat_leveled_frame(const processingObject_t * processing,
                                                     const int leveled[3],
                                                     int * width,
                                                     int * height)
{
    *width = 16;
    *height = 16;
    std::vector<uint16_t> frame(static_cast<size_t>(16) * 16 * 3u);
    for (size_t index = 0; index < frame.size(); ++index)
        frame[index] = input_sample_for_leveled_value(processing, leveled[index % 3u]);
    return frame;
}

/* CUDA-PLAYBACK-LOOK-PARITY-2 (fable r2 CUDA-LOOK-ENGINE-PARITY-HOSTED-NOT-SKIPPED-1):
 * the display-parity tests used to SKIP whenever no GL backend could be
 * created, so a hosted runner without a usable GL silently turned the whole
 * parity claim into skipped tests. A job that claims parity sets
 * MLVAPP_REQUIRE_DISPLAY_PARITY_GL=1 (the dedicated "display parity" step in
 * .github/workflows/tests.yml); with it set an unavailable backend FAILS with
 * the probe's reason instead of skipping. Unset (a developer box without GL)
 * keeps the skip. */
static bool display_parity_gl_required(void)
{
    return qEnvironmentVariableIntValue("MLVAPP_REQUIRE_DISPLAY_PARITY_GL") != 0;
}

/* Renders `frame` through the LIVE DISPLAY shader offscreen and returns the
 * renderer string. Backend policy, in one place for every display-parity test:
 *   - the render works: proceed (whatever the subset-shader probe says -- that
 *     probe builds the much larger offscreen subset program, which a software GL
 *     such as Qt's bundled Mesa 11.2 opengl32sw refuses with "Too many fragment
 *     shader texture samplers" although the display shader, which binds 9
 *     samplers, runs fine; see docs/cuda-playback-look-parity.md);
 *   - the render fails although the probe says GL works: a display-shader bug,
 *     FAIL (it used to skip, which hid a broken shader as a skipped test);
 *   - the render fails and the probe says no backend: SKIP on a developer box,
 *     FAIL when MLVAPP_REQUIRE_DISPLAY_PARITY_GL is set. */
static QString render_display_for_parity(const GpuPreviewProcessingConfig & config,
                                         const uint16_t * frame,
                                         uint16_t * output,
                                         int width,
                                         int height)
{
    qputenv("MLVAPP_GPU_PREVIEW_ALLOW_SOFTWARE", QByteArray("1"));
    const GpuPreviewProcessingBackendAvailability availability = gpuPreviewProcessingProbeGpuBackend();
    QString reason;
    QString renderer;
    if (gpuPreviewProcessingApplyDisplayGpuOffscreen(config, frame, output, width, height, &reason, &renderer))
    {
        return renderer;
    }
    if (availability.available)
    {
        ::minitest::fail(__FILE__, __LINE__,
                         "display shader offscreen render failed on a working GL backend",
                         reason.toStdString());
    }
    const std::string detail = "probe: " + availability.reason.toStdString() + "; display render: " + reason.toStdString();
    if (display_parity_gl_required())
    {
        ::minitest::fail(__FILE__, __LINE__,
                         "MLVAPP_REQUIRE_DISPLAY_PARITY_GL is set but the display shader cannot run; "
                         "the display parity claim would be skipped, not proven",
                         detail);
    }
    ASSERT_TRUE(gpu_preview_skip_reason_is_known(availability.reason));
    SKIP_TEST(detail);
}

/* Runs the display shader and compares with the engine's output (backend
 * policy: render_display_for_parity). */
static void assert_gpu_display_matches_production_engine(const char * label,
                                                         const GpuPreviewProcessingConfig & config,
                                                         const std::vector<uint16_t> & debayered,
                                                         const std::vector<uint16_t> & engine_output,
                                                         int width,
                                                         int height)
{
    ASSERT_TRUE(config.enabled);
    ASSERT_EQ(debayered.size(), engine_output.size());

    std::vector<uint16_t> gpu_output(debayered.size(), 0);
    const QString renderer =
        render_display_for_parity(config, debayered.data(), gpu_output.data(), width, height);

    const frame_compare_result_t result = compare_frames_u16(
        engine_output.data(), gpu_output.data(), width, height, 3, kEngineParityPerSampleTolerance);
    const frame_tolerance_verdict_t verdict = evaluate_frame_tolerance(
        result, debayered.size(), kEngineParityMaxAbsDiff, kEngineParityMismatchFraction);
    const std::string summary = frame_compare_summary(result);
    std::cout << "[ENGINE-PARITY] " << label << ": " << summary << "\n";
    test_artifacts::record(std::string("gpu_preview_display.engine_parity.") + label + ".renderer",
                           renderer.toStdString());
    test_artifacts::record(std::string("gpu_preview_display.engine_parity.") + label + ".compare", summary);
    if (!verdict.passed)
    {
        ::minitest::fail(__FILE__, __LINE__,
                         std::string("Display shader vs PRODUCTION ENGINE parity (") + label + ")",
                         verdict.detail);
    }
}

struct EngineSweepCase
{
    const char * label;
    double kelvin;
    double tint;
    double contrast;
    double pivot;
    double vibrance;
    double maxDiagonal; /* top of the synthetic luminance ramp, in diagonal-matrix codes */
};

/* PLAYBACK-SEEK-RENDER-PARITY-1: these tests used to switch the receipt's
 * dark/light S-curve off (neutralize_unported_creative_stages) because the
 * display shader had no creative-curve stage, which hid exactly the stage every
 * default receipt uses. The display shader now applies it, so every
 * engine-anchored comparison runs against the FULL engine, S-curve included. */

/* Configures the fixture's processing object for one sweep case. */
static void configure_engine_sweep_case(MlvPipelineFixture & fixture, const EngineSweepCase & sweep)
{
    configure_gpu_preview_supported_subset(fixture);
    processingObject_t * processing = fixture.processing();
    processingAllowCreativeAdjustments(processing);
    processingSetWhiteBalance(processing, sweep.kelvin, sweep.tint);
    if (sweep.contrast != 0.0)
    {
        processingSetSimpleContrast(processing, sweep.contrast);
        processingSetPivot(processing, sweep.pivot);
    }
    if (sweep.vibrance != 1.0) processingSetVibrance(processing, sweep.vibrance);
    (void)fixture.renderDebayeredFrame16(0); /* settle LUTs exactly like the mirror tests */
}

static void run_engine_anchored_synthetic_case(const EngineSweepCase & sweep)
{
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    configure_engine_sweep_case(fixture, sweep);
    processingObject_t * processing = fixture.processing();

    QString reason;
    ASSERT_TRUE(gpuPreviewProcessingIsSupported(processing, &reason));
    const GpuPreviewProcessingConfig config = gpuPreviewProcessingBuildConfig(processing, &reason);
    ASSERT_TRUE(config.enabled);
    ASSERT_TRUE(!config.applyShadowsHighlights);

    int width = 0;
    int height = 0;
    const std::vector<uint16_t> frame =
        make_synthetic_ramp_frame(processing, sweep.maxDiagonal, &width, &height);
    const std::vector<uint16_t> engine = run_production_engine_on_frame(processing, frame, width, height);
    assert_gpu_display_matches_production_engine(sweep.label, config, frame, engine, width, height);
}

static void run_engine_anchored_flat_pixel_case(const char * label,
                                                double kelvin,
                                                double contrast,
                                                double pivot,
                                                const int leveled[3],
                                                bool requireWbOverrange)
{
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    configure_engine_sweep_case(fixture, { label, kelvin, 0.0, contrast, pivot, 1.0, 0.0 });
    processingObject_t * processing = fixture.processing();

    QString reason;
    ASSERT_TRUE(gpuPreviewProcessingIsSupported(processing, &reason));
    const GpuPreviewProcessingConfig config = gpuPreviewProcessingBuildConfig(processing, &reason);
    ASSERT_TRUE(config.enabled);
    ASSERT_TRUE(config.applyInLoopContrast);

    int width = 0;
    int height = 0;
    const std::vector<uint16_t> frame = make_flat_leveled_frame(processing, leveled, &width, &height);
    if (requireWbOverrange)
    {
        ASSERT_TRUE(count_wb_overrange_samples(processing, frame) > 0);
    }
    const std::vector<uint16_t> engine = run_production_engine_on_frame(processing, frame, width, height);
    assert_gpu_display_matches_production_engine(label, config, frame, engine, width, height);
}

TEST(GpuPreviewProcessing, EngineAnchoredSolR1ReproContrastOverrangeMatchesEngine)
{
    /* Sol r1 BLOCKER repro: Canon 5D3 matrix (tiny_dual_iso), WB 6500/tint 0,
     * Rec709, contrast 0.14 pivot 0.46, every other stage off. The leveled
     * pixel [27000,61000,43000] becomes [63818,61000,63520] after the diagonal
     * matrix and the contrast factor (1.0957) pushes two channels past 16 bits,
     * where the engine clamps and truncates BEFORE the camera matrix. Sol's
     * numbers: GLSL [64090,64346,64293] vs CPU [60454,64592,61692]. */
    const int kSolLeveled[3] = { 27000, 61000, 43000 };
    run_engine_anchored_flat_pixel_case("sol_r1_flat_pixel_6500K", 6500.0, 0.14, 0.46, kSolLeveled, false);
}

TEST(GpuPreviewProcessing, EngineAnchoredFableR1WbOverrangeHighlightMatchesEngine)
{
    /* Fable r1 repro: at 3000 K the blue multiplier is ~2.86, so a leveled blue
     * of 30000 is ~85900 BEFORE the exposure multiply. The engine multiplies the
     * unclamped value and clamps afterwards; the round-1 shader clamped the
     * matrix LUT to 16 bits first. Contrast supplies a factor below 1. */
    const int kFableLeveled[3] = { 30000, 30000, 30000 };
    run_engine_anchored_flat_pixel_case("fable_r1_flat_pixel_3000K", 3000.0, -0.30, 0.5, kFableLeveled, true);
    run_engine_anchored_synthetic_case({ "fable_r1_ramp_3000K", 3000.0, 0.0, -0.30, 0.5, 1.0, 130000.0 });
}

TEST(GpuPreviewProcessing, EngineAnchoredContrastVibranceWbSweepMatchesEngine)
{
    /* Sweep over contrast / pivot / vibrance and WB extremes, one fixture and
     * one GL context per case. Contrast is the stage that multiplies the WB
     * boosted value, vibrance runs after gamma; both are covered alone and
     * together at cool, neutral and warm WB. The ramp reaches 130000 diagonal
     * codes, i.e. twice the 16-bit range. */
    static const EngineSweepCase kCases[] = {
        { "wb6500_contrast_pos",       6500.0,   0.0,  0.14, 0.46, 1.0,  130000.0 },
        { "wb6500_contrast_neg",       6500.0,   0.0, -0.30, 0.50, 1.0,  130000.0 },
        { "wb6500_contrast_strong",    6500.0,   0.0,  0.60, 0.30, 1.0,  130000.0 },
        { "wb2500_contrast_pos",       2500.0,   0.0,  0.14, 0.46, 1.0,  130000.0 },
        { "wb2500_contrast_strong",    2500.0,  30.0,  0.60, 0.30, 1.0,  130000.0 },
        { "wb3000_contrast_neg",       3000.0,   0.0, -0.30, 0.50, 1.0,  130000.0 },
        { "wb10000_contrast_pos",     10000.0,   0.0,  0.14, 0.46, 1.0,  130000.0 },
        { "wb10000_contrast_strong",  10000.0, -30.0,  0.60, 0.70, 1.0,  130000.0 },
        { "wb6500_vibrance_up",        6500.0,   0.0,  0.0,  0.50, 1.03, 130000.0 },
        { "wb6500_vibrance_big",       6500.0,   0.0,  0.0,  0.50, 1.60, 130000.0 },
        { "wb6500_vibrance_down",      6500.0,   0.0,  0.0,  0.50, 0.70, 130000.0 },
        { "wb2500_vibrance_up",        2500.0,   0.0,  0.0,  0.50, 1.30, 130000.0 },
        { "wb3000_contrast_vibrance",  3000.0,   0.0,  0.14, 0.46, 1.03, 130000.0 },
        { "wb10000_contrast_vibrance", 10000.0,  0.0,  0.60, 0.30, 1.60, 130000.0 },
    };
    for (const EngineSweepCase & sweep : kCases)
    {
        run_engine_anchored_synthetic_case(sweep);
    }
}

TEST(GpuPreviewProcessing, EngineAnchoredNeutralStagesMatchEngineRoundingFloor)
{
    /* Control: contrast a hair non-neutral (0.02, which keeps the engine on the
     * generic loop this card anchors to; fully neutral stages would route the
     * engine through its basic-matrix fast path, which has no pre-camera clamp
     * at all) and the ramp capped at 60000 diagonal codes so almost nothing
     * over-ranges. This is the rounding floor the tolerances above are
     * justified by. */
    run_engine_anchored_synthetic_case({ "rounding_floor_wb5600", 5600.0, 0.0, 0.02, 0.5, 1.0, 60000.0 });
}

/* Engine-anchored shadows/highlights on a synthetic ramp: the engine's own blur
 * refresh (processingRefreshShadowsHighlightsBlurFromRgb16, the function the
 * live fast path calls) builds the blur, apply_processing_object consumes it,
 * and the GPU gets the very same buffer through the production attach path. */
static void run_engine_anchored_synthetic_shadows_highlights_case(const char * label,
                                                                  double kelvin,
                                                                  double shadows,
                                                                  double highlights,
                                                                  double contrast,
                                                                  double pivot,
                                                                  double vibrance,
                                                                  double maxDiagonal)
{
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    configure_engine_sweep_case(fixture, { label, kelvin, 0.0, contrast, pivot, vibrance, maxDiagonal });
    processingObject_t * processing = fixture.processing();
    processingSetShadows(processing, shadows);
    processingSetHighlights(processing, highlights);
    (void)fixture.renderDebayeredFrame16(0);

    QString reason;
    ASSERT_TRUE(gpuPreviewProcessingIsSupported(processing, &reason));
    GpuPreviewProcessingConfig config = gpuPreviewProcessingBuildConfig(processing, &reason);
    ASSERT_TRUE(config.enabled);
    ASSERT_TRUE(config.applyShadowsHighlights);

    int width = 0;
    int height = 0;
    const std::vector<uint16_t> frame = make_synthetic_ramp_frame(processing, maxDiagonal, &width, &height);
    ASSERT_TRUE(count_wb_overrange_samples(processing, frame) > 0);

    std::vector<uint16_t> refresh_input = frame;
    ASSERT_TRUE(processingRefreshShadowsHighlightsBlurFromRgb16(
                    processing, refresh_input.data(), width, height, /*threads=*/1,
                    /*forceExportPolicy=*/1) != 0);
    const uint16_t * blur = nullptr;
    int blur_width = 0;
    int blur_height = 0;
    int curve_index_mask = 0;
    ASSERT_TRUE(processingGetShadowsHighlightsBlurData(processing, &blur, &blur_width, &blur_height,
                                                       &curve_index_mask) != 0);
    ASSERT_EQ(width, blur_width);
    ASSERT_EQ(height, blur_height);
    std::vector<uint16_t> engine_blur(blur, blur + static_cast<size_t>(width) * height * 3u);
    ASSERT_TRUE(gpuPreviewProcessingAttachFrameState(&config, processing, width, height, &reason));

    std::vector<uint16_t> engine_input = frame;
    std::vector<uint16_t> engine(frame.size(), 0);
    apply_processing_object(processing, width, height, engine_input.data(), engine.data(),
                            engine_blur.data(), processing->gradient_mask,
                            processing->vignette_mask, nullptr);
    assert_gpu_display_matches_production_engine(label, config, frame, engine, width, height);
}

TEST(GpuPreviewProcessing, EngineAnchoredShadowsHighlightsWbOverrangeMatchesEngine)
{
    /* Fable r1: highlights -26 (factor ~0.85 in the bright part of the curve)
     * on WB-overranged blue, plus shadows +32, at 3000 K and at 6500 K. */
    run_engine_anchored_synthetic_shadows_highlights_case(
        "sh_ramp_wb3000", 3000.0, 0.32, -0.26, 0.0, 0.5, 1.0, 130000.0);
    run_engine_anchored_synthetic_shadows_highlights_case(
        "sh_ramp_wb6500", 6500.0, 0.32, -0.26, 0.0, 0.5, 1.0, 130000.0);
    run_engine_anchored_synthetic_shadows_highlights_case(
        "sh_ramp_wb3000_strong", 3000.0, 0.60, -0.60, 0.0, 0.5, 1.0, 130000.0);
}

TEST(GpuPreviewProcessing, EngineAnchoredNightPresetMatchesEngine)
{
    /* The owner's night preset (contrast 14 pivot 46 shadows 32 highlights -26
     * vibrance 3) at tungsten and daylight WB, on the over-ranging ramp. */
    run_engine_anchored_synthetic_shadows_highlights_case(
        "night_preset_ramp_wb3000", 3000.0, 0.32, -0.26, 0.14, 0.46, 1.03, 130000.0);
    run_engine_anchored_synthetic_shadows_highlights_case(
        "night_preset_ramp_wb6500", 6500.0, 0.32, -0.26, 0.14, 0.46, 1.03, 130000.0);
}


/* Real-clip frame through the WHOLE production render (getMlvProcessedFrame16:
 * debayer, blur refresh, apply_processing_object) against the display shader.
 * The config is built AFTER the priming render because the engine only
 * refreshes its LUTs (contrast curve, matrix, gamma) inside a render; a config
 * built before it would carry the pre-settings LUTs. Returns the engine frame. */
static std::vector<uint16_t> assert_gpu_display_matches_engine_on_real_frame(
    MlvPipelineFixture & fixture,
    const char * label,
    bool expectContrast,
    bool expectShadowsHighlights,
    bool expectVibrance)
{
    processingObject_t * processing = fixture.processing();
    QString reason;
    ASSERT_TRUE(gpuPreviewProcessingIsSupported(processing, &reason));
    const std::vector<uint16_t> primed = fixture.renderFrame16(0, /*threads=*/1);
    ASSERT_TRUE(!primed.empty());

    GpuPreviewProcessingConfig config = gpuPreviewProcessingBuildConfig(processing, &reason);
    ASSERT_TRUE(config.enabled);
    ASSERT_TRUE(config.applyInLoopContrast == expectContrast);
    ASSERT_TRUE(config.applyShadowsHighlights == expectShadowsHighlights);
    ASSERT_TRUE(config.applyVibrance == expectVibrance);
    if (config.applyShadowsHighlights)
    {
        ASSERT_TRUE(gpuPreviewProcessingAttachFrameState(
            &config, processing, fixture.width(), fixture.height(), &reason));
    }

    std::vector<uint16_t> engine = fixture.renderFrame16(0, /*threads=*/1);
    const std::vector<uint16_t> debayered = fixture.renderDebayeredFrame16(0);
    ASSERT_EQ(debayered.size(), engine.size());
    assert_gpu_display_matches_production_engine(label, config, debayered, engine,
                                                 fixture.width(), fixture.height());
    return engine;
}

TEST(GpuPreviewProcessing, DisplayShaderContrastPivotMatchesProductionEngine)
{
    /* Contrast + pivot was silently dropped by the live display shader in the
     * original defect. A no-op contrast stage would leave the engine frame
     * equal to the neutral frame, which the last assertion forbids. */
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    configure_gpu_preview_supported_subset(fixture);
    processingObject_t * processing = fixture.processing();
    processingAllowCreativeAdjustments(processing);
    const std::vector<uint16_t> neutral = fixture.renderFrame16(0, /*threads=*/1);

    processingSetSimpleContrast(processing, 0.14);
    processingSetPivot(processing, 0.46);
    const std::vector<uint16_t> engine = assert_gpu_display_matches_engine_on_real_frame(
        fixture, "real_frame_contrast_pivot", true, false, false);
    ASSERT_TRUE(neutral != engine);
}

TEST(GpuPreviewProcessing, DisplayShaderVibranceMatchesProductionEngine)
{
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    configure_gpu_preview_supported_subset(fixture);
    processingObject_t * processing = fixture.processing();
    processingAllowCreativeAdjustments(processing);
    const std::vector<uint16_t> neutral = fixture.renderFrame16(0, /*threads=*/1);

    processingSetVibrance(processing, 1.03);
    const std::vector<uint16_t> engine = assert_gpu_display_matches_engine_on_real_frame(
        fixture, "real_frame_vibrance", false, false, true);
    ASSERT_TRUE(neutral != engine);
}

TEST(GpuPreviewProcessing, DisplayShaderShadowsHighlightsMatchesProductionEngine)
{
    /* The fast S/H frame-state path was always bypassed for the live display
     * shader (gpuTexNrDisplayLutOnlyShStateBypass), so the blur it computes
     * never reached the screen. The blur here is the engine's own. */
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    configure_gpu_preview_supported_subset(fixture);
    processingObject_t * processing = fixture.processing();
    processingAllowCreativeAdjustments(processing);
    const std::vector<uint16_t> neutral = fixture.renderFrame16(0, /*threads=*/1);

    processingSetShadows(processing, 0.32);
    processingSetHighlights(processing, -0.26);
    const std::vector<uint16_t> engine = assert_gpu_display_matches_engine_on_real_frame(
        fixture, "real_frame_shadows_highlights", false, true, false);
    ASSERT_TRUE(neutral != engine);
}

TEST(GpuPreviewProcessing, DisplayShaderCombinedLookAssistPresetMatchesProductionEngine)
{
    /* The owner's reported night preset (contrast=14 pivot=46 shadows=32
     * highlights=-26 vibrance=3) must reach the live display shader together,
     * not just individually. */
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    configure_gpu_preview_supported_subset(fixture);
    processingObject_t * processing = fixture.processing();
    processingAllowCreativeAdjustments(processing);
    const std::vector<uint16_t> neutral = fixture.renderFrame16(0, /*threads=*/1);

    processingSetSimpleContrast(processing, 0.14);
    processingSetPivot(processing, 0.46);
    processingSetShadows(processing, 0.32);
    processingSetHighlights(processing, -0.26);
    processingSetVibrance(processing, 1.03);
    const std::vector<uint16_t> engine = assert_gpu_display_matches_engine_on_real_frame(
        fixture, "real_frame_night_preset_combined", true, true, true);
    ASSERT_TRUE(neutral != engine);
}

/* ---- DIRECT8-ANCHORED display-shader parity (CUDA-PLAYBACK-LOOK-PARITY-2) ----
 *
 * DECLARED PARITY REFERENCE: the direct-8-bit preview route -- what CPU preview
 * runs when the receipt is direct8-eligible and the input is cheap, i.e. the
 * route CUDA playback replaces. These tests call the real applyProcessingObject8
 * (the shared C kernel for contrast / S-H / vibrance / saturation / AgX, the
 * intrinsics kernel for neutral receipts on an AVX2 host, exactly the dispatch
 * the app makes) and compare its 8-bit output with the display shader's output
 * (>> 8) for the SAME debayered frame, with the shader's route flag set the way
 * the host sets it (gpuPreviewProcessingApplyCpuRoute).
 *
 * Where the CPU would NOT take direct8 (a state the kernel cannot run, or an
 * input the cheapness gate refuses) it takes the generic 16-bit loop, which has
 * its own pre-camera behaviour; every cell therefore ALSO runs the shader as
 * that route against apply_processing_object. The two CPU routes disagree with
 * each other on over-ranged highlights when only vibrance (or nothing) is on;
 * the 8-bit-vs-16-bit delta of every cell is printed and recorded as
 * INFORMATION (the card that unifies the CPU routes is
 * CPU-DIRECT8-16BIT-CLAMP-UNIFY). */

/* Budget between the direct8 kernel (integer LUTs, float32/double CPU) and the
 * display shader (float32 GPU), in 8-bit codes. A one-code flip at a rounding
 * boundary is allowed per sample; the defects this exists to catch are an order
 * of magnitude larger (sol r2: [255,230,243] against [245,239,240]). */
static constexpr uint8_t kDirect8ParityPerSampleTolerance = 1;
static constexpr uint16_t kDirect8ParityMaxAbsDiff = 3;
static constexpr double kDirect8ParityMismatchFraction = 0.001;

static std::vector<uint8_t> run_direct8_engine_on_frame(processingObject_t * processing,
                                                        const std::vector<uint16_t> & debayered,
                                                        int width,
                                                        int height)
{
    /* applyProcessingObject8 applies the levels LUT to its input in place. */
    std::vector<uint16_t> engine_input = debayered;
    std::vector<uint8_t> engine_output(debayered.size(), 0);
    ASSERT_TRUE(processingCanUseDirect8BitOutput(processing) != 0);
    applyProcessingObject8(processing, width, height,
                           engine_input.data(), engine_output.data(),
                           /*threads=*/1, /*imageChanged=*/1, /*frameIndex=*/0);
    return engine_output;
}

struct DisplayVsEngine8Result
{
    frame_compare_result_t compare;
    frame_tolerance_verdict_t verdict;
    std::string summary;
    QString renderer;
};

/* Runs the display shader offscreen and compares its output (>> 8) with an
 * 8-bit engine frame. Backend policy as require_display_parity_backend_or_skip. */
static DisplayVsEngine8Result compare_display_with_engine8(const GpuPreviewProcessingConfig & config,
                                                           const std::vector<uint16_t> & debayered,
                                                           const std::vector<uint8_t> & engine8,
                                                           int width,
                                                           int height)
{
    ASSERT_TRUE(config.enabled);
    ASSERT_EQ(debayered.size(), engine8.size());
    std::vector<uint16_t> gpu_output(debayered.size(), 0);
    DisplayVsEngine8Result out{};
    out.renderer = render_display_for_parity(config, debayered.data(), gpu_output.data(), width, height);
    std::vector<uint8_t> gpu8(gpu_output.size());
    for (size_t index = 0; index < gpu_output.size(); ++index)
    {
        gpu8[index] = static_cast<uint8_t>(gpu_output[index] >> 8);
    }
    out.compare = compare_frames_u8(engine8.data(), gpu8.data(), width, height, 3,
                                    kDirect8ParityPerSampleTolerance);
    out.verdict = evaluate_frame_tolerance(out.compare, debayered.size(),
                                           kDirect8ParityMaxAbsDiff, kDirect8ParityMismatchFraction);
    out.summary = frame_compare_summary(out.compare);
    return out;
}

static void assert_gpu_display_matches_direct8_engine(const char * label,
                                                      const GpuPreviewProcessingConfig & config,
                                                      const std::vector<uint16_t> & debayered,
                                                      const std::vector<uint8_t> & engine8,
                                                      int width,
                                                      int height)
{
    const DisplayVsEngine8Result result =
        compare_display_with_engine8(config, debayered, engine8, width, height);
    std::cout << "[DIRECT8-PARITY] " << label << " preCameraClamp=" << (config.preCameraClamp ? 1 : 0)
              << ": " << result.summary << "\n";
    test_artifacts::record(std::string("gpu_preview_display.direct8_parity.") + label + ".renderer",
                           result.renderer.toStdString());
    test_artifacts::record(std::string("gpu_preview_display.direct8_parity.") + label + ".compare",
                           result.summary);
    if (!result.verdict.passed)
    {
        ::minitest::fail(__FILE__, __LINE__,
                         std::string("Display shader vs DIRECT8 route parity (") + label + ")",
                         result.verdict.detail);
    }
}

/* The 16-bit engine's output as 8-bit, for the informational CPU-route delta. */
static std::vector<uint8_t> engine16_as_8bit(const std::vector<uint16_t> & engine16)
{
    std::vector<uint8_t> out(engine16.size());
    for (size_t index = 0; index < engine16.size(); ++index)
    {
        out[index] = static_cast<uint8_t>(engine16[index] >> 8);
    }
    return out;
}

struct RouteCell
{
    const char * label;
    double kelvin;
    double tint;
    bool allowCreative;   /* processingAllowCreativeAdjustments (vibrance etc. are inert without it) */
    double contrast;      /* simple contrast; 0 = off */
    double pivot;
    double shadows;
    double highlights;
    double vibrance;      /* 1.0 = off */
    double maxDiagonal;   /* top of the synthetic ramp (diagonal-matrix codes); ignored for flat cells */
    const int * flatLeveled; /* non-null: a flat frame in LEVELED units instead of the ramp */
    bool direct8Clamp;    /* hand-derived from raw_processing_8bit_kernel.inc: AgX || contrast || S/H */
    bool generic16Clamp;  /* hand-derived from raw_processing.c: always, except the basic-matrix branch */
};

static void configure_route_cell(MlvPipelineFixture & fixture, const RouteCell & cell)
{
    configure_gpu_preview_supported_subset(fixture);
    processingObject_t * processing = fixture.processing();
    if (cell.allowCreative)
    {
        processingAllowCreativeAdjustments(processing);
    }
    processingSetWhiteBalance(processing, cell.kelvin, cell.tint);
    processingSetSimpleContrast(processing, cell.contrast);
    processingSetPivot(processing, cell.pivot);
    processingSetShadows(processing, cell.shadows);
    processingSetHighlights(processing, cell.highlights);
    processingSetVibrance(processing, cell.vibrance);
    (void)fixture.renderDebayeredFrame16(0); /* settle LUTs exactly like the other engine-anchored tests */
}

static void run_route_cell(MlvPipelineFixture & fixture, const RouteCell & cell, bool requireOverrange = false)
{
    configure_route_cell(fixture, cell);
    processingObject_t * processing = fixture.processing();

    QString reason;
    ASSERT_TRUE(gpuPreviewProcessingIsSupported(processing, &reason));
    const GpuPreviewProcessingConfig base = gpuPreviewProcessingBuildConfig(processing, &reason);
    ASSERT_TRUE(base.enabled);
    const bool localTone = cell.contrast != 0.0 || cell.shadows != 0.0 || cell.highlights != 0.0;
    ASSERT_TRUE(base.applyShadowsHighlights == (cell.shadows != 0.0 || cell.highlights != 0.0));

    int width = 0;
    int height = 0;
    std::vector<uint16_t> frame;
    if (cell.flatLeveled)
    {
        frame = make_flat_leveled_frame(processing, cell.flatLeveled, &width, &height);
    }
    else
    {
        frame = make_synthetic_ramp_frame(processing, cell.maxDiagonal, &width, &height);
    }
    const size_t overrange = count_wb_overrange_samples(processing, frame);
    std::cout << "[CELL] " << cell.label << " wb-overranged samples=" << overrange << "\n";
    if (requireOverrange)
    {
        ASSERT_TRUE(overrange > 0);
    }

    /* ---- route 1: direct8 (the declared parity reference) ---- */
    ASSERT_TRUE(processingCanUseDirect8BitOutput(processing) != 0);
    ASSERT_EQ(cell.direct8Clamp, processingCpuRoutePreCameraClamps(processing, 1) != 0);
    ASSERT_EQ(localTone, cell.direct8Clamp);   /* these cells carry no AgX */
    const std::vector<uint8_t> engine8 = run_direct8_engine_on_frame(processing, frame, width, height);

    GpuPreviewProcessingConfig direct8Config = base;
    if (direct8Config.applyShadowsHighlights)
    {
        /* the blur the direct8 kernel itself just computed */
        ASSERT_TRUE(gpuPreviewProcessingAttachFrameState(&direct8Config, processing, width, height, &reason));
    }
    gpuPreviewProcessingApplyCpuRoute(&direct8Config, processing, /*direct8Route=*/true);
    ASSERT_EQ(cell.direct8Clamp, direct8Config.preCameraClamp);
    assert_gpu_display_matches_direct8_engine(cell.label, direct8Config, frame, engine8, width, height);

    /* ---- route 2: the generic 16-bit loop (what the CPU runs when direct8 is
     * refused for this scale/input), gated wherever the CPU would choose it ---- */
    ASSERT_EQ(cell.generic16Clamp, processingCpuRoutePreCameraClamps(processing, 0) != 0);
    GpuPreviewProcessingConfig generic16Config = base;
    std::vector<uint16_t> engine_blur(frame.size(), 0);
    if (generic16Config.applyShadowsHighlights)
    {
        std::vector<uint16_t> refresh_input = frame;
        ASSERT_TRUE(processingRefreshShadowsHighlightsBlurFromRgb16(
                        processing, refresh_input.data(), width, height, /*threads=*/1,
                        /*forceExportPolicy=*/1) != 0);
        const uint16_t * blur = nullptr;
        int blur_width = 0;
        int blur_height = 0;
        int curve_index_mask = 0;
        ASSERT_TRUE(processingGetShadowsHighlightsBlurData(processing, &blur, &blur_width, &blur_height,
                                                           &curve_index_mask) != 0);
        engine_blur.assign(blur, blur + static_cast<size_t>(width) * height * 3u);
        ASSERT_TRUE(gpuPreviewProcessingAttachFrameState(&generic16Config, processing, width, height, &reason));
    }
    gpuPreviewProcessingApplyCpuRoute(&generic16Config, processing, /*direct8Route=*/false);
    ASSERT_EQ(cell.generic16Clamp, generic16Config.preCameraClamp);
    std::vector<uint16_t> engine16_input = frame;
    std::vector<uint16_t> engine16(frame.size(), 0);
    apply_processing_object(processing, width, height, engine16_input.data(), engine16.data(),
                            engine_blur.data(), processing->gradient_mask,
                            processing->vignette_mask, nullptr);
    const std::string label16 = std::string(cell.label) + ".as16bit";
    assert_gpu_display_matches_production_engine(label16.c_str(), generic16Config, frame, engine16, width, height);

    /* ---- INFORMATION, not a gate: how far apart the two CPU routes are here ---- */
    const std::vector<uint8_t> engine16_8 = engine16_as_8bit(engine16);
    const frame_compare_result_t routeDelta =
        compare_frames_u8(engine8.data(), engine16_8.data(), width, height, 3, 1);
    const std::string routeDeltaSummary = frame_compare_summary(routeDelta);
    std::cout << "[CPU-ROUTE-DELTA] " << cell.label << " direct8 vs 16-bit (info): " << routeDeltaSummary
              << " (intrin=" << processingFastPathAvx2IntrinActive() << ")\n";
    test_artifacts::record(std::string("gpu_preview_display.cpu_route_delta.") + cell.label, routeDeltaSummary);
}

static const int kSolR2LeveledA[3] = { 40000, 61000, 43000 };
static const int kSolR2LeveledB[3] = { 27000, 61000, 60000 };
static const int kSolR1Leveled[3]  = { 27000, 61000, 43000 };
static const int kFableR1Leveled[3] = { 30000, 30000, 30000 };

TEST(GpuPreviewProcessing, Direct8AnchoredSolR2VibranceOnlyReproMatchesDirect8Route)
{
    /* sol r2 BLOCKER: Canon 5D3 matrix, WB 6500/tint 0, Rec709, vibrance 1.03,
     * contrast / S-H / AgX off. Leveled [40000,61000,43000] gives a diagonal of
     * [94545,61000,63520]; the direct8 kernel (no local tone, no AgX) feeds that
     * UNclamped into the camera matrix, where the round-2 shader's unconditional
     * clamp gave a different colour. Sol's source arithmetic put the gap at ~10
     * 8-bit codes; the real kernels measure 3 codes (pixel A) and 6 codes (pixel
     * B, leveled [27000,61000,60000]) -- the [CPU-ROUTE-DELTA] lines compare the
     * 16-bit route with the kernel, which is what the legacy flag reproduced. */
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    run_route_cell(fixture, { "sol_r2_repro_a_vibrance_6500K", 6500.0, 0.0, true, 0.0, 0.5, 0.0, 0.0, 1.03,
                              0.0, kSolR2LeveledA, false, true }, /*requireOverrange=*/true);
    run_route_cell(fixture, { "sol_r2_repro_b_vibrance_6500K", 6500.0, 0.0, true, 0.0, 0.5, 0.0, 0.0, 1.03,
                              0.0, kSolR2LeveledB, false, true }, /*requireOverrange=*/true);
}

TEST(GpuPreviewProcessing, Direct8AnchoredLegacyUnconditionalClampIsDetected)
{
    /* RED-FIRST proof, kept as a regression: the round-2 shader clamped before
     * the camera matrix unconditionally, i.e. exactly config.preCameraClamp =
     * true. Forcing that on sol's vibrance-only repro must FAIL the direct8
     * comparison (it is what failed on 8e928529); the host-chosen flag passes
     * (Direct8AnchoredSolR2VibranceOnlyReproMatchesDirect8Route). */
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    const RouteCell cell = { "legacy_clamp_probe", 6500.0, 0.0, true, 0.0, 0.5, 0.0, 0.0, 1.03,
                             0.0, kSolR2LeveledB, false, true };
    configure_route_cell(fixture, cell);
    processingObject_t * processing = fixture.processing();
    QString reason;
    GpuPreviewProcessingConfig config = gpuPreviewProcessingBuildConfig(processing, &reason);
    ASSERT_TRUE(config.enabled);
    int width = 0;
    int height = 0;
    const std::vector<uint16_t> frame = make_flat_leveled_frame(processing, cell.flatLeveled, &width, &height);
    const std::vector<uint8_t> engine8 = run_direct8_engine_on_frame(processing, frame, width, height);

    config.preCameraClamp = true; /* the round-2 behaviour */
    const DisplayVsEngine8Result legacy = compare_display_with_engine8(config, frame, engine8, width, height);
    std::cout << "[DIRECT8-PARITY] legacy_clamp_probe preCameraClamp=1 (expected to FAIL): " << legacy.summary << "\n";
    ASSERT_TRUE(!legacy.verdict.passed);
    ASSERT_TRUE(legacy.compare.max_abs_diff >= 4); /* measured 6 on this pixel */

    gpuPreviewProcessingApplyCpuRoute(&config, processing, /*direct8Route=*/true);
    ASSERT_TRUE(!config.preCameraClamp);
    const DisplayVsEngine8Result routed = compare_display_with_engine8(config, frame, engine8, width, height);
    ASSERT_TRUE(routed.verdict.passed);
}

TEST(GpuPreviewProcessing, Direct8AnchoredNeutralAndVibranceMatchDirect8Route)
{
    /* Controls with NO local tone: the direct8 kernel does not clamp before the
     * camera matrix. Neutral (creative off and on), vibrance up / big / down at
     * cool, neutral and warm WB, a bare pivot (inert without contrast) and the
     * r1 repros' pixels with vibrance. The ramp reaches twice the 16-bit range. */
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    static const RouteCell kCells[] = {
        { "neutral_creative_off_wb6500",  6500.0,  0.0, false, 0.0, 0.5, 0.0, 0.0, 1.00, 130000.0, nullptr, false, false },
        { "neutral_creative_off_wb3000",  3000.0,  0.0, false, 0.0, 0.5, 0.0, 0.0, 1.00, 130000.0, nullptr, false, false },
        { "neutral_creative_on_wb2500",   2500.0, 30.0, true,  0.0, 0.5, 0.0, 0.0, 1.00, 130000.0, nullptr, false, false },
        { "pivot_only_wb6500",            6500.0,  0.0, true,  0.0, 0.30, 0.0, 0.0, 1.00, 130000.0, nullptr, false, false },
        { "vibrance_up_wb6500",           6500.0,  0.0, true,  0.0, 0.5, 0.0, 0.0, 1.03, 130000.0, nullptr, false, true },
        { "vibrance_big_wb6500",          6500.0,  0.0, true,  0.0, 0.5, 0.0, 0.0, 1.60, 130000.0, nullptr, false, true },
        { "vibrance_down_wb6500",         6500.0,  0.0, true,  0.0, 0.5, 0.0, 0.0, 0.70, 130000.0, nullptr, false, true },
        { "vibrance_up_wb2500_tint30",    2500.0, 30.0, true,  0.0, 0.5, 0.0, 0.0, 1.30, 130000.0, nullptr, false, true },
        { "vibrance_up_wb10000_tint-30", 10000.0, -30.0, true, 0.0, 0.5, 0.0, 0.0, 1.30, 130000.0, nullptr, false, true },
        { "vibrance_sol_r1_pixel_6500K",  6500.0,  0.0, true,  0.0, 0.5, 0.0, 0.0, 1.03, 0.0, kSolR1Leveled, false, true },
        { "vibrance_fable_r1_pixel_3000K", 3000.0, 0.0, true,  0.0, 0.5, 0.0, 0.0, 1.03, 0.0, kFableR1Leveled, false, true },
    };
    for (const RouteCell & cell : kCells)
    {
        run_route_cell(fixture, cell);
    }
}

TEST(GpuPreviewProcessing, Direct8AnchoredLocalToneMatchesDirect8Route)
{
    /* Local tone (contrast, shadows / highlights) makes the direct8 kernel store
     * the exposure-adjusted value as uint16 before the camera matrix, like the
     * 16-bit loop, so both routes clamp. Alone, paired, with vibrance, the
     * round-1 repros and the owner's night preset. S/H blur is the kernel's own. */
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    static const RouteCell kCells[] = {
        { "contrast_pos_wb6500",          6500.0,  0.0, true,  0.14, 0.46, 0.0, 0.0, 1.00, 130000.0, nullptr, true, true },
        { "contrast_neg_wb3000",          3000.0,  0.0, true, -0.30, 0.50, 0.0, 0.0, 1.00, 130000.0, nullptr, true, true },
        { "contrast_strong_wb2500_tint30", 2500.0, 30.0, true,  0.60, 0.30, 0.0, 0.0, 1.00, 130000.0, nullptr, true, true },
        { "contrast_sol_r1_pixel_6500K",  6500.0,  0.0, true,  0.14, 0.46, 0.0, 0.0, 1.00, 0.0, kSolR1Leveled, true, true },
        { "contrast_fable_r1_pixel_3000K", 3000.0, 0.0, true, -0.30, 0.50, 0.0, 0.0, 1.00, 0.0, kFableR1Leveled, true, true },
        { "shadows_only_wb3000",          3000.0,  0.0, true,  0.0, 0.5, 0.32, 0.0, 1.00, 130000.0, nullptr, true, true },
        { "highlights_only_wb6500",       6500.0,  0.0, true,  0.0, 0.5, 0.0, -0.26, 1.00, 130000.0, nullptr, true, true },
        { "shadows_highlights_wb3000",    3000.0,  0.0, true,  0.0, 0.5, 0.32, -0.26, 1.00, 130000.0, nullptr, true, true },
        { "contrast_and_vibrance_wb3000", 3000.0,  0.0, true,  0.14, 0.46, 0.0, 0.0, 1.03, 130000.0, nullptr, true, true },
        { "night_preset_wb3000",          3000.0,  0.0, true,  0.14, 0.46, 0.32, -0.26, 1.03, 130000.0, nullptr, true, true },
        { "night_preset_wb6500",          6500.0,  0.0, true,  0.14, 0.46, 0.32, -0.26, 1.03, 130000.0, nullptr, true, true },
    };
    for (const RouteCell & cell : kCells)
    {
        run_route_cell(fixture, cell);
    }
}

/* The thread-local preview state, saved and restored around a test. */
struct ThreadPreviewStateRestore
{
    int mode = processingPlaybackPreviewModeEnabled();
    int aggressive = processingPlaybackAggressivePreviewModeEnabled();
    int scale = processingPlaybackPreviewScaleFactor();
    ~ThreadPreviewStateRestore()
    {
        processingSetPlaybackPreviewScaleFactor(scale);
        processingSetPlaybackAggressivePreviewMode(aggressive);
        processingSetPlaybackPreviewMode(mode);
    }
};

/* The GUI-selected preview resolution (proxy level) and its env kill switch,
 * restored on scope exit. -1 = Auto (the default), 0 = Full, 1 = Half. */
struct ProxyLevelScope
{
    explicit ProxyLevelScope(int level)
        : previousLevel(mlvPlaybackProxyLevel())
        , hadEnv(qEnvironmentVariableIsSet("MLVAPP_DISABLE_HALFRES_X1_PREVIEW"))
        , previousEnv(qgetenv("MLVAPP_DISABLE_HALFRES_X1_PREVIEW"))
    {
        qunsetenv("MLVAPP_DISABLE_HALFRES_X1_PREVIEW");
        mlvSetPlaybackProxyLevel(level);
    }
    ~ProxyLevelScope()
    {
        mlvSetPlaybackProxyLevel(previousLevel);
        if (hadEnv) qputenv("MLVAPP_DISABLE_HALFRES_X1_PREVIEW", previousEnv);
    }
    int previousLevel;
    bool hadEnv;
    QByteArray previousEnv;
};

/* Dual-ISO mode of the fixture clip, restored on scope exit. */
struct DualIsoModeScope
{
    DualIsoModeScope(mlvObject_t * v, int mode) : video(v), previous(v->llrawproc->dual_iso)
    {
        llrpSetDualIsoMode(video, mode);
    }
    ~DualIsoModeScope() { llrpSetDualIsoMode(video, previous); }
    mlvObject_t * video;
    int previous;
};

/* Real clip frame through the whole direct8 render (getMlvProcessedFrame8 picks
 * direct8 for an eligible receipt outside preview mode) against the shader. The
 * tiny clip is dark, so this cross-checks the plumbing (debayer inputs, levels,
 * gamma) rather than over-range behaviour. */
static void assert_real_frame_matches_direct8_render(const char * label,
                                                     double vibrance,
                                                     double contrast)
{
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    configure_gpu_preview_supported_subset(fixture);
    processingObject_t * processing = fixture.processing();
    processingAllowCreativeAdjustments(processing);
    processingSetSimpleContrast(processing, contrast);
    processingSetPivot(processing, 0.46);
    processingSetVibrance(processing, vibrance);

    const std::vector<uint8_t> primed = fixture.renderFrame8(0, /*threads=*/1);
    ASSERT_TRUE(!primed.empty());
    /* fixture.renderFrame8 is the unscaled (export / single-frame) entry: preview
     * mode off, where the cheapness gate always passes, so an eligible receipt
     * IS the direct8 render. (The playback route of this HQ Dual ISO clip is
     * covered by the HostRoute* tests.) */
    ASSERT_TRUE(processingCanUseDirect8BitOutput(processing) != 0);

    QString reason;
    GpuPreviewProcessingConfig config = gpuPreviewProcessingBuildConfig(processing, &reason);
    ASSERT_TRUE(config.enabled);
    gpuPreviewProcessingApplyCpuRoute(&config, processing, /*direct8Route=*/true);
    const std::vector<uint8_t> engine8 = fixture.renderFrame8(0, /*threads=*/1);
    const std::vector<uint16_t> debayered = fixture.renderDebayeredFrame16(0);
    ASSERT_EQ(debayered.size(), engine8.size());
    assert_gpu_display_matches_direct8_engine(label, config, debayered, engine8,
                                              fixture.width(), fixture.height());
}

TEST(GpuPreviewProcessing, Direct8AnchoredRealFrameMatchesDirect8Render)
{
    assert_real_frame_matches_direct8_render("real_frame_direct8_vibrance", 1.03, 0.0);
    assert_real_frame_matches_direct8_render("real_frame_direct8_contrast_vibrance", 1.03, 0.14);
}

/* What the CPU does for one processing state, derived by hand from the engine
 * source (not from the function under test). */
struct RouteStateExpectation
{
    const char * label;
    std::function<void(processingObject_t *)> apply;
    bool direct8Eligible;      /* processing_can_use_direct_8bit_output */
    bool direct8Clamp;         /* kernel: AgX || contrast || S/H */
    bool generic16Clamp;       /* generic loop, minus the basic-matrix branch */
};

/* Back to a direct8-eligible, fully neutral receipt (creative adjustments off,
 * camera matrix on, no LUT / filter / grain / sharpening / AgX). */
static void reset_route_state(MlvPipelineFixture & fixture)
{
    configure_gpu_preview_supported_subset(fixture);
    processingObject_t * processing = fixture.processing();
    processingAllowCreativeAdjustments(processing);
    processingSetSimpleContrast(processing, 0.0);
    processingSetPivot(processing, 0.5);
    processingSetShadows(processing, 0.0);
    processingSetHighlights(processing, 0.0);
    processingSetVibrance(processing, 1.0);
    processingSetSaturation(processing, 1.0);
    processingDontAllowCreativeAdjustments(processing);
    processing->use_cam_matrix = 1;
    processing->lut_on = 0;
    processing->filter_on = 0;
    processing->grainStrength = 0;
    processingSetSharpening(processing, 0.0);
    processingDisableAgX(processing);
}

TEST(GpuPreviewProcessing, CpuRouteSelectionMatchesEnginePredicates)
{
    /* The host (RenderFrameThread) asks the engine for its route through
     * gpuPreviewHostCpuRoute, which also returns the clamp flag of that route.
     * This pins both against the engine's own predicates over many states
     * (saturation, AgX, LUT, sharpen ...) -- only the ROUTE is asserted here, not
     * pixels: the ported stages' pixels are EngineAnchoredCreativeChain*, and the
     * refused ones (LUT, sharpen ...) never reach the display shader
     * (DisplayShaderRefusalPredicateMatchesConfigFlags). */
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);

    const std::vector<RouteStateExpectation> states = {
        { "neutral", [](processingObject_t *) {}, true, false, false },
        { "vibrance", [](processingObject_t * p) { processingAllowCreativeAdjustments(p); processingSetVibrance(p, 1.2); },
          true, false, true },
        { "saturation", [](processingObject_t * p) { processingAllowCreativeAdjustments(p); processingSetSaturation(p, 1.2); },
          true, false, true },
        { "contrast", [](processingObject_t * p) { processingAllowCreativeAdjustments(p); processingSetSimpleContrast(p, 0.2); },
          true, true, true },
        { "shadows_highlights", [](processingObject_t * p) { processingAllowCreativeAdjustments(p); processingSetShadows(p, 0.3); },
          true, true, true },
        { "agx_neutral", [](processingObject_t * p) { processingEnableAgX(p); }, true, true, true },
        { "agx_vibrance", [](processingObject_t * p) { processingEnableAgX(p); processingAllowCreativeAdjustments(p); processingSetVibrance(p, 1.2); },
          true, true, true },
        /* direct8-INELIGIBLE: the CPU runs the generic 16-bit loop */
        { "sharpen_neutral", [](processingObject_t * p) { processingSetSharpening(p, 0.5); }, false, false, false },
        { "sharpen_vibrance", [](processingObject_t * p) { processingAllowCreativeAdjustments(p); processingSetVibrance(p, 1.2); processingSetSharpening(p, 0.5); },
          false, false, true },
        { "lut_on_vibrance", [](processingObject_t * p) { processingAllowCreativeAdjustments(p); processingSetVibrance(p, 1.2); p->lut_on = 1; },
          false, false, true },
        { "filter_on_neutral", [](processingObject_t * p) { p->filter_on = 1; }, false, false, false },
        { "grain_contrast", [](processingObject_t * p) { processingAllowCreativeAdjustments(p); processingSetSimpleContrast(p, 0.2); p->grainStrength = 5; },
          false, true, true },
        { "no_camera_matrix", [](processingObject_t * p) { p->use_cam_matrix = 0; }, false, false, true },
    };

    for (const RouteStateExpectation & state : states)
    {
        processingObject_t * processing = fixture.processing();
        reset_route_state(fixture);
        state.apply(processing);

        const std::string label(state.label);
        const bool direct8 = processingCanUseDirect8BitOutput(processing) != 0;
        if (direct8 != state.direct8Eligible)
        {
            ::minitest::fail(__FILE__, __LINE__, "direct8 eligibility of " + label, "engine disagrees with the table");
        }
        /* The host's route (gpuPreviewHostCpuRoute -- the real call-site code, the
         * thread's preview mode left OFF as on the CUDA path) at preview
         * resolution Full, where the cheapness gate passes: the route is exactly
         * eligibility, and the flag it carries is the engine's for that route.
         * The cheapness REFUSALS are the HostRoute* tests below. */
        {
            ThreadPreviewStateRestore restore;
            processingSetPlaybackPreviewMode(0);
            ProxyLevelScope full(0);
            const GpuPreviewHostCpuRoute hostRoute = gpuPreviewHostCpuRoute(fixture.video(), 1, false);
            ASSERT_EQ(direct8, hostRoute.direct8);
            ASSERT_EQ(processingCpuRoutePreCameraClamps(processing, direct8 ? 1 : 0) != 0, hostRoute.preCameraClamp);
        }

        for (const bool route : { true, false })
        {
            GpuPreviewProcessingConfig config;
            config.enabled = true;
            gpuPreviewProcessingApplyCpuRoute(&config, processing, route);
            const bool expected = route ? state.direct8Clamp : state.generic16Clamp;
            if (config.preCameraClamp != expected)
            {
                ::minitest::fail(__FILE__, __LINE__,
                                 "pre-camera clamp for " + label + (route ? " (direct8 route)" : " (16-bit route)"),
                                 std::string("host flag ") + (config.preCameraClamp ? "1" : "0")
                                     + " but the engine source says " + (expected ? "1" : "0"));
            }
            ASSERT_EQ(config.preCameraClamp, processingCpuRoutePreCameraClamps(processing, route ? 1 : 0) != 0);
        }
    }

    /* The cheapness gate (x1 reduced proxy, dual-ISO outside HQ recon) and the
     * route it picks at every preview resolution are the HostRoute* tests. */
}

/* S/H blur freshness per viewport route (fable r2
 * CUDA-LOOK-SH-BLUR-STALE-OTHER-VIEWPORT-ROUTES-1). GpuDisplayViewport cannot be
 * instantiated headlessly (tests/gui is the only harness that can, and it does
 * not paint these routes), so this pins the SOURCE contract the way
 * test_async_preupload_pipeline pins its call sites: every public presentation
 * route of the viewport either uploads a fresh blur
 * (gpuPreviewProcessingUpdateShadowsHighlightsBlurTexture) or marks the shared
 * LUT set's blur stale (gpuPreviewProcessingMarkShadowsHighlightsBlurStale),
 * and the set of routes is enumerated so a new one must make the same choice. */
static std::string cpp_body_without_comments_and_strings(const std::string & text)
{
    std::string out;
    out.reserve(text.size());
    size_t index = 0;
    while (index < text.size())
    {
        const char c = text[index];
        if (c == '/' && index + 1 < text.size() && text[index + 1] == '/')
        {
            while (index < text.size() && text[index] != '\n') ++index;
        }
        else if (c == '/' && index + 1 < text.size() && text[index + 1] == '*')
        {
            index += 2;
            while (index + 1 < text.size() && !(text[index] == '*' && text[index + 1] == '/')) ++index;
            index += 2;
        }
        else if (c == '"')
        {
            ++index;
            while (index < text.size() && text[index] != '"')
            {
                if (text[index] == '\\') ++index;
                ++index;
            }
            ++index;
            out.push_back(' ');
        }
        else
        {
            out.push_back(c);
            ++index;
        }
    }
    return out;
}

/* name -> body of every `GpuDisplayViewport::setPresented*` definition. */
static std::vector<std::pair<std::string, std::string>> viewport_present_route_bodies(const std::string & source)
{
    std::vector<std::pair<std::string, std::string>> routes;
    const std::string marker = "GpuDisplayViewport::setPresented";
    size_t from = 0;
    while (true)
    {
        const size_t at = source.find(marker, from);
        if (at == std::string::npos) break;
        from = at + marker.size();
        const size_t nameEnd = source.find('(', at);
        if (nameEnd == std::string::npos) break;
        const std::string name = source.substr(at + 20, nameEnd - (at + 20)); /* skip "GpuDisplayViewport::" */
        int depth = 1;
        size_t cursor = nameEnd + 1;
        while (cursor < source.size() && depth > 0)
        {
            if (source[cursor] == '(') ++depth;
            else if (source[cursor] == ')') --depth;
            ++cursor;
        }
        while (cursor < source.size() && source[cursor] != '{' && source[cursor] != ';') ++cursor;
        if (cursor >= source.size() || source[cursor] == ';') continue; /* declaration only */
        int braces = 0;
        size_t end = cursor;
        for (; end < source.size(); ++end)
        {
            if (source[end] == '{') ++braces;
            else if (source[end] == '}' && --braces == 0) break;
        }
        routes.emplace_back(name, source.substr(cursor, end - cursor + 1));
        from = end;
    }
    return routes;
}

/* True when a route body (starting at its '{', comments and strings blanked)
 * uploads or invalidates the S/H blur ON THE MAIN PATH (fable r1
 * CUDA-LOOK-VIEWPORT-ROUTE-CONTRACT-PLACEMENT-1): at the function's top brace
 * level (not inside an if / else / loop / lambda block), as a full statement
 * (not the body of a brace-less if), and not after an unconditional top-level
 * `return`. Conditional early-outs (a refused present) sit inside `if` blocks,
 * so they do not count as a reason to move the call. */
static bool blur_call_is_on_main_path(const std::string & body)
{
    static const std::string kCalls[] = { "gpuPreviewProcessingUpdateShadowsHighlightsBlurTexture(",
                                          "gpuPreviewProcessingMarkShadowsHighlightsBlurStale(" };
    auto isWordChar = [](char c) { return std::isalnum(static_cast<unsigned char>(c)) || c == '_'; };
    int depth = 0;
    bool unreachable = false;
    char previous = '{';   /* previous non-space character */
    for (size_t at = 0; at < body.size(); ++at)
    {
        const char c = body[at];
        if (std::isspace(static_cast<unsigned char>(c))) continue;
        const bool wordStart = isWordChar(c) && (at == 0 || !isWordChar(body[at - 1]));
        if (depth == 1 && wordStart)
        {
            const bool statementStart = previous == ';' || previous == '{' || previous == '}';
            if (statementStart && body.compare(at, 6, "return") == 0 && !isWordChar(body[at + 6]))
            {
                unreachable = true;
            }
            for (const std::string & call : kCalls)
            {
                if (body.compare(at, call.size(), call) == 0 && statementStart && !unreachable) return true;
            }
        }
        if (c == '{') ++depth;
        else if (c == '}') --depth;
        previous = c;
    }
    return false;
}

TEST(GpuPreviewProcessing, ViewportRouteContractCheckerAcceptsOnlyTheMainPath)
{
    /* The checker's own mutation test: each placement fable r1 named as slipping
     * past a substring search must now be rejected. */
    const std::string stale = "gpuPreviewProcessingMarkShadowsHighlightsBlurStale(&m_lutSet);";
    const std::string upload = "gpuPreviewProcessingUpdateShadowsHighlightsBlurTexture(m_lutSet, o, w, h);";
    ASSERT_TRUE(blur_call_is_on_main_path("{ a(); " + stale + " update(); }"));
    ASSERT_TRUE(blur_call_is_on_main_path("{ a(); " + upload + " return true; }"));
    ASSERT_TRUE(blur_call_is_on_main_path("{ if (!p) { return false; } " + stale + " return true; }"));
    ASSERT_TRUE(!blur_call_is_on_main_path("{ a(); update(); }"));
    ASSERT_TRUE(!blur_call_is_on_main_path("{ if (false) { " + stale + " } }"));
    ASSERT_TRUE(!blur_call_is_on_main_path("{ if (x) { " + upload + " } }"));
    ASSERT_TRUE(!blur_call_is_on_main_path("{ if (x) " + stale + " }"));
    ASSERT_TRUE(!blur_call_is_on_main_path("{ if (x) { a(); } else " + stale + " }"));
    ASSERT_TRUE(!blur_call_is_on_main_path("{ return; " + stale + " }"));
    ASSERT_TRUE(!blur_call_is_on_main_path("{ auto f = [&]() { " + stale + " }; f(); }"));
    ASSERT_TRUE(!blur_call_is_on_main_path("{ for (;;) { " + stale + " break; } }"));
}

TEST(GpuPreviewProcessing, ViewportPresentRoutesNeverBindAStaleShadowsHighlightsBlur)
{
    QString path = qEnvironmentVariable("MLVAPP_TEST_VIEWPORT_SOURCE"); /* red-first override */
    if (path.isEmpty()) path = QStringLiteral("platform/qt/GpuDisplayViewport.cpp");
    QFile file(path);
    ASSERT_TRUE(file.open(QIODevice::ReadOnly));
    const std::string source = cpp_body_without_comments_and_strings(file.readAll().toStdString());

    const auto routes = viewport_present_route_bodies(source);
    std::vector<std::string> names;
    std::string stale;
    for (const auto & route : routes)
    {
        names.push_back(route.first);
        if (!blur_call_is_on_main_path(route.second)) stale += " " + route.first;
    }
    for (const char * expected : { "setPresentedImage", "setPresentedRgb16", "setPresentedBayer16",
                                   "setPresentedGpuPlaybackReconTexture",
                                   "setPresentedGpuPlaybackReconAmazePostWbTexture",
                                   "setPresentedAmazePostWbTexture" })
    {
        if (std::find(names.begin(), names.end(), std::string(expected)) == names.end())
        {
            ::minitest::fail(__FILE__, __LINE__, std::string("viewport route enumerated: ") + expected,
                             "not found; update this test together with the route");
        }
    }
    if (!stale.empty())
    {
        ::minitest::fail(__FILE__, __LINE__,
                         "every viewport present route uploads or invalidates the S/H blur on its main path",
                         "routes without a top-level, reachable upload/invalidate statement:" + stale);
    }
    /* the one route that uploads must NOT also be marked stale afterwards */
    for (const auto & route : routes)
    {
        if (route.first == "setPresentedGpuPlaybackReconAmazePostWbTexture")
        {
            ASSERT_TRUE(route.second.find("gpuPreviewProcessingMarkShadowsHighlightsBlurStale(") == std::string::npos);
        }
    }
}

/* ---- CALL-SITE route tests (CUDA-PLAYBACK-LOOK-PARITY-2 round 2) ----
 *
 * The class these close: "the route the CUDA host hands the shader equals the
 * route the CPU engine actually takes for the SAME clip, scale, settings AND
 * playback/preview state". They drive gpuPreviewHostCpuRoute -- the header-only
 * helper RenderFrameThread::drawFrame calls, nothing else -- with the thread's
 * playback-preview state left the way the CUDA path leaves it (OFF: the frame is
 * OutputDebayered16, where PlaybackPreviewModeGuard is not enabled), never by
 * setting the preview mode by hand the way the predicate test above does. */

/* One host answer, asserted against a hand-derived engine expectation, in the
 * CUDA thread state (preview OFF) AND in the playback envelope the CPU render
 * thread uses (preview ON): the answer must be the same and the thread's state
 * must come back untouched. */
static void assert_host_route(MlvPipelineFixture & fixture,
                              const char * label,
                              int scale,
                              bool expectDirect8,
                              bool expectPreCameraClamp,
                              bool phase3Raw = false)
{
    ThreadPreviewStateRestore restore;
    for (const int threadPreviewMode : { 0, 1 })
    {
        processingSetPlaybackPreviewMode(threadPreviewMode);
        processingSetPlaybackAggressivePreviewMode(0);
        processingSetPlaybackPreviewScaleFactor(scale);
        const GpuPreviewHostCpuRoute route = gpuPreviewHostCpuRoute(fixture.video(), scale, phase3Raw);
        std::cout << "[HOST-ROUTE] " << label << " scale=" << scale << " thread_preview=" << threadPreviewMode
                  << " direct8=" << route.direct8 << " preCameraClamp=" << route.preCameraClamp << "\n";
        if (route.direct8 != expectDirect8 || route.preCameraClamp != expectPreCameraClamp)
        {
            ::minitest::fail(__FILE__, __LINE__,
                             std::string("host route for ") + label + " at x" + std::to_string(scale)
                                 + " (thread preview mode " + std::to_string(threadPreviewMode) + ")",
                             std::string("host says direct8=") + (route.direct8 ? "1" : "0")
                                 + " clamp=" + (route.preCameraClamp ? "1" : "0")
                                 + "; the engine takes direct8=" + (expectDirect8 ? "1" : "0")
                                 + " clamp=" + (expectPreCameraClamp ? "1" : "0"));
        }
        ASSERT_EQ(threadPreviewMode, processingPlaybackPreviewModeEnabled());
        ASSERT_EQ(0, processingPlaybackAggressivePreviewModeEnabled());
        ASSERT_EQ(scale, processingPlaybackPreviewScaleFactor());
    }
}

/* Vibrance-only on a direct8-eligible receipt: direct8 does not clamp before the
 * camera matrix, the generic 16-bit loop does. */
static void set_vibrance_only_receipt(MlvPipelineFixture & fixture)
{
    reset_route_state(fixture);
    processingAllowCreativeAdjustments(fixture.processing());
    processingSetVibrance(fixture.processing(), 1.03);
    ASSERT_TRUE(processingCanUseDirect8BitOutput(fixture.processing()) != 0);
}

TEST(GpuPreviewProcessing, HostRouteFollowsTheEngineEnvelopeForHqDualIsoAtX1)
{
    /* fable r1 BLOCKER. The fixture clip is HQ Dual ISO. In the playback envelope
     * the engine REFUSES direct8 at x1 while the reduced x1 proxy would engage
     * (preview resolution Auto or Half) and takes it at Full; the host must
     * report the engine's route, with the thread in the state the CUDA path
     * leaves it. These are the cheapness-REFUSAL fixtures (sol H1): an eligible
     * receipt whose route is the 16-bit loop only because of the cheapness gate,
     * so dropping that term fails here. */
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    set_vibrance_only_receipt(fixture);
    ASSERT_TRUE(llrpHQDualIso(fixture.video()) != 0);

    for (const int level : { -1, 1 })   /* Auto (the default) and Half */
    {
        ProxyLevelScope proxy(level);
        assert_host_route(fixture, level < 0 ? "hq_dual_iso_vibrance_preview_auto" : "hq_dual_iso_vibrance_preview_half",
                          1, /*direct8=*/false, /*clamp=*/true);
    }
    {
        ProxyLevelScope proxy(0);   /* Full: no proxy, direct8 keeps its priority */
        assert_host_route(fixture, "hq_dual_iso_vibrance_preview_full", 1, /*direct8=*/true, /*clamp=*/false);
    }
    {
        ProxyLevelScope proxy(-1);  /* the x2 / x4 proxies are not what the gate refuses */
        assert_host_route(fixture, "hq_dual_iso_vibrance_scaled", 2, /*direct8=*/true, /*clamp=*/false);
        assert_host_route(fixture, "hq_dual_iso_vibrance_scaled", 4, /*direct8=*/true, /*clamp=*/false);
    }
}

TEST(GpuPreviewProcessing, HostRouteRefusesDirect8ForDualIsoOutsideHqRecon)
{
    /* The second cheapness clause: Dual ISO clips outside HQ recon (preview
     * processing, dual_iso == 2) are refused at every scale, even at preview
     * resolution Full and for a receipt direct8 could otherwise run. */
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    set_vibrance_only_receipt(fixture);
    ASSERT_TRUE(fixture.video()->llrawproc->diso_validity != 0);

    ProxyLevelScope proxy(0);
    for (const int scale : { 1, 2, 4 })
    {
        assert_host_route(fixture, "hq_dual_iso_baseline", scale, /*direct8=*/true, /*clamp=*/false);
    }
    DualIsoModeScope previewRecon(fixture.video(), 2);
    ASSERT_TRUE(llrpHQDualIso(fixture.video()) == 0);
    for (const int scale : { 1, 2, 4 })
    {
        assert_host_route(fixture, "dual_iso_outside_hq_recon", scale, /*direct8=*/false, /*clamp=*/true);
    }
    /* ... but a frame the CPU renders from Phase 3 raw goes through the raw
     * entries, which check eligibility alone and only at scale > 1: direct8 at
     * x2 / x4 despite the refusal, and the dispatch (refusal) at x1. */
    assert_host_route(fixture, "dual_iso_outside_hq_recon_phase3_raw", 1, /*direct8=*/false, /*clamp=*/true, /*phase3Raw=*/true);
    for (const int scale : { 2, 4 })
    {
        assert_host_route(fixture, "dual_iso_outside_hq_recon_phase3_raw", scale,
                          /*direct8=*/true, /*clamp=*/false, /*phase3Raw=*/true);
    }
}

TEST(GpuPreviewProcessing, HostRouteForPhase3RawFramesFollowsTheRawEntriesNotTheDispatch)
{
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    set_vibrance_only_receipt(fixture);
    ProxyLevelScope proxy(-1);
    /* x1: the raw entries return 0 at scale 1 and the frame goes to the
     * dispatch, whose cheapness gate refuses direct8 for HQ Dual ISO at Auto */
    assert_host_route(fixture, "hq_dual_iso_phase3_raw", 1, /*direct8=*/false, /*clamp=*/true, /*phase3Raw=*/true);
    /* x2 / x4: eligibility alone */
    for (const int scale : { 2, 4 })
    {
        assert_host_route(fixture, "hq_dual_iso_phase3_raw", scale, /*direct8=*/true, /*clamp=*/false, /*phase3Raw=*/true);
    }
    /* an ineligible receipt is never direct8, raw entry or not (vibrance is still
     * on, so the generic loop clamps) */
    processingSetSharpening(fixture.processing(), 0.5);
    for (const int scale : { 1, 2, 4 })
    {
        assert_host_route(fixture, "sharpen_vibrance_phase3_raw", scale, /*direct8=*/false, /*clamp=*/true, /*phase3Raw=*/true);
    }
}

TEST(GpuPreviewProcessing, HostRouteForReceiptsDirect8CannotRunIsAlwaysTheSixteenBitLoop)
{
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    ProxyLevelScope proxy(0);

    reset_route_state(fixture);
    processingSetSharpening(fixture.processing(), 0.5);
    for (const int scale : { 1, 2, 4 })
    {
        assert_host_route(fixture, "sharpen_neutral", scale, /*direct8=*/false, /*clamp=*/false); /* basic-matrix branch */
    }
    reset_route_state(fixture);
    processingAllowCreativeAdjustments(fixture.processing());
    processingSetVibrance(fixture.processing(), 1.2);
    fixture.processing()->use_cam_matrix = 0;
    for (const int scale : { 1, 2, 4 })
    {
        assert_host_route(fixture, "no_camera_matrix_vibrance", scale, /*direct8=*/false, /*clamp=*/true);
    }
    /* contrast: both routes clamp, so only the route differs */
    reset_route_state(fixture);
    processingAllowCreativeAdjustments(fixture.processing());
    processingSetSimpleContrast(fixture.processing(), 0.2);
    assert_host_route(fixture, "contrast_full_res", 1, /*direct8=*/true, /*clamp=*/true);
    {
        ProxyLevelScope previewAuto(-1);
        assert_host_route(fixture, "contrast_preview_auto", 1, /*direct8=*/false, /*clamp=*/true);
    }
}

TEST(GpuPreviewProcessing, HostRouteAndTelemetryComeFromTheSingleEngineCallSite)
{
    /* Every place in the Qt host that computes the route or the clamp flag must
     * go through gpuPreviewHostCpuRoute (which asks the engine inside the
     * playback-preview envelope). A second, local evaluation is exactly how the
     * fable r1 blocker arose: the predicate ran with the render thread's own
     * (CUDA: off) preview state. Pinned at the source, enumerating the host. */
    const QStringList sources = { "platform/qt/RenderFrameThread.cpp", "platform/qt/MainWindow.cpp",
                                  "platform/qt/GpuDisplayViewport.cpp", "platform/qt/GpuDisplayWindow.cpp",
                                  "platform/qt/GpuPreviewProcessing.cpp", "platform/qt/GpuPreviewHostRoute.h" };
    std::string offenders;
    int helperCalls = 0;
    for (const QString & path : sources)
    {
        QFile file(path);
        ASSERT_TRUE(file.open(QIODevice::ReadOnly));
        const std::string body = cpp_body_without_comments_and_strings(file.readAll().toStdString());
        const bool isHelper = path.endsWith("GpuPreviewHostRoute.h");
        const bool isProcessing = path.endsWith("GpuPreviewProcessing.cpp");
        for (const char * forbidden : { "mlvPreviewPlaybackCpuRoute(",
                                        "processingCpuRoutePreCameraClamps(", "gpuPreviewProcessingApplyCpuRoute(" })
        {
            if (body.find(forbidden) == std::string::npos) continue;
            const std::string name(forbidden);
            const bool allowed = (isHelper && name == "mlvPreviewPlaybackCpuRoute(")
                || (isProcessing && (name == "processingCpuRoutePreCameraClamps(" || name == "gpuPreviewProcessingApplyCpuRoute("));
            if (!allowed) offenders += " " + path.toStdString() + ":" + name;
        }
        if (!isHelper)
        {
            for (size_t at = body.find("gpuPreviewHostCpuRoute("); at != std::string::npos;
                 at = body.find("gpuPreviewHostCpuRoute(", at + 1))
            {
                ++helperCalls;
            }
        }
    }
    if (!offenders.empty())
    {
        ::minitest::fail(__FILE__, __LINE__, "the Qt host evaluates the CPU route only through gpuPreviewHostCpuRoute",
                         "direct evaluations outside the helper:" + offenders);
    }
    ASSERT_EQ(1, helperCalls);   /* the one call site: RenderFrameThread::drawFrame */

    /* the telemetry keys read the engine's route (strings are blanked above) */
    QFile raw(QStringLiteral("platform/qt/RenderFrameThread.cpp"));
    ASSERT_TRUE(raw.open(QIODevice::ReadOnly));
    const QString text = QString::fromUtf8(raw.readAll());
    const int directAt = text.indexOf(QStringLiteral("QStringLiteral(\"gpu_preview_processing_cpu_route_direct8\")"));
    ASSERT_TRUE(directAt > 0);
    ASSERT_TRUE(text.mid(directAt, 160).contains(QStringLiteral("cpuRoute.direct8")));
    /* the call site tells the engine whether the frame consumed Phase 3 raw */
    const int callAt = text.indexOf(QStringLiteral("gpuPreviewHostCpuRoute("));
    ASSERT_TRUE(callAt > 0);
    ASSERT_TRUE(text.mid(callAt, 160).contains(QStringLiteral("decodedRawFrame != nullptr")));
    const int clampAt = text.indexOf(QStringLiteral("QStringLiteral(\"gpu_preview_processing_pre_camera_clamp\")"));
    ASSERT_TRUE(clampAt > 0);
    ASSERT_TRUE(text.mid(clampAt, 160).contains(QStringLiteral("cpuRoute.preCameraClamp")));
}

/* ---- 16-bit-route and ineligible-state PIXEL cells (sol H1 / fable H2) ----
 *
 * Route chosen by the HOST helper, flag applied by the host helper, frame
 * rendered by the display shader and by the engine route that helper named:
 * direct8 -> applyProcessingObject8, otherwise apply_processing_object (the
 * generic 16-bit loop). Both compared in 8-bit codes at the direct8 budget
 * (tolerance 1 / max 3 / 0.1%). PLAYBACK-SEEK-RENDER-PARITY-1 ported saturation
 * (and the rest of the post-gamma creative chain) into the display shader, so the
 * saturation cells now compare against the FULL engine. */
struct HostPixelCell
{
    const char * label;
    double kelvin;
    double tint;
    double vibrance;       /* 1.0 = off */
    double saturation;     /* 1.0 = off */
    bool useCameraMatrix;
    const int * flatLeveled; /* non-null: a flat leveled frame instead of the ramp */
    double maxDiagonal;
    int proxyLevel;        /* -1 Auto, 0 Full */
    bool expectDirect8;
    bool expectPreCameraClamp;
    /* Saturation cell: on an in-range pixel (the clamp cannot hide anything) the
     * engine output with saturation must differ from the one without, so the
     * cell provably exercises the stage the display shader now applies. */
    bool saturationCell = false;
};

static const int kInRangeLeveled[3] = { 20000, 30000, 25000 };   /* no WB over-range at 6500 K */

static void run_host_pixel_cell(MlvPipelineFixture & fixture, const HostPixelCell & cell)
{
    reset_route_state(fixture);
    processingObject_t * processing = fixture.processing();
    processingAllowCreativeAdjustments(processing);
    processingSetWhiteBalance(processing, cell.kelvin, cell.tint);
    processingSetVibrance(processing, cell.vibrance);
    processingSetSaturation(processing, cell.saturation);
    processing->use_cam_matrix = cell.useCameraMatrix ? 1 : 0;
    (void)fixture.renderDebayeredFrame16(0);

    QString reason;
    ASSERT_TRUE(gpuPreviewProcessingIsSupported(processing, &reason));
    GpuPreviewProcessingConfig config = gpuPreviewProcessingBuildConfig(processing, &reason);
    ASSERT_TRUE(config.enabled);
    ASSERT_TRUE(config.useCameraMatrix == cell.useCameraMatrix);

    ThreadPreviewStateRestore restore;
    processingSetPlaybackPreviewMode(0);   /* the CUDA render thread's state */
    ProxyLevelScope proxy(cell.proxyLevel);
    const GpuPreviewHostCpuRoute route = gpuPreviewHostCpuRoute(fixture.video(), 1, false);
    gpuPreviewHostApplyCpuRoute(&config, route);
    std::cout << "[HOST-PIXEL] " << cell.label << " host direct8=" << route.direct8
              << " preCameraClamp=" << route.preCameraClamp << "\n";
    ASSERT_EQ(cell.expectDirect8, route.direct8);
    ASSERT_EQ(cell.expectPreCameraClamp, route.preCameraClamp);

    int width = 0;
    int height = 0;
    const std::vector<uint16_t> frame = cell.flatLeveled
        ? make_flat_leveled_frame(processing, cell.flatLeveled, &width, &height)
        : make_synthetic_ramp_frame(processing, cell.maxDiagonal, &width, &height);

    auto engineOnRoute = [&]() {
        return route.direct8
            ? run_direct8_engine_on_frame(processing, frame, width, height)
            : engine16_as_8bit(run_production_engine_on_frame(processing, frame, width, height));
    };
    const std::vector<uint8_t> engine8 = engineOnRoute();
    if (cell.saturationCell)
    {
        ASSERT_EQ(size_t(0), count_wb_overrange_samples(processing, frame)); /* the clamp cannot hide the stage */
        processingSetSaturation(processing, 1.0);
        const std::vector<uint8_t> withoutSaturation = engineOnRoute();
        processingSetSaturation(processing, cell.saturation);
        ASSERT_TRUE(withoutSaturation != engine8);
    }
    const std::string label = std::string("host_") + cell.label + (route.direct8 ? ".direct8" : ".as16bit");
    assert_gpu_display_matches_direct8_engine(label.c_str(), config, frame, engine8, width, height);
}

TEST(GpuPreviewProcessing, HostRoutedSixteenBitPixelCellsMatchTheEngineWithinOneCode)
{
    /* The 16-bit route is what CUDA playback of an HQ Dual ISO clip at preview
     * resolution Auto runs on the CPU. fable r1 repro: leveled [27000,61000,
     * 60000], vibrance 1.03, contrast 0, S/H 0, WB 6500 -- the generic loop
     * clamps before the camera matrix (the round-1 shader did not on this route
     * and was 6 codes off). */
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    static const HostPixelCell kCells[] = {
        /* label                          K      tint  vib   sat   cam    flat            ramp      proxy direct8 clamp */
        { "fable_r1_vibrance_pixel_6500K", 6500.0,  0.0, 1.03, 1.00, true,  kSolR2LeveledB, 0.0,      -1, false, true },
        { "sol_r2_pixel_a_vibrance_6500K", 6500.0,  0.0, 1.03, 1.00, true,  kSolR2LeveledA, 0.0,      -1, false, true },
        { "vibrance_ramp_wb6500",          6500.0,  0.0, 1.03, 1.00, true,  nullptr,        130000.0, -1, false, true },
        { "vibrance_big_ramp_wb2500",      2500.0, 30.0, 1.30, 1.00, true,  nullptr,        130000.0, -1, false, true },
        /* saturation-only (see HostPixelCell::saturationCell) */
        { "saturation_only_inrange_up",    6500.0,  0.0, 1.00, 1.25, true,  kInRangeLeveled, 0.0,     -1, false, true,  true },
        { "saturation_only_inrange_down",  6500.0,  0.0, 1.00, 0.70, true,  kInRangeLeveled, 0.0,     -1, false, true,  true },
        { "no_camera_matrix_vibrance",     6500.0,  0.0, 1.20, 1.00, false, nullptr,        130000.0, -1, false, true },
        { "no_camera_matrix_vibrance_px",  6500.0,  0.0, 1.20, 1.00, false, kSolR2LeveledB, 0.0,      -1, false, true },
        /* neutral: the 16-bit loop's basic-matrix branch does not clamp */
        { "neutral_ramp_wb6500_16bit",     6500.0,  0.0, 1.00, 1.00, true,  nullptr,        130000.0, -1, false, false },
        /* the same states at preview resolution Full are the direct8 route */
        { "fable_r1_vibrance_pixel_full",  6500.0,  0.0, 1.03, 1.00, true,  kSolR2LeveledB, 0.0,       0, true,  false },
        { "saturation_only_inrange_full",  6500.0,  0.0, 1.00, 1.25, true,  kInRangeLeveled, 0.0,      0, true,  false, true },
    };
    for (const HostPixelCell & cell : kCells)
    {
        run_host_pixel_cell(fixture, cell);
    }
}

TEST(GpuPreviewProcessing, LutTextureCacheKeyIncludesTheRawLutsWithoutMovingTheSignature)
{
    /* fable r1 CUDA-LOOK-RAW-LUT-SIGNATURE-UNCLAMPED-ROUTE-1. For a neutral
     * receipt `signature` does not hash the unclamped (raw) diagonal LUTs (that
     * would move the pinned golden signatures), so two configs whose clamped
     * LUTs agree and raw LUTs differ share a signature. The texture cache must
     * still tell them apart, because the unclamped route feeds the raw LUT
     * straight into the camera matrix. */
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    reset_route_state(fixture);
    QString reason;
    const GpuPreviewProcessingConfig base = gpuPreviewProcessingBuildConfig(fixture.processing(), &reason);
    ASSERT_TRUE(base.enabled);
    ASSERT_TRUE(!base.applyInLoopContrast && !base.applyShadowsHighlights);
    ASSERT_TRUE(base.rawLutSignature != 0);
    ASSERT_EQ(base.rawLutSignature, gpuPreviewProcessingRawLutSignature(base));

    GpuPreviewProcessingConfig other = base;
    ASSERT_TRUE(other.matrixLutRawG.size() > 64);
    other.matrixLutRawG.data()[64] ^= 0x01;   /* a raw LUT the clamped LUTs do not see */
    other.rawLutSignature = gpuPreviewProcessingRawLutSignature(other);
    ASSERT_EQ(base.signature, other.signature);                   /* the old key cannot tell them apart ... */
    ASSERT_TRUE(base.rawLutSignature != other.rawLutSignature);   /* ... the new one can */

    GpuPreviewProcessingLutTextureSet set;
    set.signatureValid = true;
    set.signature = base.signature;
    set.rawLutSignature = base.rawLutSignature;
    ASSERT_TRUE(gpuPreviewProcessingLutTextureSetKeyMatches(set, base));
    ASSERT_TRUE(!gpuPreviewProcessingLutTextureSetKeyMatches(set, other));
    set.signatureValid = false;
    ASSERT_TRUE(!gpuPreviewProcessingLutTextureSetKeyMatches(set, base));

    /* a real raw-LUT change reaches the key: a degenerate (negative-gain) tint */
    processingSetWhiteBalance(fixture.processing(), 10000.0, -30.0);
    (void)fixture.renderDebayeredFrame16(0);
    const GpuPreviewProcessingConfig tinted = gpuPreviewProcessingBuildConfig(fixture.processing(), &reason);
    ASSERT_TRUE(tinted.enabled);
    ASSERT_TRUE(tinted.rawLutSignature != base.rawLutSignature);
}

TEST(GpuPreviewProcessing, MarkShadowsHighlightsBlurStaleClearsOnlyTheReadyFlag)
{
    GpuPreviewProcessingLutTextureSet set;
    set.shadowsHighlightsBlurReady = true;
    set.shadowsHighlightsBlurWidth = 8;
    set.shadowsHighlightsBlurHeight = 6;
    gpuPreviewProcessingMarkShadowsHighlightsBlurStale(&set);
    ASSERT_TRUE(!set.shadowsHighlightsBlurReady);
    ASSERT_EQ(8, set.shadowsHighlightsBlurWidth);
    gpuPreviewProcessingMarkShadowsHighlightsBlurStale(nullptr); /* tolerated */
}

TEST(GpuPreviewProcessing, ShadowsHighlightsKillSwitchForcesDisplayBypass)
{
    /* Round 1 lost the MLVAPP_GPU_TEX_NR_DISPLAY_LUT_ONLY_SKIP_SH_STATE kill
     * switch: the bypass was ANDed with "the display shader cannot apply S/H",
     * which became false the moment the shader learned to. The decision is now
     * one function shared by RenderFrameThread and this test. */
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    const GpuPreviewProcessingConfig config = build_shadows_highlights_config_with_frame_state(fixture);
    ASSERT_TRUE(config.applyShadowsHighlights);

    /* default (variable unset = null QString): the display shader applies S/H */
    ASSERT_TRUE(!gpuPreviewProcessingDisplayShadowsHighlightsFrameStateBypassed(config, QString()));
    /* "0" never bypasses */
    ASSERT_TRUE(!gpuPreviewProcessingDisplayShadowsHighlightsFrameStateBypassed(config, QStringLiteral("0")));
    /* any other value is the kill switch */
    ASSERT_TRUE(gpuPreviewProcessingDisplayShadowsHighlightsFrameStateBypassed(config, QStringLiteral("1")));
    ASSERT_TRUE(gpuPreviewProcessingDisplayShadowsHighlightsFrameStateBypassed(config, QStringLiteral("yes")));
    ASSERT_TRUE(gpuPreviewProcessingDisplayShadowsHighlightsFrameStateBypassed(config, QStringLiteral("")));

    /* nothing to bypass when S/H is not requested or the config is disabled */
    GpuPreviewProcessingConfig noShadowsHighlights = config;
    noShadowsHighlights.applyShadowsHighlights = false;
    ASSERT_TRUE(!gpuPreviewProcessingDisplayShadowsHighlightsFrameStateBypassed(
        noShadowsHighlights, QStringLiteral("1")));
    GpuPreviewProcessingConfig disabled = config;
    disabled.enabled = false;
    ASSERT_TRUE(!gpuPreviewProcessingDisplayShadowsHighlightsFrameStateBypassed(
        disabled, QStringLiteral("1")));
}

TEST(GpuPreviewProcessing, ShadowsHighlightsKillSwitchBypassRestoresPreFixLook)
{
    /* What the bypass does on screen: RenderFrameThread skips attaching the S/H
     * frame state, the blur texture is never ready, and the display shader
     * leaves S/H out -- the pre-fix look, which is the A/B baseline. Without
     * the kill switch the attached frame state makes S/H visibly change the
     * frame. */
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    configure_engine_sweep_case(fixture, { "kill_switch", 6500.0, 0.0, 0.0, 0.5, 1.0, 60000.0 });
    processingObject_t * processing = fixture.processing();
    processingSetShadows(processing, 0.60);
    processingSetHighlights(processing, -0.60);
    (void)fixture.renderDebayeredFrame16(0);

    QString reason;
    GpuPreviewProcessingConfig withState = gpuPreviewProcessingBuildConfig(processing, &reason);
    ASSERT_TRUE(withState.enabled);
    ASSERT_TRUE(withState.applyShadowsHighlights);
    const GpuPreviewProcessingConfig bypassed = withState;   /* frame state never attached */
    int width = 0;
    int height = 0;
    const std::vector<uint16_t> frame = make_synthetic_ramp_frame(processing, 60000.0, &width, &height);
    std::vector<uint16_t> refresh_input = frame;
    ASSERT_TRUE(processingRefreshShadowsHighlightsBlurFromRgb16(
                    processing, refresh_input.data(), width, height, 1, 1) != 0);
    ASSERT_TRUE(gpuPreviewProcessingAttachFrameState(&withState, processing, width, height, &reason));
    GpuPreviewProcessingConfig shadowsHighlightsOff = bypassed;
    shadowsHighlightsOff.applyShadowsHighlights = false;

    auto render = [&](const GpuPreviewProcessingConfig & config) {
        std::vector<uint16_t> out(frame.size(), 0);
        (void)render_display_for_parity(config, frame.data(), out.data(), width, height);
        return out;
    };
    const std::vector<uint16_t> applied = render(withState);
    const std::vector<uint16_t> bypassedOut = render(bypassed);
    const std::vector<uint16_t> baseline = render(shadowsHighlightsOff);
    ASSERT_TRUE(bypassedOut == baseline);
    ASSERT_TRUE(applied != baseline);
}

TEST(GpuPreviewProcessing, DisplayShaderFastPathFrameCostInformational)
{
    /* INFORMATIONAL, not a budget: this test records numbers and asserts only
     * the invariants of the measurement itself (every iteration was timed, the
     * per-iteration PAIRED cost is never below either component, and the paired
     * percentiles are ordered). It deliberately does NOT assert the 40 ms frame
     * budget: the numbers come from a shared CI host and software GL, which
     * cannot prove or refute steady-state CUDA cadence. The hardware A/B
     * (shadows/highlights on versus the MLVAPP_GPU_TEX_NR_DISPLAY_LUT_ONLY_SKIP_SH_STATE
     * kill switch, quiet host, real GPU, play-through cadence) is the follow-up
     * card CUDA-LOOK-SH-COST-HARDWARE-MEASURE-1, which needs a venue.
     *
     * Round-2 item 3: measure, don't guess, the per-frame cost the fast S/H
     * frame-state path now actually pays on the live CUDA texture-present
     * path now that gpuTexNrDisplayLutOnlyShStateBypass no longer refuses it.
     * Debayer + blur refresh are CPU-only production functions
     * (debayerBasicU16, processingRefreshShadowsHighlightsBlurFromRgb16) run
     * at this fixture's real 1808x2268 size, so these numbers are directly
     * comparable to steady-state production cost on THIS host regardless of
     * GPU. Measured at TWO thread counts: this test binary's own forced
     * single-threaded mode (test_runtime::force_single_threaded_pipeline(),
     * installed in main() for determinism -- mlvappEffectivePlaybackWorkerThread
     * Count() reports 1 under it, which is a worst-case bound, not what real
     * playback uses) and this host's hardware_concurrency(), which
     * RenderFrameThread.cpp:4027's mlvappEffectivePlaybackWorkerThreadCount()
     * would actually pick outside this test binary (both functions take
     * `threads` as an explicit parameter, so passing either bypasses the
     * process-wide single-thread lock safely -- no global state is touched).
     * The blur-texture-upload and display-shader-draw numbers below are
     * DIFFERENT in kind: they go through the public
     * gpuPreviewProcessingApplyDisplayGpuOffscreen() entry point, which (unlike
     * the live GpuDisplayWindow/GpuDisplayViewport presenters) creates a fresh
     * GL context, program and LUT-texture set on every call instead of reusing
     * signature-cached ones -- there is no lighter-weight public surface to
     * benchmark against, so this is reported as an upper bound, not a
     * steady-state estimate, and it runs through llvmpipe (software) on this
     * box, not real hardware. */
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    const GpuPreviewProcessingConfig config =
        build_shadows_highlights_config_with_frame_state(fixture);
    ASSERT_TRUE(config.applyShadowsHighlights);

    const int w = fixture.width();
    const int h = fixture.height();
    std::vector<uint16_t> rawBayer(static_cast<std::size_t>(w) * static_cast<std::size_t>(h));
    ASSERT_EQ(0, getMlvRawFrameUint16(fixture.video(), 0, rawBayer.data()));

    const int iterations = 12;
    std::vector<uint16_t> rgb16(static_cast<std::size_t>(w) * static_cast<std::size_t>(h) * 3u);
    /* Every iteration's debayer and refresh are timed on the SAME frame, and
     * their sum is stored per iteration, so the percentiles reported below are
     * percentiles of the real per-frame cost. (Round 1 added the p90 of the
     * debayer samples to the p90 of the refresh samples, which is not the p90
     * of their sum: two samples sets [1x8, 30x2] and [30x2, 1x8] sum to a paired
     * p90 of 31 while their independent p90s add to 60.) */
    auto measure_fast_sh_cost = [&](int threads, std::vector<double> * debayerMs, std::vector<double> * refreshMs,
                                    std::vector<double> * pairedMs)
    {
        for (int i = 0; i < iterations; ++i)
        {
            const double debayerStart = mlv_stage_timing_now();
            debayerBasicU16(rgb16.data(), rawBayer.data(), w, h, threads, /*bit_shift=*/0);
            debayerMs->push_back((mlv_stage_timing_now() - debayerStart) * 1000.0);

            /* Match RenderFrameThread.cpp:4152-4178 exactly: the refresh's
             * cost depends heavily on preview-mode state -- at scale 1, the
             * quarterres RBF path is only selected when preview mode is on
             * and aggressive preview mode is off
             * (processing_standard_x1_shadows_highlights_quarterres_enabled(),
             * raw_processing.c:264-286) -- so calling this function without
             * that state produces a number that does not match what the live
             * path pays. */
            const int previousPreviewMode = processingPlaybackPreviewModeEnabled();
            const int previousAggressivePreviewMode = processingPlaybackAggressivePreviewModeEnabled();
            const int previousPreviewScaleFactor = processingPlaybackPreviewScaleFactor();
            processingSetPlaybackPreviewMode( 1 );
            processingSetPlaybackAggressivePreviewMode(
                mlvPlaybackAggressivePreviewMode() != 0 ? 1 : 0 );
            processingSetPlaybackPreviewScaleFactor( 1 );
            const double refreshStart = mlv_stage_timing_now();
            const int refreshed = processingRefreshShadowsHighlightsBlurFromRgb16(
                fixture.processing(), rgb16.data(), w, h, threads, /*forceExportPolicy=*/0);
            refreshMs->push_back((mlv_stage_timing_now() - refreshStart) * 1000.0);
            pairedMs->push_back(debayerMs->back() + refreshMs->back());
            processingSetPlaybackPreviewScaleFactor( previousPreviewScaleFactor );
            processingSetPlaybackAggressivePreviewMode( previousAggressivePreviewMode );
            processingSetPlaybackPreviewMode( previousPreviewMode );
            ASSERT_NE(0, refreshed);
        }
    };

    const int singleThreadedCount = mlvappEffectivePlaybackWorkerThreadCount();
    std::vector<double> debayerMsSingle;
    std::vector<double> refreshMsSingle;
    std::vector<double> pairedMsSingle;
    measure_fast_sh_cost(singleThreadedCount, &debayerMsSingle, &refreshMsSingle, &pairedMsSingle);

    const int multiThreadedCount = qBound(
        1, static_cast<int>(std::thread::hardware_concurrency()), 16);
    std::vector<double> debayerMsMulti;
    std::vector<double> refreshMsMulti;
    std::vector<double> pairedMsMulti;
    if (multiThreadedCount > singleThreadedCount)
    {
        measure_fast_sh_cost(multiThreadedCount, &debayerMsMulti, &refreshMsMulti, &pairedMsMulti);
    }

    QString reason;
    QString renderer;
    std::vector<uint16_t> gpuOutput(static_cast<std::size_t>(w) * static_cast<std::size_t>(h) * 3u);
    qputenv("MLVAPP_GPU_PREVIEW_ALLOW_SOFTWARE", QByteArray("1"));
    const bool warm = gpuPreviewProcessingApplyDisplayGpuOffscreen(
        config, rgb16.data(), gpuOutput.data(), w, h, &reason, &renderer);
    std::vector<double> displayDrawMs;
    if (warm)
    {
        for (int i = 0; i < 8; ++i)
        {
            const double drawStart = mlv_stage_timing_now();
            const bool ok = gpuPreviewProcessingApplyDisplayGpuOffscreen(
                config, rgb16.data(), gpuOutput.data(), w, h, &reason, &renderer);
            displayDrawMs.push_back((mlv_stage_timing_now() - drawStart) * 1000.0);
            ASSERT_TRUE(ok);
        }
    }

    auto percentile = [](std::vector<double> values, double fraction) -> double
    {
        if (values.empty()) return 0.0;
        std::sort(values.begin(), values.end());
        const std::size_t index = static_cast<std::size_t>(
            std::min<double>(values.size() - 1, std::floor(fraction * (values.size() - 1) + 0.5)));
        return values[index];
    };

    constexpr double kFrameBudgetMs = 40.0;
    auto record_fast_sh_cost = [&](const char * prefix, int threads,
                                   const std::vector<double> & debayerMs,
                                   const std::vector<double> & refreshMs,
                                   const std::vector<double> & pairedMs)
    {
        /* Measurement invariants (the only assertions in this test). */
        ASSERT_EQ(static_cast<std::size_t>(iterations), pairedMs.size());
        ASSERT_EQ(pairedMs.size(), debayerMs.size());
        ASSERT_EQ(pairedMs.size(), refreshMs.size());
        for (std::size_t i = 0; i < pairedMs.size(); ++i)
        {
            ASSERT_TRUE(debayerMs[i] > 0.0 && refreshMs[i] > 0.0);
            ASSERT_TRUE(pairedMs[i] >= debayerMs[i] && pairedMs[i] >= refreshMs[i]);
        }
        const double debayerP90 = percentile(debayerMs, 0.90);
        const double refreshP90 = percentile(refreshMs, 0.90);
        const double pairedP50 = percentile(pairedMs, 0.50);
        const double pairedP90 = percentile(pairedMs, 0.90);
        const double pairedP99 = percentile(pairedMs, 0.99);
        /* Each sample of the sum is >= the matching component, so every order
         * statistic of the sums is >= the same order statistic of a component. */
        ASSERT_TRUE(pairedP90 >= debayerP90 && pairedP90 >= refreshP90);
        ASSERT_TRUE(pairedP50 <= pairedP90 && pairedP90 <= pairedP99);

        const std::string base = std::string("gpu_preview_display.frame_cost.") + prefix + ".";
        test_artifacts::record(base + "threads", std::to_string(threads));
        test_artifacts::record(base + "fast_sh_debayer_ms_p50", std::to_string(percentile(debayerMs, 0.50)));
        test_artifacts::record(base + "fast_sh_debayer_ms_p90", std::to_string(debayerP90));
        test_artifacts::record(base + "fast_sh_refresh_ms_p50", std::to_string(percentile(refreshMs, 0.50)));
        test_artifacts::record(base + "fast_sh_refresh_ms_p90", std::to_string(refreshP90));
        test_artifacts::record(base + "fast_sh_paired_frame_ms_p50", std::to_string(pairedP50));
        test_artifacts::record(base + "fast_sh_paired_frame_ms_p90", std::to_string(pairedP90));
        test_artifacts::record(base + "fast_sh_paired_frame_ms_p99", std::to_string(pairedP99));
        test_artifacts::record(base + "fast_sh_paired_budget_share_p90",
                               std::to_string(pairedP90 / kFrameBudgetMs));
        std::cout << "[FRAME-COST] " << prefix << " threads=" << threads
                  << " paired_ms p50=" << pairedP50 << " p90=" << pairedP90 << " p99=" << pairedP99
                  << " (informational; budget " << kFrameBudgetMs << " ms)\n";
    };
    record_fast_sh_cost("single_threaded", singleThreadedCount, debayerMsSingle, refreshMsSingle, pairedMsSingle);
    if (!debayerMsMulti.empty())
    {
        record_fast_sh_cost("multi_threaded", multiThreadedCount, debayerMsMulti, refreshMsMulti, pairedMsMulti);
    }
    if (!displayDrawMs.empty())
    {
        const double drawP50 = percentile(displayDrawMs, 0.50);
        const double drawP90 = percentile(displayDrawMs, 0.90);
        test_artifacts::record("gpu_preview_display.frame_cost.software_gl_full_offscreen_call_ms_p50",
                               std::to_string(drawP50));
        test_artifacts::record("gpu_preview_display.frame_cost.software_gl_full_offscreen_call_ms_p90",
                               std::to_string(drawP90));
        test_artifacts::record("gpu_preview_display.frame_cost.software_gl_renderer", renderer.toStdString());
    }
    else
    {
        test_artifacts::record("gpu_preview_display.frame_cost.software_gl_skipped_reason", reason.toStdString());
    }
}

TEST(GpuPreviewProcessing, ExposureStopsChangesSubsetConfigAndStableOutput)
{
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    const GpuPreviewProcessingConfig base_config = assert_gpu_preview_subset_supported(fixture);
    const std::string base_hash = render_subset_hash(fixture, base_config, 0);

    processingSetExposureStops(fixture.processing(), 0.75);

    QString reason;
    ASSERT_TRUE(gpuPreviewProcessingIsSupported(fixture.processing(), &reason));
    const GpuPreviewProcessingConfig exposed_config =
        gpuPreviewProcessingBuildConfig(fixture.processing(), &reason);
    ASSERT_TRUE(exposed_config.enabled);
    ASSERT_NEAR(0.75, exposed_config.sourceExposureStops, 0.0001);
    ASSERT_NE(base_config.signature, exposed_config.signature);
    ASSERT_TRUE(base_config.gammaLut != exposed_config.gammaLut);

    /* Directional LUT check: positive exposure must brighten mid-gray.
     * The supported subset preserves positive exposure through the copied
     * pre_calc_gamma LUT (see GpuPreviewProcessing.cpp mechanism comment
     * and src/processing/raw_processing.c::processingSetGamma). A byte-level
     * inequality alone does not prove direction - this asserts the sign. */
    ASSERT_EQ(static_cast<int>(65536u * sizeof(uint16_t)), base_config.gammaLut.size());
    ASSERT_EQ(static_cast<int>(65536u * sizeof(uint16_t)), exposed_config.gammaLut.size());
    const uint16_t * base_gamma =
        reinterpret_cast<const uint16_t *>(base_config.gammaLut.constData());
    const uint16_t * exposed_gamma =
        reinterpret_cast<const uint16_t *>(exposed_config.gammaLut.constData());
    ASSERT_TRUE(exposed_gamma[32768] > base_gamma[32768]);

    const std::string exposed_hash = render_subset_hash(fixture, exposed_config, 0);
    ASSERT_TRUE(base_hash != exposed_hash);

    test_artifacts::record("tiny_dual_iso.gpu_preview_subset.exposure_0_75.frame0",
                           exposed_hash);
    test_artifacts::record("tiny_dual_iso.gpu_preview_subset.exposure_0_75.signature.frame0",
                           std::to_string(exposed_config.signature));
}

TEST(GpuPreviewProcessing, UnsupportedProcessingFeaturesBlockGpuPreviewSubset)
{
    /* highlight_reconstruction is no longer rejected: it is ported as a per-pixel
     * clipped-green replace (see HighlightReconstructionIsSupportedAndMatchesCpuReference). */
    /* allow_creative_adjustments is no longer rejected on its own: the GPU subset
     * shader now ports every creative-family stage -- the in-loop simple-contrast
     * factor, hue-vs/luma-vs curves, vibrance, saturation, toning, the contrast
     * curve and the gradation curves. Clips are still failed closed on the
     * non-creative features below, which are gated independently of the creative
     * flag. */
    /* gradient is now supported (see GradientIsSupportedAndMatchesCpuReference);
     * it is only rejected when enabled without a built mask. */
    assert_gpu_preview_rejects_processing_feature(
        "gradient_no_mask",
        QStringLiteral("gradient mask unavailable"),
        [](processingObject_t * processing) { processing->gradient_enable = 1; processing->gradient_mask = nullptr; });
    assert_gpu_preview_rejects_processing_feature(
        "lut_no_cube",
        QStringLiteral("LUT enabled but cube unavailable"),
        [](processingObject_t * processing) {
            processing->lut_on = 1;
            if (processing->lut) { processing->lut->cube = nullptr; processing->lut->dimension = 0; }
        });
    assert_gpu_preview_rejects_processing_feature(
        "filter",
        QStringLiteral("filter enabled"),
        [](processingObject_t * processing) { processing->filter_on = 1; });
    /* median denoise is now supported for windows <=5 (see MedianIsSupportedAndMatchesCpuReference);
     * a larger window is rejected. */
    assert_gpu_preview_rejects_processing_feature(
        "median_window_too_large",
        QStringLiteral("median denoiser window exceeds 5"),
        [](processingObject_t * processing) { processing->denoiserStrength = 50; processing->denoiserWindow = 7; });
    assert_gpu_preview_rejects_processing_feature(
        "rbf_denoiser",
        QStringLiteral("RBF denoiser enabled"),
        [](processingObject_t * processing) { processing->rbfDenoiserLuma = 25; });
    assert_gpu_preview_rejects_processing_feature(
        "grain",
        QStringLiteral("grain enabled"),
        [](processingObject_t * processing) { processing->grainStrength = 25; });
    assert_gpu_preview_rejects_processing_feature(
        "ca_correction",
        QStringLiteral("CA correction enabled"),
        [](processingObject_t * processing) { processing->ca_desaturate = 1; });
    /* sharpen is now supported standalone (see SharpenIsSupportedAndMatchesCpuReference);
     * rejected only combined with the sobel edge mask or chroma separation. */
    assert_gpu_preview_rejects_processing_feature(
        "sharpen_with_chroma",
        QStringLiteral("sharpen with chroma separation enabled"),
        [](processingObject_t * processing) { processing->sharpen = 0.25; processing->cs_zone.use_cs = 1; });
    /* chroma separation/blur is now supported (see ChromaIsSupportedAndMatchesCpuReference);
     * only an over-radius chroma blur is rejected (the GPU box blur is float32-exact
     * to radius 127). chroma_blur_radius without use_cs is a no-op in the engine. */
    assert_gpu_preview_rejects_processing_feature(
        "chroma_blur_over_radius",
        QStringLiteral("chroma blur radius exceeds 127"),
        [](processingObject_t * processing) { processing->cs_zone.use_cs = 1; processing->cs_zone.chroma_blur_radius = 200; });
    assert_gpu_preview_rejects_processing_feature(
        "clarity",
        QStringLiteral("clarity enabled"),
        [](processingObject_t * processing) { processing->clarity = 0.25; });
    assert_gpu_preview_rejects_processing_feature(
        "vignette_no_mask",
        QStringLiteral("vignette mask unavailable"),
        [](processingObject_t * processing) {
            processing->vignette_strength = 1;
            processing->vignette_mask = nullptr;
            processing->vignette_end = nullptr;
        });
}

TEST(GpuPreviewProcessing, NeutralCreativeAdjustmentsAreSupportedAndApplyCurves)
{
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);

    /* Baseline: the supported subset with creative adjustments OFF. */
    const GpuPreviewProcessingConfig base_config = assert_gpu_preview_subset_supported(fixture);
    ASSERT_TRUE(!base_config.applyCreativeCurves);
    const std::string base_hash = render_subset_hash(fixture, base_config, 0);

    /* Turn the creative flag ON with every UNPORTED creative stage left neutral
     * (vibrance/saturation/toning/hue-vs at their defaults). The gate must now
     * accept it, because the contrast + gradation curves are ported to the GPU
     * subset shader. */
    processingAllowCreativeAdjustments(fixture.processing());

    QString reason;
    ASSERT_TRUE(gpuPreviewProcessingIsSupported(fixture.processing(), &reason));
    const GpuPreviewProcessingConfig creative_config =
        gpuPreviewProcessingBuildConfig(fixture.processing(), &reason);
    ASSERT_TRUE(creative_config.enabled);
    ASSERT_TRUE(creative_config.applyCreativeCurves);
    ASSERT_EQ(static_cast<int>(65536u * sizeof(uint16_t)), creative_config.contrastCurveLut.size());
    ASSERT_EQ(static_cast<int>(65536u * sizeof(uint16_t)), creative_config.gradationLutY.size());
    ASSERT_NE(base_config.signature, creative_config.signature);

    /* The default creative contrast curve (pre_calc_curve_r) is non-identity, so
     * the ported CPU-reference output must differ from the creative-off baseline,
     * proving the curve chain is actually applied. */
    const std::string creative_hash = render_subset_hash(fixture, creative_config, 0);
    ASSERT_TRUE(base_hash != creative_hash);

    test_artifacts::record("tiny_dual_iso.gpu_preview_subset.creative.frame0", creative_hash);
    test_artifacts::record("tiny_dual_iso.gpu_preview_subset.creative.signature.frame0",
                           std::to_string(creative_config.signature));
}

TEST(GpuPreviewProcessing, ShadowsHighlightsIsSupportedAndChangesCpuReference)
{
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    const GpuPreviewProcessingConfig base_config =
        assert_gpu_preview_subset_supported(fixture);
    const std::string base_hash = render_subset_hash(fixture, base_config, 0);

    const GpuPreviewProcessingConfig sh_config =
        build_shadows_highlights_config_with_frame_state(fixture);
    ASSERT_TRUE(sh_config.applyCreativeCurves);
    ASSERT_TRUE(sh_config.applyShadowsHighlights);
    ASSERT_NE(base_config.signature, sh_config.signature);

    const std::string sh_hash = render_subset_hash(fixture, sh_config, 0);
    ASSERT_TRUE(base_hash != sh_hash);

    test_artifacts::record("tiny_dual_iso.gpu_preview_subset.shadows_highlights.frame0", sh_hash);
}

TEST(GpuPreviewProcessing, NonNeutralToningIsSupportedAndChangesOutput)
{
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    (void)assert_gpu_preview_subset_supported(fixture);

    /* Creative on, every stage neutral: toning must be inert. */
    processingAllowCreativeAdjustments(fixture.processing());
    QString reason;
    ASSERT_TRUE(gpuPreviewProcessingIsSupported(fixture.processing(), &reason));
    const GpuPreviewProcessingConfig neutral_config =
        gpuPreviewProcessingBuildConfig(fixture.processing(), &reason);
    ASSERT_TRUE(neutral_config.enabled);
    ASSERT_TRUE(!neutral_config.applyToning);
    const std::string neutral_hash = render_subset_hash(fixture, neutral_config, 0);

    /* Non-neutral toning is now ported to the GPU subset, so the gate must accept
     * it (not force CPU) and the per-channel gain must change the output. */
    processingSetToning(fixture.processing(), 255, 192, 0, 40);
    ASSERT_TRUE(gpuPreviewProcessingIsSupported(fixture.processing(), &reason));
    const GpuPreviewProcessingConfig toned_config =
        gpuPreviewProcessingBuildConfig(fixture.processing(), &reason);
    ASSERT_TRUE(toned_config.enabled);
    ASSERT_TRUE(toned_config.applyToning);
    ASSERT_NE(neutral_config.signature, toned_config.signature);

    const std::string toned_hash = render_subset_hash(fixture, toned_config, 0);
    ASSERT_TRUE(neutral_hash != toned_hash);

    test_artifacts::record("tiny_dual_iso.gpu_preview_subset.toning.frame0", toned_hash);
}

TEST(GpuPreviewProcessing, NonNeutralSaturationIsSupportedAndChangesOutput)
{
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    (void)assert_gpu_preview_subset_supported(fixture);

    processingAllowCreativeAdjustments(fixture.processing());
    QString reason;
    ASSERT_TRUE(gpuPreviewProcessingIsSupported(fixture.processing(), &reason));
    const GpuPreviewProcessingConfig neutral_config =
        gpuPreviewProcessingBuildConfig(fixture.processing(), &reason);
    ASSERT_TRUE(neutral_config.enabled);
    ASSERT_TRUE(!neutral_config.applySaturation);
    const std::string neutral_hash = render_subset_hash(fixture, neutral_config, 0);

    /* Saturation is now ported (direct Y1 + (pix-Y1)*sat), so the gate accepts it
     * and the chroma scale must change the output. */
    processingSetSaturation(fixture.processing(), 1.5);
    ASSERT_TRUE(gpuPreviewProcessingIsSupported(fixture.processing(), &reason));
    const GpuPreviewProcessingConfig sat_config =
        gpuPreviewProcessingBuildConfig(fixture.processing(), &reason);
    ASSERT_TRUE(sat_config.enabled);
    ASSERT_TRUE(sat_config.applySaturation);
    ASSERT_NEAR(1.5, sat_config.saturation, 0.0001);
    ASSERT_NE(neutral_config.signature, sat_config.signature);

    const std::string sat_hash = render_subset_hash(fixture, sat_config, 0);
    ASSERT_TRUE(neutral_hash != sat_hash);

    test_artifacts::record("tiny_dual_iso.gpu_preview_subset.saturation.frame0", sat_hash);
}

TEST(GpuPreviewProcessing, NonNeutralVibranceIsSupportedAndChangesOutput)
{
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    (void)assert_gpu_preview_subset_supported(fixture);

    processingAllowCreativeAdjustments(fixture.processing());
    QString reason;
    ASSERT_TRUE(gpuPreviewProcessingIsSupported(fixture.processing(), &reason));
    const GpuPreviewProcessingConfig neutral_config =
        gpuPreviewProcessingBuildConfig(fixture.processing(), &reason);
    ASSERT_TRUE(neutral_config.enabled);
    ASSERT_TRUE(!neutral_config.applyVibrance);
    const std::string neutral_hash = render_subset_hash(fixture, neutral_config, 0);

    /* Positive vibrance is now ported (saturation-weighted blend), so the gate
     * accepts it and the output must change. */
    processingSetVibrance(fixture.processing(), 1.5);
    ASSERT_TRUE(gpuPreviewProcessingIsSupported(fixture.processing(), &reason));
    const GpuPreviewProcessingConfig vib_config =
        gpuPreviewProcessingBuildConfig(fixture.processing(), &reason);
    ASSERT_TRUE(vib_config.enabled);
    ASSERT_TRUE(vib_config.applyVibrance);
    ASSERT_NEAR(1.5, vib_config.vibrance, 0.0001);
    ASSERT_NE(neutral_config.signature, vib_config.signature);

    const std::string vib_hash = render_subset_hash(fixture, vib_config, 0);
    ASSERT_TRUE(neutral_hash != vib_hash);

    test_artifacts::record("tiny_dual_iso.gpu_preview_subset.vibrance.frame0", vib_hash);
}

TEST(GpuPreviewProcessing, NonNeutralHueVsIsSupportedAndChangesOutput)
{
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    (void)assert_gpu_preview_subset_supported(fixture);

    processingAllowCreativeAdjustments(fixture.processing());
    QString reason;
    ASSERT_TRUE(gpuPreviewProcessingIsSupported(fixture.processing(), &reason));
    const GpuPreviewProcessingConfig neutral_config =
        gpuPreviewProcessingBuildConfig(fixture.processing(), &reason);
    ASSERT_TRUE(neutral_config.enabled);
    ASSERT_TRUE(!neutral_config.applyHueVs);
    const std::string neutral_hash = render_subset_hash(fixture, neutral_config, 0);

    /* hue-vs / luma-vs curves are now ported (RGB->HSV, four curve adjustments,
     * HSV->RGB). A constant +0.5 hue_vs_hue curve rotates every chroma pixel's
     * hue by 60*0.5 = 30 degrees, so the gate must accept it (not force the CPU
     * path) and the output must change. */
    for (int i = 0; i < 36000; ++i) fixture.processing()->hue_vs_hue[i] = 0.5f;
    fixture.processing()->hue_vs_hue_used = 1;
    ASSERT_TRUE(gpuPreviewProcessingIsSupported(fixture.processing(), &reason));
    const GpuPreviewProcessingConfig huevs_config =
        gpuPreviewProcessingBuildConfig(fixture.processing(), &reason);
    ASSERT_TRUE(huevs_config.enabled);
    ASSERT_TRUE(huevs_config.applyHueVs);
    ASSERT_NE(neutral_config.signature, huevs_config.signature);

    const std::string huevs_hash = render_subset_hash(fixture, huevs_config, 0);
    ASSERT_TRUE(neutral_hash != huevs_hash);

    test_artifacts::record("tiny_dual_iso.gpu_preview_subset.hue_vs.frame0", huevs_hash);
}

TEST(GpuPreviewProcessing, NonNeutralInLoopContrastIsSupportedAndChangesOutput)
{
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    (void)assert_gpu_preview_subset_supported(fixture);

    processingAllowCreativeAdjustments(fixture.processing());
    QString reason;
    ASSERT_TRUE(gpuPreviewProcessingIsSupported(fixture.processing(), &reason));
    const GpuPreviewProcessingConfig neutral_config =
        gpuPreviewProcessingBuildConfig(fixture.processing(), &reason);
    ASSERT_TRUE(neutral_config.enabled);
    ASSERT_TRUE(!neutral_config.applyInLoopContrast);
    const std::string neutral_hash = render_subset_hash(fixture, neutral_config, 0);

    /* The in-loop simple-contrast factor is now ported (per-pixel luma-dependent
     * exposure multiply by contrast_curve[cval]), so the gate must accept a
     * non-zero contrast (previously the last creative reject) and the output must
     * change. processingSetSimpleContrast sets contrast = value*0.65 and rebuilds
     * the curve. */
    processingSetSimpleContrast(fixture.processing(), 1.0);
    ASSERT_TRUE(gpuPreviewProcessingIsSupported(fixture.processing(), &reason));
    const GpuPreviewProcessingConfig contrast_config =
        gpuPreviewProcessingBuildConfig(fixture.processing(), &reason);
    ASSERT_TRUE(contrast_config.enabled);
    ASSERT_TRUE(contrast_config.applyInLoopContrast);
    ASSERT_NE(neutral_config.signature, contrast_config.signature);

    const std::string contrast_hash = render_subset_hash(fixture, contrast_config, 0);
    ASSERT_TRUE(neutral_hash != contrast_hash);

    test_artifacts::record("tiny_dual_iso.gpu_preview_subset.in_loop_contrast.frame0", contrast_hash);
}

TEST(GpuPreviewProcessing, NonRec709GamutIsSupportedAndMatchesCpuReference)
{
    /* Non-Rec709 gamut used to fail closed. It is now supported: the gamut is
     * baked into proper_wb_matrix (applied in-shader) and the gamut-compression
     * luma weights are derived per-gamut (config.rgbToY via processingGamutRgbToY)
     * rather than assuming Rec709. Verify the gate accepts it, the config differs
     * from Rec709, and the GPU offscreen output matches the CPU reference. */
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    const GpuPreviewProcessingConfig rec709 = assert_gpu_preview_subset_supported(fixture);

    processingSetGamut(fixture.processing(), GAMUT_Rec2020);
    QString reason;
    ASSERT_TRUE(gpuPreviewProcessingIsSupported(fixture.processing(), &reason));
    const GpuPreviewProcessingConfig rec2020 =
        gpuPreviewProcessingBuildConfig(fixture.processing(), &reason);
    ASSERT_TRUE(rec2020.enabled);
    ASSERT_NE(rec709.signature, rec2020.signature);

    assert_gpu_offscreen_matches_cpu_reference(fixture, rec2020, "gamut_rec2020");
}

TEST(GpuPreviewProcessing, AgXIsSupportedAndMatchesCpuReference)
{
    /* AgX used to fail closed. It is now supported: a forward compressed-gamut
     * matmul before gamma and the inverse after the creative curves, carried as
     * uniform matrices (engine-derived via processingAgxMatrices). Verify the gate
     * accepts it, the config differs, and the GPU offscreen output matches the
     * CPU reference. */
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    const GpuPreviewProcessingConfig base = assert_gpu_preview_subset_supported(fixture);

    processingEnableAgX(fixture.processing());
    QString reason;
    ASSERT_TRUE(gpuPreviewProcessingIsSupported(fixture.processing(), &reason));
    const GpuPreviewProcessingConfig agx =
        gpuPreviewProcessingBuildConfig(fixture.processing(), &reason);
    ASSERT_TRUE(agx.enabled);
    ASSERT_TRUE(agx.applyAgx);
    ASSERT_NE(base.signature, agx.signature);

    assert_gpu_offscreen_matches_cpu_reference(fixture, agx, "agx");
}

TEST(GpuPreviewProcessing, VignetteIsSupportedAndMatchesCpuReference)
{
    /* Vignette is the first position-dependent stage: a per-pixel exposure
     * multiply by a full-frame mask, applied with the vmpix pre-increment off-by-
     * one. Build an ASYMMETRIC mask (xStretch != yStretch) so a raster-flip bug in
     * the GPU mask sampling changes the output (a symmetric 4-way-mirrored mask
     * could hide it). Verify the gate accepts it, the config carries the frame-
     * sized mask, and the GPU offscreen output matches the CPU reference. */
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    const GpuPreviewProcessingConfig base = assert_gpu_preview_subset_supported(fixture);

    processingSetVignetteMask(fixture.processing(), static_cast<uint16_t>(fixture.width()),
                              static_cast<uint16_t>(fixture.height()), 0.5f, 0.2f, 1.0f, 1.4f);
    processingSetVignetteStrength(fixture.processing(), 60);
    QString reason;
    ASSERT_TRUE(gpuPreviewProcessingIsSupported(fixture.processing(), &reason));
    const GpuPreviewProcessingConfig vig =
        gpuPreviewProcessingBuildConfig(fixture.processing(), &reason);
    ASSERT_TRUE(vig.enabled);
    ASSERT_TRUE(vig.applyVignette);
    ASSERT_EQ(static_cast<int>(static_cast<size_t>(fixture.width()) * fixture.height() * sizeof(float)),
              vig.vignetteMask.size());
    ASSERT_NE(base.signature, vig.signature);

    assert_gpu_offscreen_matches_cpu_reference(fixture, vig, "vignette");
}

TEST(GpuPreviewProcessing, HighlightReconstructionIsSupportedAndMatchesCpuReference)
{
    /* Highlight reconstruction used to fail closed. It is now supported as a
     * per-pixel stage: for a clipped green it replaces green with (R+B)/2, keyed on
     * the uint16 matrix-green (tmp1) matching the static white-level green (non
     * dual-ISO) or a +/-5000 window around the per-frame dual-ISO peak plus the
     * green-dominance guard. Verify the gate accepts it, the config carries the
     * recon fields, the recon actually FIRES (output changes vs the baseline), and
     * the GPU offscreen output matches the CPU reference. */
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    const GpuPreviewProcessingConfig base = assert_gpu_preview_subset_supported(fixture);
    const std::string base_hash = render_subset_hash(fixture, base, 0);

    /* Pick a dual-ISO peak that is GUARANTEED to fire on this frame instead of
     * guessing one: replicate the engine's levels->diagonal-matrix lookup
     * (tmp1 = matrixG[levels[green]], pix = matrix[levels[*]]) using the config
     * LUTs, and find a pixel whose matrix-green satisfies the green-dominance
     * guard (mg < 1.1*mr && mg < mb). Setting highest_green_diso to that pixel's
     * matrix-green puts it inside the +/-5000 window, so the replace branch fires. */
    const std::vector<uint16_t> debayered = fixture.renderDebayeredFrame16(0);
    ASSERT_TRUE(!debayered.empty());
    const uint16_t * levels = reinterpret_cast<const uint16_t *>(base.levelsLut.constData());
    const uint16_t * mtxR = reinterpret_cast<const uint16_t *>(base.matrixLutR.constData());
    const uint16_t * mtxG = reinterpret_cast<const uint16_t *>(base.matrixLutG.constData());
    const uint16_t * mtxB = reinterpret_cast<const uint16_t *>(base.matrixLutB.constData());
    int target_diso = -1;
    const int pixel_count = fixture.width() * fixture.height();
    for (int i = 0; i < pixel_count; ++i)
    {
        const int mr = mtxR[levels[debayered[i * 3 + 0]]];
        const int mg = mtxG[levels[debayered[i * 3 + 1]]];
        const int mb = mtxB[levels[debayered[i * 3 + 2]]];
        /* Margin keeps the chosen firing pixel clear of the guard boundary so
         * CPU/GPU float rounding of p0/p1/p2 cannot flip its replace decision. */
        if (mg + 50 < (1.1 * mr) && mg + 50 < mb) { target_diso = mg; break; }
    }
    ASSERT_TRUE(target_diso >= 0);

    processingObject_t * p = fixture.processing();
    ASSERT_TRUE(p != nullptr);
    ASSERT_TRUE(p->dual_iso != nullptr);
    p->highlight_reconstruction = 1;
    *p->dual_iso = 1;
    p->highest_green_diso = static_cast<uint16_t>(target_diso);

    QString reason;
    ASSERT_TRUE(gpuPreviewProcessingIsSupported(p, &reason));
    const GpuPreviewProcessingConfig cfg = gpuPreviewProcessingBuildConfig(p, &reason);
    ASSERT_TRUE(cfg.enabled);
    ASSERT_TRUE(cfg.applyHighlightReconstruction);
    ASSERT_TRUE(cfg.highlightReconDualIso);
    ASSERT_EQ(target_diso, cfg.highestGreenDiso);
    ASSERT_NE(base.signature, cfg.signature);

    const std::string recon_hash = render_subset_hash(fixture, cfg, 0);
    ASSERT_TRUE(base_hash != recon_hash);

    assert_gpu_offscreen_matches_cpu_reference(fixture, cfg, "highlight_recon");
}

TEST(GpuPreviewProcessing, GradientIsSupportedAndMatchesCpuReference)
{
    /* Gradient used to fail closed. It is now supported as a per-pixel stage: a
     * second pre-creative pipeline through the gradient LUTs (shared WB/gamut/AgX,
     * separate gradient matrix + gamma + contrast), blended into the base in gamma
     * space by the per-pixel mask BEFORE the creative chain (which runs once on the
     * blended result). Enable a non-identity gradient (diagonal mask ramp + one
     * stop of gradient exposure) and verify the gate accepts it, the config carries
     * the frame-sized mask, the output changes vs the baseline, and the GPU
     * offscreen output matches the CPU reference. */
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    const GpuPreviewProcessingConfig base = assert_gpu_preview_subset_supported(fixture);
    const std::string base_hash = render_subset_hash(fixture, base, 0);

    processingObject_t * p = fixture.processing();
    ASSERT_TRUE(p != nullptr);
    const uint16_t w = static_cast<uint16_t>(fixture.width());
    const uint16_t h = static_cast<uint16_t>(fixture.height());
    processingSetGradientEnable(p, 1);
    processingSetGradientMask(p, w, h, 0.0f, 0.0f, static_cast<float>(w), static_cast<float>(h));
    processingSetGradientExposure(p, 1.0);

    QString reason;
    ASSERT_TRUE(gpuPreviewProcessingIsSupported(p, &reason));
    const GpuPreviewProcessingConfig cfg = gpuPreviewProcessingBuildConfig(p, &reason);
    ASSERT_TRUE(cfg.enabled);
    ASSERT_TRUE(cfg.applyGradient);
    ASSERT_TRUE(cfg.gradientMaskData != nullptr);
    ASSERT_NE(base.signature, cfg.signature);

    const std::string grad_hash = render_subset_hash(fixture, cfg, 0);
    ASSERT_TRUE(base_hash != grad_hash);

    assert_gpu_offscreen_matches_cpu_reference(fixture, cfg, "gradient");
}

TEST(GpuPreviewProcessing, GradientAndAgXEngineOutputMatchesIndependentPreviewOracle)
{
    /* This combination exercises the engine branch that stores the AgX matrix
     * result into the float gradient layer.  The preview reference is a separate
     * implementation, so it detects both the historical uint16_t-through-float
     * pointer bug and future typed-store drift in the production engine. */
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    (void)assert_gpu_preview_subset_supported(fixture);

    processingObject_t * p = fixture.processing();
    ASSERT_TRUE(p != nullptr);
    const uint16_t w = static_cast<uint16_t>(fixture.width());
    const uint16_t h = static_cast<uint16_t>(fixture.height());
    processingSetGradientEnable(p, 1);
    processingSetGradientMask(p, w, h, 0.0f, 0.0f, static_cast<float>(w), static_cast<float>(h));
    processingSetGradientExposure(p, 1.0);
    processingEnableAgX(p);

    QString reason;
    ASSERT_TRUE(gpuPreviewProcessingIsSupported(p, &reason));
    const GpuPreviewProcessingConfig cfg = gpuPreviewProcessingBuildConfig(p, &reason);
    ASSERT_TRUE(cfg.enabled);
    ASSERT_TRUE(cfg.applyGradient);
    ASSERT_TRUE(cfg.applyAgx);

    const std::vector<uint16_t> debayered = fixture.renderDebayeredFrame16(0);
    ASSERT_TRUE(!debayered.empty());
    std::vector<uint16_t> engine_input = debayered;
    std::vector<uint16_t> engine(debayered.size(), 0);
    std::vector<uint16_t> engine_blur(debayered.size(), 0);
    apply_processing_object(p, fixture.width(), fixture.height(),
                            engine_input.data(), engine.data(),
                            engine_blur.data(), p->gradient_mask,
                            p->vignette_mask, nullptr);

    const std::vector<uint16_t> oracle =
        render_gpu_preview_subset_cpu_reference(fixture, cfg, 0);
    ASSERT_EQ(oracle.size(), engine.size());

    const frame_compare_result_t result = compare_frames_u16(
        oracle.data(), engine.data(), fixture.width(), fixture.height(), 3,
        /*per_pixel_tolerance=*/2);
    /* The independently implemented preview path and the engine differ at a
     * small set of LUT/gamut boundary samples (0.365%, max 3395 LSB on the
     * frozen fixture).  The historical uint16_t-through-float corruption
     * disagrees on 99.39% of samples, so these bounds retain a wide separation
     * from the actual regression while allowing the independently grounded
     * edge behavior. */
    const frame_tolerance_verdict_t verdict = evaluate_frame_tolerance(
        result, engine.size(), /*max_abs_diff_threshold=*/4096,
        /*max_mismatch_fraction=*/0.005);
    if (!verdict.passed)
    {
        ::minitest::fail(__FILE__, __LINE__,
                         "gradient + AgX engine vs independent preview oracle",
                         verdict.detail);
    }

    test_artifacts::record(
        "tiny_dual_iso.gradient_agx.engine_vs_preview.compare",
        frame_compare_summary(result));
}

TEST(GpuPreviewProcessing, BlurImageBoxParity)
{
    /* The GPU separable integer box blur is the keystone pre-pass for the spatial
     * stages (chroma blur / sharpen / median). It must reproduce the engine
     * blur_image (processing.c:589) BIT-EXACTLY (0 LSB). Run the same debayered
     * frame through engine blur_image and the GPU offscreen box blur at several
     * radii -- all channels, then the chroma Cb/Cr (do_r=0) case -- and assert
     * byte equality. Skips only when no GL backend is available. */
    qputenv("MLVAPP_GPU_PREVIEW_ALLOW_SOFTWARE", QByteArray("1"));
    const GpuPreviewProcessingBackendAvailability availability =
        gpuPreviewProcessingProbeGpuBackend();
    if (!availability.available)
    {
        ASSERT_TRUE(gpu_preview_skip_reason_is_known(availability.reason));
        SKIP_TEST(availability.reason.toStdString());
    }

    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    const std::vector<uint16_t> debayered = fixture.renderDebayeredFrame16(0);
    ASSERT_TRUE(!debayered.empty());
    const int width = fixture.width();
    const int height = fixture.height();
    ASSERT_EQ(static_cast<size_t>(width) * height * 3u, debayered.size());

    const int radii[] = { 1, 2, 3, 5 };
    const int channelCases[2][3] = { {1, 1, 1}, {0, 1, 1} };
    for (const auto & ch : channelCases)
    {
        for (int radius : radii)
        {
            std::vector<uint16_t> reference = debayered;
            std::vector<uint16_t> scratch(debayered.size(), 0);
            blur_image(reference.data(), scratch.data(), width, height, radius,
                       ch[0], ch[1], ch[2], 0, height);

            std::vector<uint16_t> gpu(debayered.size(), 0);
            QString reason;
            QString renderer;
            const bool ok = gpuPreviewProcessingApplyBoxBlurOffscreen(
                debayered.data(), gpu.data(), width, height, radius,
                ch[0] != 0, ch[1] != 0, ch[2] != 0, &reason, &renderer);
            if (!ok)
            {
                ASSERT_TRUE(gpu_preview_skip_reason_is_known(reason));
                SKIP_TEST(reason.toStdString());
            }

            const frame_compare_result_t result = compare_frames_u16(
                reference.data(), gpu.data(), width, height, 3, /*per_pixel_tolerance=*/0);
            const std::string label = "box_blur.r" + std::to_string(radius) + ".ch"
                + std::to_string(ch[0]) + std::to_string(ch[1]) + std::to_string(ch[2]);
            test_artifacts::record("gpu_preview_subset.gpu_parity." + label + ".renderer",
                                   renderer.toStdString());
            test_artifacts::record("gpu_preview_subset.gpu_parity." + label + ".compare",
                                   frame_compare_summary(result));
            if (result.max_abs_diff != 0)
            {
                ::minitest::fail(__FILE__, __LINE__,
                                 std::string("GPU box blur vs engine blur_image (") + label + ")",
                                 frame_compare_summary(result));
            }
        }
    }
}

TEST(GpuPreviewProcessing, ChromaIsSupportedAndMatchesCpuReference)
{
    /* Chroma separation/blur is now supported: a YCbCr round-trip post-pass with
     * an optional box blur of Cb/Cr (reusing the bit-exact box blur), applied after
     * the per-pixel colour pipeline. Verify the gate accepts use_cs, the config
     * carries the chroma fields, the output changes vs the baseline, and the GPU
     * offscreen output (per-pixel pass + GPU chroma post-pass) matches the CPU
     * reference (per-pixel + CPU chroma post-pass). */
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    const GpuPreviewProcessingConfig base = assert_gpu_preview_subset_supported(fixture);
    const std::string base_hash = render_subset_hash(fixture, base, 0);

    processingObject_t * p = fixture.processing();
    ASSERT_TRUE(p != nullptr);
    processingEnableChromaSeparation(p);
    processingSetChromaBlurRadius(p, 3);

    QString reason;
    ASSERT_TRUE(gpuPreviewProcessingIsSupported(p, &reason));
    const GpuPreviewProcessingConfig cfg = gpuPreviewProcessingBuildConfig(p, &reason);
    ASSERT_TRUE(cfg.enabled);
    ASSERT_TRUE(cfg.applyChroma);
    ASSERT_EQ(3, cfg.chromaBlurRadius);
    ASSERT_NE(base.signature, cfg.signature);

    const std::string chroma_hash = render_subset_hash(fixture, cfg, 0);
    ASSERT_TRUE(base_hash != chroma_hash);

    assert_gpu_offscreen_matches_cpu_reference(fixture, cfg, "chroma");
}

TEST(GpuPreviewProcessing, SharpenIsSupportedAndMatchesCpuReference)
{
    /* Sharpen is now supported standalone: a fixed 5-tap cross post-pass over the
     * developed image (no chroma separation, no sobel mask). Verify the gate
     * accepts it, the config carries the sharpen coefficients, the output changes
     * vs the baseline, and the GPU offscreen output matches the CPU reference. */
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    const GpuPreviewProcessingConfig base = assert_gpu_preview_subset_supported(fixture);
    const std::string base_hash = render_subset_hash(fixture, base, 0);

    processingObject_t * p = fixture.processing();
    ASSERT_TRUE(p != nullptr);
    processingSetSharpening(p, 0.5);

    QString reason;
    ASSERT_TRUE(gpuPreviewProcessingIsSupported(p, &reason));
    const GpuPreviewProcessingConfig cfg = gpuPreviewProcessingBuildConfig(p, &reason);
    ASSERT_TRUE(cfg.enabled);
    ASSERT_TRUE(cfg.applySharpen);
    ASSERT_NE(base.signature, cfg.signature);

    const std::string sharp_hash = render_subset_hash(fixture, cfg, 0);
    ASSERT_TRUE(base_hash != sharp_hash);

    assert_gpu_offscreen_matches_cpu_reference(fixture, cfg, "sharpen");
}

TEST(GpuPreviewProcessing, MedianIsSupportedAndMatchesCpuReference)
{
    /* Median denoise is now supported (windows <=5): a per-pixel window-median
     * post-pass blended by strength. Verify the gate accepts it, the config carries
     * the median fields, the output changes vs the baseline, and the GPU offscreen
     * output matches the CPU reference. */
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    const GpuPreviewProcessingConfig base = assert_gpu_preview_subset_supported(fixture);
    const std::string base_hash = render_subset_hash(fixture, base, 0);

    processingObject_t * p = fixture.processing();
    ASSERT_TRUE(p != nullptr);
    p->denoiserWindow = 3;
    p->denoiserStrength = 80;

    QString reason;
    ASSERT_TRUE(gpuPreviewProcessingIsSupported(p, &reason));
    const GpuPreviewProcessingConfig cfg = gpuPreviewProcessingBuildConfig(p, &reason);
    ASSERT_TRUE(cfg.enabled);
    ASSERT_TRUE(cfg.applyMedian);
    ASSERT_EQ(3, cfg.medianWindow);
    ASSERT_NE(base.signature, cfg.signature);

    const std::string median_hash = render_subset_hash(fixture, cfg, 0);
    ASSERT_TRUE(base_hash != median_hash);

    assert_gpu_offscreen_matches_cpu_reference(fixture, cfg, "median");
}

static void install_test_lut(MlvPipelineFixture & fixture, int dim, bool is3d)
{
    lut_t * lut = fixture.processing()->lut;
    ASSERT_TRUE(lut != nullptr);
    if (lut->cube) { free(lut->cube); lut->cube = nullptr; }
    lut->dimension = static_cast<uint16_t>(dim);
    lut->is3d = is3d ? 1 : 0;
    lut->intensity = 100;
    for (int i = 0; i < 3; ++i) { lut->domain_min[i] = 0.0f; lut->domain_max[i] = 1.0f; }
    const int entries = is3d ? (dim * dim * dim) : dim;
    lut->cube = static_cast<float *>(malloc(static_cast<size_t>(entries) * 3 * sizeof(float)));
    ASSERT_TRUE(lut->cube != nullptr);
    if (is3d)
    {
        for (int b = 0; b < dim; ++b)
            for (int g = 0; g < dim; ++g)
                for (int r = 0; r < dim; ++r)
                {
                    const int e = r + g * dim + b * dim * dim;
                    const float rn = static_cast<float>(r) / (dim - 1);
                    const float gn = static_cast<float>(g) / (dim - 1);
                    const float bn = static_cast<float>(b) / (dim - 1);
                    lut->cube[e * 3 + 0] = std::min(1.0f, rn * 0.95f + bn * 0.05f);
                    lut->cube[e * 3 + 1] = gn * 0.90f + 0.03f;
                    lut->cube[e * 3 + 2] = std::min(1.0f, bn * 1.05f);
                }
    }
    else
    {
        for (int k = 0; k < dim; ++k)
        {
            const float t = static_cast<float>(k) / (dim - 1);
            lut->cube[k * 3 + 0] = std::min(1.0f, t * 0.95f + 0.02f);
            lut->cube[k * 3 + 1] = t * 0.90f + 0.03f;
            lut->cube[k * 3 + 2] = std::min(1.0f, t * 1.05f);
        }
    }
    fixture.processing()->lut_on = 1;
}

TEST(GpuPreviewProcessing, Lut3dIsSupportedAndMatchesCpuReference)
{
    /* 3D .cube LUT (the engine's tetrahedral output-stage stage) is now supported,
     * applied last from a dim^3 RGBA32F volume texture. Install a smooth non-
     * identity 17^3 cube and verify the GPU offscreen output matches the CPU
     * reference (which replicates the exact tetrahedral T1-T6 selection). */
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    const GpuPreviewProcessingConfig base = assert_gpu_preview_subset_supported(fixture);
    install_test_lut(fixture, 17, true);
    QString reason;
    ASSERT_TRUE(gpuPreviewProcessingIsSupported(fixture.processing(), &reason));
    const GpuPreviewProcessingConfig cfg =
        gpuPreviewProcessingBuildConfig(fixture.processing(), &reason);
    ASSERT_TRUE(cfg.enabled);
    ASSERT_TRUE(cfg.applyLut);
    ASSERT_TRUE(cfg.lut3d);
    ASSERT_NE(base.signature, cfg.signature);
    assert_gpu_offscreen_matches_cpu_reference(fixture, cfg, "lut_3d");
}

TEST(GpuPreviewProcessing, Lut1dIsSupportedAndMatchesCpuReference)
{
    /* 1D .cube LUT (per-channel lerp) is now supported, applied last from a dim x 1
     * RGBA32F texture. */
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    const GpuPreviewProcessingConfig base = assert_gpu_preview_subset_supported(fixture);
    install_test_lut(fixture, 33, false);
    QString reason;
    ASSERT_TRUE(gpuPreviewProcessingIsSupported(fixture.processing(), &reason));
    const GpuPreviewProcessingConfig cfg =
        gpuPreviewProcessingBuildConfig(fixture.processing(), &reason);
    ASSERT_TRUE(cfg.enabled);
    ASSERT_TRUE(cfg.applyLut);
    ASSERT_TRUE(!cfg.lut3d);
    ASSERT_NE(base.signature, cfg.signature);
    assert_gpu_offscreen_matches_cpu_reference(fixture, cfg, "lut_1d");
}

TEST(GpuPreviewProcessing, GpuOffscreenMatchesCpuReferenceForSupportedSubset)
{
    /* Shader-level parity for the base supported subset (levels / matrix / camera
     * WB / gamut / gamma), no creative adjustments. Skips without a hardware GL
     * backend; on the RTX 4090 (or any non-software GPU) it diffs the real GLSL
     * offscreen output against the CPU reference. */
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    const GpuPreviewProcessingConfig config = assert_gpu_preview_subset_supported(fixture);
    assert_gpu_offscreen_matches_cpu_reference(fixture, config, "supported_subset");
}

TEST(GpuPreviewProcessing, GpuOffscreenMatchesCpuReferenceForShadowsHighlights)
{
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    const GpuPreviewProcessingConfig config =
        build_shadows_highlights_config_with_frame_state(fixture);
    assert_gpu_offscreen_matches_cpu_reference(fixture, config, "shadows_highlights");
}

TEST(GpuPreviewProcessing, GpuOffscreenMatchesCpuReferenceForFullCreativeGrade)
{
    /* Shader-level parity with the ENTIRE creative-adjustments family active, so a
     * single GPU render exercises every ported slice at once: in-loop contrast
     * (slice 6) + hue-vs (slice 5) + vibrance (slice 4) + saturation (slice 3) +
     * toning (slice 2) + contrast curve & gradation (slice 1). Skips without a
     * hardware GL backend; produces a real CPU-vs-GPU verdict on the 4090. */
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    (void)assert_gpu_preview_subset_supported(fixture);

    processingObject_t * p = fixture.processing();
    ASSERT_TRUE(p != nullptr);
    processingAllowCreativeAdjustments(p);
    processingSetSimpleContrast(p, 1.0);        /* slice 6: in-loop contrast factor  */
    processingSetSaturation(p, 1.4);            /* slice 3                            */
    processingSetVibrance(p, 1.3);              /* slice 4                            */
    processingSetToning(p, 255, 192, 0, 40);    /* slice 2: per-channel toning        */
    for (int i = 0; i < 36000; ++i) p->hue_vs_hue[i] = 0.4f;  /* slice 5: hue rotation */
    p->hue_vs_hue_used = 1;

    QString reason;
    ASSERT_TRUE(gpuPreviewProcessingIsSupported(p, &reason));
    const GpuPreviewProcessingConfig config = gpuPreviewProcessingBuildConfig(p, &reason);
    ASSERT_TRUE(config.enabled);
    ASSERT_TRUE(config.applyCreativeCurves);
    ASSERT_TRUE(config.applyToning);
    ASSERT_TRUE(config.applySaturation);
    ASSERT_TRUE(config.applyVibrance);
    ASSERT_TRUE(config.applyHueVs);
    ASSERT_TRUE(config.applyInLoopContrast);

    assert_gpu_offscreen_matches_cpu_reference(fixture, config, "full_creative_grade");
}
/* ---- PLAYBACK-SEEK-RENDER-PARITY-1: the full creative chain on the live display shader ----
 *
 * CUDA texture playback presents through the DISPLAY shader. It used to stop at
 * vibrance, so it dropped the receipt's dark/light S-curve (on for every default
 * receipt) and every later creative stage: playback looked lifted ("milky")
 * against the paused frame and the CPU route. The shader now applies hue-vs,
 * saturation, toning, the creative curves (S-curve + gradation, composed into one
 * per-channel table) and AgX at the engine's positions; vignette, highlight
 * reconstruction, gradient, .cube LUT, chroma separation, sharpen and median are
 * REFUSED instead (the texture route falls back, typed telemetry). Tolerances are
 * the engine-anchored / direct8 budgets above, unchanged. */

TEST(GpuPreviewProcessing, EngineAnchoredReceiptSCurveRealFrameMatchesEngine)
{
    /* The owner-visible defect: the receipt's own S-curve (ReceiptSettings defaults,
     * ds=20 dr=70), Look Assist off, on the real fixture frame, against the whole
     * production render. Before the port: max 11901 codes, 100% of samples out. */
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    configure_gpu_preview_supported_subset(fixture);
    processingObject_t * processing = fixture.processing();
    processingAllowCreativeAdjustments(processing);
    const std::vector<uint16_t> primed = fixture.renderFrame16(0, /*threads=*/1);
    ASSERT_TRUE(!primed.empty());

    QString reason;
    GpuPreviewProcessingConfig config = gpuPreviewProcessingBuildConfig(processing, &reason);
    ASSERT_TRUE(config.enabled);
    ASSERT_TRUE(config.applyCreativeCurves);
    ASSERT_TRUE(gpuPreviewProcessingDisplayShaderRefusedStages(config).isEmpty());
    int nonIdentity = 0;
    for (int index = 0; index < 65536; ++index)
    {
        if (std::abs(static_cast<int>(processing->pre_calc_curve_r[index]) - index) > 1) ++nonIdentity;
    }
    ASSERT_TRUE(nonIdentity > 1000);   /* the curve is live, not an identity table */
    gpuPreviewProcessingApplyCpuRoute(&config, processing, /*direct8Route=*/false);

    const std::vector<uint16_t> engine = fixture.renderFrame16(0, /*threads=*/1);
    const std::vector<uint16_t> debayered = fixture.renderDebayeredFrame16(0);
    ASSERT_EQ(debayered.size(), engine.size());
    assert_gpu_display_matches_production_engine("receipt_scurve_real_frame", config, debayered, engine,
                                                 fixture.width(), fixture.height());
}

struct CreativeChainCase
{
    const char * label;
    std::function<void(processingObject_t *)> apply;
    std::function<bool(const GpuPreviewProcessingConfig &)> expect;
    /* Every ported stage stacked. Each stage alone sits inside the engine budget
     * (above); stacked, the float32 shader's per-stage +-1 rounding against the
     * engine's integer/double arithmetic is amplified by the steep composed curves
     * (S-curve + gradation, then the AgX inverse matrix): measured max 27 16-bit
     * codes, 0.6% of samples above 4, and an ablation dropping any one stage still
     * leaves 6..28 (no single stage carries it; a misplaced or missing stage is
     * thousands of codes). The gate is therefore the DISPLAY domain the screen
     * shows: every sample within 1 8-bit code, plus the 16-bit max of 32. */
    bool stacked = false;
};

static void fill_test_hue_vs_curves(processingObject_t * p)
{
    for (int i = 0; i < 36000; ++i)
    {
        const double phase = 6.283185307179586 * i / 36000.0;
        p->hue_vs_hue[i] = static_cast<float>(0.15 * std::sin(phase));
        p->hue_vs_saturation[i] = static_cast<float>(0.25 * std::cos(2.0 * phase));
        p->hue_vs_luma[i] = static_cast<float>(-0.20 * std::sin(3.0 * phase));
        p->luma_vs_saturation[i] = static_cast<float>(0.30 * std::sin(0.5 * phase));
    }
    p->hue_vs_hue_used = 1;
    p->hue_vs_saturation_used = 1;
    p->hue_vs_luma_used = 1;
    p->luma_vs_saturation_used = 1;
}

static void set_test_gradation_curves(processingObject_t * p)
{
    float xs[3] = { 0.0f, 0.5f, 1.0f };
    float ysY[3] = { 0.0f, 0.60f, 1.0f };
    float ysR[3] = { 0.0f, 0.44f, 1.0f };
    float ysB[3] = { 0.0f, 0.56f, 1.0f };
    processingSetGCurve(p, 3, xs, ysY, 0);
    processingSetGCurve(p, 3, xs, ysR, 1);
    processingSetGCurve(p, 3, xs, ysB, 3);
}

/* One creative state against BOTH CPU routes: the generic 16-bit loop (always)
 * and the direct8 kernel (where the receipt is direct8-eligible; toning and hue-vs
 * are not), each with the route flag the host would set. */
static void run_creative_chain_case(const CreativeChainCase & creative)
{
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    configure_gpu_preview_supported_subset(fixture);
    processingObject_t * processing = fixture.processing();
    processingAllowCreativeAdjustments(processing);
    processingSetWhiteBalance(processing, 4500.0, 10.0);
    creative.apply(processing);
    (void)fixture.renderDebayeredFrame16(0);

    QString reason;
    ASSERT_TRUE(gpuPreviewProcessingIsSupported(processing, &reason));
    const GpuPreviewProcessingConfig config = gpuPreviewProcessingBuildConfig(processing, &reason);
    ASSERT_TRUE(config.enabled);
    ASSERT_TRUE(creative.expect(config));
    ASSERT_TRUE(gpuPreviewProcessingDisplayShaderRefusedStages(config).isEmpty());

    int width = 0;
    int height = 0;
    const std::vector<uint16_t> frame = make_synthetic_ramp_frame(processing, 130000.0, &width, &height);

    GpuPreviewProcessingConfig generic16Config = config;
    gpuPreviewProcessingApplyCpuRoute(&generic16Config, processing, /*direct8Route=*/false);
    const std::vector<uint16_t> engine16 = run_production_engine_on_frame(processing, frame, width, height);
    const std::string label16 = std::string(creative.label) + ".as16bit";
    if (!creative.stacked)
    {
        assert_gpu_display_matches_production_engine(label16.c_str(), generic16Config, frame, engine16, width, height);
    }
    else
    {
        std::vector<uint16_t> gpu16(frame.size(), 0);
        (void)render_display_for_parity(generic16Config, frame.data(), gpu16.data(), width, height);
        const frame_compare_result_t wide = compare_frames_u16(engine16.data(), gpu16.data(), width, height, 3,
                                                               kEngineParityPerSampleTolerance);
        std::vector<uint8_t> gpu8(gpu16.size());
        for (size_t index = 0; index < gpu16.size(); ++index) gpu8[index] = static_cast<uint8_t>(gpu16[index] >> 8);
        const std::vector<uint8_t> engine8 = engine16_as_8bit(engine16);
        const frame_compare_result_t display = compare_frames_u8(engine8.data(), gpu8.data(), width, height, 3, 1);
        std::cout << "[ENGINE-PARITY] " << label16 << " (stacked) 16-bit: " << frame_compare_summary(wide)
                  << " | 8-bit display: " << frame_compare_summary(display) << "\n";
        test_artifacts::record(std::string("gpu_preview_display.engine_parity.") + label16 + ".compare",
                               frame_compare_summary(wide) + " | display8 " + frame_compare_summary(display));
        ASSERT_TRUE(wide.max_abs_diff <= kEngineParityMaxAbsDiff);
        ASSERT_TRUE(display.max_abs_diff <= 1);
    }

    if (processingCanUseDirect8BitOutput(processing) != 0)
    {
        GpuPreviewProcessingConfig direct8Config = config;
        gpuPreviewProcessingApplyCpuRoute(&direct8Config, processing, /*direct8Route=*/true);
        const std::vector<uint8_t> engine8 = run_direct8_engine_on_frame(processing, frame, width, height);
        const std::string label8 = std::string(creative.label) + ".direct8";
        assert_gpu_display_matches_direct8_engine(label8.c_str(), direct8Config, frame, engine8, width, height);
    }
}

TEST(GpuPreviewProcessing, EngineAnchoredCreativeChainMatchesEngineOnBothCpuRoutes)
{
    const std::vector<CreativeChainCase> cases = {
        { "scurve_strong_lighten",
          [](processingObject_t * p) { processingSetContrast(p, 0.6, 0.45, 0.4, 0.35, 0.25); },
          [](const GpuPreviewProcessingConfig & c) { return c.applyCreativeCurves; } },
        { "gradation_y_r_b",
          [](processingObject_t * p) { set_test_gradation_curves(p); },
          [](const GpuPreviewProcessingConfig & c) { return c.applyCreativeCurves; } },
        { "saturation_up",
          [](processingObject_t * p) { processingSetSaturation(p, 1.35); },
          [](const GpuPreviewProcessingConfig & c) { return c.applySaturation; } },
        { "saturation_down",
          [](processingObject_t * p) { processingSetSaturation(p, 0.60); },
          [](const GpuPreviewProcessingConfig & c) { return c.applySaturation; } },
        { "toning",
          [](processingObject_t * p) { processingSetToning(p, 255, 160, 60, 45); },
          [](const GpuPreviewProcessingConfig & c) { return c.applyToning; } },
        { "hue_vs_curves",
          [](processingObject_t * p) { fill_test_hue_vs_curves(p); },
          [](const GpuPreviewProcessingConfig & c) { return c.applyHueVs; } },
        { "agx",
          [](processingObject_t * p) { processingEnableAgX(p); },
          [](const GpuPreviewProcessingConfig & c) { return c.applyAgx; } },
        { "agx_scurve_vibrance",
          [](processingObject_t * p) {
              processingEnableAgX(p);
              processingSetContrast(p, 0.6, 0.45, 0.4, 0.35, 0.25);
              processingSetVibrance(p, 1.2);
          },
          [](const GpuPreviewProcessingConfig & c) { return c.applyAgx && c.applyVibrance; } },
        { "every_ported_stage_night_preset",
          [](processingObject_t * p) {
              processingSetContrast(p, 0.6, 0.45, 0.4, 0.35, 0.25);
              set_test_gradation_curves(p);
              fill_test_hue_vs_curves(p);
              processingSetSaturation(p, 1.2);
              processingSetToning(p, 255, 160, 60, 30);
              processingSetSimpleContrast(p, 0.14);
              processingSetPivot(p, 0.46);
              processingSetVibrance(p, 1.03);
              processingEnableAgX(p);
          },
          [](const GpuPreviewProcessingConfig & c) {
              return c.applyCreativeCurves && c.applyHueVs && c.applySaturation && c.applyToning
                  && c.applyInLoopContrast && c.applyVibrance && c.applyAgx;
          },
          /*stacked=*/true },
    };
    for (const CreativeChainCase & creative : cases)
    {
        run_creative_chain_case(creative);
    }
}

TEST(GpuPreviewProcessing, DisplayShaderCreativeCurveCompositionIsBitExact)
{
    /* GL-free: the composed table equals the engine's three sequential lookups
     * (pre_calc_curve_r, gcurve_y, gcurve_r/g/b) for every 16-bit input. */
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    configure_gpu_preview_supported_subset(fixture);
    processingObject_t * p = fixture.processing();
    processingAllowCreativeAdjustments(p);
    processingSetContrast(p, 0.6, 0.45, 0.4, 0.35, 0.25);
    set_test_gradation_curves(p);
    QString reason;
    const GpuPreviewProcessingConfig config = gpuPreviewProcessingBuildConfig(p, &reason);
    ASSERT_TRUE(config.enabled);
    ASSERT_TRUE(config.applyCreativeCurves);

    const QByteArray composed = gpuPreviewProcessingComposeCreativeCurvesRgba16(config);
    ASSERT_EQ(static_cast<int>(65536u * 4u * sizeof(uint16_t)), composed.size());
    const uint16_t * texels = reinterpret_cast<const uint16_t *>(composed.constData());
    const uint16_t * perChannel[3] = { p->gcurve_r, p->gcurve_g, p->gcurve_b };
    int mismatches = 0;
    int offIdentity = 0;
    for (int index = 0; index < 65536; ++index)
    {
        for (int channel = 0; channel < 3; ++channel)
        {
            const uint16_t expected = perChannel[channel][p->gcurve_y[p->pre_calc_curve_r[index]]];
            if (texels[index * 4 + channel] != expected) ++mismatches;
            if (std::abs(static_cast<int>(expected) - index) > 1) ++offIdentity;
        }
        ASSERT_EQ(65535, static_cast<int>(texels[index * 4 + 3]));
    }
    ASSERT_EQ(0, mismatches);
    ASSERT_TRUE(offIdentity > 3000);

    /* empty LUTs compose as identity */
    const QByteArray identity = gpuPreviewProcessingComposeCreativeCurvesRgba16(GpuPreviewProcessingConfig());
    const uint16_t * identityTexels = reinterpret_cast<const uint16_t *>(identity.constData());
    for (int index = 0; index < 65536; index += 257)
    {
        ASSERT_EQ(index, static_cast<int>(identityTexels[index * 4 + 1]));
    }
}

TEST(GpuPreviewProcessing, DisplayShaderSourceCarriesEveryPortedStage)
{
    /* GL-free pin: the live display shader declares every ported stage's gate, and
     * the creative block runs in the engine's order (hue-vs, vibrance, saturation,
     * toning, curves, AgX inverse). */
    const QByteArray display = gpuPreviewProcessingDisplayFragmentShaderSource();
    const char * gates[] = { "previewApplyHueVs", "previewApplyVibrance", "previewApplySaturation",
                             "previewApplyToning", "previewApplyCreativeCurves", "previewApplyAgx",
                             "creativeCurveLut", "hueVsCurves" };
    for (const char * gate : gates)
    {
        ASSERT_TRUE(display.contains(gate));
    }
    const int hueVs = display.indexOf("if (previewApplyHueVs > 0.5)");
    const int vibrance = display.indexOf("if (previewApplyVibrance > 0.5)");
    const int saturation = display.indexOf("if (previewApplySaturation > 0.5)");
    const int toning = display.indexOf("if (previewApplyToning > 0.5)");
    const int curves = display.indexOf("if (previewApplyCreativeCurves > 0.5)");
    const int agxInverse = display.lastIndexOf("if (previewApplyAgx > 0.5)");
    const int agxForward = display.indexOf("if (previewApplyAgx > 0.5)");
    const int gamma = display.indexOf("sampleU16LutIndex(gammaLut");
    ASSERT_TRUE(agxForward > 0 && agxForward < gamma);
    ASSERT_TRUE(gamma < hueVs && hueVs < vibrance && vibrance < saturation && saturation < toning
                && toning < curves && curves < agxInverse);
}

TEST(GpuPreviewProcessing, DisplayShaderRefusalPredicateMatchesConfigFlags)
{
    /* GL-free. The processing-object predicate is what the per-frame policy calls;
     * the config predicate reads the config's own flags. Every state below is
     * accepted by the subset gate (so the texture route would reach it), and the two
     * predicates must name the same stages. Ported stages are never refused. */
    MlvPipelineFixture fixture;
    assert_gpu_preview_fixture_ready(fixture);
    static uint16_t dummyGradientMask[16] = { 0 };

    struct RefusalState
    {
        const char * label;
        std::function<void(MlvPipelineFixture &)> apply;
        const char * expected; /* comma list; "" = not refused */
    };
    const std::vector<RefusalState> states = {
        { "neutral", [](MlvPipelineFixture &) {}, "" },
        { "ported_creative_chain", [](MlvPipelineFixture & f) {
              processingObject_t * p = f.processing();
              processingAllowCreativeAdjustments(p);
              processingSetContrast(p, 0.6, 0.45, 0.4, 0.35, 0.25);
              set_test_gradation_curves(p);
              fill_test_hue_vs_curves(p);
              processingSetSaturation(p, 1.2);
              processingSetToning(p, 255, 160, 60, 30);
              processingSetVibrance(p, 1.2);
              processingSetSimpleContrast(p, 0.14);
              processingSetShadows(p, 0.32);
              processingEnableAgX(p);
          }, "" },
        { "vignette", [](MlvPipelineFixture & f) {
              processingSetVignetteMask(f.processing(), static_cast<uint16_t>(f.width()),
                                        static_cast<uint16_t>(f.height()), 0.5f, 0.2f, 1.0f, 1.4f);
              processingSetVignetteStrength(f.processing(), 60);
          }, "vignette" },
        { "highlight_reconstruction", [](MlvPipelineFixture & f) { f.processing()->highlight_reconstruction = 1; },
          "highlight_reconstruction" },
        { "gradient", [](MlvPipelineFixture & f) {
              f.processing()->gradient_enable = 1;
              f.processing()->gradient_exposure_stops = 0.5;
              f.processing()->gradient_mask = dummyGradientMask;
          }, "gradient" },
        { "lut_3d", [](MlvPipelineFixture & f) { install_test_lut(f, 17, true); }, "lut" },
        { "chroma_separation", [](MlvPipelineFixture & f) { f.processing()->cs_zone.use_cs = 1; },
          "chroma_separation" },
        { "sharpen", [](MlvPipelineFixture & f) { processingSetSharpening(f.processing(), 0.5); }, "sharpen" },
        { "median", [](MlvPipelineFixture & f) {
              f.processing()->denoiserStrength = 50;
              f.processing()->denoiserWindow = 3;
          }, "median_denoise" },
        { "lut_and_sharpen", [](MlvPipelineFixture & f) {
              install_test_lut(f, 17, true);
              processingSetSharpening(f.processing(), 0.5);
          }, "lut,sharpen" },
    };

    for (const RefusalState & state : states)
    {
        configure_gpu_preview_supported_subset(fixture);
        processingObject_t * p = fixture.processing();
        p->lut_on = 0;
        processingSetVignetteStrength(p, 0);
        p->gradient_mask = nullptr;
        p->gradient_enable = 0;
        state.apply(fixture);

        const std::string label(state.label);
        QString reason;
        if (!gpuPreviewProcessingIsSupported(p, &reason))
        {
            ::minitest::fail(__FILE__, __LINE__, "subset gate refused " + label, reason.toStdString());
        }
        const GpuPreviewProcessingConfig config = gpuPreviewProcessingBuildConfig(p, &reason);
        ASSERT_TRUE(config.enabled);
        const QString fromProcessing = gpuPreviewProcessingDisplayShaderRefusedStages(p).join(QLatin1Char(','));
        const QString fromConfig = gpuPreviewProcessingDisplayShaderRefusedStages(config).join(QLatin1Char(','));
        std::cout << "[DISPLAY-REFUSAL] " << label << " processing=" << fromProcessing.toStdString()
                  << " config=" << fromConfig.toStdString() << "\n";
        if (fromProcessing != QString::fromLatin1(state.expected) || fromConfig != fromProcessing)
        {
            ::minitest::fail(__FILE__, __LINE__, "display-shader refusal of " + label,
                             "expected=" + std::string(state.expected) + " processing=" + fromProcessing.toStdString()
                             + " config=" + fromConfig.toStdString());
        }
        const QString typed = gpuPreviewProcessingDisplayShaderRefusalReason(
            gpuPreviewProcessingDisplayShaderRefusedStages(config));
        if (fromConfig.isEmpty())
        {
            ASSERT_TRUE(typed.isEmpty());
        }
        else
        {
            ASSERT_TRUE(typed == QStringLiteral("display_shader_refused_stages=") + fromConfig);
            /* the display harness refuses it too, before any GL work */
            std::vector<uint16_t> in(16 * 16 * 3, 1000);
            std::vector<uint16_t> out(in.size(), 0);
            QString harnessReason;
            ASSERT_TRUE(!gpuPreviewProcessingApplyDisplayGpuOffscreen(config, in.data(), out.data(), 16, 16,
                                                                      &harnessReason));
            ASSERT_TRUE(harnessReason == typed);
        }
        p->gradient_mask = nullptr;
        p->gradient_enable = 0;
        p->gradient_exposure_stops = 0.0;
    }
}

TEST(GpuPreviewProcessing, PlaybackTextureRouteRefusalIsWiredIntoThePolicy)
{
    /* Source pins (GL-free): the texture-present routes are refused through the
     * policy state, the scale-one clamp sees the same refusal, the fallback reason
     * carries the typed token, and the viewport's RGB16 display-shader present is
     * skipped for a refused stage. */
    QFile policyFile(QStringLiteral("platform/qt/MainWindowGpuPreviewPolicy.h"));
    ASSERT_TRUE(policyFile.open(QIODevice::ReadOnly));
    const QString policy = QString::fromUtf8(policyFile.readAll());
    auto functionBody = [&policy](const QString & signature) -> QString
    {
        const int start = policy.indexOf(signature);
        if (start < 0) return QString();
        const int end = policy.indexOf(QStringLiteral("\n}"), start);
        return end < 0 ? QString() : policy.mid(start, end - start);
    };
    ASSERT_TRUE(functionBody(QStringLiteral("inline bool mainWindowAllowsGpuPlaybackReconTexturePresentation("))
                    .contains(QStringLiteral("state.gpuPreviewProcessingDisplayShaderCompatible")));
    ASSERT_TRUE(functionBody(QStringLiteral("inline bool mainWindowAllowsGpuAmazeTexturePresentation("))
                    .contains(QStringLiteral("state.gpuPreviewProcessingDisplayShaderCompatible")));

    QFile mainWindowFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    ASSERT_TRUE(mainWindowFile.open(QIODevice::ReadOnly));
    const QString mainWindow = QString::fromUtf8(mainWindowFile.readAll());
    ASSERT_TRUE(mainWindow.contains(QStringLiteral(
        "renderPolicy.gpuPreviewProcessingDisplayShaderCompatible = displayShaderRefusedStages.isEmpty();")));
    ASSERT_TRUE(mainWindow.contains(QStringLiteral(
        "gpuPreviewProcessingDisplayShaderRefusalReason( displayShaderRefusedStages )")));
    ASSERT_TRUE(mainWindow.contains(QStringLiteral(
        "&& gpuPreviewProcessingDisplayShaderRefusedStages( m_pProcessingObject ).isEmpty(),")));
    ASSERT_TRUE(mainWindow.contains(QStringLiteral("QStringLiteral(\"gpu_display_shader_refused_stages\")")));
    /* the generic RGB16 present (the other call site is inside the AMaZE texture
     * route, which the policy flag above already refuses) */
    const int rgb16Present = mainWindow.lastIndexOf(QStringLiteral("GpuDisplayViewport::presentRgb16( ui->graphicsView,"));
    ASSERT_TRUE(rgb16Present > 0);
    ASSERT_TRUE(mainWindow.mid(rgb16Present - 200, 200).contains(QStringLiteral("presentDisplayShaderRefusedStages.isEmpty()")));
}
