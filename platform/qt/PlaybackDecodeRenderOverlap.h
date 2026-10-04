/*!
 * \file PlaybackDecodeRenderOverlap.h
 * \brief Pure arithmetic for PLAYBACK-DECODE-RENDER-OVERLAP-1, kept GUI-free so it
 *        can be unit-tested with an injected clock.
 *
 * On the CUDA texture route (Phase 3 DecodeReconProcess) one frame costs decode +
 * CPU dual-ISO in the decode/recon workers (~30 ms on Bachelor), then render work
 * on the render thread (~11 ms with Look Assist), then the GUI hop (~6 ms). The GUI
 * only requested frame N+1 after N was rendered, so the stages ran in series with
 * one frame in flight (~47 ms, ~21 fps). The playback lookahead (queue
 * N+1..N+depth as speculative requests) already existed behind an env knob, but
 * after each present the GUI asked RenderFrameThread::isIdle(), which counts that
 * speculative work as busy, so the timer could not advance while it ran.
 *
 * This header holds the three pieces that fix and prove it:
 *  - effectiveLookaheadFrames(): the lookahead is on by default (depth 2) on the
 *    CUDA texture route only; MLVAPP_PLAYBACK_RENDER_LOOKAHEAD_FRAMES=0 is the kill
 *    switch, any explicit value wins, and every other route keeps depth 0.
 *  - WorkItem / blocksPlaybackAdvance(): speculative lookahead work for a frame
 *    other than the current playback target never holds the timer back; the
 *    target's own work (lookahead or not) and every non-lookahead request still do.
 *  - OverlapMeter: the engagement counter. It splits wall time into upstream busy
 *    (decode or recon worker), render busy, and both at once, and counts how many
 *    upstream starts began while a downstream frame was still rendering or awaiting
 *    its present. Frames counted are not proof of overlap; this is.
 */

#ifndef PLAYBACKDECODERENDEROVERLAP_H
#define PLAYBACKDECODERENDEROVERLAP_H

#include <cstdint>
#include <mutex>

namespace playback_overlap
{

/*! The default lookahead depth on the CUDA texture route. Two frames ahead keeps
 *  the recon worker (the slowest stage) fed through one late tick. */
inline constexpr int kDefaultTextureRouteLookaheadFrames = 2;
/*! RenderFrameThread's queuePlaybackLookaheadRequests() bound (qBound( 0, env, 3 )). */
inline constexpr int kMaxLookaheadFrames = 3;

/*! \a envFrames is the parsed MLVAPP_PLAYBACK_RENDER_LOOKAHEAD_FRAMES (or its
 *  legacy alias), or -1 when neither is set. */
inline int effectiveLookaheadFrames( int envFrames, bool textureRouteAdmitted )
{
    if( envFrames >= 0 )
    {
        return envFrames > kMaxLookaheadFrames ? kMaxLookaheadFrames : envFrames;
    }
    return textureRouteAdmitted ? kDefaultTextureRouteLookaheadFrames : 0;
}

/*! One unit of render-thread work as the playback advance sees it: a queued
 *  request, the frame being rendered, or a slot with Phase 3 work in flight. */
struct WorkItem
{
    bool playbackLookahead = false;
    uint64_t generation = 0;
    int64_t frameNumber = -1;
};

/*! True when \a item must hold the playback timer back. Only speculative
 *  lookahead work of the active generation for a frame that is not the current
 *  target is free to run behind the timer: the timer still waits for the frame it
 *  asked for, and for anything that is not speculative. */
inline bool blocksPlaybackAdvance( const WorkItem &item,
                                   int64_t activeTarget,
                                   uint64_t activeGeneration )
{
    if( !item.playbackLookahead ) return true;
    if( item.generation != activeGeneration ) return true;
    if( activeTarget < 0 ) return true;
    return item.frameNumber == activeTarget;
}

/*! RenderFrameThread's two upstream workers. */
enum class UpstreamStage
{
    Decode,
    Recon
};

/*! The exclusive recon start (default on; MLVAPP_PLAYBACK_OVERLAP_RECON_EXCLUSIVE=0 turns it
 *  off). With two frames in flight the recon worker's CPU dual-ISO and the render thread's S/H
 *  both run OpenMP teams across every core; running them at once slowed both (Bachelor, e9df0821:
 *  upstream ~30 -> ~38-43 ms a frame, render 11 -> 13-14 ms), so the overlap bought little. A recon
 *  may therefore start only when the render thread has nothing to render. Decode, the GUI hop and the
 *  present still overlap freely, and the period becomes recon + render instead of the serial
 *  decode + recon + render + hop. */
inline bool reconMayStart( bool exclusive, bool renderInFlight, bool renderWorkPending )
{
    return !exclusive || ( !renderInFlight && !renderWorkPending );
}

struct OverlapSnapshot
{
    uint64_t upstreamStarts = 0;           //!< decode + recon worker starts
    uint64_t upstreamStartsOverlapped = 0; //!< ...that began while an earlier frame rendered or awaited present
    uint64_t renderStarts = 0;
    uint64_t renderStartsWithUpstreamInFlight = 0; //!< renders that began with another frame's decode/recon in flight
    double upstreamBusyMs = 0.0;           //!< wall time with at least one decode or recon worker busy
    double decodeBusyMs = 0.0;             //!< wall time with the decode worker busy
    double reconBusyMs = 0.0;              //!< wall time with the recon worker busy
    uint64_t reconStartsHeldForRender = 0; //!< recon starts the exclusive gate delayed behind a render
    double renderBusyMs = 0.0;             //!< wall time with the render thread busy
    double overlapMs = 0.0;                //!< wall time with both busy at once
    double windowMs = 0.0;                 //!< wall time since reset

    /*! Share of render-busy time that some decode/recon ran under: 0 = strictly serial. */
    double overlapFractionOfRender() const
    {
        return renderBusyMs > 0.0 ? overlapMs / renderBusyMs : 0.0;
    }
    /*! Share of upstream starts that ran ahead of an unfinished downstream frame. */
    double upstreamEngagement() const
    {
        return upstreamStarts > 0
            ? static_cast<double>( upstreamStartsOverlapped ) / static_cast<double>( upstreamStarts )
            : 0.0;
    }
};

/*! Thread-safe: the decode worker, the recon worker and the render thread each
 *  report their own spans. Times are milliseconds on one monotonic clock. */
class OverlapMeter
{
public:
    void reset( double nowMs )
    {
        std::lock_guard<std::mutex> lock( m_mutex );
        m_snapshot = OverlapSnapshot();
        m_upstreamActive = 0;
        m_decodeActive = 0;
        m_reconActive = 0;
        m_renderActive = 0;
        m_resetMs = nowMs;
        m_lastMs = nowMs;
        m_armed = true;
    }

    void upstreamBegin( double nowMs, bool downstreamFrameInFlight,
                        UpstreamStage stage = UpstreamStage::Recon )
    {
        std::lock_guard<std::mutex> lock( m_mutex );
        if( !m_armed ) return;
        advanceLocked( nowMs );
        ++m_upstreamActive;
        ++( stage == UpstreamStage::Decode ? m_decodeActive : m_reconActive );
        ++m_snapshot.upstreamStarts;
        if( downstreamFrameInFlight ) ++m_snapshot.upstreamStartsOverlapped;
    }

    void upstreamEnd( double nowMs, UpstreamStage stage = UpstreamStage::Recon )
    {
        std::lock_guard<std::mutex> lock( m_mutex );
        if( !m_armed ) return;
        advanceLocked( nowMs );
        if( m_upstreamActive > 0 ) --m_upstreamActive;
        int &active = stage == UpstreamStage::Decode ? m_decodeActive : m_reconActive;
        if( active > 0 ) --active;
    }

    void noteReconHeldForRender()
    {
        std::lock_guard<std::mutex> lock( m_mutex );
        if( m_armed ) ++m_snapshot.reconStartsHeldForRender;
    }

    void renderBegin( double nowMs, bool upstreamWorkInFlight )
    {
        std::lock_guard<std::mutex> lock( m_mutex );
        if( !m_armed ) return;
        advanceLocked( nowMs );
        ++m_renderActive;
        ++m_snapshot.renderStarts;
        if( upstreamWorkInFlight ) ++m_snapshot.renderStartsWithUpstreamInFlight;
    }

    void renderEnd( double nowMs )
    {
        std::lock_guard<std::mutex> lock( m_mutex );
        if( !m_armed ) return;
        advanceLocked( nowMs );
        if( m_renderActive > 0 ) --m_renderActive;
    }

    OverlapSnapshot snapshot( double nowMs )
    {
        std::lock_guard<std::mutex> lock( m_mutex );
        if( m_armed ) advanceLocked( nowMs );
        return m_snapshot;
    }

private:
    void advanceLocked( double nowMs )
    {
        if( nowMs <= m_lastMs ) return;
        const double dt = nowMs - m_lastMs;
        if( m_upstreamActive > 0 ) m_snapshot.upstreamBusyMs += dt;
        if( m_decodeActive > 0 ) m_snapshot.decodeBusyMs += dt;
        if( m_reconActive > 0 ) m_snapshot.reconBusyMs += dt;
        if( m_renderActive > 0 ) m_snapshot.renderBusyMs += dt;
        if( m_upstreamActive > 0 && m_renderActive > 0 ) m_snapshot.overlapMs += dt;
        m_lastMs = nowMs;
        m_snapshot.windowMs = nowMs - m_resetMs;
    }

    std::mutex m_mutex;
    OverlapSnapshot m_snapshot;
    int m_upstreamActive = 0;
    int m_decodeActive = 0;
    int m_reconActive = 0;
    int m_renderActive = 0;
    double m_resetMs = 0.0;
    double m_lastMs = 0.0;
    bool m_armed = false;
};

} // namespace playback_overlap

#endif // PLAYBACKDECODERENDEROVERLAP_H
