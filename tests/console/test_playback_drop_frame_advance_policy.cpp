#include "../common/minitest.h"

#include "../../platform/qt/PlaybackDropFrameAdvancePolicy.h"

// CUDA-ATTRIBUTION-BASELINE-1 round 7 (astra major, "Non-looping EOF
// overshoot inflates the denominator"): astra's repro verbatim -- position
// 98, last valid frame 99, looping disabled, 30 FPS, a 100 ms tick (raw
// advance 3.0). Only ONE source frame (98 -> 99) actually exists to advance
// across; the other 2.0 of raw advance is past EOF and must not be offered.
TEST(PlaybackDropFrameAdvancePolicy, AstraRepro_NonLoopingOvershootIsCappedAtRemainingDistance)
{
    const double credited = PlaybackDropFrameAdvancePolicy::offeredAdvance(
        /*currentPosition=*/98.0,
        /*rawAdvance=*/3.0,
        /*loopEnabled=*/false,
        /*lastFrameIndex=*/99.0 );

    ASSERT_TRUE(credited == 1.0);
}

// When the raw advance does not reach the clip end, it passes through
// unchanged -- the ordinary, non-overshoot case.
TEST(PlaybackDropFrameAdvancePolicy, AdvanceWithinRangeIsUnchanged)
{
    const double credited = PlaybackDropFrameAdvancePolicy::offeredAdvance(
        /*currentPosition=*/10.0,
        /*rawAdvance=*/2.5,
        /*loopEnabled=*/false,
        /*lastFrameIndex=*/99.0 );

    ASSERT_TRUE(credited == 2.5);
}

// Looping is unaffected by the cap -- a lap wrap is a real, unbounded
// source-frame distance (playbackHandling()'s own wrap-crediting rationale),
// not an EOF overshoot, even when the raw advance carries past the loop's
// end position.
TEST(PlaybackDropFrameAdvancePolicy, LoopingIsNeverCapped)
{
    const double credited = PlaybackDropFrameAdvancePolicy::offeredAdvance(
        /*currentPosition=*/98.0,
        /*rawAdvance=*/3.0,
        /*loopEnabled=*/true,
        /*lastFrameIndex=*/99.0 );

    ASSERT_TRUE(credited == 3.0);
}

// Landing exactly on the last frame index (no overshoot past it) still
// credits the full raw advance -- only advances that reach or cross the end
// trigger the clamp branch at all, and when they land exactly on it, the
// travelled distance equals the raw advance.
TEST(PlaybackDropFrameAdvancePolicy, LandingExactlyOnLastFrameIsNotOvershoot)
{
    const double credited = PlaybackDropFrameAdvancePolicy::offeredAdvance(
        /*currentPosition=*/97.0,
        /*rawAdvance=*/2.0,
        /*loopEnabled=*/false,
        /*lastFrameIndex=*/99.0 );

    ASSERT_TRUE(credited == 2.0);
}

// A position already at or past the end (degenerate/defensive case) must
// never credit a negative amount.
TEST(PlaybackDropFrameAdvancePolicy, AlreadyPastEndNeverCreditsNegative)
{
    const double credited = PlaybackDropFrameAdvancePolicy::offeredAdvance(
        /*currentPosition=*/100.0,
        /*rawAdvance=*/3.0,
        /*loopEnabled=*/false,
        /*lastFrameIndex=*/99.0 );

    ASSERT_TRUE(credited == 0.0);
}
