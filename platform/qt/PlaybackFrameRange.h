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

// PlaybackWrapRecorder -- PLAYBACK-CLIP-LENGTH-ENFORCE-1 round 2 (sol BLOCKER 4), widened in ENFORCE-2: the
// runtime backstop against a looped or replayed clip. The AUTHORITATIVE wrap signal is noteEngineWrap(), called
// from the engine's own wrap branches in MainWindow::playbackHandling (the Loop branch that jumps the slider
// back to cutIn, and advanceDropFrameTick's `wrapped` result). ENFORCE-2 adds the two NON-Loop ways the same
// footage plays again: noteJumpToFirst() (Play pressed on the last frame jumps to the first frame, in
// on_actionPlay_triggered) and noteRestart() (any Play start after the first in the process). The
// presented-frame heuristic (noteInferredWrap(), from isContactSheetLoopWrapTransition) stays only as a SECOND
// signal: it misses a genuine wrap whenever dropped frames near the boundary make the last presented frame lie
// more than 8 frames short of the range end (e.g. 700 -> 0 over a 0..719 range jumps back by 700, under the 711
// threshold). wrapped() is true if ANY of them fired, so a replay can never escape on the strength of one
// signal alone; replayCount() is the figure the summary reports as wrap_count.
struct PlaybackWrapRecorder
{
    int engineWraps = 0;
    int jumpToFirstCount = 0;
    int restartCount = 0;
    bool inferredWrap = false;

    void noteEngineWrap() { ++engineWraps; }
    void noteJumpToFirst() { ++jumpToFirstCount; }
    void noteRestart() { ++restartCount; }
    void noteInferredWrap() { inferredWrap = true; }
    int replayCount() const { return engineWraps + jumpToFirstCount + restartCount; }
    bool wrapped() const { return replayCount() > 0 || inferredWrap; }
};

// ---------------------------------------------------------------------------------------------------------
// PLAYBACK-CLIP-LENGTH-ENFORCE-2 (owner rule 2026-09-30): THE APP IS THE GATE.
//
// No programmatic Play (autoplay hook, profile exercise modes, GUI-smoke measured Play, ...) may start unless
// the footage that Play would actually cover -- from the CURRENT position to the receipt's cut-out -- is at
// least kMinPlayWindowSeconds AND at least the window the caller asked for; and the process admits only ONE
// programmatic Play (a restart, re-Play, stress switch or contact-sheet replay is refused, never re-gated).
// Pressing Play on the last frame jumps to the first frame (on_actionPlay_triggered), which the position-aware
// window covers: from the last frame the window is one frame, so it is refused.
// ---------------------------------------------------------------------------------------------------------
constexpr double kMinPlayWindowSeconds = 20.0;

struct PlayableWindowVerdict
{
    bool ok = false;
    // "" when ok; otherwise a typed reason: CLIP_LENGTH_UNKNOWN | CLIP_TOO_SHORT | REPLAY_REFUSED.
    const char *reason = "CLIP_LENGTH_UNKNOWN";
    // "clip" when the whole clip is shorter than the requirement, "cut_range" when the clip is long enough
    // but the span from the current position to the cut-out is not. Empty when ok / unknown.
    const char *scope = "";
    double clipSeconds = 0.0;
    double playableSeconds = 0.0;
    double requiredSeconds = kMinPlayWindowSeconds;
    int positionFrame = 0;     // 0-based, clamped
    int lastPlayableFrame = 0; // 0-based inclusive: the cut-out frame
    int playableFrames = 0;    // frames positionFrame..lastPlayableFrame inclusive
};

// evaluatePlayableWindow -- pure. positionFrame is the 0-based slider position; cutIn/cutOut are the raw
// spinBoxCutIn/spinBoxCutOut values (1-based, Out inclusive: playbackHandling stops at slider >= cutOut-1).
// The range is normalized exactly as the Play path would (normalizeCutRange with the collapsed-range repair),
// so the window measured here is the window that would really play.
inline PlayableWindowVerdict evaluatePlayableWindow(
    int positionFrame, int cutIn, int cutOut, int totalFrames, double fps,
    double requestedSeconds, double floorSeconds = kMinPlayWindowSeconds )
{
    PlayableWindowVerdict v;
    v.requiredSeconds = std::max( floorSeconds, requestedSeconds );
    if( totalFrames <= 0 || !( fps > 0.0 ) )
    {
        return v; // CLIP_LENGTH_UNKNOWN, fail closed
    }

    v.clipSeconds = static_cast<double>( totalFrames ) / fps;
    const CutRange range = normalizeCutRange( cutIn, cutOut, totalFrames, true );
    v.positionFrame = clampFrameIndex( positionFrame, totalFrames );
    v.lastPlayableFrame = lastFrameIndex( range );
    v.playableFrames = v.lastPlayableFrame >= v.positionFrame
        ? v.lastPlayableFrame - v.positionFrame + 1
        : 0;
    v.playableSeconds = static_cast<double>( v.playableFrames ) / fps;

    if( v.playableSeconds + 1e-9 >= v.requiredSeconds )
    {
        v.ok = true;
        v.reason = "";
        return v;
    }
    v.reason = "CLIP_TOO_SHORT";
    v.scope = v.clipSeconds + 1e-9 < v.requiredSeconds ? "clip" : "cut_range";
    return v;
}

// A --presented-frames target ends playback EARLY, after N presented frames: it is itself a play window of
// N / fps seconds and must reach the floor (a pinned-frame run of 24 frames would play ~1 s). 0 = no early stop.
inline bool presentedFramesTargetReachesFloor( int targetPresentedFrames, double fps,
                                               double floorSeconds = kMinPlayWindowSeconds )
{
    if( targetPresentedFrames <= 0 ) return true;
    return fps > 0.0 && static_cast<double>( targetPresentedFrames ) / fps + 1e-9 >= floorSeconds;
}

// ProgrammaticPlayLedger -- pure. One per process. admit() is the only way a programmatic Play is allowed:
// the window must pass AND no programmatic Play may have been admitted before (replay refused).
struct ProgrammaticPlayLedger
{
    int admitted = 0;
    int refused = 0;
    const char *lastRefusalReason = "";

    bool admit( const PlayableWindowVerdict &window )
    {
        if( !window.ok )
        {
            ++refused;
            lastRefusalReason = window.reason;
            return false;
        }
        if( admitted >= 1 )
        {
            ++refused;
            lastRefusalReason = "REPLAY_REFUSED";
            return false;
        }
        ++admitted;
        return true;
    }
};

} // namespace playback_frame_range

#endif // PLAYBACKFRAMERANGE_H
