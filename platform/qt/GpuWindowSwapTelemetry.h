/*!
 * \file GpuWindowSwapTelemetry.h
 * \brief Pure swap-cadence summary math for GpuDisplayWindow's opt-in swap telemetry,
 *        extracted so it can be unit-tested without a live GL window (mirrors
 *        PlaybackGatePolicy.h's split between counters accumulated by the GUI and the
 *        arithmetic that turns them into a report line).
 */

#ifndef GPUWINDOWSWAPTELEMETRY_H
#define GPUWINDOWSWAPTELEMETRY_H

#include <QString>
#include <QtGlobal>
#include <algorithm>
#include <cmath>
#include <cstddef>
#include <vector>

/*! \brief Cumulative SESSION totals GpuDisplayWindow accumulates while it performs real
 *  buffer swaps (Qt's own automatic swap after paintGL() and the explicit swapBuffers()
 *  in grabPresentedFramebufferIfActive), before any summarizing arithmetic. */
struct GpuWindowSwapTelemetryCounters
{
    quint64 swapCount = 0;
    double firstSwapQpcMs = 0.0;
    double lastSwapQpcMs = 0.0;
    QString firstSwapUtc;
    QString lastSwapUtc;
    double maxGapMs = 0.0;
    quint64 maxGapBeforeSerial = 0;
    quint64 maxGapAfterSerial = 0;
    // Fate telemetry (CUDA-PLAYBACK-PRESENT-CADENCE-1): a submitted frame is "superseded
    // before paint" when a NEWER present call overwrites the window's single pending slot
    // (m_pendingPresentationSerial) before paintGL() ever drew it -- the window holds
    // exactly one pending frame, not a queue, so every submission that is not the most
    // recent one at the moment Qt actually paints is silently lost by construction. This
    // was previously invisible: only the swap count and the app's own upstream
    // frames_presented counter existed, and their difference had to be inferred
    // out-of-band (see docs/cuda-playback-present-cadence.md). Counted only while a
    // playback-smoke session is active, mirroring noteRealSwap()'s own gating.
    quint64 supersededCount = 0;
    quint64 lastSupersededSerial = 0;
    quint64 lastSupersededBySerial = 0;

    // CUDA-PLAYBACK-PRESENT-CADENCE-2 round 1: a "new frame" swap is a real swap whose
    // presentedSerialValid is true and whose presentedSerial is strictly greater than the
    // previous COUNTED new-frame swap's presentedSerial -- i.e. it displays content the
    // owner has not already seen, as opposed to a repaint of a serial already shown (a
    // leftover update() after paint-per-submit already painted, or an expose-event
    // repaint). swapCount above counts every real swap regardless of content; this is the
    // denominator the round-1 acceptance gate (new_frame_swaps/s) actually requires, and it
    // cannot be inflated by upstream frame-dropping the way swapCount/preparedFps can (see
    // the design review, "ratio gameable by upstream shedding").
    quint64 newFrameSwapCount = 0;
    quint64 lastCountedNewFramePresentedSerial = 0;
    double newFrameFirstSwapQpcMs = 0.0;
    double newFrameLastSwapQpcMs = 0.0;
    double newFrameMaxGapMs = 0.0;
    quint64 newFrameMaxGapBeforePresentedSerial = 0;
    quint64 newFrameMaxGapAfterPresentedSerial = 0;
    // Every inter-swap gap between consecutive new-frame swaps, in the order observed --
    // the raw material for a p95, which (unlike count/fps/max) cannot be reduced to a
    // running scalar as each sample arrives. Reset along with every other counter here
    // (GpuDisplayWindow::resetSwapTelemetry() replaces the whole counters struct).
    std::vector<double> newFrameGapSamplesMs;

    // PLAYBACK-VSYNC-DEFAULT-1: wall time of the paint-per-submit swapBuffers() call itself
    // (real swap path 3, the playback path), one sample per timed swap -- where a vsync wait
    // is paid at swap interval 1, and what returns at once at interval 0. Qt's own automatic
    // swap (path 1) and the capture swap (path 2) are not timed.
    std::vector<double> swapCallSamplesMs;
    // wglGetSwapIntervalEXT read with the context current at the session's first timed swap:
    // -1 = the entry point is unavailable, -2 = no timed swap happened.
    int wglSwapIntervalAtFirstTimedSwap = -2;
};

/*! \brief Derived, report-ready swap-cadence numbers for one playback smoke session's
 *  gate line. \c headGapMs/\c tailGapMs are 0 whenever \c swapCount is 0 -- there is no
 *  first/last swap to gap against -- so a caller cannot mistake "no swaps happened" for
 *  "no gap was observed". */
struct GpuWindowSwapTelemetrySummary
{
    quint64 swapCount = 0;
    double swapFps = 0.0;
    double maxGapMs = 0.0;
    quint64 maxGapBeforeSerial = 0;
    quint64 maxGapAfterSerial = 0;
    // Session begin -> first swap, and last swap -> session close ("the gate"). Together
    // with maxGapMs (which only covers gaps BETWEEN swaps) these make a terminal or
    // leading display freeze visible from the summary line alone.
    double headGapMs = 0.0;
    double tailGapMs = 0.0;
    QString firstSwapUtc;
    QString lastSwapUtc;
    // See GpuWindowSwapTelemetryCounters::supersededCount.
    quint64 supersededCount = 0;
    quint64 lastSupersededSerial = 0;
    quint64 lastSupersededBySerial = 0;

    // See GpuWindowSwapTelemetryCounters::newFrameSwapCount. newFrameSwapFps mirrors
    // swapFps's own zero-guard (fewer than 2 new-frame swaps, or a zero-width span,
    // reports 0 rather than dividing by zero). newFrameP95GapMs is 0 whenever fewer than
    // two new-frame swaps were observed -- there is no gap to measure. round 1c: when
    // summarize() is given a real gate timestamp, newFrameMaxGapMs, newFrameSwapFps and
    // (round 1e) newFrameP95GapMs also account for the interval from the LAST new-frame
    // swap through that gate, so a display that stops showing new content cannot report an
    // unchanged, passing cadence just because no further new-frame swap ever arrived to end
    // the stall.
    quint64 newFrameSwapCount = 0;
    double newFrameSwapFps = 0.0;
    double newFrameMaxGapMs = 0.0;
    quint64 newFrameMaxGapBeforePresentedSerial = 0;
    quint64 newFrameMaxGapAfterPresentedSerial = 0;
    double newFrameP95GapMs = 0.0;

    // See GpuWindowSwapTelemetryCounters::swapCallSamplesMs. All 0 when no swap was timed.
    quint64 swapCallCount = 0;
    double swapCallAvgMs = 0.0;
    double swapCallP95Ms = 0.0;
    double swapCallMaxMs = 0.0;
    int wglSwapIntervalAtFirstTimedSwap = -2;
};

/*! \brief What GpuDisplayWindow::swapTelemetrySnapshot() hands back to a caller (e.g. the
 *  playback smoke gate). \c windowActive/\c telemetryEnabled distinguish "this window
 *  wasn't presenting" from "it was, and swapCount is genuinely 0" -- both would otherwise
 *  look identical in \c summary. */
struct GpuWindowSwapTelemetrySnapshot
{
    bool windowActive = false;
    bool telemetryEnabled = false;
    quint64 sessionId = 0;
    GpuWindowSwapTelemetrySummary summary;
};

/*! \brief Pure arithmetic: turn accumulated swap counters into a report-ready summary.
 *  \c sessionBeginQpcMs and \c gateQpcMs are the QPC-ms timestamps of playback-smoke
 *  session begin and session close ("the gate") respectively; both default to 0.0 for
 *  callers that only care about the swap-count/fps/inter-swap-gap fields. */
class GpuWindowSwapTelemetryPolicy
{
public:
    static GpuWindowSwapTelemetrySummary summarize( const GpuWindowSwapTelemetryCounters &counters,
                                                      double sessionBeginQpcMs = 0.0,
                                                      double gateQpcMs = 0.0 )
    {
        GpuWindowSwapTelemetrySummary summary;
        summary.swapCount = counters.swapCount;
        summary.maxGapMs = counters.maxGapMs;
        summary.maxGapBeforeSerial = counters.maxGapBeforeSerial;
        summary.maxGapAfterSerial = counters.maxGapAfterSerial;
        summary.firstSwapUtc = counters.firstSwapUtc;
        summary.lastSwapUtc = counters.lastSwapUtc;
        summary.supersededCount = counters.supersededCount;
        summary.lastSupersededSerial = counters.lastSupersededSerial;
        summary.lastSupersededBySerial = counters.lastSupersededBySerial;

        // fps is over (swapCount - 1) intervals spanning [firstSwapQpcMs, lastSwapQpcMs];
        // a single swap (or two swaps whose QPC timestamps happen to collide at clock
        // granularity) has no interval to measure, so report 0 rather than divide by zero.
        const double spanMs = counters.lastSwapQpcMs - counters.firstSwapQpcMs;
        summary.swapFps =
            ( counters.swapCount > 1 && spanMs > 0.0 )
                ? ( static_cast<double>( counters.swapCount - 1 ) * 1000.0 ) / spanMs
                : 0.0;

        // Guarded by swapCount > 0: with no swaps there is no first/last swap to gap
        // against sessionBeginQpcMs/gateQpcMs, so headGapMs/tailGapMs stay at their
        // zero default rather than reporting the whole session as one giant "gap".
        // qMax(0.0, ...) is defensive -- a swap can never precede the session begin it
        // was reset under, nor follow the gate snapshot taken after it -- but keeps a
        // clock-skew edge case from printing a negative gap.
        if ( counters.swapCount > 0 )
        {
            summary.headGapMs = qMax( 0.0, counters.firstSwapQpcMs - sessionBeginQpcMs );
            summary.tailGapMs = qMax( 0.0, gateQpcMs - counters.lastSwapQpcMs );
        }

        summary.newFrameSwapCount = counters.newFrameSwapCount;
        summary.newFrameMaxGapMs = counters.newFrameMaxGapMs;
        summary.newFrameMaxGapBeforePresentedSerial = counters.newFrameMaxGapBeforePresentedSerial;
        summary.newFrameMaxGapAfterPresentedSerial = counters.newFrameMaxGapAfterPresentedSerial;
        double newFrameSpanMs = counters.newFrameLastSwapQpcMs - counters.newFrameFirstSwapQpcMs;

        // round 1c (sol BLOCKER): the interior gap/fps above only ever look BETWEEN
        // observed new-frame swaps, so a display that shows its last new content and then
        // simply stops -- no more new-frame swaps at all, interior math untouched --
        // reported an unchanged, passing cadence. gateQpcMs > 0.0 marks a caller that
        // actually has a session gate to compare against (swapTelemetrySnapshot() always
        // samples a real monotonic clock there); the interior-only unit tests above that
        // omit it (default 0.0) keep their original count-1/interior-span numbers.
        const bool haveGate = gateQpcMs > 0.0 && counters.newFrameSwapCount > 0;
        double newFrameTailGapMs = 0.0;
        if ( haveGate )
        {
            newFrameTailGapMs = qMax( 0.0, gateQpcMs - counters.newFrameLastSwapQpcMs );
            if ( newFrameTailGapMs > summary.newFrameMaxGapMs )
            {
                summary.newFrameMaxGapMs = newFrameTailGapMs;
                summary.newFrameMaxGapBeforePresentedSerial = counters.lastCountedNewFramePresentedSerial;
                // No new frame ever arrived to close a stall that runs to the gate, so
                // there is no "after" serial to name; 0 is never a valid presented serial
                // (they start at 1), so leave it at that default rather than inventing one.
                summary.newFrameMaxGapAfterPresentedSerial = 0;
            }
            // rate = new frames / (gate - first new frame): the denominator is the whole
            // window the display was supposed to be showing new content in, not just the
            // span between the observed swaps, so the numerator is the full swap count
            // (not count - 1) -- a tail stall shrinks this rate even though it adds no
            // interior interval to divide by.
            newFrameSpanMs = gateQpcMs - counters.newFrameFirstSwapQpcMs;
        }

        summary.newFrameSwapFps =
            haveGate
                ? ( newFrameSpanMs > 0.0
                        ? ( static_cast<double>( counters.newFrameSwapCount ) * 1000.0 ) / newFrameSpanMs
                        : 0.0 )
                : ( ( counters.newFrameSwapCount > 1 && newFrameSpanMs > 0.0 )
                        ? ( static_cast<double>( counters.newFrameSwapCount - 1 ) * 1000.0 ) / newFrameSpanMs
                        : 0.0 );
        // round 1e (sol BLOCKER): p95 mirrors newFrameMaxGapMs/newFrameSwapFps above --
        // include the last-new-frame -> gate interval as one more gap sample, or a display
        // that freezes after a few frames reports a passing p95 forever (the interior
        // samples never change once the stall begins).
        std::vector<double> gapSamplesMs = counters.newFrameGapSamplesMs;
        if ( haveGate )
        {
            gapSamplesMs.push_back( newFrameTailGapMs );
        }
        summary.newFrameP95GapMs = nearestRankP95( gapSamplesMs );

        summary.swapCallCount = counters.swapCallSamplesMs.size();
        summary.wglSwapIntervalAtFirstTimedSwap = counters.wglSwapIntervalAtFirstTimedSwap;
        if ( !counters.swapCallSamplesMs.empty() )
        {
            double totalMs = 0.0;
            for ( const double ms : counters.swapCallSamplesMs )
            {
                totalMs += ms;
                summary.swapCallMaxMs = qMax( summary.swapCallMaxMs, ms );
            }
            summary.swapCallAvgMs = totalMs / static_cast<double>( counters.swapCallSamplesMs.size() );
            summary.swapCallP95Ms = nearestRankP95( counters.swapCallSamplesMs );
        }
        return summary;
    }

    /*! \brief Nearest-rank p95: sort ascending, take the ceil(0.95 * N)-th sample (1-based),
     *  clamped so a 1-sample vector returns that sample rather than reading past the end.
     *  0 for an empty vector. */
    static double nearestRankP95( std::vector<double> samples )
    {
        if ( samples.empty() ) return 0.0;
        std::sort( samples.begin(), samples.end() );
        const std::size_t rank = static_cast<std::size_t>(
            std::ceil( 0.95 * static_cast<double>( samples.size() ) ) );
        const std::size_t index = std::min(
            samples.size() - 1,
            rank == 0 ? static_cast<std::size_t>( 0 ) : rank - 1 );
        return samples[index];
    }

    /*! \brief Pure predicate, extracted so the invariant itself (not just its accumulated
     *  effect on the counters) can be unit-tested without a live GL window: does this real
     *  swap display genuinely NEW content, as opposed to a repaint of a frame already
     *  counted? See GpuWindowSwapTelemetryCounters::newFrameSwapCount. */
    static bool isNewFrameSwap( bool presentedSerialValid,
                                 quint64 presentedSerial,
                                 quint64 previousCountedNewFramePresentedSerial )
    {
        return presentedSerialValid && presentedSerial > previousCountedNewFramePresentedSerial;
    }
};

#endif // GPUWINDOWSWAPTELEMETRY_H
