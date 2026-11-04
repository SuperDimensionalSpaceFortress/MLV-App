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
#include <limits>
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
        // r6 (adversary pass 12 A1, sibling): the absolute due saturates instead of overflowing qint64 (a negative wait is now)
        if( dueInNs < 0 ) dueInNs = 0;
        m_dueNs = nowNs > std::numeric_limits<qint64>::max() - dueInNs ? std::numeric_limits<qint64>::max() : nowNs + dueInNs;
        return m_generation;
    }
    void cancel()
    {
        m_armed = false;
        nextGeneration();
    }
    /*! r6 (adversary pass 10 A1): a new generation for the SAME arm (due time and armed state kept). A failed
     *  SetWaitableTimer re-arm takes one after cancelling the replaced timer, so a message the old wait thread posted with
     *  the generation stored before the attempt is stale; the fallback QTimer delivers the renewed generation. */
    quint32 renew()
    {
        nextGeneration();
        return m_generation;
    }
    /*! True once, for the delivery that belongs to the current arm; the gate is disarmed before the caller fires.
     *  r3 (adversary pass 7 A1): \a postNs >= 0 is when the delivery was POSTED, on the gate's clock. A waitable timer never
     *  signals early, so a delivery posted before the armed due time is an EARLIER arm's signal whose wait thread read the
     *  new generation after a re-arm: it is dropped (the gate stays armed) and the replacement's own message fires, carrying
     *  its own post time. Without this the old signal disarmed the replacement and its stale post time became the lateness
     *  split. \a postNs < 0 (the notifier / qtimer paths carry none) keeps the r2 rule. */
    bool accept( quint32 generation, qint64 nowNs, qint64 postNs = -1 )
    {
        if( !m_armed || generation != m_generation ) return false;
        if( nowNs < m_dueNs - kStaleDeliveryNs ) return false;
        if( postNs >= 0 && postNs < m_dueNs - kStaleDeliveryNs ) return false;
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

/*! r3 (adversary pass 6 A1): what a due wake does. A busy renderer defers the advance to the frameReady / drawFrameReady
 *  paths (the pending advance). An every-frame wake held because the slider's frame has not presented yet does NOT leave
 *  a pending advance: the empty-frameReady and lookahead-drop paths would consume it while that frame's preparation is
 *  still queued, and advance over it. The present that ends re-arms the wake, and its tail advance moves on. */
enum class PlaybackDueWakeOutcome { Advance, DeferBusy, HoldUnpresented };

inline PlaybackDueWakeOutcome playbackDueWakeOutcome( bool frameStillDrawing, bool dropFrameMode,
                                                      long long lastPresentedFrame, int position )
{
    if( frameStillDrawing ) return PlaybackDueWakeOutcome::DeferBusy;
    if( !playbackDueWakeMayAdvance( false, dropFrameMode, lastPresentedFrame, position ) )
        return PlaybackDueWakeOutcome::HoldUnpresented;
    return PlaybackDueWakeOutcome::Advance;
}

inline bool playbackDueWakeOutcomeLeavesPendingAdvance( PlaybackDueWakeOutcome outcome )
{
    return outcome == PlaybackDueWakeOutcome::DeferBusy;
}

/*! r3 (adversary pass 6 A1, siblings): a pending advance taken OUTSIDE the poll tick and the present's own tail advance
 *  (the empty-frameReady path and the lookahead-drop path) obeys the same rule as the wake. In every-frame mode it may
 *  advance only past the frame whose present ended; otherwise the pending flag stays set for the present's tail advance
 *  (or the next poll tick) to take. Whoever set the flag (the poll tick or a busy wake), the rule is the same.
 *  r4 (Dot I1): with the due wake off (MLVAPP_PLAYBACK_DUE_WAKE=0, \a dueWakeEnabled false) this is the base build's
 *  condition exactly (playing, pending, renderer not busy), so the wake-off arm is baseline-equivalent. */
inline bool playbackPendingAdvanceMayBeTaken( bool dueWakeEnabled, bool playChecked, bool pendingAdvance,
                                              bool frameStillDrawing, bool dropFrameMode, long long lastPresentedFrame,
                                              int position )
{
    if( !playChecked || !pendingAdvance ) return false;
    if( !dueWakeEnabled ) return !frameStillDrawing;
    return playbackDueWakeMayAdvance( frameStillDrawing, dropFrameMode, lastPresentedFrame, position );
}

/*! r3: the native message carries its post time. wParam = kNativeWakeTag in the high 32 bits and the generation in the
 *  low 32 bits; lParam = the QueryPerformanceCounter value read just before PostMessage. The GUI thread then splits the
 *  wake's lateness into due-to-post (the timer and the wait thread) and post-to-receive (the GUI thread's queue). */
inline quint64 playbackDueWakePackWParam( quint32 generation, quint32 tag )
{
    return ( static_cast<quint64>( tag ) << 32 ) | static_cast<quint64>( generation );
}
inline quint32 playbackDueWakeWParamGeneration( quint64 wParam ) { return static_cast<quint32>( wParam & 0xFFFFFFFFull ); }
inline quint32 playbackDueWakeWParamTag( quint64 wParam ) { return static_cast<quint32>( wParam >> 32 ); }

/*! r6 (adversary pass 10 A2): the lateness split as the BOUNDS the two wait-thread stamps support, never a point. The post
 *  happened somewhere in [first stamp, PostMessageW's return stamp]: the wait thread can be descheduled before PostMessageW
 *  (pass 9 A1) or after it returns and before the return stamp (pass 10 A2), and no single stamp tells the two apart. So
 *  due-to-post is in [first - due, return - due], post-to-receive is in [receive - return, receive - first], and the
 *  post-call span (return - first) is the attribution uncertainty. Per-sample bounds bound the percentiles too: the k-th
 *  smallest lower bound is <= the k-th smallest true value <= the k-th smallest upper bound. All times in ms, one clock. */
struct PlaybackDueWakeSplitBounds
{
    double dueToPostLoMs;
    double dueToPostHiMs;
    double postToReceiveLoMs;
    double postToReceiveHiMs;
    double postCallMs;
};
inline PlaybackDueWakeSplitBounds playbackDueWakeSplitBounds( double dueAtMs, double firstStampMs, double returnStampMs,
                                                              double receiveMs )
{
    return PlaybackDueWakeSplitBounds { firstStampMs - dueAtMs, returnStampMs - dueAtMs, receiveMs - returnStampMs,
                                        receiveMs - firstStampMs, returnStampMs - firstStampMs };
}

class PlaybackDueWake
{
public:
    /*! The message the wait thread posts: WM_APP (0x8000) + 0x1457. wParam is kNativeWakeTag (high 32 bits) and the
     *  generation (low 32 bits); lParam is the post time in QueryPerformanceCounter ticks (r3). */
    static constexpr unsigned kNativeWakeMessage = 0x8000u + 0x1457u;
    static constexpr quintptr kNativeWakeTag = 0x4D4C5644u;   // 'MLVD'

    enum class Delivery { NativeMessage, Notifier, QtTimer };

    /*! \a owner parents the notifier / timer (the GUI thread's object); \a onFire runs on that thread.
     *  \a preferNativeMessage false skips the wait thread and uses the notifier (r1) path. */
    PlaybackDueWake( QObject *owner, std::function<void()> onFire, bool preferNativeMessage = true );
    ~PlaybackDueWake();
    PlaybackDueWake( const PlaybackDueWake & ) = delete;
    PlaybackDueWake & operator=( const PlaybackDueWake & ) = delete;

    /*! r6 (adversary pass 12 A1): the longest wait arm() schedules, INT_MAX ms (24.8 days, QTimer's int limit). A longer one
     *  (an extreme fps override makes msUntilCredit huge; +infinity, pass 10 A4) is clamped to it, so the ns, 100 ns and int
     *  conversions below never leave their types' ranges. */
    static constexpr double kMaxDueMs = 2147483647.0;
    /*! Fire once, \a dueMs from now (a previous arm is replaced). A no-op after stop(). \a dueMs is clamped to
     *  [0, kMaxDueMs] (negative and NaN are now). */
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

    /*! The path chosen at construction (native-msg, or the notifier / qtimer fallback). */
    Delivery delivery() const;
    /*! The path the LAST arm actually used: qtimer when SetWaitableTimer failed and the arm fell back to a QTimer
     *  (r2 adversary pass 5 A1), else delivery(). hiResActive() is false after such an arm. */
    Delivery lastArmDelivery() const;
    static const char *deliveryName( Delivery delivery );   // "native-msg" | "notifier" | "qtimer"
    /*! Test hook: make arm() treat SetWaitableTimer as failed (the OS failure cannot be injected otherwise). */
    void setWaitableTimerFailsForTest( bool fails );

    /*! A Win32 MSG* seen by the GUI thread: true (consumed) when it is this wake's message, whether it fired or was
     *  dropped as stale. Called by the installed native event filter and the window procedure. */
    bool handleNativeMessage( const void *msg );
    /*! The message-only window the wait thread posts to (0 when not on the native-message path). */
    quintptr nativeWindow() const;
    bool waitThreadRunning() const;
    /*! r3: when the delivery that is firing now was posted, in ms on the QueryPerformanceCounter clock (the base of
     *  mlv_stage_timing_now() * 1000 on Windows). Valid inside the fire callback of a native-message delivery; < 0 on
     *  the notifier / qtimer paths, which carry no post time.
     *  r5 adversary pass 9 A1: it is the time PostMessageW RETURNED (the wait thread's second stamp, handed over through a
     *  seqlock), so a wait-thread pause between the wake and the post counts as due-to-post, not as the GUI queue. A native
     *  delivery the GUI thread took before the wait thread published that stamp has no split: < 0 here, and
     *  lastDeliveryUnsplit() is true (the summary line counts it as post_unsplit, in neither split set). */
    double lastDeliveryPostMs() const;
    /*! r6 (adversary pass 10 A2): the FIRST stamp of the delivery firing now (ms, the same clock), the earliest the post can
     *  have been; >= 0 exactly when lastDeliveryPostMs() is. The post lies in [lastDeliveryPostFirstMs(), lastDeliveryPostMs()],
     *  and the split is reported as those bounds (playbackDueWakeSplitBounds). */
    double lastDeliveryPostFirstMs() const;
    bool lastDeliveryUnsplit() const;
    /*! r7 (adversary passes 13/14 A1): PostMessageW fails when the GUI thread's queue is full (ERROR_NOT_ENOUGH_QUOTA, 10000
     *  posted messages by default), and the waitable timer's auto-reset signal is already consumed by then. The wait thread
     *  retries for up to this long (the stop event still wins); a post that never goes through is handed to the GUI thread
     *  as a LOST post, which deliverLostPost() delivers once, and is counted (postFailures()). */
    static constexpr int kPostRetryBudgetMs = 100;
    /*! GUI thread (MainWindow's poll tick): fire the current arm if the wait thread gave up posting its wake. True when it
     *  fired; a lost post of a cancelled or replaced arm is dropped by the gate, exactly like a stale message. */
    bool deliverLostPost();
    /*! Cumulative, any thread: PostMessageW calls the queue refused (retries included), and posts given up after the retry
     *  budget (each one left a lost post for deliverLostPost()). */
    quint64 postRefusals() const;
    quint64 postFailures() const;
    /*! GUI thread, cumulative: lost posts deliverLostPost() fired. r7 (adversary pass 15 A1): counted BEFORE the callback runs,
     *  and a failure is counted before its lost post is published, so a summary line printed from inside the callback (a
     *  rescued wake that stops Play) already shows both. */
    quint64 postRescued() const;
    /*! GUI thread. r7 (adversary pass 16 A1): the wait thread counts asynchronously, so a Play's cumulative-counter difference
     *  could take a failure of the previous Play's last arm. This returns once the wait thread has handled every timer signal
     *  that came before the call (posted, or retried and counted), waiting at most the retry budget plus kSettleSlackMs.
     *  False on that timeout (the counters may still move). True at once when there is no wait thread. */
    bool settlePostCounters();
    static constexpr int kSettleSlackMs = 1000;
    /*! Test hook: the wait thread's retry budget in ms (0: no retry). */
    void setPostRetryBudgetMsForTest( int ms );
    /*! Stop and join the wait thread (idempotent). After it returns no message is posted and arm() is a no-op. */
    void stop();

    /*! MLVAPP_PLAYBACK_DUE_WAKE=0 turns the due wake off; anything else (or unset) leaves it on. Read on every call. */
    static bool enabledByEnvironment();

private:
    struct Impl;
    std::unique_ptr<Impl> d;
};

#endif // PLAYBACKDUEWAKE_H
