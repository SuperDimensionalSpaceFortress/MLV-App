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

#include <QString>
#include <QtGlobal>

#include <array>
#include <cstddef>
#include <functional>
#include <limits>
#include <memory>

class QObject;

/*! HFR-NATIVE-M16-1457-FULLSCREEN-S1S2-PRESENT-GAP-1: what the GUI thread was doing at a given instant, read back from a
 *  bounded trace of its own transitions (GUI thread only; one clock, QueryPerformanceCounter ms, the clock the wake's post
 *  stamps use). A transition names the state ENTERED:
 *  - DispatcherWait: QAbstractEventDispatcher::aboutToBlock (the dispatcher is about to wait for messages);
 *  - RunnerSleep:    the --gui-smoke-playback runner's own sleep on the GUI thread begins (MainWindow::sleepGuiThreadForSmokeRunner);
 *  - Running:        QAbstractEventDispatcher::awake, or the runner's sleep ended.
 *  at( t ) is the state the latest transition at or before t entered, and (for the two sleeping states) when the GUI thread
 *  next left it. Memory is a fixed ring of kCapacity transitions; consecutive Running transitions are folded into the first
 *  (the dispatcher emits awake at the start of every processEvents too), and an instant older than the ring's oldest kept
 *  transition is Unknown, never guessed. */
class PlaybackGuiSleepTrace
{
public:
    enum class State { Running, DispatcherWait, RunnerSleep, Unknown };
    static constexpr std::size_t kCapacity = 256;

    struct At
    {
        State state;
        bool hasLeft;    // a sleeping state the GUI thread has left since (a later transition is kept)
        double leftMs;   // ...when it left it (the first later transition); meaningless unless hasLeft
    };

    void note( State entered, double ms )
    {
        if( entered == State::Unknown ) return;
        if( m_count > 0 && entered == State::Running && m_ring[slot( m_count - 1 )].state == State::Running ) return;
        m_ring[slot( m_count )] = Entry { ms, entered };
        ++m_count;
    }

    At at( double ms ) const
    {
        const std::size_t kept = m_count < kCapacity ? m_count : kCapacity;
        const std::size_t first = m_count - kept;
        // the latest transition at or before ms (newest first); the one after it is when that state was left
        for( std::size_t i = m_count; i > first; --i )
        {
            const Entry &entry = m_ring[slot( i - 1 )];
            if( entry.ms > ms ) continue;
            if( entry.state == State::Running ) return { State::Running, false, 0.0 };
            if( i < m_count ) return { entry.state, true, m_ring[slot( i )].ms };
            return { entry.state, false, 0.0 };
        }
        return { State::Unknown, false, 0.0 };   // nothing kept that old (or nothing traced yet)
    }

    void reset() { m_count = 0; }
    std::size_t transitions() const { return m_count; }

    static bool sleeping( State state ) { return state == State::DispatcherWait || state == State::RunnerSleep; }

private:
    struct Entry
    {
        double ms;
        State state;
    };
    static std::size_t slot( std::size_t index ) { return index % kCapacity; }
    std::array<Entry, kCapacity> m_ring {};
    std::size_t m_count = 0;
};

/*! FULLSCREEN-S1S2-PRESENT-GAP-1: one fired wake's post, classified by what the GUI thread was doing when it was posted.
 *  - Asleep: in the dispatcher's wait or the smoke runner's sleep (runnerSleep says which). postToAwakeMs = when it left
 *    that sleep - the post; awakeToReceiveMs = the receive - when it left.
 *  - Awake: running (occupancy). awakeToReceiveMs = the receive - the post; there is no post-to-awake part.
 *  - Unknown: the trace was off or did not reach back that far, or a sleep that had not ended by the receive. */
struct PlaybackDueWakePostClass
{
    enum class Kind { Asleep, Awake, Unknown };
    Kind kind;
    bool runnerSleep;
    double postToAwakeMs;
    double awakeToReceiveMs;
};

inline PlaybackDueWakePostClass playbackDueWakeClassifyPost( const PlaybackGuiSleepTrace::At &at, double postMs, double receiveMs )
{
    if( PlaybackGuiSleepTrace::sleeping( at.state ) )
    {
        if( !at.hasLeft || at.leftMs > receiveMs || at.leftMs < postMs )
            return { PlaybackDueWakePostClass::Kind::Unknown, false, 0.0, 0.0 };
        return { PlaybackDueWakePostClass::Kind::Asleep, at.state == PlaybackGuiSleepTrace::State::RunnerSleep,
                 at.leftMs - postMs, receiveMs - at.leftMs };
    }
    if( at.state == PlaybackGuiSleepTrace::State::Running )
        return { PlaybackDueWakePostClass::Kind::Awake, false, 0.0, receiveMs - postMs };
    return { PlaybackDueWakePostClass::Kind::Unknown, false, 0.0, 0.0 };
}

/*! HFR-NATIVE-M16-1457-FULLSCREEN-S1S2-PRESENT-GAP-1: how the wait thread hands the wake to the GUI thread.
 *  - SendNotify (default): SendNotifyMessageW. A cross-thread sent message sets QS_SENDMESSAGE, which a message wait that
 *    names it returns for, and it is dispatched to the window procedure inside the thread's next PeekMessage / GetMessage
 *    whatever their filter (dueWakeWindowProc -> handleNativeMessage).
 *  - Post: PostMessageW, the r2..r8 path byte for byte (MLVAPP_PLAYBACK_DUE_WAKE_DELIVERY=post, the isolation arm).
 *  - Notifier: r1's QWinEventNotifier, no wait thread (MLVAPP_PLAYBACK_DUE_WAKE_DELIVERY=notifier). */
enum class PlaybackDueWakeDeliveryMode { SendNotify, Post, Notifier };

/*! The MLVAPP_PLAYBACK_DUE_WAKE_DELIVERY parse: "post" / "notifier" (trimmed, any case); anything else, or unset, is
 *  SendNotify. Pure: MainWindow reads the variable once and hands the text here. */
inline PlaybackDueWakeDeliveryMode playbackDueWakeDeliveryModeFromText( const QString &text )
{
    const QString value = text.trimmed();
    if( value.compare( QStringLiteral( "post" ), Qt::CaseInsensitive ) == 0 ) return PlaybackDueWakeDeliveryMode::Post;
    if( value.compare( QStringLiteral( "notifier" ), Qt::CaseInsensitive ) == 0 ) return PlaybackDueWakeDeliveryMode::Notifier;
    return PlaybackDueWakeDeliveryMode::SendNotify;
}

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
        if( !wouldAccept( generation, nowNs, postNs ) ) return false;
        m_armed = false;
        return true;
    }
    /*! r2 adversary pass 5 A1: accept()'s verdict without disarming -- the runner sleep holds only a wake this would accept. */
    bool wouldAccept( quint32 generation, qint64 nowNs, qint64 postNs = -1 ) const
    {
        if( !m_armed || generation != m_generation ) return false;
        if( nowNs < m_dueNs - kStaleDeliveryNs ) return false;
        if( postNs >= 0 && postNs < m_dueNs - kStaleDeliveryNs ) return false;
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

/*! r8 (adversary pass 17 A2): the next bounded wait, in ms, computed from a clock read taken IMMEDIATELY before the wait it
 *  feeds: the time left to \a deadlineMs, clamped to \a sliceMs; 0 once \a nowMs has reached the deadline (no wait, and no
 *  further attempt). A thread preempted between that read and the wait still waits at most one slice past its resumption,
 *  never a stale whole remainder (the pass 17 sibling: a 1100 ms relative wait started after its deadline). */
inline quint32 playbackDueWakeSliceWaitMs( quint64 nowMs, quint64 deadlineMs, quint32 sliceMs )
{
    if( nowMs >= deadlineMs ) return 0;
    const quint64 left = deadlineMs - nowMs;
    return left < sliceMs ? static_cast<quint32>( left ) : sliceMs;
}

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
    /*! FULLSCREEN-S1S2-PRESENT-GAP-1: the delivery chosen by mode (MainWindow passes the parsed environment). The bool
     *  constructor is this one with Post (true) or Notifier (false), so its callers keep the r2 PostMessageW path. */
    PlaybackDueWake( QObject *owner, std::function<void()> onFire, PlaybackDueWakeDeliveryMode mode );
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
     *  as a LOST post, which deliverLostPost() delivers once, and is counted (postFailures()).
     *  r8 (adversary pass 17 A2), the budget contract: the budget bounds the retry loop's OWN waiting and deciding. Each wait
     *  is recomputed from the clock immediately before it and clamped to min(1 ms, the budget left)
     *  (playbackDueWakeSliceWaitMs); once a clock read shows the budget spent, no wait and no further attempt starts. What no
     *  check can bound is a preemption of the wait thread itself: it delays whatever step comes next by its own length, and
     *  an attempt decided inside the budget then posts late. Such a post carries the generation it loads at that moment and
     *  is judged by the gate as usual, so a wake is still delivered at most once. */
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
     *  r8 (adversary pass 18 A1): the wait thread probes the timer AFTER it loads the request number it answers, so a request
     *  newer than the wake that carried it is never answered ahead of a timer signal still pending.
     *  False on that timeout (the counters may still move). True at once when there is no wait thread.
     *  r8 (adversary pass 17 A2 sibling): it waits in slices of at most kSettleSliceMs, each recomputed from the deadline
     *  immediately before it, so a GUI thread preempted near the deadline returns at most one slice after it resumes.
     *  r8 (pass 17 A1): a caller that takes a baseline after a false return must carry it (MainWindow prints
     *  post_counts_settled=0 for that Play), never treat the baseline as settled. */
    bool settlePostCounters();
    static constexpr int kSettleSlackMs = 1000;
    static constexpr quint32 kSettleSliceMs = 10;
    /*! Test hook: the wait thread's retry budget in ms (0: no retry). */
    void setPostRetryBudgetMsForTest( int ms );
    /*! Test hook, r8 (adversary pass 18 A1): the wait thread pauses this long between waking for a settle request and loading
     *  the request number (the window in which a newer request and a timer signal can both arrive). 0: no pause. */
    void setSettleAnswerDelayMsForTest( int ms );
    /*! Stop and join the wait thread (idempotent). After it returns no message is posted and arm() is a no-op. */
    void stop();

    /*! MLVAPP_PLAYBACK_DUE_WAKE=0 turns the due wake off; anything else (or unset) leaves it on. Read on every call. */
    static bool enabledByEnvironment();

    /*! FULLSCREEN-S1S2-PRESENT-GAP-1: true when the wait thread hands the wake over with SendNotifyMessageW (the native path
     *  was taken and the mode is SendNotify). */
    bool sendsNotify() const;
    /*! "send" | "post" (the native-message path, by mode) | "notifier" | "qtimer" (the path chosen at construction) */
    const char *deliveryKindName() const;

    /*! FULLSCREEN-S1S2-PRESENT-GAP-1, measurement. GUI thread. While on, the GUI thread's event dispatcher's aboutToBlock /
     *  awake are traced (PlaybackGuiSleepTrace), and noteRunnerSleep() is recorded; off disconnects and forgets the trace.
     *  Idempotent. MainWindow turns it on only inside a playback_smoke session. */
    void setGuiTraceActive( bool active );
    bool guiTraceActive() const;
    /*! The smoke runner's own sleep on the GUI thread begins (\a sleeping true) or ends, stamped now on the wake's own (QPC)
     *  clock. A no-op while the trace is off. */
    void noteRunnerSleep( bool sleeping );
    /*! What the GUI thread was doing at \a ms (QPC ms), from the trace. Unknown while the trace is off. */
    PlaybackGuiSleepTrace::At guiStateAt( double ms ) const;
    /*! A posted (or sent) wake whose arm was replaced or cancelled before the GUI thread took it: the wait thread had
     *  published its post-return stamp for the replaced generation, and the gate never accepted it. Counted at arm() /
     *  cancel() on the GUI thread, split by what the GUI thread was doing at that post (guiStateAt of the return stamp). */
    struct StrandedCounts
    {
        quint64 total = 0;
        quint64 dispatcherWait = 0;
        quint64 runnerSleep = 0;
        quint64 running = 0;
        quint64 unknown = 0;
    };
    StrandedCounts strandedCounts() const;
    void resetStrandedCounts();
    /*! GUI thread (diagnostic): the wait thread has published the return stamp of a post carrying the current generation,
     *  i.e. the current arm's message has been handed over. */
    bool armPostPublished() const;

    /*! FULLSCREEN-S1S2-PRESENT-GAP-1, the lever's second half. The --gui-smoke-playback runner sleeps on the GUI thread
     *  between its processEvents calls (MainWindow::sleepGuiThreadForSmokeRunner) with QThread::msleep, which pumps nothing:
     *  a wake handed over during it waited for its end, posted or sent (Bachelor 4adc11c9 fullscreen: post_to_receive p50
     *  7.9 / 9.2 ms against a 10 ms sleep). The application's own loop waits with QS_ALLINPUT, so a wake reaches it at once.
     *  GUI thread. With SendNotify delivery this sleeps \a ms as a message wait for QS_SENDMESSAGE ONLY (traced as the runner
     *  sleep): the sent wake ends it early; posted messages, timers and input do not, so the runner's cadence is otherwise
     *  unchanged. Returns RunnerSleep::NotHandled for every other delivery (the caller sleeps exactly as before),
     *  Slept when the whole \a ms passed, WokenBySent when THIS wake's sent message arrived: the caller then accounts its
     *  sleep and calls dispatchSentMessages().
     *  r2 G1 (CoS PR 811): a sent message ends the wait, and the sleep dispatches the pending sent messages at once. This
     *  wake's window procedure holds its own message there (holdWindowProcWake) instead of firing it; only a dispatch that
     *  held one ends the sleep (WokenBySent). Any other sent message runs in that dispatch, is counted
     *  (runnerSleepsWokenOther, once per interruption) and the sleep goes on for the rest of \a ms, so the cadence never shortens.
     *  r2 G3: a failed wait (WAIT_FAILED) sleeps the rest of \a ms with QThread::msleep (counted runnerSleepWaitFailures). */
    enum class RunnerSleep { NotHandled, Slept, WokenBySent };
    RunnerSleep sleepForSmokeRunner( unsigned long ms );
    /*! GUI thread: fire the wakes the runner sleep held (through the gate), then dispatch the pending SENT messages (the
     *  wake's window procedure runs here), and nothing else -- posted messages stay queued for the caller's next processEvents. */
    void dispatchSentMessages();
    /*! GUI thread, called by the wake's window procedure with a kNativeWakeMessage MSG*: inside sleepForSmokeRunner's own
     *  dispatch the message is held for dispatchSentMessages() and this returns true (the window procedure does not fire
     *  it); anywhere else false. */
    bool holdWindowProcWake( const void *message );
    /*! Runner sleeps this wake's sent message ended early, since construction or resetStrandedCounts(). */
    quint64 runnerSleepsWokenBySent() const;
    /*! r2 G1: runner-sleep interruptions by a sent message that was not this wake (the sleep went on), same span. */
    quint64 runnerSleepsWokenOther() const;
    /*! r2 G3: runner sleeps whose message wait failed and fell back to QThread::msleep, same span. */
    quint64 runnerSleepWaitFailures() const;
    /*! Test hook: make the runner sleep's message wait return WAIT_FAILED (the OS failure cannot be injected otherwise). */
    void setRunnerSleepWaitFailsForTest( bool fails );

private:
    struct Impl;
    std::unique_ptr<Impl> d;
};

#endif // PLAYBACKDUEWAKE_H
