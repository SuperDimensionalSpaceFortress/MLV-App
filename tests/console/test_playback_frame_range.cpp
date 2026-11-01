#include "../common/minitest.h"
#include "../../platform/qt/PlaybackFrameRange.h"

#include <limits>
#include <string>
#include <vector>

TEST( PlaybackFrameRange, ZeroCutRangeRepairsToWholeClip )
{
    const playback_frame_range::CutRange range =
        playback_frame_range::normalizeCutRange( 0, 0, 60 );

    ASSERT_TRUE( range.valid );
    ASSERT_TRUE( range.changed );
    ASSERT_EQ( 1, range.cutIn );
    ASSERT_EQ( 60, range.cutOut );
    ASSERT_EQ( 0, playback_frame_range::firstFrameIndex( range ) );
    ASSERT_EQ( 59, playback_frame_range::lastFrameIndex( range ) );
}

TEST( PlaybackFrameRange, CutInAboveClipClampsToLastFrame )
{
    const playback_frame_range::CutRange range =
        playback_frame_range::normalizeCutRange( 75, 10, 60 );

    ASSERT_TRUE( range.valid );
    ASSERT_TRUE( range.changed );
    ASSERT_EQ( 60, range.cutIn );
    ASSERT_EQ( 60, range.cutOut );
    ASSERT_EQ( 59, playback_frame_range::firstFrameIndex( range ) );
    ASSERT_EQ( 59, playback_frame_range::lastFrameIndex( range ) );
}

TEST( PlaybackFrameRange, CollapsedSingleFrameRangeStaysCollapsedByDefault )
{
    const playback_frame_range::CutRange range =
        playback_frame_range::normalizeCutRange( 1, 1, 60 );

    ASSERT_TRUE( range.valid );
    ASSERT_FALSE( range.changed );
    ASSERT_EQ( 1, range.cutIn );
    ASSERT_EQ( 1, range.cutOut );
}

TEST( PlaybackFrameRange, PlayPathRepairsCollapsedSingleFrameRange )
{
    const playback_frame_range::CutRange range =
        playback_frame_range::normalizeCutRange( 1, 1, 60, true );

    ASSERT_TRUE( range.valid );
    ASSERT_TRUE( range.changed );
    ASSERT_EQ( 1, range.cutIn );
    ASSERT_EQ( 60, range.cutOut );
    ASSERT_EQ( 0, playback_frame_range::firstFrameIndex( range ) );
    ASSERT_EQ( 59, playback_frame_range::lastFrameIndex( range ) );
}

TEST( PlaybackFrameRange, PlayPathLeavesLastFrameCollapseAtClipEndAlone )
{
    const playback_frame_range::CutRange range =
        playback_frame_range::normalizeCutRange( 60, 60, 60, true );

    ASSERT_TRUE( range.valid );
    ASSERT_FALSE( range.changed );
    ASSERT_EQ( 60, range.cutIn );
    ASSERT_EQ( 60, range.cutOut );
}

TEST( PlaybackFrameRange, NegativeRequestedFrameClampsBeforeUnsignedRenderRequest )
{
    bool changed = false;
    const int frame =
        playback_frame_range::clampFrameIndex( -1, 60, &changed );

    ASSERT_TRUE( changed );
    ASSERT_EQ( 0, frame );
}

TEST( PlaybackFrameRange, PastEndRequestedFrameClampsBeforeRenderRequest )
{
    bool changed = false;
    const int frame =
        playback_frame_range::clampFrameIndex( 429496, 60, &changed );

    ASSERT_TRUE( changed );
    ASSERT_EQ( 59, frame );
}

TEST( PlaybackFrameRange, UnsignedSentinelIsRejectedAtRenderBoundary )
{
    ASSERT_FALSE( playback_frame_range::isValidFrameNumber( 0xFFFFFFFFu, 60 ) );
    ASSERT_FALSE( playback_frame_range::isValidFrameNumber( 60u, 60 ) );
    ASSERT_TRUE( playback_frame_range::isValidFrameNumber( 59u, 60 ) );
}

// --- CUDA-PLAYBACK-CONTACT-SHEET-2 round 2 -----------------------------------------------

TEST( PlaybackFrameRange, DropFrameModeWithLoopNeverPresentsTheLastFrameOfARange )
{
    // BLOCKER repro: a ~1.7-frame-per-tick drop step, Loop on, over cut range [cutIn=1,
    // cutOut=61] (0-based last frame 60). advanceDropFrameTick's wrap check fires on the
    // position a tick is ABOUT to reach and subtracts before that position is ever returned,
    // so the range's last frame can never come out of this function with loop enabled -- which
    // is exactly why the contact-sheet capture pass's final target (sheetEndFrame == cutOut-1
    // on a wrapped run) was never satisfied before this round's fix.
    const int cutInValue = 1;
    const int cutOutValue = 61;
    const int lastFrame = cutOutValue - 1;
    double position = 0.0;
    bool sawLastFrame = false;
    for( int tick = 0; tick < 2000; ++tick )
    {
        const playback_frame_range::DropFrameTickResult result =
            playback_frame_range::advanceDropFrameTick(
                position, 1.7, cutInValue, cutOutValue, true );
        position = result.position;
        if( static_cast<int>( position ) == lastFrame ) sawLastFrame = true;
    }
    ASSERT_FALSE( sawLastFrame );
}

TEST( PlaybackFrameRange, DropFrameModeWithoutLoopClampsExactlyToTheLastFrame )
{
    // Sanity check for advanceDropFrameTick's non-loop branch, which the pre-round-2 code
    // already relied on (Loop off + drop-frame on does present cutOut-1 via the clamp) -- the
    // extraction must not change this existing, already-correct behaviour.
    const int cutInValue = 1;
    const int cutOutValue = 61;
    double position = 58.4;
    const playback_frame_range::DropFrameTickResult result =
        playback_frame_range::advanceDropFrameTick( position, 1.7, cutInValue, cutOutValue, false );
    ASSERT_FALSE( result.wrapped );
    ASSERT_EQ( 60, static_cast<int>( result.position ) );
}

namespace
{
// Mirrors MainWindow::noteContactSheetPresentedFrame's target-satisfaction rule: targets are
// ascending, and within one mode's monotonic run the timeline only moves forward, so the first
// presented frame at or past the next target satisfies it, in order.
int countTargetsSatisfiedInOrder(
    const std::vector<int> & targets, const std::vector<int> & presentedFrames )
{
    size_t nextTarget = 0;
    for( int frame : presentedFrames )
    {
        while( nextTarget < targets.size() && frame >= targets[nextTarget] )
        {
            ++nextTarget;
        }
        if( nextTarget >= targets.size() ) break;
    }
    return static_cast<int>( nextTarget );
}
} // namespace

// CONTACT-SHEET-PLAYBACK-PARITY-1: the in-pass sheet grabs on the MEASURED Play, at the first
// presented frame past each target time. Targets are the centres of N equal slices of the play
// window, so every one lies strictly inside it whatever fps the leg reaches (a frame-number target
// past the last frame a slow leg reaches would never fire, and there is no replay to catch it).
TEST( PlaybackFrameRange, ContactSheetInPassTargetsAreSliceCentresStrictlyInsideTheWindow )
{
    const long long windowMs = 25000;
    long long previous = 0;
    for( int i = 0; i < 6; ++i )
    {
        const long long target = playback_frame_range::contactSheetInPassTargetMs( i, 6, windowMs );
        ASSERT_TRUE( target > previous );
        ASSERT_TRUE( target < windowMs );
        previous = target;
    }
    ASSERT_EQ( static_cast<long long>( 2083 ), playback_frame_range::contactSheetInPassTargetMs( 0, 6, windowMs ) );
    ASSERT_EQ( static_cast<long long>( 22917 ), playback_frame_range::contactSheetInPassTargetMs( 5, 6, windowMs ) );
    ASSERT_EQ( static_cast<long long>( 12500 ), playback_frame_range::contactSheetInPassTargetMs( 0, 1, windowMs ) );
    ASSERT_EQ( static_cast<long long>( 0 ), playback_frame_range::contactSheetInPassTargetMs( 0, 0, windowMs ) );
}

namespace
{
// One measured Play as runGuiPlaybackSmoke runs it: frame k is presented at k*1000/fps ms with k+1 source frames
// consumed; the present hook (noteContactSheetPresentedFrame) takes each due target, then the wait loop asks
// evaluatePlayStop and stops on Reached. Returns the in-pass targets captured before the stop.
int simulateInPassCaptures( bool unpaced, double fps, int frameCount, double playSeconds, double nativeFps )
{
    const int64_t required = playback_frame_range::requiredSourceFrames( nativeFps, playSeconds );
    const long long windowMs = std::llround( playSeconds * 1000.0 );
    const int64_t safetyMs = playback_frame_range::playSafetyMs( playSeconds );
    int next = 0;
    for( int64_t k = 0; ; ++k )
    {
        const int64_t elapsedMs = static_cast<int64_t>( std::llround( static_cast<double>( k ) * 1000.0 / fps ) );
        const int64_t consumed = k + 1;
        // The hook takes at most one target per present.
        if( next < frameCount
         && playback_frame_range::contactSheetInPassTargetDue(
                unpaced, elapsedMs, playback_frame_range::contactSheetInPassTargetMs( next, frameCount, windowMs ),
                consumed, playback_frame_range::contactSheetInPassTargetSourceFrames( next, frameCount, required ) ) )
        {
            ++next;
        }
        const playback_frame_range::PlayStopState state = playback_frame_range::evaluatePlayStop(
            consumed, required, true, elapsedMs, safetyMs, playback_frame_range::PlayPaceMode::MeasuredInformational );
        if( state != playback_frame_range::PlayStopState::Continue ) return next;
    }
}
} // namespace

// VENUE-LEG-AGXOFF-UNPACED-HEADROOM-1 r2 (sol B1 counterexample): the shipped unpaced look leg
// (m16-1243-look-scale2-cinematic-agxoff-unpaced.json: playSeconds 20, contactSheetFrames 1) on a 24 fps clip
// needs 480 source frames. Unpaced at 60 fps it stops at 8000 ms; its one wall-clock target is 10000 ms, so zero of
// one were captured and the healthy faster run exited 14. Declared ceiling: every fps up to 120 must capture all of
// its targets (and below the paced rate too); frameCount 1 and 6 both.
TEST( PlaybackFrameRange, UnpacedInPassTargetsAreReachedBeforeTheSourceFrameStopAtAnyFpsUpTo120 )
{
    ASSERT_EQ( static_cast<int64_t>( 480 ), playback_frame_range::requiredSourceFrames( 24.0, 20.0 ) );
    ASSERT_EQ( static_cast<int64_t>( 240 ), playback_frame_range::contactSheetInPassTargetSourceFrames( 0, 1, 480 ) );
    ASSERT_EQ( 1, simulateInPassCaptures( true, 60.0, 1, 20.0, 24.0 ) );
    for( int fps = 12; fps <= 120; ++fps )
    {
        ASSERT_EQ( 1, simulateInPassCaptures( true, static_cast<double>( fps ), 1, 20.0, 24.0 ) );
        ASSERT_EQ( 6, simulateInPassCaptures( true, static_cast<double>( fps ), 6, 20.0, 24.0 ) );
    }
    // Every unpaced target is strictly inside the required frames, ascending.
    int64_t previous = 0;
    for( int i = 0; i < 6; ++i )
    {
        const int64_t target = playback_frame_range::contactSheetInPassTargetSourceFrames( i, 6, 480 );
        ASSERT_TRUE( target > previous );
        ASSERT_TRUE( target < 480 );
        previous = target;
    }
    ASSERT_EQ( static_cast<int64_t>( 0 ), playback_frame_range::contactSheetInPassTargetSourceFrames( 0, 0, 480 ) );
    ASSERT_EQ( static_cast<int64_t>( 0 ), playback_frame_range::contactSheetInPassTargetSourceFrames( 0, 1, 0 ) );
    // Never due on an unknown requirement, whatever the wall clock says.
    ASSERT_FALSE( playback_frame_range::contactSheetInPassTargetDue( true, 999999, 10000, 999, 0 ) );
}

// The paced rule is unchanged: a PACED Play that stops before a wall-clock target leaves that target uncaptured, so
// the sheet stays short and runGuiPlaybackSmoke still exits 14 ("only %1 of %2 in-pass targets" in MainWindow.cpp,
// pinned by test_contact_sheet_capture_wiring.cpp). Source frames never make a paced target due.
TEST( PlaybackFrameRange, PacedInPassTargetsStayOnTheWallClockAndAMissedOneStaysMissed )
{
    ASSERT_EQ( 0, simulateInPassCaptures( false, 60.0, 1, 20.0, 24.0 ) );
    ASSERT_EQ( 1, simulateInPassCaptures( false, 24.0, 1, 20.0, 24.0 ) );
    ASSERT_FALSE( playback_frame_range::contactSheetInPassTargetDue( false, 8000, 10000, 480, 240 ) );
    ASSERT_TRUE( playback_frame_range::contactSheetInPassTargetDue( false, 10000, 10000, 1, 240 ) );
}

// MERGE-MASTER r3 (adversary A3 input): M16-1457, 1499 required source frames at 59.94 fps conformed to 24 fps
// (62.4 s of wall clock), six in-pass frames. The targets are the source-window centres 125 .. 1374, not the
// wall-clock centres of a 25 s window (~source frames 50 .. 550). Paced native keeps the wall clock; unpaced is unchanged.
TEST( PlaybackFrameRange, ConformedInPassTargetsAreSourceFrameCentresOfTheWholeSourceWindow )
{
    ASSERT_TRUE( playback_frame_range::contactSheetInPassTargetsAreSourceFrames( false, true, 1499 ) );
    ASSERT_FALSE( playback_frame_range::contactSheetInPassTargetsAreSourceFrames( false, false, 1499 ) );
    ASSERT_TRUE( playback_frame_range::contactSheetInPassTargetsAreSourceFrames( true, false, 1499 ) );
    ASSERT_TRUE( playback_frame_range::contactSheetInPassTargetsAreSourceFrames( true, false, 0 ) );
    ASSERT_FALSE( playback_frame_range::contactSheetInPassTargetsAreSourceFrames( false, true, 0 ) );
    const int64_t expected[] = { 125, 375, 625, 874, 1124, 1374 };
    for( int i = 0; i < 6; ++i )
        ASSERT_EQ( expected[i], playback_frame_range::contactSheetInPassTargetSourceFrames( i, 6, 1499 ) );
    // Due on source frames consumed, whatever the wall clock: the last target is reached after 57 s of a 62 s Play.
    ASSERT_FALSE( playback_frame_range::contactSheetInPassTargetDue( true, 22917, 22917, 551, 1374 ) );
    ASSERT_TRUE( playback_frame_range::contactSheetInPassTargetDue( true, 57250, 22917, 1374, 1374 ) );
}

// The grabs run on the GUI thread inside the measured interval, so the honest figure next to the
// leg's fps is the fps with their recorded cost taken out of the elapsed time. It can only be the
// same or higher, and it equals the measured fps when nothing was grabbed.
TEST( PlaybackFrameRange, FpsExcludingGrabCostRemovesOnlyTheRecordedGrabTime )
{
    ASSERT_NEAR( 24.0, playback_frame_range::fpsExcludingGrabCost( 600, 25000.0, 0.0 ), 1e-9 );
    ASSERT_NEAR( 600.0 / 24.88, playback_frame_range::fpsExcludingGrabCost( 600, 25000.0, 120.0 ), 1e-9 );
    ASSERT_TRUE( playback_frame_range::fpsExcludingGrabCost( 600, 25000.0, 120.0 )
                 > playback_frame_range::fpsExcludingGrabCost( 600, 25000.0, 0.0 ) );
    // A grab total that swallows the whole interval is a broken record, not an infinite fps.
    ASSERT_NEAR( 0.0, playback_frame_range::fpsExcludingGrabCost( 600, 25000.0, 25000.0 ), 1e-9 );
    ASSERT_NEAR( 0.0, playback_frame_range::fpsExcludingGrabCost( 600, 0.0, 0.0 ), 1e-9 );
}

TEST( PlaybackFrameRange, ContactSheetTargetFrameLastTargetEqualsTheSpanEnd )
{
    const int sheetStartFrame = 0;
    const int sheetEndFrame = 60;
    ASSERT_EQ( sheetStartFrame, playback_frame_range::contactSheetTargetFrame(
        0, 8, sheetStartFrame, sheetEndFrame ) );
    ASSERT_EQ( sheetEndFrame, playback_frame_range::contactSheetTargetFrame(
        7, 8, sheetStartFrame, sheetEndFrame ) );
}

TEST( PlaybackFrameRange,
      ContactSheetTargetsOnAWrappedSpanAreFullyReachedOnlyWithDropFrameModeForcedOff )
{
    // Full span/target reproduction (CUDA-PLAYBACK-CONTACT-SHEET-2 round 2 BLOCKER): a wrapped
    // run's capture span is cutIn-1..cutOut-1 (MainWindow.cpp's wrapped-span override), and its
    // targets are distributed across that span via contactSheetTargetFrame, so the last target
    // always equals the span end.
    const int cutInValue = 1;
    const int cutOutValue = 61;
    const int sheetStartFrame = cutInValue - 1; // 0
    const int sheetEndFrame = cutOutValue - 1;  // 60
    const int contactSheetFrames = 8;

    std::vector<int> targets;
    for( int i = 0; i < contactSheetFrames; ++i )
    {
        targets.push_back( playback_frame_range::contactSheetTargetFrame(
            i, contactSheetFrames, sheetStartFrame, sheetEndFrame ) );
    }
    ASSERT_EQ( sheetEndFrame, targets.back() );

    // Pre-round-2 behaviour: drop-frame mode stays on through the capture replay. Run well past
    // a single loop cycle so a lucky single-cycle overshoot can't hide the defect.
    std::vector<int> dropModePresented;
    double dropPosition = static_cast<double>( sheetStartFrame );
    for( int tick = 0; tick < 200; ++tick )
    {
        const playback_frame_range::DropFrameTickResult result =
            playback_frame_range::advanceDropFrameTick(
                dropPosition, 1.7, cutInValue, cutOutValue, true );
        dropPosition = result.position;
        dropModePresented.push_back( static_cast<int>( dropPosition ) );
    }
    ASSERT_TRUE( countTargetsSatisfiedInOrder( targets, dropModePresented )
                 < static_cast<int>( targets.size() ) );

    // Round-2 fix: drop-frame mode forced off for the capture replay, so playbackHandling's
    // normal-mode branch advances by exactly one frame per tick and loops cutOut -> cutIn on
    // the following tick (MainWindow.cpp's unchanged outer "when on last frame" check) --
    // every target, including the last, is satisfied before any wrap can occur.
    std::vector<int> normalModePresented;
    int normalPosition = sheetStartFrame;
    for( int tick = 0; tick < ( sheetEndFrame - sheetStartFrame ) + 5; ++tick )
    {
        normalModePresented.push_back( normalPosition );
        if( normalPosition >= sheetEndFrame ) break;
        ++normalPosition;
    }
    ASSERT_EQ( static_cast<int>( targets.size() ),
               countTargetsSatisfiedInOrder( targets, normalModePresented ) );
}

TEST( PlaybackFrameRange, LoopWrapTransitionRequiresLoopActive )
{
    // HARDENING repro (LOOP-WRAP-QUALIFICATION): an external backward scrub during a
    // NON-looping session must not be classified as a loop wrap, even though the jump size
    // alone (a full cut-range width) would otherwise look exactly like one.
    ASSERT_FALSE( playback_frame_range::isContactSheetLoopWrapTransition(
        /*loopActive=*/false, /*cutInFrame=*/0, /*cutOutFrame=*/60,
        /*lastPresentedFrame=*/59, /*displayFrame=*/1 ) );
}

TEST( PlaybackFrameRange, LoopWrapTransitionRequiresAJumpConsistentWithTheLoopWidth )
{
    // An arbitrary backward scrub or stress seek to an earlier frame, even with Loop active,
    // is not a wrap unless the jump is (close to) the whole loop-range width.
    ASSERT_FALSE( playback_frame_range::isContactSheetLoopWrapTransition(
        /*loopActive=*/true, /*cutInFrame=*/0, /*cutOutFrame=*/60,
        /*lastPresentedFrame=*/40, /*displayFrame=*/35 ) );
}

TEST( PlaybackFrameRange, LoopWrapTransitionAcceptsTheDropFrameOvershootCase )
{
    // The genuine repro this round fixes: Loop active, drop-frame mode presents a frame short
    // of cutOut-1 (never reaching it, see DropFrameModeWithLoopNeverPresentsTheLastFrameOfARange
    // above) before wrapping close to cutIn.
    ASSERT_TRUE( playback_frame_range::isContactSheetLoopWrapTransition(
        /*loopActive=*/true, /*cutInFrame=*/0, /*cutOutFrame=*/60,
        /*lastPresentedFrame=*/58, /*displayFrame=*/1 ) );
}

// ---- PLAYBACK-CLIP-LENGTH-ENFORCE-1 round 2 (sol BLOCKER 4): the wrap is RECORDED where it happens ----
//
// The presented-frame heuristic misses a genuine wrap when dropped frames leave the last presented
// frame short of the range end: over 0..719 a presented 700 followed by a presented 0 jumps back by
// 700, under the 711 (width 719 - tolerance 8) threshold. The engine's own wrap branch cannot miss it.

TEST( PlaybackWrapRecorder, DroppedFramesBeforeTheWrapStillRecordAnEngineWrap )
{
    const int cutIn = 1, cutOut = 720;               // spin-box values (1-based): frames 0..719
    playback_frame_range::PlaybackWrapRecorder recorder;

    // The last presented frame is 700; the next drop-frame tick advances 19 frames at once (the 19
    // in between are dropped), carrying the position to the range end, so the engine wraps to 0.
    const playback_frame_range::DropFrameTickResult tick =
        playback_frame_range::advanceDropFrameTick( 700.0, 19.0, cutIn, cutOut, /*loopEnabled=*/true );
    ASSERT_TRUE( tick.wrapped );
    ASSERT_EQ( 0, static_cast<int>( tick.position ) );
    if( tick.wrapped ) recorder.noteEngineWrap();   // exactly what MainWindow::playbackHandling does

    // The heuristic alone (the r1 backstop) calls this "not a wrap": that is the escape.
    ASSERT_FALSE( playback_frame_range::isContactSheetLoopWrapTransition(
        /*loopActive=*/true, cutIn - 1, cutOut - 1,
        /*lastPresentedFrame=*/700, /*displayFrame=*/static_cast<int>( tick.position ) ) );
    ASSERT_FALSE( recorder.inferredWrap );

    // The recorder reports wrapped=1 regardless.
    ASSERT_TRUE( recorder.wrapped() );
    ASSERT_EQ( 1, recorder.engineWraps );
}

TEST( PlaybackWrapRecorder, ALongLoopingRunCountsEveryWrapItMakes )
{
    const int cutIn = 1, cutOut = 720;
    playback_frame_range::PlaybackWrapRecorder recorder;
    double position = 0.0;
    int wrapsSeen = 0;
    // 1.7 frames per tick (a fast host dropping frames): 1300 ticks cover ~2210 frames = ~3 laps of 719.
    for( int tick = 0; tick < 1300; ++tick )
    {
        const playback_frame_range::DropFrameTickResult step =
            playback_frame_range::advanceDropFrameTick( position, 1.7, cutIn, cutOut, true );
        position = step.position;
        if( step.wrapped ) { recorder.noteEngineWrap(); ++wrapsSeen; }
    }
    ASSERT_EQ( 3, wrapsSeen );
    ASSERT_EQ( 3, recorder.engineWraps );
    ASSERT_TRUE( recorder.wrapped() );
}

TEST( PlaybackWrapRecorder, ARunThatNeverReachesTheEndOrCannotLoopRecordsNothing )
{
    const int cutIn = 1, cutOut = 720;
    playback_frame_range::PlaybackWrapRecorder shortRun;   // 24 s of a 30 s clip: never reaches the end
    double position = 0.0;
    for( int tick = 0; tick < 340; ++tick )
    {
        const playback_frame_range::DropFrameTickResult step =
            playback_frame_range::advanceDropFrameTick( position, 1.7, cutIn, cutOut, true );
        position = step.position;
        if( step.wrapped ) shortRun.noteEngineWrap();
    }
    ASSERT_FALSE( shortRun.wrapped() );

    playback_frame_range::PlaybackWrapRecorder noLoop;      // Loop off: clamps at the last frame, no wrap
    position = 700.0;
    for( int tick = 0; tick < 50; ++tick )
    {
        const playback_frame_range::DropFrameTickResult step =
            playback_frame_range::advanceDropFrameTick( position, 19.0, cutIn, cutOut, false );
        position = step.position;
        if( step.wrapped ) noLoop.noteEngineWrap();
    }
    ASSERT_FALSE( noLoop.wrapped() );
    ASSERT_EQ( 719, static_cast<int>( position ) );
}

TEST( PlaybackWrapRecorder, TheHeuristicCanStillAddASignalButNeverRemoveOne )
{
    playback_frame_range::PlaybackWrapRecorder recorder;
    recorder.noteInferredWrap();
    ASSERT_TRUE( recorder.wrapped() );
    ASSERT_EQ( 0, recorder.engineWraps );

    playback_frame_range::PlaybackWrapRecorder engineOnly;
    engineOnly.noteEngineWrap();
    ASSERT_TRUE( engineOnly.wrapped() );
    ASSERT_FALSE( engineOnly.inferredWrap );
}

// ---------------------------------------------------------------------------------------------------------
// PLAYBACK-CLIP-LENGTH-ENFORCE-2: the app-side play gate (evaluatePlayableWindow / ProgrammaticPlayLedger)
// and the widened wrap recorder. All pure -- the GUI's programmaticPlay() is a thin wrapper that feeds these
// the live slider / cut spin boxes / clip header and triggers Play only when admit() says so.
// ---------------------------------------------------------------------------------------------------------
namespace
{
const double kFps = 24.0;
const int kFrames30s = 720;   // 30 s at 24 fps
using playback_frame_range::evaluatePlayableWindow;
using playback_frame_range::PlayableWindowVerdict;
using playback_frame_range::ProgrammaticPlayLedger;
}

TEST( PlayableWindow, TheRound2BlockerATwoFrameCutRangeOnAThirtySecondClipIsRefused )
{
    // fable r2 / sol r2 repro: a 720-frame clip, a receipt with cutIn=1 cutOut=2, -Seconds 24, Look Assist on:
    // the whole clip is 30 s but only 2 frames would play. The gate must refuse before Play.
    const PlayableWindowVerdict v = evaluatePlayableWindow( 0, 1, 2, kFrames30s, kFps, 24.0 );
    ASSERT_FALSE( v.ok );
    ASSERT_EQ( std::string( "CLIP_TOO_SHORT" ), std::string( v.reason ) );
    ASSERT_EQ( std::string( "cut_range" ), std::string( v.scope ) );
    ASSERT_EQ( 2, v.playableFrames );
    ASSERT_TRUE( v.playableSeconds < 0.1 );
    ASSERT_TRUE( v.requiredSeconds >= 24.0 );
}

TEST( PlayableWindow, EveryTrackedReceiptCutRangeIsRefusedOnAThirtySecondClip )
{
    // The cut outs the tracked receipts carry (fable r2): 2, 4, 6, 16, 143, 283, 461 -- all under 20 s at 24 fps.
    const int cutOuts[] = { 2, 4, 6, 16, 143, 283, 461 };
    for( const int cutOut : cutOuts )
    {
        const PlayableWindowVerdict v = evaluatePlayableWindow( 0, 1, cutOut, kFrames30s, kFps, 20.0 );
        ASSERT_FALSE( v.ok );
        ASSERT_EQ( std::string( "cut_range" ), std::string( v.scope ) );
    }
}

TEST( PlayableWindow, ATwentySecondWindowFromTheCurrentPositionIsAdmittedAndOneFrameLessIsNot )
{
    // 480 frames = exactly 20 s at 24 fps.
    PlayableWindowVerdict v = evaluatePlayableWindow( 0, 1, 480, 720, kFps, 20.0 );
    ASSERT_TRUE( v.ok );
    ASSERT_EQ( 480, v.playableFrames );
    ASSERT_EQ( std::string( "" ), std::string( v.reason ) );

    v = evaluatePlayableWindow( 0, 1, 479, 720, kFps, 20.0 );
    ASSERT_FALSE( v.ok );

    // Position-aware: the same full range is refused when the position is 1 frame in (479 frames left).
    v = evaluatePlayableWindow( 1, 1, 480, 720, kFps, 20.0 );
    ASSERT_FALSE( v.ok );
    ASSERT_EQ( 479, v.playableFrames );
    v = evaluatePlayableWindow( 1, 1, 481, 720, kFps, 20.0 );
    ASSERT_TRUE( v.ok );
}

TEST( PlayableWindow, TheRequestedWindowRaisesTheBarAboveTwentySeconds )
{
    // 30 s clip, a 25 s requested window from frame 0: ok; from frame 130 (only 24.6 s left): refused.
    ASSERT_TRUE( evaluatePlayableWindow( 0, 1, 720, 720, kFps, 25.0 ).ok );
    const PlayableWindowVerdict v = evaluatePlayableWindow( 130, 1, 720, 720, kFps, 25.0 );
    ASSERT_FALSE( v.ok );
    ASSERT_EQ( std::string( "cut_range" ), std::string( v.scope ) );
    // A request under the floor never lowers it.
    ASSERT_FALSE( evaluatePlayableWindow( 0, 1, 100, 720, kFps, 1.0 ).ok );
}

TEST( PlayableWindow, ThePlayheadAtTheLastFrameIsRefusedBecauseTheJumpToFirstFrameIsAReplay )
{
    // on_actionPlay_triggered jumps to the first frame when Play is pressed on the last frame; from the last
    // frame the window is ONE frame, so a programmatic Play there is refused rather than replayed.
    const PlayableWindowVerdict v = evaluatePlayableWindow( 719, 1, 720, 720, kFps, 20.0 );
    ASSERT_FALSE( v.ok );
    ASSERT_EQ( 1, v.playableFrames );
    // And past the cut out (position beyond Out) nothing is left at all.
    ASSERT_EQ( 0, evaluatePlayableWindow( 400, 1, 300, 720, kFps, 20.0 ).playableFrames );
}

TEST( PlayableWindow, ACollapsedRangeIsMeasuredAsThePlayPathRepairsIt )
{
    // cutIn == cutOut == 1 is widened to the whole clip by the play path (normalizeCutRange repair=true), so
    // the window the gate measures is the clip, not one frame: a 30 s clip is admitted, the 2-frame fixture not.
    ASSERT_TRUE( evaluatePlayableWindow( 0, 1, 1, 720, kFps, 20.0 ).ok );
    ASSERT_FALSE( evaluatePlayableWindow( 0, 1, 1, 2, kFps, 20.0 ).ok );
}

TEST( PlayableWindow, TheTrackedFixturesAreRefusedWhateverTheCutRange )
{
    // 2 and 16 frames (tiny_dual_iso / large_dual_iso): clip-scope refusal.
    PlayableWindowVerdict v = evaluatePlayableWindow( 0, 1, 2, 2, 24.0, 20.0 );
    ASSERT_FALSE( v.ok );
    ASSERT_EQ( std::string( "clip" ), std::string( v.scope ) );
    v = evaluatePlayableWindow( 0, 1, 16, 16, 23.976, 20.0 );
    ASSERT_FALSE( v.ok );
    ASSERT_EQ( std::string( "clip" ), std::string( v.scope ) );
}

TEST( PlayableWindow, AnUnknownClipFailsClosedWithATypedReason )
{
    PlayableWindowVerdict v = evaluatePlayableWindow( 0, 1, 720, 0, kFps, 20.0 );
    ASSERT_FALSE( v.ok );
    ASSERT_EQ( std::string( "CLIP_LENGTH_UNKNOWN" ), std::string( v.reason ) );
    v = evaluatePlayableWindow( 0, 1, 720, 720, 0.0, 20.0 );
    ASSERT_FALSE( v.ok );
    ASSERT_EQ( std::string( "CLIP_LENGTH_UNKNOWN" ), std::string( v.reason ) );
}

TEST( ProgrammaticPlayLedger, TheFirstAdmittedPlayIsTheOnlyOneARestartReplayOrStressSwitchIsRefused )
{
    ProgrammaticPlayLedger ledger;
    const PlayableWindowVerdict good = evaluatePlayableWindow( 0, 1, 720, 720, kFps, 24.0 );
    ASSERT_TRUE( good.ok );
    ASSERT_TRUE( ledger.admit( good ) );
    ASSERT_EQ( 1, ledger.admitted );

    // The same perfectly good window a SECOND time (restart, re-Play, stress-switch re-Play, contact-sheet
    // replay) is REFUSED, not re-gated.
    ASSERT_FALSE( ledger.admit( good ) );
    ASSERT_EQ( std::string( "REPLAY_REFUSED" ), std::string( ledger.lastRefusalReason ) );
    ASSERT_FALSE( ledger.admit( good ) );
    ASSERT_EQ( 1, ledger.admitted );
    ASSERT_EQ( 2, ledger.refused );
}

TEST( ProgrammaticPlayLedger, ARefusedWindowDoesNotUseUpTheProcessesOnePlay )
{
    ProgrammaticPlayLedger ledger;
    ASSERT_FALSE( ledger.admit( evaluatePlayableWindow( 0, 1, 2, 720, kFps, 24.0 ) ) );
    ASSERT_EQ( std::string( "CLIP_TOO_SHORT" ), std::string( ledger.lastRefusalReason ) );
    ASSERT_EQ( 0, ledger.admitted );
    ASSERT_TRUE( ledger.admit( evaluatePlayableWindow( 0, 1, 720, 720, kFps, 24.0 ) ) );
    ASSERT_EQ( 1, ledger.admitted );
}

TEST( PlaybackWrapRecorder, JumpToFirstAndRestartsCountAsReplaysAndMakeTheRunInvalid )
{
    playback_frame_range::PlaybackWrapRecorder jump;
    jump.noteJumpToFirst();
    ASSERT_TRUE( jump.wrapped() );
    ASSERT_EQ( 1, jump.replayCount() );
    ASSERT_EQ( 0, jump.engineWraps );

    playback_frame_range::PlaybackWrapRecorder restart;
    restart.noteRestart();
    ASSERT_TRUE( restart.wrapped() );
    ASSERT_EQ( 1, restart.replayCount() );

    playback_frame_range::PlaybackWrapRecorder all;
    all.noteEngineWrap();
    all.noteJumpToFirst();
    all.noteRestart();
    ASSERT_EQ( 3, all.replayCount() );

    playback_frame_range::PlaybackWrapRecorder clean;   // one Play from frame 0 to the end: nothing recorded
    ASSERT_FALSE( clean.wrapped() );
    ASSERT_EQ( 0, clean.replayCount() );
}

TEST( PlaybackWrapRecorder, MainWindowCountsTheSecondPlayStartAsARestartAndTheLastFramePlayAsAJump )
{
    // Mirrors MainWindow::on_actionPlay_toggled (++m_playStartsInProcess > 1 -> noteRestart) and
    // on_actionPlay_triggered (position+1 >= cutOut -> noteJumpToFirst).
    playback_frame_range::PlaybackWrapRecorder recorder;
    int playStarts = 0;
    const auto pressPlay = [&]( int position, int cutOut )
    {
        if( position + 1 >= cutOut ) recorder.noteJumpToFirst();
        if( ++playStarts > 1 ) recorder.noteRestart();
    };
    pressPlay( 0, 720 );
    ASSERT_FALSE( recorder.wrapped() );   // the one measured Play
    pressPlay( 0, 720 );                  // a second Play of any origin
    ASSERT_EQ( 1, recorder.restartCount );
    pressPlay( 719, 720 );                // pressed on the last frame
    ASSERT_EQ( 1, recorder.jumpToFirstCount );
    ASSERT_EQ( 3, recorder.replayCount() );
}

TEST( PlayableWindow, APresentedFramesTargetIsAPlayWindowAndMustReachTheFloorToo )
{
    using playback_frame_range::presentedFramesTargetReachesFloor;
    // 0 = no early stop: nothing to check.
    ASSERT_TRUE( presentedFramesTargetReachesFloor( 0, 24.0 ) );
    // The pinned-frame capture default (24 frames, ~1 s) is a short play and is refused.
    ASSERT_FALSE( presentedFramesTargetReachesFloor( 24, 24.0 ) );
    ASSERT_FALSE( presentedFramesTargetReachesFloor( 479, 24.0 ) );
    // 480 frames at 24 fps is exactly 20 s; 480 at 23.976 is 20.02 s; 500 at 25 fps is 20 s.
    ASSERT_TRUE( presentedFramesTargetReachesFloor( 480, 24.0 ) );
    ASSERT_TRUE( presentedFramesTargetReachesFloor( 480, 23.976 ) );
    ASSERT_TRUE( presentedFramesTargetReachesFloor( 500, 25.0 ) );
    ASSERT_FALSE( presentedFramesTargetReachesFloor( 480, 29.97 ) );   // 16 s
    // An unknown frame rate fails closed.
    ASSERT_FALSE( presentedFramesTargetReachesFloor( 1000, 0.0 ) );
}

// ---------------------------------------------------------------------------------------------------------
// PLAYBACK-CLIP-LENGTH-ENFORCE-2 round 2 (hub ruling): the REQUESTED / played duration of every programmatic
// Play must itself be >= 20 s -- not only the available window. A refusal is typed and happens before Play.
// ---------------------------------------------------------------------------------------------------------
TEST( PlayableWindow, SolB1ARequestedWindowUnderTheFloorIsRefusedEvenWhenTheAvailableFootageIsLong )
{
    // sol r1 B1 repro: evaluatePlayableWindow(0, 1, 720, 720, 24, 1) was ok (30 s available vs a raised
    // 20 s bar) while the caller's own 1 s stop timer ended Play after one second.
    const PlayableWindowVerdict v = evaluatePlayableWindow( 0, 1, 720, 720, 24.0, 1.0 );
    ASSERT_FALSE( v.ok );
    ASSERT_EQ( std::string( "PLAY_DURATION_TOO_SHORT" ), std::string( v.reason ) );
    ASSERT_EQ( std::string( "requested" ), std::string( v.scope ) );
}

TEST( PlayableWindow, TheRequestedWindowFloorIsExactAndZeroMeansNoWindowAtAll )
{
    ASSERT_FALSE( evaluatePlayableWindow( 0, 1, 720, 720, kFps, 0.0 ).ok );     // "no timer" is not 20 s
    ASSERT_FALSE( evaluatePlayableWindow( 0, 1, 720, 720, kFps, 5.0 ).ok );
    ASSERT_FALSE( evaluatePlayableWindow( 0, 1, 720, 720, kFps, 19.999 ).ok );
    ASSERT_TRUE( evaluatePlayableWindow( 0, 1, 720, 720, kFps, 20.0 ).ok );
    ASSERT_TRUE( evaluatePlayableWindow( 0, 1, 720, 720, kFps, 24.0 ).ok );
    ASSERT_FALSE( evaluatePlayableWindow( 0, 1, 720, 720, kFps, -3.0 ).ok );
    // The ledger never admits a short request either, and a short request does not use up the one Play.
    ProgrammaticPlayLedger ledger;
    ASSERT_FALSE( ledger.admit( evaluatePlayableWindow( 0, 1, 720, 720, kFps, 1.0 ) ) );
    ASSERT_EQ( std::string( "PLAY_DURATION_TOO_SHORT" ), std::string( ledger.lastRefusalReason ) );
    ASSERT_EQ( 0, ledger.admitted );
}

TEST( PlayableWindow, TheSmokePlayRequestIsTheSoonerOfTheTimeoutAndThePresentedFramesTarget )
{
    using playback_frame_range::smokePlayRequestSeconds;
    // No target: the --seconds timeout is the window.
    ASSERT_NEAR( 24.0, smokePlayRequestSeconds( 24000, 0, kFps ), 1e-9 );
    // A target ends Play as soon as it is met, or at the timeout if that comes first: the window is the
    // SMALLER of the two (the pre-fix code took the larger and let a 5 s timeout hide behind a 25 s target).
    ASSERT_NEAR( 5.0, smokePlayRequestSeconds( 5000, 600, kFps ), 1e-9 );
    ASSERT_NEAR( 20.0, smokePlayRequestSeconds( 40000, 480, kFps ), 1e-9 );
    // A target with an unknown fps fails closed to a window the floor refuses.
    const double unknownFps = 0.0;
    ASSERT_FALSE( evaluatePlayableWindow( 0, 1, 720, 720, kFps, smokePlayRequestSeconds( 40000, 480, unknownFps ) ).ok );
    // The pre-existing 100 ms floor on the timeout is kept.
    ASSERT_NEAR( 0.1, smokePlayRequestSeconds( 1, 0, kFps ), 1e-9 );
}

// sol B3 / fable PLAY-GATE-F3-REPAIR-DIVERGENCE-1: the gate must measure the range the ENGINE plays.
TEST( PlayableWindow, SolB3WithTheCollapsedRangeRepairDisabledAOneFrameRangeIsRefused )
{
    // 720 frames, receipt cutIn=cutOut=1, MLVAPP_F3_DISABLE_CUT_RANGE_REPAIR=1: the engine leaves the range at
    // one frame (it refuses to widen it) and Play reaches the stop path at once.
    const PlayableWindowVerdict off = evaluatePlayableWindow( 0, 1, 1, 720, kFps, 20.0, 20.0, false );
    ASSERT_FALSE( off.ok );
    ASSERT_EQ( std::string( "cut_range" ), std::string( off.scope ) );
    ASSERT_EQ( 1, off.playableFrames );
    // Repair enabled (the default and the normal engine state): the same receipt plays the whole clip.
    ASSERT_TRUE( evaluatePlayableWindow( 0, 1, 1, 720, kFps, 20.0, 20.0, true ).ok );
    ASSERT_TRUE( evaluatePlayableWindow( 0, 1, 1, 720, kFps, 20.0 ).ok );
}

TEST( PlayableWindow, FableCutInEqualsCutOutFiveOnAThirtySecondClipPlaysFiveFramesWhenRepairIsDisabled )
{
    const PlayableWindowVerdict off = evaluatePlayableWindow( 0, 5, 5, 720, kFps, 24.0, 20.0, false );
    ASSERT_FALSE( off.ok );
    ASSERT_EQ( 5, off.playableFrames );   // frames 0..4: playbackHandling stops at slider >= cutOut - 1
    const PlayableWindowVerdict on = evaluatePlayableWindow( 0, 5, 5, 720, kFps, 24.0, 20.0, true );
    ASSERT_TRUE( on.ok );
}

TEST( PlayableWindow, WithTheRepairDisabledAnInvertedRangeIsMeasuredAsTheEngineStopsIt )
{
    // cutOut < cutIn: with repair the Play path widens it to the clip end; without it the engine stops at
    // slider >= cutOut - 1, so the window is what lies before the raw cut-out only.
    ASSERT_TRUE( evaluatePlayableWindow( 0, 400, 100, 720, kFps, 20.0, 20.0, true ).ok );
    const PlayableWindowVerdict off = evaluatePlayableWindow( 0, 400, 100, 720, kFps, 20.0, 20.0, false );
    ASSERT_FALSE( off.ok );
    ASSERT_EQ( 100, off.playableFrames );
    // A range that is already fine is the same either way.
    ASSERT_TRUE( evaluatePlayableWindow( 0, 1, 720, 720, kFps, 20.0, 20.0, false ).ok );
}

// sol B2: the lifecycle stress switch stops Play, so it may only happen after the floor.
TEST( PlayableWindow, TheLifecycleStressSwitchMayOnlyHappenAfterTheTwentySecondFloor )
{
    using playback_frame_range::minPlayWindowMs;
    using playback_frame_range::minPlayWindowSeconds;
    using playback_frame_range::stressSwitchReachesFloor;
    ASSERT_EQ( 20000, minPlayWindowMs( 23.976 ) );
    const double floor = minPlayWindowSeconds( 23.976 );
    ASSERT_FALSE( stressSwitchReachesFloor( 0, floor ) );
    ASSERT_FALSE( stressSwitchReachesFloor( 1000, floor ) );      // the pre-fix default: Play stopped after ~1 s
    ASSERT_FALSE( stressSwitchReachesFloor( 19999, floor ) );
    ASSERT_TRUE( stressSwitchReachesFloor( 20000, floor ) );
    ASSERT_TRUE( stressSwitchReachesFloor( 24000, floor ) );
    ASSERT_FALSE( stressSwitchReachesFloor( -1, floor ) );
    // HFR-10S-1: an HFR clip's switch may come at its own 10 s floor, never sooner; an unknown fps keeps 20 s.
    ASSERT_EQ( 10000, minPlayWindowMs( 59.94 ) );
    ASSERT_FALSE( stressSwitchReachesFloor( 9999, minPlayWindowSeconds( 59.94 ) ) );
    ASSERT_TRUE( stressSwitchReachesFloor( 10000, minPlayWindowSeconds( 59.94 ) ) );
    ASSERT_FALSE( stressSwitchReachesFloor( 10000, minPlayWindowSeconds( 0.0 ) ) );
}

// ---------------------------------------------------------------------------------------------------------
// PLAYBACK-CLIP-LENGTH-HFR-10S-1 (owner amendment 2026-10-15): "Amend rule so that hfr 60fps clips are allowed to
// be 10 seconds". The floor is minPlayWindowSeconds( NATIVE fps ): 10 s at >= 50 fps, 20 s below, 20 s unknown.
// ---------------------------------------------------------------------------------------------------------
TEST( HfrPlayFloor, TheFloorIsTenSecondsAtFiftyFpsAndAboveAndTwentyEverywhereElse )
{
    using playback_frame_range::minPlayWindowSeconds;
    using playback_frame_range::isHighFrameRate;
    ASSERT_NEAR( 10.0, minPlayWindowSeconds( 59.94 ), 1e-12 );
    ASSERT_NEAR( 10.0, minPlayWindowSeconds( 60.0 ), 1e-12 );
    ASSERT_NEAR( 10.0, minPlayWindowSeconds( 50.0 ), 1e-12 );
    ASSERT_NEAR( 10.0, minPlayWindowSeconds( 120.0 ), 1e-12 );
    ASSERT_NEAR( 20.0, minPlayWindowSeconds( 49.999 ), 1e-12 );
    ASSERT_NEAR( 20.0, minPlayWindowSeconds( 29.97 ), 1e-12 );
    ASSERT_NEAR( 20.0, minPlayWindowSeconds( 23.976 ), 1e-12 );
    // fail closed: an unknown / unusable fps is the 20 s floor, never 10
    ASSERT_NEAR( 20.0, minPlayWindowSeconds( 0.0 ), 1e-12 );
    ASSERT_NEAR( 20.0, minPlayWindowSeconds( -60.0 ), 1e-12 );
    ASSERT_NEAR( 20.0, minPlayWindowSeconds( std::numeric_limits<double>::quiet_NaN() ), 1e-12 );
    ASSERT_NEAR( 20.0, minPlayWindowSeconds( std::numeric_limits<double>::infinity() ), 1e-12 );
    ASSERT_FALSE( isHighFrameRate( std::numeric_limits<double>::quiet_NaN() ) );
    ASSERT_NEAR( 10.0, playback_frame_range::kLowestMinPlayWindowSeconds, 1e-12 );
}

TEST( HfrPlayFloor, PinnedWindowsAcceptedAndRefusedBeforePlay )
{
    // 59.94 fps 10 s ACCEPTED (12 s clip; ceil(10 x 59.94) = 600 source frames).
    const PlayableWindowVerdict a = evaluatePlayableWindow( 0, 1, 720, 720, 59.94, 10.0 );
    ASSERT_TRUE( a.ok );
    ASSERT_EQ( 600, a.requiredFrames );
    ASSERT_NEAR( 10.0, a.floorSeconds, 1e-12 );
    // 59.94 fps 9.9 s REFUSED.
    const PlayableWindowVerdict b = evaluatePlayableWindow( 0, 1, 720, 720, 59.94, 9.9 );
    ASSERT_FALSE( b.ok );
    ASSERT_EQ( std::string( "PLAY_DURATION_TOO_SHORT" ), std::string( b.reason ) );
    // ...and an HFR clip under 10 s of footage is CLIP_TOO_SHORT however long the request.
    ASSERT_EQ( std::string( "CLIP_TOO_SHORT" ), std::string( evaluatePlayableWindow( 0, 1, 599, 599, 59.94, 10.0 ).reason ) );
    // 23.976 fps 10 s REFUSED; 23.976 fps 20 s ACCEPTED.
    const PlayableWindowVerdict c = evaluatePlayableWindow( 0, 1, 720, 720, 23.976, 10.0 );
    ASSERT_FALSE( c.ok );
    ASSERT_EQ( std::string( "PLAY_DURATION_TOO_SHORT" ), std::string( c.reason ) );
    ASSERT_NEAR( 20.0, c.floorSeconds, 1e-12 );
    ASSERT_TRUE( evaluatePlayableWindow( 0, 1, 720, 720, 23.976, 20.0 ).ok );
    // unknown fps 10 s REFUSED (the 20 s floor applies before the length is even looked at).
    const PlayableWindowVerdict e = evaluatePlayableWindow( 0, 1, 720, 720, 0.0, 10.0 );
    ASSERT_FALSE( e.ok );
    ASSERT_EQ( std::string( "PLAY_DURATION_TOO_SHORT" ), std::string( e.reason ) );
    // 50 fps 10 s ACCEPTED (500 source frames).
    const PlayableWindowVerdict f = evaluatePlayableWindow( 0, 1, 600, 600, 50.0, 10.0 );
    ASSERT_TRUE( f.ok );
    ASSERT_EQ( 500, f.requiredFrames );
    // A presented-frames target is judged by the clip's own floor too.
    ASSERT_TRUE( playback_frame_range::presentedFramesTargetReachesFloor( 600, 59.94 ) );
    ASSERT_FALSE( playback_frame_range::presentedFramesTargetReachesFloor( 599, 59.94 ) );
    ASSERT_FALSE( playback_frame_range::presentedFramesTargetReachesFloor( 300, 29.97 ) );
}

TEST( HfrPlayFloor, AConformedPlayOfAnHfrClipStillAdvancesTenSecondsOfSourceFrames )
{
    // The engine paced at a 24 fps conform: the requirement is still ceil(10 x NATIVE 59.94) = 600 source frames, and
    // the pace must reach them inside the safety budget (599 / 24 = 25 s <= 10 / 0.5 + 15 = 35 s).
    const PlayableWindowVerdict v = evaluatePlayableWindow( 0, 1, 720, 720, 59.94, 10.0, playback_frame_range::kFloorFromNativeFps,
                                                            true, 24.0 );
    ASSERT_TRUE( v.ok );
    ASSERT_EQ( 600, v.requiredFrames );
    ASSERT_TRUE( playback_frame_range::evaluatePlayStop( 599, v.requiredFrames, true, 20000, 35000 ) == playback_frame_range::PlayStopState::Continue );
    ASSERT_TRUE( playback_frame_range::evaluatePlayStop( 600, v.requiredFrames, true, 25000, 35000 ) == playback_frame_range::PlayStopState::Reached );
}

TEST( HfrPlayFloor, AConformedPlayCannotStretchAShortHfrClipIntoALongerWindow )
{
    // HFR-NATIVE-60FPS-FULLRES-1 (HUB RULING 2026-10-30T22:17:51Z): 971 frames at 59.94 are 16.2 s of SOURCE. A 24 fps
    // conform plays them for 40.5 s, but a 25 s window still needs ceil(25 x 59.94) = 1499 source frames: refused.
    const PlayableWindowVerdict shortClip = evaluatePlayableWindow( 0, 1, 971, 971, 59.94, 25.0,
                                                                    playback_frame_range::kFloorFromNativeFps, true, 24.0 );
    ASSERT_FALSE( shortClip.ok );
    ASSERT_EQ( std::string( "CLIP_TOO_SHORT" ), std::string( shortClip.reason ) );
    ASSERT_EQ( 1499, shortClip.requiredFrames );
    // The 27.8 s clip (1667 frames) is admitted at the 24 fps pace: 1498 / 24 = 62.4 s <= 25 / 0.5 + 15 = 65 s.
    const PlayableWindowVerdict longClip = evaluatePlayableWindow( 0, 1, 1667, 1667, 59.94, 25.0,
                                                                   playback_frame_range::kFloorFromNativeFps, true, 24.0 );
    ASSERT_TRUE( longClip.ok );
    ASSERT_EQ( 1499, longClip.requiredFrames );
}

TEST( HfrPlayFloor, AConformTooSlowForTheSourceWindowIsRefusedBeforePlay )
{
    // The ruling's recorded side effect, fail closed: a 120 fps clip conformed to 24 needs 1200 source frames for its
    // 10 s floor, 1199 / 24 = 50 s of Play, over the 10 / 0.5 + 15 = 35 s budget: PLAY_PACE_TOO_SLOW before Play.
    const PlayableWindowVerdict v = evaluatePlayableWindow( 0, 1, 3000, 3000, 120.0, 10.0,
                                                            playback_frame_range::kFloorFromNativeFps, true, 24.0 );
    ASSERT_FALSE( v.ok );
    ASSERT_EQ( std::string( "PLAY_PACE_TOO_SLOW" ), std::string( v.reason ) );
    ASSERT_EQ( 1200, v.requiredFrames );
}

TEST( HfrPlayFloor, AnHfrClipLoopedToReachTenSecondsIsRefusedAndNeverCountsAsPlayed )
{
    // A 5 s HFR clip cannot be looped to 10 s: refused before Play ...
    const PlayableWindowVerdict g = evaluatePlayableWindow( 0, 1, 300, 300, 59.94, 10.0 );
    ASSERT_FALSE( g.ok );
    ASSERT_EQ( std::string( "CLIP_TOO_SHORT" ), std::string( g.reason ) );
    // ... and an engine that wrapped it anyway never counts a wrapped frame: the 600-frame requirement is never
    // Reached and the wrap recorder marks the run looped (the receipt oracle turns that into INVALID_LOOPED).
    playback_frame_range::SourceFrameAdvanceCounter counter;
    playback_frame_range::PlaybackWrapRecorder recorder;
    counter.begin( 0 );
    for( int lap = 0; lap < 3; ++lap )
    {
        for( int frame = 0; frame < 299; ++frame ) counter.noteEngineTick( frame, frame + 1, false );
        counter.noteEngineTick( 299, 0, true );
        recorder.noteEngineWrap();
    }
    ASSERT_EQ( 300, counter.consumed() );
    ASSERT_TRUE( playback_frame_range::evaluatePlayStop( counter.consumed(), 600, true, 1000, 60000 ) != playback_frame_range::PlayStopState::Reached );
    ASSERT_TRUE( recorder.wrapped() );
}

// ENFORCE-3: the ENFORCE-2 wall-clock hold (playHoldReachedFloor: "20000 ms elapsed") is GONE -- it is exactly
// the predicate that called ~10 s of footage a pass under an fps override. An exercise mode that ends its own
// Play now waits on evaluatePlayStop (see the SourceFrame* tests below); nothing on a wall clock alone is a pass.
TEST( PlayableWindow, NoWallClockPredicateRemainsThatCanCallAPlayLongEnough )
{
    using playback_frame_range::PlayStopState;
    using playback_frame_range::evaluatePlayStop;
    for( const int64_t elapsed : { 0, 5000, 12000, 19999, 20000, 24000, 600000 } )
    {
        ASSERT_FALSE( evaluatePlayStop( 0, 480, true, elapsed, 600001 ) == PlayStopState::Reached );
        ASSERT_FALSE( evaluatePlayStop( 239, 480, true, elapsed, 600001 ) == PlayStopState::Reached );
    }
}

// ---------------------------------------------------------------------------------------------------------
// PLAYBACK-CLIP-LENGTH-ENFORCE-3 (owner rule 2026-10-01): "20 s of real footage" is a SOURCE-FRAME quantity.
// Required = ceil(20 x NATIVE fps) distinct source frames consumed from the effective start, within the
// effective range. The engine counts them (SourceFrameAdvanceCounter), the gate admits only a window that
// holds them at the engine's REAL pace, and every automation stop waits for them -- a wall clock is only the
// safety net that ends in a typed failure.
//
// The simulation below drives the engine's OWN per-tick step (advanceDropFrameTick, the exact function
// MainWindow::playbackHandling calls) with an engine pace that differs from the clip's native fps, which is
// sol r2's repro: fpsOverride=12 on a 24 fps, 720-frame clip.
// ---------------------------------------------------------------------------------------------------------
namespace
{
using playback_frame_range::PlayStopState;
using playback_frame_range::SourceFrameAdvanceCounter;
using playback_frame_range::advanceDropFrameTick;
using playback_frame_range::evaluatePlayStop;
using playback_frame_range::playSafetyMs;
using playback_frame_range::requiredSourceFrames;

struct EngineRun
{
    PlayStopState state = PlayStopState::Continue;
    int64_t elapsedMs = 0;
    int64_t consumed = 0;
    double position = 0.0;
    bool wrapped = false;
};

// One engine, ticking every tickMs of wall time at paceFps (the engine's getFramerate()), from startFrame over
// cut range [cutIn, cutOut], stopping per evaluatePlayStop -- or, when legacyHoldLimit > 0, the pre-ENFORCE-3
// rule (stop when that many ms have elapsed, whatever was consumed).
EngineRun runEngine( int startFrame, int cutIn, int cutOut, double paceFps, int64_t required,
                     int64_t safetyMs, int64_t legacyHoldLimit = 0, bool loopEnabled = false,
                     playback_frame_range::PlayPaceMode mode = playback_frame_range::PlayPaceMode::Gated )
{
    EngineRun run;
    SourceFrameAdvanceCounter counter;
    counter.begin( startFrame );
    double position = startFrame;
    const int tickMs = 10;
    for( int64_t elapsed = 0; elapsed <= 900000; elapsed += tickMs )   // past the 765 s CPU ceiling
    {
        const bool playing = position < cutOut - 1 || loopEnabled;
        if( legacyHoldLimit > 0 )
        {
            if( elapsed >= legacyHoldLimit ) { run.state = PlayStopState::Reached; run.elapsedMs = elapsed; break; }
        }
        else
        {
            run.state = evaluatePlayStop( counter.consumed(), required, playing, elapsed, safetyMs, mode );
            if( run.state != PlayStopState::Continue ) { run.elapsedMs = elapsed; break; }
        }
        const playback_frame_range::DropFrameTickResult tick =
            advanceDropFrameTick( position, paceFps * tickMs / 1000.0, cutIn, cutOut, loopEnabled );
        counter.noteEngineTick( position, tick.position, tick.wrapped );
        if( tick.wrapped ) run.wrapped = true;
        position = tick.position;
    }
    run.consumed = counter.consumed();
    run.position = position;
    return run;
}
}

TEST( SourceFrameRequirement, TwentySecondsOfFootageIsCeilTwentyTimesTheNativeFps )
{
    ASSERT_EQ( 480, requiredSourceFrames( 24.0, 20.0 ) );
    ASSERT_EQ( 480, requiredSourceFrames( 24000.0 / 1001.0, 20.0 ) );   // 479.52 -> 480
    ASSERT_EQ( 500, requiredSourceFrames( 25.0, 20.0 ) );
    ASSERT_EQ( 600, requiredSourceFrames( 30.0, 20.0 ) );
    ASSERT_EQ( 576, requiredSourceFrames( 24.0, 24.0 ) );
    ASSERT_EQ( 0, requiredSourceFrames( 0.0, 20.0 ) );     // unknown fps -> 0, which can never be "reached"
    ASSERT_EQ( 0, requiredSourceFrames( 24.0, 0.0 ) );
}

TEST( SourceFrameCounter, CountsDistinctForwardFramesIncludingTheStartFrame )
{
    SourceFrameAdvanceCounter counter;
    ASSERT_EQ( 0, counter.consumed() );                       // not armed: nothing consumed
    counter.noteEngineTick( 0.0, 1.0, false );
    ASSERT_EQ( 0, counter.consumed() );                       // a tick before Play started counts for nothing
    counter.begin( 10 );
    ASSERT_EQ( 1, counter.consumed() );                       // the start frame is on screen
    counter.noteEngineTick( 10.0, 11.0, false );
    counter.noteEngineTick( 11.0, 12.0, false );
    ASSERT_EQ( 3, counter.consumed() );
    counter.noteEngineTick( 12.0, 12.4, false );              // same frame again (sub-frame advance)
    ASSERT_EQ( 3, counter.consumed() );
    counter.noteEngineTick( 12.4, 15.2, false );              // dropped frames: the timeline covered 13, 14, 15
    ASSERT_EQ( 6, counter.consumed() );
}

TEST( SourceFrameCounter, AWrapOrABackwardStepNeverAddsAndNeverCountsFramesTwice )
{
    SourceFrameAdvanceCounter counter;
    counter.begin( 0 );
    for( int f = 0; f < 700; ++f ) counter.noteEngineTick( f, f + 1.0, false );
    ASSERT_EQ( 701, counter.consumed() );
    counter.noteEngineTick( 700.0, 3.0, true );               // the engine wrapped back to the start of the range
    ASSERT_EQ( 701, counter.consumed() );
    for( int f = 3; f < 650; ++f ) counter.noteEngineTick( f, f + 1.0, false );   // the same footage again
    ASSERT_EQ( 701, counter.consumed() );                      // replayed frames are not "20 s of footage"
    ASSERT_TRUE( counter.wrapOrBackwardTicks > 0 );
}

TEST( SourceFrameCounter, ASeekThatIsNotAnEngineTickIsNeverCountedAsFootage )
{
    SourceFrameAdvanceCounter counter;
    counter.begin( 0 );
    counter.noteEngineTick( 0.0, 1.0, false );
    ASSERT_EQ( 2, counter.consumed() );
    // Something (a seek, a snap at Play start) moved the playhead to 400; the next engine tick starts from there.
    counter.noteEngineTick( 400.0, 401.0, false );
    ASSERT_EQ( 1, counter.externalJumpRebases );
    // r2 (sol H1): the frames the engine really advanced BEFORE the seek stay counted (0, 1 and now 401); the jump
    // itself (frames 2..399, never played) is not. consumed() never goes down.
    ASSERT_EQ( 3, counter.consumed() );
    ASSERT_EQ( 0, counter.startFrame );                        // the Play still started at frame 0
    // A jump BACK is not a rebase: those frames were already counted or never will be.
    counter.noteEngineTick( 100.0, 101.0, false );
    ASSERT_EQ( 1, counter.externalJumpRebases );
    ASSERT_EQ( 3, counter.consumed() );
}

TEST( SourceFrameCounter, SolH1ConsumedNeverDecreasesAcrossForwardSeeksAndNeverCountsTheJump )
{
    SourceFrameAdvanceCounter counter;
    counter.begin( 0 );
    int64_t last = counter.consumed();
    for( int f = 0; f < 100; ++f )
    {
        counter.noteEngineTick( f, f + 1.0, false );
        ASSERT_TRUE( counter.consumed() >= last );
        last = counter.consumed();
    }
    ASSERT_EQ( 101, counter.consumed() );
    // Three forward seeks, each followed by engine ticks: the count only ever grows, by the ticks alone.
    const int seeks[] = { 300, 500, 700 };
    for( const int base : seeks )
    {
        for( int f = base; f < base + 10; ++f )
        {
            counter.noteEngineTick( f, f + 1.0, false );
            ASSERT_TRUE( counter.consumed() >= last );
            last = counter.consumed();
        }
    }
    ASSERT_EQ( 3, counter.externalJumpRebases );
    ASSERT_EQ( 131, counter.consumed() );                      // 101 + 3 x 10 ticks; 600 frames of jumps are not footage
    ASSERT_TRUE( counter.consumed() < 701 );
}

TEST( SourceFrameCounter, ASecondBeginDoesNotRestartTheCount )
{
    SourceFrameAdvanceCounter counter;
    counter.begin( 0 );
    counter.noteEngineTick( 0.0, 1.0, false );
    counter.begin( 500 );                                      // a second Play start in the same process
    ASSERT_EQ( 2, counter.consumed() );
    ASSERT_EQ( 1, counter.beginsIgnored );
}

TEST( PlayStopDecision, ReachedEndedEarlySafetyTimeoutAndContinueAreTyped )
{
    ASSERT_TRUE( evaluatePlayStop( 480, 480, true, 100, 35000 ) == PlayStopState::Reached );
    ASSERT_TRUE( evaluatePlayStop( 479, 480, true, 100, 35000 ) == PlayStopState::Continue );
    ASSERT_TRUE( evaluatePlayStop( 200, 480, false, 100, 35000 ) == PlayStopState::EndedEarly );
    ASSERT_TRUE( evaluatePlayStop( 200, 480, true, 35000, 35000 ) == PlayStopState::SafetyTimeout );
    // Reaching the requirement on the very frame the engine stops (range end) is still Reached.
    ASSERT_TRUE( evaluatePlayStop( 480, 480, false, 100, 35000 ) == PlayStopState::Reached );
    // An unknown requirement can never be reached: fail closed.
    ASSERT_FALSE( evaluatePlayStop( 99999, 0, true, 100, 35000 ) == PlayStopState::Reached );
    // A wall clock alone never stops a Play as a success.
    ASSERT_FALSE( evaluatePlayStop( 0, 480, true, 20000, 35000 ) == PlayStopState::Reached );
    ASSERT_FALSE( evaluatePlayStop( 0, 480, true, 600000, 35000 ) == PlayStopState::Reached );
    ASSERT_EQ( std::string( "" ), std::string( playback_frame_range::playStopFailureReason( PlayStopState::Reached ) ) );
    ASSERT_EQ( std::string( "SOURCE_FRAMES_SHORT" ),
               std::string( playback_frame_range::playStopFailureReason( PlayStopState::EndedEarly ) ) );
    ASSERT_EQ( std::string( "PLAY_SAFETY_TIMEOUT" ),
               std::string( playback_frame_range::playStopFailureReason( PlayStopState::SafetyTimeout ) ) );
}

TEST( PlayStopDecision, TheSafetyNetScalesWithTheMinimumSustainedPaceNotAFixedMargin )
{
    // r2 (fable H1): requested / 0.5 + 15 s -- a venue holding half the native pace can still finish.
    ASSERT_EQ( 55000, playSafetyMs( 20.0 ) );
    ASSERT_EQ( 63000, playSafetyMs( 24.0 ) );
    ASSERT_EQ( 95000, playSafetyMs( 40.0 ) );
    ASSERT_TRUE( playSafetyMs( 20.0 ) > playback_frame_range::minPlayWindowMs( 23.976 ) );
    ASSERT_TRUE( playSafetyMs( 20.0 ) >= static_cast<int64_t>( 20000 / playback_frame_range::kMinSustainedPaceFraction ) );
}

TEST( PlayStopDecision, ARunTooSlowToFinishInsideTheBudgetEndsEarlyWithTheTypedPaceToken )
{
    const int64_t budget = playSafetyMs( 20.0 );
    // 50 frames in 9 s -> 480 frames would need ~88 s: doomed, so it ends NOW as PLAY_PACE_TOO_SLOW (not at 55 s).
    ASSERT_TRUE( evaluatePlayStop( 50, 480, true, 9000, budget ) == PlayStopState::PaceTooSlow );
    ASSERT_EQ( std::string( "PLAY_PACE_TOO_SLOW" ),
               std::string( playback_frame_range::playStopFailureReason( PlayStopState::PaceTooSlow ) ) );
    // 150 frames in 9 s projects to ~29 s: fine, keep going.
    ASSERT_TRUE( evaluatePlayStop( 150, 480, true, 9000, budget ) == PlayStopState::Continue );
    // Not trusted before the probe window, nor on a single frame (a slow first frame is not a pace).
    ASSERT_TRUE( evaluatePlayStop( 2, 480, true, playback_frame_range::kPaceProbeMs - 1, budget ) == PlayStopState::Continue );
    ASSERT_TRUE( evaluatePlayStop( 1, 480, true, 20000, budget ) == PlayStopState::Continue );
    // The hard timeout still wins when both would apply, and Reached still beats everything.
    ASSERT_TRUE( evaluatePlayStop( 50, 480, true, budget, budget ) == PlayStopState::SafetyTimeout );
    ASSERT_TRUE( evaluatePlayStop( 480, 480, true, 9000, budget ) == PlayStopState::Reached );
    // A run exactly at half pace finishes inside the budget and is never called too slow on the way.
    const EngineRun half = runEngine( 0, 1, 720, 12.0, 480, budget );
    ASSERT_TRUE( half.state == PlayStopState::Reached );
    ASSERT_TRUE( half.consumed >= 480 );
    // A run at a quarter of the pace is stopped early with the pace token, well before the safety timeout.
    const EngineRun slow = runEngine( 0, 1, 720, 6.0, 480, budget );
    ASSERT_TRUE( slow.state == PlayStopState::PaceTooSlow );
    ASSERT_TRUE( slow.elapsedMs < budget );
    ASSERT_TRUE( slow.consumed < 480 );
}

// ---------------------------------------------------------------------------------------------------------
// HFR-NATIVE-60FPS-FULLRES-1-MERGE-MASTER r11 (ADV-A1b; catalogue C-PACE-RECOVERY-01, M-04): a recovered startup wait
// or stall must not be projected onto every remaining frame. The repro (sol 6551929282, fable 6552056715, r10
// adversary A1): a 1667-frame 59.94 fps clip, default 24 fps conform, requestedSeconds 25, gated, Loop off. Admission
// accepts 1499 source frames (1498 steps / 24 = 62.4 s) inside 65 s. With the first present ~1 s after the trigger and
// 24 fps after, 8 s in it has consumed ~170, and master projected 8000 x 1498 / 169 = 70.9 s -> PLAY_PACE_TOO_SLOW
// (GUI smoke exit 14), although the run finishes at ~63.4 s.
// ---------------------------------------------------------------------------------------------------------
namespace
{
using playback_frame_range::PlayPaceHistory;

struct Stall
{
    int64_t atMs;
    int64_t forMs;
};

// The engine is held (no advance) until firstPresentMs and inside every stall, and otherwise advances at paceFps. The
// stop decision is polled every pollEvery ms with the Play's pace history, exactly as MainWindow::programmaticPlayState
// does. From stopAdvancingAt on (default: never) the engine is held while Play stays on.
EngineRun runStalledEngine( int64_t firstPresentMs, double paceFps, int64_t required, int64_t safetyMs,
                            int64_t pollEvery = 10, std::vector<Stall> stalls = std::vector<Stall>(),
                            int cutOut = 1667,
                            int64_t stopAdvancingAt = std::numeric_limits<int64_t>::max() )
{
    EngineRun run;
    SourceFrameAdvanceCounter counter;
    counter.begin( 0 );
    PlayPaceHistory history;
    double position = 0.0;
    const int64_t tickMs = 1;
    for( int64_t now = 0; now <= 900000; now += tickMs )
    {
        if( now % pollEvery == 0 )
        {
            const bool playing = position < cutOut - 1;
            history.note( counter.consumed(), now );
            run.state = evaluatePlayStop( counter.consumed(), required, playing, now, safetyMs,
                                          playback_frame_range::PlayPaceMode::Gated, history );
            if( run.state != PlayStopState::Continue ) { run.elapsedMs = now; break; }
        }
        bool held = now < firstPresentMs || now >= stopAdvancingAt;
        for( const Stall &s : stalls ) held = held || ( now >= s.atMs && now < s.atMs + s.forMs );
        if( held ) continue;
        const playback_frame_range::DropFrameTickResult tick =
            advanceDropFrameTick( position, paceFps * tickMs / 1000.0, 1, cutOut, false );
        counter.noteEngineTick( position, tick.position, tick.wrapped );
        if( tick.wrapped ) run.wrapped = true;
        position = tick.position;
    }
    run.consumed = counter.consumed();
    run.position = position;
    return run;
}

PlayPaceHistory historyOf( int64_t firstPresentMs, double paceFps, int64_t untilMs, int64_t pollMs = 10 )
{
    PlayPaceHistory history;
    for( int64_t t = 0; t <= untilMs; t += pollMs )
    {
        const int64_t steps = t < firstPresentMs ? 0 : static_cast<int64_t>( ( t - firstPresentMs ) * paceFps / 1000.0 );
        history.note( 1 + steps, t );
    }
    return history;
}
}

TEST( PlayPaceRecovery, AdvA1bTheExactReproProjectsPastTheBudgetOnlyUnderTheOldAverage )
{
    const int64_t budget = playSafetyMs( 25.0 );
    ASSERT_EQ( 65000, budget );
    const PlayableWindowVerdict v = evaluatePlayableWindow( 0, 1, 1667, 1667, 59.94, 25.0, playback_frame_range::kFloorFromNativeFps, true, 24.0 );
    ASSERT_TRUE( v.ok );
    ASSERT_EQ( 1499, v.requiredFrames );
    // master's average since the trigger: 8000 x 1498 / 169 = 70911 ms > 65000 (this is what refused the run)
    ASSERT_TRUE( playback_frame_range::playPaceProjectsPastBudget( 170, 1499, 8000, budget ) );
    ASSERT_TRUE( evaluatePlayStop( 170, 1499, true, 8000, budget ) == PlayStopState::PaceTooSlow );
    // with the Play's history (first present at ~1 s, 24 fps since): 8000 + 1329 steps / 24 fps = 63.4 s -> keep going
    const PlayPaceHistory h = historyOf( 1000, 24.0, 8000 );
    ASSERT_TRUE( h.haveFirstStep );
    ASSERT_FALSE( playback_frame_range::playPaceProjectsPastBudget( 170, 1499, 8000, budget, h ) );
    ASSERT_TRUE( evaluatePlayStop( 170, 1499, true, 8000, budget, playback_frame_range::PlayPaceMode::Gated, h )
                 == PlayStopState::Continue );
}

TEST( PlayPaceRecovery, AdvA1bAFirstPresentAtSixHundredOrOneThousandMsThen24FpsContinuesAndReachesTheEnd )
{
    const int64_t budget = playSafetyMs( 25.0 );
    for( const int64_t firstPresent : { int64_t( 600 ), int64_t( 1000 ) } )
    {
        for( const int64_t poll : { int64_t( 10 ), int64_t( 50 ) } )   // the smoke/settle loops poll at 10 ms, autoplay at 50
        {
            const EngineRun run = runStalledEngine( firstPresent, 24.0, 1499, budget, poll );
            ASSERT_TRUE( run.state == PlayStopState::Reached );
            ASSERT_TRUE( run.consumed >= 1499 );
            ASSERT_TRUE( run.elapsedMs < budget );
            // 1498 steps at 24 fps after the first present: 62.4 s + the wait, paid once
            ASSERT_TRUE( run.elapsedMs >= firstPresent + 62400 && run.elapsedMs <= firstPresent + 62500 + poll );
            ASSERT_FALSE( run.wrapped );
        }
    }
}

TEST( PlayPaceRecovery, AdvA1bTheVenueFirstPresentRangeOfCudaReceiptsIsNotRefused )
{
    // fable 6552056715: CUDA first presents of 384-1691 ms. 1691 + 62417 = 64.1 s, still inside 65 s.
    const int64_t budget = playSafetyMs( 25.0 );
    for( const int64_t firstPresent : { int64_t( 361 ), int64_t( 384 ), int64_t( 1691 ) } )
    {
        const EngineRun run = runStalledEngine( firstPresent, 24.0, 1499, budget );
        ASSERT_TRUE( run.state == PlayStopState::Reached );
    }
}

TEST( PlayPaceRecovery, AdvA1bAMidRunTransientStallThatRecoversIsNotRefused )
{
    // One second held at 4 s (straddling the 8 s probe's view) and another at 20 s: 62.4 s + 2 s = 64.4 s < 65 s.
    const int64_t budget = playSafetyMs( 25.0 );
    std::vector<Stall> stalls;
    stalls.push_back( Stall{ 4000, 1000 } );
    stalls.push_back( Stall{ 20000, 1000 } );
    const EngineRun run = runStalledEngine( 0, 24.0, 1499, budget, 10, stalls );
    ASSERT_TRUE( run.state == PlayStopState::Reached );
    ASSERT_TRUE( run.elapsedMs < budget );
    // A stall right at the probe (7-8 s) too, after 7 s of 24 fps.
    std::vector<Stall> atProbe;
    atProbe.push_back( Stall{ 7000, 1500 } );
    ASSERT_TRUE( runStalledEngine( 0, 24.0, 1499, budget, 10, atProbe ).state == PlayStopState::Reached );
}

TEST( PlayPaceRecovery, R11AdversaryA1AStallTheCountSatThroughIsNotChargedToTheStretchAfterIt )
{
    // r11 adversary A1 (aac1645f), its exact input: polled every 10 ms, consumed(t) = 1 + floor(24 x
    // max(0, t - 1000 - min(1000, max(0, t - 3000))) / 1000) -- first present at 1 s, held 3..4 s, 24 fps otherwise.
    // At (8000, 145) the 4..8 s stretch is 24 fps and the run finishes at 64.4 s; aac1645f anchored the window at the
    // count's FIRST sighting (3000, 49), measured 19.2 fps and refused PaceTooSlow (73.8 s).
    const int64_t budget = playSafetyMs( 25.0 );
    PlayPaceHistory h;
    int64_t consumed = 0;
    for( int64_t t = 0; t <= 8000; t += 10 )
    {
        const int64_t held = std::min<int64_t>( 1000, std::max<int64_t>( 0, t - 3000 ) );
        consumed = 1 + ( 24 * std::max<int64_t>( 0, t - 1000 - held ) ) / 1000;
        h.note( consumed, t );
        ASSERT_TRUE( evaluatePlayStop( consumed, 1499, true, t, budget, playback_frame_range::PlayPaceMode::Gated, h )
                     == PlayStopState::Continue );
    }
    ASSERT_EQ( int64_t( 145 ), consumed );
    ASSERT_TRUE( h.bestPace( consumed, 8000 ) * 1000.0 >= 24.0 );
    std::vector<Stall> stall;
    stall.push_back( Stall{ 3000, 1000 } );
    const EngineRun run = runStalledEngine( 1000, 24.0, 1499, budget, 10, stall );
    ASSERT_TRUE( run.state == PlayStopState::Reached );
    ASSERT_TRUE( run.elapsedMs < budget );
    // the same at the autoplay's 50 ms poll, and with the stall ending as late as 5 s
    ASSERT_TRUE( runStalledEngine( 1000, 24.0, 1499, budget, 50, stall ).state == PlayStopState::Reached );
    std::vector<Stall> late;
    late.push_back( Stall{ 4000, 1000 } );
    ASSERT_TRUE( runStalledEngine( 500, 24.0, 1499, budget, 10, late ).state == PlayStopState::Reached );
}

TEST( PlayPaceRecovery, AdvA1bAPaceThatTrulyCannotFinishIsStillRefusedWithTheTypedToken )
{
    // 20 fps sustained on the same window: 1498 / 20 = 74.9 s > 65 s. Refused at the probe, with or without a wait.
    const int64_t budget = playSafetyMs( 25.0 );
    for( const int64_t firstPresent : { int64_t( 0 ), int64_t( 1000 ) } )
    {
        for( const int64_t poll : { int64_t( 10 ), int64_t( 50 ) } )
        {
            const EngineRun run = runStalledEngine( firstPresent, 20.0, 1499, budget, poll );
            ASSERT_TRUE( run.state == PlayStopState::PaceTooSlow );
            ASSERT_TRUE( run.elapsedMs >= playback_frame_range::kPaceProbeMs && run.elapsedMs < 9000 );
            ASSERT_TRUE( run.consumed < 1499 );
            ASSERT_EQ( std::string( "PLAY_PACE_TOO_SLOW" ),
                       std::string( playback_frame_range::playStopFailureReason( run.state ) ) );
        }
    }
    // 22 fps (68.1 s) is refused too; 23.5 fps (63.7 s) is not.
    ASSERT_TRUE( runStalledEngine( 0, 22.0, 1499, budget ).state == PlayStopState::PaceTooSlow );
    ASSERT_TRUE( runStalledEngine( 0, 23.5, 1499, budget ).state == PlayStopState::Reached );
    // A wait too long to repay: first present at 4 s then 24 fps needs 66.4 s -> refused, never a timeout first.
    const EngineRun late = runStalledEngine( 4000, 24.0, 1499, budget );
    ASSERT_TRUE( late.state == PlayStopState::PaceTooSlow );
    ASSERT_TRUE( late.elapsedMs < budget );
}

TEST( PlayPaceRecovery, AdvA1bAnEngineThatStopsAdvancingNeverPassesAndEndsTyped )
{
    // 24 fps for 5 s, then held while Play stays on: refused (or, at worst, the typed timeout), never Reached, never a hang.
    const int64_t budget = playSafetyMs( 25.0 );
    const EngineRun run = runStalledEngine( 0, 24.0, 1499, budget, 10, std::vector<Stall>(), 1667, 5000 );
    ASSERT_TRUE( run.state == PlayStopState::PaceTooSlow || run.state == PlayStopState::SafetyTimeout );
    ASSERT_TRUE( run.elapsedMs <= budget );
    ASSERT_TRUE( run.consumed < 1499 );
}

TEST( PlayPaceRecovery, NativeRunsAreUnchangedFromMaster )
{
    // Native 59.94 fps (no conform), same window: 1498 / 59.94 = 25 s, tolerated a ~4.9 s startup wait on master.
    const int64_t budget = playSafetyMs( 25.0 );
    const PlayableWindowVerdict v = evaluatePlayableWindow( 0, 1, 1667, 1667, 59.94, 25.0 );
    ASSERT_TRUE( v.ok );
    ASSERT_EQ( 1499, v.requiredFrames );
    ASSERT_TRUE( runStalledEngine( 0, 59.94, 1499, budget ).state == PlayStopState::Reached );
    ASSERT_TRUE( runStalledEngine( 4900, 59.94, 1499, budget ).state == PlayStopState::Reached );
    // the existing native tables, through the history-taking path with a real history
    ASSERT_TRUE( runEngine( 0, 1, 720, 12.0, 480, playSafetyMs( 20.0 ) ).state == PlayStopState::Reached );
    ASSERT_TRUE( runEngine( 0, 1, 720, 6.0, 480, playSafetyMs( 20.0 ) ).state == PlayStopState::PaceTooSlow );
    ASSERT_TRUE( runStalledEngine( 0, 6.0, 480, playSafetyMs( 20.0 ), 10, std::vector<Stall>(), 720 ).state
                 == PlayStopState::PaceTooSlow );
    ASSERT_TRUE( runStalledEngine( 0, 4.0, 600, budget, 10, std::vector<Stall>(), 720 ).state == PlayStopState::PaceTooSlow );
    // a steady run from frame 0: the history agrees with master's average, so the verdict is master's
    for( const double fps : { 4.0, 6.0, 12.0, 24.0, 59.94 } )
    {
        const PlayPaceHistory h = historyOf( 0, fps, 9000 );
        const int64_t consumed = 1 + static_cast<int64_t>( 9000 * fps / 1000.0 );
        ASSERT_EQ( playback_frame_range::playPaceProjectsPastBudget( consumed, 1499, 9000, budget ),
                   playback_frame_range::playPaceProjectsPastBudget( consumed, 1499, 9000, budget, h ) );
    }
}

TEST( PlayPaceRecovery, TheProjectionIsNeverStricterThanMastersAndWithoutHistoryIsMasters )
{
    const int64_t budget = playSafetyMs( 25.0 );
    for( const int64_t firstPresent : { int64_t( 0 ), int64_t( 500 ), int64_t( 3000 ) } )
    {
        for( const double fps : { 10.0, 20.0, 24.0, 30.0 } )
        {
            const PlayPaceHistory h = historyOf( firstPresent, fps, 30000 );
            for( int64_t consumed = 2; consumed < 1499; consumed += 37 )
            {
                for( int64_t elapsed = 8000; elapsed < budget; elapsed += 1700 )
                {
                    const bool master = playback_frame_range::playPaceProjectsPastBudget( consumed, 1499, elapsed, budget );
                    if( !master ) ASSERT_FALSE( playback_frame_range::playPaceProjectsPastBudget( consumed, 1499, elapsed, budget, h ) );
                    // the empty history is master's formula: elapsed x stepsNeeded / stepsDone
                    const double projected = static_cast<double>( elapsed ) * 1498.0 / static_cast<double>( consumed - 1 );
                    if( std::abs( projected - static_cast<double>( budget ) ) > 1e-3 )
                        ASSERT_EQ( projected > static_cast<double>( budget ), master );
                }
            }
        }
    }
}

TEST( PlayPaceRecovery, TheHistoryStartsOverForANewPlayOrAnotherClock )
{
    PlayPaceHistory h = historyOf( 0, 24.0, 9000 );
    ASSERT_TRUE( h.haveFirstStep );
    ASSERT_TRUE( h.bestPace( 217, 9000 ) > 0.02 );
    h.note( 1, 9010 );   // the counter was re-armed (consumed went back)
    ASSERT_FALSE( h.haveFirstStep );
    ASSERT_EQ( 0.0, h.bestWindowPace );
    h = historyOf( 0, 24.0, 9000 );
    h.note( 300, 50 );   // a clock that went back
    ASSERT_TRUE( h.haveFirstStep );
    ASSERT_EQ( int64_t( 50 ), h.firstStep.elapsedMs );
    ASSERT_EQ( 0.0, h.bestWindowPace );
    // nothing measured: no pace, so the probe falls back to master's average
    const PlayPaceHistory empty;
    ASSERT_EQ( 0.0, empty.bestPace( 170, 8000 ) );
    // the window is kept bounded: a 60 s steady run holds about one window of change points, not the whole run
    const PlayPaceHistory longRun = historyOf( 0, 59.94, 60000 );
    ASSERT_TRUE( longRun.window.size() <= static_cast<size_t>( playback_frame_range::kPaceRecentWindowMs * 59.94 / 1000.0 ) + 8 );
}

// ---------------------------------------------------------------------------------------------------------
// r12 (ADV-A1b residual; Dot 6553009018, follow-up 6553147568, on e565602e): SEVERAL recoverable stalls before the 8 s
// probe, so every >= 3 s stretch contains stall time and every r11 estimate stays below the pace the engine plays at.
// Dot's exact input: required 1499, safety 65000, 24 fps, polled every 10 ms, first present at 1 s, held 3.0-3.5 s and
// 5.5-6.0 s: c(t) = 1 + floor(24 x max(0, t - 1000 - min(500, max(0, t - 3000)) - min(500, max(0, t - 5500))) / 1000).
// At 8 s it has consumed 145; continuing at 24 fps it reaches 1499 at 64416.7 ms, inside 65 s.
// ---------------------------------------------------------------------------------------------------------
namespace
{
// The count Dot's formula gives for a first present, a play rate and any number of stalls (each held [atMs, atMs+forMs)).
int64_t stalledCount( int64_t t, int64_t firstPresentMs, int64_t fpsInt, const std::vector<Stall> &stalls )
{
    int64_t played = t - firstPresentMs;
    for( const Stall &s : stalls ) played -= std::min<int64_t>( s.forMs, std::max<int64_t>( 0, t - s.atMs ) );
    return 1 + ( fpsInt * std::max<int64_t>( 0, played ) ) / 1000;
}

struct PolledRun
{
    PlayStopState state = PlayStopState::Continue;
    int64_t elapsedMs = 0;
    int64_t consumed = 0;
    int64_t consumedAt8s = 0;
};

// Polls the stop decision every pollMs exactly as MainWindow::programmaticPlayState does (note, then decide).
PolledRun pollStalledCount( int64_t firstPresentMs, int64_t fpsInt, const std::vector<Stall> &stalls,
                            int64_t required, int64_t safetyMs, int64_t pollMs = 10 )
{
    PolledRun run;
    PlayPaceHistory h;
    for( int64_t t = 0; t <= 900000; t += pollMs )
    {
        run.consumed = stalledCount( t, firstPresentMs, fpsInt, stalls );
        if( t == 8000 ) run.consumedAt8s = run.consumed;
        h.note( run.consumed, t );
        run.state = evaluatePlayStop( run.consumed, required, true, t, safetyMs, playback_frame_range::PlayPaceMode::Gated, h );
        if( run.state != PlayStopState::Continue ) { run.elapsedMs = t; break; }
    }
    return run;
}

std::vector<Stall> stallsOf( std::initializer_list<Stall> list ) { return std::vector<Stall>( list ); }
}

TEST( PlayPaceRecovery, R12DotTwoRecoverableStallsBeforeTheProbeAreNotRefused )
{
    const int64_t budget = playSafetyMs( 25.0 );
    const std::vector<Stall> dot = stallsOf( { Stall{ 3000, 500 }, Stall{ 5500, 500 } } );
    // Dot's arithmetic, re-verified: 145 at 8 s; the uninterrupted stretches are 1-3, 3.5-5.5 and 6-8 s (2 s each).
    ASSERT_EQ( int64_t( 145 ), stalledCount( 8000, 1000, 24, dot ) );
    ASSERT_EQ( int64_t( 1499 ), stalledCount( 64417, 1000, 24, dot ) );
    ASSERT_EQ( int64_t( 1498 ), stalledCount( 64416, 1000, 24, dot ) );
    // every poll up to completion: never refused, never timed out, and Reached at 64.42 s
    const PolledRun run = pollStalledCount( 1000, 24, dot, 1499, budget );
    ASSERT_EQ( int64_t( 145 ), run.consumedAt8s );
    ASSERT_TRUE( run.state == PlayStopState::Reached );
    ASSERT_TRUE( run.elapsedMs >= 64416 && run.elapsedMs <= 64430 );
    // the same through the engine model, at the smoke's 10 ms and the autoplay's 50 ms polls
    for( const int64_t poll : { int64_t( 10 ), int64_t( 50 ) } )
    {
        const EngineRun engine = runStalledEngine( 1000, 24.0, 1499, budget, poll, dot );
        ASSERT_TRUE( engine.state == PlayStopState::Reached );
        ASSERT_TRUE( engine.elapsedMs < budget );
    }
}

TEST( PlayPaceRecovery, R12ThreeStallsStallsOfDifferentLengthsAndAStallStraddlingTheWindowAreNotRefused )
{
    const int64_t budget = playSafetyMs( 25.0 );
    // three stalls (1.3 s in all; stretches of 1.5, 1.5, 1.5 and 1.2 s): finishes at 64.72 s
    // two stalls of 0.3 and 0.9 s (stretches 1.8, 1.7, 2.3 s): finishes at 64.62 s
    // a stall ending 2.9 s before the probe, so the last stretch is just short of the 3 s window, plus an earlier one
    // (stretches 1.0, 2.1, 2.9 s): finishes at 64.42 s
    const std::vector<std::vector<Stall>> cases = {
        stallsOf( { Stall{ 2500, 500 }, Stall{ 4500, 500 }, Stall{ 6500, 300 } } ),
        stallsOf( { Stall{ 2800, 300 }, Stall{ 4800, 900 } } ),
        stallsOf( { Stall{ 2000, 400 }, Stall{ 4500, 600 } } ),
    };
    for( const std::vector<Stall> &stalls : cases )
    {
        int64_t stallMs = 0;
        for( const Stall &s : stalls ) stallMs += s.forMs;
        for( const int64_t poll : { int64_t( 10 ), int64_t( 50 ) } )
        {
            const PolledRun run = pollStalledCount( 1000, 24, stalls, 1499, budget, poll );
            ASSERT_TRUE( run.state == PlayStopState::Reached );
            ASSERT_TRUE( run.elapsedMs < budget );
            ASSERT_TRUE( run.elapsedMs >= 1000 + stallMs + 62416 );
            ASSERT_TRUE( runStalledEngine( 1000, 24.0, 1499, budget, poll, stalls ).state == PlayStopState::Reached );
        }
    }
}

TEST( PlayPaceRecovery, R12AdversaryA1StallsShorterThanAFixedThresholdAreStillSeen )
{
    // r12 adversary A1 (cc9a1944), its exact input: 1 s first present, held [2500,2700), [4500,4700), [6500,6700), 24 fps
    // otherwise, polled every 10 or 50 ms. At 8 s consumed = 154; cc9a1944's fixed 250 ms threshold counted the three
    // ~240 ms holds as playing, measured 22.67 fps and refused PaceTooSlow (67.3 s), although the run finishes at 64.02 s.
    const int64_t budget = playSafetyMs( 25.0 );
    const std::vector<Stall> a1 = stallsOf( { Stall{ 2500, 200 }, Stall{ 4500, 200 }, Stall{ 6500, 200 } } );
    ASSERT_EQ( int64_t( 154 ), stalledCount( 8000, 1000, 24, a1 ) );
    for( const int64_t poll : { int64_t( 10 ), int64_t( 50 ) } )
    {
        const PolledRun run = pollStalledCount( 1000, 24, a1, 1499, budget, poll );
        ASSERT_EQ( int64_t( 154 ), run.consumedAt8s );
        ASSERT_TRUE( run.state == PlayStopState::Reached );
        ASSERT_TRUE( run.elapsedMs >= 64016 && run.elapsedMs < budget );
        ASSERT_TRUE( runStalledEngine( 1000, 24.0, 1499, budget, poll, a1 ).state == PlayStopState::Reached );
    }
    // shorter still: six 100 ms stalls at the 10 ms poll (finishes at 64.02 s)
    const std::vector<Stall> six = stallsOf( { Stall{ 2000, 100 }, Stall{ 3000, 100 }, Stall{ 4000, 100 },
                                               Stall{ 5000, 100 }, Stall{ 6000, 100 }, Stall{ 7000, 100 } } );
    ASSERT_TRUE( pollStalledCount( 1000, 24, six, 1499, budget ).state == PlayStopState::Reached );
}

TEST( PlayPaceRecovery, R12ControlsStillEndTypedPaceTooSlow )
{
    const int64_t budget = playSafetyMs( 25.0 );
    const std::vector<Stall> dot = stallsOf( { Stall{ 3000, 500 }, Stall{ 5500, 500 } } );
    // a genuinely slow engine is refused at the probe, with Dot's stalls or without
    for( const int64_t fps : { int64_t( 20 ), int64_t( 22 ) } )
    {
        for( const std::vector<Stall> &stalls : { std::vector<Stall>(), dot } )
        {
            const PolledRun run = pollStalledCount( 0, fps, stalls, 1499, budget );
            ASSERT_TRUE( run.state == PlayStopState::PaceTooSlow );
            ASSERT_TRUE( run.elapsedMs >= playback_frame_range::kPaceProbeMs && run.elapsedMs < 9000 );
            ASSERT_TRUE( runStalledEngine( 0, static_cast<double>( fps ), 1499, budget, 10, stalls ).state
                         == PlayStopState::PaceTooSlow );
        }
    }
    // a 4 s first present then 24 fps (66.4 s), with an extra stall too: refused at the probe
    ASSERT_TRUE( pollStalledCount( 4000, 24, std::vector<Stall>(), 1499, budget ).elapsedMs < 9000 );
    ASSERT_TRUE( pollStalledCount( 4000, 24, stallsOf( { Stall{ 6000, 300 } } ), 1499, budget ).state == PlayStopState::PaceTooSlow );
    // stall time that keeps growing (0.5, 1, 2, 4, 8, 16 s): the run really cannot finish inside 65 s. It is refused,
    // typed, before the budget -- never Reached, never the timeout.
    const std::vector<Stall> growing = stallsOf( { Stall{ 3000, 500 }, Stall{ 6000, 1000 }, Stall{ 10000, 2000 },
                                                   Stall{ 16000, 4000 }, Stall{ 26000, 8000 }, Stall{ 40000, 16000 } } );
    for( const int64_t poll : { int64_t( 10 ), int64_t( 50 ) } )
    {
        const PolledRun run = pollStalledCount( 500, 24, growing, 1499, budget, poll );
        ASSERT_TRUE( run.state == PlayStopState::PaceTooSlow );
        ASSERT_TRUE( run.elapsedMs < budget );
        ASSERT_TRUE( run.consumed < 1499 );
        // and at the moment it is refused, even 24 fps from here with no further stall could not make 65 s
        ASSERT_TRUE( run.elapsedMs + ( 1499 - run.consumed ) * 1000 / 24 > budget );
    }
    // a time that is all stalls: one step every 400 ms, or ten steps at once every second -- too few playing frames to
    // estimate a pace, so the probe is master's and refuses at 8 s
    {
        PlayPaceHistory h;
        PlayStopState state = PlayStopState::Continue;
        int64_t t = 0;
        for( ; t <= budget && state == PlayStopState::Continue; t += 10 )
        {
            const int64_t c = 1 + t / 400;
            h.note( c, t );
            state = evaluatePlayStop( c, 1499, true, t, budget, playback_frame_range::PlayPaceMode::Gated, h );
        }
        ASSERT_TRUE( state == PlayStopState::PaceTooSlow );
        ASSERT_TRUE( t - 10 < 9000 );
    }
    {
        PlayPaceHistory h;
        PlayStopState state = PlayStopState::Continue;
        int64_t t = 0;
        for( ; t <= budget && state == PlayStopState::Continue; t += 10 )
        {
            const int64_t c = 1 + 10 * ( t / 1000 );
            h.note( c, t );
            state = evaluatePlayStop( c, 1499, true, t, budget, playback_frame_range::PlayPaceMode::Gated, h );
        }
        ASSERT_TRUE( state == PlayStopState::PaceTooSlow );
        ASSERT_TRUE( t - 10 < 9000 );
    }
    // a single short burst (1 s at 24 fps) and then one step every 600 ms: refused at the probe
    {
        PlayPaceHistory h;
        PlayStopState state = PlayStopState::Continue;
        int64_t t = 0;
        for( ; t <= budget && state == PlayStopState::Continue; t += 10 )
        {
            const int64_t c = t < 2000 ? stalledCount( t, 1000, 24, std::vector<Stall>() ) : 25 + ( t - 2000 ) / 600;
            h.note( c, t );
            state = evaluatePlayStop( c, 1499, true, t, budget, playback_frame_range::PlayPaceMode::Gated, h );
        }
        ASSERT_TRUE( state == PlayStopState::PaceTooSlow );
        ASSERT_TRUE( t - 10 < 9000 );
    }
    // stalls that recur all the time (0.6 s of 24 fps, then 0.4 s held, over and over: ~14 fps): refused at the probe
    {
        PlayPaceHistory h;
        PlayStopState state = PlayStopState::Continue;
        int64_t t = 0;
        for( ; t <= budget && state == PlayStopState::Continue; t += 10 )
        {
            const int64_t played = ( t / 1000 ) * 600 + std::min<int64_t>( 600, t % 1000 );
            const int64_t c = 1 + 24 * played / 1000;
            h.note( c, t );
            state = evaluatePlayStop( c, 1499, true, t, budget, playback_frame_range::PlayPaceMode::Gated, h );
        }
        ASSERT_TRUE( state == PlayStopState::PaceTooSlow );
        ASSERT_TRUE( t - 10 < 9000 );
    }
}

TEST( PlayPaceRecovery, R12ThePlayingPaceStartsOverWithTheHistoryAndNeedsEnoughPlaying )
{
    const std::vector<Stall> dot = stallsOf( { Stall{ 3000, 500 }, Stall{ 5500, 500 } } );
    PlayPaceHistory h;
    for( int64_t t = 0; t <= 8000; t += 10 ) h.note( stalledCount( t, 1000, 24, dot ), t );
    ASSERT_TRUE( h.playingPace( 8000 ) * 1000.0 >= 23.9 );
    const PlayPaceHistory::PlayingSplit split = h.playingSplit( 8000 );
    // the two holds of ~550 ms each; r13 (sol r12 B2) charges only the 2 x 500 ms really held, less at most one poll gap
    // and one step period per stall (it was the whole 1100 ms of holds before r13)
    ASSERT_TRUE( split.stallMs >= 1000 - 2 * ( 10 + 42 ) && split.stallMs <= 1000 );
    ASSERT_TRUE( split.thresholdMs >= 50 && split.thresholdMs <= 80 );  // median hold 40 ms + 2 x 10 ms polls
    h.note( 1, 8010 );                                                  // a new Play re-armed the counter
    ASSERT_EQ( int64_t( 0 ), h.holds );
    ASSERT_EQ( int64_t( 0 ), h.overflowHolds );                         // (r14: the poll-gap histogram is gone)
    ASSERT_EQ( int64_t( 0 ), h.playingSplit( 8010 ).thresholdMs );      // no closed hold: no threshold yet
    ASSERT_EQ( int64_t( 0 ), h.playingSplit( 8010 ).stallMs );
    ASSERT_EQ( 0.0, h.playingPace( 8010 ) );
    // a steady engine at any pace has every hold counted as playing (the threshold follows its own cadence)
    for( const int64_t fps : { int64_t( 3 ), int64_t( 12 ), int64_t( 24 ), int64_t( 60 ) } )
    {
        for( const int64_t poll : { int64_t( 10 ), int64_t( 16 ), int64_t( 50 ) } )
        {
            PlayPaceHistory steady;
            for( int64_t t = 0; t <= 20000; t += poll ) steady.note( stalledCount( t, 0, fps, std::vector<Stall>() ), t );
            const PlayPaceHistory::PlayingSplit st = steady.playingSplit( 20000 / poll * poll );
            ASSERT_EQ( int64_t( 0 ), st.stallMs );
        }
    }
    // holds longer than the histogram are always stalls
    PlayPaceHistory longHolds;
    for( int64_t t = 0; t <= 20000; t += 10 ) longHolds.note( 1 + t / 1500, t );
    ASSERT_TRUE( longHolds.overflowHolds > 0 );
    ASSERT_EQ( 0.0, longHolds.playingPace( 20000 ) );
    // 2.9 s of playing is not enough; 3 s is
    PlayPaceHistory s;
    for( int64_t t = 0; t <= 2900; t += 10 ) s.note( stalledCount( t, 0, 24, std::vector<Stall>() ), t );
    ASSERT_EQ( 0.0, s.playingPace( 2900 ) );
    for( int64_t t = 2910; t <= 3100; t += 10 ) s.note( stalledCount( t, 0, 24, std::vector<Stall>() ), t );
    ASSERT_TRUE( s.playingPace( 3100 ) > 0.0 );
    // an open hold longer than the stall threshold is stall time, not playing time
    ASSERT_EQ( int64_t( 0 ), s.playingSplit( 3100 ).stallMs );
    s.note( s.lastConsumed, 3400 );
    ASSERT_TRUE( s.playingSplit( 3400 ).stallMs >= 250 );
    ASSERT_TRUE( s.playingPace( 3400 ) * 1000.0 >= 23.9 );
}

TEST( PlayPaceRecovery, R12TheProjectionWithStallsIsNeverStricterThanMasters )
{
    // the r11 grid, extended to histories with recoverable stalls (polled through Dot's formula)
    const int64_t budget = playSafetyMs( 25.0 );
    const std::vector<std::vector<Stall>> stallSets = {
        std::vector<Stall>(),
        stallsOf( { Stall{ 3000, 500 }, Stall{ 5500, 500 } } ),
        stallsOf( { Stall{ 2500, 500 }, Stall{ 4500, 500 }, Stall{ 6500, 300 } } ),
        stallsOf( { Stall{ 2000, 3000 } } ),
    };
    for( const std::vector<Stall> &stalls : stallSets )
    {
        for( const int64_t fps : { int64_t( 10 ), int64_t( 20 ), int64_t( 24 ), int64_t( 30 ) } )
        {
            PlayPaceHistory h;
            for( int64_t t = 0; t <= 30000; t += 10 ) h.note( stalledCount( t, 500, fps, stalls ), t );
            for( int64_t consumed = 2; consumed < 1499; consumed += 37 )
            {
                for( int64_t elapsed = 8000; elapsed < budget; elapsed += 1700 )
                {
                    const bool master = playback_frame_range::playPaceProjectsPastBudget( consumed, 1499, elapsed, budget );
                    if( !master ) ASSERT_FALSE( playback_frame_range::playPaceProjectsPastBudget( consumed, 1499, elapsed, budget, h ) );
                }
            }
        }
    }
}

// ---------------------------------------------------------------------------------------------------------
// r13 (sol r12 CHANGES_REQUESTED on d5f0a5e1, PR comment 6554246648; catalogue C-PACE-RECOVERY-01, M-04): two more
// feasible recovered conformed runs were refused PLAY_PACE_TOO_SLOW. required 1499, safety 65000, 24 fps, Gated.
// B1 (pace-estimate quantization): first present 1 s, held [1800,2575) and [4800,5575). d5f0a5e1 projected 65305.2 ms
//   at 8 s from 127 steps over 5320 ms (the open hold's 30 ms counted with no step); the run reaches 1499 at 64970 ms
//   (10 ms poll) and at 65000 ms (50 ms poll, where Reached wins over the timeout).
// B2 (stall-share overcharge): first present 10 ms, six 400 ms stalls at 1, 2, ... 6 s. d5f0a5e1 charged the six whole
//   holds (2650 ms, each a frame period plus the poll quantum more than the 2400 ms really held), crossed the one-third
//   share cap and fell back to an 84030 ms projection; the run reaches 1499 at 64830 ms.
// ---------------------------------------------------------------------------------------------------------
TEST( PlayPaceRecovery, R13SolB1QuantizationOfThePaceEstimateDoesNotRefuseAFeasibleRun )
{
    const int64_t budget = playSafetyMs( 25.0 );
    const std::vector<Stall> b1 = stallsOf( { Stall{ 1800, 775 }, Stall{ 4800, 775 } } );
    // sol's c(t) = 1 + floor(24 x max(0, t - 1000 - min(775, max(0, t - 1800)) - min(775, max(0, t - 4800))) / 1000)
    ASSERT_EQ( int64_t( 131 ), stalledCount( 8000, 1000, 24, b1 ) );
    ASSERT_EQ( int64_t( 1498 ), stalledCount( 64966, 1000, 24, b1 ) );
    ASSERT_EQ( int64_t( 1499 ), stalledCount( 64967, 1000, 24, b1 ) );
    // every poll to completion, at the smoke's 10 ms and the autoplay's 50 ms: never refused, never timed out
    const PolledRun at10 = pollStalledCount( 1000, 24, b1, 1499, budget, 10 );
    ASSERT_EQ( int64_t( 131 ), at10.consumedAt8s );
    ASSERT_TRUE( at10.state == PlayStopState::Reached );
    ASSERT_EQ( int64_t( 64970 ), at10.elapsedMs );
    const PolledRun at50 = pollStalledCount( 1000, 24, b1, 1499, budget, 50 );
    ASSERT_TRUE( at50.state == PlayStopState::Reached );
    ASSERT_EQ( int64_t( 65000 ), at50.elapsedMs );   // the poll that sees 1499 is the budget's own: Reached wins
    // and through the drop-frame engine model
    for( const int64_t poll : { int64_t( 10 ), int64_t( 50 ) } )
    {
        const EngineRun engine = runStalledEngine( 1000, 24.0, 1499, budget, poll, b1 );
        ASSERT_TRUE( engine.state == PlayStopState::Reached );
        ASSERT_TRUE( engine.elapsedMs <= budget );
    }
}

TEST( PlayPaceRecovery, R13SolB2WholeHoldsAreNotChargedAsStallTime )
{
    const int64_t budget = playSafetyMs( 25.0 );
    const std::vector<Stall> b2 = stallsOf( { Stall{ 1000, 400 }, Stall{ 2000, 400 }, Stall{ 3000, 400 },
                                              Stall{ 4000, 400 }, Stall{ 5000, 400 }, Stall{ 6000, 400 } } );
    ASSERT_EQ( int64_t( 135 ), stalledCount( 8000, 10, 24, b2 ) );
    for( const int64_t poll : { int64_t( 10 ), int64_t( 50 ) } )
    {
        const PolledRun run = pollStalledCount( 10, 24, b2, 1499, budget, poll );
        ASSERT_EQ( int64_t( 135 ), run.consumedAt8s );
        ASSERT_TRUE( run.state == PlayStopState::Reached );
        ASSERT_TRUE( run.elapsedMs >= 64826 && run.elapsedMs <= budget );
        const EngineRun engine = runStalledEngine( 10, 24.0, 1499, budget, poll, b2 );
        ASSERT_TRUE( engine.state == PlayStopState::Reached );
        ASSERT_TRUE( engine.elapsedMs <= budget );
    }
    // the share charged at 8 s is the time really held (2400 ms), never the whole holds: inside one third
    PlayPaceHistory h;
    for( int64_t t = 0; t <= 8000; t += 10 ) h.note( stalledCount( t, 10, 24, b2 ), t );
    const PlayPaceHistory::PlayingSplit split = h.playingSplit( 8000 );
    ASSERT_TRUE( split.stallMs <= 2400 );
    ASSERT_TRUE( split.stallMs >= 2400 - 6 * 10 - 6 * 42 );   // at most one poll gap and one step period per stall less
    ASSERT_TRUE( 2 * split.stallMs <= split.playingMs );
    ASSERT_TRUE( h.playingPace( 8000 ) * 1000.0 >= 23.9 );
}

TEST( PlayPaceRecovery, R13SlowEnginesAreStillRefusedWithTheB1AndB2StallsToo )
{
    // negative controls for the r13 band: 20 and 22 fps (74.9 s / 68.1 s) are refused at the probe, steady or with
    // either of sol's stall sets, at both polls -- the band only absorbs the poll quantum, never a slower engine
    const int64_t budget = playSafetyMs( 25.0 );
    const std::vector<std::vector<Stall>> sets = {
        std::vector<Stall>(),
        stallsOf( { Stall{ 1800, 775 }, Stall{ 4800, 775 } } ),
        stallsOf( { Stall{ 1000, 400 }, Stall{ 2000, 400 }, Stall{ 3000, 400 },
                    Stall{ 4000, 400 }, Stall{ 5000, 400 }, Stall{ 6000, 400 } } ),
    };
    for( const int64_t fps : { int64_t( 20 ), int64_t( 22 ) } )
    {
        for( const std::vector<Stall> &stalls : sets )
        {
            for( const int64_t poll : { int64_t( 10 ), int64_t( 50 ) } )
            {
                const PolledRun run = pollStalledCount( 0, fps, stalls, 1499, budget, poll );
                ASSERT_TRUE( run.state == PlayStopState::PaceTooSlow );
                ASSERT_TRUE( run.elapsedMs >= playback_frame_range::kPaceProbeMs && run.elapsedMs < 9000 );
                ASSERT_TRUE( runStalledEngine( 0, static_cast<double>( fps ), 1499, budget, poll, stalls ).state
                             == PlayStopState::PaceTooSlow );
            }
        }
    }
    // THE BOUND holds along both of sol's traces and Dot's: at every poll from the probe on, the optimistic finish is
    // never later than the step that really reaches the requirement (64967 / 64826 / 64417 ms in the count's formula)
    const std::vector<std::pair<int64_t, std::vector<Stall>>> traces = {
        { int64_t( 1000 ), stallsOf( { Stall{ 1800, 775 }, Stall{ 4800, 775 } } ) },
        { int64_t( 10 ), sets[2] },
        { int64_t( 1000 ), stallsOf( { Stall{ 3000, 500 }, Stall{ 5500, 500 } } ) },
    };
    for( const auto &trace : traces )
    {
        int64_t reachMs = 0;
        while( stalledCount( reachMs, trace.first, 24, trace.second ) < 1499 ) ++reachMs;
        for( const int64_t poll : { int64_t( 10 ), int64_t( 16 ), int64_t( 50 ) } )
        {
            PlayPaceHistory h;
            for( int64_t t = 0; t < reachMs; t += poll )
            {
                const int64_t c = stalledCount( t, trace.first, 24, trace.second );
                h.note( c, t );
                if( t >= playback_frame_range::kPaceProbeMs )
                    ASSERT_TRUE( h.optimisticFinishMs( c, 1499, t ) <= static_cast<double>( reachMs ) );
            }
        }
    }
    // the trust bounds the band leaves alone (sol r12 INV-6 rows): too little playing time, too few steps, a genuine
    // share-cap crossing -- each refuses the playing pace, so no optimistic finish is offered either
    using Split = PlayPaceHistory::PlayingSplit;
    const auto split = []( int64_t playingMs, int64_t steps, int64_t stallMs )
    {
        Split s;
        s.thresholdMs = 60;
        s.playingMs = playingMs;
        s.playingSteps = steps;
        s.stallMs = stallMs;
        return s;
    };
    ASSERT_FALSE( PlayPaceHistory::playingSplitTrusted( split( 2900, 69, 0 ) ) );
    ASSERT_FALSE( PlayPaceHistory::playingSplitTrusted( split( 4000, 23, 0 ) ) );
    ASSERT_FALSE( PlayPaceHistory::playingSplitTrusted( split( 4000, 96, 2100 ) ) );
    ASSERT_TRUE( PlayPaceHistory::playingSplitTrusted( split( 4000, 96, 2000 ) ) );
    ASSERT_TRUE( std::isinf( PlayPaceHistory().optimisticFinishMs( 170, 1499, 8000 ) ) );   // no history: no band
    // 23 fps steady (1498 / 23 = 65.1 s) cannot finish either; it is refused before the budget, typed
    for( const int64_t poll : { int64_t( 10 ), int64_t( 50 ) } )
    {
        const PolledRun run = pollStalledCount( 0, 23, std::vector<Stall>(), 1499, budget, poll );
        ASSERT_TRUE( run.state == PlayStopState::PaceTooSlow );
        ASSERT_TRUE( run.elapsedMs < budget );
    }
}

// r13 adversary pass on ff00e32a (A1, A2): polls are not evenly spaced (a GUI timer can be late), and the band and
// the stall charge used the MEDIAN poll gap. Each input verbatim: the count, then the poll schedule.
namespace
{
template <typename NextPoll>
PolledRun pollIrregular( int64_t firstPresentMs, const std::vector<Stall> &stalls, NextPoll next,
                         int64_t required, int64_t safetyMs, PlayPaceHistory *atProbe = nullptr )
{
    PolledRun run;
    PlayPaceHistory h;
    for( int64_t t = 0; t <= 900000; t = next( t ) )
    {
        run.consumed = stalledCount( t, firstPresentMs, 24, stalls );
        h.note( run.consumed, t );
        if( atProbe && t >= playback_frame_range::kPaceProbeMs && run.consumedAt8s == 0 )
        {
            run.consumedAt8s = run.consumed;
            *atProbe = h;
        }
        run.state = evaluatePlayStop( run.consumed, required, true, t, safetyMs, playback_frame_range::PlayPaceMode::Gated, h );
        if( run.state != PlayStopState::Continue ) { run.elapsedMs = t; break; }
    }
    return run;
}
}

TEST( PlayPaceRecovery, R13AdversaryA1TheBandUsesTheActualPollGapsNotTheirMedian )
{
    // A1: c(t) = 1 + floor(24 x max(0, t - 1400 - clamp(t - 1800, 0, 550) - clamp(t - 4800, 0, 550)) / 1000); observe
    // at t += 40 when t % 70 == 10, otherwise t += 10. ff00e32a's band (3 x the 10 ms median gap) projected 65307 ms at
    // 8030 and refused; the run is observed Reached at 64940 ms.
    const int64_t budget = playSafetyMs( 25.0 );
    const std::vector<Stall> a1 = stallsOf( { Stall{ 1800, 550 }, Stall{ 4800, 550 } } );
    const auto next = []( int64_t t ) { return t + ( t % 70 == 10 ? 40 : 10 ); };
    const PolledRun run = pollIrregular( 1400, a1, next, 1499, budget );
    ASSERT_TRUE( run.state == PlayStopState::Reached );
    ASSERT_EQ( int64_t( 64940 ), run.elapsedMs );
}

TEST( PlayPaceRecovery, R13AdversaryA2AHoldThatOnlyWaitedOutALongPollGapIsNotChargedAsAStall )
{
    // A2: c(t) = 1 + floor(24 x max(0, t - 1480 - clamp(t - 1800, 0, 500) - clamp(t - 4800, 0, 500)) / 1000); observe
    // at t += 50 when t % 100 == 0, otherwise t += 10. ff00e32a charged the 70/80 ms holds that straddle each 50 ms gap
    // (1910 ms for 1000 ms really held), crossed the share cap and refused at 8000 (74437 ms); Reached at 64900 ms.
    const int64_t budget = playSafetyMs( 25.0 );
    const std::vector<Stall> a2 = stallsOf( { Stall{ 1800, 500 }, Stall{ 4800, 500 } } );
    const auto next = []( int64_t t ) { return t + ( t % 100 == 0 ? 50 : 10 ); };
    PlayPaceHistory probe;
    const PolledRun run = pollIrregular( 1480, a2, next, 1499, budget, &probe );
    ASSERT_TRUE( run.state == PlayStopState::Reached );
    ASSERT_EQ( int64_t( 64900 ), run.elapsedMs );
    const PlayPaceHistory::PlayingSplit split = probe.playingSplit( 8000 );
    ASSERT_TRUE( split.stallMs <= 1000 );   // never more than really held
    // THE BOUND under both irregular schedules: from the probe on, the optimistic finish is never later than the step
    // that really reaches the requirement
    struct Trace { int64_t firstPresentMs; std::vector<Stall> stalls; int schedule; };
    const std::vector<Trace> traces = { { 1400, stallsOf( { Stall{ 1800, 550 }, Stall{ 4800, 550 } } ), 1 },
                                        { 1480, a2, 2 } };
    for( const Trace &tr : traces )
    {
        int64_t reachMs = 0;
        while( stalledCount( reachMs, tr.firstPresentMs, 24, tr.stalls ) < 1499 ) ++reachMs;
        PlayPaceHistory h;
        for( int64_t t = 0; t < reachMs; t += ( tr.schedule == 1 ? ( t % 70 == 10 ? 40 : 10 ) : ( t % 100 == 0 ? 50 : 10 ) ) )
        {
            const int64_t c = stalledCount( t, tr.firstPresentMs, 24, tr.stalls );
            h.note( c, t );
            if( t >= playback_frame_range::kPaceProbeMs )
                ASSERT_TRUE( h.optimisticFinishMs( c, 1499, t ) <= static_cast<double>( reachMs ) );
        }
    }
    // the slow-engine controls under the same two schedules: still refused at the probe
    for( const int64_t fps : { int64_t( 20 ), int64_t( 22 ) } )
    {
        for( const int scheduleId : { 1, 2 } )
        {
            PlayPaceHistory h;
            PlayStopState state = PlayStopState::Continue;
            int64_t t = 0;
            for( ; t <= budget && state == PlayStopState::Continue; t += ( scheduleId == 1 ? ( t % 70 == 10 ? 40 : 10 )
                                                                                  : ( t % 100 == 0 ? 50 : 10 ) ) )
            {
                const int64_t c = stalledCount( t, 0, fps, std::vector<Stall>() );
                h.note( c, t );
                state = evaluatePlayStop( c, 1499, true, t, budget, playback_frame_range::PlayPaceMode::Gated, h );
                if( state != PlayStopState::Continue ) break;
            }
            ASSERT_TRUE( state == PlayStopState::PaceTooSlow );
            ASSERT_TRUE( t >= playback_frame_range::kPaceProbeMs && t < 9000 );
        }
    }
}

// ---------------------------------------------------------------------------------------------------------
// r14 (r13 adversary A1 on 19a83d2d; Dot cos-feedback/mlv-app/pr-420.md 2026-11-01T15:29:45Z; CoS ask 6559996576, hub
// ACK 6560188752): uneven polls collapsed the stall threshold. The input verbatim: required 1499, safety 65000, Gated,
// c(t) = 1 + floor(24 x max(0, t - 800 - clamp(t - 1800, 0, 330) - clamp(t - 4800, 0, 330)) / 1000), observed from t = 0
// with next(t) = t + (t % 125 < 10 ? 80 : 10), note then decide. 19a83d2d binned 57 of the 113 holds closed by 8 s at 0
// (each ended by an 80 ms gap), so its median bin was 0 and its threshold 0 + 2 x the 10 ms median gap = 20 ms: the 51
// ordinary 30/40 ms holds became stalls, 2100 ms of playing time missed the 3 s trust floor and the probe refused at
// 8000 ms (69540 ms at 21.8 fps), although the count reaches 1499 at 63877 ms. The threshold is now the median hold
// UPPER bound (hold + the poll gap before its first sighting): 90 ms here, and r12's median bin + 2 poll gaps exactly
// whenever polls are even. Full trace: the r14 run dir, evidence/uneven-poll-trace.md.
// ---------------------------------------------------------------------------------------------------------
namespace
{
int64_t r14UnevenNext( int64_t t ) { return t + ( t % 125 < 10 ? 80 : 10 ); }

// the 39cdfc57 adversary A1 schedule: every 10 ms except 1410..1450, 2410..2450, ..., 6410..6450 ms
int64_t omittedPollNext( int64_t t )
{
    int64_t n = t + 10;
    while( n >= 1410 && n <= 6450 && n % 1000 >= 410 && n % 1000 <= 450 ) n += 10;
    return n;
}

// pollIrregular at any whole fps (note, then decide, until a verdict)
template <typename NextPoll>
PolledRun pollIrregularAt( int64_t fpsInt, int64_t firstPresentMs, const std::vector<Stall> &stalls, NextPoll next,
                           int64_t required, int64_t safetyMs )
{
    PolledRun run;
    PlayPaceHistory h;
    for( int64_t t = 0; t <= 900000; t = next( t ) )
    {
        run.consumed = stalledCount( t, firstPresentMs, fpsInt, stalls );
        h.note( run.consumed, t );
        run.state = evaluatePlayStop( run.consumed, required, true, t, safetyMs, playback_frame_range::PlayPaceMode::Gated, h );
        if( run.state != PlayStopState::Continue ) { run.elapsedMs = t; break; }
    }
    return run;
}
}

TEST( PlayPaceRecovery, R14AdversaryA1UnevenPollsDoNotTurnOrdinaryHoldsIntoStalls )
{
    const int64_t budget = playSafetyMs( 25.0 );
    const std::vector<Stall> a1 = stallsOf( { Stall{ 1800, 330 }, Stall{ 4800, 330 } } );
    ASSERT_EQ( int64_t( 157 ), stalledCount( 8000, 800, 24, a1 ) );
    ASSERT_EQ( int64_t( 1498 ), stalledCount( 63876, 800, 24, a1 ) );
    ASSERT_EQ( int64_t( 1499 ), stalledCount( 63877, 800, 24, a1 ) );
    // every poll to completion: never refused, never timed out, Reached at the first poll that sees 1499
    PlayPaceHistory probe;
    const PolledRun run = pollIrregular( 800, a1, r14UnevenNext, 1499, budget, &probe );
    ASSERT_EQ( int64_t( 157 ), run.consumedAt8s );
    ASSERT_TRUE( run.state == PlayStopState::Reached );
    ASSERT_EQ( int64_t( 63880 ), run.elapsedMs );
    // at the probe (t = 8000 is a poll): only the two 330 ms holds are stalls, charged no more than really held, and the
    // rest is trusted playing time at 24 fps
    ASSERT_EQ( int64_t( 8000 ), probe.lastElapsedMs );
    ASSERT_EQ( int64_t( 0 ), PlayPaceHistory::histogramMedian( probe.holdCount, probe.holds + probe.overflowHolds ) );
    const PlayPaceHistory::PlayingSplit split = probe.playingSplit( 8000 );
    ASSERT_TRUE( split.thresholdMs > 2 * 10 );            // not 19a83d2d's median bin 0 + 2 x 10 ms
    ASSERT_EQ( int64_t( 2 ), split.stallHolds );
    ASSERT_TRUE( split.stallMs <= 2 * 330 );
    ASSERT_TRUE( PlayPaceHistory::playingSplitTrusted( split ) );
    ASSERT_TRUE( probe.playingPace( 8000 ) * 1000.0 >= 23.9 && probe.playingPace( 8000 ) * 1000.0 <= 24.1 );
    ASSERT_FALSE( playback_frame_range::playPaceProjectsPastBudget( 157, 1499, 8000, budget, probe ) );
    // THE BOUND along this trace: from the probe on, the optimistic finish is never later than the real completion
    {
        PlayPaceHistory h;
        for( int64_t t = 0; t < 63877; t = r14UnevenNext( t ) )
        {
            const int64_t c = stalledCount( t, 800, 24, a1 );
            h.note( c, t );
            if( t >= playback_frame_range::kPaceProbeMs ) ASSERT_TRUE( h.optimisticFinishMs( c, 1499, t ) <= 63877.0 );
        }
    }
    // the "three stalls" set under the same schedule (19a83d2d refused it at 8000 too; it reaches at 64717 ms)
    const PolledRun three = pollIrregular( 1000, stallsOf( { Stall{ 2500, 500 }, Stall{ 4500, 500 }, Stall{ 6500, 300 } } ),
                                           r14UnevenNext, 1499, budget );
    ASSERT_TRUE( three.state == PlayStopState::Reached );
    ASSERT_EQ( int64_t( 64720 ), three.elapsedMs );
    // a steady engine at any pace, polled on this schedule, has no stall hold and no stall time
    for( const int64_t fps : { int64_t( 3 ), int64_t( 12 ), int64_t( 20 ), int64_t( 24 ), int64_t( 30 ), int64_t( 60 ) } )
    {
        PlayPaceHistory steady;
        int64_t last = 0;
        for( int64_t t = 0; t <= 20000; t = r14UnevenNext( t ) ) { steady.note( stalledCount( t, 0, fps, std::vector<Stall>() ), t ); last = t; }
        const PlayPaceHistory::PlayingSplit st = steady.playingSplit( last );
        ASSERT_EQ( int64_t( 0 ), st.stallHolds );
        ASSERT_EQ( int64_t( 0 ), st.stallMs );
    }
    // negative controls under the same schedule: 20 / 22 fps (74.9 s / 68.1 s), steady or with this input's stalls, are
    // refused at the probe; 23 fps (65.1 s) is refused, typed, before the budget
    for( const int64_t fps : { int64_t( 20 ), int64_t( 22 ) } )
    {
        for( const std::vector<Stall> &stalls : { std::vector<Stall>(), a1 } )
        {
            const PolledRun slow = pollIrregularAt( fps, 0, stalls, r14UnevenNext, 1499, budget );
            ASSERT_TRUE( slow.state == PlayStopState::PaceTooSlow );
            ASSERT_TRUE( slow.elapsedMs >= playback_frame_range::kPaceProbeMs && slow.elapsedMs < 9000 );
        }
    }
    const PolledRun at23 = pollIrregularAt( 23, 0, std::vector<Stall>(), r14UnevenNext, 1499, budget );
    ASSERT_TRUE( at23.state == PlayStopState::PaceTooSlow );
    ASSERT_TRUE( at23.elapsedMs < budget );
}

TEST( PlayPaceRecovery, R14WithEvenPollsTheThresholdIsR12sMedianBinPlusTwoPollGapsExactly )
{
    // the pace is never stricter than r13 on an even-poll fixture: there the hold upper bound is the bin + 2 poll gaps,
    // so the median upper bound IS r12/r13's median bin + 2 x the (one) poll gap, at every poll of every fixture
    const std::vector<std::pair<int64_t, std::vector<Stall>>> fixtures = {
        { int64_t( 0 ), std::vector<Stall>() },
        { int64_t( 1000 ), stallsOf( { Stall{ 3000, 500 }, Stall{ 5500, 500 } } ) },
        { int64_t( 1000 ), stallsOf( { Stall{ 2500, 200 }, Stall{ 4500, 200 }, Stall{ 6500, 200 } } ) },
        { int64_t( 1000 ), stallsOf( { Stall{ 1800, 775 }, Stall{ 4800, 775 } } ) },
        { int64_t( 10 ), stallsOf( { Stall{ 1000, 400 }, Stall{ 2000, 400 }, Stall{ 3000, 400 },
                                     Stall{ 4000, 400 }, Stall{ 5000, 400 }, Stall{ 6000, 400 } } ) },
    };
    for( const auto &fx : fixtures )
    {
        for( const int64_t fps : { int64_t( 12 ), int64_t( 22 ), int64_t( 24 ), int64_t( 60 ) } )
        {
            for( const int64_t poll : { int64_t( 10 ), int64_t( 16 ), int64_t( 50 ) } )
            {
                PlayPaceHistory h;
                for( int64_t t = 0; t <= 12000; t += poll )
                {
                    h.note( stalledCount( t, fx.first, fps, fx.second ), t );
                    const int64_t medianBin = PlayPaceHistory::histogramMedian( h.holdCount, h.holds + h.overflowHolds );
                    if( medianBin >= 0 ) ASSERT_EQ( medianBin + 2 * poll, h.playingSplit( t ).thresholdMs );
                }
            }
        }
    }
}

TEST( PlayPaceRecovery, R14TheOmittedPollCaseR13FixedHasAConsoleRegression )
{
    // 39cdfc57 adversary A1 (fixed in 19a83d2d by binning a hold less its own end gap; no console test until r14):
    // c(t) = 1 + floor(24 x max(0, t - 10 - sum(i = 1..6, min(400, max(0, t - 1000 i)))) / 1000), observed every 10 ms
    // except 1410..1450, 2410..2450, ..., 6410..6450 ms. 39cdfc57 charged 2584 ms of stall for the 2400 ms really held,
    // crossed the one-third share cap and refused at 8000 ms (83888 ms); the count reaches 1499 at 64827 ms.
    const int64_t budget = playSafetyMs( 25.0 );
    const std::vector<Stall> b2 = stallsOf( { Stall{ 1000, 400 }, Stall{ 2000, 400 }, Stall{ 3000, 400 },
                                              Stall{ 4000, 400 }, Stall{ 5000, 400 }, Stall{ 6000, 400 } } );
    ASSERT_EQ( int64_t( 1010 ), omittedPollNext( 1000 ) );   // the schedule: 1400 is followed by 1460, 6400 by 6460
    ASSERT_EQ( int64_t( 1460 ), omittedPollNext( 1400 ) );
    ASSERT_EQ( int64_t( 6460 ), omittedPollNext( 6400 ) );
    ASSERT_EQ( int64_t( 7410 ), omittedPollNext( 7400 ) );
    ASSERT_EQ( int64_t( 1498 ), stalledCount( 64826, 10, 24, b2 ) );
    ASSERT_EQ( int64_t( 1499 ), stalledCount( 64827, 10, 24, b2 ) );
    PlayPaceHistory probe;
    const PolledRun run = pollIrregular( 10, b2, omittedPollNext, 1499, budget, &probe );
    ASSERT_EQ( int64_t( 135 ), run.consumedAt8s );
    ASSERT_TRUE( run.state == PlayStopState::Reached );
    ASSERT_EQ( int64_t( 64830 ), run.elapsedMs );
    const PlayPaceHistory::PlayingSplit split = probe.playingSplit( 8000 );
    ASSERT_EQ( int64_t( 6 ), split.stallHolds );
    ASSERT_TRUE( split.stallMs <= 2400 );                 // never more than really held
    ASSERT_TRUE( 2 * split.stallMs <= split.playingMs );  // inside the share cap
    ASSERT_TRUE( probe.playingPace( 8000 ) * 1000.0 >= 23.9 );
    // slow controls on the same schedule: 20 / 22 fps, steady or with the six stalls, refused at the probe
    for( const int64_t fps : { int64_t( 20 ), int64_t( 22 ) } )
    {
        for( const std::vector<Stall> &stalls : { std::vector<Stall>(), b2 } )
        {
            const PolledRun slow = pollIrregularAt( fps, 0, stalls, omittedPollNext, 1499, budget );
            ASSERT_TRUE( slow.state == PlayStopState::PaceTooSlow );
            ASSERT_TRUE( slow.elapsedMs >= playback_frame_range::kPaceProbeMs && slow.elapsedMs < 9000 );
        }
    }
}

TEST( PlayPaceRecovery, R14SixShortStallsTheRecoveredClassAndThePinnedResolutionLimit )
{
    // the r12 DISTINGUISH (pr-comment-r12: six 100 ms stalls at the autoplay's 50 ms poll are refused), now an explicit
    // regression on both sides. Six equal stalls at 2, 3, ... 7 s after a 1 s first present, 24 fps: every length here is
    // feasible (1 s + 6 x len + 62.4 s < 65 s). r15: THE CADENCE THRESHOLD moved the limit at 50 ms from 140 to 80 ms.
    const int64_t budget = playSafetyMs( 25.0 );
    for( const int64_t len : { int64_t( 50 ), int64_t( 80 ), int64_t( 100 ), int64_t( 120 ), int64_t( 140 ),
                               int64_t( 150 ), int64_t( 200 ), int64_t( 250 ) } )
    {
        std::vector<Stall> six;
        for( int64_t at = 2000; at <= 7000; at += 1000 ) six.push_back( Stall{ at, len } );
        ASSERT_TRUE( stalledCount( 64999, 1000, 24, six ) >= 1499 );
        // the smoke's 10 ms poll sees every one of them: Reached, inside the budget
        const PolledRun at10 = pollStalledCount( 1000, 24, six, 1499, budget, 10 );
        ASSERT_TRUE( at10.state == PlayStopState::Reached );
        ASSERT_TRUE( at10.elapsedMs < budget );
        const PolledRun at50 = pollStalledCount( 1000, 24, six, 1499, budget, 50 );
        if( len >= 80 )
        {
            ASSERT_TRUE( at50.state == PlayStopState::Reached );   // the class r12 left open, recovered at 50 ms too
            ASSERT_TRUE( at50.elapsedMs <= budget );
        }
        else
        {
            // THE RESOLUTION LIMIT, pinned (r15): a 50 ms stall on a 41.7 ms step leaves a value that stayed about
            // 92 ms, which a 50 ms poll shows as a 50 ms bin at most, under THE CADENCE THRESHOLD (twice the run's mean
            // step, about 86 ms here). A lower threshold would also cast out an uneven slow engine's ordinary jitter and
            // let it play on (R15AdversaryA1 jitter controls), so the run reads as a ~23 fps cadence and is refused,
            // typed, at the probe -- never a timeout, never a hang.
            ASSERT_TRUE( at50.state == PlayStopState::PaceTooSlow );
            ASSERT_EQ( int64_t( 8000 ), at50.elapsedMs );
        }
    }
}

// ---------------------------------------------------------------------------------------------------------
// r15 (r14 adversary A1 on 11c27fc1; CoS ask 6559996576): recoverable ~150 ms stalls at the autoplay's 50 ms poll still
// refused a feasible conformed run, depending on the phase. The input verbatim: a 1667-frame 59.94 fps clip conformed to
// 24 fps, requestedSeconds 25, required 1499, safety 65000, Gated, Play running, no loop, polled every 50 ms from t = 0
// (note, then decide), c(t) = 1 + floor(24 x max(0, t - 1000 - sum(clamp(t - s, 0, 150) for s in [1750, 2620, 3500,
// 4410, 5380, 6230])) / 1000). At 8000 ms c = 147 and r14's threshold (the median hold upper bound) is 100 ms: three of
// the six 150 ms stall holds bin at exactly 100 ms and stayed playing (6350 ms, 141 steps, 22.2 fps), so the optimistic
// finish was 66926.6 ms and the probe refused (PaceTooSlow, exit 14). The count reaches 1499 at 64317 ms.
// THE CADENCE THRESHOLD (twice an upper bound on the mean step since the first step, about 96 ms here) calls all six
// stalls, at every phase of the poll; the r14 split is unchanged and the cadence split is only used to go on.
// ---------------------------------------------------------------------------------------------------------
namespace
{
std::vector<Stall> r15AdversaryStalls( int64_t offsetMs, int64_t lenMs )
{
    std::vector<Stall> s;
    for( const int64_t at : { int64_t( 1750 ), int64_t( 2620 ), int64_t( 3500 ), int64_t( 4410 ), int64_t( 5380 ), int64_t( 6230 ) } )
        s.push_back( Stall{ at + offsetMs, lenMs } );
    return s;
}

// a count driven by a repeating pattern of step periods (ms) after a first present at firstPresentMs
int64_t dwellPatternCount( int64_t t, int64_t firstPresentMs, const std::vector<int64_t> &dwellMs )
{
    int64_t steps = 0;
    int64_t at = firstPresentMs;
    for( size_t i = 0;; ++i )
    {
        at += dwellMs[i % dwellMs.size()];
        if( at > t ) break;
        ++steps;
    }
    return 1 + steps;
}
}

TEST( PlayPaceRecovery, R15AdversaryA1RecoveredShortStallsAreNotRefusedAtAnyPollPhase )
{
    const int64_t budget = playSafetyMs( 25.0 );
    ASSERT_EQ( int64_t( 65000 ), budget );
    const std::vector<Stall> a1 = r15AdversaryStalls( 0, 150 );
    ASSERT_EQ( int64_t( 147 ), stalledCount( 8000, 1000, 24, a1 ) );
    ASSERT_EQ( int64_t( 1498 ), stalledCount( 64316, 1000, 24, a1 ) );
    ASSERT_EQ( int64_t( 1499 ), stalledCount( 64317, 1000, 24, a1 ) );
    // the exact input: Reached at the first 50 ms poll that sees 1499, never refused, never timed out
    const PolledRun run = pollStalledCount( 1000, 24, a1, 1499, budget, 50 );
    ASSERT_EQ( int64_t( 147 ), run.consumedAt8s );
    ASSERT_TRUE( run.state == PlayStopState::Reached );
    ASSERT_EQ( int64_t( 64350 ), run.elapsedMs );
    // at the probe: the adversary's r14 split is still what playingSplit reports, and the cadence split sees six stalls
    PlayPaceHistory probe;
    for( int64_t t = 0; t <= 8000; t += 50 ) probe.note( stalledCount( t, 1000, 24, a1 ), t );
    const PlayPaceHistory::PlayingSplit r14 = probe.playingSplit( 8000 );
    ASSERT_EQ( int64_t( 100 ), r14.thresholdMs );
    ASSERT_EQ( int64_t( 6350 ), r14.playingMs );
    ASSERT_EQ( int64_t( 141 ), r14.playingSteps );
    ASSERT_EQ( int64_t( 315 ), r14.stallMs );
    ASSERT_EQ( int64_t( 200 ), r14.stretchEndLateMs );
    const PlayPaceHistory::PlayingSplit cadence = probe.cadenceSplit( 8000 );
    ASSERT_TRUE( cadence.thresholdMs >= 2 * 42 && cadence.thresholdMs < 100 );
    ASSERT_EQ( int64_t( 6 ), cadence.stallHolds );
    ASSERT_TRUE( cadence.stallMs <= 6 * 150 );              // never more than really held
    ASSERT_TRUE( PlayPaceHistory::playingSplitTrusted( cadence ) );
    // 138 steps over 5900 ms (23.4 fps, r14 22.2): the 50 ms stretch ends are late by up to 350 ms in all, which the
    // r13 band takes back -- the optimistic finish (62334 ms) is not later than the real 64317 ms
    ASSERT_TRUE( probe.playingPace( 8000 ) * 1000.0 >= 23.3 );
    ASSERT_TRUE( probe.optimisticFinishMs( 147, 1499, 8000 ) <= 64317.0 );
    ASSERT_FALSE( playback_frame_range::playPaceProjectsPastBudget( 147, 1499, 8000, budget, probe ) );
    // THE PHASE SWEEP: the stall list offset by 0..49 ms, at the autoplay's 50 ms, a 33 ms and the smoke's 10 ms poll,
    // and stalls of 80 to 250 ms on the same irregular starts: every run is Reached inside the budget, never before the
    // count really reaches 1499; and from the probe on the optimistic finish is never later than that (THE BOUND)
    for( const int64_t len : { int64_t( 80 ), int64_t( 100 ), int64_t( 120 ), int64_t( 150 ), int64_t( 200 ), int64_t( 250 ) } )
    {
        for( int64_t off = 0; off < 50; ++off )
        {
            const std::vector<Stall> stalls = r15AdversaryStalls( off, len );
            int64_t reachMs = 0;
            while( stalledCount( reachMs, 1000, 24, stalls ) < 1499 ) ++reachMs;
            ASSERT_TRUE( reachMs < budget );
            for( const int64_t poll : { int64_t( 50 ), int64_t( 33 ), int64_t( 10 ) } )
            {
                const PolledRun r = pollStalledCount( 1000, 24, stalls, 1499, budget, poll );
                ASSERT_TRUE( r.state == PlayStopState::Reached );
                ASSERT_TRUE( r.elapsedMs >= reachMs && r.elapsedMs <= budget );
            }
            if( len == 150 )
            {
                PlayPaceHistory h;
                for( int64_t t = 0; t < reachMs; t += 50 )
                {
                    const int64_t c = stalledCount( t, 1000, 24, stalls );
                    h.note( c, t );
                    if( t >= playback_frame_range::kPaceProbeMs )
                        ASSERT_TRUE( h.optimisticFinishMs( c, 1499, t ) <= static_cast<double>( reachMs ) );
                }
            }
        }
    }
    // a steady engine at any pace, on even and uneven schedules, has no cadence stall: every bin is under its step
    for( const int64_t fps : { int64_t( 3 ), int64_t( 12 ), int64_t( 20 ), int64_t( 24 ), int64_t( 30 ), int64_t( 60 ) } )
    {
        for( const int schedule : { 10, 16, 50, 125 } )
        {
            PlayPaceHistory steady;
            int64_t last = 0;
            for( int64_t t = 0; t <= 20000; t = ( schedule == 125 ? r14UnevenNext( t ) : t + schedule ) )
            {
                steady.note( stalledCount( t, 0, fps, std::vector<Stall>() ), t );
                last = t;
            }
            const PlayPaceHistory::PlayingSplit st = steady.cadenceSplit( last );
            ASSERT_EQ( int64_t( 0 ), st.stallHolds );
            ASSERT_EQ( int64_t( 0 ), st.stallMs );
        }
    }
    // negative controls: 20 / 22 fps with the adversary's stalls are refused at the probe at every poll; 23 fps (which
    // cannot finish either) is refused, typed, before the budget
    for( const int64_t poll : { int64_t( 10 ), int64_t( 50 ) } )
    {
        for( const int64_t fps : { int64_t( 20 ), int64_t( 22 ) } )
        {
            const PolledRun slow = pollStalledCount( 1000, fps, a1, 1499, budget, poll );
            ASSERT_TRUE( slow.state == PlayStopState::PaceTooSlow );
            ASSERT_TRUE( slow.elapsedMs >= playback_frame_range::kPaceProbeMs && slow.elapsedMs < 9000 );
        }
        const PolledRun at23 = pollStalledCount( 1000, 23, a1, 1499, budget, poll );
        ASSERT_TRUE( at23.state == PlayStopState::PaceTooSlow );
        ASSERT_TRUE( at23.elapsedMs < budget );
    }
    // jitter controls (why the factor is 2, not 1): uneven slow engines whose mean step is 45 ms (22.2 fps, 67.4 s) --
    // 40/50, 30/60 and 20/70 ms alternation -- are refused at the probe as on r14, at the 10, 16 and 50 ms polls: no
    // ordinary step of theirs stays twice the mean. (Factor 1 let the 30/60 one play to 62 s before the typed refusal.)
    const std::vector<std::vector<int64_t>> uneven = { { 40, 50 }, { 30, 60 }, { 20, 70 } };
    for( const std::vector<int64_t> &pattern : uneven )
    {
        for( const int64_t poll : { int64_t( 10 ), int64_t( 16 ), int64_t( 50 ) } )
        {
            PlayPaceHistory h;
            PlayStopState state = PlayStopState::Continue;
            int64_t t = 0;
            for( ; t <= budget; t += poll )
            {
                const int64_t c = dwellPatternCount( t, 0, pattern );
                h.note( c, t );
                state = evaluatePlayStop( c, 1499, true, t, budget, playback_frame_range::PlayPaceMode::Gated, h );
                if( state != PlayStopState::Continue ) break;
            }
            ASSERT_TRUE( state == PlayStopState::PaceTooSlow );
            ASSERT_TRUE( t >= playback_frame_range::kPaceProbeMs && t < 9000 );
        }
    }
    // the first forward step's lead gap must be known: a history whose first observation already had a step has no
    // cadence bound, and the cadence split is then the r14 split exactly
    PlayPaceHistory noLead;
    for( int64_t t = 1500; t <= 8000; t += 50 ) noLead.note( stalledCount( t, 1000, 24, a1 ), t );
    ASSERT_EQ( int64_t( -1 ), noLead.cadenceThresholdMs() );
    ASSERT_EQ( noLead.playingSplit( 8000 ).thresholdMs, noLead.cadenceSplit( 8000 ).thresholdMs );
}

TEST( LookAssistSettle, ASecondSettleNeverAsksForAnotherPlayBecauseTheProcessAdmitsExactlyOne )
{
    using playback_frame_range::lookAssistSettleNeedsOwnPlay;
    ASSERT_TRUE( lookAssistSettleNeedsOwnPlay( 0 ) );          // the load settle: it owns the one Play
    ASSERT_FALSE( lookAssistSettleNeedsOwnPlay( 1 ) );         // the recheck settle after the off/on toggle
    ASSERT_FALSE( lookAssistSettleNeedsOwnPlay( 2 ) );
    // The ledger agrees: a second admitted Play is REPLAY_REFUSED, so asking for one would be exit 14 every time.
    playback_frame_range::ProgrammaticPlayLedger ledger;
    const PlayableWindowVerdict ok = evaluatePlayableWindow( 0, 1, 720, 720, 24.0, 20.0 );
    ASSERT_TRUE( ledger.admit( ok ) );
    ASSERT_FALSE( ledger.admit( ok ) );
    ASSERT_EQ( std::string( "REPLAY_REFUSED" ), std::string( ledger.lastRefusalReason ) );
    ASSERT_FALSE( lookAssistSettleNeedsOwnPlay( ledger.admitted ) );
}

// sol r2 BLOCKER, reproduced: a persisted fpsOverride=12 on a 24 fps, 720-frame clip.
TEST( SourceFrameEngine, SolR2ReproTheOldWallClockRuleStopsAtTwentySecondsAfterTenSecondsOfFootage )
{
    const int64_t required = requiredSourceFrames( 24.0, 20.0 );
    const EngineRun old = runEngine( 0, 1, 720, 12.0, required, playSafetyMs( 20.0 ), 20000 );
    ASSERT_EQ( 20000, old.elapsedMs );
    ASSERT_TRUE( old.consumed < required );                    // ~10 s of source footage ...
    ASSERT_TRUE( old.consumed >= 240 && old.consumed <= 242 ); // ... 240 frames at 12 fps, not 480
    // ... and the pre-ENFORCE-3 verdict (a wall-clock hold) called that a pass; the oracle must not.
    ASSERT_FALSE( old.consumed >= required );
}

TEST( SourceFrameEngine, TheSameRunUnderTheNewRuleIsRefusedBeforePlayAtTheEnginesRealPace )
{
    // Admission at the engine's actual pace: 479 further frames at 8 fps need 59.9 s of wall clock, and the
    // caller's budget for a 20 s request is 55 s (r2: requested / 0.5 + 15 s) -> refused BEFORE Play, typed.
    const PlayableWindowVerdict v = evaluatePlayableWindow( 0, 1, 720, 720, 24.0, 20.0, 20.0, true, 8.0 );
    ASSERT_FALSE( v.ok );
    ASSERT_EQ( std::string( "PLAY_PACE_TOO_SLOW" ), std::string( v.reason ) );
    ASSERT_EQ( std::string( "pace" ), std::string( v.scope ) );
    ASSERT_EQ( 480, v.requiredFrames );
    ASSERT_TRUE( v.wallNeededSeconds > v.wallBudgetSeconds );
    // The 24 s request of the repro (--seconds 24) is refused for the same reason.
    ASSERT_FALSE( evaluatePlayableWindow( 0, 1, 720, 720, 24.0, 24.0, 20.0, true, 8.0 ).ok );
    // Exactly the minimum sustained pace (half of native) is admitted -- and must then consume all 480 frames.
    ASSERT_TRUE( evaluatePlayableWindow( 0, 1, 720, 720, 24.0, 20.0, 20.0, true, 12.0 ).ok );
}

TEST( SourceFrameEngine, ASlowPaceThatStillFitsTheBudgetIsAdmittedAndMustConsumeEverySourceFrame )
{
    // 20 fps pace on a 24 fps clip: 479 steps take 23.95 s <= 35 s: admitted, and the run only ends once the
    // 480 source frames were consumed -- 24 s of wall clock, not 20.
    ASSERT_TRUE( evaluatePlayableWindow( 0, 1, 720, 720, 24.0, 20.0, 20.0, true, 20.0 ).ok );
    const EngineRun run = runEngine( 0, 1, 720, 20.0, 480, playSafetyMs( 20.0 ) );
    ASSERT_TRUE( run.state == PlayStopState::Reached );
    ASSERT_TRUE( run.consumed >= 480 );
    ASSERT_TRUE( run.elapsedMs > 20000 );                      // a wall-clock-only stop at 20 s would have been early
    ASSERT_FALSE( run.wrapped );
}

TEST( SourceFrameEngine, AFasterPaceConsumesTheSourceFramesSoonerAndStillStopsOnConsumption )
{
    ASSERT_TRUE( evaluatePlayableWindow( 0, 1, 720, 720, 24.0, 20.0, 20.0, true, 60.0 ).ok );
    const EngineRun run = runEngine( 0, 1, 720, 60.0, 480, playSafetyMs( 20.0 ) );
    ASSERT_TRUE( run.state == PlayStopState::Reached );
    ASSERT_TRUE( run.consumed >= 480 );
    ASSERT_TRUE( run.elapsedMs < 20000 );
}

TEST( SourceFrameEngine, ARunAtNativePaceConsumesExactlyTheRequirementInAboutTwentySeconds )
{
    const EngineRun run = runEngine( 0, 1, 720, 24.0, 480, playSafetyMs( 20.0 ) );
    ASSERT_TRUE( run.state == PlayStopState::Reached );
    ASSERT_TRUE( run.consumed >= 480 && run.consumed <= 483 );
    ASSERT_TRUE( run.elapsedMs >= 19900 && run.elapsedMs <= 20200 );
}

TEST( SourceFrameEngine, AWindowOfExactlyTheRequirementIsReachedOnTheFrameTheEngineStops )
{
    // 480 frames from frame 0 -> the engine clamps on frame 479 (cutOut-1) having consumed all 480.
    ASSERT_TRUE( evaluatePlayableWindow( 0, 1, 480, 720, 24.0, 20.0, 20.0, true, 24.0 ).ok );
    const EngineRun run = runEngine( 0, 1, 480, 24.0, 480, playSafetyMs( 20.0 ) );
    ASSERT_TRUE( run.state == PlayStopState::Reached );
    ASSERT_EQ( 480, run.consumed );
}

TEST( SourceFrameEngine, ARangeEndingBeforeTheRequirementEndsEarlyNeverAsAPass )
{
    // Whatever the admission said, an engine that runs out of range first is EndedEarly (typed), not Reached.
    const EngineRun run = runEngine( 0, 1, 300, 24.0, 480, playSafetyMs( 20.0 ) );
    ASSERT_TRUE( run.state == PlayStopState::EndedEarly );
    ASSERT_TRUE( run.consumed < 480 );
}

TEST( SourceFrameEngine, ALoopingEngineNeverReachesTheRequirementByReplayingFootage )
{
    // Loop on a 100-frame range: the position wraps, the counter ignores the replayed footage, so 480 is never
    // consumed -- the run ends typed (PLAY_PACE_TOO_SLOW once the projection is doomed, else the safety timeout),
    // never Reached, and the wrap is visible.
    const EngineRun run = runEngine( 0, 1, 100, 24.0, 480, playSafetyMs( 20.0 ), 0, true );
    ASSERT_TRUE( run.state == PlayStopState::SafetyTimeout || run.state == PlayStopState::PaceTooSlow );
    ASSERT_FALSE( run.state == PlayStopState::Reached );
    ASSERT_TRUE( run.consumed <= 100 );
    ASSERT_TRUE( run.wrapped );
}

TEST( SourceFrameAdmission, TheWindowIsMeasuredInSourceFramesAtTheNativeFps )
{
    // 479 playable frames is one short of ceil(20 x 24); 480 is admitted. (Same answer as the seconds test,
    // but now stated in the unit the engine counts.)
    PlayableWindowVerdict v = evaluatePlayableWindow( 0, 1, 479, 720, 24.0, 20.0 );
    ASSERT_FALSE( v.ok );
    ASSERT_EQ( 480, v.requiredFrames );
    ASSERT_EQ( 479, v.playableFrames );
    v = evaluatePlayableWindow( 0, 1, 480, 720, 24.0, 20.0 );
    ASSERT_TRUE( v.ok );
    ASSERT_EQ( 480, v.requiredFrames );
    // A 24 s request needs 576 frames, whatever the floor.
    ASSERT_EQ( 576, evaluatePlayableWindow( 0, 1, 720, 720, 24.0, 24.0 ).requiredFrames );
    ASSERT_FALSE( evaluatePlayableWindow( 0, 1, 575, 720, 24.0, 24.0 ).ok );
    ASSERT_TRUE( evaluatePlayableWindow( 0, 1, 576, 720, 24.0, 24.0 ).ok );
}

TEST( SourceFrameAdmission, ADefaultPaceMeansTheNativeFpsAndAZeroPaceFailsClosed )
{
    ASSERT_TRUE( evaluatePlayableWindow( 0, 1, 720, 720, 24.0, 20.0 ).ok );                       // pace defaults to native
    const PlayableWindowVerdict v = evaluatePlayableWindow( 0, 1, 720, 720, 24.0, 20.0, 20.0, true, -1.0 );
    ASSERT_FALSE( v.ok );                                                                          // an unusable pace
    ASSERT_EQ( std::string( "PLAY_PACE_TOO_SLOW" ), std::string( v.reason ) );
}

using playback_frame_range::AutomationVerdictLatch;

// ENFORCE-4 (fable/sol r2 blocker: an autoplay closed mid-Play exited 0): the hook's exit verdict is a LATCH that is
// armed FAILING the moment an automation Play is requested and cleared only by consumption (Reached).
TEST( AutomationVerdictLatch, AnUnarmedLatchIsExitZeroBecauseNoAutomationPlayWasRequested )
{
    const AutomationVerdictLatch latch;
    ASSERT_EQ( 0, latch.exitCode() );
    ASSERT_FALSE( latch.pending() );
}

TEST( AutomationVerdictLatch, ARequestedPlayThatIsNeverResolvedExitsFailingNotZero )
{
    // The app is closed (closeEvent -> quit) before the poll timer resolves anything: still 14.
    AutomationVerdictLatch latch;
    latch.armPending();
    ASSERT_EQ( 14, latch.exitCode() );
    ASSERT_TRUE( latch.pending() );
}

TEST( AutomationVerdictLatch, OnlyReachedClearsTheLatch )
{
    for( PlayStopState state : { PlayStopState::Continue, PlayStopState::EndedEarly, PlayStopState::SafetyTimeout,
                                 PlayStopState::PaceTooSlow } )
    {
        AutomationVerdictLatch latch;
        latch.armPending();
        latch.resolve( state );
        ASSERT_EQ( 14, latch.exitCode() );
    }
    AutomationVerdictLatch reached;
    reached.armPending();
    reached.resolve( PlayStopState::Reached );
    ASSERT_EQ( 0, reached.exitCode() );
    ASSERT_FALSE( reached.pending() );
}

TEST( AutomationVerdictLatch, ARefusalAndALateRearmBothFailClosed )
{
    AutomationVerdictLatch refused;
    refused.armPending();
    refused.fail();
    ASSERT_EQ( 14, refused.exitCode() );
    // A resolved-Reached latch that is armed again (a second automation Play) is failing again until it resolves.
    AutomationVerdictLatch again;
    again.armPending();
    again.resolve( PlayStopState::Reached );
    again.armPending();
    ASSERT_EQ( 14, again.exitCode() );
}

TEST( AutomationVerdictLatch, ARefusalAloneFailsAnUnarmedLatch )
{
    // The refusal path must stand on its own: fail() is what the hook calls when programmaticPlay refuses, and it must
    // leave the process failing whether or not armPending() ran first.
    AutomationVerdictLatch latch;
    latch.fail();
    ASSERT_EQ( 14, latch.exitCode() );
    ASSERT_TRUE( latch.pending() );
}

TEST( AutomationVerdictLatch, ArmedIsStickyAndSurvivesAReachedVerdict )
{
    // r2 (fable H3): closeEvent skips the save-session prompt for an automation run, so "armed" must outlive the
    // verdict: a Reached latch is no longer pending, but the process was still an automation run.
    AutomationVerdictLatch latch;
    ASSERT_FALSE( latch.armed() );
    latch.armPending();
    ASSERT_TRUE( latch.armed() );
    latch.resolve( PlayStopState::Reached );
    ASSERT_FALSE( latch.pending() );
    ASSERT_TRUE( latch.armed() );
}

TEST( AutomationVerdictLatch, ARefusalAloneArmsTheLatch )
{
    AutomationVerdictLatch latch;
    latch.fail();
    ASSERT_TRUE( latch.armed() );
    ASSERT_TRUE( latch.pending() );
}

TEST( RunNonce, AValidNonceIsEchoedVerbatim )
{
    ASSERT_EQ( std::string( "n0123456789abcdef0123456789abcdef" ),
               playback_frame_range::sanitizeRunNonce( "n0123456789abcdef0123456789abcdef" ) );
    ASSERT_EQ( std::string( "ABCdef12" ), playback_frame_range::sanitizeRunNonce( "ABCdef12" ) );                 // the 8-char floor
}

TEST( RunNonce, UnsetOrMalformedIsTheNoNonceToken )
{
    // A launcher never generates "none", so a receipt from an app that was not handed (or mangled) the nonce can never
    // match one.
    const std::string none = playback_frame_range::noRunNonce();
    ASSERT_EQ( std::string( "none" ), none );
    ASSERT_EQ( none, playback_frame_range::sanitizeRunNonce( nullptr ) );
    ASSERT_EQ( none, playback_frame_range::sanitizeRunNonce( "" ) );
    ASSERT_EQ( none, playback_frame_range::sanitizeRunNonce( "short" ) );                                         // < 8
    ASSERT_EQ( none, playback_frame_range::sanitizeRunNonce( std::string( 65, 'a' ).c_str() ) );                  // > 64
    ASSERT_EQ( none, playback_frame_range::sanitizeRunNonce( "has space in it" ) );                               // would split the log line
    ASSERT_EQ( none, playback_frame_range::sanitizeRunNonce( "quote\"injection12" ) );                            // would break the JSON
    ASSERT_EQ( none, playback_frame_range::sanitizeRunNonce( "key=value12345678" ) );                             // would forge a log field
    ASSERT_EQ( std::string( 64, 'a' ), playback_frame_range::sanitizeRunNonce( std::string( 64, 'a' ).c_str() ) ); // the 64-char ceiling
}

// ---------------------------------------------------------------------------------------------------------
// CPU-LOOK-LEG-PACE-ABORT-1: a CPU-backend leg's pace is MEASURED and INFORMATIONAL. The in-Play pace probe never aborts
// it; the wall budget is the CPU ceiling (requested / kCpuInformationalMinPaceFraction + margin) and running into it is
// the typed PLAY_SAFETY_TIMEOUT. A gated (CUDA / interactive-automation) run is byte-for-byte what it was.
// ---------------------------------------------------------------------------------------------------------
namespace
{
using playback_frame_range::PlayPaceMode;
}

TEST( CpuPaceInformational, TheDefaultModeIsGatedAndTheGatedBudgetIsUnchanged )
{
    ASSERT_TRUE( playback_frame_range::playPaceModeFromEnvironmentValue( nullptr ) == PlayPaceMode::Gated );
    ASSERT_TRUE( playback_frame_range::playPaceModeFromEnvironmentValue( "" ) == PlayPaceMode::Gated );
    ASSERT_TRUE( playback_frame_range::playPaceModeFromEnvironmentValue( "gated" ) == PlayPaceMode::Gated );
    // Anything but the exact word fails closed to the gate (a typo never switches the gate off).
    ASSERT_TRUE( playback_frame_range::playPaceModeFromEnvironmentValue( "Informational" ) == PlayPaceMode::Gated );
    ASSERT_TRUE( playback_frame_range::playPaceModeFromEnvironmentValue( "informational " ) == PlayPaceMode::Gated );
    ASSERT_TRUE( playback_frame_range::playPaceModeFromEnvironmentValue( "informational" ) == PlayPaceMode::MeasuredInformational );
    ASSERT_EQ( 65000, playSafetyMs( 25.0 ) );
    ASSERT_EQ( 65000, playSafetyMs( 25.0, PlayPaceMode::Gated ) );
}

TEST( CpuPaceInformational, TheCpuCeilingIsStatedAndBoundedAndAboveTheGatedBudget )
{
    // 25 s of footage / (1/30) + 15 s = 765 s (12.75 min): the one hard ceiling of a CPU leg.
    ASSERT_EQ( 765000, playSafetyMs( 25.0, PlayPaceMode::MeasuredInformational ) );
    ASSERT_TRUE( playSafetyMs( 25.0, PlayPaceMode::MeasuredInformational ) > playSafetyMs( 25.0 ) );
    ASSERT_EQ( 615000, playSafetyMs( 20.0, PlayPaceMode::MeasuredInformational ) );
}

TEST( CpuPaceInformational, ACpuRunAtFourFpsIsNotAbortedByThePaceProbeAndGetsToTheRequirement )
{
    // 600 source frames at 4 fps = 150 s: the gated budget (65 s) would have ended it at 8 s as PLAY_PACE_TOO_SLOW.
    const int64_t gated = playSafetyMs( 25.0 );
    ASSERT_TRUE( evaluatePlayStop( 34, 600, true, 8050, gated ) == PlayStopState::PaceTooSlow );   // the 10/3 UM evidence
    const int64_t cpu = playSafetyMs( 25.0, PlayPaceMode::MeasuredInformational );
    ASSERT_TRUE( evaluatePlayStop( 34, 600, true, 8050, cpu, PlayPaceMode::MeasuredInformational ) == PlayStopState::Continue );
    const EngineRun run = runEngine( 0, 1, 720, 4.0, 600, cpu, 0, false, PlayPaceMode::MeasuredInformational );
    ASSERT_TRUE( run.state == PlayStopState::Reached );
    ASSERT_TRUE( run.consumed >= 600 );
    ASSERT_TRUE( run.elapsedMs > 140000 && run.elapsedMs < 160000 );
    ASSERT_FALSE( run.wrapped );
}

TEST( CpuPaceInformational, TheSameRunUnderTheGatedModeStillAbortsExactlyAsBefore )
{
    const int64_t gated = playSafetyMs( 25.0 );
    const EngineRun run = runEngine( 0, 1, 720, 4.0, 600, gated );
    ASSERT_TRUE( run.state == PlayStopState::PaceTooSlow );
    ASSERT_TRUE( run.elapsedMs >= playback_frame_range::kPaceProbeMs && run.elapsedMs < 9000 );
    ASSERT_TRUE( run.consumed < 600 );
}

TEST( CpuPaceInformational, ACpuRunBelowTheCeilingPaceEndsWithTheTypedSafetyTimeoutNeverAHang )
{
    // 0.5 fps: 600 frames would need 1200 s, past the 765 s ceiling -> PLAY_SAFETY_TIMEOUT at the ceiling, never Reached.
    const int64_t cpu = playSafetyMs( 25.0, PlayPaceMode::MeasuredInformational );
    const EngineRun run = runEngine( 0, 1, 720, 0.5, 600, cpu, 0, false, PlayPaceMode::MeasuredInformational );
    ASSERT_TRUE( run.state == PlayStopState::SafetyTimeout );
    ASSERT_EQ( cpu, run.elapsedMs );
    ASSERT_TRUE( run.consumed < 600 );
    ASSERT_EQ( std::string( "PLAY_SAFETY_TIMEOUT" ),
               std::string( playback_frame_range::playStopFailureReason( run.state ) ) );
    // A pace just under the ceiling (0.9 fps -> 667 s) still finishes.
    const EngineRun slow = runEngine( 0, 1, 720, 0.9, 600, cpu, 0, false, PlayPaceMode::MeasuredInformational );
    ASSERT_TRUE( slow.state == PlayStopState::Reached );
}

TEST( CpuPaceInformational, TheInformationalModeNeverRelaxesTheOtherEndsOfTheRun )
{
    const int64_t cpu = playSafetyMs( 25.0, PlayPaceMode::MeasuredInformational );
    // A Play that ended first is still EndedEarly, Reached still wins, the wall ceiling still times out.
    ASSERT_TRUE( evaluatePlayStop( 100, 600, false, 5000, cpu, PlayPaceMode::MeasuredInformational ) == PlayStopState::EndedEarly );
    ASSERT_TRUE( evaluatePlayStop( 600, 600, true, 5000, cpu, PlayPaceMode::MeasuredInformational ) == PlayStopState::Reached );
    ASSERT_TRUE( evaluatePlayStop( 100, 600, true, cpu, cpu, PlayPaceMode::MeasuredInformational ) == PlayStopState::SafetyTimeout );
    // A loop never reaches the requirement by replaying footage, in either mode.
    const EngineRun loop = runEngine( 0, 1, 100, 24.0, 600, cpu, 0, true, PlayPaceMode::MeasuredInformational );
    ASSERT_FALSE( loop.state == PlayStopState::Reached );
    ASSERT_TRUE( loop.consumed <= 100 );
    ASSERT_TRUE( loop.wrapped );
    // required <= 0 can never be reached.
    ASSERT_FALSE( evaluatePlayStop( 5, 0, true, 100, cpu, PlayPaceMode::MeasuredInformational ) == PlayStopState::Reached );
}

TEST( CpuPaceInformational, TheMeasuredPaceIsFramesAdvancedOverPlayWallClockAndFailsClosedToZero )
{
    // 34 frames consumed = 33 steps in 8.05 s.
    ASSERT_TRUE( std::abs( playback_frame_range::measuredPaceFps( 34, 8050 ) - 33.0 / 8.05 ) < 1e-9 );
    ASSERT_EQ( 0.0, playback_frame_range::measuredPaceFps( 1, 8050 ) );
    ASSERT_EQ( 0.0, playback_frame_range::measuredPaceFps( 0, 8050 ) );
    ASSERT_EQ( 0.0, playback_frame_range::measuredPaceFps( 34, 0 ) );
}

TEST( CpuPaceInformational, AdmissionUsesTheModeBudgetAndStillRefusesAnUnusablePace )
{
    // Nominal pace 24 fps is admitted in both modes; the verdict reports the budget of ITS mode.
    PlayableWindowVerdict g = evaluatePlayableWindow( 0, 1, 720, 720, 24.0, 25.0 );
    ASSERT_TRUE( g.ok );
    ASSERT_EQ( 65.0, g.wallBudgetSeconds );
    PlayableWindowVerdict c = evaluatePlayableWindow( 0, 1, 720, 720, 24.0, 25.0, 20.0, true,
                                                      playback_frame_range::kPaceIsNative, PlayPaceMode::MeasuredInformational );
    ASSERT_TRUE( c.ok );
    ASSERT_EQ( 765.0, c.wallBudgetSeconds );
    // A nominal pace of 5 fps needs 120 s: refused under the gate, admitted under the CPU ceiling.
    ASSERT_FALSE( evaluatePlayableWindow( 0, 1, 720, 720, 24.0, 25.0, 20.0, true, 5.0 ).ok );
    ASSERT_TRUE( evaluatePlayableWindow( 0, 1, 720, 720, 24.0, 25.0, 20.0, true, 5.0, PlayPaceMode::MeasuredInformational ).ok );
    // An unusable pace (<= 0) and a nominal pace past the CPU ceiling still fail closed with the typed reason.
    const PlayableWindowVerdict z = evaluatePlayableWindow( 0, 1, 720, 720, 24.0, 25.0, 20.0, true, -1.0, PlayPaceMode::MeasuredInformational );
    ASSERT_FALSE( z.ok );
    ASSERT_EQ( std::string( "PLAY_PACE_TOO_SLOW" ), std::string( z.reason ) );
    ASSERT_FALSE( evaluatePlayableWindow( 0, 1, 720, 720, 24.0, 25.0, 20.0, true, 0.5, PlayPaceMode::MeasuredInformational ).ok );
}
