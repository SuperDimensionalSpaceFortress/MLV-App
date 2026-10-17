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
 *
 * PLAYBACK-STARTUP-WAIT-S1-FIRST-PRESENT-SETUP-1 Row 0: where the first GPU-window present itself went, one line
 * beside it (FirstPresentSplit; zeros and "none" when the first present was not the window's texture route):
 *   playback_smoke.first_present_split session=N slot=<adopted|realloc|discarded|kept|none> handoff=<mode>
 *   setup_program_ms= setup_lut_ms= setup_blur_ms= setup_realloc_ms= setup_sampling_ms= recon_wall_ms=
 *   amaze_kernel_ms= amaze_download_ms= paint_ms= gui_pickup_ms= backend_preloaded=<0|1>
 *   warm_run=<0|1> set_clip_at_play=<n> set_luts_at_play=<n>
 *
 * PLAYBACK-STARTUP-WAIT-S1-RECON-WARM-RUN-1 Row 0: warm_run says this clip's paused recon warm run retained an output;
 * set_clip_at_play / set_luts_at_play count the session's GPU recon set_clip / set_luts calls up to the first present
 * (0 when the warm absorbed them). And render_first split from the first frame's existing stage timers (NA where a part
 * has none; other = render_first minus the timed parts):
 *   playback_smoke.render_first_split session=N render_first_ms= decode_ms= llrawproc_ms= recon_run_ms=NA other_ms=
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

/*! The first Play-present's GPU window parts (Row 0). slot: what the window's texture slot did (the staged prewarm
 *  pair adopted or discarded, a plain re-allocation, or kept); setup_*: GpuPresentSetupTiming of that call (blur =
 *  drain + upload + check); amaze_*: its AMaZE-only kernel / download (the download includes the output's CUDA-GL
 *  register and map); paint: the post block (paint + swap); gui_pickup: render thread done -> GUI present start. */
struct FirstPresentSplit
{
    QString slot = QStringLiteral( "none" );
    QString handoff = QStringLiteral( "none" );
    double setupProgramMs = 0.0;
    double setupLutMs = 0.0;
    double setupBlurMs = 0.0;
    double setupReallocMs = 0.0;
    double setupSamplingMs = 0.0;
    double reconWallMs = 0.0;
    double amazeKernelMs = 0.0;
    double amazeDownloadMs = 0.0;
    double paintMs = 0.0;
    double guiPickupMs = 0.0;
    bool backendPreloaded = false;
    // PLAYBACK-STARTUP-WAIT-S1-RECON-WARM-RUN-1 Row 0
    bool warmRun = false;
    long long setClipAtPlay = 0;
    long long setLutsAtPlay = 0;

    QString line( unsigned long long session ) const
    {
        const auto ms = []( double value ) { return QString::number( value, 'f', 3 ); };
        // concatenated, not .arg()-chained: slot/handoff are free text and must never be re-substituted
        return QStringLiteral( "playback_smoke.first_present_split session=" ) + QString::number( session )
             + QStringLiteral( " slot=" ) + ( slot.isEmpty() ? QStringLiteral( "none" ) : slot )
             + QStringLiteral( " handoff=" ) + ( handoff.isEmpty() ? QStringLiteral( "none" ) : handoff )
             + QStringLiteral( " setup_program_ms=" ) + ms( setupProgramMs )
             + QStringLiteral( " setup_lut_ms=" ) + ms( setupLutMs )
             + QStringLiteral( " setup_blur_ms=" ) + ms( setupBlurMs )
             + QStringLiteral( " setup_realloc_ms=" ) + ms( setupReallocMs )
             + QStringLiteral( " setup_sampling_ms=" ) + ms( setupSamplingMs )
             + QStringLiteral( " recon_wall_ms=" ) + ms( reconWallMs )
             + QStringLiteral( " amaze_kernel_ms=" ) + ms( amazeKernelMs )
             + QStringLiteral( " amaze_download_ms=" ) + ms( amazeDownloadMs )
             + QStringLiteral( " paint_ms=" ) + ms( paintMs )
             + QStringLiteral( " gui_pickup_ms=" ) + ms( guiPickupMs )
             + QStringLiteral( " backend_preloaded=" ) + ( backendPreloaded ? QStringLiteral( "1" ) : QStringLiteral( "0" ) )
             + QStringLiteral( " warm_run=" ) + ( warmRun ? QStringLiteral( "1" ) : QStringLiteral( "0" ) )
             + QStringLiteral( " set_clip_at_play=" ) + QString::number( setClipAtPlay )
             + QStringLiteral( " set_luts_at_play=" ) + QString::number( setLutsAtPlay );
    }
};

/*! PLAYBACK-STARTUP-WAIT-S1-RECON-WARM-RUN-1 Row 0: the first measured frame's render_first, split by the stage timers
 *  it already carries (raw_uint16_ms for the decode, llrawproc_total_ms for the recon worker's llrawproc call, which
 *  holds the GPU recon run). A part without a timer prints NA; other is render_first minus the parts that have one. */
struct RenderFirstSplit
{
    double renderFirstMs = 0.0;
    bool hasDecode = false;
    double decodeMs = 0.0;
    bool hasLlrawproc = false;
    double llrawprocMs = 0.0;
    bool hasReconRun = false;     // no per-frame timer today: the run is inside llrawproc_ms
    double reconRunMs = 0.0;

    double otherMs() const
    {
        double rest = renderFirstMs;
        if( hasDecode ) rest -= decodeMs;
        if( hasLlrawproc ) rest -= llrawprocMs;
        else if( hasReconRun ) rest -= reconRunMs;
        return rest > 0.0 ? rest : 0.0;
    }

    QString line( unsigned long long session ) const
    {
        const auto ms = []( bool has, double value ) { return has ? QString::number( value, 'f', 3 ) : QStringLiteral( "NA" ); };
        return QStringLiteral( "playback_smoke.render_first_split session=" ) + QString::number( session )
             + QStringLiteral( " render_first_ms=" ) + ms( true, renderFirstMs )
             + QStringLiteral( " decode_ms=" ) + ms( hasDecode, decodeMs )
             + QStringLiteral( " llrawproc_ms=" ) + ms( hasLlrawproc, llrawprocMs )
             + QStringLiteral( " recon_run_ms=" ) + ms( hasReconRun, reconRunMs )
             + QStringLiteral( " other_ms=" ) + ms( true, otherMs() );
    }
};

/*! Render thread done -> GUI present start, in ms; 0 when either stamp is unknown or out of order. */
inline double guiPickupMs( double frameReadyEmitStageTimeS, double displayStartS )
{
    if( frameReadyEmitStageTimeS <= 0.0 || displayStartS < frameReadyEmitStageTimeS ) return 0.0;
    return ( displayStartS - frameReadyEmitStageTimeS ) * 1000.0;
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

    /*! Row 0: the first present's GPU window parts (reset with the rest at every session start). */
    void noteFirstPresentSplit( const FirstPresentSplit &split ) { m_firstPresentSplit = split; }
    const FirstPresentSplit &firstPresentSplit() const { return m_firstPresentSplit; }
    QString firstPresentLine( unsigned long long session ) const { return m_firstPresentSplit.line( session ); }

    /*! PLAYBACK-STARTUP-WAIT-S1-RECON-WARM-RUN-1 Row 0: the first frame's render_first parts (reset with the rest). */
    void noteRenderFirstSplit( const RenderFirstSplit &split ) { m_renderFirstSplit = split; }
    const RenderFirstSplit &renderFirstSplit() const { return m_renderFirstSplit; }
    QString renderFirstLine( unsigned long long session ) const { return m_renderFirstSplit.line( session ); }

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
    FirstPresentSplit m_firstPresentSplit;
    RenderFirstSplit m_renderFirstSplit;
    std::vector<double> m_laterRender;
    std::vector<double> m_laterGuiPresent;
};

} // namespace playback_startup_split

#endif // PLAYBACKSTARTUPSPLIT_H
