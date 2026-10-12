// GPU-FIELD-TELEMETRY-1: the recon stage seam through the real llrawproc C code, on the in-process
// fake backend. (1) Injected failing status: a run that fails with cudaErrorLaunchTimeout (702)
// reaches the stage sink as stage "recon:run", rc -1, cuda_code 702 with its name and site, within
// that same call; a validation failure (rc 3, no CUDA code) reaches it with cuda_code 0, even right
// after a CUDA fault (the slot's count did not move). (2) Telemetry OFF: with MLVAPP_GPU_TELEMETRY=0
// install() installs no sink. This binary sets MLVAPP_GPU_TELEMETRY=0 before main(), so the
// function-local gate latches OFF before anything can read it; the ON-path line text, aggregate and
// rotation are pinned in tests/console/test_gpu_field_telemetry.cpp. (3) r4: the recon DLL's
// per-channel slot (igpu_cuda_error_slot.h, host-only, driven directly) and llrawproc's captures
// keep a preupload fault and an overlapping render fault apart, each reported once. (4) r5: a failed
// call reports EVERY code it noted (primary, then secondary), a code noted by a call nothing
// captures is emitted as an orphan against its own site and never credited to the next call, and
// queue drops are counted (dropped=N). In-call notes are armed so the fake notes them inside its
// call, as a real failure branch does; NoteCudaErrorForTesting notes at once, between calls.
#include "../common/minitest.h"
#include "../../src/mlv/llrawproc/llrawproc.h"
#include "../../platform/qt/GpuFieldTelemetry.h"
#include "../../tools/gpu/backend/igpu_cuda_error_slot.h"

#include <QByteArray>

#include <cstdint>
#include <set>
#include <string>
#include <thread>
#include <vector>

extern "C" int llrpInstallFakeGpuPlaybackReconBackendForTesting(int install);
extern "C" int llrpResetGpuExportBackendForTesting(void);
extern "C" void llrpFakeGpuBackendInjectRunFailureForTesting(int rc, int cuda_code, const char * name);
extern "C" void llrpFakeGpuBackendInjectPreuploadFailureForTesting(int rc, int cuda_code, const char * name);
extern "C" void llrpFakeGpuBackendNoteCudaErrorForTesting(int channel, int cuda_code, const char * name, const char * site);
extern "C" void llrpFakeGpuBackendArmCudaNoteForTesting(int channel, int cuda_code, const char * name, const char * site);
extern "C" long llrpFakeGpuBackendCudaErrorsPendingForTesting(int channel, unsigned long long * dropped_total);

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
    std::string role; // "" = a success record (role NULL)
    unsigned long long dropped;
};

std::vector<SinkCall> g_calls;

void recordingSink(const char * stage, int rc, double ms, int cudaCode, const char * cudaName, const char * site,
                   const char * role, unsigned long long dropped)
{
    g_calls.push_back({stage ? stage : "", rc, ms, cudaCode, cudaName ? cudaName : "", site ? site : "",
                       role ? role : "", dropped});
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

namespace cuda_slot = igpu_cuda_error_slot;
constexpr int kRender = IGPU_CUDA_ERROR_CHANNEL_RENDER;
constexpr int kPreupload = IGPU_CUDA_ERROR_CHANNEL_PREUPLOAD;

igpu_cuda_error_t take(int channel)
{
    igpu_cuda_error_t e;
    e.code = -1;
    ASSERT_EQ(0, cuda_slot::take(channel, &e));
    return e;
}

// The slot is one static per binary: empty both channels before a test (bounded: a channel holds
// at most kPending notes, so one more take must find it empty).
void drainSlot()
{
    for (int channel : {kRender, kPreupload})
    {
        for (int i = 0; i < cuda_slot::kPending; ++i) take(channel);
        ASSERT_EQ(0, take(channel).code);
        // r5: and absorb any drop count an earlier test left unreported.
        igpu_cuda_error_t out[cuda_slot::kPending];
        ASSERT_EQ(0, cuda_slot::drain(channel, out, cuda_slot::kPending, nullptr));
    }
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

// r4 B1 (sol r2 blocker 1), at the DLL slot: the preupload entry's new cudaSetDevice note (site
// preupload:set_device) lands on the preupload channel, so the preupload capture takes it and a
// render capture never does.
TEST(GpuFieldTelemetryCapture, SlotRoutesThePreuploadSetDeviceNoteToThePreuploadChannel)
{
    drainSlot();
    cuda_slot::note(101, "cudaErrorInvalidDevice", "preupload:set_device");
    ASSERT_EQ(0, take(kRender).code);
    const igpu_cuda_error_t e = take(kPreupload);
    ASSERT_EQ(101, e.code);
    ASSERT_EQ(std::string("cudaErrorInvalidDevice"), std::string(e.name));
    ASSERT_EQ(std::string("preupload:set_device"), std::string(e.site));
    ASSERT_EQ(0, take(kPreupload).code); // handed out once
    // Every other site is the render channel's, including the render's own preupload wait.
    cuda_slot::note(702, "cudaErrorLaunchTimeout", "run:preupload_wait");
    ASSERT_EQ(0, take(kPreupload).code);
    ASSERT_EQ(702, take(kRender).code);
    // The reader the r1 export serves is unchanged: the last note, whatever its channel.
    igpu_cuda_error_t last;
    ASSERT_EQ(0, cuda_slot::copy_last(&last));
    ASSERT_EQ(702, last.code);
    ASSERT_EQ(-1, cuda_slot::take(2, &last));
    ASSERT_EQ(-1, cuda_slot::take(kRender, nullptr));
}

// r4 B1 (sol r2 blocker 1), through llrawproc: a preupload whose cudaSetDevice failed (the fake
// replays the DLL's new note, then the -1 return) reaches the sink as recon:preupload with the real
// code and site, not cuda_code 0. On 0c555119 the DLL returned with no note, so the record read 0.
TEST(GpuFieldTelemetryCapture, PreuploadSetDeviceFailureCarriesItsCode)
{
    const FakeGpuBackendScope fake;
    const AsyncH2dOnScope asyncH2d;
    const SinkScope sink;
    int rc = -99;
    ASSERT_NE(0, runOnce(1, &rc));
    ASSERT_EQ(0, rc);
    g_calls.clear();
    // r5: armed, so the fake notes it inside the preupload call, as the DLL's branch does.
    llrpFakeGpuBackendArmCudaNoteForTesting(kPreupload, 101, "cudaErrorInvalidDevice", "preupload:set_device");
    llrpFakeGpuBackendInjectPreuploadFailureForTesting(-1, 0, nullptr);
    ASSERT_EQ(0, preuploadOnce(2));
    ASSERT_EQ(1, (int)g_calls.size());
    ASSERT_EQ(std::string("recon:preupload"), g_calls[0].stage);
    ASSERT_EQ(-1, g_calls[0].rc);
    ASSERT_EQ(101, g_calls[0].cudaCode);
    ASSERT_EQ(std::string("cudaErrorInvalidDevice"), g_calls[0].cudaName);
    ASSERT_EQ(std::string("preupload:set_device"), g_calls[0].site);
}

// r4 B2 (sol r2 blocker 2), at the DLL slot, the hub's case: preupload notes A, the render notes B
// before A is captured. Both survive with their own channel and are each handed out once, in
// either capture order; later validation failures on either path take nothing. Sequence numbers
// are monotonic.
TEST(GpuFieldTelemetryCapture, SlotKeepsOverlappingPreuploadAndRenderCodesOnce)
{
    drainSlot();
    cuda_slot::note(2, "cudaErrorMemoryAllocation", "preupload:malloc_device"); // A
    cuda_slot::note(700, "cudaErrorIllegalAddress", "cudaLaunchKernel");         // B, before A's capture
    const igpu_cuda_error_t a = take(kPreupload);
    const igpu_cuda_error_t b = take(kRender);
    ASSERT_EQ(2, a.code);
    ASSERT_EQ(std::string("preupload:malloc_device"), std::string(a.site));
    ASSERT_EQ(700, b.code);
    ASSERT_EQ(std::string("cudaLaunchKernel"), std::string(b.site));
    ASSERT_TRUE(b.count == a.count + 1);
    ASSERT_EQ(0, take(kRender).code);
    ASSERT_EQ(0, take(kPreupload).code);

    // The render captures first this time.
    cuda_slot::note(2, "cudaErrorMemoryAllocation", "preupload:submit");
    cuda_slot::note(719, "cudaErrorLaunchFailure", "cudaLaunchKernel");
    ASSERT_EQ(719, take(kRender).code);
    ASSERT_EQ(2, take(kPreupload).code);
    ASSERT_EQ(0, take(kRender).code);
    ASSERT_EQ(0, take(kPreupload).code);
}

// r4 B2, sol's interleaving at the slot: capture A (preupload, its note has count n) pauses, B
// (render, count n+1) notes and captures, then A resumes. take() copies and consumes in one step
// under the slot mutex, so there is no copied count to go stale: A still gets its own code, B its
// own, and a later validation failure on either channel repeats neither. Two preupload faults
// before either capture are both kept too, oldest first, and a full channel drops its oldest.
TEST(GpuFieldTelemetryCapture, SlotInterleavedCapturesNeverRepeatACode)
{
    drainSlot();
    cuda_slot::note(2, "cudaErrorMemoryAllocation", "preupload:submit");     // A noted; A's capture pauses
    cuda_slot::note(700, "cudaErrorIllegalAddress", "cudaLaunchKernel");     // B noted
    ASSERT_EQ(700, take(kRender).code);                                      // B captured
    ASSERT_EQ(2, take(kPreupload).code);                                     // A resumes
    ASSERT_EQ(0, take(kRender).code);                                        // later validation failures
    ASSERT_EQ(0, take(kPreupload).code);

    cuda_slot::note(2, "cudaErrorMemoryAllocation", "preupload:malloc_host");
    cuda_slot::note(719, "cudaErrorLaunchFailure", "preupload:submit");
    const igpu_cuda_error_t first = take(kPreupload);
    const igpu_cuda_error_t second = take(kPreupload);
    ASSERT_EQ(2, first.code);
    ASSERT_EQ(719, second.code);
    ASSERT_TRUE(second.count > first.count);
    ASSERT_EQ(0, take(kPreupload).code);

    for (int i = 1; i <= cuda_slot::kPending + 2; ++i)
        cuda_slot::note(1000 + i, "cudaErrorUnknown", "preupload:submit");
    for (int i = 3; i <= cuda_slot::kPending + 2; ++i)
        ASSERT_EQ(1000 + i, take(kPreupload).code);
    ASSERT_EQ(0, take(kPreupload).code);
}

// r4 B2 under real threads: four threads each note a preupload fault and then capture, 2000 times.
// Fewer notes than the ring holds are ever pending, so every capture gets a code and every sequence
// number is handed out exactly once.
TEST(GpuFieldTelemetryCapture, SlotHandsEachNoteOutExactlyOnceAcrossThreads)
{
    drainSlot();
    constexpr int kThreads = 4;
    constexpr int kEach = 2000;
    static_assert(kThreads < cuda_slot::kPending, "nothing may be dropped");
    std::vector<std::vector<unsigned long long>> taken(kThreads);
    std::vector<int> empty(kThreads, 0);
    std::vector<std::thread> threads;
    for (int t = 0; t < kThreads; ++t)
        threads.emplace_back([&, t] {
            for (int i = 0; i < kEach; ++i)
            {
                cuda_slot::note(2, "cudaErrorMemoryAllocation", "preupload:submit");
                igpu_cuda_error_t e;
                cuda_slot::take(kPreupload, &e);
                if (e.code == 0) ++empty[t];
                else taken[t].push_back(e.count);
            }
        });
    for (std::thread & thread : threads) thread.join();
    std::set<unsigned long long> unique;
    for (int t = 0; t < kThreads; ++t)
    {
        ASSERT_EQ(0, empty[t]);
        unique.insert(taken[t].begin(), taken[t].end());
    }
    ASSERT_EQ((size_t)(kThreads * kEach), unique.size());
    ASSERT_EQ(0, take(kPreupload).code);
}

// r4 B2 (sol r2 blocker 2), through llrawproc, the hub's case: the preupload's failure branch notes
// A, then the render (bypass overlap) notes B before A's capture runs. The preupload capture reports
// A under recon:preupload, the render capture reports B under recon:run, and later validation
// failures on either path report no code. On 0c555119's shared slot and exchanged count the
// preupload record read B and the render record read nothing, so A was lost.
TEST(GpuFieldTelemetryCapture, OverlappingPreuploadAndRenderFaultsKeepBothCodesOnce)
{
    const FakeGpuBackendScope fake;
    const AsyncH2dOnScope asyncH2d;
    const SinkScope sink;
    int rc = -99;
    ASSERT_NE(0, runOnce(1, &rc));
    ASSERT_EQ(0, rc);
    g_calls.clear();

    // r5: each note is armed into its own call (a single thread cannot interleave the two calls;
    // the channels keep them apart whatever the order).
    llrpFakeGpuBackendArmCudaNoteForTesting(kPreupload, 2, "cudaErrorMemoryAllocation", "preupload:malloc_device");
    llrpFakeGpuBackendArmCudaNoteForTesting(kRender, 700, "cudaErrorIllegalAddress", "run:kernel");
    llrpFakeGpuBackendInjectPreuploadFailureForTesting(-1, 0, nullptr);
    ASSERT_EQ(0, preuploadOnce(2));
    llrpFakeGpuBackendInjectRunFailureForTesting(-1, 0, nullptr);
    ASSERT_EQ(0, runOnce(3, &rc));
    ASSERT_EQ(-1, rc);
    ASSERT_EQ(2, (int)g_calls.size());
    ASSERT_EQ(std::string("recon:preupload"), g_calls[0].stage);
    ASSERT_EQ(-1, g_calls[0].rc);
    ASSERT_EQ(2, g_calls[0].cudaCode);
    ASSERT_EQ(std::string("preupload:malloc_device"), g_calls[0].site);
    ASSERT_EQ(std::string("recon:run"), g_calls[1].stage);
    ASSERT_EQ(-1, g_calls[1].rc);
    ASSERT_EQ(700, g_calls[1].cudaCode);
    ASSERT_EQ(std::string("cudaErrorIllegalAddress"), g_calls[1].cudaName);
    ASSERT_EQ(std::string("run:kernel"), g_calls[1].site);

    // Later validation failures repeat neither code.
    g_calls.clear();
    llrpFakeGpuBackendInjectRunFailureForTesting(3, 0, nullptr);
    ASSERT_EQ(0, runOnce(4, &rc));
    llrpFakeGpuBackendInjectPreuploadFailureForTesting(-1, 0, nullptr);
    ASSERT_EQ(0, preuploadOnce(5));
    ASSERT_EQ(2, (int)g_calls.size());
    ASSERT_EQ(std::string("recon:run"), g_calls[0].stage);
    ASSERT_EQ(3, g_calls[0].rc);
    ASSERT_EQ(0, g_calls[0].cudaCode);
    ASSERT_EQ(std::string("recon:preupload"), g_calls[1].stage);
    ASSERT_EQ(0, g_calls[1].cudaCode);
}

// r4 B2, sol's interleaving through llrawproc: A (preupload) is noted and its capture pauses; B
// (render) is noted and captured first; A resumes. Each record keeps its own stage and code, and a
// later validation failure repeats neither (on 0c555119, A's late exchange rewound the seen count
// and the validation failure re-reported B).
TEST(GpuFieldTelemetryCapture, InterleavedCapturesKeepTheirStageAndNeverRepeat)
{
    const FakeGpuBackendScope fake;
    const AsyncH2dOnScope asyncH2d;
    const SinkScope sink;
    int rc = -99;
    ASSERT_NE(0, runOnce(1, &rc));
    g_calls.clear();

    llrpFakeGpuBackendArmCudaNoteForTesting(kPreupload, 2, "cudaErrorMemoryAllocation", "preupload:submit");  // A (r5: in its call)
    llrpFakeGpuBackendInjectRunFailureForTesting(-1, 700, "cudaErrorIllegalAddress");                         // B
    ASSERT_EQ(0, runOnce(2, &rc));
    llrpFakeGpuBackendInjectPreuploadFailureForTesting(-1, 0, nullptr);                                       // A resumes
    ASSERT_EQ(0, preuploadOnce(3));
    llrpFakeGpuBackendInjectRunFailureForTesting(3, 0, nullptr);                                              // validation
    ASSERT_EQ(0, runOnce(4, &rc));
    ASSERT_EQ(3, (int)g_calls.size());
    ASSERT_EQ(std::string("recon:run"), g_calls[0].stage);
    ASSERT_EQ(700, g_calls[0].cudaCode);
    ASSERT_EQ(std::string("fake:run"), g_calls[0].site);
    ASSERT_EQ(std::string("recon:preupload"), g_calls[1].stage);
    ASSERT_EQ(2, g_calls[1].cudaCode);
    ASSERT_EQ(std::string("preupload:submit"), g_calls[1].site);
    ASSERT_EQ(std::string("recon:run"), g_calls[2].stage);
    ASSERT_EQ(3, g_calls[2].rc);
    ASSERT_EQ(0, g_calls[2].cudaCode);
}

// r5 B1 (sol r3 blocker), at the DLL slot: one failing call notes A (GL map) and then B (its cleanup
// unregister). drain() hands out both, oldest first, and leaves the channel empty, so a later
// validation-only failure finds nothing and a later timeout C is C. On 042c2787 the capture took
// one note: B stayed queued and was the next failure's code.
TEST(GpuFieldTelemetryCapture, SlotDrainHandsOutEveryNoteOfAFailedCallOnce)
{
    drainSlot();
    const volatile long * pendingRender = cuda_slot::pending_word(kRender);
    ASSERT_TRUE(pendingRender != nullptr);
    ASSERT_TRUE(cuda_slot::pending_word(2) == nullptr);
    ASSERT_EQ(0L, (long)*pendingRender);
    cuda_slot::note(999, "cudaErrorUnknown", "gl_r16:map");                // A
    cuda_slot::note(709, "cudaErrorContextIsDestroyed", "gl_cache:unregister"); // B, the cleanup
    ASSERT_EQ(2L, (long)*pendingRender);
    igpu_cuda_error_t out[cuda_slot::kPending];
    unsigned long long dropped = 99;
    ASSERT_EQ(2, cuda_slot::drain(kRender, out, cuda_slot::kPending, &dropped));
    ASSERT_EQ(999, out[0].code);
    ASSERT_EQ(std::string("gl_r16:map"), std::string(out[0].site));
    ASSERT_EQ(709, out[1].code);
    ASSERT_EQ(std::string("cudaErrorContextIsDestroyed"), std::string(out[1].name));
    ASSERT_EQ(std::string("gl_cache:unregister"), std::string(out[1].site));
    ASSERT_TRUE(out[1].count == out[0].count + 1);
    ASSERT_EQ(0ull, dropped);
    ASSERT_EQ(0L, (long)*pendingRender);
    // A later validation-only failure: nothing pending.
    ASSERT_EQ(0, cuda_slot::drain(kRender, out, cuda_slot::kPending, &dropped));
    ASSERT_EQ(0, take(kRender).code);
    // A later genuine timeout C is C, and nothing stays pending behind it.
    cuda_slot::note(702, "cudaErrorLaunchTimeout", "run:ev_after_dl");
    ASSERT_EQ(1, cuda_slot::drain(kRender, out, cuda_slot::kPending, &dropped));
    ASSERT_EQ(702, out[0].code);
    ASSERT_EQ(0L, (long)*pendingRender);
    ASSERT_EQ(0, take(kRender).code);
    // The preupload channel was never touched; bad arguments are refused.
    ASSERT_EQ(0L, (long)*cuda_slot::pending_word(kPreupload));
    ASSERT_EQ(-1, cuda_slot::drain(kRender, out, cuda_slot::kPending - 1, &dropped));
    ASSERT_EQ(-1, cuda_slot::drain(2, out, cuda_slot::kPending, &dropped));
    ASSERT_EQ(-1, cuda_slot::drain(kRender, nullptr, cuda_slot::kPending, &dropped));
}

// r5 B1 (sol r3 blocker), through llrawproc: the run notes A (map) then B (cleanup) and fails. The
// sink gets two recon:run records for that one call, A primary and B secondary, each with its own
// site. Then a validation-only failure reports cuda_code 0, and a genuine timeout C reports C with
// nothing left pending. On 042c2787 the run record carried A only, the validation record B, and
// the timeout record B again behind which C stayed pending.
TEST(GpuFieldTelemetryCapture, FailedCallReportsEveryCodeItNotedAndLeavesNothingBehind)
{
    const FakeGpuBackendScope fake;
    const SinkScope sink;
    int rc = -99;
    ASSERT_NE(0, runOnce(1, &rc));
    ASSERT_EQ(0, rc);
    g_calls.clear();

    llrpFakeGpuBackendArmCudaNoteForTesting(kRender, 999, "cudaErrorUnknown", "gl_r16:map");                    // A
    llrpFakeGpuBackendArmCudaNoteForTesting(kRender, 709, "cudaErrorContextIsDestroyed", "gl_cache:unregister"); // B
    llrpFakeGpuBackendInjectRunFailureForTesting(-1, 0, nullptr);
    ASSERT_EQ(0, runOnce(2, &rc));
    ASSERT_EQ(-1, rc);
    ASSERT_EQ(2, (int)g_calls.size());
    ASSERT_EQ(std::string("recon:run"), g_calls[0].stage);
    ASSERT_EQ(-1, g_calls[0].rc);
    ASSERT_EQ(999, g_calls[0].cudaCode);
    ASSERT_EQ(std::string("gl_r16:map"), g_calls[0].site);
    ASSERT_EQ(std::string("primary"), g_calls[0].role);
    ASSERT_EQ(std::string("recon:run"), g_calls[1].stage);
    ASSERT_EQ(-1, g_calls[1].rc);
    ASSERT_NEAR(-1.0, g_calls[1].ms, 1e-12);
    ASSERT_EQ(709, g_calls[1].cudaCode);
    ASSERT_EQ(std::string("cudaErrorContextIsDestroyed"), g_calls[1].cudaName);
    ASSERT_EQ(std::string("gl_cache:unregister"), g_calls[1].site);
    ASSERT_EQ(std::string("secondary"), g_calls[1].role);
    ASSERT_EQ(0L, llrpFakeGpuBackendCudaErrorsPendingForTesting(kRender, nullptr));

    // A later validation-only failure: cuda_code 0.
    g_calls.clear();
    llrpFakeGpuBackendInjectRunFailureForTesting(3, 0, nullptr);
    ASSERT_EQ(0, runOnce(3, &rc));
    ASSERT_EQ(3, rc);
    ASSERT_EQ(1, (int)g_calls.size());
    ASSERT_EQ(std::string("recon:run"), g_calls[0].stage);
    ASSERT_EQ(0, g_calls[0].cudaCode);
    ASSERT_EQ(std::string(), g_calls[0].site);
    ASSERT_EQ(std::string("primary"), g_calls[0].role);

    // A later genuine timeout C: reported as C, nothing pending after it.
    g_calls.clear();
    llrpFakeGpuBackendInjectRunFailureForTesting(-1, 702, "cudaErrorLaunchTimeout");
    ASSERT_EQ(0, runOnce(4, &rc));
    ASSERT_EQ(1, (int)g_calls.size());
    ASSERT_EQ(702, g_calls[0].cudaCode);
    ASSERT_EQ(std::string("fake:run"), g_calls[0].site);
    ASSERT_EQ(std::string("primary"), g_calls[0].role);
    ASSERT_EQ(0L, llrpFakeGpuBackendCudaErrorsPendingForTesting(kRender, nullptr));

    // A healthy frame after it: one success record, role NULL.
    g_calls.clear();
    ASSERT_NE(0, runOnce(5, &rc));
    ASSERT_EQ(1, (int)g_calls.size());
    ASSERT_EQ(0, g_calls[0].rc);
    ASSERT_EQ(std::string(), g_calls[0].role);
}

// r5 B2 (fable r3 UNCAPTURED-RENDER-SITES-1): a GL copy (copy_last_device_output_to_gl_texture has
// no capture) notes gl_r16:map between calls. The next captured call drains it at its entry and
// emits it as stage recon:uncaptured, role orphan, with its own site; that call's validation-only
// failure then reports cuda_code 0. The same on the preupload channel, and an orphan is emitted
// even when the next call succeeds. On 042c2787 the run record read 700 at gl_r16:map.
TEST(GpuFieldTelemetryCapture, UncapturedSiteNoteIsEmittedAsAnOrphanNotCreditedToTheNextCall)
{
    const FakeGpuBackendScope fake;
    const AsyncH2dOnScope asyncH2d;
    const SinkScope sink;
    int rc = -99;
    ASSERT_NE(0, runOnce(1, &rc));
    ASSERT_EQ(0, rc);
    g_calls.clear();

    llrpFakeGpuBackendNoteCudaErrorForTesting(kRender, 700, "cudaErrorIllegalAddress", "gl_r16:map");
    llrpFakeGpuBackendInjectRunFailureForTesting(3, 0, nullptr);
    ASSERT_EQ(0, runOnce(2, &rc));
    ASSERT_EQ(3, rc);
    ASSERT_EQ(2, (int)g_calls.size());
    ASSERT_EQ(std::string("recon:uncaptured"), g_calls[0].stage);
    ASSERT_EQ(0, g_calls[0].rc);
    ASSERT_EQ(700, g_calls[0].cudaCode);
    ASSERT_EQ(std::string("cudaErrorIllegalAddress"), g_calls[0].cudaName);
    ASSERT_EQ(std::string("gl_r16:map"), g_calls[0].site);
    ASSERT_EQ(std::string("orphan"), g_calls[0].role);
    ASSERT_EQ(std::string("recon:run"), g_calls[1].stage);
    ASSERT_EQ(3, g_calls[1].rc);
    ASSERT_EQ(0, g_calls[1].cudaCode);
    ASSERT_EQ(std::string(), g_calls[1].site);
    ASSERT_EQ(std::string("primary"), g_calls[1].role);

    // Preupload channel: a leftover preupload note, then a validation-only preupload failure.
    g_calls.clear();
    llrpFakeGpuBackendNoteCudaErrorForTesting(kPreupload, 2, "cudaErrorMemoryAllocation", "preupload:malloc_host");
    llrpFakeGpuBackendInjectPreuploadFailureForTesting(-1, 0, nullptr);
    ASSERT_EQ(0, preuploadOnce(3));
    ASSERT_EQ(2, (int)g_calls.size());
    ASSERT_EQ(std::string("recon:uncaptured"), g_calls[0].stage);
    ASSERT_EQ(2, g_calls[0].cudaCode);
    ASSERT_EQ(std::string("preupload:malloc_host"), g_calls[0].site);
    ASSERT_EQ(std::string("orphan"), g_calls[0].role);
    ASSERT_EQ(std::string("recon:preupload"), g_calls[1].stage);
    ASSERT_EQ(-1, g_calls[1].rc);
    ASSERT_EQ(0, g_calls[1].cudaCode);

    // An orphan ahead of a call that succeeds is still emitted, and the success stays a success.
    g_calls.clear();
    llrpFakeGpuBackendNoteCudaErrorForTesting(kRender, 719, "cudaErrorLaunchFailure", "reset_gl:set_device");
    ASSERT_NE(0, runOnce(4, &rc));
    ASSERT_EQ(0, rc);
    ASSERT_EQ(2, (int)g_calls.size());
    ASSERT_EQ(std::string("orphan"), g_calls[0].role);
    ASSERT_EQ(719, g_calls[0].cudaCode);
    ASSERT_EQ(std::string("reset_gl:set_device"), g_calls[0].site);
    ASSERT_EQ(std::string("recon:run"), g_calls[1].stage);
    ASSERT_EQ(0, g_calls[1].rc);
    ASSERT_EQ(0, g_calls[1].cudaCode);
    ASSERT_EQ(std::string(), g_calls[1].role);
    ASSERT_EQ(0L, llrpFakeGpuBackendCudaErrorsPendingForTesting(kRender, nullptr));
    ASSERT_EQ(0L, llrpFakeGpuBackendCudaErrorsPendingForTesting(kPreupload, nullptr));

    // With no sink installed nothing is drained or emitted: the note waits for an installed sink.
    llrpSetGpuStageTelemetrySink(nullptr);
    g_calls.clear();
    llrpFakeGpuBackendNoteCudaErrorForTesting(kRender, 700, "cudaErrorIllegalAddress", "gl_r16:map");
    ASSERT_NE(0, runOnce(5, &rc));
    ASSERT_EQ(0, (int)g_calls.size());
    ASSERT_EQ(1L, llrpFakeGpuBackendCudaErrorsPendingForTesting(kRender, nullptr));
}

// r5 H1 (both keys, RING-DROP-VISIBLE-1), at the DLL slot: nine notes with no capture drop the
// oldest; the monotonic dropped count is 1 and the next drain reports dropped=1, the one after 0.
TEST(GpuFieldTelemetryCapture, SlotCountsDropsAndReportsThemOnTheNextDrain)
{
    drainSlot();
    const unsigned long long before = cuda_slot::dropped_total(kRender);
    const unsigned long long preuploadBefore = cuda_slot::dropped_total(kPreupload);
    for (int i = 1; i <= cuda_slot::kPending + 1; ++i)
        cuda_slot::note(1000 + i, "cudaErrorUnknown", "gl_r16:map");
    ASSERT_EQ(before + 1, cuda_slot::dropped_total(kRender));
    ASSERT_EQ((long)cuda_slot::kPending, (long)*cuda_slot::pending_word(kRender));
    igpu_cuda_error_t out[cuda_slot::kPending];
    unsigned long long dropped = 0;
    ASSERT_EQ(cuda_slot::kPending, cuda_slot::drain(kRender, out, cuda_slot::kPending, &dropped));
    ASSERT_EQ(1ull, dropped);
    ASSERT_EQ(1002, out[0].code); // the oldest (1001) was the one dropped
    ASSERT_EQ(1000 + cuda_slot::kPending + 1, out[cuda_slot::kPending - 1].code);
    cuda_slot::note(702, "cudaErrorLaunchTimeout", "run:ev_after_dl");
    ASSERT_EQ(1, cuda_slot::drain(kRender, out, cuda_slot::kPending, &dropped));
    ASSERT_EQ(0ull, dropped);
    ASSERT_EQ(before + 1, cuda_slot::dropped_total(kRender)); // monotonic, never reset
    ASSERT_EQ(preuploadBefore, cuda_slot::dropped_total(kPreupload)); // per channel
}

// r5 H1, through llrawproc: nine uncaptured notes before the next call. That call emits the eight
// survivors as orphans; the first record carries dropped=1, the rest 0, and a later fault carries
// dropped=0. On 042c2787 no record carried a drop count.
TEST(GpuFieldTelemetryCapture, NineUncapturedNotesReportDroppedOneOnTheNextRecord)
{
    const FakeGpuBackendScope fake;
    const SinkScope sink;
    int rc = -99;
    ASSERT_NE(0, runOnce(1, &rc));
    g_calls.clear();

    for (int i = 1; i <= IGPU_CUDA_ERROR_PENDING_MAX + 1; ++i)
        llrpFakeGpuBackendNoteCudaErrorForTesting(kRender, 1000 + i, "cudaErrorUnknown", "gl_r16:map");
    unsigned long long droppedTotal = 0;
    ASSERT_EQ((long)IGPU_CUDA_ERROR_PENDING_MAX, llrpFakeGpuBackendCudaErrorsPendingForTesting(kRender, &droppedTotal));
    ASSERT_EQ(1ull, droppedTotal);
    llrpFakeGpuBackendInjectRunFailureForTesting(-1, 702, "cudaErrorLaunchTimeout");
    ASSERT_EQ(0, runOnce(2, &rc));
    ASSERT_EQ(IGPU_CUDA_ERROR_PENDING_MAX + 1, (int)g_calls.size());
    for (int i = 0; i < IGPU_CUDA_ERROR_PENDING_MAX; ++i)
    {
        ASSERT_EQ(std::string("orphan"), g_calls[i].role);
        ASSERT_EQ(1002 + i, g_calls[i].cudaCode);
        ASSERT_EQ(i == 0 ? 1ull : 0ull, g_calls[i].dropped);
    }
    const SinkCall & run = g_calls[IGPU_CUDA_ERROR_PENDING_MAX];
    ASSERT_EQ(std::string("recon:run"), run.stage);
    ASSERT_EQ(702, run.cudaCode);
    ASSERT_EQ(std::string("primary"), run.role);
    ASSERT_EQ(0ull, run.dropped);

    g_calls.clear();
    llrpFakeGpuBackendInjectRunFailureForTesting(-1, 702, "cudaErrorLaunchTimeout");
    ASSERT_EQ(0, runOnce(3, &rc));
    ASSERT_EQ(1, (int)g_calls.size());
    ASSERT_EQ(0ull, g_calls[0].dropped);
    ASSERT_EQ(0L, llrpFakeGpuBackendCudaErrorsPendingForTesting(kRender, &droppedTotal));
    ASSERT_EQ(1ull, droppedTotal); // monotonic
}
