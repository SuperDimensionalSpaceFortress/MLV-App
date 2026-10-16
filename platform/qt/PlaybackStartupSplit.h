/*!
 * \file PlaybackStartupSplit.h
 * \brief Where the Play -> first-present wait goes, per measured play session (pure, unit-tested).
 *
 * PLAYBACK-STARTUP-WAIT-PREWARM-1: startup_wait_ms (Play -> first present, 0.66-1.12 s on Bachelor CUDA) is the
 * one lever left on whole-run presented/expected. The raw logs show the first texture handed over ~0.35-0.48 s
 * after Play and the GUI thread then busy another ~0.3-0.65 s before the first present is stamped. This splits
 * that wait: the first render request, the first frame's render-thread time, its GUI-thread present, and the
 * sub-steps of finishPresentedFrame (the audio start among them). gpu_route_setup_first_ms is the first GUI
 * present minus the session median of the later ones: the one-time route setup on the first frame.
 *
 * One line per measured session, printed beside playback_smoke.startup_catchup:
 *   playback_smoke.startup_split session=N play_to_request_ms= render_first_ms= gui_present_first_ms=
 *   finish_first_ms= audio_sync_first_ms= audio_stop_ms= audio_jump_ms= audio_play_ms=
 *   finish_other_top=<name>:<ms> gpu_route_setup_first_ms=
 */

#ifndef PLAYBACKSTARTUPSPLIT_H
#define PLAYBACKSTARTUPSPLIT_H

#include <QString>

#include <algorithm>
#include <vector>

namespace playback_startup_split
{

/*! The non-audio sub-steps of finishPresentedFrame, in call order. */
enum FinishStep
{
    StepRecord = 0,     // recordPresentedFrame
    StepQuality,        // presented-colour analysis + quality indicator
    StepScopes,         // scope dispatch
    StepLabels,         // frame/timecode labels, zoom, bad-pixel crosses, post-stop settings
    StepAuto,           // phase-3 timing and the auto quality decision
    StepAdvance,        // first-frame note, render slot release, the tail advance
    StepSmoke,          // smoke/contact-sheet bookkeeping, frameReady, the immediate drain
    StepCount
};

inline const char *finishStepName( int step )
{
    switch( step )
    {
    case StepRecord: return "record";
    case StepQuality: return "quality";
    case StepScopes: return "scopes";
    case StepLabels: return "labels";
    case StepAuto: return "auto";
    case StepAdvance: return "advance";
    case StepSmoke: return "smoke";
    default: return "none";
    }
}

/*! Median of \a values (the mean of the two middle values for an even count); 0 when empty. */
inline double median( std::vector<double> values )
{
    if( values.empty() ) return 0.0;
    const size_t mid = values.size() / 2;
    std::nth_element( values.begin(), values.begin() + mid, values.end() );
    const double upper = values[mid];
    if( values.size() % 2 ) return upper;
    const double lower = *std::max_element( values.begin(), values.begin() + mid );
    return ( lower + upper ) * 0.5;
}

class StartupSplit
{
public:
    /*! Later presents kept for the medians: a 25 s leg is ~600 frames; the cap only bounds memory. */
    static constexpr size_t kMaxLaterSamples = 8192;

    void reset()
    {
        *this = StartupSplit();
    }

    /*! A render request was issued \a sinceStartMs after Play; only the first one counts. */
    void noteRequest( double sinceStartMs )
    {
        if( m_playToRequestMs < 0.0 ) m_playToRequestMs = sinceStartMs;
    }

    bool firstCaptured() const { return m_firstCaptured; }

    /*! The first present of the session: its render-thread total and its GUI-thread present. */
    void noteFirstPresent( double renderTotalMs, double guiPresentMs )
    {
        m_firstCaptured = true;
        m_renderFirstMs = renderTotalMs;
        m_guiPresentFirstMs = guiPresentMs;
    }

    /*! A later present: kept for the session medians. */
    void noteLaterPresent( double renderTotalMs, double guiPresentMs )
    {
        if( m_laterRender.size() >= kMaxLaterSamples ) return;
        m_laterRender.push_back( renderTotalMs );
        m_laterGuiPresent.push_back( guiPresentMs );
    }

    void noteFirstFinishStep( int step, double ms )
    {
        if( step >= 0 && step < StepCount ) m_finishStepMs[step] += ms;
    }

    void noteFirstAudio( double stopMs, double jumpMs, double playMs )
    {
        m_audioStopMs = stopMs;
        m_audioJumpMs = jumpMs;
        m_audioPlayMs = playMs;
    }

    /*! The whole audio block on the first present (may include sync bookkeeping beyond stop/jump/play). */
    void noteFirstAudioSync( double ms ) { m_audioSyncFirstMs = ms; }

    void noteFirstFinish( double ms ) { m_finishFirstMs = ms; }

    double playToRequestMs() const { return m_playToRequestMs < 0.0 ? 0.0 : m_playToRequestMs; }
    double renderFirstMs() const { return m_renderFirstMs; }
    double guiPresentFirstMs() const { return m_guiPresentFirstMs; }
    double finishFirstMs() const { return m_finishFirstMs; }
    double audioSyncFirstMs() const { return m_audioSyncFirstMs; }
    double medianLaterRenderMs() const { return median( m_laterRender ); }
    double medianLaterGuiPresentMs() const { return median( m_laterGuiPresent ); }

    /*! The first GUI present minus the median later one (never negative). */
    double gpuRouteSetupFirstMs() const
    {
        const double d = m_guiPresentFirstMs - medianLaterGuiPresentMs();
        return d > 0.0 ? d : 0.0;
    }

    /*! The largest non-audio finish sub-step; "none" when all are zero. */
    int finishOtherTopStep() const
    {
        int top = -1;
        double topMs = 0.0;
        for( int s = 0; s < StepCount; ++s )
        {
            if( m_finishStepMs[s] > topMs )
            {
                top = s;
                topMs = m_finishStepMs[s];
            }
        }
        return top;
    }
    double finishStepMs( int step ) const
    {
        return step >= 0 && step < StepCount ? m_finishStepMs[step] : 0.0;
    }

    QString line( unsigned long long session ) const
    {
        const int top = finishOtherTopStep();
        return QStringLiteral(
                   "playback_smoke.startup_split session=%1 play_to_request_ms=%2 render_first_ms=%3 "
                   "gui_present_first_ms=%4 finish_first_ms=%5 audio_sync_first_ms=%6 audio_stop_ms=%7 "
                   "audio_jump_ms=%8 audio_play_ms=%9 finish_other_top=%10:%11 gpu_route_setup_first_ms=%12" )
            .arg( session )
            .arg( playToRequestMs(), 0, 'f', 3 )
            .arg( m_renderFirstMs, 0, 'f', 3 )
            .arg( m_guiPresentFirstMs, 0, 'f', 3 )
            .arg( m_finishFirstMs, 0, 'f', 3 )
            .arg( m_audioSyncFirstMs, 0, 'f', 3 )
            .arg( m_audioStopMs, 0, 'f', 3 )
            .arg( m_audioJumpMs, 0, 'f', 3 )
            .arg( m_audioPlayMs, 0, 'f', 3 )
            .arg( QString::fromLatin1( finishStepName( top ) ) )
            .arg( finishStepMs( top ), 0, 'f', 3 )
            .arg( gpuRouteSetupFirstMs(), 0, 'f', 3 );
    }

private:
    double m_playToRequestMs = -1.0;
    bool m_firstCaptured = false;
    double m_renderFirstMs = 0.0;
    double m_guiPresentFirstMs = 0.0;
    double m_finishFirstMs = 0.0;
    double m_audioSyncFirstMs = 0.0;
    double m_audioStopMs = 0.0;
    double m_audioJumpMs = 0.0;
    double m_audioPlayMs = 0.0;
    double m_finishStepMs[StepCount] = {};
    std::vector<double> m_laterRender;
    std::vector<double> m_laterGuiPresent;
};

} // namespace playback_startup_split

#endif // PLAYBACKSTARTUPSPLIT_H
