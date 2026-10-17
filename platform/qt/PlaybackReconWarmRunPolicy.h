/*!
 * \file PlaybackReconWarmRunPolicy.h
 * \brief Whether the paused recon warm run may run, and its log line (pure, unit-tested).
 *
 * PLAYBACK-STARTUP-WAIT-S1-RECON-WARM-RUN-1 Row W. Play frame 0 is the first GPU recon run in the process: at s1 it
 * pays the first set_clip (~164 MB of recon buffers), set_luts (~25 ms) and the first-run allocations and kernel
 * loads inside a 143-182 ms render_first. While paused, once the #450/#473 route prewarm has run, MainWindow posts ONE
 * warm request: the recon worker runs the paused frame through the exact Play frame-0 prepare-only recon (same thread,
 * same llrawproc worker state, so the set_clip / set_luts keys match at Play), and the GUI runs AMaZE from its retained
 * output into #473's staged slot, then releases the token. Nothing is presented and no cache is written.
 *
 * Run only when every guard passes; real requests always win (a warm that finds one pending is skipped, and re-posted
 * kRepostDelayMs later, at most kMaxPosts posts per clip). MLVAPP_PLAYBACK_RECON_WARM_RUN=0 turns it off;
 * MLVAPP_PLAYBACK_ROUTE_PREWARM=0 still turns everything off (no prewarm, so prewarm_not_run).
 *
 *   playback_recon_warm decision=<run|skip> reason=<name> scale= width= height= render_ms= set_clip_ms= set_luts_ms=
 *   run_ms= amaze_ok=<0|1> amaze_kernel_ms= released=<0|1> outstanding_after= overlapped_play=<0|1>
 */

#ifndef PLAYBACKRECONWARMRUNPOLICY_H
#define PLAYBACKRECONWARMRUNPOLICY_H

#include <QString>

namespace playback_recon_warm_run
{

enum class Reason
{
    None,                 // run
    Disabled,             // MLVAPP_PLAYBACK_RECON_WARM_RUN=0
    Playing,              // Play is checked: the warm never competes with a play session
    StaleClip,            // another clip was opened since the warm was armed
    PrewarmNotRun,        // the route prewarm did not run (CPU or ineligible route, or MLVAPP_PLAYBACK_ROUTE_PREWARM=0)
    BackendNotPreloaded,  // the recon backend is not loaded (#473 Row B off or failed)
    CudaSuppressed,       // GPU-DEVICE-LOSS-SELF-HEAL: no CUDA call after a device loss
    RealRequestPending,   // a render, decode or recon request is queued or running
    // render-thread outcomes (the warm was admitted, then could not complete)
    PlanRefused,          // x2/x4: the reduced recon plan refused the frame
    DecodeFailed,
    ReconNotRetained,     // the prepare-only run produced no retained output (rc in the line)
    PipelineCapture,      // MLVAPP pipeline capture is on: a warm would write capture files for the frame
    SnapshotFailed        // the last-run state could not be saved
};

struct Decision
{
    bool run = false;
    Reason reason = Reason::None;
};

inline Decision decide( bool enabled,
                        bool playing,
                        bool clipGenerationMatches,
                        bool prewarmDecisionRun,
                        bool backendPreloadOk,
                        bool cudaSuppressed,
                        bool realRequestPending )
{
    Decision d;
    if( !enabled ) d.reason = Reason::Disabled;
    else if( playing ) d.reason = Reason::Playing;
    else if( !clipGenerationMatches ) d.reason = Reason::StaleClip;
    else if( !prewarmDecisionRun ) d.reason = Reason::PrewarmNotRun;
    else if( !backendPreloadOk ) d.reason = Reason::BackendNotPreloaded;
    else if( cudaSuppressed ) d.reason = Reason::CudaSuppressed;
    else if( realRequestPending ) d.reason = Reason::RealRequestPending;
    else d.run = true;
    return d;
}

inline const char *reasonName( Reason reason )
{
    switch( reason )
    {
    case Reason::None: return "none";
    case Reason::Disabled: return "disabled";
    case Reason::Playing: return "playing";
    case Reason::StaleClip: return "stale_clip";
    case Reason::PrewarmNotRun: return "prewarm_not_run";
    case Reason::BackendNotPreloaded: return "backend_not_preloaded";
    case Reason::CudaSuppressed: return "cuda_suppressed";
    case Reason::RealRequestPending: return "real_request_pending";
    case Reason::PlanRefused: return "plan_refused";
    case Reason::DecodeFailed: return "decode_failed";
    case Reason::ReconNotRetained: return "recon_not_retained";
    case Reason::PipelineCapture: return "pipeline_capture";
    case Reason::SnapshotFailed: return "snapshot_failed";
    }
    return "none";
}

/*! Posts per clip: the first, after the prewarm, plus re-posts after a real_request_pending skip, kRepostDelayMs apart
 *  (a 30 s window: the smoke's Look Assist settle alone keeps the pipeline busy for ~13 s after the prewarm). */
constexpr int kMaxPosts = 60;
constexpr int kRepostDelayMs = 500;

/*! A skipped warm is re-posted only when real work was in the way and posts remain. */
inline bool repostAfterSkip( Reason reason, int postsSoFar )
{
    return reason == Reason::RealRequestPending && postsSoFar < kMaxPosts;
}

/*! The GUI's AMaZE step into the staged slot runs only for a warm of the current clip, while paused; every other
 *  warm-done (stale clip, Play started, no output) releases its token and does nothing else. */
inline bool guiAmazeAllowed( bool hasRetainedOutput, bool playing, bool clipGenerationMatches )
{
    return hasRetainedOutput && !playing && clipGenerationMatches;
}

struct Line
{
    bool run = false;
    Reason reason = Reason::None;
    int scale = 0;
    int width = 0;
    int height = 0;
    double renderMs = 0.0;
    double setClipMs = 0.0;
    double setLutsMs = 0.0;
    double runMs = 0.0;
    bool amazeOk = false;
    double amazeKernelMs = 0.0;
    bool released = false;
    int outstandingAfter = 0;
    bool overlappedPlay = false;

    QString format() const
    {
        const auto ms = []( double value ) { return QString::number( value, 'f', 3 ); };
        const auto b01 = []( bool value ) { return value ? QStringLiteral( "1" ) : QStringLiteral( "0" ); };
        return QStringLiteral( "playback_recon_warm decision=" ) + ( run ? QStringLiteral( "run" ) : QStringLiteral( "skip" ) )
             + QStringLiteral( " reason=" ) + QString::fromLatin1( reasonName( reason ) )
             + QStringLiteral( " scale=" ) + QString::number( scale )
             + QStringLiteral( " width=" ) + QString::number( width )
             + QStringLiteral( " height=" ) + QString::number( height )
             + QStringLiteral( " render_ms=" ) + ms( renderMs )
             + QStringLiteral( " set_clip_ms=" ) + ms( setClipMs )
             + QStringLiteral( " set_luts_ms=" ) + ms( setLutsMs )
             + QStringLiteral( " run_ms=" ) + ms( runMs )
             + QStringLiteral( " amaze_ok=" ) + b01( amazeOk )
             + QStringLiteral( " amaze_kernel_ms=" ) + ms( amazeKernelMs )
             + QStringLiteral( " released=" ) + b01( released )
             + QStringLiteral( " outstanding_after=" ) + QString::number( outstandingAfter )
             + QStringLiteral( " overlapped_play=" ) + b01( overlappedPlay );
    }
};

} // namespace playback_recon_warm_run

#endif // PLAYBACKRECONWARMRUNPOLICY_H
