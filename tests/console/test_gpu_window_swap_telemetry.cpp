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

// New-frame counting (CUDA-PLAYBACK-PRESENT-CADENCE-2 round 1): the round's own acceptance
// denominator. isNewFrameSwap is the extracted pure predicate; the summarize() tests below
// cover the derived fps/gap/p95 arithmetic built on top of it.

TEST(GpuWindowSwapTelemetryPolicy, IsNewFrameSwapRejectsAnInvalidPresentedSerial)
{
    // A swap whose route never supplied a real presentationSerial (e.g. a screenshot
    // capture with no playback-smoke session) must never count as a new frame.
    ASSERT_FALSE(GpuWindowSwapTelemetryPolicy::isNewFrameSwap(
        /*presentedSerialValid=*/false, /*presentedSerial=*/7, /*previousCounted=*/0));
}

TEST(GpuWindowSwapTelemetryPolicy, IsNewFrameSwapAcceptsTheFirstValidSerial)
{
    // previousCountedNewFramePresentedSerial starts at 0 (the sentinel), and a real
    // presentationSerial is never 0 (MainWindow's presentImageIfActive/
    // presentGpuPlaybackReconAmazePostWbTextureIfActive treat 0 as "no serial supplied"),
    // so the first genuinely-serialed swap of a session must always count.
    ASSERT_TRUE(GpuWindowSwapTelemetryPolicy::isNewFrameSwap(
        /*presentedSerialValid=*/true, /*presentedSerial=*/1, /*previousCounted=*/0));
}

TEST(GpuWindowSwapTelemetryPolicy, IsNewFrameSwapRejectsARepaintOfAnAlreadyCountedSerial)
{
    // MUTATION-TESTED (design review note "e"): dropping the strict "greater than" check
    // (e.g. changing it to >=, or to just presentedSerialValid) makes this assertion fail,
    // because a leftover update() repainting serial 5 a second time -- exactly what
    // paint-per-submit's "skip the trailing update() when painted" guard exists to avoid --
    // would then double-count. presentedSerial equal to (not just less than) the previous
    // counted serial must also be rejected.
    ASSERT_FALSE(GpuWindowSwapTelemetryPolicy::isNewFrameSwap(
        /*presentedSerialValid=*/true, /*presentedSerial=*/5, /*previousCounted=*/5));
    ASSERT_FALSE(GpuWindowSwapTelemetryPolicy::isNewFrameSwap(
        /*presentedSerialValid=*/true, /*presentedSerial=*/4, /*previousCounted=*/5));
}

TEST(GpuWindowSwapTelemetryPolicy, IsNewFrameSwapAcceptsAStrictlyAdvancingSerial)
{
    ASSERT_TRUE(GpuWindowSwapTelemetryPolicy::isNewFrameSwap(
        /*presentedSerialValid=*/true, /*presentedSerial=*/6, /*previousCounted=*/5));
}

TEST(GpuWindowSwapTelemetryPolicy, ZeroNewFrameSwapsReportsZeroFpsAndZeroGaps)
{
    GpuWindowSwapTelemetryCounters counters;
    counters.swapCount = 40; // real swaps happened, but none were ever new-frame swaps

    const GpuWindowSwapTelemetrySummary summary =
        GpuWindowSwapTelemetryPolicy::summarize(counters);

    ASSERT_EQ(static_cast<quint64>(0), summary.newFrameSwapCount);
    ASSERT_NEAR(0.0, summary.newFrameSwapFps, 1e-9);
    ASSERT_NEAR(0.0, summary.newFrameMaxGapMs, 1e-9);
    ASSERT_NEAR(0.0, summary.newFrameP95GapMs, 1e-9);
}

TEST(GpuWindowSwapTelemetryPolicy, NewFrameCadenceIsIndependentOfTotalSwapCadence)
{
    // Modeled on the round's own scored diagnosis: a naive fix can inflate swaps= without
    // inflating displayed new content. 200 total swaps at ~60 fps, but only 25 of them ever
    // advanced the presented serial, at an even 25 fps -- the two fps figures must be
    // computed independently and must not collide.
    GpuWindowSwapTelemetryCounters counters;
    counters.swapCount = 200;
    counters.firstSwapQpcMs = 0.0;
    counters.lastSwapQpcMs = 3316.67; // ~60 fps over 199 intervals
    counters.newFrameSwapCount = 25;
    counters.newFrameFirstSwapQpcMs = 0.0;
    counters.newFrameLastSwapQpcMs = 1000.0; // 25 new-frame swaps over 1000 ms = 24 intervals

    const GpuWindowSwapTelemetrySummary summary =
        GpuWindowSwapTelemetryPolicy::summarize(counters);

    ASSERT_NEAR(60.0, summary.swapFps, 0.1);
    ASSERT_NEAR(24.0, summary.newFrameSwapFps, 0.1);
}

TEST(GpuWindowSwapTelemetryPolicy, NewFrameMaxGapAndItsSerialBracketPassThroughUnchanged)
{
    GpuWindowSwapTelemetryCounters counters;
    counters.newFrameSwapCount = 10;
    counters.newFrameMaxGapMs = 412.5;
    counters.newFrameMaxGapBeforePresentedSerial = 7;
    counters.newFrameMaxGapAfterPresentedSerial = 9;

    const GpuWindowSwapTelemetrySummary summary =
        GpuWindowSwapTelemetryPolicy::summarize(counters);

    ASSERT_NEAR(412.5, summary.newFrameMaxGapMs, 1e-9);
    ASSERT_EQ(static_cast<quint64>(7), summary.newFrameMaxGapBeforePresentedSerial);
    ASSERT_EQ(static_cast<quint64>(9), summary.newFrameMaxGapAfterPresentedSerial);
}

TEST(GpuWindowSwapTelemetryPolicy, NewFrameP95GapIsNearestRankOverObservedSamples)
{
    // 5 gap samples with one outlier, deliberately pushed onto the vector OUT OF ORDER
    // (so a bug that reads the unsorted vector's raw last element, rather than sorting
    // first, would read this test right by accident on a pre-sorted input but wrong here).
    // Nearest-rank p95 of N=5 ascending-sorted samples is index ceil(0.95*5)-1 = 4
    // (0-based) -- the last (largest) sample, i.e. the 500ms outlier.
    GpuWindowSwapTelemetryCounters counters;
    counters.newFrameSwapCount = 6;
    counters.newFrameGapSamplesMs.push_back(30.0);
    counters.newFrameGapSamplesMs.push_back(500.0);
    counters.newFrameGapSamplesMs.push_back(10.0);
    counters.newFrameGapSamplesMs.push_back(40.0);
    counters.newFrameGapSamplesMs.push_back(20.0);

    const GpuWindowSwapTelemetrySummary summary =
        GpuWindowSwapTelemetryPolicy::summarize(counters);

    ASSERT_NEAR(500.0, summary.newFrameP95GapMs, 1e-9);
}

TEST(GpuWindowSwapTelemetryPolicy, NewFrameP95GapWithASingleSampleReturnsThatSample)
{
    GpuWindowSwapTelemetryCounters counters;
    counters.newFrameSwapCount = 2;
    counters.newFrameGapSamplesMs.push_back(33.3);

    const GpuWindowSwapTelemetrySummary summary =
        GpuWindowSwapTelemetryPolicy::summarize(counters);

    ASSERT_NEAR(33.3, summary.newFrameP95GapMs, 1e-9);
}

// round 1c (sol BLOCKER): a display that stops showing new content must not report a
// passing new-frame cadence just because no further new-frame swap ever arrived to close
// the interior-gap/fps math above. These pin sol's own repro numbers.

TEST(GpuWindowSwapTelemetryPolicy, TailStallPastLastNewFrameWidensMaxGapAndDepressesRate)
{
    // sol's repro: 241 new frames at an even 40 ms cadence through t=9.6 s (240 intervals,
    // so first=0, last=9600), then nothing until the smoke gate at t=20 s. Pre-fix, the
    // interior max gap (40 ms) and an fps computed only over [first, last] (~24 fps) never
    // saw the 10.4 s of dead air between the last new frame and the gate.
    GpuWindowSwapTelemetryCounters counters;
    counters.newFrameSwapCount = 241;
    counters.newFrameFirstSwapQpcMs = 0.0;
    counters.newFrameLastSwapQpcMs = 9600.0;
    counters.newFrameMaxGapMs = 40.0;
    counters.newFrameMaxGapBeforePresentedSerial = 240;
    counters.newFrameMaxGapAfterPresentedSerial = 241;
    counters.lastCountedNewFramePresentedSerial = 241;

    const GpuWindowSwapTelemetrySummary summary =
        GpuWindowSwapTelemetryPolicy::summarize(counters, /*sessionBeginQpcMs=*/0.0, /*gateQpcMs=*/20000.0);

    ASSERT_NEAR(10400.0, summary.newFrameMaxGapMs, 1e-6);
    ASSERT_EQ(static_cast<quint64>(241), summary.newFrameMaxGapBeforePresentedSerial);
    ASSERT_EQ(static_cast<quint64>(0), summary.newFrameMaxGapAfterPresentedSerial);
    ASSERT_NEAR(12.05, summary.newFrameSwapFps, 0.01);
}

TEST(GpuWindowSwapTelemetryPolicy, NoTailStallLeavesInteriorGapAndPreGateRateUnchanged)
{
    // Same even cadence, but the gate arrives right after the last new-frame swap (no
    // dead air): the interior max gap must still win over the (near-zero) tail gap, and
    // the gated rate formula (count / (gate - first)) must land close to the steady-state
    // cadence rather than being depressed by a stall that never happened.
    GpuWindowSwapTelemetryCounters counters;
    counters.newFrameSwapCount = 241;
    counters.newFrameFirstSwapQpcMs = 0.0;
    counters.newFrameLastSwapQpcMs = 9600.0;
    counters.newFrameMaxGapMs = 40.0;
    counters.newFrameMaxGapBeforePresentedSerial = 240;
    counters.newFrameMaxGapAfterPresentedSerial = 241;

    const GpuWindowSwapTelemetrySummary summary =
        GpuWindowSwapTelemetryPolicy::summarize(counters, /*sessionBeginQpcMs=*/0.0, /*gateQpcMs=*/9600.0);

    ASSERT_NEAR(40.0, summary.newFrameMaxGapMs, 1e-9);
    ASSERT_EQ(static_cast<quint64>(240), summary.newFrameMaxGapBeforePresentedSerial);
    ASSERT_EQ(static_cast<quint64>(241), summary.newFrameMaxGapAfterPresentedSerial);
    ASSERT_NEAR(25.1, summary.newFrameSwapFps, 0.01);
}

TEST(GpuWindowSwapTelemetryPolicy, UngatedSummarizeCallIsUnaffectedByTheTailStallFix)
{
    // A caller that never supplies a gate (gateQpcMs defaults to 0.0, as every pre-round-1c
    // unit test above does) must keep the original interior-only count-1/span arithmetic --
    // this is the regression guard for the "haveGate" branch itself.
    GpuWindowSwapTelemetryCounters counters;
    counters.newFrameSwapCount = 241;
    counters.newFrameFirstSwapQpcMs = 0.0;
    counters.newFrameLastSwapQpcMs = 9600.0;
    counters.newFrameMaxGapMs = 40.0;

    const GpuWindowSwapTelemetrySummary summary =
        GpuWindowSwapTelemetryPolicy::summarize(counters);

    ASSERT_NEAR(40.0, summary.newFrameMaxGapMs, 1e-9);
    ASSERT_NEAR(25.0, summary.newFrameSwapFps, 0.01); // (241 - 1) * 1000 / 9600
}
