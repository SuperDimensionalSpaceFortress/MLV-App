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
 * (CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, Windows 10 1803+). If the high-resolution flag is refused, a plain waitable
 * timer is used and the timer resolution is raised to 1 ms for the duration of Play (timeBeginPeriod(1) /
 * timeEndPeriod(1), winmm loaded at run time so the link line is unchanged). Elsewhere it is a single-shot
 * Qt::PreciseTimer rounded UP to whole ms.
 *
 * r2 delivery (Delivery::NativeMessage): r1 delivered the timer through a QWinEventNotifier and measured the wake
 * 10.1-11.7 ms late (late_p50, hires=1): the notifier is signalled from a thread-pool wait callback and reaches the
 * GUI thread as a Qt posted event, whose send path is SetTimer-backed when the GUI is busy (qeventdispatcher_win_p.h
 * sendPostedEventsTimerId). Now a dedicated wait thread, owned by this object, waits on the timer and a stop event
 * and PostMessage()s kNativeWakeMessage to a message-only window this object owns on the GUI thread. The GUI thread
 * takes it through a QAbstractNativeEventFilter installed on the application (the window's own procedure handles it
 * too, for a modal loop that dispatches without the Qt filter), so no Qt posted event is involved. If the thread or
 * the window cannot be created, the r1 notifier path is the fallback (Delivery::Notifier, named in telemetry).
 *
 * Lifetime: stop() (and the destructor, which calls it first) signals the stop event and joins the wait thread
 * BEFORE the window is destroyed, so no message is posted to a dead window. A message already queued when the wake
 * is cancelled, re-armed or stopped carries an old generation and is a no-op on the GUI thread (PlaybackDueWakeGate).
 *
 * It only says "now"; MainWindow decides what the wake may do (the pace guard still caps every advance).
 */

#ifndef PLAYBACKDUEWAKE_H
#define PLAYBACKDUEWAKE_H

#include <QtGlobal>

#include <functional>
#include <memory>

class QObject;

/*! The stale-delivery gate, pure (GUI thread only). Every arm() and cancel() starts a new generation; a delivery is
 *  taken only when it carries the current generation, the wake is armed, and it is not early for the armed due time. */
class PlaybackDueWakeGate
{
public:
    /*! A delivery more than this before the armed due time is early: the wait thread read the new generation after
     *  an EARLIER arm's timer signal (the GUI re-armed in between). It is dropped and the re-armed timer delivers
     *  again. Waitable timers never fire early, so the tolerance only absorbs clock-read skew (adversary r1 A3). */
    static constexpr qint64 kStaleDeliveryNs = 50000;

    quint32 arm( qint64 nowNs, qint64 dueInNs )
    {
        nextGeneration();
        m_armed = true;
        m_dueNs = nowNs + dueInNs;
        return m_generation;
    }
    void cancel()
    {
        m_armed = false;
        nextGeneration();
    }
    /*! True once, for the delivery that belongs to the current arm; the gate is disarmed before the caller fires. */
    bool accept( quint32 generation, qint64 nowNs )
    {
        if( !m_armed || generation != m_generation ) return false;
        if( nowNs < m_dueNs - kStaleDeliveryNs ) return false;
        m_armed = false;
        return true;
    }
    bool armed() const { return m_armed; }
    quint32 generation() const { return m_generation; }

private:
    void nextGeneration()
    {
        ++m_generation;
        if( m_generation == 0 ) ++m_generation;   // 0 is never a live generation
    }
    quint32 m_generation = 0;
    bool m_armed = false;
    qint64 m_dueNs = 0;
};

/*! adversary r1 pass 3 A1: may the due wake advance the playback position now?
 *  - never while the renderer is busy (the frameReady / drawFrameReady paths consume the pending advance);
 *  - in every-frame mode, only past the frame that was PRESENTED: a frame the render thread has published as Ready but
 *    whose frameReady is still unsent does not count as busy, and an advance over it would request N+1 while N is
 *    unpresented, and latest-ready acquisition would then show N+1 and skip N. Drop-frame mode may step over frames
 *    by design. \a lastPresentedFrame < 0 means nothing was presented in this Play yet (no gate). */
inline bool playbackDueWakeMayAdvance( bool frameStillDrawing, bool dropFrameMode, long long lastPresentedFrame,
                                       int position )
{
    if( frameStillDrawing ) return false;
    if( dropFrameMode ) return true;
    return lastPresentedFrame < 0 || lastPresentedFrame == static_cast<long long>( position );
}

class PlaybackDueWake
{
public:
    /*! The message the wait thread posts: WM_APP (0x8000) + 0x1457. wParam is the generation, lParam kNativeWakeTag. */
    static constexpr unsigned kNativeWakeMessage = 0x8000u + 0x1457u;
    static constexpr quintptr kNativeWakeTag = 0x4D4C5644u;   // 'MLVD'

    enum class Delivery { NativeMessage, Notifier, QtTimer };

    /*! \a owner parents the notifier / timer (the GUI thread's object); \a onFire runs on that thread.
     *  \a preferNativeMessage false skips the wait thread and uses the notifier (r1) path. */
    PlaybackDueWake( QObject *owner, std::function<void()> onFire, bool preferNativeMessage = true );
    ~PlaybackDueWake();
    PlaybackDueWake( const PlaybackDueWake & ) = delete;
    PlaybackDueWake & operator=( const PlaybackDueWake & ) = delete;

    /*! Fire once, \a dueMs from now (a previous arm is replaced). A no-op after stop(). */
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

    Delivery delivery() const;
    static const char *deliveryName( Delivery delivery );   // "native-msg" | "notifier" | "qtimer"

    /*! A Win32 MSG* seen by the GUI thread: true (consumed) when it is this wake's message, whether it fired or was
     *  dropped as stale. Called by the installed native event filter and the window procedure. */
    bool handleNativeMessage( const void *msg );
    /*! The message-only window the wait thread posts to (0 when not on the native-message path). */
    quintptr nativeWindow() const;
    bool waitThreadRunning() const;
    /*! Stop and join the wait thread (idempotent). After it returns no message is posted and arm() is a no-op. */
    void stop();

    /*! MLVAPP_PLAYBACK_DUE_WAKE=0 turns the due wake off; anything else (or unset) leaves it on. Read on every call. */
    static bool enabledByEnvironment();

private:
    struct Impl;
    std::unique_ptr<Impl> d;
};

#endif // PLAYBACKDUEWAKE_H
