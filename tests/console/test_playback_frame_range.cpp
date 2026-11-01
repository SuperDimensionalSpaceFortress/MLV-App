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

// CONTACT-SHEET-SEEK-FRAME-LIST-APP-1: --contact-sheet-seek-frames <list>. The parser accepts only
// ^[0-9]{1,9}(,[0-9]{1,9})*$, strictly ascending, 1..60 items, exactly --contact-sheet-frames of them.
namespace {
std::string seekListToken( const std::string &text, int expectedCount, std::vector<int> *frames = nullptr )
{
    std::vector<int> parsed{ 999 };
    std::string token = "UNSET";
    const bool ok = playback_frame_range::parseContactSheetSeekFrameList( text, expectedCount, &parsed, &token );
    if( frames ) *frames = parsed;
    if( ok ) return token.empty() ? std::string( "OK" ) : std::string( "OK_WITH_TOKEN:" ) + token;
    return token;
}
}

TEST( ContactSheetSeekFrameList, AStrictlyAscendingListOfTheDeclaredCountParsesInOrder )
{
    std::vector<int> frames;
    ASSERT_EQ( std::string( "OK" ), seekListToken( "50,150,250,350,450,550", 6, &frames ) );
    ASSERT_TRUE( frames == std::vector<int>( { 50, 150, 250, 350, 450, 550 } ) );
    ASSERT_EQ( std::string( "OK" ), seekListToken( "0", 1, &frames ) );
    ASSERT_TRUE( frames == std::vector<int>( { 0 } ) );
    // Nine digits is the widest accepted item; leading zeros are digits and parse as the number.
    ASSERT_EQ( std::string( "OK" ), seekListToken( "007,999999999", 2, &frames ) );
    ASSERT_TRUE( frames == std::vector<int>( { 7, 999999999 } ) );
}

TEST( ContactSheetSeekFrameList, EveryMalformedSpellingIsRefusedAsSyntax )
{
    for( const char *text : { "", "50,,150", " 50", "-1", "+5", "1e3", "50,150,", "1234567890",
                              ",50", "50 ,150", "50;150", "0x10", "5.0", "50,150\n" } )
    {
        ASSERT_EQ( std::string( "SEEK_FRAMES_SYNTAX" ), seekListToken( text, 1 ) );
    }
}

TEST( ContactSheetSeekFrameList, ADescendingOrRepeatedFrameIsRefused )
{
    ASSERT_EQ( std::string( "SEEK_FRAMES_NOT_ASCENDING" ), seekListToken( "150,50", 2 ) );
    ASSERT_EQ( std::string( "SEEK_FRAMES_NOT_ASCENDING" ), seekListToken( "50,50", 2 ) );
    ASSERT_EQ( std::string( "SEEK_FRAMES_NOT_ASCENDING" ), seekListToken( "1,2,3,3,4,5", 6 ) );
}

TEST( ContactSheetSeekFrameList, MoreThanSixtyItemsOrAWrongCountIsRefused )
{
    std::string sixtyOne;
    std::string sixty;
    for( int i = 0; i < 61; ++i )
    {
        if( i > 0 ) sixtyOne += ",";
        sixtyOne += std::to_string( i * 10 );
        if( i < 60 )
        {
            if( i > 0 ) sixty += ",";
            sixty += std::to_string( i * 10 );
        }
    }
    ASSERT_EQ( std::string( "SEEK_FRAMES_TOO_MANY" ), seekListToken( sixtyOne, 61 ) );
    ASSERT_EQ( std::string( "SEEK_FRAMES_TOO_MANY" ), seekListToken( sixtyOne, 60 ) );
    ASSERT_EQ( std::string( "OK" ), seekListToken( sixty, 60 ) );
    ASSERT_EQ( std::string( "SEEK_FRAMES_COUNT_MISMATCH" ), seekListToken( "50,150,250,350,450", 6 ) );
    ASSERT_EQ( std::string( "SEEK_FRAMES_COUNT_MISMATCH" ), seekListToken( "50,150,250,350,450,550,650", 6 ) );
}

TEST( ContactSheetSeekFrameList, ARefusalLeavesNoPartialList )
{
    std::vector<int> frames;
    ASSERT_EQ( std::string( "SEEK_FRAMES_NOT_ASCENDING" ), seekListToken( "50,150,100", 3, &frames ) );
    ASSERT_TRUE( frames.empty() );
}

TEST( ContactSheetSeekFrameList, AStillIsHonouredOnlyWhenItSettledOnAValidPresentOfTheRequestedFrame )
{
    using playback_frame_range::contactSheetSeekFrameHonoured;
    ASSERT_TRUE( contactSheetSeekFrameHonoured( 150, true, true, 150 ) );
    ASSERT_FALSE( contactSheetSeekFrameHonoured( 150, false, true, 150 ) );   // never settled
    ASSERT_FALSE( contactSheetSeekFrameHonoured( 150, true, false, 150 ) );   // no presented frame on record
    ASSERT_FALSE( contactSheetSeekFrameHonoured( 150, true, true, 149 ) );    // a different frame is on screen
    ASSERT_FALSE( contactSheetSeekFrameHonoured( 550, true, true, 549 ) );    // the clamp to the last frame
}

TEST( ContactSheetSeekFrameList, TheFirstFramePastTheClipIsNamedBeforeAnyPlay )
{
    using playback_frame_range::firstContactSheetSeekFrameOutOfRange;
    ASSERT_EQ( -1, firstContactSheetSeekFrameOutOfRange( { 50, 150, 549 }, 550 ) );
    ASSERT_EQ( -1, firstContactSheetSeekFrameOutOfRange( { 50, 150, 550 }, 551 ) );
    ASSERT_EQ( 550, firstContactSheetSeekFrameOutOfRange( { 50, 150, 550 }, 550 ) );
    ASSERT_EQ( 550, firstContactSheetSeekFrameOutOfRange( { 50, 550, 650 }, 550 ) );
    // A clip of fewer than two frames cannot be seeked at all (seekAndSettleLoadedClip refuses it).
    ASSERT_EQ( 0, firstContactSheetSeekFrameOutOfRange( { 0 }, 1 ) );
    ASSERT_EQ( 3, firstContactSheetSeekFrameOutOfRange( { 3, 4 }, 0 ) );
    ASSERT_EQ( -1, firstContactSheetSeekFrameOutOfRange( {}, 0 ) );
}
