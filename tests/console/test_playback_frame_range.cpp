#include "../common/minitest.h"
#include "../../platform/qt/PlaybackFrameRange.h"

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
    using playback_frame_range::kMinPlayWindowMs;
    using playback_frame_range::stressSwitchReachesFloor;
    ASSERT_EQ( 20000, kMinPlayWindowMs );
    ASSERT_FALSE( stressSwitchReachesFloor( 0 ) );
    ASSERT_FALSE( stressSwitchReachesFloor( 1000 ) );      // the pre-fix default: Play stopped after ~1 s
    ASSERT_FALSE( stressSwitchReachesFloor( 19999 ) );
    ASSERT_TRUE( stressSwitchReachesFloor( 20000 ) );
    ASSERT_TRUE( stressSwitchReachesFloor( 24000 ) );
    ASSERT_FALSE( stressSwitchReachesFloor( -1 ) );
}

// sol B2: a hold for a Play that an exercise mode ends early (Look Assist settle, play-action smoke).
TEST( PlayableWindow, AnExerciseModeMayStopPlayOnlyOnceTheFloorHasElapsed )
{
    using playback_frame_range::playHoldReachedFloor;
    ASSERT_FALSE( playHoldReachedFloor( 0 ) );
    ASSERT_FALSE( playHoldReachedFloor( 12000 ) );   // the old Look Assist settle deadline
    ASSERT_FALSE( playHoldReachedFloor( 5000 ) );    // the old play-action timeout
    ASSERT_FALSE( playHoldReachedFloor( 19999 ) );
    ASSERT_TRUE( playHoldReachedFloor( 20000 ) );
}
