#ifndef PLAYBACKPREPPRESENTATIONPOLICY_H
#define PLAYBACKPREPPRESENTATIONPOLICY_H

#include <cstdint>

// PLAYBACK-CPU-PACE-GAP-1 r3: drop-frame off promises every frame, in order. The prep queue is then lossless (FIFO,
// no conflation by serial) and the playback advance waits for it instead of letting newer frames replace older ones.
// With drop-frame on (the default) nothing here changes: the queue conflates to the latest request as before.
inline bool playbackPrepLosslessActive( bool playing, bool dropFrameMode )
{
    return playing && !dropFrameMode;
}

inline bool playbackPrepTaskShouldCompute( uint64_t taskSerial,
                                           uint64_t latestSerial,
                                           uint64_t taskGeneration,
                                           uint64_t activeGeneration,
                                           bool lossless = false )
{
    return ( lossless || taskSerial == latestSerial ) && taskGeneration == activeGeneration;
}

// A newer prep task or finished result replaces the older ones only when the new work is not lossless.
inline bool playbackPrepConflatesOlderWork( bool lossless )
{
    return !lossless;
}

// PLAYBACK-CPU-PACE-GAP-1 r7 FR1: drop-frame-on work goes through master's single conflated slot (copy in, copy out),
// exactly the pre-#397 code, so the default path times as it did before. The deque (moved in and out) serves lossless
// work, and drop-frame-on work only under the MLVAPP_PLAYBACK_PREP_CONFLATED_MOVE bisect opt-in.
inline bool playbackPrepUsesConflatedSlot( bool lossless, bool conflatedMoveOptIn )
{
    return !lossless && !conflatedMoveOptIn;
}

inline bool playbackPrepCompletedResultShouldPresent( uint64_t taskGeneration,
                                                       uint64_t activeGeneration )
{
    // A newer serial may arrive while expensive prep is running. The finished
    // frame is still safe to present when its display generation is current;
    // rejecting it by serial can starve continuous playback indefinitely.
    return taskGeneration == activeGeneration;
}

namespace playback_drop_off
{

// Frames held by the prep stage (queued + computing + finished but not yet presented) at which the advance waits.
inline constexpr int kPrepMaxInFlight = 2;
// Liveness backstop: no hold lasts longer than this, so a frame that never arrives cannot stall playback.
inline constexpr double kAdvanceHoldMaxMs = 1000.0;

// With drop-frame off the timeline may not move past a frame that will not be shown: hold while the prep stage is
// full, or while the current target is rendered but not yet taken by drawFrameReady (moving on would prune it).
inline bool advanceHeld( bool dropOffPlayback, int prepBacklog, bool targetReadyUnacquired )
{
    return dropOffPlayback && ( prepBacklog >= kPrepMaxInFlight || targetReadyUnacquired );
}

// Bounds every hold by kAdvanceHoldMaxMs on the caller's clock and counts holds and timeouts for the smoke summary.
struct AdvanceHold
{
    double sinceMs = -1.0;
    uint64_t holds = 0;
    uint64_t timeouts = 0;

    bool hold( bool held, double nowMs )
    {
        if( !held )
        {
            sinceMs = -1.0;
            return false;
        }
        if( sinceMs < 0.0 )
        {
            sinceMs = nowMs;
            ++holds;
        }
        if( nowMs - sinceMs < kAdvanceHoldMaxMs ) return true;
        ++timeouts;
        sinceMs = -1.0;
        return false;
    }

    void resetCounters()
    {
        sinceMs = -1.0;
        holds = 0;
        timeouts = 0;
    }
};

} // namespace playback_drop_off

#endif
