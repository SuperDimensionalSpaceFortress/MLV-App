#include "../common/minitest.h"

#include "../../platform/qt/PlaybackFpsMeterPolicy.h"
#include "../../platform/qt/PlaybackFrameRange.h"
#include "../../platform/qt/PlaybackSourceAdvanceTracker.h"

#include <QElapsedTimer>
#include <QTime>

#include <climits>
#include <cmath>

using playback_fps_meter::fpsMeterShouldReset;
using playback_fps_meter::fpsMeterStalled;
using playback_fps_meter::kFpsMeterStallMs;
using playback_fps_meter::smoothedFrameMs;
using playback_fps_meter::stampAgeMs;

// ---- fps meter: the interval is draw-to-draw, so an 8 ms poll cannot masquerade as the rate ----

TEST(PlaybackFpsMeterPolicy, ReadsTheDrawIntervalNotThePollPeriod)
{
    // A 24 fps timeline draws every ~42 ms while the timer polls every 8 ms. Fed the draw
    // interval the meter converges on 24 fps; fed the poll-tick interval (what the old idle-tick
    // re-arm measured, ~11 ms) it read ~90 fps.
    double ema = 0.0;
    for (int i = 0; i < 200; ++i) ema = smoothedFrameMs(ema, 42);
    ASSERT_TRUE(std::fabs(1000.0 / ema - 1000.0 / 42.0) < 0.01);
    ASSERT_TRUE(1000.0 / ema > 23.5 && 1000.0 / ema < 24.5);

    double pollEma = 0.0;
    for (int i = 0; i < 200; ++i) pollEma = smoothedFrameMs(pollEma, 11);
    ASSERT_TRUE(1000.0 / pollEma > 85.0); // the bogus reading the wiring test forbids
}

TEST(PlaybackFpsMeterPolicy, SeedsFromTheFirstSampleAndSmoothsAfter)
{
    ASSERT_EQ(42.0, smoothedFrameMs(0.0, 42));
    const double next = smoothedFrameMs(42.0, 52);
    ASSERT_TRUE(std::fabs(next - 43.0) < 1e-9); // 0.9 * 42 + 0.1 * 52
}

TEST(PlaybackFpsMeterPolicy, IgnoresANonPositiveIntervalAndRestartsAfterAStall)
{
    ASSERT_EQ(42.0, smoothedFrameMs(42.0, 0));
    ASSERT_EQ(42.0, smoothedFrameMs(42.0, -3)); // clock wrap across midnight
    // A long gap is a restart, not a sample to smooth into the old average.
    ASSERT_EQ(900.0, smoothedFrameMs(42.0, 900));
    ASSERT_FALSE(fpsMeterStalled(kFpsMeterStallMs));
    ASSERT_TRUE(fpsMeterStalled(kFpsMeterStallMs + 1));
    ASSERT_FALSE(fpsMeterStalled(42)); // one 24 fps frame period is nowhere near a stall
    ASSERT_FALSE(fpsMeterStalled(0));
}

TEST(PlaybackFpsMeterPolicy, ResetsWhenPausedBeforeAnyDrawOrStalledButNotBetweenPlayingDraws)
{
    // playing, a draw 42 ms ago (one 24 fps frame, the normal idle poll tick): keep the reading
    ASSERT_FALSE(fpsMeterShouldReset(true, true, 42));
    ASSERT_FALSE(fpsMeterShouldReset(true, true, kFpsMeterStallMs));
    // paused resets even with a fresh draw: the label read 24 and the user paused
    ASSERT_TRUE(fpsMeterShouldReset(false, true, 0));
    ASSERT_TRUE(fpsMeterShouldReset(false, true, 42));
    // no draw yet (fresh clip): reset
    ASSERT_TRUE(fpsMeterShouldReset(true, false, 0));
    // the sol r1 repro: label at 24 fps, a render stays busy 900 ms -> stale, must reset
    ASSERT_TRUE(fpsMeterShouldReset(true, true, 900));
    ASSERT_TRUE(fpsMeterShouldReset(true, true, kFpsMeterStallMs + 1));
}

// ---- PLAYBACK-FPS-METER-MONOTONIC-CLOCK-1: CHARACTERIZATION of the wall clock the meter used to read ----
// These rows pin what a time-of-day stamp does at the three gates (stall check, EMA sample, 250 ms status
// throttle). They pass on both trees: they document the defect, the wiring row forbids the clock.

TEST(PlaybackFpsMeterPolicy, CharacterizationWallClockAcrossMidnightFreezesAllThreeGates)
{
    const int age = QTime(23, 59, 59, 990).msecsTo(QTime(0, 0, 0, 20)); // 30 ms of real time
    ASSERT_EQ(-86399970, age);
    ASSERT_FALSE(fpsMeterShouldReset(true, true, age)); // a stall is never seen
    ASSERT_EQ(42.0, smoothedFrameMs(42.0, age));        // the sample is dropped
    ASSERT_FALSE(age >= 250);                           // the status text never refreshes
}

TEST(PlaybackFpsMeterPolicy, CharacterizationWallClockAcrossADstEndFreezesAllThreeGates)
{
    const int age = QTime(2, 0, 0, 10).msecsTo(QTime(1, 0, 0, 50)); // 40 ms of real time, clock set back 1 h
    ASSERT_EQ(-3599960, age);
    ASSERT_FALSE(fpsMeterShouldReset(true, true, age));
    ASSERT_EQ(42.0, smoothedFrameMs(42.0, age));
    ASSERT_FALSE(age >= 250);
}

// ---- PLAYBACK-FPS-METER-MONOTONIC-CLOCK-1: stampAgeMs, the one age every timerFrameEvent gate reads ----

namespace
{
struct FakeStamp // a monotonic stamp with injected milliseconds
{
    bool valid;
    long long ms;
    bool isValid() const { return valid; }
    long long msecsTo( const FakeStamp & other ) const { return other.ms - ms; }
    long long msecsSinceReference() const { return ms; }
};

// QElapsedTimer's own contract at nanosecond grain: msecsTo truncates the DIFFERENCE, msecsSinceReference
// truncates the stamp itself (both toward zero; the stamps here are positive).
struct FakeNsStamp
{
    long long ns;
    bool isValid() const { return true; }
    long long msecsTo( const FakeNsStamp & other ) const { return ( other.ns - ns ) / 1000000; }
    long long msecsSinceReference() const { return ns / 1000000; }
};

// timerFrameEvent's drop-frame clock: each tick reads timeDiff = age( lastTime, tick ), and an idle tick
// re-arms lastTime a little AFTER the tick (the work between them). Returns the summed timeDiff over n polls.
long long summedIdleTickTimeDiffMs( long long pollNs, long long tickOffsetNs, long long reArmOffsetNs, int polls )
{
    long long sum = 0;
    FakeNsStamp lastTime{ reArmOffsetNs };
    for( int i = 1; i <= polls; ++i )
    {
        const FakeNsStamp tick{ i * pollNs + tickOffsetNs };
        sum += stampAgeMs( lastTime, tick );
        lastTime = FakeNsStamp{ i * pollNs + reArmOffsetNs };
    }
    return sum;
}
} // namespace

TEST(PlaybackFpsMeterPolicy, StampAgeIsTheMonotonicIntervalZeroWhenUnstartedAndSaturated)
{
    ASSERT_EQ(42, stampAgeMs(FakeStamp{ true, 10 }, FakeStamp{ true, 52 }));
    ASSERT_EQ(0, stampAgeMs(FakeStamp{ false, 10 }, FakeStamp{ true, 52 }));  // never-started then
    ASSERT_EQ(0, stampAgeMs(FakeStamp{ true, 10 }, FakeStamp{ false, 52 }));  // never-started now
    // adversary r1 A3 (c7c88bf8): saturated at INT_MAX, a 24.8-day suspension asked the drop-frame loop for
    // 4294967294 frames at 2000 fps. The age saturates at one day, the most the wall-clock type ever returned.
    ASSERT_EQ(86400000, stampAgeMs(FakeStamp{ true, 0 }, FakeStamp{ true, 3000000000LL }));
    ASSERT_EQ(-86400000, stampAgeMs(FakeStamp{ true, 0 }, FakeStamp{ true, -3000000000LL }));
    ASSERT_EQ(86400000, stampAgeMs(FakeStamp{ true, 0 }, FakeStamp{ true, 2147484647LL }));
    ASSERT_EQ(86400000, stampAgeMs(FakeStamp{ true, 0 }, FakeStamp{ true, 86400001LL }));
    ASSERT_EQ(86399999, stampAgeMs(FakeStamp{ true, 0 }, FakeStamp{ true, 86399999LL }));
    ASSERT_TRUE(2000.0 * stampAgeMs(FakeStamp{ true, 0 }, FakeStamp{ true, 2147484647LL }) / 1000.0 < 2147483647.0);
    // a monotonic pair either side of midnight is just 30 ms: no stall, a real sample, no wrap
    const int age = stampAgeMs(FakeStamp{ true, 86399990LL }, FakeStamp{ true, 86400020LL });
    ASSERT_EQ(30, age);
    ASSERT_FALSE(fpsMeterShouldReset(true, true, age));
}

TEST(PlaybackFpsMeterPolicy, StampAgeOnARealElapsedTimer)
{
    QElapsedTimer never;
    ASSERT_FALSE(never.isValid());
    ASSERT_EQ(0, stampAgeMs(never, never));
    QElapsedTimer a;
    a.start();
    const QElapsedTimer b = a;
    ASSERT_EQ(0, stampAgeMs(a, b));
    ASSERT_EQ(0, stampAgeMs(never, a));
    a.invalidate();
    ASSERT_EQ(0, stampAgeMs(a, b));
}

// adversary r1 A2 (c7c88bf8): truncating the DIFFERENCE (msecsTo) loses up to 1 ms on every tick, and the idle
// re-arm makes that every tick. The wall clock this replaced truncated each STAMP, so the summed timeDiff kept
// up with real time. The age must keep that grain.
TEST(PlaybackFpsMeterPolicy, StampAgeKeepsTheStampGrainSoIdleReArmedTicksKeepUpWithRealTime)
{
    // MLVAPP_PLAYBACK_TIMER_POLL_MS=1, due wake off: ticks at n+0.100 ms, idle re-arms at n+0.200 ms.
    // Each age is 0.9 ms of real time; msecsTo read 0 every tick and drop-frame never advanced.
    ASSERT_EQ(42LL, summedIdleTickTimeDiffMs(1000000LL, 100000LL, 200000LL, 42));
    // the 8 ms default poll: each post-idle interval is 7.9 ms; msecsTo read 7 (playback ran 1/8 slow)
    ASSERT_EQ(8LL * 42LL, summedIdleTickTimeDiffMs(8000000LL, 100000LL, 200000LL, 42));
    // one age is still whole stamps: 0.2 ms -> 1.1 ms crosses one millisecond boundary
    ASSERT_EQ(1, stampAgeMs(FakeNsStamp{ 200000LL }, FakeNsStamp{ 1100000LL }));
}

// adversary r2 A1 (894b938d): the one-day cap alone still let a 25000 fps source ask for 2.16e9 frames after a long
// suspension; advanceDropFrameTick caps its Loop laps at INT_MAX - 1, so the position left the cut (12516353 for cut 1..2).
// One tick's request is bounded in frames, for any source rate.
TEST(PlaybackFpsMeterPolicy, OneTickNeverAsksForMoreFramesThanTheLoopCanCount)
{
    using playback_fps_meter::frameBoundedTickTimeDiffMs;
    using playback_fps_meter::kMaxStampAgeMs;
    using playback_fps_meter::kMaxTickFrames;
    // r2 CHARACTERIZATION of the defect was: the unbounded request leaves the cut (INT_MAX - 1 laps). r4 (pass 5 A1 on
    // b70f718d): advanceDropFrameTick bounds its own laps, so even the unbounded request lands inside [0, 1) now
    const playback_frame_range::DropFrameTickResult unbounded =
        playback_frame_range::advanceDropFrameTick(0.0, 25000.0 * kMaxStampAgeMs / 1000.0, 1, 2, true);
    ASSERT_TRUE(unbounded.position >= 0.0 && unbounded.position < 1.0);
    ASSERT_TRUE(unbounded.wrapLaps <= 1000000);
    // bounded: the same suspension lands inside the wrap range [wrapAt - width, wrapAt) = [0, 1)
    const int bounded = frameBoundedTickTimeDiffMs(kMaxStampAgeMs, 25000.0);
    ASSERT_EQ(39999, bounded); // r3: 999990 frames at 25000 fps (the 1e9 bound was 40000000 ms)
    ASSERT_TRUE(25000.0 * bounded / 1000.0 <= kMaxTickFrames);
    const playback_frame_range::DropFrameTickResult tick =
        playback_frame_range::advanceDropFrameTick(0.0, 25000.0 * bounded / 1000.0, 1, 2, true);
    ASSERT_TRUE(tick.wrapped);
    ASSERT_TRUE(tick.position >= 0.0 && tick.position < 1.0);
    // the largest MLV rate (uint32 numerator over 1) is bounded too; r3 A3: past 1e9 fps even 1 ms is over the bound
    const int huge = frameBoundedTickTimeDiffMs(kMaxStampAgeMs, 4294967295.0);
    ASSERT_TRUE(huge >= 0 && huge < 1);
    ASSERT_TRUE(4294967295.0 * huge / 1000.0 <= kMaxTickFrames);
    ASSERT_TRUE(1.0e13 * frameBoundedTickTimeDiffMs(kMaxStampAgeMs, 1.0e13) / 1000.0 <= kMaxTickFrames);
    ASSERT_EQ(-39999, frameBoundedTickTimeDiffMs(-kMaxStampAgeMs, 25000.0));
    // every ordinary tick is unchanged
    ASSERT_EQ(8, frameBoundedTickTimeDiffMs(8, 1000000.0));
    ASSERT_EQ(42, frameBoundedTickTimeDiffMs(42, 24.0));
    ASSERT_EQ(3600000, frameBoundedTickTimeDiffMs(3600000, 24.0)); // an hour at 24 fps is 86400 frames
    // r3: a whole day at 24 fps is 2073600 frames, so it is cut to 999990 frames (41,666,250 ms, about 11.6 h)
    ASSERT_EQ(41666250, frameBoundedTickTimeDiffMs(kMaxStampAgeMs, 24.0));
    ASSERT_EQ(1, frameBoundedTickTimeDiffMs(1, 25000.0));
    ASSERT_EQ(7, frameBoundedTickTimeDiffMs(7, 0.0)); // no rate: nothing to bound
}

// PLAYBACK-FPS-METER-MONOTONIC-CLOCK-1 r3 (adversary pass 4 on 3bb01140).
// A1: a 1e9-frame bound still let a width-1 Loop cut take about 1e9 laps in one tick, and the wrap recorder
// (PlaybackWrapRecorder::noteEngineWrap) loops and appends once per lap on the GUI thread. The LAP WORK is bounded.
// A2: the reciprocal rounds, so the bounded age must be re-checked with the call site's own expression
// (MainWindow::playbackHandling: framerate * (double)timeDiff / 1000.0).
// A3: past the rate where even 1 ms exceeds the cap, the tick asks for nothing rather than breach it.
TEST(PlaybackFpsMeterPolicy, OneTickBoundsItsLoopLapWorkAndItsRoundedRequest)
{
    using playback_fps_meter::frameBoundedTickTimeDiffMs;
    using playback_fps_meter::kMaxStampAgeMs;
    using playback_fps_meter::kMaxTickFrames;
    // A1: a one-day suspension at a 25000 fps source on Loop cut 1..2 (width 1)
    const playback_frame_range::DropFrameTickResult lapTick = playback_frame_range::advanceDropFrameTick(
        0.0, 25000.0 * frameBoundedTickTimeDiffMs(86400000, 25000.0) / 1000.0, 1, 2, true);
    ASSERT_TRUE(lapTick.wrapped);
    ASSERT_TRUE(lapTick.wrapLaps <= 1000000);
    ASSERT_TRUE(lapTick.position >= 0.0 && lapTick.position < 1.0);
    // the pre-existing class: a wall-clock age just after local midnight (base QTime, about +86,398,000 ms) is the same loop
    const playback_frame_range::DropFrameTickResult midnightTick = playback_frame_range::advanceDropFrameTick(
        0.0, 25000.0 * frameBoundedTickTimeDiffMs(86398000, 25000.0) / 1000.0, 1, 2, true);
    ASSERT_TRUE(midnightTick.wrapLaps <= 1000000);
    // ...and the wrap recorder's per-lap work for that tick is bounded with it (reached only once the laps are bounded:
    // a failed ASSERT throws, so the RED tree never appends a billion laps here)
    playback_frame_range::PlaybackWrapRecorder recorder;
    recorder.noteEngineWrap(lapTick.wrapWidth, lapTick.wrapLaps);
    ASSERT_TRUE(recorder.engineWrapWidths.size() <= 1000000u);
}

// r3 pass A1 on 8743841f: a position already inside the cut adds a lap, and the double sum rounds up one more. 100 fps,
// Loop cut 1..2, ten 0.1-frame grants leave 0.9999999999999999, then a 10,000,010 ms suspension: a 1e6-frame request made
// 1,000,001 laps. The frame bound leaves room for that slack and for the conform debt (kConformMaxDebtFrames).
TEST(PlaybackFpsMeterPolicy, OneTickLapsStayWithinTheBoundFromAnywhereInTheCut)
{
    using playback_fps_meter::frameBoundedTickTimeDiffMs;
    double position = 0.0;
    for (int i = 0; i < 10; ++i) position += 0.1;
    ASSERT_TRUE(position > 0.0 && position < 1.0);
    const double request = 100.0 * (double)frameBoundedTickTimeDiffMs(10000010, 100.0) / 1000.0;
    const playback_frame_range::DropFrameTickResult tick =
        playback_frame_range::advanceDropFrameTick(position, request, 1, 2, true);
    ASSERT_TRUE(tick.wrapped);
    ASSERT_TRUE(tick.wrapLaps <= 1000000);
    ASSERT_TRUE(tick.position >= 0.0 && tick.position < 1.0);
    // ...with the most conform debt a tick can carry on top of its grant (conformDropFrameDelta)
    const playback_frame_range::DropFrameTickResult debtTick = playback_frame_range::advanceDropFrameTick(
        position, request + playback_frame_range::kConformMaxDebtFrames, 1, 2, true);
    ASSERT_TRUE(debtTick.wrapLaps <= 1000000);
}

// r3 pass A2 on 8743841f, CHARACTERIZATION of the stated rate range: an ordinary 8 ms tick is unchanged up to
// 124,998,750 fps. Past it (the loader accepts MLVI's quotient unchecked: 150,000,000 fps) the tick is cut to the
// frame bound, and the Loop still lands inside the cut.
TEST(PlaybackFpsMeterPolicy, OrdinaryTicksAreUnchangedUpToTheStatedRateAndCutInsideTheLoopPastIt)
{
    using playback_fps_meter::frameBoundedTickTimeDiffMs;
    using playback_fps_meter::kMaxTickFrames;
    ASSERT_EQ(8, frameBoundedTickTimeDiffMs(8, 124998750.0));
    ASSERT_EQ(8, frameBoundedTickTimeDiffMs(8, 120000000.0));
    const int cut = frameBoundedTickTimeDiffMs(8, 150000000.0);
    ASSERT_EQ(6, cut);
    ASSERT_TRUE(150000000.0 * (double)cut / 1000.0 <= kMaxTickFrames);
    const playback_frame_range::DropFrameTickResult tick =
        playback_frame_range::advanceDropFrameTick(0.0, 150000000.0 * (double)cut / 1000.0, 1, 120, true);
    ASSERT_TRUE(tick.wrapped);
    ASSERT_TRUE(tick.position >= 0.0 && tick.position < 119.0);
    ASSERT_TRUE(tick.wrapLaps <= 1000000);
}

TEST(PlaybackFpsMeterPolicy, OneTickBoundIsRecheckedWithTheCallSitesOwnRoundedExpression)
{
    using playback_fps_meter::frameBoundedTickTimeDiffMs;
    using playback_fps_meter::kMaxStampAgeMs;
    using playback_fps_meter::kMaxTickFrames;
    // A2: 781250000/821 fps, age 1,050,881 ms: the reciprocal rounded to 1,050,880 ms, 1e9 + 1 ulp frames
    const double rate = 781250000.0 / 821.0;
    const int up = frameBoundedTickTimeDiffMs(1050881, rate);
    const int down = frameBoundedTickTimeDiffMs(-1050881, rate);
    ASSERT_TRUE(rate * (double)up / 1000.0 <= kMaxTickFrames);
    ASSERT_TRUE(rate * (double)down / 1000.0 >= -kMaxTickFrames);
    ASSERT_TRUE(rate * (double)up / 1000.0 <= 1000000.0);
    ASSERT_EQ(-up, down);
    // the same rounding at the bound: 96250/539 fps (178.57) rounded UP at a 1e6 bound, 25000/35 fps (714.29) at 999990
    const double plausible = 96250.0 / 539.0;
    const int p = frameBoundedTickTimeDiffMs(kMaxStampAgeMs, plausible);
    ASSERT_TRUE(plausible * (double)p / 1000.0 <= kMaxTickFrames);
    ASSERT_TRUE(plausible * (double)( p + 1 ) / 1000.0 > kMaxTickFrames); // and it is the largest such age
    ASSERT_EQ(-p, frameBoundedTickTimeDiffMs(-kMaxStampAgeMs, plausible));
    const double rounding = 25000.0 / 35.0;
    const int q = frameBoundedTickTimeDiffMs(kMaxStampAgeMs, rounding);
    ASSERT_EQ(1399985, q); // the reciprocal gives 1399986 ms, 999990.0000000001 frames
    ASSERT_TRUE(rounding * (double)q / 1000.0 <= kMaxTickFrames);
    ASSERT_TRUE(rounding * (double)( q + 1 ) / 1000.0 > kMaxTickFrames);
    ASSERT_EQ(-q, frameBoundedTickTimeDiffMs(-kMaxStampAgeMs, rounding));
}

TEST(PlaybackFpsMeterPolicy, OneTickAsksForNothingWhenEvenOneMillisecondIsPastTheBound)
{
    using playback_fps_meter::frameBoundedTickTimeDiffMs;
    using playback_fps_meter::kMaxStampAgeMs;
    // A3: even 1 ms is past the cap here: ask for nothing, either way
    ASSERT_EQ(0, frameBoundedTickTimeDiffMs(kMaxStampAgeMs, 1.0e13));
    ASSERT_EQ(0, frameBoundedTickTimeDiffMs(-kMaxStampAgeMs, 1.0e13));
    ASSERT_TRUE(4294967295.0 * (double)frameBoundedTickTimeDiffMs(kMaxStampAgeMs, 4294967295.0) / 1000.0 <= 1000000.0);
}

// r4 (pass 5 A2 on b70f718d), CHARACTERIZATION of the exact cutoff the contract states: a positive tick keeps at least
// 1 ms up to kMaxTickFrames * 1000 = 999,990,000 fps and asks for nothing past it (the contract used to say 1e9).
TEST(PlaybackFpsMeterPolicy, APositiveTickKeepsOneMillisecondUpToTheStatedCutoffRate)
{
    using playback_fps_meter::frameBoundedTickTimeDiffMs;
    using playback_fps_meter::kMaxTickFrames;
    ASSERT_TRUE(kMaxTickFrames * 1000.0 == 999990000.0);
    ASSERT_TRUE(frameBoundedTickTimeDiffMs(8, 999990000.0) == 1);
    ASSERT_TRUE(frameBoundedTickTimeDiffMs(-8, 999990000.0) == -1);
    ASSERT_TRUE(frameBoundedTickTimeDiffMs(8, 999990001.0) == 0);
    ASSERT_TRUE(frameBoundedTickTimeDiffMs(8, 0.0) == 8); // Normal mode passes no rate: its tick is never cut
}

// r4 iteration 2 (pass 7 A1 on 7a0f5a1c): the reciprocal can round DOWN, so the cut kept 63755 ms where the call site's
// own expression accepts 63756 ms (exactly 999,990 frames at 27777500/1771 fps). The cut keeps the LARGEST accepted age.
TEST(PlaybackFpsMeterPolicy, TheBoundKeepsTheLargestAgeTheCallSiteAccepts)
{
    using playback_fps_meter::frameBoundedTickTimeDiffMs;
    using playback_fps_meter::kMaxTickFrames;
    const double rate = 27777500.0 / 1771.0;
    ASSERT_TRUE(rate * 63756.0 / 1000.0 <= kMaxTickFrames);
    ASSERT_TRUE(rate * 63757.0 / 1000.0 > kMaxTickFrames);
    ASSERT_EQ(63756, frameBoundedTickTimeDiffMs(63757, rate));
    ASSERT_EQ(-63756, frameBoundedTickTimeDiffMs(-63757, rate));
    ASSERT_EQ(63756, frameBoundedTickTimeDiffMs(playback_fps_meter::kMaxStampAgeMs, rate));
}

// r4 iteration 2 (pass 7 A3 on 7a0f5a1c), CHARACTERIZATION of the telescoping rule the idle branch now follows: when
// lastTime takes the tick's OWN stamp (as the draw branch always did), the summed whole-stamp ages are the elapsed time
// whatever the poll phase: 1 ms polls at n + 0.9 ms sum to 42 ms over 42 polls (a fresh re-arm at n + 1.1 ms read 0).
TEST(PlaybackFpsMeterPolicy, TickStampReArmTelescopesSoIdlePollsKeepUpWithRealTime)
{
    long long sum = 0;
    FakeNsStamp last{ 900000LL };
    for (int i = 1; i <= 42; ++i)
    {
        const FakeNsStamp tick{ i * 1000000LL + 900000LL };
        sum += stampAgeMs(last, tick);
        last = tick;
    }
    ASSERT_EQ(42LL, sum);
    ASSERT_TRUE(summedIdleTickTimeDiffMs(1000000LL, 900000LL, 1100000LL, 42) < 42LL); // the fresh re-arm the base and r3 used lost it
}

// r4 (pass 5 A1 on b70f718d): the drop-frame position can be stale and outside the cut (a seek while Drop Frame was on,
// Drop Frame off, a seek and a new cut, Drop Frame on: the normalizer clamps it to the clip, not the cut). From position
// 20 on Loop cut 1..2 a 999,990-frame tick made 1,000,010 width-1 laps. advanceDropFrameTick bounds its own laps from
// any start; an ordinary tick from a stale start keeps the base arithmetic exactly.
TEST(PlaybackFpsMeterPolicy, OneTickLapsStayWithinTheBoundFromAStaleStartOutsideTheCut)
{
    const playback_frame_range::DropFrameTickResult stale =
        playback_frame_range::advanceDropFrameTick(20.0, 999990.0, 1, 2, true);
    ASSERT_TRUE(stale.wrapped);
    ASSERT_TRUE(stale.wrapLaps <= 1000000);
    ASSERT_TRUE(stale.position >= 0.0 && stale.position < 1.0);
    // r4 iteration 2 (pass 7 A2 on 7a0f5a1c): only the DELTA is cut, so the tick's advance is reconstructed exactly
    // (position - before + wrapWidth * wrapLaps is what the slip histogram and the paint tracker read) and never negative
    const double staleGranted = stale.position - 20.0 + (double)stale.wrapWidth * stale.wrapLaps;
    ASSERT_TRUE(staleGranted >= 0.0 && staleGranted <= 999990.0);
    // r6 (sol r5 S2 on 6aaac7f5): a stale start already at or past the lap limit no longer keeps the base arithmetic
    // (1000001 + 2.4 wrapped 1000003 laps). r6 iteration 2 (pass 11 A1 on 4c41c80b): it is folded into the cut onto its own
    // phase (the stale laps are moved, never recorded: DropFrameTickResult::foldedFrames), and only then does the delta
    // advance: 1000001 + 2.4 starts from phase 0 and makes the 2 laps the delta itself crosses, to 0.4, granting the whole
    // 2.4 (AFoldedStartKeepsItsDeltaAndTheAppliedAdvanceAccounting checks the accounting). 4c41c80b made 3 laps.
    const playback_frame_range::DropFrameTickResult farStale =
        playback_frame_range::advanceDropFrameTick(1000001.0, 2.4, 1, 2, true);
    ASSERT_EQ(2, farStale.wrapLaps);
    ASSERT_TRUE(farStale.wrapped);
    ASSERT_TRUE(std::fabs(farStale.position - 0.4) < 1e-6);
    ASSERT_TRUE(farStale.position >= 0.0 && farStale.position < 1.0);
    // a stale start far past the cut (a long clip) with an ordinary request: 6aaac7f5 made 3,000,000 laps (the base's),
    // 4c41c80b one; 0 + 0.25 crosses no wrap point, so it records none (the tick still reads wrapped: it moved back)
    const playback_frame_range::DropFrameTickResult far =
        playback_frame_range::advanceDropFrameTick(3000000.0, 0.25, 1, 2, true);
    ASSERT_EQ(0, far.wrapLaps);
    ASSERT_TRUE(far.wrapped);
    ASSERT_TRUE(std::fabs(far.position - 0.25) < 1e-6);
    // conform (presentLastFrame: wrap past the last frame by the cut's frame count) with the most debt a tick carries
    const playback_frame_range::DropFrameTickResult conformed = playback_frame_range::advanceDropFrameTick(
        20.0, 999990.0 + playback_frame_range::kConformMaxDebtFrames, 1, 2, true, true);
    ASSERT_TRUE(conformed.wrapLaps <= 1000000);
    ASSERT_TRUE(conformed.position >= 0.0 && conformed.position < 2.0);
    // an unbounded request (an infinite delta) still lands inside the cut
    const playback_frame_range::DropFrameTickResult endless =
        playback_frame_range::advanceDropFrameTick(0.0, HUGE_VAL, 1, 2, true);
    ASSERT_TRUE(endless.wrapLaps <= 1000000);
    ASSERT_TRUE(endless.position >= 0.0 && endless.position < 1.0);
    // r9 (sol r8 on 4eb1cfd5): an ordinary tick from a stale start lands where it did (0.1) but records no lap: the 20
    // laps 4eb1cfd5 counted were the stale distance's, a correction (foldedFrames 20), never travel
    const playback_frame_range::DropFrameTickResult ordinary =
        playback_frame_range::advanceDropFrameTick(20.0, 0.1, 1, 2, true);
    ASSERT_EQ(0, ordinary.wrapLaps);
    ASSERT_TRUE(ordinary.foldedFrames == 20.0);
    ASSERT_TRUE(std::fabs(ordinary.position - 0.1) < 1e-9);
    // ...and so is a tick inside the cut that wraps exactly 1,000,000 laps (the bound itself is admitted)
    const playback_frame_range::DropFrameTickResult atBound =
        playback_frame_range::advanceDropFrameTick(0.5, 1000000.0, 1, 2, true);
    ASSERT_EQ(1000000, atBound.wrapLaps);
    ASSERT_TRUE(std::fabs(atBound.position - 0.5) < 1e-6);
    // r5 (pass 8 A1 on c37d6fd5): a clipped tick stops at the LAST position whose laps are still 1e6, not the first
    // position of that lap, so it keeps all the room the bound allows. r9 (sol r8): a stale start is folded into the cut
    // first, so only an in-range start with a delta past the bound reaches the clip: conform, cut 1..2 (wrapAt 2, width 2,
    // lapLimit 2000002), 1.5 + 2000001 is cut to just below 2000002, 1e6 laps.
    const playback_frame_range::DropFrameTickResult clippedConform =
        playback_frame_range::advanceDropFrameTick(1.5, 2000001.0, 1, 2, true, true);
    ASSERT_EQ(1000000, clippedConform.wrapLaps);
    ASSERT_TRUE(clippedConform.position > 1.9999 && clippedConform.position < 2.0);
    const double clippedConformGranted =
        clippedConform.position - 1.5 + (double)clippedConform.wrapWidth * clippedConform.wrapLaps;
    ASSERT_TRUE(clippedConformGranted > 2000000.4999 && clippedConformGranted < 2000000.5);
    // fable r8 DROP-FRAME-APPLIED-FRAMES-LAP-BOUND-CLIP-ROW-1: appliedFrames is the delta the bound KEPT, not the request
    ASSERT_TRUE(clippedConform.appliedFrames < 2000000.5 && clippedConform.appliedFrames > 2000000.4999);
    ASSERT_TRUE(std::fabs(clippedConform.appliedFrames - clippedConformGranted) < 1e-6);
    // ...and without conform (cut 1..2: wrapAt 1, width 1, lapLimit 1000001), 0.5 + 1000001 keeps almost 1000000.5 frames
    const playback_frame_range::DropFrameTickResult clippedPlain =
        playback_frame_range::advanceDropFrameTick(0.5, 1000001.0, 1, 2, true);
    ASSERT_EQ(1000000, clippedPlain.wrapLaps);
    ASSERT_TRUE(clippedPlain.position >= 0.0 && clippedPlain.position < 1.0);
    const double clippedPlainGranted =
        clippedPlain.position - 0.5 + (double)clippedPlain.wrapWidth * clippedPlain.wrapLaps;
    ASSERT_TRUE(clippedPlainGranted > 1000000.4999 && clippedPlainGranted < 1000000.5);
    ASSERT_TRUE(clippedPlain.appliedFrames < 1000000.5 && clippedPlain.appliedFrames > 1000000.4999);   // the request was 1000001
    ASSERT_TRUE(std::fabs(clippedPlain.appliedFrames - clippedPlainGranted) < 1e-6);
}

// r6 (sol r5 S2 on 6aaac7f5, fable r5 and key3 r5 hardening PLAYBACK-FRAME-RANGE-LAP-BOUND-CLAIM-WORDING-1): the comments
// promised at most 1e6 laps from ANY start, but a start already at or past lapLimit kept the base arithmetic, so its laps
// (and PlaybackWrapRecorder::noteEngineWrap's appends, one per lap) were the stale distance's own. Sol's rows, plain cut
// 1..2 (wrapAt 1, width 1, lapLimit 1000001): 1100000 + 1 made 1,100,001 laps and 1100000 + 999990 made 2,099,990.
TEST(PlaybackFpsMeterPolicy, OneTickLapsStayWithinTheBoundFromAStartAtOrPastTheLapLimit)
{
    using playback_frame_range::advanceDropFrameTick;
    using playback_frame_range::DropFrameTickResult;
    const DropFrameTickResult stale = advanceDropFrameTick(1100000.0, 1.0, 1, 2, true, false);
    ASSERT_TRUE(stale.wrapped);
    ASSERT_TRUE(stale.wrapLaps <= 1000000);
    ASSERT_EQ(1, stale.wrapLaps); // phase 0 + 1 crosses wrapAt once (4c41c80b, folded onto [wrapAt, wrapAt + 1): 2)
    ASSERT_TRUE(stale.position >= 0.0 && stale.position < 1.0);
    const DropFrameTickResult bounded = advanceDropFrameTick(1100000.0, 999990.0, 1, 2, true, false);
    ASSERT_TRUE(bounded.wrapped);
    ASSERT_TRUE(bounded.wrapLaps <= 1000000);
    ASSERT_TRUE(bounded.position >= 0.0 && bounded.position < 1.0);
    // a start exactly AT the limit, conform (cut 1..2: wrapAt 2, width 2, lapLimit 2000002): 6aaac7f5 made 1,000,001 laps
    const DropFrameTickResult atLimitConform = advanceDropFrameTick(2000002.0, 0.5, 1, 2, true, true);
    ASSERT_TRUE(atLimitConform.wrapped);
    ASSERT_TRUE(atLimitConform.wrapLaps <= 1000000);
    ASSERT_TRUE(atLimitConform.position >= 0.0 && atLimitConform.position < 2.0);
    // a wide cut (1..720: wrapAt 719, width 719, lapLimit 719000719) and a start far past it
    const DropFrameTickResult wide = advanceDropFrameTick(2000000000.0, 3.0, 1, 720, true, false);
    ASSERT_TRUE(wide.wrapped);
    ASSERT_TRUE(wide.wrapLaps <= 1000000);
    ASSERT_TRUE(wide.position >= 0.0 && wide.position < 719.0);
    // a start that is not a number of frames at all (+inf): 6aaac7f5 counted INT_MAX - 1 laps (8 GB of appends)
    const DropFrameTickResult endlessStart = advanceDropFrameTick(HUGE_VAL, 1.0, 1, 2, true, false);
    ASSERT_TRUE(endlessStart.wrapped);
    ASSERT_TRUE(endlessStart.wrapLaps <= 1000000);
    ASSERT_TRUE(endlessStart.position >= 0.0 && endlessStart.position < 1.0);
    // ...and an infinite delta from a far start too
    const DropFrameTickResult endlessBoth = advanceDropFrameTick(1100000.0, HUGE_VAL, 1, 2, true, false);
    ASSERT_TRUE(endlessBoth.wrapLaps <= 1000000);
    ASSERT_TRUE(endlessBoth.position >= 0.0 && endlessBoth.position < 1.0);
    // r9 (sol r8 on 4eb1cfd5): a start just below the limit is folded too (4eb1cfd5 kept the base arithmetic and counted
    // its 1e6 stale laps as travel); it lands where it did, with no lap
    const DropFrameTickResult below = advanceDropFrameTick(1000000.0, 0.5, 1, 2, true, false);
    ASSERT_EQ(0, below.wrapLaps);
    ASSERT_TRUE(below.foldedFrames == 1000000.0);
    ASSERT_TRUE(below.position == 1000000.0 + 0.5 - 1000000.0);
}

// r6 (sol r5 S2): the fold keeps the tick's delta and the accounting the slip histogram reads (MainWindow's
// grantedDropFrames = position - before + foldedFrames + wrapWidth * wrapLaps) is the advance the tick applied.
TEST(PlaybackFpsMeterPolicy, AFoldedStartKeepsItsDeltaAndTheAppliedAdvanceAccounting)
{
    using playback_frame_range::advanceDropFrameTick;
    using playback_frame_range::DropFrameTickResult;
    const auto granted = [](const DropFrameTickResult & tick, double before) {
        // r8 iteration 1 (pass 14 A1 on a2983bf2): MainWindow's grantedDropFrames is the tick's own appliedFrames; the
        // endpoint identity agrees on every row here and is checked alongside, but it rounds next to a far stale start
        const double identity = tick.position - before + tick.foldedFrames + tick.clampedFrames
            + static_cast<double>( tick.wrapWidth ) * tick.wrapLaps;
        if( std::fabs( identity - tick.appliedFrames ) > 1e-6 ) return HUGE_VAL;
        return tick.appliedFrames;
    };
    const DropFrameTickResult stale = advanceDropFrameTick(1100000.0, 1.0, 1, 2, true, false);
    ASSERT_TRUE(stale.foldedFrames == 1100000.0);
    ASSERT_TRUE(granted(stale, 1100000.0) == 1.0);
    const DropFrameTickResult bounded = advanceDropFrameTick(1100000.0, 999990.0, 1, 2, true, false);
    ASSERT_EQ(999990, bounded.wrapLaps);
    ASSERT_TRUE(granted(bounded, 1100000.0) == 999990.0);
    const DropFrameTickResult farStale = advanceDropFrameTick(1000001.0, 2.4, 1, 2, true);
    ASSERT_TRUE(std::fabs(granted(farStale, 1000001.0) - 2.4) < 1e-6);
    const DropFrameTickResult wide = advanceDropFrameTick(2000000000.0, 3.0, 1, 720, true, false);
    ASSERT_TRUE(std::fabs(granted(wide, 2000000000.0) - 3.0) < 1e-6);
    const DropFrameTickResult conformed = advanceDropFrameTick(2000002.0, 0.5, 1, 2, true, true);
    ASSERT_EQ(2, conformed.wrapWidth);
    ASSERT_TRUE(std::fabs(granted(conformed, 2000002.0) - 0.5) < 1e-9);
    // a folded start whose (negative) delta ends below wrapAt made no lap: the fold was its only move back
    const DropFrameTickResult back = advanceDropFrameTick(1100000.0, -5.5, 1, 2, true, false);
    ASSERT_TRUE(back.wrapped);
    ASSERT_EQ(0, back.wrapLaps);
    ASSERT_TRUE(granted(back, 1100000.0) == -5.5);
    // a +inf start is not a number of frames: it folds to the cut's first position and reports an infinite fold
    const DropFrameTickResult endlessStart = advanceDropFrameTick(HUGE_VAL, 1.0, 1, 2, true, false);
    ASSERT_TRUE(std::isinf(endlessStart.foldedFrames));
    ASSERT_TRUE(std::fabs(endlessStart.position) < 1e-9);
    // r6 iteration 2 (pass 11 A1 on 4c41c80b): the stale distance records NO lap. Sol's row: paused drop position 1100000,
    // cut 1..2, an eligible 8 ms tick at 24 fps (0.192 frames) from phase 0 crosses no wrap point; 4c41c80b recorded one lap
    // (it folded onto [wrapAt, wrapAt + width)), and a zero grant did too
    const DropFrameTickResult phaseOnly = advanceDropFrameTick(1100000.0, 0.192, 1, 2, true, false);
    ASSERT_TRUE(phaseOnly.wrapped);
    ASSERT_EQ(0, phaseOnly.wrapLaps);
    ASSERT_TRUE(std::fabs(phaseOnly.position - 0.192) < 1e-12);
    ASSERT_TRUE(std::fabs(granted(phaseOnly, 1100000.0) - 0.192) < 1e-9);
    const DropFrameTickResult zeroGrant = advanceDropFrameTick(1100000.0, 0.0, 1, 2, true, false);
    ASSERT_EQ(0, zeroGrant.wrapLaps);
    ASSERT_TRUE(zeroGrant.position == 0.0);
    ASSERT_TRUE(granted(zeroGrant, 1100000.0) == 0.0);
    // the fold keeps the start's fractional phase: 1100000.25 + 0.5 lands on 0.75
    const DropFrameTickResult fractional = advanceDropFrameTick(1100000.25, 0.5, 1, 2, true, false);
    ASSERT_EQ(0, fractional.wrapLaps);
    ASSERT_TRUE(fractional.position == 0.75);
    // pass 11 A2: a negative delta that ends inside the cut no longer skips the fold, so the fold is reported
    const DropFrameTickResult backInside = advanceDropFrameTick(1100000.0, -1099999.5, 1, 2, true, false);
    ASSERT_TRUE(backInside.foldedFrames == 1100000.0);
    ASSERT_TRUE(granted(backInside, 1100000.0) == -1099999.5);
    // pass 11 A3: the phase comes from exact fmods, not a rounded difference: 2^54 on cut 1..4 (wrapAt 3, width 3) has
    // phase 2^54 mod 3 = 1 and a zero delta stays there (4c41c80b's rounded 2^54 - 3 lost it)
    const DropFrameTickResult huge = advanceDropFrameTick(18014398509481984.0, 0.0, 1, 4, true, false);
    ASSERT_EQ(0, huge.wrapLaps);
    ASSERT_TRUE(huge.position == 1.0);
    // r9 (sol r8 on 4eb1cfd5): a stale start below the lap limit is folded as well (4eb1cfd5 did not); nothing is folded
    // inside the cut or without Loop
    ASSERT_TRUE(advanceDropFrameTick(1000000.0, 0.5, 1, 2, true, false).foldedFrames == 1000000.0);
    ASSERT_TRUE(advanceDropFrameTick(20.0, 0.1, 1, 2, true).foldedFrames == 20.0);
    ASSERT_TRUE(advanceDropFrameTick(0.5, 1000000.0, 1, 2, true).foldedFrames == 0.0);
    ASSERT_TRUE(advanceDropFrameTick(1100000.0, 1.0, 1, 2, false).foldedFrames == 0.0);
    // r8 (sol r7 on 75ed5094): without Loop that stale start is CLAMPED onto the last frame (1) instead, a correction
    // with no travel, so the accounting reads 0 frames applied (75ed5094: -1099999)
    const DropFrameTickResult clamped = advanceDropFrameTick(1100000.0, 1.0, 1, 2, false);
    ASSERT_TRUE(clamped.position == 1.0);
    ASSERT_TRUE(clamped.clampedFrames == 1099999.0);
    ASSERT_TRUE(granted(clamped, 1100000.0) == 0.0);
    // the Loop ticks above never clamp
    ASSERT_TRUE(stale.clampedFrames == 0.0);
    ASSERT_TRUE(back.clampedFrames == 0.0);
}

// PLAYBACK-FPS-METER-MONOTONIC-CLOCK-1 r9 (sol r8 blocker on 4eb1cfd5; DC-06 catalogue row "a correction of a stale start
// counted as travelled laps"): r6 folded a stale start only at or past the tick's LAP LIMIT, so a stale start between the
// wrap point and that limit still wrapped the base's way and its stale distance was counted as travelled laps. Sol's repro:
// during Play, Drop Frame off, seek 100 -> 10, cut 1..50, Drop Frame and Loop on before the next eligible tick. The Drop
// Frame position is still 100 (the normalizer keeps a position inside the clip); cut 1..50 wraps at 49 by 49. A zero
// request moved 100 -> 2 with appliedFrames 0 and recorded two 49-frame laps, which the paint credit read as 2 + 98 - 10 =
// 90 source frames played; an 8 ms request at 24 fps (0.192 frames) recorded the same two laps. After r9 wrapLaps counts
// only laps the applied delta crossed from an in-cut start: ANY start outside the loop range [wrapAt - width, wrapAt) is
// folded into it first (a correction, reported as foldedFrames, zero laps).
TEST(PlaybackFpsMeterPolicy, AStaleLoopStartAnyDistanceOutsideTheCutIsACorrectionWithNoTravelledLaps)
{
    using playback_frame_range::advanceDropFrameTick;
    using playback_frame_range::DropFrameTickResult;
    using playback_frame_range::PlaybackWrapRecorder;
    // sol's exact repro, zero request: folded onto its own phase (100 mod 49 = 2), no lap, nothing applied
    const DropFrameTickResult zero = advanceDropFrameTick(100.0, 0.0, 1, 50, true, false);
    ASSERT_EQ(0, zero.wrapLaps);                 // 4eb1cfd5: 2
    ASSERT_TRUE(zero.position == 2.0);
    ASSERT_TRUE(zero.foldedFrames == 98.0);      // 4eb1cfd5: 0
    ASSERT_TRUE(zero.appliedFrames == 0.0);
    ASSERT_TRUE(zero.wrapped);                   // the position moved back (a correction), so no forward step is counted
    // ...and the 8 ms request at 24 fps: 0.192 frames applied from the folded start, still no lap
    const double request = playback_fps_meter::dropFrameRequestFrames(24.0, 8);
    const DropFrameTickResult ordinary = advanceDropFrameTick(100.0, request, 1, 50, true, false);
    ASSERT_EQ(0, ordinary.wrapLaps);             // 4eb1cfd5: 2
    ASSERT_TRUE(std::fabs(ordinary.position - 2.192) < 1e-12);
    ASSERT_TRUE(ordinary.foldedFrames == 98.0);
    ASSERT_TRUE(std::fabs(ordinary.appliedFrames - request) < 1e-12);
    // the consumers sol cites read the laps: the wrap recorder appends none, and the paint credit for 10 then 2 is a
    // backward move, never 2 + 98 - 10 = 90 frames
    PlaybackWrapRecorder recorder;
    if( zero.wrapped ) recorder.noteEngineWrap( zero.wrapWidth, zero.wrapLaps );
    if( ordinary.wrapped ) recorder.noteEngineWrap( ordinary.wrapWidth, ordinary.wrapLaps );
    ASSERT_EQ(0, recorder.engineWraps);
    PlaybackSourceAdvanceTracker paint;
    paint.notePresented( 1.000, 10, 0, 0 );
    paint.notePresented( 1.042, 2, recorder.engineWraps, recorder.engineWrapsWidth( 0, recorder.engineWraps ) );
    ASSERT_EQ(0LL, (long long)paint.advancedFrames());   // 4eb1cfd5: 90
    // the same stale start under conform (cut 1..50 wraps at 50 by 50): 100 is on phase 0, no lap
    const DropFrameTickResult conformed = advanceDropFrameTick(100.0, 0.0, 1, 50, true, true);
    ASSERT_EQ(0, conformed.wrapLaps);            // 4eb1cfd5: 1
    ASSERT_TRUE(conformed.position == 0.0);
    ASSERT_TRUE(conformed.foldedFrames == 100.0);
    // a start exactly at the wrap point (Loop turned on over a non-Loop clamp at the last frame, 49) is a correction too
    const DropFrameTickResult atWrap = advanceDropFrameTick(49.0, request, 1, 50, true, false);
    ASSERT_EQ(0, atWrap.wrapLaps);               // 4eb1cfd5: 1
    ASSERT_TRUE(std::fabs(atWrap.position - request) < 1e-12);
    ASSERT_TRUE(atWrap.foldedFrames == 49.0);
    // a stale start BELOW the cut (seek 0, cut 41..50: loop range [40, 49), width 9) is moved onto the cut's first frame,
    // a correction (foldedFrames 0 - 40 = -40), and only then does the delta apply; 4eb1cfd5 left it at 0.192, outside the cut
    const DropFrameTickResult below = advanceDropFrameTick(0.0, request, 41, 50, true, false);
    ASSERT_EQ(0, below.wrapLaps);
    ASSERT_TRUE(below.foldedFrames == -40.0);    // 4eb1cfd5: 0
    ASSERT_TRUE(std::fabs(below.position - (40.0 + request)) < 1e-12);
    ASSERT_TRUE(std::fabs(below.appliedFrames - request) < 1e-12);
    ASSERT_TRUE(below.wrapped);                  // moved by a correction, not travel: no forward step counted
    // ...and a delta wide enough to cross the wrap point from below counts only the laps it crossed from the cut's first
    // frame: 0 + 45 on cut 41..50 (4eb1cfd5: 45, no lap, still outside the cut) is 40 + 45 = 85, five laps of 9, to 40
    const DropFrameTickResult belowWide = advanceDropFrameTick(0.0, 45.0, 41, 50, true, false);
    ASSERT_EQ(5, belowWide.wrapLaps);
    ASSERT_TRUE(belowWide.position == 40.0);
    ASSERT_TRUE(belowWide.appliedFrames == 45.0);
    // laps the applied delta crosses from an in-cut start ARE travel, unchanged: 48.9 + 0.192 wraps once, and r7's whole
    // laps that land back on the start (0 + 999990 on cut 1..616: 1626 laps, 0 -> 0) still count every lap
    const DropFrameTickResult inCut = advanceDropFrameTick(48.9, request, 1, 50, true, false);
    ASSERT_EQ(1, inCut.wrapLaps);
    ASSERT_TRUE(inCut.foldedFrames == 0.0);
    const DropFrameTickResult wholeLaps = advanceDropFrameTick(0.0, 999990.0, 1, 616, true, false);
    ASSERT_EQ(1626, wholeLaps.wrapLaps);
    ASSERT_TRUE(wholeLaps.position == 0.0);
    ASSERT_TRUE(wholeLaps.foldedFrames == 0.0);
    ASSERT_TRUE(wholeLaps.appliedFrames == 999990.0);
    // without Loop nothing is folded (the r8 clamp owns a start past the last frame; a start below the cut plays on)
    ASSERT_TRUE(advanceDropFrameTick(100.0, 0.0, 1, 50, false, false).foldedFrames == 0.0);
    ASSERT_TRUE(advanceDropFrameTick(0.0, request, 41, 50, false, false).foldedFrames == 0.0);
}

// r6 (sol r5 S1 on 6aaac7f5): the Drop Frame request is finite. An accepted rate the cut cannot bound (+inf: a silent MLV
// with FPS denominator 0) left an 8 ms tick at 0 ms and the call site's expression read inf * 0 = NaN; the request now
// fails closed to 0 frames, and every finite request is the former expression, bit for bit.
TEST(PlaybackFpsMeterPolicy, DropFrameRequestIsTheCallSitesExpressionWhenFiniteAndNothingOtherwise)
{
    using playback_fps_meter::dropFrameRequestFrames;
    using playback_fps_meter::frameBoundedTickTimeDiffMs;
    const int cappedMs = frameBoundedTickTimeDiffMs(8, HUGE_VAL);
    ASSERT_EQ(0, cappedMs);
    ASSERT_TRUE(std::isnan(HUGE_VAL * (double)cappedMs / 1000.0)); // what 6aaac7f5 handed the pace guard
    const double request = dropFrameRequestFrames(HUGE_VAL, cappedMs);
    ASSERT_TRUE(std::isfinite(request));
    ASSERT_TRUE(request == 0.0);
    ASSERT_TRUE(dropFrameRequestFrames(HUGE_VAL, 8) == 0.0);
    ASSERT_TRUE(dropFrameRequestFrames(-HUGE_VAL, 8) == 0.0);
    ASSERT_TRUE(dropFrameRequestFrames(std::nan(""), 8) == 0.0);
    ASSERT_TRUE(dropFrameRequestFrames(1.0e308, INT_MAX) == 0.0); // overflows to inf
    // every finite request is unchanged, bit for bit
    const double ntsc = 24000.0 / 1001.0;
    ASSERT_TRUE(dropFrameRequestFrames(24.0, 8) == 24.0 * (double)8 / 1000.0);
    ASSERT_TRUE(dropFrameRequestFrames(ntsc, 42) == ntsc * (double)42 / 1000.0);
    ASSERT_TRUE(dropFrameRequestFrames(ntsc, -42) == ntsc * (double)-42 / 1000.0);
    ASSERT_TRUE(dropFrameRequestFrames(24.0, 0) == 0.0);
    const double rate = 27777500.0 / 1771.0;
    const int keptMs = frameBoundedTickTimeDiffMs(63757, rate);
    ASSERT_TRUE(dropFrameRequestFrames(rate, keptMs) == rate * (double)keptMs / 1000.0);
    ASSERT_TRUE(dropFrameRequestFrames(rate, keptMs) <= playback_fps_meter::kMaxTickFrames);
    // the drop tick sol traced (eligible 8 ms, Loop cut 1..100) now lands on a finite position
    const playback_frame_range::DropFrameTickResult tick =
        playback_frame_range::advanceDropFrameTick(10.0, request, 1, 100, true);
    ASSERT_TRUE(std::isfinite(tick.position));
    ASSERT_TRUE(tick.position == 10.0);
}
