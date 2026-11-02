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

// Times one paint, from construction to destruction. Only the outermost Scope counts, so a paintGL
// that opens its own Scope is timed once whether a Paint event (already in a Scope) or a direct
// grabFramebuffer() reached it (GUI-PAINT-DIRECT-GRAB).
class Scope
{
public:
    Scope() : m_outer( scopeDepth()++ == 0 ) { if( m_outer ) m_timer.start(); }
    ~Scope()
    {
        --scopeDepth();
        if( !m_outer ) return;
        const double ms = static_cast<double>( m_timer.nsecsElapsed() ) / 1000000.0;
        Totals &value = totals();
        ++value.paints;
        value.sumMs += ms;
        if( ms > value.maxMs ) value.maxMs = ms;
    }
    Scope( const Scope & ) = delete;
    Scope &operator=( const Scope & ) = delete;

private:
    const bool m_outer;
    QElapsedTimer m_timer;
};

} // namespace preview_paint_timing

#endif // PREVIEWPAINTTIMING_H
