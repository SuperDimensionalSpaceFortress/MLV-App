// PLAYBACK-STARTUP-WAIT-S1-RECON-WARM-RUN-1 Row W: while paused, after the route prewarm, ONE warm request runs the
// paused frame through the exact Play frame-0 prepare-only recon on the recon worker, so Play frame 0 no longer pays the
// first set_clip / set_luts / first-run allocations. These pin when it runs (every guard passes), one Skip per reason,
// the re-post rule (only after real work was in the way, within the post budget), the GUI AMaZE step's gate (this clip,
// paused, with an output), and the log line's fields.
#include "../common/minitest.h"

#include "../../platform/qt/PlaybackReconWarmRunPolicy.h"

#include <string>

using playback_recon_warm_run::Decision;
using playback_recon_warm_run::Reason;
using playback_recon_warm_run::decide;
using playback_recon_warm_run::reasonName;

namespace {

// enabled, playing, clipGenerationMatches, prewarmDecisionRun, backendPreloadOk, cudaSuppressed, realRequestPending
Decision allPass() { return decide(true, false, true, true, true, false, false); }

std::string name(Reason reason) { return std::string(reasonName(reason)); }

} // namespace

TEST(PlaybackReconWarmRunPolicy, RunsOnlyWhenEveryGuardPasses)
{
    const Decision d = allPass();
    ASSERT_TRUE(d.run);
    ASSERT_TRUE(Reason::None == d.reason);
    ASSERT_EQ(std::string("none"), name(d.reason));
}

TEST(PlaybackReconWarmRunPolicy, TheKillSwitchSkipsAsDisabled)
{
    // MLVAPP_PLAYBACK_RECON_WARM_RUN=0 (or MLVAPP_PLAYBACK_ROUTE_PREWARM=0, which MainWindow folds into enabled)
    const Decision d = decide(false, false, true, true, true, false, false);
    ASSERT_FALSE(d.run);
    ASSERT_TRUE(Reason::Disabled == d.reason);
    ASSERT_EQ(std::string("disabled"), name(d.reason));
}

TEST(PlaybackReconWarmRunPolicy, PlaySkipsAsPlaying)
{
    const Decision d = decide(true, true, true, true, true, false, false);
    ASSERT_FALSE(d.run);
    ASSERT_TRUE(Reason::Playing == d.reason);
    ASSERT_EQ(std::string("playing"), name(d.reason));
}

TEST(PlaybackReconWarmRunPolicy, AnotherClipSkipsAsStaleClip)
{
    const Decision d = decide(true, false, false, true, true, false, false);
    ASSERT_FALSE(d.run);
    ASSERT_TRUE(Reason::StaleClip == d.reason);
    ASSERT_EQ(std::string("stale_clip"), name(d.reason));
}

TEST(PlaybackReconWarmRunPolicy, NoPrewarmSkipsAsPrewarmNotRun)
{
    // CPU route, an ineligible route, or MLVAPP_PLAYBACK_ROUTE_PREWARM=0: the prewarm decision was not Run
    const Decision d = decide(true, false, true, false, true, false, false);
    ASSERT_FALSE(d.run);
    ASSERT_TRUE(Reason::PrewarmNotRun == d.reason);
    ASSERT_EQ(std::string("prewarm_not_run"), name(d.reason));
}

TEST(PlaybackReconWarmRunPolicy, NoBackendSkipsAsBackendNotPreloaded)
{
    const Decision d = decide(true, false, true, true, false, false, false);
    ASSERT_FALSE(d.run);
    ASSERT_TRUE(Reason::BackendNotPreloaded == d.reason);
    ASSERT_EQ(std::string("backend_not_preloaded"), name(d.reason));
}

TEST(PlaybackReconWarmRunPolicy, ADeviceLossSkipsAsCudaSuppressed)
{
    const Decision d = decide(true, false, true, true, true, true, false);
    ASSERT_FALSE(d.run);
    ASSERT_TRUE(Reason::CudaSuppressed == d.reason);
    ASSERT_EQ(std::string("cuda_suppressed"), name(d.reason));
}

TEST(PlaybackReconWarmRunPolicy, PendingRealWorkSkipsAsRealRequestPending)
{
    const Decision d = decide(true, false, true, true, true, false, true);
    ASSERT_FALSE(d.run);
    ASSERT_TRUE(Reason::RealRequestPending == d.reason);
    ASSERT_EQ(std::string("real_request_pending"), name(d.reason));
}

TEST(PlaybackReconWarmRunPolicy, TheFirstFailingGuardNamesTheSkip)
{
    // disabled outranks everything; playing outranks a pending request
    ASSERT_TRUE(Reason::Disabled == decide(false, true, false, false, false, true, true).reason);
    ASSERT_TRUE(Reason::Playing == decide(true, true, false, false, false, true, true).reason);
    ASSERT_TRUE(Reason::StaleClip == decide(true, false, false, false, false, true, true).reason);
    ASSERT_TRUE(Reason::CudaSuppressed == decide(true, false, true, true, true, true, true).reason);
}

TEST(PlaybackReconWarmRunPolicy, RenderThreadOutcomesHaveNames)
{
    ASSERT_EQ(std::string("plan_refused"), name(Reason::PlanRefused));
    ASSERT_EQ(std::string("decode_failed"), name(Reason::DecodeFailed));
    ASSERT_EQ(std::string("recon_not_retained"), name(Reason::ReconNotRetained));
    ASSERT_EQ(std::string("pipeline_capture"), name(Reason::PipelineCapture));
    ASSERT_EQ(std::string("snapshot_failed"), name(Reason::SnapshotFailed));
    // PLAYBACK-STARTUP-WAIT-S1-WARM-RECON-DETERMINISM-1 F-a: the shared runtime state changed on both runs; never
    // re-posted (another warm would solve the same unsettled state again)
    ASSERT_EQ(std::string("not_converged"), name(Reason::NotConverged));
    ASSERT_FALSE(playback_recon_warm_run::repostAfterSkip(Reason::NotConverged, 1));
}

TEST(PlaybackReconWarmRunPolicy, OnlyARealRequestSkipIsRepostedWithinTheBudget)
{
    using playback_recon_warm_run::kMaxPosts;
    using playback_recon_warm_run::repostAfterSkip;
    ASSERT_TRUE(repostAfterSkip(Reason::RealRequestPending, 1));
    ASSERT_TRUE(repostAfterSkip(Reason::RealRequestPending, kMaxPosts - 1));
    ASSERT_FALSE(repostAfterSkip(Reason::RealRequestPending, kMaxPosts));
    // a warm that ran, or one refused for any other reason, is never re-posted
    ASSERT_FALSE(repostAfterSkip(Reason::None, 1));
    ASSERT_FALSE(repostAfterSkip(Reason::Playing, 1));
    ASSERT_FALSE(repostAfterSkip(Reason::StaleClip, 1));
    ASSERT_FALSE(repostAfterSkip(Reason::CudaSuppressed, 1));
    ASSERT_FALSE(repostAfterSkip(Reason::ReconNotRetained, 1));
    ASSERT_TRUE(playback_recon_warm_run::kRepostDelayMs > 0);
}

TEST(PlaybackReconWarmRunPolicy, TheGuiAmazeStepNeedsAnOutputOfThisClipWhilePaused)
{
    using playback_recon_warm_run::guiAmazeAllowed;
    ASSERT_TRUE(guiAmazeAllowed(true, false, true));
    // a warm-done that arrives while playing, for an older clip, or without an output: release only
    ASSERT_FALSE(guiAmazeAllowed(true, true, true));
    ASSERT_FALSE(guiAmazeAllowed(true, false, false));
    ASSERT_FALSE(guiAmazeAllowed(false, false, true));
}

TEST(PlaybackReconWarmRunPolicy, TheLogLineCarriesEveryField)
{
    playback_recon_warm_run::Line line;
    line.run = true;
    line.reason = Reason::None;
    line.scale = 1;
    line.width = 1808;
    line.height = 2268;
    line.renderMs = 151.25;
    line.setClipMs = 29.0;
    line.setLutsMs = 25.3;
    line.runMs = 120.5;
    line.amazeOk = true;
    line.amazeKernelMs = 118.1;
    line.released = true;
    line.outstandingAfter = 0;
    line.overlappedPlay = false;
    ASSERT_EQ(std::string("playback_recon_warm decision=run reason=none scale=1 width=1808 height=2268 "
                          "render_ms=151.250 set_clip_ms=29.000 set_luts_ms=25.300 run_ms=120.500 amaze_ok=1 "
                          "amaze_kernel_ms=118.100 released=1 outstanding_after=0 overlapped_play=0"),
              line.format().toStdString());
}

TEST(PlaybackReconWarmRunPolicy, ASkipLineSaysSkipAndWhy)
{
    playback_recon_warm_run::Line line;
    line.reason = Reason::RealRequestPending;
    line.scale = 2;
    line.released = true;
    line.overlappedPlay = true;
    const std::string text = line.format().toStdString();
    ASSERT_TRUE(text.find("decision=skip reason=real_request_pending scale=2 ") != std::string::npos);
    ASSERT_TRUE(text.find(" amaze_ok=0 ") != std::string::npos);
    ASSERT_TRUE(text.find(" overlapped_play=1") != std::string::npos);
}
