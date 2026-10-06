/*!
 * \file PlaybackSlipHistogram.h
 * \brief Pure per-present slip accounting for the playback smoke, extracted so it can be unit-tested
 *        with an injected clock and no GUI (pattern: PlaybackNativePaceGuard.h).
 *
 * PLAYBACK-BACHELOR-PRESENT-JITTER-1: presented_fps_after_first_present says HOW MANY frames a leg
 * lost, never WHICH or WHY. This header counts each lost frame at the present that skipped it and
 * gives it one cause, so the lever is chosen from evidence:
 *
 *   slip(present k) = displayFrame(k) - displayFrame(k-1) - 1, forward steps only. A backward step
 *   (a loop wrap) is counted on its own, never as a slip; a repeat of the same frame is not a slip.
 *   The first present is never a slip: the frames the timeline passed before it, and the jump its own
 *   early advance made (repaying the wait for the first frame), are startup_catchup_frames.
 *
 *   Each slip gets one class, first match wins:
 *     CAPTURE        a contact-sheet grab (GUI-thread framebuffer readback) ran in the interval;
 *     GAP            the interval is >= 250 ms;
 *     GUI_LATE       the presented frame was ready before the skipped frame's deadline, but presenting
 *                    it took more than one period (ready-to-present latency or draw total);
 *     UPSTREAM_LATE  the presented frame was ready after the skipped frame's deadline; sub-tagged with
 *                    the stage that ran over 2x its session median (DECODE, RECON, RENDER, QUEUE), or
 *                    NONE;
 *     CLOCK          the timeline advanced two or more frames within an interval <= 1.25 periods;
 *     OTHER          none of the above (reported so the class counts always add up).
 *   The deadline of frame N is the wall time at which the timeline first moved past N (recorded by
 *   noteTimelineMove, one store per engine advance). The "skipped frame" of a slip is the first frame
 *   it skipped, displayFrame(k-1) + 1.
 *
 * Slips are counted in FRAMES (a 3-frame jump is 3 slips), so slips_per_1000 compares directly with
 * the owner bar: 23.9 fps at 23.976 native allows 3.2 lost frames per 1000 timeline frames.
 *
 * The same object reconciles the timeline: every engine advance reports its path (drop-frame tick,
 * whole-frame tick, loop wrap) and the pace-guard grant; any slider move while playing that no engine
 * path made is OTHER, and whatever the paths do not explain at the end is folded into OTHER too, so the
 * per-path sum always equals the session's timeline delta.
 */

#ifndef PLAYBACKSLIPHISTOGRAM_H
#define PLAYBACKSLIPHISTOGRAM_H

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdlib>
#include <vector>

#include "PlaybackNativePaceGuard.h"

namespace playback_slip
{

inline constexpr int kSlipBucketCount = 6;        // {0, 1, 2, 3, 4-7, 8+}
inline constexpr int kIntervalBucketCount = 6;    // {<=45, 45-62.5, 62.5-83.4, 83.4-125, 125-250, >250} ms
inline constexpr int kMaxSlipLines = 64;          // per-slip detail lines kept per session
inline constexpr double kGapIntervalMs = 250.0;
inline constexpr double kClockMaxIntervalPeriods = 1.25;
inline constexpr double kStageOverMedianFactor = 2.0;
inline constexpr int kDeadlineRingSize = 1024;    // frames whose deadline is remembered
inline constexpr int kMaxStageSamples = 100000;   // per-present stage values kept for the medians

/*! Slip size histogram bucket: 0, 1, 2, 3, 4-7, 8+. */
inline int slipSizeBucket( int slip )
{
    if( slip <= 0 ) return 0;
    if( slip <= 3 ) return slip;
    if( slip <= 7 ) return 4;
    return 5;
}

/*! Present interval histogram bucket; each bucket includes its upper edge (45 ms is bucket 0). */
inline int presentIntervalBucket( double intervalMs )
{
    if( intervalMs <= 45.0 ) return 0;
    if( intervalMs <= 62.5 ) return 1;
    if( intervalMs <= 83.4 ) return 2;
    if( intervalMs <= 125.0 ) return 3;
    if( intervalMs <= 250.0 ) return 4;
    return 5;
}

enum class AdvancePath { DropTick = 0, WholeFrame, LoopWrap, Other, Count };
enum class SlipClass { Capture = 0, Gap, GuiLate, UpstreamLate, Clock, Other, None, Count };
enum class UpstreamStage { Decode = 0, Recon, Render, Queue, None, Count };

inline const char *slipClassName( SlipClass c )
{
    switch( c )
    {
    case SlipClass::Capture: return "capture";
    case SlipClass::Gap: return "gap";
    case SlipClass::GuiLate: return "gui_late";
    case SlipClass::UpstreamLate: return "upstream_late";
    case SlipClass::Clock: return "clock";
    case SlipClass::Other: return "other";
    default: return "none";
    }
}

inline const char *upstreamStageName( UpstreamStage s )
{
    switch( s )
    {
    case UpstreamStage::Decode: return "decode";
    case UpstreamStage::Recon: return "recon";
    case UpstreamStage::Render: return "render";
    case UpstreamStage::Queue: return "queue";
    default: return "none";
    }
}

/*! One presented frame, as notePlaybackSmokePresentedFrame sees it. Times are wall ms on the same
 *  clock as noteTimelineMove (mlv_stage_timing_now() x 1000 in the app; injected in the tests). */
struct PresentSample
{
    int displayFrame = 0;
    double presentMs = 0.0;
    bool readyKnown = false;        // the render thread's ready signal was stamped
    double readyMs = 0.0;           // when the render thread signalled the frame ready (readyKnown)
    double decodeMs = 0.0;          // raw_uint16_ms
    double reconMs = 0.0;           // llrawproc_total_ms
    double renderMs = 0.0;          // render_thread_work_ms
    double queueMs = 0.0;           // render_thread_queue_wait_ms
    double drawMs = 0.0;            // draw total
    double uiLatencyMs = 0.0;       // present UI-signal latency
    int timelinePosition = 0;       // the slider after this present's own early advance
    bool lookaheadCovered = false;  // the presented frame was already requested by the lookahead
};

struct SlipRecord
{
    int frame = 0;                  // the display frame of the present that skipped
    int size = 0;
    double intervalMs = 0.0;
    SlipClass cls = SlipClass::Other;
    UpstreamStage sub = UpstreamStage::None;
    double decodeMs = 0.0;
    double reconMs = 0.0;
    double renderMs = 0.0;
    double queueMs = 0.0;
    double drawMs = 0.0;
    double uiLatencyMs = 0.0;
    double grabMs = 0.0;
    bool lookaheadCovered = false;
    double paceCreditFrames = 0.0;
    // classification inputs
    bool grabRan = false;
    bool readyKnown = false;
    double readyMs = 0.0;
    bool deadlineKnown = false;     // the timeline was seen moving past the skipped frame
    double deadlineMs = 0.0;
    double presentMs = 0.0;
    int timelineAdvancedInInterval = 0;
};

struct Summary
{
    int presents = 0;
    int slipEvents = 0;
    long long slipsTotal = 0;
    double slipsPer1000 = 0.0;
    double nonCapturePer1000 = 0.0;
    double nonCaptureNonGapPer1000 = 0.0;
    int maxSlip = 0;
    double maxIntervalMs = 0.0;
    int maxIntervalFrame = -1;
    SlipClass maxIntervalClass = SlipClass::None;
    int startupCatchupFrames = 0;
    int startupCatchupAfterFirstFrames = 0;  // the part repaid by skips after the first present (delayed catch-up)
    int startupWaitCreditFrames = 0;         // the wall time since Play the first present had not yet repaid, in frames
    int timelineDeltaAtFirstPresent = 0;
    int timelineFramesAfterFirst = 0;
    double timelineAfterFirstFps = 0.0;      // paced: the delayed startup catch-up is not counted
    double timelineAfterFirstRawFps = 0.0;   // every timeline frame after the first present (pace_summary before this card)
    double presentedAfterFirstFps = 0.0;
    double nativeEquivPresentedFps = 0.0;
    std::array<long long, kSlipBucketCount> histSlip {};
    std::array<long long, kIntervalBucketCount> histInterval {};
    std::array<long long, static_cast<int>( SlipClass::Count )> classFrames {};
    std::array<long long, static_cast<int>( UpstreamStage::Count )> upstreamStageFrames {};
    std::array<long long, static_cast<int>( AdvancePath::Count )> advanceByPath {};
    double paceGuardGrantedFrames = 0.0;
    double paceGuardGrantedAfterFirstFrames = 0.0;
    double paceGuardMaxLeadFrames = 0.0;   // > 0: the guard granted more than kMaxCarry + elapsed x pace over some span
    double paceGuardMaxLeadMs = 0.0;       // when (ms after the session's first grant) that lead peaked
    int paceGuardRearms = 0;               // grants that found the guard unarmed (the first one included)
    double paceGuardMaxGrantFrames = 0.0;  // the largest single grant
    int paceGuardBursts = 0;               // grants of more than 2 frames (credit banked while no grant ran)
    double paceGuardBurstFrames = 0.0;     // what those grants gave beyond one frame each
    double paceGuardFpsMin = 0.0;
    double paceGuardFpsMax = 0.0;
    int wraps = 0;
    int repeats = 0;
    int grabs = 0;
    double grabMsTotal = 0.0;
    int lookaheadUncoveredSlipEvents = 0;
    std::vector<SlipRecord> slipLines;   // the first kMaxSlipLines slips, classified
};

class SlipHistogram
{
public:
    /*! New session: \a startPosition is the slider at Play, \a paceFps the pace (period = 1000 / pace). */
    void reset( int startPosition, double paceFps )
    {
        *this = SlipHistogram();
        m_startPosition = startPosition;
        m_timelinePosition = startPosition;
        m_paceKnown = paceFps > 0.0;
        m_paceFps = m_paceKnown ? paceFps : 0.0;
        m_periodMs = m_paceKnown ? 1000.0 / m_paceFps : 0.0;
        for( auto &d : m_deadlines ) d = Deadline();
    }

    /*! The Play's wall time. Without it no skip after the first present is taken for startup catch-up. */
    void setPlayStart( double playStartMs )
    {
        m_playStartKnown = true;
        m_playStartMs = playStartMs;
    }

    /*! The timeline (slider) moved to \a toPosition by \a path at \a nowMs; \a grantedFrames is what the pace
     *  guard granted for it (0 for OTHER) and \a creditAfter the credit it left. \a guardPaceFps is the pace the
     *  guard was handed for this grant (0: the session pace) and \a guardRearmed says the guard was not armed
     *  before it. One call per engine advance. */
    void noteTimelineMove( AdvancePath path, int toPosition, double nowMs, double grantedFrames = 0.0,
                           double creditAfter = 0.0, double guardPaceFps = 0.0, bool guardRearmed = false )
    {
        const int from = m_timelinePosition;
        const int delta = toPosition - from;
        m_advanceByPath[static_cast<int>( path )] += delta;
        if( path != AdvancePath::Other )
        {
            m_paceGuardGrantedFrames += grantedFrames;
            if( m_presents > 0 ) m_paceGuardGrantedAfterFirstFrames += grantedFrames;
            m_lastCreditFrames = creditAfter;
            noteGuardGrant( nowMs, grantedFrames, guardPaceFps > 0.0 ? guardPaceFps : m_paceFps, guardRearmed );
        }
        if( delta > 0 && path != AdvancePath::LoopWrap )
        {
            m_timelineAdvanceSincePresent += delta;
            const int first = std::max( from, toPosition - kDeadlineRingSize );
            for( int f = first; f < toPosition; ++f )
            {
                Deadline &d = m_deadlines[static_cast<size_t>( ( f % kDeadlineRingSize + kDeadlineRingSize ) % kDeadlineRingSize )];
                if( d.frame != f )
                {
                    d.frame = f;
                    d.ms = nowMs;
                }
            }
        }
        m_timelinePosition = toPosition;
    }

    /*! A contact-sheet grab of \a grabMs ran on the GUI thread; it is charged to the next present's interval. */
    void noteGrab( double grabMs )
    {
        m_pendingGrab = true;
        m_pendingGrabMs += std::max( 0.0, grabMs );
        ++m_grabs;
        m_grabMsTotal += std::max( 0.0, grabMs );
    }

    void notePresent( const PresentSample &s )
    {
        if( static_cast<int>( m_decode.size() ) < kMaxStageSamples )
        {
            m_decode.push_back( s.decodeMs );
            m_recon.push_back( s.reconMs );
            m_render.push_back( s.renderMs );
            m_queue.push_back( s.queueMs );
        }
        if( m_presents == 0 )
        {
            m_presents = 1;
            m_firstPresentMs = s.presentMs;
            m_lastPresentMs = s.presentMs;
            m_lastDisplayFrame = s.displayFrame;
            m_timelineAtFirstPresent = s.timelinePosition;
            m_timelineAdvanceSincePresent = 0;
            // The frames passed before the first present, plus whatever its own early advance jumped past the
            // next frame (the repaid wait for the first frame): never slips.
            m_startupCatchupFrames = std::max( 0, s.displayFrame - m_startPosition );
            m_firstPresentJumpFrames = std::max( 0, s.timelinePosition - s.displayFrame - 1 );
            // The drop-frame engine owes the wall time since Play: what the first present's own advance did not repay
            // (the guard banks credit while no grant runs) is repaid by a burst a few presents later.
            m_waitCreditFrames = 0;
            if( m_playStartKnown && m_paceKnown )
            {
                const int owed = static_cast<int>( owedFramesAt( s.presentMs ) );
                m_waitCreditFrames = std::max( 0, owed - std::abs( s.timelinePosition - m_startPosition ) );
            }
            m_pendingGrab = false;
            m_pendingGrabMs = 0.0;
            return;
        }

        ++m_presents;
        const double intervalMs = s.presentMs > m_lastPresentMs ? s.presentMs - m_lastPresentMs : 0.0;
        ++m_histInterval[static_cast<size_t>( presentIntervalBucket( intervalMs ) )];
        const bool grabRan = m_pendingGrab;
        const double grabMs = m_pendingGrabMs;
        m_pendingGrab = false;
        m_pendingGrabMs = 0.0;
        const int advanced = m_timelineAdvanceSincePresent;
        m_timelineAdvanceSincePresent = 0;

        int slip = 0;
        if( s.displayFrame < m_lastDisplayFrame )
        {
            ++m_wraps;
        }
        else if( s.displayFrame == m_lastDisplayFrame )
        {
            ++m_repeats;
        }
        else
        {
            slip = s.displayFrame - m_lastDisplayFrame - 1;
            if( m_presents == 2 && m_firstPresentJumpFrames > 0 )
            {
                const int catchup = std::min( slip, m_firstPresentJumpFrames );
                slip -= catchup;
                m_startupCatchupFrames += catchup;
            }
            // Delayed startup catch-up: while the timeline is still behind the wall time since Play, a skip repays the
            // wait for the first frame (never more than that wait's credit in all).
            if( slip > 0 && !m_caughtUp && m_waitCreditFrames > m_catchupAfterFirstFrames )
            {
                const int catchup = std::min( slip, m_waitCreditFrames - m_catchupAfterFirstFrames );
                slip -= catchup;
                m_catchupAfterFirstFrames += catchup;
                m_startupCatchupFrames += catchup;
            }
        }
        if( !m_caughtUp && m_playStartKnown && m_paceKnown
         && std::abs( s.timelinePosition - m_startPosition ) + 1.0 >= owedFramesAt( s.presentMs ) )
        {
            m_caughtUp = true;
        }
        ++m_histSlip[static_cast<size_t>( slipSizeBucket( slip ) )];

        int slipIndex = -1;
        if( slip > 0 )
        {
            SlipRecord r;
            r.frame = s.displayFrame;
            r.size = slip;
            r.intervalMs = intervalMs;
            r.decodeMs = s.decodeMs;
            r.reconMs = s.reconMs;
            r.renderMs = s.renderMs;
            r.queueMs = s.queueMs;
            r.drawMs = s.drawMs;
            r.uiLatencyMs = s.uiLatencyMs;
            r.grabMs = grabMs;
            r.grabRan = grabRan;
            r.lookaheadCovered = s.lookaheadCovered;
            r.paceCreditFrames = m_lastCreditFrames;
            r.readyKnown = s.readyKnown;
            r.readyMs = s.readyMs;
            r.deadlineKnown = deadlineOf( m_lastDisplayFrame + 1, &r.deadlineMs );
            r.presentMs = s.presentMs;
            r.timelineAdvancedInInterval = advanced;
            m_slips.push_back( r );
            slipIndex = static_cast<int>( m_slips.size() ) - 1;
        }
        if( intervalMs > m_maxIntervalMs )
        {
            m_maxIntervalMs = intervalMs;
            m_maxIntervalFrame = s.displayFrame;
            m_maxIntervalSlipIndex = slipIndex;
        }
        m_lastPresentMs = s.presentMs;
        m_lastDisplayFrame = s.displayFrame;
    }

    /*! Classify every slip against the session medians and total the session. \a nowMs is the session end and
     *  \a endPosition the slider then; whatever the advance paths did not explain is folded into OTHER. */
    Summary finish( double nowMs, int endPosition ) const
    {
        Summary out;
        out.presents = m_presents;
        out.startupCatchupFrames = m_startupCatchupFrames;
        out.timelineDeltaAtFirstPresent = m_presents > 0 ? std::abs( m_timelineAtFirstPresent - m_startPosition ) : 0;
        out.histSlip = m_histSlip;
        out.histInterval = m_histInterval;
        out.wraps = m_wraps;
        out.repeats = m_repeats;
        out.grabs = m_grabs;
        out.grabMsTotal = m_grabMsTotal;
        out.paceGuardGrantedFrames = m_paceGuardGrantedFrames;
        out.paceGuardGrantedAfterFirstFrames = m_paceGuardGrantedAfterFirstFrames;
        out.paceGuardMaxLeadFrames = m_guardMaxLead;
        out.paceGuardMaxLeadMs = m_guardMaxLeadMs;
        out.paceGuardRearms = m_guardRearms;
        out.paceGuardMaxGrantFrames = m_guardMaxGrant;
        out.paceGuardBursts = m_guardBursts;
        out.paceGuardBurstFrames = m_guardBurstFrames;
        out.startupCatchupAfterFirstFrames = m_catchupAfterFirstFrames;
        out.startupWaitCreditFrames = m_waitCreditFrames;
        out.paceGuardFpsMin = m_guardPaceMin;
        out.paceGuardFpsMax = m_guardPaceMax;
        out.maxIntervalMs = m_maxIntervalMs;
        out.maxIntervalFrame = m_maxIntervalFrame;
        out.advanceByPath = m_advanceByPath;
        long long explained = 0;
        for( long long v : m_advanceByPath ) explained += v;
        out.advanceByPath[static_cast<int>( AdvancePath::Other )] +=
            static_cast<long long>( endPosition - m_startPosition ) - explained;

        const double medDecode = median( m_decode );
        const double medRecon = median( m_recon );
        const double medRender = median( m_render );
        const double medQueue = median( m_queue );

        for( size_t i = 0; i < m_slips.size(); ++i )
        {
            SlipRecord r = m_slips[i];
            r.cls = classify( r );
            r.sub = r.cls == SlipClass::UpstreamLate
                ? overMedianStage( r, medDecode, medRecon, medRender, medQueue )
                : UpstreamStage::None;
            ++out.slipEvents;
            out.slipsTotal += r.size;
            out.maxSlip = std::max( out.maxSlip, r.size );
            out.classFrames[static_cast<int>( r.cls )] += r.size;
            if( r.cls == SlipClass::UpstreamLate ) out.upstreamStageFrames[static_cast<int>( r.sub )] += r.size;
            if( !r.lookaheadCovered ) ++out.lookaheadUncoveredSlipEvents;
            if( static_cast<int>( i ) == m_maxIntervalSlipIndex ) out.maxIntervalClass = r.cls;
            if( static_cast<int>( out.slipLines.size() ) < kMaxSlipLines ) out.slipLines.push_back( r );
        }

        if( m_presents > 0 )
        {
            const int rawAfterFirst = std::max( 0, std::abs( endPosition - m_startPosition )
                                                   - std::abs( m_timelineAtFirstPresent - m_startPosition ) );
            out.timelineFramesAfterFirst = std::max( 0, rawAfterFirst - m_catchupAfterFirstFrames );
            out.timelineAfterFirstFps = playback_native_pace::fpsAfterFirstPresent(
                out.timelineFramesAfterFirst, nowMs, m_firstPresentMs, m_presents );
            out.timelineAfterFirstRawFps = playback_native_pace::fpsAfterFirstPresent(
                rawAfterFirst, nowMs, m_firstPresentMs, m_presents );
            out.presentedAfterFirstFps = playback_native_pace::fpsAfterFirstPresent(
                m_presents - 1, nowMs, m_firstPresentMs, m_presents );
            if( out.timelineFramesAfterFirst > 0 )
            {
                out.nativeEquivPresentedFps =
                    m_paceFps * static_cast<double>( m_presents - 1 ) / out.timelineFramesAfterFirst;
                const double per = 1000.0 / out.timelineFramesAfterFirst;
                const long long capture = out.classFrames[static_cast<int>( SlipClass::Capture )];
                const long long gap = out.classFrames[static_cast<int>( SlipClass::Gap )];
                out.slipsPer1000 = out.slipsTotal * per;
                out.nonCapturePer1000 = ( out.slipsTotal - capture ) * per;
                out.nonCaptureNonGapPer1000 = ( out.slipsTotal - capture - gap ) * per;
            }
        }
        return out;
    }

    double periodMs() const { return m_periodMs; }
    int timelinePosition() const { return m_timelinePosition; }

private:
    struct Deadline
    {
        int frame = -1;
        double ms = -1.0;
    };

    /*! NativePaceGuard's contract, checked from outside: over any span of grants, granted <= kMaxCarry (1) +
     *  elapsed x pace. With S the running granted sum and Q = S - pace x t, a span (i, j] leads its ceiling by
     *  Q_j - min Q_i - 1 (the arm itself is the span's start at t of the first grant). A lead above 0 means the
     *  guard granted more than its own ceiling -- a re-arm, a bypass, or a clock the smoke does not share. */
    void noteGuardGrant( double nowMs, double grantedFrames, double paceFps, bool rearmed )
    {
        if( rearmed ) ++m_guardRearms;
        if( grantedFrames > m_guardMaxGrant ) m_guardMaxGrant = grantedFrames;
        if( grantedFrames > 2.0 )
        {
            ++m_guardBursts;
            m_guardBurstFrames += grantedFrames - 1.0;
        }
        if( paceFps > 0.0 )
        {
            if( m_guardGrants == 0 || paceFps < m_guardPaceMin ) m_guardPaceMin = paceFps;
            if( m_guardGrants == 0 || paceFps > m_guardPaceMax ) m_guardPaceMax = paceFps;
        }
        const double pace = m_paceFps > 0.0 ? m_paceFps : paceFps;
        if( m_guardGrants == 0 )
        {
            m_guardFirstGrantMs = nowMs;
            m_guardQMin = -pace * nowMs / 1000.0;
        }
        ++m_guardGrants;
        m_guardGrantSum += grantedFrames;
        const double q = m_guardGrantSum - pace * nowMs / 1000.0;
        const double lead = q - m_guardQMin - 1.0;
        if( m_guardGrants == 1 || lead > m_guardMaxLead )
        {
            m_guardMaxLead = lead;
            m_guardMaxLeadMs = nowMs - m_guardFirstGrantMs;
        }
        if( q < m_guardQMin ) m_guardQMin = q;
    }

    /*! The wall time since Play at \a nowMs, in pace frames (never negative). */
    double owedFramesAt( double nowMs ) const
    {
        return std::max( 0.0, nowMs - m_playStartMs ) * m_paceFps / 1000.0;
    }

    bool deadlineOf( int frame, double *deadlineMs ) const
    {
        const Deadline &d = m_deadlines[static_cast<size_t>( ( frame % kDeadlineRingSize + kDeadlineRingSize ) % kDeadlineRingSize )];
        if( d.frame != frame ) return false;
        *deadlineMs = d.ms;
        return true;
    }

    SlipClass classify( const SlipRecord &r ) const
    {
        if( r.grabRan ) return SlipClass::Capture;
        if( r.intervalMs >= kGapIntervalMs ) return SlipClass::Gap;
        const bool known = r.readyKnown && r.deadlineKnown;
        if( known && r.readyMs <= r.deadlineMs && m_paceKnown
         && ( r.presentMs - r.readyMs > m_periodMs || r.drawMs > m_periodMs ) )
            return SlipClass::GuiLate;
        if( known && r.readyMs > r.deadlineMs ) return SlipClass::UpstreamLate;
        if( r.timelineAdvancedInInterval >= 2 && m_paceKnown
         && r.intervalMs <= kClockMaxIntervalPeriods * m_periodMs )
            return SlipClass::Clock;
        return SlipClass::Other;
    }

    static UpstreamStage overMedianStage( const SlipRecord &r, double medDecode, double medRecon,
                                          double medRender, double medQueue )
    {
        const double values[4] = { r.decodeMs, r.reconMs, r.renderMs, r.queueMs };
        const double medians[4] = { medDecode, medRecon, medRender, medQueue };
        int best = -1;
        double bestRatio = 0.0;
        for( int i = 0; i < 4; ++i )
        {
            if( !( values[i] > kStageOverMedianFactor * medians[i] ) || !( values[i] > 0.0 ) ) continue;
            const double ratio = medians[i] > 0.0 ? values[i] / medians[i] : 1e300;
            if( best < 0 || ratio > bestRatio )
            {
                best = i;
                bestRatio = ratio;
            }
        }
        return best < 0 ? UpstreamStage::None : static_cast<UpstreamStage>( best );
    }

    static double median( std::vector<double> v )
    {
        if( v.empty() ) return 0.0;
        const size_t mid = v.size() / 2;
        std::nth_element( v.begin(), v.begin() + static_cast<std::ptrdiff_t>( mid ), v.end() );
        double m = v[mid];
        if( v.size() % 2 == 0 )
        {
            const double lower = *std::max_element( v.begin(), v.begin() + static_cast<std::ptrdiff_t>( mid ) );
            m = 0.5 * ( m + lower );
        }
        return m;
    }

    int m_startPosition = 0;
    int m_timelinePosition = 0;
    double m_paceFps = 0.0;
    double m_periodMs = 0.0;

    int m_presents = 0;
    double m_firstPresentMs = 0.0;
    double m_lastPresentMs = 0.0;
    int m_lastDisplayFrame = 0;
    int m_timelineAtFirstPresent = 0;
    int m_startupCatchupFrames = 0;
    int m_firstPresentJumpFrames = 0;
    int m_timelineAdvanceSincePresent = 0;
    int m_wraps = 0;
    int m_repeats = 0;

    bool m_pendingGrab = false;
    double m_pendingGrabMs = 0.0;
    int m_grabs = 0;
    double m_grabMsTotal = 0.0;

    double m_maxIntervalMs = 0.0;
    int m_maxIntervalFrame = -1;
    int m_maxIntervalSlipIndex = -1;

    double m_paceGuardGrantedFrames = 0.0;
    double m_paceGuardGrantedAfterFirstFrames = 0.0;
    double m_lastCreditFrames = 0.0;

    long long m_guardGrants = 0;
    int m_guardRearms = 0;
    double m_guardGrantSum = 0.0;
    double m_guardFirstGrantMs = 0.0;
    double m_guardQMin = 0.0;
    double m_guardMaxLead = 0.0;
    double m_guardMaxLeadMs = 0.0;
    double m_guardPaceMin = 0.0;
    double m_guardPaceMax = 0.0;
    double m_guardMaxGrant = 0.0;
    int m_guardBursts = 0;
    double m_guardBurstFrames = 0.0;

    bool m_playStartKnown = false;
    bool m_paceKnown = false;
    double m_playStartMs = 0.0;
    int m_waitCreditFrames = 0;
    int m_catchupAfterFirstFrames = 0;
    bool m_caughtUp = false;

    std::array<long long, kSlipBucketCount> m_histSlip {};
    std::array<long long, kIntervalBucketCount> m_histInterval {};
    std::array<long long, static_cast<int>( AdvancePath::Count )> m_advanceByPath {};
    std::array<Deadline, kDeadlineRingSize> m_deadlines {};
    std::vector<SlipRecord> m_slips;
    std::vector<double> m_decode;
    std::vector<double> m_recon;
    std::vector<double> m_render;
    std::vector<double> m_queue;
};

} // namespace playback_slip

#endif // PLAYBACKSLIPHISTOGRAM_H
