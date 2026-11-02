// PLAYBACK-STARTUP-WAIT-S1-FIRST-PRESENT-SETUP-1 Row B: llrpGpuPlaybackReconPreloadBackend() loads and creates the
// playback recon backend while paused (MainWindow::runPlaybackRoutePrewarm), through the same mutex-guarded loader and
// prefer_playback_dll=1 the playback runs use, so the first Play frame's render thread no longer pays it. Through the
// real llrawproc C code: with the in-process fake installed it reports available; without it, it really attempts the
// load, and on a machine without a usable CUDA backend (the board VM, CI) it reports unavailable and stays so.
#include "../common/minitest.h"
#include "../../src/mlv/llrawproc/llrawproc.h"

extern "C" int llrpInstallFakeGpuPlaybackReconBackendForTesting(int install);
extern "C" int llrpResetGpuExportBackendForTesting(void);
extern "C" int llrpGpuExportBackendAttemptedForTesting(void);
extern "C" int llrpGpuExportBackendUnavailableForTesting(void);

namespace
{

struct BackendResetScope
{
    BackendResetScope() { llrpResetGpuExportBackendForTesting(); }
    ~BackendResetScope()
    {
        llrpInstallFakeGpuPlaybackReconBackendForTesting(0);
        llrpResetGpuExportBackendForTesting();
    }
};

} // namespace

TEST(GpuPlaybackReconPreload, WithTheFakeBackendThePreloadReportsAvailable)
{
    BackendResetScope scope;
    ASSERT_EQ(1, llrpInstallFakeGpuPlaybackReconBackendForTesting(1));
    double loadMs = -1.0;
    ASSERT_EQ(1, llrpGpuPlaybackReconPreloadBackend(&loadMs));
    ASSERT_TRUE(loadMs >= 0.0);
    ASSERT_EQ(1, llrpGpuExportBackendAttemptedForTesting());
    ASSERT_EQ(0, llrpGpuExportBackendUnavailableForTesting());
    // a second preload (or the first Play run) finds it already loaded
    ASSERT_EQ(1, llrpGpuPlaybackReconPreloadBackend(nullptr));
}

TEST(GpuPlaybackReconPreload, WithoutTheFakeItReallyAttemptsTheLoadAndReportsTheOutcome)
{
    BackendResetScope scope;
    ASSERT_EQ(0, llrpGpuExportBackendAttemptedForTesting());
    double loadMs = -1.0;
    const int available = llrpGpuPlaybackReconPreloadBackend(&loadMs);
    ASSERT_TRUE(loadMs >= 0.0);
    ASSERT_EQ(1, llrpGpuExportBackendAttemptedForTesting());
    // the board VM and CI have no NVIDIA device: unavailable, and the loader remembers it (no reload per call)
    ASSERT_EQ(available ? 0 : 1, llrpGpuExportBackendUnavailableForTesting());
    ASSERT_EQ(available, llrpGpuPlaybackReconPreloadBackend(nullptr));
}

TEST(GpuPlaybackReconPreload, ResetLeavesNothingAttempted)
{
    {
        BackendResetScope scope;
        llrpGpuPlaybackReconPreloadBackend(nullptr);
        ASSERT_EQ(1, llrpGpuExportBackendAttemptedForTesting());
    }
    ASSERT_EQ(0, llrpGpuExportBackendAttemptedForTesting());
}
