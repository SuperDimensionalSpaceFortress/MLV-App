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
    // CHARACTERIZATION of the defect: the unbounded request leaves the cut
    const playback_frame_range::DropFrameTickResult unbounded =
        playback_frame_range::advanceDropFrameTick(0.0, 25000.0 * kMaxStampAgeMs / 1000.0, 1, 2, true);
    ASSERT_TRUE(unbounded.position >= 1.0);
    // bounded: the same suspension lands inside the wrap range [wrapAt - width, wrapAt) = [0, 1)
    const int bounded = frameBoundedTickTimeDiffMs(kMaxStampAgeMs, 25000.0);
    ASSERT_EQ(40000000, bounded); // 1e9 frames at 25000 fps
    ASSERT_TRUE(25000.0 * bounded / 1000.0 <= kMaxTickFrames);
    const playback_frame_range::DropFrameTickResult tick =
        playback_frame_range::advanceDropFrameTick(0.0, 25000.0 * bounded / 1000.0, 1, 2, true);
    ASSERT_TRUE(tick.wrapped);
    ASSERT_TRUE(tick.position >= 0.0 && tick.position < 1.0);
    // the largest MLV rate (uint32 numerator over 1) is bounded too, at no less than 1 ms
    const int huge = frameBoundedTickTimeDiffMs(kMaxStampAgeMs, 4294967295.0);
    ASSERT_EQ(232, huge);
    ASSERT_TRUE(4294967295.0 * huge / 1000.0 <= kMaxTickFrames);
    ASSERT_EQ(1, frameBoundedTickTimeDiffMs(kMaxStampAgeMs, 1.0e13));
    ASSERT_EQ(-40000000, frameBoundedTickTimeDiffMs(-kMaxStampAgeMs, 25000.0));
    // every ordinary tick is unchanged
    ASSERT_EQ(8, frameBoundedTickTimeDiffMs(8, 1000000.0));
    ASSERT_EQ(42, frameBoundedTickTimeDiffMs(42, 24.0));
    ASSERT_EQ(kMaxStampAgeMs, frameBoundedTickTimeDiffMs(kMaxStampAgeMs, 24.0));
    ASSERT_EQ(1, frameBoundedTickTimeDiffMs(1, 25000.0));
    ASSERT_EQ(7, frameBoundedTickTimeDiffMs(7, 0.0)); // no rate: nothing to bound
}
