/*!
 * \file GuiPresentGapIdle.h
 * \brief CPU-S2-WINDOWED-GUI-PRESENT-TELEMETRY-1: how one open present-to-next-result gap on the GUI
 * thread splits into dispatcher idle, smoke-runner sleep, preview paint and other busy
 * (playback_smoke.gui_present_summary). Header-only and Qt-core-only, so console_tests drives it.
 */

#ifndef GUIPRESENTGAPIDLE_H
#define GUIPRESENTGAPIDLE_H

#include <QtGlobal>

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

} // namespace gui_present_gap

#endif // GUIPRESENTGAPIDLE_H
