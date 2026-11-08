/*!
 * \file GuiPresentGapIdle.h
 * \brief CPU-S2-WINDOWED-GUI-PRESENT-TELEMETRY-1: how one open present-to-next-result gap on the GUI
 * thread splits into dispatcher idle, smoke-runner sleep, preview paint and other busy
 * (playback_smoke.gui_present_summary). Header-only and Qt-core-only, so console_tests drives it.
 */

#ifndef GUIPRESENTGAPIDLE_H
#define GUIPRESENTGAPIDLE_H

#include <QtGlobal>

#include <cmath>

namespace gui_present_gap
{

// The GUI dispatcher's waits inside one open gap. A wait is idle from aboutToBlock until the FIRST
// of: awake, or the first event the GUI thread dispatches after it (MainWindow::eventFilter, an
// application event filter, which QCoreApplication::notify runs before delivering any event to an
// object of the main thread). QEventDispatcherGlib::processEvents emits awake only after
// g_main_context_iteration() has dispatched the ready timers, posted events and window-system
// events, so ending a wait at awake alone counted that GUI work as idle (GUI-IDLE-LATE-AWAKE).
// QEventDispatcherWin32 emits awake straight after MsgWaitForMultipleObjectsEx, before it
// dispatches anything, so on Windows awake still ends the wait and the dispatch is a no-op.
struct IdleWaits
{
    qint64 blockStartNs = -1;
    qint64 idleNs = 0;

    void blocked( qint64 nowNs ) { blockStartNs = nowNs; }
    void ended( qint64 nowNs )
    {
        if( blockStartNs < 0 ) return;
        idleNs += nowNs - blockStartNs;
        blockStartNs = -1;
    }
};

struct Split
{
    double gapMs = 0.0;
    double idleMs = 0.0;
    double runnerSleepMs = 0.0;
    double busyMs = 0.0;
    double otherBusyMs = 0.0;
};

// busy = wall - idle - runner sleep; other busy = busy - preview paint. Each term is clamped so
// busy and other busy stay within [0, gap].
inline Split split( qint64 gapNs, qint64 idleNs, qint64 runnerSleepNs, double paintMs )
{
    Split s;
    s.gapMs = static_cast<double>( gapNs ) / 1000000.0;
    s.idleMs = qMin( s.gapMs, static_cast<double>( idleNs ) / 1000000.0 );
    s.runnerSleepMs = qMin( s.gapMs - s.idleMs, static_cast<double>( runnerSleepNs ) / 1000000.0 );
    s.busyMs = s.gapMs - s.idleMs - s.runnerSleepMs;
    s.otherBusyMs = qMax( 0.0, s.busyMs - qMax( 0.0, paintMs ) );
    return s;
}

// CPU-S1-RECON-UNDER-LOOK-PACE-LEVER-1 (Part B): "other busy" split by the GUI handlers that run inside
// an open gap. A span is one handler call; its EXCLUSIVE time is its wall time less the paint, the
// runner sleep and the dispatcher idle inside it (each already reported on its own) and less the
// exclusive time of the spans nested in it, so a handler that calls another is never counted twice.
// A span counts only when the SAME gap is open at its start and at its end (the gap serial): a span
// that a gap opens or closes inside is dropped, and the gap's other busy keeps it as unattributed.
enum class OtherBusySpan : int
{
    DrawFrameReady = 0,   // MainWindow::drawFrameReady (render-ready slot, prep hand-off)
    TimerFrame,           // MainWindow::timerFrameEvent (playback timer tick / advance)
    PlaybackHandling,     // MainWindow::playbackHandling (position advance, drop-frame pacing)
    DrawFrame,            // MainWindow::drawFrame (render request)
    SliderPosition,       // MainWindow::on_horizontalSliderPosition_valueChanged (position / timecode UI)
    // Not a span: the audio sync runs in finishPresentedFrame, inside the present (before the gap opens).
    Count
};

inline const char *otherBusySpanName( OtherBusySpan span )
{
    switch( span )
    {
    case OtherBusySpan::DrawFrameReady: return "draw_frame_ready";
    case OtherBusySpan::TimerFrame: return "timer_frame";
    case OtherBusySpan::PlaybackHandling: return "playback_handling";
    case OtherBusySpan::DrawFrame: return "draw_frame";
    case OtherBusySpan::SliderPosition: return "slider_position";
    case OtherBusySpan::Count: break;
    }
    return "unknown";
}

// One clock reading of the four quantities a span needs.
struct SpanClock
{
    quint64 gapSerial = 0;     // 0 = no gap open
    qint64 nowNs = 0;
    qint64 idleNs = 0;         // IdleWaits::idleNs of the open gap
    qint64 runnerSleepNs = 0;  // the open gap's runner sleep so far
    double paintMs = 0.0;      // the paint sink's running sum
};

class OtherBusySpans
{
public:
    static constexpr int kCount = static_cast<int>( OtherBusySpan::Count );
    static constexpr int kMaxDepth = 16;

    // Per-gap exclusive ms; folded into the session sums when the gap closes.
    double gapMs[kCount] = {};
    double sumMs[kCount] = {};

    void begin( const SpanClock &c )
    {
        if( m_depth < kMaxDepth )
        {
            m_stack[m_depth] = Frame{ c, 0.0 };
        }
        ++m_depth;
    }

    // Returns the exclusive ms charged (0 when dropped).
    double end( OtherBusySpan span, const SpanClock &c )
    {
        if( m_depth <= 0 ) return 0.0;
        --m_depth;
        if( m_depth >= kMaxDepth ) return 0.0;
        const Frame f = m_stack[m_depth];
        const bool sameGap = f.start.gapSerial != 0 && f.start.gapSerial == c.gapSerial;
        double inclusiveMs = 0.0;
        if( sameGap )
        {
            inclusiveMs = static_cast<double>( ( c.nowNs - f.start.nowNs )
                                               - ( c.idleNs - f.start.idleNs )
                                               - ( c.runnerSleepNs - f.start.runnerSleepNs ) ) / 1000000.0
                - ( c.paintMs - f.start.paintMs );
            // r3 (adversary A1 on f9f92e09, A4 on e7e5271e): a NaN or infinite paint reading clamps to 0 too.
            if( !( inclusiveMs > 0.0 ) || !std::isfinite( inclusiveMs ) ) inclusiveMs = 0.0;
        }
        double exclusiveMs = inclusiveMs - f.childMs;
        if( !( exclusiveMs > 0.0 ) || !std::isfinite( exclusiveMs ) ) exclusiveMs = 0.0;
        if( m_depth > 0 && m_depth - 1 < kMaxDepth ) m_stack[m_depth - 1].childMs += inclusiveMs;
        if( !sameGap ) return 0.0;
        // CPU-S1-PACE-NEXT-LEVER-1 (#770 r3 advisory A1): OtherBusySpan::Count, or any value outside
        // the enum, pops its frame but is charged to no span (it would index past gapMs).
        const int index = static_cast<int>( span );
        if( index < 0 || index >= kCount ) return 0.0;
        gapMs[index] += exclusiveMs;
        return exclusiveMs;
    }

    // A gap closed with this other busy: fold its spans into the session sums (each capped so the
    // named spans never exceed the gap's other busy) and return the unattributed remainder.
    double closeGap( double otherBusyMs )
    {
        // CPU-S1-PACE-NEXT-LEVER-1 (adversary A1 on 9bfdf8e0): a negative budget is no budget; it
        // would make the scale negative and subtract the named spans from the session sums.
        // r3 (adversary A1 on f9f92e09, A4 on e7e5271e): a NaN or infinite budget is no budget either.
        if( !( otherBusyMs > 0.0 ) || !std::isfinite( otherBusyMs ) ) otherBusyMs = 0.0;
        double named = 0.0;
        for( int i = 0; i < kCount; ++i ) named += gapMs[i];
        const double scale = ( named > otherBusyMs && named > 0.0 ) ? otherBusyMs / named : 1.0;
        double charged = 0.0;
        for( int i = 0; i < kCount; ++i )
        {
            sumMs[i] += gapMs[i] * scale;
            charged += gapMs[i] * scale;
            gapMs[i] = 0.0;
        }
        const double rest = otherBusyMs - charged;
        unattributedSumMs += rest > 0.0 ? rest : 0.0;
        return rest > 0.0 ? rest : 0.0;
    }

    // A gap opened: spans of a previous gap that never closed are not carried into it.
    void openGap()
    {
        for( int i = 0; i < kCount; ++i ) gapMs[i] = 0.0;
    }

    void resetSession()
    {
        for( int i = 0; i < kCount; ++i ) { gapMs[i] = 0.0; sumMs[i] = 0.0; }
        unattributedSumMs = 0.0;
    }

    double unattributedSumMs = 0.0;
    int depth() const { return m_depth; }

private:
    struct Frame
    {
        SpanClock start;
        double childMs;
    };
    Frame m_stack[kMaxDepth] = {};
    int m_depth = 0;
};

} // namespace gui_present_gap

#endif // GUIPRESENTGAPIDLE_H
