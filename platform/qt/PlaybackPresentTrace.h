/*!
 * \file PlaybackPresentTrace.h
 * \brief PLAYBACK-STARTUP-CATCHUP-BURST-1 r7: an opt-in per-present playback trace (MLVAPP_PLAYBACK_PRESENT_TRACE=1).
 *
 * The smoke log carries per-frame detail only for a slip, so a long present interval with no skip had no record of
 * what preceded it (r6: head3 legs with one 109-120 ms interval mid-clip, 3 of 4 not slips). With the trace on, every
 * presented frame of a measured Play gets one row: when it was presented, when its render was ready, how long the GUI
 * thread took to pick it up and draw it, the pace guard's credit, and what the playback timer did since the previous
 * present (ticks, the longest gap between two ticks, busy ticks, the advances it made and the last one's time). The
 * rows are kept in memory and written to the log after the Play stops, so the trace never writes inside the timed
 * window.
 *
 * Off (the default) it is inert: every note*() returns before touching state, nothing is allocated, and no row is
 * written. The caller reads the flag once.
 */

#ifndef PLAYBACKPRESENTTRACE_H
#define PLAYBACKPRESENTTRACE_H

#include <atomic>
#include <cstdio>
#include <string>
#include <vector>

namespace playback_present_trace
{

/*! PLAYBACK-TRACE-LOWOVERHEAD-1 r1: a stage stamp that is not known (not taken, or the stage did not run for this frame). */
constexpr double kUnknownMs = -1.0e9;

/*! The per-frame stage stamps the worker threads take, in stage-clock SECONDS (mlv_stage_timing_now()); 0 = not stamped.
 *  A plain POD that lives on the frame slot and rides to the GUI on the ReadyFrame: each field has exactly ONE writer
 *  (the thread that owns the slot at that stage), and the slot's hand-over between stages happens under the render
 *  thread's mutex, so the fields need no atomics and no lock of their own. Nothing here allocates or formats. */
struct StageStamps
{
    double requestS = 0.0;      // the frame was requested
    double decodeQueueS = 0.0;  // queued for the decode worker
    double decodeStartS = 0.0;  // decode worker began
    double decodeEndS = 0.0;    // decode worker finished
    double reconStartS = 0.0;   // recon worker began
    double reconEndS = 0.0;     // recon worker finished
    double renderStartS = 0.0;  // render thread began this frame
    double drawBeginS = 0.0;    // the GUI thread began drawing it (drawFrameReady's display start)
    // Durations in ms (not stamps; kUnknownMs = nobody measured it for THIS frame). The frame JSON's unprefixed configure and
    // H2D keys are the render thread's own last-run record, which can describe an older frame (adversary a7085920 A1,
    // 81c5805c A1/A2), so the trace never reads them for H2D, only from a source that measured it for this frame:
    //  - the recon WORKER, on its own thread, from the slot telemetry it has just written (configure and H2D);
    //  - the GUI presenter, from the backend call it just made for this frame (H2D), which replaces the worker's value;
    //  - the presenter's frame-id mismatch, which resets H2D to unknown (the retained buffer belonged to another frame).
    double configureMs = kUnknownMs;   // used only when workerSplit; any other frame keeps the caller's read of its own telemetry
    double h2dMs = kUnknownMs;
    bool workerSplit = false;   // set by the render thread when it consumed the worker's recon for this frame
};

/*! The process-wide switch for the stage hooks (MLVAPP_PLAYBACK_PRESENT_TRACE, set by the window next to the recorder's own
 *  switch). One relaxed load per hook: a stamp is a pure timing note, it orders nothing. */
inline std::atomic<bool> &stampGate()
{
    static std::atomic<bool> gate( false );
    return gate;
}
inline bool stampsEnabled() { return stampGate().load( std::memory_order_relaxed ); }
inline void setStampsEnabled( bool on ) { stampGate().store( on, std::memory_order_relaxed ); }

/*! Takes a stage stamp from a clock value the caller already holds; a no-op when the trace is off. */
inline void stamp( double &destination, double nowSeconds )
{
    if( stampsEnabled() ) destination = nowSeconds;
}

/*! One presented frame. Times are ms since the Play's smoke start; -1 is unknown (stage stamps: kUnknownMs). */
struct Row
{
    int frame = 0;                // displayFrame
    int position = 0;             // timeline (slider) position at the present
    double presentMs = 0.0;       // the present was recorded (notePlaybackSmokePresentedFrame)
    double intervalMs = 0.0;      // since the previous present (0 for the first)
    double readyMs = -1.0;        // the render thread emitted this frame ready
    double uiLatencyMs = 0.0;     // ready emit -> drawFrameReady picked it up (GUI thread)
    double drawMs = 0.0;          // drawFrameReady total
    double renderMs = 0.0;        // render work
    double queueMs = 0.0;         // render queue wait
    double grabMs = 0.0;          // contact-sheet grab cost booked since the previous present
    double creditFrames = 0.0;    // pace guard credit at the present
    double prevEndMs = -1.0;      // the previous present's presentPlaybackPreparedFrame returned
    // the playback timer since the previous present
    int ticks = 0;                // timerFrameEvent calls (poll and early advance)
    int busyTicks = 0;            // ... that found the render still drawing
    int predictiveTicks = 0;      // ... that were the early advance on a present
    double maxTickGapMs = 0.0;    // the longest gap between two consecutive ticks (or previous present -> first tick)
    int advances = 0;             // ticks that moved the timeline position
    double advancedFrames = 0.0;  // the fractional source frames those advances granted
    double lastAdvanceMs = -1.0;  // the last advance before this present: the pace target the present answered
    double lastTickDiffMs = 0.0;  // that advance's shaped timeDiff
    int audioSyncs = 0;           // audio re-syncs applied since the previous present
    // PLAYBACK-TRACE-LOWOVERHEAD-1 r1: this frame's stage stamps (ms since the Play's start; kUnknownMs when not taken).
    // readyMs, presentMs and prevEndMs above complete the chain request .. present.
    double reqMs = kUnknownMs;        // requested
    double dqMs = kUnknownMs;         // queued for decode
    double dsMs = kUnknownMs;         // decode began
    double deMs = kUnknownMs;         // decode ended
    double rsMs = kUnknownMs;         // recon began
    double reMs = kUnknownMs;         // recon ended
    double rndMs = kUnknownMs;        // render thread began
    double drawBeginMs = kUnknownMs;  // GUI draw began
    double configureMs = kUnknownMs;  // llrawproc dual-ISO configure span (a duration, not a stamp)
    double h2dMs = kUnknownMs;        // async H2D upload span (a duration, not a stamp)

    /*! Loads the worker stamps (absolute stage-clock seconds; 0 = unknown) as ABSOLUTE ms, ready for notePresent. */
    void setStages( const StageStamps &s )
    {
        const auto ms = []( double seconds ) { return seconds > 0.0 ? seconds * 1000.0 : kUnknownMs; };
        reqMs = ms( s.requestS );
        dqMs = ms( s.decodeQueueS );
        dsMs = ms( s.decodeStartS );
        deMs = ms( s.decodeEndS );
        rsMs = ms( s.reconStartS );
        reMs = ms( s.reconEndS );
        rndMs = ms( s.renderStartS );
        drawBeginMs = ms( s.drawBeginS );
        // H2D always comes from the stamps (unknown when no source measured it for this frame, never a stale value).
        // Configure follows the worker's span for a frame that consumed the worker's recon; any other frame keeps what the
        // caller read from the frame's own telemetry.
        h2dMs = s.h2dMs;
        if( s.workerSplit ) configureMs = s.configureMs;
    }

    /*! True when the stamps that are known never run backwards along request .. present (1 = ordered). */
    bool stagesOrdered() const
    {
        // readyMs alone uses -1 for unknown; every other stamp is unknown only at kUnknownMs, so a real rebased -1 ms stamp
        // still takes part (adversary a7085920 A2)
        const double ready = readyMs == -1.0 ? kUnknownMs : readyMs;
        const double chain[] = { reqMs, dqMs, dsMs, deMs, rsMs, reMs, rndMs, ready, drawBeginMs, presentMs };
        double last = kUnknownMs;
        for( double t : chain )
        {
            // a real rebased stamp is never below -1e8
            if( t <= kUnknownMs / 2.0 ) continue;
            if( t < last ) return false;
            last = t;
        }
        return true;
    }
};

class Recorder
{
public:
    /*! Read once by the caller (environment); off by default. Takes effect at the next begin(). */
    void setEnabled( bool enabled ) { m_enabled = enabled; }
    bool enabled() const { return m_enabled; }

    /*! A measured Play starts at \a originMs (absolute ms). Rows of the previous Play are dropped. The rows live in a
     *  FIXED ring of \a expectedRows slots allocated here, once, so a present never allocates; a Play that presents more
     *  frames than that keeps the newest ones (dropped() says how many were overwritten). */
    void begin( double originMs, size_t expectedRows )
    {
        if( !m_enabled ) return;
        m_active = true;
        m_originMs = originMs;
        m_ring.assign( expectedRows > 0 ? expectedRows : 1, Row() );
        m_total = 0;
        m_pending = Row();
        m_lastTickMs = -1.0;
        m_lastPresentMs = -1.0;
        m_lastPresentEndMs = -1.0;
    }

    void end() { m_active = false; }
    bool active() const { return m_enabled && m_active; }

    void noteTick( double nowMs, bool busy, bool predictive )
    {
        if( !active() ) return;
        const double t = nowMs - m_originMs;
        const double since = m_lastTickMs >= 0.0 ? m_lastTickMs : m_lastPresentMs;
        if( since >= 0.0 && t - since > m_pending.maxTickGapMs ) m_pending.maxTickGapMs = t - since;
        m_lastTickMs = t;
        ++m_pending.ticks;
        if( busy ) ++m_pending.busyTicks;
        if( predictive ) ++m_pending.predictiveTicks;
    }

    /*! A tick ran playbackHandling: the drop-frame position went from \a fromPosition to \a toPosition. */
    void noteAdvance( double nowMs, double fromPosition, double toPosition, int tickDiffMs )
    {
        if( !active() || !( toPosition != fromPosition ) ) return;
        ++m_pending.advances;
        m_pending.advancedFrames += toPosition - fromPosition;
        m_pending.lastAdvanceMs = nowMs - m_originMs;
        m_pending.lastTickDiffMs = tickDiffMs;
    }

    void noteGrab( double grabMs )
    {
        if( !active() ) return;
        m_pending.grabMs += grabMs;
    }

    void noteAudioSync()
    {
        if( !active() ) return;
        ++m_pending.audioSyncs;
    }

    /*! The present: \a row carries the frame's own fields (frame .. creditFrames, absolute ms for presentMs and
     *  readyMs); the timer fields come from the notes since the previous present. */
    void notePresent( Row row )
    {
        if( !active() ) return;
        row.presentMs -= m_originMs;
        if( row.readyMs >= 0.0 ) row.readyMs -= m_originMs;
        for( double *stage : { &row.reqMs, &row.dqMs, &row.dsMs, &row.deMs, &row.rsMs, &row.reMs, &row.rndMs,
                               &row.drawBeginMs } )
            if( *stage > kUnknownMs / 2.0 ) *stage -= m_originMs;   // a stage may precede the Play's start: it stays negative
        row.intervalMs = m_lastPresentMs >= 0.0 ? row.presentMs - m_lastPresentMs : 0.0;
        row.prevEndMs = m_lastPresentEndMs;
        row.ticks = m_pending.ticks;
        row.busyTicks = m_pending.busyTicks;
        row.predictiveTicks = m_pending.predictiveTicks;
        row.maxTickGapMs = m_pending.maxTickGapMs;
        row.advances = m_pending.advances;
        row.advancedFrames = m_pending.advancedFrames;
        row.lastAdvanceMs = m_pending.lastAdvanceMs;
        row.lastTickDiffMs = m_pending.lastTickDiffMs;
        row.grabMs = m_pending.grabMs;
        row.audioSyncs = m_pending.audioSyncs;
        m_ring[m_total % m_ring.size()] = row;   // a fixed slot: no allocation
        ++m_total;
        m_pending = Row();
        m_lastPresentMs = row.presentMs;
        m_lastTickMs = -1.0;
    }

    /*! The present's GUI work (presentPlaybackPreparedFrame) returned at \a nowMs. */
    void notePresentEnd( double nowMs )
    {
        if( !active() ) return;
        m_lastPresentEndMs = nowMs - m_originMs;
    }

    /*! The rows kept (at most the ring's size), oldest first: row(0) is the present numbered firstIndex(). */
    size_t size() const { return m_total < m_ring.size() ? m_total : m_ring.size(); }
    size_t capacity() const { return m_ring.size(); }
    size_t total() const { return m_total; }                 // presents recorded this Play
    size_t dropped() const { return m_total - size(); }      // overwritten by the ring
    size_t firstIndex() const { return dropped(); }
    bool empty() const { return size() == 0; }
    const Row &row( size_t logicalIndex ) const { return m_ring[( firstIndex() + logicalIndex ) % m_ring.size()]; }
    const Row *storage() const { return m_ring.data(); }     // the ring itself (a test pins that it never moves)

    /*! The column names, in formatRow's order (new columns are only ever appended). */
    static const char *header()
    {
        return "i,frame,pos,t,iv,ready,uil,draw,render,queue,grab,credit,prev_end,ticks,busy,pred,max_tick_gap,"
               "adv,adv_frames,last_adv,last_tick_diff,audio_syncs,"
               "req,dq,ds,de,rs,re,rnd,draw_begin,configure,h2d,stage_ok";
    }

    static std::string formatRow( size_t index, const Row &r )
    {
        char buf[640];
        std::snprintf( buf, sizeof( buf ),
                       "%d,%d,%d,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.4f,%.3f,%d,%d,%d,%.3f,%d,%.4f,%.3f,%.0f,%d,"
                       "%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%d",
                       static_cast<int>( index ), r.frame, r.position, r.presentMs, r.intervalMs, r.readyMs, r.uiLatencyMs, r.drawMs,
                       r.renderMs, r.queueMs, r.grabMs, r.creditFrames, r.prevEndMs, r.ticks, r.busyTicks,
                       r.predictiveTicks, r.maxTickGapMs, r.advances, r.advancedFrames, r.lastAdvanceMs,
                       r.lastTickDiffMs, r.audioSyncs,
                       r.reqMs, r.dqMs, r.dsMs, r.deMs, r.rsMs, r.reMs, r.rndMs, r.drawBeginMs, r.configureMs, r.h2dMs,
                       r.stagesOrdered() ? 1 : 0 );
        return std::string( buf );
    }

private:
    bool m_enabled = false;
    bool m_active = false;
    double m_originMs = 0.0;
    double m_lastTickMs = -1.0;
    double m_lastPresentMs = -1.0;
    double m_lastPresentEndMs = -1.0;
    Row m_pending;
    std::vector<Row> m_ring;   // fixed slots, sized once in begin()
    size_t m_total = 0;        // presents recorded; slot = m_total % m_ring.size()
};

} // namespace playback_present_trace

#endif // PLAYBACKPRESENTTRACE_H
