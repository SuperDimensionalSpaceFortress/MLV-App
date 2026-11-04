// CUDA-RECON-DUALISO-TIMER-SPLIT-1: the per-run split of the dual_iso timer on the CUDA route
// (llrawproc.h llrpDualIsoGpuSplitTiming_t), through the real llrawproc_gpu_recon_run_backend on the
// in-process fake backend. (a) one run is one backend call whose run wall splits into the backend's own
// GPU span (the fake reports total_ms 3.0) plus a floored sync; (b) a run queued on the backend mutex (held
// here, as the render thread's present or another run would hold it) reports that wait as mutex_wait_ms;
// (e) so does a queued run then refused by the suppression re-check under the lock (adversary r1 A1); (c) the
// thread's reset zeroes the record; (d) a run refused before the lock leaves the record untouched; (f) a run
// whose caller asks for no timing (the export paths) still splits the GPU span (adversary r1 pass 3 A1); (g) that
// run's own slow timing read is booked as post_run, not left to the enclosing residual (adversary r1 passes 4-5 A1).
// A2 (adversary r1 A2): (b) and (e) bound the wait by the hold the parent measured after it saw the run blocked
// in the mutex wait, not by a fixed holdMs/2, so a deschedule between the arrival and the timer cannot fail them.
#include "../common/minitest.h"
#include "../../src/mlv/llrawproc/llrawproc.h"

#include <windows.h>

#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <thread>
#include <vector>

extern "C" int llrpInstallFakeGpuPlaybackReconBackendForTesting(int install);
extern "C" int llrpResetGpuExportBackendForTesting(void);
extern "C" void llrpHoldGpuReconBackendMutexForTesting(int hold);
extern "C" long llrpGpuReconBackendLockArrivalsForTesting(void);
extern "C" void llrpSetFakeGpuBackendLastTimingDelayMsForTesting(int delay_ms);

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

// Adversary r1 pass 3 A1: the export callers pass no timing_out. The split still books the backend's own GPU
// span (the fake's 3.0 ms) and the floored sync; r1 left run_gpu_ms 0 and sync_ms unbooked for such a call.
TEST(DualIsoGpuSplitTiming, ARunWhoseCallerAsksForNoTimingStillSplitsTheGpuSpan)
{
    FakeBackendScope scope;
    llrpResetLastRunTimingForCurrentThread();
    int rc = -99;
    const llrpGpuPlaybackReconState_t state = splitValidatedState(6, 4, 4);
    std::vector<uint16_t> input(16, 100);
    std::vector<uint16_t> output(input.size());
    ASSERT_NE(0, llrpGpuPlaybackReconRunCpu16Probe(&state, input.data(), input.size() * sizeof(uint16_t),
                                                   output.data(), &rc, NULL));
    ASSERT_EQ(0, rc);
    const llrpDualIsoGpuSplitTiming_t s = lastSplit();
    ASSERT_EQ(1, s.backend_calls);
    ASSERT_EQ(3.0, s.run_gpu_ms);
    const double expectedSync = s.run_wall_ms > 3.0 ? s.run_wall_ms - 3.0 : 0.0;
    ASSERT_TRUE(s.sync_ms >= expectedSync - 1e-9 && s.sync_ms <= expectedSync + 1e-9);
}

// Adversary r1 passes 4-5 A1 (folded in r2): with no timing_out the split reads the backend's timing itself. That
// read is held work after the run, so a slow getter (the fake sleeps 60 ms) is booked to post_run_ms. r1 took the
// held-end stamp before the read, so post_run_ms stayed near 0 and the 60 ms fell into the enclosing other_ms.
TEST(DualIsoGpuSplitTiming, ASlowTimingReadWithNoCallerTimingIsBookedAsPostRun)
{
    FakeBackendScope scope;
    llrpResetLastRunTimingForCurrentThread();
    const int delayMs = 60;
    int rc = -99;
    const llrpGpuPlaybackReconState_t state = splitValidatedState(7, 4, 4);
    std::vector<uint16_t> input(16, 100);
    std::vector<uint16_t> output(input.size());
    llrpSetFakeGpuBackendLastTimingDelayMsForTesting(delayMs);
    const int used = llrpGpuPlaybackReconRunCpu16Probe(&state, input.data(), input.size() * sizeof(uint16_t),
                                                       output.data(), &rc, NULL);
    llrpSetFakeGpuBackendLastTimingDelayMsForTesting(0);
    ASSERT_NE(0, used);
    ASSERT_EQ(0, rc);
    const llrpDualIsoGpuSplitTiming_t s = lastSplit();
    ASSERT_EQ(1, s.backend_calls);
    ASSERT_EQ(3.0, s.run_gpu_ms);
    // Sleep(60) lasts at least most of 60 ms; the bound asks for half, so it does not hinge on timer granularity.
    ASSERT_TRUE(s.post_run_ms >= delayMs / 2.0);
}

namespace
{

// CUDA-RECON-DUALISO-TIMER-SPLIT-1-A2 (adversary r1 A2, passes 1-5). The run counts its arrival just BEFORE it
// starts its wait timer (llrawproc.c run_backend: InterlockedIncrement, then mlv_stage_timing_now, then
// pthread_mutex_lock). A hold the parent starts at the arrival can therefore run out while the run is
// descheduled between those two lines, and a correct split then reports ~0 wait: a fixed holdMs/2 bound
// false-fails it. So the parent starts the hold it measures only once it SEES the run blocked in the mutex
// wait, which is after the timer started, and the bound is that measured hold: every ms of it is inside the
// run's [timer start, acquire] interval, whatever the schedule.
enum class HoldStart
{
    AtArrival,           // fork/master before A2: hold from the arrival count, bound holdMs / 2
    AtBlockedInTheWait   // A2: hold from the run seen blocked in the mutex wait, bound the measured hold
};
// The RED build of A2 (qmake "DEFINES+=DUALISO_SPLIT_TIMING_A2_RED") restores the fork/master rendezvous and
// bound, so the deterministic timeline test below fails on it; every other build models the A2 rendezvous.
#if defined(DUALISO_SPLIT_TIMING_A2_RED)
constexpr HoldStart kModelHoldStart = HoldStart::AtArrival;
#else
constexpr HoldStart kModelHoldStart = HoldStart::AtBlockedInTheWait;
#endif
// The live contention tests use it too, except where the thread-state probe below has no layout (it is x64 only;
// adversary A2 r1 pass 1 A3): such a build keeps the fork/master rendezvous rather than fail on a probe it lacks.
#if defined(_WIN64)
constexpr HoldStart kHoldStart = kModelHoldStart;
#else
constexpr HoldStart kHoldStart = HoldStart::AtArrival;
#endif

// Both stamps below and the split use QueryPerformanceCounter (mlv_stage_timing_now, StageTiming.h), so the
// slack covers only the double rounding of the ms conversions, not a second clock.
constexpr double kQpcSlackMs = 0.01;

// How long the parent waits to see the arrived run blocked in the mutex wait before it gives up (adversary A2 r1
// pass 1 A2). The test needs SOME liveness bound; this one only fails a run kept off the CPU for a whole minute.
constexpr int kSeenBlockedDeadlineSeconds = 60;

double qpcNowMs()
{
    static LARGE_INTEGER freq = {};
    if (freq.QuadPart == 0) QueryPerformanceFrequency(&freq);
    LARGE_INTEGER now;
    QueryPerformanceCounter(&now);
    return double(now.QuadPart) * 1000.0 / double(freq.QuadPart);
}

// The lower bound a correct split owes a queued run's mutex_wait_ms under rendezvous `start`.
bool queuedWaitBoundMet(HoldStart start, double mutexWaitMs, double measuredHoldMs, int requestedHoldMs)
{
    if (start == HoldStart::AtArrival) return mutexWaitMs >= requestedHoldMs / 2.0;
    return mutexWaitMs >= measuredHoldMs - kQpcSlackMs;
}

#if defined(_WIN64)
// NtQuerySystemInformation(SystemProcessInformation) records, x64 layout (the documented winternl.h
// SYSTEM_PROCESS_INFORMATION, followed by NumberOfThreads SYSTEM_THREAD_INFORMATION records).
struct NtThreadRecord
{
    LARGE_INTEGER kernelTime;
    LARGE_INTEGER userTime;
    LARGE_INTEGER createTime;
    ULONG waitTime;
    PVOID startAddress;
    HANDLE uniqueProcess;
    HANDLE uniqueThread;
    LONG priority;
    LONG basePriority;
    ULONG contextSwitches;
    ULONG threadState;
    ULONG waitReason;
};
static_assert(sizeof(NtThreadRecord) == 80, "SYSTEM_THREAD_INFORMATION is 80 bytes on x64");
constexpr size_t kNtProcessRecordBytes = 256;      // SYSTEM_PROCESS_INFORMATION on x64
constexpr size_t kNtProcessUniqueIdOffset = 80;    // its UniqueProcessId
constexpr ULONG kNtThreadStateWaiting = 5;         // KTHREAD_STATE Waiting
// KWAIT_REASON of a voluntary user-mode wait (winpthreads' mutex waits on an event: UserRequest). Suspended,
// page-in and the other involuntary reasons are not accepted: only the mutex wait proves the timer started.
bool isUserWaitReason(ULONG reason) { return reason == 6 || reason == 13 || reason == 37; }

typedef LONG(WINAPI * NtQuerySystemInformationFn)(ULONG, PVOID, ULONG, PULONG);
#endif

// True when thread `tid` of this process is blocked in a user-mode wait right now. The only wait the queued run
// can enter after its arrival count is the backend mutex's (mlv_stage_timing_now never blocks), so after the
// arrival this is "the run is in pthread_mutex_lock, its wait timer already started".
// noexcept, and the snapshot buffer is sized by a first call made before the parent takes the mutex (adversary A2
// r1 pass 2 A2): an allocation failure is "not seen blocked", never a throw past the held mutex and the thread.
bool threadBlockedInAUserWait(DWORD tid, ULONG * waitReasonOut) noexcept
{
#if defined(_WIN64)
    static const NtQuerySystemInformationFn query = reinterpret_cast<NtQuerySystemInformationFn>(
        reinterpret_cast<void *>(GetProcAddress(GetModuleHandleW(L"ntdll.dll"), "NtQuerySystemInformation")));
    if (!query) return false;
    static std::vector<unsigned char> buffer;
    ULONG needed = 0;
    LONG status = LONG(0xC0000004);
    try
    {
        if (buffer.empty()) buffer.resize(size_t(4) << 20);
        status = query(5 /* SystemProcessInformation */, buffer.data(), ULONG(buffer.size()), &needed);
        while (status == LONG(0xC0000004) /* STATUS_INFO_LENGTH_MISMATCH */)
        {
            buffer.resize(size_t(needed) + (size_t(1) << 20));
            status = query(5, buffer.data(), ULONG(buffer.size()), &needed);
        }
    }
    catch (...)
    {
        return false;
    }
    if (status != 0) return false;
    const DWORD pid = GetCurrentProcessId();
    size_t offset = 0;
    for (;;)
    {
        const unsigned char * proc = buffer.data() + offset;
        ULONG next = 0, threads = 0;
        HANDLE procId = NULL;
        memcpy(&next, proc, sizeof(next));
        memcpy(&threads, proc + 4, sizeof(threads));
        memcpy(&procId, proc + kNtProcessUniqueIdOffset, sizeof(procId));
        if (DWORD(reinterpret_cast<ULONG_PTR>(procId)) == pid)
        {
            for (ULONG i = 0; i < threads; ++i)
            {
                NtThreadRecord t;
                memcpy(&t, proc + kNtProcessRecordBytes + size_t(i) * sizeof(NtThreadRecord), sizeof(t));
                if (DWORD(reinterpret_cast<ULONG_PTR>(t.uniqueThread)) != tid) continue;
                if (waitReasonOut) *waitReasonOut = t.waitReason;
                return t.threadState == kNtThreadStateWaiting && isUserWaitReason(t.waitReason);
            }
            return false;
        }
        if (next == 0) return false;
        offset += next;
    }
#else
    (void)tid;
    (void)waitReasonOut;
    return false;
#endif
}

struct QueuedRun
{
    bool arrived = false;
    bool seenBlocked = false;     // the parent saw the run blocked in the mutex wait before it started the hold
    ULONG lastWaitReason = 0;     // the run's KWAIT_REASON at the parent's last look (diagnostic)
    double measuredHoldMs = 0.0;  // parent, QPC: hold start (kHoldStart) .. just before the unlock
    double releaseAtMs = 0.0;     // parent, QPC: just before the unlock (the run acquires after it)
    double callEndAtMs = 0.0;     // run thread, QPC: just after splitRunOnce returned
    double callWallMs = 0.0;      // run thread, QPC: just before splitRunOnce .. just after it
    int used = -1;
    int rc = -99;
    llrpDualIsoGpuSplitTiming_t split = {};
};

// One run queued on the backend mutex held here for holdMs (measured on QPC) after kHoldStart. suppressWhileQueued
// delivers a device-loss suppression while it waits, so the run is refused by the re-check under the lock
// (adversary r1 A1).
QueuedRun runQueuedOnTheBackendMutex(uint64_t frameId, int holdMs, bool suppressWhileQueued)
{
    QueuedRun out;
    std::atomic<bool> done{false};
    std::atomic<DWORD> runTid{0};
    if (kHoldStart == HoldStart::AtBlockedInTheWait)
        (void)threadBlockedInAUserWait(GetCurrentThreadId(), NULL); // sizes the snapshot buffer before the hold
    const long arrivalsBefore = llrpGpuReconBackendLockArrivalsForTesting();
    llrpHoldGpuReconBackendMutexForTesting(1);
    std::thread queued([&]() {
        runTid.store(GetCurrentThreadId());
        llrpResetLastRunTimingForCurrentThread();
        const double callStart = qpcNowMs();
        out.used = splitRunOnce(frameId, &out.rc);
        out.callEndAtMs = qpcNowMs();
        out.callWallMs = out.callEndAtMs - callStart;
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
    if (kHoldStart == HoldStart::AtBlockedInTheWait)
    {
        // However long the run is descheduled after its arrival, the hold does not start until it is in the wait.
        const auto blockedDeadline = std::chrono::steady_clock::now() + std::chrono::seconds(kSeenBlockedDeadlineSeconds);
        while (out.arrived && !(out.seenBlocked = threadBlockedInAUserWait(runTid.load(), &out.lastWaitReason))
               && std::chrono::steady_clock::now() < blockedDeadline)
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    const double holdStart = qpcNowMs();
    while (qpcNowMs() - holdStart < double(holdMs)) // a Sleep that returns early cannot shorten the hold
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    if (suppressWhileQueued) llrpSetGpuCudaSuppressed(1);
    out.releaseAtMs = qpcNowMs(); // the run cannot acquire before the unlock below
    out.measuredHoldMs = out.releaseAtMs - holdStart;
    llrpHoldGpuReconBackendMutexForTesting(0);
    queued.join();
    if (suppressWhileQueued) llrpSetGpuCudaSuppressed(0);
    return out;
}

// The wait is not booked again as configure, run or post-run time. Those three intervals start at the acquire,
// which is after the parent's release stamp, and end before the call returns, so they fit in [release stamp, call
// end] under any schedule; a 60 ms wait booked into any of them overflows that window unless the run is itself
// held off the CPU for as long between its return and its end stamp (adversary A2 r1 pass 1 A1: a missed catch
// then, never a false failure). The whole split (with the wait) also fits the call's own wall.
bool heldIntervalsFitTheCall(const QueuedRun & q)
{
    const double afterAcquire = q.split.configure_ms + q.split.run_wall_ms + q.split.post_run_ms;
    return afterAcquire <= q.callEndAtMs - q.releaseAtMs + kQpcSlackMs
           && q.split.mutex_wait_ms + afterAcquire <= q.callWallMs + kQpcSlackMs;
}

// The A2 rendezvous needs one attempt. fork/master's three attempts are kept only for the AtArrival fallback
// (adversary A2 r1 pass 2 A1), where a deschedule between the arrival and the timer can fail one attempt.
QueuedRun runQueuedUntilBoundMet(uint64_t firstFrameId, int holdMs, bool suppressWhileQueued)
{
    const int attempts = kHoldStart == HoldStart::AtArrival ? 3 : 1;
    QueuedRun q;
    for (int attempt = 0; attempt < attempts; ++attempt)
    {
        q = runQueuedOnTheBackendMutex(firstFrameId + uint64_t(attempt), holdMs, suppressWhileQueued);
        if (queuedWaitBoundMet(kHoldStart, q.split.mutex_wait_ms, q.measuredHoldMs, holdMs) && heldIntervalsFitTheCall(q))
            break;
    }
    return q;
}

} // namespace

// Adversary r1 A2, deterministic: the schedule the adversary named (holdMs 60; the run descheduled for 70 ms
// between its arrival count and its wait-timer start), on a timeline model of the parent's rendezvous. Times in
// ms from the arrival. The parent can only see the run blocked once its timer has started; the run acquires at
// the later of the unlock and its own timer start, and a correct split books acquire - timer start.
TEST(DualIsoGpuSplitTiming, ADescheduleBetweenArrivalAndTheWaitTimerCannotFailTheContentionBound)
{
    const int holdMs = 60;
    for (double descheduledMs : {0.0, 10.0, 31.0, 59.0, 70.0, 500.0})
    {
        const double arrival = 0.0;
        const double timerStart = arrival + descheduledMs;
        const double holdStart = kModelHoldStart == HoldStart::AtBlockedInTheWait ? timerStart : arrival;
        const double unlock = holdStart + holdMs;
        const double acquire = unlock > timerStart ? unlock : timerStart;
        const double correctWaitMs = acquire - timerStart;
        const bool met = queuedWaitBoundMet(kModelHoldStart, correctWaitMs, unlock - holdStart, holdMs);
        if (!met)
            fprintf(stderr, "    deschedule %.0f ms: correct mutex_wait_ms %.3f fails the bound\n",
                    descheduledMs, correctWaitMs);
        ASSERT_TRUE(met);
    }
    // ...and the bound still rejects a split that never times the wait (0) or books half of it.
    ASSERT_FALSE(queuedWaitBoundMet(kModelHoldStart, 0.0, double(holdMs), holdMs));
    if (kModelHoldStart == HoldStart::AtBlockedInTheWait)
        ASSERT_FALSE(queuedWaitBoundMet(kModelHoldStart, holdMs / 2.0, double(holdMs), holdMs));
}

TEST(DualIsoGpuSplitTiming, AWaitOnTheBackendMutexIsReportedAsMutexWait)
{
    FakeBackendScope scope;
    const int holdMs = 60;
    // One attempt: the bound is the hold measured after the run was seen in the wait, sound under any schedule.
    const QueuedRun q = runQueuedUntilBoundMet(10, holdMs, false);
    ASSERT_TRUE(q.arrived);
    if (!q.seenBlocked) fprintf(stderr, "    run never seen in a user wait (last KWAIT_REASON %lu)\n", q.lastWaitReason);
    ASSERT_TRUE(q.seenBlocked || kHoldStart == HoldStart::AtArrival);
    ASSERT_NE(0, q.used);
    ASSERT_EQ(0, q.rc);
    ASSERT_EQ(1, q.split.backend_calls);
    fprintf(stderr, "    measured hold %.3f ms, mutex_wait_ms %.3f, call wall %.3f\n", q.measuredHoldMs,
            q.split.mutex_wait_ms, q.callWallMs);
    ASSERT_TRUE(queuedWaitBoundMet(kHoldStart, q.split.mutex_wait_ms, q.measuredHoldMs, holdMs));
    // ...and the wait is not booked again as run, configure or post-run time.
    ASSERT_TRUE(heldIntervalsFitTheCall(q));
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
    const QueuedRun q = runQueuedUntilBoundMet(20, holdMs, true);
    ASSERT_TRUE(q.arrived);
    if (!q.seenBlocked) fprintf(stderr, "    run never seen in a user wait (last KWAIT_REASON %lu)\n", q.lastWaitReason);
    ASSERT_TRUE(q.seenBlocked || kHoldStart == HoldStart::AtArrival);
    ASSERT_EQ(0, q.used);
    ASSERT_EQ(LLRP_GPU_PLAYBACK_RECON_RC_CUDA_SUPPRESSED, q.rc);
    ASSERT_EQ(1, q.split.backend_calls);
    ASSERT_EQ(0.0, q.split.run_wall_ms);
    fprintf(stderr, "    measured hold %.3f ms, mutex_wait_ms %.3f, call wall %.3f\n", q.measuredHoldMs,
            q.split.mutex_wait_ms, q.callWallMs);
    ASSERT_TRUE(queuedWaitBoundMet(kHoldStart, q.split.mutex_wait_ms, q.measuredHoldMs, holdMs));
    ASSERT_TRUE(heldIntervalsFitTheCall(q));
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
