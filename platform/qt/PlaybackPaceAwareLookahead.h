/*!
 * \file PlaybackPaceAwareLookahead.h
 * \brief Pure pieces of PLAYBACK-CPU-PACE-AWARE-LOOKAHEAD-1, kept GUI-free so they can be unit-tested
 *        with an injected pace clock.
 *
 * On the CPU route with drop-frame on, the lookahead rendered N+1 (MainWindow::queuePlaybackLookaheadRequests)
 * while the drop target the pace clock picks next is often N+2: a busy tick sets the pending flag and the next
 * advance is capped to about one frame (shapedTickTimeDiffMs), but a READY lookahead hit leaves no pending flag,
 * so the next advance is uncapped and strides 2 or more. r3 (Bachelor s2, la 1) pruned half the lookaheads
 * unshown, and their recon ran on the single FIFO recon worker ahead of the frame that was shown.
 *
 * This header holds:
 *  - predictNextDropTarget(): the next drop target, from the same pure pieces playbackHandling() uses
 *    (shapedTickTimeDiffMs, NativePaceGuard::grant on a COPY of the guard, advanceDropFrameTick). It never
 *    calls playbackHandling(), which moves the slider, the guard, the histograms and m_sourceAdvance.
 *  - cpuLookaheadPlan(): whether the prediction and the yield are engaged. Depth 0 engages neither.
 *  - playbackFrameWithinLookaheadWindow() (moved here from RenderFrameThread.cpp unchanged), and the yield rules
 *    built on it: target-first selection, the stale in-flight release, and the lookahead-only exclusive start.
 */

#ifndef PLAYBACKPACEAWARELOOKAHEAD_H
#define PLAYBACKPACEAWARELOOKAHEAD_H

#include "PlaybackDecodeRenderOverlap.h"
#include "PlaybackFrameRange.h"
#include "PlaybackLookaheadLoopPositionPolicy.h"
#include "PlaybackNativePaceGuard.h"

#include <algorithm>
#include <cstddef>
#include <cstdint>

namespace playback_pace_lookahead
{

/*! Which parts of this card run for a lookahead request. */
struct LookaheadPlan
{
    bool predict = false; //!< offset 1 renders the predicted drop target (else N+1)
    bool yield = false;   //!< the render thread applies the yield rules to lookahead work
};

/*! The CPU route only: the texture route keeps N+1..N+depth and FIFO. Drop-frame off keeps N+1 because every frame
 *  is presented there (PLAYBACK-CPU-PACE-GAP-1 D1, the lossless path). Depth 0 engages nothing, so it never reaches
 *  the predictor or a yield branch. \a predictEnabled / \a yieldEnabled are MLVAPP_CPU_LOOKAHEAD_PREDICT and
 *  MLVAPP_CPU_LOOKAHEAD_YIELD (unset = on, 0 = off). */
inline LookaheadPlan cpuLookaheadPlan( int depth, bool textureRoute, bool dropFrame,
                                       bool predictEnabled, bool yieldEnabled )
{
    LookaheadPlan plan;
    if( depth <= 0 || textureRoute ) return plan;
    plan.predict = dropFrame && predictEnabled;
    plan.yield = yieldEnabled;
    return plan;
}

/*! queuePlaybackLookaheadRequests' wrap of a frame past the cut-out: Loop wraps it by the cut range; without Loop it
 *  stays past the cut-out (the queue then issues no lookahead). Frames are 0-based. */
inline int wrapAheadFrame( int rawFrame, int cutInFrame, int cutOutFrame, bool loop )
{
    if( rawFrame <= cutOutFrame || !loop ) return rawFrame;
    return PlaybackLookaheadLoopPositionPolicy::wrap( rawFrame, cutInFrame, cutOutFrame - cutInFrame + 1 ).wrappedFrame;
}

/*! How far \a frame is ahead of \a target in playbackFrameWithinLookaheadWindow's metric (with Loop, a frame behind
 *  the target is a lap ahead). Negative or 0 is not ahead. */
inline int lookaheadDistance( int target, int frame, int cutInFrame, int cutOutFrame, bool loop )
{
    const int distance = frame - target;
    if( distance >= 0 || !loop ) return distance;
    const int loopSpan = cutOutFrame - cutInFrame + 1;
    return loopSpan > 0 ? distance + loopSpan : distance;
}

/*! #402 (PLAYBACK-STARTUP-CATCHUP-BURST-1) adds NativePaceGuard::awaitingFirstPresent(). This reads it when the
 *  guard has it and is false otherwise, so the predictor builds before and after #402 lands. */
template<typename Guard>
inline auto guardAwaitingFirstPresent( const Guard &guard, int ) -> decltype( static_cast<bool>( guard.awaitingFirstPresent() ) )
{
    return guard.awaitingFirstPresent();
}
template<typename Guard>
inline bool guardAwaitingFirstPresent( const Guard &, long )
{
    return false;
}

struct Prediction
{
    int frame = -1;        //!< the frame offset 1 renders (0-based)
    bool fallback = true;  //!< currentTarget + 1 (wrapped), not a prediction
};

/*! The drop target playbackHandling() will pick at the next advance, about \a expectedIntervalMs from \a nowMs.
 *  \a guardCopy is a COPY of m_playbackPaceGuard: granting on it leaves the caller's guard as it was.
 *  \a expectPendingAdvance: the next advance follows busy ticks (true unless this request was a READY lookahead
 *  hit), so shapedTickTimeDiffMs caps it to one frame period. \a cutInValue / \a cutOutValue are the 1-based
 *  spin-box values advanceDropFrameTick takes. Falls back to currentTarget + 1 (wrapped as the queue wraps) when the
 *  guard is not armed, is still waiting for this Play's first present (#402), the interval is unknown, or the
 *  result is not ahead of \a currentTarget. The predicted distance is clamped to kMaxLookaheadFrames. */
template<typename Guard>
inline Prediction predictNextDropTarget( double positionNow, Guard guardCopy, double nowMs, double expectedIntervalMs,
                                         double paceFps, bool expectPendingAdvance, int cutInValue, int cutOutValue,
                                         bool loop, int currentTarget )
{
    const int cutInFrame = cutInValue - 1;
    const int cutOutFrame = cutOutValue - 1;
    Prediction prediction;
    prediction.frame = wrapAheadFrame( currentTarget + 1, cutInFrame, cutOutFrame, loop );
    if( !guardCopy.armed() || guardAwaitingFirstPresent( guardCopy, 0 ) || !( expectedIntervalMs > 0.0 ) )
    {
        return prediction;
    }
    const int timeDiff = playback_native_pace::shapedTickTimeDiffMs( static_cast<int>( expectedIntervalMs ), paceFps,
                                                                     false, true, expectPendingAdvance );
    const double granted = guardCopy.grant( paceFps * static_cast<double>( timeDiff ) / 1000.0,
                                            nowMs + expectedIntervalMs, paceFps );
    const playback_frame_range::DropFrameTickResult tick =
        playback_frame_range::advanceDropFrameTick( positionNow, granted, cutInValue, cutOutValue, loop );
    const int predicted = static_cast<int>( tick.position );   // the slider's setValue truncation
    if( !tick.wrapped && predicted <= currentTarget ) return prediction;
    const int distance = lookaheadDistance( currentTarget, predicted, cutInFrame, cutOutFrame, loop );
    if( distance < 1 ) return prediction;
    prediction.fallback = false;
    prediction.frame = distance > playback_overlap::kMaxLookaheadFrames
        ? wrapAheadFrame( currentTarget + playback_overlap::kMaxLookaheadFrames, cutInFrame, cutOutFrame, loop )
        : predicted;
    return prediction;
}

/*! The card's named entry point: predictNextDropTarget()'s frame for the production guard. */
inline int predictedNextDropTarget( double positionNow, playback_native_pace::NativePaceGuard guardCopy,
                                    double nowMs, double expectedIntervalMs, double paceFps,
                                    bool expectPendingAdvance, int cutInValue, int cutOutValue, bool loop,
                                    int currentTarget )
{
    return predictNextDropTarget( positionNow, guardCopy, nowMs, expectedIntervalMs, paceFps, expectPendingAdvance,
                                  cutInValue, cutOutValue, loop, currentTarget ).frame;
}

/*! True when \a frameNumber is inside the forward window [target, target + lookaheadDepth] (Loop-aware), the window
 *  RenderFrameThread::prunePlaybackLookaheadOutsideForwardWindow keeps. Moved here from RenderFrameThread.cpp. */
inline bool playbackFrameWithinLookaheadWindow( uint32_t frameNumber,
                                                int activePlaybackTarget,
                                                int cutInFrame,
                                                int cutOutFrame,
                                                int lookaheadDepth,
                                                bool loopEnabled )
{
    if( activePlaybackTarget < 0 || lookaheadDepth < 0 ) return true;

    const int frame = static_cast<int>( frameNumber );
    const int depth = std::max( 0, lookaheadDepth );
    if( frame == activePlaybackTarget ) return true;

    if( cutOutFrame < cutInFrame )
    {
        return frame >= activePlaybackTarget
            && frame <= activePlaybackTarget + depth;
    }

    const int cutIn = std::max( 0, cutInFrame );
    const int cutOut = std::max( cutIn, cutOutFrame );
    if( activePlaybackTarget < cutIn || activePlaybackTarget > cutOut
     || frame < cutIn || frame > cutOut )
    {
        return frame >= activePlaybackTarget
            && frame <= activePlaybackTarget + depth;
    }

    if( !loopEnabled )
    {
        return frame >= activePlaybackTarget
            && frame <= std::min( cutOut, activePlaybackTarget + depth );
    }

    const int loopSpan = cutOut - cutIn + 1;
    if( loopSpan <= 0 ) return frame == activePlaybackTarget;

    int distance = frame - activePlaybackTarget;
    if( distance < 0 ) distance += loopSpan;
    return distance >= 0 && distance <= depth;
}

/*! The forward window the GUI last passed to prunePlaybackLookaheadOutsideForwardWindow. \a active is the yield:
 *  the CPU-route lookahead is engaged for \a generation (cpuLookaheadPlan().yield). */
struct ForwardWindow
{
    bool active = false;
    uint64_t generation = 0;
    int target = -1;
    int cutInFrame = 0;
    int cutOutFrame = -1;
    int span = 0;
    bool loop = false;
};

/*! Yield (b): release an in-flight lookahead at a stage hand-off when its generation is older than the window's,
 *  or it is outside the window. Never the target (always inside), never a non-lookahead request, never a newer
 *  generation (the window has not caught up with it yet), and nothing when the yield is off. */
inline bool abandonInFlightLookahead( const ForwardWindow &window, bool playbackLookahead,
                                      uint64_t generation, uint32_t frameNumber )
{
    if( !window.active || !playbackLookahead ) return false;
    if( generation < window.generation ) return true;
    if( generation != window.generation ) return false;
    return !playbackFrameWithinLookaheadWindow( frameNumber, window.target, window.cutInFrame, window.cutOutFrame,
                                                window.span, window.loop );
}

/*! Yield (c): a lookahead recon starts only when the render thread has nothing to render (the exclusive start,
 *  playback_overlap::reconMayStart( true, ... )); every other recon keeps today's gate (\a exclusive, the opt-in
 *  MLVAPP_PLAYBACK_OVERLAP_RECON_EXCLUSIVE). So the gate can delay a lookahead, never the shown frame. */
inline bool reconMayStartFor( bool playbackLookahead, bool yieldActive, bool exclusive,
                              bool renderInFlight, bool renderWorkPending )
{
    return playback_overlap::reconMayStart( exclusive || ( yieldActive && playbackLookahead ),
                                            renderInFlight, renderWorkPending );
}

/*! Yield (a): the index of the entry to take from a FIFO \a queue, or -1 to wait. The first non-lookahead entry
 *  goes first (when \a targetMayStart); a lookahead entry is taken only when no other work is queued and
 *  \a lookaheadMayStart, so it never jumps the shown frame. */
template<typename Queue, typename IsLookahead>
inline int targetFirstIndex( const Queue &queue, IsLookahead isLookahead, bool targetMayStart, bool lookaheadMayStart )
{
    int firstLookahead = -1;
    for( std::size_t i = 0; i < queue.size(); ++i )
    {
        if( !isLookahead( queue[i] ) ) return targetMayStart ? static_cast<int>( i ) : -1;
        if( firstLookahead < 0 ) firstLookahead = static_cast<int>( i );
    }
    return lookaheadMayStart ? firstLookahead : -1;
}

} // namespace playback_pace_lookahead

#endif // PLAYBACKPACEAWARELOOKAHEAD_H
