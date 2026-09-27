/*!
 * \file PlaybackPresentedFrameIdentityTracker.h
 * \brief Distinct-source-frame-OCCURRENCE dedup for playback-smoke request
 *        and presentation counting -- extracted so it can be unit-tested
 *        without the GUI.
 *
 * CUDA-ATTRIBUTION-BASELINE-1 round 5 (sol BLOCKER): MainWindow used to feed
 * PlaybackFramePopulationPolicy::computeSourceFramePopulation() raw
 * presentation-EVENT counts (one ++ per completed present). Nothing upstream
 * dedups by source-frame (displayFrame) index before that count happens, so
 * the same source frame presented three times inflated the "presented"
 * bucket exactly as much as three distinct frames presented once each --
 * identity-blind, and able to certify partition_sound=true / 0% loss on a
 * session that silently re-presented stale frames instead of advancing.
 *
 * CUDA-ATTRIBUTION-BASELINE-1 round 6 (sol + astra BLOCKER, round 5
 * INVERTED): keying identity by raw displayFrame ALONE over-corrects --
 * offeredSourceFrames (MainWindow::m_playbackTimelineSourceFramesOffered)
 * accumulates every LAP of a looping session (it is elapsed playback TIME in
 * frame units, not a set of clip positions), so three healthy laps over a
 * 24-frame span offer 72 occurrences. A tracker keyed by displayFrame alone
 * can hold at most 24 distinct values no matter how many honest laps
 * present them, which collapses the numerator onto a completely different
 * basis than the denominator and reports a healthy repeated-loop session as
 * ~2/3 lost. The identity this tracker records is therefore the pair
 * (loop epoch, displayFrame) -- see PresentationContext::
 * playbackSmokeLoopEpoch in RenderFrameThread.h for how the epoch is
 * captured per-request. Within one lap this still collapses a stale
 * re-present of the same displayFrame (the round-5 defect stays fixed);
 * across laps it does not (the round-6 defect is fixed), because a
 * presentation in lap 1 and a presentation in lap 2 are different pairs.
 *
 * CUDA-ATTRIBUTION-BASELINE-1 round 6 (astra BLOCKER, "Summing per-origin
 * unique counts double-counts overlapping source identities"): the same
 * source-frame occurrence can reach presentation via EITHER the target path
 * or a lookahead path, but never as two distinct pictures shown to the
 * user -- it is one fact, not two. presentedOccurrenceUnionCount() is the
 * union of the two origin sets for exactly this reason; the two origin-
 * specific counts remain available separately for diagnostics (which path
 * delivered it) but must never be summed into the loss denominator.
 *
 * CUDA-ATTRIBUTION-BASELINE-1 round 6 (astra major, "Request-event
 * subtraction still certifies incorrect never-requested versus
 * requested-then-skipped attribution"): this tracker also now records
 * REQUESTED occurrences (not just presented ones), by the same (loop epoch,
 * displayFrame) identity, split by origin. Bucket membership is then a
 * literal QSet difference against the SAME identity a presentation would
 * have used -- "requested via target but never presented via any origin" --
 * rather than subtracting an EVENT count (which overcounts whenever the
 * same source frame is re-requested without advancing, e.g. drop-frame mode
 * setting m_frameChanged on every timer tick regardless of whether the
 * computed position actually crossed a whole-frame boundary) from an
 * IDENTITY count. A reused lookahead-covers-current target attempt
 * (MainWindow's playbackLookaheadCoversCurrent branch) is simply never
 * recorded as a target request at all -- it issues no new request -- which
 * is why round 4's separate reusedLookaheadTargetFramesThisSession
 * back-out parameter is gone: the exclusion is now structural (the call
 * site that would record it is never reached) rather than arithmetic.
 *
 * Bounded: a playback-smoke session is time-boxed (-Seconds, typically tens
 * of seconds) and displayFrame values are drawn from the loaded clip's
 * finite frame range, so the distinct-value count each set holds cannot
 * exceed min(clip frame count, session duration x clip fps) per lap, times
 * the number of laps completed in the session -- both bounded in practice,
 * and reset (cleared) at every playback-smoke session start.
 *
 * CUDA-ATTRIBUTION-BASELINE-1 round 6 (sol + astra major, "the growing
 * identity tracker runs during ordinary GUI playback"): every insertion
 * into these sets is gated by MainWindow on m_playbackSmokeFrameTelemetry
 * (the MLVAPP_PLAYBACK_SMOKE_TELEMETRY env-var flag), not merely on
 * m_playbackSmokeActive (which is true for every ordinary Play click). The
 * "time-boxed smoke session" bound above is therefore only true of sessions
 * that actually opted into frame telemetry; ordinary interactive playback
 * never touches these sets at all. See the gating call sites in
 * MainWindow::drawFrame(), MainWindow::queuePlaybackLookaheadRequests(),
 * and MainWindow::notePlaybackSmokePresentedFrame().
 *
 * CUDA-ATTRIBUTION-BASELINE-1 round 7 (astra major, "Lookahead requests
 * outside the measured offered window are counted inside the partition"):
 * a speculative lookahead is requested AHEAD of the position playback has
 * actually reached -- e.g. a depth-1 lookahead for the current target frame
 * asks for the NEXT source frame before that frame has itself been offered.
 * astra's repro: occurrences 1..60 offered/requested/presented as targets,
 * each with a depth-1 lookahead one past it (2..61); the lookahead for
 * target 60 asks for occurrence 61, which the session's measurement window
 * never actually reached (it ends at 60). That request is real (a render
 * was issued for it) but its identity falls OUTSIDE what was ever offered,
 * so counting it as "requested then discarded" over- (or under-, depending
 * on parity) states the partition against a window it was never inside.
 * noteOfferedFrame() records the offered CEILING -- the (loop epoch,
 * displayFrame) identity of the most recent position playbackHandling()
 * actually advanced to and drawFrame() was asked to display, updated at the
 * same call site regardless of whether that specific call issues a genuine
 * request or reuses an existing lookahead (MainWindow::drawFrame(), before
 * the playbackLookaheadCoversCurrent branch). Every occurrence-count query
 * below intersects its source set(s) with "identity <= ceiling" before
 * counting, so a lookahead requested for a position the session's own
 * advancement never reached is excluded from every bucket, exactly astra's
 * "intersect request/presentation identities with an explicitly defined
 * offered population" remedy. Encoded identities compare correctly in this
 * ordering because occurrenceIdentity() packs loopEpoch into the high bits:
 * a later lap's identity is always numerically greater than an earlier
 * lap's, matching occurrence order.
 */

#ifndef PLAYBACKPRESENTEDFRAMEIDENTITYTRACKER_H
#define PLAYBACKPRESENTEDFRAMEIDENTITYTRACKER_H

#include <cstdint>

#include <QSet>

class PlaybackPresentedFrameIdentityTracker
{
public:
    void reset()
    {
        m_requestedTargetOccurrences.clear();
        m_requestedLookaheadOccurrences.clear();
        m_presentedTargetOccurrences.clear();
        m_presentedLookaheadOccurrences.clear();
        m_hasOfferedCeiling = false;
        m_offeredCeiling = 0;
    }

    /*! \param loopEpoch which lap displayFrame belongs to.
     *  \param displayFrame the source-frame index playback has actually
     *         advanced to and asked to display this call -- see the class
     *         doc comment's round-7 note. Call regardless of whether this
     *         specific call issues a genuine request or reuses an existing
     *         lookahead; the ceiling tracks the offered POSITION, not
     *         request activity. Monotonic: only ever advances the ceiling,
     *         never retreats it. */
    void noteOfferedFrame( uint64_t loopEpoch, uint64_t displayFrame )
    {
        const uint64_t occurrence = occurrenceIdentity( loopEpoch, displayFrame );
        if( !m_hasOfferedCeiling || occurrence > m_offeredCeiling )
        {
            m_offeredCeiling = occurrence;
            m_hasOfferedCeiling = true;
        }
    }

    /*! True once noteOfferedFrame() has been called this session -- i.e.
     *  false exactly until the session's first offered occurrence.
     *  MainWindow::drawFrame() reads it to recognise that first occurrence
     *  (CUDA-ATTRIBUTION-BASELINE-1 hub fix, sol r12 BLOCKER; see
     *  PlaybackFramePopulationPolicy::computeSourceFramePopulation()'s
     *  startOccurrenceOffered note). */
    bool hasOfferedCeiling() const
    {
        return m_hasOfferedCeiling;
    }

    /*! \param loopEpoch which lap displayFrame belongs to -- see
     *         PresentationContext::playbackSmokeLoopEpoch.
     *  \param displayFrame the source-frame index a genuinely new request
     *         (not a lookahead-reuse) was just issued for.
     *  \param viaLookahead true iff this is a speculative render-lookahead
     *         request rather than the one target request drawFrame() issues
     *         per call. */
    void noteRequestedFrame( uint64_t loopEpoch, uint64_t displayFrame, bool viaLookahead )
    {
        const uint64_t occurrence = occurrenceIdentity( loopEpoch, displayFrame );
        if( viaLookahead )
            m_requestedLookaheadOccurrences.insert( occurrence );
        else
            m_requestedTargetOccurrences.insert( occurrence );
    }

    /*! \param loopEpoch which lap displayFrame belongs to -- see
     *         PresentationContext::playbackSmokeLoopEpoch.
     *  \param displayFrame the source-frame index that just reached
     *         presentation.
     *  \param viaLookahead true iff the presenting request's
     *         PresentationRequestContext::playbackLookaheadRequest was true.
     */
    void notePresentedFrame( uint64_t loopEpoch, uint64_t displayFrame, bool viaLookahead )
    {
        const uint64_t occurrence = occurrenceIdentity( loopEpoch, displayFrame );
        if( viaLookahead )
            m_presentedLookaheadOccurrences.insert( occurrence );
        else
            m_presentedTargetOccurrences.insert( occurrence );
    }

    uint64_t distinctTargetPresentedCount() const
    {
        return static_cast<uint64_t>( withinOfferedCeiling( m_presentedTargetOccurrences ).size() );
    }

    uint64_t distinctLookaheadPresentedCount() const
    {
        return static_cast<uint64_t>( withinOfferedCeiling( m_presentedLookaheadOccurrences ).size() );
    }

    /*! The TOTAL distinct source-frame occurrences actually shown, counting
     *  an occurrence presented via both origins once, not twice -- see the
     *  class doc comment's astra-overlap note. */
    uint64_t presentedOccurrenceUnionCount() const
    {
        QSet<uint64_t> unionSet = withinOfferedCeiling( m_presentedTargetOccurrences );
        unionSet.unite( withinOfferedCeiling( m_presentedLookaheadOccurrences ) );
        return static_cast<uint64_t>( unionSet.size() );
    }

    /*! Occurrences a genuine (non-reuse) target request was issued for, that
     *  never reached presentation via EITHER origin. Requested identities
     *  are intersected with the offered ceiling BEFORE the subtraction
     *  (round 7); presented identities never need it -- see the class doc
     *  comment's round-7 note on why a presentation cannot outrun the
     *  ceiling. */
    uint64_t requestedThenSkippedTargetCount() const
    {
        QSet<uint64_t> skipped = withinOfferedCeiling( m_requestedTargetOccurrences );
        skipped.subtract( m_presentedTargetOccurrences );
        skipped.subtract( m_presentedLookaheadOccurrences );
        return static_cast<uint64_t>( skipped.size() );
    }

    /*! Occurrences a speculative lookahead request was issued for, that
     *  never reached presentation via EITHER origin. */
    uint64_t requestedThenDiscardedLookaheadCount() const
    {
        QSet<uint64_t> discarded = withinOfferedCeiling( m_requestedLookaheadOccurrences );
        discarded.subtract( m_presentedTargetOccurrences );
        discarded.subtract( m_presentedLookaheadOccurrences );
        return static_cast<uint64_t>( discarded.size() );
    }

    /*! The TOTAL distinct source-frame occurrences ever asked for, by
     *  either origin -- the basis for "was this offered occurrence ever
     *  requested at all". */
    uint64_t requestedOccurrenceUnionCount() const
    {
        QSet<uint64_t> unionSet = withinOfferedCeiling( m_requestedTargetOccurrences );
        unionSet.unite( withinOfferedCeiling( m_requestedLookaheadOccurrences ) );
        return static_cast<uint64_t>( unionSet.size() );
    }

    /*! Combines (loopEpoch, displayFrame) into one identity, not just
     *  displayFrame -- see the class doc comment for why raw displayFrame
     *  alone collapses every lap of a looping session onto the same
     *  identities. displayFrame is cast down to a uint32_t at every other
     *  call site in this codebase that carries it (e.g. PresentationContext
     *  ::frameNumber, MainWindow::requestContext.frameNumber), so the low
     *  32 bits are sufficient; loopEpoch occupies the high 32 bits, which is
     *  sufficient for any session with fewer than four billion loop
     *  wraps. */
    static uint64_t occurrenceIdentity( uint64_t loopEpoch, uint64_t displayFrame )
    {
        return ( loopEpoch << 32 ) | ( displayFrame & 0xFFFFFFFFull );
    }

private:
    /*! Returns the subset of `occurrences` whose identity is <=
     *  m_offeredCeiling. Without a ceiling yet (noteOfferedFrame() never
     *  called this session -- production always calls it before any
     *  request/presentation identity for the same drawFrame() invocation,
     *  see the class doc comment's round-7 note, so this branch is a
     *  pre-first-call/test-fixture case only), pass `occurrences` through
     *  unfiltered rather than guessing an empty window. */
    QSet<uint64_t> withinOfferedCeiling( const QSet<uint64_t> &occurrences ) const
    {
        if( !m_hasOfferedCeiling ) return occurrences;
        QSet<uint64_t> filtered;
        filtered.reserve( occurrences.size() );
        for( uint64_t occurrence : occurrences )
        {
            if( occurrence <= m_offeredCeiling )
                filtered.insert( occurrence );
        }
        return filtered;
    }

    QSet<uint64_t> m_requestedTargetOccurrences;
    QSet<uint64_t> m_requestedLookaheadOccurrences;
    QSet<uint64_t> m_presentedTargetOccurrences;
    QSet<uint64_t> m_presentedLookaheadOccurrences;
    uint64_t m_offeredCeiling = 0;
    bool m_hasOfferedCeiling = false;
};

#endif // PLAYBACKPRESENTEDFRAMEIDENTITYTRACKER_H
