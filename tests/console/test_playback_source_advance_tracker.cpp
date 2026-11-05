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
// (presentationLoopWraps), driven exactly as MainWindow::notePlaybackSmokePresentedFrame drives it.
namespace
{
struct PresentDriver
{
    PlaybackSourceAdvanceTracker tracker;
    int engineWraps = 0;
    int consumed = 0;
    int presented = 0;
    void startMeasuredPlay() { tracker.reset(); consumed = engineWraps; presented = 0; }
    // r17 (adversary A1 on 6451a8d9 / eb5af034): MainWindow passes the live count and the count stamped when the
    // frame was REQUESTED (PresentationContext::engineWrapsAtRequest; r20: a presented lookahead's accepted stamp).
    // stampAtRequest < 0 = requested after every wrap so far.
    void present(double t, int frame, bool loop, int cutIn0, int cutOut0, int stampAtRequest = -1)
    {
        (void)loop;   // the inference (Loop, the jump size) no longer decides the tracker's wrap (re-pass A2)
        const int stamp = stampAtRequest < 0 ? engineWraps : stampAtRequest;
        const int wraps = presentationLoopWraps(engineWraps, stamp, consumed, presented > 0);
        tracker.notePresented(t, frame, wraps, cutIn0, cutOut0);
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
    // and a wrap followed by a FORWARD presentation requested after it is taken there (r19, sol r18 B1: that frame is
    // the wrap's forward landing, a full loop plus one, 101 + 720 - 100 = 721): a later backward seek is not the wrap
    PresentDriver q;
    q.startMeasuredPlay();
    q.present(0.0, 100, true, 0, 719);
    ++q.engineWraps;
    q.present(1 / 24.0, 101, true, 0, 719);
    q.present(2 / 24.0, 50, true, 0, 719);
    ASSERT_EQ(1u, q.tracker.wraps());
    ASSERT_EQ(1u, q.tracker.backwardJumps());
    ASSERT_EQ(721, static_cast<int>(q.tracker.advancedFrames()));
    ASSERT_EQ(0, q.tracker.maxJump());
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
// was backward, advanced 1, wraps 0, 11.111 fps. r20: drawFrameReady accepts the reused 0 only because it is the engine's
// active target, and the live count at that check stamps it (presentationAcceptedLookaheadStamp): it IS the wrap's landing.
TEST(PlaybackSourceAdvanceTracker, R17AdversaryA1ALookaheadFrameRequestedAcrossTheWrapStillTakesIt)
{
    PresentDriver d;
    d.startMeasuredPlay();
    const int stamp = d.engineWraps;          // 718, 719, 0, 1 requested together
    d.present(1.000, 718, true, 0, 719, stamp);
    ++d.engineWraps;                          // the engine wraps to the cut-in
    d.present(1.045, 719, true, 0, 719, stamp);
    d.present(1.090, 0, true, 0, 719, presentationAcceptedLookaheadStamp(stamp, d.engineWraps)); // the reused speculative frame
    ASSERT_EQ(2, static_cast<int>(d.tracker.advancedFrames()));
    ASSERT_EQ(1u, d.tracker.wraps());
    ASSERT_EQ(0u, d.tracker.backwardJumps());
    ASSERT_NEAR(2.0 / 0.090, d.tracker.sourceFps(), 1e-9);
    d.present(1.135, 1, true, 0, 719, presentationAcceptedLookaheadStamp(stamp, d.engineWraps));
    d.present(1.180, 2, true, 0, 719);        // the first frame requested after the wrap
    ASSERT_EQ(4, static_cast<int>(d.tracker.advancedFrames()));
    ASSERT_EQ(1u, d.tracker.wraps());
    // the wrap was taken once: a later user seek back is a backward jump, not a second wrap
    d.present(1.225, 0, true, 0, 719);
    ASSERT_EQ(1u, d.tracker.wraps());
    ASSERT_EQ(1u, d.tracker.backwardJumps());
    // the wrap's FORWARD landing (a frame requested after it, still ahead of the last presented frame) takes it
    // (r19, sol r18 B1: credited 5 + 8 - 3 = 10, no forward jump), so a later seek is not credited as the wrap
    PresentDriver f;
    f.startMeasuredPlay();
    f.present(0.0, 3, true, 0, 7);
    ++f.engineWraps;                          // narrow cut 1..8, drop-frame: 3 -> 7 -> wraps -> 5
    f.present(0.1, 5, true, 0, 7, f.engineWraps);
    f.present(0.2, 2, true, 0, 7, f.engineWraps);
    ASSERT_EQ(1u, f.tracker.wraps());
    ASSERT_EQ(1u, f.tracker.backwardJumps());
    ASSERT_EQ(10, static_cast<int>(f.tracker.advancedFrames()));
    ASSERT_EQ(0, f.tracker.maxJump());
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

// r19 (sol r18 B1 / fable r18 OPEN 5196d4a6-A1, catalogue row M-HFR-FORWARD-WRAP): Loop + drop-frame, cut 1..8 (0..7).
// 3 is presented at 1.000 s; the next tick advanceDropFrameTick(3, 9, 1, 8, true) wraps the engine and lands on 5,
// requested after the wrap and presented at 1.160 s. 5196d4a6 consumed the wrap because 5 > 3 and returned no wrap:
// advanced 2, wraps 0, maxJump 2, 12.5 source fps. A live engine wrap is the wrap whatever the landing's direction,
// so the tracker's own wrapped-distance rule credits 5 + 8 - 3 = 10: advanced 10, wraps 1, maxJump 0, 62.5 fps.
TEST(PlaybackSourceAdvanceTracker, R19SolB1AForwardWrapLandingCreditsTheLoopDistance)
{
    const playback_frame_range::DropFrameTickResult tick =
        playback_frame_range::advanceDropFrameTick(3.0, 9.0, 1, 8, true);
    ASSERT_TRUE(tick.wrapped);
    ASSERT_EQ(5, static_cast<int>(tick.position));
    PresentDriver d;
    d.startMeasuredPlay();
    d.present(1.000, 3, true, 0, 7);
    ++d.engineWraps;                          // the drop-frame tick wraps (MainWindow notes it in playbackHandling)
    d.present(1.160, static_cast<int>(tick.position), true, 0, 7, d.engineWraps);
    ASSERT_EQ(1u, d.tracker.wraps());
    ASSERT_EQ(10, static_cast<int>(d.tracker.advancedFrames()));
    ASSERT_EQ(0, d.tracker.maxJump());
    ASSERT_EQ(0u, d.tracker.backwardJumps());
    ASSERT_EQ(0u, d.tracker.repeatedFrames());
    ASSERT_NEAR(10.0 / 0.160, d.tracker.sourceFps(), 1e-9);
    // the wrap was taken once: the next forward frame is an ordinary step and a later seek back is not a wrap
    d.present(1.200, 6, true, 0, 7);
    d.present(1.240, 2, true, 0, 7);
    ASSERT_EQ(1u, d.tracker.wraps());
    ASSERT_EQ(11, static_cast<int>(d.tracker.advancedFrames()));
    ASSERT_EQ(1, d.tracker.maxJump());
    ASSERT_EQ(1u, d.tracker.backwardJumps());
    // a landing on the frame it left (a full loop, requested after the wrap) is one cut width, never a repeat
    PresentDriver r;
    r.startMeasuredPlay();
    r.present(1.000, 3, true, 0, 7);
    ++r.engineWraps;
    r.present(1.160, 3, true, 0, 7, r.engineWraps);
    ASSERT_EQ(1u, r.tracker.wraps());
    ASSERT_EQ(8, static_cast<int>(r.tracker.advancedFrames()));
    ASSERT_EQ(0u, r.tracker.repeatedFrames());
    // an in-flight forward frame requested BEFORE the wrap is still not its landing: the wrap stays pending for the
    // first frame requested after it, here a forward one (6 -> wrap -> 7 over cut 0..7 is 7 + 8 - 6 = 9)
    PresentDriver g;
    g.startMeasuredPlay();
    g.present(1.000, 4, true, 0, 7);
    const int before = g.engineWraps;
    ++g.engineWraps;
    g.present(1.040, 6, true, 0, 7, before);
    g.present(1.080, 7, true, 0, 7);
    ASSERT_EQ(1u, g.tracker.wraps());
    ASSERT_EQ(2 + 9, static_cast<int>(g.tracker.advancedFrames()));
    ASSERT_EQ(2, g.tracker.maxJump());
    // two wraps, each landed forward by a frame requested between them: each frame takes exactly its own wrap
    PresentDriver two;
    two.startMeasuredPlay();
    two.present(1.000, 2, true, 0, 7);
    ++two.engineWraps;
    const int afterFirst = two.engineWraps;
    ++two.engineWraps;                        // a second wrap before the first one's landing is presented
    two.present(1.100, 4, true, 0, 7, afterFirst);
    ASSERT_EQ(1u, two.tracker.wraps());
    two.present(1.200, 6, true, 0, 7);
    ASSERT_EQ(2u, two.tracker.wraps());
    ASSERT_EQ((4 + 8 - 2) + (6 + 8 - 4), static_cast<int>(two.tracker.advancedFrames()));
    ASSERT_EQ(0, two.tracker.maxJump());
    // no engine wrap: a forward frame is an ordinary jump, never a wrap (the inference still decides nothing)
    PresentDriver n;
    n.startMeasuredPlay();
    n.present(1.000, 3, true, 0, 7);
    n.present(1.160, 5, true, 0, 7);
    ASSERT_EQ(0u, n.tracker.wraps());
    ASSERT_EQ(2, static_cast<int>(n.tracker.advancedFrames()));
    ASSERT_EQ(2, n.tracker.maxJump());
}

// r19 fix iteration 1 (adversary A1 on 924d3d6f): two engine wraps between presentations. Cut 0..7. 5 is presented first;
// the engine wraps, 1 is requested (stamp 1), the engine wraps again before 1 is presented (live 2), then 3 is requested
// after the second wrap (stamp 2). 924d3d6f's backward landing consumed BOTH wraps and credited one cut width, so 3 found
// no pending wrap: advanced 6, wraps 1. Each landing takes only the wraps before its own request, one cut width each:
// 5 -> 1 is 1 + 8 - 5 = 4, 1 -> 3 is 3 + 8 - 1 = 10: advanced 14, wraps 2.
TEST(PlaybackSourceAdvanceTracker, R19AdversaryA1EachLandingTakesOnlyTheWrapsItCrossed)
{
    PresentDriver d;
    d.startMeasuredPlay();
    d.present(1.000, 5, true, 0, 7);
    ++d.engineWraps;
    const int stamp1 = d.engineWraps;         // 1 is requested after the first wrap ...
    ++d.engineWraps;                          // ... and the second wrap lands before 1 is presented
    d.present(1.100, 1, true, 0, 7, stamp1);
    ASSERT_EQ(1u, d.tracker.wraps());
    ASSERT_EQ(4, static_cast<int>(d.tracker.advancedFrames()));
    d.present(1.200, 3, true, 0, 7);          // requested after the second wrap
    ASSERT_EQ(2u, d.tracker.wraps());
    ASSERT_EQ(14, static_cast<int>(d.tracker.advancedFrames()));
    ASSERT_EQ(0, d.tracker.maxJump());
    ASSERT_EQ(0u, d.tracker.backwardJumps());
    ASSERT_NEAR(14.0 / 0.200, d.tracker.sourceFps(), 1e-9);
    // the adversary's same-class case: a FORWARD landing over two wraps, 2 -> (two wraps) -> 4 = 4 + 2 x 8 - 2 = 18
    PresentDriver f;
    f.startMeasuredPlay();
    f.present(1.000, 2, true, 0, 7);
    f.engineWraps += 2;
    f.present(1.100, 4, true, 0, 7);
    ASSERT_EQ(2u, f.tracker.wraps());
    ASSERT_EQ(18, static_cast<int>(f.tracker.advancedFrames()));
    ASSERT_EQ(0, f.tracker.maxJump());
    // a BACKWARD landing over two wraps (fable r18's live=2 row): 6 -> (two wraps) -> 2 = 2 + 2 x 8 - 6 = 12
    PresentDriver b;
    b.startMeasuredPlay();
    b.present(1.000, 6, true, 0, 7);
    b.engineWraps += 2;
    b.present(1.100, 2, true, 0, 7);
    ASSERT_EQ(2u, b.tracker.wraps());
    ASSERT_EQ(12, static_cast<int>(b.tracker.advancedFrames()));
    ASSERT_EQ(0u, b.tracker.backwardJumps());
    // r20: a lookahead spawned before BOTH wraps, accepted as the target after the first, and whose own draw makes the second,
    // lands the first; the other stays pending for the first frame requested after it: 6 -> 2 is 4, 2 -> 3 is 3 + 8 - 2 = 9,
    // total 13 = 3 + 2 x 8 - 6
    PresentDriver l;
    l.startMeasuredPlay();
    const int both = l.engineWraps;
    l.present(1.000, 6, true, 0, 7, both);
    l.engineWraps += 1;
    const int acceptedOnce = presentationAcceptedLookaheadStamp(both, l.engineWraps);
    l.engineWraps += 1;
    l.present(1.050, 2, true, 0, 7, acceptedOnce);
    ASSERT_EQ(1u, l.tracker.wraps());
    l.present(1.100, 3, true, 0, 7);
    ASSERT_EQ(2u, l.tracker.wraps());
    ASSERT_EQ(13, static_cast<int>(l.tracker.advancedFrames()));
    // every wrap is taken once: a later seek back is a backward jump, never a third wrap
    l.present(1.150, 0, true, 0, 7);
    ASSERT_EQ(2u, l.tracker.wraps());
    ASSERT_EQ(1u, l.tracker.backwardJumps());
    // the tracker credits a wrap count directly: true / false still read as 1 / 0
    PlaybackSourceAdvanceTracker t;
    t.notePresented(0.0, 6, 0, 0, 7);
    t.notePresented(1.0, 2, 3, 0, 7);         // three loops of 8 from 6 to 2 = 2 + 24 - 6
    ASSERT_EQ(20, static_cast<int>(t.advancedFrames()));
    ASSERT_EQ(3u, t.wraps());
}

// r19 fix iteration 2 (adversary A1 on 1df419eb): Loop + drop-frame, cut 1..3 (0..2), lookahead depth 2. 0 is presented
// (stamp 0); the cached lookahead 1 was spawned with stamp 0; the engine wraps, advanceDropFrameTick(0, 3, 1, 3, true)
// lands on 1, and drawFrame's request for 1 (at count 1) reuses the cached 1. 1df419eb read the cached frame's own stamp
// 0, so the forward landing looked like an in-flight frame: advanced 1, wraps 0, 20 source fps. The engine's own request
// for the frame stamped a reused lookahead here; r20 replaced that record with the count at drawFrameReady's target check
// (presentationAcceptedLookaheadStamp, as MainWindow::drawFrameReady calls it): 1 + 3 - 0 = 4, wraps 1, 80 source fps.
TEST(PlaybackSourceAdvanceTracker, R19AdversaryA1ReusedLookaheadIsStampedByTheEnginesOwnRequest)
{
    const playback_frame_range::DropFrameTickResult tick =
        playback_frame_range::advanceDropFrameTick(0.0, 3.0, 1, 3, true);
    ASSERT_TRUE(tick.wrapped);
    ASSERT_EQ(1, static_cast<int>(tick.position));
    PresentDriver d;
    d.startMeasuredPlay();
    d.present(1.000, 0, true, 0, 2);
    const int lookaheadStamp = d.engineWraps;                 // the cached 1 was spawned before the wrap
    ++d.engineWraps;                                          // the engine wraps, and 1 is its active target
    const int stamp = presentationAcceptedLookaheadStamp(lookaheadStamp, d.engineWraps);
    ASSERT_EQ(1, stamp);
    d.present(1.050, 1, true, 0, 2, stamp);
    ASSERT_EQ(1u, d.tracker.wraps());
    ASSERT_EQ(4, static_cast<int>(d.tracker.advancedFrames()));
    ASSERT_EQ(0, d.tracker.maxJump());
    ASSERT_NEAR(4.0 / 0.050, d.tracker.sourceFps(), 1e-9);
    // the reused speculative 0 of eb5af034 (lookahead stamped 0, accepted as the target after the wrap) still lands the
    // wrap backward, once
    PresentDriver e;
    e.startMeasuredPlay();
    e.present(1.000, 718, true, 0, 719);
    e.present(1.045, 719, true, 0, 719);
    const int spec0 = e.engineWraps;
    ++e.engineWraps;
    e.present(1.090, 0, true, 0, 719, presentationAcceptedLookaheadStamp(spec0, e.engineWraps));
    ASSERT_EQ(1u, e.tracker.wraps());
    ASSERT_EQ(2, static_cast<int>(e.tracker.advancedFrames()));
    ASSERT_EQ(0u, e.tracker.backwardJumps());
}

// r20 (adversary A1 on dbeb9424 and on 5d820339): Loop + drop-frame, a reused lookahead frame. drawFrameReady accepts it because
// it IS the engine's active target, stamps it with the live count there, and only then runs that draw's early/tail advance. That
// advance's request for the next frame overwrote dbeb9424's record of the engine's last request (the pair became (2,1), then (6,1)),
// so the landing read as an in-flight frame: advanced 1, wraps 0, maxJump 1, and advanced 2, wraps 0, maxJump 2.
TEST(PlaybackSourceAdvanceTracker, R20AdversaryA1ALookaheadIsStampedWhenItIsAcceptedAsTheTarget)
{
    // cut 0..3, depth 2: 0 presented, cached 1 stamped 0, advanceDropFrameTick(0, 4, 1, 4, true) wraps and lands on 1
    const playback_frame_range::DropFrameTickResult tick =
        playback_frame_range::advanceDropFrameTick(0.0, 4.0, 1, 4, true);
    ASSERT_TRUE(tick.wrapped);
    ASSERT_EQ(1, static_cast<int>(tick.position));
    PresentDriver d;
    d.startMeasuredPlay();
    d.present(1.000, 0, true, 0, 3);
    const int context = d.engineWraps;
    ++d.engineWraps;
    const int accepted = presentationAcceptedLookaheadStamp(context, d.engineWraps);
    ASSERT_EQ(1, accepted);
    d.present(1.050, static_cast<int>(tick.position), true, 0, 3, accepted);
    ASSERT_EQ(5, static_cast<int>(d.tracker.advancedFrames()));
    ASSERT_EQ(1u, d.tracker.wraps());
    ASSERT_EQ(0, d.tracker.maxJump());
    // cut 0..7: 3 presented, cached 5 stamped 0, the engine wraps and requests 5, and 5's own advance then requests 6
    PresentDriver f;
    f.startMeasuredPlay();
    f.present(1.000, 3, true, 0, 7);
    const int contextFive = f.engineWraps;
    ++f.engineWraps;
    f.present(1.050, 5, true, 0, 7, presentationAcceptedLookaheadStamp(contextFive, f.engineWraps));
    ASSERT_EQ(10, static_cast<int>(f.tracker.advancedFrames()));
    ASSERT_EQ(1u, f.tracker.wraps());
    ASSERT_EQ(0, f.tracker.maxJump());
    // the count is read BEFORE that draw's own advance: a wrap the advance makes is not this frame's. 6, then cached 7 accepted at
    // count 0 whose early advance wraps (live 1), then cached 0 accepted at count 1 lands it: 7 is one step, 0 is one more
    PresentDriver g;
    g.startMeasuredPlay();
    g.present(1.000, 6, true, 0, 7);
    const int seven = presentationAcceptedLookaheadStamp(g.engineWraps, g.engineWraps);
    ++g.engineWraps;
    g.present(1.050, 7, true, 0, 7, seven);
    ASSERT_EQ(0u, g.tracker.wraps());
    g.present(1.100, 0, true, 0, 7, presentationAcceptedLookaheadStamp(0, g.engineWraps));
    ASSERT_EQ(1u, g.tracker.wraps());
    ASSERT_EQ(2, static_cast<int>(g.tracker.advancedFrames()));
    ASSERT_EQ(1, g.tracker.maxJump());
    ASSERT_EQ(0u, g.tracker.backwardJumps());
    // the rule itself: the later of the two counts, so a frame requested after the check's count keeps its own
    ASSERT_EQ(1, presentationAcceptedLookaheadStamp(0, 1));
    ASSERT_EQ(2, presentationAcceptedLookaheadStamp(2, 1));
    ASSERT_EQ(3, presentationAcceptedLookaheadStamp(3, 3));
}

// r20 (adversary A2 on dbeb9424 and on 5d820339): cut 0..7, 5 presented, an ordinary non-lookahead seek to 4 stamped 0, and its draw's
// early advance wraps the engine (live 1) before it is noted. dbeb9424's backward branch took the wrap: the seek was credited
// 4 + 8 - 5 = 7, wraps 1, backward 0, and the real landing then had no wrap left and counted backward.
TEST(PlaybackSourceAdvanceTracker, R20AdversaryA2ABackwardFrameStampedBeforeAWrapIsNotItsLanding)
{
    // advanceDropFrameTick(4, 5, 1, 8, true) lands on 2
    const playback_frame_range::DropFrameTickResult tick =
        playback_frame_range::advanceDropFrameTick(4.0, 5.0, 1, 8, true);
    ASSERT_TRUE(tick.wrapped);
    ASSERT_EQ(2, static_cast<int>(tick.position));
    PresentDriver d;
    d.startMeasuredPlay();
    d.present(1.000, 5, true, 0, 7);
    const int seekStamp = d.engineWraps;
    ++d.engineWraps;
    d.present(1.050, 4, true, 0, 7, seekStamp);
    ASSERT_EQ(0, static_cast<int>(d.tracker.advancedFrames()));
    ASSERT_EQ(0u, d.tracker.wraps());
    ASSERT_EQ(1u, d.tracker.backwardJumps());
    d.present(1.100, static_cast<int>(tick.position), true, 0, 7);
    ASSERT_EQ(6, static_cast<int>(d.tracker.advancedFrames()));
    ASSERT_EQ(1u, d.tracker.wraps());
    ASSERT_EQ(1u, d.tracker.backwardJumps());
    ASSERT_EQ(0, d.tracker.maxJump());
    // the same seek when the early advance lands on 0: 0 + 8 - 4 = 4, and the wrap is taken once
    PresentDriver z;
    z.startMeasuredPlay();
    z.present(1.000, 5, true, 0, 7);
    const int seekZero = z.engineWraps;
    ++z.engineWraps;
    z.present(1.050, 4, true, 0, 7, seekZero);
    z.present(1.100, 0, true, 0, 7);
    ASSERT_EQ(4, static_cast<int>(z.tracker.advancedFrames()));
    ASSERT_EQ(1u, z.tracker.wraps());
    ASSERT_EQ(1u, z.tracker.backwardJumps());
    z.present(1.150, 0, true, 0, 7);
    ASSERT_EQ(1u, z.tracker.wraps());
    ASSERT_EQ(1u, z.tracker.repeatedFrames());
    // two backward frames stamped before the wrap: neither is its landing, and the landing is measured from the last of them
    PresentDriver t;
    t.startMeasuredPlay();
    t.present(1.000, 5, true, 0, 7);
    const int twoStamp = t.engineWraps;
    ++t.engineWraps;
    t.present(1.050, 4, true, 0, 7, twoStamp);
    t.present(1.100, 3, true, 0, 7, twoStamp);
    ASSERT_EQ(2u, t.tracker.backwardJumps());
    ASSERT_EQ(0u, t.tracker.wraps());
    t.present(1.150, 0, true, 0, 7);
    ASSERT_EQ(5, static_cast<int>(t.tracker.advancedFrames()));
    ASSERT_EQ(1u, t.tracker.wraps());
    // a forward seek stamped before the wrap is an ordinary forward jump and leaves it pending too
    PresentDriver s;
    s.startMeasuredPlay();
    s.present(1.000, 1, true, 0, 7);
    const int forwardStamp = s.engineWraps;
    ++s.engineWraps;
    s.present(1.050, 4, true, 0, 7, forwardStamp);
    ASSERT_EQ(3, static_cast<int>(s.tracker.advancedFrames()));
    ASSERT_EQ(3, s.tracker.maxJump());
    ASSERT_EQ(0u, s.tracker.wraps());
    s.present(1.100, 0, true, 0, 7);
    ASSERT_EQ(1u, s.tracker.wraps());
    ASSERT_EQ(3 + 4, static_cast<int>(s.tracker.advancedFrames()));
}

// r20 (adversary A1 on ce603f37): Loop on, drop-frame off, cut 1..8 (0..7). 7 is presented, the engine wraps and requests 0, and Cut
// Out moves to 16 before 0 presents. MainWindow passed the current width, so the landing was credited 0 + 16 - 7 = 9, inflating source
// advance and source fps. The recorder keeps each wrap's width from when the engine made it: 0 + 8 - 7 = 1.
TEST(PlaybackSourceAdvanceTracker, R20AdversaryA1AWrapIsCreditedTheCutWidthItHadWhenTheEngineMadeIt)
{
    playback_frame_range::PlaybackWrapRecorder wraps;
    PlaybackSourceAdvanceTracker t;
    int consumed = wraps.engineWraps;
    t.notePresented(1.000, 7, 0, 0, 7);
    wraps.noteEngineWrap(8);
    const int stamp = wraps.engineWraps;      // the request for 0; Cut Out then becomes 16, and no later wrap has happened
    int before = consumed;
    int crossed = presentationLoopWraps(wraps.engineWraps, stamp, consumed, true);
    t.notePresented(1.050, 0, crossed, wraps.engineWrapsWidth(before, crossed));
    ASSERT_EQ(1, crossed);
    ASSERT_EQ(1, static_cast<int>(t.advancedFrames()));
    ASSERT_EQ(1u, t.wraps());
    ASSERT_EQ(0, t.maxJump());
    // two wraps crossed at once, one before and one after the change: 0 -> (8) -> (16) -> 3 = 3 + 8 + 16 - 0 = 27 more
    wraps.noteEngineWrap(8);
    wraps.noteEngineWrap(16);
    before = consumed;
    crossed = presentationLoopWraps(wraps.engineWraps, wraps.engineWraps, consumed, true);
    t.notePresented(1.100, 3, crossed, wraps.engineWrapsWidth(before, crossed));
    ASSERT_EQ(2, crossed);
    ASSERT_EQ(28, static_cast<int>(t.advancedFrames()));
    ASSERT_EQ(3u, t.wraps());
    // the width lookup: nothing crossed, or nothing recorded for a wrap number, is no width
    ASSERT_EQ(int64_t(0), wraps.engineWrapsWidth(1, 0));
    ASSERT_EQ(int64_t(16), wraps.engineWrapsWidth(2, 5));
    ASSERT_EQ(int64_t(0), wraps.engineWrapsWidth(9, 2));
}
