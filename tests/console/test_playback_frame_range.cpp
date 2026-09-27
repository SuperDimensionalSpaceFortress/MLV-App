#include "../common/minitest.h"
#include "../../platform/qt/PlaybackFrameRange.h"

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
