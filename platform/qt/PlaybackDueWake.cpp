/*!
 * \file PlaybackDueWake.cpp
 * \brief HFR-NATIVE-M16-1457-GUI-PRESENT-CADENCE-1: see PlaybackDueWake.h.
 */

#include "PlaybackDueWake.h"

#include <QAbstractEventDispatcher>
#include <QAbstractNativeEventFilter>
#include <QByteArray>
#include <QCoreApplication>
#include <QElapsedTimer>
#include <QObject>
#include <QTimer>

#include <QThread>

#include <algorithm>
#include <atomic>
#include <cmath>
#include <vector>

#ifdef Q_OS_WIN
#include <QWinEventNotifier>
#include <windows.h>
#ifndef CREATE_WAITABLE_TIMER_HIGH_RESOLUTION
#define CREATE_WAITABLE_TIMER_HIGH_RESOLUTION 0x00000002
#endif
#endif

struct PlaybackDueWake::Impl
{
    std::function<void()> onFire;
    PlaybackDueWakeGate gate;
    QElapsedTimer clock;
    Delivery delivery = Delivery::QtTimer;
    bool stopped = false;
    bool lastArmQtTimer = false;          // r2 pass 5 A1: the last arm could not set the waitable timer and ran on a QTimer
    bool failSetTimerForTest = false;
    QTimer *timer = nullptr;              // non-Windows, or Windows when no waitable timer could be created
#ifdef Q_OS_WIN
    HANDLE handle = nullptr;
    bool highResolution = false;          // CREATE_WAITABLE_TIMER_HIGH_RESOLUTION accepted
    bool periodRaised = false;            // the fallback raised the timer resolution for this Play
    QWinEventNotifier *notifier = nullptr;
    HMODULE winmm = nullptr;
    typedef UINT ( WINAPI *TimePeriodFn )( UINT );
    TimePeriodFn timeBeginPeriodFn = nullptr;
    TimePeriodFn timeEndPeriodFn = nullptr;
    // r2 native-message delivery
    HWND window = nullptr;                // message-only, created and destroyed on the GUI thread
    HANDLE stopEvent = nullptr;
    HANDLE thread = nullptr;
    std::atomic<quint32> postGeneration { 0 };   // the generation the wait thread posts (set before each SetWaitableTimer)
    // r5 adversary pass 9 A1: the seqlock the wait thread publishes PostMessageW's return time through. postReturnFor is the
    // first stamp (the message's lParam) that postReturnQpc belongs to; 0 while it is being written. QPC stamps only grow,
    // so a reader that sees the same first stamp before and after reading postReturnQpc read that post's return time.
    std::atomic<qint64> postReturnFor { 0 };
    std::atomic<qint64> postReturnQpc { 0 };
    // FULLSCREEN-S1S2-PRESENT-GAP-1: the generation that post carried, written inside the same seqlock window
    std::atomic<quint32> postReturnGeneration { 0 };
    QAbstractNativeEventFilter *filter = nullptr;
    // r7 (adversary passes 13/14 A1): a post the full queue refused past the retry budget leaves its generation here for the
    // GUI thread (deliverLostPost, 0: none); the counters are the wait thread's, read for the summary line
    std::atomic<quint32> lostGeneration { 0 };
    std::atomic<quint64> postRefusals { 0 };
    std::atomic<quint64> postFailures { 0 };
    std::atomic<int> postRetryBudgetMs { PlaybackDueWake::kPostRetryBudgetMs };
    // r7 (adversary pass 16 A1): settlePostCounters' handshake with the wait thread (both auto-reset)
    HANDLE settleEvent = nullptr;
    HANDLE settledEvent = nullptr;
    std::atomic<quint64> settleRequest { 0 };
    std::atomic<quint64> settleAck { 0 };
    std::atomic<int> settleAnswerDelayMsForTest { 0 };
#endif
    // r7 (adversary pass 15 A1): lost posts the poll tick fired, counted BEFORE the callback runs (it can stop Play and print
    // the summary line before deliverLostPost returns); rescuingLostPost marks the delivery deliverLostPost is making
    quint64 postRescued = 0;
    bool rescuingLostPost = false;

    // FULLSCREEN-S1S2-PRESENT-GAP-1 (measurement, GUI thread): the delivery mode, the GUI thread's sleep trace and the
    // stranded-post counts
    PlaybackDueWakeDeliveryMode mode = PlaybackDueWakeDeliveryMode::Post;
    PlaybackGuiSleepTrace guiTrace;
    bool guiTraceActive = false;
    QMetaObject::Connection traceBlock;
    QMetaObject::Connection traceAwake;
    PlaybackDueWake::StrandedCounts stranded;
    quint64 runnerSleepsWokenBySent = 0;
    // r2 G1 (GUI thread): sleepForSmokeRunner dispatches the sent messages that end its wait; inside that dispatch the window
    // procedure HOLDS this wake's own message (heldWakes) instead of firing it, so the wake still runs after the sleep is
    // accounted (dispatchSentMessages). A dispatch that held none was another sent message: counted, and the sleep goes on.
    bool inRunnerSleepDispatch = false;
    // r3 (r2 adversary pass 7 A3): the time the LAST sleepForSmokeRunner spent running sent messages (GUI work, traced as Running,
    // not RunnerSleep); the caller takes it out of the sleep it accounts (lastRunnerSleepDispatchNs)
    qint64 runnerSleepDispatchNs = 0;
    quint64 runnerSleepsWokenOther = 0;
    // r2 G3: runner sleeps whose message wait failed (WAIT_FAILED); the rest of the interval was a plain msleep
    quint64 runnerSleepWaitFailed = 0;
    bool failRunnerSleepWaitForTest = false;
#ifdef Q_OS_WIN
    std::vector<MSG> heldWakes;
#endif
    // r1 adversary pass 2 A3 (GUI thread only): the first stamp (lParam) of the last native message the GUI thread took, stale
    // or not; a published post with this stamp is consumed, never stranded
    qint64 consumedPostFor = 0;
    double nowMs() const { return static_cast<double>( nowNs() ) / 1000000.0; }
    PlaybackGuiSleepTrace::At guiStateAt( double ms ) const
    {
        if( !guiTraceActive ) return { PlaybackGuiSleepTrace::State::Unknown, false, 0.0 };
        return guiTrace.at( ms );
    }
    // GUI thread, at arm() / cancel() BEFORE the gate moves on: the arm being replaced was never accepted, and the wait
    // thread had already published the return stamp of a post carrying exactly its generation -- that message is in the
    // GUI thread's queue (or was refused nothing: it returned) and will be dropped as stale. Seqlock reader as in
    // handleNativeMessage: the same first stamp before and after the reads.
    void noteStrandedIfPosted()
    {
#ifdef Q_OS_WIN
        if( !gate.armed() || qpcFrequency <= 0 ) return;
        const qint64 forBefore = postReturnFor.load();
        const qint64 returnTicks = postReturnQpc.load();
        const quint32 postedGeneration = postReturnGeneration.load();
        const qint64 forAfter = postReturnFor.load();
        if( forBefore <= 0 || forAfter != forBefore || returnTicks < forBefore ) return;
        if( postedGeneration != gate.generation() ) return;
        if( forBefore == consumedPostFor ) return;   // r1 adversary pass 2 A3: already taken (e.g. dropped as early), not in the queue
        ++stranded.total;
        const PlaybackGuiSleepTrace::State state =
            guiStateAt( static_cast<double>( ticksToNs( returnTicks, qpcFrequency ) ) / 1000000.0 ).state;
        switch( state )
        {
        case PlaybackGuiSleepTrace::State::DispatcherWait: ++stranded.dispatcherWait; break;
        case PlaybackGuiSleepTrace::State::RunnerSleep: ++stranded.runnerSleep; break;
        case PlaybackGuiSleepTrace::State::Running: ++stranded.running; break;
        case PlaybackGuiSleepTrace::State::Unknown: ++stranded.unknown; break;
        }
#endif
    }

    // r3 (adversary pass 7 A1): the gate's clock. On Windows it is QueryPerformanceCounter in ns, the clock the wait thread
    // stamps the post time on, so the gate can drop a delivery posted before the armed due time; elsewhere QElapsedTimer.
    qint64 qpcFrequency = 0;              // > 0 when the gate runs on QPC (decided once, at construction)
    static qint64 ticksToNs( qint64 ticks, qint64 frequency )
    {
        return ( ticks / frequency ) * 1000000000LL + ( ( ticks % frequency ) * 1000000000LL ) / frequency;
    }
    qint64 nowNs() const
    {
#ifdef Q_OS_WIN
        LARGE_INTEGER counter;
        if( qpcFrequency > 0 && QueryPerformanceCounter( &counter ) ) return ticksToNs( counter.QuadPart, qpcFrequency );
#endif
        return clock.nsecsElapsed();
    }
    // the notifier / Qt timer paths: the signal belongs to whatever is armed now (the gate drops an early one).
    // r6 (adversary pass 10 A1): ...but only on the mechanism the current arm runs on. A notifier activation queued from the
    // waitable timer an arm that fell back to a QTimer replaced is not that arm's signal (only the QTimer is), and a QTimer
    // timeout is not the signal of an arm that set the waitable timer.
    void deliverCurrent( bool fromQtTimer )
    {
        if( fromQtTimer != lastArmQtTimer ) return;
        if( gate.accept( gate.generation(), nowNs() ) && onFire ) onFire();
    }
    // r3: the post time of the delivery firing now (ms on the QPC clock), < 0 when the delivery carries none
    double firingPostMs = -1.0;
    double firingFirstMs = -1.0;          // r6 pass 10 A2: its first stamp (>= 0 exactly when firingPostMs is)
    bool firingUnsplit = false;           // r5 pass 9 A1: a stamped native delivery whose post-return time was not published yet
    // the native-message path: the message names its generation and (r3) its first post stamp in QPC ticks (<= 0: none);
    // r5: returnTicks is PostMessageW's return time for that stamp (<= 0: not published when the GUI thread took it). The gate
    // still judges "posted before the due time" on the first stamp, the earliest the post can have been.
    // r2 adversary pass 5 A1: the gate's verdict on a native delivery without taking it (deliverGeneration's own test)
    bool deliveryCurrent( quint32 generation, qint64 postTicks ) const
    {
        const qint64 postNs = ( postTicks > 0 && qpcFrequency > 0 ) ? ticksToNs( postTicks, qpcFrequency ) : -1;
        return gate.wouldAccept( generation, nowNs(), postNs );
    }
    void deliverGeneration( quint32 generation, qint64 postTicks = 0, qint64 returnTicks = 0 )
    {
        const qint64 postNs = ( postTicks > 0 && qpcFrequency > 0 ) ? ticksToNs( postTicks, qpcFrequency ) : -1;
        if( !gate.accept( generation, nowNs(), postNs ) || !onFire ) return;
        const qint64 returnNs = ( postNs >= 0 && returnTicks >= postTicks ) ? ticksToNs( returnTicks, qpcFrequency ) : -1;
        firingPostMs = returnNs >= 0 ? static_cast<double>( returnNs ) / 1000000.0 : -1.0;
        firingFirstMs = returnNs >= 0 ? static_cast<double>( postNs ) / 1000000.0 : -1.0;
        firingUnsplit = postNs >= 0 && returnNs < 0;
        if( rescuingLostPost )
        {
            ++postRescued;
            rescuingLostPost = false;   // a delivery nested inside the callback is not this rescue
        }
        onFire();
        firingPostMs = -1.0;
        firingFirstMs = -1.0;
        firingUnsplit = false;
    }
};

#ifdef Q_OS_WIN
namespace
{
const wchar_t kDueWakeWindowClass[] = L"MLVAppPlaybackDueWake";

LRESULT CALLBACK dueWakeWindowProc( HWND hwnd, UINT message, WPARAM wParam, LPARAM lParam )
{
    if( message == PlaybackDueWake::kNativeWakeMessage )
    {
        // reached only when the Qt filter did not see it (a modal loop dispatching on its own)
        PlaybackDueWake *wake = reinterpret_cast<PlaybackDueWake *>( GetWindowLongPtrW( hwnd, GWLP_USERDATA ) );
        if( wake )
        {
            MSG msg = {};
            msg.hwnd = hwnd;
            msg.message = message;
            msg.wParam = wParam;
            msg.lParam = lParam;
            // r2 G1: inside the runner sleep's dispatch the wake is held (and flagged), and fires after the sleep
            if( !wake->holdWindowProcWake( &msg ) ) wake->handleNativeMessage( &msg );
        }
        return 0;
    }
    return DefWindowProcW( hwnd, message, wParam, lParam );
}

// GUI thread: PeekMessage dispatches every pending sent message before it looks at the queue; PM_QS_SENDMESSAGE with
// PM_NOREMOVE leaves every posted message where it is
void dispatchPendingSentMessages()
{
    MSG msg;
    PeekMessageW( &msg, nullptr, 0, 0, PM_NOREMOVE | PM_QS_SENDMESSAGE );
}

bool registerDueWakeWindowClass()
{
    static const bool registered = []() {
        WNDCLASSEXW wc = {};
        wc.cbSize = sizeof( wc );
        wc.lpfnWndProc = dueWakeWindowProc;
        wc.hInstance = GetModuleHandleW( nullptr );
        wc.lpszClassName = kDueWakeWindowClass;
        return RegisterClassExW( &wc ) != 0 || GetLastError() == ERROR_CLASS_ALREADY_EXISTS;
    }();
    return registered;
}

// r3: the tag and the generation share wParam, and the post QPC fills lParam (64-bit Windows only; Qt 6 has no other)
static_assert( sizeof( WPARAM ) >= 8 && sizeof( LPARAM ) >= 8, "the due-wake message packs 64-bit wParam / lParam" );

struct WaitThreadArgs
{
    HANDLE stopEvent;
    HANDLE timer;
    HWND window;
    std::atomic<quint32> *postGeneration;
    std::atomic<qint64> *postReturnFor;
    std::atomic<qint64> *postReturnQpc;
    std::atomic<quint32> *lostGeneration;
    std::atomic<quint64> *postRefusals;
    std::atomic<quint64> *postFailures;
    std::atomic<int> *postRetryBudgetMs;
    HANDLE settleEvent;                   // r7 pass 16 A1: the GUI thread asks "every signal so far is counted?"
    HANDLE settledEvent;                  // ...and the wait thread answers, with the request it answers in settleAck
    std::atomic<quint64> *settleRequest;
    std::atomic<quint64> *settleAck;
    std::atomic<int> *settleAnswerDelayMsForTest;   // r8 pass 18 A1: a pause between the settle wake and its request load
    std::atomic<quint32> *postReturnGeneration;     // FULLSCREEN-S1S2-PRESENT-GAP-1: the generation the published post carried
    bool sendNotify;                                // FULLSCREEN-S1S2-PRESENT-GAP-1: SendNotifyMessageW instead of PostMessageW
};

// One post of the wake message, carrying the generation stored now; false when the queue refused it. \a generation is the
// generation it carried.
// FULLSCREEN-S1S2-PRESENT-GAP-1: with sendNotify the same message, wParam and lParam go through SendNotifyMessageW. To a window
// of another thread it queues a sent message and returns at once (it never waits for the GUI thread); the GUI thread's next
// PeekMessage / GetMessage dispatches it to dueWakeWindowProc whatever their filter, and a message wait that names
// QS_SENDMESSAGE returns for it. A 0 return takes the refused-post path below (retry within the budget, then a lost post).
bool postDueWake( const WaitThreadArgs & args, qint64 postQpc, quint32 & generation )
{
    generation = args.postGeneration->load( std::memory_order_acquire );
    if( args.sendNotify )
        return SendNotifyMessageW( args.window, PlaybackDueWake::kNativeWakeMessage,
                                   static_cast<WPARAM>( playbackDueWakePackWParam(
                                       generation, static_cast<quint32>( PlaybackDueWake::kNativeWakeTag ) ) ),
                                   static_cast<LPARAM>( postQpc ) ) != 0;
    return PostMessageW( args.window, PlaybackDueWake::kNativeWakeMessage,
                         static_cast<WPARAM>( playbackDueWakePackWParam(
                             generation, static_cast<quint32>( PlaybackDueWake::kNativeWakeTag ) ) ),
                         static_cast<LPARAM>( postQpc ) ) != 0;
}

DWORD WINAPI dueWakeWaitThread( LPVOID parameter )
{
    WaitThreadArgs args = *static_cast<WaitThreadArgs *>( parameter );
    delete static_cast<WaitThreadArgs *>( parameter );
    // the stop event is first: it wins over a timer signalled at the same instant.
    // r7 (adversary pass 16 A1): the settle request is LAST, so a timer signal that came before it is posted (or retried and
    // counted) before it is answered: WaitForMultipleObjects reports the lowest signalled index, and this one thread does the
    // posting, the retrying and the counting in order
    const HANDLE handles[3] = { args.stopEvent, args.timer, args.settleEvent };
    quint64 pendingAck = 0;   // r8 pass 18 A1: a request answered once the timer signal it found pending is handled
    for( ;; )
    {
        if( pendingAck )
        {
            args.settleAck->store( pendingAck );
            SetEvent( args.settledEvent );
            pendingAck = 0;
        }
        DWORD result = WaitForMultipleObjects( 3, handles, FALSE, INFINITE );
        if( result == WAIT_OBJECT_0 + 2 )
        {
            const int answerDelayMs = args.settleAnswerDelayMsForTest->load();
            if( answerDelayMs > 0 ) Sleep( static_cast<DWORD>( answerDelayMs ) );
            const quint64 request = args.settleRequest->load();
            // r8 (adversary pass 18 A1): this thread can be paused between the wake and the load above, so the request it
            // loaded can be newer than a timer signal that arrived meanwhile. Probe the timer AFTER the load: a signal still
            // pending is handled (posted, or retried and counted) below before this request is answered at the loop top
            if( WaitForSingleObject( args.timer, 0 ) != WAIT_OBJECT_0 )
            {
                args.settleAck->store( request );
                SetEvent( args.settledEvent );
                continue;
            }
            pendingAck = request;
            result = WAIT_OBJECT_0 + 1;
        }
        if( result != WAIT_OBJECT_0 + 1 ) break;   // stop, or a wait failure
        // r3: stamp the post time, so the GUI thread can split due-to-post from post-to-receive
        LARGE_INTEGER postQpc;
        if( !QueryPerformanceCounter( &postQpc ) ) postQpc.QuadPart = 0;
        quint32 generation = 0;
        bool posted = postDueWake( args, postQpc.QuadPart, generation );
        // r7 (adversary passes 13/14 A1): a full queue refuses the post (ERROR_NOT_ENOUGH_QUOTA), and the auto-reset signal is
        // already consumed, so a dropped post left the arm armed and undelivered. Retry for the bounded budget (each retry
        // loads the generation again, so a re-arm or cancel meanwhile is judged by the gate as usual; the first stamp stays
        // the earliest the post can have been). Past the budget the generation is handed to the GUI thread, whose poll tick
        // delivers it once (deliverLostPost), and the failure is counted.
        if( !posted )
        {
            const ULONGLONG start = GetTickCount64();
            const ULONGLONG budget = static_cast<ULONGLONG>( ( std::max )( args.postRetryBudgetMs->load(), 0 ) );   // ms
            bool stopped = false;
            for( ;; )
            {
                args.postRefusals->fetch_add( 1 );
                // r8 (adversary pass 17 A2): the wait is recomputed from the clock in the same expression that feeds it and
                // clamped to min(1 ms, the budget left); 0 = the budget is spent (see kPostRetryBudgetMs for the contract)
                const DWORD retryWaitMs = playbackDueWakeSliceWaitMs( GetTickCount64(), start + budget, 1 );
                if( retryWaitMs == 0 ) break;
                if( WaitForSingleObject( args.stopEvent, retryWaitMs ) != WAIT_TIMEOUT )
                {
                    stopped = true;
                    break;
                }
                // r7 (adversary pass 16 A2): the wait can overrun the budget; no attempt starts past it
                if( GetTickCount64() - start >= budget ) break;
                if( postDueWake( args, postQpc.QuadPart, generation ) )
                {
                    posted = true;
                    break;
                }
            }
            if( stopped ) break;
            if( !posted )
            {
                // r7 (adversary pass 15 A1, sibling): counted BEFORE the generation is published, so a GUI thread that
                // rescues it (and may print the summary line from inside the callback) always sees the failure
                args.postFailures->fetch_add( 1 );
                args.lostGeneration->store( generation, std::memory_order_release );
                continue;   // nothing was posted: no return time to publish
            }
        }
        // r5 adversary pass 9 A1: the first stamp is taken BEFORE the post, so a pause of this thread between the two would
        // read as GUI-queue time. Publish when PostMessageW returned, keyed by the first stamp (seqlock writer: invalidate,
        // write, validate); the GUI thread splits at this time, or not at all
        LARGE_INTEGER returnQpc;
        if( postQpc.QuadPart > 0 && QueryPerformanceCounter( &returnQpc ) )
        {
            args.postReturnFor->store( 0 );
            args.postReturnQpc->store( returnQpc.QuadPart );
            args.postReturnGeneration->store( generation );   // FULLSCREEN-S1S2-PRESENT-GAP-1: inside the same window
            args.postReturnFor->store( postQpc.QuadPart );
        }
    }
    return 0;
}

class DueWakeNativeFilter : public QAbstractNativeEventFilter
{
public:
    explicit DueWakeNativeFilter( PlaybackDueWake *wake ) : m_wake( wake ) {}
#if QT_VERSION >= QT_VERSION_CHECK(6, 0, 0)
    bool nativeEventFilter( const QByteArray &eventType, void *message, qintptr *result ) override
#else
    bool nativeEventFilter( const QByteArray &eventType, void *message, long *result ) override
#endif
    {
        // the event dispatcher filters every message it peeks as "windows_dispatcher_MSG" before DispatchMessage; a
        // Qt window procedure filters as "windows_generic_MSG". This window is not a Qt window: the first one applies.
        if( !message || ( eventType != "windows_dispatcher_MSG" && eventType != "windows_generic_MSG" ) ) return false;
        if( static_cast<const MSG *>( message )->message != PlaybackDueWake::kNativeWakeMessage ) return false;
        if( !m_wake->handleNativeMessage( message ) ) return false;
        if( result ) *result = 0;
        return true;
    }
private:
    PlaybackDueWake *m_wake;
};
}
#endif

PlaybackDueWake::PlaybackDueWake( QObject *owner, std::function<void()> onFire, bool preferNativeMessage )
    : PlaybackDueWake( owner, std::move( onFire ),
                       preferNativeMessage ? PlaybackDueWakeDeliveryMode::Post : PlaybackDueWakeDeliveryMode::Notifier )
{
}

PlaybackDueWake::PlaybackDueWake( QObject *owner, std::function<void()> onFire, PlaybackDueWakeDeliveryMode mode )
    : d( new Impl )
{
    // FULLSCREEN-S1S2-PRESENT-GAP-1: Notifier skips the wait thread (r1); Post and SendNotify take the native path
    const bool preferNativeMessage = mode != PlaybackDueWakeDeliveryMode::Notifier;
    d->mode = mode;
    d->onFire = std::move( onFire );
    d->clock.start();
#ifdef Q_OS_WIN
    {
        LARGE_INTEGER frequency;
        if( QueryPerformanceFrequency( &frequency ) && frequency.QuadPart > 0 ) d->qpcFrequency = frequency.QuadPart;
    }
    d->handle = CreateWaitableTimerExW( nullptr, nullptr, CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, TIMER_ALL_ACCESS );
    d->highResolution = d->handle != nullptr;
    if( !d->handle )
        d->handle = CreateWaitableTimerExW( nullptr, nullptr, 0, TIMER_ALL_ACCESS );   // auto-reset, plain
    if( d->handle && preferNativeMessage && registerDueWakeWindowClass() )
    {
        d->window = CreateWindowExW( 0, kDueWakeWindowClass, L"", 0, 0, 0, 0, 0, HWND_MESSAGE, nullptr,
                                     GetModuleHandleW( nullptr ), nullptr );
        if( d->window )
        {
            SetWindowLongPtrW( d->window, GWLP_USERDATA, reinterpret_cast<LONG_PTR>( this ) );
            d->stopEvent = CreateEventW( nullptr, TRUE, FALSE, nullptr );   // manual reset: stays set once stopped
            d->settleEvent = CreateEventW( nullptr, FALSE, FALSE, nullptr );
            d->settledEvent = CreateEventW( nullptr, FALSE, FALSE, nullptr );
        }
        if( d->stopEvent && d->settleEvent && d->settledEvent )
        {
            WaitThreadArgs *args = new WaitThreadArgs { d->stopEvent, d->handle, d->window, &d->postGeneration,
                                                        &d->postReturnFor, &d->postReturnQpc, &d->lostGeneration,
                                                        &d->postRefusals, &d->postFailures, &d->postRetryBudgetMs,
                                                        d->settleEvent, d->settledEvent, &d->settleRequest, &d->settleAck,
                                                        &d->settleAnswerDelayMsForTest, &d->postReturnGeneration,
                                                        d->mode == PlaybackDueWakeDeliveryMode::SendNotify };
            d->thread = CreateThread( nullptr, 0, dueWakeWaitThread, args, 0, nullptr );
            if( d->thread )
                SetThreadPriority( d->thread, THREAD_PRIORITY_HIGHEST );
            else
                delete args;
        }
        if( d->thread )
        {
            d->delivery = Delivery::NativeMessage;
            if( QCoreApplication::instance() )
            {
                d->filter = new DueWakeNativeFilter( this );
                QCoreApplication::instance()->installNativeEventFilter( d->filter );
            }
            return;
        }
        // the thread could not start: undo the partial native path and fall back to the notifier
        if( d->stopEvent ) CloseHandle( d->stopEvent );
        d->stopEvent = nullptr;
        if( d->settleEvent ) CloseHandle( d->settleEvent );
        d->settleEvent = nullptr;
        if( d->settledEvent ) CloseHandle( d->settledEvent );
        d->settledEvent = nullptr;
        if( d->window ) DestroyWindow( d->window );
        d->window = nullptr;
    }
    if( d->handle )
    {
        d->delivery = Delivery::Notifier;
        d->notifier = new QWinEventNotifier( d->handle, owner );
        Impl *impl = d.get();
        QObject::connect( d->notifier, &QWinEventNotifier::activated, d->notifier, [impl]( HANDLE ) { impl->deliverCurrent( false ); } );
        d->notifier->setEnabled( true );
        return;
    }
#else
    Q_UNUSED( preferNativeMessage );
#endif
    d->delivery = Delivery::QtTimer;
    d->timer = new QTimer( owner );
    d->timer->setSingleShot( true );
    d->timer->setTimerType( Qt::PreciseTimer );
    Impl *impl = d.get();
    QObject::connect( d->timer, &QTimer::timeout, owner, [impl]() { impl->deliverCurrent( true ); } );
}

PlaybackDueWake::~PlaybackDueWake()
{
    // the wait thread is joined first, so it never posts to the window destroyed below
    stop();
    cancel();
    endPlay();
    setGuiTraceActive( false );   // FULLSCREEN-S1S2-PRESENT-GAP-1: the dispatcher connections capture this object
#ifdef Q_OS_WIN
    if( d->filter )
    {
        if( QCoreApplication::instance() ) QCoreApplication::instance()->removeNativeEventFilter( d->filter );
        delete d->filter;
        d->filter = nullptr;
    }
    if( d->window )
    {
        SetWindowLongPtrW( d->window, GWLP_USERDATA, 0 );
        DestroyWindow( d->window );   // discards a message still queued for it
        d->window = nullptr;
    }
    delete d->notifier;
    d->notifier = nullptr;
    if( d->handle ) CloseHandle( d->handle );
    d->handle = nullptr;
    if( d->winmm ) FreeLibrary( d->winmm );
    d->winmm = nullptr;
#endif
    delete d->timer;
    d->timer = nullptr;
}

void PlaybackDueWake::stop()
{
    d->stopped = true;
#ifdef Q_OS_WIN
    if( d->thread )
    {
        SetEvent( d->stopEvent );
        WaitForSingleObject( d->thread, INFINITE );
        CloseHandle( d->thread );
        d->thread = nullptr;
    }
    if( d->stopEvent ) CloseHandle( d->stopEvent );
    d->stopEvent = nullptr;
    if( d->settleEvent ) CloseHandle( d->settleEvent );
    d->settleEvent = nullptr;
    if( d->settledEvent ) CloseHandle( d->settledEvent );
    d->settledEvent = nullptr;
    if( d->handle ) CancelWaitableTimer( d->handle );
#endif
    if( d->timer ) d->timer->stop();
    d->gate.cancel();
}

void PlaybackDueWake::arm( double dueMs )
{
    if( d->stopped ) return;
    if( !( dueMs > 0.0 ) ) dueMs = 0.0;
    // r6 adversary pass 12 A1 (and pass 10 A4, +infinity): bounded before the ns, 100 ns and int conversions below
    if( dueMs > kMaxDueMs ) dueMs = kMaxDueMs;
    d->noteStrandedIfPosted();   // FULLSCREEN-S1S2-PRESENT-GAP-1: before the replaced arm's generation is gone
    const quint32 generation = d->gate.arm( d->nowNs(), static_cast<qint64>( dueMs * 1000000.0 ) );
    d->lastArmQtTimer = d->delivery == Delivery::QtTimer;
#ifdef Q_OS_WIN
    d->postGeneration.store( generation, std::memory_order_release );   // before the timer can signal
    if( d->handle )
    {
        LARGE_INTEGER due;
        // relative due, in 100 ns units (negative = relative); at least one unit so the timer always signals
        const LONGLONG units = static_cast<LONGLONG>( std::ceil( dueMs * 10000.0 ) );
        due.QuadPart = -( units > 0 ? units : 1 );
        if( !d->failSetTimerForTest && SetWaitableTimer( d->handle, &due, 0, nullptr, nullptr, FALSE ) )
        {
            if( d->timer ) d->timer->stop();   // an earlier fallback arm's QTimer belongs to a replaced arm
            return;
        }
        // could not arm: fall through to a Qt timer for this wake (delivered as "whatever is armed now"). The arm is
        // reported as a qtimer one, low resolution, whatever the construction chose (r2 adversary pass 5 A1).
        // r5 adversary pass 9 A2: the failed SetWaitableTimer left the REPLACED arm's timer running, and its signal would
        // have posted (or, on the notifier path, delivered) this arm's generation. Cancel it, and post generation 0 (never
        // live) from now on, so only the QTimer delivers this arm.
        // r6 adversary pass 10 A1: a wait thread that consumed the replaced timer's signal BEFORE the cancel can still load the
        // generation stored above and post it after this arm's due time. Renew the generation (same due): that message, and
        // any carrying an older one, is stale; the QTimer delivers the renewed one (deliverCurrent reads gate.generation()).
        CancelWaitableTimer( d->handle );
        d->postGeneration.store( 0, std::memory_order_release );
        d->gate.renew();
        d->lastArmQtTimer = true;
        if( !d->timer )
        {
            QObject *owner = d->notifier ? d->notifier->parent() : nullptr;
            d->timer = new QTimer( owner );
            d->timer->setSingleShot( true );
            d->timer->setTimerType( Qt::PreciseTimer );
            Impl *impl = d.get();
            QObject::connect( d->timer, &QTimer::timeout, [impl]() { impl->deliverCurrent( true ); } );
        }
    }
#else
    Q_UNUSED( generation );
#endif
    if( d->timer ) d->timer->start( static_cast<int>( std::ceil( dueMs ) ) );
}

void PlaybackDueWake::cancel()
{
    d->noteStrandedIfPosted();   // FULLSCREEN-S1S2-PRESENT-GAP-1
    d->gate.cancel();
#ifdef Q_OS_WIN
    d->postGeneration.store( d->gate.generation(), std::memory_order_release );
    if( d->handle ) CancelWaitableTimer( d->handle );
#endif
    if( d->timer ) d->timer->stop();
}

bool PlaybackDueWake::armed() const
{
    return d->gate.armed();
}

bool PlaybackDueWake::handleNativeMessage( const void *msg )
{
#ifdef Q_OS_WIN
    if( !msg ) return false;
    const MSG *m = static_cast<const MSG *>( msg );
    if( m->message != kNativeWakeMessage || !d->window || m->hwnd != d->window ) return false;
    const quint64 wParam = static_cast<quint64>( m->wParam );
    if( playbackDueWakeWParamTag( wParam ) != static_cast<quint32>( kNativeWakeTag ) ) return false;
    // a stale generation (cancelled, re-armed or stopped since it was posted), or (r3, pass 7 A1) a signal posted before the
    // current arm's due time, is consumed and does nothing; lParam is the post time in QPC ticks
    // r5 adversary pass 9 A1 (seqlock reader): PostMessageW's return time for exactly this message's first stamp, or none
    const qint64 postTicks = static_cast<qint64>( m->lParam );
    const qint64 forBefore = d->postReturnFor.load();
    const qint64 returnTicks = d->postReturnQpc.load();
    const qint64 forAfter = d->postReturnFor.load();
    const bool published = postTicks > 0 && forBefore == postTicks && forAfter == postTicks;
    d->consumedPostFor = postTicks;   // r1 adversary pass 2 A3: this post is out of the queue whatever the gate decides
    d->deliverGeneration( playbackDueWakeWParamGeneration( wParam ), postTicks, published ? returnTicks : 0 );
    return true;
#else
    Q_UNUSED( msg );
    return false;
#endif
}

quintptr PlaybackDueWake::nativeWindow() const
{
#ifdef Q_OS_WIN
    return reinterpret_cast<quintptr>( d->window );
#else
    return 0;
#endif
}

bool PlaybackDueWake::waitThreadRunning() const
{
#ifdef Q_OS_WIN
    if( !d->thread ) return false;
    DWORD code = 0;
    return GetExitCodeThread( d->thread, &code ) && code == STILL_ACTIVE;
#else
    return false;
#endif
}

double PlaybackDueWake::lastDeliveryPostMs() const
{
    return d->firingPostMs;
}

double PlaybackDueWake::lastDeliveryPostFirstMs() const
{
    return d->firingFirstMs;
}

bool PlaybackDueWake::lastDeliveryUnsplit() const
{
    return d->firingUnsplit;
}

bool PlaybackDueWake::deliverLostPost()
{
#ifdef Q_OS_WIN
    // r7 (adversary passes 13/14 A1): the generation whose post the full queue refused past the retry budget, taken once.
    // No post time: the gate judges it as a notifier / qtimer delivery (current generation, armed, not early).
    const quint32 generation = d->lostGeneration.exchange( 0, std::memory_order_acq_rel );
    if( generation == 0 ) return false;
    const quint64 before = d->postRescued;
    d->rescuingLostPost = true;
    d->deliverGeneration( generation );
    d->rescuingLostPost = false;
    return d->postRescued != before;
#else
    return false;
#endif
}

quint64 PlaybackDueWake::postRefusals() const
{
#ifdef Q_OS_WIN
    return d->postRefusals.load();
#else
    return 0;
#endif
}

quint64 PlaybackDueWake::postFailures() const
{
#ifdef Q_OS_WIN
    return d->postFailures.load();
#else
    return 0;
#endif
}

quint64 PlaybackDueWake::postRescued() const
{
    return d->postRescued;
}

bool PlaybackDueWake::settlePostCounters()
{
#ifdef Q_OS_WIN
    if( !d->thread || !d->settleEvent || !d->settledEvent ) return true;   // no wait thread: nothing counts asynchronously
    // r7 (adversary pass 16 A1): a numbered request, so an answer to an earlier request that timed out never settles this one
    const quint64 request = d->settleRequest.fetch_add( 1 ) + 1;
    SetEvent( d->settleEvent );
    const ULONGLONG deadline = GetTickCount64() + static_cast<ULONGLONG>( ( std::max )( d->postRetryBudgetMs.load(), 0 ) )
        + kSettleSlackMs;
    for( ;; )
    {
        if( d->settleAck.load() >= request ) return true;
        // r8 (adversary pass 17 A2 sibling): recomputed immediately before each wait and clamped to one slice, never a stale
        // relative wait taken before a preemption
        const DWORD settleWaitMs = playbackDueWakeSliceWaitMs( GetTickCount64(), deadline, kSettleSliceMs );
        if( settleWaitMs == 0 ) return d->settleAck.load() >= request;
        WaitForSingleObject( d->settledEvent, settleWaitMs );
    }
#else
    return true;
#endif
}

void PlaybackDueWake::setPostRetryBudgetMsForTest( int ms )
{
#ifdef Q_OS_WIN
    d->postRetryBudgetMs.store( ms );
#else
    Q_UNUSED( ms );
#endif
}

void PlaybackDueWake::setSettleAnswerDelayMsForTest( int ms )
{
#ifdef Q_OS_WIN
    d->settleAnswerDelayMsForTest.store( ms );
#else
    Q_UNUSED( ms );
#endif
}

PlaybackDueWake::Delivery PlaybackDueWake::delivery() const
{
    return d->delivery;
}

PlaybackDueWake::Delivery PlaybackDueWake::lastArmDelivery() const
{
    return d->lastArmQtTimer ? Delivery::QtTimer : d->delivery;
}

void PlaybackDueWake::setWaitableTimerFailsForTest( bool fails )
{
    d->failSetTimerForTest = fails;
}

const char *PlaybackDueWake::deliveryName( Delivery delivery )
{
    switch( delivery )
    {
    case Delivery::NativeMessage: return "native-msg";
    case Delivery::Notifier: return "notifier";
    case Delivery::QtTimer: break;
    }
    return "qtimer";
}

bool PlaybackDueWake::sendsNotify() const
{
    return d->delivery == Delivery::NativeMessage && d->mode == PlaybackDueWakeDeliveryMode::SendNotify;
}

PlaybackDueWake::RunnerSleep PlaybackDueWake::sleepForSmokeRunner( unsigned long ms )
{
#ifdef Q_OS_WIN
    d->runnerSleepDispatchNs = 0;
    if( !sendsNotify() ) return RunnerSleep::NotHandled;
    noteRunnerSleep( true );
    // QS_SENDMESSAGE only: the sent wake ends the sleep; posted messages, timers and input leave the runner's cadence as it
    // was. MWMO_INPUTAVAILABLE: a sent message already pending at the call returns at once (never a missed wake)
    // r1 adversary pass 1 A5: always a finite wait (INFINITE is 0xFFFFFFFF; a larger unsigned long would truncate)
    const DWORD waitMs = ms >= static_cast<unsigned long>( INFINITE ) ? INFINITE - 1 : static_cast<DWORD>( ms );
    // r1 adversary pass 2 A2: the wait may also return for a system event with no sent message queued; only a queued sent
    // message ends the sleep early (and is counted), anything else sleeps out the rest of the requested time
    QElapsedTimer slept;
    slept.start();
    for( ;; )
    {
        const qint64 sleptMs = slept.elapsed();
        const DWORD restMs = sleptMs >= static_cast<qint64>( waitMs ) ? 0 : static_cast<DWORD>( static_cast<qint64>( waitMs ) - sleptMs );
        const DWORD result = d->failRunnerSleepWaitForTest
            ? WAIT_FAILED : MsgWaitForMultipleObjectsEx( 0, nullptr, restMs, QS_SENDMESSAGE, MWMO_INPUTAVAILABLE );
        if( result == WAIT_FAILED )
        {
            // r2 G3: a failed wait never ends the sleep at ~0 ms; the rest of the interval is the plain msleep every other
            // delivery uses (no sent wake ends it), and the failure is counted (runner_sleep_wait_failed)
            ++d->runnerSleepWaitFailed;
            const qint64 leftMs = static_cast<qint64>( waitMs ) - slept.elapsed();
            if( leftMs > 0 ) QThread::msleep( static_cast<unsigned long>( leftMs ) );
            break;
        }
        if( result != WAIT_OBJECT_0 ) break;   // the timeout
        if( HIWORD( GetQueueStatus( QS_SENDMESSAGE ) ) & QS_SENDMESSAGE )
        {
            // r2 G1: only the wake ends the sleep. The sent messages are dispatched here; the wake's window procedure holds
            // this wake's own message (holdWindowProcWake) and it fires after the sleep (dispatchSentMessages). Any other sent
            // message ran in this dispatch: counted runner_sleep_woken_other, and the sleep goes on to its full interval.
            // r3 (r2 adversary pass 7 A3): the dispatch is GUI work, not the runner's sleep -- the trace says Running while it
            // runs (a wake posted from a handler in it is not classed asleep), and its time is returned to the caller
            // (lastRunnerSleepDispatchNs), which takes it out of the runner sleep it accounts
            const size_t heldBefore = d->heldWakes.size();
            noteRunnerSleep( false );
            QElapsedTimer dispatchClock;
            dispatchClock.start();
            d->inRunnerSleepDispatch = true;
            dispatchPendingSentMessages();
            d->inRunnerSleepDispatch = false;
            d->runnerSleepDispatchNs += dispatchClock.nsecsElapsed();
            if( d->heldWakes.size() > heldBefore )
            {
                ++d->runnerSleepsWokenBySent;
                return RunnerSleep::WokenBySent;
            }
            ++d->runnerSleepsWokenOther;
            if( slept.hasExpired( static_cast<qint64>( waitMs ) ) ) break;
            noteRunnerSleep( true );
            continue;
        }
        if( slept.hasExpired( static_cast<qint64>( waitMs ) ) ) break;
    }
    noteRunnerSleep( false );
    return RunnerSleep::Slept;
#else
    Q_UNUSED( ms );
    return RunnerSleep::NotHandled;
#endif
}

qint64 PlaybackDueWake::lastRunnerSleepDispatchNs() const
{
    return d->runnerSleepDispatchNs;
}

void PlaybackDueWake::dispatchSentMessages()
{
#ifdef Q_OS_WIN
    // r2 G1: first the wakes the runner sleep held, in the order they were sent (each through the gate, as the window
    // procedure would have), then every sent message still pending
    std::vector<MSG> held;
    held.swap( d->heldWakes );
    for( const MSG &msg : held ) handleNativeMessage( &msg );
    dispatchPendingSentMessages();
#endif
}

bool PlaybackDueWake::holdWindowProcWake( const void *message )
{
#ifdef Q_OS_WIN
    if( !d->inRunnerSleepDispatch || !message ) return false;
    const MSG *m = static_cast<const MSG *>( message );
    // r2 adversary pass 5 A1: only a wake the gate would accept now ends the runner sleep. A stale one (cancelled, replaced,
    // stopped, or posted before the current arm's due time), or one not for this window / tag, is not held: the window
    // procedure hands it to handleNativeMessage, which drops it as before, and the sleep goes on (runner_sleep_woken_other).
    if( m->message != kNativeWakeMessage || !d->window || m->hwnd != d->window ) return false;
    const quint64 wParam = static_cast<quint64>( m->wParam );
    if( playbackDueWakeWParamTag( wParam ) != static_cast<quint32>( kNativeWakeTag ) ) return false;
    if( !d->deliveryCurrent( playbackDueWakeWParamGeneration( wParam ), static_cast<qint64>( m->lParam ) ) ) return false;
    d->heldWakes.push_back( *m );
    return true;
#else
    Q_UNUSED( message );
    return false;
#endif
}

quint64 PlaybackDueWake::runnerSleepsWokenBySent() const
{
    return d->runnerSleepsWokenBySent;
}

quint64 PlaybackDueWake::runnerSleepsWokenOther() const
{
    return d->runnerSleepsWokenOther;
}

quint64 PlaybackDueWake::runnerSleepWaitFailures() const
{
    return d->runnerSleepWaitFailed;
}

void PlaybackDueWake::setRunnerSleepWaitFailsForTest( bool fails )
{
    d->failRunnerSleepWaitForTest = fails;
}

const char *PlaybackDueWake::deliveryKindName() const
{
    switch( d->delivery )
    {
    case Delivery::NativeMessage: return d->mode == PlaybackDueWakeDeliveryMode::SendNotify ? "send" : "post";
    case Delivery::Notifier: return "notifier";
    case Delivery::QtTimer: break;
    }
    return "qtimer";
}

void PlaybackDueWake::setGuiTraceActive( bool active )
{
    if( active == d->guiTraceActive ) return;
    if( !active )
    {
        QObject::disconnect( d->traceBlock );
        QObject::disconnect( d->traceAwake );
        d->guiTraceActive = false;
        d->guiTrace.reset();
        return;
    }
    // the calling (GUI) thread's dispatcher; none (a thread without an event loop): no trace, every state reads Unknown
    QAbstractEventDispatcher *dispatcher = QAbstractEventDispatcher::instance();
    if( !dispatcher ) return;
    Impl *impl = d.get();
    d->guiTrace.reset();
    d->traceBlock = QObject::connect( dispatcher, &QAbstractEventDispatcher::aboutToBlock, [impl]() {
        impl->guiTrace.note( PlaybackGuiSleepTrace::State::DispatcherWait, impl->nowMs() );
    } );
    d->traceAwake = QObject::connect( dispatcher, &QAbstractEventDispatcher::awake, [impl]() {
        impl->guiTrace.note( PlaybackGuiSleepTrace::State::Running, impl->nowMs() );
    } );
    d->guiTraceActive = true;
}

bool PlaybackDueWake::guiTraceActive() const
{
    return d->guiTraceActive;
}

void PlaybackDueWake::noteRunnerSleep( bool sleeping )
{
    if( !d->guiTraceActive ) return;
    d->guiTrace.note( sleeping ? PlaybackGuiSleepTrace::State::RunnerSleep : PlaybackGuiSleepTrace::State::Running, d->nowMs() );
}

PlaybackGuiSleepTrace::At PlaybackDueWake::guiStateAt( double ms ) const
{
    return d->guiStateAt( ms );
}

PlaybackDueWake::StrandedCounts PlaybackDueWake::strandedCounts() const
{
    return d->stranded;
}

void PlaybackDueWake::resetStrandedCounts()
{
    d->stranded = StrandedCounts();
    d->runnerSleepsWokenBySent = 0;
    d->runnerSleepsWokenOther = 0;
    d->runnerSleepWaitFailed = 0;
}

bool PlaybackDueWake::armPostPublished() const
{
#ifdef Q_OS_WIN
    const qint64 forBefore = d->postReturnFor.load();
    const quint32 postedGeneration = d->postReturnGeneration.load();
    const qint64 forAfter = d->postReturnFor.load();
    return forBefore > 0 && forAfter == forBefore && postedGeneration == d->gate.generation();
#else
    return false;
#endif
}

bool PlaybackDueWake::enabledByEnvironment()
{
    bool ok = false;
    const int env = qEnvironmentVariableIntValue( "MLVAPP_PLAYBACK_DUE_WAKE", &ok );
    return !( ok && env == 0 );
}

void PlaybackDueWake::beginPlay()
{
#ifdef Q_OS_WIN
    if( !d->handle || d->highResolution || d->periodRaised ) return;
    if( !d->winmm )
    {
        d->winmm = LoadLibraryW( L"winmm.dll" );
        if( d->winmm )
        {
            d->timeBeginPeriodFn = reinterpret_cast<Impl::TimePeriodFn>(
                reinterpret_cast<void *>( GetProcAddress( d->winmm, "timeBeginPeriod" ) ) );
            d->timeEndPeriodFn = reinterpret_cast<Impl::TimePeriodFn>(
                reinterpret_cast<void *>( GetProcAddress( d->winmm, "timeEndPeriod" ) ) );
        }
    }
    if( d->timeBeginPeriodFn && d->timeEndPeriodFn && d->timeBeginPeriodFn( 1 ) == 0 /* TIMERR_NOERROR */ )
        d->periodRaised = true;
#endif
}

void PlaybackDueWake::endPlay()
{
#ifdef Q_OS_WIN
    if( !d->periodRaised ) return;
    if( d->timeEndPeriodFn ) d->timeEndPeriodFn( 1 );
    d->periodRaised = false;
#endif
}

bool PlaybackDueWake::hiResActive() const
{
#ifdef Q_OS_WIN
    // a fallback QTimer arm is not high resolution, whatever the waitable timer was created with (r2 pass 5 A1)
    return d->handle && !d->lastArmQtTimer && ( d->highResolution || d->periodRaised );
#else
    return false;
#endif
}
