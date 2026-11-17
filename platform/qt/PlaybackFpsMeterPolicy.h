/*!
 * \file PlaybackFpsMeterPolicy.h
 * \brief Pure arithmetic for the status-bar playback fps meter, extracted so it
 *        can be unit-tested without the GUI.
 *
 * The interval the meter smooths is the time between two DRAWS, not between two
 * timer polls: the playback timer polls every 8 ms (mlvappPlaybackTimerIntervalMs),
 * so most ticks draw nothing, and a meter that restarts on every idle tick reads
 * the poll period (~11 ms, "90 fps") whatever rate the timeline actually runs at.
 */

#ifndef PLAYBACKFPSMETERPOLICY_H
#define PLAYBACKFPSMETERPOLICY_H

namespace playback_fps_meter
{

/*! No draw for longer than this and the meter reads 0 and restarts. */
inline constexpr int kFpsMeterStallMs = 500;

/*! The status fps text (and the corner label's rate) is re-rendered at most this often. */
inline constexpr int kFpsTextUpdateMs = 250;

/*! \return true when no draw has happened for long enough that the meter
 *  should read 0 and restart (paused, stalled, or a fresh clip). A negative
 *  age is stalled too: the meter's QTime is wall clock, so across midnight (or
 *  a clock step back) msecsTo() reads about -86.4 million and never passes the limit. */
inline bool fpsMeterStalled( int msSinceLastDraw )
{
    return msSinceLastDraw < 0 || msSinceLastDraw > kFpsMeterStallMs;
}

/*! \return true when the fps text is due a refresh. A negative age (wall-clock
 *  wrap or step back) is due, or the gate would hold the old text for a day. */
inline bool fpsTextUpdateDue( int msSinceLastUpdate )
{
    return msSinceLastUpdate < 0 || msSinceLastUpdate >= kFpsTextUpdateMs;
}

/*! \return true when the meter must read 0 and restart: playback is paused, no
 *  draw has happened yet, or the last draw is stale. Evaluated on EVERY poll
 *  tick, including the ticks a still-drawing render turns away early. */
inline bool fpsMeterShouldReset( bool playChecked, bool hasDraw, int msSinceLastDraw )
{
    return !playChecked || !hasDraw || fpsMeterStalled( msSinceLastDraw );
}

/*! One meter step: fold the interval since the previous draw into the running
 *  frame-time average. A non-positive interval (same millisecond, clock wrap)
 *  leaves the average alone; an interval past the stall limit is a restart
 *  (the average is reseeded from it rather than smoothed into stale data). */
inline double smoothedFrameMs( double emaFrameMs, int msSinceLastDraw )
{
    if( msSinceLastDraw <= 0 ) return emaFrameMs;
    if( emaFrameMs <= 0.0 || fpsMeterStalled( msSinceLastDraw ) )
    {
        return static_cast<double>( msSinceLastDraw );
    }
    return ( emaFrameMs * 0.9 ) + ( static_cast<double>( msSinceLastDraw ) * 0.1 );
}

/*! LOOK-LABEL-PLAYBACK-INFO-1 r12 (adversary pass 13 A2 on c58f5bd8): the interval a draw feeds the meter. The first draw
 *  after a Play toggle feeds none (0, which smoothedFrameMs ignores): the previous draw belongs to the previous Play, so a
 *  Stop -> Play within the stall limit never smooths the cross-Play gap into the new Play's rate. */
inline int meterIntervalMs( bool firstDrawOfPlay, bool hasPreviousDraw, int msSincePreviousDraw )
{
    return ( firstDrawOfPlay || !hasPreviousDraw ) ? 0 : msSincePreviousDraw;
}

} // namespace playback_fps_meter

#endif // PLAYBACKFPSMETERPOLICY_H
