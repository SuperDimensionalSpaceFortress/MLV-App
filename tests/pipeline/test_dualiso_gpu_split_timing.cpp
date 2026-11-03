// CUDA-RECON-DUALISO-TIMER-SPLIT-1: the per-run split of the dual_iso timer on the CUDA route
// (llrawproc.h llrpDualIsoGpuSplitTiming_t), through the real llrawproc_gpu_recon_run_backend on the
// in-process fake backend. (a) one run is one backend call whose run wall splits into the backend's own
// GPU span (the fake reports total_ms 3.0) plus a floored sync; (b) a run queued on the backend mutex (held
// here, as the render thread's present or another run would hold it) reports that wait as mutex_wait_ms;
// (e) so does a queued run then refused by the suppression re-check under the lock (adversary r1 A1); (c) the
// thread's reset zeroes the record; (d) a run refused before the lock leaves the record untouched.
#include "../common/minitest.h"
#include "../../src/mlv/llrawproc/llrawproc.h"

#include <atomic>
#include <chrono>
#include <cstdint>
#include <thread>
#include <vector>

extern "C" int llrpInstallFakeGpuPlaybackReconBackendForTesting(int install);
extern "C" int llrpResetGpuExportBackendForTesting(void);
extern "C" void llrpHoldGpuReconBackendMutexForTesting(int hold);
extern "C" long llrpGpuReconBackendLockArrivalsForTesting(void);

namespace
{

llrpGpuPlaybackReconState_t splitValidatedState(uint64_t frameId, int width, int height)
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

int splitRunOnce(uint64_t frameId, int * rcOut, int width = 4)
{
    const int height = 4;
    const llrpGpuPlaybackReconState_t state = splitValidatedState(frameId, width, height);
    std::vector<uint16_t> input(size_t(width) * size_t(height), 100);
    std::vector<uint16_t> output(input.size());
    llrpGpuPlaybackReconTiming_t timing = {};
    return llrpGpuPlaybackReconRunCpu16Probe(&state, input.data(), input.size() * sizeof(uint16_t),
                                             output.data(), rcOut, &timing);
}

struct FakeBackendScope
{
    FakeBackendScope() { llrpInstallFakeGpuPlaybackReconBackendForTesting(1); }
    ~FakeBackendScope()
    {
        llrpInstallFakeGpuPlaybackReconBackendForTesting(0);
        llrpResetGpuExportBackendForTesting();
    }
};

llrpDualIsoGpuSplitTiming_t lastSplit()
{
    llrpDualIsoGpuSplitTiming_t split = {};
    llrpGetLastDualIsoGpuSplitTiming(&split);
    return split;
}

} // namespace

// Adversary r1 A3: the fake reports a fixed 3.0 ms GPU span however short its host call is, so here the span can
// exceed the wall and sync is floored at 0. The real backend's run blocks on its last event, so its wall covers
// its span and run_wall = run_gpu + sync there; this test pins the floor, not that equality.
TEST(DualIsoGpuSplitTiming, OneRunIsOneBackendCallAndItsWallSplitsIntoGpuSpanPlusFlooredSync)
{
    FakeBackendScope scope;
    llrpResetLastRunTimingForCurrentThread();
    int rc = -99;
    ASSERT_NE(0, splitRunOnce(1, &rc));
    ASSERT_EQ(0, rc);
    const llrpDualIsoGpuSplitTiming_t s = lastSplit();
    ASSERT_EQ(1, s.backend_calls);
    ASSERT_TRUE(s.mutex_wait_ms >= 0.0);
    ASSERT_TRUE(s.configure_ms >= 0.0);
    ASSERT_TRUE(s.post_run_ms >= 0.0);
    ASSERT_TRUE(s.run_wall_ms >= 0.0);
    // The GPU span is the backend's own timing (the fake reports 3.0 ms); sync is the wall beyond it, floored.
    ASSERT_EQ(3.0, s.run_gpu_ms);
    const double expectedSync = s.run_wall_ms > 3.0 ? s.run_wall_ms - 3.0 : 0.0;
    ASSERT_TRUE(s.sync_ms >= expectedSync - 1e-9 && s.sync_ms <= expectedSync + 1e-9);
    // prepare / preupload belong to the texture route's caller, not run_backend.
    ASSERT_EQ(0.0, s.prepare_ms);
    ASSERT_EQ(0.0, s.preupload_ms);

    // A second run on the same thread without a reset accumulates (two backend calls, two GPU spans).
    ASSERT_NE(0, splitRunOnce(2, &rc));
    const llrpDualIsoGpuSplitTiming_t two = lastSplit();
    ASSERT_EQ(2, two.backend_calls);
    ASSERT_EQ(6.0, two.run_gpu_ms);
    ASSERT_TRUE(two.run_wall_ms >= s.run_wall_ms);
}

namespace
{

struct QueuedRun
{
    bool arrived = false;
    int used = -1;
    int rc = -99;
    llrpDualIsoGpuSplitTiming_t split = {};
};

// One run queued on the backend mutex held here for holdMs after its arrival. suppressWhileQueued delivers a
// device-loss suppression while it waits, so the run is refused by the re-check under the lock (adversary r1 A1).
QueuedRun runQueuedOnTheBackendMutex(uint64_t frameId, int holdMs, bool suppressWhileQueued)
{
    QueuedRun out;
    std::atomic<bool> done{false};
    const long arrivalsBefore = llrpGpuReconBackendLockArrivalsForTesting();
    llrpHoldGpuReconBackendMutexForTesting(1);
    std::thread queued([&]() {
        llrpResetLastRunTimingForCurrentThread();
        out.used = splitRunOnce(frameId, &out.rc);
        out.split = lastSplit(); // thread-local: read on the thread that ran
        done.store(true);
    });
    // Pre-lock rendezvous (as test_gpu_device_loss_selfheal.cpp): the run counts its arrival just before it
    // starts the wait timer and takes the mutex held here. Bounded: a run that never arrives fails the caller.
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(10);
    while (llrpGpuReconBackendLockArrivalsForTesting() == arrivalsBefore && !done.load()
           && std::chrono::steady_clock::now() < deadline)
        std::this_thread::yield();
    out.arrived = llrpGpuReconBackendLockArrivalsForTesting() == arrivalsBefore + 1 && !done.load();
    std::this_thread::sleep_for(std::chrono::milliseconds(holdMs));
    if (suppressWhileQueued) llrpSetGpuCudaSuppressed(1);
    llrpHoldGpuReconBackendMutexForTesting(0);
    queued.join();
    if (suppressWhileQueued) llrpSetGpuCudaSuppressed(0);
    return out;
}

} // namespace

TEST(DualIsoGpuSplitTiming, AWaitOnTheBackendMutexIsReportedAsMutexWait)
{
    FakeBackendScope scope;
    const int holdMs = 60;
    // Adversary r1 A2: the run can be descheduled between its arrival and its timer start, or inside a timed
    // step, so the bound is asked of up to three attempts; a real miss fails all three.
    bool met = false;
    for (int attempt = 0; attempt < 3 && !met; ++attempt)
    {
        const QueuedRun q = runQueuedOnTheBackendMutex(uint64_t(10 + attempt), holdMs, false);
        ASSERT_TRUE(q.arrived);
        ASSERT_NE(0, q.used);
        ASSERT_EQ(0, q.rc);
        ASSERT_EQ(1, q.split.backend_calls);
        // Most of the hold, and the wait is not booked as run, configure or post-run time.
        met = q.split.mutex_wait_ms >= holdMs / 2.0
              && q.split.run_wall_ms < q.split.mutex_wait_ms
              && q.split.configure_ms < q.split.mutex_wait_ms
              && q.split.post_run_ms < q.split.mutex_wait_ms;
    }
    ASSERT_TRUE(met);
    // This thread ran no backend call, so its own record shows none.
    llrpResetLastRunTimingForCurrentThread();
    ASSERT_EQ(0, lastSplit().backend_calls);
}

// Adversary r1 A1: a run that waited on the mutex and was then refused by the suppression re-check under it
// still reports that wait (r1 returned before booking it: mutex_wait_ms 0 after a 60 ms wait).
TEST(DualIsoGpuSplitTiming, ARunRefusedUnderTheLockStillReportsItsWait)
{
    FakeBackendScope scope;
    const int holdMs = 60;
    bool met = false;
    for (int attempt = 0; attempt < 3 && !met; ++attempt)
    {
        const QueuedRun q = runQueuedOnTheBackendMutex(uint64_t(20 + attempt), holdMs, true);
        ASSERT_TRUE(q.arrived);
        ASSERT_EQ(0, q.used);
        ASSERT_EQ(LLRP_GPU_PLAYBACK_RECON_RC_CUDA_SUPPRESSED, q.rc);
        ASSERT_EQ(1, q.split.backend_calls);
        ASSERT_EQ(0.0, q.split.run_wall_ms);
        met = q.split.mutex_wait_ms >= holdMs / 2.0;
    }
    ASSERT_TRUE(met);
    ASSERT_EQ(0, llrpGpuCudaSuppressed());
}

TEST(DualIsoGpuSplitTiming, TheThreadResetZeroesTheRecord)
{
    FakeBackendScope scope;
    llrpResetLastRunTimingForCurrentThread();
    int rc = -99;
    ASSERT_NE(0, splitRunOnce(4, &rc));
    ASSERT_EQ(1, lastSplit().backend_calls);
    llrpResetLastRunTimingForCurrentThread();
    const llrpDualIsoGpuSplitTiming_t s = lastSplit();
    ASSERT_EQ(0, s.backend_calls);
    ASSERT_EQ(0.0, s.mutex_wait_ms);
    ASSERT_EQ(0.0, s.configure_ms);
    ASSERT_EQ(0.0, s.run_wall_ms);
    ASSERT_EQ(0.0, s.run_gpu_ms);
    ASSERT_EQ(0.0, s.sync_ms);
    ASSERT_EQ(0.0, s.post_run_ms);
}

TEST(DualIsoGpuSplitTiming, ARunRefusedBeforeTheLockLeavesTheRecordUntouched)
{
    FakeBackendScope scope;
    llrpResetLastRunTimingForCurrentThread();
    int rc = -99;
    // An input size that does not match width x height is refused before the backend mutex.
    const llrpGpuPlaybackReconState_t state = splitValidatedState(5, 4, 4);
    std::vector<uint16_t> input(8, 100);
    std::vector<uint16_t> output(input.size());
    llrpGpuPlaybackReconTiming_t timing = {};
    ASSERT_EQ(0, llrpGpuPlaybackReconRunCpu16Probe(&state, input.data(), input.size() * sizeof(uint16_t),
                                                   output.data(), &rc, &timing));
    const llrpDualIsoGpuSplitTiming_t s = lastSplit();
    ASSERT_EQ(0, s.backend_calls);
    ASSERT_EQ(0.0, s.mutex_wait_ms);
    ASSERT_EQ(0.0, s.run_wall_ms);
}
