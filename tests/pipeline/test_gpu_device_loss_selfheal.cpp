// GPU-DEVICE-LOSS-SELF-HEAL-1: the device-loss breaker through the real llrawproc seam, on the
// in-process fake backend, with GPU-FIELD-TELEMETRY-1's real recon stage sink installed
// (GpuFieldTelemetry::appCloseForTesting installs it whatever MLVAPP_GPU_TELEMETRY says; this binary
// runs with telemetry OFF, see test_gpu_field_telemetry_capture.cpp). (a)/(b) a run failing with
// 702 / 719 reaches the fault listener, CUDA is suppressed at once, and the next frame's
// run_backend refuses with LLRP_GPU_PLAYBACK_RECON_RC_CUDA_SUPPRESSED without calling the backend;
// the queued GUI half then records the loss in the (isolated) settings store, shows the session
// notice and asks for one refresh. (c) a validation failure (no CUDA code) suppresses nothing.
// (d) with no fault the flag stays clear and every frame reaches the backend. (e) two losses on
// the same profile, as two launches within 10 minutes, set the persisted safe-start flag.
// r6 (CoS #437, guard under the serializing lock): (f) a frame queued on the backend mutex while a loss is
// delivered never reaches the backend; (g) a device-loss orphan delivered by an AMaZE run's telemetry entry
// stops that very run. GPU-LOSS-EMIT-BEFORE-UNLOCK-1: (h) a caller that takes the backend mutex between the
// faulting run's (or preupload's) unlock and its emit to the sink never reaches the backend; (i) a failure
// that is not a device loss leaves that caller free.
#include "../common/minitest.h"
#include "../../src/mlv/llrawproc/llrawproc.h"
#include "../../platform/qt/AutomationSettings.h"
#include "../../platform/qt/GpuDebayer.h"
#include "../../platform/qt/GpuDeviceLossBreaker.h"
#include "../../platform/qt/GpuDeviceLossBreakerCore.h"
#include "../../platform/qt/GpuFieldTelemetry.h"

#include <QCoreApplication>
#include <QSettings>
#include <QString>
#include <QStringList>
#include <QTemporaryDir>

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstring>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

extern "C" int llrpInstallFakeGpuPlaybackReconBackendForTesting(int install);
extern "C" void llrpHoldGpuReconBackendMutexForTesting(int hold);
extern "C" long llrpGpuReconBackendLockArrivalsForTesting(void);
extern "C" void llrpSetGpuReconAfterUnlockHookForTesting(void (*hook)(void));
extern "C" int llrpResetGpuExportBackendForTesting(void);
extern "C" void llrpFakeGpuBackendInjectRunFailureForTesting(int rc, int cuda_code, const char * name);
extern "C" void llrpFakeGpuBackendInjectPreuploadFailureForTesting(int rc, int cuda_code, const char * name);
extern "C" void llrpFakeGpuBackendReducedIsoNotchStateForTesting(int * last_run_flag,
                                                                 uint64_t * runs_with_notch,
                                                                 uint64_t * runs_without_notch,
                                                                 uint64_t * set_calls);

namespace
{
namespace core = GpuDeviceLossBreakerCore;

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

// One played frame through the C seam: non-zero = the GPU recon was used.
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

// Successful fake backend runs so far (an injected failure returns before it counts).
uint64_t fakeRunCount()
{
    uint64_t withNotch = 0, withoutNotch = 0;
    llrpFakeGpuBackendReducedIsoNotchStateForTesting(nullptr, &withNotch, &withoutNotch, nullptr);
    return withNotch + withoutNotch;
}

struct Seen
{
    int deviceLossFaults = 0;
    int otherFaults = 0;
    int lastCode = 0;
    int suppressedAfterListener = -1;
    int failedRunUsed = -1;
    int failedRunRc = 0;
    int nextRunUsed = -1;
    int nextRunRc = 0;
    uint64_t runsBeforeNext = 0;
    uint64_t runsAfterNext = 0;
};

// One case: an isolated settings store, the fake backend, the breaker reset, a listener that
// records what reached it and hands it to the breaker. The destructor runs the queued GUI half
// while the store is still isolated (never the native one), then undoes everything.
struct CaseScope
{
    QTemporaryDir settingsDir;
    QTemporaryDir logsDir;
    Seen seen;
    std::vector<QString> notices;
    int refreshes = 0;

    explicit CaseScope(const QString & sharedSettingsDir = QString())
    {
        automation_settings::isolate(sharedSettingsDir.isEmpty() ? settingsDir.path() : sharedSettingsDir);
        GpuDeviceLossBreaker::resetForTesting();
        // As install() does: the backend suppresses under its lock on a captured device-loss code.
        llrpSetGpuDeviceLossClassifier(&GpuDeviceLossBreaker::classifyDeviceLossCodeForBackend);
        llrpInstallFakeGpuPlaybackReconBackendForTesting(1);
        GpuDeviceLossBreaker::setNoticeSink([this](const QString & text) { notices.push_back(text); });
        GpuDeviceLossBreaker::setRefreshCallback([this]() { ++refreshes; });
        GpuFieldTelemetry::setFaultListener([this](const GpuFieldTelemetry::CudaFault & fault) {
            if (GpuFieldTelemetry::isDeviceLossCode(fault.cudaCode)) ++seen.deviceLossFaults;
            else ++seen.otherFaults;
            seen.lastCode = fault.cudaCode;
            GpuDeviceLossBreaker::onFault(fault);
            seen.suppressedAfterListener = llrpGpuCudaSuppressed();
        });
    }
    ~CaseScope()
    {
        runQueuedGuiHalf();
        llrpSetGpuReconAfterUnlockHookForTesting(nullptr);
        GpuFieldTelemetry::setFaultListener(nullptr);
        GpuDeviceLossBreaker::resetForTesting(); // also clears the classifier
        llrpInstallFakeGpuPlaybackReconBackendForTesting(0);
        llrpResetGpuExportBackendForTesting();
        automation_settings::releaseForTest();
    }
    void runQueuedGuiHalf() { QCoreApplication::sendPostedEvents(); }

    // The telemetry sink is installed only inside appCloseForTesting; no assert runs in there.
    void withTelemetrySink(const std::function<void()> & body)
    {
        GpuFieldTelemetry::appCloseForTesting(logsDir.path(), body, nullptr);
    }

    // A healthy first frame, then a run failing with `code`, then the next frame.
    void lossThenNextFrame(int code, const char * name)
    {
        withTelemetrySink([this, code, name]() {
            int rc = -99;
            runOnce(1, &rc);
            llrpFakeGpuBackendInjectRunFailureForTesting(-1, code, name);
            seen.failedRunUsed = runOnce(2, &seen.failedRunRc);
            seen.runsBeforeNext = fakeRunCount();
            seen.nextRunUsed = runOnce(3, &seen.nextRunRc);
            seen.runsAfterNext = fakeRunCount();
        });
    }

    QStringList recentLosses()
    {
        const std::unique_ptr<QSettings> s = automation_settings::openAppSettings();
        return s->value(QString::fromLatin1(core::kKeyRecentLosses)).toStringList();
    }
    bool safeStartEnabled()
    {
        const std::unique_ptr<QSettings> s = automation_settings::openAppSettings();
        return s->value(QString::fromLatin1(core::kKeySafeStartEnabled), false).toBool();
    }
};

void assertDeviceLossSuppresses(int code, const char * name)
{
    CaseScope scope;
    ASSERT_EQ(0, llrpGpuCudaSuppressed());
    scope.lossThenNextFrame(code, name);
    const Seen & s = scope.seen;
    ASSERT_EQ(1, s.deviceLossFaults);
    ASSERT_EQ(code, s.lastCode);
    ASSERT_EQ(1, s.suppressedAfterListener);       // set inside the listener, on the faulting thread
    ASSERT_EQ(1, llrpGpuCudaSuppressed());
    ASSERT_EQ(0, s.failedRunUsed);                 // the faulting frame's GPU recon was not used
    ASSERT_EQ(-1, s.failedRunRc);
    ASSERT_EQ(0, s.nextRunUsed);                   // the next frame never reaches the backend
    ASSERT_EQ(LLRP_GPU_PLAYBACK_RECON_RC_CUDA_SUPPRESSED, s.nextRunRc);
    ASSERT_EQ(s.runsBeforeNext, s.runsAfterNext);  // the fake run count did not move

    // The queued GUI half: one recent loss, the session notice, one refresh; no flag yet.
    ASSERT_EQ(0, (int)scope.notices.size());       // nothing ran before the event loop did
    scope.runQueuedGuiHalf();
    ASSERT_EQ(1, (int)scope.notices.size());
    ASSERT_EQ(core::noticeText(core::kNoticeSession, name), scope.notices[0].toStdString());
    ASSERT_EQ(scope.notices[0].toStdString(), GpuDeviceLossBreaker::currentNotice().toStdString());
    ASSERT_EQ(1, scope.refreshes);
    ASSERT_EQ(1, (int)scope.recentLosses().size());
    ASSERT_FALSE(scope.safeStartEnabled());
}
} // namespace

TEST(GpuDeviceLossSelfHeal, LaunchTimeout702SuppressesCudaBeforeTheNextFrame)
{
    assertDeviceLossSuppresses(702, "cudaErrorLaunchTimeout");
}

TEST(GpuDeviceLossSelfHeal, LaunchFailure719SuppressesCudaBeforeTheNextFrame)
{
    assertDeviceLossSuppresses(719, "cudaErrorLaunchFailure");
}

TEST(GpuDeviceLossSelfHeal, ValidationFailureDoesNotSuppress)
{
    CaseScope scope;
    scope.withTelemetrySink([&scope]() {
        int rc = -99;
        runOnce(1, &rc);
        llrpFakeGpuBackendInjectRunFailureForTesting(3, 0, nullptr);
        scope.seen.failedRunUsed = runOnce(2, &scope.seen.failedRunRc);
        scope.seen.runsBeforeNext = fakeRunCount();
        scope.seen.nextRunUsed = runOnce(3, &scope.seen.nextRunRc);
        scope.seen.runsAfterNext = fakeRunCount();
    });
    ASSERT_EQ(0, scope.seen.deviceLossFaults);
    ASSERT_EQ(1, scope.seen.otherFaults);          // the listener did fire, with cuda_code 0
    ASSERT_EQ(0, scope.seen.lastCode);
    ASSERT_EQ(0, llrpGpuCudaSuppressed());
    ASSERT_EQ(3, scope.seen.failedRunRc);
    ASSERT_NE(0, scope.seen.nextRunUsed);          // the backend recovers on the next frame
    ASSERT_EQ(0, scope.seen.nextRunRc);
    ASSERT_EQ(scope.seen.runsBeforeNext + 1, scope.seen.runsAfterNext);
    scope.runQueuedGuiHalf();
    ASSERT_EQ(0, (int)scope.notices.size());
    ASSERT_EQ(0, (int)scope.recentLosses().size());
}

TEST(GpuDeviceLossSelfHeal, NoFaultLeavesTheFlagClearAndEveryFrameReachesTheBackend)
{
    CaseScope scope;
    const int frames = 12;
    uint64_t before = 0, after = 0;
    int used = 0;
    scope.withTelemetrySink([&]() {
        before = fakeRunCount();
        for (int i = 0; i < frames; ++i)
        {
            int rc = -99;
            if (runOnce(uint64_t(i + 1), &rc) && rc == 0) ++used;
        }
        after = fakeRunCount();
    });
    ASSERT_EQ(0, llrpGpuCudaSuppressed());
    ASSERT_EQ(frames, used);
    ASSERT_EQ(before + uint64_t(frames), after);
    ASSERT_EQ(0, scope.seen.deviceLossFaults + scope.seen.otherFaults);
}

TEST(GpuDeviceLossSelfHeal, TwoLossesOnTheSameProfileSetThePersistedFlag)
{
    QTemporaryDir shared;
    ASSERT_TRUE(shared.isValid());
    {
        CaseScope first(shared.path()); // launch 1
        first.lossThenNextFrame(702, "cudaErrorLaunchTimeout");
        first.runQueuedGuiHalf();
        ASSERT_EQ(1, (int)first.recentLosses().size());
        ASSERT_FALSE(first.safeStartEnabled());
    }
    {
        CaseScope second(shared.path()); // launch 2, within 10 minutes, same (unknown) profile
        second.lossThenNextFrame(700, "cudaErrorIllegalAddress");
        second.runQueuedGuiHalf();
        ASSERT_EQ(2, (int)second.recentLosses().size());
        ASSERT_TRUE(second.safeStartEnabled());
        ASSERT_EQ(1, (int)second.notices.size());
        ASSERT_EQ(core::noticeText(core::kNoticeFlagSet, "cudaErrorIllegalAddress"), second.notices[0].toStdString());
    }
}

namespace
{
GpuFieldTelemetry::CudaFault launchTimeout()
{
    GpuFieldTelemetry::CudaFault fault;
    fault.dll = "recon";
    fault.stage = "recon:run";
    fault.rc = -1;
    fault.cudaCode = 702;
    fault.cudaName = "cudaErrorLaunchTimeout";
    fault.site = "another thread's run";
    return fault;
}
} // namespace

// (f) A frame passes the check before the lock while CUDA is fine, then waits on the backend mutex (held here,
// as another frame's run holds it). The loss is delivered while it waits. Once the lock is released it must
// refuse without touching the backend (r1-r5 checked only before the lock: it ran).
TEST(GpuDeviceLossSelfHeal, AFrameQueuedOnTheBackendMutexThroughALossNeverReachesTheBackend)
{
    CaseScope scope;
    int rc = -99;
    int used = -1;
    std::atomic<bool> done{false};
    const uint64_t before = fakeRunCount();
    const long arrivalsBefore = llrpGpuReconBackendLockArrivalsForTesting();
    llrpHoldGpuReconBackendMutexForTesting(1);
    std::thread queued([&]() {
        used = runOnce(7, &rc);
        done.store(true);
    });
    // r10 (CoS #437): a pre-lock rendezvous, not a sleep. The frame counts its arrival after its check before the
    // lock passed and just before it takes the mutex held here; a frame refused or failed earlier never counts.
    // The wait is bounded: a frame that never arrives (or finished) fails the assertion below, never hangs.
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(10);
    while (llrpGpuReconBackendLockArrivalsForTesting() == arrivalsBefore && !done.load()
           && std::chrono::steady_clock::now() < deadline)
        std::this_thread::yield();
    const bool waitedOnTheLock = llrpGpuReconBackendLockArrivalsForTesting() == arrivalsBefore + 1 && !done.load();
    GpuDeviceLossBreaker::onFault(launchTimeout()); // what the fault listener does on the faulting thread
    const int suppressedWhileQueued = llrpGpuCudaSuppressed();
    llrpHoldGpuReconBackendMutexForTesting(0);
    queued.join();
    ASSERT_TRUE(waitedOnTheLock);
    ASSERT_EQ(1, suppressedWhileQueued);
    ASSERT_EQ(0, used);
    ASSERT_EQ(LLRP_GPU_PLAYBACK_RECON_RC_CUDA_SUPPRESSED, rc);
    ASSERT_EQ(before, fakeRunCount()); // the backend never ran
}

namespace
{
// A device-loss code the AMaZE DLL noted on the render channel with no call to own it (an orphan): the next
// run's telemetry entry drains it and hands it to the sink, which reaches the breaker on this thread.
struct OrphanLaunchTimeout
{
    static long pending;
    static int drains;
    static int drain(int channel, igpu_cuda_error_t * out, int capacity, unsigned long long * dropped)
    {
        if (dropped) *dropped = 0;
        if (channel != IGPU_CUDA_ERROR_CHANNEL_RENDER || pending == 0 || capacity < 1) return 0;
        ++drains;
        std::memset(out, 0, sizeof(*out));
        out->code = 702;
        std::strcpy(out->name, "cudaErrorLaunchTimeout");
        std::strcpy(out->site, "a kernel launch nothing captured");
        out->count = 1;
        pending = 0;
        return 1;
    }
};
long OrphanLaunchTimeout::pending = 0;
int OrphanLaunchTimeout::drains = 0;
} // namespace

// (g) The AMaZE run step, telemetry entry on, with a device-loss orphan pending: the entry delivers it to the
// breaker (CUDA suppressed on this thread), and the same step then refuses the run. r1-r5 entered and ran.
TEST(GpuDeviceLossSelfHeal, ADeviceLossOrphanDeliveredAtAnAmazeRunsEntryStopsThatRun)
{
    CaseScope scope;
    OrphanLaunchTimeout::pending = 1;
    OrphanLaunchTimeout::drains = 0;
    GpuAmazeCudaErrorSource source;
    source.drain = &OrphanLaunchTimeout::drain;
    source.pending[IGPU_CUDA_ERROR_CHANNEL_RENDER] = reinterpret_cast<const volatile long *>(&OrphanLaunchTimeout::pending);
    bool maySubmit = true;
    int suppressedBefore = -1;
    scope.withTelemetrySink([&]() {
        suppressedBefore = llrpGpuCudaSuppressed();
        maySubmit = gpuAmazeDebayerEnterRunForTesting(source);
    });
    ASSERT_EQ(0, suppressedBefore);
    ASSERT_EQ(1, OrphanLaunchTimeout::drains);
    ASSERT_EQ(1, scope.seen.deviceLossFaults);   // the orphan reached the breaker's listener
    ASSERT_EQ(702, scope.seen.lastCode);
    ASSERT_EQ(1, scope.seen.suppressedAfterListener);
    ASSERT_FALSE(maySubmit);                       // and the run that entered never submits
    // With nothing pending the same step lets the run go.
    {
        CaseScope clean;
        OrphanLaunchTimeout::pending = 0;
        bool cleanMaySubmit = false;
        clean.withTelemetrySink([&]() { cleanMaySubmit = gpuAmazeDebayerEnterRunForTesting(source); });
        ASSERT_TRUE(cleanMaySubmit);
        ASSERT_EQ(0, clean.seen.deviceLossFaults);
        ASSERT_EQ(0, llrpGpuCudaSuppressed());
    }
}

namespace
{
// GPU-LOSS-EMIT-BEFORE-UNLOCK-1: the gap between the faulting call's unlock and its emit, held open by the
// after-unlock test hook on the faulting thread (A) while a second caller (B) plays a frame in it. Shared, so
// B's outputs outlive any timeout; every wait is bounded at 10 s, and B is joined before any assertion.
struct EmitGap
{
    std::mutex mutex;
    std::condition_variable cv;
    bool go = false;
    bool bDone = false;
    std::atomic<bool> armed{false};
    std::atomic<int> fired{0};
    int suppressedInGap = -1;
    bool hookTimedOut = false;
    bool bTimedOut = false;
    int bUsed = -1;
    int bRc = -99;
    uint64_t bRunsBefore = 0;
    uint64_t bRunsAfter = 0;
};

std::shared_ptr<EmitGap> g_emitGap;

// The after-unlock hook. First call only (later calls, e.g. B's own run in the RED arm, return at once).
void holdTheEmitGapOpen()
{
    const std::shared_ptr<EmitGap> gap = g_emitGap;
    if (!gap || !gap->armed.exchange(false)) return;
    gap->fired.fetch_add(1);
    gap->suppressedInGap = llrpGpuCudaSuppressed();
    std::unique_lock<std::mutex> lock(gap->mutex);
    gap->go = true;
    gap->cv.notify_all();
    if (!gap->cv.wait_for(lock, std::chrono::seconds(10), [&gap]() { return gap->bDone; })) gap->hookTimedOut = true;
}

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

// The async H2D preupload of the bytes runOnce renders (non-zero = accepted).
int preuploadOnce(uint64_t frameId)
{
    const std::vector<uint16_t> input(16, 100);
    return llrpGpuPlaybackReconPreuploadFrame(frameId, input.data(), input.size() * sizeof(uint16_t));
}

// A healthy frame 1; then `inject` arms one failure and `faultingCall` runs on this thread (A). Its after-unlock
// hook lets B play frame 9 inside the emit gap. B is joined after A's call returns (B's own wait is bounded).
std::shared_ptr<EmitGap> playAFrameInTheEmitGap(CaseScope & scope, const std::function<void()> & inject,
                                                const std::function<void()> & faultingCall)
{
    const std::shared_ptr<EmitGap> gap = std::make_shared<EmitGap>();
    g_emitGap = gap;
    scope.withTelemetrySink([&]() {
        int rc = -99;
        runOnce(1, &rc);
        std::thread b([gap]() {
            {
                std::unique_lock<std::mutex> lock(gap->mutex);
                if (!gap->cv.wait_for(lock, std::chrono::seconds(10), [&gap]() { return gap->go; }))
                {
                    gap->bTimedOut = true;
                    return;
                }
            }
            gap->bRunsBefore = fakeRunCount();
            gap->bUsed = runOnce(9, &gap->bRc);
            gap->bRunsAfter = fakeRunCount();
            std::lock_guard<std::mutex> lock(gap->mutex);
            gap->bDone = true;
            gap->cv.notify_all();
        });
        llrpSetGpuReconAfterUnlockHookForTesting(&holdTheEmitGapOpen);
        gap->armed.store(true);
        inject();
        faultingCall();
        b.join();
        gap->armed.store(false);
        llrpSetGpuReconAfterUnlockHookForTesting(nullptr);
    });
    g_emitGap.reset();
    return gap;
}

// The device-loss half: the flag was already set inside the gap, B was refused before the backend, and the emit
// after the unlock still reached the listener, so the session record and the notice still happen.
void assertTheEmitGapIsClosedOnALoss(CaseScope & scope, const EmitGap & gap, const char * name)
{
    ASSERT_EQ(1, gap.fired.load());
    ASSERT_FALSE(gap.hookTimedOut);
    ASSERT_FALSE(gap.bTimedOut);
    ASSERT_EQ(1, gap.suppressedInGap);
    ASSERT_EQ(0, gap.bUsed);
    ASSERT_EQ(LLRP_GPU_PLAYBACK_RECON_RC_CUDA_SUPPRESSED, gap.bRc);
    ASSERT_EQ(gap.bRunsBefore, gap.bRunsAfter); // B never reached the backend
    ASSERT_EQ(1, scope.seen.deviceLossFaults);
    ASSERT_EQ(1, llrpGpuCudaSuppressed());
    scope.runQueuedGuiHalf();
    ASSERT_EQ(1, (int)scope.notices.size());
    ASSERT_EQ(core::noticeText(core::kNoticeSession, name), scope.notices[0].toStdString());
    ASSERT_EQ(1, (int)scope.recentLosses().size());
}

void assertANonLossLeavesTheEmitGapFree(int rc, int code, const char * name)
{
    CaseScope scope;
    const std::shared_ptr<EmitGap> gap = playAFrameInTheEmitGap(
        scope, [rc, code, name]() { llrpFakeGpuBackendInjectRunFailureForTesting(rc, code, name); },
        []() { int frameRc = -99; runOnce(2, &frameRc); });
    ASSERT_EQ(1, gap->fired.load());
    ASSERT_FALSE(gap->hookTimedOut);
    ASSERT_FALSE(gap->bTimedOut);
    ASSERT_EQ(0, gap->suppressedInGap);
    ASSERT_NE(0, gap->bUsed);
    ASSERT_EQ(0, gap->bRc);
    ASSERT_EQ(gap->bRunsBefore + 1, gap->bRunsAfter);
    ASSERT_EQ(0, scope.seen.deviceLossFaults);
    ASSERT_EQ(0, llrpGpuCudaSuppressed());
}
} // namespace

// (h) Frame 2's run fails with a launch timeout. Between its unlock and its emit, B takes the backend mutex: on
// master the flag was set only by the emit, so B passed the re-check under the lock and submitted once to a
// dead context. Now the run sets it from the captured code before it unlocks.
TEST(GpuDeviceLossSelfHeal, ACallerEnteringBetweenTheFaultingRunsUnlockAndItsEmitNeverReachesTheBackend)
{
    CaseScope scope;
    const std::shared_ptr<EmitGap> gap = playAFrameInTheEmitGap(
        scope, []() { llrpFakeGpuBackendInjectRunFailureForTesting(-1, 702, "cudaErrorLaunchTimeout"); },
        []() { int rc = -99; runOnce(2, &rc); });
    assertTheEmitGapIsClosedOnALoss(scope, *gap, "cudaErrorLaunchTimeout");
}

// (h) The same gap at the async preupload: its captured device-loss code sets the flag before it unlocks.
TEST(GpuDeviceLossSelfHeal, ACallerEnteringBetweenTheFaultingPreuploadsUnlockAndItsEmitNeverReachesTheBackend)
{
    const AsyncH2dOnScope asyncH2d;
    CaseScope scope;
    int preuploadAccepted = -1;
    const std::shared_ptr<EmitGap> gap = playAFrameInTheEmitGap(
        scope, []() { llrpFakeGpuBackendInjectPreuploadFailureForTesting(-1, 702, "cudaErrorLaunchTimeout"); },
        [&preuploadAccepted]() { preuploadAccepted = preuploadOnce(3); });
    ASSERT_EQ(0, preuploadAccepted);
    assertTheEmitGapIsClosedOnALoss(scope, *gap, "cudaErrorLaunchTimeout");
}

// (i) A validation failure, and a CUDA failure that is not a device loss, suppress nothing in the gap: B runs.
TEST(GpuDeviceLossSelfHeal, ANonLossFailureInTheEmitGapLeavesTheNextCallerFree)
{
    assertANonLossLeavesTheEmitGapFree(3, 0, nullptr);
    assertANonLossLeavesTheEmitGapFree(-1, 2, "cudaErrorMemoryAllocation");
}
