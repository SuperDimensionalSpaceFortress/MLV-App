#include "../common/minitest.h"
#include "mlv_pipeline_fixture.h"
#include "../../src/mlv/llrawproc/llrawproc.h"
#include "../../src/mlv/pipeline_stage_capture.h"
#include <cstdint>
#include <cstring>
#include <vector>
#include <QByteArray>

extern "C" int llrpInstallFakeGpuPlaybackReconBackendForTesting(int install);
extern "C" int llrpResetGpuExportBackendForTesting(void);
extern "C" int llrpGpuPlaybackReconLastPreuploadStatusForTesting(
    llrpGpuPlaybackReconPreuploadStatus_t * status);

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

// Consume-side proof for MainWindow's compare-and-reject (blocker fixed this
// round): MainWindow.cpp's presentation path no longer reimplements this
// decision inline -- it calls this same shared, header-only function, and
// GpuDisplayViewport/Window's retainedDeviceValid gate (also fixed this
// round) keys off exactly llrpGpuPlaybackReconFrameToken() of ITS result.
// tests/gui/* (the only harness that can instantiate a real
// GpuDisplayViewport/Window to drive that gate end to end) is out of scope
// for this card (excluded path), so this proves the decision function and
// its "unarmed" consequence directly instead.
TEST(DualIsoPipeline, AsyncPreuploadFrameIdCompareAndRejectMatchesLeavesFrameIdArmed)
{
    const uint64_t result =
        llrpGpuPlaybackReconFrameIdAfterCompareAndReject(41, 41);
    ASSERT_EQ(uint64_t(41), result);
    ASSERT_NE(uint64_t(0), llrpGpuPlaybackReconFrameToken(result));
}

TEST(DualIsoPipeline, AsyncPreuploadFrameIdCompareAndRejectMismatchDisarmsFrameId)
{
    // The exact MainWindow scenario sol's pre-review repro'd: a retained
    // buffer for frame 41 paired with a display task for frame 42.
    const uint64_t result =
        llrpGpuPlaybackReconFrameIdAfterCompareAndReject(41, 42);
    ASSERT_EQ(UINT64_MAX, result);
    // This is exactly the condition GpuDisplayViewport/Window's
    // retainedDeviceValid now checks: a disarmed frame_id must map to
    // token 0, so the retained buffer is refused on the presentation path,
    // not only on llrawproc_gpu_recon_run_backend()'s token gate.
    ASSERT_EQ(uint64_t(0), llrpGpuPlaybackReconFrameToken(result));
}

// Sol pre-review #2 hardening: GpuDisplayViewport.cpp (both its public entry
// point and the internal submit function it delegates to) and
// GpuDisplayWindow.cpp used to each reimplement retainedDeviceValid inline,
// so removing the frame-token clause from any one copy had no automated
// test -- tests/gui/* is the only harness that can instantiate a real
// adapter and is an excluded path for this card, and these pure-function
// compare-and-reject tests never invoked an adapter. All three call sites
// now delegate to this one shared, header-only predicate
// (llrpGpuPlaybackReconRetainedDeviceBufferValid(), llrawproc.h), so this
// test is a direct adapter-level regression test for the retained-buffer
// mismatch without needing a GUI harness: it exercises the exact function
// both adapters call, not a re-encoding of their logic.
TEST(DualIsoPipeline, RetainedDeviceBufferValidRejectsFrameIdMismatch)
{
    const uint16_t retainedBayer16[4] = { 1024, 2048, 3072, 4096 };

    // The exact MainWindow scenario sol's pre-review repro'd: a retained
    // buffer built for frame 41, now paired with a display task for frame 42.
    const uint64_t mismatchedFrameId =
        llrpGpuPlaybackReconFrameIdAfterCompareAndReject(41, 42);
    ASSERT_EQ(UINT64_MAX, mismatchedFrameId);
    ASSERT_FALSE(llrpGpuPlaybackReconRetainedDeviceBufferValid(
        retainedBayer16, 2, 2, /*expected*/ 2, 2, /*validationProbe*/ 0,
        mismatchedFrameId));

    // A matching frame_id, otherwise identical inputs, must be accepted.
    const uint64_t armedFrameId =
        llrpGpuPlaybackReconFrameIdAfterCompareAndReject(42, 42);
    ASSERT_EQ(uint64_t(42), armedFrameId);
    ASSERT_TRUE(llrpGpuPlaybackReconRetainedDeviceBufferValid(
        retainedBayer16, 2, 2, 2, 2, 0, armedFrameId));

    // Every other clause still gates independently of frame_id.
    ASSERT_FALSE(llrpGpuPlaybackReconRetainedDeviceBufferValid(
        nullptr, 2, 2, 2, 2, 0, armedFrameId));            // no retained buffer
    ASSERT_FALSE(llrpGpuPlaybackReconRetainedDeviceBufferValid(
        retainedBayer16, 3, 2, 2, 2, 0, armedFrameId));    // width mismatch
    ASSERT_FALSE(llrpGpuPlaybackReconRetainedDeviceBufferValid(
        retainedBayer16, 2, 3, 2, 2, 0, armedFrameId));    // height mismatch
    ASSERT_FALSE(llrpGpuPlaybackReconRetainedDeviceBufferValid(
        retainedBayer16, 2, 2, 2, 2, 1, armedFrameId));    // validation probe texture
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

// ---- Executable consume-side proof (GPU-less board): an in-process fake
// implementation of the igpu_recon_backend C seam, installed in place of a
// real DLL-loaded backend. This board has no NVIDIA GPU, so it cannot prove
// real CUDA slot matching or pixel parity (see round summary,
// GPU-UNPROVEN); it DOES exercise the exact token match/mismatch/fallback
// decision in llrawproc_gpu_recon_run_backend() -- the C-side logic under
// this card's scope -- for every public consuming entry point.

namespace {
class FakeGpuBackendScope {
public:
    FakeGpuBackendScope()
    {
        llrpInstallFakeGpuPlaybackReconBackendForTesting(1);
    }
    ~FakeGpuBackendScope()
    {
        llrpInstallFakeGpuPlaybackReconBackendForTesting(0);
        llrpResetGpuExportBackendForTesting();
    }
};

class AsyncH2dEnvScope {
    QByteArray saved;
    bool hadSaved;
public:
    explicit AsyncH2dEnvScope(bool enabled)
    {
        hadSaved = qEnvironmentVariableIsSet("MLVAPP_GPU_PLAYBACK_RECON_ASYNC_H2D");
        if (hadSaved) saved = qgetenv("MLVAPP_GPU_PLAYBACK_RECON_ASYNC_H2D");
        qputenv("MLVAPP_GPU_PLAYBACK_RECON_ASYNC_H2D", enabled ? "1" : "0");
    }
    ~AsyncH2dEnvScope()
    {
        if (hadSaved) qputenv("MLVAPP_GPU_PLAYBACK_RECON_ASYNC_H2D", saved);
        else qunsetenv("MLVAPP_GPU_PLAYBACK_RECON_ASYNC_H2D");
    }
};

llrpGpuPlaybackReconState_t makeValidatedFakeBackendState(uint64_t frameId,
                                                          int width,
                                                          int height)
{
    static int dummy_int_lut[1] = { 0 };
    static double dummy_double_lut[1] = { 0.0 };
    llrpGpuPlaybackReconState_t state = {};
    state.valid = 1;
    state.width = width;
    state.height = height;
    state.black_level = 0;
    state.white_level = 65535;
    state.white_darkened = 65535;
    state.black_delta = 0;
    state.ev_correction = 0.0;
    state.dark_noise = 0.0;
    state.interp_method = 1;    // AMaZE: the only admitted no-readback class
    state.use_alias_map = 1;
    state.use_fullres = 1;
    state.chroma_smooth_method = 0;
    state.raw2ev = dummy_int_lut;
    state.ev2raw = dummy_int_lut;
    state.mix_curve = dummy_double_lut;
    state.fullres_curve = dummy_double_lut;
    state.frame_id = frameId;
    return state;
}

std::vector<uint16_t> makeBayer14(int width, int height, uint16_t base)
{
    std::vector<uint16_t> pixels(size_t(width) * size_t(height));
    for (size_t i = 0; i < pixels.size(); ++i) pixels[i] = uint16_t(base + i);
    return pixels;
}
}

TEST(DualIsoPipeline, AsyncPreuploadCpu16ProbeConsumesMatchingFrameAndBytes)
{
    const FakeGpuBackendScope fakeBackend;
    const AsyncH2dEnvScope asyncEnv(true);
    const int width = 4, height = 4;
    auto state = makeValidatedFakeBackendState(7, width, height);
    auto bytesA = makeBayer14(width, height, 100);
    std::vector<uint16_t> output(bytesA.size());
    int rc = -1;
    llrpGpuPlaybackReconTiming_t timing = {};

    // Warm up: g->clip_configured must already be true before a preupload can
    // be accepted (llrpGpuPlaybackReconPreuploadFrame() checks it) -- exactly
    // as it would be in production after the first frame's recon call.
    ASSERT_NE(0, llrpGpuPlaybackReconRunCpu16Probe(&state, bytesA.data(),
        bytesA.size() * sizeof(uint16_t), output.data(), &rc, &timing));

    ASSERT_NE(0, llrpGpuPlaybackReconPreuploadFrame(7, bytesA.data(),
        bytesA.size() * sizeof(uint16_t)));
    ASSERT_NE(0, llrpGpuPlaybackReconRunCpu16Probe(&state, bytesA.data(),
        bytesA.size() * sizeof(uint16_t), output.data(), &rc, &timing));
    ASSERT_EQ(0, rc);
    ASSERT_NE(0, timing.preupload.available);
    ASSERT_NE(0, timing.preupload.accepted);
    ASSERT_NE(0, timing.preupload.used);
    ASSERT_NE(0, timing.preupload.exact_match);
    for (size_t i = 0; i < bytesA.size(); ++i)
    {
        ASSERT_EQ(uint16_t(bytesA[i] + 1u), output[i]);
    }
}

TEST(DualIsoPipeline, AsyncPreuploadRejectsCrossFrameTokenAndFallsBackCorrectly)
{
    const FakeGpuBackendScope fakeBackend;
    const AsyncH2dEnvScope asyncEnv(true);
    const int width = 4, height = 4;
    auto stateN = makeValidatedFakeBackendState(41, width, height);
    auto stateM = makeValidatedFakeBackendState(42, width, height);
    auto bytesA = makeBayer14(width, height, 200);
    auto bytesB = makeBayer14(width, height, 300);
    std::vector<uint16_t> outputPreuploaded(bytesA.size());
    std::vector<uint16_t> outputSync(bytesB.size());
    int rc = -1;
    llrpGpuPlaybackReconTiming_t timing = {};

    ASSERT_NE(0, llrpGpuPlaybackReconRunCpu16Probe(&stateN, bytesA.data(),
        bytesA.size() * sizeof(uint16_t), outputPreuploaded.data(), &rc, &timing));

    // Stage frame 41's bytes, then run frame 42 with DIFFERENT live bytes.
    ASSERT_NE(0, llrpGpuPlaybackReconPreuploadFrame(41, bytesA.data(),
        bytesA.size() * sizeof(uint16_t)));
    ASSERT_NE(0, llrpGpuPlaybackReconRunCpu16Probe(&stateM, bytesB.data(),
        bytesB.size() * sizeof(uint16_t), outputPreuploaded.data(), &rc, &timing));
    ASSERT_EQ(0, rc);
    ASSERT_EQ(0, timing.preupload.accepted);
    ASSERT_EQ(0, timing.preupload.used);
    ASSERT_EQ(0, timing.preupload.exact_match);

    // Fallback output must equal the plain synchronous path run directly on
    // the same (mismatched-frame) live bytes -- a real backend's documented
    // "otherwise it executes the ordinary synchronous upload path".
    ASSERT_NE(0, llrpGpuPlaybackReconRunCpu16Probe(&stateM, bytesB.data(),
        bytesB.size() * sizeof(uint16_t), outputSync.data(), &rc, &timing));
    ASSERT_TRUE(outputPreuploaded == outputSync);
}

TEST(DualIsoPipeline, AsyncPreuploadUnarmedFallbackDoesNotLeakPriorSuccessfulStatus)
{
    // Blocker fixed this round: MainWindow's compare-and-reject disarms a
    // mismatched frame's state.frame_id to UINT64_MAX (token 0), which takes
    // the plain g->run() path in llrawproc_gpu_recon_run_backend() -- NOT
    // g->run_preuploaded(). Querying g->last_preupload_status() after a
    // plain run() would report a PRIOR frame's successful preupload as this
    // (unarmed) frame's status; llrawproc.c must scope that query to only
    // the call that actually used run_preuploaded().
    const FakeGpuBackendScope fakeBackend;
    const AsyncH2dEnvScope asyncEnv(true);
    const int width = 4, height = 4;
    auto stateArmed = makeValidatedFakeBackendState(5, width, height);
    auto stateUnarmed = makeValidatedFakeBackendState(UINT64_MAX, width, height);
    auto bytesA = makeBayer14(width, height, 40);
    auto bytesB = makeBayer14(width, height, 50);
    std::vector<uint16_t> output(bytesA.size());
    int rc = -1;
    llrpGpuPlaybackReconTiming_t timing = {};

    // Warm up: g->clip_configured must already be true before a preupload can
    // be accepted (llrpGpuPlaybackReconPreuploadFrame() checks it) -- exactly
    // as it would be in production after the first frame's recon call.
    ASSERT_NE(0, llrpGpuPlaybackReconRunCpu16Probe(&stateArmed, bytesA.data(),
        bytesA.size() * sizeof(uint16_t), output.data(), &rc, &timing));

    // A prior frame's preupload is staged, matched and CONSUMED: used=1,
    // accepted=1, exact_match=1 all land in g_llrawproc_fake_gpu_backend_last_status.
    ASSERT_NE(0, llrpGpuPlaybackReconPreuploadFrame(5, bytesA.data(),
        bytesA.size() * sizeof(uint16_t)));
    ASSERT_NE(0, llrpGpuPlaybackReconRunCpu16Probe(&stateArmed, bytesA.data(),
        bytesA.size() * sizeof(uint16_t), output.data(), &rc, &timing));
    ASSERT_EQ(0, rc);
    ASSERT_NE(0, timing.preupload.used);
    ASSERT_NE(0, timing.preupload.exact_match);

    // Now an unarmed (mismatch-disarmed) frame runs with NO preupload staged
    // for it at all. This must not report the previous call's used/exact_match.
    ASSERT_NE(0, llrpGpuPlaybackReconRunCpu16Probe(&stateUnarmed, bytesB.data(),
        bytesB.size() * sizeof(uint16_t), output.data(), &rc, &timing));
    ASSERT_EQ(0, rc);
    ASSERT_EQ(0, timing.preupload.available);
    ASSERT_EQ(0, timing.preupload.accepted);
    ASSERT_EQ(0, timing.preupload.used);
    ASSERT_EQ(0, timing.preupload.exact_match);
}

TEST(DualIsoPipeline, AsyncPreuploadRejectsByteMismatchWithSameFrameAndFallsBackCorrectly)
{
    const FakeGpuBackendScope fakeBackend;
    const AsyncH2dEnvScope asyncEnv(true);
    const int width = 4, height = 4;
    auto state = makeValidatedFakeBackendState(9, width, height);
    auto bytesA = makeBayer14(width, height, 10);
    auto bytesB = makeBayer14(width, height, 20);
    std::vector<uint16_t> outputPreuploaded(bytesA.size());
    std::vector<uint16_t> outputSync(bytesB.size());
    int rc = -1;
    llrpGpuPlaybackReconTiming_t timing = {};

    ASSERT_NE(0, llrpGpuPlaybackReconRunCpu16Probe(&state, bytesA.data(),
        bytesA.size() * sizeof(uint16_t), outputPreuploaded.data(), &rc, &timing));

    // Stage frame 9 with bytesA, but the decoded input changed by the time
    // recon actually runs (bytesB) -- same frame token, different bytes.
    ASSERT_NE(0, llrpGpuPlaybackReconPreuploadFrame(9, bytesA.data(),
        bytesA.size() * sizeof(uint16_t)));
    ASSERT_NE(0, llrpGpuPlaybackReconRunCpu16Probe(&state, bytesB.data(),
        bytesB.size() * sizeof(uint16_t), outputPreuploaded.data(), &rc, &timing));
    ASSERT_EQ(0, rc);
    ASSERT_NE(0, timing.preupload.accepted);   // token matched...
    ASSERT_EQ(0, timing.preupload.used);       // ...but bytes did not
    ASSERT_EQ(0, timing.preupload.exact_match);

    ASSERT_NE(0, llrpGpuPlaybackReconRunCpu16Probe(&state, bytesB.data(),
        bytesB.size() * sizeof(uint16_t), outputSync.data(), &rc, &timing));
    ASSERT_TRUE(outputPreuploaded == outputSync);
}

TEST(DualIsoPipeline, AsyncPreuploadFrameZeroIsArmedAcrossAllConsumingEntryPoints)
{
    const FakeGpuBackendScope fakeBackend;
    const AsyncH2dEnvScope asyncEnv(true);
    const int width = 2, height = 2;
    auto state = makeValidatedFakeBackendState(0, width, height);
    auto bytes = makeBayer14(width, height, 5);
    int rc = -1;
    llrpGpuPlaybackReconTiming_t timing = {};

    // Cpu16Probe.
    std::vector<uint16_t> cpuOut(bytes.size());
    ASSERT_NE(0, llrpGpuPlaybackReconRunCpu16Probe(&state, bytes.data(),
        bytes.size() * sizeof(uint16_t), cpuOut.data(), &rc, &timing));
    ASSERT_NE(0, llrpGpuPlaybackReconPreuploadFrame(0, bytes.data(),
        bytes.size() * sizeof(uint16_t)));
    ASSERT_NE(0, llrpGpuPlaybackReconRunCpu16Probe(&state, bytes.data(),
        bytes.size() * sizeof(uint16_t), cpuOut.data(), &rc, &timing));
    ASSERT_NE(0, timing.preupload.used);

    // GL texture (fake backend succeeds without touching the texture id).
    ASSERT_NE(0, llrpGpuPlaybackReconPreuploadFrame(0, bytes.data(),
        bytes.size() * sizeof(uint16_t)));
    ASSERT_NE(0, llrpGpuPlaybackReconRunGlTexture(&state, bytes.data(),
        bytes.size() * sizeof(uint16_t), 1u, &rc, &timing));
    ASSERT_NE(0, timing.preupload.used);

    // Device Bayer16.
    const uint16_t * deviceBayer16 = nullptr;
    int deviceWidth = 0, deviceHeight = 0;
    ASSERT_NE(0, llrpGpuPlaybackReconPreuploadFrame(0, bytes.data(),
        bytes.size() * sizeof(uint16_t)));
    ASSERT_NE(0, llrpGpuPlaybackReconRunDeviceBayer16(&state, bytes.data(),
        bytes.size() * sizeof(uint16_t), &deviceBayer16, &deviceWidth,
        &deviceHeight, &rc, &timing));
    ASSERT_NE(0, timing.preupload.used);
    ASSERT_TRUE(deviceBayer16 != nullptr);

    // Retained device Bayer16.
    llrpGpuPlaybackRetainedDeviceBayer16_t retained = {};
    ASSERT_NE(0, llrpGpuPlaybackReconPreuploadFrame(0, bytes.data(),
        bytes.size() * sizeof(uint16_t)));
    ASSERT_NE(0, llrpGpuPlaybackReconRunRetainedDeviceBayer16(&state,
        bytes.data(), bytes.size() * sizeof(uint16_t), &retained, &rc, &timing));
    ASSERT_NE(0, timing.preupload.used);
    ASSERT_NE(0, retained.valid);
}

TEST(DualIsoPipeline, AsyncPreuploadRenderThreadRetainedDevicePathReportsStatusSameThread)
{
    // This is the actual production call chain (blocker fixed in this round):
    // applyLLRawProcObjectWorker()'s retained-device, prepare-only branch now
    // passes a real timing_out to llrpGpuPlaybackReconRunRetainedDeviceBayer16()
    // and copies its .preupload into a same-thread TLS that
    // insertGpuPlaybackReconRunTelemetry() (RenderFrameThread.cpp) reads right
    // after this call returns. Reverting that timing_out back to NULL, or
    // dropping the TLS copy, turns this test red.
    const FakeGpuBackendScope fakeBackend;
    // Mirrors PreparedPreuploadFixtureScope's env set (the working pattern
    // used by the staging-side tests above) plus RETAIN_DEVICE_OUTPUT=1:
    // gpu_playback_prepare_only_allowed requires MLVAPP_GPU_PLAYBACK_RECON
    // truthy (not just the thread-local opt-in below) and !gpu_export_input
    // requires MLVAPP_GPU_EXPORT off, or this whole branch is silently
    // skipped and every status stays at its zeroed reset.
    std::vector<std::pair<QByteArray, QByteArray>> savedEnv;
    for (const auto &setting : std::vector<std::pair<QByteArray, QByteArray>>{
             {"MLVAPP_GPU_EXPORT", "0"},
             {"MLVAPP_GPU_PLAYBACK_RECON", "1"},
             {"MLVAPP_GPU_PLAYBACK_RECON_ASYNC_H2D", "1"},
             {"MLVAPP_GPU_PLAYBACK_RECON_VALIDATE_OUTPUT", "0"},
             {"MLVAPP_GPU_PLAYBACK_RECON_RETAIN_DEVICE_OUTPUT", "1"}})
    {
        savedEnv.push_back({setting.first, qgetenv(setting.first.constData())});
        qputenv(setting.first.constData(), setting.second);
    }

    MlvPipelineFixture fixture;
    assert_fixture_ready(fixture);
    configure_gpu_export_supported_dual_iso(fixture);
    auto *video = fixture.video();
    llrpSetDualIsoInterpolationMethod(video, DISOI_MEAN23);
    video->llrawproc->focus_pixels = 0;
    video->llrawproc->bad_pixels = 0;
    video->llrawproc->vertical_stripes = 0;

    const GpuPlaybackReconThreadOptIn optIn(true);
    const GpuPlaybackReconTexturePresentOptIn texturePresent(true);
    const GpuPlaybackReconTexturePrepareOnlyOptIn prepareOnly(true);

    const size_t pixelCount = size_t(fixture.width()) * size_t(fixture.height());
    mlv_pipeline_capture_set_current_frame(0);

    // First pass warms up clip_configured on the fake backend (see the other
    // tests in this file); the second pass is the one whose preupload is
    // actually staged-then-consumed inside the worker. Each pass decodes a
    // fresh buffer -- applyLLRawProcObjectWorker corrects it in place, and
    // reusing an already-corrected buffer would double-apply those fixes.
    std::vector<uint16_t> rawPass1(pixelCount);
    ASSERT_EQ(0, getMlvRawFrameUint16(video, 0, rawPass1.data()));
    applyLLRawProcObjectWorker(video, rawPass1.data(), rawPass1.size() * sizeof(uint16_t), nullptr, 0);

    std::vector<uint16_t> rawPass2(pixelCount);
    ASSERT_EQ(0, getMlvRawFrameUint16(video, 0, rawPass2.data()));
    applyLLRawProcObjectWorker(video, rawPass2.data(), rawPass2.size() * sizeof(uint16_t), nullptr, 0);

    llrpGpuPlaybackReconPreuploadStatus_t status = {};
    const bool available = llrpGpuPlaybackReconLastPreuploadStatusForTesting(&status) != 0;

    for (const auto &setting : savedEnv) {
        if (setting.second.isNull()) qunsetenv(setting.first.constData());
        else qputenv(setting.first.constData(), setting.second);
    }

    ASSERT_TRUE(available);
    ASSERT_NE(0, status.accepted);
    ASSERT_NE(0, status.used);
}
