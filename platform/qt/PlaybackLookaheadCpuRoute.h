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
#include "PlaybackFrameRange.h"

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
    int frameSlotCount = 4;                 // RenderFrameThread::playbackFrameSlotCount()
};

/*! r4 fix iteration 2 (adversary A3 on c7953c66): the frame slots the CPU-route lookahead needs. At depth 1 the slots
 *  hold at most the presenting slot (#774 borrow), the target and two lookaheads issued from earlier targets. With
 *  MLVAPP_PHASE3_FRAME_SLOT_COUNT=1 or 2, ready lookaheads 27 and 28 filled every slot before target 26 had one: a deadlock. */
inline constexpr int kCpuRouteLookaheadMinFrameSlots = 4;

inline bool playbackLookaheadCpuRouteAdmits( const PlaybackLookaheadCpuRouteInputs &in )
{
    if( !in.playing || !in.fileLoaded || !in.dropFrameMode ) return false;
    if( in.frameSlotCount < kCpuRouteLookaheadMinFrameSlots ) return false;
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
    if( !cpuRoute || k <= 0 ) return k;
    double fraction = std::isfinite( positionFraction ) ? positionFraction : 0.0;
    if( fraction < 0.0 ) fraction = 0.0;
    if( fraction >= 1.0 ) fraction = 0.0;
    double stride = std::isfinite( strideFrames ) ? strideFrames : kMinStrideFrames;
    if( stride < kMinStrideFrames ) stride = kMinStrideFrames;
    if( stride > kMaxStrideFrames ) stride = kMaxStrideFrames;
    const int offset = static_cast<int>( std::floor( fraction + static_cast<double>( k ) * stride ) );
    // r4 fix iteration 1 (adversary A3 on 05511dac): 0.9999999999999999 + 3.0 rounds to 4.0 in double, so the floor is
    // capped at k x kMaxStrideFrames: a request never lands past the prune window it must stay inside.
    if( offset > k * kMaxStrideFrames ) return k * kMaxStrideFrames;
    return offset < k ? k : offset;
}

/*! r4 fix iteration 1 (adversary A2 on 05511dac): the frame CPU-route lookahead \a k asks for is the frame the drop-frame
 *  engine itself reaches. It advances the position requestedFrame + fraction by lookaheadOffsetFrames whole frames under
 *  playback_frame_range::advanceDropFrameTick's rule (mirrored in integers below), so the engine's own Loop wrap (which never returns the cut's last frame
 *  unless \a presentLastFrame, i.e. conform) or end-of-cut clamp applies. Before this, Loop on with Cut 1..10,
 *  requestedFrame 8 at 8.25 and stride 1 asked for frame 9; the engine wraps 9.25 to 0.25, so 9 was never presented and
 *  was pruned. \a cutInValue and \a cutOutValue are the 1-based spin box values, as the engine takes them. */
inline int cpuRouteLookaheadFrame( int requestedFrame, int k, double positionFraction, double strideFrames,
                                   int cutInValue, int cutOutValue, bool loopEnabled, bool presentLastFrame )
{
    const int offset = lookaheadOffsetFrames( k, true, positionFraction, strideFrames );
    // r4 fix iteration 2 (adversary A2 on c7953c66): advanceDropFrameTick's rule in exact integers. The engine's position
    // is N + fraction with N = requestedFrame + offset and fraction in [0, 1), and every bound it compares against is a
    // whole frame, so N alone decides. Adding them in double first rounded 1.9999999999999998 + 3 up to 5.0, so frame 4
    // was asked for as 5.
    const long long n = static_cast<long long>( requestedFrame ) + offset;
    const long long lastFrame = static_cast<long long>( cutOutValue ) - 1;
    const long long wrapAt = presentLastFrame ? lastFrame + 1 : lastFrame;   // playback_frame_range::dropFrameLoopWrapAt
    if( loopEnabled && n >= wrapAt )
    {
        const long long width = static_cast<long long>( cutOutValue ) - cutInValue + ( presentLastFrame ? 1 : 0 );
        if( width <= 0 ) return static_cast<int>( n );   // the engine subtracts nothing
        const long long laps = ( n - wrapAt ) / width + 1;
        return static_cast<int>( n - laps * width );
    }
    if( !loopEnabled && n >= lastFrame ) return static_cast<int>( lastFrame );
    return static_cast<int>( n );
}

/*! The forward window prunePlaybackLookaheadOutsideForwardWindow keeps, in frames past the active target. The
 *  texture route keeps \a depth (unchanged). The CPU route keeps depth x kMaxStrideFrames + 1, so a request
 *  cpuRouteLookaheadFrame placed ahead is never pruned before the engine reaches it. Frames behind the target
 *  are still pruned. The + 1 (r4 fix iteration 2, adversary A1 on c7953c66): under Loop the engine wraps by
 *  cutOut - cutIn (it skips the cut's last frame), but the prune measures the distance modulo the cut's frame count
 *  cutOut - cutIn + 1. So a prediction 3 ahead that wraps (97 + 3, Cut 1..100, to frame 1) measures 4. */
inline int lookaheadWindowFrames( int depth, bool cpuRoute )
{
    if( !cpuRoute || depth <= 0 ) return depth;
    return depth * kMaxStrideFrames + 1;
}

} // namespace playback_lookahead_cpu

#endif // PLAYBACKLOOKAHEADCPUROUTE_H
