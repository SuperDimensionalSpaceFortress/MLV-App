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

#include <climits>

namespace playback_fps_meter
{

/*! The largest age stampAgeMs returns either way: one day. The wall-clock type the meter used to
 *  read never returned more (its msecsTo stays inside +-86,400,000 ms), so every reader of the
 *  age (the drop-frame loop, the pace guard, the slider) sees only the range it was built for, and
 *  a long process suspension cannot ask for billions of frames. */
inline constexpr int kMaxStampAgeMs = 86400000;

/*! Milliseconds from \a then to \a now on the stamps' own clock, 0 when either was never
 *  started (QElapsedTimer::msecsTo is undefined on an invalid timer; the wall-clock type this
 *  replaced returned 0), saturated to +-kMaxStampAgeMs. The meter passes QElapsedTimer stamps: a
 *  monotonic clock, so midnight, a DST change or a wall-clock step never reaches an age.
 *
 *  The age subtracts the two stamps' OWN whole milliseconds (msecsSinceReference), never msecsTo:
 *  msecsTo truncates the DIFFERENCE, and because the idle tick re-arms lastTime a little after each
 *  tick, that loss would repeat on every tick (a 1 ms poll reads 0 forever; the 8 ms default reads
 *  7 and drop-frame runs 1/8 slow). Whole stamps telescope, so summed ages keep up with real time,
 *  as the wall-clock stamps did. The difference is taken in unsigned arithmetic, so it is defined
 *  for any pair of stamps. */
template <class Stamp>
inline int stampAgeMs( const Stamp & then, const Stamp & now )
{
    if( !then.isValid() || !now.isValid() ) return 0;
    const long long thenMs = static_cast<long long>( then.msecsSinceReference() );
    const long long nowMs = static_cast<long long>( now.msecsSinceReference() );
    const unsigned long long thenU = static_cast<unsigned long long>( thenMs );
    const unsigned long long nowU = static_cast<unsigned long long>( nowMs );
    const unsigned long long limit = static_cast<unsigned long long>( kMaxStampAgeMs );
    if( nowMs >= thenMs )
    {
        const unsigned long long forward = nowU - thenU;
        return forward > limit ? kMaxStampAgeMs : static_cast<int>( forward );
    }
    const unsigned long long backward = thenU - nowU;
    return backward > limit ? -kMaxStampAgeMs : -static_cast<int>( backward );
}

/*! The most frames one drop-frame tick may ask for. advanceDropFrameTick (PlaybackFrameRange.h) counts Loop laps in an
 *  int, capped at INT_MAX - 1, so a request past that leaves the position outside the cut (adversary r2 A1: a one-day
 *  age at a 25000 fps source asked for 2.16e9 frames). The bound is on the LAP WORK too, not only on that count: a tick
 *  that wraps a width-1 cut makes one lap per frame, and PlaybackWrapRecorder::noteEngineWrap records every lap on the
 *  GUI thread (one increment and one append each; pass 4 A1 on 3bb01140: a 1e9 bound made that 1e9 appends, 4 GB).
 *  At most 1e6 laps per tick (about 4 MB, once per long suspension): the frame bound is 1e6 less the slack a tick adds
 *  on its way to the laps (r3 pass A1 on 8743841f: a position already inside the cut adds one lap and the double sum
 *  can round up one more, so 0.9999999999999999 + 1e6 made 1,000,001 width-1 laps; conform debt adds at most
 *  kConformMaxDebtFrames = 8). 999,990 frames is more than 11 hours of catch-up at 24 fps. */
inline constexpr double kMaxTickFrames = 999990.0;

/*! The tick's elapsed milliseconds, cut so that \a framerate * ms / 1000 stays within +-kMaxTickFrames. An ordinary
 *  8 ms tick is unchanged at any source rate up to 124,998,750 fps (no camera records within five orders of magnitude
 *  of it); past that rate an ordinary tick is cut to at most kMaxTickFrames, which still lands inside a Loop cut (r3 pass A2 on
 *  8743841f: 150,000,000 fps, 8 ms -> 6 ms). Below it only a long suspension is cut. The cut is re-checked with
 *  the call site's own expression (MainWindow::playbackHandling: framerate * (double)timeDiff / 1000.0), because the
 *  reciprocal rounds (pass 4 A2: 781250000/821 fps made 1,050,880 ms, one double step over the bound). A positive age
 *  stays at least 1 ms, so the fps meter's `timeDiff != 0` gate is unchanged, except past 1e9 fps, where even 1 ms is
 *  over the bound and the tick asks for nothing (pass 4 A3). */
inline int frameBoundedTickTimeDiffMs( int timeDiffMs, double framerate )
{
    if( !( framerate > 0.0 ) || timeDiffMs == 0 ) return timeDiffMs;
    const double frames = framerate * static_cast<double>( timeDiffMs ) / 1000.0;
    if( frames <= kMaxTickFrames && frames >= -kMaxTickFrames ) return timeDiffMs;
    const double boundedMs = kMaxTickFrames * 1000.0 / framerate;
    int magnitude = boundedMs >= static_cast<double>( kMaxStampAgeMs )
        ? kMaxStampAgeMs : static_cast<int>( boundedMs );
    while( magnitude > 0 && framerate * static_cast<double>( magnitude ) / 1000.0 > kMaxTickFrames ) --magnitude;
    return timeDiffMs > 0 ? magnitude : -magnitude;
}

/*! No draw for longer than this and the meter reads 0 and restarts. */
inline constexpr int kFpsMeterStallMs = 500;

/*! \return true when no draw has happened for long enough that the meter
 *  should read 0 and restart (paused, stalled, or a fresh clip). */
inline bool fpsMeterStalled( int msSinceLastDraw )
{
    return msSinceLastDraw > kFpsMeterStallMs;
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

} // namespace playback_fps_meter

#endif // PLAYBACKFPSMETERPOLICY_H
