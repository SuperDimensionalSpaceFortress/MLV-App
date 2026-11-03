/*!
 * \file PlaybackDueWake.cpp
 * \brief HFR-NATIVE-M16-1457-GUI-PRESENT-CADENCE-1: see PlaybackDueWake.h.
 */

#include "PlaybackDueWake.h"

#include <QAbstractNativeEventFilter>
#include <QByteArray>
#include <QCoreApplication>
#include <QElapsedTimer>
#include <QObject>
#include <QTimer>

#include <atomic>
#include <cmath>

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
    QAbstractNativeEventFilter *filter = nullptr;
#endif

    // the notifier / Qt timer paths: the signal belongs to whatever is armed now (the gate drops an early one)
    void deliverCurrent()
    {
        if( gate.accept( gate.generation(), clock.nsecsElapsed() ) && onFire ) onFire();
    }
    // r3: the post time of the delivery firing now (ms on the QPC clock), < 0 when the delivery carries none
    double firingPostMs = -1.0;
    // the native-message path: the message names its generation (and, r3, its post time)
    void deliverGeneration( quint32 generation, double postMs = -1.0 )
    {
        if( !gate.accept( generation, clock.nsecsElapsed() ) || !onFire ) return;
        firingPostMs = postMs;
        onFire();
        firingPostMs = -1.0;
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
            wake->handleNativeMessage( &msg );
        }
        return 0;
    }
    return DefWindowProcW( hwnd, message, wParam, lParam );
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
};

DWORD WINAPI dueWakeWaitThread( LPVOID parameter )
{
    WaitThreadArgs args = *static_cast<WaitThreadArgs *>( parameter );
    delete static_cast<WaitThreadArgs *>( parameter );
    // the stop event is first: it wins over a timer signalled at the same instant
    const HANDLE handles[2] = { args.stopEvent, args.timer };
    for( ;; )
    {
        const DWORD result = WaitForMultipleObjects( 2, handles, FALSE, INFINITE );
        if( result != WAIT_OBJECT_0 + 1 ) break;   // stop, or a wait failure
        // r3: stamp the post time, so the GUI thread can split due-to-post from post-to-receive
        LARGE_INTEGER postQpc;
        if( !QueryPerformanceCounter( &postQpc ) ) postQpc.QuadPart = 0;
        PostMessageW( args.window, PlaybackDueWake::kNativeWakeMessage,
                      static_cast<WPARAM>( playbackDueWakePackWParam(
                          args.postGeneration->load( std::memory_order_acquire ),
                          static_cast<quint32>( PlaybackDueWake::kNativeWakeTag ) ) ),
                      static_cast<LPARAM>( postQpc.QuadPart ) );
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
    : d( new Impl )
{
    d->onFire = std::move( onFire );
    d->clock.start();
#ifdef Q_OS_WIN
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
        }
        if( d->stopEvent )
        {
            WaitThreadArgs *args = new WaitThreadArgs { d->stopEvent, d->handle, d->window, &d->postGeneration };
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
        if( d->window ) DestroyWindow( d->window );
        d->window = nullptr;
    }
    if( d->handle )
    {
        d->delivery = Delivery::Notifier;
        d->notifier = new QWinEventNotifier( d->handle, owner );
        Impl *impl = d.get();
        QObject::connect( d->notifier, &QWinEventNotifier::activated, d->notifier, [impl]( HANDLE ) { impl->deliverCurrent(); } );
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
    QObject::connect( d->timer, &QTimer::timeout, owner, [impl]() { impl->deliverCurrent(); } );
}

PlaybackDueWake::~PlaybackDueWake()
{
    // the wait thread is joined first, so it never posts to the window destroyed below
    stop();
    cancel();
    endPlay();
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
    if( d->handle ) CancelWaitableTimer( d->handle );
#endif
    if( d->timer ) d->timer->stop();
    d->gate.cancel();
}

void PlaybackDueWake::arm( double dueMs )
{
    if( d->stopped ) return;
    if( !( dueMs > 0.0 ) ) dueMs = 0.0;
    const quint32 generation = d->gate.arm( d->clock.nsecsElapsed(), static_cast<qint64>( dueMs * 1000000.0 ) );
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
        d->lastArmQtTimer = true;
        if( !d->timer )
        {
            QObject *owner = d->notifier ? d->notifier->parent() : nullptr;
            d->timer = new QTimer( owner );
            d->timer->setSingleShot( true );
            d->timer->setTimerType( Qt::PreciseTimer );
            Impl *impl = d.get();
            QObject::connect( d->timer, &QTimer::timeout, [impl]() { impl->deliverCurrent(); } );
        }
    }
#else
    Q_UNUSED( generation );
#endif
    if( d->timer ) d->timer->start( static_cast<int>( std::ceil( dueMs ) ) );
}

void PlaybackDueWake::cancel()
{
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
    // r3: the post time in the message, on the same QPC clock as mlv_stage_timing_now()
    double postMs = -1.0;
    LARGE_INTEGER frequency;
    if( m->lParam > 0 && QueryPerformanceFrequency( &frequency ) && frequency.QuadPart > 0 )
        postMs = static_cast<double>( m->lParam ) * 1000.0 / static_cast<double>( frequency.QuadPart );
    // a stale generation (cancelled, re-armed or stopped since it was posted) is consumed and does nothing
    d->deliverGeneration( playbackDueWakeWParamGeneration( wParam ), postMs );
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
