// PLAYBACK-HFR-CONFORM-DEFAULT-1 item 8: the timeline-speed and largest-jump measures the live
// acceptance reads (source frames advanced per wall second; max jump between presented source
// frames with the loop wrap excluded).
#include "../common/minitest.h"

#include "../../platform/qt/PlaybackSourceAdvanceTracker.h"

TEST(PlaybackSourceAdvanceTracker, EveryFrameShownAt24FpsIsUnitJumpsAt24SourceFps)
{
    PlaybackSourceAdvanceTracker t;
    for (int i = 0; i <= 720; ++i) { // 30 s at 24 new frames/s
        t.notePresented(i / 24.0, i, false, 0, 9999);
    }
    ASSERT_EQ(721u, t.presented());
    ASSERT_EQ(720, t.advancedFrames());
    ASSERT_EQ(1, t.maxJump());
    ASSERT_NEAR(24.0, t.sourceFps(), 1e-9);
    ASSERT_EQ(0u, t.wraps());
    ASSERT_EQ(0u, t.backwardJumps());
    ASSERT_EQ(0u, t.repeatedFrames());
}

// A timeline running at 60 fps that only presents every ~2.5th frame shows 24 new frames/s but
// is exactly the defect the presents counter cannot see.
TEST(PlaybackSourceAdvanceTracker, SkippingTimelineHasBigJumpsAndFastSourceFps)
{
    PlaybackSourceAdvanceTracker t;
    int frame = 0;
    for (int i = 0; i <= 240; ++i) { // 10 s, 24 presents/s
        t.notePresented(i / 24.0, frame, false, 0, 9999);
        frame += (i % 2 == 0) ? 2 : 3; // 2.5 source frames per present == 60 source fps
    }
    ASSERT_TRUE(t.maxJump() >= 3);
    ASSERT_NEAR(60.0, t.sourceFps(), 0.5);
}

TEST(PlaybackSourceAdvanceTracker, LoopWrapIsExcludedFromMaxJumpButCountsAsAdvance)
{
    PlaybackSourceAdvanceTracker t;
    // Range [0, 9]; 8, 9, then wrap to 0, 1.
    t.notePresented(0.0, 8, false, 0, 9);
    t.notePresented(1.0, 9, false, 0, 9);
    t.notePresented(2.0, 0, true, 0, 9);  // 9 -> 0 is one step
    t.notePresented(3.0, 1, false, 0, 9);
    ASSERT_EQ(1u, t.wraps());
    ASSERT_EQ(1, t.maxJump());
    ASSERT_EQ(3, t.advancedFrames());
    ASSERT_EQ(0u, t.backwardJumps());
    ASSERT_NEAR(1.0, t.sourceFps(), 1e-9);
}

TEST(PlaybackSourceAdvanceTracker, WrapWithSkippedFramesCreditsTheRealDistance)
{
    PlaybackSourceAdvanceTracker t;
    t.notePresented(0.0, 7, false, 0, 9);
    t.notePresented(1.0, 1, true, 0, 9); // 7 -> 8 -> 9 -> 0 -> 1 == 4 steps
    ASSERT_EQ(4, t.advancedFrames());
    ASSERT_EQ(0, t.maxJump()); // wrap only: no ordinary forward jump recorded
}

TEST(PlaybackSourceAdvanceTracker, BackwardAndRepeatedFramesAreCountedNotCredited)
{
    PlaybackSourceAdvanceTracker t;
    t.notePresented(0.0, 10, false, 0, 99);
    t.notePresented(0.1, 10, false, 0, 99); // repeat
    t.notePresented(0.2, 4, false, 0, 99);  // scrub back, no loop wrap
    t.notePresented(0.3, 5, false, 0, 99);
    ASSERT_EQ(1u, t.repeatedFrames());
    ASSERT_EQ(1u, t.backwardJumps());
    ASSERT_EQ(1, t.advancedFrames());
    ASSERT_EQ(1, t.maxJump());
}

TEST(PlaybackSourceAdvanceTracker, FewerThanTwoPresentsReportsNoRate)
{
    PlaybackSourceAdvanceTracker t;
    ASSERT_EQ(0.0, t.sourceFps());
    t.notePresented(5.0, 3, false, 0, 9);
    ASSERT_EQ(0.0, t.spanSeconds());
    ASSERT_EQ(0.0, t.sourceFps());
    t.reset();
    ASSERT_EQ(0u, t.presented());
    ASSERT_EQ(0, t.maxJump());
}
