#include "../common/minitest.h"

#include "../../platform/qt/PlaybackFpsMeterPolicy.h"

#include <cmath>

using playback_fps_meter::fpsMeterShouldReset;
using playback_fps_meter::fpsMeterStalled;
using playback_fps_meter::kFpsMeterStallMs;
using playback_fps_meter::smoothedFrameMs;

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
