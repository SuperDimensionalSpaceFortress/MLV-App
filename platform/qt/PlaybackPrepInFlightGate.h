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

private:
    std::condition_variable m_cv;
    bool m_inFlight = false;
    uint64_t m_serial = 0;
};

#endif
