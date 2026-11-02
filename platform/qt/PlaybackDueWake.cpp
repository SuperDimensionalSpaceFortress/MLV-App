/*!
 * \file PlaybackDueWake.cpp
 * \brief HFR-NATIVE-M16-1457-GUI-PRESENT-CADENCE-1: see PlaybackDueWake.h.
 */

#include "PlaybackDueWake.h"

#include <QElapsedTimer>
#include <QObject>
#include <QTimer>

#include <cmath>

#ifdef Q_OS_WIN
#include <QWinEventNotifier>
#include <windows.h>
#ifndef CREATE_WAITABLE_TIMER_HIGH_RESOLUTION
#define CREATE_WAITABLE_TIMER_HIGH_RESOLUTION 0x00000002
#endif
#endif

namespace
{
// A delivery more than this before the armed due time belongs to an earlier arm (its signal was already in flight
// when the wake was re-armed): it is dropped, and the re-armed timer delivers again.
constexpr qint64 kStaleDeliveryNs = 500000;
}

struct PlaybackDueWake::Impl
{
    std::function<void()> onFire;
    bool armed = false;
    qint64 dueNs = 0;
    QElapsedTimer clock;
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
#endif

    void deliver()
    {
        if( !armed ) return;
        if( clock.nsecsElapsed() < dueNs - kStaleDeliveryNs ) return;
        armed = false;
        if( onFire ) onFire();
    }
};

PlaybackDueWake::PlaybackDueWake( QObject *owner, std::function<void()> onFire )
    : d( new Impl )
{
    d->onFire = std::move( onFire );
    d->clock.start();
#ifdef Q_OS_WIN
    d->handle = CreateWaitableTimerExW( nullptr, nullptr, CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, TIMER_ALL_ACCESS );
    d->highResolution = d->handle != nullptr;
    if( !d->handle )
        d->handle = CreateWaitableTimerExW( nullptr, nullptr, 0, TIMER_ALL_ACCESS );   // auto-reset, plain
    if( d->handle )
    {
        d->notifier = new QWinEventNotifier( d->handle, owner );
        Impl *impl = d.get();
        QObject::connect( d->notifier, &QWinEventNotifier::activated, owner, [impl]( HANDLE ) { impl->deliver(); } );
        d->notifier->setEnabled( true );
        return;
    }
#endif
    d->timer = new QTimer( owner );
    d->timer->setSingleShot( true );
    d->timer->setTimerType( Qt::PreciseTimer );
    Impl *impl = d.get();
    QObject::connect( d->timer, &QTimer::timeout, owner, [impl]() { impl->deliver(); } );
}

PlaybackDueWake::~PlaybackDueWake()
{
    cancel();
    endPlay();
#ifdef Q_OS_WIN
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

void PlaybackDueWake::arm( double dueMs )
{
    if( !( dueMs > 0.0 ) ) dueMs = 0.0;
    d->armed = true;
    d->dueNs = d->clock.nsecsElapsed() + static_cast<qint64>( dueMs * 1000000.0 );
#ifdef Q_OS_WIN
    if( d->handle )
    {
        LARGE_INTEGER due;
        // relative due, in 100 ns units (negative = relative); at least one unit so the timer always signals
        const LONGLONG units = static_cast<LONGLONG>( std::ceil( dueMs * 10000.0 ) );
        due.QuadPart = -( units > 0 ? units : 1 );
        if( SetWaitableTimer( d->handle, &due, 0, nullptr, nullptr, FALSE ) ) return;
        // could not arm: fall through to a Qt timer for this wake
        if( !d->timer && d->notifier )
        {
            d->timer = new QTimer( d->notifier->parent() );
            d->timer->setSingleShot( true );
            d->timer->setTimerType( Qt::PreciseTimer );
            Impl *impl = d.get();
            QObject::connect( d->timer, &QTimer::timeout, d->notifier->parent(), [impl]() { impl->deliver(); } );
        }
    }
#endif
    if( d->timer ) d->timer->start( static_cast<int>( std::ceil( dueMs ) ) );
}

void PlaybackDueWake::cancel()
{
    d->armed = false;
#ifdef Q_OS_WIN
    if( d->handle ) CancelWaitableTimer( d->handle );
#endif
    if( d->timer ) d->timer->stop();
}

bool PlaybackDueWake::armed() const
{
    return d->armed;
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
    return d->handle && ( d->highResolution || d->periodRaised );
#else
    return false;
#endif
}
