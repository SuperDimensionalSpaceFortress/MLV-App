#include "../common/minitest.h"

#include "../../platform/qt/PlaybackLookaheadLoopPositionPolicy.h"

// CUDA-ATTRIBUTION-BASELINE-1 round 6 (sol + astra BLOCKER): a lookahead
// offset that crosses cut-out by exactly one clip length lands one lap
// ahead, at the same in-clip position modulo would already have produced.
TEST(PlaybackLookaheadLoopPositionPolicy, SingleLapCrossingAdvancesOneEpoch)
{
    // cutIn=0, cutOut=23 (loopSpan=24); requestedFrame=22, offset=3 -> raw 25.
    const PlaybackLookaheadLoopPosition wrapped =
        PlaybackLookaheadLoopPositionPolicy::wrap(
            /*rawLookaheadFrame=*/25, /*cutInFrame=*/0, /*loopSpan=*/24 );

    ASSERT_EQ(1, wrapped.wrappedFrame);
    ASSERT_EQ(uint64_t(1), wrapped.lapsAhead);
}

// Landing exactly on the boundary (one past the last valid frame) is still
// exactly one lap ahead, at position 0.
TEST(PlaybackLookaheadLoopPositionPolicy, LandingExactlyOnLoopSpanIsOneEpochAtPositionZero)
{
    const PlaybackLookaheadLoopPosition wrapped =
        PlaybackLookaheadLoopPositionPolicy::wrap(
            /*rawLookaheadFrame=*/24, /*cutInFrame=*/0, /*loopSpan=*/24 );

    ASSERT_EQ(0, wrapped.wrappedFrame);
    ASSERT_EQ(uint64_t(1), wrapped.lapsAhead);
}

// A lookahead depth deep enough to cross the loop boundary twice (depth >
// loopSpan) must advance the epoch by two, not saturate at one -- this is
// the case a single "did it wrap" boolean cannot represent.
TEST(PlaybackLookaheadLoopPositionPolicy, DoubleLapCrossingAdvancesTwoEpochs)
{
    const PlaybackLookaheadLoopPosition wrapped =
        PlaybackLookaheadLoopPositionPolicy::wrap(
            /*rawLookaheadFrame=*/50, /*cutInFrame=*/0, /*loopSpan=*/24 );

    ASSERT_EQ(2, wrapped.wrappedFrame);
    ASSERT_EQ(uint64_t(2), wrapped.lapsAhead);
}

// A non-zero cut-in must be honoured by the same formula, not just the
// cut-in=0 special case.
TEST(PlaybackLookaheadLoopPositionPolicy, NonZeroCutInIsHonoured)
{
    // cutIn=100, cutOut=119 (loopSpan=20); raw=125 -> one lap ahead, at 105.
    const PlaybackLookaheadLoopPosition wrapped =
        PlaybackLookaheadLoopPositionPolicy::wrap(
            /*rawLookaheadFrame=*/125, /*cutInFrame=*/100, /*loopSpan=*/20 );

    ASSERT_EQ(105, wrapped.wrappedFrame);
    ASSERT_EQ(uint64_t(1), wrapped.lapsAhead);
}

// A degenerate (non-positive) loopSpan must not be treated as a wrap at
// all -- MainWindow itself never calls wrap() in this situation (it checks
// loopSpan <= 0 before calling), but the policy must still fail safe rather
// than divide by zero if ever misused.
TEST(PlaybackLookaheadLoopPositionPolicy, DegenerateLoopSpanLeavesPositionUnwrapped)
{
    const PlaybackLookaheadLoopPosition wrapped =
        PlaybackLookaheadLoopPositionPolicy::wrap(
            /*rawLookaheadFrame=*/30, /*cutInFrame=*/0, /*loopSpan=*/0 );

    ASSERT_EQ(30, wrapped.wrappedFrame);
    ASSERT_EQ(uint64_t(0), wrapped.lapsAhead);
}
