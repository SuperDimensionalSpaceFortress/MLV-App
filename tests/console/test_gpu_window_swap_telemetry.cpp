#include "../common/minitest.h"

#include "../../platform/qt/GpuWindowSwapTelemetry.h"

TEST(GpuWindowSwapTelemetryPolicy, ZeroSwapsReportsZeroFpsAndZeroGap)
{
    GpuWindowSwapTelemetryCounters counters;

    const GpuWindowSwapTelemetrySummary summary =
        GpuWindowSwapTelemetryPolicy::summarize(counters);

    ASSERT_EQ(static_cast<quint64>(0), summary.swapCount);
    ASSERT_NEAR(0.0, summary.swapFps, 1e-9);
    ASSERT_NEAR(0.0, summary.maxGapMs, 1e-9);
}

TEST(GpuWindowSwapTelemetryPolicy, SingleSwapReportsZeroFpsNotDivideByZero)
{
    GpuWindowSwapTelemetryCounters counters;
    counters.swapCount = 1;
    counters.firstSwapQpcMs = 1000.0;
    counters.lastSwapQpcMs = 1000.0;

    const GpuWindowSwapTelemetrySummary summary =
        GpuWindowSwapTelemetryPolicy::summarize(counters);

    ASSERT_NEAR(0.0, summary.swapFps, 1e-9);
}

TEST(GpuWindowSwapTelemetryPolicy, ZeroSpanWithMultipleSwapsReportsZeroFpsNotInfinity)
{
    // Same QPC ms twice in a row (clock-granularity edge case) must not divide by zero.
    GpuWindowSwapTelemetryCounters counters;
    counters.swapCount = 3;
    counters.firstSwapQpcMs = 500.0;
    counters.lastSwapQpcMs = 500.0;

    const GpuWindowSwapTelemetrySummary summary =
        GpuWindowSwapTelemetryPolicy::summarize(counters);

    ASSERT_NEAR(0.0, summary.swapFps, 1e-9);
}

TEST(GpuWindowSwapTelemetryPolicy, EvenCadenceReportsExpectedFpsAndGapLocation)
{
    GpuWindowSwapTelemetryCounters counters;
    counters.swapCount = 61;          // 60 intervals
    counters.firstSwapQpcMs = 0.0;
    counters.lastSwapQpcMs = 1000.0;  // 60 swaps spread over 1000 ms after the first
    counters.maxGapMs = 16.6667;
    counters.maxGapBeforeSerial = 12;
    counters.maxGapAfterSerial = 13;

    const GpuWindowSwapTelemetrySummary summary =
        GpuWindowSwapTelemetryPolicy::summarize(counters);

    ASSERT_NEAR(60.0, summary.swapFps, 0.01);
    ASSERT_NEAR(16.6667, summary.maxGapMs, 1e-6);
    ASSERT_EQ(static_cast<quint64>(12), summary.maxGapBeforeSerial);
    ASSERT_EQ(static_cast<quint64>(13), summary.maxGapAfterSerial);
}

TEST(GpuWindowSwapTelemetryPolicy, SwapCountPassesThroughUnchanged)
{
    GpuWindowSwapTelemetryCounters counters;
    counters.swapCount = 4242;
    counters.firstSwapQpcMs = 0.0;
    counters.lastSwapQpcMs = 1.0;

    const GpuWindowSwapTelemetrySummary summary =
        GpuWindowSwapTelemetryPolicy::summarize(counters);

    ASSERT_EQ(static_cast<quint64>(4242), summary.swapCount);
}

TEST(GpuWindowSwapTelemetryPolicy, HeadAndTailGapComputedAgainstSessionBeginAndGate)
{
    // Session begins at t=0, first swap at t=50 (head gap), swaps continue, last swap
    // at t=900, gate (session close) at t=1200 (tail gap) -- a terminal freeze.
    GpuWindowSwapTelemetryCounters counters;
    counters.swapCount = 2;
    counters.firstSwapQpcMs = 50.0;
    counters.lastSwapQpcMs = 900.0;

    const GpuWindowSwapTelemetrySummary summary =
        GpuWindowSwapTelemetryPolicy::summarize(counters, /*sessionBeginQpcMs=*/0.0, /*gateQpcMs=*/1200.0);

    ASSERT_NEAR(50.0, summary.headGapMs, 1e-9);
    ASSERT_NEAR(300.0, summary.tailGapMs, 1e-9);
}

TEST(GpuWindowSwapTelemetryPolicy, ZeroSwapsReportsZeroHeadAndTailGapRegardlessOfTimestamps)
{
    // No swaps happened: even with a large sessionBegin/gate span, there is no first/last
    // swap to gap against, so both must stay 0 rather than reporting the whole session.
    GpuWindowSwapTelemetryCounters counters;

    const GpuWindowSwapTelemetrySummary summary =
        GpuWindowSwapTelemetryPolicy::summarize(counters, /*sessionBeginQpcMs=*/0.0, /*gateQpcMs=*/5000.0);

    ASSERT_NEAR(0.0, summary.headGapMs, 1e-9);
    ASSERT_NEAR(0.0, summary.tailGapMs, 1e-9);
}

TEST(GpuWindowSwapTelemetryPolicy, HeadAndTailGapClampAtZeroRatherThanGoNegative)
{
    // Defensive: a swap can never precede the session begin it was reset under, nor
    // follow the gate snapshot taken after it, but the clamp must hold even if the inputs
    // disagree (e.g. a clock-skew edge case).
    GpuWindowSwapTelemetryCounters counters;
    counters.swapCount = 1;
    counters.firstSwapQpcMs = 100.0;
    counters.lastSwapQpcMs = 100.0;

    const GpuWindowSwapTelemetrySummary summary =
        GpuWindowSwapTelemetryPolicy::summarize(counters, /*sessionBeginQpcMs=*/200.0, /*gateQpcMs=*/50.0);

    ASSERT_NEAR(0.0, summary.headGapMs, 1e-9);
    ASSERT_NEAR(0.0, summary.tailGapMs, 1e-9);
}

TEST(GpuWindowSwapTelemetryPolicy, FirstAndLastSwapUtcPassThroughUnchanged)
{
    GpuWindowSwapTelemetryCounters counters;
    counters.swapCount = 2;
    counters.firstSwapUtc = QStringLiteral("2026-09-25T11:00:00.000Z");
    counters.lastSwapUtc = QStringLiteral("2026-09-25T11:00:05.000Z");

    const GpuWindowSwapTelemetrySummary summary =
        GpuWindowSwapTelemetryPolicy::summarize(counters);

    ASSERT_EQ(std::string("2026-09-25T11:00:00.000Z"), summary.firstSwapUtc.toStdString());
    ASSERT_EQ(std::string("2026-09-25T11:00:05.000Z"), summary.lastSwapUtc.toStdString());
}

// Fate telemetry (CUDA-PLAYBACK-PRESENT-CADENCE-1): superseded-before-paint counts, mirroring
// the same pure-math/counters-vs-summary split the swap-cadence fields above already use.

TEST(GpuWindowSwapTelemetryPolicy, NoSupersededFramesReportsZeroCountAndZeroSerials)
{
    GpuWindowSwapTelemetryCounters counters;
    counters.swapCount = 156;

    const GpuWindowSwapTelemetrySummary summary =
        GpuWindowSwapTelemetryPolicy::summarize(counters);

    ASSERT_EQ(static_cast<quint64>(0), summary.supersededCount);
    ASSERT_EQ(static_cast<quint64>(0), summary.lastSupersededSerial);
    ASSERT_EQ(static_cast<quint64>(0), summary.lastSupersededBySerial);
}

TEST(GpuWindowSwapTelemetryPolicy, SupersededCountAndLastSerialsPassThroughUnchanged)
{
    // Modeled on the gpu1b leg from docs/cuda-playback-present-cadence.md: 229 swaps,
    // 777 superseded-before-paint between them, most recent supersession serial 1009
    // overwritten by serial 1010 (the one that went on to swap).
    GpuWindowSwapTelemetryCounters counters;
    counters.swapCount = 229;
    counters.supersededCount = 777;
    counters.lastSupersededSerial = 1009;
    counters.lastSupersededBySerial = 1010;

    const GpuWindowSwapTelemetrySummary summary =
        GpuWindowSwapTelemetryPolicy::summarize(counters);

    ASSERT_EQ(static_cast<quint64>(777), summary.supersededCount);
    ASSERT_EQ(static_cast<quint64>(1009), summary.lastSupersededSerial);
    ASSERT_EQ(static_cast<quint64>(1010), summary.lastSupersededBySerial);
}

TEST(GpuWindowSwapTelemetryPolicy, SupersededCountIsIndependentOfSwapFpsArithmetic)
{
    // A leg with heavy supersession must not perturb the unrelated swap_fps/gap math --
    // the two are accumulated and reported independently (guards against a future change
    // that folds supersededCount into the same running total as swapCount by mistake).
    GpuWindowSwapTelemetryCounters counters;
    counters.swapCount = 61;
    counters.firstSwapQpcMs = 0.0;
    counters.lastSwapQpcMs = 1000.0;
    counters.supersededCount = 5000;

    const GpuWindowSwapTelemetrySummary summary =
        GpuWindowSwapTelemetryPolicy::summarize(counters);

    ASSERT_NEAR(60.0, summary.swapFps, 0.01);
    ASSERT_EQ(static_cast<quint64>(5000), summary.supersededCount);
}
