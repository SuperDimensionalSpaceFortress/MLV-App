#ifndef PLAYBACKLOOKAHEADCPUROUTE_H
#define PLAYBACKLOOKAHEADCPUROUTE_H

// CPU-S1-PACE-POST-785-LEVER-1: may the playback lookahead (PLAYBACK-DECODE-RENDER-OVERLAP-1) run on
// the CPU route? On the CPU route one frame cost recon (~36 ms) + render work (~8 ms) + GUI hop in
// series (overlap_ms=0, lookahead_depth=0): the lookahead was texture-only. Depth 1 lets the recon
// of N+1 run while N renders and presents.
//
// Admitted ONLY when every one of these holds:
//  - playing, a file loaded, drop-frame mode (the lookahead machinery's drop-frame erase and the
//    target-filtered acquire are what keep a lookahead slot from presenting out of turn; whole-frame
//    playback is not admitted, so it keeps today's single-frame path);
//  - Phase 3 DecodeReconProcess is active (not killed, no live fallback), so the upstream recon
//    worker exists and the lookahead has something to overlap;
//  - not the GPU texture route (it keeps its own depth-2 admission) and no GPU debayer;
//  - the CPU prep route, decided by #774's own predicate (playbackPrepMayBorrowSlotRgb16) on the
//    request's route inputs: playing, playback policy active, CPU preview processing or a non-8-bit
//    output, and no gpu16 / GPU preview processing / GPU texture candidate. The borrow's buffer-size
//    checks are per frame, at present time; here they are satisfied by a probe word so only the
//    route decides.

#include "PlaybackPrepOwnership.h"

#include <cmath>
#include <cstdint>

struct PlaybackLookaheadCpuRouteInputs
{
    bool playing = false;                   // ui->actionPlay->isChecked()
    bool fileLoaded = false;                // m_fileLoaded && m_pMlvObject && m_pRenderThread
    bool dropFrameMode = false;             // ui->actionDropFrameMode->isChecked()
    bool phase3DecodeReconProcess = false;  // phase3ModeFor(...) == DecodeReconProcess, not killed, no fallback
    bool textureRouteRequested = false;     // requestContext.gpuPlaybackReconTexturePresentRequested
    bool gpuBilinearDebayer = false;        // m_renderThreadUsingGpuBilinearDebayer
    bool gpuAmazeDebayer = false;           // m_renderThreadUsingGpuAmazeDebayer
    PlaybackPrepBorrowInputs prepRoute;     // #774's borrow inputs, request side (buffer fields unused)
};

inline bool playbackLookaheadCpuRouteAdmits( const PlaybackLookaheadCpuRouteInputs &in )
{
    if( !in.playing || !in.fileLoaded || !in.dropFrameMode ) return false;
    if( !in.phase3DecodeReconProcess ) return false;
    if( in.textureRouteRequested ) return false;
    if( in.gpuBilinearDebayer || in.gpuAmazeDebayer ) return false;
    PlaybackPrepBorrowInputs route = in.prepRoute;
    static const uint16_t kRouteProbeWord = 0;
    route.rawImage16 = &kRouteProbeWord;
    route.rawImage16Bytes = sizeof( kRouteProbeWord );
    route.sourceImage16Bytes = sizeof( kRouteProbeWord );
    return playbackPrepMayBorrowSlotRgb16( route );
}

// CPU-LOOKAHEAD-PRUNE-WASTE-1 (r4): the rule. On the CPU route the lookahead asks for the frame the drop-frame
// engine is predicted to present next, floor( position + k x stride ), not requestedFrame + k. Here position is
// the engine's fractional position and stride is its recent target-to-target advance. Before this, a CPU frame
// took ~56 ms against a 41.7 ms period, so the engine advanced ~1.35 frames per target. Every advance of 2 skipped
// the +1 lookahead, and prunePlaybackLookaheadOutsideForwardWindow threw it away (125-152 of ~587 requests per
// r3 H leg). The texture route is unchanged: offset k, window depth.
namespace playback_lookahead_cpu
{

inline constexpr double kMinStrideFrames = 1.0;
/*! A longer stride is a stall, not pacing: the prediction stops following it. */
inline constexpr int kMaxStrideFrames = 3;
/*! Weight of the newest target-to-target advance in the stride average. */
inline constexpr double kStrideEmaWeight = 0.25;

/*! The drop-frame engine's average position advance between successive DISTINCT playback targets
 *  (MainWindow::drawFrame), so floor( position + stride ) predicts the next target. It samples the fractional
 *  position, not the integer target: an engine advancing 1.35 frames per target yields 1.35, not a noisy
 *  1, 2, 1, 1, 2. */
class DropFrameStride
{
public:
    void reset()
    {
        m_strideFrames = kMinStrideFrames;
        m_lastTarget = -1;
        m_lastPosition = 0.0;
    }
    /*! A repeated target is not a new target: no sample. A backward move (a Loop wrap, a seek) or a non-finite
     *  position restarts the measurement without a sample. */
    void noteTarget( int target, double position )
    {
        if( target == m_lastTarget ) return;
        if( m_lastTarget >= 0 && target > m_lastTarget
         && std::isfinite( position ) && position > m_lastPosition )
        {
            double advance = position - m_lastPosition;
            if( advance > kMaxStrideFrames ) advance = kMaxStrideFrames;
            m_strideFrames += kStrideEmaWeight * ( advance - m_strideFrames );
        }
        m_lastTarget = target;
        m_lastPosition = std::isfinite( position ) ? position : 0.0;
    }
    double strideFrames() const { return m_strideFrames; }

private:
    double m_strideFrames = kMinStrideFrames;
    int m_lastTarget = -1;
    double m_lastPosition = 0.0;
};

/*! Frames past requestedFrame that lookahead \a k ( 1..depth ) asks for. The texture route (\a cpuRoute false) is
 *  k exactly. The CPU route is floor( \a positionFraction + k x \a strideFrames ), which is never less than k and
 *  strictly increases with k. \a positionFraction is the drop-frame position minus requestedFrame; a value
 *  outside [0, 1) or a non-finite one counts as 0. \a strideFrames is clamped to [1, kMaxStrideFrames], and a non-finite value counts as 1. */
inline int lookaheadOffsetFrames( int k, bool cpuRoute, double positionFraction, double strideFrames )
{
    // RED (80c753d8's rule): requestedFrame + k on every route.
    (void)cpuRoute; (void)positionFraction; (void)strideFrames;
    return k;
}

/*! The forward window prunePlaybackLookaheadOutsideForwardWindow keeps, in frames past the active target. The
 *  texture route keeps \a depth (unchanged). The CPU route keeps depth x kMaxStrideFrames, so a request
 *  lookaheadOffsetFrames placed ahead is never pruned before the engine reaches it. Frames behind the target
 *  are still pruned. */
inline int lookaheadWindowFrames( int depth, bool cpuRoute )
{
    (void)cpuRoute; // RED (80c753d8's rule): the depth on every route.
    return depth;
}

} // namespace playback_lookahead_cpu

#endif // PLAYBACKLOOKAHEADCPUROUTE_H
