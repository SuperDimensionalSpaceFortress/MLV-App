/*!
 * \file PlaybackNativePaceGuard.h
 * \brief Pure playback-clock arithmetic, extracted so it can be unit-tested with
 *        an injected clock and a fake renderer, without the GUI.
 *
 * PLAYBACK-CUDA-NATIVE-PACE-1: the timeline must never advance faster than the
 * pace fps (the clip's native fps, or the explicit fpsOverride). On the CUDA
 * texture route drawFrameReady() calls timerFrameEvent( true ) as soon as a frame
 * is presented, and shapedTickTimeDiffMs() rounds a short predictive tick UP to a
 * whole frame period so the next render is dispatched early. Each present
 * therefore advanced one source frame however little wall time had passed: a
 * renderer faster than native (RTX 4090, scale 1) ran the timeline at its render
 * rate (31.6 timeline fps on a 23.976 clip), while a slower one (RTX 3060) never
 * showed it. The CPU route never asks for a predictive advance, so it was paced.
 *
 * NativePaceGuard is the ceiling on every engine advance, whatever path asked for
 * it: credit accrues at pace fps per wall second, an advance spends it, and at
 * most kMaxCarryFrames is carried past a tick. So the source frames advanced
 * since Play are at most kMaxCarryFrames + elapsed x pace fps, and a stall is
 * never repaid by running faster than native. It never slows a renderer that is
 * slower than native: there the credit is always ahead of the request, and drop
 * frame mode keeps jumping (or holding) exactly as before.
 */

#ifndef PLAYBACKNATIVEPACEGUARD_H
#define PLAYBACKNATIVEPACEGUARD_H

namespace playback_native_pace
{

/*! The most credit carried past a tick: one frame, so the first tick after Play
 *  advances at once (as before) and a held frame can be caught up by one. */
inline constexpr double kMaxCarryFrames = 1.0;

/*! timerFrameEvent()'s tick shaping, moved here verbatim: a predictive tick that
 *  came early is rounded UP to a frame period (so it can dispatch the next render
 *  early), and a pending advance after a render stall is cut DOWN to one frame
 *  period (no giant catch-up step). The round-up alone would outrun native pace;
 *  NativePaceGuard is what bounds it. */
inline int shapedTickTimeDiffMs( int elapsedMs, double framerate, bool predictiveAdvance,
                                 bool playing, bool hadPendingAdvance )
{
    const double targetFrameMs = 1000.0 / ( framerate > 1.0 ? framerate : 1.0 );
    const int floorMs = static_cast<int>( targetFrameMs ) > 1 ? static_cast<int>( targetFrameMs ) : 1;
    const int ceilMs = static_cast<int>( targetFrameMs + 0.999 ) > 1 ? static_cast<int>( targetFrameMs + 0.999 ) : 1;
    int timeDiff = elapsedMs;
    if( predictiveAdvance && playing && timeDiff < targetFrameMs )
    {
        timeDiff = ceilMs;
    }
    if( hadPendingAdvance && timeDiff > targetFrameMs )
    {
        timeDiff = predictiveAdvance ? ceilMs : floorMs;
    }
    return timeDiff;
}

/*! PLAYBACK-PACE-GUARD-THROUGHPUT-1: the rate \a frames ran at over the PACED part of a Play, from the first
 *  present to \a elapsedMs (both measured from Play). The smoke's timeline_fps divides by the whole elapsed time,
 *  so it also counts the wait for the first frame, which no pace governs: UM r5 (dd15567b) waited 3753 ms of a
 *  28545 ms run, so 599 frames read 20.985 fps over the run.
 *  \a frames must be the frames advanced AFTER the first present (see timelineFramesAfterFirstPresent), never the
 *  whole run's: dividing the run's frames by the post-first-present time counts what the engine repaid for the wait
 *  as paced throughput (round 1 of this card read r5 as 24.16, native; its contact sheet shows 21.3).
 *  0 when there is no paced interval: \a presentedFrames (the count, never the first-present time) says whether
 *  anything was presented, and an interval of zero length has no rate. */
inline double fpsAfterFirstPresent( double frames, double elapsedMs, double firstPresentMs, int presentedFrames )
{
    if( presentedFrames < 1 ) return 0.0;
    const double pacedMs = elapsedMs - firstPresentMs;
    return pacedMs > 0.0 ? frames * 1000.0 / pacedMs : 0.0;
}

/*! The timeline (slider) frames advanced after the first present: the run's travel less the travel already made when
 *  the first present was recorded. In drop-frame mode the first present's early advance can request the whole wait
 *  for the first frame at once, so that jump (first_present_catchup_frames) is made before the first present is
 *  recorded and must not count as paced throughput. Never negative. */
inline int timelineFramesAfterFirstPresent( int timelineDeltaAbs, int timelineDeltaAbsAtFirstPresent )
{
    return timelineDeltaAbs > timelineDeltaAbsAtFirstPresent ? timelineDeltaAbs - timelineDeltaAbsAtFirstPresent : 0;
}

class NativePaceGuard
{
public:
    /*! Forget the clock: the next grant re-arms it (Play start, Play stop). */
    void reset()
    {
        m_armed = false;
        m_lastMs = 0.0;
        m_creditFrames = 0.0;
    }

    bool armed() const { return m_armed; }
    double creditFrames() const { return m_creditFrames; }

    /*! Drop-frame mode: the share of \a requestedFrames (a fractional source-frame
     *  advance) the wall clock allows at \a nowMs, spent from the credit. A pace
     *  that is not positive is unknown and leaves the request unchanged. */
    double grant( double requestedFrames, double nowMs, double paceFps )
    {
        if( !( paceFps > 0.0 ) ) return requestedFrames;
        accrue( nowMs, paceFps );
        double granted = requestedFrames;
        if( granted < 0.0 ) granted = 0.0;
        if( granted > m_creditFrames + kEpsilonFrames ) granted = m_creditFrames;
        spend( granted );
        return granted;
    }

    /*! Normal (every-frame) mode: true when one whole source frame may advance at
     *  \a nowMs (and spends it); false holds the current frame. */
    bool grantWholeFrame( double nowMs, double paceFps )
    {
        if( !( paceFps > 0.0 ) ) return true;
        accrue( nowMs, paceFps );
        if( m_creditFrames + kEpsilonFrames < 1.0 ) return false;
        spend( 1.0 );
        return true;
    }

    /*! The tick at the cut range's last frame with Loop on (either mode): going back to cut-in is a frame
     *  transition like any other, so it spends a whole frame of credit; false holds the last frame. Unpaid,
     *  every lap ran one frame period short (cut 1..24 at 23.976 with a 32 ms render: 25.025 fps). */
    bool grantLoopWrap( double nowMs, double paceFps )
    {
        return grantWholeFrame( nowMs, paceFps );
    }

private:
    static constexpr double kEpsilonFrames = 1e-9;

    void accrue( double nowMs, double paceFps )
    {
        if( !m_armed )
        {
            m_armed = true;
            m_lastMs = nowMs;
            m_creditFrames = kMaxCarryFrames;
            return;
        }
        if( nowMs > m_lastMs )
        {
            m_creditFrames += ( nowMs - m_lastMs ) * paceFps / 1000.0;
            m_lastMs = nowMs;
        }
    }

    void spend( double frames )
    {
        m_creditFrames -= frames;
        if( m_creditFrames < 0.0 ) m_creditFrames = 0.0;
        if( m_creditFrames > kMaxCarryFrames ) m_creditFrames = kMaxCarryFrames;
    }

    bool m_armed = false;
    double m_lastMs = 0.0;
    double m_creditFrames = 0.0;
};

} // namespace playback_native_pace

#endif // PLAYBACKNATIVEPACEGUARD_H
