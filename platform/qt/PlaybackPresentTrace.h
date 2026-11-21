/*!
 * \file PlaybackPresentTrace.h
 * \brief PLAYBACK-STARTUP-CATCHUP-BURST-1 r7: an opt-in per-present playback trace (MLVAPP_PLAYBACK_PRESENT_TRACE=1).
 *
 * The smoke log carries per-frame detail only for a slip, so a long present interval with no skip had no record of
 * what preceded it (r6: head3 legs with one 109-120 ms interval mid-clip, 3 of 4 not slips). With the trace on, every
 * presented frame of a measured Play is offered one row, and the recorder keeps the NEWEST rows that fit its fixed ring
 * (4096 in the window; a longer Play overwrites the oldest and dropped() says how many): when the frame was presented, when
 * its render was ready, how long the GUI thread took to pick it up and draw it, the pace guard's credit, and what the
 * playback timer did since the previous present (ticks, the longest gap between two ticks, busy ticks, the advances it made
 * and the last one's time). The rows are kept in memory and written to the log after the Play stops, so the trace never
 * writes inside the timed window.
 *
 * Off (the default) it is inert: every note*() returns before touching state, nothing is allocated, and no row is
 * written. The caller reads the flag once.
 */

#ifndef PLAYBACKPRESENTTRACE_H
#define PLAYBACKPRESENTTRACE_H

#include <atomic>
#include <cmath>
#include <cstdint>
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
    // r10 (sol r9 A1, INV-1 "stage timestamps name the sampled event"): requestS, decodeQueueS and renderStartS say which sample they hold. The clock reads are
    // where they always were; only the statement of the point each one is taken at changed.
    double requestS = 0.0;      // the request: sampled in renderFrame after the m_mutex wait (the request's own clock value, RenderRequest::requestStageTime)
    double decodeQueueS = 0.0;  // the decode-queue function-entry sample: taken on entry to queueDecodeRequestLocked, BEFORE the slot is set up (not the enqueue instant)
    double decodeStartS = 0.0;  // the decode-start sample: on entry to decodeFrameForWorker, after the worker took the queue entry
    double decodeEndS = 0.0;    // the decode-end sample: right after the raw decode (getMlvRawFrameUint16) returned, before the slot-state transition
    double reconStartS = 0.0;   // the recon-start sample: in reconFrameForWorker AFTER the OpenMP team setup and the warm/slot checks (team setup is before it)
    double reconEndS = 0.0;     // the recon-end sample: at the tail of reconFrameForWorker, after the ProcessReady transition (taken only with the trace on)
    double renderStartS = 0.0;  // render_start, after drawFrame's prologue: the value HEAVY logs as render_thread_start_stage_time (ready - rnd == render)
    double drawBeginS = 0.0;    // the GUI thread began drawing it (drawFrameReady's display start)
    // r3 (adversary 81c5805c A1/A2, HUB RULING 2026-11-20T11:30Z): the LIGHT trace carries STAMPS only. A per-frame configure or
    // H2D span would come from the backend's last-run preupload status, whose lifetime is wrong at the source (a stale value
    // survives a CPU fallback; a synchronous upload reports 0), so those spans stay in the HEAVY dump and are not trusted here.
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

/*! The stage stamps a frame slot carries into its render (RenderFrameThread::drawFrame, render thread, before resetMetadata).
 *  The workers' stamps survive only for the frame they belong to: the slot came through the decode queue for THIS request
 *  (\a slotQueuedSerial == \a activeSerial). A slot that did not holds an older frame's stamps (or none), and they are dropped.
 *  r8 (sol r7 B2): a frame no worker queued (the inline render, runSerial) still carries its request stamp: when no worker took
 *  it, it comes from the active request's own clock value \a activeRequestStageTimeS, the value queueDecodeRequestLocked stamps
 *  for a queued frame. With the trace off nothing is stamped. */
inline StageStamps stampsForRender( const StageStamps &slotStamps, uint64_t slotQueuedSerial, uint64_t activeSerial,
                                    double activeRequestStageTimeS )
{
    StageStamps stamps = slotQueuedSerial == activeSerial ? slotStamps : StageStamps();
    if( !( stamps.requestS > 0.0 ) ) stamp( stamps.requestS, activeRequestStageTimeS );
    return stamps;
}

/*! One presented frame. Times are ms since the Play's smoke start; -1 is unknown (stage stamps: kUnknownMs). */
struct Row
{
    int frame = 0;                // displayFrame
    int position = 0;             // timeline (slider) position at the present
    double presentMs = 0.0;       // the present was recorded (notePlaybackSmokePresentedFrame); kUnknownMs when the input was not finite
    double intervalMs = 0.0;      // since the previous present (0 for the first)
    double readyMs = -1.0;        // the render thread emitted this frame ready (input: absolute ms, -1 = not known)
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
    double reqMs = kUnknownMs;        // request: sampled after the m_mutex wait in renderFrame
    double dqMs = kUnknownMs;         // the decode-queue function-entry sample (queueDecodeRequestLocked entry, before the slot is set up)
    double dsMs = kUnknownMs;         // the decode-start sample (decodeFrameForWorker entry, after the queue take)
    double deMs = kUnknownMs;         // the decode-end sample (right after the raw decode returned)
    double rsMs = kUnknownMs;         // the recon-start sample (reconFrameForWorker, after the OpenMP team setup)
    double reMs = kUnknownMs;         // the recon-end sample (reconFrameForWorker tail, after the ProcessReady transition)
    double rndMs = kUnknownMs;        // render_start, after drawFrame's prologue (HEAVY's render_thread_start_stage_time; ready - rnd == render)
    double drawBeginMs = kUnknownMs;  // GUI draw began

    /*! Which of the ten chain stamps (request .. present, bit i = chain position i) this row KNOWS. Set in ONE place,
     *  notePresent, from the INPUT: an absolute stage-clock time that is finite and not negative is known (the present: any
     *  finite value). The rebased value may then be any finite number (-1.0 or -5e8 included), so no value can say
     *  "unknown" and none is tested for it. A row that never passed notePresent knows nothing. */
    unsigned knownMask = 0;
    /*! notePresent's verdict on the order of the KNOWN stamps, judged on the ABSOLUTE inputs before they were rebased (rebasing
     *  against a large origin rounds two different stamps onto one value). A row that never passed notePresent is "ordered". */
    bool ordered = true;
    static constexpr int kChainLength = 10;
    static constexpr int kReadyIndex = 7;
    static constexpr int kPresentIndex = 9;

    /*! The chain fields in request .. present order. */
    void chainFields( double *( &out )[kChainLength] )
    {
        double *const f[kChainLength] = { &reqMs, &dqMs, &dsMs, &deMs, &rsMs, &reMs, &rndMs, &readyMs, &drawBeginMs, &presentMs };
        for( int i = 0; i < kChainLength; ++i ) out[i] = f[i];
    }

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
    }

    /*! True when the KNOWN stamps never run backwards along request .. present (1 = ordered). It orders what was stamped;
     *  it does not say every stage was instrumented. Presence is knownMask, never a value (adversary a7085920 A2, sol r3 B1,
     *  sol r4 B1/B2). The verdict is the one notePresent stored (\a ordered), judged on the absolute inputs: the stored,
     *  rebased columns are for the log and are not what the order is read from (fable key3 r5 B1). */
    bool stagesOrdered() const { return ordered; }

    /*! The order check itself, over the ten ABSOLUTE chain values (request .. present) and the mask of the known ones. Every
     *  known value is finite (notePresent's entry rule), so the comparison cannot meet a NaN. */
    static bool chainOrdered( const double ( &absolute )[kChainLength], unsigned mask )
    {
        bool haveLast = false;
        double last = 0.0;
        for( int i = 0; i < kChainLength; ++i )
        {
            if( !( ( mask >> i ) & 1u ) ) continue;
            if( haveLast && absolute[i] < last ) return false;
            last = absolute[i];
            haveLast = true;
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
        if( !std::isfinite( originMs ) )
        {
            // REFUSED (fable key3 r5 B1): a NaN or infinite origin has no meaning, and every rebased stamp would inherit it. The
            // Play is simply not recorded: rows of the previous Play are dropped and active() stays false.
            m_active = false;
            m_total = 0;
            m_pending = Row();
            m_ring.clear();
            return;
        }
        m_active = true;
        m_originMs = originMs;
        m_ring.assign( expectedRows > 0 ? expectedRows : 1, Row() );
        m_total = 0;
        m_pending = Row();
        // "have a previous ..." is a flag, never a value a real (possibly negative) rebased time could equal
        m_haveTick = false;
        m_havePresent = false;
        m_havePresentEnd = false;
        m_lastTickMs = 0.0;
        m_lastPresentMs = 0.0;
        m_lastPresentEndMs = 0.0;
    }

    void end() { m_active = false; }
    bool active() const { return m_enabled && m_active; }

    void noteTick( double nowMs, bool busy, bool predictive )
    {
        if( !active() ) return;
        const double t = nowMs - m_originMs;
        const double since = m_haveTick ? m_lastTickMs : m_lastPresentMs;
        if( ( m_haveTick || m_havePresent ) && t - since > m_pending.maxTickGapMs ) m_pending.maxTickGapMs = t - since;
        m_lastTickMs = t;
        m_haveTick = true;
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
        // The ONE place a Row's chain stamps are judged, on the INPUT (absolute stage-clock ms). A stage stamp is known when it
        // is finite and not negative; the present is known when it is finite. A known stamp whose rebase (input - origin)
        // would overflow is unknown too. An unknown one (NaN or infinite, kUnknownMs, ready's -1) is stored as its column's
        // documented marker, so every stored value is finite and no later code can meet a NaN. The ORDER is judged here, on
        // the absolute inputs and before any rebase (which can round two stamps onto one value), and the verdict is stored;
        // a known stamp's presence is the mask bit, never its value.
        double *chain[Row::kChainLength];
        row.chainFields( chain );
        double absolute[Row::kChainLength];
        double rebased[Row::kChainLength];
        row.knownMask = 0;
        for( int i = 0; i < Row::kChainLength; ++i )
        {
            const double input = *chain[i];
            absolute[i] = input;
            rebased[i] = input - m_originMs;
            const bool inputKnown = i == Row::kPresentIndex ? std::isfinite( input ) : ( std::isfinite( input ) && input >= 0.0 );
            if( inputKnown && std::isfinite( rebased[i] ) ) row.knownMask |= 1u << i;
        }
        row.ordered = Row::chainOrdered( absolute, row.knownMask );
        for( int i = 0; i < Row::kChainLength; ++i )
        {
            if( ( row.knownMask >> i ) & 1u )
                *chain[i] = rebased[i];
            else
                *chain[i] = i == Row::kReadyIndex ? -1.0 : kUnknownMs;   // each column's documented "unknown"
        }
        const bool presentKnown = ( row.knownMask >> Row::kPresentIndex ) & 1u;
        // An unknown present (NaN, infinite) is not an event the timer can be measured from: the interval stays on the last KNOWN
        // present, and the tick history is kept (r8, sol r7 B1), so the next gap is tick-to-tick.
        row.intervalMs = ( presentKnown && m_havePresent ) ? row.presentMs - m_lastPresentMs : 0.0;
        row.prevEndMs = m_havePresentEnd ? m_lastPresentEndMs : -1.0;   // the column's documented "none yet"
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
        if( presentKnown )
        {
            m_lastPresentMs = row.presentMs;
            m_havePresent = true;
            m_haveTick = false;   // only a KNOWN present restarts the tick history (the next gap is present -> first tick)
        }
    }

    /*! The present's GUI work (presentPlaybackPreparedFrame) returned at \a nowMs. */
    void notePresentEnd( double nowMs )
    {
        if( !active() ) return;
        m_lastPresentEndMs = nowMs - m_originMs;
        m_havePresentEnd = true;
    }

    /*! The rows kept (at most the ring's size), oldest first: row(0) is the present numbered firstIndex(). */
    size_t size() const { return m_total < m_ring.size() ? m_total : m_ring.size(); }
    size_t capacity() const { return m_ring.size(); }          // the ring's SLOTS (what begin() asked for)
    size_t allocated() const { return m_ring.capacity(); }     // the backing storage's own capacity: 0 until begin() allocates
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
               "req,dq,ds,de,rs,re,rnd,draw_begin,stage_ok";
    }

    static std::string formatRow( size_t index, const Row &r )
    {
        // Sized from the row itself (fable key3 r5 B2): a finite double can print 300+ characters with %.3f, so no fixed buffer
        // is safe. This runs after the Play has stopped, so the second snprintf is off the timed path.
        static const char *const kFormat =
            "%d,%d,%d,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.4f,%.3f,%d,%d,%d,%.3f,%d,%.4f,%.3f,%.0f,%d,"
            "%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%d";
        const auto print = [&]( char *destination, size_t capacity )
        {
            return std::snprintf( destination, capacity, kFormat,
                                  static_cast<int>( index ), r.frame, r.position, r.presentMs, r.intervalMs, r.readyMs,
                                  r.uiLatencyMs, r.drawMs, r.renderMs, r.queueMs, r.grabMs, r.creditFrames, r.prevEndMs,
                                  r.ticks, r.busyTicks, r.predictiveTicks, r.maxTickGapMs, r.advances, r.advancedFrames,
                                  r.lastAdvanceMs, r.lastTickDiffMs, r.audioSyncs, r.reqMs, r.dqMs, r.dsMs, r.deMs, r.rsMs,
                                  r.reMs, r.rndMs, r.drawBeginMs, r.stagesOrdered() ? 1 : 0 );
        };
        const int needed = print( nullptr, 0 );
        if( needed <= 0 ) return std::string();
        std::string line( static_cast<size_t>( needed ), '\0' );
        print( &line[0], line.size() + 1 );
        return line;
    }

private:
    bool m_enabled = false;
    bool m_active = false;
    double m_originMs = 0.0;
    bool m_haveTick = false;
    bool m_havePresent = false;
    bool m_havePresentEnd = false;
    double m_lastTickMs = 0.0;
    double m_lastPresentMs = 0.0;
    double m_lastPresentEndMs = 0.0;
    Row m_pending;
    std::vector<Row> m_ring;   // fixed slots, sized once in begin()
    size_t m_total = 0;        // presents recorded; slot = m_total % m_ring.size()
};

} // namespace playback_present_trace

#endif // PLAYBACKPRESENTTRACE_H
