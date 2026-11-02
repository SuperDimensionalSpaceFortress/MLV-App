/*!
 * \file GpuPresentTailWindow.h
 * \brief PLAYBACK-PRESENT-TAIL-TIMING-1: the GUI-thread tail of one GpuDisplayWindow recon present, and the window
 * from a path-3 swap return to the next recon setup start. Header-only and Qt-core-only, so console_tests drives the
 * window's lifecycle (GpuDisplayWindow itself needs a GL build). Every member takes the caller's clock reading.
 */

#ifndef GPUPRESENTTAILWINDOW_H
#define GPUPRESENTTAILWINDOW_H

#include <QtGlobal>

#include <type_traits>

/* The GUI-thread tail of one recon present, plus the window from the PREVIOUS present's swap return to THIS
 * present's setup start. CPU wall time only. */
struct GpuPresentTailTiming
{
    bool painted = false;               // paint-per-submit (swap path 3) ran this call
    double post_paint_ms = 0.0;         // the paintGL() call before the path-3 swap
    double post_swap_ms = 0.0;          // the path-3 swapBuffers call (== swapCallMs)
    bool prev_known = false;            // a path-3 swap opened the window and no setup has closed it since
    double prev_post_paint_ms = 0.0;
    double prev_post_swap_ms = 0.0;
    double swap_return_to_setup_ms = 0.0;
    int qt_paint_swaps = 0;             // swap path 1 inside the window (smoke telemetry only)
    double qt_paint_swap_max_ms = 0.0;  // paintUnderGL -> frameSwapped (smoke only)
    int grab_swaps = 0;                 // swap path 2 inside the window (smoke only)
    bool gui_idle_known = false;        // smoke only: the idle probe is connected and no block was open at setup
    double gui_idle_ms = 0.0;           // event loop blocked inside the window
    // r3 (B1): a smoke runner drives the loop with non-blocking processEvents, so the dispatcher never blocks and its
    // idle reads 0 by construction; the runner's own sleep is the GUI thread's yield there. Both yields are counted
    // so a window with none observed reads as unmeasured, never as busy. busy = window - idle - runner sleep.
    int gui_idle_waits = 0;             // smoke only: dispatcher block/wake pairs closed inside the window
    double runner_sleep_ms = 0.0;       // smoke only: MainWindow::sleepGuiThreadForSmokeRunner inside the window
    int runner_sleeps = 0;
};
static_assert(std::is_trivially_copyable<GpuPresentTailTiming>::value, "no per-frame allocation");

/* The window state GpuDisplayWindow keeps between presents. POD only; GUI-thread only. */
struct GpuPresentTailWindow
{
    qint64 swapReturnNs = -1;   // >= 0 while the window is open
    qint64 blockStartNs = -1;   // >= 0 while a dispatcher block inside the window has not woken
    qint64 idleNs = 0;
    int idleWaits = 0;
    qint64 runnerSleepNs = 0;
    int runnerSleeps = 0;
    int qtPaintSwaps = 0;
    qint64 qtPaintSwapMaxNs = 0;
    qint64 qtPaintStartNs = -1; // >= 0 from a path-1 paintUnderGL until its frameSwapped (r4, G2)
    int grabSwaps = 0;
    double prevPaintMs = 0.0;
    double prevSwapMs = 0.0;

    bool isOpen() const { return swapReturnNs >= 0; }

    // A path-3 swap returned: a fresh window opens, every count from the previous one gone. r4 (G2): that includes an
    // unswapped path-1 paint stamp, so a paint whose frameSwapped never came is never paired with a later window's swap.
    void open( qint64 nowNs, double paintMs, double swapMs )
    {
        *this = GpuPresentTailWindow();
        swapReturnNs = nowNs;
        prevPaintMs = paintMs;
        prevSwapMs = swapMs;
    }
    // The dispatcher's aboutToBlock / awake (QEventDispatcherWin32 only; see GpuDisplayWindow's constructor).
    void blocked( qint64 nowNs )
    {
        if( isOpen() ) blockStartNs = nowNs;
    }
    void awake( qint64 nowNs )
    {
        if( blockStartNs < 0 ) return;
        idleNs += nowNs - blockStartNs;
        ++idleWaits;
        blockStartNs = -1;
    }
    // The smoke runner slept on this thread for sleptNs. No present runs during the sleep (same thread), so a window
    // open now was open for the whole sleep.
    void runnerSlept( qint64 sleptNs )
    {
        if( !isOpen() ) return;
        runnerSleepNs += sleptNs;
        ++runnerSleeps;
    }
    void qtPaintSwap( qint64 pairNs )
    {
        if( !isOpen() ) return;
        ++qtPaintSwaps;
        if( pairNs > qtPaintSwapMaxNs ) qtPaintSwapMaxNs = pairNs;
    }
    // r4 (G2): paintUnderGL stamps the path-1 paint start; the next frameSwapped consumes it as one pair.
    void qtPaintStarted( qint64 nowNs )
    {
        qtPaintStartNs = nowNs;
    }
    void qtPaintSwapped( qint64 nowNs )
    {
        if( qtPaintStartNs < 0 ) return;
        const qint64 pairNs = nowNs - qtPaintStartNs;
        qtPaintStartNs = -1;
        qtPaintSwap( pairNs );
    }
    // Swap-telemetry session start and stop: a stamp never pairs across a session boundary.
    void dropQtPaintStamp()
    {
        qtPaintStartNs = -1;
    }
    void grabSwap()
    {
        if( isOpen() ) ++grabSwaps;
    }
    // r3 (G1): a present refused before its setup start drops the window, so the next setup's window never spans
    // the refused call; that setup reads prev_known=false (bucket unknown).
    void abandon()
    {
        swapReturnNs = -1;
        blockStartNs = -1;
    }
    // The next recon setup start closes the window into tail.
    void close( qint64 nowNs, bool idleProbe, GpuPresentTailTiming &tail )
    {
        if( !isOpen() ) return;
        tail.prev_known = true;
        tail.prev_post_paint_ms = prevPaintMs;
        tail.prev_post_swap_ms = prevSwapMs;
        tail.swap_return_to_setup_ms = static_cast<double>( nowNs - swapReturnNs ) / 1000000.0;
        tail.qt_paint_swaps = qtPaintSwaps;
        tail.qt_paint_swap_max_ms = static_cast<double>( qtPaintSwapMaxNs ) / 1000000.0;
        tail.grab_swaps = grabSwaps;
        // A block still open here never reported its wake (this thread is running, not blocked): idle is not known.
        tail.gui_idle_known = idleProbe && blockStartNs < 0;
        tail.gui_idle_ms = static_cast<double>( idleNs ) / 1000000.0;
        tail.gui_idle_waits = idleWaits;
        tail.runner_sleep_ms = static_cast<double>( runnerSleepNs ) / 1000000.0;
        tail.runner_sleeps = runnerSleeps;
        swapReturnNs = -1;
        blockStartNs = -1;
    }
};
static_assert(std::is_trivially_copyable<GpuPresentTailWindow>::value, "no per-frame allocation");

#endif // GPUPRESENTTAILWINDOW_H
