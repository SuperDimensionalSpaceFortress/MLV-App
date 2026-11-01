// PLAYBACK-HFR-CONFORM-DEFAULT-1 item 8: the timeline-speed and largest-jump measures the live
// acceptance reads (source frames advanced per wall second; max jump between presented source
// frames with the loop wrap excluded).
#include "../common/minitest.h"

#include "../../platform/qt/PlaybackSourceAdvanceTracker.h"
#include "../../platform/qt/PlaybackFrameRange.h"

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

// HFR-NATIVE-60FPS-FULLRES-1-MERGE-MASTER r16 (adversary A1 on ff41b4fa): Loop + drop-frame, cut 1..720
// (0-based 0..719), previous presented frame 700. advanceDropFrameTick(700, 19, 1, 720, true) returns frame 0
// with wrapped = true, and frame 0 is presented next. The inference (isContactSheetLoopWrapTransition) needs a
// jump back of at least 711, so it misses this; MainWindow fed only the inference to the tracker, which then
// counted a backward jump, no wrap and no source advance across it. The engine's wrap count now decides
// (presentationIsLoopWrap), driven exactly as MainWindow::notePlaybackSmokePresentedFrame drives it.
namespace
{
struct PresentDriver
{
    PlaybackSourceAdvanceTracker tracker;
    int engineWraps = 0;
    int consumed = 0;
    int presented = 0;
    int last = -1;
    void startMeasuredPlay() { tracker.reset(); consumed = engineWraps; presented = 0; last = -1; }
    void present(double t, int frame, bool loop, int cutIn0, int cutOut0)
    {
        const bool inferred = presented > 0
            && playback_frame_range::isContactSheetLoopWrapTransition(loop, cutIn0, cutOut0, last, frame);
        const bool wrap = presentationIsLoopWrap(inferred, engineWraps, consumed, presented > 0, last, frame);
        tracker.notePresented(t, frame, wrap, cutIn0, cutOut0);
        last = frame;
        ++presented;
    }
};
}

TEST(PlaybackSourceAdvanceTracker, R16AdversaryA1AnEngineWrapTheInferenceMissesIsStillTheWrap)
{
    const playback_frame_range::DropFrameTickResult tick =
        playback_frame_range::advanceDropFrameTick(700.0, 19.0, 1, 720, true);
    ASSERT_TRUE(tick.wrapped);
    ASSERT_EQ(0, static_cast<int>(tick.position));
    ASSERT_FALSE(playback_frame_range::isContactSheetLoopWrapTransition(true, 0, 719, 700, 0));
    PresentDriver d;
    d.engineWraps = 3;                  // wraps of an earlier Play in the process are not this Play's
    d.startMeasuredPlay();
    for (int f = 690; f <= 700; ++f) d.present((f - 690) / 24.0, f, true, 0, 719);
    ++d.engineWraps;                    // the drop-frame tick wraps (MainWindow notes it in playbackHandling)
    d.present(11 / 24.0, static_cast<int>(tick.position), true, 0, 719);
    d.present(12 / 24.0, 1, true, 0, 719);
    d.present(13 / 24.0, 2, true, 0, 719);
    ASSERT_EQ(1u, d.tracker.wraps());
    ASSERT_EQ(0u, d.tracker.backwardJumps());
    ASSERT_EQ(10 + (0 + 720 - 700) + 2, static_cast<int>(d.tracker.advancedFrames()));
    // a later backward seek with no new engine wrap is still a backward jump, never a second wrap
    d.present(14 / 24.0, 1, true, 0, 719);
    ASSERT_EQ(1u, d.tracker.wraps());
    ASSERT_EQ(1u, d.tracker.backwardJumps());
    // a wrap the inference does see takes the engine's wrap once: the next backward seek is not a wrap
    PresentDriver e;
    e.startMeasuredPlay();
    for (int f = 710; f <= 718; ++f) e.present((f - 710) / 24.0, f, true, 0, 719);
    ++e.engineWraps;
    e.present(9 / 24.0, 0, true, 0, 719);
    e.present(10 / 24.0, 5, true, 0, 719);
    e.present(11 / 24.0, 2, true, 0, 719);
    ASSERT_EQ(1u, e.tracker.wraps());
    ASSERT_EQ(1u, e.tracker.backwardJumps());
    // no Loop, no engine wrap: a backward seek is never a wrap (the inference's own refusal is unchanged)
    PresentDriver s;
    s.startMeasuredPlay();
    s.present(0.0, 400, false, 0, 719);
    s.present(0.1, 300, false, 0, 719);
    ASSERT_EQ(0u, s.tracker.wraps());
    ASSERT_EQ(1u, s.tracker.backwardJumps());
}
