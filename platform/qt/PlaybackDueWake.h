/*!
 * \file PlaybackDueWake.h
 * \brief HFR-NATIVE-M16-1457-GUI-PRESENT-CADENCE-1: a one-shot "wake the GUI thread in X ms" for the paced advance.
 *
 * Measured on Bachelor CUDA (Qt 6.10.2, paced M16-1457 native 59.94): after a present the next advance waited for
 * the next playback poll tick, which landed 10.77 ms (fullscreen) / 14.92 ms (windowed, a ~15.6 ms grid) p50 after
 * presentPlaybackPreparedFrame returned, never 8 ms or less, although the next frame was ready 2.5 ms after the
 * present. A QTimer cannot do better there: Qt 6 on Windows runs every non-zero timer on the Win32 SetTimer /
 * SetCoalescableTimer path, which clamps to USER_TIMER_MINIMUM (10 ms) and rounds to the process timer resolution
 * (15.625 ms unless something raised it). So the due time is armed on a high-resolution waitable timer
 * (CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, Windows 10 1803+) and delivered on the GUI thread through a
 * QWinEventNotifier. If the high-resolution flag is refused, a plain waitable timer is used and the timer resolution
 * is raised to 1 ms for the duration of Play (timeBeginPeriod(1) / timeEndPeriod(1), winmm loaded at run time so the
 * link line is unchanged). Elsewhere it is a single-shot Qt::PreciseTimer rounded UP to whole ms.
 *
 * It only says "now"; MainWindow decides what the wake may do (the pace guard still caps every advance).
 */

#ifndef PLAYBACKDUEWAKE_H
#define PLAYBACKDUEWAKE_H

#include <functional>
#include <memory>

class QObject;

class PlaybackDueWake
{
public:
    /*! \a owner parents the notifier / timer (the GUI thread's object); \a onFire runs on that thread. */
    PlaybackDueWake( QObject *owner, std::function<void()> onFire );
    ~PlaybackDueWake();
    PlaybackDueWake( const PlaybackDueWake & ) = delete;
    PlaybackDueWake & operator=( const PlaybackDueWake & ) = delete;

    /*! Fire once, \a dueMs from now (a previous arm is replaced). */
    void arm( double dueMs );
    /*! Forget a pending wake; a delivery already queued for it is dropped. */
    void cancel();
    bool armed() const;

    /*! Play started / stopped: only the fallback (a plain waitable timer) raises the timer resolution, and only in
     *  between. Idempotent. */
    void beginPlay();
    void endPlay();

    /*! 1 when the wake runs on a high-resolution waitable timer, or on a plain one with the resolution raised. */
    bool hiResActive() const;

private:
    struct Impl;
    std::unique_ptr<Impl> d;
};

#endif // PLAYBACKDUEWAKE_H
