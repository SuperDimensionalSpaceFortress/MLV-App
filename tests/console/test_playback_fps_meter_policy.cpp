#include "../common/minitest.h"

#include "../../platform/qt/PlaybackFpsMeterPolicy.h"
#include "../../platform/qt/PlaybackFrameRange.h"

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
    // a stale start already a million laps past the cut keeps the base arithmetic: 1000001 + 2.4 wraps 1000003 laps to
    // 0.4 and grants 2.4 (7a0f5a1c landed on 0 after 1e6 laps and reported -1). The laps of the stale start ALONE are the
    // base's on any tick (PRE-EXISTING; O(1) lap recording is PLAYBACK-WRAP-RECORDER-RUNLENGTH-1)
    const playback_frame_range::DropFrameTickResult farStale =
        playback_frame_range::advanceDropFrameTick(1000001.0, 2.4, 1, 2, true);
    ASSERT_EQ(1000003, farStale.wrapLaps);
    const double farGranted = farStale.position - 1000001.0 + (double)farStale.wrapWidth * farStale.wrapLaps;
    ASSERT_TRUE(std::fabs(farGranted - 2.4) < 1e-6);
    ASSERT_TRUE(farStale.position >= 0.0 && farStale.position < 1.0);
    // PRE-EXISTING characterization: a stale start far past the cut (a long clip) with an ordinary request
    const playback_frame_range::DropFrameTickResult far =
        playback_frame_range::advanceDropFrameTick(3000000.0, 0.25, 1, 2, true);
    ASSERT_EQ(3000000, far.wrapLaps);
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
    // an ordinary tick from a stale start is unchanged: 20.1 wraps 20 laps to 0.1
    const playback_frame_range::DropFrameTickResult ordinary =
        playback_frame_range::advanceDropFrameTick(20.0, 0.1, 1, 2, true);
    ASSERT_EQ(20, ordinary.wrapLaps);
    ASSERT_TRUE(std::fabs(ordinary.position - 0.1) < 1e-9);
    // ...and so is a tick inside the cut that wraps exactly 1,000,000 laps (the bound itself is admitted)
    const playback_frame_range::DropFrameTickResult atBound =
        playback_frame_range::advanceDropFrameTick(0.5, 1000000.0, 1, 2, true);
    ASSERT_EQ(1000000, atBound.wrapLaps);
    ASSERT_TRUE(std::fabs(atBound.position - 0.5) < 1e-6);
    // r5 (pass 8 A1 on c37d6fd5): a clipped tick stops at the LAST position whose laps are still 1e6, not the first
    // position of that lap, so a stale start inside [lapLimit - width, lapLimit) keeps the room it had. c37d6fd5 cut
    // 2000000 + 4.5 (conform, cut 1..2: wrapAt 2, width 2, lapLimit 2000002) to 2000000: position 0, advance 0.
    const playback_frame_range::DropFrameTickResult clippedConform =
        playback_frame_range::advanceDropFrameTick(2000000.0, 4.5, 1, 2, true, true);
    ASSERT_EQ(1000000, clippedConform.wrapLaps);
    ASSERT_TRUE(clippedConform.position > 1.9999 && clippedConform.position < 2.0);
    const double clippedConformGranted =
        clippedConform.position - 2000000.0 + (double)clippedConform.wrapWidth * clippedConform.wrapLaps;
    ASSERT_TRUE(clippedConformGranted > 1.9999);
    // ...and without conform (cut 1..2: wrapAt 1, width 1, lapLimit 1000001), 1000000 + 1.5 keeps almost one frame
    const playback_frame_range::DropFrameTickResult clippedPlain =
        playback_frame_range::advanceDropFrameTick(1000000.0, 1.5, 1, 2, true);
    ASSERT_EQ(1000000, clippedPlain.wrapLaps);
    ASSERT_TRUE(clippedPlain.position >= 0.0 && clippedPlain.position < 1.0);
    const double clippedPlainGranted =
        clippedPlain.position - 1000000.0 + (double)clippedPlain.wrapWidth * clippedPlain.wrapLaps;
    ASSERT_TRUE(clippedPlainGranted > 0.9999 && clippedPlainGranted < 1.0);
}
