// PLAYBACK-FPS-METER-MONOTONIC-CLOCK-1-R10-OUTER-BRANCH-RESIDUE-1 r1 (sol r10 blocker on 9c454dd6, the r3 OPEN item pass8-A1 /
// pass9-A1): MainWindow::playbackHandling's outer "on the last frame" branch took every Drop Frame Loop tick from a playhead at or
// past cutOut - 1 (conform off, or past the last frame under conform). It waited for a whole frame of credit, jumped to cut-in and
// booked a lap of loopWrapWidth(slider, cut-in) plus one trace frame. So it bypassed the drop-frame correction contract
// (PlaybackFrameRange.h, advanceDropFrameTick: a start at or past the Loop wrap point is CORRECTED onto its phase in the cut, and a
// zero-width cut HOLDS its one frame). The decision is the pure function playback_frame_range::outerLastFrameBranchHandlesTick,
// which MainWindow calls at the branch. These tests feed sol r10's three INV-4 inputs through a model of that dispatch built from
// the real helpers MainWindow calls, in its order. The model is the play-path normalizer, then the outer-branch decision, then
// either the outer branch's bookkeeping (noteEngineWrap(loopWrapWidth), a lapped source tick, one trace frame, landing on cut-in)
// or the drop-frame engine's (advanceDropFrameTick, the correction rebase, laps only when the tick lapped, the applied frames as
// the trace advance). Every Drop Frame Loop tick now goes to the engine. Normal mode and Loop off keep the outer branch.

#include "../common/minitest.h"
#include "../../platform/qt/PlaybackFrameRange.h"

#include <cmath>

namespace {

struct DropLoopDispatch
{
    bool outerBranch = false;
    double landing = 0.0;
    double traceFrames = 0.0;            // m_playbackTickAppliedFrames, the present trace's advance
    bool corrected = false;
    double correctedStart = 0.0;
    playback_frame_range::PlaybackWrapRecorder recorder;
    playback_frame_range::SourceFrameAdvanceCounter source;
};

// One Drop Frame + Loop playbackHandling tick (conform as given) with \a grantFrames of credit: a whole frame of it, so the outer
// branch's grantLoopWrap would also pass. \a slider is the slider value; \a dropPosition is m_newPosDropMode.
DropLoopDispatch dispatchDropLoopTick( int totalFrames, int slider, double dropPosition, int cutIn, int cutOut, bool conform,
                                       double grantFrames )
{
    DropLoopDispatch d;
    d.source.begin( 0 );   // armed at the Play start on frame 0; the tick's start above it is a rebase, never footage
    // 1. the play-path normalizer (normalizePlaybackCutRangeForLoadedClip)
    const playback_frame_range::CutRange range = playback_frame_range::normalizeCutRange( cutIn, cutOut, totalFrames, true );
    const double position = playback_frame_range::normalizeDropFramePosition( dropPosition, range, totalFrames, conform );
    // 2. the outer last-frame branch decision, exactly as MainWindow passes it (Drop Frame on, Loop on)
    d.outerBranch = playback_frame_range::outerLastFrameBranchHandlesTick( slider, cutOut, true, true, conform );
    if( d.outerBranch )
    {
        // the outer Loop branch: cut-in, one lap of loopWrapWidth, a lapped source tick, one granted transition
        const int cutInFrame = playback_frame_range::clampFrameIndex( cutIn - 1, totalFrames );
        d.recorder.noteEngineWrap( playback_frame_range::loopWrapWidth( slider, cutInFrame ) );
        d.source.noteEngineTick( slider, cutInFrame, true );
        d.traceFrames = 1.0;
        d.landing = cutInFrame;
        return d;
    }
    // 3. the drop-frame engine (non-conform: conformDropFrameDelta returns the grant unchanged)
    const playback_frame_range::DropFrameTickResult tick =
        playback_frame_range::advanceDropFrameTick( position, grantFrames, cutIn, cutOut, true, conform );
    d.traceFrames = tick.appliedFrames;
    d.landing = tick.position;
    d.corrected = tick.corrected;
    d.correctedStart = tick.correctedStart;
    const bool lapped = playback_frame_range::dropFrameTickLapped( tick );
    d.source.noteEngineTick( tick.correctedStart, tick.position, lapped );
    if( tick.corrected ) d.recorder.noteEngineCorrection( playback_frame_range::dropFrameCorrectionFrame( tick.correctedStart ) );
    if( tick.wrapped ) d.recorder.noteEngineWrap( tick.wrapWidth, tick.wrapLaps );
    return d;
}

} // namespace

// sol r10 INV-4 row 1: 200-frame clip, Drop Frame and Loop on, conform off, slider and drop position 100, Cut In 1 and Cut Out
// lowered to 50, one whole frame of credit. 9c454dd6 landed on 0 with a width-101 lap and one trace frame. The contract
// rebases to frame 2 (100 on its phase in the 49-frame cut [0, 49)) and counts only the travel from there.
TEST( PlaybackOuterLastFrameDispatch, StaleSliderPastAnEditedCutOutRebasesOntoItsPhaseWithNoLap )
{
    ASSERT_FALSE( playback_frame_range::outerLastFrameBranchHandlesTick( 100, 50, true, true, false ) );
    const double grants[] = { 1.0, 0.192 };
    for( const double grant : grants )
    {
        const DropLoopDispatch d = dispatchDropLoopTick( 200, 100, 100.0, 1, 50, false, grant );
        ASSERT_FALSE( d.outerBranch );
        ASSERT_TRUE( d.corrected );
        ASSERT_EQ( 2.0, d.correctedStart );
        ASSERT_EQ( 0, d.recorder.engineWraps );                      // no lap: the fold is a correction, never travel
        ASSERT_EQ( 1, d.recorder.engineCorrections );
        ASSERT_EQ( 2, d.recorder.engineCorrectionRecords.front().rebaseFrame );
        ASSERT_NEAR( 2.0 + grant, d.landing, 1e-12 );
        ASSERT_NEAR( grant, d.traceFrames, 1e-12 );                  // only the travel from the corrected start
        ASSERT_EQ( 0, d.source.wrapOrBackwardTicks );
        ASSERT_EQ( static_cast<int64_t>( std::floor( 2.0 + grant + 1e-9 ) ) - 2, d.source.forwardSteps );
    }
}

// sol r10 INV-4 row 2: a terminal one-frame cut. 1000-frame clip, slider 999, cut 1000..1000, same modes and credit. 9c454dd6
// held frame 999 yet booked a width-1 lap and one trace frame. The zero-width cut holds its one frame: no lap, no trace frame.
TEST( PlaybackOuterLastFrameDispatch, TerminalOneFrameCutHoldsWithNoLapAndNoTraceFrame1000 )
{
    ASSERT_FALSE( playback_frame_range::outerLastFrameBranchHandlesTick( 999, 1000, true, true, false ) );
    const DropLoopDispatch d = dispatchDropLoopTick( 1000, 999, 999.0, 1000, 1000, false, 1.0 );
    ASSERT_FALSE( d.outerBranch );
    ASSERT_EQ( 999.0, d.landing );
    ASSERT_FALSE( d.corrected );
    ASSERT_EQ( 0, d.recorder.engineWraps );
    ASSERT_EQ( 0, d.recorder.engineCorrections );
    ASSERT_EQ( 0.0, d.traceFrames );
    ASSERT_EQ( 0, d.source.wrapOrBackwardTicks );
    ASSERT_EQ( static_cast<int64_t>( 0 ), d.source.forwardSteps );
}

// sol r10 INV-4 row 3: 41-frame clip, slider 40, terminal cut 41..41, same modes and credit.
TEST( PlaybackOuterLastFrameDispatch, TerminalOneFrameCutHoldsWithNoLapAndNoTraceFrame41 )
{
    ASSERT_FALSE( playback_frame_range::outerLastFrameBranchHandlesTick( 40, 41, true, true, false ) );
    const DropLoopDispatch d = dispatchDropLoopTick( 41, 40, 40.0, 41, 41, false, 1.0 );
    ASSERT_FALSE( d.outerBranch );
    ASSERT_EQ( 40.0, d.landing );
    ASSERT_FALSE( d.corrected );
    ASSERT_EQ( 0, d.recorder.engineWraps );
    ASSERT_EQ( 0, d.recorder.engineCorrections );
    ASSERT_EQ( 0.0, d.traceFrames );
    ASSERT_EQ( 0, d.source.wrapOrBackwardTicks );
    ASSERT_EQ( static_cast<int64_t>( 0 ), d.source.forwardSteps );
}

// Adversary pass 10 A1 on 87a6d64b: the play-path normalizer truncated a stale fractional start above the cut (100.9 -> 100) before
// the engine folded it, so the fold lost the start's phase: Cut Out lowered from 200 to 50 under 100.9 landed at 2.192 (slider 2)
// where the phase is 2.9 + 0.192 = 3.092 (slider 3) without conform, and at 0.192 where it is 0.9 + 0.192 = 1.092 with conform.
TEST( PlaybackOuterLastFrameDispatch, AStaleFractionalStartAboveTheCutKeepsItsPhaseThroughTheNormalizer )
{
    const DropLoopDispatch plain = dispatchDropLoopTick( 200, 100, 100.9, 1, 50, false, 0.192 );
    ASSERT_FALSE( plain.outerBranch );
    ASSERT_TRUE( plain.corrected );
    ASSERT_NEAR( 2.9, plain.correctedStart, 1e-9 );
    ASSERT_NEAR( 3.092, plain.landing, 1e-9 );
    ASSERT_NEAR( 0.192, plain.traceFrames, 1e-12 );
    ASSERT_EQ( 0, plain.recorder.engineWraps );
    ASSERT_EQ( 2, plain.recorder.engineCorrectionRecords.front().rebaseFrame );
    const DropLoopDispatch conformed = dispatchDropLoopTick( 200, 100, 100.9, 1, 50, true, 0.192 );
    ASSERT_FALSE( conformed.outerBranch );
    ASSERT_TRUE( conformed.corrected );
    ASSERT_NEAR( 0.9, conformed.correctedStart, 1e-9 );
    ASSERT_NEAR( 1.092, conformed.landing, 1e-9 );
    ASSERT_EQ( 0, conformed.recorder.engineWraps );
    // the repair outside the clip and below the cut is unchanged: truncated, clamped to the clip
    playback_frame_range::CutRange cut;
    cut.cutIn = 1;
    cut.cutOut = 50;
    cut.valid = true;
    ASSERT_EQ( 199.0, playback_frame_range::normalizeDropFramePosition( 250.7, cut, 200, false ) );
    ASSERT_EQ( 100.9, playback_frame_range::normalizeDropFramePosition( 100.9, cut, 200, false ) );
    playback_frame_range::CutRange late;
    late.cutIn = 41;
    late.cutOut = 50;
    late.valid = true;
    ASSERT_EQ( 10.0, playback_frame_range::normalizeDropFramePosition( 10.75, late, 200, false ) );
}

// Sibling sweep (the same dispatch): a playhead on the last frame (conform off) and one past it under conform are the engine's
// too, so they fold through the contract rather than book an outer-branch lap. Normal mode and Loop off keep the outer branch:
// Normal mode has no drop-frame correction contract, and Loop off stops playback on the last frame.
TEST( PlaybackOuterLastFrameDispatch, EveryDropFrameLoopTickIsTheEnginesAndOtherModesKeepTheOuterBranch )
{
    using playback_frame_range::outerLastFrameBranchHandlesTick;
    for( const bool conform : { false, true } )
    {
        ASSERT_FALSE( outerLastFrameBranchHandlesTick( 718, 720, true, true, conform ) );   // inside the cut: never
        ASSERT_FALSE( outerLastFrameBranchHandlesTick( 719, 720, true, true, conform ) );   // the last frame: the engine's
        ASSERT_FALSE( outerLastFrameBranchHandlesTick( 900, 720, true, true, conform ) );   // past it: the engine corrects it
        ASSERT_TRUE( outerLastFrameBranchHandlesTick( 719, 720, false, true, conform ) );   // Normal mode: unchanged
        ASSERT_TRUE( outerLastFrameBranchHandlesTick( 900, 720, false, true, conform ) );
        ASSERT_TRUE( outerLastFrameBranchHandlesTick( 719, 720, true, false, conform ) );   // Loop off: stop on the last frame
        ASSERT_TRUE( outerLastFrameBranchHandlesTick( 900, 720, true, false, conform ) );
        ASSERT_FALSE( outerLastFrameBranchHandlesTick( 718, 720, false, true, conform ) );
    }
    // conform off, the slider on the last frame (a seek there): a start at the wrap point, corrected onto the cut's first frame
    // with no lap, then the granted travel from it
    const DropLoopDispatch onLast = dispatchDropLoopTick( 720, 719, 719.0, 1, 720, false, 1.0 );
    ASSERT_FALSE( onLast.outerBranch );
    ASSERT_TRUE( onLast.corrected );
    ASSERT_EQ( 0.0, onLast.correctedStart );
    ASSERT_EQ( 0, onLast.recorder.engineWraps );
    ASSERT_EQ( 1.0, onLast.landing );
    ASSERT_EQ( 1.0, onLast.traceFrames );
    // conform on, Cut Out lowered to 460 under a playhead at 500 (720-frame clip): folded onto its phase in the 460-frame cut, no lap
    const DropLoopDispatch past = dispatchDropLoopTick( 720, 500, 500.3, 1, 460, true, 0.25 );
    ASSERT_FALSE( past.outerBranch );
    ASSERT_TRUE( past.corrected );
    ASSERT_EQ( 0, past.recorder.engineWraps );
    ASSERT_TRUE( past.correctedStart >= 0.0 && past.correctedStart < 460.0 );
    ASSERT_NEAR( 0.25, past.traceFrames, 1e-12 );
}
