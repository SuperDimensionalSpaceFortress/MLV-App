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
// call, as a real failure branch does; NoteCudaErrorForTesting notes at once, between calls. (5) r6:
// AMaZE gets the same drain-all capture and entry orphans (GpuDebayer.h's seam over this binary's
// slot), and a code noted after the last captured call is drained by the GL reset (view release)
// or by session end, once.
#include "../common/minitest.h"
#include "../../src/mlv/llrawproc/llrawproc.h"
#include "../../platform/qt/GpuDebayer.h"
#include "../../platform/qt/GpuFieldTelemetry.h"
#include "../../platform/qt/GpuFieldTelemetryCore.h"
#include "../../tools/gpu/backend/igpu_cuda_error_slot.h"

#include <QByteArray>
#include <QDir>
#include <QFile>
#include <QTemporaryDir>

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdint>
#include <cstring>
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
extern "C" int llrpFakeGpuBackendFailCreateForTesting(void);
extern "C" void llrpHoldGpuReconBackendMutexForTesting(int hold);

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

// r6: the AMaZE seam (GpuDebayer.h) driven over this binary's own slot, which is the same
// igpu_cuda_error_slot.h the AMaZE DLL exports, and a recording AMaZE sink.
std::vector<SinkCall> g_amazeCalls;

void amazeRecordingSink(const char * stage, int rc, double ms, int cudaCode, const char * cudaName,
                        const char * site, const char * role, unsigned long long dropped)
{
    g_amazeCalls.push_back({stage ? stage : "", rc, ms, cudaCode, cudaName ? cudaName : "", site ? site : "",
                            role ? role : "", dropped});
}

int slotDrain(int channel, igpu_cuda_error_t * out, int capacity, unsigned long long * dropped)
{
    return cuda_slot::drain(channel, out, capacity, dropped);
}

int slotLast(igpu_cuda_error_t * out)
{
    return cuda_slot::copy_last(out);
}

GpuAmazeCudaErrorSource slotSource()
{
    GpuAmazeCudaErrorSource source;
    source.drain = &slotDrain;
    source.pending[kRender] = cuda_slot::pending_word(kRender);
    source.pending[kPreupload] = cuda_slot::pending_word(kPreupload);
    source.last = &slotLast;
    return source;
}

struct AmazeSinkScope
{
    GpuAmazeCudaErrorSource source = slotSource();
    AmazeSinkScope()
    {
        drainSlot();
        g_amazeCalls.clear();
        gpuAmazeDebayerSetStageTelemetrySink(&amazeRecordingSink);
        gpuAmazeDebayerSetCudaErrorSourceForTesting(&source);
    }
    ~AmazeSinkScope()
    {
        gpuAmazeDebayerSetCudaErrorSourceForTesting(nullptr);
        gpuAmazeDebayerSetStageTelemetrySink(nullptr);
        g_amazeCalls.clear();
    }
};
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

// r6 B1 (sol r5 blocker 1): on the cached direct-RGBA output path, cudaGraphicsMapResources
// fails with 700 (gl_rgba16_direct:map) and the cleanup's reset_cached_gl_resource then notes 709
// (gl_cache:unregister), both inside one failing AMaZE call. The capture emits two records for that
// call: 700 primary at the map site, then 709 secondary at the unregister site, and nothing is
// left pending. A later validation-only failure reports code 0. On 0f199cea AMaZE read only the
// last note: one record, 709, and 700 was never delivered.
TEST(GpuFieldTelemetryCapture, AmazeFailedCallReportsTheMapCodeAndItsCleanupCodeOnce)
{
    const AmazeSinkScope amaze;
    // The call's entry: nothing pending, nothing emitted.
    ASSERT_EQ(0, gpuAmazeCudaErrorsEmitOrphans(amaze.source, 1, &amazeRecordingSink));
    ASSERT_EQ(0, (int)g_amazeCalls.size());
    cuda_slot::note(700, "cudaErrorIllegalAddress", "gl_rgba16_direct:map");
    cuda_slot::note(709, "cudaErrorContextIsDestroyed", "gl_cache:unregister");
    ASSERT_EQ(2, gpuAmazeCudaErrorsEmitFailure(amaze.source, "amaze:device_bayer16", 2, &amazeRecordingSink));
    ASSERT_EQ(2, (int)g_amazeCalls.size());
    ASSERT_EQ(std::string("amaze:device_bayer16"), g_amazeCalls[0].stage);
    ASSERT_EQ(2, g_amazeCalls[0].rc);
    ASSERT_NEAR(-1.0, g_amazeCalls[0].ms, 1e-12);
    ASSERT_EQ(700, g_amazeCalls[0].cudaCode);
    ASSERT_EQ(std::string("cudaErrorIllegalAddress"), g_amazeCalls[0].cudaName);
    ASSERT_EQ(std::string("gl_rgba16_direct:map"), g_amazeCalls[0].site);
    ASSERT_EQ(std::string("primary"), g_amazeCalls[0].role);
    ASSERT_EQ(0ull, g_amazeCalls[0].dropped);
    ASSERT_EQ(std::string("amaze:device_bayer16"), g_amazeCalls[1].stage);
    ASSERT_EQ(2, g_amazeCalls[1].rc);
    ASSERT_EQ(709, g_amazeCalls[1].cudaCode);
    ASSERT_EQ(std::string("cudaErrorContextIsDestroyed"), g_amazeCalls[1].cudaName);
    ASSERT_EQ(std::string("gl_cache:unregister"), g_amazeCalls[1].site);
    ASSERT_EQ(std::string("secondary"), g_amazeCalls[1].role);
    // Nothing pending after the capture.
    ASSERT_EQ(0L, (long)*cuda_slot::pending_word(kRender));
    igpu_cuda_error_t out[cuda_slot::kPending];
    ASSERT_EQ(0, cuda_slot::drain(kRender, out, cuda_slot::kPending, nullptr));

    // A later validation-only failure: one primary record with code 0, never 709 again.
    g_amazeCalls.clear();
    ASSERT_EQ(1, gpuAmazeCudaErrorsEmitFailure(amaze.source, "amaze:r16_gl", 3, &amazeRecordingSink));
    ASSERT_EQ(1, (int)g_amazeCalls.size());
    ASSERT_EQ(0, g_amazeCalls[0].cudaCode);
    ASSERT_EQ(std::string(), g_amazeCalls[0].site);
    ASSERT_EQ(std::string("primary"), g_amazeCalls[0].role);

    // No sink: nothing drained or emitted, the code waits for an installed sink.
    g_amazeCalls.clear();
    cuda_slot::note(700, "cudaErrorIllegalAddress", "gl_rgba16_direct:map");
    ASSERT_EQ(0, gpuAmazeCudaErrorsEmitFailure(amaze.source, "amaze:r16_gl", 2, nullptr));
    ASSERT_EQ(0, gpuAmazeCudaErrorsEmitOrphans(amaze.source, 2, nullptr));
    ASSERT_EQ(1L, (long)*cuda_slot::pending_word(kRender));
}

// r6 B1, the r5 entry rule for AMaZE: a code noted by an AMaZE call nothing captures (the GL reset's
// gl_cache:unregister, a create's check_cuda) is taken at the next call's entry and emitted as stage
// amaze:uncaptured, rc 0, role orphan, with its own site; that call's own validation failure then
// reports code 0, never the orphan. Nine uncaptured notes drop one: the first orphan carries
// dropped=1. On 0f199cea the next failed call was credited with the leftover 709.
TEST(GpuFieldTelemetryCapture, AmazeUncapturedCodeIsAnOrphanAtTheNextEntry)
{
    const AmazeSinkScope amaze;
    cuda_slot::note(709, "cudaErrorContextIsDestroyed", "gl_cache:unregister");
    ASSERT_EQ(1, gpuAmazeCudaErrorsEmitOrphans(amaze.source, 1, &amazeRecordingSink));
    ASSERT_EQ(1, gpuAmazeCudaErrorsEmitFailure(amaze.source, "amaze:r16_gl", 3, &amazeRecordingSink));
    ASSERT_EQ(2, (int)g_amazeCalls.size());
    ASSERT_EQ(std::string("amaze:uncaptured"), g_amazeCalls[0].stage);
    ASSERT_EQ(0, g_amazeCalls[0].rc);
    ASSERT_EQ(709, g_amazeCalls[0].cudaCode);
    ASSERT_EQ(std::string("gl_cache:unregister"), g_amazeCalls[0].site);
    ASSERT_EQ(std::string("orphan"), g_amazeCalls[0].role);
    ASSERT_EQ(std::string("amaze:r16_gl"), g_amazeCalls[1].stage);
    ASSERT_EQ(3, g_amazeCalls[1].rc);
    ASSERT_EQ(0, g_amazeCalls[1].cudaCode);
    ASSERT_EQ(std::string("primary"), g_amazeCalls[1].role);

    g_amazeCalls.clear();
    for (int i = 1; i <= cuda_slot::kPending + 1; ++i)
        cuda_slot::note(1000 + i, "cudaErrorUnknown", "gl_cache:unregister");
    ASSERT_EQ(cuda_slot::kPending, gpuAmazeCudaErrorsEmitOrphans(amaze.source, 1, &amazeRecordingSink));
    ASSERT_EQ(cuda_slot::kPending, (int)g_amazeCalls.size());
    for (int i = 0; i < cuda_slot::kPending; ++i)
    {
        ASSERT_EQ(std::string("orphan"), g_amazeCalls[i].role);
        ASSERT_EQ(1002 + i, g_amazeCalls[i].cudaCode);
        ASSERT_EQ(i == 0 ? 1ull : 0ull, g_amazeCalls[i].dropped);
    }
    ASSERT_EQ(0L, (long)*cuda_slot::pending_word(kRender));
}

// r6 B2 (sol r5 blocker 2): the GPU view closes after its last render and the GL reset's
// cudaSetDevice notes 702 (reset_gl:set_device). No captured call follows. The view-release drain
// inside llrpGpuPlaybackReconResetGlTextureResources (called by GpuDisplayWindow and
// GpuDisplayViewport destroyTexture) emits it as one recon:uncaptured orphan with its own site, and
// session end then emits nothing more: one record, not two. The AMaZE GL reset drains the same way.
// On 0f199cea the note stayed queued and session end never drained it: zero records.
TEST(GpuFieldTelemetryCapture, GlResetCodeAfterTheLastRenderIsDrainedAtViewRelease)
{
    const FakeGpuBackendScope fake;
    const SinkScope sink;
    const AmazeSinkScope amaze;
    int rc = -99;
    ASSERT_NE(0, runOnce(1, &rc)); // the last render
    ASSERT_EQ(0, rc);
    g_calls.clear();

    llrpFakeGpuBackendArmCudaNoteForTesting(kRender, 702, "cudaErrorLaunchTimeout", "reset_gl:set_device");
    cuda_slot::note(709, "cudaErrorContextIsDestroyed", "gl_cache:unregister"); // the AMaZE reset's
    gpuAmazeDebayerResetR16TextureBackendResources();
    ASSERT_EQ(1, llrpGpuPlaybackReconResetGlTextureResources());
    ASSERT_EQ(1, (int)g_calls.size());
    ASSERT_EQ(std::string("recon:uncaptured"), g_calls[0].stage);
    ASSERT_EQ(0, g_calls[0].rc);
    ASSERT_EQ(702, g_calls[0].cudaCode);
    ASSERT_EQ(std::string("cudaErrorLaunchTimeout"), g_calls[0].cudaName);
    ASSERT_EQ(std::string("reset_gl:set_device"), g_calls[0].site);
    ASSERT_EQ(std::string("orphan"), g_calls[0].role);
    ASSERT_EQ(0L, llrpFakeGpuBackendCudaErrorsPendingForTesting(kRender, nullptr));
    ASSERT_EQ(1, (int)g_amazeCalls.size());
    ASSERT_EQ(std::string("amaze:uncaptured"), g_amazeCalls[0].stage);
    ASSERT_EQ(709, g_amazeCalls[0].cudaCode);
    ASSERT_EQ(std::string("gl_cache:unregister"), g_amazeCalls[0].site);
    ASSERT_EQ(std::string("orphan"), g_amazeCalls[0].role);

    // Session end after the view release: nothing left, nothing repeated.
    GpuFieldTelemetry::sessionEndForTesting();
    ASSERT_EQ(1, (int)g_calls.size());
    ASSERT_EQ(1, (int)g_amazeCalls.size());
    ASSERT_EQ(0L, llrpFakeGpuBackendCudaErrorsPendingForTesting(kRender, nullptr));
}

// r6 B2, the session-end leg: the same 702 at reset_gl:set_device with the view-release drain
// skipped (the note is queued with no call around it). Session end's terminal drain, which runs
// before the backend is unloaded, emits exactly one orphan record for it, and one for the AMaZE
// DLL's leftover; a second session end emits nothing. On 0f199cea sessionEnd flushed app records
// only and the note was discarded with the DLL: zero records.
TEST(GpuFieldTelemetryCapture, CodeLeftAfterTheLastCallIsDrainedBySessionEnd)
{
    const FakeGpuBackendScope fake;
    const SinkScope sink;
    const AmazeSinkScope amaze;
    int rc = -99;
    ASSERT_NE(0, runOnce(1, &rc));
    g_calls.clear();

    llrpFakeGpuBackendNoteCudaErrorForTesting(kRender, 702, "cudaErrorLaunchTimeout", "reset_gl:set_device");
    cuda_slot::note(999, "cudaErrorUnknown", "cudaGetDeviceCount(&deviceCount)"); // an AMaZE create's
    GpuFieldTelemetry::sessionEndForTesting();
    ASSERT_EQ(1, (int)g_calls.size());
    ASSERT_EQ(std::string("recon:uncaptured"), g_calls[0].stage);
    ASSERT_EQ(702, g_calls[0].cudaCode);
    ASSERT_EQ(std::string("reset_gl:set_device"), g_calls[0].site);
    ASSERT_EQ(std::string("orphan"), g_calls[0].role);
    ASSERT_EQ(0L, llrpFakeGpuBackendCudaErrorsPendingForTesting(kRender, nullptr));
    ASSERT_EQ(1, (int)g_amazeCalls.size());
    ASSERT_EQ(std::string("amaze:uncaptured"), g_amazeCalls[0].stage);
    ASSERT_EQ(999, g_amazeCalls[0].cudaCode);
    ASSERT_EQ(std::string("cudaGetDeviceCount(&deviceCount)"), g_amazeCalls[0].site);
    ASSERT_EQ(std::string("orphan"), g_amazeCalls[0].role);
    ASSERT_EQ(0L, (long)*cuda_slot::pending_word(kRender));

    GpuFieldTelemetry::sessionEndForTesting();
    ASSERT_EQ(1, (int)g_calls.size());
    ASSERT_EQ(1, (int)g_amazeCalls.size());
}

// r6 class sweep: the backend release (a DLL switch) calls the DLL's destroy, whose GL cache reset can
// note gl_cache:unregister, and then frees the DLL. The release keeps that code before FreeLibrary;
// the next terminal drain emits it once as an orphan with its own site, with no backend loaded. On
// 0f199cea the code was freed with the DLL: zero records.
TEST(GpuFieldTelemetryCapture, ReleasedBackendCodeIsKeptAndDrainedAtTeardown)
{
    const SinkScope sink;
    {
        const FakeGpuBackendScope fake;
        int rc = -99;
        ASSERT_NE(0, runOnce(1, &rc));
        g_calls.clear();
        llrpFakeGpuBackendArmCudaNoteForTesting(kRender, 999, "cudaErrorUnknown", "gl_cache:unregister");
    } // the release: destroy notes 999, then the DLL is freed
    ASSERT_EQ(0, (int)g_calls.size());
    ASSERT_EQ(1, llrpGpuPlaybackReconDrainCudaErrorsAtTeardown(0));
    ASSERT_EQ(1, (int)g_calls.size());
    ASSERT_EQ(std::string("recon:uncaptured"), g_calls[0].stage);
    ASSERT_EQ(0, g_calls[0].rc);
    ASSERT_EQ(999, g_calls[0].cudaCode);
    ASSERT_EQ(std::string("gl_cache:unregister"), g_calls[0].site);
    ASSERT_EQ(std::string("orphan"), g_calls[0].role);
    ASSERT_EQ(0, llrpGpuPlaybackReconDrainCudaErrorsAtTeardown(0));
    ASSERT_EQ(1, (int)g_calls.size());
}

// r6 class sweep (create:device_count): igpu_recon_create notes cudaGetDeviceCount's code and returns
// NULL, so the backend is marked unavailable and its DLL freed before any call is captured. The
// release keeps the code although there is no backend; the terminal drain emits it once as an
// orphan with its own site. Before r6b the release kept codes only with a backend, and on 0f199cea
// it kept nothing: zero records.
TEST(GpuFieldTelemetryCapture, CreateFailureCodeIsKeptAndDrainedAtTeardown)
{
    const SinkScope sink;
    const FakeGpuBackendScope fake;
    llrpFakeGpuBackendArmCudaNoteForTesting(kRender, 100, "cudaErrorNoDevice", "create:device_count");
    ASSERT_EQ(1, llrpFakeGpuBackendFailCreateForTesting());
    ASSERT_EQ(0, (int)g_calls.size());
    ASSERT_EQ(1, llrpGpuPlaybackReconDrainCudaErrorsAtTeardown(0));
    ASSERT_EQ(1, (int)g_calls.size());
    ASSERT_EQ(std::string("recon:uncaptured"), g_calls[0].stage);
    ASSERT_EQ(0, g_calls[0].rc);
    ASSERT_EQ(100, g_calls[0].cudaCode);
    ASSERT_EQ(std::string("cudaErrorNoDevice"), g_calls[0].cudaName);
    ASSERT_EQ(std::string("create:device_count"), g_calls[0].site);
    ASSERT_EQ(std::string("orphan"), g_calls[0].role);
    ASSERT_EQ(0, llrpGpuPlaybackReconDrainCudaErrorsAtTeardown(0));
    ASSERT_EQ(1, (int)g_calls.size());
}

// r6 B2: the recon terminal drain takes nothing with no sink installed (the code waits), and drains
// a code exactly once.
TEST(GpuFieldTelemetryCapture, TerminalDrainNeedsASinkAndTakesACodeOnce)
{
    const FakeGpuBackendScope fake;
    int rc = -99;
    ASSERT_NE(0, runOnce(1, &rc));
    llrpFakeGpuBackendNoteCudaErrorForTesting(kRender, 702, "cudaErrorLaunchTimeout", "reset_gl:set_device");
    ASSERT_EQ(0, llrpGpuPlaybackReconDrainCudaErrorsAtTeardown(0)); // no sink
    ASSERT_EQ(1L, llrpFakeGpuBackendCudaErrorsPendingForTesting(kRender, nullptr));
    const SinkScope sink;
    ASSERT_EQ(1, llrpGpuPlaybackReconDrainCudaErrorsAtTeardown(0));
    ASSERT_EQ(1, (int)g_calls.size());
    ASSERT_EQ(0, llrpGpuPlaybackReconDrainCudaErrorsAtTeardown(0));
    ASSERT_EQ(1, (int)g_calls.size());
}

namespace
{
// r7: the lines of the telemetry log appCloseForTesting wrote in `dir`.
std::vector<std::string> logLines(const QTemporaryDir & dir)
{
    std::vector<std::string> lines;
    QFile file(QDir(dir.path()).filePath(QStringLiteral("gpu-field-telemetry.jsonl")));
    if (!file.open(QIODevice::ReadOnly)) return lines;
    for (const QByteArray & line : file.readAll().split('\n'))
        if (!line.isEmpty()) lines.push_back(line.toStdString());
    return lines;
}

bool has(const std::string & line, const char * text)
{
    return line.find(text) != std::string::npos;
}

// Index of every line holding all of `texts`.
std::vector<int> linesWith(const std::vector<std::string> & lines, std::initializer_list<const char *> texts)
{
    std::vector<int> out;
    for (int i = 0; i < (int)lines.size(); ++i)
    {
        bool all = true;
        for (const char * t : texts) all = all && has(lines[i], t);
        if (all) out.push_back(i);
    }
    return out;
}
} // namespace

// r7 B1 (opus r6 blocker): the normal GUI close. aboutToQuit runs session end (terminal drain and the
// final flush) BEFORE ~MainWindow destroys the GPU views; the view's destroyTexture then runs both GL
// resets, and the recon reset's cudaSetDevice notes 702 (reset_gl:set_device) and the AMaZE reset's
// cleanup 709 (gl_cache:unregister). Their r6 view-release drains hand both to the real sinks after
// the final flush. The post routine ~QApplication runs afterwards writes them: the jsonl holds
// exactly one fault line for each, after the session_end flush, in one post_session flush. On
// b7fef9b3 the post routine returned because session end had run: zero fault lines.
TEST(GpuFieldTelemetryCapture, GlResetCodeAfterSessionEndReachesTheLogOnce)
{
    const FakeGpuBackendScope fake;
    const AmazeSinkScope amaze;
    int rc = -99;
    ASSERT_NE(0, runOnce(1, &rc)); // the last render
    QTemporaryDir dir;
    ASSERT_TRUE(dir.isValid());
    GpuFieldTelemetry::appCloseForTesting(dir.path(), nullptr, []() {
        llrpFakeGpuBackendArmCudaNoteForTesting(kRender, 702, "cudaErrorLaunchTimeout", "reset_gl:set_device");
        cuda_slot::note(709, "cudaErrorContextIsDestroyed", "gl_cache:unregister");
        gpuAmazeDebayerResetR16TextureBackendResources();
        ASSERT_EQ(1, llrpGpuPlaybackReconResetGlTextureResources());
    });
    const std::vector<std::string> lines = logLines(dir);
    const std::vector<int> recon = linesWith(lines, {"\"kind\":\"fault\"", "\"site\":\"reset_gl:set_device\""});
    ASSERT_EQ(1, (int)recon.size());
    ASSERT_TRUE(has(lines[recon[0]], "\"dll\":\"recon\""));
    ASSERT_TRUE(has(lines[recon[0]], "\"cuda_code\":702"));
    ASSERT_TRUE(has(lines[recon[0]], "\"role\":\"orphan\""));
    const std::vector<int> amazeFault = linesWith(lines, {"\"kind\":\"fault\"", "\"site\":\"gl_cache:unregister\""});
    ASSERT_EQ(1, (int)amazeFault.size());
    ASSERT_TRUE(has(lines[amazeFault[0]], "\"dll\":\"amaze\""));
    ASSERT_TRUE(has(lines[amazeFault[0]], "\"cuda_code\":709"));
    ASSERT_EQ(2, (int)linesWith(lines, {"\"kind\":\"fault\""}).size());
    const std::vector<int> sessionEnd = linesWith(lines, {"\"kind\":\"flush\"", "\"reason\":\"session_end\""});
    const std::vector<int> post = linesWith(lines, {"\"kind\":\"flush\"", "\"reason\":\"post_session\""});
    ASSERT_EQ(1, (int)sessionEnd.size());
    ASSERT_EQ(1, (int)post.size());
    ASSERT_TRUE(sessionEnd[0] < post[0]);
    ASSERT_TRUE(post[0] < recon[0]);
    ASSERT_EQ(0L, llrpFakeGpuBackendCudaErrorsPendingForTesting(kRender, nullptr));
    ASSERT_EQ(0L, (long)*cuda_slot::pending_word(kRender));
}

// r7 class sweep, teardown orders after session end with no view-release drain: a code left in the
// recon DLL (noted with no call around it) and a code the backend release keeps before FreeLibrary
// (a backend unloaded after session end). The post routine's own drain takes both: one fault line
// each. On b7fef9b3 neither was ever drained or written.
TEST(GpuFieldTelemetryCapture, CodesLeftAfterSessionEndAreDrainedByThePostRoutineOnce)
{
    QTemporaryDir dir;
    ASSERT_TRUE(dir.isValid());
    {
        const FakeGpuBackendScope fake;
        int rc = -99;
        ASSERT_NE(0, runOnce(1, &rc));
        GpuFieldTelemetry::appCloseForTesting(dir.path(), nullptr, []() {
            llrpFakeGpuBackendNoteCudaErrorForTesting(kRender, 719, "cudaErrorLaunchFailure", "reset_gl:set_device");
        });
    }
    std::vector<std::string> lines = logLines(dir);
    ASSERT_EQ(1, (int)linesWith(lines, {"\"kind\":\"fault\"", "\"site\":\"reset_gl:set_device\"", "\"cuda_code\":719"}).size());
    ASSERT_EQ(1, (int)linesWith(lines, {"\"kind\":\"fault\""}).size());

    QTemporaryDir unloadDir;
    ASSERT_TRUE(unloadDir.isValid());
    {
        FakeGpuBackendScope * fake = new FakeGpuBackendScope();
        int rc = -99;
        ASSERT_NE(0, runOnce(1, &rc));
        GpuFieldTelemetry::appCloseForTesting(unloadDir.path(), nullptr, [&fake]() {
            llrpFakeGpuBackendArmCudaNoteForTesting(kRender, 999, "cudaErrorUnknown", "gl_cache:unregister");
            delete fake; // the release: destroy notes 999, kept before FreeLibrary
            fake = nullptr;
        });
        ASSERT_TRUE(fake == nullptr);
    }
    lines = logLines(unloadDir);
    ASSERT_EQ(1, (int)linesWith(lines, {"\"kind\":\"fault\"", "\"site\":\"gl_cache:unregister\"", "\"cuda_code\":999"}).size());
    ASSERT_EQ(1, (int)linesWith(lines, {"\"kind\":\"fault\""}).size());
    const SinkScope sink; // nothing kept is left for a later drain
    ASSERT_EQ(0, llrpGpuPlaybackReconDrainCudaErrorsAtTeardown(0));
    ASSERT_EQ(0, (int)g_calls.size());
}

// r7 class sweep, the earlier orders stay exactly once: a fault noted before session end (the view
// released while the app runs) is written by the session_end flush, and the post routine then
// writes nothing (no post_session flush, no second fault line). A close with no fault at all writes
// no post_session flush either.
TEST(GpuFieldTelemetryCapture, FaultBeforeSessionEndIsWrittenOnceAndThePostRoutineAddsNothing)
{
    QTemporaryDir dir;
    ASSERT_TRUE(dir.isValid());
    {
        const FakeGpuBackendScope fake;
        int rc = -99;
        ASSERT_NE(0, runOnce(1, &rc));
        GpuFieldTelemetry::appCloseForTesting(dir.path(), []() {
            llrpFakeGpuBackendArmCudaNoteForTesting(kRender, 702, "cudaErrorLaunchTimeout", "reset_gl:set_device");
            ASSERT_EQ(1, llrpGpuPlaybackReconResetGlTextureResources());
        }, nullptr);
    }
    std::vector<std::string> lines = logLines(dir);
    const std::vector<int> fault = linesWith(lines, {"\"kind\":\"fault\"", "\"site\":\"reset_gl:set_device\""});
    ASSERT_EQ(1, (int)fault.size());
    ASSERT_EQ(1, (int)linesWith(lines, {"\"kind\":\"fault\""}).size());
    const std::vector<int> sessionEnd = linesWith(lines, {"\"kind\":\"flush\"", "\"reason\":\"session_end\""});
    ASSERT_EQ(1, (int)sessionEnd.size());
    ASSERT_TRUE(sessionEnd[0] < fault[0]);
    ASSERT_EQ(0, (int)linesWith(lines, {"\"reason\":\"post_session\""}).size());

    QTemporaryDir quietDir;
    ASSERT_TRUE(quietDir.isValid());
    GpuFieldTelemetry::appCloseForTesting(quietDir.path(), nullptr, nullptr);
    lines = logLines(quietDir);
    ASSERT_EQ(1, (int)linesWith(lines, {"\"kind\":\"flush\"", "\"reason\":\"session_end\""}).size());
    ASSERT_EQ(0, (int)linesWith(lines, {"\"kind\":\"fault\""}).size());
    ASSERT_EQ(0, (int)linesWith(lines, {"\"reason\":\"post_session\""}).size());
}

namespace
{
// EXIT-DEADLINE-1: appCloseForTesting on another thread, with the backend mutex held by a third (as a
// backend call stuck at close holds it) when `holdLock`. Waits at most `giveUpMs` for the close, then
// releases the lock and joins, so a close that blocks on the mutex fails the caller instead of hanging
// the suite. sessionEndMs: how long session end (aboutToQuit) took; -1 if it never returned in time.
struct TimedClose
{
    bool closedInTime = false;
    long long sessionEndMs = -1;
};

TimedClose closeWhileHoldingTheBackendLock(const QTemporaryDir & dir, bool holdLock, long long giveUpMs)
{
    using Clock = std::chrono::steady_clock;
    std::atomic<bool> held{false};
    std::atomic<bool> release{false};
    std::thread holder([&]() {
        if (holdLock) llrpHoldGpuReconBackendMutexForTesting(1);
        held.store(true);
        while (!release.load()) std::this_thread::sleep_for(std::chrono::milliseconds(1));
        if (holdLock) llrpHoldGpuReconBackendMutexForTesting(0);
    });
    while (!held.load()) std::this_thread::yield();

    std::atomic<long long> sessionEndMs{-1};
    std::atomic<bool> closed{false};
    const QString path = dir.path();
    std::thread closer([&]() {
        Clock::time_point start;
        GpuFieldTelemetry::appCloseForTesting(path, [&start]() { start = Clock::now(); }, [&]() {
            sessionEndMs.store(std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now() - start).count());
        });
        closed.store(true);
    });
    const Clock::time_point giveUp = Clock::now() + std::chrono::milliseconds(giveUpMs);
    while (!closed.load() && Clock::now() < giveUp) std::this_thread::sleep_for(std::chrono::milliseconds(5));
    TimedClose out;
    out.closedInTime = closed.load();
    out.sessionEndMs = out.closedInTime ? sessionEndMs.load() : -1;
    release.store(true);
    holder.join();
    closer.join();
    return out;
}
} // namespace

// EXIT-DEADLINE-1 (fable DESCRIBE-CACHED-1, sol EXIT-JOIN-1): a backend call still inside the recon DLL
// at app close holds the backend mutex. The terminal drain gives up after kTerminalDrainWaitMs; the
// session_end flush must not then wait on that same mutex for the backend description. Session end
// returns inside kSessionEndWaitMs (plus scheduling slack) and still writes its record, without the
// model it could not read. With the lock free the record is unchanged: it carries the backend's model.
// On e8b04c7f sessionEnd's flush called reconDescribe -> llrpGpuPlaybackReconGetBackendInfo, which
// locks the mutex unconditionally: aboutToQuit blocked for as long as the call held it.
TEST(GpuFieldTelemetryCapture, SessionEndReturnsInsideItsBudgetWhileABackendCallHoldsTheLock)
{
    namespace core = GpuFieldTelemetryCore;
    constexpr long long kSlackMs = 250;
    const long long giveUpMs = 2 * core::kSessionEndWaitMs + 1000;
    const FakeGpuBackendScope fake;
    int rc = -99;
    ASSERT_NE(0, runOnce(1, &rc));

    QTemporaryDir heldDir;
    ASSERT_TRUE(heldDir.isValid());
    const TimedClose held = closeWhileHoldingTheBackendLock(heldDir, true, giveUpMs);
    std::printf("[exit-deadline] lock held: closed_in_time=%d session_end_ms=%lld budget_ms=%lld\n",
                held.closedInTime ? 1 : 0, held.sessionEndMs, core::kSessionEndWaitMs + kSlackMs);
    ASSERT_TRUE(held.closedInTime);
    ASSERT_TRUE(held.sessionEndMs >= 0);
    ASSERT_TRUE(held.sessionEndMs <= core::kSessionEndWaitMs + kSlackMs);
    std::vector<std::string> lines = logLines(heldDir);
    ASSERT_EQ(1, (int)linesWith(lines, {"\"kind\":\"flush\"", "\"reason\":\"session_end\""}).size());
    ASSERT_EQ(1, (int)linesWith(lines, {"\"kind\":\"session\"", "\"gpu_model\":\"\""}).size());

    QTemporaryDir freeDir;
    ASSERT_TRUE(freeDir.isValid());
    const TimedClose unheld = closeWhileHoldingTheBackendLock(freeDir, false, giveUpMs);
    ASSERT_TRUE(unheld.closedInTime);
    ASSERT_TRUE(unheld.sessionEndMs <= core::kSessionEndWaitMs + kSlackMs);
    lines = logLines(freeDir);
    ASSERT_EQ(1, (int)linesWith(lines, {"\"kind\":\"flush\"", "\"reason\":\"session_end\""}).size());
    ASSERT_EQ(1, (int)linesWith(lines, {"\"kind\":\"session\"",
                                        "\"gpu_model\":\"fake-test-backend (no GPU, C-seam only)\""}).size());
}

namespace
{
// POST-SESSION-LATE-FAULT-1: holds the session_end write after it took its snapshot and before it
// writes (the after-take test hook), as a write stuck in the file system would. It gives up after
// kStallGiveUpMs, so a regression fails the test instead of hanging the suite.
constexpr long long kStallGiveUpMs = 4 * GpuFieldTelemetryCore::kSessionEndWaitMs + 2000;
std::atomic<bool> g_stallEntered{false};
std::atomic<bool> g_stallRelease{false};
std::atomic<bool> g_stallGaveUp{false};

void stallSessionEndWrite(const char * reason)
{
    if (!reason || std::strcmp(reason, "session_end") != 0) return;
    g_stallEntered.store(true);
    const auto giveUp = std::chrono::steady_clock::now() + std::chrono::milliseconds(kStallGiveUpMs);
    while (!g_stallRelease.load())
    {
        if (std::chrono::steady_clock::now() >= giveUp)
        {
            g_stallGaveUp.store(true);
            return;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
}

struct StallSessionEndWriteScope
{
    StallSessionEndWriteScope()
    {
        g_stallEntered.store(false);
        g_stallRelease.store(false);
        g_stallGaveUp.store(false);
        GpuFieldTelemetry::setAfterTakeForTesting(&stallSessionEndWrite);
    }
    ~StallSessionEndWriteScope()
    {
        g_stallRelease.store(true);
        GpuFieldTelemetry::setAfterTakeForTesting(nullptr);
    }
};

// The recon view's GL reset notes 702 at reset_gl:set_device, as ~MainWindow's destroyTexture does.
void noteTheViewResetFault()
{
    llrpFakeGpuBackendArmCudaNoteForTesting(kRender, 702, "cudaErrorLaunchTimeout", "reset_gl:set_device");
    ASSERT_EQ(1, llrpGpuPlaybackReconResetGlTextureResources());
}

struct StalledClose
{
    bool sessionEndReturned = false;
    bool postRoutineReturned = false;
    long long sessionEndMs = 0;
    long long postRoutineMs = 0;
};

// The GUI close with the session_end write stalled until the post routine has returned. The fault is
// noted after session end (faultAfterSessionEnd: after the stalled write's snapshot, as ~MainWindow
// notes it) or before it (inside that snapshot).
StalledClose closeWithTheSessionEndWriteStalled(const QTemporaryDir & dir, bool faultAfterSessionEnd)
{
    using Clock = std::chrono::steady_clock;
    StalledClose out;
    Clock::time_point start;
    Clock::time_point postStart;
    GpuFieldTelemetry::appCloseForTesting(dir.path(), [&]() {
        start = Clock::now();
        if (!faultAfterSessionEnd) noteTheViewResetFault();
    }, [&]() {
        out.sessionEndMs = std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now() - start).count();
        out.sessionEndReturned = true;
        if (faultAfterSessionEnd) noteTheViewResetFault();
        postStart = Clock::now();
    }, [&]() {
        out.postRoutineMs = std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now() - postStart).count();
        out.postRoutineReturned = true;
        g_stallRelease.store(true);
    });
    return out;
}
} // namespace

// POST-SESSION-LATE-FAULT-1 (CoS post-merge Blocker on #435): session end hands its write to the worker
// and stops waiting when its budget runs out, while the write is still running on the snapshot it took.
// ~MainWindow's GL reset then notes 702. The post routine must queue its post_session flush behind the
// running write and write that fault once, and both exit waits stay inside their budgets. On a1ab71cb
// the post routine returned at waitForDone(0) while the write ran: zero fault lines, no post_session.
TEST(GpuFieldTelemetryCapture, FaultNotedWhileTheSessionEndWriteStallsIsWrittenAndTheCloseStaysInsideItsBudget)
{
    namespace core = GpuFieldTelemetryCore;
    constexpr long long kSlackMs = 250;
    const FakeGpuBackendScope fake;
    int rc = -99;
    ASSERT_NE(0, runOnce(1, &rc));
    QTemporaryDir dir;
    ASSERT_TRUE(dir.isValid());
    StalledClose close;
    {
        const StallSessionEndWriteScope stall;
        close = closeWithTheSessionEndWriteStalled(dir, true);
    }
    std::printf("[late-fault] session_end_ms=%lld budget_ms=%lld post_routine_ms=%lld budget_ms=%lld\n",
                close.sessionEndMs, core::kSessionEndWaitMs + kSlackMs, close.postRoutineMs,
                core::kSessionEndWaitMs + 2 * core::kTerminalDrainWaitMs + kSlackMs);
    ASSERT_TRUE(g_stallEntered.load());
    ASSERT_FALSE(g_stallGaveUp.load());
    ASSERT_TRUE(close.sessionEndReturned);
    ASSERT_TRUE(close.postRoutineReturned);
    ASSERT_TRUE(close.sessionEndMs <= core::kSessionEndWaitMs + kSlackMs);
    ASSERT_TRUE(close.postRoutineMs <= core::kSessionEndWaitMs + 2 * core::kTerminalDrainWaitMs + kSlackMs);
    const std::vector<std::string> lines = logLines(dir);
    const std::vector<int> fault =
        linesWith(lines, {"\"kind\":\"fault\"", "\"site\":\"reset_gl:set_device\"", "\"cuda_code\":702"});
    ASSERT_EQ(1, (int)fault.size());
    ASSERT_EQ(1, (int)linesWith(lines, {"\"kind\":\"fault\""}).size());
    const std::vector<int> sessionEnd = linesWith(lines, {"\"kind\":\"flush\"", "\"reason\":\"session_end\""});
    const std::vector<int> post = linesWith(lines, {"\"kind\":\"flush\"", "\"reason\":\"post_session\""});
    ASSERT_EQ(1, (int)sessionEnd.size());
    ASSERT_EQ(1, (int)post.size());
    ASSERT_TRUE(sessionEnd[0] < post[0]);
    ASSERT_TRUE(post[0] < fault[0]);
}

// POST-SESSION-LATE-FAULT-1 guard: the fault is noted before session end, so the stalled write's
// snapshot holds it. The post routine still queues behind that write (the fault is not committed
// yet), and its queued flush re-checks and writes nothing once the write ahead of it took the fault:
// one fault line after the single session_end flush, and no post_session flush.
TEST(GpuFieldTelemetryCapture, StalledSessionEndWriteWithNothingNewWritesNoPostSessionFlush)
{
    namespace core = GpuFieldTelemetryCore;
    constexpr long long kSlackMs = 250;
    const FakeGpuBackendScope fake;
    int rc = -99;
    ASSERT_NE(0, runOnce(1, &rc));
    QTemporaryDir dir;
    ASSERT_TRUE(dir.isValid());
    StalledClose close;
    {
        const StallSessionEndWriteScope stall;
        close = closeWithTheSessionEndWriteStalled(dir, false);
    }
    ASSERT_TRUE(g_stallEntered.load());
    ASSERT_FALSE(g_stallGaveUp.load());
    ASSERT_TRUE(close.sessionEndReturned);
    ASSERT_TRUE(close.postRoutineReturned);
    ASSERT_TRUE(close.sessionEndMs <= core::kSessionEndWaitMs + kSlackMs);
    ASSERT_TRUE(close.postRoutineMs <= core::kSessionEndWaitMs + 2 * core::kTerminalDrainWaitMs + kSlackMs);
    const std::vector<std::string> lines = logLines(dir);
    const std::vector<int> fault = linesWith(lines, {"\"kind\":\"fault\""});
    ASSERT_EQ(1, (int)fault.size());
    ASSERT_TRUE(has(lines[fault[0]], "\"site\":\"reset_gl:set_device\""));
    const std::vector<int> sessionEnd = linesWith(lines, {"\"kind\":\"flush\"", "\"reason\":\"session_end\""});
    ASSERT_EQ(1, (int)sessionEnd.size());
    ASSERT_TRUE(sessionEnd[0] < fault[0]);
    ASSERT_EQ(0, (int)linesWith(lines, {"\"reason\":\"post_session\""}).size());
}

namespace
{
// r7 B2: the name lookup the DLLs pass (cudaGetErrorName); counts its calls.
int g_nameLookups = 0;
const char * fakeCudaErrorName(int code)
{
    ++g_nameLookups;
    return code == 700 ? "cudaErrorIllegalAddress" : (code == 709 ? "cudaErrorContextIsDestroyed" : "cudaErrorUnknown");
}

struct GlCopySites
{
    const char * copy;
    const char * unmap;
    const char * unregister; // nullptr: recon's cached copy has no unregister
};

const GlCopySites kGlCopies[] = {
    {"gl_r16:copy_to_array", "gl_r16:unmap", nullptr},
    {"gl_r16_in:copy_from_array", "gl_r16_in:unmap", "gl_r16_in:unregister"},
    {"gl_rgba16:copy_to_array", "gl_rgba16:unmap", "gl_rgba16:unregister"},
    {"gl_rgba16_direct:copy_to_array", "gl_rgba16_direct:unmap", "gl_rgba16_direct:unregister"},
    {"gl_rgba16_post_wb:copy_to_array", "gl_rgba16_post_wb:unmap", "gl_rgba16_post_wb:unregister"},
};
} // namespace

// r7 B2 (sol r6 blocker): each of the five GL copies (recon copy_bayer16_to_gl_r16_texture and the
// four AMaZE copies) hands its evaluated copy, unmap and unregister codes to the slot's
// note_evaluated (IGPU_CUDA_ERROR_NOTE_EVALUATED; the console census
// GlCopiesNoteEveryEvaluatedCodeBeforeAnyReturn pins that each copy calls it with these sites before
// any return). Copy 700 with unmap 709 leaves two notes at their own sites, and the failure capture
// reports them as two records: 700 primary at the copy site, 709 secondary at the unmap site. The
// AMaZE unregister case (copy 700, unmap ok, unregister 709) is the same at the unregister site. All
// success notes nothing and looks no name up. On b7fef9b3 the copy failure returned first: one note,
// 709 lost.
TEST(GpuFieldTelemetryCapture, GlCopyNotesEveryEvaluatedCleanupCodeAtItsOwnSite)
{
    for (const GlCopySites & copy : kGlCopies)
    {
        const AmazeSinkScope amaze;
        const bool amazeCopy = copy.unregister != nullptr;
        const cuda_slot::Evaluated unmapFails[] = {{700, copy.copy}, {709, copy.unmap}, {0, copy.unregister}};
        g_nameLookups = 0;
        ASSERT_EQ(0, cuda_slot::note_evaluated(unmapFails, amazeCopy ? 3 : 2, &fakeCudaErrorName));
        ASSERT_EQ(2, g_nameLookups);
        ASSERT_EQ(2L, (long)*cuda_slot::pending_word(kRender));
        ASSERT_EQ(2, gpuAmazeCudaErrorsEmitFailure(amaze.source, "amaze:r16_gl", 2, &amazeRecordingSink));
        ASSERT_EQ(2, (int)g_amazeCalls.size());
        ASSERT_EQ(700, g_amazeCalls[0].cudaCode);
        ASSERT_EQ(std::string("cudaErrorIllegalAddress"), g_amazeCalls[0].cudaName);
        ASSERT_EQ(std::string(copy.copy), g_amazeCalls[0].site);
        ASSERT_EQ(std::string("primary"), g_amazeCalls[0].role);
        ASSERT_EQ(709, g_amazeCalls[1].cudaCode);
        ASSERT_EQ(std::string("cudaErrorContextIsDestroyed"), g_amazeCalls[1].cudaName);
        ASSERT_EQ(std::string(copy.unmap), g_amazeCalls[1].site);
        ASSERT_EQ(std::string("secondary"), g_amazeCalls[1].role);
        ASSERT_EQ(0L, (long)*cuda_slot::pending_word(kRender));

        if (amazeCopy)
        {
            g_amazeCalls.clear();
            const cuda_slot::Evaluated unregisterFails[] = {{700, copy.copy}, {0, copy.unmap}, {709, copy.unregister}};
            ASSERT_EQ(0, cuda_slot::note_evaluated(unregisterFails, 3, &fakeCudaErrorName));
            ASSERT_EQ(2, gpuAmazeCudaErrorsEmitFailure(amaze.source, "amaze:r16_gl", 2, &amazeRecordingSink));
            ASSERT_EQ(2, (int)g_amazeCalls.size());
            ASSERT_EQ(std::string(copy.copy), g_amazeCalls[0].site);
            ASSERT_EQ(709, g_amazeCalls[1].cudaCode);
            ASSERT_EQ(std::string(copy.unregister), g_amazeCalls[1].site);
            ASSERT_EQ(std::string("secondary"), g_amazeCalls[1].role);

            // A cleanup failure alone (the copy succeeded): its code is the call's primary.
            g_amazeCalls.clear();
            const cuda_slot::Evaluated onlyUnmap[] = {{0, copy.copy}, {709, copy.unmap}, {0, copy.unregister}};
            ASSERT_EQ(1, cuda_slot::note_evaluated(onlyUnmap, 3, &fakeCudaErrorName));
            ASSERT_EQ(1, gpuAmazeCudaErrorsEmitFailure(amaze.source, "amaze:r16_gl", 2, &amazeRecordingSink));
            ASSERT_EQ(std::string(copy.unmap), g_amazeCalls[0].site);
            ASSERT_EQ(std::string("primary"), g_amazeCalls[0].role);
        }

        const cuda_slot::Evaluated allOk[] = {{0, copy.copy}, {0, copy.unmap}, {0, copy.unregister}};
        g_nameLookups = 0;
        ASSERT_EQ(-1, cuda_slot::note_evaluated(allOk, amazeCopy ? 3 : 2, &fakeCudaErrorName));
        ASSERT_EQ(0, g_nameLookups);
        ASSERT_EQ(0L, (long)*cuda_slot::pending_word(kRender));
    }
}
