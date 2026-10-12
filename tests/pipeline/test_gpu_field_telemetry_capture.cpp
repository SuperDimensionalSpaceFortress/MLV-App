// GPU-FIELD-TELEMETRY-1: the recon stage seam through the real llrawproc C code, on the in-process
// fake backend. (1) Injected failing status: a run that fails with cudaErrorLaunchTimeout (702)
// reaches the stage sink as stage "recon:run", rc -1, cuda_code 702 with its name and site, within
// that same call; a validation failure (rc 3, no CUDA code) reaches it with cuda_code 0, even right
// after a CUDA fault (the slot's count did not move). (2) Telemetry OFF: with MLVAPP_GPU_TELEMETRY=0
// install() installs no sink. This binary sets MLVAPP_GPU_TELEMETRY=0 before main(), so the
// function-local gate latches OFF before anything can read it; the ON-path line text, aggregate and
// rotation are pinned in tests/console/test_gpu_field_telemetry.cpp.
#include "../common/minitest.h"
#include "../../src/mlv/llrawproc/llrawproc.h"
#include "../../platform/qt/GpuFieldTelemetry.h"

#include <QByteArray>

#include <cstdint>
#include <string>
#include <vector>

extern "C" int llrpInstallFakeGpuPlaybackReconBackendForTesting(int install);
extern "C" int llrpResetGpuExportBackendForTesting(void);
extern "C" void llrpFakeGpuBackendInjectRunFailureForTesting(int rc, int cuda_code, const char * name);
extern "C" void llrpFakeGpuBackendInjectPreuploadFailureForTesting(int rc, int cuda_code, const char * name);

namespace
{
const bool kTelemetryOffForThisBinary = qputenv("MLVAPP_GPU_TELEMETRY", QByteArrayLiteral("0"));

class AsyncH2dOnScope
{
    QByteArray saved;
    bool hadSaved;
public:
    AsyncH2dOnScope()
    {
        hadSaved = qEnvironmentVariableIsSet("MLVAPP_GPU_PLAYBACK_RECON_ASYNC_H2D");
        if (hadSaved) saved = qgetenv("MLVAPP_GPU_PLAYBACK_RECON_ASYNC_H2D");
        qputenv("MLVAPP_GPU_PLAYBACK_RECON_ASYNC_H2D", "1");
    }
    ~AsyncH2dOnScope()
    {
        if (hadSaved) qputenv("MLVAPP_GPU_PLAYBACK_RECON_ASYNC_H2D", saved);
        else qunsetenv("MLVAPP_GPU_PLAYBACK_RECON_ASYNC_H2D");
    }
};

struct SinkCall
{
    std::string stage;
    int rc;
    double ms;
    int cudaCode;
    std::string cudaName;
    std::string site;
};

std::vector<SinkCall> g_calls;

void recordingSink(const char * stage, int rc, double ms, int cudaCode, const char * cudaName, const char * site)
{
    g_calls.push_back({stage ? stage : "", rc, ms, cudaCode, cudaName ? cudaName : "", site ? site : ""});
}

struct SinkScope
{
    SinkScope() { g_calls.clear(); llrpSetGpuStageTelemetrySink(&recordingSink); }
    ~SinkScope() { llrpSetGpuStageTelemetrySink(nullptr); g_calls.clear(); }
};

struct FakeGpuBackendScope
{
    FakeGpuBackendScope() { llrpInstallFakeGpuPlaybackReconBackendForTesting(1); }
    ~FakeGpuBackendScope()
    {
        llrpInstallFakeGpuPlaybackReconBackendForTesting(0);
        llrpResetGpuExportBackendForTesting();
    }
};

llrpGpuPlaybackReconState_t validatedState(uint64_t frameId, int width, int height)
{
    static int dummy_int_lut[1] = { 0 };
    static double dummy_double_lut[1] = { 0.0 };
    llrpGpuPlaybackReconState_t state = {};
    state.valid = 1;
    state.width = width;
    state.height = height;
    state.white_level = 65535;
    state.white_darkened = 65535;
    state.interp_method = 1;
    state.use_alias_map = 1;
    state.use_fullres = 1;
    state.raw2ev = dummy_int_lut;
    state.ev2raw = dummy_int_lut;
    state.mix_curve = dummy_double_lut;
    state.fullres_curve = dummy_double_lut;
    state.frame_id = frameId;
    return state;
}

int runOnce(uint64_t frameId, int * rcOut)
{
    const int width = 4, height = 4;
    const llrpGpuPlaybackReconState_t state = validatedState(frameId, width, height);
    std::vector<uint16_t> input(size_t(width) * size_t(height), 100);
    std::vector<uint16_t> output(input.size());
    llrpGpuPlaybackReconTiming_t timing = {};
    return llrpGpuPlaybackReconRunCpu16Probe(&state, input.data(), input.size() * sizeof(uint16_t),
                                             output.data(), rcOut, &timing);
}

// The async H2D preupload of the bytes runOnce renders (non-zero = accepted).
int preuploadOnce(uint64_t frameId)
{
    const std::vector<uint16_t> input(16, 100);
    return llrpGpuPlaybackReconPreuploadFrame(frameId, input.data(), input.size() * sizeof(uint16_t));
}
} // namespace

// r3 B2 (sol): an async preupload that fails with a CUDA code (here cudaErrorMemoryAllocation,
// as preupload:malloc_device would) reaches the sink from the preupload call itself, attributed to
// "recon:preupload", even though the render that follows succeeds on the synchronous path and never
// reads the slot as a failure. The code is attributed once: the render does not repeat it.
TEST(GpuFieldTelemetryCapture, PreuploadFailureReachesTheSinkWhileRenderSucceeds)
{
    const FakeGpuBackendScope fake;
    const AsyncH2dOnScope asyncH2d;
    const SinkScope sink;
    int rc = -99;
    ASSERT_NE(0, runOnce(1, &rc)); // configures the clip, as the first played frame does
    ASSERT_EQ(0, rc);

    // A successful preupload emits nothing (no new work on the success path).
    g_calls.clear();
    ASSERT_NE(0, preuploadOnce(2));
    ASSERT_EQ(0, (int)g_calls.size());
    ASSERT_NE(0, runOnce(2, &rc));
    ASSERT_EQ(0, rc);

    g_calls.clear();
    llrpFakeGpuBackendInjectPreuploadFailureForTesting(-1, 2, "cudaErrorMemoryAllocation");
    ASSERT_EQ(0, preuploadOnce(3));
    ASSERT_EQ(1, (int)g_calls.size());
    ASSERT_EQ(std::string("recon:preupload"), g_calls[0].stage);
    ASSERT_EQ(-1, g_calls[0].rc);
    ASSERT_NEAR(-1.0, g_calls[0].ms, 1e-12);
    ASSERT_EQ(2, g_calls[0].cudaCode);
    ASSERT_EQ(std::string("cudaErrorMemoryAllocation"), g_calls[0].cudaName);
    ASSERT_EQ(std::string("fake:preupload"), g_calls[0].site);

    // The render of that frame succeeds (the preupload was not taken) and reports success only.
    g_calls.clear();
    ASSERT_NE(0, runOnce(3, &rc));
    ASSERT_EQ(0, rc);
    ASSERT_EQ(1, (int)g_calls.size());
    ASSERT_EQ(std::string("recon:run"), g_calls[0].stage);
    ASSERT_EQ(0, g_calls[0].rc);
    ASSERT_EQ(0, g_calls[0].cudaCode);

    // A later failed render does not inherit the preupload's code: the slot did not move again.
    g_calls.clear();
    llrpFakeGpuBackendInjectRunFailureForTesting(3, 0, nullptr);
    ASSERT_EQ(0, runOnce(4, &rc));
    ASSERT_EQ(1, (int)g_calls.size());
    ASSERT_EQ(3, g_calls[0].rc);
    ASSERT_EQ(0, g_calls[0].cudaCode);

    // A validation failure (rc -1, no CUDA code) is still reported, with cuda_code 0.
    g_calls.clear();
    llrpFakeGpuBackendInjectPreuploadFailureForTesting(-1, 0, nullptr);
    ASSERT_EQ(0, preuploadOnce(5));
    ASSERT_EQ(1, (int)g_calls.size());
    ASSERT_EQ(std::string("recon:preupload"), g_calls[0].stage);
    ASSERT_EQ(0, g_calls[0].cudaCode);

    // "No free slot" (rc 4) is back-pressure, not a fault: nothing, unless a CUDA code was noted.
    g_calls.clear();
    llrpFakeGpuBackendInjectPreuploadFailureForTesting(4, 0, nullptr);
    ASSERT_EQ(0, preuploadOnce(6));
    ASSERT_EQ(0, (int)g_calls.size());
    llrpFakeGpuBackendInjectPreuploadFailureForTesting(4, 709, "cudaErrorContextIsDestroyed");
    ASSERT_EQ(0, preuploadOnce(7));
    ASSERT_EQ(1, (int)g_calls.size());
    ASSERT_EQ(709, g_calls[0].cudaCode);
}

// With no sink installed a failed preupload is unchanged and calls nothing.
TEST(GpuFieldTelemetryCapture, PreuploadFailureWithNoSinkCallsNothing)
{
    const FakeGpuBackendScope fake;
    const AsyncH2dOnScope asyncH2d;
    llrpSetGpuStageTelemetrySink(nullptr);
    g_calls.clear();
    int rc = -99;
    ASSERT_NE(0, runOnce(1, &rc));
    llrpFakeGpuBackendInjectPreuploadFailureForTesting(-1, 2, "cudaErrorMemoryAllocation");
    ASSERT_EQ(0, preuploadOnce(2));
    ASSERT_EQ(0, (int)g_calls.size());
}

TEST(GpuFieldTelemetryCapture, InjectedLaunchTimeoutReachesTheSinkWithItsCode)
{
    const FakeGpuBackendScope fake;
    const SinkScope sink;

    // A healthy first frame: set_clip, set_luts and run all report success with a time.
    int rc = -99;
    ASSERT_NE(0, runOnce(1, &rc));
    ASSERT_EQ(0, rc);
    ASSERT_EQ(3, (int)g_calls.size());
    ASSERT_EQ(std::string("recon:set_clip"), g_calls[0].stage);
    ASSERT_EQ(std::string("recon:set_luts"), g_calls[1].stage);
    ASSERT_EQ(std::string("recon:run"), g_calls[2].stage);
    for (const SinkCall & c : g_calls)
    {
        ASSERT_EQ(0, c.rc);
        ASSERT_EQ(0, c.cudaCode);
        ASSERT_EQ(std::string(), c.cudaName);
        ASSERT_EQ(std::string(), c.site);
    }
    ASSERT_NEAR(3.0, g_calls[2].ms, 1e-9); // the fake backend's own total_ms

    // The next run fails as a real backend's CK branch would on a sticky launch timeout.
    g_calls.clear();
    llrpFakeGpuBackendInjectRunFailureForTesting(-1, 702, "cudaErrorLaunchTimeout");
    ASSERT_EQ(0, runOnce(2, &rc));
    ASSERT_EQ(-1, rc);
    ASSERT_EQ(1, (int)g_calls.size()); // clip and LUTs unchanged: only the run
    ASSERT_EQ(std::string("recon:run"), g_calls[0].stage);
    ASSERT_EQ(-1, g_calls[0].rc);
    ASSERT_EQ(702, g_calls[0].cudaCode);
    ASSERT_EQ(std::string("cudaErrorLaunchTimeout"), g_calls[0].cudaName);
    ASSERT_EQ(std::string("fake:run"), g_calls[0].site);
    ASSERT_NEAR(-1.0, g_calls[0].ms, 1e-12);
    ASSERT_TRUE(GpuFieldTelemetry::isDeviceLossCode(g_calls[0].cudaCode));

    // A validation failure right after it carries no CUDA code: the slot did not move.
    g_calls.clear();
    llrpFakeGpuBackendInjectRunFailureForTesting(3, 0, nullptr);
    ASSERT_EQ(0, runOnce(3, &rc));
    ASSERT_EQ(3, rc);
    ASSERT_EQ(1, (int)g_calls.size());
    ASSERT_EQ(std::string("recon:run"), g_calls[0].stage);
    ASSERT_EQ(3, g_calls[0].rc);
    ASSERT_EQ(0, g_calls[0].cudaCode);
    ASSERT_EQ(std::string(), g_calls[0].cudaName);
    ASSERT_FALSE(GpuFieldTelemetry::isDeviceLossCode(g_calls[0].cudaCode));

    // And the backend recovers on the following frame.
    g_calls.clear();
    ASSERT_NE(0, runOnce(4, &rc));
    ASSERT_EQ(0, rc);
    ASSERT_EQ(1, (int)g_calls.size());
    ASSERT_EQ(0, g_calls[0].rc);
}

TEST(GpuFieldTelemetryCapture, NoSinkMeansNoCallAndNoBehaviourChange)
{
    const FakeGpuBackendScope fake;
    llrpSetGpuStageTelemetrySink(nullptr);
    g_calls.clear();
    int rc = -99;
    ASSERT_NE(0, runOnce(1, &rc));
    ASSERT_EQ(0, rc);
    llrpFakeGpuBackendInjectRunFailureForTesting(-1, 702, "cudaErrorLaunchTimeout");
    ASSERT_EQ(0, runOnce(2, &rc));
    ASSERT_EQ(-1, rc); // the failure itself is unchanged
    ASSERT_EQ(0, (int)g_calls.size());
}

TEST(GpuFieldTelemetryCapture, TelemetryOffInstallsNoSink)
{
    ASSERT_TRUE(kTelemetryOffForThisBinary);
    llrpSetGpuStageTelemetrySink(nullptr);
    ASSERT_FALSE(GpuFieldTelemetry::enabled());
    GpuFieldTelemetry::install();
    ASSERT_TRUE(llrpGetGpuStageTelemetrySinkForTesting() == nullptr);
    // Every entry point is inert: nothing is recorded or written.
    GpuFieldTelemetry::noteStage("recon", "recon:run", 1.0);
    GpuFieldTelemetry::flush("session_end");
    ASSERT_TRUE(llrpGetGpuStageTelemetrySinkForTesting() == nullptr);
}
