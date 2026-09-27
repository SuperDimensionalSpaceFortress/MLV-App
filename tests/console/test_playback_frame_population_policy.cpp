#include "../common/minitest.h"

#include "../../platform/qt/PlaybackFramePopulationPolicy.h"
#include "../../platform/qt/PlaybackPresentedFrameIdentityTracker.h"

// CUDA-ATTRIBUTION-BASELINE-1 round 2 (astra major 4 / sol major 1): on
// 2bc8cc0a, requested_frames_by_serial (all render requests, including
// speculative lookaheads) was the only wrap-immune population figure, and it
// was used as the validation-gate denominator. With lookahead enabled, most
// of those requests are intentionally discarded before presentation, so
// every one of them read as "skipped" -- inflating loss far above the true
// figure. This test fails on that behavior (the target-only population
// would be indistinguishable from the all-requests population) and passes
// once the target-only counter excludes lookaheads.
TEST(PlaybackFramePopulationPolicy, LookaheadRequestsDoNotInflateTargetOnlySkipCount)
{
    // One target request per presented frame (20 presented), but each target
    // request also issues 2 speculative lookahead requests that are
    // discarded (never presented) -- 20 target + 40 lookahead = 60 total
    // serial advances, matching astra's repro shape ("~60 source frames").
    const uint64_t presentedFrames = 20;
    const uint64_t targetRequests = 20;
    const uint64_t lookaheadRequests = 40;
    const uint64_t allRequests = targetRequests + lookaheadRequests;

    const PlaybackFramePopulation population = PlaybackFramePopulationPolicy::compute(
        /*nextRenderRequestSerial=*/allRequests,
        /*startRequestSerial=*/0,
        /*nextTargetRenderRequestSerial=*/targetRequests,
        /*startTargetRequestSerial=*/0,
        presentedFrames );

    ASSERT_EQ(allRequests, population.requestedFramesBySerial);
    ASSERT_EQ(targetRequests, population.requestedTargetFramesBySerial);
    ASSERT_EQ(lookaheadRequests, population.lookaheadRequestsBySerial);
    // All 20 target requests were presented: nothing was actually skipped.
    ASSERT_EQ(uint64_t(0), population.skippedOrUnpresentedByTargetSerial);
    // The all-requests figure falsely reports 40 "skipped" frames -- every
    // discarded lookahead -- which is exactly the overcount this round
    // fixes. Kept for backward compatibility, not the gate.
    ASSERT_EQ(uint64_t(40), population.skippedOrUnpresentedBySerial);
}

TEST(PlaybackFramePopulationPolicy, NoLookaheadMakesBothPopulationsAgree)
{
    const PlaybackFramePopulation population = PlaybackFramePopulationPolicy::compute(
        /*nextRenderRequestSerial=*/30,
        /*startRequestSerial=*/10,
        /*nextTargetRenderRequestSerial=*/30,
        /*startTargetRequestSerial=*/10,
        /*presentedFrames=*/18 );

    ASSERT_EQ(uint64_t(20), population.requestedFramesBySerial);
    ASSERT_EQ(uint64_t(20), population.requestedTargetFramesBySerial);
    ASSERT_EQ(uint64_t(0), population.lookaheadRequestsBySerial);
    ASSERT_EQ(uint64_t(2), population.skippedOrUnpresentedBySerial);
    ASSERT_EQ(uint64_t(2), population.skippedOrUnpresentedByTargetSerial);
}

TEST(PlaybackFramePopulationPolicy, GenuineSkipsStillCountAgainstTargetPopulation)
{
    // 10 target requests issued, only 6 presented -- 4 genuinely skipped,
    // independent of any lookahead noise.
    const PlaybackFramePopulation population = PlaybackFramePopulationPolicy::compute(
        /*nextRenderRequestSerial=*/50,
        /*startRequestSerial=*/0,
        /*nextTargetRenderRequestSerial=*/10,
        /*startTargetRequestSerial=*/0,
        /*presentedFrames=*/6 );

    ASSERT_EQ(uint64_t(4), population.skippedOrUnpresentedByTargetSerial);
}

TEST(PlaybackFramePopulationPolicy, DeltaNeverGoesNegativeAcrossCounterReset)
{
    ASSERT_EQ(uint64_t(0), PlaybackFramePopulationPolicy::nonNegativeDelta(5, 10));
}

// CUDA-ATTRIBUTION-BASELINE-1 round 6 (sol + astra BLOCKER, round 5
// INVERTED): computeSourceFramePopulation() no longer takes raw
// request-EVENT counters at all -- every "requested"/"presented" argument
// is now a distinct-OCCURRENCE count produced by
// PlaybackPresentedFrameIdentityTracker, fed exactly the way
// MainWindow::drawFrame()/queuePlaybackLookaheadRequests()/
// notePlaybackSmokePresentedFrame() feed it (noteRequestedFrame() at the
// genuine-request call sites, notePresentedFrame() at the presentation call
// site). Building each scenario below through the tracker, rather than
// synthesizing precomputed counts by hand, exercises the SAME call pattern
// production uses, not just the arithmetic in isolation -- see this round's
// producer brief major finding 2.

// CUDA-ATTRIBUTION-BASELINE-1 round 3 (astra major, prior finding 4 NOT
// RESOLVED): astra's own repro -- "advance approximately three source frames
// per target request and present all 20 issued requests across 60
// source-frame advances" -- reproduced verbatim. The gate figures computed
// from PlaybackFramePopulationPolicy::compute() alone (round 2's fix) read
// 0% loss here, because every one of the 20 target requests WAS presented;
// the 40 source frames that were skipped over before ever becoming a request
// are invisible to that struct. computeSourceFramePopulation() must surface
// them: never-requested must be exactly 40, and the buckets must sum back to
// the 60 offered.
TEST(PlaybackFramePopulationPolicy, AstraRepro_SkippedSourceFramesSurfaceAsNeverRequested)
{
    const double offeredNow = 60.0;
    const double offeredStart = 0.0;
    const uint64_t presentedViaTarget = 20;

    // The OLD (round 2) gate figure: every target request was presented, so
    // this reads zero loss -- exactly the false pass astra proved.
    const PlaybackFramePopulation legacyPopulation = PlaybackFramePopulationPolicy::compute(
        /*nextRenderRequestSerial=*/presentedViaTarget,
        /*startRequestSerial=*/0,
        /*nextTargetRenderRequestSerial=*/presentedViaTarget,
        /*startTargetRequestSerial=*/0,
        presentedViaTarget );
    ASSERT_EQ(uint64_t(0), legacyPopulation.skippedOrUnpresentedByTargetSerial);

    // Only 20 of the 60 offered source frames ever became a request at all
    // (frames 0..19); all 20 were presented.
    PlaybackPresentedFrameIdentityTracker identity;
    for (uint64_t f = 0; f < 20; ++f)
    {
        identity.noteRequestedFrame(/*loopEpoch=*/0, f, /*viaLookahead=*/false);
        identity.notePresentedFrame(/*loopEpoch=*/0, f, /*viaLookahead=*/false);
    }

    const PlaybackSourceFramePopulation population =
        PlaybackFramePopulationPolicy::computeSourceFramePopulation(
            offeredNow, offeredStart,
            identity.requestedOccurrenceUnionCount(),
            identity.distinctTargetPresentedCount(),
            identity.distinctLookaheadPresentedCount(),
            identity.presentedOccurrenceUnionCount(),
            identity.requestedThenSkippedTargetCount(),
            identity.requestedThenDiscardedLookaheadCount() );

    ASSERT_EQ(uint64_t(60), population.offeredSourceFrames);
    ASSERT_EQ(uint64_t(40), population.neverRequestedSourceFrames);
    ASSERT_EQ(uint64_t(0), population.requestedThenSkippedTargetFrames);
    ASSERT_EQ(uint64_t(0), population.requestedThenDiscardedLookaheadFrames);
    ASSERT_EQ(uint64_t(20), population.presentedFrames);
    ASSERT_TRUE(population.partitionSound);
    // The four named buckets sum exactly back to the offered population --
    // proven here, not assumed.
    ASSERT_EQ(population.offeredSourceFrames,
              population.neverRequestedSourceFrames
              + population.requestedThenDiscardedLookaheadFrames
              + population.requestedThenSkippedTargetFrames
              + population.presentedFrames);
    // A run losing source frames must FAIL a 50%-loss gate where the old
    // target-serial-only figure passed it cleanly (66.7% > 50%).
    const double lossRatio =
        static_cast<double>( population.offeredSourceFrames - population.presentedFrames )
        / static_cast<double>( population.offeredSourceFrames );
    ASSERT_TRUE(lossRatio > 0.5);
}

TEST(PlaybackFramePopulationPolicy, SourcePopulation_NoLossIsFullyAccountedFor)
{
    // Normal (non-drop-frame) mode: every offered source frame is requested
    // and presented one at a time -- no skip is possible by construction.
    PlaybackPresentedFrameIdentityTracker identity;
    for (uint64_t f = 0; f < 20; ++f)
    {
        identity.noteRequestedFrame(/*loopEpoch=*/0, f, /*viaLookahead=*/false);
        identity.notePresentedFrame(/*loopEpoch=*/0, f, /*viaLookahead=*/false);
    }

    const PlaybackSourceFramePopulation population =
        PlaybackFramePopulationPolicy::computeSourceFramePopulation(
            /*timelineSourceFramesOfferedNow=*/25.0,
            /*timelineSourceFramesOfferedStart=*/5.0,
            identity.requestedOccurrenceUnionCount(),
            identity.distinctTargetPresentedCount(),
            identity.distinctLookaheadPresentedCount(),
            identity.presentedOccurrenceUnionCount(),
            identity.requestedThenSkippedTargetCount(),
            identity.requestedThenDiscardedLookaheadCount() );

    ASSERT_EQ(uint64_t(20), population.offeredSourceFrames);
    ASSERT_EQ(uint64_t(0), population.neverRequestedSourceFrames);
    ASSERT_EQ(uint64_t(0), population.requestedThenSkippedTargetFrames);
    ASSERT_EQ(uint64_t(0), population.requestedThenDiscardedLookaheadFrames);
    ASSERT_EQ(uint64_t(20), population.presentedFrames);
    ASSERT_TRUE(population.partitionSound);
}

TEST(PlaybackFramePopulationPolicy, SourcePopulation_DiscardedLookaheadAndSkippedTargetBothCounted)
{
    // 100 offered source frames; 70 got a genuine target request (frames
    // 0..69) but only 50 of those were presented (0..49; 50..69 genuinely
    // skipped); 20 speculative lookahead requests were issued for a
    // disjoint range (frames 70..89), of which only 5 were presented
    // (70..74; 75..89 discarded); the remaining 10 offered frames (90..99)
    // never got any request at all from either origin.
    PlaybackPresentedFrameIdentityTracker identity;
    for (uint64_t f = 0; f < 70; ++f)
        identity.noteRequestedFrame(/*loopEpoch=*/0, f, /*viaLookahead=*/false);
    for (uint64_t f = 0; f < 50; ++f)
        identity.notePresentedFrame(/*loopEpoch=*/0, f, /*viaLookahead=*/false);
    for (uint64_t f = 70; f < 90; ++f)
        identity.noteRequestedFrame(/*loopEpoch=*/0, f, /*viaLookahead=*/true);
    for (uint64_t f = 70; f < 75; ++f)
        identity.notePresentedFrame(/*loopEpoch=*/0, f, /*viaLookahead=*/true);

    const PlaybackSourceFramePopulation population =
        PlaybackFramePopulationPolicy::computeSourceFramePopulation(
            /*timelineSourceFramesOfferedNow=*/100.0,
            /*timelineSourceFramesOfferedStart=*/0.0,
            identity.requestedOccurrenceUnionCount(),
            identity.distinctTargetPresentedCount(),
            identity.distinctLookaheadPresentedCount(),
            identity.presentedOccurrenceUnionCount(),
            identity.requestedThenSkippedTargetCount(),
            identity.requestedThenDiscardedLookaheadCount() );

    ASSERT_EQ(uint64_t(100), population.offeredSourceFrames);
    ASSERT_EQ(uint64_t(20), population.requestedThenSkippedTargetFrames);
    ASSERT_EQ(uint64_t(15), population.requestedThenDiscardedLookaheadFrames);
    ASSERT_EQ(uint64_t(55), population.presentedFrames);
    ASSERT_EQ(uint64_t(10), population.neverRequestedSourceFrames);
    ASSERT_TRUE(population.partitionSound);
    ASSERT_EQ(population.offeredSourceFrames,
              population.neverRequestedSourceFrames
              + population.requestedThenDiscardedLookaheadFrames
              + population.requestedThenSkippedTargetFrames
              + population.presentedFrames);
}

// CUDA-ATTRIBUTION-BASELINE-1 round 6 (astra major, "Summing per-origin
// unique counts double-counts overlapping source identities and can falsely
// pass the source-loss gate"): 60 offered; 40 target-request events and 20
// lookahead-request events all resolve to the SAME 20 distinct source-frame
// identities, all presented via both origins. Summing the two per-origin
// presented counts (20+20=40) reports 33.3% loss and passes a 50% gate; the
// TRUE loss is 66.7% (only 20 of 60 offered source frames were ever shown,
// regardless of how many origins showed them).
TEST(PlaybackFramePopulationPolicy, SourcePopulation_OverlappingOriginsAreOneFactNotTwo)
{
    PlaybackPresentedFrameIdentityTracker identity;
    // 40 target-request EVENTS, all landing on one of 20 distinct indices.
    for (uint64_t i = 0; i < 40; ++i)
        identity.noteRequestedFrame(/*loopEpoch=*/0, i % 20, /*viaLookahead=*/false);
    for (uint64_t f = 0; f < 20; ++f)
        identity.notePresentedFrame(/*loopEpoch=*/0, f, /*viaLookahead=*/false);
    // 20 lookahead-request events, the same 20 indices, also presented.
    for (uint64_t f = 0; f < 20; ++f)
    {
        identity.noteRequestedFrame(/*loopEpoch=*/0, f, /*viaLookahead=*/true);
        identity.notePresentedFrame(/*loopEpoch=*/0, f, /*viaLookahead=*/true);
    }

    ASSERT_EQ(uint64_t(20), identity.distinctTargetPresentedCount());
    ASSERT_EQ(uint64_t(20), identity.distinctLookaheadPresentedCount());
    // The union, not the sum (40), is the true count of distinct source
    // frames actually shown.
    ASSERT_EQ(uint64_t(20), identity.presentedOccurrenceUnionCount());

    const PlaybackSourceFramePopulation population =
        PlaybackFramePopulationPolicy::computeSourceFramePopulation(
            /*timelineSourceFramesOfferedNow=*/60.0,
            /*timelineSourceFramesOfferedStart=*/0.0,
            identity.requestedOccurrenceUnionCount(),
            identity.distinctTargetPresentedCount(),
            identity.distinctLookaheadPresentedCount(),
            identity.presentedOccurrenceUnionCount(),
            identity.requestedThenSkippedTargetCount(),
            identity.requestedThenDiscardedLookaheadCount() );

    ASSERT_EQ(uint64_t(60), population.offeredSourceFrames);
    ASSERT_EQ(uint64_t(20), population.presentedFrames);
    ASSERT_EQ(uint64_t(40), population.neverRequestedSourceFrames);
    ASSERT_EQ(uint64_t(0), population.requestedThenSkippedTargetFrames);
    ASSERT_EQ(uint64_t(0), population.requestedThenDiscardedLookaheadFrames);
    ASSERT_TRUE(population.partitionSound);
    const double lossRatio =
        static_cast<double>( population.offeredSourceFrames - population.presentedFrames )
        / static_cast<double>( population.offeredSourceFrames );
    // The sum-based computation would have reported 33.3% and passed a 50%
    // gate; the correct union-based figure must fail it.
    ASSERT_TRUE(lossRatio > 0.5);
}

// CUDA-ATTRIBUTION-BASELINE-1 round 6 (astra major, "Request-event
// subtraction still certifies incorrect never-requested versus
// requested-then-skipped attribution"): 60 offered, single lap; 60 target
// REQUEST EVENTS but only 20 distinct source-frame identities are ever the
// subject of one (drop-frame mode's m_frameChanged firing every tick
// regardless of whether the position actually advanced). All 20 distinct
// requested identities are presented. Astra's correct interpretation: 40
// source frames were never requested at all (0 skipped); the pre-round-6
// event-count subtraction instead reported 40 requested-then-skipped and 0
// never-requested -- backwards.
TEST(PlaybackFramePopulationPolicy, AstraRepro_DuplicateRequestEventsAreNeverRequestedNotSkipped)
{
    PlaybackPresentedFrameIdentityTracker identity;
    for (uint64_t i = 0; i < 60; ++i)
    {
        const uint64_t f = i % 20; // only 20 distinct source frames ever asked for
        identity.noteRequestedFrame(/*loopEpoch=*/0, f, /*viaLookahead=*/false);
        identity.notePresentedFrame(/*loopEpoch=*/0, f, /*viaLookahead=*/false);
    }
    ASSERT_EQ(uint64_t(20), identity.requestedOccurrenceUnionCount());

    const PlaybackSourceFramePopulation population =
        PlaybackFramePopulationPolicy::computeSourceFramePopulation(
            /*timelineSourceFramesOfferedNow=*/60.0,
            /*timelineSourceFramesOfferedStart=*/0.0,
            identity.requestedOccurrenceUnionCount(),
            identity.distinctTargetPresentedCount(),
            identity.distinctLookaheadPresentedCount(),
            identity.presentedOccurrenceUnionCount(),
            identity.requestedThenSkippedTargetCount(),
            identity.requestedThenDiscardedLookaheadCount() );

    ASSERT_EQ(uint64_t(60), population.offeredSourceFrames);
    ASSERT_EQ(uint64_t(20), population.presentedFrames);
    // All 20 distinctly-requested identities were presented: nothing was
    // genuinely skipped.
    ASSERT_EQ(uint64_t(0), population.requestedThenSkippedTargetFrames);
    // The other 40 offered source frames were never the subject of any
    // request at all -- not "requested then skipped".
    ASSERT_EQ(uint64_t(40), population.neverRequestedSourceFrames);
    ASSERT_TRUE(population.partitionSound);
}

// CUDA-ATTRIBUTION-BASELINE-1 round 3: if a presentation is ever classified
// against a request class that issued fewer requests than were presented
// from it (an invariant violation elsewhere, not something this policy can
// happen on its own), the clamp must fire and partitionSound must go false
// -- the third-state/UNKNOWN rule -- rather than silently reporting a
// partition that merely sums by construction. Exercised directly against
// the policy (not the tracker) since a normal tracker usage cannot produce
// more presented than requested identities by construction.
TEST(PlaybackFramePopulationPolicy, SourcePopulation_UnsoundInputsAreFlaggedNotHidden)
{
    const PlaybackSourceFramePopulation population =
        PlaybackFramePopulationPolicy::computeSourceFramePopulation(
            /*timelineSourceFramesOfferedNow=*/50.0,
            /*timelineSourceFramesOfferedStart=*/0.0,
            /*requestedOccurrenceUnionCount=*/10,
            /*presentedViaTargetFramesThisSession=*/15, // more presented than requested
            /*presentedViaLookaheadFramesThisSession=*/0,
            /*presentedOccurrenceUnionCount=*/15,
            /*requestedThenSkippedTargetOccurrenceCount=*/0,
            /*requestedThenDiscardedLookaheadOccurrenceCount=*/0 );

    ASSERT_TRUE(!population.partitionSound);
}

// CUDA-ATTRIBUTION-BASELINE-1 round 6 (sol + astra major, new failure mode
// enabled by identity-based request accounting): a single source-frame
// occurrence genuinely requested via BOTH the target and the lookahead path
// and never presented via either is a member of both skip/discard
// difference sets -- counted twice in accountedForByRequestBuckets against
// a requestedOccurrenceUnionCount of only 1. The partition must surface
// this as unsound rather than silently reporting a plausible-looking
// (wrong) never-requested figure.
TEST(PlaybackFramePopulationPolicy, SourcePopulation_DualOriginNeverPresentedOccurrenceIsFlaggedNotHidden)
{
    PlaybackPresentedFrameIdentityTracker identity;
    identity.noteRequestedFrame(/*loopEpoch=*/0, /*displayFrame=*/5, /*viaLookahead=*/false);
    identity.noteRequestedFrame(/*loopEpoch=*/0, /*displayFrame=*/5, /*viaLookahead=*/true);
    // Never presented via either origin.

    ASSERT_EQ(uint64_t(1), identity.requestedOccurrenceUnionCount());
    ASSERT_EQ(uint64_t(1), identity.requestedThenSkippedTargetCount());
    ASSERT_EQ(uint64_t(1), identity.requestedThenDiscardedLookaheadCount());

    const PlaybackSourceFramePopulation population =
        PlaybackFramePopulationPolicy::computeSourceFramePopulation(
            /*timelineSourceFramesOfferedNow=*/30.0,
            /*timelineSourceFramesOfferedStart=*/0.0,
            identity.requestedOccurrenceUnionCount(),
            identity.distinctTargetPresentedCount(),
            identity.distinctLookaheadPresentedCount(),
            identity.presentedOccurrenceUnionCount(),
            identity.requestedThenSkippedTargetCount(),
            identity.requestedThenDiscardedLookaheadCount() );

    ASSERT_TRUE(!population.partitionSound);
}

// CUDA-ATTRIBUTION-BASELINE-1 round 5 (sol BLOCKER) / round 6 (sol + astra
// BLOCKER, round 5 INVERTED): sol's headline scenario, made mechanistically
// faithful under the identity-based request model -- 60 offered source
// frames spanning a single lap, each genuinely and distinctly REQUESTED
// (frames 0..59, one request per source frame, matching offered exactly),
// but the presentation path is stuck showing only frames 0..19 in a cycle
// instead of the frame that was actually requested. Presentation identity
// dedup alone (round 5) is not enough to catch this -- it must be compared
// against REQUEST identity (round 6) to see that frames 20..59 were asked
// for but never actually shown.
TEST(PlaybackFramePopulationPolicy, SolRepro_StaleRepresentationWithinOneLapStillCountsAsLoss)
{
    PlaybackPresentedFrameIdentityTracker identity;
    for (uint64_t f = 0; f < 60; ++f)
    {
        identity.noteRequestedFrame(/*loopEpoch=*/0, f, /*viaLookahead=*/false);
        // Stuck presentation: always shows one of the first 20 frames,
        // never the one actually requested once f reaches 20.
        identity.notePresentedFrame(/*loopEpoch=*/0, f % 20, /*viaLookahead=*/false);
    }
    ASSERT_EQ(uint64_t(60), identity.requestedOccurrenceUnionCount());
    ASSERT_EQ(uint64_t(20), identity.distinctTargetPresentedCount());

    const PlaybackSourceFramePopulation population =
        PlaybackFramePopulationPolicy::computeSourceFramePopulation(
            /*timelineSourceFramesOfferedNow=*/60.0,
            /*timelineSourceFramesOfferedStart=*/0.0,
            identity.requestedOccurrenceUnionCount(),
            identity.distinctTargetPresentedCount(),
            identity.distinctLookaheadPresentedCount(),
            identity.presentedOccurrenceUnionCount(),
            identity.requestedThenSkippedTargetCount(),
            identity.requestedThenDiscardedLookaheadCount() );

    ASSERT_EQ(uint64_t(60), population.offeredSourceFrames);
    // Only 20 distinct source frames actually reached presentation -- the
    // repeated re-presentations of the same 20 are not new information and
    // must not inflate this bucket to 60.
    ASSERT_EQ(uint64_t(20), population.presentedFrames);
    // The other 40 offered source frames: genuinely, distinctly requested
    // (frames 20..59), never actually shown.
    ASSERT_EQ(uint64_t(40), population.requestedThenSkippedTargetFrames);
    ASSERT_EQ(uint64_t(0), population.neverRequestedSourceFrames);
    ASSERT_TRUE(population.partitionSound);
    ASSERT_EQ(population.offeredSourceFrames,
              population.neverRequestedSourceFrames
              + population.requestedThenDiscardedLookaheadFrames
              + population.requestedThenSkippedTargetFrames
              + population.presentedFrames);
    // The true loss -- 66.7% -- is now visible.
    const double lossRatio =
        static_cast<double>(population.offeredSourceFrames - population.presentedFrames)
        / static_cast<double>(population.offeredSourceFrames);
    ASSERT_TRUE(lossRatio > 0.5);
}

// CUDA-ATTRIBUTION-BASELINE-1 round 6 (sol + astra BLOCKER, "round 5
// INVERTED the defect"): sol's wrap repro, made executable -- three
// complete, healthy laps of a 24-frame span. Every one of the 72
// occurrences (24 frames x 3 laps) is genuinely requested and presented.
// Keying identity by raw displayFrame alone (round 5) collapses all three
// laps onto the same 24 identities and reports 66.7% loss on a session that
// lost nothing; keying identity by (loop epoch, displayFrame) instead
// (round 6) keeps all 72 occurrences distinct, because a presentation in
// lap 1 and a presentation in lap 2 are different pairs.
TEST(PlaybackFramePopulationPolicy, SourcePopulation_HealthyRepeatedLapsAreNotLoss)
{
    PlaybackPresentedFrameIdentityTracker identity;
    for (uint64_t lap = 0; lap < 3; ++lap)
    {
        for (uint64_t f = 0; f < 24; ++f)
        {
            identity.noteRequestedFrame(lap, f, /*viaLookahead=*/false);
            identity.notePresentedFrame(lap, f, /*viaLookahead=*/false);
        }
    }
    // All 72 occurrences are distinct identities -- a raw-displayFrame-only
    // tracker would report 24 here instead (sol's exact blocker repro).
    ASSERT_EQ(uint64_t(72), identity.requestedOccurrenceUnionCount());
    ASSERT_EQ(uint64_t(72), identity.distinctTargetPresentedCount());
    ASSERT_EQ(uint64_t(72), identity.presentedOccurrenceUnionCount());

    const double beforeWraps = 1000.0;
    const double afterThreeLapsOf24Frames = beforeWraps + ( 24.0 * 3.0 );

    const PlaybackSourceFramePopulation population =
        PlaybackFramePopulationPolicy::computeSourceFramePopulation(
            afterThreeLapsOf24Frames, beforeWraps,
            identity.requestedOccurrenceUnionCount(),
            identity.distinctTargetPresentedCount(),
            identity.distinctLookaheadPresentedCount(),
            identity.presentedOccurrenceUnionCount(),
            identity.requestedThenSkippedTargetCount(),
            identity.requestedThenDiscardedLookaheadCount() );

    ASSERT_EQ(uint64_t(72), population.offeredSourceFrames);
    ASSERT_EQ(uint64_t(72), population.presentedFrames);
    ASSERT_EQ(uint64_t(0), population.neverRequestedSourceFrames);
    ASSERT_EQ(uint64_t(0), population.requestedThenSkippedTargetFrames);
    ASSERT_TRUE(population.partitionSound);
    const double lossRatio =
        static_cast<double>(population.offeredSourceFrames - population.presentedFrames)
        / static_cast<double>(population.offeredSourceFrames);
    ASSERT_TRUE(lossRatio == 0.0);
}

// A stale re-presentation WITHIN a single lap must still collapse (the
// round-5 fix stays intact): three complete, healthy laps, but lap 1
// re-presents frame 0 twice before advancing (a genuine stuck-frame
// blip within that lap only). That lap's presented set gains no new
// identity from the duplicate, so it reads as one fewer presentation than
// requested for that lap specifically, while the other two laps remain
// fully healthy.
TEST(PlaybackFramePopulationPolicy, SourcePopulation_WithinLapDuplicateStillCollapses)
{
    PlaybackPresentedFrameIdentityTracker identity;
    for (uint64_t lap = 0; lap < 3; ++lap)
    {
        for (uint64_t f = 0; f < 24; ++f)
            identity.noteRequestedFrame(lap, f, /*viaLookahead=*/false);
    }
    for (uint64_t f = 0; f < 24; ++f)
        identity.notePresentedFrame(/*loopEpoch=*/0, f, /*viaLookahead=*/false);
    identity.notePresentedFrame(/*loopEpoch=*/0, /*displayFrame=*/0, /*viaLookahead=*/false); // stale repeat, lap 0
    for (uint64_t f = 0; f < 24; ++f)
        identity.notePresentedFrame(/*loopEpoch=*/1, f, /*viaLookahead=*/false);
    // Lap 2 (epoch 2) never actually presents frame 23 -- one genuine skip.
    for (uint64_t f = 0; f < 23; ++f)
        identity.notePresentedFrame(/*loopEpoch=*/2, f, /*viaLookahead=*/false);

    ASSERT_EQ(uint64_t(72), identity.requestedOccurrenceUnionCount());
    // 24 (lap 0, dedup'd) + 24 (lap 1) + 23 (lap 2) = 71.
    ASSERT_EQ(uint64_t(71), identity.presentedOccurrenceUnionCount());
    ASSERT_EQ(uint64_t(1), identity.requestedThenSkippedTargetCount());
}

// The identity tracker must also keep target- and lookahead-origin
// presentations in separate per-origin sets for diagnostics, even though
// the union (not the sum) is what feeds the loss computation -- the same
// (loop epoch, displayFrame) presented once via each origin is two
// distinct facts about HOW it was delivered, even though it is one fact
// about WHETHER it was shown.
TEST(PlaybackFramePopulationPolicy, IdentityTracker_TargetAndLookaheadOriginsAreCountedIndependently)
{
    PlaybackPresentedFrameIdentityTracker identity;
    identity.notePresentedFrame(/*loopEpoch=*/0, /*displayFrame=*/7, /*viaLookahead=*/false);
    identity.notePresentedFrame(/*loopEpoch=*/0, /*displayFrame=*/7, /*viaLookahead=*/true);
    identity.notePresentedFrame(/*loopEpoch=*/0, /*displayFrame=*/7, /*viaLookahead=*/false); // duplicate, same origin

    ASSERT_EQ(uint64_t(1), identity.distinctTargetPresentedCount());
    ASSERT_EQ(uint64_t(1), identity.distinctLookaheadPresentedCount());
    // But it is one fact about whether frame 7 was shown at all.
    ASSERT_EQ(uint64_t(1), identity.presentedOccurrenceUnionCount());
}

// Two presentations of the same displayFrame in DIFFERENT laps are two
// distinct occurrences -- the round-6 fix, exercised directly on the
// tracker rather than through the policy.
TEST(PlaybackFramePopulationPolicy, IdentityTracker_SameDisplayFrameDifferentLapIsTwoOccurrences)
{
    PlaybackPresentedFrameIdentityTracker identity;
    identity.notePresentedFrame(/*loopEpoch=*/0, /*displayFrame=*/7, /*viaLookahead=*/false);
    identity.notePresentedFrame(/*loopEpoch=*/1, /*displayFrame=*/7, /*viaLookahead=*/false);

    ASSERT_EQ(uint64_t(2), identity.distinctTargetPresentedCount());
}

// reset() must fully clear all four sets (requested/presented x
// target/lookahead) -- a stale entry surviving into the next
// playback-smoke session would misattribute that session's loss.
TEST(PlaybackFramePopulationPolicy, IdentityTracker_ResetClearsEverything)
{
    PlaybackPresentedFrameIdentityTracker identity;
    identity.noteRequestedFrame(/*loopEpoch=*/0, /*displayFrame=*/1, /*viaLookahead=*/false);
    identity.noteRequestedFrame(/*loopEpoch=*/0, /*displayFrame=*/2, /*viaLookahead=*/true);
    identity.notePresentedFrame(/*loopEpoch=*/0, /*displayFrame=*/1, /*viaLookahead=*/false);
    identity.notePresentedFrame(/*loopEpoch=*/0, /*displayFrame=*/2, /*viaLookahead=*/true);
    identity.reset();

    ASSERT_EQ(uint64_t(0), identity.distinctTargetPresentedCount());
    ASSERT_EQ(uint64_t(0), identity.distinctLookaheadPresentedCount());
    ASSERT_EQ(uint64_t(0), identity.requestedOccurrenceUnionCount());
    ASSERT_EQ(uint64_t(0), identity.presentedOccurrenceUnionCount());

    identity.notePresentedFrame(/*loopEpoch=*/0, /*displayFrame=*/1, /*viaLookahead=*/false);
    ASSERT_EQ(uint64_t(1), identity.distinctTargetPresentedCount());
}

// A reused lookahead-covers-current target attempt is now structurally
// excluded (round 6): MainWindow never calls noteRequestedFrame() for it at
// all, rather than recording then subtracting a separate reuse count
// (round 4). Simulated here by simply never issuing the target-side
// noteRequestedFrame() call for the reused identities.
TEST(PlaybackFramePopulationPolicy, SourcePopulation_ReusedLookaheadTargetAttemptsAreNotSkips)
{
    PlaybackPresentedFrameIdentityTracker identity;
    // 20 target attempts, all reuse -- none reach noteRequestedFrame(target).
    // 20 lookahead requests for the same 20 identities, all presented via
    // lookahead (the reused attempts ride on these presentations).
    for (uint64_t f = 0; f < 20; ++f)
    {
        identity.noteRequestedFrame(/*loopEpoch=*/0, f, /*viaLookahead=*/true);
        identity.notePresentedFrame(/*loopEpoch=*/0, f, /*viaLookahead=*/true);
    }

    const PlaybackSourceFramePopulation population =
        PlaybackFramePopulationPolicy::computeSourceFramePopulation(
            /*timelineSourceFramesOfferedNow=*/60.0,
            /*timelineSourceFramesOfferedStart=*/0.0,
            identity.requestedOccurrenceUnionCount(),
            identity.distinctTargetPresentedCount(),
            identity.distinctLookaheadPresentedCount(),
            identity.presentedOccurrenceUnionCount(),
            identity.requestedThenSkippedTargetCount(),
            identity.requestedThenDiscardedLookaheadCount() );

    ASSERT_EQ(uint64_t(60), population.offeredSourceFrames);
    // No genuine target demand was ever recorded, so nothing was skipped on
    // the target side.
    ASSERT_EQ(uint64_t(0), population.requestedThenSkippedTargetFrames);
    ASSERT_EQ(uint64_t(0), population.requestedThenDiscardedLookaheadFrames);
    ASSERT_EQ(uint64_t(20), population.presentedFrames);
    // The 40 source frames nothing ever requested (neither a genuine target
    // attempt nor a lookahead) must surface, not be absorbed into a
    // fictitious skipped-target bucket.
    ASSERT_EQ(uint64_t(40), population.neverRequestedSourceFrames);
    ASSERT_TRUE(population.partitionSound);
    ASSERT_EQ(population.offeredSourceFrames,
              population.neverRequestedSourceFrames
              + population.requestedThenDiscardedLookaheadFrames
              + population.requestedThenSkippedTargetFrames
              + population.presentedFrames);
}

// CUDA-ATTRIBUTION-BASELINE-1 round 7 (astra major, "Lookahead requests
// outside the measured offered window are counted inside the partition"):
// astra's repro verbatim -- offer/request/present targets 3,6,...,60 (20
// targets), each with a depth-1 lookahead one past it (4,7,...,61, 20
// requests, never presented). The lookahead spawned by target 60 asks for
// occurrence 61, which the session's own advancement never reached (the
// ceiling stops at 60) -- it must not be counted as a discarded lookahead.
// astra's correct partition: never_requested=21, discarded_lookahead=19,
// presented=20, skipped_target=0 (61 is simply excluded from every bucket).
TEST(PlaybackFramePopulationPolicy, AstraRepro_LookaheadOutsideOfferedWindowIsExcluded)
{
    PlaybackPresentedFrameIdentityTracker identity;
    for (uint64_t target = 3; target <= 60; target += 3)
    {
        identity.noteOfferedFrame(/*loopEpoch=*/0, target);
        identity.noteRequestedFrame(/*loopEpoch=*/0, target, /*viaLookahead=*/false);
        identity.notePresentedFrame(/*loopEpoch=*/0, target, /*viaLookahead=*/false);
        const uint64_t lookahead = target + 1;
        identity.noteRequestedFrame(/*loopEpoch=*/0, lookahead, /*viaLookahead=*/true);
        // never presented
    }

    ASSERT_EQ(uint64_t(39), identity.requestedOccurrenceUnionCount());
    ASSERT_EQ(uint64_t(19), identity.requestedThenDiscardedLookaheadCount());
    ASSERT_EQ(uint64_t(0), identity.requestedThenSkippedTargetCount());

    const PlaybackSourceFramePopulation population =
        PlaybackFramePopulationPolicy::computeSourceFramePopulation(
            /*timelineSourceFramesOfferedNow=*/60.0,
            /*timelineSourceFramesOfferedStart=*/0.0,
            identity.requestedOccurrenceUnionCount(),
            identity.distinctTargetPresentedCount(),
            identity.distinctLookaheadPresentedCount(),
            identity.presentedOccurrenceUnionCount(),
            identity.requestedThenSkippedTargetCount(),
            identity.requestedThenDiscardedLookaheadCount() );

    ASSERT_EQ(uint64_t(60), population.offeredSourceFrames);
    ASSERT_EQ(uint64_t(20), population.presentedFrames);
    ASSERT_EQ(uint64_t(19), population.requestedThenDiscardedLookaheadFrames);
    ASSERT_EQ(uint64_t(0), population.requestedThenSkippedTargetFrames);
    ASSERT_EQ(uint64_t(21), population.neverRequestedSourceFrames);
    ASSERT_TRUE(population.partitionSound);
    ASSERT_EQ(population.offeredSourceFrames,
              population.neverRequestedSourceFrames
              + population.requestedThenDiscardedLookaheadFrames
              + population.requestedThenSkippedTargetFrames
              + population.presentedFrames);
}

// Direct tracker-level check: a request beyond the current ceiling is
// excluded, and becomes eligible once the ceiling advances to cover it.
TEST(PlaybackFramePopulationPolicy, IdentityTracker_OfferedCeilingExcludesRequestsBeyondIt)
{
    PlaybackPresentedFrameIdentityTracker identity;
    identity.noteOfferedFrame(/*loopEpoch=*/0, /*displayFrame=*/10);
    identity.noteRequestedFrame(/*loopEpoch=*/0, /*displayFrame=*/10, /*viaLookahead=*/false);
    identity.noteRequestedFrame(/*loopEpoch=*/0, /*displayFrame=*/11, /*viaLookahead=*/true);

    ASSERT_EQ(uint64_t(1), identity.requestedOccurrenceUnionCount());
    ASSERT_EQ(uint64_t(0), identity.requestedThenDiscardedLookaheadCount());

    identity.noteOfferedFrame(/*loopEpoch=*/0, /*displayFrame=*/11);

    ASSERT_EQ(uint64_t(2), identity.requestedOccurrenceUnionCount());
    ASSERT_EQ(uint64_t(1), identity.requestedThenDiscardedLookaheadCount());
}

// Without any noteOfferedFrame() call this session (a fixture that predates
// round 7, or a call before drawFrame()'s first invocation), the tracker
// must behave exactly as it did before the ceiling existed -- pass through
// unfiltered rather than guessing an empty window.
TEST(PlaybackFramePopulationPolicy, IdentityTracker_NoOfferedCeilingSetPassesThroughUnfiltered)
{
    PlaybackPresentedFrameIdentityTracker identity;
    identity.noteRequestedFrame(/*loopEpoch=*/0, /*displayFrame=*/100, /*viaLookahead=*/true);
    ASSERT_EQ(uint64_t(1), identity.requestedOccurrenceUnionCount());
}

// reset() must clear the offered ceiling too -- a stale low ceiling from the
// previous session must not survive to filter the next session's
// identities.
TEST(PlaybackFramePopulationPolicy, IdentityTracker_ResetClearsOfferedCeiling)
{
    PlaybackPresentedFrameIdentityTracker identity;
    identity.noteOfferedFrame(/*loopEpoch=*/0, /*displayFrame=*/5);
    identity.reset();
    identity.noteRequestedFrame(/*loopEpoch=*/0, /*displayFrame=*/500, /*viaLookahead=*/true);
    ASSERT_EQ(uint64_t(1), identity.requestedOccurrenceUnionCount());
}

// CUDA-ATTRIBUTION-BASELINE-1 round 7 (astra major, "the telemetry-off
// session reports a CONFIDENT 100% loss"): with frameTelemetryMeasured=false
// (MLVAPP_PLAYBACK_SMOKE_TELEMETRY off), every identity-tracker input is a
// vacuous zero -- the pre-round-7 arithmetic would report presented=0,
// never_requested=offered, partition_sound=true and loss=1.0, exactly
// astra's "confident 100% loss" defect. The unmeasured session must instead
// report attributionMeasured=false and partitionSound=false
// unconditionally, and every bucket except offeredSourceFrames must be 0 --
// UNMEASURED, not 100% loss.
TEST(PlaybackFramePopulationPolicy, AstraRepro_TelemetryOffReportsUnmeasuredNotTotalLoss)
{
    // No identity tracker insertions at all -- exactly what an unmeasured
    // session produces (every MainWindow insertion call site is gated on
    // m_playbackSmokeFrameTelemetry).
    PlaybackPresentedFrameIdentityTracker identity;

    const PlaybackSourceFramePopulation population =
        PlaybackFramePopulationPolicy::computeSourceFramePopulation(
            /*timelineSourceFramesOfferedNow=*/60.0,
            /*timelineSourceFramesOfferedStart=*/0.0,
            identity.requestedOccurrenceUnionCount(),
            identity.distinctTargetPresentedCount(),
            identity.distinctLookaheadPresentedCount(),
            identity.presentedOccurrenceUnionCount(),
            identity.requestedThenSkippedTargetCount(),
            identity.requestedThenDiscardedLookaheadCount(),
            /*frameTelemetryMeasured=*/false );

    ASSERT_TRUE(!population.attributionMeasured);
    ASSERT_TRUE(!population.partitionSound);
    // offeredSourceFrames is real, telemetry-independent data -- it is
    // reported even when unmeasured (only the identity-derived buckets are
    // meaningless).
    ASSERT_EQ(uint64_t(60), population.offeredSourceFrames);
    ASSERT_EQ(uint64_t(0), population.neverRequestedSourceFrames);
    ASSERT_EQ(uint64_t(0), population.requestedThenDiscardedLookaheadFrames);
    ASSERT_EQ(uint64_t(0), population.requestedThenSkippedTargetFrames);
    ASSERT_EQ(uint64_t(0), population.presentedFrames);
}

// The default parameter value (frameTelemetryMeasured=true) keeps every
// pre-round-7 call site -- including every other test in this file --
// behaving exactly as before.
TEST(PlaybackFramePopulationPolicy, FrameTelemetryMeasuredDefaultsToTrue)
{
    PlaybackPresentedFrameIdentityTracker identity;
    for (uint64_t f = 0; f < 20; ++f)
    {
        identity.noteRequestedFrame(/*loopEpoch=*/0, f, /*viaLookahead=*/false);
        identity.notePresentedFrame(/*loopEpoch=*/0, f, /*viaLookahead=*/false);
    }

    const PlaybackSourceFramePopulation population =
        PlaybackFramePopulationPolicy::computeSourceFramePopulation(
            /*timelineSourceFramesOfferedNow=*/20.0,
            /*timelineSourceFramesOfferedStart=*/0.0,
            identity.requestedOccurrenceUnionCount(),
            identity.distinctTargetPresentedCount(),
            identity.distinctLookaheadPresentedCount(),
            identity.presentedOccurrenceUnionCount(),
            identity.requestedThenSkippedTargetCount(),
            identity.requestedThenDiscardedLookaheadCount() );

    ASSERT_TRUE(population.attributionMeasured);
    ASSERT_TRUE(population.partitionSound);
    ASSERT_EQ(uint64_t(20), population.presentedFrames);
}

// CUDA-ATTRIBUTION-BASELINE-1 hub fix (sol r12 BLOCKER, drop-frame
// fencepost). offeredSourceFrames counts DISPLACEMENT (elapsed source-frame
// distance), the identity sets count every occurrence TOUCHED. Drop-frame
// mode's first tick advances < 1 frame, so the start frame p is requested
// and presented: occurrences p..p+k (k+1 of them) against a raw displacement
// of k+fraction, which rounds to k -- one short of the requested union. The
// start occurrence is credited only when MainWindow saw it (credit 1);
// normal mode advances +1.0 before its first drawFrame(), never requests p,
// and gets no credit.
static PlaybackSourceFramePopulation fencepostPopulation( uint64_t firstFrame,
                                                          uint64_t lastFrame,
                                                          double rawOffered,
                                                          uint64_t startOccurrenceOffered )
{
    PlaybackPresentedFrameIdentityTracker identity;
    for (uint64_t f = firstFrame; f <= lastFrame; ++f)
    {
        identity.noteOfferedFrame(/*loopEpoch=*/0, f);
        identity.noteRequestedFrame(/*loopEpoch=*/0, f, /*viaLookahead=*/false);
        identity.notePresentedFrame(/*loopEpoch=*/0, f, /*viaLookahead=*/false);
    }
    return PlaybackFramePopulationPolicy::computeSourceFramePopulation(
        /*timelineSourceFramesOfferedNow=*/rawOffered,
        /*timelineSourceFramesOfferedStart=*/0.0,
        identity.requestedOccurrenceUnionCount(),
        identity.distinctTargetPresentedCount(),
        identity.distinctLookaheadPresentedCount(),
        identity.presentedOccurrenceUnionCount(),
        identity.requestedThenSkippedTargetCount(),
        identity.requestedThenDiscardedLookaheadCount(),
        /*frameTelemetryMeasured=*/true,
        startOccurrenceOffered );
}

// (i) drop-frame: (0, 10..34) requested+presented, raw offered 24.3, start
// occurrence credited -> 25 offered, sound, nothing never-requested.
TEST(PlaybackFramePopulationPolicy, SolR12_DropFrameStartOccurrenceCreditedIsSound)
{
    const PlaybackSourceFramePopulation population =
        fencepostPopulation( 10, 34, 24.3, /*startOccurrenceOffered=*/1 );
    ASSERT_EQ(uint64_t(25), population.offeredSourceFrames);
    ASSERT_EQ(uint64_t(25), population.presentedFrames);
    ASSERT_EQ(uint64_t(0), population.neverRequestedSourceFrames);
    ASSERT_TRUE(population.partitionSound);
}

// (ii) negative control: the SAME healthy drop-frame run without the credit
// is exactly sol's r12 BLOCKER -- 24 offered < 25 requested, unsound.
TEST(PlaybackFramePopulationPolicy, SolR12_DropFrameStartOccurrenceUncreditedIsUnsound)
{
    const PlaybackSourceFramePopulation population =
        fencepostPopulation( 10, 34, 24.3, /*startOccurrenceOffered=*/0 );
    ASSERT_EQ(uint64_t(24), population.offeredSourceFrames);
    ASSERT_EQ(uint64_t(25), population.presentedFrames);
    ASSERT_TRUE(!population.partitionSound);
}

// (iii) normal mode: (0, 11..34) requested+presented, offered exactly 24,
// no credit -> sound, nothing never-requested (a blanket +1 would fabricate
// one never-requested frame here).
TEST(PlaybackFramePopulationPolicy, SolR12_NormalModeNoStartCreditIsSound)
{
    const PlaybackSourceFramePopulation population =
        fencepostPopulation( 11, 34, 24.0, /*startOccurrenceOffered=*/0 );
    ASSERT_EQ(uint64_t(24), population.offeredSourceFrames);
    ASSERT_EQ(uint64_t(24), population.presentedFrames);
    ASSERT_EQ(uint64_t(0), population.neverRequestedSourceFrames);
    ASSERT_TRUE(population.partitionSound);
}

// Same successful-reuse scenario, but with offered=20 instead of 60 --
// must resolve consistently regardless of how much of the clip's total
// span the reused identities represent.
TEST(PlaybackFramePopulationPolicy, SourcePopulation_ReusedLookaheadTargetAttemptsStaySoundAtSmallerOfferedCount)
{
    PlaybackPresentedFrameIdentityTracker identity;
    for (uint64_t f = 0; f < 20; ++f)
    {
        identity.noteRequestedFrame(/*loopEpoch=*/0, f, /*viaLookahead=*/true);
        identity.notePresentedFrame(/*loopEpoch=*/0, f, /*viaLookahead=*/true);
    }

    const PlaybackSourceFramePopulation population =
        PlaybackFramePopulationPolicy::computeSourceFramePopulation(
            /*timelineSourceFramesOfferedNow=*/20.0,
            /*timelineSourceFramesOfferedStart=*/0.0,
            identity.requestedOccurrenceUnionCount(),
            identity.distinctTargetPresentedCount(),
            identity.distinctLookaheadPresentedCount(),
            identity.presentedOccurrenceUnionCount(),
            identity.requestedThenSkippedTargetCount(),
            identity.requestedThenDiscardedLookaheadCount() );

    ASSERT_EQ(uint64_t(20), population.offeredSourceFrames);
    ASSERT_EQ(uint64_t(0), population.requestedThenSkippedTargetFrames);
    ASSERT_EQ(uint64_t(0), population.requestedThenDiscardedLookaheadFrames);
    ASSERT_EQ(uint64_t(20), population.presentedFrames);
    ASSERT_EQ(uint64_t(0), population.neverRequestedSourceFrames);
    ASSERT_TRUE(population.partitionSound);
}

// CUDA-ATTRIBUTION-POLICY-1 (both #155 reviewers, fable's repro): offered
// must be floor(rawOffered) + startOccurrenceOffered, never
// round-half-up(rawOffered) + credit. 25 frames touched (10..34), raw
// offered 24.6 (frac 0.6 >= 0.5). Under the pre-fix round-half-up rule this
// reads cast(24.6+0.5)=25, then +1 credit=26 -- one MORE than the 25-frame
// touched span, fabricating a never-requested frame on a run that lost
// nothing. floor(24.6+1e-6)+1 = 24+1 = 25, matching the touched span
// exactly. FAILS under the pre-fix rule: it reports offered=26 and
// neverRequestedSourceFrames=1 instead of 25 and 0.
TEST(PlaybackFramePopulationPolicy, CUDA_POLICY1_FableRepro_FloorNotRoundHalfUpMatchesTouchedSpan)
{
    const PlaybackSourceFramePopulation population =
        fencepostPopulation( 10, 34, 24.6, /*startOccurrenceOffered=*/1 );
    ASSERT_EQ(uint64_t(25), population.offeredSourceFrames);
    ASSERT_EQ(uint64_t(25), population.presentedFrames);
    ASSERT_EQ(uint64_t(0), population.neverRequestedSourceFrames);
    ASSERT_TRUE(population.partitionSound);
}

// A drop-frame case where frac(rawOffered) < 0.5. Non-regression control:
// for frac < 0.5, cast(rawOffered+0.5) already equals floor(rawOffered) (the
// +0.5 truncation never crosses the next integer), so the pre-fix rule
// already agreed with floor() here and this case does NOT distinguish the
// two rules -- it exists to prove the fix does not disturb the sub-0.5
// fencepost case the pre-fix code already got right, alongside the
// frac >= 0.5 repro above which it got wrong. 30 frames touched (200..229),
// raw offered 29.4 (frac 0.4), start-occurrence credited.
TEST(PlaybackFramePopulationPolicy, CUDA_POLICY1_FracBelowHalfDropFrameStaysSound)
{
    const PlaybackSourceFramePopulation population =
        fencepostPopulation( 200, 229, 29.4, /*startOccurrenceOffered=*/1 );
    ASSERT_EQ(uint64_t(30), population.offeredSourceFrames);
    ASSERT_EQ(uint64_t(30), population.presentedFrames);
    ASSERT_EQ(uint64_t(0), population.neverRequestedSourceFrames);
    ASSERT_TRUE(population.partitionSound);
}

// Normal (non-drop-frame) mode: integral rawOffered, no start-occurrence
// credit -- the majority-case call pattern (MainWindow only ever passes
// startOccurrenceOffered=1 in drop-frame mode). Asserts nothing changes:
// floor(30.0+1e-6)=30 and cast(30.0+0.5)=30 agree, exactly as they did
// before this fix. 30 frames touched (500..529), raw offered exactly 30.0.
TEST(PlaybackFramePopulationPolicy, CUDA_POLICY1_NormalModeIntegralOfferedUnaffectedByFix)
{
    const PlaybackSourceFramePopulation population =
        fencepostPopulation( 500, 529, 30.0, /*startOccurrenceOffered=*/0 );
    ASSERT_EQ(uint64_t(30), population.offeredSourceFrames);
    ASSERT_EQ(uint64_t(30), population.presentedFrames);
    ASSERT_EQ(uint64_t(0), population.neverRequestedSourceFrames);
    ASSERT_TRUE(population.partitionSound);
}

// Float-integrality guard: rawOffered=9.9999999 is float accumulation drift
// representing a "true" displacement of exactly 10.0 (10 frames touched,
// 0..9, no drop-frame credit). A bare std::floor(rawOffered) with no epsilon
// would read 9 -- one short of the 10-frame touched span -- and report the
// run as unsound (offeredBucketSound false) even though nothing was lost.
// std::floor(rawOffered + 1e-6) reads 10, matching exactly. (The pre-fix
// round-half-up rule also happened to read 10 here, via its own +0.5 pad --
// this test guards the floor()-without-epsilon mistake specifically, which
// the "Guard float integrality" requirement calls out by name.)
TEST(PlaybackFramePopulationPolicy, CUDA_POLICY1_FloatDriftBelowIntegralOfferedIsNotUndercounted)
{
    const PlaybackSourceFramePopulation population =
        fencepostPopulation( 0, 9, 9.9999999, /*startOccurrenceOffered=*/0 );
    ASSERT_EQ(uint64_t(10), population.offeredSourceFrames);
    ASSERT_EQ(uint64_t(10), population.presentedFrames);
    ASSERT_EQ(uint64_t(0), population.neverRequestedSourceFrames);
    ASSERT_TRUE(population.partitionSound);
}
