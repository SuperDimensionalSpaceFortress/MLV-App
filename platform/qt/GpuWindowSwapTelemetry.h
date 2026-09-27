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
        return summary;
    }
};

#endif // GPUWINDOWSWAPTELEMETRY_H
