#include "../common/minitest.h"

#include "../../platform/qt/PlaybackAchievedScalePolicy.h"

// CUDA-ATTRIBUTION-BASELINE-1 round 2 (astra major 1): on 2bc8cc0a,
// achieved_scale was simply the requested scale on every route --
// RenderFrameThread.cpp's playbackScaleFactorActive defaulted to
// playbackScaleFactor before the route check, so OutputDebayered16 (which
// never resizes for scale) reported the requested scale as if it had been
// achieved. This test fails on that behavior and passes once the achieved
// value is route-truthful.
TEST(PlaybackAchievedScalePolicy, DebayeredRouteNeverClaimsScaleEvenWhenRequested)
{
    const int achieved = PlaybackAchievedScalePolicy::achievedScaleFactor(
        PlaybackOutputRoute::Debayered16,
        /*requestedScaleFactor=*/4,
        /*coreActiveScale=*/4,
        /*coreActiveScaleValid=*/true );
    ASSERT_EQ(1, achieved);
}

TEST(PlaybackAchievedScalePolicy, DebayeredRouteIgnoresCoreClampToo)
{
    const int achieved = PlaybackAchievedScalePolicy::achievedScaleFactor(
        PlaybackOutputRoute::Debayered16,
        /*requestedScaleFactor=*/2,
        /*coreActiveScale=*/1,
        /*coreActiveScaleValid=*/true );
    ASSERT_EQ(1, achieved);
}

TEST(PlaybackAchievedScalePolicy, Processed8RoutePrefersValidCoreClamp)
{
    const int achieved = PlaybackAchievedScalePolicy::achievedScaleFactor(
        PlaybackOutputRoute::Processed8,
        /*requestedScaleFactor=*/4,
        /*coreActiveScale=*/2,
        /*coreActiveScaleValid=*/true );
    ASSERT_EQ(2, achieved);
}

TEST(PlaybackAchievedScalePolicy, Processed16RouteFallsBackToRequestedWhenCoreClampInvalid)
{
    const int achieved = PlaybackAchievedScalePolicy::achievedScaleFactor(
        PlaybackOutputRoute::Processed16,
        /*requestedScaleFactor=*/4,
        /*coreActiveScale=*/0,
        /*coreActiveScaleValid=*/false );
    ASSERT_EQ(4, achieved);
}

TEST(PlaybackAchievedScalePolicy, Processed8RouteAtScaleOneStaysOne)
{
    const int achieved = PlaybackAchievedScalePolicy::achievedScaleFactor(
        PlaybackOutputRoute::Processed8,
        /*requestedScaleFactor=*/1,
        /*coreActiveScale=*/1,
        /*coreActiveScaleValid=*/true );
    ASSERT_EQ(1, achieved);
}
