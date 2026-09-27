/*!
 * \file PlaybackFrameRange.h
 * \brief Small playback frame/cut-range normalization helpers.
 */

#ifndef PLAYBACKFRAMERANGE_H
#define PLAYBACKFRAMERANGE_H

#include <algorithm>
#include <cmath>
#include <cstdint>

namespace playback_frame_range {

struct CutRange
{
    int cutIn = 0;
    int cutOut = 0;
    bool valid = false;
    bool changed = false;
};

inline int clampFrameIndex( int requestedFrame, int totalFrames, bool *changed = nullptr )
{
    if( totalFrames <= 0 )
    {
        if( changed ) *changed = requestedFrame != 0;
        return 0;
    }

    const int clamped = std::max( 0, std::min( requestedFrame, totalFrames - 1 ) );
    if( changed ) *changed = clamped != requestedFrame;
    return clamped;
}

inline bool isValidFrameNumber( uint32_t frameNumber, int totalFrames )
{
    return totalFrames > 0 && frameNumber < static_cast<uint32_t>( totalFrames );
}

// repairCollapsedRangeForPlay widens a genuinely one-frame-wide range (cutIn == cutOut)
// out to the end of the clip. Left false (the default) a collapsed range is a valid,
// deliberate single-frame trim and must pass through untouched; callers on the actual
// play path opt in so pressing Play on a locked single frame still plays something.
inline CutRange normalizeCutRange( int cutIn, int cutOut, int totalFrames,
                                    bool repairCollapsedRangeForPlay = false )
{
    CutRange result;
    if( totalFrames <= 0 )
    {
        result.changed = cutIn != 0 || cutOut != 0;
        return result;
    }

    result.valid = true;
    result.cutIn = std::max( 1, std::min( cutIn, totalFrames ) );
    if( cutOut < result.cutIn || cutOut > totalFrames )
    {
        result.cutOut = totalFrames;
    }
    else
    {
        result.cutOut = cutOut;
    }
    if( result.cutOut < result.cutIn )
    {
        result.cutOut = result.cutIn;
    }

    if( repairCollapsedRangeForPlay
     && result.cutOut == result.cutIn
     && result.cutIn < totalFrames )
    {
        result.cutOut = totalFrames;
    }

    result.changed = result.cutIn != cutIn || result.cutOut != cutOut;
    return result;
}

inline int firstFrameIndex( const CutRange &range )
{
    return range.valid ? range.cutIn - 1 : 0;
}

inline int lastFrameIndex( const CutRange &range )
{
    return range.valid ? range.cutOut - 1 : 0;
}

// contactSheetTargetFrame computes the i-th of frameCount evenly-spaced target frames across
// [startFrame, endFrame] inclusive (fraction 0.0 at i==0, 1.0 at i==frameCount-1). Mirrors
// MainWindow::runGuiPlaybackSmoke's contactSheetTargetFrames loop exactly, shared so both the
// seek-mode and playback-mode capture paths (and their tests) always agree on the same targets.
inline int contactSheetTargetFrame( int i, int frameCount, int startFrame, int endFrame )
{
    const double fraction = frameCount > 1
        ? static_cast<double>( i ) / static_cast<double>( frameCount - 1 )
        : 0.0;
    return startFrame + static_cast<int>(
        std::lround( fraction * static_cast<double>( endFrame - startFrame ) ) );
}

struct DropFrameTickResult
{
    double position = 0.0;
    bool wrapped = false;
};

// advanceDropFrameTick mirrors MainWindow::playbackHandling's drop-frame-mode per-tick position
// update exactly (MainWindow.cpp ~10432-10447): add this tick's frame delta, then either wrap
// back by the loop-range width (loop enabled, the new position reached/passed the range's last
// frame) or clamp to the last frame (loop disabled). cutInValue/cutOutValue are the raw
// spinBoxCutIn/spinBoxCutOut values (1-based), matching the call site's own convention.
//
// BLOCKER (CUDA-PLAYBACK-CONTACT-SHEET-2 round 2): the wrap check fires on the position this
// tick is ABOUT to reach and subtracts before that position is ever returned/presented, so with
// loopEnabled a position of exactly cutOutValue-1 (the range's last frame) can never be
// returned by this function -- proven by the round-2 executable test, not just asserted.
inline DropFrameTickResult advanceDropFrameTick(
    double currentPosition, double frameDelta, int cutInValue, int cutOutValue,
    bool loopEnabled )
{
    DropFrameTickResult result;
    result.position = currentPosition + frameDelta;
    const double lastFrame = static_cast<double>( cutOutValue - 1 );
    if( loopEnabled && result.position >= lastFrame )
    {
        result.position -= static_cast<double>( cutOutValue - cutInValue );
        result.wrapped = true;
    }
    else if( result.position >= lastFrame )
    {
        result.position = lastFrame;
    }
    return result;
}

// isContactSheetLoopWrapTransition decides whether a presented-frame transition from
// lastPresentedFrame to displayFrame (both 0-based) is a genuine Loop wrap (cutOut back to
// cutIn) rather than an external backward scrub or a stress seek landing on an arbitrary
// earlier frame. A real wrap only ever jumps back by (close to) the whole loop-range width:
// exactly cutOutFrame-cutInFrame frames for the non-drop-mode path (MainWindow.cpp
// ~10365-10394, goto cutIn), or that width minus at most one tick's drop-frame overshoot for
// the drop-frame path (advanceDropFrameTick above). cutInFrame/cutOutFrame are 0-based
// (spinBoxCutIn/spinBoxCutOut value() - 1), matching m_playbackSmokeLastPresentedFrame's own
// convention.
//
// HARDENING (CUDA-PLAYBACK-CONTACT-SHEET-2 round 2, LOOP-WRAP-QUALIFICATION): a bare
// "displayFrame < lastPresentedFrame" test (the pre-round-2 logic) also fires for a backward
// scrub during a NON-looping session, or any stress seek to an arbitrary earlier frame -- this
// requires Loop to be active and the jump to be consistent with an actual wrap.
inline bool isContactSheetLoopWrapTransition(
    bool loopActive, int cutInFrame, int cutOutFrame,
    int lastPresentedFrame, int displayFrame )
{
    if( !loopActive ) return false;
    const int loopWidth = cutOutFrame - cutInFrame;
    if( loopWidth <= 0 ) return false;
    const int backwardJump = lastPresentedFrame - displayFrame;
    if( backwardJump <= 0 ) return false;
    // Generous tolerance for the drop-frame path's per-tick overshoot (the amount by which a
    // tick's pre-wrap position could exceed cutOutFrame-1 before being subtracted back down);
    // far smaller than any realistic loop-range width, so an unrelated backward scrub to an
    // arbitrary earlier position essentially never satisfies this by coincidence.
    const int kOvershootToleranceFrames = 8;
    return backwardJump >= loopWidth - kOvershootToleranceFrames;
}

} // namespace playback_frame_range

#endif // PLAYBACKFRAMERANGE_H
