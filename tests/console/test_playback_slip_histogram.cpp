// PLAYBACK-BACHELOR-PRESENT-JITTER-1: the per-present slip histogram behind playback_smoke.slip_summary.
// Driven with an injected clock and no GUI: the timeline moves (noteTimelineMove, as playbackHandling does)
// and frames are presented (notePresent, as notePlaybackSmokePresentedFrame does), and the summary must
// count every lost frame once, at the present that skipped it, with one cause.
#include "../common/minitest.h"

#include "../../platform/qt/GpuPresentTailWindow.h"
#include "../../platform/qt/PlaybackNativePaceGuard.h"
#include "../../platform/qt/PlaybackSlipHistogram.h"

#include <cmath>
#include <string>

using playback_slip::AdvancePath;
using playback_slip::PresentSample;
using playback_slip::SlipClass;
using playback_slip::SlipHistogram;
using playback_slip::Summary;
using playback_slip::UpstreamStage;

namespace
{

constexpr double kNativeFps = 24000.0 / 1001.0; // 23.976
const double kPeriodMs = 1000.0 / kNativeFps;   // 41.708

long long cls( const Summary &s, SlipClass c ) { return s.classFrames[static_cast<int>( c )]; }

// Plays the clip like the drop-frame engine at native pace: each present shows frame k at t0 + k x period,
// and its early advance has already moved the timeline to k + 1.
struct Driver
{
    SlipHistogram h;
    int timeline = 0;
    double t0 = 1000.0;

    explicit Driver( int start = 0 )
    {
        h.reset( start, kNativeFps );
        timeline = start;
    }

    double at( int k ) const { return t0 + k * kPeriodMs; }

    void move( int pos, double ms, AdvancePath path = AdvancePath::DropTick )
    {
        h.noteTimelineMove( path, pos, ms, path == AdvancePath::Other ? 0.0 : pos - timeline, 0.0 );
        timeline = pos;
    }

    void present( int frame, double ms, double readyMs, double drawMs = 5.0, double decodeMs = 2.0 )
    {
        PresentSample s;
        s.displayFrame = frame;
        s.presentMs = ms;
        s.readyKnown = true;
        s.readyMs = readyMs;
        s.decodeMs = decodeMs;
        s.reconMs = 4.0;
        s.renderMs = 9.0;
        s.queueMs = 3.0;
        s.drawMs = drawMs;
        s.uiLatencyMs = 1.0;
        s.timelinePosition = timeline;
        s.lookaheadCovered = true;
        h.notePresent( s );
    }

    // Frames [from, to) presented on time: ready 10 ms before the present, timeline one ahead.
    void clean( int from, int to )
    {
        for( int k = from; k < to; ++k )
        {
            move( k + 1, at( k ) );
            present( k, at( k ), at( k ) - 10.0 );
        }
    }
};

} // namespace

TEST(PlaybackSlipHistogram, ACleanNativeSequenceHasNoSlips)
{
    Driver d;
    d.clean( 0, 600 );
    const Summary s = d.h.finish( d.at( 600 ), d.timeline );
    ASSERT_EQ( 600, s.presents );
    ASSERT_EQ( 0, s.slipEvents );
    ASSERT_EQ( 0LL, s.slipsTotal );
    ASSERT_EQ( 0, s.maxSlip );
    ASSERT_EQ( 599LL, s.histSlip[0] );
    ASSERT_EQ( 599LL, s.histInterval[0] ); // 41.7 ms <= 45
    ASSERT_EQ( 0, s.startupCatchupFrames );
    ASSERT_NEAR( kNativeFps, s.nativeEquivPresentedFps, 1e-9 );
    ASSERT_EQ( 600LL, s.advanceByPath[static_cast<int>( AdvancePath::DropTick )] );
    ASSERT_EQ( 0LL, s.advanceByPath[static_cast<int>( AdvancePath::Other )] );
}

TEST(PlaybackSlipHistogram, OneTwoFrameStepIsOneSlipInBucketOne)
{
    Driver d;
    d.clean( 0, 10 );
    // The timeline moves past 10 one period after frame 9 and past 11 a period later; frame 10's render
    // only lands after its deadline, so frame 11 is shown next: a 2-frame step, one lost frame.
    d.move( 11, d.at( 10 ) );
    d.move( 12, d.at( 11 ) );
    d.present( 11, d.at( 11 ) + 1.0, d.at( 11 ), 5.0, 30.0 );
    d.clean( 12, 20 );
    const Summary s = d.h.finish( d.at( 20 ), d.timeline );
    ASSERT_EQ( 1, s.slipEvents );
    ASSERT_EQ( 1LL, s.slipsTotal );
    ASSERT_EQ( 1, s.maxSlip );
    ASSERT_EQ( 1LL, s.histSlip[1] );
    ASSERT_EQ( 1LL, s.histInterval[3] ); // 84.4 ms: (83.4, 125]
    ASSERT_EQ( 1LL, cls( s, SlipClass::UpstreamLate ) );
    ASSERT_EQ( 0LL, cls( s, SlipClass::Gap ) );
    ASSERT_EQ( 1LL, s.upstreamStageFrames[static_cast<int>( UpstreamStage::Decode )] );
    ASSERT_TRUE( s.slipLines.size() == 1 );
    ASSERT_EQ( 11, s.slipLines[0].frame );
    ASSERT_TRUE( s.slipLines[0].sub == UpstreamStage::Decode );
    ASSERT_TRUE( s.maxIntervalClass == SlipClass::UpstreamLate );
    ASSERT_EQ( 11, s.maxIntervalFrame );
}

TEST(PlaybackSlipHistogram, A300MsIntervalIsAGap)
{
    Driver d;
    d.clean( 0, 10 );
    const double t = d.at( 9 ) + 300.0;
    d.move( 17, t );
    d.present( 16, t, t - 1.0 );
    const Summary s = d.h.finish( t + 100.0, d.timeline );
    ASSERT_EQ( 6LL, s.slipsTotal );
    ASSERT_EQ( 6LL, cls( s, SlipClass::Gap ) );
    ASSERT_EQ( 1LL, s.histInterval[5] );
    ASSERT_EQ( 1LL, s.histSlip[4] );
    ASSERT_NEAR( 300.0, s.maxIntervalMs, 1e-9 );
    ASSERT_TRUE( s.maxIntervalClass == SlipClass::Gap );
    ASSERT_NEAR( 0.0, s.nonCaptureNonGapPer1000, 1e-12 );
}

TEST(PlaybackSlipHistogram, AnIntervalWithAGrabIsCapture)
{
    Driver d;
    d.clean( 0, 10 );
    d.h.noteGrab( 48.0 ); // the grab after frame 9's present, inside the 9 -> 11 interval
    d.move( 12, d.at( 11 ) );
    d.present( 11, d.at( 11 ), d.at( 11 ) - 2.0 );
    d.clean( 12, 14 );
    const Summary s = d.h.finish( d.at( 14 ), d.timeline );
    ASSERT_EQ( 1LL, cls( s, SlipClass::Capture ) );
    ASSERT_EQ( 1, s.grabs );
    ASSERT_NEAR( 48.0, s.grabMsTotal, 1e-12 );
    ASSERT_NEAR( 48.0, s.slipLines[0].grabMs, 1e-12 );
    ASSERT_NEAR( 0.0, s.nonCapturePer1000, 1e-12 );
    // the grab is charged once: the next interval has none
    ASSERT_EQ( 1, s.slipEvents );
}

TEST(PlaybackSlipHistogram, FirstPresentAndStartupCatchupAreNeverSlips)
{
    Driver d;
    // 1.4 s wait for the first frame: the first present shows frame 5, and its early advance repays the wait
    // by jumping the timeline to 40. The next present shows frame 40: 34 frames passed, all catch-up.
    const double first = d.t0 + 1400.0;
    d.move( 40, first );
    d.present( 5, first, first - 1.0 );
    d.move( 41, first + kPeriodMs );
    d.present( 40, first + kPeriodMs, first + kPeriodMs - 5.0 );
    for( int k = 41; k < 60; ++k )
    {
        const double ms = first + ( k - 39 ) * kPeriodMs;
        d.move( k + 1, ms );
        d.present( k, ms, ms - 5.0 );
    }
    const Summary s = d.h.finish( first + 21 * kPeriodMs, d.timeline );
    ASSERT_EQ( 0LL, s.slipsTotal );
    ASSERT_EQ( 0, s.slipEvents );
    ASSERT_EQ( 5 + 34, s.startupCatchupFrames );
    ASSERT_EQ( 40, s.timelineDeltaAtFirstPresent );
    ASSERT_EQ( 20, s.timelineFramesAfterFirst );
}

TEST(PlaybackSlipHistogram, ALoopWrapIsNotASlip)
{
    Driver d( 925 );
    for( int k = 925; k < 935; ++k )
    {
        d.move( k + 1, d.at( k - 925 ) );
        d.present( k, d.at( k - 925 ), d.at( k - 925 ) - 5.0 );
    }
    d.move( 0, d.at( 10 ), AdvancePath::LoopWrap );
    d.move( 1, d.at( 10 ) );
    d.present( 0, d.at( 10 ), d.at( 10 ) - 5.0 );
    d.move( 2, d.at( 11 ) );
    d.present( 1, d.at( 11 ), d.at( 11 ) - 5.0 );
    const Summary s = d.h.finish( d.at( 12 ), d.timeline );
    ASSERT_EQ( 1, s.wraps );
    ASSERT_EQ( 0LL, s.slipsTotal );
    ASSERT_EQ( 0, s.maxSlip );
    ASSERT_EQ( -935LL, s.advanceByPath[static_cast<int>( AdvancePath::LoopWrap )] );
}

TEST(PlaybackSlipHistogram, IntervalBucketEdgesAreExact)
{
    using playback_slip::presentIntervalBucket;
    ASSERT_EQ( 0, presentIntervalBucket( 0.0 ) );
    ASSERT_EQ( 0, presentIntervalBucket( 45.0 ) );
    ASSERT_EQ( 1, presentIntervalBucket( 45.001 ) );
    ASSERT_EQ( 1, presentIntervalBucket( 62.5 ) );
    ASSERT_EQ( 2, presentIntervalBucket( 62.501 ) );
    ASSERT_EQ( 2, presentIntervalBucket( 83.4 ) );
    ASSERT_EQ( 3, presentIntervalBucket( 83.401 ) );
    ASSERT_EQ( 3, presentIntervalBucket( 125.0 ) );
    ASSERT_EQ( 4, presentIntervalBucket( 125.001 ) );
    ASSERT_EQ( 4, presentIntervalBucket( 250.0 ) );
    ASSERT_EQ( 5, presentIntervalBucket( 250.001 ) );
    using playback_slip::slipSizeBucket;
    ASSERT_EQ( 0, slipSizeBucket( 0 ) );
    ASSERT_EQ( 1, slipSizeBucket( 1 ) );
    ASSERT_EQ( 3, slipSizeBucket( 3 ) );
    ASSERT_EQ( 4, slipSizeBucket( 4 ) );
    ASSERT_EQ( 4, slipSizeBucket( 7 ) );
    ASSERT_EQ( 5, slipSizeBucket( 8 ) );
    // 250 ms exactly is a GAP (>= 250) although it is the last edge of bucket 4.
    Driver d;
    d.clean( 0, 3 );
    const double t = d.at( 2 ) + 250.0;
    d.move( 6, t );
    d.present( 5, t, t - 1.0 );
    const Summary s = d.h.finish( t, d.timeline );
    ASSERT_EQ( 2LL, cls( s, SlipClass::Gap ) );
    ASSERT_EQ( 1LL, s.histInterval[4] );
}

TEST(PlaybackSlipHistogram, PerSlipLinesStopAt64)
{
    Driver d;
    int frame = 0;
    double ms = d.t0;
    for( int i = 0; i < 100; ++i )
    {
        d.move( frame + 1, ms );
        d.present( frame, ms, ms - 5.0 );
        frame += 2; // every present skips one
        ms += 2.0 * kPeriodMs;
    }
    const Summary s = d.h.finish( ms, d.timeline );
    ASSERT_EQ( 99, s.slipEvents );
    ASSERT_EQ( 99LL, s.slipsTotal );
    ASSERT_TRUE( s.slipLines.size() == static_cast<size_t>( playback_slip::kMaxSlipLines ) );
    ASSERT_EQ( 64, playback_slip::kMaxSlipLines );
}

TEST(PlaybackSlipHistogram, ReadyBeforeTheDeadlineButPresentedLateIsGuiLate)
{
    Driver d;
    d.clean( 0, 10 );
    // Frame 11 was ready 5 ms after frame 9's present -- long before frame 10's deadline -- but the GUI thread
    // only got to it two periods later.
    d.move( 11, d.at( 10 ) );
    d.move( 12, d.at( 11 ) );
    d.present( 11, d.at( 11 ) + 1.0, d.at( 9 ) + 5.0 );
    const Summary s = d.h.finish( d.at( 12 ), d.timeline );
    ASSERT_EQ( 1LL, cls( s, SlipClass::GuiLate ) );
    ASSERT_EQ( 0LL, cls( s, SlipClass::UpstreamLate ) );
}

TEST(PlaybackSlipHistogram, TwoTimelineFramesInOnePeriodIsClock)
{
    Driver d;
    d.clean( 0, 10 );
    // The timeline steps past 10 and 11 at once, one period after frame 9; frame 11 was ready in time and
    // drawn at once.
    d.move( 12, d.at( 10 ) );
    d.present( 11, d.at( 10 ), d.at( 10 ) - 2.0 );
    const Summary s = d.h.finish( d.at( 11 ), d.timeline );
    ASSERT_EQ( 1LL, cls( s, SlipClass::Clock ) );
    ASSERT_EQ( 1LL, s.slipsTotal );
}

TEST(PlaybackSlipHistogram, TimelinePathsAddUpAndUnexplainedTravelIsOther)
{
    Driver d;
    d.clean( 0, 100 );
    d.move( 130, d.at( 100 ), AdvancePath::Other ); // a slider move no engine path made
    const Summary s = d.h.finish( d.at( 101 ), d.timeline + 5 ); // and 5 frames nobody reported
    ASSERT_EQ( 100LL, s.advanceByPath[static_cast<int>( AdvancePath::DropTick )] );
    ASSERT_EQ( 35LL, s.advanceByPath[static_cast<int>( AdvancePath::Other )] );
    ASSERT_NEAR( 100.0, s.paceGuardGrantedFrames, 1e-9 );
    ASSERT_NEAR( 99.0, s.paceGuardGrantedAfterFirstFrames, 1e-9 );
}

TEST(PlaybackSlipHistogram, RatesAfterTheFirstPresent)
{
    // 598 timeline frames in 23.66 s after the first present (the r1b b-cin-head5-cuda1 shape) with 547
    // further presents: the histogram reports the same rates as pace_summary, plus the native-equivalent rate.
    SlipHistogram h;
    h.reset( 0, kNativeFps );
    h.noteTimelineMove( AdvancePath::DropTick, 1, 1427.0, 1.0, 0.0 );
    PresentSample first;
    first.displayFrame = 0;
    first.presentMs = 1427.0;
    first.timelinePosition = 1;
    h.notePresent( first );
    double ms = 1427.0;
    int frame = 0;
    for( int i = 1; i <= 547; ++i )
    {
        ms = 1427.0 + i * ( 23660.0 / 548.0 );
        frame = ( i * 598 ) / 548;
        h.noteTimelineMove( AdvancePath::DropTick, frame + 1, ms, 1.0, 0.0 );
        PresentSample p;
        p.displayFrame = frame;
        p.presentMs = ms;
        p.timelinePosition = frame + 1;
        h.notePresent( p );
    }
    const Summary s = h.finish( 1427.0 + 23660.0, 599 );
    ASSERT_EQ( 598, s.timelineFramesAfterFirst );
    ASSERT_NEAR( 598.0 * 1000.0 / 23660.0, s.timelineAfterFirstFps, 1e-9 );
    ASSERT_NEAR( 547.0 * 1000.0 / 23660.0, s.presentedAfterFirstFps, 1e-9 );
    ASSERT_NEAR( kNativeFps * 547.0 / 598.0, s.nativeEquivPresentedFps, 1e-9 );
}

namespace
{

// The drop-frame engine as playbackHandling runs it: an 8 ms poll asking for elapsed x pace, plus a predictive
// whole-frame request on every present of a renderer that takes renderMs. resetEveryTick models a guard that is
// re-armed mid-session (the leak the lead check exists to expose).
Summary guardedRun( double renderMs, bool resetEveryTick )
{
    SlipHistogram h;
    h.reset( 0, kNativeFps );
    playback_native_pace::NativePaceGuard guard;
    double position = 0.0;
    double lastTickMs = 0.0;
    double nextPresentMs = renderMs;
    for( double t = 0.0; t < 25000.0; t += 8.0 )
    {
        double request = kNativeFps * ( t - lastTickMs ) / 1000.0;
        if( t >= nextPresentMs )
        {
            request = std::max( request, 1.0 ); // the predictive advance asks for a whole frame
            nextPresentMs = t + renderMs;
        }
        lastTickMs = t;
        if( resetEveryTick ) guard.reset();
        const bool armedBefore = guard.armed();
        const double granted = guard.grant( request, t, kNativeFps );
        position += granted;
        h.noteTimelineMove( AdvancePath::DropTick, static_cast<int>( position ), t, granted, guard.creditFrames(),
                            kNativeFps, !armedBefore );
    }
    return h.finish( 25000.0, static_cast<int>( position ) );
}

} // namespace

TEST(PlaybackSlipHistogram, AGuardedRunNeverLeadsTheGuardCeiling)
{
    const Summary fast = guardedRun( 9.0, false );
    ASSERT_TRUE( fast.paceGuardMaxLeadFrames <= 1e-9 );
    ASSERT_EQ( 1, fast.paceGuardRearms );   // the arm at the first grant only
    ASSERT_NEAR( kNativeFps, fast.paceGuardFpsMin, 1e-12 );
    ASSERT_NEAR( kNativeFps, fast.paceGuardFpsMax, 1e-12 );
    const Summary slow = guardedRun( 70.0, false );
    ASSERT_TRUE( slow.paceGuardMaxLeadFrames <= 1e-9 );
}

TEST(PlaybackSlipHistogram, AGuardReArmedMidSessionShowsALeadAndTheReArms)
{
    const Summary leak = guardedRun( 9.0, true );
    ASSERT_TRUE( leak.paceGuardRearms > 1000 );
    ASSERT_TRUE( leak.paceGuardMaxLeadFrames > 100.0 ); // a 9 ms renderer outruns native once nothing carries over
}
TEST(PlaybackSlipHistogram, TheDelayedStartupCatchUpIsNeitherASlipNorPacedThroughput)
{
    // The r1b b-cin-head5-cuda1 shape: Play at 0, first present at 1427 ms with the timeline at 1 (the first present's
    // advance repaid nothing), two slow presents, then ONE burst moves the timeline to the wall time since Play, and
    // the rest is paced. Raw, 598 timeline frames in ~23.6 s read above 25 fps; the burst's first 33 frames are the
    // wait for the first frame (34 owed at 1427 ms, 1 already advanced), the rest of it a real post-first stall.
    SlipHistogram h;
    h.reset( 0, kNativeFps );
    h.setPlayStart( 0.0 );
    h.noteTimelineMove( AdvancePath::DropTick, 1, 10.0, 1.0, 0.0 );
    auto present = [&h]( int frame, double ms, int timeline )
    {
        PresentSample s;
        s.displayFrame = frame;
        s.presentMs = ms;
        s.readyKnown = true;
        s.readyMs = ms - 1.0;
        s.timelinePosition = timeline;
        h.notePresent( s );
    };
    present( 0, 1427.0, 1 );
    h.noteTimelineMove( AdvancePath::DropTick, 2, 1927.0, 1.0, 0.0 );
    present( 1, 1927.0, 2 );
    h.noteTimelineMove( AdvancePath::DropTick, 3, 2427.0, 1.0, 0.0 );
    present( 2, 2427.0, 3 );
    const double burstMs = 2470.0;
    const int owed = static_cast<int>( burstMs * kNativeFps / 1000.0 );   // 59
    h.noteTimelineMove( AdvancePath::DropTick, owed, burstMs, owed - 3, 0.0 );
    present( owed - 1, burstMs, owed );
    int timeline = owed;
    double ms = burstMs;
    while( timeline < 599 )
    {
        ms += kPeriodMs;
        ++timeline;
        h.noteTimelineMove( AdvancePath::DropTick, timeline, ms, 1.0, 0.0 );
        present( timeline - 1, ms, timeline );
    }
    const Summary s = h.finish( ms, timeline );
    const double pacedMs = ms - 1427.0;
    ASSERT_EQ( 33, s.startupWaitCreditFrames );
    ASSERT_EQ( 33, s.startupCatchupAfterFirstFrames );
    ASSERT_EQ( 33, s.startupCatchupFrames );
    ASSERT_EQ( static_cast<long long>( owed - 1 - 2 - 1 - 33 ), s.slipsTotal );    // the post-first stall only
    ASSERT_TRUE( s.timelineAfterFirstRawFps > 25.0 );                               // the anomaly, reproduced
    ASSERT_NEAR( 598.0 * 1000.0 / pacedMs, s.timelineAfterFirstRawFps, 1e-9 );
    // paced: never more than the pace plus one frame over the paced interval
    ASSERT_TRUE( s.timelineAfterFirstFps <= kNativeFps + 1000.0 / pacedMs + 1e-9 );
    ASSERT_EQ( 598 - 33, s.timelineFramesAfterFirst );
}

TEST(PlaybackSlipHistogram, TheCatchUpNeverExceedsTheWaitAndStopsOnceCaughtUp)
{
    // Caught up at the second present: a later skip is a real slip, whatever credit is left.
    SlipHistogram h;
    h.reset( 0, kNativeFps );
    h.setPlayStart( 0.0 );
    PresentSample s;
    s.displayFrame = 0; s.presentMs = 400.0; s.readyMs = 399.0; s.timelinePosition = 1;
    h.noteTimelineMove( AdvancePath::DropTick, 1, 400.0, 1.0, 0.0 );
    h.notePresent( s );                                                   // owes floor(400 x 23.976 / 1000) = 9: credit 8
    h.noteTimelineMove( AdvancePath::DropTick, 12, 450.0, 11.0, 0.0 );
    s.displayFrame = 11; s.presentMs = 450.0; s.readyMs = 449.0; s.timelinePosition = 12;
    h.notePresent( s );                                                   // a 10-frame skip: 8 catch-up, 2 slips; caught up
    h.noteTimelineMove( AdvancePath::DropTick, 16, 600.0, 4.0, 0.0 );
    s.displayFrame = 15; s.presentMs = 600.0; s.readyMs = 599.0; s.timelinePosition = 16;
    h.notePresent( s );                                                   // 3 more skipped: real slips
    const Summary sum = h.finish( 600.0, 16 );
    ASSERT_EQ( 8, sum.startupWaitCreditFrames );
    ASSERT_EQ( 8, sum.startupCatchupAfterFirstFrames );
    ASSERT_EQ( 2LL + 3LL, sum.slipsTotal );
}

TEST(PlaybackSlipHistogram, AGrantFreeStallIsRepaidInOneBurst)
{
    // NativePaceGuard banks credit while no grant runs; the first unshaped request after a 1 s stall takes it all.
    SlipHistogram h;
    h.reset( 0, kNativeFps );
    h.setPlayStart( 0.0 );
    playback_native_pace::NativePaceGuard guard;
    double position = 0.0;
    for( double t = 0.0; t <= 1000.0; t += kPeriodMs )
    {
        const double g = guard.grant( 1.0, t, kNativeFps );
        position += g;
        h.noteTimelineMove( AdvancePath::DropTick, static_cast<int>( position ), t, g, guard.creditFrames(), kNativeFps, false );
    }
    const double afterStall = 2000.0;
    const double burst = guard.grant( 1000.0 * kNativeFps / 1000.0 + 1.0, afterStall, kNativeFps );
    position += burst;
    h.noteTimelineMove( AdvancePath::DropTick, static_cast<int>( position ), afterStall, burst, guard.creditFrames(), kNativeFps, false );
    const Summary s = h.finish( afterStall, static_cast<int>( position ) );
    ASSERT_TRUE( burst > 20.0 );
    ASSERT_EQ( 1, s.paceGuardBursts );
    ASSERT_NEAR( burst, s.paceGuardMaxGrantFrames, 1e-12 );
    ASSERT_NEAR( burst - 1.0, s.paceGuardBurstFrames, 1e-12 );
    ASSERT_TRUE( s.paceGuardMaxLeadFrames <= 1e-9 );   // within the guard's own contract: carry + elapsed since its last grant
}
TEST(PlaybackSlipHistogram, TheSkipAfterAStallIsMarkedAfterGapButKeepsItsClass)
{
    // A 300 ms stall whose own present shows the next frame in order (a lookahead frame rendered before it), then a
    // short-interval present that skips what the stall cost: the slip keeps its pre-registered class (its own
    // interval is short) and is counted in slips_after_gap.
    Driver d;
    d.clean( 0, 10 );
    const double stalled = d.at( 9 ) + 300.0;
    d.move( 17, stalled );
    d.present( 10, stalled, d.at( 9 ) + 5.0 );          // in order: no slip, a 300 ms interval
    const double next = stalled + 40.0;
    d.move( 18, next );
    d.present( 17, next, next - 2.0 );                    // 6 frames skipped one present later
    d.clean( 18, 20 );
    const Summary s = d.h.finish( d.at( 20 ) + 300.0, d.timeline );
    ASSERT_EQ( 6LL, s.slipsTotal );
    ASSERT_EQ( 0LL, cls( s, SlipClass::Gap ) );
    ASSERT_EQ( 1, s.slipEventsAfterGap );
    ASSERT_EQ( 6LL, s.slipsAfterGap );
    ASSERT_TRUE( s.slipLines[0].afterGap );
    ASSERT_EQ( 10, s.maxIntervalFrame );
    ASSERT_TRUE( s.maxIntervalClass == SlipClass::None );
}
// PLAYBACK-GL-PRESENT-SETUP-STALL-1: the texture-present setup counters behind slip_summary's present_setup_* and
// lut_* fields. The first setup block is the session's one-time allocation: reported apart, counted nowhere else.
namespace
{
playback_slip::PresentSetupSample setupSample( double totalMs, double lutMs = 0.0, double drainMs = 0.0,
                                              bool rebuilt = false, const char *miss = "none" )
{
    using playback_slip::SetupPart;
    playback_slip::PresentSetupSample s;
    s.totalMs = totalMs;
    s.partsMs[static_cast<size_t>( SetupPart::Program )] = 0.01;
    s.partsMs[static_cast<size_t>( SetupPart::Lut )] = lutMs;
    s.partsMs[static_cast<size_t>( SetupPart::BlurDrain )] = drainMs;
    s.lutRebuilt = rebuilt;
    s.lutMiss = miss;
    return s;
}
}

TEST(PlaybackSlipHistogram, PresentSetupFirstIsExcludedAndTheStallThresholdIsStrict)
{
    using playback_slip::SetupPart;
    SlipHistogram h;
    h.reset( 0, kNativeFps );
    h.notePresentSetup( setupSample( 400.0, 390.0, 0.0, true, "unbuilt" ) );   // first: allocation, apart
    h.notePresentSetup( setupSample( 50.0, 0.0, 49.0 ) );                     // exactly 50: not over
    h.notePresentSetup( setupSample( 50.001, 0.0, 49.0 ) );                   // over
    h.notePresentSetup( setupSample( 1.5, 0.2, 0.3 ) );
    const Summary s = h.finish( 1000.0, 0 );
    ASSERT_TRUE( s.presentSetupFirstKnown );
    ASSERT_NEAR( 400.0, s.presentSetupFirstMs, 1e-12 );
    ASSERT_EQ( 3, s.presentSetupSamplesAfterFirst );
    ASSERT_EQ( 1, s.presentSetupOver50 );
    ASSERT_NEAR( 50.001, s.presentSetupMaxMs, 1e-12 );
    ASSERT_TRUE( s.presentSetupMaxPart == SetupPart::BlurDrain );
    ASSERT_EQ( 0, s.lutRebuildsAfterFirst );                                   // the first present's rebuild is not counted
    ASSERT_TRUE( s.lutMissFields.empty() );
}

TEST(PlaybackSlipHistogram, PresentSetupMaxPartIsAttributedIncludingUntimed)
{
    using playback_slip::SetupPart;
    ASSERT_TRUE( playback_slip::presentSetupMaxPart( setupSample( 210.0, 5.0, 200.0 ) ) == SetupPart::BlurDrain );
    ASSERT_TRUE( playback_slip::presentSetupMaxPart( setupSample( 210.0, 200.0, 5.0 ) ) == SetupPart::Lut );
    // 210 - (0.01 + 5 + 5) of the block no part timed: UNTIMED.
    ASSERT_TRUE( playback_slip::presentSetupMaxPart( setupSample( 210.0, 5.0, 5.0 ) ) == SetupPart::Untimed );
    ASSERT_EQ( std::string( "untimed" ), std::string( playback_slip::setupPartName( SetupPart::Untimed ) ) );
    ASSERT_EQ( std::string( "none" ), std::string( playback_slip::setupPartName( SetupPart::Count ) ) );

    // The session's max part is the max-setup present's own, not the session's largest part anywhere.
    SlipHistogram h;
    h.reset( 0, kNativeFps );
    h.notePresentSetup( setupSample( 2.0 ) );
    h.notePresentSetup( setupSample( 120.0, 110.0, 1.0 ) );
    h.notePresentSetup( setupSample( 230.0, 1.0, 220.0 ) );
    h.notePresentSetup( setupSample( 90.0, 85.0, 1.0 ) );
    const Summary s = h.finish( 1000.0, 0 );
    ASSERT_EQ( 3, s.presentSetupOver50 );
    ASSERT_NEAR( 230.0, s.presentSetupMaxMs, 1e-12 );
    ASSERT_TRUE( s.presentSetupMaxPart == SetupPart::BlurDrain );
}

TEST(PlaybackSlipHistogram, PresentSetupLutMissFieldsTallyRebuildsByGroupInFirstSeenOrder)
{
    SlipHistogram h;
    h.reset( 0, kNativeFps );
    h.notePresentSetup( setupSample( 30.0, 29.0, 0.0, true, "unbuilt" ) );   // first: excluded
    h.notePresentSetup( setupSample( 60.0, 59.0, 0.0, true, "highest_green_diso" ) );
    h.notePresentSetup( setupSample( 1.0 ) );
    h.notePresentSetup( setupSample( 61.0, 60.0, 0.0, true, "levels" ) );
    h.notePresentSetup( setupSample( 62.0, 61.0, 0.0, true, "highest_green_diso" ) );
    const Summary s = h.finish( 1000.0, 0 );
    ASSERT_EQ( 3, s.lutRebuildsAfterFirst );
    ASSERT_EQ( static_cast<size_t>( 2 ), s.lutMissFields.size() );
    ASSERT_EQ( std::string( "highest_green_diso" ), s.lutMissFields[0].first );
    ASSERT_EQ( 2LL, s.lutMissFields[0].second );
    ASSERT_EQ( std::string( "levels" ), s.lutMissFields[1].first );
    ASSERT_EQ( 1LL, s.lutMissFields[1].second );

    // A session with no setup block reports none.
    SlipHistogram empty;
    empty.reset( 0, kNativeFps );
    const Summary e = empty.finish( 1000.0, 0 );
    ASSERT_FALSE( e.presentSetupFirstKnown );
    ASSERT_EQ( 0, e.presentSetupOver50 );
    ASSERT_TRUE( e.presentSetupMaxPart == playback_slip::SetupPart::Count );
}

// PLAYBACK-PRESENT-TAIL-TIMING-1: each setup block carries the window from the previous present's swap return to its
// own start; the max-setup present's window is bucketed by a pre-registered first-match rule.
namespace
{
playback_slip::PresentSetupSample tailSample( double totalMs, double windowMs, double idleMs, int qtPaintSwaps = 0,
                                             int grabSwaps = 0, bool prevKnown = true, bool idleKnown = true,
                                             bool tailKnown = true )
{
    playback_slip::PresentSetupSample s = setupSample( totalMs );
    s.tailKnown = tailKnown;
    s.tail.prevKnown = prevKnown;
    s.tail.swapReturnToSetupMs = windowMs;
    s.tail.guiIdleKnown = idleKnown;
    s.tail.guiIdleMs = idleMs;
    s.tail.qtPaintSwaps = qtPaintSwaps;
    s.tail.grabSwaps = grabSwaps;
    s.tail.guiIdleWaits = 1;   // r3: a window whose probe saw a yield (the no-yield cases build their own)
    return s;
}
}

TEST(PlaybackSlipHistogram, PresentSetupBucketRuleIsFirstMatchWithStrictGuiBusyEdge)
{
    using playback_slip::SetupBucket;
    using playback_slip::presentSetupBucket;
    // UNKNOWN first: no tail, no previous window, or no idle probe -- even with swaps in the window.
    ASSERT_TRUE( presentSetupBucket( tailSample( 60.0, 80.0, 0.0, 1, 1, true, true, false ) ) == SetupBucket::Unknown );
    ASSERT_TRUE( presentSetupBucket( tailSample( 60.0, 80.0, 0.0, 1, 1, false ) ) == SetupBucket::Unknown );
    ASSERT_TRUE( presentSetupBucket( tailSample( 60.0, 80.0, 0.0, 1, 1, true, false ) ) == SetupBucket::Unknown );
    // PATH2 > PATH1 > GUI_BUSY.
    ASSERT_TRUE( presentSetupBucket( tailSample( 60.0, 80.0, 0.0, 1, 1 ) ) == SetupBucket::SwapPath2 );
    ASSERT_TRUE( presentSetupBucket( tailSample( 60.0, 80.0, 0.0, 1, 0 ) ) == SetupBucket::SwapPath1 );
    ASSERT_TRUE( presentSetupBucket( tailSample( 60.0, 80.0, 0.0 ) ) == SetupBucket::GuiBusy );
    // GUI_BUSY needs busy > 20 (strict) and busy at least half the window.
    ASSERT_TRUE( presentSetupBucket( tailSample( 60.0, 40.0, 20.0 ) ) == SetupBucket::SetupAfterSwap );   // busy 20.0
    ASSERT_TRUE( presentSetupBucket( tailSample( 60.0, 40.0, 19.999 ) ) == SetupBucket::GuiBusy );        // busy 20.001
    ASSERT_TRUE( presentSetupBucket( tailSample( 60.0, 50.0, 25.0 ) ) == SetupBucket::GuiBusy );          // 2*busy == R
    ASSERT_TRUE( presentSetupBucket( tailSample( 60.0, 50.5, 25.5 ) ) == SetupBucket::SetupAfterSwap );   // 2*busy < R
    ASSERT_TRUE( presentSetupBucket( tailSample( 60.0, 30.0, 25.0 ) ) == SetupBucket::SetupAfterSwap );
    ASSERT_NEAR( -1.0, playback_slip::presentSetupGuiBusyMs( tailSample( 60.0, 30.0, 25.0, 0, 0, true, false ) ), 1e-12 );
    ASSERT_EQ( std::string( "setup_after_swap" ), std::string( playback_slip::setupBucketName( SetupBucket::SetupAfterSwap ) ) );
    ASSERT_EQ( std::string( "gui_busy" ), std::string( playback_slip::setupBucketName( SetupBucket::GuiBusy ) ) );
    ASSERT_EQ( std::string( "swap_path1" ), std::string( playback_slip::setupBucketName( SetupBucket::SwapPath1 ) ) );
    ASSERT_EQ( std::string( "swap_path2" ), std::string( playback_slip::setupBucketName( SetupBucket::SwapPath2 ) ) );
    ASSERT_EQ( std::string( "unknown" ), std::string( playback_slip::setupBucketName( SetupBucket::Unknown ) ) );
    ASSERT_EQ( std::string( "none" ), std::string( playback_slip::setupBucketName( SetupBucket::None ) ) );
}

TEST(PlaybackSlipHistogram, PresentSetupMaxTailIsTheMaxSetupPresentsOwn)
{
    using playback_slip::SetupBucket;
    SlipHistogram h;
    h.reset( 0, kNativeFps );
    h.notePresentSetup( tailSample( 400.0, 900.0, 0.0, 4, 5 ) );    // first: allocation, apart
    h.notePresentSetup( tailSample( 120.0, 90.0, 80.0, 3, 0 ) );
    playback_slip::PresentSetupSample max = tailSample( 230.0, 60.0, 10.0 );
    max.tail.prevPostPaintMs = 7.0;
    max.tail.prevPostSwapMs = 16.0;
    max.fullscreen = true;
    h.notePresentSetup( max );
    h.notePresentSetup( tailSample( 90.0, 500.0, 0.0, 6, 2 ) );
    const Summary s = h.finish( 1000.0, 0 );
    ASSERT_NEAR( 230.0, s.presentSetupMaxMs, 1e-12 );
    ASSERT_TRUE( s.presentSetupMaxBucket == SetupBucket::GuiBusy );
    ASSERT_NEAR( 7.0, s.presentSetupMaxTail.prevPostPaintMs, 1e-12 );
    ASSERT_NEAR( 16.0, s.presentSetupMaxTail.prevPostSwapMs, 1e-12 );
    ASSERT_NEAR( 60.0, s.presentSetupMaxTail.swapReturnToSetupMs, 1e-12 );
    ASSERT_NEAR( 50.0, s.presentSetupMaxGuiBusyMs, 1e-12 );
    ASSERT_EQ( 0, s.presentSetupMaxTail.qtPaintSwaps );   // not the session maximum (6)
    ASSERT_EQ( 0, s.presentSetupMaxTail.grabSwaps );      // not the session maximum (2, or the first's 5)
    ASSERT_TRUE( s.presentSetupMaxFullscreen );
}

TEST(PlaybackSlipHistogram, PresentSetupFirstTailIsExcludedAndAnEmptySessionBucketsNone)
{
    using playback_slip::SetupBucket;
    SlipHistogram onlyFirst;
    onlyFirst.reset( 0, kNativeFps );
    playback_slip::PresentSetupSample first = tailSample( 400.0, 900.0, 0.0, 4, 5 );
    first.fullscreen = true;
    onlyFirst.notePresentSetup( first );
    const Summary f = onlyFirst.finish( 1000.0, 0 );
    ASSERT_EQ( 0, f.presentSetupSamplesAfterFirst );
    ASSERT_TRUE( f.presentSetupMaxBucket == SetupBucket::None );
    ASSERT_EQ( 0, f.presentSetupMaxTail.grabSwaps );
    ASSERT_EQ( 0, f.presentSetupMaxTail.qtPaintSwaps );
    ASSERT_FALSE( f.presentSetupMaxTail.prevKnown );
    ASSERT_NEAR( -1.0, f.presentSetupMaxGuiBusyMs, 1e-12 );
    ASSERT_FALSE( f.presentSetupMaxFullscreen );

    // After the first, a present without a tail (the GpuDisplayViewport route) buckets UNKNOWN.
    onlyFirst.notePresentSetup( tailSample( 5.0, 0.0, 0.0, 0, 0, false, false, false ) );
    ASSERT_TRUE( onlyFirst.finish( 1000.0, 0 ).presentSetupMaxBucket == SetupBucket::Unknown );

    SlipHistogram empty;
    empty.reset( 0, kNativeFps );
    ASSERT_TRUE( empty.finish( 1000.0, 0 ).presentSetupMaxBucket == SetupBucket::None );
}

// PLAYBACK-PRESENT-TAIL-TIMING-1 r3 (key3 B1): a window in which the GUI thread showed no yield at all -- the smoke
// runner's non-blocking processEvents never reaches aboutToBlock, and no runner sleep was counted -- has a structural
// zero idle. It must never name GUI_BUSY's fix row, and its busy is not known. Built from the fields 7fce5df1 already
// had, so this test compiles there and fails there (busy 30, gui_busy).
TEST(PlaybackSlipHistogram, PresentSetupWindowWithNoYieldNeverBucketsGuiBusy)
{
    playback_slip::PresentSetupSample s = setupSample( 60.0 );
    s.tailKnown = true;
    s.tail.prevKnown = true;
    s.tail.guiIdleKnown = true;
    s.tail.swapReturnToSetupMs = 30.0;
    s.tail.guiIdleMs = 0.0;
    ASSERT_FALSE( playback_slip::presentSetupBucket( s ) == playback_slip::SetupBucket::GuiBusy );
    ASSERT_NEAR( -1.0, playback_slip::presentSetupGuiBusyMs( s ), 1e-12 );
}

// PLAYBACK-PRESENT-TAIL-TIMING-1 r3: the window lifecycle GpuDisplayWindow drives (GpuPresentTailWindow.h), executed
// event by event, then bucketed the way notePlaybackSmokePresentedFrame feeds the histogram.
namespace
{
constexpr qint64 kMs = 1000000;

playback_slip::PresentSetupSample sampleFromTail( double totalMs, const GpuPresentTailTiming &t )
{
    playback_slip::PresentSetupSample s = setupSample( totalMs );
    s.tailKnown = true;
    s.tail.prevKnown = t.prev_known;
    s.tail.prevPostPaintMs = t.prev_post_paint_ms;
    s.tail.prevPostSwapMs = t.prev_post_swap_ms;
    s.tail.swapReturnToSetupMs = t.swap_return_to_setup_ms;
    s.tail.qtPaintSwaps = t.qt_paint_swaps;
    s.tail.qtPaintSwapMaxMs = t.qt_paint_swap_max_ms;
    s.tail.grabSwaps = t.grab_swaps;
    s.tail.guiIdleKnown = t.gui_idle_known;
    s.tail.guiIdleMs = t.gui_idle_ms;
    s.tail.guiIdleWaits = t.gui_idle_waits;
    s.tail.runnerSleepMs = t.runner_sleep_ms;
    s.tail.runnerSleeps = t.runner_sleeps;
    return s;
}
}

TEST(GpuPresentTailWindow, LifecycleAccumulatesInsideOneWindowAndResetsAtEveryPath3Swap)
{
    GpuPresentTailWindow w;
    // Nothing open yet: every event is ignored and a setup reports no previous window.
    w.blocked( 1 * kMs );
    w.awake( 5 * kMs );
    w.runnerSlept( 10 * kMs );
    w.qtPaintSwap( 3 * kMs );
    w.grabSwap();
    GpuPresentTailTiming t0;
    w.close( 20 * kMs, true, t0 );
    ASSERT_FALSE( t0.prev_known );
    ASSERT_EQ( 0, w.idleWaits );
    ASSERT_EQ( 0, w.runnerSleeps );
    ASSERT_EQ( 0, w.grabSwaps );

    // Present 1's path-3 swap returns at 100 ms; its window holds two block/wake pairs, one runner sleep, two path-1
    // pairs, one path-2 grab and a stray awake with no block before it.
    w.open( 100 * kMs, 7.0, 16.0 );
    ASSERT_TRUE( w.isOpen() );
    w.blocked( 102 * kMs );
    w.awake( 110 * kMs );
    w.runnerSlept( 10 * kMs );
    w.blocked( 125 * kMs );
    w.awake( 129 * kMs );
    w.awake( 131 * kMs );
    w.qtPaintSwap( 2 * kMs );
    w.qtPaintSwap( 5 * kMs );
    w.grabSwap();
    GpuPresentTailTiming t1;
    w.close( 140 * kMs, true, t1 );
    ASSERT_FALSE( w.isOpen() );
    ASSERT_TRUE( t1.prev_known );
    ASSERT_TRUE( t1.gui_idle_known );
    ASSERT_NEAR( 40.0, t1.swap_return_to_setup_ms, 1e-9 );
    ASSERT_NEAR( 12.0, t1.gui_idle_ms, 1e-9 );
    ASSERT_EQ( 2, t1.gui_idle_waits );
    ASSERT_NEAR( 10.0, t1.runner_sleep_ms, 1e-9 );
    ASSERT_EQ( 1, t1.runner_sleeps );
    ASSERT_EQ( 2, t1.qt_paint_swaps );
    ASSERT_NEAR( 5.0, t1.qt_paint_swap_max_ms, 1e-9 );
    ASSERT_EQ( 1, t1.grab_swaps );
    ASSERT_NEAR( 7.0, t1.prev_post_paint_ms, 1e-9 );
    ASSERT_NEAR( 16.0, t1.prev_post_swap_ms, 1e-9 );
    ASSERT_TRUE( playback_slip::presentSetupBucket( sampleFromTail( 60.0, t1 ) ) == playback_slip::SetupBucket::SwapPath2 );

    // Between that setup and the next swap no window is open: nothing counts.
    w.runnerSlept( 10 * kMs );
    w.blocked( 150 * kMs );
    w.awake( 160 * kMs );
    w.grabSwap();
    w.qtPaintSwap( 9 * kMs );

    // Present 2's swap opens a fresh window: nothing from window 1 or from the gap carries over.
    w.open( 200 * kMs, 3.0, 9.0 );
    GpuPresentTailTiming t2;
    w.close( 205 * kMs, true, t2 );
    ASSERT_TRUE( t2.prev_known );
    ASSERT_NEAR( 5.0, t2.swap_return_to_setup_ms, 1e-9 );
    ASSERT_NEAR( 0.0, t2.gui_idle_ms, 1e-12 );
    ASSERT_EQ( 0, t2.gui_idle_waits );
    ASSERT_NEAR( 0.0, t2.runner_sleep_ms, 1e-12 );
    ASSERT_EQ( 0, t2.runner_sleeps );
    ASSERT_EQ( 0, t2.qt_paint_swaps );
    ASSERT_NEAR( 0.0, t2.qt_paint_swap_max_ms, 1e-12 );
    ASSERT_EQ( 0, t2.grab_swaps );
    ASSERT_NEAR( 3.0, t2.prev_post_paint_ms, 1e-9 );
    ASSERT_NEAR( 9.0, t2.prev_post_swap_ms, 1e-9 );
    // A short window with no yield is still setup_after_swap: its busy cannot reach GUI_BUSY's edge.
    ASSERT_TRUE( playback_slip::presentSetupBucket( sampleFromTail( 60.0, t2 ) )
                 == playback_slip::SetupBucket::SetupAfterSwap );

    // A second setup with no swap between reports no previous window.
    GpuPresentTailTiming t3;
    w.close( 300 * kMs, true, t3 );
    ASSERT_FALSE( t3.prev_known );
    ASSERT_TRUE( playback_slip::presentSetupBucket( sampleFromTail( 60.0, t3 ) ) == playback_slip::SetupBucket::Unknown );
}

TEST(GpuPresentTailWindow, SetupDuringAnOpenBlockLeavesIdleNotKnownAndDropsTheBlock)
{
    GpuPresentTailWindow w;
    w.open( 0, 4.0, 8.0 );
    w.blocked( 2 * kMs );   // no awake before the setup: this thread is running, so the block's span is not idle
    GpuPresentTailTiming t;
    w.close( 30 * kMs, true, t );
    ASSERT_TRUE( t.prev_known );
    ASSERT_FALSE( t.gui_idle_known );
    ASSERT_NEAR( 0.0, t.gui_idle_ms, 1e-12 );
    ASSERT_EQ( 0, t.gui_idle_waits );
    ASSERT_TRUE( playback_slip::presentSetupBucket( sampleFromTail( 60.0, t ) ) == playback_slip::SetupBucket::Unknown );
    ASSERT_NEAR( -1.0, playback_slip::presentSetupGuiBusyMs( sampleFromTail( 60.0, t ) ), 1e-12 );

    // The close dropped the open block: its late awake is never charged to the next window.
    w.awake( 31 * kMs );
    w.open( 40 * kMs, 4.0, 8.0 );
    w.awake( 42 * kMs );
    GpuPresentTailTiming next;
    w.close( 45 * kMs, true, next );
    ASSERT_TRUE( next.gui_idle_known );
    ASSERT_NEAR( 0.0, next.gui_idle_ms, 1e-12 );
    ASSERT_EQ( 0, next.gui_idle_waits );

    // Without the idle probe (not QEventDispatcherWin32, or swap telemetry off) idle is never known.
    w.open( 50 * kMs, 4.0, 8.0 );
    w.blocked( 51 * kMs );
    w.awake( 60 * kMs );
    GpuPresentTailTiming noProbe;
    w.close( 90 * kMs, false, noProbe );
    ASSERT_FALSE( noProbe.gui_idle_known );
    ASSERT_TRUE( playback_slip::presentSetupBucket( sampleFromTail( 60.0, noProbe ) )
                 == playback_slip::SetupBucket::Unknown );
}

TEST(GpuPresentTailWindow, SmokeRunnerSleepIsNeverCountedAsGuiBusy)
{
    // Key3 catalogue row: the measured smoke loop is processEvents(AllEvents), which never blocks (no aboutToBlock),
    // then msleep(10). A 30.6 ms window of three runner sleeps and no real GUI work.
    GpuPresentTailWindow w;
    w.open( 0, 6.0, 16.0 );
    for( int i = 0; i < 3; ++i ) w.runnerSlept( 10 * kMs + 100000 );
    GpuPresentTailTiming t;
    w.close( 30 * kMs + 600000, true, t );
    ASSERT_TRUE( t.gui_idle_known );
    ASSERT_EQ( 0, t.gui_idle_waits );
    ASSERT_EQ( 3, t.runner_sleeps );
    ASSERT_NEAR( 30.3, t.runner_sleep_ms, 1e-9 );
    const playback_slip::PresentSetupSample s = sampleFromTail( 60.0, t );
    ASSERT_NEAR( 0.3, playback_slip::presentSetupGuiBusyMs( s ), 1e-9 );
    ASSERT_TRUE( playback_slip::presentSetupBucket( s ) == playback_slip::SetupBucket::SetupAfterSwap );

    // The same 30 ms window with no yield observed at all: idle is a structural zero, so it is unmeasured, not busy.
    GpuPresentTailTiming none;
    w.open( 100 * kMs, 6.0, 16.0 );
    w.close( 130 * kMs, true, none );
    const playback_slip::PresentSetupSample n = sampleFromTail( 60.0, none );
    ASSERT_NEAR( -1.0, playback_slip::presentSetupGuiBusyMs( n ), 1e-12 );
    ASSERT_TRUE( playback_slip::presentSetupBucket( n ) == playback_slip::SetupBucket::IdleUnmeasured );
    ASSERT_EQ( std::string( "idle_unmeasured" ),
               std::string( playback_slip::setupBucketName( playback_slip::SetupBucket::IdleUnmeasured ) ) );

    // Real GUI work still buckets GUI_BUSY: 60 ms window, one 10 ms runner sleep and a 5 ms wait leave 45 ms busy.
    GpuPresentTailTiming work;
    w.open( 200 * kMs, 6.0, 16.0 );
    w.runnerSlept( 10 * kMs );
    w.blocked( 230 * kMs );
    w.awake( 235 * kMs );
    w.close( 260 * kMs, true, work );
    const playback_slip::PresentSetupSample b = sampleFromTail( 60.0, work );
    ASSERT_NEAR( 45.0, playback_slip::presentSetupGuiBusyMs( b ), 1e-9 );
    ASSERT_TRUE( playback_slip::presentSetupBucket( b ) == playback_slip::SetupBucket::GuiBusy );

    // A runner sleep timed a hair longer than the window on its own clock never reads as negative busy.
    GpuPresentTailTiming over;
    w.open( 300 * kMs, 6.0, 16.0 );
    w.runnerSlept( 10 * kMs + 1000 );
    w.close( 310 * kMs, true, over );
    ASSERT_NEAR( 0.0, playback_slip::presentSetupGuiBusyMs( sampleFromTail( 60.0, over ) ), 1e-12 );
}

TEST(GpuPresentTailWindow, RefusedPresentAbandonsTheOpenWindow)
{
    // Key3 G1: present N swaps (window opens), present N+1 is refused before its setup start (fail()), present N+2
    // sets up. N+2's window must not span the refused call: it reports no previous window.
    GpuPresentTailWindow w;
    w.open( 0, 5.0, 12.0 );
    w.runnerSlept( 10 * kMs );
    w.blocked( 12 * kMs );
    w.abandon();
    ASSERT_FALSE( w.isOpen() );
    w.awake( 20 * kMs );         // the open block went with the window
    w.runnerSlept( 10 * kMs );
    w.grabSwap();
    GpuPresentTailTiming t;
    w.close( 80 * kMs, true, t );
    ASSERT_FALSE( t.prev_known );
    ASSERT_TRUE( playback_slip::presentSetupBucket( sampleFromTail( 60.0, t ) ) == playback_slip::SetupBucket::Unknown );

    // A refusal with no window open changes nothing, and the next path-3 swap opens a clean window.
    w.abandon();
    w.open( 100 * kMs, 5.0, 12.0 );
    w.runnerSlept( 10 * kMs );
    GpuPresentTailTiming next;
    w.close( 112 * kMs, true, next );
    ASSERT_TRUE( next.prev_known );
    ASSERT_NEAR( 12.0, next.swap_return_to_setup_ms, 1e-9 );
    ASSERT_EQ( 1, next.runner_sleeps );
    ASSERT_EQ( 0, next.grab_swaps );
}

TEST(GpuPresentTailWindow, ProfilePlayHoldSleepsAreRunnerSleepsNotGuiBusy)
{
    // r4 (ADV-A1, the r3 adversary's input): a 250 ms window holding a 100 ms modal dispatcher wait and fifteen 10 ms
    // sleeps of runHeadlessPlaybackProfile's pinned Play-hold loop. Those sleeps now reach the window through
    // sleepGuiThreadForSmokeRunner (wiring: test_playback_smoke_gui_present_attribution_wiring.cpp), so nothing is busy.
    GpuPresentTailWindow w;
    w.open( 0, 6.0, 16.0 );
    w.blocked( 0 );
    w.awake( 100 * kMs );
    for( int i = 0; i < 15; ++i ) w.runnerSlept( 10 * kMs );
    GpuPresentTailTiming t;
    w.close( 250 * kMs, true, t );
    ASSERT_EQ( 1, t.gui_idle_waits );
    ASSERT_EQ( 15, t.runner_sleeps );
    const playback_slip::PresentSetupSample s = sampleFromTail( 60.0, t );
    ASSERT_NEAR( 0.0, playback_slip::presentSetupGuiBusyMs( s ), 1e-9 );
    ASSERT_FALSE( playback_slip::presentSetupBucket( s ) == playback_slip::SetupBucket::GuiBusy );
}

TEST(GpuPresentTailWindow, AnUnswappedPaintNeverPairsWithALaterWindowsSwap)
{
    // r4 (G2, CoS 6534991791): a path-1 paint in window 1 whose frameSwapped never came, then a path-3 swap opens
    // window 2 and a frameSwapped arrives there. The stale stamp is gone: window 2 reports no paint+swap pair.
    GpuPresentTailWindow w;
    w.open( 0, 5.0, 12.0 );
    w.qtPaintStarted( 4 * kMs );
    GpuPresentTailTiming t1;
    w.close( 10 * kMs, true, t1 );
    ASSERT_EQ( 0, t1.qt_paint_swaps );
    w.open( 100 * kMs, 5.0, 12.0 );
    w.qtPaintSwapped( 103 * kMs );
    GpuPresentTailTiming t2;
    w.close( 110 * kMs, true, t2 );
    ASSERT_TRUE( t2.prev_known );
    ASSERT_EQ( 0, t2.qt_paint_swaps );
    ASSERT_NEAR( 0.0, t2.qt_paint_swap_max_ms, 1e-12 );

    // Inside one window a paint and its swap still pair, once.
    w.open( 200 * kMs, 5.0, 12.0 );
    w.qtPaintStarted( 202 * kMs );
    w.qtPaintSwapped( 205 * kMs );
    w.qtPaintSwapped( 207 * kMs );
    GpuPresentTailTiming t3;
    w.close( 210 * kMs, true, t3 );
    ASSERT_EQ( 1, t3.qt_paint_swaps );
    ASSERT_NEAR( 3.0, t3.qt_paint_swap_max_ms, 1e-9 );

    // A session start or stop drops an unswapped stamp inside an open window.
    w.open( 300 * kMs, 5.0, 12.0 );
    w.qtPaintStarted( 301 * kMs );
    w.dropQtPaintStamp();
    w.qtPaintSwapped( 340 * kMs );
    GpuPresentTailTiming t4;
    w.close( 350 * kMs, true, t4 );
    ASSERT_EQ( 0, t4.qt_paint_swaps );
    ASSERT_NEAR( 0.0, t4.qt_paint_swap_max_ms, 1e-12 );
}
