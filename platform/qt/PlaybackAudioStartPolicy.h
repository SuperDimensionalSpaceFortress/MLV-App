/*!
 * \file PlaybackAudioStartPolicy.h
 * \brief How a Play's audio starts once the audio device lives on its own thread (pure, unit-tested).
 *
 * PLAYBACK-STARTUP-WAIT-PREWARM-1 Row A. On Bachelor the first Play-present blocked the GUI thread ~0.3-0.65 s in
 * the audio start (AudioPlayback::play(): QAudioSink::start opens the WASAPI device on every Play, since stop()
 * closes it), and the smoke stamp, the pace clock and the second frame all waited behind it. Now:
 *  - At Play the audio thread opens the sink at the start frame, silent (volume 0), and suspends it: prepare.
 *  - At the first present the GUI thread only posts the sync. The audio thread resumes the prepared sink when it
 *    was prepared at the presented frame and the present is at most kAudioResumeSlackMs old (one frame period).
 *  - Otherwise it re-opens once, seeking ahead by the lag plus the expected open time (the last measured open),
 *    rounded UP to whole frames, so the audio lands on the video timeline instead of trailing it.
 *  - MLVAPP_PLAYBACK_AUDIO_SYNC_LEGACY=1: today's synchronous stop / jumpToPos / play on the GUI thread.
 */

#ifndef PLAYBACKAUDIOSTARTPOLICY_H
#define PLAYBACKAUDIOSTARTPOLICY_H

#include <cmath>

namespace playback_audio_start
{

/*! A prepared sink is resumed only within one frame period (23.976 fps) of the present it syncs to. */
inline constexpr double kAudioResumeSlackMs = 42.0;

enum class Mode { Prepared, Reopen, Legacy };

inline const char *modeName( Mode mode )
{
    switch( mode )
    {
    case Mode::Prepared: return "prepared";
    case Mode::Reopen: return "reopen";
    case Mode::Legacy: return "legacy";
    }
    return "none";
}

/*! \a lagMs is now minus the present the audio syncs to; \a prepared says a silent, suspended sink is open at
 *  \a preparedFrame. */
inline Mode chooseMode( bool legacy, bool prepared, int preparedFrame, int frame, double lagMs,
                        double slackMs = kAudioResumeSlackMs )
{
    if( legacy ) return Mode::Legacy;
    if( prepared && preparedFrame == frame && lagMs <= slackMs ) return Mode::Prepared;
    return Mode::Reopen;
}

/*! The frame a re-open starts the audio at: \a frame plus the frames the video advances during the lag and the
 *  open, rounded up (audio a part-frame early, never late). Never negative; an unknown fps leaves \a frame. */
inline int reopenSeekFrame( int frame, double lagMs, double expectedOpenMs, double fps )
{
    const int base = frame > 0 ? frame : 0;
    if( !( fps > 0.0 ) ) return base;
    const double aheadMs = ( lagMs > 0.0 ? lagMs : 0.0 ) + ( expectedOpenMs > 0.0 ? expectedOpenMs : 0.0 );
    // the epsilon keeps an exact whole number of frame periods from rounding up to one more
    const int aheadFrames = static_cast<int>( std::ceil( aheadMs * fps / 1000.0 - 1e-9 ) );
    return base + ( aheadFrames > 0 ? aheadFrames : 0 );
}

/*! App-side A/V offset when the audio became audible: the audio content time (it starts at \a audioFrame, plus
 *  \a audioPlayedMs already consumed) minus the video timeline time (\a videoFrame was presented \a sincePresentMs
 *  earlier). Signed: audio late is negative. */
inline double avOffsetEstMs( int audioFrame, double audioPlayedMs, int videoFrame, double sincePresentMs, double fps )
{
    if( !( fps > 0.0 ) ) return 0.0;
    const double audioMs = audioFrame * 1000.0 / fps + audioPlayedMs;
    const double videoMs = videoFrame * 1000.0 / fps + sincePresentMs;
    return audioMs - videoMs;
}

} // namespace playback_audio_start

#endif // PLAYBACKAUDIOSTARTPOLICY_H
