#include "../common/minitest.h"

#include "../../platform/qt/PlaybackFpsMeterPolicy.h"

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
};
} // namespace

TEST(PlaybackFpsMeterPolicy, StampAgeIsTheMonotonicIntervalZeroWhenUnstartedAndSaturated)
{
    ASSERT_EQ(42, stampAgeMs(FakeStamp{ true, 10 }, FakeStamp{ true, 52 }));
    ASSERT_EQ(0, stampAgeMs(FakeStamp{ false, 10 }, FakeStamp{ true, 52 }));  // never-started then
    ASSERT_EQ(0, stampAgeMs(FakeStamp{ true, 10 }, FakeStamp{ false, 52 }));  // never-started now
    ASSERT_EQ(INT_MAX, stampAgeMs(FakeStamp{ true, 0 }, FakeStamp{ true, 3000000000LL }));
    ASSERT_EQ(INT_MIN, stampAgeMs(FakeStamp{ true, 0 }, FakeStamp{ true, -3000000000LL }));
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
