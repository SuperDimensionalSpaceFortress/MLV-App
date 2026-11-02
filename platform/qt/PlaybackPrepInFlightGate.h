#ifndef PLAYBACKPREPINFLIGHTGATE_H
#define PLAYBACKPREPINFLIGHTGATE_H

#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <mutex>

// CPU-SCOPE-LIVE-HEAP-CORRUPTION-1 F2: the prep worker's in-flight record. Once the
// worker takes a task out of the pending slot, invalidatePlaybackPrepForDisplayChange
// can no longer see it, yet the task still borrows the pinned render slot and the core
// processing state (config.gradientMaskData points into processing->gradient_mask). A
// core mutation must wait for the worker to leave that section first.
//
// Every member requires the caller to hold the queue mutex the task is handed over
// under (MainWindow::m_playbackPrepMutex), so a drain that runs after the hand-off
// always sees the task in flight. The gate has its own condition variable so a drain
// never competes with the worker's wait for new tasks.
class PlaybackPrepInFlightGate
{
public:
    void beginLocked( uint64_t serial )
    {
        m_inFlight = true;
        m_serial = serial;
    }

    // Call after the worker's last access to borrowed task memory.
    void endLocked()
    {
        m_inFlight = false;
        m_cv.notify_all();
    }

    bool inFlightLocked() const { return m_inFlight; }
    uint64_t serialLocked() const { return m_serial; }

    // Waits on `lock` (holding the queue mutex) until no task is in flight. Returns
    // false if the worker is still inside its section after timeoutMs.
    bool drainLocked( std::unique_lock<std::mutex> &lock, int timeoutMs )
    {
        return m_cv.wait_for( lock,
                              std::chrono::milliseconds( timeoutMs > 0 ? timeoutMs : 0 ),
                              [this]{ return !m_inFlight; } );
    }

    // r9 (adversary A1): the drain a core mutation uses. A timed-out wait must not let the
    // mutation go on while the worker may still read what it frees (gradientMaskData). Waits
    // timeoutMs; if the task is still in flight, calls onTimeout() with `lock` released (the
    // caller logs), then waits until the worker leaves. Returns only with nothing in flight;
    // true if the bounded wait was enough. The worker's section takes no lock the GUI holds.
    template <class OnTimeout>
    bool drainForCoreMutationLocked( std::unique_lock<std::mutex> &lock, int timeoutMs, OnTimeout &&onTimeout )
    {
        if( drainLocked( lock, timeoutMs ) ) return true;
        lock.unlock();
        onTimeout();
        lock.lock();
        m_cv.wait( lock, [this]{ return !m_inFlight; } );
        return false;
    }

private:
    std::condition_variable m_cv;
    bool m_inFlight = false;
    uint64_t m_serial = 0;
};

// CPU-SCOPE-PLAYSTOP-LIFETIME-HARDENING-1 r2 (CoS B1): one drain, and what it allows.
struct PlaybackPrepDrainOutcome
{
    bool waited = false;   // a task was in flight when the drain began
    bool drained = true;   // false: still in flight when the budget ran out
    uint64_t serial = 0;
};

// Takes the queue mutex, then waits (up to timeoutMs) for the in-flight task to leave.
inline PlaybackPrepDrainOutcome playbackPrepDrainInFlight( std::mutex &queueMutex,
                                                           PlaybackPrepInFlightGate &gate,
                                                           int timeoutMs )
{
    PlaybackPrepDrainOutcome outcome;
    std::unique_lock<std::mutex> lk( queueMutex );
    outcome.waited = gate.inFlightLocked();
    outcome.serial = gate.serialLocked();
    if( outcome.waited ) outcome.drained = gate.drainLocked( lk, timeoutMs );
    return outcome;
}

// Whether a core mutation that frees or reallocates memory an in-flight task borrows
// may run after `outcome`. Clip replacement is one: setMlvProcessing reallocs
// processing->gradient_mask (the task's config.gradientMaskData) and
// RenderFrameThread::init re-assigns every slot buffer (the task's readyFrame
// pointers). After playbackPrepDrainInFlight times out the task may still be reading
// either. MainWindow's drain never returns that way (it blocks until the worker has
// left), but drained=false still means the caller aborts (or defers) the mutation.
inline bool playbackPrepBorrowFreeingMutationAllowed( const PlaybackPrepDrainOutcome &outcome )
{
    return outcome.drained;
}

#endif
