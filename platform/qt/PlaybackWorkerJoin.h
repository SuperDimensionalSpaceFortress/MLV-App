/*!
 * \file PlaybackWorkerJoin.h
 * \brief Bounded hand-back and join of a worker thread (QtCore only, unit-tested).
 *
 * PLAYBACK-STARTUP-WAIT-PREWARM-1 r2: AudioPlayback::shutdownWorkerThread used a BlockingQueuedConnection and an
 * unbounded QThread::wait(), so a WASAPI call hung on the audio thread hung ~MainWindow. Both waits are bounded now;
 * on a timeout the caller logs it and must leave the object alive (the worker may still run its queued work).
 */

#ifndef PLAYBACKWORKERJOIN_H
#define PLAYBACKWORKERJOIN_H

#include <QDeadlineTimer>
#include <QMetaObject>
#include <QObject>
#include <QSemaphore>
#include <QThread>

#include <memory>
#include <utility>

namespace playback_worker_join
{

/*! The audio thread gets this long to reset the engine and hand the object back, and then to finish. */
inline constexpr int kHandBackTimeoutMs = 3000;
inline constexpr int kJoinTimeoutMs = 2000;

/*! Queue \a work on \a context's thread and wait at most \a timeoutMs for it. True when it ran in time. */
template <typename F>
bool runBounded( QObject *context, F &&work, int timeoutMs )
{
    auto done = std::make_shared<QSemaphore>( 0 );
    QMetaObject::invokeMethod( context, [work = std::forward<F>( work ), done]() mutable
    {
        work();
        done->release();
    }, Qt::QueuedConnection );
    return done->tryAcquire( 1, timeoutMs );
}

/*! Ask \a thread to quit and wait at most \a timeoutMs. True when it finished. */
inline bool joinBounded( QThread *thread, int timeoutMs )
{
    thread->quit();
    return thread->wait( QDeadlineTimer( timeoutMs ) );
}

} // namespace playback_worker_join

#endif // PLAYBACKWORKERJOIN_H
