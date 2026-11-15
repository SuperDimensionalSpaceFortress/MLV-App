// CPU-S1-PRESENT-INTERVAL-BOUND-1: the GUI hop wait split (platform/qt/GuiHopWaitSplit.h). A wait window
// [a, b] in seconds is split by the closed GUI intervals inside it (runner sleep, prepared present, viewport
// paint) plus other, exclusively, so the parts sum to the window; a window older than a dropped interval of
// an overflowed ring is counted unattributed, never guessed.
#include "../common/minitest.h"
#include "../../platform/qt/GuiHopWaitSplit.h"

#include <limits>

using gui_hop_wait::Kind;

namespace
{

double ms( const gui_hop_wait::Split &s, Kind kind )
{
    return s.kindMs[static_cast<int>( kind )];
}

double closure( const gui_hop_wait::Split &s )
{
    double sum = s.otherMs;
    for( int k = 0; k < gui_hop_wait::kKinds; ++k ) sum += s.kindMs[k];
    return sum - s.waitMs;
}

} // namespace

TEST(GuiHopWaitSplit, IntervalInsideStraddlingDisjointAndAdjacent)
{
    // Window [10.000, 10.020] s = 20 ms.
    gui_hop_wait::Recorder r;
    r.push( Kind::RunnerSleep, 10.002, 10.005 );       // inside: 3 ms
    r.push( Kind::PreparedPresent, 9.990, 10.001 );    // straddles the start: 1 ms inside
    r.push( Kind::ViewportPaint, 10.018, 10.030 );     // straddles the end: 2 ms inside
    r.push( Kind::RunnerSleep, 9.000, 9.500 );         // disjoint, before
    r.push( Kind::ViewportPaint, 11.000, 11.010 );     // disjoint, after
    r.push( Kind::PreparedPresent, 10.020, 10.040 );   // adjacent to the end: 0 ms
    r.push( Kind::RunnerSleep, 9.980, 10.000 );        // adjacent to the start: 0 ms
    const gui_hop_wait::Split s = r.split( 10.000, 10.020 );
    ASSERT_TRUE( s.valid );
    ASSERT_FALSE( s.unattributed );
    ASSERT_NEAR( 20.0, s.waitMs, 1e-6 );
    ASSERT_NEAR( 3.0, ms( s, Kind::RunnerSleep ), 1e-6 );
    ASSERT_NEAR( 1.0, ms( s, Kind::PreparedPresent ), 1e-6 );
    ASSERT_NEAR( 2.0, ms( s, Kind::ViewportPaint ), 1e-6 );
    ASSERT_NEAR( 14.0, s.otherMs, 1e-6 );
    ASSERT_NEAR( 0.0, closure( s ), 1e-9 );
}

TEST(GuiHopWaitSplit, ZeroLengthWindowIsValidAndEmpty)
{
    gui_hop_wait::Recorder r;
    r.push( Kind::PreparedPresent, 4.0, 6.0 );   // covers the instant
    const gui_hop_wait::Split s = r.split( 5.0, 5.0 );
    ASSERT_TRUE( s.valid );
    ASSERT_NEAR( 0.0, s.waitMs, 1e-12 );
    ASSERT_NEAR( 0.0, ms( s, Kind::PreparedPresent ), 1e-12 );
    ASSERT_NEAR( 0.0, s.otherMs, 1e-12 );
    // A reversed or non-finite window is invalid and never accumulated.
    ASSERT_FALSE( r.split( 6.0, 5.0 ).valid );
    ASSERT_FALSE( r.split( std::numeric_limits<double>::quiet_NaN(), 5.0 ).valid );
    gui_hop_wait::Accumulator acc;
    acc.add( r.split( 6.0, 5.0 ) );
    ASSERT_EQ( 0u, static_cast<unsigned>( acc.frames ) );
    ASSERT_EQ( 0u, static_cast<unsigned>( acc.unattributedFrames ) );
}

TEST(GuiHopWaitSplit, NestedKindsAreChargedExclusivelySoThePartsClose)
{
    // A paint inside a present inside the window, and a sleep overlapping the present's tail: runner sleep
    // first, then viewport paint, then prepared present. Window [0, 0.030].
    gui_hop_wait::Recorder r;
    r.push( Kind::PreparedPresent, 0.005, 0.020 );   // 15 ms
    r.push( Kind::ViewportPaint, 0.008, 0.012 );     // 4 ms, inside the present
    r.push( Kind::RunnerSleep, 0.018, 0.025 );       // 7 ms, 2 ms of it over the present
    const gui_hop_wait::Split s = r.split( 0.0, 0.030 );
    ASSERT_NEAR( 7.0, ms( s, Kind::RunnerSleep ), 1e-6 );
    ASSERT_NEAR( 4.0, ms( s, Kind::ViewportPaint ), 1e-6 );
    ASSERT_NEAR( 9.0, ms( s, Kind::PreparedPresent ), 1e-6 );   // 15 - 4 paint - 2 sleep
    ASSERT_NEAR( 10.0, s.otherMs, 1e-6 );
    ASSERT_NEAR( 0.0, closure( s ), 1e-9 );
    // A kind outside the mask goes to other.
    const gui_hop_wait::Split m = r.split( 0.0, 0.030,
        gui_hop_wait::kindBit( Kind::RunnerSleep ) | gui_hop_wait::kindBit( Kind::ViewportPaint ) );
    ASSERT_NEAR( 0.0, ms( m, Kind::PreparedPresent ), 1e-12 );
    ASSERT_NEAR( 19.0, m.otherMs, 1e-6 );
    ASSERT_NEAR( 0.0, closure( m ), 1e-9 );
}

TEST(GuiHopWaitSplit, ClosureHoldsOnABusyRealisticClock)
{
    // Seconds since boot are large (QPC): closure must still hold to 1e-9 ms with full rings.
    gui_hop_wait::Recorder r;
    const double t0 = 86400.0 * 3.0 + 0.123456789;
    for( int i = 0; i < 40; ++i )
    {
        const double base = t0 + i * 0.0137;
        r.push( Kind::RunnerSleep, base, base + 0.0101 );
        r.push( Kind::PreparedPresent, base + 0.0103, base + 0.0131 );
        r.push( Kind::ViewportPaint, base + 0.0120, base + 0.0129 );
    }
    const double a = t0 + 38 * 0.0137 + 0.0042;
    const double b = a + 0.0133;
    const gui_hop_wait::Split s = r.split( a, b );
    ASSERT_TRUE( s.valid );
    ASSERT_FALSE( s.unattributed );
    ASSERT_NEAR( 0.0, closure( s ), 1e-9 );
    ASSERT_TRUE( s.otherMs >= 0.0 );
}

TEST(GuiHopWaitSplit, RingOverflowMakesAnOldWindowUnattributedNotGuessed)
{
    gui_hop_wait::Recorder r;
    // 17 sleeps of 1 ms at 1.000, 1.010, ... 1.160: the first (1.000-1.001) is dropped.
    for( int i = 0; i <= gui_hop_wait::kRingCapacity; ++i )
        r.push( Kind::RunnerSleep, 1.0 + i * 0.010, 1.001 + i * 0.010 );
    ASSERT_TRUE( r.ring( Kind::RunnerSleep ).overflowed() );
    ASSERT_EQ( gui_hop_wait::kRingCapacity, r.ring( Kind::RunnerSleep ).count() );
    ASSERT_NEAR( 1.010, r.ring( Kind::RunnerSleep ).oldestStartS(), 1e-12 );

    // A window from 0.999 reaches the dropped interval: unattributed, counted apart.
    const gui_hop_wait::Split old = r.split( 0.999, 1.015 );
    ASSERT_TRUE( old.valid );
    ASSERT_TRUE( old.unattributed );
    // A window inside the retained span is attributed as usual: the sleeps at 1.100 and 1.110, 1 ms each.
    const gui_hop_wait::Split recent = r.split( 1.100, 1.115 );
    ASSERT_FALSE( recent.unattributed );
    ASSERT_NEAR( 2.0, ms( recent, Kind::RunnerSleep ), 1e-6 );
    ASSERT_NEAR( 13.0, recent.otherMs, 1e-6 );

    gui_hop_wait::Accumulator acc;
    acc.add( old );
    acc.add( recent );
    ASSERT_EQ( 1u, static_cast<unsigned>( acc.frames ) );
    ASSERT_EQ( 1u, static_cast<unsigned>( acc.unattributedFrames ) );
    ASSERT_NEAR( 15.0, acc.avgWaitMs(), 1e-6 );   // the unattributed window is not in the average
    ASSERT_NEAR( 2.0, acc.avgKindMs( Kind::RunnerSleep ), 1e-6 );

    // An overflowed kind outside the mask does not make the window unattributed.
    ASSERT_FALSE( r.split( 0.999, 1.015, gui_hop_wait::kindBit( Kind::ViewportPaint ) ).unattributed );
    // A ring that never overflowed never makes a window unattributed, however old.
    gui_hop_wait::Recorder fresh;
    fresh.push( Kind::ViewportPaint, 5.0, 5.001 );
    ASSERT_FALSE( fresh.split( 1.0, 5.5 ).unattributed );
}

TEST(GuiHopWaitSplit, AdvA4HugeSamplesAreRefusedSoNoSumOverflows)
{
    // r1 advisory A4 on 6a6d7ef4: MeanMs add(1e308) twice overflowed sumMs to +inf. Samples past kMaxSampleMs are refused.
    gui_hop_wait::MeanMs mean;
    mean.add( 1e308 );
    mean.add( 1e308 );
    ASSERT_EQ( 0u, static_cast<unsigned>( mean.count ) );
    ASSERT_TRUE( std::isfinite( mean.avg() ) );
    mean.add( gui_hop_wait::kMaxSampleMs );   // the bound itself is accepted
    ASSERT_EQ( 1u, static_cast<unsigned>( mean.count ) );
    // A window whose width overflows, or is past the bound, is invalid and never accumulated.
    gui_hop_wait::Recorder r;
    ASSERT_FALSE( r.split( -1e308, 1e308 ).valid );
    ASSERT_FALSE( r.split( 0.0, gui_hop_wait::kMaxSampleMs / 1000.0 * 2.0 ).valid );
    gui_hop_wait::Accumulator acc;
    acc.add( r.split( -1e308, 1e308 ) );
    acc.add( r.split( -1e308, 1e308 ) );
    ASSERT_EQ( 0u, static_cast<unsigned>( acc.frames ) );
    ASSERT_TRUE( std::isfinite( acc.avgWaitMs() ) );
}

TEST(GuiHopWaitSplit, BadIntervalsAreIgnoredAndScopesPushOnExit)
{
    gui_hop_wait::Recorder r;
    r.push( Kind::RunnerSleep, 2.0, 1.0 );   // ends before it starts
    r.push( Kind::RunnerSleep, std::numeric_limits<double>::infinity(), 3.0 );
    r.push( static_cast<Kind>( gui_hop_wait::kKinds ), 1.0, 2.0 );   // out of range
    ASSERT_EQ( 0, r.ring( Kind::RunnerSleep ).count() );

    static double clockNow = 0.0;
    const auto clock = []() -> double { return clockNow; };
    {
        clockNow = 7.004;
        const gui_hop_wait::IntervalScope scope( &r, Kind::PreparedPresent, 7.001, clock );
    }
    ASSERT_EQ( 1, r.ring( Kind::PreparedPresent ).count() );
    ASSERT_NEAR( 7.001, r.ring( Kind::PreparedPresent ).at( 0 ).startS, 1e-12 );
    ASSERT_NEAR( 7.004, r.ring( Kind::PreparedPresent ).at( 0 ).endS, 1e-12 );
    {
        const gui_hop_wait::IntervalScope off( nullptr, Kind::PreparedPresent, 8.0, clock );   // smoke off
    }
    ASSERT_EQ( 1, r.ring( Kind::PreparedPresent ).count() );

    gui_hop_wait::MeanMs mean;
    mean.add( 12.0 );
    mean.add( 14.0 );
    mean.add( -1.0 );
    mean.add( std::numeric_limits<double>::quiet_NaN() );
    ASSERT_EQ( 2u, static_cast<unsigned>( mean.count ) );
    ASSERT_NEAR( 13.0, mean.avg(), 1e-12 );
}

TEST(GuiHopWaitSplit, AdvA1AnOpenPresentCoversAWindowThatClosesInsideIt)
{
    // r1 adversary A1 on df1b864e: present A enters at 100.000 and, before it returns, its finishPresentedFrame drains
    // the next frame's drawFrameReady (emitted 100.010, display_start 100.050). A's interval was pushed only at its
    // exit, so the window was 40 ms other; A is open over all of it: 40 ms prepared_present.
    gui_hop_wait::Recorder r;
    static double clockNow = 0.0;
    const auto clock = []() -> double { return clockNow; };
    {
        const gui_hop_wait::IntervalScope presentA( &r, Kind::PreparedPresent, 100.000, clock );
        ASSERT_EQ( 1, r.openDepth( Kind::PreparedPresent ) );
        const gui_hop_wait::Split s = r.split( 100.010, 100.050 );
        ASSERT_TRUE( s.valid );
        ASSERT_NEAR( 40.0, ms( s, Kind::PreparedPresent ), 1e-6 );
        ASSERT_NEAR( 0.0, s.otherMs, 1e-6 );
        // A nested present B [100.020, 100.030] closes inside A: A (the outermost start) still covers the window.
        {
            clockNow = 100.030;
            const gui_hop_wait::IntervalScope presentB( &r, Kind::PreparedPresent, 100.020, clock );
            ASSERT_EQ( 2, r.openDepth( Kind::PreparedPresent ) );
        }
        ASSERT_EQ( 1, r.openDepth( Kind::PreparedPresent ) );
        const gui_hop_wait::Split nested = r.split( 100.010, 100.050 );
        ASSERT_NEAR( 40.0, ms( nested, Kind::PreparedPresent ), 1e-6 );
        ASSERT_NEAR( 0.0, closure( nested ), 1e-9 );
        // A runner sleep inside the open present still outranks it.
        r.push( Kind::RunnerSleep, 100.040, 100.045 );
        const gui_hop_wait::Split slept = r.split( 100.010, 100.050 );
        ASSERT_NEAR( 5.0, ms( slept, Kind::RunnerSleep ), 1e-6 );
        ASSERT_NEAR( 35.0, ms( slept, Kind::PreparedPresent ), 1e-6 );
        clockNow = 100.060;
    }
    ASSERT_EQ( 0, r.openDepth( Kind::PreparedPresent ) );
    ASSERT_EQ( 2, r.ring( Kind::PreparedPresent ).count() );
    // Closed: A is [100.000, 100.060], so a window after it is other.
    const gui_hop_wait::Split after = r.split( 100.070, 100.080 );
    ASSERT_NEAR( 0.0, ms( after, Kind::PreparedPresent ), 1e-6 );
    ASSERT_NEAR( 10.0, after.otherMs, 1e-6 );
    // A scope whose start is not finite opens nothing, so its exit cannot close another scope's interval.
    {
        const gui_hop_wait::IntervalScope presentC( &r, Kind::PreparedPresent, 200.000, clock );
        {
            const gui_hop_wait::IntervalScope bad( &r, Kind::PreparedPresent, std::numeric_limits<double>::quiet_NaN(), clock );
        }
        ASSERT_EQ( 1, r.openDepth( Kind::PreparedPresent ) );
        clockNow = 200.010;
    }
    ASSERT_EQ( 0, r.openDepth( Kind::PreparedPresent ) );
}

TEST(GuiHopWaitSplit, AdvA4AOneUlpIntervalIsStillCharged)
{
    // r1 advisory A4 on df1b864e: the piece [1.0, nextafter(1.0)]'s midpoint rounds onto 1.0 and the strict
    // start < mid test missed it, so a covered piece went to other. Coverage now compares the piece's ends.
    gui_hop_wait::Recorder r;
    const double hi = std::nextafter( 1.0, 2.0 );
    r.push( Kind::RunnerSleep, 1.0, hi );
    const gui_hop_wait::Split s = r.split( 1.0, hi );
    ASSERT_TRUE( s.valid );
    ASSERT_TRUE( ms( s, Kind::RunnerSleep ) > 0.0 );
    ASSERT_EQ( 0.0, s.otherMs );
}
