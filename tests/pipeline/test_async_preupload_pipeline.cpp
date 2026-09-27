#include "../common/minitest.h"
#include "mlv_pipeline_fixture.h"
#include "../../src/mlv/llrawproc/llrawproc.h"
#include "../../src/mlv/pipeline_stage_capture.h"
#include <cstdint>
#include <vector>
#include <QByteArray>

static void assert_fixture_ready(MlvPipelineFixture & fixture)
{
    QString error_message;
    ASSERT_TRUE(fixture.openTinyDualIso(&error_message));
    ASSERT_TRUE(fixture.loadReceipt(QStringLiteral("tests/fixtures/receipts/tiny_dual_iso_hq.marxml"), &error_message));
    ASSERT_TRUE(fixture.applyReceipt(&error_message));
}

struct GpuExportDualIsoConfig {
    int interp;   // DISOI_MEAN23 (GPU-eligible) or DISOI_AMAZE (ineligible)
    int alias;    // FR_OFF / FR_ON
    int fullres;  // FR_OFF / FR_ON
    int chroma;   // CS_OFF (GPU-eligible) / CS_2x2 / CS_3x3 / CS_5x5 (ineligible)
};

static const GpuExportDualIsoConfig kGpuExportSupportedDualIsoConfig = {
    DISOI_MEAN23, FR_ON, FR_ON, CS_OFF
};

static const GpuExportDualIsoConfig kGpuPlaybackChromaDualIsoConfig = {
    DISOI_MEAN23, FR_ON, FR_ON, CS_2x2
};

static void configure_gpu_export_dual_iso(MlvPipelineFixture & fixture,
                                          const GpuExportDualIsoConfig & cfg)
{
    llrpSetDualIsoInterpolationMethod(fixture.video(), cfg.interp);
    llrpSetDualIsoAliasMapMode(fixture.video(), cfg.alias);
    llrpSetDualIsoFullResBlendingMode(fixture.video(), cfg.fullres);
    llrpSetChromaSmoothMode(fixture.video(), cfg.chroma);
}

static void configure_gpu_export_supported_dual_iso(MlvPipelineFixture & fixture)
{
    configure_gpu_export_dual_iso(fixture, kGpuExportSupportedDualIsoConfig);
}

class GpuPlaybackReconThreadOptIn
{
public:
    explicit GpuPlaybackReconThreadOptIn(bool enabled)
    {
        llrpSetGpuPlaybackReconAllowedForCurrentThread(enabled ? 1 : 0);
    }

    ~GpuPlaybackReconThreadOptIn()
    {
        llrpSetGpuPlaybackReconAllowedForCurrentThread(0);
    }
};

class GpuPlaybackReconTexturePresentOptIn
{
public:
    explicit GpuPlaybackReconTexturePresentOptIn(bool enabled)
    {
        llrpSetGpuPlaybackReconTexturePresentPreferredForCurrentThread(
            enabled ? 1 : 0);
    }

    ~GpuPlaybackReconTexturePresentOptIn()
    {
        llrpSetGpuPlaybackReconTexturePresentPreferredForCurrentThread(0);
    }
};

class GpuPlaybackReconTexturePrepareOnlyOptIn
{
public:
    explicit GpuPlaybackReconTexturePrepareOnlyOptIn(bool enabled)
    {
        llrpSetGpuPlaybackReconTexturePrepareOnlyForCurrentThread(
            enabled ? 1 : 0);
    }

    ~GpuPlaybackReconTexturePrepareOnlyOptIn()
    {
        llrpSetGpuPlaybackReconTexturePrepareOnlyForCurrentThread(0);
    }
};

namespace {
struct PreuploadObservation {
    int calls = 0;
    uint64_t frame = UINT64_MAX;
    std::vector<uint16_t> input;
};
thread_local PreuploadObservation *preuploadObservation = nullptr;

void observePreparedPreupload(uint64_t frame, const uint16_t *input, size_t bytes)
{
    ++preuploadObservation->calls;
    preuploadObservation->frame = frame;
    preuploadObservation->input.assign(input, input + bytes / sizeof(uint16_t));
}

class PreparedPreuploadFixtureScope {
    std::vector<std::pair<QByteArray, QByteArray>> saved;
public:
    PreparedPreuploadFixtureScope(PreuploadObservation *observation, bool gpu, bool async)
    {
        for (const auto &setting : std::vector<std::pair<QByteArray, QByteArray>>{
                 {"MLVAPP_GPU_EXPORT", "0"},
                 {"MLVAPP_GPU_PLAYBACK_RECON", gpu ? "1" : "0"},
                 {"MLVAPP_GPU_PLAYBACK_RECON_ASYNC_H2D", async ? "1" : "0"},
                 {"MLVAPP_GPU_PLAYBACK_RECON_VALIDATE_OUTPUT", "0"},
                 {"MLVAPP_GPU_PLAYBACK_RECON_RETAIN_DEVICE_OUTPUT", "0"}})
        {
            saved.push_back({setting.first, qgetenv(setting.first.constData())});
            qputenv(setting.first.constData(), setting.second);
        }
        preuploadObservation = observation;
        llrpSetGpuPlaybackPreuploadObserverForTesting(observePreparedPreupload);
    }
    ~PreparedPreuploadFixtureScope()
    {
        llrpSetGpuPlaybackPreuploadObserverForTesting(nullptr);
        preuploadObservation = nullptr;
        for (const auto &setting : saved) {
            if (setting.second.isNull()) qunsetenv(setting.first.constData());
            else qputenv(setting.first.constData(), setting.second);
        }
    }
};
}

TEST(DualIsoPipeline, AsyncPreuploadFrameTokenIncludesFirstFrameWithoutOverflow)
{
    ASSERT_EQ(uint64_t(1), llrpGpuPlaybackReconFrameToken(0));
    ASSERT_EQ(uint64_t(2), llrpGpuPlaybackReconFrameToken(1));
    ASSERT_EQ(uint64_t(UINT32_MAX) + 1u, llrpGpuPlaybackReconFrameToken(UINT32_MAX));
    ASSERT_EQ(UINT64_MAX, llrpGpuPlaybackReconFrameToken(UINT64_MAX - 1u));
    ASSERT_EQ(uint64_t(0), llrpGpuPlaybackReconFrameToken(UINT64_MAX));
}

TEST(DualIsoPipeline, AsyncPreuploadStatusSurvivesBothDisplayTimingAdapters)
{
    llrpGpuPlaybackReconTiming_t recon = {};
    recon.upload_ms = 1.0;
    recon.kernel_ms = 2.0;
    recon.interop_ms = 3.0;
    recon.total_ms = 6.0;
    recon.preupload = {1, 1, 1, 1, 1, 1, 1.25, 2.5, 3.75};
    for (int reconAvailable : {0, 1})
    {
        recon.available = reconAvailable;
        const auto timing = llrpGpuPlaybackReconCombineTiming(&recon, 1, 10, 20, 30, 60);
        ASSERT_EQ(1, timing.available);
        ASSERT_EQ(reconAvailable ? 11.0 : 10.0, timing.upload_ms);
        ASSERT_EQ(reconAvailable ? 22.0 : 20.0, timing.kernel_ms);
        ASSERT_EQ(reconAvailable ? 33.0 : 30.0, timing.interop_ms);
        ASSERT_EQ(reconAvailable ? 66.0 : 60.0, timing.total_ms);
        ASSERT_EQ(1, timing.preupload.available);
        ASSERT_EQ(1, timing.preupload.accepted);
        ASSERT_EQ(1, timing.preupload.used);
        ASSERT_EQ(1, timing.preupload.exact_match);
        ASSERT_EQ(1, timing.preupload.submitted_while_prior_run_active);
        ASSERT_EQ(1, timing.preupload.ready_before_run);
        ASSERT_EQ(1.25, timing.preupload.host_staging_ms);
        ASSERT_EQ(2.5, timing.preupload.upload_ms);
        ASSERT_EQ(3.75, timing.preupload.upload_wait_ms);
        ASSERT_EQ(0.0, timing.wall_ms);
    }
    // A retained-device handoff does no reconstruction here. Neither unavailable
    // timers nor the previous invocation's preupload status can leak into it.
    recon = {};
    auto empty = llrpGpuPlaybackReconCombineTiming(&recon, 0, 10, 20, 30, 60);
    ASSERT_EQ(0, empty.available);
    ASSERT_EQ(0.0, empty.total_ms);
    ASSERT_EQ(0, empty.preupload.available);
    ASSERT_EQ(0, empty.preupload.accepted);
    ASSERT_EQ(0, empty.preupload.used);
    ASSERT_EQ(0, empty.preupload.exact_match);
    ASSERT_EQ(0, empty.preupload.submitted_while_prior_run_active);
    ASSERT_EQ(0, empty.preupload.ready_before_run);
    ASSERT_EQ(0.0, empty.preupload.host_staging_ms);
    ASSERT_EQ(0.0, empty.preupload.upload_ms);
    ASSERT_EQ(0.0, empty.preupload.upload_wait_ms);
}

TEST(DualIsoPipeline, AsyncPreuploadStagesPreparedPixelsAfterBitExpansion)
{
    for (uint64_t frameIndex : {uint64_t(0), uint64_t(1)})
    {
        MlvPipelineFixture fixture;
        assert_fixture_ready(fixture);
        configure_gpu_export_supported_dual_iso(fixture);
        auto *video = fixture.video();
        llrpSetDualIsoInterpolationMethod(video, DISOI_MEAN23);
        video->llrawproc->focus_pixels = 0;
        video->llrawproc->bad_pixels = 0;
        video->llrawproc->vertical_stripes = 0;
        std::vector<uint16_t> raw(size_t(fixture.width()) * size_t(fixture.height()));
        ASSERT_EQ(0, getMlvRawFrameUint16(video, frameIndex, raw.data()));
        for (auto &pixel : raw) pixel >>= 2;
        const auto decoded12 = raw;
        // This synthetic unpacked frame contains exactly the decoded active area.
        video->RAWI.raw_info.width = fixture.width();
        video->RAWI.raw_info.height = fixture.height();
        video->RAWI.raw_info.bits_per_pixel = 12;
        video->RAWI.raw_info.black_level >>= 2;
        video->RAWI.raw_info.white_level >>= 2;
        // Decode is complete; exclude the independent compressed-range transform
        // so this fixture isolates actual 12-to-14-bit preparation.
        video->MLVI.videoClass &= ~MLV_VIDEO_CLASS_FLAG_LJ92;
        PreuploadObservation observation;
        const PreparedPreuploadFixtureScope observer(&observation, true, true);
        const GpuPlaybackReconThreadOptIn optIn(true);
        const GpuPlaybackReconTexturePresentOptIn texturePresent(true);
        const GpuPlaybackReconTexturePrepareOnlyOptIn prepareOnly(true);
        mlv_pipeline_capture_set_current_frame(frameIndex);
        applyLLRawProcObjectWorker(video, raw.data(), raw.size() * sizeof(uint16_t), nullptr, 0);
        ASSERT_EQ(1, llrpGpuPlaybackReconLastPrepareOnlyForTesting());
        ASSERT_EQ(1, observation.calls);
        ASSERT_EQ(frameIndex, observation.frame);
        ASSERT_EQ(raw.size(), observation.input.size());
        ASSERT_TRUE(observation.input == raw);
        ASSERT_TRUE(observation.input != decoded12);
        auto expanded14 = decoded12;
        for (auto &pixel : expanded14) pixel <<= 2;
        ASSERT_TRUE(observation.input == expanded14);
    }
}

TEST(DualIsoPipeline, AsyncPreuploadDoesNotStageDisabledOrIneligibleFrames)
{
    for (int disabledGate : {0, 1, 2})
    {
        MlvPipelineFixture fixture;
        assert_fixture_ready(fixture);
        configure_gpu_export_supported_dual_iso(fixture);
        llrpSetDualIsoInterpolationMethod(fixture.video(), disabledGate == 2 ? DISOI_AMAZE : DISOI_MEAN23);
        PreuploadObservation observation;
        const PreparedPreuploadFixtureScope observer(&observation, disabledGate != 0, disabledGate != 1);
        const GpuPlaybackReconThreadOptIn optIn(true);
        const GpuPlaybackReconTexturePresentOptIn texturePresent(true);
        const GpuPlaybackReconTexturePrepareOnlyOptIn prepareOnly(true);
        const auto frame = fixture.renderFrame16(0, 1);
        ASSERT_TRUE(!frame.empty());
        ASSERT_EQ(0, observation.calls);
    }
}
