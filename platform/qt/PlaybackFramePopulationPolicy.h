/*!
 * \file PlaybackFramePopulationPolicy.h
 * \brief Pure arithmetic: how many frames were actually asked for during a
 *        playback smoke session, and how many of those never reached
 *        presentation -- extracted so it can be unit-tested without the GUI.
 *
 * CUDA-ATTRIBUTION-BASELINE-1 round 2 (astra major 4 / sol major 1): a
 * monotonic render-request-serial delta is immune to timeline-position loop
 * wraps, but MainWindow::m_nextRenderRequestSerial advances for BOTH the one
 * target request drawFrame() issues per call and every speculative
 * render-lookahead request -- most of which are intentionally discarded
 * (never the frame playback ends up waiting on) and never reach
 * presentation. Using that counter's delta alone as the presented-frame
 * population denominator counts every discarded lookahead as "skipped",
 * overcounting loss whenever lookahead is enabled. The target-only counter
 * (MainWindow::m_nextTargetRenderRequestSerial) is both wrap-immune (same
 * monotonic-counter argument) and free of that overcount.
 */

#ifndef PLAYBACKFRAMEPOPULATIONPOLICY_H
#define PLAYBACKFRAMEPOPULATIONPOLICY_H

#include <cmath>
#include <cstdint>

struct PlaybackFramePopulation
{
    uint64_t requestedFramesBySerial = 0;
    uint64_t skippedOrUnpresentedBySerial = 0;
    uint64_t requestedTargetFramesBySerial = 0;
    uint64_t skippedOrUnpresentedByTargetSerial = 0;
    uint64_t lookaheadRequestsBySerial = 0;
};

/*! CUDA-ATTRIBUTION-BASELINE-1 round 3 (astra major, prior finding 4 NOT
 *  RESOLVED): every field above is derived from REQUEST counters, so a
 *  source frame drop-frame catch-up skipped over before ever issuing a
 *  request for it is invisible to all of them -- an "optimisation" that
 *  quietly requests fewer source frames passes cleanly. This struct starts
 *  instead from offeredSourceFrames (how many source frames the
 *  clip/timeline actually offered over the measured window, tracked by
 *  MainWindow::m_playbackTimelineSourceFramesOffered independent of request
 *  activity) and partitions it into four named, non-overlapping buckets that
 *  are PROVEN -- not assumed -- to sum back to it; see partitionSound.
 *
 *  CUDA-ATTRIBUTION-BASELINE-1 round 6 (astra major, "Request-event
 *  subtraction still certifies incorrect never-requested versus
 *  requested-then-skipped attribution"): the four buckets below are now
 *  derived from source-frame-OCCURRENCE IDENTITY sets (see
 *  PlaybackPresentedFrameIdentityTracker), not from subtracting a
 *  presented-frame count off a request-EVENT count. An event count
 *  overcounts "requested" whenever the same source frame is re-requested
 *  without advancing (e.g. drop-frame mode's m_frameChanged firing every
 *  timer tick regardless of whether the computed position crossed a
 *  whole-frame boundary), which used to misattribute those duplicate-event
 *  occurrences as requestedThenSkippedTargetFrames when they were in fact
 *  never-distinctly-requested. Bucket membership is now decided by one rule
 *  a test can name directly: an occurrence is presented if it is a member of
 *  either presented-occurrence set; requested-then-skipped/discarded if it
 *  is a member of the corresponding requested-occurrence set but not
 *  presented; never-requested if it is not a member of either
 *  requested-occurrence set. */
struct PlaybackSourceFramePopulation
{
    uint64_t offeredSourceFrames = 0;
    uint64_t neverRequestedSourceFrames = 0;
    uint64_t requestedThenDiscardedLookaheadFrames = 0;
    uint64_t requestedThenSkippedTargetFrames = 0;
    uint64_t presentedViaTargetFrames = 0;
    uint64_t presentedViaLookaheadFrames = 0;
    /*! CUDA-ATTRIBUTION-BASELINE-1 round 6 (astra BLOCKER, "Summing
     *  per-origin unique counts double-counts overlapping source
     *  identities"): the UNION of the target- and lookahead-presented
     *  occurrence sets, not their sum -- a source frame reaching
     *  presentation via both origins is one fact (one picture the user
     *  saw), not two. presentedViaTargetFrames/presentedViaLookaheadFrames
     *  above remain per-origin diagnostics only; this field, not their sum,
     *  is what the four-bucket partition and the loss ratio use. */
    uint64_t presentedFrames = 0;
    /*! CUDA-ATTRIBUTION-BASELINE-1 round 7 (astra major, "the telemetry-off
     *  session reports a CONFIDENT 100% loss"): true iff frame telemetry
     *  (MLVAPP_PLAYBACK_SMOKE_TELEMETRY) was active for this session, i.e.
     *  the identity-tracker inputs below are real measurements rather than
     *  the vacuous zeros a session that never gated any insertion at all
     *  produces. When false, partitionSound is forced false and every
     *  bucket below except offeredSourceFrames is forced to 0 -- attribution
     *  is UNAVAILABLE, a third state distinct from both "zero loss" and
     *  "100% loss", never a numeric ratio a consumer can silently accept.
     *  See PlaybackPresentedFrameIdentityTracker.h's telemetry-gating note. */
    bool attributionMeasured = false;
    /*! True iff every source-frame occurrence requested (via either origin)
     *  was requested via exactly one of {target-only, lookahead-only, both}
     *  in a way the four buckets can account for without double-counting --
     *  i.e. offeredSourceFrames >= the distinct requested-occurrence count,
     *  AND presentedFrames + requestedThenDiscardedLookaheadFrames +
     *  requestedThenSkippedTargetFrames == that same distinct
     *  requested-occurrence count. False means one of those assumptions
     *  broke (for example a single occurrence was genuinely requested via
     *  BOTH origins and never presented via either, so it is counted once
     *  in each skip/discard bucket) and the partition should be treated as
     *  UNKNOWN attribution rather than trusted -- the third-state rule this
     *  card also applies to timing. */
    bool partitionSound = false;
};

class PlaybackFramePopulationPolicy
{
public:
    static uint64_t nonNegativeDelta( uint64_t current, uint64_t start )
    {
        return current >= start ? current - start : 0;
    }

    /*! \param nextRenderRequestSerial the ALL-requests counter's current
     *         value (target requests + speculative lookaheads).
     *  \param startRequestSerial that counter's value when the smoke session
     *         started.
     *  \param nextTargetRenderRequestSerial the TARGET-only counter's
     *         current value.
     *  \param startTargetRequestSerial that counter's value when the smoke
     *         session started.
     *  \param presentedFrames frames that actually reached presentation.
     */
    static PlaybackFramePopulation compute( uint64_t nextRenderRequestSerial,
                                            uint64_t startRequestSerial,
                                            uint64_t nextTargetRenderRequestSerial,
                                            uint64_t startTargetRequestSerial,
                                            uint64_t presentedFrames )
    {
        PlaybackFramePopulation result;
        result.requestedFramesBySerial =
            nonNegativeDelta( nextRenderRequestSerial, startRequestSerial );
        result.requestedTargetFramesBySerial =
            nonNegativeDelta( nextTargetRenderRequestSerial, startTargetRequestSerial );
        result.skippedOrUnpresentedBySerial =
            result.requestedFramesBySerial >= presentedFrames
                ? result.requestedFramesBySerial - presentedFrames
                : 0;
        result.skippedOrUnpresentedByTargetSerial =
            result.requestedTargetFramesBySerial >= presentedFrames
                ? result.requestedTargetFramesBySerial - presentedFrames
                : 0;
        result.lookaheadRequestsBySerial =
            result.requestedFramesBySerial >= result.requestedTargetFramesBySerial
                ? result.requestedFramesBySerial - result.requestedTargetFramesBySerial
                : 0;
        return result;
    }

    /*! \param timelineSourceFramesOfferedNow the current value of
     *         MainWindow::m_playbackTimelineSourceFramesOffered -- a
     *         monotonic, request-independent accumulator of source-frame-
     *         units of elapsed playback time (see its declaration comment).
     *  \param timelineSourceFramesOfferedStart that accumulator's value when
     *         the smoke session started.
     *  \param requestedOccurrenceUnionCount CUDA-ATTRIBUTION-BASELINE-1
     *         round 6 (astra major, "Request identities must be reconciled
     *         too"): PlaybackPresentedFrameIdentityTracker::
     *         requestedOccurrenceUnionCount() -- the count of DISTINCT
     *         (loop epoch, displayFrame) occurrences ever asked for, by
     *         either the target path or a lookahead path, this session. A
     *         request-EVENT count cannot serve this role: the same source
     *         frame can be re-requested without advancing (drop-frame mode
     *         sets m_frameChanged on every timer tick regardless of whether
     *         the computed position actually crossed a whole-frame boundary
     *         since the previous tick), which inflated this figure with
     *         duplicate events that were never a distinct demand -- and a
     *         lookahead-covers-current reuse attempt
     *         (MainWindow::drawFrame()'s playbackLookaheadCoversCurrent
     *         branch) issues no new request at all, so it is simply never a
     *         member of this set; no separate reuse back-out parameter is
     *         needed any more (round 4's reusedLookaheadTargetFramesThisSession
     *         is gone).
     *  \param presentedViaTargetFramesThisSession CUDA-ATTRIBUTION-BASELINE-1
     *         round 5 (sol BLOCKER, was PARTIAL through round 4): the count
     *         of DISTINCT source-frame occurrences presented this session
     *         whose PresentationContext::playbackLookaheadRequest was
     *         false -- PlaybackPresentedFrameIdentityTracker::
     *         distinctTargetPresentedCount(). Per-origin diagnostic only;
     *         see presentedOccurrenceUnionCount for the figure the loss
     *         computation actually uses.
     *  \param presentedViaLookaheadFramesThisSession the same distinct-count
     *         semantics as presentedViaTargetFramesThisSession above, for
     *         presentations whose PresentationContext::
     *         playbackLookaheadRequest was true.
     *  \param presentedOccurrenceUnionCount CUDA-ATTRIBUTION-BASELINE-1
     *         round 6 (astra BLOCKER, "Summing per-origin unique counts
     *         double-counts overlapping source identities"):
     *         PlaybackPresentedFrameIdentityTracker::
     *         presentedOccurrenceUnionCount() -- the union of the two
     *         per-origin presented sets. A source-frame occurrence
     *         presented via both origins is one fact, not two; summing the
     *         two per-origin counts instead of taking their union let a
     *         session with heavy target/lookahead overlap pass a loss gate
     *         it should have failed.
     *  \param requestedThenSkippedTargetOccurrenceCount
     *         PlaybackPresentedFrameIdentityTracker::
     *         requestedThenSkippedTargetCount() -- occurrences a genuine
     *         target request was issued for that never reached presentation
     *         via either origin, computed as a QSet difference against the
     *         SAME identity a presentation would have used, not by
     *         subtracting mismatched-basis counts.
     *  \param requestedThenDiscardedLookaheadOccurrenceCount the same
     *         set-difference semantics as
     *         requestedThenSkippedTargetOccurrenceCount above, for the
     *         lookahead-requested set.
     *  \param frameTelemetryMeasured CUDA-ATTRIBUTION-BASELINE-1 round 7
     *         (astra major, "the telemetry-off session reports a CONFIDENT
     *         100% loss"): whether MLVAPP_PLAYBACK_SMOKE_TELEMETRY was
     *         active for this session (MainWindow::
     *         m_playbackSmokeFrameTelemetry). When false, every identity
     *         count above is a vacuous zero -- not evidence nothing was
     *         requested/presented, evidence nothing was ever measured.
     *         Defaults to true so every pre-round-7 call site (including
     *         every existing test in this codebase, which models an
     *         actively-measuring session) is unaffected; only
     *         MainWindow::finishPlaybackSmokeTelemetry() need pass the real
     *         value.
     *  \param startOccurrenceOffered CUDA-ATTRIBUTION-BASELINE-1 hub fix (sol
     *         r12 BLOCKER, drop-frame fencepost): 1 iff the session's FIRST
     *         offered occurrence was reached with less than one whole frame
     *         of offered-accumulator advance, else 0 (any nonzero value is
     *         treated as 1). The accumulator counts DISPLACEMENT (elapsed
     *         source-frame distance), but the identity sets count every
     *         occurrence TOUCHED, including the start position itself. In
     *         normal mode playbackHandling() advances a whole frame (+1.0)
     *         before the first drawFrame(), so the start frame is never
     *         requested and no credit is due (0). In drop-frame mode the
     *         first tick advances by < 1 frame, the start frame P IS
     *         requested, and displacement alone reports round(E) while the
     *         requested union is floor(E)+1 -- a healthy run then fails
     *         offeredSourceFrames >= requestedOccurrenceUnionCount about
     *         half the time. MainWindow::drawFrame() decides this per
     *         session (m_playbackSmokeStartOccurrenceOffered); it is never
     *         a blanket +1, which would fabricate a never-requested frame
     *         in every normal-mode run. Ignored when telemetry was not
     *         measured.
     */
    static PlaybackSourceFramePopulation computeSourceFramePopulation(
        double timelineSourceFramesOfferedNow,
        double timelineSourceFramesOfferedStart,
        uint64_t requestedOccurrenceUnionCount,
        uint64_t presentedViaTargetFramesThisSession,
        uint64_t presentedViaLookaheadFramesThisSession,
        uint64_t presentedOccurrenceUnionCount,
        uint64_t requestedThenSkippedTargetOccurrenceCount,
        uint64_t requestedThenDiscardedLookaheadOccurrenceCount,
        bool frameTelemetryMeasured = true,
        uint64_t startOccurrenceOffered = 0 )
    {
        PlaybackSourceFramePopulation result;
        const double rawOffered =
            timelineSourceFramesOfferedNow - timelineSourceFramesOfferedStart;
        /* CUDA-ATTRIBUTION-POLICY-1 (both #155 reviewers): offered must be
         * floor(rawOffered) + startOccurrenceOffered, never
         * round-half-up(rawOffered) + credit. The accumulator counts
         * DISPLACEMENT and the identity sets count every occurrence TOUCHED
         * (see startOccurrenceOffered below) -- the touched-span count is
         * exactly floor(displacement) plus the start-occurrence credit, so
         * rounding the displacement itself up on frac >= 0.5 (e.g. E=24.6
         * rounding to 25) double-counts against a credit that already
         * accounts for the fencepost, fabricating a never-requested frame
         * on an otherwise sound run. +1e-6 guards against a touched span
         * that is genuinely N frames reading back as N-epsilon (e.g.
         * 9.9999999) purely from float accumulation error, without
         * reintroducing the frac>=0.5 over-rounding a plain +0.5 would. */
        result.offeredSourceFrames =
            rawOffered > 0.0
                ? static_cast<uint64_t>( std::floor( rawOffered + 1e-6 ) )
                : 0;
        result.attributionMeasured = frameTelemetryMeasured;

        if( !frameTelemetryMeasured )
        {
            /* Every identity-tracker input is a vacuous zero when telemetry
             * never ran -- report the third state (UNMEASURED) rather than
             * the numeric zeros/100%-loss those inputs would otherwise
             * produce. partitionSound stays false unconditionally: a
             * partition that was never measured cannot be "sound". */
            result.partitionSound = false;
            return result;
        }

        /* Hub fix (sol r12 BLOCKER): credit the drop-frame start occurrence
         * AFTER the displacement rounding above -- see the
         * startOccurrenceOffered parameter note. */
        result.offeredSourceFrames += startOccurrenceOffered != 0 ? 1u : 0u;

        result.presentedViaTargetFrames = presentedViaTargetFramesThisSession;
        result.presentedViaLookaheadFrames = presentedViaLookaheadFramesThisSession;
        result.presentedFrames = presentedOccurrenceUnionCount;
        result.requestedThenSkippedTargetFrames = requestedThenSkippedTargetOccurrenceCount;
        result.requestedThenDiscardedLookaheadFrames =
            requestedThenDiscardedLookaheadOccurrenceCount;

        /* By construction (PlaybackPresentedFrameIdentityTracker's skip/
         * discard counts are each a QSet difference against the presented
         * union), this sum equals requestedOccurrenceUnionCount exactly
         * UNLESS a single occurrence was genuinely requested via BOTH the
         * target and the lookahead path and never presented via either --
         * then it is a member of both difference sets and is counted twice
         * here. That is a real double-attribution the partition must
         * surface as unsound, not silently absorb. */
        const uint64_t accountedForByRequestBuckets =
            result.presentedFrames
            + result.requestedThenDiscardedLookaheadFrames
            + result.requestedThenSkippedTargetFrames;
        const bool requestBucketsSound =
            accountedForByRequestBuckets == requestedOccurrenceUnionCount;

        const bool offeredBucketSound =
            result.offeredSourceFrames >= requestedOccurrenceUnionCount;
        result.neverRequestedSourceFrames =
            offeredBucketSound
                ? result.offeredSourceFrames - requestedOccurrenceUnionCount
                : 0;

        result.partitionSound = offeredBucketSound && requestBucketsSound;
        return result;
    }
};

#endif // PLAYBACKFRAMEPOPULATIONPOLICY_H
