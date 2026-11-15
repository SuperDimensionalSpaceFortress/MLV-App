/*!
 * \file PreviewPaintTiming.h
 * \brief CPU-S2-WINDOWED-GUI-PRESENT-TELEMETRY-1: one GUI-thread sink for the wall time of every
 * preview paint -- the QGraphicsView viewport (GraphicsZoomView), the experimental GL viewport's
 * own Paint (GpuDisplayViewport) and the GL window (GpuDisplayWindow) -- read by
 * playback_smoke.gui_present_summary. Header-only, so each paint site links on its own.
 */

#ifndef PREVIEWPAINTTIMING_H
#define PREVIEWPAINTTIMING_H

#include <QElapsedTimer>
#include <QtGlobal>

namespace preview_paint_timing
{

struct Totals
{
    quint64 paints = 0;
    double sumMs = 0.0;
    double maxMs = 0.0;
};

// GUI thread only: every paint site runs there.
inline Totals &totals()
{
    static Totals value;
    return value;
}

inline void reset()
{
    totals() = Totals();
}

inline int &scopeDepth()
{
    static int value = 0;
    return value;
}

// CPU-S1-PRESENT-INTERVAL-BOUND-1: told each outermost paint's [start, end] on the listener's own clock
// (playback_smoke.critical_hop_summary's viewport paint intervals). Both ends are read at the Scope's own
// boundaries, start before the paint and end before anything else the Scope does after it, so a thread
// descheduled after the paint never moves the interval (r1 fix iteration 1, adversary A3 on 6a6d7ef4: the
// start was end - ms, with end read after the totals). Set only while a playback smoke session is active;
// null otherwise, so a paint pays one null check.
// generation names one installation: the same fn/now/context re-installed by a new session inside a paint is
// a different listener, so that paint is not told (r1 fix iteration 2, advisory A3 on df1b864e).
struct PaintListener
{
    void ( *fn )( void *context, double startS, double endS ) = nullptr;
    double ( *now )( void ) = nullptr;
    void *context = nullptr;
    unsigned long long generation = 0;
};

inline PaintListener &paintListener()
{
    static PaintListener value;
    return value;
}

// A fresh generation per installation (never 0, the cleared listener's).
inline unsigned long long nextPaintListenerGeneration()
{
    static unsigned long long counter = 0;
    return ++counter;
}

// Times one paint, from construction to destruction. Only the outermost Scope counts, so a paintGL
// that opens its own Scope is timed once whether a Paint event (already in a Scope) or a direct
// grabFramebuffer() reached it (GUI-PAINT-DIRECT-GRAB).
class Scope
{
public:
    Scope() : m_outer( scopeDepth()++ == 0 )
    {
        if( !m_outer ) return;
        const PaintListener &listener = paintListener();
        if( listener.fn && listener.now )
        {
            m_listener = listener;
            m_listenerStartS = listener.now();
        }
        m_timer.start();
    }
    ~Scope()
    {
        --scopeDepth();
        if( !m_outer ) return;
        // Heard only by the listener that saw the start (a session that ended or began inside the paint is not told).
        const PaintListener &listener = paintListener();
        const bool heard = m_listener.fn && listener.fn == m_listener.fn && listener.context == m_listener.context
                        && listener.now == m_listener.now && listener.generation == m_listener.generation;
        const double endS = heard ? m_listener.now() : 0.0;
        const double ms = static_cast<double>( m_timer.nsecsElapsed() ) / 1000000.0;
        Totals &value = totals();
        ++value.paints;
        value.sumMs += ms;
        if( ms > value.maxMs ) value.maxMs = ms;
        if( heard ) m_listener.fn( m_listener.context, m_listenerStartS, endS );
    }
    Scope( const Scope & ) = delete;
    Scope &operator=( const Scope & ) = delete;

private:
    const bool m_outer;
    QElapsedTimer m_timer;
    PaintListener m_listener;
    double m_listenerStartS = 0.0;
};

} // namespace preview_paint_timing

#endif // PREVIEWPAINTTIMING_H
