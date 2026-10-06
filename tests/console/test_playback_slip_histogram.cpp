// PLAYBACK-BACHELOR-PRESENT-JITTER-1: the per-present slip histogram behind playback_smoke.slip_summary.
// Driven with an injected clock and no GUI: the timeline moves (noteTimelineMove, as playbackHandling does)
// and frames are presented (notePresent, as notePlaybackSmokePresentedFrame does), and the summary must
// count every lost frame once, at the present that skipped it, with one cause.
#include "../common/minitest.h"

#include "../../platform/qt/PlaybackNativePaceGuard.h"
#include "../../platform/qt/PlaybackSlipHistogram.h"

#include <cmath>

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