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

#include <cstdio>
#include <string>
#include <vector>

namespace playback_present_trace
{

/*! One presented frame. Times are ms since the Play's smoke start; -1 is unknown. */
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
};

class Recorder
{
public:
    /*! Read once by the caller (environment); off by default. Takes effect at the next begin(). */
    void setEnabled( bool enabled ) { m_enabled = enabled; }
    bool enabled() const { return m_enabled; }

    /*! A measured Play starts at \a originMs (absolute ms). Rows of the previous Play are dropped. */
    void begin( double originMs, size_t expectedRows )
    {
        if( !m_enabled ) return;
        m_active = true;
        m_originMs = originMs;
        m_rows.clear();
        m_rows.reserve( expectedRows );
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
        m_rows.push_back( row );
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

    const std::vector<Row> &rows() const { return m_rows; }

    /*! The column names, in formatRow's order. */
    static const char *header()
    {
        return "i,frame,pos,t,iv,ready,uil,draw,render,queue,grab,credit,prev_end,ticks,busy,pred,max_tick_gap,"
               "adv,adv_frames,last_adv,last_tick_diff,audio_syncs";
    }

    static std::string formatRow( size_t index, const Row &r )
    {
        char buf[384];
        std::snprintf( buf, sizeof( buf ),
                       "%d,%d,%d,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.4f,%.3f,%d,%d,%d,%.3f,%d,%.4f,%.3f,%.0f,%d",
                       static_cast<int>( index ), r.frame, r.position, r.presentMs, r.intervalMs, r.readyMs, r.uiLatencyMs, r.drawMs,
                       r.renderMs, r.queueMs, r.grabMs, r.creditFrames, r.prevEndMs, r.ticks, r.busyTicks,
                       r.predictiveTicks, r.maxTickGapMs, r.advances, r.advancedFrames, r.lastAdvanceMs,
                       r.lastTickDiffMs, r.audioSyncs );
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
    std::vector<Row> m_rows;
};

} // namespace playback_present_trace

#endif // PLAYBACKPRESENTTRACE_H
