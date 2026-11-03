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
    // r17 (adversary A1 on 6451a8d9 / eb5af034): MainWindow passes the live count and the count stamped when the
    // frame was REQUESTED (PresentationContext::engineWrapsAtRequest). stampAtRequest < 0 = requested after
    // every wrap so far.
    void present(double t, int frame, bool loop, int cutIn0, int cutOut0, int stampAtRequest = -1)
    {
        (void)loop;   // the inference (Loop, the jump size) no longer decides the tracker's wrap (re-pass A2)
        const int stamp = stampAtRequest < 0 ? engineWraps : stampAtRequest;
        const bool wrap = presentationIsLoopWrap(engineWraps, stamp, consumed, presented > 0, last, frame);
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
    // r16 re-pass A1 on c9b4ca5e: the measured Play starts at 718, its first tick wraps (718 + 1 -> 0) BEFORE the
    // first presentation, then 0, 1 and a user seek back to 0. The wrap belongs to the first presentation (which has
    // no transition), never to the seek: advanced 1, no wrap, one backward jump (c9b4ca5e: 720, 1, 0).
    const playback_frame_range::DropFrameTickResult first =
        playback_frame_range::advanceDropFrameTick(718.0, 1.0, 1, 720, true);
    ASSERT_TRUE(first.wrapped);
    ASSERT_EQ(0, static_cast<int>(first.position));
    PresentDriver p;
    p.startMeasuredPlay();
    ++p.engineWraps;
    p.present(0.0, static_cast<int>(first.position), true, 0, 719);
    p.present(1 / 24.0, 1, true, 0, 719);
    p.present(2 / 24.0, 0, true, 0, 719);
    ASSERT_EQ(1, static_cast<int>(p.tracker.advancedFrames()));
    ASSERT_EQ(0u, p.tracker.wraps());
    ASSERT_EQ(1u, p.tracker.backwardJumps());
    // and a wrap followed by a FORWARD presentation is consumed there: a later backward seek is not the wrap
    PresentDriver q;
    q.startMeasuredPlay();
    q.present(0.0, 100, true, 0, 719);
    ++q.engineWraps;
    q.present(1 / 24.0, 101, true, 0, 719);
    q.present(2 / 24.0, 50, true, 0, 719);
    ASSERT_EQ(0u, q.tracker.wraps());
    ASSERT_EQ(1u, q.tracker.backwardJumps());
    // r16 re-pass A2 on 215bd10e: Loop + drop-frame, cut 1..8 (0..7), frame 5 then a backward seek to 4 with no
    // engine wrap. The inference accepts it (a jump of 1 >= 7 - 8), but the tracker takes no wrap and no credit.
    ASSERT_TRUE(playback_frame_range::isContactSheetLoopWrapTransition(true, 0, 7, 5, 4));
    PresentDriver tiny;
    tiny.startMeasuredPlay();
    tiny.present(1.000, 5, true, 0, 7);
    tiny.present(1.010, 4, true, 0, 7);
    ASSERT_EQ(0u, tiny.tracker.wraps());
    ASSERT_EQ(1u, tiny.tracker.backwardJumps());
    ASSERT_EQ(0, static_cast<int>(tiny.tracker.advancedFrames()));
    // no Loop, no engine wrap: a backward seek is never a wrap (the inference's own refusal is unchanged)
    PresentDriver s;
    s.startMeasuredPlay();
    s.present(0.0, 400, false, 0, 719);
    s.present(0.1, 300, false, 0, 719);
    ASSERT_EQ(0u, s.tracker.wraps());
    ASSERT_EQ(1u, s.tracker.backwardJumps());
}

// r17 (adversary A1 on 6451a8d9): Loop on, drop-frame off, cut 1..720 (0..719), presentations 718, 719, 0 at 1.000,
// 1.045 and 1.090 s. Frame 719 was requested before the wrap, but the draw that presents it runs its early/tail
// advance first (MainWindow::finishPresentedFrame / drawFrameReady -> timerFrameEvent -> playbackHandling), and that
// advance wraps the engine to 0 before notePlaybackSmokePresentedFrame reads the count. With the live count, 719 took
// the wrap and 719 -> 0 was a backward jump: advanced 1, wraps 0, backward 1, 11.111 fps. With the request's stamp,
// the wrap belongs to frame 0, the first frame requested after it: advanced 2, wraps 1, backward 0, 22.222 fps.
TEST(PlaybackSourceAdvanceTracker, R17AdversaryA1AFrameRequestedBeforeTheWrapNeverTakesIt)
{
    PresentDriver d;
    d.engineWraps = 2;                       // earlier Plays' wraps
    d.startMeasuredPlay();
    d.present(1.000, 718, true, 0, 719);
    const int stamp719 = d.engineWraps;      // 719 is requested before the wrap ...
    ++d.engineWraps;                         // ... and the draw that presents it wraps the engine first
    d.present(1.045, 719, true, 0, 719, stamp719);
    d.present(1.090, 0, true, 0, 719);       // requested after the wrap
    ASSERT_EQ(2, static_cast<int>(d.tracker.advancedFrames()));
    ASSERT_EQ(1u, d.tracker.wraps());
    ASSERT_EQ(0u, d.tracker.backwardJumps());
    ASSERT_NEAR(2.0 / 0.090, d.tracker.sourceFps(), 1e-9);
    // two in-flight frames (a lookahead request) ahead of the wrap: still only the post-wrap frame takes it
    PresentDriver e;
    e.startMeasuredPlay();
    e.present(1.000, 717, true, 0, 719);
    const int before = e.engineWraps;
    ++e.engineWraps;
    e.present(1.040, 718, true, 0, 719, before);
    e.present(1.080, 719, true, 0, 719, before);
    e.present(1.120, 0, true, 0, 719);
    e.present(1.160, 1, true, 0, 719);
    ASSERT_EQ(4, static_cast<int>(e.tracker.advancedFrames()));
    ASSERT_EQ(1u, e.tracker.wraps());
    ASSERT_EQ(0u, e.tracker.backwardJumps());
    // the stamped wrap is still taken once: a later backward seek (requested after it) is a backward jump
    e.present(1.200, 0, true, 0, 719);
    ASSERT_EQ(1u, e.tracker.wraps());
    ASSERT_EQ(1u, e.tracker.backwardJumps());
    // the consumed count only rises: a frame requested before the measured Play (a stamp below the count at its
    // start) never re-opens the earlier Plays' wraps for the next presentation, so the seek after it is not a wrap
    // (6451a8d9's helper lowered the count to 2 and credited the seek 400 -> 300 as a wrap of 620 frames)
    PresentDriver late;
    late.engineWraps = 3;
    late.startMeasuredPlay();
    late.present(0.000, 400, true, 0, 719, 2);
    late.present(0.040, 300, true, 0, 719);
    ASSERT_EQ(0u, late.tracker.wraps());
    ASSERT_EQ(1u, late.tracker.backwardJumps());
    ASSERT_EQ(0, static_cast<int>(late.tracker.advancedFrames()));
}

// r17 (adversary A1 on eb5af034): lookahead 3, Loop on, drop-frame off, cut 1..720. 718 and the speculative 719, 0
// and 1 are all requested while the count is 0 (a lookahead copies its base request's stamp). The engine wraps, and
// the reused speculative 0 is presented with stamp 0. eb5af034 judged by the stamp alone (0 > 0 is false): 719 -> 0
// was backward, advanced 1, wraps 0, 11.111 fps. The live count says a wrap is pending, and 0 goes backward: it IS
// the wrap.
TEST(PlaybackSourceAdvanceTracker, R17AdversaryA1ALookaheadFrameRequestedAcrossTheWrapStillTakesIt)
{
    PresentDriver d;
    d.startMeasuredPlay();
    const int stamp = d.engineWraps;          // 718, 719, 0, 1 requested together
    d.present(1.000, 718, true, 0, 719, stamp);
    ++d.engineWraps;                          // the engine wraps to the cut-in
    d.present(1.045, 719, true, 0, 719, stamp);
    d.present(1.090, 0, true, 0, 719, stamp); // the reused speculative frame
    ASSERT_EQ(2, static_cast<int>(d.tracker.advancedFrames()));
    ASSERT_EQ(1u, d.tracker.wraps());
    ASSERT_EQ(0u, d.tracker.backwardJumps());
    ASSERT_NEAR(2.0 / 0.090, d.tracker.sourceFps(), 1e-9);
    d.present(1.135, 1, true, 0, 719, stamp);
    d.present(1.180, 2, true, 0, 719);        // the first frame requested after the wrap
    ASSERT_EQ(4, static_cast<int>(d.tracker.advancedFrames()));
    ASSERT_EQ(1u, d.tracker.wraps());
    // the wrap was taken once: a later user seek back is a backward jump, not a second wrap
    d.present(1.225, 0, true, 0, 719);
    ASSERT_EQ(1u, d.tracker.wraps());
    ASSERT_EQ(1u, d.tracker.backwardJumps());
    // the wrap's FORWARD landing (a frame requested after it, still ahead of the last presented frame) consumes
    // it, so a later seek is not credited as the wrap (r16 c9b4ca5e, with an explicit stamp)
    PresentDriver f;
    f.startMeasuredPlay();
    f.present(0.0, 3, true, 0, 7);
    ++f.engineWraps;                          // narrow cut 1..8, drop-frame: 3 -> 7 -> wraps -> 5
    f.present(0.1, 5, true, 0, 7, f.engineWraps);
    f.present(0.2, 2, true, 0, 7, f.engineWraps);
    ASSERT_EQ(0u, f.tracker.wraps());
    ASSERT_EQ(1u, f.tracker.backwardJumps());
}

// r18 (adversary A1 on 26f235a4): Loop on, drop-frame off, cut 1..720 (0..719). The measured Play's FIRST presented
// frame, 719, was requested with stamp 0, and the draw that presents it runs its early advance first, which wraps the
// engine (live 1) before notePlaybackSmokePresentedFrame reads the count. Then 0 and 1 at 1.000, 1.045, 1.090 s.
// e9b8b047 let the first presentation consume every pending wrap whatever its stamp, so 719 took the wrap and
// 719 -> 0 was a backward jump: advanced 1, wraps 0, backward 1, 11.111 fps. The first presentation consumes only
// the wraps before its own request; this wrap stays pending for 0: advanced 2, wraps 1, backward 0, 22.222 fps.
TEST(PlaybackSourceAdvanceTracker, R18AdversaryA1TheFirstFrameRequestedBeforeTheWrapLeavesItPending)
{
    PresentDriver d;
    d.startMeasuredPlay();
    const int stamp719 = d.engineWraps;      // 719 is requested before the wrap ...
    ++d.engineWraps;                         // ... and the draw that presents it wraps the engine first
    d.present(1.000, 719, true, 0, 719, stamp719);
    d.present(1.045, 0, true, 0, 719);       // requested after the wrap
    d.present(1.090, 1, true, 0, 719);
    ASSERT_EQ(2, static_cast<int>(d.tracker.advancedFrames()));
    ASSERT_EQ(1u, d.tracker.wraps());
    ASSERT_EQ(0u, d.tracker.backwardJumps());
    ASSERT_NEAR(2.0 / 0.090, d.tracker.sourceFps(), 1e-9);
    // the wrap was taken once: a later user seek back is a backward jump, not a second wrap
    d.present(1.135, 0, true, 0, 719);
    ASSERT_EQ(1u, d.tracker.wraps());
    ASSERT_EQ(1u, d.tracker.backwardJumps());
    // earlier Plays' wraps stay out: a first frame stamped below the count at the Play's start lowers nothing
    PresentDriver e;
    e.engineWraps = 4;
    e.startMeasuredPlay();
    const int stale = 2;                     // requested before the measured Play started
    ++e.engineWraps;                         // and this Play's own wrap lands before it is presented
    e.present(1.000, 719, true, 0, 719, stale);
    e.present(1.045, 0, true, 0, 719);
    ASSERT_EQ(1u, e.tracker.wraps());
    ASSERT_EQ(0u, e.tracker.backwardJumps());
    ASSERT_EQ(1, static_cast<int>(e.tracker.advancedFrames()));
    // a wrap BEFORE the first frame's request (r16 c9b4ca5e) is still consumed by that first presentation
    PresentDriver p;
    p.startMeasuredPlay();
    ++p.engineWraps;
    p.present(0.0, 0, true, 0, 719, p.engineWraps);
    p.present(1 / 24.0, 1, true, 0, 719);
    p.present(2 / 24.0, 0, true, 0, 719);
    ASSERT_EQ(0u, p.tracker.wraps());
    ASSERT_EQ(1u, p.tracker.backwardJumps());
    // the same wrap pending across a forward in-flight sibling of the first frame: still taken by the backward one
    PresentDriver g;
    g.startMeasuredPlay();
    const int both = g.engineWraps;          // 718 and 719 requested together (lookahead), then the wrap
    ++g.engineWraps;
    g.present(1.000, 718, true, 0, 719, both);
    g.present(1.045, 719, true, 0, 719, both);
    g.present(1.090, 0, true, 0, 719);
    ASSERT_EQ(2, static_cast<int>(g.tracker.advancedFrames()));
    ASSERT_EQ(1u, g.tracker.wraps());
    ASSERT_EQ(0u, g.tracker.backwardJumps());
}
