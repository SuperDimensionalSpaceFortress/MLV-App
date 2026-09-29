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

/*! \return true when no draw has happened for long enough that the meter
 *  should read 0 and restart (paused, stalled, or a fresh clip). */
inline bool fpsMeterStalled( int msSinceLastDraw )
{
    return msSinceLastDraw > kFpsMeterStallMs;
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

} // namespace playback_fps_meter

#endif // PLAYBACKFPSMETERPOLICY_H
